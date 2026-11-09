# -*- coding: utf-8 -*-
"""模块：搜索 API 封装（F-005 深挖）。职责：用 .env 的 SEARCH_API_KEY 调 Tavily
    检索，并从线索卡生成深挖检索词、按 URL 去重合并多轮结果。
    输出：results 列表 [{title,url,content,score,published_date}]。
    关键依赖：httpx（已有）、app.config、app.db、app.sources.registry。
    安全红线：任何日志/异常/返回值都不得包含搜索 Key 或代理凭据。"""
import httpx

from . import db
from .config import settings
from .sources.registry import SOURCES

TAVILY_ENDPOINT = "https://api.tavily.com/search"
MAX_QUERIES = 3                  # 每条线索卡最多检索词数


def configured() -> bool:
    """搜索 API Key 是否已配置（SEARCH_API_KEY 非空）。"""
    return bool((settings.SEARCH_API_KEY or "").strip())


def _proxy_url() -> str:
    """搜索请求的代理：SEARCH_PROXY_URL > PROXY_DEFAULT_URL > DB sources 表
    第一条非空 proxy_url（复用用户已配置的 Google 源代理）。
    全空返回空串（直连）。结果绝不写日志或返回给前端。"""
    p = (settings.SEARCH_PROXY_URL or "").strip()
    if p:
        return p
    p = (settings.PROXY_DEFAULT_URL or "").strip()
    if p:
        return p
    conn = db.get_conn()
    try:
        # 优先使用 Google×5 的代理（即「境外 SOCKS5」表单写入的地址），
        # 避免被其它源的 HTTP 代理抢先；再退回第一条非空代理。
        names = tuple(s.name for s in SOURCES
                      if s.id.startswith("gnews-"))
        if names:
            marks = ",".join("?" * len(names))
            row = conn.execute(
                f"SELECT proxy_url FROM sources WHERE name IN ({marks}) "
                "AND proxy_url IS NOT NULL AND proxy_url<>'' "
                "ORDER BY id LIMIT 1", names).fetchone()
        else:
            row = None
        if row is None:
            row = conn.execute(
                "SELECT proxy_url FROM sources "
                "WHERE proxy_url IS NOT NULL AND proxy_url<>'' "
                "ORDER BY id LIMIT 1").fetchone()
    finally:
        conn.close()
    return (row["proxy_url"] or "").strip() if row else ""


def tavily_search(query: str, max_results: int = 5) -> list:
    """调用 Tavily 检索一次，返回 [{title,url,content,score,published_date}]。

    未配置 Key → 中文 ValueError；HTTP/网络错误 → 中文 RuntimeError。
    错误信息只含异常类型，不含 Key、不含代理地址。"""
    key = (settings.SEARCH_API_KEY or "").strip()
    if not key:
        raise ValueError("未配置搜索 API Key（SEARCH_API_KEY）。"
                         "请把 Tavily Key 写入项目根目录 .env，并重启服务后再深挖")
    payload = {
        "api_key": key,
        "query": query,
        "search_depth": "basic",
        "max_results": max_results,
        "include_answer": False,
        "include_raw_content": False,
    }
    try:
        with httpx.Client(proxy=_proxy_url() or None, timeout=25,
                          follow_redirects=True) as client:
            resp = client.post(TAVILY_ENDPOINT, json=payload)
    except ImportError as e:
        raise RuntimeError(
            "搜索代理需要 socks 支持但本机缺少依赖（httpx[socks]）。"
            "请在设置里改用 http/https 代理，或安装依赖后重启服务") from e
    except httpx.HTTPError as e:
        raise RuntimeError(
            f"搜索请求失败（网络错误：{type(e).__name__}）。"
            "请检查代理与网络（境外访问需已启动的本地代理）后重试") from e
    if resp.status_code != 200:
        raise RuntimeError(
            f"搜索 API 返回 HTTP {resp.status_code}。"
            "请检查 SEARCH_API_KEY 是否有效、Tavily 额度是否充足")
    try:
        data = resp.json()
    except ValueError as e:
        raise RuntimeError("搜索 API 返回的不是 JSON，服务可能异常。请稍后重试") from e
    out = []
    for r in data.get("results") or []:
        url = (r.get("url") or "").strip()
        if not url:
            continue
        out.append({
            "title": (r.get("title") or "").strip(),
            "url": url,
            "content": (r.get("content") or "").strip(),
            "score": r.get("score"),
            "published_date": r.get("published_date") or "",
        })
    return out


def search_queries(card: dict) -> list:
    """从线索卡生成最多 3 条检索词：中/英文各至少一条；国家为空用「海外」。"""
    anchor = card.get("anchor") or {}
    country = (anchor.get("country") or "").strip() or "海外"
    category = (anchor.get("category") or "").strip()
    buyer = (card.get("buyer_hypothesis") or "").strip()

    if category:
        qs = [f"{country} {category} 买家 批发 报价",
              f"{category} {country} buyers wholesale price"]
    else:
        qs = [f"{country} 跨境电商 热销品类",
              f"{country} cross-border ecommerce trending products"]
    if buyer:
        qs.append(f"{buyer} supplier quote")

    seen, out = set(), []
    for q in qs:
        if q and q not in seen:
            seen.add(q)
            out.append(q)
    return out[:MAX_QUERIES]


def search_many(queries: list, per_query: int = 5):
    """逐条调用 tavily_search，按 URL 去重，最多取前 MAX_QUERIES 条检索词。

    返回 (results, queries_used)。单条查询网络失败不中断其余；
    全部失败则抛出最后一条中文 RuntimeError；Key 未配置直接抛 ValueError。"""
    queries = [q for q in queries if q][:MAX_QUERIES]
    seen, results, used, last_err = set(), [], [], None
    for q in queries:
        try:
            rs = tavily_search(q, max_results=per_query)
        except ValueError:
            raise
        except RuntimeError as e:
            last_err = e
            continue
        used.append(q)
        for r in rs:
            if r["url"] in seen:
                continue
            seen.add(r["url"])
            results.append(r)
    if not used and last_err is not None:
        raise last_err
    return results, used
