#!/usr/bin/env bash
# preflight.sh — kickoff checks before a ticket's first launch.
# The factory-launch preflight route runs this once per ticket before launch.
# Usage: preflight.sh --ticket T-NNN [--role ROLE]
# FACTORY_ROOT semantics match run-agent.sh (anchors factory/ under the repo root).
set -euo pipefail

TICKET="" ROLE="" LEASE_ID="" WORKDIR=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --ticket) TICKET="$2"; shift 2;;
    --role) ROLE="$2"; shift 2;;
    --lease) LEASE_ID="$2"; shift 2;;
    --workdir) WORKDIR="$2"; shift 2;;
    *) echo "unknown arg: $1" >&2; exit 2;;
  esac
done
[[ -n "$TICKET" ]] || { echo "usage: preflight.sh --ticket T-NNN" >&2; exit 2; }
[[ "$TICKET" =~ ^T-[0-9]+$ ]] || { echo "invalid ticket identifier" >&2; exit 2; }

KIT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
REPO_ROOT="${FACTORY_ROOT:-$(git rev-parse --show-toplevel 2>/dev/null || echo "$PWD")}"
FACTORY_DIR="$REPO_ROOT/factory"
OPERATOR_MAP="${FACTORY_OPERATOR_MAP:-$FACTORY_DIR/operator-map.json}"
CONTENT_ROOT="${WORKDIR:-$REPO_ROOT}"
LEDGER="${FACTORY_LEDGER:-$FACTORY_DIR/runtime-ledger.csv}"
ENV_FILE="${FACTORY_ENVELOPE:-$FACTORY_DIR/ENVELOPE.env}"
PROJECTED_TICKET_USD="${PROJECTED_TICKET_USD:-5.00}"
TICKET_SOURCE="$CONTENT_ROOT/factory/tickets/$TICKET.md"
TICKET_FILE="$TICKET_SOURCE"
MONEY="$KIT_DIR/scripts/lib/money.py"

FAIL=0
pass() { echo "PASS: $*"; }
fail() { echo "FAIL: $*"; FAIL=1; }
warn() { echo "WARN: $*"; }

# Runtime barriers, physical release validation, and existing ticket affinity
# are hard gates. None may fall through to backend probes.
# shellcheck disable=SC1091
source "$KIT_DIR/scripts/lib/kit-pin.sh"
# shellcheck disable=SC1091
source "$KIT_DIR/scripts/lib/dispatch-leases.sh"
# shellcheck disable=SC1091
source "$KIT_DIR/scripts/lib/plain-config.sh"
if [[ -f "$FACTORY_DIR/MAINTENANCE" ]]; then
  fail "MAINTENANCE file present ($FACTORY_DIR/MAINTENANCE) — factory control plane is paused"
  echo "PREFLIGHT FAIL"
  exit 1
fi
if ! factory_validate_kit_pin "$KIT_DIR" "$REPO_ROOT"; then
  fail "$FACTORY_KIT_PIN_ERROR"
  echo "PREFLIGHT FAIL"
  exit 1
fi
if [[ "$FACTORY_KIT_PIN_IMPLICIT" -eq 1 ]]; then
  pass "in-repo conformance product uses implicit physical kit pin (${FACTORY_KIT_SHA:0:7})"
elif [[ "$FACTORY_KIT_PROVENANCE_MODE" == "sealed" ]]; then
  pass "kit pin matches sealed physical release (${FACTORY_KIT_SHA:0:7})"
else
  pass "kit pin matches physical kit HEAD (${FACTORY_KIT_SHA:0:7})"
fi
if [[ -f "$TICKET_FILE" ]] &&
   ! factory_validate_ticket_kit_sha "$TICKET_FILE" "$FACTORY_KIT_SHA"; then
  fail "$FACTORY_TICKET_KIT_ERROR"
  echo "PREFLIGHT FAIL"
  exit 1
fi

if [[ -f "$TICKET_SOURCE" ]]; then
  EFFECTIVE_TICKET="$(mktemp "${TMPDIR:-/tmp}/effective-ticket.XXXXXX")"
  trap 'rm -f "$EFFECTIVE_TICKET"' EXIT
  if python3 "$KIT_DIR/scripts/lib/effective_ticket.py" \
    --ticket-file "$TICKET_SOURCE" --operator-map "$OPERATOR_MAP" \
    --ticket "$TICKET" > "$EFFECTIVE_TICKET"; then
    TICKET_FILE="$EFFECTIVE_TICKET"
  else
    fail "effective ticket state could not be resolved"
    echo "PREFLIGHT FAIL"
    exit 1
  fi
fi
if [[ -n "${FACTORY_TICKET_KIT_SHA:-}" ]]; then
  pass "ticket Kit-SHA affinity matches selected kit SHA"
fi
if ! factory_dispatch_require_lease "$REPO_ROOT" "$TICKET" "$LEASE_ID"; then
  fail "$FACTORY_DISPATCH_LEASE_ERROR"
  echo "PREFLIGHT FAIL"
  exit 1
fi

if [[ "${FACTORY_RELEASE_CONTRACT_VERSION:-}" == "1.8.0" ||
      "${FACTORY_RELEASE_CONTRACT_VERSION:-}" == "2.0.0" ]]; then
  if [[ -z "$WORKDIR" ]] ||
     ! python3 -B "$KIT_DIR/scripts/ticket-readiness.py" \
       --ticket "$TICKET" --workdir "$CONTENT_ROOT"; then
    fail "provider-free ticket readiness contract is not executable"
    echo "PREFLIGHT FAIL"
    exit 1
  fi
  pass "provider-free ticket readiness contract passed"
fi

# Product and machine configuration are data-only trust boundaries. Validate
# both before any backend probe can touch a credential-bearing CLI.
[[ -f "$ENV_FILE" ]] || {
  fail "envelope not found: $ENV_FILE"
  echo "PREFLIGHT FAIL"
  exit 1
}
unset PER_RUN_BUDGET_USD PER_TICKET_BUDGET_USD PER_RUN_MAX_TURNS \
  PER_RUN_TIMEOUT_MIN DAILY_CAP_USD
if ! factory_load_plain_config "$ENV_FILE" envelope \
  "$FACTORY_ENVELOPE_CONFIG_KEYS" "$FACTORY_ENVELOPE_REQUIRED_KEYS"; then
  fail "envelope config is unsafe or malformed"
  echo "PREFLIGHT FAIL"
  exit 1
fi
if [[ -n "$ROLE" ]] && ! factory_select_role_envelope "$ROLE"; then
  fail "requested role envelope is invalid"
  echo "PREFLIGHT FAIL"
  exit 1
fi

# --- optional machine-level cap (same anchor as run-agent.sh) ---
GLOBAL_ENV="${FACTORY_GLOBAL_ENV:-$HOME/.factory/global.env}"
if ! factory_validate_runtime_overrides; then
  fail "$FACTORY_RUNTIME_OVERRIDE_ERROR"
  echo "PREFLIGHT FAIL"
  exit 1
fi
factory_clear_plain_config_keys "$FACTORY_GLOBAL_CONFIG_KEYS"
GLOBAL_LEDGER=""
if [[ -f "$GLOBAL_ENV" ]]; then
  if ! factory_load_plain_config "$GLOBAL_ENV" global \
    "$FACTORY_GLOBAL_CONFIG_KEYS" "" 1; then
    fail "global config is unsafe or malformed"
    echo "PREFLIGHT FAIL"
    exit 1
  fi
  GLOBAL_LEDGER="${GLOBAL_LEDGER:-$(dirname "$GLOBAL_ENV")/global-ledger.csv}"
fi
if ! factory_validate_runtime_overrides; then
  fail "$FACTORY_RUNTIME_OVERRIDE_ERROR"
  echo "PREFLIGHT FAIL"
  exit 1
fi
if [[ -n "$ROLE" ]]; then
  EFFECTIVE_ENVELOPE="$(python3 -B "$KIT_DIR/scripts/envelope-control.py" effective \
    --factory-root "$REPO_ROOT" --ticket "$TICKET" --role "$ROLE" \
    --day "$(date -u +%F)" --global-env "$GLOBAL_ENV" --format shell)" || {
      fail "effective role envelope or override records are unsafe"
      echo "PREFLIGHT FAIL"
      exit 1
    }
  while IFS='=' read -r ENVELOPE_KEY ENVELOPE_VALUE; do
    case "$ENVELOPE_KEY" in
      PER_RUN_BUDGET_USD|PER_TICKET_BUDGET_USD|PER_RUN_MAX_TURNS|PER_RUN_TIMEOUT_MIN|DAILY_CAP_USD|GLOBAL_DAILY_CAP_USD|FACTORY_ENVELOPE_OVERRIDE_IDS|FACTORY_ENVELOPE_NEXT_OVERRIDE_IDS)
        printf -v "$ENVELOPE_KEY" '%s' "$ENVELOPE_VALUE"
        ;;
      *)
        fail "effective role envelope returned an unsupported value"
        echo "PREFLIGHT FAIL"
        exit 1
        ;;
    esac
  done <<<"$EFFECTIVE_ENVELOPE"
  pass "$ROLE attempt envelope: budget \$$PER_RUN_BUDGET_USD, max turns $PER_RUN_MAX_TURNS, timeout ${PER_RUN_TIMEOUT_MIN}m"
fi
# (a) backend routes — validate the pinned contract without repeating the
# controller's machine-readiness probes. The role runner re-probes its one
# selected route immediately before provider admission.
# shellcheck disable=SC1091
source "$KIT_DIR/scripts/lib/backend-policy.sh"
ROUTE_PLAN="$CONTENT_ROOT/factory/route-plans/$TICKET.json"
if [[ "${FACTORY_TEST_MODE:-0}" == "1" &&
      "${FACTORY_TRUSTED_TEST_HARNESS:-0}" == "1" &&
      "${FACTORY_ADAPTER_OVERRIDE:-}" == "mock" ]]; then
  if [[ -x "$KIT_DIR/scripts/adapters/mock.sh" ]] &&
     command -v timeout >/dev/null 2>&1 &&
     command -v python3 >/dev/null 2>&1; then
    pass "authenticated isolated mock route contract passed"
    pass "reasoning route fixed to mock by the trusted test harness"
    pass "execution route fixed to mock by the trusted test harness"
  else
    fail "isolated mock route is missing required runtime tools"
  fi
elif [[ -f "$ROUTE_PLAN" ]]; then
  for ROLE_SAMPLE in planner builder narrator spec-linter test-author reviewer; do
    if ! factory_select_pinned_model_role \
        "$ROUTE_PLAN" "$TICKET" "$FACTORY_KIT_SHA" "$ROLE_SAMPLE"; then
      fail "$ROLE_SAMPLE pinned route is invalid: ${FACTORY_RESOLVE_ERROR:-unknown}"
      continue
    fi
    pass "$ROLE_SAMPLE pinned route contract passed ($FACTORY_SELECTED_ROUTE_ID)"
  done
else
  warn "legacy routing is unpinned for $TICKET"
  if CONTRACT_OUT="$(FACTORY_GLOBAL_ENV="$GLOBAL_ENV" "$KIT_DIR/scripts/adapters/contract-test.sh" --routes 2>&1)"; then
    pass "backend route contract test passed"
  else
    fail "backend route contract test failed — output follows"
    printf '%s\n' "$CONTRACT_OUT" | sed 's/^/  | /'
  fi

  # Report both role-group routes. This is kickoff visibility only;
  # run-agent.sh resolves again immediately before every role launch.
  for ROLE_SAMPLE in planner spec-linter; do
    GROUP="$(factory_role_group "$ROLE_SAMPLE")"
    PRIMARY="$(factory_group_primary "$GROUP")"
    FALLBACK="$(factory_group_fallback "$GROUP")"
    factory_probe_adapter "$PRIMARY"
    PRIMARY_STATE="$PROBE_STATE"
    PRIMARY_REASON="$PROBE_REASON"
    if [[ "$PRIMARY_STATE" == "READY" ]]; then
      pass "$GROUP primary route ready ($PRIMARY)"
      if [[ "${FACTORY_CURSOR_FALLBACK_ENABLED:-0}" == "1" ]]; then
        factory_probe_adapter "$FALLBACK"
        if [[ "$PROBE_STATE" == "READY" ]]; then
          pass "$GROUP Cursor fallback ready ($FALLBACK)"
        else
          warn "$GROUP Cursor fallback not ready: $PROBE_STATE ($PROBE_REASON)"
        fi
      else
        pass "$GROUP Cursor fallback disabled (primary-only compatibility mode)"
      fi
    else
      factory_probe_adapter "$FALLBACK"
      if [[ "$PRIMARY_STATE" == "UNAVAILABLE" && "$PROBE_STATE" == "READY" ]]; then
        warn "$GROUP primary unavailable ($PRIMARY_REASON); pre-execution route selects $FALLBACK"
      else
        fail "$GROUP has no safe route: primary=$PRIMARY_STATE/$PRIMARY_REASON fallback=$PROBE_STATE/$PROBE_REASON"
      fi
    fi
  done
fi

# (b) daily budget — same spend computation as run-agent.sh, reserve = PROJECTED_TICKET_USD
TODAY="$(date -u +%F)"
LEDGER_READY=1
if [[ -L "$FACTORY_DIR/runs" ]] ||
   { [[ ! -d "$FACTORY_DIR/runs" ]] &&
     ! python3 "$KIT_DIR/scripts/lib/durable-file.py" touch \
       "$FACTORY_DIR/runs/.initialized"; }; then
  fail "run manifest directory could not be durably established"
  LEDGER_READY=0
fi
if [[ -z "${FACTORY_LEDGER:-}" ]] &&
   [[ "$LEDGER_READY" -eq 1 ]] &&
   ! python3 "$KIT_DIR/scripts/ledger-view.py" refresh --factory-root "$REPO_ROOT" >/dev/null; then
  fail "effective ledger could not be reduced"
  LEDGER_READY=0
fi
if [[ "$LEDGER_READY" -eq 1 ]]; then
  SPENT_TODAY="$(python3 "$MONEY" sum-csv --csv "$LEDGER" --date "$TODAY" \
    --date-column 0 --amount-column 7)"
else
  SPENT_TODAY="0.0000"
fi
if python3 "$MONEY" exceeds --spent "$SPENT_TODAY" \
    --reserve "$PROJECTED_TICKET_USD" --cap "$DAILY_CAP_USD"; then
  fail "repo daily cap insufficient (spent \$$SPENT_TODAY + reserve \$$PROJECTED_TICKET_USD > \$$DAILY_CAP_USD)"
else
  pass "repo daily budget covers projected ticket (\$$SPENT_TODAY spent + \$$PROJECTED_TICKET_USD reserve <= \$$DAILY_CAP_USD)"
fi
if [[ -n "$GLOBAL_LEDGER" && -n "${GLOBAL_DAILY_CAP_USD:-}" ]]; then
  mkdir -p "$(dirname "$GLOBAL_LEDGER")"
  [[ -f "$GLOBAL_LEDGER" ]] || echo "date,time,repo,ticket,role,adapter,prompt_version,turns,cost_usd,exit_status,run_id,provider_family,model_id,selection_reason,cost_basis,adapter_version" > "$GLOBAL_LEDGER"
  SPENT_GLOBAL="$(python3 "$MONEY" sum-csv --csv "$GLOBAL_LEDGER" \
    --date "$TODAY" --date-column 0 --amount-column 8)"
  if python3 "$MONEY" exceeds --spent "$SPENT_GLOBAL" \
      --reserve "$PROJECTED_TICKET_USD" --cap "$GLOBAL_DAILY_CAP_USD"; then
    fail "machine daily cap insufficient (spent \$$SPENT_GLOBAL + reserve \$$PROJECTED_TICKET_USD > \$$GLOBAL_DAILY_CAP_USD)"
  else
    pass "machine daily budget covers projected ticket (\$$SPENT_GLOBAL spent + \$$PROJECTED_TICKET_USD reserve <= \$$GLOBAL_DAILY_CAP_USD)"
  fi
else
  pass "no machine-level daily cap configured"
fi

# (d) production uses a clean, current main checkout. A sealed qualification
# launcher instead supplies the already-validated product tree bound by its
# owner-only active record; qualification control commits intentionally do not
# equal origin/main.
if ! git -C "$REPO_ROOT" rev-parse --git-dir >/dev/null 2>&1; then
  fail "not a git repository: $REPO_ROOT"
elif [[ -n "${FACTORY_QUALIFICATION_PRODUCT_TREE:-}" ]]; then
  if [[ ! "$FACTORY_QUALIFICATION_PRODUCT_TREE" =~ ^[0-9a-f]{40}$ ]]; then
    fail "sealed qualification product tree is invalid"
  elif [[ -n "$(git -C "$REPO_ROOT" status --porcelain)" ]]; then
    fail "working tree not clean"
  elif [[ "$(git -C "$REPO_ROOT" rev-parse HEAD^{tree})" != "$FACTORY_QUALIFICATION_PRODUCT_TREE" ]]; then
    fail "product tree does not match sealed qualification environment"
  else
    pass "repo is clean and matches sealed qualification product tree"
  fi
else
  git -C "$REPO_ROOT" fetch origin >/dev/null 2>&1 || fail "git fetch origin failed"
  BRANCH="$(git -C "$REPO_ROOT" rev-parse --abbrev-ref HEAD)"
  if [[ "$BRANCH" != "main" ]]; then
    fail "not on main branch (on $BRANCH)"
  elif [[ -n "$(git -C "$REPO_ROOT" status --porcelain)" ]]; then
    fail "working tree not clean"
  else
    LOCAL="$(git -C "$REPO_ROOT" rev-parse HEAD)"
    if git -C "$REPO_ROOT" rev-parse origin/main >/dev/null 2>&1; then
      REMOTE="$(git -C "$REPO_ROOT" rev-parse origin/main)"
      if [[ "$LOCAL" != "$REMOTE" ]]; then
        fail "not up to date with origin/main (local ${LOCAL:0:7} != origin ${REMOTE:0:7})"
      else
        pass "repo on main, clean, up to date with origin/main"
      fi
    else
      fail "origin/main not found after fetch"
    fi
  fi
fi

# (e) ticket is in the exact kickoff state, or the verified transition receipt
# authorizes an earlier repair owner beneath the visible coarse state. The
# installed launcher constructs FACTORY_VERIFIED_TRANSITION_STAGE only from
# state-machine receipt verification inside its empty helper environment.
case "${FACTORY_RELEASE_CONTRACT_VERSION:-}" in
  1.8.0|2.0.0) CONTRACT_HAS_STATE_MACHINE=1 ;;
  *) CONTRACT_HAS_STATE_MACHINE=0 ;;
esac
EXPECTED_STATE="Ready"
if [[ "$CONTRACT_HAS_STATE_MACHINE" -eq 1 &&
      "$ROLE" == "planner" ]]; then
  EXPECTED_STATE="Planning"
fi
STATE_ACCEPTED=0
if [[ ! -f "$TICKET_FILE" ]]; then
  fail "ticket file missing: $TICKET_FILE"
elif grep -qE "^State: $EXPECTED_STATE$" "$TICKET_FILE"; then
  pass "ticket $TICKET is $EXPECTED_STATE"
  STATE_ACCEPTED=1
elif [[ "$CONTRACT_HAS_STATE_MACHINE" -eq 1 &&
        "$ROLE" == "planner" &&
        "${FACTORY_VERIFIED_TRANSITION_STAGE:-}" == "FIX planner" ]]; then
  STATE="$(grep -m1 '^State:' "$TICKET_FILE" 2>/dev/null || echo 'State: unknown')"
  pass "ticket $TICKET $STATE is authorized by the verified FIX planner transition"
  STATE_ACCEPTED=1
elif [[ "$CONTRACT_HAS_STATE_MACHINE" -eq 1 &&
        "$ROLE" == "planner" &&
        "${FACTORY_VERIFIED_TRANSITION_STAGE:-}" == "CATCHUP planner" ]] &&
     grep -qE '^State: (Building|Review)$' "$TICKET_FILE"; then
  STATE="$(grep -m1 '^State:' "$TICKET_FILE")"
  pass "ticket $TICKET $STATE is authorized by the verified Planner catch-up"
  STATE_ACCEPTED=1
else
  STATE="$(grep -m1 '^State:' "$TICKET_FILE" 2>/dev/null || echo 'State: unknown')"
  fail "ticket not $EXPECTED_STATE ($STATE)"
fi
if [[ "$STATE_ACCEPTED" -eq 1 ]]; then
  INITIATIVE="$(sed -n 's/^Initiative:[[:space:]]*//p' "$TICKET_FILE" | head -n1)"
  if [[ -z "$INITIATIVE" ]]; then
    fail "ticket has no Initiative field"
  elif [[ ! -f "$CONTENT_ROOT/factory/initiatives/$INITIATIVE.md" ]]; then
    fail "ticket initiative not found: $CONTENT_ROOT/factory/initiatives/$INITIATIVE.md"
  else
    pass "ticket belongs to initiative $INITIATIVE"
  fi
fi

if [[ $FAIL -eq 0 ]]; then
  echo "PREFLIGHT PASS"
  exit 0
else
  echo "PREFLIGHT FAIL"
  exit 1
fi
