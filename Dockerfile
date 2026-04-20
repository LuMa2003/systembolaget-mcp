# syntax=docker/dockerfile:1.7
#
# sb-stack — single-container stack with supervised sb-embed / sb-mcp /
# sb-sync-scheduler services behind s6-overlay.
#
# Based on nvidia/cuda:12.4 runtime (CUDA toolkit only — torch's pip
# package ships its own nvidia-cudnn-cu12, so we don't need cuDNN in the
# base). uv manages both the Python interpreter and every package,
# including torch from PyTorch's CUDA-12.4 wheel index. One package
# manager, one venv, ~14 GB vs 18 GB for the old pytorch/pytorch layout.
#
# See docs/12_packaging.md for the full rationale.

FROM nvidia/cuda:12.4.1-runtime-ubuntu22.04

ARG S6_OVERLAY_VERSION=3.2.0.2

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DEBIAN_FRONTEND=noninteractive \
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

# System packages: curl + xz-utils for fetching uv / s6-overlay, tini for
# a clean PID-1 fallback (s6 is the real PID 1), tzdata for Stockholm.
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

# uv — installed via the Astral standalone installer since the CUDA base
# has no Python/pip to pip-install it from.
ENV UV_INSTALL_DIR=/usr/local/bin \
    UV_UNMANAGED_INSTALL=1 \
    UV_VERSION=0.11.7
RUN curl -LsSf https://astral.sh/uv/install.sh | sh

# Let uv manage the Python interpreter (downloads CPython 3.12 into
# /root/.local/share/uv/python on first sync) and the venv.
ENV UV_PYTHON=3.12 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

# Install runtime deps first (cache-friendly layer). torch comes from
# the pytorch-cu124 index declared in pyproject.toml.
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
