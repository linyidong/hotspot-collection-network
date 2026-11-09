# -*- coding: utf-8 -*-
"""模块：F-004 机会类型枚举。职责：集中定义粗筛的 6 类机会类型与 none，
    供 prompts/coarse.md、pipeline 解析/校验和前端展示共用；禁止模型自创类型。
    关键依赖：无（纯常量）。"""

OPPORTUNITY_TYPES = (
    "跨境贸易差价",
    "政策/补贴红利",
    "平台规则变化带来的套利",
    "新技术落地早期",
    "消费趋势",
    "供应链缺货",
)
NONE = "none"
ALLOWED = OPPORTUNITY_TYPES + (NONE,)


def normalize(value) -> str:
    """把模型输出规范成枚举；非法值抛 ValueError（由粗筛重试/记失败，不静默放过）。"""
    s = str(value or "").strip()
    if s in ALLOWED:
        return s
    raise ValueError(f"opportunity_type 不是允许的 6 类之一：{s or '（空）'}")
