"""Factory mapping an LLMSpec (from orchestrator.config) to a browser-use chat model."""
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
        from browser_use.llm.anthropic.chat import ChatAnthropic
        return ChatAnthropic(
            model=spec.model,
            temperature=0.0,
            api_key=os.getenv(spec.env_var),
            base_url=spec.base_url,
            http_client=shim_client(),
        )

    if spec.provider == "openrouter":
        from browser_use.llm.openai.chat import ChatOpenAI
        extra: dict = {}
        if os.getenv("OPENROUTER_SCHEMA_IN_PROMPT") == "1":
            extra["add_schema_to_system_prompt"] = True
        if os.getenv("OPENROUTER_NO_FORCE_STRUCTURED") == "1":
            extra["dont_force_structured_output"] = True
        http_client = pinned_client(os.getenv("OPENROUTER_PROVIDER", ""),
                                    os.getenv("OPENROUTER_REASONING_EFFORT", ""),
                                    os.getenv("OPENROUTER_MAX_TOKENS", ""))
        if http_client is not None:
            extra["http_client"] = http_client
        budget = os.getenv("OPENROUTER_MAX_TOKENS", "").strip()
        if budget:
            extra["max_completion_tokens"] = int(budget)
        return ChatOpenAI(
            model=spec.model,
            temperature=0.0,
            seed=seed,
            api_key=os.getenv(spec.env_var),
            base_url="https://openrouter.ai/api/v1",
            default_headers={"X-Title": "side-channel-testbed"},
            **extra,
        )

    raise ValueError(f"Unknown provider: {spec.provider}")
