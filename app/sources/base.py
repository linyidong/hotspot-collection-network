# -*- coding: utf-8 -*-
"""模块：采集层统一接口。职责：定义 RawItem（原始条目）与源适配器基类；
    每个源实现 fetch() -> list[RawItem]，供 collect.py 并行调用。
    关键依赖：stdlib dataclasses；不依赖网络库。"""
from dataclasses import dataclass, field


@dataclass
class RawItem:
    """一条原始条目（进 items 表前的最小公共结构）。"""
    title: str
    url: str
    summary: str
    published_at: str  # 尽量归一化为 'YYYY-MM-DD HH:MM:SS'（本机时区），取不到给空串
    source_id: str     # 注册表里的源 id，如 'wscn-global'
    extra: dict = field(default_factory=dict)


class BaseSource:
    """源适配器基类。子类必须实现 fetch()。"""

    def __init__(self, spec, timeout: float = 20.0):
        self.spec = spec          # registry.SourceSpec
        self.timeout = timeout

    def fetch(self) -> list:
        """抓取本源最新条目。失败必须抛异常（原因由 collect.py 转中文入库）。
        返回：list[RawItem]。"""
        raise NotImplementedError
