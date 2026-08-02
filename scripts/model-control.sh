#!/usr/bin/env bash
# Task-free model routing control for the sealed launcher.
set -euo pipefail

KIT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
# shellcheck disable=SC1091
source "$KIT_DIR/scripts/lib/plain-config.sh"
# shellcheck disable=SC1091
source "$KIT_DIR/scripts/lib/kit-pin.sh"
# shellcheck disable=SC1091
source "$KIT_DIR/scripts/lib/backend-policy.sh"
# shellcheck disable=SC1091
source "$KIT_DIR/scripts/lib/product-remote.sh"

unset FACTORY_TRUSTED_PRODUCT_ORIGIN
readonly FACTORY_TRUSTED_PRODUCT_ORIGIN="${FACTORY_CERTIFIED_PRODUCT_ORIGIN:-}"
unset FACTORY_CERTIFIED_PRODUCT_ORIGIN

PIN_PRECOMMIT=0
PIN_WORKDIR=""
PIN_TICKET_RELATIVE=""
PIN_PLAN_RELATIVE=""
PIN_PLAN_EXISTED=0
TEMPORARY_FILE=""
TEMPORARY_FILE_2=""
FALLBACK_LAUNCH_LOCK=""

cleanup() {
  local rc=$?
  [[ -z "$TEMPORARY_FILE" ]] || rm -f "$TEMPORARY_FILE"
  [[ -z "$TEMPORARY_FILE_2" ]] || rm -f "$TEMPORARY_FILE_2"
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

[[ "${FACTORY_MODEL_STATE_ROOT:-}" == /* ]] ||
  json_error "FACTORY_MODEL_STATE_ROOT must be an absolute path"
[[ -d "$FACTORY_MODEL_STATE_ROOT" && ! -L "$FACTORY_MODEL_STATE_ROOT" ]] ||
  json_error "FACTORY_MODEL_STATE_ROOT must be an existing physical directory"
[[ -n "${FACTORY_PROJECT:-}" ]] ||
  json_error "FACTORY_PROJECT is required"
OPERATOR_MAP="${FACTORY_OPERATOR_MAP:-$FACTORY_ROOT/factory/linear-map.json}"

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

push_exact_head() {
  local workdir="$1" branch="$2" remote="$3" expected_old="$4"
  local head tracking actual
  head="$(git -C "$workdir" rev-parse HEAD)" || json_error "cannot resolve commit"
  tracking="$(factory_remote_tracking_tip "$workdir" "$branch")"
  [[ "$tracking" == "$expected_old" ]] ||
    json_error "remote tracking state changed before push"
  git -C "$workdir" push --no-force \
    "$remote" "$head:refs/heads/$branch" >/dev/null 2>&1 ||
    json_error "could not push exact model-control commit"
  actual="$(git -C "$workdir" ls-remote --heads -- "$remote" \
    "refs/heads/$branch" 2>/dev/null | awk 'NR==1 {print $1; exit}')"
  [[ "$actual" == "$head" ]] || json_error "remote verification failed"
  factory_update_tracking_ref "$workdir" "$branch" "$head" "$tracking" ||
    json_error "remote tracking update failed"
  printf '%s\n' "$head"
}

command_name="${1:-}"
[[ -n "$command_name" ]] || json_error "a model-control command is required"
shift

case "$command_name" in
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
    factory_resolve_model_profile "$FACTORY_MODEL_PROFILE_ID" "$resolution" \
      "$FACTORY_DISABLED_ROUTE_IDS" ||
      json_error "model plan failed: ${FACTORY_RESOLVE_ERROR:-unknown}"
    cat "$resolution"
    ;;
  migrate-plan|migrate)
    ticket="" workdir="" approve_hash="" approved_by=""
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --ticket) [[ $# -ge 2 ]] || json_error "--ticket requires a value"; ticket="$2"; shift 2 ;;
        --workdir) [[ $# -ge 2 ]] || json_error "--workdir requires a value"; workdir="$2"; shift 2 ;;
        --approve-hash) [[ $# -ge 2 ]] || json_error "--approve-hash requires a value"; approve_hash="$2"; shift 2 ;;
        --approved-by) [[ $# -ge 2 ]] || json_error "--approved-by requires a value"; approved_by="$2"; shift 2 ;;
        *) json_error "unknown migration argument: $1" ;;
      esac
    done
    [[ "${FACTORY_RELEASE_CONTRACT_VERSION:-}" == "1.4.0" ||
       "${FACTORY_RELEASE_CONTRACT_VERSION:-}" == "1.5.0" ||
       "${FACTORY_RELEASE_CONTRACT_VERSION:-}" == "1.6.0" ||
       "${FACTORY_RELEASE_CONTRACT_VERSION:-}" == "1.7.0" ||
       "${FACTORY_RELEASE_CONTRACT_VERSION:-}" == "1.8.0" ]] ||
      json_error "route migration requires contract 1.4.0 or newer"
    if [[ "$command_name" == "migrate" ]]; then
      [[ "$approve_hash" =~ ^[0-9a-f]{64}$ ]] ||
        json_error "migration approval hash is invalid"
      [[ "$approved_by" =~ ^[a-z0-9][a-z0-9._-]{0,127}$ &&
         "$approved_by" != "auto" ]] ||
        json_error "migration approver is invalid"
    fi
    validate_control_workdir "$ticket" "$workdir" 0
    [[ -f "$CONTROL_PLAN_FILE" && ! -L "$CONTROL_PLAN_FILE" ]] ||
      json_error "ticket route document is missing or unsafe"
    factory_validate_kit_pin "$KIT_DIR" "$FACTORY_ROOT" ||
      json_error "$FACTORY_KIT_PIN_ERROR"
    profile_id="$(python3 - "$CONTROL_PLAN_FILE" <<'PY'
import base64, json, sys
value = json.load(open(sys.argv[1]))
if value.get("schema") == "ticket-model-route-plan/v1":
    resolution = value["resolution"]
elif value.get("schema") == "ticket-model-route-journal/v2":
    body = value["revisions"][-1]["body"]
    if body["kind"] == "migration":
        resolution = json.loads(base64.b64decode(body["legacy_plan_b64"]))["resolution"]
    else:
        resolution = body.get("new_resolution", body["prior_resolution"])
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
    preview="$(manager migrate-plan --ticket-plan "$CONTROL_PLAN_FILE" \
      --pin-commit "$pin_commit" --kit-sha "$FACTORY_KIT_SHA" \
      --migrated-at "$migrated_at" --readiness "$(cat "$readiness")")" ||
      json_error "route migration preview failed"
    preview_hash="$(python3 - "$preview" <<'PY'
import json, re, sys
value = json.loads(sys.argv[1])
digest = value.get("preview_hash", "")
if not re.fullmatch(r"[0-9a-f]{64}", digest):
    raise SystemExit(2)
print(digest)
PY
)" || json_error "route migration preview is malformed"
    if [[ "$command_name" == "migrate-plan" ]]; then
      printf '%s\n' "$preview"
      exit 0
    fi
    [[ "$approve_hash" == "$preview_hash" ]] ||
      json_error "migration approval hash does not match preview"
    [[ "$approved_by" =~ ^[a-z0-9][a-z0-9._-]{0,127}$ && "$approved_by" != "auto" ]] ||
      json_error "migration approver is invalid"
    current_readiness="$(mktemp "$FACTORY_MODEL_STATE_ROOT/.model-migration-current.XXXXXX")" ||
      json_error "could not allocate current migration readiness output"
    TEMPORARY_FILE_2="$current_readiness"
    current_plan_probe="$(mktemp "$FACTORY_MODEL_STATE_ROOT/.model-migration-current-plan.XXXXXX")" ||
      json_error "could not allocate current migration probe output"
    rm -f "$current_readiness"
    factory_resolve_model_profile "$profile_id" "$current_plan_probe" \
      "$FACTORY_DISABLED_ROUTE_IDS" "$current_readiness" >/dev/null 2>&1 || true
    rm -f "$current_plan_probe"
    [[ -s "$current_readiness" ]] ||
      json_error "current model migration readiness probes failed"
    cmp -s "$readiness" "$current_readiness" ||
      json_error "model migration readiness changed after approval"
    rm -f "$current_readiness"
    TEMPORARY_FILE_2=""
    expected_remote_head="$(factory_remote_tracking_tip "$workdir" "$CONTROL_BRANCH")"
    [[ "$expected_remote_head" =~ ^[0-9a-f]{40}$ ]] ||
      json_error "remote tracking state is unavailable"
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
       "${FACTORY_RELEASE_CONTRACT_VERSION:-}" == "1.8.0" ]] ||
      json_error "mid-ticket fallback requires contract 1.4.0 or newer"
    if [[ "$command_name" == "fallback-auto" &&
          "${FACTORY_RELEASE_CONTRACT_VERSION:-}" != "1.7.0" &&
          "${FACTORY_RELEASE_CONTRACT_VERSION:-}" != "1.8.0" ]]; then
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
    fallback_exception_args=()
    [[ -z "$allow_reviewer_family" ]] ||
      fallback_exception_args=(--allow-reviewer-family "$allow_reviewer_family")
    validate_control_workdir "$ticket" "$workdir" 1
    [[ -f "$CONTROL_PLAN_FILE" && ! -L "$CONTROL_PLAN_FILE" ]] ||
      json_error "ticket route document is missing or unsafe"
    profile_id="$(python3 - "$CONTROL_PLAN_FILE" "$command_name" <<'PY'
import base64, json, sys
value = json.load(open(sys.argv[1]))
if value.get("schema") == "ticket-model-route-plan/v1" and sys.argv[2] == "fallback-auto":
    resolution = value["resolution"]
elif value.get("schema") == "ticket-model-route-journal/v2":
    body = value["revisions"][-1]["body"]
    if body["kind"] == "migration":
        plan = json.loads(base64.b64decode(body["legacy_plan_b64"]))
        resolution = plan["resolution"]
    else:
        resolution = body.get("new_resolution", body["prior_resolution"])
else:
    raise SystemExit(2)
print(resolution["profile_id"])
PY
)" || json_error "route journal cannot select its profile"
    load_machine_config
    factory_load_model_probe_context ||
      json_error "model state is invalid: ${FACTORY_RESOLVE_ERROR:-unknown}"
    readiness="$(mktemp "$FACTORY_MODEL_STATE_ROOT/.model-fallback-readiness.XXXXXX")" ||
      json_error "could not allocate readiness output"
    plan_probe="$(mktemp "$FACTORY_MODEL_STATE_ROOT/.model-fallback-plan.XXXXXX")" ||
      json_error "could not allocate probe output"
    TEMPORARY_FILE="$readiness"
    rm -f "$readiness"
    factory_resolve_model_profile "$profile_id" "$plan_probe" \
      "$FACTORY_DISABLED_ROUTE_IDS" "$readiness" >/dev/null 2>&1 || true
    rm -f "$plan_probe"
    [[ -s "$readiness" ]] || json_error "model readiness probes failed"
    if [[ "$command_name" == "fallback-plan" ]]; then
      preview_file="$(mktemp "$FACTORY_MODEL_STATE_ROOT/.model-fallback-preview.XXXXXX")" ||
        json_error "could not allocate fallback preview"
      if ! python3 -B "$KIT_DIR/scripts/model-fallback.py" preview \
        --workdir "$workdir" --factory-root "$FACTORY_ROOT" \
        --project "$FACTORY_PROJECT" --ticket "$ticket" \
        --failed-run "$failed_run" --reason "$reason" \
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
      apply_file="$(mktemp "$FACTORY_MODEL_STATE_ROOT/.model-fallback-apply.XXXXXX")" ||
        json_error "could not allocate fallback result"
      if ! python3 -B "$KIT_DIR/scripts/model-fallback.py" qualification-apply \
        --workdir "$workdir" --factory-root "$FACTORY_ROOT" \
        --project "$FACTORY_PROJECT" --ticket "$ticket" \
        --failed-run "$failed_run" --reason "$reason" \
        --readiness "$readiness" --remote "$CONTROL_REMOTE" > "$apply_file"; then
        rm -f "$apply_file"
        json_error "automatic qualification fallback failed"
      fi
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
    approval_file="$(mktemp "$FACTORY_MODEL_STATE_ROOT/.model-fallback-approval.XXXXXX")" ||
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
      recovery_file="$(mktemp "$FACTORY_MODEL_STATE_ROOT/.model-fallback-recovery.XXXXXX")" ||
        json_error "could not allocate fallback recovery result"
      if ! python3 -B "$KIT_DIR/scripts/model-fallback.py" recover \
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
    or not re.fullmatch(r"[A-Za-z0-9._:-]{1,200}", receipt.get("comment_id", ""))
):
    raise SystemExit(2)
print(receipt["approval_hash"] + "\t" + receipt["comment_id"])
PY
)" || {
        rm -f "$recovery_file" "$approval_file"
        json_error "exact unexpired or previously consumed fallback approval is required"
      }
      IFS=$'\t' read -r approval_hash approval_comment_id <<< "$recovery_values"
      python3 -B "$KIT_DIR/scripts/lib/model-fallback-approval.py" verify-consumed \
        --operator-map "$OPERATOR_MAP" \
        --ticket "$ticket" --failed-run "$failed_run" --reason "$reason" \
        --approval-hash "$approval_hash" --comment-id "$approval_comment_id" \
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
    apply_file="$(mktemp "$FACTORY_MODEL_STATE_ROOT/.model-fallback-apply.XXXXXX")" ||
      json_error "could not allocate fallback result"
    if ! python3 -B "$KIT_DIR/scripts/model-fallback.py" apply \
      --workdir "$workdir" --factory-root "$FACTORY_ROOT" \
      --project "$FACTORY_PROJECT" --ticket "$ticket" \
      --failed-run "$failed_run" --reason "$reason" \
      "${fallback_exception_args[@]}" \
      --readiness "$readiness" --remote "$CONTROL_REMOTE" \
      --approval "$approval_file" > "$apply_file"; then
      rm -f "$apply_file" "$approval_file"
      json_error "fallback apply failed"
    fi
    commit_sha="$(push_exact_head "$workdir" "$CONTROL_BRANCH" \
      "$CONTROL_REMOTE" "$expected_remote_head")"
    python3 -B "$KIT_DIR/scripts/lib/model-fallback-approval.py" consume \
    --operator-map "$OPERATOR_MAP" \
      --ticket "$ticket" --failed-run "$failed_run" --reason "$reason" \
      --approval-hash "$approval_hash" >/dev/null ||
      json_error "fallback committed but Linear approval consumption requires reconciliation"
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
    factory_resolve_model_profile "$FACTORY_MODEL_PROFILE_ID" "$resolution" \
      "$FACTORY_DISABLED_ROUTE_IDS" ||
      json_error "model pin resolution failed: ${FACTORY_RESOLVE_ERROR:-unknown}"
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
      factory_resolve_model_profile "$FACTORY_MODEL_PROFILE_ID" "$resolution" \
        "$FACTORY_DISABLED_ROUTE_IDS" ||
        json_error "model pin resolution failed: ${FACTORY_RESOLVE_ERROR:-unknown}"
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
