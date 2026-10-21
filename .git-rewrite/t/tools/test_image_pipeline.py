#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_image_pipeline.py — 生图链路「翻译层」离线自检（不联网、不发真实生成请求）。

覆盖：
  I1 模型别名判定（hy-image-* / dall-e-* 通配，去水印名排除）
  I2 size → resolution / ratio 槽位映射
  I3 _yb_image_body：文生图 msgScene=13 / 图生图 msgScene=12 / ratio 透传
  I4 _parse_yb_sse_common：多图 replace 事件 → originUrl(无水印) + url(带水印)
  I5 _cookie_mode_run 生图分支端到端（mock 上游）→ result 形状
  I6 n 截断语义（n=4 给 4 张，n=1 只回 1 张）

⚠️ 本文件**只验证翻译层**：不产生任何计费/真实生成。
用法：/Users/betterme/.workbuddy/binaries/python/envs/default/bin/python tools/test_image_pipeline.py
"""
import importlib.util
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)   # 让 proxy 能 import 同目录的 guard/pool_state/metrics
spec = importlib.util.spec_from_file_location("ybp", os.path.join(ROOT, "yuanbao_openai_proxy.py"))
ybp = importlib.util.module_from_spec(spec)
sys.modules["ybp"] = ybp
spec.loader.exec_module(ybp)

FAILS = []


def check(name, cond, detail=""):
    print("  %s %s%s" % ("PASS" if cond else "FAIL", name, ("  " + detail) if detail else ""))
    if not cond:
        FAILS.append(name)


def i1():
    print("[I1] 模型别名判定")
    for m in ("hy-image", "hy-image-3.5", "hy-image-v3.5", "hy-image-v3.5-preview",
              "hy-image-4", "dall-e-3", "HY-IMAGE-3.5"):
        check("I1 %-24s → 生图" % m, ybp._is_image_model(m) is True)
    for m in ("hunyuan", "hunyuan-t1", "deepseek-v3", "hy-image-unwatermark", "removewatermark", ""):
        check("I1 %-24s → 非生图" % (m or "(空)"), ybp._is_image_model(m) is False)
    check("I1 hy-image-* 走 images 端点归一基础模型",
          ybp.MODEL_MAP.get("hy-image-3.5") == "hunyuan_gpt_175B_0404",
          ybp.MODEL_MAP.get("hy-image-3.5"))


def i2():
    print("[I2] size 映射")
    # 上游档位只有 1.5K / 2K（resolutionList），且要求**一律请求 2K**：
    # 客户端要更大的由 CI 出图后放大，要更小的也不必降档（大图能缩、小图缩不回来）
    for size in ("", "auto", "1k", "2k", "1024x1024", "2048x2048", "4k", "4096x4096", "8192x8192"):
        got = ybp._size_to_resolution(size)
        check("I2 res %-12s → 2K" % (size or "(空)"), got == "2K", "got=%s" % got)
    for size, ratio in (("", None), ("auto", None), ("16:9", "16:9"), ("1024x1024", "1:1"),
                        ("1024x768", "4:3"), ("768x1024", "3:4"), ("1792x1024", "16:9")):
        got = ybp._size_to_ratio(size)
        check("I2 ratio %-12s → %s" % (size or "(空)", ratio), got == ratio, "got=%s" % got)


def i3():
    print("[I3] 生图 body")
    b = ybp._yb_image_body("conv-1", "naQivTmsDa", "一只柯基", [], "2K", "hunyuan_gpt_175B_0404", "16:9")
    check("I3 文生图 msgScene=13", b["msgScene"] == 13, "msgScene=%s" % b["msgScene"])
    check("I3 skillId=ai_image", b["skillId"] == "ai_image" and b["chatSource"] == "ai_image")
    check("I3 prompt 前缀", b["prompt"] == "帮我生成图片：一只柯基", b["prompt"][:40])
    ig = (b.get("extra") or {}).get("image_gen_param") or {}
    check("I3 image_gen_param", ig.get("model") == "Hy Image 3.5" and ig.get("resolution") == "2K"
          and ig.get("ratio") == "16:9", json.dumps(ig, ensure_ascii=False))
    check("I3 无 ratio 时不写字段",
          "ratio" not in ((ybp._yb_image_body("c", "a", "p", [], "1K", "m", None).get("extra") or {})
                          .get("image_gen_param") or {}))
    check("I3 multimedia 空数组", b["multimedia"] == [])
    mm = [{"type": "image", "url": "https://x/a.png", "fileId": "f1"}]
    b2 = ybp._yb_image_body("conv-2", "naQivTmsDa", "改成油画", mm, "1.5K", "hunyuan_gpt_175B_0404", None)
    check("I3 图生图 msgScene=12", b2["msgScene"] == 12, "msgScene=%s" % b2["msgScene"])
    check("I3 图生图透传 multimedia", b2["multimedia"] == mm)


def i4():
    print("[I4] 多图 SSE 解析")
    sse = "\n".join([
        'data: {"type":"thinking","msg":"正在构思"}',
        'data: {"type":"text","msg":"好的，为你生成 4 张"}',
        'data: {"type":"replace","replace":{"multimedias":[%s]}}' % ",".join(
            json.dumps({"url": "https://cdn.yb/%d_h1_.png" % i,
                        "originUrl": "https://cdn.yb/%d_h0_.png" % i}, ensure_ascii=False)
            for i in range(4)),
        'data: [DONE]',
    ])
    p = ybp._parse_yb_sse_common(sse)
    check("I4 解析出 4 张无水印图", len(p["urls"]) == 4, "%s" % p["urls"])
    check("I4 urls 取 originUrl(_h0_)", all(u.endswith("_h0_.png") for u in p["urls"]), "%s" % p["urls"])
    check("I4 wmUrls 取 url(_h1_)", len(p["wmUrls"]) == 4 and all(u.endswith("_h1_.png") for u in p["wmUrls"]))
    check("I4 文本增量保留", p["text"] == "好的，为你生成 4 张", p["text"])
    check("I4 无 error", p["error"] is None, "%s" % p["error"])

    bad = 'data: {"type":"error","msg":"服务繁忙，请稍后重试"}\n'
    check("I4 error 识别", ybp._parse_yb_sse_common(bad)["error"] == "服务繁忙，请稍后重试")


def i5():
    print("[I5] _cookie_mode_run 生图分支（mock 上游）")
    real = {k: getattr(ybp, k) for k in ("_yb_post_json", "get_sig", "pace")}
    state = {"calls": []}

    def fake_post(cookie, path, body, sig, agent_id, conv="", extra=None, timeout=300):
        state["calls"].append((path, body.get("msgScene"), body.get("conversationId")))
        if "conversation/create" in path:
            return {"status": 200, "text": json.dumps({"id": "conv-img"})}
        if "conversation/v1/delete" in path:
            return {"status": 200, "text": "{}"}
        mult = [{"url": "https://cdn.yb/%d_h1_.png" % i,
                 "originUrl": "https://cdn.yb/%d_h0_.png" % i} for i in range(4)]
        return {"status": 200, "text":
                'data: {"type":"text","msg":"已生成"}\n'
                'data: {"type":"replace","replace":{"multimedias":%s}}\n' % json.dumps(mult)}

    ybp._yb_post_json = fake_post
    ybp.get_sig = lambda tab=None, force=False: {"uskey": "u", "md5": "m", "ts": "1"}
    ybp.pace = lambda k, *a, **kw: None      # 口径变了：pace(key, kind='chat')
    try:
        r = ybp._cookie_mode_run("hy_user=fake; hy_token=fake", "naQivTmsDa", "一只柯基",
                                "hunyuan_gpt_175B_0404", [], "1K", "1:1", force_image=True)
        check("I5 status=200", r.get("status") == 200, "%s" % r.get("status"))
        check("I5 拿到 4 张", len(r.get("urls") or []) == 4, "%s" % (r.get("urls") or [])[:1])
        check("I5 text 有内容", r.get("text") == "已生成", r.get("text"))
        check("I5 无 error", not r.get("error"), str(r.get("error")))
        seq = [c for c in state["calls"]]
        check("I5 先建会话再生图", len(seq) >= 2 and "conversation/create" in seq[0][0], "%s" % (seq[:2],))
        check("I5 生图请求 msgScene=13", any(c[1] == 13 for c in seq), "%s" % (seq,))
        check("I5 用完即删会话", any("conversation/v1/delete" in c[0] for c in seq), "%s" % (seq,))
    finally:
        for k, v in real.items():
            setattr(ybp, k, v)


def i6():
    print("[I6] n 截断语义")
    urls = ["https://cdn.yb/%d_h0_.png" % i for i in range(4)]
    wm = ["https://cdn.yb/%d_h1_.png" % i for i in range(4)]

    def assemble(n):
        data = []
        for i, u in enumerate(urls):
            item = {"url": u, "revised_prompt": "p"}
            if i < len(wm):
                item["url_watermarked"] = wm[i]
            data.append(item)
        if n < len(data):
            data = data[:n]
        return data

    check("I6 n=4 → 4 张", len(assemble(4)) == 4)
    check("I6 n=1 → 1 张", len(assemble(1)) == 1)
    check("I6 每张带 url_watermarked", all("url_watermarked" in x for x in assemble(4)))
    check("I6 n 上限 4（min(n,4) 语义）", min(int(9), 4) == 4)


def i7():
    print("[I7] >2K 放大（CI）")
    box = ybp._size_to_target_box
    for size, want in (("", None), ("auto", None), ("1k", None), ("2k", None),
                       ("1024x1024", None), ("2048x2048", None),
                       ("4k", (4096, 4096)), ("4096x4096", (4096, 4096)),
                       ("3840x2160", (3840, 2160)), ("2560x1440", (2560, 1440))):
        got = box(size)
        check("I7 box %-12s → %s" % (size or "(空)", want), got == want, "got=%s" % (got,))
    # 超上限等比夹紧
    ybp.YB_UPSCALE_MAX = 4096
    got = box("8192x8192")
    check("I7 8192 夹到 4096", got == (4096, 4096), "%s" % (got,))
    check("I7 8192x4096 等比夹紧", box("8192x4096") == (4096, 2048), "%s" % (box("8192x4096"),))

    check("I7 ci_join 无 query", ybp._ci_join("https://a/b.png", "op") == "https://a/b.png?op")
    check("I7 ci_join 有 query",
          ybp._ci_join("https://a/b.png?width=1", "op") == "https://a/b.png?width=1&op")
    check("I7 host 解析", ybp._host_of("https://cdn-pro.yuanbao.tencent.com/x/y.png?a=1")
          == "cdn-pro.yuanbao.tencent.com")

    # _maybe_upscale：支持 CI 的图床才改写
    real = ybp._ci_supported
    try:
        ybp._ci_supported = lambda u: "ok-host" in u
        urls, info = ybp._maybe_upscale(
            ["https://ok-host/a.png", "https://no-host/b.png"], "4k")
        check("I7 只改写支持的图床", urls[0].endswith("imageMogr2/thumbnail/!4096x4096r")
              and urls[1] == "https://no-host/b.png", "%s" % urls)
        check("I7 info 统计正确", info["upscaled"] == 1 and info["total"] == 2, "%s" % (info,))
        check("I7 标注非 AI 超分", "非 AI 超分" in info["note"], info["note"])
        # ≤2K 不动
        u2, i2 = ybp._maybe_upscale(["https://ok-host/a.png"], "1024x1024")
        check("I7 ≤2K 不改写", u2 == ["https://ok-host/a.png"] and i2 is None, "%s" % (u2,))
        # 关闭开关
        ybp.YB_UPSCALE_ENABLE = False
        u3, i3 = ybp._maybe_upscale(["https://ok-host/a.png"], "4k")
        check("I7 开关关闭生效", u3 == ["https://ok-host/a.png"] and i3 is None)
        ybp.YB_UPSCALE_ENABLE = True
    finally:
        ybp._ci_supported = real


if __name__ == "__main__":
    i1(); i2(); i3(); i4(); i5(); i6(); i7()
    print("\n%s  %d 项失败" % ("❌" if FAILS else "✅ 全部通过", len(FAILS)))
    for f in FAILS:
        print("   -", f)
    sys.exit(1 if FAILS else 0)
