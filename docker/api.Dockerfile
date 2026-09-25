# syntax=docker/dockerfile:1
# Carbon Copy API: FastAPI (carboncopy.server.app:app) + the static dashboard (web/out).
# Runs anywhere as a plain HTTP server on :8080, and on AWS Lambda through the Lambda Web
# Adapter extension (no code changes: the adapter turns Lambda events into HTTP requests).
#
#   docker buildx build --platform linux/arm64 -f docker/api.Dockerfile -t ccopy-api .
#   docker run -p 8080:8080 ccopy-api

ARG PYTHON_IMAGE=python:3.13-slim-bookworm
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.12.5
ARG NODE_IMAGE=node:24-bookworm-slim
ARG LWA_IMAGE=public.ecr.aws/awsguru/aws-lambda-adapter:1.1.0

FROM ${UV_IMAGE} AS uv
FROM ${LWA_IMAGE} AS lwa

# ---------------------------------------------------------------- dashboard
# Static export is architecture-independent, so build it on the native build platform.
FROM --platform=$BUILDPLATFORM ${NODE_IMAGE} AS web
WORKDIR /src
COPY . .
RUN --mount=type=cache,target=/root/.npm \
    if [ -f web/package.json ]; then \
      cd web && { [ -f package-lock.json ] && npm ci || npm install; } \
      && NEXT_PUBLIC_API="" npm run build && test -f out/index.html; \
    else \
      echo "web/ not present yet: shipping a placeholder dashboard"; \
      mkdir -p web/out && printf '%s\n' '<!doctype html><title>Carbon Copy</title><p>Dashboard not built.</p>' > web/out/index.html; \
    fi

# ---------------------------------------------------------------- python deps
FROM ${PYTHON_IMAGE} AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app
# Dependencies first (cached until uv.lock changes), then the project itself.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-dev --no-install-project
COPY pyproject.toml uv.lock ./
COPY carboncopy ./carboncopy
COPY policies ./policies
COPY sample-app ./sample-app
# Editable install on purpose: the engine finds policies/ relative to the source tree.
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev

# ---------------------------------------------------------------- runtime
FROM ${PYTHON_IMAGE}
LABEL org.opencontainers.image.title="carboncopy-api" \
      org.opencontainers.image.source="https://github.com/techadnank9/carboncopy"

COPY --from=lwa /lambda-adapter /opt/extensions/lambda-adapter
# git: creating a run snapshots the repo into a git workspace (and clones git_url projects).
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN useradd --uid 10001 --user-group --create-home --shell /usr/sbin/nologin ccopy
COPY --from=build /app /app
COPY --from=web /src/web/out /app/web/out
COPY --chmod=0755 docker/bootstrap.py /usr/local/bin/ccopy-bootstrap

# Lambda mounts everything read-only except /tmp and runs as its own uid, so all state goes
# to /tmp locally too and nothing needs to be writable in the image.
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/tmp \
    CCOPY_DATA_DIR=/tmp/ccopy \
    PORT=8080 \
    AWS_LWA_PORT=8080 \
    AWS_LWA_READINESS_CHECK_PATH=/api/health \
    AWS_LWA_ASYNC_INIT=true

WORKDIR /app
USER 10001
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=2).status == 200 else 1)"]

ENTRYPOINT ["python", "/usr/local/bin/ccopy-bootstrap"]
CMD ["uvicorn", "carboncopy.server.app:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers", "--forwarded-allow-ips", "*"]
