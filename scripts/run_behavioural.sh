#!/usr/bin/env bash
set -uo pipefail
cd "$(dirname "$0")/.."

REPS="${REPS:-3}"
COLD_REPS="${COLD_REPS:-10}"
DENSE_COLD_REPS="${DENSE_COLD_REPS:-20}"
SCENARIO_REPS="${SCENARIO_REPS:-}"
TASKS="${TASKS:-}"
export SCT_GUARD="${SCT_GUARD:-main}"

LLM="${LLM:-openrouter}"
PLANFILE="$(mktemp)"
trap 'rm -f "$PLANFILE"' EXIT

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

COVER=(uv run python -m orchestrator.coverage
       --reps "$REPS" --cold-reps "$COLD_REPS" --dense-cold-reps "$DENSE_COLD_REPS")
[ -n "$SCENARIO_REPS" ] && COVER+=(--scenario-reps $SCENARIO_REPS)
[ -n "$TASKS" ] && COVER+=(--tasks "$TASKS")

want=("$@")
[ ${#want[@]} -gt 0 ] && COVER+=(--scenarios "${want[@]}")

"${COVER[@]}" > "$PLANFILE" || { echo "[fatal] could not compute outstanding cells" >&2; exit 1; }
TOTAL="$("${COVER[@]}" --total)"
DONE=0

echo "=================================================================="
echo " arm A — behavioural, guard=$SCT_GUARD, reps=$REPS${TASKS:+, tasks=$TASKS}"
[ -n "$SCENARIO_REPS" ] && echo " overrides   : $SCENARIO_REPS   (reduced, NOT excluded)"
echo " dataset     : ${SCT_DATASET:-(from .env)}"
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
  if ! uv run python -m orchestrator.spend --llm "$LLM" --check >/dev/null; then
    abort_out_of_credit "provider refused a session-sized request before $scen/$conds"
  fi

  n_plants=$(echo "$plants" | tr ',' ' ' | wc -w)
  [ "$plants" = "-" ] && n_plants=1
  batch=$(( n_plants * reps_ ))

  echo "== $scen | $conds | plants=$plants | reps=$reps_  ($batch sessions)"
  if [ "$plants" = "-" ]; then
    uv run python -m orchestrator.run_matrix --scenario "$scen" --llms "$LLM" \
        --conditions "$conds" --variants A --reps "$reps_" --quiet --tqdm
  else
    uv run python -m orchestrator.run_matrix --scenario "$scen" --llms "$LLM" \
        --conditions "$conds" --plants ${plants//,/ } \
        --variants A --reps "$reps_" --quiet --tqdm
  fi
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
uv run python scripts/reconcile_events.py --apply || true
uv run python -m analysis.export_report || true
