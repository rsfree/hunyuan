#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""patch_bulk.py — cookie 批量导入流水线（幂等、带断言）。

新增：
  POST /admin/group/bulk   一次粘多个 cookie → 逐个建会话验真 → 入库（同 hy_user 自动替换）
  GET  /bulk               极简页面（textarea + 结果表），导航加「批量导入」
并把 _cookie_ban_probe 升级为能区分 banned / expired，且在探测期间**抑制**
_note_ban/_note_expired 对**实例账号**的副作用（否则验一个坏 cookie 会把实例号踢掉）。
"""
import io
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
P = os.path.join(ROOT, "yuanbao_openai_proxy.py")
src = io.open(P, encoding="utf-8").read()
orig = src


def sub(label, old, new, count=1):
    global src
    n = src.count(old)
    assert n == count, "【%s】匹配数 %d != 期望 %d" % (label, n, count)
    src = src.replace(old, new)
    print("  ok  %-40s (%d)" % (label, n))


# ---------------------------------------------------------------- 1. 探测期抑制副作用
sub("_note_ban 支持抑制", '''    blob = " ".join(str(t) for t in texts if t)
    if not blob:
        return False
    # 实测两种文案：''',
'''    if getattr(_TLS, "suppress", False):
        return False          # 正在验某个 cookie（批量导入）—— 不要动实例账号的状态
    blob = " ".join(str(t) for t in texts if t)
    if not blob:
        return False
    # 实测两种文案：''')

sub("_note_expired 支持抑制", '''    blob = " ".join(str(t) for t in texts if t)
    if not blob:
        return False
    if "23000" not in blob and "登录已过期" not in blob:''',
'''    if getattr(_TLS, "suppress", False):
        return False
    blob = " ".join(str(t) for t in texts if t)
    if not blob:
        return False
    if "23000" not in blob and "登录已过期" not in blob:''')

sub("声明 _TLS", '''def _note_ban(*texts) -> bool:''',
'''_TLS = threading.local()   # 线程局部开关：批量验 cookie 时抑制对实例账号的状态副作用


def _note_ban(*texts) -> bool:''')

# ---------------------------------------------------------------- 2. 探针区分 banned/expired + 抑制
sub("探针升级", '''    try:
        sig = get_sig(None)
    except Exception:
        sig = None            # 铸签失败不阻塞判定（退化为"未知"，不当封号处理）
    try:
        r = _yb_post_json(cookie, "/api/user/agent/conversation/create",
                          {"agentId": agent_id}, sig, agent_id, timeout=40)
    except Exception as e:
        return {"ok": False, "banned": False, "status": 0, "detail": "请求失败: %s" % str(e)[:100]}
    txt = r.get("text") or ""
    if "20004" in txt or "用户被封号" in txt:
        return {"ok": False, "banned": True, "status": r.get("status"), "detail": "20004 用户被封号"}''',
'''    try:
        sig = get_sig(None)
    except Exception:
        sig = None            # 铸签失败不阻塞判定（退化为"未知"，不当封号处理）
    _TLS.suppress = True       # ← 验的是"这个 cookie"，不是实例账号，别触发 _note_*
    try:
        r = _yb_post_json(cookie, "/api/user/agent/conversation/create",
                          {"agentId": agent_id}, sig, agent_id, timeout=40)
    except Exception as e:
        return {"ok": False, "banned": False, "kind": "error", "status": 0,
                "detail": "请求失败: %s" % str(e)[:100]}
    finally:
        _TLS.suppress = False
    txt = r.get("text") or ""
    if "20004" in txt or "用户被封号" in txt:
        return {"ok": False, "banned": True, "kind": "banned",
                "status": r.get("status"), "detail": "20004 用户被封号"}
    if "23000" in txt or "登录已过期" in txt:
        return {"ok": False, "banned": False, "kind": "expired",
                "status": r.get("status"), "detail": "23000 登录已过期"}''')

sub("探针其余分支补 kind", '''    if r.get("status") != 200:
        return {"ok": False, "banned": False, "status": r.get("status"),
                "detail": "HTTP %s %s" % (r.get("status"), txt[:100])}''',
'''    if r.get("status") != 200:
        return {"ok": False, "banned": False, "kind": "unusable", "status": r.get("status"),
                "detail": "HTTP %s %s" % (r.get("status"), txt[:100])}''')

sub("探针成功分支补 kind", '''        return {"ok": True, "banned": False, "status": 200, "detail": "ok"}
    return {"ok": False, "banned": False, "status": 200, "detail": "建会话无 id: " + txt[:100]}''',
'''        return {"ok": True, "banned": False, "kind": "ok", "status": 200, "detail": "ok"}
    return {"ok": False, "banned": False, "kind": "unusable", "status": 200,
            "detail": "建会话无 id: " + txt[:100]}''')

# ---------------------------------------------------------------- 3. 批量导入端点 + 页面
sub("新增批量导入", '''@app.post("/admin/group")''',
'''def _clean_cookie(s: str) -> str:
    """清洗粘贴进来的 cookie：兼容 curl 的 -b/-H 前缀、Cookie: 前缀、包裹引号。"""
    s = (s or "").strip()
    if s[:2] in ("-b", "-H"):
        s = s[2:].strip()
    if s.lower().startswith("cookie:"):
        s = s.split(":", 1)[1].strip()
    return s.strip().strip("'\\"").strip()


def _bulk_name(prefix: str, hy_user: str) -> str:
    """按 `前缀 + hy_user 前 6 位` 生成不冲突的账号名。"""
    base = re.sub(r"[^A-Za-z0-9_\\-]", "", (prefix or "yb"))[:8] or "yb"
    tail = re.sub(r"[^A-Za-z0-9]", "", hy_user or "")[:6] or "anon"
    nm = (base + tail)[:24]
    existing = {x["name"] for x in COOKIE_ACCOUNTS.list()}
    if nm not in existing:
        return nm
    i = 2
    while (nm[:21] + "-%d" % i) in existing:
        i += 1
    return nm[:21] + "-%d" % i


@app.post("/admin/group/bulk")
async def admin_group_bulk(req: Request):
    """**批量导入 cookie**：一次粘多个号，逐个建会话验真后入库。

    body:
      cookies: ["<cookie>", ...]   逐个 cookie 串
      text:    "多行文本"           每行一个 cookie（也接受 curl 片段，自动剥 -b/'/Cookie:）
      prefix:  "yb"                 账号名前缀（最终名 = 前缀 + hy_user 前 6 位）
      note:    "批量导入"
      dry_run: false                只验不写

    规则：
      · 缺 hy_user / 封号(20004) / 过期(23000) / 其它不可用 → **不入库**，逐条报告原因
      · 同一个 hy_user 已存在 → **替换**（换 cookie 场景），不会产生重复号
      · 每个号之间间隔 1.2s，避免瞬时冲击上游
    """
    cred = _check_admin_panel_auth(req)
    if isinstance(cred, JSONResponse):
        return cred
    if COOKIE_ACCOUNTS is None:
        return JSONResponse({"error": "账号库未启用（YB_ACCOUNTS_DIR 不可写）"}, status_code=503)
    try:
        body = await req.json()
    except Exception:
        body = {}

    raw = list(body.get("cookies") or [])
    txt = body.get("text") or ""
    if txt:
        raw.extend(txt.splitlines())
    prefix = (body.get("prefix") or "yb").strip()
    note = (body.get("note") or "批量导入").strip()
    dry = bool(body.get("dry_run"))

    items, seen = [], set()
    for r in raw:
        ck = _clean_cookie(r)
        if not ck or "=" not in ck:
            continue
        if ck in seen:
            continue
        seen.add(ck)
        items.append(ck)
    if not items:
        return JSONResponse({"error": "没有解析到任何 cookie（每行一个，需含 hy_user）"}, status_code=400)

    results, added = [], 0
    for ck in items:
        u = _acc.CookieAccounts.hy_user_of(ck)
        rec = {"hy_user": (u[:8] + "…") if u else "", "cookie_len": len(ck)}
        if not u:
            rec.update(ok=False, status="bad_format", detail="缺 hy_user（不是已登录 cookie）")
            results.append(rec)
            continue
        if "hy_token=" not in ck:
            rec["warn"] = "缺 hy_token（部分接口可能 401）"
        name = COOKIE_ACCOUNTS.find_by_user(u) or _bulk_name(prefix, u)
        rec["name"] = name

        if dry:
            rec.update(ok=True, status="would_replace" if COOKIE_ACCOUNTS.get(name) else "would_add")
            results.append(rec)
            continue

        pb = await asyncio.to_thread(_cookie_ban_probe, ck)
        kind = pb.get("kind") or ("ok" if pb.get("ok") else "unusable")
        rec["probe"] = (pb.get("detail") or "")[:60]
        if kind != "ok":
            rec.update(ok=False, status=kind)
            results.append(rec)
            continue
        r = COOKIE_ACCOUNTS.add(name, ck, note)
        if not r.get("ok"):
            rec.update(ok=False, status="store_error", detail=r.get("error"))
            results.append(rec)
            continue
        COOKIE_ACCOUNTS.record_health(name, True, 200, "alive")
        if YB_AUTO_WATERMARK:
            try:
                await asyncio.to_thread(_wm_ensure_cookie, ck, True)
            except Exception:
                pass
        rec.update(ok=True, status="replaced" if r.get("replaced") else "added")
        added += 1
        results.append(rec)
        time.sleep(1.2)

    print("[bulk] 导入 %d 条，成功 %d（dry=%s）" % (len(items), added, dry), flush=True)
    return {"result": {"total": len(items), "added": added, "results": results},
            "hint": "组内账号**只在本实例可见** —— 要让 /pool/v1 用到，必须加在公网入口实例（acc01）上"}


@app.post("/admin/group")''')

# ---------------------------------------------------------------- 4. 极简页面 + 导航
sub("导航加批量导入", '''  <a data-h="/qr" href="/qr">扫码 / 登录</a>''',
'''  <a data-h="/bulk" href="/bulk">批量导入 cookie</a>
  <a data-h="/qr" href="/qr">扫码 / 登录</a>''')

sub("新增 /bulk 页面", '''@app.get("/admin/group")''',
'''BULK_HTML = """<h2 style="margin:0 0 6px">批量导入 cookie</h2>
<p style="color:#666;font-size:13px;margin:0 0 14px">
每行一个 cookie（含 <code>hy_user</code>）。可以直接粘 curl 片段，会自动剥掉 <code>-b</code>／引号／<code>Cookie:</code> 前缀。<br>
导入时会**逐个建会话验真**：封号(20004)／过期(23000)／缺 hy_user 的一律不入库；同一个号重复导入会**替换**而不是新增。
</p>
<div style="margin-bottom:10px">
  <input id="pfx" placeholder="名前缀 yb" value="yb" style="width:90px;padding:6px 8px;border:1px solid #ddd;border-radius:6px">
  <label style="margin-left:10px;font-size:13px"><input type="checkbox" id="dry"> 只验不写（dry-run）</label>
</div>
<textarea id="ta" rows="10" placeholder="hy_user=...; hy_token=...; ...
hy_user=...; hy_token=...; ..."
  style="width:100%;box-sizing:border-box;padding:10px;border:1px solid #ddd;border-radius:8px;font:12px/1.5 ui-monospace,Menlo,monospace"></textarea>
<div style="margin:12px 0"><button id="go" style="padding:8px 18px;border:0;border-radius:8px;background:#534AB7;color:#fff;font-size:14px;cursor:pointer">开始导入</button>
<span id="st" style="margin-left:12px;font-size:13px;color:#666"></span></div>
<table id="tb" style="width:100%;border-collapse:collapse;font-size:12px"></table>
<script>
const KEY = (function(){ var q=(new URLSearchParams(location.search).get('k')||'').trim();
  try { return q || localStorage.getItem('yb_key') || ''; } catch(e){ return q; } })();
document.getElementById('go').onclick = async function () {
  const st = document.getElementById('st'), tb = document.getElementById('tb');
  const text = document.getElementById('ta').value.trim();
  if (!text) { st.textContent = '先粘 cookie'; return; }
  st.textContent = '导入中…（每个号要建一次会话验真，稍等）'; tb.innerHTML = '';
  this.disabled = true;
  try {
    const r = await fetch('/admin/group/bulk', { method:'POST',
      headers:{ 'Authorization':'Bearer '+KEY, 'content-type':'application/json' },
      body: JSON.stringify({ text: text, prefix: document.getElementById('pfx').value || 'yb',
                             dry_run: document.getElementById('dry').checked }) });
    const d = await r.json();
    const res = d.result || {};
    if (!res.results) { st.textContent = d.error || JSON.stringify(d).slice(0,200); return; }
    let html = '<tr style="background:#f6f5f2"><th align=left>账号</th><th align=left>hy_user</th><th align=left>结果</th><th align=left>说明</th></tr>';
    const cls = { added:'#1a7f37', replaced:'#1a7f37', would_add:'#0a58ca', would_replace:'#0a58ca' };
    (res.results||[]).forEach(function(x){
      const c = cls[x.status] || '#b42318';
      html += '<tr style="border-top:1px solid #eee"><td>'+(x.name||'-')+'</td><td>'+(x.hy_user||'-')+
              '</td><td style="color:'+c+';font-weight:500">'+(x.status||'?')+'</td><td style="color:#888">'+
              ((x.probe||x.detail||x.warn||'')+'').replace(/</g,'&lt;')+'</td></tr>';
    });
    tb.innerHTML = html;
    st.textContent = '共 ' + res.total + ' 条，成功入库 ' + res.added + ' 条' + (d.hint ? ' —— ' + d.hint : '');
  } catch (e) { st.textContent = String(e); }
  finally { this.disabled = false; }
};
</script>"""


@app.get("/bulk")
async def bulk_page():
    """批量导入 cookie 的极简页面（壳免鉴权；数据端点自身鉴权）。"""
    return _page(BULK_HTML)


@app.get("/admin/group")''')

assert src != orig
io.open(P, "w", encoding="utf-8").write(src)
print("写入:", P)
