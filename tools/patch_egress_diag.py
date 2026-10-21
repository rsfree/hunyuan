#!/usr/bin/env python3
"""补丁：`/admin/egress?account=<name>` —— 实测某个 cookie 账号当前的出口 IP。

没有这个端点就只能"看配置猜链路"：`/admin/group` 只能证明槽位拼得出来（url_ok），
证明不了请求真的从那个 IP 发出。运维时查"某号现在什么出口"必须能实测。
"""
import ast

P = "yuanbao_openai_proxy.py"
src = open(P).read()
orig = src

a = '''    out = {"egress_mode": ("固定单出口" if len(pool) <= 1 else "池(%d)" % len(pool)),
           "proxy": _mask(pool[0] if len(pool) == 1 else (YB_PROXY_POOL or "")),
           "browser_proxy": _mask(YB_BROWSER_PROXY),
           "data_ip": None, "browser_ip": None, "match": None}

    try:
        rq = _ureq.Request("https://api.ipify.org")
        with _open(rq, timeout=25) as r:
            out["data_ip"] = r.read().decode("utf-8", "replace").strip()
    except Exception as e:
        out["data_ip"] = "ERR: %s" % str(e)[:120]'''
b = '''    out = {"egress_mode": ("固定单出口" if len(pool) <= 1 else "池(%d)" % len(pool)),
           "proxy": _mask(pool[0] if len(pool) == 1 else (YB_PROXY_POOL or "")),
           "browser_proxy": _mask(YB_BROWSER_PROXY),
           "data_ip": None, "browser_ip": None, "match": None}

    # 可选：?account=<组内账号名> —— 用该账号绑定的出口实测（验证"一号一 IP"真的生效）
    _acct = (req.query_params.get("account") or "").strip()
    _tok = None
    if _acct:
        _slot = ""
        if COOKIE_ACCOUNTS is not None:
            _slot = ((COOKIE_ACCOUNTS.get(_acct) or {}).get("egress") or "").strip()
        out.update({"account": _acct, "account_egress": _slot,
                    "account_proxy": _mask(_egress_url(_slot)) if _slot else "",
                    "account_mode": ("账号级出口" if _slot else "实例默认出口")})
        if _slot:
            _tok = _CUR_EGRESS.set(_slot)
    try:
        rq = _ureq.Request("https://api.ipify.org")
        with _open(rq, timeout=25) as r:
            out["data_ip"] = r.read().decode("utf-8", "replace").strip()
    except Exception as e:
        out["data_ip"] = "ERR: %s" % str(e)[:120]
    finally:
        if _tok is not None:
            _CUR_EGRESS.reset(_tok)'''
assert src.count(a) == 1, "/admin/egress 测速块锚点不唯一"
src = src.replace(a, b, 1)

ast.parse(src)
assert src != orig
open(P, "w").write(src)
print("OK +%d 字符" % (len(src) - len(orig)))
