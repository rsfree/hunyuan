FROM mcr.microsoft.com/playwright/python:v1.49.0-jammy

# python-multipart：/v1/images/edits 走 multipart/form-data 上传参考图（Starlette req.form() 依赖它）
RUN pip install --no-cache-dir fastapi==0.115.6 "uvicorn[standard]==0.34.0" websocket-client==1.8.0 \
    "requests[socks]==2.32.3" python-multipart==0.0.20

WORKDIR /app
COPY yuanbao_openai_proxy.py cdp_minter.py mint_backends.py socks_bridge.py metrics.py pool_state.py accounts.py guard.py ./
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV YB_MINT_BACKEND=cdp \
    CDP_HTTP=http://127.0.0.1:9222 \
    CHROME_BIN=/ms-playwright/chromium-1148/chrome-linux/chrome \
    YB_COOKIE_FILE=/auths/cookie.txt \
    YB_BIND=0.0.0.0 \
    YB_DATA_PLANE=cookie \
    YUANBAO_PROXY_PORT=39177

EXPOSE 39177
ENTRYPOINT ["/entrypoint.sh"]
