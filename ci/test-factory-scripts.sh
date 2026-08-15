#!/usr/bin/env bash
# Self-contained regression tests for run-agent.sh and next-stage.sh.
set -u
export FACTORY_TEST_MODE=1
export FACTORY_TRUSTED_TEST_HARNESS=1
export FACTORY_MODEL_PROFILE_OVERRIDE=legacy-balanced-v1

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ "$#" -eq 0 ]]; then
  ORCHESTRATION_TMP="$(mktemp -d "${TMPDIR:-/tmp}/sf-factory-orchestration.XXXXXX")"
  trap 'rm -rf "$ORCHESTRATION_TMP"' EXIT
  python3 "$ROOT/ci/factory-script-subsets-test.py" || exit
  python3 "$ROOT/ci/factory-script-subsets.py" \
    --script "$ROOT/ci/test-factory-scripts.sh" \
    --temp-root "$ORCHESTRATION_TMP/workers"
  exit
fi
if [[ "$#" -ne 2 || "$1" != "--subset" ]]; then
  echo "usage: ci/test-factory-scripts.sh [--subset model-policy|runtime-routing|launch-controls|sequencer|role-exit-git|role-exit-policy]" >&2
  exit 2
fi
SUBSET="$2"
case "$SUBSET" in
  model-policy|runtime-routing|launch-controls|sequencer|role-exit-git|role-exit-policy) ;;
  *) echo "unknown factory-script subset: $SUBSET" >&2; exit 2 ;;
esac

RUN_AGENT="$ROOT/scripts/run-agent.sh"
NEXT_STAGE="$ROOT/scripts/next-stage.sh"
KILL_SWITCH="$ROOT/scripts/kill-switch.sh"
ATTEMPT_CANCEL="$ROOT/scripts/attempt-cancel.py"
KIT_SHA="$(git -C "$ROOT" rev-parse HEAD)"
KIT_TREE="$(git -C "$ROOT" rev-parse 'HEAD^{tree}')"
PHYSICAL_KIT_PATH="$(cd "$ROOT" && pwd -P)"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/sf-factory-tests.XXXXXX")"
TMP="$(cd "$TMP" && pwd -P)"
STUB_BIN="$TMP/bin"
FAILURES=0
mkdir -p "$STUB_BIN"
FACTORY_SCRIPT_HOME="$TMP/home"
mkdir -m 700 "$FACTORY_SCRIPT_HOME" "$FACTORY_SCRIPT_HOME/.cursor"
printf '%s\n' '{}' > "$FACTORY_SCRIPT_HOME/.cursor/auth.json"
printf '%s\n' '{}' > "$FACTORY_SCRIPT_HOME/.cursor/cli-config.json"
chmod 600 "$FACTORY_SCRIPT_HOME/.cursor/auth.json" "$FACTORY_SCRIPT_HOME/.cursor/cli-config.json"
export HOME="$FACTORY_SCRIPT_HOME"

cleanup() {
  trap - EXIT HUP INT TERM
  if [[ -n "${FIRST_PID:-}" ]] && kill -0 "$FIRST_PID" 2>/dev/null; then
    kill "$FIRST_PID" 2>/dev/null || true
    wait "$FIRST_PID" 2>/dev/null || true
  fi
  for child in $(jobs -pr); do
    kill "$child" 2>/dev/null || true
  done
  wait 2>/dev/null || true
  [[ -z "${CURSOR_PROVIDER_ROOT:-}" ]] || rm -rf "$CURSOR_PROVIDER_ROOT"
  rm -rf "$TMP"
}
trap cleanup EXIT
trap 'exit 130' HUP INT TERM

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
  printf '%s\n' \
    'factory/runtime-ledger.csv' \
    'factory/runs/' \
    'factory/.active-runs/' \
    'factory/.launch.lock/' \
    'factory/.provider.lock/' \
    'factory/.ledger.lock/' > "$1/.gitignore"
  printf '%s\n' "$KIT_SHA" > "$1/factory/KIT_PIN"
  printf '%s\n' 'date,time,ticket,role,adapter,prompt_version,turns,cost_usd,exit_status,run_id,provider_family,model_id,selection_reason,cost_basis,adapter_version' \
    > "$1/factory/ledger.csv"
  [[ "$git_mode" == "no-git" ]] || init_product_git "$1"
  [[ "$git_mode" == "no-git" ]] ||
    git -C "$1" check-ignore -q factory/.provider.lock/owner ||
    fail "product fixture ignores provider lock"
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
  mkdir -p "$dir"
  cp -R "$ROOT/scripts" "$dir/"
  cp "$ROOT/factory-contract.json" \
    "$dir/factory-contract.json"
}

write_backend_stubs() {
  cat > "$STUB_BIN/codex" <<'STUB'
#!/usr/bin/env bash
case "${1:-}" in
  --version)
    [[ "${STUB_CODEX_VERSION_EMPTY:-0}" == "1" ]] || echo "${STUB_CODEX_VERSION:-codex-cli 0.144.1}"
    exit "${STUB_CODEX_VERSION_STATUS:-0}"
    ;;
  login) [[ "${2:-}" == "status" ]] && exit 0 ;;
  exec)
    if [[ "${2:-}" == "--help" ]]; then printf '%s\n' --json --model; exit 0; fi
    [[ -z "${FACTORY_TEST_TRACE:-}" ]] || echo "codex-task" >> "$FACTORY_TEST_TRACE"
    [[ -z "${FACTORY_TEST_ARGS:-}" ]] || printf '%s\n' "$*" >> "$FACTORY_TEST_ARGS"
    echo '{"type":"turn.completed","usage":{"input_tokens":100,"output_tokens":20}}'
    exit "${STUB_CODEX_STATUS:-0}"
    ;;
esac
exit 0
STUB
  chmod +x "$STUB_BIN/codex"

  cat > "$STUB_BIN/claude" <<'STUB'
#!/usr/bin/env bash
[[ -z "${STUB_CLAUDE_CONFIG_TRACE:-}" ]] ||
  printf '%s|%s\n' "${1:-}" "${CLAUDE_CONFIG_DIR:-}" >> "$STUB_CLAUDE_CONFIG_TRACE"
if [[ -n "${STUB_CLAUDE_REFUSE_AMBIENT_DIR:-}" &&
      "${CLAUDE_CONFIG_DIR:-}" == "$STUB_CLAUDE_REFUSE_AMBIENT_DIR" ]]; then
  exit 124
fi
case "${1:-}" in
  --version)
    [[ "${STUB_CLAUDE_VERSION_EMPTY:-0}" == "1" ]] ||
      echo "${STUB_CLAUDE_VERSION:-2.1.207} (Claude Code)"
    exit "${STUB_CLAUDE_VERSION_STATUS:-0}"
    ;;
  --help)
    if [[ "${STUB_CLAUDE_HELP_EMPTY:-0}" != "1" ]]; then
      printf '%s\n' --max-budget-usd --output-format --append-system-prompt --model
      [[ "${STUB_CLAUDE_MISSING_EFFORT:-0}" == "1" ]] || printf '%s\n' --effort
    fi
    exit "${STUB_CLAUDE_HELP_STATUS:-0}" ;;
  auth)
    if [[ "${2:-}" == "status" &&
          "${STUB_CLAUDE_REQUIRE_CREDENTIAL:-0}" == "1" ]]; then
      [[ -f "${CLAUDE_CONFIG_DIR:-}/.credentials.json" &&
         ! -L "${CLAUDE_CONFIG_DIR:-}/.credentials.json" ]] || exit 9
    fi
    [[ "${2:-}" == "status" ]] && exit 0 ;;
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
    if [[ -n "${STUB_CURSOR_AUTH_FAIL_ONCE_FILE:-}" &&
          ! -f "$STUB_CURSOR_AUTH_FAIL_ONCE_FILE" ]]; then
      : > "$STUB_CURSOR_AUTH_FAIL_ONCE_FILE"
      echo '{"authenticated":false}'
    elif [[ "${STUB_CURSOR_AUTH_FALSE:-0}" == "1" ]]; then
      echo '{"authenticated":false}'
    else
      echo '{"authenticated":true}'
    fi
    exit 0 ;;
  models)
    if [[ -n "${STUB_CURSOR_MODELS_FAIL_ONCE_FILE:-}" &&
          ! -f "$STUB_CURSOR_MODELS_FAIL_ONCE_FILE" ]]; then
      : > "$STUB_CURSOR_MODELS_FAIL_ONCE_FILE"
    else
      printf '%s\n' ${STUB_CURSOR_MODELS:-gpt-5.6-sol-high claude-sonnet-5-thinking-high}
    fi
    exit 0 ;;
esac

[[ -z "${FACTORY_TEST_TRACE:-}" ]] || echo "cursor-task" >> "$FACTORY_TEST_TRACE"
[[ -z "${FACTORY_TEST_ARGS:-}" ]] || printf '%s\n' "$*" >> "$FACTORY_TEST_ARGS"
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
  claude-fable-5-thinking-medium) REPORTED_MODEL="Fable 5 300K Medium" ;;
  claude-opus-5-thinking-medium) REPORTED_MODEL="Opus 5 300K Medium" ;;
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

if [[ "$SUBSET" == "model-policy" ]]; then
printf 'GLOBAL_DAILY_CAP_USD=50.00\n' > "$TMP/global-minimal.env"
printf 'CODEX_USD_PER_MTOK_IN=1.00\n' > "$TMP/global-partial-pricing.env"
GLOBAL_CONFIG_RESET="$(GLOBAL_DAILY_CAP_USD=999 FACTORY_PROBE_CODEX=stale \
  bash -c '
    source "$1"
    factory_load_plain_config "$2" global "$FACTORY_GLOBAL_CONFIG_KEYS" "" 1
    [[ -z "${FACTORY_PROBE_CODEX+x}" && "$GLOBAL_DAILY_CAP_USD" == 50.00 ]]
  ' _ "$ROOT/scripts/lib/plain-config.sh" "$TMP/global-minimal.env" && echo clean)"
PARTIAL_PRICING_STATUS=0
CODEX_USD_PER_MTOK_OUT=2.00 bash -c '
  source "$1"
  factory_load_plain_config "$2" global "$FACTORY_GLOBAL_CONFIG_KEYS" "" 1
' _ "$ROOT/scripts/lib/plain-config.sh" "$TMP/global-partial-pricing.env" \
  >/dev/null 2>&1 || PARTIAL_PRICING_STATUS=$?
MISSING_GLOBAL_RESET="$(GLOBAL_DAILY_CAP_USD=999 FACTORY_PROBE_CODEX=stale \
  bash -c '
    source "$1"
    factory_clear_plain_config_keys "$FACTORY_GLOBAL_CONFIG_KEYS"
    [[ -z "${GLOBAL_DAILY_CAP_USD+x}" && -z "${FACTORY_PROBE_CODEX+x}" ]]
  ' _ "$ROOT/scripts/lib/plain-config.sh" && echo clean)"
if [[ "$GLOBAL_CONFIG_RESET" == clean && "$PARTIAL_PRICING_STATUS" -ne 0 &&
      "$MISSING_GLOBAL_RESET" == clean ]]; then
  pass "global config clears inherited and omitted allowlisted values"
else
  fail "global config clears inherited and omitted allowlisted values" \
    "load=$GLOBAL_CONFIG_RESET partial=$PARTIAL_PRICING_STATUS missing=$MISSING_GLOBAL_RESET"
fi

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
FORGED_PRODUCTION_SCOPE="$(FACTORY_KIT_TRUST_SCOPE=production-certified bash -c '
  source "$1"
  factory_load_kit_provenance "$2" "$2/conformance" >/dev/null 2>&1 || true
  printf "%s\n" "$FACTORY_KIT_PIN_ERROR"
' _ "$ROOT/scripts/lib/kit-pin.sh" "$ROOT")"
if [[ "$IMPLICIT_PIN" == "1:$KIT_SHA" &&
      "$ROOT_PIN_ERROR" == "external product requires factory/KIT_PIN" &&
      "$FORGED_PRODUCTION_SCOPE" == \
        "production-certified requires a sealed release" ]]; then
  pass "only in-repo conformance receives implicit kit pin"
else
  fail "only in-repo conformance receives implicit kit pin" \
    "implicit=$IMPLICIT_PIN root=$ROOT_PIN_ERROR scope=$FORGED_PRODUCTION_SCOPE"
fi

ROLE_MODELS="$(bash -c '
  source "$1"
  for role in planner spec-linter test-author builder reviewer narrator; do
    printf "%s:%s:%s\\n" "$role" "$(factory_role_model "$role")" "$(factory_role_effort "$role")"
  done
' _ "$ROOT/scripts/lib/backend-policy.sh")"
if [[ "$ROLE_MODELS" == $'planner:gpt-5.6-sol:high\nspec-linter:fable:medium\ntest-author:fable:medium\nbuilder:gpt-5.6-terra:medium\nreviewer:sonnet:medium\nnarrator:gpt-5.6-terra:medium' ]]; then
  pass "role model and effort policy is explicit"
else
  fail "role model and effort policy is explicit" "$ROLE_MODELS"
fi

NO_STATE_PROFILE="$(env -u FACTORY_MODEL_PROFILE_OVERRIDE \
  -u FACTORY_MODEL_STATE_ROOT -u FACTORY_PROJECT \
  bash -c 'source "$1"; factory_load_model_probe_context; printf "%s\n" "$FACTORY_MODEL_PROFILE_ID"' \
  _ "$ROOT/scripts/lib/backend-policy.sh")"
if [[ "$NO_STATE_PROFILE" == "cursor-opus-v1" ]]; then
  pass "no-record backend context selects cursor-opus-v1"
else
  fail "no-record backend context selects cursor-opus-v1" "$NO_STATE_PROFILE"
fi

PINNED_MANAGER="$TMP/pinned-manager"
cat > "$PINNED_MANAGER" <<'STUB'
import json
print(json.dumps({
    "account_route_id": "account", "adapter": "cursor-openai",
    "adapter_version": "current-version", "effort": "high",
    "gateway_id": "gateway", "inference_provider_id": "provider",
    "provider_family": "openai", "reported_identity": "Current Identity",
    "route_id": "cursor-route", "selection_id": "model", "transport": "cli",
}))
STUB
python3 - "$TMP/old-release-journal.json" "$TMP/new-release-journal.json" <<'PY'
import json, sys
prior = {"policy_hash": "old-policy", "profile_id": "cursor-balanced-v2"}
for path, refreshed in zip(sys.argv[1:], (False, True)):
    body = {"kind": "release-migration", "prior_resolution": prior}
    if refreshed:
        body["new_resolution"] = {
            "policy_hash": "new-policy", "profile_id": "cursor-balanced-v2"
        }
    value = {
        "revisions": [{
            "body": body, "revision": 1, "revision_hash": "revision-hash"
        }],
        "schema": "ticket-model-route-journal/v2",
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(value, handle)
PY
PINNED_RELEASE_POLICIES="$(FACTORY_MODEL_MANAGER="$PINNED_MANAGER" bash -c '
  source "$1"
  for plan in "$2" "$3"; do
    factory_select_pinned_model_role "$plan" T-901 \
      0000000000000000000000000000000000000000 planner || exit
    printf "%s\n" "$FACTORY_SELECTED_POLICY_HASH"
  done
' _ "$ROOT/scripts/lib/backend-policy.sh" \
  "$TMP/old-release-journal.json" "$TMP/new-release-journal.json")"
if [[ "$PINNED_RELEASE_POLICIES" == $'old-policy\nnew-policy' ]]; then
  pass "pinned runtime reads old and refreshed release migration resolutions"
else
  fail "pinned runtime reads old and refreshed release migration resolutions" \
    "$PINNED_RELEASE_POLICIES"
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

AUTH_RETRY_MARKER="$TMP/cursor-auth-retry"
TRANSIENT_AUTH_PROBE="$(PATH="$STUB_BIN:$PATH" FACTORY_CURSOR_FALLBACK_ENABLED=1 \
  CURSOR_AGENT_VERSION=2026.07.test CURSOR_OPENAI_MODEL=gpt-5.6-sol-high \
  STUB_CURSOR_AUTH_FAIL_ONCE_FILE="$AUTH_RETRY_MARKER" \
  bash -c 'source "$1"; factory_probe_adapter cursor-openai; echo "$PROBE_STATE:$PROBE_REASON"' \
  _ "$ROOT/scripts/lib/backend-policy.sh")"
if [[ "$TRANSIENT_AUTH_PROBE" == "READY:local_contract_ready" &&
      -f "$AUTH_RETRY_MARKER" ]]; then
  pass "Cursor readiness tolerates one transient authentication miss"
else
  fail "Cursor readiness tolerates one transient authentication miss" "$TRANSIENT_AUTH_PROBE"
fi

EXPLICIT_CURSOR_PROBE="$(PATH="$STUB_BIN:$PATH" FACTORY_CURSOR_FALLBACK_ENABLED=1 \
  CURSOR_AGENT_VERSION=2026.07.test CURSOR_OPENAI_MODEL=claude-sonnet-5-thinking-high \
  bash -c 'source "$1"; factory_probe_adapter cursor-openai gpt-5.6-sol-high; echo "$PROBE_STATE:$PROBE_REASON:$PROBE_MODEL"' \
  _ "$ROOT/scripts/lib/backend-policy.sh")"
if [[ "$EXPLICIT_CURSOR_PROBE" == "READY:local_contract_ready:gpt-5.6-sol-high" ]]; then
  pass "Cursor probe validates the explicit route model"
else
  fail "Cursor probe validates the explicit route model" "$EXPLICIT_CURSOR_PROBE"
fi

MODEL_RETRY_MARKER="$TMP/cursor-model-retry"
TRANSIENT_MODEL_PROBE="$(PATH="$STUB_BIN:$PATH" FACTORY_CURSOR_FALLBACK_ENABLED=1 \
  CURSOR_AGENT_VERSION=2026.07.test CURSOR_OPENAI_MODEL=gpt-5.6-sol-high \
  STUB_CURSOR_MODELS_FAIL_ONCE_FILE="$MODEL_RETRY_MARKER" \
  bash -c 'source "$1"; factory_probe_adapter cursor-openai; echo "$PROBE_STATE:$PROBE_REASON"' \
  _ "$ROOT/scripts/lib/backend-policy.sh")"
if [[ "$TRANSIENT_MODEL_PROBE" == "READY:local_contract_ready" &&
      -f "$MODEL_RETRY_MARKER" ]]; then
  pass "Cursor readiness tolerates one transient model-list miss"
else
  fail "Cursor readiness tolerates one transient model-list miss" "$TRANSIENT_MODEL_PROBE"
fi

PROFILE_PLAN="$TMP/profile-plan.json"
PROFILE_TRACE="$TMP/profile-probes.trace"
: > "$PROFILE_TRACE"
PROFILE_RESULT="$(PATH="$STUB_BIN:$PATH" FACTORY_CURSOR_FALLBACK_ENABLED=1 \
  CURSOR_AGENT_VERSION=2026.07.test FACTORY_PROBE_TRACE="$PROFILE_TRACE" \
  FACTORY_PROBE_CODEX=READY:test FACTORY_PROBE_CLAUDE_CODE=READY:test \
  FACTORY_PROBE_CURSOR_OPENAI=READY:test FACTORY_PROBE_CURSOR_ANTHROPIC=READY:test \
  bash -c '
    source "$1"
    factory_resolve_model_profile claude-priority-v1 "$2" || {
      echo "ERROR:$FACTORY_RESOLVE_ERROR"; exit
    }
    for role in planner builder narrator spec-linter test-author reviewer; do
      factory_select_model_role "$2" "$role" || exit
      printf "%s:%s:%s:%s\n" "$role" "$FACTORY_SELECTED_ADAPTER" \
        "$FACTORY_SELECTED_FAMILY" "$FACTORY_SELECTED_MODEL"
    done
  ' _ "$ROOT/scripts/lib/backend-policy.sh" "$PROFILE_PLAN")"
PROFILE_PROBE_COUNT="$(wc -l < "$PROFILE_TRACE" | tr -d ' ')"
if [[ "$PROFILE_PROBE_COUNT" == "4" &&
      "$(grep -c '^codex|' "$PROFILE_TRACE")" == "1" &&
      "$(grep -c '^claude-code|' "$PROFILE_TRACE")" == "1" &&
      "$PROFILE_RESULT" == *"planner:claude-code:anthropic:sonnet"* &&
      "$PROFILE_RESULT" == *"spec-linter:codex:openai:gpt-5.6-terra"* &&
      "$(printf '%s\n' "$PROFILE_RESULT" | wc -l | tr -d ' ')" == "6" ]]; then
  pass "profile resolution shares native readiness and selects all family-split roles"
else
  fail "profile resolution shares native readiness and selects all family-split roles" \
    "probes=$PROFILE_PROBE_COUNT result=$PROFILE_RESULT"
fi

PARALLEL_PLAN_ONE="$TMP/parallel-plan-one.json"
PARALLEL_PLAN_TWO="$TMP/parallel-plan-two.json"
PARALLEL_READINESS_ONE="$TMP/parallel-readiness-one.json"
PARALLEL_READINESS_TWO="$TMP/parallel-readiness-two.json"
PARALLEL_TRACE="$TMP/parallel-probes.trace"
: > "$PARALLEL_TRACE"
PARALLEL_RESULT="$(/bin/bash -u -c '
  source "$1"
  trace="$6"
  probe_order=forward
  factory_probe_adapter() {
    local adapter="$1" selection="${2:-}" delay=1
    case "$probe_order:$adapter:$selection" in
      forward:codex:*|forward:cursor-anthropic:claude-fable-5-thinking-medium|reverse:claude-code:*|reverse:cursor-openai:*) delay=2 ;;
    esac
    sleep "$delay"
    printf "%s|%s|%s\n" "$probe_order" "$adapter" "$selection" >> "$trace"
    PROBE_STATE=READY
    PROBE_REASON=mock_ready
    PROBE_VERSION=test
    PROBE_MODEL=""
    PROBE_REPORTED_IDENTITY=""
    case "$adapter" in
      cursor-*)
        PROBE_MODEL="$selection"
        PROBE_REPORTED_IDENTITY="$(factory_model_report_name "$selection")"
        ;;
    esac
  }
  SECONDS=0
  factory_resolve_model_profile cursor-balanced-v2 "$2" "" "$3" || exit
  first=$SECONDS
  probe_order=reverse
  SECONDS=0
  factory_resolve_model_profile cursor-balanced-v2 "$4" "" "$5" || exit
  second=$SECONDS
  cmp -s "$2" "$4" && cmp -s "$3" "$5" || exit
  printf "%s:%s\n" "$first" "$second"
' _ "$ROOT/scripts/lib/backend-policy.sh" \
  "$PARALLEL_PLAN_ONE" "$PARALLEL_READINESS_ONE" \
  "$PARALLEL_PLAN_TWO" "$PARALLEL_READINESS_TWO" "$PARALLEL_TRACE")"
PARALLEL_EVIDENCE="$(python3 - "$PARALLEL_READINESS_ONE" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    readiness = json.load(handle)
if len(readiness) != 7 or any(value["state"] != "READY" for value in readiness.values()):
    raise SystemExit(2)
print("ready")
PY
)"
if [[ "$PARALLEL_RESULT" =~ ^[0-9]+:[0-9]+$ &&
      "${PARALLEL_RESULT%%:*}" -lt 5 && "${PARALLEL_RESULT#*:}" -lt 5 &&
      "$(grep -c '^forward|' "$PARALLEL_TRACE")" == "5" &&
      "$(grep -c '^reverse|' "$PARALLEL_TRACE")" == "5" &&
      "$PARALLEL_EVIDENCE" == "ready" ]]; then
  pass "profile readiness runs five isolated probes concurrently and aggregates deterministically"
else
  fail "profile readiness runs five isolated probes concurrently and aggregates deterministically" \
    "elapsed=$PARALLEL_RESULT probes=$(wc -l < "$PARALLEL_TRACE" | tr -d ' ') evidence=$PARALLEL_EVIDENCE"
fi

CLAUDE_AUTH_ROOT="$TMP/claude-auth-readiness"
mkdir -p "$CLAUDE_AUTH_ROOT"
chmod 700 "$CLAUDE_AUTH_ROOT"
python3 - "$CLAUDE_AUTH_ROOT/.credentials.json" <<'PY'
import json, os, sys, time
with open(sys.argv[1], "w", encoding="utf-8") as handle:
    json.dump({"claudeAiOauth":{"expiresAt":int(time.time() * 1000) + 3_600_000}}, handle)
    handle.write("\n")
os.chmod(sys.argv[1], 0o600)
PY
DEFAULT_CLAUDE_PROBE="$(PATH="$STUB_BIN:$PATH" STUB_CLAUDE_VERSION=2.1.223 \
  CLAUDE_CONFIG_DIR="$CLAUDE_AUTH_ROOT" \
  bash -c 'set -euo pipefail; source "$1"; factory_probe_adapter claude-code; echo "$PROBE_STATE:$PROBE_REASON"' \
  _ "$ROOT/scripts/lib/backend-policy.sh")"
STALE_DEFAULT_CLAUDE_PROBE="$(PATH="$STUB_BIN:$PATH" STUB_CLAUDE_VERSION=2.1.207 \
  CLAUDE_CONFIG_DIR="$CLAUDE_AUTH_ROOT" \
  bash -c 'set -euo pipefail; source "$1"; factory_probe_adapter claude-code; echo "$PROBE_STATE:$PROBE_REASON"' \
  _ "$ROOT/scripts/lib/backend-policy.sh")"
SUBSTRING_DEFAULT_CLAUDE_PROBE="$(PATH="$STUB_BIN:$PATH" STUB_CLAUDE_VERSION=12.1.223 \
  CLAUDE_CONFIG_DIR="$CLAUDE_AUTH_ROOT" \
  bash -c 'set -euo pipefail; source "$1"; factory_probe_adapter claude-code; echo "$PROBE_STATE:$PROBE_REASON"' \
  _ "$ROOT/scripts/lib/backend-policy.sh")"
if [[ "$DEFAULT_CLAUDE_PROBE" == "READY:local_contract_ready" &&
      "$STALE_DEFAULT_CLAUDE_PROBE" == "INVALID:version_mismatch" &&
      "$SUBSTRING_DEFAULT_CLAUDE_PROBE" == "INVALID:version_mismatch" ]]; then
  pass "default Claude route accepts only the certified 2.1.223 pin"
else
  fail "default Claude route accepts only the certified 2.1.223 pin" \
    "current=$DEFAULT_CLAUDE_PROBE stale=$STALE_DEFAULT_CLAUDE_PROBE substring=$SUBSTRING_DEFAULT_CLAUDE_PROBE"
fi
CLAUDE_ISOLATION_TRACE="$TMP/claude-probe-isolation.trace"
CLAUDE_ISOLATION_PROBE="$(PATH="$STUB_BIN:$PATH" STUB_CLAUDE_VERSION=2.1.223 \
  STUB_CLAUDE_CONFIG_TRACE="$CLAUDE_ISOLATION_TRACE" \
  STUB_CLAUDE_REQUIRE_CREDENTIAL=1 CLAUDE_CODE_PINNED=2.1.223 \
  CLAUDE_CONFIG_DIR="$CLAUDE_AUTH_ROOT" \
  bash -c 'set -euo pipefail; source "$1"; factory_probe_adapter claude-code; echo "$PROBE_STATE:$PROBE_REASON"' \
  _ "$ROOT/scripts/lib/backend-policy.sh")"
CLAUDE_ISOLATED_CONFIG="$(awk -F'|' 'NR == 1 {print $2}' "$CLAUDE_ISOLATION_TRACE")"
if [[ "$CLAUDE_ISOLATION_PROBE" == "READY:local_contract_ready" &&
      -n "$CLAUDE_ISOLATED_CONFIG" &&
      "$CLAUDE_ISOLATED_CONFIG" != "$CLAUDE_AUTH_ROOT" &&
      "$(awk -F'|' '{print $2}' "$CLAUDE_ISOLATION_TRACE" | sort -u | wc -l | tr -d ' ')" == "1" &&
      ! -e "$CLAUDE_ISOLATED_CONFIG" ]]; then
  pass "Claude readiness uses one disposable credential configuration"
else
  fail "Claude readiness uses one disposable credential configuration" \
    "result=$CLAUDE_ISOLATION_PROBE config=$CLAUDE_ISOLATED_CONFIG"
fi

CLAUDE_UNSAFE_ROOT="$TMP/claude-unsafe-readiness"
mkdir -p "$CLAUDE_UNSAFE_ROOT"
cp "$CLAUDE_AUTH_ROOT/.credentials.json" "$CLAUDE_UNSAFE_ROOT/.credentials.json"
chmod 600 "$CLAUDE_UNSAFE_ROOT/.credentials.json"
ln "$CLAUDE_UNSAFE_ROOT/.credentials.json" "$CLAUDE_UNSAFE_ROOT/credential-hardlink"
CLAUDE_UNSAFE_PROBE="$(PATH="$STUB_BIN:$PATH" STUB_CLAUDE_VERSION=2.1.223 \
  CLAUDE_CODE_PINNED=2.1.223 CLAUDE_CONFIG_DIR="$CLAUDE_UNSAFE_ROOT" \
  bash -c 'set -euo pipefail; source "$1"; factory_probe_adapter claude-code; echo "$PROBE_STATE:$PROBE_REASON"' \
  _ "$ROOT/scripts/lib/backend-policy.sh")"
if [[ "$CLAUDE_UNSAFE_PROBE" == "INVALID:claude_credential_hardlinked" ]]; then
  pass "Claude readiness rejects a multiply linked credential"
else
  fail "Claude readiness rejects a multiply linked credential" "$CLAUDE_UNSAFE_PROBE"
fi

CLAUDE_MISSING_ROOT="$TMP/claude-missing-readiness"
CLAUDE_SYMLINK_ROOT="$TMP/claude-symlink-readiness"
CLAUDE_MODE_ROOT="$TMP/claude-mode-readiness"
mkdir -p "$CLAUDE_MISSING_ROOT" "$CLAUDE_SYMLINK_ROOT" "$CLAUDE_MODE_ROOT"
chmod 700 "$CLAUDE_MISSING_ROOT" "$CLAUDE_SYMLINK_ROOT" "$CLAUDE_MODE_ROOT"
ln -s "$CLAUDE_AUTH_ROOT/.credentials.json" \
  "$CLAUDE_SYMLINK_ROOT/.credentials.json"
cp "$CLAUDE_AUTH_ROOT/.credentials.json" "$CLAUDE_MODE_ROOT/.credentials.json"
chmod 644 "$CLAUDE_MODE_ROOT/.credentials.json"
for case_data in \
  "$CLAUDE_MISSING_ROOT|claude_credential_missing" \
  "$CLAUDE_SYMLINK_ROOT|claude_credential_symlink" \
  "$CLAUDE_MODE_ROOT|claude_credential_mode_0644"; do
  case_root="${case_data%%|*}"
  case_reason="${case_data#*|}"
  case_result="$(PATH="$STUB_BIN:$PATH" STUB_CLAUDE_VERSION=2.1.223 \
    CLAUDE_CODE_PINNED=2.1.223 CLAUDE_CONFIG_DIR="$case_root" \
    bash -c 'set -euo pipefail; source "$1"; factory_probe_adapter claude-code; echo "$PROBE_STATE:$PROBE_REASON"' \
    _ "$ROOT/scripts/lib/backend-policy.sh")"
  if [[ "$case_result" == "INVALID:$case_reason" ]]; then
    pass "Claude readiness reports $case_reason"
  else
    fail "Claude readiness reports $case_reason" "$case_result"
  fi
done
if grep -qF 'source_info.st_uid != os.geteuid()' \
    "$ROOT/scripts/lib/backend-policy.sh"; then
  pass "Claude readiness retains foreign-owner refusal"
else
  fail "Claude readiness retains foreign-owner refusal"
fi

CLAUDE_AMBIENT_TRACE="$TMP/claude-ambient-isolation.trace"
CLAUDE_AMBIENT_PROBE="$(PATH="$STUB_BIN:$PATH" STUB_CLAUDE_VERSION=2.1.223 \
  STUB_CLAUDE_CONFIG_TRACE="$CLAUDE_AMBIENT_TRACE" \
  STUB_CLAUDE_REFUSE_AMBIENT_DIR="$CLAUDE_AUTH_ROOT" \
  CLAUDE_CODE_PINNED=2.1.223 CLAUDE_CONFIG_DIR="$CLAUDE_AUTH_ROOT" \
  bash -c 'set -euo pipefail; source "$1"; factory_probe_adapter claude-code; echo "$PROBE_STATE:$PROBE_REASON"' \
  _ "$ROOT/scripts/lib/backend-policy.sh")"
if [[ "$CLAUDE_AMBIENT_PROBE" == "READY:local_contract_ready" ]] &&
   ! grep -qF "|$CLAUDE_AUTH_ROOT" "$CLAUDE_AMBIENT_TRACE"; then
  pass "Claude readiness succeeds when only the ambient CLI contract hangs"
else
  fail "Claude readiness succeeds when only the ambient CLI contract hangs" \
    "$CLAUDE_AMBIENT_PROBE"
fi

CLAUDE_CONCURRENT_TRACE="$TMP/claude-concurrent-isolation.trace"
for worker in 1 2; do
  PATH="$STUB_BIN:$PATH" STUB_CLAUDE_VERSION=2.1.223 \
    STUB_CLAUDE_CONFIG_TRACE="$CLAUDE_CONCURRENT_TRACE" \
    CLAUDE_CODE_PINNED=2.1.223 CLAUDE_CONFIG_DIR="$CLAUDE_AUTH_ROOT" \
    bash -c 'set -euo pipefail; source "$1"; factory_probe_adapter claude-code; echo "$PROBE_STATE:$PROBE_REASON"' \
    _ "$ROOT/scripts/lib/backend-policy.sh" > "$TMP/claude-concurrent-$worker.out" &
done
wait
CLAUDE_CONCURRENT_DIRS="$(cut -d'|' -f2 "$CLAUDE_CONCURRENT_TRACE" | sort -u)"
CLAUDE_CONCURRENT_COUNT="$(printf '%s\n' "$CLAUDE_CONCURRENT_DIRS" | sed '/^$/d' | wc -l | tr -d ' ')"
CLAUDE_CONCURRENT_LEFT=0
while IFS= read -r probe_dir; do
  [[ -z "$probe_dir" || ! -e "$probe_dir" ]] || CLAUDE_CONCURRENT_LEFT=1
done <<EOF
$CLAUDE_CONCURRENT_DIRS
EOF
if [[ "$CLAUDE_CONCURRENT_COUNT" == "2" && "$CLAUDE_CONCURRENT_LEFT" == "0" &&
      "$(cat "$TMP/claude-concurrent-1.out")" == "READY:local_contract_ready" &&
      "$(cat "$TMP/claude-concurrent-2.out")" == "READY:local_contract_ready" ]]; then
  pass "concurrent Claude probes use distinct disposable configurations"
else
  fail "concurrent Claude probes use distinct disposable configurations" \
    "directories=$CLAUDE_CONCURRENT_COUNT left=$CLAUDE_CONCURRENT_LEFT"
fi
CONTRACT_PROFILE="$(PATH="$STUB_BIN:$PATH" STUB_CLAUDE_VERSION=2.1.223 \
  FACTORY_GLOBAL_ENV="$TMP/no-global.env" FACTORY_CURSOR_FALLBACK_ENABLED=1 \
  CLAUDE_CONFIG_DIR="$CLAUDE_AUTH_ROOT" \
  FACTORY_PROBE_CODEX=READY:test FACTORY_PROBE_CLAUDE_CODE=READY:test \
  FACTORY_PROBE_CURSOR_OPENAI=READY:test FACTORY_PROBE_CURSOR_ANTHROPIC=READY:test \
  "$ROOT/scripts/adapters/contract-test.sh" --profile claude-priority-v1 2>&1)"
if [[ "$(printf '%s\n' "$CONTRACT_PROFILE" | \
      awk '/ route: / {count++} END {print count+0}')" == "6" &&
      "$CONTRACT_PROFILE" == *"requested adapter contracts hold"* ]]; then
  pass "contract profile mode reports all six roles without task submission"
else
  fail "contract profile mode reports all six roles without task submission" \
    "$CONTRACT_PROFILE"
fi

DISABLED_PLAN="$TMP/disabled-plan.json"
DISABLED_TRACE="$TMP/disabled-probes.trace"
: > "$DISABLED_TRACE"
DISABLED_RESULT="$(PATH="$STUB_BIN:$PATH" FACTORY_CURSOR_FALLBACK_ENABLED=1 \
  CURSOR_AGENT_VERSION=2026.07.test FACTORY_PROBE_TRACE="$DISABLED_TRACE" \
  FACTORY_PROBE_CODEX=READY:test FACTORY_PROBE_CLAUDE_CODE=READY:test \
  FACTORY_PROBE_CURSOR_OPENAI=READY:test FACTORY_PROBE_CURSOR_ANTHROPIC=READY:test \
  bash -c '
    source "$1"
    factory_resolve_model_profile legacy-balanced-v1 "$2" cursor-gpt-5.6-sol-high &&
      factory_select_model_role "$2" planner &&
      echo "$FACTORY_SELECTED_ROUTE_ID"
  ' _ "$ROOT/scripts/lib/backend-policy.sh" "$DISABLED_PLAN")"
if [[ "$(wc -l < "$DISABLED_TRACE" | tr -d ' ')" == "3" &&
      "$DISABLED_RESULT" == "codex-gpt-5.6-sol" &&
      "$(< "$DISABLED_TRACE")" != *"cursor-openai|gpt-5.6-sol-high"* ]]; then
  pass "disabled route is unavailable without a CLI probe"
else
  fail "disabled route is unavailable without a CLI probe" "$DISABLED_RESULT"
fi

for BAD_STATE in INVALID UNKNOWN; do
  BAD_PLAN="$TMP/bad-$BAD_STATE-plan.json"
  BAD_RESULT="$(PATH="$STUB_BIN:$PATH" FACTORY_CURSOR_FALLBACK_ENABLED=1 \
    CURSOR_AGENT_VERSION=2026.07.test FACTORY_PROBE_CODEX="$BAD_STATE:test" \
    FACTORY_PROBE_CLAUDE_CODE=READY:test FACTORY_PROBE_CURSOR_OPENAI=READY:test \
    FACTORY_PROBE_CURSOR_ANTHROPIC=READY:test \
    bash -c 'source "$1"; if factory_resolve_model_profile legacy-balanced-v1 "$2"; then echo READY; else echo "$FACTORY_RESOLVE_ERROR"; fi' \
    _ "$ROOT/scripts/lib/backend-policy.sh" "$BAD_PLAN")"
  if [[ "$BAD_RESULT" == "profile_resolution_failed" ]]; then
    pass "$BAD_STATE readiness fails closed"
  else
    fail "$BAD_STATE readiness fails closed" "$BAD_RESULT"
  fi
done

EMPTY_CODEX_VERSION_PROBE="$(PATH="$STUB_BIN:$PATH" CODEX_PINNED=0.144.1 \
  STUB_CODEX_VERSION_EMPTY=1 STUB_CODEX_VERSION_STATUS=124 \
  bash -c 'set -euo pipefail; source "$1"; factory_probe_adapter codex; echo "$PROBE_STATE:$PROBE_REASON"' \
  _ "$ROOT/scripts/lib/backend-policy.sh")"
EMPTY_CLAUDE_VERSION_PROBE="$(PATH="$STUB_BIN:$PATH" CLAUDE_CODE_PINNED=2.1.207 \
  STUB_CLAUDE_VERSION_EMPTY=1 STUB_CLAUDE_VERSION_STATUS=124 \
  bash -c 'set -euo pipefail; source "$1"; factory_probe_adapter claude-code; echo "$PROBE_STATE:$PROBE_REASON"' \
  _ "$ROOT/scripts/lib/backend-policy.sh")"
MISSING_CLAUDE_FLAG_PROBE="$(PATH="$STUB_BIN:$PATH" CLAUDE_CODE_PINNED=2.1.207 \
  STUB_CLAUDE_MISSING_EFFORT=1 \
  bash -c 'set -euo pipefail; source "$1"; factory_probe_adapter claude-code; echo "$PROBE_STATE:$PROBE_REASON"' \
  _ "$ROOT/scripts/lib/backend-policy.sh")"
FAILED_CLAUDE_HELP_PROBE="$(PATH="$STUB_BIN:$PATH" CLAUDE_CODE_PINNED=2.1.207 \
  STUB_CLAUDE_HELP_EMPTY=1 STUB_CLAUDE_HELP_STATUS=124 \
  bash -c 'set -euo pipefail; source "$1"; factory_probe_adapter claude-code; echo "$PROBE_STATE:$PROBE_REASON"' \
  _ "$ROOT/scripts/lib/backend-policy.sh")"
if [[ "$EMPTY_CODEX_VERSION_PROBE" == "UNAVAILABLE:version_probe_failed" &&
      "$EMPTY_CLAUDE_VERSION_PROBE" == "UNAVAILABLE:version_probe_failed" &&
      "$MISSING_CLAUDE_FLAG_PROBE" == "INVALID:contract_mismatch_missing_effort" &&
      "$FAILED_CLAUDE_HELP_PROBE" == "UNAVAILABLE:help_probe_failed" ]]; then
  pass "empty primary version probes permit startup fallback"
else
  fail "empty primary version probes permit startup fallback" \
    "codex=$EMPTY_CODEX_VERSION_PROBE claude=$EMPTY_CLAUDE_VERSION_PROBE missing=$MISSING_CLAUDE_FLAG_PROBE help=$FAILED_CLAUDE_HELP_PROBE"
fi

CONFUSABLE_CODEX_PROBE="$(PATH="$STUB_BIN:$PATH" CODEX_PINNED=0.144.1 \
  STUB_CODEX_VERSION='codex-cli 10.144.10' \
  bash -c 'set -euo pipefail; source "$1"; factory_probe_adapter codex; echo "$PROBE_STATE:$PROBE_REASON"' \
  _ "$ROOT/scripts/lib/backend-policy.sh")"
CONFUSABLE_CODEX_TRACE="$TMP/confusable-codex.trace"
CONFUSABLE_CODEX_STATUS=0
: > "$CONFUSABLE_CODEX_TRACE"
PATH="$STUB_BIN:$PATH" CODEX_PINNED=0.144.1 \
  STUB_CODEX_VERSION='codex-cli 10.144.10' \
  FACTORY_TEST_TRACE="$CONFUSABLE_CODEX_TRACE" \
  "$ROOT/scripts/adapters/codex.sh" --budget 1 --max-turns 1 \
    --timeout-min 1 --prompt-file /dev/null --workdir "$TMP" \
    --model gpt-test --effort medium -- task >/dev/null 2>&1 ||
  CONFUSABLE_CODEX_STATUS=$?
if [[ "$CONFUSABLE_CODEX_PROBE" == "INVALID:version_mismatch" &&
      "$CONFUSABLE_CODEX_STATUS" -eq 6 &&
      ! -s "$CONFUSABLE_CODEX_TRACE" ]]; then
  pass "Codex readiness and adapter require an exact parsed version"
else
  fail "Codex readiness and adapter require an exact parsed version" \
    "probe=$CONFUSABLE_CODEX_PROBE status=$CONFUSABLE_CODEX_STATUS"
fi

python3 - "$CLAUDE_AUTH_ROOT/.credentials.json" <<'PY'
import json, os, sys, time
with open(sys.argv[1], "w", encoding="utf-8") as handle:
    json.dump({"claudeAiOauth":{"expiresAt":int(time.time() * 1000) - 1}}, handle)
    handle.write("\n")
os.chmod(sys.argv[1], 0o600)
PY
EXPIRED_CLAUDE_PROBE="$(PATH="$STUB_BIN:$PATH" CLAUDE_CODE_PINNED=2.1.207 \
  CLAUDE_CONFIG_DIR="$CLAUDE_AUTH_ROOT" \
  bash -c 'set -euo pipefail; source "$1"; factory_probe_adapter claude-code; echo "$PROBE_STATE:$PROBE_REASON"' \
  _ "$ROOT/scripts/lib/backend-policy.sh")"
if [[ "$EXPIRED_CLAUDE_PROBE" == "UNAVAILABLE:authentication_expired" ]]; then
  pass "expired Claude OAuth falls back before task submission"
else
  fail "expired Claude OAuth falls back before task submission" "$EXPIRED_CLAUDE_PROBE"
fi

CLAUDE_TOKEN_HOME="$TMP/claude-token-home"
CLAUDE_UNSAFE_TOKEN_HOME="$TMP/claude-unsafe-token-home"
mkdir -m 700 "$CLAUDE_TOKEN_HOME" "$CLAUDE_TOKEN_HOME/.factory" \
  "$CLAUDE_UNSAFE_TOKEN_HOME" "$CLAUDE_UNSAFE_TOKEN_HOME/.factory"
CLAUDE_TOKEN="sk-ant-oat01-$(printf 'A%.0s' {1..80})"
printf '%s\n' "$CLAUDE_TOKEN" \
  >"$CLAUDE_TOKEN_HOME/.factory/claude-oauth-token"
chmod 600 "$CLAUDE_TOKEN_HOME/.factory/claude-oauth-token"
CLAUDE_TOKEN_PROBE="$(HOME="$CLAUDE_TOKEN_HOME" PATH="$STUB_BIN:$PATH" \
  STUB_CLAUDE_VERSION=2.1.223 STUB_CLAUDE_REQUIRE_CREDENTIAL=1 \
  CLAUDE_CODE_PINNED=2.1.223 CLAUDE_CONFIG_DIR="$CLAUDE_AUTH_ROOT" \
  bash -c 'set -euo pipefail; source "$1"; factory_probe_adapter claude-code; echo "$PROBE_STATE:$PROBE_REASON"' \
  _ "$ROOT/scripts/lib/backend-policy.sh")"
ln -s "$CLAUDE_TOKEN_HOME/.factory/claude-oauth-token" \
  "$CLAUDE_UNSAFE_TOKEN_HOME/.factory/claude-oauth-token"
CLAUDE_UNSAFE_TOKEN_PROBE="$(HOME="$CLAUDE_UNSAFE_TOKEN_HOME" \
  PATH="$STUB_BIN:$PATH" STUB_CLAUDE_VERSION=2.1.223 \
  CLAUDE_CODE_PINNED=2.1.223 CLAUDE_CONFIG_DIR="$CLAUDE_AUTH_ROOT" \
  bash -c 'set -euo pipefail; source "$1"; factory_probe_adapter claude-code; echo "$PROBE_STATE:$PROBE_REASON"' \
  _ "$ROOT/scripts/lib/backend-policy.sh")"
if [[ "$CLAUDE_TOKEN_PROBE" == "READY:local_contract_ready" &&
      "$CLAUDE_UNSAFE_TOKEN_PROBE" == \
        "INVALID:claude_subscription_token_unsafe" ]]; then
  pass "Claude setup token supplies isolated readiness and unsafe tokens refuse"
else
  fail "Claude setup token supplies isolated readiness and unsafe tokens refuse" \
    "ready=$CLAUDE_TOKEN_PROBE unsafe=$CLAUDE_UNSAFE_TOKEN_PROBE"
fi
fi

run_mock() {
  write_ticket "$1" "$3"
  FACTORY_ROOT="$1" \
  FACTORY_GLOBAL_ENV="$TMP/no-global.env" \
  FACTORY_TEST_MODE=1 \
  FACTORY_CERTIFIED_PRODUCT_ORIGIN="$TMP/caller-origin.git" \
  FACTORY_TRUSTED_PRODUCT_ORIGIN="$TMP/caller-trusted.git" \
  FACTORY_ADAPTER_OVERRIDE=mock \
    "$RUN_AGENT" --role "$2" --ticket "$3" -- "test task"
}

if [[ "$SUBSET" == "model-policy" ]]; then
# Optional role values inherit independently and the selected exact values are
# frozen into the run manifest that supplies adapter arguments.
ROLE_ENVELOPE="$TMP/role-envelope"
write_envelope "$ROLE_ENVELOPE"
cat >> "$ROLE_ENVELOPE/factory/ENVELOPE.env" <<'ENV'
PLANNER_PER_RUN_BUDGET_USD=2.25
PLANNER_PER_RUN_MAX_TURNS=9
PLANNER_PER_RUN_TIMEOUT_MIN=3
ENV
if run_mock "$ROLE_ENVELOPE" planner T-189 >/dev/null 2>&1 &&
   grep -l 'envelope_per_run_budget_usd=2.25' \
     "$ROLE_ENVELOPE/factory/runs"/*.meta >/dev/null 2>&1 &&
   grep -l 'envelope_max_turns=9' \
     "$ROLE_ENVELOPE/factory/runs"/*.meta >/dev/null 2>&1 &&
   grep -l 'envelope_timeout_min=3' \
     "$ROLE_ENVELOPE/factory/runs"/*.meta >/dev/null 2>&1; then
  pass "role envelope values reach the exact run manifest"
else
  fail "role envelope values reach the exact run manifest"
fi
fi

ledger_header() {
  printf '%s\n' 'date,time,ticket,role,adapter,prompt_version,turns,cost_usd,exit_status,run_id,provider_family,model_id,selection_reason,cost_basis,adapter_version'
}

ledger_row() {
  printf '2026-07-13,06:00:00,%s,%s,mock,test,1,0.10,0,,,,,,\n' "$1" "$2"
}

ledger_row_run() {
  printf '2026-07-13,06:00:00,%s,%s,mock,test,1,0.10,0,%s,,,,,\n' "$1" "$2" "$3"
}

write_run_manifest() {
  local root="$1" ticket="$2" role="$3" run_id="$4" head="$5" output_sha256
  mkdir -p "$root/factory/runs"
  printf '%s output\n' "$role" > "$root/factory/runs/$run_id.out"
  output_sha256="$(shasum -a 256 "$root/factory/runs/$run_id.out" | awk '{print $1}')"
  printf '%s\n' \
    "run_id=$run_id" \
    'phase=completed' \
    'accounting_schema=1' \
    'accounting_state=completed' \
    'cost_basis=test_fixture' \
    'go_issued=1' \
    'task_submitted=1' \
    'exit_status=0' \
    "ticket=$ticket" \
    "role=$role" \
    'role_exit=ok' \
    "output_sha256=$output_sha256" \
    "role_head_before=$head" > "$root/factory/runs/$run_id.meta"
}

expect_stage() {
  local expected="$1" root="$2" ticket="$3" actual status certified_origin contract
  mkdir -p "$root/factory/runs"
  [[ -f "$root/factory/KIT_PIN" ]] ||
    printf '%s\n' "$KIT_SHA" > "$root/factory/KIT_PIN"
  if ! git -C "$root" rev-parse --git-dir >/dev/null 2>&1; then
    init_product_git "$root"
  fi
  certified_origin="$(git -C "$root" remote get-url --push origin 2>/dev/null || true)"
  contract="${TEST_CONTRACT_VERSION:-1.2.0}"
  if [[ "$contract" == "1.8.0" || "$contract" == "2.0.0" ]]; then
    actual="$(FACTORY_ROOT="$root" FACTORY_LEDGER="$root/factory/ledger.csv" \
      FACTORY_CERTIFIED_PRODUCT_ORIGIN="$certified_origin" \
      FACTORY_RELEASE_SHA="$KIT_SHA" FACTORY_RELEASE_TREE="$SEALED_TREE" \
      FACTORY_RELEASE_PATH="$SEALED_RELEASE" \
      FACTORY_RELEASE_CONTRACT_VERSION="$contract" \
      "$SEALED_RELEASE/scripts/next-stage.sh" --ticket "$ticket" 2>&1)"
  else
    actual="$(FACTORY_ROOT="$root" FACTORY_LEDGER="$root/factory/ledger.csv" \
      FACTORY_CERTIFIED_PRODUCT_ORIGIN="$certified_origin" \
      FACTORY_CONTRACT_VERSION="$contract" \
      "$NEXT_STAGE" --ticket "$ticket" 2>&1)"
  fi
  status=$?
  [[ "$actual" == "$expected"* ]] || {
    fail "$ticket expected '$expected'" "got '$actual' (status $status)"
    return 1
  }
  return 0
}

# Real sequencer and runner execute from a physical no-.git release when the
# trusted launcher supplies a complete, self-consistent provenance tuple.
if [[ "$SUBSET" == "model-policy" || "$SUBSET" == "sequencer" ]]; then
SEALED_RELEASE="$TMP/sealed-release"
build_sealed_release "$SEALED_RELEASE"
SEALED_RELEASE="$(cd "$SEALED_RELEASE" && pwd -P)"
SEALED_TREE="$(bash -c '
  source "$1"
  factory_directory_tree "$2"
' _ "$ROOT/scripts/lib/kit-pin.sh" "$SEALED_RELEASE")"
fi

if [[ "$SUBSET" == "model-policy" ]]; then
SEALED_PRODUCT="$TMP/sealed-product"
write_envelope "$SEALED_PRODUCT"
write_ticket "$SEALED_PRODUCT" T-190
git -C "$SEALED_PRODUCT" add .gitignore factory/tickets/T-190.md
git -C "$SEALED_PRODUCT" -c user.name=test -c user.email=test@example.com \
  commit -qm "sealed ticket fixture"
SEALED_ORIGIN="$TMP/sealed-product.git"
git init --bare -q "$SEALED_ORIGIN"
git -C "$SEALED_PRODUCT" remote add origin "$SEALED_ORIGIN"
git -C "$SEALED_PRODUCT" branch -M main
git -C "$SEALED_PRODUCT" push -q -u origin main
git -C "$SEALED_PRODUCT" switch -q -c ticket/T-190
git -C "$SEALED_PRODUCT" push -q -u origin ticket/T-190
mkdir -p "$SEALED_PRODUCT/factory/runs"
SEALED_STATE="$TMP/sealed-state"
mkdir -m 700 "$SEALED_STATE"
SEALED_STAGE="$(env \
  FACTORY_ROOT="$SEALED_PRODUCT" \
  FACTORY_RELEASE_SHA="$KIT_SHA" \
  FACTORY_RELEASE_TREE="$SEALED_TREE" \
  FACTORY_RELEASE_PATH="$SEALED_RELEASE" \
  FACTORY_RELEASE_CONTRACT_VERSION=2.0.0 \
  "$SEALED_RELEASE/scripts/next-stage.sh" --ticket T-190 2>&1)"
SEALED_READY_HEAD="$(git -C "$SEALED_PRODUCT" rev-parse HEAD)"
SEALED_TRANSITION="$(env \
  FACTORY_KIT_TRUST_SCOPE=repository-test \
  FACTORY_TEST_MODE=1 \
  FACTORY_TRUSTED_TEST_HARNESS=1 \
  FACTORY_ADAPTER_OVERRIDE=mock \
  FACTORY_CERTIFIED_PRODUCT_ORIGIN="$SEALED_ORIGIN" \
  FACTORY_RELEASE_SHA="$KIT_SHA" \
  FACTORY_RELEASE_TREE="$SEALED_TREE" \
  FACTORY_RELEASE_PATH="$SEALED_RELEASE" \
  FACTORY_RELEASE_CONTRACT_VERSION=2.0.0 \
  python3 "$SEALED_RELEASE/scripts/state-machine.py" next \
    --factory-root "$SEALED_PRODUCT" --workdir "$SEALED_PRODUCT" \
    --kit-dir "$SEALED_RELEASE" --state-dir "$SEALED_STATE" \
    --ticket T-190 --contract-version 2.0.0 --factory-sha "$KIT_SHA" \
    --project sealed)"
SEALED_PLANNING_HEAD="$(git -C "$SEALED_PRODUCT" rev-parse HEAD)"
SEALED_PLANNING_PARENT="$(git -C "$SEALED_PRODUCT" rev-parse HEAD^)"
SEALED_PLANNING_TICKET="$(git -C "$SEALED_PRODUCT" show HEAD:factory/tickets/T-190.md)"
SEALED_PLANNING_REMOTE="$(git -C "$SEALED_PRODUCT" ls-remote --heads -- "$SEALED_ORIGIN" refs/heads/ticket/T-190 | awk 'NR==1 {print $1; exit}')"
if [[ "$SEALED_PLANNING_HEAD" != "$SEALED_READY_HEAD" &&
      "$SEALED_PLANNING_PARENT" == "$SEALED_READY_HEAD" &&
      "$SEALED_PLANNING_REMOTE" == "$SEALED_PLANNING_HEAD" &&
      -z "$(git -C "$SEALED_PRODUCT" status --porcelain)" ]] &&
   grep -q '^State: Planning$' <<<"$SEALED_PLANNING_TICKET" &&
   grep -q "^Kit-SHA: $KIT_SHA$" <<<"$SEALED_PLANNING_TICKET"; then
  pass "repository-test transition binds kit affinity before receipt"
else
  fail "repository-test transition binds kit affinity before receipt"
fi
SEALED_RECEIPT="$(python3 -c \
  'import json,sys; print(json.load(sys.stdin)["receipt"])' \
  <<<"$SEALED_TRANSITION")"
env \
  FACTORY_CERTIFIED_PRODUCT_ORIGIN="$SEALED_ORIGIN" \
  python3 "$SEALED_RELEASE/scripts/state-machine.py" consume \
    --factory-root "$SEALED_PRODUCT" --workdir "$SEALED_PRODUCT" \
    --kit-dir "$SEALED_RELEASE" --state-dir "$SEALED_STATE" \
    --ticket T-190 --contract-version 2.0.0 --factory-sha "$KIT_SHA" \
    --project sealed --receipt "$SEALED_RECEIPT" --role planner >/dev/null
SEALED_RUN_STATUS=0
: > "$TMP/repository-test-global.env"
: > "$TMP/repository-test-provider-activation.json"
env \
  FACTORY_ROOT="$SEALED_PRODUCT" \
  FACTORY_CERTIFIED_PRODUCT_ORIGIN="$SEALED_ORIGIN" \
  FACTORY_PROJECT=sealed \
  FACTORY_TRANSITION_RECEIPT_SHA256="$SEALED_RECEIPT" \
  FACTORY_TRANSITION_STATE_DIR="$SEALED_STATE" \
  FACTORY_GLOBAL_ENV="$TMP/repository-test-global.env" \
  FACTORY_PROVIDER_ACTIVATION="$TMP/repository-test-provider-activation.json" \
  FACTORY_TEST_MODE=1 \
  FACTORY_ADAPTER_OVERRIDE=mock \
  FACTORY_KIT_TRUST_SCOPE=repository-test \
  FACTORY_RELEASE_SHA="$KIT_SHA" \
  FACTORY_RELEASE_TREE="$SEALED_TREE" \
  FACTORY_RELEASE_PATH="$SEALED_RELEASE" \
  FACTORY_RELEASE_CONTRACT_VERSION=2.0.0 \
  "$SEALED_RELEASE/scripts/run-agent.sh" \
    --role planner --ticket T-190 -- "sealed run" >/dev/null 2>&1 ||
  SEALED_RUN_STATUS=$?
SEALED_AFTER="$(env \
  FACTORY_ROOT="$SEALED_PRODUCT" \
  FACTORY_RELEASE_SHA="$KIT_SHA" \
  FACTORY_RELEASE_TREE="$SEALED_TREE" \
  FACTORY_RELEASE_PATH="$SEALED_RELEASE" \
  FACTORY_RELEASE_CONTRACT_VERSION=2.0.0 \
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
   grep -q '^contract_version=2.0.0$' "$SEALED_META" &&
   grep -q "^physical_kit_path=$SEALED_RELEASE$" "$SEALED_META" &&
   grep -q '^kit_provenance_mode=sealed$' "$SEALED_META" &&
   grep -q '^kit_provenance_scope=repository-test$' "$SEALED_META" &&
   grep -q "^transition_receipt_sha256=$SEALED_RECEIPT$" "$SEALED_META" &&
   grep -q "^Kit-SHA: $KIT_SHA$" "$SEALED_PRODUCT/factory/tickets/T-190.md"; then
  pass "sealed release runs real sequencer and mock agent"
else
  fail "sealed release runs real sequencer and mock agent" \
    "before=$SEALED_STAGE transition=$SEALED_TRANSITION run=$SEALED_RUN_STATUS after=$SEALED_AFTER"
fi

# A provider-free Cursor stub drives the complete shared-account boundary:
# account acquire, isolated-wrapper READY, runtime binding, GO/start validation,
# process-group drainage, and lease release. All credentials and accounting are
# fixture-local; no ambient subscription CLI is invoked.
CURSOR_RESOLUTION="$TMP/sealed-cursor-resolution.json"
CURSOR_MODEL_STATE="$SEALED_STATE/model-state"
CURSOR_GLOBAL="$TMP/sealed-cursor-global/global.env"
CURSOR_HOME="$TMP/sealed-cursor-home"
CURSOR_ACCOUNT_ROOT="$TMP/sealed-cursor-account"
CURSOR_PROVIDER_ROOT="$(mktemp -d /tmp/sfc380.XXXXXX)"
CURSOR_PROVIDER_ROOT="$(cd "$CURSOR_PROVIDER_ROOT" && pwd -P)"
mkdir -m 700 "$CURSOR_HOME" "$CURSOR_HOME/.cursor" "$CURSOR_ACCOUNT_ROOT"
printf '%s\n' '{"cursor":"fixture"}' > "$CURSOR_HOME/.cursor/auth.json"
printf '%s\n' '{"version":1}' > "$CURSOR_HOME/.cursor/cli-config.json"
chmod 600 "$CURSOR_HOME/.cursor/auth.json" "$CURSOR_HOME/.cursor/cli-config.json"
CURSOR_AGENT_WRAPPER="$STUB_BIN/fable-agent"
cat > "$CURSOR_AGENT_WRAPPER" <<EOF
#!/usr/bin/env bash
export STUB_CURSOR_MODELS='gpt-5.6-sol-high claude-fable-5-thinking-medium claude-opus-5-thinking-medium claude-sonnet-5-thinking-high'
exec "$STUB_BIN/agent" "\$@"
EOF
chmod 700 "$CURSOR_AGENT_WRAPPER"
write_backend_global "$CURSOR_GLOBAL" \
  "export FACTORY_PROBE_CURSOR_ANTHROPIC=READY:test
export CURSOR_AGENT_BIN=$CURSOR_AGENT_WRAPPER"
PATH="$STUB_BIN:$PATH" FACTORY_CURSOR_FALLBACK_ENABLED=1 \
  CURSOR_AGENT_VERSION=2026.07.test \
  FACTORY_PROBE_CODEX=READY:test FACTORY_PROBE_CLAUDE_CODE=READY:test \
  FACTORY_PROBE_CURSOR_OPENAI=READY:test \
  FACTORY_PROBE_CURSOR_ANTHROPIC=READY:test \
  bash -c 'source "$1"; factory_resolve_model_profile cursor-balanced-v2 "$2"' \
    _ "$SEALED_RELEASE/scripts/lib/backend-policy.sh" "$CURSOR_RESOLUTION"
mkdir -p "$SEALED_PRODUCT/factory/route-plans"
python3 -B "$SEALED_RELEASE/scripts/model-manager.py" pin \
  --state-root "$CURSOR_MODEL_STATE" --project sealed --ticket T-190 \
  --kit-sha "$KIT_SHA" --resolution-file "$CURSOR_RESOLUTION" \
  --output "$SEALED_PRODUCT/factory/route-plans/T-190.json" >/dev/null
CURSOR_PROVIDER_PLAN="$TMP/sealed-cursor-provider-plan.json"
python3 "$SEALED_RELEASE/scripts/provider-concurrency-config.py" \
  --release "$SEALED_RELEASE" --root "$CURSOR_PROVIDER_ROOT" --capacity 2 plan \
  > "$CURSOR_PROVIDER_PLAN"
CURSOR_PROVIDER_APPROVAL="$(python3 -c \
  'import json,sys; print(json.load(open(sys.argv[1]))["approval_sha256"])' \
  "$CURSOR_PROVIDER_PLAN")"
python3 "$SEALED_RELEASE/scripts/provider-concurrency-config.py" \
  --release "$SEALED_RELEASE" --root "$CURSOR_PROVIDER_ROOT" --capacity 2 apply \
  --approve-hash "$CURSOR_PROVIDER_APPROVAL" >/dev/null
CURSOR_TRANSITION="$(env \
  FACTORY_CERTIFIED_PRODUCT_ORIGIN="$SEALED_ORIGIN" \
  FACTORY_RELEASE_SHA="$KIT_SHA" FACTORY_RELEASE_TREE="$SEALED_TREE" \
  FACTORY_RELEASE_PATH="$SEALED_RELEASE" \
  FACTORY_RELEASE_CONTRACT_VERSION=2.0.0 \
  python3 "$SEALED_RELEASE/scripts/state-machine.py" next \
    --factory-root "$SEALED_PRODUCT" --workdir "$SEALED_PRODUCT" \
    --kit-dir "$SEALED_RELEASE" --state-dir "$SEALED_STATE" \
    --ticket T-190 --contract-version 2.0.0 --factory-sha "$KIT_SHA" \
    --project sealed)"
CURSOR_RECEIPT="$(python3 -c \
  'import json,sys; print(json.load(sys.stdin)["receipt"])' \
  <<<"$CURSOR_TRANSITION")"
env FACTORY_CERTIFIED_PRODUCT_ORIGIN="$SEALED_ORIGIN" \
  python3 "$SEALED_RELEASE/scripts/state-machine.py" consume \
    --factory-root "$SEALED_PRODUCT" --workdir "$SEALED_PRODUCT" \
    --kit-dir "$SEALED_RELEASE" --state-dir "$SEALED_STATE" \
    --ticket T-190 --contract-version 2.0.0 --factory-sha "$KIT_SHA" \
    --project sealed --receipt "$CURSOR_RECEIPT" --role spec-linter >/dev/null
CURSOR_RUN_STATUS=0
CURSOR_RUN_OUTPUT="$TMP/sealed-cursor-run.out"
env \
  HOME="$CURSOR_HOME" PATH="$STUB_BIN:$PATH" \
  FACTORY_ROOT="$SEALED_PRODUCT" \
  FACTORY_CERTIFIED_PRODUCT_ORIGIN="$SEALED_ORIGIN" \
  FACTORY_PROJECT=sealed FACTORY_MODEL_STATE_ROOT="$CURSOR_MODEL_STATE" \
  FACTORY_TRANSITION_RECEIPT_SHA256="$CURSOR_RECEIPT" \
  FACTORY_TRANSITION_STATE_DIR="$SEALED_STATE" \
  FACTORY_GLOBAL_ENV="$CURSOR_GLOBAL" \
  FACTORY_PROVIDER_ACTIVATION="$CURSOR_PROVIDER_ROOT/isolated-v1.enabled" \
  FACTORY_PROVIDER_DB="$CURSOR_PROVIDER_ROOT/accounting/state-v2.sqlite3" \
  FACTORY_PROVIDER_POLICY="$CURSOR_PROVIDER_ROOT/provider-policy.json" \
  FACTORY_PROVIDER_CONFIGURATION_LOCK="$CURSOR_PROVIDER_ROOT/provider-configuration.lock" \
  FACTORY_CLI_RUNTIME_ROOT="$CURSOR_PROVIDER_ROOT/cli-runtimes" \
  FACTORY_CURSOR_ACCOUNT_DB="$CURSOR_ACCOUNT_ROOT/admission.sqlite3" \
  FACTORY_CURSOR_SESSION_HOME="$CURSOR_HOME" \
  FACTORY_KIT_TRUST_SCOPE=qualification-candidate \
  FACTORY_RELEASE_SHA="$KIT_SHA" FACTORY_RELEASE_TREE="$SEALED_TREE" \
  FACTORY_RELEASE_PATH="$SEALED_RELEASE" \
  FACTORY_RELEASE_CONTRACT_VERSION=2.0.0 \
  FACTORY_CURSOR_FALLBACK_ENABLED=1 CURSOR_AGENT_VERSION=2026.07.test \
  CURSOR_AGENT_BIN="$CURSOR_AGENT_WRAPPER" \
  FACTORY_PROBE_CURSOR_ANTHROPIC=READY:test \
  "$SEALED_RELEASE/scripts/run-agent.sh" \
    --role spec-linter --ticket T-190 -- "sealed Cursor account run" \
    >"$CURSOR_RUN_OUTPUT" 2>&1 || CURSOR_RUN_STATUS=$?
CURSOR_ACCOUNT_STATUS="$(python3 "$SEALED_RELEASE/scripts/provider-coordinator.py" \
  --db "$CURSOR_PROVIDER_ROOT/accounting/state-v2.sqlite3" \
  --account-db "$CURSOR_ACCOUNT_ROOT/admission.sqlite3" account-status)"
if [[ "$CURSOR_RUN_STATUS" -eq 0 ]] &&
   python3 - "$CURSOR_ACCOUNT_STATUS" <<'PY'
import json, sys
value = json.loads(sys.argv[1])
assert value["leases"] == []
assert len(value["starts"]) == 1
assert value["starts"][0]["account_route"] == "cursor"
PY
then
  pass "sealed Cursor run binds and releases shared account admission"
else
  fail "sealed Cursor run binds and releases shared account admission" \
    "run=$CURSOR_RUN_STATUS status=$CURSOR_ACCOUNT_STATUS output=$(tail -n 5 "$CURSOR_RUN_OUTPUT" | tr '\n' ' ')"
fi

FORGED_STAGE_STATUS=0
FORGED_STAGE="$(env \
  FACTORY_ROOT="$SEALED_PRODUCT" \
  FACTORY_RELEASE_SHA="$KIT_SHA" \
  FACTORY_RELEASE_TREE="$SEALED_TREE" \
  FACTORY_RELEASE_PATH="$TMP" \
  FACTORY_RELEASE_CONTRACT_VERSION=1.7.0 \
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

# Malformed or incoherent configuration fails before run registration, task
# submission, or runtime-ledger materialization.
INVALID_CONFIG_ROOT="$TMP/invalid-config"
write_envelope "$INVALID_CONFIG_ROOT"
write_ticket "$INVALID_CONFIG_ROOT" T-191
cp "$INVALID_CONFIG_ROOT/factory/ENVELOPE.env" "$TMP/invalid-config.clean"

assert_invalid_run_envelope() {
  local name="$1" replacement="$2" expected="$3" out status=0
  sed "$replacement" "$TMP/invalid-config.clean" > \
    "$INVALID_CONFIG_ROOT/factory/ENVELOPE.env"
  out="$(FACTORY_ROOT="$INVALID_CONFIG_ROOT" \
    FACTORY_GLOBAL_ENV="$TMP/no-global.env" FACTORY_ADAPTER_OVERRIDE=mock \
    "$RUN_AGENT" --role planner --ticket T-191 -- "invalid config" 2>&1)" ||
    status=$?
  if [[ "$status" -eq 3 && "$out" == *"$expected"* &&
        ! -d "$INVALID_CONFIG_ROOT/factory/runs" &&
        ! -f "$INVALID_CONFIG_ROOT/factory/runtime-ledger.csv" ]]; then
    pass "run-agent rejects $name config before task or manifest"
  else
    fail "run-agent rejects $name config before task or manifest" \
      "status=$status output=$out"
  fi
}

assert_invalid_run_envelope zero-money \
  's/^PER_RUN_BUDGET_USD=.*/PER_RUN_BUDGET_USD=0/' \
  'money values must be positive finite decimals'
assert_invalid_run_envelope zero-turns \
  's/^PER_RUN_MAX_TURNS=.*/PER_RUN_MAX_TURNS=0/' \
  'turns and timeout must be positive integers'
assert_invalid_run_envelope empty \
  's/^PER_RUN_BUDGET_USD=.*/PER_RUN_BUDGET_USD=/' \
  'money values must be positive finite decimals'
assert_invalid_run_envelope nan \
  's/^PER_RUN_BUDGET_USD=.*/PER_RUN_BUDGET_USD=NaN/' \
  'money values must be positive finite decimals'
assert_invalid_run_envelope negative-timeout \
  's/^PER_RUN_TIMEOUT_MIN=.*/PER_RUN_TIMEOUT_MIN=-1/' \
  'turns and timeout must be positive integers'
assert_invalid_run_envelope incoherent \
  's/^PER_RUN_BUDGET_USD=.*/PER_RUN_BUDGET_USD=30.00/' \
  'per-run budget exceeds a ticket or daily cap'
HUGE_CONFIG_VALUE="$(awk 'BEGIN { for (i=0; i<500; i++) printf "9" }')"
assert_invalid_run_envelope 500-digit-money \
  "s/^PER_RUN_BUDGET_USD=.*/PER_RUN_BUDGET_USD=$HUGE_CONFIG_VALUE/" \
  'money values must be positive finite decimals'
assert_invalid_run_envelope 500-digit-timeout \
  "s/^PER_RUN_TIMEOUT_MIN=.*/PER_RUN_TIMEOUT_MIN=$HUGE_CONFIG_VALUE/" \
  'turns and timeout must be positive integers'

cp "$TMP/invalid-config.clean" "$INVALID_CONFIG_ROOT/factory/ENVELOPE.env"
INVALID_GLOBAL="$TMP/invalid-global.env"
cat > "$INVALID_GLOBAL" <<'ENV'
GLOBAL_DAILY_CAP_USD=50.00
GLOBAL_LEDGER=relative-ledger.csv
CLAUDE_CODE_PINNED=2.1.207
CODEX_PINNED=0.144.1
FACTORY_CURSOR_FALLBACK_ENABLED=0
ENV
INVALID_GLOBAL_STATUS=0
INVALID_GLOBAL_OUT="$(FACTORY_ROOT="$INVALID_CONFIG_ROOT" \
  FACTORY_GLOBAL_ENV="$INVALID_GLOBAL" FACTORY_ADAPTER_OVERRIDE=mock \
  "$RUN_AGENT" --role planner --ticket T-191 -- "invalid global" 2>&1)" ||
  INVALID_GLOBAL_STATUS=$?
if [[ "$INVALID_GLOBAL_STATUS" -eq 3 &&
      "$INVALID_GLOBAL_OUT" == *"global config ledger path must be absolute"* &&
      ! -d "$INVALID_CONFIG_ROOT/factory/runs" &&
      ! -f "$INVALID_CONFIG_ROOT/factory/runtime-ledger.csv" ]]; then
  pass "run-agent rejects relative global ledger before task or manifest"
else
  fail "run-agent rejects relative global ledger before task or manifest" \
    "status=$INVALID_GLOBAL_STATUS output=$INVALID_GLOBAL_OUT"
fi

INVALID_PRICING="$TMP/invalid-pricing.env"
cat > "$INVALID_PRICING" <<ENV
GLOBAL_DAILY_CAP_USD=50.00
CURSOR_PRICING_SNAPSHOT_DATE=YYYY-MM-DD
CURSOR_OPENAI_USD_PER_MTOK_IN=$HUGE_CONFIG_VALUE
ENV
INVALID_PRICING_STATUS=0
INVALID_PRICING_OUT="$(FACTORY_ROOT="$INVALID_CONFIG_ROOT" \
  FACTORY_GLOBAL_ENV="$INVALID_PRICING" FACTORY_ADAPTER_OVERRIDE=mock \
  "$RUN_AGENT" --role planner --ticket T-191 -- "invalid pricing" 2>&1)" ||
  INVALID_PRICING_STATUS=$?
if [[ "$INVALID_PRICING_STATUS" -eq 3 &&
      "$INVALID_PRICING_OUT" == *"Cursor pricing requires"* &&
      ! -d "$INVALID_CONFIG_ROOT/factory/runs" ]]; then
  pass "run-agent rejects incomplete placeholder and oversized pricing before task"
else
  fail "run-agent rejects incomplete placeholder and oversized pricing before task" \
    "status=$INVALID_PRICING_STATUS output=$INVALID_PRICING_OUT"
fi

# The ignored projection is output-only: forged successes and negative costs
# are replaced before the sequencer counts roles or spend.
FORGED_VIEW="$TMP/forged-runtime-view"
write_envelope "$FORGED_VIEW"
write_ticket "$FORGED_VIEW" T-192
mkdir -p "$FORGED_VIEW/factory/runs"
ledger_header > "$FORGED_VIEW/factory/ledger.csv"
{
  ledger_header
  printf '2026-07-15,01:00:00,T-192,planner,mock,test,1,-1000,0,forged-negative,,,,,,\n'
  printf '2026-07-15,01:01:00,T-192,planner,mock,test,1,0,0,forged-success,,,,,,\n'
} > "$FORGED_VIEW/factory/runtime-ledger.csv"
FORGED_STAGE="$(FACTORY_ROOT="$FORGED_VIEW" "$NEXT_STAGE" --ticket T-192 2>&1)"
if [[ "$FORGED_STAGE" == "RUN planner" &&
      "$(wc -l < "$FORGED_VIEW/factory/runtime-ledger.csv" | tr -d ' ')" == "1" ]]; then
  pass "sequencer overwrites forged runtime cost and success rows before counting"
else
  fail "sequencer overwrites forged runtime cost and success rows before counting" \
    "output=$FORGED_STAGE"
fi

# Canonical ledger routing from a linked worktree.
MAIN="$TMP/main"
WT="$TMP/worktree"
mkdir -p "$MAIN/conformance"
write_envelope "$MAIN/conformance" no-git
git -C "$MAIN" init -q
git -C "$MAIN" add conformance/.gitignore \
  conformance/factory/ENVELOPE.env conformance/factory/KIT_PIN
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
OVERRIDE_STATUS=0
FACTORY_ROOT="$WT/conformance" FACTORY_LEDGER="$OVERRIDE" \
     FACTORY_GLOBAL_ENV="$TMP/no-global.env" FACTORY_TEST_MODE=1 \
     FACTORY_ADAPTER_OVERRIDE=mock \
     "$RUN_AGENT" --role planner --ticket T-201 -- "override" >/dev/null ||
  OVERRIDE_STATUS=$?
OVERRIDE_ROWS="$(awk -F, '$3=="T-201" {n++} END {print n+0}' "$OVERRIDE")"
CANONICAL_ROWS="$(awk -F, '$3=="T-201" {n++} END {print n+0}' \
  "$MAIN/conformance/factory/runtime-ledger.csv")"
if [[ "$OVERRIDE_STATUS" -eq 0 && "$OVERRIDE_ROWS" == "1" &&
      "$CANONICAL_ROWS" == "0" ]]; then
  pass "FACTORY_LEDGER override wins"
else
  fail "FACTORY_LEDGER override wins" \
    "status=$OVERRIDE_STATUS override_rows=$OVERRIDE_ROWS canonical_rows=$CANONICAL_ROWS"
fi

# A trusted lane override can request reduction from its own manifest root
# before stage selection instead of consuming a stale projected file.
REFRESH_OVERRIDE="$TMP/refresh-override"
write_envelope "$REFRESH_OVERRIDE"
write_ticket "$REFRESH_OVERRIDE" T-203
mkdir -p "$REFRESH_OVERRIDE/factory/runs"
{
  ledger_header
  ledger_row T-203 planner
} > "$REFRESH_OVERRIDE/factory/ledger.csv"
ledger_header > "$REFRESH_OVERRIDE/factory/runtime-ledger.csv"
REFRESH_STAGE="$(FACTORY_ROOT="$REFRESH_OVERRIDE" \
  FACTORY_LEDGER="$REFRESH_OVERRIDE/factory/runtime-ledger.csv" \
  FACTORY_REFRESH_RUNTIME_LEDGER=1 \
  "$NEXT_STAGE" --ticket T-203 2>&1)"
if [[ "$REFRESH_STAGE" == "RUN spec-linter" ]] &&
   [[ "$(awk -F, '$3=="T-203" {n++} END {print n+0}' \
       "$REFRESH_OVERRIDE/factory/runtime-ledger.csv")" == "1" ]]; then
  pass "trusted lane override refreshes its own runtime ledger"
else
  fail "trusted lane override refreshes its own runtime ledger" \
    "stage=$REFRESH_STAGE"
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
mkdir -p "$MOCK_GUARD/factory/runs"
MOCK_GUARD_STATUS=0
env -u FACTORY_TEST_MODE -u FACTORY_TRUSTED_TEST_HARNESS \
  FACTORY_ROOT="$MOCK_GUARD" FACTORY_GLOBAL_ENV="$TMP/no-global.env" \
  FACTORY_ADAPTER_OVERRIDE=mock \
  "$RUN_AGENT" --role planner --ticket T-204 -- "forbidden mock" >/dev/null 2>&1 ||
  MOCK_GUARD_STATUS=$?
if [[ "$MOCK_GUARD_STATUS" -eq 2 &&
      "$(wc -l < "$MOCK_GUARD/factory/ledger.csv")" -eq 1 ]]; then
  pass "mock override requires trusted test harness"
else
  fail "mock override requires trusted test harness" "status $MOCK_GUARD_STATUS"
fi

PROBE_GUARD_STATUS=0
PROBE_GUARD_OUT="$(env -u FACTORY_TEST_MODE -u FACTORY_TRUSTED_TEST_HARNESS \
  FACTORY_PROBE_CODEX=UNAVAILABLE:test \
  FACTORY_ROOT="$MOCK_GUARD" FACTORY_GLOBAL_ENV="$TMP/no-global.env" \
  "$RUN_AGENT" --role planner --ticket T-204 -- "forbidden probe" 2>&1)" ||
  PROBE_GUARD_STATUS=$?
if [[ "$PROBE_GUARD_STATUS" -eq 2 &&
      "$PROBE_GUARD_OUT" == *"trusted internal test harness"* &&
      "$(wc -l < "$MOCK_GUARD/factory/ledger.csv")" -eq 1 ]]; then
  pass "probe override requires trusted test harness"
else
  fail "probe override requires trusted test harness" \
    "status $PROBE_GUARD_STATUS: $PROBE_GUARD_OUT"
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
fi

if [[ "$SUBSET" == "runtime-routing" ]]; then
# Backend resolution: primary success submits exactly one primary task.
PRIMARY="$TMP/primary-route"
write_envelope "$PRIMARY"
write_ticket "$PRIMARY" T-210
PRIMARY_GLOBAL="$TMP/primary-global/global.env"
write_backend_global "$PRIMARY_GLOBAL"
PRIMARY_TRACE="$TMP/primary.trace"
PRIMARY_ARGS="$TMP/primary.args"
PRIMARY_OUT="$TMP/primary.out"
PRIMARY_STATUS=0
: > "$PRIMARY_TRACE"
: > "$PRIMARY_ARGS"
PATH="$STUB_BIN:$PATH" FACTORY_ROOT="$PRIMARY" \
  FACTORY_GLOBAL_ENV="$PRIMARY_GLOBAL" FACTORY_TEST_TRACE="$PRIMARY_TRACE" FACTORY_TEST_ARGS="$PRIMARY_ARGS" \
  "$RUN_AGENT" --role planner --ticket T-210 -- "primary route" \
  > "$PRIMARY_OUT" 2>&1 || PRIMARY_STATUS=$?
if [[ "$PRIMARY_STATUS" -eq 0 ]] &&
   [[ "$(awk -F, '$3=="T-210" {print $5}' "$PRIMARY/factory/runtime-ledger.csv")" == "codex" ]] &&
   [[ "$(awk -F, '$3=="T-210" {print $11}' "$PRIMARY/factory/runtime-ledger.csv")" == "openai" ]] &&
   [[ "$(awk -F, '$3=="T-210" {print $12}' "$PRIMARY/factory/runtime-ledger.csv")" == "gpt-5.6-sol" ]] &&
   [[ "$(awk -F, '$3=="T-210" {print $13}' "$PRIMARY/factory/runtime-ledger.csv")" == "primary_ready" ]] &&
   [[ "$(wc -l < "$PRIMARY_TRACE" | tr -d ' ')" == "1" ]] &&
   grep -q -- '-m gpt-5.6-sol -c model_reasoning_effort=high' "$PRIMARY_ARGS" &&
   grep -q '^codex-task$' "$PRIMARY_TRACE"; then
  pass "ready primary submits exactly one primary task"
else
  fail "ready primary submits exactly one primary task" "status $PRIMARY_STATUS"
  awk '{print "  | " $0}' "$PRIMARY_OUT" >&2
fi

# A ticket pin remains authoritative after profile activation changes. Its
# selected route is re-probed alone, and stable provenance stays manifest-only.
PROFILE_PLAN="$TMP/profile-plan.json"
PATH="$STUB_BIN:$PATH" FACTORY_CURSOR_FALLBACK_ENABLED=1 \
  CURSOR_AGENT_VERSION=2026.07.test \
  FACTORY_PROBE_CODEX=READY:test FACTORY_PROBE_CLAUDE_CODE=READY:test \
  FACTORY_PROBE_CURSOR_OPENAI=READY:test \
  FACTORY_PROBE_CURSOR_ANTHROPIC=READY:test \
  bash -c 'source "$1"; factory_resolve_model_profile claude-priority-v1 "$2"' \
    _ "$ROOT/scripts/lib/backend-policy.sh" "$PROFILE_PLAN"
PINNED="$TMP/pinned-route"
write_envelope "$PINNED"
write_ticket "$PINNED" T-219
PINNED_STATE="$TMP/pinned-state"
mkdir -p "$PINNED_STATE"
PINNED_PLAN="$PINNED/factory/route-plans/T-219.json"
python3 "$ROOT/scripts/model-manager.py" pin \
  --state-root "$PINNED_STATE" --project pinned-test \
  --ticket T-219 --kit-sha "$KIT_SHA" \
  --resolution-file "$PROFILE_PLAN" --output "$PINNED_PLAN" >/dev/null
LEGACY_HASH="$(python3 "$ROOT/scripts/model-manager.py" profiles \
  --state-root "$PINNED_STATE" --project pinned-test | python3 -c \
  'import json,sys; print(next(x["profile_hash"] for x in json.load(sys.stdin)["profiles"] if x["profile_id"]=="legacy-balanced-v1"))')"
python3 "$ROOT/scripts/model-manager.py" activate \
  --state-root "$PINNED_STATE" --project pinned-test \
  --profile legacy-balanced-v1 --approve-hash "$LEGACY_HASH" \
  --approved-by test >/dev/null
PINNED_DOWN_GLOBAL="$TMP/pinned-down-global/global.env"
write_backend_global "$PINNED_DOWN_GLOBAL" \
  "export FACTORY_PROBE_CLAUDE_CODE=UNAVAILABLE:pinned_outage"
PINNED_PROBE_TRACE="$TMP/pinned-probes.trace"
PINNED_TASK_TRACE="$TMP/pinned-task.trace"
: > "$PINNED_PROBE_TRACE"
: > "$PINNED_TASK_TRACE"
PINNED_DOWN_STATUS=0
PATH="$STUB_BIN:$PATH" FACTORY_ROOT="$PINNED" \
  FACTORY_GLOBAL_ENV="$PINNED_DOWN_GLOBAL" \
  FACTORY_MODEL_STATE_ROOT="$PINNED_STATE" FACTORY_PROJECT=pinned-test \
  FACTORY_PROBE_TRACE="$PINNED_PROBE_TRACE" \
  FACTORY_TEST_TRACE="$PINNED_TASK_TRACE" \
  "$RUN_AGENT" --role planner --ticket T-219 -- "pinned outage" >/dev/null 2>&1 ||
  PINNED_DOWN_STATUS=$?
if [[ "$PINNED_DOWN_STATUS" -eq 6 &&
      "$(cat "$PINNED_PROBE_TRACE")" == "claude-code|sonnet" &&
      ! -s "$PINNED_TASK_TRACE" ]] &&
   ! compgen -G "$PINNED/factory/runs/*.meta" >/dev/null; then
  pass "pinned outage stops without alternate probe, reservation, or task"
else
  fail "pinned outage stops without alternate probe, reservation, or task" \
    "status=$PINNED_DOWN_STATUS probes=$(cat "$PINNED_PROBE_TRACE")"
fi

PINNED_READY_GLOBAL="$TMP/pinned-ready-global/global.env"
write_backend_global "$PINNED_READY_GLOBAL" \
  "export FACTORY_PROBE_CLAUDE_CODE=READY:test"
: > "$PINNED_PROBE_TRACE"
: > "$PINNED_TASK_TRACE"
PINNED_STATUS=0
PATH="$STUB_BIN:$PATH" FACTORY_ROOT="$PINNED" \
  FACTORY_GLOBAL_ENV="$PINNED_READY_GLOBAL" \
  FACTORY_MODEL_STATE_ROOT="$PINNED_STATE" FACTORY_PROJECT=pinned-test \
  FACTORY_PROBE_TRACE="$PINNED_PROBE_TRACE" \
  FACTORY_TEST_TRACE="$PINNED_TASK_TRACE" \
  "$RUN_AGENT" --role planner --ticket T-219 -- "pinned ready" >/dev/null 2>&1 ||
  PINNED_STATUS=$?
PINNED_META="$(ls "$PINNED/factory/runs/"*.meta 2>/dev/null || true)"
PINNED_SHA="$(shasum -a 256 "$PINNED_PLAN" | awk '{print $1}')"
PINNED_POLICY_HASH="$(python3 -c \
  'import json,sys; print(json.load(open(sys.argv[1]))["resolution"]["policy_hash"])' \
  "$PINNED_PLAN")"
if [[ "$PINNED_STATUS" -eq 0 && -n "$PINNED_META" &&
      "$(cat "$PINNED_PROBE_TRACE")" == "claude-code|sonnet" ]] &&
   grep -q '^route_id=claude-sonnet$' "$PINNED_META" &&
   grep -q '^gateway_id=anthropic-claude-code$' "$PINNED_META" &&
   grep -q '^inference_provider_id=anthropic$' "$PINNED_META" &&
   grep -q '^account_route_id=claude-native$' "$PINNED_META" &&
   grep -q '^transport=native-cli$' "$PINNED_META" &&
   grep -q "^policy_hash=$PINNED_POLICY_HASH$" "$PINNED_META" &&
   grep -q "^route_plan_sha256=$PINNED_SHA$" "$PINNED_META" &&
   [[ "$(head -n1 "$PINNED/factory/runtime-ledger.csv")" == \
      "$(ledger_header)" ]]; then
  pass "profile changes do not affect pinned runs and manifests record provenance"
else
  fail "profile changes do not affect pinned runs and manifests record provenance" \
    "status=$PINNED_STATUS probes=$(cat "$PINNED_PROBE_TRACE")"
fi

# A non-task UNAVAILABLE probe selects family-matched Cursor before reservation.
FALLBACK="$TMP/fallback-route"
write_envelope "$FALLBACK"
write_ticket "$FALLBACK" T-211
FALLBACK_PRODUCT_TREE="$(git -C "$FALLBACK" rev-parse 'HEAD^{tree}')"
FALLBACK_GLOBAL="$TMP/fallback-global/global.env"
write_backend_global "$FALLBACK_GLOBAL" \
  $'export FACTORY_PROBE_CODEX=UNAVAILABLE:test_primary_down\nexport CURSOR_PRICING_SNAPSHOT_DATE=2026-07-15\nexport CURSOR_OPENAI_USD_PER_MTOK_IN=1.25\nexport CURSOR_OPENAI_USD_PER_MTOK_OUT=10\nexport CURSOR_ANTHROPIC_USD_PER_MTOK_IN=3\nexport CURSOR_ANTHROPIC_USD_PER_MTOK_OUT=15\nexport CURSOR_OPENAI_USD_PER_MTOK_CACHE=0\nexport CURSOR_ANTHROPIC_USD_PER_MTOK_CACHE=0'
FALLBACK_TRACE="$TMP/fallback.trace"
FALLBACK_ARGS="$TMP/fallback.args"
: > "$FALLBACK_TRACE"
: > "$FALLBACK_ARGS"
if PATH="$STUB_BIN:$PATH" FACTORY_ROOT="$FALLBACK" \
     FACTORY_GLOBAL_ENV="$FALLBACK_GLOBAL" FACTORY_TEST_TRACE="$FALLBACK_TRACE" \
     FACTORY_TEST_ARGS="$FALLBACK_ARGS" \
     "$LINKED_RUN_AGENT" --role planner --ticket T-211 -- "fallback route" >/dev/null &&
   [[ "$(awk -F, '$3=="T-211" {print $5}' "$FALLBACK/factory/runtime-ledger.csv")" == "cursor-openai" ]] &&
   [[ "$(awk -F, '$3=="T-211" {print $12}' "$FALLBACK/factory/runtime-ledger.csv")" == "gpt-5.6-sol-high" ]] &&
   [[ "$(awk -F, '$3=="T-211" {print $14}' "$FALLBACK/factory/runtime-ledger.csv")" == "conservative_reservation" ]] &&
   [[ "$(awk -F, '$3=="T-211" {print $8}' "$FALLBACK/factory/runtime-ledger.csv")" == "1.00" ]] &&
   [[ "$(wc -l < "$FALLBACK_TRACE" | tr -d ' ')" == "1" ]] &&
   grep -q -- '--model gpt-5.6-sol-high' "$FALLBACK_ARGS" &&
   tail -n 1 "$FALLBACK_ARGS" | grep -Fqx -- 'Cursor CLI control: stay in the default execution mode. Do not switch to Plan or Ask mode, invoke createPlan, or merely describe intended work. Execute the supplied role contract now while preserving its mutation limits.' &&
   grep -q '^cursor-task$' "$FALLBACK_TRACE"; then
  FALLBACK_OUT="$(ls "$FALLBACK/factory/runs/"*.out)"
  FALLBACK_META="$(ls "$FALLBACK/factory/runs/"*.meta)"
  if ! grep -qE 'supersecret|abc123|user:pass' "$FALLBACK_OUT" &&
     grep -q '\[REDACTED\]' "$FALLBACK_OUT" &&
     grep -q 'input_tokens=70' "$FALLBACK_OUT" &&
     grep -q 'cache_tokens=15' "$FALLBACK_OUT" &&
     grep -q '^phase=completed$' "$FALLBACK_META" &&
     grep -q '^task_submitted=1$' "$FALLBACK_META" &&
     grep -q "^kit_sha=$KIT_SHA$" "$FALLBACK_META" &&
     grep -q "^kit_tree=$KIT_TREE$" "$FALLBACK_META" &&
     grep -q "^product_tree=$FALLBACK_PRODUCT_TREE$" "$FALLBACK_META" &&
     grep -q "^ticket_kit_sha=$KIT_SHA$" "$FALLBACK_META" &&
     grep -q '^contract_version=2.0.0$' "$FALLBACK_META" &&
     grep -q "^physical_kit_path=$PHYSICAL_KIT_PATH$" "$FALLBACK_META"; then
    pass "unavailable primary selects one redacted Cursor task"
  else
    fail "unavailable primary selects one redacted Cursor task" \
      "artifact validation failed: out=$(sed -n '1,4p' "$FALLBACK_OUT" | tr '\n' '|') meta=$(grep -E '^(phase|task_submitted|kit_sha|kit_tree|product_tree|ticket_kit_sha|contract_version|physical_kit_path)=' "$FALLBACK_META" | tr '\n' '|')"
  fi
else
  fail "unavailable primary selects one redacted Cursor task" \
    "route validation failed: ledger=$(tail -n1 "$FALLBACK/factory/runtime-ledger.csv" 2>/dev/null) trace=$(tr '\n' '|' <"$FALLBACK_TRACE") args=$(tail -n2 "$FALLBACK_ARGS" | tr '\n' '|')"
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
      "$(wc -l < "$INVALID/factory/ledger.csv")" -eq 1 ]]; then
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
      "$(wc -l < "$WRONG_ROLE/factory/ledger.csv")" -eq 1 &&
      -d "$WRONG_ROLE/factory/runs" &&
      -z "$(find "$WRONG_ROLE/factory/runs" -name '*.meta' -print -quit)" ]] &&
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
  if [[ "$PIN_STATUS" -eq 3 &&
        "$(wc -l < "$PIN_ROOT/factory/ledger.csv")" -eq 1 &&
        ! -d "$PIN_ROOT/factory/runs" ]]; then
    pass "run-agent refuses $PIN_CASE kit pin before mutation"
  else
    fail "run-agent refuses $PIN_CASE kit pin before mutation" "status $PIN_STATUS"
  fi
done
fi

if [[ "$SUBSET" == "launch-controls" ]]; then
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
for _i in $(seq 1 500); do
  if [[ -n "$(ls "$STATE_GO/factory/runs/".*.ready 2>/dev/null || true)" ]] &&
     grep -q '^phase=prepared$' "$STATE_GO/factory/runs/"*.meta 2>/dev/null; then
    break
  fi
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
if [[ "$MAINT_STATUS" -eq 4 &&
      "$(wc -l < "$MAINT_ROOT/factory/ledger.csv")" -eq 1 &&
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
      "$(wc -l < "$AFTER_LOCK/factory/ledger.csv")" -eq 1 ]] &&
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
if [[ "$PIN_RACE_STATUS" -eq 3 &&
      "$(wc -l < "$PIN_RACE/factory/ledger.csv")" -eq 1 ]] &&
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
for _i in $(seq 1 500); do
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

# A kill published during final validation still wins before the adapter gate.
BEFORE_GATE_KILL="$TMP/kill-before-adapter-gate"
write_envelope "$BEFORE_GATE_KILL"
write_ticket "$BEFORE_GATE_KILL" T-299
FACTORY_ROOT="$BEFORE_GATE_KILL" FACTORY_GLOBAL_ENV="$TMP/no-global.env" \
  FACTORY_TEST_MODE=1 FACTORY_TEST_BEFORE_GATE_SLEEP=1 \
  FACTORY_ADAPTER_OVERRIDE=mock \
  "$RUN_AGENT" --role planner --ticket T-299 -- "before adapter gate" \
  > "$TMP/before-adapter-gate.out" 2>&1 &
BEFORE_GATE_KILL_PID=$!
for _i in $(seq 1 500); do
  [[ -n "$(ls "$BEFORE_GATE_KILL/factory/runs/".*.before-gate \
    2>/dev/null || true)" ]] && break
  sleep 0.02
done
touch "$BEFORE_GATE_KILL/factory/KILL"
wait "$BEFORE_GATE_KILL_PID"
BEFORE_GATE_KILL_STATUS=$?
if [[ "$BEFORE_GATE_KILL_STATUS" -eq 4 ]] &&
   grep -q 'KILL file appeared before adapter gate' \
     "$TMP/before-adapter-gate.out" &&
   ! grep -q 'mock adapter ran task' "$BEFORE_GATE_KILL/factory/runs/"*.out; then
  pass "kill before adapter gate prevents task submission"
else
  fail "kill before adapter gate prevents task submission" \
    "status $BEFORE_GATE_KILL_STATUS: $(tr '\n' ' ' < "$TMP/before-adapter-gate.out")"
fi

# A controller-observed stop still runs the shared integrity checks.
BEFORE_GATE_MUTATION="$TMP/controller-stop-plus-mutation"
write_envelope "$BEFORE_GATE_MUTATION"
write_ticket "$BEFORE_GATE_MUTATION" T-306
FACTORY_ROOT="$BEFORE_GATE_MUTATION" FACTORY_GLOBAL_ENV="$TMP/no-global.env" \
  FACTORY_TEST_MODE=1 FACTORY_TEST_BEFORE_GATE_SLEEP=1 \
  FACTORY_ADAPTER_OVERRIDE=mock \
  "$RUN_AGENT" --role planner --ticket T-306 -- "controller stop plus mutation" \
  > "$TMP/controller-stop-plus-mutation.out" 2>&1 &
BEFORE_GATE_MUTATION_PID=$!
for _i in $(seq 1 500); do
  [[ -n "$(ls "$BEFORE_GATE_MUTATION/factory/runs/".*.before-gate \
    2>/dev/null || true)" ]] && break
  sleep 0.02
done
touch "$BEFORE_GATE_MUTATION/factory/KILL"
touch "$BEFORE_GATE_MUTATION/unexpected-control-mutation"
wait "$BEFORE_GATE_MUTATION_PID"
BEFORE_GATE_MUTATION_STATUS=$?
if [[ "$BEFORE_GATE_MUTATION_STATUS" -eq 11 ]] &&
   grep -q 'registered checkout changed during provider execution' \
     "$TMP/controller-stop-plus-mutation.out" &&
   ! grep -q 'mock adapter ran task' \
     "$BEFORE_GATE_MUTATION/factory/runs/"*.out; then
  pass "controller boundary stop cannot mask checkout mutation"
else
  fail "controller boundary stop cannot mask checkout mutation" \
    "status $BEFORE_GATE_MUTATION_STATUS"
fi

# Maintenance published during final validation also wins before submission.
BEFORE_GATE_MAINT="$TMP/maintenance-before-adapter-gate"
write_envelope "$BEFORE_GATE_MAINT"
write_ticket "$BEFORE_GATE_MAINT" T-300
FACTORY_ROOT="$BEFORE_GATE_MAINT" FACTORY_GLOBAL_ENV="$TMP/no-global.env" \
  FACTORY_TEST_MODE=1 FACTORY_TEST_BEFORE_GATE_SLEEP=1 \
  FACTORY_ADAPTER_OVERRIDE=mock \
  "$RUN_AGENT" --role planner --ticket T-300 -- "maintenance before adapter gate" \
  > "$TMP/maintenance-before-adapter-gate.out" 2>&1 &
BEFORE_GATE_MAINT_PID=$!
for _i in $(seq 1 500); do
  [[ -n "$(ls "$BEFORE_GATE_MAINT/factory/runs/".*.before-gate \
    2>/dev/null || true)" ]] && break
  sleep 0.02
done
touch "$BEFORE_GATE_MAINT/factory/MAINTENANCE"
wait "$BEFORE_GATE_MAINT_PID"
BEFORE_GATE_MAINT_STATUS=$?
if [[ "$BEFORE_GATE_MAINT_STATUS" -eq 4 ]] &&
   grep -q 'MAINTENANCE file appeared before adapter gate' \
     "$TMP/maintenance-before-adapter-gate.out" &&
   ! grep -q 'mock adapter ran task' \
     "$BEFORE_GATE_MAINT/factory/runs/"*.out; then
  pass "maintenance before adapter gate prevents task submission"
else
  fail "maintenance before adapter gate prevents task submission" \
    "status $BEFORE_GATE_MAINT_STATUS: $(tr '\n' ' ' < "$TMP/maintenance-before-adapter-gate.out")"
fi

# A valid targeted cancellation during final validation prevents submission.
BEFORE_GATE_CANCEL="$TMP/cancel-before-adapter-gate"
write_envelope "$BEFORE_GATE_CANCEL"
write_ticket "$BEFORE_GATE_CANCEL" T-301
FACTORY_ROOT="$BEFORE_GATE_CANCEL" FACTORY_GLOBAL_ENV="$TMP/no-global.env" \
  FACTORY_TEST_MODE=1 FACTORY_TEST_BEFORE_GATE_SLEEP=30 \
  FACTORY_ADAPTER_OVERRIDE=mock \
  "$RUN_AGENT" --role planner --ticket T-301 -- "cancel before adapter gate" \
  > "$TMP/cancel-before-adapter-gate.out" 2>&1 &
BEFORE_GATE_CANCEL_PID=$!
BEFORE_GATE_CANCEL_RUN=""
for _i in $(seq 1 500); do
  BEFORE_GATE_CANCEL_META="$(ls "$BEFORE_GATE_CANCEL/factory/runs/"*.meta \
    2>/dev/null || true)"
  if [[ -n "$BEFORE_GATE_CANCEL_META" &&
        -n "$(ls "$BEFORE_GATE_CANCEL/factory/runs/".*.before-gate \
          2>/dev/null || true)" ]]; then
    BEFORE_GATE_CANCEL_RUN="$(basename "$BEFORE_GATE_CANCEL_META" .meta)"
    break
  fi
  sleep 0.02
done
BEFORE_GATE_CANCEL_PLAN="$TMP/cancel-before-adapter-gate-plan.json"
python3 "$ATTEMPT_CANCEL" preview --factory-root "$BEFORE_GATE_CANCEL" \
  --ticket T-301 --run-id "$BEFORE_GATE_CANCEL_RUN" \
  --reason operator_requested > "$BEFORE_GATE_CANCEL_PLAN"
BEFORE_GATE_CANCEL_HASH="$(python3 -c \
  'import json,sys; print(json.load(open(sys.argv[1]))["preview_hash"])' \
  "$BEFORE_GATE_CANCEL_PLAN")"
python3 "$ATTEMPT_CANCEL" apply --factory-root "$BEFORE_GATE_CANCEL" \
  --plan "$BEFORE_GATE_CANCEL_PLAN" \
  --preview-hash "$BEFORE_GATE_CANCEL_HASH" --timeout 30 \
  > "$TMP/cancel-before-adapter-gate-receipt.json"
wait "$BEFORE_GATE_CANCEL_PID" 2>/dev/null || true
if grep -q 'targeted cancellation requested before adapter gate' \
     "$TMP/cancel-before-adapter-gate.out" &&
   grep -q '^role_exit=cancelled$' \
     "$BEFORE_GATE_CANCEL/factory/runs/$BEFORE_GATE_CANCEL_RUN.meta" &&
   grep -q '^task_submitted=0$' \
     "$BEFORE_GATE_CANCEL/factory/runs/$BEFORE_GATE_CANCEL_RUN.meta" &&
   ! grep -q 'mock adapter ran task' \
     "$BEFORE_GATE_CANCEL/factory/runs/"*.out; then
  pass "targeted cancellation before adapter gate prevents task submission"
else
  fail "targeted cancellation before adapter gate prevents task submission"
fi

# Control-plane mutation outranks a simultaneously valid cancellation.
BEFORE_GATE_CANCEL_MUTATION="$TMP/cancel-plus-mutation-before-adapter-gate"
write_envelope "$BEFORE_GATE_CANCEL_MUTATION"
write_ticket "$BEFORE_GATE_CANCEL_MUTATION" T-307
FACTORY_ROOT="$BEFORE_GATE_CANCEL_MUTATION" \
  FACTORY_GLOBAL_ENV="$TMP/no-global.env" \
  FACTORY_TEST_MODE=1 FACTORY_TEST_BEFORE_GATE_SLEEP=30 \
  FACTORY_ADAPTER_OVERRIDE=mock \
  "$RUN_AGENT" --role planner --ticket T-307 -- "cancel plus mutation" \
  > "$TMP/cancel-plus-mutation-before-adapter-gate.out" 2>&1 &
BEFORE_GATE_CANCEL_MUTATION_PID=$!
BEFORE_GATE_CANCEL_MUTATION_RUN=""
for _i in $(seq 1 500); do
  BEFORE_GATE_CANCEL_MUTATION_META="$(ls \
    "$BEFORE_GATE_CANCEL_MUTATION/factory/runs/"*.meta 2>/dev/null || true)"
  if [[ -n "$BEFORE_GATE_CANCEL_MUTATION_META" &&
        -n "$(ls \
          "$BEFORE_GATE_CANCEL_MUTATION/factory/runs/".*.before-gate \
          2>/dev/null || true)" ]]; then
    BEFORE_GATE_CANCEL_MUTATION_RUN="$(
      basename "$BEFORE_GATE_CANCEL_MUTATION_META" .meta
    )"
    break
  fi
  sleep 0.02
done
BEFORE_GATE_CANCEL_MUTATION_PLAN="$TMP/cancel-plus-mutation-plan.json"
python3 "$ATTEMPT_CANCEL" preview \
  --factory-root "$BEFORE_GATE_CANCEL_MUTATION" \
  --ticket T-307 --run-id "$BEFORE_GATE_CANCEL_MUTATION_RUN" \
  --reason operator_requested > "$BEFORE_GATE_CANCEL_MUTATION_PLAN"
BEFORE_GATE_CANCEL_MUTATION_HASH="$(python3 -c \
  'import json,sys; print(json.load(open(sys.argv[1]))["preview_hash"])' \
  "$BEFORE_GATE_CANCEL_MUTATION_PLAN")"
touch "$BEFORE_GATE_CANCEL_MUTATION/unexpected-control-mutation"
BEFORE_GATE_CANCEL_MUTATION_APPLY=0
python3 "$ATTEMPT_CANCEL" apply \
  --factory-root "$BEFORE_GATE_CANCEL_MUTATION" \
  --plan "$BEFORE_GATE_CANCEL_MUTATION_PLAN" \
  --preview-hash "$BEFORE_GATE_CANCEL_MUTATION_HASH" --timeout 30 \
  > "$TMP/cancel-plus-mutation-receipt.json" 2>/dev/null ||
  BEFORE_GATE_CANCEL_MUTATION_APPLY=$?
wait "$BEFORE_GATE_CANCEL_MUTATION_PID" 2>/dev/null || true
if [[ "$BEFORE_GATE_CANCEL_MUTATION_APPLY" -ne 0 ]] &&
   grep -q '^role_exit=role_exit_control_plane_mutation$' \
     "$BEFORE_GATE_CANCEL_MUTATION/factory/runs/$BEFORE_GATE_CANCEL_MUTATION_RUN.meta" &&
   [[ ! -e "$BEFORE_GATE_CANCEL_MUTATION/factory/runs/$BEFORE_GATE_CANCEL_MUTATION_RUN.cancel.json" ]] &&
   ! grep -q 'mock adapter ran task' \
     "$BEFORE_GATE_CANCEL_MUTATION/factory/runs/"*.out; then
  pass "control-plane mutation outranks boundary cancellation"
else
  fail "control-plane mutation outranks boundary cancellation" \
    "apply status $BEFORE_GATE_CANCEL_MUTATION_APPLY"
fi

# Malformed targeted cancellation remains a hard pre-submission failure.
BEFORE_GATE_BAD_CANCEL="$TMP/malformed-cancel-before-adapter-gate"
write_envelope "$BEFORE_GATE_BAD_CANCEL"
write_ticket "$BEFORE_GATE_BAD_CANCEL" T-302
FACTORY_ROOT="$BEFORE_GATE_BAD_CANCEL" FACTORY_GLOBAL_ENV="$TMP/no-global.env" \
  FACTORY_TEST_MODE=1 FACTORY_TEST_BEFORE_GATE_SLEEP=1 \
  FACTORY_ADAPTER_OVERRIDE=mock \
  "$RUN_AGENT" --role planner --ticket T-302 -- "bad cancel before adapter gate" \
  > "$TMP/malformed-cancel-before-adapter-gate.out" 2>&1 &
BEFORE_GATE_BAD_CANCEL_PID=$!
BEFORE_GATE_BAD_CANCEL_RUN=""
for _i in $(seq 1 500); do
  BEFORE_GATE_BAD_CANCEL_META="$(ls \
    "$BEFORE_GATE_BAD_CANCEL/factory/runs/"*.meta 2>/dev/null || true)"
  if [[ -n "$BEFORE_GATE_BAD_CANCEL_META" &&
        -n "$(ls "$BEFORE_GATE_BAD_CANCEL/factory/runs/".*.before-gate \
          2>/dev/null || true)" ]]; then
    BEFORE_GATE_BAD_CANCEL_RUN="$(basename "$BEFORE_GATE_BAD_CANCEL_META" .meta)"
    break
  fi
  sleep 0.02
done
printf '{\n' > "$BEFORE_GATE_BAD_CANCEL/factory/runs/$BEFORE_GATE_BAD_CANCEL_RUN.cancel-request.json"
wait "$BEFORE_GATE_BAD_CANCEL_PID"
BEFORE_GATE_BAD_CANCEL_STATUS=$?
if [[ "$BEFORE_GATE_BAD_CANCEL_STATUS" -eq 11 ]] &&
   grep -q 'malformed targeted cancellation request before adapter gate' \
     "$TMP/malformed-cancel-before-adapter-gate.out" &&
   ! grep -q 'mock adapter ran task' \
     "$BEFORE_GATE_BAD_CANCEL/factory/runs/"*.out; then
  pass "malformed cancellation before adapter gate fails closed"
else
  fail "malformed cancellation before adapter gate fails closed" \
    "status $BEFORE_GATE_BAD_CANCEL_STATUS"
fi

# The isolated wrapper rechecks controls after observing the gate and before
# spawning the adapter, which closes the controller-check-to-gate boundary.
AT_GATE_KILL="$TMP/kill-at-adapter-boundary"
write_envelope "$AT_GATE_KILL"
write_ticket "$AT_GATE_KILL" T-303
FACTORY_ROOT="$AT_GATE_KILL" FACTORY_GLOBAL_ENV="$TMP/no-global.env" \
  FACTORY_TEST_MODE=1 FACTORY_TEST_AFTER_GATE_SLEEP=1 \
  FACTORY_ADAPTER_OVERRIDE=mock \
  "$RUN_AGENT" --role planner --ticket T-303 -- "kill at adapter boundary" \
  > "$TMP/kill-at-adapter-boundary.out" 2>&1 &
AT_GATE_KILL_PID=$!
for _i in $(seq 1 500); do
  [[ -n "$(ls "$AT_GATE_KILL/factory/runs/".*.gate 2>/dev/null || true)" ]] &&
    break
  sleep 0.02
done
touch "$AT_GATE_KILL/factory/KILL"
wait "$AT_GATE_KILL_PID"
AT_GATE_KILL_STATUS=$?
if [[ "$AT_GATE_KILL_STATUS" -eq 4 ]] &&
   grep -q 'control stop appeared at adapter submission boundary' \
     "$TMP/kill-at-adapter-boundary.out" &&
   grep -q 'KILL file appeared before adapter gate' \
     "$TMP/kill-at-adapter-boundary.out" &&
   ! grep -q 'mock adapter ran task' "$AT_GATE_KILL/factory/runs/"*.out; then
  pass "kill at adapter boundary prevents task submission"
else
  fail "kill at adapter boundary prevents task submission" \
    "status $AT_GATE_KILL_STATUS"
fi

# A legitimate boundary stop exempts only its exact control record; unrelated
# checkout mutation remains an integrity failure.
AT_GATE_MUTATION="$TMP/stop-plus-mutation-at-adapter-boundary"
write_envelope "$AT_GATE_MUTATION"
write_ticket "$AT_GATE_MUTATION" T-304
FACTORY_ROOT="$AT_GATE_MUTATION" FACTORY_GLOBAL_ENV="$TMP/no-global.env" \
  FACTORY_TEST_MODE=1 FACTORY_TEST_AFTER_GATE_SLEEP=1 \
  FACTORY_ADAPTER_OVERRIDE=mock \
  "$RUN_AGENT" --role planner --ticket T-304 -- "stop plus mutation" \
  > "$TMP/stop-plus-mutation-at-adapter-boundary.out" 2>&1 &
AT_GATE_MUTATION_PID=$!
for _i in $(seq 1 500); do
  [[ -n "$(ls "$AT_GATE_MUTATION/factory/runs/".*.gate \
    2>/dev/null || true)" ]] && break
  sleep 0.02
done
touch "$AT_GATE_MUTATION/factory/KILL"
touch "$AT_GATE_MUTATION/unexpected-control-mutation"
wait "$AT_GATE_MUTATION_PID"
AT_GATE_MUTATION_STATUS=$?
if [[ "$AT_GATE_MUTATION_STATUS" -eq 11 ]] &&
   grep -q 'registered checkout changed during provider execution' \
     "$TMP/stop-plus-mutation-at-adapter-boundary.out" &&
   ! grep -q 'mock adapter ran task' "$AT_GATE_MUTATION/factory/runs/"*.out; then
  pass "boundary stop cannot mask unrelated checkout mutation"
else
  fail "boundary stop cannot mask unrelated checkout mutation" \
    "status $AT_GATE_MUTATION_STATUS"
fi

# Only the selected stop record is exempt; a concurrent second control remains
# visible to checkout-integrity validation.
AT_GATE_CONTROLS="$TMP/concurrent-controls-at-adapter-boundary"
write_envelope "$AT_GATE_CONTROLS"
write_ticket "$AT_GATE_CONTROLS" T-305
FACTORY_ROOT="$AT_GATE_CONTROLS" FACTORY_GLOBAL_ENV="$TMP/no-global.env" \
  FACTORY_TEST_MODE=1 FACTORY_TEST_AFTER_GATE_SLEEP=1 \
  FACTORY_ADAPTER_OVERRIDE=mock \
  "$RUN_AGENT" --role planner --ticket T-305 -- "concurrent controls" \
  > "$TMP/concurrent-controls-at-adapter-boundary.out" 2>&1 &
AT_GATE_CONTROLS_PID=$!
for _i in $(seq 1 500); do
  [[ -n "$(ls "$AT_GATE_CONTROLS/factory/runs/".*.gate \
    2>/dev/null || true)" ]] && break
  sleep 0.02
done
touch "$AT_GATE_CONTROLS/factory/KILL"
touch "$AT_GATE_CONTROLS/factory/MAINTENANCE"
wait "$AT_GATE_CONTROLS_PID"
AT_GATE_CONTROLS_STATUS=$?
if [[ "$AT_GATE_CONTROLS_STATUS" -eq 11 ]] &&
   grep -q 'registered checkout changed during provider execution' \
     "$TMP/concurrent-controls-at-adapter-boundary.out" &&
   ! grep -q 'mock adapter ran task' "$AT_GATE_CONTROLS/factory/runs/"*.out; then
  pass "boundary stop exempts only its exact control record"
else
  fail "boundary stop exempts only its exact control record" \
    "status $AT_GATE_CONTROLS_STATUS"
fi
fi

if [[ "$SUBSET" == "runtime-routing" ]]; then
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
   grep -q '^go_issued=1$' "$GO_WRITE_META" &&
   grep -q '^task_submitted=0$' "$GO_WRITE_META" &&
   grep -q '^accounting_state=abandoned_conservative$' "$GO_WRITE_META" &&
   grep -q '^effective_cost=1.00$' "$GO_WRITE_META" &&
   grep -q '^terminal_reason_code=adapter_submission_unconfirmed$' \
     "$GO_WRITE_META"; then
  pass "GO attempt stays charged when marker persistence keeps gate closed"
else
  fail "GO attempt stays charged when marker persistence keeps gate closed" \
    "status $GO_WRITE_STATUS"
fi

# Provider processes cannot add or replace launcher-owned accounting manifests.
FORGED_META_ROOT="$TMP/forged-run-manifest"
write_envelope "$FORGED_META_ROOT"
write_ticket "$FORGED_META_ROOT" T-228
FORGED_META_STATUS=0
FACTORY_ROOT="$FORGED_META_ROOT" FACTORY_GLOBAL_ENV="$TMP/no-global.env" \
  FACTORY_TEST_MODE=1 FACTORY_TEST_REQUIRE_DURABLE_GO=1 \
  FACTORY_ADAPTER_OVERRIDE=mock MOCK_FORGE_MANIFEST=1 \
  "$RUN_AGENT" --role planner --ticket T-228 -- "forge manifest" \
  > "$TMP/forged-run-manifest.out" 2>&1 || FORGED_META_STATUS=$?
FORGED_META_OWN="$(ls "$FORGED_META_ROOT/factory/runs/"*.meta)"
if [[ "$FORGED_META_STATUS" -eq 11 &&
      "$(awk -F, '$3=="T-228" {print $8":"$9}' "$FORGED_META_ROOT/factory/runtime-ledger.csv")" == "0.42:11" &&
      ! -e "$FORGED_META_ROOT/factory/runs/forged.meta" &&
      -n "$(find "$FORGED_META_ROOT/factory/runs" -name 'forged.meta.rejected-role-mutation-*' -print -quit)" &&
      "$(sed -n 's/^role_exit=//p' "$FORGED_META_OWN")" == "role_exit_control_plane_mutation" ]]; then
  pass "provider manifest forgery is quarantined and actual cost retained"
else
  fail "provider manifest forgery is quarantined and actual cost retained" \
    "status=$FORGED_META_STATUS"
fi

REGISTERED_MUTATION_ROOT="$TMP/registered-main-mutation"
write_envelope "$REGISTERED_MUTATION_ROOT"
write_ticket "$REGISTERED_MUTATION_ROOT" T-229
REGISTERED_MUTATION_STATUS=0
FACTORY_ROOT="$REGISTERED_MUTATION_ROOT" FACTORY_GLOBAL_ENV="$TMP/no-global.env" \
  FACTORY_TEST_MODE=1 FACTORY_ADAPTER_OVERRIDE=mock \
  MOCK_MUTATE_REGISTERED_MAIN=1 \
  "$RUN_AGENT" --role planner --ticket T-229 -- "mutate registered main" \
  > "$TMP/registered-main-mutation.out" 2>&1 || REGISTERED_MUTATION_STATUS=$?
REGISTERED_MUTATION_META="$(ls "$REGISTERED_MUTATION_ROOT/factory/runs/"*.meta)"
if [[ "$REGISTERED_MUTATION_STATUS" -eq 11 &&
      "$(awk -F, '$3=="T-229" {print $8":"$9}' "$REGISTERED_MUTATION_ROOT/factory/runtime-ledger.csv")" == "0.42:11" &&
      "$(sed -n 's/^role_exit=//p' "$REGISTERED_MUTATION_META")" == "role_exit_control_plane_mutation" ]]; then
  pass "registered checkout mutation blocks advancement and retains actual cost"
else
  fail "registered checkout mutation blocks advancement and retains actual cost" \
    "status=$REGISTERED_MUTATION_STATUS"
fi

REGISTERED_UNTRACKED_ROOT="$TMP/registered-main-untracked-mutation"
write_envelope "$REGISTERED_UNTRACKED_ROOT"
write_ticket "$REGISTERED_UNTRACKED_ROOT" T-239
REGISTERED_UNTRACKED_STATUS=0
FACTORY_ROOT="$REGISTERED_UNTRACKED_ROOT" FACTORY_GLOBAL_ENV="$TMP/no-global.env" \
  FACTORY_TEST_MODE=1 FACTORY_ADAPTER_OVERRIDE=mock \
  MOCK_MUTATE_REGISTERED_UNTRACKED=1 \
  "$RUN_AGENT" --role planner --ticket T-239 -- "mutate registered main untracked" \
  > "$TMP/registered-main-untracked-mutation.out" 2>&1 || REGISTERED_UNTRACKED_STATUS=$?
if [[ "$REGISTERED_UNTRACKED_STATUS" -eq 11 &&
      -f "$REGISTERED_UNTRACKED_ROOT/provider-untracked.txt" ]]; then
  pass "registered checkout detects untracked mutation despite Git config"
else
  fail "registered checkout detects untracked mutation despite Git config" \
    "status=$REGISTERED_UNTRACKED_STATUS"
fi

DIRTY_CONTENT_ROOT="$TMP/registered-dirty-content-mutation"
write_envelope "$DIRTY_CONTENT_ROOT"
write_ticket "$DIRTY_CONTENT_ROOT" T-230
git -C "$DIRTY_CONTENT_ROOT" add factory/tickets/T-230.md
git -C "$DIRTY_CONTENT_ROOT" -c user.name=test -c user.email=test@example.com \
  commit -qm "track ticket"
DIRTY_CONTENT_STATUS=0
FACTORY_ROOT="$DIRTY_CONTENT_ROOT" FACTORY_GLOBAL_ENV="$TMP/no-global.env" \
  FACTORY_TEST_MODE=1 FACTORY_ADAPTER_OVERRIDE=mock \
  MOCK_MUTATE_DIRTY_TICKET=1 MOCK_DIRTY_TICKET_ID=T-230 \
  "$RUN_AGENT" --role planner --ticket T-230 -- "mutate dirty ticket" \
  > "$TMP/registered-dirty-content-mutation.out" 2>&1 || DIRTY_CONTENT_STATUS=$?
if [[ "$DIRTY_CONTENT_STATUS" -eq 11 &&
      "$(awk -F, '$3=="T-230" {print $8":"$9}' "$DIRTY_CONTENT_ROOT/factory/runtime-ledger.csv")" == "0.42:11" ]]; then
  pass "same-status registered content replacement fails closed"
else
  fail "same-status registered content replacement fails closed" \
    "status=$DIRTY_CONTENT_STATUS"
fi

OUTPUT_BIND_ROOT="$TMP/output-binding"
write_envelope "$OUTPUT_BIND_ROOT"
write_ticket "$OUTPUT_BIND_ROOT" T-231
OUTPUT_BIND_STATUS=0
FACTORY_ROOT="$OUTPUT_BIND_ROOT" FACTORY_GLOBAL_ENV="$TMP/no-global.env" \
  FACTORY_TEST_MODE=1 FACTORY_ADAPTER_OVERRIDE=mock MOCK_FORGE_OUTPUT_PATH=1 \
  "$RUN_AGENT" --role planner --ticket T-231 -- "forge output path" \
  > "$TMP/output-binding.out" 2>&1 || OUTPUT_BIND_STATUS=$?
if [[ "$OUTPUT_BIND_STATUS" -eq 0 &&
      "$(awk -F, '$3=="T-231" {print $7":"$8}' "$OUTPUT_BIND_ROOT/factory/runtime-ledger.csv")" == "3:0.42" ]]; then
  pass "accounting reads wrapper-bound output rather than provider pathname"
else
  fail "accounting reads wrapper-bound output rather than provider pathname" \
    "status=$OUTPUT_BIND_STATUS"
fi

OUTPUT_SYMLINK_ROOT="$TMP/output-symlink"
write_envelope "$OUTPUT_SYMLINK_ROOT"
write_ticket "$OUTPUT_SYMLINK_ROOT" T-240
OUTPUT_SYMLINK_TARGET="$TMP/output-symlink-target"
printf 'untouched\n' > "$OUTPUT_SYMLINK_TARGET"
OUTPUT_SYMLINK_STATUS=0
FACTORY_ROOT="$OUTPUT_SYMLINK_ROOT" FACTORY_GLOBAL_ENV="$TMP/no-global.env" \
  FACTORY_TEST_MODE=1 FACTORY_ADAPTER_OVERRIDE=mock \
  MOCK_SYMLINK_OUTPUT_TARGET="$OUTPUT_SYMLINK_TARGET" \
  "$RUN_AGENT" --role planner --ticket T-240 -- "symlink output path" \
  > "$TMP/output-symlink.out" 2>&1 || OUTPUT_SYMLINK_STATUS=$?
OUTPUT_SYMLINK_PUBLISHED="$(find "$OUTPUT_SYMLINK_ROOT/factory/runs" -name '*.out' -print -quit)"
if [[ "$OUTPUT_SYMLINK_STATUS" -eq 0 && "$(cat "$OUTPUT_SYMLINK_TARGET")" == "untouched" &&
      -f "$OUTPUT_SYMLINK_PUBLISHED" && ! -L "$OUTPUT_SYMLINK_PUBLISHED" ]]; then
  pass "output publication atomically replaces provider symlink"
else
  fail "output publication atomically replaces provider symlink" \
    "status=$OUTPUT_SYMLINK_STATUS"
fi

for metric_case in huge-cost huge-turns; do
  METRIC_ROOT="$TMP/invalid-telemetry-$metric_case"
  write_envelope "$METRIC_ROOT"
  write_ticket "$METRIC_ROOT" T-232
  HUGE_TELEMETRY="$(awk 'BEGIN { for (i=0; i<500; i++) printf "9" }')"
  if [[ "$metric_case" == "huge-cost" ]]; then
    RAW_METRICS="turns=3 cost_usd=$HUGE_TELEMETRY"
  else
    RAW_METRICS="turns=$HUGE_TELEMETRY cost_usd=0.01"
  fi
  METRIC_STATUS=0
  FACTORY_ROOT="$METRIC_ROOT" FACTORY_GLOBAL_ENV="$TMP/no-global.env" \
    FACTORY_TEST_MODE=1 FACTORY_ADAPTER_OVERRIDE=mock MOCK_RAW_METRICS="$RAW_METRICS" \
    "$RUN_AGENT" --role planner --ticket T-232 -- "$metric_case" \
    > "$TMP/invalid-telemetry-$metric_case.out" 2>&1 || METRIC_STATUS=$?
  if [[ "$METRIC_STATUS" -eq 0 &&
        "$(awk -F, '$3=="T-232" {print $7":"$8":"$14}' "$METRIC_ROOT/factory/runtime-ledger.csv")" == "0:1.00:conservative_reservation" ]]; then
    pass "$metric_case telemetry retains full reservation and zero turns"
  else
    fail "$metric_case telemetry retains full reservation and zero turns" \
      "status=$METRIC_STATUS"
  fi
done

MISSING_TURNS_ROOT="$TMP/missing-turns-telemetry"
write_envelope "$MISSING_TURNS_ROOT"
write_ticket "$MISSING_TURNS_ROOT" T-236
MISSING_TURNS_STATUS=0
FACTORY_ROOT="$MISSING_TURNS_ROOT" FACTORY_GLOBAL_ENV="$TMP/no-global.env" \
  FACTORY_TEST_MODE=1 FACTORY_ADAPTER_OVERRIDE=mock \
  MOCK_RAW_METRICS="cost_usd=0.21" \
  "$RUN_AGENT" --role planner --ticket T-236 -- "missing turns" \
  > "$TMP/missing-turns-telemetry.out" 2>&1 || MISSING_TURNS_STATUS=$?
if [[ "$MISSING_TURNS_STATUS" -eq 0 &&
      "$(awk -F, '$3=="T-236" {print $7":"$8}' "$MISSING_TURNS_ROOT/factory/runtime-ledger.csv")" == "0:0.21" ]]; then
  pass "known cost is retained when optional turn telemetry is absent"
else
  fail "known cost is retained when optional turn telemetry is absent" \
    "status=$MISSING_TURNS_STATUS"
fi

CLAIM_MUTATION_ROOT="$TMP/claim-replacement"
write_envelope "$CLAIM_MUTATION_ROOT"
write_ticket "$CLAIM_MUTATION_ROOT" T-233
CLAIM_MUTATION_STATUS=0
FACTORY_ROOT="$CLAIM_MUTATION_ROOT" FACTORY_GLOBAL_ENV="$TMP/no-global.env" \
  FACTORY_TEST_MODE=1 FACTORY_ADAPTER_OVERRIDE=mock MOCK_REPLACE_ACTIVE_CLAIM=1 \
  "$RUN_AGENT" --role planner --ticket T-233 -- "replace claim" \
  > "$TMP/claim-replacement.out" 2>&1 || CLAIM_MUTATION_STATUS=$?
CLAIM_SUCCESSOR="$CLAIM_MUTATION_ROOT/factory/.active-runs/T-233.planner.lock/owner"
if [[ "$CLAIM_MUTATION_STATUS" -eq 11 && -f "$CLAIM_SUCCESSOR" &&
      "$(sed -n 's/^token=//p' "$CLAIM_SUCCESSOR")" == "successor" &&
      "$(awk -F, '$3=="T-233" {print $8":"$9}' "$CLAIM_MUTATION_ROOT/factory/runtime-ledger.csv")" == "0.42:11" ]]; then
  pass "claim replacement fails closed without deleting successor ownership"
else
  fail "claim replacement fails closed without deleting successor ownership" \
    "status=$CLAIM_MUTATION_STATUS"
fi

CLAIM_ENTRY_ROOT="$TMP/claim-extra-entry"
write_envelope "$CLAIM_ENTRY_ROOT"
write_ticket "$CLAIM_ENTRY_ROOT" T-241
CLAIM_ENTRY_STATUS=0
FACTORY_ROOT="$CLAIM_ENTRY_ROOT" FACTORY_GLOBAL_ENV="$TMP/no-global.env" \
  FACTORY_TEST_MODE=1 FACTORY_ADAPTER_OVERRIDE=mock MOCK_ADD_ACTIVE_CLAIM_ENTRY=1 \
  "$RUN_AGENT" --role planner --ticket T-241 -- "add claim entry" \
  > "$TMP/claim-extra-entry.out" 2>&1 || CLAIM_ENTRY_STATUS=$?
if [[ "$CLAIM_ENTRY_STATUS" -eq 11 &&
      -f "$CLAIM_ENTRY_ROOT/factory/.active-runs/T-241.planner.lock/junk" ]]; then
  pass "extra run claim entries fail closed before advancement"
else
  fail "extra run claim entries fail closed before advancement" \
    "status=$CLAIM_ENTRY_STATUS"
fi

STALE_CLAIM_ROOT="$TMP/stale-claim"
write_envelope "$STALE_CLAIM_ROOT"
write_ticket "$STALE_CLAIM_ROOT" T-234
mkdir -p "$STALE_CLAIM_ROOT/factory/.active-runs/T-234.planner.lock"
printf 'pid=99999\nprocess_start=stale\ntoken=stale\n' > \
  "$STALE_CLAIM_ROOT/factory/.active-runs/T-234.planner.lock/owner"
STALE_CLAIM_STATUS=0
FACTORY_ROOT="$STALE_CLAIM_ROOT" FACTORY_GLOBAL_ENV="$TMP/no-global.env" \
  FACTORY_TEST_MODE=1 FACTORY_ADAPTER_OVERRIDE=mock \
  "$RUN_AGENT" --role planner --ticket T-234 -- "stale claim" \
  > "$TMP/stale-claim.out" 2>&1 || STALE_CLAIM_STATUS=$?
if [[ "$STALE_CLAIM_STATUS" -eq 7 &&
      -f "$STALE_CLAIM_ROOT/factory/.active-runs/T-234.planner.lock/owner" &&
      -z "$(find "$STALE_CLAIM_ROOT/factory/runs" -name '*.meta' -print -quit)" ]]; then
  pass "stale claim is never reclaimed by an ordinary launch"
else
  fail "stale claim is never reclaimed by an ordinary launch" \
    "status=$STALE_CLAIM_STATUS"
fi

STALE_PROVIDER_ROOT="$TMP/stale-provider-lock"
write_envelope "$STALE_PROVIDER_ROOT"
write_ticket "$STALE_PROVIDER_ROOT" T-242
mkdir "$STALE_PROVIDER_ROOT/factory/.provider.lock"
printf 'pid=99999999\nprocess_start=stale\ntoken=00000000000000000000000000000000\n' > \
  "$STALE_PROVIDER_ROOT/factory/.provider.lock/owner"
STALE_PROVIDER_STATUS=0
FACTORY_ROOT="$STALE_PROVIDER_ROOT" FACTORY_GLOBAL_ENV="$TMP/no-global.env" \
  FACTORY_TEST_MODE=1 FACTORY_ADAPTER_OVERRIDE=mock \
  "$RUN_AGENT" --role planner --ticket T-242 -- "stale provider lock" \
  > "$TMP/stale-provider-lock.out" 2>&1 || STALE_PROVIDER_STATUS=$?
if [[ "$STALE_PROVIDER_STATUS" -eq 8 &&
      -f "$STALE_PROVIDER_ROOT/factory/.provider.lock/owner" &&
      -z "$(find "$STALE_PROVIDER_ROOT/factory/runs" -name '*.meta' -print -quit)" &&
      "$(cat "$TMP/stale-provider-lock.out")" == *"stale provider lock requires operator reconciliation"* ]]; then
  pass "ordinary launch refuses stale provider lock before manifest creation"
else
  fail "ordinary launch refuses stale provider lock before manifest creation" \
    "status=$STALE_PROVIDER_STATUS"
fi

GLOBAL_MUTATION_ROOT="$TMP/global-ledger-mutation"
write_envelope "$GLOBAL_MUTATION_ROOT"
write_ticket "$GLOBAL_MUTATION_ROOT" T-235
GLOBAL_MUTATION_ENV="$TMP/global-ledger-mutation-config/global.env"
write_backend_global "$GLOBAL_MUTATION_ENV"
GLOBAL_MUTATION_LEDGER="$(dirname "$GLOBAL_MUTATION_ENV")/global-ledger.csv"
GLOBAL_MUTATION_STATUS=0
FACTORY_ROOT="$GLOBAL_MUTATION_ROOT" FACTORY_GLOBAL_ENV="$GLOBAL_MUTATION_ENV" \
  FACTORY_TEST_MODE=1 FACTORY_ADAPTER_OVERRIDE=mock \
  MOCK_MUTATE_GLOBAL_LEDGER=1 MOCK_GLOBAL_LEDGER_PATH="$GLOBAL_MUTATION_LEDGER" \
  "$RUN_AGENT" --role planner --ticket T-235 -- "mutate global ledger" \
  > "$TMP/global-ledger-mutation.out" 2>&1 || GLOBAL_MUTATION_STATUS=$?
if [[ "$GLOBAL_MUTATION_STATUS" -eq 11 &&
      "$(awk -F, '$4=="T-235" {print $9":"$10}' "$GLOBAL_MUTATION_LEDGER")" == "0.42:11" &&
      ! -e "$(dirname "$GLOBAL_MUTATION_ENV")/.ledger.lock" ]]; then
  pass "global ledger mutation is restored and terminalized under one lock"
else
  fail "global ledger mutation is restored and terminalized under one lock" \
    "status=$GLOBAL_MUTATION_STATUS"
fi

GLOBAL_LEDGER_TEST_HEADER='date,time,repo,ticket,role,adapter,prompt_version,turns,cost_usd,exit_status,run_id,provider_family,model_id,selection_reason,cost_basis,adapter_version'
for global_case in extra-field negative-cost huge-cost; do
  GLOBAL_BAD_ROOT="$TMP/global-ledger-$global_case-product"
  write_envelope "$GLOBAL_BAD_ROOT"
  write_ticket "$GLOBAL_BAD_ROOT" T-236
  GLOBAL_BAD_ENV="$TMP/global-ledger-$global_case-config/global.env"
  write_backend_global "$GLOBAL_BAD_ENV"
  GLOBAL_BAD_LEDGER="$(dirname "$GLOBAL_BAD_ENV")/global-ledger.csv"
  case "$global_case" in
    extra-field)
      printf '%s\n%s\n' "$GLOBAL_LEDGER_TEST_HEADER" \
        '2026-07-15,00:00:00,/tmp/product,T-1,planner,mock,test,1,0.10,0,old,mock,,,test_fixture,test,extra' > "$GLOBAL_BAD_LEDGER"
      ;;
    negative-cost)
      printf '%s\n%s\n' "$GLOBAL_LEDGER_TEST_HEADER" \
        '2026-07-15,00:00:00,/tmp/product,T-1,planner,mock,test,1,-1,0,old,mock,,,test_fixture,test' > "$GLOBAL_BAD_LEDGER"
      ;;
    huge-cost)
      printf '%s\n' "$GLOBAL_LEDGER_TEST_HEADER" > "$GLOBAL_BAD_LEDGER"
      printf '2026-07-15,00:00:00,/tmp/product,T-1,planner,mock,test,1,%s,0,old,mock,,,test_fixture,test\n' \
        "$HUGE_TELEMETRY" >> "$GLOBAL_BAD_LEDGER"
      ;;
  esac
  GLOBAL_BAD_STATUS=0
  FACTORY_ROOT="$GLOBAL_BAD_ROOT" FACTORY_GLOBAL_ENV="$GLOBAL_BAD_ENV" \
    FACTORY_TEST_MODE=1 FACTORY_ADAPTER_OVERRIDE=mock \
    "$RUN_AGENT" --role planner --ticket T-236 -- "$global_case" \
    > "$TMP/global-ledger-$global_case.out" 2>&1 || GLOBAL_BAD_STATUS=$?
  if [[ "$GLOBAL_BAD_STATUS" -eq 3 &&
        "$(cat "$GLOBAL_BAD_LEDGER")" != *"reserved-"* &&
        ! -e "$(dirname "$GLOBAL_BAD_ENV")/.ledger.lock" ]]; then
    pass "global ledger rejects $global_case before provider execution"
  else
    fail "global ledger rejects $global_case before provider execution" \
      "status=$GLOBAL_BAD_STATUS"
  fi
done

GLOBAL_SYMLINK_ROOT="$TMP/global-ledger-symlink-product"
write_envelope "$GLOBAL_SYMLINK_ROOT"
write_ticket "$GLOBAL_SYMLINK_ROOT" T-237
GLOBAL_SYMLINK_ENV="$TMP/global-ledger-symlink-config/global.env"
write_backend_global "$GLOBAL_SYMLINK_ENV"
GLOBAL_SYMLINK_LEDGER="$(dirname "$GLOBAL_SYMLINK_ENV")/global-ledger.csv"
printf '%s\n' "$GLOBAL_LEDGER_TEST_HEADER" > "$TMP/global-ledger-target.csv"
ln -s "$TMP/global-ledger-target.csv" "$GLOBAL_SYMLINK_LEDGER"
GLOBAL_SYMLINK_STATUS=0
FACTORY_ROOT="$GLOBAL_SYMLINK_ROOT" FACTORY_GLOBAL_ENV="$GLOBAL_SYMLINK_ENV" \
  FACTORY_TEST_MODE=1 FACTORY_ADAPTER_OVERRIDE=mock \
  "$RUN_AGENT" --role planner --ticket T-237 -- "symlink global" \
  > "$TMP/global-ledger-symlink.out" 2>&1 || GLOBAL_SYMLINK_STATUS=$?
if [[ "$GLOBAL_SYMLINK_STATUS" -eq 3 && -L "$GLOBAL_SYMLINK_LEDGER" &&
      ! -e "$(dirname "$GLOBAL_SYMLINK_ENV")/.ledger.lock" ]]; then
  pass "global ledger rejects symlink storage before provider execution"
else
  fail "global ledger rejects symlink storage before provider execution" \
    "status=$GLOBAL_SYMLINK_STATUS"
fi
fi

if [[ "$SUBSET" == "sequencer" ]]; then
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

if expect_stage "FIX builder-or-test-author" "$ROUNDS" T-300; then
  pass "budget-only review continues after two semantic rounds"
fi

# Contract 1.7 makes repair ownership mechanical. When both roles own a fix,
# Test-author must finish before the Builder and only then may Reviewer rerun.
OWNED_FIX="$TMP/owned-fix"
write_envelope "$OWNED_FIX" no-git
{
  ledger_header
  ledger_row T-302 planner
  ledger_row T-302 test-author
  ledger_row T-302 builder
  ledger_row T-302 reviewer
} > "$OWNED_FIX/factory/ledger.csv"
cat > "$OWNED_FIX/factory/tickets/T-302.md" <<'EOF'
# T-302
reviewer round 1: REQUEST CHANGES
reviewer round 1 FIX-OWNER: both
EOF
OWNED_FIX_18="$TMP/owned-fix-18"
cp -R "$OWNED_FIX" "$OWNED_FIX_18"
if TEST_CONTRACT_VERSION=1.7.0 expect_stage "FIX test-author" "$OWNED_FIX" T-302; then
  ledger_row T-302 test-author >> "$OWNED_FIX/factory/ledger.csv"
  if TEST_CONTRACT_VERSION=1.7.0 expect_stage "FIX builder" "$OWNED_FIX" T-302; then
    ledger_row T-302 builder >> "$OWNED_FIX/factory/ledger.csv"
    TEST_CONTRACT_VERSION=1.7.0 expect_stage "RUN reviewer" "$OWNED_FIX" T-302 &&
      pass "contract 1.7 sequences explicit dual-owner repairs"
  fi
fi
if TEST_CONTRACT_VERSION=2.0.0 expect_stage "FIX planner" "$OWNED_FIX_18" T-302; then
  ledger_row T-302 planner >> "$OWNED_FIX_18/factory/ledger.csv"
  TEST_CONTRACT_VERSION=2.0.0 expect_stage \
    "REFUSE Planner repair did not open one authenticated test-first contract epoch" \
    "$OWNED_FIX_18" T-302 &&
    pass "contract 1.8 refuses late Test-author work without a new epoch"
  printf '%s\n' \
    '### Frozen contract — version 1' \
    '- **Freeze result:** PASS. Contract version 1 is frozen.' >> \
    "$OWNED_FIX_18/factory/tickets/T-302.md"
  git -C "$OWNED_FIX_18" add factory/tickets/T-302.md
  git -C "$OWNED_FIX_18" -c user.name=test -c user.email=test@example.com \
    commit -qm "open test-first repair epoch"
  if TEST_CONTRACT_VERSION=2.0.0 expect_stage "FIX test-author" "$OWNED_FIX_18" T-302; then
    ledger_row T-302 test-author >> "$OWNED_FIX_18/factory/ledger.csv"
    if TEST_CONTRACT_VERSION=2.0.0 expect_stage "FIX builder" "$OWNED_FIX_18" T-302; then
      ledger_row T-302 builder >> "$OWNED_FIX_18/factory/ledger.csv"
      TEST_CONTRACT_VERSION=2.0.0 expect_stage "RUN reviewer" "$OWNED_FIX_18" T-302 &&
        pass "contract 1.8 sequences a test-first dual-owner repair epoch"
    fi
  fi
fi

TEST_ONLY_FIX="$TMP/test-only-fix"
write_envelope "$TEST_ONLY_FIX" no-git
{
  ledger_header
  ledger_row T-304 planner
  ledger_row T-304 test-author
  ledger_row T-304 builder
  ledger_row T-304 reviewer
} > "$TEST_ONLY_FIX/factory/ledger.csv"
printf '%s\n' '# T-304' 'reviewer round 1: REQUEST CHANGES' \
  'reviewer round 1 FIX-OWNER: test-author' > \
  "$TEST_ONLY_FIX/factory/tickets/T-304.md"
TEST_CONTRACT_VERSION=2.0.0 expect_stage "FIX planner" "$TEST_ONLY_FIX" T-304 &&
  pass "contract 1.8 opens an epoch before a single-owner Test-author repair"

MISSING_OWNER="$TMP/missing-fix-owner"
mkdir -p "$MISSING_OWNER/factory/tickets"
{
  ledger_header
  ledger_row T-303 planner
  ledger_row T-303 test-author
  ledger_row T-303 builder
  ledger_row T-303 reviewer
} > "$MISSING_OWNER/factory/ledger.csv"
printf '%s\n' '# T-303' 'reviewer round 1: REQUEST CHANGES' \
  > "$MISSING_OWNER/factory/tickets/T-303.md"
TEST_CONTRACT_VERSION=1.7.0 expect_stage "REFUSE" "$MISSING_OWNER" T-303 &&
  pass "contract 1.7 refuses missing repair ownership"

if expect_stage "FIX builder-or-test-author" "$ROUNDS" T-300; then
  ledger_row T-300 builder >> "$ROUNDS/factory/ledger.csv"
  if expect_stage "RUN reviewer" "$ROUNDS" T-300; then
    pass "semantic round authorization preserves the fix gate"
  fi
fi

# Spec-linter continues while each failed round produces a new contract revision.
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

if expect_stage "AWAIT-OPERATOR semantic-round authorization required" "$SPEC_ROUNDS" T-301; then
  pass "third semantic round waits for one exact authorization"
fi
printf '%s\n' 'OPERATOR AUTHORIZATION: spec-linter round 3' >> \
  "$SPEC_ROUNDS/factory/tickets/T-301.md"
if expect_stage "RUN planner" "$SPEC_ROUNDS" T-301; then
  pass "exact third-round authorization resumes planning"
fi

ledger_row T-301 planner >> "$SPEC_ROUNDS/factory/ledger.csv"
if expect_stage "RUN spec-linter" "$SPEC_ROUNDS" T-301; then
  pass "spec-linter authorization permits the exact lint round"
fi

ledger_row T-301 spec-linter >> "$SPEC_ROUNDS/factory/ledger.csv"
printf '%s\n' 'SPEC-LINT: FAIL — third' >> "$SPEC_ROUNDS/factory/tickets/T-301.md"
if expect_stage "ESCALATE planner-spec-linter loop cap reached" "$SPEC_ROUNDS" T-301; then
  pass "third failed spec round reaches the hard cap"
fi

sed -i.bak 's/SPEC-LINT: FAIL — third/SPEC-LINT: PASS/' "$SPEC_ROUNDS/factory/tickets/T-301.md"
rm -f "$SPEC_ROUNDS/factory/tickets/T-301.md.bak"
if expect_stage "RUN test-author" "$SPEC_ROUNDS" T-301; then
  pass "authorized spec-linter pass advances to tests"
fi

# Qualification starts one sealed role-control epoch without rewriting the
# protected ticket's historical audit log.
QUALIFICATION_EPOCH="$TMP/qualification-role-epoch"
write_envelope "$QUALIFICATION_EPOCH" no-git
cat > "$QUALIFICATION_EPOCH/factory/tickets/T-305.md" <<EOF
# T-305
State: Ready
Kit-SHA: $KIT_SHA
SPEC-LINT: FAIL — historical one
SPEC-LINT: FAIL — historical two
SPEC-LINT: FAIL — historical three
reviewer round 1: APPROVE
OPERATOR AUTHORIZATION: spec-linter round 3
EOF
cat > "$QUALIFICATION_EPOCH/factory/tickets/T-306.md" <<EOF
# T-306
State: Planning
Kit-SHA: $KIT_SHA
SPEC-LINT: PASS
EOF
cat > "$QUALIFICATION_EPOCH/factory/tickets/T-307.md" <<EOF
# T-307
State: Planning
Kit-SHA: $KIT_SHA
SPEC-LINT: FAIL — historical one
SPEC-LINT: FAIL — historical two
SPEC-LINT: FAIL — historical three
OPERATOR AUTHORIZATION: spec-linter round 3
EOF
ledger_header > "$QUALIFICATION_EPOCH/factory/ledger.csv"
init_product_git "$QUALIFICATION_EPOCH"
QUALIFICATION_EPOCH_SHA="$(git -C "$QUALIFICATION_EPOCH" rev-parse HEAD)"
export FACTORY_KIT_TRUST_SCOPE=qualification-candidate
export FACTORY_QUALIFICATION_PRODUCT_SHA="$QUALIFICATION_EPOCH_SHA"
ledger_row T-306 planner >> "$QUALIFICATION_EPOCH/factory/ledger.csv"
TEST_CONTRACT_VERSION=2.0.0 expect_stage "RUN spec-linter" \
  "$QUALIFICATION_EPOCH" T-306 || true
{
  ledger_row T-307 planner
  ledger_row T-307 spec-linter
  ledger_row T-307 planner
  ledger_row T-307 spec-linter
} >> "$QUALIFICATION_EPOCH/factory/ledger.csv"
printf '%s\n' 'SPEC-LINT: FAIL — current one' \
  'SPEC-LINT: FAIL — current two' >> \
  "$QUALIFICATION_EPOCH/factory/tickets/T-307.md"
git -C "$QUALIFICATION_EPOCH" add factory/tickets/T-307.md
git -C "$QUALIFICATION_EPOCH" -c user.name=test -c user.email=test@example.com \
  commit -qm "record current qualification failures"
TEST_CONTRACT_VERSION=2.0.0 expect_stage \
  "AWAIT-OPERATOR semantic-round authorization required" \
  "$QUALIFICATION_EPOCH" T-307 || true
printf '%s\n' 'OPERATOR AUTHORIZATION: spec-linter round 3' >> \
  "$QUALIFICATION_EPOCH/factory/tickets/T-307.md"
git -C "$QUALIFICATION_EPOCH" add factory/tickets/T-307.md
git -C "$QUALIFICATION_EPOCH" -c user.name=test -c user.email=test@example.com \
  commit -qm "authorize current qualification round"
TEST_CONTRACT_VERSION=2.0.0 expect_stage "RUN planner" \
  "$QUALIFICATION_EPOCH" T-307 || true
printf '%s\n' 'OPERATOR AUTHORIZATION: spec-linter round 3' >> \
  "$QUALIFICATION_EPOCH/factory/tickets/T-307.md"
git -C "$QUALIFICATION_EPOCH" add factory/tickets/T-307.md
git -C "$QUALIFICATION_EPOCH" -c user.name=test -c user.email=test@example.com \
  commit -qm "duplicate current qualification authorization"
TEST_CONTRACT_VERSION=2.0.0 expect_stage \
  "AWAIT-OPERATOR semantic-round authorization invalid" \
  "$QUALIFICATION_EPOCH" T-307 || true
if TEST_CONTRACT_VERSION=2.0.0 expect_stage "RUN planner" \
  "$QUALIFICATION_EPOCH" T-305; then
  ledger_row T-305 planner >> "$QUALIFICATION_EPOCH/factory/ledger.csv"
  TEST_CONTRACT_VERSION=2.0.0 expect_stage "RUN spec-linter" \
    "$QUALIFICATION_EPOCH" T-305 || true
fi
ledger_row T-305 spec-linter >> "$QUALIFICATION_EPOCH/factory/ledger.csv"
TEST_CONTRACT_VERSION=2.0.0 expect_stage "REFUSE spec-linter has 1 successful run" \
  "$QUALIFICATION_EPOCH" T-305 || true
printf '%s\n' 'SPEC-LINT: PASS' >> \
  "$QUALIFICATION_EPOCH/factory/tickets/T-305.md"
git -C "$QUALIFICATION_EPOCH" add factory/tickets/T-305.md
git -C "$QUALIFICATION_EPOCH" -c user.name=test -c user.email=test@example.com \
  commit -qm "record current spec verdict"
TEST_CONTRACT_VERSION=2.0.0 expect_stage "RUN test-author" \
  "$QUALIFICATION_EPOCH" T-305 || true
{
  ledger_row T-305 test-author
  ledger_row T-305 builder
  ledger_row T-305 reviewer
} >> "$QUALIFICATION_EPOCH/factory/ledger.csv"
TEST_CONTRACT_VERSION=2.0.0 expect_stage "REFUSE reviewer has 1 non-void successful run" \
  "$QUALIFICATION_EPOCH" T-305 || true
printf '%s\n' 'reviewer round 2: APPROVE' >> \
  "$QUALIFICATION_EPOCH/factory/tickets/T-305.md"
git -C "$QUALIFICATION_EPOCH" add factory/tickets/T-305.md
git -C "$QUALIFICATION_EPOCH" -c user.name=test -c user.email=test@example.com \
  commit -qm "record current reviewer verdict"
if TEST_CONTRACT_VERSION=2.0.0 expect_stage "RUN narrator" \
  "$QUALIFICATION_EPOCH" T-305; then
  pass "qualification role-control epoch ignores protected history"
fi
unset FACTORY_KIT_TRUST_SCOPE FACTORY_QUALIFICATION_PRODUCT_SHA

# Missing verdict still refuses unless the extra row has a void note.
grep -v 'OPERATOR NOTE' "$ROUNDS/factory/tickets/T-300.md" > "$ROUNDS/factory/tickets/T-300.tmp"
mv "$ROUNDS/factory/tickets/T-300.tmp" "$ROUNDS/factory/tickets/T-300.md"
if expect_stage "REFUSE" "$ROUNDS" T-300; then
  pass "unrecorded non-void reviewer run refuses"
fi
fi

if [[ "$SUBSET" == "sequencer" ]]; then
# Duplicate-run guard: overlap refused, same ticket+role allowed afterward.
GUARD="$TMP/guard"
write_envelope "$GUARD"
write_ticket "$GUARD" T-400
GUARD_LEDGER="$GUARD/factory/runtime-ledger.csv"
MOCK_SLEEP=30 FACTORY_ROOT="$GUARD" FACTORY_LEDGER="$GUARD_LEDGER" \
  FACTORY_GLOBAL_ENV="$TMP/no-global.env" FACTORY_TEST_MODE=1 \
  FACTORY_ADAPTER_OVERRIDE=mock \
  "$RUN_AGENT" --role planner --ticket T-400 -- "slow run" > "$TMP/first.out" 2>&1 &
FIRST_PID=$!
for _i in $(seq 1 1200); do
  [[ -n "$(ls "$GUARD/factory/.active-runs/"*.lock/owner 2>/dev/null || true)" ]] && break
  sleep 0.05
done
GUARD_CLAIM_OWNER="$(find "$GUARD/factory/.active-runs" -name owner -print -quit)"
for _i in $(seq 1 1200); do
  [[ -f "$GUARD/factory/.provider.lock/owner" ]] && break
  sleep 0.05
done
if [[ -f "$GUARD_CLAIM_OWNER" &&
      "$(cat "$GUARD_CLAIM_OWNER")" == "$(cat "$GUARD/factory/.provider.lock/owner" 2>/dev/null)" &&
      "$(python3 -c 'import os,sys; print(oct(os.stat(sys.argv[1]).st_mode & 0o777)[2:])' "$GUARD/factory/.provider.lock/owner")" == "600" ]]; then
  pass "provider lock binds to the live wrapper identity"
else
  fail "provider lock binds to the live wrapper identity"
fi
SECOND_OUTPUT="$(FACTORY_ROOT="$GUARD" FACTORY_LEDGER="$GUARD_LEDGER" \
  FACTORY_GLOBAL_ENV="$TMP/no-global.env" FACTORY_TEST_MODE=1 \
  FACTORY_ADAPTER_OVERRIDE=mock \
  "$RUN_AGENT" --role planner --ticket T-400 -- "overlap" 2>&1)"
SECOND_STATUS=$?
wait "$FIRST_PID"
FIRST_PID=""

if [[ "$SECOND_STATUS" -eq 7 && "$SECOND_OUTPUT" == *"run claim exists"* ]]; then
  pass "duplicate-run guard refuses overlap"
else
  fail "duplicate-run guard refuses overlap" "status $SECOND_STATUS: $SECOND_OUTPUT"
fi

SEQUENTIAL_STATUS=0
run_mock "$GUARD" planner T-400 >/dev/null 2>&1 || SEQUENTIAL_STATUS=$?
if [[ "$SEQUENTIAL_STATUS" -eq 10 &&
      "$(awk -F, '$3=="T-400" && $4=="planner" && $14!="launch_void" {n++} END {print n+0}' "$GUARD_LEDGER")" == "1" &&
      "$(awk -F, '$3=="T-400" && $4=="planner" && $14=="launch_void" {n++} END {print n+0}' "$GUARD_LEDGER")" == "0" ]]; then
  pass "sequencer refuses obsolete sequential role"
else
  fail "sequencer refuses obsolete sequential role" "status $SEQUENTIAL_STATUS"
fi

# Targeted cancellation binds one prepared run and never publishes product KILL.
PRE_CANCEL="$TMP/pre-go-cancel"
write_envelope "$PRE_CANCEL"
write_ticket "$PRE_CANCEL" T-405
FACTORY_ROOT="$PRE_CANCEL" FACTORY_GLOBAL_ENV="$TMP/no-global.env" \
  FACTORY_TEST_MODE=1 FACTORY_TEST_BEFORE_GO_SLEEP=30 \
  FACTORY_ADAPTER_OVERRIDE=mock MOCK_SLEEP=30 \
  "$RUN_AGENT" --role planner --ticket T-405 -- "pre-GO cancellation" \
  > "$TMP/pre-go-cancel.out" 2>&1 &
PRE_CANCEL_PID=$!
PRE_CANCEL_RUN=""
for _i in $(seq 1 450); do
  PRE_CANCEL_META="$(ls "$PRE_CANCEL/factory/runs/"*.meta 2>/dev/null || true)"
  if [[ -n "$PRE_CANCEL_META" ]] && grep -q '^phase=prepared$' "$PRE_CANCEL_META"; then
    PRE_CANCEL_RUN="$(basename "$PRE_CANCEL_META" .meta)"
    break
  fi
  sleep 0.02
done
PRE_CANCEL_PLAN="$TMP/pre-go-cancel-plan.json"
python3 "$ATTEMPT_CANCEL" preview --factory-root "$PRE_CANCEL" \
  --ticket T-405 --run-id "$PRE_CANCEL_RUN" --reason operator_requested \
  > "$PRE_CANCEL_PLAN"
PRE_CANCEL_HASH="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["preview_hash"])' "$PRE_CANCEL_PLAN")"
python3 "$ATTEMPT_CANCEL" apply --factory-root "$PRE_CANCEL" \
  --plan "$PRE_CANCEL_PLAN" --preview-hash "$PRE_CANCEL_HASH" --timeout 30 \
  > "$TMP/pre-go-cancel-receipt.json"
wait "$PRE_CANCEL_PID" 2>/dev/null || true
if grep -q '^accounting_state=launch_void$' "$PRE_CANCEL/factory/runs/$PRE_CANCEL_RUN.meta" &&
   grep -q '^effective_cost=0$' "$PRE_CANCEL/factory/runs/$PRE_CANCEL_RUN.meta" &&
   grep -q '^role_exit=cancelled$' "$PRE_CANCEL/factory/runs/$PRE_CANCEL_RUN.meta" &&
   [[ -f "$PRE_CANCEL/factory/runs/$PRE_CANCEL_RUN.cancel.json" &&
      ! -e "$PRE_CANCEL/factory/KILL" ]]; then
  pass "pre-GO targeted cancellation is zero-cost and product-local"
else
  fail "pre-GO targeted cancellation is zero-cost and product-local"
fi

# Post-GO cancellation remains charged and drains before its receipt is emitted.
POST_CANCEL="$TMP/post-go-cancel"
write_envelope "$POST_CANCEL"
write_ticket "$POST_CANCEL" T-406
FACTORY_ROOT="$POST_CANCEL" FACTORY_GLOBAL_ENV="$TMP/no-global.env" \
  FACTORY_TEST_MODE=1 FACTORY_ADAPTER_OVERRIDE=mock MOCK_SLEEP=30 \
  "$RUN_AGENT" --role planner --ticket T-406 -- "post-GO cancellation" \
  > "$TMP/post-go-cancel.out" 2>&1 &
POST_CANCEL_PID=$!
POST_CANCEL_RUN=""
for _i in $(seq 1 450); do
  POST_CANCEL_META="$(ls "$POST_CANCEL/factory/runs/"*.meta 2>/dev/null || true)"
  if [[ -n "$POST_CANCEL_META" ]] && grep -q '^go_issued=1$' "$POST_CANCEL_META"; then
    POST_CANCEL_RUN="$(basename "$POST_CANCEL_META" .meta)"
    break
  fi
  sleep 0.02
done
POST_CANCEL_PLAN="$TMP/post-go-cancel-plan.json"
python3 "$ATTEMPT_CANCEL" preview --factory-root "$POST_CANCEL" \
  --ticket T-406 --run-id "$POST_CANCEL_RUN" --reason budget_exhausted \
  > "$POST_CANCEL_PLAN"
POST_CANCEL_HASH="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["preview_hash"])' "$POST_CANCEL_PLAN")"
python3 "$ATTEMPT_CANCEL" apply --factory-root "$POST_CANCEL" \
  --plan "$POST_CANCEL_PLAN" --preview-hash "$POST_CANCEL_HASH" --timeout 30 \
  > "$TMP/post-go-cancel-receipt.json"
wait "$POST_CANCEL_PID" 2>/dev/null || true
if grep -q '^accounting_state=cancelled_conservative$' \
     "$POST_CANCEL/factory/runs/$POST_CANCEL_RUN.meta" &&
   grep -q '^role_exit=cancelled$' "$POST_CANCEL/factory/runs/$POST_CANCEL_RUN.meta" &&
   [[ ! -e "$POST_CANCEL/factory/runs/$POST_CANCEL_RUN.pid" &&
      ! -e "$POST_CANCEL/factory/.provider.lock" &&
      ! -e "$POST_CANCEL/factory/KILL" ]] &&
   awk -F, -v run="$POST_CANCEL_RUN" \
     '$10==run && $8=="1.00" && $14=="conservative_reservation" {found=1} END {exit !found}' \
     "$POST_CANCEL/factory/runtime-ledger.csv"; then
  pass "post-GO targeted cancellation is charged and converged"
else
  fail "post-GO targeted cancellation is charged and converged"
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
for _i in $(seq 1 1200); do
  KILL_PID_FILE="$(ls "$KILL_ROOT/factory/runs/"*.pid 2>/dev/null || true)"
  [[ -n "$KILL_PID_FILE" && -f "$DESCENDANT_PID_FILE" ]] && break
  sleep 0.05
done
KILL_PGID="$(sed -n 's/^pgid=//p' "$KILL_PID_FILE" 2>/dev/null | awk 'NR==1 {print; exit}')"
FACTORY_SKIP_SCHEDULE_STOP=1 "$KILL_SWITCH" "$KILL_ROOT" >/dev/null
wait "$KILL_WRAPPER_PID" 2>/dev/null || true
KILL_DESCENDANT_PID="$(awk 'NR==1 {print; exit}' "$DESCENDANT_PID_FILE" 2>/dev/null || true)"
NEW_AFTER_KILL_STATUS=0
FACTORY_ROOT="$KILL_ROOT" FACTORY_GLOBAL_ENV="$TMP/no-global.env" \
  FACTORY_TEST_MODE=1 \
  FACTORY_ADAPTER_OVERRIDE=mock \
  "$RUN_AGENT" --role planner --ticket T-402 -- "must refuse" >/dev/null 2>&1 ||
  NEW_AFTER_KILL_STATUS=$?
if [[ "$KILL_PGID" =~ ^[0-9]+$ ]] &&
   [[ "$KILL_DESCENDANT_PID" =~ ^[0-9]+$ ]] &&
   ! kill -0 "$KILL_DESCENDANT_PID" 2>/dev/null &&
   [[ -z "$(ls "$KILL_ROOT/factory/runs/"*.pid 2>/dev/null || true)" ]] &&
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

STALE_PROVIDER_KILL_ROOT="$TMP/stale-provider-kill"
mkdir -p "$STALE_PROVIDER_KILL_ROOT/factory/runs" \
  "$STALE_PROVIDER_KILL_ROOT/factory/.provider.lock"
printf 'pid=99999999\nprocess_start=stale\ntoken=00000000000000000000000000000000\n' > \
  "$STALE_PROVIDER_KILL_ROOT/factory/.provider.lock/owner"
FACTORY_SKIP_SCHEDULE_STOP=1 "$KILL_SWITCH" "$STALE_PROVIDER_KILL_ROOT" \
  > "$TMP/stale-provider-kill.out" 2>&1
STALE_PROVIDER_QUARANTINE="$(find "$STALE_PROVIDER_KILL_ROOT/factory/runs" -maxdepth 1 \
  -type d -name '.provider-lock-stale-*' -print -quit)"
if [[ ! -e "$STALE_PROVIDER_KILL_ROOT/factory/.provider.lock" &&
      -f "$STALE_PROVIDER_QUARANTINE/owner" &&
      "$(cat "$TMP/stale-provider-kill.out")" == *"quarantined stale provider lock"* ]]; then
  pass "kill switch quarantines a proven stale provider lock"
else
  fail "kill switch quarantines a proven stale provider lock"
fi
fi

if [[ "$SUBSET" == "sequencer" ]]; then
# Exhaustion blocks only a new provider role. A successful, fully accounted
# Narrator may still reduce to its deterministic bundle-attestation boundary.
POST_BUDGET_ROOT="$TMP/post-budget-reduction"
write_envelope "$POST_BUDGET_ROOT"
sed -i.bak 's/PER_TICKET_BUDGET_USD=20.00/PER_TICKET_BUDGET_USD=0.60/' \
  "$POST_BUDGET_ROOT/factory/ENVELOPE.env"
rm "$POST_BUDGET_ROOT/factory/ENVELOPE.env.bak"
sed -i.bak 's/PER_RUN_BUDGET_USD=1.00/PER_RUN_BUDGET_USD=0.10/' \
  "$POST_BUDGET_ROOT/factory/ENVELOPE.env"
rm "$POST_BUDGET_ROOT/factory/ENVELOPE.env.bak"
write_ticket "$POST_BUDGET_ROOT" T-532 Review
cat >> "$POST_BUDGET_ROOT/factory/tickets/T-532.md" <<'TICKET'
SPEC-LINT: PASS
reviewer round 1: APPROVE
TICKET
cat > "$POST_BUDGET_ROOT/factory/tickets/T-532-bundle.md" <<'BUNDLE'
# What this does
# Preview
# Screenshots
# Acceptance criteria
# Risk
# Cost
# Rollback
Approve to merge?
BUNDLE
git -C "$POST_BUDGET_ROOT" add factory/ENVELOPE.env factory/tickets
git -C "$POST_BUDGET_ROOT" -c user.name=test -c user.email=test@example.com \
  commit -qm "post-budget fixture"
POST_BUDGET_HEAD="$(git -C "$POST_BUDGET_ROOT" rev-parse HEAD)"
for role in planner spec-linter test-author builder reviewer narrator; do
  run_id="post-budget-$role"
  ledger_row_run T-532 "$role" "$run_id" >> \
    "$POST_BUDGET_ROOT/factory/ledger.csv"
  write_run_manifest "$POST_BUDGET_ROOT" T-532 "$role" "$run_id" \
    "$POST_BUDGET_HEAD"
  printf 'effective_cost=0.100000\nkit_sha=%s\n' "$KIT_SHA" >> \
    "$POST_BUDGET_ROOT/factory/runs/$run_id.meta"
done
POST_BUDGET_OK=1
TEST_CONTRACT_VERSION=2.0.0 expect_stage \
  "AWAIT-OPERATOR bundle posted" "$POST_BUDGET_ROOT" T-532 || \
  POST_BUDGET_OK=0
printf '# What this does\n' > \
  "$POST_BUDGET_ROOT/factory/tickets/T-532-bundle.md"
TEST_CONTRACT_VERSION=2.0.0 expect_stage \
  "AWAIT_BUDGET ticket budget exhausted" "$POST_BUDGET_ROOT" T-532 || \
  POST_BUDGET_OK=0
write_ticket "$POST_BUDGET_ROOT" T-533 Ready
cat > "$POST_BUDGET_ROOT/factory/runs/post-budget-planner.meta" <<META
run_id=post-budget-planner
ticket=T-533
role=planner
accounting_state=completed
effective_cost=0.600000
exit_status=5
role_exit=budget
kit_sha=$KIT_SHA
META
git -C "$POST_BUDGET_ROOT" add factory/tickets/T-533.md
git -C "$POST_BUDGET_ROOT" -c user.name=test -c user.email=test@example.com \
  commit -qm "provider-budget fixture"
TEST_CONTRACT_VERSION=2.0.0 expect_stage \
  "AWAIT_BUDGET ticket budget exhausted" "$POST_BUDGET_ROOT" T-533 || \
  POST_BUDGET_OK=0
if [[ "$POST_BUDGET_OK" -eq 1 ]]; then
  pass "budget exhaustion permits only deterministic post-role reduction"
fi

# Full sequencer walkthrough: happy path.
WALK="$TMP/walk"
mkdir -p "$WALK/factory/tickets"
printf '# T-500\n' > "$WALK/factory/tickets/T-500.md"
ledger_header > "$WALK/factory/ledger.csv"
WALK_OK=1
cat > "$WALK/factory/tickets/T-499.md" <<'TICKET'
# T-499
State: Review
Operator-Approval: Linear
TICKET
expect_stage "REFUSE contract 1.2 has no trusted bundle-attestation path for approval" \
  "$WALK" T-499 || WALK_OK=0
cat > "$WALK/factory/tickets/T-499.md" <<'TICKET'
# T-499
State: Approved
TICKET
expect_stage "REFUSE contract 1.2 has no trusted bundle-attestation path for approval" \
  "$WALK" T-499 || WALK_OK=0
expect_stage "RUN planner" "$WALK" T-500 || WALK_OK=0
ledger_row T-500 planner >> "$WALK/factory/ledger.csv"
expect_stage "RUN spec-linter" "$WALK" T-500 || WALK_OK=0
ledger_row T-500 spec-linter >> "$WALK/factory/ledger.csv"
expect_stage "REFUSE" "$WALK" T-500 || WALK_OK=0
printf 'ordinary prose says SPEC-LINT: PASS because it looks good\n' >> \
  "$WALK/factory/tickets/T-500.md"
printf 'SPEC-LINT: PASS because it looks good\n' >> \
  "$WALK/factory/tickets/T-500.md"
expect_stage "REFUSE" "$WALK" T-500 || WALK_OK=0
printf 'SPEC-LINT: PASS\n' >> "$WALK/factory/tickets/T-500.md"
expect_stage "RUN test-author" "$WALK" T-500 || WALK_OK=0
ledger_row T-500 test-author >> "$WALK/factory/ledger.csv"
expect_stage "RUN builder" "$WALK" T-500 || WALK_OK=0
ledger_row T-500 builder >> "$WALK/factory/ledger.csv"
expect_stage "RUN reviewer" "$WALK" T-500 || WALK_OK=0
ledger_row T-500 reviewer >> "$WALK/factory/ledger.csv"
expect_stage "REFUSE" "$WALK" T-500 || WALK_OK=0
printf 'ordinary reviewer prose says APPROVE this change\n' >> \
  "$WALK/factory/tickets/T-500.md"
printf 'reviewer round 1: APPROVE because it looks good\n' >> \
  "$WALK/factory/tickets/T-500.md"
expect_stage "REFUSE" "$WALK" T-500 || WALK_OK=0
printf 'reviewer round 1: APPROVE\n' >> "$WALK/factory/tickets/T-500.md"
expect_stage "RUN narrator" "$WALK" T-500 || WALK_OK=0
ledger_row T-500 builder >> "$WALK/factory/ledger.csv"
expect_stage "RUN reviewer" "$WALK" T-500 || WALK_OK=0
ledger_row T-500 reviewer >> "$WALK/factory/ledger.csv"
printf 'reviewer round 2: APPROVE\n' >> "$WALK/factory/tickets/T-500.md"
expect_stage "RUN narrator" "$WALK" T-500 || WALK_OK=0
ledger_row T-500 narrator >> "$WALK/factory/ledger.csv"
cat > "$WALK/factory/tickets/T-500-bundle.md" <<'BUNDLE'
# What this does
# Preview
# Screenshots
# Acceptance criteria
# Risk
# Cost
# Rollback
Approve to merge?
BUNDLE
expect_stage "AWAIT-OPERATOR" "$WALK" T-500 || WALK_OK=0
printf '%s\n' \
  '{"tickets":{"T-500":{"operator":{"state":"Approved","approval":"Receipt","state_base":"awaiting approval"}}}}' \
  > "$WALK/factory/operator-map.json"
expect_stage "REFUSE contract 1.2 has no trusted bundle-attestation path for approval" \
  "$WALK" T-500 || WALK_OK=0
rm "$WALK/factory/operator-map.json"
printf 'Operator-Approval: Linear because the operator said so\n' >> \
  "$WALK/factory/tickets/T-500.md"
expect_stage "REFUSE contract 1.2 has no trusted bundle-attestation path for approval" \
  "$WALK" T-500 || WALK_OK=0
grep -v '^Operator-Approval:' "$WALK/factory/tickets/T-500.md" > \
  "$WALK/factory/tickets/T-500.tmp"
mv "$WALK/factory/tickets/T-500.tmp" "$WALK/factory/tickets/T-500.md"
printf 'Operator-Approval: Linear\n' >> "$WALK/factory/tickets/T-500.md"
expect_stage "REFUSE contract 1.2 has no trusted bundle-attestation path for approval" \
  "$WALK" T-500 || WALK_OK=0
printf 'State: Approved\n' >> "$WALK/factory/tickets/T-500.md"
expect_stage "REFUSE contract 1.2 has no trusted bundle-attestation path for approval" \
  "$WALK" T-500 || WALK_OK=0
[[ "$WALK_OK" -eq 1 ]] && pass "sequencer happy-path walkthrough"

# An explicitly non-approvable bundle routes to Builder without replaying
# Narrator on the unchanged reviewed generation. A later successful Builder and
# effective Reviewer make the old bundle stale and authorize exactly one fresh
# Narrator pass for the newly reviewed generation.
NOT_APPROVABLE_ROOT="$TMP/not-approvable-bundle-repair"
mkdir -p "$NOT_APPROVABLE_ROOT/factory/tickets"
cat > "$NOT_APPROVABLE_ROOT/factory/tickets/T-503.md" <<'TICKET'
# T-503
State: Review
reviewer round 1: APPROVE
TICKET
{
  ledger_header
  ledger_row T-503 planner
  ledger_row T-503 test-author
  ledger_row T-503 builder
  ledger_row T-503 reviewer
  ledger_row T-503 narrator
} > "$NOT_APPROVABLE_ROOT/factory/ledger.csv"
cat > "$NOT_APPROVABLE_ROOT/factory/tickets/T-503-bundle.md" <<'BUNDLE'
NOT APPROVABLE: preview deployment is missing.
# What this does
# Preview
# Screenshots
# Acceptance criteria
# Risk
# Cost
# Rollback
Approve to merge?
BUNDLE
NOT_APPROVABLE_OK=1
expect_stage "FIX builder" "$NOT_APPROVABLE_ROOT" T-503 || NOT_APPROVABLE_OK=0
ledger_row T-503 builder >> "$NOT_APPROVABLE_ROOT/factory/ledger.csv"
expect_stage "RUN reviewer" "$NOT_APPROVABLE_ROOT" T-503 || NOT_APPROVABLE_OK=0
ledger_row T-503 reviewer >> "$NOT_APPROVABLE_ROOT/factory/ledger.csv"
printf 'reviewer round 2: APPROVE\n' >> \
  "$NOT_APPROVABLE_ROOT/factory/tickets/T-503.md"
expect_stage "RUN narrator" "$NOT_APPROVABLE_ROOT" T-503 || NOT_APPROVABLE_OK=0
ledger_row T-503 narrator >> "$NOT_APPROVABLE_ROOT/factory/ledger.csv"
expect_stage "FIX builder" "$NOT_APPROVABLE_ROOT" T-503 || NOT_APPROVABLE_OK=0
ledger_row T-503 narrator >> "$NOT_APPROVABLE_ROOT/factory/ledger.csv"
expect_stage "FIX builder" "$NOT_APPROVABLE_ROOT" T-503 || NOT_APPROVABLE_OK=0
[[ "$NOT_APPROVABLE_OK" -eq 1 ]] &&
  pass "sequencer binds explicit non-approvable evidence to its review generation"

# One structurally invalid, unattested bundle gets one Narrator correction.
INVALID_BUNDLE_ROOT="$TMP/invalid-bundle-retry"
mkdir -p "$INVALID_BUNDLE_ROOT/factory/tickets"
cat > "$INVALID_BUNDLE_ROOT/factory/tickets/T-502.md" <<'TICKET'
# T-502
State: Review
reviewer round 1: APPROVE
TICKET
{
  ledger_header
  ledger_row T-502 planner
  ledger_row T-502 test-author
  ledger_row T-502 builder
  ledger_row T-502 reviewer
  ledger_row T-502 narrator
} > "$INVALID_BUNDLE_ROOT/factory/ledger.csv"
cat > "$INVALID_BUNDLE_ROOT/factory/tickets/T-502-bundle.md" <<'BUNDLE'
# What this does
# Preview
# Screenshots
# Acceptance criteria
# Risk
# Rollback
Approve to merge?
BUNDLE
INVALID_BUNDLE_OK=1
expect_stage "RUN narrator" "$INVALID_BUNDLE_ROOT" T-502 || INVALID_BUNDLE_OK=0
ledger_row T-502 narrator >> "$INVALID_BUNDLE_ROOT/factory/ledger.csv"
expect_stage "AWAIT-OPERATOR semantic-round authorization required; add exact line: OPERATOR AUTHORIZATION: narrator round 3" \
  "$INVALID_BUNDLE_ROOT" T-502 || INVALID_BUNDLE_OK=0
printf '%s\n' 'OPERATOR AUTHORIZATION: narrator round 3' >> \
  "$INVALID_BUNDLE_ROOT/factory/tickets/T-502.md"
expect_stage "RUN narrator" "$INVALID_BUNDLE_ROOT" T-502 || INVALID_BUNDLE_OK=0
ledger_row T-502 narrator >> "$INVALID_BUNDLE_ROOT/factory/ledger.csv"
expect_stage "AWAIT-OPERATOR semantic-round authorization required; add exact line: OPERATOR AUTHORIZATION: narrator round 4" \
  "$INVALID_BUNDLE_ROOT" T-502 || INVALID_BUNDLE_OK=0
printf '%s\n' \
  'OPERATOR AUTHORIZATION: narrator round 4' \
  'OPERATOR AUTHORIZATION: narrator round 4' >> \
  "$INVALID_BUNDLE_ROOT/factory/tickets/T-502.md"
expect_stage "AWAIT-OPERATOR semantic-round authorization invalid; keep exactly one line: OPERATOR AUTHORIZATION: narrator round 4" \
  "$INVALID_BUNDLE_ROOT" T-502 || INVALID_BUNDLE_OK=0
sed '$d' "$INVALID_BUNDLE_ROOT/factory/tickets/T-502.md" > \
  "$TMP/narrator-authorization-ticket"
mv "$TMP/narrator-authorization-ticket" \
  "$INVALID_BUNDLE_ROOT/factory/tickets/T-502.md"
expect_stage "RUN narrator" "$INVALID_BUNDLE_ROOT" T-502 || INVALID_BUNDLE_OK=0
cat > "$INVALID_BUNDLE_ROOT/factory/tickets/T-502-bundle.md" <<'BUNDLE'
# What this does
# Preview
# Screenshots
# Acceptance criteria
# Risk
# Cost
# Rollback
Approve to merge?
BUNDLE
expect_stage "AWAIT-OPERATOR" "$INVALID_BUNDLE_ROOT" T-502 || INVALID_BUNDLE_OK=0
[[ "$INVALID_BUNDLE_OK" -eq 1 ]] &&
  pass "sequencer requires one exact authorization for every later Narrator correction"

# A sealed base refresh invalidates the old reviewer approval and narrator.
REFRESH_ROOT="$TMP/refresh-sequence"
write_envelope "$REFRESH_ROOT"
cat > "$REFRESH_ROOT/factory/tickets/T-510.md" <<TICKET
# T-510
State: Awaiting Approval
Kit-SHA: $KIT_SHA
reviewer round 1: APPROVE
OPERATOR NOTE: reviewer run 2 void — duplicate
TICKET
{
  ledger_header
  ledger_row T-510 planner
  ledger_row T-510 test-author
  ledger_row T-510 builder
  ledger_row T-510 reviewer
  ledger_row T-510 reviewer
  ledger_row T-510 narrator
} > "$REFRESH_ROOT/factory/ledger.csv"
git -C "$REFRESH_ROOT" add factory
git -C "$REFRESH_ROOT" -c user.name=test -c user.email=test@example.com \
  commit -qm "pre-refresh evidence"
REFRESH_OLD_HEAD="$(git -C "$REFRESH_ROOT" rev-parse HEAD)"
REFRESH_BRANCH="$(git -C "$REFRESH_ROOT" branch --show-current)"
git -C "$REFRESH_ROOT" checkout -qb refresh-base
printf 'protected base advanced\n' > "$REFRESH_ROOT/base.txt"
git -C "$REFRESH_ROOT" add base.txt
git -C "$REFRESH_ROOT" -c user.name=test -c user.email=test@example.com \
  commit -qm "protected base"
REFRESH_BASE_HEAD="$(git -C "$REFRESH_ROOT" rev-parse HEAD)"
git -C "$REFRESH_ROOT" checkout -q "$REFRESH_BRANCH"
git -C "$REFRESH_ROOT" -c user.name=test -c user.email=test@example.com \
  merge -q --no-ff refresh-base -m "refresh protected base"
REFRESH_MERGE_HEAD="$(git -C "$REFRESH_ROOT" rev-parse HEAD)"
mkdir -p "$REFRESH_ROOT/factory/attestations/T-510"
python3 - "$REFRESH_ROOT/factory/attestations/T-510/refresh.json" \
  "$REFRESH_OLD_HEAD" "$REFRESH_BASE_HEAD" "$REFRESH_MERGE_HEAD" <<'PY'
import json
import sys
json.dump({
    "schema": "nysa.software-factory.ticket-refresh/v1",
    "ticket": "T-510",
    "generation": 1,
    "old_head": sys.argv[2],
    "base_head": sys.argv[3],
    "merge_head": sys.argv[4],
    "prior_reviewer_runs": 1,
    "prior_approve_verdicts": 1,
    "prior_request_changes_verdicts": 0,
    "prior_narrator_runs": 1,
    "prior_bundle_blob": None,
    "prior_approval_blob": None,
    "refreshed_at": "2026-07-21T12:00:00Z",
}, open(sys.argv[1], "w", encoding="utf-8"), sort_keys=True)
PY
sed 's/^State: Awaiting Approval$/State: Review/' \
  "$REFRESH_ROOT/factory/tickets/T-510.md" > "$TMP/T-510-refreshed.md"
mv "$TMP/T-510-refreshed.md" "$REFRESH_ROOT/factory/tickets/T-510.md"
git -C "$REFRESH_ROOT" add factory/attestations/T-510/refresh.json \
  factory/tickets/T-510.md
git -C "$REFRESH_ROOT" -c user.name=test -c user.email=test@example.com \
  commit -qm "record refresh receipt"
REFRESH_COMMIT="$(git -C "$REFRESH_ROOT" rev-parse HEAD)"
cp "$REFRESH_ROOT/factory/attestations/T-510/refresh.json" "$TMP/refresh.json"
REFRESH_OK=1
expect_stage "RUN reviewer" "$REFRESH_ROOT" T-510 || REFRESH_OK=0

# Equal verdict totals cannot hide reordering across the refresh boundary.
git -C "$REFRESH_ROOT" checkout -qb swapped-refresh-verdict "$REFRESH_COMMIT"
sed 's/^reviewer round 1: APPROVE$/reviewer round 1: REQUEST CHANGES — forged/' \
  "$REFRESH_ROOT/factory/tickets/T-510.md" > "$TMP/T-510-swapped.md"
printf 'reviewer round 2: APPROVE\n' >> "$TMP/T-510-swapped.md"
mv "$TMP/T-510-swapped.md" "$REFRESH_ROOT/factory/tickets/T-510.md"
git -C "$REFRESH_ROOT" add factory/tickets/T-510.md
git -C "$REFRESH_ROOT" -c user.name=test -c user.email=test@example.com \
  commit -qm "swap old and new reviewer verdicts"
expect_stage "REFUSE old reviewer verdict" "$REFRESH_ROOT" T-510 || REFRESH_OK=0
git -C "$REFRESH_ROOT" checkout -q "$REFRESH_BRANCH"

# Removing an authenticated old duplicate-run note cannot turn that row fresh.
git -C "$REFRESH_ROOT" checkout -qb removed-refresh-void "$REFRESH_COMMIT"
grep -v '^OPERATOR NOTE: reviewer run 2 void — duplicate$' \
  "$REFRESH_ROOT/factory/tickets/T-510.md" > "$TMP/T-510-no-void.md"
mv "$TMP/T-510-no-void.md" "$REFRESH_ROOT/factory/tickets/T-510.md"
git -C "$REFRESH_ROOT" add factory/tickets/T-510.md
git -C "$REFRESH_ROOT" -c user.name=test -c user.email=test@example.com \
  commit -qm "remove old reviewer void note"
expect_stage "REFUSE old reviewer verdict or void-note sequence" \
  "$REFRESH_ROOT" T-510 || REFRESH_OK=0
git -C "$REFRESH_ROOT" checkout -q "$REFRESH_BRANCH"

# A new ledger row cannot reuse a manifest bound to the pre-refresh head.
ledger_row_run T-510 reviewer forged-refresh-reviewer >> \
  "$REFRESH_ROOT/factory/ledger.csv"
write_run_manifest "$REFRESH_ROOT" T-510 reviewer forged-refresh-reviewer \
  "$REFRESH_OLD_HEAD"
printf 'reviewer round 2: APPROVE\n' >> \
  "$REFRESH_ROOT/factory/tickets/T-510.md"
expect_stage "REFUSE post-refresh run manifest" "$REFRESH_ROOT" T-510 || REFRESH_OK=0
sed '$d' "$REFRESH_ROOT/factory/ledger.csv" > "$TMP/refresh-ledger-clean.csv"
mv "$TMP/refresh-ledger-clean.csv" "$REFRESH_ROOT/factory/ledger.csv"
sed '$d' "$REFRESH_ROOT/factory/tickets/T-510.md" > "$TMP/refresh-ticket-clean.md"
mv "$TMP/refresh-ticket-clean.md" "$REFRESH_ROOT/factory/tickets/T-510.md"
rm "$REFRESH_ROOT/factory/runs/forged-refresh-reviewer.meta"

# Exact topology cannot authenticate forged zero baselines; the old ticket can.
git -C "$REFRESH_ROOT" checkout -qb forged-refresh "$REFRESH_MERGE_HEAD"
mkdir -p "$REFRESH_ROOT/factory/attestations/T-510"
cp "$TMP/refresh.json" "$REFRESH_ROOT/factory/attestations/T-510/refresh.json"
python3 - "$REFRESH_ROOT/factory/attestations/T-510/refresh.json" <<'PY'
import json
import sys
path = sys.argv[1]
value = json.load(open(path, encoding="utf-8"))
for name in ("prior_reviewer_runs", "prior_approve_verdicts", "prior_narrator_runs"):
    value[name] = 0
json.dump(value, open(path, "w", encoding="utf-8"), sort_keys=True)
PY
sed 's/^State: Awaiting Approval$/State: Review/' \
  "$REFRESH_ROOT/factory/tickets/T-510.md" > "$TMP/T-510-forged.md"
mv "$TMP/T-510-forged.md" "$REFRESH_ROOT/factory/tickets/T-510.md"
git -C "$REFRESH_ROOT" add factory/attestations/T-510/refresh.json \
  factory/tickets/T-510.md
git -C "$REFRESH_ROOT" -c user.name=test -c user.email=test@example.com \
  commit -qm "forge zero refresh baselines"
expect_stage "REFUSE refresh receipt baselines do not match" \
  "$REFRESH_ROOT" T-510 || REFRESH_OK=0
git -C "$REFRESH_ROOT" checkout -q "$REFRESH_BRANCH"

# The same receipt on a history that does not contain its merge is stale.
git -C "$REFRESH_ROOT" checkout -qb stale-refresh "$REFRESH_OLD_HEAD"
mkdir -p "$REFRESH_ROOT/factory/attestations/T-510"
cp "$TMP/refresh.json" "$REFRESH_ROOT/factory/attestations/T-510/refresh.json"
sed 's/^State: Awaiting Approval$/State: Review/' \
  "$REFRESH_ROOT/factory/tickets/T-510.md" > "$TMP/T-510-stale.md"
mv "$TMP/T-510-stale.md" "$REFRESH_ROOT/factory/tickets/T-510.md"
git -C "$REFRESH_ROOT" add factory/attestations/T-510/refresh.json \
  factory/tickets/T-510.md
git -C "$REFRESH_ROOT" -c user.name=test -c user.email=test@example.com \
  commit -qm "stale refresh receipt"
expect_stage "REFUSE stale refresh receipt" "$REFRESH_ROOT" T-510 || REFRESH_OK=0
git -C "$REFRESH_ROOT" checkout -q "$REFRESH_BRANCH"

# A narrator completed before the fresh reviewer cannot satisfy narration.
ledger_row_run T-510 narrator refresh-narrator-before >> \
  "$REFRESH_ROOT/factory/ledger.csv"
write_run_manifest "$REFRESH_ROOT" T-510 narrator refresh-narrator-before \
  "$REFRESH_COMMIT"
expect_stage "RUN reviewer" "$REFRESH_ROOT" T-510 || REFRESH_OK=0
ledger_row_run T-510 reviewer refresh-reviewer >> "$REFRESH_ROOT/factory/ledger.csv"
write_run_manifest "$REFRESH_ROOT" T-510 reviewer refresh-reviewer "$REFRESH_COMMIT"
expect_stage "REFUSE reviewer has" "$REFRESH_ROOT" T-510 || REFRESH_OK=0
printf 'reviewer round 2: APPROVE\n' >> \
  "$REFRESH_ROOT/factory/tickets/T-510.md"
git -C "$REFRESH_ROOT" add factory/tickets/T-510.md
git -C "$REFRESH_ROOT" -c user.name=test -c user.email=test@example.com \
  commit -qm "fresh reviewer approval"
expect_stage "RUN narrator" "$REFRESH_ROOT" T-510 || REFRESH_OK=0
REFRESH_REVIEW_HEAD="$(git -C "$REFRESH_ROOT" rev-parse HEAD)"
ledger_row_run T-510 narrator forged-refresh-narrator >> \
  "$REFRESH_ROOT/factory/ledger.csv"
write_run_manifest "$REFRESH_ROOT" T-510 narrator forged-refresh-narrator \
  "$REFRESH_OLD_HEAD"
expect_stage "REFUSE post-refresh run manifest" "$REFRESH_ROOT" T-510 || REFRESH_OK=0
sed '$d' "$REFRESH_ROOT/factory/ledger.csv" > "$TMP/refresh-ledger-clean.csv"
mv "$TMP/refresh-ledger-clean.csv" "$REFRESH_ROOT/factory/ledger.csv"
rm "$REFRESH_ROOT/factory/runs/forged-refresh-narrator.meta"
ledger_row_run T-510 narrator refresh-narrator-after >> \
  "$REFRESH_ROOT/factory/ledger.csv"
write_run_manifest "$REFRESH_ROOT" T-510 narrator refresh-narrator-after \
  "$REFRESH_REVIEW_HEAD"
cat > "$REFRESH_ROOT/factory/tickets/T-510-bundle.md" <<'BUNDLE'
# What this does
# Preview
# Screenshots
# Acceptance criteria
# Risk
# Cost
# Rollback
Approve to merge?
BUNDLE
expect_stage "AWAIT-OPERATOR" "$REFRESH_ROOT" T-510 || REFRESH_OK=0
[[ "$REFRESH_OK" -eq 1 ]] && pass "refresh requires fresh review then fresh narration"

# A Review ticket whose reset is byte-for-byte a no-op legitimately commits
# only the refresh receipt.
NOOP_REFRESH_ROOT="$TMP/noop-refresh-sequence"
write_envelope "$NOOP_REFRESH_ROOT"
cat > "$NOOP_REFRESH_ROOT/factory/tickets/T-511.md" <<TICKET
# T-511
State: Review
Kit-SHA: $KIT_SHA
- [ ] Evidence bundle posted
- [ ] Operator approved
TICKET
{
  ledger_header
  ledger_row T-511 planner
  ledger_row T-511 test-author
  ledger_row T-511 builder
} > "$NOOP_REFRESH_ROOT/factory/ledger.csv"
git -C "$NOOP_REFRESH_ROOT" add factory
git -C "$NOOP_REFRESH_ROOT" -c user.name=test -c user.email=test@example.com \
  commit -qm "pre-refresh Review ticket"
NOOP_OLD_HEAD="$(git -C "$NOOP_REFRESH_ROOT" rev-parse HEAD)"
NOOP_BRANCH="$(git -C "$NOOP_REFRESH_ROOT" branch --show-current)"
git -C "$NOOP_REFRESH_ROOT" checkout -qb noop-refresh-base
printf 'new protected base\n' > "$NOOP_REFRESH_ROOT/base.txt"
printf '\nProtected-main note: harmless context.\n' >> \
  "$NOOP_REFRESH_ROOT/factory/tickets/T-511.md"
git -C "$NOOP_REFRESH_ROOT" add base.txt factory/tickets/T-511.md
git -C "$NOOP_REFRESH_ROOT" -c user.name=test -c user.email=test@example.com \
  commit -qm "advance protected base"
NOOP_BASE_HEAD="$(git -C "$NOOP_REFRESH_ROOT" rev-parse HEAD)"
git -C "$NOOP_REFRESH_ROOT" checkout -q "$NOOP_BRANCH"
git -C "$NOOP_REFRESH_ROOT" -c user.name=test -c user.email=test@example.com \
  merge -q --no-ff noop-refresh-base -m "refresh protected base"
NOOP_MERGE_HEAD="$(git -C "$NOOP_REFRESH_ROOT" rev-parse HEAD)"
mkdir -p "$NOOP_REFRESH_ROOT/factory/attestations/T-511"
python3 - "$NOOP_REFRESH_ROOT/factory/attestations/T-511/refresh.json" \
  "$NOOP_OLD_HEAD" "$NOOP_BASE_HEAD" "$NOOP_MERGE_HEAD" <<'PY'
import json
import sys
json.dump({
    "schema": "nysa.software-factory.ticket-refresh/v1",
    "ticket": "T-511",
    "generation": 1,
    "old_head": sys.argv[2],
    "base_head": sys.argv[3],
    "merge_head": sys.argv[4],
    "prior_reviewer_runs": 0,
    "prior_approve_verdicts": 0,
    "prior_request_changes_verdicts": 0,
    "prior_narrator_runs": 0,
    "prior_bundle_blob": None,
    "prior_approval_blob": None,
    "refreshed_at": "2026-07-21T12:00:00Z",
}, open(sys.argv[1], "w", encoding="utf-8"), sort_keys=True)
PY
git -C "$NOOP_REFRESH_ROOT" add factory/attestations/T-511/refresh.json
git -C "$NOOP_REFRESH_ROOT" -c user.name=test -c user.email=test@example.com \
  commit -qm "record no-op refresh receipt"
if expect_stage "RUN reviewer" "$NOOP_REFRESH_ROOT" T-511; then
  pass "refresh accepts an authenticated no-op Review ticket reset"
fi

# Successor qualification holds two sealed run reservations for one exact
# receipt-bound Reviewer/Narrator revalidation. A legacy or absent receipt
# cannot spend those slots.
REFRESH_RESERVE_ROOT="$TMP/refresh-revalidation-reserve"
write_envelope "$REFRESH_RESERVE_ROOT"
cat > "$REFRESH_RESERVE_ROOT/factory/QUALIFICATION.json" <<JSON
{"budget_usd":"300.000000","capacity":3,"contract_version":"2.0.0","factory_sha":"$KIT_SHA","generation":1,"mode":"successor","per_run_budget_usd":"10.000000","per_ticket_budget_usd":"100.000000","schema":"nysa.software-factory.qualification/v2","source_factory_sha":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","target_done":3,"tickets":["T-540","T-541","T-542"]}
JSON
cat > "$REFRESH_RESERVE_ROOT/factory/tickets/T-540.md" <<TICKET
# T-540
State: Review
Kit-SHA: $KIT_SHA
SPEC-LINT: PASS
- [ ] Evidence bundle posted
- [ ] Operator approved
TICKET
git -C "$REFRESH_RESERVE_ROOT" add factory
git -C "$REFRESH_RESERVE_ROOT" -c user.name=test -c user.email=test@example.com \
  commit -qm "reserve protected-base revalidation"
RESERVE_OLD_HEAD="$(git -C "$REFRESH_RESERVE_ROOT" rev-parse HEAD)"
RESERVE_BRANCH="$(git -C "$REFRESH_RESERVE_ROOT" branch --show-current)"
git -C "$REFRESH_RESERVE_ROOT" checkout -qb reserve-base
printf 'semantic protected advance\n' > "$REFRESH_RESERVE_ROOT/product.txt"
git -C "$REFRESH_RESERVE_ROOT" add product.txt
git -C "$REFRESH_RESERVE_ROOT" -c user.name=test -c user.email=test@example.com \
  commit -qm "advance protected product"
RESERVE_BASE_HEAD="$(git -C "$REFRESH_RESERVE_ROOT" rev-parse HEAD)"
git -C "$REFRESH_RESERVE_ROOT" checkout -q "$RESERVE_BRANCH"
git -C "$REFRESH_RESERVE_ROOT" -c user.name=test -c user.email=test@example.com \
  merge -q --no-ff reserve-base -m "refresh protected product"
RESERVE_MERGE_HEAD="$(git -C "$REFRESH_RESERVE_ROOT" rev-parse HEAD)"
mkdir -p "$REFRESH_RESERVE_ROOT/factory/attestations/T-540"
python3 - "$REFRESH_RESERVE_ROOT/factory/attestations/T-540/refresh.json" \
  "$RESERVE_OLD_HEAD" "$RESERVE_BASE_HEAD" "$RESERVE_MERGE_HEAD" \
  "$KIT_SHA" <<'PY'
import json
import sys
json.dump({
    "schema": "nysa.software-factory.ticket-refresh/v2",
    "ticket": "T-540", "generation": 1,
    "old_head": sys.argv[2], "base_head": sys.argv[3],
    "merge_head": sys.argv[4], "prior_reviewer_runs": 0,
    "prior_approve_verdicts": 0, "prior_request_changes_verdicts": 0,
    "prior_narrator_runs": 0, "prior_bundle_blob": None,
    "prior_approval_blob": None, "revalidation_budget_micro_usd": 20000000,
    "revalidation_factory_sha": sys.argv[5], "revalidation_generation": 1,
    "refreshed_at": "2026-08-08T12:00:00Z",
}, open(sys.argv[1], "w", encoding="utf-8"), sort_keys=True)
PY
git -C "$REFRESH_RESERVE_ROOT" add factory/attestations/T-540/refresh.json
git -C "$REFRESH_RESERVE_ROOT" -c user.name=test -c user.email=test@example.com \
  commit -qm "bind one revalidation reserve"
RESERVE_V2_HEAD="$(git -C "$REFRESH_RESERVE_ROOT" rev-parse HEAD)"
git -C "$REFRESH_RESERVE_ROOT" branch legacy-reserve "$RESERVE_MERGE_HEAD"
for role in planner spec-linter test-author builder; do
  run_id="reserve-$role"
  ledger_row_run T-540 "$role" "$run_id" >> \
    "$REFRESH_RESERVE_ROOT/factory/ledger.csv"
  write_run_manifest "$REFRESH_RESERVE_ROOT" T-540 "$role" "$run_id" \
    "$RESERVE_OLD_HEAD"
  printf 'effective_cost=10.000000\nkit_sha=%s\n' "$KIT_SHA" >> \
    "$REFRESH_RESERVE_ROOT/factory/runs/$run_id.meta"
done
for attempt in 1 2 3 4; do
  cat > "$REFRESH_RESERVE_ROOT/factory/runs/reserve-failed-$attempt.meta" <<META
run_id=reserve-failed-$attempt
ticket=T-540
role=builder
accounting_state=completed
effective_cost=10.000000
exit_status=5
role_exit=budget
kit_sha=$KIT_SHA
META
done
REFRESH_RESERVE_OK=1
TEST_CONTRACT_VERSION=2.0.0 expect_stage \
  "RUN reviewer" "$REFRESH_RESERVE_ROOT" T-540 || REFRESH_RESERVE_OK=0
TEST_CONTRACT_VERSION=2.0.0 expect_stage \
  "RUN reviewer" "$REFRESH_RESERVE_ROOT" T-540 || REFRESH_RESERVE_OK=0
cat > "$REFRESH_RESERVE_ROOT/factory/runs/reserve-reviewer-failed.meta" <<META
run_id=reserve-reviewer-failed
ticket=T-540
role=reviewer
accounting_state=completed
effective_cost=10.000000
exit_status=5
role_exit=budget
kit_sha=$KIT_SHA
META
TEST_CONTRACT_VERSION=2.0.0 expect_stage \
  "AWAIT_BUDGET protected-base revalidation budget reserved" \
  "$REFRESH_RESERVE_ROOT" T-540 || REFRESH_RESERVE_OK=0
rm "$REFRESH_RESERVE_ROOT/factory/runs/reserve-reviewer-failed.meta"
cp "$REFRESH_RESERVE_ROOT/factory/ledger.csv" "$TMP/reserve-ledger.csv"
git -C "$REFRESH_RESERVE_ROOT" checkout -q -- factory/ledger.csv

# A later protected advance must retain the first reservation generation.
# Neither a discontinuous receipt nor a same-Factory reset may unlock it.
git -C "$REFRESH_RESERVE_ROOT" checkout -q reserve-base
printf 'second semantic protected advance\n' >> \
  "$REFRESH_RESERVE_ROOT/product.txt"
git -C "$REFRESH_RESERVE_ROOT" add product.txt
git -C "$REFRESH_RESERVE_ROOT" -c user.name=test -c user.email=test@example.com \
  commit -qm "advance protected product again"
RESERVE_SECOND_BASE_HEAD="$(git -C "$REFRESH_RESERVE_ROOT" rev-parse HEAD)"
git -C "$REFRESH_RESERVE_ROOT" checkout -q "$RESERVE_BRANCH"
git -C "$REFRESH_RESERVE_ROOT" -c user.name=test -c user.email=test@example.com \
  merge -q --no-ff reserve-base -m "refresh protected product again"
RESERVE_SECOND_MERGE_HEAD="$(git -C "$REFRESH_RESERVE_ROOT" rev-parse HEAD)"
python3 - "$REFRESH_RESERVE_ROOT/factory/attestations/T-540/refresh.json" \
  "$RESERVE_V2_HEAD" "$RESERVE_SECOND_BASE_HEAD" \
  "$RESERVE_SECOND_MERGE_HEAD" <<'PY'
import json
import sys
path = sys.argv[1]
value = json.load(open(path, encoding="utf-8"))
value.update({
    "generation": 999,
    "old_head": sys.argv[2],
    "base_head": sys.argv[3],
    "merge_head": sys.argv[4],
    "revalidation_generation": 998,
})
json.dump(value, open(path, "w", encoding="utf-8"), sort_keys=True)
PY
git -C "$REFRESH_RESERVE_ROOT" add factory/attestations/T-540/refresh.json
git -C "$REFRESH_RESERVE_ROOT" -c user.name=test -c user.email=test@example.com \
  commit -qm "forge discontinuous refresh reserve"
TEST_CONTRACT_VERSION=2.0.0 expect_stage \
  "REFUSE malformed refresh receipt" "$REFRESH_RESERVE_ROOT" T-540 || \
  REFRESH_RESERVE_OK=0

git -C "$REFRESH_RESERVE_ROOT" checkout -qb reserve-reset \
  "$RESERVE_SECOND_MERGE_HEAD"
python3 - "$REFRESH_RESERVE_ROOT/factory/attestations/T-540/refresh.json" \
  "$RESERVE_V2_HEAD" "$RESERVE_SECOND_BASE_HEAD" \
  "$RESERVE_SECOND_MERGE_HEAD" <<'PY'
import json
import sys
path = sys.argv[1]
value = json.load(open(path, encoding="utf-8"))
value.update({
    "generation": 2,
    "old_head": sys.argv[2],
    "base_head": sys.argv[3],
    "merge_head": sys.argv[4],
    "revalidation_generation": 2,
})
json.dump(value, open(path, "w", encoding="utf-8"), sort_keys=True)
PY
git -C "$REFRESH_RESERVE_ROOT" add factory/attestations/T-540/refresh.json
git -C "$REFRESH_RESERVE_ROOT" -c user.name=test -c user.email=test@example.com \
  commit -qm "forge reset refresh reserve"
TEST_CONTRACT_VERSION=2.0.0 expect_stage \
  "REFUSE malformed refresh receipt" "$REFRESH_RESERVE_ROOT" T-540 || \
  REFRESH_RESERVE_OK=0

git -C "$REFRESH_RESERVE_ROOT" checkout -q legacy-reserve
mkdir -p "$REFRESH_RESERVE_ROOT/factory/attestations/T-540"
git -C "$REFRESH_RESERVE_ROOT" show \
  "$RESERVE_V2_HEAD:factory/attestations/T-540/refresh.json" > \
  "$REFRESH_RESERVE_ROOT/factory/attestations/T-540/refresh.json" || \
  REFRESH_RESERVE_OK=0
cp "$TMP/reserve-ledger.csv" "$REFRESH_RESERVE_ROOT/factory/ledger.csv"
python3 - "$REFRESH_RESERVE_ROOT/factory/attestations/T-540/refresh.json" <<'PY'
import json
import sys
path = sys.argv[1]
value = json.load(open(path, encoding="utf-8"))
value["schema"] = "nysa.software-factory.ticket-refresh/v1"
for key in (
    "revalidation_budget_micro_usd", "revalidation_factory_sha",
    "revalidation_generation",
):
    value.pop(key)
json.dump(value, open(path, "w", encoding="utf-8"), sort_keys=True)
PY
git -C "$REFRESH_RESERVE_ROOT" add factory/attestations/T-540/refresh.json
git -C "$REFRESH_RESERVE_ROOT" -c user.name=test -c user.email=test@example.com \
  commit -qm "legacy refresh has no reserve" || REFRESH_RESERVE_OK=0
TEST_CONTRACT_VERSION=2.0.0 expect_stage \
  "AWAIT_BUDGET protected-base revalidation budget reserved" \
  "$REFRESH_RESERVE_ROOT" T-540 || REFRESH_RESERVE_OK=0
if [[ "$REFRESH_RESERVE_OK" -eq 1 ]]; then
  pass "successor refresh reserve is exact-head, role-bound, and one candidate"
fi

# Control-only refreshes may preserve only role evidence that belongs to the
# receipt-bound old head. Auditable runs on a discarded force-pushed lineage
# must not advance the replacement branch.
ORPHAN_REFRESH_ROOT="$TMP/orphan-control-refresh"
write_envelope "$ORPHAN_REFRESH_ROOT"
cat > "$ORPHAN_REFRESH_ROOT/factory/tickets/T-512.md" <<TICKET
# T-512
State: Review
Kit-SHA: $KIT_SHA
reviewer round 1: APPROVE
- [ ] Evidence bundle posted
- [ ] Operator approved
TICKET
printf '{}\n' > "$ORPHAN_REFRESH_ROOT/factory/QUALIFICATION.json"
{
  ledger_header
  ledger_row T-512 planner
  ledger_row T-512 test-author
  ledger_row T-512 builder
  ledger_row_run T-512 reviewer orphan-reviewer
  ledger_row_run T-512 narrator orphan-narrator
} > "$ORPHAN_REFRESH_ROOT/factory/ledger.csv"
git -C "$ORPHAN_REFRESH_ROOT" add factory
git -C "$ORPHAN_REFRESH_ROOT" -c user.name=test -c user.email=test@example.com \
  commit -qm "pre-refresh orphan evidence"
ORPHAN_OLD_HEAD="$(git -C "$ORPHAN_REFRESH_ROOT" rev-parse HEAD)"
ORPHAN_BRANCH="$(git -C "$ORPHAN_REFRESH_ROOT" branch --show-current)"
ORPHAN_PARENT="$(git -C "$ORPHAN_REFRESH_ROOT" rev-parse HEAD^)"
ORPHAN_TREE="$(git -C "$ORPHAN_REFRESH_ROOT" rev-parse 'HEAD^{tree}')"
ORPHAN_REVIEW_HEAD="$(printf '%s\n' 'orphan reviewer' | \
  git -C "$ORPHAN_REFRESH_ROOT" -c user.name=test -c user.email=test@example.com \
  commit-tree "$ORPHAN_TREE" -p "$ORPHAN_PARENT")"
ORPHAN_NARRATOR_HEAD="$(printf '%s\n' 'orphan narrator' | \
  git -C "$ORPHAN_REFRESH_ROOT" -c user.name=test -c user.email=test@example.com \
  commit-tree "$ORPHAN_TREE" -p "$ORPHAN_REVIEW_HEAD")"
write_run_manifest "$ORPHAN_REFRESH_ROOT" T-512 reviewer orphan-reviewer \
  "$ORPHAN_REVIEW_HEAD"
write_run_manifest "$ORPHAN_REFRESH_ROOT" T-512 narrator orphan-narrator \
  "$ORPHAN_NARRATOR_HEAD"
git -C "$ORPHAN_REFRESH_ROOT" checkout -qb orphan-control-base
printf '{"generation":2}\n' > "$ORPHAN_REFRESH_ROOT/factory/QUALIFICATION.json"
mkdir -p "$ORPHAN_REFRESH_ROOT/factory/migrations/inflight-release"
printf '{}\n' > \
  "$ORPHAN_REFRESH_ROOT/factory/migrations/inflight-release/$KIT_SHA.json"
git -C "$ORPHAN_REFRESH_ROOT" add factory
git -C "$ORPHAN_REFRESH_ROOT" -c user.name=test -c user.email=test@example.com \
  commit -qm "advance protected control metadata"
ORPHAN_BASE_HEAD="$(git -C "$ORPHAN_REFRESH_ROOT" rev-parse HEAD)"
git -C "$ORPHAN_REFRESH_ROOT" checkout -q "$ORPHAN_BRANCH"
git -C "$ORPHAN_REFRESH_ROOT" -c user.name=test -c user.email=test@example.com \
  merge -q --no-ff orphan-control-base -m "refresh protected base"
ORPHAN_MERGE_HEAD="$(git -C "$ORPHAN_REFRESH_ROOT" rev-parse HEAD)"
mkdir -p "$ORPHAN_REFRESH_ROOT/factory/attestations/T-512"
python3 - "$ORPHAN_REFRESH_ROOT/factory/attestations/T-512/refresh.json" \
  "$ORPHAN_OLD_HEAD" "$ORPHAN_BASE_HEAD" "$ORPHAN_MERGE_HEAD" <<'PY'
import json
import sys
json.dump({
    "schema": "nysa.software-factory.ticket-refresh/v1",
    "ticket": "T-512",
    "generation": 1,
    "old_head": sys.argv[2],
    "base_head": sys.argv[3],
    "merge_head": sys.argv[4],
    "prior_reviewer_runs": 1,
    "prior_approve_verdicts": 1,
    "prior_request_changes_verdicts": 0,
    "prior_narrator_runs": 1,
    "prior_bundle_blob": None,
    "prior_approval_blob": None,
    "refreshed_at": "2026-07-21T12:00:00Z",
}, open(sys.argv[1], "w", encoding="utf-8"), sort_keys=True)
PY
git -C "$ORPHAN_REFRESH_ROOT" add \
  factory/attestations/T-512/refresh.json
git -C "$ORPHAN_REFRESH_ROOT" -c user.name=test -c user.email=test@example.com \
  commit -qm "record orphan control refresh"
if TEST_CONTRACT_VERSION=2.0.0 \
   expect_stage "RUN reviewer" "$ORPHAN_REFRESH_ROOT" T-512; then
  pass "control-only refresh invalidates orphaned role evidence"
fi

COMMITTED_APPROVAL_ROOT="$TMP/committed-approval"
write_envelope "$COMMITTED_APPROVAL_ROOT"
cat > "$COMMITTED_APPROVAL_ROOT/factory/tickets/T-242.md" <<'TICKET'
# T-242
State: Approved
SPEC-LINT: PASS
reviewer round 1: APPROVE
Operator-Approval: Linear
TICKET
{
  ledger_header
  ledger_row T-242 planner
  ledger_row T-242 spec-linter
  ledger_row T-242 test-author
  ledger_row T-242 builder
  ledger_row T-242 reviewer
  ledger_row T-242 narrator
} > "$COMMITTED_APPROVAL_ROOT/factory/ledger.csv"
if expect_stage \
  "REFUSE contract 1.2 has no trusted bundle-attestation path for approval" \
  "$COMMITTED_APPROVAL_ROOT" T-242; then
  pass "sequencer refuses approval until the bundle-attestation path exists"
fi

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

# Spec-lint fail → replan → lint → pass; later failures keep repairing.
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
if expect_stage "AWAIT-OPERATOR semantic-round authorization required" "$WALK" T-503; then
  printf 'OPERATOR AUTHORIZATION: spec-linter round 3\n' >> \
    "$WALK/factory/tickets/T-503.md"
  if expect_stage "RUN planner" "$WALK" T-503; then
    printf 'OPERATOR AUTHORIZATION: spec-linter round 3\n' >> \
      "$WALK/factory/tickets/T-503.md"
    if expect_stage "AWAIT-OPERATOR semantic-round authorization invalid" "$WALK" T-503; then
      pass "spec-lint round-three authorization is exact and unambiguous"
    fi
  fi
fi
fi

# Successful mutating roles must commit cleanly; the wrapper owns the push.
setup_role_exit_fixture() {
  local ticket="$1" authorized_role="${2:-planner}"
  local fixture_parent="${ROLE_EXIT_PARENT:-$TMP}"
  ROLE_EXIT_ROOT="$fixture_parent/role-exit-$ticket"
  ROLE_EXIT_WORKTREE="$fixture_parent/role-exit-$ticket-worktree"
  ROLE_EXIT_REMOTE="$fixture_parent/role-exit-$ticket.git"
  write_envelope "$ROLE_EXIT_ROOT"
  write_ticket "$ROLE_EXIT_ROOT" "$ticket"
  case "$authorized_role" in
    planner) ;;
    spec-linter)
      ledger_row "$ticket" planner >>"$ROLE_EXIT_ROOT/factory/ledger.csv"
      ;;
    test-author)
      ledger_row "$ticket" planner >>"$ROLE_EXIT_ROOT/factory/ledger.csv"
      ledger_row "$ticket" spec-linter >>"$ROLE_EXIT_ROOT/factory/ledger.csv"
      printf 'SPEC-LINT: PASS\n' >>"$ROLE_EXIT_ROOT/factory/tickets/$ticket.md"
      ;;
    builder)
      ledger_row "$ticket" planner >>"$ROLE_EXIT_ROOT/factory/ledger.csv"
      ledger_row "$ticket" spec-linter >>"$ROLE_EXIT_ROOT/factory/ledger.csv"
      ledger_row "$ticket" test-author >>"$ROLE_EXIT_ROOT/factory/ledger.csv"
      printf 'SPEC-LINT: PASS\n' >>"$ROLE_EXIT_ROOT/factory/tickets/$ticket.md"
      ;;
    *) fail "invalid role-exit fixture stage: $authorized_role" ;;
  esac
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

install_ticket_mode_hooks() {
  local ticket="$1" shape="$2" hook target
  hook="$(git -C "$ROLE_EXIT_WORKTREE" rev-parse --git-path hooks/pre-commit)"
  mkdir -p "$(dirname "$hook")"
  {
    printf '%s\n' '#!/usr/bin/env bash' 'set -eu'
    printf 'printf "Provider note: committed by mock\\n" >> %q\n' \
      "$ROLE_EXIT_WORKTREE/factory/tickets/$ticket.md"
    printf 'git -C %q add %q\n' "$ROLE_EXIT_WORKTREE" \
      "factory/tickets/$ticket.md"
    if [[ "$shape" == "index-executable" ]]; then
      printf 'git -C %q update-index --chmod=+x -- %q\n' \
        "$ROLE_EXIT_WORKTREE" "factory/tickets/$ticket.md"
    fi
  } >"$hook"
  chmod +x "$hook"
  hook="$(git -C "$ROLE_EXIT_WORKTREE" rev-parse --git-path hooks/post-commit)"
  target="$(git -C "$ROLE_EXIT_WORKTREE" rev-parse --git-path "provider-ticket-$ticket")"
  {
    printf '%s\n' '#!/usr/bin/env bash' 'set -eu'
    case "$shape" in
      0600|0664|0755|index-executable)
        [[ "$shape" != "index-executable" ]] || shape=0644
        printf 'chmod %s %q\n' "$shape" \
          "$ROLE_EXIT_WORKTREE/factory/tickets/$ticket.md"
        ;;
      hardlink)
        printf 'cp %q %q\nrm %q\nln %q %q\n' \
          "$ROLE_EXIT_WORKTREE/factory/tickets/$ticket.md" "$target" \
          "$ROLE_EXIT_WORKTREE/factory/tickets/$ticket.md" "$target" \
          "$ROLE_EXIT_WORKTREE/factory/tickets/$ticket.md"
        ;;
      symlink)
        printf 'cp %q %q\nrm %q\nln -s %q %q\n' \
          "$ROLE_EXIT_WORKTREE/factory/tickets/$ticket.md" "$target" \
          "$ROLE_EXIT_WORKTREE/factory/tickets/$ticket.md" "$target" \
          "$ROLE_EXIT_WORKTREE/factory/tickets/$ticket.md"
        ;;
      *) fail "invalid ticket-mode hook shape: $shape" ;;
    esac
  } >"$hook"
  chmod +x "$hook"
}

if [[ "$SUBSET" == "role-exit-git" ]]; then
setup_role_exit_fixture T-606
git -C "$ROLE_EXIT_ROOT" checkout -q --detach HEAD
DETACHED_REGISTERED_STATUS=0
MOCK_COMMIT_WORKDIR=1 FACTORY_ROOT="$ROLE_EXIT_ROOT" \
  FACTORY_GLOBAL_ENV="$TMP/no-global.env" FACTORY_TEST_MODE=1 \
  FACTORY_TEST_ENFORCE_ROLE_EXIT=1 \
  FACTORY_CERTIFIED_PRODUCT_ORIGIN="$ROLE_EXIT_REMOTE" \
  FACTORY_ADAPTER_OVERRIDE=mock \
  "$RUN_AGENT" --role planner --ticket T-606 --workdir "$ROLE_EXIT_WORKTREE" -- \
    "detached registered checkout" > "$TMP/detached-registered.out" 2>&1 ||
  DETACHED_REGISTERED_STATUS=$?
if [[ "$DETACHED_REGISTERED_STATUS" -eq 0 ]] &&
   grep -q '^task_submitted=1$' "$ROLE_EXIT_ROOT/factory/runs/"*.meta &&
   grep -q 'mock adapter ran task' "$ROLE_EXIT_ROOT/factory/runs/"*.out; then
  pass "detached registered checkout permits branch-bound ticket work"
else
  fail "detached registered checkout permits branch-bound ticket work" \
    "status=$DETACHED_REGISTERED_STATUS"
fi

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
  FACTORY_CERTIFIED_PRODUCT_ORIGIN="$ROLE_EXIT_REMOTE" \
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
ROLE_EMPTY_TARGET="$TMP/role-empty-external.txt"
printf 'product-owned target\n' > "$ROLE_EMPTY_TARGET"
ln -s "$ROLE_EMPTY_TARGET" "$ROLE_EXIT_WORKTREE/mock-role-output.txt"
git -C "$ROLE_EXIT_WORKTREE" add mock-role-output.txt
git -C "$ROLE_EXIT_WORKTREE" -c user.name=test -c user.email=test@example.com \
  commit -qm "product-owned mock path"
git -C "$ROLE_EXIT_WORKTREE" push -q origin ticket/T-600
ROLE_NO_COMMIT=0
FACTORY_ROOT="$ROLE_EXIT_ROOT" FACTORY_GLOBAL_ENV="$TMP/no-global.env" \
  FACTORY_TEST_MODE=1 FACTORY_TEST_ENFORCE_ROLE_EXIT=1 \
  FACTORY_CERTIFIED_PRODUCT_ORIGIN="$ROLE_EXIT_REMOTE" \
  FACTORY_ADAPTER_OVERRIDE=mock \
  "$RUN_AGENT" --role planner --ticket T-600 --workdir "$ROLE_EXIT_WORKTREE" -- "no commit" \
  > "$TMP/role-no-commit.out" 2>&1 || ROLE_NO_COMMIT=$?
ROLE_COMMIT=0
MOCK_COMMIT_EMPTY=1 FACTORY_ROOT="$ROLE_EXIT_ROOT" \
  FACTORY_GLOBAL_ENV="$TMP/no-global.env" FACTORY_TEST_MODE=1 \
  FACTORY_CERTIFIED_PRODUCT_ORIGIN="$ROLE_EXIT_REMOTE" \
  FACTORY_TEST_ENFORCE_ROLE_EXIT=1 FACTORY_ADAPTER_OVERRIDE=mock \
  "$RUN_AGENT" --role planner --ticket T-600 --workdir "$ROLE_EXIT_WORKTREE" -- "commit" \
  > "$TMP/role-commit.out" 2>&1 || ROLE_COMMIT=$?
ROLE_LOCAL_HEAD="$(git -C "$ROLE_EXIT_WORKTREE" rev-parse HEAD)"
ROLE_REMOTE_HEAD="$(git --git-dir="$ROLE_EXIT_REMOTE" rev-parse refs/heads/ticket/T-600)"
if [[ "$ROLE_NO_COMMIT" -eq 11 && "$ROLE_COMMIT" -eq 0 &&
      "$ROLE_LOCAL_HEAD" == "$ROLE_REMOTE_HEAD" &&
      "$(cat "$ROLE_EMPTY_TARGET")" == "product-owned target" &&
      -L "$ROLE_EXIT_WORKTREE/mock-role-output.txt" ]] &&
   grep -q 'role_exit_no_commit' "$TMP/role-no-commit.out"; then
  pass "role exit accepts an empty mock commit without touching product paths"
else
  fail "role exit accepts an empty mock commit without touching product paths" \
    "no-commit=$ROLE_NO_COMMIT commit=$ROLE_COMMIT"
fi

setup_role_exit_fixture T-655
install_ticket_mode_hooks T-655 0600
ROLE_MODE_STATUS=0
mkdir -m 700 "$TMP/role-ticket-model-state"
MOCK_COMMIT_WORKDIR=1 FACTORY_ROOT="$ROLE_EXIT_ROOT" \
  FACTORY_GLOBAL_ENV="$TMP/no-global.env" FACTORY_TEST_MODE=1 \
  FACTORY_CERTIFIED_PRODUCT_ORIGIN="$ROLE_EXIT_REMOTE" \
  FACTORY_TEST_ENFORCE_ROLE_EXIT=1 FACTORY_ADAPTER_OVERRIDE=mock \
  "$RUN_AGENT" --role planner --ticket T-655 --workdir "$ROLE_EXIT_WORKTREE" -- \
    "normalize ticket mode" >"$TMP/role-ticket-mode.out" 2>&1 ||
  ROLE_MODE_STATUS=$?
if [[ "$ROLE_MODE_STATUS" -eq 0 &&
      "$(python3 -c 'import os,stat,sys; print(oct(stat.S_IMODE(os.stat(sys.argv[1]).st_mode)))' \
        "$ROLE_EXIT_WORKTREE/factory/tickets/T-655.md")" == "0o644" ]] &&
   [[ "$(git -C "$ROLE_EXIT_WORKTREE" hash-object \
        factory/tickets/T-655.md)" == \
      "$(git -C "$ROLE_EXIT_WORKTREE" rev-parse \
        HEAD:factory/tickets/T-655.md)" ]] &&
   python3 "$ROOT/scripts/model-manager.py" ticket-status \
     --state-root "$TMP/role-ticket-model-state" --project test \
     --ticket T-655 \
     --ticket-file "$ROLE_EXIT_WORKTREE/factory/tickets/T-655.md" >/dev/null &&
   git --git-dir="$ROLE_EXIT_REMOTE" show \
     refs/heads/ticket/T-655:factory/tickets/T-655.md |
     grep -Fxq 'Provider note: committed by mock'; then
  pass "role exit normalizes a provider-authored tracked ticket to 0644"
else
  fail "role exit normalizes a provider-authored tracked ticket to 0644" \
    "status=$ROLE_MODE_STATUS"
fi

for mode_case in 0664 0755 hardlink symlink index-executable; do
  case "$mode_case" in
    0664) mode_ticket=T-656 ;;
    0755) mode_ticket=T-657 ;;
    hardlink) mode_ticket=T-658 ;;
    symlink) mode_ticket=T-659 ;;
    index-executable) mode_ticket=T-660 ;;
  esac
  setup_role_exit_fixture "$mode_ticket"
  MODE_REMOTE_BEFORE="$(git --git-dir="$ROLE_EXIT_REMOTE" \
    rev-parse "refs/heads/ticket/$mode_ticket")"
  install_ticket_mode_hooks "$mode_ticket" "$mode_case"
  ROLE_MODE_STATUS=0
  MOCK_COMMIT_WORKDIR=1 FACTORY_ROOT="$ROLE_EXIT_ROOT" \
    FACTORY_GLOBAL_ENV="$TMP/no-global.env" FACTORY_TEST_MODE=1 \
    FACTORY_CERTIFIED_PRODUCT_ORIGIN="$ROLE_EXIT_REMOTE" \
    FACTORY_TEST_ENFORCE_ROLE_EXIT=1 FACTORY_ADAPTER_OVERRIDE=mock \
    "$RUN_AGENT" --role planner --ticket "$mode_ticket" \
      --workdir "$ROLE_EXIT_WORKTREE" -- "unsafe ticket mode" \
      >"$TMP/role-ticket-mode-$mode_case.out" 2>&1 || ROLE_MODE_STATUS=$?
  MODE_SHAPE_VALID=1
  if [[ "$mode_case" == "index-executable" ]] &&
     { [[ "$(git -C "$ROLE_EXIT_WORKTREE" ls-tree HEAD -- \
          "factory/tickets/$mode_ticket.md" | awk 'NR == 1 { print $1 }')" != \
          "100755" ]] ||
       [[ "$(python3 -c 'import os,stat,sys; print(oct(stat.S_IMODE(os.stat(sys.argv[1]).st_mode)))' \
          "$ROLE_EXIT_WORKTREE/factory/tickets/$mode_ticket.md")" != "0o644" ]]; }; then
    MODE_SHAPE_VALID=0
  fi
  if [[ "$ROLE_MODE_STATUS" -eq 11 &&
        "$MODE_SHAPE_VALID" -eq 1 &&
        "$(git --git-dir="$ROLE_EXIT_REMOTE" \
          rev-parse "refs/heads/ticket/$mode_ticket")" == "$MODE_REMOTE_BEFORE" ]] &&
     grep -q 'role_exit_unsafe_ticket_mode' \
       "$TMP/role-ticket-mode-$mode_case.out"; then
    pass "role exit refuses unsafe tracked ticket shape $mode_case"
  else
    fail "role exit refuses unsafe tracked ticket shape $mode_case" \
      "status=$ROLE_MODE_STATUS"
  fi
done
if grep -qF 'before.st_uid != os.geteuid()' "$ROOT/scripts/run-agent.sh"; then
  pass "role exit retains foreign-owner ticket refusal"
else
  fail "role exit retains foreign-owner ticket refusal"
fi

setup_role_exit_fixture T-654 builder
ROLE_REWRITE_INPUT="$(git -C "$ROLE_EXIT_WORKTREE" rev-parse HEAD)"
ROLE_REWRITE_STATUS=0
MOCK_COMMIT_WORKDIR=1 MOCK_REWRITE_WORKDIR=1 \
  FACTORY_ROOT="$ROLE_EXIT_ROOT" FACTORY_GLOBAL_ENV="$TMP/no-global.env" \
  FACTORY_TEST_MODE=1 FACTORY_TEST_ENFORCE_ROLE_EXIT=1 \
  FACTORY_CERTIFIED_PRODUCT_ORIGIN="$ROLE_EXIT_REMOTE" \
  FACTORY_ADAPTER_OVERRIDE=mock \
  "$RUN_AGENT" --role builder --ticket T-654 --workdir "$ROLE_EXIT_WORKTREE" -- \
    "rewrite history" >"$TMP/role-history-rewrite.out" 2>&1 ||
  ROLE_REWRITE_STATUS=$?
ROLE_REWRITE_META="$(ls "$ROLE_EXIT_ROOT"/factory/runs/*.meta)"
ROLE_REWRITE_RUN_ID="$(sed -n 's/^run_id=//p' "$ROLE_REWRITE_META")"
ROLE_REWRITE_REF="refs/factory/failed-role/T-654/$ROLE_REWRITE_RUN_ID"
ROLE_REWRITE_OUTPUT="$(git -C "$ROLE_EXIT_WORKTREE" rev-parse \
  "$ROLE_REWRITE_REF" 2>/dev/null || true)"
if [[ "$ROLE_REWRITE_STATUS" -eq 11 &&
      "$(git -C "$ROLE_EXIT_WORKTREE" rev-parse HEAD)" == \
        "$ROLE_REWRITE_INPUT" &&
      "$(git --git-dir="$ROLE_EXIT_REMOTE" rev-parse \
        refs/heads/ticket/T-654)" == "$ROLE_REWRITE_INPUT" &&
      -z "$(git -C "$ROLE_EXIT_WORKTREE" status --porcelain=v1)" &&
      -n "$ROLE_REWRITE_OUTPUT" &&
      "$ROLE_REWRITE_OUTPUT" != "$ROLE_REWRITE_INPUT" ]] &&
   ! git -C "$ROLE_EXIT_WORKTREE" merge-base --is-ancestor \
      "$ROLE_REWRITE_INPUT" "$ROLE_REWRITE_OUTPUT" &&
   grep -q 'role_exit_history_rewritten' "$TMP/role-history-rewrite.out" &&
   grep -q '^phase=completed$' "$ROLE_REWRITE_META" &&
   grep -q '^accounting_state=abandoned_conservative$' "$ROLE_REWRITE_META" &&
   grep -q '^go_issued=1$' "$ROLE_REWRITE_META" &&
   grep -q '^task_submitted=1$' "$ROLE_REWRITE_META" &&
   grep -q '^effective_cost=1.00$' "$ROLE_REWRITE_META" &&
   grep -q '^exit_status=11$' "$ROLE_REWRITE_META" &&
   grep -q '^role_exit=role_exit_history_rewritten$' "$ROLE_REWRITE_META" &&
   grep -q "^role_head_before=$ROLE_REWRITE_INPUT$" "$ROLE_REWRITE_META"; then
  pass "non-Test-author history rewrite is quarantined and restored before push"
else
  fail "history rewrite did not preserve output and restore authenticated input" \
    "status=$ROLE_REWRITE_STATUS run=$ROLE_REWRITE_RUN_ID"
fi

setup_role_exit_fixture T-650
ROLE_REAL_GIT="$(type -P git)"
ROLE_GIT_COUNT="$TMP/role-git-count"
ROLE_GIT_WRAPPER="$TMP/git"
cat > "$ROLE_GIT_WRAPPER" <<'STUB'
#!/usr/bin/env bash
if [[ " $* " == *" ls-remote "* ]]; then
  count=0
  [[ ! -f "$FACTORY_TEST_GIT_COUNT" ]] ||
    count="$(cat "$FACTORY_TEST_GIT_COUNT")"
  count=$((count + 1))
  printf '%s\n' "$count" > "$FACTORY_TEST_GIT_COUNT"
  [[ "$count" != 3 ]] || exit 128
fi
exec "$FACTORY_TEST_GIT_REAL" "$@"
STUB
chmod +x "$ROLE_GIT_WRAPPER"
ROLE_REMOTE_RETRY=0
PATH="$TMP:$PATH" FACTORY_TEST_GIT_REAL="$ROLE_REAL_GIT" \
  FACTORY_TEST_GIT_COUNT="$ROLE_GIT_COUNT" \
  MOCK_COMMIT_WORKDIR=1 FACTORY_ROOT="$ROLE_EXIT_ROOT" \
  FACTORY_GLOBAL_ENV="$TMP/no-global.env" FACTORY_TEST_MODE=1 \
  FACTORY_CERTIFIED_PRODUCT_ORIGIN="$ROLE_EXIT_REMOTE" \
  FACTORY_TEST_ENFORCE_ROLE_EXIT=1 FACTORY_ADAPTER_OVERRIDE=mock \
  "$RUN_AGENT" --role planner --ticket T-650 --workdir "$ROLE_EXIT_WORKTREE" -- \
    "transient remote read" > "$TMP/role-remote-retry.out" 2>&1 ||
  ROLE_REMOTE_RETRY=$?
if [[ "$ROLE_REMOTE_RETRY" -eq 0 &&
      "$(cat "$ROLE_GIT_COUNT")" == "5" &&
      "$(git -C "$ROLE_EXIT_WORKTREE" rev-parse HEAD)" == \
        "$(git --git-dir="$ROLE_EXIT_REMOTE" rev-parse refs/heads/ticket/T-650)" ]]; then
  pass "role exit retries one failed remote observation without replay"
else
  fail "role exit retries one failed remote observation without replay" \
    "status=$ROLE_REMOTE_RETRY reads=$(cat "$ROLE_GIT_COUNT" 2>/dev/null || true)"
fi

ROLE_LANE_ROOT="$TMP/nysa-sf-dev.role-output"
mkdir -p "$ROLE_LANE_ROOT/product"
printf '%s\n' '{"mode":"product"}' >"$ROLE_LANE_ROOT/marker.json"
ROLE_EXIT_PARENT="$ROLE_LANE_ROOT/product"
setup_role_exit_fixture T-649 spec-linter
ROLE_LANE_REMOTE_BEFORE="$(git --git-dir="$ROLE_EXIT_REMOTE" \
  rev-parse refs/heads/ticket/T-649)"
ROLE_LANE_LEAK_STATUS=0
MOCK_SPEC_LINT_VERDICT='FAIL — /private/tmp/nysa-sf-dev.stale/runtime/db.env' \
  FACTORY_CLI_LANE_ROOT="$ROLE_LANE_ROOT" FACTORY_ROOT="$ROLE_EXIT_ROOT" \
  FACTORY_GLOBAL_ENV="$TMP/no-global.env" FACTORY_TEST_MODE=1 \
  FACTORY_CERTIFIED_PRODUCT_ORIGIN="$ROLE_EXIT_REMOTE" \
  FACTORY_TEST_ENFORCE_ROLE_EXIT=1 FACTORY_ADAPTER_OVERRIDE=mock \
  "$RUN_AGENT" --role spec-linter --ticket T-649 \
    --workdir "$ROLE_EXIT_WORKTREE" -- "lane path leak" \
    >"$TMP/role-lane-path.out" 2>&1 || ROLE_LANE_LEAK_STATUS=$?
ROLE_LANE_REMOTE_AFTER="$(git --git-dir="$ROLE_EXIT_REMOTE" \
  rev-parse refs/heads/ticket/T-649)"
if [[ "$ROLE_LANE_LEAK_STATUS" -eq 11 &&
      "$ROLE_LANE_REMOTE_BEFORE" == "$ROLE_LANE_REMOTE_AFTER" ]] &&
   grep -q 'role_exit_lane_path_leak' "$TMP/role-lane-path.out" &&
   grep -q '^role_exit=role_exit_lane_path_leak$' \
     "$ROLE_EXIT_ROOT/factory/runs/"*.meta; then
  pass "development role output cannot push a lane-local absolute path"
else
  fail "development role output cannot push a lane-local absolute path" \
    "status=$ROLE_LANE_LEAK_STATUS"
fi

setup_role_exit_fixture T-650
ROLE_PORTABLE_STATUS=0
MOCK_COMMIT_WORKDIR=1 FACTORY_CLI_LANE_ROOT="$ROLE_LANE_ROOT" \
  FACTORY_ROOT="$ROLE_EXIT_ROOT" FACTORY_GLOBAL_ENV="$TMP/no-global.env" \
  FACTORY_TEST_MODE=1 FACTORY_CERTIFIED_PRODUCT_ORIGIN="$ROLE_EXIT_REMOTE" \
  FACTORY_TEST_ENFORCE_ROLE_EXIT=1 FACTORY_ADAPTER_OVERRIDE=mock \
  "$RUN_AGENT" --role planner --ticket T-650 --workdir "$ROLE_EXIT_WORKTREE" -- \
    "portable role output" >"$TMP/role-portable.out" 2>&1 ||
  ROLE_PORTABLE_STATUS=$?
if [[ "$ROLE_PORTABLE_STATUS" -eq 0 &&
      "$(git -C "$ROLE_EXIT_WORKTREE" rev-parse HEAD)" == \
      "$(git --git-dir="$ROLE_EXIT_REMOTE" rev-parse refs/heads/ticket/T-650)" ]]; then
  pass "development role sentinel accepts portable output"
else
  fail "development role sentinel accepts portable output" \
    "status=$ROLE_PORTABLE_STATUS"
fi
unset ROLE_EXIT_PARENT
fi

if [[ "$SUBSET" == "role-exit-policy" ]]; then
# Contract 1.7 roles may commit a blocker without completing their stage.
setup_role_exit_fixture T-643 builder
ROLE_BLOCKED_STATUS=0
MOCK_COMMIT_WORKDIR=1 FACTORY_ROOT="$ROLE_EXIT_ROOT" \
  FACTORY_GLOBAL_ENV="$TMP/no-global.env" FACTORY_TEST_MODE=1 \
  FACTORY_CONTRACT_VERSION=1.7.0 \
  FACTORY_CERTIFIED_PRODUCT_ORIGIN="$ROLE_EXIT_REMOTE" \
  FACTORY_TEST_ENFORCE_ROLE_EXIT=1 FACTORY_ADAPTER_OVERRIDE=mock \
  "$RUN_AGENT" --role builder --ticket T-643 --workdir "$ROLE_EXIT_WORKTREE" -- \
    $'blocked\nROLE-ESCALATE: CONTRACT-BLOCKED' \
  > "$TMP/role-contract-blocked.out" 2>&1 || ROLE_BLOCKED_STATUS=$?
ROLE_BLOCKED_META=("$ROLE_EXIT_ROOT"/factory/runs/*.meta)
ROLE_BLOCKED_LOCAL="$(git -C "$ROLE_EXIT_WORKTREE" rev-parse HEAD)"
ROLE_BLOCKED_REMOTE="$(git --git-dir="$ROLE_EXIT_REMOTE" rev-parse refs/heads/ticket/T-643)"
ROLE_BLOCKED_STAGE="$(FACTORY_ROOT="$ROLE_EXIT_ROOT" \
  FACTORY_LEDGER="$ROLE_EXIT_ROOT/factory/runtime-ledger.csv" \
  FACTORY_CONTRACT_VERSION=1.7.0 \
  "$NEXT_STAGE" --ticket T-643 --workdir "$ROLE_EXIT_WORKTREE" 2>/dev/null || true)"
if [[ "$ROLE_BLOCKED_STATUS" -eq 12 &&
      "$ROLE_BLOCKED_LOCAL" == "$ROLE_BLOCKED_REMOTE" &&
      "$ROLE_BLOCKED_STAGE" == "RUN builder" ]] &&
   grep -q '^phase=completed$' "${ROLE_BLOCKED_META[0]}" &&
   grep -q '^accounting_state=completed$' "${ROLE_BLOCKED_META[0]}" &&
   grep -q '^exit_status=12$' "${ROLE_BLOCKED_META[0]}" &&
   grep -q '^role_exit=role_exit_contract_blocked$' "${ROLE_BLOCKED_META[0]}"; then
  pass "contract blocker is pushed and accounted without completing the role"
else
  fail "contract blocker did not remain a non-successful durable role result" \
    "status=$ROLE_BLOCKED_STATUS next=$ROLE_BLOCKED_STAGE"
fi

install_contract_blocker_hook() {
  local ticket="$1" marker="$2" count="${3:-1}" hook
  hook="$(git -C "$ROLE_EXIT_WORKTREE" rev-parse --git-path hooks/pre-commit)"
  mkdir -p "$(dirname "$hook")"
  {
    printf '%s\n' '#!/usr/bin/env bash' 'set -eu'
    printf 'for _ in $(seq 1 %s); do printf "%%s\\\\n" %q >> %q; done\n' \
      "$count" "$marker" "$ROLE_EXIT_WORKTREE/factory/tickets/$ticket.md"
    printf 'git -C %q add %q\n' "$ROLE_EXIT_WORKTREE" "factory/tickets/$ticket.md"
  } >"$hook"
  chmod +x "$hook"
}

# The committed ticket marker is the durable fallback when a provider omits
# the matching terminal-response marker.
setup_role_exit_fixture T-651 test-author
install_contract_blocker_hook T-651 'ROLE-ESCALATE: CONTRACT-BLOCKED'
ROLE_DURABLE_BLOCKED=0
MOCK_COMMIT_WORKDIR=1 FACTORY_ROOT="$ROLE_EXIT_ROOT" \
  FACTORY_GLOBAL_ENV="$TMP/no-global.env" FACTORY_TEST_MODE=1 \
  FACTORY_CONTRACT_VERSION=1.7.0 \
  FACTORY_CERTIFIED_PRODUCT_ORIGIN="$ROLE_EXIT_REMOTE" \
  FACTORY_TEST_ENFORCE_ROLE_EXIT=1 FACTORY_ADAPTER_OVERRIDE=mock \
  "$RUN_AGENT" --role test-author --ticket T-651 --workdir "$ROLE_EXIT_WORKTREE" -- \
    "durable blocker" >"$TMP/role-durable-contract-blocked.out" 2>&1 ||
  ROLE_DURABLE_BLOCKED=$?
ROLE_DURABLE_META=("$ROLE_EXIT_ROOT"/factory/runs/*.meta)
if [[ "$ROLE_DURABLE_BLOCKED" -eq 12 ]] &&
   grep -q '^role_exit=role_exit_contract_blocked$' "${ROLE_DURABLE_META[0]}" &&
   git --git-dir="$ROLE_EXIT_REMOTE" show \
     refs/heads/ticket/T-651:factory/tickets/T-651.md |
     grep -Fxq 'ROLE-ESCALATE: CONTRACT-BLOCKED'; then
  pass "durable ticket marker stops a contract-blocked role"
else
  fail "durable ticket marker did not stop the role" \
    "status=$ROLE_DURABLE_BLOCKED"
fi

for durable_case in duplicate wrong-role; do
  case "$durable_case" in
    duplicate)
      durable_ticket=T-652
      durable_role=builder
      durable_count=2
      ;;
    wrong-role)
      durable_ticket=T-653
      durable_role=spec-linter
      durable_count=1
      ;;
  esac
  setup_role_exit_fixture "$durable_ticket" "$durable_role"
  install_contract_blocker_hook "$durable_ticket" \
    'ROLE-ESCALATE: CONTRACT-BLOCKED' "$durable_count"
  ROLE_BAD_DURABLE=0
  MOCK_COMMIT_WORKDIR=1 FACTORY_ROOT="$ROLE_EXIT_ROOT" \
    FACTORY_GLOBAL_ENV="$TMP/no-global.env" FACTORY_TEST_MODE=1 \
    FACTORY_CONTRACT_VERSION=1.7.0 \
    FACTORY_CERTIFIED_PRODUCT_ORIGIN="$ROLE_EXIT_REMOTE" \
    FACTORY_TEST_ENFORCE_ROLE_EXIT=1 FACTORY_ADAPTER_OVERRIDE=mock \
    "$RUN_AGENT" --role "$durable_role" --ticket "$durable_ticket" \
      --workdir "$ROLE_EXIT_WORKTREE" -- "invalid durable blocker" \
    >"$TMP/role-durable-$durable_case.out" 2>&1 || ROLE_BAD_DURABLE=$?
  if [[ "$ROLE_BAD_DURABLE" -eq 11 ]] &&
     grep -q 'role_exit_invalid_escalation' \
       "$TMP/role-durable-$durable_case.out"; then
    pass "durable contract blocker refuses $durable_case marker"
  else
    fail "durable contract blocker accepted $durable_case marker" \
      "status=$ROLE_BAD_DURABLE"
  fi
done

setup_role_exit_fixture T-644
ROLE_BLOCKED_NO_COMMIT=0
FACTORY_ROOT="$ROLE_EXIT_ROOT" FACTORY_GLOBAL_ENV="$TMP/no-global.env" \
  FACTORY_TEST_MODE=1 FACTORY_CONTRACT_VERSION=1.7.0 \
  FACTORY_CERTIFIED_PRODUCT_ORIGIN="$ROLE_EXIT_REMOTE" \
  FACTORY_TEST_ENFORCE_ROLE_EXIT=1 FACTORY_ADAPTER_OVERRIDE=mock \
  "$RUN_AGENT" --role planner --ticket T-644 --workdir "$ROLE_EXIT_WORKTREE" -- \
    $'blocked\nROLE-ESCALATE: CONTRACT-BLOCKED' \
  > "$TMP/role-contract-blocked-no-commit.out" 2>&1 ||
  ROLE_BLOCKED_NO_COMMIT=$?
if [[ "$ROLE_BLOCKED_NO_COMMIT" -eq 11 ]] &&
   grep -q 'role_exit_no_commit' "$TMP/role-contract-blocked-no-commit.out"; then
  pass "contract blocker still requires a durable commit"
else
  fail "contract blocker bypassed the durable commit requirement" \
    "status=$ROLE_BLOCKED_NO_COMMIT"
fi

for escalation_case in malformed duplicate wrong-role; do
  case "$escalation_case" in
    malformed)
      escalation_ticket=T-645
      escalation_role=planner
      escalation_task=$'blocked\nROLE-ESCALATE: CONTRACT'
      ;;
    duplicate)
      escalation_ticket=T-646
      escalation_role=test-author
      escalation_task=$'blocked\nROLE-ESCALATE: CONTRACT-BLOCKED\nROLE-ESCALATE: CONTRACT-BLOCKED'
      ;;
    wrong-role)
      escalation_ticket=T-647
      escalation_role=spec-linter
      escalation_task=$'blocked\nROLE-ESCALATE: CONTRACT-BLOCKED'
      ;;
  esac
  setup_role_exit_fixture "$escalation_ticket" "$escalation_role"
  ROLE_BAD_ESCALATION=0
  MOCK_COMMIT_WORKDIR=1 FACTORY_ROOT="$ROLE_EXIT_ROOT" \
    FACTORY_GLOBAL_ENV="$TMP/no-global.env" FACTORY_TEST_MODE=1 \
    FACTORY_CONTRACT_VERSION=1.7.0 \
    FACTORY_CERTIFIED_PRODUCT_ORIGIN="$ROLE_EXIT_REMOTE" \
    FACTORY_TEST_ENFORCE_ROLE_EXIT=1 FACTORY_ADAPTER_OVERRIDE=mock \
    "$RUN_AGENT" --role "$escalation_role" --ticket "$escalation_ticket" \
      --workdir "$ROLE_EXIT_WORKTREE" -- "$escalation_task" \
    > "$TMP/role-escalation-$escalation_case.out" 2>&1 ||
    ROLE_BAD_ESCALATION=$?
  if [[ "$ROLE_BAD_ESCALATION" -eq 11 ]] &&
     grep -q 'role_exit_invalid_escalation' \
       "$TMP/role-escalation-$escalation_case.out"; then
    pass "contract blocker refuses $escalation_case output"
  else
    fail "contract blocker accepted $escalation_case output" \
      "status=$ROLE_BAD_ESCALATION"
  fi
done

setup_role_exit_fixture T-648 builder
ROLE_LEGACY_COMMIT=0
MOCK_COMMIT_WORKDIR=1 FACTORY_ROOT="$ROLE_EXIT_ROOT" \
  FACTORY_GLOBAL_ENV="$TMP/no-global.env" FACTORY_TEST_MODE=1 \
  FACTORY_CONTRACT_VERSION=1.6.0 \
  FACTORY_CERTIFIED_PRODUCT_ORIGIN="$ROLE_EXIT_REMOTE" \
  FACTORY_TEST_ENFORCE_ROLE_EXIT=1 FACTORY_ADAPTER_OVERRIDE=mock \
  "$RUN_AGENT" --role builder --ticket T-648 --workdir "$ROLE_EXIT_WORKTREE" -- \
    "ordinary legacy commit" > "$TMP/role-legacy-commit.out" 2>&1 ||
  ROLE_LEGACY_COMMIT=$?
if [[ "$ROLE_LEGACY_COMMIT" -eq 0 ]]; then
  pass "contract 1.6 ordinary role completion is unchanged"
else
  fail "contract blocker changed ordinary contract 1.6 completion" \
    "status=$ROLE_LEGACY_COMMIT"
fi

setup_role_exit_fixture T-642
ROLE_UNTRACKED_STATUS=0
MOCK_MUTATE_WORKDIR_UNTRACKED=1 FACTORY_ROOT="$ROLE_EXIT_ROOT" \
  FACTORY_GLOBAL_ENV="$TMP/no-global.env" FACTORY_TEST_MODE=1 \
  FACTORY_TEST_ENFORCE_ROLE_EXIT=1 FACTORY_ADAPTER_OVERRIDE=mock \
  FACTORY_CERTIFIED_PRODUCT_ORIGIN="$ROLE_EXIT_REMOTE" \
  "$RUN_AGENT" --role planner --ticket T-642 --workdir "$ROLE_EXIT_WORKTREE" -- \
    "untracked mutation" > "$TMP/role-untracked.out" 2>&1 || ROLE_UNTRACKED_STATUS=$?
if [[ "$ROLE_UNTRACKED_STATUS" -eq 11 &&
      -f "$ROLE_EXIT_WORKTREE/provider-untracked.txt" ]] &&
   grep -q 'role_exit_dirty' "$TMP/role-untracked.out"; then
  pass "role exit detects untracked mutation despite Git config"
else
  fail "role exit detects untracked mutation despite Git config" \
    "status=$ROLE_UNTRACKED_STATUS"
fi

setup_role_exit_fixture T-610
ROLE_PROTECTED_BEFORE="$(git -C "$ROLE_EXIT_WORKTREE" rev-parse HEAD)"
ROLE_PROTECTED_STATUS=0
MOCK_PROTECTED_TICKET_MUTATION=1 FACTORY_ROOT="$ROLE_EXIT_ROOT" \
  FACTORY_GLOBAL_ENV="$TMP/no-global.env" FACTORY_TEST_MODE=1 \
  FACTORY_TEST_ENFORCE_ROLE_EXIT=1 FACTORY_ADAPTER_OVERRIDE=mock \
  FACTORY_CERTIFIED_PRODUCT_ORIGIN="$ROLE_EXIT_REMOTE" \
  "$RUN_AGENT" --role planner --ticket T-610 --workdir "$ROLE_EXIT_WORKTREE" -- \
    "protected mutation" > "$TMP/role-protected.out" 2>&1 ||
  ROLE_PROTECTED_STATUS=$?
ROLE_PROTECTED_LOCAL="$(git -C "$ROLE_EXIT_WORKTREE" rev-parse HEAD)"
ROLE_PROTECTED_REMOTE="$(git --git-dir="$ROLE_EXIT_REMOTE" rev-parse refs/heads/ticket/T-610)"
ROLE_PROTECTED_META="$(ls "$ROLE_EXIT_ROOT/factory/runs/"*.meta)"
ROLE_PROTECTED_RUN="$(awk -F= '$1=="run_id" {print $2}' "$ROLE_PROTECTED_META")"
ROLE_PROTECTED_DIAGNOSTIC="$(git -C "$ROLE_EXIT_WORKTREE" rev-parse \
  "refs/factory/failed-role/T-610/$ROLE_PROTECTED_RUN")"
if [[ "$ROLE_PROTECTED_STATUS" -eq 11 &&
      "$ROLE_PROTECTED_LOCAL" == "$ROLE_PROTECTED_BEFORE" &&
      "$ROLE_PROTECTED_REMOTE" == "$ROLE_PROTECTED_BEFORE" &&
      "$ROLE_PROTECTED_DIAGNOSTIC" != "$ROLE_PROTECTED_BEFORE" &&
      -z "$(git -C "$ROLE_EXIT_WORKTREE" status --porcelain)" ]] &&
   git -C "$ROLE_EXIT_WORKTREE" show \
     "$ROLE_PROTECTED_DIAGNOSTIC:factory/tickets/T-610.md" |
     grep -q '^State: Done$' &&
   git -C "$ROLE_EXIT_WORKTREE" show \
     "$ROLE_PROTECTED_DIAGNOSTIC:factory/tickets/T-610.md" |
     grep -q '^Operator-Approval: Receipt$' &&
   grep -q 'role_exit_protected_ticket_mutation' "$TMP/role-protected.out" &&
   grep -q '^role_exit=role_exit_protected_ticket_mutation$' "$ROLE_PROTECTED_META" &&
   grep -q '^effective_cost=0.42$' "$ROLE_PROTECTED_META" &&
   grep -q '^exit_status=11$' "$ROLE_PROTECTED_META" &&
   [[ "$(awk -F, '$3=="T-610" {print $8 ":" $9}' \
      "$ROLE_EXIT_ROOT/factory/runtime-ledger.csv")" == "0.42:11" ]]; then
  pass "role exit preserves protected-field mutation without push or advancement"
else
  fail "role exit preserves protected-field mutation without push or advancement" \
    "status=$ROLE_PROTECTED_STATUS local=$ROLE_PROTECTED_LOCAL"
fi

setup_role_exit_fixture T-611
ROLE_SPEC_FORGE_BEFORE="$(git -C "$ROLE_EXIT_WORKTREE" rev-parse HEAD)"
ROLE_SPEC_FORGE_STATUS=0
MOCK_SPEC_LINT_VERDICT=PASS FACTORY_ROOT="$ROLE_EXIT_ROOT" \
  FACTORY_GLOBAL_ENV="$TMP/no-global.env" FACTORY_TEST_MODE=1 \
  FACTORY_TEST_ENFORCE_ROLE_EXIT=1 FACTORY_ADAPTER_OVERRIDE=mock \
  FACTORY_CERTIFIED_PRODUCT_ORIGIN="$ROLE_EXIT_REMOTE" \
  "$RUN_AGENT" --role planner --ticket T-611 --workdir "$ROLE_EXIT_WORKTREE" -- \
    "forged lint" > "$TMP/role-spec-forge.out" 2>&1 ||
  ROLE_SPEC_FORGE_STATUS=$?
if [[ "$ROLE_SPEC_FORGE_STATUS" -eq 11 &&
      "$(git --git-dir="$ROLE_EXIT_REMOTE" rev-parse refs/heads/ticket/T-611)" == \
        "$ROLE_SPEC_FORGE_BEFORE" ]] &&
   grep -q 'role_exit_protected_ticket_mutation' "$TMP/role-spec-forge.out"; then
  pass "non-linter role cannot forge spec-lint history"
else
  fail "non-linter role cannot forge spec-lint history" \
    "status=$ROLE_SPEC_FORGE_STATUS"
fi

setup_role_exit_fixture T-612
{
  ledger_header
  ledger_row T-612 planner
} > "$ROLE_EXIT_ROOT/factory/ledger.csv"
ROLE_SPEC_APPEND_STATUS=0
MOCK_SPEC_LINT_VERDICT=PASS FACTORY_ROOT="$ROLE_EXIT_ROOT" \
  FACTORY_GLOBAL_ENV="$TMP/no-global.env" FACTORY_TEST_MODE=1 \
  FACTORY_TEST_ENFORCE_ROLE_EXIT=1 FACTORY_ADAPTER_OVERRIDE=mock \
  FACTORY_CERTIFIED_PRODUCT_ORIGIN="$ROLE_EXIT_REMOTE" \
  "$RUN_AGENT" --role spec-linter --ticket T-612 --workdir "$ROLE_EXIT_WORKTREE" -- \
    "canonical lint" > "$TMP/role-spec-append.out" 2>&1 ||
  ROLE_SPEC_APPEND_STATUS=$?
if [[ "$ROLE_SPEC_APPEND_STATUS" -eq 0 &&
      "$(git --git-dir="$ROLE_EXIT_REMOTE" rev-parse refs/heads/ticket/T-612)" == \
        "$(git -C "$ROLE_EXIT_WORKTREE" rev-parse HEAD)" &&
      "$(grep -c '^SPEC-LINT: PASS$' \
        "$ROLE_EXIT_WORKTREE/factory/tickets/T-612.md")" == "1" ]]; then
  pass "spec-linter may append exactly one canonical verdict"
else
  fail "spec-linter may append exactly one canonical verdict" \
    "status=$ROLE_SPEC_APPEND_STATUS"
fi

setup_role_exit_fixture T-609
ROLE_ENV_DECOY="$TMP/role-env-decoy.git"
ROLE_ENV_GIT_MARKER="$TMP/role-env-git-invoked"
git init --bare -q "$ROLE_ENV_DECOY"
ROLE_ENV_BEFORE="$(git -C "$ROLE_EXIT_WORKTREE" rev-parse HEAD)"
{
  printf 'export FACTORY_CERTIFIED_PRODUCT_ORIGIN=%q\n' "$ROLE_ENV_DECOY"
  printf 'export FACTORY_TRUSTED_PRODUCT_ORIGIN PRODUCT_REMOTE FACTORY_TRUSTED_GIT_BIN\n'
  printf 'PRODUCT_REMOTE=%q\n' "$ROLE_ENV_DECOY"
  printf 'git() { printf invoked > %q; return 99; }; export -f git\n' "$ROLE_ENV_GIT_MARKER"
  printf 'factory_capture_product_remote() { printf "%%s\\n" %q; } 2>/dev/null || true\n' \
    "$ROLE_ENV_DECOY"
  printf 'factory_product_remote_matches() { return 0; } 2>/dev/null || true\n'
  printf 'factory_update_tracking_ref() { return 0; } 2>/dev/null || true\n'
  printf 'set -a\n'
} >> "$ROLE_EXIT_ROOT/factory/ENVELOPE.env"
ROLE_ENV_STATUS=0
MOCK_COMMIT_WORKDIR=1 FACTORY_ROOT="$ROLE_EXIT_ROOT" \
  FACTORY_GLOBAL_ENV="$TMP/no-global.env" FACTORY_TEST_MODE=1 \
  FACTORY_TEST_ENFORCE_ROLE_EXIT=1 FACTORY_ADAPTER_OVERRIDE=mock \
  FACTORY_TRUSTED_PRODUCT_ORIGIN="$ROLE_ENV_DECOY" \
  FACTORY_CERTIFIED_PRODUCT_ORIGIN="$ROLE_EXIT_REMOTE" \
  "$RUN_AGENT" --role planner --ticket T-609 --workdir "$ROLE_EXIT_WORKTREE" -- \
    "sealed origin" > "$TMP/role-env.out" 2>&1 || ROLE_ENV_STATUS=$?
if [[ "$ROLE_ENV_STATUS" -eq 3 &&
      "$(git --git-dir="$ROLE_EXIT_REMOTE" rev-parse refs/heads/ticket/T-609)" == \
        "$ROLE_ENV_BEFORE" &&
      "$(git -C "$ROLE_EXIT_WORKTREE" rev-parse HEAD)" == "$ROLE_ENV_BEFORE" &&
      ! -f "$ROLE_EXIT_ROOT/factory/runs/"*.out ]] &&
   ! git --git-dir="$ROLE_ENV_DECOY" show-ref --verify --quiet \
     refs/heads/ticket/T-609 &&
   [[ ! -e "$ROLE_ENV_GIT_MARKER" ]]; then
  pass "executable envelope content fails closed before trusted Git or adapter use"
else
  fail "executable envelope content fails closed before trusted Git or adapter use" \
    "status=$ROLE_ENV_STATUS"
fi

setup_role_exit_fixture T-608
ROLE_DESTINATION_BEFORE="$(git -C "$ROLE_EXIT_WORKTREE" rev-parse HEAD)"
ROLE_DESTINATION_DECOY="$TMP/role-exit-decoy.git"
git init --bare -q "$ROLE_DESTINATION_DECOY"
git -C "$ROLE_EXIT_WORKTREE" push -q "$ROLE_DESTINATION_DECOY" \
  HEAD:refs/heads/ticket/T-608
ROLE_DESTINATION_STATUS=0
MOCK_COMMIT_WORKDIR=1 MOCK_PUSHURL="$ROLE_DESTINATION_DECOY" \
  FACTORY_ROOT="$ROLE_EXIT_ROOT" FACTORY_GLOBAL_ENV="$TMP/no-global.env" \
  FACTORY_TEST_MODE=1 FACTORY_TEST_ENFORCE_ROLE_EXIT=1 \
  FACTORY_CERTIFIED_PRODUCT_ORIGIN="$ROLE_EXIT_REMOTE" \
  FACTORY_ADAPTER_OVERRIDE=mock \
  "$RUN_AGENT" --role planner --ticket T-608 --workdir "$ROLE_EXIT_WORKTREE" -- \
    "destination drift" > "$TMP/role-destination.out" 2>&1 ||
  ROLE_DESTINATION_STATUS=$?
if [[ "$ROLE_DESTINATION_STATUS" -eq 11 &&
      "$(git --git-dir="$ROLE_EXIT_REMOTE" rev-parse refs/heads/ticket/T-608)" == \
        "$ROLE_DESTINATION_BEFORE" &&
      "$(git --git-dir="$ROLE_DESTINATION_DECOY" rev-parse refs/heads/ticket/T-608)" == \
        "$ROLE_DESTINATION_BEFORE" ]] &&
   grep -q 'role_exit_remote_mismatch' "$TMP/role-destination.out"; then
  pass "role exit refuses a drifted product push destination"
else
  fail "role exit refuses a drifted product push destination" \
    "status=$ROLE_DESTINATION_STATUS"
fi
fi

if [[ "$SUBSET" == "model-policy" ]]; then
if grep -Eq '(^|[^[:alnum:]_])HEAD:refs/heads/' \
     "$RUN_AGENT" "$ROOT/scripts/ticket-state.sh"; then
  fail "trusted pushes use captured commit SHAs" "symbolic HEAD refspec found"
elif grep -Fq '"$ROLE_HEAD_BEFORE:refs/heads/$ROLE_BRANCH_BEFORE"' "$RUN_AGENT" &&
     grep -Fq '"$ROLE_HEAD_AFTER:refs/heads/$ROLE_BRANCH_BEFORE"' "$RUN_AGENT" &&
     grep -Fq '"$LOCAL_HEAD:refs/heads/$BRANCH"' "$ROOT/scripts/ticket-state.sh"; then
  pass "trusted pushes use captured commit SHAs"
else
  fail "trusted pushes use captured commit SHAs" "exact SHA refspec missing"
fi

# A nearly exhausted ticket reserves only its remaining budget instead of
# being refused by flat-reserve arithmetic (T-009 regression).
NEAR_CAP="$TMP/near-cap"
write_envelope "$NEAR_CAP"
{
  ledger_header
  printf '2026-07-13,06:00:00,T-620,planner,mock,test,1,19.50,1,,,,,,\n'
  printf '%s,06:00:00,T-619,planner,mock,test,1,49.50,1,,,,,,\n' \
    "$(date -u +%F)"
} > "$NEAR_CAP/factory/ledger.csv"
NEAR_CAP_STATUS=0
run_mock "$NEAR_CAP" planner T-620 > "$TMP/near-cap.out" 2>&1 || NEAR_CAP_STATUS=$?
if [[ "$NEAR_CAP_STATUS" -eq 0 ]] &&
   grep -l 'reserved_usd=0.5000' "$NEAR_CAP/factory/runs"/*.meta >/dev/null 2>&1; then
  pass "shrunken ticket reservation is used by the repo daily cap"
else
  fail "shrunken ticket reservation is used by the repo daily cap" \
    "status=$NEAR_CAP_STATUS output=$(cat "$TMP/near-cap.out")"
fi

# The same shrunken reservation is used by the machine cap and persisted in
# the unchanged global-ledger schema.
NEAR_GLOBAL="$TMP/near-global-cap"
write_envelope "$NEAR_GLOBAL"
{
  ledger_header
  printf '2026-07-13,06:00:00,T-622,planner,mock,test,1,19.50,1,,,,,,\n'
} > "$NEAR_GLOBAL/factory/ledger.csv"
NEAR_GLOBAL_DIR="$TMP/near-global-accounting"
NEAR_GLOBAL_ENV="$NEAR_GLOBAL_DIR/global.env"
NEAR_GLOBAL_LEDGER="$NEAR_GLOBAL_DIR/global-ledger.csv"
mkdir -p "$NEAR_GLOBAL_DIR"
printf 'GLOBAL_DAILY_CAP_USD=50.00\n' > "$NEAR_GLOBAL_ENV"
printf '%s\n' 'date,time,repo,ticket,role,adapter,prompt_version,turns,cost_usd,exit_status,run_id,provider_family,model_id,selection_reason,cost_basis,adapter_version' \
  > "$NEAR_GLOBAL_LEDGER"
printf '%s,06:00:00,/other,T-000,planner,mock,test,1,49.50,0,old,test,test,test,test,test\n' \
  "$(date -u +%F)" >> "$NEAR_GLOBAL_LEDGER"
write_ticket "$NEAR_GLOBAL" T-622
NEAR_GLOBAL_STATUS=0
FACTORY_ROOT="$NEAR_GLOBAL" FACTORY_GLOBAL_ENV="$NEAR_GLOBAL_ENV" \
  FACTORY_TEST_MODE=1 FACTORY_ADAPTER_OVERRIDE=mock \
  "$RUN_AGENT" --role planner --ticket T-622 -- "near global cap" \
  > "$TMP/near-global-cap.out" 2>&1 || NEAR_GLOBAL_STATUS=$?
if [[ "$NEAR_GLOBAL_STATUS" -eq 0 &&
      "$(awk -F, '$4=="T-622" {print $9}' "$NEAR_GLOBAL_LEDGER")" == "0.42" &&
      "$(awk -F, 'NR==1 {print NF}' "$NEAR_GLOBAL_LEDGER")" == "16" ]]; then
  pass "shrunken ticket reservation is used by the machine daily cap"
else
  fail "shrunken ticket reservation is used by the machine daily cap" \
    "status=$NEAR_GLOBAL_STATUS output=$(cat "$TMP/near-global-cap.out")"
fi

if python3 "$ROOT/scripts/lib/money.py" exceeds \
    --spent 0.100000000000000001 --reserve 0.200000000000000002 \
    --cap 0.300000000000000003; then
  fail "budget comparisons use exact decimal arithmetic" "equal decimals compared greater"
else
  pass "budget comparisons use exact decimal arithmetic"
fi

# A ticket at or over its cap still refuses exactly as before.
EXHAUSTED="$TMP/exhausted-cap"
write_envelope "$EXHAUSTED"
{
  ledger_header
  printf '2026-07-13,06:00:00,T-621,planner,mock,test,1,20.00,1,,,,,,\n'
} > "$EXHAUSTED/factory/ledger.csv"
EXHAUSTED_STATUS=0
EXHAUSTED_OUT="$(run_mock "$EXHAUSTED" planner T-621 2>&1)" || EXHAUSTED_STATUS=$?
if [[ "$EXHAUSTED_STATUS" -eq 5 ]] &&
   [[ "$EXHAUSTED_OUT" == *'ticket budget would be exceeded'* ]]; then
  pass "exhausted ticket budget still refuses launch"
else
  fail "exhausted ticket budget still refuses launch" \
    "status=$EXHAUSTED_STATUS output=$EXHAUSTED_OUT"
fi
fi

if [[ "$FAILURES" -gt 0 ]]; then
  echo "FAIL: $FAILURES factory-script $SUBSET test(s) failed" >&2
  exit 1
fi
