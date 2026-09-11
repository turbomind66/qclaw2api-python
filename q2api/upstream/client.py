"""client.py — QClaw 上游客户端（OpenAI 协议透传 + 两条 QClaw 特有约束修正）。

这是相对 workbuddy2api 简化最多的地方：上游就是 OpenAI 协议，所以
**不需要** 请求体重写、SSE 重新封装、指纹脱敏、tool_call 增量合并 —— 进出都是
标准 OpenAI 报文，本模块只做三件事：

1. 换 Authorization（客户端的 key → 上游 apiKey）
2. **自动补 system 消息**：QClaw 网关只传单条 user 消息会返回 400 invalid request
3. **max_tokens 钳制**：QClaw 所有模型最大输出 8192，超出会被拒

因此流式场景直接转发上游 SSE 字节流即可，不再需要 sse.py 那一层。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests

from q2api.auth import Auth
from q2api.upstream import consts, errors

LOG = logging.getLogger("q2api.upstream")

_LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "::1", "0.0.0.0", "")


def _is_loopback(url: str) -> bool:
    """判断 base url 是否指向本机回环。

    存在的意义：很多办公环境设置了 HTTP_PROXY，requests 默认会连环境变量里的代理。
    访问公网的上游网关时这是**期望行为**（公司网络需要），但如果 base_url 指向
    本机的自建网关/假上游，再绕一层代理就会出现各种诡异故障（404 / Bad request
    syntax / RemoteDisconnected）。因此回环地址一律禁用 trust_env。
    """
    try:
        host = (urlparse(url).hostname or "").lower().strip("[]")
    except ValueError:
        return False
    return host in _LOOPBACK_HOSTS or host.startswith("127.")


class Client:
    def __init__(self, base_url: str = "", timeout_seconds: int = 120,
                 max_tokens_cap: int = 0, auto_system_prompt: bool = True,
                 auto_system_text: str = "", default_model: str = "") -> None:
        self.base = (base_url or consts.DEFAULT_BASE_URL).rstrip("/")
        self.timeout = timeout_seconds if timeout_seconds > 0 else 120
        # max_tokens_cap <= 0 表示「不钳制」，原样透传给上游（本地 openclaw
        # 网关由自身限制出参长度；远程 QClaw 用户可在配置里显式设 8192）。
        self.max_tokens_cap = max_tokens_cap
        self.auto_system_prompt = auto_system_prompt
        self.auto_system_text = auto_system_text or "You are a helpful assistant."
        self.default_model = default_model or consts.DEFAULT_MODEL
        self.sess = requests.Session()
        # 回环地址绕过系统代理（原因见 _is_loopback 文档字符串）
        self.loopback = _is_loopback(self.base)
        if self.loopback:
            self.sess.trust_env = False

    # ---- URL ----
    @property
    def chat_url(self) -> str:
        return self.base + "/chat/completions"

    @property
    def models_url(self) -> str:
        return self.base + "/models"

    # ---- 请求体改写 ----
    def prepare_body(self, raw: bytes) -> Tuple[bytes, List[str]]:
        """返回 (转发体, 改写说明)。解析失败时原样透传（交由上游报错）。"""
        notes: List[str] = []
        try:
            obj = json.loads(raw)
        except (json.JSONDecodeError, ValueError, TypeError):
            return raw, ["body:非JSON，原样透传"]
        if not isinstance(obj, dict):
            return raw, ["body:非对象，原样透传"]

        # 1) 模型名归一化
        m = obj.get("model")
        if isinstance(m, str):
            nm = consts.normalize_model(m, self.default_model)
            if nm != m:
                obj["model"] = nm
                notes.append(f"model:{m}→{nm}")

        # 2) 自动补 system（QClaw 硬约束：仅一条 user 会 400 invalid request）
        msgs = obj.get("messages")
        if self.auto_system_prompt and isinstance(msgs, list) and msgs:
            has_system = any(isinstance(x, dict) and x.get("role") == "system" for x in msgs)
            if not has_system:
                obj["messages"] = [{"role": "system", "content": self.auto_system_text}] + msgs
                notes.append("messages:自动补 system")

        # 3) max_tokens 钳制（仅当 max_tokens_cap > 0 时生效；0 表示不钳制）
        if self.max_tokens_cap > 0:
            for key in ("max_tokens", "max_completion_tokens"):
                v = obj.get(key)
                if isinstance(v, bool):
                    continue
                if isinstance(v, (int, float)) and v > self.max_tokens_cap:
                    obj[key] = self.max_tokens_cap
                    notes.append(f"{key}:{int(v)}→{self.max_tokens_cap}(钳制)")

        try:
            out = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        except (TypeError, ValueError) as e:
            LOG.warning("prepare_body: 序列化失败，原样透传: %s", e)
            return raw, ["body:序列化失败，原样透传"]
        return out, notes

    def _headers(self, acct: Auth, stream: bool) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {acct.api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if stream else "application/json",
            "User-Agent": consts.CLIENT_UA,
        }

    @staticmethod
    def _is_stream(raw: bytes) -> bool:
        try:
            obj = json.loads(raw)
        except (json.JSONDecodeError, ValueError, TypeError):
            return False
        return isinstance(obj, dict) and bool(obj.get("stream"))

    # ---- 对话 ----
    def chat(self, acct: Auth, raw: bytes):
        """发起对话。

        返回 (Response|None, http_status, body_bytes, err|None)。
        调用方负责消费 resp（流式转发 resp.raw，非流式读 resp.content 后 json 解析）。
        """
        prepared, notes = self.prepare_body(raw)
        stream = self._is_stream(prepared)
        if notes:
            LOG.info("upstream uid=%s 改写: %s", acct.uid, " | ".join(notes))
        try:
            resp = self.sess.post(
                self.chat_url,
                data=prepared,
                headers=self._headers(acct, stream),
                timeout=(10, self.timeout),
                stream=True,
            )
        except requests.RequestException as e:
            return None, 0, b"", e
        return resp, resp.status_code, b"", None

    # ---- 模型列表 ----
    def fetch_models(self, acct: Auth) -> Tuple[List[str], Optional[Exception]]:
        """拉取上游 /v1/models。失败返回 ([], err)。"""
        try:
            r = self.sess.get(self.models_url, headers=self._headers(acct, False),
                              timeout=(10, 15))
        except requests.RequestException as e:
            return [], e
        if r.status_code >= 400:
            return [], errors.Error(errors.classify(r.status_code, r.text),
                                    r.status_code, r.text[:200])
        try:
            env = r.json()
        except ValueError as e:
            return [], e
        data = env.get("data") if isinstance(env, dict) else None
        ids: List[str] = []
        if isinstance(data, list):
            for it in data:
                if isinstance(it, dict) and isinstance(it.get("id"), str):
                    ids.append(it["id"])
                elif isinstance(it, str):
                    ids.append(it)
        return ids, None

    # ---- 探活 ----
    def probe(self, acct: Auth) -> Optional[Exception]:
        """用最小请求验证 key 是否可用（1 个 token 即可）。

        QClaw 必须带 system 消息，否则会 400，这里显式带上。
        """
        body = json.dumps({
            "model": self.default_model,
            "messages": [
                {"role": "system", "content": "hi"},
                {"role": "user", "content": "hi"},
            ],
            "max_tokens": 1,
        }).encode("utf-8")
        resp, status, _, err = self.chat(acct, body)
        if err is not None:
            return err
        try:
            if resp is not None:
                if status >= 400:
                    return errors.Error(errors.classify(status, resp.text[:400]), status,
                                        resp.text[:200])
                resp.close()
                return None
        finally:
            if resp is not None:
                try:
                    resp.close()
                except Exception:
                    pass
        return None
