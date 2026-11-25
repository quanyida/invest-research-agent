from __future__ import annotations

import re

SECTION_LABELS = ('结论', '依据', '关键数据', '待确认')
LEGACY_SECTION_LABELS = ('结论', '关键数字', '正文', '边界')


def _section_pattern(label: str) -> re.Pattern[str]:
    return re.compile(
        rf'(?m)^[ \t]*(?:#{{1,6}}[ \t]*)?(?:\*\*)?{re.escape(label)}(?:\*\*)?'
        rf'[ \t]*(?:[:：][ \t]*(?:\*\*)?|$)'
    )


SECTION_PATTERNS = tuple(_section_pattern(label) for label in SECTION_LABELS)
LEGACY_SECTION_PATTERNS = tuple(_section_pattern(label) for label in LEGACY_SECTION_LABELS)
FORMAT_CONTRACT = '结论：/依据：/关键数据：/待确认：'

SECTION_RULES = '''答案用 Markdown 组织，结构由你根据问题的性质自行决定。
- 所有数字必须紧跟 [E轮次-序号] 引用；原文没有的数字一律不写。
- 没有数据支撑的判断写“据此推测”；没有取到的数据写“未取到”，不要用空话凑长度。
- 有证据时，答案中至少出现一处有效引用。
- 允许：**加粗**、*斜体*、## 标题、- 无序列表、1. 有序列表、> 引用块、`行内代码`。
- 禁止：代码块（三反引号）、表格、图片和 []() 超链接。'''


def section_spans(
    text: str,
    patterns: tuple[re.Pattern[str], ...] = SECTION_PATTERNS,
) -> list[tuple[int, int, int]] | None:
    value = text or ''
    found: list[tuple[int, re.Match[str]]] = []
    for index, pattern in enumerate(patterns):
        match = pattern.search(value)
        if match is None:
            return None
        found.append((index, match))
    found.sort(key=lambda item: item[1].start())
    spans: list[tuple[int, int, int]] = []
    for position, (index, match) in enumerate(found):
        end = found[position + 1][1].start() if position + 1 < len(found) else len(value)
        spans.append((index, match.end(), end))
    return spans


def has_all_sections(
    text: str,
    patterns: tuple[re.Pattern[str], ...] = SECTION_PATTERNS,
) -> bool:
    return section_spans(text, patterns) is not None


def section_body(
    text: str,
    index: int,
    patterns: tuple[re.Pattern[str], ...] = SECTION_PATTERNS,
) -> str | None:
    spans = section_spans(text, patterns)
    if spans is None:
        return None
    value = text or ''
    for found_index, start, end in spans:
        if found_index == index:
            return value[start:end]
    return None


def answer_is_complete(text: str, min_chars: int = 80) -> bool:
    value = (text or '').strip()
    return len(value) >= min_chars