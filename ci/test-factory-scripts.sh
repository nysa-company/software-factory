#!/usr/bin/env bash
# Self-contained regression tests for run-agent.sh and next-stage.sh.
set -u
export FACTORY_TEST_MODE=1
export FACTORY_TRUSTED_TEST_HARNESS=1

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_AGENT="$ROOT/scripts/run-agent.sh"
NEXT_STAGE="$ROOT/scripts/next-stage.sh"
KILL_SWITCH="$ROOT/scripts/kill-switch.sh"
KIT_SHA="$(git -C "$ROOT" rev-parse HEAD)"
KIT_TREE="$(git -C "$ROOT" rev-parse 'HEAD^{tree}')"
PHYSICAL_KIT_PATH="$(cd "$ROOT" && pwd -P)"
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
  local git_mode="${2:-git}"
  mkdir -p "$1/factory/tickets"
  printf '%s\n' \
    'PER_RUN_BUDGET_USD=1.00' \
    'PER_TICKET_BUDGET_USD=20.00' \
    'PER_RUN_MAX_TURNS=5' \
    'PER_RUN_TIMEOUT_MIN=1' \
    'DAILY_CAP_USD=50.00' > "$1/factory/ENVELOPE.env"
  echo "factory/runtime-ledger.csv" > "$1/.gitignore"
  printf '%s\n' "$KIT_SHA" > "$1/factory/KIT_PIN"
  [[ "$git_mode" == "no-git" ]] || init_product_git "$1"
}

write_ticket() {
  local root="$1" ticket="$2" state="${3:-Ready}"
  mkdir -p "$root/factory/tickets"
  [[ -f "$root/factory/tickets/$ticket.md" ]] ||
    printf '# %s\n\nState: %s\n' "$ticket" "$state" > "$root/factory/tickets/$ticket.md"
}

init_product_git() {
  local root="$1"
  git -C "$root" init -q
  git -C "$root" add factory
  GIT_AUTHOR_NAME=test GIT_AUTHOR_EMAIL=test@example.com \
  GIT_COMMITTER_NAME=test GIT_COMMITTER_EMAIL=test@example.com \
    git -C "$root" commit -qm "fixture"
}

build_sealed_release() {
  local dir="$1"
  mkdir -p "$dir/integrations/hermes"
  cp -R "$ROOT/scripts" "$dir/"
  cp "$ROOT/integrations/hermes/contract.json" \
    "$dir/integrations/hermes/contract.json"
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
ln -s "$ROOT" "$TMP/kit-link"
LINKED_RUN_AGENT="$TMP/kit-link/scripts/run-agent.sh"

IMPLICIT_PIN="$(bash -c '
  source "$1"
  factory_validate_kit_pin "$2" "$2/conformance"
  printf "%s:%s\n" "$FACTORY_KIT_PIN_IMPLICIT" "$FACTORY_KIT_SHA"
' _ "$ROOT/scripts/lib/kit-pin.sh" "$ROOT")"
ROOT_PIN_ERROR="$(bash -c '
  source "$1"
  factory_validate_kit_pin "$2" "$2" >/dev/null 2>&1 || true
  printf "%s\n" "$FACTORY_KIT_PIN_ERROR"
' _ "$ROOT/scripts/lib/kit-pin.sh" "$ROOT")"
if [[ "$IMPLICIT_PIN" == "1:$KIT_SHA" &&
      "$ROOT_PIN_ERROR" == "external product requires factory/KIT_PIN" ]]; then
  pass "only in-repo conformance receives implicit kit pin"
else
  fail "only in-repo conformance receives implicit kit pin" \
    "implicit=$IMPLICIT_PIN root=$ROOT_PIN_ERROR"
fi

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
  write_ticket "$1" "$3"
  FACTORY_ROOT="$1" \
  FACTORY_GLOBAL_ENV="$TMP/no-global.env" \
  FACTORY_TEST_MODE=1 \
  FACTORY_ADAPTER_OVERRIDE=mock \
    "$RUN_AGENT" --role "$2" --ticket "$3" -- "test task"
}

ledger_header() {
  printf '%s\n' 'date,time,ticket,role,adapter,prompt_version,turns,cost_usd,exit_status,run_id,provider_family,model_id,selection_reason,cost_basis,adapter_version'
}

ledger_row() {
  printf '2026-07-13,06:00:00,%s,%s,mock,test,1,0.10,0,,,,,,\n' "$1" "$2"
}

expect_stage() {
  local expected="$1" root="$2" ticket="$3" actual status
  mkdir -p "$root/factory"
  [[ -f "$root/factory/KIT_PIN" ]] ||
    printf '%s\n' "$KIT_SHA" > "$root/factory/KIT_PIN"
  if ! git -C "$root" rev-parse --git-dir >/dev/null 2>&1; then
    init_product_git "$root"
  fi
  actual="$(FACTORY_ROOT="$root" FACTORY_LEDGER="$root/factory/ledger.csv" "$NEXT_STAGE" --ticket "$ticket" 2>&1)"
  status=$?
  [[ "$actual" == "$expected"* ]] || {
    fail "$ticket expected '$expected'" "got '$actual' (status $status)"
    return 1
  }
  return 0
}

# Real sequencer and runner execute from a physical no-.git release when the
# trusted launcher supplies a complete, self-consistent provenance tuple.
SEALED_RELEASE="$TMP/sealed-release"
build_sealed_release "$SEALED_RELEASE"
SEALED_RELEASE="$(cd "$SEALED_RELEASE" && pwd -P)"
SEALED_TREE="$(bash -c '
  source "$1"
  factory_directory_tree "$2"
' _ "$ROOT/scripts/lib/kit-pin.sh" "$SEALED_RELEASE")"
SEALED_PRODUCT="$TMP/sealed-product"
write_envelope "$SEALED_PRODUCT"
write_ticket "$SEALED_PRODUCT" T-190
SEALED_STAGE="$(env \
  FACTORY_ROOT="$SEALED_PRODUCT" \
  FACTORY_RELEASE_SHA="$KIT_SHA" \
  FACTORY_RELEASE_TREE="$SEALED_TREE" \
  FACTORY_RELEASE_PATH="$SEALED_RELEASE" \
  FACTORY_RELEASE_CONTRACT_VERSION=1.2.0 \
  "$SEALED_RELEASE/scripts/next-stage.sh" --ticket T-190 2>&1)"
SEALED_RUN_STATUS=0
env \
  FACTORY_ROOT="$SEALED_PRODUCT" \
  FACTORY_GLOBAL_ENV="$TMP/no-global.env" \
  FACTORY_TEST_MODE=1 \
  FACTORY_ADAPTER_OVERRIDE=mock \
  FACTORY_RELEASE_SHA="$KIT_SHA" \
  FACTORY_RELEASE_TREE="$SEALED_TREE" \
  FACTORY_RELEASE_PATH="$SEALED_RELEASE" \
  FACTORY_RELEASE_CONTRACT_VERSION=1.2.0 \
  "$SEALED_RELEASE/scripts/run-agent.sh" \
    --role planner --ticket T-190 -- "sealed run" >/dev/null 2>&1 ||
  SEALED_RUN_STATUS=$?
SEALED_AFTER="$(env \
  FACTORY_ROOT="$SEALED_PRODUCT" \
  FACTORY_RELEASE_SHA="$KIT_SHA" \
  FACTORY_RELEASE_TREE="$SEALED_TREE" \
  FACTORY_RELEASE_PATH="$SEALED_RELEASE" \
  FACTORY_RELEASE_CONTRACT_VERSION=1.2.0 \
  "$SEALED_RELEASE/scripts/next-stage.sh" --ticket T-190 2>&1)"
SEALED_META="$(ls "$SEALED_PRODUCT/factory/runs/"*.meta 2>/dev/null || true)"
if [[ "$SEALED_STAGE" == "RUN planner" &&
      "$SEALED_RUN_STATUS" -eq 0 &&
      "$SEALED_AFTER" == "RUN spec-linter" &&
      -n "$SEALED_META" &&
      ! -e "$SEALED_RELEASE/.git" ]] &&
   grep -q "^kit_sha=$KIT_SHA$" "$SEALED_META" &&
   grep -q "^kit_tree=$SEALED_TREE$" "$SEALED_META" &&
   grep -q "^ticket_kit_sha=$KIT_SHA$" "$SEALED_META" &&
   grep -q '^contract_version=1.2.0$' "$SEALED_META" &&
   grep -q "^physical_kit_path=$SEALED_RELEASE$" "$SEALED_META" &&
   grep -q '^kit_provenance_mode=sealed$' "$SEALED_META" &&
   grep -q "^Kit-SHA: $KIT_SHA$" "$SEALED_PRODUCT/factory/tickets/T-190.md"; then
  pass "sealed release runs real sequencer and mock agent"
else
  fail "sealed release runs real sequencer and mock agent" \
    "before=$SEALED_STAGE run=$SEALED_RUN_STATUS after=$SEALED_AFTER"
fi

FORGED_STAGE_STATUS=0
FORGED_STAGE="$(env \
  FACTORY_ROOT="$SEALED_PRODUCT" \
  FACTORY_RELEASE_SHA="$KIT_SHA" \
  FACTORY_RELEASE_TREE="$SEALED_TREE" \
  FACTORY_RELEASE_PATH="$TMP" \
  FACTORY_RELEASE_CONTRACT_VERSION=1.2.0 \
  "$SEALED_RELEASE/scripts/next-stage.sh" --ticket T-190 2>&1)" ||
  FORGED_STAGE_STATUS=$?
if [[ "$FORGED_STAGE_STATUS" -eq 1 &&
      "$FORGED_STAGE" == "REFUSE physical kit path does not match trusted release path" ]]; then
  pass "sealed sequencer rejects forged release path"
else
  fail "sealed sequencer rejects forged release path" \
    "status=$FORGED_STAGE_STATUS output=$FORGED_STAGE"
fi

BAD_NEXT_STATUS=0
BAD_NEXT="$(env FACTORY_ROOT="$TMP/does-not-exist" FACTORY_RELEASE_SHA=partial \
  "$NEXT_STAGE" --ticket '../T-190' 2>&1)" || BAD_NEXT_STATUS=$?
BAD_RUN_STATUS=0
BAD_RUN="$(env FACTORY_ROOT="$TMP/does-not-exist" FACTORY_RELEASE_SHA=partial \
  "$RUN_AGENT" --role planner --ticket '../T-190' -- "invalid" 2>&1)" ||
  BAD_RUN_STATUS=$?
if [[ "$BAD_NEXT_STATUS" -eq 2 && "$BAD_NEXT" == "invalid ticket identifier" &&
      "$BAD_RUN_STATUS" -eq 2 && "$BAD_RUN" == "invalid ticket identifier" ]]; then
  pass "sequencer and runner reject malformed tickets before file access"
else
  fail "sequencer and runner reject malformed tickets before file access" \
    "next=$BAD_NEXT_STATUS/$BAD_NEXT run=$BAD_RUN_STATUS/$BAD_RUN"
fi

# Canonical ledger routing from a linked worktree.
MAIN="$TMP/main"
WT="$TMP/worktree"
mkdir -p "$MAIN/conformance"
write_envelope "$MAIN/conformance" no-git
git -C "$MAIN" init -q
git -C "$MAIN" add conformance/factory/ENVELOPE.env conformance/factory/KIT_PIN
GIT_AUTHOR_NAME=test GIT_AUTHOR_EMAIL=test@example.com \
GIT_COMMITTER_NAME=test GIT_COMMITTER_EMAIL=test@example.com \
  git -C "$MAIN" commit -qm "fixture"
git -C "$MAIN" worktree add -q -b ticket-worktree "$WT"

if run_mock "$WT/conformance" planner T-200 >/dev/null &&
   [[ "$(awk -F, '$3=="T-200" {n++} END {print n+0}' "$MAIN/conformance/factory/runtime-ledger.csv")" == "1" ]] &&
   [[ ! -f "$WT/conformance/factory/runtime-ledger.csv" ]]; then
  pass "linked worktree writes canonical main runtime ledger"
else
  fail "linked worktree writes canonical main runtime ledger"
fi

# Main-clone subdirectory root: canonical ledger must resolve to itself
# (regression: relative --git-common-dir was resolved against the wrong base,
# producing a nonexistent path and an empty LEDGER).
if run_mock "$MAIN/conformance" planner T-202 >/dev/null 2>"$TMP/mainclone.err" &&
   [[ "$(awk -F, '$3=="T-202" {n++} END {print n+0}' "$MAIN/conformance/factory/runtime-ledger.csv")" == "1" ]] &&
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
write_ticket "$WT/conformance" T-201
if FACTORY_ROOT="$WT/conformance" FACTORY_LEDGER="$OVERRIDE" \
     FACTORY_GLOBAL_ENV="$TMP/no-global.env" FACTORY_TEST_MODE=1 \
     FACTORY_ADAPTER_OVERRIDE=mock \
     "$RUN_AGENT" --role planner --ticket T-201 -- "override" >/dev/null &&
   [[ "$(awk -F, '$3=="T-201" {n++} END {print n+0}' "$OVERRIDE")" == "1" ]] &&
   [[ "$(awk -F, '$3=="T-201" {n++} END {print n+0}' "$MAIN/conformance/factory/runtime-ledger.csv")" == "0" ]]; then
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
   [[ "$(awk 'NR==1 {print; exit}' "$PARTIAL/factory/runtime-ledger.csv")" == \
      "date,time,ticket,role,adapter,prompt_version,turns,cost_usd,exit_status,run_id,provider_family,model_id,selection_reason,cost_basis,adapter_version" ]] &&
   [[ "$(awk -F, '$3=="T-OLD" {print $9}' "$PARTIAL/factory/runtime-ledger.csv")" == "0" ]]; then
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
write_ticket "$MOCK_GUARD" T-204
MOCK_GUARD_STATUS=0
env -u FACTORY_TEST_MODE -u FACTORY_TRUSTED_TEST_HARNESS \
  FACTORY_ROOT="$MOCK_GUARD" FACTORY_GLOBAL_ENV="$TMP/no-global.env" \
  FACTORY_ADAPTER_OVERRIDE=mock \
  "$RUN_AGENT" --role planner --ticket T-204 -- "forbidden mock" >/dev/null 2>&1 ||
  MOCK_GUARD_STATUS=$?
if [[ "$MOCK_GUARD_STATUS" -eq 2 && ! -f "$MOCK_GUARD/factory/ledger.csv" ]]; then
  pass "mock override requires trusted test harness"
else
  fail "mock override requires trusted test harness" "status $MOCK_GUARD_STATUS"
fi

PROBE_GUARD_STATUS=0
env -u FACTORY_TRUSTED_TEST_HARNESS \
  FACTORY_TEST_MODE=1 FACTORY_PROBE_CODEX=UNAVAILABLE:test \
  FACTORY_ROOT="$MOCK_GUARD" FACTORY_GLOBAL_ENV="$TMP/no-global.env" \
  "$RUN_AGENT" --role planner --ticket T-204 -- "forbidden probe" >/dev/null 2>&1 ||
  PROBE_GUARD_STATUS=$?
if [[ "$PROBE_GUARD_STATUS" -eq 2 && ! -f "$MOCK_GUARD/factory/ledger.csv" ]]; then
  pass "probe override requires trusted test harness"
else
  fail "probe override requires trusted test harness" "status $PROBE_GUARD_STATUS"
fi

NEXT_OVERRIDE_STATUS=0
NEXT_OVERRIDE="$(env -u FACTORY_TRUSTED_TEST_HARNESS \
  FACTORY_TEST_MODE=1 FACTORY_PROBE_CODEX=UNAVAILABLE:test \
  FACTORY_ROOT="$MOCK_GUARD" "$NEXT_STAGE" --ticket T-204 2>&1)" ||
  NEXT_OVERRIDE_STATUS=$?
if [[ "$NEXT_OVERRIDE_STATUS" -eq 1 &&
      "$NEXT_OVERRIDE" == "REFUSE "*"trusted internal test harness" ]]; then
  pass "sequencer rejects untrusted probe overrides"
else
  fail "sequencer rejects untrusted probe overrides" \
    "status $NEXT_OVERRIDE_STATUS: $NEXT_OVERRIDE"
fi

# Backend resolution: primary success submits exactly one primary task.
PRIMARY="$TMP/primary-route"
write_envelope "$PRIMARY"
write_ticket "$PRIMARY" T-210
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
   [[ "$(awk -F, '$3=="T-210" {print $5}' "$PRIMARY/factory/runtime-ledger.csv")" == "codex" ]] &&
   [[ "$(awk -F, '$3=="T-210" {print $11}' "$PRIMARY/factory/runtime-ledger.csv")" == "openai" ]] &&
   [[ "$(awk -F, '$3=="T-210" {print $13}' "$PRIMARY/factory/runtime-ledger.csv")" == "primary_ready" ]] &&
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
write_ticket "$FALLBACK" T-211
FALLBACK_PRODUCT_TREE="$(git -C "$FALLBACK" rev-parse 'HEAD^{tree}')"
FALLBACK_GLOBAL="$TMP/fallback-global/global.env"
write_backend_global "$FALLBACK_GLOBAL" \
  "export FACTORY_PROBE_CODEX=UNAVAILABLE:test_primary_down"
FALLBACK_TRACE="$TMP/fallback.trace"
: > "$FALLBACK_TRACE"
if PATH="$STUB_BIN:$PATH" FACTORY_ROOT="$FALLBACK" \
     FACTORY_GLOBAL_ENV="$FALLBACK_GLOBAL" FACTORY_TEST_TRACE="$FALLBACK_TRACE" \
     "$LINKED_RUN_AGENT" --role planner --ticket T-211 -- "fallback route" >/dev/null &&
   [[ "$(awk -F, '$3=="T-211" {print $5}' "$FALLBACK/factory/runtime-ledger.csv")" == "cursor-openai" ]] &&
   [[ "$(awk -F, '$3=="T-211" {print $12}' "$FALLBACK/factory/runtime-ledger.csv")" == "gpt-5.6-sol-high" ]] &&
   [[ "$(awk -F, '$3=="T-211" {print $14}' "$FALLBACK/factory/runtime-ledger.csv")" == "conservative_reservation" ]] &&
   [[ "$(awk -F, '$3=="T-211" {print $8}' "$FALLBACK/factory/runtime-ledger.csv")" == "1.00" ]] &&
   [[ "$(wc -l < "$FALLBACK_TRACE" | tr -d ' ')" == "1" ]] &&
   grep -q '^cursor-task$' "$FALLBACK_TRACE"; then
  FALLBACK_OUT="$(ls "$FALLBACK/factory/runs/"*.out)"
  FALLBACK_META="$(ls "$FALLBACK/factory/runs/"*.meta)"
  if ! grep -qE 'supersecret|abc123|user:pass' "$FALLBACK_OUT" &&
     grep -q '\[REDACTED\]' "$FALLBACK_OUT" &&
     grep -q 'input_tokens=70' "$FALLBACK_OUT" &&
     grep -q 'cache_tokens=15' "$FALLBACK_OUT" &&
     grep -q '^phase=completed$' "$FALLBACK_META" &&
     grep -q "^kit_sha=$KIT_SHA$" "$FALLBACK_META" &&
     grep -q "^kit_tree=$KIT_TREE$" "$FALLBACK_META" &&
     grep -q "^product_tree=$FALLBACK_PRODUCT_TREE$" "$FALLBACK_META" &&
     grep -q "^ticket_kit_sha=$KIT_SHA$" "$FALLBACK_META" &&
     grep -q '^contract_version=1.2.0$' "$FALLBACK_META" &&
     grep -q "^physical_kit_path=$PHYSICAL_KIT_PATH$" "$FALLBACK_META"; then
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
write_ticket "$ANTHROPIC_FALLBACK" T-214
{
  ledger_header
  ledger_row T-214 planner
} > "$ANTHROPIC_FALLBACK/factory/ledger.csv"
ANTHROPIC_GLOBAL="$TMP/anthropic-global/global.env"
write_backend_global "$ANTHROPIC_GLOBAL" \
  "export FACTORY_PROBE_CLAUDE_CODE=UNAVAILABLE:test_primary_down"
ANTHROPIC_TRACE="$TMP/anthropic.trace"
: > "$ANTHROPIC_TRACE"
if PATH="$STUB_BIN:$PATH" FACTORY_ROOT="$ANTHROPIC_FALLBACK" \
     FACTORY_GLOBAL_ENV="$ANTHROPIC_GLOBAL" FACTORY_TEST_TRACE="$ANTHROPIC_TRACE" \
     "$RUN_AGENT" --role spec-linter --ticket T-214 -- "checking fallback" >/dev/null &&
   [[ "$(awk -F, '$3=="T-214" && $4=="spec-linter" {print $5}' "$ANTHROPIC_FALLBACK/factory/runtime-ledger.csv")" == "cursor-anthropic" ]] &&
   [[ "$(awk -F, '$3=="T-214" && $4=="spec-linter" {print $11}' "$ANTHROPIC_FALLBACK/factory/runtime-ledger.csv")" == "anthropic" ]] &&
   [[ "$(awk -F, '$3=="T-214" && $4=="spec-linter" {print $12}' "$ANTHROPIC_FALLBACK/factory/runtime-ledger.csv")" == "claude-sonnet-5-thinking-high" ]] &&
   [[ "$(wc -l < "$ANTHROPIC_TRACE" | tr -d ' ')" == "1" ]]; then
  pass "checking fallback preserves Anthropic family"
else
  fail "checking fallback preserves Anthropic family"
fi

# Malformed Cursor output is a terminal failed run, never another fallback.
MALFORMED="$TMP/malformed-cursor"
write_envelope "$MALFORMED"
write_ticket "$MALFORMED" T-215
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
   [[ "$(awk -F, '$3=="T-215" {print $9}' "$MALFORMED/factory/runtime-ledger.csv")" == "9" ]]; then
  pass "malformed Cursor output fails without another task"
else
  fail "malformed Cursor output fails without another task" "status $MALFORMED_STATUS"
fi

# A reported opposite-family model fails closed despite successful CLI exit.
MODEL_DRIFT="$TMP/model-drift"
write_envelope "$MODEL_DRIFT"
write_ticket "$MODEL_DRIFT" T-217
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
      "$(awk -F, '$3=="T-217" {print $9}' "$MODEL_DRIFT/factory/runtime-ledger.csv")" == "9" ]]; then
  pass "reported cross-family Cursor model fails closed"
else
  fail "reported cross-family Cursor model fails closed" "status $MODEL_DRIFT_STATUS"
fi

# Once the primary task starts, its failure never launches Cursor.
TASK_FAIL="$TMP/task-fail"
write_envelope "$TASK_FAIL"
write_ticket "$TASK_FAIL" T-212
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
write_ticket "$INVALID" T-213
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
write_ticket "$UNKNOWN" T-216
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

# The sequencer, not the caller, authorizes the first role.
WRONG_ROLE="$TMP/wrong-initial-role"
write_envelope "$WRONG_ROLE"
write_ticket "$WRONG_ROLE" T-218
WRONG_ROLE_GLOBAL="$TMP/wrong-role-global/global.env"
write_backend_global "$WRONG_ROLE_GLOBAL" \
  "export FACTORY_PROBE_CODEX=INVALID:sequencer_must_run_first"
WRONG_ROLE_STATUS=0
PATH="$STUB_BIN:$PATH" FACTORY_ROOT="$WRONG_ROLE" \
  FACTORY_GLOBAL_ENV="$WRONG_ROLE_GLOBAL" \
  "$RUN_AGENT" --role builder --ticket T-218 -- "wrong role" >/dev/null 2>&1 ||
  WRONG_ROLE_STATUS=$?
if [[ "$WRONG_ROLE_STATUS" -eq 10 &&
      ! -f "$WRONG_ROLE/factory/ledger.csv" &&
      ! -d "$WRONG_ROLE/factory/runs" ]] &&
   ! grep -q '^Kit-SHA:' "$WRONG_ROLE/factory/tickets/T-218.md"; then
  pass "sequencer rejects mismatched initial builder"
else
  fail "sequencer rejects mismatched initial builder" "status $WRONG_ROLE_STATUS"
fi

# The one non-RUN authorization is the exact mechanical FIX action.
FIX_GATE="$TMP/fix-role-gate"
write_envelope "$FIX_GATE"
write_ticket "$FIX_GATE" T-226
printf 'reviewer round 1: REQUEST CHANGES — fix code\n' \
  >> "$FIX_GATE/factory/tickets/T-226.md"
{
  ledger_header
  ledger_row T-226 planner
  ledger_row T-226 test-author
  ledger_row T-226 builder
  ledger_row T-226 reviewer
} > "$FIX_GATE/factory/ledger.csv"
FIX_GATE_STATUS=0
run_mock "$FIX_GATE" builder T-226 >/dev/null 2>&1 || FIX_GATE_STATUS=$?
FIX_GATE_NEXT="$(FACTORY_ROOT="$FIX_GATE" \
  "$NEXT_STAGE" --ticket T-226 2>&1)"
if [[ "$FIX_GATE_STATUS" -eq 0 && "$FIX_GATE_NEXT" == "RUN reviewer" ]]; then
  pass "sequencer permits builder for exact FIX action"
else
  fail "sequencer permits builder for exact FIX action" \
    "status $FIX_GATE_STATUS next=$FIX_GATE_NEXT"
fi

# Per-run strict pins refuse before backend selection or mutable run state.
for PIN_CASE in missing abbreviated mismatch; do
  PIN_ROOT="$TMP/run-pin-$PIN_CASE"
  write_envelope "$PIN_ROOT"
  write_ticket "$PIN_ROOT" T-218
  case "$PIN_CASE" in
    missing) rm "$PIN_ROOT/factory/KIT_PIN" ;;
    abbreviated) printf '%s\n' "${KIT_SHA:0:12}" > "$PIN_ROOT/factory/KIT_PIN" ;;
    mismatch) printf '%s\n' "0000000000000000000000000000000000000000" > "$PIN_ROOT/factory/KIT_PIN" ;;
  esac
  PIN_STATUS=0
  FACTORY_ROOT="$PIN_ROOT" FACTORY_GLOBAL_ENV="$TMP/no-global.env" \
    FACTORY_TEST_MODE=1 FACTORY_ADAPTER_OVERRIDE=mock \
    "$RUN_AGENT" --role planner --ticket T-218 -- "strict pin" >/dev/null 2>&1 ||
    PIN_STATUS=$?
  if [[ "$PIN_STATUS" -eq 3 && ! -f "$PIN_ROOT/factory/ledger.csv" &&
        ! -d "$PIN_ROOT/factory/runs" ]]; then
    pass "run-agent refuses $PIN_CASE kit pin before mutation"
  else
    fail "run-agent refuses $PIN_CASE kit pin before mutation" "status $PIN_STATUS"
  fi
done

# A state transition while launch waits is caught under the launch lock.
STATE_LOCK="$TMP/sequence-after-lock"
write_envelope "$STATE_LOCK"
write_ticket "$STATE_LOCK" T-224
mkdir "$STATE_LOCK/factory/.launch.lock"
FACTORY_ROOT="$STATE_LOCK" FACTORY_GLOBAL_ENV="$TMP/no-global.env" \
  FACTORY_TEST_MODE=1 FACTORY_ADAPTER_OVERRIDE=mock \
  "$RUN_AGENT" --role planner --ticket T-224 -- "state race" \
  > "$TMP/sequence-after-lock.out" 2>&1 &
STATE_LOCK_PID=$!
for _i in $(seq 1 100); do
  [[ -n "$(ls "$STATE_LOCK/factory/runs/"*.meta 2>/dev/null || true)" ]] && break
  sleep 0.02
done
{
  ledger_header
  ledger_row T-224 planner
} > "$STATE_LOCK/factory/ledger.csv"
rmdir "$STATE_LOCK/factory/.launch.lock"
wait "$STATE_LOCK_PID"
STATE_LOCK_STATUS=$?
if [[ "$STATE_LOCK_STATUS" -eq 10 &&
      ! -f "$STATE_LOCK/factory/runs/"*.out ]] &&
   grep -q 'after launch lock acquisition' "$TMP/sequence-after-lock.out" &&
   grep -q '^accounting_schema=1$' "$STATE_LOCK/factory/runs/"*.meta &&
   grep -q '^accounting_state=launch_void$' "$STATE_LOCK/factory/runs/"*.meta &&
   grep -q '^go_issued=0$' "$STATE_LOCK/factory/runs/"*.meta &&
   grep -q '^effective_cost=0$' "$STATE_LOCK/factory/runs/"*.meta &&
   grep -q '^exit_status=10$' "$STATE_LOCK/factory/runs/"*.meta &&
   awk -F, '$3=="T-224" && $8==0 && $9==10 && $14=="launch_void" {found=1} END {exit !found}' \
     "$STATE_LOCK/factory/runtime-ledger.csv"; then
  pass "post-lock sequencer catches state-change race"
else
  fail "post-lock sequencer catches state-change race" \
    "status $STATE_LOCK_STATUS"
fi

# A final sequencer pass closes the prepared-process-to-GO state race.
STATE_GO="$TMP/sequence-before-go"
write_envelope "$STATE_GO"
write_ticket "$STATE_GO" T-225
FACTORY_ROOT="$STATE_GO" FACTORY_GLOBAL_ENV="$TMP/no-global.env" \
  FACTORY_TEST_MODE=1 FACTORY_TEST_BEFORE_GO_SLEEP=1 \
  FACTORY_ADAPTER_OVERRIDE=mock \
  "$RUN_AGENT" --role planner --ticket T-225 -- "pre-GO state race" \
  > "$TMP/sequence-before-go.out" 2>&1 &
STATE_GO_PID=$!
for _i in $(seq 1 100); do
  [[ -n "$(ls "$STATE_GO/factory/runs/".*.ready 2>/dev/null || true)" ]] && break
  sleep 0.02
done
{
  ledger_header
  ledger_row T-225 planner
} > "$STATE_GO/factory/ledger.csv"
wait "$STATE_GO_PID"
STATE_GO_STATUS=$?
if [[ "$STATE_GO_STATUS" -eq 10 ]] &&
   grep -q 'before GO' "$TMP/sequence-before-go.out" &&
   ! grep -q 'mock adapter ran task' "$STATE_GO/factory/runs/"*.out; then
  pass "pre-GO sequencer catches state-change race"
else
  fail "pre-GO sequencer catches state-change race" \
    "status $STATE_GO_STATUS: $(tr '\n' ' ' < "$TMP/sequence-before-go.out")"
fi

# The first role run writes one durable lease, even for a blocked ticket.
LEASE_ROOT="$TMP/ticket-lease"
write_envelope "$LEASE_ROOT"
write_ticket "$LEASE_ROOT" T-219 Blocked-Escalated
if run_mock "$LEASE_ROOT" planner T-219 >/dev/null &&
   [[ "$(grep -c '^Kit-SHA:' "$LEASE_ROOT/factory/tickets/T-219.md")" == "1" ]] &&
   grep -q "^Kit-SHA: $KIT_SHA$" "$LEASE_ROOT/factory/tickets/T-219.md"; then
  pass "first blocked-ticket run records one Kit-SHA lease"
else
  fail "first blocked-ticket run records one Kit-SHA lease"
fi
sed "s/^Kit-SHA: .*$/Kit-SHA: 0000000000000000000000000000000000000000/" \
  "$LEASE_ROOT/factory/tickets/T-219.md" > "$LEASE_ROOT/factory/tickets/T-219.tmp"
mv "$LEASE_ROOT/factory/tickets/T-219.tmp" "$LEASE_ROOT/factory/tickets/T-219.md"
LEASE_STATUS=0
run_mock "$LEASE_ROOT" planner T-219 >/dev/null 2>&1 || LEASE_STATUS=$?
LEASE_STAGE="$(FACTORY_ROOT="$LEASE_ROOT" "$NEXT_STAGE" --ticket T-219 2>&1)"
if [[ "$LEASE_STATUS" -eq 3 &&
      "$LEASE_STAGE" == "REFUSE ticket Kit-SHA lease does not match"* ]] &&
   grep -q '^Kit-SHA: 0000000000000000000000000000000000000000$' \
     "$LEASE_ROOT/factory/tickets/T-219.md" &&
   [[ "$(grep -c '^Kit-SHA:' "$LEASE_ROOT/factory/tickets/T-219.md")" == "1" ]] &&
   [[ "$(awk -F, '$3=="T-219" {n++} END {print n+0}' "$LEASE_ROOT/factory/runtime-ledger.csv")" == "1" ]]; then
  pass "blocked-ticket lease mismatch refuses without overwrite"
else
  fail "blocked-ticket lease mismatch refuses without overwrite" \
    "run status $LEASE_STATUS; stage $LEASE_STAGE"
fi

# Maintenance is a hard gate for both sequencing and initial launch.
MAINT_ROOT="$TMP/maintenance-initial"
write_envelope "$MAINT_ROOT"
write_ticket "$MAINT_ROOT" T-220
touch "$MAINT_ROOT/factory/MAINTENANCE"
MAINT_STATUS=0
FACTORY_ROOT="$MAINT_ROOT" FACTORY_GLOBAL_ENV="$TMP/no-global.env" \
  FACTORY_TEST_MODE=1 FACTORY_ADAPTER_OVERRIDE=mock \
  "$RUN_AGENT" --role planner --ticket T-220 -- "maintenance" >/dev/null 2>&1 ||
  MAINT_STATUS=$?
MAINT_STAGE="$(FACTORY_ROOT="$MAINT_ROOT" "$NEXT_STAGE" --ticket T-220 2>&1)"
if [[ "$MAINT_STATUS" -eq 4 && ! -f "$MAINT_ROOT/factory/ledger.csv" &&
      "$MAINT_STAGE" == "REFUSE MAINTENANCE file present"* ]]; then
  pass "maintenance blocks initial launch and sequencing"
else
  fail "maintenance blocks initial launch and sequencing" \
    "run status $MAINT_STATUS; stage $MAINT_STAGE"
fi

# If maintenance appears while launch waits, the post-lock check wins.
AFTER_LOCK="$TMP/maintenance-after-lock"
write_envelope "$AFTER_LOCK"
write_ticket "$AFTER_LOCK" T-221
mkdir "$AFTER_LOCK/factory/.launch.lock"
FACTORY_ROOT="$AFTER_LOCK" FACTORY_GLOBAL_ENV="$TMP/no-global.env" \
  FACTORY_TEST_MODE=1 FACTORY_ADAPTER_OVERRIDE=mock \
  "$RUN_AGENT" --role planner --ticket T-221 -- "after lock" \
  > "$TMP/after-lock.out" 2>&1 &
AFTER_LOCK_PID=$!
for _i in $(seq 1 100); do
  [[ -n "$(ls "$AFTER_LOCK/factory/runs/"*.meta 2>/dev/null || true)" ]] && break
  sleep 0.02
done
touch "$AFTER_LOCK/factory/MAINTENANCE"
rmdir "$AFTER_LOCK/factory/.launch.lock"
wait "$AFTER_LOCK_PID"
AFTER_LOCK_STATUS=$?
if [[ "$AFTER_LOCK_STATUS" -eq 4 &&
      ! -f "$AFTER_LOCK/factory/ledger.csv" ]] &&
   grep -q 'appeared after launch lock acquisition' "$TMP/after-lock.out"; then
  pass "maintenance publication wins after launch-lock race"
else
  fail "maintenance publication wins after launch-lock race" \
    "status $AFTER_LOCK_STATUS"
fi

# A launch that resolved an old physical release cannot cross a pin activation.
PIN_RACE="$TMP/pin-after-lock"
write_envelope "$PIN_RACE"
write_ticket "$PIN_RACE" T-223
mkdir "$PIN_RACE/factory/.launch.lock"
FACTORY_ROOT="$PIN_RACE" FACTORY_GLOBAL_ENV="$TMP/no-global.env" \
  FACTORY_TEST_MODE=1 FACTORY_ADAPTER_OVERRIDE=mock \
  "$RUN_AGENT" --role planner --ticket T-223 -- "pin race" \
  > "$TMP/pin-race.out" 2>&1 &
PIN_RACE_PID=$!
for _i in $(seq 1 100); do
  [[ -n "$(ls "$PIN_RACE/factory/runs/"*.meta 2>/dev/null || true)" ]] && break
  sleep 0.02
done
printf '%s\n' "0000000000000000000000000000000000000000" \
  > "$PIN_RACE/factory/KIT_PIN"
rmdir "$PIN_RACE/factory/.launch.lock"
wait "$PIN_RACE_PID"
PIN_RACE_STATUS=$?
if [[ "$PIN_RACE_STATUS" -eq 3 && ! -f "$PIN_RACE/factory/ledger.csv" ]] &&
   grep -q 'does not match the selected kit SHA after launch lock acquisition' \
     "$TMP/pin-race.out"; then
  pass "post-lock pin recheck blocks activation-path drift"
else
  fail "post-lock pin recheck blocks activation-path drift" \
    "status $PIN_RACE_STATUS"
fi

# A final maintenance check closes the prepared-process-to-GO race.
BEFORE_GO="$TMP/maintenance-before-go"
write_envelope "$BEFORE_GO"
write_ticket "$BEFORE_GO" T-222
FACTORY_ROOT="$BEFORE_GO" FACTORY_GLOBAL_ENV="$TMP/no-global.env" \
  FACTORY_TEST_MODE=1 FACTORY_TEST_BEFORE_GO_SLEEP=1 \
  FACTORY_ADAPTER_OVERRIDE=mock \
  "$RUN_AGENT" --role planner --ticket T-222 -- "before go" \
  > "$TMP/before-go.out" 2>&1 &
BEFORE_GO_PID=$!
for _i in $(seq 1 100); do
  [[ -n "$(ls "$BEFORE_GO/factory/runs/".*.ready 2>/dev/null || true)" ]] && break
  sleep 0.02
done
touch "$BEFORE_GO/factory/MAINTENANCE"
wait "$BEFORE_GO_PID"
BEFORE_GO_STATUS=$?
if [[ "$BEFORE_GO_STATUS" -eq 4 ]] &&
   grep -q 'MAINTENANCE file appeared before GO' "$TMP/before-go.out" &&
   ! grep -q 'mock adapter ran task' "$BEFORE_GO/factory/runs/"*.out; then
  pass "maintenance before GO prevents task submission"
else
  fail "maintenance before GO prevents task submission" \
    "status $BEFORE_GO_STATUS"
fi

# The adapter gate never opens unless go_issued=1 reached durable storage.
GO_WRITE_FAIL="$TMP/go-marker-write-failure"
write_envelope "$GO_WRITE_FAIL"
write_ticket "$GO_WRITE_FAIL" T-227
GO_WRITE_STATUS=0
FACTORY_ROOT="$GO_WRITE_FAIL" FACTORY_GLOBAL_ENV="$TMP/no-global.env" \
  FACTORY_TEST_MODE=1 FACTORY_TEST_FAIL_GO_MANIFEST_WRITE=1 \
  FACTORY_ADAPTER_OVERRIDE=mock \
  "$RUN_AGENT" --role planner --ticket T-227 -- "go marker failure" \
  > "$TMP/go-marker-write-failure.out" 2>&1 || GO_WRITE_STATUS=$?
GO_WRITE_META="$(ls "$GO_WRITE_FAIL/factory/runs/"*.meta)"
if [[ "$GO_WRITE_STATUS" -eq 125 ]] &&
   grep -q 'could not persist GO marker' "$TMP/go-marker-write-failure.out" &&
   ! grep -q 'mock adapter ran task' "$GO_WRITE_FAIL/factory/runs/"*.out &&
   grep -q '^go_issued=0$' "$GO_WRITE_META" &&
   grep -q '^accounting_state=launch_void$' "$GO_WRITE_META" &&
   grep -q '^effective_cost=0$' "$GO_WRITE_META"; then
  pass "GO marker persistence failure keeps adapter gate closed"
else
  fail "GO marker persistence failure keeps adapter gate closed" \
    "status $GO_WRITE_STATUS"
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

# Spec-linter uses the same exact, next-semantic-round authorization. One
# authorization permits the replan + lint cycle, not a stale fourth round.
SPEC_ROUNDS="$TMP/spec-rounds"
mkdir -p "$SPEC_ROUNDS/factory/tickets"
{
  ledger_header
  ledger_row T-301 planner
  ledger_row T-301 spec-linter
  ledger_row T-301 planner
  ledger_row T-301 spec-linter
} > "$SPEC_ROUNDS/factory/ledger.csv"
cat > "$SPEC_ROUNDS/factory/tickets/T-301.md" <<'EOF'
# T-301
SPEC-LINT: FAIL — first
SPEC-LINT: FAIL — second
OPERATOR AUTHORIZATION: spec-linter round 2
OPERATOR AUTHORIZATION: spec-linter round 3 because the operator said so
EOF

if expect_stage "ESCALATE" "$SPEC_ROUNDS" T-301; then
  pass "stale or inexact spec-linter authorization is ignored"
fi

printf '%s\n' 'OPERATOR AUTHORIZATION: spec-linter round 3' >> "$SPEC_ROUNDS/factory/tickets/T-301.md"
if expect_stage "RUN planner" "$SPEC_ROUNDS" T-301; then
  pass "spec-linter authorization starts the next planning cycle"
fi

ledger_row T-301 planner >> "$SPEC_ROUNDS/factory/ledger.csv"
if expect_stage "RUN spec-linter" "$SPEC_ROUNDS" T-301; then
  pass "spec-linter authorization permits the exact lint round"
fi

ledger_row T-301 spec-linter >> "$SPEC_ROUNDS/factory/ledger.csv"
printf '%s\n' 'SPEC-LINT: FAIL — third' >> "$SPEC_ROUNDS/factory/tickets/T-301.md"
if expect_stage "ESCALATE" "$SPEC_ROUNDS" T-301; then
  pass "spent spec-linter authorization does not permit a later round"
fi

sed -i.bak 's/SPEC-LINT: FAIL — third/SPEC-LINT: PASS/' "$SPEC_ROUNDS/factory/tickets/T-301.md"
rm -f "$SPEC_ROUNDS/factory/tickets/T-301.md.bak"
if expect_stage "RUN test-author" "$SPEC_ROUNDS" T-301; then
  pass "authorized spec-linter pass advances to tests"
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
write_ticket "$GUARD" T-400
GUARD_LEDGER="$GUARD/factory/ledger.csv"
MOCK_SLEEP=5 FACTORY_ROOT="$GUARD" FACTORY_LEDGER="$GUARD_LEDGER" \
  FACTORY_GLOBAL_ENV="$TMP/no-global.env" FACTORY_TEST_MODE=1 \
  FACTORY_ADAPTER_OVERRIDE=mock \
  "$RUN_AGENT" --role planner --ticket T-400 -- "slow run" > "$TMP/first.out" 2>&1 &
FIRST_PID=$!
for _i in $(seq 1 50); do
  [[ -n "$(ls "$GUARD/factory/.active-runs/"*.pid 2>/dev/null || true)" ]] && break
  sleep 0.05
done
SECOND_OUTPUT="$(FACTORY_ROOT="$GUARD" FACTORY_LEDGER="$GUARD_LEDGER" \
  FACTORY_GLOBAL_ENV="$TMP/no-global.env" FACTORY_TEST_MODE=1 \
  FACTORY_ADAPTER_OVERRIDE=mock \
  "$RUN_AGENT" --role planner --ticket T-400 -- "overlap" 2>&1)"
SECOND_STATUS=$?
wait "$FIRST_PID"
FIRST_PID=""

if [[ "$SECOND_STATUS" -eq 7 && "$SECOND_OUTPUT" == *"live run already exists"* ]]; then
  pass "duplicate-run guard refuses overlap"
else
  fail "duplicate-run guard refuses overlap" "status $SECOND_STATUS: $SECOND_OUTPUT"
fi

SEQUENTIAL_STATUS=0
run_mock "$GUARD" planner T-400 >/dev/null 2>&1 || SEQUENTIAL_STATUS=$?
if [[ "$SEQUENTIAL_STATUS" -eq 10 &&
      "$(awk -F, '$3=="T-400" && $4=="planner" {n++} END {print n+0}' "$GUARD_LEDGER")" == "1" ]]; then
  pass "sequencer refuses obsolete sequential role"
else
  fail "sequencer refuses obsolete sequential role" "status $SEQUENTIAL_STATUS"
fi

# Kill switch terminates the isolated adapter process group and descendants.
KILL_ROOT="$TMP/kill-root"
write_envelope "$KILL_ROOT"
write_ticket "$KILL_ROOT" T-401
DESCENDANT_PID_FILE="$TMP/mock-descendant.pid"
MOCK_SLEEP=30 MOCK_DESCENDANT_PID_FILE="$DESCENDANT_PID_FILE" \
  FACTORY_ROOT="$KILL_ROOT" FACTORY_GLOBAL_ENV="$TMP/no-global.env" \
  FACTORY_TEST_MODE=1 \
  FACTORY_ADAPTER_OVERRIDE=mock \
  "$RUN_AGENT" --role planner --ticket T-401 -- "kill group" \
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
  "$RUN_AGENT" --role planner --ticket T-402 -- "must refuse" >/dev/null 2>&1 ||
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
write_ticket "$RACE_ROOT" T-403
RACE_DESCENDANT="$TMP/race-descendant.pid"
MOCK_SLEEP=30 MOCK_DESCENDANT_PID_FILE="$RACE_DESCENDANT" \
  FACTORY_ROOT="$RACE_ROOT" FACTORY_GLOBAL_ENV="$TMP/no-global.env" \
  FACTORY_TEST_MODE=1 FACTORY_TEST_BEFORE_REGISTER_SLEEP=1 \
  FACTORY_ADAPTER_OVERRIDE=mock \
  "$RUN_AGENT" --role planner --ticket T-403 -- "serialized kill" \
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

# Successful mutating roles must commit cleanly; the wrapper owns the push.
setup_role_exit_fixture() {
  local ticket="$1"
  ROLE_EXIT_ROOT="$TMP/role-exit-$ticket"
  ROLE_EXIT_WORKTREE="$TMP/role-exit-$ticket-worktree"
  ROLE_EXIT_REMOTE="$TMP/role-exit-$ticket.git"
  write_envelope "$ROLE_EXIT_ROOT"
  write_ticket "$ROLE_EXIT_ROOT" "$ticket"
  git -C "$ROLE_EXIT_ROOT" add "factory/tickets/$ticket.md"
  git -C "$ROLE_EXIT_ROOT" -c user.name=test -c user.email=test@example.com \
    commit -qm "ticket fixture"
  git init --bare -q "$ROLE_EXIT_REMOTE"
  git -C "$ROLE_EXIT_ROOT" remote add origin "$ROLE_EXIT_REMOTE"
  git -C "$ROLE_EXIT_ROOT" branch -M main
  git -C "$ROLE_EXIT_ROOT" push -q -u origin main
  git -C "$ROLE_EXIT_ROOT" worktree add -q -b "ticket/$ticket" \
    "$ROLE_EXIT_WORKTREE" main
  printf '\nKit-SHA: %s\n' "$KIT_SHA" >> "$ROLE_EXIT_WORKTREE/factory/tickets/$ticket.md"
  git -C "$ROLE_EXIT_WORKTREE" add "factory/tickets/$ticket.md"
  git -C "$ROLE_EXIT_WORKTREE" -c user.name=test -c user.email=test@example.com \
    commit -qm "ticket affinity"
  git -C "$ROLE_EXIT_WORKTREE" push -q -u origin "ticket/$ticket"
}

setup_role_exit_fixture T-607
REMOTE_DRIFT_TREE="$(git -C "$ROLE_EXIT_WORKTREE" rev-parse 'HEAD^{tree}')"
REMOTE_DRIFT_COMMIT="$(printf '%s\n' 'remote drift' | git -C "$ROLE_EXIT_WORKTREE" \
  -c user.name=test -c user.email=test@example.com \
  commit-tree "$REMOTE_DRIFT_TREE" -p HEAD)"
git -C "$ROLE_EXIT_WORKTREE" push -q origin \
  "${REMOTE_DRIFT_COMMIT}:refs/heads/ticket/T-607"
ROLE_REMOTE_STATUS=0
FACTORY_ROOT="$ROLE_EXIT_ROOT" FACTORY_GLOBAL_ENV="$TMP/no-global.env" \
  FACTORY_TEST_MODE=1 FACTORY_TEST_ENFORCE_ROLE_EXIT=1 \
  FACTORY_ADAPTER_OVERRIDE=mock \
  "$RUN_AGENT" --role planner --ticket T-607 --workdir "$ROLE_EXIT_WORKTREE" -- \
    "remote drift" > "$TMP/role-remote.out" 2>&1 || ROLE_REMOTE_STATUS=$?
if [[ "$ROLE_REMOTE_STATUS" -eq 11 &&
      ! -f "$ROLE_EXIT_ROOT/factory/runs/"*.out ]] &&
   grep -q 'role_exit_remote_mismatch' "$TMP/role-remote.out" &&
   grep -q '^accounting_schema=1$' "$ROLE_EXIT_ROOT/factory/runs/"*.meta &&
   grep -q '^accounting_state=launch_void$' "$ROLE_EXIT_ROOT/factory/runs/"*.meta &&
   grep -q '^go_issued=0$' "$ROLE_EXIT_ROOT/factory/runs/"*.meta; then
  pass "stale remote ticket branch refuses before GO"
else
  fail "stale remote ticket branch refuses before GO" "status=$ROLE_REMOTE_STATUS"
fi

setup_role_exit_fixture T-600
ROLE_NO_COMMIT=0
FACTORY_ROOT="$ROLE_EXIT_ROOT" FACTORY_GLOBAL_ENV="$TMP/no-global.env" \
  FACTORY_TEST_MODE=1 FACTORY_TEST_ENFORCE_ROLE_EXIT=1 \
  FACTORY_ADAPTER_OVERRIDE=mock \
  "$RUN_AGENT" --role planner --ticket T-600 --workdir "$ROLE_EXIT_WORKTREE" -- "no commit" \
  > "$TMP/role-no-commit.out" 2>&1 || ROLE_NO_COMMIT=$?
ROLE_COMMIT=0
MOCK_COMMIT_WORKDIR=1 FACTORY_ROOT="$ROLE_EXIT_ROOT" \
  FACTORY_GLOBAL_ENV="$TMP/no-global.env" FACTORY_TEST_MODE=1 \
  FACTORY_TEST_ENFORCE_ROLE_EXIT=1 FACTORY_ADAPTER_OVERRIDE=mock \
  "$RUN_AGENT" --role planner --ticket T-600 --workdir "$ROLE_EXIT_WORKTREE" -- "commit" \
  > "$TMP/role-commit.out" 2>&1 || ROLE_COMMIT=$?
ROLE_LOCAL_HEAD="$(git -C "$ROLE_EXIT_WORKTREE" rev-parse HEAD)"
ROLE_REMOTE_HEAD="$(git --git-dir="$ROLE_EXIT_REMOTE" rev-parse refs/heads/ticket/T-600)"
if [[ "$ROLE_NO_COMMIT" -eq 11 && "$ROLE_COMMIT" -eq 0 &&
      "$ROLE_LOCAL_HEAD" == "$ROLE_REMOTE_HEAD" ]] &&
   grep -q 'role_exit_no_commit' "$TMP/role-no-commit.out"; then
  pass "role exit requires a clean commit and pushes it non-force"
else
  fail "role exit requires a clean commit and pushes it non-force" \
    "no-commit=$ROLE_NO_COMMIT commit=$ROLE_COMMIT"
fi

if [[ "$FAILURES" -gt 0 ]]; then
  echo "FAIL: $FAILURES factory-script test(s) failed" >&2
  exit 1
fi
echo "PASS: all factory-script tests"
