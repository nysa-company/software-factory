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
#   PER_RUN_TIMEOUT_MIN, DAILY_CAP_USD
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
PROVIDER_LOCK_TOKEN=""
RUN_PID=""
RUN_PGID=""
RUN_GROUP_ACTIVE=0
RUN_GROUP_TERMINATED=1
RUN_PID_FILE=""
RUN_READY_FILE=""
RUN_GO_FILE=""
RUN_GATE_FILE=""
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
ROLE_HEAD_BEFORE=""
ROLE_BRANCH_BEFORE=""
ROLE_REMOTE_BEFORE=""
ROLE_PROTECTED_BEFORE=""
PRODUCT_REMOTE=""
ACCOUNTING_SCHEMA=""
ACCOUNTING_STATE=""
GO_ISSUED=0
RUN_STARTED_AT=""
TERMINAL_AT=""
RESERVED_USD=""
EFFECTIVE_COST=""
EXIT_STATUS=""
COST_BASIS=""
TURNS=0
PROMPT_VERSION="unversioned"
SEQUENCER="$KIT_DIR/scripts/next-stage.sh"
SEQUENCER_ERROR=""

sequencer_allows_role() {
  local output rc=0
  SEQUENCER_ERROR=""
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

process_start_identity() {
  ps -o lstart= -p "$1" 2>/dev/null | awk '{$1=$1; print; exit}'
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
        if not re.fullmatch(r"[0-9]{1,7}(?:\.[0-9]{1,18})?", cost) or float(cost) > 1_000_000:
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
      "$(cat "$PROVIDER_LOCK/owner" 2>/dev/null)" == "$PROVIDER_LOCK_TOKEN" ]]
}

release_provider_lock() {
  provider_lock_is_owned || return 1
  rm -f "$PROVIDER_LOCK/owner" || return 1
  rmdir "$PROVIDER_LOCK" || return 1
  HELD_PROVIDER_LOCK=0
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
    echo "go_issued=$(meta_value "$GO_ISSUED")"
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
    echo "role_exit=$(meta_value "${ROLE_EXIT_STATUS:-}")"
    echo "role_branch_before=$(meta_value "${ROLE_BRANCH_BEFORE:-}")"
    echo "role_head_before=$(meta_value "${ROLE_HEAD_BEFORE:-}")"
    echo "role_remote_before=$(meta_value "${ROLE_REMOTE_BEFORE:-}")"
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

role_remote_head() {
  "$FACTORY_TRUSTED_GIT_BIN" -C "$WORKDIR" ls-remote --heads -- "$PRODUCT_REMOTE" \
    "refs/heads/$ROLE_BRANCH_BEFORE" 2>/dev/null | awk 'NR==1 {print $1; exit}'
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
    r"^\s*reviewer round\s+\d+:\s*(?:APPROVE|REQUEST CHANGES(?:\s+—\s+.*)?)\s*$",
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

cleanup() {
  local status=$? accounting_finalized=0
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
  [[ -z "$RUN_GATE_FILE" ]] || rm -f "$RUN_GATE_FILE"
  [[ -z "$RUN_OUTPUT_TEMP" ]] || rm -f "$RUN_OUTPUT_TEMP"
  exec 8<&- 9>&- 2>/dev/null || true
  if [[ -n "$MANIFEST" && "$ACCOUNTING_STATE" == "reserved" ]]; then
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
  if [[ "$HELD_PROVIDER_LOCK" -eq 1 ]]; then
    release_provider_lock ||
      echo "WARNING: provider lock ownership changed; operator reconciliation required" >&2
  fi
  [[ "$HELD_LAUNCH_LOCK" -eq 0 ]] || rmdir "$LAUNCH_LOCK" 2>/dev/null || true
  if [[ "$OWNS_ACTIVE_RUN" -eq 1 ]]; then
    if [[ -d "$ACTIVE_RUN_FILE" && ! -L "$ACTIVE_RUN_FILE" &&
          -f "$ACTIVE_RUN_FILE/owner" && ! -L "$ACTIVE_RUN_FILE/owner" &&
          "$(cat "$ACTIVE_RUN_FILE/owner" 2>/dev/null)" == "$ACTIVE_RUN_EXPECTED" ]]; then
      rm -f "$ACTIVE_RUN_FILE/owner"
      rmdir "$ACTIVE_RUN_FILE" 2>/dev/null ||
        echo "WARNING: run claim gained unexpected entries; operator reconciliation required" >&2
    else
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
  "$FACTORY_ENVELOPE_CONFIG_KEYS" "$FACTORY_ENVELOPE_CONFIG_KEYS" || exit 3
PER_TICKET_BUDGET_USD="${PER_TICKET_BUDGET_USD:-$PER_RUN_BUDGET_USD}"

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
if ! factory_validate_ticket_kit_sha "$TICKET_FILE" "$FACTORY_KIT_SHA"; then
  echo "$FACTORY_TICKET_KIT_ERROR; no task was submitted" >&2
  exit 3
fi
TICKET_AFFINITY_WAS_MISSING=0
if ! factory_dispatch_require_lease "$REPO_ROOT" "$TICKET" "$DISPATCH_LEASE_ID"; then
  echo "$FACTORY_DISPATCH_LEASE_ERROR; no task was submitted" >&2
  exit 7
fi
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

# Serialize claim creation with kill-switch publication. Claims are mkdir
# locks and are never reclaimed automatically; operator recovery must inspect
# an abandoned owner record rather than guessing from a reusable PID.
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

# ponytail: serialize providers until an OS-enforced writer boundary can keep
# provider processes out of factory/runs while preserving parallel execution.
PROVIDER_LOCK_ATTEMPTS=$((PER_RUN_TIMEOUT_MIN * 600 + 100))
for i in $(seq 1 "$PROVIDER_LOCK_ATTEMPTS"); do
  if mkdir "$PROVIDER_LOCK" 2>/dev/null; then
    HELD_PROVIDER_LOCK=1
    PROVIDER_LOCK_TOKEN="$(python3 -c 'import secrets; print(secrets.token_hex(16))')"
    printf '%s\n' "$PROVIDER_LOCK_TOKEN" > "$PROVIDER_LOCK/owner" || exit 8
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
  sleep 0.1
done
if [[ "$HELD_PROVIDER_LOCK" -ne 1 ]]; then
  echo "provider lock stuck — no task was submitted" >&2
  exit 8
fi

GLOBAL_LEDGER_HEADER="date,time,repo,ticket,role,adapter,prompt_version,turns,cost_usd,exit_status,run_id,provider_family,model_id,selection_reason,cost_basis,adapter_version"
LEGACY_GLOBAL_HEADER="date,time,repo,ticket,role,adapter,prompt_version,turns,cost_usd,exit_status"
PARTIAL_GLOBAL_HEADER="$LEGACY_GLOBAL_HEADER,run_id,provider_family"
RUN_ID="$(date +%s)-$$"
MANIFEST="$RUNS_DIR/$RUN_ID.meta"
RUN_STARTED_AT="$(date -u +%FT%TZ)"
TODAY="${RUN_STARTED_AT%%T*}"
RUN_START_TIME="${RUN_STARTED_AT#*T}"; RUN_START_TIME="${RUN_START_TIME%Z}"
RESERVED_USD="$PER_RUN_BUDGET_USD"
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

# --- serialized cap check with budget reservation ---
# mkdir is atomic: it is the lock. Reservation counts this run's full per-run
# budget against the caps, so N concurrent runs cannot all squeeze past.
for i in $(seq 1 50); do mkdir "$LOCK_DIR" 2>/dev/null && { HELD_LEDGER_LOCK=1; break; }; sleep 0.2; [[ $i -eq 50 ]] && { echo "ledger lock stuck — see runbook" >&2; exit 8; }; done
if ! refresh_runtime_ledger; then
  echo "effective ledger could not be reduced; refusing launch" >&2
  exit 3
fi

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
  SPENT_GLOBAL="$(awk -F, -v d="$TODAY" 'NR>1 && $1==d {s+=$9} END {printf "%.4f", s+0}' "$GLOBAL_LEDGER")"
  if awk -v s="$SPENT_GLOBAL" -v r="$PER_RUN_BUDGET_USD" -v cap="$GLOBAL_DAILY_CAP_USD" 'BEGIN{exit !((s+r)>cap)}'; then
    echo "MACHINE daily cap would be exceeded across all factories (spent \$$SPENT_GLOBAL + reserve \$$PER_RUN_BUDGET_USD > \$$GLOBAL_DAILY_CAP_USD) — refusing. See docs/runbooks/operator.md." >&2
    release_global_lock || true
    rmdir "$LOCK_DIR"; HELD_LEDGER_LOCK=0; exit 5
  fi
  {
    cat "$GLOBAL_LEDGER"
    echo "$TODAY,$RUN_START_TIME,$REPO_ROOT,$TICKET,$ROLE,$ADAPTER,reserved,0,$PER_RUN_BUDGET_USD,reserved-$RUN_ID,$RUN_ID,$LEDGER_FAMILY,$LEDGER_MODEL,$LEDGER_REASON,conservative_reservation,$LEDGER_VERSION"
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

# Reserve in the per-run manifest, then materialize the ignored runtime view.
# A crash after GO leaves the full conservative reservation in force.
write_manifest "reserved"
if ! refresh_runtime_ledger; then
  echo "effective ledger could not be materialized; refusing launch" >&2
  exit 3
fi
rmdir "$LOCK_DIR"; HELD_LEDGER_LOCK=0

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
rm -f "$RUN_READY_FILE" "$RUN_GO_FILE" "$RUN_GATE_FILE"
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
RUN_OUTPUT_TEMP="$(mktemp "$RUNS_DIR/.$RUN_ID.output.XXXXXX")" || {
  echo "could not allocate wrapper-owned output capture" >&2
  exit 125
}
exec 8< "$RUN_OUTPUT_TEMP"
exec 9> "$RUN_OUTPUT_TEMP"
rm -f "$RUN_OUTPUT_TEMP"
RUN_OUTPUT_TEMP=""
python3 "$KIT_DIR/scripts/lib/run-in-process-group.py" \
  "$RUN_READY_FILE" "$RUN_GATE_FILE" "$ADAPTER_SH" \
  "${ADAPTER_ARGS[@]}" \
  -- "$TASK" 8<&- >&9 2>&1 &
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
    else
      GO_ISSUED=1
      if ! write_manifest "spawned"; then
        GO_ISSUED=0
        echo "could not persist GO marker; no task was submitted" >&2
        terminate_run_group
        wait "$RUN_PID" 2>/dev/null
        STATUS=125
      elif ! python3 "$KIT_DIR/scripts/lib/durable-file.py" touch "$RUN_GO_FILE"; then
        GO_ISSUED=0
        echo "could not persist GO marker; no task was submitted" >&2
        terminate_run_group
        wait "$RUN_PID" 2>/dev/null
        STATUS=125
      elif ! RUNS_META_SNAPSHOT="$(python3 "$KIT_DIR/scripts/lib/runs-integrity.py" \
          snapshot "$RUNS_DIR")"; then
        GO_ISSUED=0
        echo "could not snapshot run manifests; no task was submitted" >&2
        terminate_run_group
        wait "$RUN_PID" 2>/dev/null
        STATUS=125
      elif ! REGISTERED_BRANCH_BEFORE="$("$FACTORY_TRUSTED_GIT_BIN" -C "$REPO_ROOT" \
          symbolic-ref --quiet --short HEAD 2>/dev/null)" ||
           ! REGISTERED_HEAD_BEFORE="$("$FACTORY_TRUSTED_GIT_BIN" -C "$REPO_ROOT" \
          rev-parse HEAD 2>/dev/null)" ||
           ! REGISTERED_STATUS_BEFORE="$("$FACTORY_TRUSTED_GIT_BIN" -C "$REPO_ROOT" \
          status --porcelain --untracked-files=all 2>/dev/null)" ||
           ! REGISTERED_CONTENT_BEFORE="$(registered_tracked_content 2>/dev/null)"; then
        GO_ISSUED=0
        echo "could not snapshot registered checkout; no task was submitted" >&2
        terminate_run_group
        wait "$RUN_PID" 2>/dev/null
        STATUS=125
      elif ! ACTIVE_RUN_SNAPSHOT="$(active_claim_snapshot 2>/dev/null)"; then
        GO_ISSUED=0
        echo "could not bind run claim ownership; no task was submitted" >&2
        terminate_run_group
        wait "$RUN_PID" 2>/dev/null
        STATUS=125
      elif ! : > "$RUN_GATE_FILE"; then
        GO_ISSUED=0
        echo "could not open adapter GO gate; no task was submitted" >&2
        terminate_run_group
        wait "$RUN_PID" 2>/dev/null
        STATUS=125
      else
        rmdir "$LAUNCH_LOCK"
        HELD_LAUNCH_LOCK=0
        wait "$RUN_PID"
        STATUS=$?
        if ! printf '%s' "$RUNS_META_SNAPSHOT" | \
            python3 "$KIT_DIR/scripts/lib/runs-integrity.py" check "$RUNS_DIR"; then
          CONTROL_PLANE_MUTATION=1
          STATUS=11
        fi
        if [[ "$(active_claim_snapshot 2>/dev/null || true)" != "$ACTIVE_RUN_SNAPSHOT" ]]; then
          echo "role_exit_control_plane_mutation: run claim changed during provider execution" >&2
          CONTROL_PLANE_MUTATION=1
          STATUS=11
        fi
        if ! provider_lock_is_owned; then
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
        if [[ "$("$FACTORY_TRUSTED_GIT_BIN" -C "$REPO_ROOT" symbolic-ref --quiet --short HEAD 2>/dev/null || true)" != "$REGISTERED_BRANCH_BEFORE" ||
              "$("$FACTORY_TRUSTED_GIT_BIN" -C "$REPO_ROOT" rev-parse HEAD 2>/dev/null || true)" != "$REGISTERED_HEAD_BEFORE" ||
              "$("$FACTORY_TRUSTED_GIT_BIN" -C "$REPO_ROOT" status --porcelain --untracked-files=all 2>/dev/null || true)" != "$REGISTERED_STATUS_BEFORE" ||
              "$(registered_tracked_content 2>/dev/null || true)" != "$REGISTERED_CONTENT_BEFORE" ]]; then
          echo "role_exit_control_plane_mutation: registered checkout changed during provider execution" >&2
          CONTROL_PLANE_MUTATION=1
          STATUS=11
        fi
      fi
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
rm -f "$RUN_READY_FILE" "$RUN_GO_FILE" "$RUN_GATE_FILE"
RUN_READY_FILE=""
RUN_GO_FILE=""
RUN_GATE_FILE=""
exec 9>&-
RESULT="$(cat <&8)"
exec 8<&-
printf '%s\n' "$RESULT" | \
  python3 "$KIT_DIR/scripts/lib/durable-file.py" write "$RUNS_DIR/$RUN_ID.out"

PROVIDER_STATUS="$STATUS"
if [[ "$CONTROL_PLANE_MUTATION" -eq 1 ]]; then
  ROLE_EXIT_STATUS="role_exit_control_plane_mutation"
elif [[ "$ROLE_EXIT_ENFORCED" -eq 1 ]]; then
  ROLE_BRANCH_AFTER="$("$FACTORY_TRUSTED_GIT_BIN" -C "$WORKDIR" symbolic-ref --quiet --short HEAD 2>/dev/null || true)"
  ROLE_HEAD_AFTER="$("$FACTORY_TRUSTED_GIT_BIN" -C "$WORKDIR" rev-parse HEAD 2>/dev/null || true)"
  ROLE_DIRTY="$("$FACTORY_TRUSTED_GIT_BIN" -C "$WORKDIR" status --porcelain --untracked-files=all 2>/dev/null || true)"
  ROLE_PROTECTED_AFTER="$(ticket_evidence_snapshot "$TICKET_FILE" 2>/dev/null)" ||
    ROLE_PROTECTED_AFTER="__invalid__"
  if [[ "$PROVIDER_STATUS" -eq 0 ]]; then
    if [[ "$ROLE_BRANCH_AFTER" != "$ROLE_BRANCH_BEFORE" ]]; then
      ROLE_EXIT_STATUS="role_exit_wrong_branch"
    elif [[ "$ROLE" == "reviewer" &&
            ( -n "$ROLE_DIRTY" || "$ROLE_HEAD_AFTER" != "$ROLE_HEAD_BEFORE" ) ]]; then
      ROLE_EXIT_STATUS="reviewer_mutated_worktree"
    elif [[ "$ROLE_PROTECTED_AFTER" == "__invalid__" ]] ||
         ! ticket_evidence_is_legal "$ROLE_PROTECTED_BEFORE" \
           "$ROLE_PROTECTED_AFTER" "$ROLE"; then
      ROLE_EXIT_STATUS="role_exit_protected_ticket_mutation"
    elif ! factory_product_remote_matches "$REPO_ROOT" "$PRODUCT_REMOTE"; then
      ROLE_EXIT_STATUS="role_exit_remote_mismatch"
    elif [[ "$ROLE" == "reviewer" &&
            "$(role_remote_head || true)" != "$ROLE_REMOTE_BEFORE" ]]; then
      ROLE_EXIT_STATUS="role_exit_remote_mismatch"
    elif [[ -n "$ROLE_DIRTY" ]]; then
      ROLE_EXIT_STATUS="role_exit_dirty"
    elif [[ "$ROLE" == "reviewer" ]]; then
      ROLE_EXIT_STATUS="ok"
    elif [[ "$ROLE_HEAD_AFTER" == "$ROLE_HEAD_BEFORE" ]]; then
      ROLE_EXIT_STATUS="role_exit_no_commit"
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
          ROLE_EXIT_STATUS="ok"
        fi
      fi
    fi
    if [[ "$ROLE_EXIT_STATUS" != "ok" ]]; then
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

METRICS_LINE="$(printf '%s\n' "$RESULT" | tail -n1)"
TURNS="$(awk '{ for (i=1; i<=NF; i++) if ($i ~ /^turns=/) { sub(/^turns=/, "", $i); print $i; exit } }' <<<"$METRICS_LINE")"
COST="$(awk '{ for (i=1; i<=NF; i++) if ($i ~ /^cost_usd=/) { sub(/^cost_usd=/, "", $i); print $i; exit } }' <<<"$METRICS_LINE")"
COST_BASIS="$(awk '{ for (i=1; i<=NF; i++) if ($i ~ /^cost_basis=/) { sub(/^cost_basis=/, "", $i); print $i; exit } }' <<<"$METRICS_LINE")"
TELEMETRY_INVALID=0
if [[ -z "$TURNS" ]]; then
  TURNS=0
elif [[ ! "$TURNS" =~ ^[0-9]{1,4}$ ]] ||
   ! awk -v value="$TURNS" 'BEGIN { exit !(value >= 0 && value <= 1000) }'; then
  TELEMETRY_INVALID=1
fi
if [[ -n "$COST" ]] &&
   { [[ ! "$COST" =~ ^[0-9]{1,7}([.][0-9]{1,18})?$ ]] ||
     ! awk -v value="$COST" 'BEGIN { exit !(value >= 0 && value <= 1000000) }'; }; then
  TELEMETRY_INVALID=1
fi
if [[ "$TELEMETRY_INVALID" -eq 1 ]]; then
  echo "WARNING: adapter telemetry invalid or oversized — keeping the full reservation and zero turns." >&2
  COST=""
  TURNS=0
  COST_BASIS=""
fi
if [[ "$GO_ISSUED" -eq 0 ]]; then
  COST="0"
  TURNS="0"
  COST_BASIS="launch_void"
  FINAL_ACCOUNTING_STATE="launch_void"
elif [[ -z "$COST" ]]; then
  echo "WARNING: run cost unparsable — ledger keeps conservative reservation of \$$PER_RUN_BUDGET_USD for this run. Reconcile with the provider console." >&2
  COST="$PER_RUN_BUDGET_USD"
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

if ! provider_lock_is_owned; then
  echo "role_exit_control_plane_mutation: provider lock changed before terminal accounting" >&2
  CONTROL_PLANE_MUTATION=1
  ROLE_EXIT_STATUS="role_exit_control_plane_mutation"
  STATUS=11
fi
finalize_accounting "$FINAL_ACCOUNTING_STATE" "$COST" "${TURNS:-0}" "$STATUS" "$COST_BASIS" "completed"

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

finalize_global_ledger
release_provider_lock || {
  echo "WARNING: provider lock ownership changed after terminal accounting; operator reconciliation required" >&2
  STATUS=11
}

printf '%s\n' "$RESULT"
exit "$STATUS"
