# -*- coding: utf-8 -*-
"""模块：业务配置存取。职责：把界面可改的参数集中到 data/config.json，
    提供带校验的读取/保存；环境变量（.env）优先级更高，这里只存"界面可改"的部分。
    关键依赖：json、app.config.ROOT。"""
import json
import os

from .config import ROOT

CONFIG_PATH = ROOT / "data" / "config.json"

# 默认值：与 PRD 一致（预算 200 元、深加工 10 条/轮、3 小时一轮）
DEFAULTS = {
    "coarse_model": "deepseek-flash",
    "deep_model": "deepseek-v4-pro",
    "deep_limit": 10,
    "deep_per_category": 3,
    "run_interval_minutes": 0,      # 0 = 每日 08/11/14/17/20/23（默认）；>0 = 自定义间隔
    "monthly_budget_cny": 200.0,
    "budget_stop_ratio": 0.10,      # 剩余 <10% 停深加工
    "budget_hard_ratio": 0.02,      # 剩余 <2% 停所有模型调用
    "item_retention_days": 90,
    "startup_catchup_rounds": 1,     # 开机/启动时最多补跑几轮（0–5）
}

# 已知可用的模型名（新增模型时在这里补一行；写错名字会在保存时被拒）
KNOWN_MODELS = ["deepseek-flash", "deepseek-v4-pro", "deepseek-chat", "deepseek-reasoner"]

# 允许写入的键 + 类型/范围
_RULES = {
    "coarse_model": (str, None),
    "deep_model": (str, None),
    "deep_limit": (int, (0, 100)),
    "deep_per_category": (int, (0, 20)),
    "run_interval_minutes": (int, (0, 1440)),
    "startup_catchup_rounds": (int, (0, 5)),
    "monthly_budget_cny": (float, (1, 100000)),
    "budget_stop_ratio": (float, (0, 1)),
    "budget_hard_ratio": (float, (0, 1)),
    "item_retention_days": (int, (1, 3650)),
}


def load() -> dict:
    """读配置；文件缺失/损坏时返回默认值（不抛异常）。"""
    data = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        try:
            raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for k in DEFAULTS:
                    if k in raw:
                        data[k] = raw[k]
        except (ValueError, OSError):
            pass
    return data


def save(updates: dict) -> tuple:
    """校验并保存；返回 (ok, data_or_error)。只接受已知键。"""
    data = load()
    for key, value in (updates or {}).items():
        if key not in _RULES:
            return False, f"未知配置项：{key}"
        typ, rng = _RULES[key]
        try:
            value = typ(value)
        except (TypeError, ValueError):
            return False, f"{key} 的类型不对，应为 {typ.__name__}"
        if rng and not (rng[0] <= value <= rng[1]):
            return False, f"{key} 超出范围 {rng[0]}–{rng[1]}"
        data[key] = value
    for key in ("coarse_model", "deep_model"):
        name = str(data.get(key) or "").strip()
        if not name:
            return False, f"{key} 不能为空"
        if name not in KNOWN_MODELS:
            return False, (f"{key} 不是已知模型：{name}。"
                           f"可用：{'、'.join(KNOWN_MODELS)}（如需新增请在 settings_store.KNOWN_MODELS 补充）")
        data[key] = name
    if data["budget_hard_ratio"] >= data["budget_stop_ratio"]:
        return False, "硬停阈值必须小于软停阈值（hard < stop）"
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, CONFIG_PATH)
    return True, data


def get(key: str):
    return load().get(key, DEFAULTS.get(key))
