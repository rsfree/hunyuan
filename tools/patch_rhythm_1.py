#!/usr/bin/env python3
"""补丁：guard.py 增加「风控节律」三件套 —— 时间窗 / 失败退避 / 生图冷却。

为什么（2026-10-22 用线上 121 次生图日志复盘）：
  · 生图分散在 00/17/18/20/21/22/23 点，**凌晨还在生成** —— 真实用户不会
  · 相邻生图间隔中位 12s、46% 不到 5s —— 是并发同时发车的"簇状"节奏，
    这比"每 30 秒一次"更像脚本（风控看的不是总量，是突发形态）
  · 失败后立刻重试会形成固定重试节奏，同样可识别

三件套的分工：
  Window    —— 把"凌晨"这类非人类时段降速（默认只降速不拒绝，避免 API 客户半夜全挂）
  Backoff   —— 连续失败时指数退避，避免"失败→立刻重试→再失败"的机器循环
  image_cool—— 生图结束后补一段"看结果"的停顿（人类生成完会先看图）
"""
import ast

P = "guard.py"
src = open(P).read()
orig = src

# 1) 文首加模块说明补充
a = '''  - `check()` 与 `note()` 分开：调用方先 check 再 note，失败请求也计入配额（防"失败重试"刷量）。
"""'''
b = '''  - `check()` 与 `note()` 分开：调用方先 check 再 note，失败请求也计入配额（防"失败重试"刷量）。

节律三件套（2026-10-22 加）：
  Window   活跃时间窗（窗口外按倍率降速，默认不硬拒）
  Backoff  连续失败的指数退避（每凭证独立）
  image_cool 生图后的"看结果"停顿
"""'''
assert src.count(a) == 1
src = src.replace(a, b, 1)

# 2) 追加三个类（放在 pace_seconds 之前）
anchor = "def pace_seconds(min_interval: float, jitter: float) -> float:"
assert src.count(anchor) == 1
NEW = '''class Window:
    """活跃时间窗：`spec` 形如 "7-24" / "8-23"（本地时区，左闭右开）。

    窗口外**不拒绝**、只把发车间隔乘上 `slow` —— 直接把 API 客户凌晨的请求打成 503
    会逼对方去找别的渠道，风控效果反而变差；把节奏拖慢到"偶尔一张"就够像人了。
    `mode="block"` 时才硬拒（给不接受夜间调用的部署用）。
    """

    def __init__(self, spec: str = "", slow: float = 3.0, mode: str = "slow"):
        self.start = self.end = None
        self.ok = False
        self.slow = max(1.0, float(slow or 1.0))
        self.mode = (mode or "slow").strip().lower()
        s = (spec or "").strip()
        try:
            if s and "-" in s:
                a, b = s.split("-", 1)
                self.start, self.end = int(a), int(b)
                self.ok = 0 <= self.start <= 24 and 0 <= self.end <= 24 and self.start != self.end
        except Exception:
            self.ok = False

    def inside(self, ts=None) -> bool:
        if not self.ok:
            return True
        h = time.localtime(ts if ts is not None else time.time()).tm_hour
        if self.start < self.end:
            return self.start <= h < self.end
        return h >= self.start or h < self.end      # 跨零点，如 22-6

    def factor(self, ts=None) -> float:
        return 1.0 if self.inside(ts) else self.slow

    def snapshot(self) -> dict:
        return {"active_hours": ("%d-%d" % (self.start, self.end)) if self.ok else "",
                "mode": self.mode, "slow_factor": self.slow,
                "inside_now": self.inside()}


class Backoff:
    """每凭证的失败指数退避。

    🔴 只对**连不上/被拒**这类失败计数；上游正常的 4xx 业务错误不该退避（那是客户自己写错了）。
    成功一次就清零 —— 否则偶发失败会累积成永久降速。
    """

    def __init__(self, base: float = 2.0, cap: float = 60.0, enable: bool = True):
        self.base = max(0.0, float(base or 0))
        self.cap = max(self.base, float(cap or 0))
        self.enable = bool(enable) and self.base > 0
        self._n = {}
        self._until = {}
        self._lock = __import__("threading").Lock()

    def fail(self, key: str) -> float:
        if not self.enable:
            return 0.0
        with self._lock:
            n = self._n.get(key, 0) + 1
            self._n[key] = n
            wait = min(self.cap, self.base * (2 ** (n - 1)))
            self._until[key] = time.time() + wait
        return wait

    def ok(self, key: str):
        with self._lock:
            self._n.pop(key, None)
            self._until.pop(key, None)

    def wait(self, key: str) -> float:
        if not self.enable:
            return 0.0
        with self._lock:
            left = self._until.get(key, 0) - time.time()
        return max(0.0, left)

    def snapshot(self) -> dict:
        with self._lock:
            now = time.time()
            inflight = {k: round(v - now, 1) for k, v in self._until.items() if v > now}
        return {"enabled": self.enable, "base": self.base, "cap": self.cap,
                "waiting": inflight}


def image_cool(lo: float = 4.0, hi: float = 10.0) -> float:
    """生图结束后的"看结果"停顿秒数（人类生成完会先看图再发下一条）。"""
    import random
    lo, hi = max(0.0, float(lo or 0)), max(0.0, float(hi or 0))
    if hi <= 0:
        return 0.0
    if hi < lo:
        lo, hi = hi, lo
    return random.uniform(lo, hi)


''' + anchor
src = src.replace(anchor, NEW, 1)

ast.parse(src)
assert src != orig
open(P, "w").write(src)
print("OK guard.py +%d 字符" % (len(src) - len(orig)))
