"""SQLite event log for the attacker origin."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from orchestrator.config import EVENT_LOG_DB as DEFAULT_DB

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT    NOT NULL,
    ts          REAL    NOT NULL,   -- server receive time (epoch seconds)
    client_ts   REAL,               -- client performance.now() ms, if provided
    event_type  TEXT    NOT NULL,   -- pageview|click|hover|scroll|focus|blur|visibility
                                    -- |nav|beacon
    target_id   TEXT,               -- DOM element id / data-probe attribute
    x           REAL,
    y           REAL,
    dwell_ms    REAL,
    url         TEXT,
    referrer    TEXT,
    form_payload TEXT,              -- JSON: form fields, clicked link, etc.
    extra       TEXT                -- JSON: anything else
);
CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type);
"""


def connect(db_path: Path | str = DEFAULT_DB) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def init_db(db_path: Path | str = DEFAULT_DB) -> None:
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)


def log_event(
    session_id: str,
    event_type: str,
    *,
    db_path: Path | str = DEFAULT_DB,
    client_ts: float | None = None,
    target_id: str | None = None,
    x: float | None = None,
    y: float | None = None,
    dwell_ms: float | None = None,
    url: str | None = None,
    referrer: str | None = None,
    form_payload: Any | None = None,
    extra: Any | None = None,
) -> None:
    with connect(db_path) as conn:
        conn.execute(
            """INSERT INTO events
               (session_id, ts, client_ts, event_type, target_id, x, y, dwell_ms,
                url, referrer, form_payload, extra)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                session_id,
                time.time(),
                client_ts,
                event_type,
                target_id,
                x,
                y,
                dwell_ms,
                url,
                referrer,
                json.dumps(form_payload) if form_payload is not None else None,
                json.dumps(extra) if extra is not None else None,
            ),
        )
        conn.commit()


def events_for_session(session_id: str, db_path: Path | str = DEFAULT_DB) -> list[dict]:
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM events WHERE session_id = ? ORDER BY id", (session_id,)
        ).fetchall()
    return [dict(r) for r in rows]
