import time

import pytest
from agentscope.message import SystemMsg

from app.agent.budget import Budget
from app.agent.time_budget import (
    TIME_MARKER,
    TimeBudgetMiddleware,
    render_time_budget,
)


def test_render_time_budget_includes_total_remaining_and_threshold():
    text = render_time_budget(300, 200)

    assert TIME_MARKER in text
    assert "本轮总时长 300 秒" in text
    assert "当前剩余约 200 秒" in text
    assert "剩余 60 秒时必须停止取数" in text
    assert "【必须收敛】" not in text


def test_render_time_budget_switches_to_convergence_instruction():
    text = render_time_budget(300, 30)

    assert "当前剩余约 30 秒" in text
    assert "【必须收敛】" in text
    assert "不要再调用工具" in text


@pytest.mark.asyncio
async def test_middleware_refreshes_remaining_seconds_between_model_calls():
    budget = Budget(timeout_seconds=300)
    budget.started = time.monotonic() - 100
    middleware = TimeBudgetMiddleware(budget)

    first = await middleware.on_system_prompt(None, "BASE PROMPT")
    budget.started = time.monotonic() - 270
    second = await middleware.on_system_prompt(None, "BASE PROMPT")

    assert "当前剩余约 200 秒" in first
    assert "当前剩余约 30 秒" in second
    assert "【必须收敛】" in second
    assert first != second
    assert middleware.prompt_updates == 2


@pytest.mark.asyncio
async def test_model_call_snapshot_proves_dynamic_budget_reached_model():
    middleware = TimeBudgetMiddleware(Budget(timeout_seconds=300))

    async def next_handler():
        return "ok"

    await middleware.on_model_call(
        None,
        {"messages": [SystemMsg(name="system", content=render_time_budget(300, 200))]},
        next_handler,
    )
    await middleware.on_model_call(
        None,
        {"messages": [SystemMsg(name="system", content=render_time_budget(300, 30))]},
        next_handler,
    )

    snapshot = middleware.snapshot()
    assert snapshot["total_seconds"] == 300
    assert snapshot["model_calls"] == 2
    assert snapshot["urgent_calls"] == 1
    assert snapshot["calls"][0]["remaining_seconds"] == 200
    assert snapshot["calls"][0]["marker_present"] is True
    assert snapshot["calls"][1]["remaining_seconds"] == 30
    assert snapshot["calls"][1]["urgent"] is True