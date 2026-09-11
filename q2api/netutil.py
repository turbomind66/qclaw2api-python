"""netutil.py — 网络层小工具：本机回环的代理绕过。

背景（实测踩坑）：办公环境普遍设置了 `HTTP_PROXY`，而 `requests` 默认信任环境变量。
这会带来两类问题：

1. 上游网关在公网 → 走代理是**期望行为**（公司网络出口需要），必须保留；
2. base_url 指向本机（自建网关 / 假上游） → 绕一层代理就会出现
   404 / "Bad request syntax" / RemoteDisconnected 等与被测代码完全无关的假故障。

因此：
  - `Client` 在回环地址上把 Session 设为 `trust_env=False`（最彻底，只影响自己）；
  - CLI / 脚本里用裸 `requests` 的地方，调用 `ensure_no_proxy()` 补 `NO_PROXY`。
"""
from __future__ import annotations

import os
from urllib.parse import urlparse

_LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "::1", "0.0.0.0", "")


def is_loopback(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower().strip("[]")
    except ValueError:
        return False
    return host in _LOOPBACK_HOSTS or host.startswith("127.")


def ensure_no_proxy(extra=("127.0.0.1", "localhost")) -> str:
    """把回环地址补进 NO_PROXY / no_proxy（大小写两个变量都要设，不同库读的不一样）。"""
    parts = [p.strip() for p in os.environ.get("NO_PROXY", "").split(",") if p.strip()]
    for e in extra:
        if e not in parts:
            parts.append(e)
    val = ",".join(parts)
    os.environ["NO_PROXY"] = val
    os.environ["no_proxy"] = val
    return val
