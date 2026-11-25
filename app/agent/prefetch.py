from __future__ import annotations

import asyncio
import re
from typing import Any

from app.agent.evidence_collector import ToolRecord
from app.agent.run_context import RunContext
from app.tools.web_search import MAX_QUERY_CHARS, get_web_search

INSTRUCTION_PHRASES = tuple(
    sorted(
        {
            "多渠道交叉验证",
            "对比分析下",
            "帮我",
            "请帮我",
            "麻烦",
            "请问",
            "我想知道",
            "想问一下",
            "告诉我",
            "对比分析",
            "分析一下",
            "分析下",
            "各方面",
            "多渠道",
            "交叉验证",
            "详细",
            "深度",
            "梳理",
            "总结",
            "看看",
            "查一下",
            "查询",
            "研究一下",
            "研究",
            "一下",
        },
        key=len,
        reverse=True,
    )
)

_STRUCTURAL_WORDS = {"的", "在", "和", "与", "以及"}
_STRUCTURAL_RE = re.compile(
    r"(?<=[\u4e00-\u9fff])(?:以及|的|在|和|与)(?=[\u4e00-\u9fff])"
    r"|(?<=[A-Za-z0-9])(?:以及|的|在|和|与)(?=[\u4e00-\u9fff])"
    r"|(?<=[\u4e00-\u9fff])(?:以及|的|在|和|与)(?=[A-Za-z0-9])"
)
_PUNCTUATION_RE = re.compile(r"[，,。；;：:、！？?!（）()\[\]【】{}<>/\\|\n\r\t]+")
_PERIOD_RE = re.compile(r"20\d{2}(?:Q[1-4]|H[1-2]|年)?")
_SPACE_RE = re.compile(r"\s+")


def build_prefetch_query(question: str) -> str:
    """Build a deterministic keyword query without spending another model call."""
    original = _SPACE_RE.sub(" ", str(question or "")).strip()
    if not original:
        return ""
    value = original
    for phrase in INSTRUCTION_PHRASES:
        value = value.replace(phrase, " ")
    value = _PUNCTUATION_RE.sub(" ", value)
    value = _STRUCTURAL_RE.sub(" ", value)
    value = _PERIOD_RE.sub(lambda match: f" {match.group(0)} ", value)

    tokens: list[str] = []
    seen: set[str] = set()
    for token in _SPACE_RE.split(value):
        token = token.strip(" \t\r\n-_")
        if token.endswith("的") and len(token) > 1:
            token = token[:-1].rstrip()
        if not token or token in _STRUCTURAL_WORDS:
            continue
        key = token.lower()
        if key in seen:
            continue
        seen.add(key)
        tokens.append(token)

    meaningful = [token for token in tokens if len(token) >= 2]
    if len(meaningful) < 2:
        return original[:MAX_QUERY_CHARS].strip()
    return " ".join(tokens)[:MAX_QUERY_CHARS].strip()


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