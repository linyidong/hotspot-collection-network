# -*- coding: utf-8 -*-
"""
T0 加工质量基准题脚本（v0.4）：硬过滤 -> 粗筛 -> 深加工 -> 10 条校验 -> 双集报告。

职责：
  1. 读取测试集样本（gold=含机会新闻 20 条 / live=华尔街见闻真实快讯 20 条）。
  2. 硬过滤（纯字符串规则，模型调用前）：命中公告/早报类词 → skipped，不计费。
  3. 逐条调用 prompts/coarse.md 做粗筛（deepseek-flash，只判经济性 + 抽国家/品类）。
  4. 对经济相关条目调用 prompts/deep.md 做深加工（deepseek-v4-pro，输出线索卡）。
  5. 用 validate() 执行 PRD F-006 的 8 条 + r9（S3 门槛）/ r10（反推测）。
  6. 输出 report-gold.md / report-live.md（含每条线索卡摘要、阶段、A/B/C 分级位、
     状态、拒绝原因、tokens、成本 + 汇总 + 硬过滤计数）。

用法：
  python tools/baseline_test.py --dry-run                 # 默认 gold 集，不调 API
  python tools/baseline_test.py --set live --dry-run      # live 集 dry-run
  python tools/baseline_test.py --set gold --limit 3      # gold 集试跑前 3 条
  python tools/baseline_test.py --set live                # live 集真实模式
"""

import argparse
import json
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
SAMPLES_GOLD_PATH = PROJECT_ROOT / "tools" / "baseline" / "samples-gold.jsonl"
SAMPLES_LIVE_PATH = PROJECT_ROOT / "tools" / "baseline" / "samples-live.jsonl"
REPORT_GOLD_PATH = PROJECT_ROOT / "tools" / "baseline" / "report-gold.md"
REPORT_LIVE_PATH = PROJECT_ROOT / "tools" / "baseline" / "report-live.md"
RAW_DIR = PROJECT_ROOT / "tools" / "baseline" / "raw"
COARSE_PROMPT_PATH = PROJECT_ROOT / "prompts" / "coarse.md"
DEEP_PROMPT_PATH = PROJECT_ROOT / "prompts" / "deep.md"

# 单价常量：元 / 百万 tokens。2026-10-19 官方价，空闲时段（来源：DeepSeek 官方定价页）。
PRICE_FLASH_INPUT = 1.0      # deepseek-flash 输入
PRICE_FLASH_OUTPUT = 4.0     # deepseek-flash 输出
PRICE_PRO_INPUT = 4.5        # deepseek-v4-pro 输入
PRICE_PRO_OUTPUT = 13.5      # deepseek-v4-pro 输出

# 校验词表（PRD F-006 8 条 + v0.4 的 r9/r10）
OPPORTUNITY_STAGES = ["S1", "S2", "S3", "S4"]
FACT_BANNED_PREFIXES = ["据悉", "业内人士称", "有消息称", "据称"]
QUERY_ONLY_WORDS = ["查", "查阅", "下载", "调取", "阅读", "检索", "搜索", "浏览"]
TRADE_ACTION_WORDS = ["联系", "报价", "询盘", "发信", "开发信", "打电话", "注册", "寄样", "下单", "签约", "拜访"]

# 硬过滤词（v0.4）：标题/正文命中任一 → 不进模型，直接 skipped，不计费
HARD_FILTER_WORDS = ["早报", "日报", "综述", "快讯汇总", "投资者关系", "答记者问", "批复"]

# r10 反推测：signal 里这些词出现 ≥2 次 → 纯推测
SPECULATION_WORDS = ["可能", "或将", "预示", "预计"]

# r9 S3 门槛：signal 必须含数值或幅度；anchor.category 不得是泛词
S3_AMPLITUDE_WORDS = ["涨", "跌", "升", "降", "%", "倍", "回升", "回落", "反弹",
                      "走高", "走低", "上调", "下调", "上涨", "下跌", "增长", "下降"]
S3_GENERIC_CATEGORIES = ["先进制造", "跨境电商", "电商", "消费品", "制造业"]

# r12 vertical 枚举（playbook §3）：行业标签，与 6 类机制是两个维度
VERTICALS = ["宠物经济", "银发经济", "母婴", "户外", "家居", "美妆个护",
             "数码配件", "食品饮料", "汽车后市场", "其他"]

# r11 能力闸（docs/capability-profile.md）：四布尔任一为 true → 超出个人能力
FEASIBILITY_FIELDS = ["needs_license", "needs_capital", "needs_team", "needs_identity"]


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
    """读取 samples-*.jsonl，每行一个 JSON 对象。样本文件只读，不改动。"""
    if not path.exists():
        print(f"[错误] 找不到样本文件：{path}", file=sys.stderr)
        sys.exit(1)
    samples = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip().lstrip("\ufeff")
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
# 硬过滤（v0.4：模型调用之前，纯字符串规则）
# ---------------------------------------------------------------------------
def hard_filter(item: dict) -> str | None:
    """标题/正文命中硬过滤词 → 返回命中词；否则 None。命中条目不进模型，不计费。"""
    text = f"{item.get('title') or ''} {item.get('content') or ''} {item.get('summary') or ''}"
    for w in HARD_FILTER_WORDS:
        if w in text:
            return w
    return None


# ---------------------------------------------------------------------------
# 校验（PRD F-006 的 8 条 + v0.4 的 r9/r10）
# ---------------------------------------------------------------------------
def validate(res: dict) -> tuple[bool, list[str]]:
    """
    严格实现 12 条校验（v0.5）。
    ①字段齐全(r1)；②stage∈S1–S4(r2)；③anchor 国家或品类至少一非空(r3)；
    ④verify_plan 恰好 3 条且每条含具体查询对象(r4)；⑤money_hypothesis.form 非空(r5)；
    ⑥next_action 不得只是查资料(r6)；⑦fact 不得以传闻词开头(r7)；
    ⑧opportunity_type=none 判 no_playbook(r8，认输不算失败)；
    ⑨stage=S3 时 signal 必须含数值或幅度且 anchor.category 非泛词(r9)；
    ⑩signal 中"可能/或将/预示/预计"≥2 次 → 纯推测(r10)；
    ⑪feasibility 四项齐全且全为 false，任一 true → r11_超出个人能力(r11)；
    ⑫anchor.vertical 在 10 枚举内或留空，自创 → r12_vertical_自创(r12)。
    返回 (是否通过, 原因列表)。任一不满足 → 判 rejected。
    """
    reasons: list[str] = []
    if not isinstance(res, dict):
        return False, ["r0_深加工输出不是 JSON 对象"]

    # r8 按规则认输：opportunity_type=none（不再检查其余字段）
    if res.get("opportunity_type") == "none":
        why = str(res.get("reason", "") or "").strip() or "未给原因"
        return False, [f"no_playbook:按规则认输（{why}）"]

    # r1 字段齐全（线索卡字段）
    missing = []
    for key in ("fact", "stage", "anchor", "signal", "verify_plan",
                "buyer_hypothesis", "money_hypothesis", "next_action",
                "verify_cost", "feasibility", "verifiable_data", "confidence"):
        val = res.get(key)
        if key in ("verify_plan", "verifiable_data"):
            if not isinstance(val, list) or not val:
                missing.append(key)
        elif key == "anchor":
            if not isinstance(val, dict) or not val:
                missing.append(key)
        elif key in ("money_hypothesis", "next_action", "verify_cost", "feasibility"):
            if not isinstance(val, dict) or not val:
                missing.append(key)
        else:
            if not isinstance(val, str) or not val.strip():
                missing.append(key)
    if missing:
        reasons.append(f"r1_字段不齐:缺少{'、'.join(missing)}")

    # r2 stage 必填且 ∈ S1–S4
    stage = res.get("stage")
    if not (isinstance(stage, str) and stage in OPPORTUNITY_STAGES):
        reasons.append(f"r2_stage缺失或不在S1–S4:{stage!r}")

    # r3 anchor 至少给出国家或品类之一
    anchor = res.get("anchor")
    if isinstance(anchor, dict):
        country = (anchor.get("country") or "").strip()
        category = (anchor.get("category") or "").strip()
        if not country and not category:
            reasons.append("r3_anchor国家与品类均为空")
    else:
        reasons.append("r1_字段不齐:anchor 缺失")

    # r4 verify_plan 恰好 3 条且每条含具体查询对象
    vp = res.get("verify_plan")
    if isinstance(vp, list):
        if len(vp) != 3:
            reasons.append(f"r4_verify_plan条数应为3:{len(vp)}")
        else:
            for x in vp:
                if not (isinstance(x, str) and x.strip() and len(x.strip()) >= 4):
                    reasons.append("r4_verify_plan每条需写清查什么+在哪查")
                    break
    else:
        reasons.append("r1_字段不齐:verify_plan 缺失")

    # r5 money_hypothesis 必须写出钱流形态 form（差价/佣金/服务费），status 可待验证
    mh = res.get("money_hypothesis")
    if isinstance(mh, dict):
        form = mh.get("form")
        if not (isinstance(form, str) and form.strip()):
            reasons.append("r5_money_hypothesis.form为空（差价/佣金/服务费三选一）")
    else:
        reasons.append("r1_字段不齐:money_hypothesis 缺失")

    # r6 next_action 不得只是查资料（除非 target/how 里有交易动作词）
    na = res.get("next_action")
    if isinstance(na, dict):
        action = (na.get("action") or "").strip()
        if not action:
            reasons.append("r1_字段不齐:next_action.action 为空")
        else:
            query_only = any(q in action for q in QUERY_ONLY_WORDS)
            trade_ctx = (na.get("target") or "") + (na.get("how") or "")
            has_trade = any(t in action or t in trade_ctx for t in TRADE_ACTION_WORDS)
            if query_only and not has_trade:
                reasons.append(f"r6_下一步只是查资料(action={action})，无交易动作")
    else:
        reasons.append("r1_字段不齐:next_action 缺失")

    # r7 fact 不得以传闻词开头
    fact = res.get("fact")
    if isinstance(fact, str):
        f = fact.strip().lstrip('「“"\'‘（(')
        if any(f.startswith(p) for p in FACT_BANNED_PREFIXES):
            reasons.append(f"r7_fact以'{FACT_BANNED_PREFIXES[0]}'等传闻词开头")

    # r9 S3 门槛：stage=S3 时 signal 必须含数值或幅度，且 anchor.category 不得是泛词
    signal = res.get("signal")
    if stage == "S3":
        if isinstance(signal, str) and signal.strip():
            has_num = any(c.isdigit() for c in signal)
            has_amp = any(w in signal for w in S3_AMPLITUDE_WORDS)
            if not (has_num or has_amp):
                reasons.append("r9_S3signal无数值或幅度")
        else:
            reasons.append("r1_字段不齐:signal 缺失")
        if isinstance(anchor, dict):
            category = (anchor.get("category") or "").strip()
            if not category or any(g in category for g in S3_GENERIC_CATEGORIES):
                reasons.append("r9_S3anchor.category为泛品类或为空")

    # r10 反推测：signal 中推测词出现 ≥2 次 → 纯推测
    if isinstance(signal, str):
        cnt = sum(signal.count(w) for w in SPECULATION_WORDS)
        if cnt >= 2:
            reasons.append(f"r10_signal_纯推测:{cnt}次")

    # r11 能力闸：feasibility 四项齐全且全为 false（docs/capability-profile.md）
    feas = res.get("feasibility")
    if isinstance(feas, dict):
        for field in FEASIBILITY_FIELDS:
            v = feas.get(field)
            if not isinstance(v, bool):
                reasons.append(f"r11_feasibility.缺少{field}或非布尔")
            elif v is True:
                reasons.append(f"r11_超出个人能力:{field}")
    else:
        reasons.append("r1_字段不齐:feasibility 缺失")

    # r12 vertical 枚举校验：在 10 枚举内或留空，自创 → 不合格
    if isinstance(anchor, dict):
        vertical = (anchor.get("vertical") or "").strip()
        if vertical and vertical not in VERTICALS:
            reasons.append(f"r12_vertical_自创:{vertical}")

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
    """内置确定性假响应（v0.5 线索卡）。包含 9 条故意不合格：
    G01 缺 stage（r2）；G02 anchor 国家与品类都空（r3）；
    G03 verify_plan 只有 2 条（r4）；G04 money_hypothesis 缺 form（r5）；
    G05 fact 以「据悉」开头（r7）；G06 stage=S3 但 signal 无幅度且品类泛（r9）；
    G07 signal 推测词 ≥2（r10）；G08 feasibility 含 true（r11）；
    G09 anchor.vertical 自创（r12）。其余为通过 12 条校验的合格线索卡假响应。
    """

    COARSE_TOKENS = (220, 60)    # (prompt_tokens, completion_tokens)
    DEEP_TOKENS = (420, 130)

    def __init__(self, samples: list[dict]):
        self.samples = samples

    def coarse(self, item: dict) -> tuple[dict | None, tuple[int, int]]:
        idx = self.samples.index(item)
        resp = {
            "is_economic": True,
            "anchors": {"country": "越南", "category": "手机配件"},
            "potential": 40 + (idx * 13) % 61,
        }
        return resp, self.COARSE_TOKENS

    def _valid_card(self, sample_id: str, idx: int) -> dict:
        """一条通过校验的合格线索卡假响应（signal 含数字，避免 S3 触发 r9）。"""
        return {
            "fact": f"{sample_id} 的确定性假响应：某国某品类进口额连续增长，终端价差约3倍。",
            "stage": OPPORTUNITY_STAGES[idx % 4],
            "anchor": {
                "country": "越南", "category": "手机配件", "hs_hint": "", "vertical": "数码配件",
            },
            "signal": "某国该品类进口额连续18个月增长，平台类目爆发",
            "verify_plan": [
                "查该品类HS编码近12个月进口额，用UN Comtrade",
                "查该品类电商平台类目销量榜单",
                "查1688出厂价并对比当地零售价",
            ],
            "buyer_hypothesis": "某国该品类中小采购商",
            "money_hypothesis": {
                "form": "差价", "payer": "某国采购商", "your_cut": "出厂价与零售价差价", "status": "待验证",
            },
            "next_action": {
                "action": "发询盘", "target": "某国3家采购商", "how": "注册卖家账号并发开发信报价",
            },
            "verify_cost": {"money": "50元", "time": "2小时"},
            "feasibility": {
                "needs_license": False, "needs_capital": False, "needs_team": False,
                "needs_identity": False, "reason": "注册平台账号、发开发信、查公开数据均在个人能力内",
            },
            "verifiable_data": ["该品类HS编码进口额", "电商平台类目销量榜"],
            "confidence": "low" if idx % 3 == 0 else "medium",
        }

    def deep(self, item: dict, coarse_res: dict) -> tuple[dict | None, str, tuple[int, int]]:
        idx = self.samples.index(item)
        sample_id = item.get("id", "")
        if sample_id == "G01":  # r2：缺 stage（也会连带 r1）
            resp = self._valid_card(sample_id, idx)
            resp.pop("stage")
        elif sample_id == "G02":  # r3：anchor 国家与品类都空
            resp = self._valid_card(sample_id, idx)
            resp["anchor"] = {"country": "", "category": "", "hs_hint": "", "vertical": "数码配件"}
        elif sample_id == "G03":  # r4：verify_plan 只有 2 条
            resp = self._valid_card(sample_id, idx)
            resp["verify_plan"] = resp["verify_plan"][:2]
        elif sample_id == "G04":  # r5：money_hypothesis 缺 form
            resp = self._valid_card(sample_id, idx)
            resp["money_hypothesis"] = {"payer": "某国采购商", "your_cut": "差价", "status": "待验证"}
        elif sample_id == "G05":  # r7：fact 以「据悉」开头
            resp = self._valid_card(sample_id, idx)
            resp["fact"] = "据悉，某国将放宽该品类进口限制，市场供给格局将变。"
        elif sample_id == "G06":  # r9：S3 但 signal 无幅度 + category 泛词
            resp = self._valid_card(sample_id, idx)
            resp["stage"] = "S3"
            resp["signal"] = "关税政策可能影响该品类出口"
            resp["anchor"] = {"country": "某国", "category": "先进制造", "hs_hint": "", "vertical": "其他"}
        elif sample_id == "G07":  # r10：signal 推测词 ≥2
            resp = self._valid_card(sample_id, idx)
            resp["signal"] = "该品类需求可能回暖，或将吸引更多买家，预计价格走高"
        elif sample_id == "G08":  # r11：feasibility 含 true
            resp = self._valid_card(sample_id, idx)
            resp["feasibility"] = {
                "needs_license": True, "needs_capital": False, "needs_team": False,
                "needs_identity": False, "reason": "联系基金公司谈代销",
            }
        elif sample_id == "G09":  # r12：vertical 自创
            resp = self._valid_card(sample_id, idx)
            resp["anchor"]["vertical"] = "web3.0"
        else:  # 合格线索卡假响应
            resp = self._valid_card(sample_id, idx)
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

    PAYLOAD_KEYS = ("id", "title", "summary", "content", "published_at", "source", "url")

    def __init__(self, raw_client, coarse_model: str, deep_model: str,
                 coarse_prompt: str, deep_prompt: str):
        self.raw = raw_client
        self.coarse_model = coarse_model
        self.deep_model = deep_model
        self.coarse_prompt = coarse_prompt
        self.deep_prompt = deep_prompt

    def coarse(self, item: dict) -> tuple[dict | None, tuple[int, int]]:
        payload = {k: item.get(k) for k in self.PAYLOAD_KEYS}
        text, tokens = call_llm(self.raw, self.coarse_model, self.coarse_prompt, payload)
        return parse_json(text), tokens

    def deep(self, item: dict, coarse_res: dict) -> tuple[dict | None, str, tuple[int, int]]:
        payload = {k: item.get(k) for k in self.PAYLOAD_KEYS}
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
    """把线索卡压缩成一行，超长截断到 80 字。"""
    if not isinstance(res, dict):
        return str(res)
    fact = (res.get("fact") or "")[:28]
    stage = res.get("stage") or ""
    anchor = res.get("anchor") or {}
    country = (anchor.get("country") or "")[:8]
    category = (anchor.get("category") or "")[:12]
    buyer = (res.get("buyer_hypothesis") or "")[:12]
    mh = res.get("money_hypothesis") or {}
    form = (mh.get("form") or "")[:6]
    na = res.get("next_action") or {}
    action = (na.get("action") or "")[:10]
    target = (na.get("target") or "")[:12]
    s = f"事实:{fact}｜阶段:{stage}｜锚:{country}/{category}｜买家:{buyer}｜钱流:{form}｜动作:{action}->{target}"
    return s if len(s) <= limit else s[: limit - 3] + "…"


def write_report(rows: list[dict], summary: dict, set_name: str, report_path: Path) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    set_label = "gold（含机会新闻）" if set_name == "gold" else "live（华尔街见闻真实快讯）"
    lines = [
        "# T0 加工质量基准题报告",
        "",
        "- 生成时间：由脚本自动生成",
        "- 测试集：{set_label}".format(set_label=set_label),
        "- 版本：v0.5（提示词 v0.5 + 12 条校验，输出线索卡 + 能力闸 + 行业标签）",
        "- 硬过滤 {hard} 条 / 进入模型 {model} 条 / ok {ok} / rejected {rejected}".format(**summary),
        "- 样本数：{total}；ok：{ok}；rejected：{rejected}；skipped：{skipped}；failed：{failed}".format(**summary),
        "- 总花费：{cost_total:.4f} 元；平均每条：{cost_avg:.4f} 元".format(**summary),
        "",
        "## 逐条结果",
        "",
        "| 样本 | 标题 | 线索卡摘要 | 阶段 | 行业 | A/B/C | 状态 | 拒绝原因 | prompt | completion | 成本(元) |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        title = (r["title"] or "").replace("|", "\\|")[:20]
        four = (r["four"] or "").replace("|", "\\|")
        reason = (r["reason"] or "").replace("|", "\\|")
        lines.append(
            f"| {r['id']} | {title} | {four} | {r['stage']} | {r['vertical']} | {r['grade']} | {r['status']} | {reason or '-'} "
            f"| {r['pt']} | {r['ct']} | {r['cost']:.4f} |"
        )
    lines.append("")
    lines.append("## 汇总")
    lines.append("")
    lines.append(f"- 总数：{summary['total']}")
    lines.append(f"- 硬过滤命中：{summary['hard']}（标题/正文命中公告类词，不进模型，不计费）")
    lines.append(f"- 进入模型：{summary['model']}")
    lines.append(f"- ok（通过 12 条校验）：{summary['ok']}")
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
    lines.append("A/B/C 分级口径：A=今天就能联系/报价、钱流清楚；B=查 1 次数据即可判断；C=只是信号，暂时无动作。")
    lines.append("合格率只统计 A + B。")
    lines.append("人工盲评合格率：__ / 20（由人来填，脚本不猜）")
    lines.append("")
    report_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"[完成] 报告已写入：{report_path}")


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="T0 加工质量基准题：硬过滤->粗筛->深加工->10条校验->报告")
    parser.add_argument("--dry-run", action="store_true", help="不调用 API，用内置假响应跑通全流程")
    parser.add_argument("--limit", type=int, default=None, help="只跑前 N 条（便于省钱试跑）")
    parser.add_argument("--set", choices=["gold", "live"], default="gold",
                        help="测试集：gold=含机会新闻 / live=华尔街见闻真实快讯（默认 gold）")
    args = parser.parse_args()

    samples_path = SAMPLES_GOLD_PATH if args.set == "gold" else SAMPLES_LIVE_PATH
    report_path = REPORT_GOLD_PATH if args.set == "gold" else REPORT_LIVE_PATH

    coarse_prompt = load_prompt(COARSE_PROMPT_PATH)
    deep_prompt = load_prompt(DEEP_PROMPT_PATH)
    samples = load_samples(samples_path)
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
    n_ok = n_rejected = n_skipped = n_failed = n_hard = 0
    RAW_DIR.mkdir(parents=True, exist_ok=True)

    for i, item in enumerate(samples, 1):
        sample_id = item.get("id", f"line{i}")
        title = item.get("title", "")

        # 硬过滤：模型调用之前执行，命中不计费
        hf_word = hard_filter(item)
        if hf_word:
            n_hard += 1
            status, reason, four, stage = "skipped", f"hard_filter:{hf_word}", "", "-"
            vertical = ""
            pt, ct, cost = 0, 0, 0.0
            coarse_res, deep_res, deep_raw_text, deep_reasons = None, None, "", None
        else:
            coarse_res, coarse_tokens = client.coarse(item)
            c_cost = cost_of("coarse", coarse_tokens)

            status, reason, four, stage, vertical = "failed", "", "", "-", ""
            pt, ct, cost = 0, 0, 0.0
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
                        stage = deep_res.get("stage") or "-"
                        _anchor = deep_res.get("anchor")
                        vertical = (_anchor or {}).get("vertical") or "" if isinstance(_anchor, dict) else ""
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
            "vertical": vertical, "grade": "", "status": status, "reason": reason,
            "pt": pt, "ct": ct, "cost": cost,
        })
        print(f"[{i}/{total}] {sample_id} {status}  cost={cost:.4f}")

    n_model = total - n_hard
    summary = {
        "total": total,
        "hard": n_hard,
        "model": n_model,
        "ok": n_ok,
        "rejected": n_rejected,
        "skipped": n_skipped,
        "failed": n_failed,
        "reject_reasons": dict(sorted(reject_reason_counter.items())),
        "cost_total": total_cost,
        "cost_avg": total_cost / total if total else 0.0,
    }
    write_report(rows, summary, args.set, report_path)

    print(f"\n汇总[{args.set}]：硬过滤={n_hard} 进模型={n_model} ok={n_ok} rejected={n_rejected} "
          f"skipped={n_skipped} failed={n_failed}")
    print(f"总花费={total_cost:.4f} 元，平均每条={summary['cost_avg']:.4f} 元")


if __name__ == "__main__":
    main()
