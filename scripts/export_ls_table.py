"""Export the Leakage Score of every scenario on every backbone, with 95% bootstrap intervals.

The Leakage Score (LS) is the paper's main metric (Section 4, "Leakage Score") and plan.md's
Metric 2, the Scenario Leak Score: for each held value s, the fraction of loaded sessions that
take a(s) minus the fraction of cold sessions that take a(s), averaged over the k held values
and reported in percentage points. The scenario-level score pools each held value's loaded
sessions over the five tasks (analysis/channel_metrics.scenario_leak) and carries a percentile
bootstrap CI (channel_metrics.scenario_leak_ci). The task-level score LS_j is the same statistic
on the loaded sessions of one plant route against the shared cold pool
(channel_metrics.plant_metrics). This script computes no metric itself. It runs the existing
`python -m analysis.channel_metrics` CLI once per finalized dataset, converts its fractions to
percentage points, and collects the output into one JSON file, plus optional LaTeX table rows
for the paper appendix.

Two isolation rules hold for every dataset:

- The dataset namespace is bound once, at import, by orchestrator.config. Each dataset therefore
  runs in its own subprocess with SCT_DATASET set before anything is imported.
- The recorded databases are never opened. The analysis connectors open SQLite read-write and set
  WAL mode, which can rewrite a tracked database file. Each dataset's databases are byte-copied
  into a temporary directory under results/ and the subprocess reads the copy.

PYTHONHASHSEED is fixed because channel_metrics.scenario_leak_ci iterates the held-value groups
in set order, so its interval endpoints reproduce only under a fixed hash seed.

Usage:
  uv run python scripts/export_ls_table.py
  uv run python scripts/export_ls_table.py --n-boot 3000 \
      --out appendix_material/ls_scores.json --latex appendix_material/ls_tables.tex
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RESULTS_ROOT = REPO_ROOT / "results"

sys.path.insert(0, str(REPO_ROOT))

from harness.scenarios import get_scenario  # noqa: E402

logger = logging.getLogger(__name__)

#: Finalized dataset directory under results/ -> backbone name used in the paper, in the order
#: Section 4 lists the backbones.
BACKBONES: dict[str, str] = {
    "final-browseruse-claude-sonnet-5": "Claude",
    "final-browseruse-gemini-3.7-flash": "Gemini",
    "final-browseruse-gpt-5.6-luna": "GPT-5.6",
    "final-browseruse-qwen3-vl-235b": "Qwen",
    "final-browseruse-glm-4.6v": "GLM",
    "final-browseruse-kimi-k2.6": "Kimi",
}

#: Registry key -> scenario name exactly as in the paper's Table 1 (tab:scenarios), in SC order.
#: The position in this mapping gives the scenario ID, so SC01 is the first entry.
PAPER_SCENARIOS: dict[str, str] = {
    "sso_wall": "Single-sign-on provider",
    "checkout": "Bank identity",
    "account_deanon": "Account de-anonymisation",
    "health_service": "Health-provider identity",
    "sensitive_access": "Sensitive reading interest",
    "affiliation": "Organisational affiliation",
    "tier_gated": "Subscription tier",
    "mfa_config": "Second factor",
    "recovery_channel": "Recovery route",
    "balance_threshold": "Balance band",
    "credit_standing": "Credit source",
    "profession_gated": "Occupation",
    "stigma_platform": "Stigma-associated service",
    "privilege_role": "Privilege level",
    "content_ownership": "Document ownership",
    "life_event": "Recent life event",
    "account_age": "Account tenure",
    "confidential_role": "Confidential assignment",
    "locale_default": "Locale",
    "vendor_session": "Consumer-service identity",
}

#: Leakage Scores are reported in percentage points; channel_metrics returns fractions.
PERCENTAGE_POINTS = 100.0

_DB_FILES = ("results.db", "events.db")
_SIDECARS = ("", "-wal", "-shm")


class ExportError(RuntimeError):
    """The analysis could not produce a complete, trustworthy table for a dataset."""


def scenario_id(key: str) -> str:
    """Return the paper's scenario ID (SC01..SC20) for a registry key."""
    return f"SC{list(PAPER_SCENARIOS).index(key) + 1:02d}"


def task_ids(key: str) -> dict[str, str]:
    """Map each plant route of a scenario to its paper task ID, in registry plant order.

    Raises:
        ExportError: If the scenario does not declare exactly five plant routes.
    """
    plants = list(get_scenario(key).plant_ids)
    if len(plants) != 5:
        raise ExportError(f"{key}: expected 5 plant routes, found {len(plants)}: {plants}")
    return {plant: f"{scenario_id(key)}{letter}" for plant, letter in zip(plants, "abcde")}


def _copy_databases(source: Path, target: Path) -> None:
    for db in _DB_FILES:
        if not (source / db).exists():
            raise ExportError(f"{source.name}: {db} is missing")
        for suffix in _SIDECARS:
            path = source / f"{db}{suffix}"
            if path.exists():
                shutil.copy2(path, target / path.name)


def run_channel_metrics(dataset: str, n_boot: int, hash_seed: int) -> list[dict]:
    """Run the channel-metrics CLI on a copy of one finalized dataset and return its report.

    Args:
        dataset: Directory name under results/, for example "final-browseruse-kimi-k2.6".
        n_boot: Bootstrap resamples for the scenario-level and task-level intervals.
        hash_seed: PYTHONHASHSEED for the subprocess, which fixes the resampling group order.

    Raises:
        ExportError: If the dataset is incomplete or the analysis subprocess fails.
    """
    with tempfile.TemporaryDirectory(dir=RESULTS_ROOT, prefix=".ls-export-") as tmp:
        copy_dir = Path(tmp)
        _copy_databases(RESULTS_ROOT / dataset, copy_dir)
        env = dict(os.environ)
        env["SCT_DATASET"] = str(copy_dir.relative_to(RESULTS_ROOT))
        env["PYTHONHASHSEED"] = str(hash_seed)
        logger.info("scoring %s with n_boot=%d", dataset, n_boot)
        proc = subprocess.run(  # noqa: S603  fixed interpreter and module, no user input
            [sys.executable, "-m", "analysis.channel_metrics", "--n-boot", str(n_boot)],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
    if proc.returncode != 0:
        raise ExportError(f"{dataset}: channel_metrics failed:\n{proc.stderr[-2000:]}")
    return json.loads(proc.stdout)


def _pp(value: float) -> float:
    return PERCENTAGE_POINTS * value


def _interval(values: Sequence[float]) -> list[float]:
    lo, hi = values
    if math.isnan(lo) or math.isnan(hi):
        raise ExportError(f"undefined confidence interval {values}")
    return [_pp(lo), _pp(hi)]


def _per_candidate(rates: Mapping[str, Mapping]) -> dict[str, dict]:
    """Per held value: the loaded and cold matching fractions and LS_s in percentage points."""
    return {s: {"p_load": r["p_load"], "p_cold": r["p_cold"], "ls": _pp(r["LR"]),
                "n_loaded": r["n_load"]}
            for s, r in rates.items()}


def scenario_record(key: str, report: Mapping) -> dict:
    """Reduce one scenario's channel-metrics report to the fields the appendix reports.

    All Leakage Scores and interval bounds are in percentage points.

    Raises:
        ExportError: If the report carries an error or lacks a task the design requires.
    """
    if "error" in report:
        raise ExportError(f"{key}: {report['error']}")
    tasks = []
    for plant, task_id in task_ids(key).items():
        per_task = report["per_plant"].get(plant)
        if per_task is None:
            raise ExportError(f"{key}: no sessions for plant route {plant} ({task_id})")
        tasks.append({
            "task_id": task_id,
            "plant": plant,
            "ls": _pp(per_task["scenario_leak"]),
            "ci95": _interval(per_task["scenario_leak_ci95"]),
            "n_loaded": per_task["n_loaded"],
            "n_cold": per_task["n_cold"],
            "verdict": per_task["verdict"],
        })
    return {
        "scenario_id": scenario_id(key),
        "key": key,
        "name": PAPER_SCENARIOS[key],
        "k": report["k_options"],
        "ls": _pp(report["scenario_leak"]),
        "ci95": _interval(report["scenario_leak_ci95"]),
        "n_loaded": report["n_behavioural"] - report["n_cold"],
        "n_cold": report["n_cold"],
        "null_share": report["abstention"],
        "verdict": report["verdict"],
        "per_candidate": _per_candidate(report["leak_rate_per_target"]),
        "capability_arm": report.get("capability_arm") or None,
        "tasks": tasks,
    }


def _cross_check(dataset: str, scenarios: Mapping[str, Mapping]) -> dict:
    """Compare the recomputed scores with the dataset's stored results.json (a fraction)."""
    stored_path = RESULTS_ROOT / dataset / "results.json"
    if not stored_path.exists():
        logger.warning("%s: no results.json to cross-check against", dataset)
        return {"results_json": None, "backbone_model": None, "max_abs_diff_pp": None,
                "matches": None}
    stored = json.loads(stored_path.read_text())
    diffs = []
    for key, record in scenarios.items():
        headline = stored["scenarios"].get(key, {}).get("channel_headline", {})
        if "scenario_leak" not in headline:
            raise ExportError(f"{dataset}: results.json has no scenario score for {key}")
        diffs.append(abs(_pp(headline["scenario_leak"]) - record["ls"]))
    max_diff = max(diffs)
    if max_diff > 1e-7:
        logger.warning("%s: recomputed LS differs from results.json by up to %.4f pp",
                       dataset, max_diff)
    return {"results_json": str(stored_path.relative_to(REPO_ROOT)),
            "backbone_model": stored.get("backbone_model"),
            "max_abs_diff_pp": max_diff, "matches": max_diff <= 1e-7}


def export_backbone(dataset: str, n_boot: int, hash_seed: int) -> dict:
    """Score one finalized dataset and return its LS records for the 20 paper scenarios."""
    by_key = {r["scenario"]: r for r in run_channel_metrics(dataset, n_boot, hash_seed)}
    missing = [k for k in PAPER_SCENARIOS if k not in by_key]
    if missing:
        raise ExportError(f"{dataset}: channel_metrics returned no report for {missing}")
    scenarios = {key: scenario_record(key, by_key[key]) for key in PAPER_SCENARIOS}
    scores = [r["ls"] for r in scenarios.values()]
    return {
        "backbone": BACKBONES[dataset],
        "dataset": f"results/{dataset}",
        "overall_ls": sum(scores) / len(scores),
        "n_loaded": sum(r["n_loaded"] for r in scenarios.values()),
        "n_cold": sum(r["n_cold"] for r in scenarios.values()),
        "cross_check": _cross_check(dataset, scenarios),
        "scenarios": scenarios,
    }


def build_export(n_boot: int, hash_seed: int, jobs: int) -> dict:
    """Score every finalized dataset and assemble the combined export."""
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        futures = {d: pool.submit(export_backbone, d, n_boot, hash_seed) for d in BACKBONES}
        backbones = {BACKBONES[d]: f.result() for d, f in futures.items()}
    return {
        "metric": "Leakage Score (LS), percentage points: 100 times the mean over held values s "
                  "of p_load(s) - p_cold(s), loaded sessions pooled over the five tasks; "
                  "analysis/channel_metrics.py",
        "units": "percentage points for every ls, ci95 and overall_ls field; p_load, p_cold "
                 "and null_share are fractions",
        "generated_at_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "n_boot": n_boot,
        "pythonhashseed": hash_seed,
        "ci": "percentile bootstrap, 2.5 and 97.5 percentiles, resampling sessions with "
              "replacement within each held-value group and within the cold pool",
        "backbones": backbones,
    }


def _fmt(value: float) -> str:
    text = f"{value:.1f}"
    if text == "-0.0":
        return "0.0"
    return f"$-${text[1:]}" if text.startswith("-") else text


def _mark(verdict: str) -> str:
    return {"channel": "", "inverted": "$^\\ddagger$"}.get(verdict, "$^\\dagger$")


def to_latex(export: Mapping) -> str:
    """Render the scenario-level and task-level Leakage Score table bodies for the appendix.

    The scenario rows use the appendix macro \\lsci{LS}{low}{high}. A dagger marks an interval
    whose lower bound is not above zero, and a double dagger an interval entirely below zero.
    """
    backbones = list(export["backbones"].values())
    lines = ["% Generated by scripts/export_ls_table.py. Do not edit by hand.",
             "% --- scenario-level rows (Table: Leakage Score with 95% CI) ---"]
    for key in PAPER_SCENARIOS:
        first = backbones[0]["scenarios"][key]
        cells = []
        for b in backbones:
            r = b["scenarios"][key]
            cells.append(f"\\lsci{{{_fmt(r['ls'])}{_mark(r['verdict'])}}}"
                         f"{{{_fmt(r['ci95'][0])}}}{{{_fmt(r['ci95'][1])}}}")
        lines.append(f"{first['scenario_id']} & {first['name']} & {first['k']} & "
                     + " & ".join(cells) + " \\\\")
    lines.append("\\midrule")
    lines.append("\\multicolumn{3}{@{}l}{Overall score (mean over 20 scenarios)} & "
                 + " & ".join(_fmt(b["overall_ls"]) for b in backbones) + " \\\\")
    lines.append("% --- task-level rows (Table: task-level Leakage Score) ---")
    for key in PAPER_SCENARIOS:
        first = backbones[0]["scenarios"][key]
        lines.append(f"\\multicolumn{{7}}{{@{{}}l}}{{\\textit{{{first['scenario_id']} "
                     f"{first['name']}}}}} \\\\")
        for i, task in enumerate(first["tasks"]):
            cells = []
            for b in backbones:
                t = b["scenarios"][key]["tasks"][i]
                cells.append(f"{_fmt(t['ls'])}{_mark(t['verdict'])}")
            lines.append(f"{task['task_id']} & " + " & ".join(cells) + " \\\\")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--n-boot", type=int, default=3000)
    ap.add_argument("--hash-seed", type=int, default=0)
    ap.add_argument("--jobs", type=int, default=3, help="datasets scored in parallel")
    ap.add_argument("--out", type=Path, default=REPO_ROOT / "appendix_material/ls_scores.json")
    ap.add_argument("--latex", type=Path, default=None,
                    help="also write the appendix table rows to this file")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    export = build_export(args.n_boot, args.hash_seed, args.jobs)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(export, indent=2) + "\n")
    print(f"wrote {args.out}")
    if args.latex is not None:
        args.latex.parent.mkdir(parents=True, exist_ok=True)
        args.latex.write_text(to_latex(export))
        print(f"wrote {args.latex}")
    for b in export["backbones"].values():
        check = b["cross_check"]
        print(f"{b['backbone']:8s} overall LS {b['overall_ls']:5.1f}  "
              f"loaded {b['n_loaded']}  cold {b['n_cold']}  "
              f"matches results.json: {check['matches']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
