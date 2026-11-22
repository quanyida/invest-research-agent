from agentscope.message import Msg, TextBlock, ToolCallBlock, ToolResultBlock
from agentscope.state import AgentState

from app.agent.agent_state_store import compact_agent_state, state_was_truncated


def _state() -> AgentState:
    state = AgentState(session_id="conversation")
    state.context.append(
        Msg(
            name="user",
            role="user",
            content=[TextBlock(text="上面的营销服务具体包括哪些")],
        )
    )
    state.context.append(
        Msg(
            name="assistant",
            role="assistant",
            content=[
                ToolCallBlock(id="t1", name="ifind_news_search", input="{}"),
                ToolResultBlock(
                    id="t1",
                    name="ifind_news_search",
                    output="RAW" * 5000,
                    state="success",
                ),
                TextBlock(text="结论：营销服务包括 A、B。\n依据：1. 公告披露。"),
            ],
        )
    )
    return state


def test_compact_agent_state_drops_tool_payloads_but_keeps_answer():
    state = _state()
    before = len(state.model_dump_json())

    released = compact_agent_state(state)
    after = len(state.model_dump_json())

    assert released == 2
    assert after < before
    assert [block.type for block in state.context[-1].content] == ["text"]
    assert "营销服务包括" in state.context[-1].get_text_content()

    restored = AgentState.model_validate(state.model_dump(mode="json"))
    assert restored.context[-1].get_text_content() == state.context[-1].get_text_content()


def test_compact_agent_state_is_idempotent():
    state = _state()
    compact_agent_state(state)
    assert compact_agent_state(state) == 0


def test_state_was_truncated_detects_fallback_marker():
    state = AgentState(session_id="s")
    assert not state_was_truncated(state)

    state.summary = (
        "<system-info>Some earlier messages were truncated for limited context."
        "</system-info>"
    )
    assert state_was_truncated(state)