#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_headers.py — 用**真实浏览器抓包**当基准，校验我们发出的请求头/body 是否对齐。

基准来源（2026-10-21，元宝「AI 创作」页 / 图生图 / msgScene 12 / Chrome 153 macOS）
见 tools/fixtures/yb_image_capture.json。

覆盖：
  H1 抓包里出现的头名，我们都要有（case-insensitive）
  H2 X-Traceparent 是 32 位 hex 且每次不同
  H3 X-device-id == X-HY93 == _qimei_uuid42
  H4 sec-ch-ua 的版本号与 UA 的 Chrome 主版本一致
  H5 CdpMinter.headers() 下发 X-HY92 / X-HY93 / X-device-id
  H6 i2i body 覆盖抓包的全部字段（模板专属 PromptParam 除外）
  H7 realPrompt 是用户原话（不含前置语）

用法：/Users/betterme/.workbuddy/binaries/python/envs/default/bin/python tools/test_headers.py
"""
import importlib.util
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

spec = importlib.util.spec_from_file_location("ybp", os.path.join(ROOT, "yuanbao_openai_proxy.py"))
ybp = importlib.util.module_from_spec(spec)
sys.modules["ybp"] = ybp
spec.loader.exec_module(ybp)

FAILS = []


def check(name, cond, detail=""):
    print("  %s %s%s" % ("PASS" if cond else "FAIL", name, ("  " + detail) if detail else ""))
    if not cond:
        FAILS.append(name)


CAPTURE = json.load(open(os.path.join(ROOT, "tools", "fixtures", "yb_image_capture.json"), encoding="utf-8"))

# 只在生图请求上额外带（普通聊天不带），单独校验
IMAGE_ONLY = {"x-event-input-type"}


def h1():
    print("[H1] 抓包里的头名覆盖度")
    sig = {"uskey": "u", "md5": "m", "ts": "1"}
    # 按 CdpMinter.headers() 的真实产出模拟（X-HY92/93 来自页内指纹，铸签成功时必有）
    ybp._DYN_HEADERS = {"X-HY92": "h38val", "X-HY93": "u42val", "X-device-id": "u42val",
                        "X-WebVersion": "2.89.1", "x-commit-tag": "7a0c0e99",
                        "X-os_version": "Mac OS(10.15.7)-Blink"}
    h = ybp._yb_headers("hy_user=x; hy_token=y", sig, "naQivTmsDa", "conv1",
                        extra={"X-Event-Input-Type": "15"})
    have = {k.lower() for k in h}
    need = {k.lower() for k in CAPTURE["headers"]}
    missing = sorted(need - have - IMAGE_ONLY)
    check("H1 无缺失头", not missing, "missing=%s" % missing)
    for k in ("sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform", "chat_version",
              "x-traceparent", "x-trid-channel", "x-hy93", "x-device-id"):
        check("H1 有 %s" % k, k in have)


def h2():
    print("[H2] X-Traceparent")
    sig = {"uskey": "u", "md5": "m", "ts": "1"}
    ybp._DYN_HEADERS = {}
    a = ybp._yb_headers("c", sig, "a")["X-Traceparent"]
    b = ybp._yb_headers("c", sig, "a")["X-Traceparent"]
    check("H2 32 位 hex", bool(re.fullmatch(r"[0-9a-f]{32}", a)), a)
    check("H2 每次不同", a != b)


def h3():
    print("[H3] X-device-id == X-HY93 == _qimei_uuid42")
    sig = {"uskey": "u", "md5": "m", "ts": "1"}
    ybp._DYN_HEADERS = {"X-HY92": "h38val", "X-HY93": "u42val", "X-device-id": "u42val",
                        "X-WebVersion": "2.89.1", "x-commit-tag": "7a0c0e99",
                        "X-os_version": "Mac OS(10.15.7)-Blink"}
    h = ybp._yb_headers("c", sig, "a")
    check("H3 device-id == HY93", h["X-device-id"] == h["X-HY93"] == "u42val",
          "%s / %s" % (h["X-device-id"], h["X-HY93"]))
    check("H3 不再用写死常量", h["X-device-id"] != "19c100d220910063ab8e4f54c0cba26e4d7c4bd2b8")
    # 动态头缺失时兜底不空
    ybp._DYN_HEADERS = {}
    h2b = ybp._yb_headers("c", sig, "a")
    check("H3 兜底非空", bool(h2b["X-device-id"]))


def h4():
    print("[H4] sec-ch-ua 与 UA 版本一致")
    sig = {"uskey": "u", "md5": "m", "ts": "1"}
    ybp._DYN_HEADERS = {}
    h = ybp._yb_headers("c", sig, "a")
    m = re.search(r"Chrome/(\d+)", h["user-agent"])
    ver = re.search(r'v="(\d+)"', h["sec-ch-ua"])
    check("H4 UA 可解析", bool(m), h["user-agent"][:60])
    check("H4 UA 主版本 == sec-ch-ua 主版本", m and ver and m.group(1) == ver.group(1),
          "ua=%s ch=%s" % (m and m.group(1), ver and ver.group(1)))
    check("H4 platform 与 UA 一致", '"macOS"' in h["sec-ch-ua-platform"]
          and "Macintosh" in h["user-agent"], h["sec-ch-ua-platform"])


def h5():
    print("[H5] CdpMinter.headers() 下发三件套")
    import mint_backends

    class Stub:
        def dynamic_fp(self):
            return {"h38": "h38val", "u42": "u42val", "ver": "2.89.1", "tag": "7a0c0e99",
                    "osv": "Mac OS(10.15.7)-Blink", "ua": "UA-string"}

    cm = object.__new__(mint_backends.CdpMinter)
    cm._m = Stub()
    h = cm.headers()
    check("H5 X-HY92 = h38", h.get("X-HY92") == "h38val", str(h.get("X-HY92")))
    check("H5 X-HY93 = u42", h.get("X-HY93") == "u42val", str(h.get("X-HY93")))
    check("H5 X-device-id = u42", h.get("X-device-id") == "u42val", str(h.get("X-device-id")))
    check("H5 保留动态指纹", h.get("X-WebVersion") == "2.89.1" and h.get("x-commit-tag") == "7a0c0e99")


def h6():
    print("[H6] i2i body 字段覆盖度")
    mm = CAPTURE["body"]["multimedia"]
    b = ybp._yb_image_body(CAPTURE["body"]["conversationId"], "naQivTmsDa",
                           "参考上传图生成图片：只改睫毛", mm, "2K",
                           "hunyuan_gpt_175B_0404", None)
    want = {k for k in CAPTURE["body"] if k not in ("PromptParam",)}   # 创作 tab 模板专属
    missing = sorted(want - set(b))
    check("H6 无缺失字段", not missing, "missing=%s" % missing)
    extra = sorted(set(b) - set(CAPTURE["body"]))
    check("H6 无多余字段（允许为空）", extra == [], "extra=%s" % extra)
    check("H6 msgScene=12（图生图）", b["msgScene"] == 12, str(b["msgScene"]))
    check("H6 skillId=ai_image", b["skillId"] == "ai_image")
    check("H6 image_gen_param 形态", set(b["extra"]["image_gen_param"]) >= {"model", "resolution"},
          json.dumps(b["extra"]["image_gen_param"], ensure_ascii=False))
    check("H6 multimedia 原样透传", b["multimedia"] == mm)


def h7():
    print("[H7] realPrompt = 用户原话")
    raw = "参考上传图生成图片：只改睫毛"
    b = ybp._yb_image_body("c", "a", raw, [], "2K", "m", None)
    check("H7 realPrompt 无前缀", b["realPrompt"] == raw, b["realPrompt"])
    check("H7 prompt 带默认前缀", b["prompt"].endswith(raw) and b["prompt"] != raw, b["prompt"][:20])
    # 前缀可配为空 ⇒ 完全对齐浏览器
    old = ybp.YB_IMAGE_PROMPT_PREFIX
    try:
        ybp.YB_IMAGE_PROMPT_PREFIX = ""
        b2 = ybp._yb_image_body("c", "a", raw, [], "2K", "m", None)
        check("H7 前缀置空后 prompt == realPrompt", b2["prompt"] == raw, b2["prompt"])
    finally:
        ybp.YB_IMAGE_PROMPT_PREFIX = old


if __name__ == "__main__":
    h1(); h2(); h3(); h4(); h5(); h6(); h7()
    print("\n%s  %d 项失败" % ("❌" if FAILS else "✅ 全部通过", len(FAILS)))
    for f in FAILS:
        print("   -", f)
    sys.exit(1 if FAILS else 0)
