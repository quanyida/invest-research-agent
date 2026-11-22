from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

CATALOG_PATH = Path(__file__).with_name("skill_catalog.json")

DOMAIN_SERVERS = {
    "stock": {"stock"},
    "global": {"global"},
    "index": {"index"},
    "macro": {"edb"},
    "news": {"news"},
}


@lru_cache(maxsize=1)
def _catalog() -> dict:
    try:
        return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"domains": {}}


def domain_label(domain: str) -> str:
    item = (_catalog().get("domains") or {}).get(domain) or {}
    return str(item.get("label") or domain)


def tools_in(domain: str) -> dict:
    item = (_catalog().get("domains") or {}).get(domain) or {}
    return item.get("tools") or {}


def example_for(upstream: str) -> str:
    for domain in (_catalog().get("domains") or {}).values():
        tool = (domain.get("tools") or {}).get(upstream)
        if tool and tool.get("example"):
            return str(tool["example"])
    return ""
