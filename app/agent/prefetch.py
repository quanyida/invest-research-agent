from __future__ import annotations

import asyncio
import re
from typing import Any

from app.agent.evidence_collector import ToolRecord
from app.agent.run_context import RunContext
from app.tools.web_search import MAX_QUERY_CHARS, get_web_search

LEADING_PREFIXES = (
    "请帮我",
    "帮我",
    "麻烦",
    "请问",
    "我想知道",
    "想问一下",
    "告诉我",
    "详细",
    "深度",
    "分析一下",
    "分析下",
    "分析",
    "研究一下",
    "研究",
    "看看",
    "查一下",
    "查询",
)


def build_prefetch_query(question: str) -> str:
    """Build a deterministic web query without spending another model call."""
    value = re.sub(r"\s+", " ", str(question or "")).strip()
    for prefix in LEADING_PREFIXES:
        if value.startswith(prefix):
            value = value[len(prefix) :].lstrip("：:，,。 ")
    value = value or str(question or "").strip()
    return value[:MAX_QUERY_CHARS].strip()


async def prefetch_web(ctx: RunContext) -> ToolRecord | None:
    """Run one tenant-scoped web search before the Agent starts reasoning."""
    settings = ctx.settings
    if not settings.web_search_ready or "web.search" not in ctx.allowed_tools:
        ctx.prefetch_info = {"status": "skipped", "reason": "web_search_unavailable"}
        return None

    query = build_prefetch_query(getattr(ctx.run, "question", ""))
    if len(query) < 2:
        ctx.prefetch_info = {"status": "skipped", "reason": "empty_query"}
        return None

    args: dict[str, Any] = {
        "query": query,
        "count": min(6, max(1, int(settings.web_search_max_results))),
        "freshness": "oneMonth",
    }
    try:
        outcome = await asyncio.wait_for(
            asyncio.to_thread(
                get_web_search().call,
                "web.search",
                args,
                ctx.run.tenant_id,
                ctx.run.user_id,
            ),
            timeout=max(2.0, float(settings.web_search_timeout_seconds) + 2.0),
        )
        record = ctx.collector.add_outcome("web.search", args, outcome)
    except Exception as exc:
        record = ctx.collector.add_error(
            "web.search",
            args,
            "WEB_PREFETCH_FAILED",
            str(exc)[:240],
        )

    ctx.prefetch_block = record.observation
    ctx.prefetch_info = {
        "status": record.status,
        "query": query,
        "cached": record.cached,
        "latency_ms": record.latency_ms,
        "error_code": record.error_code,
        "evidence_id": str((record.evidence or {}).get("id") or ""),
    }
    return record