#!/usr/bin/env python3
"""
cookie_minimizer.py — 元宝 cookie 串最小化器

用法：
  1. 浏览器打开 yuanbao.tencent.com → F12 → Network → 随便点一个 /api/ 请求
     → Request Headers → 复制完整 Cookie 头的值（一整行）
  2. 存到 /tmp/yb_cookie.txt（或直接改下面 COOKIE_FILE 指向的文件）
  3. python3 cookie_minimizer.py

流程：
  A. 基线：完整 cookie + 页内铸新鲜签名 → POST /api/user/agent/conversation/create，期望 200
     （基线不过 = cookie 失效 / 或与签名设备不匹配，直接报告，不进入精简）
  B. 逐对剔除：每次尝试去掉一对 cookie，仍 200 就永久剔除；一轮稳定后再扫一遍
  C. 输出最小集 + 验证（用最小集真实发一条聊天）
"""
import json
import subprocess
import sys
import time
import urllib.request
import urllib.error

COOKIE_FILE = "/tmp/yb_cookie.txt"
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
    "Origin": "https://yuanbao.tencent.com",
    "Referer": f"https://yuanbao.tencent.com/chat/{AGENT_ID}",
    "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36",
}
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 直连


def mint_trio():
    """借元宝页面铸新鲜签名三件套（自动找/建会话）。"""
    def ev(expr, sid, tab, timeout=120):
        r = subprocess.run(['bsk', 'evaluate', expr, '--json', '--tab-id', tab, '--session', sid],
                           capture_output=True, text=True, timeout=timeout)
        o = r.stdout.strip()
        i = o.find('{')
        j = json.loads(o[i:])
        if 'value' not in j:
            raise RuntimeError('evaluate 失败: ' + o[:200])
        return j['value']

    out = subprocess.run(['bsk', 'session', 'list'], capture_output=True, text=True).stdout
    sid = None
    for line in out.splitlines()[1:]:
        p = line.split()
        if len(p) >= 3:
            sid = p[0]
            break
    if not sid:
        r = subprocess.run(['bsk', 'session', 'start', '--json', '--no-focus'], capture_output=True, text=True)
        sid = json.loads(r.stdout[r.stdout.index('{'):])['session_id']
        time.sleep(2)
    out2 = subprocess.run(['bsk', 'tab', 'list', '--session', sid], capture_output=True, text=True).stdout
    tab = None
    for line in out2.splitlines():
        p = line.split()
        if len(p) >= 5 and p[1] == 'agent':
            tab = p[0]
            break
    href = ev('location.href', sid, tab)
    if 'yuanbao' not in href:
        subprocess.run(['bsk', 'navigate', 'https://yuanbao.tencent.com/chat', '--session', sid, '--tab-id', tab],
                       capture_output=True)
        time.sleep(5)
    expr = '''
(() => {
  const arr = Object.keys(window).filter(k=>k.startsWith('webpackChunk')).map(k=>window[k])[0];
  let req; arr.push([['min'+Date.now()], {}, (r)=>{req=r}]);
  const modSig = req(77004); const modHdr = req(28850);
  const sig = modHdr.TE(modSig.PU);
  return {uskey: sig['X-Uskey'], md5: sig['X-Bus-Params-Md5'].toString(), ts: String(sig['X-Timestamp'])};
})()
'''
    return ev(expr, sid, tab)


def try_create(cookie: str, sig: dict):
    """POST conversation/create → (http_status, body_head)。"""
    headers = dict(STATIC)
    headers['Cookie'] = cookie
    headers.update({'X-Uskey': sig['uskey'], 'X-Bus-Params-Md5': sig['md5'], 'X-Timestamp': sig['ts']})
    req = urllib.request.Request(
        "https://yuanbao.tencent.com/api/user/agent/conversation/create",
        data=json.dumps({"agentId": AGENT_ID}).encode(), headers=headers, method='POST')
    try:
        with OPENER.open(req, timeout=30) as r:
            return r.status, r.read().decode()[:150]
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:150]


def main():
    cookie = open(COOKIE_FILE).read().strip()
    pairs = [p.strip() for p in cookie.split(';') if p.strip()]
    print(f"载入 {len(pairs)} 对 cookie: {[p.split('=')[0] for p in pairs]}")

    sig = mint_trio()
    print(f"签名已铸 (ts={sig['ts']})")

    st, body = try_create(cookie, sig)
    print(f"基线(完整串): {st} {body}")
    if st != 200:
        print("\n❌ 基线不过：cookie 可能失效，或与签名设备不匹配（cookie 来自其他设备时 uskey 的 h38 对不上）。")
        print("   → 确认从本机已登录的元宝浏览器 profile 复制，或重新登录后重试。")
        sys.exit(1)

    minted_at = time.time()
    current = list(pairs)

    def attempt(lst):
        nonlocal sig, minted_at
        if time.time() - minted_at > 90:  # 签名可能过期，重铸
            sig = mint_trio()
            minted_at = time.time()
        return try_create("; ".join(lst), sig)

    # 两轮剔除（第一轮剔除后可能暴露新的可剔除项）
    for round_no in (1, 2):
        print(f"\n--- 第 {round_no} 轮剔除（当前 {len(current)} 对）---")
        i = 0
        removed = []
        while i < len(current):
            candidate = current[:i] + current[i+1:]
            st, _ = attempt(candidate)
            name = current[i].split('=')[0]
            if st == 200:
                removed.append(name)
                current = candidate
                print(f"  剔除 {name:32s} ✓ (剩 {len(current)})")
            else:
                print(f"  保留 {name:32s} ✗ ({st})")
                i += 1
        if not removed:
            break

    print(f"\n=== 最小集（{len(current)} 对）===")
    minimal = "; ".join(current)
    print(minimal)

    st, body = try_create(minimal, sig)
    print(f"\n复验 conversation/create: {st} {body}")
    open('/tmp/yb_cookie_minimal.txt', 'w').write(minimal)
    print("最小集已存 /tmp/yb_cookie_minimal.txt")


if __name__ == "__main__":
    main()
