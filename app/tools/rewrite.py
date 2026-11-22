from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

METRIC_SYNONYMS = {
    "营收": "营业收入",
    "收入": "营业收入",
    "毛利": "销售毛利率",
    "毛利率": "销售毛利率",
    "净利": "净利润",
    "净利率": "销售净利率",
    "净资产收益率": "ROE",
    "市盈率": "市盈率TTM",
    "PE": "市盈率TTM",
    "总市值": "总市值",
}

QUOTE_METRICS = ["最新价", "涨跌幅", "总市值", "市盈率TTM"]
DEFAULT_METRICS = {
    "ifind.stock_financials": ["营业收入", "净利润", "销售毛利率", "ROE"],
    "ifind.stock_holders": ["前十大股东", "总股本", "流通股本"],
    "ifind.stock_events": ["分红", "增发", "回购", "解禁", "业绩预告"],
    "ifind.stock_performance": ["收盘价", "涨跌幅", "换手率"],
}


def normalize_metrics(values: Any) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        raw = re.split(r"[,，、;；\s]+", values)
    elif isinstance(values, list):
        raw = [str(item) for item in values]
    else:
        raw = [str(values)]
    result: list[str] = []
    for item in raw:
        item = item.strip()
        if not item:
            continue
        result.append(METRIC_SYNONYMS.get(item, item))
    return list(dict.fromkeys(result))


def normalize_symbols(values: Any) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        raw = re.split(r"[,，、;；\s]+", values)
    elif isinstance(values, list):
        raw = [str(item) for item in values]
    else:
        raw = [str(values)]
    result: list[str] = []
    for item in raw:
        item = item.strip()
        if not item:
            continue
        item = item.replace(".SH", "").replace(".SZ", "").replace(".BJ", "")
        result.append(item)
    return list(dict.fromkeys(result))[:5]


def normalize_date(value: str | None, default: date | None = None) -> str | None:
    if not value:
        return default.isoformat() if default else None
    text = str(value).strip()
    m = re.match(r"^(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})", text)
    if m:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
    q = re.match(r"^(20\d{2})\s*[Qq]([1-4])$", text)
    if q:
        year, quarter = int(q.group(1)), int(q.group(2))
        month_day = {1: (3, 31), 2: (6, 30), 3: (9, 30), 4: (12, 31)}[quarter]
        return date(year, *month_day).isoformat()
    h = re.match(r"^(20\d{2})\s*[Hh]([12])$", text)
    if h:
        return date(int(h.group(1)), 6 if h.group(2) == "1" else 12, 30 if h.group(2) == "1" else 31).isoformat()
    year = re.match(r"^(20\d{2})$", text)
    if year:
        return date(int(year.group(1)), 12, 31).isoformat()
    return None


def period_label(period: str | None) -> str:
    if not period:
        return ""
    q = re.match(r"^(20\d{2})\s*[Qq]([1-4])$", period)
    if q:
        names = {"1": "一季报", "2": "中报", "3": "三季报", "4": "年报"}
        return f"{q.group(1)}年{names[q.group(2)]}"
    return period


def financial_period_label(period: str | None) -> str:
    normalized = normalize_date(period)
    if normalized:
        report_date = date.fromisoformat(normalized)
        names = {
            (3, 31): "一季报",
            (6, 30): "中报",
            (9, 30): "三季报",
            (12, 31): "年报",
        }
        suffix = names.get((report_date.month, report_date.day))
        if suffix:
            return f"{report_date.year}年{suffix}"
    return period_label(period)


def prior_year_period_label(period: str | None) -> str:
    label = financial_period_label(period)
    match = re.match(r"^(20\d{2})(年.+)$", label)
    if match:
        return f"{int(match.group(1)) - 1}{match.group(2)}"
    normalized = normalize_date(period)
    if normalized:
        current = date.fromisoformat(normalized)
        try:
            return date(current.year - 1, current.month, current.day).isoformat()
        except ValueError:
            return ""
    return ""


def window_start(days: int, end: date | None = None) -> date:
    return (end or date.today()) - timedelta(days=max(1, min(int(days), 365)))


def build_query(tool: str, args: dict[str, Any]) -> str:
    if tool.startswith("ifind.global_"):
        parts = [str(args.get("query", "")).strip(), str(args.get("market", "")).strip()]
        return " ".join(part for part in parts if part)
    if tool == "ifind.stock_quote":
        symbols = " ".join(normalize_symbols(args.get("symbols") or args.get("securities")))
        metrics = " ".join(normalize_metrics(args.get("metrics")) or QUOTE_METRICS)
        return f"{symbols} {metrics} 实时快照".strip()
    if tool == "ifind.stock_summary":
        return str(args.get("query", "")).strip()
    if tool == "ifind.stock_financials":
        securities = " ".join(normalize_symbols(args.get("securities") or args.get("security")))
        period = financial_period_label(args.get("period"))
        compare = args.get("compare") in {True, "同比", "yoy"}
        prior_period = prior_year_period_label(args.get("period")) if compare else ""
        periods = " ".join(part for part in [period, prior_period] if part)
        metrics = " ".join(normalize_metrics(args.get("metrics")) or DEFAULT_METRICS[tool])
        return " ".join(part for part in [securities, periods, metrics] if part)
    if tool == "ifind.stock_performance":
        security = " ".join(normalize_symbols(args.get("security")))
        start = normalize_date(args.get("start")) or window_start(30).isoformat()
        end = normalize_date(args.get("end")) or date.today().isoformat()
        metrics = " ".join(normalize_metrics(args.get("metrics")) or DEFAULT_METRICS[tool])
        return f"{security} {start}-{end} {metrics}".strip()
    if tool == "ifind.stock_holders":
        security = " ".join(normalize_symbols(args.get("security")))
        metrics = " ".join(normalize_metrics(args.get("metrics")) or DEFAULT_METRICS[tool])
        return f"{security} {metrics}".strip()
    if tool == "ifind.stock_events":
        security = " ".join(normalize_symbols(args.get("security")))
        period = period_label(args.get("period"))
        metrics = " ".join(normalize_metrics(args.get("metrics")) or DEFAULT_METRICS[tool])
        return " ".join(part for part in [security, period, metrics] if part)
    if tool == "ifind.stock_risk":
        security = " ".join(normalize_symbols(args.get("security")))
        window = str(args.get("window") or "过去1年")
        benchmark = str(args.get("benchmark") or "沪深300")
        return f"{security} {window} beta 波动率 夏普比率 最大回撤，基准{benchmark}"
    if tool in {"ifind.news_search", "ifind.notice_search"}:
        security = " ".join(normalize_symbols(args.get("security")))
        query = str(args.get("query") or args.get("topic") or "").strip()
        return " ".join(part for part in [security, query] if part)
    if tool == "ifind.macro":
        query = str(args.get("query", "")).strip()
        indicator = re.search(r"(?i)(CPI|PPI)", query)
        if indicator and ("上月" in query or "本月" in query):
            period = "上月" if "上月" in query else "本月"
            if "环比" in query:
                suffix = "环比"
            elif "累计" in query:
                suffix = "累计同比"
            else:
                suffix = "当月同比"
            return f"{period} {indicator.group(1).upper()} {suffix}"
        return query
    if tool == "ifind.index":
        target = str(args.get("target") or "").strip()
        query = str(args.get("query") or "").strip()
        start = normalize_date(args.get("start"))
        end = normalize_date(args.get("end"))
        span = f"{start}-{end}" if start and end else ""
        return " ".join(part for part in [target, query, span] if part)
    if tool == "ifind.screen":
        return str(args.get("query", "")).strip()
    return str(args.get("query", "")).strip()


def quote_upstream_args(args: dict[str, Any]) -> dict[str, Any]:
    symbols = normalize_symbols(args.get("symbols") or args.get("securities"))
    metrics = normalize_metrics(args.get("metrics")) or QUOTE_METRICS
    metrics = [m for m in metrics if m in {"最新价", "现额", "现量", "委比", "委差", "量比", "总股本", "总市值", "市净率", "1分钟涨跌幅", "3分钟涨跌幅", "5分钟涨跌幅", "流通市值", "市盈率TTM", "开盘价", "最高价", "最低价", "涨跌", "涨跌幅", "成交额", "成交量", "换手率"}]
    return {"symbols": ",".join(symbols), "indicators": ",".join(metrics[:10]), "data_mode": "real_time"}


def validate_args(tool: str, args: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(args, dict):
        raise ValueError("args must be an object")
    forbidden = {"__proto__", "prototype", "constructor"}
    if forbidden.intersection(args):
        raise ValueError("args contains blocked field")
    if tool == "ifind.stock_quote" and not normalize_symbols(args.get("symbols")):
        raise ValueError("symbols is required")
    if tool in {"ifind.stock_financials", "ifind.stock_performance", "ifind.stock_holders", "ifind.stock_events", "ifind.stock_risk"} and not normalize_symbols(args.get("security") or args.get("securities")):
        raise ValueError("security is required")
    if tool == "ifind.stock_summary" and not str(args.get("query", "")).strip():
        raise ValueError("query is required")
    if tool.startswith("ifind.global_") and not str(args.get("query", "")).strip():
        raise ValueError("query is required")
    return args
