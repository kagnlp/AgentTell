"""Experiment runner: iterates the matrix (agent × LLM × condition × repetitions), drives the agent
through each session plan, and records results.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path

from agents.browseruse_runner import BrowserUseRunner, guard_level
from attacker.db import connect as events_connect
from harness.scenarios import get_scenario
from harness.session import new_session_id, trace_basename
from harness.state_planting import build_plan
from orchestrator import results_store, spend
from orchestrator.config import (DATASET, EVENT_LOG_DB, LLM_REGISTRY, MATRIX, RESULTS_DB,
                                 TRACES_DIR)


RUNNERS = {"browseruse": BrowserUseRunner}


def _is_exhaustion(error: str) -> bool:
    """Whether a session error means the provider has stopped serving this key entirely."""
    if "402" in error:
        return True
    return "403" in error and "limit exceeded" in error.lower()


def _reached_probe(session_id: str) -> bool:
    """Whether the attacker origin ever rendered the probe for this session."""
    with events_connect(EVENT_LOG_DB) as conn:
        return conn.execute(
            "SELECT 1 FROM events WHERE session_id = ? AND event_type = 'pageview' LIMIT 1",
            (session_id,)).fetchone() is not None


def run_cell(agent: str, llm: str, condition: str, rep: int, variant: str,
             scenario: str, headless: bool, plant: str = "", log=print) -> str | None:
    session_id = new_session_id()
    plan = build_plan(session_id, condition, variant, scenario, plant_id=plant)
    runner = RUNNERS[agent](headless=headless)
    secret_label = get_scenario(scenario).label_for_condition(condition)
    sc = get_scenario(scenario)
    keep_plant = bool(sc.surfaces) or secret_label != "none"
    plant = plan.meta.get("plant", "") if keep_plant else ""

    seed = MATRIX.base_seed + rep
    log(f"  -> {session_id} ({condition}/{plant}/{variant})")
    spend_before = spend.key_spend(llm)
    trace = runner.run_session(plan, llm_key=llm, seed=seed)
    cost_usd = spend.spent_between(spend_before, spend.key_spend(llm))

    if trace.error is None and not _reached_probe(session_id):
        trace.error = "probe never rendered: the agent did not reach it"

    trace_path = str(Path(TRACES_DIR) / trace_basename(session_id, scenario, condition, variant,
                                                       plant))
    meta = dict(plan.meta or {})
    meta.setdefault("backbone_model",
                    LLM_REGISTRY[llm].model if llm in LLM_REGISTRY else llm)
    meta.setdefault("guard", guard_level())
    if cost_usd is not None:
        meta.setdefault("cost_usd", round(cost_usd, 6))
    results_store.record(
        session_id=session_id, agent=agent, llm=llm, condition=condition, rep=rep,
        variant=variant, scenario=scenario, plant=plant, secret_label=secret_label,
        duration_s=trace.duration_s, final_result=trace.final_result,
        error=trace.error, trace_path=trace_path if Path(trace_path).exists() else None,
        meta=meta,
    )
    if trace.error:
        log(f"     ! error: {trace.error}")
    return trace.error


def _iter_cells(agents, llms, variants, conditions, sc, reps, plants):
    """Every (agent, llm, variant, condition, plant, rep) cell that will actually run."""
    per_surface = bool(getattr(sc, "surfaces", None))
    for agent in agents:
        for llm in llms:
            for variant in variants:
                for condition in conditions:
                    label = sc.label_for_condition(condition)
                    cell_plants = plants if per_surface or label != "none" else plants[:1]
                    for plant in cell_plants:
                        for rep in range(reps):
                            yield agent, llm, variant, condition, plant, rep


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agents", nargs="*", default=MATRIX.agents)
    ap.add_argument("--llms", nargs="*", default=None,
                    help="default: all LLMs with a key present in .env")
    ap.add_argument("--scenario", default="sso_wall",
                    help="a scenario key from benchmark/scenarios/index.yaml")
    ap.add_argument("--conditions", nargs="*", default=None,
                    help="default: the scenario's own condition set")
    ap.add_argument("--reps", type=int, default=MATRIX.repetitions)
    ap.add_argument("--variants", nargs="*", default=None,
                    help="variants to sweep (default A)")
    ap.add_argument("--plants", nargs="*", default=None,
                    help="which of the scenario's plant routes to sweep (default: all of "
                         "them). Each plants the SAME secret by a different route, so "
                         "comparing them separates 'this model does not leak' from 'this "
                         "plant did not put the value in context'.")
    ap.add_argument("--headful", action="store_true", help="show the browser")
    ap.add_argument("--tqdm", action="store_true",
                    help="show a tqdm progress bar (N/total, rate, ETA) across all cells; "
                         "without it, behaviour is unchanged (one printed line per cell)")
    ap.add_argument("--quiet", action="store_true",
                    help="silence browser-use's per-step agent logs (Step/Eval/Memory/action "
                         "chatter); pair with --tqdm for a clean progress bar")
    ap.add_argument("--smoke", action="store_true",
                    help="one secret-present session with the first available LLM")
    args = ap.parse_args()

    if args.quiet:
        os.environ["BROWSER_USE_LOGGING_LEVEL"] = "result"
        os.environ["SCT_QUIET_AGENT"] = "1"

    results_store.init_db()
    print(f"[dataset: {DATASET or '(default flat results/)'}  ->  writing {RESULTS_DB}]")
    llms = args.llms or MATRIX.available_llms()
    if not llms:
        raise SystemExit("No LLMs available — set a provider key in .env (see .env.example).")

    sc = get_scenario(args.scenario)
    conditions = args.conditions or list(sc.conditions)
    variants = args.variants or ["A"]
    plants = args.plants or list(sc.plant_ids)
    unknown = [p for p in plants if p not in sc.plant_ids]
    if unknown:
        raise SystemExit(f"{args.scenario}: unknown plant(s) {unknown}. "
                         f"Known: {sc.plant_ids}")

    positive = next((c for c in conditions if sc.label_for_condition(c) != "none"),
                    conditions[0])

    if args.smoke:
        run_cell(args.agents[0], llms[0], positive, 0, variants[0], args.scenario,
                 headless=not args.headful, plant=plants[0])
        return

    cells = list(_iter_cells(args.agents, llms, variants, conditions, sc, args.reps, plants))

    bar = None
    if args.tqdm:
        try:
            from tqdm import tqdm
            bar = tqdm(total=len(cells), unit="run", dynamic_ncols=True, desc=args.scenario)
        except ImportError:
            print("[warn] --tqdm requested but tqdm is not installed "
                  "(`uv pip install tqdm`); continuing without a progress bar.")
    log = bar.write if bar is not None else print

    run_spend_before = spend.key_spend(llms[0])

    aborted = False
    for agent, llm, variant, condition, plant, rep in cells:
        log(f"[{agent} | {llm} | {args.scenario} | {condition} | {plant} | {variant} "
            f"| rep {rep}]")
        if bar is not None:
            bar.set_postfix_str(f"{condition}/{plant}/{variant} r{rep}")
        error = run_cell(agent, llm, condition, rep, variant, args.scenario,
                         headless=not args.headful, plant=plant, log=log)
        if bar is not None:
            bar.update(1)
        if error and _is_exhaustion(error):
            log(f"[abort] provider refused to serve — {error[:120]}. Stopping this run.")
            aborted = True
            break
    if bar is not None:
        bar.close()

    run_cost = spend.spent_between(run_spend_before, spend.key_spend(llms[0]))
    if run_cost is None:
        print(f"[cost] {len(cells)} sessions — spend unavailable for {llms[0]!r} "
              f"(no spend endpoint, key unset, or the request failed)")
    else:
        print(f"[cost] {len(cells)} sessions = ${run_cost:.4f} "
              f"(${run_cost / max(len(cells), 1):.4f}/session)")
    if aborted:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
