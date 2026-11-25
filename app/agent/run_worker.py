from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
import time
import uuid
from typing import Any, AsyncIterator

from sqlalchemy import func, select

from app.agent.agentscope_runtime import AgentScopeRunner
from app.agent.loop import AgentLoop, ChatRun
from app.agent.persist import RunFenced, mark_assistant_failed
from app.agent.run_queue import (
    QueueTicket,
    append_event,
    claim,
    finish,
    mark_events_expiring,
    purge_expired_events,
    run_status,
    sweep,
    sweep_orphan_messages,
)
from app.config import Settings, get_settings
from app.db import tenant_scope
from app.models import Message

logger = logging.getLogger(__name__)

_LEGACY_DONE = object()
ORPHAN_SWEEP_INTERVAL_SECONDS = 60.0
EVENT_PURGE_INTERVAL_SECONDS = 600.0
# 真流式下 delta 很密，落事件表前合并成小块，避免一轮产生上千行。
_DELTA_FLUSH_CHARS = 200

def _next_event(iterator: Any) -> Any:
    try:
        return next(iterator)
    except StopIteration:
        return _LEGACY_DONE

def load_chat_run(ticket: QueueTicket, settings: Settings | None = None) -> ChatRun | None:
    """从数据库还原一轮对话，并把助手消息切到 running。"""
    with tenant_scope(ticket.tenant_id, ticket.user_id) as db:
        assistant = db.scalar(select(Message).where(Message.id == ticket.message_id))
        if assistant is None:
            return None
        user_message = db.scalar(
            select(Message)
            .where(
                Message.request_id == ticket.request_id,
                Message.role == "user",
            )
            .order_by(Message.created_at)
            .limit(1)
        )
        if user_message is None:
            return None

        assistant.status = "running"
        if ticket.attempt > 1:
            assistant.content = ""
            assistant.meta = {}

        previous = db.scalars(
            select(Message)
            .where(
                Message.conversation_id == ticket.conversation_id,
                Message.request_id != ticket.request_id,
                Message.status.in_(["done", "degraded"]),
            )
            .order_by(Message.created_at.desc())
            .limit(max(2, int((settings or get_settings()).history_turns) * 2))
        ).all()
        history = [
            {"role": item.role, "content": item.content}
            for item in reversed(previous)
            if item.role in {"user", "assistant"} and item.content
        ]
        turn = int(
            db.scalar(
                select(func.count(Message.id)).where(
                    Message.conversation_id == ticket.conversation_id,
                    Message.role == "assistant",
                    Message.created_at < assistant.created_at,
                )
            )
            or 0
        ) + 1

        return ChatRun(
            tenant_id=ticket.tenant_id,
            user_id=ticket.user_id,
            conversation_id=ticket.conversation_id,
            message_id=ticket.message_id,
            request_id=ticket.request_id,
            question=user_message.content,
            history=history,
            turn=max(1, turn),
            run_id=ticket.run_id,
            worker_id=ticket.worker_id,
            attempt=ticket.attempt,
        )

class RunWorker:
    """单进程内的 durable queue worker；多实例靠 SKIP LOCKED 协作。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.worker_id = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._loop(), name="chat-run-worker")
        logger.info("run worker started: %s", self.worker_id)

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None
        logger.info("run worker stopped: %s", self.worker_id)

    async def _sleep(self, seconds: float) -> None:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._stopping.wait(), timeout=max(0.05, seconds))

    async def _loop(self) -> None:
        last_orphan_sweep = 0.0
        last_event_purge = 0.0
        while not self._stopping.is_set():
            try:
                recovered = await asyncio.to_thread(sweep, self.settings.run_max_attempts)
                for ticket, state in recovered:
                    logger.warning(
                        "run %s recovered by lease sweep (attempt=%s, state=%s)",
                        ticket.run_id,
                        ticket.attempt,
                        state,
                    )

                now = time.monotonic()
                if now - last_orphan_sweep >= ORPHAN_SWEEP_INTERVAL_SECONDS:
                    last_orphan_sweep = now
                    orphans = await asyncio.to_thread(
                        sweep_orphan_messages,
                        self.settings.orphan_sweep_seconds,
                    )
                    if orphans:
                        logger.warning("marked %s orphan chat message(s) as failed", orphans)
                if now - last_event_purge >= EVENT_PURGE_INTERVAL_SECONDS:
                    last_event_purge = now
                    purged = await asyncio.to_thread(purge_expired_events)
                    if purged:
                        logger.info("purged %s expired session event(s)", purged)

                ticket = await asyncio.to_thread(
                    claim,
                    self.worker_id,
                    self.settings.run_lease_seconds,
                )
                if ticket is None:
                    await self._sleep(self.settings.run_queue_poll_seconds)
                    continue
                await self._execute(ticket)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("run worker loop failed")
                await self._sleep(1.0)

    async def _execute(self, ticket: QueueTicket) -> None:
        run = await asyncio.to_thread(load_chat_run, ticket, self.settings)
        if run is None:
            await asyncio.to_thread(
                finish, ticket.run_id, "failed", error_code="RUN_NOT_FOUND",
                error_message="message or conversation missing",
            )
            return

        done = False
        run_finalized = False
        pending: list[str] = []
        pending_chars = 0

        async def flush_delta() -> None:
            nonlocal pending_chars
            if not pending:
                return
            text = "".join(pending)
            pending.clear()
            pending_chars = 0
            await asyncio.to_thread(append_event, ticket, "delta", {"text": text})

        try:
            async for event, data in self._stream(run, ticket.attempt):
                if event == "delta":
                    text = str(data.get("text") or "")
                    pending.append(text)
                    pending_chars += len(text)
                    if pending_chars >= _DELTA_FLUSH_CHARS:
                        await flush_delta()
                    continue
                if event == "delta_reset":
                    pending.clear()
                    pending_chars = 0
                await flush_delta()
                if event == "done":
                    done = True
                    run_finalized = bool(data.get("run_finalized"))
                await asyncio.to_thread(append_event, ticket, event, data)
            await flush_delta()
        except asyncio.CancelledError:
            raise
        except RunFenced:
            # 本 Worker 已被租约回收，新的持有者会继续；不要写任何失败状态。
            logger.warning("run %s fenced; another worker owns it now", ticket.run_id)
            return
        except Exception as exc:
            if await asyncio.to_thread(run_status, ticket.tenant_id, ticket.run_id) == "done":
                logger.warning(
                    "run %s already finalized; ignoring late error", ticket.run_id
                )
                return
            logger.exception("run %s failed", ticket.run_id)
            error = {"code": type(exc).__name__, "message": "Agent 执行异常，请稍后重试。"}
            await asyncio.to_thread(append_event, ticket, "error", error)
            await asyncio.to_thread(
                append_event,
                ticket,
                "done",
                {
                    "status": "error",
                    "message_id": str(ticket.message_id),
                    "conversation_id": str(ticket.conversation_id),
                    "request_id": str(ticket.request_id),
                },
            )
            await asyncio.to_thread(
                mark_assistant_failed,
                ticket.tenant_id,
                ticket.user_id,
                ticket.message_id,
                "error",
                type(exc).__name__,
                "Agent 执行异常，请稍后重试。",
            )
            await asyncio.to_thread(
                mark_events_expiring, ticket.tenant_id, ticket.user_id, ticket.message_id
            )
            await asyncio.to_thread(
                finish, ticket.run_id, "failed",
                error_code=type(exc).__name__, error_message=str(exc),
            )
            return

        await asyncio.to_thread(
            mark_events_expiring, ticket.tenant_id, ticket.user_id, ticket.message_id
        )
        if run_finalized:
            return
        if not done:
            await asyncio.to_thread(
                append_event,
                ticket,
                "done",
                {
                    "status": "error",
                    "message_id": str(ticket.message_id),
                    "conversation_id": str(ticket.conversation_id),
                    "request_id": str(ticket.request_id),
                },
            )
            await asyncio.to_thread(
                mark_assistant_failed,
                ticket.tenant_id,
                ticket.user_id,
                ticket.message_id,
                "error",
                "PERSIST_FAILED",
                "回答保存失败，请稍后重试。",
            )
            await asyncio.to_thread(
                mark_events_expiring, ticket.tenant_id, ticket.user_id, ticket.message_id
            )
            await asyncio.to_thread(
                finish,
                ticket.run_id,
                "failed",
                error_code="PERSIST_FAILED",
                error_message="turn persistence did not complete",
            )
            return
        await asyncio.to_thread(finish, ticket.run_id, "done")

    async def _stream(
        self,
        run: ChatRun,
        attempt: int,
    ) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        if self.settings.agent_engine == "legacy":
            yield (
                "meta",
                {
                    "conversation_id": str(run.conversation_id),
                    "message_id": str(run.message_id),
                    "request_id": str(run.request_id),
                    "timeout_seconds": self.settings.task_timeout_seconds,
                    "engine": "legacy",
                    "turn": run.turn,
                    "attempt": attempt,
                },
            )
            iterator = AgentLoop().stream(run)
            while True:
                item = await asyncio.to_thread(_next_event, iterator)
                if item is _LEGACY_DONE:
                    return
                yield item
            return

        async for event, data in AgentScopeRunner().stream(run):
            if event == "meta":
                data = {**data, "attempt": attempt}
            yield event, data
