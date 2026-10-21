#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""guard.py — 调用护栏：养号期禁生图 / 每日配额 / 节奏抖动。

为什么需要这个（2026-10-21 封号复盘）：
  封号的直接诱因是「免费额度被批量消耗」+「单号高频无节奏」。技术上防不住模型侧的判定，
  能做的是把行为特征拉回人类范围：
    1) 新号（首次成功 < N 天）只让纯文本聊天，**不许生图**（生图单价最高、最扎眼）
    2) 每实例每天最多 N 次生成类调用（0=不限）
    3) 同凭证请求之间加**随机抖动**（固定 2s 比 2~3.5s 更像脚本）

设计取舍：
  - 计数按**自然日**（本地时区）滚动，进程重启即清零 —— 宁可少算也不做持久化的复杂度；
    真正的长期统计走 metrics.py（JSONL，按天保留）。
  - `check()` 与 `note()` 分开：调用方先 check 再 note，失败请求也计入配额（防"失败重试"刷量）。

节律三件套（2026-10-22 加）：
  Window   活跃时间窗（窗口外按倍率降速，默认不硬拒）
  Backoff  连续失败的指数退避（每凭证独立）
  image_cool 生图后的"看结果"停顿
"""
import time
from typing import Callable, Optional


class Guard:
    def __init__(self, warmup_days: float = 0, daily_quota: int = 0,
                 age_days: Optional[Callable[[], float]] = None):
        self.warmup_days = float(warmup_days or 0)
        self.daily_quota = int(daily_quota or 0)
        self._age_days = age_days or (lambda: 0.0)
        self._day = ""
        self._count = 0

    # ---------- 计数 ----------
    def _roll(self):
        today = time.strftime("%Y-%m-%d")
        if today != self._day:
            self._day = today
            self._count = 0

    def used(self) -> int:
        self._roll()
        return self._count

    def note(self, kind: str = "chat") -> int:
        """计数。**只对生成类（image）计数**。

        🔴 配额语义是"每实例每天最多 N 次生成"，不是"最多 N 次调用"。
           早先实现无条件 `_count += 1`，导致聊天也吃配额 ——
           养号期天天聊天的号会被自己的配额锁死（实测 check("chat") 在
           quota 用尽后被拒，与"只禁生图"的设计直接矛盾）。
        """
        self._roll()
        if kind == "image":
            self._count += 1
        return self._count

    # ---------- 判定 ----------
    def check(self, kind: str = "chat") -> Optional[dict]:
        """返回 None=放行；否则返回可直接 JSONResponse 的 {status, error}。"""
        self._roll()
        if kind == "image" and self.warmup_days > 0:
            age = 0.0
            try:
                age = float(self._age_days() or 0.0)
            except Exception:
                age = 0.0
            if age < self.warmup_days:
                return {
                    "status": 503,
                    "error": {"message": "该号处于养号期（首次成功 %.1f 天 / 需 %.1f 天），暂不允许生图。"
                                         "先只用纯文本聊天养号，可显著降低封号概率。"
                                         % (age, self.warmup_days),
                              "type": "api_error", "code": "warmup_image_blocked"},
                }
        # 配额只约束生成类；聊天永远不受日配额影响（否则养号期没法养号）
        if kind == "image" and self.daily_quota > 0 and self._count >= self.daily_quota:
            return {
                "status": 429,
                "error": {"message": "本实例今日配额已用尽（%d/%d）。这是主动限频，用于降低封号风险；"
                                     "明天自动恢复，或调大 YB_DAILY_QUOTA。"
                                     % (self._count, self.daily_quota),
                          "type": "rate_limit_error", "code": "daily_quota_exceeded"},
            }
        return None

    def snapshot(self) -> dict:
        self._roll()
        return {"warmup_days": self.warmup_days, "daily_quota": self.daily_quota,
                "used_today": self._count, "day": self._day,
                "account_age_days": round(float(self._age_days() or 0.0), 2)}


class Window:
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


def pace_seconds(min_interval: float, jitter: float) -> float:
    """把"固定最小间隔"变成"最小间隔 + 0~jitter 随机"（调用方再扣掉已流逝时间）。"""
    import random
    base = float(min_interval or 0)
    if jitter and jitter > 0:
        base += random.uniform(0, float(jitter))
    return base
