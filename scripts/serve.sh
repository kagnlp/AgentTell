#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

ATTACKER_PORT="${ATTACKER_PORT:-8001}"
BANK_PORT="${BANK_PORT:-8002}"
SERVICES_PORT="${SERVICES_PORT:-8004}"

uv run python -c "from orchestrator.config import DATASET, EVENT_LOG_DB; print(f'[serve] dataset={DATASET or \"(default flat results/)\"}  ->  events {EVENT_LOG_DB}')"

busy=0
for port in "${ATTACKER_PORT}" "${BANK_PORT}" "${SERVICES_PORT}"; do
  pid="$(ss -lptnH "sport = :${port}" 2>/dev/null | grep -oP 'pid=\K[0-9]+' | head -1 || true)"
  if [ -n "${pid}" ]; then
    ds="$(tr '\0' '\n' < "/proc/${pid}/environ" 2>/dev/null | grep '^SCT_DATASET=' || echo 'SCT_DATASET=(unset)')"
    echo "[serve] ERROR: port ${port} already held by pid ${pid} (${ds})" >&2
    busy=1
  fi
done
if [ "${busy}" -ne 0 ]; then
  echo "[serve] Refusing to start — stop the old servers first, e.g.:" >&2
  echo "        pkill -f 'bin/uvicorn (attacker|mock_origins)'" >&2
  exit 1
fi

echo "Starting attacker origin  -> http://127.0.0.1:${ATTACKER_PORT}"
uv run uvicorn attacker.app:app --host 127.0.0.1 --port "${ATTACKER_PORT}" &
A=$!
echo "Starting mock bank        -> http://127.0.0.1:${BANK_PORT}"
uv run uvicorn mock_origins.bank.app:app --host 127.0.0.1 --port "${BANK_PORT}" &
B=$!
echo "Starting mock services    -> http://127.0.0.1:${SERVICES_PORT}"
uv run uvicorn mock_origins.services.app:app --host 127.0.0.1 --port "${SERVICES_PORT}" &
S=$!

trap "echo; echo 'stopping...'; kill $A $B $S 2>/dev/null || true" INT TERM
wait
