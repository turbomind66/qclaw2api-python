"""key.py — 管理 auths/ 下的 QClaw apiKey 凭证。

用法：
    py cli/key.py extract [--path ~/.qclaw/openclaw.json] [--no-verify]   # 从本机 QClaw 提取 token
    py cli/key.py add <apiKey> [--nickname 备注]   # 手动新增凭证
    py cli/key.py list                              # 列出所有凭证（key 脱敏）
    py cli/key.py remove <uid>                      # 删除凭证
    py cli/key.py gen-fake <N>                      # 生成 N 个假凭证（仅供联调冒烟）

**路径约定**：相对路径经 q2api.projpath 解析（先 cwd，再项目根）。
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from q2api import projpath  # noqa: E402
from q2api.auth import Auth  # noqa: E402
from q2api.netutil import ensure_no_proxy  # noqa: E402


def _auth_dir(custom: str = "") -> str:
    return projpath.resolve_path(custom or "./auths", is_dir=True)


def _candidate_qclaw_configs() -> list:
    """按优先级列出可能存放本地网关 token 的配置文件路径。"""
    home = os.path.expanduser("~")
    appdata = os.environ.get("APPDATA", "")
    cands = [
        os.path.join(home, ".qclaw", "openclaw.json"),
        os.path.join(home, ".openclaw", "openclaw.json"),
    ]
    if appdata:
        cands.append(os.path.join(appdata, "QClaw", "openclaw.json"))
        cands.append(os.path.join(appdata, "QClaw", "app-store.json"))
    return cands


def _extract_token_from_config(path: str):
    """从 openclaw.json 读取 gateway.auth.token；返回 (token, port) 或抛 ValueError。"""
    try:
        d = json.load(open(path, "r", encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise ValueError(f"读取配置失败: {e}")
    gw = d.get("gateway") if isinstance(d.get("gateway"), dict) else {}
    auth = gw.get("auth") if isinstance(gw.get("auth"), dict) else {}
    # 兼容部分版本把 token 直接放在 gateway.auth.token
    token = auth.get("token") or ""
    if not token:
        # 退一步：远程 QClaw 客户端可能把 apiKey 放在别处（此处不解密 app-store.json，
        # 因为它用的是 Electron safeStorage(DPAPI)，需专门工具；本命令只处理已明文可读的本地网关）
        raise ValueError("未在 gateway.auth.token 找到 token（本命令不支持解密加密的 app-store.json）")
    port = int(gw.get("port") or 62522)
    return token, port


def cmd_extract(args) -> int:
    ensure_no_proxy()
    path = args.path
    if not path:
        for c in _candidate_qclaw_configs():
            if os.path.exists(c):
                path = c
                break
    if not path or not os.path.exists(path):
        print("❌ 未找到本机 QClaw 配置（已尝试）:")
        for c in _candidate_qclaw_configs():
            print(f"   - {c}")
        print("\n提示：可用 --path 指定 openclaw.json 的绝对路径。")
        return 1

    try:
        token, port = _extract_token_from_config(path)
    except ValueError as e:
        print(f"❌ {e}")
        return 1

    d = _auth_dir(args.dir)
    os.makedirs(d, exist_ok=True)
    a = Auth()
    a.api_key = token
    # 稳定 uid：本地网关只有一个，重复 extract 覆盖同一文件而非新增
    a.uid = "local-qclaw-gateway"
    a.nickname = args.nickname or f"{socket.gethostname()} 本地QClaw网关"
    a.note = f"auto-extracted from {path}"
    a = Auth.parse(a.to_dict())
    fp = os.path.join(d, f"qclaw-{a.uid}.json")
    a.save_atomic(fp)
    print(f"✅ 已从 {path} 提取 token 并写入 {fp}")
    print(f"   uid={a.uid}  key={a.masked()}  gateway=127.0.0.1:{port}")

    if args.no_verify:
        print("\n（已跳过连通性验证，可用 py cli/probe.py 自行探活）")
        return 0

    # 验证：直连本地网关 /v1/models（绕过系统代理）
    import requests  # noqa: E402
    base = f"http://127.0.0.1:{port}/v1"
    try:
        r = requests.get(base + "/models",
                         headers={"Authorization": f"Bearer {token}",
                                   "Content-Type": "application/json"},
                         timeout=(5, 15))
    except requests.RequestException as e:
        print(f"\n⚠️  验证失败：无法连接本地网关 {base}（{e}）")
        print("   token 已写入，但网关可能未启动；启动 QClaw 后可用 py cli/probe.py 复查。")
        return 0
    if r.status_code == 200:
        try:
            ids = [m.get("id") for m in r.json().get("data", [])]
        except (ValueError, AttributeError):
            ids = []
        print(f"\n✅ 验证通过：本地网关返回 200，可用模型 {ids}")
    else:
        print(f"\n⚠️  验证返回 {r.status_code}：token 已写入但网关鉴权未通过，请检查 QClaw 是否登录。")
        print(f"   响应: {r.text[:200]}")
    return 0


def cmd_add(args) -> int:
    d = _auth_dir(args.dir)
    os.makedirs(d, exist_ok=True)
    a = Auth()
    a.api_key = args.apikey.strip()
    a.nickname = args.nickname or ""
    a.uid = args.uid or ""
    if not a.api_key:
        print("❌ apiKey 不能为空")
        return 1
    # 触发 uid 指纹计算
    if not a.uid:
        a.uid = ""
        a = Auth.parse(a.to_dict())
    fp = os.path.join(d, f"qclaw-{a.uid}.json")
    a.save_atomic(fp)
    print(f"✅ 已写入 {fp}")
    print(f"   uid={a.uid}  key={a.masked()}")
    return 0


def cmd_list(args) -> int:
    d = _auth_dir(args.dir)
    auths = Auth.load_dir(d)
    if not auths:
        print(f"(空) {d} 下没有 qclaw-*.json")
        return 0
    print(f"{'uid':<14} {'key':<20} {'备注':<20} 状态")
    print("-" * 70)
    for a in auths:
        st = "禁用" if a.disabled else "启用"
        print(f"{a.uid:<14} {a.masked():<20} {(a.nickname or a.note or '-'):<20} {st}")
    print(f"\n共 {len(auths)} 个凭证，目录：{d}")
    return 0


def cmd_remove(args) -> int:
    d = _auth_dir(args.dir)
    for a in Auth.load_dir(d):
        if a.uid == args.uid or a.uid.startswith(args.uid):
            os.remove(a.file_path)
            print(f"✅ 已删除 {a.file_path}")
            return 0
    print(f"❌ 未找到 uid={args.uid}")
    return 1


def cmd_gen_fake(args) -> int:
    """生成假凭证：key 里带 fake 标记，只用于对接 scripts/fake_upstream.py 做冒烟。"""
    d = _auth_dir(args.dir)
    os.makedirs(d, exist_ok=True)
    n = max(1, min(args.n, 20))
    made = []
    for i in range(1, n + 1):
        a = Auth()
        a.api_key = f"sk-fake-{i:03d}-{'0' * 16}"
        a.nickname = f"fake-{i}"
        a = Auth.parse(a.to_dict())
        fp = os.path.join(d, f"qclaw-{a.uid}.json")
        a.save_atomic(fp)
        made.append((a.uid, a.masked()))
    print(f"✅ 已在 {d} 生成 {len(made)} 个假凭证（仅供联调，真实请求会 401）：")
    for uid, m in made:
        print(f"   {uid}  {m}")
    print("\n提示：配合 scripts/fake_upstream.py 使用，用 -upstream-base 指向假上游。")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="QClaw apiKey 凭证管理")
    ap.add_argument("--dir", default="", help="凭证目录（默认 ./auths）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_ext = sub.add_parser("extract", help="从本机 QClaw 提取网关 token")
    p_ext.add_argument("--path", default="", help="openclaw.json 路径（默认自动探测 ~/.qclaw/openclaw.json）")
    p_ext.add_argument("--nickname", default="", help="凭证备注")
    p_ext.add_argument("--no-verify", action="store_true", help="提取后不验证连通性")
    p_ext.set_defaults(func=cmd_extract)

    p_add = sub.add_parser("add", help="新增凭证")
    p_add.add_argument("apikey")
    p_add.add_argument("--nickname", default="")
    p_add.add_argument("--uid", default="")
    p_add.set_defaults(func=cmd_add)

    p_list = sub.add_parser("list", help="列出凭证")
    p_list.set_defaults(func=cmd_list)

    p_rm = sub.add_parser("remove", help="删除凭证")
    p_rm.add_argument("uid")
    p_rm.set_defaults(func=cmd_remove)

    p_fake = sub.add_parser("gen-fake", help="生成假凭证（联调用）")
    p_fake.add_argument("n", type=int)
    p_fake.set_defaults(func=cmd_gen_fake)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
