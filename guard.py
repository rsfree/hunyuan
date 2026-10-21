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

    def note(self) -> int:
        self._roll()
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
        if self.daily_quota > 0 and self._count >= self.daily_quota:
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


def pace_seconds(min_interval: float, jitter: float) -> float:
    """把"固定最小间隔"变成"最小间隔 + 0~jitter 随机"（调用方再扣掉已流逝时间）。"""
    import random
    base = float(min_interval or 0)
    if jitter and jitter > 0:
        base += random.uniform(0, float(jitter))
    return base
