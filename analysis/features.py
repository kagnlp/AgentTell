"""Feature extraction: join the attacker event log + results store into a per-session feature vector
for scoring.
"""
from __future__ import annotations

import pandas as pd

from attacker.db import connect as events_connect
from attacker.db import init_db as events_init
from harness.scenarios import get_scenario
from orchestrator.config import EVENT_LOG_DB, RESULTS_DB
from orchestrator.results_store import connect as results_connect
from orchestrator.results_store import init_db as results_init


def _events_df() -> pd.DataFrame:
    events_init(EVENT_LOG_DB)
    with events_connect(EVENT_LOG_DB) as c:
        rows = c.execute("SELECT * FROM events").fetchall()
    return pd.DataFrame([dict(r) for r in rows])


def _sessions_df() -> pd.DataFrame:
    """Every scorable session — i.e. every session that actually RAN."""
    results_init(RESULTS_DB)
    with results_connect(RESULTS_DB) as c:
        rows = c.execute("SELECT * FROM sessions WHERE error IS NULL").fetchall()
    return pd.DataFrame([dict(r) for r in rows])


def _id_from_target(target_id: str | None, ids: list[str]) -> str | None:
    if not target_id:
        return None
    for b in ids:
        if b in target_id:
            return b
    return None


def _session_features(ev: pd.DataFrame, sc, neutral_ids: list[str]) -> dict:
    """Compute one feature vector from one session's event rows."""
    feats: dict[str, float] = {}
    ids = sc.label_ids

    clicks = ev[ev.event_type == "click"]

    for b in ids:
        feats[f"click_{b}"] = float(
            clicks.target_id.apply(lambda t: _id_from_target(t, ids)).eq(b).sum()
        ) if not clicks.empty else 0.0

    navs = ev[ev.event_type == "nav"]
    nav_target = None
    if not navs.empty:
        for tid in navs.target_id:
            nav_target = tid
            break
    nav_label = _id_from_target(nav_target, ids)
    for b in ids:
        feats[f"nav_{b}"] = 1.0 if nav_label == b else 0.0

    neutral_nav = _id_from_target(nav_target, neutral_ids) is not None
    neutral_click = (not clicks.empty and
                     clicks.target_id.apply(lambda t: _id_from_target(t, neutral_ids))
                     .notna().any())
    feats["picked_neutral"] = 1.0 if (neutral_nav or neutral_click) else 0.0

    return feats


def build_feature_table(scenario_key: str) -> pd.DataFrame:
    """One row per session of the given scenario: features + ground-truth labels."""
    sc = get_scenario(scenario_key)
    neutral_ids = [a.id for a in sc.filler]
    sessions = _sessions_df()
    events = _events_df()
    if sessions.empty:
        return pd.DataFrame()

    sessions = sessions[sessions["scenario"] == scenario_key]
    if sessions.empty:
        return pd.DataFrame()

    rows = []
    for _, s in sessions.iterrows():
        sid = s["session_id"]
        ev = events[events.session_id == sid] if not events.empty else pd.DataFrame()
        feats = _session_features(ev, sc, neutral_ids) if not ev.empty else {}

        secret = s.get("secret_label") if "secret_label" in s else None
        if not secret:
            secret = sc.label_for_condition(s["condition"])
        variant = s.get("variant") if "variant" in s else "A"
        plant_id = (s.get("plant") if "plant" in s else "") or ""
        feats.update({
            "session_id": sid,
            "agent": s["agent"],
            "llm": s["llm"],
            "condition": s["condition"],
            "variant": variant,
            "scenario": scenario_key,
            "plant": plant_id,
            "secret_label": secret,
            "n_events": int(len(ev)),
        })
        rows.append(feats)

    return pd.DataFrame(rows).fillna(0.0)

