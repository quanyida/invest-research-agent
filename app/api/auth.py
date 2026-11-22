from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.api.deps import check_origin, require_principal
from app.config import get_settings
from app.db import session_scope
from app.security import (
    COOKIE_NAME,
    Principal,
    create_session,
    hash_password,
    resolve_session,
    revoke_session,
    verify_password,
)

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


class RegisterBody(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=8, max_length=128)
    tenant_name: str | None = Field(default=None, max_length=80)


class LoginBody(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=128)


def _normalize_email(email: str) -> str:
    value = email.strip().lower()
    if "@" not in value or value.startswith("@") or value.endswith("@"):
        raise HTTPException(status_code=400, detail={"code": "BAD_REQUEST", "message": "邮箱格式不正确"})
    return value


def _set_cookie(response: Response, token: str) -> None:
    settings = get_settings()
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=settings.session_ttl_hours * 3600,
        httponly=True,
        samesite="lax",
        secure=not settings.is_dev,
        path="/",
    )


def _principal_payload(principal: Principal) -> dict:
    return {
        "user": {"id": str(principal.user_id), "email": principal.email, "role": principal.role},
        "tenant": {"id": str(principal.tenant_id), "name": principal.tenant_name},
    }


@router.post("/register")
def register(body: RegisterBody, request: Request, response: Response) -> dict:
    check_origin(request)
    if not get_settings().is_dev:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "未开放注册"})
    email = _normalize_email(body.email)
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    tenant_name = (body.tenant_name or f"{email.split('@')[0]} 的工作区").strip()[:80]
    principal = Principal(user_id, tenant_id, email, "owner", tenant_name)
    try:
        with session_scope() as db:
            db.execute(
                text(
                    "select auth_register(:tenant_id, :user_id, :email, :password_hash, :tenant_name)"
                ),
                {
                    "tenant_id": str(tenant_id),
                    "user_id": str(user_id),
                    "email": email,
                    "password_hash": hash_password(body.password),
                    "tenant_name": tenant_name,
                },
            )
            token = create_session(db, principal)
    except IntegrityError:
        raise HTTPException(status_code=409, detail={"code": "EMAIL_EXISTS", "message": "邮箱已注册"})
    _set_cookie(response, token)
    return _principal_payload(principal)


@router.post("/login")
def login(body: LoginBody, request: Request, response: Response) -> dict:
    check_origin(request)
    email = _normalize_email(body.email)
    with session_scope() as db:
        row = db.execute(
            text("select * from auth_login_lookup(:email)"), {"email": email}
        ).mappings().first()
        if not row or not verify_password(body.password, row["password_hash"]):
            raise HTTPException(status_code=401, detail={"code": "UNAUTHORIZED", "message": "邮箱或密码错误"})
        if row["tenant_status"] != "active":
            raise HTTPException(status_code=401, detail={"code": "UNAUTHORIZED", "message": "账号已停用"})
        principal = Principal(
            user_id=row["user_id"],
            tenant_id=row["tenant_id"],
            email=row["email"],
            role=row["role"],
            tenant_name=row["tenant_name"],
        )
        token = create_session(db, principal)
    _set_cookie(response, token)
    return _principal_payload(principal)


@router.post("/logout")
def logout(request: Request, response: Response) -> dict:
    check_origin(request)
    token = request.cookies.get(COOKIE_NAME)
    if token:
        with session_scope() as db:
            revoke_session(db, token)
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"ok": True}


@router.get("/me")
def me(request: Request) -> dict:
    principal = require_principal(request)
    return _principal_payload(principal)