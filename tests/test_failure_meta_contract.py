# -*- coding: utf-8 -*-
"""失败路径的 meta 契约：retryable 必须显式传递，run_id 不能被冲掉。

这两个性质在实现里被踩过两次：
  1. persist_turn 直接覆盖 meta，把 run_id 冲掉了 —— 刷新后再也找不到 run
  2. mark_assistant_failed 没接 retryable，聊天失败也写成 True

用假 session 直接验函数契约，不需要真库。
"""

from __future__ import annotations

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


def test_failed_payload_defaults_to_retryable():
    _content, meta = persist.failed_message_payload("X", "msg")
    assert meta["retryable"] is True


def test_failed_payload_honours_retryable_false():
    _content, meta = persist.failed_message_payload("X", "msg", retryable=False)
    assert meta["retryable"] is False


def test_failed_message_keeps_run_id(fake_message):
    """run_id 是刷新后接回放流的唯一线索，失败时不能被覆盖掉。"""
    row, db = fake_message
    ok = persist.mark_assistant_failed_in_session(
        db, MESSAGE, "failed", "WORKER_LOST", "中断了", retryable=False
    )
    assert ok is True
    assert row.meta["run_id"] == "run-abc"
    assert row.meta["run_type"] == "chat"
    assert row.meta["retryable"] is False


def test_mark_assistant_failed_forwards_retryable(fake_message):
    """外层包装必须把 retryable 透传下去，否则聊天失败会写成可重试。"""
    row, _db = fake_message
    ok = persist.mark_assistant_failed(
        TENANT, USER, MESSAGE, "error", "RateLimitError", "异常", False
    )
    assert ok is True
    assert row.meta["retryable"] is False


def test_mark_assistant_failed_default_is_retryable(fake_message):
    """不传时保持原行为（研究路径的历史调用依赖这个默认值）。"""
    row, _db = fake_message
    ok = persist.mark_assistant_failed(
        TENANT, USER, MESSAGE, "error", "SOME", "异常"
    )
    assert ok is True
    assert row.meta["retryable"] is True


def test_worker_error_path_passes_retryable_false(monkeypatch):
    """worker 的异常收尾必须显式传 False —— 这是 bug1 的回归点。"""
    marks: list[tuple] = []
    events: list[tuple] = []

    monkeypatch.setattr(
        run_worker, "mark_assistant_failed", lambda *args: marks.append(args)
    )
    monkeypatch.setattr(
        run_worker, "append_event", lambda ticket, event, data: events.append((event, data))
    )
    monkeypatch.setattr(run_worker, "mark_events_expiring", lambda *a, **k: 0)
    monkeypatch.setattr(run_worker, "finish", lambda *a, **k: None)
    monkeypatch.setattr(run_worker, "run_status", lambda *a, **k: "running")

    ticket = run_worker.QueueTicket(
        run_id=uuid.uuid4(),
        tenant_id=TENANT,
        user_id=USER,
        conversation_id=uuid.uuid4(),
        message_id=MESSAGE,
        request_id=uuid.uuid4(),
        attempt=1,
        run_type="chat",
    )

    worker = run_worker.RunWorker.__new__(run_worker.RunWorker)
    # _execute_inner 读 self.settings（真构造要连库，这里只给需要的字段）
    worker.settings = SimpleNamespace()

    async def boom(_run, _attempt):
        if False:  # pragma: no cover
            yield
        raise RuntimeError("model exploded")

    # _execute_inner 里 first thing 是 load_chat_run
    monkeypatch.setattr(
        run_worker,
        "load_chat_run",
        lambda *_a, **_k: SimpleNamespace(
            conversation_id=ticket.conversation_id,
            message_id=ticket.message_id,
            request_id=ticket.request_id,
            turn=1,
        ),
    )
    monkeypatch.setattr(worker, "_stream", boom)

    import asyncio

    asyncio.run(worker._execute_inner(ticket))

    assert marks, "异常路径必须标记消息失败"
    assert marks[0][-1] is False, f"retryable 应为 False，实际 {marks[0][-1]}"

    done = [d for e, d in events if e == "done"]
    assert done, "必须写 done 事件让前端收尾"
    assert done[0]["retryable"] is False
