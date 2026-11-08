# -*- coding: utf-8 -*-
"""模块：行情指标（F-015：金价 / 油价 / 汇率）。
职责：从新浪财经公开行情接口取 3 个报价，解析成统一结构并做 60 秒内存缓存；
     接口失败时返回「暂不可用」而不是抛异常，保证大屏不崩。
     主源：hq.sinajs.cn（黄金 hf_GC / WTI 原油 hf_CL / 在岸美元人民币 fx_susdcny）
     备源：腾讯 qt.gtimg.cn（金/油）+ open.er-api.com（汇率，日更）
输入输出：get_quotes() -> {ok, updated_at, source, items[3], error}
关键依赖：httpx、re、time。
"""
import re
import time
from datetime import datetime

import httpx

SINA_URL = "https://hq.sinajs.cn/list=hf_GC,hf_CL,fx_susdcny"
TENCENT_URL = "https://qt.gtimg.cn/q=hf_GC,hf_CL"
FX_FALLBACK_URL = "https://open.er-api.com/v6/latest/USD"
TIMEOUT = 8.0
TTL_OK = 60          # 成功缓存 60 秒
TTL_FAIL = 30        # 失败缓存 30 秒，避免接口挂掉时被前端打爆

_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Referer": "https://finance.sina.com.cn",
}

# 三个指标的定义（顺序即大屏展示顺序）
ITEMS = [
    {"key": "gold", "name": "金价", "unit": "美元/盎司"},
    {"key": "oil", "name": "油价", "unit": "美元/桶"},
    {"key": "fx", "name": "汇率", "unit": "USD/CNY"},
]

_CACHE = {"ts": 0.0, "data": None}


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _num(x):
    try:
        v = float(str(x).strip())
        return v if v == v else None       # 过滤 NaN
    except (TypeError, ValueError):
        return None


def _change(price, prev):
    if price is None or prev in (None, 0):
        return None
    return round((price - prev) / prev * 100, 2)


def _blank_items(error: str):
    """三个指标全部置为不可用（接口失败时的稳定结构，前端无需特判）。"""
    out = []
    for meta in ITEMS:
        out.append({"key": meta["key"], "name": meta["name"], "unit": meta["unit"],
                    "price": None, "change_pct": None, "ok": False,
                    "quote_at": None, "error": error})
    return out


def _unavailable(error: str, source: str = "—"):
    return {"ok": False, "updated_at": _now(), "source": source,
            "items": _blank_items(error), "error": error}


def _parse_sina(text: str):
    """解析新浪返回的 var hq_str_xxx="a,b,c"; 行，返回 {code: [fields]}。"""
    out = {}
    for m in re.finditer(r'var hq_str_(\w+)="([^"]*)"', text):
        out[m.group(1)] = [x.strip() for x in m.group(2).split(",")]
    return out


def _sina_quotes() -> list:
    """主源：新浪财经。任何一项解析失败只影响该项。"""
    with httpx.Client(timeout=TIMEOUT, headers=_HEADERS, follow_redirects=True) as client:
        resp = client.get(SINA_URL)
        resp.raise_for_status()
        text = resp.content.decode("gbk", errors="ignore")
    raw = _parse_sina(text)
    if not raw:
        raise ValueError("新浪行情返回为空")

    items = []
    # 黄金 / 原油：字段 0=最新价、7=昨结算、6=时间、12=日期、13=名称
    for code, key, name, unit in (("hf_GC", "gold", "金价", "美元/盎司"),
                                  ("hf_CL", "oil", "油价", "美元/桶")):
        f = raw.get(code) or []
        price = _num(f[0]) if len(f) > 0 else None
        prev = _num(f[7]) if len(f) > 7 else None
        quote_at = None
        if len(f) > 12 and f[12]:
            quote_at = (f[12] + " " + (f[6] if len(f) > 6 and f[6] else "")).strip()
        items.append({"key": key, "name": name, "unit": unit,
                      "price": price if (price and price > 0) else None,
                      "change_pct": _change(price, prev) if (price and price > 0) else None,
                      "ok": bool(price and price > 0),
                      "quote_at": quote_at or None,
                      "error": None if (price and price > 0) else "行情字段解析失败"})

    # 汇率：字段 8=最新价、3=昨收、9=名称
    f = raw.get("fx_susdcny") or []
    price = _num(f[8]) if len(f) > 8 else (_num(f[1]) if len(f) > 1 else None)
    prev = _num(f[3]) if len(f) > 3 else None
    items.append({"key": "fx", "name": "汇率", "unit": "USD/CNY",
                  "price": price if (price and price > 0) else None,
                  "change_pct": _change(price, prev) if (price and price > 0) else None,
                  "ok": bool(price and price > 0),
                  "quote_at": (f[17] if len(f) > 17 and f[17] else None),
                  "error": None if (price and price > 0) else "行情字段解析失败"})
    return items


def _tencent_quotes() -> list:
    """备源：腾讯行情（金/油）。第二字段就是涨跌幅。"""
    with httpx.Client(timeout=TIMEOUT, headers=_HEADERS, follow_redirects=True) as client:
        resp = client.get(TENCENT_URL)
        resp.raise_for_status()
        text = resp.content.decode("gbk", errors="ignore")
    out = []
    for code, key, name, unit in (("hf_GC", "gold", "金价", "美元/盎司"),
                                  ("hf_CL", "oil", "油价", "美元/桶")):
        m = re.search(r'v_' + code + r'="([^"]*)"', text)
        f = [x.strip() for x in m.group(1).split(",")] if m else []
        price = _num(f[0]) if f else None
        out.append({"key": key, "name": name, "unit": unit,
                    "price": price if (price and price > 0) else None,
                    "change_pct": _num(f[1]) if (price and price > 0 and len(f) > 1) else None,
                    "ok": bool(price and price > 0),
                    "quote_at": (f[12] + " " + f[6]).strip() if len(f) > 12 else None,
                    "error": None if (price and price > 0) else "备源解析失败"})
    return out


def _fx_fallback() -> dict:
    """备源：open.er-api（日更）。只给价格，不给日内涨跌幅。"""
    with httpx.Client(timeout=TIMEOUT, follow_redirects=True) as client:
        resp = client.get(FX_FALLBACK_URL)
        resp.raise_for_status()
        data = resp.json()
    price = _num((data.get("rates") or {}).get("CNY"))
    return {"key": "fx", "name": "汇率", "unit": "USD/CNY",
            "price": price if (price and price > 0) else None,
            "change_pct": None, "ok": bool(price and price > 0),
            "quote_at": data.get("time_last_update_utc"),
            "error": None if (price and price > 0) else "备源解析失败"}


def _fetch() -> dict:
    """取一次行情：主源新浪；新浪整体失败时用腾讯 + er-api 兜底。"""
    try:
        items = _sina_quotes()
        source = "新浪财经"
        error = None if all(x["ok"] for x in items) else "部分指标暂不可用"
    except Exception as exc:
        # 主源整体失败 → 备源
        try:
            tencent = _tencent_quotes()
            fx = _fx_fallback()
            by_key = {x["key"]: x for x in (tencent + [fx])}
            items = [by_key[k] for k in ("gold", "oil", "fx")]
            source = "腾讯财经 / er-api（备源）"
            error = None if all(x["ok"] for x in items) else "部分指标暂不可用"
        except Exception:
            return _unavailable(_human_error(exc), source="新浪财经（备源也失败）")
    return {"ok": any(x["ok"] for x in items), "updated_at": _now(),
            "source": source, "items": items, "error": error}


def _human_error(exc: Exception) -> str:
    if isinstance(exc, httpx.ConnectTimeout):
        return f"行情接口连接超时（>{int(TIMEOUT)} 秒）"
    if isinstance(exc, httpx.ReadTimeout):
        return f"行情接口读取超时（>{int(TIMEOUT)} 秒）"
    if isinstance(exc, httpx.ConnectError):
        return "行情接口连接失败（网络不可达）"
    if isinstance(exc, httpx.HTTPStatusError):
        return f"行情接口返回 HTTP {exc.response.status_code}"
    return f"行情接口异常：{type(exc).__name__}"


def get_quotes(force: bool = False) -> dict:
    """读取行情（带 60 秒缓存）。任何情况下都返回可 JSON 序列化的结构，不抛异常。"""
    now = time.time()
    cached = _CACHE.get("data")
    ttl = TTL_OK if (cached and cached.get("ok")) else TTL_FAIL
    if cached and not force and (now - _CACHE.get("ts", 0)) < ttl:
        return cached
    try:
        data = _fetch()
    except Exception as exc:                 # 最后一道保险：绝不让行情拖垮大屏
        data = _unavailable(_human_error(exc))
    _CACHE["ts"] = now
    _CACHE["data"] = data
    return data
