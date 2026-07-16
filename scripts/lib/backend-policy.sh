#!/usr/bin/env bash
# Kit-owned backend policy and non-task readiness probes.
#
# This file is sourced by run-agent.sh, preflight.sh, and contract-test.sh.
# Probes never receive a task. A task-bearing CLI is launched only after one
# adapter has been selected, so fallback is selection rather than retry.

FACTORY_POLICY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FACTORY_CURSOR_MODEL_ALLOWLIST="${FACTORY_CURSOR_MODEL_ALLOWLIST:-$FACTORY_POLICY_DIR/cursor-model-families.txt}"

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
    spec-linter) printf '%s\n' haiku ;;
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
  local adapter="$1" value=""
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
    cursor-*) PROBE_MODEL="$(factory_cursor_model "$adapter")" ;;
  esac
  return 0
}

factory_probe_adapter() {
  local adapter="$1" installed installed_version help model expected_family actual_family
  local cursor_bin="${CURSOR_AGENT_BIN:-agent}"
  local probe_timeout="${FACTORY_PROBE_TIMEOUT_SEC:-10}"
  PROBE_STATE="UNKNOWN"
  PROBE_REASON="unclassified"
  PROBE_VERSION=""
  PROBE_MODEL=""

  if factory_probe_override "$adapter"; then
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
      help="$(timeout "$probe_timeout" claude --help 2>/dev/null || true)"
      if [[ "$help" != *"--max-budget-usd"* ||
            "$help" != *"--output-format"* ||
            "$help" != *"--append-system-prompt"* ||
            "$help" != *"--model"* || "$help" != *"--effort"* ]]; then
        PROBE_STATE="INVALID"; PROBE_REASON="contract_mismatch"; return 0
      fi
      if ! timeout "$probe_timeout" claude auth status >/dev/null 2>&1; then
        PROBE_STATE="UNAVAILABLE"; PROBE_REASON="authentication_unavailable"; return 0
      fi
      PROBE_STATE="READY"; PROBE_REASON="local_contract_ready"
      ;;
    cursor-openai|cursor-anthropic)
      if [[ "${FACTORY_CURSOR_FALLBACK_ENABLED:-0}" != "1" ]]; then
        PROBE_STATE="UNAVAILABLE"; PROBE_REASON="fallback_disabled"; return 0
      fi
      model="$(factory_cursor_model "$adapter")"
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
      installed="$(timeout "$probe_timeout" "$cursor_bin" --version 2>/dev/null | awk 'NR==1 {print; exit}' || true)"
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
      help="$(timeout "$probe_timeout" "$cursor_bin" --help 2>/dev/null || true)"
      if [[ "$help" != *"--print"* ||
            "$help" != *"--output-format"* ||
            "$help" != *"--workspace"* ||
            "$help" != *"--model"* ||
            "$help" != *"--force"* ||
            "$help" != *"--trust"* ]]; then
        PROBE_STATE="INVALID"; PROBE_REASON="contract_mismatch"; return 0
      fi
      if ! timeout "$probe_timeout" "$cursor_bin" status --format json 2>/dev/null |
           python3 "$FACTORY_POLICY_DIR/cursor-status.py" - >/dev/null 2>&1; then
        PROBE_STATE="UNAVAILABLE"; PROBE_REASON="authentication_unavailable"; return 0
      fi
      if ! timeout "$probe_timeout" "$cursor_bin" models 2>/dev/null |
           awk -v model="$model" '{ for (i=1; i<=NF; i++) if ($i==model) found=1 } END { exit !found }'; then
        PROBE_STATE="INVALID"; PROBE_REASON="model_unavailable"; return 0
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
