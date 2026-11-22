from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
import redis as redis_lib

from app.config import ROOT, get_settings
from app.tools.parser import extract_texts, parse_ifind_response
from app.tools.registry import ToolSpec, get_tool
from app.tools.rewrite import (
    build_query,
    financial_period_label,
    normalize_date,
    normalize_metrics,
    quote_upstream_args,
    validate_args,
)


SERVER_PATHS = {
    "stock": "hexin-ifind-ds-stock-mcp",
    "news": "hexin-ifind-ds-news-mcp",
    "edb": "hexin-ifind-ds-edb-mcp",
    "index": "hexin-ifind-ds-index-mcp",
    "global": "hexin-ifind-ds-global-stock-mcp",
}

def _result_payload_text(raw: Any) -> str:
    """Return iFinD's content text without interpreting business fields."""
    if not isinstance(raw, dict):
        return json.dumps(raw, ensure_ascii=False)
    result = raw.get("result")
    if isinstance(result, dict) and result.get("isError"):
        detail = result.get("content")
        raise RuntimeError(f"UPSTREAM_ERROR:{str(detail)[:200]}")
    content = result.get("content") if isinstance(result, dict) else None
    if isinstance(content, list):
        texts = [
            item.get("text")
            for item in content
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        ]
        if texts:
            return "\n".join(texts)
    if isinstance(result, dict) and isinstance(result.get("text"), str):
        return result["text"]
    return json.dumps(raw, ensure_ascii=False)

def _format_decimal(value: Decimal) -> str:
    quantized = value.quantize(Decimal("0.0001"))
    text = format(quantized, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _append_derived_yoy(parsed: dict[str, Any], args: dict[str, Any]) -> None:
    target = normalize_date(args.get("period"))
    if not target:
        return
    current = date.fromisoformat(target)
    try:
        prior = date(current.year - 1, current.month, current.day).isoformat()
    except ValueError:
        return
    metrics = normalize_metrics(args.get("metrics"))
    facts = parsed.get("facts") or []
    for metric in metrics:
        target_fact = next(
            (
                fact
                for fact in facts
                if fact.get("period") == target
                and fact.get("metric") == metric
                and fact.get("unit") != "text"
            ),
            None,
        )
        prior_fact = next(
            (
                fact
                for fact in facts
                if fact.get("period") == prior
                and fact.get("metric") == metric
                and fact.get("unit") != "text"
            ),
            None,
        )
        if target_fact is None or prior_fact is None:
            continue
        if any(
            fact.get("period") == target
            and metric in str(fact.get("metric") or "")
            and "同比" in str(fact.get("metric") or "")
            for fact in facts
        ):
            continue
        try:
            target_value = Decimal(str(target_fact.get("value")))
            prior_value = Decimal(str(prior_fact.get("value")))
        except (InvalidOperation, TypeError, ValueError):
            continue
        target_unit = str(target_fact.get("unit") or "")
        prior_unit = str(prior_fact.get("unit") or "")
        if target_unit != prior_unit:
            continue
        if target_unit == "%":
            change = target_value - prior_value
            unit = "个百分点"
            derived_metric = f"{metric}同比变化"
        else:
            if prior_value == 0:
                continue
            change = (target_value - prior_value) / abs(prior_value) * Decimal("100")
            unit = "%"
            derived_metric = f"{metric}同比"
        facts.append(
            {
                "fact_id": f"E1-{len(facts) + 1}",
                "entity": target_fact.get("entity"),
                "period": target,
                "metric": derived_metric,
                "value": _format_decimal(change),
                "unit": unit,
                "source_row": target_fact.get("source_row"),
                "section": "同比计算",
                "derived": True,
                "derived_from": [prior_fact.get("fact_id"), target_fact.get("fact_id")],
            }
        )


def _extract_edb_indicator(raw: Any, query: str = "") -> str | None:
    names: list[str] = []
    for text in extract_texts(raw):
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            continue
        data = parsed.get("data") if isinstance(parsed, dict) else None
        content = data.get("data") if isinstance(data, dict) else None
        if not isinstance(content, str):
            continue
        normalized = content.replace('\\"', '"')
        for match in re.finditer(r'"indexName"\s*:\s*"([^"]+)"', normalized):
            name = match.group(1)
            if name not in names:
                names.append(name)
    if not names:
        return None
    query_chars = {char for char in query if char.isalnum()}

    def score(name: str) -> tuple[float, int, int]:
        name_chars = [char for char in name if char.isalnum()]
        if not name_chars:
            return (0.0, 0, 0)
        common = sum(1 for char in name_chars if char in query_chars)
        coverage = common / len(name_chars)
        return (coverage, -(len(name_chars) - common), -len(name_chars))

    return max(names, key=score)


class TTLCache:
    def __init__(self) -> None:
        self._items: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> Any | None:
        now = time.monotonic()
        with self._lock:
            item = self._items.get(key)
            if not item:
                return None
            expires, value = item
            if expires <= now:
                self._items.pop(key, None)
                return None
            return value

    def set(self, key: str, value: Any, ttl: int) -> None:
        with self._lock:
            self._items[key] = (time.monotonic() + ttl, value)

    def delete(self, key: str) -> None:
        with self._lock:
            self._items.pop(key, None)


class HybridCache:
    def __init__(self, prefix: str, url: str) -> None:
        self._local = TTLCache()
        self._prefix = prefix
        self._url = url
        self._redis: Any | None = None
        self._retry_at = 0.0
        self._connect()

    def _connect(self) -> None:
        if not self._url or time.monotonic() < self._retry_at:
            return
        self._retry_at = time.monotonic() + 60
        try:
            client = redis_lib.Redis.from_url(
                self._url,
                decode_responses=True,
                socket_connect_timeout=0.4,
                socket_timeout=0.6,
            )
            client.ping()
            self._redis = client
        except Exception:
            self._redis = None

    def _client(self) -> Any | None:
        if self._redis is None:
            self._connect()
        return self._redis

    def get(self, key: str) -> Any | None:
        client = self._client()
        if client is not None:
            try:
                raw = client.get(f"{self._prefix}:{key}")
                if raw:
                    return json.loads(raw)
            except Exception:
                self._redis = None
        return self._local.get(key)

    def set(self, key: str, value: Any, ttl: int) -> None:
        self._local.set(key, value, ttl)
        client = self._client()
        if client is None:
            return
        try:
            client.setex(f"{self._prefix}:{key}", max(1, int(ttl)), json.dumps(value, ensure_ascii=False))
        except Exception:
            self._redis = None

    def delete(self, key: str) -> None:
        self._local.delete(key)
        client = self._client()
        if client is None:
            return
        try:
            client.delete(f"{self._prefix}:{key}")
        except Exception:
            self._redis = None

    @property
    def redis_available(self) -> bool:
        return self._redis is not None

@dataclass
class ToolOutcome:
    status: str
    evidence: dict[str, Any] | None
    error_code: str | None
    latency_ms: int
    cached: bool
    query_text: str
    upstream_calls: list[dict[str, Any]]
    raw_text: str | None = None


class IfindGateway:
    def __init__(self) -> None:
        self.settings = get_settings()
        self._sessions: dict[str, str] = {}
        self._locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
        self._request_ids: dict[str, int] = defaultdict(int)
        self._cache = HybridCache(self.settings.redis_prefix, self.settings.redis_url)
        self._circuits: dict[str, float] = {}
        self._circuit_lock = threading.Lock()
        self._runtime = ROOT / ".runtime"

    def _next_id(self, server: str) -> int:
        self._request_ids[server] += 1
        return self._request_ids[server]

    def _circuit_open(self, tool_name: str) -> bool:
        with self._circuit_lock:
            until = self._circuits.get(tool_name, 0.0)
            if until and until <= time.monotonic():
                self._circuits.pop(tool_name, None)
                return False
            return until > time.monotonic()

    def _open_circuit(self, tool_name: str, seconds: int = 600) -> None:
        with self._circuit_lock:
            self._circuits[tool_name] = time.monotonic() + seconds

    def _dump_schema_changed(self, tool_name: str, raw: Any, detail: dict[str, Any]) -> str | None:
        try:
            folder = self._runtime / "schema_changes"
            folder.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            safe_tool = re.sub(r"[^a-zA-Z0-9_.-]+", "_", tool_name)
            path = folder / f"{stamp}-{safe_tool}-{uuid.uuid4().hex[:8]}.json"
            path.write_text(
                json.dumps(
                    {"tool": tool_name, "at": datetime.now(timezone.utc).isoformat(), "detail": detail, "raw": raw},
                    ensure_ascii=False,
                    indent=2,
                    default=str,
                ),
                encoding="utf-8",
            )
            return str(path)
        except Exception:
            return None

    def _url(self, server: str) -> str:
        path = SERVER_PATHS.get(server)
        if not path:
            raise ValueError(f"unsupported server: {server}")
        return f"{self.settings.ifind_mcp_base}/{path}"

    def _headers(self, server: str) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": self.settings.ifind_mcp_token,
            "User-Agent": "invest-agent-v1",
        }
        session_id = self._sessions.get(server)
        if session_id:
            headers["Mcp-Session-Id"] = session_id
        return headers

    @staticmethod
    def _parse_sse(text: str) -> Any:
        payloads: list[Any] = []
        data_lines: list[str] = []
        for raw in text.replace("\r\n", "\n").split("\n"):
            if raw.startswith("data:"):
                data_lines.append(raw[5:].strip())
            elif raw.strip() == "" and data_lines:
                block = "\n".join(data_lines).strip()
                data_lines = []
                if block and block != "[DONE]":
                    try:
                        payloads.append(json.loads(block))
                    except json.JSONDecodeError:
                        continue
        if data_lines:
            block = "\n".join(data_lines).strip()
            if block and block != "[DONE]":
                try:
                    payloads.append(json.loads(block))
                except json.JSONDecodeError:
                    pass
        for payload in reversed(payloads):
            if isinstance(payload, dict) and ("result" in payload or "error" in payload):
                return payload
        return payloads[-1] if payloads else None

    def _decode_response(self, response: httpx.Response) -> Any:
        content_type = response.headers.get("content-type", "")
        text = response.text
        if "text/event-stream" in content_type:
            return self._parse_sse(text)
        if not text.strip():
            return None
        try:
            return response.json()
        except json.JSONDecodeError:
            return self._parse_sse(text)

    def _post(self, server: str, payload: dict[str, Any], timeout: float | None = None) -> tuple[httpx.Response, Any]:
        with httpx.Client(trust_env=True, timeout=timeout or self.settings.ifind_mcp_timeout_seconds) as client:
            response = client.post(self._url(server), json=payload, headers=self._headers(server))
        return response, self._decode_response(response)

    def _ensure_session(self, server: str) -> None:
        if server in self._sessions:
            return
        with self._locks[server]:
            if server in self._sessions:
                return
            cached = self._cache.get(f"mcp:session:{server}")
            if isinstance(cached, dict) and cached.get("id"):
                self._sessions[server] = str(cached["id"])
                return
            payload = {
                "jsonrpc": "2.0",
                "id": self._next_id(server),
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "invest-agent-v1", "version": "1.0.0"},
                },
            }
            response, data = self._post(server, payload, timeout=20)
            if response.status_code >= 400:
                raise RuntimeError(f"initialize HTTP {response.status_code}: {data}")
            session_id = response.headers.get("Mcp-Session-Id")
            if not session_id:
                raise RuntimeError("initialize 未返回 Mcp-Session-Id")
            self._sessions[server] = session_id
            self._cache.set(f"mcp:session:{server}", {"id": session_id}, 3600)
            notify = {"jsonrpc": "2.0", "method": "notifications/initialized"}
            with httpx.Client(trust_env=True, timeout=10) as client:
                client.post(self._url(server), json=notify, headers=self._headers(server))

    def _reset_session(self, server: str) -> None:
        self._sessions.pop(server, None)
        self._cache.delete(f"mcp:session:{server}")

    def _rpc_call(self, server: str, tool_name: str, arguments: dict[str, Any]) -> Any:
        for attempt in range(self.settings.ifind_mcp_max_retries + 1):
            self._ensure_session(server)
            payload = {
                "jsonrpc": "2.0",
                "id": self._next_id(server),
                "method": "tools/call",
                "params": {"name": tool_name, "arguments": arguments},
            }
            try:
                response, data = self._post(server, payload)
            except (httpx.ConnectError, httpx.ProxyError) as exc:
                raise RuntimeError(f"NETWORK_BLOCKED:{exc}") from exc
            except httpx.TimeoutException as exc:
                if attempt < self.settings.ifind_mcp_max_retries:
                    continue
                raise RuntimeError("TIMEOUT") from exc
            if response.status_code in {401, 404} and attempt == 0:
                self._reset_session(server)
                continue
            if response.status_code == 429:
                raise RuntimeError("RATE_LIMITED")
            if response.status_code in {401, 403}:
                raise RuntimeError("AUTH_FAILED")
            if response.status_code >= 500:
                if attempt < self.settings.ifind_mcp_max_retries:
                    continue
                raise RuntimeError("UPSTREAM_ERROR")
            if response.status_code >= 400:
                raise RuntimeError(f"UPSTREAM_ERROR:HTTP {response.status_code}")
            if isinstance(data, dict) and "error" in data:
                raise RuntimeError(f"UPSTREAM_ERROR:{data['error']}")
            return data
        raise RuntimeError("UPSTREAM_ERROR")

    def _call_upstream(self, spec: ToolSpec, args: dict[str, Any]) -> tuple[str, dict[str, Any], list[dict[str, Any]]]:
        query = build_query(spec.name, args)
        calls: list[dict[str, Any]] = []
        if spec.name == "ifind.stock_quote":
            upstream_args = quote_upstream_args(args)
        elif spec.name in {"ifind.news_search", "ifind.notice_search"}:
            start = str(args.get("time_start") or "")
            end = str(args.get("time_end") or "")
            if not start or not end:
                days = int(args.get("days") or 30)
                end_date = date.today()
                start_date = date.fromordinal(end_date.toordinal() - min(days, 365))
                start, end = start_date.isoformat(), end_date.isoformat()
            upstream_args = {
                "query": query,
                "time_start": start,
                "time_end": end,
                "size": max(1, min(int(args.get("size") or 5), 20)),
            }
        elif spec.name == "ifind.macro":
            search_raw = self._rpc_call("edb", "search_edb", {"query": query})
            calls.append({"tool": "search_edb", "raw": search_raw})
            indicator = _extract_edb_indicator(search_raw, query) or query
            upstream_args = {
                "query": f"{indicator} {args.get('period', '')}".strip(),
            }
            raw = self._rpc_call("edb", "get_edb_data", upstream_args)
            calls.append({"tool": "get_edb_data", "raw": raw})
            return query, raw, calls
        elif spec.name == "ifind.index":
            upstream = "sector_data" if args.get("kind") == "sector" else "index_data"
            spec = ToolSpec(
                name=spec.name,
                server=spec.server,
                upstream=upstream,
                description=spec.description,
                schema=spec.schema,
                cache_ttl_seconds=spec.cache_ttl_seconds,
            )
            upstream_args = {"query": query}
        else:
            upstream_args = {"query": query}
        raw = self._rpc_call(spec.server, spec.upstream, upstream_args)
        calls.append({"tool": spec.upstream, "raw": raw})
        return query, raw, calls

    def call(
        self,
        tool_name: str,
        args: dict[str, Any],
        tenant_id: uuid.UUID,
        user_id: uuid.UUID | None = None,
    ) -> ToolOutcome:
        started = time.monotonic()
        try:
            spec = get_tool(tool_name)
        except KeyError:
            return ToolOutcome("bad_request", None, "BAD_REQUEST:UNKNOWN_TOOL", 0, False, "", [])
        args_hash = hashlib.sha256(
            json.dumps(args, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()[:16]
        cache_scope = f"{tenant_id}:{user_id or 'shared'}"
        cache_key = f"ifind:v4:{tool_name}:{args_hash}:{cache_scope}"
        cached = self._cache.get(cache_key)
        if cached is not None and (
            not self.settings.ifind_raw_passthrough or cached.get("raw_text")
        ):
            latency = int((time.monotonic() - started) * 1000)
            evidence = dict(cached)
            evidence["cached"] = True
            return ToolOutcome(
                "ok",
                evidence,
                None,
                latency,
                True,
                str(evidence.get("query_text") or ""),
                [],
                str(evidence.get("raw_text") or "") or None,
            )
        query = ""
        try:
            validate_args(tool_name, args)
            query = build_query(tool_name, args)
            if self._circuit_open(tool_name):
                latency = int((time.monotonic() - started) * 1000)
                return ToolOutcome("schema_changed", None, "SCHEMA_CHANGED:CIRCUIT_OPEN", latency, False, query, [])
            query, raw, calls = self._call_upstream(spec, args)
            if self.settings.ifind_raw_passthrough:
                raw_text = _result_payload_text(raw)
                if not raw_text.strip():
                    raise RuntimeError("NO_DATA:EMPTY_RAW")
                now = datetime.now(timezone.utc)
                evidence = {
                    "tool": tool_name,
                    "server": spec.server,
                    "upstream_tool": spec.upstream,
                    "args": args,
                    "args_hash": args_hash,
                    "query_text": query,
                    "period_end": None,
                    "data_as_of": now.isoformat(),
                    "as_of_label": "iFinD 原始返回",
                    "fetched_at": now.isoformat(),
                    "sections": [],
                    "facts": [],
                    "raw_text": raw_text,
                    "raw_excerpt": raw_text[:2000],
                    "truncated": False,
                    "parse_quality": 1.0,
                    "parser_version": "raw-passthrough",
                    "tool_schema_hash": spec.schema_hash,
                    "cached": False,
                }
                self._cache.set(cache_key, evidence, spec.cache_ttl_seconds)
                latency = int((time.monotonic() - started) * 1000)
                return ToolOutcome(
                    "ok",
                    evidence,
                    None,
                    latency,
                    False,
                    query,
                    calls,
                    raw_text,
                )
            parsed = parse_ifind_response(raw, entity_hint=query, evidence_seq=1)
            if tool_name == "ifind.stock_financials" and args.get("compare") in {True, "同比", "yoy"}:
                _append_derived_yoy(parsed, args)
                target_period = normalize_date(args.get("period"))
                if target_period:
                    parsed["period_end"] = target_period
            if tool_name == "ifind.stock_financials":
                basis = {
                    (3, 31): "Q1累计",
                    (6, 30): "上半年累计",
                    (9, 30): "前三季度累计",
                    (12, 31): "全年累计",
                }
                for fact in parsed.get("facts") or []:
                    period = normalize_date(fact.get("period"))
                    label = financial_period_label(period)
                    if not label:
                        continue
                    try:
                        key = (date.fromisoformat(period).month, date.fromisoformat(period).day)
                    except (TypeError, ValueError):
                        key = None
                    suffix = basis.get(key)
                    fact["period_label"] = f"{label}（{suffix}）" if suffix else label
                target_period = normalize_date(args.get("period"))
                if target_period and not any(
                    normalize_date(fact.get("period")) == target_period
                    for fact in parsed.get("facts") or []
                ):
                    raise RuntimeError("NO_DATA:PERIOD_MISMATCH")
            inner_code = parsed.get("inner_code")
            if inner_code is not None and str(inner_code) not in {"1", "0", "200"}:
                raise RuntimeError(f"NO_DATA:{parsed.get('inner_message') or inner_code}")
            if parsed["parse_quality"] <= 0:
                dump_path = self._dump_schema_changed(
                    tool_name,
                    raw,
                    {"args": args, "query": query, "upstream_calls": [item.get("tool") for item in calls]},
                )
                self._open_circuit(tool_name, 600)
                raise RuntimeError(f"SCHEMA_CHANGED:{dump_path or 'NO_TABLE'}")
            if not parsed["facts"]:
                raise RuntimeError("NO_DATA:EMPTY_FACTS")
            now = datetime.now(timezone.utc)
            data_as_of = now if tool_name == "ifind.stock_quote" else None
            period_end = normalize_date(parsed.get("period_end"))
            if period_end:
                data_as_of = datetime.fromisoformat(period_end + "T15:00:00+08:00")
            as_of_label = f"报告期 {period_end}" if period_end else ("实时快照" if tool_name == "ifind.stock_quote" else "最新可得")
            evidence = {
                "tool": tool_name,
                "server": spec.server,
                "upstream_tool": spec.upstream,
                "args": args,
                "args_hash": args_hash,
                "query_text": query,
                "period_end": parsed.get("period_end"),
                "data_as_of": data_as_of.isoformat() if data_as_of else None,
                "as_of_label": as_of_label,
                "fetched_at": now.isoformat(),
                "sections": parsed["sections"],
                "facts": parsed["facts"],
                "raw_excerpt": parsed["raw_excerpt"],
                "truncated": parsed["truncated"],
                "parse_quality": parsed["parse_quality"],
                "parser_version": parsed["parser_version"],
                "tool_schema_hash": spec.schema_hash,
                "cached": False,
            }
            self._cache.set(cache_key, evidence, spec.cache_ttl_seconds)
            latency = int((time.monotonic() - started) * 1000)
            return ToolOutcome("ok", evidence, None, latency, False, query, calls)
        except ValueError as exc:
            latency = int((time.monotonic() - started) * 1000)
            return ToolOutcome("bad_request", None, f"BAD_REQUEST:{exc}", latency, False, query, [])
        except RuntimeError as exc:
            message = str(exc)
            code = message.split(":", 1)[0]
            if code not in {
                "TIMEOUT",
                "RATE_LIMITED",
                "AUTH_FAILED",
                "NO_DATA",
                "SCHEMA_CHANGED",
                "NETWORK_BLOCKED",
                "UPSTREAM_ERROR",
            }:
                code = "UPSTREAM_ERROR"
            latency = int((time.monotonic() - started) * 1000)
            return ToolOutcome(code.lower(), None, code, latency, False, query, [])
        except Exception as exc:
            latency = int((time.monotonic() - started) * 1000)
            return ToolOutcome("upstream_error", None, f"UPSTREAM_ERROR:{type(exc).__name__}", latency, False, query, [])

_gateway: IfindGateway | None = None


def get_gateway() -> IfindGateway:
    global _gateway
    if _gateway is None:
        _gateway = IfindGateway()
    return _gateway
