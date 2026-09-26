"""Export one dataset's scored results to JSON — the machine-readable twin of RESULTS_SUMMARY.md.

Every run leaves behind two SQLite stores (`results.db`, `events.db`) and a hand-written
RESULTS_SUMMARY.md; nothing wrote the numbers themselves in a form another tool can read. This
module is the missing pipeline step: it scores every scenario present in the dataset with
`analysis.inference.full_report` (which bundles plan.md's three cold-subtracted channel metrics
plus the run bookkeeping) and writes

    results/<dataset>/results.json

with a `headline` table (one row per scenario: ScenarioLeak, CI, verdict, recovered bits) for
quick cross-model comparison, and the full per-scenario reports underneath.

Because `orchestrator.config` binds the dataset at import time, `--dataset` is applied to the
environment BEFORE the analysis modules are imported, and `--all` re-invokes this module once per
dataset in a subprocess.

Usage:
  uv run python -m analysis.export_report                       # current SCT_DATASET
  uv run python -m analysis.export_report --dataset browseruse-qwen3-vl-235b
  uv run python -m analysis.export_report --all                 # every dataset under results/
  uv run python -m analysis.export_report --stdout              # print instead of writing
"""
from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RESULTS_ROOT = REPO_ROOT / "results"
OUTPUT_NAME = "results.json"

# Out of scope for the behavioural headline: the solicited-disclosure baseline (kept in step with
# analysis.channel_metrics.full_channel_report, which skips it for the same reason).
HEADLINE_EXCLUDED = {"authstate_v1"}


def _iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts, UTC).isoformat() if ts else None


def _clean(obj):
    """JSON-safe: NaN/Inf (which json.dumps would emit as invalid JSON literals) become null,
    numpy scalars become plain Python numbers."""
    if isinstance(obj, dict):
        return {str(k): _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    if isinstance(obj, float):
        return None if (math.isnan(obj) or math.isinf(obj)) else obj
    if hasattr(obj, "item") and hasattr(obj, "dtype"):   # numpy scalar
        return _clean(obj.item())
    return obj


def _run_bookkeeping() -> dict:
    """Session counts, error count, wall-clock span, probe hosts and backbone ids — all read
    from results.db, so the report describes the dataset rather than the ambient shell."""
    import sqlite3
    from urllib.parse import urlparse

    from orchestrator.config import RESULTS_DB

    if not Path(RESULTS_DB).exists():
        return {"error": f"no results.db at {RESULTS_DB}"}
    con = sqlite3.connect(RESULTS_DB)
    total, n_err, first_ts, last_ts = con.execute(
        "SELECT COUNT(*), SUM(error IS NOT NULL), MIN(ts), MAX(ts) FROM sessions").fetchone()
    per_cell = {f"{s}/{c}": n for s, c, n in con.execute(
        "SELECT scenario, condition, COUNT(*) FROM sessions GROUP BY scenario, condition")}
    hosts, models = set(), set()
    for (meta,) in con.execute("SELECT DISTINCT meta FROM sessions WHERE meta IS NOT NULL"):
        try:
            m = json.loads(meta)
        except json.JSONDecodeError:
            continue
        if not isinstance(m, dict):
            continue
        if m.get("probe_url"):
            hosts.add(urlparse(m["probe_url"]).hostname or "")
        if m.get("backbone_model"):
            models.add(m["backbone_model"])
    return {
        "n_sessions": int(total or 0),
        "n_errors": int(n_err or 0),
        "agents": [r[0] for r in con.execute("SELECT DISTINCT agent FROM sessions")],
        "llm_keys": [r[0] for r in con.execute("SELECT DISTINCT llm FROM sessions")],
        "backbone_models": sorted(models),
        "probe_hosts": sorted(h for h in hosts if h),
        "first_session_utc": _iso(first_ts),
        "last_session_utc": _iso(last_ts),
        "sessions_per_cell": per_cell,
    }


def _scenarios_present() -> list[str]:
    """Registered scenarios that actually have sessions in this dataset, in registry order."""
    import sqlite3

    from harness.scenarios import SCENARIOS
    from orchestrator.config import RESULTS_DB

    if not Path(RESULTS_DB).exists():
        return []
    con = sqlite3.connect(RESULTS_DB)
    have = {r[0] for r in con.execute(
        "SELECT DISTINCT scenario FROM sessions WHERE scenario IS NOT NULL")}
    return [k for k in SCENARIOS if k in have]


def _headline_row(key: str, report: dict) -> dict:
    """One flat row per scenario — the RESULTS_SUMMARY.md table, machine-readable."""
    ch = report.get("channel_headline", {})
    # criterion (a) comes from the ask_only arm alone — see refusal_rate_on_direct_request.
    ask = (report.get("refusal_on_direct_request", {}).get("by_variant", {})
           .get("ask_only", {}))
    return {
        "scenario": key,
        "rig": ch.get("rig"),
        "secret_class": report.get("secret_class"),
        "k_options": ch.get("k_options"),
        "n_behavioural": ch.get("n_behavioural"),
        "n_cold": ch.get("n_cold"),
        "scenario_leak": ch.get("scenario_leak"),
        "ci95": ch.get("scenario_leak_ci95"),
        "verdict": ch.get("verdict", "insufficient-data"),
        "recovered_info_bits": ch.get("recovered_info_bits"),
        "max_info_bits": ch.get("max_info_bits"),
        # Validity check: a guarded arm whose abstention jumps is uninterpretable, not a
        # closed channel. Read alongside scenario_leak, never instead of it.
        "abstention_load": ch.get("abstention", {}).get("load"),
        "abstention_cold": ch.get("abstention", {}).get("cold"),
        # Criterion (a): did the agent name the secret when the box was the only affordance?
        "ask_only_n": ask.get("n_scorable"),
        "ask_only_disclosure_rate": ask.get("disclosure_rate"),
        # Per plant route. A scenario plants the same secret several ways, and a pooled figure
        # cannot distinguish "this model does not leak" from "one of these plants never put the
        # value in context" — so the breakdown travels with the headline, not beside it.
        "leak_by_plant": {p: m.get("scenario_leak") for p, m in
                          (ch.get("per_plant") or {}).items()},
        # Correct-the-default rig only. When this is present and zero, the loaded and cold cells
        # below it measure nothing and must not be read as a protective disposition.
        "capability_arm": ch.get("capability_arm") or None,
    }


def build_report(n_boot: int = 3000, backbone_model: str | None = None,
                 backbone_fallback: str | None = None) -> dict:
    """Score every scenario present in the active dataset and assemble the JSON bundle."""
    from analysis.channel_metrics import scenario_metrics
    from analysis.features import build_feature_table
    from analysis.inference import full_report
    from orchestrator.config import DATASET, EVENT_LOG_DB, RESULTS_DB

    run = _run_bookkeeping()
    keys = _scenarios_present()
    scenarios: dict[str, dict] = {}
    headline: list[dict] = []
    for key in keys:
        # Build the feature table once and hand it to both scorers; `n_boot` only reaches
        # channel_metrics through the direct call, so re-score the headline with it.
        df = build_feature_table(key)
        rep = full_report(key, df=df)
        if "error" not in rep:
            rep["channel_headline"] = scenario_metrics(key, df=df, n_boot=n_boot)
        scenarios[key] = rep
        if key not in HEADLINE_EXCLUDED and "error" not in rep:
            headline.append(_headline_row(key, rep))

    # The backbone comes from the sessions themselves (run_matrix stamps it into meta).
    # `backbone_fallback` labels pre-stamp datasets and is only supplied when the caller knows
    # it applies to THIS dataset — otherwise a stale $OPENROUTER_MODEL would mislabel the data.
    models = [backbone_model] if backbone_model else (run.get("backbone_models") or [])
    if not models and backbone_fallback:
        models = [backbone_fallback]
    hosts = run.get("probe_hosts") or []

    return _clean({
        "dataset": DATASET or "(default)",
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "backbone_model": models[0] if len(models) == 1 else (models or None),
        "attacker_host": hosts[0] if len(hosts) == 1 else (hosts or None),
        "n_boot": n_boot,
        "sources": {"results_db": str(RESULTS_DB), "events_db": str(EVENT_LOG_DB)},
        "run": run,
        "scenarios_scored": keys,
        "headline": headline,
        "scenarios": scenarios,
    })


def export(n_boot: int = 3000, to_stdout: bool = False,
           backbone_model: str | None = None,
           backbone_fallback: str | None = None) -> Path | None:
    from orchestrator.config import DATA_ROOT

    report = build_report(n_boot=n_boot, backbone_model=backbone_model,
                          backbone_fallback=backbone_fallback)
    text = json.dumps(report, indent=2, allow_nan=False)
    if to_stdout:
        print(text)
        return None
    out = Path(DATA_ROOT) / OUTPUT_NAME
    out.write_text(text + "\n")
    return out


def _known_datasets() -> list[str]:
    return sorted(p.parent.name for p in RESULTS_ROOT.glob("*/results.db"))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dataset", default=None,
                    help="dataset name under results/ (default: $SCT_DATASET)")
    ap.add_argument("--all", action="store_true",
                    help="export every results/<dataset>/ that has a results.db")
    ap.add_argument("--n-boot", type=int, default=3000)
    ap.add_argument("--backbone-model", default=None,
                    help="label the report with this backbone id (e.g. openai/gpt-5.2); only "
                         "needed for datasets recorded before run_matrix stamped it into meta")
    ap.add_argument("--stdout", action="store_true", help="print the JSON instead of writing it")
    args = ap.parse_args()

    if args.all:
        datasets = _known_datasets()
        if not datasets:
            print(f"no datasets found under {RESULTS_ROOT}", file=sys.stderr)
            return 1
        rc = 0
        for ds in datasets:
            print(f"===== {ds} =====", file=sys.stderr)
            rc |= subprocess.call(
                [sys.executable, "-m", "analysis.export_report", "--dataset", ds,
                 "--n-boot", str(args.n_boot)] + (["--stdout"] if args.stdout else []),
                cwd=REPO_ROOT)
        return rc

    # Bind the dataset BEFORE orchestrator.config is imported (it reads SCT_DATASET at import).
    # $OPENROUTER_MODEL describes the shell's dataset, so it may only label the report when the
    # dataset came from that same shell (no --dataset override).
    fallback = None if args.dataset else os.getenv("OPENROUTER_MODEL")
    if args.dataset:
        os.environ["SCT_DATASET"] = args.dataset

    out = export(n_boot=args.n_boot, to_stdout=args.stdout,
                 backbone_model=args.backbone_model, backbone_fallback=fallback)
    if out is not None:
        from orchestrator.config import DATASET
        print(f"[{DATASET or '(default)'}] wrote {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
