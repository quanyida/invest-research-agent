from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from app.tools.channels import resolve_channel


@dataclass(frozen=True)
class SourceDomainSpec:
    """iFinD 渠道下的数据域，用于区分“财报”“资讯”等独立出处。"""

    key: str
    label: str


SOURCE_DOMAINS: dict[str, SourceDomainSpec] = {
    "financials": SourceDomainSpec("financials", "iFinD 财报"),
    "quotes": SourceDomainSpec("quotes", "iFinD 行情"),
    "holders": SourceDomainSpec("holders", "iFinD 股东"),
    "events": SourceDomainSpec("events", "iFinD 事件"),
    "risk": SourceDomainSpec("risk", "iFinD 风险"),
    "macro": SourceDomainSpec("macro", "iFinD 宏观"),
    "index": SourceDomainSpec("index", "iFinD 指数"),
    "profile": SourceDomainSpec("profile", "iFinD 概况"),
    "screen": SourceDomainSpec("screen", "iFinD 选股"),
    "announcement": SourceDomainSpec("announcement", "iFinD 公告"),
    "news": SourceDomainSpec("news", "iFinD 资讯"),
    "other": SourceDomainSpec("other", "iFinD 数据"),
}

# 工具归属是产品口径。新增 iFinD 工具时必须在这里登记，否则归入 other。
TOOL_SOURCE_DOMAINS: dict[str, str] = {
    "ifind.stock_quote": "quotes",
    "ifind.stock_summary": "profile",
    "ifind.stock_financials": "financials",
    "ifind.stock_performance": "quotes",
    "ifind.stock_holders": "holders",
    "ifind.stock_events": "events",
    "ifind.stock_risk": "risk",
    "ifind.notice_search": "announcement",
    "ifind.news_search": "news",
    "ifind.macro": "macro",
    "ifind.index": "index",
    "ifind.screen": "screen",
    "ifind.global_profile": "profile",
    "ifind.global_quotes": "quotes",
    "ifind.global_financials": "financials",
    "ifind.global_events": "events",
    "ifind.global_screen": "screen",
}


def source_domain_for_tool(tool: str | None) -> SourceDomainSpec:
    return SOURCE_DOMAINS.get(TOOL_SOURCE_DOMAINS.get(str(tool or ""), ""), SOURCE_DOMAINS["other"])


# 只维护“网站主体 -> 展示名”。匹配时会忽略任意左侧子域。
WEBSITE_NAMES: dict[str, str] = {
    "sina.com.cn": "新浪财经",
    "eastmoney.com": "东方财富",
    "stcn.com": "证券时报",
    "cls.cn": "财联社",
    "nbd.com.cn": "每日经济新闻",
    "hexun.com": "和讯网",
    "sohu.com": "搜狐",
    "baijiahao.baidu.com": "百家号",
    "toutiao.com": "今日头条",
    "eeo.com.cn": "经济观察网",
    "stdaily.com": "中国科技网",
    "baike.baidu.com": "百度百科",
    "163.com": "网易财经",
    "qq.com": "腾讯财经",
    "yicai.com": "第一财经",
    "jiemian.com": "界面新闻",
    "thepaper.cn": "澎湃新闻",
    "21jingji.com": "21世纪经济报道",
    "cs.com.cn": "中国证券报",
    "cnstock.com": "上海证券报",
    "sina.cn": "新浪财经",
    "xueqiu.com": "雪球",
    "jrj.com.cn": "金融界",
    "ifeng.com": "凤凰网",
    "wallstreetcn.com": "华尔街见闻",
    "36kr.com": "36氪",
    "10jqka.com.cn": "同花顺",
    "tmtpost.com": "钛媒体",
    "zhihu.com": "知乎",
    "ithome.com": "IT之家",
}

_WEBSITE_SUFFIXES = sorted(WEBSITE_NAMES.items(), key=lambda item: len(item[0]), reverse=True)

_COMMON_SUBDOMAINS = {
    "www",
    "www2",
    "m",
    "mobile",
    "wap",
    "3g",
    "finance",
    "money",
    "stock",
    "stocks",
    "news",
    "data",
    "quote",
    "quotes",
    "auto",
    "tech",
    "blog",
    "mp",
    "forex",
    "futures",
    "caifu",
}


def _host(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    if "." not in text or " " in text:
        return ""
    if "://" not in text:
        text = "//" + text
    try:
        host = urlparse(text).netloc
    except Exception:
        return ""
    return host.split("@")[-1].split(":")[0].strip(".")


def _strip_common_subdomains(host: str) -> str:
    parts = host.lower().split(".")
    while len(parts) > 2 and parts[0] in _COMMON_SUBDOMAINS:
        parts.pop(0)
    return ".".join(parts)

# 公共后缀按标签数从多到少匹配，保证 com.cn 优先于 cn、co.uk 优先于 uk。
_PUBLIC_SUFFIXES = tuple(
    sorted(
        {
            "com.cn", "net.cn", "org.cn", "gov.cn", "edu.cn", "ac.cn",
            "com.hk", "com.tw", "com.mo", "com.sg", "com.au", "com.br",
            "com.mx", "com.tr", "co.uk", "org.uk", "ac.uk", "co.jp",
            "co.kr", "co.in", "co.nz", "co.za", "com", "cn", "net",
            "org", "co", "io", "ai", "me", "tv", "info", "biz", "dev",
            "app", "xyz", "top", "site", "online",
        },
        key=lambda item: (item.count("."), len(item)),
        reverse=True,
    )
)

def _readable_site_name(host: str) -> str:
    """把未登记域名转成确定性可读名称，不猜、不调用模型。"""
    parts = [part for part in _strip_common_subdomains(host).split(".") if part]
    if len(parts) < 2 or all(part.isdigit() for part in parts):
        return ""
    for suffix in _PUBLIC_SUFFIXES:
        suffix_parts = suffix.split(".")
        if parts[-len(suffix_parts):] == suffix_parts:
            parts = parts[: -len(suffix_parts)]
            break
    else:
        parts = parts[:-1]
    if not parts:
        return ""
    label = parts[-1].strip()
    if not label or label.startswith("xn--"):
        return ""
    label = label.replace("-", " ").strip()
    if not label:
        return ""
    return label[:1].upper() + label[1:]


def resolve_website(url: Any = None, publisher: Any = None) -> tuple[str, str]:
    """Return (site_key, display_name) without guessing or calling a model."""

    host = _host(url) or _host(publisher)
    if host:
        for domain, name in _WEBSITE_SUFFIXES:
            if host == domain or host.endswith("." + domain):
                return domain, name
        fallback = _strip_common_subdomains(host)
        return fallback, _readable_site_name(host) or "网页"
    text = str(publisher or "").strip()
    if text:
        return text.lower(), text
    return "", "网页"


def apply_source_naming(evidence: dict[str, Any]) -> dict[str, Any]:
    """Derive display name and same-source identity from registered metadata."""

    payload = dict(evidence or {})
    channel = resolve_channel(str(payload.get("channel") or ""))
    tool = str(payload.get("tool") or payload.get("source_tool") or "")
    payload["channel"] = channel.key
    payload["channel_label"] = channel.label
    payload["channel_kind"] = channel.kind
    payload["channel_kind_label"] = channel.kind_label

    if channel.key == "ifind":
        domain = source_domain_for_tool(tool)
        payload["source_domain"] = domain.key
        payload["source_domain_label"] = domain.label
        payload["source_key"] = f"ifind:{domain.key}"
        payload["source_name"] = domain.label
    elif channel.key == "web":
        site_key, site_name = resolve_website(
            payload.get("source_url"), payload.get("source_publisher")
        )
        payload["source_site"] = site_key or None
        payload["source_key"] = f"web:{site_key}" if site_key else ""
        payload["source_name"] = site_name or channel.label
    else:
        identity = tool or str(payload.get("citation_id") or payload.get("id") or "unknown")
        payload["source_key"] = f"{channel.key}:{identity}"
        payload["source_name"] = channel.label
    return payload