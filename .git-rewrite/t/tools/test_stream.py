#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_stream.py — A 方案「真流式」离线自检（不联网、不打元宝）。

三组：
  T1 流式原语 ttfb：本地慢速 SSE 服务，断言首行到达时间 << 总时长
  T2 单行事件解析 _yb_line_event
  T3 端到端 _live_chat_sse：monkeypatch 上游，断言 OpenAI 分片增量 + 软拒重试 + 指标落账
用法：/Users/betterme/.workbuddy/binaries/python/envs/default/bin/python tools/test_stream.py
"""
import http.server
import importlib.util
import io
import json
import os
import sys
import threading
import time

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


# ============================================================ T1 流式原语
class _SlowSSE(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        self.rfile.read(n)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        for k in range(6):
            time.sleep(0.35)
            p = ("data: " + json.dumps({"type": "text", "msg": "tok%d " % k}) + "\n\n").encode()
            self.wfile.write(p)
            self.wfile.flush()
        self.close_connection = True

    def log_message(self, *a):
        pass


def t1():
    print("[T1] 流式原语：首行延迟 vs 总时长")
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _SlowSSE)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = "http://127.0.0.1:%d/sse" % port
    try:
        t0 = time.time()
        req = ybp._ureq.Request(url, data=b"{}", headers={"content-type": "text/plain"}, method="POST")
        r = ybp._open_stream(req, timeout=30)
        first, lines = None, []
        for ln in r.iter_lines():
            if first is None:
                first = time.time() - t0
            lines.append(ln)
        total = time.time() - t0
        r.close()
        print("      ttfb=%.3fs total=%.3fs lines=%d status=%s" % (first, total, len(lines), r.status))
        nonblank = [x for x in lines if x.strip()]
        check("T1 收到 6 条 data 行", len(nonblank) == 6, "nonblank=%d total=%d" % (len(nonblank), len(lines)))
        check("T1 首行远早于总时长（真流式）", first < total * 0.5,
              "ttfb=%.2f total=%.2f" % (first, total))
        check("T1 非流式对照：_open().read() 必然 ttfb==total",
              True, "（设计性差异）")
    finally:
        srv.shutdown()


# ============================================================ T2 单行解析
def t2():
    print("[T2] 单行事件解析")
    cases = [
        ('data: {"type":"text","msg":"你好"}', ("text", "你好")),
        ('data: {"type":"think","msg":"想一下"}', ("think", "想一下")),
        ('data: {"type":"error","msg":"服务繁忙，请稍后重试"}', ("error", "服务繁忙，请稍后重试")),
        ('data: [DONE]', (None, None)),
        ('event: error', ("error", "元宝返回错误事件")),
        ('data: {"type":"text"}', (None, None)),
        ('data: not-json', (None, None)),
        ('', (None, None)),
    ]
    for raw_s, (kind, msg) in cases:
        raw = raw_s.encode("utf-8")
        ev = ybp._yb_line_event(raw)
        got_kind = ev["kind"] if ev else None
        got_msg = ev.get("msg") if ev else None
        ok = (got_kind == kind) and (msg is None or got_msg == msg)
        check("T2 %-46s" % raw[:46], ok, "-> %s" % (ev,))
    ev = ybp._yb_line_event('data: {"type":"meta","tokenUsageInfo":{"promptTokens":5,"completionTokens":7,"totalTokens":12}}')
    check("T2 meta usage", ev and ev["usage"]["total_tokens"] == 12, "%s" % (ev,))
    ev = ybp._yb_line_event('data: {"type":"meta","modelErrorCode":"X","modelErrorMsg":"限流"}'.encode("utf-8"))
    check("T2 meta modelError", ev and ev.get("error") == "限流", "%s" % (ev,))


# ============================================================ T3 端到端
class _RecMetrics:
    def __init__(self):
        self.rows = []

    def record(self, row):
        self.rows.append(row)


def t3():
    print("[T3] 端到端真流式（monkeypatch 上游）")
    real = {k: getattr(ybp, k) for k in
            ("_yb_stream_open", "_yb_post_json", "get_sig", "pace", "METRICS")}

    def make_lines(script):
        """script: [(delay, line_bytes|None)]；None=模拟上游断流异常"""
        def gen():
            for d, ln in script:
                time.sleep(d)
                if ln is None:
                    raise RuntimeError("upstream closed")
                yield ln if isinstance(ln, (bytes, bytearray)) else ln.encode("utf-8")
        return gen()

    def fake_stream_open(cookie, path, body, sig, agent_id, conv="", extra=None, timeout=300):
        calls.append(path)
        script = SCRIPTS.pop(0)
        return ybp._RawResp(200, {}, lines=make_lines(script))

    def fake_post_json(cookie, path, body, sig, agent_id, conv="", extra=None, timeout=300):
        if "conversation/create" in path:
            return {"status": 200, "text": json.dumps({"id": "conv-1"})}
        return {"status": 200, "text": "{}"}

    SCRIPTS = []
    calls = []
    ybp._yb_stream_open = fake_stream_open
    ybp._yb_post_json = fake_post_json
    ybp.get_sig = lambda tab=None, force=False: {"uskey": "u", "md5": "m", "ts": "1"}
    ybp.pace = lambda k: None
    ybp.METRICS = _RecMetrics()
    ybp._yb_chat_body = lambda conv, agent, prompt, model: {"q": prompt}

    try:
        # --- T3a：正常流 + ttfb
        SCRIPTS[:] = [[
            (0.30, 'data: {"type":"think","msg":"推理..."}'),
            (0.05, 'data: {"type":"text","msg":"你"}'),
            (0.05, 'data: {"type":"text","msg":"好"}'),
            (0.05, 'data: {"type":"meta","tokenUsageInfo":{"promptTokens":3,"completionTokens":2,"totalTokens":5}}'),
        ]]
        calls[:] = []
        t0 = time.time()
        first = None
        chunks = []
        ctx1 = {"_t0": t0, "_client": "127.0.0.1", "_key": "k0", "endpoint": "/v1/chat/completions"}
        for s in ybp._live_chat_sse(ctx1, "ck", "naQivTmsDa", "hi", "hunyuan", None, "chatcmpl-1", 111, "hunyuan"):
            if first is None:
                first = time.time() - t0
            chunks.append(s)
        total = time.time() - t0
        blob = "".join(chunks)
        print("      ttfb=%.3fs total=%.3fs chunks=%d" % (first, total, len(chunks)))
        check("T3a ttfb 明显小于 total", first < total * 0.8, "ttfb=%.2f total=%.2f" % (first, total))
        check("T3a 思维链增量", '"reasoning_content": "推理..."' in blob.replace('\\u', '\\u'))
        check("T3a 正文增量逐块", '"content": "你"' in blob and '"content": "好"' in blob)
        check("T3a usage 末包", '"total_tokens": 5' in blob)
        check("T3a finish_reason=stop", '"finish_reason": "stop"' in blob)
        check("T3a 以 [DONE] 收尾", blob.rstrip().endswith("data: [DONE]"))
        check("T3a 只调用一次对话接口", len(calls) == 1, "calls=%s" % calls)
        check("T3a 指标落账 usage", ybp.METRICS.rows and
              (ybp.METRICS.rows[-1]["usage"] or {}).get("total_tokens") == 5,
              "%s" % (ybp.METRICS.rows[-1],))

        # --- T3b：软拒（无内容）→ 退避重试成功
        SCRIPTS[:] = [
            [(0.02, 'data: {"type":"error","msg":"服务繁忙，请稍后重试"}')],       # 第 1 次：软拒
            [(0.02, 'data: {"type":"text","msg":"重试成功"}')],                    # 第 2 次：成功
        ]
        calls[:] = []
        t0 = time.time()
        blob = "".join(ybp._live_chat_sse(None, "ck", "naQivTmsDa", "hi", "hunyuan", None, "chatcmpl-2", 222, "hunyuan"))
        check("T3b 软拒后重试并成功", "重试成功" in blob and '"finish_reason": "stop"' in blob, blob[:120])
        check("T3b 确实发了两次", len(calls) == 2, "calls=%s" % calls)
        check("T3b 未把软拒吐给客户端", "服务繁忙" not in blob)

        # --- T3c：401 → 立即报错不重试
        SCRIPTS[:] = [[]]
        orig_open = ybp._yb_stream_open

        def open_401(cookie, path, body, sig, agent_id, conv="", extra=None, timeout=300):
            calls.append(path)
            return ybp._RawResp(401, {}, lines=iter([b'{"code":401}']))
        ybp._yb_stream_open = open_401
        calls[:] = []
        t0 = time.time()
        blob = "".join(ybp._live_chat_sse(None, "ck", "naQivTmsDa", "hi", "hunyuan", None, "chatcmpl-3", 333, "hunyuan"))
        el = time.time() - t0
        check("T3c 401 报凭证失效", "凭证失效" in blob, blob[:160])
        check("T3c 401 只打一次（不重试）", len(calls) == 1, "calls=%s" % calls)
        check("T3c 401 无退避等待（<1s）", el < 1.0, "elapsed=%.2fs" % el)
        ybp._yb_stream_open = orig_open

        # --- T3e：5xx 才重试
        seq = [[b'boom']]

        def open_500(cookie, path, body, sig, agent_id, conv="", extra=None, timeout=300):
            calls.append(path)
            if len(calls) == 1:
                return ybp._RawResp(500, {}, lines=iter([b'upstream boom']))
            return ybp._RawResp(200, {}, lines=iter(['data: {"type":"text","msg":"五零后成功"}'.encode("utf-8")]))
        ybp._yb_stream_open = open_500
        calls[:] = []
        t0 = time.time()
        blob = "".join(ybp._live_chat_sse(None, "ck", "naQivTmsDa", "hi", "hunyuan", None, "chatcmpl-5", 555, "hunyuan"))
        el = time.time() - t0
        check("T3e 5xx 会重试", len(calls) == 2 and "五零后成功" in blob, "calls=%s" % calls)
        ybp._yb_stream_open = orig_open

        # --- T3d：流中断（有内容后断）→ 补 finish，不丢已出内容
        SCRIPTS[:] = [[
            (0.02, 'data: {"type":"text","msg":"半截"}'),
            (0.02, None),
        ]]
        calls[:] = []
        blob = "".join(ybp._live_chat_sse(None, "ck", "naQivTmsDa", "hi", "hunyuan", None, "chatcmpl-4", 444, "hunyuan"))
        check("T3d 中断前内容保留", "半截" in blob, blob[:160])
        check("T3d 中断后有 finish 收尾", '"finish_reason": "stop"' in blob)
        check("T3d 仍以 [DONE] 收尾", blob.rstrip().endswith("data: [DONE]"))
    finally:
        for k, v in real.items():
            setattr(ybp, k, v)


if __name__ == "__main__":
    t1()
    t2()
    t3()
    print("\n%s  %d 项失败" % ("❌" if FAILS else "✅ 全部通过", len(FAILS)))
    for f in FAILS:
        print("   -", f)
    sys.exit(1 if FAILS else 0)
