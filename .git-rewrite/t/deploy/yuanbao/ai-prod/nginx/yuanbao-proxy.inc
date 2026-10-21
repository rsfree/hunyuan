# yuanbao-service 反代（回环 39177；服务侧 YUANBAO_API_KEY 鉴权）
proxy_pass http://127.0.0.1:39177;
proxy_http_version 1.1;
proxy_set_header Host              $host;
proxy_set_header X-Real-IP         $remote_addr;
proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
proxy_set_header X-Forwarded-Proto $scheme;
proxy_buffering off;          # 聊天/生图耗时长，关缓冲
proxy_read_timeout 300s;
proxy_send_timeout 300s;
client_max_body_size 64m;     # 图进图出（base64 放大 ~33%）
