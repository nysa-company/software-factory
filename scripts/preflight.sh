#!/usr/bin/env bash
# preflight.sh — kickoff checks before a ticket's first launch.
# The dispatcher runs this once per ticket before the first run-agent.sh call.
# Usage: preflight.sh --ticket T-NNN
# FACTORY_ROOT semantics match run-agent.sh (anchors factory/ under the repo root).
set -euo pipefail

KIT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="${FACTORY_ROOT:-$(git rev-parse --show-toplevel 2>/dev/null || echo "$PWD")}"
FACTORY_DIR="$REPO_ROOT/factory"
LEDGER="${FACTORY_LEDGER:-$FACTORY_DIR/ledger.csv}"
ENV_FILE="${FACTORY_ENVELOPE:-$FACTORY_DIR/ENVELOPE.env}"
PROJECTED_TICKET_USD="${PROJECTED_TICKET_USD:-5.00}"

TICKET=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --ticket) TICKET="$2"; shift 2;;
    *) echo "unknown arg: $1" >&2; exit 2;;
  esac
done
[[ -n "$TICKET" ]] || { echo "usage: preflight.sh --ticket T-NNN" >&2; exit 2; }

FAIL=0
pass() { echo "PASS: $*"; }
fail() { echo "FAIL: $*"; FAIL=1; }
warn() { echo "WARN: $*"; }

# --- optional machine-level cap (same anchor as run-agent.sh) ---
GLOBAL_ENV="${FACTORY_GLOBAL_ENV:-$HOME/.factory/global.env}"
GLOBAL_LEDGER=""
if [[ -f "$GLOBAL_ENV" ]]; then
  # shellcheck disable=SC1090
  source "$GLOBAL_ENV"
  GLOBAL_LEDGER="${GLOBAL_LEDGER:-$(dirname "$GLOBAL_ENV")/global-ledger.csv}"
fi

# (a) adapter contract test — CLIs must be on PATH and honor adapter assumptions
if "$KIT_DIR/scripts/adapters/contract-test.sh" >/dev/null 2>&1; then
  pass "adapter contract test passed"
else
  fail "adapter contract test failed — run scripts/adapters/contract-test.sh"
fi

# (b) version pins match installed CLIs (pins may come from ~/.factory/global.env)
CLAUDE_PIN="${CLAUDE_CODE_PINNED:-2.1.207}"
CODEX_PIN="${CODEX_PINNED:-0.144.1}"
if ! command -v claude >/dev/null; then
  fail "claude CLI not on PATH (required for version pin check)"
elif ! command -v codex >/dev/null; then
  fail "codex CLI not on PATH (required for version pin check)"
else
  INSTALLED_CLAUDE="$(claude --version 2>/dev/null | head -n1 || true)"
  INSTALLED_CODEX="$(codex --version 2>/dev/null | head -n1 || true)"
  case "$INSTALLED_CLAUDE" in
    *"$CLAUDE_PIN"*) pass "Claude Code pin matches ($CLAUDE_PIN)" ;;
    *) fail "Claude Code pin mismatch: installed ($INSTALLED_CLAUDE) != pinned ($CLAUDE_PIN)" ;;
  esac
  case "$INSTALLED_CODEX" in
    *"$CODEX_PIN"*) pass "Codex pin matches ($CODEX_PIN)" ;;
    *) fail "Codex pin mismatch: installed ($INSTALLED_CODEX) != pinned ($CODEX_PIN)" ;;
  esac
fi

# (c) daily budget — same spend computation as run-agent.sh, reserve = PROJECTED_TICKET_USD
if [[ ! -f "$ENV_FILE" ]]; then
  fail "envelope not found: $ENV_FILE"
else
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  TODAY="$(date +%F)"
  [[ -f "$LEDGER" ]] || echo "date,time,ticket,role,adapter,prompt_version,turns,cost_usd,exit_status" > "$LEDGER"
  SPENT_TODAY="$(awk -F, -v d="$TODAY" 'NR>1 && $1==d {s+=$8} END {printf "%.4f", s+0}' "$LEDGER")"
  if awk -v s="$SPENT_TODAY" -v r="$PROJECTED_TICKET_USD" -v cap="$DAILY_CAP_USD" 'BEGIN{exit !((s+r)>cap)}'; then
    fail "repo daily cap insufficient (spent \$$SPENT_TODAY + reserve \$$PROJECTED_TICKET_USD > \$$DAILY_CAP_USD)"
  else
    pass "repo daily budget covers projected ticket (\$$SPENT_TODAY spent + \$$PROJECTED_TICKET_USD reserve <= \$$DAILY_CAP_USD)"
  fi
  if [[ -n "$GLOBAL_LEDGER" && -n "${GLOBAL_DAILY_CAP_USD:-}" ]]; then
    mkdir -p "$(dirname "$GLOBAL_LEDGER")"
    [[ -f "$GLOBAL_LEDGER" ]] || echo "date,time,repo,ticket,role,adapter,prompt_version,turns,cost_usd,exit_status" > "$GLOBAL_LEDGER"
    SPENT_GLOBAL="$(awk -F, -v d="$TODAY" 'NR>1 && $1==d {s+=$9} END {printf "%.4f", s+0}' "$GLOBAL_LEDGER")"
    if awk -v s="$SPENT_GLOBAL" -v r="$PROJECTED_TICKET_USD" -v cap="$GLOBAL_DAILY_CAP_USD" 'BEGIN{exit !((s+r)>cap)}'; then
      fail "machine daily cap insufficient (spent \$$SPENT_GLOBAL + reserve \$$PROJECTED_TICKET_USD > \$$GLOBAL_DAILY_CAP_USD)"
    else
      pass "machine daily budget covers projected ticket (\$$SPENT_GLOBAL spent + \$$PROJECTED_TICKET_USD reserve <= \$$GLOBAL_DAILY_CAP_USD)"
    fi
  else
    pass "no machine-level daily cap configured"
  fi
fi

# (d) repo clone on main, clean, up to date with origin/main
if ! git -C "$REPO_ROOT" rev-parse --git-dir >/dev/null 2>&1; then
  fail "not a git repository: $REPO_ROOT"
else
  git -C "$REPO_ROOT" fetch origin >/dev/null 2>&1 || fail "git fetch origin failed"
  BRANCH="$(git -C "$REPO_ROOT" rev-parse --abbrev-ref HEAD)"
  if [[ "$BRANCH" != "main" ]]; then
    fail "not on main branch (on $BRANCH)"
  elif [[ -n "$(git -C "$REPO_ROOT" status --porcelain)" ]]; then
    fail "working tree not clean"
  else
    LOCAL="$(git -C "$REPO_ROOT" rev-parse HEAD)"
    if git -C "$REPO_ROOT" rev-parse origin/main >/dev/null 2>&1; then
      REMOTE="$(git -C "$REPO_ROOT" rev-parse origin/main)"
      if [[ "$LOCAL" != "$REMOTE" ]]; then
        fail "not up to date with origin/main (local ${LOCAL:0:7} != origin ${REMOTE:0:7})"
      else
        pass "repo on main, clean, up to date with origin/main"
      fi
    else
      fail "origin/main not found after fetch"
    fi
  fi
fi

# (e) ticket exists and is Ready
TICKET_FILE="$FACTORY_DIR/tickets/$TICKET.md"
if [[ ! -f "$TICKET_FILE" ]]; then
  fail "ticket file missing: $TICKET_FILE"
elif grep -qE '^State: Ready' "$TICKET_FILE"; then
  pass "ticket $TICKET is Ready"
else
  STATE="$(grep -m1 '^State:' "$TICKET_FILE" 2>/dev/null || echo 'State: unknown')"
  fail "ticket not Ready ($STATE)"
fi

# (f) GH_TOKEN — warn only
if [[ -n "${GH_TOKEN:-}" ]]; then
  pass "GH_TOKEN available (environment)"
elif [[ -f "$HOME/.hermes/profiles/factory/.env" ]] && grep -qE '^GH_TOKEN=' "$HOME/.hermes/profiles/factory/.env" 2>/dev/null; then
  pass "GH_TOKEN available (~/.hermes/profiles/factory/.env)"
else
  warn "GH_TOKEN not set — PR and CI status checks may fail"
fi

if [[ $FAIL -eq 0 ]]; then
  echo "PREFLIGHT PASS"
  exit 0
else
  echo "PREFLIGHT FAIL"
  exit 1
fi
