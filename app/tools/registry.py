from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from app.tools.channels import ChannelSpec, channel_for_provider


@dataclass(frozen=True)
class ToolSpec:
    name: str
    server: str
    upstream: str
    description: str
    schema: dict[str, Any]
    cache_ttl_seconds: int
    cache_scope: str = "tenant"
    provider: str = "ifind"
    source_kind: str = "structured"
    source_kind_label: str = "结构化数据"

    @property
    def schema_hash(self) -> str:
        raw = json.dumps({"name": self.name, "schema": self.schema}, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    @property
    def channel_spec(self) -> ChannelSpec:
        return channel_for_provider(self.provider)

    @property
    def channel(self) -> str:
        return self.channel_spec.key

    @property
    def channel_label(self) -> str:
        return self.channel_spec.label


def _schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required or [], "additionalProperties": False}


TOOL_SPECS: dict[str, ToolSpec] = {
    "ifind.stock_quote": ToolSpec(
        name="ifind.stock_quote",
        server="stock",
        upstream="stock_highfreq_quotes",
        description="A股实时快照：最新价、涨跌幅、成交额、总市值、市盈率TTM。",
        schema=_schema(
            {
                "symbols": {"type": "array", "items": {"type": "string"}, "description": "股票代码或简称，最多 5 个"},
                "metrics": {"type": "array", "items": {"type": "string"}, "description": "指标，最多 8 个"},
            },
            ["symbols"],
        ),
        cache_ttl_seconds=180,
    ),
    "ifind.stock_summary": ToolSpec(
        name="ifind.stock_summary",
        server="stock",
        upstream="get_stock_summary",
        description="股票信息摘要，仅在结构化工具取不到时回退。",
        schema=_schema({"query": {"type": "string"}}, ["query"]),
        cache_ttl_seconds=1800,
    ),
    "ifind.stock_financials": ToolSpec(
        name="ifind.stock_financials",
        server="stock",
        upstream="get_stock_financials",
        description="三大报表与财务指标，支持同比和多标的对比。",
        schema=_schema(
            {
                "securities": {"type": "array", "items": {"type": "string"}, "description": "股票代码或简称，最多 5 个"},
                "period": {"type": "string", "description": "报告期，如 2025Q3 或 2025-09-30"},
                "metrics": {"type": "array", "items": {"type": "string"}, "description": "指标，最多 8 个"},
                "compare": {"type": "string", "description": "同比/yoy，可空"},
            },
            ["securities", "period", "metrics"],
        ),
        cache_ttl_seconds=43200,
    ),
    "ifind.stock_performance": ToolSpec(
        name="ifind.stock_performance",
        server="stock",
        upstream="get_stock_performance",
        description="日频行情、技术指标，必须给绝对起止日期。",
        schema=_schema(
            {
                "security": {"type": "string"},
                "start": {"type": "string", "description": "YYYY-MM-DD"},
                "end": {"type": "string", "description": "YYYY-MM-DD"},
                "metrics": {"type": "array", "items": {"type": "string"}},
            },
            ["security", "start", "end", "metrics"],
        ),
        cache_ttl_seconds=1800,
    ),
    "ifind.stock_holders": ToolSpec(
        name="ifind.stock_holders",
        server="stock",
        upstream="get_stock_shareholders",
        description="股本结构与股东结构。",
        schema=_schema(
            {
                "security": {"type": "string"},
                "metrics": {"type": "array", "items": {"type": "string"}},
            },
            ["security"],
        ),
        cache_ttl_seconds=43200,
    ),
    "ifind.stock_events": ToolSpec(
        name="ifind.stock_events",
        server="stock",
        upstream="get_stock_events",
        description="分红、增发、回购、解禁、业绩预告等结构化事件指标。",
        schema=_schema(
            {
                "security": {"type": "string"},
                "period": {"type": "string"},
                "metrics": {"type": "array", "items": {"type": "string"}},
            },
            ["security"],
        ),
        cache_ttl_seconds=43200,
    ),
    "ifind.stock_risk": ToolSpec(
        name="ifind.stock_risk",
        server="stock",
        upstream="get_risk_indicators",
        description="beta、波动率、夏普、最大回撤等定量风险指标。",
        schema=_schema(
            {
                "security": {"type": "string"},
                "window": {"type": "string", "description": "如 过去1年"},
                "benchmark": {"type": "string", "description": "默认沪深300"},
            },
            ["security"],
        ),
        cache_ttl_seconds=43200,
    ),
    "ifind.notice_search": ToolSpec(
        name="ifind.notice_search",
        server="news",
        upstream="search_notice",
        source_kind="announcement",
        source_kind_label="公告",
        description="公告片段检索，显式 30 天窗口。",
        schema=_schema(
            {
                "security": {"type": "string"},
                "query": {"type": "string"},
                "days": {"type": "integer", "minimum": 1, "maximum": 365},
                "size": {"type": "integer", "minimum": 1, "maximum": 20},
            },
            ["query"],
        ),
        cache_ttl_seconds=600,
    ),
    "ifind.news_search": ToolSpec(
        name="ifind.news_search",
        server="news",
        upstream="search_news",
        source_kind="news",
        source_kind_label="资讯",
        description="新闻资讯检索，显式 30 天窗口。",
        schema=_schema(
            {
                "security": {"type": "string"},
                "topic": {"type": "string"},
                "query": {"type": "string"},
                "days": {"type": "integer", "minimum": 1, "maximum": 365},
                "size": {"type": "integer", "minimum": 1, "maximum": 20},
            },
            ["query"],
        ),
        cache_ttl_seconds=600,
    ),
    "ifind.macro": ToolSpec(
        name="ifind.macro",
        server="edb",
        upstream="get_edb_data",
        description="宏观与行业指标，先搜指标再取数。",
        schema=_schema({"query": {"type": "string"}, "period": {"type": "string"}}, ["query"]),
        cache_ttl_seconds=86400,
    ),
    "ifind.index": ToolSpec(
        name="ifind.index",
        server="index",
        upstream="index_data",
        description="指数或板块行情；kind=index/sector。",
        schema=_schema(
            {
                "target": {"type": "string"},
                "kind": {"type": "string", "enum": ["index", "sector"]},
                "query": {"type": "string"},
                "start": {"type": "string"},
                "end": {"type": "string"},
            },
            ["target", "kind"],
        ),
        cache_ttl_seconds=1800,
    ),
    "ifind.screen": ToolSpec(
        name="ifind.screen",
        server="stock",
        upstream="search_stocks",
        description="按条件筛选 A 股股票，返回代码列表。",
        schema=_schema({"query": {"type": "string"}}, ["query"]),
        cache_ttl_seconds=1800,
    ),
    "ifind.global_profile": ToolSpec(
        name="ifind.global_profile",
        server="global",
        upstream="global_stock_profile",
        description="港美股基本资料：所属行业、上市日期、发行价、公司简介。",
        schema=_schema(
            {
                "query": {
                    "type": "string",
                    "description": "股票名称/代码+指标，如「谷歌的所属行业、上市日期」",
                }
            },
            ["query"],
        ),
        cache_ttl_seconds=3600,
    ),
    "ifind.global_quotes": ToolSpec(
        name="ifind.global_quotes",
        server="global",
        upstream="global_stock_quotes",
        description="港美股行情与技术指标：股价、涨跌幅、成交额、换手率、市值。",
        schema=_schema(
            {
                "query": {
                    "type": "string",
                    "description": "股票名称/代码+时间+指标，如「苹果和特斯拉近10个交易日的涨跌幅」",
                }
            },
            ["query"],
        ),
        cache_ttl_seconds=180,
    ),
    "ifind.global_financials": ToolSpec(
        name="ifind.global_financials",
        server="global",
        upstream="global_stock_financial",
        description="港美股财务与估值：营业收入、净利润、毛利率、ROE、市盈率。",
        schema=_schema(
            {
                "query": {
                    "type": "string",
                    "description": "股票名称/代码+指标+报告期，如「Google 最新报告期的 ROE、利润增速」",
                }
            },
            ["query"],
        ),
        cache_ttl_seconds=1800,
    ),
    "ifind.global_events": ToolSpec(
        name="ifind.global_events",
        server="global",
        upstream="global_stock_events",
        description="港美股公告事件：IPO、回购、分红、ESG。",
        schema=_schema(
            {
                "query": {
                    "type": "string",
                    "description": "股票名称/代码+事件指标，如「minimax 的 IPO 日期、发行价」",
                }
            },
            ["query"],
        ),
        cache_ttl_seconds=1800,
    ),
    "ifind.global_screen": ToolSpec(
        name="ifind.global_screen",
        server="global",
        upstream="search_global_stocks",
        description="港美股条件选股，返回标的列表。",
        schema=_schema(
            {
                "query": {"type": "string", "description": "自然语言选股条件"},
                "market": {"type": "string", "description": "港股 或 美股，可留空"},
            },
            ["query"],
        ),
        cache_ttl_seconds=1800,
    ),
    "web.search": ToolSpec(
        name="web.search",
        server="web",
        upstream="search",
        provider="web",
        source_kind="search",
        source_kind_label="网页搜索结果",
        description=(
            "公开网页搜索。适合最新新闻、政策、行业动态、公司公告解读、"
            "管理层或机构观点、iFinD 未覆盖的背景信息。"
            "不适合行情、财报、股东、宏观等结构化数据（用 iFinD）。"
            "返回标题、链接、媒体、发布时间和摘要；正文只引用来源编号，媒体名、日期、链接由系统在来源区展示。"
        ),
        schema=_schema(
            {
                "query": {
                    "type": "string",
                    "minLength": 2,
                    "maxLength": 200,
                    "description": "具体搜索词，带公司名、行业或时间，不要整句照抄用户问题",
                },
                "count": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 10,
                    "default": 6,
                },
                "freshness": {
                    "type": "string",
                    "enum": ["oneDay", "oneWeek", "oneMonth", "oneYear", "noLimit"],
                    "default": "oneMonth",
                },
            },
            ["query"],
        ),
        cache_ttl_seconds=600,
    ),
}


def tool_specs() -> list[ToolSpec]:
    return list(TOOL_SPECS.values())


def get_tool(name: str) -> ToolSpec:
    if name not in TOOL_SPECS:
        raise KeyError(f"unknown tool: {name}")
    return TOOL_SPECS[name]


def public_tools() -> list[dict[str, Any]]:
    from app.config import get_settings

    web_ready = get_settings().web_search_ready
    return [
        {
            "name": spec.name,
            "description": spec.description,
            "server": spec.server,
            "upstream": spec.upstream,
            "provider": spec.provider,
            "schema": spec.schema,
        }
        for spec in tool_specs()
        if spec.provider != "web" or web_ready
    ]
