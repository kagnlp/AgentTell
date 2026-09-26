#!/usr/bin/env bash
# The full behavioural sweep, restricted to the design the finalized datasets report on.
#
# WHY THIS EXISTS. `run_behavioural.sh` with no arguments plans every scenario in the registry
# — 1,740 sessions at reps=3. `scripts/finalize_results.py` then discards 110 of them, because
# `authstate_v1`, `checkout_neutral` and `locale_region` are registered and still run but are
# not among the 20 scenarios of `scenario-spec/new-scenario-v2.md` that the report covers. This
# wrapper runs the 20 and nothing else, so the sweep costs what the report uses.
#
# THE COUNT. reps=3, cold=10, dense-cold=20 over those 20 scenarios is exactly 1,630 sessions
# across 455 cells, which is what every `results/final-browseruse-*/results.db` already holds.
# Reaching it in full is what keeps a new backbone comparable: `finalize_results.common_floor`
# trims every cell to the THINNEST model, so one short cell here pulls all the other finalized
# datasets down with it when they are rebuilt together.
#
# BACKBONE-AGNOSTIC, like the script it delegates to. `.env` picks the backbone
# (OPENROUTER_MODEL), the upstream (OPENROUTER_PROVIDER) and the dataset (SCT_DATASET).
#
# The scenario roster is READ FROM `finalize_results.SPEC_V2` rather than copied, so the sweep
# cannot come to plan a different set of scenarios than the finalizer reports on.
#
# RESUMABLE. Sessions already recorded without an error count toward the target, so a run killed
# at hour 30 resumes at the cell it stopped on. Errored rows never count.
#
# Usage:
#   bash scripts/run_final_sweep.sh              # run / resume the whole 1,630
#   PLAN=1 bash scripts/run_final_sweep.sh       # show the plan and the count, run nothing
#   SCENARIO_REPS="checkout=1:3" bash scripts/run_final_sweep.sh   # reduce a refused scenario
#
# Requires the servers to be up on the SAME dataset:  ./scripts/serve.sh
set -uo pipefail
cd "$(dirname "$0")/.."

LLM="${LLM:-openrouter}"
REPS="${REPS:-3}"

# Wall-clock ceiling per session. The runner's own default is 600 s, chosen against backbones
# whose median session is 45-102 s; for those, exceeding it meant a stalled browser-use click
# (0-6 sessions in ~1,800, i.e. a pathology) and abandoning them was right.
#
# It is the wrong number for a SLOW backbone. Measured on kimi-k2.6: stigma_platform takes 874 s
# and sensitive_access 500 s in normal operation, running their 25 steps at kimi's per-step
# latency. At 600 s those are abandoned and re-run forever — the cell never fills, so the sweep
# never reaches the design and `finalize_results.common_floor` would trim EVERY finalized dataset
# down to kimi's shortfall, silently shrinking the other four models' reported samples.
#
# 1200 s clears the observed legitimate sessions while still catching the 5.9 h stall the ceiling
# exists for. It does not make the comparison less fair: the other four datasets were collected
# BEFORE this ceiling existed and kept sessions of 21,107 s, 17,029 s and 16,340 s as results,
# three of gpt-5.6-luna's and six of claude's over-600 s sessions being in the reported 1,630.
# Raising it here brings kimi's treatment closer to theirs, not further from it.
export SCT_SESSION_TIMEOUT_S="${SCT_SESSION_TIMEOUT_S:-1200}"
COLD_REPS="${COLD_REPS:-10}"
DENSE_COLD_REPS="${DENSE_COLD_REPS:-20}"
ATTACKER_PORT="${ATTACKER_PORT:-8001}"

DATASET="$(uv run python -c 'from orchestrator.config import DATASET; print(DATASET)')"
if [ -z "$DATASET" ]; then
  echo "[fatal] SCT_DATASET is unset. This sweep must write to its own results/<name>/." >&2
  exit 1
fi

# --- the servers must be up AND on this dataset -----------------------------------------------
# run_behavioural.sh does not check this; the mismatch is silent and ruins the run. The attacker
# origin binds SCT_DATASET at startup, so clicks land in the OLD events.db while results.db fills
# up in the new one, and every session then scores as an agent that did nothing.
SERVER_PID="$(ss -lptnH "sport = :${ATTACKER_PORT}" 2>/dev/null | grep -oP 'pid=\K[0-9]+' | head -1 || true)"
if [ -z "$SERVER_PID" ]; then
  echo "[fatal] nothing listening on :$ATTACKER_PORT — start ./scripts/serve.sh first" >&2
  exit 1
fi
SERVER_DS="$(tr '\0' '\n' < "/proc/${SERVER_PID}/environ" 2>/dev/null | sed -n 's/^SCT_DATASET=//p')"
# The servers inherit SCT_DATASET from .env rather than the environment when serve.sh is started
# without it exported, in which case /proc shows nothing and the value came from the same file
# this script just read. Only a DISAGREEMENT is fatal.
if [ -n "$SERVER_DS" ] && [ "$SERVER_DS" != "$DATASET" ]; then
  echo "[fatal] the attacker origin is logging events into '$SERVER_DS' but this run writes" >&2
  echo "        sessions into '$DATASET'. Restart serve.sh with the same value." >&2
  exit 1
fi

SPEC_V2="$(uv run python -c "
import sys
sys.path.insert(0, 'scripts')
from finalize_results import SPEC_V2
print(' '.join(SPEC_V2))")"
[ -z "$SPEC_V2" ] && { echo "[fatal] could not read the scenario roster" >&2; exit 1; }

echo "=================================================================="
echo " final sweep — the $(echo "$SPEC_V2" | wc -w) scenarios of the paper"
echo " backbone : $LLM -> $(uv run python -c "
from orchestrator.config import LLM_REGISTRY; print(LLM_REGISTRY['$LLM'].model)")"
echo " upstream : ${OPENROUTER_PROVIDER:-$(sed -n 's/^OPENROUTER_PROVIDER=//p' .env | head -1)}"
echo " dataset  : $DATASET"
echo " design   : reps=$REPS cold=$COLD_REPS dense-cold=$DENSE_COLD_REPS"
echo " ceiling  : ${SCT_SESSION_TIMEOUT_S}s per session"
echo "=================================================================="

# shellcheck disable=SC2086  the roster is a space-separated list of scenario keys by design
PLAN="${PLAN:-}" REPS="$REPS" COLD_REPS="$COLD_REPS" DENSE_COLD_REPS="$DENSE_COLD_REPS" \
  LLM="$LLM" SCENARIO_REPS="${SCENARIO_REPS:-}" \
  bash scripts/run_behavioural.sh $SPEC_V2
sweep_status=$?
[ -n "${PLAN:-}" ] && exit "$sweep_status"

# --- optional: finalize THIS dataset alone -----------------------------------------------------
# FINALIZE=1 builds results/final-<dataset>/ from this dataset and nothing else. Scoping it to one
# dataset is what keeps the run self-contained: the default roster balances across every model in
# `finalize_results.DEFAULT_DATASETS`, and `common_floor` trims each cell to the THINNEST of them,
# so a default --force rebuild would rewrite the other four models' finalized samples too.
#
# The trade is real and is the reason this is opt-in rather than automatic. Balancing one dataset
# against itself is only equivalent to balancing it against the others WHEN IT REACHES THE FULL
# DESIGN in every cell — then the floor is the design target either way. If it is short anywhere,
# this produces a final/ that is internally consistent but holds fewer sessions than the other
# models do in those cells, so it is NOT the cross-model comparison the report wants. The
# shortfall is listed in BALANCE.md; check it before quoting a number beside another backbone.
if [ -n "${FINALIZE:-}" ]; then
  outstanding="$(uv run python -m orchestrator.coverage --reps "$REPS" --cold-reps "$COLD_REPS" \
      --dense-cold-reps "$DENSE_COLD_REPS" --scenarios $SPEC_V2 --total)"
  echo
  if [ "${outstanding:-1}" -ne 0 ]; then
    echo "[finalize] SKIPPED — $outstanding sessions still outstanding. Finalizing now would"
    echo "           bake the shortfall into results/final-$DATASET/. Resume the sweep first."
    exit "$sweep_status"
  fi
  echo "[finalize] sweep complete; building results/final-$DATASET/ from this dataset only"
  uv run python scripts/finalize_results.py --datasets "$DATASET" \
      --reps "$REPS" --cold-reps "$COLD_REPS" --dense-cold-reps "$DENSE_COLD_REPS" --force
fi
exit "$sweep_status"
