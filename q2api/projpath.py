"""projpath.py — 路径锚定。

把所有 CLI 入口用到的相对路径统一解析到**项目根**，而不是当前工作目录（cwd）。

背景（workbuddy2api-python 踩过的坑，原样继承）：
Python 版用户经常 `cd cli` 后直接跑 `server.py`，`./auths` 这类相对路径就变成了
`cli/auths`，结果是「加载 0 个账号」却没有任何报错，极难排查。

统一策略：
  - 绝对路径：原样使用。
  - 相对路径：先按 cwd 找，找到就用；找不到就回退到项目根。

**本项目的所有 CLI 入口都必须通过本模块解析路径，禁止直接写 "./auths"。**
"""
from __future__ import annotations

import os
import sys


def project_root() -> str:
    """项目根 = 本文件（q2api/projpath.py）的上两级目录。"""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


PROJECT_ROOT = project_root()


def resolve(p: str, root: str = "") -> str:
    """把相对路径锚定到项目根（不做 cwd 探测）。"""
    if not p:
        return p
    if os.path.isabs(p):
        return p
    return os.path.join(root or PROJECT_ROOT, p)


def resolve_path(p: str, root: str = "", is_dir: bool = False) -> str:
    """相对路径：优先 cwd，不存在则回退项目根。"""
    if not p:
        return resolve(p, root)
    if os.path.isabs(p):
        return p
    cwd_path = os.path.abspath(p)
    ok = os.path.isdir if is_dir else os.path.exists
    if ok(cwd_path):
        return cwd_path
    return resolve(p, root)


def ensure_importable(root: str = "") -> str:
    """把项目根加入 sys.path，保证 `py cli/server.py` 直接运行时能 import q2api。"""
    r = root or PROJECT_ROOT
    if r not in sys.path:
        sys.path.insert(0, r)
    return r
