"""consts.py — QClaw 上游常量。

QClaw（openclaw 桌面客户端）本地网关 `http://127.0.0.1:62522/v1` **本身就是
OpenAI 兼容协议**，因此这里没有「私有协议 → OpenAI」的字段映射表，只有模型清单
与两条 QClaw 特有的硬约束（见 client.prepare_body）。
"""
from __future__ import annotations

# 上游网关（OpenAI 兼容）。base_url 是「完整前缀」，需自带 /v1，
# 形如 http://127.0.0.1:62522/v1 或 https://mmgrcalltoken.3g.qq.com/aizone/v1，
# 不含 /chat/completions。client 会在此之上拼接 /chat/completions 与 /models。
DEFAULT_BASE_URL = "http://127.0.0.1:62522/v1"

# 默认映射模型：客户端发送 auto / 空 / gpt-4o 等别名时，统一映射到该模型。
# 本地 openclaw 网关接受的模型 id 是 openclaw / openclaw/main / openclaw/default。
DEFAULT_MODEL = "openclaw"

CLIENT_UA = "qclaw2api-python/0.1"

# Auto 路由模型别名（映射到 DEFAULT_MODEL）
MODEL_AUTO = DEFAULT_MODEL

# QClaw 当前所有模型统一规格（本地网关实测值，可能随官方调整）
CONTEXT_WINDOW = 200_000
MAX_OUTPUT_TOKENS = 8192

# 静态模型表：动态拉取失败时的兜底。
# 本地 openclaw 网关 GET /v1/models 实际返回 openclaw / openclaw/default / openclaw/main。
STATIC_MODELS = [
    {"id": "openclaw", "label": "OpenClaw（默认 agent）"},
    {"id": "openclaw/main", "label": "OpenClaw main agent"},
    {"id": "openclaw/default", "label": "OpenClaw default agent"},
]

STATIC_MODEL_IDS = {m["id"] for m in STATIC_MODELS}

# 触发 Auto 路由的别名 → 统一映射到 DEFAULT_MODEL（具体值由 Client 决定）
AUTO_ALIASES = {"", "auto", "auto-route", "modelroute", "qclaw", "default",
                "gpt-4o", "gpt-4", "claude", "claude-3", "claude-3.5", "claude-3.7",
                "deepseek", "deepseek-chat", "glm", "kimi", "minimax"}


def normalize_model(name: str, default_model: str = DEFAULT_MODEL) -> str:
    """把客户端传入的模型名映射到上游可接受的 id。

    规则（保守，映射不了就原样透传，让上游给明确报错）：
      - auto 系列别名（含常见 OpenAI/Claude/DeepSeek 名字）→ default_model
      - 其余原样透传（如 openclaw/main、openclaw/default 直接放行）
    """
    n = (name or "").strip()
    if n.lower() in AUTO_ALIASES:
        return default_model
    return n
