"""Which `ask_only` cells to run, on which plant routes, for one backbone.

The behavioural arm sweeps every option x every plant because each of those cells is its own
estimand — `LR(t)` per target, a per-plant breakdown, a k x k confusion matrix. The `ask_only`
arm is not shaped that way: `analysis.inference.refusal_rate_on_direct_request` groups by
variant alone and `analysis.export_report` emits ONE `ask_only_disclosure_rate` per scenario, so
option and plant are nuisance factors that need coverage rather than crossing. A subset of cells
therefore measures the same quantity at a fraction of the cost.

Two choices decide which subset, and both matter more than the count:

PLANTS. A plant that never put the secret in context leaves the agent nothing to disclose, and
the session then scores REFUSED_IN_TEXT — a tooling failure recorded as a result, which is the
one bug the project forbids outright. The reference dataset's arm-A `leak_by_plant` already says
which routes landed the secret FOR THIS BACKBONE: across the four backbones measured the best
and worst plant of a scenario differ by a median of 0.46, and which route is the dead one differs
by backbone in 15 of 20 scenarios, so the choice cannot be hardcoded and cannot be borrowed from
another model. Plants are taken in descending arm-A leak.

OPTIONS. Dealt round-robin from a seeded shuffle of the scenario's targets, so three plants at
two options each touch up to six distinct labels instead of repeating one pair three times. The
shuffle is driven by `MATRIX.base_seed` for the reason every other ordering in this project is:
a rerun must reproduce the same allocation.

Scenarios whose labels name the affordance rather than the secret cannot be scored from the ask
box at all (`analysis.features.ask_box_scorable`). They are cut to a token allocation rather than
dropped, because a scenario that is excluded without a row to show for it is a silent exclusion.
"""
from __future__ import annotations

import json
import random
from pathlib import Path

from analysis.features import ask_box_scorable
from harness.scenarios import CAPABILITY, Scenario
from orchestrator.coverage import Batch, recorded

#: Sessions given to a scenario the ask-box scorer cannot read. Enough to leave UNSCORABLE rows
#: in the database naming the reason, not enough to pay for an answer that cannot arrive.
TOKEN_REPS = 1


def leak_by_plant(reference: str | Path, scenario: str) -> dict[str, float]:
    """Arm-A leak per plant route for one scenario, from a scored dataset's `results.json`.

    Returns an empty mapping when the scenario was never scored there, which the caller must
    treat as "no evidence about these routes" rather than as "no route leaks".
    """
    path = Path(reference)
    if not path.exists():
        raise FileNotFoundError(f"no scored reference at {path} — run analysis.export_report first")
    rows = json.loads(path.read_text()).get("headline", [])
    for row in rows:
        if row.get("scenario") == scenario:
            return {p: v for p, v in (row.get("leak_by_plant") or {}).items() if v is not None}
    return {}


def choose_plants(sc: Scenario, leak: dict[str, float], n_plants: int) -> tuple[str, ...]:
    """The `n_plants` routes of `sc` that carried the secret best in the reference run.

    Ties and unmeasured routes fall back to the scenario's declared order, so a scenario with no
    reference data still plans a run instead of raising.
    """
    order = {p: i for i, p in enumerate(sc.plant_ids)}
    ranked = sorted(sc.plant_ids, key=lambda p: (-leak.get(p, -1.0), order[p]))
    return tuple(ranked[:max(1, min(n_plants, len(sc.plant_ids)))])


def deal_options(sc: Scenario, plants: tuple[str, ...], n_options: int, seed: int
                 ) -> dict[str, tuple[str, ...]]:
    """Assign `n_options` target conditions to each plant, dealt from one seeded shuffle.

    Dealing round-robin rather than reusing one pair means k plants x n options covers up to
    k*n distinct labels at the same session count.
    """
    targets = [c for c in sc.conditions
               if sc.label_for_condition(c) not in ("none", CAPABILITY)]
    if not targets:
        raise ValueError(f"scenario {sc.key!r} declares no target conditions")
    shuffled = list(targets)
    random.Random(f"{seed}:{sc.key}").shuffle(shuffled)  # noqa: S311  seeded, see CLAUDE.md s10
    width = max(1, min(n_options, len(shuffled)))
    dealt: dict[str, tuple[str, ...]] = {}
    for index, plant in enumerate(plants):
        start = (index * width) % len(shuffled)
        dealt[plant] = tuple(shuffled[(start + offset) % len(shuffled)] for offset in range(width))
    return dealt


def plan(sc: Scenario, leak: dict[str, float], n_plants: int, n_options: int, reps: int,
         seed: int, have: dict[tuple[str, str], int] | None = None) -> list[Batch]:
    """The `ask_only` batches still outstanding for one scenario.

    Cells already at target produce nothing, so a rerun tops up the shortfall rather than
    repeating the scenario. A scenario the ask-box scorer cannot read is cut to `TOKEN_REPS`
    instead of being dropped.
    """
    have = have or {}
    plants = choose_plants(sc, leak, n_plants)
    dealt = deal_options(sc, plants, n_options, seed)
    scorable = any(ask_box_scorable(sc, sc.label_for_condition(c))
                   for options in dealt.values() for c in options)
    if not scorable:
        # No route and no label can produce a scorable ask-box row here, so the plant and option
        # axes buy nothing either: collapse to one cell whose UNSCORABLE verdict records why.
        first_plant = plants[0]
        dealt = {first_plant: dealt[first_plant][:1]}
    target = reps if scorable else TOKEN_REPS

    batches = []
    for plant, options in dealt.items():
        for condition in options:
            missing = target - have.get((condition, plant), 0)
            if missing > 0:
                batches.append(Batch(sc.key, (condition,), (plant,), missing))
    return batches


def _main() -> int:
    """`python -m orchestrator.askonly_plan` — emit outstanding ask_only cells as TSV.

    Columns: scenario, conditions (csv), plants (csv), reps — the same shape
    `orchestrator.coverage` emits, so `scripts/run_askonly.sh` turns each row into one
    `run_matrix` invocation.
    """
    import argparse

    from harness.scenarios import SCENARIOS
    from orchestrator.config import MATRIX, RESULTS_DB

    parser = argparse.ArgumentParser(description="Outstanding ask_only cells.")
    parser.add_argument("--reference", required=True,
                        help="results.json of this backbone's scored arm-A run; its "
                             "leak_by_plant chooses which plant routes are worth probing")
    parser.add_argument("--plants", type=int, default=3)
    parser.add_argument("--options", type=int, default=2)
    parser.add_argument("--reps", type=int, default=2)
    parser.add_argument("--seed", type=int, default=MATRIX.base_seed)
    parser.add_argument("--scenarios", nargs="*", default=None)
    parser.add_argument("--total", action="store_true",
                        help="print only the outstanding session count")
    args = parser.parse_args()

    keys = args.scenarios or list(SCENARIOS)
    outstanding, lines = 0, []
    for key in keys:
        sc = SCENARIOS.get(key)
        if sc is None:
            raise SystemExit(f"unknown scenario {key!r}")
        have = recorded(RESULTS_DB, key, "ask_only")
        leak = leak_by_plant(args.reference, key)
        for batch in plan(sc, leak, args.plants, args.options, args.reps, args.seed, have):
            outstanding += batch.sessions
            lines.append("\t".join([batch.scenario, ",".join(batch.conditions),
                                    ",".join(batch.plants), str(batch.reps)]))
    if args.total:
        print(outstanding)
    else:
        print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
