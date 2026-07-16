#!/usr/bin/env bash
# Adapter: mock — for testing run-agent.sh mechanics without spending money.
# Env: MOCK_COST (default 0.42), MOCK_STATUS (default 0).
set -euo pipefail
[[ ${FACTORY_DISPATCH_LEASE_ID+x} != x ]] || {
  echo "dispatcher lease leaked into task adapter" >&2
  exit 97
}
while [[ $# -gt 0 ]]; do
  case "$1" in
    --budget|--max-turns|--timeout-min|--prompt-file|--workdir) shift 2;;
    --) shift; break;;
    *) shift;;
  esac
done
if [[ "${MOCK_SLEEP:-0}" != "0" ]]; then
  if [[ -n "${MOCK_DESCENDANT_PID_FILE:-}" ]]; then
    bash -c 'sleep "$1" & wait' -- "$MOCK_SLEEP" &
    MOCK_CHILD_PID=$!
    echo "$MOCK_CHILD_PID" > "$MOCK_DESCENDANT_PID_FILE"
    wait "$MOCK_CHILD_PID"
  else
    sleep "$MOCK_SLEEP"
  fi
fi
echo "mock adapter ran task: ${*:-<none>}"
if [[ "${MOCK_NO_COST:-0}" == "1" ]]; then
  echo "turns=3"
else
  echo "turns=3 cost_usd=${MOCK_COST:-0.42}"
fi
exit "${MOCK_STATUS:-0}"
