import uuid

import pytest

from app.agent import run_worker
from app.agent.run_queue import QueueTicket

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
    attempt=1,
)


async def _boom(run, attempt):
    raise RuntimeError("agent failed")
    yield  # pragma: no cover


def _patch_common(monkeypatch):
    monkeypatch.setattr(run_worker, "load_chat_run", lambda ticket, settings: object())
    events = []
    marks = []
    finishes = []
    monkeypatch.setattr(
        run_worker,
        "append_event",
        lambda ticket, event, data: events.append(event),
    )
    monkeypatch.setattr(
        run_worker,
        "mark_assistant_failed",
        lambda *args, **kwargs: marks.append(args),
    )
    monkeypatch.setattr(
        run_worker,
        "finish",
        lambda *args, **kwargs: finishes.append((args, kwargs)),
    )
    return events, marks, finishes


@pytest.mark.asyncio
async def test_late_error_after_commit_is_ignored(monkeypatch):
    events, marks, finishes = _patch_common(monkeypatch)
    status_calls = []

    def fake_run_status(tenant_id, run_id):
        status_calls.append((tenant_id, run_id))
        return "done"

    monkeypatch.setattr(run_worker, "run_status", fake_run_status)
    worker = run_worker.RunWorker()
    worker._stream = _boom

    await worker._execute(TICKET)

    assert status_calls == [(TENANT, RUN)]
    assert events == []
    assert marks == []
    assert finishes == []


@pytest.mark.asyncio
async def test_error_before_commit_marks_message_and_run_failed(monkeypatch):
    events, marks, finishes = _patch_common(monkeypatch)

    def fake_run_status(tenant_id, run_id):
        assert (tenant_id, run_id) == (TENANT, RUN)
        return "running"

    monkeypatch.setattr(run_worker, "run_status", fake_run_status)
    worker = run_worker.RunWorker()
    worker._stream = _boom

    await worker._execute(TICKET)

    assert events == ["error", "done"]
    assert len(marks) == 1
    assert marks[0][0] == TENANT
    assert marks[0][1] == USER
    assert marks[0][2] == MESSAGE
    assert len(finishes) == 1
    assert finishes[0][0] == (RUN, "failed")
    assert finishes[0][1]["error_code"] == "RuntimeError"