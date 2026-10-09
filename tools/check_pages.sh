#!/usr/bin/env bash
# check_pages.sh — 校验内嵌页面里的 JS 语法（每次改 HTML/JS 后跑一遍）
#
# 为什么需要：这些页面是 Python 里的 triple-quoted 字符串，往里写 `\n` 会被 Python
# 解析成**真换行**，把 JS 字符串截断 ⇒ 页面整段脚本挂掉（SyntaxError），且服务端不报错。
# 用 node --check 逐个 <script> 过一遍，能在部署前拦住。
set -euo pipefail
PY="${PY:-/usr/bin/python3}"
NODE="${NODE:-$(command -v node || echo /opt/homebrew/bin/node)}"
"$PY" - "$NODE" <<'PY'
import re, subprocess, sys, tempfile, os
node = sys.argv[1]
src = open('yuanbao_openai_proxy.py', encoding='utf-8').read()
bad = 0
for n in ['MANAGE_HTML', 'STATS_HTML', 'QR_HTML', 'ADMIN_HTML', 'NAV_HTML']:
    a = src.index(n + ' = """'); b = src.index('"""', a + len(n) + 6)
    html = src[a + len(n) + 6:b]
    html = html.encode('utf-8').decode('unicode_escape').encode('latin-1', 'ignore').decode('utf-8', 'ignore')
    for i, js in enumerate(re.findall(r'<script>(.*?)</script>', html, re.S)):
        f = tempfile.mktemp(suffix='.js'); open(f, 'w').write(js)
        r = subprocess.run([node, '--check', f], capture_output=True, text=True); os.remove(f)
        if r.returncode:
            bad += 1
            print("!! %s script#%d: %s" % (n, i, r.stderr.strip().splitlines()[:2]))
print("内嵌 JS 语法: %s" % ("通过 ✓" if not bad else "失败 ✗ (%d)" % bad))
sys.exit(1 if bad else 0)
PY
