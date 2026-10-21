#!/usr/bin/env python3
"""风控节律单测：时间窗 / 失败退避 / 生图冷却 / 生图独立并发 / 非活跃时段拦截。

为什么单独测：这几条都是"时间维度的行为"，出错时不会报错、只会**悄悄变回脚本节奏**
（例如 sem 缓存键写错 ⇒ 生图又用回 5 并发），线上根本看不出来。
"""
import importlib.util
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

PASS = FAIL = 0


def ok(cond, name, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  PASS %s %s" % (name, extra))
    else:
        FAIL += 1
        print("  FAIL %s %s" % (name, extra))


def load():
    spec = importlib.util.spec_from_file_location("ybp_rh", os.path.join(ROOT, "yuanbao_openai_proxy.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


m = load()
import guard as _g  # noqa: E402

# 统一用很小的间隔，测试跑得快
m.YB_MIN_INTERVAL = 0.10
m.YB_MIN_JITTER = 0.0
m.YB_IMAGE_COOL = "0"

# ---------- R1 时间窗：窗口外 ×3 ----------
h = time.localtime().tm_hour
out_start, out_end = (h + 2) % 24, (h + 3) % 24      # 保证"现在"不在窗口内
m.WINDOW = _g.Window("%d-%d" % (out_start, out_end), slow=3.0)
m.pace("k1", "chat")                                  # 预热：首次发车不等待（设计如此）
t0 = time.time()
m.pace("k1", "chat")
dt_out = time.time() - t0
m.WINDOW = _g.Window("", 3.0)                        # 关闭窗口
m.pace("k2", "chat")                                 # 预热
t0 = time.time()
m.pace("k2", "chat")
dt_in = time.time() - t0
ok(0.25 <= dt_out <= 0.45 and dt_in < 0.15, "R1 非活跃时段间隔 ×3",
   "%.2fs vs %.2fs" % (dt_out, dt_in))

# ---------- R2 失败退避 ----------
m.BACKOFF = _g.Backoff(0.2, 2.0)
m.pace("k3", "chat")                                 # 预热
m.BACKOFF.fail("k3")
t0 = time.time()
m.pace("k3", "chat")
dt = time.time() - t0
ok(dt >= 0.25, "R2 失败后退避生效", "%.2fs" % dt)
m.BACKOFF.ok("k3")
t0 = time.time()
m.pace("k3", "chat")
ok(time.time() - t0 < 0.2, "R2 成功后清零")

# ---------- R3 生图冷却 ----------
m.pace("k4", "chat")                                 # 预热
m._IMG_COOL_UNTIL["k4"] = time.time() + 0.30
t0 = time.time()
m.pace("k4", "chat")
dt = time.time() - t0
ok(dt >= 0.28, "R3 生图后冷却计入下一次发车", "%.2fs" % dt)
ok(m._IMG_COOL_UNTIL.get("k4") is None, "R3 冷却一次性消费（不会永久拖慢）")

# ---------- R4 生图独立并发 ----------
m.YB_ACCT_CONCURRENCY = 5
m.YB_IMAGE_CONCURRENCY = 2
s_chat = m.acct_slot("k5", "chat")
s_img = m.acct_slot("k5", "image")
ok(s_chat is not s_img, "R4 生图与聊天是不同闸门")
ok(getattr(s_img, "_yb_limit", None) == 2 and getattr(s_chat, "_yb_limit", None) == 5,
   "R4 限值各自独立", "img=%s chat=%s" % (s_img._yb_limit, s_chat._yb_limit))
ok(m.acct_slot("k5", "image") is s_img, "R4 同一 kind 复用同一 sem")

# ---------- R5 非活跃时段 block ----------
m.WINDOW = _g.Window("%d-%d" % (out_start, out_end), slow=3.0, mode="block")
called = []
m._cookie_mode_run_inner = lambda *a, **k: called.append(1) or {"status": 200}
r = m._cookie_mode_run("hy_source=web; hy_user=ZZ; a=1", "ag", "p", "hunyuan", ["ref"], "2K")
ok(r.get("status") == 503 and not called, "R5 block 模式下生图被拦且未发上游", str(r.get("error", ""))[:30])
m.WINDOW = _g.Window("", 3.0)

# ---------- R6 冷却区间解析 ----------
ok(m._cool_range() == (0.0, 0.0), "R6 YB_IMAGE_COOL=0 → 不冷却")
m.YB_IMAGE_COOL = "4-10"
ok(m._cool_range() == (4.0, 10.0), "R6 解析 4-10", str(m._cool_range()))
m.YB_IMAGE_COOL = "7"
ok(m._cool_range() == (7.0, 7.0), "R6 单值 → 固定")

# ---------- R7 成交路径：生图成功后排冷却 + 清退避 ----------
m.YB_IMAGE_COOL = "4-10"
m._IMG_COOL_UNTIL.clear()
m._cookie_mode_run_inner = lambda *a, **k: {"status": 200, "urls": ["https://x/a.png"]}
m.pace = lambda *a, **k: None                # 跳过等待，只看记账
m.acct_slot = lambda *a, **k: type("S", (), {"acquire": lambda s: None, "release": lambda s: None})()
m.BACKOFF = _g.Backoff(0.5, 5)
m.BACKOFF.fail("ck:ZZ")
m._cookie_mode_run("hy_source=web; hy_user=ZZ; a=1", "ag", "p", "hunyuan", ["ref"], "2K")
ok("ck:ZZ" in m._IMG_COOL_UNTIL, "R7 生图成功后排冷却")
ok(m.BACKOFF.wait("ck:ZZ") == 0, "R7 成功后清退避")

print("\n%s 通过 %d 项，失败 %d 项" % ("✅ 全部通过" if not FAIL else "❌ 有失败", PASS, FAIL))
sys.exit(1 if FAIL else 0)
