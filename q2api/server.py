"""server.py — OpenAI 兼容 HTTP 接口：pool 挑号 + 上游透传。

相对 workbuddy2api 的简化：
  - **没有 token refresh**（apiKey 是静态凭证）
  - **流式直接转发上游 SSE 字节流**，不再解析再封装（上游本就是 OpenAI 协议）
  - **非流式直接回吐上游 JSON**，不做聚合/改写
因此本模块只剩「挑号 → 转发 → 按错误分类冷却/换号」这三件事。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse

from q2api.pool import COOL_SOFT, Pool
from q2api.session import Router, extract_key
from q2api.upstream import Client, ErrKind, classify, consts, openai_error_body

LOG = logging.getLogger("q2api.server")

DEFAULT_SOFT_COOLDOWN = timedelta(seconds=60)
DEFAULT_MAX_ROTATE = 3

# 这些状态码属于「请求体本身不合法」，换号重试不会改变结果，直接透传上游错误，
# 避免白烧 3 次额度且把真实原因吞掉。
_NO_ROTATE_STATUS = (400, 415, 422)

_MODELS_TTL = timedelta(hours=1)
_MODELS_FAIL_COOLDOWN = timedelta(minutes=5)
_models_lock = threading.RLock()
_models_cache: List[str] = []
_models_fetched: Optional[datetime] = None
_models_last_fail: Optional[datetime] = None


class Config:
    def __init__(self, pool=None, upstream=None, api_key="", session=None,
                 sticky_count=None, redis_mode=None, soft_cooldown=None,
                 max_rotate=None) -> None:
        self.pool: Optional[Pool] = pool
        self.upstream: Optional[Client] = upstream
        self.api_key = api_key
        self.max_rotate = max_rotate if max_rotate is not None else DEFAULT_MAX_ROTATE
        self.session: Optional[Router] = session
        self.sticky_count = sticky_count  # () -> int
        self.redis_mode = redis_mode if redis_mode is not None else "noop"
        self.soft_cooldown = soft_cooldown if soft_cooldown is not None else DEFAULT_SOFT_COOLDOWN


def _model_entries(ids: List[str]) -> List[Dict[str, Any]]:
    out = []
    for mid in ids:
        out.append({
            "id": mid,
            "object": "model",
            "created": 1753600000,
            "owned_by": "qclaw",
            "context_length": consts.CONTEXT_WINDOW,
            "max_output_tokens": consts.MAX_OUTPUT_TOKENS,
        })
    return out


class Handler(BaseHTTPRequestHandler):
    cfg: Config = None  # type: ignore
    protocol_version = "HTTP/1.1"
    _headers_sent = False

    def log_message(self, fmt, *args):  # 静默默认访问日志
        return

    def send_response(self, code, message=None):
        self._headers_sent = True
        super().send_response(code, message)

    def handle(self):
        # 客户端在请求任意阶段断开（WinError 10053/10054 等）时静默关闭连接，
        # 避免 socketserver 框架层打印整段 traceback 噪音。
        try:
            super().handle()
        except ConnectionError:
            self.close_connection = True
        except Exception:
            LOG.exception("unhandled error serving %s", self.path)
            self.close_connection = True
            try:
                if not self._headers_sent:
                    self._send_openai_error(500, "internal_error", "internal server error")
            except Exception:  # noqa
                pass

    # ---- 路由 ----
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/v1/models":
            if not self._check_auth():
                return
            self.models()
        elif path == "/status":
            if not self._check_auth():
                return
            self.status()
        elif path == "/healthz":
            self.healthz()
        else:
            self._send_openai_error(404, "not_found", "not found")

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/v1/chat/completions":
            if not self._check_auth():
                return
            self.chat_completions()
        else:
            self._send_openai_error(404, "not_found", "not found")

    def _check_auth(self) -> bool:
        cfg = self.cfg
        if not cfg.api_key:
            return True
        authz = self.headers.get("Authorization", "")
        if not authz.startswith("Bearer ") or authz[len("Bearer "):] != cfg.api_key:
            self._send_openai_error(401, "invalid_api_key", "missing or invalid API key")
            return False
        return True

    # ---- 发送 ----
    def _send_json(self, status: int, v: Any) -> None:
        raw = json.dumps(v, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _send_raw(self, status: int, raw: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _send_openai_error(self, status: int, code: str, msg: str) -> None:
        self._send_json(status, {"error": {"message": msg, "type": "api_error", "code": code}})

    # ---- 端点 ----
    def healthz(self):
        total, healthy, _, _, _ = self.cfg.pool.counts_detailed()
        status = 200 if self.cfg.pool.servable_now() else 503
        self._send_json(status, {"healthy": healthy, "total": total})

    def status(self):
        total, healthy, cooling, disabled, in_flight_full = self.cfg.pool.counts_detailed()
        sticky = self.cfg.sticky_count() if self.cfg.sticky_count is not None else 0
        self._send_json(200, {
            "accounts": self.cfg.pool.list(),
            "total": total,
            "healthy": healthy,
            "cooling": cooling,
            "disabled": disabled,
            "in_flight_full": in_flight_full,
            "sticky_sessions": sticky,
            "redis_mode": self.cfg.redis_mode or "noop",
            "upstream_base": getattr(self.cfg.upstream, "base", ""),
        })

    def models(self):
        ids = self._fetch_dynamic_models()
        data = _model_entries(ids) if ids else _model_entries(
            [m["id"] for m in consts.STATIC_MODELS])
        self._send_json(200, {"object": "list", "data": data})

    def _fetch_dynamic_models(self) -> List[str]:
        global _models_cache, _models_fetched, _models_last_fail
        with _models_lock:
            if _models_cache and _models_fetched and (datetime.now() - _models_fetched) < _MODELS_TTL:
                return list(_models_cache)
            if _models_last_fail and (datetime.now() - _models_last_fail) < _MODELS_FAIL_COOLDOWN:
                return []
        acct = self.cfg.pool.pick()
        if acct is None:
            return []
        ids, err = self.cfg.upstream.fetch_models(acct)
        with _models_lock:
            if err is not None or not ids:
                _models_last_fail = datetime.now()
                return []
            _models_cache[:] = ids
            _models_fetched = datetime.now()
            _models_last_fail = None
            return list(ids)

    # ---- 对话 ----
    def chat_completions(self):
        clen = int(self.headers.get("Content-Length", "0") or 0)
        body = self.rfile.read(clen) if clen > 0 else b""
        self._chat_loop(body)

    def _chat_loop(self, body: bytes) -> None:
        cfg = self.cfg
        pool = cfg.pool
        up = cfg.upstream

        tried: Dict[str, bool] = {}
        last_status = 503
        last_body = ""

        sess_key = ""
        sticky_uid = ""
        if cfg.session is not None:
            sess_key = extract_key(body)
            if sess_key:
                r = cfg.session.resolve(sess_key)
                if r[1]:
                    sticky_uid = r[0]

        held_uid = ""
        started = time.time()

        def release_held():
            nonlocal held_uid
            if held_uid:
                pool.release(held_uid)
                held_uid = ""

        def fail(uid):
            # sticky_uid 在下方被重新赋值，必须声明 nonlocal，否则它会被视为
            # fail() 的局部变量，读取时抛 UnboundLocalError（Python 闭包作用域陷阱）。
            nonlocal sticky_uid
            release_held()
            if sticky_uid and uid == sticky_uid and cfg.session is not None:
                cfg.session.unbind(sess_key)
                sticky_uid = ""

        for _ in range(cfg.max_rotate):
            acct = None
            if sticky_uid:
                acct = pool.pick_by_uid(sticky_uid)
                if acct is None and cfg.session is not None:
                    cfg.session.unbind(sess_key)
                    sticky_uid = ""
            if acct is None:
                acct = pool.pick_excluding(tried)
            if acct is None:
                break
            tried[acct.uid] = True

            if not pool.acquire(acct.uid):
                if sticky_uid and acct.uid == sticky_uid and cfg.session is not None:
                    cfg.session.unbind(sess_key)
                    sticky_uid = ""
                continue
            held_uid = acct.uid

            resp, status, _, err = up.chat(acct, body)
            if err is not None:
                LOG.warning("chat uid=%s: 上游请求异常: %s", acct.uid[:12], err)
                pool.note_error(acct.uid)
                fail(acct.uid)
                last_status, last_body = 502, str(err)
                continue

            if status >= 400:
                body_txt = resp.text if resp is not None else ""
                if resp is not None:
                    resp.close()
                kind = classify(status, body_txt)
                last_status, last_body = status, body_txt
                self._apply_error_policy(acct.uid, kind)

                if kind == ErrKind.PARAM or status in _NO_ROTATE_STATUS:
                    LOG.warning("chat uid=%s: upstream %d 参数错误，停止换号并透传: %s",
                                acct.uid[:12], status, body_txt[:300])
                    release_held()
                    self._send_json(status, openai_error_body(status, body_txt))
                    return
                fail(acct.uid)
                continue

            # ---- 成功 ----
            pool.note_success(acct.uid)
            if sess_key and cfg.session is not None:
                cfg.session.bind(sess_key, acct.uid)

            try:
                self._relay(acct, resp, body, started)
            finally:
                release_held()
            return

        # 转完所有账号都没成功
        self._send_json(last_status, openai_error_body(last_status, last_body)
                        if last_body else
                        {"error": {"message": "no available account", "type": "api_error",
                                   "code": "no_available_account"}})

    def _relay(self, acct, resp, req_body: bytes, started: float) -> None:
        """把上游响应原样回给客户端（流式转发字节 / 非流式回吐 JSON）。"""
        stream = False
        try:
            stream = bool(json.loads(req_body).get("stream"))
        except (json.JSONDecodeError, ValueError, TypeError, AttributeError):
            stream = False

        if stream:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("X-Q2A-Uid", acct.uid)
            self.end_headers()
            try:
                first = True
                for chunk in resp.iter_content(chunk_size=8192):
                    if not chunk:
                        continue
                    if first:
                        first = False
                        LOG.info("chat uid=%s stream TTFB=%.0fms", acct.uid[:12],
                                 (time.time() - started) * 1000)
                    self.wfile.write(chunk)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionError):
                pass
            # Python http.server 不会自动 chunked；HTTP/1.1 keep-alive 下若
            # 不关闭连接，部分客户端会 hang 在「等响应结束」。SSE 发完 [DONE]
            # 后主动关闭连接，确保客户端正确结束。
            self.close_connection = True
            return

        raw = resp.content
        ctype = resp.headers.get("Content-Type", "application/json")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("X-Q2A-Uid", acct.uid)
        self.end_headers()
        self.wfile.write(raw)

    def _apply_error_policy(self, uid: str, kind: ErrKind) -> None:
        """按错误分类决定账号处置策略。"""
        pool = self.cfg.pool
        if kind == ErrKind.AUTH:
            pool.disable(uid, "apiKey invalid or revoked")
            LOG.error("chat uid=%s: apiKey 失效，已禁用该账号", uid[:12])
        elif kind == ErrKind.HARD_CREDIT:
            pool.cooldown_until_tomorrow_4am(uid, "quota exhausted")
            LOG.warning("chat uid=%s: 额度耗尽，冷却至次日 04:00", uid[:12])
        elif kind == ErrKind.SOFT_RATE:
            pool.cooldown(uid, COOL_SOFT, self.cfg.soft_cooldown, "429 soft rate")
        elif kind == ErrKind.NOT_FOUND:
            pool.cooldown(uid, COOL_SOFT, self.cfg.soft_cooldown, "404 not found")
        else:
            pool.note_error(uid)


def _split_listen(listen: str):
    """":7864" → ("0.0.0.0", 7864)；"127.0.0.1:9000" → ("127.0.0.1", 9000)。"""
    listen = (listen or ":7864").strip()
    if listen.startswith(":"):
        return "0.0.0.0", int(listen[1:] or 7864)
    if ":" in listen:
        host, _, port = listen.rpartition(":")
        return host or "0.0.0.0", int(port)
    return "0.0.0.0", int(listen)


def serve(cfg: Config, listen: str = ":7864"):
    host, port = _split_listen(listen)
    Handler.cfg = cfg
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    LOG.info("listening on %s:%d", host, port)
    return httpd
