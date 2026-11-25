from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import uuid
from collections import defaultdict, deque
from datetime import date, datetime, timedelta, timezone
from typing import Any, AsyncIterator

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.agent import run_queue
from app.api.deps import check_origin, require_principal
from app.config import get_settings
from app.db import tenant_scope
from app.models import Conversation, Message, UsageDaily

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", tags=["chat"])

class ChatBody(BaseModel):
    conversation_id: uuid.UUID | None = None
    client_request_id: str | None = Field(default=None, max_length=128)
    message: str = Field(min_length=1, max_length=2000)

_request_windows: dict[uuid.UUID, deque[float]] = defaultdict(deque)
_request_lock = threading.Lock()

_SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "X-Accel-Buffering": "no",
    "Connection": "keep-alive",
}

def _check_rate(user_id: uuid.UUID) -> None:
    now = time.monotonic()
    with _request_lock:
        window = _request_windows[user_id]
        while window and now - window[0] > 60:
            window.popleft()
        if len(window) >= 20:
            raise HTTPException(
                status_code=429,
                detail={"code": "RATE_LIMITED", "message": "请求过于频繁，请稍后再试"},
                headers={"Retry-After": "60"},
            )
        window.append(now)

def _check_daily_quota(db: Any, user_id: uuid.UUID) -> None:
    row = db.scalar(
        select(UsageDaily).where(
            UsageDaily.user_id == user_id,
            UsageDaily.day == date.today(),
        )
    )
    if row is not None and row.questions >= 50:
        raise HTTPException(status_code=429, detail={"code": "DAILY_QUESTION_LIMIT", "message": "今日提问次数已达上限（50）"})
    if row is not None and row.tool_calls >= 400:
        raise HTTPException(status_code=429, detail={"code": "DAILY_LIMIT_REACHED", "message": "今日用量已达上限，请明天再试"})

def _sse(event: str, data: dict[str, Any], event_id: int | None = None) -> str:
    payload = json.dumps(data, ensure_ascii=False, default=str, separators=(",", ":"))
    head = f"id: {event_id}\n" if event_id is not None else ""
    return f"{head}event: {event}\ndata: {payload}\n\n"

def _constraint_name(exc: IntegrityError) -> str:
    diag = getattr(getattr(exc, "orig", None), "diag", None)
    name = getattr(diag, "constraint_name", None) if diag is not None else None
    if name:
        return str(name)
    return "chat_runs_one_active" if "chat_runs_one_active" in str(exc) else ""

def _is_conversation_busy(exc: IntegrityError) -> bool:
    return _constraint_name(exc) == "chat_runs_one_active"

def _busy_error() -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={"code": "CONVERSATION_BUSY", "message": "该会话上一条回答仍在生成，请稍候再问"},
    )

@router.post("/chat")
def chat(body: ChatBody, request: Request) -> StreamingResponse:
    check_origin(request)
    principal = require_principal(request)
    question = body.message.strip()
    if not question:
        raise HTTPException(status_code=400, detail={"code": "BAD_REQUEST", "message": "问题不能为空"})
    _check_rate(principal.user_id)

    request_id = uuid.uuid4()
    message_id = uuid.uuid4()
    try:
        with tenant_scope(principal.tenant_id, principal.user_id) as db:
            _check_daily_quota(db, principal.user_id)

            if body.client_request_id:
                duplicated = db.scalar(
                    select(Message.id).where(
                        Message.user_id == principal.user_id,
                        Message.client_request_id == body.client_request_id,
                    ).limit(1)
                )
                if duplicated is not None:
                    raise HTTPException(
                        status_code=409,
                        detail={"code": "DUPLICATE_REQUEST", "message": "该请求已提交过，请刷新会话查看结果"},
                    )

            if body.conversation_id is None:
                conversation = Conversation(
                    id=uuid.uuid4(),
                    tenant_id=principal.tenant_id,
                    user_id=principal.user_id,
                    title=question[:36],
                )
                db.add(conversation)
                db.flush()
            else:
                conversation = db.scalar(
                    select(Conversation).where(
                        Conversation.id == body.conversation_id,
                        Conversation.deleted_at.is_(None),
                    )
                )
                if conversation is None:
                    raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "会话不存在"})
                if conversation.title in {"", "新会话"}:
                    conversation.title = question[:36]

            assistant_count = db.scalar(
                select(func.count(Message.id)).where(
                    Message.conversation_id == conversation.id,
                    Message.role == "assistant",
                )
            )
            turn = int(assistant_count or 0) + 1

            created_at = datetime.now(timezone.utc)
            db.add(
                Message(
                    id=uuid.uuid4(),
                    tenant_id=principal.tenant_id,
                    user_id=principal.user_id,
                    conversation_id=conversation.id,
                    request_id=request_id,
                    client_request_id=body.client_request_id,
                    role="user",
                    content=question,
                    status="done",
                    meta={},
                    created_at=created_at,
                )
            )
            db.add(
                Message(
                    id=message_id,
                    tenant_id=principal.tenant_id,
                    user_id=principal.user_id,
                    conversation_id=conversation.id,
                    request_id=request_id,
                    client_request_id=None,
                    role="assistant",
                    content="",
                    status="queued",
                    meta={},
                    created_at=created_at + timedelta(microseconds=1),
                )
            )
            conversation.updated_at = created_at
            db.flush()
            conversation_id = conversation.id

            try:
                run_queue.enqueue(
                    db,
                    tenant_id=principal.tenant_id,
                    user_id=principal.user_id,
                    conversation_id=conversation_id,
                    message_id=message_id,
                    request_id=request_id,
                )
            except IntegrityError as exc:
                if _is_conversation_busy(exc):
                    raise _busy_error() from exc
                raise
    except IntegrityError as exc:
        if _is_conversation_busy(exc):
            raise _busy_error() from exc
        raise HTTPException(status_code=409, detail={"code": "DUPLICATE_REQUEST", "message": "该请求已提交过"}) from exc

    settings = get_settings()
    meta = {
        "conversation_id": str(conversation_id),
        "message_id": str(message_id),
        "request_id": str(request_id),
        "timeout_seconds": settings.task_timeout_seconds,
        "engine": settings.agent_engine,
        "turn": turn,
        "queued": True,
    }
    return StreamingResponse(
        _stream_events(principal.tenant_id, principal.user_id, message_id, 0, initial=meta),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )

@router.get("/messages/{message_id}/events")
def message_events(message_id: uuid.UUID, request: Request, after: int = 0) -> StreamingResponse:
    """断线重连：从 cursor 之后回放事件，并继续跟随到结束。"""
    check_origin(request)
    principal = require_principal(request)
    cursor = max(0, int(after))
    last_event_id = request.headers.get("last-event-id")
    if last_event_id:
        try:
            cursor = max(cursor, int(last_event_id))
        except ValueError:
            pass

    with tenant_scope(principal.tenant_id, principal.user_id) as db:
        found = db.scalar(
            select(Message.id).where(
                Message.id == message_id,
                Message.user_id == principal.user_id,
            )
        )
    if found is None:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "消息不存在"})

    return StreamingResponse(
        _stream_events(principal.tenant_id, principal.user_id, message_id, cursor),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )

async def _stream_events(
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    message_id: uuid.UUID,
    after: int,
    *,
    initial: dict[str, Any] | None = None,
) -> AsyncIterator[str]:
    settings = get_settings()
    cursor = max(0, int(after))
    deadline = time.monotonic() + settings.event_tail_timeout_seconds
    last_keepalive = time.monotonic()

    if initial is not None:
        yield _sse("meta", initial)

    try:
        while True:
            events = await asyncio.to_thread(
                run_queue.read_events,
                tenant_id,
                user_id,
                message_id,
                cursor,
            )
            if events:
                for item in events:
                    cursor = item["id"]
                    yield _sse(item["event"], item["data"], cursor)
                    if item["event"] == "done":
                        return
                continue

            state = await asyncio.to_thread(
                run_queue.message_run_state, tenant_id, user_id, message_id
            )
            if state is None:
                return
            status = str(state.get("status") or "")
            if status == "done":
                message_status = str(state.get("message_status") or "")
                if message_status in {"done", "degraded"}:
                    yield _sse(
                        "done",
                        {"status": message_status, "message_id": str(message_id)},
                    )
                return
            if status in {"failed", "cancelled"}:
                code = str(state.get("error_code") or "RUN_FAILED")
                message = str(state.get("error_message") or "任务执行失败，请重试。")
                yield _sse("error", {"code": code, "message": message})
                yield _sse(
                    "done",
                    {"status": "error", "message_id": str(message_id)},
                )
                return

            now = time.monotonic()
            if now >= deadline:
                logger.info("event tail timeout for message %s at cursor %s", message_id, cursor)
                return
            if now - last_keepalive >= 10:
                last_keepalive = now
                yield ": keepalive\n\n"
            await asyncio.sleep(settings.event_poll_seconds)
    except asyncio.CancelledError:
        raise