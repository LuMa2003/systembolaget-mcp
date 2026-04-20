# TrueNAS Scale deployment

Images are built by `.github/workflows/docker.yaml` on every push to
`main` and released tag, pushed to **ghcr.io/luma2003/systembolaget-mcp**.
Tags: `latest`, `main`, `sha-<short>`, and semver `vX.Y.Z` on releases.

**One-time setup:** in GitHub → repo → Packages → `systembolaget-mcp` →
package settings, flip visibility to **public** so TrueNAS can pull
without needing a PAT.

1. Create a dataset for persistent state, e.g. `/mnt/tank/sb-data`.
2. Ensure the nvidia-container-toolkit is installed and GPU passthrough works.
3. From the TrueNAS UI → Apps → **Install Custom App** → paste the YAML at
   `../../docker-compose.yaml`, with `image: ghcr.io/luma2003/systembolaget-mcp:latest`.
   Override the following env vars:
    - `SB_MCP_TOKEN` — generate with `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
    - `SB_STORE_SUBSET` / `SB_MAIN_STORE` — your home stores.
    - `SB_API_KEY_MOBILE` — default cfc702… works today; override with a fresh
      mobile-app capture if `/sb-api-mobile/*` starts 401'ing (you'll get an
      ntfy alert when that happens).
    - (optional) `SB_NTFY_URL` for push notifications.
4. Mount `/mnt/tank/sb-data → /data`.
5. Expose port 8000 on the LAN.
6. First boot downloads Qwen3-Embedding-4B (~8 GB) to `/data/models/hf` and
   triggers the initial full sync (~50 minutes). Subsequent runs are ~3 min.

**Updating:** re-deploy the app — TrueNAS will pull whatever tag your
compose points at. `:latest` auto-rolls on every main commit; pin to
a `:vX.Y.Z` tag if you want explicit control.

See `docs/07_deployment.md` for rationale + `docs/12_packaging.md` for the
Dockerfile / s6 layout.
