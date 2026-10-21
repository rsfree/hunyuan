#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""i2i_matrix.py — 图生图「全形态」真机测试（在 ai-prod 上跑）。

覆盖：
  1 generations + base64 data URI (PNG)
  2 generations + https URL
  3 generations + 多图（2 张）
  4 generations + JPEG 输入
  5 generations + size=4k（验上游 2K + CI 放大）
  6 chat 门 + content parts (data URI)
  7 chat 门 + content parts (https URL)
  8 generations + n=2（多图返回）
每个用例：记录 http / 耗时 / 返回图数量 / 首图实际尺寸（回源下载后读头部）

用法：python3 /tmp/i2i_matrix.py <pool_base> <api_key>
"""
import base64
import json
import os
import sys
import time
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:39177/pool"
KEY = sys.argv[2] if len(sys.argv) > 2 else os.environ.get("YBKEY", "")
URL_REF = os.environ.get("URL_REF", "")

PROMPT = "把这张图的背景换成雪山日落，主体保持完全不变，光影自然，高清细节"


def b64(path):
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode()


def post(path, payload, timeout=300):
    data = json.dumps(payload, ensure_ascii=False).encode()
    req = urllib.request.Request(BASE + path, data=data, method="POST",
                                 headers={"Authorization": "Bearer " + KEY,
                                          "content-type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
            return r.status, dict(r.headers), json.loads(body), time.time() - t0
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()[:400], time.time() - t0
    except Exception as e:
        return -1, {}, str(e)[:300], time.time() - t0


def dims(url, timeout=90):
    """回源读图头拿尺寸（只读前 64KB）。"""
    try:
        req = urllib.request.Request(url, headers={"user-agent": "Mozilla/5.0",
                                                   "Range": "bytes=0-65535"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            blob = r.read()
            total = r.headers.get("Content-Range") or r.headers.get("Content-Length") or "?"
        w = h = None
        if blob[:8] == b"\x89PNG\r\n\x1a\n":
            w, h = int.from_bytes(blob[16:20], "big"), int.from_bytes(blob[20:24], "big")
        elif blob[:2] == b"\xff\xd8":
            i = 2
            while i < len(blob) - 9:
                if blob[i] == 0xFF and blob[i + 1] in (0xC0, 0xC2, 0xC1):
                    h, w = int.from_bytes(blob[i + 5:i + 7], "big"), int.from_bytes(blob[i + 7:i + 9], "big")
                    break
                i += 2 + int.from_bytes(blob[i + 2:i + 4], "big")
        elif blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
            fmt = blob[12:16]
            if fmt == b"VP8 ":
                i = blob.find(b"\x9d\x01\x2a")
                if i > 0:
                    w = int.from_bytes(blob[i + 3:i + 5], "little") & 0x3FFF
                    h = int.from_bytes(blob[i + 5:i + 7], "little") & 0x3FFF
            elif fmt == b"VP8X":
                w = (int.from_bytes(blob[24:27], "little") & 0xFFFFFF) + 1
                h = (int.from_bytes(blob[27:30], "little") & 0xFFFFFF) + 1
        return "%sx%s" % (w, h) if w else "?x?", total
    except Exception as e:
        return "ERR:%s" % str(e)[:60], "?"


def first_url(js):
    """从两种响应形状里取图 URL 列表。"""
    if isinstance(js, dict) and js.get("data"):
        return [d.get("url") for d in js["data"] if d.get("url")]
    try:
        parts = js["choices"][0]["message"]["content"]
        if isinstance(parts, list):
            return [p["image_url"]["url"] for p in parts if p.get("type") == "image_url"]
        if isinstance(parts, str):
            return []
    except Exception:
        pass
    return []


def show(tag, status, hdr, js, el, extra=""):
    urls = first_url(js) if isinstance(js, dict) else []
    routed = hdr.get("X-YB-Routed-To") or hdr.get("x-yb-routed-to") or "-"
    up = hdr.get("X-YB-Upscale") or hdr.get("x-yb-upscale") or ""
    if urls:
        d, size = dims(urls[0])
        ci = "CI" if "imageMogr2" in urls[0] else "--"
        print("  %-38s http=%s %6.1fs n=%d routed=%-6s %s 首图=%s %s %s%s"
              % (tag, status, el, len(urls), routed, ci, d, size, extra,
                 ("  " + up) if up else ""))
        print("      %s" % urls[0][:150])
    else:
        msg = ""
        if isinstance(js, dict):
            msg = (js.get("error") or {}).get("message") if isinstance(js.get("error"), dict) else str(js)[:160]
        else:
            msg = str(js)[:220]
        print("  %-38s http=%s %6.1fs n=0 routed=%-6s  %s" % (tag, status, el, routed, msg))
    sys.stdout.flush()
    return urls


def main():
    if not os.path.exists("/tmp/ref.png"):
        print("缺少 /tmp/ref.png"); return 1
    png = b64("/tmp/ref.png")
    jpg = b64("/tmp/ref.jpg")
    png2 = b64("/tmp/ref2.png")
    print("参考图 base64：png=%dKB jpg=%dKB ref2=%dKB  上游URL=%s"
          % (len(png) // 1024, len(jpg) // 1024, len(png2) // 1024,
             (URL_REF[:60] + "…") if URL_REF else "(未给)"))

    results = {}

    # 1 base64 PNG
    s, h, js, el = post("/v1/images/generations",
                        {"model": "hy-image-3.5", "prompt": PROMPT,
                         "image": "data:image/png;base64," + png, "size": "2048x2048", "n": 1})
    results["1.dataURI-PNG"] = show("1 generations + dataURI(PNG)", s, h, js, el)

    # 2 https URL
    if URL_REF:
        s, h, js, el = post("/v1/images/generations",
                            {"model": "hy-image-3.5", "prompt": PROMPT,
                             "image": URL_REF, "size": "2048x2048", "n": 1})
        results["2.url"] = show("2 generations + https URL", s, h, js, el)
    else:
        print("  (跳过 2：未提供 URL_REF)")

    # 3 多图（2 张）
    s, h, js, el = post("/v1/images/generations",
                        {"model": "hy-image-3.5", "prompt": "把两张图里的主体合成到同一个雪山场景里",
                         "image": ["data:image/png;base64," + png, "data:image/png;base64," + png2],
                         "size": "2048x2048", "n": 1})
    results["3.multi2"] = show("3 generations + 多图(2)", s, h, js, el)

    # 4 JPEG
    s, h, js, el = post("/v1/images/generations",
                        {"model": "hy-image-3.5", "prompt": PROMPT,
                         "image": "data:image/jpeg;base64," + jpg, "size": "2048x2048", "n": 1})
    results["4.jpeg"] = show("4 generations + JPEG", s, h, js, el)

    # 5 4K（验 CI 放大）
    s, h, js, el = post("/v1/images/generations",
                        {"model": "hy-image-3.5", "prompt": PROMPT,
                         "image": "data:image/png;base64," + png, "size": "4k", "n": 1})
    results["5.4k"] = show("5 generations + size=4k (CI)", s, h, js, el)

    # 6 chat 门 data URI
    s, h, js, el = post("/v1/chat/completions",
                        {"model": "hunyuan", "messages": [{"role": "user", "content": [
                            {"type": "text", "text": PROMPT},
                            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + png}}]}],
                         "size": "2048x2048"})
    results["6.chat-dataURI"] = show("6 chat门 + dataURI", s, h, js, el)

    # 7 chat 门 https URL
    if URL_REF:
        s, h, js, el = post("/v1/chat/completions",
                            {"model": "hunyuan", "messages": [{"role": "user", "content": [
                                {"type": "text", "text": PROMPT},
                                {"type": "image_url", "image_url": {"url": URL_REF}}]}],
                             "size": "2048x2048"})
        results["7.chat-url"] = show("7 chat门 + https URL", s, h, js, el)
    else:
        print("  (跳过 7：未提供 URL_REF)")

    # 8 n=2
    s, h, js, el = post("/v1/images/generations",
                        {"model": "hy-image-3.5", "prompt": "把背景换成星空，主体不变",
                         "image": "data:image/png;base64," + png, "size": "2048x2048", "n": 2})
    results["8.n2"] = show("8 generations + n=2", s, h, js, el)

    print("\n===== 汇总 =====")
    ok = 0
    for k, v in results.items():
        flag = "PASS" if v else "FAIL"
        ok += 1 if v else 0
        print("  %-4s %-22s 图数=%d" % (flag, k, len(v)))
    print("  %d/%d 形态通过" % (ok, len(results)))
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
