#!/usr/bin/env python3
"""
yuanbao_openai_proxy.py — 腾讯元宝 (yuanbao.tencent.com) → OpenAI API 适配器

架构：
  OpenAI 客户端 → 本代理 (FastAPI) → bsk CLI → 元宝页面上下文 (页内签名 + 页内 fetch + SSE)
  - 签名三件套 (X-Uskey / X-Bus-Params-Md5 / X-Timestamp) 由页面 webpack 里的
    module28850.TE(module77004.PU) 现场铸造（Qimei SDK getUSKeySync，无法脱离页面复现）
  - 请求经页内 fetch 发出（自动携带登录 cookie），响应 SSE 全量收完后转译为 OpenAI 格式
  - 聊天为"缓冲伪流式"：等元宝完整回答后按增量 chunk 快速回放

依赖：本机已装 bsk CLI 且浏览器扩展在线；pip install fastapi uvicorn

启动：
  YUANBAO_TAB_ID=auto YUANBAO_AGENT_ID=auto python3 yuanbao_openai_proxy.py
  # 默认端口 8177；TAB/agentId 留 auto 会自动探测

鉴权：
  客户端 → 本代理：Authorization: Bearer <key>。
    - YUANBAO_API_KEY 未设 → 不校验，任何 Bearer 都放行
    - 设了 → 必须精确匹配，否则 401（OpenAI 错误体）
  注意：该 key 仅是本代理的门禁（new-api 渠道必带 Bearer），不会转发给元宝；
  元宝凭证 = 浏览器登录会话（cookie + 页内签名），hy_token 单传实测 401 无效。

端点：
  GET  /v1/models
  POST /v1/chat/completions        (stream / non-stream)
  #   纯文本 → 元宝聊天；messages 带 image_url → 图生图（约定同 image-adapter：
  #   system 前置、最后一条 user 生效、assistant 忽略；返回 content parts 携带 image_url）
  POST /v1/images/generations      (dall-e-3 → 元宝 Hy Image 3.5)
"""
import os
import re
import json
import asyncio
import time
import uuid
import subprocess
import threading
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse, Response

# ---------------- 配置 ----------------
BSK_SESSION = os.environ.get("BSK_SESSION", "")          # 留空自动用当前可达 daemon 会话
YUANBAO_TAB_ID = os.environ.get("YUANBAO_TAB_ID", "auto")  # agent 窗口里的元宝 tab
YUANBAO_AGENT_ID = os.environ.get("YUANBAO_AGENT_ID", "auto")  # naQivTmsDa
YUANBAO_CONVERSATION = os.environ.get("YUANBAO_CONVERSATION", "")  # 固定会话；空=每请求新建
YUANBAO_API_KEY = os.environ.get("YUANBAO_API_KEY", "")  # 门禁 key；空=不校验（客户端 Bearer 随意）
YUANBAO_TEMP_CONV = os.environ.get("YUANBAO_TEMP_CONV", "1")  # 1=临时会话(不进历史，反风控)；0=普通
YB_DELETE_CONV = os.environ.get("YB_DELETE_CONV", "1")    # 1=生成完自动删除本次创建的会话（历史零残留）
YB_CREATE_CONV = os.environ.get("YB_CREATE_CONV", "1")
YB_SIG_TTL = float(os.environ.get("YB_SIG_TTL", "60"))    # 签名三件套复用秒数（实测可复用，避免每请求铸签）
YB_MINT_BACKEND = os.environ.get("YB_MINT_BACKEND", "cdp")  # cdp（默认，headless Chrome 铸签，无 bsk）| bsk（遗留）| http（远程 minter）
YB_COOKIE_FILE = os.environ.get("YB_COOKIE_FILE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookie.txt"))  # 默认凭证文件（无 bsk 数据面）
YB_DATA_PLANE = os.environ.get("YB_DATA_PLANE", "auto")  # auto|page|cookie：auto=cdp 走页内、bsk 走页内+文件兜底
YB_MINTER_URL = os.environ.get("YB_MINTER_URL", "")   # 远程 minter 服务（YB_MINT_BACKEND=http 时用）
YB_MINTER_TOKEN = os.environ.get("YB_MINTER_TOKEN", "")  # minter 服务鉴权（X-Minter-Key）
YB_MIN_INTERVAL = float(os.environ.get("YB_MIN_INTERVAL", "2"))  # 同凭证两请求最小间隔秒（限速）
YB_SOFTRETRY = int(os.environ.get("YB_SOFTRETRY", "1"))   # 软拒("服务繁忙")自动退避重试次数
PORT = int(os.environ.get("YUANBAO_PROXY_PORT", "8177"))

# 静态头骨架：易变字段（UA/os_version/webversion/commit-tag/设备三件）运行时从页面动态覆盖
STATIC_HEADERS = {
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
}

# OpenAI 风格模型名 → 元宝 chatModelId
MODEL_MAP = {
    "hunyuan": "hunyuan_gpt_175B_0404",
    "hunyuan-t1": "hunyuan_t1",
    "hunyuan-turbo": "hunyuan_gpt_175B_0404",
    "deepseek-v3": "deep_seek_v3",
    "deepseek-chat": "deep_seek_v3",
    "deepseek-r1": "deep_seek",
    "deepseek-reasoner": "deep_seek",
    "dall-e-3": "hunyuan_gpt_175B_0404",  # 生图走 images 端点
    # hy-image 系列别名：chat 门带这些名字 → 生图管道；images 端点原样接受
    "hy-image": "hunyuan_gpt_175B_0404",
    "hy-image-3.5": "hunyuan_gpt_175B_0404",
    "hy-image-v3.5": "hunyuan_gpt_175B_0404",
    "hy-image-v3.5-preview": "hunyuan_gpt_175B_0404",
    "hy-image-4": "hunyuan_gpt_175B_0404",
}

IMAGE_MODEL_PREFIXES = ("hy-image", "dall-e")


def _is_image_model(name: str) -> bool:
    """hy-image-* / dall-e-* 系列一律视为生图模型（hy-image-* 通配别名）。
    排除去水印专用名（hy-image-unwatermark / removewatermark）。"""
    n = (name or "").lower()
    if n in ("hy-image-unwatermark", "removewatermark"):
        return False
    return n.startswith(IMAGE_MODEL_PREFIXES)

app = FastAPI(title="yuanbao-openai-proxy")


# ---------------- 鉴权与凭证路由 ----------------
def _bearer_value(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    return auth[7:].strip() if auth.lower().startswith("bearer ") else ""


def _looks_like_cookie(v: str) -> bool:
    """cookie 串粗判：含 '=' 且含 '; '（多对）或单对且长度>40。"""
    return ("=" in v) and (";" in v or len(v) > 80)


def _check_auth(request: Request):
    """凭证路由（返回 None=放行+走浏览器会话；(mode, value)=凭证模式；JSONResponse=拒绝）。

    Authorization: Bearer <value> 三种情形：
      1. value 是 cookie 串（含 '=' 且 ';'）→ 元宝凭证透传：数据面走代理出站 HTTP
      2. value == YUANBAO_API_KEY（设了门禁时）→ 走浏览器会话
      3. 其余：YUANBAO_API_KEY 未设 → 放行走浏览器会话；设了 → 401
    另支持 X-Yuanbao-Cookie 头直接放 cookie 串（优先级高于 Bearer 的 cookie 判定）。"""
    yb_cookie = request.headers.get("x-yuanbao-cookie", "").strip()
    if yb_cookie:
        return ("cookie", yb_cookie)
    value = _bearer_value(request)
    if value and _looks_like_cookie(value):
        return ("cookie", value)
    if YUANBAO_API_KEY:
        if value == YUANBAO_API_KEY:
            return None
        return JSONResponse(
            {"error": {"message": "Incorrect API key provided. Pass 'Authorization: Bearer <YUANBAO_API_KEY>' or a yuanbao cookie string.",
                       "type": "invalid_request_error", "code": "invalid_api_key"}},
            status_code=401,
        )
    return None


# ---------------- 出站 HTTP（凭证透传模式：客户端 cookie + 页内铸签名） ----------------
import base64 as _b64mod
import urllib.request as _ureq
import urllib.error as _uerr

_OPENER = _ureq.build_opener(_ureq.ProxyHandler({}))  # 直连，不吃环境代理（信任边界同 curl --noproxy '*'）


def _yb_headers(cookie: str, sig: Optional[dict], agent_id: str, conv: str = "", extra: Optional[dict] = None) -> dict:
    h = {**STATIC_HEADERS, **(_DYN_HEADERS or {})}
    h.update({
        "Cookie": cookie,
        "content-type": "text/plain;charset=UTF-8",
        "Origin": "https://yuanbao.tencent.com",
        "Referer": "https://yuanbao.tencent.com/chat/" + agent_id,
        "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36",
        "X-AgentID": (agent_id + "/" + conv) if conv else agent_id,
    })
    if sig:
        h.update({"X-Uskey": sig["uskey"], "X-Bus-Params-Md5": sig["md5"], "X-Timestamp": sig["ts"]})
    if extra:
        h.update(extra)
    return h


def _yb_post_json(cookie: str, path: str, body: dict, sig: Optional[dict], agent_id: str, conv: str = "", extra: Optional[dict] = None, timeout: int = 300) -> dict:
    """出站 POST /api/...（text/plain 载荷，同元宝 web），返回 {status, text}。
    非 2xx 不抛异常（返回状态码），由上层统一映射（如 401 → 凭证过期）。"""
    h = _yb_headers(cookie, sig, agent_id, conv, extra)
    req = _ureq.Request("https://yuanbao.tencent.com" + path,
                        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                        headers=h, method="POST")
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            return {"status": r.status, "text": r.read().decode("utf-8", "replace")}
    except _uerr.HTTPError as e:
        return {"status": e.code, "text": e.read().decode("utf-8", "replace")}


def _yb_upload_ref(cookie: str, agent_id: str, b64_data: str, name: str, mime: str) -> dict:
    """出站上传参考图：genUploadInfo（无需签名）→ COS PUT（putAuthorization）→ multimedia entry。"""
    info = _yb_post_json(cookie, "/api/resource/genUploadInfo",
                         {"fileName": name, "docFrom": "localDoc", "docOpenId": "", "needAuth": True},
                         None, agent_id, timeout=60)
    info = json.loads(info["text"])
    if not info.get("cosURL") or not info.get("putAuthorization"):
        raise RuntimeError("genUploadInfo 异常: " + json.dumps(info, ensure_ascii=False)[:150])
    blob = _b64mod.b64decode(b64_data)
    req = _ureq.Request(info["cosURL"], data=blob, method="PUT", headers={
        "Authorization": info["putAuthorization"],
        "Content-Type": mime,
        "user-agent": "Mozilla/5.0",
    })
    try:
        with _OPENER.open(req, timeout=120) as r:
            r.read()
    except _uerr.HTTPError as e:
        raise RuntimeError(f"COS PUT {e.code}")
    # 尺寸：PNG/JPEG 头解析（避免引 PIL）
    w = h = 0
    if mime == "image/png" and blob[:8] == b"\x89PNG\r\n\x1a\n":
        w, h = int.from_bytes(blob[16:20], "big"), int.from_bytes(blob[20:24], "big")
    elif mime == "image/jpeg" and blob[:2] == b"\xff\xd8":
        i = 2
        while i < len(blob) - 9:
            if blob[i] == 0xFF and blob[i+1] in (0xC0, 0xC2):
                h, w = int.from_bytes(blob[i+5:i+7], "big"), int.from_bytes(blob[i+7:i+9], "big")
                break
            i += 2 + int.from_bytes(blob[i+2:i+4], "big")
    fid = os.urandom(6).hex()
    return {"type": "image", "docType": "image", "url": info["resourceUrl"], "signUrl": info["cosURL"],
            "fileName": name, "size": len(blob), "width": w, "height": h, "fileId": fid,
            "uploadStatus": "success", "progress": 100}


def _parse_yb_sse_common(text: str) -> dict:
    """出站 SSE → {urls, wmUrls, text, error}（聊天/生图两用）。"""
    urls, wm_urls, text_parts, error = [], [], [], None
    for line in text.split("\n"):
        line = line.strip()
        if not line.startswith("data: "):
            if line.startswith("event: error"):
                error = error or "元宝返回错误事件"
            continue
        payload = line[6:].strip()
        if payload == "[DONE]" or not payload.startswith("{"):
            continue
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            continue
        t = obj.get("type")
        if t == "text" and obj.get("msg") is not None:
            text_parts.append(obj["msg"])
        elif t == "replace" and obj.get("replace", {}).get("multimedias"):
            ms = obj["replace"]["multimedias"]
            fresh = [m.get("originUrl") or m.get("url") for m in ms if m.get("url")]
            fresh_wm = [m.get("url") for m in ms if m.get("url")]
            if fresh:
                urls.clear(); urls.extend(fresh)
            if fresh_wm:
                wm_urls.clear(); wm_urls.extend(fresh_wm)
        elif t == "error":
            error = obj.get("msg") or "服务繁忙"
    return {"urls": urls, "wmUrls": wm_urls, "text": "".join(text_parts), "error": error}

# ---------------- bsk 桥接 ----------------
_bsk_lock = threading.Lock()
_cached_tab_id: Optional[str] = None
_cached_agent_id: Optional[str] = None
_bsk_session: Optional[str] = None


def _session() -> str:
    """惰性创建/复用一个 bsk 会话（agent 窗口）。"""
    global _bsk_session
    if BSK_SESSION:
        return BSK_SESSION
    if _bsk_session:
        return _bsk_session
    out = _run(["bsk", "session", "start", "--json", "--no-focus"])
    i = out.find("{")
    if i < 0:
        raise RuntimeError(f"bsk session start 失败: {out[:200]}")
    _bsk_session = json.loads(out[i:])["session_id"]
    return _bsk_session


def _run(cmd: list, timeout: int = 120) -> str:
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return r.stdout.strip()


def _ev(expr: str, tab_id: Optional[str] = None, timeout: int = 180):
    """在元宝页面执行 JS，返回反序列化后的值。"""
    cmd = ["bsk", "evaluate", expr, "--json", "--session", _session()]
    tid = tab_id or (YUANBAO_TAB_ID if YUANBAO_TAB_ID != "auto" else _cached_tab_id)
    if tid:
        cmd += ["--tab-id", str(tid)]
    out = _run(cmd, timeout=timeout)
    i = out.find("{")
    if i < 0:
        raise RuntimeError(f"bsk evaluate 无输出: {out[:200]}")
    j = json.loads(out[i:])
    if not j.get("ok"):
        err = j.get("error") or {}
        diag = ""
        if "SyntaxError" in str(err.get("text", "")):
            lines = expr.split("\n")
            ln = err.get("line", 0)
            diag = f" | expr_lines={len(lines)} expr_len={len(expr)} L{ln}={lines[ln-1][:80]!r}" if 0 < ln <= len(lines) else f" | expr_len={len(expr)}"
        raise RuntimeError(f"bsk evaluate 失败: {out[:200]}{diag}")
    return j.get("value")


def _discover_tab() -> str:
    """找到元宝 agent tab；没有则把本会话的空白 agent tab 导航过去。"""
    global _cached_tab_id
    if YUANBAO_TAB_ID != "auto":
        return YUANBAO_TAB_ID
    if _cached_tab_id:
        return _cached_tab_id
    out = _run(["bsk", "tab", "list", "--session", _session()])
    fallback_agent_tab = None
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[1] == "agent":
            if "yuanbao.tencent.com" in line:
                _cached_tab_id = parts[0]
                return _cached_tab_id
            if fallback_agent_tab is None:
                fallback_agent_tab = parts[0]
    if fallback_agent_tab:
        _run(["bsk", "navigate", "https://yuanbao.tencent.com/chat", "--session", _session(), "--tab-id", fallback_agent_tab])
        time.sleep(4)
        _cached_tab_id = fallback_agent_tab
        return _cached_tab_id
    raise RuntimeError("未找到 agent tab，无法打开元宝页面")


def _ensure_page() -> dict:
    """确认 tab 停在元宝页面，返回 {tabId, agentId}；会话/tab 失效时清缓存重试一次。"""
    global _cached_agent_id, _bsk_session, _cached_tab_id
    for attempt in (1, 2):
        try:
            tab = _discover_tab()
            url = _ev("location.href", tab_id=tab)
            if "yuanbao.tencent.com" not in url:
                # tab 被导航走了，拉回来
                cmd = ["bsk", "navigate", "https://yuanbao.tencent.com/chat", "--session", _session(), "--tab-id", str(tab)]
                _run(cmd)
                time.sleep(4)
                url = _ev("location.href", tab_id=tab)
                if "yuanbao.tencent.com" not in url:
                    raise RuntimeError("导航后仍不在元宝页面")
            if YUANBAO_AGENT_ID != "auto":
                agent = YUANBAO_AGENT_ID
            elif _cached_agent_id:
                agent = _cached_agent_id
            else:
                m = re.search(r"/chat/([A-Za-z0-9]+)", url)
                agent = m.group(1) if m else "naQivTmsDa"
                _cached_agent_id = agent
            return {"tabId": tab, "agentId": agent}
        except Exception:
            if attempt == 2:
                raise
            # 会话或 tab 可能已被回收：清缓存重建
            _bsk_session = None
            _cached_tab_id = None
            _cached_agent_id = None
            time.sleep(1)


# ---------------- 风控对策：签名缓存 / 动态指纹 / 限速 / 软拒退避 ----------------
_sig_cache: dict = {}          # {uskey, md5, ts, minted_at}
_fp_cache: dict = {}           # 动态指纹头缓存 {headers, at}
_fp_TTL = 600.0                # 指纹 10 分钟刷新（跟随元宝前端发版）
_last_req_at: dict = {}        # 凭证/会话维度限速 {"browser": ts, cookie前8位: ts}
_softreject_count = 0          # 软拒计数（观测用）

JS_FINGERPRINT = """
(() => {
  const arr = Object.keys(window).filter(k=>k.startsWith('webpackChunk')).map(k=>window[k])[0];
  let req; arr.push([['fp'+Date.now()], {}, (r)=>{req=r}]);
  const modSig = req(77004);
  const sdk = modSig.I5(modSig.PU);
  const h38 = sdk.getLocalQimei36().h38;
  // webversion/commit-tag：从 rumt 上报 URL 里解析（version=2.87.2__02746073-modern）
  let ver = null, tag = null;
  try {
    for (const e of performance.getEntriesByType('resource')) {
      const m = (e.name||'').match(/version=([\\d.]+)__([0-9a-f]+)-/);
      if (m) { ver = m[1]; tag = m[2]; break; }
    }
  } catch (e) {}
  const ua = navigator.userAgent;
  let osv = 'Mac OS(10.15.7)-Blink';
  const m = ua.match(/(Mac OS X|Windows NT [\\d.]+|Android [\\d.]+|CrOS \\w+ [\\d.]+)[^)]*?([\\d__.]+)?/);
  if (ua.includes('Mac OS X') && m) {
    const v = (m[2] || '').replace(/_/g, '.');
    osv = 'Mac OS(' + (v || '10.15.7') + ')-Blink';
  } else if (ua.includes('Windows')) {
    osv = 'Windows' + (m && m[2] ? '(' + m[2].replace(/_/g,'.') + ')-Blink' : '-Blink');
  }
  return {h38, osv, ver, tag, ua: navigator.userAgent};
})()
"""


def _dynamic_fp(tab: str) -> dict:
    """动态指纹头：UA/os_version/webversion/commit-tag/设备三件 全部跟随真实页面。
    缓存 10 分钟 —— 元宝发版后代理自动跟上，不残留陈旧版本指纹。"""
    global _fp_cache
    now = time.time()
    if _fp_cache.get("headers") and now - _fp_cache.get("at", 0) < _fp_TTL:
        return _fp_cache["headers"]
    try:
        fp = _ev(JS_FINGERPRINT, tab_id=tab)
    except Exception:
        fp = None
    if not fp or not fp.get("h38"):
        return dict(STATIC_HEADERS)  # 页面异常时退回骨架，不阻塞请求
    h = dict(STATIC_HEADERS)
    h["X-HY92"] = fp["h38"]
    h["X-HY93"] = h["X-device-id"]  # 实抓：HY93 == device-id，h38 走 HY92
    h["X-os_version"] = fp.get("osv") or h["X-os_version"]
    if fp.get("ver"):
        h["X-WebVersion"] = fp["ver"]
    if fp.get("tag"):
        h["x-commit-tag"] = fp["tag"]
    h["user-agent"] = fp.get("ua") or h.get("user-agent", "")
    _fp_cache = {"headers": h, "at": now}
    return h


def _pev(js: str, timeout: int = 300):
    """页内执行（执行器无关）：cdp 后端走 headless Chromium 的 CDP，bsk 后端走浏览器桥。"""
    if YB_MINT_BACKEND == "cdp":
        import cdp_minter
        return cdp_minter.get_minter().evaluate(js, timeout_s=timeout)
    ctx = _ensure_page()
    return _ev(js, tab_id=ctx["tabId"], timeout=timeout)


_MINTER = None
_DYN_HEADERS: Optional[dict] = None


def _get_minter():
    """铸签后端（懒加载单例）。bsk 后端需要主代理模块引用（防循环导入）。"""
    global _MINTER
    if _MINTER is None:
        import sys
        import mint_backends
        _MINTER = mint_backends.create_minter(sys.modules[__name__])
    return _MINTER


def get_sig(tab: str, force: bool = False) -> dict:
    """签名三件套缓存复用（TTL 内复用同一套，减少铸签频率；实测可复用）。
    铸签后端由 mint_backends 按环境选择：bsk（本机）/ http（远程 minter 服务）/ cdp（服务器自管 Chromium）。"""
    now = time.time()
    if not force and _sig_cache.get("sig") and now - _sig_cache.get("minted_at", 0) < YB_SIG_TTL:
        return _sig_cache["sig"]
    sig = _get_minter().mint()
    global _DYN_HEADERS
    _DYN_HEADERS = _get_minter().headers() or {}
    _sig_cache.clear()
    _sig_cache.update({"sig": sig, "minted_at": now})
    return sig


def pace(key: str):
    """同凭证限速：距上次请求不足 YB_MIN_INTERVAL 则等待。"""
    last = _last_req_at.get(key, 0)
    wait = YB_MIN_INTERVAL - (time.time() - last)
    if wait > 0:
        time.sleep(wait)
    _last_req_at[key] = time.time()


def is_softreject(result: dict) -> bool:
    """风控软拒识别：HTTP 200 包着 error 事件（服务繁忙/限流）。"""
    if not isinstance(result, dict):
        return False
    if result.get("status") != 200:
        return False
    blob = result.get("raw") or result.get("text") or ""
    return ("服务繁忙" in blob) or ('"type":"error"' in blob and not result.get("urls"))


def with_softretry(fn, sig_tab: str, cookie_mode: bool = False):
    """软拒自动退避重试：识别"服务繁忙"→ 强制重铸签名 + 指数退避 → 重试。"""
    global _softreject_count
    result = fn()
    attempt = 0
    while is_softreject(result) and attempt < YB_SOFTRETRY:
        attempt += 1
        _softreject_count += 1
        wait = 2.0 * attempt
        time.sleep(wait)
        # 强制刷新签名（软拒常因签名时效/风控计数）
        if not cookie_mode:
            try:
                get_sig(sig_tab, force=True)
            except Exception:
                pass
        result = fn()
    return result


# ---------------- 页内 JS 片段 ----------------
JS_BOOT = """
(() => {
  const arr = Object.keys(window).filter(k=>k.startsWith('webpackChunk')).map(k=>window[k])[0];
  if (!arr) throw new Error('webpack runtime 未找到');
  let req; arr.push([['ybproxy'+Date.now()], {}, (r)=>{req=r}]);
  const modSig = req(77004);
  const modHdr = req(28850);
  const sig = modHdr.TE(modSig.PU);
  return {
    uskey: sig['X-Uskey'],
    md5: sig['X-Bus-Params-Md5'].toString(),
    ts: String(sig['X-Timestamp'])
  };
})()
"""

JS_CREATE_CONV = """
(async (agentId) => {
  const r = await (await fetch('/api/user/agent/conversation/create', {
    method: 'POST', headers: {'content-type': 'application/json'},
    body: JSON.stringify({agentId})
  })).json();
  if (!r.id) throw new Error('创建会话失败: ' + JSON.stringify(r).slice(0,200));
  return r.id;
})
"""

JS_CHAT = """
(async (p) => {
  const {conv, agentId, prompt, chatModelId, temp, sig} = p;
  const BASE = 'hunyuan_gpt_175B_0404';
  const chatModelExtInfo = JSON.stringify({
    modelId: BASE,
    agentModeModelSetting: {modelId: chatModelId},
    supportFunctions: {internetSearch: ''},
    internetSearch: 'autoInternetSearch'
  });
  const body = {
    model: 'gpt_175B_0404',
    prompt, plugin: '', displayPrompt: prompt, displayPromptType: 1,
    agentId, isTemporary: !!temp, projectId: '',
    chatModelId,
    supportFunctions: ['openAutoSearchSwitch', 'autoInternetSearch'],
    docOpenid: '',
    options: {imageIntention: {needIntentionModel: true, backendUpdateFlag: 2, intentionStatus: true}},
    multimedia: [], supportHint: 1,
    chatModelExtInfo,
    applicationIdList: [], version: 'v2', extReportParams: null,
    isAtomInput: false, conversationId: conv,
    offsetOfHour: 8, offsetOfMinute: 0
  };
  const headers = Object.assign({}, __ybStaticHeaders, {
    'X-AgentID': agentId + '/' + conv,
    'X-Uskey': sig.uskey,
    'X-Bus-Params-Md5': sig.md5,
    'X-Timestamp': sig.ts,
    'content-type': 'text/plain;charset=UTF-8'
  });
  const resp = await fetch('/api/chat/' + conv, {method: 'POST', headers, body: JSON.stringify(body)});
  const text = await resp.text();
  return {status: resp.status, text};
})
"""

JS_IMAGE = """
(async (p) => {
  const {conv, agentId, prompt, resolution, ratio, sig} = p;
  const igen = {model: 'Hy Image 3.5', resolution: resolution || '1.5K'};
  if (ratio) igen.ratio = ratio;
  const body = {
    model: 'gpt_175B_0404',
    prompt: '帮我生成图片：' + prompt,
    plugin: 'Adaptive',
    displayPrompt: '帮我生成图片：' + prompt,
    displayPromptType: 1,
    question: '帮我生成图片：' + prompt,
    skillIdParam: 'ai_image',
    msgScene: 13,
    chatModelId: 'hunyuan_gpt_175B_0404',
    chatModelExtInfo: JSON.stringify({modelId: 'hunyuan_gpt_175B_0404', supportFunctions: {internetSearch: ''}, internetSearch: 'autoInternetSearch'}),
    extra: {image_gen_param: igen},
    agentId, isTemporary: false, projectId: '',
    supportFunctions: ['openAutoSearchSwitch', 'autoInternetSearch'],
    docOpenid: '',
    options: {imageIntention: {needIntentionModel: true, backendUpdateFlag: 2, intentionStatus: true}},
    multimedia: [], supportHint: 1,
    applicationIdList: ['application_id_ai_image'],
    skillId: 'ai_image',
    chatSource: 'ai_image',
    version: 'v2', extReportParams: null, isAtomInput: false,
    conversationId: conv,
    offsetOfHour: 8, offsetOfMinute: 0
  };
  const headers = Object.assign({}, __ybStaticHeaders, {
    'X-Event-Input-Type': '15',
    'X-AgentID': agentId + '/' + conv,
    'X-Uskey': sig.uskey,
    'X-Bus-Params-Md5': sig.md5,
    'X-Timestamp': sig.ts,
    'content-type': 'text/plain;charset=UTF-8'
  });
  const resp = await fetch('/api/chat/' + conv, {method: 'POST', headers, body: JSON.stringify(body)});
  const text = await resp.text();
  // 提取最后一个 replace 事件里的图片 URL（最终事件携带带签名的成品图）
  // originUrl = _h0_ 无水印原图；url/downloadUrl = _h1_ 带"混元AI生成"水印
  const urls = [];
  const wmUrls = [];
  let error = null;
  for (const line of text.split('\\n')) {
    if (!line.startsWith('data: ')) continue;
    const payload = line.slice(6).trim();
    if (payload === '[DONE]' || payload.startsWith('[')) continue;
    try {
      const obj = JSON.parse(payload);
      if (obj.type === 'replace' && obj.replace && obj.replace.multimedias) {
        const fresh = obj.replace.multimedias.map(m => m.originUrl || m.url).filter(Boolean);
        const freshWm = obj.replace.multimedias.map(m => m.url).filter(Boolean);
        if (fresh.length) { urls.length = 0; urls.push(...fresh); }
        if (freshWm.length) { wmUrls.length = 0; wmUrls.push(...freshWm); }
      }
      if (obj.type === 'error') error = obj.msg || '生图失败';
    } catch (e) {}
  }
  return {status: resp.status, urls, wmUrls, error, raw: urls.length ? undefined : text.slice(0, 400)};
})
"""

JS_DELETE_CONV = """
(async (p) => {
  const r = await fetch('/api/user/agent/conversation/v1/delete', {
    method: 'POST', headers: {'content-type': 'application/json'},
    body: JSON.stringify({cid: p.conv})
  });
  return {status: r.status};
})
"""

JS_INJECT_STATIC = (
    "window.__ybStaticHeaders = " + json.dumps(STATIC_HEADERS) + "; 'ok'"
)

# ---------------- 消息折叠（约定对齐 image-adapter frontdoor） ----------------
def _read_part(part, where: str):
    """一个 content part -> (text, image_ref)；未知类型报 400（不静默丢弃）。"""
    if isinstance(part, str):
        return part, None
    if not isinstance(part, dict):
        raise ValueError(f"'{where}' 必须是对象或字符串")
    kind = part.get("type")
    if kind in ("text", "input_text"):
        return str(part.get("text") or ""), None
    if kind in ("image_url", "input_image"):
        ref = part.get("image_url")
        if isinstance(ref, dict):
            ref = ref.get("url")
        if not isinstance(ref, str) or not ref:
            raise ValueError(f"'{where}.image_url.url' 缺失")
        return "", ref
    raise ValueError(f"'{where}.type' 不支持: {kind!r}")


def _read_content(content, where: str):
    """一条消息的 content -> (text, [image_refs])。"""
    if content is None:
        return "", []
    if isinstance(content, str):
        return content, []
    if not isinstance(content, list):
        raise ValueError(f"'{where}' 必须是字符串或数组")
    texts, refs = [], []
    for i, part in enumerate(content):
        text, ref = _read_part(part, f"{where}[{i}]")
        if text:
            texts.append(text)
        if ref:
            refs.append(ref)
    return "\n".join(texts), refs


def fold_messages(messages: list) -> tuple:
    """chat body -> (prompt, image_refs)，规则同 image-adapter：
    system 指令前置（其图为全局参考）；只取最后一条 user（其图生效）；
    assistant 历史忽略；未知角色 400；无 user 报 400。"""
    instructions: list = []
    instruction_refs: list = []
    user_text, user_refs = "", []
    users = 0
    for idx, message in enumerate(messages):
        where = f"messages[{idx}]"
        if not isinstance(message, dict):
            raise ValueError(f"'{where}' 必须是对象")
        role = message.get("role")
        if role in ("system", "developer"):
            text, found = _read_content(message.get("content"), f"{where}.content")
            if text:
                instructions.append(text)
            instruction_refs.extend(found)
        elif role == "user":
            users += 1
            user_text, user_refs = _read_content(message.get("content"), f"{where}.content")
        elif role == "assistant":
            continue
        else:
            raise ValueError(f"'{where}.role' 不支持: {role!r}")
    if users == 0:
        raise ValueError("'messages' 必须包含 user 消息")
    prompt = user_text
    if instructions:
        prompt = "\n".join(instructions) + "\n\n" + user_text
    return prompt.strip(), list(instruction_refs) + list(user_refs)


def _ref_to_data_uri(ref: str) -> str:
    """image ref → data URI（支持 URL / data URI / 裸 base64）。"""
    if ref.startswith("data:"):
        return ref
    if ref.startswith("http://") or ref.startswith("https://"):
        import base64 as _b64
        import urllib.request as _u
        req = _u.Request(ref, headers={"user-agent": "Mozilla/5.0"})
        with _u.urlopen(req, timeout=60) as resp:
            blob = resp.read()
            mime = resp.headers.get("content-type", "image/png").split(";")[0].strip()
            if not mime.startswith("image/"):
                mime = "image/png"
        return f"data:{mime};base64,{_b64.b64encode(blob).decode()}"
    return f"data:image/png;base64,{ref}"


# ---------------- 去水印（实验性） ----------------
JS_REMOVE_WATERMARK = """
(async (p) => {
  const dataUri = "data:image/png;base64," + window.__payload.b64;
  const shapes = [
    {images: [dataUri]},
    {images: [{url: dataUri}]},
  ];
  for (const body of shapes) {
    const r = await fetch("/api/image/removewatermark", {
      method: "POST", headers: {"content-type": "application/json"}, body: JSON.stringify(body)
    });
    const t = await r.text();
    if (!t.includes("url is nil") && !t.includes("输入图片为空")) {
      // 解析 SSE 里的图片结果
      const urls = [];
      for (const line of t.split("\\n")) {
        if (!line.startsWith("data: ")) continue;
        try {
          const o = JSON.parse(line.slice(6).trim());
          if (o.url) urls.push(o.url);
          if (o.replace && o.replace.multimedias) {
            urls.push(...o.replace.multimedias.map(m => m.originUrl || m.url).filter(Boolean));
          }
        } catch (e) {}
      }
      if (urls.length) return {ok: true, urls};
      return {ok: false, status: r.status, body: t.slice(0, 200)};
    }
  }
  return {ok: false, reason: "param_shape_unknown", dataUriLen: dataUri.length, head: dataUri.slice(0, 40), tail: dataUri.slice(-20)};
})
"""

# ---------------- 页内 JS：i2i 上传 + 图生图 ----------------
JS_UPLOAD_REF = """
(async (p) => {
  const bin = atob(p.b64Data);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  const info = await (await fetch('/api/resource/genUploadInfo', {
    method: 'POST', headers: {'content-type': 'application/json'},
    body: JSON.stringify({fileName: p.name, docFrom: 'localDoc', docOpenId: '', needAuth: true})
  })).json();
  if (!info.cosURL || !info.putAuthorization) throw new Error('genUploadInfo 异常: ' + JSON.stringify(info).slice(0,150));
  const putResp = await fetch(info.cosURL, {
    method: 'PUT',
    headers: {'Authorization': info.putAuthorization, 'Content-Type': p.mime},
    body: bytes
  });
  if (!putResp.ok) throw new Error('COS PUT ' + putResp.status);
  const dims = await new Promise((resolve) => {
    const blob = new Blob([bytes], {type: p.mime});
    const img = new Image();
    img.onload = () => resolve({w: img.naturalWidth, h: img.naturalHeight});
    img.onerror = () => resolve({w: 0, h: 0});
    img.src = URL.createObjectURL(blob);
  });
  const fid = Array.from(crypto.getRandomValues(new Uint8Array(6))).map(b => b.toString(16).padStart(2,'0')).join('');
  return {
    type: 'image', docType: 'image',
    url: info.resourceUrl,
    signUrl: info.cosURL,
    fileName: p.name,
    size: bytes.length,
    width: dims.w, height: dims.h,
    fileId: fid,
    uploadStatus: 'success',
    progress: 100
  };
})
"""

JS_CHAT_I2I = """
(async (p) => {
  const {conv, agentId, prompt, multimedia, resolution, ratio, chatModelId, temp, sig} = p;
  const BASE = 'hunyuan_gpt_175B_0404';
  const igen = {model: 'Hy Image 3.5', resolution: resolution || '1.5K'};
  if (ratio) igen.ratio = ratio;
  const body = {
    model: 'gpt_175B_0404',
    prompt: '帮我生成图片：' + prompt,
    plugin: 'Adaptive',
    displayPrompt: '帮我生成图片：' + prompt,
    displayPromptType: 1,
    question: '帮我生成图片：' + prompt,
    skillIdParam: 'ai_image',
    msgScene: 12,
    chatModelId: chatModelId,
    chatModelExtInfo: JSON.stringify({modelId: BASE, agentModeModelSetting: {modelId: chatModelId}, supportFunctions: {internetSearch: ''}, internetSearch: 'autoInternetSearch'}),
    extra: {image_gen_param: igen},
    multimedia: multimedia,
    agentId, isTemporary: !!temp, projectId: '',
    supportFunctions: ['openAutoSearchSwitch', 'autoInternetSearch'],
    docOpenid: '',
    options: {imageIntention: {needIntentionModel: true, backendUpdateFlag: 2, intentionStatus: true}},
    supportHint: 1,
    applicationIdList: ['application_id_ai_image'],
    skillId: 'ai_image',
    chatSource: 'ai_image',
    version: 'v2', extReportParams: null, isAtomInput: false,
    conversationId: conv,
    offsetOfHour: 8, offsetOfMinute: 0
  };
  const headers = Object.assign({}, __ybStaticHeaders, {
    'X-Event-Input-Type': '15',
    'X-AgentID': agentId + '/' + conv,
    'X-Uskey': sig.uskey,
    'X-Bus-Params-Md5': sig.md5,
    'X-Timestamp': sig.ts,
    'content-type': 'text/plain;charset=UTF-8'
  });
  const resp = await fetch('/api/chat/' + conv, {method: 'POST', headers, body: JSON.stringify(body)});
  const text = await resp.text();
  const urls = [];
  const wmUrls = [];
  let error = null;
  for (const line of text.split('\\n')) {
    if (!line.startsWith('data: ')) continue;
    const payload = line.slice(6).trim();
    if (payload === '[DONE]' || payload.startsWith('[')) continue;
    try {
      const obj = JSON.parse(payload);
      if (obj.type === 'replace' && obj.replace && obj.replace.multimedias) {
        const fresh = obj.replace.multimedias.map(m => m.originUrl || m.url).filter(Boolean);
        const freshWm = obj.replace.multimedias.map(m => m.url).filter(Boolean);
        if (fresh.length) { urls.length = 0; urls.push(...fresh); }
        if (freshWm.length) { wmUrls.length = 0; wmUrls.push(...freshWm); }
      }
      if (obj.type === 'error') error = obj.msg || '生图失败';
    } catch (e) {}
  }
  return {status: resp.status, urls, wmUrls, error, raw: urls.length ? undefined : text.slice(0, 400)};
})
"""


def _yb_chat_body(conv: str, agent_id: str, prompt: str, chat_model: str) -> dict:
    """纯文本聊天 body（与页内 JS_CHAT 同构）。"""
    BASE = "hunyuan_gpt_175B_0404"
    return {
        "model": "gpt_175B_0404",
        "prompt": prompt, "plugin": "", "displayPrompt": prompt, "displayPromptType": 1,
        "agentId": agent_id, "isTemporary": YUANBAO_TEMP_CONV == "1", "projectId": "",
        "chatModelId": chat_model,
        "chatModelExtInfo": json.dumps({
            "modelId": BASE, "agentModeModelSetting": {"modelId": chat_model},
            "supportFunctions": {"internetSearch": ""}, "internetSearch": "autoInternetSearch"
        }),
        "supportFunctions": ["openAutoSearchSwitch", "autoInternetSearch"],
        "docOpenid": "",
        "options": {"imageIntention": {"needIntentionModel": True, "backendUpdateFlag": 2, "intentionStatus": True}},
        "multimedia": [], "supportHint": 1,
        "applicationIdList": [], "version": "v2", "extReportParams": None,
        "isAtomInput": False, "conversationId": conv,
        "offsetOfHour": 8, "offsetOfMinute": 0,
    }


def _yb_image_body(conv: str, agent_id: str, prompt: str, multimedia: list, resolution: str, chat_model: str, ratio: Optional[str] = None) -> dict:
    """生图 body（出站统一）：带 multimedia → i2i（msgScene 12）；无 → t2i（msgScene 13）。"""
    BASE = "hunyuan_gpt_175B_0404"
    full_prompt = "帮我生成图片：" + prompt
    igen = {"model": "Hy Image 3.5", "resolution": resolution or "1.5K"}
    if ratio:
        igen["ratio"] = ratio
    return {
        "model": "gpt_175B_0404",
        "prompt": full_prompt, "plugin": "Adaptive",
        "displayPrompt": full_prompt, "displayPromptType": 1,
        "question": full_prompt,
        "skillIdParam": "ai_image", "msgScene": 12 if multimedia else 13,
        "chatModelId": chat_model,
        "chatModelExtInfo": json.dumps({
            "modelId": BASE, "agentModeModelSetting": {"modelId": chat_model},
            "supportFunctions": {"internetSearch": ""}, "internetSearch": "autoInternetSearch"
        }),
        "extra": {"image_gen_param": igen},
        "multimedia": multimedia,
        "agentId": agent_id, "isTemporary": YUANBAO_TEMP_CONV == "1", "projectId": "",
        "supportFunctions": ["openAutoSearchSwitch", "autoInternetSearch"],
        "docOpenid": "",
        "options": {"imageIntention": {"needIntentionModel": True, "backendUpdateFlag": 2, "intentionStatus": True}},
        "supportHint": 1,
        "applicationIdList": ["application_id_ai_image"],
        "skillId": "ai_image", "chatSource": "ai_image",
        "version": "v2", "extReportParams": None, "isAtomInput": False,
        "conversationId": conv,
        "offsetOfHour": 8, "offsetOfMinute": 0,
    }


def _mint_only(tab: str) -> dict:
    """仅铸签名（不注入静态头；出站模式用代理自己的头集合）。"""
    sig = _ev(f"({JS_BOOT})", tab_id=tab)
    if not sig or "uskey" not in sig:
        raise RuntimeError(f"签名铸造失败: {sig}")
    return sig


def _cookie_mode_run(cookie: str, agent_id: str, prompt: str, chat_model: str,
                     image_refs: list, resolution: str, ratio: Optional[str] = None,
                     force_image: bool = False) -> dict:
    """凭证透传模式：数据面全走代理出站 HTTP（签名仍借页面铸造）。
    返回与浏览器路径同构的 result dict。含限速 + 软拒退避重试。
    force_image=True 时（chat 门生图模型）无参考图也走文生图管道。"""
    # cdp 模式无 bsk 页面：agent 取常量；bsk 模式才需 _ensure_page
    ctx = _ensure_page() if YB_MINT_BACKEND != "cdp" else {"tabId": None, "agentId": "naQivTmsDa"}
    pace("ck:" + cookie[:16])
    with _bsk_lock:
        created_convs = []  # 本次运行创建的会话（结束后统一删除）
        def run_once(sig):
            if YUANBAO_CONVERSATION:
                conv = YUANBAO_CONVERSATION
            elif YB_CREATE_CONV == "1":
                r = _yb_post_json(cookie, "/api/user/agent/conversation/create",
                                  {"agentId": agent_id}, sig, agent_id)
                if r["status"] == 401:
                    return {"status": 401, "text": r["text"]}
                conv = json.loads(r["text"]).get("id")
                if not conv:
                    raise RuntimeError("创建会话失败: " + r["text"][:150])
                created_convs.append(conv)
            else:
                # 可选捷径（YB_CREATE_CONV=0）：默认关闭
                conv = str(uuid.uuid4())
                created_convs.append(conv)
            if image_refs or force_image:
                multimedia = []
                for i, ref in enumerate(image_refs[:4]):
                    uri = _ref_to_data_uri(ref)
                    head, _, b64data = uri.partition(";base64,")
                    mime = head[5:].split(";")[0] or "image/png"
                    if not mime.startswith("image/"):
                        mime = "image/png"
                    ext = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif"}.get(mime, "png")
                    multimedia.append(_yb_upload_ref(cookie, agent_id, b64data, f"ref_{i}.{ext}", mime))
                body = _yb_image_body(conv, agent_id, prompt, multimedia, resolution, chat_model, ratio)
                r = _yb_post_json(cookie, f"/api/chat/{conv}", body, sig, agent_id, conv,
                                  extra={"X-Event-Input-Type": "15"})
                parsed = _parse_yb_sse_common(r["text"])
                return {"status": r["status"], "urls": parsed["urls"], "wmUrls": parsed["wmUrls"],
                        "text": parsed["text"], "error": parsed["error"],
                        "raw": None if parsed["urls"] else r["text"][:400]}
            body = _yb_chat_body(conv, agent_id, prompt, chat_model)
            r = _yb_post_json(cookie, f"/api/chat/{conv}", body, sig, agent_id, conv)
            return {"status": r["status"], "text": r["text"]}

        sig = get_sig(ctx["tabId"])
        result = run_once(sig)
        # 软拒退避：强制重铸签名重试
        attempt = 0
        while is_softreject(result) and attempt < YB_SOFTRETRY and result.get("status") == 200:
            attempt += 1
            time.sleep(2.0 * attempt)
            sig = get_sig(ctx["tabId"], force=True)
            result = run_once(sig)
        # 用完即删：本次创建的会话全部清理（含软拒重试产生的），历史零残留
        if YB_DELETE_CONV == "1":
            for cv in created_convs:
                try:
                    _yb_post_json(cookie, "/api/user/agent/conversation/v1/delete",
                                  {"cid": cv}, sig, agent_id, timeout=30)
                except Exception:
                    pass
        return result


# ---------------- 元宝调用封装 ----------------
def _yb_auth_error(result: dict):
    """上游 401 → 明确的凭证过期错误（透传模式常见 = hy_token/hy_user 失效，需重新抓 cookie）。"""
    if isinstance(result, dict) and result.get("status") == 401:
        return JSONResponse(
            {"error": {"message": "元宝凭证失效（HTTP 401）：透传的 cookie 已过期或无效，请重新从浏览器抓取 hy_token/hy_user 并更新。",
                       "type": "api_error", "code": "yuanbao_credential_expired"}},
            status_code=502,
        )
    return None


def _mint_and_inject(tab: str) -> dict:
    """注入静态头 + 现场铸造签名。"""
    _ev(JS_INJECT_STATIC, tab_id=tab)
    sig = _ev(JS_BOOT, tab_id=tab)
    if not sig or "uskey" not in sig:
        raise RuntimeError(f"签名铸造失败: {sig}")
    return sig


def _parse_chat_sse(text: str) -> dict:
    """解析元宝聊天 SSE → {content, reasoning, usage, error}"""
    content_parts, reasoning_parts = [], []
    usage, error = None, None
    for line in text.split("\n"):
        line = line.strip()
        if not line.startswith("data: "):
            if line.startswith("event: error"):
                error = error or "元宝返回错误事件"
            continue
        payload = line[6:].strip()
        if payload == "[DONE]":
            break
        if not payload.startswith("{"):
            continue
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            continue
        t = obj.get("type")
        if t == "text" and obj.get("msg") is not None:
            content_parts.append(obj["msg"])
        elif t == "think" and obj.get("msg") is not None:
            reasoning_parts.append(obj["msg"])
        elif t == "meta":
            tok = obj.get("tokenUsageInfo") or {}
            usage = {
                "prompt_tokens": tok.get("promptTokens", 0),
                "completion_tokens": tok.get("completionTokens", 0),
                "total_tokens": tok.get("totalTokens", 0),
            }
            if obj.get("modelErrorCode"):
                error = obj.get("modelErrorMsg") or f"modelErrorCode={obj['modelErrorCode']}"
        elif t == "error":
            error = obj.get("msg") or "服务繁忙"
    return {
        "content": "".join(content_parts),
        "reasoning": "".join(reasoning_parts),
        "usage": usage,
        "error": error,
    }


def _flatten_messages(messages: list) -> str:
    """把 OpenAI messages 摊平成单条 prompt（元宝无客户端历史注入，靠会话自身记忆）。"""
    parts = []
    for m in messages:
        role = m.get("role", "user")
        content = m.get("content", "")
        if isinstance(content, list):  # 多模态数组 → 取文本段
            content = "".join(
                seg.get("text", "") for seg in content if isinstance(seg, dict) and seg.get("type") == "text"
            )
        if not content:
            continue
        if role == "system":
            parts.append(f"[系统指令] {content}")
        elif role == "assistant":
            parts.append(f"[助手] {content}")
        else:
            parts.append(content)
    return "\n".join(parts).strip()


# ---------------- OpenAI 端点 ----------------
@app.get("/login")
async def login_page(req: Request):
    """headless 部署模式的扫码入口：返回当前元宝页面的截图（含登录二维码）。需门禁 key。"""
    cred = _check_auth(req)
    if isinstance(cred, JSONResponse):
        return cred
    if YB_MINT_BACKEND != "cdp":
        return JSONResponse({"error": "仅 cdp 后端支持"}, status_code=400)
    try:
        import base64
        import cdp_minter
        m = cdp_minter.get_minter()
        with m._lock:
            ws = m._ensure_page()
            ws.settimeout(30)
            ws.send(json.dumps({"id": 30, "method": "Page.captureScreenshot", "params": {"format": "png"}}))
            deadline = time.time() + 25
            while time.time() < deadline:
                msg = json.loads(ws.recv())
                if msg.get("id") == 30:
                    b64 = msg["result"]["data"]
                    return Response(content=base64.b64decode(b64), media_type="image/png")
            return JSONResponse({"error": "截图超时"}, status_code=504)
    except Exception as e:
        return JSONResponse({"error": str(e)[:200]}, status_code=502)


JS_PHONE_SEND = """
(async (p) => {
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  const setVal = (el, v) => {
    const proto = el.tagName === 'INPUT' ? HTMLInputElement.prototype : HTMLTextAreaElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
    setter.call(el, v);
    el.dispatchEvent(new Event('input', {bubbles: true}));
    el.dispatchEvent(new Event('change', {bubbles: true}));
  };
  const byText = (txt, exact) => [...document.querySelectorAll('*')]
    .filter(e => e.offsetWidth > 0 && e.children.length <= 2)
    .filter(e => { const t = (e.textContent || '').trim(); return exact ? t === txt : t.startsWith(txt); })
    .sort((a, b) => (a.offsetWidth * a.offsetHeight) - (b.offsetWidth * b.offsetHeight))[0];

  // 0) 若登录弹窗未开，先点 Log In 打开
  if (!document.querySelector('.hyc-phone-login')) {
    const loginBtn = byText('Log In');
    if (loginBtn) { loginBtn.click(); await sleep(1500); }
    const phoneTab = byText('Phone', true);
    if (phoneTab && !document.querySelector('.hyc-phone-login')) { phoneTab.click(); await sleep(1500); }
  }
  if (!document.querySelector('.hyc-phone-login')) return {ok: false, step: 'open_modal', msg: '登录弹窗未出现'};

  // 1) 切区号
  const areaEl = document.querySelector('.yuanbao-oversea-input__wrap__formitem__areaCode');
  if (areaEl && !areaEl.textContent.includes(p.area.replace('+', ''))) {
    areaEl.click();
    await sleep(800);
    const opt = byText(p.area, false);
    if (!opt) return {ok: false, step: 'area', msg: '未找到区号 ' + p.area};
    opt.click();
    await sleep(500);
  }
  const areaNow = (document.querySelector('.yuanbao-oversea-input__wrap__formitem__areaCode') || {}).textContent || '';

  // 2) 填手机号
  const tel = document.querySelector('.hyc-phone-login input[type=tel]') || document.querySelector('input[type=tel]');
  if (!tel) return {ok: false, step: 'phone_input', msg: '手机号输入框未找到'};
  setVal(tel, p.phone);
  await sleep(300);

  // 3) 勾协议（如未勾）
  const cb = document.querySelector('.hyc-phone-login .t-checkbox__former') || document.querySelector('.t-checkbox__former');
  if (cb && !cb.checked) {
    const boxLabel = cb.closest('.t-checkbox');
    (boxLabel || cb).click();
    await sleep(300);
  }

  // 4) dry_run：到此为止（不点发送）
  if (p.dry_run) return {ok: true, dry_run: true, area: areaNow.trim(),
    phone_filled: !!(tel.value), agree_checked: !!(cb && cb.checked)};
  const send = document.querySelector('a.hyc-phone-login__send-code');
  if (!send) return {ok: false, step: 'send_btn', msg: 'Send 按钮未找到'};
  send.click();
  await sleep(2500);

  // 5) 读校验结果（Toast / 按钮态）
  const toast = [...document.querySelectorAll('[class*=toast],[class*=message],[class*=Toast]')]
    .filter(e => e.offsetWidth > 0).map(e => (e.textContent || '').trim()).filter(Boolean)[0] || '';
  const sendTxt = (document.querySelector('a.hyc-phone-login__send-code') || {}).textContent || '';
  const err = [...document.querySelectorAll('[class*=error],[class*=tip]')]
    .filter(e => e.offsetWidth > 0).map(e => (e.textContent || '').trim()).filter(t => t && t.length < 60)[0] || '';
  return {ok: true, area: areaNow.trim(), phone: p.phone, send_text: sendTxt.trim(), toast, err};
})
"""

JS_PHONE_VERIFY = """
(async (p) => {
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  const setVal = (el, v) => {
    const proto = el.tagName === 'INPUT' ? HTMLInputElement.prototype : HTMLTextAreaElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
    setter.call(el, v);
    el.dispatchEvent(new Event('input', {bubbles: true}));
    el.dispatchEvent(new Event('change', {bubbles: true}));
  };
  const codeInput = document.querySelector('.hyc-phone-login input[type=number]')
    || [...document.querySelectorAll('input')].find(e => (e.placeholder || '').toLowerCase().includes('verification'));
  if (!codeInput) return {ok: false, step: 'code_input', msg: '验证码输入框未找到'};
  setVal(codeInput, p.code);
  await sleep(300);
  const btn = document.querySelector('button.hyc-phone-login__btn');
  if (!btn) return {ok: false, step: 'submit_btn', msg: 'Log In 按钮未找到'};
  btn.click();
  await sleep(4000);
  const loggedIn = !document.querySelector('.hyc-phone-login') && !document.body.innerText.includes('Not logged in');
  const toast = [...document.querySelectorAll('[class*=toast],[class*=message],[class*=Toast]')]
    .filter(e => e.offsetWidth > 0).map(e => (e.textContent || '').trim()).filter(Boolean)[0] || '';
  return {ok: true, logged_in: loggedIn, toast};
})
"""

@app.post("/login/phone/send")
async def login_phone_send(req: Request):
    """香港/大陆手机号接码登录 —— 第一步：切区号 + 填号 + 点发送验证码。"""
    cred = _check_auth(req)
    if isinstance(cred, JSONResponse):
        return cred
    if YB_MINT_BACKEND != "cdp":
        return JSONResponse({"error": "仅 cdp 后端支持"}, status_code=400)
    try:
        body = await req.json()
    except Exception:
        body = {}
    phone = str(body.get("phone", "")).strip()
    area = str(body.get("area", "+852")).strip()
    if not phone:
        return JSONResponse({"error": "缺少 phone（本地号，不含区号）"}, status_code=400)
    import cdp_minter
    m = cdp_minter.get_minter()
    dry_run = bool(body.get("dry_run"))
    js = f"({JS_PHONE_SEND})({json.dumps({'phone': phone, 'area': area, 'dry_run': dry_run})})"
    try:
        r = await asyncio.to_thread(m.evaluate, js, 60)  # evaluate 内部自带锁，勿在外层重复抢锁（跨线程死锁）
        return {"result": r, "hint": "验证码已触发（若 send_text 计数中即已发出）；收到后 POST /login/phone/verify {code}"}
    except Exception as e:
        return JSONResponse({"error": str(e)[:200]}, status_code=502)


@app.post("/login/phone/verify")
async def login_phone_verify(req: Request):
    """接码登录 —— 第二步：填验证码 + 提交，成功即持久化到容器 profile。"""
    cred = _check_auth(req)
    if isinstance(cred, JSONResponse):
        return cred
    if YB_MINT_BACKEND != "cdp":
        return JSONResponse({"error": "仅 cdp 后端支持"}, status_code=400)
    try:
        body = await req.json()
    except Exception:
        body = {}
    code = str(body.get("code", "")).strip()
    if not code:
        return JSONResponse({"error": "缺少 code"}, status_code=400)
    import cdp_minter
    m = cdp_minter.get_minter()
    js = f"({JS_PHONE_VERIFY})({json.dumps({'code': code})})"
    try:
        r = await asyncio.to_thread(m.evaluate, js, 60)
        return {"result": r, "hint": "logged_in=true 即登录成功（登录态持久化在容器 chrome-profile）"}
    except Exception as e:
        return JSONResponse({"error": str(e)[:200]}, status_code=502)


import json
import os

ADMIN_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>元宝账号管理</title>
<style>
  :root { --bg:#faf9f7; --card:#fff; --line:#e6e4df; --text:#2c2c2a; --muted:#6b6a66; --accent:#534AB7; --ok:#3B6D11; --warn:#854F0B; --err:#A32D2D; }
  * { box-sizing:border-box; }
  body { margin:0; padding:24px; background:var(--bg); color:var(--text); font:14px/1.6 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif; }
  h1 { font-size:18px; font-weight:500; margin:0 0 4px; }
  .sub { color:var(--muted); font-size:12px; margin-bottom:18px; }
  .grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(340px,1fr)); gap:16px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:16px; }
  .card h2 { font-size:14px; font-weight:500; margin:0 0 12px; display:flex; align-items:center; gap:8px; }
  .badge { font-size:11px; padding:2px 8px; border-radius:99px; border:1px solid var(--line); color:var(--muted); }
  .badge.ok { color:var(--ok); border-color:var(--ok); }
  .badge.err { color:var(--err); border-color:var(--err); }
  .badge.warn { color:var(--warn); border-color:var(--warn); }
  label { display:block; font-size:12px; color:var(--muted); margin:10px 0 4px; }
  input, select, button { font:13px inherit; }
  input, select { width:100%; padding:8px 10px; border:1px solid var(--line); border-radius:8px; background:#fff; color:var(--text); }
  button { margin-top:12px; padding:8px 14px; border:1px solid var(--accent); background:var(--accent); color:#fff; border-radius:8px; cursor:pointer; }
  button.ghost { background:#fff; color:var(--accent); }
  button:disabled { opacity:.5; cursor:not-allowed; }
  .row { display:flex; gap:8px; align-items:flex-end; }
  .row > div { flex:1; }
  .msg { margin-top:10px; font-size:12px; min-height:18px; }
  .msg.ok { color:var(--ok); } .msg.err { color:var(--err); } .msg.warn { color:var(--warn); }
  #shot { width:100%; border:1px solid var(--line); border-radius:8px; margin-top:12px; display:block; background:#fff; }
  .muted { color:var(--muted); font-size:12px; }
  code { background:#f1efe8; padding:1px 5px; border-radius:4px; font-size:12px; }
  table { width:100%; border-collapse:collapse; font-size:12px; }
  td, th { text-align:left; padding:6px 8px; border-bottom:1px solid var(--line); }
  a { color:var(--accent); }
</style>
</head>
<body>
<h1>元宝账号管理</h1>
<div class="sub">单实例自管理 · <span id="whoami">...</span></div>

<div class="grid">
  <div class="card">
    <h2>访问凭证 <span class="badge" id="keyBadge">未设置</span></h2>
    <label>门禁 Key（浏览器本地保存，不上传）</label>
    <input id="key" type="password" placeholder="sk-yuanbao-...">
    <button onclick="saveKey()">保存并检测</button>
    <div class="msg" id="keyMsg"></div>
  </div>

  <div class="card">
    <h2>服务状态 <span class="badge" id="svcBadge">待检测</span></h2>
    <table id="svcTable"><tr><td class="muted">保存 Key 后自动检测</td></tr></table>
    <button class="ghost" onclick="probe()">重新探活</button>
    <div class="msg" id="svcMsg"></div>
  </div>

  <div class="card">
    <h2>手机号接码登录 <span class="badge" id="phBadge">香港 +852</span></h2>
    <div class="row">
      <div style="max-width:110px">
        <label>区号</label>
        <select id="area"><option value="+852">+852 中国香港</option><option value="+86">+86 中国大陆</option></select>
      </div>
      <div>
        <label>手机号（不含区号）</label>
        <input id="phone" placeholder="例如 98765432">
      </div>
    </div>
    <div class="row">
      <button id="sendBtn" onclick="sendCode()">发送验证码</button>
      <div style="flex:1">
        <label>验证码</label>
        <input id="code" placeholder="6 位数字">
      </div>
    </div>
    <button onclick="verifyCode()">提交登录</button>
    <div class="msg" id="phMsg"></div>
  </div>

  <div class="card">
    <h2>当前页面（扫码 / 状态）<span class="badge" id="shotBadge">-</span></h2>
    <img id="shot" alt="页面截图">
    <button class="ghost" onclick="loadShot()">刷新截图</button>
    <div class="muted" style="margin-top:8px">未登录时此处显示登录二维码；也可切换上方接码登录。</div>
  </div>
</div>

<script>
const $ = (id) => document.getElementById(id);
let KEY = localStorage.getItem('yb_key') || '';
$('key').value = KEY;

function authHeaders() { return { 'Authorization': 'Bearer ' + KEY, 'content-type': 'application/json' }; }
function setMsg(el, text, cls) { const e = $(el); e.textContent = text; e.className = 'msg ' + (cls || ''); }

function saveKey() {
  KEY = $('key').value.trim();
  localStorage.setItem('yb_key', KEY);
  $('keyBadge').textContent = KEY ? '已保存' : '未设置';
  $('keyBadge').className = 'badge ' + (KEY ? 'ok' : '');
  probe(); loadShot();
}

async function probe() {
  if (!KEY) return setMsg('svcMsg', '先设置 Key', 'warn');
  $('svcBadge').textContent = '检测中';
  try {
    const t0 = Date.now();
    const r = await fetch('/v1/models', { headers: authHeaders() });
    const ms = Date.now() - t0;
    if (r.status === 401) { $('svcBadge').textContent = 'Key 无效'; $('svcBadge').className = 'badge err'; return setMsg('svcMsg', '门禁 Key 被拒绝（401）', 'err'); }
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const d = await r.json();
    $('svcBadge').textContent = '在线'; $('svcBadge').className = 'badge ok';
    $('svcTable').innerHTML = '<tr><th>模型</th><td>' + d.data.map(m => m.id).join('、') + '</td></tr>'
      + '<tr><th>延迟</th><td>' + ms + ' ms</td></tr>';
    setMsg('svcMsg', '服务正常', 'ok');
  } catch (e) {
    $('svcBadge').textContent = '异常'; $('svcBadge').className = 'badge err';
    setMsg('svcMsg', String(e), 'err');
  }
}

async function loadShot() {
  if (!KEY) return;
  $('shotBadge').textContent = '加载中';
  try {
    const r = await fetch('/login', { headers: { 'Authorization': 'Bearer ' + KEY } });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const b = await r.blob();
    $('shot').src = URL.createObjectURL(b);
    $('shotBadge').textContent = new Date().toLocaleTimeString();
    $('shotBadge').className = 'badge ok';
  } catch (e) {
    $('shotBadge').textContent = '失败'; $('shotBadge').className = 'badge err';
  }
}

async function sendCode() {
  if (!KEY) return setMsg('phMsg', '先设置 Key', 'warn');
  const phone = $('phone').value.trim(), area = $('area').value;
  if (!phone) return setMsg('phMsg', '填手机号', 'warn');
  $('sendBtn').disabled = true;
  setMsg('phMsg', '发送中...');
  try {
    const r = await fetch('/login/phone/send', { method: 'POST', headers: authHeaders(), body: JSON.stringify({ phone, area }) });
    const d = await r.json();
    const res = d.result || {};
    if (res.toast) setMsg('phMsg', '页面返回：' + res.toast, res.toast.toLowerCase().includes('valid') ? 'err' : 'ok');
    else if (res.err) setMsg('phMsg', res.err, 'err');
    else setMsg('phMsg', '已触发发送（区号 ' + (res.area || area) + '），收到验证码后填入并提交', 'ok');
    loadShot();
  } catch (e) { setMsg('phMsg', String(e), 'err'); }
  finally { $('sendBtn').disabled = false; }
}

async function verifyCode() {
  if (!KEY) return setMsg('phMsg', '先设置 Key', 'warn');
  const code = $('code').value.trim();
  if (!code) return setMsg('phMsg', '填验证码', 'warn');
  setMsg('phMsg', '提交中...');
  try {
    const r = await fetch('/login/phone/verify', { method: 'POST', headers: authHeaders(), body: JSON.stringify({ code }) });
    const d = await r.json();
    const res = d.result || {};
    if (res.logged_in) { setMsg('phMsg', '登录成功 ✓ 登录态已持久化', 'ok'); $('phBadge').textContent = '已登录'; $('phBadge').className = 'badge ok'; }
    else setMsg('phMsg', '未登录成功' + (res.toast ? '：' + res.toast : ''), 'err');
    loadShot();
  } catch (e) { setMsg('phMsg', String(e), 'err'); }
}

(async function init() {
  try { const i = await (await fetch('/admin/whoami')).json(); $('whoami').textContent = i.name + ' · ' + i.base_url; } catch (e) {}
  if (KEY) { probe(); loadShot(); } else { $('keyBadge').textContent = '未设置'; }
})();
</script>
</body>
</html>
"""


POOL_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>元宝号池总览</title>
<style>
  :root { --bg:#faf9f7; --card:#fff; --line:#e6e4df; --text:#2c2c2a; --muted:#6b6a66; --accent:#534AB7; --ok:#3B6D11; --err:#A32D2D; }
  body { margin:0; padding:24px; background:var(--bg); color:var(--text); font:14px/1.6 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif; }
  h1 { font-size:18px; font-weight:500; margin:0 0 4px; }
  .sub { color:var(--muted); font-size:12px; margin-bottom:18px; }
  .grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(300px,1fr)); gap:14px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:16px; }
  .card h2 { font-size:14px; font-weight:500; margin:0 0 8px; display:flex; justify-content:space-between; align-items:center; }
  .badge { font-size:11px; padding:2px 8px; border-radius:99px; border:1px solid var(--line); color:var(--muted); }
  .badge.ok { color:var(--ok); border-color:var(--ok); }
  .badge.err { color:var(--err); border-color:var(--err); }
  .muted { color:var(--muted); font-size:12px; }
  input { width:100%; padding:8px 10px; border:1px solid var(--line); border-radius:8px; font:13px inherit; margin-top:8px; }
  a { color:var(--accent); }
  img { width:100%; border:1px solid var(--line); border-radius:8px; margin-top:10px; }
  button { margin-top:10px; padding:7px 12px; border:1px solid var(--accent); background:#fff; color:var(--accent); border-radius:8px; cursor:pointer; font:13px inherit; }
</style>
</head>
<body>
<h1>元宝号池总览</h1>
<div class="sub">实例列表来自服务端 YB_POOL_PEERS 配置 · 状态实时探测</div>
<label class="muted">门禁 Key（本地保存）</label>
<input id="key" type="password" placeholder="sk-yuanbao-...">
<div class="grid" id="pool" style="margin-top:16px"></div>
<script>
const $ = (id) => document.getElementById(id);
let KEY = localStorage.getItem('yb_key') || '';
$('key').value = KEY;
$('key').onchange = () => { KEY = $('key').value.trim(); localStorage.setItem('yb_key', KEY); render(); };

async function peers() { const r = await fetch('/admin/peers'); return (await r.json()).peers || []; }

async function render() {
  const list = await peers();
  $('pool').innerHTML = '';
  if (!list.length) { $('pool').innerHTML = '<div class="card muted">未配置实例（YB_POOL_PEERS 为空）</div>'; return; }
  for (const p of list) {
    const el = document.createElement('div');
    el.className = 'card';
    el.innerHTML = '<h2>' + p.name + ' <span class="badge" id="b_' + p.id + '">检测中</span></h2>'
      + '<div class="muted" id="m_' + p.id + '">' + p.url + '</div>';
    $('pool').appendChild(el);
    probePeer(p);
  }
}

async function probePeer(p) {
  const badge = $('b_' + p.id), info = $('m_' + p.id);
  try {
    const t0 = Date.now();
    const r = await fetch(p.url + '/v1/models', { headers: { 'Authorization': 'Bearer ' + KEY } });
    const ms = Date.now() - t0;
    if (r.status === 401) { badge.textContent = 'Key 无效'; badge.className = 'badge err'; return; }
    if (!r.ok) throw new Error('HTTP ' + r.status);
    badge.textContent = '在线 ' + ms + 'ms'; badge.className = 'badge ok';
    info.innerHTML = p.url + ' · <a href="' + p.url + '/admin" target="_blank">管理页</a>';
    try {
      const sr = await fetch(p.url + '/login', { headers: { 'Authorization': 'Bearer ' + KEY } });
      if (sr.ok) {
        const shot = document.createElement('img');
        shot.src = URL.createObjectURL(await sr.blob());
        badge.closest('.card').appendChild(shot);
      }
    } catch (e) {}
  } catch (e) {
    badge.textContent = '异常'; badge.className = 'badge err';
    info.textContent = p.url + ' · ' + String(e);
  }
}
render();
</script>
</body>
</html>
"""


def _pool_peers():
    """YB_POOL_PEERS: JSON 数组 [{"name","url"}] 或 "name=url,name2=url2" 形式。"""
    raw = os.environ.get("YB_POOL_PEERS", "").strip()
    peers = []
    if not raw:
        return peers
    if raw.startswith("["):
        try:
            peers = json.loads(raw)
        except Exception:
            peers = []
    else:
        for i, item in enumerate(raw.split(",")):
            item = item.strip()
            if not item:
                continue
            if "=" in item:
                name, url = item.split("=", 1)
            else:
                name, url = f"acc{i+1}", item
            peers.append({"name": name.strip(), "url": url.strip().rstrip("/")})
    for i, x in enumerate(peers):
        x["id"] = f"p{i}"
        x.setdefault("name", f"acc{i+1}")
    return peers


@app.get("/admin", response_class=Response)
async def admin_page(req: Request):
    # 页面壳免鉴权（无 Key 者需打开页面输入 Key）；所有数据端点仍校门禁
    return Response(content=ADMIN_HTML, media_type="text/html; charset=utf-8")


@app.get("/pool", response_class=Response)
async def pool_page(req: Request):
    return Response(content=POOL_HTML, media_type="text/html; charset=utf-8")


@app.get("/admin/whoami")
async def admin_whoami(req: Request):
    cred = _check_auth(req)
    if isinstance(cred, JSONResponse):
        return cred
    return {"name": os.environ.get("YB_INSTANCE_NAME", "yuanbao-proxy"),
            "base_url": os.environ.get("YB_BASE_URL", ""),
            "mint_backend": YB_MINT_BACKEND, "data_plane": YB_DATA_PLANE}


@app.get("/admin/peers")
async def admin_peers(req: Request):
    cred = _check_auth(req)
    if isinstance(cred, JSONResponse):
        return cred
    return {"peers": _pool_peers()}


@app.get("/v1/models")
async def list_models(req: Request):
    cred = _check_auth(req)
    if isinstance(cred, JSONResponse):
        return cred
    now = int(time.time())
    ids = ["hunyuan", "hunyuan-t1", "deepseek-v3", "deepseek-r1",
           "hy-image", "hy-image-3.5", "hy-image-v3.5", "hy-image-v3.5-preview", "dall-e-3",
           "hy-image-unwatermark"]
    return {"object": "list", "data": [{"id": i, "object": "model", "created": now, "owned_by": "tencent-yuanbao"} for i in ids]}


@app.post("/v1/chat/completions")
async def chat_completions(req: Request):
    cred = _check_auth(req)
    if isinstance(cred, JSONResponse):
        return cred
    cookie_str = cred[1] if isinstance(cred, tuple) else None
    if not cookie_str and (YB_DATA_PLANE == "cookie" or YB_MINT_BACKEND != "cdp") and YB_COOKIE_FILE:
        try:
            cookie_str = open(YB_COOKIE_FILE).read().strip() or None
        except Exception:
            pass
    # cdp+auto：无显式 cookie → 页内数据面（需容器页面已登录）；YB_DATA_PLANE=cookie 强制出站+文件凭证
    body = await req.json()
    model_in = body.get("model", "hunyuan")
    chat_model = MODEL_MAP.get(model_in, model_in)  # 未知名字直接透传给元宝
    stream = bool(body.get("stream"))
    messages = body.get("messages", [])

    # 折叠：取 prompt + 图片引用（约定对齐 image-adapter）。
    # 图片折叠失败时回退纯文本折叠（chat 门对普通文本对话保持原行为）。
    image_refs: list = []
    try:
        prompt, image_refs = fold_messages(messages)
    except ValueError as e:
        return JSONResponse({"error": {"message": str(e), "type": "invalid_request_error"}}, status_code=400)
    if not image_refs:
        prompt = _flatten_messages(messages)
    if not prompt and not image_refs:
        return JSONResponse({"error": {"message": "messages 为空", "type": "invalid_request_error"}}, status_code=400)
    unwm = model_in.lower() in ("hy-image-unwatermark", "removewatermark")
    if unwm and not image_refs:
        return JSONResponse({"error": {"message": "去水印需要提供图片：messages 里带 image_url", "type": "invalid_request_error"}}, status_code=400)

    created = int(time.time())
    comp_id = "chatcmpl-" + uuid.uuid4().hex[:24]
    # hy-image-* / dall-e-* 生图模型：chat 门直接走生图管道（无图 t2i，带图 i2i）
    image_flow = bool(image_refs) or (_is_image_model(model_in) and not unwm)

    conv_created = None  # 本次新建的会话（用完即删）
    try:
        if cookie_str:
            # ---- 凭证透传模式：客户端 cookie + 页内铸签名，数据面走出站 HTTP ----
            result = _cookie_mode_run(cookie_str, (("naQivTmsDa" if YB_MINT_BACKEND == "cdp" else _ensure_page()["agentId"])), prompt,
                                      chat_model, image_refs,
                                      _size_to_resolution(body.get("size", "")),
                                      _size_to_ratio(body.get("size", "")),
                                      force_image=_is_image_model(model_in))

        else:
            # 浏览器模式：执行器 = bsk（本机）| cdp（服务器 headless Chromium）
            if YB_MINT_BACKEND == "cdp":
                tab, agent = None, "naQivTmsDa"
            else:
                ctx = _ensure_page()
                tab, agent = ctx["tabId"], ctx["agentId"]
            with _bsk_lock:
                if YUANBAO_CONVERSATION:
                    conv = YUANBAO_CONVERSATION
                elif YB_CREATE_CONV == "1":
                    conv = _pev(f"({JS_CREATE_CONV})({json.dumps(agent)})", 60)
                    conv_created = conv
                else:
                    # 可选捷径（YB_CREATE_CONV=0）：客户端 UUID 直当会话 ID，跳过 create；默认关闭（反风控优先）
                    conv = str(uuid.uuid4())
                    conv_created = conv
                sig = get_sig(tab)
                if image_refs and not unwm:
                    # ---- 图生图前门：上传参考图 → msgScene 12 生图 ----
                    import base64 as _b64
                    multimedia = []
                    for i, ref in enumerate(image_refs[:4]):  # 元宝单次上限 4 张参考
                        uri = _ref_to_data_uri(ref)
                        head, _, b64data = uri.partition(";base64,")
                        mime = head[5:].split(";")[0] or "image/png"
                        if not mime.startswith("image/"):
                            mime = "image/png"
                        ext = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif"}.get(mime, "png")
                        entry = _pev(
                            f"({JS_UPLOAD_REF})({json.dumps({'b64Data': b64data, 'name': f'ref_{i}.{ext}', 'mime': mime})})",
                            timeout=300,
                        )
                        multimedia.append(entry)
                    result = _pev(
                        f"({JS_CHAT_I2I})({json.dumps({'conv': conv, 'agentId': agent, 'prompt': prompt, 'multimedia': multimedia, 'resolution': _size_to_resolution(body.get('size', '')), 'ratio': _size_to_ratio(body.get('size', '')), 'chatModelId': chat_model, 'temp': YUANBAO_TEMP_CONV == '1', 'sig': sig})})",
                        timeout=300,
                    )
                elif unwm:
                    # ---- 去水印：转 data URI → 分块暂存 → /api/image/removewatermark ----
                    import base64 as _b64
                    data_uri = _ref_to_data_uri(image_refs[0])
                    head, _, b64data = data_uri.partition(";base64,")
                    mime = head[5:].split(";")[0] or "image/png"
                    if not mime.startswith("image/"):
                        mime = "image/png"
                    _pev("window.__payload = {}; 'ok'")
                    CHUNK = 100_000
                    n_chunks = (len(b64data) + CHUNK - 1) // CHUNK
                    print(f"[unwm] b64 {len(b64data)//1024}KB -> {n_chunks} chunks", flush=True)
                    for i in range(0, len(b64data), CHUNK):
                        part = b64data[i:i+CHUNK]
                        try:
                            _pev(f"window.__payload.b64 = (window.__payload.b64||'') + {json.dumps(part)}", 60)
                            print(f"[unwm] chunk {i//CHUNK + 1}/{n_chunks} ok", flush=True)
                        except Exception as ce:
                            print(f"[unwm] chunk {i//CHUNK + 1}/{n_chunks} FAILED: {str(ce)[:150]}", flush=True)
                            raise
                    result = _pev(f"({JS_REMOVE_WATERMARK})({json.dumps({})})", 180)
                    print(f"[unwm] removewatermark done: ok={result.get('ok')}", flush=True)
                    if result.get("ok") and result.get("urls"):
                        result = {"status": 200, "urls": result["urls"], "wmUrls": result["urls"], "error": None, "raw": None}
                    else:
                        reason = json.dumps(result, ensure_ascii=False)[:250]
                        return JSONResponse({"error": {"message": f"去水印（实验性）失败：{reason}", "type": "api_error", "code": "removewatermark_failed"}}, status_code=502)
                elif image_flow:
                    # ---- chat 门生图模型（hy-image-* 等）：文生图 ----
                    result = _pev(
                        f"({JS_IMAGE})({json.dumps({'conv': conv, 'agentId': agent, 'prompt': prompt, 'resolution': _size_to_resolution(body.get('size', '')), 'ratio': _size_to_ratio(body.get('size', '')), 'temp': YUANBAO_TEMP_CONV == '1', 'sig': sig})})",
                        timeout=300,
                    )
                else:
                    # ---- 纯文本聊天 ----
                    result = _pev(
                        f"({JS_CHAT})({json.dumps({'conv': conv, 'agentId': agent, 'prompt': prompt, 'chatModelId': chat_model, 'temp': YUANBAO_TEMP_CONV == '1', 'sig': sig})})",
                        timeout=300,
                    )
    except ValueError as e:
        return JSONResponse({"error": {"message": str(e), "type": "invalid_request_error"}}, status_code=400)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return JSONResponse({"error": {"message": f"浏览器桥接失败: {e}", "type": "api_error"}}, status_code=502)

    # 用完即删（浏览器模式）：历史零残留
    if conv_created:
        try:
            _pev(f"({JS_DELETE_CONV})({json.dumps({'conv': conv_created})})", 30)
        except Exception:
            pass

    # ---- 图生图：data[] 包回 choices[0].message.content parts ----
    if image_flow or unwm:
        auth_err = _yb_auth_error(result)
        if auth_err:
            return auth_err
        if result.get("status") != 200:
            return JSONResponse({"error": {"message": f"元宝 HTTP {result['status']}", "type": "api_error"}}, status_code=502)
        if result.get("error") and not result.get("urls"):
            return JSONResponse({"error": {"message": result["error"], "type": "api_error"}}, status_code=502)
        parts = []
        if result.get("text"):
            parts.append({"type": "text", "text": result["text"]})
        for i, u in enumerate(result.get("urls") or []):
            parts.append({"type": "image_url", "image_url": {"url": u}})
        if not parts:
            return JSONResponse({"error": {"message": "未获取到图片: " + str(result.get("raw"))[:200], "type": "api_error"}}, status_code=502)

        def i2i_sse():
            obj = {
                "id": comp_id, "object": "chat.completion.chunk", "created": created,
                "model": model_in,
                "choices": [{"index": 0, "delta": {"content": parts}}],
            }
            yield f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"
            done = {
                "id": comp_id, "object": "chat.completion.chunk", "created": created,
                "model": model_in,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            }
            yield f"data: {json.dumps(done, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"

        if stream:
            return StreamingResponse(i2i_sse(), media_type="text/event-stream")
        return {
            "id": comp_id, "object": "chat.completion", "created": created, "model": model_in,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": parts}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }

    # ---- 纯文本聊天 ----
    auth_err = _yb_auth_error(result)
    if auth_err:
        return auth_err
    if result.get("status") != 200:
        return JSONResponse({"error": {"message": f"元宝 HTTP {result['status']}", "type": "api_error"}}, status_code=502)

    parsed = _parse_chat_sse(result["text"])
    if parsed["error"] and not parsed["content"]:
        return JSONResponse({"error": {"message": parsed["error"], "type": "api_error"}}, status_code=502)

    def delta_chunks():
        if parsed["reasoning"]:
            yield {"delta": {"reasoning_content": parsed["reasoning"]}}
        if parsed["content"]:
            # 按小块回放，模拟流式
            step = 8
            for i in range(0, len(parsed["content"]), step):
                yield {"delta": {"content": parsed["content"][i:i+step]}}
        yield {"delta": {}, "finish_reason": "stop"}

    def sse_gen():
        for ch in delta_chunks():
            obj = {
                "id": comp_id, "object": "chat.completion.chunk", "created": created,
                "model": model_in,
                "choices": [{"index": 0, **ch}],
            }
            yield f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"
        # usage 附在最后（OpenAI stream options.include_usage 风格）
        if parsed["usage"]:
            uobj = {"id": comp_id, "object": "chat.completion.chunk", "created": created,
                    "model": model_in, "choices": [], "usage": parsed["usage"]}
            yield f"data: {json.dumps(uobj, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"

    if stream:
        return StreamingResponse(sse_gen(), media_type="text/event-stream")

    msg = {"role": "assistant", "content": parsed["content"]}
    if parsed["reasoning"]:
        msg["reasoning_content"] = parsed["reasoning"]
    resp = {
        "id": comp_id, "object": "chat.completion", "created": created, "model": model_in,
        "choices": [{"index": 0, "message": msg, "finish_reason": "stop"}],
        "usage": parsed["usage"] or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }
    return resp


def _size_to_resolution(size: str) -> str:
    if not size:
        return "1.5K"
    s = size.lower()
    if re.match(r"^(1k|1024x1024|512|512x512)", s):
        return "1K"
    if re.match(r"^(2k|2048|1792|1920)", s):
        return "2K"
    return "1.5K"


_RATIO_SLOTS = {"1:1": 1.0, "4:3": 4 / 3, "3:4": 3 / 4, "16:9": 16 / 9, "9:16": 9 / 16}


def _size_to_ratio(size: str) -> Optional[str]:
    """OpenAI size → 元宝比例槽位（extra.image_gen_param.ratio）。
    auto/缺省/智能比例语义 → None（不带 ratio 字段 = 智能比例，模型自定构图）。
    'WxH' → 取对数距离最近的槽位；'16:9' 这类显式比例直接透传（须在槽位表内）。"""
    if not size:
        return None
    s = size.strip().lower()
    if s in ("auto", "smart"):
        return None
    if s in _RATIO_SLOTS:
        return s
    m = re.match(r"^(\d{2,4})x(\d{2,4})$", s)
    if not m:
        return None
    w, h = int(m.group(1)), int(m.group(2))
    if w <= 0 or h <= 0:
        return None
    aspect = w / h
    return min(_RATIO_SLOTS, key=lambda k: abs(__import__("math").log(aspect / _RATIO_SLOTS[k])))


@app.post("/v1/images/generations")
async def images_generations(req: Request):
    cred = _check_auth(req)
    if isinstance(cred, JSONResponse):
        return cred
    cookie_str = cred[1] if isinstance(cred, tuple) else None
    if not cookie_str and (YB_DATA_PLANE == "cookie" or YB_MINT_BACKEND != "cdp") and YB_COOKIE_FILE:
        try:
            cookie_str = open(YB_COOKIE_FILE).read().strip() or None
        except Exception:
            pass
    # cdp+auto：无显式 cookie → 页内数据面（需容器页面已登录）；YB_DATA_PLANE=cookie 强制出站+文件凭证
    body = await req.json()
    prompt = (body.get("prompt") or "").strip()
    if not prompt:
        return JSONResponse({"error": {"message": "prompt 为空", "type": "invalid_request_error"}}, status_code=400)
    # 规范 image 字段（约定对齐 image-adapter）：带 image 即图生图；null/[]/空串等同不传
    image_field = body.get("image")
    if isinstance(image_field, str):
        image_refs = [image_field] if image_field.strip() else []
    elif isinstance(image_field, list):
        image_refs = [x for x in image_field if isinstance(x, str) and x.strip()]
    else:
        image_refs = []
    chat_model = MODEL_MAP.get(body.get("model", "hunyuan"), body.get("model") or "hunyuan_gpt_175B_0404")
    if _is_image_model(chat_model):
        chat_model = "hunyuan_gpt_175B_0404"  # 生图端点里 model 仅是别名，chatModelId 归一到基础模型
    n = min(int(body.get("n", 1) or 1), 4)
    resolution = _size_to_resolution(body.get("size", ""))

    created = int(time.time())
    conv_created = None
    try:
        if cookie_str:
            result = _cookie_mode_run(cookie_str, (("naQivTmsDa" if YB_MINT_BACKEND == "cdp" else _ensure_page()["agentId"])), prompt,
                                      chat_model, image_refs, resolution,
                                      _size_to_ratio(body.get("size", "")))

        else:
            # 浏览器模式：执行器 = bsk（本机）| cdp（服务器 headless Chromium）
            if YB_MINT_BACKEND == "cdp":
                tab, agent = None, "naQivTmsDa"
            else:
                ctx = _ensure_page()
                tab, agent = ctx["tabId"], ctx["agentId"]
            with _bsk_lock:
                if YUANBAO_CONVERSATION:
                    conv = YUANBAO_CONVERSATION
                elif YB_CREATE_CONV == "1":
                    conv = _pev(f"({JS_CREATE_CONV})({json.dumps(agent)})", 60)
                    conv_created = conv
                else:
                    # 可选捷径（YB_CREATE_CONV=0）：客户端 UUID 直当会话 ID，跳过 create；默认关闭（反风控优先）
                    conv = str(uuid.uuid4())
                    conv_created = conv
                sig = get_sig(tab)
                if image_refs and not unwm:
                    # ---- 图生图（image 字段）----
                    import base64 as _b64
                    multimedia = []
                    for i, ref in enumerate(image_refs[:4]):
                        uri = _ref_to_data_uri(ref)
                        head, _, b64data = uri.partition(";base64,")
                        mime = head[5:].split(";")[0] or "image/png"
                        if not mime.startswith("image/"):
                            mime = "image/png"
                        ext = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif"}.get(mime, "png")
                        entry = _pev(
                            f"({JS_UPLOAD_REF})({json.dumps({'b64Data': b64data, 'name': f'ref_{i}.{ext}', 'mime': mime})})",
                            timeout=300,
                        )
                        multimedia.append(entry)
                    result = _pev(
                        f"({JS_CHAT_I2I})({json.dumps({'conv': conv, 'agentId': agent, 'prompt': prompt, 'multimedia': multimedia, 'resolution': resolution, 'ratio': _size_to_ratio(body.get('size', '')), 'chatModelId': chat_model, 'temp': YUANBAO_TEMP_CONV == '1', 'sig': sig})})",
                        timeout=300,
                    )
                else:
                    result = _pev(
                        f"({JS_IMAGE})({json.dumps({'conv': conv, 'agentId': agent, 'prompt': prompt, 'resolution': resolution, 'ratio': _size_to_ratio(body.get('size', '')), 'temp': YUANBAO_TEMP_CONV == '1', 'sig': sig})})",
                        timeout=300,
                    )
    except Exception as e:
        return JSONResponse({"error": {"message": f"浏览器桥接失败: {e}", "type": "api_error"}}, status_code=502)

    # 用完即删（浏览器模式）：历史零残留
    if conv_created:
        try:
            _pev(f"({JS_DELETE_CONV})({json.dumps({'conv': conv_created})})", 30)
        except Exception:
            pass

    auth_err = _yb_auth_error(result)
    if auth_err:
        return auth_err
    if result.get("error") and not result.get("urls"):
        return JSONResponse({"error": {"message": result["error"], "type": "api_error"}}, status_code=502)
    urls = result.get("urls") or []
    wm_urls = result.get("wmUrls") or []
    if not urls:
        return JSONResponse({"error": {"message": "未获取到图片: " + str(result.get("raw"))[:200], "type": "api_error"}}, status_code=502)

    # url = 无水印原图(originUrl/_h0_)；url_watermarked = 带"混元AI生成"水印版(_h1_)
    data = []
    for i, u in enumerate(urls):
        item = {"url": u, "revised_prompt": prompt}
        if i < len(wm_urls):
            item["url_watermarked"] = wm_urls[i]
        data.append(item)
    if n < len(data):
        data = data[:n]
    return {"created": created, "data": data}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=os.environ.get("YB_BIND", "127.0.0.1"), port=PORT, log_level="info")
