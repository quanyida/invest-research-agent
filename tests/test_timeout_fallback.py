from app.agent.agentscope_runtime import _timeout_fallback
from app.agent.verify import verify_answer

FACTS = [
    {
        "fact_id": "E1-1",
        "entity": "安克创新",
        "period": "2026-09-30",
        "metric": "营业收入",
        "value": "261.40",
        "unit": "亿元",
    }
]
EVIDENCES = [{"facts": FACTS}]


def test_model_timeout_fallback_returns_verifiable_digest():
    answer, source, state = _timeout_fallback(
        EVIDENCES,
        model_timeout=True,
    )

    assert source == "model_timeout"
    assert state == "MODEL_TIMEOUT"
    assert "模型调用超时未完成" in answer
    assert verify_answer(answer, EVIDENCES).passed


def test_task_timeout_fallback_returns_verifiable_digest():
    answer, source, state = _timeout_fallback(
        EVIDENCES,
        model_timeout=False,
    )

    assert source == "timeout"
    assert state == "TIMEOUT"
    assert "本次分析超时未完成" in answer
    assert verify_answer(answer, EVIDENCES).passed

RAW_WEB = (
    '{"provider":"tavily","results":[{"title":"订阅会员打八折",'
    '"snippet":"扫码加群，限时优惠"}]}'
)


def test_timeout_fallback_without_facts_reports_counts_and_hides_raw_payload():
    answer, source, state = _timeout_fallback(
        [
            {"id": "E1-1", "channel": "web", "source_kind": "search", "raw_text": RAW_WEB},
            {"id": "E1-2", "channel": "ifind", "source_kind": "financials", "raw_text": "{}"},
        ],
        model_timeout=False,
    )

    assert source == "timeout"
    assert state == "TIMEOUT"
    assert "已获取 2 条数据" in answer
    assert "iFinD 1 条" in answer
    assert "网页 1 条" in answer
    assert "建议缩小问题范围后重试" in answer
    assert "拆分标的" in answer
    assert len(answer) >= 80
    for marker in ("{", '"provider"', '"results"', "raw_text", "订阅", "打八折", "扫码"):
        assert marker not in answer
    for section in ("结论：", "依据：", "关键数据：", "待确认："):
        assert section in answer