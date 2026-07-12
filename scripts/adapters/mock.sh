#!/usr/bin/env bash
# Adapter: mock — for testing run-agent.sh mechanics without spending money.
# Env: MOCK_COST (default 0.42), MOCK_STATUS (default 0).
set -euo pipefail
while [[ $# -gt 0 ]]; do
  case "$1" in
    --budget|--max-turns|--timeout-min|--prompt-file|--workdir) shift 2;;
    --) shift; break;;
    *) shift;;
  esac
done
echo "mock adapter ran task: ${*:-<none>}"
if [[ "${MOCK_NO_COST:-0}" == "1" ]]; then
  echo "turns=3"
else
  echo "turns=3 cost_usd=${MOCK_COST:-0.42}"
fi
exit "${MOCK_STATUS:-0}"
