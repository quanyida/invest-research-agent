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
from fastapi import APIRouter, HTTPException, Query, Request
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
    run_type: str = "research",
) -> PreparedTurn:
    """写 user/assistant 两条消息并投队列。

    两条消息与 chat_runs 必须在同一事务：否则撞 ``chat_runs_one_active``
    回滚时会留下一条孤立的问题。
    """
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
    assistant_message = Message(
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
    db.add(assistant_message)
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
            run_type=run_type,
        )
        # 会话列表靠 meta.run_id 判断能否连事件回放。
        assistant_message.meta = {
            **(assistant_message.meta or {}),
            "run_id": str(run_id),
            "run_type": run_type,
            "queued": True,
        }
    else:
        assistant_message.meta = {"mode": "chat"}
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
    run_type: str = "research",
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
                run_type=run_type,
            )
    except IntegrityError as exc:
        if _is_conversation_busy(exc):
            raise _busy_error() from exc
        raise HTTPException(status_code=409, detail={"code": "DUPLICATE_REQUEST", "message": "该请求已提交过"}) from exc
    return request_id, prepared

def _resolve_cursor(after: int, request: Request) -> int:
    """游标取 ?after 与 Last-Event-ID 的较大值。"""
    cursor = max(0, int(after))
    last_event_id = request.headers.get("last-event-id")
    if last_event_id:
        try:
            cursor = max(cursor, int(last_event_id))
        except ValueError:
            pass
    return cursor

async def _replay_run(
    principal: Any,
    run: dict[str, Any],
    cursor: int,
) -> AsyncIterator[str]:
    """统一回放：先补历史，再接直播，终态关闭。"""
    run_id = run["id"]
    run_type = str(run["run_type"] or "research")
    yield _sse(
        "meta",
        {
            "run_id": str(run_id),
            "run_type": run_type,
            "message_id": str(run["message_id"]),
            "conversation_id": str(run["conversation_id"]),
        },
    )

    # 聊天首帧恢复：只给最后一条快照，避免几百条 delta 逐条重绘。
    if run_type == "chat" and cursor == 0:
        snap = await asyncio.to_thread(
            run_queue.last_snapshot,
            principal.tenant_id,
            principal.user_id,
            run_id,
        )
        if snap is not None:
            cursor = int(snap["id"]) - 1

    async for chunk in _tail_run(principal, run_id, run_type, cursor):
        yield chunk

async def _tail_run(
    principal: Any,
    run_id: uuid.UUID,
    run_type: str,
    cursor: int,
) -> AsyncIterator[str]:
    """轮询 run 的事件直到终态。"""
    settings = get_settings()
    deadline = time.monotonic() + settings.event_tail_timeout_seconds
    last_keepalive = time.monotonic()
    terminal = {"done", "error"}
    try:
        while True:
            events = await asyncio.to_thread(
                run_queue.read_events_by_run,
                principal.tenant_id,
                principal.user_id,
                run_id,
                cursor,
            )
            if events:
                for item in events:
                    cursor = item["id"]
                    yield _sse(item["event"], item["data"], cursor)
                    if item["event"] in terminal:
                        return
                continue

            state = await asyncio.to_thread(
                run_queue.run_state,
                principal.tenant_id,
                principal.user_id,
                run_id,
            )
            if state is None:
                return
            if state in {"done", "failed", "cancelled"}:
                # 终态事件可能还没落库，给一次机会重读
                events = await asyncio.to_thread(
                    run_queue.read_events_by_run,
                    principal.tenant_id,
                    principal.user_id,
                    run_id,
                    cursor,
                )
                for item in events:
                    cursor = item["id"]
                    yield _sse(item["event"], item["data"], cursor)
                    if item["event"] in terminal:
                        return
                yield _sse(
                    "done",
                    {"status": state, "run_id": str(run_id)},
                )
                return

            now = time.monotonic()
            if now >= deadline:
                logger.info("run tail timeout for %s at cursor %s", run_id, cursor)
                return
            if now - last_keepalive >= 10:
                last_keepalive = now
                yield ": keepalive\n\n"
            await asyncio.sleep(settings.event_poll_seconds)
    except asyncio.CancelledError:
        raise

async def _watch_run(
    principal: Any,
    prepared: PreparedTurn,
    detach_seconds: float,
) -> AsyncIterator[str]:
    """观战一条刚创建的 run；超过阈值就断开，让前端改走回放。"""
    if prepared.run_id is None:
        yield _sse("error", {"code": "QUEUE_FAILED", "message": "任务创建失败"})
        return

    run = await asyncio.to_thread(
        run_queue.load_run,
        principal.tenant_id,
        principal.user_id,
        prepared.run_id,
    )
    if run is None:
        yield _sse("error", {"code": "RUN_NOT_FOUND", "message": "任务不存在"})
        return

    started = time.monotonic()
    last_seq = 0
    async for chunk in _replay_run(principal, run, 0):
        # 跟踪最后事件 id，断流时告诉前端从哪续
        if chunk.startswith("id: "):
            head = chunk.split("\n", 1)[0][4:]
            if head.isdigit():
                last_seq = int(head)
        yield chunk
        if time.monotonic() - started > detach_seconds:
            yield _sse(
                "run_detached",
                {
                    "run_id": str(prepared.run_id),
                    "last_seq": last_seq,
                    "stream_url": f"/api/v1/runs/{prepared.run_id}/events",
                },
            )
            return

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

    # 任务从第一毫秒起就不在 HTTP 生命周期里：投队列，worker 执行到底。
    # HTTP 只是「观战」，30 秒后主动断开让前端改走回放接口。
    _, prepared = _start_turn(principal, body, question, run_type="chat")

    settings = get_settings()
    return StreamingResponse(
        _watch_run(principal, prepared, settings.chat_detach_seconds),
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

    _, prepared = _start_turn(principal, body, question, run_type="research")
    return {
        "run_id": str(prepared.run_id),
        "message_id": str(prepared.message_id),
        "conversation_id": str(prepared.conversation_id),
        "stream_url": f"/api/v1/runs/{prepared.run_id}/events",
    }

@router.get("/runs/{run_id}/events")
def run_events(
    run_id: uuid.UUID,
    request: Request,
    after_seq: int = Query(default=0, ge=0),
) -> StreamingResponse:
    """统一回放入口：普通聊天与深度研究共用。

    ``run_type='chat'`` 且 ``after_seq=0`` 时只吐最后一条 snapshot，
    避免逐条重绘；深度研究逐条重放，因为中间步骤不能丢。
    """
    check_origin(request)
    principal = require_principal(request)
    cursor = _resolve_cursor(after_seq, request)
    run = run_queue.load_run(principal.tenant_id, principal.user_id, run_id)
    if run is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "NOT_FOUND", "message": "任务不存在"},
        )
    return StreamingResponse(
        _replay_run(principal, run, cursor),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )

