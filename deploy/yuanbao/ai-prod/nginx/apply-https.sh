#!/usr/bin/env bash
# yuanbao-service · ai-prod 域名入口一键落地（幂等；DNS 未就绪会拒绝执行）
#
# 用法（在 ai-prod 上跑，root）：
#   bash /opt/baidu/apply-https.sh            # 默认 baidu.1task.cn
#   HOST=baidu.livetest.cn bash .../apply-https.sh
#
# 它做什么：DNS 预检 → acme.sh 签发（HTTP-01 webroot）→ install-cert（含续期 reloadcmd）
#          → 投 :443 vhost → nginx -t + reload → 打印验证矩阵
set -euo pipefail

HOST="${HOST:-yuanbao.1task.cn}"
IP_EXPECT="${IP_EXPECT:-210.121.44.245}"
VHOST_DIR=/www/server/panel/vhost/nginx
CERT_DIR="/www/server/panel/vhost/cert/$HOST"
ACME=/root/.acme.sh/acme.sh
NGINX=/www/server/nginx/sbin/nginx

echo "== 1) DNS 预检：$HOST 必须已指向 $IP_EXPECT =="
# 预检走**多解析器**：权威 NS 最准（无缓存），公共解析器兜底（阿里云解析器有自己的缓存窗口）
GOT=""
for R in dns13.hichina.com dns14.hichina.com 8.8.8.8 1.1.1.1 223.5.5.5; do
  GOT=$(dig +short "$HOST" @"$R" 2>/dev/null | grep -E '^[0-9]+\.' | head -1 || true)
  [ "$GOT" = "$IP_EXPECT" ] && break
done
if [ "$GOT" != "$IP_EXPECT" ]; then
  echo "❌ DNS 未就绪：$HOST -> '${GOT:-<空>}'（期望 $IP_EXPECT）—— 先在万网加 A 记录，再跑本脚本"
  exit 2
fi
echo "✅ $HOST -> $GOT"

echo "== 2) ACME 通道自检（挑战文件名必须能经本 vhost 取回）=="
mkdir -p /www/wwwroot/acme/.well-known/acme-challenge
echo "preflight-$$" > /www/wwwroot/acme/.well-known/acme-challenge/preflight
CODE=$(curl -s -o /dev/null -w '%{http_code}' -H "Host: $HOST" http://127.0.0.1/.well-known/acme-challenge/preflight)
[ "$CODE" = "200" ] || { echo "❌ 挑战路径返回 $CODE（应 200）—— 检查 $VHOST_DIR/$HOST.conf 的 ^~ location"; exit 3; }
echo "✅ 挑战路径 200"

echo "== 3) 签发证书（已存在则跳过）=="
if [ -d "/root/.acme.sh/${HOST}_ecc" ]; then
  echo "· 证书已存在，跳过签发"
else
  "$ACME" --issue -d "$HOST" --webroot /www/wwwroot/acme --keylength ec-256
fi
mkdir -p "$CERT_DIR"
"$ACME" --install-cert -d "$HOST" --ecc \
  --key-file       "$CERT_DIR/privkey.pem" \
  --fullchain-file "$CERT_DIR/fullchain.pem" \
  --reloadcmd      "$NGINX -s reload"
echo "✅ 证书就位：$CERT_DIR"

echo "== 4) 投 :443 vhost 并 reload（幂等）=="
SSL_CONF="$VHOST_DIR/$HOST-ssl.conf"
if [ ! -f "$SSL_CONF" ]; then
  SRC="/opt/yuanbao/nginx/$HOST-ssl.conf"
  [ -f "$SRC" ] || SRC="/tmp/yuanbao-ingress/$HOST-ssl.conf"
  sed "s/yuanbao\.1task\.cn/$HOST/g" "$SRC" > "$SSL_CONF"
  echo "· 已写入 $SSL_CONF"
else
  echo "· $SSL_CONF 已存在，跳过"
fi
"$NGINX" -t && "$NGINX" -s reload
echo "✅ nginx reloaded"

echo "== 5) 验证矩阵 =="
KEY=$(grep ^YUANBAO_API_KEY /opt/yuanbao/build/.env | cut -d= -f2)
printf '%-34s %s\n' ":80 → :443 301" "$(curl -s -o /dev/null -w '%{http_code}' -H "Host: $HOST" http://127.0.0.1/v1/models)"
R="--resolve $HOST:443:127.0.0.1"
printf '%-34s %s\n' ":443 无Key 应 401" "$(curl -s -o /dev/null -w '%{http_code}' $R https://$HOST/v1/models)"
printf '%-34s %s\n' ":443 带Key /v1/models 应 200" "$(curl -s -o /dev/null -w '%{http_code}' $R -H "Authorization: Bearer $KEY" https://$HOST/v1/models)"
printf '%-34s %s\n' ":443 /login 截图（cdp 扫码入口）" "$(curl -s -o /dev/null -w '%{http_code} %{content_type}' $R https://$HOST/login)"
printf '%-34s %s\n' ":443 E2E chat（带Key）" "$(curl -s $R -m 120 -o /tmp/e2e_chat.json -w '%{http_code}' -X POST https://$HOST/v1/chat/completions -H "Authorization: Bearer $KEY" -H 'content-type: application/json' -d '{"model":"hunyuan","messages":[{"role":"user","content":"回复两个字：域名"}]}')"
printf '%-34s %s\n' "E2E 回复内容" "$(python3 -c "import json;print(json.load(open('/tmp/e2e_chat.json'))['choices'][0]['message']['content'])" 2>/dev/null || echo '解析失败')"
printf '%-34s %s\n' "证书链校验（不加 -k）" "$(curl -s -o /dev/null -w '%{http_code} ssl_verify=%{ssl_verify_result}' $R https://$HOST/v1/models)"
printf '%-34s %s\n' "未登记 Host 应 404" "$(curl -s -o /dev/null -w '%{http_code}' -H 'Host: nope.example.com' http://127.0.0.1/v1/models)"
echo
echo "证书：$(openssl x509 -in "$CERT_DIR/fullchain.pem" -noout -subject -dates 2>/dev/null | tr '\n' ' ')"
echo "✅ 完成：https://$HOST"
