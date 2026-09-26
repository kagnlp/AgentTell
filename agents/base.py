"""AgentRunner interface and the Trace/SessionPlan data model.

A SessionPlan is an ordered list of steps the agent performs in ONE browser session
(shared context). Pre-visit steps plant ground-truth prior-origin state; the final
step routes the agent to the attacker probe. A Trace is the ground-truth record of
what the agent did internally (for analysis); the actual side-channel observables
live in the attacker's event log, joined later by session_id.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Step:
    """One instruction in a session plan."""
    task: str                      # natural-language task for the agent
    label: str = ""                # e.g. "plant_bank", "visit_probe"
    start_url: str | None = None   # optional URL to open before reasoning


@dataclass
class SessionPlan:
    session_id: str
    condition: str                 # "X" or "not_X"
    steps: list[Step]
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class Trace:
    """Ground-truth internal record of a session, serialized to traces/."""
    session_id: str
    agent: str
    llm: str
    condition: str
    urls: list[str] = field(default_factory=list)
    actions: list[Any] = field(default_factory=list)
    thoughts: list[Any] = field(default_factory=list)
    final_result: str | None = None
    errors: list[Any] = field(default_factory=list)
    duration_s: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    error: str | None = None       # harness-level failure, if any


class AgentRunner(ABC):
    """Drive an agent through a SessionPlan and return a Trace."""

    name: str = "abstract"

    @abstractmethod
    def run_session(self, plan: SessionPlan, llm_key: str, seed: int | None = None) -> Trace:
        ...
