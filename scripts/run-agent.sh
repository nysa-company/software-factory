#!/usr/bin/env bash
# run-agent.sh — internal sealed-release role runner. Live dispatch enters it
# only through ~/.factory/bin/factory-launch <project> run.
# Enforces per-run, per-ticket, and daily budgets; serializes cap checks with a
# lock; anchors run artifacts to the caller's product root; records each run
# atomically and materializes an ignored runtime ledger; enforces
# the cross-family role→adapter mapping; rejects overlapping ticket+role runs.
#
# Usage:
#   run-agent.sh --role builder --ticket T-123 --prompt-file factory/roles/builder.md \
#                [--adapter claude-code] [--workdir /path/to/worktree] -- "task text"
#
# Envelope (factory/ENVELOPE.env at the repo root):
#   PER_RUN_BUDGET_USD, PER_TICKET_BUDGET_USD, PER_RUN_MAX_TURNS,
#   PER_RUN_TIMEOUT_MIN, DAILY_CAP_USD, plus optional ROLE_PER_RUN_* values.
set -euo pipefail

ROLE="" TICKET="" PROMPT_FILE="" ADAPTER="" WORKDIR="" WORKDIR_SET=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --role) ROLE="$2"; shift 2;;
    --ticket) TICKET="$2"; shift 2;;
    --prompt-file) PROMPT_FILE="$2"; shift 2;;
    --adapter) ADAPTER="$2"; shift 2;;
    --workdir) WORKDIR="$2"; WORKDIR_SET=1; shift 2;;
    --) shift; break;;
    *) echo "unknown arg: $1" >&2; exit 2;;
  esac
done
TASK="${*:-}"
[[ -n "$ROLE" && -n "$TICKET" && -n "$TASK" ]] || { echo "missing required args" >&2; exit 2; }
[[ "$TICKET" =~ ^T-[0-9]+$ ]] || { echo "invalid ticket identifier" >&2; exit 2; }

KIT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
[[ -z "$(declare -F)" ]] || {
  echo "inherited shell functions are forbidden" >&2
  exit 2
}
# shellcheck disable=SC1091
source "$KIT_DIR/scripts/lib/kit-pin.sh"
# shellcheck disable=SC1091
source "$KIT_DIR/scripts/lib/dispatch-leases.sh"
# shellcheck disable=SC1091
source "$KIT_DIR/scripts/lib/product-remote.sh"
# shellcheck disable=SC1091
source "$KIT_DIR/scripts/lib/plain-config.sh"
FACTORY_TRUSTED_GIT_BIN="$(type -P git 2>/dev/null || true)"
[[ "$FACTORY_TRUSTED_GIT_BIN" == /* && -x "$FACTORY_TRUSTED_GIT_BIN" ]] || {
  echo "trusted Git executable is unavailable" >&2
  exit 2
}
readonly FACTORY_TRUSTED_GIT_BIN
readonly -f factory_capture_product_remote factory_product_remote_matches \
  factory_remote_tracking_tip factory_update_tracking_ref
unset FACTORY_TRUSTED_PRODUCT_ORIGIN
readonly FACTORY_TRUSTED_PRODUCT_ORIGIN="${FACTORY_CERTIFIED_PRODUCT_ORIGIN:-}"
unset FACTORY_CERTIFIED_PRODUCT_ORIGIN
DISPATCH_LEASE_ID="${FACTORY_DISPATCH_LEASE_ID:-}"
unset FACTORY_DISPATCH_LEASE_ID

# --- anchor factory state to the repo root, never to $PWD ---
REPO_ROOT="${FACTORY_ROOT:-$("$FACTORY_TRUSTED_GIT_BIN" rev-parse --show-toplevel 2>/dev/null || echo "$PWD")}"
[[ "$WORKDIR_SET" -eq 1 ]] || WORKDIR="$REPO_ROOT"
FACTORY_DIR="$REPO_ROOT/factory"
BUDGET_DAY=""
PROVIDER_WAIT_SECONDS=0
DEVELOPMENT_LANE_ROOT=""
CLI_RUNTIME_STATE_ROOT=""
CLI_RUNTIME_LAYOUT=""
ROLE_GUARD_ROOT=""
if [[ "${FACTORY_CLI_LANE_ROOT:-}" == /* &&
      ( "$(basename "$FACTORY_CLI_LANE_ROOT")" == nysa-sf-dev.* ||
        "$(basename "$FACTORY_CLI_LANE_ROOT")" == nysa-sf-qualification.* ) &&
      -f "$FACTORY_CLI_LANE_ROOT/marker.json" ]]; then
  case "$(cd "$REPO_ROOT" 2>/dev/null && pwd -P)" in
    "$(cd "$FACTORY_CLI_LANE_ROOT" && pwd -P)/product" | \
    "$(cd "$FACTORY_CLI_LANE_ROOT" && pwd -P)/product"/*)
      DEVELOPMENT_LANE_ROOT="$(cd "$FACTORY_CLI_LANE_ROOT" && pwd -P)"
      ;;
  esac
fi
if [[ -n "${FACTORY_CLI_RUNTIME_ROOT:-}" ]]; then
  # Legacy/serialized releases do not require this owner-local directory.
  # Concurrent admission still refuses below unless the configured root exists
  # and passes the complete safety check.
  if [[ -d "${FACTORY_CLI_RUNTIME_ROOT:-}" &&
        ! -L "${FACTORY_CLI_RUNTIME_ROOT:-}" ]]; then
    CLI_RUNTIME_STATE_ROOT="$(python3 - "${FACTORY_CLI_RUNTIME_ROOT:-}" <<'PY'
import os
import pathlib
import stat
import sys

path = pathlib.Path(sys.argv[1])
try:
    info = path.lstat()
except (FileNotFoundError, OSError):
    raise SystemExit(1)
if (
    not path.is_absolute()
    or path.is_symlink()
    or not stat.S_ISDIR(info.st_mode)
    or path.resolve(strict=True) != path
    or info.st_uid != os.geteuid()
    or stat.S_IMODE(info.st_mode) & 0o077
):
    raise SystemExit(1)
print(path)
PY
    )" || {
      echo "subscription CLI runtime root is unsafe" >&2
      exit 2
    }
    CLI_RUNTIME_LAYOUT="owner"
  fi
elif [[ -n "$DEVELOPMENT_LANE_ROOT" ]]; then
  CLI_RUNTIME_STATE_ROOT="$DEVELOPMENT_LANE_ROOT"
  CLI_RUNTIME_LAYOUT="lane"
fi
if [[ -n "${FACTORY_DEV_BUDGET_DAY:-}" ]]; then
  [[ "${FACTORY_CLI_LANE_ROOT:-}" == /* &&
     "$(basename "$FACTORY_CLI_LANE_ROOT")" == nysa-sf-dev.* &&
     -f "$FACTORY_CLI_LANE_ROOT/marker.json" &&
     "$FACTORY_DEV_BUDGET_DAY" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] || {
    echo "development budget-day binding is invalid" >&2
    exit 2
  }
  case "$(cd "$REPO_ROOT" && pwd -P)" in
    "$(cd "$FACTORY_CLI_LANE_ROOT" && pwd -P)/product" | \
    "$(cd "$FACTORY_CLI_LANE_ROOT" && pwd -P)/product"/*) ;;
    *) echo "development budget-day binding is outside its lane" >&2; exit 2 ;;
  esac
  [[ "$FACTORY_DEV_BUDGET_DAY" == "$(date -u +%F)" ]] || {
    echo "development budget day changed; no task was submitted" >&2
    exit 8
  }
  BUDGET_DAY="$FACTORY_DEV_BUDGET_DAY"
fi
if [[ -n "${FACTORY_DEV_PROVIDER_WAIT_SECONDS:-}" ]]; then
  [[ "${FACTORY_CLI_LANE_ROOT:-}" == /* &&
     "$(basename "$FACTORY_CLI_LANE_ROOT")" == nysa-sf-dev.* &&
     -f "$FACTORY_CLI_LANE_ROOT/marker.json" &&
     "$FACTORY_DEV_PROVIDER_WAIT_SECONDS" =~ ^[1-9][0-9]*$ &&
     "$FACTORY_DEV_PROVIDER_WAIT_SECONDS" -le 900 ]] || {
    echo "development provider wait binding is invalid" >&2
    exit 2
  }
  case "$(cd "$REPO_ROOT" && pwd -P)" in
    "$(cd "$FACTORY_CLI_LANE_ROOT" && pwd -P)/product" | \
    "$(cd "$FACTORY_CLI_LANE_ROOT" && pwd -P)/product"/*) ;;
    *) echo "development provider wait is outside its lane" >&2; exit 2 ;;
  esac
  PROVIDER_WAIT_SECONDS="$FACTORY_DEV_PROVIDER_WAIT_SECONDS"
fi
unset FACTORY_DEV_BUDGET_DAY
unset FACTORY_DEV_PROVIDER_WAIT_SECONDS
readonly BUDGET_DAY PROVIDER_WAIT_SECONDS DEVELOPMENT_LANE_ROOT \
  CLI_RUNTIME_STATE_ROOT CLI_RUNTIME_LAYOUT

# Direct callers may anchor FACTORY_ROOT inside a linked worktree. Runtime
# accounting still belongs beside the same product path in the main checkout.
canonical_factory_file() {
  local root="$1" name="$2" root_abs worktree_root common_dir main_root relative
  root_abs="$(cd "$root" 2>/dev/null && pwd -P || printf '%s' "$root")"
  if worktree_root="$("$FACTORY_TRUSTED_GIT_BIN" -C "$root" rev-parse --show-toplevel 2>/dev/null)" &&
     common_dir="$("$FACTORY_TRUSTED_GIT_BIN" -C "$root" rev-parse --git-common-dir 2>/dev/null)"; then
    worktree_root="$(cd "$worktree_root" && pwd -P)"
    # A relative --git-common-dir is relative to git's cwd ($root), NOT the
    # worktree root — resolving against the wrong base broke main-clone
    # subdirectory roots (e.g. FACTORY_ROOT=<repo>/conformance).
    case "$common_dir" in
      /*) ;;
      *) common_dir="$root_abs/$common_dir" ;;
    esac
    if ! main_root="$(cd "$common_dir/.." 2>/dev/null && pwd -P)"; then
      printf '%s/factory/%s\n' "$root_abs" "$name"
      return
    fi
    if [[ "$root_abs" == "$worktree_root" ]]; then
      relative=""
    elif [[ "$root_abs" == "$worktree_root/"* ]]; then
      relative="${root_abs#"$worktree_root/"}"
    else
      printf '%s/factory/%s\n' "$root_abs" "$name"
      return
    fi
    printf '%s%s/factory/%s\n' "$main_root" "${relative:+/$relative}" "$name"
  else
    printf '%s/factory/%s\n' "$root_abs" "$name"
  fi
}

LEDGER="${FACTORY_LEDGER:-$(canonical_factory_file "$REPO_ROOT" runtime-ledger.csv)}"
DURABLE_LEDGER="${FACTORY_DURABLE_LEDGER:-$(canonical_factory_file "$REPO_ROOT" ledger.csv)}"
ENV_FILE="${FACTORY_ENVELOPE:-$FACTORY_DIR/ENVELOPE.env}"
LEDGER_DIR="$(dirname "$LEDGER")"
LOCK_DIR="$LEDGER_DIR/.ledger.lock"
LAUNCH_LOCK="$FACTORY_DIR/.launch.lock"
PROVIDER_LOCK="$FACTORY_DIR/.provider.lock"
RUNS_DIR="$FACTORY_DIR/runs"
ACTIVE_RUN_FILE=""
ACTIVE_RUN_TEMP=""
ACTIVE_RUN_EXPECTED=""
ACTIVE_RUN_SNAPSHOT=""
OWNS_ACTIVE_RUN=0
HELD_LEDGER_LOCK=0
HELD_GLOBAL_LOCK=0
HELD_LAUNCH_LOCK=0
HELD_PROVIDER_LOCK=0
RETAIN_PROVIDER_LOCK=0
PROVIDER_LOCK_EXPECTED=""
LEGACY_INTERVAL_ACTIVE=0
LEGACY_INTERVAL_ID=""
RUN_PID=""
RUN_PGID=""
RUN_GROUP_ACTIVE=0
RUN_GROUP_TERMINATED=1
RUN_PID_FILE=""
RUN_READY_FILE=""
RUN_GO_FILE=""
RUN_GATE_FILE=""
RUN_SUBMITTED_FILE=""
RUN_OUTPUT_TEMP=""
RUNS_META_SNAPSHOT=""
CONTROL_PLANE_MUTATION=0
REGISTERED_BRANCH_BEFORE=""
REGISTERED_HEAD_BEFORE=""
REGISTERED_STATUS_BEFORE=""
REGISTERED_CONTENT_BEFORE=""
GLOBAL_LEDGER_SNAPSHOT=""
GLOBAL_LOCK_TOKEN=""
GLOBAL_STATE_MUTATED=0
RUN_START_ID=""
MANIFEST=""
MANIFEST_PHASE=""
ROLE_EXIT_STATUS=""
TERMINAL_REASON_CODE=""
ROLE_HEAD_BEFORE=""
ROLE_BRANCH_BEFORE=""
ROLE_REMOTE_BEFORE=""
ROLE_PROTECTED_BEFORE=""
ROLE_ESCALATION_REQUESTED=0
ROLE_ESCALATION_INVALID=0
PRODUCT_REMOTE=""
ACCOUNTING_SCHEMA=""
ACCOUNTING_STATE=""
GO_ISSUED=0
TASK_SUBMITTED=0
ADAPTER_BOUNDARY_STOPPED=0
ADAPTER_BOUNDARY_STOP_PATH=""
RUN_STARTED_AT=""
TERMINAL_AT=""
RESERVED_USD=""
EFFECTIVE_COST=""
EXIT_STATUS=""
COST_BASIS=""
TURNS=0
CANCEL_REQUEST_FILE=""
CANCELLATION_REASON=""
CANCELLATION_PREVIEW_HASH=""
CANCELLATION_ACCEPTED=0
LEASE_HEARTBEAT_PID=""
LEASE_HEARTBEAT_FAILED=0
CLI_ATTEMPT_ID=""
CLI_ATTEMPT_ACTIVE=0
CURSOR_ACCOUNT_LEASE_ID=""
CURSOR_ACCOUNT_LEASE_ACTIVE=0
CURSOR_ACCOUNT_OWNER_PID=""
CURSOR_ACCOUNT_OWNER_PGID=""
CURSOR_ACCOUNT_OWNER_START=""
CLI_RUNTIME_ROOT=""
CLI_PROVIDER_HOME=""
CLI_PROVIDER_TMPDIR=""
CLI_PROVIDER_CACHE_DIR=""
CLI_PROVIDER_OUTPUT_DIR=""
CLI_CLAUDE_CONFIG_DIR=""
CLI_CLAUDE_SETTINGS=""
CLI_CURSOR_CONFIG_DIR=""
CLI_CURSOR_DATA_DIR=""
PROVIDER_EXECUTION_MODE="legacy-serialized"
PROVIDER_BUDGET_MICRO_VALUES=()
RUN_OUTPUT_SHA256=""
PROGRESS_EVENTS=""
PROGRESS_JOURNAL_SHA256=""
TIMEOUT_KIND=""
PROMPT_VERSION="unversioned"
SEQUENCER="$KIT_DIR/scripts/next-stage.sh"
MONEY="$KIT_DIR/scripts/lib/money.py"
ENVELOPE_CONTROL="$KIT_DIR/scripts/envelope-control.py"
SEQUENCER_ERROR=""
PROVIDER_CONTRACT_VERSION="${FACTORY_RELEASE_CONTRACT_VERSION:-${FACTORY_CONTRACT_VERSION:-${FACTORY_HERMES_CONTRACT_VERSION:-}}}"
unset TRANSITION_PROJECT
readonly TRANSITION_PROJECT="${FACTORY_PROJECT:-}"

load_effective_envelope() {
  local key value output
  output="$(python3 -B "$ENVELOPE_CONTROL" effective \
    --factory-root "$REPO_ROOT" --ticket "$TICKET" --role "$ROLE" \
    --day "${BUDGET_DAY:-$(date -u +%F)}" \
    --base-envelope "$ENV_FILE" \
    --global-env "$GLOBAL_ENV" --format shell)" || return 1
  while IFS='=' read -r key value; do
    case "$key" in
      PER_RUN_BUDGET_USD|PER_TICKET_BUDGET_USD|PER_RUN_MAX_TURNS|PER_RUN_TIMEOUT_MIN|DAILY_CAP_USD|GLOBAL_DAILY_CAP_USD|FACTORY_ENVELOPE_OVERRIDE_IDS|FACTORY_ENVELOPE_NEXT_OVERRIDE_IDS)
        printf -v "$key" '%s' "$value"
        ;;
      *) return 1 ;;
    esac
  done <<<"$output"
}

sequencer_allows_role() {
  local output rc=0
  SEQUENCER_ERROR=""
  if [[ "$PROVIDER_CONTRACT_VERSION" == "1.8.0" || "$PROVIDER_CONTRACT_VERSION" == "1.9.0" ]]; then
    if [[ ! "${FACTORY_TRANSITION_RECEIPT_SHA256:-}" =~ ^[0-9a-f]{64}$ ||
          -z "${FACTORY_TRANSITION_STATE_DIR:-}" ||
          -z "$TRANSITION_PROJECT" ]]; then
      SEQUENCER_ERROR="consumed transition receipt is unavailable"
      return 1
    fi
    local -a receipt_args=(
      verify
      --factory-root "$REPO_ROOT"
      --workdir "$WORKDIR"
      --kit-dir "$KIT_DIR"
      --state-dir "$FACTORY_TRANSITION_STATE_DIR"
      --ticket "$TICKET"
      --contract-version "$PROVIDER_CONTRACT_VERSION"
      --factory-sha "$FACTORY_KIT_SHA"
      --project "$TRANSITION_PROJECT"
      --receipt "$FACTORY_TRANSITION_RECEIPT_SHA256"
      --role "$ROLE"
      --require-used
    )
    [[ -z "$DISPATCH_LEASE_ID" ]] ||
      receipt_args+=(--lease "$DISPATCH_LEASE_ID")
    if ! FACTORY_CERTIFIED_PRODUCT_ORIGIN="$FACTORY_TRUSTED_PRODUCT_ORIGIN" \
      python3 -B "$KIT_DIR/scripts/state-machine.py" "${receipt_args[@]}" \
        >/dev/null 2>&1; then
      SEQUENCER_ERROR="consumed transition receipt no longer authorizes the role"
      return 1
    fi
    return 0
  fi
  if [[ ! -f "$SEQUENCER" || -L "$SEQUENCER" ]]; then
    SEQUENCER_ERROR="selected release sequencer is missing or unsafe"
    return 1
  fi
  if [[ -n "$DISPATCH_LEASE_ID" ]]; then
    if [[ -n "${FACTORY_LEDGER:-}" ]]; then
      output="$(FACTORY_ROOT="$REPO_ROOT" FACTORY_LEDGER="$LEDGER" \
        bash "$SEQUENCER" --ticket "$TICKET" --lease "$DISPATCH_LEASE_ID" \
          --workdir "$WORKDIR" 2>/dev/null)" || rc=$?
    else
      output="$(FACTORY_ROOT="$REPO_ROOT" \
        bash "$SEQUENCER" --ticket "$TICKET" --lease "$DISPATCH_LEASE_ID" \
          --workdir "$WORKDIR" 2>/dev/null)" || rc=$?
    fi
  else
    if [[ -n "${FACTORY_LEDGER:-}" ]]; then
      output="$(FACTORY_ROOT="$REPO_ROOT" FACTORY_LEDGER="$LEDGER" \
        bash "$SEQUENCER" --ticket "$TICKET" --workdir "$WORKDIR" 2>/dev/null)" || rc=$?
    else
      output="$(FACTORY_ROOT="$REPO_ROOT" \
        bash "$SEQUENCER" --ticket "$TICKET" --workdir "$WORKDIR" 2>/dev/null)" || rc=$?
    fi
  fi
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
  if [[ "$output" == "FIX $ROLE" &&
        ( "$ROLE" == "builder" || "$ROLE" == "test-author" ) ]]; then
    return 0
  fi
  SEQUENCER_ERROR="sequencer did not authorize the requested role"
  return 1
}

meta_value() {
  printf '%s' "${1:-}" | tr '\n,' '__'
}

registered_tracked_content() {
  "$FACTORY_TRUSTED_GIT_BIN" -C "$REPO_ROOT" diff --binary HEAD -- |
    "$FACTORY_TRUSTED_GIT_BIN" hash-object --stdin
}

registered_ref_identity() {
  local branch status=0
  branch="$("$FACTORY_TRUSTED_GIT_BIN" -C "$REPO_ROOT" \
    symbolic-ref --quiet --short HEAD 2>/dev/null)" || status=$?
  case "$status" in
    0) printf 'branch:%s' "$branch" ;;
    1) printf 'detached' ;;
    *) return "$status" ;;
  esac
}

registered_status_after_run() {
  local status relative line
  status="$("$FACTORY_TRUSTED_GIT_BIN" -C "$REPO_ROOT" \
    status --porcelain --untracked-files=all 2>/dev/null)" || return 1
  if [[ "$ADAPTER_BOUNDARY_STOPPED" -eq 0 ]]; then
    printf '%s' "$status"
    return 0
  fi
  [[ -n "$ADAPTER_BOUNDARY_STOP_PATH" ]] || return 1
  relative="$ADAPTER_BOUNDARY_STOP_PATH"
  while IFS= read -r line; do
    if [[ "${#line}" -ge 4 && "${line:3}" == "$relative" ]]; then
      printf '%s' "$status"
      return 0
    fi
  done <<EOF
$REGISTERED_STATUS_BEFORE
EOF
  while IFS= read -r line; do
    if [[ "${#line}" -lt 4 || "${line:3}" != "$relative" ]]; then
      printf '%s\n' "$line"
    fi
  done <<EOF
$status
EOF
}

process_start_identity() {
  ps -o lstart= -p "$1" 2>/dev/null | awk '{$1=$1; print; exit}'
}

load_cancellation_request() {
  local output parsed
  [[ -n "$CANCEL_REQUEST_FILE" && -f "$CANCEL_REQUEST_FILE" &&
      ! -L "$CANCEL_REQUEST_FILE" ]] || return 1
  output="$(python3 "$KIT_DIR/scripts/attempt-cancel.py" request \
    --factory-root "$REPO_ROOT" --ticket "$TICKET" --run-id "$RUN_ID" 2>/dev/null)" ||
    return 2
  parsed="$(printf '%s' "$output" | python3 -c '
import json, re, sys
value = json.load(sys.stdin)
reason = value.get("reason", "")
preview = value.get("preview_hash", "")
if reason not in ("budget_exhausted", "operator_requested"):
    raise SystemExit(1)
if not re.fullmatch(r"[0-9a-f]{64}", preview):
    raise SystemExit(1)
print(reason)
print(preview)
')" || return 2
  CANCELLATION_REASON="${parsed%%$'\n'*}"
  CANCELLATION_PREVIEW_HASH="${parsed#*$'\n'}"
}

ensure_runs_directory() {
  [[ ! -L "$RUNS_DIR" ]] || return 1
  mkdir -p "$FACTORY_DIR" "$LEDGER_DIR" "$RUNS_DIR" || return 1
  python3 - "$FACTORY_DIR" "$RUNS_DIR" <<'PY'
import os
import stat
import sys

for raw in sys.argv[1:]:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(raw, flags)
    try:
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError(f"not a directory: {raw}")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
PY
}

active_claim_snapshot() {
  python3 - "$ACTIVE_RUN_FILE" "$ACTIVE_RUN_EXPECTED" <<'PY'
import base64
import os
import stat
import sys
from pathlib import Path

directory = Path(sys.argv[1])
expected = sys.argv[2].encode()
owner = directory / "owner"
directory_stat = directory.lstat()
owner_stat = owner.lstat()
content = owner.read_bytes()
if (not stat.S_ISDIR(directory_stat.st_mode) or directory.is_symlink() or
        not stat.S_ISREG(owner_stat.st_mode) or owner.is_symlink() or content.rstrip(b"\n") != expected):
    raise SystemExit(1)
if sorted(entry.name for entry in directory.iterdir()) != ["owner"]:
    raise SystemExit(1)
print(f"{directory_stat.st_dev}:{directory_stat.st_ino}:"
      f"{owner_stat.st_dev}:{owner_stat.st_ino}:"
      f"{base64.b64encode(content).decode()}")
PY
}

validate_global_ledger() {
  python3 - "$1" <<'PY'
import csv
import re
import sys
from decimal import Decimal
from pathlib import Path

path = Path(sys.argv[1])
header = ("date,time,repo,ticket,role,adapter,prompt_version,turns,cost_usd,"
          "exit_status,run_id,provider_family,model_id,selection_reason,cost_basis,adapter_version").split(",")
with path.open(newline="", encoding="utf-8") as handle:
    reader = csv.DictReader(handle)
    if reader.fieldnames != header:
        raise SystemExit(1)
    for row in reader:
        if None in row or None in row.values():
            raise SystemExit(1)
        if any(not row[key] for key in ("date", "repo", "ticket", "role", "adapter", "turns", "cost_usd", "exit_status")):
            raise SystemExit(1)
        turns, cost, status, run_id = row["turns"], row["cost_usd"], row["exit_status"], row["run_id"]
        if not re.fullmatch(r"[0-9]{1,4}", turns) or int(turns) > 1000:
            raise SystemExit(1)
        if (not re.fullmatch(r"[0-9]{1,7}(?:\.[0-9]{1,18})?", cost) or
                Decimal(cost) > Decimal("1000000")):
            raise SystemExit(1)
        if run_id and not re.fullmatch(r"[A-Za-z0-9._-]{1,200}", run_id):
            raise SystemExit(1)
        if not ((re.fullmatch(r"[0-9]{1,3}", status) and int(status) <= 255) or
                (run_id and status == f"reserved-{run_id}")):
            raise SystemExit(1)
PY
}

snapshot_global_ledger() {
  python3 - "$1" <<'PY'
import base64
import sys
from pathlib import Path
print(base64.b64encode(Path(sys.argv[1]).read_bytes()).decode("ascii"))
PY
}

restore_global_ledger() {
  printf '%s' "$GLOBAL_LEDGER_SNAPSHOT" | python3 -c '
import base64
import os
import sys
import tempfile
from pathlib import Path

path = Path(sys.argv[1])
content = base64.b64decode(sys.stdin.buffer.read().strip(), validate=True)
descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
try:
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
finally:
    try:
        os.unlink(temporary)
    except FileNotFoundError:
        pass
' "$GLOBAL_LEDGER"
}

global_lock_is_owned() {
  [[ -n "$GLOBAL_LOCK_TOKEN" && -f "$GLOBAL_LOCK/owner" && ! -L "$GLOBAL_LOCK/owner" &&
      "$(cat "$GLOBAL_LOCK/owner" 2>/dev/null)" == "$GLOBAL_LOCK_TOKEN" ]]
}

release_global_lock() {
  global_lock_is_owned || return 1
  rm -f "$GLOBAL_LOCK/owner" || return 1
  rmdir "$GLOBAL_LOCK" || return 1
  HELD_GLOBAL_LOCK=0
}

provider_lock_is_owned() {
  [[ "$HELD_PROVIDER_LOCK" -eq 1 && -d "$PROVIDER_LOCK" && ! -L "$PROVIDER_LOCK" &&
      -f "$PROVIDER_LOCK/owner" && ! -L "$PROVIDER_LOCK/owner" &&
      "$(cat "$PROVIDER_LOCK/owner" 2>/dev/null)" == "$PROVIDER_LOCK_EXPECTED" ]] &&
    provider_lock_owner_is_live
}

provider_lock_owner_is_live() {
  local identity pid started
  identity="$(python3 - "$PROVIDER_LOCK" <<'PY'
import re
import stat
import sys
from pathlib import Path

directory = Path(sys.argv[1])
owner = directory / "owner"
try:
    directory_stat = directory.lstat()
    owner_stat = owner.lstat()
except FileNotFoundError:
    raise SystemExit(3)
if (not stat.S_ISDIR(directory_stat.st_mode) or directory.is_symlink() or
        not stat.S_ISREG(owner_stat.st_mode) or owner.is_symlink() or
        owner_stat.st_nlink != 1 or
        sorted(entry.name for entry in directory.iterdir()) != ["owner"]):
    raise SystemExit(2)
lines = owner.read_text(encoding="utf-8").splitlines()
if (len(lines) != 3 or not re.fullmatch(r"pid=[1-9][0-9]*", lines[0]) or
        not lines[1].startswith("process_start=") or len(lines[1]) == 14 or
        not re.fullmatch(r"token=[0-9a-f]{32}", lines[2])):
    raise SystemExit(2)
print(lines[0][4:])
print(lines[1][14:])
PY
  )" || return 2
  pid="${identity%%$'\n'*}"
  started="${identity#*$'\n'}"
  [[ "$(process_start_identity "$pid")" == "$started" ]]
}

release_provider_lock() {
  local quarantine
  provider_lock_is_owned || return 1
  quarantine="$RUNS_DIR/.provider-lock-release-$CLAIM_TOKEN"
  [[ ! -e "$quarantine" && ! -L "$quarantine" ]] || return 1
  mv "$PROVIDER_LOCK" "$quarantine" || return 1
  HELD_PROVIDER_LOCK=0
  rm -f "$quarantine/owner" || return 1
  rmdir "$quarantine" || return 1
}

release_legacy_interval() {
  [[ "$LEGACY_INTERVAL_ACTIVE" -eq 1 ]] || return 0
  local output
  output="$(python3 "$KIT_DIR/scripts/provider-coordinator.py" \
    --db "$FACTORY_PROVIDER_DB" legacy-exit \
    --operation-id "$LEGACY_INTERVAL_ID-exit" \
    --interval-id "$LEGACY_INTERVAL_ID" 2>/dev/null)" || return 1
  printf '%s' "$output" | python3 -c '
import json, sys
raise SystemExit(0 if json.load(sys.stdin).get("exited") else 1)
' || return 1
  LEGACY_INTERVAL_ACTIVE=0
}

restore_global_if_changed() {
  [[ -n "$GLOBAL_LEDGER_SNAPSHOT" ]] || return 0
  local current
  current="$(snapshot_global_ledger "$GLOBAL_LEDGER" 2>/dev/null || true)"
  if [[ "$current" == "$GLOBAL_LEDGER_SNAPSHOT" ]] && global_lock_is_owned; then
    return 0
  fi
  GLOBAL_STATE_MUTATED=1
  global_lock_is_owned || return 1
  restore_global_ledger && validate_global_ledger "$GLOBAL_LEDGER"
}

write_manifest() {
  local phase="$1"
  [[ -n "$MANIFEST" ]] || return 0
  if [[ "$phase" == "spawned" && "${FACTORY_TEST_MODE:-0}" == "1" &&
        "${FACTORY_TEST_FAIL_GO_MANIFEST_WRITE:-0}" == "1" ]]; then
    return 1
  fi
  if ! {
    echo "run_id=$(meta_value "${RUN_ID:-}")"
    echo "phase=$(meta_value "$phase")"
    echo "accounting_schema=$(meta_value "$ACCOUNTING_SCHEMA")"
    echo "accounting_state=$(meta_value "$ACCOUNTING_STATE")"
    echo "reserved_usd=$(meta_value "$RESERVED_USD")"
    echo "envelope_per_run_budget_usd=$(meta_value "${PER_RUN_BUDGET_USD:-}")"
    echo "envelope_per_ticket_budget_usd=$(meta_value "${PER_TICKET_BUDGET_USD:-}")"
    echo "envelope_max_turns=$(meta_value "${PER_RUN_MAX_TURNS:-}")"
    echo "envelope_timeout_min=$(meta_value "${PER_RUN_TIMEOUT_MIN:-}")"
    echo "envelope_daily_cap_usd=$(meta_value "${DAILY_CAP_USD:-}")"
    echo "envelope_override_ids=$(meta_value "${FACTORY_ENVELOPE_OVERRIDE_IDS:-}")"
    echo "go_issued=$(meta_value "$GO_ISSUED")"
    echo "task_submitted=$(meta_value "$TASK_SUBMITTED")"
    echo "started_at=$(meta_value "$RUN_STARTED_AT")"
    echo "terminal_at=$(meta_value "$TERMINAL_AT")"
    echo "prompt_version=$(meta_value "$PROMPT_VERSION")"
    echo "turns=$(meta_value "$TURNS")"
    echo "effective_cost=$(meta_value "$EFFECTIVE_COST")"
    echo "exit_status=$(meta_value "$EXIT_STATUS")"
    echo "cost_basis=$(meta_value "$COST_BASIS")"
    echo "ticket=$(meta_value "$TICKET")"
    echo "role=$(meta_value "$ROLE")"
    echo "adapter=$(meta_value "${ADAPTER:-}")"
    echo "provider_family=$(meta_value "${SELECTED_FAMILY:-}")"
    echo "model_id=$(meta_value "${SELECTED_MODEL:-}")"
    echo "effort=$(meta_value "${SELECTED_EFFORT:-}")"
    echo "selection_reason=$(meta_value "${SELECTION_REASON:-}")"
    echo "adapter_version=$(meta_value "${SELECTED_VERSION:-}")"
    echo "route_id=$(meta_value "${SELECTED_ROUTE_ID:-}")"
    echo "gateway_id=$(meta_value "${SELECTED_GATEWAY_ID:-}")"
    echo "inference_provider_id=$(meta_value "${SELECTED_PROVIDER_ID:-}")"
    echo "account_route_id=$(meta_value "${SELECTED_ACCOUNT_ROUTE_ID:-}")"
    echo "provider_execution_mode=$(meta_value "${PROVIDER_EXECUTION_MODE:-legacy-serialized}")"
    echo "provider_attempt_id=$(meta_value "${CLI_ATTEMPT_ID:-}")"
    echo "activation_policy_sha256=$(meta_value "${ACTIVATED_POLICY_HASH:-}")"
    echo "transport=$(meta_value "${SELECTED_TRANSPORT:-}")"
    echo "policy_hash=$(meta_value "${SELECTED_POLICY_HASH:-}")"
    echo "route_plan_sha256=$(meta_value "${SELECTED_ROUTE_PLAN_SHA256:-}")"
    echo "route_revision=$(meta_value "${SELECTED_ROUTE_REVISION:-}")"
    echo "route_revision_hash=$(meta_value "${SELECTED_ROUTE_REVISION_HASH:-}")"
    echo "primary_probe=$(meta_value "${PRIMARY_PROBE_SUMMARY:-}")"
    echo "kit_sha=$(meta_value "${FACTORY_KIT_SHA:-}")"
    echo "kit_tree=$(meta_value "${FACTORY_KIT_TREE:-}")"
    echo "product_tree=$(meta_value "${FACTORY_PRODUCT_TREE:-}")"
    echo "ticket_kit_sha=$(meta_value "${FACTORY_TICKET_KIT_SHA:-}")"
    echo "contract_version=$(meta_value "${FACTORY_CONTRACT_VERSION:-}")"
    echo "physical_kit_path=$(meta_value "${FACTORY_KIT_PATH:-}")"
    echo "kit_provenance_mode=$(meta_value "${FACTORY_KIT_PROVENANCE_MODE:-}")"
    echo "kit_provenance_scope=$(meta_value "${FACTORY_KIT_PROVENANCE_SCOPE:-}")"
    echo "pid=$(meta_value "${RUN_PID:-}")"
    echo "pgid=$(meta_value "${RUN_PGID:-}")"
    echo "process_start=$(meta_value "${RUN_START_ID:-}")"
    echo "role_exit=$(meta_value "${ROLE_EXIT_STATUS:-}")"
    echo "role_branch_before=$(meta_value "${ROLE_BRANCH_BEFORE:-}")"
    echo "role_head_before=$(meta_value "${ROLE_HEAD_BEFORE:-}")"
    echo "role_remote_before=$(meta_value "${ROLE_REMOTE_BEFORE:-}")"
    echo "transition_receipt_sha256=$(meta_value "${FACTORY_TRANSITION_RECEIPT_SHA256:-}")"
    echo "output_sha256=$(meta_value "$RUN_OUTPUT_SHA256")"
    echo "progress_events=$(meta_value "$PROGRESS_EVENTS")"
    echo "progress_journal_sha256=$(meta_value "$PROGRESS_JOURNAL_SHA256")"
    echo "timeout_kind=$(meta_value "$TIMEOUT_KIND")"
    echo "terminal_reason_code=$(meta_value "$TERMINAL_REASON_CODE")"
    echo "cancellation_reason=$(meta_value "$CANCELLATION_REASON")"
    echo "cancellation_preview_hash=$(meta_value "$CANCELLATION_PREVIEW_HASH")"
    echo "updated_at=$(date -u +%FT%TZ)"
  } | python3 "$KIT_DIR/scripts/lib/durable-file.py" write "$MANIFEST"; then
    return 1
  fi
  MANIFEST_PHASE="$phase"
}

finalize_accounting() {
  ACCOUNTING_STATE="$1"
  EFFECTIVE_COST="$2"
  TURNS="$3"
  EXIT_STATUS="$4"
  COST_BASIS="$5"
  TERMINAL_AT="$(date -u +%FT%TZ)"
  write_manifest "${6:-$ACCOUNTING_STATE}"
}

refresh_runtime_ledger() {
  python3 "$KIT_DIR/scripts/ledger-view.py" refresh \
    --factory-root "$REPO_ROOT" \
    --durable-ledger "$DURABLE_LEDGER" \
    --runtime-ledger "$LEDGER" \
    --runs-dir "$RUNS_DIR" >/dev/null
}

finalize_global_ledger() {
  local acquired=0
  [[ -n "$GLOBAL_LEDGER" && -f "$GLOBAL_LEDGER" ]] || return 0
  [[ -n "$GLOBAL_LEDGER_SNAPSHOT" ]] || return 0
  if [[ "$HELD_GLOBAL_LOCK" -ne 1 ]]; then
    if [[ -n "$GLOBAL_LEDGER_SNAPSHOT" ]]; then
      echo "WARNING: global ledger lock was lost; conservative reservation retained for operator reconciliation" >&2
      return 0
    fi
    for _global_try in $(seq 1 50); do
      if mkdir "$GLOBAL_LOCK" 2>/dev/null; then
        HELD_GLOBAL_LOCK=1
        GLOBAL_LOCK_TOKEN="$(python3 -c 'import secrets; print(secrets.token_hex(16))')"
        printf '%s\n' "$GLOBAL_LOCK_TOKEN" > "$GLOBAL_LOCK/owner"
        acquired=1
        break
      fi
      sleep 0.2
    done
  fi
  if [[ "$HELD_GLOBAL_LOCK" -ne 1 ]]; then
    echo "WARNING: global ledger lock stuck while finalizing run $RUN_ID — conservative reservation retained" >&2
    return 0
  fi
  if ! restore_global_if_changed; then
    echo "WARNING: global ledger or lock ownership changed; conservative reservation retained for operator reconciliation" >&2
    return 0
  fi
  if ! {
    awk -F, -v reserved="reserved-$RUN_ID" '$10 != reserved' "$GLOBAL_LEDGER"
    echo "$TODAY,$RUN_START_TIME,$REPO_ROOT,$TICKET,$ROLE,$ADAPTER,$PROMPT_VERSION,${TURNS:-0},$EFFECTIVE_COST,$EXIT_STATUS,$RUN_ID,$LEDGER_FAMILY,$LEDGER_MODEL,$LEDGER_REASON,$COST_BASIS,$LEDGER_VERSION"
  } | python3 "$KIT_DIR/scripts/lib/durable-file.py" write "$GLOBAL_LEDGER" ||
     ! validate_global_ledger "$GLOBAL_LEDGER"; then
    echo "WARNING: global ledger terminalization failed; lock retained for operator reconciliation" >&2
    return 0
  fi
  GLOBAL_LEDGER_SNAPSHOT="$(snapshot_global_ledger "$GLOBAL_LEDGER")"
  release_global_lock ||
    echo "WARNING: global ledger lock ownership changed during release; operator reconciliation required" >&2
  : "$acquired"
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

stop_before_adapter_gate() {
  if [[ -e "$CANCEL_REQUEST_FILE" || -L "$CANCEL_REQUEST_FILE" ]]; then
    ADAPTER_BOUNDARY_STOP_PATH="factory/runs/$RUN_ID.cancel-request.json"
    if load_cancellation_request; then
      echo "targeted cancellation requested before adapter gate; no task was submitted" >&2
      STATUS=130
    else
      echo "malformed targeted cancellation request before adapter gate; no task was submitted" >&2
      STATUS=11
    fi
  elif [[ -f "$FACTORY_DIR/KILL" ]]; then
    ADAPTER_BOUNDARY_STOP_PATH="factory/KILL"
    echo "KILL file appeared before adapter gate; no task was submitted" >&2
    STATUS=4
  elif [[ -f "$FACTORY_DIR/MAINTENANCE" ]]; then
    ADAPTER_BOUNDARY_STOP_PATH="factory/MAINTENANCE"
    echo "MAINTENANCE file appeared before adapter gate; no task was submitted" >&2
    STATUS=4
  else
    return 1
  fi
  ADAPTER_BOUNDARY_STOPPED=1
  terminate_run_group
  wait "$RUN_PID" 2>/dev/null
  return 0
}

start_lease_heartbeat() {
  local interval=300
  [[ -n "$DISPATCH_LEASE_ID" ]] || return 0
  [[ -z "$LEASE_HEARTBEAT_PID" ]] || return 0
  if [[ "${FACTORY_TEST_MODE:-0}" == 1 &&
        "${FACTORY_TRUSTED_TEST_HARNESS:-0}" == 1 &&
        "${FACTORY_TEST_LEASE_HEARTBEAT_SECONDS:-}" =~ ^[1-9][0-9]*$ &&
        "${FACTORY_TEST_LEASE_HEARTBEAT_SECONDS}" -le 300 ]]; then
    interval="$FACTORY_TEST_LEASE_HEARTBEAT_SECONDS"
  fi
  python3 "$KIT_DIR/scripts/dispatch-lease-heartbeat.py" \
    --renew-script "$KIT_DIR/scripts/dispatch-lease.sh" \
    --factory-root "$REPO_ROOT" --ticket "$TICKET" \
    --lease "$DISPATCH_LEASE_ID" --interval "$interval" &
  LEASE_HEARTBEAT_PID=$!
}

stop_lease_heartbeat() {
  local status=0
  [[ -n "$LEASE_HEARTBEAT_PID" ]] || return 0
  kill -TERM "$LEASE_HEARTBEAT_PID" 2>/dev/null || true
  wait "$LEASE_HEARTBEAT_PID" 2>/dev/null || status=$?
  LEASE_HEARTBEAT_PID=""
  if [[ "$status" -ne 0 ]]; then
    LEASE_HEARTBEAT_FAILED=1
    return 1
  fi
}

verify_control_interval_integrity() {
  local registered_status_after
  if [[ "$CLI_CONCURRENT_RUN" -eq 1 ]]; then
    if ! printf '%s' "$RUNS_META_SNAPSHOT" | python3 \
        "$KIT_DIR/scripts/lib/runs-integrity.py" check-concurrent \
        "$RUNS_DIR" "$ACTIVE_RUNS_DIR" \
        "$KIT_DIR/scripts/provider-coordinator.py" "$FACTORY_PROVIDER_DB"; then
      CONTROL_PLANE_MUTATION=1
      STATUS=11
    fi
  elif ! printf '%s' "$RUNS_META_SNAPSHOT" | \
      python3 "$KIT_DIR/scripts/lib/runs-integrity.py" check "$RUNS_DIR"; then
    CONTROL_PLANE_MUTATION=1
    STATUS=11
  fi
  if [[ "$(active_claim_snapshot 2>/dev/null || true)" != "$ACTIVE_RUN_SNAPSHOT" ]]; then
    echo "role_exit_control_plane_mutation: run claim changed during provider execution" >&2
    CONTROL_PLANE_MUTATION=1
    STATUS=11
  fi
  if [[ "$PARALLEL_PROVIDER_RUN" -eq 0 ]] && ! provider_lock_is_owned; then
    echo "role_exit_control_plane_mutation: provider lock changed during provider execution" >&2
    CONTROL_PLANE_MUTATION=1
    STATUS=11
  fi
  GLOBAL_STATE_MUTATED=0
  if [[ -n "$GLOBAL_LEDGER_SNAPSHOT" ]] &&
     { ! restore_global_if_changed || [[ "$GLOBAL_STATE_MUTATED" -eq 1 ]]; }; then
    echo "role_exit_control_plane_mutation: global ledger or lock changed during provider execution" >&2
    CONTROL_PLANE_MUTATION=1
    STATUS=11
  fi
  registered_status_after="$(registered_status_after_run 2>/dev/null || true)"
  if [[ "$(registered_ref_identity 2>/dev/null || true)" != "$REGISTERED_BRANCH_BEFORE" ||
        "$("$FACTORY_TRUSTED_GIT_BIN" -C "$REPO_ROOT" rev-parse HEAD 2>/dev/null || true)" != "$REGISTERED_HEAD_BEFORE" ||
        "$registered_status_after" != "$REGISTERED_STATUS_BEFORE" ||
        "$(registered_tracked_content 2>/dev/null || true)" != "$REGISTERED_CONTENT_BEFORE" ]]; then
    if [[ "${FACTORY_TEST_MODE:-0}" == "1" ]]; then
      printf 'test checkout status before=%q after=%q trigger=%q\n' \
        "$REGISTERED_STATUS_BEFORE" "$registered_status_after" \
        "$ADAPTER_BOUNDARY_STOP_PATH" >&2
    fi
    echo "role_exit_control_plane_mutation: registered checkout changed during provider execution" >&2
    CONTROL_PLANE_MUTATION=1
    STATUS=11
  fi
}

role_remote_head() {
  local attempt output
  for attempt in 1 2; do
    if output="$("$FACTORY_TRUSTED_GIT_BIN" -C "$WORKDIR" ls-remote --heads -- \
        "$PRODUCT_REMOTE" "refs/heads/$ROLE_BRANCH_BEFORE" 2>/dev/null)"; then
      printf '%s\n' "$output" | awk 'NR==1 {print $1; exit}'
      return 0
    fi
  done
  return 1
}

quarantine_rewritten_role_history() {
  local diagnostic_ref="refs/factory/failed-role/$TICKET/$RUN_ID"
  local existing="" current_branch current_head remote_head
  [[ "$ROLE" != "test-author" && "$ROLE" != "reviewer" ]] || return 1
  [[ "$RUN_ID" =~ ^[A-Za-z0-9._-]+$ ]] || return 1
  current_branch="$("$FACTORY_TRUSTED_GIT_BIN" -C "$WORKDIR" \
    symbolic-ref --quiet --short HEAD 2>/dev/null || true)"
  current_head="$("$FACTORY_TRUSTED_GIT_BIN" -C "$WORKDIR" \
    rev-parse HEAD 2>/dev/null || true)"
  remote_head="$(role_remote_head || true)"
  [[ "$current_branch" == "$ROLE_BRANCH_BEFORE" &&
     "$current_head" == "$ROLE_HEAD_AFTER" &&
     "$remote_head" == "$ROLE_REMOTE_BEFORE" &&
     -z "$("$FACTORY_TRUSTED_GIT_BIN" -C "$WORKDIR" \
       status --porcelain --untracked-files=all)" ]] || return 1
  if "$FACTORY_TRUSTED_GIT_BIN" -C "$WORKDIR" show-ref --verify --quiet \
      "$diagnostic_ref"; then
    existing="$("$FACTORY_TRUSTED_GIT_BIN" -C "$WORKDIR" \
      rev-parse "$diagnostic_ref" 2>/dev/null || true)"
    [[ "$existing" == "$ROLE_HEAD_AFTER" ]] || return 1
  else
    "$FACTORY_TRUSTED_GIT_BIN" -C "$WORKDIR" update-ref \
      "$diagnostic_ref" "$ROLE_HEAD_AFTER" \
      0000000000000000000000000000000000000000 || return 1
  fi
  "$FACTORY_TRUSTED_GIT_BIN" -C "$WORKDIR" update-ref \
    "refs/heads/$ROLE_BRANCH_BEFORE" "$ROLE_HEAD_BEFORE" \
    "$ROLE_HEAD_AFTER" || return 1
  "$FACTORY_TRUSTED_GIT_BIN" -C "$WORKDIR" restore \
    --source="$ROLE_HEAD_BEFORE" --staged --worktree -- . || return 1
  [[ "$("$FACTORY_TRUSTED_GIT_BIN" -C "$WORKDIR" \
       symbolic-ref --quiet --short HEAD 2>/dev/null || true)" == \
       "$ROLE_BRANCH_BEFORE" &&
     "$("$FACTORY_TRUSTED_GIT_BIN" -C "$WORKDIR" \
       rev-parse HEAD 2>/dev/null || true)" == "$ROLE_HEAD_BEFORE" &&
     "$(role_remote_head || true)" == "$ROLE_REMOTE_BEFORE" &&
     -z "$("$FACTORY_TRUSTED_GIT_BIN" -C "$WORKDIR" \
       status --porcelain --untracked-files=all)" &&
     "$("$FACTORY_TRUSTED_GIT_BIN" -C "$WORKDIR" \
       rev-parse "$diagnostic_ref" 2>/dev/null || true)" == \
       "$ROLE_HEAD_AFTER" ]]
}

ticket_evidence_snapshot() {
  python3 - "$1" <<'PY'
import json
import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
if not path.is_file():
    raise SystemExit(1)
names = (
    "State", "Operator-Approval", "Priority", "Initiative", "Kit-SHA",
    "Resume-State"
)
snapshot = {
    "fields": {name: [] for name in names},
    "authorizations": [],
    "reviewer_verdicts": [],
    "reviewer_voids": [],
    "spec_lint": [],
}
patterns = {}
for name in names:
    prefix = r"\s*" if name == "Kit-SHA" else ""
    patterns[name] = re.compile(
        rf"^{prefix}{re.escape(name)}:\s*.*$", re.IGNORECASE
    )
authorization = re.compile(
    r"^\s*OPERATOR AUTHORIZATION:\s*(?:spec-linter|reviewer) round\s*\d+\s*$",
    re.IGNORECASE,
)
reviewer_verdict = re.compile(
    r"^\s*reviewer round\s+\d+(?::\s*(?:APPROVE|REQUEST CHANGES(?:\s+—\s+.*)?)"
    r"|\s+FIX-OWNER:\s*(?:builder|test-author|both))\s*$",
    re.IGNORECASE,
)
reviewer_void = re.compile(
    r"^\s*OPERATOR NOTE:\s*reviewer run\s+\d+\s+void[^a-z0-9]*duplicate\s*$",
    re.IGNORECASE,
)
spec_lint = re.compile(
    r"^\s*SPEC-LINT:\s*(?:PASS|FAIL(?:\s+—\s+.*)?)\s*$",
    re.IGNORECASE,
)
for line in path.read_text().splitlines():
    for name, pattern in patterns.items():
        if pattern.fullmatch(line):
            snapshot["fields"][name].append(line)
            break
    if authorization.fullmatch(line):
        snapshot["authorizations"].append(line)
    if reviewer_verdict.fullmatch(line):
        snapshot["reviewer_verdicts"].append(line)
    if reviewer_void.fullmatch(line):
        snapshot["reviewer_voids"].append(line)
    if spec_lint.fullmatch(line):
        snapshot["spec_lint"].append(line)
print(json.dumps(snapshot, sort_keys=True, separators=(",", ":")))
PY
}

ticket_evidence_is_legal() {
  python3 - "$1" "$2" "$3" <<'PY'
import json
import sys

before, after = (json.loads(value) for value in sys.argv[1:3])
role = sys.argv[3]
for key in ("fields", "authorizations", "reviewer_verdicts", "reviewer_voids"):
    if before[key] != after[key]:
        raise SystemExit(1)
if role == "spec-linter":
    if (len(after["spec_lint"]) != len(before["spec_lint"]) + 1 or
            after["spec_lint"][:-1] != before["spec_lint"]):
        raise SystemExit(1)
elif before["spec_lint"] != after["spec_lint"]:
    raise SystemExit(1)
PY
}

normalize_role_ticket_mode() {
  local relative="factory/tickets/$TICKET.md" committed_mode
  committed_mode="$("$FACTORY_TRUSTED_GIT_BIN" -C "$WORKDIR" \
    ls-tree "$ROLE_HEAD_AFTER" -- "$relative" 2>/dev/null | \
    awk 'NR == 1 { print $1; exit }')"
  [[ "$committed_mode" == "100644" ]] || return 1
  python3 - "$WORKDIR" "$relative" <<'PY'
import os
import stat
import sys

root, relative = sys.argv[1:]
descriptors = []
try:
    current = os.open(
        root,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    descriptors.append(current)
    parts = relative.split("/")
    for part in parts[:-1]:
        current = os.open(
            part,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) |
            getattr(os, "O_NOFOLLOW", 0),
            dir_fd=current,
        )
        descriptors.append(current)
    descriptor = os.open(
        parts[-1], os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=current
    )
    descriptors.append(descriptor)
    before = os.fstat(descriptor)
    mode = stat.S_IMODE(before.st_mode)
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_uid != os.geteuid()
        or before.st_nlink != 1
        or mode not in {0o600, 0o644}
    ):
        raise SystemExit(1)
    if mode == 0o600:
        os.fchmod(descriptor, 0o644)
    after = os.fstat(descriptor)
    current_path = os.stat(parts[-1], dir_fd=current, follow_symlinks=False)
    if (
        (after.st_dev, after.st_ino, after.st_uid, after.st_nlink) !=
        (before.st_dev, before.st_ino, before.st_uid, before.st_nlink)
        or (current_path.st_dev, current_path.st_ino) !=
        (after.st_dev, after.st_ino)
        or not stat.S_ISREG(after.st_mode)
        or stat.S_IMODE(after.st_mode) != 0o644
    ):
        raise SystemExit(1)
except OSError:
    raise SystemExit(1)
finally:
    for descriptor in reversed(descriptors):
        os.close(descriptor)
PY
}

reconcile_cli_attempt() {
  [[ "$CLI_ATTEMPT_ACTIVE" -eq 1 && -n "$CLI_ATTEMPT_ID" ]] || return 0
  local output parsed state version result charge
  output="$(python3 "$KIT_DIR/scripts/provider-coordinator.py" \
    --db "$FACTORY_PROVIDER_DB" status --attempt-id "$CLI_ATTEMPT_ID" 2>/dev/null)" || return 1
  parsed="$(printf '%s' "$output" | python3 -c '
import json, sys
attempts = json.load(sys.stdin).get("attempts", [])
if len(attempts) != 1:
    raise SystemExit(1)
print(attempts[0]["state"])
print(attempts[0]["version"])
')" || return 1
  state="${parsed%%$'\n'*}"
  version="${parsed#*$'\n'}"
  if [[ "$state" == "terminal" ]]; then
    CLI_ATTEMPT_ACTIVE=0
    return 0
  fi
  case "$state" in
    prepared|reserved)
      [[ "${1:-failed}" != cancelled ]] || result=cancelled
      result="${result:-failed_pre_go}"; charge=0
      ;;
    GO|submitted)
      case "${1:-failed}" in
        succeeded|cancelled|failed) result="${1:-failed}" ;;
        *) result="failed" ;;
      esac
      charge="${PROVIDER_BUDGET_MICRO_VALUES[0]}"
      ;;
    *) return 1 ;;
  esac
  python3 "$KIT_DIR/scripts/provider-coordinator.py" \
    --db "$FACTORY_PROVIDER_DB" terminalize \
    --operation-id "$CLI_ATTEMPT_ID-host-terminal-$version" \
    --attempt-id "$CLI_ATTEMPT_ID" --expected-version "$version" \
    --result "$result" --charge-micro-usd "$charge" >/dev/null || return 1
  CLI_ATTEMPT_ACTIVE=0
}

release_cursor_account_lease() {
  [[ "$CURSOR_ACCOUNT_LEASE_ACTIVE" -eq 1 ]] || return 0
  local output
  output="$(python3 "$KIT_DIR/scripts/provider-coordinator.py" \
    --db "$FACTORY_PROVIDER_DB" \
    --account-db "$FACTORY_CURSOR_ACCOUNT_DB" account-release \
    --lease-id "$CURSOR_ACCOUNT_LEASE_ID" \
    --owner-pid "$CURSOR_ACCOUNT_OWNER_PID" \
    --owner-pgid "$CURSOR_ACCOUNT_OWNER_PGID" \
    --owner-start "$CURSOR_ACCOUNT_OWNER_START" 2>/dev/null)" || return 1
  printf '%s' "$output" | python3 -c '
import json, sys
raise SystemExit(0 if json.load(sys.stdin).get("released") is True else 1)
' || return 1
  CURSOR_ACCOUNT_LEASE_ACTIVE=0
}

bind_cursor_account_runtime() {
  [[ "$CURSOR_ACCOUNT_LEASE_ACTIVE" -eq 1 ]] || return 0
  local output
  output="$(python3 "$KIT_DIR/scripts/provider-coordinator.py" \
    --db "$FACTORY_PROVIDER_DB" \
    --account-db "$FACTORY_CURSOR_ACCOUNT_DB" account-bind-runtime \
    --lease-id "$CURSOR_ACCOUNT_LEASE_ID" \
    --owner-pid "$CURSOR_ACCOUNT_OWNER_PID" \
    --owner-pgid "$CURSOR_ACCOUNT_OWNER_PGID" \
    --owner-start "$CURSOR_ACCOUNT_OWNER_START" \
    --runtime-pid "$RUN_PID" --runtime-pgid "$RUN_PGID" \
    --runtime-start "$RUN_START_ID" 2>/dev/null)" || return 1
  printf '%s' "$output" | python3 -c '
import json, sys
raise SystemExit(0 if json.load(sys.stdin).get("bound") is True else 1)
'
}

copy_cli_credential() {
  python3 - "$1" "$2" <<'PY'
import os
import pathlib
import stat
import sys

source, destination = map(pathlib.Path, sys.argv[1:])
info = source.lstat()
if (
    source.is_symlink()
    or not stat.S_ISREG(info.st_mode)
    or info.st_uid != os.geteuid()
    or info.st_nlink != 1
    or stat.S_IMODE(info.st_mode) & 0o077
    or info.st_size > 1_000_000
):
    raise SystemExit(1)
source_fd = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
try:
    opened = os.fstat(source_fd)
    if (
        (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino)
        or not stat.S_ISREG(opened.st_mode)
        or opened.st_nlink != 1
    ):
        raise SystemExit(1)
    data = os.read(source_fd, 1_000_001)
finally:
    os.close(source_fd)
if len(data) > 1_000_000:
    raise SystemExit(1)
destination_fd = os.open(
    destination,
    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
    0o600,
)
with os.fdopen(destination_fd, "wb") as stream:
    stream.write(data)
    stream.flush()
    os.fsync(stream.fileno())
PY
}

prepare_cli_runtime() {
  local runtime_state_root="${CLI_RUNTIME_STATE_ROOT:-${DEVELOPMENT_LANE_ROOT:-}}"
  local runtime_layout="${CLI_RUNTIME_LAYOUT:-lane}"
  [[ "$CLI_CONCURRENT_RUN" -eq 1 ]] || return 0
  case "$ADAPTER" in
    claude-code|codex|cursor-openai|cursor-anthropic) ;;
    *) return 0 ;;
  esac
  [[ -n "$runtime_state_root" &&
     "$CLI_ATTEMPT_ID" =~ ^[A-Za-z0-9._-]+$ ]] || {
    TERMINAL_REASON_CODE="provider_attempt_isolation_unavailable"
    echo "subscription CLI isolation requires an owner-local attempt root" >&2
    return 1
  }
  local base="$runtime_state_root/attempts"
  if [[ "$runtime_layout" == "lane" ]]; then
    base="$runtime_state_root/runtime/cli-attempts"
  fi
  if [[ "$ADAPTER" == cursor-* ]]; then
    if [[ "$runtime_layout" == "lane" ]]; then
      base="$DEVELOPMENT_LANE_ROOT/c"
    else
      base="$runtime_state_root/c"
    fi
  fi
  mkdir -p "$base"
  chmod 700 "$base"
  CLI_RUNTIME_ROOT="$base/$CLI_ATTEMPT_ID"
  mkdir -m 700 "$CLI_RUNTIME_ROOT" || {
    TERMINAL_REASON_CODE="provider_attempt_runtime_collision"
    echo "subscription CLI attempt runtime already exists" >&2
    return 1
  }
  mkdir -m 700 \
    "$CLI_RUNTIME_ROOT/cache" \
    "$CLI_RUNTIME_ROOT/home" \
    "$CLI_RUNTIME_ROOT/output" \
    "$CLI_RUNTIME_ROOT/tmp"
  printf '%s\n' "$CLI_ATTEMPT_ID" >"$CLI_RUNTIME_ROOT/owner"
  CLI_PROVIDER_HOME="$CLI_RUNTIME_ROOT/home"
  CLI_PROVIDER_TMPDIR="$CLI_RUNTIME_ROOT/tmp"
  CLI_PROVIDER_CACHE_DIR="$CLI_RUNTIME_ROOT/cache"
  CLI_PROVIDER_OUTPUT_DIR="$CLI_RUNTIME_ROOT/output"
  if [[ "$ADAPTER" == claude-code ]]; then
    local credential_reason
    mkdir -m 700 "$CLI_RUNTIME_ROOT/config"
    if ! credential_reason="$(factory_prepare_claude_config \
        "$HOME/.claude" "$CLI_RUNTIME_ROOT/config")"; then
      if [[ "$credential_reason" == "claude_credential_missing" ||
            "$credential_reason" == "claude_credential_unreadable" ]]; then
        TERMINAL_REASON_CODE="claude_credential_unavailable"
        echo "Claude subscription credential is unavailable" >&2
      else
        TERMINAL_REASON_CODE="claude_credential_unsafe"
        echo "Claude subscription credential is unsafe" >&2
      fi
      return 1
    fi
    CLI_CLAUDE_CONFIG_DIR="$CLI_RUNTIME_ROOT/config"
    CLI_CLAUDE_SETTINGS="$CLI_RUNTIME_ROOT/settings.json"
    if [[ -n "${FACTORY_CLAUDE_SETTINGS:-}" ]]; then
      copy_cli_credential "$FACTORY_CLAUDE_SETTINGS" \
        "$CLI_CLAUDE_SETTINGS" || {
          TERMINAL_REASON_CODE="claude_settings_unsafe"
          echo "Claude settings are unsafe" >&2
          return 1
        }
    else
      printf '%s\n' '{"sandbox":{"enabled":false}}' >"$CLI_CLAUDE_SETTINGS"
      chmod 600 "$CLI_CLAUDE_SETTINGS"
    fi
  elif [[ "$ADAPTER" == codex ]]; then
    local source="$HOME/.codex/auth.json"
    [[ -f "$source" && ! -L "$source" ]] || {
      TERMINAL_REASON_CODE="codex_credential_unavailable"
      echo "Codex subscription credential is unavailable" >&2
      return 1
    }
    mkdir -m 700 "$CLI_PROVIDER_HOME/.codex"
    copy_cli_credential "$source" "$CLI_PROVIDER_HOME/.codex/auth.json" || {
      TERMINAL_REASON_CODE="codex_credential_unsafe"
      echo "Codex subscription credential is unsafe" >&2
      return 1
    }
  else
    local cursor_source="${FACTORY_CURSOR_SESSION_HOME:-$HOME}/.cursor"
    [[ -f "$cursor_source/auth.json" && ! -L "$cursor_source/auth.json" &&
       -f "$cursor_source/cli-config.json" &&
       ! -L "$cursor_source/cli-config.json" ]] || {
      TERMINAL_REASON_CODE="cursor_credential_unavailable"
      echo "Cursor subscription credential is unavailable" >&2
      return 1
    }
    mkdir -m 700 "$CLI_PROVIDER_HOME/.cursor" "$CLI_RUNTIME_ROOT/data"
    copy_cli_credential "$cursor_source/auth.json" \
      "$CLI_PROVIDER_HOME/.cursor/auth.json" &&
      copy_cli_credential "$cursor_source/cli-config.json" \
        "$CLI_PROVIDER_HOME/.cursor/cli-config.json" || {
          TERMINAL_REASON_CODE="cursor_credential_unsafe"
          echo "Cursor subscription credential is unsafe" >&2
          return 1
        }
    CLI_CURSOR_CONFIG_DIR="$CLI_PROVIDER_HOME/.cursor"
    CLI_CURSOR_DATA_DIR="$CLI_RUNTIME_ROOT/data"
    [[ "${#CLI_CURSOR_DATA_DIR}" -le 75 ]] || {
      TERMINAL_REASON_CODE="cursor_attempt_path_too_long"
      echo "Cursor attempt data path is too long for isolated scratch" >&2
      return 1
    }
  fi
  chmod 600 "$CLI_RUNTIME_ROOT/owner"
}

prepare_cli_login_shell() {
  [[ "$CLI_CONCURRENT_RUN" -eq 1 ]] || return 0
  [[ -n "${FACTORY_CERTIFIED_NODE_VERSION:-}${FACTORY_CERTIFIED_NPM_VERSION:-}" ]] ||
    return 0
  [[ "${FACTORY_CERTIFIED_NODE_VERSION:-}" =~ ^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$ &&
     "${FACTORY_CERTIFIED_NPM_VERSION:-}" =~ ^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$ &&
     "$TASK_PATH" == /* && "$TASK_PATH" != *$'\n'* && "$TASK_PATH" != *$'\r'* ]] || {
    TERMINAL_REASON_CODE="provider_task_runtime_invalid"
    echo "certified provider task runtime is invalid" >&2
    return 1
  }
  local profile="$CLI_PROVIDER_HOME/.zlogin"
  [[ ! -e "$profile" && ! -L "$profile" ]] || {
    TERMINAL_REASON_CODE="provider_task_runtime_collision"
    echo "provider login-shell profile already exists" >&2
    return 1
  }
  {
    printf 'export PATH=%q\n' "$TASK_PATH"
    printf 'if [[ "$(node --version 2>/dev/null)" != %q || "$(npm --version 2>/dev/null)" != %q ]]; then\n' \
      "$FACTORY_CERTIFIED_NODE_VERSION" "$FACTORY_CERTIFIED_NPM_VERSION"
    printf "  print -u2 -- 'Factory product runtime mismatch; command refused'\n"
    printf '  exit 126\nfi\n'
  } >"$profile" || return 1
  chmod 600 "$profile"
}

cleanup_cli_runtime() {
  local runtime_state_root="${CLI_RUNTIME_STATE_ROOT:-${DEVELOPMENT_LANE_ROOT:-}}"
  local runtime_layout="${CLI_RUNTIME_LAYOUT:-lane}"
  [[ -n "$CLI_RUNTIME_ROOT" ]] || return 0
  [[ "$RUN_GROUP_TERMINATED" -eq 1 ]] || {
    echo "WARNING: retaining subscription CLI runtime because its process group survived" >&2
    return 0
  }
  python3 - "$CLI_RUNTIME_ROOT" "$runtime_state_root" "$CLI_ATTEMPT_ID" \
    "$ADAPTER" "$runtime_layout" <<'PY'
import os
import pathlib
import shutil
import sys

root = pathlib.Path(sys.argv[1])
lane = pathlib.Path(sys.argv[2])
attempt = sys.argv[3]
adapter = sys.argv[4]
layout = sys.argv[5]
expected_parent = lane / (
    "c" if adapter.startswith("cursor-")
    else "runtime/cli-attempts" if layout == "lane"
    else "attempts"
)
if (
    not root.is_absolute()
    or root.parent != expected_parent
    or root.name != attempt
    or root.is_symlink()
    or not root.is_dir()
    or (root / "owner").is_symlink()
    or (root / "owner").read_text(encoding="utf-8") != attempt + "\n"
    or root.stat().st_uid != os.geteuid()
):
    raise SystemExit(1)
shutil.rmtree(root)
PY
  CLI_RUNTIME_ROOT=""
}

release_active_run_claim() {
  [[ "$OWNS_ACTIVE_RUN" -eq 1 ]] || return 0
  if [[ -d "$ACTIVE_RUN_FILE" && ! -L "$ACTIVE_RUN_FILE" &&
        -f "$ACTIVE_RUN_FILE/owner" && ! -L "$ACTIVE_RUN_FILE/owner" &&
        "$(cat "$ACTIVE_RUN_FILE/owner" 2>/dev/null)" == "$ACTIVE_RUN_EXPECTED" ]]; then
    rm -f "$ACTIVE_RUN_FILE/owner"
    rmdir "$ACTIVE_RUN_FILE" 2>/dev/null || return 1
    OWNS_ACTIVE_RUN=0
    return 0
  fi
  return 1
}

cleanup() {
  local status=$? accounting_finalized=0
  stop_lease_heartbeat || true
  terminate_run_group || true
  if [[ "$RUN_GROUP_TERMINATED" -eq 1 ]]; then
    release_cursor_account_lease ||
      echo "WARNING: Cursor account admission lease retained for operator reconciliation" >&2
    reconcile_cli_attempt "$([[ "$status" -eq 130 || "$status" -eq 143 ]] && printf cancelled || printf failed)" ||
      echo "WARNING: CLI provider reservation retained for operator reconciliation" >&2
  elif [[ "$CLI_ATTEMPT_ACTIVE" -eq 1 ]]; then
    echo "WARNING: CLI provider reservation retained because its process group survived" >&2
  fi
  cleanup_cli_runtime ||
    echo "WARNING: subscription CLI runtime retained for operator reconciliation" >&2
  if [[ -n "$ROLE_GUARD_ROOT" && -d "$ROLE_GUARD_ROOT" ]]; then
    rm -f "$ROLE_GUARD_ROOT"/npm "$ROLE_GUARD_ROOT"/npx \
      "$ROLE_GUARD_ROOT"/pnpm "$ROLE_GUARD_ROOT"/yarn \
      "$ROLE_GUARD_ROOT"/corepack
    rmdir "$ROLE_GUARD_ROOT" 2>/dev/null || true
  fi
  if [[ -n "$RUN_PID_FILE" ]]; then
    if [[ "$RUN_GROUP_TERMINATED" -eq 1 ]]; then
      rm -f "$RUN_PID_FILE"
    else
      echo "WARNING: retaining $RUN_PID_FILE because the process group survived cleanup" >&2
    fi
  fi
  [[ -z "$RUN_READY_FILE" ]] || rm -f "$RUN_READY_FILE"
  [[ -z "$RUN_GO_FILE" ]] || rm -f "$RUN_GO_FILE"
  [[ -z "$RUN_GATE_FILE" ]] || rm -f "$RUN_GATE_FILE"
  [[ -z "$RUN_SUBMITTED_FILE" ]] || rm -f "$RUN_SUBMITTED_FILE"
  [[ -z "$RUN_OUTPUT_TEMP" ]] || rm -f "$RUN_OUTPUT_TEMP"
  exec 8<&- 9>&- 2>/dev/null || true
  if [[ "$CLI_ATTEMPT_ACTIVE" -eq 1 && "$RUN_GROUP_TERMINATED" -ne 1 ]]; then
    echo "WARNING: run accounting retained because its CLI process group survived" >&2
  elif [[ -n "$MANIFEST" && "$ACCOUNTING_STATE" == "reserved" ]]; then
    [[ "$status" -ne 0 ]] || status=125
    if [[ "$GO_ISSUED" -eq 1 ]]; then
      finalize_accounting "abandoned_conservative" "$RESERVED_USD" "${TURNS:-0}" "$status" "conservative_reservation" "abandoned"
    else
      finalize_accounting "launch_void" "0" "0" "$status" "launch_void" "abandoned"
    fi
    accounting_finalized=1
  elif [[ -n "$MANIFEST" && -z "$ACCOUNTING_STATE" && "$MANIFEST_PHASE" != "abandoned" ]]; then
    [[ "$status" -ne 0 ]] || status=125
    ACCOUNTING_SCHEMA=1
    finalize_accounting "launch_void" "0" "0" "$status" "launch_void" "abandoned"
    accounting_finalized=1
  fi
  if [[ "$accounting_finalized" -eq 1 ]]; then
    if [[ "$HELD_LEDGER_LOCK" -eq 0 ]]; then
      for _cleanup_try in $(seq 1 50); do
        if mkdir "$LOCK_DIR" 2>/dev/null; then
          HELD_LEDGER_LOCK=1
          break
        fi
        sleep 0.2
      done
    fi
    if [[ "$HELD_LEDGER_LOCK" -eq 1 ]]; then
      refresh_runtime_ledger ||
        echo "WARNING: cleanup could not refresh runtime accounting; terminal manifest remains authoritative" >&2
    else
      echo "WARNING: cleanup could not lock runtime accounting; terminal manifest remains authoritative" >&2
    fi
    finalize_global_ledger || true
  fi
  if [[ "$HELD_GLOBAL_LOCK" -eq 1 ]]; then
    if [[ -z "$GLOBAL_LEDGER_SNAPSHOT" ]]; then
      release_global_lock ||
        echo "WARNING: global lock ownership changed; it was not removed" >&2
    else
      echo "WARNING: global ledger lock retained for operator reconciliation" >&2
    fi
  fi
  [[ "$HELD_LEDGER_LOCK" -eq 0 ]] || rmdir "$LOCK_DIR" 2>/dev/null || true
  if [[ "$LEGACY_INTERVAL_ACTIVE" -eq 1 ]]; then
    release_legacy_interval ||
      echo "WARNING: legacy provider interval retained for reconciliation" >&2
  fi
  if [[ "$HELD_PROVIDER_LOCK" -eq 1 && "$RETAIN_PROVIDER_LOCK" -eq 0 ]]; then
    release_provider_lock ||
      echo "WARNING: provider lock ownership changed; operator reconciliation required" >&2
  elif [[ "$HELD_PROVIDER_LOCK" -eq 1 ]]; then
    echo "WARNING: provider lock retained until cancellation accounting is reconciled" >&2
  fi
  [[ "$HELD_LAUNCH_LOCK" -eq 0 ]] || rmdir "$LAUNCH_LOCK" 2>/dev/null || true
  if [[ "$OWNS_ACTIVE_RUN" -eq 1 ]]; then
    if [[ "$RUN_GROUP_TERMINATED" -ne 1 ]]; then
      echo "WARNING: run claim retained because its process group survived" >&2
    elif ! release_active_run_claim; then
      echo "WARNING: run claim ownership changed; successor state was not removed" >&2
    fi
  fi
}
trap cleanup EXIT
trap 'exit 143' TERM INT HUP

TICKET_FILE="$WORKDIR/factory/tickets/$TICKET.md"
ROLE_EXIT_ENFORCED=1
if [[ "${FACTORY_TEST_MODE:-0}" == "1" &&
      "${FACTORY_TEST_ENFORCE_ROLE_EXIT:-0}" != "1" ]]; then
  ROLE_EXIT_ENFORCED=0
fi
[[ -f "$ENV_FILE" ]] || { echo "envelope not found: $ENV_FILE — fill ENVELOPE.md and write ENVELOPE.env first" >&2; exit 3; }
unset PER_RUN_BUDGET_USD PER_TICKET_BUDGET_USD PER_RUN_MAX_TURNS \
  PER_RUN_TIMEOUT_MIN DAILY_CAP_USD
factory_load_plain_config "$ENV_FILE" envelope \
  "$FACTORY_ENVELOPE_CONFIG_KEYS" "$FACTORY_ENVELOPE_REQUIRED_KEYS" || exit 3
PER_TICKET_BUDGET_USD="${PER_TICKET_BUDGET_USD:-$PER_RUN_BUDGET_USD}"
factory_select_role_envelope "$ROLE" || exit 3

# --- optional machine-level cap across all factories on this machine ---
# ~/.factory/global.env defines GLOBAL_DAILY_CAP_USD; every run on the machine
# then also reserves against ~/.factory/global-ledger.csv, so N projects can't
# multiply the daily budget silently. Absent file = single-project behavior.
GLOBAL_ENV="${FACTORY_GLOBAL_ENV:-$HOME/.factory/global.env}"
if ! factory_validate_runtime_overrides; then
  echo "$FACTORY_RUNTIME_OVERRIDE_ERROR; no task was submitted" >&2
  exit 2
fi
factory_clear_plain_config_keys "$FACTORY_GLOBAL_CONFIG_KEYS"
GLOBAL_LEDGER="" GLOBAL_LOCK=""
if [[ -f "$GLOBAL_ENV" ]]; then
  factory_load_plain_config "$GLOBAL_ENV" global \
    "$FACTORY_GLOBAL_CONFIG_KEYS" "" 1 || exit 3
  GLOBAL_LEDGER="${GLOBAL_LEDGER:-$(dirname "$GLOBAL_ENV")/global-ledger.csv}"
  GLOBAL_LOCK="$(dirname "$GLOBAL_ENV")/.ledger.lock"
  [[ -n "${GLOBAL_DAILY_CAP_USD:-}" ]] || { echo "global env $GLOBAL_ENV exists but GLOBAL_DAILY_CAP_USD is unset" >&2; exit 3; }
fi
FACTORY_ENVELOPE_OVERRIDE_IDS=""
FACTORY_ENVELOPE_NEXT_OVERRIDE_IDS=""
export -n GLOBAL_LEDGER GLOBAL_DAILY_CAP_USD 2>/dev/null || true
# Product and machine configuration are not trusted to supply launcher-only
# authority, and adapters must never inherit it.
set +a
unset -f git 2>/dev/null || true
if declare -F git >/dev/null; then
  echo "Git shell function is forbidden" >&2
  exit 2
fi
unset FACTORY_CERTIFIED_PRODUCT_ORIGIN
export -n FACTORY_TRUSTED_PRODUCT_ORIGIN FACTORY_TRUSTED_GIT_BIN PRODUCT_REMOTE 2>/dev/null || true
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
[[ -f "$TICKET_FILE" ]] || { echo "ticket file missing from worktree: $TICKET_FILE" >&2; exit 3; }
if [[ "$ROLE_EXIT_ENFORCED" -eq 1 ]]; then
  PRODUCT_REMOTE="$(factory_capture_product_remote "$REPO_ROOT" "$FACTORY_TRUSTED_PRODUCT_ORIGIN")" || {
    echo "role_exit_remote_mismatch: certified product push destination validation failed" >&2
    exit 11
  }
  ROLE_BRANCH_BEFORE="$("$FACTORY_TRUSTED_GIT_BIN" -C "$WORKDIR" symbolic-ref --quiet --short HEAD 2>/dev/null || true)"
  ROLE_HEAD_BEFORE="$("$FACTORY_TRUSTED_GIT_BIN" -C "$WORKDIR" rev-parse HEAD 2>/dev/null || true)"
  [[ -n "$ROLE_BRANCH_BEFORE" && -n "$ROLE_HEAD_BEFORE" ]] || {
    echo "role_exit_wrong_branch: ticket worktree must be on a branch" >&2
    exit 11
  }
  [[ -z "$("$FACTORY_TRUSTED_GIT_BIN" -C "$WORKDIR" status --porcelain --untracked-files=all)" ]] || {
    echo "role_exit_dirty: ticket worktree must be clean before launch" >&2
    exit 11
  }
fi
if ! factory_validate_kit_pin "$KIT_DIR" "$REPO_ROOT"; then
  echo "$FACTORY_KIT_PIN_ERROR; no task was submitted" >&2
  exit 3
fi
PROVIDER_PRODUCT_ID="$(basename "$REPO_ROOT" | tr -c 'A-Za-z0-9._:@-' '_')"
if [[ -n "${FACTORY_PROVIDER_PRODUCT_ID:-}" ]]; then
  if [[ "${FACTORY_CLI_LANE_ROOT:-}" != /* ||
        ! -d "$FACTORY_CLI_LANE_ROOT" ||
        -L "$FACTORY_CLI_LANE_ROOT" ||
        "$(basename "$FACTORY_CLI_LANE_ROOT")" != nysa-sf-qualification.* ||
        ! -f "$FACTORY_CLI_LANE_ROOT/marker.json" ||
        -L "$FACTORY_CLI_LANE_ROOT/marker.json" ||
        "$FACTORY_PROVIDER_PRODUCT_ID" != "$TRANSITION_PROJECT:$FACTORY_KIT_SHA" ]]; then
    echo "qualification provider product identity is invalid; no task was submitted" >&2
    exit 3
  fi
  PROVIDER_PRODUCT_ID="$FACTORY_PROVIDER_PRODUCT_ID"
fi
unset FACTORY_PROVIDER_PRODUCT_ID
readonly PROVIDER_PRODUCT_ID
if ! factory_validate_ticket_kit_sha "$TICKET_FILE" "$FACTORY_KIT_SHA"; then
  echo "$FACTORY_TICKET_KIT_ERROR; no task was submitted" >&2
  exit 3
fi
TICKET_AFFINITY_WAS_MISSING=0
if ! factory_dispatch_require_lease "$REPO_ROOT" "$TICKET" "$DISPATCH_LEASE_ID"; then
  echo "$FACTORY_DISPATCH_LEASE_ERROR; no task was submitted" >&2
  exit 7
fi
# Keep the claim alive while route resolution, launch locking, and provider
# admission are queued. Later calls are idempotent and retain the same worker.
start_lease_heartbeat
# The first manifest phase still records the release affinity that will be
# persisted under the launch lock. factory_record_ticket_kit_sha revalidates
# and writes it before any reservation or task submission.
if [[ -z "${FACTORY_TICKET_KIT_SHA:-}" ]]; then
  TICKET_AFFINITY_WAS_MISSING=1
  FACTORY_TICKET_KIT_SHA="$FACTORY_KIT_SHA"
fi
ensure_runs_directory || {
  echo "run manifest directory could not be durably established" >&2
  exit 3
}
if [[ -z "${FACTORY_LEDGER:-}" ]] && ! refresh_runtime_ledger; then
  echo "effective ledger could not be reduced; no task was submitted" >&2
  exit 3
fi
if ! sequencer_allows_role; then
  echo "$SEQUENCER_ERROR; no task was submitted" >&2
  exit 10
fi

# --- resolve one backend before reservation and before submitting the task ---
# shellcheck disable=SC1091
source "$KIT_DIR/scripts/lib/backend-policy.sh"
ROUTE_PLAN="$WORKDIR/factory/route-plans/$TICKET.json"
SELECTED_ROUTE_ID=""
SELECTED_GATEWAY_ID=""
SELECTED_PROVIDER_ID=""
SELECTED_ACCOUNT_ROUTE_ID=""
SELECTED_TRANSPORT=""
SELECTED_POLICY_HASH=""
SELECTED_ROUTE_PLAN_SHA256=""
SELECTED_ROUTE_REVISION=""
SELECTED_ROUTE_REVISION_HASH=""
if [[ -n "${FACTORY_ADAPTER_OVERRIDE:-}" ]]; then
  if [[ "$FACTORY_ADAPTER_OVERRIDE" != "mock" || "${FACTORY_TEST_MODE:-0}" != "1" ]]; then
    echo "FACTORY_ADAPTER_OVERRIDE requires FACTORY_TEST_MODE=1 and the mock adapter" >&2
    exit 2
  fi
  SELECTED="$FACTORY_ADAPTER_OVERRIDE"
  SELECTED_FAMILY="$(factory_adapter_family "$SELECTED" 2>/dev/null || echo test)"
  SELECTED_MODEL="${FACTORY_OVERRIDE_MODEL:-test-mock-model}"
  SELECTED_VERSION="test"
  SELECTED_ROUTE_ID="test-mock-$TICKET"
  case "$TICKET" in
    *1|*3|*5|*7|*9) SELECTED_ACCOUNT_ROUTE_ID="test-mock-a" ;;
    *) SELECTED_ACCOUNT_ROUTE_ID="test-mock-b" ;;
  esac
  SELECTION_REASON="test_override"
  PRIMARY_PROBE_SUMMARY="test_override"
elif [[ -f "$ROUTE_PLAN" ]]; then
  if ! factory_select_pinned_model_role \
      "$ROUTE_PLAN" "$TICKET" "$FACTORY_KIT_SHA" "$ROLE"; then
    echo "invalid pinned route for role '$ROLE': ${FACTORY_RESOLVE_ERROR:-unknown}; no task was submitted" >&2
    exit 6
  fi
  if ! factory_verify_selected_pinned_route_ready; then
    echo "pinned route unavailable or drifted for role '$ROLE': ${FACTORY_RESOLVE_ERROR:-unknown}; no task was submitted" >&2
    exit 6
  fi
  SELECTED="$FACTORY_SELECTED_ADAPTER"
  SELECTED_FAMILY="$FACTORY_SELECTED_FAMILY"
  SELECTED_MODEL="$FACTORY_SELECTED_MODEL"
  SELECTED_EFFORT="$FACTORY_SELECTED_EFFORT"
  SELECTED_VERSION="$FACTORY_SELECTED_VERSION"
  SELECTED_ROUTE_ID="$FACTORY_SELECTED_ROUTE_ID"
  SELECTED_GATEWAY_ID="$FACTORY_SELECTED_GATEWAY_ID"
  SELECTED_PROVIDER_ID="$FACTORY_SELECTED_PROVIDER_ID"
  SELECTED_ACCOUNT_ROUTE_ID="$FACTORY_SELECTED_ACCOUNT_ROUTE_ID"
  SELECTED_TRANSPORT="$FACTORY_SELECTED_TRANSPORT"
  SELECTED_POLICY_HASH="$FACTORY_SELECTED_POLICY_HASH"
  SELECTED_ROUTE_PLAN_SHA256="$FACTORY_SELECTED_ROUTE_PLAN_SHA256"
  SELECTED_ROUTE_REVISION="$FACTORY_SELECTED_ROUTE_REVISION"
  SELECTED_ROUTE_REVISION_HASH="$FACTORY_SELECTED_ROUTE_REVISION_HASH"
  SELECTION_REASON="$FACTORY_SELECTION_REASON"
  PRIMARY_PROBE_SUMMARY="pinned:${PROBE_STATE}:${PROBE_REASON}"
elif ! factory_load_model_probe_context; then
  echo "model routing state is invalid: ${FACTORY_RESOLVE_ERROR:-unknown}; no task was submitted" >&2
  exit 6
elif [[ "$FACTORY_MODEL_PROFILE_ID" != "legacy-balanced-v1" ]]; then
  echo "active model profile '$FACTORY_MODEL_PROFILE_ID' requires a pinned ticket route plan; no task was submitted" >&2
  exit 6
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
ISOLATED_RUN=0
CLI_CONCURRENT_RUN=0
PARALLEL_PROVIDER_RUN=0
ISOLATED_PROTOCOL=""
ISOLATED_BROKER_PATH=""
ISOLATED_MODEL=""
ISOLATED_PROVIDER_FAMILY=""
ISOLATED_ACCOUNT_ROUTE=""
if [[ ( "$PROVIDER_CONTRACT_VERSION" == "1.6.0" ||
        "$PROVIDER_CONTRACT_VERSION" == "1.7.0" ||
        "$PROVIDER_CONTRACT_VERSION" == "1.8.0" ||
        "$PROVIDER_CONTRACT_VERSION" == "1.9.0" ) &&
      -n "${FACTORY_PROVIDER_ACTIVATION:-}" &&
      -f "${FACTORY_PROVIDER_ACTIVATION:-}" ]]; then
  ACTIVATION_ARGS=(--config "$FACTORY_PROVIDER_ACTIVATION" \
    --contract-version "$PROVIDER_CONTRACT_VERSION")
  [[ -z "${FACTORY_PROVIDER_POLICY:-}" ]] ||
    ACTIVATION_ARGS+=(--policy "$FACTORY_PROVIDER_POLICY")
  if ! ACTIVATION_OUTPUT="$(python3 "$KIT_DIR/scripts/provider-activation.py" \
      "${ACTIVATION_ARGS[@]}" --route-id "$SELECTED_ROUTE_ID" 2>/dev/null)"; then
    echo "isolated-v1 activation is invalid for the selected route" >&2
    exit 3
  fi
  if ! ACTIVATION_VALUES_OUTPUT="$(printf '%s' "$ACTIVATION_OUTPUT" | python3 -c '
import json, sys
value = json.load(sys.stdin)
if value.get("status") != "enabled":
    raise SystemExit(1)
fields = ("execution_mode", "protocol", "broker_path", "adapter", "model",
          "provider_family", "account_route", "policy_sha256")
for field in fields:
    selected = value.get(field, "api-isolated-v1" if field == "execution_mode" else "-")
    if not isinstance(selected, str) or "\n" in selected or selected == "":
        raise SystemExit(1)
    print(selected)
' 2>/dev/null)"; then
    echo "isolated-v1 activation returned invalid selection data" >&2
    exit 3
  fi
  ACTIVATION_VALUES=()
  while IFS= read -r activation_value; do
    ACTIVATION_VALUES+=("$activation_value")
  done <<< "$ACTIVATION_VALUES_OUTPUT"
  if [[ "${#ACTIVATION_VALUES[@]}" -eq 8 ]]; then
      EXECUTION_MODE="${ACTIVATION_VALUES[0]}"
      ISOLATED_PROTOCOL="${ACTIVATION_VALUES[1]}"
      ISOLATED_BROKER_PATH="${ACTIVATION_VALUES[2]}"
      ACTIVATED_ADAPTER="${ACTIVATION_VALUES[3]}"
      ISOLATED_MODEL="${ACTIVATION_VALUES[4]}"
      ISOLATED_PROVIDER_FAMILY="${ACTIVATION_VALUES[5]}"
      ISOLATED_ACCOUNT_ROUTE="${ACTIVATION_VALUES[6]}"
      ACTIVATED_POLICY_HASH="${ACTIVATION_VALUES[7]}"
      if [[ "$ISOLATED_MODEL" == "$SELECTED_MODEL" &&
            "$ISOLATED_PROVIDER_FAMILY" == "$SELECTED_FAMILY" &&
            "$ISOLATED_ACCOUNT_ROUTE" == "$SELECTED_ACCOUNT_ROUTE_ID" ]]; then
        if [[ "$EXECUTION_MODE" == "api-isolated-v1" &&
              "$ACTIVATED_ADAPTER" == - && "$ACTIVATED_POLICY_HASH" == - ]]; then
          [[ "$ROLE" == "reviewer" ]] || ISOLATED_RUN=1
        elif [[ "$EXECUTION_MODE" == "cli-concurrent-v1" &&
                ( "$PROVIDER_CONTRACT_VERSION" == "1.7.0" ||
                  "$PROVIDER_CONTRACT_VERSION" == "1.8.0" ||
                  "$PROVIDER_CONTRACT_VERSION" == "1.9.0" ) &&
                "$ACTIVATED_ADAPTER" == "$ADAPTER" &&
                "$ACTIVATED_POLICY_HASH" =~ ^[0-9a-f]{64}$ ]]; then
          CURRENT_POLICY_HASH="$(python3 - "$FACTORY_PROVIDER_POLICY" <<'PY'
import hashlib, json, sys
value=json.load(open(sys.argv[1], encoding="utf-8"))
raw=json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",",":"))
print(hashlib.sha256(raw.encode()).hexdigest())
PY
          )" || { echo "CLI concurrency policy is unavailable" >&2; exit 3; }
          [[ "$CURRENT_POLICY_HASH" == "$ACTIVATED_POLICY_HASH" ]] || {
            echo "CLI concurrency activation policy does not match" >&2
            exit 3
          }
          CLI_CONCURRENT_RUN=1
        else
          echo "provider activation execution mode is invalid" >&2
          exit 3
        fi
      else
        echo "isolated-v1 activation does not match the selected route identity" >&2
        exit 3
      fi
  else
    echo "isolated-v1 activation returned incomplete selection data" >&2
    exit 3
  fi
fi
if [[ "$ISOLATED_RUN" -eq 1 || "$CLI_CONCURRENT_RUN" -eq 1 ]]; then
  PARALLEL_PROVIDER_RUN=1
fi
if [[ ( "$PROVIDER_CONTRACT_VERSION" == "1.8.0" || "$PROVIDER_CONTRACT_VERSION" == "1.9.0" ) &&
      "$ADAPTER" =~ ^(claude-code|codex|cursor-openai|cursor-anthropic)$ ]]; then
  CONTRACT_CAPACITY="$(factory_dispatch_max_tickets \
    "$REPO_ROOT" "$PROVIDER_CONTRACT_VERSION" 2>/dev/null)" || {
      echo "ticket concurrency configuration is invalid; no task was submitted" >&2
      exit 3
    }
  if [[ "$CONTRACT_CAPACITY" -gt 1 ]]; then
    if [[ "${FACTORY_PROVIDER_POLICY:-}" != */provider-policy.json ]] ||
       [[ "${FACTORY_PROVIDER_CONFIGURATION_LOCK:-}" != \
          "$(dirname "$FACTORY_PROVIDER_POLICY")/provider-configuration.lock" ]] ||
       ! python3 "$KIT_DIR/scripts/provider-concurrency-config.py" \
         --release "$KIT_DIR" --root "$(dirname "$FACTORY_PROVIDER_POLICY")" \
         --capacity "$CONTRACT_CAPACITY" check \
         --activation "${FACTORY_PROVIDER_ACTIVATION:-}" \
         --cli-root "${FACTORY_CLI_RUNTIME_ROOT:-}" >/dev/null; then
      echo "Contract 1.8 multi-ticket provider concurrency is not ready; no task was submitted" >&2
      exit 3
    fi
    if [[ "$CLI_CONCURRENT_RUN" -ne 1 ]]; then
      echo "Contract 1.8 multi-ticket execution requires activated subscription CLI concurrency; no task was submitted" >&2
      exit 3
    fi
  fi
fi
if [[ "$ISOLATED_RUN" -eq 1 ]]; then
  PROVIDER_EXECUTION_MODE="api-isolated-v1"
elif [[ "$CLI_CONCURRENT_RUN" -eq 1 ]]; then
  PROVIDER_EXECUTION_MODE="cli-concurrent-v1"
fi
if [[ "$CLI_CONCURRENT_RUN" -eq 1 && "$ADAPTER" == cursor-* ]]; then
  [[ "${FACTORY_CURSOR_ACCOUNT_DB:-}" == /* &&
     "${FACTORY_KIT_TRUST_SCOPE:-}" =~ ^(production-certified|qualification-candidate)$ ]] || {
    echo "Cursor account admission authority is unavailable; no task was submitted" >&2
    exit 3
  }
fi

# Serialize claim creation with kill-switch publication. Claims are mkdir
# locks and are never reclaimed automatically; operator recovery must inspect
# an abandoned owner record rather than guessing from a reusable PID.
LOCK_ATTEMPTS=$((PER_RUN_TIMEOUT_MIN * 600 + 100))
for i in $(seq 1 "$LOCK_ATTEMPTS"); do
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
# Re-resolve under the launch lock. Envelope-control apply and override-apply
# take this same lock, so these are the exact values reserved and sent to the
# adapter rather than a stale pre-lock observation.
if ! load_effective_envelope; then
  echo "effective envelope or override records are unsafe; no task was submitted" >&2
  exit 3
fi
ACTIVE_RUNS_DIR="$LEDGER_DIR/.active-runs"
GUARD_KEY="$(printf '%s.%s' "$TICKET" "$ROLE" | tr -c 'A-Za-z0-9._-' '_')"
ACTIVE_RUN_FILE="$ACTIVE_RUNS_DIR/$GUARD_KEY.lock"
mkdir -p "$ACTIVE_RUNS_DIR"
if ! mkdir "$ACTIVE_RUN_FILE" 2>/dev/null; then
  echo "live or unreconciled run claim exists for $TICKET role $ROLE — refusing duplicate launch" >&2
  exit 7
fi
OWNS_ACTIVE_RUN=1
CLAIM_START="$(process_start_identity "$$")"
[[ -n "$CLAIM_START" ]] || { echo "could not record run claim process identity" >&2; exit 8; }
CLAIM_TOKEN="$(python3 -c 'import secrets; print(secrets.token_hex(16))')"
ACTIVE_RUN_EXPECTED="pid=$$
process_start=$CLAIM_START
token=$CLAIM_TOKEN"
printf '%s\n' "$ACTIVE_RUN_EXPECTED" > "$ACTIVE_RUN_FILE/owner"
ACTIVE_RUN_EXPECTED="$(cat "$ACTIVE_RUN_FILE/owner")"

if [[ "$PARALLEL_PROVIDER_RUN" -eq 0 ]]; then
  # Legacy contracts retain the interval-wide product provider lock.
  PROVIDER_LOCK_TRANSIENTS=0
  for i in $(seq 1 "$LOCK_ATTEMPTS"); do
    if mkdir "$PROVIDER_LOCK" 2>/dev/null; then
      HELD_PROVIDER_LOCK=1
      PROVIDER_LOCK_EXPECTED="$ACTIVE_RUN_EXPECTED"
      printf '%s\n' "$PROVIDER_LOCK_EXPECTED" |
        python3 "$KIT_DIR/scripts/lib/durable-file.py" write "$PROVIDER_LOCK/owner" || exit 8
      break
    fi
    if [[ -f "$FACTORY_DIR/KILL" ]]; then
      echo "KILL file appeared while waiting for provider lock; no task was submitted" >&2
      exit 4
    fi
    if [[ -f "$FACTORY_DIR/MAINTENANCE" ]]; then
      echo "MAINTENANCE file appeared while waiting for provider lock; no task was submitted" >&2
      exit 4
    fi
    if provider_lock_owner_is_live; then
      PROVIDER_LOCK_TRANSIENTS=0
    else
      owner_status=$?
      if [[ "$PROVIDER_LOCK_TRANSIENTS" -lt 10 ]]; then
        PROVIDER_LOCK_TRANSIENTS=$((PROVIDER_LOCK_TRANSIENTS + 1))
        sleep 0.1
        continue
      elif [[ "$owner_status" -eq 1 ]]; then
        echo "stale provider lock requires operator reconciliation; ordinary launch will not reclaim it" >&2
      else
        echo "unsafe provider lock requires operator reconciliation; ordinary launch will not reclaim it" >&2
      fi
      exit 8
    fi
    sleep 0.1
  done
  if [[ "$HELD_PROVIDER_LOCK" -ne 1 ]]; then
    echo "provider lock stuck — no task was submitted" >&2
    exit 8
  fi
fi

GLOBAL_LEDGER_HEADER="date,time,repo,ticket,role,adapter,prompt_version,turns,cost_usd,exit_status,run_id,provider_family,model_id,selection_reason,cost_basis,adapter_version"
LEGACY_GLOBAL_HEADER="date,time,repo,ticket,role,adapter,prompt_version,turns,cost_usd,exit_status"
PARTIAL_GLOBAL_HEADER="$LEGACY_GLOBAL_HEADER,run_id,provider_family"
RUN_ID="$(date +%s)-$$"
MANIFEST="$RUNS_DIR/$RUN_ID.meta"
CANCEL_REQUEST_FILE="$RUNS_DIR/$RUN_ID.cancel-request.json"
RUN_STARTED_AT="$(date -u +%FT%TZ)"
TODAY="${RUN_STARTED_AT%%T*}"
if [[ -n "$BUDGET_DAY" ]]; then
  [[ "$(date -u +%F)" == "$BUDGET_DAY" ]] || {
    echo "development budget day changed before reservation; no task was submitted" >&2
    exit 8
  }
  TODAY="$BUDGET_DAY"
fi
RUN_START_TIME="${RUN_STARTED_AT#*T}"; RUN_START_TIME="${RUN_START_TIME%Z}"
RESERVED_USD="$PER_RUN_BUDGET_USD"
if [[ "$PARALLEL_PROVIDER_RUN" -eq 1 ]]; then
  while IFS= read -r micro_value; do
    PROVIDER_BUDGET_MICRO_VALUES+=("$micro_value")
  done < <(python3 - "$RESERVED_USD" "$DAILY_CAP_USD" \
    "$PER_TICKET_BUDGET_USD" "${GLOBAL_DAILY_CAP_USD:-1000000000}" <<'PY'
from decimal import Decimal, InvalidOperation, ROUND_CEILING
import sys
try:
    for value in sys.argv[1:]:
        amount = (Decimal(value) * Decimal(1_000_000)).to_integral_value(
            rounding=ROUND_CEILING
        )
        if amount < 0 or amount > 10**15:
            raise ValueError
        print(int(amount))
except (InvalidOperation, ValueError):
    raise SystemExit(1)
PY
  )
  if [[ "${#PROVIDER_BUDGET_MICRO_VALUES[@]}" -ne 4 ]]; then
    echo "provider budget conversion failed; no task was submitted" >&2
    exit 3
  fi
fi
if [[ "$CLI_CONCURRENT_RUN" -eq 1 ]]; then
  CLI_ATTEMPT_ID="$RUN_ID-cli"
  CLI_PRODUCT_ID="$PROVIDER_PRODUCT_ID"
  CLI_CONFIGURATION_LOCK_ARGS=()
  if [[ -n "${FACTORY_PROVIDER_CONFIGURATION_LOCK:-}" ]]; then
    CLI_CONFIGURATION_LOCK_ARGS=(
      --configuration-lock "$FACTORY_PROVIDER_CONFIGURATION_LOCK"
    )
  fi
  CLI_ATTEMPT_ARGS=(
    --attempt-id "$CLI_ATTEMPT_ID" \
    --provider-family "$SELECTED_FAMILY" \
    --account-route "$SELECTED_ACCOUNT_ROUTE_ID" \
    --reserve-micro-usd "${PROVIDER_BUDGET_MICRO_VALUES[0]}" \
    --product-id "$CLI_PRODUCT_ID" \
    --ticket-id "$TICKET" \
    --budget-day "$TODAY" \
    --product-daily-cap-micro-usd "${PROVIDER_BUDGET_MICRO_VALUES[1]}" \
    --ticket-cap-micro-usd "${PROVIDER_BUDGET_MICRO_VALUES[2]}" \
    --machine-daily-cap-micro-usd "${PROVIDER_BUDGET_MICRO_VALUES[3]}"
  )
  CLI_RESERVATION_ARGS=(
    "${CLI_ATTEMPT_ARGS[@]}" \
    --policy "$FACTORY_PROVIDER_POLICY" \
    "${CLI_CONFIGURATION_LOCK_ARGS[@]}" \
    --expected-policy-sha256 "$ACTIVATED_POLICY_HASH"
  )
  if [[ "$PROVIDER_WAIT_SECONDS" -gt 0 ]]; then
    CLI_WAIT_ENVELOPE_BINDING="$RESERVED_USD|$DAILY_CAP_USD|$PER_TICKET_BUDGET_USD|${GLOBAL_DAILY_CAP_USD:-1000000000}"
    python3 "$KIT_DIR/scripts/provider-coordinator.py" \
      --db "$FACTORY_PROVIDER_DB" prepare \
      --operation-id "$CLI_ATTEMPT_ID-prepare" \
      "${CLI_ATTEMPT_ARGS[@]}" >/dev/null || {
        echo "CLI provider preparation failed; no task was submitted" >&2
        exit 8
      }
    CLI_ATTEMPT_ACTIVE=1
    start_lease_heartbeat
    rmdir "$LAUNCH_LOCK"
    HELD_LAUNCH_LOCK=0
    if CLI_RESERVATION="$(python3 "$KIT_DIR/scripts/provider-coordinator.py" \
        --db "$FACTORY_PROVIDER_DB" wait-admit \
        --operation-id "$CLI_ATTEMPT_ID-wait-admit" \
        --attempt-id "$CLI_ATTEMPT_ID" --expected-version 1 \
        --policy "$FACTORY_PROVIDER_POLICY" \
        "${CLI_CONFIGURATION_LOCK_ARGS[@]}" \
        --expected-policy-sha256 "$ACTIVATED_POLICY_HASH" \
        --wait-seconds "$PROVIDER_WAIT_SECONDS" \
        --cancel-path "$FACTORY_DIR/KILL" \
        --cancel-path "$FACTORY_DIR/MAINTENANCE" \
        --cancel-path "$CANCEL_REQUEST_FILE")"; then
      CLI_RESERVATION_STATUS=0
    else
      CLI_RESERVATION_STATUS=$?
    fi
    for i in $(seq 1 "$LOCK_ATTEMPTS"); do
      mkdir "$LAUNCH_LOCK" 2>/dev/null && { HELD_LAUNCH_LOCK=1; break; }
      sleep 0.1
    done
    [[ "$HELD_LAUNCH_LOCK" -eq 1 ]] || {
      echo "launch lock stuck after CLI provider wait; no task was submitted" >&2
      exit 8
    }
    [[ "$CLI_RESERVATION_STATUS" -eq 0 ]] || {
      echo "CLI provider reservation wait failed; no task was submitted" >&2
      exit 8
    }
    load_effective_envelope || {
      echo "effective envelope changed during CLI provider wait; no task was submitted" >&2
      exit 3
    }
    [[ "$CLI_WAIT_ENVELOPE_BINDING" == \
      "$PER_RUN_BUDGET_USD|$DAILY_CAP_USD|$PER_TICKET_BUDGET_USD|${GLOBAL_DAILY_CAP_USD:-1000000000}" ]] || {
      echo "effective envelope changed during CLI provider wait; no task was submitted" >&2
      exit 3
    }
    POST_WAIT_ACTIVATION_OUTPUT="$(python3 "$KIT_DIR/scripts/provider-activation.py" \
      "${ACTIVATION_ARGS[@]}" --route-id "$SELECTED_ROUTE_ID" 2>/dev/null)" || {
        echo "CLI concurrency activation changed during provider wait; no task was submitted" >&2
        exit 3
      }
    [[ "$POST_WAIT_ACTIVATION_OUTPUT" == "$ACTIVATION_OUTPUT" ]] || {
      echo "CLI concurrency activation changed during provider wait; no task was submitted" >&2
      exit 3
    }
    [[ ! -f "$FACTORY_DIR/KILL" ]] || {
      echo "KILL file appeared during CLI provider wait; no task was submitted" >&2
      exit 4
    }
    [[ ! -f "$FACTORY_DIR/MAINTENANCE" ]] || {
      echo "MAINTENANCE file appeared during CLI provider wait; no task was submitted" >&2
      exit 4
    }
    [[ ! -e "$CANCEL_REQUEST_FILE" && ! -L "$CANCEL_REQUEST_FILE" ]] || {
      echo "targeted cancellation appeared during CLI provider wait; no task was submitted" >&2
      exit 130
    }
    factory_dispatch_require_lease "$REPO_ROOT" "$TICKET" "$DISPATCH_LEASE_ID" || {
      echo "$FACTORY_DISPATCH_LEASE_ERROR after CLI provider wait; no task was submitted" >&2
      exit 7
    }
  else
    CLI_RESERVATION="$(python3 "$KIT_DIR/scripts/provider-coordinator.py" \
      --db "$FACTORY_PROVIDER_DB" reserve \
      --operation-id "$CLI_ATTEMPT_ID-reserve" \
      "${CLI_RESERVATION_ARGS[@]}")" || {
        echo "CLI provider reservation failed; no task was submitted" >&2
        exit 8
      }
  fi
  if ! printf '%s' "$CLI_RESERVATION" | python3 -c '
import json, sys
raise SystemExit(0 if json.load(sys.stdin).get("admitted") is True else 1)
'; then
    python3 "$KIT_DIR/scripts/provider-coordinator.py" \
      --db "$FACTORY_PROVIDER_DB" terminalize \
      --operation-id "$CLI_ATTEMPT_ID-capacity-denied" \
      --attempt-id "$CLI_ATTEMPT_ID" --expected-version 1 \
      --result capacity_denied --charge-micro-usd 0 >/dev/null || {
        echo "CLI provider denial could not be terminalized" >&2
        exit 8
      }
    echo "CLI provider capacity or budget refused; no task was submitted" >&2
    exit 8
  fi
  CLI_ATTEMPT_ACTIVE=1
fi
if [[ "$CLI_CONCURRENT_RUN" -eq 1 && "$ADAPTER" == cursor-* ]]; then
  CURSOR_ACCOUNT_LEASE_ID="$CLI_ATTEMPT_ID-account"
  CURSOR_ACCOUNT_OWNER_PID="$$"
  CURSOR_ACCOUNT_OWNER_PGID="$(ps -o pgid= -p "$$" 2>/dev/null | awk '{$1=$1; print; exit}')"
  CURSOR_ACCOUNT_OWNER_START="$CLAIM_START"
  [[ "$CURSOR_ACCOUNT_OWNER_PGID" =~ ^[1-9][0-9]*$ ]] || {
    echo "Cursor account admission owner identity is unavailable; no task was submitted" >&2
    exit 8
  }
  CURSOR_ACCOUNT_ENVELOPE_BINDING="$RESERVED_USD|$DAILY_CAP_USD|$PER_TICKET_BUDGET_USD|${GLOBAL_DAILY_CAP_USD:-1000000000}"
  rmdir "$LAUNCH_LOCK"
  HELD_LAUNCH_LOCK=0
  if CURSOR_ACCOUNT_RESERVATION="$(python3 "$KIT_DIR/scripts/provider-coordinator.py" \
      --db "$FACTORY_PROVIDER_DB" \
      --account-db "$FACTORY_CURSOR_ACCOUNT_DB" account-acquire \
      --lease-id "$CURSOR_ACCOUNT_LEASE_ID" \
      --account-route "$SELECTED_ACCOUNT_ROUTE_ID" \
      --trust-scope "$FACTORY_KIT_TRUST_SCOPE" \
      --owner-pid "$CURSOR_ACCOUNT_OWNER_PID" \
      --owner-pgid "$CURSOR_ACCOUNT_OWNER_PGID" \
      --owner-start "$CURSOR_ACCOUNT_OWNER_START" \
      --policy "$FACTORY_PROVIDER_POLICY" \
      "${CLI_CONFIGURATION_LOCK_ARGS[@]}" \
      --expected-policy-sha256 "$ACTIVATED_POLICY_HASH" \
      --wait-seconds 900 \
      --cancel-path "$FACTORY_DIR/KILL" \
      --cancel-path "$FACTORY_DIR/MAINTENANCE" \
      --cancel-path "$CANCEL_REQUEST_FILE" 2>&1)"; then
    CURSOR_ACCOUNT_RESERVATION_STATUS=0
  else
    CURSOR_ACCOUNT_RESERVATION_STATUS=$?
  fi
  for i in $(seq 1 "$LOCK_ATTEMPTS"); do
    mkdir "$LAUNCH_LOCK" 2>/dev/null && { HELD_LAUNCH_LOCK=1; break; }
    sleep 0.1
  done
  [[ "$HELD_LAUNCH_LOCK" -eq 1 ]] || {
    echo "launch lock stuck after Cursor account wait; no task was submitted" >&2
    exit 8
  }
  if [[ "$CURSOR_ACCOUNT_RESERVATION_STATUS" -ne 0 ]]; then
    CURSOR_ACCOUNT_ERROR="$(printf '%s' "$CURSOR_ACCOUNT_RESERVATION" | python3 -c '
import json, sys
try:
    value = json.load(sys.stdin).get("error", "")
except Exception:
    value = ""
print(value if value in {
    "live account admission policies disagree across lanes",
    "active account start-window policies disagree across lanes",
} else "")
' 2>/dev/null || true)"
    if [[ -n "$CURSOR_ACCOUNT_ERROR" ]]; then
      echo "Cursor account admission failed: $CURSOR_ACCOUNT_ERROR" >&2
    else
      echo "Cursor account admission failed; no task was submitted" >&2
    fi
    exit 8
  fi
  if ! printf '%s' "$CURSOR_ACCOUNT_RESERVATION" | python3 -c '
import json, sys
raise SystemExit(0 if json.load(sys.stdin).get("admitted") is True else 1)
'; then
    CURSOR_ACCOUNT_STOPPED="$(printf '%s' "$CURSOR_ACCOUNT_RESERVATION" | python3 -c '
import json, sys
print("yes" if json.load(sys.stdin).get("stopped_by") else "")
' 2>/dev/null || true)"
    if [[ -n "$CURSOR_ACCOUNT_STOPPED" ]]; then
      echo "Cursor account admission stopped before GO; no task was submitted" >&2
    else
      echo "Cursor account admission timed out; no task was submitted" >&2
    fi
    exit 8
  fi
  CURSOR_ACCOUNT_LEASE_ACTIVE=1
  load_effective_envelope || {
    echo "effective envelope changed during Cursor account wait; no task was submitted" >&2
    exit 3
  }
  [[ "$CURSOR_ACCOUNT_ENVELOPE_BINDING" == \
    "$PER_RUN_BUDGET_USD|$DAILY_CAP_USD|$PER_TICKET_BUDGET_USD|${GLOBAL_DAILY_CAP_USD:-1000000000}" ]] || {
    echo "effective envelope changed during Cursor account wait; no task was submitted" >&2
    exit 3
  }
  POST_ACCOUNT_ACTIVATION_OUTPUT="$(python3 "$KIT_DIR/scripts/provider-activation.py" \
    "${ACTIVATION_ARGS[@]}" --route-id "$SELECTED_ROUTE_ID" 2>/dev/null)" || {
      echo "CLI concurrency activation changed during Cursor account wait; no task was submitted" >&2
      exit 3
    }
  [[ "$POST_ACCOUNT_ACTIVATION_OUTPUT" == "$ACTIVATION_OUTPUT" ]] || {
    echo "CLI concurrency activation changed during Cursor account wait; no task was submitted" >&2
    exit 3
  }
  [[ ! -f "$FACTORY_DIR/KILL" ]] || {
    echo "KILL file appeared during Cursor account wait; no task was submitted" >&2
    exit 4
  }
  [[ ! -f "$FACTORY_DIR/MAINTENANCE" ]] || {
    echo "MAINTENANCE file appeared during Cursor account wait; no task was submitted" >&2
    exit 4
  }
  [[ ! -e "$CANCEL_REQUEST_FILE" && ! -L "$CANCEL_REQUEST_FILE" ]] || {
    echo "targeted cancellation appeared during Cursor account wait; no task was submitted" >&2
    exit 130
  }
  factory_dispatch_require_lease "$REPO_ROOT" "$TICKET" "$DISPATCH_LEASE_ID" || {
    echo "$FACTORY_DISPATCH_LEASE_ERROR after Cursor account wait; no task was submitted" >&2
    exit 7
  }
fi
if [[ "$PARALLEL_PROVIDER_RUN" -eq 0 &&
      ( "$PROVIDER_CONTRACT_VERSION" == "1.6.0" ||
        "$PROVIDER_CONTRACT_VERSION" == "1.7.0" ||
        "$PROVIDER_CONTRACT_VERSION" == "1.8.0" ||
        "$PROVIDER_CONTRACT_VERSION" == "1.9.0" ) &&
      -n "${FACTORY_PROVIDER_DB:-}" && -f "$FACTORY_PROVIDER_DB" ]]; then
  LEGACY_INTERVAL_ID="legacy-$RUN_ID"
  LEGACY_PRODUCT_ID="$(basename "$REPO_ROOT" | tr -c 'A-Za-z0-9._:@-' '_')"
  LEGACY_ENTER_OUTPUT="$(python3 "$KIT_DIR/scripts/provider-coordinator.py" \
    --db "$FACTORY_PROVIDER_DB" legacy-enter \
    --operation-id "$LEGACY_INTERVAL_ID-enter" \
    --interval-id "$LEGACY_INTERVAL_ID" \
    --product-id "$LEGACY_PRODUCT_ID")" || {
      echo "legacy provider barrier could not be entered; no task was submitted" >&2
      exit 8
    }
  if ! printf '%s' "$LEGACY_ENTER_OUTPUT" | python3 -c '
import json, sys
raise SystemExit(0 if json.load(sys.stdin).get("entered") else 1)
'; then
    echo "isolated provider intervals are active; legacy run refused" >&2
    exit 8
  fi
  LEGACY_INTERVAL_ACTIVE=1
fi
if [[ -n "$FACTORY_ENVELOPE_NEXT_OVERRIDE_IDS" ]] &&
   ! python3 -B "$ENVELOPE_CONTROL" consume --factory-root "$REPO_ROOT" \
     --record-ids "$FACTORY_ENVELOPE_NEXT_OVERRIDE_IDS" --run-id "$RUN_ID" >/dev/null; then
  echo "next-attempt envelope override could not be consumed; no task was submitted" >&2
  exit 3
fi
[[ -n "$PROMPT_FILE" && -f "$PROMPT_FILE" ]] && PROMPT_VERSION="$(grep -m1 '^Version:' "$PROMPT_FILE" | awk '{print $2}' || echo unversioned)"
write_manifest "resolved"
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
if [[ "$ROLE_EXIT_ENFORCED" -eq 1 && "$TICKET_AFFINITY_WAS_MISSING" -eq 1 ]]; then
  TICKET_RELATIVE="${TICKET_FILE#"$WORKDIR/"}"
  CHANGED_PATHS="$("$FACTORY_TRUSTED_GIT_BIN" -C "$WORKDIR" status --porcelain --untracked-files=all | awk '{print $2}')"
  if [[ "$CHANGED_PATHS" != "$TICKET_RELATIVE" ]]; then
    echo "role_exit_dirty: Kit-SHA recording changed unexpected paths" >&2
    exit 11
  fi
  "$FACTORY_TRUSTED_GIT_BIN" -C "$WORKDIR" add -- "$TICKET_RELATIVE"
  "$FACTORY_TRUSTED_GIT_BIN" -C "$WORKDIR" -c user.name="Software Factory" -c user.email="factory@local" \
    commit -m "Record $TICKET kit affinity" >/dev/null 2>&1 || {
    echo "role_exit_no_commit: could not commit Kit-SHA affinity" >&2
    exit 11
  }
  ROLE_HEAD_BEFORE="$("$FACTORY_TRUSTED_GIT_BIN" -C "$WORKDIR" rev-parse HEAD)"
  factory_product_remote_matches "$REPO_ROOT" "$PRODUCT_REMOTE" || {
    echo "role_exit_remote_mismatch: $FACTORY_PRODUCT_REMOTE_ERROR" >&2
    exit 11
  }
  ROLE_TRACKING_BEFORE="$(factory_remote_tracking_tip "$WORKDIR" "$ROLE_BRANCH_BEFORE")"
  "$FACTORY_TRUSTED_GIT_BIN" -C "$WORKDIR" push --no-force -- "$PRODUCT_REMOTE" \
    "$ROLE_HEAD_BEFORE:refs/heads/$ROLE_BRANCH_BEFORE" >/dev/null 2>&1 || {
    echo "role_exit_push_failed: could not push Kit-SHA affinity" >&2
    exit 11
  }
  factory_update_tracking_ref "$WORKDIR" "$ROLE_BRANCH_BEFORE" \
    "$ROLE_HEAD_BEFORE" "$ROLE_TRACKING_BEFORE" || {
    echo "role_exit_remote_mismatch: could not update the verified tracking ref" >&2
    exit 11
  }
fi
if [[ "$ROLE_EXIT_ENFORCED" -eq 1 ]]; then
  ROLE_REMOTE_BEFORE="$(role_remote_head || true)"
  [[ "$ROLE_REMOTE_BEFORE" == "$ROLE_HEAD_BEFORE" ]] || {
    echo "role_exit_remote_mismatch: origin/$ROLE_BRANCH_BEFORE does not match the ticket worktree" >&2
    exit 11
  }
fi
if ! factory_dispatch_require_lease "$REPO_ROOT" "$TICKET" "$DISPATCH_LEASE_ID"; then
  echo "$FACTORY_DISPATCH_LEASE_ERROR after launch lock acquisition; no task was submitted" >&2
  exit 7
fi
if [[ "${FACTORY_TEST_MODE:-0}" == "1" &&
      "${FACTORY_TEST_BEFORE_REGISTER_SLEEP:-0}" != "0" ]]; then
  sleep "$FACTORY_TEST_BEFORE_REGISTER_SLEEP"
fi

if [[ "$PARALLEL_PROVIDER_RUN" -eq 0 ]]; then
# --- serialized legacy cap check with budget reservation ---
# mkdir is atomic: it is the lock. Reservation counts this run's full per-run
# budget against the caps, so N concurrent runs cannot all squeeze past.
for i in $(seq 1 50); do mkdir "$LOCK_DIR" 2>/dev/null && { HELD_LEDGER_LOCK=1; break; }; sleep 0.2; [[ $i -eq 50 ]] && { echo "ledger lock stuck — see runbook" >&2; exit 8; }; done
if ! refresh_runtime_ledger; then
  echo "effective ledger could not be reduced; refusing launch" >&2
  exit 3
fi

SPENT_TODAY="$(python3 "$MONEY" sum-csv --csv "$LEDGER" --date "$TODAY" \
  --date-column 0 --amount-column 7)"
SPENT_TICKET="$(python3 "$MONEY" sum-csv --csv "$LEDGER" \
  --date-column 0 --amount-column 7 --filter-column 2 --filter-value "$TICKET")"
# ponytail: shrink the reservation to the remaining ticket budget so a nearly
# finished ticket is not refused by flat-reserve arithmetic. The resulting
# reservation is the amount charged against every cap and passed to the adapter.
RESERVED_USD="$(python3 "$MONEY" reserve --budget "$PER_RUN_BUDGET_USD" \
  --spent "$SPENT_TICKET" --cap "$PER_TICKET_BUDGET_USD")"
if python3 "$MONEY" exceeds --spent "$SPENT_TODAY" --reserve "$RESERVED_USD" \
    --cap "$DAILY_CAP_USD"; then
  echo "daily cap would be exceeded (spent \$$SPENT_TODAY + reserve \$$RESERVED_USD > \$$DAILY_CAP_USD) — refusing. See docs/runbooks/operator.md." >&2
  exit 5
fi
if python3 "$MONEY" exceeds --spent "$SPENT_TICKET" --reserve "$RESERVED_USD" \
    --cap "$PER_TICKET_BUDGET_USD"; then
  echo "ticket budget would be exceeded for $TICKET (spent \$$SPENT_TICKET + reserve \$$RESERVED_USD > \$$PER_TICKET_BUDGET_USD) — move ticket to Blocked-Escalated." >&2
  exit 5
fi
# Global cap check + reservation (own lock, taken while holding the repo
# lock — lock order is always repo → global, so no deadlock is possible).
LEDGER_FAMILY="$(meta_value "$SELECTED_FAMILY")"
LEDGER_MODEL="$(meta_value "$SELECTED_MODEL")"
LEDGER_REASON="$(meta_value "$SELECTION_REASON")"
LEDGER_VERSION="$(meta_value "$SELECTED_VERSION")"
ACCOUNTING_SCHEMA=1
ACCOUNTING_STATE="reserved"
if [[ -n "$GLOBAL_LEDGER" ]]; then
  GLOBAL_DIR="$(dirname "$GLOBAL_LEDGER")"
  [[ -d "$GLOBAL_DIR" && ! -L "$GLOBAL_DIR" ]] || {
    echo "global ledger directory must be an existing real directory" >&2
    exit 3
  }
  for i in $(seq 1 50); do
    if mkdir "$GLOBAL_LOCK" 2>/dev/null; then
      HELD_GLOBAL_LOCK=1
      GLOBAL_LOCK_TOKEN="$(python3 -c 'import secrets; print(secrets.token_hex(16))')"
      printf '%s\n' "$GLOBAL_LOCK_TOKEN" > "$GLOBAL_LOCK/owner"
      break
    fi
    sleep 0.2
    [[ $i -eq 50 ]] && { echo "global ledger lock stuck — see runbook" >&2; rmdir "$LOCK_DIR"; HELD_LEDGER_LOCK=0; exit 8; }
  done
  if [[ -L "$GLOBAL_LEDGER" ]]; then
    echo "global ledger must be a regular non-symlink file" >&2
    exit 3
  elif [[ ! -f "$GLOBAL_LEDGER" ]]; then
    echo "$GLOBAL_LEDGER_HEADER" > "$GLOBAL_LEDGER"
  else
    CURRENT_GLOBAL_HEADER="$(awk 'NR==1 {print; exit}' "$GLOBAL_LEDGER")"
    case "$CURRENT_GLOBAL_HEADER" in
      "$GLOBAL_LEDGER_HEADER") ;;
      "$LEGACY_GLOBAL_HEADER")
        TMP_HEADER="$GLOBAL_LEDGER.header.$$"
        { echo "$GLOBAL_LEDGER_HEADER"; awk 'NR>1 {print $0 ",,,,,,"}' "$GLOBAL_LEDGER"; } > "$TMP_HEADER"
        mv "$TMP_HEADER" "$GLOBAL_LEDGER"
        ;;
      "$PARTIAL_GLOBAL_HEADER")
        TMP_HEADER="$GLOBAL_LEDGER.header.$$"
        { echo "$GLOBAL_LEDGER_HEADER"; awk 'NR>1 {print $0 ",,,,"}' "$GLOBAL_LEDGER"; } > "$TMP_HEADER"
        mv "$TMP_HEADER" "$GLOBAL_LEDGER"
        ;;
      *)
        echo "unsupported global ledger schema; refusing automatic rewrite: $CURRENT_GLOBAL_HEADER" >&2
        exit 3
        ;;
    esac
  fi
  validate_global_ledger "$GLOBAL_LEDGER" || {
    echo "global ledger contains invalid accounting rows" >&2
    exit 3
  }
  SPENT_GLOBAL="$(python3 "$MONEY" sum-csv --csv "$GLOBAL_LEDGER" \
    --date "$TODAY" --date-column 0 --amount-column 8)"
  if python3 "$MONEY" exceeds --spent "$SPENT_GLOBAL" \
      --reserve "$RESERVED_USD" --cap "$GLOBAL_DAILY_CAP_USD"; then
    echo "MACHINE daily cap would be exceeded across all factories (spent \$$SPENT_GLOBAL + reserve \$$RESERVED_USD > \$$GLOBAL_DAILY_CAP_USD) — refusing. See docs/runbooks/operator.md." >&2
    release_global_lock || true
    rmdir "$LOCK_DIR"; HELD_LEDGER_LOCK=0; exit 5
  fi
  {
    cat "$GLOBAL_LEDGER"
    echo "$TODAY,$RUN_START_TIME,$REPO_ROOT,$TICKET,$ROLE,$ADAPTER,reserved,0,$RESERVED_USD,reserved-$RUN_ID,$RUN_ID,$LEDGER_FAMILY,$LEDGER_MODEL,$LEDGER_REASON,conservative_reservation,$LEDGER_VERSION"
  } | python3 "$KIT_DIR/scripts/lib/durable-file.py" write "$GLOBAL_LEDGER" || {
    echo "global ledger reservation could not be persisted" >&2
    exit 3
  }
  validate_global_ledger "$GLOBAL_LEDGER" || {
    echo "global ledger reservation could not be validated" >&2
    exit 3
  }
  GLOBAL_LEDGER_SNAPSHOT="$(snapshot_global_ledger "$GLOBAL_LEDGER")"
fi
fi
if [[ "$PARALLEL_PROVIDER_RUN" -eq 1 ]]; then
  LEDGER_FAMILY="$(meta_value "$SELECTED_FAMILY")"
  LEDGER_MODEL="$(meta_value "$SELECTED_MODEL")"
  LEDGER_REASON="$(meta_value "$SELECTION_REASON")"
  LEDGER_VERSION="$(meta_value "$SELECTED_VERSION")"
  ACCOUNTING_SCHEMA=2
  [[ "$CLI_CONCURRENT_RUN" -eq 0 ]] || ACCOUNTING_SCHEMA=1
  ACCOUNTING_STATE="reserved"
fi

# Reserve in the per-run manifest, then materialize the ignored runtime view.
# A crash after GO leaves the full conservative reservation in force.
write_manifest "reserved"
if ! refresh_runtime_ledger; then
  echo "effective ledger could not be materialized; refusing launch" >&2
  exit 3
fi
if [[ "$ROLE" == "narrator" ]]; then
  NARRATOR_ATTEMPTS="$(python3 -B - "$LEDGER" "$TICKET" <<'PY'
import csv
import sys

with open(sys.argv[1], newline="", encoding="utf-8") as handle:
    print(sum(row.get("ticket") == sys.argv[2] for row in csv.DictReader(handle)))
PY
)" || {
    echo "Narrator accounting evidence could not be counted; refusing launch" >&2
    exit 3
  }
  NARRATOR_COST="$(python3 "$MONEY" sum-csv --csv "$LEDGER" \
    --date-column 0 --amount-column 7 --filter-column 2 --filter-value "$TICKET")" || {
    echo "Narrator accounting evidence could not be summed; refusing launch" >&2
    exit 3
  }
  TASK="$TASK Trusted effective accounting at launch, including this Narrator attempt's conservative reservation: attempts=$NARRATOR_ATTEMPTS cost_usd=$NARRATOR_COST. Do not rerun tests, builds, repo-check, secret-scan, or any broad verification suite."
fi
if [[ "$PARALLEL_PROVIDER_RUN" -eq 0 ]]; then
  rmdir "$LOCK_DIR"; HELD_LEDGER_LOCK=0
fi
start_lease_heartbeat

# --- run one task-bearing process in an isolated process group ---
if [[ "$ROLE_EXIT_ENFORCED" -eq 1 ]]; then
  ROLE_PROTECTED_BEFORE="$(ticket_evidence_snapshot "$TICKET_FILE")" || {
    echo "role_exit_protected_ticket_mutation: protected ticket fields could not be captured" >&2
    exit 11
  }
fi
set +e
RUN_READY_FILE="$RUNS_DIR/.$RUN_ID.ready"
RUN_GO_FILE="$RUNS_DIR/.$RUN_ID.go"
RUN_GATE_FILE="$RUNS_DIR/.$RUN_ID.gate"
RUN_SUBMITTED_FILE="$RUNS_DIR/.$RUN_ID.submitted"
rm -f "$RUN_READY_FILE" "$RUN_GO_FILE" "$RUN_GATE_FILE" "$RUN_SUBMITTED_FILE"
ADAPTER_ARGS=(
  --budget "$RESERVED_USD"
  --max-turns "$PER_RUN_MAX_TURNS"
  --timeout-min "$PER_RUN_TIMEOUT_MIN"
  --prompt-file "${PROMPT_FILE:-/dev/null}"
  --workdir "$WORKDIR"
)
case "$ADAPTER" in
  codex|claude-code|cursor-openai|cursor-anthropic|claude-kimi)
    ADAPTER_ARGS+=(--model "$SELECTED_MODEL" --effort "$SELECTED_EFFORT")
    ;;
esac
RUN_OUTPUT_TEMP="$(mktemp "$RUNS_DIR/.$RUN_ID.output.XXXXXX")" || {
  echo "could not allocate wrapper-owned output capture" >&2
  exit 125
}
export FACTORY_PROGRESS_JOURNAL="$RUNS_DIR/$RUN_ID.progress.jsonl"
exec 8< "$RUN_OUTPUT_TEMP"
exec 9> "$RUN_OUTPUT_TEMP"
rm -f "$RUN_OUTPUT_TEMP"
RUN_OUTPUT_TEMP=""
# The controller may read project model state while selecting a route, but
# task-bearing adapters must never inherit mutation-capable state paths.
unset FACTORY_MODEL_STATE_ROOT FACTORY_PROJECT
CLI_PROVIDER_HOME="$HOME"
CLI_PROVIDER_TMPDIR="${TMPDIR:-/tmp}"
if [[ "$CLI_CONCURRENT_RUN" -eq 1 ]]; then
  prepare_cli_runtime || exit 6
fi
TASK_PATH="$PATH"
if [[ "$ROLE" == "planner" ]]; then
  ROLE_GUARD_ROOT="$(mktemp -d "$CLI_PROVIDER_TMPDIR/factory-planner-policy.XXXXXX")" ||
    exit 125
  for command in npm npx pnpm yarn corepack; do
    ln -s "$KIT_DIR/scripts/lib/role-command-guard.sh" "$ROLE_GUARD_ROOT/$command" ||
      exit 125
  done
  TASK_PATH="$ROLE_GUARD_ROOT:$PATH"
fi
prepare_cli_login_shell || exit 6
TASK_COMMAND=()
STATUS=0
if [[ "$ISOLATED_RUN" -eq 1 ]]; then
  if [[ -z "${FACTORY_PROVIDER_BROKER_URL:-}" ]]; then
    echo "isolated-v1 broker URL is unavailable" >&2
    STATUS=3
  else
    if [[ "${#PROVIDER_BUDGET_MICRO_VALUES[@]}" -ne 4 ]]; then
      echo "isolated-v1 budget conversion failed" >&2
      STATUS=3
    else
      ISOLATED_PRODUCT_ID="$PROVIDER_PRODUCT_ID"
      TASK_COMMAND=(
        /usr/bin/env -u GH_TOKEN -u OPENAI_API_KEY -u ANTHROPIC_API_KEY
        python3 "$KIT_DIR/scripts/provider-isolated-run.py"
          --runtime "$KIT_DIR/scripts/provider-runtime.py"
          --db "$FACTORY_PROVIDER_DB"
          --policy "$FACTORY_PROVIDER_POLICY"
          --broker-db "$FACTORY_PROVIDER_BROKER_DB"
          --broker-credentials "$FACTORY_PROVIDER_CREDENTIALS"
          --broker-url "$FACTORY_PROVIDER_BROKER_URL"
          --broker-path "$ISOLATED_BROKER_PATH"
          --protocol "$ISOLATED_PROTOCOL"
          --model "$ISOLATED_MODEL"
          --attempt-root "$FACTORY_PROVIDER_ATTEMPT_ROOT"
          --artifact-policy "$FACTORY_PROVIDER_ARTIFACT_POLICY"
          --apply-lock "$FACTORY_PROVIDER_APPLY_LOCK_ROOT/$TICKET.lock"
          --worktree "$WORKDIR"
          --branch "$ROLE_BRANCH_BEFORE"
          --base-sha "$ROLE_HEAD_BEFORE"
          --ticket "$TICKET"
          --role "$ROLE"
          --route-id "$SELECTED_ROUTE_ID"
          --provider-family "$ISOLATED_PROVIDER_FAMILY"
          --account-route "$ISOLATED_ACCOUNT_ROUTE"
          --product-id "$ISOLATED_PRODUCT_ID"
          --budget-day "$TODAY"
          --reserve-micro-usd "${PROVIDER_BUDGET_MICRO_VALUES[0]}"
          --product-cap-micro-usd "${PROVIDER_BUDGET_MICRO_VALUES[1]}"
          --ticket-cap-micro-usd "${PROVIDER_BUDGET_MICRO_VALUES[2]}"
          --machine-cap-micro-usd "${PROVIDER_BUDGET_MICRO_VALUES[3]}"
          --prompt-file "$PROMPT_FILE"
          --task "$TASK"
        --image-lock "$KIT_DIR/worker/image-lock.json"
      )
      if [[ -n "${FACTORY_PROVIDER_BROKER_CA:-}" ]]; then
        TASK_COMMAND+=(--broker-ca "$FACTORY_PROVIDER_BROKER_CA")
      fi
    fi
  fi
elif [[ "$CLI_CONCURRENT_RUN" -eq 1 ]]; then
  CLI_RUNTIME_ADAPTER_ARGS=(--adapter "$ADAPTER")
  if [[ "$ADAPTER" == cursor-* ]]; then
    CLI_RUNTIME_ADAPTER_ARGS+=(
      --account-db "$FACTORY_CURSOR_ACCOUNT_DB"
      --account-lease-id "$CURSOR_ACCOUNT_LEASE_ID"
      --account-owner-pid "$CURSOR_ACCOUNT_OWNER_PID"
      --account-owner-pgid "$CURSOR_ACCOUNT_OWNER_PGID"
      --account-owner-start "$CURSOR_ACCOUNT_OWNER_START"
      --trust-scope "$FACTORY_KIT_TRUST_SCOPE"
      --account-policy-sha256 "$ACTIVATED_POLICY_HASH"
    )
  fi
  TASK_COMMAND=(
    /usr/bin/env -u GH_TOKEN -u OPENAI_API_KEY -u ANTHROPIC_API_KEY
    python3 "$KIT_DIR/scripts/provider-cli-runtime.py"
      --coordinator "$KIT_DIR/scripts/provider-coordinator.py"
      --db "$FACTORY_PROVIDER_DB"
      --policy "$FACTORY_PROVIDER_POLICY"
      "${CLI_CONFIGURATION_LOCK_ARGS[@]}"
      "${CLI_RUNTIME_ADAPTER_ARGS[@]}"
      --attempt-id "$CLI_ATTEMPT_ID"
      --provider-family "$SELECTED_FAMILY"
      --account-route "$SELECTED_ACCOUNT_ROUTE_ID"
      --reserve-micro-usd "${PROVIDER_BUDGET_MICRO_VALUES[0]}"
      --product-id "$CLI_PRODUCT_ID"
      --ticket-id "$TICKET"
      --budget-day "$TODAY"
      --product-cap-micro-usd "${PROVIDER_BUDGET_MICRO_VALUES[1]}"
      --ticket-cap-micro-usd "${PROVIDER_BUDGET_MICRO_VALUES[2]}"
      --machine-cap-micro-usd "${PROVIDER_BUDGET_MICRO_VALUES[3]}"
      --pre-reserved
      -- /usr/bin/env -i
        "HOME=$CLI_PROVIDER_HOME" "PATH=$TASK_PATH" "TMPDIR=$CLI_PROVIDER_TMPDIR"
        "XDG_CACHE_HOME=$CLI_PROVIDER_CACHE_DIR"
        "npm_config_cache=$CLI_PROVIDER_CACHE_DIR/npm"
        "FACTORY_ATTEMPT_OUTPUT_ROOT=$CLI_PROVIDER_OUTPUT_DIR"
        "USER=${USER:-}" "LOGNAME=${LOGNAME:-}" "LANG=${LANG:-C}"
        "CODEX_PINNED=${CODEX_PINNED:-}" "CLAUDE_CODE_PINNED=${CLAUDE_CODE_PINNED:-}"
        "CURSOR_AGENT_VERSION=${CURSOR_AGENT_VERSION:-}"
        "CURSOR_AGENT_BIN=${CURSOR_AGENT_BIN:-agent}"
        "AGENT_CLI_CREDENTIAL_STORE=${AGENT_CLI_CREDENTIAL_STORE:-}"
        "FACTORY_CURSOR_SESSION_HOME=$CLI_PROVIDER_HOME"
        "CURSOR_CONFIG_DIR=$CLI_CURSOR_CONFIG_DIR"
        "CURSOR_DATA_DIR=$CLI_CURSOR_DATA_DIR"
        "FACTORY_CURSOR_INTERNAL_SANDBOX=${FACTORY_CURSOR_INTERNAL_SANDBOX:-0}"
        "FACTORY_CURSOR_REPEATED_TOOL_ERROR_LIMIT=${FACTORY_CURSOR_REPEATED_TOOL_ERROR_LIMIT:-0}"
        "FACTORY_PROGRESS_JOURNAL=$FACTORY_PROGRESS_JOURNAL"
        "FACTORY_CLI_INTERNAL_SANDBOX=1"
        "FACTORY_TIMEOUT_FOREGROUND=1"
        "FACTORY_CLI_ATTEMPT_ID=$CLI_ATTEMPT_ID"
        "FACTORY_CLAUDE_SETTINGS=$CLI_CLAUDE_SETTINGS"
        "CLAUDE_CONFIG_DIR=$CLI_CLAUDE_CONFIG_DIR"
        "CLAUDE_CODE_TMPDIR=$CLI_PROVIDER_TMPDIR"
        "FACTORY_ROLE=$ROLE"
        "FACTORY_PROBE_TIMEOUT_SEC=${FACTORY_PROBE_TIMEOUT_SEC:-10}"
        "FACTORY_TEST_MODE=${FACTORY_TEST_MODE:-0}"
        "FACTORY_TRUSTED_TEST_HARNESS=${FACTORY_TRUSTED_TEST_HARNESS:-0}"
        "MOCK_SLEEP=${MOCK_SLEEP:-0}"
        GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=remote.origin.pushurl
        GIT_CONFIG_VALUE_0=disabled://factory-provider-must-not-push
        "$ADAPTER_SH" "${ADAPTER_ARGS[@]}" -- "$TASK"
  )
else
  TASK_COMMAND=(/usr/bin/env "PATH=$TASK_PATH" "$ADAPTER_SH" "${ADAPTER_ARGS[@]}" -- "$TASK")
fi
if [[ "$STATUS" -eq 0 ]]; then
python3 "$KIT_DIR/scripts/lib/run-in-process-group.py" \
  "$RUN_READY_FILE" "$RUN_GATE_FILE" "$RUN_SUBMITTED_FILE" \
  "$FACTORY_DIR/KILL" "$FACTORY_DIR/MAINTENANCE" "$CANCEL_REQUEST_FILE" \
  "${TASK_COMMAND[@]}" 8<&- >&9 2>&1 &
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
    if [[ -e "$CANCEL_REQUEST_FILE" || -L "$CANCEL_REQUEST_FILE" ]]; then
      if load_cancellation_request; then
        echo "targeted cancellation requested before GO; no task was submitted" >&2
        terminate_run_group
        wait "$RUN_PID" 2>/dev/null
        STATUS=130
      else
        echo "malformed targeted cancellation request; no task was submitted" >&2
        terminate_run_group
        wait "$RUN_PID" 2>/dev/null
        STATUS=11
      fi
    elif [[ -f "$FACTORY_DIR/KILL" ]]; then
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
    elif ! factory_dispatch_require_lease "$REPO_ROOT" "$TICKET" "$DISPATCH_LEASE_ID"; then
      echo "$FACTORY_DISPATCH_LEASE_ERROR before GO; no task was submitted" >&2
      terminate_run_group
      wait "$RUN_PID" 2>/dev/null
      STATUS=7
    elif ! sequencer_allows_role; then
      echo "$SEQUENCER_ERROR before GO; no task was submitted" >&2
      terminate_run_group
      wait "$RUN_PID" 2>/dev/null
      STATUS=10
    elif [[ "$ROLE_EXIT_ENFORCED" -eq 1 &&
            "$(role_remote_head || true)" != "$ROLE_REMOTE_BEFORE" ]]; then
      echo "role_exit_remote_mismatch: ticket branch changed before GO; no task was submitted" >&2
      terminate_run_group
      wait "$RUN_PID" 2>/dev/null
      STATUS=11
    elif ! bind_cursor_account_runtime; then
      echo "could not bind Cursor account admission to provider runtime; no task was submitted" >&2
      terminate_run_group
      wait "$RUN_PID" 2>/dev/null
      STATUS=125
    else
      GO_ISSUED=1
      if ! write_manifest "spawned"; then
        echo "could not persist GO marker; no task was submitted" >&2
        terminate_run_group
        wait "$RUN_PID" 2>/dev/null
        STATUS=125
      elif ! python3 "$KIT_DIR/scripts/lib/durable-file.py" touch "$RUN_GO_FILE"; then
        echo "could not persist GO marker; no task was submitted" >&2
        terminate_run_group
        wait "$RUN_PID" 2>/dev/null
        STATUS=125
      elif ! RUNS_META_SNAPSHOT="$(
        if [[ "$CLI_CONCURRENT_RUN" -eq 1 ]]; then
          python3 "$KIT_DIR/scripts/lib/runs-integrity.py" \
            snapshot-one "$RUNS_DIR" "$(basename "$MANIFEST")"
        else
          python3 "$KIT_DIR/scripts/lib/runs-integrity.py" snapshot "$RUNS_DIR"
        fi
      )"; then
        echo "could not snapshot run manifests; no task was submitted" >&2
        terminate_run_group
        wait "$RUN_PID" 2>/dev/null
        STATUS=125
      elif ! REGISTERED_BRANCH_BEFORE="$(registered_ref_identity)" ||
           ! REGISTERED_HEAD_BEFORE="$("$FACTORY_TRUSTED_GIT_BIN" -C "$REPO_ROOT" \
          rev-parse HEAD 2>/dev/null)" ||
           ! REGISTERED_STATUS_BEFORE="$("$FACTORY_TRUSTED_GIT_BIN" -C "$REPO_ROOT" \
          status --porcelain --untracked-files=all 2>/dev/null)" ||
           ! REGISTERED_CONTENT_BEFORE="$(registered_tracked_content 2>/dev/null)"; then
        echo "could not snapshot registered checkout; no task was submitted" >&2
        terminate_run_group
        wait "$RUN_PID" 2>/dev/null
        STATUS=125
      elif ! ACTIVE_RUN_SNAPSHOT="$(active_claim_snapshot 2>/dev/null)"; then
        echo "could not bind run claim ownership; no task was submitted" >&2
        terminate_run_group
        wait "$RUN_PID" 2>/dev/null
        STATUS=125
      elif {
        if [[ "${FACTORY_TEST_MODE:-0}" == "1" &&
              "${FACTORY_TEST_BEFORE_GATE_SLEEP:-0}" != "0" ]]; then
          : > "$RUNS_DIR/.$RUN_ID.before-gate"
          sleep "$FACTORY_TEST_BEFORE_GATE_SLEEP"
          rm -f "$RUNS_DIR/.$RUN_ID.before-gate"
        fi
        stop_before_adapter_gate
      }; then
        verify_control_interval_integrity
      elif ! : > "$RUN_GATE_FILE"; then
        echo "could not open adapter GO gate; no task was submitted" >&2
        terminate_run_group
        wait "$RUN_PID" 2>/dev/null
        STATUS=125
      else
        rmdir "$LAUNCH_LOCK"
        HELD_LAUNCH_LOCK=0
        wait "$RUN_PID"
        STATUS=$?
        if [[ -f "$RUN_SUBMITTED_FILE" && ! -L "$RUN_SUBMITTED_FILE" ]]; then
          TASK_SUBMITTED=1
        fi
        if [[ "$STATUS" -eq 123 && "$TASK_SUBMITTED" -eq 0 ]]; then
          if ! stop_before_adapter_gate; then
            echo "adapter boundary stopped without a valid control record" >&2
            STATUS=11
          fi
        fi
        if [[ "$CLI_CONCURRENT_RUN" -eq 1 ]]; then
          for _integrity_lock_try in $(seq 1 "$LOCK_ATTEMPTS"); do
            mkdir "$LAUNCH_LOCK" 2>/dev/null && { HELD_LAUNCH_LOCK=1; break; }
            sleep 0.1
          done
          if [[ "$HELD_LAUNCH_LOCK" -ne 1 ]]; then
            echo "role_exit_control_plane_mutation: launch lock stuck before concurrent integrity check" >&2
            CONTROL_PLANE_MUTATION=1
            STATUS=11
          else
            verify_control_interval_integrity
          fi
        else
          verify_control_interval_integrity
        fi
      fi
    fi
  fi
fi
fi
if [[ "$GO_ISSUED" -eq 1 && "$TASK_SUBMITTED" -eq 0 &&
      "$STATUS" -eq 125 && -z "$TERMINAL_REASON_CODE" ]]; then
  TERMINAL_REASON_CODE="adapter_submission_unconfirmed"
fi
if [[ -z "$CANCELLATION_REASON" &&
      ( -e "$CANCEL_REQUEST_FILE" || -L "$CANCEL_REQUEST_FILE" ) ]]; then
  if ! load_cancellation_request; then
    echo "role_exit_control_plane_mutation: targeted cancellation request is invalid" >&2
    CONTROL_PLANE_MUTATION=1
    STATUS=11
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
  if ! release_cursor_account_lease; then
    echo "role_exit_control_plane_mutation: Cursor account admission lease could not be released" >&2
    CONTROL_PLANE_MUTATION=1
    STATUS=11
  fi
else
  echo "WARNING: process group $RUN_PGID survived; PID and Cursor account lease records retained for kill-switch" >&2
fi
rm -f "$RUN_READY_FILE" "$RUN_GO_FILE" "$RUN_GATE_FILE" "$RUN_SUBMITTED_FILE"
RUN_READY_FILE=""
RUN_GO_FILE=""
RUN_GATE_FILE=""
RUN_SUBMITTED_FILE=""
exec 9>&-
ROLE_OUTPUT_VALID=1
if ! RUN_OUTPUT_SHA256="$(
  python3 "$KIT_DIR/scripts/lib/role_output.py" publish \
    "$RUNS_DIR/$RUN_ID.out" <&8
)"; then
  ROLE_OUTPUT_VALID=0
  STATUS=11
fi
exec 8<&-
emit_role_output() {
  [[ "$ROLE_OUTPUT_VALID" -eq 1 ]] || return 0
  cat "$RUNS_DIR/$RUN_ID.out"
}
if [[ "$ROLE_OUTPUT_VALID" -eq 1 ]]; then
  ROLE_ESCALATION_PARSE="$(python3 - "$RUNS_DIR/$RUN_ID.out" \
    "$PROVIDER_CONTRACT_VERSION" "$ROLE" <<'PY'
import sys

output, contract, role = sys.argv[1:]
candidates = [
    line.rstrip("\r\n")
    for line in open(output, encoding="utf-8", errors="replace")
    if line.lstrip().upper().startswith("ROLE-ESCALATE:")
]
if not candidates:
    print("none")
elif (
    contract in {"1.7.0", "1.8.0", "1.9.0"}
    and role in {"planner", "test-author", "builder"}
    and candidates == ["ROLE-ESCALATE: CONTRACT-BLOCKED"]
):
    print("contract-blocked")
else:
    print("invalid")
PY
  )" || ROLE_ESCALATION_PARSE=invalid
else
  ROLE_ESCALATION_PARSE=none
fi
case "$ROLE_ESCALATION_PARSE" in
  none) ;;
  contract-blocked)
    if [[ "$ROLE_EXIT_ENFORCED" -eq 1 ]]; then
      ROLE_ESCALATION_REQUESTED=1
    else
      ROLE_ESCALATION_INVALID=1
    fi
    ;;
  *) ROLE_ESCALATION_INVALID=1 ;;
esac

if ! stop_lease_heartbeat; then
  echo "role_exit_control_plane_mutation: dispatcher lease heartbeat failed" >&2
  CONTROL_PLANE_MUTATION=1
  STATUS=11
fi

PROVIDER_STATUS="$STATUS"
if [[ "$CONTROL_PLANE_MUTATION" -eq 0 &&
      -n "$CANCELLATION_REASON" ]]; then
  CANCELLATION_ACCEPTED=1
fi
if [[ "$CONTROL_PLANE_MUTATION" -eq 1 ]]; then
  ROLE_EXIT_STATUS="role_exit_control_plane_mutation"
elif [[ "$CANCELLATION_ACCEPTED" -eq 1 ]]; then
  ROLE_EXIT_STATUS="cancelled"
elif [[ "$ROLE_OUTPUT_VALID" -eq 0 ]]; then
  ROLE_EXIT_STATUS="role_exit_invalid_output"
  echo "role_exit_invalid_output: provider output exceeded or failed the bounded artifact contract" >&2
  STATUS=11
elif [[ "$ROLE_EXIT_ENFORCED" -eq 1 ]]; then
  ROLE_BRANCH_AFTER="$("$FACTORY_TRUSTED_GIT_BIN" -C "$WORKDIR" symbolic-ref --quiet --short HEAD 2>/dev/null || true)"
  ROLE_HEAD_AFTER="$("$FACTORY_TRUSTED_GIT_BIN" -C "$WORKDIR" rev-parse HEAD 2>/dev/null || true)"
  ROLE_TICKET_MODE_VALID=1
  if [[ "$PROVIDER_STATUS" -eq 0 &&
        "$ROLE_BRANCH_AFTER" == "$ROLE_BRANCH_BEFORE" ]] &&
     ! normalize_role_ticket_mode; then
    ROLE_TICKET_MODE_VALID=0
  fi
  ROLE_DIRTY="$("$FACTORY_TRUSTED_GIT_BIN" -C "$WORKDIR" status --porcelain --untracked-files=all 2>/dev/null || true)"
  if [[ "$ROLE_TICKET_MODE_VALID" -eq 1 ]]; then
    ROLE_PROTECTED_AFTER="$(ticket_evidence_snapshot "$TICKET_FILE" 2>/dev/null)" ||
      ROLE_PROTECTED_AFTER="__invalid__"
  else
    ROLE_PROTECTED_AFTER="__invalid__"
  fi
  ROLE_DURABLE_ESCALATION_PARSE="$("$FACTORY_TRUSTED_GIT_BIN" -C "$WORKDIR" \
    diff --no-ext-diff --unified=0 "$ROLE_HEAD_BEFORE" "$ROLE_HEAD_AFTER" -- \
    "factory/tickets/$TICKET.md" 2>/dev/null | awk \
    -v contract="$PROVIDER_CONTRACT_VERSION" -v role="$ROLE" '
      /^\+[^+]/ {
        line=substr($0, 2)
        upper=toupper(line)
        sub(/^[[:space:]]+/, "", upper)
        if (upper ~ /^ROLE-ESCALATE:/) {
          candidates++
          if (upper == "ROLE-ESCALATE: CONTRACT-BLOCKED") exact++
        }
      }
      END {
        if (candidates == 0) print "none"
        else if ((contract == "1.7.0" || contract == "1.8.0" || contract == "1.9.0") &&
                 (role == "planner" || role == "test-author" || role == "builder") &&
                 candidates == 1 && exact == 1) print "contract-blocked"
        else print "invalid"
      }'
  )" || ROLE_DURABLE_ESCALATION_PARSE=invalid
  case "$ROLE_DURABLE_ESCALATION_PARSE" in
    none) ;;
    contract-blocked) ROLE_ESCALATION_REQUESTED=1 ;;
    *) ROLE_ESCALATION_INVALID=1 ;;
  esac
  if [[ "$PROVIDER_STATUS" -eq 0 ]]; then
    if [[ "$ROLE_ESCALATION_INVALID" -eq 1 ]]; then
      ROLE_EXIT_STATUS="role_exit_invalid_escalation"
    elif [[ "$ROLE_BRANCH_AFTER" != "$ROLE_BRANCH_BEFORE" ]]; then
      ROLE_EXIT_STATUS="role_exit_wrong_branch"
    elif [[ "$ROLE_TICKET_MODE_VALID" -ne 1 ]]; then
      ROLE_EXIT_STATUS="role_exit_unsafe_ticket_mode"
    elif [[ "$ROLE" == "reviewer" &&
            ( -n "$ROLE_DIRTY" || "$ROLE_HEAD_AFTER" != "$ROLE_HEAD_BEFORE" ) ]]; then
      ROLE_EXIT_STATUS="reviewer_mutated_worktree"
    elif [[ "$ROLE_PROTECTED_AFTER" == "__invalid__" ]] ||
         ! ticket_evidence_is_legal "$ROLE_PROTECTED_BEFORE" \
           "$ROLE_PROTECTED_AFTER" "$ROLE"; then
      if [[ "$ROLE" == "test-author" || "$ROLE" == "reviewer" ]]; then
        ROLE_EXIT_STATUS="role_exit_protected_ticket_mutation"
      elif quarantine_rewritten_role_history; then
        ROLE_EXIT_STATUS="role_exit_protected_ticket_mutation"
      else
        ROLE_EXIT_STATUS="role_exit_control_plane_mutation"
      fi
    elif ! factory_product_remote_matches "$REPO_ROOT" "$PRODUCT_REMOTE"; then
      ROLE_EXIT_STATUS="role_exit_remote_mismatch"
    elif [[ "$ROLE" == "reviewer" &&
            "$(role_remote_head || true)" != "$ROLE_REMOTE_BEFORE" ]]; then
      ROLE_EXIT_STATUS="role_exit_remote_mismatch"
    elif [[ -n "$ROLE_DIRTY" ]]; then
      ROLE_EXIT_STATUS="role_exit_dirty"
    elif [[ "$ROLE" == "reviewer" ]] &&
         ! python3 "$KIT_DIR/scripts/lib/reviewer-verdict.py" \
           --adapter "$ADAPTER" --input "$RUNS_DIR/$RUN_ID.out" \
           --contract-version "$PROVIDER_CONTRACT_VERSION" >/dev/null 2>&1; then
      ROLE_EXIT_STATUS="role_exit_invalid_output"
    elif [[ "$ROLE" == "reviewer" ]]; then
      ROLE_EXIT_STATUS="ok"
    elif [[ "$ROLE_HEAD_AFTER" == "$ROLE_HEAD_BEFORE" ]]; then
      ROLE_EXIT_STATUS="role_exit_no_commit"
    elif [[ -n "$DEVELOPMENT_LANE_ROOT" ]] &&
         ! python3 "$KIT_DIR/scripts/lib/lane-path-sentinel.py" \
           "$WORKDIR" "$ROLE_HEAD_BEFORE" "$ROLE_HEAD_AFTER"; then
      ROLE_EXIT_STATUS="role_exit_lane_path_leak"
    elif [[ "$ROLE" != "test-author" && "$ROLE" != "reviewer" ]] &&
         ! "$FACTORY_TRUSTED_GIT_BIN" -C "$WORKDIR" merge-base --is-ancestor \
           "$ROLE_HEAD_BEFORE" "$ROLE_HEAD_AFTER"; then
      if quarantine_rewritten_role_history; then
        ROLE_EXIT_STATUS="role_exit_history_rewritten"
      else
        ROLE_EXIT_STATUS="role_exit_control_plane_mutation"
      fi
    elif [[ "$(role_remote_head || true)" != "$ROLE_REMOTE_BEFORE" ]]; then
      ROLE_EXIT_STATUS="role_exit_remote_mismatch"
    else
      ROLE_TRACKING_BEFORE="$(factory_remote_tracking_tip "$WORKDIR" "$ROLE_BRANCH_BEFORE")"
      if ! "$FACTORY_TRUSTED_GIT_BIN" -C "$WORKDIR" push --no-force -- "$PRODUCT_REMOTE" \
        "$ROLE_HEAD_AFTER:refs/heads/$ROLE_BRANCH_BEFORE" >/dev/null 2>&1; then
        ROLE_EXIT_STATUS="role_exit_push_failed"
      else
        REMOTE_HEAD="$("$FACTORY_TRUSTED_GIT_BIN" -C "$WORKDIR" ls-remote --heads -- "$PRODUCT_REMOTE" \
          "refs/heads/$ROLE_BRANCH_BEFORE" 2>/dev/null | awk 'NR==1 {print $1; exit}')"
        if [[ "$REMOTE_HEAD" != "$ROLE_HEAD_AFTER" ]]; then
          ROLE_EXIT_STATUS="role_exit_remote_mismatch"
        elif ! factory_update_tracking_ref "$WORKDIR" "$ROLE_BRANCH_BEFORE" \
          "$ROLE_HEAD_AFTER" "$ROLE_TRACKING_BEFORE"; then
          ROLE_EXIT_STATUS="role_exit_remote_mismatch"
        else
          if [[ "$ROLE_ESCALATION_REQUESTED" -eq 1 ]]; then
            ROLE_EXIT_STATUS="role_exit_contract_blocked"
          else
            ROLE_EXIT_STATUS="ok"
          fi
        fi
      fi
    fi
    if [[ "$ROLE_EXIT_STATUS" == "role_exit_contract_blocked" ]]; then
      echo "role_exit_contract_blocked: durable role output requests operator escalation" >&2
      STATUS=12
    elif [[ "$ROLE_EXIT_STATUS" != "ok" ]]; then
      echo "$ROLE_EXIT_STATUS: successful provider run did not leave durable role output" >&2
      STATUS=11
    fi
  else
    ROLE_EXIT_STATUS="provider_failed"
    if [[ "$ROLE_BRANCH_AFTER" != "$ROLE_BRANCH_BEFORE" || -n "$ROLE_DIRTY" ||
          "$(role_remote_head || true)" != "$ROLE_REMOTE_BEFORE" ]]; then
      echo "WARNING: provider failed and left ticket worktree changes; preserving them for diagnosis" >&2
    fi
  fi
fi

METRICS_LINE=""
if [[ "$ROLE_OUTPUT_VALID" -eq 1 ]]; then
  METRICS_LINE="$(tail -n1 "$RUNS_DIR/$RUN_ID.out")"
fi
TURNS="$(awk '{ for (i=1; i<=NF; i++) if ($i ~ /^turns=/) { sub(/^turns=/, "", $i); print $i; exit } }' <<<"$METRICS_LINE")"
COST="$(awk '{ for (i=1; i<=NF; i++) if ($i ~ /^cost_usd=/) { sub(/^cost_usd=/, "", $i); print $i; exit } }' <<<"$METRICS_LINE")"
COST_BASIS="$(awk '{ for (i=1; i<=NF; i++) if ($i ~ /^cost_basis=/) { sub(/^cost_basis=/, "", $i); print $i; exit } }' <<<"$METRICS_LINE")"
PROGRESS_EVENTS="$(awk '{ for (i=1; i<=NF; i++) if ($i ~ /^progress_events=/) { sub(/^progress_events=/, "", $i); print $i; exit } }' <<<"$METRICS_LINE")"
PROGRESS_JOURNAL_SHA256="$(awk '{ for (i=1; i<=NF; i++) if ($i ~ /^progress_sha256=/) { sub(/^progress_sha256=/, "", $i); print $i; exit } }' <<<"$METRICS_LINE")"
TIMEOUT_KIND="$(awk '{ for (i=1; i<=NF; i++) if ($i ~ /^timeout_kind=/) { sub(/^timeout_kind=/, "", $i); print $i; exit } }' <<<"$METRICS_LINE")"
TELEMETRY_INVALID=0
if [[ -z "$TURNS" ]]; then
  TURNS=0
elif [[ ! "$TURNS" =~ ^[0-9]{1,4}$ ]] ||
   ! awk -v value="$TURNS" 'BEGIN { exit !(value >= 0 && value <= 1000) }'; then
  TELEMETRY_INVALID=1
fi
if [[ -n "$COST" ]] &&
   { [[ ! "$COST" =~ ^[0-9]{1,7}([.][0-9]{1,18})?$ ]] ||
     python3 "$MONEY" exceeds --spent "$COST" --reserve 0 --cap 1000000; }; then
  TELEMETRY_INVALID=1
fi
if [[ -n "$PROGRESS_EVENTS" ]] &&
   { [[ ! "$PROGRESS_EVENTS" =~ ^[0-9]{1,6}$ ]] ||
     [[ ! "$PROGRESS_JOURNAL_SHA256" =~ ^[0-9a-f]{64}$ ]]; }; then
  TELEMETRY_INVALID=1
fi
if [[ -n "$TIMEOUT_KIND" &&
      "$TIMEOUT_KIND" != "soft_timeout" &&
      "$TIMEOUT_KIND" != "hard_timeout" &&
      "$TIMEOUT_KIND" != "invalid_progress" ]]; then
  TELEMETRY_INVALID=1
fi
if [[ "$TELEMETRY_INVALID" -eq 1 ]]; then
  echo "WARNING: adapter telemetry invalid or oversized — keeping the full reservation and zero turns." >&2
  COST=""
  TURNS=0
  COST_BASIS=""
fi
if [[ "$CANCELLATION_ACCEPTED" -eq 1 && "$GO_ISSUED" -eq 0 ]]; then
  COST="0"
  TURNS="0"
  COST_BASIS="launch_void"
  FINAL_ACCOUNTING_STATE="launch_void"
elif [[ "$CANCELLATION_ACCEPTED" -eq 1 ]]; then
  COST="$RESERVED_USD"
  TURNS="${TURNS:-0}"
  COST_BASIS="conservative_reservation"
  FINAL_ACCOUNTING_STATE="cancelled_conservative"
elif [[ "$GO_ISSUED" -eq 0 ]]; then
  COST="0"
  TURNS="0"
  COST_BASIS="launch_void"
  FINAL_ACCOUNTING_STATE="launch_void"
elif [[ "$ROLE_EXIT_STATUS" == "role_exit_history_rewritten" ]]; then
  COST="$RESERVED_USD"
  TURNS="${TURNS:-0}"
  COST_BASIS="conservative_reservation"
  FINAL_ACCOUNTING_STATE="abandoned_conservative"
elif [[ -z "$COST" ]]; then
  echo "WARNING: run cost unparsable — ledger keeps conservative reservation of \$$RESERVED_USD for this run. Reconcile with the provider console." >&2
  COST="$RESERVED_USD"
  TURNS="${TURNS:-0}"
  COST_BASIS="conservative_reservation"
  FINAL_ACCOUNTING_STATE="abandoned_conservative"
elif [[ -z "$COST_BASIS" ]]; then
  case "$ADAPTER" in
    claude-code) COST_BASIS="provider_reported" ;;
    codex) COST_BASIS="estimated_tokens" ;;
    mock) COST_BASIS="test_fixture" ;;
    *) COST_BASIS="estimated_tokens" ;;
  esac
  FINAL_ACCOUNTING_STATE="completed"
else
  FINAL_ACCOUNTING_STATE="completed"
fi

if [[ "$CLI_CONCURRENT_RUN" -eq 1 ]]; then
  if [[ "$RUN_GROUP_TERMINATED" -ne 1 ]]; then
    echo "role_exit_control_plane_mutation: CLI process group survived; reservation retained" >&2
    STATUS=11
    emit_role_output
    exit "$STATUS"
  fi
  for _terminal_lock_try in $(seq 1 "$LOCK_ATTEMPTS"); do
    mkdir "$LAUNCH_LOCK" 2>/dev/null && { HELD_LAUNCH_LOCK=1; break; }
    sleep 0.1
  done
  if [[ "$HELD_LAUNCH_LOCK" -ne 1 ]]; then
    echo "role_exit_control_plane_mutation: launch lock stuck before CLI terminalization" >&2
    STATUS=11
    emit_role_output
    exit "$STATUS"
  fi
  CLI_TERMINAL_RESULT="failed"
  [[ "$STATUS" -ne 0 ]] || CLI_TERMINAL_RESULT="succeeded"
  [[ "$CANCELLATION_ACCEPTED" -eq 0 ]] || CLI_TERMINAL_RESULT="cancelled"
  if ! reconcile_cli_attempt "$CLI_TERMINAL_RESULT"; then
    echo "role_exit_control_plane_mutation: CLI provider attempt could not be terminalized" >&2
    CONTROL_PLANE_MUTATION=1
    ROLE_EXIT_STATUS="role_exit_control_plane_mutation"
    STATUS=11
  fi
fi

if [[ "$PARALLEL_PROVIDER_RUN" -eq 0 ]] && ! provider_lock_is_owned; then
  echo "role_exit_control_plane_mutation: provider lock changed before terminal accounting" >&2
  CONTROL_PLANE_MUTATION=1
  ROLE_EXIT_STATUS="role_exit_control_plane_mutation"
  STATUS=11
fi
FINAL_PHASE="completed"
[[ "$CANCELLATION_ACCEPTED" -eq 0 ]] || FINAL_PHASE="$FINAL_ACCOUNTING_STATE"
finalize_accounting "$FINAL_ACCOUNTING_STATE" "$COST" "${TURNS:-0}" "$STATUS" "$COST_BASIS" "$FINAL_PHASE"

# Refresh the materialized view under the same lock used by budget checks.
for i in $(seq 1 50); do
  mkdir "$LOCK_DIR" 2>/dev/null && { HELD_LEDGER_LOCK=1; break; }
  sleep 0.2
done
if [[ "$HELD_LEDGER_LOCK" -ne 1 ]]; then
  echo "WARNING: ledger lock stuck while materializing run $RUN_ID; manifest remains authoritative" >&2
else
  if ! refresh_runtime_ledger; then
    echo "WARNING: effective ledger materialization failed for run $RUN_ID; manifest remains authoritative" >&2
  fi
  rmdir "$LOCK_DIR"; HELD_LEDGER_LOCK=0
fi

if [[ "$CLI_CONCURRENT_RUN" -eq 1 ]]; then
  if ! release_active_run_claim; then
    echo "role_exit_control_plane_mutation: CLI run claim could not be terminalized" >&2
    STATUS=11
  fi
  rmdir "$LAUNCH_LOCK" 2>/dev/null || STATUS=11
  HELD_LAUNCH_LOCK=0
fi

finalize_global_ledger
if [[ "$CANCELLATION_ACCEPTED" -eq 1 ]]; then
  if ! python3 "$KIT_DIR/scripts/attempt-cancel.py" receipt \
      --factory-root "$REPO_ROOT" --ticket "$TICKET" --run-id "$RUN_ID" >/dev/null; then
    echo "WARNING: cancellation receipt could not be emitted; provider lock retained" >&2
    RETAIN_PROVIDER_LOCK=1
    STATUS=11
  fi
fi
if [[ "$HELD_PROVIDER_LOCK" -eq 1 && "$RETAIN_PROVIDER_LOCK" -eq 0 ]]; then
  release_provider_lock || {
    echo "WARNING: provider lock ownership changed after terminal accounting; operator reconciliation required" >&2
    STATUS=11
  }
fi

emit_role_output
exit "$STATUS"
