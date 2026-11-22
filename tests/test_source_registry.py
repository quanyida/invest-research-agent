from app.tools.registry import TOOL_SPECS
from app.tools.source_registry import (
    SOURCE_DOMAINS,
    TOOL_SOURCE_DOMAINS,
    WEBSITE_NAMES,
    apply_source_naming,
    resolve_website,
    source_domain_for_tool,
)


def test_every_ifind_tool_has_a_registered_domain():
    for name, spec in TOOL_SPECS.items():
        if spec.provider != "ifind":
            continue
        assert name in TOOL_SOURCE_DOMAINS, name
        assert TOOL_SOURCE_DOMAINS[name] in SOURCE_DOMAINS


def test_domain_labels_match_product_wording():
    assert source_domain_for_tool("ifind.stock_financials").label == "iFinD 财报"
    assert source_domain_for_tool("ifind.stock_quote").label == "iFinD 行情"
    assert source_domain_for_tool("ifind.stock_holders").label == "iFinD 股东"
    assert source_domain_for_tool("ifind.stock_events").label == "iFinD 事件"
    assert source_domain_for_tool("ifind.stock_risk").label == "iFinD 风险"
    assert source_domain_for_tool("ifind.macro").label == "iFinD 宏观"
    assert source_domain_for_tool("ifind.index").label == "iFinD 指数"
    assert source_domain_for_tool("ifind.global_profile").label == "iFinD 概况"
    assert source_domain_for_tool("ifind.screen").label == "iFinD 选股"
    assert source_domain_for_tool("ifind.notice_search").label == "iFinD 公告"
    assert source_domain_for_tool("ifind.news_search").label == "iFinD 资讯"


def test_unknown_ifind_tool_falls_back_without_merging_into_known_domain():
    assert source_domain_for_tool("ifind.made_up").key == "other"
    assert source_domain_for_tool("ifind.made_up").label == "iFinD 数据"


def test_website_mapping_covers_required_outlets():
    required = {
        "sina.com.cn",
        "eastmoney.com",
        "stcn.com",
        "cls.cn",
        "nbd.com.cn",
        "hexun.com",
        "sohu.com",
        "baijiahao.baidu.com",
        "toutiao.com",
        "eeo.com.cn",
        "stdaily.com",
        "baike.baidu.com",
        "163.com",
        "qq.com",
        "yicai.com",
        "jiemian.com",
        "thepaper.cn",
        "21jingji.com",
        "cs.com.cn",
        "cnstock.com",
    }
    assert required <= set(WEBSITE_NAMES)


def test_subdomains_map_to_same_site():
    assert resolve_website("https://finance.sina.com.cn/a") == ("sina.com.cn", "新浪财经")
    assert resolve_website("https://sina.com.cn/b") == ("sina.com.cn", "新浪财经")
    assert resolve_website("https://www.eastmoney.com/a") == ("eastmoney.com", "东方财富")


def test_specific_subdomain_wins_over_parent_domain():
    assert resolve_website("https://baijiahao.baidu.com/a")[1] == "百家号"
    assert resolve_website("https://baike.baidu.com/item/x")[1] == "百度百科"


def test_unknown_domain_falls_back_to_readable_name():
    assert resolve_website("https://finance.example.com/a") == ("example.com", "Example")
    assert resolve_website("https://news.example.co.uk/a") == ("example.co.uk", "Example")

def test_unknown_domains_render_readable_names():
    cases = {
        "https://www.facebook.com/x": ("facebook.com", "Facebook"),
        "https://www.reuters.com/x": ("reuters.com", "Reuters"),
        "https://www.bloomberg.com/x": ("bloomberg.com", "Bloomberg"),
        "https://example.com.cn/x": ("example.com.cn", "Example"),
        "https://foo-bar.com/x": ("foo-bar.com", "Foo bar"),
    }
    for url, expected in cases.items():
        assert resolve_website(url) == expected, url
        assert resolve_website(url) == expected, url

def test_ip_host_falls_back_to_web_label():
    assert resolve_website("http://10.0.0.8:8080/a")[1] == "网页"


def test_publisher_domain_is_used_when_url_is_missing():
    assert resolve_website(None, "finance.sina.com.cn") == ("sina.com.cn", "新浪财经")


def test_readable_publisher_is_kept_as_is():
    assert resolve_website(None, "某财经媒体") == ("某财经媒体", "某财经媒体")


def test_missing_web_metadata_falls_back_to_web_label():
    assert resolve_website(None, None) == ("", "网页")


def test_apply_source_naming_for_ifind_domains():
    financials = apply_source_naming({"channel": "ifind", "tool": "ifind.stock_financials"})
    news = apply_source_naming({"channel": "ifind", "tool": "ifind.news_search"})

    assert financials["source_name"] == "iFinD 财报"
    assert financials["source_key"] == "ifind:financials"
    assert news["source_name"] == "iFinD 资讯"
    assert news["source_key"] == "ifind:news"


def test_apply_source_naming_for_web_site():
    payload = apply_source_naming(
        {
            "channel": "web",
            "tool": "web.search",
            "source_url": "https://finance.sina.com.cn/a",
        }
    )

    assert payload["source_name"] == "新浪财经"
    assert payload["source_key"] == "web:sina.com.cn"
    assert payload["source_site"] == "sina.com.cn"


def test_apply_source_naming_for_unknown_web_domain():
    payload = apply_source_naming(
        {"channel": "web", "tool": "web.search", "source_url": "https://finance.example.com/a"}
    )

    assert payload["source_name"] == "Example"


def test_apply_source_naming_keeps_cross_channel_keys_distinct():
    ifind = apply_source_naming({"channel": "ifind", "tool": "ifind.stock_financials"})
    web = apply_source_naming(
        {"channel": "web", "tool": "web.search", "source_url": "https://finance.sina.com.cn/a"}
    )
    unknown = apply_source_naming({"channel": "legacy", "tool": "x"})

    assert len({ifind["source_key"], web["source_key"], unknown["source_key"]}) == 3