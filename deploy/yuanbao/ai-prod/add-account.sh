#!/usr/bin/env bash
# add-account.sh — 新增一个元宝账号实例（一号一容器一 profile）
#
# 设计（与"轮询在 new-api 做"的分工一致）：
#   本服务 = 一个实例承载一个账号；多号 = 多实例；**轮询/故障转移由 new-api 渠道负责**。
#   每个实例：独立端口 + 独立 chrome-profile + 独立 API key，共享同一个镜像。
#
# 用法：
#   ./add-account.sh acc02 39178 yuanbao2.1task.cn
#     acc02           账号/实例名
#     39178           宿主机回环端口（容器内固定 39177）
#     yuanbao2.1task.cn  可选：对外域名（不填则只给回环地址）
#
# 之后手动三步（脚本会把命令打出来）：
#   1) 起容器 → 打开 /qr 扫码登录
#   2) nginx 加 vhost + DNS A 记录 + 签证书
#   3) new-api 里把该实例加成一个渠道（Base URL + 该实例的 API key）
set -uo pipefail

ROOT="${YB_ROOT:-/opt/yuanbao/build}"
NAME="${1:-}"
PORT="${2:-}"
DOMAIN="${3:-}"

log() { printf '\033[1;36m[add]\033[0m %s\n' "$*"; }
err() { printf '\033[1;31m[add]\033[0m %s\n' "$*" >&2; }

[ -n "$NAME" ] || { err "用法: $0 <实例名> [端口] [域名]   （端口可省略，自动取下一个）"; exit 1; }
case "$NAME" in acc*) ;; *) err "实例名建议用 accNN（如 acc02）"; exit 1;; esac
[ -d "$ROOT" ] || { err "找不到 $ROOT"; exit 1; }
# 共享网络：跨实例的管理/聚合都靠它
docker network inspect yuanbao-net >/dev/null 2>&1 || docker network create yuanbao-net >/dev/null
cd "$ROOT"

# 端口可省略：按注册表现有条数自动往后排（39177, 39178, ...）
if [ -z "$PORT" ]; then
  PORT=$(python3 -c "
import json, os
p = os.path.join('$ROOT', '..', 'pool.json')
try:
    n = len(json.load(open(p)).get('peers', []))
except Exception:
    n = 0
print(39177 + n)
")
  log "未指定端口，自动分配 $PORT"
fi

ENVF=".env.$NAME"
[ -f "$ENVF" ] && { err "$ENVF 已存在，别重复添加"; exit 1; }
# 端口占用检查
if ss -lntp 2>/dev/null | grep -q "127.0.0.1:$PORT "; then err "端口 $PORT 已被占用"; exit 1; fi

# 1) 目录
log "创建 auths/$NAME/chrome-profile"
mkdir -p "auths/$NAME/chrome-profile"
: > "auths/$NAME/cookie.txt"

# 2) 复用 acc01 的共享密钥（代理池/fleet key），只换实例自身的变量
src=".env"; [ -f "$src" ] || src=".env.acc01"
[ -f "$src" ] || { err "缺少 $src（先跑通 acc01）"; exit 1; }
log "从 $src 继承共享配置（代理池 / fleet key / 保活间隔）"
{
  echo "# $NAME —— 由 add-account.sh 生成"
  echo "YUANBAO_API_KEY=sk-yuanbao-$(openssl rand -hex 16)"
  grep -E '^(YB_FLEET_KEY|YB_KEEPALIVE_SEC|YB_PROXY_POOL|YB_BROWSER_PROXY)=' "$src" || true
  echo "YB_INSTANCE_NAME=$NAME"
  echo "YB_PORT=$PORT"
  echo "YB_BASE_URL=${DOMAIN:+https://$DOMAIN}"
  echo "YB_POOL_PEERS=$(sed -n 's/^YB_POOL_PEERS=//p' "$src" | tail -1)"
  echo "YB_DATA_PLANE=page"
  echo "YB_WORKERS=1"
} > "$ENVF"
chmod 600 "$ENVF"

# 3) 登记进共享注册表 pool.json（各实例按 mtime 热重载，**无需重启任何实例**）
log "登记到号池注册表 $ROOT/../pool.json"
python3 - "$ROOT" "$NAME" "$PORT" "$DOMAIN" <<'PY'
import json, os, sys
root, name, port, domain = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
path = os.path.join(os.path.dirname(os.path.abspath(root)), "pool.json")
url = ("https://%s" % domain) if domain else ("http://yuanbao-%s:39177" % name)
try:
    data = json.load(open(path, encoding="utf-8"))
except Exception:
    data = {"peers": []}
peers = data.setdefault("peers", [])
peers = [p for p in peers if p.get("name") != name]
peers.append({"name": name, "url": url, "port": port})
data["peers"] = sorted(peers, key=lambda x: x.get("name", ""))
os.makedirs(os.path.dirname(path), exist_ok=True)
json.dump(data, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print("  注册表现有:", ", ".join(p["name"] for p in data["peers"]))
PY

# 4) 直接起容器（不必再手敲 compose 命令）
log "启动容器 yuanbao-$NAME ..."
docker compose --env-file "$ENVF" -p "$NAME" up -d 2>&1 | tail -2

NEWKEY="$(sed -n 's/^YUANBAO_API_KEY=//p' "$ENVF" | tail -1)"
cat <<EOF

============================================================
实例 $NAME 已生成
  数据目录 : $ROOT/auths/$NAME/chrome-profile
  配置     : $ROOT/$ENVF
  端口     : 127.0.0.1:$PORT
  API Key  : $NEWKEY

下一步（逐条执行）：

1) 扫码登录这个号（用刚生成的 key）：
   https://yuanbao.1task.cn/qr?k=$NEWKEY
   —— 若还没配域名，先用 ssh 隧道：
   ssh -L 9102:127.0.0.1:$PORT root@<本机IP> 然后开 http://127.0.0.1:9102/qr?k=$NEWKEY

2) 看整池状态（任一实例都能看）：
   curl -s -H "Authorization: Bearer <任一实例key>" http://127.0.0.1:$PORT/admin/pool

3) 接入公网（可选，域名 $DOMAIN）：
   - 阿里云 DNS 加 A 记录 $DOMAIN → 本机公网 IP
   - 复制 nginx/yuanbao.1task.cn.conf 改 ServerName 与 proxy_pass 端口为 $PORT
   - 跑 nginx/apply-https.sh 签证书

4) new-api 里加渠道（轮询/故障转移在这里做）：
   Base URL: https://${DOMAIN:-<域名>}/v1     API Key: $NEWKEY
============================================================
EOF
