"""Cumulative spend reported by the backbone provider, so a run can be costed exactly."""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request

from orchestrator.config import LLM_REGISTRY

logger = logging.getLogger(__name__)

_SUPPORTED_ENV_VARS = {"OPENROUTER_API_KEY"}

_KEY_ENDPOINT = "https://openrouter.ai/api/v1/key"
_TIMEOUT_S = 8


def key_spend(llm_key: str) -> float | None:
    """Dollars billed against `llm_key`'s configured API key so far, or None if unavailable."""
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
        logger.warning("could not read spend for %s: %s", llm_key, err)
        return None
    return float(usage)


def spent_between(before: float | None, after: float | None) -> float | None:
    """Cost of the work between two `key_spend` readings, or None if either is unknown."""
    if before is None or after is None:
        return None
    delta = after - before
    return delta if delta >= 0 else None


def balance(llm_key: str) -> dict[str, float]:
    """The provider's own spend figures for `llm_key`, empty when unavailable."""
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
        logger.warning("could not read balance for %s: %s", llm_key, err)
        return {}
    return {k: float(data[k]) for k in
            ("usage", "usage_daily", "usage_monthly", "limit_remaining")
            if isinstance(data.get(k), (int, float))}


def can_afford(llm_key: str, max_tokens: int = 4096) -> tuple[bool, str]:
    """Whether the provider will currently accept a request of `max_tokens`."""
    spec = LLM_REGISTRY.get(llm_key)
    if spec is None or spec.env_var not in _SUPPORTED_ENV_VARS:
        return True, ""
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
        if err.code == 402 or (err.code == 403 and "limit exceeded" in detail.lower()):
            return False, f"HTTP {err.code} — {detail}"
        logger.warning("affordability probe got HTTP %s: %s", err.code, detail)
        return True, ""
    except (OSError, ValueError) as err:
        logger.warning("affordability probe failed: %s", err)
        return True, ""


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
