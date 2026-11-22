from __future__ import annotations

import hashlib
import html
import json
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import httpx

from app.config import get_settings
from app.tools.ifind_gateway import HybridCache, ToolOutcome
from app.tools.registry import get_tool

FRESHNESS_MAP = {
    "oneDay": "day",
    "oneWeek": "week",
    "oneMonth": "month",
    "oneYear": "year",
    "noLimit": "",
}

MAX_QUERY_CHARS = 200
MAX_RESULT_CHARS = 1200
MAX_TOTAL_CHARS = 8000


def _clean_text(value: Any, limit: int) -> str:
    text = str(value or "")
    text = re.sub(r"<script.*?</script>", " ", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def _site(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        return ""
    return host[4:] if host.startswith("www.") else host


class WebSearchClient:
    """Tavily-backed public web search with tenant-scoped caching."""

    def __init__(self) -> None:
        self.settings = get_settings()
        self._cache = HybridCache(
            f"{self.settings.redis_prefix}:web",
            self.settings.redis_url,
        )

    def call(
        self,
        tool_name: str,
        args: dict[str, Any],
        tenant_id: uuid.UUID,
        user_id: uuid.UUID | None = None,
    ) -> ToolOutcome:
        started = time.monotonic()
        query = str(args.get("query") or "").strip()

        try:
            if tool_name != "web.search":
                raise ValueError("UNKNOWN_TOOL")
            if len(query) < 2 or len(query) > MAX_QUERY_CHARS:
                raise ValueError("QUERY_LENGTH")
            count = int(args.get("count") or 6)
            max_results = max(1, min(int(self.settings.web_search_max_results), 10))
            if count < 1 or count > max_results:
                raise ValueError("COUNT_RANGE")
            freshness = str(args.get("freshness") or "oneMonth")
            if freshness not in FRESHNESS_MAP:
                raise ValueError("FRESHNESS")
        except (TypeError, ValueError) as exc:
            latency = int((time.monotonic() - started) * 1000)
            return ToolOutcome(
                "bad_request",
                None,
                f"BAD_REQUEST:{exc}",
                latency,
                False,
                query,
                [],
            )

        normalized = {"query": query, "count": count, "freshness": freshness}
        args_hash = hashlib.sha256(
            json.dumps(normalized, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()[:16]
        cache_key = (
            f"search:v1:{args_hash}:{tenant_id}:{user_id or 'shared'}"
        )
        cached = self._cache.get(cache_key)
        if cached is not None:
            latency = int((time.monotonic() - started) * 1000)
            evidence = dict(cached)
            evidence["cached"] = True
            return ToolOutcome(
                "ok",
                evidence,
                None,
                latency,
                True,
                str(evidence.get("query_text") or query),
                [],
                str(evidence.get("raw_text") or "") or None,
            )

        try:
            payload = self._request(query, count, freshness)
            results = self._normalize(payload)
            if not results:
                raise RuntimeError("NO_DATA:EMPTY_RESULTS")
            now = datetime.now(timezone.utc)
            raw_text, kept, truncated = self._render(query, results, now)
            evidence = {
                "tool": tool_name,
                "server": "web",
                "upstream_tool": f"{self.settings.web_search_provider}.search",
                "args": normalized,
                "args_hash": args_hash,
                "query_text": query,
                "period_end": None,
                "data_as_of": now.isoformat(),
                "as_of_label": "公开网页搜索",
                "fetched_at": now.isoformat(),
                "sections": [],
                "facts": [],
                "raw_text": raw_text,
                "raw_excerpt": raw_text[:2000],
                "truncated": truncated,
                "parse_quality": 1,
                "parser_version": "web-v1",
                "tool_schema_hash": get_tool(tool_name).schema_hash,
                "cached": False,
                "provider": self.settings.web_search_provider,
                "results_count": len(kept),
                "source_items": kept,
                "source_title": kept[0].get("title") if kept else None,
                "source_url": kept[0].get("url") if kept else None,
                "source_publisher": kept[0].get("site") if kept else None,
                "source_published_at": kept[0].get("published_at") if kept else None,
            }
            ttl = max(1, int(self.settings.web_search_cache_ttl_seconds))
            self._cache.set(cache_key, evidence, ttl)
            latency = int((time.monotonic() - started) * 1000)
            return ToolOutcome(
                "ok",
                evidence,
                None,
                latency,
                False,
                query,
                [{"tool": f"{self.settings.web_search_provider}.search", "raw": payload}],
                raw_text,
            )
        except ValueError as exc:
            latency = int((time.monotonic() - started) * 1000)
            return ToolOutcome(
                "bad_request",
                None,
                f"BAD_REQUEST:{exc}",
                latency,
                False,
                query,
                [],
            )
        except RuntimeError as exc:
            message = str(exc)
            code = message.split(":", 1)[0]
            if code not in {
                "TIMEOUT",
                "RATE_LIMITED",
                "AUTH_FAILED",
                "NO_DATA",
                "NETWORK_BLOCKED",
                "UPSTREAM_ERROR",
            }:
                code = "UPSTREAM_ERROR"
            latency = int((time.monotonic() - started) * 1000)
            return ToolOutcome(code.lower(), None, code, latency, False, query, [])
        except Exception as exc:
            latency = int((time.monotonic() - started) * 1000)
            return ToolOutcome(
                "upstream_error",
                None,
                f"UPSTREAM_ERROR:{type(exc).__name__}",
                latency,
                False,
                query,
                [],
            )

    def _request(self, query: str, count: int, freshness: str) -> dict[str, Any]:
        provider = self.settings.web_search_provider
        if provider != "tavily":
            raise RuntimeError(f"UPSTREAM_ERROR:UNSUPPORTED_PROVIDER:{provider}")

        body: dict[str, Any] = {
            "query": query,
            "search_depth": "basic",
            "max_results": count,
            "include_answer": False,
            "include_raw_content": False,
            "topic": "general",
        }
        time_range = FRESHNESS_MAP.get(freshness)
        if time_range:
            body["time_range"] = time_range

        headers = {
            "Authorization": f"Bearer {self.settings.web_search_api_key}",
            "Content-Type": "application/json",
            "User-Agent": "invest-agent-v1",
        }

        last_error = "UPSTREAM_ERROR"
        for attempt in range(2):
            try:
                with httpx.Client(
                    trust_env=True,
                    timeout=self.settings.web_search_timeout_seconds,
                ) as client:
                    response = client.post(
                        self.settings.web_search_base_url,
                        json=body,
                        headers=headers,
                    )
                if response.status_code == 429 and attempt == 0:
                    time.sleep(1.0)
                    continue
                if 500 <= response.status_code < 600 and attempt == 0:
                    time.sleep(1.0)
                    continue
                if response.status_code in {401, 403}:
                    raise RuntimeError("AUTH_FAILED")
                if response.status_code == 429:
                    raise RuntimeError("RATE_LIMITED")
                if response.status_code >= 500:
                    raise RuntimeError(f"UPSTREAM_ERROR:HTTP_{response.status_code}")
                if response.status_code >= 400:
                    raise RuntimeError(f"UPSTREAM_ERROR:HTTP_{response.status_code}")
                try:
                    data = response.json()
                except Exception:
                    raise RuntimeError("UPSTREAM_ERROR:BAD_JSON")
                if not isinstance(data, dict):
                    raise RuntimeError("UPSTREAM_ERROR:BAD_JSON")
                return data
            except httpx.TimeoutException:
                last_error = "TIMEOUT"
                if attempt == 0:
                    time.sleep(1.0)
                    continue
                raise RuntimeError("TIMEOUT")
            except httpx.RequestError:
                last_error = "NETWORK_BLOCKED"
                if attempt == 0:
                    time.sleep(1.0)
                    continue
                raise RuntimeError("NETWORK_BLOCKED")

        raise RuntimeError(last_error)

    @staticmethod
    def _normalize(payload: dict[str, Any]) -> list[dict[str, str]]:
        items = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            return []
        results: list[dict[str, str]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").strip()
            title = _clean_text(item.get("title"), 200)
            snippet = _clean_text(
                item.get("content") or item.get("snippet"),
                MAX_RESULT_CHARS,
            )
            if not title and not snippet and not url:
                continue
            results.append(
                {
                    "title": title,
                    "url": url,
                    "site": _site(url),
                    "published_at": str(
                        item.get("published_date")
                        or item.get("published_at")
                        or ""
                    ).strip(),
                    "snippet": snippet,
                }
            )
        return results

    @staticmethod
    def _render(
        query: str,
        results: list[dict[str, str]],
        now: datetime,
    ) -> tuple[str, list[dict[str, str]], bool]:
        kept: list[dict[str, str]] = []
        total = 0
        truncated = False
        for item in results:
            size = len(json.dumps(item, ensure_ascii=False))
            if total + size > MAX_TOTAL_CHARS:
                truncated = True
                break
            kept.append(item)
            total += size
        body: dict[str, Any] = {
            "provider": get_settings().web_search_provider,
            "query": query,
            "fetched_at": now.isoformat(),
            "results": kept,
        }
        if truncated:
            body["truncated"] = True
        return json.dumps(body, ensure_ascii=False, indent=2), kept, truncated


_web_search: WebSearchClient | None = None


def get_web_search() -> WebSearchClient:
    global _web_search
    if _web_search is None:
        _web_search = WebSearchClient()
    return _web_search