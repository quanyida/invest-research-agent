from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import get_settings


PBKDF2_ITERATIONS = 120_000
COOKIE_NAME = "ira_session"


@dataclass(frozen=True)
class Principal:
    user_id: uuid.UUID
    tenant_id: uuid.UUID
    email: str
    role: str
    tenant_name: str


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return "pbkdf2_sha256${}${}${}".format(
        PBKDF2_ITERATIONS,
        base64.urlsafe_b64encode(salt).decode("ascii"),
        base64.urlsafe_b64encode(digest).decode("ascii"),
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt_b64, digest_b64 = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        salt = base64.urlsafe_b64decode(salt_b64.encode("ascii"))
        expected = base64.urlsafe_b64decode(digest_b64.encode("ascii"))
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iterations))
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def hash_session_token(token: str) -> str:
    key = get_settings().app_secret.encode("utf-8")
    return hmac.new(key, token.encode("utf-8"), hashlib.sha256).hexdigest()


def create_session(db: Session, principal: Principal) -> str:
    token = new_session_token()
    expires_at = datetime.now(timezone.utc) + timedelta(hours=get_settings().session_ttl_hours)
    db.execute(
        text(
            "select auth_session_create(:session_id, :token_hash, :user_id, :tenant_id, :expires_at)"
        ),
        {
            "session_id": str(uuid.uuid4()),
            "token_hash": hash_session_token(token),
            "user_id": str(principal.user_id),
            "tenant_id": str(principal.tenant_id),
            "expires_at": expires_at,
        },
    )
    return token


def resolve_session(db: Session, token: str) -> Principal | None:
    row = db.execute(
        text("select * from auth_session_resolve(:token_hash)"),
        {"token_hash": hash_session_token(token)},
    ).mappings().first()
    if not row:
        return None
    return Principal(
        user_id=row["user_id"],
        tenant_id=row["tenant_id"],
        email=row["email"],
        role=row["role"],
        tenant_name=row["tenant_name"],
    )


def revoke_session(db: Session, token: str) -> None:
    db.execute(
        text("select auth_session_revoke(:token_hash)"),
        {"token_hash": hash_session_token(token)},
    )


def touch_session(db: Session, token: str) -> None:
    db.execute(
        text("select auth_session_touch(:token_hash)"),
        {"token_hash": hash_session_token(token)},
    )