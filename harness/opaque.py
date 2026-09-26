"""Opaque plant codes.

The plant page must know WHICH service/brand/topic to render, but that id must not appear in
the URL the agent navigates to. If it did, the held secret would sit in the agent's own
instructions (the typed URL) as an instruction-echo confound alongside the intended
carried-context route — the agent reading the name off the plant PAGE. See the de-confound
rationale in harness/state_planting.py.

So the plant URL carries a one-way `code_for(value)` instead of the readable id. The mock server
recovers the id by `resolve(code, candidates)` — hashing each of its own known ids and matching —
so no id→secret mapping ever travels in the URL. `code_for` is deterministic, so state_planting
(which builds the URL) and the mock server (which resolves it) agree without sharing state.
"""
from __future__ import annotations

import hashlib
from collections.abc import Iterable

_SALT = "sct-plant-v1"


def code_for(value: str) -> str:
    """A short, non-reversible code for a plant id (e.g. 'google' -> '9f2a1c...')."""
    return hashlib.sha256(f"{_SALT}:{value}".encode()).hexdigest()[:12]


def resolve(code: str, candidates: Iterable[str]) -> str | None:
    """Recover the id whose `code_for` equals `code`, or None if none match."""
    return next((c for c in candidates if code_for(c) == code), None)
