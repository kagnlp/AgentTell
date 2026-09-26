"""Central configuration: loads secrets from .env, defines the experiment matrix.

All provider API keys come from the git-ignored .env file via python-dotenv.
Nothing is hardcoded. Only providers you actually run need a key present.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# Load .env from repo root regardless of CWD.
REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(REPO_ROOT / ".env")

# --- Closed-world network (all loopback) -----------------------------------
ATTACKER_HOST = os.getenv("ATTACKER_HOST", "app.test")
ATTACKER_PORT = int(os.getenv("ATTACKER_PORT", "8001"))
BANK_HOST = os.getenv("BANK_HOST", "bank.test")
BANK_PORT = int(os.getenv("BANK_PORT", "8002"))
# Generic services origin (SSO / retailers / stores / storage / task services) — hosts the
# cookie-gated /continue endpoint the set-membership scenarios route through.
SERVICES_HOST = os.getenv("SERVICES_HOST", "services.test")
SERVICES_PORT = int(os.getenv("SERVICES_PORT", "8004"))

ATTACKER_BASE_URL = os.getenv("ATTACKER_BASE_URL") or f"http://{ATTACKER_HOST}:{ATTACKER_PORT}"
BANK_BASE_URL = f"http://{BANK_HOST}:{BANK_PORT}"
SERVICES_BASE_URL = f"http://{SERVICES_HOST}:{SERVICES_PORT}"

# --- Storage ---------------------------------------------------------------
# Dataset namespace. Set SCT_DATASET (in .env, or exported in the shell) to isolate one
# (agent, model) run's storage under results/<dataset>/ — its own results.db, events.db and
# traces/ — so different backbones never pool into a single DB. It MUST be set identically for
# the three parties that share the storage: the mock servers (they write events.db), run_matrix
# (writes results.db), and the analysis (reads both). The simplest way to guarantee that is one
# line in .env, since every process loads it here. Unset (default) keeps the original flat
# results/ layout for back-compat.
DATASET = os.getenv("SCT_DATASET", "").strip().strip("/")
DATA_ROOT = (REPO_ROOT / "results" / DATASET) if DATASET else (REPO_ROOT / "results")
DATA_ROOT.mkdir(parents=True, exist_ok=True)

RESULTS_DB = DATA_ROOT / "results.db"
EVENT_LOG_DB = DATA_ROOT / "events.db"
TRACES_DIR = (DATA_ROOT / "traces") if DATASET else (REPO_ROOT / "traces")


# --- LLM backbone registry -------------------------------------------------
@dataclass(frozen=True)
class LLMSpec:
    key: str                # short id used in the matrix
    provider: str           # anthropic | openai | google | hf | openrouter
    model: str              # provider model id
    env_var: str            # which .env var must be set to use it
    #: Override for the provider's API host. Empty means the provider's own default. Set it to
    #: reach a gateway that speaks the same wire format — OpenRouter serves Anthropic's
    #: /v1/messages at https://openrouter.ai/api, which is how Claude is driven here.
    base_url: str = ""

    @property
    def available(self) -> bool:
        return bool(os.getenv(self.env_var))


LLM_REGISTRY: dict[str, LLMSpec] = {
    "claude": LLMSpec("claude", "anthropic", "claude-opus-4-8", "ANTHROPIC_API_KEY"),
    # Claude through OpenRouter's Anthropic-compatible /v1/messages endpoint, so it is keyed and
    # billed on OPENROUTER_API_KEY like every other current backbone. The OpenAI-compatible path
    # (provider "openrouter") CANNOT drive Claude under browser-use: OpenRouter compiles
    # browser-use's json_schema into an Anthropic strict tool and Anthropic rejects the request
    # with "the compiled grammar is too large". Native Anthropic tool-use has no compiled
    # grammar. The model is read from ANTHROPIC_MODEL so the backbone swaps from .env.
    "claude_openrouter": LLMSpec(
        "claude_openrouter", "anthropic",
        os.getenv("ANTHROPIC_MODEL", "anthropic/claude-sonnet-5"), "OPENROUTER_API_KEY",
        base_url="https://openrouter.ai/api",
    ),
    "gpt4o": LLMSpec("gpt4o", "openai", "gpt-4o", "OPENAI_API_KEY"),
    "gemini": LLMSpec("gemini", "google", "gemini-1.5-pro", "GOOGLE_API_KEY"),
    "qwen2vl": LLMSpec("qwen2vl", "hf", "Qwen/Qwen2-VL-7B-Instruct", "HF_API_TOKEN"),
    # OpenRouter (OpenAI-compatible gateway). Used for current experiments. The model is
    # read from OPENROUTER_MODEL so a backbone can be swapped by editing .env (alongside
    # SCT_DATASET) instead of this file — e.g. openai/gpt-5.2, qwen/qwen3-vl-235b-a22b-instruct,
    # z-ai/glm-4.6v. Default preserves the previous hardcoded behaviour.
    "openrouter": LLMSpec(
        "openrouter", "openrouter",
        os.getenv("OPENROUTER_MODEL", "x-ai/grok-4.5"), "OPENROUTER_API_KEY"
    ),
}


# --- Experiment matrix -----------------------------------------------------
@dataclass
class MatrixConfig:
    agents: list[str] = field(default_factory=lambda: ["browseruse"])
    llms: list[str] = field(
        default_factory=lambda: ["openrouter", "claude", "gpt4o", "gemini", "qwen2vl"]
    )
    conditions: list[str] = field(default_factory=lambda: ["X", "not_X"])  # bank-authed or not
    repetitions: int = 10
    base_seed: int = 1234

    def available_llms(self) -> list[str]:
        """Only the LLMs whose API key is present in .env."""
        return [k for k in self.llms if LLM_REGISTRY[k].available]


MATRIX = MatrixConfig()
