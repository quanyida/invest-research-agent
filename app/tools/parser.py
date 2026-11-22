from __future__ import annotations

import json
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any


TABLE_SEPARATOR = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?\s*$")
DATE_RE = re.compile(r"(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})")
COMPACT_DATE_RE = re.compile(r"(?<!\d)(20\d{2})(\d{2})(\d{2})(?!\d)")
UNIT_RE = re.compile(r"[（(]([^）)]+)[）)]\s*$")
NUMBER_WITH_UNIT_RE = re.compile(r"^([+-]?(?:\d{1,3}(?:,\d{3})*|\d+)(?:\.\d+)?)\s*(万亿元|亿元|万元|百万元|亿欧元|亿美元|亿股|GWh|MWh|万辆|个百分点|元|亿|万|%|倍|股|手|辆|吨)?$")

def _clean_cell(value: str) -> str:
    return value.strip().strip("`").strip()


def _normalize_unit_hint(value: str) -> str:
    text = value.strip()
    match = re.search(r"(?:单位|币种)\s*[:：]\s*([^\s；;，,）)]+)", text)
    if match:
        return match.group(1).strip()
    text = re.sub(r"^(?:单位|币种)\s*[:：]\s*", "", text)
    return text.strip().strip("；;，,")

def _normalize_currency(value: str) -> str:
    text = value.strip()
    mapping = {
        "USD": "美元",
        "US$": "美元",
        "$": "美元",
        "HKD": "港元",
        "HK$": "港元",
        "港币": "港元",
        "CNY": "元",
        "RMB": "元",
        "人民币": "元",
    }
    return mapping.get(text.upper(), text)

def _apply_currency(unit: str, currency: str) -> str:
    if not unit or not currency or currency == "元":
        return unit
    if unit.endswith(currency):
        return unit
    if unit.endswith("元"):
        return unit[:-1] + currency
    return unit

def _meta_for_metric(metadata: dict[str, dict[str, Any]], metric: str) -> dict[str, Any]:
    if not metric:
        return {}
    direct = metadata.get(metric)
    if isinstance(direct, dict):
        return direct
    for key, value in metadata.items():
        if not isinstance(value, dict):
            continue
        if key.startswith(metric) or metric.startswith(key):
            return value
    return {}

def _period_from_meta(meta: dict[str, Any]) -> str:
    for key in ("报告期", "交易日期", "统计期间", "截止日期"):
        value = str(meta.get(key) or "").strip()
        if value:
            return value
    return ""


def _try_decode_json_container(value: Any) -> Any | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or text[0] not in "[{":
        return None
    try:
        decoded = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    return decoded if isinstance(decoded, (dict, list)) else None

def _split_row(line: str) -> list[str]:
    body = line.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|"):
        body = body[:-1]
    return [_clean_cell(part) for part in body.split("|")]


def _unit_from_column(column: str) -> str:
    if any(word in column for word in ("市盈率", "市净率", "市销率", "市现率")):
        return "倍"
    match = UNIT_RE.search(column)
    if match:
        return _normalize_unit_hint(match.group(1))
    if any(word in column for word in ("涨跌幅", "涨幅", "跌幅", "换手率", "占比", "比例")) or "率" in column:
        return "%"
    if any(word in column for word in ("市值", "金额", "成交额", "营业收入", "净利润")):
        return "元"
    if any(word in column for word in ("股本", "成交量", "持股数")):
        return "股"
    if any(word in column for word in ("价格", "最新价", "收盘价", "开盘价", "最高价", "最低价")):
        return "元"
    return ""


def _metric_from_column(column: str) -> str:
    metric = UNIT_RE.sub("", column).strip()
    rank = re.search(r"第\s*\d+\s*名", column)
    if rank and rank.group(0) not in metric:
        metric = f"{metric}（{rank.group(0)}）"
    return metric


def _parse_number_with_unit(value: str, default_unit: str = "") -> tuple[Decimal, str] | None:
    text = _clean_cell(value).replace(",", "").replace(" ", "")
    if not text or text in {"-", "--", "—", "N/A", "nan", "None"}:
        return None
    match = NUMBER_WITH_UNIT_RE.match(text)
    if not match:
        return None
    try:
        number = Decimal(match.group(1))
    except InvalidOperation:
        return None
    suffix = match.group(2) or ""
    if suffix == "亿":
        suffix = f"亿{default_unit}" if default_unit in {"元", "股"} else "亿元"
    elif suffix == "万":
        suffix = f"万{default_unit}" if default_unit in {"元", "股"} else "万元"
    return number, suffix or default_unit


def _to_decimal(value: str) -> Decimal | None:
    parsed = _parse_number_with_unit(value)
    return parsed[0] if parsed else None


def _normalize_date(value: str) -> str | None:
    match = DATE_RE.search(value)
    if match:
        year, month, day = (int(item) for item in match.groups())
    else:
        match = COMPACT_DATE_RE.search(value)
        if not match:
            return None
        year, month, day = (int(item) for item in match.groups())
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None

def _pick_column(columns: list[str], keywords: tuple[str, ...]) -> str | None:
    for keyword in keywords:
        for column in columns:
            if keyword in column:
                return column
    return None


def parse_markdown_sections(markdown: str) -> list[dict[str, Any]]:
    lines = markdown.replace("\r\n", "\n").split("\n")
    sections: list[dict[str, Any]] = []
    current_title = "数据"
    current_lines: list[str] = []

    def flush() -> None:
        nonlocal current_lines, current_title
        if not current_lines:
            return
        tables = _parse_tables(current_lines)
        if tables:
            for table in tables:
                table["title"] = current_title
                sections.append(table)
        elif any(line.strip() for line in current_lines):
            sections.append(
                {
                    "title": current_title,
                    "columns": [],
                    "rows": [],
                    "unit_hints": {},
                    "text": "\n".join(current_lines).strip(),
                }
            )
        current_lines = []

    for line in lines:
        if re.match(r"^#{1,4}\s+", line):
            flush()
            current_title = re.sub(r"^#{1,4}\s+", "", line).strip() or "数据"
            continue
        current_lines.append(line)
    flush()
    return sections


def _parse_tables(lines: list[str]) -> list[dict[str, Any]]:
    tables: list[dict[str, Any]] = []
    index = 0
    while index + 1 < len(lines):
        header = lines[index].strip()
        if "|" not in header or not TABLE_SEPARATOR.match(lines[index + 1]):
            index += 1
            continue
        columns = _split_row(header)
        rows: list[dict[str, str]] = []
        unit_hints = {column: _unit_from_column(column) for column in columns if _unit_from_column(column)}
        index += 2
        while index < len(lines) and "|" in lines[index] and lines[index].strip():
            cells = _split_row(lines[index])
            if len(cells) == len(columns):
                rows.append(dict(zip(columns, cells, strict=False)))
            index += 1
        if columns and rows:
            tables.append({"columns": columns, "rows": rows, "unit_hints": unit_hints})
        continue
    return tables


def extract_texts(envelope: Any) -> list[str]:
    texts: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            if isinstance(value.get("text"), str):
                texts.append(value["text"])
            for key, item in value.items():
                if key != "text":
                    walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(envelope)
    return texts


def _parse_structured_tables(data: dict[str, Any]) -> list[dict[str, Any]]:
    sections: list[dict[str, Any]] = []
    raw_tables = data.get("tables") or []
    if not isinstance(raw_tables, list):
        return sections
    if raw_tables and isinstance(raw_tables[0], list) and raw_tables[0] and isinstance(raw_tables[0][0], list):
        table_groups = raw_tables
    else:
        table_groups = [raw_tables]
    for table_index, table in enumerate(table_groups, start=1):
        if not isinstance(table, list) or len(table) < 2:
            continue
        columns = [str(item).strip() for item in table[0]]
        rows: list[dict[str, str]] = []
        for row in table[1:]:
            if not isinstance(row, list):
                continue
            cells = [str(item) for item in row]
            if len(cells) >= len(columns):
                rows.append(dict(zip(columns, cells[: len(columns)], strict=False)))
        if not columns or not rows:
            continue
        unit_hints = {column: _unit_from_column(column) for column in columns if _unit_from_column(column)}
        sections.append(
            {
                "title": f"结构化数据 {table_index}",
                "columns": columns,
                "rows": rows,
                "unit_hints": unit_hints,
            }
        )
    return sections


TEXT_NUMBER_RE = re.compile(
    r"([+-]?\d+(?:,\d{3})*(?:\.\d+)?)\s*(亿元|万元|百万元|元|%|GWh|MWh|万辆|辆|吨|倍|个百分点)"
)


def _parse_record_sections(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, dict):
        data = data.get("data", data)
    decoded = _try_decode_json_container(data)
    if decoded is not None:
        data = decoded
    if not isinstance(data, list):
        return []
    rows = [item for item in data if isinstance(item, dict)]
    if not rows:
        return []
    columns: list[str] = []
    for row in rows:
        for key in row:
            if key not in columns:
                columns.append(str(key))
    if not columns:
        return []
    title = "资讯列表" if any("资讯" in column or "新闻" in column for column in columns) else "公告列表"
    unit_hints = {column: _unit_from_column(column) for column in columns if _unit_from_column(column)}
    normalized_rows = [{column: str(row.get(column, "")) for column in columns} for row in rows]
    return [{"title": title, "columns": columns, "rows": normalized_rows, "unit_hints": unit_hints}]

def parse_ifind_response(envelope: Any, entity_hint: str = "", evidence_seq: int = 1) -> dict[str, Any]:
    markdown_parts: list[str] = []
    structured_data: list[dict[str, Any]] = []
    record_data: list[Any] = []
    metadata: dict[str, dict[str, Any]] = {}
    inner_codes: list[Any] = []
    inner_messages: list[str] = []
    for text in extract_texts(envelope):
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            markdown_parts.append(text)
            continue
        if isinstance(parsed, dict):
            if "code" in parsed:
                inner_codes.append(parsed.get("code"))
            if parsed.get("msg"):
                inner_messages.append(str(parsed["msg"]))
            data = parsed.get("data")
            if isinstance(data, str):
                decoded = _try_decode_json_container(data)
                if decoded is not None:
                    record_data.append(decoded)
                else:
                    markdown_parts.append(data)
            elif isinstance(data, dict) and isinstance(data.get("tables"), list):
                structured_data.append(data)
            elif isinstance(data, dict) and isinstance(data.get("answer"), str):
                markdown_parts.append(data["answer"])
                params = data.get("indicators_params")
                if isinstance(params, dict):
                    for key, value in params.items():
                        if isinstance(value, dict):
                            metadata.setdefault(str(key), {}).update(value)
            elif isinstance(data, dict):
                nested = _try_decode_json_container(data.get("data"))
                record_data.append(nested if nested is not None else data)
            elif isinstance(data, list):
                record_data.append(data)
        elif isinstance(parsed, list):
            record_data.append(parsed)
        elif isinstance(parsed, str):
            markdown_parts.append(parsed)

    markdown = "\n\n".join(part for part in markdown_parts if part.strip())
    sections = parse_markdown_sections(markdown) if markdown else []
    for data in structured_data:
        sections.extend(_parse_structured_tables(data))
    for data in record_data:
        sections.extend(_parse_record_sections(data))

    facts: list[dict[str, Any]] = []
    period_dates: list[str] = []

    for section in sections:
        columns = section.get("columns") or []
        rows = section.get("rows") or []
        if not rows:
            continue
        entity_col = _pick_column(columns, ("证券简称", "股票简称", "证券代码", "股票代码", "名称", "标的"))
        period_col = _pick_column(columns, ("报告期", "统计期间", "日期", "时间", "time", "发布时间", "季度"))
        currency_col = _pick_column(columns, ("记账本位币", "货币种类", "交易币种", "币种", "currency"))
        if period_col is None:
            period_col = next((column for column in columns if column.strip() in {"年度", "年份", "年"}), None)
        for row_index, row in enumerate(rows, start=1):
            entity = str(row.get(entity_col, "")) if entity_col else entity_hint
            period = str(row.get(period_col, "")) if period_col else ""
            normalized_period = _normalize_date(period) or (period if period else None)
            row_currency = _normalize_currency(str(row.get(currency_col, ""))) if currency_col else ""
            row_period = normalized_period
            row_has_numeric = False
            for column, raw_value in row.items():
                if column in {entity_col, period_col, currency_col}:
                    continue
                unit_hint = section.get("unit_hints", {}).get(column) or _unit_from_column(column)
                parsed_number = _parse_number_with_unit(str(raw_value), unit_hint)
                if parsed_number is None:
                    continue
                number, unit = parsed_number
                row_has_numeric = True
                metric = _metric_from_column(column)
                meta = _meta_for_metric(metadata, metric)
                fact_period = normalized_period or _period_from_meta(meta) or None
                if fact_period and not row_period:
                    row_period = fact_period
                normalized_fact_period = _normalize_date(str(fact_period)) if fact_period else None
                if normalized_fact_period:
                    period_dates.append(normalized_fact_period)
                unit = _apply_currency(unit, row_currency)
                facts.append(
                    {
                        "fact_id": f"E{evidence_seq}-{len(facts) + 1}",
                        "entity": entity or entity_hint,
                        "period": fact_period,
                        "metric": metric,
                        "value": format(number, "f"),
                        "unit": unit,
                        "source_row": row_index,
                        "section": section.get("title"),
                    }
                )
            if not row_has_numeric:
                business_values = [
                    str(value).strip()
                    for key, value in row.items()
                    if key not in {entity_col, period_col} and str(value).strip()
                ]
                if not business_values:
                    continue
                text_value = "；".join(f"{key}={value}" for key, value in row.items() if value)
                facts.append(
                    {
                        "fact_id": f"E{evidence_seq}-{len(facts) + 1}",
                        "entity": entity or entity_hint,
                        "period": row_period,
                        "metric": section.get("title") or "记录",
                        "value": text_value[:500],
                        "unit": "text",
                        "source_row": row_index,
                        "section": section.get("title"),
                    }
                )
                for column, raw_value in row.items():
                    if column in {entity_col, period_col}:
                        continue
                    for match in TEXT_NUMBER_RE.finditer(str(raw_value)):
                        facts.append(
                            {
                                "fact_id": f"E{evidence_seq}-{len(facts) + 1}",
                                "entity": entity or entity_hint,
                                "period": row_period,
                                "metric": _metric_from_column(column),
                                "value": match.group(1).replace(",", ""),
                                "unit": match.group(2),
                                "source_row": row_index,
                                "section": section.get("title"),
                            }
                        )

    quality = 0.95 if any(section.get("rows") for section in sections) else (0.5 if markdown else 0.0)
    period_end = max(period_dates) if period_dates else None
    return {
        "sections": sections,
        "facts": facts,
        "period_end": period_end,
        "raw_excerpt": markdown[:2000],
        "truncated": len(markdown) > 2000,
        "parse_quality": quality,
        "parser_version": "2026-11-17",
        "inner_code": inner_codes[0] if inner_codes else None,
        "inner_message": inner_messages[0] if inner_messages else None,
        "now": datetime.now().astimezone().isoformat(),
    }
