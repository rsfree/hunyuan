#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""i2i_ratio_multi.py — 图生图「比例矩阵」+「多图参考」真机测试（在 ai-prod 上跑）。

Part A 比例：同一张参考图，逐个比例提交，量输出实际宽高比是否与请求一致
  （上游档位只有 1.5K/2K，我们恒请求 2K；比例靠 extra.image_gen_param.ratio 控制）
Part B 多图：2 / 3 / 4 张参考图（元宝单次上限 4 张），验证都能出图

用法：YBKEY=<cookie> python3 /tmp/i2i_ratio_multi.py <base_url>
"""
import base64
import json
import os
import sys
import time
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:39177"
KEY = sys.argv[2] if len(sys.argv) > 2 else os.environ.get("YBKEY", "")

# size → 期望比例（与 _RATIO_SLOTS 对齐）
RATIOS = [
    ("1:1",  "2048x2048", 1.0),
    ("4:3",  "2048x1536", 4 / 3),
    ("3:4",  "1536x2048", 3 / 4),
    ("16:9", "2048x1152", 16 / 9),
    ("9:16", "1152x2048", 9 / 16),
    ("auto", "auto", None),
]

MULTI = [2, 3, 4]
TOL = 0.04          # 宽高比容差 4%

PROMPT = "保持参考图主体的外形与特征不变，把背景换成雪山日出，光影自然，高清细节"
PROMPT_MULTI = "把这几个参考图中的主体自然地组合到同一个雪山日出场景里，保持各自外形特征不变"


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
            return r.status, dict(r.headers), json.loads(r.read()), time.time() - t0
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()[:300], time.time() - t0
    except Exception as e:
        return -1, {}, str(e)[:250], time.time() - t0


def dims(url, timeout=90):
    try:
        req = urllib.request.Request(url, headers={"user-agent": "Mozilla/5.0",
                                                   "Range": "bytes=0-65535"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            blob = r.read()
        w = h = None
        if blob[:8] == b"\x89PNG\r\n\x1a\n":
            w, h = int.from_bytes(blob[16:20], "big"), int.from_bytes(blob[20:24], "big")
        elif blob[:2] == b"\xff\xd8":
            i = 2
            while i < len(blob) - 9:
                if blob[i] == 0xFF and blob[i + 1] in (0xC0, 0xC1, 0xC2):
                    h, w = int.from_bytes(blob[i + 5:i + 7], "big"), int.from_bytes(blob[i + 7:i + 9], "big")
                    break
                i += 2 + int.from_bytes(blob[i + 2:i + 4], "big")
        elif blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
            fmt = blob[12:16]
            if fmt == b"VP8X":
                w = (int.from_bytes(blob[24:27], "little") & 0xFFFFFF) + 1
                h = (int.from_bytes(blob[27:30], "little") & 0xFFFFFF) + 1
            elif fmt == b"VP8 ":
                i = blob.find(b"\x9d\x01\x2a")
                if i > 0:
                    w = int.from_bytes(blob[i + 3:i + 5], "little") & 0x3FFF
                    h = int.from_bytes(blob[i + 5:i + 7], "little") & 0x3FFF
        return w, h
    except Exception:
        return None, None


def urls_of(js):
    if isinstance(js, dict) and js.get("data"):
        return [d.get("url") for d in js["data"] if d.get("url")]
    try:
        parts = js["choices"][0]["message"]["content"]
        if isinstance(parts, list):
            return [p["image_url"]["url"] for p in parts if p.get("type") == "image_url"]
    except Exception:
        pass
    return []


def main():
    refs = {n: b64("/tmp/ref%s.png" % n) for n in ("", "2", "3", "4")}
    print("参考图：ref.png(%dKB) ref2(%dKB) ref3(%dKB) ref4(%dKB)"
          % tuple(len(refs[k]) // 1024 for k in ("", "2", "3", "4")))
    print()

    print("===== Part A：比例矩阵（图生图 · 1 张参考图）=====")
    a_ok = a_all = 0
    for tag, size, want in RATIOS:
        s, h, js, el = post("/v1/images/generations", {
            "model": "hy-image-3.5", "prompt": PROMPT,
            "image": "data:image/png;base64," + refs[""], "size": size, "n": 1})
        us = urls_of(js)
        a_all += 1
        if not us:
            msg = (js.get("error") or {}).get("message") if isinstance(js, dict) else str(js)[:150]
            print("  %-5s size=%-10s http=%s %5.1fs  FAIL  %s" % (tag, size, s, el, str(msg)[:90]))
            continue
        w, hh = dims(us[0])
        got = (w / hh) if (w and hh) else None
        if want is None:
            ok, note = True, "智能比例"
        else:
            ok = got is not None and abs(got - want) / want <= TOL
            note = "期望%.3f 实际%.3f 偏差%.1f%%" % (want, got or 0,
                                                  abs((got or 0) - want) / want * 100 if got else 999)
        a_ok += 1 if ok else 0
        print("  %-5s size=%-10s http=%s %5.1fs  %s  %sx%s  %s"
              % (tag, size, s, el, "PASS" if ok else "FAIL", w, hh, note))
        sys.stdout.flush()

    print()
    print("===== Part B：多图生图（2 / 3 / 4 张参考图 · 1:1）=====")
    b_ok = 0
    for n in MULTI:
        keys = ["", "2", "3", "4"][:n]
        imgs = ["data:image/png;base64," + refs[k] for k in keys]
        s, h, js, el = post("/v1/images/generations", {
            "model": "hy-image-3.5", "prompt": PROMPT_MULTI,
            "image": imgs, "size": "2048x2048", "n": 1})
        us = urls_of(js)
        if not us:
            msg = (js.get("error") or {}).get("message") if isinstance(js, dict) else str(js)[:150]
            print("  %d 张参考  http=%s %5.1fs  FAIL  %s" % (n, s, el, str(msg)[:110]))
            continue
        w, hh = dims(us[0])
        ok = bool(w and hh)
        b_ok += 1 if ok else 0
        print("  %d 张参考  http=%s %5.1fs  %s  %sx%s"
              % (n, s, el, "PASS" if ok else "FAIL", w, hh))
        sys.stdout.flush()

    print()
    print("===== 汇总 =====")
    print("  A 比例：%d/%d 通过" % (a_ok, a_all))
    print("  B 多图：%d/%d 通过" % (b_ok, len(MULTI)))
    return 0 if (a_ok == a_all and b_ok == len(MULTI)) else 1


if __name__ == "__main__":
    sys.exit(main())
