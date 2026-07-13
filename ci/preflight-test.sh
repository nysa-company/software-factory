#!/usr/bin/env bash
# preflight-test.sh — sandboxed tests for scripts/preflight.sh.
# Stubs claude/codex/timeout on a prepended PATH; never invokes real CLIs.
set -euo pipefail

KIT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PREFLIGHT="$KIT_DIR/scripts/preflight.sh"
TMP="$(mktemp -d)"
STUB_BIN="$TMP/bin"
FAILURES=0

cleanup() { rm -rf "$TMP"; }
trap cleanup EXIT

mkdir -p "$STUB_BIN"

# --- stub CLIs that satisfy contract-test.sh and version-pin checks ---
write_stub_claude() {
  local ver="${1:-2.1.207}"
  cat > "$STUB_BIN/claude" <<STUB
#!/usr/bin/env bash
case "\${1:-}" in
  --version) echo "$ver (Claude Code)"; exit 0 ;;
  --help)
    echo "--max-budget-usd"
    echo "--output-format"
    echo "--append-system-prompt"
    exit 0 ;;
esac
exit 0
STUB
  chmod +x "$STUB_BIN/claude"
}

write_stub_codex() {
  local ver="${1:-0.144.1}"
  cat > "$STUB_BIN/codex" <<STUB
#!/usr/bin/env bash
case "\${1:-}" in
  --version) echo "codex-cli $ver"; exit 0 ;;
  exec)
    if [[ "\${2:-}" == "--help" ]]; then echo "--json"; fi
    exit 0
    ;;
esac
exit 0
STUB
  chmod +x "$STUB_BIN/codex"
}

write_stub_timeout() {
  cat > "$STUB_BIN/timeout" <<'STUB'
#!/usr/bin/env bash
shift
exec "$@"
STUB
  chmod +x "$STUB_BIN/timeout"
}

write_envelope() {
  local dir="$1" daily_cap="${2:-15.00}"
  mkdir -p "$dir/factory/tickets" "$dir/factory/initiatives"
  cat > "$dir/factory/ENVELOPE.env" <<ENV
PER_RUN_BUDGET_USD=1.50
PER_TICKET_BUDGET_USD=6.50
PER_RUN_MAX_TURNS=15
PER_RUN_TIMEOUT_MIN=5
DAILY_CAP_USD=$daily_cap
ENV
  echo "date,time,ticket,role,adapter,prompt_version,turns,cost_usd,exit_status" > "$dir/factory/ledger.csv"
}

write_ready_ticket() {
  local dir="$1" ticket="${2:-T-001}"
  cat > "$dir/factory/tickets/$ticket.md" <<TICKET
# $ticket — test ticket

State: Ready
Initiative: I-001
Priority: normal

## Description

Preflight test ticket.
TICKET
  cat > "$dir/factory/initiatives/I-001.md" <<'INITIATIVE'
# Test initiative

Status: planned

## Summary

Preflight fixture.
INITIATIVE
}

init_git_repo() {
  local dir="$1"
  local bare="$TMP/$(basename "$dir").git"
  git init --bare "$bare" >/dev/null 2>&1
  git -C "$dir" init -b main >/dev/null 2>&1
  git -C "$dir" config user.email "preflight@test.local"
  git -C "$dir" config user.name "preflight-test"
  [[ -f "$dir/README.md" ]] || echo "seed" > "$dir/README.md"
  git -C "$dir" add -A
  git -C "$dir" commit -m "init" >/dev/null 2>&1
  git -C "$dir" remote add origin "$bare"
  git -C "$dir" push -u origin main >/dev/null 2>&1
}

run_preflight() {
  local factory_root="$1" ticket="$2"
  shift 2
  local env_args=(PATH="$STUB_BIN:$PATH" FACTORY_ROOT="$factory_root")
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --global-env) env_args+=(FACTORY_GLOBAL_ENV="$2"); shift 2;;
      --home) env_args+=(HOME="$2"); shift 2;;
      --gh-token) env_args+=(GH_TOKEN="$2"); shift 2;;
      --projected) env_args+=(PROJECTED_TICKET_USD="$2"); shift 2;;
      *) echo "unknown run_preflight opt: $1" >&2; return 2;;
    esac
  done
  env "${env_args[@]}" bash "$PREFLIGHT" --ticket "$ticket" 2>&1
}

assert_preflight() {
  local name="$1" expect_exit="$2" expect_line="$3"
  local factory_root="$4" ticket="$5"
  shift 5
  local out rc=0
  out="$(run_preflight "$factory_root" "$ticket" "$@")" || rc=$?
  if [[ "$rc" -ne "$expect_exit" ]]; then
    echo "FAIL: $name — expected exit $expect_exit, got $rc"
    echo "$out"
    FAILURES=$((FAILURES + 1))
    return
  fi
  if ! grep -qF "$expect_line" <<<"$out"; then
    echo "FAIL: $name — missing expected line: $expect_line"
    echo "$out"
    FAILURES=$((FAILURES + 1))
    return
  fi
  echo "PASS: $name"
}

write_stub_claude "2.1.207"
write_stub_codex "0.144.1"
write_stub_timeout

# --- all-pass ---
ALLPASS="$TMP/allpass"
mkdir -p "$ALLPASS"
write_envelope "$ALLPASS" "15.00"
write_ready_ticket "$ALLPASS" "T-001"
init_git_repo "$ALLPASS"
GLOBAL_ENV="$TMP/allpass-global/global.env"
GLOBAL_LEDGER="$TMP/allpass-global/global-ledger.csv"
mkdir -p "$TMP/allpass-global"
cat > "$GLOBAL_ENV" <<ENV
GLOBAL_DAILY_CAP_USD=15.00
CLAUDE_CODE_PINNED=2.1.207
CODEX_PINNED=0.144.1
ENV
echo "date,time,repo,ticket,role,adapter,prompt_version,turns,cost_usd,exit_status" > "$GLOBAL_LEDGER"
assert_preflight "all-pass" 0 "PREFLIGHT PASS" "$ALLPASS" "T-001" --global-env "$GLOBAL_ENV"

# --- version-pin fail ---
PINFAIL="$TMP/pinfail"
mkdir -p "$PINFAIL"
write_envelope "$PINFAIL"
write_ready_ticket "$PINFAIL" "T-002"
init_git_repo "$PINFAIL"
write_stub_claude "9.9.999"
assert_preflight "version-pin fail" 1 "FAIL: Claude Code pin mismatch" "$PINFAIL" "T-002"
write_stub_claude "2.1.207"

# --- repo budget fail ---
REPOBUDGET="$TMP/repobudget"
mkdir -p "$REPOBUDGET"
write_envelope "$REPOBUDGET" "5.00"
write_ready_ticket "$REPOBUDGET" "T-003"
init_git_repo "$REPOBUDGET"
TODAY="$(date +%F)"
echo "$TODAY,12:00:00,T-000,planner,claude-code,v1,1,4.50,0" >> "$REPOBUDGET/factory/ledger.csv"
assert_preflight "repo budget fail" 1 "FAIL: repo daily cap insufficient" "$REPOBUDGET" "T-003"

# --- global budget fail ---
GLOBALBUDGET="$TMP/globalbudget"
mkdir -p "$GLOBALBUDGET"
write_envelope "$GLOBALBUDGET" "15.00"
write_ready_ticket "$GLOBALBUDGET" "T-004"
init_git_repo "$GLOBALBUDGET"
GLOBAL_ENV2="$TMP/globalbudget-global/global.env"
GLOBAL_LEDGER2="$TMP/globalbudget-global/global-ledger.csv"
mkdir -p "$TMP/globalbudget-global"
cat > "$GLOBAL_ENV2" <<ENV
GLOBAL_DAILY_CAP_USD=10.00
ENV
echo "date,time,repo,ticket,role,adapter,prompt_version,turns,cost_usd,exit_status" > "$GLOBAL_LEDGER2"
echo "$TODAY,12:00:00,/other,other,planner,claude-code,v1,1,9.00,0" >> "$GLOBAL_LEDGER2"
assert_preflight "global budget fail" 1 "FAIL: machine daily cap insufficient" \
  "$GLOBALBUDGET" "T-004" --global-env "$GLOBAL_ENV2"

# --- dirty clone fail ---
DIRTY="$TMP/dirty"
mkdir -p "$DIRTY"
write_envelope "$DIRTY"
write_ready_ticket "$DIRTY" "T-005"
init_git_repo "$DIRTY"
echo "dirty" > "$DIRTY/untracked.txt"
assert_preflight "dirty clone fail" 1 "FAIL: working tree not clean" "$DIRTY" "T-005"

# --- missing ticket fail ---
NOTICKET="$TMP/noticket"
mkdir -p "$NOTICKET"
write_envelope "$NOTICKET"
init_git_repo "$NOTICKET"
assert_preflight "missing ticket fail" 1 "FAIL: ticket file missing" "$NOTICKET" "T-999"

# --- kit pin: unpinned external product warns but passes ---
UNPINNED="$TMP/unpinned"
mkdir -p "$UNPINNED"
write_envelope "$UNPINNED"
write_ready_ticket "$UNPINNED" "T-007"
init_git_repo "$UNPINNED"
out="$(run_preflight "$UNPINNED" "T-007" --global-env "$TMP/no-global.env")" || true
if grep -qF "WARN: no kit pin" <<<"$out" && grep -qF "PREFLIGHT PASS" <<<"$out"; then
  echo "PASS: kit pin unpinned warns but passes"
else
  echo "FAIL: kit pin unpinned warns but passes"
  echo "$out"
  FAILURES=$((FAILURES + 1))
fi

# --- kit pin: match passes, mismatch fails ---
PINNED="$TMP/pinned"
mkdir -p "$PINNED"
write_envelope "$PINNED"
write_ready_ticket "$PINNED" "T-008"
init_git_repo "$PINNED"
KIT_HEAD_NOW="$(git -C "$KIT_DIR" rev-parse HEAD)"
echo "$KIT_HEAD_NOW" > "$PINNED/factory/KIT_PIN"
git -C "$PINNED" add -A && git -C "$PINNED" commit -qm "pin" && git -C "$PINNED" push -q origin main
assert_preflight "kit pin match passes" 0 "PASS: kit pin matches" "$PINNED" "T-008" --global-env "$TMP/no-global.env"
echo "0000000deadbeef0000000deadbeef00000000" > "$PINNED/factory/KIT_PIN"
git -C "$PINNED" add -A && git -C "$PINNED" commit -qm "bad pin" && git -C "$PINNED" push -q origin main
assert_preflight "kit pin mismatch fails" 1 "FAIL: kit pin mismatch" "$PINNED" "T-008" --global-env "$TMP/no-global.env"

# --- GH_TOKEN warn-but-pass ---
NOWARN="$TMP/nowarn"
mkdir -p "$NOWARN"
write_envelope "$NOWARN"
write_ready_ticket "$NOWARN" "T-006"
init_git_repo "$NOWARN"
FAKE_HOME="$TMP/fakehome"
mkdir -p "$FAKE_HOME"
out="$(run_preflight "$NOWARN" "T-006" --home "$FAKE_HOME" --gh-token "")" || rc=$?
rc="${rc:-0}"
if [[ "$rc" -ne 0 ]]; then
  echo "FAIL: GH_TOKEN warn-but-pass — expected exit 0, got $rc"
  echo "$out"
  FAILURES=$((FAILURES + 1))
elif grep -qF "WARN: GH_TOKEN not set" <<<"$out" && grep -qF "PREFLIGHT PASS" <<<"$out"; then
  echo "PASS: GH_TOKEN warn-but-pass"
else
  echo "FAIL: GH_TOKEN warn-but-pass — missing WARN or PREFLIGHT PASS"
  echo "$out"
  FAILURES=$((FAILURES + 1))
fi

if [[ $FAILURES -eq 0 ]]; then
  echo "preflight-test: all cases passed"
  exit 0
else
  echo "preflight-test: $FAILURES case(s) failed"
  exit 1
fi
