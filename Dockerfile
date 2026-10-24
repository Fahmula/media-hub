# syntax=docker/dockerfile:1.7
# MediaHub: multi-stage build. No Node.js: CSS is built with the Tailwind standalone CLI.
ARG PYTHON_VERSION=3.13

# ---------- 1. CSS (Tailwind v4 + daisyUI 5, pinned and checksum-verified) ----------
FROM debian:bookworm-slim AS css
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /build
COPY scripts/build-css.sh scripts/build-css.sh
COPY src/mediahub/static src/mediahub/static
COPY src/mediahub/templates src/mediahub/templates
COPY src/mediahub/web src/mediahub/web
RUN sh scripts/build-css.sh

# ---------- 2. Python dependencies + app (uv, locked) ----------
FROM python:${PYTHON_VERSION}-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.12.24 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /src
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev --no-install-project
COPY src ./src
COPY --from=css /build/src/mediahub/static/dist ./src/mediahub/static/dist
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev --no-editable

# ---------- 3. Runtime ----------
FROM python:${PYTHON_VERSION}-slim
LABEL org.opencontainers.image.title="MediaHub" \
      org.opencontainers.image.description="Self-hosted media monitoring for Emby, Sonarr, Radarr and InfiniDysk" \
      org.opencontainers.image.licenses="MIT"

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MEDIAHUB_CONFIG_DIR=/config \
    MEDIAHUB_PORT=8080 \
    PUID=99 \
    PGID=100 \
    UMASK=022

COPY --from=builder /app/.venv /app/.venv
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod 0755 /usr/local/bin/entrypoint.sh && mkdir -p /config

VOLUME ["/config"]
EXPOSE 8080
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('MEDIAHUB_PORT', '8080'), timeout=4)"]

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["mediahub"]
