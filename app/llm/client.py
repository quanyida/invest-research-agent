from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import get_settings


class LlmError(RuntimeError):
    def __init__(self, code: str, message: str, retryable: bool = False, attempts: int = 0) -> None:
        super().__init__(f"{code}:{message}")
        self.code = code
        self.message = message
        self.retryable = retryable
        self.attempts = attempts


@dataclass
class LlmResult:
    content: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    reasoning_tokens: int
    finish_reason: str | None
    latency_ms: int
    attempts: int = 1

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


def _decode_json_object(text: str) -> dict[str, Any]:
    body = text.strip()
    if body.startswith("data:"):
        lines = [line[5:].strip() for line in body.splitlines() if line.startswith("data:")]
        lines = [line for line in lines if line and line != "[DONE]"]
        body = lines[-1] if lines else ""
    decoder = json.JSONDecoder()
    try:
        value, _ = decoder.raw_decode(body)
    except json.JSONDecodeError as exc:
        raise LlmError("BAD_RESPONSE", "模型返回不是完整 JSON", retryable=True) from exc
    if not isinstance(value, dict):
        raise LlmError("BAD_RESPONSE", "模型返回不是对象", retryable=True)
    return value


def _extract_content(payload: dict[str, Any]) -> tuple[str, str | None, dict[str, int]]:
    choices = payload.get("choices") or []
    if not choices:
        raise LlmError("BAD_RESPONSE", "模型没有返回 choices", retryable=True)
    choice = choices[0] or {}
    message = choice.get("message") or {}
    content = message.get("content")
    if content is None:
        content = (choice.get("delta") or {}).get("content") or ""
    if isinstance(content, list):
        content = "".join(str(item.get("text", "")) for item in content if isinstance(item, dict))
    content = str(content or "").strip()
    usage = payload.get("usage") or {}
    detail = usage.get("completion_tokens_details") or {}
    tokens = {
        "prompt_tokens": int(usage.get("prompt_tokens") or 0),
        "completion_tokens": int(usage.get("completion_tokens") or 0),
        "reasoning_tokens": int(detail.get("reasoning_tokens") or 0),
    }
    return content, choice.get("finish_reason"), tokens


class LlmClient:
    def __init__(self) -> None:
        self.settings = get_settings()

    def _request_once(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        messages: list[dict[str, str]],
        temperature: float,
        max_tokens: int,
        timeout: float,
    ) -> tuple[LlmResult, str | None]:
        if timeout <= 0:
            raise LlmError("TIMEOUT", "剩余时间不足", retryable=False)
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        body = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        started = time.monotonic()
        try:
            with httpx.Client(trust_env=False, timeout=timeout) as client:
                response = client.post(f"{base_url.rstrip('/')}/chat/completions", headers=headers, json=body)
        except httpx.TimeoutException as exc:
            raise LlmError("TIMEOUT", "模型调用超时", retryable=True) from exc
        except httpx.NetworkError as exc:
            raise LlmError("UPSTREAM_UNAVAILABLE", f"模型网络错误：{type(exc).__name__}", retryable=True) from exc
        latency_ms = int((time.monotonic() - started) * 1000)
        if response.status_code == 429:
            raise LlmError("RATE_LIMITED", "模型限流", retryable=True)
        if response.status_code in {401, 403}:
            raise LlmError("AUTH_FAILED", "模型鉴权失败", retryable=False)
        if response.status_code >= 500:
            raise LlmError("UPSTREAM_UNAVAILABLE", f"模型 HTTP {response.status_code}", retryable=True)
        if response.status_code >= 400:
            raise LlmError("BAD_REQUEST", f"模型 HTTP {response.status_code}", retryable=False)
        payload = _decode_json_object(response.text)
        content, finish_reason, tokens = _extract_content(payload)
        result = LlmResult(
            content=content,
            model=str(payload.get("model") or model),
            prompt_tokens=tokens["prompt_tokens"],
            completion_tokens=tokens["completion_tokens"],
            reasoning_tokens=tokens["reasoning_tokens"],
            finish_reason=finish_reason,
            latency_ms=latency_ms,
        )
        return result, content or None

    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        purpose: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
        timeout: float | None = None,
        max_attempts: int | None = None,
    ) -> LlmResult:
        del purpose
        settings = self.settings
        limit = max(1, min(int(max_attempts or settings.model_max_attempts), settings.model_max_attempts))
        overall_deadline = time.monotonic() + float(timeout or settings.llm_timeout_seconds)
        primary = (settings.llm_base_url, settings.llm_api_key, settings.llm_model)
        fallback = None
        if settings.llm_fallback_base_url and settings.llm_fallback_model:
            fallback = (
                settings.llm_fallback_base_url,
                settings.llm_fallback_api_key,
                settings.llm_fallback_model,
            )
        target = primary
        fallback_used = False
        attempts = 0
        tokens = max(max_tokens or settings.llm_max_tokens, 512)
        last_error: LlmError | None = None
        while attempts < limit:
            remaining = overall_deadline - time.monotonic()
            if remaining <= 0.5:
                raise LlmError("TIMEOUT", "模型总预算已耗尽", retryable=False, attempts=attempts)
            attempts += 1
            try:
                result, content = self._request_once(
                    base_url=target[0],
                    api_key=target[1],
                    model=target[2],
                    messages=messages,
                    temperature=settings.llm_temperature if temperature is None else temperature,
                    max_tokens=tokens,
                    timeout=min(remaining, max(1.0, settings.llm_timeout_seconds)),
                )
                result.attempts = attempts
            except LlmError as exc:
                exc.attempts = attempts
                last_error = exc
                if not exc.retryable or attempts >= limit:
                    raise
                if fallback and not fallback_used:
                    target = fallback
                    fallback_used = True
                elif exc.code == "RATE_LIMITED":
                    wait = min(3.0, 1.0 * attempts)
                    if overall_deadline - time.monotonic() > wait + 0.8:
                        time.sleep(wait)
                continue
            if content:
                return result
            if attempts < limit:
                tokens = min(max(tokens * 2, 1024), 8192)
                continue
            raise LlmError("EMPTY_CONTENT", "模型没有返回正文", retryable=False, attempts=attempts)
        raise last_error or LlmError("EMPTY_CONTENT", "模型没有返回正文", retryable=False, attempts=attempts)


_client: LlmClient | None = None


def get_llm_client() -> LlmClient:
    global _client
    if _client is None:
        _client = LlmClient()
    return _client