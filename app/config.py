from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def _sanitize_no_proxy() -> None:
    """Drop bracketed IPv6 entries that crash httpx 0.27.2 with InvalidURL.

    httpx parses ``NO_PROXY`` by splitting on commas; an entry like ``[::1]``
    is read as a host with the port ``:1]`` and raises ``InvalidURL`` for every
    outbound request. The bracketed form adds nothing over the bare ``::1``
    entry, so strip only the redundant bracket copies.
    """
    for name in ("NO_PROXY", "no_proxy"):
        raw = os.getenv(name)
        if not raw:
            continue
        parts = [part.strip() for part in raw.split(",")]
        kept = [part for part in parts if part and not (part.startswith("[") and part.endswith("]"))]
        cleaned = ",".join(kept)
        if cleaned != raw.strip():
            os.environ[name] = cleaned


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}

def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    app_env: str
    app_secret: str
    app_base_url: str
    log_level: str
    database_url: str
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    llm_temperature: float
    llm_max_tokens: int
    llm_timeout_seconds: float
    llm_context_size: int
    llm_fallback_base_url: str
    llm_fallback_api_key: str
    llm_fallback_model: str
    ifind_mcp_base: str
    ifind_mcp_url: str
    ifind_mcp_token: str
    ifind_mcp_timeout_seconds: float
    ifind_mcp_max_retries: int
    redis_url: str
    redis_prefix: str
    http_proxy: str
    https_proxy: str
    max_tool_calls: int
    max_steps: int
    task_timeout_seconds: float
    model_max_attempts: int
    session_ttl_hours: int
    max_tokens_per_task: int
    max_upload_mb: int
    agent_engine: str
    answer_stream_mode: str
    agent_state_save: bool
    agent_state_max_kb: int
    history_turns: int
    ifind_raw_passthrough: bool
    tool_result_limit: int
    run_worker_enabled: bool
    run_queue_poll_seconds: float
    run_lease_seconds: float
    run_max_attempts: int
    event_poll_seconds: float
    event_tail_timeout_seconds: float
    web_search_enabled: bool
    web_search_provider: str
    web_search_base_url: str
    web_search_api_key: str
    web_search_timeout_seconds: float
    web_search_max_results: int
    web_search_cache_ttl_seconds: int

    @property
    def is_dev(self) -> bool:
        return self.app_env.lower() in {"dev", "local", "test"}

    @property
    def web_search_ready(self) -> bool:
        return self.web_search_enabled and bool(self.web_search_api_key)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    _load_dotenv(ROOT / ".env")
    _sanitize_no_proxy()
    return Settings(
        app_env=os.getenv("APP_ENV", "dev"),
        app_secret=os.getenv("APP_SECRET", "dev-only-change-me"),
        app_base_url=os.getenv("APP_BASE_URL", "http://127.0.0.1:8000").rstrip("/"),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        database_url=os.getenv(
            "DATABASE_URL",
            "postgresql+psycopg://invest_app:ira_dev_9f3c@127.0.0.1:55432/invest",
        ),
        llm_base_url=os.getenv("LLM_BASE_URL", "http://localhost:20128/v1").rstrip("/"),
        llm_api_key=os.getenv("LLM_API_KEY", ""),
        llm_model=os.getenv("LLM_MODEL", "scnet/SCNet-Max"),
        llm_temperature=_float("LLM_TEMPERATURE", 0.2),
        llm_max_tokens=_int("LLM_MAX_TOKENS", 2048),
        llm_timeout_seconds=_float("LLM_TIMEOUT_SECONDS", 60.0),
        llm_context_size=max(4096, _int("LLM_CONTEXT_SIZE", 1000000)),
        llm_fallback_base_url=os.getenv("LLM_FALLBACK_BASE_URL", "").rstrip("/"),
        llm_fallback_api_key=os.getenv("LLM_FALLBACK_API_KEY", ""),
        llm_fallback_model=os.getenv("LLM_FALLBACK_MODEL", ""),
        ifind_mcp_base=os.getenv(
            "IFIND_MCP_BASE", "https://api-mcp.51ifind.com:8643/ds-mcp-servers"
        ).rstrip("/"),
        ifind_mcp_url=os.getenv("IFIND_MCP_URL", "").rstrip("/"),
        ifind_mcp_token=os.getenv("IFIND_MCP_TOKEN", ""),
        ifind_mcp_timeout_seconds=_float("IFIND_MCP_TIMEOUT_SECONDS", 12.0),
        ifind_mcp_max_retries=_int("IFIND_MCP_MAX_RETRIES", 1),
        redis_url=os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0"),
        redis_prefix=os.getenv("REDIS_PREFIX", "ira"),
        http_proxy=os.getenv("HTTP_PROXY", ""),
        https_proxy=os.getenv("HTTPS_PROXY", ""),
        # Deprecated: single-turn tool quota removed; kept for compatibility with old scripts.
        max_tool_calls=_int("MAX_TOOL_CALLS", 4),
        max_steps=_int("MAX_STEPS", 6),
        task_timeout_seconds=_float("TASK_TIMEOUT_SECONDS", 180.0),
        model_max_attempts=_int("MODEL_MAX_ATTEMPTS", 4),
        session_ttl_hours=_int("SESSION_TTL_HOURS", 24),
        max_tokens_per_task=_int("MAX_TOKENS_PER_TASK", 12000),
        max_upload_mb=_int("MAX_UPLOAD_MB", 50),
        agent_engine=os.getenv("AGENT_ENGINE", "agentscope").strip().lower(),
        answer_stream_mode=os.getenv("ANSWER_STREAM_MODE", "verified").strip().lower(),
        agent_state_save=_bool("AGENT_STATE_SAVE", True),
        agent_state_max_kb=_int("AGENT_STATE_MAX_KB", 512),
        history_turns=max(1, _int("HISTORY_TURNS", 3)),
        ifind_raw_passthrough=_bool("IFIND_RAW_PASSTHROUGH", True),
        tool_result_limit=_int("TOOL_RESULT_LIMIT", 32000),
        run_worker_enabled=_bool("RUN_WORKER_ENABLED", True),
        run_queue_poll_seconds=_float("RUN_QUEUE_POLL_SECONDS", 0.2),
        run_lease_seconds=_float("RUN_LEASE_SECONDS", 900.0),
        run_max_attempts=_int("RUN_MAX_ATTEMPTS", 2),
        event_poll_seconds=_float("EVENT_POLL_SECONDS", 0.15),
        event_tail_timeout_seconds=_float("EVENT_TAIL_TIMEOUT_SECONDS", 600.0),
        web_search_enabled=_bool("WEB_SEARCH_ENABLED", True),
        web_search_provider=os.getenv("WEB_SEARCH_PROVIDER", "tavily").strip().lower(),
        web_search_base_url=os.getenv(
            "WEB_SEARCH_BASE_URL", "https://api.tavily.com/search"
        ).strip(),
        web_search_api_key=os.getenv("WEB_SEARCH_API_KEY", "").strip(),
        web_search_timeout_seconds=_float("WEB_SEARCH_TIMEOUT_SECONDS", 12.0),
        web_search_max_results=_int("WEB_SEARCH_MAX_RESULTS", 10),
        web_search_cache_ttl_seconds=_int("WEB_SEARCH_CACHE_TTL_SECONDS", 600),
    )
