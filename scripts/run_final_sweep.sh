#!/usr/bin/env bash
set -uo pipefail
cd "$(dirname "$0")/.."

LLM="${LLM:-openrouter}"
REPS="${REPS:-3}"

export SCT_SESSION_TIMEOUT_S="${SCT_SESSION_TIMEOUT_S:-1200}"
COLD_REPS="${COLD_REPS:-10}"
DENSE_COLD_REPS="${DENSE_COLD_REPS:-20}"
ATTACKER_PORT="${ATTACKER_PORT:-8001}"

DATASET="$(uv run python -c 'from orchestrator.config import DATASET; print(DATASET)')"
if [ -z "$DATASET" ]; then
  echo "[fatal] SCT_DATASET is unset. This sweep must write to its own results/<name>/." >&2
  exit 1
fi

SERVER_PID="$(ss -lptnH "sport = :${ATTACKER_PORT}" 2>/dev/null | grep -oP 'pid=\K[0-9]+' | head -1 || true)"
if [ -z "$SERVER_PID" ]; then
  echo "[fatal] nothing listening on :$ATTACKER_PORT — start ./scripts/serve.sh first" >&2
  exit 1
fi
SERVER_DS="$(tr '\0' '\n' < "/proc/${SERVER_PID}/environ" 2>/dev/null | sed -n 's/^SCT_DATASET=//p')"
if [ -n "$SERVER_DS" ] && [ "$SERVER_DS" != "$DATASET" ]; then
  echo "[fatal] the attacker origin is logging events into '$SERVER_DS' but this run writes" >&2
  echo "        sessions into '$DATASET'. Restart serve.sh with the same value." >&2
  exit 1
fi

ROSTER="$(uv run python -c "
from harness.scenarios import SCENARIOS
print(' '.join(SCENARIOS))")"
[ -z "$ROSTER" ] && { echo "[fatal] could not read the scenario roster" >&2; exit 1; }

echo "=================================================================="
echo " sweep — $(echo "$ROSTER" | wc -w) scenarios, guard=${SCT_GUARD:-scoped}"
echo " backbone : $LLM -> $(uv run python -c "
from orchestrator.config import LLM_REGISTRY; print(LLM_REGISTRY['$LLM'].model)")"
echo " upstream : ${OPENROUTER_PROVIDER:-$(sed -n 's/^OPENROUTER_PROVIDER=//p' .env | head -1)}"
echo " dataset  : $DATASET"
echo " design   : reps=$REPS cold=$COLD_REPS dense-cold=$DENSE_COLD_REPS${TASKS:+ tasks=$TASKS}"
echo " ceiling  : ${SCT_SESSION_TIMEOUT_S}s per session"
echo "=================================================================="

PLAN="${PLAN:-}" REPS="$REPS" COLD_REPS="$COLD_REPS" DENSE_COLD_REPS="$DENSE_COLD_REPS" \
  LLM="$LLM" SCENARIO_REPS="${SCENARIO_REPS:-}" TASKS="${TASKS:-}" \
  bash scripts/run_behavioural.sh $ROSTER
exit $?
