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
# 设备指纹种子：注入已知账号绑定的设备身份（避免 headless 新指纹触发风控）
# 格式 "k1=v1;k2=v2"（localStorage 键值对）
DEVICE_SEED = os.environ.get("YB_DEVICE_SEED", "_qimei_h38=e9632faf082420cd40bb971703000001419610")
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
        self._chrome_proc = subprocess.Popen(
            [CHROME_BIN, "--headless=new", "--no-sandbox", "--disable-dev-shm-usage",
             "--disable-gpu", "--remote-debugging-port=" + CDP_HTTP.split(":")[-1],
             "--user-data-dir=/tmp/yb-chrome", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _ws_send(self, ws, method, params=None, mid=1):
        ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        deadline = time.time() + 60
        while time.time() < deadline:
            msg = json.loads(ws.recv())
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
        # 重建
        self._ensure_chrome()
        ver = None
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
        r = self._ws_send(self._browser_ws, "Target.createTarget",
                          {"url": CDP_YB_URL}, mid=901)
        tid = r.get("targetId")  # _ws_send 已返回 result 内层
        if not tid:
            raise RuntimeError("createTarget 失败: " + json.dumps(r)[:200])
        time.sleep(3)
        targets = self._http_json("/json/list")
        page = next(t for t in targets if t["type"] == "page" and "yuanbao" in t["url"])
        if self._page_ws:
            try:
                self._page_ws.close()
            except Exception:
                pass
        self._page_ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=120, suppress_origin=True)
        self._ws_send(self._page_ws, "Emulation.setUserAgentOverride", {"userAgent": UA}, mid=902)
        self._ws_send(self._page_ws, "Page.addScriptToEvaluateOnNewDocument",
                      {"source": SEED_JS}, mid=904)
        self._page_ws.settimeout(120)
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
            ws.settimeout(timeout_s)
            ws.send(json.dumps({"id": 20, "method": "Runtime.evaluate",
                                "params": {"expression": js, "returnByValue": True, "awaitPromise": True}}))
            deadline = time.time() + timeout_s
            while time.time() < deadline:
                msg = json.loads(ws.recv())
                if msg.get("id") == 20:
                    res = msg.get("result", {})
                    if res.get("exceptionDetails"):
                        raise RuntimeError("页内执行失败: " + json.dumps(res["exceptionDetails"], ensure_ascii=False)[:200])
                    return res.get("result", {}).get("value")
            raise TimeoutError("evaluate 超时")

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
