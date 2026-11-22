from uuid import uuid4

from app.agent.evidence_collector import EvidenceCollector
from app.agent.persist import _evidence_row
from app.tools.ifind_gateway import ToolOutcome


def _outcome() -> ToolOutcome:
    return ToolOutcome("ok", {"facts": [], "raw_text": "{}"}, None, 1, False, "q", [])


def test_outcome_channel_comes_from_tool_registry():
    collector = EvidenceCollector(turn=1)
    record = collector.add_outcome("web.search", {"query": "宁德时代"}, _outcome())
    assert record.channel == "web"
    assert record.evidence["channel"] == "web"
    assert collector.public_evidences()[0]["channel"] == "web"


def test_unknown_tool_degrades_to_unknown_channel():
    collector = EvidenceCollector(turn=1)
    record = collector.add_outcome("legacy.tool", {"query": "x"}, _outcome())
    assert record.channel == "unknown"
    assert collector.public_evidences()[0]["channel"] == "unknown"


def test_error_record_keeps_channel():
    collector = EvidenceCollector(turn=1)
    record = collector.add_error("ifind.stock_quote", {"symbols": ["600519.SH"]}, "TIMEOUT", "超时", 10)
    assert record.channel == "ifind"

def test_evidence_row_persists_channel_and_source_fields():
    row = _evidence_row(
        uuid4(),
        uuid4(),
        uuid4(),
        {
            "seq": 1,
            "tool": "web.search",
            "channel": "web",
            "raw_text": "raw",
            "source_title": "标题",
            "source_url": "https://example.com",
            "source_publisher": "example.com",
            "source_published_at": "2025-11-19T10:00:00+00:00",
        },
    )

    assert row.channel == "web"
    assert row.source_title == "标题"
    assert row.source_url == "https://example.com"
    assert row.source_publisher == "example.com"
    assert row.source_published_at is not None


def test_evidence_row_falls_back_to_unknown_channel():
    row = _evidence_row(uuid4(), uuid4(), uuid4(), {"seq": 1, "channel": "bogus"})

    assert row.channel == "unknown"

def test_source_kind_comes_from_tool_registration():
    collector = EvidenceCollector(turn=1)
    record = collector.add_outcome("ifind.notice_search", {"query": "分红"}, _outcome())

    assert record.evidence["source_kind"] == "announcement"
    assert record.evidence["source_kind_label"] == "公告"
    assert collector.public_evidences()[0]["source_kind_label"] == "公告"