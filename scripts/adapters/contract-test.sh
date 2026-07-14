#!/usr/bin/env bash
# contract-test.sh — verifies requested adapters or complete role routes.
set -uo pipefail
FAIL=0
note() { echo "[contract-test] $*"; }
bad() { echo "[contract-test] FAIL: $*" >&2; FAIL=1; }

KIT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
GLOBAL_ENV="${FACTORY_GLOBAL_ENV:-$HOME/.factory/global.env}"
if [[ -f "$GLOBAL_ENV" ]]; then
  # shellcheck disable=SC1090
  source "$GLOBAL_ENV"
fi
# shellcheck disable=SC1091
source "$KIT_DIR/scripts/lib/backend-policy.sh"

MODE="adapters"
ADAPTERS="claude-code,codex"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --adapters) MODE="adapters"; ADAPTERS="$2"; shift 2 ;;
    --routes) MODE="routes"; shift ;;
    *) echo "usage: contract-test.sh [--adapters a,b | --routes]" >&2; exit 2 ;;
  esac
done

check_adapter() {
  local adapter="$1"
  factory_probe_adapter "$adapter"
  if [[ "$PROBE_STATE" == "READY" ]]; then
    note "$adapter: READY (${PROBE_REASON})"
  else
    bad "$adapter: $PROBE_STATE (${PROBE_REASON})"
  fi
}

if [[ "$MODE" == "routes" ]]; then
  for role in planner spec-linter; do
    if factory_resolve_role "$role"; then
      note "$role route: $FACTORY_SELECTED_ADAPTER ($FACTORY_SELECTION_REASON)"
    else
      bad "$role route: ${FACTORY_RESOLVE_ERROR:-unknown}"
    fi
  done
else
  OLD_IFS="$IFS"
  IFS=,
  for adapter in $ADAPTERS; do
    check_adapter "$adapter"
  done
  IFS="$OLD_IFS"
fi

command -v timeout >/dev/null 2>&1 ||
  bad "GNU timeout not on PATH (brew install coreutils)"
command -v python3 >/dev/null 2>&1 ||
  bad "python3 not on PATH (required for process isolation and redaction)"

if [[ $FAIL -eq 0 ]]; then
  note "requested adapter contracts hold"
else
  echo "[contract-test] One or more routes are unavailable or invalid. Fix config/auth/version before any factory run." >&2
fi
exit $FAIL
