#!/usr/bin/env python3
"""验证 COS CI 算符组合能否把成品图放到「精确」尺寸（对 4K 输出 3877 而非 3840 的修法）。

结论要回答两件事：
  1. `thumbnail/!WxHr`（覆盖放大）之后追加 `cut/WxH` 是否被支持、能否得到精确像素
  2. 裁剪是从中心还是左上（决定画面会不会歪）
"""
import base64
import os
import struct
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools.live_image_test import _dims, _req, _need, BASE, KEY, MODEL  # noqa: E402

OP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def get(url: str) -> tuple[bytes, str, int]:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with OP.open(req, timeout=90) as r:
        blob = r.read()
        return blob, _dims(blob), len(blob)


def main():
    _need()
    cache = "/tmp/ybt/last_url.txt"
    u = ""
    if os.path.exists(cache) and "--fresh" not in sys.argv:
        u = open(cache).read().strip()
        print("① 复用缓存的原图 URL")
    if not u:
        print("① 先拿一张 16:9 原图 URL")
        code, data, dt, hdr = _req("/v1/images/generations", {
            "model": MODEL, "prompt": "横构图：樱花树下的小路，柔和光线，写实",
            "size": "1792x1024", "n": 1, "response_format": "url"})
        print("   状态", code, f"{dt:.1f}s", "routed=", hdr.get("x-yb-routed-to"))
        if code != 200:
            print("   失败:", str(data)[:300])
            return
        u = data["data"][0]["url"]
        os.makedirs("/tmp/ybt", exist_ok=True)
        with open(cache, "w") as f:
            f.write(u)
    base, _, q = u.partition("?")

    def join(op):
        return base + "?" + op + ("&" + q if q else "")

    tests = [
        ("原图", u),
        ("thumbnail|cut（竖线链式）", join("imageMogr2/thumbnail/!3840x2160r|imageMogr2/cut/3840x2160")),
        ("thumbnail|gravity/center/cut", join("imageMogr2/thumbnail/!3840x2160r|imageMogr2/gravity/center/cut/3840x2160")),
        ("thumbnail|cut|gravity/center/cut", join("imageMogr2/thumbnail/!3840x2160r|imageMogr2/cut/3840x2160|imageMogr2/gravity/center")),
        ("2K：thumbnail|cut", join("imageMogr2/thumbnail/!2560x1440r|imageMogr2/cut/2560x1440")),
        ("auto 框不匹配：!1080x1920r", join("imageMogr2/thumbnail/!1080x1920r|imageMogr2/cut/1080x1920")),
    ]
    for name, url in tests:
        try:
            blob, dim, n = get(url)
            fn = "/tmp/ybt/opt_%04d.bin" % (abs(hash(name)) % 10000)
            with open(fn, "wb") as f:
                f.write(blob)
            print(f"   {name:46} → {dim:16} {n/1024:8.0f}KB  {fn}")
        except Exception as e:  # noqa: BLE001
            print(f"   {name:46} → 失败 {str(e)[:120]}")


if __name__ == "__main__":
    main()
