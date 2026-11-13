"""Opaque plant codes."""
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
