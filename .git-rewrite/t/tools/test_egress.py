#!/usr/bin/env python3
"""账号级出口（一号一固定 IP）单测。

关键断言：
  E1 会话槽能拼成正确代理 URL（保留密码与 host:port，只换用户名后缀）
  E2 新号自动挑"用得最少"的槽（均匀摊开，删号后仍不偏斜）
  E3 换 cookie 不换 IP（同号重登沿用原绑定）
  E4 cookie 串 → 出口反查（路由时靠它）
  E5 有账号级出口时，`_open` 只走那一个出口、且**不换 IP 重试**
  E6 非 socks 形态 / 没配代理时优雅退回实例默认出口（不崩、不乱拼）
"""
import importlib.util
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

PASS = FAIL = 0


def ok(cond, name, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  PASS %s %s" % (name, extra))
    else:
        FAIL += 1
        print("  FAIL %s %s" % (name, extra))


def load(env=None):
    os.environ.update(env or {})
    spec = importlib.util.spec_from_file_location("ybp_eg", os.path.join(ROOT, "yuanbao_openai_proxy.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


TMP = tempfile.mkdtemp(prefix="yb-egress-")
ybp = load({
    "YB_PROXY_URL": "socks5h://poolclient-acc01:s3cretPw@pool.example.com:2090",
    "YB_ACCOUNTS_DIR": TMP,
})
import accounts as _acc  # noqa: E402

# ---------- E1 ----------
u = ybp._egress_url("acct03")
ok(u == "socks5h://poolclient-acct03:s3cretPw@pool.example.com:2090", "E1 槽位拼 URL", u)
ok(ybp._egress_url("") == "", "E1 空槽返回空")
ok(ybp._egress_url("acct01").endswith(":2090"), "E1 端口保留")

# 非 socks 形态不启用（不能把 http 代理瞎拼）
ybp.YB_PROXY_URL = "http://127.0.0.1:7890"
ok(ybp._egress_url("acct03") == "", "E6 http 代理不拼会话键")
ybp.YB_PROXY_URL = "socks5h://poolclient-acc01:s3cretPw@pool.example.com:2090"

# ---------- E2 均摊 ----------
ca = _acc.CookieAccounts(TMP)
ybp.COOKIE_ACCOUNTS = ca
slots = []
for i in range(1, 4):
    nm = "u%d" % i
    ca.add(nm, "hy_source=web; hy_user=U%d; x=1" % i, "")
    slots.append((ca.get(nm) or {}).get("egress"))
ok(slots == ["acct01", "acct02", "acct03"], "E2 连续加号摊到不同槽", slots)

# 删掉中间的号，再加一个应当补空槽而不是接着往后排
ca.remove("u2")
ca.add("u4", "hy_source=web; hy_user=U4; x=1", "")
e4 = (ca.get("u4") or {}).get("egress")
ok(e4 == "acct02", "E2 删号后补空洞不偏斜", e4)

# ---------- E3 换 cookie 不换 IP ----------
ca.add("u1", "hy_source=web; hy_user=U1; x=2", "")   # 同号换 token
ok((ca.get("u1") or {}).get("egress") == "acct01", "E3 同号换 cookie 沿用原槽")

# ---------- E4 反查 ----------
m = ca.by_user_map()
ok(m.get("U1", {}).get("egress") == "acct01", "E4 by_user_map 命中", str(m.get("U1")))
ok(ybp._egress_of_cookie("hy_source=web; hy_user=U4; x=1") == "acct02", "E4 cookie 反查出口")
ok(ybp._egress_of_cookie("hy_source=web; hy_user=NOPE") == "", "E4 未知号返回空")

# ---------- E5 单出口、不换 IP ----------
calls = []
ybp._open_socks = lambda req, pr, timeout: calls.append(pr) or type("R", (), {"status": 200})()
import urllib.request as _rq
import urllib.error as _er
req = _rq.Request("https://yuanbao.tencent.com/api/x", data=b"{}", method="POST")

ybp._CUR_EGRESS.set("acct03")
calls.clear()
ybp._open(req, timeout=5)
ok(len(calls) == 1 and "poolclient-acct03" in calls[0], "E5 账号出口只发一次", str(calls))


# 出口连不上时必须**直接抛错**，不能偷偷换下一个 IP
def boom(*a, **k):
    raise RuntimeError("proxy down")


ybp._open_socks = boom
calls.clear()
try:
    ybp._open(req, timeout=5)
    ok(False, "E5 出口故障应直接抛错")
except Exception as e:  # noqa: BLE001
    ok("proxy down" in str(e), "E5 出口故障直接抛出（不换 IP）", str(e)[:40])

# 没设账号出口时回到实例池路径（这里池里只有一条，也会被调用）
ybp._CUR_EGRESS.set("")
ybp._open_socks = lambda req, pr, timeout: calls.append(pr) or type("R", (), {"status": 200})()
calls.clear()
ybp._open(req, timeout=5)
ok(len(calls) == 1 and calls[0] == ybp.YB_PROXY_URL, "E6 无绑定走实例默认出口", str(calls))

shutil.rmtree(TMP, ignore_errors=True)
print("\n%s 通过 %d 项，失败 %d 项" % ("✅ 全部通过" if not FAIL else "❌ 有失败", PASS, FAIL))
sys.exit(1 if FAIL else 0)
