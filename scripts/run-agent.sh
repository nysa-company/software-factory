#!/usr/bin/env bash
# run-agent.sh — the only sanctioned way to start a factory agent run.
# Enforces per-run and daily budgets, logs every run to the cost ledger,
# and records role prompt versions for attribution.
#
# Usage:
#   run-agent.sh --role builder --ticket T-123 --prompt-file factory/roles/builder.md \
#                --adapter claude-code --workdir /path/to/worktree -- "task text or @file"
#
# Reads envelope limits from factory/ENVELOPE.env (generated from ENVELOPE.md):
#   PER_RUN_BUDGET_USD, PER_RUN_MAX_TURNS, PER_RUN_TIMEOUT_MIN, DAILY_CAP_USD
set -euo pipefail

KIT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LEDGER="${FACTORY_LEDGER:-$PWD/factory/ledger.csv}"
ENV_FILE="${FACTORY_ENVELOPE:-$PWD/factory/ENVELOPE.env}"

ROLE="" TICKET="" PROMPT_FILE="" ADAPTER="" WORKDIR="$PWD"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --role) ROLE="$2"; shift 2;;
    --ticket) TICKET="$2"; shift 2;;
    --prompt-file) PROMPT_FILE="$2"; shift 2;;
    --adapter) ADAPTER="$2"; shift 2;;
    --workdir) WORKDIR="$2"; shift 2;;
    --) shift; break;;
    *) echo "unknown arg: $1" >&2; exit 2;;
  esac
done
TASK="${*:-}"
[[ -n "$ROLE" && -n "$TICKET" && -n "$ADAPTER" && -n "$TASK" ]] || { echo "missing required args" >&2; exit 2; }
[[ -f "$ENV_FILE" ]] || { echo "envelope not found: $ENV_FILE — fill ENVELOPE.md and generate ENVELOPE.env first" >&2; exit 3; }
# shellcheck disable=SC1090
source "$ENV_FILE"

# --- kill switch check ---
if [[ -f "$PWD/factory/KILL" ]]; then
  echo "KILL file present — factory is stopped. Remove factory/KILL to resume." >&2
  exit 4
fi

# --- daily cap check (sums today's ledger entries) ---
mkdir -p "$(dirname "$LEDGER")"
[[ -f "$LEDGER" ]] || echo "date,time,ticket,role,adapter,prompt_version,turns,cost_usd,exit_status" > "$LEDGER"
TODAY="$(date +%F)"
SPENT_TODAY="$(awk -F, -v d="$TODAY" '$1==d {s+=$8} END {printf "%.2f", s+0}' "$LEDGER")"
if awk -v s="$SPENT_TODAY" -v cap="$DAILY_CAP_USD" 'BEGIN{exit !(s>=cap)}'; then
  echo "daily cap reached (\$$SPENT_TODAY / \$$DAILY_CAP_USD) — refusing to start. See runbooks/operator.md." >&2
  exit 5
fi

PROMPT_VERSION="unversioned"
[[ -n "$PROMPT_FILE" && -f "$PROMPT_FILE" ]] && PROMPT_VERSION="$(grep -m1 '^Version:' "$PROMPT_FILE" | awk '{print $2}' || echo unversioned)"

# --- run via adapter (adapter prints "turns=N cost_usd=X" as its last line) ---
ADAPTER_SH="$KIT_DIR/scripts/adapters/$ADAPTER.sh"
[[ -x "$ADAPTER_SH" ]] || { echo "no adapter: $ADAPTER_SH" >&2; exit 6; }

set +e
RESULT="$("$ADAPTER_SH" \
  --budget "$PER_RUN_BUDGET_USD" \
  --max-turns "$PER_RUN_MAX_TURNS" \
  --timeout-min "$PER_RUN_TIMEOUT_MIN" \
  --prompt-file "${PROMPT_FILE:-/dev/null}" \
  --workdir "$WORKDIR" \
  -- "$TASK")"
STATUS=$?
set -e

METRICS_LINE="$(printf '%s\n' "$RESULT" | tail -n1)"
TURNS="$(sed -n 's/.*turns=\([0-9][0-9]*\).*/\1/p' <<<"$METRICS_LINE")"
COST="$(sed -n 's/.*cost_usd=\([0-9.][0-9.]*\).*/\1/p' <<<"$METRICS_LINE")"
echo "$TODAY,$(date +%T),$TICKET,$ROLE,$ADAPTER,$PROMPT_VERSION,${TURNS:-0},${COST:-0},$STATUS" >> "$LEDGER"

printf '%s\n' "$RESULT"
exit "$STATUS"
