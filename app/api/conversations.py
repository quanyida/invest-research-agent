from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import case, select, text

from app.agent.citation_support import build_citation_support
from app.agent.persist import mark_assistant_failed_in_session
from app.api.deps import check_origin, require_principal
from app.config import get_settings
from app.db import tenant_scope
from app.models import Conversation, Evidence, Message
from app.tools.channels import resolve_channel
from app.tools.registry import get_tool
from app.tools.source_registry import apply_source_naming

router = APIRouter(prefix="/api/v1/conversations", tags=["conversations"])


class CreateConversationBody(BaseModel):
    title: str | None = Field(default=None, max_length=120)


def _conversation_payload(row: Conversation) -> dict:
    return {
        "id": str(row.id),
        "title": row.title,
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
    }


def _mark_orphan_messages(db: Any, rows: list[Message]) -> int:
    """把超时的 running 消息就地标为失败。

    只处理没有 chat_runs 的记录：research 任务由租约回收负责。
    """
    threshold = max(0.0, float(get_settings().orphan_threshold_seconds))
    now = datetime.now(timezone.utc)
    marked = 0
    for row in rows:
        if row.status != "running" or row.updated_at is None:
            continue
        if (now - row.updated_at).total_seconds() <= threshold:
            continue
        has_run = db.scalar(
            text("select count(*) from chat_runs where message_id = :id"),
            {"id": str(row.id)},
        )
        if has_run:
            continue
        if mark_assistant_failed_in_session(
            db,
            row.id,
            "failed",
            "CHAT_ORPHANED",
            "执行中断，请重试。",
        ):
            marked += 1
    return marked

def _message_turn(row: Message) -> int | None:
    meta = row.meta if isinstance(row.meta, dict) else {}
    try:
        turn = int(meta.get("turn"))
    except (TypeError, ValueError):
        return None
    return turn if turn > 0 else None

def _evidence_payload(row: Evidence, turn: int | None = None) -> dict:
    channel = resolve_channel(row.channel)
    try:
        tool_spec = get_tool(row.tool)
        source_kind = tool_spec.source_kind
        source_kind_label = tool_spec.source_kind_label
    except KeyError:
        source_kind = channel.kind
        source_kind_label = channel.kind_label
    payload = {
        "id": str(row.id),
        "citation_id": f"E{turn}-{row.seq}" if turn is not None else None,
        "seq": row.seq,
        "tool": row.tool,
        "server": row.server,
        "upstream_tool": row.upstream_tool,
        "args": row.args,
        "args_hash": row.args_hash,
        "query_text": row.query_text,
        "period_end": row.period_end.isoformat() if row.period_end else None,
        "data_as_of": row.data_as_of.isoformat() if row.data_as_of else None,
        "as_of_label": row.as_of_label,
        "fetched_at": row.fetched_at.isoformat(),
        "sections": (row.payload or {}).get("sections", []),
        "facts": row.facts,
        "raw_excerpt": row.raw_excerpt,
        "truncated": row.truncated,
        "parse_quality": float(row.parse_quality),
        "channel": channel.key,
        "channel_label": channel.label,
        "channel_kind": channel.kind,
        "channel_kind_label": channel.kind_label,
        "source_kind": source_kind,
        "source_kind_label": source_kind_label,
        "source_title": row.source_title,
        "source_url": row.source_url,
        "source_publisher": row.source_publisher,
        "source_published_at": row.source_published_at.isoformat() if row.source_published_at else None,
    }
    return apply_source_naming(payload)

def _message_payload(row: Message, evidence: list[Evidence]) -> dict:
    turn = _message_turn(row)
    evidence_payload = [_evidence_payload(item, turn) for item in evidence]
    meta = row.meta
    if row.role == "assistant" and isinstance(meta, dict):
        verify = meta.get("verify")
        if isinstance(verify, dict):
            support = build_citation_support(row.content or "", evidence_payload)
            if support or "citation_support" not in verify:
                meta = {**meta, "verify": {**verify, "citation_support": support}}
    return {
        "id": str(row.id),
        "role": row.role,
        "content": row.content,
        "status": row.status,
        "created_at": row.created_at.isoformat(),
        "meta": meta,
        "turn": turn,
        "evidence": evidence_payload,
    }

@router.post("")
def create_conversation(body: CreateConversationBody, request: Request) -> dict:
    check_origin(request)
    principal = require_principal(request)
    title = (body.title or "新会话").strip()[:120] or "新会话"
    with tenant_scope(principal.tenant_id, principal.user_id) as db:
        row = Conversation(
            id=uuid.uuid4(),
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            title=title,
        )
        db.add(row)
        db.flush()
        payload = _conversation_payload(row)
    return payload


@router.get("")
def list_conversations(
    request: Request,
    limit: int = Query(default=30, ge=1, le=100),
) -> dict:
    principal = require_principal(request)
    with tenant_scope(principal.tenant_id, principal.user_id) as db:
        rows = db.scalars(
            select(Conversation)
            .where(Conversation.deleted_at.is_(None))
            .order_by(Conversation.updated_at.desc())
            .limit(limit)
        ).all()
        return {"items": [_conversation_payload(row) for row in rows]}


@router.get("/{conversation_id}/messages")
def list_messages(conversation_id: uuid.UUID, request: Request) -> dict:
    principal = require_principal(request)
    with tenant_scope(principal.tenant_id, principal.user_id) as db:
        conversation = db.scalar(
            select(Conversation).where(
                Conversation.id == conversation_id,
                Conversation.deleted_at.is_(None),
            )
        )
        if conversation is None:
            raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "会话不存在"})
        role_order = case((Message.role == "user", 0), else_=1)
        messages = db.scalars(
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.created_at, role_order, Message.id)
        ).all()
        _mark_orphan_messages(db, list(messages))
        evidence_rows = db.scalars(
            select(Evidence)
            .where(Evidence.message_id.in_([item.id for item in messages]))
            .order_by(Evidence.message_id, Evidence.seq)
        ).all() if messages else []
        by_message: dict[uuid.UUID, list[Evidence]] = {}
        for item in evidence_rows:
            by_message.setdefault(item.message_id, []).append(item)
        return {
            "conversation": _conversation_payload(conversation),
            "items": [_message_payload(item, by_message.get(item.id, [])) for item in messages],
        }


@router.delete("/{conversation_id}")
def delete_conversation(conversation_id: uuid.UUID, request: Request) -> dict:
    check_origin(request)
    principal = require_principal(request)
    with tenant_scope(principal.tenant_id, principal.user_id) as db:
        conversation = db.scalar(
            select(Conversation).where(
                Conversation.id == conversation_id,
                Conversation.deleted_at.is_(None),
            )
        )
        if conversation is None:
            raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "会话不存在"})
        conversation.deleted_at = datetime.now(timezone.utc)
        conversation.updated_at = conversation.deleted_at
    return {"ok": True}