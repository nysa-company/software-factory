#!/usr/bin/env bash
# contract-test.sh — verifies each installed CLI still honors what its adapter
# assumes. Run after any CLI upgrade and at instantiation. Exits nonzero on
# any broken assumption so the factory stops before budgets silently fail.
set -uo pipefail
FAIL=0
note() { echo "[contract-test] $*"; }
bad() { echo "[contract-test] FAIL: $*" >&2; FAIL=1; }

# --- claude-code assumptions ---
if command -v claude >/dev/null; then
  note "claude: $(claude --version 2>/dev/null | head -n1)"
  claude --help 2>/dev/null | grep -q -- "--max-turns" || bad "claude: --max-turns flag missing"
  claude --help 2>/dev/null | grep -q -- "--output-format" || bad "claude: --output-format flag missing"
  claude --help 2>/dev/null | grep -q -- "--append-system-prompt" || bad "claude: --append-system-prompt flag missing"
else
  bad "claude CLI not installed"
fi

# --- codex assumptions ---
if command -v codex >/dev/null; then
  note "codex: $(codex --version 2>/dev/null | head -n1)"
  codex exec --help 2>/dev/null | grep -q -- "--json" || bad "codex: exec --json flag missing"
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
