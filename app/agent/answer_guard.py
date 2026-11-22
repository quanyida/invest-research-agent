from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.agent.context import degraded_answer, no_evidence_answer
from app.agent.citation_support import build_citation_support
from app.agent.verify import VerifyResult, repair_citations, verify_answer, verify_raw_answer

@dataclass
class GuardResult:
    answer: str
    verify: VerifyResult
    repaired: bool = False
    degraded: bool = False

def _clean(text: str) -> str:
    value = (text or "").strip()
    if value.startswith("```"):
        lines = value.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        value = "\n".join(lines).strip()
    for prefix in ("assistant:", "助手:", "AI:"):
        if value.startswith(prefix):
            value = value[len(prefix) :].strip()
    return value

def extract_answer(text: str) -> str:
    value = _clean(text)
    marker = value.find("结论：")
    if marker >= 0:
        value = value[marker:]
    return value.strip()

def guard_answer(
    raw_answer: str,
    evidences: list[dict[str, Any]],
    reason: str = "",
    *,
    verify_numbers: bool = True,
) -> GuardResult:
    text = extract_answer(raw_answer)
    if not evidences:
        return GuardResult(
            answer=no_evidence_answer([reason or "NO_EVIDENCE"]),
            verify=VerifyResult(
                passed=False,
                issues=["no_evidence"],
                mode="raw" if not verify_numbers else "fact",
            ),
            degraded=True,
        )
    if not text:
        return GuardResult(
            answer=degraded_answer(evidences, reason or "模型未输出可解析答案"),
            verify=VerifyResult(
                passed=False,
                issues=["empty_answer"],
                mode="raw" if not verify_numbers else "fact",
            ),
            degraded=True,
        )
    if not verify_numbers:
        text, result = verify_raw_answer(text, evidences)
        return GuardResult(
            answer=text,
            verify=result,
            degraded=result.removed_citations > 0,
        )
    text, repaired_count = repair_citations(text, evidences)
    result = verify_answer(text, evidences)
    return GuardResult(
        answer=text,
        verify=result,
        repaired=repaired_count > 0,
        degraded=False,
    )

def revision_message(draft: str, issues: list[str]) -> str:
    issue_text = "\n".join(f"- {item}" for item in issues[:12]) or "- 未通过事实校验"
    return (
        "上一版答案未通过事实校验。请只修正下列问题，不要重新取数，不要添加新数字，"
        "不要解释修订过程。\n\n"
        f"校验问题：\n{issue_text}\n\n"
        f"上一版答案：\n{draft[:6000]}\n\n"
        "请输出修正后的最终答案，严格四段纯文本：结论：/依据：/关键数据：/待确认：。"
        "删除无法由 observation 中 fact 支持的数字或句子；每个保留的数字后必须紧跟 [E轮次-序号]。"
        "“待确认”只写数据口径、缺失和分歧，不要把校验问题本身写进答案。"
    )


def digest_answer(evidences: list[dict[str, Any]], reason: str) -> str:
    return degraded_answer(evidences, reason)

def verify_payload(
    result: VerifyResult,
    answer: str | None = None,
    evidences: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    payload = {
        "passed": result.passed,
        "mode": result.mode,
        "issues": result.issues[:20],
        "unverified": result.unverified[:20],
        "cited_fact_ids": result.cited_fact_ids[:100],
        "number_count": result.number_count,
        "removed_citations": result.removed_citations,
        "removed_citation_ids": result.removed_citation_ids[:20],
        "normalized_citations": result.normalized_citations,
    }
    if answer is not None and evidences is not None:
        payload["citation_support"] = build_citation_support(answer, evidences)
    return payload
