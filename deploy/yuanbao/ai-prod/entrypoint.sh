#!/bin/bash
# 同容器：chromium（常驻预热，供签名铸造）+ 代理（可多 worker，共享同一 chromium）
# 清陈旧 profile 锁：容器重建后 hostname 变化，Chromium 会误判"profile 被另一台电脑占用"而拒绝启动
rm -f /data/chrome-profile/SingletonLock /data/chrome-profile/SingletonSocket /data/chrome-profile/SingletonCookie

# 浏览器侧代理：YB_BROWSER_PROXY（带认证 socks5h）→ 本地 socks 桥（无认证）→ chromium
# chromium --proxy-server 不支持 URL 内认证，故经桥中转
PROXY_ARGS=""
if [ -n "${YB_BROWSER_PROXY:-}" ]; then
  BRIDGE_UPSTREAM="${YB_BROWSER_PROXY}" BRIDGE_LISTEN="127.0.0.1:1080" python socks_bridge.py &
  sleep 1.5
  PROXY_ARGS="--proxy-server=socks5://127.0.0.1:1080"
fi
/ms-playwright/chromium-1148/chrome-linux/chrome \
  --headless=new --no-sandbox --disable-dev-shm-usage --disable-gpu \
  --ignore-certificate-errors --remote-debugging-port=9222 \
  --disable-blink-features=AutomationControlled \
  --lang=zh-CN --window-size=1440,900 \
  $PROXY_ARGS \
  --user-data-dir=/data/chrome-profile about:blank &
WORKERS="${YB_WORKERS:-1}"
if [ "$WORKERS" -gt 1 ]; then
  exec python -m uvicorn yuanbao_openai_proxy:app \
    --host "${YB_BIND:-127.0.0.1}" --port "${YUANBAO_PROXY_PORT:-39177}" \
    --workers "$WORKERS" --timeout-keep-alive 300
fi
exec python yuanbao_openai_proxy.py
