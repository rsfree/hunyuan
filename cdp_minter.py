#!/usr/bin/env python3
"""cdp_minter.py — Linux 无 bsk 环境的铸签后端

自管一个 headless Chromium（或接入已运行的 CDP 端点），加载元宝页面，
通过 CDP Runtime.evaluate 页内铸造签名三件套。页面常驻预热（SDK 初始化一次）。

配置（env）：
  CDP_HTTP        CDP 端点，默认 http://127.0.0.1:9222（chromium 由本模块拉起或外部提供）
  CHROME_BIN      chromium 可执行路径；设置则由本模块拉起，不设则假定外部已在跑
  CDP_YB_URL      元宝页面地址，默认 https://yuanbao.tencent.com/chat/naQivTmsDa
"""
import json
import os
import subprocess
import threading
import time

import urllib.request

import websocket

CDP_HTTP = os.environ.get("CDP_HTTP", "http://127.0.0.1:9222")
CHROME_BIN = os.environ.get("CHROME_BIN", "")
if not CHROME_BIN:
    for _c in ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
               "/usr/bin/chromium", "/usr/bin/chromium-browser",
               "/ms-playwright/chromium-1148/chrome-linux/chrome"):
        if os.path.exists(_c):
            CHROME_BIN = _c
            break
CDP_YB_URL = os.environ.get("CDP_YB_URL", "https://yuanbao.tencent.com/chat/naQivTmsDa")
CHROME_PROFILE_DIR = os.environ.get("CHROME_PROFILE_DIR", "/data/chrome-profile")  # 与 entrypoint 保持一致（登录态所在）
# 设备指纹种子：注入已知账号绑定的设备身份（避免 headless 新指纹触发风控）
# 格式 "k1=v1;k2=v2"（localStorage 键值对）
DEVICE_SEED = os.environ.get("YB_DEVICE_SEED", "_qimei_h38=e9632faf082420cd40bb971703000001419610")
STEALTH_JS = """
(() => {
  // 平台/语言与 UA 保持一致（Mac Chrome zh-CN）
  try { Object.defineProperty(navigator, 'platform', {get: () => 'MacIntel'}); } catch (e) {}
  try { Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh', 'en']}); } catch (e) {}
  try { Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => 8}); } catch (e) {}
  try { Object.defineProperty(navigator, 'deviceMemory', {get: () => 8}); } catch (e) {}
  try { Object.defineProperty(navigator, 'maxTouchPoints', {get: () => 0}); } catch (e) {}
  // WebGL 真实渲染器伪装（headless 默认 SwiftShader，风控强特征）
  const patch = (proto) => {
    if (!proto) return;
    const orig = proto.getParameter;
    proto.getParameter = function (p) {
      if (p === 37445) return 'Apple Inc.';      // UNMASKED_VENDOR_WEBGL
      if (p === 37446) return 'Apple M2';        // UNMASKED_RENDERER_WEBGL
      return orig.call(this, p);
    };
  };
  try { patch(window.WebGLRenderingContext && window.WebGLRenderingContext.prototype); } catch (e) {}
  try { patch(window.WebGL2RenderingContext && window.WebGL2RenderingContext.prototype); } catch (e) {}
  // chrome 运行时对象补全（headless 偶缺）
  try { if (!window.chrome) window.chrome = {}; if (!window.chrome.runtime) window.chrome.runtime = {}; } catch (e) {}
  return 'stealth-ok';
})()
"""

SEED_JS = """
(() => {
  const pairs = "__SEED__".split(";").filter(Boolean);
  for (const p of pairs) {
    const i = p.indexOf("=");
    if (i > 0) localStorage.setItem(p.slice(0, i), p.slice(i + 1));
  }
  return localStorage.getItem("_qimei_h38") || "";
})()
""".replace("__SEED__", DEVICE_SEED)
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36")

MINT_JS = """
(() => {
  const arrs = Object.keys(window).filter(k => k.startsWith("webpackChunk"));
  if (!arrs.length) return {err: "no webpackChunk"};
  const arr = window[arrs[0]];
  let req; arr.push([["mint_" + Date.now()], {}, (r) => { req = r; }]);
  const modSig = req(77004), modHdr = req(28850);
  const s = modHdr.TE(modSig.PU);
  return {uskey: s["X-Uskey"] || "", md5: String(s["X-Bus-Params-Md5"]), ts: String(s["X-Timestamp"])};
})()
"""

FP_JS = """
(() => {
  let ver = null, tag = null, h38 = null;
  try {
    const arrs = Object.keys(window).filter(k => k.startsWith("webpackChunk"));
    const arr = window[arrs[0]];
    let req; arr.push([["fp_" + Date.now()], {}, (r) => { req = r; }]);
    const sdk = req(77004).I5(req(77004).PU);
    const q = sdk.getLocalQimei36();
    h38 = q ? q.h38 : null;
  } catch (e) {}
  try {
    for (const e of performance.getEntriesByType('resource')) {
      const m = (e.name || '').match(/version=([\d.]+)__([0-9a-f]+)-/);
      if (m) { ver = m[1]; tag = m[2]; break; }
    }
  } catch (e) {}
  const ua = navigator.userAgent;
  let osv = 'Mac OS(10.15.7)-Blink';
  if (ua.includes('Mac OS X')) {
    const m = ua.match(/Mac OS X ([\d_]+)/);
    if (m) osv = 'Mac OS(' + m[1].replace(/_/g, '.') + ')-Blink';
  }
  return {ver, tag, osv, ua, h38};
})()
"""

_UNUSED_FP_JS = """
(() => {
  let ver = null, tag = null;
  try {
    for (const e of performance.getEntriesByType('resource')) {
      const m = (e.name || '').match(/version=([\\d.]+)__([0-9a-f]+)-/);
      if (m) { ver = m[1]; tag = m[2]; break; }
    }
  } catch (e) {}
  const ua = navigator.userAgent;
  let osv = 'Mac OS(10.15.7)-Blink';
  if (ua.includes('Mac OS X')) {
    const m = ua.match(/Mac OS X ([\\d_]+)/);
    if (m) osv = 'Mac OS(' + m[1].replace(/_/g, '.') + ')-Blink';
  }
  return {ver, tag, osv, ua};
})()
"""


# 登录页截图裁剪：找出"含微信二维码的最内层容器"。
# 实测 DOM：二维码在 iframe(open.weixin.qq.com/connect/qrconnect, 200x400) 里，
# 其最内层容器是 .hyc-login__content(460x440)；同层的 .hyc-login__left(240x440) 是推广面板，
# 所以不能只按"面积最小"选 —— 必须加"矩形要包含二维码中心点"这一约束。
CLIP_JS = """
(() => {
  const ifrs = Array.prototype.slice.call(document.querySelectorAll('iframe')).filter(function (f) {
    const s = f.getAttribute('src') || '';
    return s.indexOf('qrconnect') >= 0 || s.indexOf('open.weixin') >= 0;
  });
  let qr = null;
  for (const f of ifrs) { const r = f.getBoundingClientRect(); if (r.width > 50 && r.height > 50) { qr = r; break; } }
  const sels = ['.hyc-login-v2', '.hyc-phone-login', '[class*=hyc-login]', '.t-dialog', '[role=dialog]',
                '[class*=login-modal]', '[class*=login-dialog]', '[class*=login-box]', '[class*=login-wrap]'];
  const cands = [];
  for (const s of sels) document.querySelectorAll(s).forEach(function (e) { cands.push(e); });
  if (ifrs.length) { let q = ifrs[0].parentElement; while (q && q !== document.body) { cands.push(q); q = q.parentElement; } }
  const cx = qr ? (qr.x + qr.width / 2) : null;
  const cy = qr ? (qr.y + qr.height / 2) : null;
  const minW = 380;
  let best = null;
  for (const el of cands) {
    const r = el.getBoundingClientRect();
    if (r.width < minW || r.height < 240) continue;
    if (r.width > window.innerWidth * 0.96 || r.height > window.innerHeight * 0.96) continue;
    if (cx !== null && (cx < r.x || cx > r.x + r.width || cy < r.y || cy > r.y + r.height)) continue;
    const a = r.width * r.height;
    if (!best || a < best.a) best = { a: a, r: r };
  }
  if (!best) { if (!qr) return null; best = { a: 0, r: qr }; }
  const p = 20, r = best.r;
  const x = Math.max(0, r.x - p), y = Math.max(0, r.y - p);
  return { x: x, y: y,
           width: Math.min(r.width + p * 2, window.innerWidth - x),
           height: Math.min(r.height + p * 2, window.innerHeight - y) };
})()
"""


class CDPMinter:
    def __init__(self):
        self._lock = threading.Lock()
        self._browser_ws = None
        self._page_ws = None
        self._mid = 100
        self._warm = False
        self._chrome_proc = None

    # ---- 基础设施 ----
    def _http_json(self, path, method="GET", timeout=10):
        req = urllib.request.Request(CDP_HTTP + path, method=method)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())

    def _ensure_chrome(self):
        if self._chrome_proc and self._chrome_proc.poll() is None:
            return
        if not CHROME_BIN:
            return  # 假定外部已提供 CDP 端点
        proxy = getattr(self, "_proxy_override", None) or os.environ.get("YB_PROXY_URL") or ""
        args = [CHROME_BIN, "--headless=new", "--no-sandbox", "--disable-dev-shm-usage",
                "--disable-gpu", "--ignore-certificate-errors",
                "--disable-blink-features=AutomationControlled", "--lang=zh-CN",
                "--remote-debugging-port=" + CDP_HTTP.split(":")[-1],
                "--user-data-dir=" + CHROME_PROFILE_DIR]
        if proxy:
            args.append("--proxy-server=" + proxy)
        args.append("about:blank")
        self._chrome_proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def restart_with_proxy(self, proxy_url: str) -> bool:
        """换浏览器出口 IP：杀掉现 chromium（含 entrypoint 拉起的），以新代理重启并重新预热。"""
        with self._lock:
            self._proxy_override = proxy_url
            for ws_attr in ("_page_ws", "_browser_ws"):
                ws = getattr(self, ws_attr)
                if ws:
                    try:
                        ws.close()
                    except Exception:
                        pass
                    setattr(self, ws_attr, None)
            if self._chrome_proc:
                try:
                    self._chrome_proc.terminate()
                    self._chrome_proc.wait(timeout=10)
                except Exception:
                    pass
                self._chrome_proc = None
            # entrypoint 拉起的 chromium 没有本地句柄：按路径匹配清理
            try:
                subprocess.run(["pkill", "-f", "chrome-linux/chrome"], timeout=10)
            except Exception:
                pass
            time.sleep(2)
            # 清陈旧 profile 锁（hostname 变化后 Chromium 会拒启）
            for lk in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
                try:
                    os.remove(os.path.join(CHROME_PROFILE_DIR, lk))
                except Exception:
                    pass
            try:
                self._ensure_page()
                return bool(self._warm)
            except Exception:
                return False

    def _ws_send(self, ws, method, params=None, mid=1):
        ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        deadline = time.time() + 60
        while time.time() < deadline:
            # 短轮询：recv 若按 socket 超时（180s）阻塞，下面的 deadline 形同虚设
            ws.settimeout(max(1.0, min(10.0, deadline - time.time())))
            try:
                msg = json.loads(ws.recv())
            except websocket.WebSocketTimeoutException:
                continue
            if msg.get("id") == mid:
                return msg.get("result", {})
        raise TimeoutError(method)

    def _ensure_page(self):
        """返回就绪的页 ws；断线/崩溃自动重建。"""
        try:
            if self._page_ws:
                self._ws_send(self._page_ws, "Runtime.evaluate",
                              {"expression": "1", "returnByValue": True}, mid=1)
                return self._page_ws
        except Exception:
            pass
        # 重建：CDP 端点已可达（如 entrypoint 已拉起）则不再重复拉起 chromium
        ver = None
        try:
            ver = self._http_json("/json/version", timeout=3)
        except Exception:
            ver = None
        if not ver:
            self._ensure_chrome()
        for _ in range(30):
            try:
                ver = self._http_json("/json/version")
                break
            except Exception:
                time.sleep(1)
        if not ver:
            raise RuntimeError("CDP 端点不可用: " + CDP_HTTP)
        if self._browser_ws:
            try:
                self._browser_ws.close()
            except Exception:
                pass
        self._browser_ws = websocket.create_connection(ver["webSocketDebuggerUrl"], timeout=60, suppress_origin=True)
        # 1) 先开 about:blank（此时不加载元宝应用）
        r = self._ws_send(self._browser_ws, "Target.createTarget",
                          {"url": "about:blank"}, mid=901)
        tid = r.get("targetId")  # _ws_send 已返回 result 内层
        if not tid:
            raise RuntimeError("createTarget 失败: " + json.dumps(r)[:200])
        time.sleep(1)
        targets = self._http_json("/json/list")
        # 精确按 targetId 取"刚创建的那个"：
        # 旧写法 next(... url=="about:blank") 会命中 entrypoint 启动时那个常驻 about:blank，
        # 于是刚建的 target 永不回收 —— 多次 /login 后堆积出多个重复元宝标签页
        # （实测 4 worker 各建一页 → /json/list 里 3 个 /chat/naQivTmsDa + 1 个 /chat）。
        page = next((t for t in targets if t.get("id") == tid), None)
        if page is None:
            page = next(t for t in targets if t["type"] == "page" and t["url"] == "about:blank")
        # 顺手清理同类残留页（保留当前这一个），避免 stack 越滚越大
        for t in targets:
            if (t.get("type") == "page" and t.get("id") != page.get("id")
                    and "yuanbao.tencent.com" in (t.get("url") or "")):
                try:
                    self._http_json("/json/close/" + t["id"], timeout=3)
                except Exception:
                    pass
        if self._page_ws:
            try:
                self._page_ws.close()
            except Exception:
                pass
        self._page_ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=180, suppress_origin=True)
        self._page_ws.settimeout(180)
        # 2) 关键顺序：先注册 init 脚本与指纹覆盖，再导航（否则脚本对首个文档不生效）
        self._ws_send(self._page_ws, "Emulation.setUserAgentOverride",
                      {"userAgent": UA, "platform": "MacIntel", "acceptLanguage": "zh-CN,zh,en"}, mid=902)
        self._ws_send(self._page_ws, "Page.addScriptToEvaluateOnNewDocument",
                      {"source": SEED_JS}, mid=904)
        self._ws_send(self._page_ws, "Page.addScriptToEvaluateOnNewDocument",
                      {"source": STEALTH_JS}, mid=905)
        self._ws_send(self._page_ws, "Page.enable", mid=906)
        self._ws_send(self._page_ws, "Network.enable", mid=908)   # 供 extract_cookies 用（保活探针）
        self._ws_send(self._page_ws, "Page.navigate", {"url": CDP_YB_URL}, mid=907)
        time.sleep(3)
        # 等应用 chunk 就绪
        for _ in range(60):
            r = self._ws_send(self._page_ws, "Runtime.evaluate",
                              {"expression": "Object.keys(window).some(k=>k.startsWith('webpackChunk')) && window.webpackChunk_N_E && window.webpackChunk_N_E.length>3",
                               "returnByValue": True}, mid=903)
            if r.get("result", {}).get("value"):
                break
            time.sleep(1)
        else:
            raise RuntimeError("应用 chunk 加载超时")
        time.sleep(4)  # SDK 初始化窗口
        return self._page_ws

    # ---- 对外 ----
    def mint(self) -> dict:
        """铸造三件套；页面断线自动重建一次。"""
        with self._lock:
            for attempt in (1, 2):
                try:
                    ws = self._ensure_page()
                    ws.send(json.dumps({"id": 11, "method": "Runtime.evaluate",
                                        "params": {"expression": MINT_JS, "returnByValue": True}}))
                    deadline = time.time() + 30
                    while time.time() < deadline:
                        msg = json.loads(ws.recv())
                        if msg.get("id") == 11:
                            val = msg.get("result", {}).get("result", {}).get("value") or {}
                            if "uskey" in val and val["uskey"]:
                                self._warm = True
                                return val
                            raise RuntimeError("mint 返回异常: " + json.dumps(val)[:150])
                    raise TimeoutError("mint evaluate 超时")
                except Exception:
                    if attempt == 2:
                        raise
                    self._page_ws = None
                    self._browser_ws = None
                    time.sleep(1)

    def dynamic_fp(self) -> dict:
        """动态指纹（webversion/commit-tag/os_version/UA），失败返回 None 由上层兜底。"""
        try:
            with self._lock:
                ws = self._ensure_page()
                ws.send(json.dumps({"id": 12, "method": "Runtime.evaluate",
                                    "params": {"expression": FP_JS, "returnByValue": True}}))
                deadline = time.time() + 20
                while time.time() < deadline:
                    msg = json.loads(ws.recv())
                    if msg.get("id") == 12:
                        return msg.get("result", {}).get("result", {}).get("value")
        except Exception:
            return None
        return None

    def evaluate(self, js: str, timeout_s: int = 300):
        """页内执行 JS 并返回值（浏览器模式数据面通道）。"""
        with self._lock:
            ws = self._ensure_page()
            return self._eval_on(ws, js, timeout_s, mid=20)

    # ---- 登录页：点击切换 + 截图（必须全程持锁，见下方注释） ----
    def _eval_on(self, ws, js: str, timeout_s: int = 30, mid: int = 25):
        """在**已持锁**的 ws 上执行 JS（不再抢锁，否则死锁）。

        🔴 每条 CDP 会话必须带唯一 id 并读到自己的回包：_ensure_page 的存活探针用 id=1，
        evaluate 用 id=20。两个线程若在同一 ws 上交错 send/recv，会互相"偷"到对方的回包，
        各自永远等不到自己的 id → 卡到超时（实测表现为 /login 永久 hang + 502）。
        故所有 CDP 会话一律在 self._lock 内串行化。
        """
        ws.settimeout(timeout_s)
        ws.send(json.dumps({"id": mid, "method": "Runtime.evaluate",
                            "params": {"expression": js, "returnByValue": True, "awaitPromise": True}}))
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            msg = json.loads(ws.recv())
            if msg.get("id") == mid:
                res = msg.get("result", {})
                if res.get("exceptionDetails"):
                    raise RuntimeError("页内执行失败: " + json.dumps(res["exceptionDetails"], ensure_ascii=False)[:200])
                return res.get("result", {}).get("value")
        raise TimeoutError("eval 超时")

    def screenshot(self, tab: str = None, timeout_s: int = 60) -> bytes:
        """登录页截图：可选先切到 wechat/phone 登录方式。整段（切页 + 截图）持同一把锁，
        对外是一个原子 CDP 会话 —— 上层只需 asyncio.to_thread(m.screenshot, tab)。"""
        import base64
        with self._lock:
            ws = self._ensure_page()
            if tab in ("wechat", "phone"):
                names = "['微信','WeChat']" if tab == "wechat" else "['手机','Phone']"
                self._eval_on(ws, (
                    "(() => { const b=[...document.querySelectorAll('*')]"
                    ".filter(e=>e.offsetWidth>0&&e.children.length<=2"
                    "&&['登录','Log In'].includes((e.textContent||'').trim()))"
                    ".sort((a,b)=>(a.offsetWidth*a.offsetHeight)-(b.offsetWidth*b.offsetHeight))[0];"
                    " if(b && !document.querySelector('.hyc-login-v2,.hyc-phone-login')) b.click(); return 'ok'; })()"
                ), 30, mid=31)
                self._eval_on(ws, (
                    f"(() => {{ const c=[...document.querySelectorAll('*')]"
                    f".filter(e=>e.offsetWidth>0&&e.children.length<=2&&{names}.includes((e.textContent||'').trim()))"
                    f".sort((a,b)=>(a.offsetWidth*a.offsetHeight)-(b.offsetWidth*b.offsetHeight))[0];"
                    f" if(c) c.click(); return 'ok'; }})()"
                ), 30, mid=32)
                time.sleep(2)
            ws.settimeout(timeout_s)
            # 尽量只截"登录弹窗"区域并放大 2x：整页截图里二维码太小，手机扫不出来
            params = {"format": "png"}
            try:
                clip = self._eval_on(ws, CLIP_JS, 15, mid=33)
                if clip:
                    params["clip"] = {"x": clip["x"], "y": clip["y"],
                                      "width": clip["width"], "height": clip["height"], "scale": 2}
            except Exception:
                pass
            ws.settimeout(timeout_s)
            ws.send(json.dumps({"id": 40, "method": "Page.captureScreenshot", "params": params}))
            deadline = time.time() + timeout_s
            while time.time() < deadline:
                msg = json.loads(ws.recv())
                if msg.get("id") == 40:
                    return base64.b64decode(msg["result"]["data"])
        raise TimeoutError("截图超时")

    # ---- 登录态管理（给管理页/扫码页用） ----
    def extract_cookies(self, domain: str = "yuanbao.tencent.com") -> str:
        """取出该域的 cookie 并拼成 Cookie 头。

        用途：**脱离页面**做轻量保活/探活（GET /api/info/general 只需 cookie + 静态头，
        无需签名、无需页面）—— 探活的 HTTP 往返不占 CDP 锁，不干扰正在进行的对话。
        """
        with self._lock:
            ws = self._ensure_page()
            r = self._ws_send(ws, "Network.getCookies",
                              {"urls": ["https://" + domain + "/"]}, mid=45)
            cks = (r or {}).get("cookies") or []
            return "; ".join(f"{c['name']}={c['value']}" for c in cks if c.get("name"))

    def page_state(self) -> dict:
        """轻量页面状态：是否已登录 / 是否被冻结。给管理页做状态提示用。"""
        try:
            with self._lock:
                ws = self._ensure_page()
                return self._eval_on(ws, (
                    "(() => { const t = document.body.innerText || '';"
                    " return { url: location.href, title: document.title,"
                    "          frozen: t.indexOf('账号已冻结') >= 0,"
                    "          logged_in: document.cookie.indexOf('hy_user') >= 0,"
                    "          text: t.slice(0, 120) }; })()"
                ), 20, mid=34) or {}
        except Exception as e:
            return {"error": str(e)[:120]}

    def reset_login(self) -> dict:
        """重置登录态：清浏览器 cookie + localStorage（**保留设备种子 _qimei_h38**）+ 重载页面。

        账号被冻结 / 要换账号登录时必须先做这一步：冻结态页面没有"登录"按钮，
        不清会话就没法扫码或接码登入新账号。保留设备种子是为了不换指纹（换指纹更易触发风控）。
        """
        with self._lock:
            ws = self._ensure_page()
            self._ws_send(ws, "Network.enable", {}, mid=51)
            self._ws_send(ws, "Network.clearBrowserCookies", {}, mid=52)
            self._eval_on(ws, (
                "(() => { const k = localStorage.getItem('_qimei_h38');"
                " localStorage.clear(); if (k) localStorage.setItem('_qimei_h38', k);"
                " return 'ok'; })()"
            ), 20, mid=53)
            try:
                self._ws_send(ws, "Page.navigate", {"url": CDP_YB_URL}, mid=54)
            except Exception:
                pass
            time.sleep(4)
            for _ in range(60):
                try:
                    r = self._ws_send(ws, "Runtime.evaluate",
                                      {"expression": "document.readyState === 'complete'",
                                       "returnByValue": True}, mid=55)
                except Exception:
                    break
                if r.get("result", {}).get("value"):
                    break
                time.sleep(1)
            time.sleep(3)
            return {"ok": True}

    def healthz(self):
        return {"warm": self._warm}


_singleton = None
_slock = threading.Lock()


def get_minter() -> CDPMinter:
    global _singleton
    with _slock:
        if _singleton is None:
            _singleton = CDPMinter()
        return _singleton


if __name__ == "__main__":
    m = get_minter()
    print(json.dumps(m.mint(), ensure_ascii=False)[:120])
