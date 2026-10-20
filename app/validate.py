# -*- coding: utf-8 -*-
"""模块：深加工线索卡 12 条校验（T5）。职责：把 deep 模型输出容错解析为 JSON，
    执行 prompts/deep.md 的 12 条反废话禁令（r1–r12，含 r8 认输、r11 能力闸、
    r12 vertical 枚举），返回 (是否通过, 原因列表)。
    规则与 tools/baseline_test.py 的 validate()（v0.5）保持一致。"""
import json
import re

# ---- 校验词表（与 tools/baseline_test.py v0.5 一致）----
OPPORTUNITY_STAGES = ["S1", "S2", "S3", "S4"]
FACT_BANNED_PREFIXES = ["据悉", "业内人士称", "有消息称", "据称"]
QUERY_ONLY_WORDS = ["查", "查阅", "下载", "调取", "阅读", "检索", "搜索", "浏览"]
TRADE_ACTION_WORDS = ["联系", "报价", "询盘", "发信", "开发信", "打电话", "注册",
                      "寄样", "下单", "签约", "拜访"]
SPECULATION_WORDS = ["可能", "或将", "预示", "预计"]
S3_AMPLITUDE_WORDS = ["涨", "跌", "升", "降", "%", "倍", "回升", "回落", "反弹",
                      "走高", "走低", "上调", "下调", "上涨", "下跌", "增长", "下降"]
S3_GENERIC_CATEGORIES = ["先进制造", "跨境电商", "电商", "消费品", "制造业"]
VERTICALS = ["宠物经济", "银发经济", "母婴", "户外", "家居", "美妆个护",
             "数码配件", "食品饮料", "汽车后市场", "其他"]
FEASIBILITY_FIELDS = ["needs_license", "needs_capital", "needs_team", "needs_identity"]

# 线索卡必填字段（r1 用）
CARD_KEYS = ("fact", "stage", "anchor", "signal", "verify_plan",
             "buyer_hypothesis", "money_hypothesis", "next_action",
             "verify_cost", "feasibility", "verifiable_data", "confidence")


def parse_deep_json(text: str):
    """从模型输出容错解析 JSON：剥 markdown 围栏，取第一对 {}。
    失败返回 None。"""
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


def validate_deep_card(res: dict) -> tuple:
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

    # r1 字段齐全
    missing = []
    for key in CARD_KEYS:
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

    # r5 money_hypothesis 必须写出钱流形态 form
    mh = res.get("money_hypothesis")
    if isinstance(mh, dict):
        form = mh.get("form")
        if not (isinstance(form, str) and form.strip()):
            reasons.append("r5_money_hypothesis.form为空（差价/佣金/服务费三选一）")
    else:
        reasons.append("r1_字段不齐:money_hypothesis 缺失")

    # r6 next_action 不得只是查资料
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

    # r9 S3 门槛
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

    # r10 反推测
    if isinstance(signal, str):
        cnt = sum(signal.count(w) for w in SPECULATION_WORDS)
        if cnt >= 2:
            reasons.append(f"r10_signal_纯推测:{cnt}次")

    # r11 能力闸
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

    # r12 vertical 枚举校验
    if isinstance(anchor, dict):
        vertical = (anchor.get("vertical") or "").strip()
        if vertical and vertical not in VERTICALS:
            reasons.append(f"r12_vertical_自创:{vertical}")

    return (len(reasons) == 0), reasons
