"""scheduler.py — 周期性对账号池里的 key 做连通性探活。

相对 workbuddy2api 的差异：那边是「定时签到 + 保活」（OAuth 账号要每日签到领积分），
QClaw 的 apiKey 没有签到概念，所以这里改为**定期探活**：
用 1 个 token 的最小请求验证 key 是否还有效，失效的直接 disable，
避免客户端请求时才撞上死 key。
"""
from __future__ import annotations

import logging
import threading
from datetime import timedelta
from typing import Optional

from q2api.pool import Pool
from q2api.upstream import Client, ErrKind

LOG = logging.getLogger("q2api.scheduler")


class Scheduler:
    def __init__(self, pool: Pool, upstream: Client,
                 interval: timedelta = timedelta(minutes=30)) -> None:
        self.pool = pool
        self.upstream = upstream
        self.interval = interval if interval and interval > timedelta(0) else timedelta(minutes=30)
        self._stop: Optional[threading.Event] = None

    def start(self) -> None:
        if self._stop is not None:
            return
        self._stop = threading.Event()
        stop = self._stop

        def run():
            while not stop.wait(self.interval.total_seconds()):
                try:
                    self.probe_all()
                except Exception as e:  # noqa
                    LOG.warning("[scheduler] 探活异常: %s", e)

        threading.Thread(target=run, daemon=True).start()
        LOG.info("[scheduler] 已启动，探活间隔 %s", self.interval)

    def stop(self) -> None:
        if self._stop is not None:
            self._stop.set()
            self._stop = None

    def probe_all(self) -> dict:
        """对全部账号做一次探活，返回统计。"""
        uids = self.pool.available_uids()
        stats = {"checked": 0, "ok": 0, "disabled": 0, "cooled": 0}
        for uid in uids:
            acct = self.pool.auth_by_uid(uid)
            if acct is None:
                continue
            err = self.upstream.probe(acct)
            stats["checked"] += 1
            if err is None:
                stats["ok"] += 1
                continue
            kind = getattr(err, "kind", None)
            if kind == ErrKind.AUTH:
                self.pool.disable(uid, "probe: apiKey invalid")
                stats["disabled"] += 1
                LOG.warning("[scheduler] uid=%s apiKey 失效，已禁用", uid[:12])
            elif kind == ErrKind.HARD_CREDIT:
                self.pool.cooldown_until_tomorrow_4am(uid, "probe: quota exhausted")
                stats["cooled"] += 1
                LOG.warning("[scheduler] uid=%s 额度耗尽，冷却至次日 04:00", uid[:12])
            else:
                self.pool.note_error(uid)
                LOG.warning("[scheduler] uid=%s 探活失败: %s", uid[:12], err)
        return stats
