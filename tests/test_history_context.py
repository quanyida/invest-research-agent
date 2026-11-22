from __future__ import annotations

import asyncio

from agentscope.message import UserMsg
from agentscope.state import AgentState

from app.agent.agent_state_store import (
    detect_gap,
    ensure_turn_marks,
    record_saved_turn,
    reset_turn_marks,
)
from app.agent.context_probe import ContextProbeMiddleware
from types import SimpleNamespace

from app.agent.budget import Budget
from app.agent.history_context import (
    SUMMARY_CHUNK_CHARS,
    chunk_turns,
    group_history,
    render_summary_input,
    split_history,
    wrap_summary,
)

def _rows() -> list[dict]:
    return [
        {"role": "user", "content": "Q1", "status": "done", "meta": {}},
        {
            "role": "assistant",
            "content": "A1 [E1-1]",
            "status": "done",
            "meta": {"turn": 1},
        },
        {"role": "user", "content": "Q2", "status": "done", "meta": {}},
        {"role": "assistant", "content": "", "status": "error", "meta": {"turn": 2}},
        {"role": "user", "content": "Q3", "status": "done", "meta": {}},
        {
            "role": "assistant",
            "content": "A3",
            "status": "degraded",
            "meta": {"turn": 3},
        },
    ]

def test_group_history_pairs_questions_and_skips_failed_turns():
    turns = group_history(_rows())
    assert [item["turn"] for item in turns] == [1, 3]
    assert turns[0]["question"] == "Q1"
    assert turns[1]["question"] == "Q3"

def test_group_history_numbers_turns_without_meta():
    rows = [
        {"role": "user", "content": "Q1", "status": "done", "meta": {}},
        {"role": "assistant", "content": "A1", "status": "done", "meta": {}},
        {"role": "user", "content": "Q2", "status": "done", "meta": {}},
        {"role": "assistant", "content": "A2", "status": "done", "meta": {}},
    ]
    turns = group_history(rows)
    assert [item["turn"] for item in turns] == [1, 2]

def test_split_history_keeps_recent_turns_whole():
    turns = [{"turn": index, "question": f"Q{index}", "answer": "A"} for index in range(1, 11)]
    early, recent = split_history(turns, 3)
    assert [item["turn"] for item in early] == list(range(1, 8))
    assert [item["turn"] for item in recent] == [8, 9, 10]

def test_split_history_with_fewer_turns_has_no_summary_input():
    turns = [{"turn": 1, "question": "Q", "answer": "A"}]
    early, recent = split_history(turns, 3)
    assert early == []
    assert len(recent) == 1

def test_render_summary_input_strips_citations_and_keeps_newest():
    turns = [
        {"turn": 1, "question": "Q1", "answer": "A1 [E1-1]"},
        {"turn": 2, "question": "Q2", "answer": "A2 " + "x" * 500},
        {"turn": 3, "question": "Q3", "answer": "A3"},
    ]
    body, included = render_summary_input(turns, max_chars=80)
    assert included == [3]
    assert "[E1-1]" not in body
    assert "第3轮" in body

def test_turn_marks_detect_missing_turn():
    state = AgentState()
    ensure_turn_marks(state, 1)
    record_saved_turn(state, 1)
    assert detect_gap(state, 2) == []

    record_saved_turn(state, 3)
    assert detect_gap(state, 4) == [2]

def test_reset_turn_marks_clears_history_gap():
    state = AgentState()
    reset_turn_marks(state, 4)
    record_saved_turn(state, 4)
    assert detect_gap(state, 5) == []

def test_legacy_state_without_marks_reports_no_gap():
    state = AgentState()
    assert detect_gap(state, 11) == []

def test_gap_detection_matches_requirement_scenario():
    state = AgentState()
    reset_turn_marks(state, 1)
    for turn in [1, 2, 3, 4, 5, 6, 7, 9, 10]:
        record_saved_turn(state, turn)
    assert detect_gap(state, 11) == [8]

def test_wrap_summary_labels_turn_range():
    text = wrap_summary("第1轮：讨论安克创新。", [{"turn": 1}, {"turn": 7}])
    assert text.startswith("【早期对话摘要（第1-7轮）】")
    single = wrap_summary("要点", [{"turn": 3}])
    assert single.startswith("【早期对话摘要（第3轮）】")

def test_context_probe_sees_summary_and_recent_questions():
    summary = "【早期对话摘要（第1-7轮）】\n第1轮：讨论安克创新。"
    probe = ContextProbeMiddleware(
        summary=summary,
        recent_questions=["第8轮的问题", "第9轮的问题"],
    )
    messages = [
        UserMsg(name="user", content=summary),
        UserMsg(name="user", content="第8轮的问题"),
        UserMsg(name="user", content="第9轮的问题"),
    ]

    async def handler():
        return "ok"

    result = asyncio.run(
        probe.on_model_call(None, {"messages": messages}, handler)
    )
    assert result == "ok"
    snapshot = probe.snapshot()
    assert snapshot["summary_present"] is True
    assert snapshot["summary_chars"] == len(summary)
    assert snapshot["recent_questions_present"] == 2
    assert snapshot["message_count"] == 3

def _turns(count: int, answer_chars: int = 800) -> list[dict]:
    return [
        {
            "turn": index,
            "question": f"第{index}轮的问题",
            "answer": "答" * answer_chars,
        }
        for index in range(1, count + 1)
    ]


def test_chunk_turns_respects_char_budget():
    turns = _turns(5)
    chunks = chunk_turns(turns, max_chars=SUMMARY_CHUNK_CHARS, max_chunks=10)
    assert len(chunks) >= 2
    assert [item["turn"] for chunk in chunks for item in chunk] == [1, 2, 3, 4, 5]


def test_chunk_turns_keeps_newest_chunks_when_over_cap():
    turns = _turns(10)
    chunks = chunk_turns(turns, max_chars=SUMMARY_CHUNK_CHARS, max_chunks=2)
    assert len(chunks) == 2
    covered = [item["turn"] for chunk in chunks for item in chunk]
    assert covered == sorted(covered)
    assert 10 in covered


def test_response_text_accepts_duck_typed_blocks():
    from app.agent.agentscope_runtime import AgentScopeRunner

    response = SimpleNamespace(
        content=[
            SimpleNamespace(type="thinking", text="不应计入"),
            SimpleNamespace(type="text", text="第1轮：要点。"),
            SimpleNamespace(type="text", text="第2轮：要点。"),
        ]
    )
    # thinking blocks carry a `text` attribute as well; only real text is used
    assert AgentScopeRunner._response_text(response) == "第1轮：要点。第2轮：要点。"


def test_summarize_history_chunks_and_records_failures(monkeypatch):
    from app.agent.agentscope_runtime import AgentScopeRunner

    runner = AgentScopeRunner.__new__(AgentScopeRunner)
    runner.settings = SimpleNamespace(llm_model="test-model")
    ctx = SimpleNamespace(
        budget=Budget(timeout_seconds=60, max_tools=0, max_models=50)
    )
    prompts: list[str] = []

    class FakeModel:
        def __init__(self, text: str) -> None:
            self.text = text

        async def __call__(self, messages):
            prompts.append(messages[-1].get_text_content())
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text=self.text)],
                usage=SimpleNamespace(input_tokens=100, output_tokens=20),
                finished_reason="completed",
            )

    texts = iter(["第1轮：讨论A。", "", "第5轮：讨论B。"])
    monkeypatch.setattr(
        runner,
        "_model",
        lambda **kwargs: FakeModel(next(texts, "")),
    )

    summary, audit = asyncio.run(runner._summarize_history(_turns(5), ctx))
    assert len(prompts) == 2
    assert audit["calls"] == 2
    assert audit["covered_turns"] == [1, 2, 3]
    assert audit["failed_turns"] == [4, 5]
    assert summary.startswith("【早期对话摘要（第1-3轮）】")
    assert "第1轮：讨论A。" in summary
    assert ctx.budget.models_used == 2