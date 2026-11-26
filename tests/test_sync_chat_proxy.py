# -*- coding: utf-8 -*-
"""仍在使用中的落库与串行逻辑。

原文件覆盖的同步转发链路（`_proxy_chat` / `_load_run` / `_absorb_sse` /
`_finalize_interrupted_turn` / `agent_service.execute`）已随之删除：任务现在
统一走 chat_runs 队列，API 层只订阅 session_events，不再转发。
"""
from __future__ import annotations

import uuid
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from app.agent import run_queue
from app.agent.persist import message_preview
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


def test_message_preview_strips_citation_markers():
    # 引用标记形如 [E1-3]；括号种类与连字符变体都要能删掉
    assert message_preview("毛利率 25.31% [E1-3] 同比升", 40) == "毛利率 25.31% 同比升"
    assert message_preview("营收（E2–5）增长", 40) == "营收增长"
    assert message_preview("净利【E3—7】改善", 40) == "净利改善"


def test_message_preview_truncates_to_limit():
    text = "甲" * 200
    preview = message_preview(text, 40)
    assert len(preview) <= 41
    assert preview.startswith("甲")


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
    """同会话已有活跃 run 时，_prepare_turn 必须转成 409。"""

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
