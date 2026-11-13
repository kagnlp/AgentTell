"""Plant-survival check: did the secret actually get planted before the probe?"""
from __future__ import annotations

import argparse
import contextlib
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness.session import trace_basename
from harness.state_planting import build_plan
from orchestrator.config import DATASET, RESULTS_DB, TRACES_DIR

DELIVERED = "DELIVERED"
ENTERED_ONLY = "ENTERED_ONLY"
NEVER_VISITED = "NEVER_VISITED"
NO_TRACE = "NO_TRACE"

LOGIN_PLANTS = {"plant_bank", "plant_balance", "plant_state"}
POST_LOGIN_PATHS = ("/dashboard", "/home")
ACTION_PLANTS = {"plant_act"}
POST_ACTION_PATHS = ("/do",)


def _visited_urls(path: Path) -> list[str] | None:
    """Every URL the agent's browser was actually on, from the saved trace."""
    try:
        data = json.loads(path.read_text())
    except Exception:
        return None
    return [(step.get("state") or {}).get("url") or ""
            for step in (data.get("history") or [])]


def _origin(url: str) -> str:
    return url.split("/", 3)[2] if url.count("/") >= 2 else ""


def classify(session_id: str, condition: str, variant: str, scenario: str,
             trace_path: Path, plant_id: str = "") -> tuple[str, str]:
    """(verdict, plant_url) for one loaded session."""
    plan = build_plan(session_id, condition, variant, scenario, plant_id=plant_id)
    plant = next((s for s in plan.steps if (s.label or "").startswith("plant")), None)
    if plant is None or not plant.start_url:
        return "NO_PLANT_STEP", ""
    plant_url = plant.start_url
    urls = _visited_urls(trace_path)
    if urls is None:
        return NO_TRACE, plant_url

    visited = {u.split("?")[0].rstrip("/") for u in urls if u}
    if plant_url.split("?")[0].rstrip("/") not in visited:
        return NEVER_VISITED, plant_url
    label = plant.label or ""
    if label in LOGIN_PLANTS:
        needed = POST_LOGIN_PATHS
    elif label in ACTION_PLANTS:
        needed = POST_ACTION_PATHS
    else:
        return DELIVERED, plant_url
    host = _origin(plant_url)
    cleared = any(_origin(u) == host and u.split("?")[0].rstrip("/").endswith(needed)
                  for u in urls if u)
    return (DELIVERED if cleared else ENTERED_ONLY), plant_url


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenarios", nargs="*", default=None)
    ap.add_argument("--verbose", action="store_true", help="one line per failing session")
    args = ap.parse_args()

    with sqlite3.connect(f"file:{RESULTS_DB}?mode=ro", uri=True) as c:
        c.row_factory = sqlite3.Row
        cols = {r[1] for r in c.execute("PRAGMA table_info(sessions)")}
        plant_col = "plant" if "plant" in cols else "'' AS plant"
        rows = c.execute(
            f"SELECT session_id, scenario, condition, variant, {plant_col}, secret_label, "  # noqa: S608  plant_col is one of two literals above
            "trace_path, error, meta FROM sessions ORDER BY scenario, condition").fetchall()
    if args.scenarios:
        rows = [r for r in rows if r["scenario"] in args.scenarios]

    print(f"\n==== PLANT SURVIVAL  (dataset: {DATASET or '(flat results/)'}) ====")
    print("DELIVERED = secret reached the agent's context; ENTERED_ONLY = opened the plant "
          "page but never signed in\n")

    per: dict[tuple[str, str], Counter] = {}
    failures: list[str] = []
    n_cold = 0
    for r in rows:
        if (r["secret_label"] or "none") == "none":
            n_cold += 1
            continue
        guard = "?"
        if r["meta"]:
            with contextlib.suppress(Exception):
                guard = json.loads(r["meta"]).get("guard") or "?"
        tp = Path(r["trace_path"]) if r["trace_path"] else (
            Path(TRACES_DIR) / trace_basename(r["session_id"], r["scenario"],
                                              r["condition"], r["variant"], r["plant"]))
        try:
            verdict, plant_url = classify(r["session_id"], r["condition"], r["variant"],
                                          r["scenario"], tp, r["plant"] or "")
        except Exception as e:
            verdict, plant_url = f"ERROR({type(e).__name__})", ""
        per.setdefault((f"{r['scenario']}/{r['plant'] or '-'}", guard), Counter())[verdict] += 1
        if verdict != DELIVERED:
            failures.append(
                f"  {r['scenario']:22s} {r['condition']:16s} "
                f"{r['plant'] or '-':16s} {r['variant']:9s} "
                f"{verdict:14s} {r['session_id'][:12]}  plant={plant_url or '?'}"
                + (f"  err={r['error']}" if r["error"] else ""))

    width = max((len(s) for s, _ in per), default=10)
    print(f"  {'scenario/plant':{width}s}  {'guard':8s}  {'delivered':>12s}   breakdown")
    for (scen, guard), cnt in sorted(per.items()):
        n = sum(cnt.values())
        d = cnt[DELIVERED]
        other = {k: v for k, v in cnt.items() if k != DELIVERED}
        print(f"  {scen:{width}s}  {guard:8s}  {d:5d}/{n:<5d} {100*d/n:3.0f}%   "
              f"{other or ''}")

    if failures and args.verbose:
        print(f"\n---- {len(failures)} non-delivered session(s) ----")
        print("\n".join(failures))
    elif failures:
        print(f"\n  {len(failures)} non-delivered session(s) — rerun with --verbose to list them")
    print(f"\n  ({n_cold} cold session(s) skipped — no plant step by construction)")


if __name__ == "__main__":
    main()
