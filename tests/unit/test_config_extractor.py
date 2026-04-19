"""Unit tests for the NEXT_PUBLIC_* config extractor.

respx mocks the frontend; we craft HTML + a fake chunk bundle that
carries every NEXT_PUBLIC_* we care about, plus a distractor chunk so
the parallel-find code path is actually exercised.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from sb_stack.api_client.config_extractor import extract_config
from sb_stack.errors import ConfigExtractionError

APP = "https://www.systembolaget.se"

HOMEPAGE_HTML = """
<!doctype html>
<html><body>
<script src="/_next/static/chunks/webpack.abc123.js"></script>
<script src="/_next/static/chunks/main.def456.js"></script>
<script src="/_next/static/chunks/pages_config.1234567890abcdef.js"></script>
<script>
self.__BUILD_MANIFEST = {
  "/": ["static/chunks/main.def456.js"],
  "/sortiment": ["static/chunks/sortiment.xyz.js"]
};
</script>
</body></html>
"""

KEY_CHUNK_JS = """
(window.__INLINE__=[])
({"NEXT_PUBLIC_API_KEY_APIM":"8d39a7340ee7439f8b4c1e995c8f3e4a",
  "NEXT_PUBLIC_API_MANAGEMENT_URL":"https://api-extern.systembolaget.se",
  "NEXT_PUBLIC_APP_IMAGE_STORAGE_URL":"https://product-cdn.systembolaget.se/productimages",
  "NEXT_PUBLIC_CMS_URL":"https://cms.systembolaget.se",
  "NEXT_PUBLIC_APP_BASE_URL":"https://www.systembolaget.se"});
"""

# Distractor chunk: looks like JS, but no NEXT_PUBLIC_API_KEY_APIM.
DECOY_CHUNK_JS = "window.__x=function(){return 42};"


@respx.mock
async def test_extract_returns_parsed_config() -> None:
    # Register specific routes BEFORE the homepage catch-all — respx
    # matches in registration order and treats a bare host as a base URL.
    respx.get(f"{APP}/_next/static/chunks/webpack.abc123.js").mock(
        return_value=httpx.Response(200, text=DECOY_CHUNK_JS)
    )
    respx.get(f"{APP}/_next/static/chunks/main.def456.js").mock(
        return_value=httpx.Response(200, text=DECOY_CHUNK_JS)
    )
    respx.get(f"{APP}/_next/static/chunks/pages_config.1234567890abcdef.js").mock(
        return_value=httpx.Response(200, text=KEY_CHUNK_JS)
    )
    respx.get(f"{APP}/_next/static/chunks/sortiment.xyz.js").mock(return_value=httpx.Response(404))
    respx.get(APP).mock(return_value=httpx.Response(200, text=HOMEPAGE_HTML))

    cfg = await extract_config(app_base_url=APP)

    assert cfg.api_key == "8d39a7340ee7439f8b4c1e995c8f3e4a"
    assert cfg.api_management_url == "https://api-extern.systembolaget.se"
    assert cfg.app_image_storage_url == ("https://product-cdn.systembolaget.se/productimages")
    assert cfg.cms_url == "https://cms.systembolaget.se"
    assert cfg.app_base_url == "https://www.systembolaget.se"
    assert "NEXT_PUBLIC_API_KEY_APIM" in cfg.raw


@respx.mock
async def test_homepage_failure_raises() -> None:
    respx.get(APP).mock(return_value=httpx.Response(502))
    with pytest.raises(ConfigExtractionError, match="homepage"):
        await extract_config(app_base_url=APP)


@respx.mock
async def test_no_chunks_on_homepage_raises() -> None:
    respx.get(APP).mock(return_value=httpx.Response(200, text="<html></html>"))
    with pytest.raises(ConfigExtractionError, match="no NEXT"):
        await extract_config(app_base_url=APP)


@respx.mock
async def test_no_chunk_contains_key_raises() -> None:
    # Register specific routes first so they match before the homepage mock.
    for name in (
        "webpack.abc123.js",
        "main.def456.js",
        "pages_config.1234567890abcdef.js",
        "sortiment.xyz.js",
    ):
        respx.get(f"{APP}/_next/static/chunks/{name}").mock(
            return_value=httpx.Response(200, text=DECOY_CHUNK_JS)
        )
    respx.get(APP).mock(return_value=httpx.Response(200, text=HOMEPAGE_HTML))

    with pytest.raises(ConfigExtractionError, match="not found"):
        await extract_config(app_base_url=APP)
