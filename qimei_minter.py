#!/usr/bin/env python3
"""qimei_minter.py — 元宝签名三件套 minter 服务（对齐 minter-service 惯例）

契约：POST /v1/qimei/mint  （X-Minter-Key 鉴权；body 留空）
返回：{"uskey":..., "md5":..., "ts":...}（trio 缓存 30s；失败自动重建会话重试一次）
探活：GET  /healthz

监听：127.0.0.1:39100（MINTER_PORT 可改）——只绑回环，公网入口走网关/隧道
依赖：本机 bsk CLI + 已登录元宝的浏览器（复用 mint_backends.BskMinter）
"""
import os
import json
import time
import threading
import importlib.util

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import uvicorn

from mint_backends import BskMinter

MINTER_PORT = int(os.environ.get("MINTER_PORT", "39100"))
MINTER_KEY = os.environ.get("MINTER_KEY", "")
TRIO_TTL = float(os.environ.get("TRIO_TTL", "30"))
_PROXY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "yuanbao_openai_proxy.py")

app = FastAPI()
_lock = threading.Lock()
_trio = {"sig": None, "at": 0.0}
_backend = None


def _get_backend() -> BskMinter:
    global _backend
    if _backend is None:
        spec = importlib.util.spec_from_file_location("yuanbao_proxy", _PROXY_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _backend = BskMinter(mod)
    return _backend


@app.post("/v1/qimei/mint")
def mint(request: Request):
    if MINTER_KEY and request.headers.get("X-Minter-Key") != MINTER_KEY:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        with _lock:
            now = time.time()
            if not (_trio["sig"] and now - _trio["at"] < TRIO_TTL):
                _trio["sig"] = _get_backend().mint()
                _trio["at"] = time.time()
        return _trio["sig"]
    except Exception as e:
        # 失败即弃缓存，下次强制重铸
        _trio["sig"] = None
        return JSONResponse({"error": str(e)[:200]}, status_code=502)


@app.get("/healthz")
def healthz():
    ok = _trio["sig"] is not None and time.time() - _trio["at"] < 600
    return {"ok": ok, "age": round(time.time() - _trio["at"], 1) if _trio["sig"] else None}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=MINTER_PORT, log_level="info")
