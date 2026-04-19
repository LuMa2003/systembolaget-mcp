# syntax=docker/dockerfile:1.7
#
# sb-stack — single-container stack with supervised sb-embed / sb-mcp /
# sb-sync-scheduler services behind s6-overlay.
#
# See docs/12_packaging.md for the full rationale.

# ==============================================================
# Stage 1 — build the project .venv on slim-python via uv
# ==============================================================
FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /usr/local/bin/uv

WORKDIR /build

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_PREFERENCE=only-system

# Cache dependency resolution — project code comes in the next copy.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

COPY src/ ./src/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev


# ==============================================================
# Stage 2 — runtime with CUDA + s6-overlay
# ==============================================================
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
    S6_CMD_WAIT_FOR_SERVICES_MAXTIME=900000

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

WORKDIR /app

# Bring in the built venv + app source.
COPY --from=builder /build/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"

COPY src/ /app/src/
COPY deploy/s6-rc.d /etc/s6-overlay/s6-rc.d/
COPY deploy/scripts/ /app/deploy/scripts/

RUN find /etc/s6-overlay/s6-rc.d -type f \( -name run -o -name up -o -name finish \) \
        -exec chmod +x {} + \
 && chmod +x /app/deploy/scripts/*.sh

EXPOSE 8000

# First boot downloads the model (~8 GB); give the doctor subset a
# generous start_period before it starts failing the container.
HEALTHCHECK --interval=5m --timeout=30s --start-period=15m --retries=3 \
    CMD sb-stack doctor \
        --only db_reachable,disk_space,embed_service_reachable \
        --exit-on-warn

ENTRYPOINT ["/init"]
