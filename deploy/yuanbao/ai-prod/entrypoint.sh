#!/bin/bash
# 同容器：chromium（常驻预热，供签名铸造）+ 代理（可多 worker，共享同一 chromium）
PROXY_ARGS=""
[ -n "${YB_PROXY_URL:-}" ] && PROXY_ARGS="--proxy-server=${YB_PROXY_URL}"
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
