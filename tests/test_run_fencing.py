# -*- coding: utf-8 -*-
import uuid
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from app.agent import persist, run_worker
from app.agent.persist import RunFenced
from app.agent.run_queue import QueueTicket, _ticket

TENANT = uuid.UUID("11111111-1111-1111-1111-111111111111")
USER = uuid.UUID("22222222-2222-2222-2222-222222222222")
CONV = uuid.UUID("33333333-3333-3333-3333-333333333333")
MESSAGE = uuid.UUID("44444444-4444-4444-4444-444444444444")
REQUEST = uuid.UUID("55555555-5555-5555-5555-555555555555")
RUN = uuid.UUID("66666666-6666-6666-6666-666666666666")

TICKET = QueueTicket(
    run_id=RUN,
    tenant_id=TENANT,
    user_id=USER,
    conversation_id=CONV,
    message_id=MESSAGE,
    request_id=REQUEST,
    attempt=2,
    worker_id="worker-b",
)

def test_queue_ticket_carries_worker_id():
    ticket = _ticket(
        {
            "id": RUN,
            "tenant_id": TENANT,
            "user_id": USER,
            "conversation_id": CONV,
            "message_id": MESSAGE,
            "request_id": REQUEST,
            "attempt": 2,
            "worker_id": "worker-b",
        }
    )
    assert ticket.worker_id == "worker-b"
    assert ticket.attempt == 2

class _Result:
    def __init__(self, rowcount: int) -> None:
        self.rowcount = rowcount

class _FakeSession:
    def __init__(self, run_rowcount: int) -> None:
        self.run_rowcount = run_rowcount
        self.executed: list[str] = []
        self.message = SimpleNamespace(content="old", status="running", meta={})

    def scalar(self, stmt):
        if "FROM messages" in str(stmt):
            return self.message
        return SimpleNamespace(title="synthetic", updated_at=None)

    def add(self, obj) -> None:
        raise AssertionError("fenced persist must not add rows")

    def execute(self, stmt, params=None):
        sql = str(stmt)
        self.executed.append(sql)
        if "update chat_runs" in sql:
            return _Result(self.run_rowcount)
        return _Result(1)

def test_persist_rolls_back_when_worker_is_fenced(monkeypatch):
    fake = _FakeSession(run_rowcount=0)
    state = {"committed": False}

    @contextmanager
    def fake_scope(tenant_id, user_id):
        yield fake
        state["committed"] = True

    monkeypatch.setattr(persist, "tenant_scope", fake_scope)

    with pytest.raises(RunFenced):
        persist.persist_turn(
            tenant_id=TENANT,
            user_id=USER,
            conversation_id=CONV,
            message_id=MESSAGE,
            request_id=REQUEST,
            question="q",
            answer="answer from stale worker",
            status="done",
            meta={},
            evidences=[],
            tool_audits=[],
            llm_audits=[],
            usage={},
            run_id=RUN,
            worker_id="worker-a",
            attempt=1,
        )

    assert state["committed"] is False
    assert any("worker_id = :worker_id and attempt = :attempt" in sql for sql in fake.executed)

@pytest.mark.asyncio
async def test_fenced_run_leaves_message_and_events_untouched(monkeypatch):
    events: list[str] = []
    marks: list[tuple] = []
    finishes: list[tuple] = []

    monkeypatch.setattr(run_worker, "load_chat_run", lambda ticket, settings: object())
    monkeypatch.setattr(
        run_worker, "append_event", lambda ticket, event, data: events.append(event)
    )
    monkeypatch.setattr(
        run_worker, "mark_assistant_failed", lambda *args: marks.append(args)
    )
    monkeypatch.setattr(
        run_worker, "finish", lambda *args, **kwargs: finishes.append((args, kwargs))
    )

    async def _fenced(run, attempt):
        raise RunFenced("run is owned by another worker")
        yield  # pragma: no cover

    worker = run_worker.RunWorker()
    worker._stream = _fenced

    await worker._execute(TICKET)

    assert events == []
    assert marks == []
    assert finishes == []