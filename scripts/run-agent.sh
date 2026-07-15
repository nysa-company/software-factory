#!/usr/bin/env bash
# run-agent.sh — the only sanctioned way to start a factory agent run.
# Enforces per-run, per-ticket, and daily budgets; serializes cap checks with a
# lock; anchors run artifacts to the caller's product root while routing the
# ledger to the main working tree; logs every run to the cost ledger; enforces
# the cross-family role→adapter mapping; rejects overlapping ticket+role runs.
#
# Usage:
#   run-agent.sh --role builder --ticket T-123 --prompt-file factory/roles/builder.md \
#                [--adapter claude-code] [--workdir /path/to/worktree] -- "task text"
#
# Envelope (factory/ENVELOPE.env at the repo root):
#   PER_RUN_BUDGET_USD, PER_TICKET_BUDGET_USD, PER_RUN_MAX_TURNS,
#   PER_RUN_TIMEOUT_MIN, DAILY_CAP_USD
set -euo pipefail

ROLE="" TICKET="" PROMPT_FILE="" ADAPTER="" WORKDIR="$PWD"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --role) ROLE="$2"; shift 2;;
    --ticket) TICKET="$2"; shift 2;;
    --prompt-file) PROMPT_FILE="$2"; shift 2;;
    --adapter) ADAPTER="$2"; shift 2;;
    --workdir) WORKDIR="$2"; shift 2;;
    --) shift; break;;
    *) echo "unknown arg: $1" >&2; exit 2;;
  esac
done
TASK="${*:-}"
[[ -n "$ROLE" && -n "$TICKET" && -n "$TASK" ]] || { echo "missing required args" >&2; exit 2; }
[[ "$TICKET" =~ ^T-[0-9]+$ ]] || { echo "invalid ticket identifier" >&2; exit 2; }

KIT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
# shellcheck disable=SC1091
source "$KIT_DIR/scripts/lib/kit-pin.sh"

# --- anchor factory state to the repo root, never to $PWD ---
REPO_ROOT="${FACTORY_ROOT:-$(git rev-parse --show-toplevel 2>/dev/null || echo "$PWD")}"
FACTORY_DIR="$REPO_ROOT/factory"

# A linked worktree has its own copy of factory/, but costs must have one
# source of truth. Map the caller's FACTORY_ROOT to the same relative path
# under the main working tree. An explicit FACTORY_LEDGER always wins.
canonical_ledger() {
  local root="$1" root_abs worktree_root common_dir main_root relative
  root_abs="$(cd "$root" 2>/dev/null && pwd -P || printf '%s' "$root")"
  if worktree_root="$(git -C "$root" rev-parse --show-toplevel 2>/dev/null)" &&
     common_dir="$(git -C "$root" rev-parse --git-common-dir 2>/dev/null)"; then
    worktree_root="$(cd "$worktree_root" && pwd -P)"
    # A relative --git-common-dir is relative to git's cwd ($root), NOT the
    # worktree root — resolving against the wrong base broke main-clone
    # subdirectory roots (e.g. FACTORY_ROOT=<repo>/conformance).
    case "$common_dir" in
      /*) ;;
      *) common_dir="$root_abs/$common_dir" ;;
    esac
    if ! main_root="$(cd "$common_dir/.." 2>/dev/null && pwd -P)"; then
      printf '%s/factory/ledger.csv\n' "$root_abs"
      return
    fi
    if [[ "$root_abs" == "$worktree_root" ]]; then
      relative=""
    elif [[ "$root_abs" == "$worktree_root/"* ]]; then
      relative="${root_abs#"$worktree_root/"}"
    else
      printf '%s/factory/ledger.csv\n' "$root_abs"
      return
    fi
    printf '%s%s/factory/ledger.csv\n' "$main_root" "${relative:+/$relative}"
  else
    printf '%s/factory/ledger.csv\n' "$root_abs"
  fi
}

LEDGER="${FACTORY_LEDGER:-$(canonical_ledger "$REPO_ROOT")}"
ENV_FILE="${FACTORY_ENVELOPE:-$FACTORY_DIR/ENVELOPE.env}"
LEDGER_DIR="$(dirname "$LEDGER")"
LOCK_DIR="$LEDGER_DIR/.ledger.lock"
LAUNCH_LOCK="$FACTORY_DIR/.launch.lock"
RUNS_DIR="$FACTORY_DIR/runs"
ACTIVE_RUN_FILE=""
ACTIVE_RUN_TEMP=""
OWNS_ACTIVE_RUN=0
HELD_LEDGER_LOCK=0
HELD_GLOBAL_LOCK=0
HELD_LAUNCH_LOCK=0
RUN_PID=""
RUN_PGID=""
RUN_GROUP_ACTIVE=0
RUN_GROUP_TERMINATED=1
RUN_PID_FILE=""
RUN_READY_FILE=""
RUN_GO_FILE=""
RUN_START_ID=""
MANIFEST=""
MANIFEST_PHASE=""
SEQUENCER="$KIT_DIR/scripts/next-stage.sh"
SEQUENCER_ERROR=""

sequencer_allows_role() {
  local output rc=0
  SEQUENCER_ERROR=""
  if [[ ! -f "$SEQUENCER" || -L "$SEQUENCER" ]]; then
    SEQUENCER_ERROR="selected release sequencer is missing or unsafe"
    return 1
  fi
  output="$(FACTORY_ROOT="$REPO_ROOT" FACTORY_LEDGER="$LEDGER" \
    bash "$SEQUENCER" --ticket "$TICKET" 2>/dev/null)" || rc=$?
  if [[ "$rc" -ne 0 ]]; then
    SEQUENCER_ERROR="sequencer refused the ticket state"
    return 1
  fi
  if [[ "$output" == "RUN $ROLE" ]]; then
    return 0
  fi
  if [[ "$output" == "FIX builder-or-test-author" &&
        ( "$ROLE" == "builder" || "$ROLE" == "test-author" ) ]]; then
    return 0
  fi
  SEQUENCER_ERROR="sequencer did not authorize the requested role"
  return 1
}

meta_value() {
  printf '%s' "${1:-}" | tr '\n,' '__'
}

write_manifest() {
  local phase="$1" tmp
  [[ -n "$MANIFEST" ]] || return 0
  tmp="$MANIFEST.tmp.$$"
  {
    echo "run_id=$(meta_value "${RUN_ID:-}")"
    echo "phase=$(meta_value "$phase")"
    echo "ticket=$(meta_value "$TICKET")"
    echo "role=$(meta_value "$ROLE")"
    echo "adapter=$(meta_value "${ADAPTER:-}")"
    echo "provider_family=$(meta_value "${SELECTED_FAMILY:-}")"
    echo "model_id=$(meta_value "${SELECTED_MODEL:-}")"
    echo "effort=$(meta_value "${SELECTED_EFFORT:-}")"
    echo "selection_reason=$(meta_value "${SELECTION_REASON:-}")"
    echo "adapter_version=$(meta_value "${SELECTED_VERSION:-}")"
    echo "primary_probe=$(meta_value "${PRIMARY_PROBE_SUMMARY:-}")"
    echo "kit_sha=$(meta_value "${FACTORY_KIT_SHA:-}")"
    echo "kit_tree=$(meta_value "${FACTORY_KIT_TREE:-}")"
    echo "product_tree=$(meta_value "${FACTORY_PRODUCT_TREE:-}")"
    echo "ticket_kit_sha=$(meta_value "${FACTORY_TICKET_KIT_SHA:-}")"
    echo "contract_version=$(meta_value "${FACTORY_CONTRACT_VERSION:-}")"
    echo "physical_kit_path=$(meta_value "${FACTORY_KIT_PATH:-}")"
    echo "kit_provenance_mode=$(meta_value "${FACTORY_KIT_PROVENANCE_MODE:-}")"
    echo "pid=$(meta_value "${RUN_PID:-}")"
    echo "pgid=$(meta_value "${RUN_PGID:-}")"
    echo "process_start=$(meta_value "${RUN_START_ID:-}")"
    echo "updated_at=$(date -u +%FT%TZ)"
  } > "$tmp"
  mv "$tmp" "$MANIFEST"
  MANIFEST_PHASE="$phase"
}

terminate_run_group() {
  [[ "$RUN_GROUP_ACTIVE" -eq 1 && "$RUN_PGID" =~ ^[0-9]+$ ]] || return 0
  if ! kill -0 -- "-$RUN_PGID" 2>/dev/null; then
    RUN_GROUP_ACTIVE=0
    RUN_GROUP_TERMINATED=1
    return 0
  fi
  kill -TERM -- "-$RUN_PGID" 2>/dev/null || true
  sleep 1
  kill -KILL -- "-$RUN_PGID" 2>/dev/null || true
  RUN_GROUP_ACTIVE=0
  sleep 0.1
  if kill -0 -- "-$RUN_PGID" 2>/dev/null; then
    RUN_GROUP_TERMINATED=0
    return 1
  fi
  RUN_GROUP_TERMINATED=1
}

cleanup() {
  terminate_run_group || true
  if [[ -n "$RUN_PID_FILE" ]]; then
    if [[ "$RUN_GROUP_TERMINATED" -eq 1 ]]; then
      rm -f "$RUN_PID_FILE"
    else
      echo "WARNING: retaining $RUN_PID_FILE because the process group survived cleanup" >&2
    fi
  fi
  [[ -z "$RUN_READY_FILE" ]] || rm -f "$RUN_READY_FILE"
  [[ -z "$RUN_GO_FILE" ]] || rm -f "$RUN_GO_FILE"
  if [[ -n "$MANIFEST" && "$MANIFEST_PHASE" != "completed" && "$MANIFEST_PHASE" != "abandoned" ]]; then
    write_manifest "abandoned"
  fi
  [[ "$HELD_GLOBAL_LOCK" -eq 0 ]] || rmdir "$GLOBAL_LOCK" 2>/dev/null || true
  [[ "$HELD_LEDGER_LOCK" -eq 0 ]] || rmdir "$LOCK_DIR" 2>/dev/null || true
  [[ "$HELD_LAUNCH_LOCK" -eq 0 ]] || rmdir "$LAUNCH_LOCK" 2>/dev/null || true
  [[ -z "$ACTIVE_RUN_TEMP" ]] || rm -f "$ACTIVE_RUN_TEMP"
  if [[ "$OWNS_ACTIVE_RUN" -eq 1 ]]; then
    rm -f "$ACTIVE_RUN_FILE"
  fi
}
trap cleanup EXIT
trap 'exit 143' TERM INT HUP

TICKET_FILE="$FACTORY_DIR/tickets/$TICKET.md"
[[ -f "$ENV_FILE" ]] || { echo "envelope not found: $ENV_FILE — fill ENVELOPE.md and write ENVELOPE.env first" >&2; exit 3; }
# shellcheck disable=SC1090
source "$ENV_FILE"
PER_TICKET_BUDGET_USD="${PER_TICKET_BUDGET_USD:-$PER_RUN_BUDGET_USD}"

# --- optional machine-level cap across all factories on this machine ---
# ~/.factory/global.env defines GLOBAL_DAILY_CAP_USD; every run on the machine
# then also reserves against ~/.factory/global-ledger.csv, so N projects can't
# multiply the daily budget silently. Absent file = single-project behavior.
GLOBAL_ENV="${FACTORY_GLOBAL_ENV:-$HOME/.factory/global.env}"
GLOBAL_LEDGER="" GLOBAL_LOCK=""
if [[ -f "$GLOBAL_ENV" ]]; then
  # shellcheck disable=SC1090
  source "$GLOBAL_ENV"
  GLOBAL_LEDGER="${GLOBAL_LEDGER:-$(dirname "$GLOBAL_ENV")/global-ledger.csv}"
  GLOBAL_LOCK="$(dirname "$GLOBAL_ENV")/.ledger.lock"
  [[ -n "${GLOBAL_DAILY_CAP_USD:-}" ]] || { echo "global env $GLOBAL_ENV exists but GLOBAL_DAILY_CAP_USD is unset" >&2; exit 3; }
fi
if ! factory_validate_runtime_overrides; then
  echo "$FACTORY_RUNTIME_OVERRIDE_ERROR; no task was submitted" >&2
  exit 2
fi

# --- kill switch check (anchored) ---
if [[ -f "$FACTORY_DIR/KILL" ]]; then
  echo "KILL file present ($FACTORY_DIR/KILL) — factory is stopped. Remove it to resume." >&2
  exit 4
fi
if [[ -f "$FACTORY_DIR/MAINTENANCE" ]]; then
  echo "MAINTENANCE file present ($FACTORY_DIR/MAINTENANCE) — factory control plane is paused; no task was submitted." >&2
  exit 4
fi
if ! factory_validate_kit_pin "$KIT_DIR" "$REPO_ROOT"; then
  echo "$FACTORY_KIT_PIN_ERROR; no task was submitted" >&2
  exit 3
fi
if ! factory_validate_ticket_kit_sha "$TICKET_FILE" "$FACTORY_KIT_SHA"; then
  echo "$FACTORY_TICKET_KIT_ERROR; no task was submitted" >&2
  exit 3
fi
# The first manifest phase still records the release affinity that will be
# persisted under the launch lock. factory_record_ticket_kit_sha revalidates
# and writes it before any reservation or task submission.
if [[ -z "${FACTORY_TICKET_KIT_SHA:-}" ]]; then
  FACTORY_TICKET_KIT_SHA="$FACTORY_KIT_SHA"
fi
if ! sequencer_allows_role; then
  echo "$SEQUENCER_ERROR; no task was submitted" >&2
  exit 10
fi

# --- resolve one backend before reservation and before submitting the task ---
# shellcheck disable=SC1091
source "$KIT_DIR/scripts/lib/backend-policy.sh"
if [[ -n "${FACTORY_ADAPTER_OVERRIDE:-}" ]]; then
  if [[ "$FACTORY_ADAPTER_OVERRIDE" != "mock" || "${FACTORY_TEST_MODE:-0}" != "1" ]]; then
    echo "FACTORY_ADAPTER_OVERRIDE requires FACTORY_TEST_MODE=1 and the mock adapter" >&2
    exit 2
  fi
  SELECTED="$FACTORY_ADAPTER_OVERRIDE"
  SELECTED_FAMILY="$(factory_adapter_family "$SELECTED" 2>/dev/null || echo test)"
  SELECTED_MODEL="${FACTORY_OVERRIDE_MODEL:-}"
  SELECTED_VERSION="test"
  SELECTION_REASON="test_override"
  PRIMARY_PROBE_SUMMARY="test_override"
elif factory_resolve_role "$ROLE"; then
  SELECTED="$FACTORY_SELECTED_ADAPTER"
  SELECTED_FAMILY="$FACTORY_SELECTED_FAMILY"
  SELECTED_MODEL="${FACTORY_SELECTED_MODEL:-cli-default}"
  [[ -n "$SELECTED_MODEL" ]] || SELECTED_MODEL="cli-default"
  SELECTED_EFFORT="${FACTORY_SELECTED_EFFORT:-}"
  SELECTED_VERSION="$FACTORY_SELECTED_VERSION"
  SELECTION_REASON="$FACTORY_SELECTION_REASON"
  PRIMARY_PROBE_SUMMARY="${FACTORY_PRIMARY_STATE}:${FACTORY_PRIMARY_REASON}"
else
  echo "no safe backend route for role '$ROLE': ${FACTORY_RESOLVE_ERROR:-unknown}; no task was submitted" >&2
  exit 6
fi

if [[ -n "$ADAPTER" && "$ADAPTER" != "$SELECTED" ]]; then
  echo "role '$ROLE' resolved to adapter '$SELECTED'; explicit adapter '$ADAPTER' is forbidden" >&2
  exit 2
fi
ADAPTER="$SELECTED"

ADAPTER_SH="$KIT_DIR/scripts/adapters/$ADAPTER.sh"
[[ -x "$ADAPTER_SH" ]] || { echo "no adapter: $ADAPTER_SH" >&2; exit 6; }

mkdir -p "$FACTORY_DIR" "$RUNS_DIR" "$LEDGER_DIR"
REPO_LEDGER_HEADER="date,time,ticket,role,adapter,prompt_version,turns,cost_usd,exit_status,run_id,provider_family,model_id,selection_reason,cost_basis,adapter_version"
GLOBAL_LEDGER_HEADER="date,time,repo,ticket,role,adapter,prompt_version,turns,cost_usd,exit_status,run_id,provider_family,model_id,selection_reason,cost_basis,adapter_version"
LEGACY_REPO_HEADER="date,time,ticket,role,adapter,prompt_version,turns,cost_usd,exit_status"
PARTIAL_REPO_HEADER="$LEGACY_REPO_HEADER,run_id,provider_family"
LEGACY_GLOBAL_HEADER="date,time,repo,ticket,role,adapter,prompt_version,turns,cost_usd,exit_status"
PARTIAL_GLOBAL_HEADER="$LEGACY_GLOBAL_HEADER,run_id,provider_family"
TODAY="$(date +%F)"
RUN_ID="$(date +%s)-$$"
MANIFEST="$RUNS_DIR/$RUN_ID.meta"
write_manifest "resolved"

# Serialize task registration with kill-switch KILL creation + PID scanning.
for i in $(seq 1 100); do
  mkdir "$LAUNCH_LOCK" 2>/dev/null && { HELD_LAUNCH_LOCK=1; break; }
  sleep 0.1
done
if [[ "$HELD_LAUNCH_LOCK" -ne 1 ]]; then
  echo "launch lock stuck — no task was submitted" >&2
  exit 8
fi
if [[ -f "$FACTORY_DIR/KILL" ]]; then
  echo "KILL file appeared before reservation; no task was submitted" >&2
  exit 4
fi
if [[ -f "$FACTORY_DIR/MAINTENANCE" ]]; then
  echo "MAINTENANCE file appeared after launch lock acquisition; no task was submitted" >&2
  exit 4
fi
if ! factory_validate_kit_pin "$KIT_DIR" "$REPO_ROOT"; then
  echo "$FACTORY_KIT_PIN_ERROR after launch lock acquisition; no task was submitted" >&2
  exit 3
fi
if ! sequencer_allows_role; then
  echo "$SEQUENCER_ERROR after launch lock acquisition; no task was submitted" >&2
  exit 10
fi
if ! factory_record_ticket_kit_sha "$TICKET_FILE" "$FACTORY_KIT_SHA"; then
  echo "$FACTORY_TICKET_KIT_ERROR; no task was submitted" >&2
  exit 3
fi
if [[ "${FACTORY_TEST_MODE:-0}" == "1" &&
      "${FACTORY_TEST_BEFORE_REGISTER_SLEEP:-0}" != "0" ]]; then
  sleep "$FACTORY_TEST_BEFORE_REGISTER_SLEEP"
fi

# --- serialized cap check with budget reservation ---
# mkdir is atomic: it is the lock. Reservation counts this run's full per-run
# budget against the caps, so N concurrent runs cannot all squeeze past.
for i in $(seq 1 50); do mkdir "$LOCK_DIR" 2>/dev/null && { HELD_LEDGER_LOCK=1; break; }; sleep 0.2; [[ $i -eq 50 ]] && { echo "ledger lock stuck — see runbook" >&2; exit 8; }; done
if [[ ! -f "$LEDGER" ]]; then
  echo "$REPO_LEDGER_HEADER" > "$LEDGER"
else
  CURRENT_HEADER="$(awk 'NR==1 {print; exit}' "$LEDGER")"
  case "$CURRENT_HEADER" in
    "$REPO_LEDGER_HEADER") ;;
    "$LEGACY_REPO_HEADER"|"$PARTIAL_REPO_HEADER")
      TMP_HEADER="$LEDGER.header.$$"
      { echo "$REPO_LEDGER_HEADER"; awk 'NR>1' "$LEDGER"; } > "$TMP_HEADER"
      mv "$TMP_HEADER" "$LEDGER"
      ;;
    *)
      echo "unsupported ledger schema; refusing automatic rewrite: $CURRENT_HEADER" >&2
      exit 3
      ;;
  esac
fi

# --- one live run per ticket+role across all linked worktrees ---
# Acquisition is serialized by the ledger lock above, including stale cleanup.
ACTIVE_RUNS_DIR="$LEDGER_DIR/.active-runs"
GUARD_KEY="$(printf '%s.%s' "$TICKET" "$ROLE" | tr -c 'A-Za-z0-9._-' '_')"
ACTIVE_RUN_FILE="$ACTIVE_RUNS_DIR/$GUARD_KEY.pid"
ACTIVE_RUN_TEMP="$ACTIVE_RUNS_DIR/.$GUARD_KEY.$$.pid"
mkdir -p "$ACTIVE_RUNS_DIR"
echo "$$" > "$ACTIVE_RUN_TEMP"
while ! ln "$ACTIVE_RUN_TEMP" "$ACTIVE_RUN_FILE" 2>/dev/null; do
  EXISTING_PID="$(cat "$ACTIVE_RUN_FILE" 2>/dev/null || true)"
  if [[ "$EXISTING_PID" =~ ^[0-9]+$ ]] && kill -0 "$EXISTING_PID" 2>/dev/null; then
    echo "live run already exists for $TICKET role $ROLE (wrapper pid $EXISTING_PID) — refusing duplicate launch" >&2
    exit 7
  fi
  rm -f "$ACTIVE_RUN_FILE"
done
OWNS_ACTIVE_RUN=1
rm -f "$ACTIVE_RUN_TEMP"
ACTIVE_RUN_TEMP=""

SPENT_TODAY="$(awk -F, -v d="$TODAY" 'NR>1 && $1==d {s+=$8} END {printf "%.4f", s+0}' "$LEDGER")"
SPENT_TICKET="$(awk -F, -v t="$TICKET" 'NR>1 && $3==t {s+=$8} END {printf "%.4f", s+0}' "$LEDGER")"
if awk -v s="$SPENT_TODAY" -v r="$PER_RUN_BUDGET_USD" -v cap="$DAILY_CAP_USD" 'BEGIN{exit !((s+r)>cap)}'; then
  echo "daily cap would be exceeded (spent \$$SPENT_TODAY + reserve \$$PER_RUN_BUDGET_USD > \$$DAILY_CAP_USD) — refusing. See docs/runbooks/operator.md." >&2
  exit 5
fi
if awk -v s="$SPENT_TICKET" -v r="$PER_RUN_BUDGET_USD" -v cap="$PER_TICKET_BUDGET_USD" 'BEGIN{exit !((s+r)>cap)}'; then
  echo "ticket budget would be exceeded for $TICKET (spent \$$SPENT_TICKET + reserve \$$PER_RUN_BUDGET_USD > \$$PER_TICKET_BUDGET_USD) — move ticket to Blocked-Escalated." >&2
  exit 5
fi
# Global cap check + reservation (own lock, taken while holding the repo
# lock — lock order is always repo → global, so no deadlock is possible).
PROMPT_VERSION="unversioned"
[[ -n "$PROMPT_FILE" && -f "$PROMPT_FILE" ]] && PROMPT_VERSION="$(grep -m1 '^Version:' "$PROMPT_FILE" | awk '{print $2}' || echo unversioned)"
LEDGER_FAMILY="$(meta_value "$SELECTED_FAMILY")"
LEDGER_MODEL="$(meta_value "$SELECTED_MODEL")"
LEDGER_REASON="$(meta_value "$SELECTION_REASON")"
LEDGER_VERSION="$(meta_value "$SELECTED_VERSION")"
if [[ -n "$GLOBAL_LEDGER" ]]; then
  mkdir -p "$(dirname "$GLOBAL_LEDGER")"
  for i in $(seq 1 50); do mkdir "$GLOBAL_LOCK" 2>/dev/null && { HELD_GLOBAL_LOCK=1; break; }; sleep 0.2; [[ $i -eq 50 ]] && { echo "global ledger lock stuck — see runbook" >&2; rmdir "$LOCK_DIR"; HELD_LEDGER_LOCK=0; exit 8; }; done
  if [[ ! -f "$GLOBAL_LEDGER" ]]; then
    echo "$GLOBAL_LEDGER_HEADER" > "$GLOBAL_LEDGER"
  else
    CURRENT_GLOBAL_HEADER="$(awk 'NR==1 {print; exit}' "$GLOBAL_LEDGER")"
    case "$CURRENT_GLOBAL_HEADER" in
      "$GLOBAL_LEDGER_HEADER") ;;
      "$LEGACY_GLOBAL_HEADER"|"$PARTIAL_GLOBAL_HEADER")
        TMP_HEADER="$GLOBAL_LEDGER.header.$$"
        { echo "$GLOBAL_LEDGER_HEADER"; awk 'NR>1' "$GLOBAL_LEDGER"; } > "$TMP_HEADER"
        mv "$TMP_HEADER" "$GLOBAL_LEDGER"
        ;;
      *)
        echo "unsupported global ledger schema; refusing automatic rewrite: $CURRENT_GLOBAL_HEADER" >&2
        exit 3
        ;;
    esac
  fi
  SPENT_GLOBAL="$(awk -F, -v d="$TODAY" 'NR>1 && $1==d {s+=$9} END {printf "%.4f", s+0}' "$GLOBAL_LEDGER")"
  if awk -v s="$SPENT_GLOBAL" -v r="$PER_RUN_BUDGET_USD" -v cap="$GLOBAL_DAILY_CAP_USD" 'BEGIN{exit !((s+r)>cap)}'; then
    echo "MACHINE daily cap would be exceeded across all factories (spent \$$SPENT_GLOBAL + reserve \$$PER_RUN_BUDGET_USD > \$$GLOBAL_DAILY_CAP_USD) — refusing. See docs/runbooks/operator.md." >&2
    rmdir "$GLOBAL_LOCK" "$LOCK_DIR"; HELD_GLOBAL_LOCK=0; HELD_LEDGER_LOCK=0; exit 5
  fi
  echo "$TODAY,$(date +%T),$REPO_ROOT,$TICKET,$ROLE,$ADAPTER,reserved,0,$PER_RUN_BUDGET_USD,reserved-$RUN_ID,$RUN_ID,$LEDGER_FAMILY,$LEDGER_MODEL,$LEDGER_REASON,conservative_reservation,$LEDGER_VERSION" >> "$GLOBAL_LEDGER"
  rmdir "$GLOBAL_LOCK"
  HELD_GLOBAL_LOCK=0
fi

# Reserve: write a provisional ledger row at full per-run budget; replaced with
# the real cost after the run. A crash leaves the conservative row in place.
echo "$TODAY,$(date +%T),$TICKET,$ROLE,$ADAPTER,reserved,0,$PER_RUN_BUDGET_USD,reserved-$RUN_ID,$RUN_ID,$LEDGER_FAMILY,$LEDGER_MODEL,$LEDGER_REASON,conservative_reservation,$LEDGER_VERSION" >> "$LEDGER"
rmdir "$LOCK_DIR"; HELD_LEDGER_LOCK=0
write_manifest "reserved"

# --- run one task-bearing process in an isolated process group ---
set +e
RUN_READY_FILE="$RUNS_DIR/.$RUN_ID.ready"
RUN_GO_FILE="$RUNS_DIR/.$RUN_ID.go"
rm -f "$RUN_READY_FILE" "$RUN_GO_FILE"
ADAPTER_ARGS=(
  --budget "$PER_RUN_BUDGET_USD"
  --max-turns "$PER_RUN_MAX_TURNS"
  --timeout-min "$PER_RUN_TIMEOUT_MIN"
  --prompt-file "${PROMPT_FILE:-/dev/null}"
  --workdir "$WORKDIR"
)
case "$ADAPTER" in
  codex|claude-code)
    ADAPTER_ARGS+=(--model "$SELECTED_MODEL" --effort "$SELECTED_EFFORT")
    ;;
esac
python3 "$KIT_DIR/scripts/lib/run-in-process-group.py" \
  "$RUN_READY_FILE" "$RUN_GO_FILE" "$ADAPTER_SH" \
  "${ADAPTER_ARGS[@]}" \
  -- "$TASK" > "$RUNS_DIR/$RUN_ID.out" 2>&1 &
RUN_PID=$!
RUN_PGID="$RUN_PID"
RUN_GROUP_ACTIVE=1
RUN_GROUP_TERMINATED=0
for _ready_try in $(seq 1 500); do
  [[ -f "$RUN_READY_FILE" ]] && break
  kill -0 "$RUN_PID" 2>/dev/null || break
  sleep 0.01
done
READY_PID="$(sed -n 's/^pid=//p' "$RUN_READY_FILE" 2>/dev/null | awk 'NR==1 {print; exit}')"
READY_PGID="$(sed -n 's/^pgid=//p' "$RUN_READY_FILE" 2>/dev/null | awk 'NR==1 {print; exit}')"
if [[ "$READY_PID" != "$RUN_PID" || "$READY_PGID" != "$RUN_PID" ]]; then
  echo "process-group readiness handshake failed; no task was submitted" >&2
  terminate_run_group
  wait "$RUN_PID" 2>/dev/null
  STATUS=125
elif [[ -f "$FACTORY_DIR/KILL" ]]; then
  echo "KILL file appeared before task submission; run cancelled" >&2
  terminate_run_group
  wait "$RUN_PID" 2>/dev/null
  STATUS=4
else
  RUN_START_ID="$(ps -o lstart= -p "$RUN_PID" 2>/dev/null | awk '{$1=$1; print; exit}')"
  if [[ -z "$RUN_START_ID" ]]; then
    echo "could not validate process start identity; no task was submitted" >&2
    terminate_run_group
    wait "$RUN_PID" 2>/dev/null
    STATUS=125
  else
    RUN_PID_FILE="$RUNS_DIR/$RUN_ID.pid"
    {
      echo "pid=$RUN_PID"
      echo "pgid=$RUN_PGID"
      echo "run_id=$RUN_ID"
      echo "process_start=$RUN_START_ID"
    } > "$RUN_PID_FILE"
    write_manifest "prepared"
    if [[ "${FACTORY_TEST_MODE:-0}" == "1" &&
          "${FACTORY_TEST_BEFORE_GO_SLEEP:-0}" != "0" ]]; then
      sleep "$FACTORY_TEST_BEFORE_GO_SLEEP"
    fi
    if [[ -f "$FACTORY_DIR/KILL" ]]; then
      echo "KILL file appeared before GO; no task was submitted" >&2
      terminate_run_group
      wait "$RUN_PID" 2>/dev/null
      STATUS=4
    elif [[ -f "$FACTORY_DIR/MAINTENANCE" ]]; then
      echo "MAINTENANCE file appeared before GO; no task was submitted" >&2
      terminate_run_group
      wait "$RUN_PID" 2>/dev/null
      STATUS=4
    elif ! factory_validate_kit_pin "$KIT_DIR" "$REPO_ROOT"; then
      echo "$FACTORY_KIT_PIN_ERROR before GO; no task was submitted" >&2
      terminate_run_group
      wait "$RUN_PID" 2>/dev/null
      STATUS=3
    elif ! factory_validate_ticket_kit_sha "$TICKET_FILE" "$FACTORY_KIT_SHA"; then
      echo "$FACTORY_TICKET_KIT_ERROR; no task was submitted" >&2
      terminate_run_group
      wait "$RUN_PID" 2>/dev/null
      STATUS=3
    elif ! sequencer_allows_role; then
      echo "$SEQUENCER_ERROR before GO; no task was submitted" >&2
      terminate_run_group
      wait "$RUN_PID" 2>/dev/null
      STATUS=10
    else
      : > "$RUN_GO_FILE"
      write_manifest "spawned"
      rmdir "$LAUNCH_LOCK"
      HELD_LAUNCH_LOCK=0
      wait "$RUN_PID"
      STATUS=$?
    fi
  fi
fi
if [[ "$HELD_LAUNCH_LOCK" -eq 1 ]]; then
  rmdir "$LAUNCH_LOCK"
  HELD_LAUNCH_LOCK=0
fi
set -e
terminate_run_group || true
if [[ "$RUN_GROUP_TERMINATED" -eq 1 ]]; then
  rm -f "$RUN_PID_FILE"
  RUN_PID_FILE=""
else
  echo "WARNING: process group $RUN_PGID survived; PID record retained for kill-switch" >&2
fi
rm -f "$RUN_READY_FILE" "$RUN_GO_FILE"
RUN_READY_FILE=""
RUN_GO_FILE=""
RESULT="$(cat "$RUNS_DIR/$RUN_ID.out")"

METRICS_LINE="$(printf '%s\n' "$RESULT" | tail -n1)"
TURNS="$(sed -n 's/.*turns=\([0-9][0-9]*\).*/\1/p' <<<"$METRICS_LINE")"
COST="$(sed -n 's/.*cost_usd=\([0-9.][0-9.]*\).*/\1/p' <<<"$METRICS_LINE")"
COST_BASIS="$(sed -n 's/.*cost_basis=\([A-Za-z0-9_-]*\).*/\1/p' <<<"$METRICS_LINE")"
# Unparsable cost: keep the conservative full-budget reservation and say so
# loudly — never silently log $0 against the caps.
if [[ -z "$COST" ]]; then
  echo "WARNING: run cost unparsable — ledger keeps conservative reservation of \$$PER_RUN_BUDGET_USD for this run. Reconcile with the provider console." >&2
  COST="$PER_RUN_BUDGET_USD"
  TURNS="${TURNS:-0}"
  COST_BASIS="conservative_reservation"
elif [[ -z "$COST_BASIS" ]]; then
  case "$ADAPTER" in
    claude-code) COST_BASIS="provider_reported" ;;
    codex) COST_BASIS="estimated_tokens" ;;
    mock) COST_BASIS="test_fixture" ;;
    *) COST_BASIS="estimated_tokens" ;;
  esac
fi

# Replace the reservation row with the real result (under lock).
for i in $(seq 1 50); do
  mkdir "$LOCK_DIR" 2>/dev/null && { HELD_LEDGER_LOCK=1; break; }
  sleep 0.2
done
if [[ "$HELD_LEDGER_LOCK" -ne 1 ]]; then
  echo "ledger lock stuck while finalizing run $RUN_ID — conservative reservation retained" >&2
  exit 8
fi
TMP_LEDGER="$LEDGER.tmp.$$"
awk -F, -v reserved="reserved-$RUN_ID" '$9 != reserved' "$LEDGER" > "$TMP_LEDGER"
echo "$TODAY,$(date +%T),$TICKET,$ROLE,$ADAPTER,$PROMPT_VERSION,${TURNS:-0},$COST,$STATUS,$RUN_ID,$LEDGER_FAMILY,$LEDGER_MODEL,$LEDGER_REASON,$COST_BASIS,$LEDGER_VERSION" >> "$TMP_LEDGER"
mv "$TMP_LEDGER" "$LEDGER"
rmdir "$LOCK_DIR"; HELD_LEDGER_LOCK=0

# Same replacement in the machine-global ledger, if configured.
if [[ -n "$GLOBAL_LEDGER" && -f "$GLOBAL_LEDGER" ]]; then
  for i in $(seq 1 50); do
    mkdir "$GLOBAL_LOCK" 2>/dev/null && { HELD_GLOBAL_LOCK=1; break; }
    sleep 0.2
  done
  if [[ "$HELD_GLOBAL_LOCK" -ne 1 ]]; then
    echo "global ledger lock stuck while finalizing run $RUN_ID — conservative reservation retained" >&2
    exit 8
  fi
  TMP_GLOBAL="$GLOBAL_LEDGER.tmp.$$"
  awk -F, -v reserved="reserved-$RUN_ID" '$10 != reserved' "$GLOBAL_LEDGER" > "$TMP_GLOBAL"
  echo "$TODAY,$(date +%T),$REPO_ROOT,$TICKET,$ROLE,$ADAPTER,$PROMPT_VERSION,${TURNS:-0},$COST,$STATUS,$RUN_ID,$LEDGER_FAMILY,$LEDGER_MODEL,$LEDGER_REASON,$COST_BASIS,$LEDGER_VERSION" >> "$TMP_GLOBAL"
  mv "$TMP_GLOBAL" "$GLOBAL_LEDGER"
  rmdir "$GLOBAL_LOCK"; HELD_GLOBAL_LOCK=0
fi

write_manifest "completed"
printf '%s\n' "$RESULT"
exit "$STATUS"
