FROM python:3.13-slim

# 构建期代理：Chromium 是个上百 MB 的下载，国内构建机上基本必须走它。
# 只在 docker build 阶段生效，不影响容器运行时访问 chatgpt.com 的出口
# （那个由 SENTINEL_SOLVER_PROXY 控制）。
ARG HTTP_PROXY
ARG HTTPS_PROXY
ARG http_proxy
ARG https_proxy

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

COPY requirements.txt ./
# 只装完整 Chromium（新版无头模式）：headless shell 过不了 Cloudflare。
# tzdata: SENTINEL_SOLVER_TIMEZONE 是 IANA 名，缺 /usr/share/zoneinfo 时页面时区
# 会退回 UTC，和主服务上报的时区就对不上了。
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps --no-shell chromium \
    && apt-get update && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY app.py vm.js ./

EXPOSE 8080
CMD ["python", "app.py"]
