#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_bulk.py — cookie 批量导入流水线（端点级，离线）

用 FastAPI TestClient 真打 `/admin/group/bulk`，只把两个下游依赖 mock 掉：
  _cookie_ban_probe（建会话验真）与 _wm_ensure_cookie（补无水印）。

覆盖：
  U1 _clean_cookie 各种粘贴形态（curl -b / 引号 / Cookie: 前缀）
  U2 _bulk_name 前缀 + 唯一性
  U3 端点：新增 / 同 hy_user 替换 / 封号拒收 / 过期拒收 / 缺 hy_user 拒收 / 同批去重
  U4 dry_run 只验不写
  U5 探测期间 _note_ban/_note_expired 被抑制（不误伤实例账号）
"""
import importlib.util
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

import accounts as _acc          # noqa: E402
import pool_state                # noqa: E402
from fastapi.testclient import TestClient   # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print("  %s %s%s" % ("PASS" if cond else "FAIL", name, ("  " + str(detail)) if detail != "" else ""))
    if not cond:
        FAILS.append(name)


def ck(user, tok="t0ken", extra=""):
    return "hy_source=web; hy_user=%s; hy_token=%s%s" % (user, tok, extra)


def u1():
    print("[U1] _clean_cookie")
    cases = [
        ("hy_user=a; hy_token=b", "hy_user=a; hy_token=b"),
        ("  hy_user=a; hy_token=b  ", "hy_user=a; hy_token=b"),
        ("-b 'hy_user=a; hy_token=b'", "hy_user=a; hy_token=b"),
        ('Cookie: hy_user=a; hy_token=b', "hy_user=a; hy_token=b"),
        ('"hy_user=a; hy_token=b"', "hy_user=a; hy_token=b"),
        ("", ""),
    ]
    for raw, want in cases:
        got = ybp._clean_cookie(raw)
        check("U1 %-34s" % (raw[:34] or "(空)"), got == want, "got=%r" % got)


def u2():
    print("[U2] _bulk_name")
    tmp = tempfile.mkdtemp()
    try:
        acc = _acc.CookieAccounts(tmp)
        ybp.COOKIE_ACCOUNTS = acc
        n1 = ybp._bulk_name("yb", "371243fa233246a1")
        check("U2 前缀+hy_user前6", n1 == "yb371243", n1)
        acc.add(n1, ck("371243fa233246a1"))
        n2 = ybp._bulk_name("yb", "371243fa233246a1")
        check("U2 冲突自动加序号", n2 == "yb371243-2", n2)
        n3 = ybp._bulk_name("prod", "abcdef123456")
        check("U2 自定义前缀", n3 == "prodabcdef", n3)
        n4 = ybp._bulk_name("", "!!!!")
        check("U2 非法字符被清洗", n4.startswith("yb") and "-" not in n4, n4)
    finally:
        ybp.COOKIE_ACCOUNTS = None
        shutil.rmtree(tmp, ignore_errors=True)


VERDICT = {}     # cookie 里的 hy_user → 探针结论


def fake_probe(cookie, agent_id="naQivTmsDa"):
    u = _acc.CookieAccounts.hy_user_of(cookie)
    kind = VERDICT.get(u, "ok")
    return {"ok": kind == "ok", "banned": kind == "banned", "kind": kind,
            "status": 200 if kind == "ok" else 403, "detail": "mock:" + kind}


def u3():
    print("[U3] 端点：导入 / 替换 / 拒收 / 去重")
    tmp = tempfile.mkdtemp()
    real = (ybp._cookie_ban_probe, ybp._wm_ensure_cookie, ybp.COOKIE_ACCOUNTS, ybp.YB_BULK_GAP,
            ybp.YB_AUTO_WATERMARK)
    try:
        acc = _acc.CookieAccounts(tmp)
        ybp.COOKIE_ACCOUNTS = acc
        ybp._cookie_ban_probe = fake_probe
        ybp._wm_ensure_cookie = lambda *a, **k: None
        ybp.YB_BULK_GAP = 0
        ybp.YB_AUTO_WATERMARK = 0
        VERDICT.clear()
        VERDICT.update({"u_ban": "banned", "u_exp": "expired", "u_bad": "unusable"})
        c = TestClient(ybp.app)

        text = "\n".join([
            ck("u_ok1", "tok1"),
            ck("u_ok1", "tok1"),                      # 完全重复 → 同批去重
            ck("u_ban", "tok"),                       # 封号
            ck("u_exp", "tok"),                        # 过期
            ck("u_bad", "tok"),                        # 其它不可用
            "hy_source=web; hy_token=nouser",          # 缺 hy_user
        ])
        r = c.post("/admin/group/bulk", json={"text": text, "prefix": "ybt"})
        d = r.json()["result"]
        st = [x["status"] for x in d["results"]]
        check("U3 http 200", r.status_code == 200, r.status_code)
        check("U3 同批去重后共 5 条", d["total"] == 5, d["total"])
        check("U3 只成功 1 条", d["added"] == 1, d["added"])
        check("U3 状态序列正确", st == ["added", "banned", "expired", "unusable", "bad_format"], str(st))
        names = [x["name"] for x in d["results"] if x.get("name")]
        check("U3 落库名按前缀", names and names[0].startswith("ybt"), str(names))
        check("U3 库里只有 1 个号", len(acc.list()) == 1, [x["name"] for x in acc.list()])
        check("U3 入库健康态 alive",
              (acc.list()[0]["state"] or {}).get("last_health", {}).get("ok") is True)

        # 同一个 hy_user 换 cookie → 替换而非新增
        r2 = c.post("/admin/group/bulk", json={"cookies": [ck("u_ok1", "tok2-new")], "prefix": "ybt"})
        d2 = r2.json()["result"]
        check("U3 同号二次导入=替换", d2["results"][0]["status"] == "replaced",
              d2["results"][0]["status"])
        check("U3 仍然只有 1 个号", len(acc.list()) == 1, len(acc.list()))
        check("U3 cookie 已更新",
              "tok2-new" in (acc.cookie(acc.list()[0]["name"]) or ""))
    finally:
        (ybp._cookie_ban_probe, ybp._wm_ensure_cookie, ybp.COOKIE_ACCOUNTS, ybp.YB_BULK_GAP,
         ybp.YB_AUTO_WATERMARK) = real
        shutil.rmtree(tmp, ignore_errors=True)


def u4():
    print("[U4] dry_run 只验不写")
    tmp = tempfile.mkdtemp()
    real = (ybp._cookie_ban_probe, ybp.COOKIE_ACCOUNTS, ybp.YB_BULK_GAP)
    try:
        acc = _acc.CookieAccounts(tmp)
        ybp.COOKIE_ACCOUNTS = acc
        ybp._cookie_ban_probe = fake_probe
        ybp.YB_BULK_GAP = 0
        c = TestClient(ybp.app)
        r = c.post("/admin/group/bulk", json={"cookies": [ck("u_dry", "t")], "dry_run": True})
        d = r.json()["result"]
        check("U4 状态 would_add", d["results"][0]["status"] == "would_add", d["results"])
        check("U4 未落库", len(acc.list()) == 0, len(acc.list()))
        check("U4 added=0", d["added"] == 0)
    finally:
        (ybp._cookie_ban_probe, ybp.COOKIE_ACCOUNTS, ybp.YB_BULK_GAP) = real
        shutil.rmtree(tmp, ignore_errors=True)


def u5():
    print("[U5] 探测期抑制副作用（不误伤实例账号）")
    tmp = tempfile.mkdtemp()
    real = ybp.ACCOUNT
    try:
        inst = pool_state.AccountState(os.path.join(tmp, "inst.json"), "accX", 3, 1, 0)
        ybp.ACCOUNT = inst
        # 未抑制 → 会把实例账号踢掉（重现旧 bug）
        ybp._note_expired('{"code":"23000","message":"登录已过期"}')
        check("U5 未抑制时确实会误伤", inst.get()["state"] == "quarantined", inst.get()["state"])
        inst.apply("enable", "复位")
        # 抑制后 → 不动
        ybp._TLS.suppress = True
        hit_b = ybp._note_ban("20004 用户被封号")
        hit_e = ybp._note_expired("23000 登录已过期")
        ybp._TLS.suppress = False
        check("U5 抑制后返回 False", hit_b is False and hit_e is False)
        check("U5 抑制后实例账号不变", inst.get()["state"] == "enabled", inst.get()["state"])
    finally:
        ybp.ACCOUNT = real
        ybp._TLS.suppress = False
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    u1(); u2(); u3(); u4(); u5()
    print("\n%s  %d 项失败" % ("❌" if FAILS else "✅ 全部通过", len(FAILS)))
    for f in FAILS:
        print("   -", f)
    sys.exit(1 if FAILS else 0)
