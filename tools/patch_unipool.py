"""补丁：删除 `/pool/v1`，`/v1` 成为唯一入口；轮询只认"有效"账号。

用户要求（原话）：
  「pool/v1 删除 用 /v1 替代 默认就是用 /v1 轮询 另外只轮询有效账号」

改动：
  1. 删掉 `@app.api_route("/pool/v1/{rest:path}")` 装饰器 —— 函数体保留，
     因为 `/v1/*` 的分流要调它。它是**内部实现**，不再是公开地址。
  2. `_router_ok` 从"routable + 保活成功"升级为"**有效账号**"：
     - `state == enabled`（routable）
     - `last_health.ok is True`
     - `last_health.frozen` 为假（页内显示冻结的立刻排除）
     - **健康新鲜度**：`last_health.at` 在 `YB_ROUTER_HEALTH_TTL`（默认 1800s）内
       —— 几小时前的"曾成功"不能代表现在可用；保活停摆后应自动退出轮询
  3. 文案/README 里所有 `/pool/v1` 改为 `/v1`
"""
import ast
import io

P = "yuanbao_openai_proxy.py"
src = io.open(P, encoding="utf-8").read()


def sub(label, a, b, n=1):
    global src
    c = src.count(a)
    assert c == n, "%s count=%d (want %d)" % (label, c, n)
    src = src.replace(a, b)
    print("  ok", label)


# ---------- 1) 删除路由（保留函数体）----------
sub("删 /pool/v1 路由",
    '''@app.api_route("/pool/v1/{rest:path}", methods=["GET", "POST"])
async def pool_router(req: Request, rest: str):
    """**轮询入口**：/pool/v1/<rest> 轮询分发到各可用账号，失败自动切下一个。

    与直连入口的分工：
      /v1/*       → 只用本实例自己的账号（直连，行为不变）
      /pool/v1/*  → 在整个号池里轮询（本接口；调用方只认一个 key）
    仅 state=enabled 的账号参与；遇到 5xx/连接错误自动 failover 到下一个。
    """''',
    '''async def pool_router(req: Request, rest: str):
    """**号池轮询的内部实现** —— 由 `/v1/*` 分流进来调用，**不再单独暴露地址**。

    `/v1/chat/completions` / `/v1/images/generations` / `/v1/images/edits` 在入口处
    用 `_want_pool_route()` 判定（Bearer = 实例 key 就轮询），然后调本函数。

    只有 `_router_ok()` 认可的**有效账号**参与（enabled + 保活成功 + 未冻结 + 健康新鲜）；
    遇到 5xx / 连接错误自动 failover 到下一个，最多试 3 个。
    """''')

# ---------- 2) 配置项：健康新鲜度 ----------
sub("新增 YB_ROUTER_HEALTH_TTL",
    'YB_ROUTER = os.environ.get("YB_ROUTER", "1") == "1"                            # 是否开放 /pool/v1 轮询入口',
    '''YB_ROUTER = os.environ.get("YB_ROUTER", "1") == "1"                            # /v1 是否走号池轮询（0=只直连本实例）
# 轮询的"健康新鲜度"窗口（秒）：last_health 超过这么久就认为不可信、不进轮询。
# 保活周期默认 900s ⇒ 1800s 给两次机会；设 0 关闭该检查。
YB_ROUTER_HEALTH_TTL = int(os.environ.get("YB_ROUTER_HEALTH_TTL", "1800"))''')

# ---------- 3) _router_ok 升级为"有效账号" ----------
sub("_router_ok 升级",
    '''def _router_ok(st: dict) -> bool:
    """路由门禁：**必须 enabled 且最近一次保活成功**。

    只判 routable 是不够的 —— 刚 add-account 出来、还没扫码登录的实例也是 enabled，
    但把它算进轮询只会把用户请求打到一个用不了的号上。所以要求"健康被证实过"。
    新号登录后 /admin/state 会立刻触发一次保活（见下），约 20s 内即可参与轮询。
    """
    if not st or not st.get("routable"):
        return False
    h = st.get("last_health") or {}
    return h.get("ok") is True''',
    '''def _router_ok(st: dict) -> bool:
    """路由门禁：**只放行"有效"账号**，四个条件缺一不可。

    1. `state == enabled`（routable）—— disabled/quarantined/ejected 一律不参与
    2. `last_health.ok is True` —— 刚 add 出来、从没成功过的号不能进（会白打）
    3. `last_health.frozen` 为假 —— 页内显示"已冻结"的号立刻排除，不等状态机
    4. **健康新鲜度** —— `last_health.at` 距今不超过 `YB_ROUTER_HEALTH_TTL`
       （几小时前的"曾成功"不代表现在还能用；保活停摆后应自动退出轮询）

    ⚠️ 单看 routable/enabled 是不够的：cookie 残留但已 23000 过期的号
       在服务端看来仍是"登录中"，只有真发一次请求才知道 —— 这正是健康新鲜度的意义。
    """
    if not st or not st.get("routable"):
        return False
    h = st.get("last_health") or {}
    if h.get("ok") is not True:
        return False
    if h.get("frozen"):
        return False
    if YB_ROUTER_HEALTH_TTL > 0:
        try:
            at = int(h.get("at") or 0)
        except Exception:
            at = 0
        if at > 0 and (time.time() - at) > YB_ROUTER_HEALTH_TTL:
            return False
    return True''')

# ---------- 4) 文案：router_entry ----------
sub("router_entry 文案",
    '''            "router_enabled": YB_ROUTER, "router_entry": "/pool/v1",''',
    '''            "router_enabled": YB_ROUTER, "router_entry": "/v1（默认走号池轮询）",''')

# ---------- 5) 文案：递归注释 ----------
sub("递归注释文案",
    '''      1. `X-YB-Direct: 1` —— 内部转发标记，强制直连（否则 /v1 ⇄ /pool/v1 无限递归）''',
    '''      1. `X-YB-Direct: 1` —— 内部转发标记，强制直连（否则轮询转发回 /v1 会无限递归）''')

# ---------- 6) 文案：group hint ----------
sub("group hint 文案",
    '''            "hint": "组内账号**只在本实例可见** —— 要让 /pool/v1 用到，必须加在公网入口实例（acc01）上"}''',
    '''            "hint": "组内账号**只在本实例可见** —— 要让 /v1 轮询用到，必须加在公网入口实例（acc01）上"}''')

ast.parse(src)
io.open(P, "w", encoding="utf-8").write(src)
print("AST OK, written")
