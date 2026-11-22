from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

from app.api.conversations import _evidence_payload, _message_payload


def _row(channel: str) -> SimpleNamespace:
    now = datetime.now(timezone.utc)
    return SimpleNamespace(
        id=uuid4(),
        seq=1,
        tool="ifind.stock_financials",
        server="stock",
        upstream_tool="get_stock_financials",
        args={},
        args_hash="abc",
        query_text="测试",
        period_end=None,
        data_as_of=now,
        as_of_label="2025Q3",
        fetched_at=now,
        payload={"sections": []},
        facts=[],
        raw_excerpt="片段",
        truncated=False,
        parse_quality=Decimal("0.90"),
        channel=channel,
        source_title=None,
        source_url=None,
        source_publisher=None,
        source_published_at=None,
    )


def test_history_payload_carries_channel_and_label():
    payload = _evidence_payload(_row("ifind"))

    assert payload["channel"] == "ifind"
    assert payload["channel_label"] == "iFinD"
    assert payload["channel_kind"] == "structured"
    assert payload["channel_kind_label"] == "结构化数据"


def test_history_payload_falls_back_for_unknown_channel():
    payload = _evidence_payload(_row("legacy-channel"))

    assert payload["channel"] == "unknown"
    assert payload["channel_label"] == "未知来源"
    assert payload["channel_kind_label"] == "未知来源"

def test_history_payload_carries_citation_id_from_persisted_turn():
    payload = _evidence_payload(_row("ifind"), turn=3)

    assert payload["citation_id"] == "E3-1"

def test_history_payload_does_not_guess_citation_without_turn():
    payload = _evidence_payload(_row("ifind"))

    assert payload["citation_id"] is None

def test_message_payload_reads_turn_from_message_meta():
    now = datetime.now(timezone.utc)
    row = SimpleNamespace(
        id=uuid4(),
        role="assistant",
        content="",
        status="done",
        created_at=now,
        meta={"turn": "4"},
    )

    payload = _message_payload(row, [_row("ifind")])

    assert payload["turn"] == 4
    assert payload["evidence"][0]["citation_id"] == "E4-1"
