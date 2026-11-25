from __future__ import annotations

import pytest

from app.agent.answer_format import answer_is_complete, has_all_sections, section_body
from app.agent.answer_guard import extract_answer
from app.agent.verify import verify_answer, verify_raw_answer

FACTS = [
    {
        'fact_id': 'E1-1',
        'entity': '宁德时代',
        'period': '2025-09-30',
        'metric': '销售毛利率',
        'value': '25.3098',
        'unit': '%',
    },
    {
        'fact_id': 'E1-6',
        'entity': '宁德时代',
        'period': '2025-09-30',
        'metric': '销售毛利率同比变化',
        'value': '-2.8752',
        'unit': '个百分点',
    },
]
EVIDENCES = [{'facts': FACTS}]

CONSISTENT_FORMS = [
    ('## 结论', '## 依据', '## 关键数据', '## 待确认'),
    ('**结论：**', '**依据：**', '**关键数据：**', '**待确认：**'),
    ('**结论**：', '**依据**：', '**关键数据**：', '**待确认**：'),
    ('### 结论：', '### 依据：', '### 关键数据：', '### 待确认：'),
]


def build_answer(heads: tuple[str, str, str, str]) -> str:
    conclusion, evidence, numbers, pending = heads
    return (
        f'{conclusion}\n'
        '销售毛利率为 25.31% [E1-1]，同比下降 2.88 个百分点 [E1-6]。\n'
        f'{evidence}\n'
        '1. 本期毛利率为 25.31% [E1-1]。\n'
        '2. 同比变化为 -2.88 个百分点 [E1-6]。\n'
        f'{numbers}\n'
        '销售毛利率 25.31% [E1-1]；同比 -2.88 个百分点 [E1-6]。\n'
        f'{pending}\n'
        '本轮未发现明显缺口。'
    )


@pytest.mark.parametrize('heads', CONSISTENT_FORMS)
def test_four_markdown_forms_agree_between_completeness_and_verify(heads):
    answer = build_answer(heads)
    assert has_all_sections(answer)
    result = verify_answer(answer, EVIDENCES)
    assert result.passed, result.issues
    assert not any(issue.startswith('缺少四段格式') for issue in result.issues)


@pytest.mark.parametrize('heads', CONSISTENT_FORMS)
def test_markdown_heads_do_not_leak_markers_into_body(heads):
    answer = build_answer(heads)
    body = section_body(answer, 0)
    assert body is not None
    assert body.lstrip().startswith('销售毛利率')


@pytest.mark.parametrize('head', [head[0] for head in CONSISTENT_FORMS])
def test_extract_answer_trims_leading_chatter_for_markdown_heads(head):
    raw = f'我先核对一下数据。\n{head}\n销售毛利率为 25.31% [E1-1]。'
    extracted = extract_answer(raw)
    assert '核对' not in extracted
    assert '销售毛利率为 25.31%' in extracted


def test_plain_legacy_answer_still_passes():
    answer = (
        '结论：销售毛利率为 25.31% [E1-1]，同比下降 2.88 个百分点 [E1-6]。\n'
        '关键数字：25.3098% [E1-1]；-2.8752 个百分点 [E1-6]。\n'
        '正文：毛利率同比下降 [E1-6]。\n'
        '边界：口径与缺失：累计口径，单季数据未取到。'
    )
    assert verify_answer(answer, EVIDENCES).passed

def test_answer_without_sections_passes_with_content_and_citation():
    answer = (
        "## 毛利率承压\n"
        "宁德时代 2025 年三季度销售毛利率为 25.31% [E1-1]，同比下降 2.88 个百分点 [E1-6]。\n"
        "- 毛利率仍处高位，但同比走弱 [E1-6]。\n"
        "> 口径为累计值。\n"
        "综合看，盈利能力边际承压，具体原因需结合后续披露确认。"
    )
    assert answer_is_complete(answer)
    result = verify_answer(answer, EVIDENCES)
    assert result.passed, result.issues
    assert not any("四段" in issue for issue in result.issues)


def test_answer_without_sections_still_requires_a_citation():
    answer = (
        "## 毛利率承压\n"
        "宁德时代 2025 年三季度毛利率同比走弱，盈利能力边际承压，具体原因需要结合后续披露继续确认。"
    )
    result = verify_answer(answer, EVIDENCES)
    assert not result.passed
    assert any(issue.startswith("answer_has_no_citation") for issue in result.issues)


def test_answer_without_sections_still_rejects_unknown_citation():
    answer = "毛利率同比走弱 [E1-99]，需要结合后续披露继续确认，短期难以判断拐点是否出现。"
    result = verify_answer(answer, EVIDENCES)
    assert not result.passed
    assert any("E1-99" in issue for issue in result.issues)


def test_short_answer_without_sections_is_incomplete():
    assert not answer_is_complete("太短了")


def test_raw_mode_without_sections_still_requires_a_citation():
    answer = (
        "宁德时代 2025 年三季度销售毛利率同比走弱，盈利能力边际承压，"
        "具体原因需要结合后续披露和行业景气变化继续确认。"
    )
    _, result = verify_raw_answer(answer, [{"id": "E1-1"}])
    assert not result.passed
    assert any(issue.startswith("answer_has_no_citation") for issue in result.issues)


def test_raw_mode_without_sections_passes_with_citation():
    answer = (
        "宁德时代 2025 年三季度销售毛利率同比走弱 [E1-1]，盈利能力边际承压，"
        "具体原因需要结合后续披露和行业景气变化继续确认。"
    )
    _, result = verify_raw_answer(answer, [{"id": "E1-1"}])
    assert result.passed, result.issues
