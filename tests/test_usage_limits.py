from __future__ import annotations

import re
import uuid
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api import chat, usage as usage_api


ROOT = Path(__file__).resolve().parents[1]


class _QuotaDB:
    def __init__(self, *, questions: int, tool_calls: int) -> None:
        self.row = SimpleNamespace(questions=questions, tool_calls=tool_calls)

    def scalar(self, _statement):
        return self.row


def test_tool_quota_message_does_not_expose_metric():
    db = _QuotaDB(questions=0, tool_calls=400)

    with pytest.raises(HTTPException) as raised:
        chat._check_daily_quota(db, uuid.uuid4())

    assert raised.value.status_code == 429
    assert raised.value.detail == {
        "code": "DAILY_LIMIT_REACHED",
        "message": "今日用量已达上限，请明天再试",
    }
    message = raised.value.detail["message"]
    assert "400" not in message
    assert "工具" not in message
    assert "次" not in message


def test_question_quota_message_remains_specific():
    db = _QuotaDB(questions=50, tool_calls=0)

    with pytest.raises(HTTPException) as raised:
        chat._check_daily_quota(db, uuid.uuid4())

    assert raised.value.status_code == 429
    assert raised.value.detail == {
        "code": "DAILY_QUESTION_LIMIT",
        "message": "今日提问次数已达上限（50）",
    }
    assert "提问次数" in raised.value.detail["message"]


def test_usage_limits_keep_all_backend_fields(monkeypatch):
    principal = SimpleNamespace(tenant_id=uuid.uuid4(), user_id=uuid.uuid4())
    empty_db = SimpleNamespace(scalars=lambda _statement: SimpleNamespace(all=lambda: []))

    @contextmanager
    def fake_tenant_scope(*_args, **_kwargs):
        yield empty_db

    monkeypatch.setattr(usage_api, "require_principal", lambda _request: principal)
    monkeypatch.setattr(usage_api, "tenant_scope", fake_tenant_scope)
    monkeypatch.setattr(
        usage_api,
        "get_settings",
        lambda: SimpleNamespace(
            task_timeout_seconds=300,
            model_max_attempts=4,
            llm_model="test-model",
        ),
    )

    payload = usage_api.usage(object(), days=7)

    assert set(payload["limits"]) == {
        "questions_per_day",
        "tool_calls_per_day",
        "requests_per_minute",
        "task_timeout_seconds",
        "max_tool_calls_per_question",
        "model_max_attempts",
    }
    assert payload["limits"]["questions_per_day"] == 50
    assert payload["limits"]["tool_calls_per_day"] == 400


def test_usage_page_limit_renderer_only_shows_question_progress():
    app_js = (ROOT / "static" / "assets" / "app.js").read_text(encoding="utf-8")
    match = re.search(r"const pairs = \[(.*?)\];", app_js, re.S)
    assert match is not None
    pairs = match.group(1)

    assert '"每日提问"' in pairs
    assert 'formatNumber(today.questions || 0) + " / " + formatNumber(limits.questions_per_day) + " 次"' in pairs
    for label in (
        "每日工具调用",
        "每分钟请求",
        "单次任务超时",
        "单次最多工具",
        "单次模型尝试",
    ):
        assert f'"{label}"' not in pairs


def test_usage_html_does_not_expose_tool_limit_unit():
    html = (ROOT / "static" / "usage.html").read_text(encoding="utf-8")

    assert 'id="todayQuestions"' not in html
    assert "/ 400 次" not in html