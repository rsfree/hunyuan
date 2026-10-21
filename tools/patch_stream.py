#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""patch_stream.py — A 方案「真流式」改造补丁（幂等、带断言）。
用法：python3 tools/patch_stream.py
改完自检：python3 -c "import ast;ast.parse(open('yuanbao_openai_proxy.py').read())"
"""
import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TARGET = os.path.join(ROOT, "yuanbao_openai_proxy.py")

src = io.open(TARGET, encoding="utf-8").read()
orig = src


def sub(label, old, new, count=1):
    global src
    n = src.count(old)
    assert n == count, "【%s】匹配数 %d != 期望 %d" % (label, n, count)
    src = src.replace(old, new)
    print("  ok  %-34s (%d)" % (label, n))


# ---------------------------------------------------------------- 1. 流式出站原语
ANCHOR1 = '''    raise RuntimeError("代理池全部不可用且直连失败: " + " → ".join(order))
YB_DATA_PLANE = os.environ.get("YB_DATA_PLANE", "auto")'''

NEW1 = '''    raise RuntimeError("代理池全部不可用且直连失败: " + " → ".join(order))


class _RawResp:
    """流式出站响应垫片（urllib HTTPResponse / requests Response 统一）。
    契约：.status .headers .read() .iter_lines() -> bytes 行 .close()"""

    def __init__(self, status, headers, reader=None, lines=None, close=None):
        self.status = status
        self.headers = headers
        self._reader = reader
        self._lines = lines
        self._close = close

    def read(self):
        if self._reader is not None:
            return self._reader.read()
        if self._lines is None:
            return b""
        return b"".join(self.iter_lines())

    def iter_lines(self):
        """逐行产出 SSE 行（已剥离 CRLF），不等到 body 收完。"""
        if self._lines is not None:
            for ln in self._lines:
                if isinstance(ln, (bytes, bytearray)):
                    yield bytes(ln).rstrip(b"\\r\\n")
                else:
                    yield str(ln).rstrip("\\r\\n").encode("utf-8")
            return
        if self._reader is None:
            return
        while True:
            ln = self._reader.readline()
            if not ln:
                break
            yield ln.rstrip(b"\\r\\n")

    def close(self):
        try:
            if self._close is not None:
                self._close()
            elif self._reader is not None and hasattr(self._reader, "close"):
                self._reader.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()
        return False


def _open_socks_stream(req, proxy_url: str, timeout: int):
    """socks5/socks5h 流式出站（requests stream=True；iter_lines 内部已解压）。"""
    import requests
    resp = requests.request(
        req.get_method(), req.full_url, headers=dict(req.headers),
        data=getattr(req, "data", None),
        proxies={"http": proxy_url, "https": proxy_url},
        timeout=timeout, verify=False, stream=True,
    )
    return _RawResp(resp.status_code, resp.headers,
                    lines=resp.iter_lines(chunk_size=8192, decode_unicode=False),
                    close=resp.close)


def _open_stream(req, timeout: int = 300):
    """流式出站（不预读 body）。与 _open 同款代理池轮换；
    仅在"连不上"时换下一个/直连兜底，HTTP 4xx/5xx 原样返回（body 仍可流式读）。"""
    order = []
    pool = _proxy_list()
    n = len(pool) or 1
    for _ in range(n + 1):
        pr = _proxy_next()
        order.append(pr or "(直连)")
        fresh = _ureq.Request(req.full_url, data=getattr(req, "data", None),
                              headers=dict(req.headers), method=req.get_method())
        try:
            if pr and pr.split("://", 1)[0].lower().startswith("socks"):
                return _open_socks_stream(fresh, pr, timeout)
            handler = _ureq.ProxyHandler({"http": pr, "https": pr}) if pr else _ureq.ProxyHandler({})
            try:
                r = _ureq.build_opener(handler).open(fresh, timeout=timeout)
            except _uerr.HTTPError as he:
                r = he                      # 非 2xx 也是可流式读的响应
            status = getattr(r, "status", None) or getattr(r, "code", 0)
            return _RawResp(status, r.headers, reader=r, close=r.close)
        except Exception:
            if pr:
                _proxy_mark_bad(pr, 60)
                continue
            raise
    raise RuntimeError("代理池全部不可用且直连失败: " + " → ".join(order))


YB_DATA_PLANE = os.environ.get("YB_DATA_PLANE", "auto")'''

sub("流式出站原语", ANCHOR1, NEW1)

# ---------------------------------------------------------------- 2. 单行事件解析 + 流式 POST
ANCHOR2 = '''    return {"urls": urls, "wmUrls": wm_urls, "text": "".join(text_parts), "error": error}

# ---------------- bsk 桥接 ----------------'''

NEW2 = '''    return {"urls": urls, "wmUrls": wm_urls, "text": "".join(text_parts), "error": error}


def _yb_stream_open(cookie: str, path: str, body: dict, sig: Optional[dict], agent_id: str,
                    conv: str = "", extra: Optional[dict] = None, timeout: int = 300):
    """出站 POST /api/... 的**流式**版本：立刻返回 _RawResp，body 边到边读（真流式关键）。"""
    h = _yb_headers(cookie, sig, agent_id, conv, extra)
    h["Accept-Encoding"] = "identity"     # 禁止 gzip，避免代理层缓冲住 SSE 分片
    req = _ureq.Request("https://yuanbao.tencent.com" + path,
                        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                        headers=h, method="POST")
    return _open_stream(req, timeout=timeout)


def _yb_line_event(line) -> Optional[dict]:
    """元宝 SSE 单行 → 归一化事件 {kind: text|think|meta|error|image, ...}（None=忽略）。"""
    s = line.decode("utf-8", "replace") if isinstance(line, (bytes, bytearray)) else str(line)
    s = s.strip()
    if not s:
        return None
    if s.startswith("event: error"):
        return {"kind": "error", "msg": "元宝返回错误事件"}
    if not s.startswith("data: "):
        return None
    payload = s[6:].strip()
    if payload == "[DONE]" or not payload.startswith("{"):
        return None
    try:
        obj = json.loads(payload)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    t = obj.get("type")
    if t == "text" and obj.get("msg") is not None:
        return {"kind": "text", "msg": obj["msg"]}
    if t == "think" and obj.get("msg") is not None:
        return {"kind": "think", "msg": obj["msg"]}
    if t == "meta":
        tok = obj.get("tokenUsageInfo") or {}
        ev = {"kind": "meta", "usage": {
            "prompt_tokens": tok.get("promptTokens", 0),
            "completion_tokens": tok.get("completionTokens", 0),
            "total_tokens": tok.get("totalTokens", 0),
        }}
        if obj.get("modelErrorCode"):
            ev["error"] = obj.get("modelErrorMsg") or ("modelErrorCode=%s" % obj["modelErrorCode"])
        return ev
    if t == "error":
        return {"kind": "error", "msg": obj.get("msg") or "服务繁忙"}
    if t == "replace" and (obj.get("replace") or {}).get("multimedias"):
        ms = obj["replace"]["multimedias"]
        return {"kind": "image",
                "urls": [m.get("originUrl") or m.get("url") for m in ms if m.get("url")],
                "wmUrls": [m.get("url") for m in ms if m.get("url")]}
    return None


# ---------------- bsk 桥接 ----------------'''

sub("单行事件解析 + 流式 POST", ANCHOR2, NEW2)

# ---------------------------------------------------------------- 3. 真流式生成器 + 指标收尾
ANCHOR3 = '''        return result


# ---------------- 元宝调用封装 ----------------'''

NEW3 = '''        return result


def _cookie_mode_stream(cookie: str, agent_id: str, prompt: str, chat_model: str,
                        tab: Optional[str] = None, timeout: int = 300):
    """真流式（透传模式 · 纯文本聊天）：边收元宝 SSE 边产出事件。

    产出事件（dict）：
      {"delta": {"content"|"reasoning_content": str}}   # 正文/思维链增量
      {"usage": {...}}                                  # meta 事件（末尾一条）
      {"error": str, "status": int?}                    # 失败（软拒/401/中斷）
      {"finish": "stop"}                                # 正常结束
    本次创建的会话在结束（含客户端中断触发 GeneratorExit）时统一删除。
    软拒（"服务繁忙"）且**尚无内容产出**时退避重试（重铸签名）。"""
    pace("ck:" + cookie[:16])
    if tab is None and YB_MINT_BACKEND != "cdp":
        tab = _ensure_page()["tabId"]
    for attempt in range(YB_SOFTRETRY + 1):
        with _bsk_lock:
            sig = get_sig(tab, force=attempt > 0)
        conv, created = None, False
        if YUANBAO_CONVERSATION:
            conv = YUANBAO_CONVERSATION
        elif YB_CREATE_CONV == "1":
            r = _yb_post_json(cookie, "/api/user/agent/conversation/create",
                              {"agentId": agent_id}, sig, agent_id)
            if r["status"] != 200:
                yield {"error": ("元宝凭证失效（HTTP 401）" if r["status"] == 401
                                 else "创建会话失败 HTTP %d" % r["status"]),
                       "status": r["status"]}
                return
            try:
                conv = json.loads(r["text"]).get("id")
            except Exception:
                conv = None
            if not conv:
                yield {"error": "创建会话失败: " + r["text"][:150]}
                return
            created = True
        else:
            conv = str(uuid.uuid4())
            created = True

        emitted, err, status = False, None, 200
        body = _yb_chat_body(conv, agent_id, prompt, chat_model)
        try:
            resp = _yb_stream_open(cookie, "/api/chat/%s" % conv, body, sig, agent_id, conv, timeout=timeout)
        except Exception as e:
            resp, err, status = None, ("上游连接失败: %s" % (str(e)[:160])), 502
        if resp is not None:
            status = resp.status or 0
            if status != 200:
                try:
                    detail = resp.read().decode("utf-8", "replace")[:200]
                except Exception:
                    detail = ""
                finally:
                    resp.close()
                err = ("元宝凭证失效（HTTP 401）" if status == 401
                       else "元宝 HTTP %d %s" % (status, detail))
            else:
                try:
                    for line in resp.iter_lines():
                        ev = _yb_line_event(line)
                        if not ev:
                            continue
                        kind = ev["kind"]
                        if kind == "text":
                            emitted = True
                            yield {"delta": {"content": ev["msg"]}}
                        elif kind == "think":
                            yield {"delta": {"reasoning_content": ev["msg"]}}
                        elif kind == "meta":
                            if ev.get("usage"):
                                yield {"usage": ev["usage"]}
                            if ev.get("error"):
                                err = ev["error"]
                        elif kind == "error":
                            err = ev["msg"]
                except GeneratorExit:
                    raise
                except Exception as e:
                    err = err or ("上游流中断: %s" % (str(e)[:160]))
                finally:
                    resp.close()
        if created and YB_DELETE_CONV == "1":
            try:
                _yb_post_json(cookie, "/api/user/agent/conversation/v1/delete",
                              {"cid": conv}, sig, agent_id, timeout=30)
            except Exception:
                pass

        if err and not emitted and attempt < YB_SOFTRETRY:
            time.sleep(2.0 * (attempt + 1))
            continue
        if err:
            yield {"error": err, "status": status}
        else:
            yield {"finish": "stop"}
        return


def _live_chat_sse(mctx, cookie: str, agent_id: str, prompt: str, chat_model: str,
                   tab, comp_id: str, created: int, model_in: str):
    """把 _cookie_mode_stream 的事件流翻成 OpenAI chat.completion.chunk SSE（真流式转发）。"""
    usage_seen, err_seen, status, emitted = None, None, 200, False
    try:
        for ev in _cookie_mode_stream(cookie, agent_id, prompt, chat_model, tab=tab):
            if "delta" in ev:
                if ev["delta"].get("content"):
                    emitted = True
                obj = {"id": comp_id, "object": "chat.completion.chunk", "created": created,
                       "model": model_in, "choices": [{"index": 0, "delta": ev["delta"]}]}
                yield "data: %s\\n\\n" % json.dumps(obj, ensure_ascii=False)
            elif "usage" in ev:
                usage_seen = ev["usage"]
            elif ev.get("error"):
                err_seen = ev["error"]
                status = ev.get("status") or 502
            elif "finish" in ev:
                done = {"id": comp_id, "object": "chat.completion.chunk", "created": created,
                        "model": model_in,
                        "choices": [{"index": 0, "delta": {}, "finish_reason": ev["finish"]}]}
                yield "data: %s\\n\\n" % json.dumps(done, ensure_ascii=False)
    except GeneratorExit:
        raise
    except Exception as e:
        err_seen = err_seen or ("流式转发异常: %s" % (str(e)[:200]))
        status = 502
        print("[stream] 异常: %r" % (e,), flush=True)

    if err_seen and not emitted:
        # 内容零产出 → 直接给一条 OpenAI 风格 error 分片（客户端会据此报错）
        yield "data: %s\\n\\n" % json.dumps(
            {"error": {"message": err_seen, "type": "api_error"}}, ensure_ascii=False)
    elif err_seen:
        done = {"id": comp_id, "object": "chat.completion.chunk", "created": created,
                "model": model_in, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
        yield "data: %s\\n\\n" % json.dumps(done, ensure_ascii=False)
    if usage_seen:
        uobj = {"id": comp_id, "object": "chat.completion.chunk", "created": created,
                "model": model_in, "choices": [], "usage": usage_seen}
        yield "data: %s\\n\\n" % json.dumps(uobj, ensure_ascii=False)
    yield "data: [DONE]\\n\\n"

    if mctx is not None:
        mctx["usage"] = usage_seen
        mctx["err"] = err_seen
    _metrics_finalize(mctx, 200 if (not err_seen or emitted) else status)


def _metrics_finalize(ctx, status: int = 200):
    """流式响应由生成器自己落统计（middleware 在 body 消费前就返回了，拿不到 usage）。"""
    if METRICS is None or ctx is None:
        return
    try:
        t0 = ctx.get("_t0") or time.time()
        METRICS.record({
            "ts": int(t0), "instance": YB_INSTANCE_NAME_ENV,
            "endpoint": ctx.get("endpoint"), "model": ctx.get("model") or "?",
            "status": status, "ok": 200 <= status < 300,
            "ms": int((time.time() - t0) * 1000),
            "stream": True,
            "usage": ctx.get("usage"), "err": ctx.get("err"),
            "client": ctx.get("_client") or "", "key": ctx.get("_key") or "anon",
        })
    except Exception:
        pass


# ---------------- 元宝调用封装 ----------------'''

sub("真流式生成器 + 指标收尾", ANCHOR3, NEW3)

# ---------------------------------------------------------------- 4. middleware：支持流式自记账
ANCHOR4 = '''    t0 = time.time()
    ctx = {"endpoint": path, "model": None, "stream": False, "usage": None, "err": None}'''
NEW4 = '''    t0 = time.time()
    ctx = {"endpoint": path, "model": None, "stream": False, "usage": None, "err": None,
           "_t0": t0, "_client": (request.client.host if request.client else ""),
           "_key": _key_tag(request), "_stream_owned": False}'''
sub("middleware ctx 扩展", ANCHOR4, NEW4)

ANCHOR5 = '''    finally:
        _MCTX.reset(tok)
        try:
            METRICS.record({
                "ts": int(t0), "instance": YB_INSTANCE_NAME_ENV,
                "endpoint": path, "model": ctx.get("model") or "?",
                "status": status, "ok": 200 <= status < 300,
                "ms": int((time.time() - t0) * 1000),
                "stream": bool(ctx.get("stream")),
                "usage": ctx.get("usage"), "err": ctx.get("err"),
                "client": (request.client.host if request.client else ""),
                "key": _key_tag(request),
            })
        except Exception:
            pass'''
NEW5 = '''    finally:
        _MCTX.reset(tok)
        if not ctx.get("_stream_owned"):
            try:
                METRICS.record({
                    "ts": int(t0), "instance": YB_INSTANCE_NAME_ENV,
                    "endpoint": path, "model": ctx.get("model") or "?",
                    "status": status, "ok": 200 <= status < 300,
                    "ms": int((time.time() - t0) * 1000),
                    "stream": bool(ctx.get("stream")),
                    "usage": ctx.get("usage"), "err": ctx.get("err"),
                    "client": ctx.get("_client") or "",
                    "key": ctx.get("_key") or "anon",
                })
            except Exception:
                pass'''
sub("middleware 让位流式自记账", ANCHOR5, NEW5)

# ---------------------------------------------------------------- 5. 路由：真流式分支
ANCHOR6 = '''    conv_created = None  # 本次新建的会话（用完即删）
    try:
        if cookie_str:'''
NEW6 = '''    # ---- 真流式优先：透传模式 · 纯文本聊天（边收元宝 SSE 边转发，首字节 ~1s）----
    if stream and cookie_str and not image_flow and not unwm:
        if YB_MINT_BACKEND == "cdp":
            _tab, _agent = None, "naQivTmsDa"
        else:
            _pg = _ensure_page()
            _tab, _agent = _pg["tabId"], _pg["agentId"]
        _m = _MCTX.get()
        if _m is not None:
            _m["_stream_owned"] = True
        return StreamingResponse(
            _live_chat_sse(_m, cookie_str, _agent, prompt, chat_model, _tab, comp_id, created, model_in),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    conv_created = None  # 本次新建的会话（用完即删）
    try:
        if cookie_str:'''
sub("路由真流式分支", ANCHOR6, NEW6)

# ---------------------------------------------------------------- 6. 文档头同步
sub("docstring", "  - 聊天为\"缓冲伪流式\"：等元宝完整回答后按增量 chunk 快速回放",
    "  - 纯文本聊天走**真流式**：边收元宝 SSE 边转发 OpenAI 分片（首字节 ~1s）")

assert src != orig
io.open(TARGET, "w", encoding="utf-8").write(src)
print("\n写入完成:", TARGET)
