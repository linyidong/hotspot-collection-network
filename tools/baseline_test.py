# -*- coding: utf-8 -*-
"""
T0 加工质量基准题脚本：粗筛 -> 深加工 -> 6 条校验 -> 报告。

职责：
  1. 读取 tools/baseline/samples.jsonl（20 条真实热点样本，不改动样本文件）。
  2. 逐条调用 prompts/coarse.md 做粗筛（deepseek-flash）。
  3. 对经济相关条目调用 prompts/deep.md 做深加工（deepseek-v4-pro）。
  4. 用 validate() 严格执行架构文档 §3.3 的 6 条反废话校验。
  5. 输出 tools/baseline/report.md（含每条四件套、状态、拒绝原因、tokens、成本 + 汇总）。

关键输入输出：
  输入：samples.jsonl、prompts/coarse.md、prompts/deep.md
  输出：tools/baseline/report.md（以及每条的进度行）
关键依赖：openai（真实模式）、python-dotenv（读 .env）；--dry-run 模式不依赖网络与 key。

用法：
  python tools/baseline_test.py --dry-run            # 不调 API，内置假响应验证管道
  python tools/baseline_test.py --dry-run --limit 3  # 只跑前 3 条
  python tools/baseline_test.py --limit 5            # 真实模式，只跑前 5 条（省钱试跑）
"""

import argparse
import json
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
SAMPLES_PATH = PROJECT_ROOT / "tools" / "baseline" / "samples.jsonl"
REPORT_PATH = PROJECT_ROOT / "tools" / "baseline" / "report.md"
RAW_DIR = PROJECT_ROOT / "tools" / "baseline" / "raw"
COARSE_PROMPT_PATH = PROJECT_ROOT / "prompts" / "coarse.md"
DEEP_PROMPT_PATH = PROJECT_ROOT / "prompts" / "deep.md"

# 单价常量：元 / 百万 tokens。2026-10-19 官方价，空闲时段（来源：DeepSeek 官方定价页）。
PRICE_FLASH_INPUT = 1.0      # deepseek-flash 输入
PRICE_FLASH_OUTPUT = 4.0     # deepseek-flash 输出
PRICE_PRO_INPUT = 4.5        # deepseek-v4-pro 输入
PRICE_PRO_OUTPUT = 13.5      # deepseek-v4-pro 输出

# 6 类机会类型（任务书给定字符串，prompts 中同样使用）
OPPORTUNITY_TYPES = [
    "跨境贸易差价",
    "政策补贴红利",
    "平台规则套利",
    "新技术落地早期",
    "消费趋势",
    "供应链缺货",
]

# 校验词表（architecture.md §3.3 + playbook v0.1 的 r7/r8/r9）
OPPORTUNITY_STAGES = ["S1", "S2", "S3", "S4"]
BANNED_ACTION_WORDS = ["关注", "留意", "研究", "重视", "了解", "把握"]
EMPTY_VERIFIABLE_PHRASES = ["相关数据", "公开数据"]
FACT_BANNED_PREFIXES = ["据悉", "业内人士称", "有消息称", "据称"]
QUERY_ONLY_WORDS = ["查", "查阅", "下载", "调取", "阅读", "检索", "搜索", "浏览"]
TRADE_ACTION_WORDS = ["联系", "报价", "询盘", "发信", "开发信", "打电话", "注册", "寄样", "下单", "签约", "拜访"]


# ---------------------------------------------------------------------------
# 提示词加载
# ---------------------------------------------------------------------------
def load_prompt(path: Path) -> str:
    """加载提示词文件；缺失或为空时给出中文错误并退出。"""
    if not path.exists():
        print(f"[错误] 缺少提示词文件：{path}\n请确认项目结构完整。", file=sys.stderr)
        sys.exit(1)
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        print(f"[错误] 提示词文件为空：{path}", file=sys.stderr)
        sys.exit(1)
    return text


def load_samples(path: Path) -> list[dict]:
    """读取 samples.jsonl，每行一个 JSON 对象。样本文件只读，不改动。"""
    if not path.exists():
        print(f"[错误] 找不到样本文件：{path}", file=sys.stderr)
        sys.exit(1)
    samples = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            samples.append(json.loads(line))
        except json.JSONDecodeError as e:
            print(f"[错误] 样本第 {line_no} 行不是合法 JSON：{e}", file=sys.stderr)
            sys.exit(1)
    if not samples:
        print("[错误] 样本文件为空。", file=sys.stderr)
        sys.exit(1)
    return samples


# ---------------------------------------------------------------------------
# 校验（6 条反废话闸，对应 architecture.md §3.3）
# ---------------------------------------------------------------------------
def validate(res: dict) -> tuple[bool, list[str]]:
    """
    严格实现 9 条校验（v0.2）。返回 (是否通过, 原因列表)。
    no_playbook 按规则认输；r0 输出非 JSON；r1 字段齐全；r2 禁止自创机会类型；
    r3 动词+禁用词；r4 目标对象；r5 可验证数据非空话；r6 fact 不以传闻词开头；
    r7 playbook_stage 必填且 ∈ S1–S4；r8 money_path 三要素齐全非空话；r9 动作不得只是查资料。
    """
    reasons: list[str] = []
    if not isinstance(res, dict):
        return False, ["r0_深加工输出不是 JSON 对象"]

    # 按规则认输：opportunity_type=none（不再检查其余字段）
    if res.get("opportunity_type") == "none":
        why = str(res.get("reason", "") or "").strip() or "未给原因"
        return False, [f"no_playbook:按规则认输（{why}）"]

    # r1 字段齐全（六件套字段）
    missing = []
    for key in ("fact", "playbook_stage", "who_lacks_what", "money_path",
                "next_action", "verify_cost", "verifiable_data"):
        val = res.get(key)
        if key == "verifiable_data":
            if not isinstance(val, list) or not val:
                missing.append(key)
        elif key in ("who_lacks_what", "money_path", "next_action", "verify_cost"):
            if not isinstance(val, dict) or not val:
                missing.append(key)
        else:
            if not isinstance(val, str) or not val.strip():
                missing.append(key)
    if missing:
        reasons.append(f"r1_字段不齐:缺少 {'、'.join(missing)}")

    wlw = res.get("who_lacks_what")
    if isinstance(wlw, dict):
        for sub in ("subject", "need", "evidence"):
            v = wlw.get(sub)
            if not isinstance(v, str) or not v.strip():
                reasons.append(f"r1_字段不齐:who_lacks_what.缺少{sub}")

    # r2 禁止自创机会类型（正常输出不应含 opportunity_type 字段）
    if "opportunity_type" in res:
        reasons.append(f"r2_正常输出不应含opportunity_type:{res.get('opportunity_type')!r}")

    # r3 next_action.action 含动词且不含禁用词
    na = res.get("next_action")
    if isinstance(na, dict):
        action = na.get("action")
        if not isinstance(action, str) or not action.strip():
            reasons.append("r1_字段不齐:next_action.action 为空")
        else:
            a = action.strip()
            if len(a) < 2:
                reasons.append("r3_下一步缺少有效动词(动作过短)")
            if any(w in a for w in BANNED_ACTION_WORDS):
                reasons.append(f"r3_下一步含禁用词:{a}")
    else:
        reasons.append("r1_字段不齐:next_action 缺失")

    # r4 next_action 必须包含目标对象
    if isinstance(na, dict):
        target = na.get("target")
        if not (isinstance(target, str) and len(target.strip()) >= 2):
            reasons.append("r4_缺少目标对象(target为空或过短)")
        how = na.get("how")
        if not isinstance(how, str) or not how.strip():
            reasons.append("r1_字段不齐:next_action.how 为空")

    # r5 verifiable_data 至少 1 条且不是空话
    vd = res.get("verifiable_data")
    if isinstance(vd, list):
        valid = [
            x for x in vd
            if isinstance(x, str) and x.strip() and len(x.strip()) >= 4
            and not any(p in x for p in EMPTY_VERIFIABLE_PHRASES)
        ]
        if not valid:
            reasons.append("r5_可验证数据为空或为'相关数据'式空话")
    else:
        reasons.append("r5_verifiable_data 不是数组")

    # r6 fact 不得以传闻词开头
    fact = res.get("fact")
    if isinstance(fact, str):
        f = fact.strip().lstrip('「“"\'‘（(')
        if any(f.startswith(p) for p in FACT_BANNED_PREFIXES):
            reasons.append(f"r6_fact以'{FACT_BANNED_PREFIXES[0]}'等传闻词开头")

    # r7 playbook_stage 必填且 ∈ S1–S4
    stage = res.get("playbook_stage")
    if not (isinstance(stage, str) and stage in OPPORTUNITY_STAGES):
        reasons.append(f"r7_playbook_stage缺失或不在S1–S4:{stage!r}")

    # r8 money_path 三要素（payer/receiver/your_cut）齐全且非空话
    mp = res.get("money_path")
    if isinstance(mp, dict):
        for field in ("payer", "receiver", "your_cut"):
            v = mp.get(field)
            if not (isinstance(v, str) and v.strip()):
                reasons.append(f"r8_money_path.缺少{field}或为空话")
    else:
        reasons.append("r1_字段不齐:money_path 缺失")

    # r9 action 不得只是查资料（除非 target/how 里有交易动作词）
    if isinstance(na, dict):
        action = (na.get("action") or "").strip()
        if action:
            query_only = any(q in action for q in QUERY_ONLY_WORDS)
            trade_ctx = (na.get("target") or "") + (na.get("how") or "")
            has_trade = any(t in action or t in trade_ctx for t in TRADE_ACTION_WORDS)
            if query_only and not has_trade:
                reasons.append(f"r9_下一步只是查资料(action={action})，无交易动作")

    return (len(reasons) == 0), reasons


# ---------------------------------------------------------------------------
# JSON 解析（容忍 markdown 代码块与前后缀）
# ---------------------------------------------------------------------------
def parse_json(text: str):
    """从模型输出中解析 JSON；失败返回 None。"""
    if not text:
        return None
    t = text.strip()
    if t.startswith("```"):
        lines = t.splitlines()
        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        t = "\n".join(lines).strip()
        if t.lower().startswith("json"):
            t = t[4:].strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        start, end = t.find("{"), t.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(t[start:end + 1])
            except json.JSONDecodeError:
                return None
    return None


# ---------------------------------------------------------------------------
# 假客户端（--dry-run 用，不调 API）
# ---------------------------------------------------------------------------
class DryRunClient:
    """内置确定性假响应（v0.2）。包含 4 条故意不合格：
    s03 缺 playbook_stage（r7）；s07 money_path 缺 payer（r8）；
    s11 action 只有「查询」（r9）；s15 fact 以「据悉」开头（r6）。
    其余 16 条为通过 9 条校验的合格六件套假响应。
    """

    COARSE_TOKENS = (220, 60)    # (prompt_tokens, completion_tokens)
    DEEP_TOKENS = (420, 130)

    def __init__(self, samples: list[dict]):
        self.samples = samples

    def coarse(self, item: dict) -> tuple[dict | None, tuple[int, int]]:
        idx = self.samples.index(item)
        resp = {
            "is_economic": True,
            "playbook_stage": OPPORTUNITY_STAGES[idx % 4],
            "potential": 40 + (idx * 13) % 61,
        }
        return resp, self.COARSE_TOKENS

    def deep(self, item: dict, coarse_res: dict) -> tuple[dict | None, str, tuple[int, int]]:
        idx = self.samples.index(item)
        sample_id = item.get("id", "")
        if sample_id == "s03":  # r7：缺 playbook_stage
            resp = {
                "fact": "某国某品类进口额连续增长，平台类目爆发。",
                "who_lacks_what": {"subject": "该国采购商", "need": "差异化货源", "evidence": "进口增速快于本地供给"},
                "money_path": {"payer": "该国采购商", "receiver": "你", "your_cut": "差价"},
                "next_action": {"action": "发询盘", "target": "该国3家采购商", "how": "注册卖家账号并发开发信"},
                "verify_cost": {"money": "50元", "time": "2小时"},
                "verifiable_data": ["该品类HS编码进口额"],
                "confidence": "medium",
            }
        elif sample_id == "s07":  # r8：money_path 缺 payer
            resp = {
                "fact": "关税调整使得某品类单票成本结构变化。",
                "playbook_stage": "S3",
                "who_lacks_what": {"subject": "出口商", "need": "新的成本测算", "evidence": "关税上调"},
                "money_path": {"receiver": "你", "your_cut": "服务费"},
                "next_action": {"action": "打电话", "target": "货代公司", "how": "询最新运费并重算单票成本"},
                "verify_cost": {"money": "0元", "time": "30分钟"},
                "verifiable_data": ["最新关税税率表"],
                "confidence": "medium",
            }
        elif sample_id == "s11":  # r9：action 只有「查询」，无交易动作
            resp = {
                "fact": "某国出现抢购潮，供应链紧张。",
                "playbook_stage": "S4",
                "who_lacks_what": {"subject": "该国买家", "need": "稳定货源", "evidence": "缺货抢购"},
                "money_path": {"payer": "该国采购商", "receiver": "你", "your_cut": "差价"},
                "next_action": {"action": "查询", "target": "某行业数据库", "how": "下载年度报告"},
                "verify_cost": {"money": "20元", "time": "1小时"},
                "verifiable_data": ["该国该品类供需报告"],
                "confidence": "low",
            }
        elif sample_id == "s15":  # r6：fact 以「据悉」开头
            resp = {
                "fact": "据悉，某国发布新的进口配额政策。",
                "playbook_stage": "S3",
                "who_lacks_what": {"subject": "进口商", "need": "配额政策解读", "evidence": "新配额发布"},
                "money_path": {"payer": "进口商", "receiver": "你", "your_cut": "咨询费"},
                "next_action": {"action": "联系", "target": "该国报关行", "how": "要配额申请细则并报价"},
                "verify_cost": {"money": "0元", "time": "30分钟"},
                "verifiable_data": ["配额政策原文"],
                "confidence": "high",
            }
        else:  # 合格六件套假响应
            resp = {
                "fact": f"{sample_id} 的确定性假响应：某国某品类进口额连续增长，终端价差约3倍。",
                "playbook_stage": OPPORTUNITY_STAGES[idx % 4],
                "who_lacks_what": {
                    "subject": "示例需求方（该品类采购商）",
                    "need": "示例缺口（差异化货源）",
                    "evidence": "示例依据（供需信号）",
                },
                "money_path": {
                    "payer": "海外采购商",
                    "receiver": "你（成交后收款）",
                    "your_cut": "差价",
                },
                "next_action": {
                    "action": "发询盘",
                    "target": "示例海外采购商",
                    "how": "注册卖家账号并发送开发信报价",
                },
                "verify_cost": {"money": "50元", "time": "2小时"},
                "verifiable_data": ["示例可验证数据1", "示例可验证数据2"],
                "confidence": "low" if idx % 3 == 0 else "medium",
            }
        return resp, json.dumps(resp, ensure_ascii=False), self.DEEP_TOKENS


# ---------------------------------------------------------------------------
# 真实客户端（openai SDK 指向 DeepSeek）
# ---------------------------------------------------------------------------
def build_real_client():
    """读取 .env，构造 OpenAI 客户端；缺 key 时给出中文提示。"""
    try:
        from dotenv import load_dotenv
        load_dotenv(PROJECT_ROOT / ".env")
    except ImportError:
        pass

    import os
    from openai import OpenAI

    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not key or key.startswith("sk-xxx") or "xxxxxxxx" in key:
        print("=" * 60)
        print("[提示] 未检测到有效的 DEEPSEEK_API_KEY。")
        print("  1. 到 https://platform.deepseek.com 的 API Keys 页面申请一个 Key。")
        print("  2. 复制项目根目录的 .env.example 为 .env，把 Key 填入 DEEPSEEK_API_KEY=sk-...")
        print("  3. 不想申请可先验证管道：python tools/baseline_test.py --dry-run")
        print("=" * 60)
        sys.exit(1)

    base_url = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").strip()
    coarse_model = os.environ.get("COARSE_MODEL", "deepseek-flash").strip()
    deep_model = os.environ.get("DEEP_MODEL", "deepseek-v4-pro").strip()
    client = OpenAI(api_key=key, base_url=base_url)
    return client, coarse_model, deep_model


def call_llm(client, model: str, prompt: str, payload: dict) -> tuple[str, tuple[int, int]]:
    """调用一次模型。返回 (原始文本, (prompt_tokens, completion_tokens))。"""
    import json as _json
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": prompt},
            {"role": "user", "content": _json.dumps(payload, ensure_ascii=False)},
        ],
        response_format={"type": "json_object"},
        temperature=0.2,
    )
    usage = resp.usage
    pt = int(getattr(usage, "prompt_tokens", 0) or 0)
    ct = int(getattr(usage, "completion_tokens", 0) or 0)
    return resp.choices[0].message.content or "", (pt, ct)


class RealClient:
    """真实模式客户端：coarse 用 flash，deep 用 pro，统一返回与 DryRunClient 相同的接口。"""

    def __init__(self, raw_client, coarse_model: str, deep_model: str,
                 coarse_prompt: str, deep_prompt: str):
        self.raw = raw_client
        self.coarse_model = coarse_model
        self.deep_model = deep_model
        self.coarse_prompt = coarse_prompt
        self.deep_prompt = deep_prompt

    def coarse(self, item: dict) -> tuple[dict | None, tuple[int, int]]:
        payload = {k: item.get(k) for k in ("id", "title", "summary", "published_at", "source", "url")}
        text, tokens = call_llm(self.raw, self.coarse_model, self.coarse_prompt, payload)
        return parse_json(text), tokens

    def deep(self, item: dict, coarse_res: dict) -> tuple[dict | None, str, tuple[int, int]]:
        payload = {k: item.get(k) for k in ("id", "title", "summary", "published_at", "source", "url")}
        payload["coarse"] = coarse_res
        text, tokens = call_llm(self.raw, self.deep_model, self.deep_prompt, payload)
        return parse_json(text), text, tokens


# ---------------------------------------------------------------------------
# 成本计算
# ---------------------------------------------------------------------------
def cost_of(stage: str, tokens: tuple[int, int]) -> float:
    """按常量单价计算一次调用成本（元）。stage: 'coarse' 用 flash，'deep' 用 pro。"""
    pt, ct = tokens
    if stage == "coarse":
        return pt / 1_000_000 * PRICE_FLASH_INPUT + ct / 1_000_000 * PRICE_FLASH_OUTPUT
    return pt / 1_000_000 * PRICE_PRO_INPUT + ct / 1_000_000 * PRICE_PRO_OUTPUT


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------
def short_four(res: dict, limit: int = 80) -> str:
    """把六件套压缩成一行，超长截断到 80 字。"""
    if not isinstance(res, dict):
        return str(res)
    fact = (res.get("fact") or "")[:30]
    stage = res.get("playbook_stage") or ""
    wlw = res.get("who_lacks_what") or {}
    mp = res.get("money_path") or {}
    na = res.get("next_action") or {}
    vd = res.get("verifiable_data") or []
    subject = (wlw.get("subject") or "")[:15]
    payer = (mp.get("payer") or "")[:10]
    action = (na.get("action") or "")[:12]
    target = (na.get("target") or "")[:15]
    vd_str = "、".join(str(x)[:12] for x in vd[:2])
    s = f"事实:{fact}｜阶段:{stage}｜缺:{subject}｜付:{payer}｜动作:{action}->{target}｜验证:{vd_str}"
    return s if len(s) <= limit else s[: limit - 3] + "…"


def write_report(rows: list[dict], summary: dict) -> None:
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# T0 加工质量基准题报告",
        "",
        "- 生成时间：由脚本自动生成",
        "- 版本：v0.2（提示词 v0.2 + 9 条校验）",
        "- 样本数：{total}；ok：{ok}；rejected：{rejected}；skipped：{skipped}；failed：{failed}".format(**summary),
        "- 总花费：{cost_total:.4f} 元；平均每条：{cost_avg:.4f} 元".format(**summary),
        "",
        "## 逐条结果",
        "",
        "| 样本 | 标题 | 六件套摘要 | 阶段 | 状态 | 拒绝原因 | prompt | completion | 成本(元) |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        title = (r["title"] or "").replace("|", "\\|")[:20]
        four = (r["four"] or "").replace("|", "\\|")
        reason = (r["reason"] or "").replace("|", "\\|")
        lines.append(
            f"| {r['id']} | {title} | {four} | {r['stage']} | {r['status']} | {reason or '-'} "
            f"| {r['pt']} | {r['ct']} | {r['cost']:.4f} |"
        )
    lines.append("")
    lines.append("## 汇总")
    lines.append("")
    lines.append(f"- 总数：{summary['total']}")
    lines.append(f"- ok（通过 9 条校验）：{summary['ok']}")
    lines.append(f"- rejected（被校验拦下）：{summary['rejected']}")
    lines.append("- rejected 按原因分类计数：")
    if summary["reject_reasons"]:
        for label, cnt in summary["reject_reasons"].items():
            lines.append(f"  - {label}：{cnt}")
    else:
        lines.append("  - （无）")
    lines.append(f"- skipped（粗筛判定非经济，未深加工）：{summary['skipped']}")
    lines.append(f"- failed（输出无法解析/调用失败）：{summary['failed']}")
    lines.append(f"- 总花费：{summary['cost_total']:.4f} 元")
    lines.append(f"- 平均每条花费：{summary['cost_avg']:.4f} 元")
    lines.append("")
    lines.append("## 人工盲评")
    lines.append("")
    lines.append("人工盲评合格率：__ / 20（由人来填，脚本不猜）")
    lines.append("")
    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")
    print(f"[完成] 报告已写入：{REPORT_PATH}")


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="T0 加工质量基准题：粗筛->深加工->6条校验->报告")
    parser.add_argument("--dry-run", action="store_true", help="不调用 API，用内置假响应跑通全流程")
    parser.add_argument("--limit", type=int, default=None, help="只跑前 N 条（便于省钱试跑）")
    args = parser.parse_args()

    coarse_prompt = load_prompt(COARSE_PROMPT_PATH)
    deep_prompt = load_prompt(DEEP_PROMPT_PATH)
    samples = load_samples(SAMPLES_PATH)
    if args.limit is not None:
        samples = samples[: args.limit]
    total = len(samples)

    if args.dry_run:
        client = DryRunClient(samples)
    else:
        raw_client, coarse_model, deep_model = build_real_client()
        client = RealClient(raw_client, coarse_model, deep_model, coarse_prompt, deep_prompt)

    rows = []
    reject_reason_counter: dict[str, int] = {}
    total_cost = 0.0
    n_ok = n_rejected = n_skipped = n_failed = 0
    RAW_DIR.mkdir(parents=True, exist_ok=True)

    for i, item in enumerate(samples, 1):
        sample_id = item.get("id", f"line{i}")
        title = item.get("title", "")
        coarse_res, coarse_tokens = client.coarse(item)
        c_cost = cost_of("coarse", coarse_tokens)

        status, reason, four, stage, pt, ct, cost = "failed", "", "", "-", 0, 0, 0.0
        deep_res, deep_raw_text, deep_reasons = None, "", None
        if coarse_res is None or not isinstance(coarse_res, dict):
            status, reason, cost = "failed", "粗筛输出无法解析", c_cost
        elif coarse_res.get("is_economic") is not True:
            status, reason, cost = "skipped", "粗筛判定非经济，未深加工", c_cost
        else:
            deep_res, deep_raw_text, deep_tokens = client.deep(item, coarse_res)
            d_cost = cost_of("deep", deep_tokens)
            pt, ct = deep_tokens
            cost = c_cost + d_cost
            if deep_res is None:
                status, reason = "failed", "深加工输出无法解析为 JSON"
            else:
                ok, reasons = validate(deep_res)
                deep_reasons = reasons
                four = short_four(deep_res)
                if isinstance(deep_res, dict):
                    stage = deep_res.get("playbook_stage") or "-"
                if ok:
                    status = "ok"
                    n_ok += 1
                else:
                    status = "rejected"
                    n_rejected += 1
                    reason = "；".join(reasons)
                    for r in reasons:
                        label = r.split(":")[0] if ":" in r else r
                        reject_reason_counter[label] = reject_reason_counter.get(label, 0) + 1

        # 每条结果的完整 JSON 落盘（含粗筛/深加工原文/校验原因），便于复盘与 prompt-log
        record = {
            "id": sample_id,
            "status": status,
            "stage": stage,
            "coarse": coarse_res,
            "deep": deep_res,
            "deep_raw_text": deep_raw_text if deep_res is None else None,
            "reasons": deep_reasons,
        }
        (RAW_DIR / f"{sample_id}.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")

        if status == "failed":
            n_failed += 1
        elif status == "skipped":
            n_skipped += 1
        total_cost += cost

        rows.append({
            "id": sample_id, "title": title, "four": four, "stage": stage,
            "status": status, "reason": reason, "pt": pt, "ct": ct, "cost": cost,
        })
        print(f"[{i}/{total}] {sample_id} {status}  cost={cost:.4f}")

    summary = {
        "total": total,
        "ok": n_ok,
        "rejected": n_rejected,
        "skipped": n_skipped,
        "failed": n_failed,
        "reject_reasons": dict(sorted(reject_reason_counter.items())),
        "cost_total": total_cost,
        "cost_avg": total_cost / total if total else 0.0,
    }
    write_report(rows, summary)

    print(f"\n汇总：总数={total} ok={n_ok} rejected={n_rejected} skipped={n_skipped} failed={n_failed}")
    print(f"总花费={total_cost:.4f} 元，平均每条={summary['cost_avg']:.4f} 元")


if __name__ == "__main__":
    main()
