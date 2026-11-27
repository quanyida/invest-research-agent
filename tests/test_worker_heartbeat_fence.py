# -*- coding: utf-8 -*-
"""心跳与执行的协作契约。

钉住四件事：
  1. 心跳判定被接管（RunFenced）后，执行协程必须被取消，不能继续跑
  2. 执行先结束时先停心跳，不能被心跳反过来取消
  3. 外部取消原样抛出，不伪装成 RunFenced
  4. 被接管的 Worker 写不进事件、标不了失败，也不会覆盖新持有者
"""

from __future__ import annotations

import asyncio
import uuid
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from app.agent import persist, run_queue, run_worker
from app.agent.persist import RunFenced
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
    worker_id="worker-a",
    run_type="research",
)

def _worker() -> run_worker.RunWorker:
    worker = run_worker.RunWorker.__new__(run_worker.RunWorker)
    worker.settings = SimpleNamespace(run_lease_seconds=120.0)
    worker.worker_id = "worker-a"
    worker._tasks = []
    worker._stopping = asyncio.Event()
    return worker

@pytest.mark.asyncio
async def test_heartbeat_fence_cancels_execution():
    """心跳发现租约被拿走 → 执行协程被取消，不继续跑。"""
    started = asyncio.Event()
    state = {"cancelled": False}

    async def slow(ticket):
        started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            state["cancelled"] = True
            raise

    async def fenced(ticket):
        await started.wait()
        raise RunFenced("lease lost")

    worker = _worker()
    worker._execute_inner = slow
    worker._heartbeat = fenced

    await asyncio.wait_for(worker._execute(TICKET), timeout=5)
    assert state["cancelled"] is True, "被 fence 后执行协程必须被取消"

@pytest.mark.asyncio
async def test_fenced_worker_stops_writing_events(monkeypatch):
    """被 fence 之后不再追加事件，也不写失败状态。"""
    appended: list[str] = []
    finished: list[tuple] = []
    marked: list[tuple] = []
    first_event = asyncio.Event()

    monkeypatch.setattr(
        run_worker, "load_chat_run", lambda ticket, settings: SimpleNamespace()
    )
    monkeypatch.setattr(
        run_worker, "append_event", lambda t, e, d: appended.append(e)
    )
    monkeypatch.setattr(run_worker, "finish", lambda *a, **k: finished.append(a))
    monkeypatch.setattr(
        run_worker, "mark_assistant_failed", lambda *a, **k: marked.append(a)
    )
    monkeypatch.setattr(run_worker, "mark_events_expiring", lambda *a, **k: 0)
    monkeypatch.setattr(run_worker, "run_status", lambda *a, **k: "running")

    async def stream(run, attempt):
        yield ("tool_start", {"tool": "ifind.financials"})
        first_event.set()
        await asyncio.sleep(30)
        yield ("delta", {"text": "late"})  # pragma: no cover

    async def fenced(ticket):
        await first_event.wait()
        raise RunFenced("lease lost")

    worker = _worker()
    worker._stream = stream
    worker._heartbeat = fenced

    await asyncio.wait_for(worker._execute(TICKET), timeout=5)

    assert appended == ["tool_start"], "被 fence 后不能再写事件"
    assert finished == [], "被 fence 后不能把任务标成失败"
    assert marked == [], "被 fence 后不能改消息状态"

@pytest.mark.asyncio
async def test_execution_finishing_first_cancels_heartbeat():
    """正常完成时先停心跳，不能被心跳反过来取消。"""
    heartbeat = {"cancelled": False}
    state = {"done": False}

    async def quick(ticket):
        state["done"] = True

    async def idle(ticket):
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            heartbeat["cancelled"] = True
            raise

    worker = _worker()
    worker._execute_inner = quick
    worker._heartbeat = idle

    await asyncio.wait_for(worker._execute(TICKET), timeout=5)
    assert state["done"] is True
    assert heartbeat["cancelled"] is True, "执行结束后心跳必须被取消"

@pytest.mark.asyncio
async def test_external_cancel_is_not_converted_to_fence():
    """外部取消原样抛出，两个子任务都被收掉。"""
    started = asyncio.Event()
    state = {"exec": False, "heartbeat": False}

    async def slow(ticket):
        started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            state["exec"] = True
            raise

    async def idle(ticket):
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            state["heartbeat"] = True
            raise

    worker = _worker()
    worker._execute_inner = slow
    worker._heartbeat = idle

    task = asyncio.create_task(worker._execute(TICKET))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert state == {"exec": True, "heartbeat": True}

@pytest.mark.asyncio
async def test_simultaneous_completion_keeps_result(monkeypatch):
    """心跳与执行同时结束时不丢异常、不错写状态。"""
    finished: list[tuple] = []
    marked: list[tuple] = []

    monkeypatch.setattr(
        run_worker, "load_chat_run", lambda ticket, settings: SimpleNamespace()
    )
    monkeypatch.setattr(run_worker, "append_event", lambda t, e, d: None)
    monkeypatch.setattr(run_worker, "finish", lambda *a, **k: finished.append(a))
    monkeypatch.setattr(
        run_worker, "mark_assistant_failed", lambda *a, **k: marked.append(a)
    )
    monkeypatch.setattr(run_worker, "mark_events_expiring", lambda *a, **k: 0)

    async def stream(run, attempt):
        if False:  # pragma: no cover
            yield
        return

    async def fenced(ticket):
        await asyncio.sleep(0)
        raise RunFenced("lease lost")

    worker = _worker()
    worker._stream = stream
    worker._heartbeat = fenced

    await asyncio.wait_for(worker._execute(TICKET), timeout=5)
    assert finished == []
    assert marked == []

@pytest.mark.asyncio
async def test_heartbeat_retries_transient_db_error(monkeypatch):
    """偶发数据库错误不能停掉续租；只有真正失去所有权才抛 RunFenced。"""
    real_sleep = asyncio.sleep

    async def fast_sleep(_seconds, *args, **kwargs):
        await real_sleep(0)

    monkeypatch.setattr(run_worker.asyncio, "sleep", fast_sleep)
    calls = {"renew": 0}

    def flaky_renew(*args, **kwargs):
        calls["renew"] += 1
        if calls["renew"] == 1:
            raise RuntimeError("db blip")
        return False

    monkeypatch.setattr(run_worker, "renew_lease", flaky_renew)
    monkeypatch.setattr(run_worker, "owns_run", lambda *a, **k: False)

    worker = _worker()
    with pytest.raises(RunFenced):
        await asyncio.wait_for(worker._heartbeat(TICKET), timeout=5)
    assert calls["renew"] == 2, "第一次失败后必须继续重试"

@pytest.mark.asyncio
async def test_heartbeat_exits_quietly_when_run_already_finalized(monkeypatch):
    """run 已由本 Worker 收尾时心跳正常退场，不触发 fencing。"""
    real_sleep = asyncio.sleep

    async def fast_sleep(_seconds, *args, **kwargs):
        await real_sleep(0)

    monkeypatch.setattr(run_worker.asyncio, "sleep", fast_sleep)
    monkeypatch.setattr(run_worker, "renew_lease", lambda *a, **k: False)
    monkeypatch.setattr(run_worker, "owns_run", lambda *a, **k: True)

    worker = _worker()
    await asyncio.wait_for(worker._heartbeat(TICKET), timeout=5)

@pytest.mark.asyncio
async def test_execution_not_cancelled_when_heartbeat_exits_quietly():
    """心跳正常退场时执行照常跑完。"""
    state = {"done": False}

    async def quick(ticket):
        await asyncio.sleep(0)
        state["done"] = True

    async def quiet(ticket):
        return None

    worker = _worker()
    worker._execute_inner = quick
    worker._heartbeat = quiet

    await asyncio.wait_for(worker._execute(TICKET), timeout=5)
    assert state["done"] is True

class _Result:
    def __init__(self, rowcount: int, row=None) -> None:
        self.rowcount = rowcount
        self._row = row

    def first(self):
        return self._row

class _FakeSession:
    def __init__(self, rowcount: int, row=None) -> None:
        self.rowcount = rowcount
        self.row = row
        self.statements: list[str] = []

    def execute(self, stmt, params=None):
        self.statements.append(str(stmt))
        return _Result(self.rowcount, self.row)

def test_append_event_is_fenced_by_worker_identity():
    db = _FakeSession(rowcount=0)
    with pytest.raises(RunFenced):
        run_queue.append_event_in_session(db, TICKET, "delta", {"text": "x"})
    sql = db.statements[0]
    assert "worker_id = :worker_id" in sql
    assert "attempt = :attempt" in sql

def test_append_event_without_worker_id_keeps_plain_insert():
    plain = QueueTicket(
        run_id=RUN,
        tenant_id=TENANT,
        user_id=USER,
        conversation_id=CONV,
        message_id=MESSAGE,
        request_id=REQUEST,
        attempt=0,
        worker_id=None,
    )
    db = _FakeSession(rowcount=1)
    run_queue.append_event_in_session(db, plain, "delta", {"text": "x"})
    assert "values (" in db.statements[0]

def test_sweep_writes_events_without_fence(monkeypatch):
    """租约回收自己写收尾事件时用 fenced=False：它刚把 worker_id 清空。"""
    row = {
        "id": RUN,
        "tenant_id": TENANT,
        "user_id": USER,
        "conversation_id": CONV,
        "message_id": MESSAGE,
        "request_id": REQUEST,
        "attempt": 1,
        "worker_id": "worker-a",
        "run_type": "chat",
    }
    statements: list[str] = []

    class _Rows:
        def mappings(self):
            return self

        def all(self):
            return [row]

    class _DB:
        def execute(self, stmt, params=None):
            sql = str(stmt)
            statements.append(sql)
            if sql.startswith("select id, tenant_id"):
                return _Rows()
            return _Result(1)

        def scalar(self, stmt):
            return SimpleNamespace(
                id=MESSAGE, status="running", content="", meta={}, updated_at=None
            )

    db = _DB()

    @contextmanager
    def fake_scope():
        yield db

    monkeypatch.setattr(run_queue, "session_scope", fake_scope)

    recovered = run_queue.sweep(2)
    assert [state for _ticket, state in recovered] == ["failed"]
    inserts = [s for s in statements if s.startswith("insert into session_events")]
    assert len(inserts) == 2
    assert all("worker_id = :worker_id" not in s for s in inserts)

def test_sweep_failed_run_marks_events_expiring(monkeypatch):
    """回收判失败后要在同一事务补 expires_at，否则这些事件永远清理不掉。"""
    row = {
        "id": RUN,
        "tenant_id": TENANT,
        "user_id": USER,
        "conversation_id": CONV,
        "message_id": MESSAGE,
        "request_id": REQUEST,
        "attempt": 1,
        "worker_id": "worker-a",
        "run_type": "chat",
    }
    statements: list[tuple[str, dict]] = []

    class _Rows:
        def mappings(self):
            return self

        def all(self):
            return [row]

    class _DB:
        def execute(self, stmt, params=None):
            sql = str(stmt)
            statements.append((sql, params or {}))
            if sql.startswith("select id, tenant_id"):
                return _Rows()
            return _Result(1)

        def scalar(self, stmt):
            return SimpleNamespace(
                id=MESSAGE, status="running", content="", meta={}, updated_at=None
            )

    @contextmanager
    def fake_scope():
        yield _DB()

    monkeypatch.setattr(run_queue, "session_scope", fake_scope)

    recovered = run_queue.sweep(2, chat_retention_hours=7)
    assert [state for _ticket, state in recovered] == ["failed"]
    expiring = [(sql, p) for sql, p in statements if sql.startswith("update session_events")]
    assert expiring, "failed run must mark its events expiring"
    sql, params = expiring[0]
    assert "expires_at" in sql and "expires_at is null" in sql
    assert ":hours" in sql
    assert params["hours"] == 7, "必须使用传入的保留时长，而不是写死 24"


def test_sweep_uses_research_retention_hours(monkeypatch):
    """research 用另一档保留时长。"""
    row = {
        "id": RUN,
        "tenant_id": TENANT,
        "user_id": USER,
        "conversation_id": CONV,
        "message_id": MESSAGE,
        "request_id": REQUEST,
        "attempt": 2,
        "worker_id": "worker-a",
        "run_type": "research",
    }
    statements: list[tuple[str, dict]] = []

    class _Rows:
        def mappings(self):
            return self

        def all(self):
            return [row]

    class _DB:
        def execute(self, stmt, params=None):
            sql = str(stmt)
            statements.append((sql, params or {}))
            if sql.startswith("select id, tenant_id"):
                return _Rows()
            return _Result(1)

        def scalar(self, stmt):
            return SimpleNamespace(
                id=MESSAGE, status="running", content="", meta={}, updated_at=None
            )

    @contextmanager
    def fake_scope():
        yield _DB()

    monkeypatch.setattr(run_queue, "session_scope", fake_scope)

    run_queue.sweep(2, research_retention_hours=5)
    expiring = [(sql, p) for sql, p in statements if sql.startswith("update session_events")]
    assert expiring[0][1]["hours"] == 5


def test_retention_hours_by_run_type_and_fallback():
    settings = SimpleNamespace(
        event_retention_chat_hours=3, event_retention_research_hours=9
    )
    assert run_worker._retention_hours(settings, "chat") == 3
    assert run_worker._retention_hours(settings, "research") == 9
    # 假 settings 缺字段时回退默认值，测试替身不用跟着改
    assert run_worker._retention_hours(SimpleNamespace(), "chat") == 24

def test_finish_is_fenced(monkeypatch):
    captured = {}

    class _DB:
        def execute(self, stmt, params=None):
            captured["sql"] = str(stmt)
            captured["params"] = params
            return _Result(0)

    @contextmanager
    def fake_scope():
        yield _DB()

    monkeypatch.setattr(run_queue, "session_scope", fake_scope)

    ok = run_queue.finish(RUN, "failed", worker_id="worker-a", attempt=1)
    assert ok is False, "rowcount=0 时不能冒充成功"
    assert "worker_id = :worker_id" in captured["sql"]
    assert captured["params"]["worker_id"] == "worker-a"

def test_mark_assistant_failed_skips_when_not_owner(monkeypatch):
    class _DB:
        def execute(self, stmt, params=None):
            return _Result(0, row=None)

    @contextmanager
    def fake_scope(_tenant, _user):
        yield _DB()

    monkeypatch.setattr(persist, "tenant_scope", fake_scope)

    def _boom(*args, **kwargs):
        raise AssertionError("非持有者不能写消息状态")

    monkeypatch.setattr(persist, "mark_assistant_failed_in_session", _boom)

    ok = persist.mark_assistant_failed(
        TENANT, USER, MESSAGE, "error", "X", "msg",
        run_id=RUN, worker_id="worker-a", attempt=1,
    )
    assert ok is False

@pytest.mark.asyncio
async def test_error_path_stops_at_first_fenced_write(monkeypatch):
    """失败流程里第一条被 fence 的写入就收手，后续标记一个都不发。"""
    written: list[str] = []
    finished: list[tuple] = []
    marked: list[tuple] = []

    monkeypatch.setattr(
        run_worker, "load_chat_run", lambda ticket, settings: SimpleNamespace()
    )
    monkeypatch.setattr(run_worker, "run_status", lambda *a, **k: "running")

    def fenced_append(ticket, event, data):
        written.append(event)
        raise RunFenced("lease lost")

    monkeypatch.setattr(run_worker, "append_event", fenced_append)
    monkeypatch.setattr(run_worker, "finish", lambda *a, **k: finished.append(a))
    monkeypatch.setattr(
        run_worker, "mark_assistant_failed", lambda *a, **k: marked.append(a)
    )
    monkeypatch.setattr(run_worker, "mark_events_expiring", lambda *a, **k: 0)

    async def boom(run, attempt):
        if False:  # pragma: no cover
            yield
        raise RuntimeError("model exploded")

    worker = _worker()
    worker._stream = boom

    await asyncio.wait_for(worker._execute_inner(TICKET), timeout=5)

    assert written == ["error"], "第一条被拒绝后不应继续写"
    assert marked == []
    assert finished == []
