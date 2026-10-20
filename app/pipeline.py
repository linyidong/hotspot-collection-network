# -*- coding: utf-8 -*-
"""模块：T4 单轮流水线。职责：把「采集 → 建批次 → 落库 → 去重 → 粗筛 → 记账」串成
    run_once()，返回统计 dict 供 CLI 摘要与 batches.stats_json。
    去重：URL 归一化（去 utm_*/fbclid/锚点、host 小写）相同或标题相似度 ≥0.9 → 重复条；
    粗筛：deepseek-flash + prompts/coarse.md，并发 6，单条失败重试 1 次，失败不阻塞。
    关键依赖：app.sources.collect、app.llm、app.db、app.config。"""
import hashlib
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from difflib import SequenceMatcher
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from . import db
from .config import ROOT, settings
from .llm import chat
from .sources.collect import collect_once
from .sources.registry import SOURCES

COARSE_PROMPT = (ROOT / "prompts" / "coarse.md").read_text(encoding="utf-8")
COARSE_MODEL = settings.COARSE_MODEL or "deepseek-flash"
COARSE_CONCURRENCY = 6          # 并发粗筛（任务书 4–8）

# source_id(注册表) → region 标签（落库用）
_REGION = {
    "wscn-global": "全球", "wscn-a-stock": "中国", "wscn-forex": "全球",
    "cls": "中国", "eastmoney": "中国", "ths": "中国",
    "jin10": "中国", "wscn-rss": "中国",
}

# 需剔除的追踪参数（小写匹配前缀）
_TRACKING_PARAMS = {"utm_source", "utm_medium", "utm_campaign", "utm_term",
                    "utm_content", "fbclid", "gclid", "mc_cid", "mc_eid",
                    "igshid", "spm"}


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# 去重
# ---------------------------------------------------------------------------
def normalize_url(url: str) -> str:
    """URL 归一化：host 小写、去追踪参数（utm_*/fbclid 等）、去锚点、参数排序。
    归一化结果用于 url_hash 与去重。"""
    if not url:
        return ""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return url.strip().lower()
    scheme = (parts.scheme or "http").lower()
    netloc = (parts.netloc or "").lower()
    path = parts.path or "/"
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if k.lower() not in _TRACKING_PARAMS and not k.lower().startswith("utm_")]
    query.sort()
    qs = urlencode(query)
    return urlunsplit((scheme, netloc, path, qs, ""))  # fragment 置空


def url_hash(url: str) -> str:
    """归一化 URL 的 sha256（hex）。"""
    norm = normalize_url(url)
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


def title_similar(a: str, b: str) -> float:
    """标题相似度 0–1（difflib）。"""
    return SequenceMatcher(None, a or "", b or "").ratio()


def mark_duplicates(rows: list) -> int:
    """批内去重：rows 为 [(item_id, url, title)] 按插入序；返回重复条数。
    保留先出现者：归一化 URL 相同 或 标题相似度 ≥0.9 → is_duplicate=1、duplicate_of=保留 id。"""
    conn = db.get_conn()
    n_dup = 0
    try:
        for i in range(len(rows)):
            if rows[i][2] is None:          # 已被标记重复，跳过
                continue
            keep_id, keep_url, keep_title = rows[i]
            for j in range(i + 1, len(rows)):
                cand_id, cand_url, cand_title = rows[j]
                if cand_id is None:          # 已被标记重复，跳过
                    continue
                if (keep_url and cand_url and keep_url == cand_url) \
                        or title_similar(keep_title, cand_title) >= 0.9:
                    conn.execute(
                        "UPDATE items SET is_duplicate=1, duplicate_of=? WHERE id=?",
                        (keep_id, cand_id))
                    rows[j] = (None, None, None)   # 标记为已处理
                    n_dup += 1
        conn.commit()
    finally:
        conn.close()
    return n_dup


# ---------------------------------------------------------------------------
# 粗筛
# ---------------------------------------------------------------------------
def _parse_coarse_output(text: str) -> dict:
    """宽松解析模型 JSON：剥 markdown 围栏，取第一个 { 到最后一个 }。"""
    if not text:
        raise ValueError("模型输出为空")
    s = text.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    start, end = s.find("{"), s.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("输出中找不到 JSON 对象")
    data = json.loads(s[start:end + 1])
    if not isinstance(data.get("is_economic"), bool):
        raise ValueError("is_economic 不是布尔")
    anchors = data.get("anchors")
    if not isinstance(anchors, dict):
        raise ValueError("anchors 不是对象")
    if not isinstance(data.get("potential"), int) and \
            not (isinstance(data.get("potential"), float)):
        raise ValueError("potential 不是数字")
    return data


def coarse_one(item, batch_id: int) -> str:
    """粗筛单条（含 1 次失败重试），写 analyses + llm_calls。线程内自建连接。
    返回 status：ok / skipped / failed。"""
    conn = db.get_conn()
    try:
        prompt = (
            COARSE_PROMPT + "\n\n# 新闻\n"
            f"id: {item['id']}\ntitle: {item['title']}\n"
            f"summary: {(item['summary'] or '')[:500]}\n"
            f"published_at: {item['published_at'] or ''}\n"
            f"source: {item['source_name']}\nurl: {item['url'] or ''}\n"
        )
        last_err = ""
        for attempt in range(2):  # 首次 + 重试 1 次
            try:
                text = chat(COARSE_MODEL, prompt, batch_id, stage="coarse",
                            max_tokens=200, conn=conn)
                data = _parse_coarse_output(text)
                status = "ok" if data["is_economic"] else "skipped"
                conn.execute(
                    "INSERT INTO analyses(item_id, stage, model, payload_json, status, cost, created_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (item["id"], "coarse", COARSE_MODEL,
                     json.dumps(data, ensure_ascii=False), status,
                     _last_call_cost(conn, batch_id), _now()))
                conn.commit()
                return status
            except Exception as exc:
                last_err = _human_last_error(exc)
                if attempt == 0:
                    time.sleep(0.3)
        conn.execute(
            "INSERT INTO analyses(item_id, stage, model, payload_json, status, cost, created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (item["id"], "coarse", COARSE_MODEL,
             json.dumps({"error": last_err}, ensure_ascii=False), "failed", 0.0, _now()))
        conn.commit()
        return "failed"
    finally:
        conn.close()


def _last_call_cost(conn, batch_id: int) -> float:
    """取本批最近一次粗筛调用成本（刚由 chat() 写入）。"""
    row = conn.execute(
        "SELECT cost FROM llm_calls WHERE batch_id=? AND stage='coarse' "
        "ORDER BY id DESC LIMIT 1", (batch_id,)).fetchone()
    return row["cost"] if row else 0.0


def _human_last_error(exc: Exception) -> str:
    try:
        from .llm import _human_openai_error
        return _human_openai_error(exc)
    except Exception:
        return str(exc) or exc.__class__.__name__


# ---------------------------------------------------------------------------
# 单轮流水线
# ---------------------------------------------------------------------------
def run_once() -> dict:
    """完整一轮：建批次 → 采集 → 落库 → 去重 → 粗筛 → 记账 → 收尾。返回 stats。"""
    t0 = time.time()
    conn = db.get_conn()
    stats = {"fetched": 0, "after_dedup": 0, "coarse_ok": 0, "coarse_skipped": 0,
             "coarse_failed": 0, "cost": 0.0, "duration_s": 0.0,
             "sources_ok": 0, "sources_failed": 0}
    try:
        # 1) 建批次
        batch_id = conn.execute(
            "INSERT INTO batches(started_at, trigger, status) VALUES(?,?, 'running')",
            (_now(), "手动")).lastrowid
        conn.commit()

        # 2) 采集（含 sources 状态回写）
        results = collect_once(conn=conn)

        # 3) 落库（全部条目入库，重复标记留到去重）
        src_id_by_name = {r["name"]: r["id"] for r in conn.execute(
            "SELECT id, name FROM sources").fetchall()}
        rows_for_dedup = []      # [(item_id, normalized_url, title)]
        for spec in SOURCES:
            r = results.get(spec.id)
            if not r or r.get("skipped"):
                continue
            if r["error"] is not None:
                stats["sources_failed"] += 1
                continue
            stats["sources_ok"] += 1
            for raw in r["items"]:
                cur = conn.execute(
                    "INSERT INTO items(batch_id, source_id, title, url, url_hash, "
                    "summary, published_at, region, is_duplicate) "
                    "VALUES(?,?,?,?,?,?,?,?,0)",
                    (batch_id, src_id_by_name.get(spec.name), raw.title, raw.url,
                     url_hash(raw.url), raw.summary, raw.published_at,
                     _REGION.get(spec.id, "中国"))).lastrowid
                stats["fetched"] += 1
                rows_for_dedup.append((cur, normalize_url(raw.url), raw.title))
        conn.commit()

        # 4) 去重（标记 is_duplicate / duplicate_of）
        stats["after_dedup"] = stats["fetched"] - mark_duplicates(rows_for_dedup)

        # 5) 粗筛（非重复条，并发 6，失败重试 1 次）
        if stats["after_dedup"] > 0:
            to_coarse = conn.execute(
                "SELECT i.id, i.title, i.summary, i.published_at, i.url, s.name AS source_name "
                "FROM items i JOIN sources s ON s.id=i.source_id "
                "WHERE i.batch_id=? AND i.is_duplicate=0 ORDER BY i.id",
                (batch_id,)).fetchall()

            def _run(item):
                return coarse_one(item, batch_id)

            with ThreadPoolExecutor(max_workers=COARSE_CONCURRENCY) as pool:
                for fut in as_completed([pool.submit(_run, it) for it in to_coarse]):
                    st = fut.result()
                    if st == "ok":
                        stats["coarse_ok"] += 1
                    elif st == "skipped":
                        stats["coarse_skipped"] += 1
                    else:
                        stats["coarse_failed"] += 1
            conn.commit()

        # 6) 收尾：花费（本批 llm_calls 求和，与摘要完全一致）+ 批次状态
        cost_row = conn.execute(
            "SELECT COALESCE(SUM(cost),0) c FROM llm_calls WHERE batch_id=?",
            (batch_id,)).fetchone()
        stats["cost"] = round(cost_row["c"] or 0.0, 6)
        stats["duration_s"] = round(time.time() - t0, 1)
        status = "degraded" if stats["sources_failed"] > 0 else "done"
        conn.execute(
            "UPDATE batches SET finished_at=?, status=?, stats_json=? WHERE id=?",
            (_now(), status, json.dumps(stats, ensure_ascii=False), batch_id))
        conn.commit()
        stats["batch_id"] = batch_id
        stats["status"] = status
        return stats
    finally:
        conn.close()
