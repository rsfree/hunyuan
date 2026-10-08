#!/usr/bin/env python3
"""
token_monitor.py — 元宝最小凭证（hy_token+hy_user）存活监控

每 CHECK_INTERVAL 秒用最小 cookie + 静态头 GET /api/info/general 探活（零副作用、
无需签名、无需浏览器）：
  200 → 存活；401 → 过期告警（写日志 + macOS 通知）。

用法：python3 token_monitor.py &   （或 nohup 挂后台）
Cookie 来源：/tmp/yb_cookie_minimal.txt（过期后更新该文件即可，监控自动用新的）
"""
import json
import time
import subprocess
import urllib.request
import urllib.error
import os

COOKIE_FILE = "/tmp/yb_cookie_minimal.txt"
LOG_FILE = "/tmp/yb_token_monitor.log"
CHECK_INTERVAL = int(os.environ.get("YB_CHECK_INTERVAL", "1800"))  # 默认 30 分钟
AGENT_ID = "naQivTmsDa"

STATIC = {
    "X-Input-Type": "text", "X-Requested-With": "XMLHttpRequest", "X-Instance-ID": "5",
    "X-Source": "web", "X-Language": "zh-CN",
    "X-device-id": "19c100d220910063ab8e4f54c0cba26e4d7c4bd2b8",
    "X-HY106": "", "X-HY92": "e9632faf082420cd40bb971703000001419610",
    "X-HY93": "19c100d220910063ab8e4f54c0cba26e4d7c4bd2b8",
    "X-os_version": "Mac OS(10.15.7)-Blink", "X-Platform": "mac",
    "X-webdriver": "0", "X-ybuitest": "0", "X-Exp-Params": "enableNewPcStyle=2",
    "x-web-ch-id": "null", "X-Web-Third-Source": "main", "x-commit-tag": "02746073",
    "X-WebVersion": "2.87.2", "X-AgentID": AGENT_ID,
    "content-type": "text/plain;charset=UTF-8",
    "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36",
}
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def log(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def notify(msg):
    subprocess.run(["osascript", "-e", f'display notification "{msg}" with title "元宝凭证监控"'],
                   capture_output=True)


def mint_trio():
    """借元宝页面铸签名（依赖本机 bsk + 浏览器）。"""
    out = subprocess.run(["bsk", "session", "list"], capture_output=True, text=True).stdout
    sid = None
    for line in out.splitlines()[1:]:
        p = line.split()
        if len(p) >= 3:
            sid = p[0]
            break
    if not sid:
        r = subprocess.run(["bsk", "session", "start", "--json", "--no-focus"], capture_output=True, text=True)
        sid = json.loads(r.stdout[r.stdout.index("{"):])["session_id"]
        time.sleep(2)
    out2 = subprocess.run(["bsk", "tab", "list", "--session", sid], capture_output=True, text=True).stdout
    tab = None
    for line in out2.splitlines():
        p = line.split()
        if len(p) >= 5 and p[1] == "agent":
            tab = p[0]
            break
    r = subprocess.run(["bsk", "evaluate", "location.href", "--json", "--tab-id", tab, "--session", sid],
                       capture_output=True, text=True)
    o = r.stdout
    href = json.loads(o[o.index("{"):]).get("value", "")
    if "yuanbao" not in href:
        subprocess.run(["bsk", "navigate", "https://yuanbao.tencent.com/chat", "--session", sid, "--tab-id", tab],
                       capture_output=True)
        time.sleep(5)
    expr = '''
(() => {
  const arr = Object.keys(window).filter(k=>k.startsWith('webpackChunk')).map(k=>window[k])[0];
  let req; arr.push([['mon'+Date.now()], {}, (r)=>{req=r}]);
  const modSig = req(77004); const modHdr = req(28850);
  const sig = modHdr.TE(modSig.PU);
  return {uskey: sig['X-Uskey'], md5: sig['X-Bus-Params-Md5'].toString(), ts: String(sig['X-Timestamp'])};
})()
'''
    r = subprocess.run(["bsk", "evaluate", expr, "--json", "--tab-id", tab, "--session", sid],
                       capture_output=True, text=True)
    o = r.stdout
    return json.loads(o[o.index("{"):])["value"]


def check(cookie):
    """探活：GET /api/info/general（静态头即可，无签名 → 完全去浏览器）"""
    headers = dict(STATIC)
    headers["Cookie"] = cookie
    req = urllib.request.Request("https://yuanbao.tencent.com/api/info/general",
                                 headers=headers, method="GET")
    try:
        with OPENER.open(req, timeout=30) as r:
            return r.status, r.read().decode()[:80]
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:80]


def main():
    log(f"监控启动，间隔 {CHECK_INTERVAL}s，cookie 文件 {COOKIE_FILE}")
    last_status = None
    while True:
        try:
            if not os.path.exists(COOKIE_FILE):
                log(f"缺少 {COOKIE_FILE}，跳过")
                time.sleep(CHECK_INTERVAL)
                continue
            cookie = open(COOKIE_FILE).read().strip()
            st, body = check(cookie)
            if st == 200:
                if last_status == 401:
                    log("凭证恢复 ✓（文件已更新？）")
                    notify("凭证恢复存活")
                else:
                    log(f"存活 ✓ {body}")
                last_status = 200
            elif st == 401:
                if last_status != 401:  # 首次 401 才告警
                    log(f"⚠️ 凭证过期 (401) {body} —— 需重新登录元宝并更新 {COOKIE_FILE}")
                    notify("元宝凭证已过期！请重新抓取 cookie")
                last_status = 401
            else:
                log(f"异常 {st} {body}")
        except Exception as e:
            log(f"监控异常: {e}")
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
