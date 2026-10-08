#!/usr/bin/env python3
"""mint_backends.py — 元宝签名三件套铸签后端（统一接口，三实现）

契约：backend.mint() -> {"uskey": str, "md5": str, "ts": str}
      backend.headers() -> dict   # 与该次 mint 配套的动态指纹头（X-WebVersion 等）

三实现：
  BskMinter   本机 WorkBuddy 浏览器桥（Mac 开发模式）
  HttpMinter  远程 minter 服务，契约 POST {url}/v1/qimei/mint + X-Minter-Key
              （对齐 minter-service 惯例；服务端实现见 qimei_minter.py）
  CdpMinter   服务器自管 headless Chromium，CDP 直驱页内铸造（Linux 部署模式）
              浏览器管理见 cdp_minter.CDPMinter

选择：YB_MINT_BACKEND = auto|bsk|http|cdp
  auto 规则：YB_MINTER_URL 有值 → http；CDP_HTTP 有值 → cdp；否则 bsk
"""
import json
import os
import urllib.request

CDP_HTTP = os.environ.get("CDP_HTTP", "http://127.0.0.1:9222")
YB_MINTER_URL = os.environ.get("YB_MINTER_URL", "")
YB_MINTER_TOKEN = os.environ.get("YB_MINTER_TOKEN", "")
YB_MINT_BACKEND = os.environ.get("YB_MINT_BACKEND", "auto")


class MinterBase:
    def mint(self) -> dict:  # pragma: no cover
        raise NotImplementedError

    def headers(self) -> dict:  # 动态指纹头；无动态信息返回 {}
        return {}


class BskMinter(MinterBase):
    """本机 bsk 浏览器桥。proxy_mod = 主代理模块（复用其 _ensure_page/_dynamic_fp/_mint_only）。"""

    def __init__(self, proxy_mod):
        self._p = proxy_mod
        self._tab = None

    def mint(self) -> dict:
        ctx = self._p._ensure_page()
        tab = ctx["tabId"]
        fp = self._p._dynamic_fp(tab)
        self._p._ev("window.__ybStaticHeaders = " + json.dumps(fp) + "; 'ok'", tab_id=tab)
        self._tab = tab
        return self._p._mint_only(tab)

    def headers(self) -> dict:
        return self._p._dynamic_fp(self._tab) if self._tab else {}


class HttpMinter(MinterBase):
    """远程 minter 服务（POST {url}/v1/qimei/mint，X-Minter-Key 鉴权）。"""

    def __init__(self, url: str, key: str):
        self._url = url.rstrip("/")
        self._key = key

    def mint(self) -> dict:
        req = urllib.request.Request(
            self._url + "/v1/qimei/mint", data=b"{}",
            headers={"content-type": "application/json", "X-Minter-Key": self._key},
            method="POST")
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=30) as r:
            data = json.loads(r.read())
        if "uskey" not in data:
            raise RuntimeError("minter 返回异常: " + json.dumps(data)[:150])
        return {"uskey": data["uskey"], "md5": data["md5"], "ts": str(data["ts"])}


class CdpMinter(MinterBase):
    """服务器自管 headless Chromium（CDP 直驱）。"""

    def __init__(self):
        from cdp_minter import get_minter
        self._m = get_minter()

    def mint(self) -> dict:
        v = self._m.mint()
        return {"uskey": v["uskey"], "md5": v["md5"], "ts": str(v["ts"])}

    def headers(self) -> dict:
        fp = self._m.dynamic_fp() or {}
        h = {}
        if fp.get("h38"):
            h["X-HY92"] = fp["h38"]          # uskey 与 h38 必须同源
        if fp.get("ver"):
            h["X-WebVersion"] = fp["ver"]
        if fp.get("tag"):
            h["x-commit-tag"] = fp["tag"]
        if fp.get("osv"):
            h["X-os_version"] = fp["osv"]
        if fp.get("ua"):
            h["user-agent"] = fp["ua"]
        return h


def create_minter(proxy_mod=None) -> MinterBase:
    backend = YB_MINT_BACKEND
    if backend == "auto":
        if YB_MINTER_URL:
            backend = "http"
        elif os.environ.get("CDP_HTTP"):
            backend = "cdp"
        else:
            backend = "bsk"
    if backend == "http":
        if not YB_MINTER_URL:
            raise RuntimeError("YB_MINT_BACKEND=http 需要 YB_MINTER_URL")
        return HttpMinter(YB_MINTER_URL, YB_MINTER_TOKEN)
    if backend == "cdp":
        return CdpMinter()
    if proxy_mod is None:
        raise RuntimeError("YB_MINT_BACKEND=bsk 需要 proxy_mod")
    return BskMinter(proxy_mod)
