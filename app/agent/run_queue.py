from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any, Sequence

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.agent.persist import (
    RunFenced,
    failed_message_payload,
    mark_assistant_failed_in_session,
)
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
    run_type: str = "research"

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
        run_type=str(row.get("run_type") or "research"),
    )

def enqueue(
    db: Session,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
    message_id: uuid.UUID,
    request_id: uuid.UUID,
    run_type: str = "research",
    priority: int | None = None,
) -> uuid.UUID:
    """在调用方事务内写入一条待执行任务。

    依赖 ``chat_runs_one_active`` 唯一索引保证同会话串行；
    冲突会抛 ``IntegrityError``，由调用方转成 409。

    ``run_type`` 决定 worker 用哪一组协程消费它，也决定崩溃后是否重试。
    ``priority`` 越小越先被抢占；不传时 chat=10、research=100。
    """
    run_id = uuid.uuid4()
    resolved_priority = priority if priority is not None else (
        10 if run_type == "chat" else 100
    )
    db.execute(
        text(
            "insert into chat_runs "
            "(id, tenant_id, user_id, conversation_id, message_id, request_id,"
            " run_type, priority) "
            "values (:id, :tenant_id, :user_id, :conversation_id, :message_id,"
            " :request_id, :run_type, :priority)"
        ),
        {
            "id": str(run_id),
            "tenant_id": str(tenant_id),
            "user_id": str(user_id),
            "conversation_id": str(conversation_id),
            "message_id": str(message_id),
            "request_id": str(request_id),
            "run_type": run_type,
            "priority": int(resolved_priority),
        },
    )
    return run_id

def claim(
    worker_id: str,
    lease_seconds: float,
    run_type: str = "research",
) -> QueueTicket | None:
    """跨进程抢占一条 queued 任务，使用 SKIP LOCKED 避免互相等待。

    只抢 ``run_type`` 匹配的任务：普通聊天和深度研究各用一组协程，
    长任务占满自己的槽也不会挡住短任务。
    """
    with session_scope() as db:
        row = db.execute(
            text(
                "with picked as ("
                "  select id from chat_runs"
                "  where status = 'queued'"
                "    and run_type = :run_type"
                "  order by priority, created_at"
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
                "           r.message_id, r.request_id, r.attempt, r.worker_id,"
                "           r.run_type"
            ),
            {"worker_id": worker_id, "lease": float(lease_seconds), "run_type": run_type},
        ).mappings().first()
    return _ticket(row) if row is not None else None

def renew_lease(
    run_id: uuid.UUID,
    worker_id: str,
    attempt: int,
    lease_seconds: float,
) -> bool:
    """续租；worker_id 或 attempt 不匹配说明已被回收，返回 False。

    两个条件都要带：``worker_id`` 挡住「另一个 worker 接手」，
    ``attempt`` 挡住「自己重启后拿到旧 ticket」。
    """
    with session_scope() as db:
        result = db.execute(
            text(
                "update chat_runs"
                "   set lease_until = now() + (:lease * interval '1 second')"
                " where id = :id"
                "   and worker_id = :worker_id"
                "   and attempt = :attempt"
                "   and status = 'running'"
            ),
            {
                "id": str(run_id),
                "worker_id": worker_id,
                "attempt": int(attempt),
                "lease": float(lease_seconds),
            },
        )
        return int(result.rowcount or 0) == 1

def owns_run(run_id: uuid.UUID, worker_id: str, attempt: int) -> bool:
    """当前 Worker 是否仍持有这条 run（不看状态，只看归属）。

    用来区分「租约被别人拿走」和「run 已由自己收尾」：后者 ``renew_lease``
    同样会失败，但不需要触发 fencing。
    """
    with session_scope() as db:
        row = db.execute(
            text(
                "select 1 from chat_runs"
                " where id = :id and worker_id = :worker_id and attempt = :attempt"
            ),
            {"id": str(run_id), "worker_id": worker_id, "attempt": int(attempt)},
        ).first()
    return row is not None

def finish(
    run_id: uuid.UUID,
    status: str,
    *,
    error_code: str | None = None,
    error_message: str | None = None,
    worker_id: str | None = None,
    attempt: int | None = None,
) -> bool:
    """把 run 落到终态；带 worker_id/attempt 时只有持有者能写。

    返回是否真的写入了一行。被接管的旧 Worker 传了 fencing 参数后
    只会得到 ``False``，不会把新持有者的任务改成 failed/done。
    """
    fence = ""
    params = {
        "id": str(run_id),
        "status": status,
        "code": error_code,
        "message": (error_message or "")[:1000] or None,
    }
    if worker_id is not None and attempt is not None:
        fence = " and worker_id = :worker_id and attempt = :attempt"
        params["worker_id"] = worker_id
        params["attempt"] = int(attempt)
    with session_scope() as db:
        result = db.execute(
            text(
                "update chat_runs"
                "   set status = :status,"
                "       finished_at = now(),"
                "       lease_until = null,"
                "       error_code = :code,"
                "       error_message = :message"
                " where id = :id and status = 'running'" + fence
            ),
            params,
        )
        return int(result.rowcount or 0) == 1

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
                "       request_id, attempt, worker_id, run_type"
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
            is_chat = ticket.run_type == "chat"
            # 普通聊天一次机会：3 秒的任务重跑收益低于复杂度，崩溃即判死。
            # 深度研究允许重跑一次（attempt < limit）。
            if is_chat:
                state = "failed"
            else:
                state = "failed" if ticket.attempt >= limit else "queued"
            code = "WORKER_LOST" if is_chat else "LEASE_EXPIRED"
            message = (
                "回答生成中断，请重新提问。"
                if is_chat
                else "执行中断，请重试。"
            )
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
                    "                         then :code else error_code end,"
                    "       error_message = case when :state = 'failed'"
                    "                            then :message else error_message end"
                    " where id = :id"
                    "   and status = 'running'"
                    "   and lease_until is not null"
                    "   and lease_until < now()"
                ),
                {
                    "id": str(ticket.run_id),
                    "state": state,
                    "code": code,
                    "message": message,
                },
            )
            if int(result.rowcount or 0) != 1:
                continue
            if state == "failed":
                mark_assistant_failed_in_session(
                    db,
                    ticket.message_id,
                    "failed",
                    code,
                    message,
                )
                # 同一事务补两条事件：观战中的前端靠它立刻收尾，
                # 否则要等 EVENT_TAIL_TIMEOUT_SECONDS（600 秒）才断开。
                append_event_in_session(
                    db,
                    ticket,
                    "error",
                    {"code": code, "message": message},
                    fenced=False,
                )
                append_event_in_session(
                    db,
                    ticket,
                    "done",
                    {
                        "status": "failed",
                        "message_id": str(ticket.message_id),
                        "conversation_id": str(ticket.conversation_id),
                    },
                    fenced=False,
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

def _known_tenants(db: Session) -> list[tuple[str, str]]:
    """跨租户作业的租户清单只能取自无 RLS 的 chat_runs。

    messages / session_events 都是强制 RLS，没有 app.tenant_id 时一行也看不到。
    """
    rows = db.execute(text("select distinct tenant_id, user_id from chat_runs")).all()
    return [(str(row[0]), str(row[1])) for row in rows]

def _scope(db: Session, tenant_id: str, user_id: str) -> None:
    db.execute(
        text(
            "select set_config('app.tenant_id', :tenant_id, true),"
            " set_config('app.user_id', :user_id, true)"
        ),
        {"tenant_id": tenant_id, "user_id": user_id},
    )

def sweep_orphan_messages(timeout_seconds: float) -> int:
    """把长期停在 running 且没有 chat_runs 的消息标记为失败。

    ``/chat`` 不写 ``chat_runs``，所以租约回收扫不到它；这里是后台兜底。
    messages 是强制 RLS，只能逐租户 set_config 后再更新；纯 /chat 租户
    没有 chat_runs 行，其孤儿由读取时的 90 秒检测兜底。
    """
    timeout = max(0.0, float(timeout_seconds))
    content, meta = failed_message_payload("CHAT_ORPHANED", "执行中断，请重试。")
    marked = 0
    with session_scope() as db:
        for tenant_id, user_id in _known_tenants(db):
            _scope(db, tenant_id, user_id)
            result = db.execute(
                text(
                    "update messages m"
                    "   set status = 'failed',"
                    "       content = :content,"
                    "       meta = m.meta || cast(:meta as jsonb),"
                    "       updated_at = now()"
                    " where m.status = 'running'"
                    "   and m.updated_at < now() - (:timeout * interval '1 second')"
                    "   and not exists ("
                    "       select 1 from chat_runs r where r.message_id = m.id"
                    "   )"
                ),
                {
                    "content": content,
                    "meta": json.dumps(meta, ensure_ascii=False),
                    "timeout": timeout,
                },
            )
            marked += int(result.rowcount or 0)
    return marked

def mark_events_expiring(
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    message_id: uuid.UUID,
    hours: int = 24,
    *,
    run_id: uuid.UUID | None = None,
    worker_id: str | None = None,
    attempt: int | None = None,
) -> int:
    """任务终态后给回放事件打上过期时间。

    落库时已标一次，但部分事件（usage/done 等）在 persist 之后才写入，
    这里在流结束后补齐。带 run fencing 时，旧 Worker 连打标记都改不到
    新持有者的事件。
    """
    fence = ""
    params = {"hours": int(hours), "message_id": str(message_id)}
    if run_id is not None and worker_id is not None and attempt is not None:
        fence = (
            " and exists (select 1 from chat_runs"
            " where id = :run_id and worker_id = :worker_id and attempt = :attempt)"
        )
        params.update(
            {
                "run_id": str(run_id),
                "worker_id": worker_id,
                "attempt": int(attempt),
            }
        )
    with tenant_scope(tenant_id, user_id) as db:
        result = db.execute(
            text(
                "update session_events"
                "   set expires_at = now() + (:hours * interval '1 hour')"
                " where message_id = :message_id and expires_at is null" + fence
            ),
            params,
        )
        return int(result.rowcount or 0)

def purge_expired_events() -> int:
    """删除已过期的回放事件（research 任务终态 24 小时后）。"""
    purged = 0
    with session_scope() as db:
        for tenant_id, user_id in _known_tenants(db):
            _scope(db, tenant_id, user_id)
            result = db.execute(text("delete from session_events where expires_at < now()"))
            purged += int(result.rowcount or 0)
    return purged

def append_events_in_session(
    db: Session,
    ticket: QueueTicket,
    items: Sequence[tuple[str, dict[str, Any]]],
    *,
    fenced: bool = True,
) -> None:
    """在调用方事务内批量写事件（一条 INSERT，多个 VALUES）。

    ``run_id`` 必须落库：同一 message 重试时会有多次尝试，只按 message_id
    读会把两次尝试的事件混在一起，``after_seq`` 游标随之失效。

    ``fenced=True`` 时插入和所有权校验在同一条语句里完成：租约被别的
    Worker 接管后，旧 Worker 写不进任何一行，并抛 ``RunFenced``。
    租约回收（``sweep``）写收尾事件时传 ``fenced=False``——它在同一个事务
    里刚把 ``worker_id`` 清空，所有权由那条 update 的 rowcount 保证。

    ``id`` 由 bigserial 按 VALUES 顺序取值，批内递增；回放按 id 排序，
    因此批内顺序与调用方给出的顺序一致。
    """
    if not items:
        return
    params: dict[str, Any] = {
        "tenant_id": str(ticket.tenant_id),
        "user_id": str(ticket.user_id),
        "conversation_id": str(ticket.conversation_id),
        "message_id": str(ticket.message_id),
        "run_id": str(ticket.run_id),
    }
    for index, (event, data) in enumerate(items):
        params[f"event{index}"] = event
        params[f"data{index}"] = json.dumps(data, ensure_ascii=False, default=str)
    values = ", ".join(
        f"(cast(:event{index} as text), cast(:data{index} as text))"
        for index in range(len(items))
    )
    head = (
        "insert into session_events "
        "(tenant_id, user_id, conversation_id, message_id, run_id, event, data) "
        "select cast(:tenant_id as uuid), cast(:user_id as uuid),"
        " cast(:conversation_id as uuid), cast(:message_id as uuid),"
        " cast(:run_id as uuid), v.event, cast(v.data as jsonb)"
        f" from (values {values}) as v(event, data)"
    )
    if fenced and ticket.worker_id is not None:
        result = db.execute(
            text(
                head
                + " where exists ("
                "   select 1 from chat_runs"
                "    where id = :run_id"
                "      and worker_id = :worker_id"
                "      and attempt = :attempt"
                " )"
            ),
            {**params, "worker_id": ticket.worker_id, "attempt": int(ticket.attempt)},
        )
        if int(result.rowcount or 0) != len(items):
            raise RunFenced(
                f"run {ticket.run_id} is no longer owned by {ticket.worker_id}"
            )
        return
    result = db.execute(text(head), params)
    if int(result.rowcount or 0) != len(items):
        # 少写一行就是数据丢失，必须让调用方看到，不能静默吞掉。
        raise RuntimeError(
            f"append_events wrote {result.rowcount} of {len(items)} session_events"
        )

def append_events(
    ticket: QueueTicket,
    items: Sequence[tuple[str, dict[str, Any]]],
) -> None:
    with tenant_scope(ticket.tenant_id, ticket.user_id) as db:
        append_events_in_session(db, ticket, items)

def append_event_in_session(
    db: Session,
    ticket: QueueTicket,
    event: str,
    data: dict[str, Any],
    *,
    fenced: bool = True,
) -> None:
    """单条事件的兼容入口；内部仍走批量 INSERT，只发一条 SQL。"""
    append_events_in_session(db, ticket, [(event, data)], fenced=fenced)

def append_event(ticket: QueueTicket, event: str, data: dict[str, Any]) -> None:
    append_events(ticket, [(event, data)])

def read_events_by_run(
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    run_id: uuid.UUID,
    after_id: int,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """按 run 读事件。

    必须带 run_id：同一 message 重试时会有多次尝试，只按 message_id 读
    会把两次尝试的事件混在一起，``after_seq`` 游标随之失效。
    """
    with tenant_scope(tenant_id, user_id) as db:
        rows = db.execute(
            text(
                "select id, event, data from session_events"
                " where run_id = :run_id and id > :after"
                " order by id limit :limit"
            ),
            {"run_id": str(run_id), "after": int(after_id), "limit": int(limit)},
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

def load_run(
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    run_id: uuid.UUID,
) -> dict[str, Any] | None:
    """按 run_id 取回放所需的元信息。"""
    with session_scope() as db:
        row = db.execute(
            text(
                "select id, message_id, conversation_id, run_type, status, attempt"
                "  from chat_runs"
                " where id = :id and tenant_id = :tenant_id and user_id = :user_id"
            ),
            {"id": str(run_id), "tenant_id": str(tenant_id), "user_id": str(user_id)},
        ).mappings().first()
    return dict(row) if row is not None else None

def last_snapshot(
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    run_id: uuid.UUID,
) -> dict[str, Any] | None:
    """该 run 最后一条 snapshot 事件。"""
    with tenant_scope(tenant_id, user_id) as db:
        row = db.execute(
            text(
                "select id, event, data from session_events"
                " where run_id = :run_id and event = 'snapshot'"
                " order by id desc limit 1"
            ),
            {"run_id": str(run_id)},
        ).mappings().first()
    if row is None:
        return None
    data = row["data"]
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError:
            data = {"raw": data}
    return {"id": int(row["id"]), "event": row["event"], "data": data or {}}

def run_state(
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    run_id: uuid.UUID,
) -> str | None:
    """该 run 的当前状态。"""
    with session_scope() as db:
        row = db.execute(
            text(
                "select status from chat_runs"
                " where id = :id and tenant_id = :tenant_id and user_id = :user_id"
            ),
            {"id": str(run_id), "tenant_id": str(tenant_id), "user_id": str(user_id)},
        ).fetchone()
    return str(row[0]) if row is not None else None

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
