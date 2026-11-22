from types import SimpleNamespace

import pytest

from app.agent.evidence_collector import EvidenceCollector
from app.agent.event_bridge import EventBridge
from app.agent.prefetch import build_prefetch_query, prefetch_web
from app.tools.ifind_gateway import ToolOutcome

def test_build_prefetch_query_strips_leading_noise():
    assert build_prefetch_query("帮我深度分析下 宁德时代 2025Q3 毛利率") == "宁德时代 2025Q3 毛利率"

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
