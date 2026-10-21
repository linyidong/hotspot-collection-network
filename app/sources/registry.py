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
    SourceSpec(
        id="sina", name="新浪财经7×24", type="api",
        path="sina", url="https://finance.sina.com.cn/7x24/"),
    # ---- T12 平台/需求侧信号（A 方案）----
    SourceSpec(
        id="gnews-kuajing", name="Google新闻·跨境电商", type="rss",
        path="gnews", channel="跨境电商",
        url="https://news.google.com/rss/search?q=%E8%B7%A8%E5%A2%83%E7%94%B5%E5%95%86&hl=zh-CN&gl=CN&ceid=CN:zh-Hans"),
    SourceSpec(
        id="gnews-xuanpin", name="Google新闻·选品爆款", type="rss",
        path="gnews", channel="选品 爆款",
        url="https://news.google.com/rss/search?q=%E9%80%89%E5%93%81%20%E7%88%86%E6%AC%BE&hl=zh-CN&gl=CN&ceid=CN:zh-Hans"),
    SourceSpec(
        id="gnews-guanshui", name="Google新闻·关税出海", type="rss",
        path="gnews", channel="关税 出海",
        url="https://news.google.com/rss/search?q=%E5%85%B3%E7%A8%8E%20%E5%87%BA%E6%B5%B7&hl=zh-CN&gl=CN&ceid=CN:zh-Hans"),
    SourceSpec(
        id="gnews-platform", name="Google新闻·平台政策", type="rss",
        path="gnews", channel="TikTok Shop Shopee Temu",
        url="https://news.google.com/rss/search?q=TikTok%20Shop%20OR%20Shopee%20OR%20Temu&hl=zh-CN&gl=CN&ceid=CN:zh-Hans"),
    SourceSpec(
        id="gnews-warehouse", name="Google新闻·海外仓物流", type="rss",
        path="gnews", channel="海外仓 物流",
        url="https://news.google.com/rss/search?q=%E6%B5%B7%E5%A4%96%E4%BB%93%20%E7%89%A9%E6%B5%81&hl=zh-CN&gl=CN&ceid=CN:zh-Hans"),
    SourceSpec(
        id="weibo-hot", name="微博热搜", type="json",
        path="weibo", channel="hot",
        url="https://weibo.com/ajax/side/hotSearch"),
    # ---- T12 平台榜单（C 方案，日更快照）----
    SourceSpec(
        id="amazon-kitchen", name="Amazon·厨房家居榜", type="snapshot",
        path="amazon-kitchen", channel="amazon-kitchen",
        url="https://www.amazon.com/gp/bestsellers/kitchen/"),
    SourceSpec(
        id="amazon-electronics", name="Amazon·电子配件榜", type="snapshot",
        path="amazon-electronics", channel="amazon-electronics",
        url="https://www.amazon.com/gp/bestsellers/electronics/"),
    SourceSpec(
        id="amazon-beauty", name="Amazon·美妆个护榜", type="snapshot",
        path="amazon-beauty", channel="amazon-beauty",
        url="https://www.amazon.com/gp/bestsellers/beauty/"),
]


def get_spec(source_id: str):
    """按 id 找源定义，找不到返回 None。"""
    for spec in SOURCES:
        if spec.id == source_id:
            return spec
    return None
