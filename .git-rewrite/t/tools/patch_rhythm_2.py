#!/usr/bin/env python3
"""补丁 2/3：主服务接入节律三件套 + 生图独立并发。

改动点：
  1. 新配置：YB_ACTIVE_HOURS / YB_IMAGE_CONCURRENCY / YB_FAIL_BACKOFF / YB_IMAGE_COOL
  2. `acct_slot(key, kind)`：生图用**独立**并发槽（默认 2，聊天仍 5）
     —— 真实用户不会同一瞬间生成 5 张图，这是最强的"非人类"信号之一
  3. `pace(key, kind)`：叠上时间窗倍率 + 失败退避等待
  4. 闸门层记成败：失败→指数退避；生图成功→"看结果"冷却（拉长下一次发车间隔）
"""
import ast

P = "yuanbao_openai_proxy.py"
src = open(P).read()
orig = src

# ---- 1) 配置项 ----
a = '''YB_ACCT_CONCURRENCY = int(os.environ.get("YB_ACCT_CONCURRENCY", "5"))'''
b = '''YB_ACCT_CONCURRENCY = int(os.environ.get("YB_ACCT_CONCURRENCY", "5"))
# 生图单独一道更窄的并发闸：人类一次只生成一张，并发 5 张是最明显的脚本特征
YB_IMAGE_CONCURRENCY = int(os.environ.get("YB_IMAGE_CONCURRENCY", "2"))
# 活跃时间窗（"7-24"=早 7 点到 24 点；空=不启用）。窗口外只降速不拒绝，见 guard.Window
YB_ACTIVE_HOURS = os.environ.get("YB_ACTIVE_HOURS", "")
YB_OFFHOURS_SLOW = float(os.environ.get("YB_OFFHOURS_SLOW", "3"))   # 窗口外间隔倍率
YB_OFFHOURS_MODE = os.environ.get("YB_OFFHOURS_MODE", "slow")        # slow|block
YB_FAIL_BACKOFF = float(os.environ.get("YB_FAIL_BACKOFF", "2"))      # 连续失败退避基数（0=关）
YB_FAIL_BACKOFF_CAP = float(os.environ.get("YB_FAIL_BACKOFF_CAP", "60"))
YB_IMAGE_COOL = os.environ.get("YB_IMAGE_COOL", "4-10")              # 生图后停顿区间（秒）
YB_BROWSE_EVERY = int(os.environ.get("YB_BROWSE_EVERY", "0"))        # 每 N 次生图插一次浏览，0=关'''
assert src.count(a) == 1, "YB_ACCT_CONCURRENCY 锚点不唯一"
src = src.replace(a, b, 1)

# ---- 2) 实例化 Window / Backoff（跟在 GUARD 之后） ----
a = '''GUARD = _guard.Guard(YB_WARMUP_DAYS, YB_DAILY_QUOTA, _account_age_days)'''
b = '''GUARD = _guard.Guard(YB_WARMUP_DAYS, YB_DAILY_QUOTA, _account_age_days)
# 节律三件套（账号池环境变量可能被 compose 覆盖，这里集中实例化便于 /admin 展示）
WINDOW = _guard.Window(YB_ACTIVE_HOURS, YB_OFFHOURS_SLOW, YB_OFFHOURS_MODE)
BACKOFF = _guard.Backoff(YB_FAIL_BACKOFF, YB_FAIL_BACKOFF_CAP)
_IMG_COOL_UNTIL = {}             # key -> 该凭证最早可再次发车的时刻（生图"看结果"停顿）
_IMG_DONE_N = {}                 # key -> 本进程内生图成功次数（行为穿插用）'''
assert src.count(a) == 1, "GUARD 锚点不唯一"
src = src.replace(a, b, 1)

# ---- 3) acct_slot 支持 kind ----
a = '''def acct_slot(key: str):'''
b = '''def acct_slot(key: str, kind: str = "chat"):'''
assert src.count(a) == 1
src = src.replace(a, b, 1)

a = '''    limit = max(1, YB_ACCT_CONCURRENCY)
    with _ACCT_SEMS_LOCK:
        sem = _ACCT_SEMS.get(key)
        if sem is None or getattr(sem, "_yb_limit", None) != limit:
            sem = threading.Semaphore(limit)
            sem._yb_limit = limit
            _ACCT_SEMS[key] = sem
        return sem'''
b = '''    limit = max(1, YB_IMAGE_CONCURRENCY if kind == "image" else YB_ACCT_CONCURRENCY)
    skey = (key, kind) if kind == "image" else key
    with _ACCT_SEMS_LOCK:
        sem = _ACCT_SEMS.get(skey)
        if sem is None or getattr(sem, "_yb_limit", None) != limit:
            sem = threading.Semaphore(limit)
            sem._yb_limit = limit
            _ACCT_SEMS[skey] = sem
        return sem'''
assert src.count(a) == 1, "acct_slot 主体锚点不唯一"
src = src.replace(a, b, 1)

# ---- 4) pace 接入窗口倍率 / 退避 / 生图冷却 ----
a = '''    target = _guard.pace_seconds(YB_MIN_INTERVAL, YB_MIN_JITTER)'''
b = '''    target = _guard.pace_seconds(YB_MIN_INTERVAL, YB_MIN_JITTER)
    # ① 非活跃时段整体拖慢（默认 ×3）：凌晨还在稳定出图是最刺眼的信号之一
    target *= WINDOW.factor()
    # ② 刚生完图的那一小段"看结果"时间人不该发下一条
    _cool = _IMG_COOL_UNTIL.get(key, 0)
    if _cool > time.time():
        target += _cool - time.time()
        _IMG_COOL_UNTIL.pop(key, None)
    # ③ 连续失败时退避（失败后立刻重试会形成固定重试节奏）
    _bw = BACKOFF.wait(key)
    if _bw > 0:
        target += _bw'''
assert src.count(a) == 1, "pace target 锚点不唯一"
src = src.replace(a, b, 1)

# ---- 5) 闸门层传 kind + 记成败（非流式） ----
a = '''    pkey = _acct_key(cookie)          # 账号级分桶（不能用 cookie 前缀，见 _acct_key 注释）
    pace(pkey)
    sem = acct_slot(pkey)
    sem.acquire()
    try:
        return _cookie_mode_run_inner(cookie, agent_id, prompt, chat_model,
                                      image_refs, resolution, ratio, force_image)
    finally:
        sem.release()'''
b = '''    pkey = _acct_key(cookie)          # 账号级分桶（不能用 cookie 前缀，见 _acct_key 注释）
    _kind = "image" if (image_refs or force_image) else "chat"
    if _kind == "image" and WINDOW.ok and not WINDOW.inside() and WINDOW.mode == "block":
        return {"error": "当前处于非活跃时段（%d-%d 点），生成请求被主动延后 —— 这是降风控策略，"
                         "可用 YB_OFFHOURS_MODE=slow 改为仅降速",
                "status": 503}
    pace(pkey, _kind)
    sem = acct_slot(pkey, _kind)
    sem.acquire()
    try:
        _r = _cookie_mode_run_inner(cookie, agent_id, prompt, chat_model,
                                    image_refs, resolution, ratio, force_image)
        _bad = bool((_r or {}).get("error")) or (_r or {}).get("status") in (429, 500, 502, 503)
        if _bad:
            BACKOFF.fail(pkey)
        else:
            BACKOFF.ok(pkey)
            if _kind == "image":
                _lo, _hi = _cool_range()
                if _hi > 0:
                    _IMG_COOL_UNTIL[pkey] = time.time() + _guard.image_cool(_lo, _hi)
                _maybe_browse(cookie, agent_id, pkey)
        return _r
    except Exception:
        BACKOFF.fail(pkey)
        raise
    finally:
        sem.release()'''
assert src.count(a) == 1, "闸门层锚点不唯一"
src = src.replace(a, b, 1)

# ---- 6) 流式闸门也用 kind=chat（保持分桶一致） ----
a = '''    pkey = _acct_key(cookie)        # 账号级分桶
    pace(pkey)                      # 先排队（等待不占并发槽）'''
b = '''    pkey = _acct_key(cookie)        # 账号级分桶
    pace(pkey, "chat")              # 先排队（等待不占并发槽）'''
assert src.count(a) == 1, "流式 pkey 锚点不唯一"
src = src.replace(a, b, 1)

a = '''    sem = acct_slot(pkey)
    sem.acquire()
    try:
        for attempt in range(YB_SOFTRETRY + 1):'''
b = '''    sem = acct_slot(pkey, "chat")
    sem.acquire()
    try:
        for attempt in range(YB_SOFTRETRY + 1):'''
assert src.count(a) == 1, "流式 sem 锚点不唯一"
src = src.replace(a, b, 1)

# ---- 7) 辅助：cool_range / 行为穿插 ----
a = '''def pace(key: str):'''
assert src.count(a) == 1, "pace 定义锚点不唯一"
NEW = '''def _cool_range():
    """YB_IMAGE_COOL="4-10" → (4.0, 10.0)；"0"/空 → (0, 0)。"""
    s = (YB_IMAGE_COOL or "").strip()
    if not s or s == "0":
        return (0.0, 0.0)
    try:
        if "-" in s:
            a, b = s.split("-", 1)
            return (float(a), float(b))
        v = float(s)
        return (v, v)
    except Exception:
        return (4.0, 10.0)


def _maybe_browse(cookie: str, agent_id: str, pkey: str):
    """行为穿插：每 N 次生图插一次**只读**请求。

    为什么：一个只会"上传 → 出图 → 上传 → 出图"的账号，行为熵极低。
    真实用户会翻历史、看设置。这里用 `/api/userinfo/getuserconfig`（页面自己也会调、
    无副作用、不需要签名里的额外字段），成本几乎为零。
    """
    if YB_BROWSE_EVERY <= 0:
        return
    n = _IMG_DONE_N.get(pkey, 0) + 1
    _IMG_DONE_N[pkey] = n
    if n % YB_BROWSE_EVERY:
        return
    try:
        with _bsk_lock:
            sig = get_sig(None, False)
        _yb_post_json(cookie, "/api/userinfo/getuserconfig",
                      {"scene": 1, "configFields": ["watermarkConfig"]}, sig, agent_id)
        print("[rhythm] 行为穿插：第 %d 次生图后读一次用户配置" % n, flush=True)
    except Exception as e:  # noqa: BLE001
        print("[rhythm] 行为穿插失败（忽略）: %s" % str(e)[:80], flush=True)


def pace(key: str, kind: str = "chat"):'''
src = src.replace(a, NEW, 1)
ast.parse(src)
assert src != orig
open(P, "w").write(src)
print("OK 主服务 +%d 字符" % (len(src) - len(orig)))
