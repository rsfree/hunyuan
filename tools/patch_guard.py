#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""patch_guard.py — 接入 guard：养号期禁生图 / 日配额 / 频率抖动（幂等、带断言）。"""
import io
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
P = os.path.join(ROOT, "yuanbao_openai_proxy.py")
PS = os.path.join(ROOT, "pool_state.py")

src = io.open(P, encoding="utf-8").read()
orig = src


def sub(label, old, new, count=1):
    global src
    n = src.count(old)
    assert n == count, "【%s】匹配数 %d != 期望 %d" % (label, n, count)
    src = src.replace(old, new)
    print("  ok  %-38s (%d)" % (label, n))


# ---------------------------------------------------------------- 1. 配置项
sub("新增 YB_MIN_JITTER", '''YB_MIN_INTERVAL = float(os.environ.get("YB_MIN_INTERVAL", "2"))  # 同凭证两请求最小间隔秒（限速）''',
    '''YB_MIN_INTERVAL = float(os.environ.get("YB_MIN_INTERVAL", "2"))  # 同凭证两请求最小间隔秒（限速）
YB_MIN_JITTER = float(os.environ.get("YB_MIN_JITTER", "1.5"))     # 最小间隔之上再叠 0~N 秒随机（越像人越好）
YB_WARMUP_DAYS = float(os.environ.get("YB_WARMUP_DAYS", "0"))     # 养号期：首次成功后的 N 天内禁生图（0=关闭）
YB_DAILY_QUOTA = int(os.environ.get("YB_DAILY_QUOTA", "0"))       # 每实例每天最多 N 次生成类调用（0=不限）''')

# ---------------------------------------------------------------- 2. import guard
sub("import guard", '''from fastapi import FastAPI, Request''',
    '''import guard as _guard

from fastapi import FastAPI, Request''')

# ---------------------------------------------------------------- 3. GUARD 初始化
sub("GUARD 初始化", '''def _mctx(**kw):
    c = _MCTX.get()
    if c is not None:
        c.update(kw)''',
    '''def _account_age_days() -> float:
    """本实例账号「首次成功」至今的天数（没有成功过 → 0）。养号期护栏用。"""
    try:
        if ACCOUNT is None:
            return 0.0
        t = (ACCOUNT.get() or {}).get("first_ok_at")
        if not t:
            return 0.0
        return max(0.0, (time.time() - float(t)) / 86400.0)
    except Exception:
        return 0.0


GUARD = _guard.Guard(YB_WARMUP_DAYS, YB_DAILY_QUOTA, _account_age_days)


def _mctx(**kw):
    c = _MCTX.get()
    if c is not None:
        c.update(kw)''')

# ---------------------------------------------------------------- 4. pace 抖动
sub("pace 加抖动", '''def pace(key: str):
    """同凭证限速：距上次请求不足 YB_MIN_INTERVAL 则等待。"""
    last = _last_req_at.get(key, 0)
    wait = YB_MIN_INTERVAL - (time.time() - last)
    if wait > 0:
        time.sleep(wait)
    _last_req_at[key] = time.time()''',
    '''def pace(key: str):
    """同凭证限速：最小间隔 YB_MIN_INTERVAL + 0~YB_MIN_JITTER 随机抖动。

    为什么加抖动：固定 2s 的等距请求是最容易被识别的脚本特征之一；
    人类操作间隔是长尾分布，加随机后每条连接的时间指纹就不再可疑。
    """
    target = _guard.pace_seconds(YB_MIN_INTERVAL, YB_MIN_JITTER)
    last = _last_req_at.get(key, 0)
    wait = target - (time.time() - last)
    if wait > 0:
        time.sleep(wait)
    _last_req_at[key] = time.time()''')

# ---------------------------------------------------------------- 5. chat 门护栏
sub("chat 门护栏", '''    created = int(time.time())
    comp_id = "chatcmpl-" + uuid.uuid4().hex[:24]''',
    '''    # ---- 护栏：养号期禁生图 / 每日配额（放在最前面，连会话都不建）----
    _g = GUARD.check("image" if (image_flow or unwm) else "chat")
    if _g:
        print("[guard] 拒绝 %s: %s" % (_g["error"].get("code"), _g["error"].get("message", "")[:80]), flush=True)
        return JSONResponse(_g["error"], status_code=_g["status"])
    GUARD.note()

    created = int(time.time())
    comp_id = "chatcmpl-" + uuid.uuid4().hex[:24]''')

# ---------------------------------------------------------------- 6. images 门护栏
sub("images 门护栏", '''    n = min(int(body.get("n", 1) or 1), 4)
    resolution = _size_to_resolution(body.get("size", ""))''',
    '''    n = min(int(body.get("n", 1) or 1), 4)
    resolution = _size_to_resolution(body.get("size", ""))

    # ---- 护栏：生图一律按 image 计（养号期直接挡掉）----
    _g = GUARD.check("image")
    if _g:
        print("[guard] 拒绝生图: %s" % _g["error"].get("code"), flush=True)
        return JSONResponse(_g["error"], status_code=_g["status"])
    GUARD.note()''')

# ---------------------------------------------------------------- 7. 状态可见
sub("state 暴露护栏", '''    return {"result": st, "keepalive": _KEEPALIVE, "watermark": _WATERMARK,
            "account": (ACCOUNT.get() if ACCOUNT else None)}''',
    '''    return {"result": st, "keepalive": _KEEPALIVE, "watermark": _WATERMARK,
            "account": (ACCOUNT.get() if ACCOUNT else None),
            "guard": GUARD.snapshot()}''')

assert src != orig
io.open(P, "w", encoding="utf-8").write(src)
print("写入:", P)

# ---------------------------------------------------------------- 8. pool_state 记首次成功时间
ps = io.open(PS, encoding="utf-8").read()
p_orig = ps
old = '''            if ok:
                self._d["ever_ok"] = True'''
new = '''            if ok:
                self._d["ever_ok"] = True
                if not self._d.get("first_ok_at"):
                    self._d["first_ok_at"] = int(time.time())   # 养号期的起点'''
assert ps.count(old) == 1, ps.count(old)
ps = ps.replace(old, new)
assert ps != p_orig
io.open(PS, "w", encoding="utf-8").write(ps)
print("写入:", PS)
