#!/usr/bin/env bash
# Shared Cursor Agent CLI implementation. Invoke only through the family-typed
# cursor-openai.sh or cursor-anthropic.sh shims.
set -euo pipefail

KIT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck disable=SC1091
source "$KIT_DIR/scripts/lib/backend-policy.sh"

BUDGET="" MAX_TURNS="" TIMEOUT_MIN="" PROMPT_FILE="" WORKDIR="$PWD"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --budget) BUDGET="$2"; shift 2 ;;
    --max-turns) MAX_TURNS="$2"; shift 2 ;;
    --timeout-min) TIMEOUT_MIN="$2"; shift 2 ;;
    --prompt-file) PROMPT_FILE="$2"; shift 2 ;;
    --workdir) WORKDIR="$2"; shift 2 ;;
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

MODEL="$(factory_cursor_model "$ADAPTER")"
EXPECTED_FAMILY="$(factory_adapter_family "$ADAPTER")"
ACTUAL_FAMILY="$(factory_model_family "$MODEL" 2>/dev/null || true)"
EXPECTED_REPORTED_MODEL="$(factory_model_report_name "$MODEL" 2>/dev/null || true)"
if [[ -z "$MODEL" || "$MODEL" == "auto" || "$ACTUAL_FAMILY" != "$EXPECTED_FAMILY" ||
      -z "$EXPECTED_REPORTED_MODEL" ]]; then
  echo "Cursor model is not explicitly allowlisted for $EXPECTED_FAMILY" >&2
  exit 6
fi

CURSOR_BIN="${CURSOR_AGENT_BIN:-agent}"
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
INSTALLED="$("$CURSOR_BIN" --version 2>/dev/null | awk 'NR==1 {print; exit}')"
INSTALLED_VERSION="$(printf '%s\n' "$INSTALLED" | awk '{print $NF}')"
[[ "$INSTALLED_VERSION" == "$CURSOR_AGENT_VERSION" ]] || {
  echo "Cursor Agent compatibility version mismatch" >&2
  exit 6
}

FULL_TASK="$TASK"
if [[ -s "$PROMPT_FILE" ]]; then
  FULL_TASK="$(cat "$PROMPT_FILE")

$TASK"
fi

NORMALIZED="$(mktemp "${TMPDIR:-/tmp}/factory-cursor-metrics.XXXXXX")"
cleanup_cursor() {
  rm -f "$NORMALIZED"
}
trap cleanup_cursor EXIT

set +e
(
  cd "$WORKDIR" &&
    timeout "$((TIMEOUT_MIN * 60))" \
      "$CURSOR_BIN" --print --output-format stream-json \
      --workspace "$WORKDIR" --trust --force --model "$MODEL" "$FULL_TASK"
) 2>&1 | python3 "$KIT_DIR/scripts/lib/cursor-stream.py" \
  "$NORMALIZED" "$MODEL" "$EXPECTED_REPORTED_MODEL" "$WORKDIR" "$MAX_TURNS"
PIPE_RESULT="${PIPESTATUS[*]}"
STATUS="${PIPE_RESULT%% *}"
STREAM_STATUS="${PIPE_RESULT##* }"
set -e
if [[ "$STREAM_STATUS" != "0" && "$STATUS" == "0" ]]; then
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
