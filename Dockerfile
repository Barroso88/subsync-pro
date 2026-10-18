# syntax=docker/dockerfile:1.7
# ---- 1) frontend ------------------------------------------------------------
# o frontend compila-se sempre na plataforma do construtor (não em emulação QEMU): o resultado (HTML/JS) é igual para amd64 e arm64
FROM --platform=$BUILDPLATFORM node:22-alpine AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci --no-audit --no-fund || npm install --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# ---- 2) runtime ---------------------------------------------------------------
FROM python:3.12-slim AS runtime
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 \
    SUBSYNC_DATA_DIR=/data SUBSYNC_STATIC_DIR=/app/static SUBSYNC_ALLOWED_ROOTS=/media PORT=8484
# ffprobe (vem no pacote ffmpeg); sem recomendados para manter a imagem pequena
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg tini \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY backend/requirements.txt .
RUN pip install -r requirements.txt
COPY backend/app ./app
COPY --from=web /web/dist ./static
# /data é o único volume de escrita da aplicação; o utilizador real é definido com `user:` (Unraid: 99:100)
RUN mkdir -p /data && chown 99:100 /data && chmod 775 /data
USER 99:100
VOLUME ["/data"]
EXPOSE 8484
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,os;urllib.request.urlopen('http://127.0.0.1:%s/api/health'%os.environ.get('PORT','8484'),timeout=4)" || exit 1
ENTRYPOINT ["/usr/bin/tini","--"]
CMD ["sh","-c","exec uvicorn app.main:app_factory --factory --host 0.0.0.0 --port ${PORT} --no-server-header"]
