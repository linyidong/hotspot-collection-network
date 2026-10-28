"""把热点采集网络的 app.db 建成一个可检索的知识库（rag.db）。

设计取舍：
- 不用向量库/embedding：本机无外网依赖也能跑，查询结果可解释（命中了哪些词看得见）。
- 中文用「二元组切分 + FTS5」：FTS5 自带的 unicode61 不切中文，直接索引中文句子会把整句当一个词，
  所以入库前把中文切成 bigram（"锂电池" -> "锂电 电池"），查询时同样切，召回足够好且零依赖。
- 分三类文档入库，便于按 kind 过滤：deep（结构化线索卡）/ coarse（粗筛命中）/ news（有摘要的正文明）。

用法：
    python build_index.py            # 默认读 ../data/app.db，写 ./rag.db
    python build_index.py --rebuild  # 删除旧索引重建
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_SRC = os.path.abspath(os.path.join(HERE, "..", "data", "app.db"))
DEFAULT_DST = os.path.join(HERE, "rag.db")

CJK = re.compile(r"[\u4e00-\u9fa5]")
CHUNK = re.compile(r"[\u4e00-\u9fa5]+|[A-Za-z0-9]+")


def tokenize(text: str) -> str:
    """中文切 bigram，英文/数字整词保留。索引与查询必须用同一个函数。"""
    if not text:
        return ""
    out = []
    for ch in CHUNK.findall(text.lower()):
        if CJK.match(ch):
            if len(ch) == 1:
                out.append(ch)
            else:
                out.extend(ch[i : i + 2] for i in range(len(ch) - 1))
        else:
            out.append(ch)
    return " ".join(out)


def card_to_text(card: dict) -> str:
    """把 deep 阶段的结构化线索卡拼成一段可检索文本。"""
    if not isinstance(card, dict):
        return ""
    parts = []
    for key in ("fact", "signal", "buyer_hypothesis", "category"):
        v = card.get(key)
        if isinstance(v, str) and v:
            parts.append(v)
    anchor = card.get("anchor") or {}
    if isinstance(anchor, dict):
        parts.extend(str(v) for v in anchor.values() if isinstance(v, (str, int)))
    money = card.get("money_hypothesis") or {}
    if isinstance(money, dict):
        parts.extend(str(v) for k, v in money.items() if k != "status" and isinstance(v, str))
    for step in card.get("verify_plan") or []:
        if isinstance(step, str):
            parts.append(step)
    nxt = card.get("next_action") or {}
    if isinstance(nxt, dict):
        parts.extend(str(v) for v in nxt.values() if isinstance(v, str))
    return "；".join(p for p in parts if p)


def build(src: str, dst: str, rebuild: bool = False) -> None:
    if rebuild and os.path.exists(dst):
        os.remove(dst)
    con = sqlite3.connect(dst)
    cur = con.cursor()
    cur.executescript(
        """
        drop table if exists docs;
        drop table if exists docs_fts;
        create table docs(
            id integer primary key,
            kind text not null,
            item_id integer,
            title text,
            body text,
            url text,
            published_at text,
            source text
        );
        create virtual table docs_fts using fts5(title_tok, body_tok, tokenize='unicode61');
        """
    )

    s = sqlite3.connect(src)
    s.row_factory = sqlite3.Row
    sc = s.cursor()

    def src_name(sid):
        return SOURCE_MAP.get(sid, "")

    sc.execute("select id, name from sources")
    SOURCE_MAP = {row["id"]: row["name"] for row in sc.fetchall()}
    print(f"[源] 载入 {len(SOURCE_MAP)} 个数据源名称")

    docs = []

    # 1) deep：结构化线索卡
    rows = sc.execute(
        """
        select a.payload_json, i.id, i.title, i.url, i.published_at, i.source_id
        from analyses a join items i on i.id = a.item_id
        where a.stage='deep' and a.status='ok' and length(a.payload_json) > 60
        """
    ).fetchall()
    deep_n = 0
    for r in rows:
        try:
            payload = json.loads(r["payload_json"])
        except Exception:
            continue
        card = payload.get("card") if isinstance(payload, dict) else None
        body = card_to_text(card or {})
        if not body:
            continue
        docs.append(("deep", r["id"], r["title"] or "", body,
                     r["url"] or "", r["published_at"] or "", src_name(r["source_id"])))
        deep_n += 1
    print(f"[deep] 结构化线索卡 {deep_n} 条")

    # 2) coarse：粗筛命中（带锚点与潜力分）
    rows = sc.execute(
        """
        select a.payload_json, i.id, i.title, i.url, i.published_at, i.source_id
        from analyses a join items i on i.id = a.item_id
        where a.stage='coarse' and a.status='ok' and length(a.payload_json) > 20
        """
    ).fetchall()
    coarse_n = 0
    for r in rows:
        try:
            p = json.loads(r["payload_json"])
        except Exception:
            continue
        if not isinstance(p, dict) or not p.get("is_economic"):
            continue
        anchors = p.get("anchors") or {}
        body = "；".join(str(v) for v in anchors.values() if isinstance(v, (str, int)))
        body += f"；潜力分 {p.get('potential')}"
        docs.append(("coarse", r["id"], r["title"] or "", body,
                     r["url"] or "", r["published_at"] or "", src_name(r["source_id"])))
        coarse_n += 1
    print(f"[coarse] 经济相关粗筛 {coarse_n} 条")

    # 3) news：有实际摘要的正文明（排除榜单快照）
    rows = sc.execute(
        """
        select id, title, summary, url, published_at, source_id
        from items
        where length(coalesce(summary,'')) >= 60
          and coalesce(summary,'') not like '%榜单快照%'
        """
    ).fetchall()
    news_n = 0
    for r in rows:
        docs.append(("news", r["id"], r["title"] or "", r["summary"] or "",
                     r["url"] or "", r["published_at"] or "", src_name(r["source_id"])))
        news_n += 1
    print(f"[news] 有摘要原文 {news_n} 条")

    t0 = time.time()
    for kind, item_id, title, body, url, published_at, source in docs:
        cur.execute(
            "insert into docs(kind,item_id,title,body,url,published_at,source) values(?,?,?,?,?,?,?)",
            (kind, item_id, title, body, url, published_at, source),
        )
        rid = cur.lastrowid
        cur.execute(
            "insert into docs_fts(rowid,title_tok,body_tok) values(?,?,?)",
            (rid, tokenize(title), tokenize(body)),
        )
    con.commit()
    print(f"[索引] 写入 {len(docs)} 条，耗时 {time.time()-t0:.1f}s")

    cur.execute("insert into docs_fts(docs_fts) values('optimize')")
    con.commit()
    s.close()
    con.close()

    size = os.path.getsize(dst) / 1024 / 1024
    print(f"[完成] {dst}  ({size:.1f} MB)")
    print(f"[统计] deep={deep_n} coarse={coarse_n} news={news_n} 合计={len(docs)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=DEFAULT_SRC)
    ap.add_argument("--dst", default=DEFAULT_DST)
    ap.add_argument("--rebuild", action="store_true")
    args = ap.parse_args()
    if not os.path.exists(args.src):
        sys.exit(f"找不到源库：{args.src}")
    build(args.src, args.dst, args.rebuild)


if __name__ == "__main__":
    main()
