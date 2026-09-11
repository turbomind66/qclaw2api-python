"""smoke.py — 无 key 环境下的端到端冒烟。

用 scripts/fake_upstream.py 假上游（复刻了 QClaw 的真实约束）驱动，全流程不需要真实 apiKey：

  A 段 单元级：账号池错误策略（死 key → 禁用 / 额度耗尽 → 冷却 / 正常 → 计数）
  B 段 集成级：HTTP 全链路（healthz / models / 非流式 / 流式 SSE / 自动补 system /
              max_tokens 钳制 / 模型名归一化 / 鉴权 / 换号 / 粘性会话 / 全死号兜底）

用法：
    py scripts/smoke.py
退出码 0 表示全部通过。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
from datetime import timedelta

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import requests  # noqa: E402

from q2api.auth import Auth  # noqa: E402
from q2api.netutil import ensure_no_proxy  # noqa: E402
from q2api.pool import Pool  # noqa: E402
from q2api.server import Config as ServerConfig, serve  # noqa: E402
from q2api.session import Config as SessionConfig, Router  # noqa: E402
from q2api.upstream import Client, ErrKind, classify  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((name, ok, detail))
    print(f"  {'✅' if ok else '❌'} {name}" + (f"  — {detail}" if detail else ""))
    return ok


def _write_key(d: str, key: str, nickname: str = "") -> Auth:
    a = Auth()
    a.api_key = key
    a.nickname = nickname
    a = Auth.parse(a.to_dict())
    a.save_atomic(os.path.join(d, f"qclaw-{a.uid}.json"))
    return a


def start_proxy(auth_dir: str, data_dir: str, upstream_base: str) -> tuple[str, object, Pool]:
    """启动一个代理实例，返回 (base_url, httpd, pool)。"""
    auths = Auth.load_dir(auth_dir)
    pool = Pool(os.path.join(data_dir, "state.json"))
    pool.set_breaker(3, timedelta(minutes=30), timedelta(hours=6))
    pool.set_max_in_flight(3)
    pool.sync_to_dir(auths)

    sess = SessionConfig(ttl=timedelta(minutes=30), gc_interval=timedelta(minutes=5),
                         available=pool.available_uids)
    router = Router(sess)
    router.start_gc()

    up = Client(base_url=upstream_base, timeout_seconds=15,
                default_model="openclaw", max_tokens_cap=8192)
    cfg = ServerConfig(pool=pool, upstream=up, api_key="smoke-token",
                       session=router, sticky_count=router.count,
                       soft_cooldown=timedelta(seconds=2))

    from q2api.server import _split_listen
    host, port = _split_listen(":0")
    httpd = serve(cfg, f"{host}:{port}")
    real_port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{real_port}/v1", httpd, pool


# ---------------------------------------------------------------------------
# A 段：账号池错误策略
# ---------------------------------------------------------------------------
def section_a(up_base: str) -> None:
    print("\n[A] 账号池错误策略")
    tmp = tempfile.mkdtemp(prefix="q2a-smoke-a-")
    try:
        pool = Pool(os.path.join(tmp, "state.json"))
        pool.set_breaker(3, timedelta(minutes=30), timedelta(hours=6))
        up = Client(base_url=up_base, timeout_seconds=15, default_model="openclaw")

        ok_acct = _write_key(tmp, "sk-fake-ok-00000000000000000", "ok")
        dead = _write_key(tmp, "sk-fake-dead-000000000000000", "dead")
        quota = _write_key(tmp, "sk-fake-quota-0000000000000", "quota")
        pool.sync_to_dir([ok_acct, dead, quota])

        body = json.dumps({"model": "auto", "messages": [{"role": "user", "content": "hi"}],
                           "max_tokens": 16}).encode()

        # 正常 key
        resp, status, _, err = up.chat(ok_acct, body)
        check("正常 key 返回 200", err is None and status == 200,
              f"status={status} err={err}")
        if resp is not None:
            resp.close()

        # 死 key → AUTH → disable
        resp, status, _, err = up.chat(dead, body)
        kind = classify(status, resp.text if resp is not None else "")
        if resp is not None:
            resp.close()
        check("死 key 被分类为 AUTH", kind == ErrKind.AUTH, f"status={status} kind={kind}")
        pool.disable(dead.uid, "apiKey invalid")
        st, _ = pool.status(dead.uid)
        check("死 key 被禁用", bool(st and st["disabled"]))

        # 额度 key → HARD_CREDIT → 冷却至次日 04:00
        resp, status, _, err = up.chat(quota, body)
        kind = classify(status, resp.text if resp is not None else "")
        if resp is not None:
            resp.close()
        check("额度 key 被分类为 HARD_CREDIT", kind == ErrKind.HARD_CREDIT,
              f"status={status} kind={kind}")
        pool.cooldown_until_tomorrow_4am(quota.uid, "quota exhausted")
        st, _ = pool.status(quota.uid)
        check("额度 key 进入长冷却", bool(st and st["cooling"]),
              f"remaining={st.get('cool_remaining_sec') if st else '?'}s")

        healthy = pool.available_uids()
        check("仅正常 key 仍可选", healthy == [ok_acct.uid], f"available={healthy}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# B 段：HTTP 全链路
# ---------------------------------------------------------------------------
def section_b(up_base: str) -> None:
    print("\n[B] HTTP 全链路")
    tmp = tempfile.mkdtemp(prefix="q2a-smoke-b-")
    httpd = None
    try:
        auth_dir = os.path.join(tmp, "auths")
        data_dir = os.path.join(tmp, "data")
        os.makedirs(auth_dir, exist_ok=True)
        os.makedirs(data_dir, exist_ok=True)
        _write_key(auth_dir, "sk-fake-ok-00000000000000000", "ok-1")
        _write_key(auth_dir, "sk-fake-ok-00000000000000001", "ok-2")

        base, httpd, pool = start_proxy(auth_dir, data_dir, up_base)
        H = {"Authorization": "Bearer smoke-token", "Content-Type": "application/json"}

        # T1 healthz
        r = requests.get(base.replace("/v1", "/healthz"), timeout=10)
        check("GET /healthz → 200", r.status_code == 200, f"body={r.text[:80]}")
        check("healthz 报告 2 个健康账号", r.json().get("healthy") == 2,
              f"body={r.text[:80]}")

        # T2 models
        r = requests.get(base + "/models", headers=H, timeout=10)
        check("GET /v1/models → 200", r.status_code == 200)
        ids = [m["id"] for m in r.json().get("data", [])]
        check("models 返回非空列表", len(ids) > 0, f"ids={ids[:4]}")

        # T3 非流式 + 自动补 system（客户端只发 user）
        requests.get(up_base.replace("/v1", "/_reset"), timeout=5)
        r = requests.post(base + "/chat/completions", headers=H, timeout=20,
                          json={"model": "auto", "messages": [{"role": "user", "content": "你好"}]})
        check("非流式 chat → 200", r.status_code == 200, f"status={r.status_code} body={r.text[:200]}")
        last = requests.get(up_base.replace("/v1", "/_last"), timeout=5).json().get("last") or {}
        roles = [m.get("role") for m in (last.get("messages") or [])]
        check("自动补了 system 消息", roles[:1] == ["system"], f"roles={roles}")

        # T4 流式 SSE
        r = requests.post(base + "/chat/completions", headers=H, timeout=20, stream=True,
                          json={"model": "auto", "messages": [{"role": "user", "content": "你好"}],
                                "stream": True})
        txt = "".join(chunk.decode("utf-8", "replace")
                      for chunk in r.iter_content(chunk_size=1024))
        check("流式 chat → 200", r.status_code == 200)
        check("流式收到 SSE 数据帧", txt.count("data: ") >= 2)
        check("流式以 [DONE] 结束", "data: [DONE]" in txt)

        # T5 max_tokens 钳制
        requests.get(up_base.replace("/v1", "/_reset"), timeout=5)
        r = requests.post(base + "/chat/completions", headers=H, timeout=20,
                          json={"model": "auto", "messages": [{"role": "user", "content": "hi"}],
                                "max_tokens": 99999})
        check("超上限 max_tokens 仍 200（已钳制）", r.status_code == 200,
              f"status={r.status_code} body={r.text[:160]}")
        last = requests.get(up_base.replace("/v1", "/_last"), timeout=5).json().get("last") or {}
        check("max_tokens 被钳制到 8192", last.get("max_tokens") == 8192,
              f"max_tokens={last.get('max_tokens')}")

        # T6 模型名归一化
        # 6a: auto 别名 → openclaw（本地网关默认模型）
        requests.get(up_base.replace("/v1", "/_reset"), timeout=5)
        requests.post(base + "/chat/completions", headers=H, timeout=20,
                      json={"model": "auto", "messages": [{"role": "user", "content": "hi"}]})
        last = requests.get(up_base.replace("/v1", "/_last"), timeout=5).json().get("last") or {}
        check("auto 别名映射到默认模型 openclaw", last.get("model") == "openclaw",
              f"model={last.get('model')}")

        # 6b: 不在别名表里的模型名原样透传（用 custom-llm-2024 这种自定义名）
        requests.get(up_base.replace("/v1", "/_reset"), timeout=5)
        requests.post(base + "/chat/completions", headers=H, timeout=20,
                      json={"model": "custom-llm-2024", "messages": [{"role": "user", "content": "hi"}]})
        last = requests.get(up_base.replace("/v1", "/_last"), timeout=5).json().get("last") or {}
        check("未知模型名原样透传", last.get("model") == "custom-llm-2024",
              f"model={last.get('model')}")

        # T7 鉴权
        r = requests.post(base + "/chat/completions",
                          headers={"Authorization": "Bearer wrong", "Content-Type": "application/json"},
                          timeout=10, json={"model": "auto", "messages": [{"role": "user", "content": "hi"}]})
        check("错误 api_key → 401", r.status_code == 401, f"status={r.status_code}")

        # T8 粘性会话
        uid_set = set()
        for _ in range(3):
            rr = requests.post(base + "/chat/completions", headers=H, timeout=20,
                               json={"model": "auto", "messages": [{"role": "user", "content": "hi"}],
                                     "metadata": {"conversation_id": "smoke-conv-1"}})
            if rr.status_code == 200:
                uid_set.add(rr.headers.get("X-Q2A-Uid", ""))
        check("粘性会话固定到同一账号", len(uid_set) == 1, f"uids={uid_set}")

        # T9 换号：注入一个死 key，请求应自动绕开并把它禁用
        _write_key(auth_dir, "sk-fake-dead-000000000000000", "dead")
        pool.sync_to_dir(Auth.load_dir(auth_dir))
        ok_cnt = 0
        for _ in range(6):
            rr = requests.post(base + "/chat/completions", headers=H, timeout=20,
                               json={"model": "auto", "messages": [{"role": "user", "content": "hi"}]})
            if rr.status_code == 200:
                ok_cnt += 1
        check("存在死 key 时仍能成功应答", ok_cnt >= 5, f"成功 {ok_cnt}/6")
        st = {a["uid"]: a for a in pool.list()}
        dead_disabled = any(a.get("disabled") for a in st.values()
                            if a.get("nickname") == "dead")
        check("死 key 被自动禁用", dead_disabled,
              f"状态={[(a['nickname'], a['disabled'], a['err_total']) for a in st.values()]}")
    finally:
        if httpd is not None:
            try:
                httpd.shutdown()
                httpd.server_close()
            except Exception:
                pass
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# C 段：全死号兜底
# ---------------------------------------------------------------------------
def section_c(up_base: str) -> None:
    print("\n[C] 全部 key 失效时的兜底")
    tmp = tempfile.mkdtemp(prefix="q2a-smoke-c-")
    httpd = None
    try:
        auth_dir = os.path.join(tmp, "auths")
        data_dir = os.path.join(tmp, "data")
        os.makedirs(auth_dir, exist_ok=True)
        os.makedirs(data_dir, exist_ok=True)
        _write_key(auth_dir, "sk-fake-dead-000000000000000", "dead")
        base, httpd, pool = start_proxy(auth_dir, data_dir, up_base)
        H = {"Authorization": "Bearer smoke-token", "Content-Type": "application/json"}
        r = requests.post(base + "/chat/completions", headers=H, timeout=20,
                          json={"model": "auto", "messages": [{"role": "user", "content": "hi"}]})
        check("全死号 → 透传 401 而非 500", r.status_code == 401,
              f"status={r.status_code} body={r.text[:160]}")
        codes = [a.get("disabled") for a in pool.list()]
        check("该 key 被标记为禁用", all(codes), f"disabled={codes}")
        hz = requests.get(base.replace("/v1", "/healthz"), timeout=10)
        check("healthz 转为 503", hz.status_code == 503, f"status={hz.status_code}")
    finally:
        if httpd is not None:
            try:
                httpd.shutdown()
                httpd.server_close()
            except Exception:
                pass
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    sys.path.insert(0, _HERE)
    from fake_upstream import start as start_fake

    # 本机回环不走系统代理，否则假故障会淹没真实断言（详见 q2api/netutil.py）
    ensure_no_proxy()

    up_base, fake_httpd = start_fake()
    print(f"假上游已启动: {up_base}")
    try:
        section_a(up_base)
        section_b(up_base)
        section_c(up_base)
    finally:
        try:
            fake_httpd.shutdown()
            fake_httpd.server_close()
        except Exception:
            pass

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print(f"\n{'=' * 60}")
    print(f"冒烟结果: {passed}/{total} 通过")
    failed = [n for n, ok, _ in RESULTS if not ok]
    if failed:
        print("失败项:")
        for n in failed:
            print(f"  - {n}")
    print("=" * 60)
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
