# ---- 前端构建阶段 ----
FROM node:22-alpine AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# ---- 运行阶段 ----
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY keyport/ ./keyport/
COPY scripts/ ./scripts/
COPY config.example.json ./config.example.json
COPY login.sh ./login.sh
RUN chmod +x ./login.sh

# 前端静态导出产物（由 FastAPI 托管）
COPY --from=web /web/out ./frontend/out

# 非 root 运行；数据与凭证目录由卷挂载
RUN useradd -u 10001 -m -s /bin/bash app \
    && mkdir -p /app/data /app/auths \
    && chown -R 10001:10001 /app
USER 10001

EXPOSE 8790

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8790/healthz || exit 1

CMD ["python", "-m", "keyport", "--host", "0.0.0.0", "--port", "8790"]
