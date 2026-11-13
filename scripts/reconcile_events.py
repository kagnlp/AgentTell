"""Reattach event streams the agent logged under a mistyped session id."""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PASSIVE = ("pageview", "ready", "visibility")

_SLACK_S = 120.0


def one_char_apart(a: str, b: str) -> bool:
    """Whether `a` and `b` differ by exactly one character, by substitution or a single edge edit.
    """
    if a == b:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) == 1
    if abs(len(a) - len(b)) != 1:
        return False
    short, long_ = (a, b) if len(a) < len(b) else (b, a)
    return long_.startswith(short) or long_.endswith(short)


def reconcile(results_db: Path, events_db: Path, apply_changes: bool) -> tuple[int, int]:
    """Adopt every unambiguously-matched orphan stream."""
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
