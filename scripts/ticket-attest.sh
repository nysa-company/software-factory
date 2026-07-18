#!/usr/bin/env bash
# Validate the opaque dispatcher lease without exposing it to Python or output.
set -euo pipefail

KIT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
# shellcheck disable=SC1091
source "$KIT_DIR/scripts/lib/dispatch-leases.sh"

LEASE_ID="${FACTORY_DISPATCH_LEASE_ID:-}"
unset FACTORY_DISPATCH_LEASE_ID
TICKET=""
for ((index=1; index <= $#; index++)); do
  if [[ "${!index}" == "--ticket" ]]; then
    next=$((index + 1))
    [[ "$next" -le "$#" ]] && TICKET="${!next}"
    break
  fi
done
[[ "$TICKET" =~ ^T-[0-9]+$ ]] || {
  echo "ticket-attest: invalid ticket identifier" >&2
  exit 2
}
if ! factory_dispatch_require_lease "${FACTORY_ROOT:?}" "$TICKET" "$LEASE_ID"; then
  echo "ticket-attest: $FACTORY_DISPATCH_LEASE_ERROR" >&2
  exit 1
fi

PYTHON_BIN="/usr/bin/python3"
[[ ! -x /Library/Developer/CommandLineTools/usr/bin/python3 ]] ||
  PYTHON_BIN="/Library/Developer/CommandLineTools/usr/bin/python3"
exec "$PYTHON_BIN" -I -S "$KIT_DIR/scripts/ticket-attest.py" "$@"
