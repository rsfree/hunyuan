#!/usr/bin/env python3
"""线上生图实测：多比例文生图 + 超分 + 图生图单/多图。

只做「调线上 /v1 接口 → 下载成品 → 读真实像素」这一件事，不碰账号池内部。
用法: python3 tools/live_image_test.py [阶段]   # 阶段 = t2i | i2i | all
"""
from __future__ import annotations

import base64
import json
import os
import struct
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

# 🔴 本文件会随仓库公开：入口与 key 一律走环境变量，不留默认值
BASE = os.environ.get("YB_BASE", "").rstrip("/")
KEY = os.environ.get("YB_KEY", "")
OUT = os.environ.get("YB_OUT", "/tmp/ybt")
MODEL = os.environ.get("YB_MODEL", "hy-image-3.5")


def _need() -> None:
    """缺配置时给一句能照着做的提示，而不是抛 401（401 会被误读成 key 错）。"""
    miss = [n for n, v in (("YB_BASE", BASE), ("YB_KEY", KEY)) if not v]
    if miss:
        raise SystemExit("缺少环境变量 %s。例：\n"
                         "  YB_BASE=https://your-proxy.example.com YB_KEY=sk-xxx "
                         "python3 tools/live_image_test.py all"
                         % " / ".join(miss))

# 全局禁用代理：本机 HTTP_PROXY 会把发往公网的请求劫走
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_print_lock = threading.Lock()


def log(*a):
    with _print_lock:
        print(*a, flush=True)


def _req(path: str, payload: dict, timeout: int = 240) -> tuple[int, dict, float, dict]:
    body = json.dumps(payload).encode()
    r = urllib.request.Request(
        BASE + path, data=body,
        headers={"Authorization": "Bearer " + KEY, "Content-Type": "application/json"},
        method="POST",
    )
    t0 = time.time()
    try:
        with _OPENER.open(r, timeout=timeout) as resp:
            raw = resp.read()
            code = resp.status
            hdr = {k.lower(): v for k, v in resp.headers.items()}
    except urllib.error.HTTPError as e:
        raw = e.read()
        code = e.code
        hdr = {k.lower(): v for k, v in e.headers.items()}
    dt = time.time() - t0
    try:
        data = json.loads(raw or b"{}")
    except Exception:
        data = {"_raw": raw[:400].decode("utf-8", "replace")}
    return code, data, dt, hdr


def _dl(url: str, timeout: int = 120) -> tuple[bytes, str]:
    """下载成品图；返回 (bytes, 尺寸描述)。"""
    r = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with _OPENER.open(r, timeout=timeout) as resp:
        blob = resp.read()
    return blob, _dims(blob)


def _dims(blob: bytes) -> str:
    """从文件头读真实像素（PNG / JPEG / WEBP / GIF）。"""
    try:
        if blob[:8] == b"\x89PNG\r\n\x1a\n":
            w, h = struct.unpack(">II", blob[16:24])
            return f"{w}x{h} PNG"
        if blob[:2] == b"\xff\xd8":
            i = 2
            while i < len(blob) - 9:
                if blob[i] != 0xFF:
                    i += 1
                    continue
                m = blob[i + 1]
                if m in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    h, w = struct.unpack(">HH", blob[i + 5:i + 9])
                    return f"{w}x{h} JPEG"
                seg = struct.unpack(">H", blob[i + 2:i + 4])[0]
                i += 2 + seg
            return "JPEG(未知)"
        if blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
            fmt = blob[12:16]
            if fmt == b"VP8X":
                w = int.from_bytes(blob[24:27], "little") + 1
                h = int.from_bytes(blob[27:30], "little") + 1
                return f"{w}x{h} WEBP"
            return "WEBP(未知)"
        if blob[:4] == b"GIF8":
            w, h = struct.unpack("<HH", blob[6:10])
            return f"{w}x{h} GIF"
    except Exception as e:  # noqa: BLE001
        return f"解析失败:{e}"
    return f"未知头 {blob[:8]!r}"


def one_case(tag: str, size: str, prompt: str) -> dict:
    """一个文生图用例：出图 → 下载 → 量像素。"""
    payload = {"model": MODEL, "prompt": prompt, "size": size, "n": 1, "response_format": "url"}
    code, data, dt, hdr = _req("/v1/images/generations", payload)
    rec = {"tag": tag, "req": size, "code": code, "ms": int(dt * 1000),
           "routed": hdr.get("x-yb-routed-to") or hdr.get("X-YB-Routed-To"),
           "upscale": hdr.get("x-yb-upscale")}
    if code != 200 or not data.get("data"):
        rec["err"] = json.dumps(data, ensure_ascii=False)[:300]
        log(f"❌ {tag:14} {size:11} {code} {dt:5.1f}s  {rec['err'][:160]}")
        return rec
    item = data["data"][0]
    url = item.get("url") or ""
    rec["has_wm_url"] = bool(item.get("url_watermarked"))
    if url:
        try:
            blob, dim = _dl(url)
            rec["dim"] = dim
            rec["bytes"] = len(blob)
            fn = os.path.join(OUT, f"{tag.replace(':', '').replace('/', '-')}.img")
            with open(fn, "wb") as f:
                f.write(blob)
            rec["file"] = fn
        except Exception as e:  # noqa: BLE001
            rec["dl_err"] = str(e)[:200]
    elif item.get("b64_json"):
        blob = base64.b64decode(item["b64_json"])
        rec["dim"] = _dims(blob)
        rec["bytes"] = len(blob)
    log(f"✅ {tag:14} {size:11} {code} {dt:5.1f}s  → {rec.get('dim','?'):12} "
        f"{rec.get('bytes',0)/1024:6.0f}KB  routed={rec['routed']}  upscale={rec['upscale']}")
    return rec


T2I_CASES = [
    ("1x1", "1024x1024", "一只橘猫坐在窗台上，午后阳光，写实摄影"),
    ("16x9", "1792x1024", "赛博朋克风格的上海外滩夜景，霓虹灯，广角"),
    ("9x16", "1024x1792", "竖构图：瀑布从山谷倾泻，晨雾，电影感"),
    ("4x3", "1408x1056", "水彩画：安静的江南小镇，小雨，青石板路"),
    ("3x4", "1056x1408", "油画风：一位老者侧脸肖像，伦勃朗光"),
    ("21x9", "1920x816", "超宽画幅：雪山日出全景，金色云海"),
    ("2K", "2560x1440", "极简主义：白色空间中的一把红色椅子，产品摄影"),
    ("4K", "3840x2160", "航拍：热带海岛俯瞰，碧绿海水与白色沙滩"),
]


def run_t2i(workers: int = 5) -> list[dict]:
    os.makedirs(OUT, exist_ok=True)
    log(f"== 文生图 {len(T2I_CASES)} 例，并发 {workers}，模型 {MODEL} ==")
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(one_case, t, s, p) for t, s, p in T2I_CASES]
        recs = [f.result() for f in futs]
    log(f"-- 文生图墙钟 {time.time()-t0:.1f}s  ok={sum(1 for r in recs if r['code']==200)}/{len(recs)}")
    return recs


def _is_png(path: str) -> bool:
    with open(path, "rb") as f:
        return f.read(8) == b"\x89PNG\r\n\x1a\n"


def _data_uri(path: str) -> str:
    with open(path, "rb") as f:
        return "data:image/png;base64," + base64.b64encode(f.read()).decode()


def i2i_case(tag: str, images: list[str], size: str, prompt: str) -> dict:
    """一个图生图用例（走 /v1/images/edits，OpenAI 兼容的多图 image 数组）。"""
    payload = {"model": MODEL, "prompt": prompt, "size": size, "n": 1,
               "response_format": "url", "image": images}
    code, data, dt, hdr = _req("/v1/images/edits", payload)
    rec = {"tag": tag, "req": size, "n_img": len(images), "code": code,
           "ms": int(dt * 1000), "routed": hdr.get("x-yb-routed-to") or hdr.get("X-YB-Routed-To")}
    if code != 200 or not data.get("data"):
        rec["err"] = json.dumps(data, ensure_ascii=False)[:300]
        log(f"❌ {tag:16} n={len(images)} {code} {dt:5.1f}s  {rec['err'][:160]}")
        return rec
    item = data["data"][0]
    url = item.get("url") or ""
    if url:
        try:
            blob, dim = _dl(url)
            rec["dim"] = dim
            rec["bytes"] = len(blob)
            fn = os.path.join(OUT, f"i2i_{tag}.img")
            with open(fn, "wb") as f:
                f.write(blob)
            rec["file"] = fn
        except Exception as e:  # noqa: BLE001
            rec["dl_err"] = str(e)[:200]
    log(f"✅ {tag:16} n={len(images)} {code} {dt:5.1f}s  → {rec.get('dim','?')} "
        f"{rec.get('bytes',0)/1024:6.0f}KB routed={rec['routed']}")
    return rec


def run_i2i(src: list[dict], workers: int = 2) -> list[dict]:
    files = [r["file"] for r in src if r.get("file")][:3]
    if not files:
        # 单独跑 i2i 阶段时，复用上次 t2i 落盘的成品图
        import glob
        files = sorted(glob.glob(os.path.join(OUT, "*.img")))[:3]
    if not files:
        log("!! 没有可用源图，跳过图生图")
        return []
    for p in files:
        if not _is_png(p):
            log(f"!! 源图不是 PNG（{os.path.basename(p)}），edits 需要正确 mime，跳过")
    files = [p for p in files if _is_png(p)]
    if not files:
        return []
    uris = [_data_uri(p) for p in files]

    cases = [
        ("single", [uris[0]], "1024x1024", "把这张图改成油画风格，保持构图"),
        ("single_wide", [uris[0]], "1792x1024", "把这张图扩成宽幅电影画面，保持主体"),
        ("multi2", uris[:2] if len(uris) >= 2 else uris[:1], "1024x1024",
         "把这两张图的主体合成到同一场景里，自然融合"),
        ("multi3", uris[:3] if len(uris) >= 3 else uris, "1024x1024",
         "把这三张图的风格统一成同一张海报，主体都保留"),
        ("multi_4k", uris[:2] if len(uris) >= 2 else uris[:1], "3840x2160",
         "融合这几张图，输出 4K 宽幅壁纸"),
    ]
    log(f"== 图生图 {len(cases)} 例，并发 {workers} ==")
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(i2i_case, t, im, s, p) for t, im, s, p in cases]
        recs = [f.result() for f in futs]
    log(f"-- 图生图墙钟 {time.time()-t0:.1f}s  ok={sum(1 for r in recs if r['code']==200)}/{len(recs)}")
    return recs


def main():
    _need()
    phase = (sys.argv[1] if len(sys.argv) > 1 else "all").lower()
    os.makedirs(OUT, exist_ok=True)
    all_recs: dict[str, list] = {}
    if phase in ("t2i", "all"):
        all_recs["t2i"] = run_t2i()
    if phase in ("i2i", "all"):
        src = all_recs.get("t2i") or []
        all_recs["i2i"] = run_i2i(src)
    with open(os.path.join(OUT, "report.json"), "w") as f:
        json.dump(all_recs, f, ensure_ascii=False, indent=2)

    print("\n==== 汇总 ====")
    for ph, recs in all_recs.items():
        ok = [r for r in recs if r["code"] == 200]
        if ok:
            ms = sorted(r["ms"] for r in ok)
            print(f"{ph}: {len(ok)}/{len(recs)} 成功  "
                  f"耗时 min/med/max = {ms[0]/1000:.1f}/{ms[len(ms)//2]/1000:.1f}/{ms[-1]/1000:.1f}s")
        else:
            print(f"{ph}: 0/{len(recs)} 成功")
        for r in recs:
            print(f"   {'OK ' if r['code']==200 else 'ERR'} {r['tag']:16} {r['req']:11} "
                  f"{r['ms']/1000:5.1f}s {r.get('dim','')}")


if __name__ == "__main__":
    main()
