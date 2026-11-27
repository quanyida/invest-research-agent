# -*- coding: utf-8 -*-
"""session_events 批量持久化契约。

钉住四件事：
  1. chat 只写合并后的 delta，不再每 200ms 写一条全文 snapshot
  2. 字符阈值 / 时间阈值 / 流结束三条 flush 路径都生效，拼接结果与原始输出逐字一致
  3. 关键事件前先 flush，delta 与关键事件进同一个事务且顺序不变
  4. 重试 attempt 先 reset、批量写仍然带 fence、被 fence 时一条都不写
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.agent import run_worker
from app.agent.persist import RunFenced
from app.agent.run_queue import QueueTicket

TENANT = uuid.UUID("11111111-1111-1111-1111-111111111111")
USER = uuid.UUID("22222222-2222-2222-2222-222222222222")
CONV = uuid.UUID("33333333-3333-3333-3333-333333333333")
MESSAGE = uuid.UUID("44444444-4444-4444-4444-444444444444")
REQUEST = uuid.UUID("55555555-5555-5555-5555-555555555555")
RUN = uuid.UUID("66666666-6666-6666-6666-666666666666")


def _ticket(run_type: str = "chat", attempt: int = 1) -> QueueTicket:
    return QueueTicket(
        run_id=RUN,
        tenant_id=TENANT,
        user_id=USER,
        conversation_id=CONV,
        message_id=MESSAGE,
        request_id=REQUEST,
        attempt=attempt,
        worker_id="worker-a",
        run_type=run_type,
    )


class Clock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> float:
        self.now += seconds
        return self.now


class Recorder:
    """记录每一次落库；一次调用 = 一个事务。"""

    def __init__(self) -> None:
        self.batches: list[list[tuple[str, dict]]] = []

    @property
    def events(self) -> list[tuple[str, dict]]:
        return [item for batch in self.batches for item in batch]

    @property
    def names(self) -> list[str]:
        return [event for event, _data in self.events]

    def append_event(self, ticket, event, data) -> None:
        self.batches.append([(event, data)])

    def append_events(self, ticket, items) -> None:
        self.batches.append(list(items))


def _worker(monkeypatch, recorder: Recorder, stream, clock: Clock | None = None):
    monkeypatch.setattr(run_worker, "load_chat_run", lambda ticket, settings: object())
    monkeypatch.setattr(run_worker, "append_event", recorder.append_event)
    monkeypatch.setattr(run_worker, "append_events", recorder.append_events)
    monkeypatch.setattr(run_worker, "mark_events_expiring", lambda *a, **k: 0)
    monkeypatch.setattr(run_worker, "mark_assistant_failed", lambda *a, **k: None)
    monkeypatch.setattr(run_worker, "finish", lambda *a, **k: True)
    monkeypatch.setattr(run_worker, "run_status", lambda *a, **k: "running")
    if clock is not None:
        monkeypatch.setattr(run_worker, "time", SimpleNamespace(monotonic=clock.monotonic))
    worker = run_worker.RunWorker.__new__(run_worker.RunWorker)
    worker.settings = SimpleNamespace()
    worker._stream = stream
    return worker


def _delta_texts(recorder: Recorder) -> list[str]:
    return [data["text"] for event, data in recorder.events if event == "delta"]


@pytest.mark.asyncio
@pytest.mark.parametrize("run_type", ["chat", "research"])
async def test_deltas_are_merged_and_never_written_as_snapshot(monkeypatch, run_type):
    clock = Clock()
    recorder = Recorder()
    chunks = ["a" * 50, "b" * 50, "c" * 50, "d" * 50]

    async def stream(run, attempt):
        for chunk in chunks:
            yield ("delta", {"text": chunk})
        yield ("done", {"status": "ok", "run_finalized": True})

    worker = _worker(monkeypatch, recorder, stream, clock)
    await worker._execute_inner(_ticket(run_type))

    assert "snapshot" not in recorder.names, "chat 不再写全文快照"
    assert recorder.names == ["delta", "done"]
    assert _delta_texts(recorder) == ["".join(chunks)]
    assert recorder.events[0][1]["text"] == "".join(chunks)


@pytest.mark.asyncio
@pytest.mark.parametrize("run_type", ["chat", "research"])
async def test_char_threshold_triggers_write_before_stream_end(monkeypatch, run_type):
    clock = Clock()
    recorder = Recorder()
    chunks = ["x" * 100, "y" * 100, "z" * 100]

    async def stream(run, attempt):
        for chunk in chunks:
            yield ("delta", {"text": chunk})
        yield ("done", {"status": "ok", "run_finalized": True})

    worker = _worker(monkeypatch, recorder, stream, clock)
    await worker._execute_inner(_ticket(run_type))

    # 第 2 个 delta 后达到 200 字符立刻落库；第 3 个随 done 一起落库。
    assert _delta_texts(recorder) == ["x" * 100 + "y" * 100, "z" * 100]
    assert recorder.names == ["delta", "delta", "done"]
    assert recorder.names[-2:] == ["delta", "done"]


@pytest.mark.asyncio
@pytest.mark.parametrize("run_type", ["chat", "research"])
async def test_interval_triggers_write_without_char_threshold(monkeypatch, run_type):
    clock = Clock()
    recorder = Recorder()

    async def stream(run, attempt):
        for index in range(4):
            clock.advance(0.5)
            yield ("delta", {"text": str(index) * 5})
        yield ("done", {"status": "ok", "run_finalized": True})

    worker = _worker(monkeypatch, recorder, stream, clock)
    await worker._execute_inner(_ticket(run_type))

    # 1s 的间隔触发两次落库；最后 pending 为空，done 单独一条。
    assert _delta_texts(recorder) == ["0" * 5 + "1" * 5, "2" * 5 + "3" * 5]
    assert recorder.names == ["delta", "delta", "done"]


@pytest.mark.asyncio
@pytest.mark.parametrize("run_type", ["chat", "research"])
async def test_stream_end_flushes_remaining_text(monkeypatch, run_type):
    clock = Clock()
    recorder = Recorder()

    async def stream(run, attempt):
        yield ("delta", {"text": "短"})
        yield ("done", {"status": "ok", "run_finalized": True})

    worker = _worker(monkeypatch, recorder, stream, clock)
    await worker._execute_inner(_ticket(run_type))

    assert _delta_texts(recorder) == ["短"]
    assert recorder.names == ["delta", "done"]


@pytest.mark.asyncio
async def test_critical_event_flushes_delta_in_same_batch(monkeypatch):
    clock = Clock()
    recorder = Recorder()

    async def stream(run, attempt):
        yield ("tool_start", {"tool": "ifind.financials"})
        yield ("delta", {"text": "abc"})
        yield ("tool_end", {"tool": "ifind.financials", "status": "ok"})
        yield ("done", {"status": "ok", "run_finalized": True})

    worker = _worker(monkeypatch, recorder, stream, clock)
    await worker._execute_inner(_ticket("research"))

    assert recorder.batches[0] == [("tool_start", {"tool": "ifind.financials"})]
    assert recorder.batches[1][0][0] == "delta"
    assert recorder.batches[1][1][0] == "tool_end"
    assert _delta_texts(recorder) == ["abc"], "正文不能丢，也不能排到 tool_end 之后"


@pytest.mark.asyncio
async def test_delta_reset_discards_buffered_text(monkeypatch):
    clock = Clock()
    recorder = Recorder()

    async def stream(run, attempt):
        yield ("delta", {"text": "旧文本"})
        yield ("delta_reset", {"reason": "tool_call"})
        yield ("delta", {"text": "新文本"})
        yield ("done", {"status": "ok", "run_finalized": True})

    worker = _worker(monkeypatch, recorder, stream, clock)
    await worker._execute_inner(_ticket("research"))

    assert recorder.names == ["delta_reset", "delta", "done"]
    assert _delta_texts(recorder) == ["新文本"]
    assert all("旧文本" not in str(data) for _event, data in recorder.events)


@pytest.mark.asyncio
async def test_retry_attempt_writes_reset_before_any_delta(monkeypatch):
    clock = Clock()
    recorder = Recorder()

    async def stream(run, attempt):
        yield ("delta", {"text": "重试正文"})
        yield ("done", {"status": "ok", "run_finalized": True})

    worker = _worker(monkeypatch, recorder, stream, clock)
    await worker._execute_inner(_ticket("research", attempt=2))

    first_batch = recorder.batches[0]
    assert [event for event, _data in first_batch] == ["delta_reset", "delta", "done"]
    assert first_batch[0][1] == {"reason": "retry"}
    assert first_batch[1][1] == {"text": "重试正文"}, "reset 必须排在正文之前"


@pytest.mark.asyncio
async def test_fenced_retry_writes_nothing_at_all(monkeypatch):
    clock = Clock()
    recorder = Recorder()

    async def stream(run, attempt):
        raise RunFenced("lease lost")
        yield  # pragma: no cover

    worker = _worker(monkeypatch, recorder, stream, clock)
    await worker._execute_inner(_ticket("research", attempt=2))

    assert recorder.batches == []


@pytest.mark.asyncio
async def test_error_path_flushes_partial_answer_first(monkeypatch):
    clock = Clock()
    recorder = Recorder()

    async def stream(run, attempt):
        yield ("delta", {"text": "半截正文"})
        raise RuntimeError("model exploded")

    worker = _worker(monkeypatch, recorder, stream, clock)
    await worker._execute_inner(_ticket("research"))

    assert recorder.names == ["delta", "error", "done"]
    assert recorder.events[0][1] == {"text": "半截正文"}
    assert recorder.events[2][1]["status"] == "error"


class _Result:
    def __init__(self, rowcount: int) -> None:
        self.rowcount = rowcount


class _FakeSession:
    def __init__(self, rowcount: int) -> None:
        self.rowcount = rowcount
        self.calls: list[tuple[str, dict]] = []

    def execute(self, stmt, params=None):
        self.calls.append((str(stmt), params or {}))
        return _Result(self.rowcount)


def test_batch_insert_is_one_statement_with_ordered_values():
    from app.agent import run_queue

    db = _FakeSession(rowcount=2)
    items = [("delta", {"text": "a"}), ("tool_end", {"tool": "x"})]
    run_queue.append_events_in_session(db, _ticket("research"), items)

    assert len(db.calls) == 1, "一个批次只能发一条 INSERT"
    sql, params = db.calls[0]
    assert sql.count("insert into session_events") == 1
    assert "from (values" in sql
    assert "where exists" in sql
    assert "worker_id = :worker_id" in sql
    assert params["event0"] == "delta" and params["event1"] == "tool_end"
    assert params["worker_id"] == "worker-a" and params["attempt"] == 1


def test_batch_insert_raises_when_fence_rejects_rows():
    from app.agent import run_queue

    db = _FakeSession(rowcount=1)
    with pytest.raises(RunFenced):
        run_queue.append_events_in_session(
            db, _ticket("research"), [("delta", {"text": "a"}), ("delta", {"text": "b"})]
        )


def test_unfenced_batch_raises_instead_of_dropping_rows():
    from app.agent import run_queue

    db = _FakeSession(rowcount=1)
    with pytest.raises(RuntimeError):
        run_queue.append_events_in_session(
            db,
            _ticket("research"),
            [("delta", {"text": "a"}), ("delta", {"text": "b"})],
            fenced=False,
        )


def test_single_event_helper_keeps_old_signature():
    from app.agent import run_queue

    db = _FakeSession(rowcount=1)
    run_queue.append_event_in_session(db, _ticket("research"), "delta", {"text": "x"})

    sql, params = db.calls[0]
    assert sql.startswith("insert into session_events")
    assert params["event0"] == "delta"


@pytest.mark.asyncio
async def test_replay_uses_last_snapshot_only_for_legacy_chat(monkeypatch):
    from app.api import chat

    seen: list[int] = []

    async def fake_tail(principal, run_id, run_type, cursor):
        seen.append(cursor)
        if False:  # pragma: no cover
            yield ""

    monkeypatch.setattr(chat, "_tail_run", fake_tail)
    principal = SimpleNamespace(tenant_id=TENANT, user_id=USER)
    run = {
        "id": RUN,
        "run_type": "chat",
        "message_id": MESSAGE,
        "conversation_id": CONV,
    }

    monkeypatch.setattr(
        chat.run_queue,
        "last_snapshot",
        lambda *a, **k: {"id": 42, "event": "snapshot", "data": {"text": "历史全文"}},
    )
    chunks = [chunk async for chunk in chat._replay_run(principal, run, 0)]
    assert chunks[0].startswith("event: meta")
    assert seen == [41], "历史 run 从最后一条 snapshot 的前一条开始回放"

    seen.clear()
    monkeypatch.setattr(chat.run_queue, "last_snapshot", lambda *a, **k: None)
    [chunk async for chunk in chat._replay_run(principal, run, 0)]
    assert seen == [0], "新 run 没有 snapshot，从 0 开始按 delta 回放"


def test_read_events_uses_after_cursor_and_id_order():
    from app.agent import run_queue

    captured: dict[str, str] = {}

    class _Rows:
        def mappings(self):
            return self

        def all(self):
            return []

    class _DB:
        def execute(self, stmt, params=None):
            captured["sql"] = str(stmt)
            return _Rows()

    class _Scope:
        def __enter__(self):
            return _DB()

        def __exit__(self, *args):
            return False

    import app.agent.run_queue as module

    original = module.tenant_scope
    module.tenant_scope = lambda tenant_id, user_id: _Scope()
    try:
        run_queue.read_events_by_run(TENANT, USER, RUN, 17)
    finally:
        module.tenant_scope = original

    assert "id > :after" in captured["sql"]
    assert "order by id" in captured["sql"]