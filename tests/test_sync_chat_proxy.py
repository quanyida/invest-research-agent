# -*- coding: utf-8 -*-
from __future__ import annotations

import codecs
import uuid
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from app.agent import run_queue
from app.agent.persist import message_preview
from app.agent_service import execute
from app.api import chat

TENANT = uuid.UUID("11111111-1111-1111-1111-111111111111")
USER = uuid.UUID("22222222-2222-2222-2222-222222222222")
CONV = uuid.UUID("33333333-3333-3333-3333-333333333333")
MESSAGE = uuid.UUID("44444444-4444-4444-4444-444444444444")
REQUEST = uuid.UUID("55555555-5555-5555-5555-555555555555")

class _Result:
    def __init__(self, rowcount: int, rows: list[tuple] | None = None) -> None:
        self.rowcount = rowcount
        self._rows = rows or []

    def all(self):
        return self._rows

class _Session:
    def __init__(self, message=None, rowcount: int = 1) -> None:
        self.message = message
        self.rowcount = rowcount
        self.rows: list[tuple] = []
        self.executed: list[tuple[str, dict | None]] = []

    def scalar(self, _statement):
        return self.message

    def scalars(self, _statement):
        return SimpleNamespace(all=lambda: [])

    def execute(self, statement, params=None):
        self.executed.append((str(statement), params))
        rows = self.rows if "from chat_runs" in str(statement) else None
        return _Result(self.rowcount, rows)

def _body(timeout_seconds: float = 60.0) -> execute.ExecuteBody:
    return execute.ExecuteBody(
        tenant_id=TENANT,
        user_id=USER,
        conversation_id=CONV,
        message_id=MESSAGE,
        question="对比分析瑞芯微和恒玄科技",
        turn=2,
        timeout_seconds=timeout_seconds,
    )

def test_message_preview_strips_citation_markers():
    text = (
        "结论：2025Q3 毛利率 25.1% [E2-1]，同比 +1.2pct（E2-2）。\n"
        "补充【E3-4】与 [E5–6] 的数据。"
    )
    preview = message_preview(text)
    assert "毛利率" in preview
    assert "E2-1" not in preview
    assert "E2-2" not in preview
    assert "E3-4" not in preview
    assert "E5–6" not in preview
    assert "\n" not in preview

def test_message_preview_truncates_to_limit():
    assert len(message_preview("字" * 500)) == 80

def test_convergence_seconds_scales_with_timeout():
    assert execute._convergence_seconds(60) == 21.0
    assert execute._convergence_seconds(180) == 60.0
    assert execute._convergence_seconds(10) == 10.0

def test_execute_sse_frame_shape():
    frame = execute._sse("delta", {"text": "毛利率"})
    assert frame.startswith("event: delta\n")
    assert '"text":"毛利率"' in frame
    assert frame.endswith("\n\n")

def test_absorb_sse_handles_split_utf8_and_multiple_blocks():
    payload = (
        "event: delta\ndata: {\"text\":\"毛利\"}\n\n"
        "event: delta\ndata: {\"text\":\"率\"}\n\n"
        "event: done\ndata: {\"status\":\"done\"}\n\n"
    ).encode("utf-8")
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    state = {"buffer": "", "text": "", "done": False}
    for index in range(0, len(payload), 3):
        chat._absorb_sse(decoder.decode(payload[index : index + 3]), state)
    chat._absorb_sse(decoder.decode(b"", True), state)
    assert state["text"] == "毛利率"
    assert state["done"] is True

def test_absorb_sse_ignores_keepalive_and_bad_json():
    state = {"buffer": "", "text": "", "done": False}
    chat._absorb_sse(": keepalive\n\n", state)
    chat._absorb_sse("event: delta\ndata: not-json\n\n", state)
    assert state == {"buffer": "", "text": "", "done": False}

@pytest.mark.asyncio
async def test_execute_reports_missing_message(monkeypatch):
    monkeypatch.setattr(execute, "get_settings", lambda: SimpleNamespace())
    monkeypatch.setattr(execute, "_load_run", lambda body, settings: None)

    frames = [frame async for frame in execute._run(_body())]

    assert len(frames) == 2
    assert frames[0].startswith("event: error\n")
    assert '"code":"RUN_NOT_FOUND"' in frames[0]
    assert frames[1].startswith("event: done\n")
    assert '"status":"error"' in frames[1]

def test_load_run_returns_none_when_message_missing(monkeypatch):
    session = _Session(None)

    @contextmanager
    def fake_scope(*_args):
        yield session

    monkeypatch.setattr(execute, "tenant_scope", fake_scope)
    assert execute._load_run(_body(), SimpleNamespace(history_turns=3)) is None

def test_load_run_builds_chat_run_with_chat_budget(monkeypatch):
    assistant = SimpleNamespace(
        id=MESSAGE,
        conversation_id=CONV,
        role="assistant",
        status="running",
        request_id=REQUEST,
        updated_at=None,
    )
    session = _Session(assistant)

    @contextmanager
    def fake_scope(*_args):
        yield session

    monkeypatch.setattr(execute, "tenant_scope", fake_scope)

    run = execute._load_run(_body(timeout_seconds=60), SimpleNamespace(history_turns=3))

    assert run is not None
    assert run.timeout_seconds == 60
    assert run.convergence_seconds == 21.0
    assert run.turn == 2
    assert run.history == []
    assert run.run_id is None
    assert assistant.status == "running"

def test_load_run_rejects_finished_message(monkeypatch):
    assistant = SimpleNamespace(
        id=MESSAGE,
        conversation_id=CONV,
        role="assistant",
        status="done",
        request_id=REQUEST,
        updated_at=None,
    )
    session = _Session(assistant)

    @contextmanager
    def fake_scope(*_args):
        yield session

    monkeypatch.setattr(execute, "tenant_scope", fake_scope)
    assert execute._load_run(_body(), SimpleNamespace(history_turns=3)) is None

def test_finalize_interrupted_turn_only_touches_running(monkeypatch):
    session = _Session(rowcount=0)

    @contextmanager
    def fake_scope(*_args):
        yield session

    monkeypatch.setattr(chat, "tenant_scope", fake_scope)
    assert chat._finalize_interrupted_turn(TENANT, USER, CONV, MESSAGE, "部分", None) is False
    sql = session.executed[0][0]
    assert "status = 'running'" in sql
    assert len(session.executed) == 1

def test_finalize_interrupted_turn_updates_conversation(monkeypatch):
    session = _Session(rowcount=1)

    @contextmanager
    def fake_scope(*_args):
        yield session

    monkeypatch.setattr(chat, "tenant_scope", fake_scope)
    assert chat._finalize_interrupted_turn(TENANT, USER, CONV, MESSAGE, "部分回答", None) is True
    assert len(session.executed) == 2
    assert "last_message_preview" in session.executed[1][0]
    assert session.executed[1][1]["conversation_id"] == str(CONV)

def test_sweep_orphan_messages_guards_running_without_runs(monkeypatch):
    session = _Session(rowcount=2)
    session.rows = [(str(TENANT), str(USER))]

    @contextmanager
    def fake_scope():
        yield session

    monkeypatch.setattr(run_queue, "session_scope", fake_scope)
    assert run_queue.sweep_orphan_messages(3600) == 2
    updates = [item for item in session.executed if "update messages" in item[0]]
    assert len(updates) == 1
    sql, params = updates[0]
    assert "status = 'running'" in sql
    assert "not exists" in sql.lower()
    assert "updated_at < now()" in sql
    assert params["timeout"] == 3600.0
    scoped = [item for item in session.executed if "set_config('app.tenant_id'" in item[0]]
    assert scoped and scoped[0][1]["tenant_id"] == str(TENANT)

def test_chat_busy_returns_409(monkeypatch):
    class _BusySession(_Session):
        def __init__(self) -> None:
            super().__init__()
            self.answers = iter(
                [SimpleNamespace(id=CONV, title="标题", deleted_at=None), 0, 1]
            )

        def scalar(self, _statement):
            return next(self.answers)

    session = _BusySession()

    @contextmanager
    def fake_scope(*_args):
        yield session

    monkeypatch.setattr(chat, "tenant_scope", fake_scope)
    monkeypatch.setattr(chat, "_check_daily_quota", lambda *_a: None)
    principal = SimpleNamespace(tenant_id=TENANT, user_id=USER)
    body = chat.ChatBody(message="追问", conversation_id=CONV)

    with pytest.raises(Exception) as raised:
        chat._prepare_turn(
            session, principal, body, "追问", REQUEST, MESSAGE, enqueue=False
        )

    detail = getattr(raised.value, "detail", {})
    assert detail.get("code") == "CONVERSATION_BUSY"

def test_absorb_sse_delta_reset_clears_accumulated_text():
    state = {"buffer": "", "text": "", "done": False}
    chat._absorb_sse('event: delta\ndata: {"text":"我先查一下"}\n\n', state)
    assert state["text"] == "我先查一下"
    chat._absorb_sse('event: delta_reset\ndata: {"reason":"tool_call"}\n\n', state)
    assert state["text"] == ""
    assert state["done"] is False
