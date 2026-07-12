#!/usr/bin/env bash
# Adapter: Claude Code CLI (model family A — builder, planner).
# Pinned CLI version below; the contract test fails loudly when the installed
# CLI drifts, instead of silently losing budget enforcement.
#
# Contract with run-agent.sh: accept the flags below, run the task,
# print agent output, and print a final line: "turns=N cost_usd=X".
set -euo pipefail

PINNED_VERSION="${CLAUDE_CODE_PINNED:-2.x}"  # set the exact installed version at instantiation

BUDGET="" MAX_TURNS="" TIMEOUT_MIN="" PROMPT_FILE="" WORKDIR="$PWD"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --budget) BUDGET="$2"; shift 2;;
    --max-turns) MAX_TURNS="$2"; shift 2;;
    --timeout-min) TIMEOUT_MIN="$2"; shift 2;;
    --prompt-file) PROMPT_FILE="$2"; shift 2;;
    --workdir) WORKDIR="$2"; shift 2;;
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

APPEND=()
[[ -s "$PROMPT_FILE" ]] && APPEND=(--append-system-prompt "$(cat "$PROMPT_FILE")")

# JSON output carries total_cost_usd and num_turns; timeout guards the wall clock.
OUT="$(cd "$WORKDIR" && timeout "$((TIMEOUT_MIN * 60))" \
  claude -p "$TASK" --output-format json --max-turns "$MAX_TURNS" "${APPEND[@]}" 2>&1)" || STATUS=$?
STATUS="${STATUS:-0}"

COST="$(printf '%s' "$OUT" | sed -n 's/.*"total_cost_usd"[: ]*\([0-9.]*\).*/\1/p' | head -n1)"
TURNS="$(printf '%s' "$OUT" | sed -n 's/.*"num_turns"[: ]*\([0-9]*\).*/\1/p' | head -n1)"

# Budget is checked post-hoc per run (the CLI has no hard USD stop); the daily
# cap in run-agent.sh is the cumulative backstop, console caps the final one.
if [[ -n "$COST" && -n "$BUDGET" ]] && awk -v c="$COST" -v b="$BUDGET" 'BEGIN{exit !(c>b)}'; then
  echo "BUDGET EXCEEDED: run cost \$$COST > per-run budget \$$BUDGET — flag on ticket" >&2
  STATUS=7
fi

printf '%s\n' "$OUT"
echo "turns=${TURNS:-0} cost_usd=${COST:-0}"
exit "$STATUS"
