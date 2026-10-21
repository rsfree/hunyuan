#!/usr/bin/env python3
"""多形态生图测试：不同 size/分辨率/数量 → 打印图片链接 + 下载验证"""
import json
import subprocess
import time
import urllib.request

PROXY = "http://127.0.0.1:8177"

CASES = [
    {"tag": "方形1K",   "prompt": "一只圆滚滚的柯基犬趴在木地板上", "size": "1024x1024", "n": 1},
    {"tag": "横版1.5K", "prompt": "雪山脚下的蓝色湖泊，清晨薄雾",   "size": "1536x1024", "n": 2},
    {"tag": "竖版1.5K", "prompt": "赛博朋克风格的雨夜街道霓虹灯",   "size": "1024x1536", "n": 1},
    {"tag": "方形2K",   "prompt": "水彩风格的樱花树与小木屋",       "size": "2048x2048", "n": 1},
]


def post_images(payload: dict, timeout: int = 300) -> dict:
    req = urllib.request.Request(
        PROXY + "/v1/images/generations",
        data=json.dumps(payload).encode(),
        headers={"content-type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def probe_url(url: str) -> tuple:
    """HEAD 风格探测：返回 (http_status, content_type, content_length)"""
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.headers.get("content-type", "?"), r.headers.get("content-length", "?")
    except Exception as e:
        return getattr(e, "code", str(e)), "?", "?"


results = []
for i, case in enumerate(CASES, 1):
    print(f"\n=== [{i}/{len(CASES)}] {case['tag']} | size={case['size']} n={case['n']} ===")
    print(f"prompt: {case['prompt']}")
    t0 = time.time()
    try:
        resp = post_images({"model": "dall-e-3", "prompt": case["prompt"], "size": case["size"], "n": case["n"]})
    except Exception as e:
        print(f"  请求失败: {e}")
        results.append({**case, "error": str(e)})
        continue
    elapsed = time.time() - t0
    if "error" in resp:
        print(f"  服务端错误: {resp['error']}")
        results.append({**case, "error": resp["error"]})
        continue
    print(f"  耗时 {elapsed:.1f}s，返回 {len(resp['data'])} 张：")
    for j, d in enumerate(resp["data"]):
        url = d["url"]
        st, ct, cl = probe_url(url)
        ok = "OK" if (st == 200 and "image" in ct) else "FAIL"
        print(f"  [{j}] [{ok}] http={st} type={ct} bytes={cl}")
        print(f"      {url}")
        results.append({**case, "index": j, "url": url, "http": st, "type": ct, "bytes": cl})
    time.sleep(3)

print("\n" + "=" * 70)
print("汇总")
print("=" * 70)
ok_n = sum(1 for r in results if r.get("http") == 200)
print(f"成功 {ok_n}/{len(results)} 张")
for r in results:
    if "url" in r:
        print(f"  [{r['tag']} #{r.get('index',0)}] {r['type']} {r['bytes']}B  {r['url'][:80]}...")
    else:
        print(f"  [{r['tag']}] 失败: {str(r.get('error'))[:60]}")

with open("/Users/betterme/PycharmProjects/AI/reverse/hunyuan/capture/image_multi_test_result.json", "w") as f:
    json.dump(results, f, ensure_ascii=False, indent=2)
print("\n结果已存 capture/image_multi_test_result.json")
