FROM mcr.microsoft.com/playwright/python:v1.49.0-jammy

RUN pip install --no-cache-dir fastapi==0.115.6 "uvicorn[standard]==0.34.0" websocket-client==1.8.0

WORKDIR /app
COPY yuanbao_openai_proxy.py cdp_minter.py mint_backends.py ./
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
