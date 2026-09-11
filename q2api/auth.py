"""auth.py — QClaw apiKey 凭证解析。

与 workbuddy2api 的关键差异：
  - **静态凭证**：apiKey 是长期有效的 Bearer token，没有 access/refresh 之分，
    因此本模块**不需要** refresh / 过期判断 / 原子写回，逻辑大幅简化。
  - **uid 可缺省**：凭证文件里没写 uid 时，用 apiKey 的 sha256 前 12 位作为 uid
    （稳定且不会泄露 key 本身）。

凭证文件命名：`auths/qclaw-*.json`，内容最少只需一个字段：
    {"api_key": "sk-xxxx"}
可选字段：uid / nickname / note / disabled。
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import List

GLOB = "qclaw*.json"


def _fingerprint(api_key: str) -> str:
    """apiKey 指纹，作为缺省 uid（前 12 位十六进制）。"""
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:12]


def _mask(key: str) -> str:
    if len(key) <= 10:
        return "*" * len(key)
    return key[:6] + "..." + key[-4:]


class Auth:
    """归一化后的账号凭证。"""

    def __init__(self) -> None:
        self.api_key: str = ""
        self.uid: str = ""
        self.nickname: str = ""
        self.note: str = ""
        self.disabled: bool = False  # 凭证文件里手工禁用
        self.file_path: str = ""

    # ---- 展示用（绝不打印明文 key） ----
    def masked(self) -> str:
        return _mask(self.api_key)

    def label(self) -> str:
        return self.nickname or self.note or self.uid

    # ---- 解析 ----
    @staticmethod
    def parse(raw) -> "Auth":
        """兼容四种输入形态：
          - dict（已解析好的对象，便于程序内构造）
          - {"api_key": "..."} 的 JSON 文本 / bytes
          - {"key": "..."}
          - {"auth": {"apiKey": "..."}}
        """
        if isinstance(raw, dict):
            probe = raw
        else:
            if isinstance(raw, (bytes, bytearray)):
                raw = raw.decode("utf-8")
            if not raw or not raw.strip():
                raise ValueError("empty auth storage")
            try:
                probe = json.loads(raw)
            except json.JSONDecodeError as e:
                raise ValueError(f"storage_parse_error: {e}")
        if not isinstance(probe, dict):
            raise ValueError("storage_parse_error: top-level not object")

        a = Auth()
        inner = probe.get("auth") if isinstance(probe.get("auth"), dict) else probe
        a.api_key = (inner.get("api_key") or inner.get("apiKey")
                     or inner.get("key") or "").strip()
        a.uid = str(inner.get("uid") or "").strip()
        a.nickname = str(inner.get("nickname") or "").strip()
        a.note = str(inner.get("note") or "").strip()
        a.disabled = bool(inner.get("disabled", False))

        if not a.api_key:
            raise ValueError("parse_error: missing api_key")
        if not a.uid:
            a.uid = _fingerprint(a.api_key)
        return a

    def to_dict(self) -> dict:
        return {
            "uid": self.uid,
            "nickname": self.nickname,
            "note": self.note,
            "api_key": self.api_key,
        }

    def save_atomic(self, path: str = "") -> None:
        """原子写回凭证文件（tmp + rename）。用于 `cli/key.py add` 落盘。"""
        target = path or self.file_path
        if not target:
            raise ValueError("no file_path set")
        raw = json.dumps(self.to_dict(), indent=2, ensure_ascii=False).encode("utf-8")
        tmp = target + ".tmp"
        d = os.path.dirname(target)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(tmp, "wb") as f:
            f.write(raw)
        os.replace(tmp, target)

    # ---- 批量扫描 ----
    @staticmethod
    def load_dir(directory: str) -> List["Auth"]:
        """扫描 dir 下 qclaw*.json。解析失败的文件静默跳过。"""
        out: List[Auth] = []
        p = Path(directory)
        if not p.is_dir():
            return out
        for fp in sorted(p.glob(GLOB)):
            try:
                raw = fp.read_bytes()
            except OSError:
                continue
            try:
                a = Auth.parse(raw)
            except ValueError:
                continue
            a.file_path = str(fp)
            out.append(a)
        return out
