"""server.py — 启动 qclaw2api 代理服务。

用法：
    py cli/server.py -config config.json

**路径约定**：所有相对路径（config.json / auths / data）都经 `q2api.projpath` 解析，
先按 cwd 找、找不到回退项目根，因此从任意目录启动都能正确加载账号。
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from q2api import config as cfgmod, projpath  # noqa: E402
from q2api import redisstore  # noqa: E402
from q2api.auth import Auth  # noqa: E402
from q2api.pool import Pool  # noqa: E402
from q2api.scheduler import Scheduler  # noqa: E402
from q2api.server import Config as ServerConfig, serve  # noqa: E402
from q2api.session import Config as SessionConfig, Router  # noqa: E402
from q2api.upstream import Client  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="qclaw2api 代理服务")
    ap.add_argument("-config", default="config.json", help="配置文件路径")
    ap.add_argument("-listen", default="", help="覆盖监听地址，如 :9000")
    ap.add_argument("-v", "--verbose", action="store_true", help="开启 debug 日志")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    cfg_path = projpath.resolve_path(args.config)
    c = cfgmod.Config.load(cfg_path)
    if args.listen:
        c.listen = args.listen

    auth_dir = projpath.resolve_path(c.auth_dir, is_dir=True)
    state_file = projpath.resolve_path(c.state_file)

    auths = Auth.load_dir(auth_dir)
    if not auths:
        print(f"⚠️  未在 {auth_dir} 下找到任何凭证（期望 qclaw-*.json）")
        print("   请先执行: py cli/key.py add <apiKey>")
        print("   或生成假凭证用于联调: py cli/key.py gen-fake 3")

    pool = Pool(state_file)
    pool.set_breaker(c.Pool.BreakerThreshold, c.BreakerCooldownDur, c.BreakerCooldownMaxD)
    pool.set_weights(c.Pool.IdleWeightPerHour, c.Pool.IdleWeightMax)
    pool.set_max_in_flight(c.Pool.MaxInFlight)

    store = redisstore.new(c.Upstash.URL, c.Upstash.Token)
    pool.set_store(store)
    pool.restore_from_snapshot()

    pool.sync_to_dir(auths)
    for a in auths:
        if a.disabled:
            pool.disable(a.uid, "credential disabled in file")

    session = None
    sticky_count = None
    if c.SessionSticky.Enabled:
        sess = SessionConfig(ttl=c.SessionTTL, gc_interval=c.SessionGCInterval,
                             store=store, available=pool.available_uids)
        session = Router(sess)
        session.load_from_store()
        session.start_gc()
        sticky_count = session.count

    up = Client(base_url=c.Upstream.BaseURL,
                timeout_seconds=c.Upstream.TimeoutSeconds,
                max_tokens_cap=c.Upstream.MaxTokensCap,
                auto_system_prompt=c.Upstream.AutoSystemPrompt,
                auto_system_text=c.Upstream.AutoSystemText,
                default_model=c.Upstream.DefaultModel)

    sched = Scheduler(pool, up, c.ProbeIntervalDur)
    sched.start()

    scfg = ServerConfig(pool=pool, upstream=up, api_key=c.api_key,
                        session=session, sticky_count=sticky_count,
                        redis_mode="upstash" if c.Upstash.URL else "noop",
                        soft_cooldown=c.SoftRateDur)
    httpd = serve(scfg, c.listen)

    total, healthy, cooling, disabled, _ = pool.counts_detailed()
    print(f"loaded {len(auths)} account(s): total={total} healthy={healthy} "
          f"cooling={cooling} disabled={disabled}")
    print(f"upstream={up.base}")
    print(f"listening on {c.listen}  (Ctrl+C 退出)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n正在退出…")
    finally:
        sched.stop()
        if session is not None:
            session.stop_gc()
        pool.flush()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
