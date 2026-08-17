#!/usr/bin/env bash
# Task-free model routing control for the sealed launcher.
set -euo pipefail

unset FACTORY_GITHUB_TOKEN_FD GH_TOKEN GITHUB_TOKEN GH_ENTERPRISE_TOKEN
unset GITHUB_ENTERPRISE_TOKEN GH_HOST GH_CONFIG_DIR
export GH_PROMPT_DISABLED=1

KIT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
# shellcheck disable=SC1091
source "$KIT_DIR/scripts/lib/plain-config.sh"
# shellcheck disable=SC1091
source "$KIT_DIR/scripts/lib/kit-pin.sh"
# shellcheck disable=SC1091
source "$KIT_DIR/scripts/lib/backend-policy.sh"
# shellcheck disable=SC1091
source "$KIT_DIR/scripts/lib/product-remote.sh"
# shellcheck disable=SC1091
source "$KIT_DIR/scripts/lib/dispatch-leases.sh"

unset FACTORY_TRUSTED_PRODUCT_ORIGIN
readonly FACTORY_TRUSTED_PRODUCT_ORIGIN="${FACTORY_CERTIFIED_PRODUCT_ORIGIN:-}"
unset FACTORY_CERTIFIED_PRODUCT_ORIGIN

PIN_PRECOMMIT=0
PIN_WORKDIR=""
PIN_TICKET_RELATIVE=""
PIN_PLAN_RELATIVE=""
PIN_PLAN_EXISTED=0
TEMPORARY_FILE=""
TEMPORARY_READINESS_FILE=""
TEMPORARY_DIR=""
FALLBACK_LAUNCH_LOCK=""
CONTROL_GITHUB_HELPER=""
CONTROL_GITHUB_CONFIG_DIR=""

cleanup() {
  local rc=$?
  [[ -z "$TEMPORARY_FILE" ]] || rm -f "$TEMPORARY_FILE"
  [[ -z "$TEMPORARY_READINESS_FILE" ]] || rm -f "$TEMPORARY_READINESS_FILE"
  [[ -z "$TEMPORARY_DIR" ]] || rm -rf "$TEMPORARY_DIR"
  [[ -z "$FALLBACK_LAUNCH_LOCK" ]] || rmdir "$FALLBACK_LAUNCH_LOCK" 2>/dev/null || true
  if [[ "$PIN_PRECOMMIT" -eq 1 && -n "$PIN_WORKDIR" ]]; then
    git -C "$PIN_WORKDIR" restore --staged --worktree -- \
      "$PIN_TICKET_RELATIVE" >/dev/null 2>&1 || true
    if [[ "$PIN_PLAN_EXISTED" -eq 1 ]]; then
      git -C "$PIN_WORKDIR" restore --staged --worktree -- \
        "$PIN_PLAN_RELATIVE" >/dev/null 2>&1 || true
    fi
    if [[ "$PIN_PLAN_EXISTED" -eq 0 ]]; then
      rm -f "$PIN_WORKDIR/$PIN_PLAN_RELATIVE"
      rmdir "$PIN_WORKDIR/factory/route-plans" >/dev/null 2>&1 || true
    fi
  fi
  exit "$rc"
}
trap cleanup EXIT

json_error() {
  python3 - "$1" <<'PY'
import json
import sys
print(json.dumps({"error": sys.argv[1], "status": "error"},
                 ensure_ascii=False, sort_keys=True, separators=(",", ":")))
PY
  exit 2
}

json_resolution_error() {
  local operation="$1" reason="$2" profile_id="$3" readiness="$4" prefix
  case "$operation" in
    plan) prefix="model plan failed" ;;
    pin) prefix="model pin resolution failed" ;;
    *) json_error "model resolution failed" ;;
  esac
  [[ "$reason" =~ ^[a-z0-9][a-z0-9._-]{0,127}$ ]] || reason="resolution_error"
  [[ "$profile_id" =~ ^[a-z0-9][a-z0-9._-]{0,127}$ ]] || profile_id="unknown"
  python3 -B - "$prefix" "$reason" "$profile_id" "$readiness" <<'PY'
import json
import os
import pathlib
import re
import stat
import sys

prefix, reason, profile_id, readiness_path = sys.argv[1:]
safe_id = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}\Z")
safe_text = re.compile(r"[^\x00-\x1f\x7f]{0,500}\Z")
sensitive = re.compile(
    r"(?i)[A-Za-z0-9_.-]*(?:key|token|secret|password|url|dsn|conn|auth)"
    r"[A-Za-z0-9_.-]*\s*[:=]"
)
url = re.compile(r"(?i)\b[A-Za-z][A-Za-z0-9+.-]*://")
states = {"READY", "UNAVAILABLE", "INVALID", "UNKNOWN"}
fields = {"adapter_version", "reason", "reported_identity", "state"}


def no_duplicates(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError
        value[key] = item
    return value


readiness = {}
try:
    path = pathlib.Path(readiness_path)
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_nlink != 1
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_size > 1_000_000
    ):
        raise ValueError
    with path.open(encoding="utf-8") as handle:
        candidate = json.load(handle, object_pairs_hook=no_duplicates)
    if not isinstance(candidate, dict) or not 1 <= len(candidate) <= 64:
        raise ValueError
    for route_id, value in candidate.items():
        if (
            not isinstance(route_id, str)
            or not safe_id.fullmatch(route_id)
            or not isinstance(value, dict)
            or set(value) != fields
            or value.get("state") not in states
            or not isinstance(value.get("reason"), str)
            or not safe_id.fullmatch(value["reason"])
            or any(
                not isinstance(value.get(name), str)
                or not safe_text.fullmatch(value[name])
                or url.search(value[name])
                or sensitive.search(value[name])
                for name in ("adapter_version", "reported_identity")
            )
        ):
            raise ValueError
    readiness = candidate
except (OSError, TypeError, UnicodeError, ValueError, json.JSONDecodeError):
    readiness = {}

print(json.dumps({
    "error": f"{prefix}: {reason}",
    "profile_id": profile_id,
    "readiness": readiness,
    "reason_code": reason,
    "schema": "nysa.software-factory.model-resolution-error/v1",
    "status": "error",
}, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
PY
  exit 2
}

[[ "${FACTORY_MODEL_STATE_ROOT:-}" == /* ]] ||
  json_error "FACTORY_MODEL_STATE_ROOT must be an absolute path"
[[ -d "$FACTORY_MODEL_STATE_ROOT" && ! -L "$FACTORY_MODEL_STATE_ROOT" ]] ||
  json_error "FACTORY_MODEL_STATE_ROOT must be an existing physical directory"
[[ -n "${FACTORY_PROJECT:-}" ]] ||
  json_error "FACTORY_PROJECT is required"
OPERATOR_MAP="${FACTORY_OPERATOR_MAP:-$FACTORY_ROOT/factory/operator-map.json}"

manager() {
  local -a policy_args=()
  [[ -z "${FACTORY_MODEL_POLICY_FILE:-}" ]] ||
    policy_args=(--policy-file "$FACTORY_MODEL_POLICY_FILE")
  python3 -B "$FACTORY_MODEL_MANAGER" "$1" \
    --state-root "$FACTORY_MODEL_STATE_ROOT" --project "$FACTORY_PROJECT" \
    --catalog "$FACTORY_MODEL_CATALOG" \
    --profiles-file "$FACTORY_MODEL_PROFILES" "${policy_args[@]}" "${@:2}"
}

load_machine_config() {
  local global_env="${FACTORY_GLOBAL_ENV:-${HOME:-}/.factory/global.env}"
  factory_clear_plain_config_keys "$FACTORY_GLOBAL_CONFIG_KEYS"
  if [[ -f "$global_env" ]] &&
     ! factory_load_plain_config "$global_env" global \
       "$FACTORY_GLOBAL_CONFIG_KEYS" "" 1; then
    json_error "machine model configuration is unsafe or malformed"
  fi
  factory_validate_runtime_overrides ||
    json_error "$FACTORY_RUNTIME_OVERRIDE_ERROR"
}

validate_control_workdir() {
  local ticket="$1" workdir="$2" allow_dirty="${3:-0}"
  local physical git_top branch descriptor prefix_data prefix_count prefix_value
  [[ "$ticket" =~ ^T-[0-9]+$ ]] || json_error "ticket must match T-NNN"
  [[ "$workdir" == /* && "${FACTORY_ROOT:-}" == /* ]] ||
    json_error "workdir and FACTORY_ROOT must be absolute"
  physical="$(cd "$workdir" 2>/dev/null && pwd -P)" ||
    json_error "workdir is unavailable"
  [[ "$physical" == "$workdir" ]] || json_error "workdir must be physical"
  git_top="$(git -C "$workdir" rev-parse --show-toplevel 2>/dev/null)" ||
    json_error "workdir is not a git worktree"
  git_top="$(cd "$git_top" && pwd -P)"
  [[ "$git_top" == "$workdir" ]] || json_error "workdir must be the exact worktree root"
  branch="$(git -C "$workdir" symbolic-ref --quiet --short HEAD 2>/dev/null)" ||
    json_error "ticket worktree must be on a branch"
  CONTROL_BRANCH_PREFIX="ticket/"
  descriptor="$FACTORY_ROOT/factory/PROJECT.env"
  if [[ -e "$descriptor" || -L "$descriptor" ]]; then
    [[ -f "$descriptor" && ! -L "$descriptor" ]] ||
      json_error "product project descriptor is unsafe"
    prefix_data="$(awk '
      /^[[:space:]]*#/ { next }
      {
        line=$0
        sub(/^[[:space:]]*export[[:space:]]+/, "", line)
        if (line ~ /^TICKET_BRANCH_PREFIX[[:space:]]*=/) {
          count++
          sub(/^TICKET_BRANCH_PREFIX[[:space:]]*=[[:space:]]*/, "", line)
          sub(/[[:space:]]+$/, "", line)
          if ((line ~ /^".*"$/) || (line ~ /^'\''.*'\''$/))
            line=substr(line, 2, length(line)-2)
          value=line
        }
      }
      END { printf "%d\t%s\n", count+0, value }
    ' "$descriptor")" || json_error "ticket branch prefix cannot be parsed"
    IFS="$(printf '\t')" read -r prefix_count prefix_value <<EOF
$prefix_data
EOF
    case "$prefix_count" in
      0) ;;
      1) CONTROL_BRANCH_PREFIX="$prefix_value" ;;
      *) json_error "ticket branch prefix must be defined at most once" ;;
    esac
  fi
  [[ "$branch" == "$CONTROL_BRANCH_PREFIX$ticket" ]] ||
    json_error "worktree branch does not match the requested ticket"
  if [[ "$allow_dirty" -ne 1 ]]; then
    [[ -z "$(git -C "$workdir" status --porcelain --untracked-files=all \
      --ignore-submodules=none)" ]] || json_error "ticket worktree must be clean"
  fi
  CONTROL_WORKDIR="$workdir"
  CONTROL_BRANCH="$branch"
  CONTROL_TICKET_FILE="$workdir/factory/tickets/$ticket.md"
  CONTROL_PLAN_FILE="$workdir/factory/route-plans/$ticket.json"
  [[ -f "$CONTROL_TICKET_FILE" && ! -L "$CONTROL_TICKET_FILE" ]] ||
    json_error "ticket file is missing or unsafe"
  CONTROL_REMOTE="$(factory_capture_product_remote \
    "$workdir" "$FACTORY_TRUSTED_PRODUCT_ORIGIN")" ||
    json_error "${FACTORY_PRODUCT_REMOTE_ERROR:-certified origin validation failed}"
}

prepare_github_git_auth() {
  [[ "$CONTROL_REMOTE" == https://github.com/* ]] || return 0
  local auth_capability auth_rc candidate candidate_path
  candidate="${FACTORY_TEST_GITHUB_HELPER:-}"
  if [[ -n "$candidate" ]]; then
    [[ "${FACTORY_TEST_MODE:-}" == "1" &&
       "${FACTORY_TRUSTED_TEST_HARNESS:-}" == "1" ]] ||
      json_error "github credential helper test override is forbidden"
  else
    for candidate_path in /opt/homebrew/bin/gh /usr/local/bin/gh /usr/bin/gh; do
      if [[ -x "$candidate_path" ]]; then
        candidate="$candidate_path"
        break
      fi
    done
  fi
  unset FACTORY_TEST_GITHUB_HELPER
  [[ -n "$candidate" ]] || json_error "github credential helper is unavailable"
  if auth_capability="$(python3 -I -S - "$candidate" "${HOME:-}" <<'PY'
import os, pathlib, re, stat, subprocess, sys

candidate, home_raw = sys.argv[1:]
path = os.path.realpath(candidate)
try:
    metadata = os.stat(path)
    parent = os.stat(os.path.dirname(path))
    home = pathlib.Path(home_raw)
    home_metadata = home.lstat()
except OSError:
    raise SystemExit(3)
if (
    not path.startswith("/")
    or not re.fullmatch(r"/[A-Za-z0-9_./+-]+", path)
    or not stat.S_ISREG(metadata.st_mode)
    or metadata.st_nlink != 1
    or metadata.st_uid not in {0, os.geteuid()}
    or stat.S_IMODE(metadata.st_mode) & 0o022
    or not os.access(path, os.X_OK)
    or not stat.S_ISDIR(parent.st_mode)
    or parent.st_uid not in {0, os.geteuid()}
    or stat.S_IMODE(parent.st_mode) & 0o022
    or not home.is_absolute()
    or home.is_symlink()
    or home.resolve() != home
    or not stat.S_ISDIR(home_metadata.st_mode)
    or home_metadata.st_uid != os.geteuid()
    or stat.S_IMODE(home_metadata.st_mode) & 0o022
):
    raise SystemExit(3)
config_dir = str(home / ".config" / "gh")
try:
    config_parent = home / ".config"
    config = config_parent / "gh"
    hosts = config / "hosts.yml"
    for directory in (config_parent, config):
        value = directory.lstat()
        if (
            directory.is_symlink() or not stat.S_ISDIR(value.st_mode)
            or value.st_uid != os.geteuid()
            or stat.S_IMODE(value.st_mode) & 0o022
        ):
            raise OSError
    value = hosts.lstat()
    if (
        hosts.is_symlink() or not stat.S_ISREG(value.st_mode)
        or value.st_uid != os.geteuid() or value.st_nlink != 1
        or stat.S_IMODE(value.st_mode) & 0o077
    ):
        raise OSError
except OSError:
    raise SystemExit(3)
try:
    result = subprocess.run(
        [path, "auth", "status", "--active", "--hostname", "github.com"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=15,
        env={
            "GH_CONFIG_DIR": config_dir,
            "GH_PROMPT_DISABLED": "1",
            "HOME": str(home),
            "PATH": "/usr/bin:/bin",
        },
    )
except (OSError, subprocess.TimeoutExpired):
    raise SystemExit(4)
if result.returncode:
    raise SystemExit(4)
print(path)
print(config_dir)
PY
  )"; then
    CONTROL_GITHUB_HELPER="${auth_capability%%$'\n'*}"
    CONTROL_GITHUB_CONFIG_DIR="${auth_capability#*$'\n'}"
  else
    auth_rc=$?
    [[ "$auth_rc" -ne 3 ]] || json_error "github credential helper is unsafe"
    json_error "github_credential_unavailable"
  fi
}

fallback_python() {
  if [[ -n "$CONTROL_GITHUB_HELPER" ]]; then
    python3 -B "$KIT_DIR/scripts/model-fallback.py" "$@" \
      --github-helper "$CONTROL_GITHUB_HELPER"
  else
    python3 -B "$KIT_DIR/scripts/model-fallback.py" "$@"
  fi
}

control_git() {
  local workdir="$1"
  shift
  if [[ -n "$CONTROL_GITHUB_HELPER" ]]; then
    GH_CONFIG_DIR="$CONTROL_GITHUB_CONFIG_DIR" GH_PROMPT_DISABLED=1 \
      git -C "$workdir" \
      -c credential.helper= \
      -c "credential.https://github.com.helper=!$CONTROL_GITHUB_HELPER auth git-credential" \
      "$@"
  else
    git -C "$workdir" "$@"
  fi
}

git_network_error() {
  local ordinary="$1"
  [[ -z "$CONTROL_GITHUB_HELPER" ]] || json_error "github_https_authentication_failed"
  json_error "$ordinary"
}

push_exact_head() {
  local workdir="$1" branch="$2" remote="$3" expected_old="$4"
  local head tracking actual
  head="$(git -C "$workdir" rev-parse HEAD)" || json_error "cannot resolve commit"
  tracking="$(factory_remote_tracking_tip "$workdir" "$branch")"
  [[ "$tracking" == "$expected_old" ]] ||
    json_error "remote tracking state changed before push"
  control_git "$workdir" push \
    "--force-with-lease=refs/heads/$branch:$expected_old" \
    "$remote" "$head:refs/heads/$branch" >/dev/null 2>&1 ||
    git_network_error "could not push exact model-control commit"
  actual="$(control_git "$workdir" ls-remote --heads -- "$remote" \
    "refs/heads/$branch" 2>/dev/null | awk 'NR==1 {print $1; exit}')"
  [[ "$actual" == "$head" ]] || git_network_error "remote verification failed"
  factory_update_tracking_ref "$workdir" "$branch" "$head" "$tracking" ||
    json_error "remote tracking update failed"
  printf '%s\n' "$head"
}

remote_branch_head() {
  local branch="$1" lines
  lines="$(control_git "$CONTROL_WORKDIR" ls-remote --heads -- "$CONTROL_REMOTE" \
    "refs/heads/$branch" 2>/dev/null)" || return 1
  python3 - "$branch" "$lines" <<'PY'
import re, sys
branch, raw = sys.argv[1:]
lines = raw.splitlines()
if len(lines) != 1:
    raise SystemExit(1)
fields = lines[0].split()
if len(fields) != 2 or not re.fullmatch(r"[0-9a-f]{40}", fields[0]):
    raise SystemExit(1)
if fields[1] != "refs/heads/" + branch:
    raise SystemExit(1)
print(fields[0])
PY
}

validate_inflight_migration_authority() {
  local current_head tracking_ticket tracking_main actual_ticket expected_parent
  local qualification_head
  current_head="$(git -C "$CONTROL_WORKDIR" rev-parse HEAD)" ||
    json_error "cannot resolve ticket head"
  tracking_ticket="$(factory_remote_tracking_tip \
    "$CONTROL_WORKDIR" "$CONTROL_BRANCH")"
  actual_ticket="$(remote_branch_head "$CONTROL_BRANCH")" ||
    git_network_error "remote ticket head lookup failed"
  CONTROL_PROTECTED_MAIN="$(remote_branch_head main)" ||
    git_network_error "protected main head lookup failed"
  tracking_main="$(factory_remote_tracking_tip "$CONTROL_WORKDIR" main)"
  [[ "$tracking_main" == "$CONTROL_PROTECTED_MAIN" ]] ||
    json_error "protected main tracking state is stale"
  CONTROL_AUTHORIZATION_REF="$CONTROL_PROTECTED_MAIN"
  if [[ "${FACTORY_KIT_TRUST_SCOPE:-}" == "qualification-candidate" ]]; then
    [[ "${FACTORY_QUALIFICATION_PRODUCT_SHA:-}" =~ ^[0-9a-f]{40}$ ]] ||
      json_error "qualification product authorization is invalid"
    qualification_head="$(git -C "$FACTORY_ROOT" rev-parse HEAD)" ||
      json_error "qualification product authorization is unavailable"
    [[ "$qualification_head" == "$FACTORY_QUALIFICATION_PRODUCT_SHA" ]] ||
      json_error "qualification product authorization changed"
    CONTROL_AUTHORIZATION_REF="$FACTORY_QUALIFICATION_PRODUCT_SHA"
  fi
  CONTROL_AUTHORIZATION_MODE="$(python3 -B \
    "$KIT_DIR/scripts/lib/inflight_release.py" \
    --repo "$CONTROL_WORKDIR" --protected "$CONTROL_AUTHORIZATION_REF" \
    --target "$FACTORY_KIT_SHA" --ticket "$ticket" \
    --branch "$CONTROL_BRANCH" --head "$current_head")" ||
    json_error "ticket does not match its exact protected in-flight release authorization"
  [[ "$CONTROL_AUTHORIZATION_MODE" == "exact" ||
     "$CONTROL_AUTHORIZATION_MODE" == "replay" ]] ||
    json_error "in-flight release authorization result is invalid"
  [[ "$tracking_ticket" =~ ^[0-9a-f]{40}$ ]] ||
    json_error "ticket remote tracking state is unavailable"
  CONTROL_OBSERVED_TRACKING_HEAD="$tracking_ticket"
  CONTROL_AUTHORIZED_LOCAL_HEAD="$current_head"
  if [[ "$CONTROL_AUTHORIZATION_MODE" == "exact" ]]; then
    if [[ "$actual_ticket" == "$current_head" ]]; then
      CONTROL_EXPECTED_REMOTE_HEAD="$current_head"
    elif git -C "$CONTROL_WORKDIR" merge-base --is-ancestor \
           "$actual_ticket" "$current_head"; then
      CONTROL_EXPECTED_REMOTE_HEAD="$actual_ticket"
    else
      json_error "ticket head is not a fast-forward of its certified remote"
    fi
  else
    expected_parent="$(git -C "$CONTROL_WORKDIR" rev-parse "$current_head^")" ||
      json_error "authorized migration parent is unavailable"
    if [[ "$actual_ticket" == "$expected_parent" ]]; then
      CONTROL_EXPECTED_REMOTE_HEAD="$expected_parent"
    elif [[ "$actual_ticket" == "$current_head" ]]; then
      CONTROL_EXPECTED_REMOTE_HEAD="$current_head"
    else
      json_error "authorized migration child is not current on its certified remote"
    fi
  fi
}

recheck_inflight_migration_authority() {
  local current_head tracking_ticket tracking_main actual_ticket protected authority
  local qualification_head worktree_status
  current_head="$(git -C "$CONTROL_WORKDIR" rev-parse HEAD)" ||
    json_error "cannot resolve ticket head"
  tracking_ticket="$(factory_remote_tracking_tip \
    "$CONTROL_WORKDIR" "$CONTROL_BRANCH")"
  tracking_main="$(factory_remote_tracking_tip "$CONTROL_WORKDIR" main)"
  actual_ticket="$(remote_branch_head "$CONTROL_BRANCH")" ||
    git_network_error "remote ticket head lookup failed"
  protected="$(remote_branch_head main)" ||
    git_network_error "protected main head lookup failed"
  [[ "$current_head" == "$CONTROL_AUTHORIZED_LOCAL_HEAD" &&
     "$tracking_ticket" == "$CONTROL_EXPECTED_REMOTE_HEAD" &&
     "$actual_ticket" == "$CONTROL_EXPECTED_REMOTE_HEAD" ]] ||
    json_error "ticket authorization changed before migration"
  [[ "$protected" == "$CONTROL_PROTECTED_MAIN" ]] ||
    json_error "protected in-flight release authorization changed before migration"
  [[ "$tracking_main" == "$CONTROL_PROTECTED_MAIN" ]] ||
    json_error "protected main tracking state changed before migration"
  if [[ "${FACTORY_KIT_TRUST_SCOPE:-}" == "qualification-candidate" ]]; then
    qualification_head="$(git -C "$FACTORY_ROOT" rev-parse HEAD)" ||
      json_error "qualification product authorization is unavailable"
    [[ "$qualification_head" == "$CONTROL_AUTHORIZATION_REF" ]] ||
      json_error "qualification product authorization changed before migration"
  fi
  authority="$(python3 -B "$KIT_DIR/scripts/lib/inflight_release.py" \
    --repo "$CONTROL_WORKDIR" --protected "$CONTROL_AUTHORIZATION_REF" \
    --target "$FACTORY_KIT_SHA" --ticket "$ticket" \
    --branch "$CONTROL_BRANCH" --head "$current_head")" ||
    json_error "ticket authorization evidence changed before migration"
  [[ "$authority" == "$CONTROL_AUTHORIZATION_MODE" ]] ||
    json_error "ticket authorization evidence changed before migration"
  worktree_status="$(git -C "$CONTROL_WORKDIR" status --porcelain \
    --untracked-files=all --ignore-submodules=none)" ||
    json_error "ticket worktree status is unavailable"
  [[ -z "$worktree_status" ]] ||
    json_error "ticket worktree changed during migration readiness"
}

command_name="${1:-}"
[[ -n "$command_name" ]] || json_error "a model-control command is required"
shift

case "$command_name" in
  qualification-readiness)
    [[ $# -eq 0 ]] || json_error "qualification-readiness takes no arguments"
    load_machine_config
    factory_load_model_probe_context ||
      json_error "model state is invalid: ${FACTORY_RESOLVE_ERROR:-unknown}"
    TEMPORARY_DIR="$(mktemp -d "$FACTORY_MODEL_STATE_ROOT/.qualification-readiness.XXXXXX")" ||
      json_error "could not allocate qualification readiness"
    resolution="$TEMPORARY_DIR/resolution.json"
    readiness="$TEMPORARY_DIR/readiness.json"
    factory_resolve_model_profile "$FACTORY_MODEL_PROFILE_ID" "$resolution" \
      "$FACTORY_DISABLED_ROUTE_IDS" "$readiness" ||
      json_error "qualification model resolution failed: ${FACTORY_RESOLVE_ERROR:-unknown}"
    python3 -B "$KIT_DIR/scripts/model-fallback-readiness.py" \
      --plan "$resolution" --readiness "$readiness" \
      --catalog "$FACTORY_MODEL_CATALOG" --profiles "$FACTORY_MODEL_PROFILES"
    ;;
  inventory)
    [[ $# -eq 0 ]] || json_error "inventory accepts no arguments"
    load_machine_config
    cursor_source_home="${FACTORY_CURSOR_SESSION_HOME:-$HOME}"
    cursor_bin="${CURSOR_AGENT_BIN:-agent}"
    probe_timeout="${FACTORY_PROBE_TIMEOUT_SEC:-30}"
    command -v timeout >/dev/null 2>&1 ||
      json_error "cursor model inventory requires timeout"
    cursor_bin="$(type -P "$cursor_bin" 2>/dev/null)" ||
      json_error "cursor model inventory executable is unavailable"
    [[ -n "${CURSOR_AGENT_VERSION:-}" ]] ||
      json_error "cursor model inventory version is not approved"
    TEMPORARY_DIR="$(mktemp -d "${TMPDIR:-/tmp}/factory-model-inventory.XXXXXX")" ||
      json_error "could not allocate isolated cursor model inventory"
    chmod 700 "$TEMPORARY_DIR"
    version_output="$(mktemp "$TEMPORARY_DIR/.version.XXXXXX")" ||
      json_error "could not allocate cursor version output"
    chmod 600 "$version_output"
    HOME="$TEMPORARY_DIR" timeout "$probe_timeout" "$cursor_bin" --version \
      >"$version_output" 2>/dev/null ||
      json_error "cursor model inventory version is not approved"
    python3 - "$version_output" "$CURSOR_AGENT_VERSION" <<'PY' ||
import os
import stat
import sys

path, expected = sys.argv[1:]
descriptor = -1
try:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    before = os.fstat(descriptor)
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_uid != os.geteuid()
        or before.st_nlink != 1
        or stat.S_IMODE(before.st_mode) != 0o600
        or before.st_size > 256
    ):
        raise OSError
    raw = os.read(descriptor, 257)
    after = os.fstat(descriptor)
    if (
        (before.st_dev, before.st_ino, before.st_mode, before.st_size,
         before.st_mtime_ns)
        != (after.st_dev, after.st_ino, after.st_mode, after.st_size,
            after.st_mtime_ns)
        or raw != f"Cursor Agent {expected}\n".encode()
    ):
        raise OSError
except (OSError, UnicodeError):
    raise SystemExit(1)
finally:
    if descriptor >= 0:
        os.close(descriptor)
PY
      json_error "cursor model inventory version is not approved"
    rm -f "$version_output"
    credential_reason=""
    if ! credential_reason="$(factory_prepare_cursor_probe_home \
        "$cursor_source_home" "$TEMPORARY_DIR")"; then
      json_error "cursor model inventory refused: ${credential_reason:-credential_invalid}"
    fi
    inventory_output="$(mktemp "$TEMPORARY_DIR/.models.XXXXXX")" ||
      json_error "could not allocate cursor model inventory output"
    chmod 600 "$inventory_output"
    HOME="$TEMPORARY_DIR" timeout "$probe_timeout" "$cursor_bin" models \
      >"$inventory_output" 2>/dev/null ||
      json_error "cursor model inventory probe failed"
    python3 - "$inventory_output" <<'PY' ||
import json
import os
import pathlib
import re
import stat
import sys

path = pathlib.Path(sys.argv[1])
try:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    before = os.fstat(descriptor)
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_uid != os.geteuid()
        or before.st_nlink != 1
        or stat.S_IMODE(before.st_mode) != 0o600
        or before.st_size > 1_000_000
    ):
        raise OSError
    chunks = []
    remaining = 1_000_001
    while remaining:
        chunk = os.read(descriptor, min(65_536, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    raw = b"".join(chunks)
    after = os.fstat(descriptor)
    if (
        (before.st_dev, before.st_ino, before.st_mode, before.st_size,
         before.st_mtime_ns)
        != (after.st_dev, after.st_ino, after.st_mode, after.st_size,
            after.st_mtime_ns)
    ):
        raise OSError
except OSError:
    raise SystemExit(1)
finally:
    if "descriptor" in locals() and descriptor >= 0:
        os.close(descriptor)
if len(raw) > 1_000_000:
    raise SystemExit(1)
try:
    text = raw.decode("utf-8", errors="strict")
except UnicodeError:
    raise SystemExit(1)
ansi = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
lines = [ansi.sub("", value) for value in text.splitlines()]
normalized = "\n".join(lines)
credential = re.compile(
    r"(?i)(?:"
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----|"
    r"github_pat_[A-Za-z0-9_]{20,}|"
    r"gh[opusr]_[A-Za-z0-9]{20,}|"
    r"sk-(?:proj-)?[A-Za-z0-9_-]{20,}|"
    r"xox[baprs]-[A-Za-z0-9-]{10,}|"
    r"AKIA[0-9A-Z]{16}|"
    r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\."
    r"[A-Za-z0-9_-]{10,})"
)
tip = (
    "Tip: use --model <id> (or /model <id> in interactive mode) to switch. "
    "Parameterized models also accept quoted overrides, e.g. --model "
    "'claude-opus-4-8[context=1m,effort=high,fast=false]'."
)
if (
    not text.endswith("\n")
    or len(lines) < 5
    or lines[:2] != ["Available models", ""]
    or lines[-2:] != ["", tip]
    or re.search(
        r"(?i)https?://|authorization|api[_-]?key|token|secret|password",
        normalized,
    )
    or credential.search(normalized)
):
    raise SystemExit(1)
model_id = re.compile(r"[a-z0-9][a-z0-9._:/+-]{0,127}")
label = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._:/()+-]{0,199}")
models = []
for line in lines[2:-2]:
    if not line or line != line.strip():
        raise SystemExit(1)
    for suffix in (" (current, default)", " (current)", " (default)"):
        if line.endswith(suffix):
            line = line[:-len(suffix)]
            break
    if re.search(r" \([^)]*\)$", line):
        raise SystemExit(1)
    fields = line.split(" - ", 1)
    if (
        not model_id.fullmatch(fields[0])
        or len(fields) > 1 and not label.fullmatch(fields[1])
        or fields[0] in models
    ):
        raise SystemExit(1)
    models.append(fields[0])
if not models or len(models) > 500:
    raise SystemExit(1)
models.sort()
print(json.dumps({
    "count": len(models),
    "models": models,
    "schema": "factory-cursor-model-inventory/v1",
    "status": "ok",
}, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
PY
      json_error "cursor model inventory returned unsafe or invalid output"
    ;;
  profiles|status|policy-candidates|reviewer-exception-contract)
    manager "$command_name" "$@" || json_error "$command_name failed"
    ;;
  policy-preview|policy-apply)
    [[ "${FACTORY_ROOT:-}" == /* ]] ||
      json_error "FACTORY_ROOT must be an absolute path"
    product_root="$(cd "$FACTORY_ROOT" 2>/dev/null && pwd -P)" ||
      json_error "FACTORY_ROOT is unavailable"
    [[ "$product_root" == "$FACTORY_ROOT" ]] ||
      json_error "FACTORY_ROOT must be physical"
    git_top="$(git -C "$FACTORY_ROOT" rev-parse --show-toplevel 2>/dev/null)" ||
      json_error "FACTORY_ROOT must be a git worktree"
    git_top="$(cd "$git_top" && pwd -P)"
    [[ "$git_top" == "$FACTORY_ROOT" ]] ||
      json_error "FACTORY_ROOT must be the exact worktree root"
    expected_policy="$FACTORY_ROOT/factory/model-policy.json"
    [[ "$FACTORY_MODEL_POLICY_FILE" == "$expected_policy" ]] ||
      json_error "model policy path must be product-owned factory/model-policy.json"
    manager "$command_name" "$@" || json_error "$command_name failed"
    ;;
  ticket-status)
    ticket=""
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --ticket) [[ $# -ge 2 ]] || json_error "--ticket requires a value"; ticket="$2"; shift 2 ;;
        *) json_error "unknown ticket-status argument: $1" ;;
      esac
    done
    [[ "$ticket" =~ ^T-[0-9]+$ ]] || json_error "ticket must match T-NNN"
    [[ "${FACTORY_ROOT:-}" == /* ]] ||
      json_error "FACTORY_ROOT must be an absolute path"
    ticket_file="$FACTORY_ROOT/factory/tickets/$ticket.md"
    ticket_plan="$FACTORY_ROOT/factory/route-plans/$ticket.json"
    manager ticket-status --ticket "$ticket" --ticket-file "$ticket_file" \
      --ticket-plan "$ticket_plan" || json_error "ticket-status failed"
    ;;
  activate|disable|enable)
    manager "$command_name" "$@" || json_error "$command_name failed"
    ;;
  plan)
    profile=""
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --profile)
          [[ $# -ge 2 ]] || json_error "--profile requires a value"
          profile="$2"
          shift 2
          ;;
        *) json_error "unknown plan argument: $1" ;;
      esac
    done
    load_machine_config
    factory_load_model_probe_context ||
      json_error "model state is invalid: ${FACTORY_RESOLVE_ERROR:-unknown}"
    [[ -z "$profile" ]] || FACTORY_MODEL_PROFILE_ID="$profile"
    resolution="$(mktemp "$FACTORY_MODEL_STATE_ROOT/.model-control-plan.XXXXXX")" ||
      json_error "could not allocate plan output"
    TEMPORARY_FILE="$resolution"
    readiness="$(mktemp "$FACTORY_MODEL_STATE_ROOT/.model-control-readiness.XXXXXX")" ||
      json_error "could not allocate plan readiness"
    TEMPORARY_READINESS_FILE="$readiness"
    factory_resolve_model_profile "$FACTORY_MODEL_PROFILE_ID" "$resolution" \
      "$FACTORY_DISABLED_ROUTE_IDS" "$readiness" ||
      json_resolution_error plan "${FACTORY_RESOLVE_ERROR:-unknown}" \
        "$FACTORY_MODEL_PROFILE_ID" "$readiness"
    cat "$resolution"
    ;;
  migrate-batch-plan|migrate-batch)
    batch_approve_hash="" batch_approved_by=""
    batch_tickets=()
    batch_workdirs=()
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --ticket)
          [[ $# -ge 4 && "$3" == "--workdir" ]] ||
            json_error "migration batch requires ticket/workdir pairs"
          [[ "$2" =~ ^T-[0-9]+$ ]] || json_error "ticket must match T-NNN"
          for existing in "${batch_tickets[@]-}"; do
            [[ "$existing" != "$2" ]] ||
              json_error "migration batch tickets must be unique"
          done
          batch_tickets+=("$2")
          batch_workdirs+=("$4")
          shift 4
          ;;
        --approve-hash)
          [[ $# -ge 2 && -z "$batch_approve_hash" ]] ||
            json_error "migration batch approval hash is duplicated"
          batch_approve_hash="$2"
          shift 2
          ;;
        --approved-by)
          [[ $# -ge 2 && -z "$batch_approved_by" ]] ||
            json_error "migration batch approver is duplicated"
          batch_approved_by="$2"
          shift 2
          ;;
        *) json_error "unknown migration batch argument: $1" ;;
      esac
    done
    [[ "${FACTORY_RELEASE_CONTRACT_VERSION:-}" == "2.0.0" ]] ||
      json_error "migration batches require contract 2.0.0"
    factory_validate_kit_pin "$KIT_DIR" "$FACTORY_ROOT" ||
      json_error "$FACTORY_KIT_PIN_ERROR"
    [[ "${#batch_tickets[@]}" -ge 1 && "${#batch_tickets[@]}" -le 4 ]] ||
      json_error "migration batch requires one to four tickets"
    if [[ "$command_name" == "migrate-batch-plan" ]]; then
      [[ -z "$batch_approve_hash$batch_approved_by" ]] ||
        json_error "migration batch preview does not accept apply arguments"
    else
      [[ "$batch_approve_hash" =~ ^[0-9a-f]{64}$ ]] ||
        json_error "migration batch approval hash is invalid"
      [[ "$batch_approved_by" =~ ^[a-z0-9][a-z0-9._-]{0,127}$ &&
         "$batch_approved_by" != "auto" ]] ||
        json_error "migration batch approver is invalid"
      [[ "${FACTORY_CONTROLLER_STATE_DIR:-}" == /* ]] ||
        json_error "FACTORY_CONTROLLER_STATE_DIR is required for migration batch recovery"
    fi
    batch_args=()
    for index in "${!batch_tickets[@]}"; do
      validate_control_workdir "${batch_tickets[$index]}" "${batch_workdirs[$index]}"
      batch_args+=(
        --ticket-workdir "${batch_tickets[$index]}" "${batch_workdirs[$index]}"
      )
    done
    batch_capacity="$(factory_dispatch_max_tickets \
      "$FACTORY_ROOT" "$FACTORY_RELEASE_CONTRACT_VERSION")" ||
      json_error "$(factory_dispatch_capacity_error "$FACTORY_RELEASE_CONTRACT_VERSION")"
    batch_helper="$KIT_DIR/scripts/model-migration-batch.py"
    [[ -f "$batch_helper" && ! -L "$batch_helper" ]] ||
      json_error "sealed migration batch helper is missing or unsafe"
    if [[ "$command_name" == "migrate-batch-plan" ]]; then
      FACTORY_CERTIFIED_PRODUCT_ORIGIN="$FACTORY_TRUSTED_PRODUCT_ORIGIN" \
        python3 -B "$batch_helper" plan --control "$KIT_DIR/scripts/model-control.sh" \
        --factory-sha "$FACTORY_KIT_SHA" --capacity "$batch_capacity" \
        "${batch_args[@]}"
      exit $?
    fi
    FACTORY_CERTIFIED_PRODUCT_ORIGIN="$FACTORY_TRUSTED_PRODUCT_ORIGIN" \
      python3 -B "$batch_helper" apply \
      --control "$KIT_DIR/scripts/model-control.sh" \
      --factory-sha "$FACTORY_KIT_SHA" --capacity "$batch_capacity" \
      --approve-hash "$batch_approve_hash" --approved-by "$batch_approved_by" \
      --state-dir "$FACTORY_CONTROLLER_STATE_DIR" "${batch_args[@]}"
    exit $?
    ;;
  migrate-plan|migrate)
    ticket="" workdir="" approve_hash="" readiness_hash="" approved_by=""
    include_journal=0
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --ticket) [[ $# -ge 2 ]] || json_error "--ticket requires a value"; ticket="$2"; shift 2 ;;
        --workdir) [[ $# -ge 2 ]] || json_error "--workdir requires a value"; workdir="$2"; shift 2 ;;
        --approve-hash) [[ $# -ge 2 ]] || json_error "--approve-hash requires a value"; approve_hash="$2"; shift 2 ;;
        --readiness-hash) [[ $# -ge 2 ]] || json_error "--readiness-hash requires a value"; readiness_hash="$2"; shift 2 ;;
        --approved-by) [[ $# -ge 2 ]] || json_error "--approved-by requires a value"; approved_by="$2"; shift 2 ;;
        --include-journal) include_journal=1; shift ;;
        *) json_error "unknown migration argument: $1" ;;
      esac
    done
    [[ "${FACTORY_RELEASE_CONTRACT_VERSION:-}" == "1.4.0" ||
       "${FACTORY_RELEASE_CONTRACT_VERSION:-}" == "1.5.0" ||
       "${FACTORY_RELEASE_CONTRACT_VERSION:-}" == "1.6.0" ||
       "${FACTORY_RELEASE_CONTRACT_VERSION:-}" == "1.7.0" ||
       "${FACTORY_RELEASE_CONTRACT_VERSION:-}" == "1.8.0" ||
       "${FACTORY_RELEASE_CONTRACT_VERSION:-}" == "2.0.0" ]] ||
      json_error "route migration requires contract 1.4.0 or newer"
    if [[ "$command_name" == "migrate" ]]; then
      [[ "$approve_hash" =~ ^[0-9a-f]{64}$ ]] ||
        json_error "migration approval hash is invalid"
      [[ "$readiness_hash" =~ ^[0-9a-f]{64}$ ]] ||
        json_error "migration readiness hash is invalid"
      [[ "$approved_by" =~ ^[a-z0-9][a-z0-9._-]{0,127}$ &&
         "$approved_by" != "auto" ]] ||
        json_error "migration approver is invalid"
      [[ "$include_journal" -eq 0 ]] ||
        json_error "--include-journal is valid only for migration preview"
    fi
    validate_control_workdir "$ticket" "$workdir" 0
    [[ "$command_name" != "migrate" ]] || prepare_github_git_auth
    [[ -f "$CONTROL_PLAN_FILE" && ! -L "$CONTROL_PLAN_FILE" ]] ||
      json_error "ticket route document is missing or unsafe"
    factory_validate_kit_pin "$KIT_DIR" "$FACTORY_ROOT" ||
      json_error "$FACTORY_KIT_PIN_ERROR"
    if [[ "$command_name" == "migrate" ]]; then
      validate_inflight_migration_authority
    fi
    bundle="$workdir/factory/attestations/$ticket/bundle.json"
    if [[ -e "$bundle" || -L "$bundle" ]]; then
      [[ -f "$bundle" && ! -L "$bundle" ]] ||
        json_error "bundle attestation is unsafe"
      python3 - "$bundle" "$CONTROL_PLAN_FILE" "$FACTORY_KIT_SHA" \
        "$workdir" "$ticket" <<'PY' ||
import json, re, subprocess, sys
bundle = json.load(open(sys.argv[1]))
route = json.load(open(sys.argv[2]))
bundle_kit = bundle.get("kit_sha", "") if isinstance(bundle, dict) else ""
route_kit = route.get("kit_sha", "") if isinstance(route, dict) else ""
kits = (bundle_kit, route_kit)
if not all(re.fullmatch(r"[0-9a-f]{40}", item) for item in kits):
    raise SystemExit(2)
source_kit = route_kit
if bundle_kit != sys.argv[3] and route_kit == sys.argv[3]:
    parent = subprocess.run(
        ["git", "-C", sys.argv[4], "show",
         f"HEAD^:factory/route-plans/{sys.argv[5]}.json"],
        text=True, capture_output=True,
    )
    try:
        prior = json.loads(parent.stdout) if parent.returncode == 0 else None
    except json.JSONDecodeError:
        prior = None
    source_kit = prior.get("kit_sha", "") if isinstance(prior, dict) else ""
revisions = route.get("revisions") if isinstance(route, dict) else None
first = revisions[0].get("body") if (
    isinstance(revisions, list) and revisions
    and isinstance(revisions[0], dict)
) else None
historical = (
    bundle.get("schema") in {
        "nysa.software-factory.ticket-bundle/v1",
        "nysa.software-factory.ticket-bundle/v2",
    }
    and bundle.get("ticket") == route.get("ticket") == sys.argv[5]
    and route.get("schema") == "ticket-model-route-journal/v2"
    and isinstance(first, dict)
    and first.get("kind") == "migration"
    and first.get("old_kit_sha") == bundle_kit
    and first.get("legacy_plan_sha256") == bundle.get("route_plan_sha256")
    and re.fullmatch(r"[0-9a-f]{64}", bundle.get("route_plan_sha256", ""))
)
if bundle_kit != sys.argv[3] and not (
    bundle_kit == source_kit and source_kit != sys.argv[3]
    or historical
):
    raise SystemExit(1)
PY
      case "$?" in
        1) json_error "bundle attestation must be invalidated before route migration" ;;
        *) json_error "bundle attestation is malformed" ;;
      esac
    fi
    profile_id="$(python3 - "$CONTROL_PLAN_FILE" <<'PY'
import base64, json, sys
value = json.load(open(sys.argv[1]))
if value.get("schema") == "ticket-model-route-plan/v1":
    resolution = value["resolution"]
elif value.get("schema") == "ticket-model-route-journal/v2":
    for revision in reversed(value["revisions"]):
        body = revision["body"]
        if body["kind"] == "migration":
            resolution = json.loads(base64.b64decode(body["legacy_plan_b64"]))["resolution"]
            break
        if body["kind"] == "fallback" or "new_resolution" in body:
            resolution = body["new_resolution"]
            break
        if "prior_resolution" in body:
            resolution = body["prior_resolution"]
            break
else:
    raise SystemExit(2)
print(resolution["profile_id"])
PY
)" || json_error "route document cannot select its profile"
    load_machine_config
    factory_load_model_probe_context ||
      json_error "model state is invalid: ${FACTORY_RESOLVE_ERROR:-unknown}"
    readiness="$(mktemp "$FACTORY_MODEL_STATE_ROOT/.model-migration-readiness.XXXXXX")" ||
      json_error "could not allocate migration readiness output"
    TEMPORARY_FILE="$readiness"
    plan_probe="$(mktemp "$FACTORY_MODEL_STATE_ROOT/.model-migration-plan.XXXXXX")" ||
      json_error "could not allocate migration probe output"
    rm -f "$readiness"
    factory_resolve_model_profile "$profile_id" "$plan_probe" \
      "$FACTORY_DISABLED_ROUTE_IDS" "$readiness" >/dev/null 2>&1 || true
    rm -f "$plan_probe"
    [[ -s "$readiness" ]] || json_error "model migration readiness probes failed"
    pin_commit="$(git -C "$workdir" log -1 --format=%H -- \
      "factory/route-plans/$ticket.json")"
    [[ "$pin_commit" =~ ^[0-9a-f]{40}$ ]] ||
      json_error "route document has no committed provenance"
    commit_epoch="$(git -C "$workdir" show -s --format=%ct "$pin_commit")" ||
      json_error "cannot derive migration timestamp"
    migrated_at="$(python3 - "$commit_epoch" <<'PY'
import datetime as dt, sys
print(dt.datetime.fromtimestamp(int(sys.argv[1]), dt.timezone.utc)
      .replace(microsecond=0).isoformat().replace("+00:00", "Z"))
PY
)" || json_error "cannot normalize migration timestamp"
    if [[ "$include_journal" -eq 1 ]]; then
      preview="$(manager migrate-plan --ticket-plan "$CONTROL_PLAN_FILE" \
        --pin-commit "$pin_commit" --kit-sha "$FACTORY_KIT_SHA" \
        --migrated-at "$migrated_at" --readiness "$(cat "$readiness")" \
        --include-journal)" || json_error "route migration preview failed"
    else
      preview="$(manager migrate-plan --ticket-plan "$CONTROL_PLAN_FILE" \
        --pin-commit "$pin_commit" --kit-sha "$FACTORY_KIT_SHA" \
        --migrated-at "$migrated_at" --readiness "$(cat "$readiness")")" ||
        json_error "route migration preview failed"
    fi
    preview_values="$(python3 - "$preview" <<'PY'
import json, re, sys
value = json.loads(sys.argv[1])
digest = value.get("preview_hash", "")
readiness = value.get("readiness_sha256", "")
if not all(re.fullmatch(r"[0-9a-f]{64}", item) for item in (digest, readiness)):
    raise SystemExit(2)
print(digest + "\t" + readiness)
PY
)" || json_error "route migration preview is malformed"
    IFS=$'\t' read -r preview_hash preview_readiness_hash <<< "$preview_values"
    if [[ "$command_name" == "migrate-plan" ]]; then
      printf '%s\n' "$preview"
      exit 0
    fi
    [[ "$approve_hash" == "$preview_hash" ]] ||
      json_error "migration approval hash does not match preview"
    [[ "$readiness_hash" == "$preview_readiness_hash" ]] ||
      json_error "model migration readiness changed after approval"
    tracking_ticket="$(factory_remote_tracking_tip \
      "$CONTROL_WORKDIR" "$CONTROL_BRANCH")"
    if [[ "$tracking_ticket" != "$CONTROL_EXPECTED_REMOTE_HEAD" ]]; then
      [[ "$tracking_ticket" == "$CONTROL_OBSERVED_TRACKING_HEAD" ]] ||
        json_error "ticket remote tracking state changed before migration"
      factory_update_tracking_ref \
        "$CONTROL_WORKDIR" "$CONTROL_BRANCH" \
        "$CONTROL_EXPECTED_REMOTE_HEAD" "$tracking_ticket" ||
        json_error "ticket remote tracking state could not be refreshed"
      [[ "$(factory_remote_tracking_tip \
        "$CONTROL_WORKDIR" "$CONTROL_BRANCH")" \
        == "$CONTROL_EXPECTED_REMOTE_HEAD" ]] ||
        json_error "ticket remote tracking state could not be refreshed"
    fi
    expected_remote_head="$CONTROL_EXPECTED_REMOTE_HEAD"
    recheck_inflight_migration_authority
    PIN_PRECOMMIT=1
    PIN_WORKDIR="$workdir"
    PIN_TICKET_RELATIVE="factory/tickets/$ticket.md"
    PIN_PLAN_RELATIVE="factory/route-plans/$ticket.json"
    PIN_PLAN_EXISTED=1
    python3 - "$CONTROL_TICKET_FILE" "$FACTORY_KIT_SHA" <<'PY' ||
import pathlib, re, sys
path = pathlib.Path(sys.argv[1])
new = sys.argv[2]
text = path.read_text()
pattern = re.compile(r"^[ \t]*Kit-SHA:[ \t]*([0-9a-f]{40})[ \t]*$", re.M)
matches = list(pattern.finditer(text))
if len(matches) != 1:
    raise SystemExit(2)
path.write_text(text[:matches[0].start()] + "Kit-SHA: " + new +
                text[matches[0].end():])
PY
      json_error "ticket Kit-SHA migration failed"
    manager migrate --ticket-plan "$CONTROL_PLAN_FILE" \
      --pin-commit "$pin_commit" --kit-sha "$FACTORY_KIT_SHA" \
      --migrated-at "$migrated_at" --approve-hash "$approve_hash" \
      --readiness "$(cat "$readiness")" \
      --output "$CONTROL_PLAN_FILE" >/dev/null ||
      json_error "route journal migration failed"
    if git -C "$workdir" diff --quiet -- \
      "$PIN_TICKET_RELATIVE" "$PIN_PLAN_RELATIVE"; then
      PIN_PRECOMMIT=0
      commit_sha="$(push_exact_head "$workdir" "$CONTROL_BRANCH" \
        "$CONTROL_REMOTE" "$expected_remote_head")"
      python3 - "$preview" "$commit_sha" "$approved_by" <<'PY'
import json, sys
value = json.loads(sys.argv[1])
value.update(commit_sha=sys.argv[2], approved_by=sys.argv[3], recovered=True)
print(json.dumps(value, sort_keys=True, separators=(",", ":")))
PY
      exit 0
    fi
    git -C "$workdir" add -- "$PIN_TICKET_RELATIVE" "$PIN_PLAN_RELATIVE" ||
      json_error "could not stage route migration"
    git -C "$workdir" -c user.name="Software Factory" \
      -c user.email="factory@local" commit \
      -m "$ticket: migrate model route journal" -- \
      "$PIN_TICKET_RELATIVE" "$PIN_PLAN_RELATIVE" >/dev/null ||
      json_error "could not commit route migration"
    PIN_PRECOMMIT=0
    commit_sha="$(push_exact_head "$workdir" "$CONTROL_BRANCH" \
      "$CONTROL_REMOTE" "$expected_remote_head")"
    python3 - "$preview" "$commit_sha" "$approved_by" <<'PY'
import json, sys
value = json.loads(sys.argv[1])
value.update(commit_sha=sys.argv[2], approved_by=sys.argv[3])
print(json.dumps(value, sort_keys=True, separators=(",", ":")))
PY
    ;;
  fallback-plan|fallback|fallback-auto)
    ticket="" failed_run="" workdir="" reason="" allow_reviewer_family=""
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --ticket) [[ $# -ge 2 ]] || json_error "--ticket requires a value"; ticket="$2"; shift 2 ;;
        --failed-run) [[ $# -ge 2 ]] || json_error "--failed-run requires a value"; failed_run="$2"; shift 2 ;;
        --workdir) [[ $# -ge 2 ]] || json_error "--workdir requires a value"; workdir="$2"; shift 2 ;;
        --reason) [[ $# -ge 2 ]] || json_error "--reason requires a value"; reason="$2"; shift 2 ;;
        --allow-reviewer-family)
          [[ $# -ge 2 ]] || json_error "--allow-reviewer-family requires a value"
          allow_reviewer_family="$2"
          shift 2
          ;;
        *) json_error "unknown fallback argument: $1" ;;
      esac
    done
    [[ "${FACTORY_RELEASE_CONTRACT_VERSION:-}" == "1.4.0" ||
       "${FACTORY_RELEASE_CONTRACT_VERSION:-}" == "1.5.0" ||
       "${FACTORY_RELEASE_CONTRACT_VERSION:-}" == "1.6.0" ||
       "${FACTORY_RELEASE_CONTRACT_VERSION:-}" == "1.7.0" ||
       "${FACTORY_RELEASE_CONTRACT_VERSION:-}" == "1.8.0" ||
       "${FACTORY_RELEASE_CONTRACT_VERSION:-}" == "2.0.0" ]] ||
      json_error "mid-ticket fallback requires contract 1.4.0 or newer"
    if [[ "$command_name" == "fallback-auto" &&
          "${FACTORY_RELEASE_CONTRACT_VERSION:-}" != "1.7.0" &&
          "${FACTORY_RELEASE_CONTRACT_VERSION:-}" != "1.8.0" &&
          "${FACTORY_RELEASE_CONTRACT_VERSION:-}" != "2.0.0" ]]; then
      json_error "automatic qualification fallback requires contract 1.7.0"
    fi
    [[ "$failed_run" =~ ^[A-Za-z0-9._-]{1,200}$ ]] ||
      json_error "failed run identifier is invalid"
    [[ "$reason" == "credits_exhausted" || "$reason" == "provider_unavailable" ]] ||
      json_error "fallback reason is invalid"
    if [[ -n "$allow_reviewer_family" &&
          ! "$allow_reviewer_family" =~ ^[a-z0-9][a-z0-9._-]{0,127}$ ]]; then
      json_error "Reviewer exception family is invalid"
    fi
    fallback_exception_args=(
      --workdir "$workdir" --factory-root "$FACTORY_ROOT"
      --project "$FACTORY_PROJECT" --ticket "$ticket"
      --failed-run "$failed_run" --reason "$reason"
    )
    [[ -z "$allow_reviewer_family" ]] ||
      fallback_exception_args+=(--allow-reviewer-family "$allow_reviewer_family")
    validate_control_workdir "$ticket" "$workdir" 1
    prepare_github_git_auth
    [[ -f "$CONTROL_PLAN_FILE" && ! -L "$CONTROL_PLAN_FILE" ]] ||
      json_error "ticket route document is missing or unsafe"
    profile_id="$(python3 - "$CONTROL_PLAN_FILE" "$command_name" <<'PY'
import base64, json, sys
value = json.load(open(sys.argv[1]))
if value.get("schema") == "ticket-model-route-plan/v1" and sys.argv[2] == "fallback-auto":
    resolution = value["resolution"]
elif value.get("schema") == "ticket-model-route-journal/v2":
    for revision in reversed(value["revisions"]):
        body = revision["body"]
        if body["kind"] == "migration":
            resolution = json.loads(base64.b64decode(body["legacy_plan_b64"]))["resolution"]
            break
        if body["kind"] == "fallback" or "new_resolution" in body:
            resolution = body["new_resolution"]
            break
        if "prior_resolution" in body:
            resolution = body["prior_resolution"]
            break
else:
    raise SystemExit(2)
print(resolution["profile_id"])
PY
)" || json_error "route journal cannot select its profile"
    load_machine_config
    factory_load_model_probe_context ||
      json_error "model state is invalid: ${FACTORY_RESOLVE_ERROR:-unknown}"
    TEMPORARY_DIR="$(mktemp -d "$FACTORY_MODEL_STATE_ROOT/.model-fallback.XXXXXX")" ||
      json_error "could not allocate fallback readiness"
    readiness="$TEMPORARY_DIR/readiness.json"
    plan_probe="$TEMPORARY_DIR/plan.json"
    factory_resolve_model_profile "$profile_id" "$plan_probe" \
      "$FACTORY_DISABLED_ROUTE_IDS" "$readiness" >/dev/null 2>&1 || true
    [[ -s "$readiness" ]] || json_error "model readiness probes failed"
    fallback_readiness="$TEMPORARY_DIR/fallback-readiness.json"
    python3 -B "$KIT_DIR/scripts/model-fallback-readiness.py" \
      --plan "$plan_probe" --readiness "$readiness" \
      --catalog "$FACTORY_MODEL_CATALOG" --profiles "$FACTORY_MODEL_PROFILES" \
      > "$fallback_readiness" || true
    fallback_diagnostic="$(python3 - "$fallback_readiness" <<'PY'
import json, pathlib, sys
try:
    value = json.loads(pathlib.Path(sys.argv[1]).read_text())
except (OSError, json.JSONDecodeError):
    raise SystemExit(2)
if value.get("status") == "ready":
    raise SystemExit(0)
item = next((item for item in value.get("checks", []) if item.get("state") != "READY"), {})
route = item.get("fallback_route_id") or item.get("cursor_route_id") or "unknown"
expected = item.get("expected_version") or "unknown"
installed = item.get("installed_version") or "unknown"
reason = item.get("reason") if item.get("reason") in {
    "authentication_unavailable", "contract_mismatch", "executable_missing",
    "local_contract_ready", "same_family_native_fallback_missing",
    "version_mismatch", "version_probe_failed",
} else "invalid"
print(f"{route}:{reason}:expected={expected}:installed={installed}")
raise SystemExit(1)
PY
)" || json_error "fallback readiness refused:${fallback_diagnostic:-invalid}"
    rm -f "$plan_probe" "$fallback_readiness"
    if [[ "$command_name" == "fallback-plan" ]]; then
      preview_file="$(mktemp "$TEMPORARY_DIR/preview.XXXXXX")" ||
        json_error "could not allocate fallback preview"
      if ! fallback_python preview \
        "${fallback_exception_args[@]}" \
        --readiness "$readiness" --remote "$CONTROL_REMOTE" > "$preview_file"; then
        rm -f "$preview_file"
        json_error "fallback preview failed"
      fi
      cat "$preview_file"
      rm -f "$preview_file"
      exit 0
    fi
    if [[ "$command_name" == "fallback-auto" ]]; then
      launch_lock="$FACTORY_ROOT/factory/.launch.lock"
      provider_lock="$FACTORY_ROOT/factory/.provider.lock"
      ledger_lock="$FACTORY_ROOT/factory/.ledger.lock"
      mkdir "$launch_lock" 2>/dev/null || json_error "launch lock is busy"
      FALLBACK_LAUNCH_LOCK="$launch_lock"
      [[ ! -e "$provider_lock" && ! -L "$provider_lock" &&
         ! -e "$ledger_lock" && ! -L "$ledger_lock" ]] ||
        json_error "provider or accounting state is busy"
      expected_remote_head="$(factory_remote_tracking_tip "$workdir" "$CONTROL_BRANCH")"
      [[ "$expected_remote_head" =~ ^[0-9a-f]{40}$ ]] ||
        json_error "remote tracking state is unavailable"
      apply_file="$(mktemp "$TEMPORARY_DIR/apply.XXXXXX")" ||
        json_error "could not allocate fallback result"
      error_file="$(mktemp "$TEMPORARY_DIR/error.XXXXXX")" ||
        json_error "could not allocate fallback error"
      if ! fallback_python qualification-apply \
        --workdir "$workdir" --factory-root "$FACTORY_ROOT" \
        --project "$FACTORY_PROJECT" --ticket "$ticket" \
        --failed-run "$failed_run" --reason "$reason" \
        --readiness "$readiness" --remote "$CONTROL_REMOTE" \
        > "$apply_file" 2> "$error_file"; then
        reason_code="$(python3 -B "$KIT_DIR/scripts/lib/fallback_refusal.py" "$error_file")"
        rm -f "$apply_file"
        rm -f "$error_file"
        json_error "automatic qualification fallback refused:$reason_code"
      fi
      rm -f "$error_file"
      commit_sha="$(push_exact_head "$workdir" "$CONTROL_BRANCH" \
        "$CONTROL_REMOTE" "$expected_remote_head")"
      rmdir "$FALLBACK_LAUNCH_LOCK"
      FALLBACK_LAUNCH_LOCK=""
      python3 - "$apply_file" "$commit_sha" <<'PY'
import json, sys
value = json.load(open(sys.argv[1]))
value["commit_sha"] = sys.argv[2]
print(json.dumps(value, sort_keys=True, separators=(",", ":")))
PY
      rm -f "$apply_file"
      exit 0
    fi
    approval_file="$(mktemp "$TEMPORARY_DIR/approval.XXXXXX")" ||
      json_error "could not allocate approval input"
    approval_available=1
    if ! python3 -B "$KIT_DIR/scripts/lib/model-fallback-approval.py" read \
      --operator-map "$OPERATOR_MAP" \
      --ticket "$ticket" --failed-run "$failed_run" --reason "$reason" \
      > "$approval_file"; then
      approval_available=0
      : > "$approval_file"
    fi
    approval_hash=""
    if [[ "$approval_available" -eq 1 ]]; then
      approval_hash="$(python3 - "$approval_file" <<'PY'
import json, re, sys
value = json.load(open(sys.argv[1]))
digest = value.get("approval_hash", "")
if not re.fullmatch(r"[0-9a-f]{64}", digest):
    raise SystemExit(2)
print(digest)
PY
)" || json_error "fallback approval hash is invalid"
    fi
    launch_lock="$FACTORY_ROOT/factory/.launch.lock"
    provider_lock="$FACTORY_ROOT/factory/.provider.lock"
    ledger_lock="$FACTORY_ROOT/factory/.ledger.lock"
    mkdir "$launch_lock" 2>/dev/null || json_error "launch lock is busy"
    FALLBACK_LAUNCH_LOCK="$launch_lock"
    [[ ! -e "$provider_lock" && ! -L "$provider_lock" &&
       ! -e "$ledger_lock" && ! -L "$ledger_lock" ]] ||
      json_error "provider or accounting state is busy"
    expected_remote_head="$(factory_remote_tracking_tip "$workdir" "$CONTROL_BRANCH")"
    [[ "$expected_remote_head" =~ ^[0-9a-f]{40}$ ]] ||
      json_error "remote tracking state is unavailable"
    if [[ "$approval_available" -eq 0 ]]; then
      recovery_file="$(mktemp "$TEMPORARY_DIR/recovery.XXXXXX")" ||
        json_error "could not allocate fallback recovery result"
      if ! fallback_python recover \
        --workdir "$workdir" --factory-root "$FACTORY_ROOT" \
        --project "$FACTORY_PROJECT" --ticket "$ticket" \
        --failed-run "$failed_run" --reason "$reason" \
        --readiness "$readiness" --remote "$CONTROL_REMOTE" > "$recovery_file"; then
        rm -f "$recovery_file" "$approval_file"
        json_error "fallback recovery validation failed"
      fi
      recovery_values="$(python3 - "$recovery_file" <<'PY'
import json, re, sys
value = json.load(open(sys.argv[1]))
receipt = value.get("approval_receipt")
if (
    value.get("recovered") is not True
    or not isinstance(receipt, dict)
    or not re.fullmatch(r"[0-9a-f]{64}", receipt.get("approval_hash", ""))
    or not re.fullmatch(r"[0-9a-f]{64}", receipt.get("receipt_sha256", ""))
):
    raise SystemExit(2)
print(receipt["approval_hash"] + "\t" + receipt["receipt_sha256"])
PY
)" || {
        rm -f "$recovery_file" "$approval_file"
        json_error "exact unexpired or previously consumed fallback approval is required"
      }
      IFS=$'\t' read -r approval_hash approval_receipt_sha256 <<< "$recovery_values"
      python3 -B "$KIT_DIR/scripts/lib/model-fallback-approval.py" verify-consumed \
        --operator-map "$OPERATOR_MAP" \
        --ticket "$ticket" --failed-run "$failed_run" --reason "$reason" \
        --approval-hash "$approval_hash" --receipt-sha256 "$approval_receipt_sha256" \
        >/dev/null || {
          rm -f "$recovery_file" "$approval_file"
          json_error "committed fallback approval consumption is not recorded"
        }
      commit_sha="$(push_exact_head "$workdir" "$CONTROL_BRANCH" \
        "$CONTROL_REMOTE" "$expected_remote_head")"
      rmdir "$FALLBACK_LAUNCH_LOCK"
      FALLBACK_LAUNCH_LOCK=""
      python3 - "$recovery_file" "$commit_sha" <<'PY'
import json, sys
value = json.load(open(sys.argv[1]))
value["commit_sha"] = sys.argv[2]
value.pop("approval_receipt", None)
print(json.dumps(value, sort_keys=True, separators=(",", ":")))
PY
      rm -f "$recovery_file" "$approval_file"
      exit 0
    fi
    apply_file="$(mktemp "$TEMPORARY_DIR/apply.XXXXXX")" ||
      json_error "could not allocate fallback result"
    if ! fallback_python apply \
      "${fallback_exception_args[@]}" \
      --readiness "$readiness" --remote "$CONTROL_REMOTE" \
      --approval "$approval_file" > "$apply_file"; then
      rm -f "$apply_file" "$approval_file"
      json_error "fallback apply failed"
    fi
    [[ "${FACTORY_CONTROLLER_STATE_DIR:-}" == /* ]] ||
      json_error "FACTORY_CONTROLLER_STATE_DIR is required to consume a fallback approval"
    commit_sha="$(push_exact_head "$workdir" "$CONTROL_BRANCH" \
      "$CONTROL_REMOTE" "$expected_remote_head")"
    python3 -B "$KIT_DIR/scripts/lib/model-fallback-approval.py" consume \
    --operator-map "$OPERATOR_MAP" \
      --ticket "$ticket" --failed-run "$failed_run" --reason "$reason" \
      --approval-hash "$approval_hash" --state-dir "$FACTORY_CONTROLLER_STATE_DIR" >/dev/null ||
      json_error "fallback committed but operator approval receipt consumption requires reconciliation"
    rmdir "$FALLBACK_LAUNCH_LOCK"
    FALLBACK_LAUNCH_LOCK=""
    python3 - "$apply_file" "$commit_sha" <<'PY'
import json, sys
value = json.load(open(sys.argv[1]))
value["commit_sha"] = sys.argv[2]
print(json.dumps(value, sort_keys=True, separators=(",", ":")))
PY
    rm -f "$apply_file" "$approval_file"
    ;;
  pin-batch)
    tickets=()
    workdirs=()
    while [[ $# -gt 0 ]]; do
      [[ $# -ge 4 && "$1" == "--ticket" && "$3" == "--workdir" ]] ||
        json_error "pin-batch requires ticket/workdir pairs"
      [[ "$2" =~ ^T-[0-9]+$ ]] || json_error "ticket must match T-NNN"
      [[ "$4" == /* ]] || json_error "workdir must be absolute"
      for existing in "${tickets[@]-}"; do
        [[ "$existing" != "$2" ]] || json_error "pin-batch tickets must be unique"
      done
      tickets+=("$2")
      workdirs+=("$4")
      shift 4
    done
    [[ "${#tickets[@]}" -ge 1 && "${#tickets[@]}" -le 4 ]] ||
      json_error "pin-batch requires one to four tickets"
    for index in "${!tickets[@]}"; do
      validate_control_workdir "${tickets[$index]}" "${workdirs[$index]}"
    done
    load_machine_config
    factory_load_model_probe_context ||
      json_error "model state is invalid: ${FACTORY_RESOLVE_ERROR:-unknown}"
    resolution="$(mktemp "$FACTORY_MODEL_STATE_ROOT/.model-control-batch.XXXXXX")" ||
      json_error "could not allocate batch pin resolution"
    TEMPORARY_FILE="$resolution"
    readiness="$(mktemp "$FACTORY_MODEL_STATE_ROOT/.model-control-readiness.XXXXXX")" ||
      json_error "could not allocate batch pin readiness"
    TEMPORARY_READINESS_FILE="$readiness"
    factory_resolve_model_profile "$FACTORY_MODEL_PROFILE_ID" "$resolution" \
      "$FACTORY_DISABLED_ROUTE_IDS" "$readiness" ||
      json_resolution_error pin "${FACTORY_RESOLVE_ERROR:-unknown}" \
        "$FACTORY_MODEL_PROFILE_ID" "$readiness"
    pin_results=()
    for index in "${!tickets[@]}"; do
      pin_result="$(FACTORY_CERTIFIED_PRODUCT_ORIGIN="$FACTORY_TRUSTED_PRODUCT_ORIGIN" \
        FACTORY_INTERNAL_BATCH_RESOLUTION="$resolution" \
        /bin/bash "$KIT_DIR/scripts/model-control.sh" pin \
        --ticket "${tickets[$index]}" --workdir "${workdirs[$index]}")" ||
        json_error "batch ticket pin failed for ${tickets[$index]}"
      pin_results+=("$pin_result")
    done
    python3 - "${pin_results[@]}" <<'PY'
import json
import sys
print(json.dumps({
    "pins": [json.loads(value) for value in sys.argv[1:]],
    "schema": "model-pin-batch/v1",
    "status": "ok",
}, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
PY
    ;;
  pin)
    internal_resolution="${FACTORY_INTERNAL_BATCH_RESOLUTION:-}"
    unset FACTORY_INTERNAL_BATCH_RESOLUTION
    ticket=""
    workdir=""
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --ticket)
          [[ $# -ge 2 ]] || json_error "--ticket requires a value"
          ticket="$2"
          shift 2
          ;;
        --workdir)
          [[ $# -ge 2 ]] || json_error "--workdir requires a value"
          workdir="$2"
          shift 2
          ;;
        *) json_error "unknown pin argument: $1" ;;
      esac
    done
    [[ "$ticket" =~ ^T-[0-9]+$ ]] || json_error "ticket must match T-NNN"
    [[ "$workdir" == /* ]] || json_error "workdir must be absolute"
    [[ "${FACTORY_ROOT:-}" == /* ]] ||
      json_error "FACTORY_ROOT must be an absolute path"
    physical_workdir="$(cd "$workdir" 2>/dev/null && pwd -P)" ||
      json_error "workdir is unavailable"
    [[ "$physical_workdir" == "$workdir" ]] ||
      json_error "workdir must be a physical absolute path"
    git_top="$(git -C "$workdir" rev-parse --show-toplevel 2>/dev/null)" ||
      json_error "workdir is not a git worktree"
    git_top="$(cd "$git_top" && pwd -P)"
    [[ "$git_top" == "$workdir" ]] ||
      json_error "workdir must be the exact ticket worktree root"
    branch="$(git -C "$workdir" symbolic-ref --quiet --short HEAD 2>/dev/null)" ||
      json_error "ticket worktree must be on a non-detached branch"
    branch_prefix="ticket/"
    descriptor="$FACTORY_ROOT/factory/PROJECT.env"
    if [[ -e "$descriptor" || -L "$descriptor" ]]; then
      [[ -f "$descriptor" && ! -L "$descriptor" ]] ||
        json_error "product project descriptor is unsafe"
      prefix_data="$(awk '
        /^[[:space:]]*#/ { next }
        {
          line=$0
          sub(/^[[:space:]]*export[[:space:]]+/, "", line)
          if (line ~ /^TICKET_BRANCH_PREFIX[[:space:]]*=/) {
            count++
            sub(/^TICKET_BRANCH_PREFIX[[:space:]]*=[[:space:]]*/, "", line)
            sub(/[[:space:]]+$/, "", line)
            if ((line ~ /^".*"$/) || (line ~ /^'\''.*'\''$/))
              line=substr(line, 2, length(line)-2)
            value=line
          }
        }
        END { printf "%d\t%s\n", count+0, value }
      ' "$descriptor" 2>/dev/null)" ||
        json_error "product ticket branch prefix cannot be parsed"
      IFS="$(printf '\t')" read -r prefix_count prefix_value <<EOF
$prefix_data
EOF
      case "$prefix_count" in
        0) ;;
        1) branch_prefix="$prefix_value" ;;
        *) json_error "product ticket branch prefix must be defined at most once" ;;
      esac
    fi
    [[ "$branch_prefix" =~ ^[A-Za-z0-9][A-Za-z0-9._/-]*/$ ]] ||
      json_error "product ticket branch prefix is invalid"
    case "$branch_prefix" in
      *..*|*//*|*@\{*|*\\*|*~*|*^*|*:*)
        json_error "product ticket branch prefix is invalid"
        ;;
    esac
    expected_branch="$branch_prefix$ticket"
    git check-ref-format --branch "$expected_branch" >/dev/null 2>&1 ||
      json_error "ticket branch name is invalid"
    [[ "$branch" == "$expected_branch" ]] ||
      json_error "worktree branch does not match the requested ticket"
    [[ -z "$(git -C "$workdir" status --porcelain --untracked-files=all)" ]] ||
      json_error "ticket worktree must be clean before pinning"
    product_remote="$(factory_capture_product_remote \
      "$workdir" "$FACTORY_TRUSTED_PRODUCT_ORIGIN")" ||
      json_error "${FACTORY_PRODUCT_REMOTE_ERROR:-certified product origin validation failed}"
    ticket_file="$workdir/factory/tickets/$ticket.md"
    [[ -f "$ticket_file" ]] || json_error "ticket file is missing"
    factory_validate_kit_pin "$KIT_DIR" "$FACTORY_ROOT" ||
      json_error "$FACTORY_KIT_PIN_ERROR"
    factory_validate_ticket_kit_sha "$ticket_file" "$FACTORY_KIT_SHA" ||
      json_error "$FACTORY_TICKET_KIT_ERROR"
    output="$workdir/factory/route-plans/$ticket.json"
    ticket_relative="factory/tickets/$ticket.md"
    plan_relative="factory/route-plans/$ticket.json"
    ticket_had_pin=0
    [[ -z "${FACTORY_TICKET_KIT_SHA:-}" ]] || ticket_had_pin=1
    plan_existed=0
    if [[ -e "$output" || -L "$output" ]]; then
      plan_existed=1
      [[ "$ticket_had_pin" -eq 1 ]] ||
        json_error "partial ticket route pin exists without Kit-SHA affinity"
    fi

    if [[ "$plan_existed" -eq 1 ]]; then
      git -C "$workdir" ls-files --error-unmatch -- "$plan_relative" >/dev/null 2>&1 ||
        json_error "existing ticket route pin is not committed"
      git -C "$workdir" diff --quiet HEAD -- "$ticket_relative" "$plan_relative" ||
        json_error "existing ticket route pin has partial changes"
      # The manager validates the complete existing pin before consulting the
      # resolution source, making an exact committed pin idempotent.
      pin_json="$(manager pin --ticket "$ticket" --kit-sha "$FACTORY_KIT_SHA" \
        --resolution-file "$output" --output "$output")" ||
        json_error "ticket route pin is invalid or conflicts with an existing pin"
      commit_sha="$(git -C "$workdir" rev-parse HEAD)" ||
        json_error "could not resolve existing pin commit"
      factory_product_remote_matches "$workdir" "$product_remote" ||
        json_error "$FACTORY_PRODUCT_REMOTE_ERROR"
      tracking_sha="$(factory_remote_tracking_tip "$workdir" "$branch")"
      git -C "$workdir" push --no-force -- "$product_remote" \
        "$commit_sha:refs/heads/$branch" >/dev/null 2>&1 ||
        json_error "could not push exact existing ticket pin"
      remote_sha="$(git -C "$workdir" ls-remote --heads -- "$product_remote" \
        "refs/heads/$branch" 2>/dev/null | awk 'NR==1 {print $1; exit}')"
      [[ "$remote_sha" == "$commit_sha" ]] ||
        json_error "existing ticket pin remote verification failed"
      factory_update_tracking_ref "$workdir" "$branch" "$commit_sha" "$tracking_sha" ||
        json_error "existing ticket pin remote tracking update failed"
      pin_hash="$(python3 - "$output" <<'PY'
import hashlib
import sys
print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())
PY
)" || json_error "could not hash existing ticket route pin"
      python3 - "$pin_json" "$commit_sha" "$pin_hash" <<'PY'
import json
import sys
value = json.loads(sys.argv[1])
value.update(commit_created=False, commit_sha=sys.argv[2], pin_hash=sys.argv[3])
print(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
PY
      exit 0
    fi

    PIN_PRECOMMIT=1
    PIN_WORKDIR="$workdir"
    PIN_TICKET_RELATIVE="$ticket_relative"
    PIN_PLAN_RELATIVE="$plan_relative"
    PIN_PLAN_EXISTED="$plan_existed"
    if [[ "$ticket_had_pin" -eq 0 ]]; then
      factory_record_ticket_kit_sha "$ticket_file" "$FACTORY_KIT_SHA" ||
        json_error "$FACTORY_TICKET_KIT_ERROR"
    fi
    if [[ -n "$internal_resolution" ]]; then
      resolution_parent="$(cd "$(dirname "$internal_resolution")" 2>/dev/null && pwd -P)" ||
        json_error "batch pin resolution is unavailable"
      resolution="$resolution_parent/$(basename "$internal_resolution")"
      [[ "$resolution" == "$internal_resolution" &&
         "$resolution_parent" == "$(cd "$FACTORY_MODEL_STATE_ROOT" && pwd -P)" &&
         "$(basename "$resolution")" == .model-control-batch.* ]] ||
        json_error "batch pin resolution is outside model state"
      python3 - "$resolution" <<'PY' ||
import os
import pathlib
import stat
import sys

value = pathlib.Path(sys.argv[1]).lstat()
if (
    not stat.S_ISREG(value.st_mode)
    or value.st_uid != os.geteuid()
    or value.st_nlink != 1
    or stat.S_IMODE(value.st_mode) != 0o600
):
    raise SystemExit(1)
PY
        json_error "batch pin resolution is unsafe"
    else
      load_machine_config
      factory_load_model_probe_context ||
        json_error "model state is invalid: ${FACTORY_RESOLVE_ERROR:-unknown}"
      resolution="$(mktemp "$FACTORY_MODEL_STATE_ROOT/.model-control-pin.XXXXXX")" ||
        json_error "could not allocate pin resolution"
      TEMPORARY_FILE="$resolution"
      readiness="$(mktemp "$FACTORY_MODEL_STATE_ROOT/.model-control-readiness.XXXXXX")" ||
        json_error "could not allocate pin readiness"
      TEMPORARY_READINESS_FILE="$readiness"
      factory_resolve_model_profile "$FACTORY_MODEL_PROFILE_ID" "$resolution" \
        "$FACTORY_DISABLED_ROUTE_IDS" "$readiness" ||
        json_resolution_error pin "${FACTORY_RESOLVE_ERROR:-unknown}" \
          "$FACTORY_MODEL_PROFILE_ID" "$readiness"
    fi
    pin_json="$(manager pin --ticket "$ticket" --kit-sha "$FACTORY_KIT_SHA" \
      --resolution-file "$resolution" --output "$output")" ||
      json_error "ticket route pin is invalid or conflicts with an existing pin"
    git -C "$workdir" add -- "$ticket_relative" "$plan_relative" ||
      json_error "could not stage the ticket affinity and route plan"
    staged_paths="$(git -C "$workdir" diff --cached --name-only)" ||
      json_error "could not inspect staged pin changes"
    expected_staged="$plan_relative"
    if [[ "$ticket_had_pin" -eq 0 ]]; then
      expected_staged="$(printf '%s\n%s' "$plan_relative" "$ticket_relative" | LC_ALL=C sort)"
    fi
    [[ "$staged_paths" == "$expected_staged" ]] ||
      json_error "ticket pin produced partial or unexpected staged changes"
    git -C "$workdir" -c user.name="Software Factory" \
      -c user.email="factory@local" commit \
      -m "$ticket: pin kit and model route plan" -- \
      "$ticket_relative" "$plan_relative" >/dev/null ||
      json_error "could not commit ticket affinity and route plan"
    PIN_PRECOMMIT=0
    [[ -z "$(git -C "$workdir" status --porcelain --untracked-files=all)" ]] ||
      json_error "ticket pin commit left partial changes"
    factory_product_remote_matches "$workdir" "$product_remote" ||
      json_error "$FACTORY_PRODUCT_REMOTE_ERROR"
    commit_sha="$(git -C "$workdir" rev-parse HEAD)" ||
      json_error "could not resolve ticket pin commit"
    tracking_sha="$(factory_remote_tracking_tip "$workdir" "$branch")"
    git -C "$workdir" push --no-force -- "$product_remote" \
      "$commit_sha:refs/heads/$branch" >/dev/null 2>&1 ||
      json_error "could not push ticket pin commit"
    remote_sha="$(git -C "$workdir" ls-remote --heads -- "$product_remote" \
      "refs/heads/$branch" 2>/dev/null | awk 'NR==1 {print $1; exit}')"
    [[ "$remote_sha" == "$commit_sha" ]] ||
      json_error "ticket pin remote verification failed"
    factory_update_tracking_ref "$workdir" "$branch" "$commit_sha" "$tracking_sha" ||
      json_error "ticket pin remote tracking update failed"
    pin_hash="$(python3 - "$output" <<'PY'
import hashlib
import sys
print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())
PY
)" || json_error "could not hash ticket route pin"
    python3 - "$pin_json" "$commit_sha" "$pin_hash" <<'PY'
import json
import sys
value = json.loads(sys.argv[1])
value.update(commit_created=True, commit_sha=sys.argv[2], pin_hash=sys.argv[3])
print(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
PY
    ;;
  *) json_error "unknown model-control command: $command_name" ;;
esac
