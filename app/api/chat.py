from __future__ import annotations

import asyncio
import codecs
import json
import logging
import threading
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, AsyncIterator

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from app.agent import run_queue
from app.agent.persist import failed_message_payload, message_preview
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

@dataclass(frozen=True)
class PreparedTurn:
    conversation_id: uuid.UUID
    message_id: uuid.UUID
    turn: int
    run_id: uuid.UUID | None = None

def _question_of(body: ChatBody) -> str:
    question = body.message.strip()
    if not question:
        raise HTTPException(status_code=400, detail={"code": "BAD_REQUEST", "message": "问题不能为空"})
    return question

def _prepare_turn(
    db: Any,
    principal: Any,
    body: ChatBody,
    question: str,
    request_id: uuid.UUID,
    message_id: uuid.UUID,
    *,
    enqueue: bool = True,
) -> PreparedTurn:
    """写 user/assistant 两条消息；research 入队，chat 直接执行。"""
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

    if not enqueue:
        busy = db.scalar(
            select(func.count(Message.id)).where(
                Message.conversation_id == conversation.id,
                Message.role == "assistant",
                Message.status.in_(["queued", "running"]),
            )
        )
        if busy:
            raise _busy_error()

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
            status="queued" if enqueue else "running",
            meta={},
            created_at=created_at + timedelta(microseconds=1),
        )
    )
    conversation.updated_at = created_at
    db.flush()

    run_id: uuid.UUID | None = None
    if enqueue:
        run_id = run_queue.enqueue(
            db,
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            conversation_id=conversation.id,
            message_id=message_id,
            request_id=request_id,
        )
    return PreparedTurn(
        conversation_id=conversation.id,
        message_id=message_id,
        turn=turn,
        run_id=run_id,
    )

def _start_turn(
    principal: Any,
    body: ChatBody,
    question: str,
    *,
    enqueue: bool = True,
) -> tuple[uuid.UUID, PreparedTurn]:
    request_id = uuid.uuid4()
    message_id = uuid.uuid4()
    try:
        with tenant_scope(principal.tenant_id, principal.user_id) as db:
            prepared = _prepare_turn(
                db,
                principal,
                body,
                question,
                request_id,
                message_id,
                enqueue=enqueue,
            )
    except IntegrityError as exc:
        if _is_conversation_busy(exc):
            raise _busy_error() from exc
        raise HTTPException(status_code=409, detail={"code": "DUPLICATE_REQUEST", "message": "该请求已提交过"}) from exc
    return request_id, prepared

_AGENT_STALL_SECONDS = 30.0
_background_tasks: set[asyncio.Task[Any]] = set()

def _spawn_background(coro: Any) -> None:
    """保持强引用，避免消费任务被 GC；前端断开后它仍要跑完。"""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

def _agent_payload(
    principal: Any,
    prepared: PreparedTurn,
    question: str,
    settings: Any,
) -> dict[str, Any]:
    return {
        "tenant_id": str(principal.tenant_id),
        "user_id": str(principal.user_id),
        "conversation_id": str(prepared.conversation_id),
        "message_id": str(prepared.message_id),
        "question": question,
        "turn": prepared.turn,
        "timeout_seconds": float(settings.chat_timeout_seconds),
    }

def _absorb_sse(chunk: str, state: dict[str, Any]) -> None:
    """边转发边解析：只累计 delta 文本和是否收到 done。"""
    state["buffer"] = str(state.get("buffer") or "") + chunk
    while True:
        index = state["buffer"].find("\n\n")
        if index < 0:
            return
        block = state["buffer"][:index]
        state["buffer"] = state["buffer"][index + 2 :]
        event = ""
        data_lines: list[str] = []
        for raw in block.split("\n"):
            line = raw.rstrip("\r")
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
        if not data_lines:
            continue
        try:
            payload = json.loads("\n".join(data_lines))
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        if event == "delta":
            state["text"] = str(state.get("text") or "") + str(payload.get("text") or "")
        elif event == "delta_reset":
            state["text"] = ""
        elif event == "done":
            state["done"] = True

def _finalize_interrupted_turn(
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
    message_id: uuid.UUID,
    partial: str,
    error: Exception | None,
) -> bool:
    """流在 done 之前结束时收尾；只在消息仍是 running 时生效。

    Agent Service 正常落库后消息已是 done/degraded，这里影响 0 行，
    不会覆盖 persist_turn 写好的正文、元数据和证据。
    """
    code = "CHAT_STREAM_INTERRUPTED" if error is None else type(error).__name__
    meta = {
        "error": {"code": code, "message": "回答中断，请重试。"},
        "failed_at": datetime.now(timezone.utc).isoformat(),
    }
    # 一个字都没收到时也留下可读的失败说明，避免会话列表没摘要。
    content_value = partial or failed_message_payload(code, "回答中断，请重试。")[0]
    try:
        with tenant_scope(tenant_id, user_id) as db:
            result = db.execute(
                text(
                    "update messages "
                    "   set content = :content,"
                    "       status = 'failed',"
                    "       meta = meta || cast(:meta as jsonb),"
                    "       updated_at = now() "
                    " where id = :id and tenant_id = :tenant_id and status = 'running'"
                ),
                {
                    "content": content_value,
                    "meta": json.dumps(meta, ensure_ascii=False),
                    "id": str(message_id),
                    "tenant_id": str(tenant_id),
                },
            )
            if int(result.rowcount or 0) != 1:
                return False
            db.execute(
                text(
                    "update conversations "
                    "   set last_message_preview = "
                    "       coalesce(nullif(:preview, ''), last_message_preview),"
                    "       message_count = ("
                    "           select count(*) from messages where conversation_id = :conversation_id"
                    "       ) "
                    " where id = :conversation_id"
                ),
                {
                    "preview": message_preview(content_value),
                    "conversation_id": str(conversation_id),
                },
            )
            return True
    except Exception:
        logger.exception("failed to finalize interrupted turn %s", message_id)
        return False

async def _proxy_chat(
    principal: Any,
    prepared: PreparedTurn,
    question: str,
    settings: Any,
) -> AsyncIterator[str]:
    """转发 Agent Service 的 SSE。

    消费上游的协程独立于响应生成器：前端断开时只停 yield，
    上游继续跑完并落库。
    """
    queue: asyncio.Queue[str | None] = asyncio.Queue()
    payload = _agent_payload(principal, prepared, question, settings)
    url = f"{settings.agent_service_url}/execute"

    async def consume() -> None:
        state: dict[str, Any] = {"buffer": "", "text": "", "done": False}
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        try:
            async with httpx.AsyncClient(timeout=None, trust_env=False) as client:
                async with client.stream("POST", url, json=payload) as upstream:
                    if upstream.status_code != 200:
                        body = (await upstream.aread()).decode("utf-8", "replace")
                        raise RuntimeError(
                            f"agent service returned {upstream.status_code}: {body[:200]}"
                        )
                    iterator = upstream.aiter_raw().__aiter__()
                    pending: asyncio.Task[Any] | None = None
                    try:
                        while True:
                            if pending is None:
                                pending = asyncio.create_task(anext(iterator))
                            done, _ = await asyncio.wait(
                                {pending}, timeout=_AGENT_STALL_SECONDS
                            )
                            if pending not in done:
                                # 上游只是慢（例如长模型调用），发 keepalive 后继续等。
                                await queue.put(": keepalive\n\n")
                                continue
                            try:
                                chunk = pending.result()
                            except StopAsyncIteration:
                                pending = None
                                break
                            pending = None
                            chunk_text = decoder.decode(chunk)
                            if chunk_text:
                                _absorb_sse(chunk_text, state)
                                await queue.put(chunk_text)
                        tail = decoder.decode(b"", True)
                        if tail:
                            _absorb_sse(tail, state)
                            await queue.put(tail)
                    finally:
                        if pending is not None:
                            pending.cancel()
                            await asyncio.gather(pending, return_exceptions=True)
                        await upstream.aclose()
        except asyncio.CancelledError:
            logger.info(
                "agent service stream cancelled; message %s left to orphan recovery",
                prepared.message_id,
            )
            raise
        except Exception as exc:
            logger.exception("agent service stream failed for %s", prepared.message_id)
            await queue.put(
                _sse(
                    "error",
                    {"code": type(exc).__name__, "message": "回答生成失败，请重试。"},
                )
            )
            await asyncio.to_thread(
                _finalize_interrupted_turn,
                principal.tenant_id,
                principal.user_id,
                prepared.conversation_id,
                prepared.message_id,
                str(state.get("text") or ""),
                exc,
            )
        else:
            if not state["done"]:
                await asyncio.to_thread(
                    _finalize_interrupted_turn,
                    principal.tenant_id,
                    principal.user_id,
                    prepared.conversation_id,
                    prepared.message_id,
                    str(state.get("text") or ""),
                    None,
                )
        finally:
            queue.put_nowait(None)

    _spawn_background(consume())

    try:
        while True:
            item = await queue.get()
            if item is None:
                return
            yield item
    except asyncio.CancelledError:
        # 前端断开：不取消 consume()，Agent 会跑完并落库。
        logger.info(
            "client disconnected; agent keeps running (message=%s)",
            prepared.message_id,
        )
        raise

@router.post("/chat")
def chat(body: ChatBody, request: Request) -> StreamingResponse:
    check_origin(request)
    principal = require_principal(request)
    question = _question_of(body)
    _check_rate(principal.user_id)

    _, prepared = _start_turn(principal, body, question, enqueue=False)

    settings = get_settings()
    return StreamingResponse(
        _proxy_chat(principal, prepared, question, settings),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )

@router.post("/research")
def research(body: ChatBody, request: Request) -> dict[str, Any]:
    """深度研究：写队列后立即返回 run_id，进度通过回放流获取。"""
    check_origin(request)
    principal = require_principal(request)
    question = _question_of(body)
    _check_rate(principal.user_id)

    _, prepared = _start_turn(principal, body, question)
    return {
        "run_id": str(prepared.run_id),
        "message_id": str(prepared.message_id),
        "conversation_id": str(prepared.conversation_id),
        "stream_url": f"/api/v1/research/{prepared.run_id}/stream",
    }

@router.get("/research/{run_id}/stream")
def research_stream(run_id: uuid.UUID, request: Request, after: int = 0) -> StreamingResponse:
    """回放 research 任务事件，支持 ?after 与 Last-Event-ID 续传。"""
    check_origin(request)
    principal = require_principal(request)
    cursor = max(0, int(after))
    last_event_id = request.headers.get("last-event-id")
    if last_event_id:
        try:
            cursor = max(cursor, int(last_event_id))
        except ValueError:
            pass

    message_id = run_queue.research_target(principal.tenant_id, principal.user_id, run_id)
    if message_id is None:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "任务不存在"})

    return StreamingResponse(
        _stream_events(principal.tenant_id, principal.user_id, message_id, cursor),
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