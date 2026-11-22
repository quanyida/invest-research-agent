import asyncio
import uuid
from types import SimpleNamespace

from agentscope.state import AgentState

from app.agent import agentscope_runtime as runtime
from app.agent.agent_state_store import (
    AgentStateStore,
    AgentStateTooLarge,
    detect_gap,
    record_saved_turn,
    reset_turn_marks,
)


class _TooLargeStore:
    def serialize(self, state):
        raise AgentStateTooLarge(1024 * 1024)


class _BrokenStore:
    def serialize(self, state):
        raise ValueError("cannot serialize")


class _OkStore:
    def serialize(self, state):
        return {"schema_version": 1, "state": {}}


def _runner(*, save=True):
    runner = runtime.AgentScopeRunner.__new__(runtime.AgentScopeRunner)
    runner.settings = SimpleNamespace(agent_state_save=save)
    return runner


def _run():
    return SimpleNamespace(
        tenant_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        message_id=uuid.uuid4(),
        request_id=uuid.uuid4(),
        question="q",
        run_id=None,
        worker_id=None,
        attempt=None,
    )


def _ctx(turn=3):
    return SimpleNamespace(
        turn=turn,
        budget=SimpleNamespace(elapsed_ms=lambda: 12),
    )


def _capture_persist(monkeypatch):
    captured = {}

    def fake_persist_turn(**kwargs):
        captured.update(kwargs)
        return True, False

    monkeypatch.setattr(runtime, "persist_turn", fake_persist_turn)
    return captured


def _persist(runner, *, agent, store, meta=None):
    return asyncio.run(
        runner._persist(
            _run(),
            _ctx(),
            answer="answer",
            status="done",
            meta={} if meta is None else meta,
            evidences=[],
            tool_audits=[],
            llm_audits=[],
            usage={},
            agent=agent,
            state_version=7,
            state_store=store,
        )
    )


def test_compression_failure_skips_state_without_empty_payload(monkeypatch):
    captured = _capture_persist(monkeypatch)

    async def _compress_fails():
        raise RuntimeError("compression failed")

    agent = SimpleNamespace(
        state=AgentState(session_id="s"),
        compress_context=_compress_fails,
    )

    saved, skipped, _ = _persist(_runner(), agent=agent, store=_TooLargeStore())

    assert saved is True
    assert skipped is True
    assert captured["agent_state"] is None
    assert captured["agent_state_version"] is None
    assert captured["meta"]["agent_state_skipped"] is True
    assert captured["meta"]["agent_state_skip_reason"] == "compression_error"


def test_serialization_failure_skips_state(monkeypatch):
    captured = _capture_persist(monkeypatch)

    async def _not_called():
        raise AssertionError("compression should not run")

    agent = SimpleNamespace(
        state=AgentState(session_id="s"),
        compress_context=_not_called,
    )

    _, skipped, _ = _persist(_runner(), agent=agent, store=_BrokenStore())

    assert skipped is True
    assert captured["agent_state"] is None
    assert captured["agent_state_version"] is None
    assert captured["meta"]["agent_state_skip_reason"] == "serialization_error"


def test_disabled_state_save_reports_reason(monkeypatch):
    captured = _capture_persist(monkeypatch)

    _, skipped, _ = _persist(_runner(save=False), agent=None, store=_OkStore())

    assert skipped is True
    assert captured["agent_state"] is None
    assert captured["meta"]["agent_state_skip_reason"] == "state_disabled"


def test_missing_agent_reports_interruption(monkeypatch):
    captured = _capture_persist(monkeypatch)

    _, skipped, _ = _persist(_runner(), agent=None, store=_OkStore())

    assert skipped is True
    assert captured["agent_state"] is None
    assert captured["meta"]["agent_state_skip_reason"] == "agent_unavailable"


def test_normal_save_has_no_skip_reason(monkeypatch):
    captured = _capture_persist(monkeypatch)
    agent = SimpleNamespace(state=AgentState(session_id="s"))

    _, skipped, _ = _persist(_runner(), agent=agent, store=_OkStore())

    assert skipped is False
    assert captured["agent_state"] == {"schema_version": 1, "state": {}}
    assert captured["agent_state_version"] == 7
    assert captured["meta"]["agent_state_skipped"] is False
    assert captured["meta"]["agent_state_skip_reason"] is None


def test_unsaved_turn_remains_missing_in_last_persisted_state():
    persisted = AgentState(session_id="s")
    reset_turn_marks(persisted, 1)
    record_saved_turn(persisted, 1)

    reloaded = AgentState.model_validate(persisted.model_dump(mode="json"))
    volatile = AgentState.model_validate(reloaded.model_dump(mode="json"))

    record_saved_turn(volatile, 2)
    assert detect_gap(volatile, 3) == []

    assert detect_gap(reloaded, 3) == [2]


def test_store_exposes_no_empty_payload_writer():
    assert not hasattr(AgentStateStore, "empty")