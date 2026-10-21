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
    print("  %s %s%s" % ("PASS" if cond else "FAIL", name, ("  " + str(detail)) if detail != "" else ""))
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
    print("[G2] 每日配额（**只约束生成类**，聊天不受限）")
    g = guard.Guard(warmup_days=0, daily_quota=3)
    check("G2 初始 0", g.used() == 0)
    g.note("image"); g.note("image")
    check("G2 用 2 次仍放行", g.check("image") is None and g.used() == 2)
    g.note("image")
    r = g.check("image")
    check("G2 用满 3 次 ⇒ 429", r is not None and r["status"] == 429, str(r and r["status"]))
    check("G2 错误码正确", r and r["error"]["code"] == "daily_quota_exceeded")
    # 🔴 曾经的 bug：quota 把聊天也算进去 ⇒ 配额用尽后连聊天都 429（养号期没法养号）
    check("G2 配额用尽后聊天仍放行", g.check("chat") is None, "聊天被配额锁死了")
    g2b = guard.Guard(daily_quota=0)
    for _ in range(50):
        g2b.note("image")
    check("G2 quota=0 ⇒ 不限", g2b.check("image") is None)
    g2c = guard.Guard(daily_quota=2)
    for _ in range(50):
        g2c.note("chat")
    check("G2 只记聊天不占配额", g2c.used() == 0 and g2c.check("image") is None, g2c.used())


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
    g.note("image")
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
        if 'GUARD.check(_gk)' in ln or 'GUARD.check("image" if (image_flow' in ln:
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
    # 配额只卡生成类 ⇒ check/note 必须带上 image_flow 推出的 kind，而不是恒定 "chat"
    check("G5 kind 由 image_flow 决定", '_gk = "image" if (image_flow' in src)
    check("G5 note 透传 kind", "GUARD.note(_gk)" in src)


def g6():
    """配额语义：只卡生成类，聊天永远放行。

    真实事故：`note()` 无条件计数 + quota 判定不区分 kind ⇒
    设 YB_DAILY_QUOTA 后，第 N 次**任何**调用（含聊天）都被 429，
    与"养号期靠纯文本聊天养号"的设计直接矛盾。
    """
    print("[G6] 日配额只卡生成类")
    G = guard.Guard(warmup_days=0, daily_quota=2)
    real_q, real_c = G.daily_quota, G._count
    try:
        G.daily_quota, G._count = 2, 0
        seq = []
        for _ in range(4):
            r = G.check("image")
            seq.append("拒绝" if r else "放行")
            if not r:
                G.note("image")
        check("G6 生成类前 2 次放行", seq[:2] == ["放行", "放行"], str(seq))
        check("G6 生成类第 3/4 次拒绝", seq[2:] == ["拒绝", "拒绝"], str(seq))
        check("G6 配额用尽后聊天仍放行", G.check("chat") is None, "聊天被配额锁死了")

        # note 只对 image 计数
        G.daily_quota, G._count = 0, 0
        for _ in range(5):
            G.note("chat")
        check("G6 连记 5 次聊天 → 计数不变", G._count == 0, G._count)
        for _ in range(3):
            G.note("image")
        check("G6 记 3 次生图 → 计数 = 3", G._count == 3, G._count)
        check("G6 note() 缺省不计数（向后兼容老调用点）",
              (G.note() or 0) == 3, G._count)
    finally:
        G.daily_quota, G._count = real_q, real_c


if __name__ == "__main__":
    g1(); g2(); g3(); g4(); g5(); g6()
    print("\n%s  %d 项失败" % ("❌" if FAILS else "✅ 全部通过", len(FAILS)))
    for f in FAILS:
        print("   -", f)
    sys.exit(1 if FAILS else 0)
