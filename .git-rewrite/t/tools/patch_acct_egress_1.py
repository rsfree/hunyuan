#!/usr/bin/env python3
"""补丁：账号级出口（一号一固定 IP）。

现状：`YB_PROXY_URL` 是容器级 ⇒ 一个实例只有一个出口，组内 N 个 cookie 账号全从同一 IP 出去
（多号同 IP = 强关联信号）。实测代理池 `:2090` 的会话键「用户名裸后缀」可任意指定，
11 个键拿到 11 个互不相同且粘性的出口 ⇒ 可以把出口下沉到账号级。

本补丁只做「能力」：
  · 账号记录加 `egress` 字段（会话键，如 acct03），add 时自动挑用得最少的槽
  · 出站 `_open` / `_open_stream` 优先用当前账号的出口（contextvar 传递，零侵入调用点）
  · `_check_auth` 认出 cookie 串时就把该账号的出口放进 contextvar
  · 管理面 `/admin/group` 展示与改绑

默认**不改变行为**：没配 egress 的账号继续用实例默认出口。

🔴 有账号级出口时**不换 IP 重试**：换出口等于换身份，比失败更糟。
"""
import ast

# ---------------- accounts.py ----------------
P1 = "accounts.py"
s1 = open(P1).read()
o1 = s1

a = '''    def add(self, name: str, cookie: str, note: str = "") -> dict:
        name = (name or "").strip()
        cookie = (cookie or "").strip()
        if not NAME_RE.match(name):
            return {"ok": False, "error": "名字只允许字母数字下划线中划线（≤24 字符）"}
        if "hy_user=" not in cookie + ";":
            return {"ok": False, "error": "cookie 里没有 hy_user（不是已登录状态？）"}
        with self._lock:
            exists = os.path.exists(self._f(name))
            self._write(self._f(name), {"name": name, "cookie": cookie, "note": note,
                                        "added_at": int(time.time())})'''
b = '''    def add(self, name: str, cookie: str, note: str = "", egress: str = "") -> dict:
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
                                        "added_at": int(time.time())})'''
assert s1.count(a) == 1, "accounts.add 锚点不唯一"
s1 = s1.replace(a, b, 1)

# list() 带出 egress
a = '''            out.append({"name": nm, "note": d.get("note", ""), "added_at": d.get("added_at", 0),
                        "cookie_len": len(d.get("cookie", "")), "state": st,'''
b = '''            out.append({"name": nm, "note": d.get("note", ""), "added_at": d.get("added_at", 0),
                        "egress": d.get("egress", ""),
                        "cookie_len": len(d.get("cookie", "")), "state": st,'''
assert s1.count(a) == 1, "accounts.list 锚点不唯一"
s1 = s1.replace(a, b, 1)

# 新增 _pick_egress / set_egress / by_user_map
anchor = '''    @staticmethod
    def hy_user_of(cookie: str) -> str:'''
assert s1.count(anchor) == 1
NEW = '''    # ---------- 出口（一号一固定 IP） ----------
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

''' + anchor
s1 = s1.replace(anchor, NEW, 1)

ast.parse(s1)
assert s1 != o1
open(P1, "w").write(s1)
print("OK accounts.py +%d 字符" % (len(s1) - len(o1)))
