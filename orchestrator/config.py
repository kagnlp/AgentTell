"""Central configuration: loads secrets from .env, defines the experiment matrix."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(REPO_ROOT / ".env")

ATTACKER_HOST = os.getenv("ATTACKER_HOST", "app.test")
ATTACKER_PORT = int(os.getenv("ATTACKER_PORT", "8001"))
BANK_HOST = os.getenv("BANK_HOST", "bank.test")
BANK_PORT = int(os.getenv("BANK_PORT", "8002"))
SERVICES_HOST = os.getenv("SERVICES_HOST", "services.test")
SERVICES_PORT = int(os.getenv("SERVICES_PORT", "8004"))

ATTACKER_BASE_URL = os.getenv("ATTACKER_BASE_URL") or f"http://{ATTACKER_HOST}:{ATTACKER_PORT}"
BANK_BASE_URL = f"http://{BANK_HOST}:{BANK_PORT}"
SERVICES_BASE_URL = f"http://{SERVICES_HOST}:{SERVICES_PORT}"

DATASET = os.getenv("SCT_DATASET", "").strip().strip("/")
DATA_ROOT = (REPO_ROOT / "results" / DATASET) if DATASET else (REPO_ROOT / "results")
DATA_ROOT.mkdir(parents=True, exist_ok=True)

RESULTS_DB = DATA_ROOT / "results.db"
EVENT_LOG_DB = DATA_ROOT / "events.db"
TRACES_DIR = (DATA_ROOT / "traces") if DATASET else (REPO_ROOT / "traces")


@dataclass(frozen=True)
class LLMSpec:
    key: str
    provider: str
    model: str
    env_var: str
    base_url: str = ""

    @property
    def available(self) -> bool:
        return bool(os.getenv(self.env_var))


LLM_REGISTRY: dict[str, LLMSpec] = {
    "claude_openrouter": LLMSpec(
        "claude_openrouter", "anthropic",
        os.getenv("ANTHROPIC_MODEL", "anthropic/claude-sonnet-5"), "OPENROUTER_API_KEY",
        base_url="https://openrouter.ai/api",
    ),
    "openrouter": LLMSpec(
        "openrouter", "openrouter",
        os.getenv("OPENROUTER_MODEL", "x-ai/grok-4.5"), "OPENROUTER_API_KEY"
    ),
}


@dataclass
class MatrixConfig:
    agents: list[str] = field(default_factory=lambda: ["browseruse"])
    llms: list[str] = field(default_factory=lambda: ["openrouter"])
    repetitions: int = 10
    base_seed: int = 1234

    def available_llms(self) -> list[str]:
        """Only the LLMs whose API key is present in .env."""
        return [k for k in self.llms if LLM_REGISTRY[k].available]


MATRIX = MatrixConfig()
