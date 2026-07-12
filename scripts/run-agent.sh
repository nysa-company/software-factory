#!/usr/bin/env bash
# run-agent.sh — the only sanctioned way to start a factory agent run.
# Enforces per-run, per-ticket, and daily budgets; serializes cap checks with a
# lock; anchors all state to the product repo root (not $PWD); logs every run
# to the cost ledger; enforces the cross-family role→adapter mapping.
#
# Usage:
#   run-agent.sh --role builder --ticket T-123 --prompt-file factory/roles/builder.md \
#                [--adapter claude-code] [--workdir /path/to/worktree] -- "task text"
#
# Envelope (factory/ENVELOPE.env at the repo root):
#   PER_RUN_BUDGET_USD, PER_TICKET_BUDGET_USD, PER_RUN_MAX_TURNS,
#   PER_RUN_TIMEOUT_MIN, DAILY_CAP_USD
set -euo pipefail

KIT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# --- anchor factory state to the repo root, never to $PWD ---
REPO_ROOT="${FACTORY_ROOT:-$(git rev-parse --show-toplevel 2>/dev/null || echo "$PWD")}"
FACTORY_DIR="$REPO_ROOT/factory"
LEDGER="${FACTORY_LEDGER:-$FACTORY_DIR/ledger.csv}"
ENV_FILE="${FACTORY_ENVELOPE:-$FACTORY_DIR/ENVELOPE.env}"
LOCK_DIR="$FACTORY_DIR/.ledger.lock"
RUNS_DIR="$FACTORY_DIR/runs"

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
[[ -n "$ROLE" && -n "$TICKET" && -n "$TASK" ]] || { echo "missing required args" >&2; exit 2; }
[[ -f "$ENV_FILE" ]] || { echo "envelope not found: $ENV_FILE — fill ENVELOPE.md and write ENVELOPE.env first" >&2; exit 3; }
# shellcheck disable=SC1090
source "$ENV_FILE"
PER_TICKET_BUDGET_USD="${PER_TICKET_BUDGET_USD:-$PER_RUN_BUDGET_USD}"

# --- cross-family role→adapter mapping (mechanical, not a prompt rule) ---
# Override only via FACTORY_ADAPTER_OVERRIDE (used for mock in kit tests).
case "$ROLE" in
  builder|planner) DEFAULT_ADAPTER="claude-code";;
  test-author|reviewer) DEFAULT_ADAPTER="codex";;
  narrator) DEFAULT_ADAPTER="claude-code";;
  *) echo "unknown role: $ROLE" >&2; exit 2;;
esac
if [[ -n "${FACTORY_ADAPTER_OVERRIDE:-}" ]]; then
  ADAPTER="$FACTORY_ADAPTER_OVERRIDE"
elif [[ -z "$ADAPTER" ]]; then
  ADAPTER="$DEFAULT_ADAPTER"
elif [[ "$ADAPTER" != "$DEFAULT_ADAPTER" ]]; then
  echo "role '$ROLE' must run on adapter '$DEFAULT_ADAPTER' (cross-family rule); got '$ADAPTER'" >&2
  exit 2
fi

# --- kill switch check (anchored) ---
if [[ -f "$FACTORY_DIR/KILL" ]]; then
  echo "KILL file present ($FACTORY_DIR/KILL) — factory is stopped. Remove it to resume." >&2
  exit 4
fi

mkdir -p "$FACTORY_DIR" "$RUNS_DIR"
[[ -f "$LEDGER" ]] || echo "date,time,ticket,role,adapter,prompt_version,turns,cost_usd,exit_status" > "$LEDGER"
TODAY="$(date +%F)"

# --- serialized cap check with budget reservation ---
# mkdir is atomic: it is the lock. Reservation counts this run's full per-run
# budget against the caps, so N concurrent runs cannot all squeeze past.
for i in $(seq 1 50); do mkdir "$LOCK_DIR" 2>/dev/null && break; sleep 0.2; [[ $i -eq 50 ]] && { echo "ledger lock stuck — see runbook" >&2; exit 8; }; done
trap 'rmdir "$LOCK_DIR" 2>/dev/null || true' EXIT

SPENT_TODAY="$(awk -F, -v d="$TODAY" 'NR>1 && $1==d {s+=$8} END {printf "%.4f", s+0}' "$LEDGER")"
SPENT_TICKET="$(awk -F, -v t="$TICKET" 'NR>1 && $3==t {s+=$8} END {printf "%.4f", s+0}' "$LEDGER")"
if awk -v s="$SPENT_TODAY" -v r="$PER_RUN_BUDGET_USD" -v cap="$DAILY_CAP_USD" 'BEGIN{exit !((s+r)>cap)}'; then
  echo "daily cap would be exceeded (spent \$$SPENT_TODAY + reserve \$$PER_RUN_BUDGET_USD > \$$DAILY_CAP_USD) — refusing. See runbooks/operator.md." >&2
  exit 5
fi
if awk -v s="$SPENT_TICKET" -v r="$PER_RUN_BUDGET_USD" -v cap="$PER_TICKET_BUDGET_USD" 'BEGIN{exit !((s+r)>cap)}'; then
  echo "ticket budget would be exceeded for $TICKET (spent \$$SPENT_TICKET + reserve \$$PER_RUN_BUDGET_USD > \$$PER_TICKET_BUDGET_USD) — move ticket to Blocked-Escalated." >&2
  exit 5
fi
# Reserve: write a provisional ledger row at full per-run budget; replaced with
# the real cost after the run. A crash leaves the conservative row in place.
RUN_ID="$(date +%s)-$$"
echo "$TODAY,$(date +%T),$TICKET,$ROLE,$ADAPTER,reserved,0,$PER_RUN_BUDGET_USD,reserved-$RUN_ID" >> "$LEDGER"
rmdir "$LOCK_DIR"; trap - EXIT

PROMPT_VERSION="unversioned"
[[ -n "$PROMPT_FILE" && -f "$PROMPT_FILE" ]] && PROMPT_VERSION="$(grep -m1 '^Version:' "$PROMPT_FILE" | awk '{print $2}' || echo unversioned)"

ADAPTER_SH="$KIT_DIR/scripts/adapters/$ADAPTER.sh"
[[ -x "$ADAPTER_SH" ]] || { echo "no adapter: $ADAPTER_SH" >&2; exit 6; }

# --- run; record PID so the kill switch can target factory runs precisely ---
set +e
"$ADAPTER_SH" \
  --budget "$PER_RUN_BUDGET_USD" \
  --max-turns "$PER_RUN_MAX_TURNS" \
  --timeout-min "$PER_RUN_TIMEOUT_MIN" \
  --prompt-file "${PROMPT_FILE:-/dev/null}" \
  --workdir "$WORKDIR" \
  -- "$TASK" > "$RUNS_DIR/$RUN_ID.out" 2>&1 &
ADAPTER_PID=$!
echo "$ADAPTER_PID" > "$RUNS_DIR/$RUN_ID.pid"
wait "$ADAPTER_PID"
STATUS=$?
set -e
rm -f "$RUNS_DIR/$RUN_ID.pid"
RESULT="$(cat "$RUNS_DIR/$RUN_ID.out")"

METRICS_LINE="$(printf '%s\n' "$RESULT" | tail -n1)"
TURNS="$(sed -n 's/.*turns=\([0-9][0-9]*\).*/\1/p' <<<"$METRICS_LINE")"
COST="$(sed -n 's/.*cost_usd=\([0-9.][0-9.]*\).*/\1/p' <<<"$METRICS_LINE")"
# Unparsable cost: keep the conservative full-budget reservation and say so
# loudly — never silently log $0 against the caps.
if [[ -z "$COST" ]]; then
  echo "WARNING: run cost unparsable — ledger keeps conservative reservation of \$$PER_RUN_BUDGET_USD for this run. Reconcile with the provider console." >&2
  COST="$PER_RUN_BUDGET_USD"
  TURNS="${TURNS:-0}"
fi

# Replace the reservation row with the real result (under lock).
for i in $(seq 1 50); do mkdir "$LOCK_DIR" 2>/dev/null && break; sleep 0.2; done
trap 'rmdir "$LOCK_DIR" 2>/dev/null || true' EXIT
TMP_LEDGER="$LEDGER.tmp.$$"
grep -v ",reserved-$RUN_ID\$" "$LEDGER" > "$TMP_LEDGER" || true
echo "$TODAY,$(date +%T),$TICKET,$ROLE,$ADAPTER,$PROMPT_VERSION,${TURNS:-0},$COST,$STATUS" >> "$TMP_LEDGER"
mv "$TMP_LEDGER" "$LEDGER"
rmdir "$LOCK_DIR"; trap - EXIT

printf '%s\n' "$RESULT"
exit "$STATUS"
