from __future__ import annotations

import re
from typing import Any

from app.tools.source_registry import resolve_website, source_domain_for_tool

CITATION_OPEN = r"[\[（(]"
CITATION_CLOSE = r"[\]）)]"
CITATION_BLOCK_RE = re.compile(
    rf"{CITATION_OPEN}E\d+-\d+{CITATION_CLOSE}"
    rf"(?:[ \t]*[，,、;；]?[ \t]*{CITATION_OPEN}E\d+-\d+{CITATION_CLOSE})*"
)
CITATION_RE = re.compile(rf"{CITATION_OPEN}(E\d+-\d+){CITATION_CLOSE}")

def _citation_id(evidence: dict[str, Any]) -> str:
    explicit = str(evidence.get("citation_id") or "")
    if re.fullmatch(r"E\d+-\d+", explicit):
        return explicit
    legacy = str(evidence.get("id") or "")
    return legacy if re.fullmatch(r"E\d+-\d+", legacy) else ""

def _channel(evidence: dict[str, Any]) -> str:
    return str(evidence.get("channel") or "unknown").lower()

def _tool(evidence: dict[str, Any]) -> str:
    return str(evidence.get("tool") or evidence.get("source_tool") or "")

def _retrieval_key(evidence: dict[str, Any]) -> str:
    args_hash = str(evidence.get("args_hash") or "")
    if args_hash:
        return args_hash
    tool = str(evidence.get("tool") or "")
    seq = str(evidence.get("seq") or "")
    return f"{tool}:{seq}"

def _source_key(evidence: dict[str, Any]) -> str:
    explicit = str(evidence.get("source_key") or "")
    if explicit:
        return explicit
    channel = _channel(evidence)
    if channel == "ifind":
        return f"ifind:{source_domain_for_tool(_tool(evidence)).key}"
    if channel == "web":
        site_key, _ = resolve_website(
            evidence.get("source_url"), evidence.get("source_publisher")
        )
        if site_key:
            return f"web:{site_key}"
        # 站点无法识别时才退回“同一次检索”，避免同一块显示多个站名却只算一个来源。
        return f"web-retrieval:{_retrieval_key(evidence)}"
    return f"{channel}:{_retrieval_key(evidence) or _citation_id(evidence)}"

def _source_name(evidence: dict[str, Any]) -> str:
    explicit = str(evidence.get("source_name") or "")
    if explicit:
        return explicit
    channel = _channel(evidence)
    if channel == "ifind":
        return source_domain_for_tool(_tool(evidence)).label
    if channel == "web":
        _, name = resolve_website(
            evidence.get("source_url"), evidence.get("source_publisher")
        )
        return name
    return str(evidence.get("channel_label") or "未知来源")

def _same_source(left: dict[str, Any], right: dict[str, Any]) -> bool:
    if _channel(left) != _channel(right):
        return False
    return _source_key(left) == _source_key(right)

def _independent_source_count(evidences: list[dict[str, Any]]) -> int:
    if not evidences:
        return 0
    parent = list(range(len(evidences)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    for right in range(len(evidences)):
        for left in range(right):
            if _same_source(evidences[left], evidences[right]):
                union(left, right)
    return len({find(index) for index in range(len(evidences))})

def _source_groups(cited: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for evidence in cited:
        key = _source_key(evidence)
        if key not in grouped:
            grouped[key] = {"key": key, "label": _source_name(evidence), "citations": []}
            order.append(key)
        citation_id = _citation_id(evidence)
        if citation_id and citation_id not in grouped[key]["citations"]:
            grouped[key]["citations"].append(citation_id)
    return [grouped[key] for key in order]

def build_citation_support(
    answer: str,
    evidences: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return independent-source counts for each contiguous citation group.

    Source rules:
    - iFinD: same data domain (financials/news/...) is one source.
    - Web: same website is one source; same retrieval only when the site is unknown.
    - Different channels are always independent.
    """
    by_citation = {
        citation_id: evidence
        for evidence in evidences or []
        if (citation_id := _citation_id(evidence))
    }
    groups: list[dict[str, Any]] = []
    for block in CITATION_BLOCK_RE.finditer(str(answer or "")):
        ids: list[str] = []
        for match in CITATION_RE.finditer(block.group(0)):
            citation_id = match.group(1)
            if citation_id not in ids:
                ids.append(citation_id)
        cited = [by_citation[citation_id] for citation_id in ids if citation_id in by_citation]
        if not cited:
            continue
        count = _independent_source_count(cited)
        groups.append(
            {
                "citations": ids,
                "independent_sources": count,
                "single_source": count == 1,
                "sources": _source_groups(cited),
            }
        )
    return groups