"""Agent Service 的 ``POST /execute`` 端点。

API 层把同步聊天的执行请求转发到这里：构建 ``ChatRun``，调用现有
``AgentScopeRunner``（或 legacy ``AgentLoop``），把事件原样以 SSE 下发。
落库仍由 runner 内部的 ``persist_turn`` 完成，这里不改事务边界。
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, AsyncIterator

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select, text

from app.agent.agentscope_runtime import AgentScopeRunner
from app.agent.loop import AgentLoop, ChatRun
from app.agent.persist import mark_assistant_failed
from app.config import Settings, get_settings
from app.db import tenant_scope
from app.models import Message

logger = logging.getLogger(__name__)
router = APIRouter(tags=["agent-service"])

_LEGACY_DONE = object()

_SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "X-Accel-Buffering": "no",
    "Connection": "keep-alive",
}

RESEARCH_HINT = "本次回答未在限定时间内完成，建议使用深度研究。"

class ExecuteBody(BaseModel):
    tenant_id: uuid.UUID
    user_id: uuid.UUID
    conversation_id: uuid.UUID
    message_id: uuid.UUID
    question: str = Field(min_length=1, max_length=2000)
    turn: int = Field(ge=1)
    timeout_seconds: float = Field(default=60.0, gt=0)

def _sse(event: str, data: dict[str, Any]) -> str:
    payload = json.dumps(data, ensure_ascii=False, default=str, separators=(",", ":"))
    return f"event: {event}\ndata: {payload}\n\n"

def _convergence_seconds(timeout_seconds: float) -> float:
    """同步聊天总时长只有几十秒，收敛阈值必须按总时长缩放。"""
    return max(10.0, min(60.0, float(timeout_seconds) * 0.35))

def _next_event(iterator: Any) -> Any:
    try:
        return next(iterator)
    except StopIteration:
        return _LEGACY_DONE

def _load_run(body: ExecuteBody, settings: Settings) -> ChatRun | None:
    """还原本轮对话；消息不存在或已结束时返回 None。"""
    with tenant_scope(body.tenant_id, body.user_id) as db:
        assistant = db.scalar(
            select(Message).where(
                Message.id == body.message_id,
                Message.conversation_id == body.conversation_id,
            )
        )
        if assistant is None or assistant.role != "assistant":
            return None
        if assistant.status not in {"queued", "running"}:
            return None

        assistant.status = "running"
        assistant.updated_at = datetime.now(timezone.utc)

        previous = db.scalars(
            select(Message)
            .where(
                Message.conversation_id == body.conversation_id,
                Message.request_id != assistant.request_id,
                Message.status.in_(["done", "degraded"]),
            )
            .order_by(Message.created_at.desc())
            .limit(max(2, int(settings.history_turns) * 2))
        ).all()
        history = [
            {"role": item.role, "content": item.content}
            for item in reversed(previous)
            if item.role in {"user", "assistant"} and item.content
        ]

        return ChatRun(
            tenant_id=body.tenant_id,
            user_id=body.user_id,
            conversation_id=body.conversation_id,
            message_id=body.message_id,
            request_id=assistant.request_id,
            question=body.question,
            history=history,
            turn=max(1, int(body.turn)),
            timeout_seconds=float(body.timeout_seconds),
            convergence_seconds=_convergence_seconds(body.timeout_seconds),
        )

async def _stream(
    run: ChatRun, settings: Settings
) -> AsyncIterator[tuple[str, dict[str, Any]]]:
    if settings.agent_engine == "legacy":
        yield (
            "meta",
            {
                "conversation_id": str(run.conversation_id),
                "message_id": str(run.message_id),
                "request_id": str(run.request_id),
                "timeout_seconds": run.timeout_seconds,
                "engine": "legacy",
                "turn": run.turn,
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
        yield event, data

def _mark_research_suggested(body: ExecuteBody) -> None:
    """超时降级后记录一条建议，前端可从 meta 读出（本次不改前端）。"""
    patch = {"research_suggested": True, "research_hint": RESEARCH_HINT}
    with tenant_scope(body.tenant_id, body.user_id) as db:
        db.execute(
            text(
                "update messages "
                "   set meta = meta || cast(:patch as jsonb), updated_at = now() "
                " where id = :id and tenant_id = :tenant_id"
            ),
            {
                "patch": json.dumps(patch, ensure_ascii=False),
                "id": str(body.message_id),
                "tenant_id": str(body.tenant_id),
            },
        )

async def _run(body: ExecuteBody) -> AsyncIterator[str]:
    settings = get_settings()
    run = await asyncio.to_thread(_load_run, body, settings)
    if run is None:
        yield _sse(
            "error",
            {"code": "RUN_NOT_FOUND", "message": "消息不存在或已结束。"},
        )
        yield _sse("done", {"status": "error", "message_id": str(body.message_id)})
        return

    done: dict[str, Any] | None = None
    try:
        async for event, data in _stream(run, settings):
            if event == "done":
                done = data
            yield _sse(event, data)
    except asyncio.CancelledError:
        # 上游（API 进程）断开：不写失败，交给孤儿回收处理。
        logger.info("execute cancelled; message %s left to orphan recovery", body.message_id)
        raise
    except Exception as exc:
        logger.exception("execute failed for message %s", body.message_id)
        await asyncio.to_thread(
            mark_assistant_failed,
            body.tenant_id,
            body.user_id,
            body.message_id,
            "failed",
            type(exc).__name__,
            "回答生成失败，请重试。",
        )
        yield _sse(
            "error",
            {"code": type(exc).__name__, "message": "回答生成失败，请重试。"},
        )
        yield _sse("done", {"status": "error", "message_id": str(body.message_id)})
        return

    if done is None:
        await asyncio.to_thread(
            mark_assistant_failed,
            body.tenant_id,
            body.user_id,
            body.message_id,
            "failed",
            "PERSIST_FAILED",
            "回答保存失败，请稍后重试。",
        )
        yield _sse(
            "error",
            {"code": "PERSIST_FAILED", "message": "回答保存失败，请稍后重试。"},
        )
        yield _sse("done", {"status": "error", "message_id": str(body.message_id)})
        return

    elapsed_ms = int(done.get("elapsed_ms") or 0)
    if (
        str(done.get("status") or "") == "degraded"
        and elapsed_ms >= int(float(body.timeout_seconds) * 1000 * 0.9)
    ):
        try:
            await asyncio.to_thread(_mark_research_suggested, body)
        except Exception:
            logger.exception("failed to mark research hint for %s", body.message_id)

@router.post("/execute")
def execute(body: ExecuteBody) -> StreamingResponse:
    return StreamingResponse(
        _run(body),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )