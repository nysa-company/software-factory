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

# (f) kit pin — the product certifies which kit commit it runs against.
# factory/KIT_PIN holds a kit commit SHA (full or short). Missing pin is a
# warning (single-project era); a mismatch is a hard fail so a kit upgrade
# never changes a project's behavior silently. A product living inside the
# kit repo itself (e.g. the Relay conformance app) is implicitly pinned.
KIT_PIN_FILE="$FACTORY_DIR/KIT_PIN"
KIT_HEAD="$(git -C "$KIT_DIR" rev-parse HEAD 2>/dev/null || true)"
PRODUCT_TOPLEVEL="$(git -C "$REPO_ROOT" rev-parse --show-toplevel 2>/dev/null || true)"
KIT_TOPLEVEL="$(git -C "$KIT_DIR" rev-parse --show-toplevel 2>/dev/null || true)"
if [[ -n "$KIT_TOPLEVEL" && "$PRODUCT_TOPLEVEL" == "$KIT_TOPLEVEL" ]]; then
  pass "product lives inside the kit repo — kit pin implicit ($( [[ -n "$KIT_HEAD" ]] && echo "${KIT_HEAD:0:7}" || echo unknown))"
elif [[ ! -f "$KIT_PIN_FILE" ]]; then
  warn "no kit pin ($KIT_PIN_FILE missing) — write the certified kit SHA there so kit upgrades are deliberate"
elif [[ -z "$KIT_HEAD" ]]; then
  fail "kit pin present but kit dir is not a git repo ($KIT_DIR)"
else
  PINNED_SHA="$(grep -m1 -oE '[0-9a-f]{7,40}' "$KIT_PIN_FILE" || true)"
  if [[ -z "$PINNED_SHA" ]]; then
    fail "kit pin file has no SHA: $KIT_PIN_FILE"
  elif [[ "$KIT_HEAD" == "$PINNED_SHA"* ]]; then
    pass "kit pin matches (${PINNED_SHA:0:7})"
  else
    fail "kit pin mismatch: kit at ${KIT_HEAD:0:7}, product certified against ${PINNED_SHA:0:7} — re-certify the project (run its suite against the new kit, update factory/KIT_PIN) or check out the pinned kit commit"
  fi
fi

# (g) GH_TOKEN — warn only
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
