# -*- coding: utf-8 -*-
"""模块：F-005 深挖加工。职责：基于最新 deep ok 线索卡，调 Tavily 检索
    （app.search），再调 DeepSeek 二次加工出「具体买家 + 报价」深挖卡；
    校验证据 URL 与买家名/报价依据后落 analyses(stage='deepdive')。
    输出：run(item_id) 返回 {ok,status,data,reasons,model,cost,analyzed_at,
    search_count,search_queries}。
    关键依赖：app.search、app.llm.chat、app.db、app.budget、prompts/deepdive.md。
    预算/调度检查在 API 端点做，本模块只做加工（run 内再复检一次预算）。"""
import json
import re
from datetime import datetime

from . import budget, db, search, settings_store
from .config import ROOT
from .llm import chat

DEEPDIVE_PROMPT = (ROOT / "prompts" / "deepdive.md").read_text(encoding="utf-8")
DEEPDIVE_MAX_TOKENS = 1500
DEEPDIVE_TIMEOUT = 90.0


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _parse_output(content: str):
    """解析模型输出为 dict；容忍 markdown 围栏；失败返回 None。"""
    s = (content or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    try:
        data = json.loads(s)
        return data if isinstance(data, dict) else None
    except (ValueError, TypeError):
        return None


def _validate(data: dict, results: list) -> list:
    """反编造校验：结构齐全 + 所有证据 URL 属于本次检索结果 +
    买家名/报价数字能在对应结果原文中找到。返回中文原因列表，空=通过。"""
    reasons = []
    by_url = {r["url"]: r for r in results}
    urls = set(by_url)

    if not isinstance(data.get("summary"), str) or not data["summary"].strip():
        reasons.append("缺少 summary（一句话深挖结论）")
    if not isinstance(data.get("limits"), str) or not data["limits"].strip():
        reasons.append("缺少 limits（中文说明本次还缺什么证据）")

    steps = data.get("next_steps")
    if not isinstance(steps, list) or not (3 <= len(steps) <= 6):
        n = len(steps) if isinstance(steps, list) else "非数组"
        reasons.append(f"next_steps 必须是 3–6 条（当前 {n}）")
    elif any(not isinstance(s, str) or not s.strip() for s in steps):
        reasons.append("next_steps 每条必须是非空字符串")

    buyers = data.get("buyer_candidates")
    if not isinstance(buyers, list):
        reasons.append("buyer_candidates 必须是数组（可为空）")
    else:
        for i, b in enumerate(buyers, 1):
            if not isinstance(b, dict):
                reasons.append(f"buyer_candidates[{i}] 不是对象")
                continue
            name = str(b.get("name") or "").strip()
            url = str(b.get("evidence_url") or "").strip()
            if not name:
                reasons.append(f"buyer_candidates[{i}] 缺 name")
            if url not in urls:
                reasons.append(f"buyer_candidates[{i}] 证据链接不在本次检索结果中：{url or '（空）'}")
                continue
            if name:
                text = (by_url[url]["title"] + " " + by_url[url]["content"]).lower()
                if name.lower() not in text:
                    reasons.append(f"买家名无依据：「{name}」未出现在该检索结果原文中（{url}）")

    quotes = data.get("quote_references")
    if not isinstance(quotes, list):
        reasons.append("quote_references 必须是数组（可为空）")
    else:
        for i, q in enumerate(quotes, 1):
            if not isinstance(q, dict):
                reasons.append(f"quote_references[{i}] 不是对象")
                continue
            price = str(q.get("price") or "").strip()
            url = str(q.get("source_url") or "").strip()
            if not price:
                reasons.append(f"quote_references[{i}] 缺 price")
            if url not in urls:
                reasons.append(f"quote_references[{i}] 来源链接不在本次检索结果中：{url or '（空）'}")
                continue
            if price:
                nums = re.findall(r"\d+(?:\.\d+)?", price)
                text = by_url[url]["title"] + " " + by_url[url]["content"]
                if not nums or not all(n in text for n in nums):
                    reasons.append(f"报价无依据：「{price}」的数字未出现在该检索结果原文中（{url}）")
    return reasons


def _record(item_id: int, model: str, payload: dict, status: str, cost: float):
    """落库一条 deepdive 分析（ok/rejected/failed 都留痕，不静默）。"""
    conn = db.get_conn()
    try:
        conn.execute(
            "INSERT INTO analyses(item_id, stage, model, payload_json, status, cost, created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (item_id, "deepdive", model,
             json.dumps(payload, ensure_ascii=False), status, cost, _now()))
        conn.commit()
    finally:
        conn.close()


def run(item_id: int) -> dict:
    """深挖一条：检索 → 模型加工 → 校验 → 落库。前置不满足抛中文 ValueError。

    返回 {ok,status,data,reasons,model,cost,analyzed_at,search_count,search_queries}；
    status 取 ok / rejected / failed。"""
    cfg = settings_store.load()
    bs = budget.status()
    if bs["mode"] != "ok":
        mode_cn = {"hard": "硬停", "soft": "软停"}.get(bs["mode"], bs["mode"])
        raise ValueError(f"预算{mode_cn}（本月剩余 ¥{bs['remaining']:.2f} / "
                         f"预算 ¥{bs['budget']:.2f}），深挖已暂停；"
                         "请在大屏设置里调整月度预算或等下月重置")

    conn = db.get_conn()
    try:
        item = conn.execute(
            "SELECT id, batch_id FROM items WHERE id=?", (item_id,)).fetchone()
        if item is None:
            raise ValueError("条目不存在，请回大屏确认条目 ID")
        drow = conn.execute(
            "SELECT payload_json FROM analyses WHERE item_id=? AND stage='deep' "
            "AND status='ok' ORDER BY id DESC LIMIT 1", (item_id,)).fetchone()
    finally:
        conn.close()
    if drow is None:
        raise ValueError("该条还没有可深挖的线索卡，请先「重新加工」并通过校验")
    card = {}
    try:
        card = (json.loads(drow["payload_json"] or "{}") or {}).get("card") or {}
    except (ValueError, TypeError):
        card = {}
    if not (card.get("fact") or card.get("anchor")):
        raise ValueError("该条还没有可深挖的线索卡，请先「重新加工」并通过校验")

    results, used = search.search_many(search.search_queries(card))
    if not results:
        raise ValueError("检索没有返回结果，换个时间/网络再试")

    model = cfg["deep_model"]
    sources = [r["url"] for r in results]
    user_prompt = ("# 线索卡\n" + json.dumps(card, ensure_ascii=False) +
                   "\n\n# 检索结果（唯一可用证据；evidence_url/source_url 必须"
                   "原样使用其中的 url）\n" + json.dumps(results, ensure_ascii=False))
    analyzed_at = _now()
    try:
        content, cost, _cid = chat(
            model, user_prompt, item["batch_id"], stage="deepdive",
            system_prompt=DEEPDIVE_PROMPT, max_tokens=DEEPDIVE_MAX_TOKENS,
            timeout=DEEPDIVE_TIMEOUT, thinking=False,
            response_format={"type": "json_object"}, return_meta=True)
    except Exception as exc:
        _record(item_id, model,
                {"data": None,
                 "reasons": [f"模型调用失败：{type(exc).__name__}：{str(exc)[:120]}"],
                 "search_queries": used, "search_count": len(results),
                 "sources": sources},
                "failed", 0.0)
        raise

    data = _parse_output(content)
    if data is None:
        reasons = ["深挖输出无法解析为 JSON，本次作废，可重试"]
        _record(item_id, model,
                {"data": None, "reasons": reasons, "search_queries": used,
                 "search_count": len(results), "sources": sources},
                "failed", cost)
        return {"ok": False, "status": "failed", "data": None, "reasons": reasons,
                "model": model, "cost": cost, "analyzed_at": analyzed_at,
                "search_count": len(results), "search_queries": used}

    reasons = _validate(data, results)
    status = "ok" if not reasons else "rejected"
    _record(item_id, model,
            {"data": data, "reasons": reasons, "search_queries": used,
             "search_count": len(results), "sources": sources},
            status, cost)
    return {"ok": status == "ok", "status": status, "data": data,
            "reasons": reasons, "model": model, "cost": cost,
            "analyzed_at": analyzed_at, "search_count": len(results),
            "search_queries": used}
