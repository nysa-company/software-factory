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

cleanup() {
  local rc=$?
  [[ -z "$TEMPORARY_FILE" ]] || rm -f "$TEMPORARY_FILE"
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

manager() {
  python3 -B "$FACTORY_MODEL_MANAGER" "$1" \
    --state-root "$FACTORY_MODEL_STATE_ROOT" --project "$FACTORY_PROJECT" \
    --catalog "$FACTORY_MODEL_CATALOG" \
    --profiles-file "$FACTORY_MODEL_PROFILES" "${@:2}"
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

command_name="${1:-}"
[[ -n "$command_name" ]] || json_error "a model-control command is required"
shift

case "$command_name" in
  profiles|status)
    manager "$command_name" "$@" || json_error "$command_name failed"
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
  pin)
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
    load_machine_config
    factory_load_model_probe_context ||
      json_error "model state is invalid: ${FACTORY_RESOLVE_ERROR:-unknown}"
    resolution="$(mktemp "$FACTORY_MODEL_STATE_ROOT/.model-control-pin.XXXXXX")" ||
      json_error "could not allocate pin resolution"
    TEMPORARY_FILE="$resolution"
    factory_resolve_model_profile "$FACTORY_MODEL_PROFILE_ID" "$resolution" \
      "$FACTORY_DISABLED_ROUTE_IDS" ||
      json_error "model pin resolution failed: ${FACTORY_RESOLVE_ERROR:-unknown}"
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
