#!/usr/bin/env bash
# contract-test.sh — verifies requested adapters or complete role routes.
set -uo pipefail
FAIL=0
note() { echo "[contract-test] $*"; }
bad() { echo "[contract-test] FAIL: $*" >&2; FAIL=1; }

KIT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck disable=SC1091
source "$KIT_DIR/scripts/lib/plain-config.sh"
GLOBAL_ENV="${FACTORY_GLOBAL_ENV:-$HOME/.factory/global.env}"
factory_clear_plain_config_keys "$FACTORY_GLOBAL_CONFIG_KEYS"
if [[ -f "$GLOBAL_ENV" ]]; then
  factory_load_plain_config "$GLOBAL_ENV" global \
    "$FACTORY_GLOBAL_CONFIG_KEYS" "" 1 || exit 2
fi
# shellcheck disable=SC1091
source "$KIT_DIR/scripts/lib/backend-policy.sh"

MODE="adapters"
ADAPTERS="claude-code,codex"
PROFILE_ID=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --adapters) MODE="adapters"; ADAPTERS="$2"; shift 2 ;;
    --routes) MODE="routes"; shift ;;
    --profile)
      MODE="profile"
      shift
      if [[ $# -gt 0 && "$1" != --* ]]; then
        PROFILE_ID="$1"
        shift
      fi
      ;;
    *) echo "usage: contract-test.sh [--adapters a,b | --routes | --profile [profile-id]]" >&2; exit 2 ;;
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
elif [[ "$MODE" == "profile" ]]; then
  PROFILE_PLAN="$(mktemp "${TMPDIR:-/tmp}/factory-contract-profile.XXXXXX")" || exit 2
  cleanup_profile() { rm -f "$PROFILE_PLAN"; }
  trap cleanup_profile EXIT
  if [[ -z "$PROFILE_ID" ]]; then
    if factory_load_model_probe_context; then
      PROFILE_ID="$FACTORY_MODEL_PROFILE_ID"
      PROFILE_DISABLED="$FACTORY_DISABLED_ROUTE_IDS"
    else
      bad "profile context: ${FACTORY_RESOLVE_ERROR:-unknown}"
      PROFILE_DISABLED=""
    fi
  else
    PROFILE_DISABLED=""
  fi
  if [[ "$FAIL" -eq 0 ]] &&
     factory_resolve_model_profile "$PROFILE_ID" "$PROFILE_PLAN" "$PROFILE_DISABLED"; then
    for role in planner builder narrator spec-linter test-author reviewer; do
      if factory_select_model_role "$PROFILE_PLAN" "$role"; then
        note "$role route: $FACTORY_SELECTED_ADAPTER/$FACTORY_SELECTED_MODEL ($FACTORY_SELECTED_ROUTE_ID)"
      else
        bad "$role route: ${FACTORY_RESOLVE_ERROR:-unknown}"
      fi
    done
  elif [[ "$FAIL" -eq 0 ]]; then
    bad "profile $PROFILE_ID: ${FACTORY_RESOLVE_ERROR:-unknown}"
  fi
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
