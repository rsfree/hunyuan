#!/usr/bin/env python3
"""补丁 2/2：主服务支持账号级出口（一号一固定 IP）。"""
import ast

P = "yuanbao_openai_proxy.py"
src = open(P).read()
orig = src

# ---- 1) import contextvars ----
a = "import threading\nfrom typing import Optional"
b = "import threading\nimport contextvars\nfrom typing import Optional"
assert src.count(a) == 1, "import 锚点不唯一"
src = src.replace(a, b, 1)

# ---- 2) 配置 + contextvar + 出口 URL 构造函数（放在 _proxy_list 之前） ----
a = "def _proxy_list():"
assert src.count(a) == 1
NEW = '''# ---------------------------------------------------------------- 账号级出口
# 默认空=不启用（沿用实例级 YB_PROXY_URL）。非空时按账号分出口，见 accounts.py 的 egress。
YB_EGRESS_USER_PREFIX = os.environ.get("YB_EGRESS_USER_PREFIX", "")   # 默认从 YB_PROXY_URL 推断
_CUR_EGRESS = contextvars.ContextVar("yb_cur_egress", default="")


def _egress_url(slot: str) -> str:
    """会话槽 → 代理 URL。`slot` 形如 acct03，拼成 `socks5h://poolclient-acct03:pw@host:port`。

    会话键只换**用户名裸后缀**（实测 `poolclient-session-xxx` 会被 SOCKS5 鉴权拒）。
    不是 socks5 形态或拿不到密码时返回空 ⇒ 调用方退回实例默认出口。
    """
    u = (YB_PROXY_URL or "").strip()
    if not u or not slot:
        return ""
    m = re.match(r"^(socks5h?)://([^:@/]+):([^@]*)@(.+)$", u)
    if not m:
        return ""
    scheme, user, pw, hostport = m.group(1), m.group(2), m.group(3), m.group(4)
    prefix = YB_EGRESS_USER_PREFIX or (user.split("-", 1)[0] + "-" if "-" in user else "")
    if not prefix:
        return ""
    return "%s://%s%s:%s@%s" % (scheme, prefix, slot, pw, hostport)


def _egress_of_cookie(cookie: str) -> str:
    """cookie 串 → 该账号绑定的会话槽（没绑定/查不到返回空）。"""
    if not cookie or COOKIE_ACCOUNTS is None:
        return ""
    try:
        hu = _acc.CookieAccounts.hy_user_of(cookie)
        if not hu:
            return ""
        return ((COOKIE_ACCOUNTS.by_user_map().get(hu) or {}).get("egress") or "").strip()
    except Exception:
        return ""


def _set_egress_from_cookie(cookie: str):
    """把当前请求的出口写进 contextvar —— `_open` 零侵入地跟着账号走。"""
    try:
        _CUR_EGRESS.set(_egress_of_cookie(cookie) or "")
    except Exception:
        pass


def _proxy_list():'''
src = src.replace(a, NEW, 1)

# ---- 3) _open 优先用账号出口 ----
a = '''    order = []
    pool = _proxy_list()
    n = len(pool) or 1
    for _ in range(n + 1):          # 池内每个代理各试一次 + 直连兜底
        pr = _proxy_next()'''
b = '''    order = []
    _cur = _CUR_EGRESS.get()
    if _cur:
        pr_one = _egress_url(_cur)
        if pr_one:
            # 🔴 账号级出口**不做故障转移**：换出口＝换身份，比这次失败更糟。
            fresh = _ureq.Request(req.full_url, data=getattr(req, "data", None),
                                  headers=dict(req.headers), method=req.get_method())
            if pr_one.split("://", 1)[0].lower().startswith("socks"):
                return _open_socks(fresh, pr_one, timeout)
            handler = _ureq.ProxyHandler({"http": pr_one, "https": pr_one})
            return _ureq.build_opener(handler).open(fresh, timeout=timeout)
    pool = _proxy_list()
    n = len(pool) or 1
    for _ in range(n + 1):          # 池内每个代理各试一次 + 直连兜底
        pr = _proxy_next()'''
assert src.count(a) == 1, "_open 锚点不唯一"
src = src.replace(a, b, 1)

# ---- 4) _open_stream 同上 ----
a = '''    order = []
    pool = _proxy_list()
    n = len(pool) or 1
    for _ in range(n + 1):
        pr = _proxy_next()
        order.append(pr or "(直连)")
        fresh = _ureq.Request(req.full_url, data=getattr(req, "data", None),
                              headers=dict(req.headers), method=req.get_method())
        try:
            if pr and pr.split("://", 1)[0].lower().startswith("socks"):
                return _open_socks_stream(fresh, pr, timeout)'''
b = '''    order = []
    _cur = _CUR_EGRESS.get()
    if _cur:
        pr_one = _egress_url(_cur)
        if pr_one:
            fresh = _ureq.Request(req.full_url, data=getattr(req, "data", None),
                                  headers=dict(req.headers), method=req.get_method())
            return _open_socks_stream(fresh, pr_one, timeout)
    pool = _proxy_list()
    n = len(pool) or 1
    for _ in range(n + 1):
        pr = _proxy_next()
        order.append(pr or "(直连)")
        fresh = _ureq.Request(req.full_url, data=getattr(req, "data", None),
                              headers=dict(req.headers), method=req.get_method())
        try:
            if pr and pr.split("://", 1)[0].lower().startswith("socks"):
                return _open_socks_stream(fresh, pr, timeout)'''
assert src.count(a) == 1, "_open_stream 锚点不唯一"
src = src.replace(a, b, 1)

# ---- 5) _check_auth 认出 cookie 时绑定出口 ----
a = '''    yb_cookie = request.headers.get("x-yuanbao-cookie", "").strip()
    if yb_cookie:
        return ("cookie", yb_cookie)'''
b = '''    yb_cookie = request.headers.get("x-yuanbao-cookie", "").strip()
    if yb_cookie:
        _set_egress_from_cookie(yb_cookie)
        return ("cookie", yb_cookie)'''
assert src.count(a) == 1, "x-yuanbao-cookie 锚点不唯一"
src = src.replace(a, b, 1)

a = '''    if value and _looks_like_cookie(value):
        return ("cookie", value)'''
b = '''    if value and _looks_like_cookie(value):
        _set_egress_from_cookie(value)
        return ("cookie", value)'''
assert src.count(a) == 1, "_looks_like_cookie 锚点不唯一"
src = src.replace(a, b, 1)

# ---- 6) /admin/group 带出出口分布 ----
a = '''    return {"instance": YB_INSTANCE_NAME_ENV, "accounts": COOKIE_ACCOUNTS.list(),
            "note": "组内账号共用本实例浏览器的设备指纹；跨组隔离靠部署多个实例"}'''
b = '''    _accs = COOKIE_ACCOUNTS.list()
    for _it in _accs:                      # 让页面/脚本能直接看到"这个号从哪个 IP 出去"
        _it["egress_url_ok"] = bool(_egress_url(_it.get("egress") or ""))
    return {"instance": YB_INSTANCE_NAME_ENV, "accounts": _accs,
            "egress_slots": COOKIE_ACCOUNTS.EGRESS_SLOTS,
            "egress_mode": "按账号分出口" if YB_EGRESS_USER_PREFIX or _egress_url(
                COOKIE_ACCOUNTS.EGRESS_PREFIX + "01") else "实例默认出口",
            "note": "组内账号共用本实例浏览器的设备指纹；出口 IP 可按账号绑定（egress 字段）"}'''
assert src.count(a) == 1, "/admin/group 返回锚点不唯一"
src = src.replace(a, b, 1)

# ---- 7) POST /admin/group 支持 action=egress ----
a = '''    action = (body.get("action") or "").strip()
    name = (body.get("name") or "").strip()
    note = (body.get("note") or "").strip()
'''
b = '''    action = (body.get("action") or "").strip()
    name = (body.get("name") or "").strip()
    note = (body.get("note") or "").strip()

    if action == "egress":
        # 改绑出口槽：egress="" 表示改回实例默认出口；"auto" 表示重新均匀分配
        slot = (body.get("egress") or "").strip()
        if slot == "auto":
            slot = COOKIE_ACCOUNTS._pick_egress()
        r = COOKIE_ACCOUNTS.set_egress(name, slot)
        if not r.get("ok"):
            return JSONResponse({"error": r.get("error")}, status_code=400)
        r["url_ok"] = bool(_egress_url(r.get("egress") or ""))
        return r
'''
assert src.count(a) == 1, "admin_group_op 入参锚点不唯一"
src = src.replace(a, b, 1)

ast.parse(src)
assert src != orig
open(P, "w").write(src)
print("OK 主服务 +%d 字符" % (len(src) - len(orig)))
