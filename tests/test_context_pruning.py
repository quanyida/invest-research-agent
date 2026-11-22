import asyncio

from agentscope.message import Msg, TextBlock, ToolCallBlock, ToolResultBlock
from agentscope.state import AgentState

from app.agent.context_pruning import (
    PRUNED_PREFIX,
    ContextPruningMiddleware,
    prune_tool_payloads,
)


def _state(tool_count: int = 5) -> AgentState:
    state = AgentState(session_id="conversation")
    for index in range(tool_count):
        state.context.append(
            Msg(
                name="user",
                role="user",
                content=[TextBlock(text=f"问题{index}")],
            )
        )
        state.context.append(
            Msg(
                name="investment_research_agent",
                role="assistant",
                content=[
                    ToolCallBlock(id=f"t{index}", name="ifind_news_search", input="{}"),
                    ToolResultBlock(
                        id=f"t{index}",
                        name="ifind_news_search",
                        output=f"RAW{index}-" * 1000,
                        state="success",
                    ),
                    TextBlock(text=f"结论{index}"),
                ],
            )
        )
    return state


def test_prune_old_tool_payloads_keeps_recent_and_pairs():
    state = _state()
    pruned, released = prune_tool_payloads(state, keep_recent_tool_results=1)

    assert pruned == 4
    assert released > 0
    results = [
        block
        for message in state.context
        for block in message.content
        if isinstance(block, ToolResultBlock)
    ]
    assert results[-1].output == "RAW4-" * 1000
    assert all(
        isinstance(block.output, str) and block.output.startswith(PRUNED_PREFIX)
        for block in results[:-1]
    )

    for index, message in enumerate(state.context):
        calls = [
            block.id
            for block in message.content
            if isinstance(block, ToolCallBlock)
        ]
        pairs = [
            block.id
            for block in message.content
            if isinstance(block, ToolResultBlock)
        ]
        assert calls == pairs
        if message.role == "user":
            assert message.get_text_content() == f"问题{index // 2}"
        else:
            assert f"结论{index // 2}" in message.get_text_content()

    assert prune_tool_payloads(state, keep_recent_tool_results=1) == (0, 0)


def test_middleware_retries_then_keeps_context_without_truncation():
    state = _state()
    middleware = ContextPruningMiddleware(
        keep_recent_tool_results=4,
        retry_keep_recent_tool_results=1,
    )
    calls = 0

    class DummyAgent:
        pass

    agent = DummyAgent()
    agent.state = state

    async def next_handler() -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("The connection closed before the model finished")

    asyncio.run(middleware.on_compress_context(agent, {}, next_handler))

    assert calls == 2
    assert middleware.compression_attempts == 2
    assert middleware.compression_failed is True
    assert middleware.fallback_mode == "keep_context"
    assert "connection closed" in middleware.last_error
    assert len(state.context) == 10
    assert any(
        isinstance(block, TextBlock) and block.text == "结论4"
        for block in state.context[-1].content
    )


def test_middleware_snapshot_reports_pruning():
    state = _state()
    middleware = ContextPruningMiddleware(keep_recent_tool_results=1)

    class DummyAgent:
        pass

    agent = DummyAgent()
    agent.state = state

    async def next_handler() -> None:
        return None

    asyncio.run(middleware.on_compress_context(agent, {}, next_handler))
    snapshot = middleware.snapshot()

    assert snapshot["pruned_tool_results"] == 4
    assert snapshot["pruned_chars"] > 0
    assert snapshot["compression_failed"] is False
    assert snapshot["fallback_mode"] is None