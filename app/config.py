# -*- coding: utf-8 -*-
"""模块：配置层。职责：按「.env > data/config.json > 代码默认值」合并读取配置；
    缺 DEEPSEEK_API_KEY 不退出，只标记 key_configured=false。输出：Settings 单例。
    关键依赖：python-dotenv；不依赖数据库。"""
import json
import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

# 项目版本（health 接口与控制台摘要共用）
VERSION = "0.1.0"

# 工作目录根（本文件位于 <root>/app/config.py）
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
CONFIG_JSON = DATA_DIR / "config.json"
DB_PATH = DATA_DIR / "app.db"
STATIC_DIR = ROOT / "static"

# 代码默认值（优先级最低）
DEFAULTS = {
    "DEEPSEEK_BASE_URL": "https://api.deepseek.com",
    "COARSE_MODEL": "deepseek-flash",
    "DEEP_MODEL": "deepseek-v4-pro",
    "PORT": "8010",
    "RSS_BRIDGE_PORT": "8053",
    "PROXY_DEFAULT_URL": "",
    "MONTHLY_BUDGET_CNY": "200",
    "DEEP_LIMIT_PER_RUN": "10",
    "RUN_INTERVAL_HOURS": "3",
    "ITEM_RETENTION_DAYS": "90",
}

# 需要从 .env 读取的键（其余业务键来自 data/config.json）
ENV_KEYS = ("DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "COARSE_MODEL",
            "DEEP_MODEL", "PORT", "RSS_BRIDGE_PORT", "PROXY_DEFAULT_URL")


def mask_key(key: str) -> str:
    """打码 API Key：保留前 4 后 4，中间用 ****；空值返回 '（未设置）'。"""
    if not key:
        return "（未设置）"
    return key[:4] + "****" + key[-4:]


def _read_config_json() -> dict:
    """读 data/config.json 的业务参数；文件缺失返回 {}，损坏时回退默认并提示。"""
    if not CONFIG_JSON.exists():
        return {}
    try:
        data = json.loads(CONFIG_JSON.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError) as e:
        print(f"[config] 警告：{CONFIG_JSON} 读取失败（{e}），已回退到默认值。")
        return {}


@lru_cache(maxsize=1)
def get_settings() -> "Settings":
    """加载并合并配置，返回 Settings 单例（.env > config.json > 默认值）。"""
    load_dotenv(ROOT / ".env", override=False)
    file_cfg = _read_config_json()
    merged = dict(DEFAULTS)
    merged.update({k: str(v) for k, v in file_cfg.items() if k in DEFAULTS or k in ENV_KEYS})
    for k in ENV_KEYS:
        env_v = os.environ.get(k)
        if env_v is not None:
            merged[k] = env_v
    return Settings(merged)


class Settings:
    """配置访问对象：属性化取值；业务参数用 as_dict 供后续界面读写。"""

    def __init__(self, merged: dict):
        self._m = merged
        self.VERSION = VERSION
        self.DEEPSEEK_API_KEY = merged.get("DEEPSEEK_API_KEY", "") or ""
        self.key_configured = bool(self.DEEPSEEK_API_KEY) and not self.DEEPSEEK_API_KEY.startswith("sk-xxx")

    def __getattr__(self, name):
        if name in self._m:
            return self._m[name]
        raise AttributeError(name)

    @property
    def port(self) -> int:
        try:
            return int(self._m.get("PORT", "8000"))
        except ValueError:
            return 8000

    @property
    def db_path(self) -> Path:
        return DB_PATH

    @property
    def static_dir(self) -> Path:
        return STATIC_DIR

    def as_dict(self) -> dict:
        """业务参数全量（供 /api/settings 等后续使用），不含密钥。"""
        return {k: v for k, v in self._m.items() if k != "DEEPSEEK_API_KEY"}


settings = get_settings()
