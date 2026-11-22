from dataclasses import replace
from types import SimpleNamespace

from app.config import get_settings

from app.agent.run_context import build_run_context, plan_payload
from app.tools.agentscope_tools import create_tools
from app.tools.registry import tool_specs


def _context():
    run = SimpleNamespace(
        question="Analyze US stock SMTC",
        history=[],
        turn=1,
        tenant_id="tenant",
        user_id="user",
        conversation_id="conversation",
        message_id="message",
        request_id="request",
    )
    settings = SimpleNamespace(
        task_timeout_seconds=180.0,
        max_tool_calls=4,
        max_steps=6,
    )
    return build_run_context(run, settings, object())


def test_agentscope_context_has_no_question_specific_plan():
    ctx = _context()
    assert ctx.plan.intent == "agentscope_auto"
    assert ctx.domain == "auto"
    assert ctx.entities == []


def test_agentscope_context_exposes_full_tool_catalog(monkeypatch):
    configured = replace(get_settings(), web_search_api_key='test-key')
    monkeypatch.setattr('app.config.get_settings', lambda: configured)
    ctx = _context()
    expected = {spec.name for spec in tool_specs()}
    assert set(ctx.allowed_tools) == expected
    assert len(create_tools(ctx)) == len(expected)


def test_plan_payload_marks_agentscope_auto_mode():
    ctx = _context()
    payload = plan_payload(ctx)
    assert payload["mode"] == "agentscope_auto"
    assert payload["intent"] == "agentscope_auto"
    assert payload["tools"] == []
    assert set(payload["allowed"]) == set(ctx.allowed_tools)
