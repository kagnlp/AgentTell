"""Session plan and trace data model."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Step:
    """One instruction in a session plan."""
    task: str
    label: str = ""
    start_url: str | None = None


@dataclass
class SessionPlan:
    session_id: str
    condition: str
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
    final_result: str | None = None
    errors: list[Any] = field(default_factory=list)
    duration_s: float | None = None
    error: str | None = None
