from __future__ import annotations

from dataclasses import dataclass

@dataclass(frozen=True)
class ChannelSpec:
    """Product-facing source channel.

    channel 是展示与统计口径，不是上游响应字段。新增渠道时：
    1. 在 CHANNELS 注册 key、展示名和类型；
    2. 在工具注册表里把相关 ToolSpec.provider 设为同一个 key。
    其余证据采集、SSE、落库、历史接口和前端渲染会自动跟随。
    """

    key: str
    label: str
    kind: str
    kind_label: str

CHANNELS: dict[str, ChannelSpec] = {
    "ifind": ChannelSpec(
        key="ifind",
        label="iFinD",
        kind="structured",
        kind_label="结构化数据",
    ),
    "web": ChannelSpec(
        key="web",
        label="网页",
        kind="search",
        kind_label="网页搜索结果",
    ),
    "unknown": ChannelSpec(
        key="unknown",
        label="未知来源",
        kind="unknown",
        kind_label="未知来源",
    ),
}

UNKNOWN_CHANNEL = CHANNELS["unknown"]

def channel_for_provider(provider: str | None) -> ChannelSpec:
    """Resolve a tool provider to its product channel without trusting tool output."""
    return CHANNELS.get(str(provider or ""), UNKNOWN_CHANNEL)

def resolve_channel(key: str | None) -> ChannelSpec:
    """Resolve a stored/request channel key, falling back to unknown."""
    return CHANNELS.get(str(key or ""), UNKNOWN_CHANNEL)