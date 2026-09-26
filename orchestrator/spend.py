"""Cumulative spend reported by the backbone provider, so a run can be costed exactly.

Only OpenRouter exposes a cheap per-key running total (`GET /api/v1/key` -> `data.usage`, in
US dollars). Read it either side of a unit of work and the difference is what that work cost.
The provider's `usage_daily` figure cannot do this once two runs share a day, and the traces
carry no token counts at all — `state_message` holds only the task preamble, not the prompt
that was billed.

Absence is a NORMAL result here and is reported as `None`, never as `0.0`. A run whose cost
could not be read must not be recorded as free: that is the "a tooling failure never becomes a
result" rule from the project standard, and a silent 0.00 in a cost column is exactly the kind
of wrong number it exists to prevent.

Eventual consistency: the provider's total may lag a completion by a moment, so a single cell's
delta can land in the next cell. Over a run the sum is right, and the run-level figure
(`main()` in orchestrator/run_matrix) is the one to quote.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request

from orchestrator.config import LLM_REGISTRY

logger = logging.getLogger(__name__)

#: API keys whose spend this module knows how to read. Anything else returns None, which
#: callers must treat as "unknown", not "free".
#:
#: Keyed on the env var rather than on `LLMSpec.provider` because the endpoint below reports a
#: running total for a KEY, so what it can answer for is which key paid — not which wire format
#: was spoken. `claude_openrouter` bills to OPENROUTER_API_KEY while carrying provider
#: "anthropic" (it talks to OpenRouter's Anthropic-compatible /v1/messages), and gating on the
#: provider name silently dropped the cost from every one of its rows.
_SUPPORTED_ENV_VARS = {"OPENROUTER_API_KEY"}

_KEY_ENDPOINT = "https://openrouter.ai/api/v1/key"
#: Deliberately tight. This runs twice per session, so a degraded provider would otherwise add
#: up to 2 x timeout x n_sessions to a sweep — over an hour on a 253-session run — to collect
#: bookkeeping the run does not depend on. Failing fast and recording "unknown" is the right
#: trade.
_TIMEOUT_S = 8


def key_spend(llm_key: str) -> float | None:
    """Dollars billed against `llm_key`'s configured API key so far, or None if unavailable.

    Returns None — never 0.0 — when the backbone has no spend endpoint, its key is unset, or
    the request fails. The caller records the absence rather than a number.

    Args:
        llm_key: A key into `orchestrator.config.LLM_REGISTRY` (e.g. "openrouter").
    """
    spec = LLM_REGISTRY.get(llm_key)
    if spec is None or spec.env_var not in _SUPPORTED_ENV_VARS:
        return None
    api_key = os.getenv(spec.env_var)
    if not api_key:
        return None

    request = urllib.request.Request(  # noqa: S310  fixed https literal, not caller-supplied
        _KEY_ENDPOINT, headers={"Authorization": f"Bearer {api_key}"})
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_S) as response:  # noqa: S310
            usage = json.load(response)["data"]["usage"]
    except (OSError, KeyError, ValueError) as err:
        # Costing is bookkeeping, not measurement: losing it must never abort a run that is
        # otherwise fine. Log loudly enough to notice, then report the absence.
        #
        # OSError, not urllib.error.URLError: urllib only wraps failures from the CONNECT phase.
        # A reset while reading the response body surfaces as a bare ConnectionResetError, which
        # escaped this handler and killed run_cell AFTER its session had run - costing a
        # completed session its results row and leaving its events orphaned (2026-08-15).
        logger.warning("could not read spend for %s: %s", llm_key, err)
        return None
    return float(usage)


def spent_between(before: float | None, after: float | None) -> float | None:
    """Cost of the work between two `key_spend` readings, or None if either is unknown.

    A negative difference means the provider's monthly counter reset mid-run, which is not a
    cost and is reported as unknown rather than as a nonsensical refund.
    """
    if before is None or after is None:
        return None
    delta = after - before
    return delta if delta >= 0 else None


def balance(llm_key: str) -> dict[str, float]:
    """The provider's own spend figures for `llm_key`, empty when unavailable.

    Reported verbatim rather than reduced to one number, because they do not always reconcile:
    a key can show hundreds of dollars of `limit_remaining` while still refusing a request for
    want of credit. `can_afford` is the authoritative check; these are for the progress line.
    """
    spec = LLM_REGISTRY.get(llm_key)
    if spec is None or spec.env_var not in _SUPPORTED_ENV_VARS:
        return {}
    api_key = os.getenv(spec.env_var)
    if not api_key:
        return {}
    request = urllib.request.Request(  # noqa: S310  fixed https literal
        _KEY_ENDPOINT, headers={"Authorization": f"Bearer {api_key}"})
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_S) as response:  # noqa: S310
            data = json.load(response)["data"]
    except (OSError, KeyError, ValueError) as err:
        # See key_spend: a bare ConnectionResetError escaping here crashed `--check`, whose
        # non-zero exit the sweep script reads as "out of credit" - so a DNS outage announced
        # itself as credit exhaustion and stopped a 38-hour run under the wrong diagnosis.
        logger.warning("could not read balance for %s: %s", llm_key, err)
        return {}
    return {k: float(data[k]) for k in
            ("usage", "usage_daily", "usage_monthly", "limit_remaining")
            if isinstance(data.get(k), (int, float))}


def can_afford(llm_key: str, max_tokens: int = 4096) -> tuple[bool, str]:
    """Whether the provider will currently accept a request of `max_tokens`.

    This ISSUES a tiny completion rather than reading a balance field, because the balance
    fields proved not to predict the refusal: the run of 2026-08-13 died on HTTP 402 while the
    same key reported $321 of `limit_remaining`. Only the provider's own admission control
    knows, so ask it. The probe costs a few tokens.

    `max_tokens` must match what the agent actually requests (browser-use's ChatOpenAI defaults
    to 4096) — a probe at a smaller budget would pass while every real session still failed.

    Returns:
        (True, "") when a run may proceed, else (False, the provider's reason).
    """
    spec = LLM_REGISTRY.get(llm_key)
    if spec is None or spec.env_var not in _SUPPORTED_ENV_VARS:
        return True, ""            # nothing to check; do not block a run we cannot assess
    api_key = os.getenv(spec.env_var)
    if not api_key:
        return False, f"{spec.env_var} is not set"

    body = json.dumps({"model": spec.model, "max_tokens": max_tokens,
                       "messages": [{"role": "user", "content": "ok"}]}).encode()
    request = urllib.request.Request(  # noqa: S310  fixed https literal
        "https://openrouter.ai/api/v1/chat/completions", data=body,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_S * 4) as response:
            json.load(response)
        return True, ""
    except urllib.error.HTTPError as err:
        detail = err.read().decode("utf-8", "replace")[:300]
        try:
            detail = json.loads(detail)["error"]["message"]
        except (ValueError, KeyError, TypeError):
            pass
        # 402 means "out of money". 403 usually does not — but OpenRouter also uses it for
        # "Key limit exceeded", which is exhaustion by another name and is just as terminal: the
        # run of 2026-08-17 hit it and kept going for 52 minutes, burning 1,001 sessions into
        # errored rows, because only 402 was recognised. Everything else (429, 5xx, other 403s)
        # stays transient and must NOT abort a 40-hour run over a blip.
        if err.code == 402 or (err.code == 403 and "limit exceeded" in detail.lower()):
            return False, f"HTTP {err.code} — {detail}"
        logger.warning("affordability probe got HTTP %s: %s", err.code, detail)
        return True, ""
    except (OSError, ValueError) as err:
        logger.warning("affordability probe failed: %s", err)
        return True, ""            # a network blip is not exhaustion


def _main() -> int:
    """`python -m orchestrator.spend` — print the balance; exit 2 if a run cannot proceed."""
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--llm", default="openrouter")
    parser.add_argument("--max-tokens", type=int, default=4096,
                        help="the budget a real session requests; the probe must match it")
    parser.add_argument("--check", action="store_true",
                        help="exit 2 if the provider will not accept a session-sized request")
    args = parser.parse_args()

    figures = balance(args.llm)
    if figures:
        print("  ".join(f"{k}={v:,.4f}" for k, v in figures.items()))
    else:
        print("balance unavailable")
    if not args.check:
        return 0
    ok, reason = can_afford(args.llm, args.max_tokens)
    if ok:
        return 0
    print(f"CANNOT PROCEED: {reason}")
    return 2


if __name__ == "__main__":
    raise SystemExit(_main())
