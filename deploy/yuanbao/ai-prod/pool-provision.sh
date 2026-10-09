#!/usr/bin/env bash
# pool-provision.sh — 号池「自动加号」执行器（**跑在宿主机**，由 systemd 常驻）
#
# 为什么要这样分层：
#   代理容器里**没有也不该有** docker socket。所以页面只做一件事——往共享目录写一个"意向文件"，
#   特权动作（建目录 / 起容器 / 改注册表）全部由本脚本以宿主机 root 身份完成。
#   ⇒ 页面侧无法执行任意命令，只有白名单动作 + 严格名字校验（^acc[0-9]{1,3}$）。
#
# 约定：
#   /opt/yuanbao/requests/<id>.json   页面写入：{"id","action":"add","name":"acc03"}
#   /opt/yuanbao/results/<id>.json    本脚本写回：{"id","status":"running|done|error","name","log"}
set -uo pipefail

ROOT="${YB_ROOT:-/opt/yuanbao/build}"
REQ="${YB_REQ_DIR:-/opt/yuanbao/requests}"
RES="${YB_RES_DIR:-/opt/yuanbao/results}"
KEEP="${YB_RES_KEEP:-50}"

mkdir -p "$REQ" "$RES"
log() { echo "[provision $(date '+%H:%M:%S')] $*"; }

write_result() {  # write_result <id> <status> <name> [logfile]
  python3 - "$1" "$2" "$3" "${4:-}" "$RES" <<'PY'
import json, os, sys
rid, status, name, lf, res = sys.argv[1:6]
log = ""
if lf and os.path.exists(lf):
    with open(lf, encoding="utf-8", errors="ignore") as f:
        log = "".join(f.readlines()[-60:])[-4000:]
p = os.path.join(res, rid + ".json")
with open(p + ".tmp", "w", encoding="utf-8") as f:
    json.dump({"id": rid, "status": status, "name": name, "log": log}, f, ensure_ascii=False)
os.replace(p + ".tmp", p)
PY
}

next_name() {  # 按注册表现有条数取下一个 accNN（acc01/acc02 → acc03）
  python3 - "$ROOT" <<'PY'
import json, os, sys
root = sys.argv[1]
p = os.path.join(os.path.dirname(os.path.abspath(root)), "pool.json")
used = set()
try:
    for x in json.load(open(p, encoding="utf-8")).get("peers", []):
        used.add(x.get("name"))
except Exception:
    pass
n = 1
while ("acc%02d" % n) in used:
    n += 1
print("acc%02d" % n)
PY
}

jget() { python3 -c "import json,sys;print(json.load(open(sys.argv[1])).get(sys.argv[2],''))" "$1" "$2" 2>/dev/null; }

handle() {  # handle <request-file>
  local f="$1"
  # 🔴 文件名此刻是 <id>.json.processing —— basename 的".json"后缀剥不掉 ".processing"，
  # 必须两级都剥，否则结果文件会写成 <id>.json.processing.json，页面永远查不到（踩过）
  local id; id="$(basename "$f")"; id="${id%.processing}"; id="${id%.json}"
  local action name
  action="$(jget "$f" action)"
  name="$(jget "$f" name)"

  # 🔴 名字严格白名单：只允许 acc + 最多 3 位数字，杜绝命令注入
  if [ -z "$name" ]; then name="$(next_name)"; fi
  if ! printf '%s' "$name" | grep -Eq '^acc[0-9]{1,3}$'; then
    log "拒绝非法名字: $name"; write_result "$id" "error" "$name"; return 0
  fi
  if [ "$action" != "add" ]; then
    log "未知动作: $action"; write_result "$id" "error" "$name"; return 0
  fi

  log "处理加号请求 id=$id name=$name"
  write_result "$id" "running" "$name"
  local lf="/tmp/ybprovision-$id.log"
  if ( cd "$ROOT" && ./add-account.sh "$name" ) >"$lf" 2>&1; then
    log "✔ $name 创建完成"
    write_result "$id" "done" "$name" "$lf"
  else
    log "✘ $name 创建失败"
    write_result "$id" "error" "$name" "$lf"
  fi
  rm -f "$lf"
  ls -1t "$RES"/*.json 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm -f
}

log "启动，监听 $REQ（ROOT=$ROOT）"
while :; do
  shopt -s nullglob
  for f in "$REQ"/*.json; do
    # 原子改名抢占：避免重复处理与半写文件
    if mv "$f" "$f.processing" 2>/dev/null; then
      handle "$f.processing"
      rm -f "$f.processing"
    fi
  done
  shopt -u nullglob
  sleep 3
done
