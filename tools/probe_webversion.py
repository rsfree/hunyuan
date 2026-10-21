"""第三轮：按 URL 分组，看**业务 API** 用的是哪一套版本号。"""
import collections
import json
import time
import urllib.request

import websocket

CDP = "http://127.0.0.1:9222"
OP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def http(path, method="GET"):
    try:
        req = urllib.request.Request(CDP + path, method=method)
        with OP.open(req, timeout=15) as r:
            raw = r.read()
        return json.loads(raw) if raw.strip()[:1] in (b"{", b"[") else {}
    except Exception:
        return {}


tgt = http("/json/new?about:blank", "PUT")
tid = tgt.get("id")
ws = websocket.create_connection(tgt["webSocketDebuggerUrl"], timeout=60, suppress_origin=True)
ws.settimeout(3)
for mid, (method, params) in enumerate([
    ("Network.enable", {}), ("Page.enable", {}),
    ("Page.navigate", {"url": "https://yuanbao.tencent.com/"}),
], start=1):
    ws.send(json.dumps({"id": mid, "method": method, "params": params}))

rows = collections.Counter()
t0 = time.time()
while time.time() - t0 < 30:
    try:
        msg = json.loads(ws.recv())
    except Exception:
        continue
    if msg.get("method") != "Network.requestWillBeSent":
        continue
    req = msg["params"]["request"]
    url = req.get("url", "")
    if "//" not in url:
        continue
    host = url.split("/")[2]
    path = "/" + "/".join(url.split("/")[3:]).split("?")[0]
    pfx = "/".join(path.split("/")[:3]) or path
    h = {k.lower(): v for k, v in (req.get("headers") or {}).items()}
    rows[(host, pfx, h.get("x-webversion", "-"), h.get("x-commit-tag", "-"))] += 1

print("%-28s %-34s %-8s %-10s %s" % ("host", "path前缀", "webver", "commit", "次数"))
for (host, pfx, ver, tag), n in sorted(rows.items(), key=lambda x: -x[1])[:14]:
    if ver != "-" or tag != "-":
        print("%-28s %-34s %-8s %-10s %d" % (host, pfx[:34], ver, tag, n))
try:
    ws.close()
except Exception:
    pass
http("/json/close/" + str(tid))
