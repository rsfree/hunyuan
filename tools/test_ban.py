#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_ban.py — 「封号」识别与自动剔除自检（离线）。

覆盖：
  B1 _note_ban 命中/不命中（20004 + 封号 / 无关文本）
  B2 pool_state：冻结优先于"新号不计失败"（never_ok 的号也要被隔离）
  B3 流式 error 里的 20004 → 剔除 + 不重试
  B4 页内异常文本（同真实抓到的报错）→ 剔除
用法：/Users/betterme/.workbuddy/binaries/python/envs/default/bin/python tools/test_ban.py
"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

spec = importlib.util.spec_from_file_location("ybp", os.path.join(ROOT, "yuanbao_openai_proxy.py"))
ybp = importlib.util.module_from_spec(spec)
sys.modules["ybp"] = ybp
spec.loader.exec_module(ybp)

import pool_state  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print("  %s %s%s" % ("PASS" if cond else "FAIL", name, ("  " + detail) if detail else ""))
    if not cond:
        FAILS.append(name)


REAL_PAGE_ERR = ('页内执行失败: {"exceptionId": 4, "text": "Uncaught (in promise) Error: '
                 '创建会话失败: {\\"error\\":{\\"code\\":\\"20004\\",\\"message\\":\\"用户被封号\\"}}"}')


def b1():
    print("[B1] _note_ban 命中判定")
    tmp = tempfile.mkdtemp()
    try:
        acc = pool_state.AccountState(os.path.join(tmp, "a.json"), "accX", 3, 1, 0)
        ybp.ACCOUNT = acc
        check("B1 无关文本不误判", ybp._note_ban("HTTP 401 凭证失效") is False)
        check("B1 无关后状态未变", acc.get()["state"] == "enabled", acc.get()["state"])
        check("B1 真实页内报错命中", ybp._note_ban(REAL_PAGE_ERR) is True)
        st = acc.get()
        check("B1 命中后自动剔除", st["state"] == "ejected", "%s / %s" % (st["state"], st.get("reason")))
        check("B1 记了原因", "20004" in (st.get("reason") or ""), st.get("reason"))
        check("B1 changed_by=auto", st.get("changed_by") == "auto", st.get("changed_by"))
        check("B1 剔除后不可轮询", acc.get().get("state") == "ejected")
        # 幂等：再打一次还是 ejected
        ybp._note_ban("20004 用户被封号")
        check("B1 重复触发幂等", acc.get()["state"] == "ejected")
    finally:
        ybp.ACCOUNT = None
        shutil.rmtree(tmp, ignore_errors=True)


def b2():
    print("[B2] 冻结优先于「新号不计失败」")
    tmp = tempfile.mkdtemp()
    try:
        acc = pool_state.AccountState(os.path.join(tmp, "b.json"), "accNew", 3, 1, 0)
        check("B2 初始从未成功", acc.get().get("ever_ok") in (False, None))
        d, act = acc.record_health(False, None, "页内显示：账号已冻结", True, count_failure=False)
        check("B2 冻结⇒立即隔离", d["state"] == "quarantined", "%s / %s" % (d["state"], d.get("reason")))
        check("B2 返回动作标记", act == "quarantine:frozen", str(act))
        check("B2 不可轮询", d["routable"] is False)
        check("B2 last_health.frozen", (d.get("last_health") or {}).get("frozen") is True)

        # 非冻结 + 未登录（配置态）→ 不应被隔离
        acc2 = pool_state.AccountState(os.path.join(tmp, "c.json"), "accNew2", 3, 1, 0)
        d2, act2 = acc2.record_health(False, None, "缺少 hy_user（未登录）", False, count_failure=False)
        check("B2 未登录不隔离（配置态）", d2["state"] == "enabled" and act2 is None,
              "%s / %s" % (d2["state"], act2))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def b3():
    print("[B3] 流式 error 里的封号 ⇒ 剔除 + 不重试")
    tmp = tempfile.mkdtemp()
    real = {k: getattr(ybp, k) for k in ("_yb_stream_open", "_yb_post_json", "get_sig", "pace")}
    calls = []

    def fake_post(cookie, path, body, sig, agent_id, conv="", extra=None, timeout=300):
        if "conversation/create" in path:
            return {"status": 200, "text": json.dumps({"id": "c1"})}
        return {"status": 200, "text": "{}"}

    def fake_open(cookie, path, body, sig, agent_id, conv="", extra=None, timeout=300):
        calls.append(path)
        return ybp._RawResp(200, {}, lines=iter(
            ['data: {"type":"error","msg":"20004 用户被封号"}'.encode("utf-8")]))

    try:
        acc = pool_state.AccountState(os.path.join(tmp, "d.json"), "accS", 3, 1, 0)
        ybp.ACCOUNT = acc
        ybp._yb_post_json = fake_post
        ybp._yb_stream_open = fake_open
        ybp.get_sig = lambda tab=None, force=False: {"uskey": "u", "md5": "m", "ts": "1"}
        ybp.pace = lambda k: None
        blob = "".join(ybp._live_chat_sse(None, "ck", "naQivTmsDa", "hi", "hunyuan", None,
                                          "chatcmpl-b", 1, "hunyuan"))
        check("B3 只打一次（不重试）", len(calls) == 1, "calls=%s" % calls)
        check("B3 账号被剔除", acc.get()["state"] == "ejected", acc.get()["state"])
        check("B3 错误透传给客户端", "封号" in blob, blob[:120])
    finally:
        for k, v in real.items():
            setattr(ybp, k, v)
        ybp.ACCOUNT = None
        shutil.rmtree(tmp, ignore_errors=True)


def b4():
    print("[B4] 页内异常文本（与线上实测一致）")
    tmp = tempfile.mkdtemp()
    try:
        acc = pool_state.AccountState(os.path.join(tmp, "e.json"), "accP", 3, 1, 0)
        ybp.ACCOUNT = acc
        hit = ybp._note_ban(Exception(REAL_PAGE_ERR))
        check("B4 捕获异常对象也能识别", hit is True)
        check("B4 状态=ejected", acc.get()["state"] == "ejected", acc.get()["state"])
    finally:
        ybp.ACCOUNT = None
        shutil.rmtree(tmp, ignore_errors=True)


def b5():
    print("[B5] 加号前封号探针 _cookie_ban_probe")
    real = {k: getattr(ybp, k) for k in ("_yb_post_json", "get_sig")}
    seen = []

    def post_ban(cookie, path, body, sig, agent_id, conv="", extra=None, timeout=300):
        seen.append(path)
        return {"status": 200, "text": '{"error":{"code":"20004","message":"用户被封号"}}'}

    def post_ok(cookie, path, body, sig, agent_id, conv="", extra=None, timeout=300):
        seen.append(path)
        if "conversation/create" in path:
            return {"status": 200, "text": '{"id":"c-probe"}'}
        return {"status": 200, "text": "{}"}

    try:
        ybp.get_sig = lambda tab=None, force=False: {"uskey": "u", "md5": "m", "ts": "1"}
        ybp._yb_post_json = post_ban
        r = ybp._cookie_ban_probe("hy_user=x; hy_token=y")
        check("B5 封号号判 banned", r.get("banned") is True, json.dumps(r, ensure_ascii=False))
        check("B5 封号号不误判 ok", r.get("ok") is False)

        ybp._yb_post_json = post_ok
        seen[:] = []
        r = ybp._cookie_ban_probe("hy_user=x; hy_token=y")
        check("B5 正常号判 ok", r.get("ok") is True, json.dumps(r, ensure_ascii=False))
        check("B5 探针会话用完即删", any("conversation/v1/delete" in x for x in seen), "%s" % seen)

        # 探活 200 但建会话封号 —— 正是我们要防的假象
        def post_alive_but_banned(cookie, path, body, sig, agent_id, conv="", extra=None, timeout=300):
            if "conversation/create" in path:
                return {"status": 200, "text": '{"error":{"code":"20004","message":"用户被封号"}}'}
            return {"status": 200, "text": "{}"}
        ybp._yb_post_json = post_alive_but_banned
        r = ybp._cookie_ban_probe("hy_user=x; hy_token=y")
        check("B5 '探活 alive 但实为封号' 能识别", r.get("banned") is True, json.dumps(r, ensure_ascii=False))
    finally:
        for k, v in real.items():
            setattr(ybp, k, v)


def b6():
    print("[B6] 23000 登录已过期 ⇒ 隔离（不是剔除）")
    tmp = tempfile.mkdtemp()
    try:
        acc = pool_state.AccountState(os.path.join(tmp, "f.json"), "accE", 3, 1, 0)
        ybp.ACCOUNT = acc
        check("B6 无关文本不误判", ybp._note_expired("20004 用户被封号") is False)
        check("B6 20004 不算过期", acc.get()["state"] == "enabled", acc.get()["state"])
        txt = ('创建会话失败: {"error":{"code":"23000","message":"登录已过期，请重新登录"}}')
        check("B6 23000 命中", ybp._note_expired(txt) is True)
        st = acc.get()
        check("B6 过期⇒隔离（不是剔除）", st["state"] == "quarantined",
              "%s / %s" % (st["state"], st.get("reason")))
        check("B6 原因可读", "重新登录" in (st.get("reason") or ""), st.get("reason"))
        # 反向：过期文本不能被 _note_ban 当成封号
        acc2 = pool_state.AccountState(os.path.join(tmp, "g.json"), "accE2", 3, 1, 0)
        ybp.ACCOUNT = acc2
        check("B6 过期不触发剔除", ybp._note_ban(txt) is False)
        check("B6 账号仍 enabled", acc2.get()["state"] == "enabled", acc2.get()["state"])
    finally:
        ybp.ACCOUNT = None
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    b1(); b2(); b3(); b4(); b5(); b6()
    print("\n%s  %d 项失败" % ("❌" if FAILS else "✅ 全部通过", len(FAILS)))
    for f in FAILS:
        print("   -", f)
    sys.exit(1 if FAILS else 0)
