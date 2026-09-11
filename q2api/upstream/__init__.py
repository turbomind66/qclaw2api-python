"""upstream 包：QClaw 网关客户端（OpenAI 兼容，透传为主）。"""
from __future__ import annotations

from q2api.upstream import consts, errors
from q2api.upstream.client import Client
from q2api.upstream.consts import (
    CONTEXT_WINDOW,
    DEFAULT_BASE_URL,
    MAX_OUTPUT_TOKENS,
    MODEL_AUTO,
    STATIC_MODELS,
    normalize_model,
)
from q2api.upstream.errors import ErrKind, Error, classify, kind_name, openai_error_body

__all__ = [
    "consts", "errors", "Client",
    "CONTEXT_WINDOW", "DEFAULT_BASE_URL", "MAX_OUTPUT_TOKENS", "MODEL_AUTO",
    "STATIC_MODELS", "normalize_model",
    "ErrKind", "Error", "classify", "kind_name", "openai_error_body",
]
