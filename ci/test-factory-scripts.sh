#!/usr/bin/env bash
# Self-contained regression tests for run-agent.sh and next-stage.sh.
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_AGENT="$ROOT/scripts/run-agent.sh"
NEXT_STAGE="$ROOT/scripts/next-stage.sh"
KILL_SWITCH="$ROOT/scripts/kill-switch.sh"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/sf-factory-tests.XXXXXX")"
STUB_BIN="$TMP/bin"
FAILURES=0
mkdir -p "$STUB_BIN"

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

write_backend_stubs() {
  cat > "$STUB_BIN/codex" <<'STUB'
#!/usr/bin/env bash
case "${1:-}" in
  --version)
    [[ "${STUB_CODEX_VERSION_EMPTY:-0}" == "1" ]] || echo "codex-cli 0.144.1"
    exit "${STUB_CODEX_VERSION_STATUS:-0}"
    ;;
  login) [[ "${2:-}" == "status" ]] && exit 0 ;;
  exec)
    if [[ "${2:-}" == "--help" ]]; then echo "--json"; exit 0; fi
    [[ -z "${FACTORY_TEST_TRACE:-}" ]] || echo "codex-task" >> "$FACTORY_TEST_TRACE"
    echo '{"type":"turn.completed","usage":{"input_tokens":100,"output_tokens":20}}'
    exit "${STUB_CODEX_STATUS:-0}"
    ;;
esac
exit 0
STUB
  chmod +x "$STUB_BIN/codex"

  cat > "$STUB_BIN/claude" <<'STUB'
#!/usr/bin/env bash
case "${1:-}" in
  --version)
    [[ "${STUB_CLAUDE_VERSION_EMPTY:-0}" == "1" ]] || echo "2.1.207 (Claude Code)"
    exit "${STUB_CLAUDE_VERSION_STATUS:-0}"
    ;;
  --help)
    printf '%s\n' --max-budget-usd --output-format --append-system-prompt
    exit 0 ;;
  auth) [[ "${2:-}" == "status" ]] && exit 0 ;;
  -p)
    [[ -z "${FACTORY_TEST_TRACE:-}" ]] || echo "claude-task" >> "$FACTORY_TEST_TRACE"
    echo '{"type":"result","num_turns":2,"total_cost_usd":0.10}'
    exit "${STUB_CLAUDE_STATUS:-0}"
    ;;
esac
exit 0
STUB
  chmod +x "$STUB_BIN/claude"

  cat > "$STUB_BIN/agent" <<'STUB'
#!/usr/bin/env bash
case "${1:-}" in
  --version|-v) echo "Cursor Agent 2026.07.test"; exit 0 ;;
  --help|-h)
    printf '%s\n' --print --output-format --workspace --model --force --trust
    exit 0 ;;
  status)
    if [[ "${STUB_CURSOR_AUTH_FALSE:-0}" == "1" ]]; then
      echo '{"authenticated":false}'
    else
      echo '{"authenticated":true}'
    fi
    exit 0 ;;
  models) printf '%s\n' ${STUB_CURSOR_MODELS:-gpt-5.6-sol-high claude-sonnet-5-thinking-high}; exit 0 ;;
esac

[[ -z "${FACTORY_TEST_TRACE:-}" ]] || echo "cursor-task" >> "$FACTORY_TEST_TRACE"
if [[ "${STUB_CURSOR_MALFORMED:-0}" == "1" ]]; then
  echo '{"type":"assistant","message":{"content":"no terminal result"}}'
  exit 0
fi
MODEL=""
WORKSPACE=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --model) MODEL="$2"; shift 2 ;;
    --workspace) WORKSPACE="$2"; shift 2 ;;
    *) shift ;;
  esac
done
case "$MODEL" in
  gpt-5.6-sol-high) REPORTED_MODEL="GPT-5.6 Sol 272K High" ;;
  claude-sonnet-5-thinking-high) REPORTED_MODEL="Sonnet 5 300K High" ;;
  *) REPORTED_MODEL="$MODEL" ;;
esac
REPORTED_MODEL="${STUB_CURSOR_REPORTED_MODEL:-$REPORTED_MODEL}"
printf '{"type":"system","subtype":"init","model":"%s","cwd":"%s"}\n' "$REPORTED_MODEL" "$WORKSPACE"
echo '{"type":"assistant","message":{"content":"Authorization: Bearer abc123 https://user:pass@example.com"},"api_token":"supersecret"}'
echo '{"type":"assistant","message":{"content":"stub 2"}}'
echo '{"type":"result","subtype":"success","usage":{"inputTokens":"70","outputTokens":20,"cacheReadTokens":10,"cacheWriteTokens":5}}'
exit "${STUB_CURSOR_STATUS:-0}"
STUB
  chmod +x "$STUB_BIN/agent"
}

write_backend_global() {
  local file="$1" extra="${2:-}"
  mkdir -p "$(dirname "$file")"
  cat > "$file" <<ENV
export GLOBAL_DAILY_CAP_USD=50.00
export CLAUDE_CODE_PINNED=2.1.207
export CODEX_PINNED=0.144.1
export FACTORY_CURSOR_FALLBACK_ENABLED=1
export CURSOR_AGENT_VERSION=2026.07.test
export CURSOR_OPENAI_MODEL=gpt-5.6-sol-high
export CURSOR_ANTHROPIC_MODEL=claude-sonnet-5-thinking-high
$extra
ENV
}

write_backend_stubs

AUTO_PROBE="$(PATH="$STUB_BIN:$PATH" FACTORY_CURSOR_FALLBACK_ENABLED=1 \
  CURSOR_AGENT_VERSION=2026.07.test CURSOR_OPENAI_MODEL=auto \
  bash -c 'source "$1"; factory_probe_adapter cursor-openai; echo "$PROBE_STATE:$PROBE_REASON"' \
  _ "$ROOT/scripts/lib/backend-policy.sh")"
MISMATCH_PROBE="$(PATH="$STUB_BIN:$PATH" FACTORY_CURSOR_FALLBACK_ENABLED=1 \
  CURSOR_AGENT_VERSION=2026.07.test CURSOR_OPENAI_MODEL=claude-sonnet-5-thinking-high \
  bash -c 'source "$1"; factory_probe_adapter cursor-openai; echo "$PROBE_STATE:$PROBE_REASON"' \
  _ "$ROOT/scripts/lib/backend-policy.sh")"
if [[ "$AUTO_PROBE" == "INVALID:model_not_explicit" &&
      "$MISMATCH_PROBE" == "INVALID:model_not_allowlisted" ]]; then
  pass "Cursor model policy rejects auto and cross-family IDs"
else
  fail "Cursor model policy rejects auto and cross-family IDs" \
    "auto=$AUTO_PROBE mismatch=$MISMATCH_PROBE"
fi

SUBSTRING_MODEL_PROBE="$(PATH="$STUB_BIN:$PATH" FACTORY_CURSOR_FALLBACK_ENABLED=1 \
  CURSOR_AGENT_VERSION=2026.07.test CURSOR_OPENAI_MODEL=gpt-5.6-sol-high \
  STUB_CURSOR_MODELS=gpt-5.6-sol-high-fast \
  bash -c 'source "$1"; factory_probe_adapter cursor-openai; echo "$PROBE_STATE:$PROBE_REASON"' \
  _ "$ROOT/scripts/lib/backend-policy.sh")"
SUBSTRING_VERSION_PROBE="$(PATH="$STUB_BIN:$PATH" FACTORY_CURSOR_FALLBACK_ENABLED=1 \
  CURSOR_AGENT_VERSION=2026.07.test-extra CURSOR_OPENAI_MODEL=gpt-5.6-sol-high \
  bash -c 'source "$1"; factory_probe_adapter cursor-openai; echo "$PROBE_STATE:$PROBE_REASON"' \
  _ "$ROOT/scripts/lib/backend-policy.sh")"
if [[ "$SUBSTRING_MODEL_PROBE" == "INVALID:model_unavailable" &&
      "$SUBSTRING_VERSION_PROBE" == "INVALID:version_mismatch" ]]; then
  pass "Cursor readiness rejects substring model and version matches"
else
  fail "Cursor readiness rejects substring model and version matches" \
    "model=$SUBSTRING_MODEL_PROBE version=$SUBSTRING_VERSION_PROBE"
fi

FALSE_AUTH_PROBE="$(PATH="$STUB_BIN:$PATH" FACTORY_CURSOR_FALLBACK_ENABLED=1 \
  CURSOR_AGENT_VERSION=2026.07.test CURSOR_OPENAI_MODEL=gpt-5.6-sol-high \
  STUB_CURSOR_AUTH_FALSE=1 \
  bash -c 'source "$1"; factory_probe_adapter cursor-openai; echo "$PROBE_STATE:$PROBE_REASON"' \
  _ "$ROOT/scripts/lib/backend-policy.sh")"
if [[ "$FALSE_AUTH_PROBE" == "UNAVAILABLE:authentication_unavailable" ]]; then
  pass "Cursor status JSON must affirm authentication"
else
  fail "Cursor status JSON must affirm authentication" "$FALSE_AUTH_PROBE"
fi

EMPTY_CODEX_VERSION_PROBE="$(PATH="$STUB_BIN:$PATH" CODEX_PINNED=0.144.1 \
  STUB_CODEX_VERSION_EMPTY=1 STUB_CODEX_VERSION_STATUS=124 \
  bash -c 'set -euo pipefail; source "$1"; factory_probe_adapter codex; echo "$PROBE_STATE:$PROBE_REASON"' \
  _ "$ROOT/scripts/lib/backend-policy.sh")"
EMPTY_CLAUDE_VERSION_PROBE="$(PATH="$STUB_BIN:$PATH" CLAUDE_CODE_PINNED=2.1.207 \
  STUB_CLAUDE_VERSION_EMPTY=1 STUB_CLAUDE_VERSION_STATUS=124 \
  bash -c 'set -euo pipefail; source "$1"; factory_probe_adapter claude-code; echo "$PROBE_STATE:$PROBE_REASON"' \
  _ "$ROOT/scripts/lib/backend-policy.sh")"
if [[ "$EMPTY_CODEX_VERSION_PROBE" == "UNAVAILABLE:version_probe_failed" &&
      "$EMPTY_CLAUDE_VERSION_PROBE" == "UNAVAILABLE:version_probe_failed" ]]; then
  pass "empty primary version probes permit startup fallback"
else
  fail "empty primary version probes permit startup fallback" \
    "codex=$EMPTY_CODEX_VERSION_PROBE claude=$EMPTY_CLAUDE_VERSION_PROBE"
fi

run_mock() {
  FACTORY_ROOT="$1" \
  FACTORY_GLOBAL_ENV="$TMP/no-global.env" \
  FACTORY_TEST_MODE=1 \
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

# Main-clone subdirectory root: canonical ledger must resolve to itself
# (regression: relative --git-common-dir was resolved against the wrong base,
# producing a nonexistent path and an empty LEDGER).
if run_mock "$MAIN/conformance" planner T-202 >/dev/null 2>"$TMP/mainclone.err" &&
   [[ "$(awk -F, '$3=="T-202" {n++} END {print n+0}' "$MAIN/conformance/factory/ledger.csv")" == "1" ]] &&
   ! grep -q "No such file or directory" "$TMP/mainclone.err"; then
  pass "main-clone subdirectory root writes its own ledger"
else
  fail "main-clone subdirectory root writes its own ledger" "$(cat "$TMP/mainclone.err")"
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
     FACTORY_GLOBAL_ENV="$TMP/no-global.env" FACTORY_TEST_MODE=1 \
     FACTORY_ADAPTER_OVERRIDE=mock \
     "$RUN_AGENT" --role planner --ticket T-201 -- "override" >/dev/null &&
   [[ "$(awk -F, '$3=="T-201" {n++} END {print n+0}' "$OVERRIDE")" == "1" ]] &&
   [[ "$(awk -F, '$3=="T-201" {n++} END {print n+0}' "$MAIN/conformance/factory/ledger.csv")" == "0" ]]; then
  pass "FACTORY_LEDGER override wins"
else
  fail "FACTORY_LEDGER override wins"
fi

# Legacy or partial headers migrate to the complete append-only schema.
PARTIAL="$TMP/partial-ledger"
write_envelope "$PARTIAL"
printf '%s\n' \
  'date,time,ticket,role,adapter,prompt_version,turns,cost_usd,exit_status,run_id,provider_family' \
  > "$PARTIAL/factory/ledger.csv"
echo '2026-07-12,01:00:00,T-OLD,planner,codex,v1,1,0.10,0,old-run,openai' \
  >> "$PARTIAL/factory/ledger.csv"
if run_mock "$PARTIAL" planner T-203 >/dev/null &&
   [[ "$(awk 'NR==1 {print; exit}' "$PARTIAL/factory/ledger.csv")" == \
      "date,time,ticket,role,adapter,prompt_version,turns,cost_usd,exit_status,run_id,provider_family,model_id,selection_reason,cost_basis,adapter_version" ]] &&
   [[ "$(awk -F, '$3=="T-OLD" {print $9}' "$PARTIAL/factory/ledger.csv")" == "0" ]]; then
  pass "partial ledger header migrates to complete schema"
else
  fail "partial ledger header migrates to complete schema"
fi

# Unknown/future schemas fail closed and are never rewritten.
FUTURE="$TMP/future-ledger"
write_envelope "$FUTURE"
echo 'date,time,ticket,future_schema' > "$FUTURE/factory/ledger.csv"
FUTURE_STATUS=0
run_mock "$FUTURE" planner T-205 >/dev/null 2>&1 || FUTURE_STATUS=$?
if [[ "$FUTURE_STATUS" -eq 3 &&
      "$(awk 'NR==1 {print; exit}' "$FUTURE/factory/ledger.csv")" == \
      "date,time,ticket,future_schema" ]]; then
  pass "unknown ledger schema fails closed"
else
  fail "unknown ledger schema fails closed" "status $FUTURE_STATUS"
fi

# Mock override is impossible without the explicit test-mode gate.
MOCK_GUARD="$TMP/mock-guard"
write_envelope "$MOCK_GUARD"
MOCK_GUARD_STATUS=0
FACTORY_ROOT="$MOCK_GUARD" FACTORY_GLOBAL_ENV="$TMP/no-global.env" \
  FACTORY_ADAPTER_OVERRIDE=mock \
  "$RUN_AGENT" --role planner --ticket T-204 -- "forbidden mock" >/dev/null 2>&1 ||
  MOCK_GUARD_STATUS=$?
if [[ "$MOCK_GUARD_STATUS" -eq 2 && ! -f "$MOCK_GUARD/factory/ledger.csv" ]]; then
  pass "mock override requires explicit test mode"
else
  fail "mock override requires explicit test mode" "status $MOCK_GUARD_STATUS"
fi

# Backend resolution: primary success submits exactly one primary task.
PRIMARY="$TMP/primary-route"
write_envelope "$PRIMARY"
PRIMARY_GLOBAL="$TMP/primary-global/global.env"
write_backend_global "$PRIMARY_GLOBAL"
PRIMARY_TRACE="$TMP/primary.trace"
PRIMARY_OUT="$TMP/primary.out"
PRIMARY_STATUS=0
: > "$PRIMARY_TRACE"
PATH="$STUB_BIN:$PATH" FACTORY_ROOT="$PRIMARY" \
  FACTORY_GLOBAL_ENV="$PRIMARY_GLOBAL" FACTORY_TEST_TRACE="$PRIMARY_TRACE" \
  "$RUN_AGENT" --role planner --ticket T-210 -- "primary route" \
  > "$PRIMARY_OUT" 2>&1 || PRIMARY_STATUS=$?
if [[ "$PRIMARY_STATUS" -eq 0 ]] &&
   [[ "$(awk -F, '$3=="T-210" {print $5}' "$PRIMARY/factory/ledger.csv")" == "codex" ]] &&
   [[ "$(awk -F, '$3=="T-210" {print $11}' "$PRIMARY/factory/ledger.csv")" == "openai" ]] &&
   [[ "$(awk -F, '$3=="T-210" {print $13}' "$PRIMARY/factory/ledger.csv")" == "primary_ready" ]] &&
   [[ "$(wc -l < "$PRIMARY_TRACE" | tr -d ' ')" == "1" ]] &&
   grep -q '^codex-task$' "$PRIMARY_TRACE"; then
  pass "ready primary submits exactly one primary task"
else
  fail "ready primary submits exactly one primary task" "status $PRIMARY_STATUS"
  awk '{print "  | " $0}' "$PRIMARY_OUT" >&2
fi

# A non-task UNAVAILABLE probe selects family-matched Cursor before reservation.
FALLBACK="$TMP/fallback-route"
write_envelope "$FALLBACK"
FALLBACK_GLOBAL="$TMP/fallback-global/global.env"
write_backend_global "$FALLBACK_GLOBAL" \
  "export FACTORY_PROBE_CODEX=UNAVAILABLE:test_primary_down"
FALLBACK_TRACE="$TMP/fallback.trace"
: > "$FALLBACK_TRACE"
if PATH="$STUB_BIN:$PATH" FACTORY_ROOT="$FALLBACK" \
     FACTORY_GLOBAL_ENV="$FALLBACK_GLOBAL" FACTORY_TEST_TRACE="$FALLBACK_TRACE" \
     "$RUN_AGENT" --role planner --ticket T-211 -- "fallback route" >/dev/null &&
   [[ "$(awk -F, '$3=="T-211" {print $5}' "$FALLBACK/factory/ledger.csv")" == "cursor-openai" ]] &&
   [[ "$(awk -F, '$3=="T-211" {print $12}' "$FALLBACK/factory/ledger.csv")" == "gpt-5.6-sol-high" ]] &&
   [[ "$(awk -F, '$3=="T-211" {print $14}' "$FALLBACK/factory/ledger.csv")" == "conservative_reservation" ]] &&
   [[ "$(awk -F, '$3=="T-211" {print $8}' "$FALLBACK/factory/ledger.csv")" == "1.00" ]] &&
   [[ "$(wc -l < "$FALLBACK_TRACE" | tr -d ' ')" == "1" ]] &&
   grep -q '^cursor-task$' "$FALLBACK_TRACE"; then
  FALLBACK_OUT="$(ls "$FALLBACK/factory/runs/"*.out)"
  FALLBACK_META="$(ls "$FALLBACK/factory/runs/"*.meta)"
  if ! grep -qE 'supersecret|abc123|user:pass' "$FALLBACK_OUT" &&
     grep -q '\[REDACTED\]' "$FALLBACK_OUT" &&
     grep -q 'input_tokens=70' "$FALLBACK_OUT" &&
     grep -q 'cache_tokens=15' "$FALLBACK_OUT" &&
     grep -q '^phase=completed$' "$FALLBACK_META"; then
    pass "unavailable primary selects one redacted Cursor task"
  else
    fail "unavailable primary selects one redacted Cursor task"
  fi
else
  fail "unavailable primary selects one redacted Cursor task"
fi

# Checking roles select the Anthropic-typed Cursor adapter.
ANTHROPIC_FALLBACK="$TMP/anthropic-fallback"
write_envelope "$ANTHROPIC_FALLBACK"
ANTHROPIC_GLOBAL="$TMP/anthropic-global/global.env"
write_backend_global "$ANTHROPIC_GLOBAL" \
  "export FACTORY_PROBE_CLAUDE_CODE=UNAVAILABLE:test_primary_down"
ANTHROPIC_TRACE="$TMP/anthropic.trace"
: > "$ANTHROPIC_TRACE"
if PATH="$STUB_BIN:$PATH" FACTORY_ROOT="$ANTHROPIC_FALLBACK" \
     FACTORY_GLOBAL_ENV="$ANTHROPIC_GLOBAL" FACTORY_TEST_TRACE="$ANTHROPIC_TRACE" \
     "$RUN_AGENT" --role spec-linter --ticket T-214 -- "checking fallback" >/dev/null &&
   [[ "$(awk -F, '$3=="T-214" {print $5}' "$ANTHROPIC_FALLBACK/factory/ledger.csv")" == "cursor-anthropic" ]] &&
   [[ "$(awk -F, '$3=="T-214" {print $11}' "$ANTHROPIC_FALLBACK/factory/ledger.csv")" == "anthropic" ]] &&
   [[ "$(awk -F, '$3=="T-214" {print $12}' "$ANTHROPIC_FALLBACK/factory/ledger.csv")" == "claude-sonnet-5-thinking-high" ]] &&
   [[ "$(wc -l < "$ANTHROPIC_TRACE" | tr -d ' ')" == "1" ]]; then
  pass "checking fallback preserves Anthropic family"
else
  fail "checking fallback preserves Anthropic family"
fi

# Malformed Cursor output is a terminal failed run, never another fallback.
MALFORMED="$TMP/malformed-cursor"
write_envelope "$MALFORMED"
MALFORMED_GLOBAL="$TMP/malformed-global/global.env"
write_backend_global "$MALFORMED_GLOBAL" \
  "export FACTORY_PROBE_CODEX=UNAVAILABLE:test_primary_down"
MALFORMED_TRACE="$TMP/malformed.trace"
: > "$MALFORMED_TRACE"
MALFORMED_STATUS=0
PATH="$STUB_BIN:$PATH" FACTORY_ROOT="$MALFORMED" \
  FACTORY_GLOBAL_ENV="$MALFORMED_GLOBAL" FACTORY_TEST_TRACE="$MALFORMED_TRACE" \
  STUB_CURSOR_MALFORMED=1 \
  "$RUN_AGENT" --role planner --ticket T-215 -- "malformed output" >/dev/null 2>&1 ||
  MALFORMED_STATUS=$?
if [[ "$MALFORMED_STATUS" -eq 9 &&
      "$(wc -l < "$MALFORMED_TRACE" | tr -d ' ')" == "1" ]] &&
   [[ "$(awk -F, '$3=="T-215" {print $9}' "$MALFORMED/factory/ledger.csv")" == "9" ]]; then
  pass "malformed Cursor output fails without another task"
else
  fail "malformed Cursor output fails without another task" "status $MALFORMED_STATUS"
fi

# A reported opposite-family model fails closed despite successful CLI exit.
MODEL_DRIFT="$TMP/model-drift"
write_envelope "$MODEL_DRIFT"
MODEL_DRIFT_GLOBAL="$TMP/model-drift-global/global.env"
write_backend_global "$MODEL_DRIFT_GLOBAL" \
  "export FACTORY_PROBE_CODEX=UNAVAILABLE:test_primary_down"
MODEL_DRIFT_STATUS=0
PATH="$STUB_BIN:$PATH" FACTORY_ROOT="$MODEL_DRIFT" \
  FACTORY_GLOBAL_ENV="$MODEL_DRIFT_GLOBAL" \
  STUB_CURSOR_REPORTED_MODEL="Claude 4 Sonnet" \
  "$RUN_AGENT" --role planner --ticket T-217 -- "model drift" >/dev/null 2>&1 ||
  MODEL_DRIFT_STATUS=$?
if [[ "$MODEL_DRIFT_STATUS" -eq 9 &&
      "$(awk -F, '$3=="T-217" {print $9}' "$MODEL_DRIFT/factory/ledger.csv")" == "9" ]]; then
  pass "reported cross-family Cursor model fails closed"
else
  fail "reported cross-family Cursor model fails closed" "status $MODEL_DRIFT_STATUS"
fi

# Once the primary task starts, its failure never launches Cursor.
TASK_FAIL="$TMP/task-fail"
write_envelope "$TASK_FAIL"
TASK_FAIL_GLOBAL="$TMP/task-fail-global/global.env"
write_backend_global "$TASK_FAIL_GLOBAL"
TASK_FAIL_TRACE="$TMP/task-fail.trace"
TASK_FAIL_OUT="$TMP/task-fail.out"
: > "$TASK_FAIL_TRACE"
TASK_FAIL_STATUS=0
PATH="$STUB_BIN:$PATH" FACTORY_ROOT="$TASK_FAIL" \
  FACTORY_GLOBAL_ENV="$TASK_FAIL_GLOBAL" FACTORY_TEST_TRACE="$TASK_FAIL_TRACE" \
  STUB_CODEX_STATUS=42 \
  "$RUN_AGENT" --role planner --ticket T-212 -- "task failure" \
  > "$TASK_FAIL_OUT" 2>&1 ||
  TASK_FAIL_STATUS=$?
if [[ "$TASK_FAIL_STATUS" -eq 42 &&
      "$(wc -l < "$TASK_FAIL_TRACE" | tr -d ' ')" == "1" ]] &&
   grep -q '^codex-task$' "$TASK_FAIL_TRACE" &&
   ! grep -q 'cursor-task' "$TASK_FAIL_TRACE"; then
  pass "post-submission primary failure never launches Cursor"
else
  fail "post-submission primary failure never launches Cursor" "status $TASK_FAIL_STATUS"
  awk '{print "  | " $0}' "$TASK_FAIL_OUT" >&2
fi

# INVALID primary state fails closed before any task process or reservation.
INVALID="$TMP/invalid-route"
write_envelope "$INVALID"
INVALID_GLOBAL="$TMP/invalid-global/global.env"
write_backend_global "$INVALID_GLOBAL" \
  "export FACTORY_PROBE_CODEX=INVALID:test_contract_drift"
INVALID_TRACE="$TMP/invalid.trace"
: > "$INVALID_TRACE"
INVALID_STATUS=0
PATH="$STUB_BIN:$PATH" FACTORY_ROOT="$INVALID" \
  FACTORY_GLOBAL_ENV="$INVALID_GLOBAL" FACTORY_TEST_TRACE="$INVALID_TRACE" \
  "$RUN_AGENT" --role planner --ticket T-213 -- "invalid route" >/dev/null 2>&1 ||
  INVALID_STATUS=$?
if [[ "$INVALID_STATUS" -eq 6 && ! -s "$INVALID_TRACE" &&
      ! -f "$INVALID/factory/ledger.csv" ]]; then
  pass "invalid primary fails closed before task submission"
else
  fail "invalid primary fails closed before task submission" "status $INVALID_STATUS"
fi

UNKNOWN="$TMP/unknown-route"
write_envelope "$UNKNOWN"
UNKNOWN_GLOBAL="$TMP/unknown-global/global.env"
write_backend_global "$UNKNOWN_GLOBAL" \
  "export FACTORY_PROBE_CODEX=UNKNOWN:test_unclassified"
UNKNOWN_TRACE="$TMP/unknown.trace"
: > "$UNKNOWN_TRACE"
UNKNOWN_STATUS=0
PATH="$STUB_BIN:$PATH" FACTORY_ROOT="$UNKNOWN" \
  FACTORY_GLOBAL_ENV="$UNKNOWN_GLOBAL" FACTORY_TEST_TRACE="$UNKNOWN_TRACE" \
  "$RUN_AGENT" --role planner --ticket T-216 -- "unknown route" >/dev/null 2>&1 ||
  UNKNOWN_STATUS=$?
if [[ "$UNKNOWN_STATUS" -eq 6 && ! -s "$UNKNOWN_TRACE" ]]; then
  pass "unknown primary state fails before task submission"
else
  fail "unknown primary state fails before task submission" "status $UNKNOWN_STATUS"
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
MOCK_SLEEP=5 FACTORY_ROOT="$GUARD" FACTORY_LEDGER="$GUARD_LEDGER" \
  FACTORY_GLOBAL_ENV="$TMP/no-global.env" FACTORY_TEST_MODE=1 \
  FACTORY_ADAPTER_OVERRIDE=mock \
  "$RUN_AGENT" --role builder --ticket T-400 -- "slow run" > "$TMP/first.out" 2>&1 &
FIRST_PID=$!
for _i in $(seq 1 50); do
  [[ -n "$(ls "$GUARD/factory/.active-runs/"*.pid 2>/dev/null || true)" ]] && break
  sleep 0.05
done
SECOND_OUTPUT="$(FACTORY_ROOT="$GUARD" FACTORY_LEDGER="$GUARD_LEDGER" \
  FACTORY_GLOBAL_ENV="$TMP/no-global.env" FACTORY_TEST_MODE=1 \
  FACTORY_ADAPTER_OVERRIDE=mock \
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

# Kill switch terminates the isolated adapter process group and descendants.
KILL_ROOT="$TMP/kill-root"
write_envelope "$KILL_ROOT"
DESCENDANT_PID_FILE="$TMP/mock-descendant.pid"
MOCK_SLEEP=30 MOCK_DESCENDANT_PID_FILE="$DESCENDANT_PID_FILE" \
  FACTORY_ROOT="$KILL_ROOT" FACTORY_GLOBAL_ENV="$TMP/no-global.env" \
  FACTORY_TEST_MODE=1 \
  FACTORY_ADAPTER_OVERRIDE=mock \
  "$RUN_AGENT" --role builder --ticket T-401 -- "kill group" \
  > "$TMP/kill-wrapper.out" 2>&1 &
KILL_WRAPPER_PID=$!
KILL_PID_FILE=""
for _i in $(seq 1 100); do
  KILL_PID_FILE="$(ls "$KILL_ROOT/factory/runs/"*.pid 2>/dev/null || true)"
  [[ -n "$KILL_PID_FILE" && -f "$DESCENDANT_PID_FILE" ]] && break
  sleep 0.05
done
KILL_PGID="$(sed -n 's/^pgid=//p' "$KILL_PID_FILE" 2>/dev/null | awk 'NR==1 {print; exit}')"
FACTORY_SKIP_SCHEDULE_STOP=1 "$KILL_SWITCH" "$KILL_ROOT" >/dev/null
wait "$KILL_WRAPPER_PID" 2>/dev/null || true
NEW_AFTER_KILL_STATUS=0
FACTORY_ROOT="$KILL_ROOT" FACTORY_GLOBAL_ENV="$TMP/no-global.env" \
  FACTORY_TEST_MODE=1 \
  FACTORY_ADAPTER_OVERRIDE=mock \
  "$RUN_AGENT" --role builder --ticket T-402 -- "must refuse" >/dev/null 2>&1 ||
  NEW_AFTER_KILL_STATUS=$?
if [[ "$KILL_PGID" =~ ^[0-9]+$ ]] &&
   ! kill -0 -- "-$KILL_PGID" 2>/dev/null &&
   [[ "$NEW_AFTER_KILL_STATUS" -eq 4 ]]; then
  pass "kill switch terminates process group and blocks new runs"
else
  fail "kill switch terminates process group and blocks new runs"
fi

# KILL creation cannot scan before an in-flight launch publishes its PID.
RACE_ROOT="$TMP/kill-race"
write_envelope "$RACE_ROOT"
RACE_DESCENDANT="$TMP/race-descendant.pid"
MOCK_SLEEP=30 MOCK_DESCENDANT_PID_FILE="$RACE_DESCENDANT" \
  FACTORY_ROOT="$RACE_ROOT" FACTORY_GLOBAL_ENV="$TMP/no-global.env" \
  FACTORY_TEST_MODE=1 FACTORY_TEST_BEFORE_REGISTER_SLEEP=1 \
  FACTORY_ADAPTER_OVERRIDE=mock \
  "$RUN_AGENT" --role builder --ticket T-403 -- "serialized kill" \
  > "$TMP/race-wrapper.out" 2>&1 &
RACE_WRAPPER_PID=$!
sleep 0.1
FACTORY_SKIP_SCHEDULE_STOP=1 "$KILL_SWITCH" "$RACE_ROOT" >/dev/null
wait "$RACE_WRAPPER_PID" 2>/dev/null || true
RACE_DESCENDANT_PID="$(awk 'NR==1 {print; exit}' "$RACE_DESCENDANT" 2>/dev/null || true)"
if [[ -f "$RACE_ROOT/factory/KILL" &&
      -z "$(ls "$RACE_ROOT/factory/runs/"*.pid 2>/dev/null || true)" ]] &&
   { [[ -z "$RACE_DESCENDANT_PID" ]] ||
     ! kill -0 "$RACE_DESCENDANT_PID" 2>/dev/null; }; then
  pass "launch lock closes kill-switch PID publication race"
else
  fail "launch lock closes kill-switch PID publication race"
fi

# A stale registration lock cannot prevent KILL publication or PID scanning.
STALE_LOCK_ROOT="$TMP/stale-launch-lock"
mkdir -p "$STALE_LOCK_ROOT/factory/.launch.lock"
FACTORY_SKIP_SCHEDULE_STOP=1 FACTORY_LAUNCH_LOCK_ATTEMPTS=2 \
  "$KILL_SWITCH" "$STALE_LOCK_ROOT" > "$TMP/stale-lock.out" 2>&1
if [[ -f "$STALE_LOCK_ROOT/factory/KILL" ]] &&
   grep -q "launch lock stuck" "$TMP/stale-lock.out"; then
  pass "stale launch lock cannot disable kill switch"
else
  fail "stale launch lock cannot disable kill switch"
fi

# A stale/reused PID identity is retained and never signalled.
STALE_ROOT="$TMP/stale-pid"
mkdir -p "$STALE_ROOT/factory/runs"
python3 -c 'import os,time; os.setsid(); time.sleep(30)' &
STALE_PROC_PID=$!
for _i in $(seq 1 100); do
  STALE_PGID="$(ps -o pgid= -p "$STALE_PROC_PID" 2>/dev/null | tr -d ' ')"
  [[ "$STALE_PGID" == "$STALE_PROC_PID" ]] && break
  sleep 0.01
done
cat > "$STALE_ROOT/factory/runs/stale-test.meta" <<META
run_id=stale-test
phase=spawned
pgid=$STALE_PROC_PID
META
cat > "$STALE_ROOT/factory/runs/stale-test.pid" <<PID
pid=$STALE_PROC_PID
pgid=$STALE_PROC_PID
run_id=stale-test
process_start=definitely-not-the-real-start
PID
FACTORY_SKIP_SCHEDULE_STOP=1 "$KILL_SWITCH" "$STALE_ROOT" \
  > "$TMP/stale-kill.out" 2>&1
if kill -0 "$STALE_PROC_PID" 2>/dev/null &&
   [[ -f "$STALE_ROOT/factory/runs/stale-test.pid" ]] &&
   grep -q "refusing stale or mismatched" "$TMP/stale-kill.out"; then
  pass "kill switch refuses stale PID identity"
else
  fail "kill switch refuses stale PID identity"
fi
kill -TERM -- "-$STALE_PROC_PID" 2>/dev/null || true
wait "$STALE_PROC_PID" 2>/dev/null || true

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
printf 'Operator-Approval: Linear\n' >> "$WALK/factory/tickets/T-500.md"
expect_stage "AWAIT-MERGE" "$WALK" T-500 || WALK_OK=0
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

# ---------------------------------------------------------------------------
# T-106: operator backend-readiness diagnostic (contract-test.sh --readiness),
# frozen command contract version 1. These checks fail before the builder adds
# the --readiness mode — the flag is unrecognized, so the command prints the
# old usage line and exits 2 with no adapter/route records — and pass once the
# contract is implemented. Existing checks above are untouched.
CONTRACT_TEST="$ROOT/scripts/adapters/contract-test.sh"
READINESS_DIR="$TMP/readiness"
mkdir -p "$READINESS_DIR"
READINESS_SCAN="$READINESS_DIR/scan.out"   # accumulated Fixtures A-E output (AC8)
: > "$READINESS_SCAN"
READINESS_DIRTY=""                          # fixtures that left a task trace (AC6)

# Run --readiness under a global env built from write_backend_global "$extra".
# Empties FACTORY_TEST_TRACE first, captures combined output to $out, records
# READINESS_STATUS, and flags any fixture whose trace ended up non-empty.
run_readiness() {
  local name="$1" extra="$2" out="$3"
  local gfile="$READINESS_DIR/$name.env" trace="$READINESS_DIR/$name.trace"
  write_backend_global "$gfile" "$extra"
  : > "$trace"
  READINESS_STATUS=0
  PATH="$STUB_BIN:$PATH" FACTORY_GLOBAL_ENV="$gfile" FACTORY_TEST_TRACE="$trace" \
    "$CONTRACT_TEST" --readiness > "$out" 2>&1 || READINESS_STATUS=$?
  [[ -s "$trace" ]] && READINESS_DIRTY="$READINESS_DIRTY $name"
  cat "$out" >> "$READINESS_SCAN"
}

# The contract's six records (four adapters, two routes), in output order.
readiness_records() { grep -E '^\[contract-test\] (adapter|route)=' "$1"; }

# AC1 — Fixture A: four ready adapters and both primary routes; exit 0.
A_OUT="$READINESS_DIR/a.out"
run_readiness fixtureA "" "$A_OUT"
A_EXPECT="$(cat <<'REC'
[contract-test] adapter=codex family=openai state=READY reason=local_contract_ready
[contract-test] adapter=cursor-openai family=openai state=READY reason=local_contract_ready
[contract-test] adapter=claude-code family=anthropic state=READY reason=local_contract_ready
[contract-test] adapter=cursor-anthropic family=anthropic state=READY reason=local_contract_ready
[contract-test] route=production family=openai state=SAFE adapter=codex reason=primary_ready
[contract-test] route=checking family=anthropic state=SAFE adapter=claude-code reason=primary_ready
REC
)"
if [[ "$READINESS_STATUS" -eq 0 && "$(readiness_records "$A_OUT")" == "$A_EXPECT" ]]; then
  pass "AC1 readiness Fixture A: four ready adapters and primary routes exit 0"
else
  fail "AC1 readiness Fixture A: four ready adapters and primary routes exit 0" \
    "status $READINESS_STATUS"
fi

# AC2 — Fixture B: disabled optional fallbacks stay visible as
# UNAVAILABLE/fallback_disabled while ready primaries keep both routes SAFE.
B_OUT="$READINESS_DIR/b.out"
run_readiness fixtureB 'export FACTORY_CURSOR_FALLBACK_ENABLED=0' "$B_OUT"
B_EXPECT="$(cat <<'REC'
[contract-test] adapter=codex family=openai state=READY reason=local_contract_ready
[contract-test] adapter=cursor-openai family=openai state=UNAVAILABLE reason=fallback_disabled
[contract-test] adapter=claude-code family=anthropic state=READY reason=local_contract_ready
[contract-test] adapter=cursor-anthropic family=anthropic state=UNAVAILABLE reason=fallback_disabled
[contract-test] route=production family=openai state=SAFE adapter=codex reason=primary_ready
[contract-test] route=checking family=anthropic state=SAFE adapter=claude-code reason=primary_ready
REC
)"
if [[ "$READINESS_STATUS" -eq 0 && "$(readiness_records "$B_OUT")" == "$B_EXPECT" ]]; then
  pass "AC2 readiness Fixture B: disabled fallback visible, primary routes safe"
else
  fail "AC2 readiness Fixture B: disabled fallback visible, primary routes safe" \
    "status $READINESS_STATUS"
fi

# AC3 — Fixture C: unavailable primaries select the ready startup fallback in
# each family with reason primary_test_primary_down; exit 0.
C_OUT="$READINESS_DIR/c.out"
run_readiness fixtureC \
  $'export FACTORY_PROBE_CODEX=UNAVAILABLE:test_primary_down\nexport FACTORY_PROBE_CLAUDE_CODE=UNAVAILABLE:test_primary_down' \
  "$C_OUT"
C_EXPECT="$(cat <<'REC'
[contract-test] adapter=codex family=openai state=UNAVAILABLE reason=test_primary_down
[contract-test] adapter=cursor-openai family=openai state=READY reason=local_contract_ready
[contract-test] adapter=claude-code family=anthropic state=UNAVAILABLE reason=test_primary_down
[contract-test] adapter=cursor-anthropic family=anthropic state=READY reason=local_contract_ready
[contract-test] route=production family=openai state=SAFE adapter=cursor-openai reason=primary_test_primary_down
[contract-test] route=checking family=anthropic state=SAFE adapter=cursor-anthropic reason=primary_test_primary_down
REC
)"
if [[ "$READINESS_STATUS" -eq 0 && "$(readiness_records "$C_OUT")" == "$C_EXPECT" ]]; then
  pass "AC3 readiness Fixture C: unavailable primaries select startup fallbacks"
else
  fail "AC3 readiness Fixture C: unavailable primaries select startup fallbacks" \
    "status $READINESS_STATUS"
fi

# AC4 — Fixture D: unavailable production primary with disabled fallback leaves
# production with no route (exit 1) while checking stays SAFE.
D_OUT="$READINESS_DIR/d.out"
run_readiness fixtureD \
  $'export FACTORY_CURSOR_FALLBACK_ENABLED=0\nexport FACTORY_PROBE_CODEX=UNAVAILABLE:test_primary_down' \
  "$D_OUT"
D_EXPECT="$(cat <<'REC'
[contract-test] adapter=codex family=openai state=UNAVAILABLE reason=test_primary_down
[contract-test] adapter=cursor-openai family=openai state=UNAVAILABLE reason=fallback_disabled
[contract-test] adapter=claude-code family=anthropic state=READY reason=local_contract_ready
[contract-test] adapter=cursor-anthropic family=anthropic state=UNAVAILABLE reason=fallback_disabled
[contract-test] route=production family=openai state=UNSAFE adapter=none reason=no_ready_route_primary_test_primary_down_fallback_fallback_disabled
[contract-test] route=checking family=anthropic state=SAFE adapter=claude-code reason=primary_ready
REC
)"
if [[ "$READINESS_STATUS" -eq 1 && "$(readiness_records "$D_OUT")" == "$D_EXPECT" ]]; then
  pass "AC4 readiness Fixture D: no production route exits 1, checking stays safe"
else
  fail "AC4 readiness Fixture D: no production route exits 1, checking stays safe" \
    "status $READINESS_STATUS"
fi

# AC5 — Fixture E: an INVALID production primary fails closed (exit 1) and never
# selects the ready fallback.
E_OUT="$READINESS_DIR/e.out"
run_readiness fixtureE 'export FACTORY_PROBE_CODEX=INVALID:test_contract_drift' "$E_OUT"
E_EXPECT="$(cat <<'REC'
[contract-test] adapter=codex family=openai state=INVALID reason=test_contract_drift
[contract-test] adapter=cursor-openai family=openai state=READY reason=local_contract_ready
[contract-test] adapter=claude-code family=anthropic state=READY reason=local_contract_ready
[contract-test] adapter=cursor-anthropic family=anthropic state=READY reason=local_contract_ready
[contract-test] route=production family=openai state=UNSAFE adapter=none reason=primary_test_contract_drift
[contract-test] route=checking family=anthropic state=SAFE adapter=claude-code reason=primary_ready
REC
)"
if [[ "$READINESS_STATUS" -eq 1 && "$(readiness_records "$E_OUT")" == "$E_EXPECT" ]]; then
  pass "AC5 readiness Fixture E: invalid primary fails closed without fallback"
else
  fail "AC5 readiness Fixture E: invalid primary fails closed without fallback" \
    "status $READINESS_STATUS"
fi

# AC6 — every fixture above ran the readiness command (route records present)
# yet left its FACTORY_TEST_TRACE empty, proving no task-bearing stub branch ran.
if [[ -z "$READINESS_DIRTY" ]] &&
   grep -q '^\[contract-test\] route=production ' "$READINESS_SCAN" &&
   grep -q '^\[contract-test\] route=checking ' "$READINESS_SCAN"; then
  pass "AC6 readiness fixtures submit no task (trace stays empty)"
else
  fail "AC6 readiness fixtures submit no task (trace stays empty)" \
    "dirty:${READINESS_DIRTY:-none}"
fi

# AC8 — a positional task is a usage error (exit 2, exact usage line, empty
# trace); Fixtures A-E output leaks no model ID, version, task text, or secret.
SENTINEL_OUT="$READINESS_DIR/sentinel.out"
SENTINEL_TRACE="$READINESS_DIR/sentinel.trace"
SENTINEL_GLOBAL="$READINESS_DIR/sentinel.env"
write_backend_global "$SENTINEL_GLOBAL"
: > "$SENTINEL_TRACE"
SENTINEL_STATUS=0
PATH="$STUB_BIN:$PATH" FACTORY_GLOBAL_ENV="$SENTINEL_GLOBAL" \
  FACTORY_TEST_TRACE="$SENTINEL_TRACE" \
  "$CONTRACT_TEST" --readiness sentinel-task > "$SENTINEL_OUT" 2>&1 || SENTINEL_STATUS=$?
READINESS_USAGE='usage: contract-test.sh [--adapters a,b | --routes | --readiness]'
if [[ "$SENTINEL_STATUS" -eq 2 ]] &&
   grep -Fxq "$READINESS_USAGE" "$SENTINEL_OUT" &&
   [[ ! -s "$SENTINEL_TRACE" && -s "$READINESS_SCAN" ]] &&
   ! grep -qE 'gpt-5\.6-sol-high|claude-sonnet-5-thinking-high|0\.144\.1|2\.1\.207|2026\.07\.test|sentinel-task|supersecret|abc123|user:pass' \
     "$READINESS_SCAN"; then
  pass "AC8 readiness rejects task text (exit 2) and excludes model/version/secret output"
else
  fail "AC8 readiness rejects task text (exit 2) and excludes model/version/secret output" \
    "status $SENTINEL_STATUS"
fi

if [[ "$FAILURES" -gt 0 ]]; then
  echo "FAIL: $FAILURES factory-script test(s) failed" >&2
  exit 1
fi
echo "PASS: all factory-script tests"
