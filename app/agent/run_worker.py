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
    owns_run,
    purge_expired_events,
    renew_lease,
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
# chat 快照间隔：刷新最多丢这么多字符的生成过程
_SNAPSHOT_INTERVAL_SECONDS = 0.2

def _next_event(iterator: Any) -> Any:
    try:
        return next(iterator)
    except StopIteration:
        return _LEGACY_DONE

async def _drain(*tasks: asyncio.Task[Any]) -> None:
    """取消未结束的子任务并回收，避免悬挂任务与「异常未取回」警告。"""
    for task in tasks:
        if not task.done():
            task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)

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
    """单进程内的 durable queue worker；多实例靠 SKIP LOCKED 协作。

    分两组协程消费：chat 快进快出、research 长跑。两组额度独立，
    一个 40 分钟的深度研究占满自己的槽，不会挡住普通聊天。
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.worker_id = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self._tasks: list[asyncio.Task[None]] = []
        self._stopping = asyncio.Event()

    async def start(self) -> None:
        if self._tasks:
            return
        chat_n = max(1, int(self.settings.chat_concurrency))
        research_n = max(1, int(self.settings.research_concurrency))
        for i in range(chat_n):
            self._tasks.append(
                asyncio.create_task(self._loop("chat"), name=f"worker-chat-{i}")
            )
        for i in range(research_n):
            self._tasks.append(
                asyncio.create_task(self._loop("research"), name=f"worker-research-{i}")
            )
        logger.info(
            "run worker started: %s (chat=%s research=%s)",
            self.worker_id,
            chat_n,
            research_n,
        )

    async def stop(self) -> None:
        self._stopping.set()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks = []
        logger.info("run worker stopped: %s", self.worker_id)

    async def _sleep(self, seconds: float) -> None:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._stopping.wait(), timeout=max(0.05, seconds))

    async def _loop(self, run_type: str) -> None:
        # 维护类任务只让 chat 组的第 0 个协程做，避免 N 个协程重复扫库。
        is_maintenance = run_type == "chat" and asyncio.current_task() is self._tasks[0]
        last_orphan_sweep = 0.0
        last_event_purge = 0.0
        while not self._stopping.is_set():
            try:
                if is_maintenance:
                    recovered = await asyncio.to_thread(
                        sweep, self.settings.run_max_attempts
                    )
                    for ticket, state in recovered:
                        logger.warning(
                            "run %s recovered by lease sweep (type=%s attempt=%s state=%s)",
                            ticket.run_id,
                            ticket.run_type,
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
                            logger.warning(
                                "marked %s orphan chat message(s) as failed", orphans
                            )
                    if now - last_event_purge >= EVENT_PURGE_INTERVAL_SECONDS:
                        last_event_purge = now
                        purged = await asyncio.to_thread(purge_expired_events)
                        if purged:
                            logger.info("purged %s expired session event(s)", purged)

                ticket = await asyncio.to_thread(
                    claim,
                    self.worker_id,
                    self.settings.run_lease_seconds,
                    run_type,
                )
                if ticket is None:
                    await self._sleep(self.settings.run_queue_poll_seconds)
                    continue
                await self._execute(ticket)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("run worker loop failed (%s)", run_type)
                await self._sleep(1.0)

    async def _execute(self, ticket: QueueTicket) -> None:
        """执行一条任务；心跳与执行互为守卫。

        心跳续租失败意味着任务已被别的 Worker 接管，必须立刻取消执行协程，
        否则旧 Worker 会继续发起工具调用、继续往同一个 message 写事件。
        反过来执行先结束时先停心跳，避免它在终态之后续租失败刷告警。
        """
        execution = asyncio.create_task(
            self._execute_inner(ticket), name=f"execute-{ticket.run_id}"
        )
        heartbeat = asyncio.create_task(
            self._heartbeat(ticket), name=f"heartbeat-{ticket.run_id}"
        )
        try:
            waiting: set[asyncio.Task[Any]] = {execution, heartbeat}
            while True:
                done, waiting = await asyncio.wait(
                    waiting, return_when=asyncio.FIRST_COMPLETED
                )
                if execution in done:
                    break
                exc = None if heartbeat.cancelled() else heartbeat.exception()
                if exc is None:
                    # 正常退场：run 已由本 Worker 收尾，等执行写完尾事件即可
                    continue
                await _drain(execution)
                if isinstance(exc, RunFenced):
                    logger.warning(
                        "run %s fenced (attempt=%s worker=%s); execution stopped",
                        ticket.run_id,
                        ticket.attempt,
                        ticket.worker_id,
                    )
                    return
                raise exc
        except asyncio.CancelledError:
            # 外部取消：两个子任务都要收掉，原样抛出，不伪装成 RunFenced。
            await _drain(execution, heartbeat)
            raise
        finally:
            await _drain(heartbeat)
            if not execution.done():
                await _drain(execution)

        if not execution.cancelled():
            exc = execution.exception()
            if exc is not None:
                raise exc

    async def _execute_inner(self, ticket: QueueTicket) -> None:
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
        # chat 路径每 200ms 落一条 snapshot（全文），刷新时只读最后一条就能恢复；
        # research 逐条写 delta/工具事件，因为中间步骤不能丢。
        is_chat = ticket.run_type == "chat"
        accumulated: list[str] = []
        last_snapshot_at = 0.0

        async def flush_delta() -> None:
            nonlocal pending_chars
            if not pending:
                return
            text = "".join(pending)
            pending.clear()
            pending_chars = 0
            await asyncio.to_thread(append_event, ticket, "delta", {"text": text})

        async def flush_snapshot(force: bool = False) -> None:
            nonlocal last_snapshot_at
            if not is_chat or not accumulated:
                return
            now = time.monotonic()
            if not force and now - last_snapshot_at < _SNAPSHOT_INTERVAL_SECONDS:
                return
            last_snapshot_at = now
            await asyncio.to_thread(
                append_event, ticket, "snapshot", {"text": "".join(accumulated)}
            )

        # 租约只有 120 秒，长任务必须靠心跳续租，否则会被 sweep 误判崩溃。
        # 心跳由 _execute 统一启停，这里只管执行。

        try:
            async for event, data in self._stream(run, ticket.attempt):
                if event == "delta":
                    text = str(data.get("text") or "")
                    accumulated.append(text)
                    if is_chat:
                        # chat 不写逐条 delta，只按时间窗写快照
                        await flush_snapshot()
                        continue
                    pending.append(text)
                    pending_chars += len(text)
                    if pending_chars >= _DELTA_FLUSH_CHARS:
                        await flush_delta()
                    continue
                if event == "delta_reset":
                    pending.clear()
                    pending_chars = 0
                    accumulated.clear()
                await flush_delta()
                if event == "done":
                    done = True
                    run_finalized = bool(data.get("run_finalized"))
                await asyncio.to_thread(append_event, ticket, event, data)
            await flush_delta()
            await flush_snapshot(force=True)
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
            error = {
                "code": type(exc).__name__,
                "message": "回答生成失败，请重新提问。",
            }
            # 所有权校验在 append_event 里：被接管后第一条写入就被拒绝，
            # 后面的失败标记全部跳过，不会覆盖新持有者的状态。
            try:
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
                    "回答生成失败，请重新提问。",
                    run_id=ticket.run_id,
                    worker_id=ticket.worker_id,
                    attempt=ticket.attempt,
                )
                await asyncio.to_thread(
                    mark_events_expiring,
                    ticket.tenant_id,
                    ticket.user_id,
                    ticket.message_id,
                    run_id=ticket.run_id,
                    worker_id=ticket.worker_id,
                    attempt=ticket.attempt,
                )
                await asyncio.to_thread(
                    finish, ticket.run_id, "failed",
                    error_code=type(exc).__name__, error_message=str(exc),
                    worker_id=ticket.worker_id, attempt=ticket.attempt,
                )
            except RunFenced:
                logger.warning(
                    "run %s lost ownership while failing; new owner keeps its state",
                    ticket.run_id,
                )
            return

        await asyncio.to_thread(
            mark_events_expiring,
            ticket.tenant_id,
            ticket.user_id,
            ticket.message_id,
            run_id=ticket.run_id,
            worker_id=ticket.worker_id,
            attempt=ticket.attempt,
        )
        if run_finalized:
            return
        if not done:
            try:
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
                    "回答保存失败，请重新提问。",
                    run_id=ticket.run_id,
                    worker_id=ticket.worker_id,
                    attempt=ticket.attempt,
                )
                await asyncio.to_thread(
                    mark_events_expiring,
                    ticket.tenant_id,
                    ticket.user_id,
                    ticket.message_id,
                    run_id=ticket.run_id,
                    worker_id=ticket.worker_id,
                    attempt=ticket.attempt,
                )
                await asyncio.to_thread(
                    finish,
                    ticket.run_id,
                    "failed",
                    error_code="PERSIST_FAILED",
                    error_message="turn persistence did not complete",
                    worker_id=ticket.worker_id,
                    attempt=ticket.attempt,
                )
            except RunFenced:
                logger.warning(
                    "run %s lost ownership before finalize; new owner keeps its state",
                    ticket.run_id,
                )
            return
        await asyncio.to_thread(
            finish,
            ticket.run_id,
            "done",
            worker_id=ticket.worker_id,
            attempt=ticket.attempt,
        )

    async def _heartbeat(self, ticket: QueueTicket) -> None:
        """执行期间定期续租；续租失败说明 run 已被回收，主动退出。

        只有 RunFenced 会终止心跳。偶发的数据库错误不能停掉续租循环，
        否则一次抖动就会让活着的 Worker 被 sweep 误判成崩溃；而 run 已经
        由本 Worker 收尾（不再是 running）属于正常退场，返回即可。
        """
        lease = float(self.settings.run_lease_seconds)
        interval = max(5.0, lease / 3)
        worker_id = ticket.worker_id or self.worker_id
        while True:
            await asyncio.sleep(interval)
            try:
                ok = await asyncio.to_thread(
                    renew_lease,
                    ticket.run_id,
                    worker_id,
                    ticket.attempt,
                    lease,
                )
                if ok:
                    continue
                if await asyncio.to_thread(
                    owns_run, ticket.run_id, worker_id, ticket.attempt
                ):
                    logger.info(
                        "run %s already finalized by this worker; heartbeat stops",
                        ticket.run_id,
                    )
                    return
                raise RunFenced(
                    f"run {ticket.run_id} is no longer owned by {worker_id}"
                )
            except asyncio.CancelledError:
                raise
            except RunFenced:
                logger.warning(
                    "run %s heartbeat lost; another worker owns it", ticket.run_id
                )
                raise
            except Exception:
                logger.exception("heartbeat failed for run %s; retrying", ticket.run_id)

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
