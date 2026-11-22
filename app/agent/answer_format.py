from __future__ import annotations

import re

SECTION_LABELS = ("结论", "依据", "关键数据", "待确认")
SECTION_PATTERNS = tuple(
    re.compile(rf"(?m)^\s*(?:#+\s*)?(?:\*\*)?{label}(?:\*\*)?\s*(?:[:：]|$)")
    for label in SECTION_LABELS
)
FORMAT_CONTRACT = "结论：/依据：/关键数据：/待确认："

SECTION_RULES = """最终答案必须严格按以下四段纯文本输出，不要 Markdown、不要代码块、不要过程说明：
结论：先直接回答；是非题先写“符合/部分符合/不符合/无法判断”，再用一两句话给核心理由。
依据：用编号逐条列出支撑事实或推理，每个编号条目必须独占一行（1. / 2. / 3.），不要把多条挤在同一段；每条只承担一个判断，事实后紧跟引用，推测写“据此推测”。
关键数据：只列最重要的数字，写清主体、期间、指标、数值、单位、引用；没有数字写“本轮无关键数字。”。
待确认：只写未取到的数据、未验证的标准、口径差异和来源分歧；没有明显缺口写“本轮未发现明显缺口。”。"""


def answer_is_complete(text: str, min_chars: int = 80) -> bool:
    value = (text or "").strip()
    return len(value) >= min_chars and all(
        pattern.search(value) for pattern in SECTION_PATTERNS
    )