#!/usr/bin/env bash
# Arm A — the behavioural headline run, guard=scoped, on the OpenRouter backbone in .env.
#
# BACKBONE-AGNOSTIC. Everything model-specific lives in .env: OPENROUTER_MODEL picks the
# backbone, SCT_DATASET picks the results/<name>/ it writes to, OPENROUTER_PROVIDER pins the
# upstream so the price is the price you costed, and OPENROUTER_MAX_TOKENS /
# OPENROUTER_REASONING_EFFORT keep a reasoning backbone from spending its whole completion
# budget thinking and truncating its structured output. Switch backbone = edit .env and
# RESTART scripts/serve.sh, because the attacker origin binds SCT_DATASET at startup.
#
# THE DESIGN. Full control (a): the held target rotates across EVERY option of every scenario,
# not a 3-value subset, so ScenarioLeak averages over the whole label set and the k x k confusion
# matrix is complete. All five plant routes per scenario, `REPS` sessions per (target, plant)
# cell. Cold is denser for the scenarios whose cold arm is a genuine prior over options rather
# than a near-deterministic fallback, because those need a precise P_cold rather than a token one.
#
# ~1,600 sessions at reps=3. Budget roughly $22 and 40+ hours serial. Run it under tmux/screen.
#
# BALANCE GUARD. The run of 2026-08-13 died on HTTP 402 after 57 sessions and kept going, burning
# the rest of the plan into errored rows. This script refuses to start a scenario it cannot pay
# for, and aborts the moment a session comes back 402 — loudly, with a desktop notification and a
# non-zero exit. Note the check ISSUES a tiny probe request rather than reading a balance field:
# that same key reported $321 of `limit_remaining` while refusing every request, so only the
# provider's own admission control is authoritative (see orchestrator/spend.can_afford).
#
# RESUMABLE. Sessions already recorded without an error count toward a scenario's target, so a
# run killed at hour 30 picks up where it stopped. Errored rows never count — a dead session is
# re-run, never accepted, which is what keeps the resume logic honest.
#
# Usage:
#   bash scripts/run_behavioural.sh                    # run / resume everything
#   bash scripts/run_behavioural.sh sso_wall checkout  # named scenarios only
#   PLAN=1 bash scripts/run_behavioural.sh             # show the plan, run nothing
#   REPS=5 bash scripts/run_behavioural.sh             # denser cells
#
# Requires the servers to be up:  ./scripts/serve.sh
set -uo pipefail
cd "$(dirname "$0")/.."

REPS="${REPS:-3}"                 # sessions per (target, plant) cell
COLD_REPS="${COLD_REPS:-10}"      # cold sessions per scenario
DENSE_COLD_REPS="${DENSE_COLD_REPS:-20}"   # ...for scenarios with a real cold prior
# Per-scenario target overrides, "key=loaded[:cold]", space separated. Use this where the
# BACKBONE refuses a scenario outright: the full allocation then buys nothing but repeated
# refusals. The scenario still RUNS at a reduced count — a refusal is a result, and dropping it
# would be a silent exclusion. gpt-5.6-luna refuses the payment scenarios ("I can't log into an
# online banking account... or place an order using payment/account information"), which is why
# checkout_neutral is trimmed here; leave SCENARIO_REPS empty for a backbone that does not.
SCENARIO_REPS="${SCENARIO_REPS:-}"
export SCT_GUARD="${SCT_GUARD:-scoped}"
unset SCT_SANDBOX_FRAMING SCT_CONFIDENTIALITY

LLM="${LLM:-openrouter}"
PLANFILE="$(mktemp)"
trap 'rm -f "$PLANFILE"' EXIT

# --- notification -----------------------------------------------------------------------------
notify() {
  local title="$1" body="$2"
  printf '\a' >&2
  echo >&2
  echo "################################################################" >&2
  echo "# $title" >&2
  echo "# $body" >&2
  echo "################################################################" >&2
  command -v notify-send >/dev/null 2>&1 && notify-send -u critical "$title" "$body" || true
}

abort_out_of_credit() {
  notify "SIDE-CHANNEL RUN STOPPED — OUT OF CREDIT" \
         "$1 | $DONE/$TOTAL sessions done. Top up, then rerun this script to resume."
  uv run python -m orchestrator.spend --llm "$LLM" || true
  exit 2
}

# --- outstanding cells, computed per (condition, plant) so a resume runs only what is missing --
COVER=(uv run python -m orchestrator.coverage
       --reps "$REPS" --cold-reps "$COLD_REPS" --dense-cold-reps "$DENSE_COLD_REPS")
# shellcheck disable=SC2206
[ -n "$SCENARIO_REPS" ] && COVER+=(--scenario-reps $SCENARIO_REPS)

want=("$@")
[ ${#want[@]} -gt 0 ] && COVER+=(--scenarios "${want[@]}")

"${COVER[@]}" > "$PLANFILE" || { echo "[fatal] could not compute outstanding cells" >&2; exit 1; }
TOTAL="$("${COVER[@]}" --total)"
DONE=0

echo "=================================================================="
echo " arm A — behavioural, guard=$SCT_GUARD, reps=$REPS"
[ -n "$SCENARIO_REPS" ] && echo " overrides   : $SCENARIO_REPS   (reduced, NOT excluded)"
echo " dataset     : ${SCT_DATASET:-(from .env)}"
# Per-session cost and duration come from THIS dataset's own recorded sessions, not a constant:
# the three backbones measured so far span $0.0092-$0.0241, so a hardcoded rate misestimates by
# up to 2.6x. Falls back to a mid-range guess only when nothing has been costed yet.
RATE="$(uv run python -c "
import json, sqlite3, statistics
from orchestrator.config import RESULTS_DB
try:
    c = sqlite3.connect(f'file:{RESULTS_DB}?mode=ro', uri=True)
    costs = [v for (m,) in c.execute('select meta from sessions where error is null')
             if (v := json.loads(m or '{}').get('cost_usd')) and v > 0]
    secs = [r[0] for r in c.execute('select duration_s from sessions where error is null and duration_s is not null')]
    print(f'{statistics.mean(costs):.6f} {statistics.mean(secs):.1f}' if costs and secs else '0.015 90')
except Exception:
    print('0.015 90')" 2>/dev/null)"
[ -z "$RATE" ] && RATE="0.015 90"
echo " outstanding : $TOTAL sessions   (~\$$(awk "BEGIN{split(\"$RATE\",r,\" \"); printf \"%.2f\", $TOTAL*r[1]}"), ~$(awk "BEGIN{split(\"$RATE\",r,\" \"); printf \"%.1f\", $TOTAL*r[2]/3600}") h serial)"
echo " already run : $(uv run python -c "
import sqlite3
from orchestrator.config import RESULTS_DB
try:
    print(sqlite3.connect(RESULTS_DB).execute(
        \"select count(*) from sessions where variant='A' and error is null\").fetchone()[0])
except Exception: print(0)") usable sessions credited"
echo "=================================================================="
uv run python -m orchestrator.spend --llm "$LLM"
echo

if [ "$TOTAL" -eq 0 ]; then
  echo "nothing outstanding — the sweep is complete."
  exit 0
fi

if [ -n "${PLAN:-}" ]; then
  printf '%-20s %-18s %-6s %s\n' scenario condition reps plants
  while IFS=$'\t' read -r scen conds plants reps_; do
    printf '%-20s %-18s %-6s %s\n' "$scen" "$conds" "$reps_" "$plants"
  done < "$PLANFILE"
  exit 0
fi

START="$(date +%s)"
LAST_SCEN=""

while IFS=$'\t' read -r scen conds plants reps_; do
  # Refuse to START a batch we cannot pay for. Cheaper than finding out 40 sessions in.
  if ! uv run python -m orchestrator.spend --llm "$LLM" --check >/dev/null; then
    abort_out_of_credit "provider refused a session-sized request before $scen/$conds"
  fi

  n_plants=$(echo "$plants" | tr ',' ' ' | wc -w)
  [ "$plants" = "-" ] && n_plants=1
  batch=$(( n_plants * reps_ ))

  echo "== $scen | $conds | plants=$plants | reps=$reps_  ($batch sessions)"
  # --llms "$LLM" is load-bearing. Without it run_matrix falls back to the first entry of
  # MATRIX.available_llms(), which is "openrouter" -> whatever OPENROUTER_MODEL names in .env.
  # $LLM reached only the spend probes, so on 2026-08-23 a run launched as
  # LLM=claude_openrouter checked Claude's balance and then ran 715 gpt-5.6-luna sessions into
  # results/browseruse-claude-sonnet-5/ for 15 hours. The dataset directory said one backbone
  # and the rows said another.
  if [ "$plants" = "-" ]; then
    uv run python -m orchestrator.run_matrix --scenario "$scen" --llms "$LLM" \
        --conditions "$conds" --variants A --reps "$reps_" --quiet --tqdm
  else
    # shellcheck disable=SC2086
    uv run python -m orchestrator.run_matrix --scenario "$scen" --llms "$LLM" \
        --conditions "$conds" --plants ${plants//,/ } \
        --variants A --reps "$reps_" --quiet --tqdm
  fi
  # run_matrix exits 2 the moment any worker sees HTTP 402, and stops submitting the rest of the
  # batch rather than burning it into errored rows. (The previous guard here called a `saw_402`
  # helper that was never defined, so it had never once fired.)
  status=$?
  [ "$status" -eq 2 ] && abort_out_of_credit "a session returned HTTP 402 during $scen"

  DONE=$(( DONE + batch ))
  elapsed=$(( $(date +%s) - START ))
  if [ "$scen" != "$LAST_SCEN" ] || [ $(( DONE % 50 )) -lt "$batch" ]; then
    pct=$(awk "BEGIN{printf \"%.1f\", 100*$DONE/$TOTAL}")
    eta=$(awk "BEGIN{d=$DONE>0?$DONE:1; printf \"%.1f\", ($TOTAL-$DONE)*($elapsed/d)/3600}")
    echo "------------------------------------------------------------------"
    echo " progress : $DONE/$TOTAL this run (${pct}%)   elapsed $((elapsed/3600))h$(( (elapsed%3600)/60 ))m   ETA ~${eta}h"
    printf ' balance  : '; uv run python -m orchestrator.spend --llm "$LLM"
    echo "------------------------------------------------------------------"
    LAST_SCEN="$scen"
  fi
done < "$PLANFILE"

echo
echo "=== run finished: $DONE sessions this pass ==="
echo "still outstanding: $("${COVER[@]}" --total)"
uv run python -m orchestrator.spend --llm "$LLM"
notify "SIDE-CHANNEL RUN COMPLETE" "$DONE sessions this pass. Scoring now."
# Some backbones mistype the 32-char session id into the address bar, which files that session's
# clicks under a bogus id and leaves the real row looking like an agent that declined to act.
# Reattach those streams before scoring; unambiguous matches only (see the script's docstring).
uv run python scripts/reconcile_events.py --apply || true
uv run python -m analysis.export_report || true
