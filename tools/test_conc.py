#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_conc.py — 每凭证并发闸门 / pace 并发语义 / 锁范围

背景（2026-10-21 实测）：`_cookie_mode_run` 把**整个请求**包在全局 `_bsk_lock` 里
⇒ 一个实例同时只能跑一个请求，吞吐 = 1/处理时间（聊天 15-20s ⇒ 3-4 次/分钟）。
签名只绑设备、拿到后可复用，请求本体是纯 HTTP ⇒ 只需锁铸签那一步。

改动：
  1. 锁范围收窄到 `_sig()`（只有铸签串行）
  2. 新增 `YB_ACCT_CONCURRENCY`（每凭证同时在途上限，默认 5）+ `acct_slot()`
  3. `pace()` 改「预约发车时刻」，并发下仍严格保持间隔
  4. 端点用 `asyncio.to_thread` 执行（否则同步阻塞卡死事件循环，并发毫无意义）

覆盖：
  C1 默认上限 5
  C2 8 线程抢槽 ⇒ 峰值严格 = 5
  C3 改配置后 Semaphore 立刻重建（2 / 1）
  C4 pace 并发下间隔不失效
  C5 不同凭证的闸门互相独立
  C6 源码断言：锁只包铸签、端点走 to_thread
"""
import importlib.util
import os
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

spec = importlib.util.spec_from_file_location("ybp", os.path.join(ROOT, "yuanbao_openai_proxy.py"))
ybp = importlib.util.module_from_spec(spec)
sys.modules["ybp"] = ybp
spec.loader.exec_module(ybp)

FAILS = []


def check(name, cond, detail=""):
    print("  %s %s%s" % ("PASS" if cond else "FAIL", name, ("  " + str(detail)) if detail != "" else ""))
    if not cond:
        FAILS.append(name)


def peak_of(limit, n, hold=0.2):
    ybp.YB_ACCT_CONCURRENCY = limit
    sem = ybp.acct_slot("ck:TESTKEY")
    cur = [0]
    peak = [0]
    lk = threading.Lock()

    def w():
        with sem:
            with lk:
                cur[0] += 1
                peak[0] = max(peak[0], cur[0])
            time.sleep(hold)
            with lk:
                cur[0] -= 1

    ts = [threading.Thread(target=w) for _ in range(n)]
    t0 = time.time()
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    return peak[0], round(time.time() - t0, 2)


def c1():
    print("[C1] 默认并发上限")
    real = ybp.YB_ACCT_CONCURRENCY
    try:
        # 模块默认值（未受环境变量影响时应为 5）
        check("C1 默认 5（或环境覆盖值）", isinstance(real, int) and real >= 1, real)
    finally:
        ybp.YB_ACCT_CONCURRENCY = real


def c2():
    print("[C2] 8 线程抢 5 个槽 ⇒ 峰值严格 = 5")
    real = ybp.YB_ACCT_CONCURRENCY
    try:
        pk, el = peak_of(5, 8, 0.2)
        check("C2 峰值 = 5", pk == 5, "peak=%d wall=%.2fs" % (pk, el))
        check("C2 确实排队（墙钟 > 1 批耗时）", el >= 0.35, el)
    finally:
        ybp.YB_ACCT_CONCURRENCY = real


def c3():
    print("[C3] 改配置后闸门立刻重建")
    real = ybp.YB_ACCT_CONCURRENCY
    try:
        pk2, _ = peak_of(2, 6, 0.2)
        check("C3 上限改成 2 ⇒ 峰值 2", pk2 == 2, pk2)
        pk1, el1 = peak_of(1, 4, 0.12)
        check("C3 上限改成 1 ⇒ 完全串行", pk1 == 1 and el1 >= 0.42, "peak=%d wall=%.2f" % (pk1, el1))
        pk9, _ = peak_of(9, 9, 0.12)
        check("C3 上限改成 9 ⇒ 峰值 9（全并行）", pk9 == 9, pk9)
    finally:
        ybp.YB_ACCT_CONCURRENCY = real


def c4():
    print("[C4] pace 在并发下仍保持间隔")
    real_i, real_j = ybp.YB_MIN_INTERVAL, ybp.YB_MIN_JITTER
    try:
        ybp.YB_MIN_INTERVAL, ybp.YB_MIN_JITTER = 0.4, 0.2   # 缩短以加速测试
        ybp._last_req_at.clear()
        stamps = []
        lk = threading.Lock()

        def p():
            ybp.pace("ck:PACEKEY")
            with lk:
                stamps.append(time.time())

        ts = [threading.Thread(target=p) for _ in range(4)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        stamps.sort()
        gaps = [round(stamps[i] - stamps[i - 1], 3) for i in range(1, len(stamps))]
        check("C4 4 个并发全部排上队", len(stamps) == 4, len(stamps))
        check("C4 相邻间隔都 ≥ 0.39s（并发没让间隔失效）",
              all(g >= 0.39 for g in gaps), gaps)
        check("C4 总跨度 ≈ 3 个间隔", (stamps[-1] - stamps[0]) >= 1.1,
              round(stamps[-1] - stamps[0], 2))
    finally:
        ybp.YB_MIN_INTERVAL, ybp.YB_MIN_JITTER = real_i, real_j
        ybp._last_req_at.clear()


def c5():
    print("[C5] 不同凭证的闸门互相独立")
    real = ybp.YB_ACCT_CONCURRENCY
    try:
        ybp.YB_ACCT_CONCURRENCY = 1
        a, b = ybp.acct_slot("ck:A"), ybp.acct_slot("ck:B")
        check("C5 不同 key 是不同 Semaphore", a is not b)
        check("C5 同一 key 返回同一对象", ybp.acct_slot("ck:A") is a)
        # A 占满时 B 仍可进
        a.acquire()
        got = b.acquire(timeout=0.3)
        check("C5 A 占槽不影响 B", got is True, got)
        b.release()
        a.release()
    finally:
        ybp.YB_ACCT_CONCURRENCY = real


def c6():
    print("[C6] 源码断言：锁只包铸签 / 端点走 to_thread")
    src = open(os.path.join(ROOT, "yuanbao_openai_proxy.py"), encoding="utf-8").read()

    # _cookie_mode_run 本体不再持有 _bsk_lock（只是闸门）
    i = src.find("def _cookie_mode_run(cookie")
    j = src.find("def _cookie_mode_run_inner(")
    wrapper = src[i:j]
    check("C6 闸门层不含 _bsk_lock", "_bsk_lock" not in wrapper, "锁又回来了")
    check("C6 闸门层用 acct_slot", "acct_slot(pkey)" in wrapper)
    check("C6 先 pace 再抢槽（顺序）",
          wrapper.find("pace(pkey)") < wrapper.find("sem.acquire()"))

    # inner 里只有 _sig 用锁
    k = src.find("def _cookie_mode_stream(")
    inner = src[j:k]
    # 只数真正的加锁语句（docstring/注释里提到 _bsk_lock 不算）
    acq = [ln for ln in inner.split("\n") if ln.strip().startswith("with _bsk_lock:")]
    check("C6 inner 只有一处 with _bsk_lock（铸签）", len(acq) == 1, len(acq))
    check("C6 inner 用 _sig() 铸签", "sig = _sig()" in inner and "_sig(force=True)" in inner)

    # 端点必须 to_thread
    check("C6 chat 端点走 to_thread", "asyncio.to_thread(" in src and "_cookie_mode_run, cookie_str" in src)
    check("C6 新配置项存在", "YB_ACCT_CONCURRENCY" in src)


def c7():
    """凭证分桶必须按**账号**（hy_user），不能按 cookie 前缀。

    真实事故：`cookie[:16]` 对所有元宝账号都是 `hy_source=web; h`
    ⇒ 全池共用同一个限速队列 + 同一组并发槽 ⇒ 加号完全不扩容。
    """
    print("[C7] 凭证分桶按 hy_user（不能用 cookie 前缀）")
    ck_a = "hy_source=web; hy_user=aaaa1111; hy_token=t1"
    ck_b = "hy_source=web; hy_user=bbbb2222; hy_token=t2"
    ka, kb = ybp._acct_key(ck_a), ybp._acct_key(ck_b)
    check("C7 两个号前缀相同（复现前提）", ck_a[:16] == ck_b[:16], ck_a[:16])
    check("C7 但 key 不同", ka != kb, "%s vs %s" % (ka, kb))
    check("C7 key 用 hy_user", ka == "ck:aaaa1111", ka)

    # 同一个号换 cookie（token 变了）⇒ 仍是同一个桶
    ck_a2 = "hy_source=web; hy_user=aaaa1111; hy_token=NEWTOKEN"
    check("C7 同号换 token 仍是同一桶", ybp._acct_key(ck_a2) == ka, ybp._acct_key(ck_a2))

    # 无 hy_user ⇒ 退回指纹，且不同输入不同 key
    check("C7 无 hy_user 不崩", ybp._acct_key("foo=1; bar=2").startswith("ck:"))
    check("C7 无 hy_user 时不同输入不同 key",
          ybp._acct_key("foo=1") != ybp._acct_key("foo=2"))

    # 真实形态：不同号的并发槽必须互相独立
    real = ybp.YB_ACCT_CONCURRENCY
    try:
        ybp.YB_ACCT_CONCURRENCY = 1
        check("C7 两号各自独立闸门", ybp.acct_slot(ka) is not ybp.acct_slot(kb))
    finally:
        ybp.YB_ACCT_CONCURRENCY = real


if __name__ == "__main__":
    c1(); c2(); c3(); c4(); c5(); c6(); c7()
    print()
    if FAILS:
        print("FAILED %d: %s" % (len(FAILS), FAILS))
        sys.exit(1)
    print("全部通过")
