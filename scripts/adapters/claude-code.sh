#!/usr/bin/env bash
# Adapter: Claude Code CLI (checking family — spec-linter, test-author, reviewer;
# flipped from production roles 2026-07-13, operator decision).
# Pinned CLI version below; the contract test fails loudly when the installed
# CLI drifts, instead of silently losing budget enforcement.
#
# Contract with run-agent.sh: accept the flags below, run the task,
# print agent output, and print a final line: "turns=N cost_usd=X".
set -euo pipefail

PINNED_VERSION="${CLAUDE_CODE_PINNED:-2.1.207}"  # pinned at shakedown 2026-07-11

BUDGET="" MAX_TURNS="" TIMEOUT_MIN="" PROMPT_FILE="" WORKDIR="$PWD" MODEL="" EFFORT=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --budget) BUDGET="$2"; shift 2;;
    --max-turns) MAX_TURNS="$2"; shift 2;;
    --timeout-min) TIMEOUT_MIN="$2"; shift 2;;
    --prompt-file) PROMPT_FILE="$2"; shift 2;;
    --workdir) WORKDIR="$2"; shift 2;;
    --model) MODEL="$2"; shift 2;;
    --effort) EFFORT="$2"; shift 2;;
    --) shift; break;;
    *) echo "unknown arg: $1" >&2; exit 2;;
  esac
done
TASK="${*:-}"

command -v claude >/dev/null || { echo "claude CLI not installed" >&2; exit 6; }
INSTALLED="$(claude --version 2>/dev/null | head -n1 || true)"
case "$INSTALLED" in
  *"$PINNED_VERSION"*) : ;;
  *) echo "WARNING: installed Claude Code ($INSTALLED) != pinned ($PINNED_VERSION). Run adapters/contract-test.sh before continuing." >&2 ;;
esac

# Shakedown finding (2026-07-11, Claude Code 2.1.207): --max-turns is gone from
# the CLI; --max-budget-usd exists and is a HARD in-run dollar stop — strictly
# better enforcement than the old post-hoc check. Turns remain logged from the
# JSON result; timeout guards the wall clock.
# Note: no bash arrays for the optional prompt (macOS ships bash 3.2, where
# empty-array expansion under `set -u` aborts).
# First-real-run finding (2026-07-12): headless -p mode cannot edit files or
# run commands without --dangerously-skip-permissions. Factory runs are
# autonomous by design; containment comes from the envelope (budget, timeout),
# the worktree, and CI gates — not from interactive permission prompts.
if [[ -s "$PROMPT_FILE" ]]; then
  OUT="$(cd "$WORKDIR" && timeout "$((TIMEOUT_MIN * 60))" \
    claude -p "$TASK" --model "$MODEL" --effort "$EFFORT" --output-format json --max-budget-usd "$BUDGET" \
    --dangerously-skip-permissions \
    --append-system-prompt "$(cat "$PROMPT_FILE")" 2>&1)" || STATUS=$?
else
  OUT="$(cd "$WORKDIR" && timeout "$((TIMEOUT_MIN * 60))" \
    claude -p "$TASK" --model "$MODEL" --effort "$EFFORT" --output-format json --max-budget-usd "$BUDGET" \
    --dangerously-skip-permissions 2>&1)" || STATUS=$?
fi
STATUS="${STATUS:-0}"

COST="$(printf '%s' "$OUT" | sed -n 's/.*"total_cost_usd"[: ]*\([0-9.]*\).*/\1/p' | head -n1)"
TURNS="$(printf '%s' "$OUT" | sed -n 's/.*"num_turns"[: ]*\([0-9]*\).*/\1/p' | head -n1)"
[[ -z "$COST" ]] && echo "WARNING: no total_cost_usd in claude output — wrapper will keep its conservative reservation" >&2

# Post-hoc sanity check stays as a belt-and-suspenders alert.
if [[ -n "$COST" && -n "$BUDGET" ]] && awk -v c="$COST" -v b="$BUDGET" 'BEGIN{exit !(c>b)}'; then
  echo "BUDGET EXCEEDED despite --max-budget-usd: \$$COST > \$$BUDGET — investigate before next run" >&2
  STATUS=7
fi

printf '%s\n' "$OUT"
if [[ -n "$COST" ]]; then
  echo "turns=${TURNS:-0} cost_usd=$COST"
else
  echo "turns=${TURNS:-0}"  # no cost_usd token → wrapper keeps the reservation
fi
exit "$STATUS"
