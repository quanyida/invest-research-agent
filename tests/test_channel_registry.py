from app.tools.channels import CHANNELS, channel_for_provider, resolve_channel
from app.tools.registry import get_tool


def test_provider_maps_to_channel():
    assert channel_for_provider("ifind").key == "ifind"
    assert channel_for_provider("web").key == "web"
    assert channel_for_provider("").key == "unknown"
    assert channel_for_provider(None).key == "unknown"
    assert channel_for_provider("broker").key == "unknown"


def test_resolve_channel_falls_back_to_unknown():
    assert resolve_channel("ifind").label == "iFinD"
    assert resolve_channel("web").label == "网页"
    assert resolve_channel("made-up").key == "unknown"
    assert resolve_channel("made-up").label == "未知来源"
    assert resolve_channel(None).label == "未知来源"


def test_tool_spec_channel_is_derived_from_provider():
    assert get_tool("ifind.stock_financials").channel == "ifind"
    assert get_tool("ifind.stock_financials").channel_label == "iFinD"
    assert get_tool("web.search").channel == "web"
    assert get_tool("web.search").channel_label == "网页"


def test_channel_registry_declares_required_metadata():
    for key, spec in CHANNELS.items():
        assert spec.key == key
        assert spec.label
        assert spec.kind
        assert spec.kind_label
    assert {"ifind", "web", "unknown"} <= set(CHANNELS)

def test_source_kind_is_derived_from_tool_registration():
    assert get_tool("ifind.stock_financials").source_kind_label == "结构化数据"
    assert get_tool("ifind.notice_search").source_kind_label == "公告"
    assert get_tool("ifind.news_search").source_kind_label == "资讯"
    assert get_tool("web.search").source_kind_label == "网页搜索结果"