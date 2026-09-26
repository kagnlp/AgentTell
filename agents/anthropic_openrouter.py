"""Make browser-use's Anthropic requests acceptable to OpenRouter's Messages shim.

OpenRouter serves Anthropic's `/v1/messages` at https://openrouter.ai/api, which is the only
way Claude runs under browser-use here — the OpenAI-compatible path turns browser-use's
`json_schema` into an Anthropic strict tool and Anthropic refuses it with "the compiled grammar
is too large" (see agents/llms.build_llm).

The shim is stricter than the API it stands in for. browser-use's serializer builds every
content block as a TypedDict and writes `cache_control` unconditionally, so a block with
caching off travels as an explicit `"cache_control": null`. Anthropic itself accepts that;
OpenRouter validates the field as an object and rejects the whole request with HTTP 400
`invalid_union` at `messages.0.content`. Every step fails identically, the agent never reaches
the probe, and the session is recorded as an error rather than a result.

Dropping the null keys is a wire-level repair of a serialization detail, not a change to what
the model is asked. A `cache_control` that actually carries a value is left alone, so prompt
caching still works where browser-use asks for it.

The same hook pins the upstream, for the reason set out in agents/openrouter_routing: OpenRouter
serves this model from nine endpoints at three different prices and picks per request, so an
unpinned sweep is billed at a rate nobody chose and the per-session cost in results.db means
nothing. The tag travels in `provider`, an OpenRouter extension the Messages shim honours (a
bogus tag is refused with "No endpoints found", which is how we know it is read rather than
ignored). It is deliberately NOT read from OPENROUTER_PROVIDER: that variable names an upstream
for the OpenAI-compatible path — it is set to "openai" in this repo's .env — and an OpenAI
upstream cannot serve an Anthropic model, so reusing it would 404 every request.
"""
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
    """Repairs every Messages request: no null `cache_control`, plus any pinned routing."""

    def __init__(self, inner: httpx.AsyncBaseTransport, fields: dict | None = None) -> None:
        self._inner = inner
        self._fields = fields or {}

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if not (request.method == "POST" and request.url.path.endswith("/messages")):
            return await self._inner.handle_async_request(request)
        body = _drop_null_cache_control(json.loads(request.content))
        body.update(self._fields)
        payload = json.dumps(body).encode()
        # Content-Length is dropped rather than recomputed: httpx sets it from the new body, and
        # a stale one truncates the request into a JSON parse error on the far side.
        headers = httpx.Headers(request.headers)
        headers.pop("content-length", None)
        repaired = httpx.Request(request.method, request.url, headers=headers, content=payload,
                                 extensions=request.extensions)
        return await self._inner.handle_async_request(repaired)

    async def aclose(self) -> None:
        await self._inner.aclose()


def shim_client(provider_tags: str = "") -> httpx.AsyncClient:
    """An httpx client whose Messages requests OpenRouter's Anthropic shim will accept.

    Args:
        provider_tags: Comma-separated OpenRouter endpoint tags, most preferred first, as listed
            by `/api/v1/models/<author>/<slug>/endpoints` (e.g. "anthropic"). Empty leaves
            OpenRouter's own routing untouched, which means the run's price is not fixed.

    Raises:
        ValueError: If `provider_tags` is non-empty but names no usable tag, since falling back
            to unpinned routing is how a run gets billed at a rate nobody chose.
    """
    fields: dict = {}
    if provider_tags.strip():
        tags = [t.strip() for t in provider_tags.split(",") if t.strip()]
        if not tags:
            raise ValueError(
                f"ANTHROPIC_PROVIDER is set but names no provider: {provider_tags!r}")
        # allow_fallbacks=False makes an unavailable upstream a visible failure instead of a
        # silent reroute to a pricier one. A failed session is re-run; a mispriced one is never
        # noticed.
        fields["provider"] = {"order": tags, "allow_fallbacks": False}
    return httpx.AsyncClient(transport=_StripNullCacheControl(httpx.AsyncHTTPTransport(), fields),
                             timeout=httpx.Timeout(180.0))
