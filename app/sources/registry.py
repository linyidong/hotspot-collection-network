# -*- coding: utf-8 -*-
"""模块：源注册表。职责：集中定义 8 个采集源的元数据（id/name/type/path/channel/url）。
    type=api 走直连（华尔街见闻 3 频道），type=bridge 走本地 RSS 桥（8053）。
    本表是源的唯一真相；数据库 sources 表只存开关与代理配置，两者按 name 关联。
    关键依赖：stdlib dataclasses。"""
from dataclasses import dataclass


@dataclass(frozen=True)
class SourceSpec:
    id: str          # 稳定唯一 id，采集与 /api/sources 共用
    name: str        # 中文名，与 DB sources.name 关联
    type: str        # 'api'（直连）| 'bridge'（本地 RSS 桥）
    path: str        # bridge：桥上的路由路径；api：直连频道名（channel）
    channel: str = ""   # api 源的频道参数（global-channel 等）
    url: str = ""       # 源入口/主页，用于展示


# 直连华尔街见闻 3 频道（任务书实测：api-one.wallstcn.com 可达）
WSCN_API_BASE = "https://api-one.wallstcn.com/apiv1/content/lives"

# 本地 RSS 桥基础地址（端口来自配置 RSS_BRIDGE_PORT，默认 8053）
BRIDGE_BASE = "http://127.0.0.1:{port}"

SOURCES = [
    SourceSpec(
        id="wscn-global", name="华尔街见闻-全球", type="api",
        path="global-channel", channel="global-channel",
        url="https://wallstreetcn.com/live/global"),
    SourceSpec(
        id="wscn-a-stock", name="华尔街见闻-A股", type="api",
        path="a-stock-channel", channel="a-stock-channel",
        url="https://wallstreetcn.com/live/a-stock"),
    SourceSpec(
        id="wscn-forex", name="华尔街见闻-外汇", type="api",
        path="forex-channel", channel="forex-channel",
        url="https://wallstreetcn.com/live/forex"),
    SourceSpec(
        id="cls", name="财联社", type="bridge",
        path="/cls/telegraph", url="https://www.cls.cn/telegraph"),
    SourceSpec(
        id="eastmoney", name="东方财富", type="bridge",
        path="/eastmoney/kuaixun", url="https://kuaixun.eastmoney.com/"),
    SourceSpec(
        id="ths", name="同花顺", type="bridge",
        path="/ths/kuaixun", url="https://news.10jqka.com.cn/"),
    SourceSpec(
        id="jin10", name="金十", type="bridge",
        path="/jin10/flash", url="https://www.jin10.com/"),
    SourceSpec(
        id="wscn-rss", name="华尔街见闻RSS", type="bridge",
        path="/wallstreetcn/live", url="https://wallstreetcn.com/live"),
]


def get_spec(source_id: str):
    """按 id 找源定义，找不到返回 None。"""
    for spec in SOURCES:
        if spec.id == source_id:
            return spec
    return None
