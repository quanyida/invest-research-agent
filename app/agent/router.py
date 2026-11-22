from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from app.tools.rewrite import (
    DEFAULT_METRICS,
    METRIC_SYNONYMS,
    QUOTE_METRICS,
    normalize_date,
    normalize_metrics,
)


KNOWN_TICKERS = [
    "宁德时代", "比亚迪", "贵州茅台", "五粮液", "招商银行", "平安银行", "中国平安",
    "隆基绿能", "阳光电源", "亿纬锂能", "赣锋锂业", "天齐锂业", "美的集团", "格力电器",
    "海康威视", "中芯国际", "北方华创", "东方财富", "中信证券", "中国中免", "药明康德",
    "恒瑞医药", "万科A", "紫金矿业", "长江电力", "中国神华",
]

GLOBAL_ALIASES = {
    "谷歌": "谷歌", "google": "Google", "googl": "GOOGL", "alphabet": "Alphabet",
    "苹果": "苹果", "apple": "Apple", "aapl": "AAPL",
    "微软": "微软", "microsoft": "Microsoft", "msft": "MSFT",
    "亚马逊": "亚马逊", "amazon": "Amazon", "amzn": "AMZN",
    "英伟达": "英伟达", "nvidia": "NVIDIA", "nvda": "NVDA",
    "特斯拉": "特斯拉", "tesla": "Tesla", "tsla": "TSLA",
    "meta": "Meta", "facebook": "Meta",
    "奈飞": "奈飞", "netflix": "Netflix", "nflx": "NFLX",
    "台积电": "台积电", "tsmc": "台积电", "tsm": "TSM",
    "博通": "博通", "broadcom": "Broadcom", "avgo": "AVGO",
    "英特尔": "英特尔", "intel": "Intel", "intc": "INTC",
    "amd": "AMD", "高通": "高通", "qualcomm": "Qualcomm", "qcom": "QCOM",
    "美光": "美光", "micron": "Micron", "mu": "MU",
    "甲骨文": "甲骨文", "oracle": "Oracle", "orcl": "ORCL",
    "palantir": "Palantir", "pltr": "PLTR",
    "阿里巴巴": "阿里巴巴", "alibaba": "阿里巴巴", "baba": "BABA",
    "京东": "京东", "jd": "JD", "拼多多": "拼多多", "pdd": "PDD",
    "百度": "百度", "baidu": "百度", "bidu": "BIDU",
    "网易": "网易", "netease": "网易", "ntes": "NTES",
    "腾讯": "腾讯", "tencent": "腾讯", "0700.hk": "0700.HK",
    "美团": "美团", "meituan": "美团", "3690.hk": "3690.HK",
    "小米": "小米", "xiaomi": "小米", "1810.hk": "1810.HK",
    "快手": "快手", "kuaishou": "快手", "1024.hk": "1024.HK",
    "蔚来": "蔚来", "nio": "NIO", "小鹏": "小鹏", "xpeng": "小鹏", "xpev": "XPEV",
    "理想汽车": "理想汽车", "li auto": "理想汽车",
    "迪士尼": "迪士尼", "disney": "Disney", "dis": "DIS",
    "可口可乐": "可口可乐", "coca-cola": "可口可乐", "ko": "KO",
    "smtc": "SMTC", "semtech": "SMTC",
}

GLOBAL_CANONICALS = set(GLOBAL_ALIASES.values())
GLOBAL_MARKET_PATTERN = re.compile(
    r"(美股|港美股|美国股市|纳斯达克|NASDAQ|纽交所|NYSE|港股|HKEX)",
    re.IGNORECASE,
)
GLOBAL_TICKER_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])(?:\$)?([A-Za-z]{1,5})(?:\.(?:US|HK))?(?![A-Za-z0-9])"
)
GLOBAL_TICKER_BLOCKLIST = {
    "AI", "AGI", "API", "CEO", "CFO", "CPI", "CPU", "CTO", "EPS", "ESG",
    "ETF", "EV", "GDP", "GPU", "HBM", "HK", "IPO", "LLM", "M2", "NASDAQ",
    "NYSE", "PB", "PE", "PMI", "PPI", "PS", "ROA", "ROE", "SAAS", "SEC",
    "US", "USD", "HKEX",
}

STOPWORDS = [
    "帮我", "请问", "查一下", "查询", "看一下", "告诉我", "最近", "现在", "目前",
    "今天", "今年", "去年", "怎么样", "多少", "什么", "如何", "有没有",
]


@dataclass
class PlannedTool:
    name: str
    args: dict[str, Any]


@dataclass
class Plan:
    intent: str
    tools: list[PlannedTool] = field(default_factory=list)
    securities: list[str] = field(default_factory=list)
    period: str | None = None
    metrics: list[str] = field(default_factory=list)
    note: str = ""
    unsupported: bool = False
    needs_llm: bool = False
    raw_question: str = ""


def _unique(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        value = value.strip()
        if value and value not in result:
            result.append(value)
    return result

def _alias_position(text: str, alias: str) -> int:
    if alias.isascii() and alias.replace(".", "").isalpha():
        match = re.search(rf"(?<![A-Za-z]){re.escape(alias)}(?![A-Za-z])", text)
        return match.start() if match else -1
    return text.find(alias)

def extract_global_entities(question: str) -> list[str]:
    lower = question.lower()
    found: list[tuple[int, str]] = []
    for alias, canonical in GLOBAL_ALIASES.items():
        position = _alias_position(lower, alias)
        if position >= 0:
            found.append((position, canonical))
    if GLOBAL_MARKET_PATTERN.search(question):
        for match in GLOBAL_TICKER_PATTERN.finditer(question):
            raw = match.group(0)
            ticker = match.group(1).upper()
            if ticker in GLOBAL_TICKER_BLOCKLIST:
                continue
            if not match.group(1).isupper() and not raw.startswith("$") and "." not in raw:
                continue
            found.append((match.start(), ticker))
    found.sort(key=lambda item: item[0])
    return _unique([item[1] for item in found])

def _is_global_entity(value: str) -> bool:
    return value in GLOBAL_CANONICALS or bool(re.fullmatch(r"[A-Z]{1,5}", value or ""))

def detect_domain(question: str, entities: list[str] | None = None) -> str:
    if extract_global_entities(question):
        return "global"
    if GLOBAL_MARKET_PATTERN.search(question):
        return "global"
    if entities and any(_is_global_entity(item) for item in entities):
        return "global"
    if re.search(r"(GDP|CPI|PPI|PMI|M2|社融|宏观|政策|工业增加值|固定资产投资)", question, re.IGNORECASE):
        return "macro"
    if re.search(r"(指数|板块|大盘|走势)", question):
        return "index"
    if re.search(r"(新闻|消息|公告|发生了什么|动态|进展|事件)", question):
        return "news"
    return "stock" if entities else ""


def extract_entities(question: str, history: list[dict[str, str]] | None = None) -> list[str]:
    found: list[str] = []
    code_pattern = re.compile(r"(?<!\d)([0368]\d{5})(?:\.(?:SZ|SH|BJ))?(?!\d)", re.IGNORECASE)
    for match in code_pattern.finditer(question):
        found.append(match.group(1))
    for name in KNOWN_TICKERS:
        if name in question:
            found.append(name)
    found.extend(extract_global_entities(question))
    if not found:
        pattern = re.compile(
            r"([\u4e00-\u9fa5A-Za-z]{2,8})(?=的|现在|今天|目前|股价|市值|营收|营业收入|净利润|毛利率|净利率|ROE|股东|分红|公告|新闻|财报|季报|年报)"
        )
        for match in pattern.finditer(question):
            candidate = match.group(1)
            for prefix in ("深度分析", "分析一下", "分析", "研究一下", "研究", "介绍一下", "介绍", "点评一下", "点评", "解读一下", "解读", "说一下", "说说", "看一下", "看看", "下"):
                if candidate.startswith(prefix) and len(candidate) > len(prefix):
                    candidate = candidate[len(prefix):]
                    break
            if candidate.endswith("公司") and len(candidate) > 2:
                candidate = candidate[:-2]
            if candidate not in STOPWORDS and not any(word in candidate for word in ("时代", "预期", "业务", "情况", "前景", "未来")):
                found.append(candidate)
    if not found and history:
        for item in reversed(history[-6:]):
            found = extract_entities(item.get("content", ""), None)
            if found:
                break
    return _unique(found)[:5]


def _latest_report_period(today: date) -> str:
    month = today.month
    if month >= 11:
        return date(today.year, 9, 30).isoformat()
    if month >= 8:
        return date(today.year, 6, 30).isoformat()
    if month >= 5:
        return date(today.year, 3, 31).isoformat()
    return date(today.year - 1, 12, 31).isoformat()


def extract_period(question: str, today: date | None = None) -> str:
    today = today or date.today()
    explicit = re.search(r"(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})", question)
    if explicit:
        try:
            return date(int(explicit.group(1)), int(explicit.group(2)), int(explicit.group(3))).isoformat()
        except ValueError:
            pass
    quarter = re.search(r"(20\d{2})\s*[Qq]([1-4])", question)
    if quarter:
        return normalize_date(quarter.group(0)) or _latest_report_period(today)
    half = re.search(r"(20\d{2})\s*[Hh]([12])", question)
    if half:
        return normalize_date(half.group(0)) or _latest_report_period(today)
    yearly = re.search(r"(20\d{2})\s*年?\s*(一季报|中报|半年报|三季报|年报)", question)
    if yearly:
        mapping = {"一季报": (3, 31), "中报": (6, 30), "半年报": (6, 30), "三季报": (9, 30), "年报": (12, 31)}
        month, day = mapping[yearly.group(2)]
        return date(int(yearly.group(1)), month, day).isoformat()
    year = re.search(r"(20\d{2})\s*年", question)
    if year:
        return date(int(year.group(1)), 12, 31).isoformat()
    relative = re.search(r"(去年|今年).*?(一季报|中报|半年报|三季报|年报)", question)
    if relative:
        target_year = today.year - 1 if relative.group(1) == "去年" else today.year
        mapping = {"一季报": (3, 31), "中报": (6, 30), "半年报": (6, 30), "三季报": (9, 30), "年报": (12, 31)}
        month, day = mapping[relative.group(2)]
        return date(target_year, month, day).isoformat()
    return _latest_report_period(today)

def _extract_metrics(question: str) -> list[str]:
    values: list[str] = []
    for word, canonical in METRIC_SYNONYMS.items():
        if word in question:
            values.append(canonical)
    for canonical in {
        "营业收入", "净利润", "销售毛利率", "销售净利率", "ROE", "总市值", "市盈率TTM",
        "最新价", "涨跌幅", "成交额", "换手率", "前十大股东", "总股本", "流通股本",
        "分红", "回购", "增发", "解禁", "业绩预告", "波动率", "beta", "最大回撤", "夏普比率",
    }:
        if canonical in question:
            values.append(canonical)
    return normalize_metrics(values)


def _topic(question: str, securities: list[str], period: str | None) -> str:
    text = question
    for security in securities:
        text = text.replace(security, " ")
    if period:
        text = text.replace(period, " ")
    for stop in STOPWORDS + ["新闻", "公告", "消息", "动态", "进展", "有哪些", "有什么", "的"]:
        text = text.replace(stop, " ")
    text = re.sub(r"[，。！？、,.!?；;：:\s]+", " ", text)
    return text.strip()[:80] or "最新经营动态"


def _range_days(question: str, default: int) -> int:
    if re.search(r"(近|过去)?\s*(一周|7天)", question):
        return 7
    if re.search(r"(近|过去)?\s*(一个月|30天)", question):
        return 30
    if re.search(r"(近|过去)?\s*(三个月|90天|一个季度)", question):
        return 90
    if re.search(r"(近|过去)?\s*(半年|6个月)", question):
        return 180
    if re.search(r"(近|过去)?\s*(一年|12个月)", question):
        return 365
    return default


def _performance_args(question: str, security: str) -> dict[str, Any]:
    days = _range_days(question, 90)
    end = date.today()
    start = end - timedelta(days=days)
    return {
        "security": security,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "metrics": _extract_metrics(question) or DEFAULT_METRICS["ifind.stock_performance"],
    }

def _global_financial_query(entity: str, question: str) -> str:
    metrics = _extract_metrics(question)
    defaults = ["营业收入", "净利润", "销售毛利率", "ROE", "市盈率PE(TTM)"]
    if "分部" in question or "业务" in question:
        defaults.insert(0, "分部收入")
    selected = list(dict.fromkeys(metrics + defaults))
    return f"{entity} 最新报告期 {' '.join(selected)}"

def _route_global_question(question: str, securities: list[str], period: str | None) -> Plan:
    entities = _unique(
        [item for item in securities if _is_global_entity(item)]
        + extract_global_entities(question)
    )
    lower = question.lower()
    if re.search(r"(帮我找|筛选|符合|哪些股票|选股|排名前)", question):
        market = "港股" if "港股" in question else ("美股" if "美股" in question else "")
        return Plan(
            "global_screen",
            tools=[PlannedTool("ifind.global_screen", {"query": question, "market": market})],
            note="港美股条件选股返回代码列表，不构成投资建议。",
            raw_question=question,
        )
    if not entities:
        return Plan("unknown", needs_llm=True, raw_question=question)

    wants_quote = bool(re.search(r"(股价|最新价|收盘价|涨跌|成交额|换手率|市值|行情|走势|技术指标|近\d+|过去)", question, re.IGNORECASE))
    wants_events = bool(re.search(r"(分红|回购|增发|并购|解禁|IPO|上市仪式|股东|事件|公告)", question, re.IGNORECASE))
    wants_profile = bool(re.search(r"(业务|主营|行业|上市|发行价|简介|公司介绍|基本面|深度分析|分析|研究|怎么样)", question))
    wants_financials = bool(re.search(r"(财务|营收|营业收入|收入|净利润|利润|毛利|净利|ROE|ROA|PE|市盈率|估值|财报|季报|年报|同比|环比|现金流|深度分析|分析|研究|基本面|业务)", question, re.IGNORECASE))
    deep = bool(re.search(r"(深度分析|全面分析|系统分析|研究一下|基本面)", question))

    tools: list[PlannedTool] = []
    if wants_quote:
        metrics = "近10个交易日 收盘价 涨跌幅 换手率 成交额" if re.search(r"(历史|走势|近\d+|过去)", question) else "最新价 涨跌幅 成交额 换手率 总市值"
        tools.append(PlannedTool("ifind.global_quotes", {"query": f"{'、'.join(entities)} {metrics}"}))
    if wants_events:
        tools.append(PlannedTool("ifind.global_events", {"query": f"{'、'.join(entities)} 分红 回购 IPO 事件"}))
    if wants_profile or deep:
        tools.append(PlannedTool("ifind.global_profile", {"query": f"{'、'.join(entities)} 所属行业 上市日期 首发价格 公司简介"}))
    if wants_financials or deep:
        tools.append(PlannedTool("ifind.global_financials", {"query": _global_financial_query('、'.join(entities), question)}))
    if not tools:
        tools = [
            PlannedTool("ifind.global_profile", {"query": f"{'、'.join(entities)} 所属行业 上市日期 首发价格 公司简介"}),
            PlannedTool("ifind.global_financials", {"query": _global_financial_query('、'.join(entities), question)}),
        ]
    intent = "global_analysis" if len(tools) > 1 else tools[0].name.replace("ifind.", "").replace(".", "_")
    return Plan(
        intent,
        tools=tools[:2],
        securities=entities[:5],
        period=period,
        note="已识别为港美股标的，使用 global server 取数。",
        raw_question=question,
    )

def route_question(question: str, history: list[dict[str, str]] | None = None, today: date | None = None) -> Plan:
    question = question.strip()
    today = today or date.today()
    securities = extract_entities(question, history)
    period = extract_period(question, today)
    metrics = _extract_metrics(question)
    lower = question.lower()

    if re.search(r"(上传|附件|pdf|图片|截图|文件)", lower):
        return Plan("unsupported_upload", unsupported=True, note="v1 不支持文件上传和 PDF 检索。", raw_question=question)
    if re.search(r"(公告原文|全文|第\s*\d+\s*页|写了什么)", question):
        return Plan("unsupported_fulltext", unsupported=True, note="v1 只能检索公告片段，不能读取公告全文或页码。", raw_question=question)
    if "为什么" in question and re.search(r"(涨|跌|波动|大涨|大跌)", question):
        return Plan("unsupported_causal", unsupported=True, note="v1 不推断涨跌原因，只能提供行情、财务和新闻事实。", raw_question=question)

    if detect_domain(question, securities) == "global":
        return _route_global_question(question, securities, period)

    if re.search(r"(帮我找|筛选|符合|哪些股票|选股|排名前)", question):
        return Plan("screen", tools=[PlannedTool("ifind.screen", {"query": question})], raw_question=question, note="条件选股返回代码列表，不构成投资建议。")

    if re.search(r"(对比|比较|谁高|谁低|排名|vs|VS)", question) and len(securities) >= 2:
        if re.search(r"(股价|市值|市盈率|PE|涨跌|成交额|换手率)", question, re.IGNORECASE):
            quote_metrics = metrics or QUOTE_METRICS
            return Plan(
                "compare",
                tools=[PlannedTool("ifind.stock_quote", {"symbols": securities, "metrics": quote_metrics})],
                securities=securities,
                period=period,
                metrics=quote_metrics,
                raw_question=question,
            )
        compare_metrics = metrics or ["营业收入", "净利润", "销售毛利率"]
        return Plan(
            "compare",
            tools=[PlannedTool("ifind.stock_financials", {"securities": securities, "period": period, "metrics": compare_metrics, "compare": "同比" if "同比" in question else ""})],
            securities=securities,
            period=period,
            metrics=compare_metrics,
            raw_question=question,
        )

    if re.search(r"(股东|股本|前十大|质押|持股)", question):
        if not securities:
            return Plan("unknown", needs_llm=True, raw_question=question)
        holder_metrics = metrics or DEFAULT_METRICS["ifind.stock_holders"]
        return Plan("holders", tools=[PlannedTool("ifind.stock_holders", {"security": securities[0], "metrics": holder_metrics})], securities=securities[:1], metrics=holder_metrics, raw_question=question)

    if re.search(r"(分红|回购|增发|并购|解禁|业绩预告|资本运作)", question):
        if not securities:
            return Plan("unknown", needs_llm=True, raw_question=question)
        event_metrics = metrics or DEFAULT_METRICS["ifind.stock_events"]
        return Plan("events", tools=[PlannedTool("ifind.stock_events", {"security": securities[0], "period": period, "metrics": event_metrics})], securities=securities[:1], period=period, metrics=event_metrics, raw_question=question)

    if re.search(r"(波动率|beta|最大回撤|夏普|VaR|风险指标)", question, re.IGNORECASE):
        if not securities:
            return Plan("unknown", needs_llm=True, raw_question=question)
        return Plan("risk", tools=[PlannedTool("ifind.stock_risk", {"security": securities[0], "window": f"过去{_range_days(question, 365)}天", "benchmark": "沪深300"})], securities=securities[:1], raw_question=question)

    if re.search(r"(新闻|消息|公告|发生了什么|动态|进展|事件)", question):
        if not securities and not re.search(r"(宏观|行业|政策)", question):
            return Plan("unknown", needs_llm=True, raw_question=question)
        days = _range_days(question, 30)
        topic = _topic(question, securities, period)
        news_args = {"security": securities[0] if securities else "", "topic": topic, "query": topic, "days": days, "size": 5}
        tools = [PlannedTool("ifind.news_search", news_args)]
        if re.search(r"(公告|披露|事件)", question):
            tools.append(PlannedTool("ifind.notice_search", {**news_args, "size": 5}))
        return Plan("news", tools=tools, securities=securities[:1], period=period, raw_question=question)
    if re.search(r"(GDP|CPI|PPI|PMI|M2|社融|产量|宏观|政策|(?<![毛净])利率|工业增加值|固定资产投资)", question, re.IGNORECASE):
        return Plan("macro", tools=[PlannedTool("ifind.macro", {"query": question, "period": ""})], raw_question=question)

    if re.search(r"(指数|板块|大盘|走势)", question):
        kind = "sector" if "板块" in question else "index"
        target = re.sub(r"(最近|近期|走势|行情|指数|板块|怎么样|如何)", "", question).strip() or question
        return Plan("index", tools=[PlannedTool("ifind.index", {"target": target, "kind": kind, "query": question})], raw_question=question)

    if re.search(r"(营收|营业收入|净利润|毛利|净利|ROE|现金流|负债|财报|季报|年报|同比|环比)", question):
        if not securities:
            return Plan("unknown", needs_llm=True, raw_question=question)
        financial_metrics = metrics or DEFAULT_METRICS["ifind.stock_financials"]
        return Plan(
            "financials",
            tools=[PlannedTool("ifind.stock_financials", {"securities": securities[:5], "period": period, "metrics": financial_metrics, "compare": "同比" if "同比" in question else ""})],
            securities=securities[:5],
            period=period,
            metrics=financial_metrics,
            raw_question=question,
        )

    if re.search(r"(股价|最新价|市值|市盈率|PE|涨跌|成交额|换手率|现在多少钱|收盘价)", question, re.IGNORECASE):
        if not securities:
            return Plan("unknown", needs_llm=True, raw_question=question)
        if re.search(r"(历史|走势|近\d+|过去|一年|三个月|一个月)", question):
            return Plan("performance", tools=[PlannedTool("ifind.stock_performance", _performance_args(question, securities[0]))], securities=securities[:1], raw_question=question)
        quote_metrics = metrics or QUOTE_METRICS
        return Plan("quote", tools=[PlannedTool("ifind.stock_quote", {"symbols": securities[:5], "metrics": quote_metrics})], securities=securities[:5], metrics=quote_metrics, raw_question=question)

    return Plan("unknown", needs_llm=True, raw_question=question)
