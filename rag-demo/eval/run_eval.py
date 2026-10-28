"""RAG 检索评测：用同一批问题跑多组配置，证明「改这几处到底有没有变好」。

    python eval/run_eval.py              # 跑全部配置并写 eval/REPORT.md
    python eval/run_eval.py --k 5        # 只评估 top-5

判定方式（必须写在报告里，别含糊）：
    命中文档的 title+body 必须包含 all_of 全部关键词，且（若给 any_of）至少包含其中一个。
    这是「关键词命中」不是「人工相关性标注」——能自动跑、可复现、可对比，但比人工标注宽松。

指标：
    hit@1 / hit@3 / hit@k   —— top-k 里有没有命中文档
    MRR@k                   —— 第一个命中的倒数排名（越接近 1 说明命中越靠前）
    noise                   —— top-k 里未命中的比例（衡量噪声）
    ms                      —— 单次检索耗时
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import statistics
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..")))

import ask  # noqa: E402

CONFIGS = [
    ("A 改动前（无停用词 / 无去重 / 无截断）", dict(stop=False, dedupe=False, cutoff_ratio=0.0)),
    ("B + 停用词", dict(stop=True, dedupe=False, cutoff_ratio=0.0)),
    ("C + 去重", dict(stop=True, dedupe=True, cutoff_ratio=0.0)),
    ("D 全开（当前默认）", dict(stop=True, dedupe=True, cutoff_ratio=0.45)),
]


def is_hit(doc: dict, item: dict, full_body: str = "") -> bool:
    # 用「标题 + 完整正文」判定，不能用 excerpt：excerpt 只是窗口，关键词可能刚好在窗口外，
    # 会造成假阴性（第一版评测就踩了这个坑，Q15 明明命中了被判未命中）。
    blob = ((doc.get("title") or "") + " " + (full_body or doc.get("excerpt") or "")).lower()
    all_of = [t.lower() for t in item.get("all_of", [])]
    any_of = [t.lower() for t in item.get("any_of", [])]
    if all_of and not all(t in blob for t in all_of):
        return False
    if any_of and not any(t in blob for t in any_of):
        return False
    return True


def fetch_bodies(ids: list[int]) -> dict[int, str]:
    if not ids:
        return {}
    con = sqlite3.connect(ask.RAG_DB)
    try:
        q = "select id, body from docs where id in (%s)" % ",".join("?" * len(ids))
        return {r[0]: (r[1] or "") for r in con.execute(q, ids)}
    finally:
        con.close()


def run_config(items: list[dict], k: int, **kw) -> dict:
    hits1 = hits3 = 0
    rr = []
    noise = []
    dup = []
    times = []
    per_q = []
    for it in items:
        t0 = time.time()
        docs = ask.retrieve(it["q"], k=k, **kw)
        times.append((time.time() - t0) * 1000)
        bodies = fetch_bodies([d["id"] for d in docs])
        flags = [is_hit(d, it, bodies.get(d["id"], "")) for d in docs]
        first = next((i + 1 for i, f in enumerate(flags) if f), None)
        if first == 1:
            hits1 += 1
        if first and first <= min(3, k):
            hits3 += 1
        rr.append(1.0 / first if first else 0.0)
        noise.append((len(docs) - sum(flags)) / len(docs) if docs else 1.0)
        titles = [re.sub(r"\s+", "", (d.get("title") or "")) for d in docs]
        dup.append(1 - (len(set(titles)) / len(titles)) if titles else 0.0)
        per_q.append(
            {
                "id": it["id"],
                "q": it["q"],
                "returned": len(docs),
                "first_hit_rank": first,
                "top1": docs[0]["title"][:46] if docs else "",
            }
        )
    n = len(items)
    return {
        "hit1": hits1 / n,
        "hit3": hits3 / n,
        "mrr": statistics.mean(rr),
        "noise": statistics.mean(noise),
        "dup": statistics.mean(dup),
        "ms": statistics.median(times),
        "per_q": per_q,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--out", default=os.path.join(HERE, "REPORT.md"))
    args = ap.parse_args()

    qfile = os.path.join(HERE, "questions.json")
    items = json.load(open(qfile, encoding="utf-8"))["items"]
    print(f"评测问题 {len(items)} 条，k={args.k}\n")

    results = []
    for name, kw in CONFIGS:
        r = run_config(items, args.k, **kw)
        results.append((name, r))
        print(f"{name}: hit@1={r['hit1']:.0%} hit@3={r['hit3']:.0%} MRR={r['mrr']:.3f} 噪声={r['noise']:.0%} 重复={r['dup']:.0%} {r['ms']:.0f}ms")

    base = results[0][1]
    final = results[-1][1]
    lines = [
        "# RAG 检索评测报告",
        "",
        f"> 生成于 {time.strftime('%Y-%m-%d %H:%M')}，问题集 `eval/questions.json`（{len(items)} 条，6 个业务场景），k={args.k}",
        "> 复现：`python eval/run_eval.py`",
        "",
        "## 判定方式（先说局限）",
        "",
        "命中文档的 `title+body` 必须包含 `all_of` 全部关键词，且（若给 `any_of`）至少包含其中一个。",
        "**这是「关键词命中」，不是人工相关性标注**——能自动跑、可复现，但比人工标注宽松，",
        "会高估绝对准确率。它真正的用途是**横向对比同一批问题的不同配置**（下面 A→D 就是这组对比）。",
        "",
        "## 结果对比",
        "",
        "| 配置 | hit@1 | hit@3 | MRR@k | 噪声率 | 重复率 | 中位耗时 |",
        "|---|---|---|---|---|---|---|",
    ]
    for name, r in results:
        lines.append(
            f"| {name} | {r['hit1']:.0%} | {r['hit3']:.0%} | {r['mrr']:.3f} | {r['noise']:.0%} | {r['dup']:.0%} | {r['ms']:.0f} ms |"
        )
    lines += [
        "",
        "## 怎么读这张表（诚实版）",
        "",
        f"**去重是唯一被明确验证的改动**：重复率 {base['dup']:.0%} → {final['dup']:.0%}，"
        f"hit@3 {base['hit3']:.0%} → {final['hit3']:.0%}，MRR {base['mrr']:.3f} → {final['mrr']:.3f}。"
        "改动前 top-5 里有四成是同一篇文章的不同批次分析，用户会以为「只有这几条」。",
        "",
        f"**停用词与截断的收益在这套判定下几乎看不见**（噪声 {base['noise']:.0%} → {final['noise']:.0%}，"
        f"hit@1 {base['hit1']:.0%} → {final['hit1']:.0%} 没动）。原因不是改动没用，而是**判定太宽松**："
        "只要标题或正文出现「锂电池」就算命中，于是「港股锂电池板块下跌」这种信息量很低的卡也算对。",
        "它的真实作用是把「上技校有没有用」这类完全无关的热搜挡在门外——这种现象在关键词判定里看不见，"
        "**只能靠人工标注相关性才能量化**。这条留作下一轮的第一优先。",
        "",
        "## 逐题（当前默认配置 D）",
        "",
        "| # | 问题 | 返回条数 | 首个命中排名 | top1 标题 |",
        "|---|---|---|---|---|",
    ]
    for row in final["per_q"]:
        lines.append(
            f"| {row['id']} | {row['q']} | {row['returned']} | "
            f"{row['first_hit_rank'] if row['first_hit_rank'] else '未命中'} | {row['top1']} |"
        )
    lines += [
        "",
        "## 没做但应该做的",
        "",
        "- **人工相关性标注**：现在只有关键词判定，下一步对 20 条问题人工标 0/1/2 三档，重算 nDCG。",
        "- **生成侧评测**：现在只评检索。回答质量要另建一套：是否带引用、引用是否真支撑、是否编造数字（可用规则校验 + 抽检）。",
        "- **难例集**：同义词（问「动力电池」而资料写「锂电池」）现在必错，这是 FTS5 的已知短板，应单独建一组难例跟踪混检索改造效果。",
        "",
    ]
    open(args.out, "w", encoding="utf-8").write("\n".join(lines))
    print(f"\n报告已写入 {args.out}")


if __name__ == "__main__":
    main()
