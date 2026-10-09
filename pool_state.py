#!/usr/bin/env python3
"""pool_state.py — 账号生命周期状态机（号池管理）

状态语义
--------
| state        | 含义                     | 谁改的        | 参与轮询 |
|--------------|--------------------------|---------------|----------|
| `enabled`    | 正常可用                 | 初始/手动/自动恢复 | ✅ |
| `disabled`   | **手动**禁用（临时下线） | 人工          | ❌ |
| `quarantined`| **自动**隔离（健康规则） | 自动          | ❌ |
| `ejected`    | 剔除（不再使用）         | 人工或超期自动 | ❌ |

自动规则（在保活观测里评估）
----------------------------
· 页面被冻结            → 立即 `quarantined`（reason=账号被冻结）
· 保活连续失败 ≥ N 次    → `quarantined`（N = YB_AUTO_DISABLE_AFTER，默认 3）
· 保活连续成功 ≥ M 次    → 若当前是**自动**隔离则自动恢复为 `enabled`（M = YB_AUTO_REENABLE_AFTER，默认 3；0=不自动恢复）
· 隔离超过 D 天         → 自动 `ejected`（D = YB_AUTO_EJECT_AFTER_DAYS，默认 0=关闭）

**手动禁用不会被自动恢复覆盖**（changed_by 记录来源，只有 auto 才自动回滚）——
避免"我明明手动下线了，它自己又活了"。
"""
import json
import os
import threading
import time
from datetime import datetime

STATES = ("enabled", "disabled", "quarantined", "ejected")
LABELS = {"enabled": "启用", "disabled": "手动禁用", "quarantined": "自动隔离", "ejected": "已剔除"}
ACTIONS = ("enable", "disable", "eject", "restore", "reset", "note")


class AccountState:
    def __init__(self, path: str, name: str,
                 auto_disable_after: int = 3, auto_reenable_after: int = 3,
                 auto_eject_after_days: int = 0):
        self.path = path
        self.name = name
        self.auto_disable_after = max(1, int(auto_disable_after))
        self.auto_reenable_after = max(0, int(auto_reenable_after))
        self.auto_eject_after_days = max(0, int(auto_eject_after_days))
        self._lock = threading.Lock()
        self._d = self._load()

    # ---------- 持久化 ----------
    def _default(self):
        return {"name": self.name, "state": "enabled", "reason": "初始状态",
                "changed_at": int(time.time()), "changed_by": "init",
                "fail_streak": 0, "ok_streak": 0, "ever_ok": False,
                "last_health": None, "history": []}

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                d = json.load(f)
            if d.get("state") not in STATES:
                d["state"] = "enabled"
            for k in ("fail_streak", "ok_streak"):
                d.setdefault(k, 0)
            d.setdefault("ever_ok", False)
            # 兼容旧文件：已有成功健康记录 ⇒ 视为曾可用
            if (d.get("last_health") or {}).get("ok"):
                d["ever_ok"] = True
            d.setdefault("history", [])
            return d
        except Exception:
            return self._default()

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._d, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        except Exception as e:
            print("[pool_state] save failed: %s" % e, flush=True)

    def _log(self, action, reason, by):
        self._d["history"].append({
            "at": int(time.time()),
            "iso": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "action": action, "reason": reason, "by": by,
        })
        self._d["history"] = self._d["history"][-200:]

    # ---------- 查询 ----------
    def get(self):
        with self._lock:
            d = json.loads(json.dumps(self._d))
        d["label"] = LABELS.get(d["state"], d["state"])
        d["routable"] = d["state"] == "enabled"
        return d

    # ---------- 手动操作 ----------
    def apply(self, action: str, reason: str = "", by: str = "manual"):
        if action not in ACTIONS:
            return {"ok": False, "error": "未知操作：%s（支持 %s）" % (action, "|".join(ACTIONS))}
        with self._lock:
            if action == "note":
                self._d["notes"] = reason[:400]
            elif action == "enable":
                self._d.update({"state": "enabled", "reason": reason or "手动启用"})
            elif action == "restore":   # 从剔除态恢复
                self._d.update({"state": "enabled", "reason": reason or "手动恢复"})
            elif action == "disable":
                self._d.update({"state": "disabled", "reason": reason or "手动禁用"})
            elif action == "eject":
                self._d.update({"state": "ejected", "reason": reason or "手动剔除"})
            elif action == "reset":     # 清空健康计数并启用
                self._d.update({"state": "enabled", "reason": reason or "手动重置",
                                "fail_streak": 0, "ok_streak": 0})
            self._d["changed_at"] = int(time.time())
            self._d["changed_by"] = by
            if action != "note":
                self._log(action, self._d.get("reason", ""), by)
            self._save()
        return {"ok": True, "state": self.get()}

    # ---------- 自动规则 ----------
    def record_health(self, ok: bool, status=None, detail: str = "", frozen: bool = False):
        """保活观测 → 自动隔离 / 自动恢复 / 超期剔除。返回 (当前状态字典, 本次自动动作)。"""
        act = None
        with self._lock:
            self._d["last_health"] = {"at": int(time.time()), "ok": bool(ok), "status": status,
                                      "detail": detail[:200], "frozen": bool(frozen)}
            if ok:
                self._d["ever_ok"] = True
            elif not self._d.get("ever_ok"):
                # 🔴 从未成功过（＝还没扫码登录过的新实例）不计失败、不自动隔离。
                # 否则刚 add-account 出来的号会在 3 个保活周期后被误隔离，反而更难上手。
                self._save()
                d = json.loads(json.dumps(self._d))
                d["label"] = LABELS.get(d["state"], d["state"])
                d["routable"] = d["state"] == "enabled"
                return d, None
            if ok:
                self._d["ok_streak"] = self._d.get("ok_streak", 0) + 1
                self._d["fail_streak"] = 0
            else:
                self._d["fail_streak"] = self._d.get("fail_streak", 0) + 1
                self._d["ok_streak"] = 0

            st = self._d["state"]
            # 1) 冻结 → 立即自动隔离
            if frozen and st in ("enabled", "quarantined"):
                self._d.update({"state": "quarantined", "reason": "账号被冻结（自动隔离）",
                                "changed_at": int(time.time()), "changed_by": "auto"})
                self._log("quarantine", "账号被冻结", "auto")
                act = "quarantine:frozen"
            # 2) 连续失败达阈值 → 自动隔离（只动 enabled，不覆盖手动 disabled/ejected）
            elif (not ok and st == "enabled"
                  and self._d["fail_streak"] >= self.auto_disable_after):
                self._d.update({"state": "quarantined",
                                "reason": "保活连续失败 %d 次（自动隔离）" % self._d["fail_streak"],
                                "changed_at": int(time.time()), "changed_by": "auto"})
                self._log("quarantine", self._d["reason"], "auto")
                act = "quarantine:unhealthy"
            # 3) 连续成功达阈值 → 自动恢复（仅回滚"自动"造成的隔离）
            elif (ok and st == "quarantined" and self._d.get("changed_by") == "auto"
                  and self.auto_reenable_after > 0
                  and self._d["ok_streak"] >= self.auto_reenable_after):
                self._d.update({"state": "enabled",
                                "reason": "连续保活成功 %d 次，自动恢复" % self._d["ok_streak"],
                                "changed_at": int(time.time()), "changed_by": "auto"})
                self._log("enable", self._d["reason"], "auto")
                act = "enable:recovered"

            # 4) 隔离超期 → 自动剔除
            if (self.auto_eject_after_days > 0 and self._d["state"] == "quarantined"
                    and time.time() - self._d.get("changed_at", 0) > self.auto_eject_after_days * 86400):
                self._d.update({"state": "ejected",
                                "reason": "隔离超过 %d 天未恢复（自动剔除）" % self.auto_eject_after_days,
                                "changed_at": int(time.time()), "changed_by": "auto"})
                self._log("eject", self._d["reason"], "auto")
                act = "eject:expired"

            self._save()
            d = json.loads(json.dumps(self._d))
        d["label"] = LABELS.get(d["state"], d["state"])
        d["routable"] = d["state"] == "enabled"
        return d, act
