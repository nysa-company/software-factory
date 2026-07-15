#!/usr/bin/env bash
# preflight-test.sh — sandboxed tests for scripts/preflight.sh.
# Stubs claude/codex/Cursor/timeout on a prepended PATH; never invokes real CLIs.
set -euo pipefail
export FACTORY_TEST_MODE=1
export FACTORY_TRUSTED_TEST_HARNESS=1

KIT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PREFLIGHT="$KIT_DIR/scripts/preflight.sh"
KIT_HEAD_NOW="$(git -C "$KIT_DIR" rev-parse HEAD)"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/preflight-test.XXXXXX")"
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
[[ -z "\${FACTORY_TEST_PROBE_TRACE:-}" ]] || echo "claude:\${1:-}" >> "\$FACTORY_TEST_PROBE_TRACE"
case "\${1:-}" in
  --version) echo "$ver (Claude Code)"; exit 0 ;;
  --help)
    echo "--max-budget-usd"
    echo "--output-format"
    echo "--append-system-prompt"
    exit 0 ;;
  auth) [[ "\${2:-}" == "status" ]] && exit 0 ;;
esac
exit 0
STUB
  chmod +x "$STUB_BIN/claude"
}

write_stub_codex() {
  local ver="${1:-0.144.1}"
  cat > "$STUB_BIN/codex" <<STUB
#!/usr/bin/env bash
[[ -z "\${FACTORY_TEST_PROBE_TRACE:-}" ]] || echo "codex:\${1:-}" >> "\$FACTORY_TEST_PROBE_TRACE"
case "\${1:-}" in
  --version) echo "codex-cli $ver"; exit 0 ;;
  login) [[ "\${2:-}" == "status" ]] && exit 0 ;;
  exec)
    if [[ "\${2:-}" == "--help" ]]; then echo "--json"; fi
    exit 0
    ;;
esac
exit 0
STUB
  chmod +x "$STUB_BIN/codex"
}

write_stub_cursor() {
  local ver="${1:-2026.07.test}" status="${2:-0}"
  cat > "$STUB_BIN/agent" <<STUB
#!/usr/bin/env bash
[[ -z "\${FACTORY_TEST_PROBE_TRACE:-}" ]] || echo "agent:\${1:-}" >> "\$FACTORY_TEST_PROBE_TRACE"
case "\${1:-}" in
  --version|-v) echo "Cursor Agent $ver"; exit 0 ;;
  --help|-h)
    printf '%s\n' --print --output-format --workspace --model --force --trust
    exit 0 ;;
  status)
    [[ "$status" == "0" ]] && echo '{"authenticated":true}'
    exit "$status" ;;
  models)
    printf '%s\n' 'gpt-5.6-sol-high' 'claude-sonnet-5-thinking-high'
    exit 0 ;;
esac
exit 0
STUB
  chmod +x "$STUB_BIN/agent"
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
  echo "factory/runtime-ledger.csv" > "$dir/.gitignore"
  printf '%s\n' "$KIT_HEAD_NOW" > "$dir/factory/KIT_PIN"
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

build_sealed_release() {
  local dir="$1"
  mkdir -p "$dir/integrations/hermes"
  cp -R "$KIT_DIR/scripts" "$dir/"
  cp "$KIT_DIR/integrations/hermes/contract.json" \
    "$dir/integrations/hermes/contract.json"
}

run_preflight() {
  local factory_root="$1" ticket="$2"
  shift 2
  local env_args=(
    PATH="$STUB_BIN:$PATH"
    FACTORY_ROOT="$factory_root"
    FACTORY_GLOBAL_ENV="$TMP/default-global.env"
    CLAUDE_CODE_PINNED=
    CODEX_PINNED=
    FACTORY_CURSOR_FALLBACK_ENABLED=0
    CURSOR_AGENT_VERSION=
    CURSOR_OPENAI_MODEL=
    CURSOR_ANTHROPIC_MODEL=
  )
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --global-env) env_args+=(FACTORY_GLOBAL_ENV="$2"); shift 2;;
      --home) env_args+=(HOME="$2"); shift 2;;
      --gh-token) env_args+=(GH_TOKEN="$2"); shift 2;;
      --projected) env_args+=(PROJECTED_TICKET_USD="$2"); shift 2;;
      --probe-trace) env_args+=(FACTORY_TEST_PROBE_TRACE="$2"); shift 2;;
      --adapter-override) env_args+=(FACTORY_ADAPTER_OVERRIDE="$2"); shift 2;;
      *) echo "unknown run_preflight opt: $1" >&2; return 2;;
    esac
  done
  env "${env_args[@]}" bash "$PREFLIGHT" --ticket "$ticket" 2>&1
}

run_sealed_preflight() {
  local factory_root="$1" ticket="$2" release="$3" tree="$4"
  env \
    PATH="$STUB_BIN:$PATH" \
    FACTORY_ROOT="$factory_root" \
    FACTORY_GLOBAL_ENV="$TMP/default-global.env" \
    FACTORY_RELEASE_SHA="$KIT_HEAD_NOW" \
    FACTORY_RELEASE_TREE="$tree" \
    FACTORY_RELEASE_PATH="$release" \
    FACTORY_RELEASE_CONTRACT_VERSION=1.1.0 \
    FACTORY_CURSOR_FALLBACK_ENABLED=0 \
    bash "$release/scripts/preflight.sh" --ticket "$ticket" 2>&1
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
write_stub_cursor "2026.07.test" "0"
write_stub_timeout
cat > "$TMP/default-global.env" <<'ENV'
GLOBAL_DAILY_CAP_USD=50.00
CLAUDE_CODE_PINNED=2.1.207
CODEX_PINNED=0.144.1
FACTORY_CURSOR_FALLBACK_ENABLED=0
ENV

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
assert_preflight "authenticated isolated mock route" 0 \
  "PASS: authenticated isolated mock route contract passed" \
  "$ALLPASS" "T-001" --global-env "$GLOBAL_ENV" --adapter-override mock

UNTRUSTED_TRACE="$TMP/untrusted-probes"
: > "$UNTRUSTED_TRACE"
UNTRUSTED_STATUS=0
UNTRUSTED_OUT="$(env -u FACTORY_TEST_MODE -u FACTORY_TRUSTED_TEST_HARNESS \
  PATH="$STUB_BIN:$PATH" \
  FACTORY_ROOT="$ALLPASS" \
  FACTORY_GLOBAL_ENV="$TMP/no-global.env" \
  FACTORY_PROBE_CODEX=UNAVAILABLE:forbidden \
  FACTORY_TEST_PROBE_TRACE="$UNTRUSTED_TRACE" \
  bash "$PREFLIGHT" --ticket T-001 2>&1)" || UNTRUSTED_STATUS=$?
if [[ "$UNTRUSTED_STATUS" -eq 1 && ! -s "$UNTRUSTED_TRACE" &&
      "$UNTRUSTED_OUT" == *"trusted internal test harness"* ]]; then
  echo "PASS: preflight rejects untrusted probe overrides before probes"
else
  echo "FAIL: preflight rejects untrusted probe overrides before probes"
  echo "$UNTRUSTED_OUT"
  FAILURES=$((FAILURES + 1))
fi

# --- trusted launcher provenance runs a real preflight from no-.git bits ---
SEALED_RELEASE="$TMP/sealed-release"
build_sealed_release "$SEALED_RELEASE"
SEALED_RELEASE="$(cd "$SEALED_RELEASE" && pwd -P)"
SEALED_TREE="$(bash -c '
  source "$1"
  factory_directory_tree "$2"
' _ "$KIT_DIR/scripts/lib/kit-pin.sh" "$SEALED_RELEASE")"
SEALED_PRODUCT="$TMP/sealed-product"
mkdir -p "$SEALED_PRODUCT"
write_envelope "$SEALED_PRODUCT"
write_ready_ticket "$SEALED_PRODUCT" "T-090"
init_git_repo "$SEALED_PRODUCT"
SEALED_OUT="$(run_sealed_preflight "$SEALED_PRODUCT" T-090 "$SEALED_RELEASE" "$SEALED_TREE")"
if [[ "$SEALED_OUT" == *"PASS: kit pin matches sealed physical release"* &&
      "$SEALED_OUT" == *"PREFLIGHT PASS"* &&
      ! -e "$SEALED_RELEASE/.git" ]]; then
  echo "PASS: sealed no-.git release runs real preflight"
else
  echo "FAIL: sealed no-.git release runs real preflight"
  echo "$SEALED_OUT"
  FAILURES=$((FAILURES + 1))
fi

FORGED_OUT="$(run_sealed_preflight "$SEALED_PRODUCT" T-090 "$SEALED_RELEASE" \
  0000000000000000000000000000000000000000)" || FORGED_STATUS=$?
FORGED_STATUS="${FORGED_STATUS:-0}"
if [[ "$FORGED_STATUS" -eq 1 &&
      "$FORGED_OUT" == *"physical release tree does not match trusted release provenance"* ]]; then
  echo "PASS: sealed release rejects forged tree metadata"
else
  echo "FAIL: sealed release rejects forged tree metadata"
  echo "$FORGED_OUT"
  FAILURES=$((FAILURES + 1))
fi

PARTIAL_STATUS=0
PARTIAL_OUT="$(env FACTORY_ROOT="$SEALED_PRODUCT" FACTORY_RELEASE_SHA="$KIT_HEAD_NOW" \
  bash "$SEALED_RELEASE/scripts/preflight.sh" --ticket T-090 2>&1)" ||
  PARTIAL_STATUS=$?
if [[ "$PARTIAL_STATUS" -eq 1 &&
      "$PARTIAL_OUT" == *"trusted release provenance is partial"* ]]; then
  echo "PASS: sealed release rejects partial launcher metadata"
else
  echo "FAIL: sealed release rejects partial launcher metadata"
  echo "$PARTIAL_OUT"
  FAILURES=$((FAILURES + 1))
fi

printf '\n# sealed release drift\n' >> "$SEALED_RELEASE/scripts/preflight.sh"
DRIFT_STATUS=0
DRIFT_OUT="$(run_sealed_preflight "$SEALED_PRODUCT" T-090 "$SEALED_RELEASE" \
  "$SEALED_TREE")" || DRIFT_STATUS=$?
if [[ "$DRIFT_STATUS" -eq 1 &&
      "$DRIFT_OUT" == *"physical release tree does not match trusted release provenance"* ]]; then
  echo "PASS: sealed release rejects physical tree drift"
else
  echo "FAIL: sealed release rejects physical tree drift"
  echo "$DRIFT_OUT"
  FAILURES=$((FAILURES + 1))
fi

SEALED_WITH_GIT="$TMP/sealed-with-git"
build_sealed_release "$SEALED_WITH_GIT"
SEALED_WITH_GIT="$(cd "$SEALED_WITH_GIT" && pwd -P)"
SEALED_WITH_GIT_TREE="$(bash -c '
  source "$1"
  factory_directory_tree "$2"
' _ "$KIT_DIR/scripts/lib/kit-pin.sh" "$SEALED_WITH_GIT")"
mkdir "$SEALED_WITH_GIT/.git"
GIT_METADATA_STATUS=0
GIT_METADATA_OUT="$(run_sealed_preflight "$SEALED_PRODUCT" T-090 \
  "$SEALED_WITH_GIT" "$SEALED_WITH_GIT_TREE")" || GIT_METADATA_STATUS=$?
if [[ "$GIT_METADATA_STATUS" -eq 1 &&
      "$GIT_METADATA_OUT" == *"trusted sealed release unexpectedly contains Git metadata"* ]]; then
  echo "PASS: sealed release rejects Git metadata"
else
  echo "FAIL: sealed release rejects Git metadata"
  echo "$GIT_METADATA_OUT"
  FAILURES=$((FAILURES + 1))
fi

# Ticket syntax is rejected before root, release, or ticket-file access.
MALFORMED_STATUS=0
MALFORMED_OUT="$(env FACTORY_ROOT="$TMP/does-not-exist" \
  FACTORY_RELEASE_SHA=partial \
  bash "$PREFLIGHT" --ticket '../T-090' 2>&1)" || MALFORMED_STATUS=$?
if [[ "$MALFORMED_STATUS" -eq 2 &&
      "$MALFORMED_OUT" == "invalid ticket identifier" ]]; then
  echo "PASS: preflight rejects malformed ticket before file access"
else
  echo "FAIL: preflight rejects malformed ticket before file access"
  echo "$MALFORMED_OUT"
  FAILURES=$((FAILURES + 1))
fi

# --- version-pin fail ---
PINFAIL="$TMP/pinfail"
mkdir -p "$PINFAIL"
write_envelope "$PINFAIL"
write_ready_ticket "$PINFAIL" "T-002"
init_git_repo "$PINFAIL"
write_stub_claude "9.9.999"
assert_preflight "version-pin fail" 1 "primary_version_mismatch" "$PINFAIL" "T-002"
write_stub_claude "2.1.207"

# --- primary ready; optional Cursor fallback unavailable warns but passes ---
FALLBACK_WARN="$TMP/fallback-warn"
mkdir -p "$FALLBACK_WARN"
write_envelope "$FALLBACK_WARN"
write_ready_ticket "$FALLBACK_WARN" "T-020"
init_git_repo "$FALLBACK_WARN"
write_stub_cursor "2026.07.test" "1"
FALLBACK_WARN_ENV="$TMP/fallback-warn-global/global.env"
mkdir -p "$(dirname "$FALLBACK_WARN_ENV")"
cat > "$FALLBACK_WARN_ENV" <<'ENV'
GLOBAL_DAILY_CAP_USD=50.00
CLAUDE_CODE_PINNED=2.1.207
CODEX_PINNED=0.144.1
FACTORY_CURSOR_FALLBACK_ENABLED=1
CURSOR_AGENT_VERSION=2026.07.test
CURSOR_OPENAI_MODEL=gpt-5.6-sol-high
CURSOR_ANTHROPIC_MODEL=claude-sonnet-5-thinking-high
ENV
assert_preflight "fallback unavailable warns" 0 "WARN: production Cursor fallback not ready" \
  "$FALLBACK_WARN" "T-020" --global-env "$FALLBACK_WARN_ENV"

# --- unavailable primary selects a ready family-matched Cursor route ---
FALLBACK_READY="$TMP/fallback-ready"
mkdir -p "$FALLBACK_READY"
write_envelope "$FALLBACK_READY"
write_ready_ticket "$FALLBACK_READY" "T-021"
init_git_repo "$FALLBACK_READY"
write_stub_cursor "2026.07.test" "0"
FALLBACK_READY_ENV="$TMP/fallback-ready-global/global.env"
mkdir -p "$(dirname "$FALLBACK_READY_ENV")"
cat > "$FALLBACK_READY_ENV" <<'ENV'
GLOBAL_DAILY_CAP_USD=50.00
CLAUDE_CODE_PINNED=2.1.207
CODEX_PINNED=0.144.1
FACTORY_CURSOR_FALLBACK_ENABLED=1
CURSOR_AGENT_VERSION=2026.07.test
CURSOR_OPENAI_MODEL=gpt-5.6-sol-high
CURSOR_ANTHROPIC_MODEL=claude-sonnet-5-thinking-high
FACTORY_PROBE_CODEX=UNAVAILABLE:test_primary_down
ENV
assert_preflight "unavailable primary selects Cursor" 0 "production primary unavailable" \
  "$FALLBACK_READY" "T-021" --global-env "$FALLBACK_READY_ENV"

# --- unavailable primary with disabled fallback has no safe route ---
NO_ROUTE="$TMP/no-route"
mkdir -p "$NO_ROUTE"
write_envelope "$NO_ROUTE"
write_ready_ticket "$NO_ROUTE" "T-022"
init_git_repo "$NO_ROUTE"
NO_ROUTE_ENV="$TMP/no-route-global/global.env"
mkdir -p "$(dirname "$NO_ROUTE_ENV")"
cat > "$NO_ROUTE_ENV" <<'ENV'
GLOBAL_DAILY_CAP_USD=50.00
CLAUDE_CODE_PINNED=2.1.207
CODEX_PINNED=0.144.1
FACTORY_CURSOR_FALLBACK_ENABLED=0
FACTORY_PROBE_CODEX=UNAVAILABLE:test_primary_down
ENV
assert_preflight "no safe backend route fails" 1 "no_ready_route" \
  "$NO_ROUTE" "T-022" --global-env "$NO_ROUTE_ENV"

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

# --- kit pin: every external product requires one canonical full SHA ---
UNPINNED="$TMP/unpinned"
mkdir -p "$UNPINNED"
write_envelope "$UNPINNED"
write_ready_ticket "$UNPINNED" "T-007"
rm "$UNPINNED/factory/KIT_PIN"
init_git_repo "$UNPINNED"
UNPINNED_TRACE="$TMP/unpinned-probes"
: > "$UNPINNED_TRACE"
assert_preflight "kit pin missing fails before probes" 1 \
  "FAIL: external product requires factory/KIT_PIN" "$UNPINNED" "T-007" \
  --global-env "$TMP/no-global.env" --probe-trace "$UNPINNED_TRACE"
if [[ -s "$UNPINNED_TRACE" ]]; then
  echo "FAIL: kit pin missing reached backend probes"
  FAILURES=$((FAILURES + 1))
else
  echo "PASS: kit pin missing reached no backend probes"
fi

ABBREVIATED="$TMP/abbreviated"
mkdir -p "$ABBREVIATED"
write_envelope "$ABBREVIATED"
write_ready_ticket "$ABBREVIATED" "T-071"
printf '%s\n' "${KIT_HEAD_NOW:0:12}" > "$ABBREVIATED/factory/KIT_PIN"
init_git_repo "$ABBREVIATED"
assert_preflight "abbreviated kit pin fails" 1 \
  "FAIL: factory/KIT_PIN must contain exactly one lowercase full 40-hex SHA" \
  "$ABBREVIATED" "T-071" --global-env "$TMP/no-global.env"

MULTIPIN="$TMP/multiple-pins"
mkdir -p "$MULTIPIN"
write_envelope "$MULTIPIN"
write_ready_ticket "$MULTIPIN" "T-072"
printf '%s\n%s\n' "$KIT_HEAD_NOW" "$KIT_HEAD_NOW" > "$MULTIPIN/factory/KIT_PIN"
init_git_repo "$MULTIPIN"
assert_preflight "multiple kit pins fail" 1 \
  "FAIL: factory/KIT_PIN must contain exactly one lowercase full 40-hex SHA" \
  "$MULTIPIN" "T-072" --global-env "$TMP/no-global.env"

# --- kit pin: match passes, mismatch fails ---
PINNED="$TMP/pinned"
mkdir -p "$PINNED"
write_envelope "$PINNED"
write_ready_ticket "$PINNED" "T-008"
init_git_repo "$PINNED"
assert_preflight "kit pin match passes" 0 "PASS: kit pin matches physical kit HEAD" "$PINNED" "T-008" --global-env "$TMP/no-global.env"
echo "0000000000000000000000000000000000000000" > "$PINNED/factory/KIT_PIN"
git -C "$PINNED" add -A && git -C "$PINNED" commit -qm "bad pin" && git -C "$PINNED" push -q origin main
assert_preflight "kit pin mismatch fails" 1 \
  "FAIL: factory/KIT_PIN does not match the selected kit SHA" \
  "$PINNED" "T-008" --global-env "$TMP/no-global.env"

# --- maintenance and durable ticket affinity are pre-probe hard gates ---
MAINTENANCE="$TMP/maintenance"
mkdir -p "$MAINTENANCE"
write_envelope "$MAINTENANCE"
write_ready_ticket "$MAINTENANCE" "T-080"
init_git_repo "$MAINTENANCE"
touch "$MAINTENANCE/factory/MAINTENANCE"
MAINTENANCE_TRACE="$TMP/maintenance-probes"
: > "$MAINTENANCE_TRACE"
assert_preflight "maintenance refuses before probes" 1 \
  "FAIL: MAINTENANCE file present" "$MAINTENANCE" "T-080" \
  --probe-trace "$MAINTENANCE_TRACE"
if [[ -s "$MAINTENANCE_TRACE" ]]; then
  echo "FAIL: maintenance refusal reached backend probes"
  FAILURES=$((FAILURES + 1))
else
  echo "PASS: maintenance refusal reached no backend probes"
fi

LEASED="$TMP/leased"
mkdir -p "$LEASED"
write_envelope "$LEASED"
write_ready_ticket "$LEASED" "T-081"
printf '\nKit-SHA: %s\n' "$KIT_HEAD_NOW" >> "$LEASED/factory/tickets/T-081.md"
init_git_repo "$LEASED"
assert_preflight "matching ticket lease passes" 0 \
  "PASS: ticket Kit-SHA affinity matches selected kit SHA" \
  "$LEASED" "T-081"

LEASE_MISMATCH="$TMP/lease-mismatch"
mkdir -p "$LEASE_MISMATCH"
write_envelope "$LEASE_MISMATCH"
write_ready_ticket "$LEASE_MISMATCH" "T-082"
sed 's/^State: Ready$/State: Blocked-Escalated/' \
  "$LEASE_MISMATCH/factory/tickets/T-082.md" > "$LEASE_MISMATCH/factory/tickets/T-082.tmp"
mv "$LEASE_MISMATCH/factory/tickets/T-082.tmp" "$LEASE_MISMATCH/factory/tickets/T-082.md"
printf '\nKit-SHA: %s\n' "0000000000000000000000000000000000000000" \
  >> "$LEASE_MISMATCH/factory/tickets/T-082.md"
init_git_repo "$LEASE_MISMATCH"
LEASE_TRACE="$TMP/lease-probes"
: > "$LEASE_TRACE"
assert_preflight "blocked ticket lease mismatch refuses" 1 \
  "FAIL: ticket Kit-SHA lease does not match the selected kit SHA" \
  "$LEASE_MISMATCH" "T-082" --probe-trace "$LEASE_TRACE"
if [[ -s "$LEASE_TRACE" ]]; then
  echo "FAIL: ticket lease mismatch reached backend probes"
  FAILURES=$((FAILURES + 1))
else
  echo "PASS: ticket lease mismatch reached no backend probes"
fi

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
