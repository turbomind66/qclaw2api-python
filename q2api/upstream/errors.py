"""errors.py — 上游错误分类（驱动 pool 冷却状态机）。

相对 workbuddy2api：去掉了 SESSION_DEAD（OAuth session 概念，apiKey 不存在），
新增 PARAM（400 参数错误：换号重试毫无意义，必须直接透传）。
"""
from __future__ import annotations

from enum import IntEnum
from typing import Optional


class ErrKind(IntEnum):
    NONE = 0          # 成功
    HARD_CREDIT = 1   # 额度/积分耗尽 → 长冷却至次日 04:00
    SOFT_RATE = 2     # 429 软限流 → 短冷却
    NOT_FOUND = 4     # 404 上游偶发 → 短冷却，不累计错误计数（防雪崩）
    SERVER = 5        # 5xx 上游故障
    PARAM = 6         # 400 参数错误（含 invalid request / proxy_param_error）
    CLIENT = 7        # 其他 4xx / 业务错误
    AUTH = 8          # 401/403：apiKey 失效 → 禁用该账号


_KIND_NAMES = {
    ErrKind.NONE: "none",
    ErrKind.HARD_CREDIT: "hard_credit",
    ErrKind.SOFT_RATE: "soft_rate",
    ErrKind.NOT_FOUND: "not_found",
    ErrKind.SERVER: "server",
    ErrKind.PARAM: "param",
    ErrKind.CLIENT: "client",
    ErrKind.AUTH: "auth",
}


def kind_name(k: ErrKind) -> str:
    return _KIND_NAMES.get(k, "none")


# 额度耗尽关键词（小写比较 + 中文原文双通道）
HARD_MARKERS = [
    "insufficient credit", "no credit", "credit exhausted", "out of credit",
    "quota exceeded", "quota exhaust", "payment required", "credit not enough",
    "not enough credit", "insufficient balance", "insufficient_quota",
    "积分不足", "额度不足", "余额不足", "积分用完", "额度用尽", "没有积分",
    "今日额度已用完", "免费额度已用完",
]

# 400 参数错误特征（QClaw 实测会返回 proxy_param_error / invalid request）
PARAM_MARKERS = [
    "proxy_param_error", "invalid request", "model_param_invalid",
    "invalid_request_error", "invalid model", "unknown model",
]


class Error(Exception):
    """带分类的上游错误。"""

    def __init__(self, kind: ErrKind, status: int, msg: str) -> None:
        self.kind = kind
        self.status = status
        self.msg = msg
        super().__init__(f"upstream {kind_name(kind)} (http {status}): {msg}")


def classify(status: int, body: str) -> ErrKind:
    """按 HTTP 状态码 + body 判定错误类别。"""
    if status in (401, 403):
        return ErrKind.AUTH
    if status == 402:
        return ErrKind.HARD_CREDIT
    lower = (body or "").lower()
    for m in HARD_MARKERS:
        if m.lower() in lower or m in (body or ""):
            return ErrKind.HARD_CREDIT
    if status == 400:
        for m in PARAM_MARKERS:
            if m.lower() in lower or m in (body or ""):
                return ErrKind.PARAM
        return ErrKind.PARAM
    if status == 429:
        return ErrKind.SOFT_RATE
    if status == 404:
        return ErrKind.NOT_FOUND
    if status >= 500:
        return ErrKind.SERVER
    if status >= 400:
        return ErrKind.CLIENT
    return ErrKind.NONE


def truncate(s: str, n: int) -> str:
    s = (s or "").strip()
    return s[:n] if len(s) > n else s


def openai_error_body(status: int, body_txt: str) -> dict:
    """把上游 OpenAI 风格错误体规整成 OpenAI 错误响应。"""
    code = "upstream_error"
    msg = truncate(body_txt, 2000) or f"upstream http {status}"
    try:
        import json
        env = json.loads(body_txt)
    except (ValueError, TypeError):
        env = None
    if isinstance(env, dict):
        err = env.get("error") if isinstance(env.get("error"), dict) else env
        if isinstance(err, dict):
            if err.get("code"):
                code = str(err["code"])
            if err.get("message"):
                msg = str(err["message"])
    return {"error": {"message": msg, "type": "api_error", "code": code}}
