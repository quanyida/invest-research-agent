from __future__ import annotations

from fastapi import HTTPException, Request

from app.config import get_settings
from app.db import session_scope
from app.security import COOKIE_NAME, Principal, resolve_session, touch_session


def require_principal(request: Request) -> Principal:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=401, detail={"code": "UNAUTHORIZED", "message": "请先登录"})
    with session_scope() as db:
        principal = resolve_session(db, token)
        if principal is None:
            raise HTTPException(status_code=401, detail={"code": "UNAUTHORIZED", "message": "登录已过期"})
        touch_session(db, token)
    return principal


def check_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if origin and origin.rstrip("/") != get_settings().app_base_url:
        raise HTTPException(status_code=403, detail={"code": "FORBIDDEN", "message": "跨站请求被拒绝"})