# TrueNAS Scale deployment

1. Create a dataset for persistent state, e.g. `/mnt/tank/sb-data`.
2. Ensure the nvidia-container-toolkit is installed and GPU passthrough works.
3. From the TrueNAS UI → Apps → **Install Custom App** → paste the YAML at
   `../../docker-compose.yaml`. Override the following env vars:
    - `SB_MCP_TOKEN` — generate with `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
    - `SB_STORE_SUBSET` / `SB_MAIN_STORE` — your home stores.
    - (optional) `SB_NTFY_URL` for push notifications.
4. Mount `/mnt/tank/sb-data → /data`.
5. Expose port 8000 on the LAN.
6. First boot downloads Qwen3-Embedding-4B (~8 GB) to `/data/models/hf` and
   triggers the initial full sync (~50 minutes). Subsequent runs are ~3 min.

See `docs/07_deployment.md` for rationale + `docs/12_packaging.md` for the
Dockerfile / s6 layout.
