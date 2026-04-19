# syntax=docker/dockerfile:1.7
#
# sb-stack — single-container stack with supervised sb-embed / sb-mcp /
# sb-sync-scheduler services behind s6-overlay.
#
# Single-stage build on the pytorch runtime — a two-stage layout would
# have to copy the .venv across images whose Python executables live at
# different paths, which breaks the venv's absolute symlinks. The pip +
# uv layer caches still make rebuilds fast, and the `--no-install-recommends`
# apt invocation keeps the layer lean.
#
# See docs/12_packaging.md for the full rationale.

FROM pytorch/pytorch:2.5.1-cuda12.4-cudnn9-runtime

ARG S6_OVERLAY_VERSION=3.2.0.2

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    \
    SB_DATA_DIR=/data \
    HF_HOME=/data/models/hf \
    TRANSFORMERS_CACHE=/data/models/hf \
    HUGGINGFACE_HUB_CACHE=/data/models/hf \
    \
    TZ=Europe/Stockholm \
    \
    S6_KEEP_ENV=1 \
    S6_VERBOSITY=1 \
    S6_KILL_GRACETIME=30000 \
    S6_SERVICES_GRACETIME=5000 \
    S6_CMD_WAIT_FOR_SERVICES_MAXTIME=1800000

# System packages we need (s6 install uses curl + xz-utils).
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates xz-utils tini tzdata \
    && ln -fs /usr/share/zoneinfo/Europe/Stockholm /etc/localtime \
    && rm -rf /var/lib/apt/lists/*

# s6-overlay (supervisor).
RUN curl -fsSL -o /tmp/s6-noarch.tar.xz \
        "https://github.com/just-containers/s6-overlay/releases/download/v${S6_OVERLAY_VERSION}/s6-overlay-noarch.tar.xz" \
 && curl -fsSL -o /tmp/s6-x86_64.tar.xz \
        "https://github.com/just-containers/s6-overlay/releases/download/v${S6_OVERLAY_VERSION}/s6-overlay-x86_64.tar.xz" \
 && tar -C / -Jxpf /tmp/s6-noarch.tar.xz \
 && tar -C / -Jxpf /tmp/s6-x86_64.tar.xz \
 && rm /tmp/s6-*.tar.xz

# uv — pinned to match the host-side version.
RUN pip install --no-cache-dir uv==0.11.7

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# Install runtime deps first (cache-friendly layer).
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# Then the project itself. pyproject references README.md as the readme
# (hatchling reads it at install time).
COPY src/ ./src/
COPY README.md LICENSE ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH"

# s6 service definitions.
COPY deploy/s6-rc.d /etc/s6-overlay/s6-rc.d/
COPY deploy/scripts/ /app/deploy/scripts/

RUN find /etc/s6-overlay/s6-rc.d -type f \( -name run -o -name up -o -name finish \) \
        -exec chmod +x {} + \
 && chmod +x /app/deploy/scripts/*.sh

EXPOSE 8000

# First boot downloads the model (~8 GB); give the doctor subset a
# generous start_period before it starts failing the container.
HEALTHCHECK --interval=5m --timeout=30s --start-period=30m --retries=3 \
    CMD sb-stack doctor \
        --only db_reachable,disk_space,embed_service_reachable \
        --exit-on-warn

ENTRYPOINT ["/init"]
