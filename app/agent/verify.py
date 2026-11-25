from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from app.agent.answer_format import (
    LEGACY_SECTION_PATTERNS,
    section_body,
)


CITATION_OPEN = r"[\[（(]"
CITATION_CLOSE = r"[\]）)]"
CITATION_RE = re.compile(rf"{CITATION_OPEN}E\d+-\d+{CITATION_CLOSE}")
CITATION_BLOCK_RE = re.compile(
    rf"(?:\s*[，,、;；]?\s*{CITATION_OPEN}E\d+-\d+{CITATION_CLOSE})+"
)
CITATION_LIKE_RE = re.compile(
    r"\[E[^\]\n]{0,240}\]"
    r"|[（(]\s*E\d+-\d+(?:\s*[，,、;；]?\s*E\d+-\d+)*\s*[）)]"
)
CITATION_ID_RE = re.compile(r"E(\d+)-(\d+)")
NUMBER_RE = re.compile(
    r"(?<![\w.])([-−－]?\d+(?:\.\d+)?)\s*"
    r"(%|％|亿元|万元|元|亿股|万股|倍|股|个百分点|个基点|bp|BP|个月|月份|月|年|天|周|页|名|大|条|项)?"
)
DATE_RE = re.compile(
    r"20\d{2}\s*[-/年]\s*\d{1,2}\s*[-/月]\s*\d{1,2}"
    r"|20\d{2}\s*[QqHh][1-4]"
    r"|(?<!\d)20\d{2}(?!\d)\s*年?"
)
CN_DATE_RE = re.compile(r"20\d{2}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日?")
CN_MONTH_DAY_RE = re.compile(r"\d{1,2}\s*月\s*\d{1,2}\s*日")
COMPACT_DATE_RE = re.compile(r"(?<!\d)20\d{6}(?!\d)")
DIAGNOSTIC_RE = re.compile(r"(?m)^(?:边界|待确认)：\s*(?:事实校验未通过|模型调用失败)[^\n]*")
BASE_INDEX_RE = re.compile(r"上年(?:同月|同期)?\s*=\s*100")
CODE_RE = re.compile(r"(?<!\d)[0368]\d{5}(?!\d)")
PAGE_RE = re.compile(r"第\s*\d+\s*[页条项名大]|前\s*\d+\s*[大名人]")
RANK_RANGE_RE = re.compile(r"(?:第|前)\s*\d+\s*(?:至|-|~|—)\s*\d+\s*[名大]")
MONTH_RE = re.compile(r"(?<!\d)\d{1,2}\s*月份?(?!\d)")
QUARTER_RE = re.compile(r"(?<![A-Za-z0-9])[QqHh][1-4](?![0-9])")
LIST_RE = re.compile(r"(?m)^\s*(?:\d{1,2}\s*[.、)）]|[（(]\d{1,2}[）)])\s*")
PER_SHARE_RE = re.compile(r"每\s*\d+\s*股")

TIME_UNITS = {"年", "个月", "天", "周", "页", "名", "大", "条", "项"}
UNIT_CONVERSIONS = {
    ("元", "亿元"): Decimal("0.00000001"),
    ("万元", "亿元"): Decimal("0.0001"),
    ("亿元", "元"): Decimal("100000000"),
    ("亿元", "万元"): Decimal("10000"),
    ("百万元", "亿元"): Decimal("0.01"),
    ("亿元", "百万元"): Decimal("100"),
    ("股", "万股"): Decimal("0.0001"),
    ("股", "亿股"): Decimal("0.00000001"),
    ("万股", "股"): Decimal("10000"),
    ("亿股", "股"): Decimal("100000000"),
    ("万股", "亿股"): Decimal("0.0001"),
    ("亿股", "万股"): Decimal("10000"),
}


@dataclass
class VerifyResult:
    passed: bool
    issues: list[str] = field(default_factory=list)
    unverified: list[dict[str, str]] = field(default_factory=list)
    cited_fact_ids: list[str] = field(default_factory=list)
    number_count: int = 0
    mode: str = "fact"
    removed_citations: int = 0
    removed_citation_ids: list[str] = field(default_factory=list)
    normalized_citations: int = 0


def _unit(value: Any) -> str:
    text = str(value or "").strip().replace("％", "%").replace(" ", "")
    return text


def _mask(text: str) -> str:
    def blank(match: re.Match[str]) -> str:
        return " " * len(match.group(0))

    masked = CITATION_RE.sub(blank, text)
    masked = DIAGNOSTIC_RE.sub(blank, masked)
    masked = COMPACT_DATE_RE.sub(blank, masked)
    masked = CN_DATE_RE.sub(blank, masked)
    masked = CN_MONTH_DAY_RE.sub(blank, masked)
    masked = DATE_RE.sub(blank, masked)
    masked = MONTH_RE.sub(blank, masked)
    masked = QUARTER_RE.sub(blank, masked)
    masked = LIST_RE.sub(blank, masked)
    masked = PER_SHARE_RE.sub(blank, masked)
    masked = CODE_RE.sub(blank, masked)
    masked = RANK_RANGE_RE.sub(blank, masked)
    masked = PAGE_RE.sub(blank, masked)
    masked = BASE_INDEX_RE.sub(blank, masked)
    return masked


def _facts_by_id(evidences: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for evidence in evidences:
        for fact in evidence.get("facts") or []:
            fact_id = str(fact.get("fact_id") or "")
            if fact_id:
                result[fact_id] = fact
    return result


def _close(answer: Decimal, fact: Decimal, fact_unit: str, answer_unit: str) -> tuple[bool, Decimal | None]:
    expected = fact
    rate = UNIT_CONVERSIONS.get((fact_unit, answer_unit))
    if rate is not None:
        expected = fact * rate
    elif fact_unit and answer_unit and fact_unit != answer_unit:
        return False, None
    decimals = max(0, -answer.as_tuple().exponent)
    display_tolerance = Decimal("0.5") * (Decimal("10") ** -decimals)
    tolerance = max(
        Decimal("0.02"),
        min(abs(expected) * Decimal("0.002"), Decimal("0.5")),
        display_tolerance,
    )
    return abs(answer - expected) <= tolerance, expected


DIRECTION_DOWN_RE = re.compile(r"(下降|减少|降低|下滑|回落|收窄|负增长|低|少于|不及)")
DIRECTION_UP_RE = re.compile(r"(增长|上升|增加|提高|扩大|高|多于|超过)")


def _numeric_fact_value(fact: dict[str, Any]) -> Decimal | None:
    try:
        return Decimal(str(fact.get("value")))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _adjust_direction(value: Decimal, before: str, raw: str, answer_unit: str) -> Decimal:
    if raw.lstrip().startswith(("-", "−", "－")):
        return value
    if answer_unit not in {"%", "个百分点", "个基点", "bp", "BP"}:
        return value
    window = before[-10:]
    if value > 0 and DIRECTION_DOWN_RE.search(window):
        return -value
    if value < 0 and DIRECTION_UP_RE.search(window):
        return abs(value)
    return value


def _traceable(value: Decimal, answer_unit: str, cited_facts: list[dict[str, Any]]) -> bool:
    for fact in cited_facts:
        fact_value = _numeric_fact_value(fact)
        if fact_value is None:
            continue
        ok, _ = _close(value, fact_value, _unit(fact.get("unit")), answer_unit)
        if ok:
            return True
    return False

def _matching_facts(value: Decimal, answer_unit: str, facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for fact in facts:
        if _unit(fact.get("unit")) == "text":
            continue
        fact_value = _numeric_fact_value(fact)
        if fact_value is None:
            continue
        ok, _ = _close(value, fact_value, _unit(fact.get("unit")), answer_unit)
        if ok:
            matches.append(fact)
    return matches

def repair_citations(answer: str, evidences: list[dict[str, Any]]) -> tuple[str, int]:
    facts = _facts_by_id(evidences)
    all_facts = list(facts.values())
    cited_ids = [citation[1:-1] for citation in CITATION_RE.findall(answer)]
    cited_facts = [facts[fact_id] for fact_id in cited_ids if fact_id in facts]
    masked = _mask(answer)
    replacements: list[tuple[int, int, str]] = []
    for match in NUMBER_RE.finditer(masked):
        raw_number = match.group(1).replace("−", "-").replace("－", "-")
        answer_unit = _unit(match.group(2))
        if answer_unit in TIME_UNITS:
            continue
        before = answer[max(0, match.start() - 16) : match.start()]
        try:
            answer_value = _adjust_direction(Decimal(raw_number), before, raw_number, answer_unit)
        except InvalidOperation:
            continue
        after = answer[match.end() : match.end() + 80]
        block_match = CITATION_BLOCK_RE.match(after)
        if block_match:
            block = block_match.group(0)
            block_ids = [citation[1:-1] for citation in CITATION_RE.findall(block)]
            if any(
                fact_id in facts
                and _numeric_fact_value(facts[fact_id]) is not None
                and _close(
                    answer_value,
                    _numeric_fact_value(facts[fact_id]),
                    _unit(facts[fact_id].get("unit")),
                    answer_unit,
                )[0]
                for fact_id in block_ids
            ):
                continue
            sign_fixed = False
            for fact_id in block_ids:
                fact = facts.get(fact_id)
                if fact is None:
                    continue
                fact_value = _numeric_fact_value(fact)
                if fact_value is None or _unit(fact.get("unit")) == "text":
                    continue
                if answer_value * fact_value < 0 and _close(
                    abs(answer_value), abs(fact_value), _unit(fact.get("unit")), answer_unit
                )[0]:
                    signed = raw_number if fact_value > 0 else "-" + raw_number.lstrip("-")
                    replacements.append((match.start(1), match.end(1), signed))
                    sign_fixed = True
                    break
            if sign_fixed:
                continue
            candidates = _matching_facts(answer_value, answer_unit, all_facts)
            if len(candidates) == 1:
                first = CITATION_RE.search(block)
                start = match.end() + block_match.start() + first.start()
                end = match.end() + block_match.start() + first.end()
                replacements.append((start, end, f"[{candidates[0]['fact_id']}]"))
            continue
        if _traceable(answer_value, answer_unit, cited_facts):
            continue
        candidates = _matching_facts(answer_value, answer_unit, all_facts)
        if len(candidates) == 1:
            replacements.append((match.end(), match.end(), f" [{candidates[0]['fact_id']}]"))
    if not replacements:
        return answer, 0
    repaired = answer
    for start, end, replacement in sorted(replacements, key=lambda item: (item[0], item[1]), reverse=True):
        repaired = repaired[:start] + replacement + repaired[end:]
    return repaired, len(replacements)


def strip_citations(text: str) -> str:
    """Remove citation tokens from historical text before it enters the model context."""
    return CITATION_LIKE_RE.sub("", str(text or ""))



def citation_only_issues(result: VerifyResult) -> bool:
    """True when the only problem is that invalid citations were removed."""
    return bool(result.removed_citations) and all(
        issue.startswith("已移除 ") for issue in result.issues
    )


def verify_raw_answer(
    answer: str,
    evidences: list[dict[str, Any]],
) -> tuple[str, VerifyResult]:
    """Validate evidence-level citations in raw mode.

    Raw mode has no fact ids, so only the evidence ids ``E{turn}-{seq}`` are
    checked. Invalid citations are removed from the body instead of failing the
    whole answer; the caller must treat ``removed_citations > 0`` as degraded.
    """
    evidence_ids = {
        str(item.get("id"))
        for item in evidences or []
        if item and item.get("id")
    }
    issues: list[str] = []
    unverified: list[dict[str, str]] = []
    cited: list[str] = []
    removed_ids: list[str] = []
    removed_count = 0
    normalized = 0

    pieces: list[str] = []
    last = 0
    for match in CITATION_LIKE_RE.finditer(answer):
        token = match.group(0)
        inner = token[1:-1].strip()
        ids = [f"E{number}-{index}" for number, index in CITATION_ID_RE.findall(inner)]
        if not ids:
            removed_count += 1
            label = inner[:60] or token
            removed_ids.append(label)
            unverified.append({"fact_id": label, "reason": "引用格式无效，已移除"})
            pieces.append(answer[last : match.start()])
            last = match.end()
            continue

        valid: list[str] = []
        for evidence_id in ids:
            if evidence_id in evidence_ids:
                if evidence_id not in valid:
                    valid.append(evidence_id)
                if evidence_id not in cited:
                    cited.append(evidence_id)
            else:
                removed_count += 1
                if evidence_id not in removed_ids:
                    removed_ids.append(evidence_id)
                unverified.append(
                    {"fact_id": evidence_id, "reason": "引用不在本轮证据中，已移除"}
                )

        canonical = len(ids) == 1 and token == f"[{ids[0]}]"
        pieces.append(answer[last : match.start()])
        if valid:
            replacement = "".join(f"[{evidence_id}]" for evidence_id in valid)
            if not canonical or replacement != token:
                normalized += 1
            pieces.append(replacement)
        last = match.end()
    pieces.append(answer[last:])
    text = "".join(pieces)

    if removed_count:
        issues.append(f"已移除 {removed_count} 条无法核实的引用")
    if evidence_ids and not cited:
        issues.append("answer_has_no_citation: 回答没有任何有效引用")
    issues = list(dict.fromkeys(issues))
    return text, VerifyResult(
        passed=not issues,
        issues=issues,
        unverified=unverified,
        cited_fact_ids=cited,
        mode="raw",
        removed_citations=removed_count,
        removed_citation_ids=removed_ids,
        normalized_citations=normalized,
    )


def verify_answer(answer: str, evidences: list[dict[str, Any]]) -> VerifyResult:
    text = answer.strip()
    facts = _facts_by_id(evidences)
    issues: list[str] = []
    unverified: list[dict[str, str]] = []
    cited: list[str] = []


    conclusion = section_body(text, 0)
    if conclusion is None:
        conclusion = section_body(text, 0, LEGACY_SECTION_PATTERNS)
    if conclusion is not None and not CITATION_RE.search(conclusion):
        issues.append("结论段没有 fact 级引用")

    for citation in CITATION_RE.findall(text):
        fact_id = citation[1:-1]
        if fact_id not in cited:
            cited.append(fact_id)
        if fact_id not in facts:
            issues.append(f"引用了不存在的 {citation}")
            unverified.append({"fact_id": fact_id, "reason": "引用不存在"})

    cited_facts = [facts[fact_id] for fact_id in cited if fact_id in facts]
    masked = _mask(text)
    number_count = 0
    for match in NUMBER_RE.finditer(masked):
        raw_number = match.group(1).replace("−", "-").replace("－", "-")
        answer_unit = _unit(match.group(2))
        if answer_unit in TIME_UNITS:
            continue
        number_count += 1
        after = text[match.end() : match.end() + 80]
        block_match = CITATION_BLOCK_RE.match(after)
        if block_match:
            citation_match = CITATION_RE.search(block_match.group(0))
        else:
            citation_match = CITATION_RE.search(after[:18])
        before = text[max(0, match.start() - 16) : match.start()]
        try:
            answer_value = _adjust_direction(Decimal(raw_number), before, raw_number, answer_unit)
        except InvalidOperation:
            continue
        if not citation_match:
            if _traceable(answer_value, answer_unit, cited_facts):
                continue
            reason = f"数字 {raw_number}{answer_unit} 后面没有 fact 引用"
            issues.append(reason)
            unverified.append({"fact_id": "", "reason": reason})
            continue
        block_ids = (
            [citation[1:-1] for citation in CITATION_RE.findall(block_match.group(0))]
            if block_match
            else [citation_match.group(0)[1:-1]]
        )
        candidates = [(fact_id, facts.get(fact_id)) for fact_id in block_ids]
        candidates = [(fact_id, fact) for fact_id, fact in candidates if fact is not None]
        matched = False
        for _, candidate in candidates:
            candidate_value = _numeric_fact_value(candidate)
            if candidate_value is None or _unit(candidate.get("unit")) == "text":
                continue
            if _close(answer_value, candidate_value, _unit(candidate.get("unit")), answer_unit)[0]:
                matched = True
                break
        if matched:
            continue
        if not candidates:
            fact_id = block_ids[0]
            reason = f"数字 {raw_number}{answer_unit} 引用了不存在的 {citation_match.group(0)}"
            issues.append(reason)
            unverified.append({"fact_id": fact_id, "reason": reason})
            continue
        fact_id, fact = candidates[0]
        fact_value = _numeric_fact_value(fact)
        if fact_value is None:
            reason = f"[{fact_id}] 不是可校验的数值事实"
            issues.append(reason)
            unverified.append({"fact_id": fact_id, "reason": reason})
            continue
        fact_unit = _unit(fact.get("unit"))
        if fact_unit == "text":
            reason = f"数字 {raw_number}{answer_unit} 引用了文本事实 {citation_match.group(0)}"
            issues.append(reason)
            unverified.append({"fact_id": fact_id, "reason": reason})
            continue
        ok, expected = _close(answer_value, fact_value, fact_unit, answer_unit)
        if not ok:
            expected_text = f"{expected}" if expected is not None else f"{fact_value}{fact_unit}"
            reason = f"数字 {raw_number}{answer_unit} 与 {citation_match.group(0)} 的 {expected_text} 不一致"
            issues.append(reason)
            unverified.append({"fact_id": fact_id, "reason": reason})

    if not cited:
        issues.append("answer_has_no_citation: 回答没有任何 fact 级引用")
    passed = not issues and bool(cited)
    return VerifyResult(
        passed=passed,
        issues=list(dict.fromkeys(issues)),
        unverified=unverified,
        cited_fact_ids=cited,
        number_count=number_count,
    )
