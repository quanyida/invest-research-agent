from __future__ import annotations

from fastapi import APIRouter, HTTPException
from sqlalchemy import text

from app.config import get_settings
from app.db import session_scope

router = APIRouter(tags=["health"])


@router.get("/healthz")
def healthz() -> dict:
    return {"ok": True, "service": "投研Agent"}


@router.get("/readyz")
def readyz() -> dict:
    settings = get_settings()
    checks: dict[str, str] = {}
    try:
        with session_scope() as db:
            db.execute(text("select 1"))
        checks["postgres"] = "ok"
    except Exception as exc:
        checks["postgres"] = f"error: {type(exc).__name__}"
        raise HTTPException(status_code=503, detail={"code": "NOT_READY", "message": "数据库不可用", "checks": checks})
    checks["redis"] = "configured" if settings.redis_url else "disabled"
    checks["ifind"] = "configured" if settings.ifind_mcp_token else "missing_token"
    checks["llm"] = "configured" if settings.llm_api_key else "missing_key"
    return {"ok": True, "checks": checks}