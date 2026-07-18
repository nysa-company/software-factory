#!/usr/bin/env bash
# Experimental, disabled pilot: Claude CLI routed to exact Kimi through
# OpenRouter's Anthropic-compatible endpoint. A same-UID process can still
# inspect another process's environment on some hosts; host isolation remains
# a residual risk even though the token is excluded from argv, files, and logs.
set -euo pipefail
umask 077

EXPECTED_MODEL="moonshotai/kimi-k2.6"
PINNED_VERSION="2.1.207"
OPENROUTER_ENDPOINT="https://openrouter.ai/api"
KIT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SECRET_VALIDATOR="$KIT_DIR/scripts/lib/claude-kimi-secret.py"
OUTPUT_VALIDATOR="$KIT_DIR/scripts/lib/claude-kimi-output.py"

BUDGET="" MAX_TURNS="" TIMEOUT_MIN="" PROMPT_FILE="" WORKDIR="$PWD" MODEL="" EFFORT=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --budget) BUDGET="$2"; shift 2 ;;
    --max-turns) MAX_TURNS="$2"; shift 2 ;;
    --timeout-min) TIMEOUT_MIN="$2"; shift 2 ;;
    --prompt-file) PROMPT_FILE="$2"; shift 2 ;;
    --workdir) WORKDIR="$2"; shift 2 ;;
    --model) MODEL="$2"; shift 2 ;;
    --effort) EFFORT="$2"; shift 2 ;;
    --) shift; break ;;
    *) echo "unknown adapter argument" >&2; exit 2 ;;
  esac
done
TASK="${*:-}"

[[ "$MODEL" == "$EXPECTED_MODEL" ]] ||
  { echo "Kimi adapter requires its exact pinned model" >&2; exit 2; }
[[ "$BUDGET" =~ ^[0-9]{1,7}([.][0-9]{1,6})?$ ]] ||
  { echo "Kimi adapter budget is invalid" >&2; exit 2; }
[[ "$MAX_TURNS" =~ ^[0-9]{1,4}$ ]] &&
  (( MAX_TURNS > 0 && MAX_TURNS <= 1000 )) ||
  { echo "Kimi adapter max turns is invalid" >&2; exit 2; }
[[ "$TIMEOUT_MIN" =~ ^[0-9]{1,4}$ ]] &&
  (( TIMEOUT_MIN > 0 && TIMEOUT_MIN <= 1440 )) ||
  { echo "Kimi adapter timeout is invalid" >&2; exit 2; }
[[ -d "$WORKDIR" && ! -L "$WORKDIR" && -r "$PROMPT_FILE" ]] ||
  { echo "Kimi adapter workdir or prompt file is invalid" >&2; exit 2; }

# Test-only path selection is intentionally not a production configuration key.
SECRET_FILE="$HOME/.factory/secrets/openrouter-kimi.key"
if [[ -n "${FACTORY_KIMI_SECRET_FILE:-}" ]]; then
  if [[ "${FACTORY_TEST_MODE:-0}" != "1" ||
        "${FACTORY_TRUSTED_TEST_HARNESS:-0}" != "1" ]]; then
    echo "Kimi credential override requires the trusted test harness" >&2
    exit 2
  fi
  SECRET_FILE="$FACTORY_KIMI_SECRET_FILE"
fi

PYTHON_BIN="$(type -P python3 || true)"
TIMEOUT_BIN="$(type -P timeout || true)"
CLAUDE_BIN="$(type -P claude || true)"
[[ -n "$PYTHON_BIN" && -n "$TIMEOUT_BIN" ]] ||
  { echo "Kimi adapter runtime dependency is missing" >&2; exit 6; }
[[ -n "$CLAUDE_BIN" ]] ||
  { echo "claude CLI not installed" >&2; exit 6; }

TOKEN="$("$PYTHON_BIN" "$SECRET_VALIDATOR" "$SECRET_FILE")" || exit $?
[[ -n "$TOKEN" ]] ||
  { echo "Kimi credential failed secure-file validation" >&2; exit 2; }

CONFIG_DIR="$HOME/.factory/claude-kimi-config"
FACTORY_KIMI_CONFIG_DIR="$CONFIG_DIR" "$PYTHON_BIN" - <<'PY'
import os
from pathlib import Path
import stat

path = Path(os.environ["FACTORY_KIMI_CONFIG_DIR"])
for directory in (path.parent, path):
    if directory.exists():
        metadata = os.lstat(directory)
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise SystemExit("Kimi config directory is unsafe")
    else:
        directory.mkdir(mode=0o700)
    metadata = os.lstat(directory)
    if metadata.st_uid != os.getuid():
        raise SystemExit("Kimi config directory is unsafe")
os.chmod(path, 0o700)
PY

MINIMAL_PATH="$(dirname "$CLAUDE_BIN"):/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin"
VERSION="$(env -i HOME="$HOME" PATH="$MINIMAL_PATH" CLAUDE_CONFIG_DIR="$CONFIG_DIR" \
  "$CLAUDE_BIN" --version 2>/dev/null | awk 'NR==1 {print; exit}' || true)"
[[ "${VERSION%% *}" == "$PINNED_VERSION" ]] ||
  { echo "installed Claude CLI does not match the Kimi pilot pin" >&2; exit 6; }
HELP="$(env -i HOME="$HOME" PATH="$MINIMAL_PATH" CLAUDE_CONFIG_DIR="$CONFIG_DIR" \
  "$CLAUDE_BIN" --help 2>/dev/null || true)"
for REQUIRED_FLAG in --max-turns --max-budget-usd --output-format --model \
  --append-system-prompt-file --dangerously-skip-permissions; do
  [[ "$HELP" == *"$REQUIRED_FLAG"* ]] ||
    { echo "Claude CLI is missing a required Kimi pilot flag" >&2; exit 6; }
done

STDOUT_FILE="$(mktemp "${TMPDIR:-/tmp}/factory-kimi-stdout.XXXXXX")"
STDERR_FILE="$(mktemp "${TMPDIR:-/tmp}/factory-kimi-stderr.XXXXXX")"
METRICS_FILE="$(mktemp "${TMPDIR:-/tmp}/factory-kimi-metrics.XXXXXX")"
cleanup() {
  rm -f "$STDOUT_FILE" "$STDERR_FILE" "$METRICS_FILE"
  TOKEN=""
}
trap cleanup EXIT

CLAUDE_ARGS=(
  -p "$TASK"
  --model "$EXPECTED_MODEL"
  --output-format json
  --max-turns "$MAX_TURNS"
  --max-budget-usd "$BUDGET"
  --dangerously-skip-permissions
)
if [[ -s "$PROMPT_FILE" ]]; then
  CLAUDE_ARGS+=(--append-system-prompt-file "$PROMPT_FILE")
fi

STATUS=0
(cd "$WORKDIR" && "$TIMEOUT_BIN" "$((TIMEOUT_MIN * 60))" \
  env -i \
    HOME="$HOME" \
    PATH="$MINIMAL_PATH" \
    CLAUDE_CONFIG_DIR="$CONFIG_DIR" \
    ANTHROPIC_BASE_URL="$OPENROUTER_ENDPOINT" \
    ANTHROPIC_AUTH_TOKEN="$TOKEN" \
    ANTHROPIC_API_KEY= \
    ANTHROPIC_MODEL="$EXPECTED_MODEL" \
    ANTHROPIC_DEFAULT_OPUS_MODEL="$EXPECTED_MODEL" \
    ANTHROPIC_DEFAULT_SONNET_MODEL="$EXPECTED_MODEL" \
    ANTHROPIC_DEFAULT_HAIKU_MODEL="$EXPECTED_MODEL" \
    ANTHROPIC_DEFAULT_FABLE_MODEL="$EXPECTED_MODEL" \
    CLAUDE_CODE_SUBAGENT_MODEL="$EXPECTED_MODEL" \
    "$CLAUDE_BIN" "${CLAUDE_ARGS[@]}" \
    >"$STDOUT_FILE" 2>"$STDERR_FILE") || STATUS=$?

VALIDATOR_STATUS=0
FACTORY_KIMI_REDACTION_TOKEN="$TOKEN" \
  "$PYTHON_BIN" "$OUTPUT_VALIDATOR" \
  "$STDOUT_FILE" "$STDERR_FILE" "$METRICS_FILE" "$MAX_TURNS" ||
  VALIDATOR_STATUS=$?
if [[ -s "$METRICS_FILE" ]]; then
  cat "$METRICS_FILE"
else
  echo "turns=0 input_tokens=0 output_tokens=0 cache_read_tokens=0 cache_write_tokens=0 token_basis=observational cost_basis=conservative_reservation"
fi
[[ "$STATUS" -eq 0 ]] || exit "$STATUS"
[[ "$VALIDATOR_STATUS" -eq 0 ]] || exit "$VALIDATOR_STATUS"
