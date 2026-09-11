"""fake_upstream.py — 假 QClaw 上游（OpenAI 兼容），用于无 key 环境下的联调与冒烟。

**关键设计**：刻意复刻 QClaw 网关的真实约束，这样冒烟才能真正验证代理层的补丁生效：
  - 请求体没有 system 消息 → 400 invalid request（对应 client.prepare_body 的自动补 system）
  - max_tokens > 8192 → 400（对应 client 的钳制）
  - model=pool-hy3-preview → 400 proxy_param_error
  - key 含 "dead" → 401；key 含 "quota" → 402 额度不足

另一个坑（已在代码中处理）：HTTP/1.1 keep-alive 下，**任何提前返回的错误响应都必须先把
请求体读完**，否则残留字节会被下一次请求当作请求行解析，表现为 "Bad request syntax"。

用法：
    py scripts/fake_upstream.py            # 独立启动，默认 127.0.0.1:8799
    from scripts.fake_upstream import start  # 作为模块嵌入冒烟脚本
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional, Tuple

DEBUG = os.environ.get("Q2A_FAKE_DEBUG") == "1"

MAX_TOKENS = 8192
MODELS = ["openclaw", "openclaw/main", "openclaw/default"]

_LOCK = threading.Lock()
_LAST: Optional[Dict[str, Any]] = None  # 最近一次收到的请求体（供断言）
_HITS = 0


def last_request() -> Optional[Dict[str, Any]]:
    with _LOCK:
        return dict(_LAST) if _LAST else None


def hit_count() -> int:
    with _LOCK:
        return _HITS


def reset() -> None:
    global _LAST, _HITS
    with _LOCK:
        _LAST = None
        _HITS = 0


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        return

    def handle(self):
        # 客户端（代理）提前关闭连接是流式场景的常态，静默处理，
        # 避免 socketserver 打印 ConnectionAbortedError 噪音淹没测试结果。
        try:
            super().handle()
        except (ConnectionError, OSError):
            self.close_connection = True

    def _json(self, status: int, obj: Any) -> None:
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)
        self.wfile.flush()

    def _err(self, status: int, code: str, msg: str) -> None:
        self._json(status, {"error": {"message": msg, "type": "api_error", "code": code}})

    def _drain(self, clen: int) -> bytes:
        """读满 Content-Length 字节；keep-alive 下必须读完，否则残留会污染下一次请求。"""
        if clen <= 0:
            return b""
        return self.rfile.read(clen)

    def _record(self, obj: Dict[str, Any]) -> None:
        global _LAST, _HITS
        with _LOCK:
            _LAST = obj
            _HITS += 1

    def do_GET(self):
        if DEBUG:
            print(f"[fake] GET {self.path}", file=sys.stderr)
        if self.path == "/v1/models":
            now = int(time.time())
            self._json(200, {"object": "list", "data": [
                {"id": m, "object": "model", "created": now, "owned_by": "qclaw"} for m in MODELS]})
        elif self.path == "/_last":
            self._json(200, {"last": last_request(), "hits": hit_count()})
        elif self.path == "/_reset":
            reset()
            self._json(200, {"ok": True})
        else:
            self._err(404, "not_found", "not found")

    def do_POST(self):
        clen = int(self.headers.get("Content-Length", "0") or 0)
        if DEBUG:
            print(f"[fake] POST {self.path} clen={clen} "
                  f"auth={(self.headers.get('Authorization') or '')[:26]}", file=sys.stderr)
        if self.path != "/v1/chat/completions":
            self._drain(clen)
            self._err(404, "not_found", "not found")
            return

        raw = self._drain(clen)
        try:
            obj = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            self._err(400, "invalid_request", "body is not valid json")
            return
        if not isinstance(obj, dict):
            self._err(400, "invalid_request", "body is not an object")
            return

        # ---- 鉴权模拟 ----
        authz = self.headers.get("Authorization", "")
        key = authz[len("Bearer "):] if authz.startswith("Bearer ") else ""
        if not key:
            self._err(401, "invalid_api_key", "missing api key")
            return
        if "dead" in key:
            self._err(401, "invalid_api_key", "apiKey has been revoked")
            return
        if "quota" in key:
            self._err(402, "insufficient_quota", "额度不足")
            return

        # ---- QClaw 硬约束模拟 ----
        msgs = obj.get("messages")
        if not isinstance(msgs, list) or not msgs:
            self._err(400, "invalid_request", "messages required")
            return
        if not any(isinstance(m, dict) and m.get("role") == "system" for m in msgs):
            self._err(400, "invalid_request", "invalid request: system message required")
            return
        mt = obj.get("max_tokens")
        if isinstance(mt, bool):
            mt = None
        if isinstance(mt, int) and mt > MAX_TOKENS:
            self._err(400, "proxy_param_error", f"max_tokens {mt} exceeds {MAX_TOKENS}")
            return
        model = str(obj.get("model") or "")
        if model == "pool-hy3-preview":
            self._err(400, "proxy_param_error", "model not supported")
            return

        self._record(obj)

        if obj.get("stream"):
            self._stream(model)
        else:
            self._json(200, {
                "id": "chatcmpl-fake", "object": "chat.completion",
                "created": int(time.time()), "model": model,
                "choices": [{"index": 0, "message": {"role": "assistant",
                                                     "content": "你好，这是假上游的回复。"},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 6, "total_tokens": 11},
            })

    def _stream(self, model: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        now = int(time.time())
        pieces = ["你", "好", "，", "世", "界"]
        try:
            for p in pieces:
                chunk = {
                    "id": "chatcmpl-fake", "object": "chat.completion.chunk",
                    "created": now, "model": model,
                    "choices": [{"index": 0, "delta": {"content": p}, "finish_reason": None}],
                }
                self.wfile.write(b"data: " + json.dumps(chunk, ensure_ascii=False).encode("utf-8") + b"\n\n")
                self.wfile.flush()
                time.sleep(0.01)
            done = {
                "id": "chatcmpl-fake", "object": "chat.completion.chunk", "created": now,
                "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 5, "completion_tokens": len(pieces),
                          "total_tokens": 5 + len(pieces)},
            }
            self.wfile.write(b"data: " + json.dumps(done, ensure_ascii=False).encode("utf-8") + b"\n\n")
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionError):
            pass
        # 无 Content-Length 的 SSE 只能靠关闭连接告知客户端结束
        self.close_connection = True


def start(host: str = "127.0.0.1", port: int = 0) -> Tuple[str, "ThreadingHTTPServer"]:
    """启动假上游，返回 (base_url, httpd)。"""
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    real_port = httpd.server_address[1]
    return f"http://{host}:{real_port}/v1", httpd


def main() -> int:
    url, httpd = start("127.0.0.1", 8799)
    print(f"fake QClaw upstream listening: {url}")
    print("  POST /v1/chat/completions   GET /v1/models   GET /_last   GET /_reset")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
