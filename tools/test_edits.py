#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_edits.py — OpenAI 标准图生图入口 `/v1/images/edits`（multipart）

背景：此前只有 `/v1/images/generations`（JSON 的 image 放 URL/dataURI），
而 OpenAI 官方图生图是 `/v1/images/edits`（multipart 上传文件）⇒ 多数 SDK 走这个。

覆盖：
  E1 _bytes_to_data_uri：按魔数嗅探 mime（PNG/JPEG/WebP/GIF/未知）
  E2 _edits_parse：JSON 形态（str / list / 空）
  E3 _edits_parse：multipart（单图 / 多图 / 超 4 张截断 / mask 标记 / 字段解析）
  E4 端点级：multipart 上传 → 200 且参考图真的进了下游（捕获 image_refs）
  E5 端点级：无图 → 400；有图无 prompt → 用默认 prompt 而不是报错
  E6 源码断言：generations 与 edits 共用 `_images_core`（不允许复制两份）
"""
import importlib.util
import io as _io
import os
import sys
import base64
import struct
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

spec = importlib.util.spec_from_file_location("ybp", os.path.join(ROOT, "yuanbao_openai_proxy.py"))
ybp = importlib.util.module_from_spec(spec)
sys.modules["ybp"] = ybp
spec.loader.exec_module(ybp)

from fastapi.testclient import TestClient   # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print("  %s %s%s" % ("PASS" if cond else "FAIL", name, ("  " + str(detail)) if detail != "" else ""))
    if not cond:
        FAILS.append(name)


def _png(w=8, h=8, rgb=(200, 30, 30)):
    """手搓一个最小合法 PNG（不依赖 PIL）。"""
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))

    def chunk(tag, data):
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b""))


PNG = _png()
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
WEBP = b"RIFF" + struct.pack("<I", 64) + b"WEBP" + b"VP8 " + b"\x00" * 40
GIF = b"GIF89a" + b"\x00" * 32
BLOB = b"\x01\x02\x03\x04unknown"


def e1():
    print("[E1] _bytes_to_data_uri mime 嗅探")
    cases = [("PNG", PNG, "image/png"), ("JPEG", JPEG, "image/jpeg"),
             ("WebP", WEBP, "image/webp"), ("GIF", GIF, "image/gif"),
             ("未知字节", BLOB, "image/png")]
    for name, blob, mime in cases:
        uri = ybp._bytes_to_data_uri(blob)
        want = "data:%s;base64," % mime
        check("E1 %-8s → %s" % (name, mime), uri.startswith(want), uri[:36])
        # 往返：base64 解回来必须与原字节一致
        got = base64.b64decode(uri.split(";base64,", 1)[1])
        check("E1 %-8s base64 往返一致" % name, got == blob, "len %d vs %d" % (len(got), len(blob)))

    # 显式 mime 优先（哪怕魔数不认）
    uri = ybp._bytes_to_data_uri(BLOB, "image/jpeg")
    check("E1 显式 mime 优先", uri.startswith("data:image/jpeg;base64,"), uri[:34])
    # 非 image/* 的 mime 要回落嗅探
    uri = ybp._bytes_to_data_uri(PNG, "text/plain")
    check("E1 非图片 mime 回落嗅探", uri.startswith("data:image/png;base64,"), uri[:34])


class FakeForm(dict):
    def getlist(self, k):
        v = self.get(k)
        if v is None:
            return []
        return v if isinstance(v, list) else [v]


class FakeUpload:
    def __init__(self, blob, ctype="image/png"):
        self._blob = blob
        self.content_type = ctype

    async def read(self):
        return self._blob


class FakeReq:
    def __init__(self, headers=None, form=None, json_body=None):
        self.headers = headers or {}
        self._form = form
        self._json = json_body

    async def form(self):
        return self._form

    async def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


def _run(coro):
    import asyncio
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def e2():
    print("[E2] _edits_parse（JSON 形态）")
    rq = FakeReq(headers={"content-type": "application/json"},
                 json_body={"prompt": "改造", "image": "https://a/b.png", "size": "2k"})
    body, refs = _run(ybp._edits_parse(rq))
    check("E2 JSON 单图", refs == ["https://a/b.png"], str(refs))
    check("E2 JSON 字段保留", body.get("prompt") == "改造" and body.get("size") == "2k", str(body))

    rq = FakeReq(headers={"content-type": "application/json"},
                 json_body={"prompt": "x", "image": ["https://a/1.png", "https://a/2.png", ""]})
    body, refs = _run(ybp._edits_parse(rq))
    check("E2 JSON 多图（去空）", refs == ["https://a/1.png", "https://a/2.png"], str(refs))

    rq = FakeReq(headers={"content-type": "application/json"}, json_body={"prompt": "x"})
    body, refs = _run(ybp._edits_parse(rq))
    check("E2 JSON 无图 ⇒ []", refs == [])

    rq = FakeReq(headers={"content-type": "text/plain"}, json_body=None)
    body, refs = _run(ybp._edits_parse(rq))
    check("E2 非 JSON 非 multipart ⇒ 400", isinstance(body, ybp.JSONResponse) and body.status_code == 400)


def e3():
    print("[E3] _edits_parse（multipart 形态）")
    form = FakeForm({"image": FakeUpload(PNG, "image/png"), "prompt": "换成雪山",
                     "model": "hy-image-3.5", "size": "4k", "n": "2"})
    rq = FakeReq(headers={"content-type": "multipart/form-data; boundary=x"}, form=form)
    body, refs = _run(ybp._edits_parse(rq))
    check("E3 单图转 dataURI", len(refs) == 1 and refs[0].startswith("data:image/png;base64,"), str(refs)[:50])
    check("E3 prompt 解析", body.get("prompt") == "换成雪山", body.get("prompt"))
    check("E3 size 解析", body.get("size") == "4k", body.get("size"))
    check("E3 n 转 int", body.get("n") == 2, body.get("n"))

    # 多图
    form = FakeForm({"image": [FakeUpload(PNG), FakeUpload(JPEG, "image/jpeg")], "prompt": "p"})
    rq = FakeReq(headers={"content-type": "multipart/form-data; boundary=x"}, form=form)
    body, refs = _run(ybp._edits_parse(rq))
    check("E3 多图", len(refs) == 2 and refs[1].startswith("data:image/jpeg;base64,"), str(len(refs)))

    # 超 4 张截断（元宝上限）
    form = FakeForm({"image": [FakeUpload(PNG) for _ in range(7)], "prompt": "p"})
    rq = FakeReq(headers={"content-type": "multipart/form-data; boundary=x"}, form=form)
    body, refs = _run(ybp._edits_parse(rq))
    check("E3 超 4 张截断到 4", len(refs) == 4, len(refs))

    # mask ⇒ 标记忽略
    form = FakeForm({"image": FakeUpload(PNG), "prompt": "p", "mask": FakeUpload(PNG)})
    rq = FakeReq(headers={"content-type": "multipart/form-data; boundary=x"}, form=form)
    body, refs = _run(ybp._edits_parse(rq))
    check("E3 mask 标记为忽略", body.get("_mask_ignored") is True, body.get("_mask_ignored"))

    # image 作为普通文本字段（URL）
    form = FakeForm({"image": "https://a/b.png", "prompt": "p"})
    rq = FakeReq(headers={"content-type": "multipart/form-data; boundary=x"}, form=form)
    body, refs = _run(ybp._edits_parse(rq))
    check("E3 image 为文本 URL 也支持", refs == ["https://a/b.png"], str(refs))

    # 单张超限
    real_max = ybp.YB_EDITS_MAX_BYTES
    try:
        ybp.YB_EDITS_MAX_BYTES = 16
        form = FakeForm({"image": FakeUpload(PNG), "prompt": "p"})
        rq = FakeReq(headers={"content-type": "multipart/form-data; boundary=x"}, form=form)
        body, refs = _run(ybp._edits_parse(rq))
        check("E3 单张超限 ⇒ 413", isinstance(body, ybp.JSONResponse) and body.status_code == 413)
    finally:
        ybp.YB_EDITS_MAX_BYTES = real_max


def _client():
    return TestClient(ybp.app)


def _patch_downstream(seen):
    """mock 下游：记录 image_refs，返回一个合法的 result。"""
    real = ybp._cookie_mode_run

    def fake(cookie, agent, prompt, chat_model, image_refs, resolution, ratio=None, force_image=False):
        seen.append({"prompt": prompt, "refs": list(image_refs), "resolution": resolution,
                     "model": chat_model, "cookie": cookie[:20]})
        return {"status": 200, "urls": ["https://cdn.example.com/out_h0_.png"],
                "wmUrls": ["https://cdn.example.com/out_h1_.png"], "text": "", "error": None, "raw": None}
    ybp._cookie_mode_run = fake
    return real


def e4():
    print("[E4] 端点级：multipart 上传 → 参考图进下游")
    real_run = ybp._cookie_mode_run
    real_auth = ybp._check_auth
    seen = []
    try:
        ybp._check_auth = lambda req: ("cookie", "hy_user=x; hy_token=y")
        ybp._want_pool_route = lambda req: False
        real_run_cm = _patch_downstream(seen)      # 注意：它内部已设好 fake，返回值是原函数
        ybp.YB_UPSCALE_ENABLE = False

        c = _client()
        r = c.post("/v1/images/edits",
                   files={"image": ("ref.png", PNG, "image/png")},
                   data={"prompt": "换成雪山小木屋", "size": "2048x2048", "model": "hy-image-3.5"})
        check("E4 http=200", r.status_code == 200, r.status_code)
        d = r.json() if r.status_code == 200 else {}
        data = d.get("data") or []
        check("E4 返回 1 张", len(data) == 1, str(len(data)))
        check("E4 url = 无水印原图", (data[0].get("url") if data else "").endswith("_h0_.png"),
              (data[0].get("url") if data else ""))
        check("E4 同时给带水印版", bool(data and data[0].get("url_watermarked")), str(data[:1])[:80])
        check("E4 参考图确实进下游", seen and seen[0]["refs"] and
              seen[0]["refs"][0].startswith("data:image/png;base64,"), str(seen[:1])[:90])
        check("E4 prompt 透传", seen and seen[0]["prompt"] == "换成雪山小木屋", seen[0]["prompt"] if seen else "")
        check("E4 上游恒 2K", seen and seen[0]["resolution"] == "2K", seen[0]["resolution"] if seen else "")
    finally:
        ybp._cookie_mode_run = real_run
        ybp._check_auth = real_auth


def e5():
    print("[E5] 端点级：边界（无图 / 无 prompt / mask 提示）")
    real_run, real_auth = ybp._cookie_mode_run, ybp._check_auth
    seen = []
    try:
        ybp._check_auth = lambda req: ("cookie", "hy_user=x; hy_token=y")
        ybp._want_pool_route = lambda req: False
        real_run_cm = _patch_downstream(seen)      # 注意：它内部已设好 fake，返回值是原函数
        ybp.YB_UPSCALE_ENABLE = False
        c = _client()

        r = c.post("/v1/images/edits", data={"prompt": "只有文字没图"})
        check("E5 无图 ⇒ 400", r.status_code == 400, r.status_code)
        check("E5 400 文案指明要参考图", "参考图" in r.text, r.text[:80])

        # 有图无 prompt → 用默认 prompt（不白等）
        seen[:] = []
        r = c.post("/v1/images/edits", files={"image": ("a.png", PNG, "image/png")}, data={})
        check("E5 有图无 prompt ⇒ 200", r.status_code == 200, r.status_code)
        check("E5 自动补默认 prompt", seen and seen[0]["prompt"].startswith("保持主体不变"),
              seen[0]["prompt"] if seen else "")

        # mask ⇒ 响应头警告
        seen[:] = []
        r = c.post("/v1/images/edits",
                   files={"image": ("a.png", PNG, "image/png"),
                          "mask": ("m.png", PNG, "image/png")},
                   data={"prompt": "局部改"})
        check("E5 mask 有警告头", "mask" in (r.headers.get("x-yb-warning") or "").lower(),
              r.headers.get("x-yb-warning"))
        check("E5 mask 不当第二张参考图", seen and len(seen[0]["refs"]) == 1,
              len(seen[0]["refs"]) if seen else "")
    finally:
        ybp._cookie_mode_run, ybp._check_auth = real_run, real_auth


def e6():
    print("[E6] 源码断言：两条路径共用 _images_core")
    src = open(os.path.join(ROOT, "yuanbao_openai_proxy.py"), encoding="utf-8").read()
    lines = src.split("\n")
    i_core = next((i for i, l in enumerate(lines, 1) if l.startswith("async def _images_core")), None)
    i_gen = next((i for i, l in enumerate(lines, 1) if l.startswith("async def images_generations")), None)
    i_edt = next((i for i, l in enumerate(lines, 1) if l.startswith("async def images_edits")), None)
    check("E6 找到 _images_core", i_core is not None, i_core)
    check("E6 找到两个端点", i_gen and i_edt and i_gen < i_edt, "%s/%s" % (i_gen, i_edt))

    gen_body = "\n".join(lines[i_gen - 1:i_edt - 1])
    edt_body = "\n".join(lines[i_edt - 1:i_edt + 40])
    check("E6 generations 调 _images_core", "_images_core(req, body)" in gen_body)
    check("E6 edits 调 _images_core", "_images_core(req, body, image_refs_in=refs)" in edt_body)
    check("E6 generations 不再自己跑 GUARD（避免两处重复）",
          "GUARD.check" not in gen_body, "命中说明逻辑又复制了一份")
    check("E6 edits 走轮询", 'pool_router(req, "images/edits")' in edt_body)
    # 路由必须注册
    check("E6 注册 /v1/images/edits", '@app.post("/v1/images/edits")' in src)


def e7():
    print("[E7] 轮询入口透传所有 X-YB-* 头")
    src = open(os.path.join(ROOT, "yuanbao_openai_proxy.py"), encoding="utf-8").read()
    # 必须按前缀通配透传，不能只挑固定的几个（mask 警告就这样经轮询丢过）
    check("E7 按 x-yb- 前缀透传", 'startswith("x-yb-")' in src)
    check("E7 排除 routed-to（避免被上游覆盖）", '"x-yb-routed-to"' in src)
    # 不要再出现"只挑 upscale 一个"的写法
    check("E7 没有硬编码只透传 upscale",
          'up.headers.get("X-YB-Upscale")' not in src, "说明还是单点透传，新头会漏")


if __name__ == "__main__":
    e1(); e2(); e3(); e4(); e5(); e6(); e7()
    print()
    if FAILS:
        print("FAILED %d: %s" % (len(FAILS), FAILS))
        sys.exit(1)
    print("全部通过")
