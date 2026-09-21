"""config.py — 加载 JSON 配置 + Q2A_* 环境变量覆盖 + 时长解析。

相对 workbuddy2api 的裁剪：
  - 去掉 region（QClaw 只有国内一个网关）
  - 去掉 schedule.checkin_hours / keepalive_hours（apiKey 无签到保活概念），
    改为 schedule.probe_interval（定期对 key 做一次连通性探活）
  - 去掉 features.sanitize_blacklist_fingerprints（上游是原生 OpenAI 协议，无需脱敏）
  - 新增 upstream.base_url（可被 Q2A_UPSTREAM_BASE 覆盖，用于对接假上游做冒烟）
  - 新增 upstream.max_tokens_cap / upstream.auto_system_prompt / upstream.default_model
    （QClaw 本地网关约束；max_tokens_cap=0 表示不钳制）
"""
from __future__ import annotations

import json
import os
import re
from datetime import timedelta
from types import SimpleNamespace
from typing import Optional

_DURATION_UNITS = {
    "ns": 1e-9, "us": 1e-6, "µs": 1e-6, "ms": 1e-3,
    "s": 1.0, "m": 60.0, "h": 3600.0,
}
_DURATION_RE = re.compile(r"(-?\d+(?:\.\d+)?)(ns|us|µs|ms|s|m|h)")


def parse_duration(s) -> timedelta:
    """解析 Go 风格时长字符串（"60s" / "30m" / "1h30m" / "500ms"）。

    入参归一化（项目级约定，防止 timedelta 与 int 秒混用）：
      - timedelta → 原样返回
      - int/float → 视为秒
      - 字符串 → 解析后缀
    """
    if isinstance(s, timedelta):
        return s
    if isinstance(s, bool):  # bool 是 int 子类，显式排除
        raise ValueError(f"invalid duration: {s!r}")
    if isinstance(s, (int, float)):
        return timedelta(seconds=float(s))

    s = (s or "").strip()
    if not s:
        raise ValueError("empty duration")
    neg = False
    if s.startswith("-"):
        neg = True
        s = s[1:]
    matches = _DURATION_RE.findall(s)
    if not matches:
        raise ValueError(f"invalid duration: {s!r}")
    total = 0.0
    for val, unit in matches:
        total += float(val) * _DURATION_UNITS[unit]
    if neg:
        total = -total
    return timedelta(seconds=total)


def _bool_env(v: Optional[str], default: bool) -> bool:
    if not v:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


class Config:
    def __init__(self) -> None:
        # 顶层
        self.listen = ":7864"
        self.api_key = ""
        self.auth_dir = "./auths"
        self.state_file = "./data/state.json"

        # 嵌套子结构
        self.Cooldown = SimpleNamespace(SoftRate="60s")
        self.Schedule = SimpleNamespace(ProbeInterval="30m")
        self.Upstream = SimpleNamespace(
            BaseURL="",
            GatewayConfigPath="~/.qclaw/openclaw.json",
            TimeoutSeconds=120,
            MaxTokensCap=0,
            AutoSystemPrompt=True,
            AutoSystemText="You are a helpful assistant.",
            DefaultModel="openclaw",
        )
        self.Upstash = SimpleNamespace(URL="", Token="")
        self.Pool = SimpleNamespace(
            MaxInFlight=3,
            BreakerThreshold=3,
            BreakerCooldown="30m",
            BreakerCooldownMax="6h",
            IdleWeightPerHour=0.5,
            IdleWeightMax=5.0,
        )
        self.SessionSticky = SimpleNamespace(Enabled=True, TTL="30m", GCInterval="5m")

        # 解析后
        self.SoftRateDur = parse_duration("60s")
        self.ProbeIntervalDur = parse_duration("30m")
        self.BreakerCooldownDur = parse_duration("30m")
        self.BreakerCooldownMaxD = parse_duration("6h")
        self.SessionTTL = parse_duration("30m")
        self.SessionGCInterval = parse_duration("5m")

    @classmethod
    def default(cls) -> "Config":
        return cls()

    @classmethod
    def load(cls, path: str) -> "Config":
        c = cls.default()
        if path:
            with open(path, "r", encoding="utf-8") as f:
                raw = f.read()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError as e:
                raise ValueError(f"parse config: {e}")
            c._apply_dict(data)
        c._apply_env()
        c._normalize()
        return c

    def _apply_dict(self, data: dict) -> None:
        if "listen" in data:
            self.listen = data["listen"]
        if "api_key" in data:
            self.api_key = data["api_key"]
        if "auth_dir" in data:
            self.auth_dir = data["auth_dir"]
        if "state_file" in data:
            self.state_file = data["state_file"]
        cd = data.get("cooldown")
        if isinstance(cd, dict) and "soft_rate" in cd:
            self.Cooldown.SoftRate = cd["soft_rate"]
        sch = data.get("schedule")
        if isinstance(sch, dict) and "probe_interval" in sch:
            self.Schedule.ProbeInterval = sch["probe_interval"]
        up = data.get("upstream")
        if isinstance(up, dict):
            if "base_url" in up:
                self.Upstream.BaseURL = up["base_url"]
            if "gateway_config_path" in up:
                self.Upstream.GatewayConfigPath = up["gateway_config_path"]
            if "timeout_seconds" in up:
                self.Upstream.TimeoutSeconds = up["timeout_seconds"]
            if "max_tokens_cap" in up:
                self.Upstream.MaxTokensCap = up["max_tokens_cap"]
            if "auto_system_prompt" in up:
                self.Upstream.AutoSystemPrompt = up["auto_system_prompt"]
            if "auto_system_text" in up:
                self.Upstream.AutoSystemText = up["auto_system_text"]
            if "default_model" in up:
                self.Upstream.DefaultModel = up["default_model"]
        us = data.get("upstash")
        if isinstance(us, dict):
            if "url" in us:
                self.Upstash.URL = us["url"]
            if "token" in us:
                self.Upstash.Token = us["token"]
        pl = data.get("pool")
        if isinstance(pl, dict):
            for k, attr in (("max_in_flight", "MaxInFlight"),
                            ("breaker_threshold", "BreakerThreshold"),
                            ("breaker_cooldown", "BreakerCooldown"),
                            ("breaker_cooldown_max", "BreakerCooldownMax"),
                            ("idle_weight_per_hour", "IdleWeightPerHour"),
                            ("idle_weight_max", "IdleWeightMax")):
                if k in pl:
                    setattr(self.Pool, attr, pl[k])
        ss = data.get("session_sticky")
        if isinstance(ss, dict):
            if "enabled" in ss:
                self.SessionSticky.Enabled = ss["enabled"]
            if "ttl" in ss:
                self.SessionSticky.TTL = ss["ttl"]
            if "gc_interval" in ss:
                self.SessionSticky.GCInterval = ss["gc_interval"]

    def _apply_env(self) -> None:
        if v := os.environ.get("Q2A_LISTEN"):
            self.listen = v
        if v := os.environ.get("Q2A_API_KEY"):
            self.api_key = v
        if v := os.environ.get("Q2A_AUTH_DIR"):
            self.auth_dir = v
        if v := os.environ.get("Q2A_STATE_FILE"):
            self.state_file = v
        if v := os.environ.get("Q2A_SOFT_RATE"):
            self.Cooldown.SoftRate = v
        if v := os.environ.get("Q2A_UPSTREAM_BASE"):
            self.Upstream.BaseURL = v
        if v := os.environ.get("Q2A_TIMEOUT_SECONDS"):
            try:
                self.Upstream.TimeoutSeconds = int(v)
            except ValueError:
                pass
        if v := os.environ.get("Q2A_AUTO_SYSTEM_PROMPT"):
            self.Upstream.AutoSystemPrompt = _bool_env(v, True)
        if v := os.environ.get("Q2A_UPSTREAM_DEFAULT_MODEL"):
            self.Upstream.DefaultModel = v

    @staticmethod
    def resolve_gateway(path: str):
        """从 QClaw 的 openclaw.json 读取网关 port（与 token），自动构造上游 base_url。

        用途：当 `upstream.base_url` 设为 "auto"（或留空）时，跟随 QClaw 网关的
        实际监听端口，避免网关重启换端口后代理因硬编码旧端口而静默 502。

        返回 (base_url, token)：
          - base_url: "http://127.0.0.1:<port>/v1"
          - token:    gateway.auth.token（可能为空串，调用方按需覆盖账号凭证）
        失败抛异常（文件缺失 / gateway.port 缺失），由调用方给出明确告警。
        """
        from pathlib import Path as _Path
        p = _Path(os.path.expanduser(path)).expanduser()
        if not p.is_file():
            raise FileNotFoundError(f"gateway config not found: {p}")
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            raise ValueError(f"read gateway config {p}: {e}")
        gw = data.get("gateway") or {}
        port = gw.get("port")
        if not port:
            raise ValueError(f"gateway.port missing in {p}")
        token = (gw.get("auth") or {}).get("token", "") or ""
        base = f"http://127.0.0.1:{port}/v1"
        return base, token

    def _normalize(self) -> None:
        self.SoftRateDur = parse_duration(self.Cooldown.SoftRate)
        self.ProbeIntervalDur = parse_duration(self.Schedule.ProbeInterval)
        self.BreakerCooldownDur = parse_duration(self.Pool.BreakerCooldown)
        self.BreakerCooldownMaxD = parse_duration(self.Pool.BreakerCooldownMax)
        self.SessionTTL = parse_duration(self.SessionSticky.TTL)
        self.SessionGCInterval = parse_duration(self.SessionSticky.GCInterval)

        if self.Pool.BreakerThreshold <= 0:
            self.Pool.BreakerThreshold = 3
        if self.Pool.IdleWeightPerHour <= 0:
            self.Pool.IdleWeightPerHour = 0.5
        if self.Pool.IdleWeightMax <= 0:
            self.Pool.IdleWeightMax = 5.0
        if self.Upstream.TimeoutSeconds <= 0:
            self.Upstream.TimeoutSeconds = 120
        # MaxTokensCap <= 0 表示「不钳制」，原样透传（本地 openclaw 网关由自身限长）
        if not str(self.Upstream.AutoSystemText or "").strip():
            self.Upstream.AutoSystemText = "You are a helpful assistant."
        if not (self.listen.startswith(":") or ":" in self.listen):
            self.listen = ":" + self.listen
