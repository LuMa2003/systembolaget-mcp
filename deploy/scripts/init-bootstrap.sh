#!/usr/bin/env bash
# One-shot bootstrap that runs before sb-mcp / sb-sync-scheduler come up.
# - ensures /data subdirs exist
# - applies pending schema migrations (idempotent)
# - seeds home stores from SB_STORE_SUBSET on first-run

set -euo pipefail

: "${SB_DATA_DIR:=/data}"

mkdir -p \
    "${SB_DATA_DIR}" \
    "${SB_DATA_DIR}/raw" \
    "${SB_DATA_DIR}/backup" \
    "${SB_DATA_DIR}/backup/pre-migration" \
    "${SB_DATA_DIR}/models" \
    "${SB_DATA_DIR}/logs" \
    "${SB_DATA_DIR}/state" \
    "${SB_DATA_DIR}/duckdb_extensions"

echo "[init-bootstrap] applying migrations…"
sb-stack migrate

# Bootstrap is idempotent — the CLI itself handles the "first run or not" check.
echo "[init-bootstrap] seeding home stores…"
sb-stack bootstrap || true
