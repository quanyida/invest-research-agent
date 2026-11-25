from types import SimpleNamespace

import pytest

from app.agent.evidence_collector import EvidenceCollector
from app.agent.event_bridge import EventBridge
from app.agent.prefetch import build_prefetch_query, prefetch_web
from app.tools.ifind_gateway import ToolOutcome

@pytest.mark.parametrize(
    ("question", "expected"),
    [
        (
            "对比分析下瑞芯微和恒玄科技在端侧AI芯片的各方面，多渠道交叉验证",
            "瑞芯微 恒玄科技 端侧AI芯片",
        ),
        ("帮我深度分析下 宁德时代 2025Q3 毛利率", "宁德时代 2025Q3 毛利率"),
        (
            "总结一下新能源汽车行业最近的政策和销量",
            "新能源汽车行业最近 政策 销量",
        ),
        ("分析下 AAPL 2025Q3 财报", "AAPL 2025Q3 财报"),
        ("查一下 2025年 新能源 汽车 销量", "2025年 新能源 汽车 销量"),
        ("深度分析", "深度分析"),
    ],
)
def test_build_prefetch_query_extracts_keywords(question, expected):
    assert build_prefetch_query(question) == expected


def test_build_prefetch_query_falls_back_when_cleaning_is_too_short():
    assert build_prefetch_query("详细看看") == "详细看看"


def test_build_prefetch_query_deduplicates_tokens():
    assert build_prefetch_query("分析下 宁德时代 宁德时代 财报") == "宁德时代 财报"

def test_record_events_marks_prefetch_as_emitted():
    collector = EvidenceCollector(turn=1)
    record = collector.add_outcome(
        "web.search",
        {"query": "宁德时代"},
        ToolOutcome("ok", {"raw_text": "{}", "facts": []}, None, 1, False, "宁德时代", []),
    )
    events = EventBridge(collector).record_events(record)

    assert [name for name, _ in events] == ["tool_start", "evidence", "tool_end"]
    assert collector.drain() == []

@pytest.mark.asyncio
async def test_prefetch_skips_when_web_search_disabled():
    ctx = SimpleNamespace(
        settings=SimpleNamespace(web_search_ready=False),
        allowed_tools=["web.search"],
        run=SimpleNamespace(question="宁德时代", tenant_id=None, user_id=None),
        collector=EvidenceCollector(turn=1),
        prefetch_block="",
        prefetch_info=None,
    )
    record = await prefetch_web(ctx)

    assert record is None
    assert ctx.prefetch_info == {"status": "skipped", "reason": "web_search_unavailable"}
