from __future__ import annotations

import uuid
from datetime import date, timedelta

from fastapi import APIRouter, Query, Request
from sqlalchemy import select

from app.api.deps import require_principal
from app.config import get_settings
from app.db import tenant_scope
from app.models import UsageDaily

router = APIRouter(prefix="/api/v1", tags=["usage"])

def _row_payload(row: UsageDaily | None, day: date) -> dict:
    if row is None:
        return {
            "day": day.isoformat(),
            "questions": 0,
            "tool_calls": 0,
            "llm_calls": 0,
            "failed": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_latency_ms": 0,
        }
    return {
        "day": row.day.isoformat(),
        "questions": row.questions,
        "tool_calls": row.tool_calls,
        "llm_calls": row.llm_calls,
        "failed": row.failed,
        "prompt_tokens": row.prompt_tokens,
        "completion_tokens": row.completion_tokens,
        "total_latency_ms": row.total_latency_ms,
    }

@router.get("/usage")
def usage(request: Request, days: int = Query(default=7, ge=1, le=90)) -> dict:
    principal = require_principal(request)
    today = date.today()
    start = today - timedelta(days=days - 1)
    with tenant_scope(principal.tenant_id, principal.user_id) as db:
        rows = db.scalars(
            select(UsageDaily)
            .where(UsageDaily.day >= start, UsageDaily.day <= today)
            .order_by(UsageDaily.day)
        ).all()
    by_day = {row.day: row for row in rows}
    items = [_row_payload(by_day.get(start + timedelta(days=index)), start + timedelta(days=index)) for index in range(days)]
    summary = {
        "questions": sum(item["questions"] for item in items),
        "tool_calls": sum(item["tool_calls"] for item in items),
        "llm_calls": sum(item["llm_calls"] for item in items),
        "failed": sum(item["failed"] for item in items),
        "prompt_tokens": sum(item["prompt_tokens"] for item in items),
        "completion_tokens": sum(item["completion_tokens"] for item in items),
        "total_latency_ms": sum(item["total_latency_ms"] for item in items),
    }
    settings = get_settings()
    return {
        "today": _row_payload(by_day.get(today), today),
        "summary": summary,
        "items": items,
        "limits": {
            "questions_per_day": 50,
            "tool_calls_per_day": 400,
            "requests_per_minute": 20,
            "task_timeout_seconds": settings.task_timeout_seconds,
            "max_tool_calls_per_question": None,
            "model_max_attempts": settings.model_max_attempts,
        },
        "model": settings.llm_model,
    }