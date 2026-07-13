#!/usr/bin/env bash
# contract-test.sh — verifies each installed CLI still honors what its adapter
# assumes. Run after any CLI upgrade and at instantiation. Exits nonzero on
# any broken assumption so the factory stops before budgets silently fail.
set -uo pipefail
FAIL=0
note() { echo "[contract-test] $*"; }
bad() { echo "[contract-test] FAIL: $*" >&2; FAIL=1; }

# --- claude-code assumptions ---
# Capture help output once, then grep the variable: piping `cli --help` straight
# into `grep -q` under pipefail is racy — grep exits on the first match and can
# SIGPIPE the CLI, failing the pipeline spuriously (seen flaky in preflight tests).
if command -v claude >/dev/null; then
  note "claude: $(claude --version 2>/dev/null | head -n1)"
  CLAUDE_HELP="$(claude --help 2>/dev/null || true)"
  grep -q -- "--max-budget-usd" <<<"$CLAUDE_HELP" || bad "claude: --max-budget-usd flag missing (hard budget stop)"
  grep -q -- "--output-format" <<<"$CLAUDE_HELP" || bad "claude: --output-format flag missing"
  grep -q -- "--append-system-prompt" <<<"$CLAUDE_HELP" || bad "claude: --append-system-prompt flag missing"
else
  bad "claude CLI not installed"
fi

# --- codex assumptions ---
if command -v codex >/dev/null; then
  note "codex: $(codex --version 2>/dev/null | head -n1)"
  CODEX_HELP="$(codex exec --help 2>/dev/null || true)"
  grep -q -- "--json" <<<"$CODEX_HELP" || bad "codex: exec --json flag missing"
else
  bad "codex CLI not installed"
fi

# --- shared assumptions ---
command -v timeout >/dev/null || bad "GNU timeout not on PATH (brew install coreutils)"

if [[ $FAIL -eq 0 ]]; then
  note "all adapter contracts hold"
else
  echo "[contract-test] One or more contracts broken. Update the adapter(s), re-pin versions, and re-run before any factory run." >&2
fi
exit $FAIL
