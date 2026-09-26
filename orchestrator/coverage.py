"""How many sessions each matrix cell still needs, so a stopped run resumes at the cell.

A sweep of ~1,700 sessions runs for two days, and it WILL be interrupted. Resuming per scenario
re-runs a whole 100-session scenario to recover the last few, which at 95 s a session is hours of
wasted backbone. This module computes the shortfall per (condition, plant) cell instead, and
groups what is left into the fewest `run_matrix` invocations that cover it.

Only sessions that actually RAN count. An errored row — a backbone that never responded, a
credit exhaustion, a crash — is not an observation and is re-run rather than accepted, which is
the same rule `analysis.features` applies when scoring. Counting them would let a half-failed
sweep look complete and quietly report a thinner sample than the design asked for.

The arm is part of a cell's identity: this counts one variant (default "A", the behavioural arm),
so topping up the behavioural sweep never mistakes an `ask_only` session for one of its own.
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from harness.scenarios import CAPABILITY, Scenario

#: A cold row records no plant, so its cell key uses this in the plant position. The exception is
#: a scenario whose probe copy varies by plant (vendor_session's surface factor), where cold IS
#: per plant and the real plant id is recorded — see orchestrator/run_matrix.run_cell.
NO_PLANT = ""


@dataclass(frozen=True, slots=True)
class Batch:
    """One `run_matrix` invocation: this scenario, these conditions x these plants, `reps` each.

    `plants` is empty when the arm plants nothing and the plant axis should be left to
    run_matrix's own collapsing (the cold and capability arms of a non-surfaces scenario).
    """
    scenario: str
    conditions: tuple[str, ...]
    plants: tuple[str, ...]
    reps: int

    @property
    def sessions(self) -> int:
        return len(self.conditions) * max(len(self.plants), 1) * self.reps


def recorded(db_path: str | Path, scenario: str, variant: str = "A") -> dict[tuple[str, str], int]:
    """Usable sessions per (condition, plant) for one scenario and arm.

    Missing database or table is a normal state on a first run and yields an empty mapping —
    the caller then plans the whole sweep, which is correct.
    """
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
                 cap_reps: int) -> dict[tuple[str, str], int]:
    """Target session count for every (condition, plant) cell the design asks for.

    This IS the design: the sweep plans against it and `scripts/finalize_results.py` trims to it,
    so a change here moves both together rather than letting the two drift into reporting
    different denominators.

    Args:
        sc: The scenario to size.
        reps: Target sessions per (loaded target, plant) cell.
        cold_reps: Target cold sessions (per surface, on a scenario with surfaces).
        cap_reps: Target capability sessions; only the correct-the-default rig has this arm.
    """
    targets = [c for c in sc.conditions
               if sc.label_for_condition(c) not in ("none", CAPABILITY)]
    cold_condition = "none" if "none" in sc.conditions else "not_X"

    # Loaded cells are fully crossed: every target on every plant route.
    wanted: dict[tuple[str, str], int] = {
        (condition, plant): reps for condition in targets for plant in sc.plant_ids}

    # Cold carries a plant only where the probe copy varies by plant, because there each surface
    # needs its own baseline; elsewhere one cold pool serves every route.
    if sc.surfaces:
        wanted.update({(cold_condition, plant): cold_reps for plant in sc.plant_ids})
    else:
        wanted[(cold_condition, NO_PLANT)] = cold_reps

    if sc.rig == "correct" and cap_reps:
        wanted[(CAPABILITY, NO_PLANT)] = cap_reps
    return wanted


def plan(sc: Scenario, reps: int, cold_reps: int, cap_reps: int,
         have: dict[tuple[str, str], int] | None = None) -> list[Batch]:
    """The batches still needed to bring every cell of `sc` up to its target.

    Cells already at or over target produce nothing, so a completed scenario plans to an empty
    list and a scenario missing four sessions plans four sessions — not a hundred.

    Args:
        sc: The scenario to plan.
        reps: Target sessions per (loaded target, plant) cell.
        cold_reps: Target cold sessions (per surface, on a scenario with surfaces).
        cap_reps: Target capability sessions; only the correct-the-default rig has this arm.
        have: Output of `recorded`. Empty or omitted plans the full sweep.
    """
    have = have or {}
    wanted = wanted_cells(sc, reps, cold_reps, cap_reps)
    cold_condition = "none" if "none" in sc.conditions else "not_X"

    # Group the shortfall so cells needing the same number of sessions share one invocation.
    # Without this, resuming a 100-session scenario would issue 100 subprocesses.
    grouped: dict[tuple[str, int], list[str]] = defaultdict(list)
    for (condition, plant), target in wanted.items():
        missing = target - have.get((condition, plant), 0)
        if missing > 0:
            grouped[(condition, missing)].append(plant)

    # Cold first within a scenario, then capability, then the loaded targets. Plain alphabetical
    # ordering put cold LAST on the scenarios whose condition ids sort before it (account_age,
    # balance_threshold), which defeats the cold_prior gate: the gate exists to decide whether the
    # loaded cells are worth running at all, so it has to be answerable before they run.
    def _order(item: tuple[tuple[str, int], list[str]]) -> tuple[int, str, int]:
        (condition, missing), _ = item
        rank = 0 if condition == cold_condition else (1 if condition == CAPABILITY else 2)
        return (rank, condition, missing)

    batches = []
    for (condition, missing), plants in sorted(grouped.items(), key=_order):
        real_plants = tuple(sorted(p for p in plants if p))
        batches.append(Batch(sc.key, (condition,), real_plants, missing))
    return batches


#: The scenarios whose cold arm is a genuine PRIOR over the options rather than a
#: near-deterministic fallback (sensitive_access), or whose options are ordinal so a prior toward
#: the commonest value is plausible (the rest). All are additionally gated on
#: scripts/cold_prior.py passing before their loaded cells are worth running.
#:
#: The four rebuilt/new spec-v2 scenarios are here because each one's file states the requirement
#: directly: affiliation and stigma_platform because a model prior over organisation TYPES and
#: over stigma CATEGORIES is far more plausible than one over the invented brand names they
#: replaced; credit_standing because its options are ordinal and its two distress targets are
#: expected to carry a NEGATIVE cold prior; confidential_role because its decoys are
#: template-identical, so a flat cold distribution across positions is the precondition for
#: reading any loaded cell at all.
DENSE_COLD = frozenset({"sensitive_access", "tier_gated", "privilege_role", "account_age",
                        "balance_threshold", "affiliation", "credit_standing",
                        "stigma_platform", "confidential_role"})


def _main() -> int:
    """`python -m orchestrator.coverage` — emit the batches still outstanding, as TSV.

    Columns: scenario, conditions (csv), plants (csv, "-" for none), reps. Consumed by
    scripts/run_behavioural.sh, which turns each row into one run_matrix invocation.
    """
    import argparse

    from harness.scenarios import SCENARIOS
    from orchestrator.config import RESULTS_DB

    parser = argparse.ArgumentParser(description="Outstanding matrix cells.")
    parser.add_argument("--reps", type=int, default=3)
    parser.add_argument("--cold-reps", type=int, default=10)
    parser.add_argument("--dense-cold-reps", type=int, default=20)
    parser.add_argument("--variant", default="A")
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
        cap = args.cold_reps if sc.rig == "correct" else 0
        reps = args.reps
        if key in overrides:
            reps, cold_override = overrides[key]
            if cold_override is not None:
                cold = cold_override
        have = recorded(RESULTS_DB, key, args.variant)
        for batch in plan(sc, reps, cold, cap, have):
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
