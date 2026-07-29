#!/usr/bin/env bash
# Kit-owned backend policy and non-task readiness probes.
#
# This file is sourced by run-agent.sh, preflight.sh, and contract-test.sh.
# Probes never receive a task. A task-bearing CLI is launched only after one
# adapter has been selected, so fallback is selection rather than retry.

FACTORY_POLICY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FACTORY_CURSOR_MODEL_ALLOWLIST="${FACTORY_CURSOR_MODEL_ALLOWLIST:-$FACTORY_POLICY_DIR/cursor-model-families.txt}"
FACTORY_MODEL_ROUTER="${FACTORY_MODEL_ROUTER:-$FACTORY_POLICY_DIR/../model-router.py}"
FACTORY_MODEL_MANAGER="${FACTORY_MODEL_MANAGER:-$FACTORY_POLICY_DIR/../model-manager.py}"
FACTORY_MODEL_CATALOG="${FACTORY_MODEL_CATALOG:-$FACTORY_POLICY_DIR/../model-routing/catalog-v1.json}"
FACTORY_MODEL_PROFILES="${FACTORY_MODEL_PROFILES:-$FACTORY_POLICY_DIR/../model-routing/profiles-v1.json}"
FACTORY_MODEL_POLICY_FILE="${FACTORY_MODEL_POLICY_FILE:-${FACTORY_ROOT:+$FACTORY_ROOT/factory/model-policy.json}}"

factory_role_group() {
  case "$1" in
    planner|builder|narrator) printf '%s\n' production ;;
    spec-linter|test-author|reviewer) printf '%s\n' checking ;;
    *) return 1 ;;
  esac
}

factory_group_family() {
  case "$1" in
    production) printf '%s\n' openai ;;
    checking) printf '%s\n' anthropic ;;
    *) return 1 ;;
  esac
}

factory_group_primary() {
  case "$1" in
    production) printf '%s\n' codex ;;
    checking) printf '%s\n' claude-code ;;
    *) return 1 ;;
  esac
}

factory_group_fallback() {
  case "$1" in
    production) printf '%s\n' cursor-openai ;;
    checking) printf '%s\n' cursor-anthropic ;;
    *) return 1 ;;
  esac
}

factory_role_model() {
  case "$1" in
    planner) printf '%s\n' gpt-5.6-sol ;;
    builder|narrator) printf '%s\n' gpt-5.6-terra ;;
    spec-linter) printf '%s\n' fable ;;
    test-author) printf '%s\n' fable ;;
    reviewer) printf '%s\n' sonnet ;;
    *) return 1 ;;
  esac
}

factory_role_effort() {
  case "$1" in
    planner) printf '%s\n' high ;;
    builder|narrator|spec-linter|test-author|reviewer) printf '%s\n' medium ;;
    *) return 1 ;;
  esac
}

factory_adapter_family() {
  case "$1" in
    codex|cursor-openai) printf '%s\n' openai ;;
    claude-code|cursor-anthropic) printf '%s\n' anthropic ;;
    claude-kimi) printf '%s\n' moonshot ;;
    mock) printf '%s\n' mock ;;
    *) return 1 ;;
  esac
}

factory_cursor_model() {
  case "$1" in
    cursor-openai) printf '%s\n' "${CURSOR_OPENAI_MODEL:-}" ;;
    cursor-anthropic) printf '%s\n' "${CURSOR_ANTHROPIC_MODEL:-}" ;;
    *) return 1 ;;
  esac
}

factory_model_family() {
  local model="$1"
  [[ -f "$FACTORY_CURSOR_MODEL_ALLOWLIST" ]] || return 1
  awk -F'|' -v model="$model" '
    /^[[:space:]]*#/ || /^[[:space:]]*$/ { next }
    $1 == model { print $2; found=1; exit }
    END { if (!found) exit 1 }
  ' "$FACTORY_CURSOR_MODEL_ALLOWLIST"
}

factory_model_report_name() {
  local model="$1"
  [[ -f "$FACTORY_CURSOR_MODEL_ALLOWLIST" ]] || return 1
  awk -F'|' -v model="$model" '
    /^[[:space:]]*#/ || /^[[:space:]]*$/ { next }
    $1 == model { report=$3; print report; found=1; exit }
    END { if (!found || report=="") exit 1 }
  ' "$FACTORY_CURSOR_MODEL_ALLOWLIST"
}

factory_probe_override() {
  local adapter="$1" explicit_model="${2:-}" value=""
  case "$adapter" in
    codex) value="${FACTORY_PROBE_CODEX:-}" ;;
    claude-code) value="${FACTORY_PROBE_CLAUDE_CODE:-}" ;;
    cursor-openai) value="${FACTORY_PROBE_CURSOR_OPENAI:-}" ;;
    cursor-anthropic) value="${FACTORY_PROBE_CURSOR_ANTHROPIC:-}" ;;
  esac
  [[ -n "$value" ]] || return 1
  PROBE_STATE="${value%%:*}"
  if [[ "$value" == *:* ]]; then
    PROBE_REASON="${value#*:}"
  else
    PROBE_REASON="test_override"
  fi
  PROBE_VERSION="test"
  PROBE_MODEL=""
  case "$adapter" in
    cursor-*) PROBE_MODEL="${explicit_model:-$(factory_cursor_model "$adapter")}" ;;
  esac
  PROBE_REPORTED_IDENTITY=""
  if [[ "$adapter" == cursor-* && -n "$PROBE_MODEL" ]]; then
    PROBE_REPORTED_IDENTITY="$(factory_model_report_name "$PROBE_MODEL" 2>/dev/null || true)"
  fi
  return 0
}

factory_claude_oauth_readiness() {
  local config_dir="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
  python3 - "$config_dir/.credentials.json" <<'PY'
import json
import os
import pathlib
import stat
import sys
import time

path = pathlib.Path(sys.argv[1])
try:
    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_nlink != 1
        or metadata.st_mode & 0o077
    ):
        raise ValueError
    oauth = json.loads(path.read_text(encoding="utf-8"))["claudeAiOauth"]
    expires_at = oauth["expiresAt"]
    if isinstance(expires_at, bool) or not isinstance(expires_at, int):
        raise ValueError
except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
    print("INVALID:credential_invalid")
    raise SystemExit

if expires_at <= int(time.time() * 1000) + 300_000:
    print("UNAVAILABLE:authentication_expired")
else:
    print("READY:credential_fresh")
PY
}

factory_probe_adapter() {
  local adapter="$1" explicit_model="${2:-}"
  local installed installed_version help model expected_family actual_family
  local claude_bin secret_file minimal_path required_flag
  local cursor_bin="${CURSOR_AGENT_BIN:-agent}" model_ready attempt
  local cursor_home="${FACTORY_CURSOR_SESSION_HOME:-$HOME}"
  local probe_timeout="${FACTORY_PROBE_TIMEOUT_SEC:-30}"
  PROBE_STATE="UNKNOWN"
  PROBE_REASON="unclassified"
  PROBE_VERSION=""
  PROBE_MODEL=""
  PROBE_REPORTED_IDENTITY=""

  if [[ -n "${FACTORY_PROBE_TRACE:-}" &&
        "${FACTORY_TEST_MODE:-0}" == "1" &&
        "${FACTORY_TRUSTED_TEST_HARNESS:-0}" == "1" ]]; then
    printf '%s|%s\n' "$adapter" "$explicit_model" >> "$FACTORY_PROBE_TRACE"
  fi
  if factory_probe_override "$adapter" "$explicit_model"; then
    return 0
  fi
  if [[ "$adapter" == "claude-kimi" ]] &&
     { [[ "${FACTORY_KIMI_PILOT_TEST:-0}" != "1" ]] ||
       [[ "${FACTORY_TEST_MODE:-0}" != "1" ]] ||
       [[ "${FACTORY_TRUSTED_TEST_HARNESS:-0}" != "1" ]]; }; then
    PROBE_STATE="UNAVAILABLE"
    PROBE_REASON="experimental_route_disabled"
    return 0
  fi
  if [[ "$adapter" != "mock" ]] && ! command -v timeout >/dev/null 2>&1; then
    PROBE_STATE="INVALID"; PROBE_REASON="probe_timeout_missing"; return 0
  fi

  case "$adapter" in
    codex)
      if ! command -v codex >/dev/null 2>&1; then
        PROBE_STATE="UNAVAILABLE"; PROBE_REASON="executable_missing"; return 0
      fi
      installed="$(timeout "$probe_timeout" codex --version 2>/dev/null | awk 'NR==1 {print; exit}' || true)"
      PROBE_VERSION="$installed"
      if [[ -z "$installed" ]]; then
        PROBE_STATE="UNAVAILABLE"; PROBE_REASON="version_probe_failed"; return 0
      fi
      if [[ "$installed" != *"${CODEX_PINNED:-0.144.1}"* ]]; then
        PROBE_STATE="INVALID"; PROBE_REASON="version_mismatch"; return 0
      fi
      help="$(timeout "$probe_timeout" codex exec --help 2>/dev/null || true)"
      if [[ "$help" != *"--json"* || "$help" != *"--model"* ]]; then
        PROBE_STATE="INVALID"; PROBE_REASON="contract_mismatch"; return 0
      fi
      if ! timeout "$probe_timeout" codex login status >/dev/null 2>&1; then
        PROBE_STATE="UNAVAILABLE"; PROBE_REASON="authentication_unavailable"; return 0
      fi
      PROBE_STATE="READY"; PROBE_REASON="local_contract_ready"
      ;;
    claude-code)
      if ! command -v claude >/dev/null 2>&1; then
        PROBE_STATE="UNAVAILABLE"; PROBE_REASON="executable_missing"; return 0
      fi
      installed="$(timeout "$probe_timeout" claude --version 2>/dev/null | awk 'NR==1 {print; exit}' || true)"
      PROBE_VERSION="$installed"
      if [[ -z "$installed" ]]; then
        PROBE_STATE="UNAVAILABLE"; PROBE_REASON="version_probe_failed"; return 0
      fi
      if [[ "$installed" != *"${CLAUDE_CODE_PINNED:-2.1.207}"* ]]; then
        PROBE_STATE="INVALID"; PROBE_REASON="version_mismatch"; return 0
      fi
      if ! help="$(timeout "$probe_timeout" claude --help 2>/dev/null)"; then
        PROBE_STATE="UNAVAILABLE"; PROBE_REASON="help_probe_failed"; return 0
      fi
      for required_flag in --max-budget-usd --output-format \
        --append-system-prompt --model --effort; do
        if [[ "$help" != *"$required_flag"* ]]; then
          PROBE_STATE="INVALID"
          PROBE_REASON="contract_mismatch_missing_${required_flag#--}"
          return 0
        fi
      done
      installed="$(factory_claude_oauth_readiness 2>/dev/null || true)"
      case "$installed" in
        READY:*) ;;
        UNAVAILABLE:*)
          PROBE_STATE="UNAVAILABLE"; PROBE_REASON="${installed#*:}"; return 0 ;;
        *)
          PROBE_STATE="INVALID"; PROBE_REASON="credential_invalid"; return 0 ;;
      esac
      if ! timeout "$probe_timeout" claude auth status >/dev/null 2>&1; then
        PROBE_STATE="UNAVAILABLE"; PROBE_REASON="authentication_unavailable"; return 0
      fi
      PROBE_STATE="READY"; PROBE_REASON="local_contract_ready"
      ;;
    claude-kimi)
      PROBE_MODEL="${explicit_model:-moonshotai/kimi-k2.6}"
      PROBE_REPORTED_IDENTITY="moonshotai/kimi-k2.6"
      if [[ "${FACTORY_KIMI_PILOT_TEST:-0}" != "1" ||
            "${FACTORY_TEST_MODE:-0}" != "1" ||
            "${FACTORY_TRUSTED_TEST_HARNESS:-0}" != "1" ]]; then
        PROBE_STATE="UNAVAILABLE"
        PROBE_REASON="experimental_route_disabled"
        PROBE_REPORTED_IDENTITY=""
        return 0
      fi
      if [[ "$PROBE_MODEL" != "moonshotai/kimi-k2.6" ]]; then
        PROBE_STATE="INVALID"; PROBE_REASON="model_not_explicit"; return 0
      fi
      claude_bin="$(type -P claude || true)"
      if [[ -z "$claude_bin" ]]; then
        PROBE_STATE="UNAVAILABLE"; PROBE_REASON="executable_missing"; return 0
      fi
      minimal_path="$(dirname "$claude_bin"):/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin"
      installed="$(timeout "$probe_timeout" env -i HOME="$HOME" PATH="$minimal_path" \
        "$claude_bin" --version 2>/dev/null | awk 'NR==1 {print; exit}' || true)"
      PROBE_VERSION="$installed"
      if [[ -z "$installed" ]]; then
        PROBE_STATE="UNAVAILABLE"; PROBE_REASON="version_probe_failed"; return 0
      fi
      if [[ "${installed%% *}" != "2.1.207" ]]; then
        PROBE_STATE="INVALID"; PROBE_REASON="version_mismatch"; return 0
      fi
      help="$(timeout "$probe_timeout" env -i HOME="$HOME" PATH="$minimal_path" \
        "$claude_bin" --help 2>/dev/null || true)"
      for required_flag in --max-turns --max-budget-usd --output-format --model \
        --append-system-prompt-file --dangerously-skip-permissions; do
        if [[ "$help" != *"$required_flag"* ]]; then
          PROBE_STATE="INVALID"; PROBE_REASON="contract_mismatch"; return 0
        fi
      done
      secret_file="$HOME/.factory/secrets/openrouter-kimi.key"
      if [[ -n "${FACTORY_KIMI_SECRET_FILE:-}" ]]; then
        secret_file="$FACTORY_KIMI_SECRET_FILE"
      fi
      if ! timeout "$probe_timeout" python3 \
          "$FACTORY_POLICY_DIR/claude-kimi-secret.py" --check "$secret_file" \
          >/dev/null 2>&1; then
        PROBE_STATE="UNAVAILABLE"; PROBE_REASON="authentication_unavailable"; return 0
      fi
      PROBE_STATE="READY"; PROBE_REASON="trusted_pilot_contract_ready"
      ;;
    cursor-openai|cursor-anthropic)
      if [[ "${FACTORY_CURSOR_FALLBACK_ENABLED:-0}" != "1" ]]; then
        PROBE_STATE="UNAVAILABLE"; PROBE_REASON="fallback_disabled"; return 0
      fi
      model="${explicit_model:-$(factory_cursor_model "$adapter")}"
      PROBE_MODEL="$model"
      if [[ -z "$model" || "$model" == "auto" ]]; then
        PROBE_STATE="INVALID"; PROBE_REASON="model_not_explicit"; return 0
      fi
      expected_family="$(factory_adapter_family "$adapter")"
      actual_family="$(factory_model_family "$model" 2>/dev/null || true)"
      if [[ "$actual_family" != "$expected_family" ]]; then
        PROBE_STATE="INVALID"; PROBE_REASON="model_not_allowlisted"; return 0
      fi
      if ! command -v "$cursor_bin" >/dev/null 2>&1; then
        PROBE_STATE="UNAVAILABLE"; PROBE_REASON="executable_missing"; return 0
      fi
      installed="$(HOME="$cursor_home" timeout "$probe_timeout" "$cursor_bin" --version 2>/dev/null | awk 'NR==1 {print; exit}' || true)"
      PROBE_VERSION="$installed"
      if [[ -z "${CURSOR_AGENT_VERSION:-}" ]]; then
        PROBE_STATE="INVALID"; PROBE_REASON="version_unapproved"; return 0
      fi
      installed_version="$(printf '%s\n' "$installed" | awk '{print $NF}')"
      if [[ "$installed_version" != "$CURSOR_AGENT_VERSION" ]]; then
        if [[ -z "$installed" ]]; then
          PROBE_STATE="UNAVAILABLE"; PROBE_REASON="version_probe_failed"
        else
          PROBE_STATE="INVALID"; PROBE_REASON="version_mismatch"
        fi
        return 0
      fi
      help="$(HOME="$cursor_home" timeout "$probe_timeout" "$cursor_bin" --help 2>/dev/null || true)"
      if [[ "$help" != *"--print"* ||
            "$help" != *"--output-format"* ||
            "$help" != *"--workspace"* ||
            "$help" != *"--model"* ||
            "$help" != *"--force"* ||
            "$help" != *"--trust"* ]]; then
        PROBE_STATE="INVALID"; PROBE_REASON="contract_mismatch"; return 0
      fi
      auth_ready=0
      for attempt in 1 2; do
        if HOME="$cursor_home" timeout "$probe_timeout" "$cursor_bin" status --format json 2>/dev/null |
             python3 "$FACTORY_POLICY_DIR/cursor-status.py" - >/dev/null 2>&1; then
          auth_ready=1
          break
        fi
      done
      if [[ "$auth_ready" != 1 ]]; then
        PROBE_STATE="UNAVAILABLE"; PROBE_REASON="authentication_unavailable"; return 0
      fi
      model_ready=0
      for attempt in 1 2; do
        if HOME="$cursor_home" timeout "$probe_timeout" "$cursor_bin" models 2>/dev/null |
             awk -v model="$model" '{ for (i=1; i<=NF; i++) if ($i==model) found=1 } END { exit !found }'; then
          model_ready=1
          break
        fi
      done
      if [[ "$model_ready" != 1 ]]; then
        PROBE_STATE="INVALID"; PROBE_REASON="model_unavailable"; return 0
      fi
      PROBE_REPORTED_IDENTITY="$(factory_model_report_name "$model" 2>/dev/null || true)"
      if [[ -z "$PROBE_REPORTED_IDENTITY" ]]; then
        PROBE_STATE="INVALID"; PROBE_REASON="model_not_allowlisted"; return 0
      fi
      PROBE_STATE="READY"; PROBE_REASON="local_contract_ready"
      ;;
    mock)
      PROBE_STATE="READY"; PROBE_REASON="test_override"; PROBE_VERSION="test"
      ;;
    *)
      PROBE_STATE="INVALID"; PROBE_REASON="unknown_adapter"
      ;;
  esac
}

factory_resolve_role() {
  local role="$1" group required primary fallback model effort
  group="$(factory_role_group "$role")" || {
    FACTORY_RESOLVE_ERROR="unknown_role"
    return 2
  }
  required="$(factory_group_family "$group")"
  primary="$(factory_group_primary "$group")"
  fallback="$(factory_group_fallback "$group")"
  model="$(factory_role_model "$role")" || {
    FACTORY_RESOLVE_ERROR="unknown_role_model"
    return 2
  }
  effort="$(factory_role_effort "$role")" || {
    FACTORY_RESOLVE_ERROR="unknown_role_effort"
    return 2
  }

  factory_probe_adapter "$primary"
  FACTORY_PRIMARY_STATE="$PROBE_STATE"
  FACTORY_PRIMARY_REASON="$PROBE_REASON"
  FACTORY_PRIMARY_VERSION="$PROBE_VERSION"

  case "$PROBE_STATE" in
    READY)
      FACTORY_SELECTED_ADAPTER="$primary"
      FACTORY_SELECTED_FAMILY="$required"
      FACTORY_SELECTED_MODEL="$model"
      FACTORY_SELECTED_EFFORT="$effort"
      FACTORY_SELECTED_VERSION="$PROBE_VERSION"
      FACTORY_SELECTION_REASON="primary_ready"
      return 0
      ;;
    INVALID|UNKNOWN)
      FACTORY_RESOLVE_ERROR="primary_${PROBE_REASON}"
      return 2
      ;;
    UNAVAILABLE) ;;
    *)
      FACTORY_RESOLVE_ERROR="primary_probe_invalid"
      return 2
      ;;
  esac

  factory_probe_adapter "$fallback"
  FACTORY_FALLBACK_STATE="$PROBE_STATE"
  FACTORY_FALLBACK_REASON="$PROBE_REASON"
  if [[ "$PROBE_STATE" != "READY" ]]; then
    FACTORY_RESOLVE_ERROR="no_ready_route_primary_${FACTORY_PRIMARY_REASON}_fallback_${PROBE_REASON}"
    return 2
  fi

  FACTORY_SELECTED_ADAPTER="$fallback"
  FACTORY_SELECTED_FAMILY="$required"
  FACTORY_SELECTED_MODEL="$PROBE_MODEL"
  FACTORY_SELECTED_EFFORT="$effort"
  FACTORY_SELECTED_VERSION="$PROBE_VERSION"
  FACTORY_SELECTION_REASON="primary_${FACTORY_PRIMARY_REASON}"
  return 0
}

# Resolve a complete profile from non-task readiness probes. Each catalog route
# is probed once, even when several roles share it.
factory_resolve_model_profile() {
  local profile_id="$1" output_plan="$2" disabled="${3:-}" readiness_output="${4:-}"
  local tmp probes rows readiness plan_tmp readiness_tmp route_id adapter selection expected
  local disabled_route state reason version reported
  FACTORY_RESOLVE_ERROR=""
  [[ -n "$profile_id" && -n "$output_plan" ]] || {
    FACTORY_RESOLVE_ERROR="invalid_resolution_arguments"
    return 2
  }
  command -v python3 >/dev/null 2>&1 || {
    FACTORY_RESOLVE_ERROR="python_missing"
    return 2
  }
  tmp="$(mktemp -d "${TMPDIR:-/tmp}/factory-model-resolution.XXXXXX")" || {
    FACTORY_RESOLVE_ERROR="temporary_directory_failed"
    return 2
  }
  probes="$tmp/probes.json"
  rows="$tmp/probes.tsv"
  readiness="$tmp/readiness.tsv"
  : > "$readiness"

  if [[ -n "${FACTORY_MODEL_STATE_ROOT:-}" && -n "${FACTORY_PROJECT:-}" &&
        -n "$FACTORY_MODEL_POLICY_FILE" ]]; then
    probe_command=(python3 -B "$FACTORY_MODEL_MANAGER" probe-list
      --state-root "$FACTORY_MODEL_STATE_ROOT" --project "$FACTORY_PROJECT"
      --catalog "$FACTORY_MODEL_CATALOG" --profiles-file "$FACTORY_MODEL_PROFILES"
      --policy-file "$FACTORY_MODEL_POLICY_FILE" --profile "$profile_id")
  else
    probe_command=(python3 -B "$FACTORY_MODEL_ROUTER" probe-list "$profile_id"
      --catalog "$FACTORY_MODEL_CATALOG" --profiles "$FACTORY_MODEL_PROFILES")
  fi
  if ! "${probe_command[@]}" > "$probes" 2>/dev/null; then
    rm -rf "$tmp"
    FACTORY_RESOLVE_ERROR="probe_list_invalid"
    return 2
  fi
  if ! python3 - "$probes" "$rows" "$disabled" <<'PY'
import json
import re
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    probes = json.load(handle)
disabled = set(filter(None, re.split(r"[\s,]+", sys.argv[3])))
safe = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}\Z")
if any(not safe.fullmatch(value) or value == "auto" for value in disabled):
    raise SystemExit(2)
with open(sys.argv[2], "w", encoding="utf-8") as handle:
    for probe in probes:
        values = (
            probe["route_id"], probe["adapter"], probe["selection_id"],
            probe["expected_reported_identity"],
        )
        if any("\t" in value or "\n" in value for value in values):
            raise SystemExit(2)
        handle.write("\t".join(values) + "\n")
PY
  then
    rm -rf "$tmp"
    FACTORY_RESOLVE_ERROR="disabled_route_ids_invalid"
    return 2
  fi

  while IFS=$'\t' read -r route_id adapter selection expected; do
    [[ -n "$route_id" ]] || continue
    disabled_route=0
    case ",$(printf '%s' "$disabled" | tr '[:space:]' ',')," in
      *",$route_id,"*) disabled_route=1 ;;
    esac
    if [[ "$disabled_route" == "1" ]]; then
      state="UNAVAILABLE"
      reason="credits_exhausted"
      version=""
      reported=""
    else
      factory_probe_adapter "$adapter" "$selection"
      state="$PROBE_STATE"
      reason="$PROBE_REASON"
      version="$PROBE_VERSION"
      reported="$PROBE_REPORTED_IDENTITY"
      if [[ "$state" == "READY" && "$reported" != "$expected" ]]; then
        state="INVALID"
        reason="reported_identity_mismatch"
      fi
    fi
    printf '%s\t%s\t%s\t%s\t%s\n' \
      "$route_id" "$state" "$reason" "$version" "$reported" >> "$readiness"
  done < "$rows"

  if ! python3 - "$readiness" "$tmp/readiness.json" <<'PY'
import json
import sys

result = {}
with open(sys.argv[1], encoding="utf-8") as handle:
    for line in handle:
        route_id, state, reason, version, reported = line.rstrip("\n").split("\t")
        if route_id in result:
            raise SystemExit(2)
        result[route_id] = {
            "adapter_version": version,
            "reason": reason,
            "reported_identity": reported,
            "state": state,
        }
with open(sys.argv[2], "w", encoding="utf-8") as handle:
    json.dump(result, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    handle.write("\n")
PY
  then
    rm -rf "$tmp"
    FACTORY_RESOLVE_ERROR="readiness_generation_failed"
    return 2
  fi
  if [[ -n "$readiness_output" ]]; then
    readiness_tmp="$(mktemp "${readiness_output}.tmp.XXXXXX")" || {
      rm -rf "$tmp"
      FACTORY_RESOLVE_ERROR="readiness_output_temporary_file_failed"
      return 2
    }
    cp "$tmp/readiness.json" "$readiness_tmp" &&
      chmod 0600 "$readiness_tmp" &&
      mv -f "$readiness_tmp" "$readiness_output" || {
        rm -f "$readiness_tmp"
        rm -rf "$tmp"
        FACTORY_RESOLVE_ERROR="readiness_output_install_failed"
        return 2
      }
  fi

  plan_tmp="$(mktemp "${output_plan}.tmp.XXXXXX")" || {
    rm -rf "$tmp"
    FACTORY_RESOLVE_ERROR="output_temporary_file_failed"
    return 2
  }
  if [[ -n "${FACTORY_MODEL_STATE_ROOT:-}" && -n "${FACTORY_PROJECT:-}" &&
        -n "$FACTORY_MODEL_POLICY_FILE" ]]; then
    resolve_command=(python3 -B "$FACTORY_MODEL_MANAGER" plan
      --state-root "$FACTORY_MODEL_STATE_ROOT" --project "$FACTORY_PROJECT"
      --catalog "$FACTORY_MODEL_CATALOG" --profiles-file "$FACTORY_MODEL_PROFILES"
      --policy-file "$FACTORY_MODEL_POLICY_FILE"
      --readiness "$(cat "$tmp/readiness.json")")
    [[ "$profile_id" == "project-policy" ]] ||
      resolve_command+=(--profile "$profile_id")
  else
    resolve_command=(python3 -B "$FACTORY_MODEL_ROUTER" resolve "$profile_id"
      "$tmp/readiness.json" --catalog "$FACTORY_MODEL_CATALOG"
      --profiles "$FACTORY_MODEL_PROFILES")
  fi
  if ! "${resolve_command[@]}" > "$plan_tmp" 2>/dev/null; then
    rm -f "$plan_tmp"
    if python3 - "$tmp/readiness.json" <<'PY'
import json
import sys

states = {value["state"] for value in json.load(open(sys.argv[1])).values()}
raise SystemExit(not ("UNAVAILABLE" in states and states <= {"READY", "UNAVAILABLE"}))
PY
    then
      FACTORY_RESOLVE_ERROR="profile_temporarily_unavailable"
    else
      FACTORY_RESOLVE_ERROR="profile_resolution_failed"
    fi
    rm -rf "$tmp"
    return 2
  fi
  chmod 0600 "$plan_tmp" 2>/dev/null || {
    rm -f "$plan_tmp"
    rm -rf "$tmp"
    FACTORY_RESOLVE_ERROR="output_mode_failed"
    return 2
  }
  if ! mv -f "$plan_tmp" "$output_plan"; then
    rm -f "$plan_tmp"
    rm -rf "$tmp"
    FACTORY_RESOLVE_ERROR="output_install_failed"
    return 2
  fi
  rm -rf "$tmp"
  return 0
}

# Load project routing state for probes, with no product or task content.
factory_load_model_probe_context() {
  local context override="${FACTORY_MODEL_PROFILE_OVERRIDE:-}"
  local -a policy_args=()
  FACTORY_RESOLVE_ERROR=""
  if [[ -n "$override" ]]; then
    if [[ "${FACTORY_TEST_MODE:-0}" != "1" ||
          "${FACTORY_TRUSTED_TEST_HARNESS:-0}" != "1" ]]; then
      FACTORY_RESOLVE_ERROR="profile_override_requires_trusted_test_harness"
      return 2
    fi
  fi
  if [[ -n "${FACTORY_MODEL_STATE_ROOT:-}" || -n "${FACTORY_PROJECT:-}" ]]; then
    if [[ -z "${FACTORY_MODEL_STATE_ROOT:-}" || -z "${FACTORY_PROJECT:-}" ]]; then
      FACTORY_RESOLVE_ERROR="model_state_context_incomplete"
      return 2
    fi
    [[ -z "$FACTORY_MODEL_POLICY_FILE" ]] ||
      policy_args=(--policy-file "$FACTORY_MODEL_POLICY_FILE")
    if ! context="$(python3 -B "$FACTORY_MODEL_MANAGER" probe-context \
        --state-root "$FACTORY_MODEL_STATE_ROOT" --project "$FACTORY_PROJECT" \
        --catalog "$FACTORY_MODEL_CATALOG" \
        --profiles-file "$FACTORY_MODEL_PROFILES" \
        "${policy_args[@]}" 2>/dev/null)"; then
      FACTORY_RESOLVE_ERROR="model_state_invalid"
      return 2
    fi
    if ! read -r FACTORY_MODEL_PROFILE_ID FACTORY_DISABLED_ROUTE_IDS < <(
      python3 -c 'import json,sys
d=json.load(sys.stdin)
assert d["schema"]=="model-manager-probe-context/v1"
print(d["profile_id"], ",".join(d["disabled_route_ids"]))' <<< "$context"
    ); then
      FACTORY_RESOLVE_ERROR="model_context_invalid"
      return 2
    fi
  else
    FACTORY_MODEL_PROFILE_ID="cursor-balanced-v2"
    FACTORY_DISABLED_ROUTE_IDS=""
  fi
  [[ -z "$override" ]] || FACTORY_MODEL_PROFILE_ID="$override"
  return 0
}

# Select one validated role tuple from a pure model-resolution plan.
factory_select_model_role() {
  local plan="$1" role="$2" selection values
  FACTORY_RESOLVE_ERROR=""
  if ! selection="$(python3 -B "$FACTORY_MODEL_ROUTER" select "$plan" "$role" \
      --catalog "$FACTORY_MODEL_CATALOG" --profiles "$FACTORY_MODEL_PROFILES" \
      2>/dev/null)"; then
    FACTORY_RESOLVE_ERROR="plan_selection_invalid"
    return 2
  fi
  if ! values="$(python3 - "$plan" "$selection" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    plan = json.load(handle)
value = json.loads(sys.argv[2])
fields = (
    value["adapter"], value["provider_family"], value["selection_id"],
    value["effort"], value["adapter_version"], value["route_id"],
    value["gateway_id"], value["inference_provider_id"],
    value["account_route_id"], value["transport"], plan["policy_hash"],
    value["reported_identity"],
)
if any("\t" in item or "\n" in item for item in fields):
    raise SystemExit(2)
print("\t".join(fields))
PY
  )"; then
    FACTORY_RESOLVE_ERROR="plan_tuple_invalid"
    return 2
  fi
  IFS=$'\t' read -r FACTORY_SELECTED_ADAPTER FACTORY_SELECTED_FAMILY \
    FACTORY_SELECTED_MODEL FACTORY_SELECTED_EFFORT FACTORY_SELECTED_VERSION \
    FACTORY_SELECTED_ROUTE_ID FACTORY_SELECTED_GATEWAY_ID \
    FACTORY_SELECTED_PROVIDER_ID FACTORY_SELECTED_ACCOUNT_ROUTE_ID \
    FACTORY_SELECTED_TRANSPORT FACTORY_SELECTED_POLICY_HASH \
    FACTORY_SELECTED_REPORTED_IDENTITY <<< "$values"
  FACTORY_SELECTION_REASON="resolved_profile"
  return 0
}

# Select one role from an immutable ticket route plan. The manager validates
# the wrapper, embedded pure resolution, ticket/kit affinity, and exact role
# tuple before any values are exposed to the launcher.
factory_select_pinned_model_role() {
  local ticket_plan="$1" ticket="$2" kit_sha="$3" role="$4"
  local state_root project selection values
  FACTORY_RESOLVE_ERROR=""
  state_root="${FACTORY_MODEL_STATE_ROOT:-${HOME:-/tmp}/.factory/model-state}"
  project="${FACTORY_PROJECT:-software-factory}"
  if [[ "$state_root" != /* ]]; then
    FACTORY_RESOLVE_ERROR="model_state_root_not_absolute"
    return 2
  fi
  if ! selection="$(python3 -B "$FACTORY_MODEL_MANAGER" select \
      --state-root "$state_root" --project "$project" \
      --catalog "$FACTORY_MODEL_CATALOG" \
      --profiles-file "$FACTORY_MODEL_PROFILES" \
      --ticket-plan "$ticket_plan" --ticket "$ticket" \
      --kit-sha "$kit_sha" --role "$role" 2>/dev/null)"; then
    FACTORY_RESOLVE_ERROR="pinned_selection_invalid"
    return 2
  fi
  if ! values="$(python3 - "$ticket_plan" "$selection" <<'PY'
import base64
import hashlib
import json
import sys

with open(sys.argv[1], "rb") as handle:
    raw = handle.read()
plan = json.loads(raw)
value = json.loads(sys.argv[2])
if plan.get("schema") == "ticket-model-route-plan/v1":
    resolution = plan["resolution"]
    revision = ""
    revision_hash = ""
    selection_reason = "pinned_route_plan"
elif plan.get("schema") == "ticket-model-route-journal/v2":
    revision_value = plan["revisions"][-1]
    body = revision_value["body"]
    if body["kind"] == "migration":
        legacy = json.loads(base64.b64decode(body["legacy_plan_b64"]))
        resolution = legacy["resolution"]
    else:
        resolution = body.get("new_resolution", body["prior_resolution"])
    revision = str(revision_value["revision"])
    revision_hash = revision_value["revision_hash"]
    selection_reason = "route_journal"
else:
    raise SystemExit(2)
fields = (
    value["adapter"], value["provider_family"], value["selection_id"],
    value["effort"], value["adapter_version"], value["route_id"],
    value["gateway_id"], value["inference_provider_id"],
    value["account_route_id"], value["transport"],
    resolution["policy_hash"], value["reported_identity"],
    hashlib.sha256(raw).hexdigest(), revision, revision_hash, selection_reason,
)
if any(not isinstance(item, str) or "\x1f" in item or "\n" in item for item in fields):
    raise SystemExit(2)
print("\x1f".join(fields))
PY
  )"; then
    FACTORY_RESOLVE_ERROR="pinned_tuple_invalid"
    return 2
  fi
  IFS=$'\x1f' read -r FACTORY_SELECTED_ADAPTER FACTORY_SELECTED_FAMILY \
    FACTORY_SELECTED_MODEL FACTORY_SELECTED_EFFORT FACTORY_SELECTED_VERSION \
    FACTORY_SELECTED_ROUTE_ID FACTORY_SELECTED_GATEWAY_ID \
    FACTORY_SELECTED_PROVIDER_ID FACTORY_SELECTED_ACCOUNT_ROUTE_ID \
    FACTORY_SELECTED_TRANSPORT FACTORY_SELECTED_POLICY_HASH \
    FACTORY_SELECTED_REPORTED_IDENTITY FACTORY_SELECTED_ROUTE_PLAN_SHA256 \
    FACTORY_SELECTED_ROUTE_REVISION FACTORY_SELECTED_ROUTE_REVISION_HASH \
    FACTORY_SELECTION_REASON \
    <<< "$values"
  return 0
}

# Re-probe only the already selected route. This is deliberately verification,
# never resolution: an outage or identity/version drift cannot select fallback.
factory_verify_selected_pinned_route_ready() {
  FACTORY_RESOLVE_ERROR=""
  factory_probe_adapter "$FACTORY_SELECTED_ADAPTER" "$FACTORY_SELECTED_MODEL"
  if [[ "$PROBE_STATE" != "READY" ]]; then
    FACTORY_RESOLVE_ERROR="pinned_route_${PROBE_STATE}_${PROBE_REASON}"
    return 2
  fi
  if [[ "$PROBE_VERSION" != "$FACTORY_SELECTED_VERSION" ]]; then
    FACTORY_RESOLVE_ERROR="pinned_route_adapter_version_drift"
    return 2
  fi
  if [[ "$PROBE_REPORTED_IDENTITY" != "$FACTORY_SELECTED_REPORTED_IDENTITY" ]]; then
    FACTORY_RESOLVE_ERROR="pinned_route_reported_identity_drift"
    return 2
  fi
  return 0
}
