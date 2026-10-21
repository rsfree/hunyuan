#!/usr/bin/env bash
# set-egress.sh — 给每个号钉一个**固定且互不相同**的住宅出口 IP
#
# 背景（对代理池的 SOCKS5 端口实测，端口语义如下）：
#   2086  http    每请求换出口
#   2088  socks5h 每请求换出口            ← 我们原来用的，同一 cookie 一分钟跨 N 个 IP
#   2089  socks5h 按目标域名哈希（同域名恒定，但**所有号共用一个** ⇒ 多号同 IP）
#   2090  socks5h **用户名后缀 = 会话键** ⇒ 同后缀恒定同出口，不同后缀出口不同
#   ⇒ 一号一个后缀（poolclient-<acc>）就是"一号一个固定 IP"，这才是打断关联的正解。
#   ⚠️ 不是每个后缀都能分到 IP（实测 yb03 是死槽）⇒ 脚本探测、失败自动换候选。
#
# 写入内容（该号的 .env/.env.accNN）：
#   YB_PROXY_URL     = socks5h://<user>-<acc>:<pass>@<host>:2090   （数据面：聊天/生图出站）
#   YB_BROWSER_PROXY = 同上                                        （浏览器：扫码/铸签，经 socks_bridge）
#   YB_PROXY_POOL    = 空                                           （必须清掉，否则优先用池）
#   ⇒ 数据面与浏览器**共用同一出口**，消灭"登录一个 IP、使用另一个 IP"
#
# 用法：
#   ./set-egress.sh acc01                 # 探测 + 写入 + 重建容器（不重建镜像）
#   ./set-egress.sh all                   # 对 .env / .env.accNN 全部执行
#   ./set-egress.sh acc01 --dry-run       # 只探测打印
#   ./set-egress.sh acc01 --no-restart    # 只写配置
set -uo pipefail

ROOT="${YB_ROOT:-/opt/yuanbao/build}"
STICKY_PORT="${YB_STICKY_PORT:-2090}"
PROBE_URL="${YB_PROBE_URL:-https://api.ipify.org}"

log() { printf '\033[1;36m[egress]\033[0m %s\n' "$*"; }
err() { printf '\033[1;31m[egress]\033[0m %s\n' "$*" >&2; }

envfile_for() { [ "$1" = "acc01" ] && echo ".env" || echo ".env.$1"; }
proj_for()    { [ "$1" = "acc01" ] && echo "build" || echo "$1"; }
env_get()     { sed -n "s/^$2=//p" "$1" 2>/dev/null | tail -1; }

# 去环境代理（本机 HTTP_PROXY 会把探测也代理掉）
unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy

probe_ip() {  # probe_ip <socks_url> → 打印出口 IP，失败打印空
  curl -s -m 25 --proxy "$1" "$PROBE_URL" 2>/dev/null | tr -d '\r\n'
}

set_kv() {  # set_kv <file> <KEY> <VALUE>（值含 @ : 等，用 python 安全改写）
  python3 - "$1" "$2" "$3" <<'PY'
import io, re, sys
path, key, val = sys.argv[1], sys.argv[2], sys.argv[3]
lines = io.open(path, encoding="utf-8").read().split("\n")
out, hit = [], False
for ln in lines:
    if re.match(r"^%s=" % re.escape(key), ln):
        out.append("%s=%s" % (key, val)); hit = True
    else:
        out.append(ln)
if not hit:
    while out and out[-1] == "":
        out.pop()
    out.append("%s=%s" % (key, val))
    out.append("")
io.open(path, "w", encoding="utf-8").write("\n".join(out))
PY
}

# 从现有配置里抠出上游凭据：socks5h://user:pass@host:port
upstream_of() {
  local f="$1" raw
  raw="$(env_get "$f" YB_PROXY_POOL)"
  [ -z "$raw" ] && raw="$(env_get "$f" YB_PROXY_URL)"
  [ -z "$raw" ] && raw="$(env_get "$f" YB_BROWSER_PROXY)"
  printf '%s' "$raw" | cut -d, -f1
}

parse_upstream() {  # $1 -> 输出 "user|pass|host"
  local u="${1#*://}"
  local creds="${u%@*}" hostport="${u##*@}"
  local user="${creds%%:*}" pass="${creds#*:}"
  local host="${hostport%%:*}"
  printf '%s|%s|%s' "$user" "$pass" "$host"
}

do_one() {
  local acc="$1" mode="${2:-apply}"
  local f; f="$(envfile_for "$acc")"
  [ -f "$ROOT/$f" ] || { err "$acc: 缺少 $f"; return 1; }

  local up; up="$(upstream_of "$ROOT/$f")"
  if [ -z "$up" ]; then
    err "$acc: $f 里没有 YB_PROXY_POOL/YB_PROXY_URL/YB_BROWSER_PROXY 可继承上游凭据"
    return 1
  fi
  IFS='|' read -r user pass host <<<"$(parse_upstream "$up")"
  [ -z "$user" ] || [ -z "$host" ] && { err "$acc: 解析上游失败: $up"; return 1; }
  # 去掉可能已存在的会话后缀，避免二次叠加
  local base="${user%%-*}"
  [ "$base" = "poolclient" ] || base="$user"

  local cand suffix url ip1 ip2 chosen_ip=""
  for suffix in "$acc" "${acc}a" "${acc}b" "${acc}2" "${acc}3"; do
    cand="${base}-${suffix}"
    url="socks5h://${cand}:${pass}@${host}:${STICKY_PORT}"
    ip1="$(probe_ip "$url")"
    [ -z "$ip1" ] && { log "  ${cand} → 空槽，试下一个"; continue; }
    sleep 1
    ip2="$(probe_ip "$url")"
    if [ "$ip1" != "$ip2" ]; then
      log "  ${cand} → 不稳定（$ip1 ≠ $ip2），试下一个"; continue
    fi
    chosen_ip="$ip1"; EGRESS_URL="$url"; EGRESS_USER="$cand"
    break
  done

  if [ -z "$chosen_ip" ]; then
    err "$acc: 5 个候选会话键都没拿到稳定 IP（上游可能限流/无可用节点）"
    return 1
  fi

  log "$acc  会话键=${EGRESS_USER}  出口=${chosen_ip}"

  if [ "$mode" = "dry-run" ]; then
    return 0
  fi

  set_kv "$ROOT/$f" YB_PROXY_URL "$EGRESS_URL"
  set_kv "$ROOT/$f" YB_BROWSER_PROXY "$EGRESS_URL"
  set_kv "$ROOT/$f" YB_PROXY_POOL ""
  log "$acc  已写入 $f"

  if [ "$mode" = "no-restart" ]; then
    return 0
  fi
  local proj; proj="$(proj_for "$acc")"
  local args; if [ "$acc" = "acc01" ]; then args="--env-file .env -p build"; else args="--env-file $f -p $proj"; fi
  log "$acc  重建容器（不重建镜像）…"
  # shellcheck disable=SC2086
  ( cd "$ROOT" && docker compose $args up -d --no-build ) >/dev/null || { err "$acc: 重建失败"; return 1; }
  sleep 6
  local port; port="$(env_get "$ROOT/$f" YB_PORT)"; port="${port:-39177}"
  local key;  key="$(env_get "$ROOT/$f" YUANBAO_API_KEY)"
  local got
  got="$(curl -s -m 40 -H "Authorization: Bearer $key" "http://127.0.0.1:${port}/admin/egress" 2>/dev/null)"
  if [ -n "$got" ]; then
    log "$acc  线上校验: $(printf '%s' "$got" | head -c 400)"
  else
    err "$acc  /admin/egress 取不到（容器可能还在启动）"
  fi
}

accounts() {
  if [ "$1" = "all" ]; then
    cd "$ROOT" || return 1
    for f in .env .env.acc*; do
      [ -f "$f" ] || continue
      case "$f" in .env.example|.env.bak) continue;; esac
      [ "$f" = ".env" ] && echo acc01 || echo "${f#.env.}"
    done | sort -u
  else
    echo "$1"
  fi
}

main() {
  local target="${1:-}" mode="apply"
  [ -z "$target" ] && { echo "用法: $0 <acc名|all> [--dry-run|--no-restart]"; exit 1; }
  case "${2:-}" in
    --dry-run)    mode="dry-run";;
    --no-restart) mode="no-restart";;
    "")           ;;
    *) echo "未知参数: $2"; exit 1;;
  esac
  local rc=0
  for a in $(accounts "$target"); do do_one "$a" "$mode" || rc=1; done
  exit $rc
}

main "$@"
