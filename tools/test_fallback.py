#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_fallback.py — `/v1` 统一入口 + 实例账号不可用时的组内 cookie 兜底

两个真实缺陷（2026-10-21 公网实测）：
  ① acc01 浏览器号被封（20004）后，用实例 key 直连 /v1/images/generations → 502；
     同样请求走 /pool/v1 → 200。根因：/v1 走页内数据面，只能用实例自己那一个浏览器。
  ② 用户原话「为啥两个哇，应该 v1 就行」—— /v1 与 /pool/v1 两套地址容易配错。

统一后的语义（按 Bearer 值区分，用户只需一个地址）：
  Bearer = 实例 key   → 轮询全池（默认）
  Bearer = cookie 串  → 用这个 cookie 直连（诊断/临时用，不轮询）
  X-YB-Direct: 1      → 强制直连（内部转发用，防 /v1 ⇄ /pool/v1 递归）
  池内无可用目标       → 回落直连（保证单实例裸部署仍可用，不会突然全 503）

覆盖：
  F1 _self_usable 语义
  F2 _fallback_cookie 决策
  F3 _want_pool_route 语义矩阵（实例key/cookie/内部头/无目标）
  F4 轮询目标包含组内 cookie 账号
  F5 源码静态断言（分流位置 + 两处兜底 + 内部标记）
"""
import os
import shutil
import sys
import tempfile
import importlib.util

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

spec = importlib.util.spec_from_file_location("ybp", os.path.join(ROOT, "yuanbao_openai_proxy.py"))
ybp = importlib.util.module_from_spec(spec)
sys.modules["ybp"] = ybp
spec.loader.exec_module(ybp)

import accounts as _acc          # noqa: E402
import pool_state                # noqa: E402

FAILS = []
CK_A = "hy_source=web; hy_user=aaaa1111; hy_token=tkA"
CK_B = "hy_source=web; hy_user=bbbb2222; hy_token=tkB"


def check(name, cond, detail=""):
    print("  %s %s%s" % ("PASS" if cond else "FAIL", name, ("  " + str(detail)) if detail != "" else ""))
    if not cond:
        FAILS.append(name)


class FakeReq:
    """只实现 _check_auth / _want_pool_route 用到的那点接口。"""

    def __init__(self, headers=None, qs=None):
        self.headers = headers or {}
        self.query_params = qs or {}


def make_env(tmp, state="ejected", with_healthy=True):
    acc = pool_state.AccountState(os.path.join(tmp, "acc.json"), "accX", 3, 1, 0)
    if state == "ejected":
        acc.apply("eject", "20004 用户被封号", by="auto")
    elif state == "healthy":
        acc.record_health(True, 200, "alive", False)
    elif state == "quarantined":
        acc.apply("quarantine", "23000 登录已过期", by="auto")
    ca = _acc.CookieAccounts(os.path.join(tmp, "accounts"))
    if with_healthy:
        ca.add("ybA", CK_A)
        ca.record_health("ybA", True, 200, "alive", False)
        ca.add("ybB", CK_B)
        ca.record_health("ybB", True, 200, "alive", False)
    return acc, ca


def f1():
    print("[F1] _self_usable 语义")
    tmp = tempfile.mkdtemp()
    real = ybp.ACCOUNT
    try:
        ybp.ACCOUNT = None
        check("F1 无状态机 ⇒ 视为可用（不改老行为）", ybp._self_usable() is True)

        acc, _ = make_env(tmp, "ejected")
        ybp.ACCOUNT = acc
        check("F1 ejected ⇒ 不可用", ybp._self_usable() is False, acc.get().get("state"))

        acc2 = pool_state.AccountState(os.path.join(tmp, "a2.json"), "accY", 3, 1, 0)
        acc2.record_health(True, 200, "alive", False)
        ybp.ACCOUNT = acc2
        check("F1 enabled+保活成功 ⇒ 可用", ybp._self_usable() is True, acc2.get().get("state"))

        acc3 = pool_state.AccountState(os.path.join(tmp, "a3.json"), "accZ", 3, 1, 0)
        ybp.ACCOUNT = acc3
        check("F1 enabled 但从未保活 ⇒ 不可用（与路由门禁一致）", ybp._self_usable() is False,
              acc3.get().get("last_health"))
    finally:
        ybp.ACCOUNT = real
        shutil.rmtree(tmp, ignore_errors=True)


def f2():
    print("[F2] _fallback_cookie 决策")
    tmp = tempfile.mkdtemp()
    real_a, real_c = ybp.ACCOUNT, ybp.COOKIE_ACCOUNTS
    try:
        acc, ca = make_env(tmp, "healthy")
        ybp.ACCOUNT, ybp.COOKIE_ACCOUNTS = acc, ca
        check("F2 实例健康 ⇒ 不兜底（保持页面模式）", ybp._fallback_cookie() == "")

        shutil.rmtree(tmp, ignore_errors=True)
        os.makedirs(tmp, exist_ok=True)
        acc, ca = make_env(tmp, "ejected")
        ybp.ACCOUNT, ybp.COOKIE_ACCOUNTS = acc, ca
        got = ybp._fallback_cookie()
        check("F2 实例被封 ⇒ 返回组内 cookie", got in (CK_A, CK_B), got[:40])

        ca.record_health("ybB", True, 200, "alive", False)
        check("F2 取最近健康的号", ybp._fallback_cookie() == CK_B, ybp._fallback_cookie()[:40])

        ca.record_health("ybA", False, 401, "unauthorized", False)
        ca.record_health("ybB", False, 401, "unauthorized", False)
        check("F2 组内也没健康号 ⇒ 返回空", ybp._fallback_cookie() == "")

        ybp.COOKIE_ACCOUNTS = None
        check("F2 无账号库 ⇒ 返回空", ybp._fallback_cookie() == "")
    finally:
        ybp.ACCOUNT, ybp.COOKIE_ACCOUNTS = real_a, real_c
        shutil.rmtree(tmp, ignore_errors=True)


def f3():
    print("[F3] _want_pool_route 语义矩阵")
    tmp = tempfile.mkdtemp()
    real = (ybp.ACCOUNT, ybp.COOKIE_ACCOUNTS, ybp.YUANBAO_API_KEY, ybp.YB_ROUTER,
            ybp._fleet_peers_all)
    try:
        acc, ca = make_env(tmp, "ejected")
        ybp.ACCOUNT, ybp.COOKIE_ACCOUNTS = acc, ca
        ybp.YUANBAO_API_KEY = "sk-test"
        ybp.YB_ROUTER = True
        ybp._fleet_peers_all = lambda: [{"id": "self", "name": "accX", "self": True,
                                         "url": "http://127.0.0.1:%d" % ybp.YB_SELF_PORT,
                                         "key": "sk-test"}]

        # ① 实例 key → 轮询（池内有 ybA/ybB 可用）
        rq = FakeReq(headers={"authorization": "Bearer sk-test"})
        check("F3 实例 key ⇒ 轮询", ybp._want_pool_route(rq) is True)

        # ② cookie 透传 → 直连（用调用方自己的号）
        rq = FakeReq(headers={"authorization": "Bearer " + CK_A})
        check("F3 cookie 串 ⇒ 直连（不抢池里的号）", ybp._want_pool_route(rq) is False)

        # ③ X-YB-Direct → 直连（内部转发，防递归）
        rq = FakeReq(headers={"authorization": "Bearer sk-test", "x-yb-direct": "1"})
        check("F3 X-YB-Direct ⇒ 直连（防递归）", ybp._want_pool_route(rq) is False)

        # ④ 鉴权错 → 交给原逻辑返回 401（不在这里轮询）
        rq = FakeReq(headers={"authorization": "Bearer wrong-key"})
        check("F3 错 key ⇒ 不轮询（让原逻辑回 401）", ybp._want_pool_route(rq) is False)

        # ⑤ 池内无可用目标 → 回落直连（单实例裸部署不会突然全 503）
        ca.record_health("ybA", False, 401, "unauthorized", False)
        ca.record_health("ybB", False, 401, "unauthorized", False)
        rq = FakeReq(headers={"authorization": "Bearer sk-test"})
        check("F3 池内无目标 ⇒ 回落直连", ybp._want_pool_route(rq) is False)

        # ⑥ YB_ROUTER=0 → 不轮询（保留关闭开关）
        ybp.YB_ROUTER = False
        rq = FakeReq(headers={"authorization": "Bearer sk-test"})
        check("F3 YB_ROUTER=0 ⇒ 不轮询", ybp._want_pool_route(rq) is False)
    finally:
        (ybp.ACCOUNT, ybp.COOKIE_ACCOUNTS, ybp.YUANBAO_API_KEY, ybp.YB_ROUTER,
         ybp._fleet_peers_all) = real
        shutil.rmtree(tmp, ignore_errors=True)


def f4():
    print("[F4] 轮询目标包含组内 cookie 账号")
    tmp = tempfile.mkdtemp()
    real = (ybp.ACCOUNT, ybp.COOKIE_ACCOUNTS, ybp._fleet_peers_all)
    try:
        acc, ca = make_env(tmp, "ejected")
        ybp.ACCOUNT, ybp.COOKIE_ACCOUNTS = acc, ca
        ybp._fleet_peers_all = lambda: [{"id": "self", "name": "accX", "self": True,
                                         "url": "http://127.0.0.1:1", "key": "sk-test"}]
        ts = ybp._router_targets()
        names = [t.get("name") for t in ts]
        check("F4 包含 ybA / ybB", set(["ybA", "ybB"]) <= set(names), str(names))
        check("F4 不含已剔除的实例账号", "accX" not in names, str(names))
        ck = [t for t in ts if t.get("name") == "ybA"]
        check("F4 cookie 账号 key 就是 cookie 串",
              ck and ck[0].get("key", "").startswith("hy_source=web"), (ck[0].get("key") or "")[:30] if ck else "")
        check("F4 cookie 账号 url 指回本机", ck and "127.0.0.1" in ck[0].get("url", ""),
              ck[0].get("url") if ck else "")
    finally:
        ybp.ACCOUNT, ybp.COOKIE_ACCOUNTS, ybp._fleet_peers_all = real
        shutil.rmtree(tmp, ignore_errors=True)


def f5():
    print("[F5] 源码静态断言")
    src = open(os.path.join(ROOT, "yuanbao_openai_proxy.py"), encoding="utf-8").read()
    lines = src.split("\n")
    i_chat = next((i for i, l in enumerate(lines, 1) if l.startswith("async def chat_completions")), None)
    i_img = next((i for i, l in enumerate(lines, 1) if l.startswith("async def images_generations")), None)
    check("F5 找到两个端点", i_chat and i_img, "%s/%s" % (i_chat, i_img))

    # 分流必须在函数最前（在 _check_auth 之前，避免重复鉴权开销无所谓，但必须在业务之前）
    for nm, i, rest in (("chat", i_chat, "chat/completions"), ("images", i_img, "images/generations")):
        seg = "\n".join(lines[i - 1:i + 6])
        check("F5 %s 端点在开头分流" % nm, "_want_pool_route(req)" in seg, seg[:90].replace("\n", " "))
        check("F5 %s 分流到 pool_router" % nm, 'pool_router(req, "%s")' % rest in seg)

    # 两处 cookie 兜底仍在（直连路径的最后一层保险）
    hits = [i for i, l in enumerate(lines, 1) if "cookie_str = _fallback_cookie()" in l]
    check("F5 两处 cookie 兜底", len(hits) == 2, str(hits))

    # pool_router 必须打内部标记，否则 /v1 ⇄ /pool/v1 递归
    check("F5 pool_router 打 X-YB-Direct", '"X-YB-Direct": "1"' in src)


if __name__ == "__main__":
    f1(); f2(); f3(); f4(); f5()
    print()
    if FAILS:
        print("FAILED %d: %s" % (len(FAILS), FAILS))
        sys.exit(1)
    print("全部通过")
