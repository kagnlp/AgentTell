"""Pin OpenRouter to a chosen upstream provider, so a run's price is the price we costed."""
from __future__ import annotations

import json

import httpx


class _InjectBody(httpx.AsyncBaseTransport):
    """Merges OpenRouter-only fields into every chat completion this client sends."""

    def __init__(self, inner: httpx.AsyncBaseTransport, fields: dict) -> None:
        self._inner = inner
        self._fields = fields

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if not (request.method == "POST" and request.url.path.endswith("/chat/completions")):
            return await self._inner.handle_async_request(request)
        body = json.loads(request.content)
        body.update(self._fields)
        payload = json.dumps(body).encode()
        headers = httpx.Headers(request.headers)
        headers.pop("content-length", None)
        pinned = httpx.Request(request.method, request.url, headers=headers, content=payload,
                               extensions=request.extensions)
        return await self._inner.handle_async_request(pinned)

    async def aclose(self) -> None:
        await self._inner.aclose()


def pinned_client(provider_tags: str, reasoning_effort: str = "",
                  max_tokens: str = "") -> httpx.AsyncClient | None:
    """An httpx client carrying OpenRouter-only request fields, or None when none are wanted."""
    fields: dict = {}
    if provider_tags.strip():
        tags = [t.strip() for t in provider_tags.split(",") if t.strip()]
        if not tags:
            raise ValueError(
                f"OPENROUTER_PROVIDER is set but names no provider: {provider_tags!r}")
        fields["provider"] = {"order": tags, "allow_fallbacks": False}
    if reasoning_effort.strip():
        fields["reasoning"] = {"effort": reasoning_effort.strip()}
    if max_tokens.strip():
        fields["max_tokens"] = int(max_tokens.strip())
    if not fields:
        return None
    return httpx.AsyncClient(transport=_InjectBody(httpx.AsyncHTTPTransport(), fields),
                             timeout=httpx.Timeout(180.0))
