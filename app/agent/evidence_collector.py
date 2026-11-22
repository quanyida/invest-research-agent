from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

from app.tools.source_registry import apply_source_naming
from app.tools.ifind_gateway import ToolOutcome
from app.tools.registry import get_tool

FACT_ID_RE = re.compile(r"E\d+-\d+")

@dataclass
class ToolRecord:
    seq: int
    tool: str
    args: dict[str, Any]
    args_hash: str
    status: str
    error_code: str | None
    latency_ms: int
    cached: bool
    query_text: str
    evidence: dict[str, Any] | None
    observation: str
    emitted: bool = False
    upstreams: list[str] = field(default_factory=list)
    channel: str = "unknown"
    split_group: str = ""
    is_primary: bool = True

def args_hash(args: dict[str, Any]) -> str:
    raw = json.dumps(args, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

def _tool_channel(tool: str) -> str:
    try:
        return get_tool(tool).channel
    except KeyError:
        return "unknown"

def _tool_source_kind(tool: str) -> tuple[str, str]:
    try:
        spec = get_tool(tool)
    except KeyError:
        return "unknown", "未知来源"
    return spec.source_kind, spec.source_kind_label

def _host(url: str) -> str:
    try:
        host = urlparse(str(url or "")).netloc.lower()
    except Exception:
        return ""
    return host[4:] if host.startswith("www.") else host


_DATE_RE = re.compile(
    r"(\d{4})\s*[年/-]\s*(\d{1,2})\s*[月/-]\s*(\d{1,2})\s*日?"
    r"(?:[ T](\d{1,2}):(\d{2})(?::(\d{2}))?)?"
)


def _iso_datetime(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).isoformat()
    except ValueError:
        pass
    match = _DATE_RE.search(text)
    if not match:
        return text
    year, month, day, hour, minute, second = match.groups()
    try:
        return datetime(
            int(year),
            int(month),
            int(day),
            int(hour or 0),
            int(minute or 0),
            int(second or 0),
        ).isoformat()
    except ValueError:
        return text


def _split_web_evidence(evidence: dict[str, Any]) -> list[dict[str, Any]]:
    items = evidence.get("source_items")
    if not isinstance(items, list) or len(items) <= 1:
        return []
    raw_text = str(evidence.get("raw_text") or "")
    try:
        raw_obj = json.loads(raw_text) if raw_text else {}
    except (TypeError, ValueError):
        raw_obj = {}
    if not isinstance(raw_obj, dict):
        raw_obj = {}
    if not raw_obj:
        raw_obj = {"query": evidence.get("query_text"), "results": []}
    splits: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        clone = copy.deepcopy(evidence)
        item_obj = copy.deepcopy(raw_obj)
        item_obj["results"] = [item]
        item_obj.pop("truncated", None)
        item_text = json.dumps(item_obj, ensure_ascii=False, indent=2)
        clone["raw_text"] = item_text
        clone["raw_excerpt"] = item_text[:2000]
        clone["source_items"] = [item]
        clone["source_title"] = item.get("title")
        clone["source_url"] = item.get("url")
        clone["source_publisher"] = item.get("site")
        clone["source_published_at"] = item.get("published_at")
        clone["results_count"] = 1
        splits.append(clone)
    return splits if len(splits) > 1 else []


def _split_ifind_evidence(tool: str, evidence: dict[str, Any]) -> list[dict[str, Any]]:
    if tool not in {"ifind.news_search", "ifind.notice_search"}:
        return []
    raw_text = str(evidence.get("raw_text") or "").strip()
    if not raw_text:
        return []
    try:
        envelope = json.loads(raw_text)
    except (TypeError, ValueError):
        return []
    items: Any = None
    if isinstance(envelope, dict):
        data = envelope.get("data")
        if isinstance(data, str):
            try:
                items = json.loads(data)
            except (TypeError, ValueError):
                return []
        elif isinstance(data, list):
            items = data
        elif isinstance(envelope.get("result"), list):
            items = envelope["result"]
    elif isinstance(envelope, list):
        items = envelope
    if not isinstance(items, list) or len(items) <= 1:
        return []
    splits: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        clone = copy.deepcopy(evidence)
        if isinstance(envelope, dict):
            item_envelope = copy.deepcopy(envelope)
            item_envelope["data"] = json.dumps([item], ensure_ascii=False)
            item_text = json.dumps(item_envelope, ensure_ascii=False)
        else:
            item_text = json.dumps([item], ensure_ascii=False)
        clone["raw_text"] = item_text
        clone["raw_excerpt"] = item_text[:2000]
        title = item.get("资讯标题") or item.get("公告标题") or item.get("标题") or ""
        content = (
            item.get("资讯内容")
            or item.get("公告片段内容")
            or item.get("内容")
            or ""
        )
        url = item.get("URL") or item.get("url") or item.get("链接") or ""
        publisher = (
            item.get("来源")
            or item.get("媒体")
            or item.get("机构")
            or _host(str(url))
        )
        published_at = (
            item.get("日期")
            or item.get("发布时间")
            or item.get("时间")
            or ""
        )
        clone["source_title"] = str(title)[:300] or None
        clone["source_url"] = str(url)[:1000] or None
        clone["source_publisher"] = str(publisher)[:120] or None
        clone["source_published_at"] = _iso_datetime(published_at)
        clone["source_items"] = [
            {
                "title": clone["source_title"],
                "url": clone["source_url"],
                "site": clone["source_publisher"],
                "published_at": clone["source_published_at"],
                "snippet": str(content)[:1200],
            }
        ]
        clone["results_count"] = 1
        splits.append(clone)
    return splits if len(splits) > 1 else []


def _split_evidence(tool: str, evidence: dict[str, Any]) -> list[dict[str, Any]]:
    if not evidence.get("raw_text"):
        return []
    return _split_web_evidence(evidence) or _split_ifind_evidence(tool, evidence)


def public_evidence_payload(evidence: dict[str, Any], fact_limit: int = 80) -> dict[str, Any]:
    """Evidence shape for SSE/history. Full raw_text stays out of the event log."""
    payload = apply_source_naming(dict(evidence or {}))
    payload.setdefault("source_kind", payload["channel_kind"])
    payload.setdefault("source_kind_label", payload["channel_kind_label"])

    facts = list(payload.get("facts") or [])
    payload["facts_total"] = len(facts)
    payload["facts"] = facts[:fact_limit]
    if len(facts) > fact_limit:
        payload["facts_omitted"] = len(facts) - fact_limit

    raw_text = str(payload.get("raw_text") or "")
    raw_excerpt = str(payload.get("raw_excerpt") or "")
    payload["raw_excerpt"] = (raw_excerpt or raw_text)[:1200]
    payload.pop("raw_text", None)

    source_items = payload.get("source_items")
    if not isinstance(source_items, list):
        source_items = []
    if source_items:
        first = source_items[0] if isinstance(source_items[0], dict) else {}
        payload.setdefault("source_title", first.get("title"))
        payload.setdefault("source_url", first.get("url"))
        payload.setdefault("source_publisher", first.get("site"))
        payload.setdefault("source_published_at", first.get("published_at"))
    return payload

def _safe(value: Any, limit: int = 260) -> str:
    text = str(value if value is not None else "").replace("\r", " ").replace("\n", " ").strip()
    return text[:limit]

class EvidenceCollector:
    """Turn-scoped evidence, observation and audit collector."""

    def __init__(self, turn: int) -> None:
        self.turn = max(1, int(turn))
        self.records: list[ToolRecord] = []
        self._by_key: dict[tuple[str, str], ToolRecord] = {}
        self.errors: list[str] = []
        self._fact_seq = 0

    def lookup(self, tool: str, key: str) -> ToolRecord | None:
        record = self._by_key.get((tool, key))
        if record is not None and record.status == "ok":
            return record
        return None

    def add_outcome(
        self,
        tool: str,
        args: dict[str, Any],
        outcome: ToolOutcome,
    ) -> ToolRecord:
        key = args_hash(args)
        channel = _tool_channel(tool)
        source_kind, source_kind_label = _tool_source_kind(tool)
        base = copy.deepcopy(outcome.evidence) if outcome.evidence else None
        evidence_list: list[dict[str, Any] | None] = [None]
        if base is not None:
            base["channel"] = channel
            base["source_kind"] = source_kind
            base["source_kind_label"] = source_kind_label
            evidence_list = list(_split_evidence(tool, base) or [base])

        group = f"{tool}:{key}:{len(self.records) + 1}"
        records: list[ToolRecord] = []
        for index, evidence in enumerate(evidence_list):
            seq = len(self.records) + 1
            if evidence is not None:
                evidence["seq"] = seq
                evidence["id"] = f"E{self.turn}-{seq}"
                evidence["citation_id"] = evidence["id"]
                evidence["channel"] = channel
                evidence["source_kind"] = source_kind
                evidence["source_kind_label"] = source_kind_label
                self._rewrite_facts(evidence)
                evidence["source_tool"] = tool
            record = ToolRecord(
                seq=seq,
                tool=tool,
                args=args,
                args_hash=key,
                status=outcome.status,
                error_code=outcome.error_code,
                latency_ms=int(outcome.latency_ms or 0),
                cached=bool(outcome.cached),
                query_text=str(outcome.query_text or ""),
                evidence=evidence,
                observation="",
                upstreams=[str(item.get("tool") or "") for item in outcome.upstream_calls],
                channel=channel,
                split_group=group if len(evidence_list) > 1 else "",
                is_primary=index == 0,
            )
            record.observation = self._observation(record)
            self.records.append(record)
            records.append(record)

        primary = records[0]
        if len(records) > 1:
            primary.observation = "\n\n".join(item.observation for item in records)
        self._by_key[(tool, key)] = primary
        if outcome.status != "ok":
            self.errors.append(outcome.error_code or outcome.status)
        return primary

    def add_error(
        self,
        tool: str,
        args: dict[str, Any],
        code: str,
        message: str,
        latency_ms: int = 0,
    ) -> ToolRecord:
        seq = len(self.records) + 1
        key = args_hash(args)
        channel = _tool_channel(tool)
        record = ToolRecord(
            seq=seq,
            tool=tool,
            args=args,
            args_hash=key,
            status="bad_request",
            error_code=code,
            latency_ms=latency_ms,
            cached=False,
            query_text="",
            evidence=None,
            observation="",
            channel=channel,
        )
        record.observation = self._observation(record, message=message)
        self.records.append(record)
        self.errors.append(code)
        return record

    def _rewrite_facts(self, evidence: dict[str, Any]) -> None:
        facts = list(evidence.get("facts") or [])
        mapping: dict[str, str] = {}
        for fact in facts:
            old = str(fact.get("fact_id") or "")
            self._fact_seq += 1
            new = f"E{self.turn}-{self._fact_seq}"
            fact["fact_id"] = new
            if old:
                mapping[old] = new
        for fact in facts:
            derived = fact.get("derived_from")
            if isinstance(derived, list):
                fact["derived_from"] = [
                    mapping.get(str(item), str(item)) for item in derived
                ]
        evidence["facts"] = facts

    def _observation(self, record: ToolRecord, message: str = "") -> str:
        if record.status != "ok" or record.evidence is None:
            detail = message or record.error_code or record.status
            lines = [
                f'<tool_observation tool="{record.tool}" status="{record.status}" error="{_safe(detail, 160)}">',
                f'query: {_safe(record.query_text, 300)}',
                f"TOOL_ERROR: {_safe(detail, 300)}；本次没有可引用 fact，请不要编造数字。",
                "</tool_observation>",
            ]
            return "\n".join(lines)

        evidence = record.evidence
        raw_text = str(evidence.get("raw_text") or "").strip()
        if raw_text:
            eid = _safe(evidence.get("id"), 30)
            lines = [
                f'<tool_observation tool="{record.tool}" status="ok" cached="{str(record.cached).lower()}" call="{eid}">',
                raw_text,
                "</tool_observation>",
            ]
            return "\n".join(lines)
        facts = list(evidence.get("facts") or [])
        lines = [
            f'<tool_observation tool="{record.tool}" status="ok" cached="{str(record.cached).lower()}" '
            f'as_of="{_safe(evidence.get("as_of_label"), 80)}">',
            f'query: {_safe(record.query_text, 300)}',
        ]
        for fact in facts[:60]:
            lines.append(
                "[{fact_id}] entity={entity} | period={period} | metric={metric} | value={value} | unit={unit}".format(
                    fact_id=_safe(fact.get("fact_id"), 30),
                    entity=_safe(fact.get("entity"), 80),
                    period=_safe(fact.get("period_label") or fact.get("period"), 80),
                    metric=_safe(fact.get("metric"), 120),
                    value=_safe(fact.get("value"), 300),
                    unit=_safe(fact.get("unit"), 30),
                )
            )
        if len(facts) > 60:
            lines.append(f"（其余 {len(facts) - 60} 条 fact 已省略；如需更多请缩小查询范围）")
        if not facts and evidence.get("raw_excerpt"):
            lines.append(f"raw_excerpt: {_safe(evidence.get('raw_excerpt'), 1200)}")
        lines.append("</tool_observation>")
        return "\n".join(lines)

    def drain(self) -> list[ToolRecord]:
        pending = [record for record in self.records if not record.emitted]
        for record in pending:
            record.emitted = True
        return pending

    def public_evidences(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for record in self.records:
            if record.evidence is None:
                continue
            evidence = copy.deepcopy(record.evidence)
            evidence["channel"] = str(evidence.get("channel") or record.channel or "unknown")
            evidence = apply_source_naming(evidence)
            facts = list(evidence.get("facts") or [])
            evidence["facts_total"] = len(facts)
            evidence["facts"] = facts[:80]
            if len(facts) > 80:
                evidence["facts_omitted"] = len(facts) - 80
            raw_text = str(evidence.get("raw_text") or "")
            if raw_text:
                evidence["raw_text"] = raw_text
                evidence["raw_excerpt"] = raw_text[:2000]
            else:
                evidence["raw_excerpt"] = str(evidence.get("raw_excerpt") or "")[:1200]
            result.append(evidence)
        return result

    def audits(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for record in self.records:
            if not record.is_primary:
                continue
            evidence = record.evidence or {}
            result.append(
                {
                    "kind": "tool",
                    "step": record.seq,
                    "tool": record.tool,
                    "server": str(evidence.get("server") or ""),
                    "upstream_tool": str(evidence.get("upstream_tool") or ""),
                    "args": record.args,
                    "args_hash": record.args_hash,
                    "query_text": record.query_text,
                    "status": record.status,
                    "cached": record.cached,
                    "latency_ms": record.latency_ms,
                    "error_code": record.error_code,
                    "upstreams": record.upstreams,
                }
            )
        return result

    def error_messages(self) -> list[str]:
        return list(dict.fromkeys(item for item in self.errors if item))
