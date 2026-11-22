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
