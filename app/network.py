# -*- coding: utf-8 -*-
"""模块：出口网络探测（给「来源体检 / 采集模式切换」用）。
职责：查询本机当前出口 IP 与大致地区，判断「在国内 / 在境外」，
      结果缓存 10 分钟并落盘 data/network_cache.json，避免频繁请求。
输入输出：current(force=False) -> {"ip","country","region","is_cn","checked_at","source"}
关键依赖：httpx；失败时返回 ok=False 与中文原因，不影响其它功能。
"""
import json
from datetime import datetime, timedelta

import httpx

from .config import ROOT

CACHE_PATH = ROOT / "data" / "network_cache.json"
CACHE_MINUTES = 10
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0"}


def _read_cache():
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_cache(data: dict):
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def _probe():
    """依次尝试多个服务，返回最早成功的结果。"""
    errors = []
    with httpx.Client(timeout=8, follow_redirects=True, headers=UA) as client:
        # 1) ipinfo.io：ip + 国家 + 城市
        try:
            r = client.get("https://ipinfo.io/json")
            if r.status_code == 200:
                d = r.json()
                cc = (d.get("country") or "").upper()
                return {"ip": d.get("ip") or "", "country": cc,
                        "region": " ".join(x for x in (d.get("country"), d.get("city")) if x),
                        "is_cn": cc == "CN", "source": "ipinfo.io"}
        except Exception as exc:
            errors.append(f"ipinfo: {type(exc).__name__}")
        # 2) ip-api.com：免费、返回中文
        try:
            r = client.get("http://ip-api.com/json/?lang=zh-CN")
            if r.status_code == 200:
                d = r.json()
                cc = (d.get("countryCode") or "").upper()
                return {"ip": d.get("query") or "", "country": cc,
                        "region": " ".join(x for x in (d.get("country"), d.get("regionName")) if x),
                        "is_cn": cc == "CN", "source": "ip-api.com"}
        except Exception as exc:
            errors.append(f"ip-api: {type(exc).__name__}")
        # 3) 国内接口（纯文本）
        try:
            r = client.get("https://myip.ipip.net")
            if r.status_code == 200:
                text = r.text.strip()
                ip = ""
                for token in text.replace("：", " ").replace(":", " ").split():
                    if token.count(".") == 3 and token.replace(".", "").isdigit():
                        ip = token
                        break
                is_cn = "中国" in text
                return {"ip": ip, "country": "CN" if is_cn else "",
                        "region": text[:60], "is_cn": is_cn, "source": "myip.ipip.net"}
        except Exception as exc:
            errors.append(f"ipip: {type(exc).__name__}")
    raise RuntimeError("；".join(errors) or "全部探测服务不可用")


def current(force: bool = False) -> dict:
    """返回当前出口信息；force=True 忽略缓存。"""
    cached = _read_cache()
    if not force and cached.get("ok"):
        try:
            ts = datetime.strptime(cached["checked_at"], "%Y-%m-%d %H:%M:%S")
            if datetime.now() - ts < timedelta(minutes=CACHE_MINUTES):
                return cached
        except (KeyError, ValueError):
            pass
    try:
        info = _probe()
        info.update({"ok": True, "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
        _write_cache(info)
        return info
    except Exception as exc:
        if cached.get("ok"):
            cached["stale"] = True
            cached["error"] = f"{type(exc).__name__}: {exc}"
            return cached
        return {"ok": False, "error": f"探测失败：{type(exc).__name__}: {exc}",
                "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
