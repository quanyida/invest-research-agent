from types import SimpleNamespace

from app.agent.evidence_collector import public_evidence_payload
from app.agent.event_bridge import EventBridge


def test_public_payload_drops_full_raw_text_and_has_channel():
    payload = public_evidence_payload(
        {
            "id": "E1",
            "channel": "web",
            "facts": [{"fact_id": "E1-1"}],
            "raw_text": "x" * 5000,
            "raw_excerpt": "",
        }
    )

    assert "raw_text" not in payload
    assert len(payload["raw_excerpt"]) == 1200
    assert payload["channel"] == "web"
    assert payload["channel_label"] == "网页"
    assert payload["channel_kind"] == "search"
    assert payload["facts"] == [{"fact_id": "E1-1"}]
    assert payload["facts_total"] == 1


def test_public_payload_degrades_unknown_channel():
    payload = public_evidence_payload({"channel": "no-such-channel", "facts": []})

    assert payload["channel"] == "unknown"
    assert payload["channel_label"] == "未知来源"
    assert payload["channel_kind_label"] == "未知来源"
    assert payload["source_kind_label"] == "未知来源"


def test_event_bridge_uses_sanitized_payload():
    bridge = EventBridge(collector=SimpleNamespace(records=[]))
    record = SimpleNamespace(
        seq=1,
        tool="web.search",
        status="ok",
        latency_ms=1,
        cached=False,
        error_code=None,
        evidence={"id": "E2", "channel": "ifind", "raw_text": "y" * 3000, "facts": []},
        emitted=False,
    )
    payload = dict(bridge.record_events(record))["evidence"]["evidence"]

    assert payload["channel"] == "ifind"
    assert payload["channel_label"] == "iFinD"
    assert "raw_text" not in payload
    assert len(payload["raw_excerpt"]) == 1200

def test_public_payload_keeps_tool_source_kind():
    payload = public_evidence_payload(
        {"channel": "ifind", "source_kind": "announcement", "source_kind_label": "公告", "facts": []}
    )

    assert payload["source_kind"] == "announcement"
    assert payload["source_kind_label"] == "公告"