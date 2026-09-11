"""qclaw2api — 把腾讯 QClaw 的 apiKey 封装成标准 OpenAI API 的反向代理。

上游网关（https://mmgrcalltoken.3g.qq.com/aizone/v1）本身就是 OpenAI 兼容协议，
所以本项目的职责不是「协议转换」，而是：
  - 多 key 账号池轮换 + 熔断 + 冷却
  - 粘性会话
  - 修正 QClaw 的两条硬约束（必须带 system 消息 / max_tokens ≤ 8192）
"""
from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
