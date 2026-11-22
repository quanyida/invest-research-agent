from types import SimpleNamespace

from app.agent.context import SYSTEM_PROMPT, build_agent_system_prompt, build_repair_messages
from app.agent.verify import verify_answer

FACTS = [
    {"fact_id": "E1-1", "entity": "宁德时代", "period": "2025-09-30", "metric": "销售毛利率", "value": "25.3098", "unit": "%"},
    {"fact_id": "E1-6", "entity": "宁德时代", "period": "2025-09-30", "metric": "销售毛利率同比变化", "value": "-2.8752", "unit": "个百分点"},
]
EVIDENCES = [{"facts": FACTS}]


def test_prompt_requires_new_four_sections():
    for label in ("结论：", "依据：", "关键数据：", "待确认："):
        assert label in SYSTEM_PROMPT


def test_prompt_defines_company_and_industry_skeletons():
    for head in ("核心逻辑：", "业务与财务：", "驱动与催化：", "可比与估值：", "跟踪指标："):
        assert head in SYSTEM_PROMPT
    for head in ("行业全景：", "商业模式：", "政策环境：", "产业链：", "空间测算：", "竞争格局：", "核心标的："):
        assert head in SYSTEM_PROMPT


def test_agent_prompt_mentions_task_templates_and_two_line_boundary():
    ctx = SimpleNamespace(
        settings=SimpleNamespace(max_tool_calls=4),
        turn=2,
        allowed_tools=["ifind.stock_financials"],
    )
    prompt = build_agent_system_prompt(ctx)
    assert "依据：" in prompt
    assert "关键数据：" in prompt
    assert "待确认：" in prompt
    assert "小标题" in prompt


def test_repair_prompt_targets_two_line_boundary():
    messages = build_repair_messages("问题", EVIDENCES, "初稿", ["缺引用"])
    assert "待确认：" in messages[-1]["content"]


def test_legacy_answer_still_passes_verification():
    answer = (
        "结论：销售毛利率为 25.31% [E1-1]，同比下降 2.88 个百分点 [E1-6]。\n"
        "关键数字：25.3098% [E1-1]；-2.8752 个百分点 [E1-6]。\n"
        "正文：毛利率同比下降 [E1-6]。\n"
        "边界：口径与缺失：累计口径，单季数据未取到。分歧：未取到多机构观点。"
    )
    result = verify_answer(answer, EVIDENCES)
    assert result.passed, result.issues

def test_new_answer_format_passes_verification():
    answer = (
        "结论：销售毛利率为 25.31% [E1-1]，同比下降 2.88 个百分点 [E1-6]。\n"
        "依据：\n1. 本期毛利率为 25.31% [E1-1]。\n2. 同比变化为 -2.88 个百分点 [E1-6]。\n"
        "关键数据：销售毛利率 25.31% [E1-1]；同比 -2.88 个百分点 [E1-6]。\n"
        "待确认：本轮未发现明显缺口。"
    )
    result = verify_answer(answer, EVIDENCES)
    assert result.passed, result.issues
