#!/usr/bin/env bash
# Adapter: mock — for testing run-agent.sh mechanics without spending money.
# Env: MOCK_COST (default 0.42), MOCK_STATUS (default 0).
set -euo pipefail
WORKDIR=""
[[ ${FACTORY_DISPATCH_LEASE_ID+x} != x ]] || {
  echo "dispatcher lease leaked into task adapter" >&2
  exit 97
}
[[ ${FACTORY_CERTIFIED_PRODUCT_ORIGIN+x} != x ]] || {
  echo "certified product origin leaked into task adapter" >&2
  exit 97
}
[[ ${FACTORY_TRUSTED_PRODUCT_ORIGIN+x} != x ]] || {
  echo "trusted product origin leaked into task adapter" >&2
  exit 97
}
[[ ${PRODUCT_REMOTE+x} != x ]] || {
  echo "captured product origin leaked into task adapter" >&2
  exit 97
}
while [[ $# -gt 0 ]]; do
  case "$1" in
    --workdir) WORKDIR="$2"; shift 2;;
    --budget|--max-turns|--timeout-min|--prompt-file) shift 2;;
    --) shift; break;;
    *) shift;;
  esac
done
if [[ "${MOCK_COMMIT_WORKDIR:-0}" == "1" ]]; then
  printf 'mock role output\n' >> "$WORKDIR/mock-role-output.txt"
  git -C "$WORKDIR" add mock-role-output.txt
fi
if [[ "${MOCK_PROTECTED_TICKET_MUTATION:-0}" == "1" ]]; then
  for ticket_file in "$WORKDIR"/factory/tickets/T-*.md; do
    [[ -f "$ticket_file" ]] || continue
    python3 - "$ticket_file" <<'PY'
import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
text = re.sub(r"^State:\s*.*$", "State: Done", path.read_text(), count=1, flags=re.MULTILINE)
text += "Operator-Approval: Linear\n"
path.write_text(text)
PY
    git -C "$WORKDIR" add "${ticket_file#"$WORKDIR/"}"
    break
  done
fi
if [[ -n "${MOCK_SPEC_LINT_VERDICT:-}" ]]; then
  for ticket_file in "$WORKDIR"/factory/tickets/T-*.md; do
    [[ -f "$ticket_file" ]] || continue
    printf 'SPEC-LINT: %s\n' "$MOCK_SPEC_LINT_VERDICT" >> "$ticket_file"
    git -C "$WORKDIR" add "${ticket_file#"$WORKDIR/"}"
    break
  done
fi
if [[ "${MOCK_COMMIT_WORKDIR:-0}" == "1" ||
      "${MOCK_PROTECTED_TICKET_MUTATION:-0}" == "1" ||
      -n "${MOCK_SPEC_LINT_VERDICT:-}" ]]; then
  git -C "$WORKDIR" -c user.name=mock -c user.email=mock@example.com \
    commit -m "Mock role output" >/dev/null
fi
if [[ -n "${MOCK_PUSHURL:-}" ]]; then
  git -C "$WORKDIR" config remote.origin.pushurl "$MOCK_PUSHURL"
fi
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
