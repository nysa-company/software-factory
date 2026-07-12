#!/usr/bin/env bash
# Adapter: Codex CLI (model family B — test-author, reviewer).
# Codex does not expose the same budget controls as Claude Code; this adapter
# enforces wall-clock timeout, estimates cost from token usage in the output,
# and relies on the daily cap + console caps as the hard stops.
#
# Contract with run-agent.sh: accept the flags below, run the task,
# print agent output, and print a final line: "turns=N cost_usd=X".
set -euo pipefail

PINNED_VERSION="${CODEX_PINNED:-0.144.1}"  # pinned at shakedown 2026-07-11

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

command -v codex >/dev/null || { echo "codex CLI not installed" >&2; exit 6; }
INSTALLED="$(codex --version 2>/dev/null | head -n1 || true)"
case "$INSTALLED" in
  *"$PINNED_VERSION"*) : ;;
  *) echo "WARNING: installed Codex ($INSTALLED) != pinned ($PINNED_VERSION). Run adapters/contract-test.sh before continuing." >&2 ;;
esac

FULL_TASK="$TASK"
[[ -s "$PROMPT_FILE" ]] && FULL_TASK="$(cat "$PROMPT_FILE")

$TASK"

# First-real-run finding (2026-07-12): exec mode defaults to a read-only
# sandbox; test-author must write test files and commit, so grant
# workspace-write (still sandboxed to the worktree; exec never prompts).
OUT="$(cd "$WORKDIR" && timeout "$((TIMEOUT_MIN * 60))" \
  codex exec --json -s workspace-write "$FULL_TASK" 2>&1)" || STATUS=$?
STATUS="${STATUS:-0}"

# Cost estimation from token counts. If tokens are missing, emit NO cost token
# — the wrapper then keeps its conservative full-budget reservation instead of
# silently logging $0 against the caps.
IN_TOK="$(printf '%s' "$OUT" | sed -n 's/.*"input_tokens"[: ]*\([0-9]*\).*/\1/p' | tail -n1)"
OUT_TOK="$(printf '%s' "$OUT" | sed -n 's/.*"output_tokens"[: ]*\([0-9]*\).*/\1/p' | tail -n1)"
COST=""
if [[ -n "$IN_TOK" && -n "$OUT_TOK" ]]; then
  COST="$(awk -v i="$IN_TOK" -v o="$OUT_TOK" \
    -v ir="${CODEX_USD_PER_MTOK_IN:-1.25}" -v or="${CODEX_USD_PER_MTOK_OUT:-10}" \
    'BEGIN{printf "%.4f", (i*ir + o*or)/1000000}')"
else
  echo "WARNING: no token usage in codex output — wrapper will keep its conservative reservation. Reconcile with console." >&2
fi

if [[ -n "$COST" ]] && awk -v c="$COST" -v b="${BUDGET:-999999}" 'BEGIN{exit !(c>b)}'; then
  echo "BUDGET EXCEEDED: run cost \$$COST > per-run budget \$$BUDGET — flag on ticket" >&2
  STATUS=7
fi

printf '%s\n' "$OUT"
if [[ -n "$COST" ]]; then
  echo "turns=1 cost_usd=$COST"
else
  echo "turns=1"
fi
exit "$STATUS"
