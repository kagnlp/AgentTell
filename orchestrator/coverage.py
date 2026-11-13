"""How many sessions each matrix cell still needs, so a stopped run resumes at the cell."""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from harness.scenarios import Scenario

NO_PLANT = ""
COLD = "none"


@dataclass(frozen=True, slots=True)
class Batch:
    """One `run_matrix` invocation: this scenario, these conditions x these plants, `reps` each."""
    scenario: str
    conditions: tuple[str, ...]
    plants: tuple[str, ...]
    reps: int

    @property
    def sessions(self) -> int:
        return len(self.conditions) * max(len(self.plants), 1) * self.reps


def recorded(db_path: str | Path, scenario: str, variant: str = "A") -> dict[tuple[str, str], int]:
    """Usable sessions per (condition, plant) for one scenario and arm."""
    try:
        connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return {}
    try:
        rows = connection.execute(
            "SELECT condition, COALESCE(plant, ''), COUNT(*) FROM sessions "
            "WHERE scenario = ? AND variant = ? AND error IS NULL "
            "GROUP BY condition, COALESCE(plant, '')",
            (scenario, variant),
        ).fetchall()
    except sqlite3.Error:
        return {}
    finally:
        connection.close()
    return {(condition, plant): count for condition, plant, count in rows}


def wanted_cells(sc: Scenario, reps: int, cold_reps: int,
                 tasks: int | None = None) -> dict[tuple[str, str], int]:
    """Target session count for every (condition, plant) cell the design asks for."""
    plant_ids = list(sc.plant_ids)[:tasks]
    targets = sc.label_ids

    wanted: dict[tuple[str, str], int] = {
        (condition, plant): reps for condition in targets for plant in plant_ids}

    if sc.surfaces:
        wanted.update({(COLD, plant): cold_reps for plant in plant_ids})
    else:
        wanted[(COLD, NO_PLANT)] = cold_reps
    return wanted


def plan(sc: Scenario, reps: int, cold_reps: int,
         have: dict[tuple[str, str], int] | None = None,
         tasks: int | None = None) -> list[Batch]:
    """The batches still needed to bring every cell of `sc` up to its target."""
    have = have or {}
    wanted = wanted_cells(sc, reps, cold_reps, tasks)

    grouped: dict[tuple[str, int], list[str]] = defaultdict(list)
    for (condition, plant), target in wanted.items():
        missing = target - have.get((condition, plant), 0)
        if missing > 0:
            grouped[(condition, missing)].append(plant)

    def _order(item: tuple[tuple[str, int], list[str]]) -> tuple[int, str, int]:
        (condition, missing), _ = item
        return (0 if condition == COLD else 1, condition, missing)

    batches = []
    for (condition, missing), plants in sorted(grouped.items(), key=_order):
        real_plants = tuple(sorted(p for p in plants if p))
        batches.append(Batch(sc.key, (condition,), real_plants, missing))
    return batches


DENSE_COLD = frozenset({"sensitive_access", "tier_gated", "privilege_role", "account_age",
                        "affiliation", "credit_standing", "stigma_platform",
                        "confidential_role"})


def _main() -> int:
    """`python -m orchestrator.coverage` — emit the batches still outstanding, as TSV."""
    import argparse

    from harness.scenarios import SCENARIOS
    from orchestrator.config import RESULTS_DB

    parser = argparse.ArgumentParser(description="Outstanding matrix cells.")
    parser.add_argument("--reps", type=int, default=3)
    parser.add_argument("--cold-reps", type=int, default=10)
    parser.add_argument("--dense-cold-reps", type=int, default=20)
    parser.add_argument("--tasks", type=int, default=None,
                        help="use only the first N tasks (plant routes) of each scenario")
    parser.add_argument("--scenarios", nargs="*", default=None)
    parser.add_argument("--total", action="store_true",
                        help="print only the outstanding session count")
    parser.add_argument("--scenario-reps", nargs="*", default=[], metavar="KEY=LOADED[:COLD]",
                        help="override the target counts for one scenario, e.g. "
                             "checkout=1:3. For a scenario the backbone REFUSES, the "
                             "full allocation buys nothing but repeated refusals — but the "
                             "scenario is still run, because a refusal is a result and dropping "
                             "it would be a silent exclusion.")
    args = parser.parse_args()

    overrides: dict[str, tuple[int, int | None]] = {}
    for item in args.scenario_reps:
        key, _, spec = item.partition("=")
        if key not in SCENARIOS or not spec:
            raise SystemExit(f"--scenario-reps: unknown or malformed entry {item!r}")
        loaded, _, cold = spec.partition(":")
        overrides[key] = (int(loaded), int(cold) if cold else None)

    keys = args.scenarios or list(SCENARIOS)
    outstanding, lines = 0, []
    for key in keys:
        sc = SCENARIOS.get(key)
        if sc is None:
            continue
        cold = args.dense_cold_reps if key in DENSE_COLD else args.cold_reps
        reps = args.reps
        if key in overrides:
            reps, cold_override = overrides[key]
            if cold_override is not None:
                cold = cold_override
        have = recorded(RESULTS_DB, key)
        for batch in plan(sc, reps, cold, have, args.tasks):
            outstanding += batch.sessions
            lines.append("\t".join([
                batch.scenario, ",".join(batch.conditions),
                ",".join(batch.plants) if batch.plants else "-", str(batch.reps)]))
    if args.total:
        print(outstanding)
    else:
        print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
