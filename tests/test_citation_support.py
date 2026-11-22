from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

from app.agent.answer_guard import verify_payload
from app.agent.citation_support import build_citation_support
from app.agent.verify import VerifyResult
from app.api.conversations import _message_payload

def _evidence(
    citation_id,
    channel="web",
    args_hash="",
    title="",
    tool=None,
    url=None,
    publisher=None,
):
    return {
        "citation_id": citation_id,
        "channel": channel,
        "args_hash": args_hash,
        "source_title": title,
        "source_url": url,
        "source_publisher": publisher,
        "tool": tool or ("ifind.stock_financials" if channel == "ifind" else "web.search"),
        "seq": int(citation_id.split("-")[1]),
    }

def _support(answer, evidences):
    return build_citation_support(answer, evidences)

def test_single_ifind_citation_is_single_source():
    groups = _support("营收 1 亿元 [E1-1]。", [_evidence("E1-1", "ifind", "h1")])

    assert groups == [
        {
            "citations": ["E1-1"],
            "independent_sources": 1,
            "single_source": True,
            "sources": [{"key": "ifind:financials", "label": "iFinD 财报", "citations": ["E1-1"]}],
        }
    ]

def test_ifind_plus_web_is_two_independent_sources():
    groups = _support(
        "营收 1 亿元 [E1-1][E1-2]。",
        [
            _evidence("E1-1", "ifind", "h1"),
            _evidence("E1-2", "web", "h2", "某报道", url="https://finance.sina.com.cn/a"),
        ],
    )

    assert groups[0]["independent_sources"] == 2
    assert groups[0]["single_source"] is False

def test_two_ifind_evidences_same_domain_still_one_source():
    groups = _support(
        "营收 1 亿元 [E1-1][E1-2]。",
        [_evidence("E1-1", "ifind", "h1"), _evidence("E1-2", "ifind", "h2")],
    )

    assert groups[0]["independent_sources"] == 1
    assert groups[0]["single_source"] is True
    assert groups[0]["sources"] == [
        {"key": "ifind:financials", "label": "iFinD 财报", "citations": ["E1-1", "E1-2"]}
    ]

def test_ifind_financials_and_news_are_two_sources():
    groups = _support(
        "营收 1 亿元 [E1-1][E1-2]。",
        [
            _evidence("E1-1", "ifind", "h1"),
            _evidence("E1-2", "ifind", "h2", tool="ifind.news_search"),
        ],
    )

    assert groups[0]["independent_sources"] == 2
    assert groups[0]["single_source"] is False
    assert [item["label"] for item in groups[0]["sources"]] == ["iFinD 财报", "iFinD 资讯"]

def test_ifind_financials_and_quotes_are_two_sources():
    groups = _support(
        "营收与股价 [E1-1][E1-2]。",
        [
            _evidence("E1-1", "ifind", "h1"),
            _evidence("E1-2", "ifind", "h2", tool="ifind.stock_quote"),
        ],
    )

    assert groups[0]["independent_sources"] == 2

def test_same_web_site_counts_as_one_source():
    groups = _support(
        "数据 [E1-1][E1-2]。",
        [
            _evidence("E1-1", "web", "hash-a", "报道甲", url="https://finance.sina.com.cn/a"),
            _evidence("E1-2", "web", "hash-b", "报道乙", url="https://sina.com.cn/b"),
        ],
    )

    assert groups[0]["independent_sources"] == 1
    assert groups[0]["sources"] == [
        {"key": "web:sina.com.cn", "label": "新浪财经", "citations": ["E1-1", "E1-2"]}
    ]

def test_different_web_sites_count_as_two_sources():
    groups = _support(
        "数据 [E1-1][E1-2]。",
        [
            _evidence("E1-1", "web", "hash-a", "报道甲", url="https://finance.sina.com.cn/a"),
            _evidence("E1-2", "web", "hash-b", "报道乙", url="https://www.eastmoney.com/b"),
        ],
    )

    assert groups[0]["independent_sources"] == 2
    assert [item["label"] for item in groups[0]["sources"]] == ["新浪财经", "东方财富"]

def test_same_retrieval_without_site_counts_as_one_source():
    groups = _support(
        "数据 [E1-1][E1-2]。",
        [
            _evidence("E1-1", "web", "same-hash", "报道甲"),
            _evidence("E1-2", "web", "same-hash", "报道乙"),
        ],
    )

    assert groups[0]["independent_sources"] == 1

def test_different_retrievals_without_site_count_as_two_sources():
    groups = _support(
        "数据 [E1-1][E1-2]。",
        [
            _evidence("E1-1", "web", "hash-a", "公司发布三季度财报"),
            _evidence("E1-2", "web", "hash-b", "公司发布三季度财报"),
        ],
    )

    assert groups[0]["independent_sources"] == 2

def test_unknown_web_domain_falls_back_to_readable_name():
    groups = _support(
        "数据 [E1-1]。",
        [_evidence("E1-1", "web", "h1", url="https://finance.example.com/a")],
    )

    assert groups[0]["sources"][0]["label"] == "Example"

def test_separate_sentences_are_separate_groups():
    groups = _support(
        "第一处 [E1-1]。\n\n第二处 [E1-2]。",
        [_evidence("E1-1", "ifind", "h1"), _evidence("E1-2", "web", "h2", "报道")],
    )

    assert len(groups) == 2
    assert all(item["single_source"] is True for item in groups)

def test_unknown_citation_is_ignored():
    groups = _support("数据 [E1-9]。", [_evidence("E1-1", "ifind", "h1")])

    assert groups == []

def test_verify_payload_includes_support_only_with_context():
    result = VerifyResult(passed=True, mode="raw")

    assert "citation_support" not in verify_payload(result)

    payload = verify_payload(result, "营收 [E1-1]。", [_evidence("E1-1", "ifind", "h1")])

    assert payload["citation_support"][0]["single_source"] is True
    assert payload["citation_support"][0]["sources"][0]["label"] == "iFinD 财报"

def _evidence_row(seq, channel, args_hash, title, tool=None, url=None, publisher=None):
    now = datetime.now(timezone.utc)
    return SimpleNamespace(
        id=uuid4(),
        seq=seq,
        tool=tool or ("ifind.stock_financials" if channel == "ifind" else "web.search"),
        server="stock" if channel == "ifind" else "web",
        upstream_tool="get_stock_financials",
        args={},
        args_hash=args_hash,
        query_text="测试",
        period_end=None,
        data_as_of=now,
        as_of_label=None,
        fetched_at=now,
        payload={"sections": []},
        facts=[],
        raw_excerpt="片段",
        truncated=False,
        parse_quality=Decimal("0.90"),
        channel=channel,
        source_title=title,
        source_url=url,
        source_publisher=publisher,
        source_published_at=None,
    )

def _message(**overrides):
    now = datetime.now(timezone.utc)
    values = {
        "id": uuid4(),
        "role": "assistant",
        "content": "营收 1 亿元 [E1-1]。",
        "status": "degraded",
        "created_at": now,
        "meta": {"turn": 1, "verify": {"passed": False, "removed_citations": 1}},
    }
    values.update(overrides)
    return SimpleNamespace(**values)

def test_history_payload_backfills_citation_support():
    payload = _message_payload(_message(), [_evidence_row(1, "ifind", "h1", None)])

    assert payload["meta"]["verify"]["citation_support"] == [
        {
            "citations": ["E1-1"],
            "independent_sources": 1,
            "single_source": True,
            "sources": [{"key": "ifind:financials", "label": "iFinD 财报", "citations": ["E1-1"]}],
        }
    ]

def test_history_payload_recomputes_existing_citation_support():
    existing = [
        {"citations": ["E1-1"], "independent_sources": 2, "single_source": False}
    ]
    row = _message(meta={"turn": 1, "verify": {"citation_support": existing}})

    payload = _message_payload(row, [_evidence_row(1, "ifind", "h1", None)])

    assert payload["meta"]["verify"]["citation_support"][0]["independent_sources"] == 1
    assert payload["meta"]["verify"]["citation_support"][0]["sources"][0]["label"] == "iFinD 财报"

def test_history_payload_keeps_existing_support_when_evidence_is_missing():
    existing = [
        {"citations": ["E1-1"], "independent_sources": 2, "single_source": False}
    ]
    row = _message(meta={"turn": 1, "verify": {"citation_support": existing}})

    payload = _message_payload(row, [])

    assert payload["meta"]["verify"]["citation_support"] == existing

def test_history_payload_does_not_invent_verify_block():
    row = _message(meta={"turn": 1})

    payload = _message_payload(row, [_evidence_row(1, "ifind", "h1", None)])

    assert payload["meta"] == {"turn": 1}