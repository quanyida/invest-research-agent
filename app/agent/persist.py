from __future__ import annotations

import logging
import re
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.db import tenant_scope
from app.models import Conversation, Evidence, LlmCall, Message, ToolCall
from app.tools.channels import resolve_channel

logger = logging.getLogger(__name__)

class RunFenced(RuntimeError):
    """当前 Worker 已不是任务持有者，本次落库必须整体回滚。"""

def json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, Decimal):
        return str(value)
    return value

_PREVIEW_CITATION_RE = re.compile(r"[\[(（【]\s*E\s*\d+\s*[-–—]\s*\d+\s*[\])）】]")
_PREVIEW_WHITESPACE_RE = re.compile(r"\s+")

def message_preview(text: str, limit: int = 80) -> str:
    """Remove citation markers and collapse whitespace for the conversation list."""
    value = _PREVIEW_CITATION_RE.sub("", str(text or ""))
    value = _PREVIEW_WHITESPACE_RE.sub(" ", value).strip()
    return value[:limit]

def run_terminal_status(status: str) -> str:
    """Map a message status to the chat_runs terminal status."""
    return "done" if status in {"done", "degraded"} else "failed"

def _as_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None

def _evidence_row(
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    message_id: uuid.UUID,
    evidence: dict[str, Any],
) -> Evidence:
    fetched_at = evidence.get("fetched_at")
    period_end = evidence.get("period_end")
    data_as_of = evidence.get("data_as_of")
    raw_text = str(evidence.get("raw_text") or "")
    return Evidence(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        user_id=user_id,
        message_id=message_id,
        seq=int(evidence.get("seq") or 0),
        tool=str(evidence.get("tool") or ""),
        server=str(evidence.get("server") or ""),
        upstream_tool=str(evidence.get("upstream_tool") or ""),
        args=json_safe(evidence.get("args") or {}),
        args_hash=str(evidence.get("args_hash") or ""),
        query_text=evidence.get("query_text"),
        period_end=date.fromisoformat(period_end) if period_end else None,
        data_as_of=datetime.fromisoformat(data_as_of) if data_as_of else None,
        as_of_label=evidence.get("as_of_label"),
        fetched_at=datetime.fromisoformat(fetched_at) if fetched_at else datetime.now(timezone.utc),
        payload=json_safe(
            {
                "mode": "raw_passthrough" if raw_text else "parsed",
                "sections": evidence.get("sections") or [],
                "cached": bool(evidence.get("cached")),
                "parse_quality": evidence.get("parse_quality"),
                "parser_version": evidence.get("parser_version"),
            }
        ),
        facts=json_safe([] if raw_text else (evidence.get("facts") or [])),
        raw_excerpt=raw_text or evidence.get("raw_excerpt"),
        truncated=False if raw_text else bool(evidence.get("truncated")),
        parse_quality=Decimal("1.0" if raw_text else str(evidence.get("parse_quality") or 0.9)),
        parser_version="raw-passthrough" if raw_text else evidence.get("parser_version"),
        tool_schema_hash=evidence.get("tool_schema_hash"),
        source_title=evidence.get("source_title"),
        source_url=evidence.get("source_url"),
        source_publisher=evidence.get("source_publisher"),
        source_published_at=_as_datetime(evidence.get("source_published_at")),
        channel=resolve_channel(str(evidence.get("channel") or "")).key,
    )

def _tool_rows(
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    message_id: uuid.UUID,
    conversation_id: uuid.UUID,
    request_id: uuid.UUID,
    audit: dict[str, Any],
) -> list[ToolCall]:
    step = int(audit.get("step") or 1)
    main_id = uuid.uuid4()
    common = {
        "tenant_id": tenant_id,
        "user_id": user_id,
        "message_id": message_id,
        "conversation_id": conversation_id,
        "request_id": request_id,
        "step": step,
        "tool": str(audit.get("tool") or ""),
        "server": str(audit.get("server") or ""),
        "args": json_safe(audit.get("args") or {}),
        "args_hash": str(audit.get("args_hash") or ""),
        "query_text": audit.get("query_text"),
    }
    rows = [
        ToolCall(
            id=main_id,
            parent_id=None,
            upstream_tool=str(audit.get("upstream_tool") or ""),
            status=str(audit.get("status") or ""),
            cached=bool(audit.get("cached")),
            latency_ms=int(audit.get("latency_ms") or 0),
            error_code=audit.get("error_code"),
            **common,
        )
    ]
    for upstream in audit.get("upstreams") or []:
        rows.append(
            ToolCall(
                id=uuid.uuid4(),
                parent_id=main_id,
                upstream_tool=str(upstream),
                status="ok" if audit.get("status") == "ok" else "skipped",
                cached=bool(audit.get("cached")),
                latency_ms=0,
                error_code=audit.get("error_code"),
                **common,
            )
        )
    return rows

def persist_turn(
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
    message_id: uuid.UUID,
    request_id: uuid.UUID,
    question: str,
    answer: str,
    status: str,
    meta: dict[str, Any],
    evidences: list[dict[str, Any]],
    tool_audits: list[dict[str, Any]],
    llm_audits: list[dict[str, Any]],
    usage: dict[str, Any],
    agent_state: dict[str, Any] | None = None,
    agent_state_version: int | None = None,
    elapsed_ms: int = 0,
    run_id: uuid.UUID | None = None,
    worker_id: str | None = None,
    attempt: int | None = None,
) -> tuple[bool | None, bool]:
    """Write one turn and advance its run state in the same transaction."""
    token_prompt = sum(int(item.get("prompt_tokens") or 0) for item in llm_audits)
    token_completion = sum(int(item.get("completion_tokens") or 0) for item in llm_audits)

    with tenant_scope(tenant_id, user_id) as db:
        message = db.scalar(
            select(Message).where(
                Message.id == message_id,
                Message.conversation_id == conversation_id,
            )
        )
        if message is None:
            raise RuntimeError("assistant message not found")
        message.content = answer
        message.status = status
        # 合并而非覆盖：meta 里的 run_id / run_type 是刷新后接回放流的唯一线索，
        # 被终态写入冲掉的话前端就再也找不到这条 run。
        previous = message.meta if isinstance(message.meta, dict) else {}
        message.meta = {**previous, **json_safe(meta)}

        for evidence in evidences:
            db.add(_evidence_row(tenant_id, user_id, message_id, evidence))

        for audit in tool_audits:
            for row in _tool_rows(
                tenant_id, user_id, message_id, conversation_id, request_id, audit
            ):
                db.add(row)

        for audit in llm_audits:
            db.add(
                LlmCall(
                    id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    user_id=user_id,
                    message_id=message_id,
                    request_id=request_id,
                    purpose=str(audit.get("purpose") or ""),
                    model=str(audit.get("model") or ""),
                    prompt_version="v1",
                    temperature=Decimal(str(audit.get("temperature") or 0)),
                    max_tokens=int(audit.get("max_tokens") or 0),
                    prompt_tokens=int(audit.get("prompt_tokens") or 0),
                    completion_tokens=int(audit.get("completion_tokens") or 0),
                    reasoning_tokens=int(audit.get("reasoning_tokens") or 0),
                    finish_reason=audit.get("finish_reason"),
                    latency_ms=int(audit.get("latency_ms") or 0),
                    status=str(audit.get("status") or ""),
                )
            )

        conversation = db.scalar(select(Conversation).where(Conversation.id == conversation_id))
        if conversation is not None:
            conversation.updated_at = datetime.now(timezone.utc)
            if conversation.title in {"", "新会话"}:
                conversation.title = question[:36]
            db.execute(
                text(
                    "update conversations "
                    "set last_message_preview = coalesce(nullif(:preview, ''), last_message_preview), "
                    "    message_count = (select count(*) from messages where conversation_id = :id) "
                    "where id = :id"
                ),
                {"preview": message_preview(answer), "id": conversation_id},
            )

        state_saved: bool | None = None
        if agent_state is not None and agent_state_version is not None:
            result = db.execute(
                text(
                    "update conversations "
                    "set agent_state = cast(:state as jsonb), "
                    "agent_state_version = agent_state_version + 1, "
                    "agent_state_updated_at = now() "
                    "where id = :id and agent_state_version = :version"
                ),
                {
                    "state": __import__("json").dumps(agent_state, ensure_ascii=False),
                    "id": conversation_id,
                    "version": agent_state_version,
                },
            )
            state_saved = int(result.rowcount or 0) == 1

        db.execute(
            text(
                "insert into usage_daily (tenant_id, user_id, day, questions, tool_calls, llm_calls, failed, prompt_tokens, completion_tokens, total_latency_ms) "
                "values (:tenant_id, :user_id, current_date, 1, :tool_calls, :llm_calls, :failed, :prompt_tokens, :completion_tokens, :total_latency_ms) "
                "on conflict (tenant_id, user_id, day) do update set "
                "questions = usage_daily.questions + 1, "
                "tool_calls = usage_daily.tool_calls + excluded.tool_calls, "
                "llm_calls = usage_daily.llm_calls + excluded.llm_calls, "
                "failed = usage_daily.failed + excluded.failed, "
                "prompt_tokens = usage_daily.prompt_tokens + excluded.prompt_tokens, "
                "completion_tokens = usage_daily.completion_tokens + excluded.completion_tokens, "
                "total_latency_ms = usage_daily.total_latency_ms + excluded.total_latency_ms"
            ),
            {
                "tenant_id": tenant_id,
                "user_id": user_id,
                "tool_calls": len(tool_audits),
                "llm_calls": len(llm_audits),
                "failed": 0 if status == "done" else 1,
                "prompt_tokens": token_prompt,
                "completion_tokens": token_completion,
                "total_latency_ms": elapsed_ms,
            },
        )

        run_finalized = False
        if run_id is not None:
            params = {
                "id": str(run_id),
                "tenant_id": str(tenant_id),
                "status": run_terminal_status(status),
            }
            fence = ""
            if worker_id is not None and attempt is not None:
                fence = " and worker_id = :worker_id and attempt = :attempt"
                params["worker_id"] = worker_id
                params["attempt"] = int(attempt)
            result = db.execute(
                text(
                    "update chat_runs"
                    "   set status = :status,"
                    "       finished_at = now(),"
                    "       lease_until = null,"
                    "       error_code = null,"
                    "       error_message = null"
                    " where id = :id and tenant_id = :tenant_id and status = 'running'"
                    + fence
                ),
                params,
            )
            run_finalized = int(result.rowcount or 0) == 1
            if run_finalized:
                db.execute(
                    text(
                        "update session_events "
                        "set expires_at = now() + interval '24 hours' "
                        "where message_id = :message_id and expires_at is null"
                    ),
                    {"message_id": str(message_id)},
                )
            if not run_finalized:
                raise RunFenced(
                    f"run {run_id} is not owned by worker {worker_id} (attempt {attempt})"
                )

    return state_saved, run_finalized

def failed_message_payload(
    code: str,
    message: str,
    retryable: bool = True,
) -> tuple[str, dict[str, Any]]:
    content = (
        "结论：本次回答未完成。\n"
        "依据：服务端没有保存到可用的完整回答。\n"
        "关键数据：本轮无关键数字。\n"
        f"待确认：{message}"
    )
    meta = {
        "error": {"code": code, "message": message},
        # 前端据此决定给「重试」还是「重新提问」；轮询路径读 meta 而不是事件流。
        "retryable": retryable,
        "failed_at": datetime.now(timezone.utc).isoformat(),
    }
    return content, meta

def mark_assistant_failed_in_session(
    db: Session,
    message_id: uuid.UUID,
    status: str,
    code: str,
    message: str,
    retryable: bool = True,
) -> bool:
    """在调用方事务内同步助手消息状态，供租约回收与任务状态一起提交。"""
    row = db.scalar(select(Message).where(Message.id == message_id))
    if row is None or row.status not in {"queued", "running"}:
        return False
    content, meta = failed_message_payload(code, message, retryable)
    row.content = content
    row.status = status
    # 保留 run_id / run_type：失败后前端仍要能连回放看已生成的部分
    previous = row.meta if isinstance(row.meta, dict) else {}
    row.meta = {**previous, **meta}
    row.updated_at = datetime.now(timezone.utc)
    return True

def mark_assistant_failed(
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    message_id: uuid.UUID,
    status: str,
    code: str,
    message: str,
) -> bool:
    """把仍处于 running 的助手消息标记为失败，避免重启后留僵尸。"""
    try:
        with tenant_scope(tenant_id, user_id) as db:
            return mark_assistant_failed_in_session(db, message_id, status, code, message)
    except Exception:
        logger.exception("failed to mark assistant message %s", message_id)
        return False