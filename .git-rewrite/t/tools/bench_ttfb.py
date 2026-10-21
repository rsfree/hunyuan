#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bench_ttfb.py — 元宝代理「真流式」首字节基准（逐分片到达时刻）。

用法：
  python3 tools/bench_ttfb.py <base_url> <api_key|cookie> [model] [prompt]

判定：
  真流式 ⇒ ttfb 显著小于 total，且分片是**逐步**到达的（间隔有节奏）
  伪流式 ⇒ ttfb ≈ total（上游全量读完才吐第一个分片），后续分片瞬间刷完

注意：requests 一律 trust_env=False（本机 HTTP_PROXY 会把 127.0.0.1 也代理掉）。
"""
import json
import sys
import time

import requests

BASE = (sys.argv[1] if len(sys.argv) > 1 else "").rstrip("/")
KEY = sys.argv[2] if len(sys.argv) > 2 else ""
MODEL = sys.argv[3] if len(sys.argv) > 3 else "hunyuan"
PROMPT = sys.argv[4] if len(sys.argv) > 4 else "请用 5 句话介绍杭州，每句话单独成段。"

S = requests.Session()
S.trust_env = False
H = {"Authorization": "Bearer " + KEY, "content-type": "application/json"}


def stream_run():
    body = {"model": MODEL, "stream": True, "messages": [{"role": "user", "content": PROMPT}]}
    t0 = time.time()
    r = S.post(BASE + "/v1/chat/completions", headers={**H, "Accept": "text/event-stream"},
               json=body, stream=True, timeout=180)
    if r.status_code != 200:
        print("  HTTP %s: %s" % (r.status_code, r.text[:300]))
        return None
    first, stamps, text, err = None, [], [], None
    for line in r.iter_lines(decode_unicode=True):
        if not line:
            continue
        if line.startswith("data: "):
            p = line[6:].strip()
            if p == "[DONE]":
                break
            try:
                o = json.loads(p)
            except Exception:
                continue
            if o.get("error"):
                err = o["error"].get("message")
            ch = (o.get("choices") or [{}])[0].get("delta") or {}
            if ch.get("content"):
                text.append(ch["content"])
            if first is None and (ch.get("content") or ch.get("reasoning_content")):
                first = time.time() - t0
            stamps.append(time.time() - t0)
    total = time.time() - t0
    return {"ttfb": first, "total": total, "n": len(stamps), "text": "".join(text), "err": err,
            "stamps": stamps}


def nonstream_run():
    body = {"model": MODEL, "stream": False, "messages": [{"role": "user", "content": PROMPT}]}
    t0 = time.time()
    r = S.post(BASE + "/v1/chat/completions", headers=H, json=body, timeout=180)
    total = time.time() - t0
    return {"total": total, "status": r.status_code, "text": r.text[:300], "ttfb": r.elapsed.total_seconds() if r.elapsed else None}


print("base=%s model=%s" % (BASE, MODEL))
print("== 非流式（先打，用作对照）==")
ns = nonstream_run()
print("  total=%.3fs http=%s" % (ns["total"], ns["status"]))
try:
    j = json.loads(ns["text"]) if ns["status"] == 200 else None
except Exception:
    j = None
if j:
    print("  content=%r" % ((j["choices"][0]["message"].get("content") or "")[:80],))
else:
    print("  body=%s" % ns["text"][:200])

print("== 真流式 ==")
st = stream_run()
if not st:
    sys.exit(1)
print("  ttfb=%.3fs total=%.3fs chunks=%d" % (st["ttfb"], st["total"], st["n"]))
if st["stamps"]:
    head = st["stamps"][:6]
    print("  前 %d 个分片到达: %s" % (len(head), " ".join("+%.2f" % x for x in head)))
gap_first = st["ttfb"] or 0
tail_span = (st["total"] - (st["stamps"][0] if st["stamps"] else 0))
print("  content=%r" % (st["text"][:80],))
if st["err"]:
    print("  err=%s" % st["err"])
ratio = (st["ttfb"] or 0) / st["total"] if st["total"] else 1
verdict = "真流式 ✅" if ratio < 0.5 else "伪流式 ⚠️"
print("  ttfb/total=%.2f ⇒ %s" % (ratio, verdict))
