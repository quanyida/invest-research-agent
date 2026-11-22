import json

from agentscope.message import Msg, TextBlock, ToolCallBlock, ToolResultBlock
from agentscope.state import AgentState

from app.agent.agent_state_store import compact_agent_state
from app.agent.agentscope_runtime import AgentScopeRunner
from app.agent.answer_guard import guard_answer, verify_payload
from app.agent.context import history_text
from app.agent.evidence_collector import EvidenceCollector
from app.agent.event_bridge import EventBridge
from app.agent.verify import strip_citations, verify_raw_answer
from app.tools.ifind_gateway import ToolOutcome


RAW_ANSWER = (
    "结论：甲公司收入保持增长 [E2-1]。\n"
    "依据：公开报道显示需求稳定 [E2-2]。\n"
    "关键数据：本期收入增长 [E2-1]。\n"
    "待确认：口径与后续变化仍需核验。"
)


def _outcome(evidence: dict) -> ToolOutcome:
    return ToolOutcome(
        status="ok",
        evidence=evidence,
        error_code=None,
        latency_ms=1,
        cached=False,
        query_text="test",
        upstream_calls=[],
        raw_text=str(evidence.get("raw_text") or ""),
    )


def test_raw_verification_accepts_only_current_evidence_ids():
    text, result = verify_raw_answer(RAW_ANSWER, [{"id": "E2-1"}, {"id": "E2-2"}])

    assert text == RAW_ANSWER
    assert result.passed
    assert result.mode == "raw"
    assert result.removed_citations == 0
    assert result.cited_fact_ids == ["E2-1", "E2-2"]


def test_raw_verification_removes_unknown_id_and_keeps_sentence():
    answer = (
        "结论：甲公司收入保持增长 [E2-1]。\n"
        "依据：这条判断来自不存在的证据 [E2-9]。\n"
        "关键数据：本期收入增长 [E2-1]。\n"
        "待确认：口径仍需核验。"
    )
    text, result = verify_raw_answer(answer, [{"id": "E2-1"}])

    assert "[E2-9]" not in text
    assert "这条判断来自不存在的证据" in text
    assert result.removed_citations == 1
    assert result.removed_citation_ids == ["E2-9"]
    assert not result.passed
    assert result.issues == ["已移除 1 条无法核实的引用"]


def test_raw_verification_strips_metadata_inside_brackets():
    answer = (
        "结论：甲公司收入保持增长 [E2-1，新浪财经，2025-12-03，https://example.com/a]。\n"
        "依据：公开报道显示需求稳定 [E2-2]。\n"
        "关键数据：本期收入增长。\n"
        "待确认：口径仍需核验。"
    )
    text, result = verify_raw_answer(answer, [{"id": "E2-1"}, {"id": "E2-2"}])

    assert "[E2-1]" in text
    assert "新浪财经" not in text
    assert "https://example.com/a" not in text
    assert result.normalized_citations == 1
    assert result.removed_citations == 0
    assert result.passed


def test_guard_raw_marks_degraded_when_citations_are_removed():
    answer = (
        "结论：甲公司收入保持增长 [E2-1]。\n"
        "依据：这条判断没有证据 [E2-9]。\n"
        "关键数据：本期收入增长 [E2-1]。\n"
        "待确认：口径仍需核验。"
    )
    result = guard_answer(
        answer,
        [{"id": "E2-1", "facts": []}],
        verify_numbers=False,
    )

    assert "[E2-9]" not in result.answer
    assert result.degraded
    assert not result.verify.passed
    assert verify_payload(result.verify)["removed_citations"] == 1


def test_guard_raw_keeps_clean_answer_passed():
    result = guard_answer(
        RAW_ANSWER,
        [{"id": "E2-1", "facts": []}, {"id": "E2-2", "facts": []}],
        verify_numbers=False,
    )

    assert result.answer == RAW_ANSWER
    assert result.verify.passed
    assert not result.degraded


def test_strip_citations_and_history_text_remove_stale_ids():
    text = "结论：上面提到 [E13-2，新浪财经，https://example.com]。"

    assert strip_citations(text) == "结论：上面提到 。"
    assert "[E13-2]" not in history_text(
        [{"role": "assistant", "content": text}]
    )
    assert "上面提到" in history_text([{"role": "assistant", "content": text}])


def test_history_messages_remove_stale_citations():
    messages = AgentScopeRunner._history_messages(
        [
            {"role": "user", "content": "上面的营销服务具体包括哪些"},
            {"role": "assistant", "content": "结论：包括 A、B [E13-2]。"},
        ]
    )

    assert len(messages) == 2
    assert "[E13-2]" not in messages[1].get_text_content()
    assert "包括 A、B" in messages[1].get_text_content()


def test_compact_state_removes_citations_from_saved_assistant_text():
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
                    output="RAW" * 1000,
                    state="success",
                ),
                TextBlock(text="结论：营销服务包括 A、B [E13-2]。"),
            ],
        )
    )

    compact_agent_state(state)

    saved = state.context[-1].get_text_content()
    assert "营销服务包括 A、B" in saved
    assert "[E13-2]" not in saved


def test_web_search_results_are_split_into_independent_evidence():
    items = [
        {
            "title": "来源一",
            "url": "https://one.example/a",
            "site": "one.example",
            "published_at": "2025-12-01",
            "snippet": "第一条",
        },
        {
            "title": "来源二",
            "url": "https://two.example/b",
            "site": "two.example",
            "published_at": "2025-12-02",
            "snippet": "第二条",
        },
        {
            "title": "来源三",
            "url": "https://three.example/c",
            "site": "three.example",
            "published_at": "2025-12-03",
            "snippet": "第三条",
        },
    ]
    evidence = {
        "raw_text": json.dumps(
            {"query": "测试", "results": items}, ensure_ascii=False
        ),
        "source_items": items,
        "facts": [],
    }

    collector = EvidenceCollector(turn=1)
    primary = collector.add_outcome(
        "web.search", {"query": "测试"}, _outcome(evidence)
    )

    assert len(collector.records) == 3
    assert [record.evidence["id"] for record in collector.records] == [
        "E1-1",
        "E1-2",
        "E1-3",
    ]
    assert all(record.channel == "web" for record in collector.records)
    assert all(record.evidence["channel"] == "web" for record in collector.records)
    assert [record.evidence["source_title"] for record in collector.records] == [
        "来源一",
        "来源二",
        "来源三",
    ]
    assert len({record.split_group for record in collector.records}) == 1
    assert [record.is_primary for record in collector.records] == [True, False, False]
    assert primary.observation.count('call="E1-') == 3
    assert len(collector.audits()) == 1


def test_ifind_news_results_are_split_and_keep_channel():
    items = [
        {
            "资讯标题": "资讯一",
            "资讯内容": "内容一",
            "日期": "2025-12-01",
            "URL": "https://news.example/1",
        },
        {
            "资讯标题": "资讯二",
            "资讯内容": "内容二",
            "日期": "2025-12-02",
            "URL": "https://news.example/2",
        },
    ]
    envelope = {"code": 0, "data": json.dumps(items, ensure_ascii=False)}
    evidence = {
        "raw_text": json.dumps(envelope, ensure_ascii=False),
        "facts": [],
    }

    collector = EvidenceCollector(turn=2)
    collector.add_outcome("ifind.news_search", {"query": "测试"}, _outcome(evidence))

    assert len(collector.records) == 2
    assert [record.evidence["id"] for record in collector.records] == [
        "E2-1",
        "E2-2",
    ]
    assert all(record.channel == "ifind" for record in collector.records)
    assert all(record.evidence["channel"] == "ifind" for record in collector.records)
    assert [record.evidence["source_title"] for record in collector.records] == [
        "资讯一",
        "资讯二",
    ]
    assert len(collector.audits()) == 1


def test_event_bridge_emits_one_tool_end_for_split_group():
    items = [
        {
            "title": "来源一",
            "url": "https://one.example/a",
            "site": "one.example",
            "published_at": "2025-12-01",
            "snippet": "第一条",
        },
        {
            "title": "来源二",
            "url": "https://two.example/b",
            "site": "two.example",
            "published_at": "2025-12-02",
            "snippet": "第二条",
        },
    ]
    evidence = {
        "raw_text": json.dumps(
            {"query": "测试", "results": items}, ensure_ascii=False
        ),
        "source_items": items,
        "facts": [],
    }
    collector = EvidenceCollector(turn=1)
    primary = collector.add_outcome(
        "web.search", {"query": "测试"}, _outcome(evidence)
    )
    bridge = EventBridge(collector)

    events = bridge.record_events(primary)
    names = [name for name, _ in events]

    assert names == ["tool_start", "evidence", "evidence", "tool_end"]
    assert all(
        payload["evidence"]["channel"] == "web"
        for name, payload in events
        if name == "evidence"
    )