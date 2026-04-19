"""Scrape the Systembolaget frontend for its `NEXT_PUBLIC_*` runtime config.

The frontend is Next.js: `NEXT_PUBLIC_*` env vars are inlined into a
static JS chunk at build time. We pull the homepage, discover chunk
URLs, fetch them in parallel, and locate the chunk that embeds the
APIM subscription key. That same chunk carries every other
`NEXT_PUBLIC_*` value, so we extract them all in one pass.

See docs/02_systembolaget_api.md §Key extraction for rationale.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

from sb_stack.errors import ConfigExtractionError

# Property names may appear quoted ("NEXT_PUBLIC_X":"v") or unquoted
# (NEXT_PUBLIC_X:"v") depending on Next.js/webpack minifier output.
_APIM_KEY_RE = re.compile(r'NEXT_PUBLIC_API_KEY_APIM"?\s*:\s*"([0-9a-f]{32})"')
_NEXT_PUBLIC_PAIR_RE = re.compile(r'"?(NEXT_PUBLIC_[A-Z0-9_]+)"?\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)"')
_CHUNK_URL_RE = re.compile(r'"(/_next/static/chunks/[^"]+\.js)"')
_BUILD_MANIFEST_CHUNK_RE = re.compile(r'"(static/chunks/[^"]+\.js)"')

DEFAULT_CHUNK_FETCH_CONCURRENCY = 10
DEFAULT_CHUNK_FETCH_TIMEOUT_S = 15.0


class ExtractedConfig(BaseModel):
    """The subset of `NEXT_PUBLIC_*` values sb-stack actually uses."""

    model_config = ConfigDict(frozen=True)

    api_key: str = Field(..., description="NEXT_PUBLIC_API_KEY_APIM")
    api_management_url: str = Field(..., description="NEXT_PUBLIC_API_MANAGEMENT_URL")
    app_image_storage_url: str | None = Field(None, description="NEXT_PUBLIC_APP_IMAGE_STORAGE_URL")
    cms_url: str | None = Field(None, description="NEXT_PUBLIC_CMS_URL")
    app_base_url: str | None = Field(None, description="NEXT_PUBLIC_APP_BASE_URL")
    # Everything else we spotted — kept for debugging / `extract-key` CLI.
    raw: dict[str, str] = Field(default_factory=dict)


@dataclass
class _ChunkCandidate:
    url: str
    body: str = ""
    errors: list[str] = field(default_factory=list)


async def extract_config(
    *,
    app_base_url: str = "https://www.systembolaget.se",
    http_client: httpx.AsyncClient | None = None,
    chunk_concurrency: int = DEFAULT_CHUNK_FETCH_CONCURRENCY,
    timeout_s: float = DEFAULT_CHUNK_FETCH_TIMEOUT_S,
    logger: Any | None = None,
) -> ExtractedConfig:
    """Scrape + return the frontend's `NEXT_PUBLIC_*` config.

    Raises `ConfigExtractionError` if the homepage can't be parsed or the
    subscription key chunk can't be identified.
    """
    if logger is not None:
        logger.info("api_key_extraction_started")

    owned = http_client is None
    client = http_client or httpx.AsyncClient(timeout=timeout_s, follow_redirects=True)
    try:
        homepage_html = await _fetch_homepage(client, app_base_url)
        chunk_urls = _discover_chunk_urls(homepage_html, app_base_url)
        if not chunk_urls:
            raise ConfigExtractionError(f"no NEXT.js chunks discovered on {app_base_url}")
        chunk = await _find_key_chunk(client, chunk_urls, concurrency=chunk_concurrency)
        if chunk is None:
            raise ConfigExtractionError("inspected all chunks; NEXT_PUBLIC_API_KEY_APIM not found")
        pairs = dict(_NEXT_PUBLIC_PAIR_RE.findall(chunk.body))
        api_key = pairs.get("NEXT_PUBLIC_API_KEY_APIM")
        api_url = pairs.get("NEXT_PUBLIC_API_MANAGEMENT_URL")
        if not api_key or not api_url:
            raise ConfigExtractionError(
                "chunk matched the key regex but was missing the "
                "API management URL — schema drift, refusing to cache"
            )
        result = ExtractedConfig(
            api_key=api_key,
            api_management_url=api_url,
            app_image_storage_url=pairs.get("NEXT_PUBLIC_APP_IMAGE_STORAGE_URL"),
            cms_url=pairs.get("NEXT_PUBLIC_CMS_URL"),
            app_base_url=pairs.get("NEXT_PUBLIC_APP_BASE_URL") or app_base_url,
            raw=pairs,
        )
        if logger is not None:
            logger.info(
                "api_key_extracted",
                key_prefix=api_key[:8],
                source="fresh",
            )
        return result
    finally:
        if owned:
            await client.aclose()


async def _fetch_homepage(client: httpx.AsyncClient, app_base_url: str) -> str:
    try:
        resp = await client.get(app_base_url)
        resp.raise_for_status()
    except httpx.HTTPError as e:  # includes transport, decode, and status errors
        raise ConfigExtractionError(f"homepage fetch failed: {e!r}") from e
    return resp.text


def _discover_chunk_urls(html: str, app_base_url: str) -> list[str]:
    """Collect every `/_next/static/chunks/*.js` URL referenced by the page."""
    urls: set[str] = set()
    for path in _CHUNK_URL_RE.findall(html):
        urls.add(app_base_url.rstrip("/") + path)
    for path in _BUILD_MANIFEST_CHUNK_RE.findall(html):
        urls.add(app_base_url.rstrip("/") + "/_next/" + path)
    return sorted(urls)


async def _find_key_chunk(
    client: httpx.AsyncClient,
    chunk_urls: list[str],
    *,
    concurrency: int,
) -> _ChunkCandidate | None:
    sem = asyncio.Semaphore(concurrency)

    async def _fetch(url: str) -> _ChunkCandidate:
        candidate = _ChunkCandidate(url=url)
        try:
            async with sem:
                resp = await client.get(url)
            if resp.status_code != 200:
                candidate.errors.append(f"status={resp.status_code}")
                return candidate
            candidate.body = resp.text
        except httpx.HTTPError as e:
            candidate.errors.append(repr(e))
        return candidate

    tasks = [asyncio.create_task(_fetch(u)) for u in chunk_urls]
    try:
        for coro in asyncio.as_completed(tasks):
            chunk = await coro
            if chunk.body and _APIM_KEY_RE.search(chunk.body):
                return chunk
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()
    return None
