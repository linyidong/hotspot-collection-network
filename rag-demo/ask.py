"""在 rag.db 上做检索 + 生成（RAG）。

用法：
    python ask.py "东南亚储能组装厂最近有什么机会"        # 检索 + 调模型回答
    python ask.py "锂电池出口" --no-llm                   # 只检索（离线可跑，不花 token）
    python ask.py "..." --k 8 --kind deep                 # 只看结构化线索卡

设计要点：
- 检索与生成分离：检索永远可离线跑，模型挂了也能给出「命中了哪些资料」，不至于整个功能不可用。
- 强制引用：prompt 里要求每个结论后标 [编号]，回答里不带编号即视为不合格。
- 资料不足必须说不足：prompt 明确禁止用常识补全成事实。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
RAG_DB = os.path.join(HERE, "rag.db")
ENV_PATH = os.path.abspath(os.path.join(HERE, "..", ".env"))

from build_index import tokenize  # 索引与查询必须共用同一个切词函数

CJK = re.compile(r"[\u4e00-\u9fa5]")

# 疑问词/功能词的 bigram：它们对"相关性"没有贡献，却会把无关文档拉进结果（实测："有没有" 召回了
# "上技校有没有用" 这条完全无关的热搜）。检索时直接丢掉。
STOP_BIGRAMS = {
    "有没", "没有", "哪些", "什么", "怎么", "最近", "可以", "是不", "不是",
    "我们", "这个", "那个", "有什", "么产", "一下", "有哪", "哪些", "如何",
}


def load_env(path: str = ENV_PATH) -> dict:
    env = {}
    if not os.path.exists(path):
        return env
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def fts_query(q: str, stop: bool = True) -> str:
    toks = [t for t in tokenize(q).split() if not (stop and t in STOP_BIGRAMS)]
    if not toks:
        return ""
    # 去重保序
    seen, uniq = set(), []
    for t in toks:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
        if len(uniq) >= 24:  # 控制表达式长度
            break
    return " OR ".join(f'"{t}"' for t in uniq)


def window(body: str, q: str, width: int = 90) -> str:
    """在原文本里截一段包含查询词的窗口，用于展示（比 FTS 的 token 片段可读）。"""
    if not body:
        return ""
    for tok in sorted(tokenize(q).split(), key=len, reverse=True):
        idx = body.find(tok)
        if idx >= 0:
            s = max(0, idx - width // 2)
            return ("…" if s > 0 else "") + body[s : s + width] + ("…" if s + width < len(body) else "")
    return body[:width] + ("…" if len(body) > width else "")


def retrieve(
    q: str,
    k: int = 8,
    kind: str | None = None,
    stop: bool = True,
    dedupe: bool = True,
    cutoff_ratio: float = 0.45,
) -> list[dict]:
    """检索。stop/dedupe/cutoff_ratio 可关，用于评测里做 A/B 对比（关掉即"改动之前的行为"）。"""
    if not os.path.exists(RAG_DB):
        raise SystemExit(f"找不到索引 {RAG_DB}，先跑 python build_index.py")
    expr = fts_query(q, stop=stop)
    if not expr:
        return []
    sql = """
        select d.id, d.kind, d.title, d.body, d.url, d.published_at, d.source,
               bm25(docs_fts) as score
        from docs_fts f join docs d on d.id = f.rowid
        where docs_fts match ?
    """
    params: list = [expr]
    if kind:
        sql += " and d.kind = ?"
        params.append(kind)
    want = max(k * 3, 12) if (dedupe or cutoff_ratio > 0) else k
    sql += " order by score limit ?"
    params.append(want)  # 需要去重/截断时多取一些

    con = sqlite3.connect(RAG_DB)
    con.row_factory = sqlite3.Row
    rows = con.execute(sql, params).fetchall()
    con.close()

    uniq = sorted(rows, key=lambda r: r["score"])

    if dedupe:
        # 去重：同一篇文章可能被多个批次重复分析；榜单类同款商品每天生成一条（标题相同、url 带不同快照参数）。
        # 所以按「规范化标题」去重，而不是按 url——url 去重会被快照参数骗过去。保留分数最高的一条。
        def _key(r):
            t = re.sub(r"\s+", "", (r["title"] or "")).strip().lower()
            if not t:
                return (r["url"] or "").strip() or f"__{r['id']}"
            return t[:40]

        best: dict[str, sqlite3.Row] = {}
        for r in uniq:
            dk = _key(r)
            if dk not in best or r["score"] < best[dk]["score"]:  # bm25 越小越相关
                best[dk] = r
        uniq = sorted(best.values(), key=lambda r: r["score"])

    # 相对分数截断：只保留与最佳结果同量级的，去掉靠 OR 拼出来的噪声尾巴
    if uniq and cutoff_ratio > 0:
        cutoff = uniq[0]["score"] * cutoff_ratio
        uniq = [r for r in uniq if r["score"] <= cutoff]
    uniq = uniq[:k]

    out = []
    for i, r in enumerate(uniq, 1):
        out.append(
            {
                "n": i,
                "id": r["id"],
                "kind": r["kind"],
                "title": r["title"],
                "excerpt": window(r["body"], q),
                "url": r["url"],
                "published_at": r["published_at"],
                "source": r["source"],
                "score": round(-r["score"], 4),  # bm25 越负越相关，取正值便于展示
            }
        )
    return out


SYSTEM = (
    "你是跨境电商选品与行情分析助手。只允许依据【资料】回答，不得引入资料之外的事实。"
    "每条结论后面必须用 [编号] 标注依据；资料不足以支撑时，直接说「资料不足」，"
    "并说明还缺什么数据。不要编造数字、公司名与日期。"
)


def build_prompt(q: str, hits: list[dict]) -> str:
    blocks = []
    for h in hits:
        blocks.append(
            f"[{h['n']}] {h['title']}\n"
            f"    时间：{h['published_at']}  来源：{h['source']}  类型：{h['kind']}\n"
            f"    内容：{h['excerpt']}"
        )
    return (
        f"问题：{q}\n\n"
        f"【资料】（共 {len(hits)} 条，按相关度排序）\n" + "\n".join(blocks) + "\n\n"
        "请输出：\n"
        "1) 结论：3 条以内，每条带 [编号]；\n"
        "2) 依据：指出哪条资料支撑了哪句；\n"
        "3) 缺口：还缺什么信息才能下判断。"
    )


def _chat(base: str, key: str, model: str, prompt: str, timeout: int) -> str:
    """所有 OpenAI 兼容端点通用（DeepSeek / 硅基流动 / Ollama / LM Studio 都是这个形状）。"""
    body = json.dumps(
        {
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
            "stream": False,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{base.rstrip('/')}/chat/completions",
        data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data["choices"][0]["message"]["content"]


def call_llm(prompt: str, env: dict, timeout: int = 60) -> str:
    key = env.get("DEEPSEEK_API_KEY")
    base = env.get("DEEPSEEK_BASE_URL") or "https://api.deepseek.com"
    model = env.get("DEEP_MODEL") or env.get("COARSE_MODEL") or "deepseek-chat"
    if not key:
        raise RuntimeError("没有 DEEPSEEK_API_KEY（检查项目根目录 .env）")
    return _chat(base, key, model, prompt, timeout)


def call_local(prompt: str, env: dict, timeout: int = 180) -> str:
    """本地模型兜底：任何 OpenAI 兼容的本地服务都能用。

    Ollama:    LOCAL_LLM_BASE=http://127.0.0.1:11434/v1  LOCAL_LLM_MODEL=qwen2.5:7b
    LM Studio: LOCAL_LLM_BASE=http://127.0.0.1:1234/v1   LOCAL_LLM_MODEL=qwen2.5-7b-instruct
    没配 LOCAL_LLM_BASE 就直接报错（不做静默降级，否则会让人误以为在用云端）。
    """
    base = env.get("LOCAL_LLM_BASE")
    if not base:
        raise RuntimeError("没有配置 LOCAL_LLM_BASE（见 rag-demo/README.md 第五节）")
    model = env.get("LOCAL_LLM_MODEL") or "qwen2.5:7b"
    key = env.get("LOCAL_LLM_KEY") or "local"
    return _chat(base, key, model, prompt, timeout)


def answer(prompt: str, env: dict) -> tuple[str, str]:
    """先云端，失败再本地兜底。返回值带「谁答的」，避免把本地答案当成云端结果。"""
    try:
        return call_llm(prompt, env), "deepseek"
    except Exception as e_cloud:
        try:
            return call_local(prompt, env), "local"
        except Exception as e_local:
            raise RuntimeError(f"云端失败：{e_cloud}；本地也未配置/不可用：{e_local}") from e_local


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("question")
    ap.add_argument("--k", type=int, default=8)
    ap.add_argument("--kind", default=None, help="deep / coarse / news")
    ap.add_argument("--no-llm", action="store_true", help="只检索，不调模型")
    args = ap.parse_args()

    hits = retrieve(args.question, args.k, args.kind)
    print(f"\n=== 检索到 {len(hits)} 条 ===")
    for h in hits:
        print(f"[{h['n']}] ({h['kind']}/{h['source']}) {h['title'][:60]}")
        print(f"     {h['excerpt'][:110]}")
        print(f"     score={h['score']}  {h['published_at']}")

    if args.no_llm:
        return
    if not hits:
        print("\n没有命中资料，不调用模型。")
        return

    print("\n=== 生成回答 ===")
    try:
        text, engine = answer(build_prompt(args.question, hits), load_env())
        if engine == "local":
            print("[注意] 云端不可用，本条由本地模型生成，质量可能下降")
        print(text)
    except urllib.error.URLError as e:
        print(f"[模型不可用] {e}；检索结果仍然有效（这就是检索与生成分离的意义）")
    except Exception as e:
        print(f"[生成失败] {e}")


if __name__ == "__main__":
    main()
