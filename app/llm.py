# -*- coding: utf-8 -*-
"""模块：LLM 调用封装 + 调用即记账。职责：封装 DeepSeek Chat 调用（openai SDK），
    把 单价 → 成本 的计算固化；每次调用（含失败重试）立刻写一条 llm_calls。
    关键依赖：openai、app.config、app.db。"""
from datetime import datetime

from .config import settings

# 单价常量（元 / 百万 tokens）——2026-10-20 官网空闲档
# deepseek-flash：输入 1 元/M、输出 4 元/M；deepseek-v4-pro：输入 4.5 元/M、输出 13.5 元/M
UNIT_PRICE_PER_M = {
    "deepseek-flash": {"in": 1.0, "out": 4.0},
    "deepseek-v4-pro": {"in": 4.5, "out": 13.5},
}

DEFAULT_TIMEOUT = 40.0


def _unit_price(model: str) -> tuple:
    """(输入单价, 输出单价) 元/token；未知模型报错。"""
    p = UNIT_PRICE_PER_M.get(model)
    if not p:
        raise ValueError(f"未知模型单价：{model}")
    return p["in"] / 1_000_000, p["out"] / 1_000_000


def unit_price(model: str) -> tuple:
    """公开单价（元/token），供调用方自行记账。"""
    return _unit_price(model)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _get_client(timeout: float = DEFAULT_TIMEOUT):
    """懒加载 OpenAI 客户端（线程安全，可并发复用）。"""
    from openai import OpenAI
    if not settings.key_configured:
        raise RuntimeError("DEEPSEEK_API_KEY 未配置，无法调用模型")
    return OpenAI(api_key=settings.DEEPSEEK_API_KEY,
                  base_url=settings.DEEPSEEK_BASE_URL,
                  timeout=timeout)


def chat_raw(model: str, user_prompt: str, system_prompt: str = "",
             max_tokens: int = 600, timeout: float = DEFAULT_TIMEOUT,
             thinking: bool = False, response_format=None, temperature=None) -> tuple:
    """纯模型调用（不写库），供并发场景在 worker 线程使用。
    返回 (content, prompt_tokens, completion_tokens)。失败抛异常。
    thinking：与 chat() 保持一致，默认关闭思考模式——否则思考内容会占用
    max_tokens 导致正文为空（实测粗筛 70% 返回空，深加工 thinking 也实测截断正文）。
    response_format：如 {"type": "json_object"} 强制结构化输出（T0 基准题同款）。
    temperature：默认 None 不传（用服务端默认）；需要可显式给 0.2 之类。"""
    client = _get_client(timeout)
    messages = [{"role": "user", "content": user_prompt}]
    if system_prompt:
        messages.insert(0, {"role": "system", "content": system_prompt})
    kwargs = {
        "model": model, "messages": messages, "max_tokens": max_tokens,
        "extra_body": {"thinking": {"type": "enabled" if thinking else "disabled"}},
    }
    if response_format is not None:
        kwargs["response_format"] = response_format
    if temperature is not None:
        kwargs["temperature"] = temperature
    resp = client.chat.completions.create(**kwargs)
    usage = resp.usage
    return (resp.choices[0].message.content or "",
            usage.prompt_tokens or 0, usage.completion_tokens or 0)


def _human_openai_error(exc: Exception) -> str:
    """openai 异常 → 一行中文原因。"""
    try:
        from openai import APIStatusError, RateLimitError, APIConnectionError
    except ImportError:
        return str(exc) or exc.__class__.__name__
    if isinstance(exc, RateLimitError):
        return "限流：请求过频或额度不足（429）"
    if isinstance(exc, APIConnectionError):
        return "网络错误：无法连接 DeepSeek API"
    if isinstance(exc, APIStatusError):
        return f"API 错误：HTTP {exc.status_code}"
    return str(exc) or exc.__class__.__name__


def chat(model: str, user_prompt: str, batch_id: int,
         stage: str = "coarse", system_prompt: str = "",
         max_tokens: int = 600, timeout: float = DEFAULT_TIMEOUT,
         conn=None, thinking: bool = False, response_format=None,
         temperature=None) -> str:
    """调用一次 DeepSeek Chat 并立刻记账（llm_calls）。
    返回模型输出文本；失败抛异常（调用记录仍在，供成本审计）。
    conn 复用连接时由调用方负责 commit。

    thinking：DeepSeek 默认开启思考模式（effort=high），粗筛这类简单分类任务
    不需要思考，且思考内容会占用 max_tokens 导致正文为空（实测 70% 返回空）。
    默认关闭；深加工（T5）经实测同样会被思考截断正文，改用 json_object +
    关闭思考（与 T0 基准题一致）以稳定产出 JSON。
    response_format：如 {"type": "json_object"} 强制结构化输出。
    temperature：默认 None 不传（用服务端默认）。"""
    from . import db

    own = conn is None
    if own:
        conn = db.get_conn()

    prompt_tok = completion_tok = 0
    cost = 0.0
    in_price = out_price = 0.0
    content = ""
    try:
        from openai import OpenAI
        client = OpenAI(
            api_key=settings.DEEPSEEK_API_KEY,
            base_url=settings.DEEPSEEK_BASE_URL,
            timeout=timeout,
        )
        messages = [{"role": "user", "content": user_prompt}]
        if system_prompt:
            messages.insert(0, {"role": "system", "content": system_prompt})
        kwargs = {
            "model": model, "messages": messages, "max_tokens": max_tokens,
            "extra_body": {"thinking": {"type": "enabled" if thinking else "disabled"}},
        }
        if response_format is not None:
            kwargs["response_format"] = response_format
        if temperature is not None:
            kwargs["temperature"] = temperature
        resp = client.chat.completions.create(**kwargs)
        content = resp.choices[0].message.content or ""
        usage = resp.usage
        prompt_tok = usage.prompt_tokens or 0
        completion_tok = usage.completion_tokens or 0
    except Exception:
        _record(conn, batch_id, stage, model, prompt_tok, completion_tok, cost,
                in_price, out_price, content, commit=own)
        raise
    else:
        in_price, out_price = _unit_price(model)
        cost = prompt_tok * in_price + completion_tok * out_price
        _record(conn, batch_id, stage, model, prompt_tok, completion_tok, cost,
                in_price, out_price, content, commit=own)
        return content
    finally:
        if own:
            conn.close()


def _record(conn, batch_id, stage, model, pt, ct, cost, in_p, out_p, content, commit):
    """写一条 llm_calls 调用记录。调用即记账：无论成败都落库。"""
    conn.execute(
        "INSERT INTO llm_calls(batch_id, stage, provider, model, prompt_tokens, "
        "completion_tokens, unit_price_in, unit_price_out, cost, created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (batch_id, stage, "deepseek", model, pt, ct, in_p, out_p, cost, _now()),
    )
    if commit:
        conn.commit()
