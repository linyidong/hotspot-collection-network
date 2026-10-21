# -*- coding: utf-8 -*-
"""模块：事件级去重（借鉴 Horizon 的 topic_dedup 思路）。
职责：把同一事件的多条报道合并为一个代表条目，其余标 is_duplicate=1 并记录
      duplicate_of，避免同一件事重复消耗深加工预算、污染大屏。
输入：batch_id、粗筛通过的条目（含潜力分与品类）。
输出：被合并的条数；失败时返回 0（不阻断整轮）。
关键依赖：app.llm.chat、app.db。
"""
import json
import re

from . import db
from .llm import chat

MAX_ITEMS = 120         # 送 LLM 分组的条目上限（控制成本）
MAX_TOKENS = 700

PROMPT = """下面是一批新闻条目的编号与标题（同一批次）。
请找出**描述同一件事**的条目（同一事件被多家媒体报道 / 同一政策的重复播报）。

要求：
1. 只合并"确实是同一件事"的，不确定就不要合并。
2. 每组只保留 1 条（保留信息量最大、最具体的），其余归入同组。
3. 不要输出与事件无关的分组；没有重复就返回空数组。

只输出 JSON，格式：
{"groups": [[编号, 编号, ...], ...]}

条目：
{items}
"""


def _parse_groups(text: str):
    """从模型输出里抠出 {"groups": [[...]]}；失败返回 None。"""
    if not text:
        return None
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except (ValueError, TypeError):
        return None
    groups = data.get("groups")
    return groups if isinstance(groups, list) else None


def _norm_title(t: str) -> str:
    """标题归一：去掉【】前缀、标点与空白，便于比较。"""
    t = re.sub(r"[【】]", "", t or "")          # 只去括号符号，保留里面的标题
    t = re.sub(r"[\s\W_]+", "", t)
    return t


def _bigrams(t: str) -> set:
    return {t[i:i + 2] for i in range(len(t) - 1)} if len(t) > 1 else {t}


def _similar(a: str, b: str) -> float:
    """包含度（overlap coefficient）：|A∩B| / min(|A|,|B|)。
    短标题被长标题完整包含时接近 1.0，适合识别「同事件 + 不同后缀」。"""
    A, B = _bigrams(a), _bigrams(b)
    if not A or not B or min(len(A), len(B)) < 6:
        return 0.0
    return len(A & B) / float(min(len(A), len(B)))


def merge_same_events(batch_id: int, model: str = None) -> int:
    """执行一轮事件级去重，返回被合并（标记为重复）的条目数。"""
    conn = db.get_conn()
    try:
        # 全量拉取（确定性去重要看全部），LLM 分组只用前 MAX_ITEMS 条
        rows = conn.execute(
            "SELECT i.id, i.title, a.payload_json FROM items i "
            "JOIN analyses a ON a.item_id=i.id AND a.stage='coarse' "
            "WHERE i.batch_id=? AND i.is_duplicate=0 AND a.status='ok' "
            "ORDER BY i.id DESC LIMIT 400", (batch_id,)).fetchall()
    finally:
        conn.close()

    if len(rows) < 4:
        return 0

    by_id = {r["id"]: r for r in rows}
    llm_rows = rows[:MAX_ITEMS]
    lines = []
    for idx, r in enumerate(llm_rows, 1):
        title = re.sub(r"\s+", " ", (r["title"] or "")).strip()[:80]
        lines.append(f"{idx}. {title}")

    text = chat(model, PROMPT.replace("{items}", "\n".join(lines)), batch_id,
                stage="dedup", max_tokens=MAX_TOKENS, thinking=False, timeout=60)
    groups = _parse_groups(text)
    if not groups:
        return 0

    merged = 0
    conn = db.get_conn()
    try:
        # 先做确定性兜底：标题高度相似的直接合并（LLM 偶尔会漏，如带【】前缀的同一条）
        norm = [(r["id"], _norm_title(r["title"])) for r in rows]
        kept = []
        for iid, nt in norm:
            dup_of = None
            for kid, knt in kept:
                if _similar(nt, knt) >= 0.85:
                    dup_of = kid
                    break
            if dup_of is None:
                kept.append((iid, nt))
            else:
                conn.execute(
                    "UPDATE items SET is_duplicate=1, duplicate_of=? WHERE id=? AND is_duplicate=0",
                    (dup_of, iid))
                merged += 1

        for group in groups:
            ids = []
            for g in group:
                try:
                    n = int(g)
                except (ValueError, TypeError):
                    continue
                if 1 <= n <= len(llm_rows):
                    ids.append(llm_rows[n - 1]["id"])
            if len(ids) < 2:
                continue
            keep = ids[0]
            for other in ids[1:]:
                if other == keep or other not in by_id:
                    continue
                conn.execute(
                    "UPDATE items SET is_duplicate=1, duplicate_of=? WHERE id=? AND is_duplicate=0",
                    (keep, other))
                merged += 1
        conn.commit()
    finally:
        conn.close()
    return merged
