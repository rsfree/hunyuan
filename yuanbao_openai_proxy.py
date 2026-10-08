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
# 号池内部互通 key：仅用于**管理面只读端点**（/admin/state|keepalive|peers|pool）的实例间互查，
# 让"任一实例都能看到整池状态"。它**不能**当 API key 调 /v1/*（与实例 key 权限隔离）。
YB_FLEET_KEY = os.environ.get("YB_FLEET_KEY", "").strip()
YB_INSTANCE_NAME_ENV = os.environ.get("YB_INSTANCE_NAME", "yuanbao-acc01")
YB_BASE_URL_ENV = os.environ.get("YB_BASE_URL", "").rstrip("/")
# 保活：定时用 cookie 探活（GET /api/info/general），0=关闭。默认 15 分钟
YB_KEEPALIVE_SEC = int(os.environ.get("YB_KEEPALIVE_SEC", "900"))
# 保活告警阈值：连续失败 N 次后日志升级为 error（便于外部日志告警）
YB_KEEPALIVE_ALERT = int(os.environ.get("YB_KEEPALIVE_ALERT", "3"))
# 无水印保存（账号级开关）：新号登录后自动开启。1=开（默认），0=关
YB_AUTO_WATERMARK = os.environ.get("YB_AUTO_WATERMARK", "1") == "1"
# 调用统计：YBY_STATS_DIR 放 JSONL 明细 + 内存聚合；保留天数
YB_STATS_DIR = os.environ.get("YB_STATS_DIR", "/data/stats")
YB_STATS_KEEP_DAYS = int(os.environ.get("YB_STATS_KEEP_DAYS", "30"))
# 号池生命周期：状态文件目录 + 自动规则阈值
YB_STATE_DIR = os.environ.get("YB_STATE_DIR", "/data/state")
YB_AUTO_DISABLE_AFTER = int(os.environ.get("YB_AUTO_DISABLE_AFTER", "3"))       # 连续保活失败几次→自动隔离
YB_AUTO_REENABLE_AFTER = int(os.environ.get("YB_AUTO_REENABLE_AFTER", "3"))     # 连续成功几次→自动恢复（0=不自动恢复）
YB_AUTO_EJECT_AFTER_DAYS = int(os.environ.get("YB_AUTO_EJECT_AFTER_DAYS", "0")) # 隔离超几天→自动剔除（0=关闭）
YB_ROUTER = os.environ.get("YB_ROUTER", "1") == "1"                            # 是否开放 /pool/v1 轮询入口
_WATERMARK: dict = {"at": 0, "ok": None, "enabled": None, "detail": "", "attempts": 0}
_WM_RUNNING = False
_KEEPALIVE: dict = {"count": 0, "fail_streak": 0, "at": 0, "ok": None, "status": None, "detail": ""}
YUANBAO_TEMP_CONV = os.environ.get("YUANBAO_TEMP_CONV", "1")  # 1=临时会话(不进历史，反风控)；0=普通
YB_DELETE_CONV = os.environ.get("YB_DELETE_CONV", "1")    # 1=生成完自动删除本次创建的会话（历史零残留）
YB_CREATE_CONV = os.environ.get("YB_CREATE_CONV", "1")
YB_SIG_TTL = float(os.environ.get("YB_SIG_TTL", "60"))    # 签名三件套复用秒数（实测可复用，避免每请求铸签）
YB_MINT_BACKEND = os.environ.get("YB_MINT_BACKEND", "cdp")  # cdp（默认，headless Chrome 铸签，无 bsk）| bsk（遗留）| http（远程 minter）
YB_COOKIE_FILE = os.environ.get("YB_COOKIE_FILE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookie.txt"))  # 默认凭证文件（无 bsk 数据面）
YB_PROXY_URL = os.environ.get("YB_PROXY_URL", "")    # 单条代理（兼容）：http://host:port
YB_PROXY_POOL = os.environ.get("YB_PROXY_POOL", "")   # 代理池（逗号分隔）：url1,url2,... 数据面逐请求轮换
_PROXY_BAD: dict = {}          # 代理 -> 冷却截止（失败后 60s 内跳过）
_PROXY_IDX = 0


def _proxy_list():
    pool = [x.strip() for x in YB_PROXY_POOL.split(",") if x.strip()]
    if not pool and YB_PROXY_URL:
        pool = [YB_PROXY_URL.strip()]
    return pool


def _proxy_next():
    """池内轮换：跳过冷却中的；全冷却时返回 None（直连兜底）。"""
    global _PROXY_IDX
    pool = _proxy_list()
    if not pool:
        return None
    now = time.time()
    healthy = [x for x in pool if _PROXY_BAD.get(x, 0) <= now]
    if not healthy:
        return None
    pr = healthy[_PROXY_IDX % len(healthy)]
    _PROXY_IDX += 1
    return pr


def _proxy_mark_bad(url: str, seconds: float = 60):
    _PROXY_BAD[url] = time.time() + seconds


class _RespShim:
    """requests 响应 → urllib 风格垫片（status/read/headers/上下文管理）。"""

    def __init__(self, resp):
        self._r = resp
        self.status = resp.status_code
        self.headers = resp.headers

    def read(self):
        return self._r.content

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _open_socks(req, proxy_url: str, timeout: int):
    """socks5/socks5h 出站（urllib 不支持 socks，走 requests+PySocks）。"""
    import io
    import requests
    resp = requests.request(
        req.get_method(), req.full_url, headers=dict(req.headers),
        data=getattr(req, "data", None),
        proxies={"http": proxy_url, "https": proxy_url},
        timeout=timeout, verify=False,  # 上游证书链在部分链路不稳定（同 chromium -201）
    )
    if resp.status_code >= 400:
        raise _uerr.HTTPError(req.full_url, resp.status_code, resp.reason, resp.headers, io.BytesIO(resp.content))
    return _RespShim(resp)


def _open(req, timeout: int = 300):
    """带池轮换的出站请求。
    socks5h 走 requests；http/https 代理与直连走 urllib。
    仅"连不上"类错误才标记代理故障并换下一个/直连兜底；HTTP 4xx/5xx 属正常响应（代理是通的），直接返回。"""
    order = []
    pool = _proxy_list()
    n = len(pool) or 1
    for _ in range(n + 1):          # 池内每个代理各试一次 + 直连兜底
        pr = _proxy_next()
        order.append(pr or "(直连)")
        # 每次尝试用全新 Request（同一对象在失败后复用会带着旧连接状态）
        fresh = _ureq.Request(req.full_url, data=getattr(req, "data", None),
                              headers=dict(req.headers), method=req.get_method())
        try:
            if pr and pr.split("://", 1)[0].lower().startswith("socks"):
                return _open_socks(fresh, pr, timeout)
            handler = _ureq.ProxyHandler({"http": pr, "https": pr}) if pr else _ureq.ProxyHandler({})
            return _ureq.build_opener(handler).open(fresh, timeout=timeout)
        except _uerr.HTTPError:
            raise                    # 有 HTTP 响应 = 链路通，交给上层按状态码处理
        except Exception:
            if pr:
                _proxy_mark_bad(pr, 60)
                continue
            raise
    raise RuntimeError("代理池全部不可用且直连失败: " + " → ".join(order))
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
    if not value:
        # 也允许从查询串取（?k= / ?key=）：便于 <img src="/login?k=..."> 直链展示截图，
        # 免去 fetch+blob（内嵌预览面板/沙箱 iframe 里 blob: 常被拦，导致裂图）
        value = (request.query_params.get("k") or request.query_params.get("key") or "").strip()
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


def _check_admin_panel_auth(request: Request):
    """管理面**只读**端点鉴权：接受本实例 key 或 YB_FLEET_KEY（供实例间互查号池状态）。

    与 _check_auth 的差别只在"多认一把 fleet key"，权限边界不变：
    fleet key 不能用来调 /v1/chat|images|models（那些仍只认实例 key）。
    """
    if YB_FLEET_KEY:
        v = _bearer_value(request) or (request.query_params.get("k") or "").strip()
        if v == YB_FLEET_KEY:
            return None
    return _check_auth(request)


# ---------------- 出站 HTTP（凭证透传模式：客户端 cookie + 页内铸签名） ----------------
import base64 as _b64mod
import urllib.request as _ureq
import urllib.error as _uerr

_OPENER = _ureq.build_opener(_ureq.ProxyHandler({}))  # 直连，不吃环境代理（信任边界同 curl --noproxy '*'）


# ---------------- 账号保活（keepalive） ----------------
# 思路：`GET /api/info/general` 只需 cookie + 静态头（**无签名、无页面**），
# 是零副作用的最小探活。cookie 从 Chromium 现取（CDP Network.getCookies），
# 探活的 HTTP 往返**不占 CDP 锁** ⇒ 不干扰正在进行的对话。
# 200 = 会话仍活；401 = 已过期（需重新登录）。
KEEPALIVE_URL = "https://yuanbao.tencent.com/api/info/general"


def _keepalive_probe_sync(cookie: str):
    """同步探活（放线程池跑）。返回 (status, body)。"""
    h = dict(STATIC_HEADERS)
    h["Cookie"] = cookie
    req = _ureq.Request(KEEPALIVE_URL, headers=h, method="GET")
    try:
        with _OPENER.open(req, timeout=30) as r:
            return r.status, r.read().decode("utf-8", "ignore")[:200]
    except _uerr.HTTPError as e:
        try:
            return e.code, e.read().decode("utf-8", "ignore")[:200]
        except Exception:
            return e.code, ""
    except Exception as e:
        return -1, ("%s: %s" % (type(e).__name__, e))[:200]


async def _keepalive_once() -> dict:
    """保活一次：取 cookie → 探活 → 更新 _KEEPALIVE。"""
    import cdp_minter
    m = cdp_minter.get_minter()
    try:
        cookie = await asyncio.to_thread(m.extract_cookies)
    except Exception as e:
        r = {"ok": False, "status": None, "detail": "取 cookie 失败: %s" % str(e)[:160], "cookie_len": 0}
        return r
    if not cookie:
        return {"ok": False, "status": None, "detail": "页面无 cookie（未登录？）", "cookie_len": 0}
    # hy_user 是元宝登录态本体；没有它说明页面处于未登录/已重置状态
    if "hy_user=" not in (cookie + ";"):
        return {"ok": False, "status": None, "detail": "缺少 hy_user（未登录）", "cookie_len": len(cookie)}
    st, body = await asyncio.to_thread(_keepalive_probe_sync, cookie)
    return {"ok": st == 200, "status": st,
            "detail": body if st != 200 else "alive", "cookie_len": len(cookie)}


def _record_keepalive(r: dict) -> dict:
    """把探活结果并入全局状态（含连续失败计数，供日志告警）。"""
    _KEEPALIVE["count"] += 1
    _KEEPALIVE["at"] = int(time.time())
    _KEEPALIVE["ok"] = r.get("ok")
    _KEEPALIVE["status"] = r.get("status")
    _KEEPALIVE["detail"] = r.get("detail", "")
    _KEEPALIVE["fail_streak"] = 0 if r.get("ok") else _KEEPALIVE.get("fail_streak", 0) + 1
    return _KEEPALIVE


async def _keepalive_loop():
    """进程内定时保活。页面数据面必须单 worker，故这里只有一份循环（不会 N 份叠加）。"""
    await asyncio.sleep(60)  # 启动后先等页面预热/首次使用
    while True:
        try:
            r = await _keepalive_once()
            snap = _record_keepalive(r)
            lvl = "OK " if r.get("ok") else ("WARN" if snap["fail_streak"] < YB_KEEPALIVE_ALERT else "ERR ")
            print("[keepalive] %s count=%d streak=%d status=%s %s"
                  % (lvl, snap["count"], snap["fail_streak"], r.get("status"), r.get("detail", "")[:120]), flush=True)
            # 新号登录后自动开启"无水印保存"（幂等：已是开启态则不写）
            if YB_AUTO_WATERMARK and r.get("ok"):
                await _watermark_auto()
            # 号池生命周期：用健康观测驱动 自动隔离 / 自动恢复 / 超期剔除
            if ACCOUNT is not None:
                frozen, logged = False, True
                try:
                    import cdp_minter
                    stt = await asyncio.to_thread(cdp_minter.get_minter().page_state)
                    frozen = bool(stt.get("frozen"))
                    logged = bool(stt.get("logged_in"))
                except Exception:
                    pass
                ok_now, det = bool(r.get("ok")), r.get("detail", "")
                if not logged:
                    ok_now, det = False, det or "页面未登录"
                a, act = ACCOUNT.record_health(ok_now, r.get("status"), det, frozen)
                if act:
                    print("[pool_state] auto %s → %s（%s）" % (act, a["state"], a["reason"]), flush=True)
                elif a["state"] != "enabled":
                    print("[pool_state] %s（%s，失败 %d 次）" % (a["state"], a["reason"], a["fail_streak"]),
                          flush=True)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print("[keepalive] loop err: %s" % str(e)[:160], flush=True)
        await asyncio.sleep(YB_KEEPALIVE_SEC)


@app.on_event("startup")
async def _start_metrics():
    _init_metrics()
    _init_account()


@app.on_event("startup")
async def _start_keepalive():
    if YB_KEEPALIVE_SEC <= 0:
        print("[keepalive] disabled (YB_KEEPALIVE_SEC=0)", flush=True)
        return
    if os.environ.get("YB_WORKERS", "1") not in ("", "1"):
        print("[keepalive] skip: 多 worker 会重复保活（页面数据面本应单 worker）", flush=True)
        return
    print("[keepalive] enabled, every %ds" % YB_KEEPALIVE_SEC, flush=True)
    asyncio.create_task(_keepalive_loop())


# ---------------- 调用统计（账号 × 模型 × 端点） ----------------
import contextvars as _ctxvars

_MCTX = _ctxvars.ContextVar("yb_metrics_ctx", default=None)
METRICS = None


def _init_metrics():
    """初始化统计器；/data/stats 不可写则退回代码目录下的 .stats（保证功能不因挂载缺失而消失）。"""
    global METRICS
    import metrics as _m
    for d in (YB_STATS_DIR, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".stats")):
        try:
            os.makedirs(d, exist_ok=True)
            probe = os.path.join(d, ".w")
            with open(probe, "a"):
                pass
            os.remove(probe)
            METRICS = _m.Metrics(os.path.join(d, "events-%s.jsonl" % YB_INSTANCE_NAME_ENV),
                                 YB_STATS_KEEP_DAYS)
            print("[metrics] %s (keep %dd)" % (os.path.join(d, "events-%s.jsonl" % YB_INSTANCE_NAME_ENV),
                                               YB_STATS_KEEP_DAYS), flush=True)
            return
        except Exception as e:
            continue
    print("[metrics] disabled: 无可写目录", flush=True)


ACCOUNT = None


def _init_account():
    """号池账号状态机；/data/state 不可写则退回代码目录 .state。"""
    global ACCOUNT
    import pool_state
    for d in (YB_STATE_DIR, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".state")):
        try:
            os.makedirs(d, exist_ok=True)
            probe = os.path.join(d, ".w")
            with open(probe, "a"):
                pass
            os.remove(probe)
            pth = os.path.join(d, "account-%s.json" % YB_INSTANCE_NAME_ENV)
            ACCOUNT = pool_state.AccountState(pth, YB_INSTANCE_NAME_ENV,
                                              YB_AUTO_DISABLE_AFTER, YB_AUTO_REENABLE_AFTER,
                                              YB_AUTO_EJECT_AFTER_DAYS)
            print("[pool_state] %s (auto-disable>=%d, re-enable>=%d, eject>%dd)"
                  % (pth, YB_AUTO_DISABLE_AFTER, YB_AUTO_REENABLE_AFTER, YB_AUTO_EJECT_AFTER_DAYS),
                  flush=True)
            return
        except Exception:
            continue
    print("[pool_state] disabled: 无可写目录", flush=True)


def _key_tag(request: Request) -> str:
    """给调用方打一个短指纹（绝不落库明文 key）。"""
    v = _bearer_value(request) or (request.query_params.get("k") or "")
    if not v:
        return "anon"
    import hashlib
    if YUANBAO_API_KEY and v == YUANBAO_API_KEY:
        return "instance-key"
    return "k" + hashlib.sha256(v.encode()).hexdigest()[:8]


def _mctx(**kw):
    c = _MCTX.get()
    if c is not None:
        c.update(kw)


@app.middleware("http")
async def _metrics_mw(request: Request, call_next):
    path = request.url.path
    if METRICS is None or not path.startswith("/v1/"):
        return await call_next(request)
    t0 = time.time()
    ctx = {"endpoint": path, "model": None, "stream": False, "usage": None, "err": None}
    # 从请求体里取 model / stream（Starlette 会缓存 body，下游仍可正常读）
    try:
        if request.method == "POST" and "json" in (request.headers.get("content-type") or ""):
            raw = await request.body()
            if 0 < len(raw) < 200000:
                b = json.loads(raw)
                ctx["model"] = b.get("model")
                ctx["stream"] = bool(b.get("stream"))
    except Exception:
        pass
    tok = _MCTX.set(ctx)
    status = 500
    try:
        resp = await call_next(request)
        status = resp.status_code
        return resp
    except Exception as ex:
        ctx["err"] = ("%s: %s" % (type(ex).__name__, ex))[:200]
        raise
    finally:
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
            pass


# ---------------- 无水印保存（账号级开关，新号登录后自动开启） ----------------
# 逆向自 `yb_v2_yb-component` chunk（其模块内常量 FIELD = "watermarkConfig"）：
#   资质门禁: GET  /api/info/general          → graySwitches.grayKeyWithoutWatermark !== false
#   读配置  : POST /api/userinfo/getuserconfig {scene:1, configFields:["watermarkConfig"]}
#   写配置  : POST /api/updateuserinfo         {updateFields:["watermarkConfig"],
#                                               userConfig:{watermarkConfig:{...读到的值, ...patch}}}
#   开启    = patch {saveWithoutWatermark:true, hasPopupAgreement:true}
#   （前端 d(n)=u({saveWithoutWatermark:n, ...(n?{hasPopupAgreement:true}:{})})）
# 关键：这两个接口 **不需要签名**（cookie + 静态头即可）⇒ 可完全脱离页面调用，零浏览器开销。
WM_FIELD = "watermarkConfig"
WM_HOST = "https://yuanbao.tencent.com"


def _wm_call(cookie: str, path: str, payload=None, method: str = "POST", cap: int = 200000):
    h = {**STATIC_HEADERS, **(_DYN_HEADERS or {})}
    h["Cookie"] = cookie
    h["content-type"] = "application/json"
    data = json.dumps(payload).encode() if payload is not None else None
    req = _ureq.Request(WM_HOST + path, data=data, headers=h, method=method)
    try:
        with _OPENER.open(req, timeout=30) as r:
            # 🔴 不要截断得太狠：/api/info/general 的 graySwitches 很长，
            # 截到几百字符会 JSON 解析失败（曾导致 gray_ok 恒为 null）
            return r.status, r.read().decode("utf-8", "ignore")[:cap]
    except _uerr.HTTPError as e:
        try:
            return e.code, e.read().decode("utf-8", "ignore")[:cap]
        except Exception:
            return e.code, ""
    except Exception as e:
        return -1, ("%s: %s" % (type(e).__name__, e))[:200]


def _wm_read(cookie: str):
    st, body = _wm_call(cookie, "/api/userinfo/getuserconfig",
                        {"scene": 1, "configFields": [WM_FIELD]})
    try:
        cfg = (json.loads(body).get("userConfig") or {}).get(WM_FIELD) or {}
        return st, cfg
    except Exception:
        return st, {"__raw": body[:200]}


def _wm_gray_ok(cookie: str):
    """灰度门禁：grayKeyWithoutWatermark !== false 才有资格。"""
    st, body = _wm_call(cookie, "/api/info/general", None, "GET", cap=2_000_000)
    if st != 200:
        return None
    try:
        g = (json.loads(body).get("graySwitches") or {}).get("grayKeyWithoutWatermark")
        return g is not False
    except Exception:
        return None


async def _watermark_ensure(target: bool = True, force: bool = False) -> dict:
    """确保账号「无水印保存」处于目标状态；已是目标状态则不写入（幂等）。"""
    import cdp_minter
    try:
        cookie = await asyncio.to_thread(cdp_minter.get_minter().extract_cookies)
    except Exception as e:
        return {"ok": False, "enabled": None, "detail": "取 cookie 失败: %s" % str(e)[:120]}
    if not cookie or "hy_user=" not in (cookie + ";"):
        return {"ok": False, "enabled": None, "detail": "未登录（无 hy_user）"}
    st, cur = await asyncio.to_thread(_wm_read, cookie)
    if st != 200:
        return {"ok": False, "enabled": None, "detail": "读取失败 HTTP %s %s" % (st, cur.get("__raw", ""))}
    if cur.get("saveWithoutWatermark") is target and not force:
        return {"ok": True, "enabled": target, "detail": "已是目标状态（未写）", "config": cur}
    patch = {"saveWithoutWatermark": True, "hasPopupAgreement": True} if target else {"saveWithoutWatermark": False}
    cur2 = {k: v for k, v in cur.items() if not str(k).startswith("__")}
    st2, body = await asyncio.to_thread(
        _wm_call, cookie, "/api/updateuserinfo",
        {"updateFields": [WM_FIELD], "userConfig": {WM_FIELD: {**cur2, **patch}}})
    if st2 != 200:
        return {"ok": False, "enabled": cur.get("saveWithoutWatermark"), "detail": "写入失败 HTTP %s %s" % (st2, body)}
    _, after = await asyncio.to_thread(_wm_read, cookie)
    ok = after.get("saveWithoutWatermark") is target
    return {"ok": ok, "enabled": after.get("saveWithoutWatermark"),
            "detail": "已写入并复核" if ok else "写入后复核未生效", "before": cur2, "after": after}


def _record_watermark(r: dict) -> dict:
    _WATERMARK.update({"at": int(time.time()), "ok": r.get("ok"),
                       "enabled": r.get("enabled"), "detail": r.get("detail", "")})
    _WATERMARK["attempts"] = _WATERMARK.get("attempts", 0) + 1
    return _WATERMARK


async def _watermark_auto():
    """后台自动补开（新号登录后立即生效）。加运行锁避免并发重复写。"""
    global _WM_RUNNING
    if _WM_RUNNING:
        return None
    _WM_RUNNING = True
    try:
        r = await _watermark_ensure(True)
        _record_watermark(r)
        print("[watermark] auto ok=%s enabled=%s %s"
              % (r.get("ok"), r.get("enabled"), r.get("detail", "")[:100]), flush=True)
        return r
    finally:
        _WM_RUNNING = False



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
        with _open(req, timeout=timeout) as r:
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
    """页内执行（执行器无关）：cdp 后端走 headless Chromium 的 CDP，bsk 后端走浏览器桥。

    🔴 页内 JS（JS_CHAT / JS_IMAGE / JS_UPLOAD_REF）会读全局 `__ybStaticHeaders`，
    而该全局在 **页面重建 / reset_login / 容器重启** 后都会丢 —— 原实现只定义了
    JS_INJECT_STATIC 却从没调用过，导致页面数据面一跑就 `ReferenceError: __ybStaticHeaders is not defined`。
    这里把注入与调用拼进**同一次** Runtime.evaluate（不增加往返），保证每次执行前都就绪。
    """
    if YB_MINT_BACKEND == "cdp":
        import cdp_minter
        return cdp_minter.get_minter().evaluate(JS_INJECT_STATIC + ";\n" + js, timeout_s=timeout)
    ctx = _ensure_page()
    return _ev(JS_INJECT_STATIC + ";\n" + js, tab_id=ctx["tabId"], timeout=timeout)


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
def _screenshot_png(m):
    """CDP 截图（返回图片响应）。保留兼容：统一走 m.screenshot（内部持锁，原子会话）。"""
    try:
        png = m.screenshot(None)
    except Exception as e:
        return JSONResponse({"error": str(e)[:200]}, status_code=502)
    return Response(content=png, media_type="image/png")


@app.get("/login")
async def login_page(req: Request):
    """headless 部署模式的扫码入口：返回当前元宝页面的截图（含登录二维码）。需门禁 key。
    支持 ?tab=wechat|phone 先切换到对应登录方式（wechat 会刷新二维码）。"""
    cred = _check_auth(req)
    if isinstance(cred, JSONResponse):
        return cred
    if YB_MINT_BACKEND != "cdp":
        return JSONResponse({"error": "仅 cdp 后端支持"}, status_code=400)
    try:
        import cdp_minter
        m = cdp_minter.get_minter()
        tab = (req.query_params.get("tab") or "").strip().lower()
        # 🔴 必须整体交给 m.screenshot（内部一次性持锁，切页+截图是一个原子 CDP 会话）：
        # 旧写法把 ensure_page/两次 evaluate/截图拆成 4 段、跨"事件循环线程 + to_thread 线程"，
        # 同一 ws 上交错收包会互相偷回包（探针 id=1 vs evaluate id=20）⇒ /login 永久 hang。
        png = await asyncio.to_thread(
            m.screenshot, tab if tab in ("wechat", "phone") else None)
        return Response(content=png, media_type="image/png")
    except Exception as e:
        import traceback
        traceback.print_exc()  # 502 必须留痕（否则 docker logs 里只有一行 502，无从排查）
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
  const byText = (txts, exact) => [...document.querySelectorAll('*')]
    .filter(e => e.offsetWidth > 0 && e.children.length <= 2)
    .filter(e => { const t = (e.textContent || '').trim();
      return txts.some(x => exact ? t === x : t.startsWith(x)); })
    .sort((a, b) => (a.offsetWidth * a.offsetHeight) - (b.offsetWidth * b.offsetHeight))[0];

  // 0) 若登录弹窗未开/未到手机表单，逐级导航（中英文双语匹配）
  if (!document.querySelector('.hyc-phone-login')) {
    const loginBtn = byText(['Log In', '登录'], true) || byText(['Log In', '登录'], false);
    if (loginBtn) { loginBtn.click(); await sleep(2500); }
    if (!document.querySelector('.hyc-phone-login')) {
      const phoneTab = byText(['Phone', '手机'], true) || byText(['Phone', '手机'], false);
      if (phoneTab) { phoneTab.click(); await sleep(2000); }
    }
  }
  if (!document.querySelector('.hyc-phone-login')) return {ok: false, step: 'open_modal', msg: '登录弹窗未出现'};

  // 1) 切区号
  const areaEl = document.querySelector('.yuanbao-oversea-input__wrap__formitem__areaCode');
  if (areaEl && !areaEl.textContent.includes(p.area.replace('+', ''))) {
    areaEl.click();
    await sleep(800);
    const opt = byText([p.area], false);
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


@app.get("/admin/state")
async def admin_state(req: Request):
    """容器页面状态：已登录 / 已被冻结（冻结时页面上没有"登录"按钮，必须先重置登录态）。"""
    cred = _check_admin_panel_auth(req)
    if isinstance(cred, JSONResponse):
        return cred
    if YB_MINT_BACKEND != "cdp":
        return JSONResponse({"error": "仅 cdp 后端支持"}, status_code=400)
    import cdp_minter
    m = cdp_minter.get_minter()
    st = await asyncio.to_thread(m.page_state)
    # 登录态出现后 ~一个轮询周期内自动补开"无水印保存"（新号扫码后即生效）
    if (YB_AUTO_WATERMARK and st.get("logged_in") and not st.get("frozen")
            and not _WM_RUNNING and time.time() - _WATERMARK.get("at", 0) > 60):
        asyncio.create_task(_watermark_auto())
    return {"result": st, "keepalive": _KEEPALIVE, "watermark": _WATERMARK}


@app.api_route("/admin/keepalive", methods=["GET", "POST"])
async def admin_keepalive(req: Request):
    """主动保活/探活一次（也可由外部 cron 定时打这个端点，代替进程内定时任务）。"""
    cred = _check_admin_panel_auth(req)
    if isinstance(cred, JSONResponse):
        return cred
    if YB_MINT_BACKEND != "cdp":
        return JSONResponse({"error": "仅 cdp 后端支持"}, status_code=400)
    r = await _keepalive_once()
    return {"result": r, "last": _record_keepalive(r)}


@app.api_route("/admin/watermark", methods=["GET", "POST"])
async def admin_watermark(req: Request):
    """账号级「无水印保存」开关（下载图片/视频不带水印）。

    GET                     → 查看当前配置 + 灰度资质
    POST {"enabled": true}  → 开启（等价 UI 里的"无水印保存"开关）
    POST {"enabled": false} → 关闭
    POST {"force": true}    → 已是目标状态也重写一次
    新号登录后由保活循环 + /admin/state 自动补开（YB_AUTO_WATERMARK=1，默认开）。
    """
    cred = _check_admin_panel_auth(req)
    if isinstance(cred, JSONResponse):
        return cred
    if YB_MINT_BACKEND != "cdp":
        return JSONResponse({"error": "仅 cdp 后端支持"}, status_code=400)
    import cdp_minter
    if req.method == "GET":
        try:
            cookie = await asyncio.to_thread(cdp_minter.get_minter().extract_cookies)
        except Exception as e:
            return JSONResponse({"error": str(e)[:160]}, status_code=502)
        st, cfg = await asyncio.to_thread(_wm_read, cookie)
        gray = await asyncio.to_thread(_wm_gray_ok, cookie)
        return {"result": {"http": st, "config": cfg, "gray_ok": gray,
                           "enabled": cfg.get("saveWithoutWatermark")},
                "last": _WATERMARK}
    try:
        body = await req.json()
    except Exception:
        body = {}
    target = bool(body.get("enabled", True))
    r = await _watermark_ensure(target, force=bool(body.get("force")))
    return {"result": r, "last": _record_watermark(r)}


@app.get("/admin/pool")
async def admin_pool(req: Request):
    """号池总览（**服务端聚合**）：本实例 + 各 peer 的页面状态 / 保活状态。

    走服务端代查而不是浏览器跨域 fetch —— 后者会被 CORS 拦掉，且 peer 的 key 不该下发到前端。
    peer 鉴权用 YB_FLEET_KEY（各实例共享的只读 key）。
    """
    cred = _check_admin_panel_auth(req)
    if isinstance(cred, JSONResponse):
        return cred
    import cdp_minter
    me_state = {}
    try:
        me_state = await asyncio.to_thread(cdp_minter.get_minter().page_state)
    except Exception as e:
        me_state = {"error": str(e)[:120]}
    me = {"name": YB_INSTANCE_NAME_ENV, "url": YB_BASE_URL_ENV, "self": True,
          "ok": True, "state": me_state, "keepalive": _KEEPALIVE}

    def _fetch(p):
        import requests
        url = p["url"].rstrip("/") + "/admin/state"
        hdr = {"Authorization": "Bearer " + (YB_FLEET_KEY or YUANBAO_API_KEY)}
        try:
            r = requests.get(url, headers=hdr, timeout=20)
            if r.status_code == 401:
                return {**p, "ok": False, "error": "鉴权失败（peer 需设置相同的 YB_FLEET_KEY）"}
            j = r.json()
            return {**p, "ok": r.ok, "state": j.get("result"), "keepalive": j.get("keepalive")}
        except Exception as e:
            return {**p, "ok": False, "error": str(e)[:140]}

    peers = [pp for pp in _pool_peers() if pp["url"].rstrip("/") != YB_BASE_URL_ENV]
    results = await asyncio.gather(*[asyncio.to_thread(_fetch, p) for p in peers]) if peers else []
    return {"self": me, "peers": list(results), "fleet_key_set": bool(YB_FLEET_KEY)}


# ---------------- 统计 & 号池批量运维 ----------------
def _fleet_peers_all():
    """所有实例（含自己），self 标记自身 —— 批量操作与汇总统计的目标集合。"""
    out = [{**pp, "self": pp["url"].rstrip("/") == YB_BASE_URL_ENV} for pp in _pool_peers()]
    if YB_BASE_URL_ENV and not any(x["self"] for x in out):
        out.insert(0, {"id": "self", "name": YB_INSTANCE_NAME_ENV, "url": YB_BASE_URL_ENV, "self": True})
    return out


def _fleet_http(url: str, path: str, method: str = "GET", payload=None, timeout: int = 60):
    """服务端代调某个实例（用 YB_FLEET_KEY）。"""
    import requests
    hdr = {"Authorization": "Bearer " + (YB_FLEET_KEY or YUANBAO_API_KEY)}
    try:
        u = url.rstrip("/") + path
        r = (requests.post(u, headers=hdr, json=payload or {}, timeout=timeout)
             if method == "POST" else requests.get(u, headers=hdr, timeout=timeout))
        if r.status_code == 401:
            return {"ok": False, "error": "鉴权失败（peer 需配置相同 YB_FLEET_KEY）"}
        return {"ok": r.ok, "data": (r.json() if r.content else None), "status": r.status_code}
    except Exception as e:
        return {"ok": False, "error": str(e)[:140]}


@app.get("/admin/stats")
async def admin_stats(req: Request, days: int = 7):
    """本实例调用统计：账号 × 模型 × 端点 × 状态 + 按天走势。"""
    cred = _check_admin_panel_auth(req)
    if isinstance(cred, JSONResponse):
        return cred
    if METRICS is None:
        return JSONResponse({"error": "统计未启用（YB_STATS_DIR 不可写）"}, status_code=503)
    return {"result": METRICS.summary(days), "instance": YB_INSTANCE_NAME_ENV}


@app.get("/admin/stats/detail")
async def admin_stats_detail(req: Request, limit: int = 100, model: str = "",
                             ok: str = "", endpoint: str = "", instance: str = ""):
    """明细日志（最近优先，可按模型/结果/端点过滤）。"""
    cred = _check_admin_panel_auth(req)
    if isinstance(cred, JSONResponse):
        return cred
    if METRICS is None:
        return JSONResponse({"error": "统计未启用"}, status_code=503)
    rows = METRICS.detail(limit, model or None, ok or None, endpoint or None, instance or None)
    return {"result": rows, "count": len(rows), "instance": YB_INSTANCE_NAME_ENV}


@app.get("/admin/stats/export")
async def admin_stats_export(req: Request):
    """导出原始 JSONL（一行一次调用，可直接喂分析工具）。"""
    cred = _check_admin_panel_auth(req)
    if isinstance(cred, JSONResponse):
        return cred
    if METRICS is None:
        return JSONResponse({"error": "统计未启用"}, status_code=503)
    name = os.path.basename(METRICS.path)
    data = b""
    if os.path.exists(METRICS.path):
        with open(METRICS.path, "rb") as f:
            data = f.read()
    return Response(content=data, media_type="application/x-ndjson",
                    headers={"Content-Disposition": 'attachment; filename="%s"' % name})


@app.post("/admin/stats/prune")
async def admin_stats_prune(req: Request):
    cred = _check_admin_panel_auth(req)
    if isinstance(cred, JSONResponse):
        return cred
    if METRICS is None:
        return JSONResponse({"error": "统计未启用"}, status_code=503)
    return {"result": METRICS.prune()}


@app.get("/admin/fleet/stats")
async def admin_fleet_stats(req: Request, days: int = 7):
    """号池汇总统计：合并各实例 summary ⇒ **账号 × 模型**矩阵。"""
    cred = _check_admin_panel_auth(req)
    if isinstance(cred, JSONResponse):
        return cred
    targets = _fleet_peers_all()

    async def one(t):
        if t.get("self"):
            return {**t, "ok": METRICS is not None,
                    "data": {"result": METRICS.summary(days)} if METRICS else None}
        r = await asyncio.to_thread(_fleet_http, t["url"], "/admin/stats?days=%d" % days)
        return {**t, **r}

    res = await asyncio.gather(*[one(t) for t in targets]) if targets else []
    merged, per_acc = {}, []
    for t in res:
        d = ((t.get("data") or {}).get("result")) or {}
        w = d.get("window") or {}
        per_acc.append({
            "name": t.get("name") or t.get("url"), "url": t.get("url"),
            "self": bool(t.get("self")), "ok": bool(t.get("ok")), "error": t.get("error"),
            "total": w.get("total", 0), "ok_n": w.get("ok", 0), "err_n": w.get("err", 0),
            "all_time": (d.get("all_time") or {}).get("total", 0),
            "models": {m["model"]: m["n"] for m in (d.get("by_model") or [])},
        })
        for m in (d.get("by_model") or []):
            e = merged.setdefault(m["model"], {"model": m["model"], "n": 0, "ok": 0, "err": 0})
            e["n"] += m["n"]; e["ok"] += m.get("ok", 0); e["err"] += m.get("err", 0)
    return {"accounts": per_acc, "by_model": sorted(merged.values(), key=lambda x: -x["n"]),
            "fleet_key_set": bool(YB_FLEET_KEY), "window_days": days}


@app.post("/admin/fleet/{op}")
async def admin_fleet_op(req: Request, op: str):
    """批量运维扇出。op ∈ keepalive（批量保活）| watermark（批量无水印）| state（批量状态）。
    body: {"enabled": true|false} 供 watermark 使用。
    """
    cred = _check_admin_panel_auth(req)
    if isinstance(cred, JSONResponse):
        return cred
    try:
        body = await req.json()
    except Exception:
        body = {}
    spec = {"keepalive": ("/admin/keepalive", "POST"),
            "watermark": ("/admin/watermark", "POST"),
            "state": ("/admin/state", "GET")}.get(op)
    if not spec:
        return JSONResponse({"error": "未知操作：%s（支持 keepalive|watermark|state）" % op}, status_code=400)
    path, method = spec
    targets = _fleet_peers_all()

    async def one(t):
        if t.get("self"):
            try:
                if op == "keepalive":
                    r = await _keepalive_once(); _record_keepalive(r)
                    return {**t, "ok": bool(r.get("ok")), "data": {"result": r}}
                if op == "watermark":
                    r = await _watermark_ensure(bool(body.get("enabled", True)))
                    _record_watermark(r)
                    return {**t, "ok": bool(r.get("ok")), "data": {"result": r}}
                import cdp_minter
                st = await asyncio.to_thread(cdp_minter.get_minter().page_state)
                return {**t, "ok": True, "data": {"result": st}}
            except Exception as e:
                return {**t, "ok": False, "error": str(e)[:140]}
        r = await asyncio.to_thread(_fleet_http, t["url"], path, method, body)
        return {**t, **r}

    res = await asyncio.gather(*[one(t) for t in targets]) if targets else []
    ok_n = sum(1 for x in res if x.get("ok"))
    return {"op": op, "total": len(res), "ok": ok_n, "failed": len(res) - ok_n, "results": res}


# ---------------- 号池生命周期：状态 / 管理 / 轮询路由 ----------------
_PEER_STATE_CACHE: dict = {}          # url -> (ts, state_dict)
_ROT_IDX = 0
_ROT_LOCK = threading.Lock()


def _peer_state_cached(url: str, ttl: int = 30):
    now = time.time()
    hit = _PEER_STATE_CACHE.get(url)
    if hit and now - hit[0] < ttl:
        return hit[1]
    r = _fleet_http(url, "/admin/account")
    st = ((r.get("data") or {}).get("result")) if r.get("ok") else None
    _PEER_STATE_CACHE[url] = (now, st)
    return st


def _router_targets():
    """可参与轮询的实例：自己 + 各 peer 中 state=enabled 且最近健康非 False。"""
    out = []
    for t in _fleet_peers_all():
        if t.get("self"):
            st = ACCOUNT.get() if ACCOUNT else {"state": "enabled", "routable": True, "last_health": None}
            h = st.get("last_health") or {}
            if st.get("routable") and h.get("ok") is not False:
                out.append({**t, "state": st, "key": YUANBAO_API_KEY})
        else:
            st = _peer_state_cached(t["url"])
            if st and st.get("routable"):
                out.append({**t, "state": st})
    return out


def _router_pick():
    global _ROT_IDX
    ts = _router_targets()
    if not ts:
        return None
    with _ROT_LOCK:
        t = ts[_ROT_IDX % len(ts)]
        _ROT_IDX += 1
    return t


@app.get("/admin/account")
async def admin_account(req: Request):
    """本实例账号的状态机快照（启用/禁用/隔离/剔除 + 健康 + 变更历史）。"""
    cred = _check_admin_panel_auth(req)
    if isinstance(cred, JSONResponse):
        return cred
    if ACCOUNT is None:
        return {"result": {"state": "unknown", "label": "未启用", "routable": True},
                "instance": YB_INSTANCE_NAME_ENV}
    return {"result": ACCOUNT.get(), "instance": YB_INSTANCE_NAME_ENV}


@app.post("/admin/account")
async def admin_account_op(req: Request):
    """账号生命周期操作。body: {action, reason, name?}
    action ∈ enable|disable|eject|restore|reset|note；带 name 则转发给对应实例执行。
    """
    cred = _check_admin_panel_auth(req)
    if isinstance(cred, JSONResponse):
        return cred
    try:
        body = await req.json()
    except Exception:
        body = {}
    action = (body.get("action") or "").strip()
    reason = (body.get("reason") or "").strip()
    target = (body.get("name") or "").strip()
    if target and target != YB_INSTANCE_NAME_ENV:
        t = next((x for x in _fleet_peers_all() if x.get("name") == target), None)
        if not t:
            return JSONResponse({"error": "未知实例：%s" % target}, status_code=404)
        r = await asyncio.to_thread(_fleet_http, t["url"], "/admin/account", "POST",
                                    {"action": action, "reason": reason}, 30)
        _PEER_STATE_CACHE.pop(t["url"], None)
        return {"result": (r.get("data") or {}).get("result") if r.get("ok") else None,
                "via": "fleet", "target": target, "upstream": r}
    if ACCOUNT is None:
        return JSONResponse({"error": "状态机未启用（YB_STATE_DIR 不可写）"}, status_code=503)
    out = ACCOUNT.apply(action, reason, by="manual")
    print("[pool_state] manual %s by=%s → %s（%s）"
          % (action, "api", out.get("state", {}).get("state"), reason) if out.get("ok") else "", flush=True)
    return {"result": out, "instance": YB_INSTANCE_NAME_ENV}


@app.get("/admin/accounts")
async def admin_accounts(req: Request):
    """整个号池的账号清单：状态 + 健康 + 近 24h 调用量（管理页数据源）。"""
    cred = _check_admin_panel_auth(req)
    if isinstance(cred, JSONResponse):
        return cred
    targets = _fleet_peers_all()

    async def one(t):
        if t.get("self"):
            st = ACCOUNT.get() if ACCOUNT else {"state": "unknown", "routable": True}
            stat = METRICS.summary(1) if METRICS else {}
            return {**t, "ok": True, "state": st, "stats": (stat.get("window") or {})}
        r = await asyncio.to_thread(_fleet_http, t["url"], "/admin/account", "GET", None, 20)
        s2 = await asyncio.to_thread(_fleet_http, t["url"], "/admin/stats?days=1", "GET", None, 20)
        return {**t, "ok": bool(r.get("ok")),
                "state": ((r.get("data") or {}).get("result")),
                "stats": (((s2.get("data") or {}).get("result") or {}).get("window") or {}),
                "error": r.get("error")}

    res = await asyncio.gather(*[one(t) for t in targets]) if targets else []
    routable = [x for x in res if (x.get("state") or {}).get("routable")]
    return {"accounts": res, "routable": len(routable), "total": len(res),
            "router_enabled": YB_ROUTER, "router_entry": "/pool/v1",
            "auto": {"disable_after": YB_AUTO_DISABLE_AFTER,
                     "reenable_after": YB_AUTO_REENABLE_AFTER,
                     "eject_after_days": YB_AUTO_EJECT_AFTER_DAYS},
            "rotation_index": _ROT_IDX}


@app.api_route("/pool/v1/{rest:path}", methods=["GET", "POST"])
async def pool_router(req: Request, rest: str):
    """**轮询入口**：/pool/v1/<rest> 轮询分发到各可用账号，失败自动切下一个。

    与直连入口的分工：
      /v1/*       → 只用本实例自己的账号（直连，行为不变）
      /pool/v1/*  → 在整个号池里轮询（本接口；调用方只认一个 key）
    仅 state=enabled 的账号参与；遇到 5xx/连接错误自动 failover 到下一个。
    """
    cred = _check_auth(req)
    if isinstance(cred, JSONResponse):
        return cred
    if not YB_ROUTER:
        return JSONResponse({"error": "轮询入口未启用（YB_ROUTER=0）"}, status_code=404)
    import requests
    raw = await req.body()
    tried, last_err = [], None
    cand = _router_targets()
    if not cand:
        return JSONResponse({"error": {"message": "号池内没有可用账号（全部被禁用/隔离/剔除）",
                                       "type": "api_error"}}, status_code=503)
    for _ in range(min(3, len(cand))):
        t = _router_pick()
        if not t:
            break
        tried.append(t.get("name"))
        url = t["url"].rstrip("/") + "/v1/" + rest
        hdr = {"content-type": req.headers.get("content-type", "application/json")}
        tkey = t.get("key") or ""
        if tkey:
            hdr["Authorization"] = "Bearer " + tkey
        try:
            up = await asyncio.to_thread(
                lambda: requests.request(req.method, url, data=raw, headers=hdr,
                                         stream=True, timeout=(15, 600)))
        except Exception as e:
            last_err = "%s: %s" % (type(e).__name__, str(e)[:120])
            continue
        if up.status_code >= 500:
            last_err = "上游 %s 返回 %d" % (t.get("name"), up.status_code)
            try:
                up.close()
            except Exception:
                pass
            continue

        def gen(resp=up):
            try:
                for chunk in resp.iter_content(chunk_size=8192):
                    if chunk:
                        yield chunk
            finally:
                try:
                    resp.close()
                except Exception:
                    pass

        # 同步生成器：Starlette 会自动放到线程池迭代，不会阻塞事件循环
        return StreamingResponse(gen(), status_code=up.status_code,
                                 media_type=up.headers.get("content-type", "application/json"),
                                 headers={"X-YB-Routed-To": str(t.get("name"))})
    return JSONResponse({"error": {"message": "号池轮询全部失败：%s（已尝试 %s）" % (last_err, tried),
                                   "type": "api_error"}}, status_code=503)


@app.post("/login/reset")
async def login_reset(req: Request):
    """重置登录态：清 cookie + localStorage（保留设备种子）后重载页面 —— 换账号登录前必做。"""
    cred = _check_auth(req)
    if isinstance(cred, JSONResponse):
        return cred
    if YB_MINT_BACKEND != "cdp":
        return JSONResponse({"error": "仅 cdp 后端支持"}, status_code=400)
    import cdp_minter
    m = cdp_minter.get_minter()
    try:
        r = await asyncio.to_thread(m.reset_login)
        # 换号后必须重新判定无水印状态（旧号的结论不适用于新号）
        _WATERMARK.update({"at": 0, "ok": None, "enabled": None,
                           "detail": "登录态已重置，待新号登录后自动补开", "attempts": 0})
        return {"result": r, "hint": "已回到未登录态；新号登录后会自动开启无水印保存"}
    except Exception as e:
        import traceback
        traceback.print_exc()
        return JSONResponse({"error": str(e)[:200]}, status_code=502)


import json
import os

QR_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>元宝登录</title>
<style>
  :root { --bg:#faf9f7; --line:#e6e4df; --text:#2c2c2a; --muted:#6b6a66; --accent:#534AB7; }
  * { box-sizing:border-box; }
  body { margin:0; padding:20px; background:var(--bg); color:var(--text); font:14px/1.6 -apple-system,"PingFang SC",sans-serif; text-align:center; }
  h1 { font-size:16px; font-weight:500; margin:0 0 12px; }
  .wrap { max-width:520px; margin:0 auto; background:#fff; border:1px solid var(--line); border-radius:12px; padding:14px; }
  img { width:100%; border-radius:8px; display:block; }
  input, select { width:100%; padding:9px 11px; border:1px solid var(--line); border-radius:8px; font:13px inherit; background:#fff; color:var(--text); }
  .row { display:flex; gap:8px; align-items:center; margin-top:10px; }
  button { flex:1; padding:9px 12px; border:1px solid var(--accent); background:var(--accent); color:#fff; border-radius:8px; font:13px inherit; cursor:pointer; }
  button.ghost { background:#fff; color:var(--accent); }
  button:disabled { opacity:.5; cursor:not-allowed; }
  .tabs { display:flex; gap:6px; margin:2px 0 12px; }
  .tab { flex:1; padding:8px 10px; border:1px solid var(--line); background:#fff; color:var(--muted); border-radius:8px; font:13px inherit; cursor:pointer; }
  .tab.on { border-color:var(--accent); color:var(--accent); font-weight:500; background:#f3f1fc; }
  .muted { color:var(--muted); font-size:12px; margin-top:8px; }
  .status { margin-top:8px; font-size:12px; }
  .ok { color:#3B6D11; } .err { color:#A32D2D; } .warn { color:#854F0B; }
  .warnbar { background:#fdf3e3; border:1px solid #e8cfa0; color:#854F0B; border-radius:8px;
             padding:8px 10px; font-size:12px; margin-bottom:10px; text-align:left; }
  .okbar { background:#eef7e9; border:1px solid #b8d9a4; color:#3B6D11; border-radius:8px;
           padding:8px 10px; font-size:12px; margin-bottom:10px; text-align:left; }
</style>
</head>
<body>
<h1>元宝登录（登录态持久化到容器）</h1>
<div class="wrap">
  <input id="key" type="password" placeholder="门禁 Key（浏览器本地保存）">
  <div class="muted" id="kinfo" style="margin:-4px 0 10px"></div>
  <div id="warn" style="display:none"></div>

  <div class="tabs">
    <button class="tab on" id="tWx" onclick="setTab('wechat')">微信扫码</button>
    <button class="tab"    id="tPh" onclick="setTab('phone')">手机号登录</button>
  </div>

  <div id="paneWx">
    <img id="qr" alt="登录二维码">
  </div>

  <div id="panePh" style="display:none">
    <div class="row" style="margin-top:0">
      <select id="area" style="max-width:150px">
        <option value="+852">+852 中国香港</option>
        <option value="+86">+86 中国大陆</option>
      </select>
      <input id="phone" placeholder="手机号（不含区号）" style="flex:1">
    </div>
    <button id="sendBtn" onclick="sendCode()" style="margin-top:10px">发送验证码</button>
    <div class="row">
      <input id="code" placeholder="6 位验证码" style="flex:1">
      <button onclick="verifyCode()">提交登录</button>
    </div>
    <div class="status" id="phSt">未发送</div>
    <img id="shot" alt="手机号登录页面截图" style="margin-top:12px">
    <div class="muted">⚠️ 接码平台的虚拟号段大概率被风控直接拒（AQ1001）；自有真实号更可靠。微信扫码不过短信风控。</div>
  </div>

  <div class="row">
    <button class="ghost" onclick="refresh(true)">立即刷新</button>
    <button class="ghost" onclick="saveKey()">保存 Key</button>
  </div>
  <div class="row">
    <button class="ghost" onclick="resetLogin()">重置登录态（清 cookie · 换账号用）</button>
  </div>
  <div class="status" id="st">等待 Key…</div>
  <div class="muted" id="tip">用微信扫码完成登录 · 自动刷新 · 已登录后此处显示当前页面</div>
</div>
<script>
const _qk = (new URLSearchParams(location.search).get('k') || '').trim();
let KEY = '';
try { KEY = _qk || (localStorage.getItem('yb_key') || '').trim(); if (_qk) localStorage.setItem('yb_key', _qk); } catch (e) { KEY = _qk || ''; }
let TAB = (new URLSearchParams(location.search).get('tab') === 'phone') ? 'phone' : 'wechat';
const $ = (id) => document.getElementById(id);
const st = $('st'), phSt = $('phSt');
$('key').value = KEY;
(function () {
  const e = $('kinfo'); if (!e) return;
  e.textContent = KEY ? ('当前 Key：' + KEY.slice(0, 12) + '…' + KEY.slice(-4) + '（长度 ' + KEY.length + '）')
                      : '未载入 Key —— 请用带 ?k= 的完整链接打开本页';
})();

function setTab(t) {
  TAB = t;
  $('tWx').className = 'tab' + (t === 'wechat' ? ' on' : '');
  $('tPh').className = 'tab' + (t === 'phone' ? ' on' : '');
  $('paneWx').style.display = (t === 'wechat') ? '' : 'none';
  $('panePh').style.display = (t === 'phone') ? '' : 'none';
  $('tip').textContent = (t === 'wechat')
    ? '用微信扫码完成登录 · 自动刷新 · 已登录后此处显示当前页面'
    : '切换容器页面到手机号登录 · 填号 → 发送 → 填验证码 → 提交';
  refresh(true);
}

function bindImg(im, label) {
  im.onload = () => {
    st.textContent = label + '已刷新 ' + new Date().toLocaleTimeString()
                     + (TAB === 'wechat' ? ' · 每 20 秒自动刷新' : '');
    st.className = 'status ok';
  };
  im.onerror = () => {
    st.textContent = label + '截图加载失败：Key 可能不完整（长度 43）· 请用带 ?k= 的完整链接打开';
    st.className = 'status err';
  };
}

function refresh(force) {
  if (!KEY) { st.textContent = '请先填写门禁 Key'; st.className = 'status err'; return; }
  const k = encodeURIComponent(KEY);
  if (TAB === 'wechat') {
    const im = $('qr'); bindImg(im, ''); im.src = '/login?tab=wechat&k=' + k + '&t=' + Date.now();
  } else {
    const im = $('shot'); bindImg(im, '登录页'); im.src = '/login?tab=phone&k=' + k + '&t=' + Date.now();
  }
}

function saveKey() {
  KEY = $('key').value.trim();
  try { localStorage.setItem('yb_key', KEY); } catch (e) {}
  refresh(true);
}

async function sendCode() {
  if (!KEY) { st.textContent = '请先填写门禁 Key'; st.className = 'status err'; return; }
  const phone = $('phone').value.trim(), area = $('area').value;
  if (!phone) { phSt.textContent = '请填手机号'; phSt.className = 'status warn'; return; }
  $('sendBtn').disabled = true;
  phSt.textContent = '发送中…'; phSt.className = 'status';
  try {
    const r = await fetch('/login/phone/send', {
      method: 'POST',
      headers: { 'Authorization': 'Bearer ' + KEY, 'content-type': 'application/json' },
      body: JSON.stringify({ phone: phone, area: area })
    });
    const d = await r.json();
    const res = d.result || {};
    if (res.toast) {
      const bad = String(res.toast).toLowerCase().indexOf('valid') >= 0;
      phSt.textContent = '页面返回：' + res.toast;
      phSt.className = 'status ' + (bad ? 'err' : 'ok');
    } else if (res.err) {
      phSt.textContent = res.err; phSt.className = 'status err';
    } else {
      phSt.textContent = '已触发发送（' + (res.area || area) + '）· 收到验证码后填入下方';
      phSt.className = 'status ok';
    }
    refresh(true);
  } catch (e) { phSt.textContent = String(e); phSt.className = 'status err'; }
  finally { $('sendBtn').disabled = false; }
}

async function verifyCode() {
  if (!KEY) { st.textContent = '请先填写门禁 Key'; st.className = 'status err'; return; }
  const code = $('code').value.trim();
  if (!code) { phSt.textContent = '请填验证码'; phSt.className = 'status warn'; return; }
  phSt.textContent = '提交中…'; phSt.className = 'status';
  try {
    const r = await fetch('/login/phone/verify', {
      method: 'POST',
      headers: { 'Authorization': 'Bearer ' + KEY, 'content-type': 'application/json' },
      body: JSON.stringify({ code: code })
    });
    const d = await r.json();
    const res = d.result || {};
    if (res.logged_in) { phSt.textContent = '登录成功 ✓ 登录态已持久化到容器'; phSt.className = 'status ok'; }
    else { phSt.textContent = '未登录成功' + (res.toast ? '：' + res.toast : ''); phSt.className = 'status err'; }
    refresh(true);
  } catch (e) { phSt.textContent = String(e); phSt.className = 'status err'; }
}

async function checkState() {
  if (!KEY) return;
  const w = $('warn');
  try {
    const r = await fetch('/admin/state', { headers: { 'Authorization': 'Bearer ' + KEY } });
    const d = (await r.json()).result || {};
    if (d.frozen) {
      w.className = 'warnbar'; w.style.display = '';
      w.textContent = '⚠️ 容器当前账号已被冻结，页面上没有「登录」按钮 —— 请先点下方「重置登录态」，再扫码或用手机号登入新账号。';
    } else if (d.logged_in) {
      const wm = d.watermark || {};
      const wmTxt = (wm.enabled === true) ? '· 无水印保存 已开启 ✓'
                  : (wm.enabled === false) ? '· 无水印保存 未开启（正在自动补开…）'
                  : '· 无水印保存 待检测';
      w.className = 'okbar'; w.style.display = '';
      w.textContent = '✓ 容器已登录（' + (d.url || '') + '） ' + wmTxt;
    } else {
      w.style.display = 'none';
    }
  } catch (e) {}
}

async function resetLogin() {
  if (!KEY) { st.textContent = '请先填写门禁 Key'; st.className = 'status err'; return; }
  st.textContent = '重置登录态中（清 cookie + localStorage 并重载页面）…'; st.className = 'status';
  try {
    const r = await fetch('/login/reset', { method: 'POST', headers: { 'Authorization': 'Bearer ' + KEY } });
    const d = await r.json();
    st.textContent = d.error ? ('重置失败：' + d.error) : '已重置登录态 · 现在可用微信扫码或手机号登入新账号';
    st.className = 'status ' + (d.error ? 'err' : 'ok');
    checkState(); refresh(true);
  } catch (e) { st.textContent = String(e); st.className = 'status err'; }
}

setInterval(function () { if (TAB === 'wechat') refresh(); checkState(); }, 20000);
setTab(TAB);
checkState();
</script>
</body>
</html>
"""

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
const _qk = (new URLSearchParams(location.search).get('k') || '').trim(); let KEY = ''; try { KEY = _qk || (localStorage.getItem('yb_key') || '').trim(); if (_qk) localStorage.setItem('yb_key', _qk); } catch (e) { KEY = _qk || ''; }
$('key').value = KEY;

function authHeaders() { return { 'Authorization': 'Bearer ' + KEY, 'content-type': 'application/json' }; }
function setMsg(el, text, cls) { const e = $(el); e.textContent = text; e.className = 'msg ' + (cls || ''); }

function saveKey() {
  KEY = $('key').value.trim();
  try { localStorage.setItem('yb_key', KEY); } catch (e) {}
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

function loadShot() {
  if (!KEY) return;
  $('shotBadge').textContent = '加载中';
  const im = $('shot');
  im.onload = () => { $('shotBadge').textContent = new Date().toLocaleTimeString(); $('shotBadge').className = 'badge ok'; };
  im.onerror = () => { $('shotBadge').textContent = '失败'; $('shotBadge').className = 'badge err'; };
  im.src = '/login?k=' + encodeURIComponent(KEY) + '&t=' + Date.now();
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


# 各管理页共用的顶部导航。渲染时用 _page() 统一注入到 <body> 之后，
# 这样每个页面 HTML 不用各自维护一份导航。本地已存的 key 会自动带到目标页。
NAV_HTML = """<style>
  .ybnav { display:flex; gap:8px; flex-wrap:wrap; margin:0 0 16px; font-size:12px; }
  .ybnav a { color:#534AB7; text-decoration:none; border:1px solid #e6e4df; border-radius:99px;
             padding:3px 12px; background:#fff; }
  .ybnav a.cur { border-color:#534AB7; background:#f3f1fc; font-weight:500; }
</style>
<div class="ybnav">
  <a data-h="/pool,/manage" href="/pool">号池管理</a>
  <a data-h="/stats" href="/stats">调用统计</a>
  <a data-h="/qr" href="/qr">扫码 / 登录</a>
  <a data-h="/admin" href="/admin">单实例</a>
</div>
<script>
(function () {
  var k = '';
  try { k = localStorage.getItem('yb_key') || ''; } catch (e) {}
  document.querySelectorAll('.ybnav a').forEach(function (a) {
    var h = (a.getAttribute('data-h') || '').split(',');
    if (k) a.href = h[0] + '?k=' + encodeURIComponent(k);
    if (h.indexOf(location.pathname) >= 0) a.className = 'cur';
  });
})();
</script>
"""


def _page(html: str) -> str:
    """给页面注入共享导航（插在 <body> 之后）。"""
    return html.replace("<body>", "<body>" + NAV_HTML, 1)


MANAGE_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>元宝号池管理</title>
<style>
  :root { --bg:#faf9f7; --card:#fff; --line:#e6e4df; --text:#2c2c2a; --muted:#6b6a66; --accent:#534AB7; --ok:#3B6D11; --err:#A32D2D; --warn:#854F0B; }
  * { box-sizing:border-box; }
  body { margin:0; padding:24px; background:var(--bg); color:var(--text); font:14px/1.6 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif; }
  h1 { font-size:18px; font-weight:500; margin:0 0 4px; }
  .sub { color:var(--muted); font-size:12px; margin-bottom:16px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:16px; margin-bottom:16px; }
  .card h2 { font-size:14px; font-weight:500; margin:0 0 10px; }
  .bar { display:flex; gap:10px; align-items:center; flex-wrap:wrap; margin-bottom:14px; }
  .bar > div { flex:1; min-width:240px; }
  input { width:100%; padding:8px 10px; border:1px solid var(--line); border-radius:8px; font:13px inherit; background:#fff; color:var(--text); }
  button { padding:7px 12px; border:1px solid var(--accent); background:var(--accent); color:#fff; border-radius:8px; font:12px inherit; cursor:pointer; }
  button.ghost { background:#fff; color:var(--accent); }
  button.danger { border-color:var(--err); color:var(--err); background:#fff; }
  button.okb { border-color:var(--ok); color:var(--ok); background:#fff; }
  button:disabled { opacity:.45; cursor:not-allowed; }
  table { width:100%; border-collapse:collapse; font-size:12px; }
  th, td { text-align:left; padding:7px 8px; border-bottom:1px solid var(--line); vertical-align:middle; }
  th { color:var(--muted); font-weight:400; }
  td.num, th.num { text-align:right; }
  .badge { font-size:11px; padding:1px 8px; border-radius:99px; border:1px solid var(--line); color:var(--muted); white-space:nowrap; }
  .badge.ok { color:var(--ok); border-color:var(--ok); }
  .badge.err { color:var(--err); border-color:var(--err); }
  .badge.warn { color:var(--warn); border-color:var(--warn); }
  .muted { color:var(--muted); font-size:12px; }
  .acts { display:flex; gap:6px; flex-wrap:wrap; }
  .rules { display:flex; gap:18px; flex-wrap:wrap; font-size:12px; color:var(--muted); }
  .rules b { color:var(--text); font-weight:500; }
  code { background:#f1efe8; padding:1px 5px; border-radius:4px; font-size:12px; }
  .err-txt { color:var(--err); font-size:12px; }
  .scroll { max-height:300px; overflow:auto; }
</style>
</head>
<body>
<h1>元宝号池管理</h1>
<div class="sub">生命周期（启用 / 禁用 / 隔离 / 剔除）· 自动规则 · 批量运维 · 轮询入口</div>

<div class="bar">
  <div><input id="key" type="password" placeholder="门禁 Key 或 YB_FLEET_KEY"></div>
  <button onclick="saveKey()">保存并刷新</button>
  <button class="ghost" onclick="load()">刷新</button>
  <button class="ghost" onclick="batch('keepalive')">批量保活</button>
  <button class="ghost" onclick="batch('watermark')">批量开无水印</button>
  <button class="ghost" onclick="batch('state')">批量查状态</button>
</div>
<div id="opMsg" class="muted" style="margin-bottom:12px"></div>

<div class="card">
  <h2>账号清单 <span class="muted" id="sum"></span></h2>
  <div id="list" class="muted">加载中…</div>
</div>

<div class="card">
  <h2>自动规则与轮询入口</h2>
  <div class="rules" id="rules"></div>
</div>

<div class="card">
  <h2>状态变更历史</h2>
  <div class="scroll"><div id="hist" class="muted">加载中…</div></div>
</div>

<script>
const $ = (id) => document.getElementById(id);
const _qk = (new URLSearchParams(location.search).get('k') || '').trim();
let KEY = '';
try { KEY = _qk || (localStorage.getItem('yb_key') || '').trim(); if (_qk) localStorage.setItem('yb_key', _qk); } catch (e) { KEY = _qk || ''; }
$('key').value = KEY;
let LAST = null;

function H() { return { 'Authorization': 'Bearer ' + KEY, 'content-type': 'application/json' }; }
function esc(s) { return String(s == null ? '' : s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])); }
function saveKey() { KEY = $('key').value.trim(); try { localStorage.setItem('yb_key', KEY); } catch (e) {} load(); }

const STATE_BADGE = {
  enabled: '<span class="badge ok">启用</span>',
  disabled: '<span class="badge warn">手动禁用</span>',
  quarantined: '<span class="badge err">自动隔离</span>',
  ejected: '<span class="badge err">已剔除</span>',
  unknown: '<span class="badge">未知</span>'
};

function healthBadge(h) {
  if (!h) return '<span class="badge">未探测</span>';
  const t = h.iso || (h.at ? new Date(h.at * 1000).toLocaleString() : '');
  if (h.frozen) return '<span class="badge err" title="' + esc(t) + '">已冻结</span>';
  return h.ok ? '<span class="badge ok" title="' + esc(t) + '">正常 ' + (h.status || '') + '</span>'
              : '<span class="badge err" title="' + esc(t) + '">异常 ' + (h.status == null ? '' : h.status) + '</span>';
}

async function load() {
  if (!KEY) { $('list').textContent = '请先填写门禁 Key'; return; }
  try {
    const r = await fetch('/admin/accounts', { headers: H() });
    if (r.status === 401) { $('list').innerHTML = '<span class="err-txt">Key 被拒绝（401）</span>'; return; }
    const d = await r.json();
    LAST = d;
    $('sum').textContent = '（可路由 ' + d.routable + ' / 共 ' + d.total + '）';
    const a = d.auto || {};
    $('rules').innerHTML =
      '<span>自动隔离：连续失败 ≥ <b>' + a.disable_after + '</b> 次 或 账号被冻结</span>'
      + '<span>自动恢复：连续成功 ≥ <b>' + (a.reenable_after || '关闭') + '</b> 次（仅回滚自动隔离）</span>'
      + '<span>自动剔除：隔离超 <b>' + (a.eject_after_days ? a.eject_after_days + ' 天' : '关闭') + '</b></span>'
      + '<span>轮询入口：<code>' + esc(d.router_entry) + '/…</code> ' + (d.router_enabled ? '<span class="badge ok">已开启</span>' : '<span class="badge warn">已关闭</span>') + '</span>'
      + '<span>已轮询次数：<b>' + (d.rotation_index || 0) + '</b></span>';
    render(d.accounts || []);
  } catch (e) { $('list').innerHTML = '<span class="err-txt">' + esc(e) + '</span>'; }
}

function render(accs) {
  if (!accs.length) { $('list').textContent = '号池为空（YB_POOL_PEERS 未配置）'; return; }
  let h = '<table><tr><th>账号</th><th>地址</th><th>状态</th><th>原因</th><th class="num">连续失败</th><th>健康</th><th class="num">24h(成功/失败)</th><th>操作</th></tr>';
  const hist = [];
  for (const a of accs) {
    const st = a.state || {};
    const s = a.stats || {};
    const nm = esc(a.name || a.url);
    h += '<tr>';
    h += '<td>' + nm + (a.self ? ' <span class="badge">本实例</span>' : '') + '</td>';
    h += '<td class="muted">' + esc(a.error || a.url || '') + '</td>';
    h += '<td>' + (STATE_BADGE[st.state] || STATE_BADGE.unknown) + '</td>';
    h += '<td class="muted">' + esc((st.reason || '').slice(0, 46)) + '</td>';
    h += '<td class="num">' + (st.fail_streak == null ? '·' : st.fail_streak) + '</td>';
    h += '<td>' + healthBadge(st.last_health) + '</td>';
    h += '<td class="num"><span style="color:#3B6D11">' + (s.ok || 0) + '</span> / <span style="color:#A32D2D">' + (s.err || 0) + '</span></td>';
    const name = esc(a.name || '');
    h += '<td><div class="acts">'
      + '<button class="okb" onclick="act(\\'' + name + '\\',\\'enable\\')">启用</button>'
      + '<button class="ghost" onclick="act(\\'' + name + '\\',\\'disable\\')">禁用</button>'
      + '<button class="danger" onclick="act(\\'' + name + '\\',\\'eject\\')">剔除</button>'
      + '<button class="ghost" onclick="act(\\'' + name + '\\',\\'reset\\')">重置</button>'
      + '</div></td>';
    h += '</tr>';
    for (const ev of (st.history || []).slice(-6).reverse()) {
      hist.push({ name: a.name, t: ev.iso || ev.at, action: ev.action, reason: ev.reason, by: ev.by });
    }
  }
  h += '</table>';
  $('list').innerHTML = h;
  hist.sort((x, y) => String(y.t).localeCompare(String(x.t)));
  if (!hist.length) { $('hist').textContent = '暂无变更记录'; }
  else {
    let t = '<table><tr><th>时间</th><th>账号</th><th>动作</th><th>原因</th><th>来源</th></tr>';
    hist.slice(0, 40).forEach(e => {
      t += '<tr><td class="muted">' + esc(e.t) + '</td><td>' + esc(e.name) + '</td><td>' + esc(e.action)
        + '</td><td>' + esc(e.reason || '') + '</td><td class="muted">' + esc(e.by) + '</td></tr>';
    });
    $('hist').innerHTML = t + '</table>';
  }
}

async function act(name, action) {
  let reason = '';
  if (action !== 'enable' && action !== 'reset') {
    reason = prompt('给这次「' + action + '」写个原因（可留空）：', '') || '';
  }
  try {
    const r = await fetch('/admin/account', { method: 'POST', headers: H(),
      body: JSON.stringify({ action: action, reason: reason, name: name }) });
    const d = await r.json();
    $('opMsg').innerHTML = d.error ? '<span class="err-txt">' + esc(d.error) + '</span>'
      : '已对 <b>' + esc(name) + '</b> 执行 <b>' + esc(action) + '</b>';
    load();
  } catch (e) { $('opMsg').innerHTML = '<span class="err-txt">' + esc(e) + '</span>'; }
}

async function batch(op) {
  if (!KEY) return;
  $('opMsg').textContent = '批量 ' + op + ' 执行中…';
  try {
    const r = await fetch('/admin/fleet/' + op, { method: 'POST', headers: H(), body: JSON.stringify({ enabled: true }) });
    const d = await r.json();
    if (d.error) { $('opMsg').innerHTML = '<span class="err-txt">' + esc(d.error) + '</span>'; return; }
    const bad = (d.results || []).filter(x => !x.ok);
    $('opMsg').innerHTML = '批量 ' + op + '：成功 <b>' + d.ok + '</b> / 共 <b>' + d.total + '</b>'
      + (bad.length ? ' · 失败：' + bad.map(x => esc(x.name || x.url)).join('、') : '');
    load();
  } catch (e) { $('opMsg').innerHTML = '<span class="err-txt">' + esc(e) + '</span>'; }
}

if (KEY) load(); else $('list').textContent = '请先填写门禁 Key（或 YB_FLEET_KEY）';
</script>
</body>
</html>
"""


STATS_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>元宝号池统计</title>
<style>
  :root { --bg:#faf9f7; --card:#fff; --line:#e6e4df; --text:#2c2c2a; --muted:#6b6a66; --accent:#534AB7; --ok:#3B6D11; --err:#A32D2D; --warn:#854F0B; }
  * { box-sizing:border-box; }
  body { margin:0; padding:24px; background:var(--bg); color:var(--text); font:14px/1.6 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif; }
  h1 { font-size:18px; font-weight:500; margin:0 0 4px; }
  .sub { color:var(--muted); font-size:12px; margin-bottom:16px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:16px; margin-bottom:16px; }
  .card h2 { font-size:14px; font-weight:500; margin:0 0 10px; }
  .bar { display:flex; gap:10px; align-items:center; flex-wrap:wrap; margin-bottom:14px; }
  .bar > div { flex:1; min-width:240px; }
  input, select { width:100%; padding:8px 10px; border:1px solid var(--line); border-radius:8px; font:13px inherit; background:#fff; color:var(--text); }
  button { padding:8px 13px; border:1px solid var(--accent); background:var(--accent); color:#fff; border-radius:8px; font:13px inherit; cursor:pointer; }
  button.ghost { background:#fff; color:var(--accent); }
  button:disabled { opacity:.5; cursor:not-allowed; }
  table { width:100%; border-collapse:collapse; font-size:12px; }
  th, td { text-align:left; padding:6px 8px; border-bottom:1px solid var(--line); white-space:nowrap; }
  th { color:var(--muted); font-weight:400; }
  td.num, th.num { text-align:right; }
  .badge { font-size:11px; padding:1px 7px; border-radius:99px; border:1px solid var(--line); color:var(--muted); }
  .badge.ok { color:var(--ok); border-color:var(--ok); }
  .badge.err { color:var(--err); border-color:var(--err); }
  .badge.warn { color:var(--warn); border-color:var(--warn); }
  .muted { color:var(--muted); font-size:12px; }
  .ok { color:var(--ok); } .errc { color:var(--err); }
  .spark { display:flex; gap:2px; align-items:flex-end; height:40px; margin-top:6px; }
  .spark i { flex:1; background:#CECBF6; border-radius:2px 2px 0 0; min-height:2px; }
  .spark i.has { background:#534AB7; }
  .grid2 { display:grid; grid-template-columns:repeat(auto-fit,minmax(320px,1fr)); gap:16px; }
  code { background:#f1efe8; padding:1px 5px; border-radius:4px; font-size:12px; }
  .scroll { max-height:420px; overflow:auto; }
</style>
</head>
<body>
<h1>元宝号池统计</h1>
<div class="sub">账号 × 模型 调用量 · 明细日志 · 批量运维（保活 / 无水印 / 状态）</div>

<div class="bar">
  <div><input id="key" type="password" placeholder="门禁 Key 或 YB_FLEET_KEY"></div>
  <button onclick="saveKey()">保存并刷新</button>
  <button class="ghost" onclick="load()">刷新</button>
  <button class="ghost" onclick="fleet('keepalive')">批量保活</button>
  <button class="ghost" onclick="fleet('watermark')">批量开启无水印</button>
  <button class="ghost" onclick="fleet('state')">批量查状态</button>
</div>
<div id="opMsg" class="muted" style="margin-bottom:12px"></div>

<div class="card">
  <h2>账号 × 模型 调用矩阵 <span class="muted" id="winInfo"></span></h2>
  <div id="matrix" class="muted">加载中…</div>
</div>

<div class="grid2">
  <div class="card">
    <h2>本实例模型明细</h2>
    <div id="models" class="muted">加载中…</div>
  </div>
  <div class="card">
    <h2>按天走势</h2>
    <div id="series" class="muted">加载中…</div>
  </div>
</div>

<div class="card">
  <h2>调用明细日志</h2>
  <div class="bar" style="margin-bottom:10px">
    <div style="max-width:160px"><select id="fModel" onchange="loadDetail()"><option value="">全部模型</option></select></div>
    <div style="max-width:130px"><select id="fOk" onchange="loadDetail()"><option value="">全部结果</option><option value="true">成功</option><option value="false">失败</option></select></div>
    <div style="max-width:120px"><select id="fLimit" onchange="loadDetail()"><option>50</option><option selected>100</option><option>500</option></select></div>
    <button class="ghost" onclick="loadDetail()">刷新明细</button>
    <button class="ghost" onclick="exportLog()">导出 JSONL</button>
  </div>
  <div class="scroll"><div id="detail" class="muted">加载中…</div></div>
</div>

<script>
const $ = (id) => document.getElementById(id);
const _qk = (new URLSearchParams(location.search).get('k') || '').trim();
let KEY = '';
try { KEY = _qk || (localStorage.getItem('yb_key') || '').trim(); if (_qk) localStorage.setItem('yb_key', _qk); } catch (e) { KEY = _qk || ''; }
$('key').value = KEY;
let DAYS = 7;
function H() { return { 'Authorization': 'Bearer ' + KEY, 'content-type': 'application/json' }; }
function esc(s) { return String(s == null ? '' : s).replace(/[&<>]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c])); }
function saveKey() { KEY = $('key').value.trim(); try { localStorage.setItem('yb_key', KEY); } catch (e) {} load(); }

async function jget(url, opt) { const r = await fetch(url, opt || { headers: H() }); if (!r.ok && r.status !== 401) throw new Error('HTTP ' + r.status); return r.json(); }

async function load() {
  if (!KEY) { $('matrix').textContent = '请先填写门禁 Key'; return; }
  try {
    const fleet = await jget('/admin/fleet/stats?days=' + DAYS);
    renderMatrix(fleet);
    const st = await jget('/admin/stats?days=' + DAYS);
    renderModels(st.result); renderSeries(st.result);
    loadDetail();
  } catch (e) { $('matrix').innerHTML = '<span class="errc">' + esc(e) + '</span>'; }
}

function renderMatrix(f) {
  const accs = f.accounts || [];
  const models = f.by_model || [];
  $('winInfo').textContent = '（近 ' + f.window_days + ' 天）' + (f.fleet_key_set ? '' : ' · 未设 YB_FLEET_KEY，仅本实例');
  if (!accs.length) { $('matrix').textContent = '无数据'; return; }
  const allModels = [];
  models.forEach(m => allModels.push(m.model));
  accs.forEach(a => Object.keys(a.models || {}).forEach(m => { if (allModels.indexOf(m) < 0) allModels.push(m); }));
  let h = '<table><tr><th>账号</th><th>实例</th><th class="num">窗口调用</th><th class="num">成功</th><th class="num">失败</th>';
  allModels.forEach(m => { h += '<th class="num">' + esc(m) + '</th>'; });
  h += '<th class="num">累计</th></tr>';
  for (const a of accs) {
    const st = a.ok ? '<span class="badge ok">在线</span>' : '<span class="badge err">异常</span>';
    h += '<tr><td>' + esc(a.name) + ' ' + (a.self ? '<span class="badge">本实例</span>' : '') + '</td>'
      + '<td class="muted">' + esc(a.error || a.url || '') + '</td>'
      + '<td class="num">' + a.total + '</td><td class="num ok">' + a.ok_n + '</td>'
      + '<td class="num ' + (a.err_n ? 'errc' : '') + '">' + a.err_n + '</td>';
    allModels.forEach(m => { const n = (a.models || {})[m] || 0; h += '<td class="num">' + (n || '<span class="muted">·</span>') + '</td>'; });
    h += '<td class="num">' + a.all_time + '</td></tr>';
  }
  h += '<tr><th>合计</th><th></th>';
  const tot = accs.reduce((s, a) => s + a.total, 0), okt = accs.reduce((s, a) => s + a.ok_n, 0), ert = accs.reduce((s, a) => s + a.err_n, 0);
  h += '<th class="num">' + tot + '</th><th class="num">' + okt + '</th><th class="num">' + ert + '</th>';
  allModels.forEach(m => { const row = models.find(x => x.model === m); h += '<th class="num">' + (row ? row.n : 0) + '</th>'; });
  h += '<th></th></tr></table>';
  $('matrix').innerHTML = h;

  const sel = $('fModel'); const cur = sel.value;
  sel.innerHTML = '<option value="">全部模型</option>' + allModels.map(m => '<option>' + esc(m) + '</option>').join('');
  sel.value = cur;
}

function renderModels(r) {
  const ms = r.by_model || [];
  if (!ms.length) { $('models').textContent = '窗口内无调用'; return; }
  let h = '<table><tr><th>模型</th><th class="num">次数</th><th class="num">成功/失败</th><th class="num">均耗时</th><th class="num">token(入/出)</th></tr>';
  ms.forEach(m => {
    h += '<tr><td>' + esc(m.model) + '</td><td class="num">' + m.n + '</td>'
      + '<td class="num"><span class="ok">' + m.ok + '</span>/<span class="' + (m.err ? 'errc' : 'muted') + '">' + m.err + '</span></td>'
      + '<td class="num">' + m.avg_ms + 'ms</td>'
      + '<td class="num">' + m.prompt_tokens + '/' + m.completion_tokens + '</td></tr>';
  });
  h += '</table>';
  $('models').innerHTML = h;
}

function renderSeries(r) {
  const s = r.series || [];
  const max = Math.max(1, ...s.map(x => x.n));
  let h = '<div class="spark">';
  s.forEach(x => {
    const pct = Math.round(x.n / max * 100);
    h += '<i class="' + (x.n ? 'has' : '') + '" style="height:' + Math.max(2, pct) + '%" title="' + x.date + ' · ' + x.n + ' 次"></i>';
  });
  h += '</div><div class="muted" style="display:flex;justify-content:space-between;margin-top:4px">'
    + '<span>' + (s[0] ? s[0].date : '') + '</span><span>峰值 ' + max + '</span><span>' + (s.length ? s[s.length - 1].date : '') + '</span></div>';
  const tot = s.reduce((a, x) => a + x.n, 0);
  $('series').innerHTML = h + '<div class="muted" style="margin-top:8px">窗口合计 ' + tot + ' 次</div>';
}

async function loadDetail() {
  if (!KEY) return;
  const q = new URLSearchParams({ limit: $('fLimit').value, model: $('fModel').value, ok: $('fOk').value });
  try {
    const d = await jget('/admin/stats/detail?' + q.toString());
    const rows = d.result || [];
    if (!rows.length) { $('detail').textContent = '暂无记录'; return; }
    let h = '<table><tr><th>时间</th><th>账号</th><th>模型</th><th>端点</th><th class="num">状态</th><th class="num">耗时</th><th>流式</th><th class="num">token</th><th>调用方</th></tr>';
    rows.forEach(e => {
      const u = e.usage || {};
      h += '<tr><td>' + esc(e.ts_iso || '') + '</td><td>' + esc(e.instance || '') + '</td>'
        + '<td>' + esc(e.model || '') + '</td><td class="muted">' + esc((e.endpoint || '').replace('/v1/', '')) + '</td>'
        + '<td class="num"><span class="badge ' + (e.ok ? 'ok' : 'err') + '">' + e.status + '</span></td>'
        + '<td class="num">' + (e.ms || 0) + 'ms</td><td>' + (e.stream ? '是' : '') + '</td>'
        + '<td class="num">' + (u.total_tokens || '') + '</td>'
        + '<td class="muted">' + esc(e.key || '') + '</td></tr>';
      if (e.err) h += '<tr><td></td><td colspan="8" class="errc">' + esc(e.err) + '</td></tr>';
    });
    h += '</table>';
    $('detail').innerHTML = h;
  } catch (e) { $('detail').innerHTML = '<span class="errc">' + esc(e) + '</span>'; }
}

async function fleet(op) {
  if (!KEY) return;
  $('opMsg').textContent = '批量 ' + op + ' 执行中…';
  try {
    const r = await fetch('/admin/fleet/' + op, { method: 'POST', headers: H(), body: JSON.stringify({ enabled: true }) });
    const d = await r.json();
    if (d.error) { $('opMsg').innerHTML = '<span class="errc">' + esc(d.error) + '</span>'; return; }
    const bad = (d.results || []).filter(x => !x.ok);
    $('opMsg').innerHTML = '批量 ' + op + '：成功 <b>' + d.ok + '</b> / 共 <b>' + d.total + '</b>'
      + (bad.length ? ' · 失败：' + bad.map(x => esc(x.name || x.url)).join('、') : '');
    load();
  } catch (e) { $('opMsg').innerHTML = '<span class="errc">' + esc(e) + '</span>'; }
}

async function exportLog() {
  if (!KEY) return;
  const r = await fetch('/admin/stats/export', { headers: { 'Authorization': 'Bearer ' + KEY } });
  const b = await r.blob();
  const a = document.createElement('a');
  a.href = URL.createObjectURL(b); a.download = 'yuanbao-stats.jsonl'; a.click();
}

if (KEY) load(); else $('matrix').textContent = '请先填写门禁 Key（或 YB_FLEET_KEY）';
</script>
</body>
</html>
"""


POOL_HTML = MANAGE_HTML   # /pool 与 /manage 是同一个"号池管理"页（保留 /pool 这个老入口）



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
            name, url = name.strip(), url.strip()
            # 支持 name=url|key —— 轮询转发需要目标实例自己的 API key（/v1 只认实例 key）
            pkey = ""
            if "|" in url:
                url, pkey = url.split("|", 1)
            peers.append({"name": name, "url": url.strip().rstrip("/"), "key": pkey.strip()})
    for i, x in enumerate(peers):
        x["id"] = f"p{i}"
        x.setdefault("name", f"acc{i+1}")
    return peers


@app.get("/admin", response_class=Response)
async def admin_page(req: Request):
    # 页面壳免鉴权（无 Key 者需打开页面输入 Key）；所有数据端点仍校门禁
    return Response(content=_page(ADMIN_HTML), media_type="text/html; charset=utf-8")


@app.get("/qr", response_class=Response)
async def qr_page(req: Request):
    # 扫码专用页（壳免鉴权，截图走带 Key 的 fetch）
    return Response(content=_page(QR_HTML), media_type="text/html; charset=utf-8")


@app.get("/pool", response_class=Response)
async def pool_page(req: Request):
    return Response(content=_page(POOL_HTML), media_type="text/html; charset=utf-8")


@app.get("/stats", response_class=Response)
async def stats_page(req: Request):
    return Response(content=_page(STATS_HTML), media_type="text/html; charset=utf-8")


@app.get("/manage", response_class=Response)
async def manage_page(req: Request):
    return Response(content=_page(MANAGE_HTML), media_type="text/html; charset=utf-8")


@app.get("/admin/whoami")
async def admin_whoami(req: Request):
    cred = _check_auth(req)
    if isinstance(cred, JSONResponse):
        return cred
    return {"name": os.environ.get("YB_INSTANCE_NAME", "yuanbao-proxy"),
            "base_url": os.environ.get("YB_BASE_URL", ""),
            "mint_backend": YB_MINT_BACKEND, "data_plane": YB_DATA_PLANE}


@app.get("/admin/proxy")
async def admin_proxy(req: Request):
    cred = _check_auth(req)
    if isinstance(cred, JSONResponse):
        return cred
    now = time.time()
    return {"pool": [{"url": p, "cooldown_left": round(max(0, _PROXY_BAD.get(p, 0) - now), 1)} for p in _proxy_list()],
            "browser_proxy": os.environ.get("YB_PROXY_URL") or "(未启用)"}


@app.post("/admin/proxy/rotate")
async def admin_proxy_rotate(req: Request):
    """换浏览器出口 IP：从池里取下一个代理，重启 chromium 并重新预热页面。"""
    cred = _check_auth(req)
    if isinstance(cred, JSONResponse):
        return cred
    if YB_MINT_BACKEND != "cdp":
        return JSONResponse({"error": "仅 cdp 后端支持"}, status_code=400)
    pr = _proxy_next()
    if not pr:
        return JSONResponse({"error": "代理池为空（YB_PROXY_POOL/YB_PROXY_URL 未配置）"}, status_code=400)
    import cdp_minter
    m = cdp_minter.get_minter()
    try:
        ok = await asyncio.to_thread(m.restart_with_proxy, pr)
        return {"rotated_to": pr, "ok": ok}
    except Exception as e:
        return JSONResponse({"error": str(e)[:200]}, status_code=502)


@app.get("/admin/peers")
async def admin_peers(req: Request):
    cred = _check_admin_panel_auth(req)
    if isinstance(cred, JSONResponse):
        return cred
    return {"peers": _pool_peers()}


@app.get("/v1/models")
async def list_models(req: Request):
    cred = _check_auth(req)
    _mctx(model="(models)")
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
    _mctx(model=model_in, stream=stream, usage=parsed.get("usage"))
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
    _mctx(endpoint=req.url.path)
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
