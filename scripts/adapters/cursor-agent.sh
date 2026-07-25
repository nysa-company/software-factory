#!/usr/bin/env bash
# Shared Cursor Agent CLI implementation. Invoke only through the family-typed
# cursor-openai.sh or cursor-anthropic.sh shims.
set -euo pipefail

KIT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck disable=SC1091
source "$KIT_DIR/scripts/lib/backend-policy.sh"

BUDGET="" MAX_TURNS="" TIMEOUT_MIN="" PROMPT_FILE="" WORKDIR="$PWD"
MODEL="" EFFORT=""
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
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
TASK="${*:-}"

case "${FACTORY_CURSOR_FAMILY:-}" in
  openai) ADAPTER="cursor-openai" ;;
  anthropic) ADAPTER="cursor-anthropic" ;;
  *) echo "Cursor adapter must be invoked through a family-typed shim" >&2; exit 2 ;;
esac

# Temporary compatibility for the legacy launcher. Resolved callers always
# supply both values; remove this bridge when run-agent consumes plans.
if [[ -z "$MODEL" && -z "$EFFORT" ]]; then
  MODEL="$(factory_cursor_model "$ADAPTER")"
  EFFORT="medium"
fi
if [[ -z "$MODEL" || -z "$EFFORT" ]]; then
  echo "Cursor adapter requires --model and --effort" >&2
  exit 2
fi
case "$EFFORT" in
  low|medium|high) ;;
  *) echo "Cursor effort is invalid" >&2; exit 2 ;;
esac
EXPECTED_FAMILY="$(factory_adapter_family "$ADAPTER")"
ACTUAL_FAMILY="$(factory_model_family "$MODEL" 2>/dev/null || true)"
EXPECTED_REPORTED_MODEL="$(factory_model_report_name "$MODEL" 2>/dev/null || true)"
if [[ -z "$MODEL" || "$MODEL" == "auto" || "$ACTUAL_FAMILY" != "$EXPECTED_FAMILY" ||
      -z "$EXPECTED_REPORTED_MODEL" ]]; then
  echo "Cursor model is not explicitly allowlisted for $EXPECTED_FAMILY" >&2
  exit 6
fi

CURSOR_BIN="${CURSOR_AGENT_BIN:-agent}"
CURSOR_HOME="${FACTORY_CURSOR_SESSION_HOME:-$HOME}"
if [[ "${FACTORY_CLI_INTERNAL_SANDBOX:-0}" == 1 ]]; then
  python3 - "$CURSOR_HOME" "${CURSOR_CONFIG_DIR:-}" "${CURSOR_DATA_DIR:-}" \
    "${TMPDIR:-}" "${FACTORY_CLI_ATTEMPT_ID:-}" <<'PY' || {
import os
import pathlib
import stat
import sys

home, config, data, tmp = map(pathlib.Path, sys.argv[1:5])
attempt = sys.argv[5]
runtime = data.parent
expected = {
    "home": runtime / "home",
    "config": runtime / "home" / ".cursor",
    "data": runtime / "data",
    "tmp": runtime / "tmp",
}
paths = {"home": home, "config": config, "data": data, "tmp": tmp}
if (
    not attempt
    or runtime.name != attempt
    or runtime.parent.name != "cli-attempts"
    or len(str(data / "projects")) > 84
):
    raise SystemExit(1)
for name, path in paths.items():
    if not path.is_absolute() or path != expected[name] or path.is_symlink():
        raise SystemExit(1)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
        raise SystemExit(1)
    if stat.S_IMODE(info.st_mode) != 0o700:
        raise SystemExit(1)
owner = runtime / "owner"
if (
    owner.is_symlink()
    or owner.read_text(encoding="utf-8") != attempt + "\n"
    or stat.S_IMODE(owner.stat().st_mode) != 0o600
):
    raise SystemExit(1)
for name in ("auth.json", "cli-config.json"):
    path = config / name
    info = path.stat()
    if path.is_symlink() or not stat.S_ISREG(info.st_mode):
        raise SystemExit(1)
    if info.st_uid != os.geteuid() or info.st_nlink != 1:
        raise SystemExit(1)
    if stat.S_IMODE(info.st_mode) != 0o600:
        raise SystemExit(1)
PY
    echo "Cursor CLI attempt runtime is unsafe" >&2
    exit 6
  }
fi
command -v "$CURSOR_BIN" >/dev/null 2>&1 || {
  echo "Cursor Agent CLI not installed" >&2
  exit 6
}
command -v timeout >/dev/null 2>&1 || {
  echo "GNU timeout not installed" >&2
  exit 6
}
[[ -n "${CURSOR_AGENT_VERSION:-}" ]] || {
  echo "CURSOR_AGENT_VERSION is not approved in the machine config" >&2
  exit 6
}
INSTALLED="$(HOME="$CURSOR_HOME" "$CURSOR_BIN" --version 2>/dev/null | awk 'NR==1 {print; exit}')"
INSTALLED_VERSION="$(printf '%s\n' "$INSTALLED" | awk '{print $NF}')"
[[ "$INSTALLED_VERSION" == "$CURSOR_AGENT_VERSION" ]] || {
  echo "Cursor Agent compatibility version mismatch" >&2
  exit 6
}
if ! HOME="$CURSOR_HOME" timeout "${FACTORY_PROBE_TIMEOUT_SEC:-10}" "$CURSOR_BIN" models 2>/dev/null |
     awk -v model="$MODEL" \
       '{ for (i=1; i<=NF; i++) if ($i==model) found=1 } END { exit !found }'; then
  echo "Resolved Cursor model is unavailable" >&2
  exit 6
fi

FULL_TASK="$TASK"
if [[ -s "$PROMPT_FILE" ]]; then
  FULL_TASK="$(cat "$PROMPT_FILE")

$TASK"
fi
if [[ "${FACTORY_ROLE:-}" == reviewer ]]; then
  FULL_TASK="$FULL_TASK

Reviewer CLI control: remain read-only in native Ask mode, inspect the supplied change, run the required deterministic checks with read-only terminal access, and return the required verdict without editing or committing."
else
  FULL_TASK="$FULL_TASK

Cursor CLI control: stay in the default execution mode. Do not switch to Plan or Ask mode, invoke createPlan, or merely describe intended work. Execute the supplied role contract now while preserving its mutation limits."
fi

NORMALIZED="$(mktemp "${TMPDIR:-/tmp}/factory-cursor-metrics.XXXXXX")"
RAW_STREAM="$(mktemp "${TMPDIR:-/tmp}/factory-cursor-stream.XXXXXX")"
rm -f "$RAW_STREAM"
mkfifo "$RAW_STREAM"
CURSOR_PRODUCER_PID=""
cleanup_cursor() {
  if [[ -n "$CURSOR_PRODUCER_PID" ]] &&
     kill -0 "$CURSOR_PRODUCER_PID" 2>/dev/null; then
    kill -TERM "$CURSOR_PRODUCER_PID" 2>/dev/null || true
    wait "$CURSOR_PRODUCER_PID" 2>/dev/null || true
  fi
  rm -f "$NORMALIZED" "$RAW_STREAM"
}
trap cleanup_cursor EXIT

set +e
(
  cd "$WORKDIR" || exit
  CURSOR_ARGS=(--print --output-format stream-json \
    --workspace "$WORKDIR" --trust --model "$MODEL")
  if [[ "${FACTORY_ROLE:-}" == reviewer ]]; then
    CURSOR_ARGS=(--mode ask --force "${CURSOR_ARGS[@]}")
  else
    CURSOR_ARGS=(--force "${CURSOR_ARGS[@]}")
  fi
  if [[ "${FACTORY_CURSOR_INTERNAL_SANDBOX:-0}" == 1 ]]; then
    CURSOR_ARGS=(--sandbox enabled "${CURSOR_ARGS[@]}")
  fi
  if [[ "${FACTORY_TIMEOUT_FOREGROUND:-0}" == 1 ]]; then
    exec env HOME="$CURSOR_HOME" timeout --foreground "$((TIMEOUT_MIN * 60))" \
      "$CURSOR_BIN" "${CURSOR_ARGS[@]}" "$FULL_TASK"
  else
    exec env HOME="$CURSOR_HOME" timeout "$((TIMEOUT_MIN * 60))" \
      "$CURSOR_BIN" "${CURSOR_ARGS[@]}" "$FULL_TASK"
  fi
) >"$RAW_STREAM" 2>&1 &
CURSOR_PRODUCER_PID="$!"
python3 "$KIT_DIR/scripts/lib/cursor-stream.py" \
  "$NORMALIZED" "$MODEL" "$EXPECTED_REPORTED_MODEL" "$WORKDIR" "$MAX_TURNS" \
  "${FACTORY_CURSOR_REPEATED_TOOL_ERROR_LIMIT:-0}" <"$RAW_STREAM"
STREAM_STATUS="$?"
if [[ "$STREAM_STATUS" == 15 ]] &&
   kill -0 "$CURSOR_PRODUCER_PID" 2>/dev/null; then
  kill -TERM "$CURSOR_PRODUCER_PID" 2>/dev/null || true
fi
wait "$CURSOR_PRODUCER_PID"
STATUS="$?"
CURSOR_PRODUCER_PID=""
set -e
if [[ "$STREAM_STATUS" == 15 ]]; then
  STATUS=15
elif [[ "$STREAM_STATUS" != "0" && "$STATUS" == "0" ]]; then
  echo "Cursor output validation/redaction failed" >&2
  STATUS=9
fi

[[ -f "$NORMALIZED" ]] || : > "$NORMALIZED"
TURNS="$(sed -n 's/^turns=//p' "$NORMALIZED" | awk 'NR==1 {print; exit}')"
IN_TOKENS="$(sed -n 's/^input_tokens=//p' "$NORMALIZED" | awk 'NR==1 {print; exit}')"
OUT_TOKENS="$(sed -n 's/^output_tokens=//p' "$NORMALIZED" | awk 'NR==1 {print; exit}')"
CACHE_TOKENS="$(sed -n 's/^cache_tokens=//p' "$NORMALIZED" | awk 'NR==1 {print; exit}')"
TURNS="${TURNS:-0}"
IN_TOKENS="${IN_TOKENS:-0}"
OUT_TOKENS="${OUT_TOKENS:-0}"
CACHE_TOKENS="${CACHE_TOKENS:-0}"

if [[ "$TURNS" =~ ^[0-9]+$ && "$MAX_TURNS" =~ ^[0-9]+$ &&
      "$TURNS" -gt "$MAX_TURNS" ]]; then
  echo "TURN LIMIT EXCEEDED: observed $TURNS > configured $MAX_TURNS" >&2
  STATUS=7
fi

COST=""
RATE_IN=""
RATE_OUT=""
RATE_CACHE=""
case "$EXPECTED_FAMILY" in
  openai)
    RATE_IN="${CURSOR_OPENAI_USD_PER_MTOK_IN:-}"
    RATE_OUT="${CURSOR_OPENAI_USD_PER_MTOK_OUT:-}"
    RATE_CACHE="${CURSOR_OPENAI_USD_PER_MTOK_CACHE:-0}"
    ;;
  anthropic)
    RATE_IN="${CURSOR_ANTHROPIC_USD_PER_MTOK_IN:-}"
    RATE_OUT="${CURSOR_ANTHROPIC_USD_PER_MTOK_OUT:-}"
    RATE_CACHE="${CURSOR_ANTHROPIC_USD_PER_MTOK_CACHE:-0}"
    ;;
esac
if [[ -n "${CURSOR_PRICING_SNAPSHOT_DATE:-}" &&
      -n "$RATE_IN" && -n "$RATE_OUT" &&
      $((IN_TOKENS + OUT_TOKENS + CACHE_TOKENS)) -gt 0 ]]; then
  COST="$(awk -v i="$IN_TOKENS" -v o="$OUT_TOKENS" -v c="$CACHE_TOKENS" \
    -v in_rate="$RATE_IN" -v out_rate="$RATE_OUT" -v cache_rate="$RATE_CACHE" \
    'BEGIN { printf "%.4f", (i*in_rate + o*out_rate + c*cache_rate)/1000000 }')"
fi

if [[ -n "$COST" ]] &&
   awk -v c="$COST" -v b="${BUDGET:-999999}" 'BEGIN { exit !(c>b) }'; then
  echo "BUDGET EXCEEDED: estimated Cursor usage \$$COST > per-run budget \$$BUDGET" >&2
  STATUS=7
fi

if [[ -n "$COST" ]]; then
  # CLI token events are optional and not a stable billing schema. Keep this
  # estimate for observability, but do not emit cost_usd: the wrapper retains
  # the conservative full-budget reservation.
  echo "turns=$TURNS estimated_cost_usd=$COST cost_basis=conservative_reservation pricing_snapshot_date=$CURSOR_PRICING_SNAPSHOT_DATE input_rate=$RATE_IN output_rate=$RATE_OUT cache_rate=$RATE_CACHE input_tokens=$IN_TOKENS output_tokens=$OUT_TOKENS cache_tokens=$CACHE_TOKENS"
else
  echo "turns=$TURNS cost_basis=conservative_reservation input_tokens=$IN_TOKENS output_tokens=$OUT_TOKENS cache_tokens=$CACHE_TOKENS"
fi
exit "$STATUS"
