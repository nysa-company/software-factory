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
source "$FACTORY_POLICY_DIR/provider-cli-version.sh"

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
  local test_version="" test_identity=""
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
  if [[ "${FACTORY_TEST_MODE:-0}" == "1" &&
        "${FACTORY_TRUSTED_TEST_HARNESS:-0}" == "1" ]]; then
    case "$adapter" in
      codex)
        test_version="${FACTORY_TEST_PROBE_CODEX_VERSION:-}"
        test_identity="${FACTORY_TEST_PROBE_CODEX_IDENTITY:-}"
        ;;
      claude-code)
        test_version="${FACTORY_TEST_PROBE_CLAUDE_VERSION:-}"
        test_identity="${FACTORY_TEST_PROBE_CLAUDE_IDENTITY:-}"
        ;;
      cursor-openai)
        test_version="${FACTORY_TEST_PROBE_CURSOR_OPENAI_VERSION:-}"
        test_identity="${FACTORY_TEST_PROBE_CURSOR_OPENAI_IDENTITY:-}"
        ;;
      cursor-anthropic)
        test_version="${FACTORY_TEST_PROBE_CURSOR_ANTHROPIC_VERSION:-}"
        test_identity="${FACTORY_TEST_PROBE_CURSOR_ANTHROPIC_IDENTITY:-}"
        ;;
    esac
    [[ -z "$test_version" ]] || PROBE_VERSION="$test_version"
    [[ -z "$test_identity" ]] || PROBE_REPORTED_IDENTITY="$test_identity"
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

factory_prepare_claude_config() {
  python3 - "$1/.credentials.json" "$2/.credentials.json" \
    "$HOME/.factory/claude-oauth-token" <<'PY'
import json
import os
import pathlib
import re
import stat
import sys
import time

source, destination, token_path = map(pathlib.Path, sys.argv[1:])

def refuse(reason):
    print(reason)
    raise SystemExit(1)

token_present = token_path.exists() or token_path.is_symlink()
if token_present:
    try:
        source_info = token_path.lstat()
        source_root_info = token_path.parent.lstat()
        if (
            not token_path.is_absolute()
            or not stat.S_ISDIR(source_root_info.st_mode)
            or source_root_info.st_uid != os.geteuid()
            or stat.S_IMODE(source_root_info.st_mode) != 0o700
            or token_path.is_symlink()
            or not stat.S_ISREG(source_info.st_mode)
            or source_info.st_uid != os.geteuid()
            or stat.S_IMODE(source_info.st_mode) != 0o600
            or source_info.st_nlink != 1
            or source_info.st_size > 4096
        ):
            raise ValueError
        source_fd = os.open(
            token_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        )
        with os.fdopen(source_fd, "r", encoding="utf-8") as stream:
            opened = os.fstat(stream.fileno())
            token = stream.read(4097).strip()
            after = os.fstat(stream.fileno())
        identity = lambda value: (
            value.st_dev,
            value.st_ino,
            value.st_mode,
            value.st_nlink,
            value.st_uid,
            value.st_size,
            value.st_mtime_ns,
        )
        if (
            identity(source_info) != identity(opened)
            or identity(opened) != identity(after)
            or not re.fullmatch(r"sk-ant-oat01-[A-Za-z0-9_-]{80,}", token)
        ):
            raise ValueError
        expires_at = (
            source_info.st_mtime_ns // 1_000_000
            + 365 * 24 * 60 * 60 * 1000
        )
        if expires_at <= int(time.time() * 1000) + 300_000:
            raise ValueError
        data = (
            json.dumps(
                {
                    "claudeAiOauth": {
                        "accessToken": token,
                        "expiresAt": expires_at,
                        "refreshToken": "",
                        "refreshTokenExpiresAt": expires_at,
                        "scopes": ["user:inference"],
                        "subscriptionType": "team",
                    }
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode()
    except (OSError, UnicodeError, ValueError):
        refuse("claude_subscription_token_unsafe")
else:
    try:
        source_info = source.lstat()
    except FileNotFoundError:
        refuse("claude_credential_missing")
    except OSError:
        refuse("claude_credential_unreadable")
    mode = stat.S_IMODE(source_info.st_mode)
    if source.is_symlink():
        refuse("claude_credential_symlink")
    if not stat.S_ISREG(source_info.st_mode):
        refuse("claude_credential_nonregular")
    if source_info.st_uid != os.geteuid():
        refuse("claude_credential_foreign_owner")
    if source_info.st_nlink != 1:
        refuse("claude_credential_hardlinked")
    if mode & 0o077:
        refuse(f"claude_credential_mode_{mode:04o}")
    if source_info.st_size > 1_000_000:
        refuse("claude_credential_oversized")
    try:
        source_fd = os.open(
            source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        )
    except OSError:
        refuse("claude_credential_changed")
    try:
        opened = os.fstat(source_fd)
        if (
            (opened.st_dev, opened.st_ino)
            != (source_info.st_dev, source_info.st_ino)
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
        ):
            refuse("claude_credential_changed")
        data = os.read(source_fd, 1_000_001)
    finally:
        os.close(source_fd)
    if len(data) > 1_000_000:
        refuse("claude_credential_oversized")

try:
    destination_root_info = destination.parent.lstat()
except OSError:
    refuse("claude_probe_config_unsafe")
if (
    not stat.S_ISDIR(destination_root_info.st_mode)
    or destination_root_info.st_uid != os.geteuid()
    or stat.S_IMODE(destination_root_info.st_mode) & 0o077
):
    refuse("claude_probe_config_unsafe")
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

factory_prepare_cursor_probe_home() {
  local source_home="$1" probe_home="$2"
  mkdir -m 700 "$probe_home/.cursor" || return 1
  python3 - "$source_home/.cursor" "$probe_home/.cursor" <<'PY'
import os
import pathlib
import stat
import sys

source_root, destination_root = map(pathlib.Path, sys.argv[1:])
sources = [source_root / name for name in ("auth.json", "cli-config.json")]

def refuse(reason):
    print(reason)
    raise SystemExit(1)

if not any(path.exists() or path.is_symlink() for path in sources):
    raise SystemExit(0)
if not all(path.exists() and not path.is_symlink() for path in sources):
    refuse("cursor_credential_pair_incomplete")

for source in sources:
    info = source.lstat()
    mode = stat.S_IMODE(info.st_mode)
    label = source.stem.replace("-", "_")
    if mode & 0o077:
        refuse(f"cursor_{label}_mode_{mode:04o}")
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_nlink != 1
        or info.st_size > 1_000_000
    ):
        refuse(f"cursor_{label}_unsafe")
    source_fd = os.open(
        source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        opened = os.fstat(source_fd)
        if (
            (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino)
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
        ):
            refuse(f"cursor_{label}_changed")
        data = os.read(source_fd, 1_000_001)
    finally:
        os.close(source_fd)
    if len(data) > 1_000_000:
        refuse(f"cursor_{label}_oversized")
    destination_fd = os.open(
        destination_root / source.name,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    with os.fdopen(destination_fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
PY
}

factory_probe_adapter() {
  local adapter="$1" explicit_model="${2:-}"
  local installed installed_version="" help model expected_family actual_family
  local claude_bin secret_file minimal_path required_flag
  local claude_config_dir claude_probe_config claude_oauth_state
  local cursor_bin="${CURSOR_AGENT_BIN:-agent}" auth_ready model_ready attempt
  local credential_reason
  local cursor_source_home="${FACTORY_CURSOR_SESSION_HOME:-$HOME}"
  local cursor_home=""
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
      if [[ -z "$installed" ]]; then
        PROBE_STATE="UNAVAILABLE"; PROBE_REASON="version_probe_failed"; return 0
      fi
      installed_version="$(factory_codex_version "$installed" 2>/dev/null || true)"
      PROBE_VERSION="$installed_version"
      if [[ -z "$installed_version" ||
            "$installed_version" != "${CODEX_PINNED:-0.144.1}" ]]; then
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
      claude_config_dir="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
      claude_probe_config="$(mktemp -d "${TMPDIR:-/tmp}/factory-claude-probe.XXXXXX")" || {
        PROBE_STATE="INVALID"; PROBE_REASON="probe_isolation_unavailable"; return 0
      }
      chmod 700 "$claude_probe_config"
      installed="$(CLAUDE_CONFIG_DIR="$claude_probe_config" \
        timeout "$probe_timeout" claude --version 2>/dev/null | \
        awk 'NR==1 {print; exit}' || true)"
      PROBE_VERSION="$installed"
      if [[ -z "$installed" ]]; then
        PROBE_STATE="UNAVAILABLE"; PROBE_REASON="version_probe_failed"
      elif [[ "${installed%% *}" != "${CLAUDE_CODE_PINNED:-2.1.223}" ]]; then
        PROBE_STATE="INVALID"; PROBE_REASON="version_mismatch"
      elif ! help="$(CLAUDE_CONFIG_DIR="$claude_probe_config" \
          timeout "$probe_timeout" claude --help 2>/dev/null)"; then
        PROBE_STATE="UNAVAILABLE"; PROBE_REASON="help_probe_failed"
      else
        for required_flag in --max-budget-usd --output-format \
          --append-system-prompt --model --effort; do
          if [[ "$help" != *"$required_flag"* ]]; then
            PROBE_STATE="INVALID"
            PROBE_REASON="contract_mismatch_missing_${required_flag#--}"
            break
          fi
        done
      fi
      if [[ "$PROBE_REASON" == "unclassified" ]]; then
        if ! credential_reason="$(factory_prepare_claude_config \
            "$claude_config_dir" "$claude_probe_config")"; then
          PROBE_STATE="INVALID"
          PROBE_REASON="${credential_reason:-credential_invalid}"
        elif ! CLAUDE_CONFIG_DIR="$claude_probe_config" \
            timeout "$probe_timeout" claude auth status >/dev/null 2>&1; then
          PROBE_STATE="UNAVAILABLE"; PROBE_REASON="authentication_unavailable"
        else
          claude_oauth_state="$(CLAUDE_CONFIG_DIR="$claude_probe_config" \
            factory_claude_oauth_readiness 2>/dev/null || true)"
          case "$claude_oauth_state" in
            READY:*) ;;
            UNAVAILABLE:*)
              PROBE_STATE="UNAVAILABLE"; PROBE_REASON="${claude_oauth_state#*:}" ;;
            *)
              PROBE_STATE="INVALID"; PROBE_REASON="credential_invalid" ;;
          esac
        fi
      fi
      if [[ "$PROBE_REASON" == "unclassified" ]]; then
        PROBE_STATE="READY"; PROBE_REASON="local_contract_ready"
      fi
      rm -rf "$claude_probe_config"
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
      cursor_home="$(mktemp -d "${TMPDIR:-/tmp}/factory-cursor-probe.XXXXXX")" || {
        PROBE_STATE="INVALID"; PROBE_REASON="probe_isolation_unavailable"; return 0
      }
      chmod 700 "$cursor_home"
      if ! credential_reason="$(factory_prepare_cursor_probe_home \
          "$cursor_source_home" "$cursor_home")"; then
        rm -rf "$cursor_home"
        PROBE_STATE="INVALID"
        PROBE_REASON="${credential_reason:-credential_invalid}"
        return 0
      fi
      installed="$(HOME="$cursor_home" timeout "$probe_timeout" "$cursor_bin" --version 2>/dev/null | awk 'NR==1 {print; exit}' || true)"
      PROBE_VERSION="$installed"
      if [[ -z "${CURSOR_AGENT_VERSION:-}" ]]; then
        PROBE_STATE="INVALID"; PROBE_REASON="version_unapproved"
      elif [[ "$(printf '%s\n' "$installed" | awk '{print $NF}')" != "$CURSOR_AGENT_VERSION" ]]; then
        if [[ -z "$installed" ]]; then
          PROBE_STATE="UNAVAILABLE"; PROBE_REASON="version_probe_failed"
        else
          PROBE_STATE="INVALID"; PROBE_REASON="version_mismatch"
        fi
      else
        installed_version="$CURSOR_AGENT_VERSION"
      fi
      if [[ -n "$installed_version" ]]; then
        help="$(HOME="$cursor_home" timeout "$probe_timeout" "$cursor_bin" --help 2>/dev/null || true)"
        if [[ "$help" != *"--print"* ||
              "$help" != *"--output-format"* ||
              "$help" != *"--workspace"* ||
              "$help" != *"--model"* ||
              "$help" != *"--force"* ||
              "$help" != *"--trust"* ]]; then
          PROBE_STATE="INVALID"; PROBE_REASON="contract_mismatch"
        fi
      fi
      if [[ -n "$installed_version" && "$PROBE_REASON" == "unclassified" ]]; then
        auth_ready=0
        for attempt in 1 2; do
          if HOME="$cursor_home" timeout "$probe_timeout" "$cursor_bin" status --format json 2>/dev/null |
               python3 "$FACTORY_POLICY_DIR/cursor-status.py" - >/dev/null 2>&1; then
            auth_ready=1
            break
          fi
        done
        if [[ "$auth_ready" != 1 ]]; then
          PROBE_STATE="UNAVAILABLE"; PROBE_REASON="authentication_unavailable"
        fi
      fi
      if [[ -n "$installed_version" && "$PROBE_REASON" == "unclassified" ]]; then
        model_ready=0
        for attempt in 1 2; do
          if HOME="$cursor_home" timeout "$probe_timeout" "$cursor_bin" models 2>/dev/null |
               awk -v model="$model" '{ for (i=1; i<=NF; i++) if ($i==model) found=1 } END { exit !found }'; then
            model_ready=1
            break
          fi
        done
        if [[ "$model_ready" != 1 ]]; then
          PROBE_STATE="INVALID"; PROBE_REASON="model_unavailable"
        fi
      fi
      if [[ -n "$installed_version" && "$PROBE_REASON" == "unclassified" ]]; then
        PROBE_REPORTED_IDENTITY="$(factory_model_report_name "$model" 2>/dev/null || true)"
        if [[ -z "$PROBE_REPORTED_IDENTITY" ]]; then
          PROBE_STATE="INVALID"; PROBE_REASON="model_not_allowlisted"
        else
          PROBE_STATE="READY"; PROBE_REASON="local_contract_ready"
        fi
      fi
      rm -rf "$cursor_home"
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

# Resolve a complete profile from non-task readiness probes. Native adapters
# share model-independent readiness; every remaining route is probed once.
factory_resolve_model_profile() {
  local profile_id="$1" output_plan="$2" disabled="${3:-}" readiness_output="${4:-}"
  local tmp probes rows readiness probe_routes plan_tmp readiness_tmp
  local route_id adapter selection expected disabled_route
  local probe_result new_probe pid probe_failed=0 probe_number=0 probe_count=0
  local codex_result="" claude_result=""
  local -a probe_pids=()
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
  readiness="$tmp/readiness.json"
  probe_routes="$tmp/probe-routes.tsv"
  : > "$probe_routes"

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
    probe_number=$((probe_number + 1))
    probe_result="$tmp/probe-$probe_number.tsv"
    new_probe=0
    disabled_route=0
    case ",$(printf '%s' "$disabled" | tr '[:space:]' ',')," in
      *",$route_id,"*) disabled_route=1 ;;
    esac
    if [[ "$disabled_route" == "1" ]]; then
      printf 'UNAVAILABLE\tcredits_exhausted\t\t\n' > "$probe_result"
    else
      case "$adapter" in
        codex)
          if [[ -n "$codex_result" ]]; then
            probe_result="$codex_result"
          else
            codex_result="$probe_result"
            new_probe=1
          fi
          ;;
        claude-code)
          if [[ -n "$claude_result" ]]; then
            probe_result="$claude_result"
          else
            claude_result="$probe_result"
            new_probe=1
          fi
          ;;
        *) new_probe=1 ;;
      esac
      if [[ "$new_probe" == "1" ]]; then
        (
          umask 077
          factory_probe_adapter "$adapter" "$selection" || exit 2
          printf '%s\t%s\t%s\t%s\n' \
            "$PROBE_STATE" "$PROBE_REASON" "$PROBE_VERSION" \
            "$PROBE_REPORTED_IDENTITY" > "$probe_result"
        ) &
        probe_pids[$probe_count]=$!
        probe_count=$((probe_count + 1))
        if [[ "$probe_count" -eq 5 ]]; then
          for pid in "${probe_pids[@]}"; do
            wait "$pid" || probe_failed=1
          done
          probe_pids=()
          probe_count=0
        fi
      fi
    fi
    printf '%s\t%s\t%s\n' \
      "$route_id" "$probe_result" "$expected" >> "$probe_routes"
  done < "$rows"
  if [[ "$probe_count" -gt 0 ]]; then
    for pid in "${probe_pids[@]}"; do
      wait "$pid" || probe_failed=1
    done
  fi
  if [[ "$probe_failed" != "0" ]]; then
    rm -rf "$tmp"
    FACTORY_RESOLVE_ERROR="readiness_probe_failed"
    return 2
  fi

  if ! python3 - "$probe_routes" "$readiness" <<'PY'
import json
import pathlib
import sys

routes = pathlib.Path(sys.argv[1])
output = pathlib.Path(sys.argv[2])
result = {}
with routes.open(encoding="utf-8") as source:
    for line in source:
        values = line.rstrip("\n").split("\t")
        if len(values) != 3:
            raise SystemExit(2)
        route_id, result_path, expected = values
        raw = pathlib.Path(result_path).read_text(encoding="utf-8")
        if not raw.endswith("\n") or raw.count("\n") != 1:
            raise SystemExit(2)
        values = raw[:-1].split("\t")
        if len(values) != 4:
            raise SystemExit(2)
        state, reason, version, reported = values
        if state == "READY" and reported != expected:
            state = "INVALID"
            reason = "reported_identity_mismatch"
        if route_id in result:
            raise SystemExit(2)
        result[route_id] = {
            "adapter_version": version,
            "reason": reason,
            "reported_identity": reported,
            "state": state,
        }
with output.open("w", encoding="utf-8") as handle:
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
    cp "$readiness" "$readiness_tmp" &&
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
      --readiness "$(cat "$readiness")")
    [[ "$profile_id" == "project-policy" ]] ||
      resolve_command+=(--profile "$profile_id")
  else
    resolve_command=(python3 -B "$FACTORY_MODEL_ROUTER" resolve "$profile_id"
      "$readiness" --catalog "$FACTORY_MODEL_CATALOG"
      --profiles "$FACTORY_MODEL_PROFILES")
  fi
  if ! "${resolve_command[@]}" > "$plan_tmp" 2>/dev/null; then
    rm -f "$plan_tmp"
    if python3 - "$readiness" <<'PY'
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
    FACTORY_MODEL_PROFILE_ID="cursor-opus-v2"
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
    for revision in reversed(plan["revisions"]):
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
