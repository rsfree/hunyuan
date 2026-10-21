"""补丁：/v1 直连在本实例账号不可用时，回退到组内 cookie 账号。

实测缺陷（2026-10-21 公网验证）：
  acc01 浏览器号被封（20004）后，用实例 key 直连 https://yuanbao.example.com/v1/images/generations
  → 502「创建会话失败 20004 用户被封号」；而同样的请求走 /pool/v1 → 200 出图。
  根因：/v1 无显式 cookie 时走**页内数据面**，只能用实例自己那一个浏览器，
        它一死整个实例的直连接口就全废 —— 哪怕组里还有健康的 cookie 账号（纯 HTTP，与浏览器无关）。

补法：新增 _self_usable() + _fallback_cookie()，两个 /v1 端点在 cookie 判定之后兜底。
⚠️ 只在"本实例账号不可用"时才兜底 —— 健康时保持页面模式（签名更"真"），不改既有行为。
"""
import io
import sys

P = "yuanbao_openai_proxy.py"
src = io.open(P, encoding="utf-8").read()


def sub(label, a, b, n=1):
    global src
    c = src.count(a)
    assert c == n, "%s count=%d (want %d)" % (label, c, n)
    src = src.replace(a, b)
    print("  ok", label)


# ---------- 1) 新增两个工具函数（紧跟 _router_ok 之后）----------
sub("新增 _self_usable/_fallback_cookie",
    '''def _router_targets():
    """可参与轮询的：本组 cookie 账号 + 自己 + 各 peer（严格按 _router_ok 判定）。''',
    '''def _self_usable() -> bool:
    """本实例自带浏览器账号当前是否可路由（enabled + 最近一次保活成功）。

    ACCOUNT 为 None（老部署/未初始化状态机）时返回 True —— 保持原行为，不擅自兜底。
    """
    if ACCOUNT is None:
        return True
    try:
        return _router_ok(ACCOUNT.get())
    except Exception:
        return True


def _fallback_cookie() -> str:
    """页面模式兜底：本实例浏览器账号不可用 → 改用组内健康的 cookie 账号。

    🔴 为什么需要（2026-10-21 公网实测）：实例浏览器号一被封（20004）或掉登录（23000），
       用实例 key 直连 `/v1/*` 会**全部 502** —— 页内数据面只能用它自己那一个浏览器，
       账号死了整个实例的直连接口就全废，哪怕组里还有健康的 cookie 账号。
       cookie 账号是纯 HTTP，与浏览器状态无关，正好当兜底。
    只在 `_self_usable()` 为 False 时才兜底：健康时继续用页面模式（签名更"真实"）。
    """
    if COOKIE_ACCOUNTS is None or _self_usable():
        return ""
    best = None
    for a in COOKIE_ACCOUNTS.list():
        st = a.get("state") or {}
        if not _router_ok(st):
            continue
        ck = COOKIE_ACCOUNTS.cookie(a["name"])
        if not ck:
            continue
        at = int((st.get("last_health") or {}).get("at") or 0)
        if best is None or at >= best[0]:        # 取最近一次健康时间最新的
            best = (at, a["name"], ck)
    if best:
        print("[fallback] 本实例账号不可用（%s）→ 改用组内账号 %s"
              % ((ACCOUNT.get().get("state") if ACCOUNT else "?"), best[1]), flush=True)
        return best[2]
    return ""


def _router_targets():
    """可参与轮询的：本组 cookie 账号 + 自己 + 各 peer（严格按 _router_ok 判定）。''')

# ---------- 2) chat 端点兜底 ----------
sub("chat 端点兜底",
    '''    cookie_str = cred[1] if isinstance(cred, tuple) else None
    if not cookie_str and (YB_DATA_PLANE == "cookie" or YB_MINT_BACKEND != "cdp") and YB_COOKIE_FILE:
        try:
            cookie_str = open(YB_COOKIE_FILE).read().strip() or None
        except Exception:
            pass
    # cdp+auto：无显式 cookie → 页内数据面（需容器页面已登录）；YB_DATA_PLANE=cookie 强制出站+文件凭证
    body = await req.json()
    model_in = body.get("model", "hunyuan")''',
    '''    cookie_str = cred[1] if isinstance(cred, tuple) else None
    if not cookie_str and (YB_DATA_PLANE == "cookie" or YB_MINT_BACKEND != "cdp") and YB_COOKIE_FILE:
        try:
            cookie_str = open(YB_COOKIE_FILE).read().strip() or None
        except Exception:
            pass
    # 兜底：本实例浏览器号已封/掉登录时，改用组内健康的 cookie 账号（否则 /v1 全 502）
    if not cookie_str:
        cookie_str = _fallback_cookie()
    # cdp+auto：无显式 cookie → 页内数据面（需容器页面已登录）；YB_DATA_PLANE=cookie 强制出站+文件凭证
    body = await req.json()
    model_in = body.get("model", "hunyuan")''')

# ---------- 3) images 端点兜底 ----------
sub("images 端点兜底",
    '''    if not cookie_str and (YB_DATA_PLANE == "cookie" or YB_MINT_BACKEND != "cdp") and YB_COOKIE_FILE:
        try:
            cookie_str = open(YB_COOKIE_FILE).read().strip() or None
        except Exception:
            pass
    # cdp+auto：无显式 cookie → 页内数据面（需容器页面已登录）；YB_DATA_PLANE=cookie 强制出站+文件凭证
    body = await req.json()
    prompt = (body.get("prompt") or "").strip()''',
    '''    if not cookie_str and (YB_DATA_PLANE == "cookie" or YB_MINT_BACKEND != "cdp") and YB_COOKIE_FILE:
        try:
            cookie_str = open(YB_COOKIE_FILE).read().strip() or None
        except Exception:
            pass
    # 兜底：本实例浏览器号已封/掉登录时，改用组内健康的 cookie 账号（否则 /v1 全 502）
    if not cookie_str:
        cookie_str = _fallback_cookie()
    # cdp+auto：无显式 cookie → 页内数据面（需容器页面已登录）；YB_DATA_PLANE=cookie 强制出站+文件凭证
    body = await req.json()
    prompt = (body.get("prompt") or "").strip()''')

# ---------- 4) /pool/v1 透传 X-YB-Upscale（客户端能看见放大结果）----------
sub("pool/v1 透传 upscale 头",
    '''        return StreamingResponse(gen(), status_code=up.status_code,
                                 media_type=up.headers.get("content-type", "application/json"),
                                 headers={"X-YB-Routed-To": str(t.get("name"))})''',
    '''        _oh = {"X-YB-Routed-To": str(t.get("name"))}
        _up_hdr = up.headers.get("X-YB-Upscale") or up.headers.get("x-yb-upscale")
        if _up_hdr:
            _oh["X-YB-Upscale"] = _up_hdr      # 让客户端经轮询入口也能看到放大结果
        return StreamingResponse(gen(), status_code=up.status_code,
                                 media_type=up.headers.get("content-type", "application/json"),
                                 headers=_oh)''')

import ast
ast.parse(src)
io.open(P, "w", encoding="utf-8").write(src)
print("AST OK, written")
