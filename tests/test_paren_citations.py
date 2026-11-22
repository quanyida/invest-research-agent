from app.agent.answer_guard import guard_answer
from app.agent.verify import strip_citations, verify_answer, verify_raw_answer


def _sections(body_conclusion: str, body_evidence: str) -> str:
    return (
        f"结论：{body_conclusion}\n"
        f"依据：{body_evidence}\n"
        "关键数据：见上。\n"
        "待确认：口径仍需核验。"
    )


def test_raw_accepts_fullwidth_paren_and_normalizes():
    answer = _sections("公司毛利率为 30%（E2-1）。", "公开报道显示需求稳定（E2-1）。")

    text, result = verify_raw_answer(answer, [{"id": "E2-1"}])

    assert "（E2-1）" not in text
    assert text.count("[E2-1]") == 2
    assert result.passed
    assert result.removed_citations == 0
    assert result.normalized_citations == 2
    assert result.cited_fact_ids == ["E2-1"]


def test_raw_accepts_halfwidth_paren_and_normalizes():
    answer = _sections("公司毛利率为 30%(E2-1)。", "公开报道显示需求稳定 [E2-1]。")

    text, result = verify_raw_answer(answer, [{"id": "E2-1"}])

    assert "(E2-1)" not in text
    assert result.passed
    assert result.normalized_citations == 1


def test_raw_removes_unknown_fullwidth_citation():
    answer = _sections("公司毛利率为 30%（E2-1）。", "这条判断来自不存在的证据（E2-9）。")

    text, result = verify_raw_answer(answer, [{"id": "E2-1"}])

    assert "（E2-9）" not in text
    assert "这条判断来自不存在的证据" in text
    assert result.removed_citations == 1
    assert result.removed_citation_ids == ["E2-9"]
    assert result.issues == ["已移除 1 条无法核实的引用"]
    assert not result.passed


def test_guard_marks_degraded_for_unknown_fullwidth_citation():
    answer = _sections("公司毛利率为 30%（E2-1）。", "这条判断来自不存在的证据（E2-9）。")

    guarded = guard_answer(answer, [{"id": "E2-1", "facts": []}], verify_numbers=False)

    assert "E2-9" not in guarded.answer
    assert "[E2-1]" in guarded.answer
    assert guarded.degraded
    assert not guarded.verify.passed


def test_mixed_forms_in_one_group_share_the_same_rules():
    answer = _sections("公司毛利率为 30%（E2-1、E2-9）。", "公开报道显示需求稳定（E2-1）。")

    text, result = verify_raw_answer(answer, [{"id": "E2-1"}])

    assert "[E2-1]" in text
    assert "E2-9" not in text
    assert result.removed_citations == 1
    assert result.cited_fact_ids == ["E2-1"]


def test_plain_prose_parentheses_are_not_treated_as_citations():
    answer = _sections(
        "公司的主营业务（E系列产品线）保持稳定（详见下表）。",
        "公开报道显示需求稳定（资料来源：年报）。",
    )

    text, result = verify_raw_answer(answer, [{"id": "E2-1"}])

    assert "（E系列产品线）" in text
    assert "（详见下表）" in text
    assert "（资料来源：年报）" in text
    assert result.removed_citations == 0
    assert result.normalized_citations == 0


def test_strip_citations_removes_fullwidth_form_from_history():
    text = "结论：上面提到（E13-2），另有（E13-3、E13-4）。"

    stripped = strip_citations(text)

    assert "E13-" not in stripped
    assert "上面提到" in stripped


def test_fact_mode_recognises_fullwidth_citation():
    answer = (
        "结论：公司毛利率为 30%（E1-1）。\n"
        "依据：年报显示毛利率提升 [E1-1]。\n"
        "关键数据：毛利率 30% [E1-1]。\n"
        "待确认：口径仍需核验。"
    )
    evidences = [
        {"id": "E1-1", "facts": [{"fact_id": "E1-1", "value": 30, "unit": "%"}]}
    ]

    result = verify_answer(answer, evidences)

    assert result.cited_fact_ids == ["E1-1"]
    assert result.passed