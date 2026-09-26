"""Reattach event streams the agent logged under a mistyped session id.

The session id is a 32-character hex token the agent must transcribe into the address bar to
reach the probe. Some backbones get it wrong by one character — gpt-5.6-luna dropped a trailing
digit and substituted another within a single 69-session smoke. The probe then renders under the
bogus id, every click is logged against it, and the real session row shows no events at all.

That is a silent data loss with a bias: the session looks like an agent that reached the probe
and declined to act, which is exactly how a non-leak looks. `run_matrix` fails such a row rather
than scoring it (see `_agent_events`), so nothing wrong is reported — but a real observation is
thrown away, and on a 1,700-session sweep that is tens of sessions.

An orphan stream is adopted only when the evidence is unambiguous:

  * its id differs from exactly ONE recorded session id by a single character (substitution, or
    one extra/missing character at either end), and
  * that session has no agent events of its own, and
  * the orphan's events fall inside that session's run window.

Anything matching two sessions, or none, is left alone and reported. Guessing would attribute a
click to the wrong condition, which is worse than losing it.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

#: Emitted by the probe page itself on load, so they prove nothing about the agent.
PASSIVE = ("pageview", "ready", "visibility")

#: How far outside a session's recorded window an adopted event may fall, in seconds. The row
#: stores a start time and a duration; beacons can land slightly after the agent stops.
_SLACK_S = 120.0


def one_char_apart(a: str, b: str) -> bool:
    """Whether `a` and `b` differ by exactly one character, by substitution or a single edge edit."""
    if a == b:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) == 1
    if abs(len(a) - len(b)) != 1:
        return False
    short, long_ = (a, b) if len(a) < len(b) else (b, a)
    return long_.startswith(short) or long_.endswith(short)


def reconcile(results_db: Path, events_db: Path, apply_changes: bool) -> tuple[int, int]:
    """Adopt every unambiguously-matched orphan stream. Returns (adopted, ambiguous)."""
    r = sqlite3.connect(results_db)
    e = sqlite3.connect(events_db)
    sessions = {sid: (ts, dur or 0.0)
                for sid, ts, dur in r.execute("SELECT session_id, ts, duration_s FROM sessions")}
    ph = ",".join("?" * len(PASSIVE))
    has_agent_events = {s for (s,) in e.execute(
        f"SELECT DISTINCT session_id FROM events WHERE event_type NOT IN ({ph})", PASSIVE)}

    orphans = [s for (s,) in e.execute("SELECT DISTINCT session_id FROM events")
               if s not in sessions and s != "anon"]

    adopted = ambiguous = 0
    for orphan in orphans:
        window = e.execute("SELECT MIN(ts), MAX(ts) FROM events WHERE session_id = ?",
                           (orphan,)).fetchone()
        candidates = []
        for sid, (started, duration) in sessions.items():
            if not one_char_apart(orphan, sid) or sid in has_agent_events:
                continue
            if not (started - _SLACK_S <= window[0] and window[1] <= started + duration + _SLACK_S):
                continue
            candidates.append(sid)
        if len(candidates) != 1:
            if candidates:
                ambiguous += 1
                print(f"  AMBIGUOUS {orphan} -> {candidates}")
            continue
        target = candidates[0]
        print(f"  adopt {orphan} -> {target}")
        if apply_changes:
            e.execute("UPDATE events SET session_id = ? WHERE session_id = ?", (target, orphan))
            r.execute("UPDATE sessions SET error = NULL WHERE session_id = ? AND error LIKE ?",
                      (target, "no agent events%"))
        adopted += 1
    if apply_changes:
        e.commit()
        r.commit()
    r.close()
    e.close()
    return adopted, ambiguous


def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", default=None,
                        help="results/<name>/; defaults to the one bound by SCT_DATASET")
    parser.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    args = parser.parse_args()

    if args.dataset:
        root = Path("results") / args.dataset
        results_db, events_db = root / "results.db", root / "events.db"
    else:
        from orchestrator.config import EVENT_LOG_DB, RESULTS_DB
        results_db, events_db = Path(RESULTS_DB), Path(EVENT_LOG_DB)

    print(f"{results_db.parent.name}  ({'APPLY' if args.apply else 'dry run'})")
    adopted, ambiguous = reconcile(results_db, events_db, args.apply)
    print(f"  adopted={adopted} ambiguous={ambiguous}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
