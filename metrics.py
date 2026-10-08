#!/usr/bin/env python3
"""metrics.py — 调用统计与明细日志（账号 × 模型 × 端点）

设计要点
--------
1. **真源 = 追加式 JSONL**（一行一次调用）——「明细日志」和导出直接读它，无需额外存储。
2. **内存聚合 = 启动回放 + 增量更新** —— `/admin/stats` 查询 O(1)，不去扫文件。
3. **每条记录带 `instance`（= 账号名）** —— 所以「每个号 × 每个模型调用多少次」天然可算。
4. **按天保留 N 天**，超期在启动时与每日首次写入时清理（重写文件，避免无限膨胀）。

并发：单 worker 进程内用 `threading.Lock`；追加写单行 `write()` 到 O_APPEND 文件，
POSIX 下小于 PIPE_BUF 的写入是原子的，故多线程不会串行。
"""
import json
import os
import threading
import time
from datetime import datetime, timedelta


def _today(ts: float = None) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts or time.time()))


class Metrics:
    def __init__(self, path: str, keep_days: int = 30):
        self.path = path
        self.keep_days = max(1, int(keep_days))
        self._lock = threading.Lock()
        self._agg = self._empty()
        self._pruned_day = None
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        except Exception:
            pass
        self._replay()

    # ---------- 聚合骨架 ----------
    @staticmethod
    def _empty():
        return {
            "total": 0, "ok": 0, "err": 0,
            "by_model": {},      # model -> {n, ok, err, ms_sum, ms_max, pt, ct}
            "by_endpoint": {},   # endpoint -> {n, ok, err}
            "by_status": {},     # "200" -> n
            "by_instance": {},   # instance -> {n, ok, err}
            "by_day": {},        # "YYYY-MM-DD" -> {n, ok, err, models: {model: n}}
            "first_ts": None, "last_ts": None,
        }

    def _apply(self, a: dict, e: dict):
        day = e.get("date") or _today(e.get("ts"))
        model = e.get("model") or "?"
        inst = e.get("instance") or "-"
        ep = e.get("endpoint") or "-"
        ok = bool(e.get("ok"))
        ms = int(e.get("ms") or 0)
        u = e.get("usage") or {}

        a["total"] += 1
        a["ok" if ok else "err"] += 1
        a["first_ts"] = e.get("ts") if a["first_ts"] is None else min(a["first_ts"], e.get("ts") or 0)
        a["last_ts"] = e.get("ts") if a["last_ts"] is None else max(a["last_ts"], e.get("ts") or 0)

        m = a["by_model"].setdefault(model, {"n": 0, "ok": 0, "err": 0, "ms_sum": 0, "ms_max": 0, "pt": 0, "ct": 0})
        m["n"] += 1
        m["ok" if ok else "err"] += 1
        m["ms_sum"] += ms
        m["ms_max"] = max(m["ms_max"], ms)
        m["pt"] += int(u.get("prompt_tokens") or 0)
        m["ct"] += int(u.get("completion_tokens") or 0)

        p = a["by_endpoint"].setdefault(ep, {"n": 0, "ok": 0, "err": 0})
        p["n"] += 1
        p["ok" if ok else "err"] += 1

        a["by_status"][str(e.get("status"))] = a["by_status"].get(str(e.get("status")), 0) + 1

        i = a["by_instance"].setdefault(inst, {"n": 0, "ok": 0, "err": 0})
        i["n"] += 1
        i["ok" if ok else "err"] += 1

        d = a["by_day"].setdefault(day, {"n": 0, "ok": 0, "err": 0, "models": {}})
        d["n"] += 1
        d["ok" if ok else "err"] += 1
        d["models"][model] = d["models"].get(model, 0) + 1

    # ---------- 回放 / 清理 ----------
    def _replay(self):
        if not os.path.exists(self.path):
            return
        cutoff = (datetime.now() - timedelta(days=self.keep_days)).timestamp()
        keep, dropped = [], 0
        try:
            with open(self.path, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        e = json.loads(line)
                    except Exception:
                        continue
                    if (e.get("ts") or 0) < cutoff:
                        dropped += 1
                        continue
                    keep.append(line)
                    self._apply(self._agg, e)
        except Exception:
            return
        if dropped:                      # 有超期记录 → 重写文件
            try:
                with open(self.path, "w", encoding="utf-8") as f:
                    f.write("\n".join(keep) + ("\n" if keep else ""))
                print("[metrics] 清理 %d 条超期记录（保留 %d 天）" % (dropped, self.keep_days), flush=True)
            except Exception:
                pass

    # ---------- 写入 ----------
    def record(self, e: dict):
        e = dict(e)
        e.setdefault("ts", int(time.time()))
        e.setdefault("date", _today(e["ts"]))
        e.setdefault("ts_iso", datetime.fromtimestamp(e["ts"]).strftime("%Y-%m-%d %H:%M:%S"))
        line = json.dumps(e, ensure_ascii=False)
        with self._lock:
            self._apply(self._agg, e)
            try:
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except Exception:
                pass
            # 每天首次写入顺带清理一次（长期运行也不会无限长）
            if self._pruned_day != e["date"]:
                self._pruned_day = e["date"]
                threading.Thread(target=self._replay, daemon=True).start()
        return e

    # ---------- 查询 ----------
    def summary(self, days: int = 7):
        days = max(1, min(int(days), self.keep_days))
        with self._lock:
            a = json.loads(json.dumps(self._agg))   # 快照，避免外部改动
        keep_days_set = {(datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days)}
        by_day = {k: v for k, v in a["by_day"].items() if k in keep_days_set}
        tot = sum(v["n"] for v in by_day.values())
        ok = sum(v["ok"] for v in by_day.values())
        err = sum(v["err"] for v in by_day.values())
        # 窗口内 模型/实例 计数
        win_model, win_inst = {}, {}
        for v in by_day.values():
            for mname, n in (v.get("models") or {}).items():
                win_model[mname] = win_model.get(mname, 0) + n
        models = []
        for mname, n in sorted(win_model.items(), key=lambda kv: -kv[1]):
            allm = a["by_model"].get(mname, {})
            models.append({
                "model": mname, "n": n,
                "ok": allm.get("ok", 0), "err": allm.get("err", 0),
                "avg_ms": int(allm.get("ms_sum", 0) / allm["n"]) if allm.get("n") else 0,
                "max_ms": allm.get("ms_max", 0),
                "prompt_tokens": allm.get("pt", 0), "completion_tokens": allm.get("ct", 0),
            })
        series = []
        for i in range(days - 1, -1, -1):
            d = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
            v = by_day.get(d) or {"n": 0, "ok": 0, "err": 0}
            series.append({"date": d, "n": v["n"], "ok": v["ok"], "err": v["err"]})
        return {
            "window_days": days,
            "all_time": {"total": a["total"], "ok": a["ok"], "err": a["err"],
                         "first_ts": a["first_ts"], "last_ts": a["last_ts"]},
            "window": {"total": tot, "ok": ok, "err": err},
            "by_model": models,
            "by_endpoint": a["by_endpoint"],
            "by_instance": a["by_instance"],
            "by_status": a["by_status"],
            "series": series,
        }

    def detail(self, limit: int = 100, model: str = None, ok: str = None,
               endpoint: str = None, instance: str = None):
        """读文件尾部若干行再过滤（明细日志，最近的在前）。"""
        limit = max(1, min(int(limit), 2000))
        rows = []
        if not os.path.exists(self.path):
            return rows
        need = limit * (20 if (model or ok or endpoint or instance) else 1)
        with open(self.path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            block = min(size, 4 * 1024 * 1024)
            f.seek(size - block)
            data = f.read().decode("utf-8", "ignore")
        for line in reversed(data.splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except Exception:
                continue
            if model and e.get("model") != model:
                continue
            if ok in ("true", "false") and str(bool(e.get("ok"))).lower() != ok:
                continue
            if endpoint and e.get("endpoint") != endpoint:
                continue
            if instance and e.get("instance") != instance:
                continue
            rows.append(e)
            if len(rows) >= need:
                break
        return rows[:limit]

    def prune(self):
        """手动清理超期记录，返回清理条数。"""
        before = self._agg["total"]
        self._agg = self._empty()
        self._replay()
        return {"before": before, "after": self._agg["total"]}
