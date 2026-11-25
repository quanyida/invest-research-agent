from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.agent.persist import mark_assistant_failed_in_session
from app.db import session_scope, tenant_scope

logger = logging.getLogger(__name__)

ACTIVE_STATUSES = ("queued", "running")
TERMINAL_STATUSES = ("done", "failed", "cancelled")

class ConversationBusy(RuntimeError):
    """另一个 run 仍占用该会话。"""

@dataclass(frozen=True)
class QueueTicket:
    run_id: uuid.UUID
    tenant_id: uuid.UUID
    user_id: uuid.UUID
    conversation_id: uuid.UUID
    message_id: uuid.UUID
    request_id: uuid.UUID
    attempt: int = 0
    worker_id: str | None = None

def _ticket(row: Any) -> QueueTicket:
    return QueueTicket(
        run_id=row["id"],
        tenant_id=row["tenant_id"],
        user_id=row["user_id"],
        conversation_id=row["conversation_id"],
        message_id=row["message_id"],
        request_id=row["request_id"],
        attempt=int(row["attempt"] or 0),
        worker_id=row.get("worker_id"),
    )

def enqueue(
    db: Session,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
    message_id: uuid.UUID,
    request_id: uuid.UUID,
) -> uuid.UUID:
    """在调用方事务内写入一条待执行任务。

    依赖 ``chat_runs_one_active`` 唯一索引保证同会话串行；
    冲突会抛 ``IntegrityError``，由调用方转成 409。
    """
    run_id = uuid.uuid4()
    db.execute(
        text(
            "insert into chat_runs "
            "(id, tenant_id, user_id, conversation_id, message_id, request_id) "
            "values (:id, :tenant_id, :user_id, :conversation_id, :message_id, :request_id)"
        ),
        {
            "id": str(run_id),
            "tenant_id": str(tenant_id),
            "user_id": str(user_id),
            "conversation_id": str(conversation_id),
            "message_id": str(message_id),
            "request_id": str(request_id),
        },
    )
    return run_id

def claim(worker_id: str, lease_seconds: float) -> QueueTicket | None:
    """跨进程抢占一条 queued 任务，使用 SKIP LOCKED 避免互相等待。"""
    with session_scope() as db:
        row = db.execute(
            text(
                "with picked as ("
                "  select id from chat_runs"
                "  where status = 'queued'"
                "  order by created_at"
                "  for update skip locked"
                "  limit 1"
                ")"
                " update chat_runs r"
                "    set status = 'running',"
                "        attempt = r.attempt + 1,"
                "        worker_id = :worker_id,"
                "        started_at = now(),"
                "        lease_until = now() + (:lease * interval '1 second')"
                "   from picked"
                "  where r.id = picked.id"
                " returning r.id, r.tenant_id, r.user_id, r.conversation_id,"
                "           r.message_id, r.request_id, r.attempt, r.worker_id"
            ),
            {"worker_id": worker_id, "lease": float(lease_seconds)},
        ).mappings().first()
    return _ticket(row) if row is not None else None

def finish(
    run_id: uuid.UUID,
    status: str,
    *,
    error_code: str | None = None,
    error_message: str | None = None,
) -> None:
    with session_scope() as db:
        db.execute(
            text(
                "update chat_runs"
                "   set status = :status,"
                "       finished_at = now(),"
                "       lease_until = null,"
                "       error_code = :code,"
                "       error_message = :message"
                " where id = :id and status = 'running'"
            ),
            {
                "id": str(run_id),
                "status": status,
                "code": error_code,
                "message": (error_message or "")[:1000] or None,
            },
        )

def sweep(max_attempts: int) -> list[tuple[QueueTicket, str]]:
    """回收租约过期的 running 任务：未超次数回队列，超次数判失败。

    任务状态与消息状态在同一个事务里提交；扫尾 Worker 在两步之间宕机时
    两者一起回滚，不会留下「任务失败、消息仍在运行」的孤儿。
    """
    limit = max(1, int(max_attempts))
    recovered: list[tuple[QueueTicket, str]] = []
    with session_scope() as db:
        rows = db.execute(
            text(
                "select id, tenant_id, user_id, conversation_id, message_id,"
                "       request_id, attempt, worker_id"
                " from chat_runs"
                " where status = 'running'"
                "   and lease_until is not null"
                "   and lease_until < now()"
                " order by lease_until"
                " for update skip locked"
            )
        ).mappings().all()
        for row in rows:
            ticket = _ticket(row)
            state = "failed" if ticket.attempt >= limit else "queued"
            db.execute(
                text(
                    "select set_config('app.tenant_id', :tenant_id, true), "
                    "set_config('app.user_id', :user_id, true)"
                ),
                {"tenant_id": str(ticket.tenant_id), "user_id": str(ticket.user_id)},
            )
            result = db.execute(
                text(
                    "update chat_runs"
                    "   set status = :state,"
                    "       worker_id = null,"
                    "       lease_until = null,"
                    "       finished_at = case when :state = 'failed' then now() else null end,"
                    "       error_code = case when :state = 'failed'"
                    "                         then coalesce(error_code, 'LEASE_EXPIRED') else error_code end,"
                    "       error_message = case when :state = 'failed'"
                    "                            then coalesce(error_message, 'worker lease expired') else error_message end"
                    " where id = :id"
                    "   and status = 'running'"
                    "   and lease_until is not null"
                    "   and lease_until < now()"
                ),
                {"id": str(ticket.run_id), "state": state},
            )
            if int(result.rowcount or 0) != 1:
                continue
            if state == "failed":
                mark_assistant_failed_in_session(
                    db,
                    ticket.message_id,
                    "failed",
                    "LEASE_EXPIRED",
                    "Worker 意外中断，任务已终止，请重试。",
                )
            else:
                db.execute(
                    text(
                        "update messages set status = 'queued'"
                        " where id = :id and status = 'running'"
                    ),
                    {"id": str(ticket.message_id)},
                )
            recovered.append((ticket, state))
    return recovered

def append_event(ticket: QueueTicket, event: str, data: dict[str, Any]) -> None:
    payload = json.dumps(data, ensure_ascii=False, default=str)
    with tenant_scope(ticket.tenant_id, ticket.user_id) as db:
        db.execute(
            text(
                "insert into session_events "
                "(tenant_id, user_id, conversation_id, message_id, event, data) "
                "values (:tenant_id, :user_id, :conversation_id, :message_id, :event, cast(:data as jsonb))"
            ),
            {
                "tenant_id": str(ticket.tenant_id),
                "user_id": str(ticket.user_id),
                "conversation_id": str(ticket.conversation_id),
                "message_id": str(ticket.message_id),
                "event": event,
                "data": payload,
            },
        )

def read_events(
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    message_id: uuid.UUID,
    after_id: int,
    limit: int = 200,
) -> list[dict[str, Any]]:
    with tenant_scope(tenant_id, user_id) as db:
        rows = db.execute(
            text(
                "select id, event, data from session_events"
                " where message_id = :message_id and id > :after"
                " order by id limit :limit"
            ),
            {"message_id": str(message_id), "after": int(after_id), "limit": int(limit)},
        ).mappings().all()
    result: list[dict[str, Any]] = []
    for row in rows:
        data = row["data"]
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except json.JSONDecodeError:
                data = {"raw": data}
        result.append({"id": int(row["id"]), "event": row["event"], "data": data or {}})
    return result

def research_target(
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    run_id: uuid.UUID,
) -> uuid.UUID | None:
    """按 run_id 找到 research 任务的助手消息。

    ``chat_runs`` 没有 RLS，必须显式带租户与用户过滤。
    """
    with session_scope() as db:
        value = db.execute(
            text(
                "select message_id from chat_runs"
                " where id = :id and tenant_id = :tenant_id and user_id = :user_id"
            ),
            {
                "id": str(run_id),
                "tenant_id": str(tenant_id),
                "user_id": str(user_id),
            },
        ).scalar_one_or_none()
    if value is None:
        return None
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))

def run_status(tenant_id: uuid.UUID, run_id: uuid.UUID) -> str | None:
    with session_scope() as db:
        return db.execute(
            text("select status from chat_runs where id = :id and tenant_id = :tenant_id"),
            {"id": str(run_id), "tenant_id": str(tenant_id)},
        ).scalar_one_or_none()

def active_run_for_message(tenant_id: uuid.UUID, message_id: uuid.UUID) -> QueueTicket | None:
    with session_scope() as db:
        row = db.execute(
            text(
                "select id, tenant_id, user_id, conversation_id, message_id, request_id, attempt, worker_id"
                " from chat_runs"
                " where message_id = :message_id and tenant_id = :tenant_id"
                " order by created_at desc limit 1"
            ),
            {"message_id": str(message_id), "tenant_id": str(tenant_id)},
        ).mappings().first()
    return _ticket(row) if row is not None else None
def message_run_state(
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    message_id: uuid.UUID,
) -> dict[str, Any] | None:
    """读取某条助手消息最近一次 run 的终态信息，供 SSE 断线重连收尾。"""
    with tenant_scope(tenant_id, user_id) as db:
        row = db.execute(
            text(
                "select r.status, r.error_code, r.error_message,"
                "       m.status as message_status"
                " from chat_runs r"
                " join messages m on m.tenant_id = r.tenant_id and m.id = r.message_id"
                " where r.tenant_id = :tenant_id and r.message_id = :message_id"
                " order by r.created_at desc limit 1"
            ),
            {"tenant_id": str(tenant_id), "message_id": str(message_id)},
        ).mappings().first()
    return dict(row) if row is not None else None