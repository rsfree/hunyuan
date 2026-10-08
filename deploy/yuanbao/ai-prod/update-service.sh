#!/usr/bin/env bash
# update-service.sh — 元宝代理「安全更新」：备份 → 构建 → 滚动重启 → 验证
#
# 为什么不能直接 docker compose up -d --build：
#   1) 更新前后不备份 profile —— 一旦 chromium 起不来/profile 损坏，登录态就白丢了
#   2) 不验证就宣布成功 —— 起得来 ≠ 登录态还在、保活还通
#
# 用法：
#   ./update-service.sh                 # 更新 acc01（默认，项目名 build，用 .env）
#   ./update-service.sh acc02           # 更新 acc02（项目名 acc02，用 .env.acc02）
#   ./update-service.sh all             # 依次更新所有账号（逐台，串行 —— 天然滚动，不进水的号继续服务）
#   SKIP_BACKUP=1 ./update-service.sh   # 跳过备份（不推荐）
set -uo pipefail

ROOT="${YB_ROOT:-/opt/yuanbao/build}"
BACKUP_DIR="${YB_BACKUP_DIR:-/opt/yuanbao/backup}"
KEEP="${YB_BACKUP_KEEP:-10}"          # 每个账号保留最近 N 份备份

log() { printf '\033[1;36m[update]\033[0m %s\n' "$*"; }
err() { printf '\033[1;31m[update]\033[0m %s\n' "$*" >&2; }

# 账号 -> (compose 参数, 数据目录, 端口)
compose_args() {
  local acc="$1"
  if [ "$acc" = "acc01" ] && [ -f "$ROOT/.env" ]; then
    echo "--env-file .env -p build"
  else
    echo "--env-file .env.$acc -p $acc"
  fi
}

env_get() {  # env_get <envfile> <KEY>
  [ -f "$1" ] || return 0
  sed -n "s/^$2=//p" "$1" | tail -1
}

accounts() {
  if [ "$1" = "all" ]; then
    cd "$ROOT" || exit 1
    for f in .env .env.*; do
      [ -f "$f" ] || continue
      case "$f" in .env.example|.env.bak) continue;; esac
      [ "$f" = ".env" ] && echo acc01 || echo "${f#.env.}"
    done | sort -u
  else
    echo "$1"
  fi
}

backup_one() {
  local acc="$1"
  [ "${SKIP_BACKUP:-0}" = "1" ] && { log "跳过备份（SKIP_BACKUP=1）"; return 0; }
  local src="$ROOT/auths/$acc"
  if [ ! -d "$src" ]; then log "无 $src，跳过备份"; return 0; fi
  local dst="$BACKUP_DIR/$acc-$(date +%Y%m%d-%H%M%S)"
  log "备份 profile: $src → $dst"
  cp -a "$src" "$dst" || { err "备份失败，中止（不冒险更新）"; return 1; }
  # 只留最近 KEEP 份
  ls -1dt "$BACKUP_DIR"/"$acc"-* 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm -rf
  return 0
}

update_one() {
  local acc="$1"
  cd "$ROOT" || return 1
  local args; args="$(compose_args "$acc")"
  local envf=".env"; [ "$acc" = "acc01" ] || envf=".env.$acc"
  if [ ! -f "$ROOT/$envf" ]; then err "$acc: 缺少 $envf"; return 1; fi
  local port; port="$(env_get "$ROOT/$envf" YB_PORT)"; port="${port:-39177}"
  local key;  key="$(env_get "$ROOT/$envf" YUANBAO_API_KEY)"
  local name; name="$(env_get "$ROOT/$envf" YB_INSTANCE_NAME)"; name="${name:-$acc}"

  log "=== 更新 $acc（容器 yuanbao-$name, 端口 $port）==="
  backup_one "$acc" || return 1

  # 注入构建版本（git describe 优先，退回短 SHA），便于部署后 /admin/version 核对
  local build_ver
  if [ -n "${YB_BUILD_VERSION:-}" ]; then
    build_ver="$YB_BUILD_VERSION"          # 显式传入优先（部署目录通常不是 git 仓库）
  else
    build_ver="$(git -C "$ROOT" describe --tags --always --dirty 2>/dev/null || echo unknown)"
  fi
  export YB_BUILD_VERSION="$build_ver"
  log "构建版本：$build_ver"

  log "构建镜像 ..."
  # shellcheck disable=SC2086
  docker compose $args build || { err "构建失败"; return 1; }

  log "滚动重启 ..."
  # shellcheck disable=SC2086
  docker compose $args up -d || { err "启动失败"; return 1; }

  log "等待容器 + chromium 预热 ..."
  local ok=0 i
  for i in $(seq 1 30); do
    sleep 4
    code=$(curl -s -o /dev/null -m 10 -w '%{http_code}' \
      -H "Authorization: Bearer $key" "http://127.0.0.1:$port/v1/models" || true)
    [ "$code" = "200" ] && { ok=1; break; }
  done
  if [ "$ok" != "1" ]; then
    err "$acc 未就绪（/v1/models 最后返回 $code）"
    err "回滚：docker compose $args down && cp -a $BACKUP_DIR/$acc-<最新> $ROOT/auths/$acc && docker compose $args up -d"
    return 1
  fi

  log "校验登录态 + 保活 ..."
  state=$(curl -s -m 90 -H "Authorization: Bearer $key" "http://127.0.0.1:$port/admin/state" || true)
  if printf '%s' "$state" | grep -q '"logged_in":true'; then
    log "✓ 已登录"
    printf '%s' "$state" | grep -q '"frozen":true' && err "⚠️ 该号已被冻结"
  else
    err "✗ 未登录（需重新扫码 / 接码登录）"
  fi
  ka=$(curl -s -m 120 -X POST -H "Authorization: Bearer $key" "http://127.0.0.1:$port/admin/keepalive" || true)
  log "保活: $ka"
  log "线上版本: $(curl -s -m 15 "http://127.0.0.1:$port/admin/version" || echo '(取不到)')"
  printf '%s' "$ka" | grep -q '"ok":true' || err "⚠️ 保活未通过（cookie 可能已失效）"
  log "=== $acc 更新完成 ==="
}

main() {
  local target="${1:-acc01}"
  local rc=0
  for a in $(accounts "$target"); do
    update_one "$a" || rc=1
  done
  exit $rc
}

main "$@"
