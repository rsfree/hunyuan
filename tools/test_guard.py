#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_guard.py — 调用护栏自检（离线）。
用法：/Users/betterme/.workbuddy/binaries/python/envs/default/bin/python tools/test_guard.py
"""
import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import guard  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print("  %s %s%s" % ("PASS" if cond else "FAIL", name, ("  " + detail) if detail else ""))
    if not cond:
        FAILS.append(name)


def g1():
    print("[G1] 养号期禁生图")
    g = guard.Guard(warmup_days=3, daily_quota=0, age_days=lambda: 0.5)
    r = g.check("image")
    check("G1 0.5 天 < 3 天 ⇒ 拒生图", r is not None and r["status"] == 503, str(r and r["status"]))
    check("G1 错误码正确", r and r["error"]["code"] == "warmup_image_blocked", str(r and r["error"]))
    check("G1 文案含天数", r and "0.5" in r["error"]["message"], r and r["error"]["message"][:60])
    check("G1 聊天放行", g.check("chat") is None)
    r2 = guard.Guard(warmup_days=3, age_days=lambda: 3.2).check("image")
    check("G1 3.2 天 ≥ 3 天 ⇒ 放行", r2 is None)
    r3 = guard.Guard(warmup_days=0, age_days=lambda: 0.0).check("image")
    check("G1 warmup=0 ⇒ 关闭", r3 is None)


def g2():
    print("[G2] 每日配额")
    g = guard.Guard(warmup_days=0, daily_quota=3)
    check("G2 初始 0", g.used() == 0)
    g.note(); g.note()
    check("G2 用 2 次仍放行", g.check("chat") is None and g.used() == 2)
    g.note()
    r = g.check("chat")
    check("G2 用满 3 次 ⇒ 429", r is not None and r["status"] == 429, str(r and r["status"]))
    check("G2 错误码正确", r and r["error"]["code"] == "daily_quota_exceeded")
    check("G2 生图同样被挡", g.check("image")["status"] == 429)
    g2b = guard.Guard(daily_quota=0)
    for _ in range(50):
        g2b.note()
    check("G2 quota=0 ⇒ 不限", g2b.check("image") is None)


def g3():
    print("[G3] 节奏抖动")
    import random
    random.seed(7)
    vals = [guard.pace_seconds(2, 1.5) for _ in range(200)]
    check("G3 下界 ≥ 2", min(vals) >= 2.0, "min=%.3f" % min(vals))
    check("G3 上界 ≤ 3.5", max(vals) <= 3.5, "max=%.3f" % max(vals))
    check("G3 确实有随机（不是常数）", len({round(v, 3) for v in vals}) > 100)
    check("G3 jitter=0 ⇒ 恒定", {guard.pace_seconds(2, 0) for _ in range(10)} == {2.0})


def g4():
    print("[G4] snapshot")
    g = guard.Guard(warmup_days=3, daily_quota=10, age_days=lambda: 1.234)
    g.note()
    s = g.snapshot()
    check("G4 字段齐全", all(k in s for k in ("warmup_days", "daily_quota", "used_today", "account_age_days")))
    check("G4 数值正确", s["used_today"] == 1 and s["account_age_days"] == 1.23, str(s))


def g5():
    """源码顺序的静态断言：护栏必须引用的变量已经先定义。

    这条是为真实事故加的：把 GUARD.check(...) 插到 `image_flow = ...` **之前**，
    编译能过、导入能过，但一调用就 UnboundLocalError ⇒ 整条流式链路 500（线上踩过）。
    """
    print("[G5] 护栏插入位置（静态顺序）")
    src = io.open(os.path.join(ROOT, "yuanbao_openai_proxy.py"), encoding="utf-8").read()
    lines = src.split("\n")
    pos = {}
    for i, ln in enumerate(lines, 1):
        if "image_flow = bool(" in ln:
            pos.setdefault("image_flow", i)
        if 'GUARD.check("image" if (image_flow' in ln:
            pos.setdefault("guard", i)
        if ln.strip() == "created = int(time.time())":
            pos.setdefault("created", i)
    check("G5 找到 image_flow 赋值", "image_flow" in pos, str(pos))
    check("G5 找到 guard 调用", "guard" in pos, str(pos))
    check("G5 image_flow 先于 guard", pos.get("image_flow", 10**9) < pos.get("guard", -1),
          "image_flow=%s guard=%s" % (pos.get("image_flow"), pos.get("guard")))
    # guard 必须早于"真流式分支"，否则被拦的请求会先建立 SSE 连接再报错
    stream_line = next((i for i, ln in enumerate(lines, 1) if "真流式优先" in ln), None)
    check("G5 找到真流式分支", stream_line is not None, str(stream_line))
    check("G5 guard 先于真流式分支", pos.get("guard", 10**9) < (stream_line or -1),
          "guard=%s stream=%s" % (pos.get("guard"), stream_line))


if __name__ == "__main__":
    g1(); g2(); g3(); g4(); g5()
    print("\n%s  %d 项失败" % ("❌" if FAILS else "✅ 全部通过", len(FAILS)))
    for f in FAILS:
        print("   -", f)
    sys.exit(1 if FAILS else 0)
