#!/usr/bin/env bash
# Polls sb-embed's /health until it reports status=ok or we hit a timeout.
# Downstream longruns (sb-mcp, sb-sync-scheduler) wait on this oneshot so
# they don't start hitting /v1/embeddings before the model is loaded.

set -euo pipefail

: "${SB_EMBED_URL:=http://localhost:9000/v1/embeddings}"
# Derive /health from /v1/embeddings.
EMBED_HEALTH="${SB_EMBED_URL%/v1/embeddings}"
EMBED_HEALTH="${EMBED_HEALTH%/embeddings}"
EMBED_HEALTH="${EMBED_HEALTH}/health"

TIMEOUT_SECONDS="${SB_EMBED_WAIT_TIMEOUT_SECONDS:-600}"
INTERVAL_SECONDS=5
elapsed=0

echo "[wait-for-embed] polling ${EMBED_HEALTH} (timeout ${TIMEOUT_SECONDS}s)"
while : ; do
    if body="$(curl -fsS --max-time 3 "${EMBED_HEALTH}" 2>/dev/null)"; then
        if echo "${body}" | grep -q '"status":"ok"'; then
            echo "[wait-for-embed] ready after ${elapsed}s"
            exit 0
        fi
    fi
    if [ "${elapsed}" -ge "${TIMEOUT_SECONDS}" ]; then
        echo "[wait-for-embed] giving up after ${elapsed}s; sb-embed not ready"
        exit 1
    fi
    sleep "${INTERVAL_SECONDS}"
    elapsed=$((elapsed + INTERVAL_SECONDS))
done
