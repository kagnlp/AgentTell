"""Session identity helpers.

The attacker origin ties all its observations to one logical agent session via a
session_id carried in the probe URL. We generate it here and build the probe URL the
agent will be told to visit.
"""
from __future__ import annotations

import uuid

from orchestrator.config import ATTACKER_BASE_URL


def new_session_id(agent: str, llm: str, condition: str, rep: int,
                   scenario: str = "authstate_v1") -> str:
    """An OPAQUE per-run token.

    The id is carried in the probe URL, so anything descriptive in it ends up in the agent's
    address bar while it is deciding. The old form
    (agent-llm-<scenario>-<condition>-r<rep>-<hash>) therefore put the held condition itself
    into the agent's context in every loaded run: .../probe/sso_wall/...-sso_wall-google-r0-...
    named google, and a prefilled_identity run named priya_nair, which the correction matcher
    scores on. That is a second, unintended route to the secret alongside the plant.

    The arguments are kept in the signature because callers pass them, but they are NOT encoded.
    The (agent, llm, condition, rep, scenario) mapping is written to results.db by
    orchestrator.results_store, which is where every analysis path already reads it from.
    """
    return uuid.uuid4().hex


def trace_basename(session_id: str, scenario: str = "", condition: str = "",
                   variant: str = "", plant: str = "") -> str:
    """Descriptive on-disk filename: `<scenario>-<condition>-<plant>-<variant>-<hash>.json`.

    This is a DISK filename only — it is NEVER shown to the agent. The agent's only inputs are the
    composed task text and the browser; the trace is written AFTER the run (history.save_to_file)
    and re-read only to augment it. So, unlike the probe `session_id` (which rides in the URL and
    therefore stays an opaque uuid, see new_session_id), the trace file is free to be descriptive.
    The opaque session_id is kept as the hash suffix so the file stays unique and linkable back to
    its results.db row.
    """
    import re

    def slug(s: str) -> str:
        return re.sub(r"[^0-9A-Za-z_.-]+", "_", s).strip("_")

    parts = [slug(p) for p in (scenario, condition, plant, variant) if p]
    h = session_id[:8] if session_id else "nohash"
    prefix = "-".join(parts)
    return f"{prefix}-{h}.json" if prefix else f"{h}.json"


def probe_url(session_id: str, variant: str = "A",
              scenario: str = "authstate_v1", plant: str = "") -> str:
    # `authstate_v1` — the out-of-scope solicited-disclosure baseline — keeps its dedicated
    # /finance route and template; every other scenario is served by the generic /probe route,
    # which dispatches on `scenario`.
    if scenario == "authstate_v1":
        return f"{ATTACKER_BASE_URL}/finance?session_id={session_id}&variant={variant}"
    # Path-based so scenario + session_id AND variant survive even if the agent drops the query
    # string when it types the URL into a navigate action (observed with some LLM backbones).
    # variant in the path matters for the `direct` arm, whose ask-outright box only renders when
    # variant=direct actually reaches the server.
    url = f"{ATTACKER_BASE_URL}/probe/{scenario}/{session_id}/{variant}"
    # `plant` is appended ONLY for a scenario whose probe copy varies by plant (vendor_session's
    # surface factor — see Scenario.names_for). It rides in the PATH for the same reason `variant`
    # does: a backbone that drops the query string would otherwise render the wrong vendor names,
    # which reads as a clean 0.00 rather than as the broken run it is. Every other scenario's
    # probe URL is unchanged, byte for byte.
    return f"{url}/{plant}" if plant else url
