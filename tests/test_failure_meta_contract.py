# -*- coding: utf-8 -*-
"""失败路径的契约。

钉住三件事（每一项都在实现里被踩过）：
  1. 失败写入必须保留 run_id / run_type —— 被覆盖过两次，
     冲掉之后刷新就再也找不到这条 run
  2. 失败一律可重问，不按异常类型分类 —— 分类必然漏，
     漏掉的那一类用户只能自己重打问题
  3. 失败必须同时写 error 与 done 事件 —— 只写状态不写事件，
     观战中的前端要干等 600 秒超时
"""

from __future__ import annotations

import asyncio
import uuid
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from app.agent import persist, run_worker

TENANT = uuid.UUID("11111111-1111-1111-1111-111111111111")
USER = uuid.UUID("22222222-2222-2222-2222-222222222222")
MESSAGE = uuid.UUID("44444444-4444-4444-4444-444444444444")


@pytest.fixture()
def fake_message(monkeypatch):
    """让 mark_assistant_failed_in_session 拿到一条可写的假消息。"""
    row = SimpleNamespace(
        id=MESSAGE,
        status="running",
        content="",
        meta={"run_id": "run-abc", "run_type": "chat"},
        updated_at=None,
    )

    class _DB:
        def scalar(self, _stmt):
            return row

    db = _DB()

    @contextmanager
    def fake_scope(_tenant, _user):
        yield db

    monkeypatch.setattr(persist, "tenant_scope", fake_scope)
    return row, db


def test_failed_payload_has_no_retry_classification():
    """payload 不再携带 retryable：是否给重问入口不该由错误类型决定。"""
    _content, meta = persist.failed_message_payload("X", "msg")
    assert "retryable" not in meta
    assert meta["error"]["code"] == "X"


def test_failed_message_keeps_run_id(fake_message):
    """run_id 是刷新后接回放流的唯一线索，失败时不能被覆盖掉。"""
    row, db = fake_message
    ok = persist.mark_assistant_failed_in_session(
        db, MESSAGE, "failed", "WORKER_LOST", "中断了"
    )
    assert ok is True
    assert row.meta["run_id"] == "run-abc"
    assert row.meta["run_type"] == "chat"
    assert row.status == "failed"


def test_mark_assistant_failed_wrapper_keeps_run_id(fake_message):
    row, _db = fake_message
    ok = persist.mark_assistant_failed(
        TENANT, USER, MESSAGE, "error", "RateLimitError", "失败了"
    )
    assert ok is True
    assert row.meta["run_id"] == "run-abc"


def _ticket(**over) -> run_worker.QueueTicket:
    base = dict(
        run_id=uuid.uuid4(),
        tenant_id=TENANT,
        user_id=USER,
        conversation_id=uuid.uuid4(),
        message_id=MESSAGE,
        request_id=uuid.uuid4(),
        attempt=1,
        run_type="chat",
    )
    base.update(over)
    return run_worker.QueueTicket(**base)


def _worker():
    w = run_worker.RunWorker.__new__(run_worker.RunWorker)
    w.settings = SimpleNamespace()
    return w


def test_worker_error_path_writes_both_events(monkeypatch):
    """失败必须写 error + done，前端才能立刻收尾。"""
    events: list[tuple] = []
    marks: list[tuple] = []

    monkeypatch.setattr(
        run_worker, "append_event", lambda t, e, d: events.append((e, d))
    )
    monkeypatch.setattr(
        run_worker, "mark_assistant_failed", lambda *a, **k: marks.append(a)
    )
    monkeypatch.setattr(run_worker, "mark_events_expiring", lambda *a, **k: 0)
    monkeypatch.setattr(run_worker, "finish", lambda *a, **k: None)
    monkeypatch.setattr(run_worker, "run_status", lambda *a, **k: "running")
    monkeypatch.setattr(
        run_worker,
        "load_chat_run",
        lambda *_a, **_k: SimpleNamespace(
            conversation_id=uuid.uuid4(),
            message_id=MESSAGE,
            request_id=uuid.uuid4(),
            turn=1,
        ),
    )

    worker = _worker()

    async def boom(_run, _attempt):
        if False:  # pragma: no cover
            yield
        raise RuntimeError("model exploded")

    monkeypatch.setattr(worker, "_stream", boom)
    asyncio.run(worker._execute_inner(_ticket()))

    names = [e for e, _ in events]
    assert "error" in names, "必须写 error 事件"
    assert "done" in names, "必须写 done 事件，否则前端等超时"
    assert marks, "必须标记消息失败"

    done = [d for e, d in events if e == "done"][0]
    assert "retryable" not in done, "done 事件不再携带 retryable"


@pytest.mark.parametrize("run_type", ["chat", "research"])
def test_failure_events_do_not_branch_on_error_kind(monkeypatch, run_type):
    """chat 与 research 的失败事件结构一致：不给前端分类信号。"""
    events: list[tuple] = []
    monkeypatch.setattr(
        run_worker, "append_event", lambda t, e, d: events.append((e, d))
    )
    monkeypatch.setattr(run_worker, "mark_assistant_failed", lambda *a, **k: None)
    monkeypatch.setattr(run_worker, "mark_events_expiring", lambda *a, **k: 0)
    monkeypatch.setattr(run_worker, "finish", lambda *a, **k: None)
    monkeypatch.setattr(run_worker, "run_status", lambda *a, **k: "running")
    monkeypatch.setattr(
        run_worker,
        "load_chat_run",
        lambda *_a, **_k: SimpleNamespace(
            conversation_id=uuid.uuid4(),
            message_id=MESSAGE,
            request_id=uuid.uuid4(),
            turn=1,
        ),
    )

    worker = _worker()

    async def boom(_run, _attempt):
        if False:  # pragma: no cover
            yield
        raise ValueError("any error")

    monkeypatch.setattr(worker, "_stream", boom)
    asyncio.run(worker._execute_inner(_ticket(run_type=run_type)))

    done = [d for e, d in events if e == "done"][0]
    assert "retryable" not in done
    # 两种类型都不额外加限制字段
    assert set(done) == {"status", "message_id", "conversation_id", "request_id"}
