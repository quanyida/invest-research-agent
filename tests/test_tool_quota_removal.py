import asyncio
from types import SimpleNamespace

import pytest

from agentscope.agent import Agent
from agentscope.event import ReplyEndEvent
from agentscope.message import Msg, TextBlock, ToolCallBlock, UserMsg
from agentscope.model import ChatResponse
from agentscope.tool import Toolkit
from agentscope.types import ReplyFinishedReason

from app.agent.budget import Budget, BudgetExceeded
from app.agent.context import build_agent_system_prompt
from app.agent.evidence_collector import EvidenceCollector
from app.agent.agentscope_runtime import AgentScopeRunner
from app.tools.agentscope_tools import AuditedTool
from app.tools.ifind_gateway import ToolOutcome
from app.tools.registry import ToolSpec

def _web_search_spec() -> ToolSpec:
    return ToolSpec(
        name="web.search",
        server="web",
        upstream="search",
        description="web search",
        schema={"type": "object", "properties": {"query": {"type": "string"}}},
        cache_ttl_seconds=0,
        provider="web",
        source_kind="news",
        source_kind_label="资讯",
    )

def _web_search_context():
    ctx = SimpleNamespace(
        allowed_tools=["web.search"],
        budget=Budget(timeout_seconds=60),
        collector=EvidenceCollector(turn=1),
    )

    def executor(args):
        return ToolOutcome(
            status="ok",
            evidence=None,
            error_code=None,
            latency_ms=1,
            cached=False,
            query_text=args["query"],
            upstream_calls=[],
        )

    return ctx, AuditedTool(_web_search_spec(), ctx, executor)

def test_budget_allows_more_than_four_tool_calls():
    budget = Budget(timeout_seconds=60)

    for _ in range(10):
        budget.take_tool()

    assert budget.tools_used == 10

def test_budget_only_stops_on_timeout():
    budget = Budget(timeout_seconds=0)

    with pytest.raises(BudgetExceeded):
        budget.take_tool()

def test_main_agent_uses_agentscope_default_max_iters():
    ctx = SimpleNamespace(settings=SimpleNamespace(max_steps=6, max_tool_calls=4))

    config = AgentScopeRunner._react_config(ctx)

    assert config.max_iters == 50

def test_system_prompt_has_no_single_turn_tool_quota():
    for raw_mode in (False, True):
        ctx = SimpleNamespace(
            settings=SimpleNamespace(max_tool_calls=4, ifind_raw_passthrough=raw_mode),
            turn=2,
            allowed_tools=["ifind.stock_financials"],
        )

        prompt = build_agent_system_prompt(ctx)

        assert "不设工具调用次数上限" in prompt
        assert "最多调用" not in prompt
        assert "BUDGET_EXCEEDED" not in prompt

def test_audited_tool_executes_more_than_four_distinct_calls():
    ctx, tool = _web_search_context()

    for index in range(6):
        asyncio.run(tool.call(query=f"query-{index}"))

    assert ctx.budget.tools_used == 6
    assert len(ctx.collector.records) == 6

class _AlwaysToolCallModel:
    model = "fake-tool-model"
    stream = False
    context_size = 1_000_000
    formatter = SimpleNamespace(supported_input_media_types=set())

    def __init__(self) -> None:
        self.calls = 0
        self.tool_call_requests = 0
        self.forced_finalization_calls = 0

    async def count_tokens(self, messages, tools=None):
        return 1

    async def __call__(self, messages, tools=None, tool_choice=None, **kwargs):
        self.calls += 1
        if getattr(tool_choice, "mode", None) == "none":
            self.forced_finalization_calls += 1
            return ChatResponse(
                content=[TextBlock(text="达到迭代上限后的收尾答案")],
                is_last=True,
            )
        self.tool_call_requests += 1
        return ChatResponse(
            content=[
                ToolCallBlock(
                    id=f"call-{self.tool_call_requests}",
                    name="web_search",
                    input=f'{{"query":"q-{self.tool_call_requests}"}}',
                )
            ],
            is_last=True,
        )

def test_react_loop_terminates_at_max_iters():
    ctx, audit_tool = _web_search_context()
    model = _AlwaysToolCallModel()
    runner_ctx = SimpleNamespace(settings=SimpleNamespace(max_steps=6, max_tool_calls=4))
    config = AgentScopeRunner._react_config(runner_ctx).model_copy(update={"max_iters": 3})
    agent = Agent(
        name="max_iters_probe",
        system_prompt="test",
        model=model,
        toolkit=Toolkit(tools=[audit_tool]),
        react_config=config,
    )

    final = None
    reason = None

    async def run():
        nonlocal final, reason
        async for item in agent.reply_stream(
            UserMsg(name="user", content="test"), yield_final_msg=True
        ):
            if isinstance(item, Msg):
                final = item
            elif isinstance(item, ReplyEndEvent):
                reason = item.finished_reason

    asyncio.run(run())

    assert config.max_iters == 3
    assert model.calls == 4
    assert model.tool_call_requests == 3
    assert model.forced_finalization_calls == 1
    assert ctx.budget.tools_used == 3
    assert reason == ReplyFinishedReason.EXCEED_MAX_ITERS
    assert str(reason) == "exceed_max_iters"
    assert final is not None
    assert final.get_text_content() == "达到迭代上限后的收尾答案"