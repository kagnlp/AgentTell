"""Factory mapping an LLMSpec (from orchestrator.config) to a browser-use chat model.

API keys are read from the environment (loaded from .env by orchestrator.config).
The chat wrappers pick up the standard provider env vars automatically; we pass the
key explicitly where the wrapper supports it to be safe. `seed` is set for
reproducibility where the provider allows it.
"""
from __future__ import annotations

import os

from agents.anthropic_openrouter import shim_client
from agents.openrouter_routing import pinned_client
from orchestrator.config import LLM_REGISTRY, LLMSpec


def build_llm(llm_key: str, seed: int | None = None):
    spec: LLMSpec = LLM_REGISTRY[llm_key]
    if not spec.available:
        raise RuntimeError(
            f"LLM '{llm_key}' needs {spec.env_var} set in .env (not found)."
        )

    if spec.provider == "anthropic":
        # Native Anthropic tool-use, which is the only way Claude runs under browser-use: the
        # OpenAI-compatible path turns browser-use's json_schema into an Anthropic strict tool
        # and the request is refused with "the compiled grammar is too large". `base_url` lets the
        # same branch reach either Anthropic directly or a gateway that serves /v1/messages
        # (see LLMSpec.base_url).
        from browser_use.llm.anthropic.chat import ChatAnthropic
        # `seed` is deliberately NOT passed: ChatAnthropic accepts the field but forwards it to
        # the Anthropic SDK, which rejects it ("unexpected keyword argument 'seed'"). Anthropic
        # has no seed parameter, so a run on this backbone carries weaker reproducibility control
        # than the seeded OpenAI-compatible ones (see the standard, section 10).
        return ChatAnthropic(
            model=spec.model,
            temperature=0.0,
            api_key=os.getenv(spec.env_var),
            base_url=spec.base_url or None,
            # Only a gateway needs the repair; Anthropic's own API accepts the request as
            # browser-use serializes it (see agents/anthropic_openrouter).
            http_client=(shim_client(os.getenv("ANTHROPIC_PROVIDER", "anthropic"))
                         if spec.base_url else None),
        )

    if spec.provider == "openai":
        from browser_use.llm.openai.chat import ChatOpenAI
        return ChatOpenAI(model=spec.model, temperature=0.0, seed=seed)

    if spec.provider == "google":
        from browser_use.llm.google.chat import ChatGoogle
        return ChatGoogle(model=spec.model, temperature=0.0, seed=seed)

    if spec.provider == "openrouter":
        # OpenRouter exposes an OpenAI-compatible API, so we reuse ChatOpenAI but point
        # it at the OpenRouter endpoint with the OPENROUTER_API_KEY. Override the base
        # URL via OPENROUTER_BASE_URL if needed.
        from browser_use.llm.openai.chat import ChatOpenAI
        base_url = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
        # Optional attribution headers OpenRouter uses for ranking; harmless if unset.
        default_headers = {
            k: v
            for k, v in (
                ("HTTP-Referer", os.getenv("OPENROUTER_REFERER", "")),
                ("X-Title", os.getenv("OPENROUTER_TITLE", "side-channel-testbed")),
            )
            if v
        }
        # Structured-output fallback for backbones that don't support OpenAI's strict
        # json_schema response_format (e.g. z-ai/glm-4.6v: structured_outputs=false on
        # OpenRouter). browser-use forces strict structured output by default, which such
        # models reject or fail to satisfy; these opt-in flags make it put the schema in the
        # system prompt and stop forcing the strict format instead. Off by default.
        extra: dict = {}
        if os.getenv("OPENROUTER_SCHEMA_IN_PROMPT") == "1":
            extra["add_schema_to_system_prompt"] = True
        if os.getenv("OPENROUTER_NO_FORCE_STRUCTURED") == "1":
            extra["dont_force_structured_output"] = True
        # Pin the upstream provider when OPENROUTER_PROVIDER names one. Unset means OpenRouter
        # routes as it likes, which is the historical behaviour and leaves earlier datasets
        # reproducible; set, it fixes the price the run is billed at (see agents/openrouter_routing).
        http_client = pinned_client(os.getenv("OPENROUTER_PROVIDER", ""),
                                    os.getenv("OPENROUTER_REASONING_EFFORT", ""),
                                    os.getenv("OPENROUTER_MAX_TOKENS", ""),
                                    os.getenv("OPENROUTER_JSON_OBJECT") == "1")
        if http_client is not None:
            extra["http_client"] = http_client
        # browser-use caps completions at 4096 tokens. On a REASONING backbone those tokens are
        # spent thinking before the structured output is emitted, so a step whose page has a lot
        # to read back gets its JSON truncated mid-string — browser-use logs "Failed to parse
        # structured output", the step yields no action, and after a few of those the agent gives
        # up on the plant page having never opened the probe. That looks like an agent declining
        # to act; it is a truncated response. gemini-3.7-flash lost 6% of sessions this way, all
        # on the read-back-heavy plants. Raise it per backbone rather than globally, so datasets
        # already collected at the default stay reproducible.
        budget = os.getenv("OPENROUTER_MAX_TOKENS", "").strip()
        if budget:
            extra["max_completion_tokens"] = int(budget)
        return ChatOpenAI(
            model=spec.model,
            temperature=0.0,
            seed=seed,
            api_key=os.getenv(spec.env_var),
            base_url=base_url,
            default_headers=default_headers or None,
            **extra,
        )

    if spec.provider == "hf":
        # Qwen2-VL via HF's OpenAI-compatible router. Best-effort: requires the model
        # to be served on an OpenAI-compatible endpoint. Override HF_BASE_URL if needed.
        from browser_use.llm.openai.chat import ChatOpenAI
        base_url = os.getenv("HF_BASE_URL", "https://router.huggingface.co/v1")
        return ChatOpenAI(
            model=spec.model,
            temperature=0.0,
            seed=seed,
            api_key=os.getenv("HF_API_TOKEN"),
            base_url=base_url,
        )

    raise ValueError(f"Unknown provider: {spec.provider}")
