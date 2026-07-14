#!/usr/bin/env bash
# preflight.sh — kickoff checks before a ticket's first launch.
# The dispatcher runs this once per ticket before the first run-agent.sh call.
# Usage: preflight.sh --ticket T-NNN
# FACTORY_ROOT semantics match run-agent.sh (anchors factory/ under the repo root).
set -euo pipefail

TICKET=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --ticket) TICKET="$2"; shift 2;;
    *) echo "unknown arg: $1" >&2; exit 2;;
  esac
done
[[ -n "$TICKET" ]] || { echo "usage: preflight.sh --ticket T-NNN" >&2; exit 2; }
[[ "$TICKET" =~ ^T-[0-9]+$ ]] || { echo "invalid ticket identifier" >&2; exit 2; }

KIT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
REPO_ROOT="${FACTORY_ROOT:-$(git rev-parse --show-toplevel 2>/dev/null || echo "$PWD")}"
FACTORY_DIR="$REPO_ROOT/factory"
LEDGER="${FACTORY_LEDGER:-$FACTORY_DIR/ledger.csv}"
ENV_FILE="${FACTORY_ENVELOPE:-$FACTORY_DIR/ENVELOPE.env}"
PROJECTED_TICKET_USD="${PROJECTED_TICKET_USD:-5.00}"
TICKET_FILE="$FACTORY_DIR/tickets/$TICKET.md"

FAIL=0
pass() { echo "PASS: $*"; }
fail() { echo "FAIL: $*"; FAIL=1; }
warn() { echo "WARN: $*"; }

# Runtime barriers, physical release validation, and existing ticket affinity
# are hard gates. None may fall through to backend probes.
# shellcheck disable=SC1091
source "$KIT_DIR/scripts/lib/kit-pin.sh"
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
if [[ -n "${FACTORY_TICKET_KIT_SHA:-}" ]]; then
  pass "ticket Kit-SHA affinity matches selected kit SHA"
fi

# --- optional machine-level cap (same anchor as run-agent.sh) ---
GLOBAL_ENV="${FACTORY_GLOBAL_ENV:-$HOME/.factory/global.env}"
GLOBAL_LEDGER=""
if [[ -f "$GLOBAL_ENV" ]]; then
  # shellcheck disable=SC1090
  source "$GLOBAL_ENV"
  GLOBAL_LEDGER="${GLOBAL_LEDGER:-$(dirname "$GLOBAL_ENV")/global-ledger.csv}"
fi
if ! factory_validate_runtime_overrides; then
  fail "$FACTORY_RUNTIME_OVERRIDE_ERROR"
  echo "PREFLIGHT FAIL"
  exit 1
fi

# (a) backend routes — resolve without submitting any task.
# shellcheck disable=SC1091
source "$KIT_DIR/scripts/lib/backend-policy.sh"
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

# (b) daily budget — same spend computation as run-agent.sh, reserve = PROJECTED_TICKET_USD
if [[ ! -f "$ENV_FILE" ]]; then
  fail "envelope not found: $ENV_FILE"
else
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  TODAY="$(date +%F)"
  [[ -f "$LEDGER" ]] || echo "date,time,ticket,role,adapter,prompt_version,turns,cost_usd,exit_status,run_id,provider_family,model_id,selection_reason,cost_basis,adapter_version" > "$LEDGER"
  SPENT_TODAY="$(awk -F, -v d="$TODAY" 'NR>1 && $1==d {s+=$8} END {printf "%.4f", s+0}' "$LEDGER")"
  if awk -v s="$SPENT_TODAY" -v r="$PROJECTED_TICKET_USD" -v cap="$DAILY_CAP_USD" 'BEGIN{exit !((s+r)>cap)}'; then
    fail "repo daily cap insufficient (spent \$$SPENT_TODAY + reserve \$$PROJECTED_TICKET_USD > \$$DAILY_CAP_USD)"
  else
    pass "repo daily budget covers projected ticket (\$$SPENT_TODAY spent + \$$PROJECTED_TICKET_USD reserve <= \$$DAILY_CAP_USD)"
  fi
  if [[ -n "$GLOBAL_LEDGER" && -n "${GLOBAL_DAILY_CAP_USD:-}" ]]; then
    mkdir -p "$(dirname "$GLOBAL_LEDGER")"
    [[ -f "$GLOBAL_LEDGER" ]] || echo "date,time,repo,ticket,role,adapter,prompt_version,turns,cost_usd,exit_status,run_id,provider_family,model_id,selection_reason,cost_basis,adapter_version" > "$GLOBAL_LEDGER"
    SPENT_GLOBAL="$(awk -F, -v d="$TODAY" 'NR>1 && $1==d {s+=$9} END {printf "%.4f", s+0}' "$GLOBAL_LEDGER")"
    if awk -v s="$SPENT_GLOBAL" -v r="$PROJECTED_TICKET_USD" -v cap="$GLOBAL_DAILY_CAP_USD" 'BEGIN{exit !((s+r)>cap)}'; then
      fail "machine daily cap insufficient (spent \$$SPENT_GLOBAL + reserve \$$PROJECTED_TICKET_USD > \$$GLOBAL_DAILY_CAP_USD)"
    else
      pass "machine daily budget covers projected ticket (\$$SPENT_GLOBAL spent + \$$PROJECTED_TICKET_USD reserve <= \$$GLOBAL_DAILY_CAP_USD)"
    fi
  else
    pass "no machine-level daily cap configured"
  fi
fi

# (d) repo clone on main, clean, up to date with origin/main
if ! git -C "$REPO_ROOT" rev-parse --git-dir >/dev/null 2>&1; then
  fail "not a git repository: $REPO_ROOT"
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

# (e) ticket exists, is reconciled Ready, and belongs to a known initiative
if [[ ! -f "$TICKET_FILE" ]]; then
  fail "ticket file missing: $TICKET_FILE"
elif grep -qE '^State: Ready' "$TICKET_FILE"; then
  pass "ticket $TICKET is Ready"
  INITIATIVE="$(sed -n 's/^Initiative:[[:space:]]*//p' "$TICKET_FILE" | head -n1)"
  if [[ -z "$INITIATIVE" ]]; then
    fail "ticket has no Initiative field"
  elif [[ ! -f "$FACTORY_DIR/initiatives/$INITIATIVE.md" ]]; then
    fail "ticket initiative not found: $FACTORY_DIR/initiatives/$INITIATIVE.md"
  else
    pass "ticket belongs to initiative $INITIATIVE"
  fi
else
  STATE="$(grep -m1 '^State:' "$TICKET_FILE" 2>/dev/null || echo 'State: unknown')"
  fail "ticket not Ready ($STATE)"
fi

LINEAR_MAP="$FACTORY_DIR/linear-map.json"
if [[ -f "$LINEAR_MAP" ]] && grep -q '"last_success_at":[[:space:]]*"[^"]' "$LINEAR_MAP"; then
  pass "Linear reconciliation has a recorded successful pull"
else
  warn "no successful Linear reconciliation recorded — verify board sync before trusting a new operator action"
fi

# (f) GH_TOKEN — warn only
if [[ -n "${GH_TOKEN:-}" ]]; then
  pass "GH_TOKEN available (environment)"
elif [[ -f "$HOME/.hermes/profiles/factory/.env" ]] && grep -qE '^GH_TOKEN=' "$HOME/.hermes/profiles/factory/.env" 2>/dev/null; then
  pass "GH_TOKEN available (~/.hermes/profiles/factory/.env)"
else
  warn "GH_TOKEN not set — PR and CI status checks may fail"
fi

if [[ $FAIL -eq 0 ]]; then
  echo "PREFLIGHT PASS"
  exit 0
else
  echo "PREFLIGHT FAIL"
  exit 1
fi
