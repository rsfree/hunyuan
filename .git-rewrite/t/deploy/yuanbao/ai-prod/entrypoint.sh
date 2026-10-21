#!/bin/bash
# 同容器：chromium（常驻，供铸签 + 页面数据面）+ API 进程
#
# 🔴 为什么必须优雅关闭：
#   Chromium 的 Cookies 是 SQLite 文件，登录态刷新后并非立刻落盘。若容器停止时直接
#   SIGKILL，最近的 cookie 变更可能丢 —— 这正是"更新/重启服务后掉登录"的常见原因。
#   所以这里不用 exec 把 PID1 让给 python，而是自己当 PID1 接 SIGTERM：
#   先停 API，再给 chromium 发 SIGTERM 等它收尾落盘。
set -m

PROFILE_DIR="${CHROME_PROFILE_DIR:-/data/chrome-profile}"
CHROME="${CHROME_BIN:-/ms-playwright/chromium-1148/chrome-linux/chrome}"

# 清陈旧 profile 锁：容器重建后 hostname 变化，Chromium 会误判"profile 被另一台电脑占用"而拒绝启动
rm -f "$PROFILE_DIR"/SingletonLock "$PROFILE_DIR"/SingletonSocket "$PROFILE_DIR"/SingletonCookie

# 浏览器侧代理：YB_BROWSER_PROXY（带认证 socks5h）→ 本地 socks 桥（无认证）→ chromium
# chromium --proxy-server 不支持 URL 内认证，故经桥中转
PROXY_ARGS=""
BRIDGE_PID=""
if [ -n "${YB_BROWSER_PROXY:-}" ]; then
  BRIDGE_UPSTREAM="${YB_BROWSER_PROXY}" BRIDGE_LISTEN="127.0.0.1:1080" python socks_bridge.py &
  BRIDGE_PID=$!
  sleep 1.5
  PROXY_ARGS="--proxy-server=socks5://127.0.0.1:1080"
fi

"$CHROME" \
  --headless=new --no-sandbox --disable-dev-shm-usage --disable-gpu \
  --ignore-certificate-errors --remote-debugging-port=9222 \
  --disable-blink-features=AutomationControlled \
  --lang=zh-CN --window-size=1440,900 \
  $PROXY_ARGS \
  --user-data-dir="$PROFILE_DIR" about:blank &
CHROME_PID=$!

WORKERS="${YB_WORKERS:-1}"
if [ "$WORKERS" -gt 1 ]; then
  python -m uvicorn yuanbao_openai_proxy:app \
    --host "${YB_BIND:-127.0.0.1}" --port "${YUANBAO_PROXY_PORT:-39177}" \
    --workers "$WORKERS" --timeout-keep-alive 300 &
else
  python yuanbao_openai_proxy.py &
fi
APP_PID=$!

stopping=0
shutdown() {
  [ "$stopping" = "1" ] && return 0
  stopping=1
  echo "[entrypoint] graceful shutdown: 停 API → chromium 优雅退出（等 cookie 落盘）"
  kill -TERM "$APP_PID" 2>/dev/null
  for _ in $(seq 1 8); do kill -0 "$APP_PID" 2>/dev/null || break; sleep 0.5; done
  kill -KILL "$APP_PID" 2>/dev/null
  # 关键一步：SIGTERM 让 chromium 正常收尾并把 cookie/localStorage flush 到 profile
  kill -TERM "$CHROME_PID" 2>/dev/null
  for _ in $(seq 1 20); do kill -0 "$CHROME_PID" 2>/dev/null || break; sleep 0.5; done
  kill -KILL "$CHROME_PID" 2>/dev/null
  [ -n "$BRIDGE_PID" ] && kill -TERM "$BRIDGE_PID" 2>/dev/null
  echo "[entrypoint] shutdown done"
  exit 0
}
trap shutdown TERM INT

# 任一子进程退出 → 整体收尾（避免半死不活：chromium 挂了 API 还在傻跑）
while :; do
  if ! kill -0 "$APP_PID" 2>/dev/null; then
    echo "[entrypoint] API 进程已退出"
    shutdown
  fi
  if ! kill -0 "$CHROME_PID" 2>/dev/null; then
    echo "[entrypoint] chromium 进程已退出"
    shutdown
  fi
  sleep 1
done
