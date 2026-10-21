#!/usr/bin/env python3
"""accounts.py — 「分组共享」模式下的账号库（cookie 账号）

设计背景（C 方案：分组共享）
--------------------------
实测证明 **签名三件套只绑设备、不绑账号**：一个 Chromium 铸的签名，配任意账号的 cookie 都能调通。
所以不需要"一个账号养一个浏览器"。改成：

    一个实例 = 一个**组**：组内 1 个 Chromium 只负责**铸签**，
    组内 N 个账号各自只保留 **cookie**（保活 / 无水印 / 请求都走纯 HTTP + cookie，不用浏览器）。

隔离粒度 = 组（每个组一台独立设备指纹），组内号共用指纹。
⇒ 3 组 9 个号 = 3 个浏览器（≈1GB）而不是 9 个（≈3GB）；
   且一组出事不牵连其他组。

存储：`auths/<实例>/accounts/<名字>.json`（含 cookie），
      `auths/<实例>/accounts/<名字>.state.json`（复用 pool_state 的状态机）。
"""
import json
import os
import re
import threading
import time

NAME_RE = re.compile(r"^[A-Za-z0-9_\-]{1,24}$")


class CookieAccounts:
    def __init__(self, dirpath: str, auto_disable_after: int = 3,
                 auto_reenable_after: int = 1, auto_eject_after_days: int = 0):
        self.dir = dirpath
        self.auto_disable_after = auto_disable_after
        self.auto_reenable_after = auto_reenable_after
        self.auto_eject_after_days = auto_eject_after_days
        self._lock = threading.Lock()
        try:
            os.makedirs(self.dir, exist_ok=True)
        except Exception:
            pass

    # ---------- 基础 ----------
    def _f(self, name: str) -> str:
        return os.path.join(self.dir, name + ".json")

    def _sf(self, name: str) -> str:
        return os.path.join(self.dir, name + ".state.json")

    def _write(self, path: str, obj: dict):
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)
        try:
            os.chmod(tmp, 0o600)          # cookie 是敏感凭证
        except Exception:
            pass
        os.replace(tmp, path)

    # ---------- CRUD ----------
    def add(self, name: str, cookie: str, note: str = "", egress: str = "") -> dict:
        name = (name or "").strip()
        cookie = (cookie or "").strip()
        if not NAME_RE.match(name):
            return {"ok": False, "error": "名字只允许字母数字下划线中划线（≤24 字符）"}
        if "hy_user=" not in cookie + ";":
            return {"ok": False, "error": "cookie 里没有 hy_user（不是已登录状态？）"}
        with self._lock:
            exists = os.path.exists(self._f(name))
            old = {}
            if exists:
                try:
                    old = json.load(open(self._f(name), encoding="utf-8")) or {}
                except Exception:
                    old = {}
            # 没显式给出口就沿用原绑定；新号才自动分槽（换 cookie 不该换 IP）
            if not egress:
                egress = (old.get("egress") or "").strip() or self._pick_egress()
            self._write(self._f(name), {"name": name, "cookie": cookie, "note": note,
                                        "egress": egress,
                                        "added_at": int(time.time())})
            # 新账号：清掉旧健康结论，当作"待验证"
            st = self._state(name)
            if not exists:
                st.apply("reset", "新账号", by="manual")
        return {"ok": True, "name": name, "replaced": exists, "cookie_len": len(cookie)}

    def remove(self, name: str) -> dict:
        if not NAME_RE.match(name or ""):
            return {"ok": False, "error": "非法名字"}
        with self._lock:
            n = 0
            for p in (self._f(name), self._sf(name)):
                if os.path.exists(p):
                    os.remove(p)
                    n += 1
        return {"ok": n > 0, "removed": n}

    def list(self):
        out = []
        try:
            names = sorted(x[:-5] for x in os.listdir(self.dir)
                           if x.endswith(".json") and not x.endswith(".state.json"))
        except Exception:
            names = []
        for nm in names:
            try:
                with open(self._f(nm), "r", encoding="utf-8") as f:
                    d = json.load(f)
            except Exception:
                continue
            st = self._state(nm).get()
            out.append({"name": nm, "note": d.get("note", ""), "added_at": d.get("added_at", 0),
                        "egress": d.get("egress", ""),
                        "cookie_len": len(d.get("cookie", "")), "state": st,
                        "routable": bool(st.get("routable")) and (st.get("last_health") or {}).get("ok") is True})
        return out

    # ---------- 出口（一号一固定 IP） ----------
    EGRESS_SLOTS = 12              # 可用会话槽数，与代理池侧一致
    EGRESS_PREFIX = "acct"         # acct01 … acct12

    def _pick_egress(self) -> str:
        """挑一个**当前账号数最少**的会话槽 —— 均匀摊到不同出口 IP。

        为什么按"数量最少"而不是纯轮询：账号会被删/会被替换，纯轮询会出现
        "3 个号挤在两个槽、另一个槽空着"的偏斜。
        """
        used = {}
        for item in self.list():
            e = (item.get("egress") or "").strip()
            if e:
                used[e] = used.get(e, 0) + 1
        slots = ["%s%02d" % (self.EGRESS_PREFIX, i) for i in range(1, self.EGRESS_SLOTS + 1)]
        return min(slots, key=lambda s: (used.get(s, 0), s))

    def set_egress(self, name: str, egress: str) -> dict:
        """改绑某个账号的出口槽（egress="" 表示改回实例默认出口）。"""
        if not NAME_RE.match(name or ""):
            return {"ok": False, "error": "非法名字"}
        with self._lock:
            d = self.get(name)
            if not d:
                return {"ok": False, "error": "账号不存在"}
            d["egress"] = (egress or "").strip()
            self._write(self._f(name), d)
        self._umap_at = 0            # 让反查缓存立刻失效
        return {"ok": True, "name": name, "egress": d["egress"]}

    def by_user_map(self) -> dict:
        """{hy_user: {"name":…, "egress":…}}，带 5s 缓存。

        为什么要缓存：路由时每个请求都要"从 cookie 串反查账号属于哪个出口"，
        逐个开文件在号多时会成为热点。5s 的陈旧代价可接受（改绑不是高频操作）。
        """
        now = time.time()
        if getattr(self, "_umap_at", 0) and now - self._umap_at < 5:
            return getattr(self, "_umap", {})
        m = {}
        for item in self.list():
            d = self.get(item["name"]) or {}
            hu = self.hy_user_of(d.get("cookie", ""))
            if hu:
                m[hu] = {"name": item["name"], "egress": (d.get("egress") or "").strip()}
        self._umap, self._umap_at = m, now
        return m

    @staticmethod
    def hy_user_of(cookie: str) -> str:
        """从 cookie 串里取 hy_user（账号唯一标识）。取不到返回空串。"""
        m = re.search(r"(?:^|;\s*)hy_user=([^;]+)", cookie or "")
        return (m.group(1).strip() if m else "")

    def find_by_user(self, hy_user: str):
        """按 hy_user 找已有账号名 —— 同一号换了 cookie 时用它做"替换"而不是"新增"。"""
        if not hy_user:
            return None
        try:
            names = [x[:-5] for x in os.listdir(self.dir)
                     if x.endswith(".json") and not x.endswith(".state.json")]
        except Exception:
            return None
        for nm in names:
            d = self.get(nm)
            if d and self.hy_user_of(d.get("cookie")) == hy_user:
                return nm
        return None

    def get(self, name: str):
        if not NAME_RE.match(name or ""):
            return None
        try:
            with open(self._f(name), "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    def cookie(self, name: str):
        d = self.get(name)
        return (d or {}).get("cookie") or None

    # ---------- 状态机（复用 pool_state，规则一致） ----------
    def _state(self, name: str):
        import pool_state
        return pool_state.AccountState(self._sf(name), name,
                                       self.auto_disable_after, self.auto_reenable_after,
                                       self.auto_eject_after_days)

    def state(self, name: str) -> dict:
        return self._state(name).get()

    def record_health(self, name: str, ok: bool, status=None, detail: str = "",
                      frozen: bool = False, count_failure: bool = True):
        return self._state(name).record_health(ok, status, detail, frozen, count_failure)
