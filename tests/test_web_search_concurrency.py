import asyncio
import threading
import time
from types import SimpleNamespace

from agentscope.agent import Agent
from agentscope.message import ToolCallBlock
from agentscope.model import ChatResponse
from agentscope.tool import Toolkit

import app.tools.web_search as web_search
from app.agent.budget import Budget
from app.agent.context import build_agent_system_prompt
from app.agent.evidence_collector import EvidenceCollector
from app.tools.agentscope_tools import AuditedTool
from app.tools.ifind_gateway import ToolOutcome
from app.tools.registry import ToolSpec

def _spec(provider):
    name = "web.search" if provider == "web" else "ifind.stock_quote"
    return ToolSpec(
        name=name,
        server=provider,
        upstream="search" if provider == "web" else "quote",
        description=f"{provider} tool",
        schema={"type": "object", "properties": {"query": {"type": "string"}}},
        cache_ttl_seconds=0,
        provider=provider,
        source_kind="news" if provider == "web" else "structured",
        source_kind_label="资讯" if provider == "web" else "结构化",
    )

def _tool_ctx(provider="web", delay=0.0):
    ctx = SimpleNamespace(
        allowed_tools=[_spec(provider).name],
        budget=Budget(timeout_seconds=60),
        collector=EvidenceCollector(turn=1),
    )

    def executor(args):
        if delay:
            time.sleep(delay)
        return ToolOutcome(
            status="ok",
            evidence=None,
            error_code=None,
            latency_ms=int(delay * 1000),
            cached=False,
            query_text=str(args.get("query") or ""),
            upstream_calls=[],
        )

    return ctx, AuditedTool(_spec(provider), ctx, executor)

class _NoopModel:
    model = "fake-model"
    stream = False
    context_size = 1_000_000
    formatter = SimpleNamespace(supported_input_media_types=set())

    async def count_tokens(self, messages, tools=None):
        return 1

    async def __call__(self, messages, tools=None, tool_choice=None, **kwargs):
        return ChatResponse(content=[], is_last=True)

def test_web_tools_are_concurrency_safe_and_ifind_tools_are_not():
    _, web_tool = _tool_ctx("web")
    _, ifind_tool = _tool_ctx("ifind")

    assert web_tool.is_concurrency_safe is True
    assert ifind_tool.is_concurrency_safe is False

def test_prompt_asks_for_parallel_multi_query_research():
    for raw_mode in (False, True):
        ctx = SimpleNamespace(
            settings=SimpleNamespace(
                max_tool_calls=4,
                ifind_raw_passthrough=raw_mode,
            ),
            turn=2,
            allowed_tools=["web.search"],
        )

        prompt = build_agent_system_prompt(ctx)

        assert "只补新闻" not in prompt
        assert "不要用 web_search 查行情" not in prompt
        assert "一次性发出多条 web_search" in prompt
        assert "系统会并发执行" in prompt
        assert "上限 15 条" in prompt
        assert "它只是起点" in prompt

def test_two_web_calls_land_in_one_concurrent_batch():
    _, tool = _tool_ctx("web")
    agent = Agent(
        name="batch_probe",
        system_prompt="test",
        model=_NoopModel(),
        toolkit=Toolkit(tools=[tool]),
    )
    calls = [
        ToolCallBlock(id="c1", name=tool.name, input='{"query":"a"}'),
        ToolCallBlock(id="c2", name=tool.name, input='{"query":"b"}'),
    ]

    batches = asyncio.run(agent._batch_tool_calls(calls))

    assert len(batches) == 1
    assert batches[0].type == "concurrent"
    assert [call.id for call in batches[0].tool_calls] == ["c1", "c2"]

def test_web_calls_actually_overlap_and_keep_unique_sequences():
    ctx, tool = _tool_ctx("web", delay=0.2)
    state = {"peak": 0, "active": 0}
    lock = threading.Lock()
    inner = tool.executor

    def counted(args):
        with lock:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
        try:
            return inner(args)
        finally:
            with lock:
                state["active"] -= 1

    tool.executor = counted

    async def run():
        await asyncio.gather(*(tool.call(query=f"q-{index}") for index in range(4)))

    started = time.monotonic()
    asyncio.run(run())
    elapsed = time.monotonic() - started

    seqs = sorted(record.seq for record in ctx.collector.records)
    assert state["peak"] == 4
    assert seqs == [1, 2, 3, 4]
    assert ctx.budget.tools_used == 4
    assert elapsed < 0.2 * 4

def test_duplicate_arguments_still_dedupe_under_concurrency():
    ctx, tool = _tool_ctx("web")

    async def run():
        await asyncio.gather(tool.call(query="same"), tool.call(query="other"))

    asyncio.run(run())
    asyncio.run(tool.call(query="same"))

    assert len(ctx.collector.records) == 2
    assert ctx.budget.tools_used == 2

class _FakeResponse:
    status_code = 200

    def json(self):
        return {"results": [{"title": "t", "url": "https://example.com/a", "content": "c"}]}

def test_inflight_requests_are_capped(monkeypatch):
    state = {"active": 0, "peak": 0}
    lock = threading.Lock()

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, *args, **kwargs):
            with lock:
                state["active"] += 1
                state["peak"] = max(state["peak"], state["active"])
            time.sleep(0.1)
            with lock:
                state["active"] -= 1
            return _FakeResponse()

    monkeypatch.setattr(web_search.httpx, "Client", FakeClient)
    client = object.__new__(web_search.WebSearchClient)
    client.settings = SimpleNamespace(
        web_search_provider="tavily",
        web_search_base_url="https://example.com/search",
        web_search_api_key="test-key",
        web_search_timeout_seconds=5.0,
    )

    threads = [
        threading.Thread(target=client._request, args=(f"q-{index}", 5, "oneMonth"))
        for index in range(8)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert state["peak"] == web_search.MAX_INFLIGHT_REQUESTS
    assert web_search.MAX_INFLIGHT_REQUESTS == 4