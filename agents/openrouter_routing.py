"""Pin OpenRouter to a chosen upstream provider, so a run's price is the price we costed.

OpenRouter serves one model id from several upstreams and load-balances between them. They are
NOT priced alike: `google/gemini-3.7-flash` is $0.375/M prompt tokens on `google-vertex/global`
and $0.75/M on `google-ai-studio` — the same model, twice the money, chosen per request by
whichever upstream OpenRouter feels like using. A sweep of ~1,700 sessions cannot be budgeted
against a price that moves, and two halves of one dataset billed at different rates make the
per-session cost recorded in results.db meaningless.

The routing preference travels in the request body (`provider`), which is an OpenRouter
extension to the OpenAI schema. browser-use's `ChatOpenAI` exposes no `extra_body`, but it does
accept an `http_client`, so the preference is injected there. The alternative — pinning through
the model id as `model@tag` — is not supported and is rejected with "is not a valid model ID".
"""
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
        # Content-Length is dropped rather than recomputed: httpx sets it from the new body, and
        # a stale one truncates the request into a JSON parse error on the far side.
        headers = httpx.Headers(request.headers)
        headers.pop("content-length", None)
        pinned = httpx.Request(request.method, request.url, headers=headers, content=payload,
                               extensions=request.extensions)
        return await self._inner.handle_async_request(pinned)

    async def aclose(self) -> None:
        await self._inner.aclose()


def pinned_client(provider_tags: str, reasoning_effort: str = "",
                  max_tokens: str = "", json_object: bool = False) -> httpx.AsyncClient | None:
    """An httpx client carrying OpenRouter-only request fields, or None when none are wanted.

    Args:
        provider_tags: Comma-separated OpenRouter endpoint tags, most preferred first, as listed
            by `/api/v1/models/<author>/<slug>/endpoints` (e.g. "google-vertex/global"). Empty
            means no preference, which leaves OpenRouter's own routing untouched.
        max_tokens: Completion budget to send as OpenRouter's own `max_tokens`. Empty leaves it
            to whatever the client already sends.
        reasoning_effort: "minimal", "low", "medium" or "high" to cap how much of the completion
            budget a reasoning backbone spends thinking. Empty leaves the model's default. This
            is a correctness control, not a tuning knob: browser-use never sends a reasoning
            setting, so a model that reasons freely can spend the whole budget before emitting
            its structured output, truncating the JSON and costing the step its action.
        json_object: Send `response_format: {"type": "json_object"}`. For a backbone run with
            `OPENROUTER_NO_FORCE_STRUCTURED=1` (browser-use's own strict `json_schema` mode
            rejected outright, e.g. "Model ... does not support 'json_schema' response format.
            Supported formats: json_object."), sending nothing lets the model's raw completion
            leak chain-of-thought text ahead of the JSON on any step generating a long free-text
            field, which then fails to parse. `json_object` constrains decoding to syntactically
            valid JSON without asserting the exact schema, which the rejecting provider does
            support and which stops that leak.

    Raises:
        ValueError: If `provider_tags` is non-empty but contains no usable tag, since silently
            falling back to unpinned routing is how a run gets billed at a rate nobody chose.
    """
    fields: dict = {}
    if json_object:
        fields["response_format"] = {"type": "json_object"}
    if provider_tags.strip():
        tags = [t.strip() for t in provider_tags.split(",") if t.strip()]
        if not tags:
            raise ValueError(
                f"OPENROUTER_PROVIDER is set but names no provider: {provider_tags!r}")
        # allow_fallbacks=False makes an unavailable upstream a visible failure instead of a
        # silent reroute to a pricier one. A failed session is re-run; a mispriced one is never
        # noticed.
        fields["provider"] = {"order": tags, "allow_fallbacks": False}
    if reasoning_effort.strip():
        fields["reasoning"] = {"effort": reasoning_effort.strip()}
    if max_tokens.strip():
        # `max_tokens` as well as browser-use's `max_completion_tokens`. They are not
        # interchangeable here: raising only the latter left gemini-3.7-flash still truncating,
        # and a side-by-side probe returned visibly longer completions under `max_tokens`, so
        # OpenRouter does not appear to map the newer name onto this upstream's limit. Sending
        # both is harmless where the newer one is honoured and load-bearing where it is not.
        fields["max_tokens"] = int(max_tokens.strip())
    if not fields:
        return None
    return httpx.AsyncClient(transport=_InjectBody(httpx.AsyncHTTPTransport(), fields),
                             timeout=httpx.Timeout(180.0))
