#!/usr/bin/env bash
# Adapter: Codex CLI (production family — planner, builder, narrator;
# flipped from checking roles 2026-07-13, operator decision).
# Codex does not expose the same budget controls as Claude Code; this adapter
# enforces wall-clock timeout, estimates cost from token usage in the output,
# and relies on the daily cap + console caps as the hard stops.
#
# Contract with run-agent.sh: accept the flags below, run the task,
# print agent output, and print a final line: "turns=N cost_usd=X".
set -euo pipefail

PINNED_VERSION="${CODEX_PINNED:-0.144.1}"  # pinned at shakedown 2026-07-11

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

command -v codex >/dev/null || { echo "codex CLI not installed" >&2; exit 6; }
INSTALLED="$(codex --version 2>/dev/null | head -n1 || true)"
case "$INSTALLED" in
  *"$PINNED_VERSION"*) : ;;
  *) echo "installed Codex does not match the approved version" >&2; exit 6 ;;
esac
if [[ "${FACTORY_CLI_INTERNAL_SANDBOX:-0}" == 1 ]]; then
  python3 - "${HOME:-}" "${TMPDIR:-}" "${FACTORY_CLI_ATTEMPT_ID:-}" <<'PY' || {
import os
import pathlib
import stat
import sys

home, tmp = map(pathlib.Path, sys.argv[1:3])
attempt = sys.argv[3]
root = home.parent
if (
    not attempt
    or root.name != attempt
    or home != root / "home"
    or tmp != root / "tmp"
    or any(not path.is_absolute() or path.is_symlink() or not path.is_dir()
           for path in (root, home, tmp, home / ".codex"))
    or (root / "owner").is_symlink()
    or (root / "owner").read_text(encoding="utf-8") != attempt + "\n"
):
    raise SystemExit(1)
for path in (root, home, tmp, home / ".codex"):
    info = path.stat()
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise SystemExit(1)
credential = home / ".codex/auth.json"
info = credential.lstat()
if (
    credential.is_symlink()
    or not stat.S_ISREG(info.st_mode)
    or info.st_uid != os.geteuid()
    or info.st_nlink != 1
    or stat.S_IMODE(info.st_mode) != 0o600
):
    raise SystemExit(1)
PY
    echo "Codex CLI attempt runtime is unsafe" >&2
    exit 6
  }
fi

FULL_TASK="$TASK"
if [[ -s "$PROMPT_FILE" ]]; then
  FULL_TASK="$(cat "$PROMPT_FILE")

$TASK"
fi

run_with_timeout() {
  if [[ "${FACTORY_TIMEOUT_FOREGROUND:-0}" == 1 ]]; then
    timeout --foreground "$@"
  else
    timeout "$@"
  fi
}

# First-real-run finding (2026-07-12): workspace-write is not enough — it
# blocks TCP listen (tests spawn a real server) and blocks git commits from
# worktrees (their .git metadata lives in the main repo, outside the sandbox).
# Same call as the claude adapter: bypass the CLI sandbox; containment comes
# from the envelope (budget, timeout), the worktree, and CI gates.
OUT="$(cd "$WORKDIR" && run_with_timeout "$((TIMEOUT_MIN * 60))" \
  codex exec --json --dangerously-bypass-approvals-and-sandbox \
    -m "$MODEL" -c "model_reasoning_effort=$EFFORT" "$FULL_TASK" 2>&1)" || STATUS=$?
STATUS="${STATUS:-0}"

# Cost estimation from token counts. If tokens are missing, emit NO cost token
# — the wrapper then keeps its conservative full-budget reservation instead of
# silently logging $0 against the caps.
IN_TOK="$(printf '%s' "$OUT" | sed -n 's/.*"input_tokens"[: ]*\([0-9]*\).*/\1/p' | tail -n1)"
OUT_TOK="$(printf '%s' "$OUT" | sed -n 's/.*"output_tokens"[: ]*\([0-9]*\).*/\1/p' | tail -n1)"
COST=""
if [[ -n "$IN_TOK" && -n "$OUT_TOK" ]]; then
  COST="$(awk -v i="$IN_TOK" -v o="$OUT_TOK" \
    -v in_rate="${CODEX_USD_PER_MTOK_IN:-1.25}" \
    -v out_rate="${CODEX_USD_PER_MTOK_OUT:-10}" \
    'BEGIN{printf "%.4f", (i*in_rate + o*out_rate)/1000000}')"
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
