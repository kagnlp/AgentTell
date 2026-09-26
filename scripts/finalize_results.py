"""Build the balanced, cross-model-comparable copy of the behavioural datasets.

The four backbones were swept against the same matrix, but they did not come back with the same
number of usable sessions: a dead session (backbone timeout, HTTP 402, a crash) is an errored row
that `analysis.features` correctly refuses to score, so a model that failed more often ends up
with thinner cells. Reporting the raw per-model totals side by side would compare 1,747 sessions
of one model against 1,738 of another over a DIFFERENT set of cells, and any difference in
ScenarioLeak would then be part model and part sample.

This module writes `results/final-<dataset>/` — a full copy of each source dataset whose
`results.db` has been reduced to a design that is IDENTICAL across the models being balanced:

  * errored rows are removed (they are not observations),
  * arms other than the behavioural one (`variant != "A"`) are removed,
  * scenarios outside `SPEC_V2` are removed,
  * every remaining (scenario, condition, plant) cell is trimmed to the same count in every
    model — `min(design target, the fewest usable sessions any model has for that cell)`.

Nothing is dropped silently: every removed row is counted by reason in `BALANCE.md` and
`balance.json` next to the filtered database, and every cell still below its design target is
listed there with the number of sessions that would fill it. When those top-ups have been run,
re-running this script yields the full design (3 reps per loaded cell) with no trimming at all,
because the floor then equals the target.

The design targets come from `orchestrator.coverage.wanted_cells`, the same function the sweep
plans against, so the finalized denominator cannot drift from what the sweep was asked to run.

Usage:
  uv run python scripts/finalize_results.py --plan          # report only, write nothing
  uv run python scripts/finalize_results.py                 # build results/final-<dataset>/
  uv run python scripts/finalize_results.py --datasets browseruse-a browseruse-b
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
import subprocess
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from harness.scenarios import SCENARIOS  # noqa: E402  needs sys.path above
from orchestrator.coverage import DENSE_COLD, wanted_cells  # noqa: E402

RESULTS_ROOT = REPO_ROOT / "results"
FINAL_PREFIX = "final-"

#: The 20 scenarios of `scenario-spec/new-scenario-v2.md`, in the spec's own §1–§20 order.
#: `authstate_v1` (the solicited-disclosure contrast case), `checkout_neutral` (§2's framing
#: control) and `locale_region` (§19's choice-rig twin) are registered and still run, but they are
#: not part of the 20 the report covers, so they are excluded here and counted as such.
SPEC_V2 = (
    "sso_wall", "checkout", "account_deanon", "health_service", "sensitive_access",
    "affiliation", "tier_gated", "mfa_config", "recovery_channel", "balance_threshold",
    "credit_standing", "profession_gated", "stigma_platform", "privilege_role",
    "content_ownership", "life_event", "account_age", "confidential_role", "locale_default",
    "vendor_session",
)

#: The behavioural arm. The `direct` / `ask_only` disclosure arms answer a different question and
#: are balanced separately, if at all.
BEHAVIOURAL_VARIANT = "A"

DEFAULT_DATASETS = (
    "browseruse-claude-sonnet-5",
    "browseruse-gemini-3.7-flash",
    "browseruse-gpt-5.6-luna",
    "browseruse-kimi-k2.6",
    "browseruse-qwen3-vl-235b",
)

#: Why a source row is not in the finalized database. Order is the order the reasons are applied.
DROP_ERRORED = "errored"
DROP_OTHER_ARM = "other_arm"
DROP_OFF_SPEC = "scenario_outside_spec_v2"
DROP_OFF_DESIGN = "cell_not_in_current_design"
DROP_SURPLUS = "surplus_above_common_floor"


@dataclass(frozen=True, slots=True)
class Cell:
    """One matrix cell: a scenario's condition on one plant route (empty plant = no route)."""
    scenario: str
    condition: str
    plant: str


def design_targets(scenarios: tuple[str, ...], reps: int, cold_reps: int,
                   dense_cold_reps: int) -> dict[Cell, int]:
    """Sessions the design asks for in every cell of every named scenario.

    Raises:
        KeyError: If a name is not in the scenario registry, which means the roster above and
            `benchmark/scenarios/index.yaml` have diverged.
    """
    targets: dict[Cell, int] = {}
    for key in scenarios:
        sc = SCENARIOS[key]
        cold = dense_cold_reps if key in DENSE_COLD else cold_reps
        cap = cold_reps if sc.rig == "correct" else 0
        for (condition, plant), n in wanted_cells(sc, reps, cold, cap).items():
            targets[Cell(key, condition, plant)] = n
    return targets


def usable_sessions(db_path: Path) -> dict[Cell, list[str]]:
    """Session ids of every scorable behavioural session, per cell, in design order.

    Ordered by rep then start time, so trimming a cell keeps its FIRST repetitions and the choice
    of which sessions survive is deterministic rather than a property of how the rows were
    written.
    """
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT scenario, condition, COALESCE(plant, ''), session_id FROM sessions "
            "WHERE error IS NULL AND variant = ? "
            "ORDER BY scenario, condition, COALESCE(plant, ''), rep, ts, session_id",
            (BEHAVIOURAL_VARIANT,),
        ).fetchall()
    finally:
        connection.close()
    per_cell: dict[Cell, list[str]] = defaultdict(list)
    for scenario, condition, plant, session_id in rows:
        per_cell[Cell(scenario or "authstate_v1", condition, plant)].append(session_id)
    return dict(per_cell)


def common_floor(targets: dict[Cell, int],
                 have: dict[str, dict[Cell, list[str]]]) -> dict[Cell, int]:
    """How many sessions each cell keeps: the target, or the thinnest model if that is thinner.

    A cell no model reached at all is absent from the result rather than present at zero, so the
    caller can report it as missing coverage instead of as a balanced empty cell.
    """
    floor: dict[Cell, int] = {}
    for cell, target in targets.items():
        n = min(len(have[dataset].get(cell, ())) for dataset in have)
        if n:
            floor[cell] = min(target, n)
    return floor


def shortfall(targets: dict[Cell, int], have: dict[Cell, list[str]]) -> dict[Cell, int]:
    """Sessions still missing, per cell, before this dataset meets the design."""
    return {cell: target - len(have.get(cell, ()))
            for cell, target in targets.items() if len(have.get(cell, ())) < target}


def trimmed_cells(targets: dict[Cell, int], floor: dict[Cell, int],
                  have: dict[str, dict[Cell, list[str]]]) -> list[dict]:
    """Every cell the balancing holds below its design target, and what each model had.

    Identical in all finalized datasets by construction — it describes the shared reduced design,
    not one model's sample — and it is the list that `orchestrator.coverage` will plan once the
    missing sessions are run.
    """
    rows = []
    for cell, target in sorted(targets.items(),
                               key=lambda kv: (kv[0].scenario, kv[0].condition, kv[0].plant)):
        kept = floor.get(cell, 0)
        if kept >= target:
            continue
        rows.append({
            "scenario": cell.scenario, "condition": cell.condition, "plant": cell.plant,
            "kept": kept, "target": target,
            "available": {dataset: len(have[dataset].get(cell, ())) for dataset in have},
        })
    return rows


def filter_database(db_path: Path, keep: set[str]) -> Counter[str]:
    """Delete every session outside `keep` from a COPIED results.db, and report why.

    Args:
        db_path: The copy to filter. Never the source database.
        keep: Session ids to retain.

    Returns:
        Row counts per drop reason, plus `kept`.
    """
    connection = sqlite3.connect(db_path)
    try:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        rows = connection.execute(
            "SELECT session_id, scenario, error, variant FROM sessions").fetchall()
        tally: Counter[str] = Counter()
        doomed: list[tuple[str]] = []
        for session_id, scenario, error, variant in rows:
            if session_id in keep:
                tally["kept"] += 1
                continue
            if error is not None:
                reason = DROP_ERRORED
            elif variant != BEHAVIOURAL_VARIANT:
                reason = DROP_OTHER_ARM
            elif (scenario or "authstate_v1") not in SPEC_V2:
                reason = DROP_OFF_SPEC
            else:
                reason = DROP_SURPLUS
            tally[reason] += 1
            doomed.append((session_id,))
        connection.executemany("DELETE FROM sessions WHERE session_id = ?", doomed)
        connection.commit()
        connection.execute("VACUUM")
    finally:
        connection.close()
    return tally


#: Trace files are named `<scenario>-<condition>-<plant>-<variant>-<first 8 of session id>.json`.
#: The id prefix is the only part that identifies the session, and it is unique within a dataset.
_TRACE_ID = re.compile(r"-([0-9a-f]{8})\.json$")


def prune_traces(destination: Path, keep: set[str]) -> dict[str, int]:
    """Reduce a copied `traces/` to exactly the retained sessions and repoint `trace_path` at it.

    Matching is on the session-id prefix in the filename, NOT on `trace_path`: cold sessions write
    a trace but record no path (see `orchestrator.run_matrix.run_cell`), so pruning by the column
    would delete a quarter of the retained traces — every cold baseline in the set.

    Args:
        destination: The finalized dataset directory. Never the source.
        keep: Session ids retained in the filtered database.

    Returns:
        Counts of files kept and removed, and retained sessions that have no trace at all.
    """
    traces = destination / "traces"
    if not traces.is_dir():
        return {"trace_files_kept": 0, "trace_files_removed": 0,
                "sessions_without_trace": len(keep)}

    prefixes = {session_id[:8] for session_id in keep}
    kept, removed, matched = 0, 0, set()
    for path in traces.iterdir():
        found = _TRACE_ID.search(path.name)
        if found and found.group(1) in prefixes:
            kept += 1
            matched.add(found.group(1))
        else:
            path.unlink()
            removed += 1

    # The copied rows still point into the SOURCE dataset, so a reader of this folder would open
    # the unbalanced tree's files. Rewrite them onto the copy that was just pruned.
    connection = sqlite3.connect(destination / "results.db")
    try:
        rows = connection.execute(
            "SELECT session_id, trace_path FROM sessions "
            "WHERE trace_path IS NOT NULL AND trace_path != ''").fetchall()
        connection.executemany(
            "UPDATE sessions SET trace_path = ? WHERE session_id = ?",
            [(str(traces / Path(old).name), session_id) for session_id, old in rows])
        connection.commit()
    finally:
        connection.close()

    return {"trace_files_kept": kept, "trace_files_removed": removed,
            "sessions_without_trace": len(prefixes - matched)}


def copy_dataset(source: Path, destination: Path, force: bool) -> None:
    """Copy a whole dataset directory, backups and traces included.

    Raises:
        FileExistsError: If the destination exists and `force` is not set. Overwriting a finalized
            dataset in place would silently rebase a number someone may already have quoted.
    """
    if destination.exists():
        if not force:
            raise FileExistsError(f"{destination} exists; pass --force to rebuild it")
        shutil.rmtree(destination)
    shutil.copytree(source, destination)


def _stale_banner(destination: Path) -> None:
    """Mark a copied hand-written RESULTS.md as describing the pre-balance sample."""
    stale = destination / "RESULTS.md"
    if not stale.exists():
        return
    banner = ("> **Superseded.** This summary describes the UNBALANCED source dataset. The "
              "balanced, cross-model-comparable numbers are in `results.json`, and what was "
              "removed to get there is in `BALANCE.md`.\n\n")
    stale.write_text(banner + stale.read_text())


def _balance_document(dataset: str, tally: Counter[str], floor: dict[Cell, int],
                      targets: dict[Cell, int], missing: dict[Cell, int],
                      off_design: dict[Cell, int],
                      trimmed: list[dict],
                      traces: dict[str, int]) -> tuple[str, dict]:
    per_scenario = Counter()
    for cell, n in floor.items():
        per_scenario[cell.scenario] += n
    target_per_scenario = Counter()
    for cell, n in targets.items():
        target_per_scenario[cell.scenario] += n
    missing_per_scenario = Counter()
    for cell, n in missing.items():
        missing_per_scenario[cell.scenario] += n

    payload = {
        "dataset": dataset,
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "behavioural_variant": BEHAVIOURAL_VARIANT,
        "scenarios": list(SPEC_V2),
        "sessions_kept": tally["kept"],
        "rows_removed": {k: v for k, v in sorted(tally.items()) if k != "kept"},
        "sessions_per_scenario": dict(sorted(per_scenario.items())),
        "design_per_scenario": dict(sorted(target_per_scenario.items())),
        "cells_kept": len(floor),
        "cells_in_design": len(targets),
        "sessions_short_of_design": sum(missing.values()),
        "short_cells": [{"scenario": c.scenario, "condition": c.condition, "plant": c.plant,
                         "missing": n} for c, n in sorted(
                            missing.items(), key=lambda kv: (kv[0].scenario, kv[0].condition))],
        "off_design_cells": [{"scenario": c.scenario, "condition": c.condition, "plant": c.plant,
                              "rows": n} for c, n in sorted(
                                 off_design.items(), key=lambda kv: kv[0].scenario)],
        "cells_trimmed": trimmed,
        **traces,
    }

    lines = [
        f"# Balance report — `{dataset}`",
        "",
        f"Generated {payload['generated_at_utc']} by `scripts/finalize_results.py`.",
        "",
        f"`results.db` here holds **{tally['kept']} sessions** over **{len(floor)} cells** of the "
        f"{len(SPEC_V2)} scenarios in `scenario-spec/new-scenario-v2.md`, behavioural arm "
        f"(`variant = {BEHAVIOURAL_VARIANT}`) only. Every cell holds the same number of sessions "
        "in every finalized dataset, so the models are compared over one sample.",
        "",
        (f"`traces/` holds {traces['trace_files_kept']} files for the "
         f"{tally['kept']} retained sessions ({traces['trace_files_removed']} traces of removed "
         f"sessions deleted"
         + (f"; {traces['sessions_without_trace']} retained sessions never wrote one"
            if traces.get("sessions_without_trace") else "")
         + ")." if traces else ""),
        "`traces/` holds exactly the retained sessions: every trace belonging to a removed "
        "session was deleted, and `trace_path` points into this folder rather than the source. "
        "`events.db` and the `*.bak-*` backups are copied whole and unfiltered \u2014 the scorer "
        "selects events by session id, so rows for removed sessions are inert, and the backups "
        "are the pre-balance record.",
        "",
        "## Rows removed from the source",
        "",
        "| reason | rows |",
        "| --- | ---: |",
    ]
    for reason, n in sorted(payload["rows_removed"].items()):
        lines.append(f"| {reason} | {n} |")
    lines += [
        "",
        "`errored` are sessions that never produced an observation (a dead backbone, an HTTP 402, "
        "a crash); `analysis.features` already refuses to score them. `surplus_above_common_floor` "
        "are usable sessions dropped only because another model has fewer in that cell — they "
        "remain in the source dataset and in this folder's backups.",
        "",
        "## Sessions per scenario (kept / design)",
        "",
        "| scenario | kept | design | short |",
        "| --- | ---: | ---: | ---: |",
    ]
    for key in SPEC_V2:
        lines.append(f"| {key} | {per_scenario[key]} | {target_per_scenario[key]} | "
                     f"{missing_per_scenario[key] or ''} |")
    lines += [
        f"| **total** | **{tally['kept']}** | **{sum(target_per_scenario.values())}** | "
        f"**{sum(missing.values()) or ''}** |",
        "",
    ]
    if missing:
        lines += [
            "## Cells still below the design target",
            "",
            f"{sum(missing.values())} sessions would bring THIS dataset to the full design. Until "
            "they are run, the cells below are trimmed to the same reduced count in every "
            "finalized dataset — the comparison stays fair, but the sample is thinner than the "
            "design asked for. `orchestrator.coverage` plans exactly these cells.",
            "",
            "| scenario | condition | plant | missing |",
            "| --- | --- | --- | ---: |",
        ]
        for entry in payload["short_cells"]:
            lines.append(f"| {entry['scenario']} | {entry['condition']} | "
                         f"{entry['plant'] or '—'} | {entry['missing']} |")
        lines.append("")
    if trimmed:
        lines += [
            "## Cells held below the design target",
            "",
            f"{len(trimmed)} of the {len(targets)} cells carry fewer than their design target, "
            "because at least one model has fewer usable sessions there. The reduced count is "
            "applied to EVERY finalized dataset, so this table is the same in all of them and "
            "describes the shared design rather than this model's sample.",
            "",
            "| scenario | condition | plant | kept | target | "
            + " | ".join(d.replace("browseruse-", "") for d in sorted(trimmed[0]["available"]))
            + " |",
            "| --- | --- | --- | ---: | ---: | " + " | ".join(
                "---:" for _ in trimmed[0]["available"]) + " |",
        ]
        for entry in trimmed:
            available = " | ".join(str(entry["available"][d]) for d in sorted(entry["available"]))
            lines.append(f"| {entry['scenario']} | {entry['condition']} | "
                         f"{entry['plant'] or '—'} | {entry['kept']} | {entry['target']} | "
                         f"{available} |")
        lines.append("")
    if off_design:
        lines += [
            "## Cells in the source that the current design does not contain",
            "",
            "Rows from a condition or plant route the scenario file no longer declares. They are "
            "removed rather than pooled, and listed here so the removal is on the record.",
            "",
            "| scenario | condition | plant | rows |",
            "| --- | --- | --- | ---: |",
        ]
        for entry in payload["off_design_cells"]:
            lines.append(f"| {entry['scenario']} | {entry['condition']} | "
                         f"{entry['plant'] or '—'} | {entry['rows']} |")
        lines.append("")
    return "\n".join(lines), payload


def finalize(datasets: tuple[str, ...], reps: int, cold_reps: int, dense_cold_reps: int,
             force: bool, plan_only: bool) -> list[dict]:
    """Balance every named dataset and write `results/final-<dataset>/`.

    Returns:
        One balance payload per dataset, in the order given.

    Raises:
        FileNotFoundError: If a dataset has no results.db.
    """
    sources = {}
    for dataset in datasets:
        db_path = RESULTS_ROOT / dataset / "results.db"
        if not db_path.exists():
            raise FileNotFoundError(f"no results.db for dataset {dataset!r} at {db_path}")
        sources[dataset] = db_path

    targets = design_targets(SPEC_V2, reps, cold_reps, dense_cold_reps)
    have = {dataset: usable_sessions(path) for dataset, path in sources.items()}
    floor = common_floor(targets, have)
    trimmed = trimmed_cells(targets, floor, have)

    reports = []
    for dataset in datasets:
        cells = have[dataset]
        keep = {session_id
                for cell, n in floor.items()
                for session_id in cells.get(cell, ())[:n]}
        off_design = {cell: len(ids) for cell, ids in cells.items()
                      if cell.scenario in SPEC_V2 and cell not in targets}
        destination = RESULTS_ROOT / f"{FINAL_PREFIX}{dataset}"

        if plan_only:
            tally = Counter({"kept": len(keep)})
            connection = sqlite3.connect(f"file:{sources[dataset]}?mode=ro", uri=True)
            try:
                for session_id, scenario, error, variant in connection.execute(
                        "SELECT session_id, scenario, error, variant FROM sessions"):
                    if session_id in keep:
                        continue
                    if error is not None:
                        tally[DROP_ERRORED] += 1
                    elif variant != BEHAVIOURAL_VARIANT:
                        tally[DROP_OTHER_ARM] += 1
                    elif (scenario or "authstate_v1") not in SPEC_V2:
                        tally[DROP_OFF_SPEC] += 1
                    else:
                        tally[DROP_SURPLUS] += 1
            finally:
                connection.close()
            traces = {}
        else:
            copy_dataset(RESULTS_ROOT / dataset, destination, force)
            tally = filter_database(destination / "results.db", keep)
            traces = prune_traces(destination, keep)
            _stale_banner(destination)

        text, payload = _balance_document(
            dataset, tally, floor, targets, shortfall(targets, cells), off_design, trimmed,
            traces)
        payload["source_dataset"] = dataset
        payload["finalized_dataset"] = destination.name
        if not plan_only:
            (destination / "BALANCE.md").write_text(text + "\n")
            (destination / "balance.json").write_text(json.dumps(payload, indent=2) + "\n")
        reports.append(payload)
    return reports


def _export(dataset: str) -> bool:
    """Regenerate results.json for a finalized dataset, in a subprocess.

    `orchestrator.config` binds the dataset at import, so the export cannot run in this process
    without rebinding the namespace the rest of this script already read.
    """
    completed = subprocess.run(
        [sys.executable, "-m", "analysis.export_report", "--dataset", dataset],
        cwd=REPO_ROOT, check=False)
    return completed.returncode == 0


def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--datasets", nargs="*", default=list(DEFAULT_DATASETS),
                        help="source datasets under results/ to balance against each other")
    parser.add_argument("--reps", type=int, default=3, help="design target per loaded cell")
    parser.add_argument("--cold-reps", type=int, default=10)
    parser.add_argument("--dense-cold-reps", type=int, default=20)
    parser.add_argument("--force", action="store_true",
                        help="rebuild results/final-<dataset>/ if it already exists")
    parser.add_argument("--plan", action="store_true",
                        help="report what would be kept and dropped; write nothing")
    parser.add_argument("--no-export", action="store_true",
                        help="skip regenerating results.json for the finalized datasets")
    args = parser.parse_args()

    reports = finalize(tuple(args.datasets), args.reps, args.cold_reps, args.dense_cold_reps,
                       args.force, args.plan)

    width = max(len(r["source_dataset"]) for r in reports)
    print(f"{'dataset':{width}s} {'kept':>6s} {'errored':>8s} {'other arm':>10s} "
          f"{'off-spec':>9s} {'surplus':>8s} {'short':>6s}")
    for report in reports:
        removed = report["rows_removed"]
        print(f"{report['source_dataset']:{width}s} {report['sessions_kept']:6d} "
              f"{removed.get(DROP_ERRORED, 0):8d} {removed.get(DROP_OTHER_ARM, 0):10d} "
              f"{removed.get(DROP_OFF_SPEC, 0):9d} {removed.get(DROP_SURPLUS, 0):8d} "
              f"{report['sessions_short_of_design']:6d}")
    kept = {r["sessions_kept"] for r in reports}
    print(f"\ncells kept: {reports[0]['cells_kept']} of {reports[0]['cells_in_design']} in the "
          f"design; sessions per model: {kept.pop() if len(kept) == 1 else sorted(kept)}")
    if len(kept) > 0:
        print("WARNING: the finalized datasets do NOT hold the same number of sessions",
              file=sys.stderr)
        return 1
    if args.plan:
        print("\n--plan: nothing written")
        return 0
    if args.no_export:
        return 0
    print()
    failed = [r["finalized_dataset"] for r in reports if not _export(r["finalized_dataset"])]
    if failed:
        print(f"WARNING: results.json export failed for {', '.join(failed)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
