# -*- coding: utf-8 -*-
from __future__ import annotations

import uuid
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from app.api import chat

ROOT = Path(__file__).resolve().parents[1]
TENANT = uuid.UUID("11111111-1111-1111-1111-111111111111")
USER = uuid.UUID("22222222-2222-2222-2222-222222222222")
RUN = uuid.UUID("33333333-3333-3333-3333-333333333333")
MESSAGE = uuid.UUID("44444444-4444-4444-4444-444444444444")

class _FakeDB:
    def __init__(self) -> None:
        self.added: list[object] = []

    def add(self, obj: object) -> None:
        self.added.append(obj)

    def flush(self) -> None:
        return None

    def scalar(self, _statement):
        return None

class _Request:
    def __init__(self, headers: dict[str, str] | None = None) -> None:
        self.headers = headers or {}
        self.cookies: dict[str, str] = {}

def _principal():
    return SimpleNamespace(tenant_id=TENANT, user_id=USER)

def _patch_request(monkeypatch) -> _FakeDB:
    db = _FakeDB()
    monkeypatch.setattr(chat, "check_origin", lambda _request: None)
    monkeypatch.setattr(chat, "require_principal", lambda _request: _principal())
    monkeypatch.setattr(chat, "_check_rate", lambda _user_id: None)

    @contextmanager
    def fake_scope(*_args, **_kwargs):
        yield db

    monkeypatch.setattr(chat, "tenant_scope", fake_scope)
    return db

def test_research_returns_run_id_and_stream_url(monkeypatch):
    db = _patch_request(monkeypatch)
    monkeypatch.setattr(chat.run_queue, "enqueue", lambda *_a, **_k: RUN)

    payload = chat.research(chat.ChatBody(message="深度研究一下瑞芯微"), _Request())

    assert payload["run_id"] == str(RUN)
    assert payload["stream_url"] == f"/api/v1/research/{RUN}/stream"
    assert uuid.UUID(payload["message_id"])
    assert uuid.UUID(payload["conversation_id"])
    assistant = [item for item in db.added if getattr(item, "role", None) == "assistant"]
    assert len(assistant) == 1
    assert assistant[0].status == "queued"

def test_chat_and_research_share_enqueue_path(monkeypatch):
    _patch_request(monkeypatch)
    calls: list[int] = []
    monkeypatch.setattr(chat.run_queue, "enqueue", lambda *_a, **_k: calls.append(1) or RUN)

    chat.research(chat.ChatBody(message="问题一"), _Request())
    chat.chat(chat.ChatBody(message="问题二"), _Request())

    assert len(calls) == 2

def test_research_stream_rejects_unknown_run(monkeypatch):
    _patch_request(monkeypatch)
    monkeypatch.setattr(chat.run_queue, "research_target", lambda *_a: None)

    with pytest.raises(HTTPException) as raised:
        chat.research_stream(RUN, _Request(), 0)

    assert raised.value.status_code == 404
    assert raised.value.detail["code"] == "NOT_FOUND"

def test_research_stream_replays_from_cursor(monkeypatch):
    _patch_request(monkeypatch)
    captured: dict[str, object] = {}
    monkeypatch.setattr(chat.run_queue, "research_target", lambda *_a: MESSAGE)

    def fake_events(tenant_id, user_id, message_id, after, **_kwargs):
        captured.update(
            tenant_id=tenant_id,
            user_id=user_id,
            message_id=message_id,
            after=after,
        )

        async def _gen():
            if False:  # pragma: no cover - 只为得到 async generator
                yield ""

        return _gen()

    monkeypatch.setattr(chat, "_stream_events", fake_events)

    response = chat.research_stream(RUN, _Request(headers={"last-event-id": "7"}), 3)

    assert isinstance(response, StreamingResponse)
    assert captured == {
        "tenant_id": TENANT,
        "user_id": USER,
        "message_id": MESSAGE,
        "after": 7,
    }

def test_entry_points_renamed():
    assert (ROOT / "app" / "api_main.py").is_file()
    assert (ROOT / "app" / "agent_service_main.py").is_file()
    assert not (ROOT / "app" / "main.py").exists()

def test_no_stale_entry_references():
    stale = "app" + ".main"
    paths = [path for path in (ROOT / "tests").rglob("*.py")]
    paths += [
        ROOT / "scripts" / "dev.ps1",
        ROOT / "scripts" / "bootstrap_db.py",
        ROOT / "README.md",
    ]
    offenders = [
        str(path.relative_to(ROOT))
        for path in paths
        if stale in path.read_text(encoding="utf-8")
    ]
    assert offenders == []

def test_api_layer_does_not_import_agentscope():
    paths = [ROOT / "app" / "api_main.py", *sorted((ROOT / "app" / "api").glob("*.py"))]
    offenders = [
        path.name
        for path in paths
        if "agentscope" in path.read_text(encoding="utf-8").lower()
    ]
    assert offenders == []

def test_api_layer_does_not_import_agent_service():
    paths = [ROOT / "app" / "api_main.py", *sorted((ROOT / "app" / "api").glob("*.py"))]
    offenders = [
        path.name
        for path in paths
        if "from app.agent_service" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []

def test_agent_service_exposes_execute():
    from app.agent_service_main import app

    paths = {route.path for route in app.routes}
    assert "/execute" in paths
    assert "/healthz" in paths
    assert "/readyz" in paths

def test_run_worker_default_is_false():
    source = (ROOT / "app" / "config.py").read_text(encoding="utf-8")
    assert '_bool("RUN_WORKER_ENABLED", False)' in source

def test_migration_008_is_idempotent():
    sql = (ROOT / "infra" / "sql" / "008_chat_research_split.sql").read_text(encoding="utf-8")
    ddl = [
        line.strip().lower()
        for line in sql.splitlines()
        if line.strip() and not line.strip().startswith("--")
    ]
    add_columns = [line for line in ddl if line.startswith("add column")]
    indexes = [line for line in ddl if line.startswith("create index")]
    assert add_columns and all("if not exists" in line for line in add_columns)
    assert indexes and all("if not exists" in line for line in indexes)
    assert "last_message_preview" in sql
    assert "message_count" in sql
    assert "messages_orphan_idx" in sql
    assert "session_events_expiry_idx" in sql