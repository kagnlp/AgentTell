"""Results store: one row per agent session (a single matrix cell repetition).

Links to the attacker event log by session_id, and to the ground-truth trace file.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from orchestrator.config import RESULTS_DB

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id  TEXT PRIMARY KEY,
    agent       TEXT NOT NULL,
    llm         TEXT NOT NULL,
    condition   TEXT NOT NULL,   -- ground-truth condition (scenario-specific)
    rep         INTEGER NOT NULL,
    variant     TEXT,
    scenario    TEXT,            -- which scenario produced this session
    plant       TEXT,            -- which of the scenario's plant routes was used
    secret_label TEXT,           -- ground-truth secret value (e.g. first_national | none)
    ts          REAL NOT NULL,
    duration_s  REAL,
    final_result TEXT,
    error       TEXT,
    trace_path  TEXT,
    meta        TEXT
);
"""


def connect(db_path: Path | str = RESULTS_DB) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_column(conn: sqlite3.Connection, col: str, decl: str) -> None:
    """Add a column to an existing `sessions` table if an older database lacks it."""
    have = {r["name"] for r in conn.execute("PRAGMA table_info(sessions)").fetchall()}
    if col not in have:
        conn.execute(f"ALTER TABLE sessions ADD COLUMN {col} {decl}")


def init_db(db_path: Path | str = RESULTS_DB) -> None:
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)
        _ensure_column(conn, "scenario", "TEXT")
        _ensure_column(conn, "secret_label", "TEXT")
        _ensure_column(conn, "plant", "TEXT")
        conn.commit()


def already_done(session_key: str, db_path: Path | str = RESULTS_DB) -> bool:
    """A matrix cell is identified by agent-llm-condition-rep (the session_id prefix)."""
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT 1 FROM sessions WHERE session_id LIKE ? AND error IS NULL LIMIT 1",
            (session_key + "%",),
        ).fetchone()
    return row is not None


def record(
    *,
    session_id: str,
    agent: str,
    llm: str,
    condition: str,
    rep: int,
    variant: str,
    duration_s: float | None,
    final_result: str | None,
    error: str | None,
    trace_path: str | None,
    scenario: str = "authstate_v1",
    plant: str = "",
    secret_label: str | None = None,
    meta: dict[str, Any] | None = None,
    db_path: Path | str = RESULTS_DB,
) -> None:
    with connect(db_path) as conn:
        conn.execute(
            """INSERT OR REPLACE INTO sessions
               (session_id, agent, llm, condition, rep, variant, scenario, plant, secret_label,
                ts, duration_s, final_result, error, trace_path, meta)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (session_id, agent, llm, condition, rep, variant, scenario, plant, secret_label,
             time.time(), duration_s, final_result, error, trace_path,
             json.dumps(meta or {})),
        )
        conn.commit()
