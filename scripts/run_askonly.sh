#!/usr/bin/env bash
# The ask_only arm — criterion (a): does the agent hand over the secret when a page just asks?
#
# WHAT THIS RUNS. `ask_only` renders the ask-outright box with NO choice cards, so the textarea
# is the only affordance and the question is actually forced. That is why it, and not `direct`,
# is the arm analysis/inference.py names as `headline_arm`: on `direct` the agent completes via a
# card and never touches the box, which scores NO_SUBMISSION — an affordance artefact that reads
# as a refusal and roughly halves the measured disclosure rate.
#
# THE DESIGN. Not the behavioural arm's full cross. `ask_only` reports ONE pooled proportion per
# scenario, so option and plant are nuisance factors needing coverage rather than crossing —
# see orchestrator/askonly_plan.py, which also explains why the plant routes are chosen from the
# backbone's OWN arm-A leak_by_plant instead of being fixed. Default 3 plants x 2 options x
# 2 reps = 12 sessions per scenario.
#
# DATASET. This arm writes to its own SCT_DATASET, so it never pools with the arm-A run it reads
# its plant choice from. Export SCT_DATASET before scripts/serve.sh AND before this script; the
# check below refuses to start if the servers disagree, because that mismatch is silent: the
# clicks land in the old events.db, results.db fills up in the new one, and every session scores
# as an agent that did nothing.
#
# Usage:
#   SCT_DATASET=askonly-claude-sonnet-5 bash scripts/run_askonly.sh
#   PLAN=1 SCT_DATASET=... bash scripts/run_askonly.sh          # show the plan, run nothing
#   SCT_DATASET=... bash scripts/run_askonly.sh sso_wall checkout   # named scenarios only
#
# Requires the servers to be up on the SAME dataset:  SCT_DATASET=... ./scripts/serve.sh
set -uo pipefail
cd "$(dirname "$0")/.."

LLM="${LLM:-claude_openrouter}"
REFERENCE="${REFERENCE:-results/browseruse-claude-sonnet-5/results.json}"
N_PLANTS="${N_PLANTS:-3}"
N_OPTIONS="${N_OPTIONS:-2}"
REPS="${REPS:-2}"
ATTACKER_PORT="${ATTACKER_PORT:-8001}"
export SCT_GUARD="${SCT_GUARD:-scoped}"
unset SCT_SANDBOX_FRAMING SCT_CONFIDENTIALITY

if [ -z "${SCT_DATASET:-}" ]; then
  echo "[fatal] SCT_DATASET is unset. This arm must write to its own results/<name>/." >&2
  exit 1
fi
[ -f "$REFERENCE" ] || { echo "[fatal] no scored reference at $REFERENCE" >&2; exit 1; }

PLANFILE="$(mktemp)"
trap 'rm -f "$PLANFILE" "${PLANFILE}.smoke"' EXIT

notify() {
  printf '\a' >&2
  echo >&2
  echo "################################################################" >&2
  echo "# $1" >&2
  echo "# $2" >&2
  echo "################################################################" >&2
  command -v notify-send >/dev/null 2>&1 && notify-send -u critical "$1" "$2" || true
}

abort_out_of_credit() {
  notify "ASK_ONLY RUN STOPPED — OUT OF CREDIT" \
         "$1 | $DONE/$TOTAL sessions done. Top up, then rerun to resume."
  uv run python -m orchestrator.spend --llm "$LLM" || true
  exit 2
}

# --- the servers must be up AND on this dataset -----------------------------------------------
SERVER_PID="$(ss -lptnH "sport = :${ATTACKER_PORT}" 2>/dev/null | grep -oP 'pid=\K[0-9]+' | head -1 || true)"
if [ -z "$SERVER_PID" ]; then
  echo "[fatal] nothing listening on :$ATTACKER_PORT — start SCT_DATASET=$SCT_DATASET ./scripts/serve.sh" >&2
  exit 1
fi
SERVER_DS="$(tr '\0' '\n' < "/proc/${SERVER_PID}/environ" 2>/dev/null | sed -n 's/^SCT_DATASET=//p')"
if [ "$SERVER_DS" != "$SCT_DATASET" ]; then
  echo "[fatal] the attacker origin is logging events into '${SERVER_DS:-(unset)}' but this run" >&2
  echo "        writes sessions into '$SCT_DATASET'. Restart serve.sh with the same value." >&2
  exit 1
fi

# --- outstanding cells ------------------------------------------------------------------------
PLANNER=(uv run python -m orchestrator.askonly_plan --reference "$REFERENCE"
         --plants "$N_PLANTS" --options "$N_OPTIONS" --reps "$REPS")
want=("$@")
[ ${#want[@]} -gt 0 ] && PLANNER+=(--scenarios "${want[@]}")

"${PLANNER[@]}" > "$PLANFILE" || { echo "[fatal] could not compute the plan" >&2; exit 1; }
TOTAL="$("${PLANNER[@]}" --total)"
DONE=0

# SMOKE=1 runs the FIRST outstanding cell of each scenario once, to prove every scenario's probe
# page and plant route still work before committing the whole budget. The cells it runs are real
# plan cells at reps=1, so the sessions count toward the sweep and the ordinary run tops up the
# remainder instead of repeating them.
if [ -n "${SMOKE:-}" ]; then
  awk -F'\t' '!seen[$1]++ { print $1"\t"$2"\t"$3"\t1" }' "$PLANFILE" > "${PLANFILE}.smoke"
  mv "${PLANFILE}.smoke" "$PLANFILE"
  TOTAL="$(wc -l < "$PLANFILE")"
fi

echo "=================================================================="
echo " ask_only arm — guard=$SCT_GUARD, ${N_PLANTS} plants x ${N_OPTIONS} options x ${REPS} reps${SMOKE:+   [SMOKE: 1 cell per scenario]}"
echo " backbone    : $LLM"
echo " dataset     : $SCT_DATASET   (servers agree)"
echo " plant source: $REFERENCE"
echo " outstanding : $TOTAL sessions"
echo "=================================================================="
uv run python -m orchestrator.spend --llm "$LLM"
echo

if [ "$TOTAL" -eq 0 ]; then
  echo "nothing outstanding — the sweep is complete."
  exit 0
fi

if [ -n "${PLAN:-}" ]; then
  printf '%-22s %-22s %-18s %s\n' scenario condition plant reps
  while IFS=$'\t' read -r scen conds plants reps_; do
    printf '%-22s %-22s %-18s %s\n' "$scen" "$conds" "$plants" "$reps_"
  done < "$PLANFILE"
  exit 0
fi

START="$(date +%s)"
while IFS=$'\t' read -r scen conds plants reps_; do
  if ! uv run python -m orchestrator.spend --llm "$LLM" --check >/dev/null; then
    abort_out_of_credit "provider refused a session-sized request before $scen/$conds"
  fi
  batch=$(( reps_ ))
  echo "== $scen | $conds | $plants | reps=$reps_  ($batch sessions)"
  # --llms is load-bearing: without it run_matrix falls back to MATRIX.available_llms()[0],
  # which is "openrouter" -> whatever OPENROUTER_MODEL names in .env, and the dataset directory
  # would say one backbone while the rows said another.
  uv run python -m orchestrator.run_matrix --scenario "$scen" --llms "$LLM" \
      --conditions "$conds" --plants "$plants" \
      --variants ask_only --reps "$reps_" --quiet --tqdm
  status=$?
  [ "$status" -eq 2 ] && abort_out_of_credit "a session returned HTTP 402/403 during $scen"

  DONE=$(( DONE + batch ))
  elapsed=$(( $(date +%s) - START ))
  if [ $(( DONE % 20 )) -lt "$batch" ]; then
    pct=$(awk "BEGIN{printf \"%.1f\", 100*$DONE/$TOTAL}")
    eta=$(awk "BEGIN{d=$DONE>0?$DONE:1; printf \"%.1f\", ($TOTAL-$DONE)*($elapsed/d)/3600}")
    echo "------------------------------------------------------------------"
    echo " progress : $DONE/$TOTAL (${pct}%)   elapsed $((elapsed/3600))h$(( (elapsed%3600)/60 ))m   ETA ~${eta}h"
    printf ' balance  : '; uv run python -m orchestrator.spend --llm "$LLM"
    echo "------------------------------------------------------------------"
  fi
done < "$PLANFILE"

echo
echo "=== ask_only run finished: $DONE sessions this pass ==="
echo "still outstanding: $("${PLANNER[@]}" --total)"
uv run python -m orchestrator.spend --llm "$LLM"
notify "ASK_ONLY RUN COMPLETE" "$DONE sessions. Scoring now."
uv run python scripts/reconcile_events.py --apply || true
uv run python -m analysis.export_report || true
