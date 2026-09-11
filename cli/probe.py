"""probe.py — 对 auths/ 下所有 key 做一次连通性探活（1 个 token 的最小请求）。

用法：
    py cli/probe.py                      # 探活全部账号
    py cli/probe.py --json               # JSON 输出
    py cli/probe.py -upstream-base URL   # 指定上游（联调假上游时用）
"""
from __future__ import annotations

import argparse
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from q2api import projpath  # noqa: E402
from q2api import config as cfgmod  # noqa: E402
from q2api.auth import Auth  # noqa: E402
from q2api.netutil import ensure_no_proxy  # noqa: E402
from q2api.upstream import Client, ErrKind, kind_name  # noqa: E402


def main() -> int:
    ensure_no_proxy()
    ap = argparse.ArgumentParser(description="QClaw apiKey 探活")
    ap.add_argument("--dir", default="", help="凭证目录（默认 ./auths）")
    ap.add_argument("-config", default="config.json")
    ap.add_argument("-upstream-base", default="", help="覆盖上游 base url")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    base = args.upstream_base
    timeout = 120
    if not base:
        cfg_path = projpath.resolve_path(args.config)
        if os.path.exists(cfg_path):
            c = cfgmod.Config.load(cfg_path)
            base = c.Upstream.BaseURL
            timeout = c.Upstream.TimeoutSeconds

    auth_dir = projpath.resolve_path(args.dir or "./auths", is_dir=True)
    auths = Auth.load_dir(auth_dir)
    if not auths:
        print(f"⚠️  {auth_dir} 下没有 qclaw-*.json")
        return 1

    up = Client(base_url=base, timeout_seconds=timeout)
    rows = []
    for a in auths:
        err = up.probe(a)
        if err is None:
            rows.append({"uid": a.uid, "ok": True, "kind": "none", "detail": "",
                         "key": a.masked(), "nickname": a.nickname})
            continue
        k = getattr(err, "kind", None)
        rows.append({"uid": a.uid, "ok": False,
                     "kind": kind_name(k) if k is not None else "exception",
                     "detail": str(err)[:200], "key": a.masked(), "nickname": a.nickname})

    if args.json:
        print(json.dumps({"upstream": up.base, "results": rows}, ensure_ascii=False, indent=2))
    else:
        print(f"上游: {up.base}\n")
        print(f"{'uid':<14} {'key':<20} {'结果':<6} 分类 / 详情")
        print("-" * 78)
        for r in rows:
            flag = "✅ OK" if r["ok"] else "❌ 失败"
            detail = r["detail"] if not r["ok"] else "-"
            print(f"{r['uid']:<14} {r['key']:<20} {flag:<6} {r['kind']} {detail}")
        ok = sum(1 for r in rows if r["ok"])
        print(f"\n可用 {ok}/{len(rows)}")
    return 0 if all(r["ok"] for r in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
