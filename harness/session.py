"""Session identity helpers."""
from __future__ import annotations

import uuid

from orchestrator.config import ATTACKER_BASE_URL


def new_session_id() -> str:
    """An OPAQUE per-run token."""
    return uuid.uuid4().hex


def trace_basename(session_id: str, scenario: str = "", condition: str = "",
                   variant: str = "", plant: str = "") -> str:
    """Descriptive on-disk filename: `<scenario>-<condition>-<plant>-<variant>-<hash>.json`."""
    import re

    def slug(s: str) -> str:
        return re.sub(r"[^0-9A-Za-z_.-]+", "_", s).strip("_")

    parts = [slug(p) for p in (scenario, condition, plant, variant) if p]
    h = session_id[:8] if session_id else "nohash"
    prefix = "-".join(parts)
    return f"{prefix}-{h}.json" if prefix else f"{h}.json"


def probe_url(session_id: str, variant: str = "A",
              scenario: str = "sso_wall", plant: str = "") -> str:
    url = f"{ATTACKER_BASE_URL}/probe/{scenario}/{session_id}/{variant}"
    return f"{url}/{plant}" if plant else url
