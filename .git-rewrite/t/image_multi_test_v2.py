#!/usr/bin/env python3
"""多形态生图测试 v2 — 覆盖 size→ratio 适配：比例、分辨率、数量、智能比例。"""
import json
import math
import time
import base64
import urllib.request

PROXY = "http://127.0.0.1:8177"

CASES = [
    {"tag": "1:1_1K",    "prompt": "一枚极简红色圆点",              "size": "1024x1024", "n": 1},
    {"tag": "16:9_2K",   "prompt": "雪山脚下的金色麦田与热气球",    "size": "1792x1024", "n": 1},
    {"tag": "9:16_2K",   "prompt": "赛博朋克雨夜街道竖版霓虹灯",    "size": "1024x1792", "n": 1},
    {"tag": "4:3_1.5K",  "prompt": "湖边的一只白鹭",               "size": "1536x1024", "n": 1},
    {"tag": "3:4_1.5K",  "prompt": "古风亭子在樱花树下",            "size": "1024x1536", "n": 1},
    {"tag": "auto_智能",  "prompt": "宇宙深处的一座空间站",          "size": "auto",      "n": 1},
]

SLOTS = {"1:1": 1.0, "4:3": 4/3, "3:4": 3/4, "16:9": 16/9, "9:16": 9/16}
def size_to_ratio(size):
    if not size or size.strip().lower() in ("auto", "smart"): return None
    s = size.strip().lower()
    if s in SLOTS: return s
    import re
    m = re.match(r"^(\d{2,4})x(\d{2,4})$", s)
    if not m: return None
    w, h = int(m.group(1)), int(m.group(2))
    return min(SLOTS, key=lambda k: abs(math.log((w/h)/SLOTS[k])))



def post_images(payload, timeout=300):
    req = urllib.request.Request(PROXY + "/v1/images/generations",
                                 data=json.dumps(payload).encode(),
                                 headers={"content-type": "application/json"}, method="POST")
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read()), time.time() - t0
    except urllib.error.HTTPError as e:
        return json.loads(e.read()), time.time() - t0


def img_dims(url):
    import subprocess
    p = f"/tmp/mv_{int(time.time()*1000)}.png"
    subprocess.run(["curl", "-s", "--noproxy", "*", "-o", p, url], timeout=60)
    out = subprocess.run(["sips", "-g", "pixelWidth", "-g", "pixelHeight", p],
                         capture_output=True, text=True).stdout
    dims = [int(l.split(":")[1].strip()) for l in out.splitlines() if "pixel" in l]
    return dims, p


def main():
    results = []
    for i, c in enumerate(CASES, 1):
        print(f"\n=== [{i}/{len(CASES)}] {c['tag']} | size={c['size']} n={c['n']} ===")
        print(f"prompt: {c['prompt']}")
        resp, dt = post_images({"model": "dall-e-3", "prompt": c["prompt"], "size": c["size"], "n": c["n"]})
        if "error" in resp:
            print(f"  ❌ {resp['error'].get('message','')[:80]}")
            results.append({**c, "error": resp["error"].get("message", "")[:80]})
            continue
        for j, d in enumerate(resp["data"]):
            url = d["url"]
            wm = "_h0_" not in url and "h0" not in url
            dims, path = img_dims(url)
            # 校验比例：与映射后槽位比对（对数容差 3%）；auto/智能比例不校验
            slot = size_to_ratio(c["size"])
            ratio_ok = None
            exp = None
            if slot:
                exp = SLOTS[slot]
                act = dims[0] / dims[1]
                ratio_ok = abs(math.log(act / exp)) < 0.03
            mark = "OK" if (not req_w or ratio_ok) else "RATIO-MISMATCH"
            print(f"  [{j}] {dims[0]}x{dims[1]} ({dims[0]*dims[1]/1e6:.2f}MP) 实际={act:.4f} 期望槽位={slot or '智能'}({exp if exp else '-'}) [{mark}] {dt:.1f}s 无水印={not wm}")
            results.append({**c, "index": j, "url": url, "w": dims[0], "h": dims[1],
                            "ratio_ok": ratio_ok, "elapsed": round(dt, 1)})
            # 存样图
            import shutil
            shutil.copy(path, f"/Users/betterme/PycharmProjects/AI/reverse/hunyuan/samples/mv2_{c['tag']}_{j}.png")
        time.sleep(3)

    print("\n" + "=" * 70)
    ok = sum(1 for r in results if r.get("ratio_ok") is not False and "url" in r)
    total = len([r for r in results if "url" in r])
    print(f"汇总：{total} 张图，比例命中 {ok}/{total}")
    for r in results:
        if "url" in r:
            print(f"  [{'PASS' if r.get('ratio_ok') is not False else 'FAIL'}] {r['tag']} #{r.get('index',0)}  {r['w']}x{r['h']}  {r['elapsed']}s")
        else:
            print(f"  [FAIL] {r['tag']}  {r.get('error')}")

    with open("/Users/betterme/PycharmProjects/AI/reverse/hunyuan/capture/multivariant_v2_result.json", "w") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print("结果已存 capture/multivariant_v2_result.json")


if __name__ == "__main__":
    main()
