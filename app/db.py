from __future__ import annotations

import uuid
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings


_settings = get_settings()
engine = create_engine(
    _settings.database_url,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=5,
    future=True,
)
SessionLocal = sessionmaker(bind=engine, class_=Session, expire_on_commit=False, autoflush=False)


@contextmanager
def session_scope() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@contextmanager
def tenant_scope(tenant_id: uuid.UUID, user_id: uuid.UUID) -> Iterator[Session]:
    db = SessionLocal()
    try:
        db.execute(
            text(
                "select set_config('app.tenant_id', :tenant_id, true), "
                "set_config('app.user_id', :user_id, true)"
            ),
            {"tenant_id": str(tenant_id), "user_id": str(user_id)},
        )
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@contextmanager
def session_token_scope(token_hash: str) -> Iterator[Session]:
    db = SessionLocal()
    try:
        db.execute(
            text("select set_config('app.session_hash', :token_hash, true)"),
            {"token_hash": token_hash},
        )
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()