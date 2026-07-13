#!/usr/bin/env bash
# Self-contained regression tests for run-agent.sh and next-stage.sh.
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_AGENT="$ROOT/scripts/run-agent.sh"
NEXT_STAGE="$ROOT/scripts/next-stage.sh"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/sf-factory-tests.XXXXXX")"
FAILURES=0

cleanup() {
  if [[ -n "${FIRST_PID:-}" ]] && kill -0 "$FIRST_PID" 2>/dev/null; then
    kill "$FIRST_PID" 2>/dev/null || true
    wait "$FIRST_PID" 2>/dev/null || true
  fi
  rm -rf "$TMP"
}
trap cleanup EXIT

pass() { echo "PASS: $1"; }
fail() { echo "FAIL: $1${2:+ — $2}" >&2; FAILURES=$((FAILURES + 1)); }

write_envelope() {
  mkdir -p "$1/factory"
  printf '%s\n' \
    'PER_RUN_BUDGET_USD=1.00' \
    'PER_TICKET_BUDGET_USD=20.00' \
    'PER_RUN_MAX_TURNS=5' \
    'PER_RUN_TIMEOUT_MIN=1' \
    'DAILY_CAP_USD=50.00' > "$1/factory/ENVELOPE.env"
}

run_mock() {
  FACTORY_ROOT="$1" \
  FACTORY_GLOBAL_ENV="$TMP/no-global.env" \
  FACTORY_ADAPTER_OVERRIDE=mock \
    "$RUN_AGENT" --role "$2" --ticket "$3" -- "test task"
}

ledger_header() {
  printf '%s\n' 'date,time,ticket,role,adapter,prompt_version,turns,cost_usd,exit_status'
}

ledger_row() {
  printf '2026-07-13,06:00:00,%s,%s,mock,test,1,0.10,0\n' "$1" "$2"
}

expect_stage() {
  local expected="$1" root="$2" ticket="$3" actual status
  actual="$(FACTORY_ROOT="$root" FACTORY_LEDGER="$root/factory/ledger.csv" "$NEXT_STAGE" --ticket "$ticket" 2>&1)"
  status=$?
  [[ "$actual" == "$expected"* ]] || {
    fail "$ticket expected '$expected'" "got '$actual' (status $status)"
    return 1
  }
  return 0
}

# Canonical ledger routing from a linked worktree.
MAIN="$TMP/main"
WT="$TMP/worktree"
mkdir -p "$MAIN/conformance"
write_envelope "$MAIN/conformance"
git -C "$MAIN" init -q
git -C "$MAIN" add conformance/factory/ENVELOPE.env
GIT_AUTHOR_NAME=test GIT_AUTHOR_EMAIL=test@example.com \
GIT_COMMITTER_NAME=test GIT_COMMITTER_EMAIL=test@example.com \
  git -C "$MAIN" commit -qm "fixture"
git -C "$MAIN" worktree add -q -b ticket-worktree "$WT"

if run_mock "$WT/conformance" planner T-200 >/dev/null &&
   [[ "$(awk -F, '$3=="T-200" {n++} END {print n+0}' "$MAIN/conformance/factory/ledger.csv")" == "1" ]] &&
   [[ ! -f "$WT/conformance/factory/ledger.csv" ]]; then
  pass "linked worktree writes canonical main ledger"
else
  fail "linked worktree writes canonical main ledger"
fi

# Sequencer reads that canonical ledger but the worktree-local ticket.
mkdir -p "$WT/conformance/factory/tickets"
printf '# T-200\n' > "$WT/conformance/factory/tickets/T-200.md"
STAGE="$(FACTORY_ROOT="$WT/conformance" "$NEXT_STAGE" --ticket T-200 2>&1)"
if [[ "$STAGE" == "RUN spec-linter" ]]; then
  pass "sequencer combines canonical ledger with caller ticket"
else
  fail "sequencer combines canonical ledger with caller ticket" "got '$STAGE'"
fi

# Explicit override wins over canonical routing.
OVERRIDE="$TMP/override/ledger.csv"
mkdir -p "$(dirname "$OVERRIDE")"
if FACTORY_ROOT="$WT/conformance" FACTORY_LEDGER="$OVERRIDE" \
     FACTORY_GLOBAL_ENV="$TMP/no-global.env" FACTORY_ADAPTER_OVERRIDE=mock \
     "$RUN_AGENT" --role planner --ticket T-201 -- "override" >/dev/null &&
   [[ "$(awk -F, '$3=="T-201" {n++} END {print n+0}' "$OVERRIDE")" == "1" ]] &&
   [[ "$(awk -F, '$3=="T-201" {n++} END {print n+0}' "$MAIN/conformance/factory/ledger.csv")" == "0" ]]; then
  pass "FACTORY_LEDGER override wins"
else
  fail "FACTORY_LEDGER override wins"
fi

# Semantic round numbering with one explicitly voided duplicate row.
ROUNDS="$TMP/rounds"
mkdir -p "$ROUNDS/factory/tickets"
{
  ledger_header
  ledger_row T-300 planner
  ledger_row T-300 test-author
  ledger_row T-300 builder
  ledger_row T-300 reviewer
  ledger_row T-300 reviewer
  ledger_row T-300 reviewer
} > "$ROUNDS/factory/ledger.csv"
cat > "$ROUNDS/factory/tickets/T-300.md" <<'EOF'
# T-300
reviewer round 1: REQUEST CHANGES — first
reviewer round 2: REQUEST CHANGES — second
OPERATOR NOTE: reviewer run 3 void — duplicate
EOF

if expect_stage "ESCALATE" "$ROUNDS" T-300 &&
   FACTORY_ROOT="$ROUNDS" FACTORY_LEDGER="$ROUNDS/factory/ledger.csv" \
     "$NEXT_STAGE" --ticket T-300 | grep -q "reviewer round 3"; then
  pass "duplicate row preserves semantic round 3"
fi

printf '%s\n' 'OPERATOR AUTHORIZATION: reviewer round 3' >> "$ROUNDS/factory/tickets/T-300.md"
if expect_stage "RUN reviewer" "$ROUNDS" T-300; then
  pass "semantic round authorization matches"
fi

# Missing verdict still refuses unless the extra row has a void note.
grep -v 'OPERATOR NOTE' "$ROUNDS/factory/tickets/T-300.md" > "$ROUNDS/factory/tickets/T-300.tmp"
mv "$ROUNDS/factory/tickets/T-300.tmp" "$ROUNDS/factory/tickets/T-300.md"
if expect_stage "REFUSE" "$ROUNDS" T-300; then
  pass "unrecorded non-void reviewer run refuses"
fi

# Duplicate-run guard: overlap refused, same ticket+role allowed afterward.
GUARD="$TMP/guard"
write_envelope "$GUARD"
GUARD_LEDGER="$GUARD/factory/ledger.csv"
MOCK_SLEEP=2 FACTORY_ROOT="$GUARD" FACTORY_LEDGER="$GUARD_LEDGER" \
  FACTORY_GLOBAL_ENV="$TMP/no-global.env" FACTORY_ADAPTER_OVERRIDE=mock \
  "$RUN_AGENT" --role builder --ticket T-400 -- "slow run" > "$TMP/first.out" 2>&1 &
FIRST_PID=$!
for _i in $(seq 1 50); do
  [[ -f "$GUARD/factory/.active-runs/T-400.builder.pid" ]] && break
  sleep 0.05
done
SECOND_OUTPUT="$(FACTORY_ROOT="$GUARD" FACTORY_LEDGER="$GUARD_LEDGER" \
  FACTORY_GLOBAL_ENV="$TMP/no-global.env" FACTORY_ADAPTER_OVERRIDE=mock \
  "$RUN_AGENT" --role builder --ticket T-400 -- "overlap" 2>&1)"
SECOND_STATUS=$?
wait "$FIRST_PID"
FIRST_PID=""

if [[ "$SECOND_STATUS" -eq 7 && "$SECOND_OUTPUT" == *"live run already exists"* ]]; then
  pass "duplicate-run guard refuses overlap"
else
  fail "duplicate-run guard refuses overlap" "status $SECOND_STATUS: $SECOND_OUTPUT"
fi

if run_mock "$GUARD" builder T-400 >/dev/null &&
   [[ "$(awk -F, '$3=="T-400" && $4=="builder" {n++} END {print n+0}' "$GUARD_LEDGER")" == "2" ]]; then
  pass "duplicate-run guard allows sequential run"
else
  fail "duplicate-run guard allows sequential run"
fi

# Full sequencer walkthrough: happy path.
WALK="$TMP/walk"
mkdir -p "$WALK/factory/tickets"
printf '# T-500\n' > "$WALK/factory/tickets/T-500.md"
ledger_header > "$WALK/factory/ledger.csv"
WALK_OK=1
expect_stage "RUN planner" "$WALK" T-500 || WALK_OK=0
ledger_row T-500 planner >> "$WALK/factory/ledger.csv"
expect_stage "RUN spec-linter" "$WALK" T-500 || WALK_OK=0
ledger_row T-500 spec-linter >> "$WALK/factory/ledger.csv"
expect_stage "REFUSE" "$WALK" T-500 || WALK_OK=0
printf 'SPEC-LINT: PASS\n' >> "$WALK/factory/tickets/T-500.md"
expect_stage "RUN test-author" "$WALK" T-500 || WALK_OK=0
ledger_row T-500 test-author >> "$WALK/factory/ledger.csv"
expect_stage "RUN builder" "$WALK" T-500 || WALK_OK=0
ledger_row T-500 builder >> "$WALK/factory/ledger.csv"
expect_stage "RUN reviewer" "$WALK" T-500 || WALK_OK=0
ledger_row T-500 reviewer >> "$WALK/factory/ledger.csv"
expect_stage "REFUSE" "$WALK" T-500 || WALK_OK=0
printf 'reviewer round 1: APPROVE\n' >> "$WALK/factory/tickets/T-500.md"
expect_stage "RUN narrator" "$WALK" T-500 || WALK_OK=0
ledger_row T-500 narrator >> "$WALK/factory/ledger.csv"
expect_stage "AWAIT-OPERATOR" "$WALK" T-500 || WALK_OK=0
[[ "$WALK_OK" -eq 1 ]] && pass "sequencer happy-path walkthrough"

# One rejection, a fix, and a successful second review.
printf '# T-501\n' > "$WALK/factory/tickets/T-501.md"
{
  ledger_row T-501 planner
  ledger_row T-501 test-author
  ledger_row T-501 builder
  ledger_row T-501 reviewer
} >> "$WALK/factory/ledger.csv"
REJECT_OK=1
printf 'reviewer round 1: REQUEST CHANGES — fix code\n' >> "$WALK/factory/tickets/T-501.md"
expect_stage "FIX builder-or-test-author" "$WALK" T-501 || REJECT_OK=0
ledger_row T-501 builder >> "$WALK/factory/ledger.csv"
expect_stage "RUN reviewer" "$WALK" T-501 || REJECT_OK=0
ledger_row T-501 reviewer >> "$WALK/factory/ledger.csv"
expect_stage "REFUSE" "$WALK" T-501 || REJECT_OK=0
printf 'reviewer round 2: APPROVE\n' >> "$WALK/factory/tickets/T-501.md"
expect_stage "RUN narrator" "$WALK" T-501 || REJECT_OK=0
[[ "$REJECT_OK" -eq 1 ]] && pass "sequencer rejection-round walkthrough"
# (T-501 seeded no spec-linter rows: a ticket already past test-author skips
# the lint gate — that is the backward-compatibility contract for old tickets.)

# Spec-lint fail → replan → lint → pass; second fail escalates.
printf '# T-502\n' > "$WALK/factory/tickets/T-502.md"
{
  ledger_row T-502 planner
  ledger_row T-502 spec-linter
} >> "$WALK/factory/ledger.csv"
LINT_OK=1
printf 'SPEC-LINT: FAIL — criterion 2 not pass/fail\n' >> "$WALK/factory/tickets/T-502.md"
expect_stage "RUN planner" "$WALK" T-502 || LINT_OK=0
ledger_row T-502 planner >> "$WALK/factory/ledger.csv"
expect_stage "RUN spec-linter" "$WALK" T-502 || LINT_OK=0
ledger_row T-502 spec-linter >> "$WALK/factory/ledger.csv"
expect_stage "REFUSE" "$WALK" T-502 || LINT_OK=0
printf 'SPEC-LINT: PASS\n' >> "$WALK/factory/tickets/T-502.md"
expect_stage "RUN test-author" "$WALK" T-502 || LINT_OK=0
[[ "$LINT_OK" -eq 1 ]] && pass "spec-lint fail-replan-pass walkthrough"

printf '# T-503\n' > "$WALK/factory/tickets/T-503.md"
{
  ledger_row T-503 planner
  ledger_row T-503 spec-linter
  ledger_row T-503 planner
  ledger_row T-503 spec-linter
} >> "$WALK/factory/ledger.csv"
printf 'SPEC-LINT: FAIL — round 1\nSPEC-LINT: FAIL — round 2\n' >> "$WALK/factory/tickets/T-503.md"
if expect_stage "ESCALATE spec-lint failed twice" "$WALK" T-503; then
  pass "spec-lint two-fail escalation"
fi

if [[ "$FAILURES" -gt 0 ]]; then
  echo "FAIL: $FAILURES factory-script test(s) failed" >&2
  exit 1
fi
echo "PASS: all factory-script tests"
