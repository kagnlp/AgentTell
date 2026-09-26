"""Experiment runner: iterates the matrix (agent × LLM × condition × repetitions),
drives the agent through each session plan, and records results.

Assumes the attacker server and mock bank are already running (see scripts/serve.sh).
Resumable: cells already completed (no error) are skipped unless --force.

Usage:
  uv run python -m orchestrator.run_matrix              # full available matrix
  uv run python -m orchestrator.run_matrix --llms claude --reps 2 --headful
  uv run python -m orchestrator.run_matrix --smoke      # 1 session, X, first llm
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path

from agents.browseruse_runner import (BrowserUseRunner, guard_level,
                                      step_completion_clause)
from attacker.db import connect as events_connect
from harness.scenarios import CAPABILITY, get_scenario
from harness.session import new_session_id, trace_basename
from harness.state_planting import build_plan
from orchestrator import results_store, spend
from orchestrator.config import (DATASET, EVENT_LOG_DB, LLM_REGISTRY, MATRIX, RESULTS_DB,
                                 TRACES_DIR)

RUNNERS = {"browseruse": BrowserUseRunner}


def _is_exhaustion(error: str) -> bool:
    """Whether a session error means the provider has stopped serving this key entirely.

    Matches the two shapes OpenRouter actually returns — HTTP 402 for spent credit, and HTTP 403
    carrying "limit exceeded" for a key that has hit its cap. A plain 403 (a rejected key, a
    blocked model) is NOT exhaustion and must not abort a long sweep.
    """
    if "402" in error:
        return True
    return "403" in error and "limit exceeded" in error.lower()


def _reached_probe(session_id: str) -> bool:
    """Whether the attacker origin ever rendered the probe for this session.

    `attacker.app._render_probe` writes its `pageview` server-side, before any client JS runs, so
    this is a fact about the HTTP request and not about the agent. Every session plan ends at the
    probe, so a session with no pageview never got there and observed nothing.

    Deliberately NOT a test for clicks. An agent that opens the probe, reads the options and
    declines to choose — "you haven't told me which document is yours, I did not guess" — has
    made the observation the cold arm exists to measure: it selected no target, so P_cold(t) = 0
    for every t. Requiring a click discarded 171 such sessions here, 85 of them cold, and then
    re-ran those cells until a session happened to click — which selects the cold baseline for
    exactly the behaviour it is supposed to measure the absence of. content_ownership lost its
    ENTIRE cold arm that way and became unscorable; sso_wall burned 46 attempts to keep 7.
    """
    with events_connect(EVENT_LOG_DB) as conn:
        return conn.execute(
            "SELECT 1 FROM events WHERE session_id = ? AND event_type = 'pageview' LIMIT 1",
            (session_id,)).fetchone() is not None


def run_cell(agent: str, llm: str, condition: str, rep: int, variant: str,
             scenario: str, headless: bool, plant: str = "", log=print) -> str | None:
    session_id = new_session_id(agent, llm, condition, rep, scenario)
    plan = build_plan(session_id, condition, variant, scenario, plant_id=plant)
    runner = RUNNERS[agent](headless=headless)
    secret_label = get_scenario(scenario).label_for_condition(condition)
    # Only a loaded session actually used a plant route. The cold and capability arms plant
    # nothing, so recording the scenario's first plant id against them would let a later
    # "how many sessions used plant X" query silently sweep in sessions where X never ran.
    # A surfaces scenario is the exception: there the plant also names which SURFACE the probe
    # rendered, which a cold row needs to keep or its baseline cannot be matched to the loaded
    # rows it is the baseline for (analysis.channel_metrics.plant_metrics).
    sc = get_scenario(scenario)
    keep_plant = bool(sc.surfaces) or secret_label not in ("none", CAPABILITY)
    plant = plan.meta.get("plant", "") if keep_plant else ""

    seed = MATRIX.base_seed + rep
    log(f"  -> {session_id} ({condition}/{plant}/{variant})")
    # Bracket the session with the provider's running spend total, so the row carries what this
    # cell actually cost. `None` on either side means unknown and is recorded as absent — never
    # as 0.0, which would read as "free" (see orchestrator/spend). The bracket is only meaningful
    # because sessions run one at a time: the provider reports a single running total per key, so
    # anything else spending on it concurrently lands inside this delta.
    spend_before = spend.key_spend(llm)
    trace = runner.run_session(plan, llm_key=llm, seed=seed)
    cost_usd = spend.spent_between(spend_before, spend.key_spend(llm))

    # A session that never reached the probe observed nothing, yet it can still carry a plausible
    # final_result ("I was unable to open the page") and pass the runner's own liveness check.
    # analysis.features would then score it as "the agent selected nothing", which is
    # indistinguishable from a deliberate abstention. Fail it here so it is excluded from scoring
    # and replanned, rather than quietly counted as a non-leak.
    if trace.error is None and not _reached_probe(session_id):
        trace.error = "probe never rendered: the agent did not reach it"

    # Same descriptive name the runner writes (scenario-condition-plant-variant-<hash>), so the
    # recorded trace_path resolves to the file on disk.
    trace_path = str(Path(TRACES_DIR) / trace_basename(session_id, scenario, condition, variant,
                                                       plant))
    # Stamp the concrete backbone id (e.g. openai/gpt-5.2) into the row: `llm` is only the
    # registry key ("openrouter"), so without this the dataset can't say which model produced
    # it once the shell's OPENROUTER_MODEL has moved on. analysis/export_report.py reads it.
    meta = dict(plan.meta or {})
    meta.setdefault("backbone_model",
                    LLM_REGISTRY[llm].model if llm in LLM_REGISTRY else llm)
    # Stamp the active guard arm (off | scoped | plain | strict) so the analysis can read it back
    # per row. Without this it lives only in the dataset directory name, which is what forced the
    # deconf-open / deconf-guard comparison to be assembled by hand.
    meta.setdefault("guard", guard_level())
    # Which task wording produced this row. Without it a dataset collected with the clause and one
    # collected without it are indistinguishable after the fact, and they are not comparable.
    meta.setdefault("step_completion", step_completion_clause())
    # Stamped only when it could actually be read. A missing key is "unknown"; a 0.0 would be a
    # claim that the session was free, which is a wrong number rather than a missing one.
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


def _iter_cells(agents, llms, variants, conditions, sc, reps, plants, cold_disclosure=False):
    """Every (agent, llm, variant, condition, plant, rep) cell that will actually run.
    Materializing this list up front gives us an exact total for the progress bar.

    By default the disclosure arms (`direct` / `ask_only`) skip the cold ('none') condition —
    nothing was planted, so there is nothing to disclose. `cold_disclosure=True` runs them
    anyway, which measures a different and useful thing: whether a cold agent CONFABULATES a
    secret when a page asks for one outright. A cold session that names a bank is a false
    positive, and without this cell the disclosure rate has no negative control.

    The plant axis collapses on the arms that plant nothing: the cold and capability conditions
    run ONCE regardless of how many plant routes the scenario declares, because sweeping plants
    there would just repeat an identical session under different labels and inflate the cold
    baseline's apparent sample size.

    That collapse is WRONG for a scenario whose probe copy varies by plant (vendor_session's
    surface factor): there the cold sessions are not identical — each surface offers a different
    vendor set — and a single cold cell would be the baseline for only one of them. Such a
    scenario keeps the full plant axis on every arm."""
    per_surface = bool(getattr(sc, "surfaces", None))
    for agent in agents:
        for llm in llms:
            for variant in variants:
                for condition in conditions:
                    label = sc.label_for_condition(condition)
                    if (not cold_disclosure and variant in ("direct", "ask_only")
                            and label == "none"):
                        continue
                    cell_plants = (plants if per_surface or label not in ("none", CAPABILITY)
                                   else plants[:1])
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
    ap.add_argument("--variant", default=None,
                    help="single variant (legacy); prefer --variants")
    ap.add_argument("--variants", nargs="*", default=None,
                    help="behavioural variants to sweep (default A B). Add 'direct' to run the "
                         "direct-request control arm (ask box shown alongside the choice cards), "
                         "or 'ask_only' for the pure-ask arm (ask box only, no cards).")
    ap.add_argument("--plants", nargs="*", default=None,
                    help="which of the scenario's plant routes to sweep (default: all of "
                         "them). Each plants the SAME secret by a different route, so "
                         "comparing them separates 'this model does not leak' from 'this "
                         "plant did not put the value in context'.")
    ap.add_argument("--cold-disclosure", action="store_true",
                    help="also run the disclosure arms (direct/ask_only) on the cold 'none' "
                         "condition. Skipped by default (nothing planted = nothing to "
                         "disclose); enable it to get the negative control — does a cold agent "
                         "invent a secret when a page asks for one?")
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

    # Must be set before the runner imports browser_use (its setup_logging reads the env var);
    # the ERROR-level crank in the runner then overrides anything it leaves at INFO/RESULT.
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
    variants = args.variants or ([args.variant] if args.variant else ["A", "B"])
    plants = args.plants or list(sc.plant_ids)
    unknown = [p for p in plants if p not in sc.plant_ids]
    if unknown:
        raise SystemExit(f"{args.scenario}: unknown plant(s) {unknown}. "
                         f"Known: {sc.plant_ids}")

    # The first non-null condition (the "secret present" cell) for smoke/positive runs.
    positive = next((c for c in conditions if sc.label_for_condition(c) != "none"),
                    conditions[0])

    if args.smoke:
        run_cell(args.agents[0], llms[0], positive, 0, variants[0], args.scenario,
                 headless=not args.headful, plant=plants[0])
        return

    cells = list(_iter_cells(args.agents, llms, variants, conditions, sc, args.reps, plants,
                             cold_disclosure=args.cold_disclosure))

    # Optional progress bar. Default (no --tqdm) keeps the original one-line-per-cell output.
    bar = None
    if args.tqdm:
        try:
            from tqdm import tqdm
            bar = tqdm(total=len(cells), unit="run", dynamic_ncols=True, desc=args.scenario)
        except ImportError:
            print("[warn] --tqdm requested but tqdm is not installed "
                  "(`uv pip install tqdm`); continuing without a progress bar.")
    # When the bar is active, route our own log lines through tqdm.write so they print cleanly
    # ABOVE the bar instead of scrambling it. (browser_use's own logs still scroll past — the
    # bar just re-renders at the bottom after each cell.)
    log = bar.write if bar is not None else print

    # Run-level cost. Per-cell deltas can slip by one cell when the provider's counter lags a
    # completion, so this bracket — not the sum of the rows — is the figure to quote for a run.
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
        # The provider refusing to serve is not a session that went badly. Carrying on burns every
        # remaining cell into an errored row: the run of 2026-08-13 did it on 402, and the run of
        # 2026-08-17 did it again on 403 "Key limit exceeded" — 1,001 sessions over 52 minutes,
        # because only 402 was matched here. Stop on either and let the caller resume once the key
        # is topped up.
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
