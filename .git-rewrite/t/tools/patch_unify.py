"""补丁：统一入口 —— `POST /v1/*` 直接具备号池轮询能力。

用户诉求（原话）：「为啥两个哇，应该 v1 就行」
现状问题：
  /v1/*      只用本实例自己那一个浏览器账号 → 号一死，公网 /v1 全 502（已实测）
  /pool/v1/* 跨账号轮询            → 要记两套地址，容易配错

统一后的语义（按 Bearer 值区分，无需用户理解"实例/号池"概念）：
  Bearer = 实例 key        → **轮询全池**（默认行为，就是一个地址）
  Bearer = cookie 串       → 用这个 cookie 直连（诊断/临时用自己的号，不轮询）
  X-YB-Direct: 1（内部）   → 强制直连，供 pool_router 内部转发用（防无限递归）

/pool/v1/* 原样保留（new-api 渠道 40 已指向它，不动即兼容）。
"""
import io
import ast

P = "yuanbao_openai_proxy.py"
src = io.open(P, encoding="utf-8").read()


def sub(label, a, b, n=1):
    global src
    c = src.count(a)
    assert c == n, "%s count=%d (want %d)" % (label, c, n)
    src = src.replace(a, b)
    print("  ok", label)


# ---------- 1) pool_router 转发时打上内部标记（防 /v1 ⇄ /pool/v1 递归）----------
sub("pool_router 打内部标记",
    '''        hdr = {"content-type": req.headers.get("content-type", "application/json")}
        tkey = t.get("key") or ""''',
    '''        hdr = {"content-type": req.headers.get("content-type", "application/json"),
               # 🔴 内部标记：目标实例的 /v1 看到它就必须**直连**，否则会再次轮询 → 无限递归
               "X-YB-Direct": "1"}
        tkey = t.get("key") or ""''')

# ---------- 2) 统一入口的判定函数（放在 _router_pick 之后）----------
sub("新增 _want_pool_route",
    '''@app.get("/admin/account")
async def admin_account(req: Request):''',
    '''def _want_pool_route(req: Request) -> bool:
    """`POST /v1/*` 是否应该走号池轮询（而不是本实例直连）。

    判定只看两件事：
      1. `X-YB-Direct: 1` —— 内部转发标记，强制直连（否则 /v1 ⇄ /pool/v1 无限递归）
      2. Bearer 是不是 **cookie 串** —— 是则用调用方自己的号直连，不抢池里的号

    剩下的情况（实例 key / 无门禁）一律轮询：用户配一个地址就该能用全部号。
    """
    if req.headers.get("x-yb-direct"):
        return False
    if not YB_ROUTER:
        return False
    try:
        cred = _check_auth(req)
    except Exception:
        return False
    if isinstance(cred, JSONResponse):
        return False            # 鉴权失败交给原逻辑返回 401
    if isinstance(cred, tuple):
        return False            # tuple = cookie 透传 ⇒ 用调用方自己的号直连
    # 🔴 池内真的要有可用目标才轮询：否则回落直连，让原有页面模式跑出真实错误。
    #    这条保证「单实例裸部署 / 刚启动还没保活」时 /v1 仍然可用（否则会突然全 503）。
    try:
        if not _router_targets():
            return False
    except Exception:
        return False
    return True


@app.get("/admin/account")
async def admin_account(req: Request):''')

# ---------- 3) chat 端点分流 ----------
sub("chat 端点分流到轮询",
    '''async def chat_completions(req: Request):
    cred = _check_auth(req)''',
    '''async def chat_completions(req: Request):
    # 统一入口：实例 key ⇒ 走号池轮询（cookie 串 / 内部转发 ⇒ 直连本实例）
    if _want_pool_route(req):
        return await pool_router(req, "chat/completions")
    cred = _check_auth(req)''')

# ---------- 4) images 端点分流 ----------
sub("images 端点分流到轮询",
    '''async def images_generations(req: Request):
    cred = _check_auth(req)''',
    '''async def images_generations(req: Request):
    # 统一入口：实例 key ⇒ 走号池轮询（cookie 串 / 内部转发 ⇒ 直连本实例）
    if _want_pool_route(req):
        return await pool_router(req, "images/generations")
    cred = _check_auth(req)''')

ast.parse(src)
io.open(P, "w", encoding="utf-8").write(src)
print("AST OK, written")
