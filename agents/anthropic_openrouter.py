"""Make browser-use's Anthropic requests acceptable to OpenRouter's Messages shim."""
from __future__ import annotations

import json
from typing import Any

import httpx


def _drop_null_cache_control(node: Any) -> Any:
    """Recursively remove `cache_control` keys whose value is null."""
    if isinstance(node, dict):
        return {k: _drop_null_cache_control(v) for k, v in node.items()
                if not (k == "cache_control" and v is None)}
    if isinstance(node, list):
        return [_drop_null_cache_control(v) for v in node]
    return node


class _StripNullCacheControl(httpx.AsyncBaseTransport):
    """Repairs every Messages request: no null `cache_control`, pinned to Anthropic."""

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self._inner = inner

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if not (request.method == "POST" and request.url.path.endswith("/messages")):
            return await self._inner.handle_async_request(request)
        body = _drop_null_cache_control(json.loads(request.content))
        body["provider"] = {"order": ["anthropic"], "allow_fallbacks": False}
        payload = json.dumps(body).encode()
        headers = httpx.Headers(request.headers)
        headers.pop("content-length", None)
        repaired = httpx.Request(request.method, request.url, headers=headers, content=payload,
                                 extensions=request.extensions)
        return await self._inner.handle_async_request(repaired)

    async def aclose(self) -> None:
        await self._inner.aclose()


def shim_client() -> httpx.AsyncClient:
    """An httpx client whose Messages requests OpenRouter's Anthropic shim will accept."""
    return httpx.AsyncClient(transport=_StripNullCacheControl(httpx.AsyncHTTPTransport()),
                             timeout=httpx.Timeout(180.0))
