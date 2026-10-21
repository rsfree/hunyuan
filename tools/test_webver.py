#!/usr/bin/env python3
"""版本号动态化单测（X-WebVersion / x-commit-tag）。

背景：这两个值原本恒为硬编码，根因是 FP_JS 的正则结尾多了一个 `-`（URL 里是 `&`），
于是采集恒返回 None、静默退回硬编码。线上实测业务 API(/api/v5) 用的是 2.67.1 / dfd04310，
而硬编码是 2.87.2 / 02746073 —— 两个值在站点上都不存在。

断言重点：
  W1 真实 RUM URL 能提取出 (版本, commit)
  W2 缺少 `__commit` 段的 URL 不会被误判
  W3 app_version 兜底可用（格式再变也不至于拿不到版本）
  W4 FP_JS 源码里不再有那个多余的 `-`（静态防回归）
  W5 STATIC_HEADERS 的兜底值 = 实测值
  W6 CdpMinter.headers() 会把 ver/tag 下发成 X-WebVersion / x-commit-tag
"""
import importlib.util
import os
import re
import sys

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


# ---------- W1/W2/W3：正则语义（JS 与 Python 的字符类语法一致，可直接等价验证） ----------
RE_VER = re.compile(r"[?&]version=([\d.]+)__([0-9a-f]{6,})")
RE_APP = re.compile(r"[?&]app_version=([\d.]+)")
OLD_RE = re.compile(r"version=([\d.]+)__([0-9a-f]+)-")     # 修复前的错误正则

REAL = ("https://rumt-zh.com/collect/whitelist?id=XVoqetglrG1Z3Rkl1X&uin=no-auth"
        "&version=2.67.1__dfd04310&aid=dbab6abc&env=production&platform=4")

m = RE_VER.search(REAL)
ok(m and m.group(1) == "2.67.1" and m.group(2) == "dfd04310", "W1 真实 RUM URL 提取正确",
   str(m.groups() if m else None))
ok(OLD_RE.search(REAL) is None, "W1 旧正则确实匹配不上（复现根因）")

TRACE = "https://h.trace.qq.com/kv?attaid=004&app_version=2.67.1&sdk_version=4.7.0-web"
ok(RE_VER.search(TRACE) is None, "W2 无 __commit 段不误判")
ok(RE_APP.search(TRACE).group(1) == "2.67.1", "W3 app_version 兜底可用")

# ---------- W4：源码里不再有多余的 `-` 结尾 ----------
src = open(os.path.join(ROOT, "cdp_minter.py")).read()
ok("version=([\\d.]+)__([0-9a-f]+)-/" not in src, "W4 FP_JS 里已无错误正则")
ok("[?&]version=([\\d.]+)__([0-9a-f]{6,})" in src, "W4 FP_JS 已是修好的正则")
ok("__BOT_COMMIT_ID__" in src, "W4 commit 有全局兜底来源")

# ---------- W5/W6：兜底值与下发 ----------
os.environ.setdefault("YB_PROXY_URL", "socks5h://poolclient-acc01:pw@pool.example.com:2090")
spec = importlib.util.spec_from_file_location("ybp_wv", os.path.join(ROOT, "yuanbao_openai_proxy.py"))
ybp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ybp)
ok(ybp.STATIC_HEADERS.get("X-WebVersion") == "2.67.1", "W5 兜底版本号 = 实测值",
   ybp.STATIC_HEADERS.get("X-WebVersion"))
ok(ybp.STATIC_HEADERS.get("x-commit-tag") == "dfd04310", "W5 兜底 commit = 实测值",
   ybp.STATIC_HEADERS.get("x-commit-tag"))


class _FakeMinter:
    def dynamic_fp(self):
        return {"ver": "2.99.9", "tag": "abcdef12", "h38": "h38xx", "u42": "u42xx",
                "osv": "Mac OS(10.15.7)-Blink", "ua": "UA"}

    def mint(self):
        return {"uskey": "u", "md5": "m", "ts": "1"}


import mint_backends  # noqa: E402

cm = mint_backends.CdpMinter.__new__(mint_backends.CdpMinter)
cm._m = _FakeMinter()
h = cm.headers()
ok(h.get("X-WebVersion") == "2.99.9", "W6 动态版本下发覆盖兜底", h.get("X-WebVersion"))
ok(h.get("x-commit-tag") == "abcdef12", "W6 动态 commit 下发覆盖兜底", h.get("x-commit-tag"))

# 动态采集为空时必须能退回兜底（不能下发 None/空串，否则请求头会缺字段）
cm._m = type("F", (), {"dynamic_fp": lambda self: {"h38": "x", "u42": "y"}})()
h2 = cm.headers()
ok("X-WebVersion" not in h2 and "x-commit-tag" not in h2,
   "W6 采集为空时不下发（由 STATIC_HEADERS 兜底）")

print("\n%s 通过 %d 项，失败 %d 项" % ("✅ 全部通过" if not FAIL else "❌ 有失败", PASS, FAIL))
sys.exit(1 if FAIL else 0)
