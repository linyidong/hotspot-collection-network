# -*- coding: utf-8 -*-
"""模块：T4 单轮流水线。职责：把「采集 → 建批次 → 落库 → 去重 → 粗筛 → 记账」串成
    run_once()，返回统计 dict 供 CLI 摘要与 batches.stats_json。
    去重：URL 归一化（去 utm_*/fbclid/锚点、host 小写）相同或标题相似度 ≥0.9 → 重复条；
    粗筛：先纯字符串硬过滤（T4.1，与 tools/baseline_test.py 同口径，命中=skipped 不进模型不计费），
    再 deepseek-flash + prompts/coarse.md，并发 6，单条失败重试 1 次，失败不阻塞。
    熔断：轮首查预算 mode——hard 整轮不调任何模型、soft 只跑粗筛跳过深加工；
    深加工每次调用前复检一次，防止轮内超支。
    关键依赖：app.sources.collect、app.llm、app.db、app.config。"""
import hashlib
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from difflib import SequenceMatcher
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from . import budget, candidates, db, dedup_topic, opportunity, settings_store
from .config import ROOT, settings
from .llm import chat
from .sources.collect import collect_once
from .sources.registry import SOURCES
from .validate import parse_deep_json, validate_deep_card

COARSE_PROMPT = (ROOT / "prompts" / "coarse.md").read_text(encoding="utf-8")
COARSE_MODEL = settings.COARSE_MODEL or "deepseek-flash"
COARSE_CONCURRENCY = 6          # 并发粗筛（任务书 4–8）

DEEP_PROMPT = (ROOT / "prompts" / "deep.md").read_text(encoding="utf-8")
DEEP_MODEL = settings.DEEP_MODEL or "deepseek-v4-pro"
DEEP_MAX_TOKENS = 1200          # 线索卡正文（关闭思考后无需给推理留空间）
DEEP_TIMEOUT = 90.0
DEEP_RETRIES = 2                # 失败重试次数（任务书 ≤2）
DEEP_LIMIT_DEFAULT = 10         # 每轮深加工上限（超出进候选池）

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
# 硬过滤（T4.1：与 tools/baseline_test.py 同口径，粗筛前的纯字符串闸，不进模型不计费）
# ---------------------------------------------------------------------------
HARD_FILTER_WORDS = ["早报", "日报", "综述", "快讯汇总", "投资者关系", "答记者问", "批复"]


def hard_filter(item: dict) -> "str | None":
    """标题/摘要命中硬过滤词 → 返回命中词；否则 None。命中条目不进模型，不计费。
    生产 item 无 content 字段（baseline 版检查 title+content+summary），这里查 title+summary；
    纯字符串包含判断，字段为 None 也不抛异常。"""
    text = f"{item.get('title') or ''} {item.get('summary') or ''}"
    for w in HARD_FILTER_WORDS:
        if w in text:
            return w
    return None


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


def mark_duplicates(rows: list, batch_id: int = None, cross_days: int = 7) -> int:
    """批内 + 跨批次去重：rows 为 [(item_id, url, title)] 按插入序；返回重复条数。
    保留先出现者：归一化 URL 相同 或 标题相似度 ≥0.9 → is_duplicate=1、duplicate_of=保留 id。
    跨批次：同 url_hash 在最近 cross_days 天的旧批次里出现过，同样判重（避免同一新闻反复花钱）。"""
    conn = db.get_conn()
    n_dup = 0
    try:
        # 跨批次：先取最近 N 天其它批次已入库的 url_hash → id
        old = {}
        if batch_id is not None:
            for r in conn.execute(
                    "SELECT url_hash, MIN(id) AS fid FROM items "
                    "WHERE batch_id != ? AND url_hash IS NOT NULL AND url_hash != '' "
                    "AND published_at >= datetime('now','localtime',?) "
                    "GROUP BY url_hash", (batch_id, f"-{int(cross_days)} day")).fetchall():
                old[r["url_hash"]] = r["fid"]
        if old:
            for i, row in enumerate(rows):
                if row[2] is None or not row[1]:
                    continue
                h = url_hash(row[1])
                if h and h in old:
                    conn.execute("UPDATE items SET is_duplicate=1, duplicate_of=? WHERE id=?",
                                 (old[h], row[0]))
                    rows[i] = (None, None, None)
                    n_dup += 1
            conn.commit()

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
    # F-004：经济条目必须给固定 6 类之一；非经济条目统一规范为 none
    if not data["is_economic"]:
        data["opportunity_type"] = opportunity.NONE
    else:
        data["opportunity_type"] = opportunity.normalize(
            data.get("opportunity_type"))
    return data


def coarse_one(item, batch_id: int, model: str = None) -> str:
    """粗筛单条（含 1 次失败重试），写 analyses + llm_calls。线程内自建连接。
    model 为空时用常量默认值（界面设置可覆盖）。返回 status：ok / skipped / failed。"""
    model = model or COARSE_MODEL
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
                text = chat(model, prompt, batch_id, stage="coarse",
                            max_tokens=200, conn=conn)
                data = _parse_coarse_output(text)
                status = "ok" if data["is_economic"] else "skipped"
                conn.execute(
                    "INSERT INTO analyses(item_id, stage, model, payload_json, status, cost, created_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (item["id"], "coarse", model,
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
            (item["id"], "coarse", model,
             json.dumps({"error": last_err}, ensure_ascii=False), "failed", 0.0, _now()))
        conn.commit()
        return "failed"
    finally:
        conn.close()


def _last_call_cost(conn, batch_id: int, stage: str = "coarse") -> float:
    """取本批最近一次指定 stage 调用成本（刚由 chat() 写入）。"""
    row = conn.execute(
        "SELECT cost FROM llm_calls WHERE batch_id=? AND stage=? "
        "ORDER BY id DESC LIMIT 1", (batch_id, stage)).fetchone()
    return row["cost"] if row else 0.0


def _human_last_error(exc: Exception) -> str:
    try:
        from .llm import _human_openai_error
        return _human_openai_error(exc)
    except Exception:
        return str(exc) or exc.__class__.__name__


def deep_one(item, coarse_res: dict, batch_id: int, model: str = None) -> dict:
    """深加工单条：调 deep 模型 → 容错解析 → 12 条校验（含 r11 能力闸）。
    model 为空时用常量默认值（界面设置可覆盖）。
    失败重试 ≤2 次（每次调用即记账）；校验拒绝不算失败，不重试。
    cost 返回本次所有尝试的累计（含「API 成功但 JSON 解析失败」的那次），
    与 llm_calls 本次新增记账一致，保证批次对账不漏算。
    返回 {'status': ok|rejected|failed, 'card':..., 'reasons': [...], 'cost': float}。"""
    user = (
        "# 新闻\n"
        f"id: {item['id']}\ntitle: {item['title']}\n"
        f"summary: {(item['summary'] or '')[:800]}\n"
        f"published_at: {item['published_at'] or ''}\n"
        f"source: {item['source_name']}\nurl: {item['url'] or ''}\n\n"
        f"# 粗筛结果\n{json.dumps(coarse_res, ensure_ascii=False)}\n"
    )
    conn = db.get_conn()
    last_err = ""
    total_cost = 0.0
    try:
        for attempt in range(DEEP_RETRIES + 1):
            try:
                text, call_cost, _call_id = chat(
                    model or DEEP_MODEL, user, batch_id, stage="deep",
                    system_prompt=DEEP_PROMPT, max_tokens=DEEP_MAX_TOKENS,
                    timeout=DEEP_TIMEOUT, thinking=False, conn=conn,
                    return_meta=True)
                conn.commit()   # 调用即记账：立即持久化本次 deep 调用（含失败，chat 已写 cost=0 行）
                total_cost += call_cost
                card = parse_deep_json(text)
                if card is None:
                    raise ValueError("深加工输出无法解析为 JSON")
                ok, reasons = validate_deep_card(card)
                return {"status": "ok" if ok else "rejected", "card": card,
                        "reasons": reasons, "cost": total_cost}
            except Exception as exc:
                conn.commit()
                last_err = _human_last_error(exc)
                if attempt < DEEP_RETRIES:
                    time.sleep(1.0 + attempt)
        return {"status": "failed", "card": None,
                "reasons": [last_err], "cost": total_cost}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 单轮流水线
# ---------------------------------------------------------------------------
def analyze_one(item_id: int) -> dict:
    """按需解析单条（界面「AI 解析」按钮）：读条目 + 最近一次粗筛结果 → deep_one → 落库。
    返回 {'status': ok|rejected|failed, 'card':..., 'reasons': [...]}。"""
    cfg = settings_store.load()
    bs = budget.status()
    if bs["mode"] != "ok":
        tip = {"hard": "预算硬停（剩余不足 2%）",
               "soft": "预算软停（剩余不足 10%）"}.get(bs["mode"], bs["mode"])
        raise ValueError(f"{tip}，已暂停按需 AI 解析；请提高月预算或等待下月重置")
    conn = db.get_conn()
    try:
        item = conn.execute(
            "SELECT i.id, i.batch_id, i.title, i.summary, i.published_at, i.url, "
            "s.name AS source_name FROM items i LEFT JOIN sources s ON s.id=i.source_id "
            "WHERE i.id=?", (item_id,)).fetchone()
        if item is None:
            raise ValueError("条目不存在")
        crow = conn.execute(
            "SELECT payload_json FROM analyses WHERE item_id=? AND stage='coarse' "
            "ORDER BY id DESC LIMIT 1", (item_id,)).fetchone()
        try:
            coarse = json.loads(crow["payload_json"]) if crow and crow["payload_json"] else {}
        except (ValueError, TypeError):
            coarse = {}
    finally:
        conn.close()

    res = deep_one(item, coarse, item["batch_id"], cfg["deep_model"])
    conn = db.get_conn()
    try:
        conn.execute(
            "INSERT INTO analyses(item_id, stage, model, payload_json, status, cost, created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (item_id, "deep", cfg["deep_model"],
             json.dumps({"card": res.get("card"), "reasons": res.get("reasons") or []},
                        ensure_ascii=False),
             res["status"], res.get("cost", 0.0), _now()))
        conn.commit()
    finally:
        conn.close()
    return res


def run_once(deep_limit: int = None, trigger: str = "手动") -> dict:
    """完整一轮：建批次 → 采集 → 落库 → 去重 → 粗筛 → 深加工 → 记账 → 收尾。
    deep_limit：本轮深加工上限（None 用默认 10；0 表示跳过深加工）。
    trigger：批次来源，'手动' 或 '自动'。返回 stats。"""
    t0 = time.time()
    cfg = settings_store.load()          # 界面设置（模型/限额），.env 仍优先级更高
    conn = db.get_conn()
    stats = {"fetched": 0, "after_dedup": 0, "coarse_ok": 0, "coarse_skipped": 0,
             "coarse_failed": 0, "hard_filtered": 0,
             "deep_ok": 0, "deep_rejected": 0,
             "deep_failed": 0, "deep_skipped": 0, "deep_candidates": 0,
             "deep_r11": 0, "cost": 0.0, "duration_s": 0.0,
             "sources_ok": 0, "sources_failed": 0,
             "failed_sources": [], "budget_stop": "ok", "deep_skipped_budget": 0,
             "candidate_pool_added": 0, "candidate_pool_evicted": 0,
             "candidate_pool_total": 0}
    try:
        # 0) 轮首预算熔断检查（hard → 整轮不调用任何模型；soft → 只跑粗筛）
        budget_mode = budget.status(conn=conn)["mode"]
        stats["budget_stop"] = budget_mode
        # 1) 建批次
        batch_id = conn.execute(
            "INSERT INTO batches(started_at, trigger, status) VALUES(?,?, 'running')",
            (_now(), trigger)).lastrowid
        conn.commit()

        # 2) 采集（含 sources 状态回写）
        # 采集前确保 RSS 桥可用（夜间桥僵死时自动重启，避免 5 个桥源整轮 502）
        try:
            from .bridge import bridge
            if not bridge.ensure():
                stats["bridge_before"] = "unavailable"
                print("[桥] 采集前检查：桥不可用，本轮桥源可能失败", flush=True)
        except Exception as exc:
            stats["bridge_before"] = f"error:{type(exc).__name__}"
        _t_collect = time.time()
        results = collect_once(conn=conn)
        stats["collect_s"] = round(time.time() - _t_collect, 1)

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
                stats["failed_sources"].append(spec.name)
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
        stats["after_dedup"] = stats["fetched"] - mark_duplicates(rows_for_dedup, batch_id)

        # 5) 粗筛（非重复条，并发 6，失败重试 1 次；预算硬停时整轮跳过）
        if stats["after_dedup"] > 0 and budget_mode != "hard":
            to_coarse = conn.execute(
                "SELECT i.id, i.title, i.summary, i.published_at, i.url, s.name AS source_name "
                "FROM items i JOIN sources s ON s.id=i.source_id "
                "WHERE i.batch_id=? AND i.is_duplicate=0 ORDER BY i.id",
                (batch_id,)).fetchall()

            # 5.1) 硬过滤（T4.1，与基准题同口径）：命中词条目不进模型、不计费、
            #      不提交线程池；条目保留在 items，只补一条 coarse=skipped 的分析记录。
            survivors = []
            for it in to_coarse:
                word = hard_filter(dict(it))
                if word is None:
                    survivors.append(it)
                    continue
                conn.execute(
                    "INSERT INTO analyses(item_id, stage, model, payload_json, "
                    "status, cost, created_at) VALUES(?,?,?,?,?,?,?)",
                    (it["id"], "coarse", cfg["coarse_model"],
                     json.dumps({"hard_filter": word,
                                 "reason": f"hard_filter:{word}"},
                                ensure_ascii=False),
                     "skipped", 0.0, _now()))
                stats["coarse_skipped"] += 1
                stats["hard_filtered"] += 1
            conn.commit()
            to_coarse = survivors

            def _run(item):
                return coarse_one(item, batch_id, cfg["coarse_model"])

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

        # 5.5) 深加工（只处理粗筛 is_economic=true 的条目，限额，超出进候选池；
        #      预算软停时跳过，每次调用前复检一次防止轮内超支）
        # 5.4) 事件级去重（借鉴 Horizon 的 topic_dedup）：同一件事的重复报道先合并，
        #      避免重复消耗深加工预算、也避免大屏出现多条同事件卡片
        stats["topic_merged"] = 0
        if stats["coarse_ok"] > 0 and budget_mode == "ok":
            try:
                stats["topic_merged"] = dedup_topic.merge_same_events(batch_id, cfg["coarse_model"])
            except Exception as exc:   # 去重失败不影响整轮，但要在统计里看得见
                stats["topic_merged_error"] = f"{type(exc).__name__}: {exc}"

        limit = cfg["deep_limit"] if deep_limit is None else max(0, int(deep_limit))

        # 先一次性取出本轮所有粗筛合格条目；不论是否深加工，都要落候选池。
        econ = []
        if stats["coarse_ok"] > 0:
            rows = conn.execute(
                "SELECT i.id, i.title, i.summary, i.published_at, i.url, "
                "s.name AS source_name, a.payload_json "
                "FROM items i JOIN sources s ON s.id=i.source_id "
                "JOIN analyses a ON a.item_id=i.id AND a.stage='coarse' "
                "WHERE i.batch_id=? AND i.is_duplicate=0 AND a.status='ok'",
                (batch_id,)).fetchall()
            for r in rows:
                try:
                    cp = json.loads(r["payload_json"] or "{}")
                except (ValueError, TypeError):
                    cp = {}
                if cp.get("is_economic") is False:
                    continue
                cat = ((cp.get("anchors") or {}).get("category") or "其他").strip() or "其他"
                econ.append({
                    "row": r, "coarse": cp,
                    "potential": int(cp.get("potential") or 0),
                    "category": cat,
                    "opportunity_type": cp.get("opportunity_type") or "",
                })

        def _persist_pool(entries):
            if not entries:
                return
            res = candidates.add_many(conn, batch_id, [
                {"item_id": e["row"]["id"], "potential": e["potential"],
                 "category": e["category"],
                 "opportunity_type": e["opportunity_type"]}
                for e in entries])
            stats["candidate_pool_added"] += res["inserted"]
            stats["candidate_pool_evicted"] += res["evicted"]
            stats["candidate_pool_total"] = res["total"]
            conn.commit()

        if limit <= 0 or budget_mode in ("soft", "hard"):
            # 没有深加工名额/预算熔断：全部进候选池，不丢条目
            _persist_pool(econ)
            stats["deep_skipped"] = len(econ)
            if budget_mode == "soft":
                stats["deep_skipped_budget"] = len(econ)
        else:
            # 按潜力分降序 + 品类配额（借鉴 Horizon 的 balanced digest，避免一个品类刷屏）
            pool = sorted(econ, key=lambda e: -e["potential"])
            per_cat = int(cfg.get("deep_per_category") or 0)
            picked, used = [], {}
            for e in pool:
                cat = e["category"]
                if per_cat and used.get(cat, 0) >= per_cat:
                    continue
                used[cat] = used.get(cat, 0) + 1
                picked.append(e)
                if len(picked) >= limit:
                    break
            picked_ids = {e["row"]["id"] for e in picked}
            _persist_pool([e for e in econ if e["row"]["id"] not in picked_ids])
            stats["deep_candidates"] = len(picked)
            stats["deep_skipped"] = max(0, len(econ) - len(picked))
            for idx, e in enumerate(picked):
                bs = budget.status(conn=conn)
                if bs["mode"] != "ok":
                    stats["budget_stop"] = bs["mode"]
                    stats["deep_skipped_budget"] = len(picked) - idx
                    # 预算中途熔断：剩余候选也进池，避免丢失
                    _persist_pool(picked[idx:])
                    break
                res = deep_one(e["row"], e["coarse"], batch_id, cfg["deep_model"])
                payload = {"card": res["card"], "reasons": res["reasons"]}
                conn.execute(
                    "INSERT INTO analyses(item_id, stage, model, payload_json, "
                    "status, cost, created_at) VALUES(?,?,?,?,?,?,?)",
                    (e["row"]["id"], "deep", cfg["deep_model"],
                     json.dumps(payload, ensure_ascii=False), res["status"],
                     res["cost"], _now()))
                conn.commit()
                key = {"ok": "deep_ok", "rejected": "deep_rejected"}.get(
                    res["status"], "deep_failed")
                stats[key] += 1
                if res["status"] == "rejected" and any(
                        str(x).startswith("r11") for x in res["reasons"]):
                    stats["deep_r11"] += 1

        # 6) 收尾：花费（本批 llm_calls 求和，与摘要完全一致）+ 批次状态
        cost_row = conn.execute(
            "SELECT COALESCE(SUM(cost),0) c FROM llm_calls WHERE batch_id=?",
            (batch_id,)).fetchone()
        stats["cost"] = round(cost_row["c"] or 0.0, 6)
        stats["duration_s"] = round(time.time() - t0, 1)
        # 状态优先级：全源不可用 > 预算硬停 > 部分源失败 > 正常
        enabled_n = conn.execute("SELECT COUNT(*) c FROM sources WHERE enabled=1").fetchone()["c"]
        if stats.get("sources_ok", 0) == 0 and stats.get("fetched", 0) == 0:
            if enabled_n == 0:
                status = "no_sources"
                stats["no_sources"] = True
                stats["note"] = "所有数据源都已停用"
            else:
                status = "no_network"
                stats["no_network"] = True
                stats["note"] = f"{enabled_n} 个启用的数据源全部失败（网络/代理/上游问题）"
        elif stats["budget_stop"] == "hard":
            status = "stopped_budget"
        elif stats["sources_failed"] > 0:
            status = "degraded"
        else:
            status = "done"
        conn.execute(
            "UPDATE batches SET finished_at=?, status=?, stats_json=? WHERE id=?",
            (_now(), status, json.dumps(stats, ensure_ascii=False), batch_id))
        conn.commit()
        stats["batch_id"] = batch_id
        stats["status"] = status
        return stats
    except Exception as exc:                      # 任何中途异常都要落库，不能只留空批次
        bid = locals().get("batch_id")
        stats["duration_s"] = round(time.time() - t0, 1)
        stats["error"] = f"{type(exc).__name__}: {exc}"
        stats["note"] = "本轮中途失败，已记录原因；请查看控制台日志后重试，或手动「一键采集」"
        try:
            if bid:
                conn.execute(
                    "UPDATE batches SET finished_at=?, status='failed', stats_json=? WHERE id=?",
                    (_now(), json.dumps(stats, ensure_ascii=False), bid))
            conn.commit()
        except Exception:
            pass
        stats["batch_id"] = bid
        stats["status"] = "failed"
        print(f"[批次 #{bid}] 失败：{stats['error']}", flush=True)
        return stats
    finally:
        conn.close()
