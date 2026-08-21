FROM node:22-bookworm-slim AS web-build

WORKDIR /app/web

COPY web/package.json web/package-lock.json ./
RUN npm ci

COPY web/ ./
RUN npm run build


FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/data/home \
    TRACEABLE_RAG_DATABASE_PATH=/data/traceable_v3.db \
    TRACEABLE_RAG_MANAGED_STORAGE_ROOT=/data/managed \
    TRACEABLE_RAG_SETTINGS_PATH=/data/settings.json \
    TRACEABLE_RAG_STATIC_DIR=/app/web/dist

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        ca-certificates \
        curl \
        fonts-noto-cjk \
        libreoffice-writer \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY traceable_rag/ ./traceable_rag/
COPY integrations/ ./integrations/

RUN python -m pip install --no-cache-dir . \
    && groupadd --gid 10001 traceable-rag \
    && useradd \
        --uid 10001 \
        --gid 10001 \
        --home-dir /data/home \
        --create-home \
        --shell /usr/sbin/nologin \
        traceable-rag \
    && mkdir -p /data/managed /app/web/dist \
    && chown -R traceable-rag:traceable-rag /data /app

COPY --from=web-build --chown=traceable-rag:traceable-rag /app/web/dist/ ./web/dist/

USER traceable-rag

VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl --fail --silent --show-error http://127.0.0.1:8000/readyz || exit 1

CMD ["python", "-m", "uvicorn", "traceable_rag.web_app:app", "--host", "0.0.0.0", "--port", "8000"]
