# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import uuid
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

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
    assert payload["stream_url"] == f"/api/v1/runs/{RUN}/events"
    assert uuid.UUID(payload["message_id"])
    assert uuid.UUID(payload["conversation_id"])
    assistant = [item for item in db.added if getattr(item, "role", None) == "assistant"]
    assert len(assistant) == 1
    assert assistant[0].status == "queued"
    assert assistant[0].meta["run_type"] == "research"

def test_chat_enqueues_as_chat_type(monkeypatch):
    """普通聊天也投队列，但 run_type=chat：崩溃后不重试。"""
    db = _patch_request(monkeypatch)
    seen: dict[str, object] = {}

    def fake_enqueue(*_a, **kwargs):
        seen.update(kwargs)
        return RUN

    monkeypatch.setattr(chat.run_queue, "enqueue", fake_enqueue)

    response = chat.chat(chat.ChatBody(message="问题二"), _Request())

    assert isinstance(response, StreamingResponse)
    assert seen.get("run_type") == "chat"
    assistant = [
        item for item in db.added if getattr(item, "role", None) == "assistant"
    ]
    assert len(assistant) == 1
    # 投队列后状态是 queued，由 worker 抢占改成 running
    assert assistant[0].status == "queued"
    assert assistant[0].meta["run_type"] == "chat"
    assert assistant[0].meta["run_id"] == str(RUN)

def test_only_one_replay_endpoint_remains():
    """回放入口只保留 /runs/{run_id}/events。

    两个兼容别名（/research/{run_id}/stream、/messages/{message_id}/events）
    已删除：它们和统一入口做同一件事，只是寻址方式不同，留着等于长期
    维护两套鉴权分支。
    """
    paths = {route.path for route in chat.router.routes}
    assert "/api/v1/runs/{run_id}/events" in paths
    assert "/api/v1/research/{run_id}/stream" not in paths
    assert "/api/v1/messages/{message_id}/events" not in paths


def test_cursor_takes_max_of_query_and_header():
    assert chat._resolve_cursor(3, _Request(headers={"last-event-id": "7"})) == 7
    assert chat._resolve_cursor(9, _Request(headers={"last-event-id": "7"})) == 9
    assert chat._resolve_cursor(0, _Request()) == 0

def test_run_events_rejects_foreign_run(monkeypatch):
    """别人的 run 一律 404，不泄露存在性。"""
    _patch_request(monkeypatch)
    monkeypatch.setattr(chat.run_queue, "load_run", lambda *_a, **_k: None)

    with pytest.raises(HTTPException) as raised:
        chat.run_events(RUN, _Request(), after_seq=0)

    assert raised.value.status_code == 404

def test_chat_run_first_replay_uses_last_snapshot(monkeypatch):
    """chat 首帧恢复只吐最后一条 snapshot，不逐条重绘。

    只验证分流决策：chat + after_seq=0 时查一次 last_snapshot，
    并把游标退到快照前一条；不跑完整流（那需要真实终态）。
    """
    _patch_request(monkeypatch)
    calls: list[tuple] = []

    def fake_snapshot(tenant_id, user_id, run_id):
        calls.append((tenant_id, user_id, run_id))
        return {"id": 42, "event": "snapshot", "data": {"text": "已生成的内容"}}

    monkeypatch.setattr(chat.run_queue, "last_snapshot", fake_snapshot)
    monkeypatch.setattr(chat, "_tail_run", lambda *a, **k: _empty_stream())

    run = {
        "id": RUN,
        "message_id": MESSAGE,
        "conversation_id": UUID("00000000-0000-0000-0000-0000000000cc"),
        "run_type": "chat",
        "status": "running",
        "attempt": 1,
    }

    seen: dict[str, object] = {}

    def capture_tail(principal, run_id, run_type, cursor):
        seen.update(run_type=run_type, cursor=cursor)
        return _empty_stream()

    monkeypatch.setattr(chat, "_tail_run", capture_tail)

    async def drain():
        chunks = []
        async for chunk in chat._replay_run(_principal(), run, 0):
            chunks.append(chunk)
        return chunks

    asyncio.run(drain())

    assert calls == [(TENANT, USER, RUN)], "chat 的 after_seq=0 应该查一次最后快照"
    # 游标退到快照前一条，让重放把它吐出来
    assert seen == {"run_type": "chat", "cursor": 41}


def _empty_stream():
    async def _gen():
        if False:  # pragma: no cover
            yield ""

    return _gen()


def test_research_run_does_not_snapshot(monkeypatch):
    """research 逐条重放，不查快照。"""
    _patch_request(monkeypatch)
    calls: list[int] = []
    monkeypatch.setattr(
        chat.run_queue, "last_snapshot", lambda *a, **k: calls.append(1) or None
    )
    monkeypatch.setattr(chat, "_tail_run", lambda *a, **k: _empty_stream())

    run = {
        "id": RUN,
        "message_id": MESSAGE,
        "conversation_id": UUID("00000000-0000-0000-0000-0000000000cc"),
        "run_type": "research",
        "status": "running",
        "attempt": 1,
    }

    async def drain():
        async for _ in chat._replay_run(_principal(), run, 0):
            pass

    asyncio.run(drain())
    assert calls == [], "research 不该走快照路径"

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