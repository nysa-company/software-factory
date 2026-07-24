#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE_ROOT="$ROOT"
LANE="${FACTORY_DEV_LANE_UNDER_TEST:-$ROOT/scripts/factory-dev-lane.sh}"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/factory-dev-lane-test.XXXXXX")"
TMP="$(cd "$TMP" && pwd -P)"
FAKE_SANDBOX="$TMP/sandbox-exec"
FAKE_CURSOR="$TMP/cursor-agent"
OUT="$TMP/out"
CALLER_HOME="$TMP/caller-home"

cleanup() {
  chmod -R u+w "$TMP" 2>/dev/null || true
  rm -rf "$TMP"
}
trap cleanup EXIT
trap 'status=$?; printf "FAIL: unexpected command at line %s (exit %s)\n" "${BASH_LINENO[0]:-$LINENO}" "$status" >&2; [[ ! -s "$OUT" ]] || sed -n "1,120p" "$OUT" >&2; exit "$status"' ERR

fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }

expect_failure() {
  local label="$1"
  shift
  if "$@" >"$OUT" 2>&1; then
    fail "$label unexpectedly succeeded"
  fi
}

test_env() {
  FACTORY_DEV_LANE_TEST_MODE=1 \
  FACTORY_TRUSTED_TEST_HARNESS=1 \
  FACTORY_DEV_LANE_UNAME=Darwin \
  FACTORY_DEV_LANE_SANDBOX_EXEC="$FAKE_SANDBOX" \
  FACTORY_DEV_LANE_ACCOUNT_HOME="$CALLER_HOME" \
  HOME="$CALLER_HOME" \
  TMPDIR="$TMP/lanes" \
  "$@"
}

cursor_env() {
  FACTORY_DEV_LANE_TEST_MODE=1 \
  FACTORY_TRUSTED_TEST_HARNESS=1 \
  FACTORY_DEV_LANE_UNAME=Darwin \
  FACTORY_DEV_LANE_SANDBOX_EXEC="$FAKE_SANDBOX" \
  FACTORY_DEV_LANE_CURSOR_BIN="$FAKE_CURSOR" \
  FACTORY_DEV_LANE_CURSOR_TMP_BRIDGE="$TMP/cursor-tmp-bridge" \
  FACTORY_DEV_LANE_CURSOR_SESSION_HOME="$CALLER_HOME" \
  HOME="$CALLER_HOME" \
  TMPDIR="$TMP/lanes" \
  "$@"
}

clean_cmd() { TMPDIR="$TMP/lanes" bash "$LANE" clean --root "$1"; }

mkdir -p "$TMP/lanes" "$CALLER_HOME/.factory" "$CALLER_HOME/.cursor" \
  "$CALLER_HOME/.hermes/profiles/factory" "$CALLER_HOME/Library/LaunchAgents" \
  "$CALLER_HOME/Projects/nysa-company/nysa-app"
printf 'factory production sentinel\n' >"$CALLER_HOME/.factory/sentinel"
printf 'profile production sentinel\n' >"$CALLER_HOME/.hermes/profiles/factory/sentinel"
printf 'service production sentinel\n' >"$CALLER_HOME/Library/LaunchAgents/sentinel"
printf 'product production sentinel\n' >"$CALLER_HOME/Projects/nysa-company/nysa-app/sentinel"
printf '{"accessToken":"test","refreshToken":"test"}\n' >"$CALLER_HOME/.cursor/auth.json"
printf '{}\n' >"$CALLER_HOME/.cursor/cli-config.json"
chmod 600 "$CALLER_HOME/.cursor/"*.json
sentinels_before="$(cksum "$CALLER_HOME/.factory/sentinel" \
  "$CALLER_HOME/.hermes/profiles/factory/sentinel" \
  "$CALLER_HOME/Library/LaunchAgents/sentinel" \
  "$CALLER_HOME/Projects/nysa-company/nysa-app/sentinel")"
cursor_session_before="$(cksum "$CALLER_HOME/.cursor/auth.json" \
  "$CALLER_HOME/.cursor/cli-config.json")"
cat >"$FAKE_SANDBOX" <<'EOF'
#!/usr/bin/env bash
set -eu
[[ "$1" == "-f" && -f "$2" ]] || exit 97
shift 2
exec "$@"
EOF
chmod +x "$FAKE_SANDBOX"
cat >"$FAKE_CURSOR" <<'EOF'
#!/usr/bin/env bash
case "${1:-}" in
  --version) printf '2026.07.17-test\n' ;;
  --help) printf '%s\n' --print --output-format --workspace --model --force --trust --sandbox ;;
  status) printf '{"authenticated":true}\n' ;;
  models) printf '%s\n' \
    gpt-5.6-sol-high claude-fable-5-thinking-medium claude-sonnet-5-thinking-high ;;
  *) printf '%s\n' "$@" >"$(dirname "$0")/cursor-args"; exit 42 ;;
esac
EOF
chmod +x "$FAKE_CURSOR"

[[ -x "$LANE" ]] || fail "development lane wrapper is not executable"
LANE_PATH_REPO="$TMP/lane-path-repo"
git init -q "$LANE_PATH_REPO"
printf '%s\n' 'source ../../runtime/product-db/T-1.env' \
  >"$LANE_PATH_REPO/contract.md"
git -C "$LANE_PATH_REPO" add contract.md
git -C "$LANE_PATH_REPO" -c user.name=Test -c user.email=test@local \
  commit -qm 'Add portable contract'
LANE_PATH_BASE="$(git -C "$LANE_PATH_REPO" rev-parse HEAD)"
printf '%s\n' 'portable role output' >>"$LANE_PATH_REPO/contract.md"
git -C "$LANE_PATH_REPO" add contract.md
git -C "$LANE_PATH_REPO" -c user.name=Test -c user.email=test@local \
  commit -qm 'Add portable role output'
python3 "$ROOT/scripts/lib/lane-path-sentinel.py" "$LANE_PATH_REPO" \
  "$LANE_PATH_BASE" HEAD ||
  fail "portable development role output was rejected"
printf '%s\n' \
  "source '/private/tmp/old lane/nysa-sf-dev.stale/runtime/product-db/T-1.env'" \
  >>"$LANE_PATH_REPO/contract.md"
git -C "$LANE_PATH_REPO" add contract.md
git -C "$LANE_PATH_REPO" -c user.name=Test -c user.email=test@local \
  commit -qm 'Add stale lane path'
expect_failure "lane-local absolute role path" \
  python3 "$ROOT/scripts/lib/lane-path-sentinel.py" "$LANE_PATH_REPO" \
    "$LANE_PATH_BASE" HEAD
subscription_plan_source="$(sed -n \
  '/^subscription_probe_and_plan()/,/^run_subscription_internal()/p' "$LANE")"
grep -Fq '"account_routes":{"lane-codex-subscription":limit(4)}' \
  <<<"$subscription_plan_source" ||
  fail "subscription canary does not grant one Codex account four slots"
grep -Fq '"adapter":"codex"' <<<"$subscription_plan_source" ||
  fail "subscription canary does not route through Codex"
if grep -Eq 'cursor-openai|claude-code|lane-cursor-subscription|lane-claude-subscription' \
  <<<"$subscription_plan_source"; then
  fail "subscription canary retained a mixed-adapter route"
fi
subscription_run_source="$(sed -n \
  '/^run_subscription_internal()/,/^product_role_run()/p' "$LANE")"
grep -Fq 'PROVIDER_SPLIT=codex:4' <<<"$subscription_run_source" ||
  fail "subscription canary does not report four Codex calls"
grep -Fq 'codex_subscription_ready "$root"' <<<"$subscription_run_source" ||
  fail "subscription canary readiness is not Codex-only"
lane_env_source="$(sed -n '/^lane_env()/,/^lane_cursor_env()/p' "$LANE")"
grep -Fq 'FACTORY_CLI_LANE_ROOT="$root"' <<<"$lane_env_source" ||
  fail "trusted product helpers lost the checkpoint lane-root binding"
grep -Fq 'FACTORY_CLI_INTERNAL_SANDBOX=1' <<<"$lane_env_source" ||
  fail "trusted product helpers lost the development sandbox marker"
seatbelt_source="$(sed -n '/^write_seatbelt_profiles()/,/^}/p' "$LANE")"
grep -Fq 'for item in ("/opt/homebrew", "/usr/local"):' <<<"$seatbelt_source" ||
  fail "development sandbox dropped the trusted Node toolchain roots"
if grep -Eq 'file-write.*(/opt/homebrew|/usr/local)' <<<"$seatbelt_source"; then
  fail "development sandbox made the host toolchain writable"
fi
eval "$(sed -n '/^prepare_product_dependencies()/,/^}/p' "$LANE")"
sandbox_exec() { printf '%s\n' "$FAKE_SANDBOX"; }
DEPENDENCY_ROOT="$TMP/nysa-sf-dev.dependencies"
mkdir -p "$DEPENDENCY_ROOT"/{home,product,runtime,tmp,worktrees/T-1,worktrees/T-2,worktrees/T-FAIL,worktrees/T-DRIFT}
: >"$DEPENDENCY_ROOT/runtime/native.sb"
printf '{}\n' >"$DEPENDENCY_ROOT/product/package-lock.json"
cat >"$DEPENDENCY_ROOT/home/node" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
cat >"$DEPENDENCY_ROOT/home/npm" <<'EOF'
#!/usr/bin/env bash
set -eu
prefix=""
while [[ $# -gt 0 ]]; do
  if [[ "$1" == --prefix ]]; then prefix="$2"; shift 2; else shift; fi
done
[[ -n "$prefix" ]]
root="$(cd "$(dirname "$0")/.." && pwd -P)"
printf '%s\n' "$(basename "$prefix")" >>"$root/runtime/npm-calls"
case "$(basename "$prefix")" in
  T-FAIL) exit 9 ;;
  T-DRIFT) printf '\n' >>"$prefix/package.json" ;;
esac
mkdir -p "$prefix/node_modules/.bin"
EOF
chmod +x "$DEPENDENCY_ROOT/home/node" "$DEPENDENCY_ROOT/home/npm"
for dependency_ticket in T-1 T-2 T-FAIL T-DRIFT; do
  dependency_work="$DEPENDENCY_ROOT/worktrees/$dependency_ticket"
  printf '{"scripts":{}}\n' >"$dependency_work/package.json"
  printf '{}\n' >"$dependency_work/package-lock.json"
  printf 'node_modules/\n' >"$dependency_work/.gitignore"
  git -C "$dependency_work" init -q
  git -C "$dependency_work" add .
  git -C "$dependency_work" -c user.name=Test -c user.email=test@local \
    commit -qm 'Create dependency fixture'
done
( die() { printf '%s\n' "$*" >&2; exit 1; }
  prepare_product_dependencies "$DEPENDENCY_ROOT" T-1 T-2 ) ||
  fail "pinned dependency bootstrap rejected clean ticket worktrees"
[[ "$(cat "$DEPENDENCY_ROOT/runtime/npm-calls")" == $'T-1\nT-2' ]] ||
  fail "pinned dependency bootstrap did not run exactly once per ticket"
for dependency_ticket in T-1 T-2; do
  [[ -d "$DEPENDENCY_ROOT/worktrees/$dependency_ticket/node_modules" ]] ||
    fail "pinned dependency bootstrap omitted ticket-local node_modules"
done
if ( die() { exit 1; }
     prepare_product_dependencies "$DEPENDENCY_ROOT" T-FAIL ); then
  fail "pinned dependency bootstrap accepted an npm failure"
fi
if ( die() { exit 1; }
     prepare_product_dependencies "$DEPENDENCY_ROOT" T-DRIFT ); then
  fail "pinned dependency bootstrap accepted tracked-tree drift"
fi
lane_count_before="$(find "$TMP/lanes" -mindepth 1 -maxdepth 1 -type d | wc -l | tr -d ' ')"
expect_failure "production product source" test_env bash "$LANE" product-plan \
  --source "$CALLER_HOME/Projects/nysa-company/nysa-app" \
  --base-sha aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa \
  --tickets T-1,T-2,T-3,T-4
expect_failure "duplicate product tickets" test_env bash "$LANE" product-plan \
  --source "$TMP/safe-source" --base-sha aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa \
  --tickets T-1,T-1,T-2,T-3
expect_failure "two-ticket source validation" test_env bash "$LANE" product-plan \
  --source "$TMP/safe-source" --base-sha aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa \
  --tickets T-1,T-2
grep -Fq 'product source must be an absolute, non-symlink repository' "$OUT" ||
  fail "two-ticket source validation returned: $(sed -n '1p' "$OUT")"
[[ "$(find "$TMP/lanes" -mindepth 1 -maxdepth 1 -type d | wc -l | tr -d ' ')" == \
   "$lane_count_before" ]] ||
  fail "invalid product input created a lane"
MISSING_SESSION_HOME="$TMP/missing-session-home"
mkdir -p "$MISSING_SESSION_HOME"
expect_failure "incomplete lane cleanup" env \
  FACTORY_DEV_LANE_TEST_MODE=1 FACTORY_TRUSTED_TEST_HARNESS=1 \
  FACTORY_DEV_LANE_UNAME=Darwin FACTORY_DEV_LANE_SANDBOX_EXEC="$FAKE_SANDBOX" \
  FACTORY_DEV_LANE_CURSOR_BIN="$FAKE_CURSOR" \
  FACTORY_DEV_LANE_ACCOUNT_HOME="$MISSING_SESSION_HOME" \
  FACTORY_DEV_LANE_CURSOR_SESSION_HOME="$MISSING_SESSION_HOME" \
  HOME="$MISSING_SESSION_HOME" TMPDIR="$TMP/lanes" \
  bash "$LANE" cursor-plan
[[ "$(find "$TMP/lanes" -mindepth 1 -maxdepth 1 -type d | wc -l | tr -d ' ')" == \
   "$lane_count_before" ]] ||
  fail "failed lane construction retained an isolated root"
DAY_LANE="$TMP/nysa-sf-dev.day"
mkdir -p "$DAY_LANE/product"
printf '{}\n' >"$DAY_LANE/marker.json"
STALE_RUN_DAY="$(python3 -c 'import datetime; print(datetime.datetime.now(datetime.timezone.utc).date()-datetime.timedelta(days=1))')"
expect_failure "stale development budget day" env \
  FACTORY_ROOT="$DAY_LANE/product" FACTORY_CLI_LANE_ROOT="$DAY_LANE" \
  FACTORY_DEV_BUDGET_DAY="$STALE_RUN_DAY" \
  bash "$ROOT/scripts/run-agent.sh" --role builder --ticket T-1 \
  --prompt-file "$ROOT/roles/builder.md" --workdir "$DAY_LANE/product" -- task
grep -Fq 'development budget day changed; no task was submitted' "$OUT" ||
  fail "stale development budget day did not fail before reservation"
if sed -n '/^subscription_env()/,/^}/p' "$LANE" | grep -q 'remote.origin.pushurl'; then
  fail "subscription host environment disables its own trusted push destination"
fi

READINESS_ROOT="$TMP/nysa-sf-dev.readiness"
mkdir -p "$READINESS_ROOT/home" "$READINESS_ROOT/session-home" \
  "$READINESS_ROOT/tmp"
cat >"$READINESS_ROOT/home/timeout" <<'EOF'
#!/usr/bin/env bash
shift
exec "$@"
EOF
chmod +x "$READINESS_ROOT/home/timeout"
for readiness_tool in agent codex claude; do
  cat >"$READINESS_ROOT/home/$readiness_tool" <<EOF
#!/usr/bin/env bash
[[ -z "\${AMBIENT_AUTH_READY+x}" ]]
[[ "\$HOME" == "$READINESS_ROOT/session-home" ]]
[[ "\$PWD" == "$READINESS_ROOT" ]]
[[ "\${FACTORY_CURSOR_SESSION_HOME:-}" == "$READINESS_ROOT/session-home" ]]
if [[ "\${1:-}" == --version ]]; then
  printf '%s\n' "$readiness_tool 1.0-test"
  exit 0
fi
if [[ "$readiness_tool" == codex && -f "\$HOME/.transient-auth" ]]; then
  count="\$(cat "\$HOME/.transient-auth")"
  if [[ "\$count" -lt 2 ]]; then
    printf '%s\n' "\$((count + 1))" >"\$HOME/.transient-auth"
    exit 1
  fi
fi
[[ -f "\$HOME/.auth-ready" ]]
EOF
  chmod +x "$READINESS_ROOT/home/$readiness_tool"
done
eval "$(sed -n '/^subscription_base_env()/,/^subscription_approval_hash()/p' \
  "$LANE" | sed '$d')"
eval "$(sed -n '/^subscription_env()/,/^product_approval_hash()/p' \
  "$LANE" | sed '$d')"
[[ "$(subscription_base_env "$READINESS_ROOT" \
  /usr/bin/printenv AGENT_CLI_CREDENTIAL_STORE)" == file ]] ||
  fail "subscription environment did not isolate Cursor credentials in the lane"
if (
  die() { exit 1; }
  AMBIENT_AUTH_READY=1 \
    FACTORY_CURSOR_SESSION_HOME="$TMP/external-cursor-home" \
    subscription_ready "$READINESS_ROOT"
); then
  fail "ambient-only subscription authentication passed lane readiness"
fi
touch "$READINESS_ROOT/session-home/.auth-ready"
(
  die() { exit 1; }
  AMBIENT_AUTH_READY=1 \
    FACTORY_CURSOR_SESSION_HOME="$TMP/external-cursor-home" \
    subscription_ready "$READINESS_ROOT"
) || fail "lane-local subscription authentication failed readiness"
AMBIENT_AUTH_READY=1 \
  FACTORY_CURSOR_SESSION_HOME="$TMP/external-cursor-home" \
  subscription_env "$READINESS_ROOT" \
    "$READINESS_ROOT/home/codex" login status >/dev/null ||
  fail "role environment disagreed with lane-local readiness"
printf '%s\n' 0 >"$READINESS_ROOT/session-home/.transient-auth"
(
  die() { exit 1; }
  subscription_ready "$READINESS_ROOT"
) || fail "subscription readiness did not outwait transient authentication"
[[ "$(<"$READINESS_ROOT/session-home/.transient-auth")" == 2 ]] ||
  fail "subscription readiness did not exercise bounded authentication retries"
for unused_tool in agent claude; do
  printf '%s\n' '#!/usr/bin/env bash' 'exit 1' \
    >"$READINESS_ROOT/home/$unused_tool"
  chmod +x "$READINESS_ROOT/home/$unused_tool"
done
(
  die() { exit 1; }
  codex_subscription_ready "$READINESS_ROOT"
) || fail "Codex-only canary readiness depended on another provider session"
sed -n '/^product_resume_plan()/,/^}/p' "$LANE" |
  grep -q 'subscription_ready "\$root"' ||
  fail "product resume planning does not stabilize authentication before approval"
(
  eval "$(sed -n '/^load_product_tickets()/,/^product_resume_drained()/p' \
    "$LANE" | sed '$d')"
  eval "$(sed -n '/^validate_product_resume_basis()/,/^restore_product_resume_source()/p' \
    "$LANE" | sed '$d')"
  sha256_file() { shasum -a 256 "$1" | awk '{print $1}'; }
  sha256_text() { shasum -a 256 | awk '{print $1}'; }
  die() { printf 'resume fixture failed: %s\n' "$*" >&2; return 1; }
  require_lane_mode() { :; }
  validate_runtime_paths() { :; }
  product_resume_drained() { :; }
  ensure_cursor_file_credential_config() { :; }
  subscription_ready() { :; }
  product_role_for_stage() {
    case "$1" in
      'RUN reviewer'|'RUN builder') return 0 ;;
      *) return 1 ;;
    esac
  }
  product_approval_hash() { printf '%064d\n' 1; }

  RESUME_ROOT="$TMP/targeted-resume"
  SOURCE_ROOT="$TMP/targeted-resume-controller"
  mkdir -p "$RESUME_ROOT/kit/scripts" "$RESUME_ROOT/runtime/product-envelope" \
    "$RESUME_ROOT/product/factory" "$RESUME_ROOT/worktrees" "$SOURCE_ROOT"
  git -C "$SOURCE_ROOT" init -q
  git -C "$SOURCE_ROOT" config user.name 'Factory Test'
  git -C "$SOURCE_ROOT" config user.email factory-test@local
  printf 'controller\n' >"$SOURCE_ROOT/controller"
  git -C "$SOURCE_ROOT" add controller
  git -C "$SOURCE_ROOT" commit -qm 'controller'
  git init -q --bare "$RESUME_ROOT/origin.git"
  cat >"$RESUME_ROOT/kit/scripts/provider-coordinator.py" <<'PY'
#!/usr/bin/env python3
import json
print(json.dumps({"active_reserve_micro_usd":0,"attempts":[],"counts":{}}))
PY
  chmod +x "$RESUME_ROOT/kit/scripts/provider-coordinator.py"
  : >"$RESUME_ROOT/runtime/provider-state.sqlite3"
  python3 - "$RESUME_ROOT/runtime/product-source.json" <<'PY'
import json, sys
value={
  "schema":"factory-dev-product-source/v1",
  "tickets":["T-046","T-047","T-048"],
  "base_sha":"a"*40,
  "base_tree":"b"*40,
  "lane_control_sha":"c"*40,
}
open(sys.argv[1],"w",encoding="utf-8").write(
  json.dumps(value,sort_keys=True,separators=(",",":"))+"\n"
)
PY
  for ticket in T-046 T-047 T-048; do
    work="$RESUME_ROOT/worktrees/$ticket"
    mkdir -p "$work/factory/tickets" "$work/factory/route-plans"
    git -C "$work" init -q
    git -C "$work" config user.name 'Factory Test'
    git -C "$work" config user.email factory-test@local
    printf '# %s\nState: Building\n' "$ticket" >"$work/factory/tickets/$ticket.md"
    printf '{}\n' >"$work/factory/route-plans/$ticket.json"
    git -C "$work" add .
    git -C "$work" commit -qm "$ticket fixture"
    git -C "$work" push -q "$RESUME_ROOT/origin.git" \
      "HEAD:refs/heads/ticket/$ticket"
    printf 'PER_TICKET_BUDGET_USD=100.00\n' \
      >"$RESUME_ROOT/runtime/product-envelope/$ticket.env"
  done
  STAGE_TRACE="$RESUME_ROOT/stages"
  : >"$STAGE_TRACE"
  product_resume_stage() {
    printf '%s\n' "$2" >>"$STAGE_TRACE"
    case "$2" in
      T-048) printf 'RUN reviewer\n' ;;
      T-047) printf 'RUN builder\n' ;;
      *) return 1 ;;
    esac
  }

  first="$(product_resume_plan "$RESUME_ROOT" T-048)"
  [[ "$first" == *'TICKETS=T-048'* &&
     -s "$STAGE_TRACE" &&
     -z "$(grep -Fvx T-048 "$STAGE_TRACE")" ]] ||
    fail "targeted resume resolved an excluded blocked sibling or lost T-048 stage"
  python3 - "$RESUME_ROOT/runtime/product-source.json" <<'PY' ||
import json, sys
v=json.load(open(sys.argv[1],encoding="utf-8"))
assert v["tickets"] == ["T-048"]
assert v["resume_original_tickets"] == ["T-046","T-047","T-048"]
PY
    fail "targeted resume lost its original ticket universe"

  rm "$RESUME_ROOT/runtime/product-approval"
  : >"$STAGE_TRACE"
  second="$(product_resume_plan "$RESUME_ROOT" T-047)"
  [[ "$second" == *'TICKETS=T-047'* &&
     -s "$STAGE_TRACE" &&
     -z "$(grep -Fvx T-047 "$STAGE_TRACE")" ]] ||
    fail "subsequent targeted resume could not select another original sibling"

  printf 'excluded drift\n' >>"$RESUME_ROOT/worktrees/T-046/excluded"
  git -C "$RESUME_ROOT/worktrees/T-046" add excluded
  git -C "$RESUME_ROOT/worktrees/T-046" commit -qm 'drift excluded sibling'
  git -C "$RESUME_ROOT/worktrees/T-046" push -q "$RESUME_ROOT/origin.git" \
    "HEAD:refs/heads/ticket/T-046"
  if validate_product_resume_basis "$RESUME_ROOT"; then
    fail "targeted resume accepted excluded sibling head/tree drift"
  fi
)
sed -n '/^run_product_internal()/,/^}/p' "$LANE" |
  grep -Fq '[[ "$readiness_proven" == 1 ]] || subscription_ready "$root"' ||
  fail "product runtime cannot reuse the trusted resume readiness proof"
sed -n '/^product_probe_and_plan()/,/^}/p' "$LANE" |
  grep -Fq 'ensure_product_budget_day "$root"' ||
  fail "fresh product planning does not bind a resumable budget day"
eval "$(sed -n '/^ensure_product_budget_day()/,/^}/p' "$LANE")"
BUDGET_DAY_ROOT="$TMP/product-budget-day"
mkdir -p "$BUDGET_DAY_ROOT/runtime"
ensure_product_budget_day "$BUDGET_DAY_ROOT" ||
  fail "fresh product planning could not create its budget day"
[[ "$(cat "$BUDGET_DAY_ROOT/runtime/product-envelope/budget-day")" == \
   "$(date -u +%F)" ]] ||
  fail "fresh product planning wrote the wrong budget day"
[[ "$(stat -f '%Lp' "$BUDGET_DAY_ROOT/runtime/product-envelope/budget-day")" == 600 ]] ||
  fail "fresh product budget day is not owner-only"
printf '%s\n' 2000-01-01 \
  >"$BUDGET_DAY_ROOT/runtime/product-envelope/budget-day"
chmod 600 "$BUDGET_DAY_ROOT/runtime/product-envelope/budget-day"
if ensure_product_budget_day "$BUDGET_DAY_ROOT"; then
  fail "fresh product planning overwrote a stale budget day"
fi

EXPORT_ROOT="$TMP/product-export"
EXPORT_WORK="$EXPORT_ROOT/worktrees/T-1"
mkdir -p "$EXPORT_ROOT/product/factory/runs" "$EXPORT_WORK/app" \
  "$EXPORT_WORK/factory/tickets"
ln -s "$ROOT" "$EXPORT_ROOT/kit"
git -C "$EXPORT_WORK" init -q
git -C "$EXPORT_WORK" config user.name 'Factory Dev Lane'
git -C "$EXPORT_WORK" config user.email factory-dev@local
printf '%s\n' old >"$EXPORT_WORK/app/source.txt"
printf '%s\n' 'State: Ready' >"$EXPORT_WORK/factory/tickets/T-1.md"
printf '%s\n' sibling >"$EXPORT_WORK/factory/tickets/T-2.md"
printf '%s\n' 'TEST_PATHS="app/tests/"' >"$EXPORT_WORK/factory/PROJECT.env"
git -C "$EXPORT_WORK" add .
git -C "$EXPORT_WORK" commit -qm base
EXPORT_BASE="$(git -C "$EXPORT_WORK" rev-parse HEAD)"
mkdir -p "$EXPORT_WORK/docs" "$EXPORT_WORK/factory/route-plans"
mkdir -p "$EXPORT_WORK/app/tests"
printf '%s\n' acceptance >"$EXPORT_WORK/app/tests/acceptance.test"
printf '%s\n' 'State: Building' >"$EXPORT_WORK/factory/tickets/T-1.md"
git -C "$EXPORT_WORK" add .
git -C "$EXPORT_WORK" commit -qm 'T-1: author acceptance tests'
EXPORT_TEST_AUTHOR_1="$(git -C "$EXPORT_WORK" rev-parse HEAD)"
printf '%s\n' new >"$EXPORT_WORK/app/source.txt"
git -C "$EXPORT_WORK" add app/source.txt
git -C "$EXPORT_WORK" commit -qm 'T-1: build initial implementation'
printf '%s\n' acceptance repaired >"$EXPORT_WORK/app/tests/acceptance.test"
git -C "$EXPORT_WORK" add app/tests/acceptance.test
git -C "$EXPORT_WORK" commit -qm 'T-1: repair acceptance tests'
printf '\000\001\002' >"$EXPORT_WORK/app/binary.dat"
printf '%s\n' reviewed >"$EXPORT_WORK/docs/contract.md"
printf '%s\n' 'State: Review' 'reviewer round 1: APPROVE' \
  >"$EXPORT_WORK/factory/tickets/T-1.md"
printf '%s\n' changed >"$EXPORT_WORK/factory/tickets/T-2.md"
printf '%s\n' '{}' >"$EXPORT_WORK/factory/route-plans/T-1.json"
git -C "$EXPORT_WORK" add .
git -C "$EXPORT_WORK" commit -qm 'T-1: repair implementation'
EXPORT_REVIEWED="$(git -C "$EXPORT_WORK" rev-parse HEAD)"
printf '%s\n' evidence >"$EXPORT_WORK/factory/tickets/T-1-bundle.md"
git -C "$EXPORT_WORK" add .
git -C "$EXPORT_WORK" commit -qm narrator
EXPORT_HEAD="$(git -C "$EXPORT_WORK" rev-parse HEAD)"
cat >"$EXPORT_ROOT/product/factory/runs/export-review.meta" <<EOF
ticket=T-1
role=reviewer
phase=completed
accounting_schema=1
accounting_state=completed
go_issued=1
task_submitted=1
exit_status=0
role_exit=ok
run_id=export-review
cost_basis=estimated_tokens
role_head_before=$EXPORT_REVIEWED
EOF
cat >"$EXPORT_ROOT/product/factory/runtime-ledger.csv" <<'EOF'
date,time,ticket,role,adapter,prompt_version,turns,cost_usd,exit_status,run_id,provider_family,model_id,selection_reason,cost_basis,adapter_version
2026-01-01,00:00:00,T-1,reviewer,mock,3,1,0.00,0,export-review,test,test,pinned_route_plan,estimated_tokens,test
EOF
eval "$(sed -n '/^product_export_patch()/,/^select_product_export_tickets()/p' \
  "$LANE" | sed '$d')"
EXPORT_PATCH="$EXPORT_ROOT/T-1.patch"
[[ "$(product_export_patch "$EXPORT_ROOT" T-1 "$EXPORT_BASE" \
  "$EXPORT_HEAD" "$EXPORT_PATCH")" == "$EXPORT_REVIEWED" ]] ||
  fail "approved product patch did not bind the Reviewer head"
EXPORT_APPLY="$TMP/product-export-apply"
git clone -q "$EXPORT_WORK" "$EXPORT_APPLY"
git -C "$EXPORT_APPLY" checkout -q "$EXPORT_BASE"
git -C "$EXPORT_APPLY" apply --check "$EXPORT_PATCH" ||
  fail "approved product patch is not applicable"
patch_paths="$(git -C "$EXPORT_APPLY" apply --numstat "$EXPORT_PATCH" | cut -f3-)"
for expected_path in app/source.txt app/binary.dat docs/contract.md; do
  grep -Fxq "$expected_path" <<<"$patch_paths" ||
    fail "approved product patch omitted $expected_path"
done
if grep -Eq '^factory(/|$)' <<<"$patch_paths"; then
  fail "approved product patch exported Factory control state"
fi
EXPORT_MBOX="$EXPORT_ROOT/T-1.mbox"
product_export_mbox "$EXPORT_ROOT" T-1 "$EXPORT_BASE" "$EXPORT_REVIEWED" \
  "$EXPORT_MBOX" ||
  fail "approved product mailbox was not created"
product_export_mbox "$EXPORT_ROOT" T-1 "$EXPORT_BASE" "$EXPORT_REVIEWED" \
  "$EXPORT_ROOT/T-1-repeat.mbox" ||
  fail "approved product mailbox was not reproducible"
cmp -s "$EXPORT_MBOX" "$EXPORT_ROOT/T-1-repeat.mbox" ||
  fail "approved product mailbox changed for identical reviewed input"
[[ -z "$(find "$EXPORT_ROOT" -maxdepth 1 -type d \
  -name 'factory-export-mbox.*' -print -quit)" ]] ||
  fail "product mailbox left its lane-local temporary repository"
git -C "$EXPORT_APPLY" config user.name 'Factory Export Test'
git -C "$EXPORT_APPLY" config user.email factory-export@local
git -C "$EXPORT_APPLY" am "$EXPORT_MBOX" >/dev/null ||
  fail "approved product mailbox is not applicable"
[[ "$(git -C "$EXPORT_APPLY" rev-list --count "$EXPORT_BASE..HEAD")" == 2 ]] ||
  fail "product mailbox did not produce exactly two publication strata"
[[ "$(git -C "$EXPORT_APPLY" log -1 --format=%s HEAD~1)" == \
   'T-1: publish approved tests' ]] ||
  fail "product mailbox did not publish the final test stratum first"
[[ "$(git -C "$EXPORT_APPLY" log -1 --format=%s HEAD)" == \
   'T-1: publish approved implementation' ]] ||
  fail "product mailbox did not publish the implementation stratum last"
first_paths="$(git -C "$EXPORT_APPLY" diff-tree --no-commit-id --name-only -r HEAD~1)"
[[ -n "$first_paths" ]] &&
  [[ -z "$(grep -Ev '^app/tests/' <<<"$first_paths")" ]] ||
  fail "product mailbox test stratum is not pure"
second_paths="$(git -C "$EXPORT_APPLY" diff-tree --no-commit-id --name-only -r HEAD)"
[[ -n "$second_paths" ]] &&
  [[ -z "$(grep -E '^app/tests/' <<<"$second_paths")" ]] ||
  fail "product mailbox implementation stratum contains tests"
(cd "$EXPORT_APPLY" &&
  BASE_REF="$EXPORT_BASE" TEST_PATHS='app/tests/' EXEMPT_PATHS='factory/' \
    bash "$ROOT/ci/test-immutability-check.sh" >/dev/null) ||
  fail "product mailbox does not satisfy tests-first immutability"
if git -C "$EXPORT_APPLY" diff-tree --no-commit-id --name-only -r \
    "$EXPORT_BASE..HEAD" | grep -Eq '^factory(/|$)'; then
  fail "product mailbox exported Factory control state"
fi
git -C "$EXPORT_APPLY" diff --exit-code "$EXPORT_REVIEWED" -- . \
  ':(exclude)factory' >/dev/null ||
  fail "product mailbox tree differs from the reviewed application projection"
if product_export_mbox "$EXPORT_ROOT" T-1 "$EXPORT_BASE" \
    "$EXPORT_TEST_AUTHOR_1" "$EXPORT_ROOT/empty-stratum.mbox"; then
  fail "product mailbox accepted an empty implementation stratum"
fi
printf '%s\n' drift >>"$EXPORT_WORK/app/source.txt"
git -C "$EXPORT_WORK" add app/source.txt
git -C "$EXPORT_WORK" commit -qm post-review-drift
if product_export_patch "$EXPORT_ROOT" T-1 "$EXPORT_BASE" \
    "$(git -C "$EXPORT_WORK" rev-parse HEAD)" "$EXPORT_ROOT/drift.patch" \
    >/dev/null; then
  fail "post-review product drift was exportable"
fi
ln -s source.txt "$EXPORT_WORK/app/unsafe-link"
git -C "$EXPORT_WORK" add app/unsafe-link
git -C "$EXPORT_WORK" commit -qm 'unsafe application link'
if product_export_mbox "$EXPORT_ROOT" T-1 "$EXPORT_BASE" \
    "$(git -C "$EXPORT_WORK" rev-parse HEAD)" "$EXPORT_ROOT/symlink.mbox"; then
  fail "product mailbox accepted an application symlink"
fi
eval "$(sed -n '/^select_product_export_tickets()/,/^}/p' "$LANE")"
PRODUCT_TICKETS=(T-1 T-2)
select_product_export_tickets T-2
[[ "${PRODUCT_TICKETS[*]}" == T-2 ]] ||
  fail "product export did not select the requested completed sibling"
PRODUCT_TICKETS=(T-1 T-2)
if (die() { exit 1; }; select_product_export_tickets T-2,T-2) ||
   (die() { exit 1; }; select_product_export_tickets T-3); then
  fail "product export accepted an unsafe ticket selection"
fi

VERDICT="$TMP/reviewer.out"
printf '%s\n' '{"type":"result","subtype":"success","result":"Reviewed safely.\n\nAPPROVE"}' >"$VERDICT"
[[ "$(python3 "$ROOT/scripts/lib/reviewer-verdict.py" --adapter cursor-anthropic --input "$VERDICT")" == APPROVE ]] ||
  fail "strict reviewer parser rejected a Cursor approval"
printf '%s\n' 'Review complete.' 'REQUEST CHANGES' >"$VERDICT"
[[ "$(python3 "$ROOT/scripts/lib/reviewer-verdict.py" --adapter codex --input "$VERDICT")" == 'REQUEST CHANGES' ]] ||
  fail "strict reviewer parser rejected a plain request-changes verdict"
printf '%s\n' 'Review complete.' 'REQUEST CHANGES' 'FIX-OWNER: both' >"$VERDICT"
[[ "$(python3 "$ROOT/scripts/lib/reviewer-verdict.py" --adapter codex --input "$VERDICT" \
  --contract-version 1.7.0 --format fields)" == $'REQUEST CHANGES\tboth' ]] ||
  fail "contract 1.7 reviewer parser lost explicit repair ownership"
printf '%s\n' 'Review complete.' 'REQUEST CHANGES' >"$VERDICT"
expect_failure "missing contract 1.7 fix owner" \
  python3 "$ROOT/scripts/lib/reviewer-verdict.py" --adapter codex --input "$VERDICT" \
    --contract-version 1.7.0
printf '%s\n' 'Review complete.' 'APPROVE' 'FIX-OWNER: builder' >"$VERDICT"
expect_failure "approval with contract 1.7 fix owner" \
  python3 "$ROOT/scripts/lib/reviewer-verdict.py" --adapter codex --input "$VERDICT" \
    --contract-version 1.7.0
printf '%s\n' '**Request changes.**' 'Build is not green.' '**REQUEST CHANGES** due to the build failure.' >"$VERDICT"
[[ "$(python3 "$ROOT/scripts/lib/reviewer-verdict.py" --adapter codex --input "$VERDICT")" == 'REQUEST CHANGES' ]] ||
  fail "reviewer parser rejected repeated agreeing Markdown verdicts"
rm -f "$TMP/cursor-args"
FACTORY_ROLE=reviewer FACTORY_CURSOR_SESSION_HOME="$CALLER_HOME" \
  FACTORY_CURSOR_INTERNAL_SANDBOX=1 CURSOR_AGENT_BIN="$FAKE_CURSOR" \
  CURSOR_AGENT_VERSION=2026.07.17-test \
  "$ROOT/scripts/adapters/cursor-anthropic.sh" --budget 1 --max-turns 1 \
    --timeout-min 1 --prompt-file "$ROOT/roles/reviewer.md" --workdir "$TMP" \
    --model claude-sonnet-5-thinking-high --effort high -- review \
    >/dev/null 2>&1 || true
if ! python3 - "$TMP/cursor-args" <<'PY'
import sys
args = open(sys.argv[1], encoding="utf-8").read().splitlines()
assert args[args.index("--mode") + 1] == "ask"
assert "--force" in args
assert "Reviewer CLI control: remain read-only" in args[-1]
assert "run the required deterministic checks" in args[-1]
assert "read-only terminal access" in args[-1]
PY
then
  fail "concurrent Cursor Reviewer did not use executable read-only mode"
fi
grep -Fq 'FACTORY_DEV_PRLESS_EVIDENCE_V1' "$ROOT/roles/narrator.md" ||
  fail "Narrator backend-only exception lacks its trusted development marker"
grep -Fq 'Not applicable — backend-only contract' "$ROOT/roles/narrator.md" ||
  fail "Narrator cannot represent an explicitly backend-only preview"
grep -Fq 'never weakens the normal' \
  "$ROOT/roles/narrator.md" ||
  fail "Narrator backend-only exception weakens production preview evidence"
grep -Fq 'backend-only HTTP API' "$ROOT/roles/narrator.md" ||
  fail "Narrator still excludes backend-only HTTP APIs from development evidence"
product_role_source="$(sed -n '/^product_role_run()/,/^product_transition_contract_blocked()/p' "$LANE")"
printf '%s\n' "$product_role_source" |
  grep -Fq 'Trusted host marker: FACTORY_DEV_PRLESS_EVIDENCE_V1' ||
  fail "development product runner did not supply the PR-less evidence marker"
printf '%s\n' "$product_role_source" |
  grep -Fq 'including a backend-only HTTP API' ||
  fail "development product runner still excludes backend-only HTTP APIs"
printf '%s\n' "$product_role_source" |
  grep -Fq 'later publication gate and must not block this development bundle' ||
  fail "development Narrator can still block on the later publication preview"
eval "$(sed -n '/^validate_product_dev_bundle()/,/^}/p' "$LANE")"
cat >"$TMP/http-backend-bundle.md" <<'EOF'
# Development-only evidence — not a production attestation
## What this does
Adds a backend HTTP API.
## Preview
Not applicable — backend-only contract
## Screenshots
Not applicable — backend-only contract
## Acceptance criteria
| Criterion | Evidence | Result |
| --- | --- | --- |
| HTTP behavior | focused API test | Pass |
## Risk
Internal change.
## Cost
$1.00, one attempt.
## Rollback
Revert the later PR.
Approve to merge, or send back with what's wrong?
EOF
validate_product_dev_bundle "$TMP/http-backend-bundle.md" ||
  fail "development validator rejected a backend-only HTTP API bundle"
sed 's/^Not applicable — backend-only contract$/Not applicable — backend-only contract. No browser or visual surface exists./' \
  "$TMP/http-backend-bundle.md" >"$TMP/annotated-backend-bundle.md"
validate_product_dev_bundle "$TMP/annotated-backend-bundle.md" ||
  fail "development validator rejected an annotated backend-only marker"
sed '/^## Preview$/,/^## Screenshots$/{
  /Not applicable — backend-only contract/c\
Unavailable in this sandbox; pending until the PR/deploy publication gate.
}' "$TMP/http-backend-bundle.md" >"$TMP/pending-http-backend-bundle.md"
validate_product_dev_bundle "$TMP/pending-http-backend-bundle.md" ||
  fail "development validator rejected a pending HTTP publication preview"
sed '/^## Screenshots$/,/^## Acceptance criteria$/{
  /Not applicable — backend-only contract/c\
**Unavailable — no preview deploy exists.** This backend HTTP contract has no UI or visual surface.
}' "$TMP/pending-http-backend-bundle.md" >"$TMP/retained-http-backend-bundle.md"
validate_product_dev_bundle "$TMP/retained-http-backend-bundle.md" ||
  fail "development validator rejected the retained HTTP bundle shape"
sed 's/no UI or visual surface/a changed UI/' \
  "$TMP/retained-http-backend-bundle.md" >"$TMP/visual-bundle-without-evidence.md"
expect_failure "visual bundle without preview evidence" \
  validate_product_dev_bundle "$TMP/visual-bundle-without-evidence.md"
subscription_cases="$(sed -n '/^  subscription-plan)/,/^  product-seed-lineage)/p' "$LANE")"
printf '%s\n' "$subscription_cases" |
  grep -Fq 'run_in_sandbox "$root" subscription __subscription-plan' ||
  fail "Codex subscription planning still depends on the Cursor scratch bridge"
printf '%s\n' "$subscription_cases" |
  grep -Fq 'run_in_sandbox "$root" subscription __subscription-run' ||
  fail "Codex subscription execution still depends on the Cursor scratch bridge"
eval "$(sed -n '/^cleanup_empty_cursor_bridge()/,/^}/p' "$LANE")"
REPLACED_BRIDGE="$TMP/replaced-cursor-bridge"
mkdir -p "$REPLACED_BRIDGE/empty-session"
chmod 755 "$REPLACED_BRIDGE" "$REPLACED_BRIDGE/empty-session"
cleanup_empty_cursor_bridge "$REPLACED_BRIDGE" ||
  fail "empty Cursor-replaced bridge was not cleanable"
[[ ! -e "$REPLACED_BRIDGE" ]] ||
  fail "empty Cursor-replaced bridge survived cleanup"
mkdir -p "$REPLACED_BRIDGE"
printf '%s\n' unsafe >"$REPLACED_BRIDGE/provider-state"
expect_failure "nonempty Cursor-replaced bridge" \
  cleanup_empty_cursor_bridge "$REPLACED_BRIDGE"
[[ -f "$REPLACED_BRIDGE/provider-state" ]] ||
  fail "Cursor bridge cleanup removed unrecognized provider state"
(
  BRIDGE_CLAIM_ROOT="$TMP/cursor-bridge-claim"
  BRIDGE_CLAIM_PATH="$TMP/cursor-bridge-claim-path"
  mkdir -p "$BRIDGE_CLAIM_ROOT/kit/scripts" "$BRIDGE_CLAIM_ROOT/runtime" \
    "$BRIDGE_CLAIM_ROOT/home" "$BRIDGE_CLAIM_ROOT/tmp" \
    "$BRIDGE_CLAIM_ROOT/session-home" "$BRIDGE_CLAIM_PATH/empty-session"
  cat >"$BRIDGE_CLAIM_ROOT/kit/scripts/factory-dev-lane.sh" <<'EOF'
#!/usr/bin/env bash
[[ "$1" == verify && -L "$2" ]]
[[ "$(python3 - "$2" <<'PY'
import os, sys
print(os.path.realpath(sys.argv[1]))
PY
)" == "$3" ]]
EOF
  chmod +x "$BRIDGE_CLAIM_ROOT/kit/scripts/factory-dev-lane.sh"
  eval "$(sed -n '/^run_in_sandbox()/,/^}/p' "$LANE")"
  cursor_tmp_bridge() { printf '%s\n' "$BRIDGE_CLAIM_PATH"; }
  subscription_provider_idle() { :; }
  die() { echo "$*" >&2; exit 1; }
  run_in_sandbox "$BRIDGE_CLAIM_ROOT" cursor verify "$BRIDGE_CLAIM_PATH" \
    "$BRIDGE_CLAIM_ROOT/runtime/cursor-tmp" ||
    fail "safe empty Cursor bridge was not reclaimed and atomically claimed"
  [[ ! -e "$BRIDGE_CLAIM_PATH" && ! -L "$BRIDGE_CLAIM_PATH" ]] ||
    fail "reclaimed Cursor bridge survived normal cleanup"

  mkdir -p "$BRIDGE_CLAIM_PATH/empty-session"
  subscription_provider_idle() { return 1; }
  expect_failure "active-provider Cursor bridge reclaim" \
    run_in_sandbox "$BRIDGE_CLAIM_ROOT" cursor verify "$BRIDGE_CLAIM_PATH" \
      "$BRIDGE_CLAIM_ROOT/runtime/cursor-tmp"
  [[ -d "$BRIDGE_CLAIM_PATH/empty-session" ]] ||
    fail "active-provider refusal changed the Cursor bridge"
  rm -rf "$BRIDGE_CLAIM_PATH"

  subscription_provider_idle() { :; }
  mkdir -p "$BRIDGE_CLAIM_PATH"
  printf '%s\n' unsafe >"$BRIDGE_CLAIM_PATH/provider-state"
  expect_failure "nonempty Cursor bridge reclaim" \
    run_in_sandbox "$BRIDGE_CLAIM_ROOT" cursor verify "$BRIDGE_CLAIM_PATH" \
      "$BRIDGE_CLAIM_ROOT/runtime/cursor-tmp"
  [[ -f "$BRIDGE_CLAIM_PATH/provider-state" ]] ||
    fail "nonempty Cursor bridge refusal removed provider state"
  rm -rf "$BRIDGE_CLAIM_PATH"

  mkdir "$BRIDGE_CLAIM_PATH"
  chmod 777 "$BRIDGE_CLAIM_PATH"
  expect_failure "unsafe-mode Cursor bridge reclaim" \
    run_in_sandbox "$BRIDGE_CLAIM_ROOT" cursor verify "$BRIDGE_CLAIM_PATH" \
      "$BRIDGE_CLAIM_ROOT/runtime/cursor-tmp"
  [[ -d "$BRIDGE_CLAIM_PATH" ]] ||
    fail "unsafe-mode Cursor bridge refusal removed the directory"
  chmod 700 "$BRIDGE_CLAIM_PATH"
  rmdir "$BRIDGE_CLAIM_PATH"

  printf '%s\n' unsafe >"$BRIDGE_CLAIM_PATH"
  expect_failure "file Cursor bridge reclaim" \
    run_in_sandbox "$BRIDGE_CLAIM_ROOT" cursor verify "$BRIDGE_CLAIM_PATH" \
      "$BRIDGE_CLAIM_ROOT/runtime/cursor-tmp"
  [[ -f "$BRIDGE_CLAIM_PATH" ]] ||
    fail "file Cursor bridge refusal removed the file"
  rm "$BRIDGE_CLAIM_PATH"

  ln -s "$BRIDGE_CLAIM_ROOT" "$BRIDGE_CLAIM_PATH"
  expect_failure "symlink Cursor bridge reclaim" \
    run_in_sandbox "$BRIDGE_CLAIM_ROOT" cursor verify "$BRIDGE_CLAIM_PATH" \
      "$BRIDGE_CLAIM_ROOT/runtime/cursor-tmp"
  [[ -L "$BRIDGE_CLAIM_PATH" ]] ||
    fail "symlink Cursor bridge refusal removed the link"
)
for invalid in 'APPROVE|REQUEST CHANGES' '**APPROVE**|**REQUEST CHANGES**' 'no verdict'; do
  printf '%s\n' "$invalid" | tr '|' '\n' >"$VERDICT"
  expect_failure "ambiguous reviewer verdict" python3 "$ROOT/scripts/lib/reviewer-verdict.py" \
    --adapter codex --input "$VERDICT"
done
product_role_source="$(sed -n '/^product_role_run()/,/^product_reconcile_reviewer()/p' "$LANE")"
if grep -Fq '"$role" == builder' <<<"$product_role_source" ||
   grep -Fq 'set_review_state "$root"' <<<"$product_role_source"; then
  fail "product Builder still owns a bespoke Review-state transition"
fi
product_scheduler_source="$(sed -n '/^run_product_internal()/,/^product_export_patch()/p' "$LANE")"
grep -Fq 'product_prepare_role_state "$root" "$ticket" "$role"' \
  <<<"$product_scheduler_source" ||
  fail "development scheduler does not prepare shared role state before launch"
python3 - "$product_scheduler_source" <<'PY' ||
import sys
text=sys.argv[1]
prepare=text.index('product_prepare_role_state "$root" "$ticket" "$role"')
launch=text.index('product_role_run "$root" "$ticket"',prepare)
assert prepare < launch
assert "failed_stages[$i]=state-transition" in text[prepare:launch]
PY
  fail "development role-state refusal is not pre-provider"
printf '%s\n' "$product_role_source" |
  grep -Fq 'FACTORY_DEV_PROVIDER_WAIT_SECONDS=900' ||
  fail "development product wait is not the bounded fifteen-minute policy"
printf '%s\n' "$product_role_source" |
  grep -Fq '\$(git rev-parse --show-toplevel)/../../runtime/product-db/$ticket.env' ||
  fail "product role instruction does not use a portable database path"
if printf '%s\n' "$product_role_source" |
   grep -Fq "source '\$root/runtime/product-db"; then
  fail "product role instruction still exposes a physical lane path"
fi
seed_source="$(sed -n \
  '/^seed_product_worktrees()/,/^write_product_checkpoint_import()/p' "$LANE")"
printf '%s\n' "$seed_source" |
  grep -Fq 'scripts/lib/lane-path-sentinel.py' ||
  fail "checkpoint import lost its lane-path sentinel"
checkpoint_export_source="$(sed -n \
  '/^export_product_checkpoint_internal()/,/^product_export_roles_complete()/p' \
  "$LANE")"
printf '%s\n' "$checkpoint_export_source" |
  grep -Fq '"$SOURCE_ROOT/scripts/lib/lane-path-sentinel.py"' ||
  fail "checkpoint export does not use the trusted controller sentinel"
if printf '%s\n' "$checkpoint_export_source" |
   grep -Fq '"$root/kit/scripts/lib/lane-path-sentinel.py"'; then
  fail "checkpoint export still requires the retained kit sentinel"
fi
grep -Fq '["lane_control_sha"]' <<<"$checkpoint_export_source" ||
  fail "checkpoint export does not exclude trusted lane-control output"
(
  RETAINED="$TMP/retained-pre-sentinel"
  mkdir -p "$RETAINED/runtime" "$RETAINED/worktrees" "$RETAINED/kit/scripts/lib"
  git init -q "$RETAINED/source"
  printf '%s\n' portable >"$RETAINED/source/output.txt"
  git -C "$RETAINED/source" add output.txt
  git -C "$RETAINED/source" -c user.name=Test -c user.email=test@local \
    commit -qm 'Base'
  RETAINED_BASE="$(git -C "$RETAINED/source" rev-parse HEAD)"
  mkdir -p "$RETAINED/source/factory"
  printf '%s\n' \
    'WORKTREES_DIR="/private/tmp/nysa-sf-dev.trusted-control/worktrees"' \
    >"$RETAINED/source/factory/PROJECT.env"
  git -C "$RETAINED/source" add factory/PROJECT.env
  git -C "$RETAINED/source" -c user.name=Test -c user.email=test@local \
    commit -qm 'Trusted lane control'
  RETAINED_CONTROL="$(git -C "$RETAINED/source" rev-parse HEAD)"
  git -C "$RETAINED/source" checkout -qb ticket/T-997
  printf '%s\n' retained >>"$RETAINED/source/output.txt"
  git -C "$RETAINED/source" add output.txt
  git -C "$RETAINED/source" -c user.name=Test -c user.email=test@local \
    commit -qm 'Retained role output'
  git clone -q --bare "$RETAINED/source" "$RETAINED/origin.git"
  git clone -q "$RETAINED/origin.git" "$RETAINED/worktrees/T-997"
  git -C "$RETAINED/worktrees/T-997" checkout -q ticket/T-997
  printf '{"base_sha":"%s","lane_control_sha":"%s"}\n' \
    "$RETAINED_BASE" "$RETAINED_CONTROL" \
    >"$RETAINED/runtime/product-source.json"
  chmod 700 "$RETAINED"
  RETAINED_OUTPUT_PARENT="$TMP/retained-checkpoint-output"
  mkdir -m 700 "$RETAINED_OUTPUT_PARENT"

  require_lane_mode() { :; }
  load_product_tickets() { PRODUCT_TICKETS=(T-997); }
  select_product_export_tickets() { PRODUCT_TICKETS=(T-997); }
  validate_runtime_paths() { :; }
  product_resume_drained() { :; }
  refuse_production_path() { :; }
  subscription_env() { :; }
  write_product_checkpoint() {
    printf '%s\n' retained >"$3"
  }
  eval "$(sed -n \
    '/^export_product_checkpoint_internal()/,/^write_product_checkpoint()/p' \
    "$LANE" | sed '$d')"
  export_product_checkpoint_internal "$RETAINED" T-997 \
    "$RETAINED_OUTPUT_PARENT/checkpoint-1" >"$OUT"
  [[ -s "$RETAINED_OUTPUT_PARENT/checkpoint-1/seed.bundle" &&
     -s "$RETAINED_OUTPUT_PARENT/checkpoint-1/checkpoint.json" ]] ||
    fail "checkpoint export from a pre-sentinel retained kit failed"
)
REC="$TMP/reviewer-reconcile"
mkdir -p "$REC/product/factory/runs" "$REC/worktrees/T-900001/factory/tickets"
ln -s "$ROOT" "$REC/kit"
review_ticket="$REC/worktrees/T-900001/factory/tickets/T-900001.md"
printf '%s\n' 'State: Review' >"$review_ticket"
review_head=1111111111111111111111111111111111111111
printf '%s\n' '{"type":"result","subtype":"success","result":"Reviewed safely.\n\nAPPROVE"}' \
  >"$REC/product/factory/runs/review.out"
review_digest="$(shasum -a 256 "$REC/product/factory/runs/review.out" | awk '{print $1}')"
printf '%s\n' \
  'ticket=T-900001' 'role=reviewer' 'adapter=cursor-anthropic' \
  'contract_version=1.7.0' 'role_exit=ok' \
  "role_head_before=$review_head" "role_remote_before=$review_head" \
  "output_sha256=$review_digest" \
  'accounting_state=abandoned_conservative' 'exit_status=0' 'started_at=2026-01-01T00:00:00Z' \
  >"$REC/product/factory/runs/review.meta"
python3 "$ROOT/scripts/lib/reviewer-reconcile.py" \
  --runs-dir "$REC/product/factory/runs" --ticket-file "$review_ticket" \
  --ticket T-900001 --head "$review_head" --contract-version 1.7.0 \
  --output "$REC/reconciled"
mv "$REC/reconciled" "$review_ticket"
python3 "$ROOT/scripts/lib/reviewer-reconcile.py" \
  --runs-dir "$REC/product/factory/runs" --ticket-file "$review_ticket" \
  --ticket T-900001 --head "$review_head" --contract-version 1.7.0 \
  --output "$REC/reconciled" ||
  fail "successful unpaired review was not reconciled"
cmp -s "$review_ticket" "$REC/reconciled" ||
  fail "replayed review reconciliation was not idempotent"
[[ "$(grep -c '^reviewer round 1: APPROVE$' "$review_ticket")" -eq 1 ]] ||
  fail "review reconciliation did not append exactly once"
review_ticket="$REC/worktrees/T-900002/factory/tickets/T-900002.md"
mkdir -p "$(dirname "$review_ticket")"
printf '%s\n' 'State: Review' >"$review_ticket"
printf '%s\n' 'Review complete.' 'REQUEST CHANGES' 'FIX-OWNER: test-author' \
  >"$REC/product/factory/runs/review-request.out"
review_digest="$(shasum -a 256 "$REC/product/factory/runs/review-request.out" | awk '{print $1}')"
printf '%s\n' \
  'ticket=T-900002' 'role=reviewer' 'adapter=codex' \
  'contract_version=1.7.0' 'role_exit=ok' \
  "role_head_before=$review_head" "role_remote_before=$review_head" \
  "output_sha256=$review_digest" \
  'accounting_state=completed' 'exit_status=0' 'started_at=2026-01-02T00:00:00Z' \
  >"$REC/product/factory/runs/review-request.meta"
python3 "$ROOT/scripts/lib/reviewer-reconcile.py" \
  --runs-dir "$REC/product/factory/runs" --ticket-file "$review_ticket" \
  --ticket T-900002 --head "$review_head" --contract-version 1.7.0 \
  --output "$REC/reconciled" ||
  fail "request-changes review was treated as a terminal lifecycle failure"
mv "$REC/reconciled" "$review_ticket"
grep -qx 'State: Building' "$review_ticket" ||
  fail "request-changes review did not atomically return the ticket to Building"
grep -qx 'reviewer round 1: REQUEST CHANGES' "$review_ticket" ||
  fail "request-changes review was not recorded durably"
grep -qx 'reviewer round 1 FIX-OWNER: test-author' "$review_ticket" ||
  fail "request-changes repair ownership was not recorded durably"
product_reconcile_source="$(sed -n '/^product_reconcile_reviewer()/,/^}/p' "$LANE")"
printf '%s\n' "$product_reconcile_source" |
  grep -Fq '"$SOURCE_ROOT/scripts/ticket-state.sh"' ||
  fail "development scheduler does not use the corrected trusted controller"
if printf '%s\n' "$product_reconcile_source" |
   grep -Fq '"$root/kit/scripts/ticket-state.sh"'; then
  fail "development scheduler still uses the retained kit for reconciliation"
fi
printf '%s\n' "$product_reconcile_source" | grep -Fq -- '--action reviewer-reconcile' ||
  fail "development scheduler does not request shared reviewer reconciliation"
eval "$product_reconcile_source"
RECONCILE_GUARD="$TMP/reviewer-reconcile-guard"
mkdir -p "$RECONCILE_GUARD/product/factory/runs"
lane_env() { printf '%s\n' called >>"$RECONCILE_GUARD/calls"; }
product_reconcile_reviewer "$RECONCILE_GUARD" T-1 ||
  fail "scheduler rejected a ticket before its first Reviewer output"
[[ ! -e "$RECONCILE_GUARD/calls" ]] ||
  fail "scheduler reconciled before a successful Reviewer output existed"
printf '%s\n' ticket=T-1 role=reviewer phase=completed exit_status=0 \
  >"$RECONCILE_GUARD/product/factory/runs/reviewer.meta"
product_reconcile_reviewer "$RECONCILE_GUARD" T-1 ||
  fail "scheduler did not reconcile a successful Reviewer output"
[[ "$(cat "$RECONCILE_GUARD/calls")" == called ]] ||
  fail "scheduler did not use the shared reconciliation helper exactly once"
eval "$(sed -n \
  '/^product_transition_contract_blocked()/,/^product_reconcile_reviewer()/p' \
  "$LANE" | sed '$d')"
BLOCKED_ROOT="$TMP/contract-blocked"
mkdir -p "$BLOCKED_ROOT/product/factory/runs" "$BLOCKED_ROOT/worktrees/T-1"
printf '%s\n' \
  'run_id=blocked-run' 'ticket=T-1' 'role=builder' \
  'contract_version=1.7.0' 'phase=completed' 'accounting_state=completed' \
  'exit_status=12' 'role_exit=role_exit_contract_blocked' \
  'started_at=2026-07-23T00:00:00Z' \
  >"$BLOCKED_ROOT/product/factory/runs/blocked.meta"
lane_env() { printf '%s\n' "$*" >"$BLOCKED_ROOT/transition"; }
product_transition_contract_blocked "$BLOCKED_ROOT" T-1 builder ||
  fail "authenticated contract blocker was not transitioned"
grep -Fq -- '--action transition --state Blocked-Escalated' \
  "$BLOCKED_ROOT/transition" ||
  fail "contract blocker did not use the trusted blocked transition"
sed -i '' 's/^role_exit=.*/role_exit=ok/' \
  "$BLOCKED_ROOT/product/factory/runs/blocked.meta"
expect_failure "forged contract blocker" \
  product_transition_contract_blocked "$BLOCKED_ROOT" T-1 builder
grep -Fq 'GIT_CONFIG_KEY_0=remote.origin.pushurl' "$ROOT/scripts/run-agent.sh" ||
  fail "provider task environment no longer owns the push guard"
grep -Fq '"AGENT_CLI_CREDENTIAL_STORE=${AGENT_CLI_CREDENTIAL_STORE:-}"' \
  "$ROOT/scripts/run-agent.sh" ||
  fail "provider task environment dropped the lane-local Cursor credential store"
grep -Fq -- '--base-envelope "$ENV_FILE"' "$ROOT/scripts/run-agent.sh" ||
  fail "effective budget resolution dropped the ticket-specific envelope"

eval "$(sed -n '/^product_resume_reason()/,/^}/p' "$LANE")"
printf '%s\n' 'Resolved Cursor model is unavailable' >"$TMP/retryable-role.log"
[[ "$(product_resume_reason "$TMP/retryable-role.log")" == pinned-route-readiness ]] ||
  fail "resume handoff lost the model-readiness diagnosis"
printf '%s\n' 'subscription authentication is unavailable' >"$TMP/retryable-role.log"
[[ "$(product_resume_reason "$TMP/retryable-role.log")" == role-failed ]] ||
  fail "resume handoff misclassified a provider failure"
printf '%s\n' \
  "pinned route unavailable or drifted for role 'reviewer': pinned_route_UNAVAILABLE_authentication_unavailable; no task was submitted" \
  >"$TMP/retryable-role.log"
[[ "$(product_resume_reason "$TMP/retryable-role.log")" == pinned-route-readiness ]] ||
  fail "resume handoff lost the pinned-route authentication diagnosis"
printf '%s\n' \
  "pinned route unavailable or drifted for role 'reviewer': pinned_route_INVALID_version_mismatch; no task was submitted" \
  >"$TMP/retryable-role.log"
[[ "$(product_resume_reason "$TMP/retryable-role.log")" == role-failed ]] ||
  fail "resume handoff misclassified identity or contract drift"
run_product_source="$(sed -n '/^run_product_internal()/,/^product_export_patch()/p' "$LANE")"
if grep -Eq 'retries\[|retry_after|product_role_retryable' <<<"$run_product_source"; then
  fail "product scheduler retained automatic provider retries"
fi
for expected in 'STATUS=RESUME-REQUIRED' 'RESUME_RECOMMENDED=1' \
  'RESUME_TICKETS=' 'RESUME_NEXT=product-resume-plan' 'FAILED_STAGE=' \
  'COMPLETED_ROLES=' 'REMAINING_BUDGET_USD=' 'RETAINED_ROOT=' \
  'RESUME_COMMAND=' 'STATUS=BLOCKED-ESCALATED' 'BLOCKED_TICKETS=' \
  'BLOCKED_STAGE='; do
  grep -Fq "$expected" <<<"$run_product_source" ||
    fail "product failure omitted explicit same-lane resume handoff: $expected"
done
product_plan_case="$(sed -n '/^  product-plan)/,/^  product-resume-plan)/p' "$LANE")"
python3 - "$product_plan_case" <<'PY' ||
import sys
text=sys.argv[1]
if text.index("run_in_sandbox") >= text.index("consume_product_seed_authorization"):
    raise SystemExit(1)
if text.index("consume_product_seed_authorization") >= text.rindex('echo "ROOT=$root"'):
    raise SystemExit(1)
PY
  fail "seed authorization is exposed before successful lane planning"
eval "$(sed -n '/^product_completed_roles()/,/^run_product_internal()/p' \
  "$LANE" | sed '$d')"
TIMING_ROOT="$TMP/product-timing"
mkdir -p "$TIMING_ROOT/kit/scripts" "$TIMING_ROOT/runtime" \
  "$TIMING_ROOT/product/factory/runs"
cat >"$TIMING_ROOT/kit/scripts/provider-coordinator.py" <<'PY'
#!/usr/bin/env python3
import json
base={
  "ticket_id":"T-1","prepared_at":9,"admitted_at":10,"submitted_at":11,
  "state":"terminal","terminal_result":"succeeded","reserve_micro_usd":10000000,
  "charge_micro_usd":10000000,
}
print(json.dumps({"attempts":[
  dict(base,attempt_id="one",go_at=10,terminal_at=20),
  dict(base,attempt_id="two",go_at=12,terminal_at=18),
]}))
PY
chmod +x "$TIMING_ROOT/kit/scripts/provider-coordinator.py"
: >"$TIMING_ROOT/runtime/provider-state.sqlite3"
printf '%s\n' 'PER_TICKET_BUDGET_USD=100.00' \
  >"$TIMING_ROOT/product/factory/ENVELOPE.env"
for timing_run in one two; do
  printf '%s\n' phase=completed ticket=T-1 role=planner exit_status=0 \
    >"$TIMING_ROOT/product/factory/runs/$timing_run.meta"
done
[[ "$(product_completed_roles "$TIMING_ROOT" T-1)" == planner ]] ||
  fail "resume diagnostics lost completed-role evidence"
[[ "$(product_remaining_budget "$TIMING_ROOT" T-1)" == 80.000000 ]] ||
  fail "resume diagnostics calculated the wrong remaining budget"
product_write_timing_report "$TIMING_ROOT" 5 35
python3 - "$TIMING_ROOT/runtime/product-timing.json" <<'PY' ||
import json, pathlib, stat, sys
path=pathlib.Path(sys.argv[1]); value=json.loads(path.read_text())
assert stat.S_IMODE(path.stat().st_mode)==0o600
assert value["schema"]=="factory-dev-product-timing/v1"
assert value["elapsed_seconds"]==30
assert value["maximum_provider_overlap"]==2
assert value["successful_role_replay_count"]==1
assert len(value["attempts"])==2
PY
  fail "product timing evidence was incomplete"
eval "$(sed -n '/^product_role_for_stage()/,/^}/p' "$LANE")"
if product_role_for_stage 'FIX builder-or-test-author' >/dev/null; then
  fail "development lane guessed Builder for ambiguous repair ownership"
fi
[[ "$(product_role_for_stage 'FIX test-author')" == test-author ]] ||
  fail "explicit review ownership did not select Test-author"
[[ "$(product_role_for_stage 'RUN reviewer')" == reviewer ]] ||
  fail "ordinary sequencer role mapping changed"
if product_role_for_stage AWAIT-OPERATOR >/dev/null; then
  fail "operator boundary was mapped to a provider role"
fi
eval "$(sed -n '/^product_prepare_role_state()/,/^}/p' "$LANE")"
ROLE_STATE_ROOT="$TMP/role-state-parity"
ROLE_STATE_TICKET="$ROLE_STATE_ROOT/worktrees/T-1/factory/tickets/T-1.md"
mkdir -p "$(dirname "$ROLE_STATE_TICKET")"
printf '%s\n' 'State: Ready' >"$ROLE_STATE_TICKET"
lane_env() {
  local ignored_root="$1" command="$2" target="" workdir="" ticket="" state_file
  shift 2
  [[ "$command" == "$ROOT/scripts/ticket-state.sh" ]] || return 1
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --ticket) ticket="$2"; shift 2 ;;
      --workdir) workdir="$2"; shift 2 ;;
      --action) [[ "$2" == transition ]] || return 1; shift 2 ;;
      --state) target="$2"; shift 2 ;;
      *) return 1 ;;
    esac
  done
  [[ "$workdir" == "$ignored_root/worktrees/$ticket" ]] ||
    return 1
  state_file="$workdir/factory/tickets/$ticket.md"
  python3 - "$state_file" "$target" <<'PY'
import re, sys
path,target=sys.argv[1:]
text=open(path,encoding="utf-8").read()
current=re.search(r"^State:\s*(.+)$",text,re.M).group(1)
allowed={("Ready","Planning"),("Planning","Building"),("Building","Review")}
if (current,target) not in allowed: raise SystemExit(1)
open(path,"w",encoding="utf-8").write(
    re.sub(r"^State:\s*.*$",f"State: {target}",text,count=1,flags=re.M))
PY
  printf '%s\n' "$target" >>"$ignored_root/transitions"
}
role_states=""
for role in planner spec-linter test-author builder reviewer narrator; do
  product_prepare_role_state "$ROLE_STATE_ROOT" T-1 "$role" ||
    fail "development role state preparation rejected $role"
  state="$(sed -n 's/^State: //p' "$ROLE_STATE_TICKET")"
  role_states="${role_states:+$role_states }$state"
done
[[ "$role_states" == \
   'Planning Planning Building Building Review Review' ]] ||
  fail "development and shared role-state sequences diverged: $role_states"
[[ "$(cat "$ROLE_STATE_ROOT/transitions")" == $'Planning\nBuilding\nReview' ]] ||
  fail "no-op development stages created redundant state transitions"
printf '%s\n' 'State: Ready' >"$ROLE_STATE_TICKET"
: >"$ROLE_STATE_ROOT/transitions"
product_prepare_role_state "$ROLE_STATE_ROOT" T-1 spec-linter ||
  fail "authenticated Ready checkpoint could not normalize before Spec-linter"
grep -qx 'State: Planning' "$ROLE_STATE_TICKET" ||
  fail "Ready checkpoint did not normalize to Planning before Spec-linter"
printf '%s\n' 'State: Review' >"$ROLE_STATE_TICKET"
: >"$ROLE_STATE_ROOT/transitions"
if product_prepare_role_state "$ROLE_STATE_ROOT" T-1 spec-linter; then
  fail "regressive Spec-linter state was accepted"
fi
[[ ! -s "$ROLE_STATE_ROOT/transitions" ]] ||
  fail "invalid role state mutated the ticket before refusal"
eval "$(sed -n '/^validate_product_seed_accounting()/,/^}/p' "$LANE")"
refuse_production_path() { :; }
die() { return 1; }
SEED_ACCOUNTING="$TMP/seed-accounting.json"
SEED_BUNDLE="$TMP/seed.bundle"
SEED_BASE=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
SEED_DAY="$(date -u +%F)"
SEED_NONCE=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
printf '%s\n' seed >"$SEED_BUNDLE"
chmod 600 "$SEED_BUNDLE"
sha256_file() { shasum -a 256 "$1" | awk '{print $1}'; }
seed_bundle_sha="$(sha256_file "$SEED_BUNDLE")"
printf '%s\n' \
  "{\"schema\":\"factory-dev-product-seed-accounting/v2\",\"seed_bundle_sha256\":\"$seed_bundle_sha\",\"base_sha\":\"$SEED_BASE\",\"reserved_micro_usd\":{\"T-1\":90000000,\"T-2\":140000000}}" \
  >"$SEED_ACCOUNTING"
chmod 600 "$SEED_ACCOUNTING"
validate_product_seed_accounting "$SEED_ACCOUNTING" "$SEED_BUNDLE" "$SEED_BASE" T-1 ||
  fail "valid cumulative seed accounting was rejected"
SEED_ACCOUNTING_LINK="$TMP/seed-accounting-link.json"
ln "$SEED_ACCOUNTING" "$SEED_ACCOUNTING_LINK"
if validate_product_seed_accounting "$SEED_ACCOUNTING" "$SEED_BUNDLE" "$SEED_BASE" T-1; then
  fail "hard-linked cumulative seed accounting was accepted"
fi
unlink "$SEED_ACCOUNTING_LINK"
if validate_product_seed_accounting "$SEED_ACCOUNTING" "$SEED_BUNDLE" "$SEED_BASE" T-2; then
  fail "selected exhausted cumulative seed accounting was accepted"
fi
if validate_product_seed_accounting "$SEED_ACCOUNTING" "$SEED_BUNDLE" "$SEED_BASE" T-3; then
  fail "missing selected cumulative seed accounting was accepted"
fi
printf '%s\n' \
  "{\"schema\":\"factory-dev-product-seed-accounting/v2\",\"seed_bundle_sha256\":\"$seed_bundle_sha\",\"base_sha\":\"$SEED_BASE\",\"reserved_micro_usd\":{\"T-1\":90000000,\"T-2\":410000000}}" \
  >"$SEED_ACCOUNTING"
if validate_product_seed_accounting "$SEED_ACCOUNTING" "$SEED_BUNDLE" "$SEED_BASE" T-1; then
  fail "exhausted cumulative global seed accounting was accepted"
fi
printf '%s\n' \
  "{\"schema\":\"factory-dev-product-seed-accounting/v2\",\"seed_bundle_sha256\":\"$seed_bundle_sha\",\"base_sha\":\"$SEED_BASE\",\"reserved_micro_usd\":{\"T-1\":90000000,\"T-2\":140000000}}" \
  >"$SEED_ACCOUNTING"
printf '%s\n' changed >>"$SEED_BUNDLE"
if validate_product_seed_accounting "$SEED_ACCOUNTING" "$SEED_BUNDLE" "$SEED_BASE" T-1; then
  fail "seed accounting detached from its bundle was accepted"
fi
printf '%s\n' seed >"$SEED_BUNDLE"
eval "$(sed -n '/^prepare_product_seed_accounting()/,/^}/p' "$LANE")"
SEED_ROOT="$TMP/seed-root"
mkdir -p "$SEED_ROOT/product/factory" "$SEED_ROOT/runtime"
printf '%s\n' \
  'PER_RUN_BUDGET_USD=10.00' 'PER_TICKET_BUDGET_USD=100.00' \
  'PER_RUN_MAX_TURNS=15' 'PER_RUN_TIMEOUT_MIN=20' 'DAILY_CAP_USD=1000.00' \
  >"$SEED_ROOT/product/factory/ENVELOPE.env"
prepare_product_seed_accounting "$SEED_ROOT" "$SEED_ACCOUNTING" \
  "$SEED_BUNDLE" "$SEED_BASE" T-1
grep -qx 'PER_TICKET_BUDGET_USD=10.000000' \
  "$SEED_ROOT/runtime/product-envelope/T-1.env" ||
  fail "selected ticket remaining budget was not carried"
grep -qx 'GLOBAL_DAILY_CAP_USD=270.000000' \
  "$SEED_ROOT/runtime/product-envelope/global.env" ||
  fail "excluded ticket spend did not reduce the resumed global cap"
[[ ! -e "$SEED_ROOT/runtime/product-envelope/T-2.env" ]] ||
  fail "excluded ticket received an active budget envelope"

printf '%s\n' \
  "{\"schema\":\"factory-dev-product-seed-accounting/v3\",\"seed_bundle_sha256\":\"$seed_bundle_sha\",\"base_sha\":\"$SEED_BASE\",\"ticket_cap_micro_usd\":200000000,\"aggregate_cap_micro_usd\":700000000,\"authorized_by\":\"operator\",\"authorization_nonce\":\"$SEED_NONCE\",\"budget_day\":\"$SEED_DAY\",\"reserved_micro_usd\":{\"T-1\":130000000,\"T-2\":140000000,\"T-3\":80000000,\"T-4\":100000000}}" \
  >"$SEED_ACCOUNTING"
validate_product_seed_accounting "$SEED_ACCOUNTING" "$SEED_BUNDLE" "$SEED_BASE" \
  T-1 T-3 ||
  fail "operator-authorized cumulative seed accounting was rejected"
SEED_ROOT_V3="$TMP/seed-root-v3"
mkdir -p "$SEED_ROOT_V3/product/factory" "$SEED_ROOT_V3/runtime"
cp "$SEED_ROOT/product/factory/ENVELOPE.env" \
  "$SEED_ROOT_V3/product/factory/ENVELOPE.env"
prepare_product_seed_accounting "$SEED_ROOT_V3" "$SEED_ACCOUNTING" \
  "$SEED_BUNDLE" "$SEED_BASE" T-1 T-3
grep -qx 'PER_TICKET_BUDGET_USD=70.000000' \
  "$SEED_ROOT_V3/runtime/product-envelope/T-1.env" ||
  fail "authorized ticket cap did not carry cumulative spend"
grep -qx 'PER_TICKET_BUDGET_USD=120.000000' \
  "$SEED_ROOT_V3/runtime/product-envelope/T-3.env" ||
  fail "authorized sibling ticket cap did not carry cumulative spend"
grep -qx 'GLOBAL_DAILY_CAP_USD=250.000000' \
  "$SEED_ROOT_V3/runtime/product-envelope/global.env" ||
  fail "authorized aggregate cap did not carry cumulative spend"
grep -qx "$SEED_DAY" "$SEED_ROOT_V3/runtime/product-envelope/budget-day" ||
  fail "authorized budget day was not carried"
SEED_ACCOUNTING_V4="$TMP/accounting-v4.json"
SEED_NONCE_V4="$(printf 'v4-%s' "$TMP" | shasum -a 256 | awk '{print $1}')"
printf '%s\n' \
  "{\"schema\":\"factory-dev-product-seed-accounting/v4\",\"seed_bundle_sha256\":\"$seed_bundle_sha\",\"base_sha\":\"$SEED_BASE\",\"ticket_caps_micro_usd\":{\"T-1\":300000000,\"T-2\":200000000,\"T-3\":200000000,\"T-4\":200000000},\"aggregate_cap_micro_usd\":1000000000,\"authorized_by\":\"operator\",\"authorization_nonce\":\"$SEED_NONCE_V4\",\"budget_day\":\"$SEED_DAY\",\"reserved_micro_usd\":{\"T-1\":210000000,\"T-2\":140000000,\"T-3\":150000000,\"T-4\":160000000}}" \
  >"$SEED_ACCOUNTING_V4"
chmod 600 "$SEED_ACCOUNTING_V4"
validate_product_seed_accounting "$SEED_ACCOUNTING_V4" "$SEED_BUNDLE" \
  "$SEED_BASE" T-1 T-3 ||
  fail "operator-authorized per-ticket seed accounting was rejected"
SEED_ACCOUNTING_V4_BAD="$TMP/accounting-v4-bad.json"
sed 's/"T-2":200000000/"T-2":350000001/' "$SEED_ACCOUNTING_V4" \
  >"$SEED_ACCOUNTING_V4_BAD"
chmod 600 "$SEED_ACCOUNTING_V4_BAD"
if validate_product_seed_accounting "$SEED_ACCOUNTING_V4_BAD" "$SEED_BUNDLE" \
  "$SEED_BASE" T-1; then
  fail "oversized per-ticket seed accounting was accepted"
fi
SEED_ROOT_V4="$TMP/seed-root-v4"
mkdir -p "$SEED_ROOT_V4/product/factory" "$SEED_ROOT_V4/runtime"
cp "$SEED_ROOT/product/factory/ENVELOPE.env" \
  "$SEED_ROOT_V4/product/factory/ENVELOPE.env"
prepare_product_seed_accounting "$SEED_ROOT_V4" "$SEED_ACCOUNTING_V4" \
  "$SEED_BUNDLE" "$SEED_BASE" T-1 T-3
grep -qx 'PER_TICKET_BUDGET_USD=90.000000' \
  "$SEED_ROOT_V4/runtime/product-envelope/T-1.env" ||
  fail "per-ticket override did not carry cumulative spend"
grep -qx 'PER_TICKET_BUDGET_USD=50.000000' \
  "$SEED_ROOT_V4/runtime/product-envelope/T-3.env" ||
  fail "unchanged sibling cap did not carry cumulative spend"
grep -qx 'GLOBAL_DAILY_CAP_USD=340.000000' \
  "$SEED_ROOT_V4/runtime/product-envelope/global.env" ||
  fail "per-ticket accounting did not carry aggregate spend"
SEED_ACCOUNTING_V4_HIGH="$TMP/accounting-v4-high.json"
printf '%s\n' \
  "{\"schema\":\"factory-dev-product-seed-accounting/v4\",\"seed_bundle_sha256\":\"$seed_bundle_sha\",\"base_sha\":\"$SEED_BASE\",\"ticket_caps_micro_usd\":{\"T-1\":350000000,\"T-2\":350000000,\"T-3\":300000000,\"T-4\":300000000},\"aggregate_cap_micro_usd\":1500000000,\"authorized_by\":\"operator\",\"authorization_nonce\":\"$SEED_NONCE_V4\",\"budget_day\":\"$SEED_DAY\",\"reserved_micro_usd\":{\"T-1\":210000000,\"T-2\":140000000,\"T-3\":150000000,\"T-4\":160000000}}" \
  >"$SEED_ACCOUNTING_V4_HIGH"
chmod 600 "$SEED_ACCOUNTING_V4_HIGH"
validate_product_seed_accounting "$SEED_ACCOUNTING_V4_HIGH" "$SEED_BUNDLE" \
  "$SEED_BASE" T-1 T-2 ||
  fail "higher operator-authorized development caps were rejected"
eval "$(sed -n '/^validate_product_checkpoint()/,/^seed_product_worktrees()/p' \
  "$LANE" | sed '$d')"
eval "$(sed -n '/^validate_checkpoint_accounting()/,/^prepare_product_seed_accounting()/p' \
  "$LANE" | sed '$d')"
SEED_CHECKPOINT="$TMP/seed-checkpoint.json"
printf '%s\n' \
  "{\"schema\":\"factory-dev-product-checkpoint/v1\",\"base_sha\":\"$SEED_BASE\",\"base_tree\":\"$SEED_BASE\",\"source_factory_sha\":\"$SEED_BASE\",\"source_factory_tree\":\"$SEED_BASE\",\"source_marker_sha256\":\"$SEED_NONCE\",\"source_product_sha256\":\"$SEED_NONCE\",\"prior_accounting_sha256\":null,\"seed_bundle_sha256\":\"$seed_bundle_sha\",\"lane_charges_micro_usd\":{\"T-1\":10000000,\"T-2\":20000000,\"T-3\":30000000,\"T-4\":40000000},\"tickets\":[{\"ticket\":\"T-1\",\"head_sha\":\"$SEED_BASE\",\"head_tree\":\"$SEED_BASE\",\"ticket_blob\":\"$SEED_BASE\",\"route_plan_sha256\":\"$SEED_NONCE\",\"next_stage\":\"RUN spec-linter\",\"state\":\"Ready\",\"roles\":[{\"role\":\"planner\",\"run_id\":\"checkpoint-planner\",\"manifest_sha256\":\"$SEED_NONCE\",\"output_sha256\":\"$SEED_NONCE\",\"role_head_before\":\"$SEED_BASE\"}],\"spec_verdicts\":[]}]}" \
  >"$SEED_CHECKPOINT"
chmod 600 "$SEED_CHECKPOINT"
SEED_CHECKPOINT_SHA="$(sha256_file "$SEED_CHECKPOINT")"
SEED_ACCOUNTING_V5="$TMP/accounting-v5.json"
SEED_NONCE_V5="$(printf 'v5-checkpoint-%s' "$TMP" | shasum -a 256 | awk '{print $1}')"
printf '%s\n' \
  "{\"schema\":\"factory-dev-product-seed-accounting/v5\",\"seed_bundle_sha256\":\"$seed_bundle_sha\",\"checkpoint_sha256\":\"$SEED_CHECKPOINT_SHA\",\"parent_manifest_sha256\":null,\"checkpoint_charges_micro_usd\":{\"T-1\":10000000,\"T-2\":20000000,\"T-3\":30000000,\"T-4\":40000000},\"base_sha\":\"$SEED_BASE\",\"ticket_caps_micro_usd\":{\"T-1\":350000000,\"T-2\":350000000,\"T-3\":300000000,\"T-4\":300000000},\"aggregate_cap_micro_usd\":1500000000,\"authorized_by\":\"operator\",\"authorization_nonce\":\"$SEED_NONCE_V5\",\"budget_day\":\"$SEED_DAY\",\"reserved_micro_usd\":{\"T-1\":10000000,\"T-2\":20000000,\"T-3\":30000000,\"T-4\":40000000}}" \
  >"$SEED_ACCOUNTING_V5"
chmod 600 "$SEED_ACCOUNTING_V5"
validate_product_checkpoint "$SEED_CHECKPOINT" "$SEED_BUNDLE" "$SEED_BASE" \
  T-1 T-2 T-3 T-4 ||
  fail "valid partial pre-Reviewer checkpoint was rejected"
validate_product_checkpoint "$SEED_CHECKPOINT" "$SEED_BUNDLE" "$SEED_BASE" \
  T-1 T-2 ||
  fail "targeted checkpoint lost full original-ticket charges"
SEED_CHECKPOINT_MISSING_CHARGE="$TMP/seed-checkpoint-missing-charge.json"
sed 's/,\"T-2\":20000000//' "$SEED_CHECKPOINT" \
  >"$SEED_CHECKPOINT_MISSING_CHARGE"
chmod 600 "$SEED_CHECKPOINT_MISSING_CHARGE"
if validate_product_checkpoint "$SEED_CHECKPOINT_MISSING_CHARGE" \
  "$SEED_BUNDLE" "$SEED_BASE" T-1 T-2; then
  fail "targeted checkpoint accepted a selected ticket without accounting"
fi
validate_product_seed_accounting "$SEED_ACCOUNTING_V5" "$SEED_BUNDLE" \
  "$SEED_BASE" T-1 T-2 T-3 T-4 ||
  fail "valid checkpoint accounting was rejected"
validate_checkpoint_accounting "$SEED_ACCOUNTING_V5" "$SEED_CHECKPOINT" ||
  fail "checkpoint accounting was not bound to its evidence"
SEED_ACCOUNTING_V5_BAD="$TMP/accounting-v5-bad.json"
sed 's/"T-1":10000000,"T-2":20000000/"T-1":10000001,"T-2":20000000/' \
  "$SEED_ACCOUNTING_V5" >"$SEED_ACCOUNTING_V5_BAD"
chmod 600 "$SEED_ACCOUNTING_V5_BAD"
if validate_checkpoint_accounting "$SEED_ACCOUNTING_V5_BAD" "$SEED_CHECKPOINT"; then
  fail "underreported checkpoint accounting was accepted"
fi
SEED_ACCOUNTING_V5_CAP_BAD="$TMP/accounting-v5-cap-bad.json"
sed 's/"aggregate_cap_micro_usd":1500000000/"aggregate_cap_micro_usd":1500000001/' \
  "$SEED_ACCOUNTING_V5" >"$SEED_ACCOUNTING_V5_CAP_BAD"
chmod 600 "$SEED_ACCOUNTING_V5_CAP_BAD"
SEED_ROOT_V5_BAD="$TMP/seed-root-v5-bad"
mkdir -p "$SEED_ROOT_V5_BAD/product/factory" "$SEED_ROOT_V5_BAD/runtime"
cp "$SEED_ROOT/product/factory/ENVELOPE.env" \
  "$SEED_ROOT_V5_BAD/product/factory/ENVELOPE.env"
validate_product_seed_accounting() { :; }
expect_failure "v5 internal aggregate defense" prepare_product_seed_accounting \
  "$SEED_ROOT_V5_BAD" "$SEED_ACCOUNTING_V5_CAP_BAD" "$SEED_BUNDLE" \
  "$SEED_BASE" T-1
eval "$(sed -n '/^validate_product_seed_accounting()/,/^}/p' "$LANE")"

CHECKPOINT_SEQ_REPO="$TMP/checkpoint-sequencer"
git clone -q "$ROOT" "$CHECKPOINT_SEQ_REPO"
for ticket in T-991 T-992; do
  printf '%s\n' "# $ticket checkpoint fixture" '' 'State: Ready' \
    >"$CHECKPOINT_SEQ_REPO/conformance/factory/tickets/$ticket.md"
done
printf '%s\n' '# T-993 checkpoint fixture' '' 'State: Review' \
  'SPEC-LINT: PASS' \
  >"$CHECKPOINT_SEQ_REPO/conformance/factory/tickets/T-993.md"
git -C "$CHECKPOINT_SEQ_REPO" add conformance/factory/tickets
git -C "$CHECKPOINT_SEQ_REPO" -c user.name=Test -c user.email=test@local \
  commit -qm 'Add checkpoint sequencing fixtures'
CHECKPOINT_SEQ_HEAD="$(git -C "$CHECKPOINT_SEQ_REPO" rev-parse HEAD)"
CHECKPOINT_SEQ_TREE="$(git -C "$CHECKPOINT_SEQ_REPO" rev-parse 'HEAD^{tree}')"
CHECKPOINT_LANE="$TMP/nysa-sf-dev.checkpoint"
mkdir -m 700 -p "$CHECKPOINT_LANE/runtime"
printf '%s\n' '{"mode":"product"}' >"$CHECKPOINT_LANE/marker.json"
chmod 600 "$CHECKPOINT_LANE/marker.json"
CHECKPOINT_IMPORT="$CHECKPOINT_LANE/runtime/product-checkpoint-import.json"
printf '%s\n' \
  "{\"schema\":\"factory-dev-product-checkpoint-import/v1\",\"checkpoint_sha256\":\"$SEED_NONCE\",\"tickets\":[{\"ticket\":\"T-991\",\"import_head\":\"$CHECKPOINT_SEQ_HEAD\",\"import_tree\":\"$CHECKPOINT_SEQ_TREE\",\"roles\":[\"planner\"],\"spec_verdicts\":[],\"expected_next_stage\":\"RUN spec-linter\"},{\"ticket\":\"T-993\",\"import_head\":\"$CHECKPOINT_SEQ_HEAD\",\"import_tree\":\"$CHECKPOINT_SEQ_TREE\",\"roles\":[\"planner\",\"spec-linter\",\"test-author\",\"builder\"],\"spec_verdicts\":[\"SPEC-LINT: PASS\"],\"expected_next_stage\":\"RUN reviewer\"}]}" \
  >"$CHECKPOINT_IMPORT"
chmod 600 "$CHECKPOINT_IMPORT"
CHECKPOINT_LEDGER="$CHECKPOINT_SEQ_REPO/conformance/factory/checkpoint-ledger.csv"
head -n 1 "$CHECKPOINT_SEQ_REPO/conformance/factory/ledger.csv" >"$CHECKPOINT_LEDGER"
checkpoint_next_stage() {
  env FACTORY_ROOT="$CHECKPOINT_SEQ_REPO/conformance" \
    FACTORY_LEDGER="$CHECKPOINT_LEDGER" \
    FACTORY_HERMES_CONTRACT_VERSION=1.7.0 \
    FACTORY_CLI_LANE_ROOT="$CHECKPOINT_LANE" \
    FACTORY_DEV_PRODUCT_CHECKPOINT="$CHECKPOINT_IMPORT" \
    bash "$CHECKPOINT_SEQ_REPO/scripts/next-stage.sh" \
      --ticket "$1" --workdir "$CHECKPOINT_SEQ_REPO/conformance"
}
AUTH_CHECKPOINT_STAGE="$(checkpoint_next_stage T-991)"
[[ "$AUTH_CHECKPOINT_STAGE" == "RUN spec-linter" ]] ||
  fail "Planner checkpoint did not resume at Spec-linter"
mkdir -p "$CHECKPOINT_LANE/worktrees/T-991/factory/tickets"
cp "$CHECKPOINT_SEQ_REPO/conformance/factory/tickets/T-991.md" \
  "$CHECKPOINT_LANE/worktrees/T-991/factory/tickets/T-991.md"
: >"$CHECKPOINT_LANE/transitions"
product_prepare_role_state "$CHECKPOINT_LANE" T-991 \
  "$(product_role_for_stage "$AUTH_CHECKPOINT_STAGE")" ||
  fail "authenticated Ready checkpoint could not normalize before Spec-linter"
grep -qx 'State: Planning' \
  "$CHECKPOINT_LANE/worktrees/T-991/factory/tickets/T-991.md" ||
  fail "authenticated Ready checkpoint remained outside Planning"
[[ "$(checkpoint_next_stage T-993)" == "RUN reviewer" ]] ||
  fail "Builder checkpoint did not resume at Reviewer"
[[ "$(checkpoint_next_stage T-992)" == "RUN planner" ]] ||
  fail "ticket omitted from checkpoint did not remain at Planner"

printf '%s\n' 'SPEC-LINT: FAIL — current-lane finding' \
  >>"$CHECKPOINT_SEQ_REPO/conformance/factory/tickets/T-991.md"
git -C "$CHECKPOINT_SEQ_REPO" add conformance/factory/tickets/T-991.md
git -C "$CHECKPOINT_SEQ_REPO" -c user.name=Test -c user.email=test@local \
  commit -qm 'Record current-lane checkpoint verdict'
printf '%s\n' \
  '2026-07-24,00:00:00,T-991,spec-linter,mock,test,1,0.10,0,current-lint,mock,,,test_fixture,test' \
  >>"$CHECKPOINT_LEDGER"
[[ "$(checkpoint_next_stage T-991)" == "RUN planner" ]] ||
  fail "current-lane verdict did not extend the checkpoint prefix"

cp "$CHECKPOINT_IMPORT" "$CHECKPOINT_IMPORT.good"
sed 's/SPEC-LINT: PASS/SPEC-LINT: FAIL/' \
  "$CHECKPOINT_IMPORT.good" >"$CHECKPOINT_IMPORT"
expect_failure "altered checkpoint spec prefix" checkpoint_next_stage T-993
cp "$CHECKPOINT_IMPORT.good" "$CHECKPOINT_IMPORT"

printf '%s\n' 'SPEC-LINT: PASS' \
  >>"$CHECKPOINT_SEQ_REPO/conformance/factory/tickets/T-991.md"
git -C "$CHECKPOINT_SEQ_REPO" add conformance/factory/tickets/T-991.md
git -C "$CHECKPOINT_SEQ_REPO" -c user.name=Test -c user.email=test@local \
  commit -qm 'Add unmatched current-lane verdict'
expect_failure "current-lane verdict without successful run" \
  checkpoint_next_stage T-991

sed "s/\"import_tree\":\"$CHECKPOINT_SEQ_TREE\"/\"import_tree\":\"$SEED_BASE\"/" \
  "$CHECKPOINT_IMPORT.good" >"$CHECKPOINT_IMPORT"
expect_failure "checkpoint head tree drift" checkpoint_next_stage T-991
mv "$CHECKPOINT_IMPORT.good" "$CHECKPOINT_IMPORT"

eval "$(sed -n '/^product_export_roles_complete()/,/^export_product_internal()/p' \
  "$LANE" | sed '$d')"
EXPORT_GATE_ROOT="$TMP/checkpoint-export-gate"
mkdir -p "$EXPORT_GATE_ROOT/product/factory/runs" "$EXPORT_GATE_ROOT/runtime"
cp "$CHECKPOINT_IMPORT" "$EXPORT_GATE_ROOT/runtime/product-checkpoint-import.json"
printf '%s\n' 'ticket=T-993' 'role=reviewer' 'accounting_state=completed' \
  'exit_status=0' >"$EXPORT_GATE_ROOT/product/factory/runs/reviewer.meta"
expect_failure "checkpoint export without current Narrator" \
  product_export_roles_complete "$EXPORT_GATE_ROOT" T-993
printf '%s\n' 'ticket=T-993' 'role=narrator' 'accounting_state=completed' \
  'exit_status=0' >"$EXPORT_GATE_ROOT/product/factory/runs/narrator.meta"
product_export_roles_complete "$EXPORT_GATE_ROOT" T-993 ||
  fail "checkpoint export rejected current Reviewer and Narrator"

eval "$(sed -n '/^write_product_checkpoint_import()/,/^validate_product_seed_accounting()/p' \
  "$LANE" | sed '$d')"
eval "$(sed -n '/^write_product_checkpoint()/,/^product_export_roles_complete()/p' \
  "$LANE" | sed '$d')"
CHAIN_ROOT="$TMP/checkpoint-chain"
mkdir -p "$CHAIN_ROOT/runtime" "$CHAIN_ROOT/product/factory/runs" \
  "$CHAIN_ROOT/worktrees"
CHAIN_NONCE="$(printf 'checkpoint-chain-%s' "$TMP" | shasum -a 256 | awk '{print $1}')"
for ticket in T-046 T-048; do
  work="$CHAIN_ROOT/worktrees/$ticket"
  git init -q "$work"
  mkdir -p "$work/factory/tickets" "$work/factory/route-plans"
  if [[ "$ticket" == T-046 ]]; then
    printf '%s\n' "# $ticket chain fixture" '' 'State: Ready' \
      >"$work/factory/tickets/$ticket.md"
  else
    printf '%s\n' "# $ticket chain fixture" '' 'State: Review' 'SPEC-LINT: PASS' \
      >"$work/factory/tickets/$ticket.md"
  fi
  printf '%s\n' '{"route":"checkpoint-chain"}' \
    >"$work/factory/route-plans/$ticket.json"
  git -C "$work" add factory
  git -C "$work" -c user.name=Test -c user.email=test@local \
    commit -qm 'Import checkpoint prefix'
  git -C "$work" update-ref "refs/remotes/origin/ticket/$ticket" HEAD
done
CHAIN_T46_HEAD="$(git -C "$CHAIN_ROOT/worktrees/T-046" rev-parse HEAD)"
CHAIN_T46_TREE="$(git -C "$CHAIN_ROOT/worktrees/T-046" rev-parse 'HEAD^{tree}')"
CHAIN_T48_HEAD="$(git -C "$CHAIN_ROOT/worktrees/T-048" rev-parse HEAD)"
CHAIN_T48_TREE="$(git -C "$CHAIN_ROOT/worktrees/T-048" rev-parse 'HEAD^{tree}')"
CHAIN_SOURCE="$CHAIN_ROOT/source-checkpoint.json"
printf '%s\n' \
  "{\"schema\":\"factory-dev-product-checkpoint/v1\",\"base_sha\":\"$CHAIN_T46_HEAD\",\"base_tree\":\"$CHAIN_T46_TREE\",\"source_factory_sha\":\"$CHAIN_T46_HEAD\",\"source_factory_tree\":\"$CHAIN_T46_TREE\",\"source_marker_sha256\":\"$CHAIN_NONCE\",\"source_product_sha256\":\"$CHAIN_NONCE\",\"prior_accounting_sha256\":null,\"seed_bundle_sha256\":\"$CHAIN_NONCE\",\"lane_charges_micro_usd\":{\"T-045\":0,\"T-046\":11000000,\"T-047\":0,\"T-048\":13000000},\"tickets\":[{\"ticket\":\"T-046\",\"head_sha\":\"$CHAIN_T46_HEAD\",\"head_tree\":\"$CHAIN_T46_TREE\",\"ticket_blob\":\"$CHAIN_T46_HEAD\",\"route_plan_sha256\":\"$CHAIN_NONCE\",\"next_stage\":\"RUN spec-linter\",\"state\":\"Ready\",\"roles\":[{\"role\":\"planner\",\"run_id\":\"prior-t46-planner\",\"manifest_sha256\":\"$CHAIN_NONCE\",\"output_sha256\":\"$CHAIN_NONCE\",\"role_head_before\":\"$CHAIN_T46_HEAD\"}],\"spec_verdicts\":[]},{\"ticket\":\"T-048\",\"head_sha\":\"$CHAIN_T48_HEAD\",\"head_tree\":\"$CHAIN_T48_TREE\",\"ticket_blob\":\"$CHAIN_T48_HEAD\",\"route_plan_sha256\":\"$CHAIN_NONCE\",\"next_stage\":\"RUN reviewer\",\"state\":\"Review\",\"roles\":[{\"role\":\"planner\",\"run_id\":\"prior-t48-planner\",\"manifest_sha256\":\"$CHAIN_NONCE\",\"output_sha256\":\"$CHAIN_NONCE\",\"role_head_before\":\"$CHAIN_T48_HEAD\"},{\"role\":\"spec-linter\",\"run_id\":\"prior-t48-spec\",\"manifest_sha256\":\"$CHAIN_NONCE\",\"output_sha256\":\"$CHAIN_NONCE\",\"role_head_before\":\"$CHAIN_T48_HEAD\"},{\"role\":\"test-author\",\"run_id\":\"prior-t48-tests\",\"manifest_sha256\":\"$CHAIN_NONCE\",\"output_sha256\":\"$CHAIN_NONCE\",\"role_head_before\":\"$CHAIN_T48_HEAD\"},{\"role\":\"builder\",\"run_id\":\"prior-t48-builder\",\"manifest_sha256\":\"$CHAIN_NONCE\",\"output_sha256\":\"$CHAIN_NONCE\",\"role_head_before\":\"$CHAIN_T48_HEAD\"}],\"spec_verdicts\":[\"SPEC-LINT: PASS\"]}]}" \
  >"$CHAIN_SOURCE"
chmod 600 "$CHAIN_SOURCE"
PRODUCT_TICKETS=(T-046 T-048)
write_product_checkpoint_import "$CHAIN_ROOT" "$CHAIN_SOURCE"
cmp -s "$CHAIN_SOURCE" "$CHAIN_ROOT/runtime/product-checkpoint-source.json" ||
  fail "checkpoint import did not retain the exact source"
[[ "$(stat -f '%Su:%Lp:%l' \
  "$CHAIN_ROOT/runtime/product-checkpoint-source.json")" == "$(id -un):600:1" ]] ||
  fail "retained checkpoint source is unsafe"
printf '%s\n' 'SPEC-LINT: FAIL — retry Planner' \
  >>"$CHAIN_ROOT/worktrees/T-046/factory/tickets/T-046.md"
git -C "$CHAIN_ROOT/worktrees/T-046" add factory/tickets/T-046.md
git -C "$CHAIN_ROOT/worktrees/T-046" -c user.name=Test -c user.email=test@local \
  commit -qm 'Record current Spec-linter failure'
git -C "$CHAIN_ROOT/worktrees/T-046" update-ref \
  refs/remotes/origin/ticket/T-046 HEAD
CHAIN_CURRENT_OUT="$CHAIN_ROOT/product/factory/runs/current-spec.out"
printf '%s\n' 'SPEC-LINT: FAIL — retry Planner' >"$CHAIN_CURRENT_OUT"
CHAIN_CURRENT_OUT_SHA="$(sha256_file "$CHAIN_CURRENT_OUT")"
printf '%s\n' 'run_id=current-spec' 'ticket=T-046' 'role=spec-linter' \
  'phase=completed' 'accounting_state=completed' 'contract_version=1.7.0' \
  'exit_status=0' 'role_exit=ok' 'task_submitted=1' 'go_issued=1' \
  "output_sha256=$CHAIN_CURRENT_OUT_SHA" "role_head_before=$CHAIN_T46_HEAD" \
  'effective_cost=7.000000' \
  >"$CHAIN_ROOT/product/factory/runs/current-spec.meta"
printf '%s\n' 'ticket,role,run_id,exit_status' \
  'T-046,spec-linter,current-spec,0' \
  >"$CHAIN_ROOT/product/factory/runtime-ledger.csv"
CHAIN_CHECKPOINT_SHA="$(sha256_file "$CHAIN_SOURCE")"
printf '%s\n' \
  "{\"schema\":\"factory-dev-product-source/v1\",\"base_sha\":\"$CHAIN_T46_HEAD\",\"base_tree\":\"$CHAIN_T46_TREE\",\"lane_control_sha\":\"$CHAIN_T46_HEAD\",\"seed_bundle_sha256\":\"$CHAIN_NONCE\",\"seed_accounting_sha256\":\"$CHAIN_NONCE\",\"seed_lineage_sha256\":\"$CHAIN_NONCE\",\"seed_checkpoint_sha256\":\"$CHAIN_CHECKPOINT_SHA\",\"tickets\":[\"T-046\",\"T-048\"],\"resume_original_tickets\":[\"T-045\",\"T-046\",\"T-047\",\"T-048\"]}" \
  >"$CHAIN_ROOT/runtime/product-source.json"
printf '%s\n' \
  "{\"kit_sha\":\"$CHAIN_T46_HEAD\",\"kit_tree\":\"$CHAIN_T46_TREE\"}" \
  >"$CHAIN_ROOT/marker.json"
printf '%s\n' 'new chained bundle' >"$CHAIN_ROOT/seed.bundle"
write_product_checkpoint "$CHAIN_ROOT" "$CHAIN_ROOT/seed.bundle" \
  "$CHAIN_ROOT/chained.json" T-046 T-048 ||
  fail "checkpoint chaining rejected valid prior roles"
python3 - "$CHAIN_ROOT/chained.json" "$CHAIN_SOURCE" "$CHAIN_NONCE" <<'PY' ||
import json, sys
chained=json.load(open(sys.argv[1])); source=json.load(open(sys.argv[2]))
new={item["ticket"]:item for item in chained["tickets"]}
old={item["ticket"]:item for item in source["tickets"]}
if (new["T-046"]["next_stage"] != "RUN planner" or
    new["T-046"]["roles"][0] != old["T-046"]["roles"][0] or
    [run["role"] for run in new["T-046"]["roles"]] !=
        ["planner","spec-linter"] or
    new["T-048"]["next_stage"] != "RUN reviewer" or
    new["T-048"]["roles"] != old["T-048"]["roles"] or
    chained["lane_charges_micro_usd"] !=
        {"T-045":0,"T-046":7000000,"T-047":0,"T-048":0} or
    chained["prior_accounting_sha256"] != sys.argv[3]):
    raise SystemExit(1)
PY
  fail "chained checkpoint lost sequence, evidence, accounting, or stage"
validate_product_checkpoint "$CHAIN_ROOT/chained.json" \
  "$CHAIN_ROOT/seed.bundle" "$CHAIN_T46_HEAD" T-046 T-048 ||
  fail "two-ticket export with four-ticket charge history was rejected"
rm -f "$CHAIN_ROOT/runtime/product-checkpoint-import.json" \
  "$CHAIN_ROOT/runtime/product-checkpoint-source.json"
PRODUCT_TICKETS=(T-046 T-048)
write_product_checkpoint_import "$CHAIN_ROOT" "$CHAIN_ROOT/chained.json"
python3 - "$CHAIN_ROOT/runtime/product-checkpoint-import.json" <<'PY' ||
import json, sys
if [item["ticket"] for item in
    json.load(open(sys.argv[1],encoding="utf-8"))["tickets"]] != ["T-046","T-048"]:
    raise SystemExit(1)
PY
  fail "two-ticket checkpoint import changed the selected ticket set"
printf '%s\n' \
  "{\"schema\":\"factory-dev-product-source/v1\",\"base_sha\":\"$CHAIN_T46_HEAD\",\"base_tree\":\"$CHAIN_T46_TREE\",\"lane_control_sha\":\"$CHAIN_T46_HEAD\",\"seed_bundle_sha256\":\"$CHAIN_NONCE\",\"seed_accounting_sha256\":\"$CHAIN_NONCE\",\"seed_lineage_sha256\":\"$CHAIN_NONCE\",\"seed_checkpoint_sha256\":\"$(sha256_file "$CHAIN_ROOT/chained.json")\",\"tickets\":[\"T-046\",\"T-048\"]}" \
  >"$CHAIN_ROOT/runtime/product-source.json"
rm -f "$CHAIN_ROOT/product/factory/runs/current-spec.meta" \
  "$CHAIN_ROOT/product/factory/runs/current-spec.out"
printf '%s\n' 'ticket,role,run_id,exit_status' \
  >"$CHAIN_ROOT/product/factory/runtime-ledger.csv"
printf '%s\n' 'second chained bundle' >"$CHAIN_ROOT/seed-2.bundle"
chmod 600 "$CHAIN_ROOT/seed-2.bundle"
write_product_checkpoint "$CHAIN_ROOT" "$CHAIN_ROOT/seed-2.bundle" \
  "$CHAIN_ROOT/chained-2.json" T-046 T-048 ||
  fail "second targeted checkpoint chain lost its full accounting universe"
python3 - "$CHAIN_ROOT/chained-2.json" <<'PY' ||
import json, sys
value=json.load(open(sys.argv[1],encoding="utf-8"))
if (set(value["lane_charges_micro_usd"]) !=
        {"T-045","T-046","T-047","T-048"} or
    any(value["lane_charges_micro_usd"].values())):
    raise SystemExit(1)
PY
  fail "second targeted checkpoint did not retain four-ticket charge keys"
validate_product_checkpoint "$CHAIN_ROOT/chained-2.json" \
  "$CHAIN_ROOT/seed-2.bundle" "$CHAIN_T46_HEAD" T-046 T-048 ||
  fail "second targeted checkpoint failed selected-ticket validation"
CHAIN_SECOND_SHA="$(sha256_file "$CHAIN_ROOT/chained-2.json")"
CHAIN_SECOND_BUNDLE_SHA="$(sha256_file "$CHAIN_ROOT/seed-2.bundle")"
CHAIN_V5="$CHAIN_ROOT/accounting-v5.json"
printf '%s\n' \
  "{\"schema\":\"factory-dev-product-seed-accounting/v5\",\"seed_bundle_sha256\":\"$CHAIN_SECOND_BUNDLE_SHA\",\"checkpoint_sha256\":\"$CHAIN_SECOND_SHA\",\"parent_manifest_sha256\":\"$CHAIN_NONCE\",\"checkpoint_charges_micro_usd\":{\"T-045\":0,\"T-046\":0,\"T-047\":0,\"T-048\":0},\"base_sha\":\"$CHAIN_T46_HEAD\",\"ticket_caps_micro_usd\":{\"T-045\":350000000,\"T-046\":350000000,\"T-047\":350000000,\"T-048\":350000000},\"aggregate_cap_micro_usd\":1500000000,\"authorized_by\":\"operator\",\"authorization_nonce\":\"$CHAIN_NONCE\",\"budget_day\":\"$(date -u +%F)\",\"reserved_micro_usd\":{\"T-045\":0,\"T-046\":0,\"T-047\":0,\"T-048\":0}}" \
  >"$CHAIN_V5"
chmod 600 "$CHAIN_V5"
validate_product_seed_accounting "$CHAIN_V5" "$CHAIN_ROOT/seed-2.bundle" \
  "$CHAIN_T46_HEAD" T-046 T-048 ||
  fail "second targeted checkpoint v5 successor was rejected"
validate_checkpoint_accounting "$CHAIN_V5" "$CHAIN_ROOT/chained-2.json" ||
  fail "second targeted checkpoint detached from its v5 successor"
CHAIN_IMPORT="$CHAIN_ROOT/runtime/product-checkpoint-import.json"
cp "$CHAIN_IMPORT" "$CHAIN_IMPORT.good"
CHAIN_IMPORTED_T46_HEAD="$(python3 - "$CHAIN_IMPORT" <<'PY'
import json, sys
print(next(item["import_head"] for item in
    json.load(open(sys.argv[1],encoding="utf-8"))["tickets"]
    if item["ticket"] == "T-046"))
PY
)"
CHAIN_ROGUE_HEAD="$(printf 'detached checkpoint head\n' | \
  git -C "$CHAIN_ROOT/worktrees/T-046" -c user.name=Test \
    -c user.email=test@local commit-tree "$CHAIN_T46_TREE")"
sed "s/\"import_head\":\"$CHAIN_IMPORTED_T46_HEAD\"/\"import_head\":\"$CHAIN_ROGUE_HEAD\"/" \
  "$CHAIN_IMPORT.good" >"$CHAIN_IMPORT"
expect_failure "detached imported checkpoint head" write_product_checkpoint \
  "$CHAIN_ROOT" "$CHAIN_ROOT/seed.bundle" "$CHAIN_ROOT/detached.json" T-046 T-048
mv "$CHAIN_IMPORT.good" "$CHAIN_IMPORT"
cp "$CHAIN_ROOT/runtime/product-checkpoint-source.json" \
  "$CHAIN_ROOT/runtime/product-checkpoint-source.good"
printf '\n' >>"$CHAIN_ROOT/runtime/product-checkpoint-source.json"
expect_failure "altered retained checkpoint source" write_product_checkpoint \
  "$CHAIN_ROOT" "$CHAIN_ROOT/seed.bundle" "$CHAIN_ROOT/altered.json" T-046 T-048
mv "$CHAIN_ROOT/runtime/product-checkpoint-source.good" \
  "$CHAIN_ROOT/runtime/product-checkpoint-source.json"

eval "$(sed -n '/^consume_product_seed_authorization()/,/^}/p' "$LANE")"
eval "$(sed -n '/^product_seed_lineage_id()/,/^}/p' "$LANE")"
eval "$(sed -n '/^write_product_seed_lineage()/,/^}/p' "$LANE")"
physical() { (cd "$1" 2>/dev/null && pwd -P); }
SEED_LINEAGE="$TMP/seed-lineage.json"
SEED_LINEAGE_ID="$(product_seed_lineage_id "$SEED_ACCOUNTING")"
SEED_MANIFEST_SHA="$(sha256_file "$SEED_ACCOUNTING")"
write_product_seed_lineage "$SEED_ACCOUNTING" "$SEED_LINEAGE"
SEED_V5_ROOT="$TMP/v5-lineage"
mkdir -m 700 "$SEED_V5_ROOT"
cp "$SEED_ACCOUNTING_V5" "$SEED_V5_ROOT/accounting.json"
SEED_ACCOUNTING_V5_ISOLATED="$SEED_V5_ROOT/accounting.json"
SEED_LINEAGE_V5="$SEED_V5_ROOT/lineage.json"
write_product_seed_lineage "$SEED_ACCOUNTING_V5_ISOLATED" "$SEED_LINEAGE_V5"
SEED_ACCOUNTING_V5_SHA="$(sha256_file "$SEED_ACCOUNTING_V5_ISOLATED")"
consume_product_seed_authorization "$SEED_ACCOUNTING_V5_ISOLATED" \
  "$SEED_ACCOUNTING_V5_SHA" "$SEED_LINEAGE_V5"
CHECKPOINT_CONSUMPTION="$SEED_V5_ROOT/.seed-accounting-lineages/$(product_seed_lineage_id "$SEED_ACCOUNTING_V5_ISOLATED")/checkpoints/$SEED_CHECKPOINT_SHA.used/receipt"
[[ "$(stat -f '%Su:%Lp' "$CHECKPOINT_CONSUMPTION")" == "$(id -un):600" ]] ||
  fail "checkpoint consumption receipt is unsafe"
die() { exit 1; }
if (consume_product_seed_authorization "$SEED_ACCOUNTING_V5_ISOLATED" \
    "$SEED_ACCOUNTING_V5_SHA" "$SEED_LINEAGE_V5"); then
  fail "checkpoint authorization was consumed twice"
fi
die() { return 1; }
ROGUE_LINEAGE="$TMP/rogue-lineage.json"
printf '%s\n' \
  "{\"schema\":\"factory-dev-product-seed-lineage/v1\",\"lineage_id\":\"$(printf rogue | shasum -a 256 | awk '{print $1}')\",\"parent_manifest_sha256\":null,\"manifest_sha256\":\"$SEED_MANIFEST_SHA\"}" \
  >"$ROGUE_LINEAGE"
chmod 600 "$ROGUE_LINEAGE"
die() { exit 1; }
if (consume_product_seed_authorization "$SEED_ACCOUNTING" \
    "$SEED_MANIFEST_SHA" "$ROGUE_LINEAGE"); then
  fail "caller-selected sibling lineage identity was accepted"
fi
die() { return 1; }
consume_product_seed_authorization "$SEED_ACCOUNTING" \
  "$SEED_MANIFEST_SHA" "$SEED_LINEAGE"
CONSUMPTION="$TMP/.seed-accounting-lineages/$SEED_LINEAGE_ID/nonces/$SEED_NONCE.used/receipt"
[[ "$(stat -f '%Su:%Lp' "$CONSUMPTION")" == "$(id -un):600" ]] ||
  fail "seed authorization consumption receipt is unsafe"
[[ "$(sed -n '1p' "$TMP/.seed-accounting-lineages/$SEED_LINEAGE_ID/head")" == \
   "$SEED_MANIFEST_SHA" ]] ||
  fail "seed accounting lineage head was not advanced"
die() { exit 1; }
if (consume_product_seed_authorization "$SEED_ACCOUNTING" \
    "$SEED_MANIFEST_SHA" "$SEED_LINEAGE"); then
  fail "seed authorization was consumed twice"
fi
die() { return 1; }
SEED_SUCCESSOR="$TMP/seed-successor.json"
SEED_SIBLING="$TMP/seed-sibling.json"
SEED_SUCCESSOR_NONCE="$(printf 'successor-%s' "$TMP" | shasum -a 256 | awk '{print $1}')"
SEED_SIBLING_NONCE="$(printf 'sibling-%s' "$TMP" | shasum -a 256 | awk '{print $1}')"
sed "s/$SEED_NONCE/$SEED_SUCCESSOR_NONCE/" "$SEED_ACCOUNTING" >"$SEED_SUCCESSOR"
sed "s/$SEED_NONCE/$SEED_SIBLING_NONCE/" "$SEED_ACCOUNTING" >"$SEED_SIBLING"
chmod 600 "$SEED_SUCCESSOR" "$SEED_SIBLING"
SEED_SUCCESSOR_SHA="$(sha256_file "$SEED_SUCCESSOR")"
SEED_SIBLING_SHA="$(sha256_file "$SEED_SIBLING")"
SEED_SUCCESSOR_LINEAGE="$TMP/seed-successor-lineage.json"
SEED_SIBLING_LINEAGE="$TMP/seed-sibling-lineage.json"
write_product_seed_lineage "$SEED_SUCCESSOR" "$SEED_SUCCESSOR_LINEAGE" \
  "$SEED_ACCOUNTING"
write_product_seed_lineage "$SEED_SIBLING" "$SEED_SIBLING_LINEAGE" \
  "$SEED_ACCOUNTING"
consume_product_seed_authorization "$SEED_SUCCESSOR" \
  "$SEED_SUCCESSOR_SHA" "$SEED_SUCCESSOR_LINEAGE"
die() { exit 1; }
if (consume_product_seed_authorization "$SEED_SIBLING" \
    "$SEED_SIBLING_SHA" "$SEED_SIBLING_LINEAGE"); then
  fail "stale sibling seed accounting lineage was consumed"
fi
die() { return 1; }
printf '%s\n' \
  "{\"schema\":\"factory-dev-product-seed-accounting/v3\",\"seed_bundle_sha256\":\"$seed_bundle_sha\",\"base_sha\":\"$SEED_BASE\",\"ticket_cap_micro_usd\":200000000,\"aggregate_cap_micro_usd\":700000000,\"authorized_by\":\"operator\",\"authorization_nonce\":\"$SEED_NONCE\",\"budget_day\":\"$SEED_DAY\",\"reserved_micro_usd\":{\"T-1\":200000000}}" \
  >"$SEED_ACCOUNTING"
if validate_product_seed_accounting "$SEED_ACCOUNTING" "$SEED_BUNDLE" "$SEED_BASE" T-1; then
  fail "authorized exhausted ticket accounting was accepted"
fi
printf '%s\n' \
  "{\"schema\":\"factory-dev-product-seed-accounting/v3\",\"seed_bundle_sha256\":\"$seed_bundle_sha\",\"base_sha\":\"$SEED_BASE\",\"ticket_cap_micro_usd\":200000000,\"aggregate_cap_micro_usd\":700000001,\"authorized_by\":\"operator\",\"authorization_nonce\":\"$SEED_NONCE\",\"budget_day\":\"$SEED_DAY\",\"reserved_micro_usd\":{\"T-1\":0}}" \
  >"$SEED_ACCOUNTING"
if validate_product_seed_accounting "$SEED_ACCOUNTING" "$SEED_BUNDLE" "$SEED_BASE" T-1; then
  fail "unauthorized aggregate cap was accepted"
fi
printf '%s\n' \
  "{\"schema\":\"factory-dev-product-seed-accounting/v3\",\"seed_bundle_sha256\":\"$seed_bundle_sha\",\"base_sha\":\"$SEED_BASE\",\"ticket_cap_micro_usd\":200000000,\"aggregate_cap_micro_usd\":700000000,\"authorized_by\":\"agent\",\"authorization_nonce\":\"$SEED_NONCE\",\"budget_day\":\"$SEED_DAY\",\"reserved_micro_usd\":{\"T-1\":0}}" \
  >"$SEED_ACCOUNTING"
if validate_product_seed_accounting "$SEED_ACCOUNTING" "$SEED_BUNDLE" "$SEED_BASE" T-1; then
  fail "agent-authored budget authorization was accepted"
fi
STALE_SEED_DAY="$(python3 -c 'import datetime; print(datetime.datetime.now(datetime.timezone.utc).date()-datetime.timedelta(days=1))')"
printf '%s\n' \
  "{\"schema\":\"factory-dev-product-seed-accounting/v3\",\"seed_bundle_sha256\":\"$seed_bundle_sha\",\"base_sha\":\"$SEED_BASE\",\"ticket_cap_micro_usd\":200000000,\"aggregate_cap_micro_usd\":700000000,\"authorized_by\":\"operator\",\"authorization_nonce\":\"$SEED_NONCE\",\"budget_day\":\"$STALE_SEED_DAY\",\"reserved_micro_usd\":{\"T-1\":0}}" \
  >"$SEED_ACCOUNTING"
if validate_product_seed_accounting "$SEED_ACCOUNTING" "$SEED_BUNDLE" "$SEED_BASE" T-1; then
  fail "stale authorized budget day was accepted"
fi

SEED_HISTORY="$TMP/seed-history"
SEED_HISTORY_ROOT="$TMP/seed-history-root"
mkdir -p "$SEED_HISTORY/factory/tickets" "$SEED_HISTORY/factory/route-plans" \
  "$SEED_HISTORY/app" "$SEED_HISTORY_ROOT/worktrees"
git -C "$SEED_HISTORY" init -q
printf '%s\n' 'State: Ready' >"$SEED_HISTORY/factory/tickets/T-1.md"
printf '%s\n' base >"$SEED_HISTORY/app/base"
git -C "$SEED_HISTORY" add .
git -C "$SEED_HISTORY" -c user.name=Base -c user.email=base@local \
  commit -qm 'Create base'
SEED_HISTORY_BASE="$(git -C "$SEED_HISTORY" rev-parse HEAD)"
printf '%s\n' kit >"$SEED_HISTORY/factory/KIT_PIN"
printf '%s\n' \
  'WORKTREES_DIR="/private/tmp/nysa-sf-dev.trusted-control/worktrees"' \
  >"$SEED_HISTORY/factory/PROJECT.env"
git -C "$SEED_HISTORY" add factory
git -C "$SEED_HISTORY" -c user.name='Factory Dev Lane' \
  -c user.email=factory-dev@local commit -qm \
  'Configure isolated Contract 1.7 product lane'
printf '%s\n' before >"$SEED_HISTORY/app/before"
git -C "$SEED_HISTORY" add app/before
git -C "$SEED_HISTORY" -c user.name='Software Factory' \
  -c user.email=factory@local commit -qm 'T-1: retain earlier lifecycle output'
printf '%s\n' '{}' >"$SEED_HISTORY/factory/route-plans/T-1.json"
printf '\n%s\n' 'Kit-SHA: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' \
  >>"$SEED_HISTORY/factory/tickets/T-1.md"
git -C "$SEED_HISTORY" add factory/route-plans/T-1.json \
  factory/tickets/T-1.md
git -C "$SEED_HISTORY" -c user.name='Software Factory' \
  -c user.email=factory@local commit -qm 'T-1: pin kit and model route plan'
printf '%s\n' after >"$SEED_HISTORY/app/after"
printf '%s\n' 'State: Review' 'SPEC-LINT: PASS' \
  'reviewer round 1: REQUEST CHANGES' \
  >"$SEED_HISTORY/factory/tickets/T-1.md"
git -C "$SEED_HISTORY" add app/after factory/tickets/T-1.md
git -C "$SEED_HISTORY" -c user.name='Software Factory' \
  -c user.email=factory@local commit -qm 'T-1: retain later lifecycle output'
git -C "$SEED_HISTORY" branch ticket/T-1
git -C "$SEED_HISTORY" bundle create "$TMP/seed-history.bundle" ticket/T-1
chmod 600 "$TMP/seed-history.bundle"
git clone -q "$ROOT" "$SEED_HISTORY_ROOT/kit"
git clone -q "$SEED_HISTORY" "$SEED_HISTORY_ROOT/product"
git -C "$SEED_HISTORY_ROOT/product" checkout -q --detach "$SEED_HISTORY_BASE"
git init -q --bare "$SEED_HISTORY_ROOT/origin.git"
git -C "$SEED_HISTORY_ROOT/product" remote set-url origin \
  "$SEED_HISTORY_ROOT/origin.git"
printf '%s\n' kit >"$SEED_HISTORY_ROOT/product/factory/KIT_PIN"
printf 'WORKTREES_DIR="%s"\n' "$SEED_HISTORY_ROOT/worktrees" \
  >"$SEED_HISTORY_ROOT/product/factory/PROJECT.env"
git -C "$SEED_HISTORY_ROOT/product" add factory
git -C "$SEED_HISTORY_ROOT/product" -c user.name='Factory Dev Lane' \
  -c user.email=factory-dev@local commit -qm \
  'Configure isolated Contract 1.7 product lane'
git -C "$SEED_HISTORY_ROOT/product" worktree add -q -b ticket/T-1 \
  "$SEED_HISTORY_ROOT/worktrees/T-1" HEAD
git -C "$SEED_HISTORY_ROOT/worktrees/T-1" push -q -u origin ticket/T-1
eval "$(sed -n \
  '/^seed_product_worktrees()/,/^write_product_checkpoint_import()/p' \
  "$LANE" | sed '$d')"
PRODUCT_SEED_CHECKPOINT=""
require_lane_path() { :; }
die() { exit 1; }
seed_product_worktrees "$SEED_HISTORY_ROOT" "$TMP/seed-history.bundle" \
  "$SEED_HISTORY_BASE" T-1
[[ -f "$SEED_HISTORY_ROOT/worktrees/T-1/app/before" &&
   -f "$SEED_HISTORY_ROOT/worktrees/T-1/app/after" ]] ||
  fail "late route pin caused retained lifecycle output to be skipped"
[[ ! -e "$SEED_HISTORY_ROOT/worktrees/T-1/factory/route-plans/T-1.json" ]] ||
  fail "stale retained route plan was replayed"
grep -qx 'State: Ready' \
  "$SEED_HISTORY_ROOT/worktrees/T-1/factory/tickets/T-1.md" ||
  fail "retained ticket was not reset to its evidence-backed start"
grep -qx "Kit-SHA: $(git -C "$SEED_HISTORY_ROOT/kit" rev-parse HEAD)" \
  "$SEED_HISTORY_ROOT/worktrees/T-1/factory/tickets/T-1.md" ||
  fail "retained ticket did not receive the current development kit"
if grep -Eq '^(SPEC-LINT:|reviewer round )' \
  "$SEED_HISTORY_ROOT/worktrees/T-1/factory/tickets/T-1.md"; then
  fail "retained ticket kept stale role evidence"
fi
grep -Fxq "WORKTREES_DIR=\"$SEED_HISTORY_ROOT/worktrees\"" \
  "$SEED_HISTORY_ROOT/worktrees/T-1/factory/PROJECT.env" ||
  fail "retained seed replaced the new lane configuration"
git -C "$SEED_HISTORY" checkout -q ticket/T-1
printf '%s\n' \
  "source '/private/tmp/nysa-sf-dev.untrusted/runtime/product-db/T-1.env'" \
  >"$SEED_HISTORY/app/untrusted-path"
git -C "$SEED_HISTORY" add app/untrusted-path
git -C "$SEED_HISTORY" -c user.name=Provider -c user.email=provider@local \
  commit -qm 'Add untrusted stale lane path'
git -C "$SEED_HISTORY" bundle create "$TMP/seed-history-untrusted.bundle" \
  ticket/T-1
chmod 600 "$TMP/seed-history-untrusted.bundle"
if ( seed_product_worktrees "$SEED_HISTORY_ROOT" \
    "$TMP/seed-history-untrusted.bundle" "$SEED_HISTORY_BASE" T-1 \
    >"$OUT" 2>&1 ); then
  fail "untrusted retained lane path unexpectedly succeeded"
fi
grep -Fq 'lane-local absolute path detected in role output' "$OUT" ||
  fail "untrusted retained lane path did not fail at the seed sentinel"
die() { return 1; }

RESUME_ROOT="$TMP/resume-drained"
mkdir -p "$RESUME_ROOT/kit/scripts" "$RESUME_ROOT/runtime/product-envelope" \
  "$RESUME_ROOT/product/factory/.dispatch-leases" \
  "$RESUME_ROOT/product/factory/.active-runs"
cat >"$RESUME_ROOT/kit/scripts/provider-coordinator.py" <<'PY'
#!/usr/bin/env python3
print('{"active_reserve_micro_usd":0,"attempts":[{"state":"terminal"}],"counts":{"terminal":1}}')
PY
chmod 700 "$RESUME_ROOT/kit/scripts/provider-coordinator.py"
printf '%s\n' \
  approval_hash=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa \
  used=0 \
  >"$RESUME_ROOT/runtime/product-approval.used"
chmod 600 "$RESUME_ROOT/runtime/product-approval.used"
printf '%s\n' '{"containers":[]}' \
  >"$RESUME_ROOT/runtime/product-containers.json"
date -u +%F >"$RESUME_ROOT/runtime/product-envelope/budget-day"
eval "$(sed -n '/^product_resume_drained()/,/^}/p' "$LANE")"
subscription_provider_idle() { :; }
product_resume_drained "$RESUME_ROOT" ||
  fail "fully drained retained lane was rejected"
printf 'pid=%s\n' "$$" >"$RESUME_ROOT/runtime/live.pid"
if product_resume_drained "$RESUME_ROOT"; then
  fail "retained lane with a live process was resumable"
fi
rm "$RESUME_ROOT/runtime/live.pid"
cp "$RESUME_ROOT/runtime/product-approval.used" \
  "$RESUME_ROOT/runtime/product-approval"
if product_resume_drained "$RESUME_ROOT"; then
  fail "retained lane with a fresh approval was rearmed twice"
fi

eval "$(sed -n '/^load_product_tickets()/,/^}/p' "$LANE")"
SOURCE_ROOT_TEST="$TMP/source-binding"
mkdir -p "$SOURCE_ROOT_TEST/runtime"
printf '%s\n' '{"schema":"factory-dev-product-source/v1","tickets":["T-1","T-2","T-3"]}' \
  >"$SOURCE_ROOT_TEST/runtime/product-source.json"
load_product_tickets "$SOURCE_ROOT_TEST"
[[ "${PRODUCT_TICKETS[*]}" == 'T-1 T-2 T-3' ]] ||
  fail "partial product source binding was rejected"
for invalid_source in \
  '{"schema":"factory-dev-product-source/v1","tickets":[]}' \
  '{"schema":"factory-dev-product-source/v1","tickets":["T-1","T-1"]}' \
  '{"schema":"factory-dev-product-source/v1","tickets":["T-1","bad"]}' \
  '{"schema":"factory-dev-product-source/v1","tickets":["T-1","T-2","T-3","T-4","T-5"]}'; do
  printf '%s\n' "$invalid_source" >"$SOURCE_ROOT_TEST/runtime/product-source.json"
  if load_product_tickets "$SOURCE_ROOT_TEST"; then
    fail "malformed partial product source binding was accepted: $invalid_source"
  fi
done

eval "$(sed -n '/^run_product_internal()/,/^}/p' "$LANE")"
CLAIM_ROOT="$TMP/claim-rollback"
mkdir -p "$CLAIM_ROOT/runtime"
printf '%s\n' 'approval_hash=test-approval' 'used=0' \
  >"$CLAIM_ROOT/runtime/product-approval"
require_lane_mode() { :; }
load_product_tickets() { PRODUCT_TICKETS=(T-1 T-2 T-3); }
validate_runtime_paths() { :; }
product_approval_hash() { printf '%s\n' test-approval; }
subscription_ready() { :; }
subscription_provider_idle() { :; }
subscription_env() {
  local ignored="$1" command action ticket
  shift
  command="$1"; action="$2"; ticket="$4"
  printf '%s %s\n' "$action" "$ticket" >>"$CLAIM_ROOT/lease-actions"
  [[ "$action" != claim || "$ticket" != T-2 ]] || return 1
  [[ "$action" != claim ]] || printf '{"lease_id":"lease-%s"}\n' "$ticket"
}
die() { exit 1; }
if (run_product_internal "$CLAIM_ROOT" test-approval); then
  fail "partial lease claim failure unexpectedly succeeded"
fi
[[ "$(cat "$CLAIM_ROOT/lease-actions")" == $'claim T-1\nclaim T-2\nrelease T-1' ]] ||
  fail "partial lease claim failure did not release only prior leases"

PARTIAL_ROOT="$TMP/partial-run"
mkdir -p "$PARTIAL_ROOT/runtime" "$PARTIAL_ROOT/worktrees"
printf '%s\n' 'approval_hash=test-approval' 'used=0' \
  >"$PARTIAL_ROOT/runtime/product-approval"
for ticket in T-1 T-2 T-3; do
  mkdir -p "$PARTIAL_ROOT/worktrees/$ticket"
  git -C "$PARTIAL_ROOT/worktrees/$ticket" init -q
done
subscription_env() {
  local ignored="$1" command action ticket
  shift
  command="$1"; action="$2"; ticket="$4"
  printf '%s %s\n' "$action" "$ticket" >>"$PARTIAL_ROOT/lease-actions"
  [[ "$action" != claim ]] || printf '{"lease_id":"lease-%s"}\n' "$ticket"
}
product_reconcile_reviewer() { :; }
product_prepare_role_state() { :; }
next_stage() { printf '%s\n' AWAIT-OPERATOR; }
product_write_timing_report() { :; }
die() { exit 1; }
partial_output="$(run_product_internal "$PARTIAL_ROOT" test-approval)"
grep -qx 'STATUS=AWAIT-OPERATOR' <<<"$partial_output" ||
  fail "partial product lifecycle did not reach operator approval"
[[ "$(cat "$PARTIAL_ROOT/lease-actions")" == \
   $'claim T-1\nclaim T-2\nclaim T-3\nrenew T-1\nrelease T-1\nrenew T-2\nrelease T-2\nrenew T-3\nrelease T-3' ]] ||
  fail "partial product lifecycle did not claim, renew, and release exactly its tickets"

die() { return 1; }
printf '%s\n' \
  "{\"schema\":\"factory-dev-product-seed-accounting/v2\",\"seed_bundle_sha256\":\"$seed_bundle_sha\",\"base_sha\":\"$SEED_BASE\",\"reserved_micro_usd\":{\"T-1\":100000000}}" \
  >"$SEED_ACCOUNTING"
if validate_product_seed_accounting "$SEED_ACCOUNTING" "$SEED_BUNDLE" "$SEED_BASE" T-1; then
  fail "exhausted cumulative seed accounting was accepted"
fi
grep -Fq 'FACTORY_ENVELOPE="$envelope"' "$LANE" ||
  fail "seeded remaining-budget envelope is not passed to role execution"

review_pattern='^[[:space:]#*]*(((Review[[:space:]]+)?Verdict:[[:space:]*]*)?APPROVE|Review[[:space:]]+verdict:[[:space:]]+T-[0-9]+[[:space:]]+—[[:space:]]+APPROVE)[*[:space:]]*$'
printf '%s\n' '## Verdict: Approve' | grep -Eiq "$review_pattern" ||
  fail "review verdict parser rejected a canonical approval"
printf '%s\n' '**Verdict: Approve**' | grep -Eiq "$review_pattern" ||
  fail "review verdict parser rejected a bold canonical approval"
printf '%s\n' '## Review verdict: **Approve**' | grep -Eiq "$review_pattern" ||
  fail "review verdict parser rejected a headed bold approval"
printf '%s\n' '## Review verdict: T-900001 — Approve' | grep -Eiq "$review_pattern" ||
  fail "review verdict parser rejected a ticket-qualified approval"
if printf '%s\n' 'Do not approve' | grep -Eiq "$review_pattern"; then
  fail "review verdict parser accepted approval prose"
fi
if printf '%s\n' 'I approve' | grep -Eiq "$review_pattern"; then
  fail "review verdict parser accepted first-person approval prose"
fi
if printf '%s\n' 'Review verdict: I cannot approve' | grep -Eiq "$review_pattern"; then
  fail "review verdict parser accepted a negative verdict"
fi
printf '%s\n' \
  'warning outside stream' \
  '{"type":"result","subtype":"success","result":"Reviewed safely.\n\nAPPROVE"}' |
  python3 "$ROOT/scripts/lib/cursor-result.py" |
  grep -Eiq "$review_pattern" ||
  fail "review verdict parser did not decode the terminal Cursor result"

# Production execution is macOS-only. On other systems no test override may
# accidentally turn a real invocation into a development lane.
if [[ "$(uname -s)" != "Darwin" ]]; then
  expect_failure "non-Darwin invocation" env -u FACTORY_DEV_LANE_TEST_MODE \
    -u FACTORY_TRUSTED_TEST_HARNESS bash "$LANE" mock
  grep -Eqi 'macOS|Darwin' "$OUT" || fail "non-Darwin invocation did not fail on platform"
fi

started=$SECONDS
test_env bash "$LANE" mock --keep >"$OUT"
elapsed=$((SECONDS - started))
[[ "$(cksum "$CALLER_HOME/.factory/sentinel" \
  "$CALLER_HOME/.hermes/profiles/factory/sentinel" \
  "$CALLER_HOME/Library/LaunchAgents/sentinel" \
  "$CALLER_HOME/Projects/nysa-company/nysa-app/sentinel")" == "$sentinels_before" ]] ||
  fail "mock changed caller production sentinels"
[[ "$(find "$CALLER_HOME/.factory" "$CALLER_HOME/.hermes/profiles/factory" \
  "$CALLER_HOME/Library/LaunchAgents" "$CALLER_HOME/Projects/nysa-company/nysa-app" \
  -type f | wc -l | tr -d ' ')" -eq 4 ]] ||
  fail "mock added caller production state"
lane_root="$(sed -n 's/^ROOT=//p' "$OUT")"
[[ "$lane_root" == "$TMP/lanes"/nysa-sf-dev.* ]] || fail "mock returned an unsafe root"
[[ "$(grep -c '^ROOT=' "$OUT")" -eq 1 ]] || fail "mock did not return one root"
grep -qx 'STATUS=AWAIT-OPERATOR' "$OUT" || fail "mock did not stop at operator approval"
grep -qx 'ROLES=planner,spec-linter,test-author,builder,reviewer,narrator' "$OUT" ||
  fail "mock role order changed"
reported_elapsed="$(sed -n 's/^ELAPSED_SECONDS=//p' "$OUT")"
[[ "$reported_elapsed" =~ ^[0-9]+$ && "$reported_elapsed" -lt 900 && "$elapsed" -lt 900 ]] ||
  fail "mock exceeded the 15-minute ceiling"

[[ -f "$lane_root/marker.json" ]] || fail "lane ownership marker is missing"
python3 - "$lane_root" "$TMP/lanes" <<'PY' || fail "lane ownership marker is not bound to its root"
import json, os, stat, sys
root, tmp_parent = sys.argv[1:]
r = os.lstat(root)
m = os.lstat(os.path.join(root, "marker.json"))
v = json.load(open(os.path.join(root, "marker.json"), encoding="utf-8"))
assert set(v) == {"schema", "root", "nonce", "kit_sha", "kit_tree", "mode",
                  "uid", "root_dev", "root_ino", "tmp_parent"}
assert v["root"] == root and v["uid"] == os.getuid()
assert v["root_dev"] == r.st_dev and v["root_ino"] == r.st_ino
assert v["tmp_parent"] == tmp_parent == os.path.dirname(root)
assert stat.S_ISDIR(r.st_mode) and stat.S_IMODE(r.st_mode) == 0o700
assert stat.S_ISREG(m.st_mode) and stat.S_IMODE(m.st_mode) == 0o600
assert m.st_uid == r.st_uid == os.getuid() and m.st_dev == r.st_dev and m.st_nlink == 1
assert m.st_ino != r.st_ino
PY
[[ -d "$lane_root/kit/.git" || -f "$lane_root/kit/.git" ]] || fail "lane-local kit is missing"
if find "$lane_root" -type f \( -name active.json -o -path '*/receipts/*.json' \) -print -quit |
   grep -q .; then
  fail "development lane created production activation evidence"
fi
[[ -d "$lane_root/origin.git" ]] || fail "local-only origin is missing"
[[ -d "$lane_root/worktrees/T-900001" ]] || fail "synthetic ticket worktree is missing"
grep -Fq 'ports `4781` and `4782`' \
  "$lane_root/worktrees/T-900001/factory/tickets/T-900001.md" ||
  fail "synthetic ticket does not reserve collision-free fixture ports"
grep -Fq 'JSON member order is not contractual' \
  "$lane_root/worktrees/T-900001/docs/engine-spec.md" ||
  fail "synthetic contract leaves JSON member order ambiguous"
grep -Fq 'duplicate webhook delivery' \
  "$lane_root/worktrees/T-900001/docs/acceptance/health-version.md" ||
  fail "synthetic contract leaves duplicate delivery in scope"
grep -qx 'State: Review' "$lane_root/worktrees/T-900001/factory/tickets/T-900001.md" ||
  fail "synthetic ticket did not remain in Review"
[[ -z "$(git -C "$lane_root/product" status --porcelain)" ]] || fail "synthetic product is dirty"
[[ -z "$(git -C "$lane_root/worktrees/T-900001" status --porcelain)" ]] ||
  fail "synthetic ticket worktree is dirty"
[[ "$(git -C "$lane_root/worktrees/T-900001" rev-parse HEAD)" == \
   "$(git -C "$lane_root/origin.git" rev-parse refs/heads/ticket/T-900001)" ]] ||
  fail "synthetic ticket branch was not pushed locally"

manifest_list="$TMP/manifests"
find "$lane_root/product/factory/runs" -type f -name '*.meta' -print | sort >"$manifest_list"
[[ "$(wc -l <"$manifest_list" | tr -d ' ')" -eq 6 ]] ||
  fail "mock did not retain exactly six role manifests"
python3 - "$manifest_list" <<'PY' || fail "role manifests are not successful and ordered"
import sys
expected = ["planner", "spec-linter", "test-author", "builder", "reviewer", "narrator"]
actual = []
with open(sys.argv[1], encoding="utf-8") as paths:
    for raw_path in paths:
        values = dict(line.rstrip("\n").split("=", 1) for line in open(raw_path.rstrip("\n"), encoding="utf-8") if "=" in line)
        assert values["accounting_state"] == "completed", raw_path
        assert values["exit_status"] == "0", raw_path
        actual.append(values["role"])
assert sorted(actual) == sorted(expected), actual
PY
python3 - "$lane_root/product/factory/runtime-ledger.csv" <<'PY' || fail "runtime ledger role order changed"
import csv, sys
expected = ["planner", "spec-linter", "test-author", "builder", "reviewer", "narrator"]
with open(sys.argv[1], newline="", encoding="utf-8") as handle:
    rows = list(csv.DictReader(handle))
assert [row["role"] for row in rows] == expected, rows
assert all(row["exit_status"] == "0" for row in rows), rows
PY

profile="$lane_root/runtime/mock.sb"
[[ -f "$profile" ]] || fail "Seatbelt profile was not retained"
grep -Eq '\(deny +default\)' "$profile" || fail "mock profile is not default-deny"
grep -Eq '\(deny +network' "$profile" || fail "mock profile does not deny network"
grep -Fq "$lane_root" "$profile" || fail "mock profile does not bind filesystem access to its lane"
for forbidden in "$CALLER_HOME/.factory" "$CALLER_HOME/.hermes/profiles/factory" \
  "$CALLER_HOME/Library/LaunchAgents" "$CALLER_HOME/Projects/nysa-company/nysa-app"; do
  grep -Fq "$forbidden" "$profile" && fail "mock profile names production path: $forbidden"
done

chmod 755 "$lane_root"
expect_failure "root mode drift cleanup" clean_cmd "$lane_root"
chmod 700 "$lane_root"
chmod 644 "$lane_root/marker.json"
expect_failure "marker mode drift cleanup" clean_cmd "$lane_root"
chmod 600 "$lane_root/marker.json"

ln "$lane_root/marker.json" "$TMP/marker.link"
expect_failure "marker link-count drift cleanup" clean_cmd "$lane_root"
rm "$TMP/marker.link"

root_saved="$TMP/root.saved"
mv "$lane_root" "$root_saved"
mkdir -m 700 "$lane_root"
mv "$root_saved/marker.json" "$lane_root/marker.json"
expect_failure "root inode drift cleanup" clean_cmd "$lane_root"
mv "$lane_root/marker.json" "$root_saved/marker.json"
rmdir "$lane_root"
mv "$root_saved" "$lane_root"

mkdir "$TMP/other-parent"
expect_failure "TMP parent drift cleanup" env TMPDIR="$TMP/other-parent" \
  bash "$LANE" clean --root "$lane_root"

printf 'pid=%s\n' "$$" >"$lane_root/runtime/live-cleanup-test.pid"
expect_failure "live process cleanup" clean_cmd "$lane_root"
rm "$lane_root/runtime/live-cleanup-test.pid"

clean_cmd "$lane_root"
[[ ! -e "$lane_root" ]] || fail "clean retained the lane"
expect_failure "repeat cleanup" clean_cmd "$lane_root"
expect_failure "relative cleanup" clean_cmd relative
unmarked="$TMP/lanes/nysa-sf-dev.unmarked"
forged="$TMP/lanes/nysa-sf-dev.forged"
lane_link="$TMP/lanes/nysa-sf-dev.link"
mkdir -p "$unmarked"
expect_failure "unmarked cleanup" clean_cmd "$unmarked"
ln -s "$unmarked" "$lane_link"
expect_failure "symlink cleanup" clean_cmd "$lane_link"
mkdir -p "$forged"
printf '{}\n' >"$forged/marker.json"
expect_failure "forged cleanup" clean_cmd "$forged"
[[ -d "$unmarked" && -d "$forged" ]] || fail "refused cleanup removed data"

started=$SECONDS
test_env bash "$LANE" mock-concurrency --keep >"$OUT"
elapsed=$((SECONDS - started))
concurrency_root="$(sed -n 's/^ROOT=//p' "$OUT")"
[[ "$concurrency_root" == "$TMP/lanes"/nysa-sf-dev.* ]] ||
  fail "concurrency mock returned an unsafe root"
grep -qx 'PROVIDER_CALLS=4' "$OUT" || fail "concurrency mock did not run four providers"
grep -qx 'PROVIDER_MODE=cli-concurrent-v1' "$OUT" ||
  fail "concurrency mock did not use the subscription CLI coordinator path"
overlap_ms="$(sed -n 's/^PROVIDER_OVERLAP_MILLISECONDS=//p' "$OUT")"
[[ "$overlap_ms" =~ ^[0-9]+$ && "$overlap_ms" -ge 2000 && "$overlap_ms" -lt 5000 ]] ||
  fail "four provider calls did not overlap for one bounded interval"
reported_elapsed="$(sed -n 's/^ELAPSED_SECONDS=//p' "$OUT")"
[[ "$reported_elapsed" =~ ^[0-9]+$ && "$reported_elapsed" -lt 900 && "$elapsed" -lt 900 ]] ||
  fail "four lifecycle batch exceeded the 15-minute ceiling"
[[ "$(cksum "$CALLER_HOME/.factory/sentinel" \
  "$CALLER_HOME/.hermes/profiles/factory/sentinel" \
  "$CALLER_HOME/Library/LaunchAgents/sentinel" \
  "$CALLER_HOME/Projects/nysa-company/nysa-app/sentinel")" == "$sentinels_before" ]] ||
  fail "concurrency mock changed production sentinels"
python3 "$concurrency_root/kit/scripts/provider-coordinator.py" \
  --db "$concurrency_root/runtime/provider-state.sqlite3" status | python3 -c '
import json, sys
value=json.load(sys.stdin)
assert value["counts"] == {"terminal":28}, value
assert value["active_reserve_micro_usd"] == 0, value
' || fail "concurrency mock retained provider capacity or reservations"
[[ "$(find "$concurrency_root/product/factory/runs" -type f -name '*.meta' | wc -l | tr -d ' ')" -eq 24 ]] ||
  fail "four lifecycle batch did not record 24 role runs"
if find "$concurrency_root/product/factory/runs" -type f -name '*.meta' -exec \
     grep -L '^provider_execution_mode=cli-concurrent-v1$' {} + | grep -q .; then
  fail "synthetic lifecycles did not use CLI concurrent admission"
fi
for ticket in T-900001 T-900002 T-900003 T-900004; do
  work="$concurrency_root/worktrees/$ticket"
  grep -qx 'State: Review' "$work/factory/tickets/$ticket.md" ||
    fail "$ticket did not complete in Review"
  [[ "$(git -C "$work" rev-parse HEAD)" == \
     "$(git -C "$concurrency_root/origin.git" rev-parse "refs/heads/ticket/$ticket")" ]] ||
    fail "$ticket trusted host output was not pushed locally"
done
if find "$concurrency_root/product/factory" -type f -name '*.pid' -print -quit | grep -q . ||
   find "$concurrency_root" -type f \( -name active.json -o -path '*/receipts/*.json' \) -print -quit | grep -q .; then
  fail "concurrency mock retained a live process or activation artifact"
fi
clean_cmd "$concurrency_root"
[[ ! -e "$concurrency_root" ]] || fail "concurrency cleanup retained its lane"

# Real Cursor cannot authenticate inside a nested Seatbelt profile. Its lane
# uses Cursor's own explicit sandbox; only mock mode invokes sandbox-exec.
mv "$FAKE_SANDBOX" "$FAKE_SANDBOX.disabled"
cursor_env bash "$LANE" cursor-plan >"$OUT"
[[ "$(cksum "$CALLER_HOME/.cursor/auth.json" "$CALLER_HOME/.cursor/cli-config.json")" == \
   "$cursor_session_before" ]] || fail "Cursor planning changed the normal session files"
[[ ! -e "$TMP/cursor-tmp-bridge" && ! -L "$TMP/cursor-tmp-bridge" ]] ||
  fail "Cursor temporary bridge remained after planning"
cursor_root="$(sed -n 's/^ROOT=//p' "$OUT")"
approval_hash="$(sed -n 's/^APPROVE_HASH=//p' "$OUT")"
[[ "$cursor_root" == "$TMP/lanes"/nysa-sf-dev.* ]] || fail "cursor plan returned an unsafe root"
[[ "$approval_hash" =~ ^[0-9a-f]{64}$ ]] || fail "cursor plan returned an invalid approval hash"
grep -Fq "$TMP/cursor-tmp-bridge" "$cursor_root/runtime/cursor.sb" ||
  fail "Cursor profile does not bind its ephemeral temporary bridge"
grep -Fq "$CALLER_HOME/.cursor/auth.json" "$cursor_root/runtime/cursor.sb" ||
  fail "Cursor profile does not bind the exact session file"
grep -Fq "$CALLER_HOME/Library/Keychains" "$cursor_root/runtime/cursor.sb" ||
  fail "Cursor profile does not bind the login Keychain database"
grep -Fq 'Shell(security)' "$cursor_root/worktrees/T-900001/.cursor/cli.json" ||
  fail "synthetic product does not deny agent-initiated Keychain commands"
grep -Fq '"allow":[]' "$cursor_root/worktrees/T-900001/.cursor/cli.json" ||
  fail "synthetic Cursor permissions omit the required allow list"
grep -Fq 'com.apple.securityd' "$cursor_root/runtime/cursor.sb" &&
  fail "Cursor profile blocks the authenticated CLI session"
bad_hash="${approval_hash%?}0"
[[ "$bad_hash" != "$approval_hash" ]] || bad_hash="${approval_hash%?}1"
expect_failure "wrong cursor approval hash" cursor_env bash "$LANE" cursor-run \
  --root "$cursor_root" --approve-hash "$bad_hash"
cp "$FAKE_CURSOR" "$TMP/cursor-original"
printf '\n# changed after approval\n' >>"$FAKE_CURSOR"
expect_failure "Cursor binary byte drift" cursor_env bash "$LANE" cursor-run \
  --root "$cursor_root" --approve-hash "$approval_hash"
[[ -f "$cursor_root/runtime/cursor-approval" ]] ||
  fail "Cursor binary drift consumed the approval"
cp "$TMP/cursor-original" "$FAKE_CURSOR"
chmod +x "$FAKE_CURSOR"
# The fake provider fails after authorization. The approval must still be
# consumed so a post-submission failure cannot be replayed.
expect_failure "fake cursor execution" cursor_env bash "$LANE" cursor-run \
  --root "$cursor_root" --approve-hash "$approval_hash"
grep -qx 'State: Ready' "$cursor_root/worktrees/T-900001/factory/tickets/T-900001.md" ||
  fail "failed provider output advanced ticket state"
[[ "$(git -C "$cursor_root/worktrees/T-900001" rev-parse HEAD)" == \
   "$(git -C "$cursor_root/origin.git" rev-parse refs/heads/ticket/T-900001)" ]] ||
  fail "failed provider output changed the trusted remote"
grep -qx -- '--sandbox' "$cursor_root/home/cursor-args" ||
  fail "real Cursor lane did not enable Cursor's internal sandbox"
grep -qx -- 'enabled' "$cursor_root/home/cursor-args" ||
  fail "real Cursor lane did not select the enabled sandbox mode"
[[ ! -e "$TMP/cursor-tmp-bridge" && ! -L "$TMP/cursor-tmp-bridge" ]] ||
  fail "Cursor temporary bridge remained after failed execution"
expect_failure "reused cursor approval hash" cursor_env bash "$LANE" cursor-run \
  --root "$cursor_root" --approve-hash "$approval_hash"
clean_cmd "$cursor_root"

printf 'PASS: isolated factory development lane\n'
