# syntax=docker/dockerfile:1
# Carbon Copy worker: polls SQS and runs one job at a time. It drives the HOST's Docker
# daemon (socket mounted) to start copies (app + postgres + localstack + mailpit), tests them
# with Playwright Chromium, and gates changes with OPA, Checkov and Terraform.
#
#   docker buildx build --platform linux/arm64 -f docker/worker.Dockerfile -t ccopy-worker .
#   docker run --network host --group-add $(getent group docker | cut -d: -f3) \
#     -v /var/run/docker.sock:/var/run/docker.sock -v /var/lib/ccopy:/var/lib/ccopy \
#     -e CCOPY_DATA_DIR=/var/lib/ccopy ccopy-worker
#
# --network host: copies publish on 127.0.0.1 of the host. The data dir must be mounted at
# the same path as on the host because compose bind mounts are resolved by the host daemon.

ARG PYTHON_IMAGE=python:3.13-slim-bookworm
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.12.5
ARG NODE_IMAGE=node:24-bookworm-slim
ARG DOCKER_CLI_IMAGE=docker:29-cli
ARG TERRAFORM_IMAGE=hashicorp/terraform:1.16.4
ARG OPA_IMAGE=openpolicyagent/opa:1.21.0-static
ARG CHECKOV_VERSION=3.3.19
ARG CLAUDE_CODE_VERSION=2.1.282

FROM ${UV_IMAGE} AS uv
FROM ${DOCKER_CLI_IMAGE} AS dockercli
FROM ${TERRAFORM_IMAGE} AS terraform
FROM ${OPA_IMAGE} AS opa

# ---------------------------------------------------------------- Claude Code CLI (claude-agent-sdk drives it)
# Built for the target platform: the npm package pulls a native per-arch binary.
FROM ${NODE_IMAGE} AS claude
ARG CLAUDE_CODE_VERSION
RUN --mount=type=cache,target=/root/.npm \
    npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
    && claude --version

# ---------------------------------------------------------------- checkov (own venv: its pins must not fight the app's)
FROM ${PYTHON_IMAGE} AS checkov
ARG CHECKOV_VERSION
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
RUN --mount=type=cache,target=/root/.cache/uv \
    uv venv /opt/checkov && uv pip install --python /opt/checkov/bin/python "checkov==${CHECKOV_VERSION}"

# ---------------------------------------------------------------- app
FROM ${PYTHON_IMAGE} AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app
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
LABEL org.opencontainers.image.title="carboncopy-worker" \
      org.opencontainers.image.source="https://github.com/techadnank9/shipit"

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl git openssh-client \
    && rm -rf /var/lib/apt/lists/*

# Tools, all static binaries or self-contained trees.
COPY --from=dockercli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=dockercli /usr/local/libexec/docker/cli-plugins/docker-compose /usr/local/libexec/docker/cli-plugins/docker-compose
COPY --from=dockercli /usr/local/libexec/docker/cli-plugins/docker-buildx /usr/local/libexec/docker/cli-plugins/docker-buildx
COPY --from=terraform /bin/terraform /usr/local/bin/terraform
COPY --from=opa /opa /usr/local/bin/opa
COPY --from=claude /usr/local/bin/node /usr/local/bin/node
COPY --from=claude /usr/local/lib/node_modules/@anthropic-ai /usr/local/lib/node_modules/@anthropic-ai
COPY --from=checkov /opt/checkov /opt/checkov
RUN ln -s /usr/local/lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe /usr/local/bin/claude \
    && ln -s /opt/checkov/bin/checkov /usr/local/bin/checkov

COPY --from=build /app /app
ENV PATH=/app/.venv/bin:$PATH \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PYTHONUNBUFFERED=1

# Headless Chromium shell + its system libraries, matching the locked playwright version.
RUN playwright install --with-deps --only-shell chromium \
    && rm -rf /var/lib/apt/lists/* \
    && chmod -R a+rX /ms-playwright

# Sanity check every tool the engine shells out to.
RUN docker --version && docker compose version && terraform version && opa version \
    && checkov --version && claude --version && node --version

COPY --chmod=0755 docker/bootstrap.py /usr/local/bin/ccopy-bootstrap

# Non-root; talks to the host daemon via the socket (add the host's docker GID at run time
# with --group-add). HOME must be writable: Claude Code keeps session state there.
# The base image's pip is never used at runtime (uv builds /app/.venv); its vendored
# msgpack/setuptools only add CVEs.
RUN rm -rf /usr/local/lib/python3.13/site-packages/pip /usr/local/lib/python3.13/site-packages/pip-* /usr/local/bin/pip*
RUN useradd --uid 10001 --user-group --create-home --shell /usr/sbin/nologin ccopy \
    && install -d -o 10001 -g 10001 /var/lib/ccopy
ENV HOME=/home/ccopy \
    CCOPY_DATA_DIR=/var/lib/ccopy \
    DO_NOT_TRACK=1 \
    DISABLE_AUTOUPDATER=1
WORKDIR /app
USER 10001

HEALTHCHECK --interval=60s --timeout=5s --start-period=30s \
  CMD ["docker", "version", "--format", "{{.Server.Version}}"]

ENTRYPOINT ["python", "/usr/local/bin/ccopy-bootstrap"]
CMD ["python", "-m", "carboncopy.server.worker"]
