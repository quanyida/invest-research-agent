from __future__ import annotations

from typing import Any

from app.agent.router import Plan
from app.tools.registry import tool_specs

STOCK_TOOLS = [
    "ifind.stock_quote",
    "ifind.stock_financials",
    "ifind.stock_performance",
    "ifind.stock_holders",
    "ifind.stock_events",
    "ifind.stock_risk",
    "ifind.stock_summary",
]

GLOBAL_TOOLS = [
    "ifind.global_profile",
    "ifind.global_quotes",
    "ifind.global_financials",
    "ifind.global_events",
    "ifind.global_screen",
]

NEWS_TOOLS = ["ifind.news_search", "ifind.notice_search"]
MACRO_TOOLS = ["ifind.macro"]
INDEX_TOOLS = ["ifind.index", "ifind.stock_performance"]

CORE_TOOLS = [
    "ifind.stock_quote",
    "ifind.stock_financials",
    "ifind.stock_performance",
    "ifind.news_search",
    "ifind.notice_search",
    "ifind.macro",
    "ifind.index",
    "ifind.global_profile",
    "ifind.global_quotes",
    "ifind.global_financials",
]

MAX_TOOLS = 10

def _unique(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return result


def resolve_agentscope_tools() -> list[str]:
    """Return the full tenant tool catalog for AgentScope's own planning."""
    from app.config import get_settings

    settings = get_settings()
    return [
        spec.name
        for spec in tool_specs()
        if spec.provider != "web" or settings.web_search_ready
    ]


def resolve_allowed_tools(plan: Plan, domain: str) -> list[str]:
    """Return the small, per-turn tool surface exposed to the model."""
    if plan.unsupported:
        return []

    intent = plan.intent
    if domain == "global" or intent.startswith("global"):
        base = GLOBAL_TOOLS + NEWS_TOOLS + MACRO_TOOLS + INDEX_TOOLS
    elif intent == "macro":
        base = MACRO_TOOLS + INDEX_TOOLS + NEWS_TOOLS + ["ifind.stock_quote"]
    elif intent == "news":
        base = NEWS_TOOLS + [
            "ifind.stock_quote",
            "ifind.stock_events",
            "ifind.global_quotes",
            "ifind.global_events",
            "ifind.macro",
            "ifind.index",
        ]
    elif intent in {"index", "performance"}:
        base = INDEX_TOOLS + NEWS_TOOLS + MACRO_TOOLS + ["ifind.stock_quote"]
    elif intent in {"screen"}:
        base = ["ifind.screen"] + STOCK_TOOLS[:5] + NEWS_TOOLS
    elif intent in {"quote", "financials", "holders", "events", "risk", "compare"}:
        base = STOCK_TOOLS + NEWS_TOOLS + MACRO_TOOLS + INDEX_TOOLS
    else:
        base = CORE_TOOLS

    planned = [item.name for item in plan.tools]
    return _unique(planned + base)[:MAX_TOOLS]

def allowed_payload(allowed: list[str]) -> list[dict[str, Any]]:
    return [{"name": name} for name in allowed]
