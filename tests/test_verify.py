from app.agent.verify import verify_answer


from app.agent.verify import repair_citations
from app.agent.context import degraded_answer

FACTS = [
    {"fact_id": "E1-1", "entity": "宁德时代", "period": "2025-09-30", "metric": "销售毛利率", "value": "25.3098", "unit": "%"},
    {"fact_id": "E1-5", "entity": "宁德时代", "period": "2024-09-30", "metric": "销售毛利率", "value": "28.185", "unit": "%"},
    {"fact_id": "E1-6", "entity": "宁德时代", "period": "2025-09-30", "metric": "销售毛利率同比变化", "value": "-2.8752", "unit": "个百分点"},
]
EVIDENCES = [{"facts": FACTS}]


def test_negative_direction_and_restatement_pass():
    answer = (
        "结论：销售毛利率为 25.31% [E1-1]，同比下降 2.88 个百分点 [E1-6]。\n"
        "关键数字：25.3098% [E1-1]；28.185% [E1-5]；-2.8752 个百分点 [E1-6]。\n"
        "正文：本期比对比期低 2.8752 个百分点 [E1-6]。\n"
        "边界：累计口径。"
    )
    result = verify_answer(answer, EVIDENCES)
    assert result.passed, result.issues
    assert result.unverified == []


def test_conclusion_without_citation_fails():
    answer = (
        "结论：销售毛利率为 25.31%，同比下降 2.88 个百分点。\n"
        "关键数字：25.3098% [E1-1]；28.185% [E1-5]；-2.8752 个百分点 [E1-6]。\n"
        "正文：本期比对比期低 2.8752 个百分点 [E1-6]。\n"
        "边界：累计口径。"
    )
    result = verify_answer(answer, EVIDENCES)
    assert not result.passed
    assert any("结论段" in issue for issue in result.issues)


def test_fabricated_number_fails():
    answer = (
        "结论：销售毛利率为 25.31% [E1-1]。\n"
        "关键数字：25.3098% [E1-1]。\n"
        "正文：毛利率为 99.99%。\n"
        "边界：累计口径。"
    )
    result = verify_answer(answer, EVIDENCES)
    assert not result.passed
    assert any("99.99" in issue for issue in result.issues)


def test_missing_fact_reference_fails():
    answer = (
        "结论：销售毛利率为 25.31% [E9-9]。\n"
        "关键数字：无可引用数字。\n"
        "正文：无。\n"
        "边界：累计口径。"
    )
    result = verify_answer(answer, EVIDENCES)
    assert not result.passed
    assert any("E9-9" in issue for issue in result.issues)

EXTRA_FACTS = FACTS + [
    {"fact_id": "E1-7", "entity": "宁德时代", "period": "2025-09-30", "metric": "总股本", "value": "46.2725", "unit": "亿股"},
    {"fact_id": "E1-8", "entity": "宁德时代", "period": "2025-09-30", "metric": "股东持股数量", "value": "6000.0607", "unit": "万股"},
    {"fact_id": "E1-9", "entity": "宁德时代", "period": "2025-09-30", "metric": "股东持股比例", "value": "40.38", "unit": "%"},
    {"fact_id": "E1-10", "entity": "宁德时代", "period": "2025-09-30", "metric": "净利润同比", "value": "-7.6803", "unit": "%"},
    {"fact_id": "E1-11", "entity": "宁德时代", "period": "2025-09-30", "metric": "净利润同比", "value": "36.2018", "unit": "%"},
    {"fact_id": "E1-12", "entity": "比亚迪", "period": "2025-09-30", "metric": "净利润", "value": "242.3166", "unit": "亿元"},
]
EXTRA_EVIDENCES = [{"facts": EXTRA_FACTS}]

def test_share_units_and_noise_masking_pass():
    answer = (
        "结论：总股本为 46.27 亿股 [E1-7]，第十名股东持股 0.60 亿股 [E1-8]，占比 40.38% [E1-9]。\n"
        "关键数字：46.2725 亿股 [E1-7]；6000.0607 万股 [E1-8]；40.38% [E1-9]。\n"
        "正文：20260916 解禁，上年同月=100，第10名股东未变化；净利润同比下降 7.68% [E1-10]。\n"
        "边界：累计口径。"
    )
    result = verify_answer(answer, EXTRA_EVIDENCES)
    assert result.passed, result.issues

def test_explicit_negative_sign_is_not_flipped():
    answer = (
        "结论：净利润同比下降 -7.68% [E1-10]。\n"
        "关键数字：-7.6803% [E1-10]。\n"
        "正文：无。\n"
        "边界：累计口径。"
    )
    result = verify_answer(answer, EXTRA_EVIDENCES)
    assert result.passed, result.issues

def test_internal_diagnostic_text_is_not_reverified():
    answer = (
        "结论：销售毛利率为 25.31% [E1-1]。\n"
        "关键数字：25.3098% [E1-1]。\n"
        "正文：无。\n"
        "边界：事实校验未通过：数字 99.99 与 [E1-1] 的 25.3098 不一致；数字 4 后面没有 fact 引用。"
    )
    result = verify_answer(answer, EVIDENCES)
    assert result.passed, result.issues

def test_integer_percentage_rounding_passes():
    answer = (
        "结论：净利润同比增长 36% [E1-11]。\n"
        "关键数字：36.2018% [E1-11]。\n"
        "正文：无。\n"
        "边界：累计口径。"
    )
    result = verify_answer(answer, EXTRA_EVIDENCES)
    assert result.passed, result.issues

def test_direction_word_does_not_flip_amount():
    answer = (
        "结论：净利润同比下降，但净利润为 242.32 亿元 [E1-12]。\n"
        "关键数字：242.3166 亿元 [E1-12]。\n"
        "正文：无。\n"
        "边界：累计口径。"
    )
    result = verify_answer(answer, EXTRA_EVIDENCES)
    assert result.passed, result.issues

def test_unicode_minus_is_normalized():
    answer = (
        "结论：净利润同比 −7.68% [E1-10]。\n"
        "关键数字：-7.6803% [E1-10]。\n"
        "正文：无。\n"
        "边界：累计口径。"
    )
    result = verify_answer(answer, EXTRA_EVIDENCES)
    assert result.passed, result.issues

REPAIR_FACTS = [
    {"fact_id": "E1-1", "entity": "贵州茅台", "period": "2025-09-30", "metric": "销售净利率", "value": "52.0801", "unit": "%"},
    {"fact_id": "E1-2", "entity": "贵州茅台", "period": "2025-09-30", "metric": "净利润", "value": "668.988", "unit": "亿元"},
    {"fact_id": "E1-3", "entity": "贵州茅台", "period": "2025-09-30", "metric": "归母净利润同比", "value": "6.2458", "unit": "%"},
    {"fact_id": "E1-5", "entity": "贵州茅台", "period": "2025-09-30", "metric": "净利润同比", "value": "6.1356", "unit": "%"},
]
REPAIR_EVIDENCES = [{"facts": REPAIR_FACTS}]

def test_misaligned_citation_is_repaired():
    answer = (
        "结论：销售净利率为 52.08% [E1-1]。\n"
        "关键数字：净利润 668.99 亿元 [E1-2]。\n"
        "正文：净利润同比增长 6.14% [E1-2]。\n"
        "边界：累计口径。"
    )
    repaired, count = repair_citations(answer, REPAIR_EVIDENCES)
    assert count == 1
    assert "[E1-5]" in repaired
    result = verify_answer(repaired, REPAIR_EVIDENCES)
    assert result.passed, result.issues

def test_missing_citation_is_added_when_unique():
    answer = (
        "结论：销售净利率为 52.08% [E1-1]。\n"
        "关键数字：净利润同比增长 6.14%。\n"
        "正文：无。\n"
        "边界：累计口径。"
    )
    repaired, count = repair_citations(answer, REPAIR_EVIDENCES)
    assert count == 1
    assert "6.14% [E1-5]" in repaired
    result = verify_answer(repaired, REPAIR_EVIDENCES)
    assert result.passed, result.issues

def test_ambiguous_number_is_not_auto_cited():
    facts = [
        {"fact_id": "E1-1", "entity": "A", "period": "2025-09-30", "metric": "同比", "value": "1.0", "unit": "%"},
        {"fact_id": "E1-2", "entity": "B", "period": "2025-09-30", "metric": "同比", "value": "1.0", "unit": "%"},
    ]
    answer = "结论：同比为 1%。\n关键数字：无。\n正文：无。\n边界：无。"
    repaired, count = repair_citations(answer, [{"facts": facts}])
    assert count == 0
    assert repaired == answer
    result = verify_answer(repaired, [{"facts": facts}])
    assert not result.passed

def test_numbered_list_marker_is_ignored():
    answer = (
        "结论：销售净利率为 52.08% [E1-1]。\n"
        "关键数字：\n1. 净利润 668.99 亿元 [E1-2]。\n2. 同比增长 6.14% [E1-5]。\n"
        "正文：无。\n"
        "边界：累计口径。"
    )
    result = verify_answer(answer, REPAIR_EVIDENCES)
    assert result.passed, result.issues

def test_trailing_citation_block_is_not_misrewritten():
    facts = [
        {"fact_id": "E1-2", "entity": "宁德时代", "period": "2025-09-30", "metric": "持股数量", "value": "10.197", "unit": "亿股"},
        {"fact_id": "E1-3", "entity": "宁德时代", "period": "2025-09-30", "metric": "持股比例", "value": "22.04", "unit": "%"},
        {"fact_id": "E1-22", "entity": "宁德时代", "period": "2025-09-30", "metric": "合计比例", "value": "64.69", "unit": "%"},
        {"fact_id": "E1-23", "entity": "宁德时代", "period": "2025-09-30", "metric": "合计数量", "value": "29.927", "unit": "亿股"},
    ]
    answer = (
        "结论：合计持股 29.927 亿股、合计比例 64.69% [E1-23][E1-22]。\n"
        "关键数字：第1名持股 10.197 亿股、比例 22.04% [E1-2][E1-3]。\n"
        "正文：无。\n"
        "边界：累计口径。"
    )
    repaired, count = repair_citations(answer, [{"facts": facts}])
    assert count == 0
    assert repaired == answer
    result = verify_answer(repaired, [{"facts": facts}])
    assert result.passed, result.issues

def test_chinese_date_is_masked():
    facts = [
        {"fact_id": "E1-1", "entity": "宁德时代", "period": "2026-09-16", "metric": "解禁日期", "value": "2026-09-16", "unit": "text"},
        {"fact_id": "E1-2", "entity": "宁德时代", "period": "2026-09-30", "metric": "流通数量", "value": "404432", "unit": "股"},
    ]
    answer = (
        "结论：解禁日期为 2026年9月16日 [E1-1]。\n"
        "关键数字：本期流通数量 404432 股 [E1-2]。\n"
        "正文：无。\n"
        "边界：累计口径。"
    )
    result = verify_answer(answer, [{"facts": facts}])
    assert result.passed, result.issues

def test_wrong_sign_is_repaired_against_cited_fact():
    facts = [
        {"fact_id": "E1-5", "entity": "比亚迪", "period": "2025-09-30", "metric": "净利润同比", "value": "-7.6803", "unit": "%"},
    ]
    answer = (
        "结论：净利润同比增长 7.68% [E1-5]。\n"
        "关键数字：-7.6803% [E1-5]。\n"
        "正文：无。\n"
        "边界：累计口径。"
    )
    repaired, count = repair_citations(answer, [{"facts": facts}])
    assert count == 1
    assert "增长 -7.68%" in repaired
    result = verify_answer(repaired, [{"facts": facts}])
    assert result.passed, result.issues

def test_per_share_phrase_is_ignored():
    facts = [
        {"fact_id": "E1-1", "entity": "宁德时代", "period": "2026-09-30", "metric": "年度累计单位分红", "value": "1.411", "unit": "元"},
    ]
    answer = (
        "结论：每 10 股派息 1.411 元 [E1-1]。\n"
        "关键数字：年度累计单位分红 1.411 元 [E1-1]。\n"
        "正文：无。\n"
        "边界：累计口径。"
    )
    result = verify_answer(answer, [{"facts": facts}])
    assert result.passed, result.issues

def test_rank_range_is_ignored():
    answer = (
        "结论：第1至第3名股东性质均为流通A股 [E1-1]。\n"
        "关键数字：无可引用数字。\n"
        "正文：无。\n"
        "边界：累计口径。"
    )
    facts = [{"fact_id": "E1-1", "entity": "宁德时代", "period": "", "metric": "数据", "value": "第1至第3名股东性质=流通A股", "unit": "text"}]
    result = verify_answer(answer, [{"facts": facts}])
    assert result.passed, result.issues

def test_degraded_answer_is_verifiable():
    facts = [
        {"fact_id": "E1-1", "entity": "宁德时代", "period": "2025-09-30", "metric": "销售毛利率", "value": "25.3098", "unit": "%"},
        {"fact_id": "E1-2", "entity": "宁德时代", "period": "2025-09-30", "metric": "数据", "value": "证券代码=300750.SZ", "unit": "text"},
    ]
    answer = degraded_answer([{"facts": facts}], "模型调用失败：TIMEOUT")
    result = verify_answer(answer, [{"facts": facts}])
    assert result.passed, result.issues
