#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""patch_headers.py — 按真实浏览器抓包对齐请求头（幂等、带断言）。

依据（2026-10-21 用户在元宝「AI 创作」页抓到的图生图请求）：
  缺失且**真实 Chrome 必发**的头：
    sec-ch-ua / sec-ch-ua-mobile / sec-ch-ua-platform   ← client hints 三件套，缺了就很"非浏览器"
    chat_version: v1
    X-Traceparent: <32hex>（每次请求随机）
    X-HY93: <_qimei_uuid42>
  并修正：
    X-device-id 必须 == X-HY93 == _qimei_uuid42
    （原来 STATIC_HEADERS 里写死 19c100d2…，4 个号全一样 ⇒ 强关联信号）
"""
import io
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
P = os.path.join(ROOT, "yuanbao_openai_proxy.py")
M = os.path.join(ROOT, "cdp_minter.py")

src = io.open(P, encoding="utf-8").read()
orig = src


def sub(label, old, new, count=1):
    global src
    n = src.count(old)
    assert n == count, "【%s】匹配数 %d != 期望 %d" % (label, n, count)
    src = src.replace(old, new)
    print("  ok  %-40s (%d)" % (label, n))


# ---------------------------------------------------------------- 1. 补齐 STATIC_HEADERS
sub("补 sec-ch-ua 三件套 + chat_version + X-Trid-Channel",
'''STATIC_HEADERS = {
    "X-Input-Type": "text",
    "X-Requested-With": "XMLHttpRequest",
    "X-Instance-ID": "5",
    "X-Source": "web",
    "X-Language": "zh-CN",
    "X-device-id": "19c100d220910063ab8e4f54c0cba26e4d7c4bd2b8",  # 动态覆盖
    "X-HY106": "",
    "X-os_version": "Mac OS(10.15.7)-Blink",                     # 动态覆盖
    "X-Platform": "mac",
    "X-webdriver": "0",
    "X-ybuitest": "0",
    "X-Exp-Params": "enableNewPcStyle=2",
    "x-web-ch-id": "null",
    "X-Web-Third-Source": "main",
    "x-commit-tag": "02746073",                                  # 动态覆盖
    "X-WebVersion": "2.87.2",                                    # 动态覆盖
}''',
'''# 真实浏览器（Chrome 153 / macOS）POST 时**必发**的 client hints 三件套。
# 🔴 缺这三条是很硬的"非浏览器"证据：真 Chrome 只要 UA 里是 Chrome，就一定会带上。
#    且其值必须与 UA 的版本号一致（这里 UA 固定 Chrome/153 ⇒ v="153"）。
STATIC_HEADERS = {
    "X-Input-Type": "text",
    "X-Requested-With": "XMLHttpRequest",
    "X-Instance-ID": "5",
    "X-Source": "web",
    "X-Language": "zh-CN",
    "X-device-id": "",          # 动态覆盖为 _qimei_uuid42（与 X-HY93 同值）；空则由下面兜底
    "X-HY106": "",
    "X-os_version": "Mac OS(10.15.7)-Blink",                     # 动态覆盖
    "X-Platform": "mac",
    "X-webdriver": "0",
    "X-ybuitest": "0",
    "X-Exp-Params": "enableNewPcStyle=2",
    "x-web-ch-id": "null",
    "X-Web-Third-Source": "main",
    "x-commit-tag": "02746073",                                  # 动态覆盖
    "X-WebVersion": "2.87.2",                                    # 动态覆盖
    "X-Trid-Channel": "undefined",
    # --- 以下三项来自真实抓包，原来完全缺失 ---
    "sec-ch-ua": '"Google Chrome";v="153", "Not_A Brand";v="8", "Chromium";v="153"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
    "chat_version": "v1",
}''')

# ---------------------------------------------------------------- 2. 每次请求随机 X-Traceparent
sub("_yb_headers 加 X-Traceparent", '''    if sig:
        h.update({"X-Uskey": sig["uskey"], "X-Bus-Params-Md5": sig["md5"], "X-Timestamp": sig["ts"]})
    if extra:
        h.update(extra)
    return h''',
'''    # 真浏览器每个请求都带一个新的 traceparent（32 位 hex，无连字符）
    h["X-Traceparent"] = uuid.uuid4().hex
    # X-device-id 兜底：动态头没给（如铸签失败）时不要留空 —— 留空比给个假值更可疑
    if not h.get("X-device-id"):
        h["X-device-id"] = h.get("X-HY93") or "19c100d220910063ab8e4f54c0cba26e4d7c4bd2b8"
    if sig:
        h.update({"X-Uskey": sig["uskey"], "X-Bus-Params-Md5": sig["md5"], "X-Timestamp": sig["ts"]})
    if extra:
        h.update(extra)
    return h''')

assert src != orig
io.open(P, "w", encoding="utf-8").write(src)
print("写入:", P)

# ---------------------------------------------------------------- 3. 页内取 uuid42 并下发 X-device-id/X-HY93
ms = io.open(M, encoding="utf-8").read()
m_orig = ms


def sub2(label, old, new, count=1):
    global ms
    n = ms.count(old)
    assert n == count, "【%s】匹配数 %d != 期望 %d" % (label, n, count)
    ms = ms.replace(old, new)
    print("  ok  %-40s (%d)" % (label, n))


sub2("FP_JS 采集 uuid42",
'''  let ver = null, tag = null, h38 = null;
  try {
    const arrs = Object.keys(window).filter(k => k.startsWith("webpackChunk"));
    const arr = window[arrs[0]];
    let req; arr.push([["fp_" + Date.now()], {}, (r) => { req = r; }]);
    const sdk = req(77004).I5(req(77004).PU);
    const q = sdk.getLocalQimei36();
    h38 = q ? q.h38 : null;
  } catch (e) {}''',
'''  let ver = null, tag = null, h38 = null, u42 = null;
  try {
    const arrs = Object.keys(window).filter(k => k.startsWith("webpackChunk"));
    const arr = window[arrs[0]];
    let req; arr.push([["fp_" + Date.now()], {}, (r) => { req = r; }]);
    const sdk = req(77004).I5(req(77004).PU);
    const q = sdk.getLocalQimei36();
    h38 = q ? q.h38 : null;
    u42 = (q && (q.uuid42 || q.uuid)) || null;
  } catch (e) {}
  // _qimei_uuid42 就是真实请求里的 X-device-id / X-HY93（实测值与抓包完全一致）
  if (!u42) { try { u42 = localStorage.getItem('_qimei_uuid42'); } catch (e) {} }''')

sub2("FP_JS 返回 u42", "  return {ver, tag, osv, ua, h38};", "  return {ver, tag, osv, ua, h38, u42};")

sub2("headers() 下发 X-HY93 / X-device-id",
'''        if fp.get("h38"):
            h["X-HY92"] = fp["h38"]          # uskey 与 h38 必须同源''',
'''        if fp.get("h38"):
            h["X-HY92"] = fp["h38"]          # uskey 与 h38 必须同源
        if fp.get("u42"):
            # 🔴 实测：真实请求里 X-device-id == X-HY93 == _qimei_uuid42（同一串）
            # 原来 STATIC_HEADERS 写死一个常量 ⇒ 所有号共用同一个 device-id（强关联）
            h["X-HY93"] = fp["u42"]
            h["X-device-id"] = fp["u42"]''')

assert ms != m_orig
io.open(M, "w", encoding="utf-8").write(ms)
print("写入:", M)
