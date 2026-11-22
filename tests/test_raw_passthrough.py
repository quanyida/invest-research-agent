from types import SimpleNamespace

from app.agent.agentscope_runtime import _synthesis_context, _synthesis_prompt
from app.agent.answer_format import answer_is_complete
from app.agent.answer_guard import guard_answer
from app.agent.context import build_agent_system_prompt, degraded_answer
from app.agent.evidence_collector import EvidenceCollector
from app.tools.ifind_gateway import ToolOutcome, _result_payload_text


RAW = '{"code":0,"data":{"answer":"|证券代码|证券简称|\\n|603268.SH|松发股份|"}}'


def _outcome() -> ToolOutcome:
    return ToolOutcome(
        status="ok",
        evidence={
            "as_of_label": "iFinD 原始返回",
            "raw_text": RAW,
            "facts": [],
        },
        error_code=None,
        latency_ms=1,
        cached=False,
        query_text="松发股份",
        upstream_calls=[],
        raw_text=RAW,
    )


def test_raw_observation_passes_content_text_verbatim():
    collector = EvidenceCollector(turn=1)
    record = collector.add_outcome("ifind.stock_financials", {"query": "松发股份"}, _outcome())

    assert RAW in record.observation
    assert 'call="E1-1"' in record.observation
    assert collector.public_evidences()[0]["raw_text"] == RAW


def test_raw_mode_skips_fact_verification():
    result = guard_answer(
        "结论：松发股份的原始数据已返回。\n关键数字：见原始数据。\n正文：未做二次解析。\n边界：口径与缺失：原始返回。分歧：未取到多机构观点。",
        [{"id": "E1-1", "raw_text": RAW, "facts": []}],
        verify_numbers=False,
    )

    assert result.verify.passed
    assert result.verify.mode == "raw"


def test_raw_prompt_tells_agent_to_read_original_payload():
    ctx = SimpleNamespace(
        settings=SimpleNamespace(max_tool_calls=4, ifind_raw_passthrough=True),
        turn=1,
        allowed_tools=["ifind.stock_financials"],
    )
    prompt = build_agent_system_prompt(ctx)

    assert "iFinD 或公开网页搜索的原始数据" in prompt
    assert "未经程序解析" in prompt
    assert "自行阅读原文" in prompt


def test_raw_timeout_digest_uses_original_text():
    answer = degraded_answer([{"id": "E1-1", "raw_text": RAW}], "本次分析超时未完成")

    assert "松发股份" in answer
    assert "未做 fact 级校验" in answer


def test_result_payload_text_keeps_content_order_and_newlines():
    raw = {"result": {"content": [{"text": "A"}, {"text": "B"}]}}

    assert _result_payload_text(raw) == "A\nB"

def test_answer_completeness_requires_four_sections():
    plain = (
        "结论：" + "甲" * 20 + "\n依据：" + "乙" * 20
        + "\n关键数据：" + "丙" * 20 + "\n待确认：" + "丁" * 20
    )
    marked = (
        "## 结论\n" + "甲" * 20 + "\n## 依据\n" + "乙" * 20
        + "\n## 关键数据\n" + "丙" * 20 + "\n## 待确认\n" + "丁" * 20
    )
    assert answer_is_complete(plain)
    assert answer_is_complete(marked)
    assert not answer_is_complete("结论：只有开头")

def test_synthesis_prompt_keeps_question_and_draft_anchor():
    prompt = _synthesis_prompt(
        [{"id": "E1-1", "tool": "ifind.stock_financials", "raw_text": RAW}],
        question="深度分析下安克创新",
        draft="结论：安克创新 2026 年前三季度收入",
    )

    assert "用户问题：深度分析下安克创新" in prompt
    assert "<draft>" in prompt
    assert "不得写其他公司" in prompt
    assert RAW in prompt

def test_synthesis_context_does_not_reuse_tool_history():
    context = _synthesis_context("深度分析下安克创新")

    assert len(context) == 1
    assert context[0].get_text_content() == "深度分析下安克创新"