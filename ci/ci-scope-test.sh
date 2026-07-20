#!/usr/bin/env bash
set -euo pipefail

# CI_FORCE_FULL controls the outer suite only. Fixture cases set it explicitly
# when they are testing forced selection.
unset CI_FORCE_FULL

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

WORKFLOW="$ROOT/.github/workflows/ci.yml"
[[ "$(grep -c -- '--changed "\$BASE_SHA" "\$GITHUB_SHA"' "$WORKFLOW")" -eq 2 ]] || {
  echo "FAIL: Linux and macOS PR jobs must use changed-file selection" >&2
  exit 1
}
[[ "$(grep -c 'CI_FORCE_FULL: "1"' "$WORKFLOW")" -eq 2 ]] || {
  echo "FAIL: both protected-main platform jobs must force the full suite" >&2
  exit 1
}
[[ "$(grep -c '^    needs: scope$' "$WORKFLOW")" -eq 2 ]] || {
  echo "FAIL: platform jobs must depend only on classification so they can run in parallel" >&2
  exit 1
}
LIGHTWEIGHT="$ROOT/ci/lightweight-change.sh"
MACOS="$ROOT/ci/macos-required-change.sh"
SELECTOR="$ROOT/ci/changed-test-suites.sh"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/ci-scope.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT

new_repo() {
  local repo="$1"
  git init -q -b main "$repo"
  git -C "$repo" config user.name "CI scope test"
  git -C "$repo" config user.email "ci-scope@example.invalid"
}

commit_all() {
  local repo="$1" message="$2"
  git -C "$repo" add -A
  git -C "$repo" commit -qm "$message"
  git -C "$repo" rev-parse HEAD
}

expect_status() {
  local expected="$1" check="$2" repo="$3" base="$4" head="$5" label="$6"
  local status=0
  (cd "$repo" && bash "$check" "$base" "$head") || status=$?
  if [[ "$status" -ne "$expected" ]]; then
    printf 'FAIL: %s (expected %s, got %s)\n' "$label" "$expected" "$status" >&2
    exit 1
  fi
}

expect_selection() {
  local expected="$1" repo="$2" base="$3" head="$4" label="$5" output
  output="$(cd "$repo" && bash ci/changed-test-suites.sh "$base" "$head")"
  if [[ "$output" != "$expected" ]]; then
    printf 'FAIL: %s (expected %s, got %s)\n' "$label" "$expected" "$output" >&2
    exit 1
  fi
}

REPO="$TMP/main"
new_repo "$REPO"
mkdir -p "$REPO/docs" "$REPO/context" "$REPO/.github/workflows" \
  "$REPO/integrations/hermes" "$REPO/conformance/app" "$REPO/roles" "$REPO/scripts"
printf 'initial\n' > "$REPO/docs/guide.md"
printf 'initial\n' > "$REPO/README.md"
printf 'initial\n' > "$REPO/context/memory.md"
printf 'initial\n' > "$REPO/AGENTS.md"
printf '@AGENTS.md\n' > "$REPO/CLAUDE.md"
printf 'initial\n' > "$REPO/.github/pull_request_template.md"
printf 'initial\n' > "$REPO/integrations/hermes/CHANGELOG.md"
printf 'initial\n' > "$REPO/conformance/SHAKEDOWN-REPORT.md"
printf 'initial\n' > "$REPO/conformance/app/app.js"
printf 'initial\n' > "$REPO/roles/builder.md"
printf 'initial\n' > "$REPO/scripts/tool.py"
printf 'initial\n' > "$REPO/.github/workflows/ci.yml"
BASE="$(commit_all "$REPO" "initial")"

for path in docs/guide.md README.md context/memory.md AGENTS.md CLAUDE.md \
  .github/pull_request_template.md integrations/hermes/CHANGELOG.md \
  conformance/SHAKEDOWN-REPORT.md; do
  printf 'metadata update\n' >> "$REPO/$path"
done
LIGHT="$(commit_all "$REPO" "lightweight metadata")"
expect_status 0 "$LIGHTWEIGHT" "$REPO" "$BASE" "$LIGHT" "explicit lightweight allowlist"
expect_status 1 "$MACOS" "$REPO" "$BASE" "$LIGHT" "metadata does not need macOS"
expect_status 1 "$LIGHTWEIGHT" "$REPO" "$LIGHT" "$LIGHT" "empty diff runs Linux"
expect_status 0 "$MACOS" "$REPO" "$LIGHT" "$LIGHT" "empty diff runs macOS"
expect_status 1 "$LIGHTWEIGHT" "$REPO" missing "$LIGHT" "invalid base runs Linux"
expect_status 0 "$MACOS" "$REPO" missing "$LIGHT" "invalid base runs macOS"

printf 'prompt update\n' >> "$REPO/roles/builder.md"
LINUX="$(commit_all "$REPO" "runtime prompt")"
expect_status 1 "$LIGHTWEIGHT" "$REPO" "$LIGHT" "$LINUX" "runtime prompt runs Linux"
expect_status 1 "$MACOS" "$REPO" "$LIGHT" "$LINUX" "runtime prompt skips PR macOS"

printf 'app update\n' >> "$REPO/conformance/app/app.js"
APP="$(commit_all "$REPO" "app")"
expect_status 1 "$LIGHTWEIGHT" "$REPO" "$LINUX" "$APP" "application code runs Linux"
expect_status 1 "$MACOS" "$REPO" "$LINUX" "$APP" "application code skips PR macOS"

printf 'script update\n' >> "$REPO/scripts/tool.py"
SCRIPT="$(commit_all "$REPO" "script")"
expect_status 0 "$MACOS" "$REPO" "$APP" "$SCRIPT" "shared scripts run macOS"

printf 'workflow update\n' >> "$REPO/.github/workflows/ci.yml"
WORKFLOW="$(commit_all "$REPO" "workflow")"
expect_status 0 "$MACOS" "$REPO" "$SCRIPT" "$WORKFLOW" "workflows run macOS"

RENAME_REPO="$TMP/rename"
new_repo "$RENAME_REPO"
mkdir -p "$RENAME_REPO/docs"
printf 'executable\n' > "$RENAME_REPO/tool.sh"
RENAME_BASE="$(commit_all "$RENAME_REPO" "initial")"
git -C "$RENAME_REPO" mv tool.sh docs/tool.md
RENAME_HEAD="$(commit_all "$RENAME_REPO" "move code into docs")"
expect_status 1 "$LIGHTWEIGHT" "$RENAME_REPO" "$RENAME_BASE" "$RENAME_HEAD" \
  "code renamed into docs runs Linux"
expect_status 0 "$MACOS" "$RENAME_REPO" "$RENAME_BASE" "$RENAME_HEAD" \
  "shell renamed into docs runs macOS"

SELECT_REPO="$TMP/selection"
new_repo "$SELECT_REPO"
mkdir -p "$SELECT_REPO/ci" "$SELECT_REPO/docs" "$SELECT_REPO/scripts/lib" \
  "$SELECT_REPO/scripts/adapters" "$SELECT_REPO/integrations/operator-console" \
  "$SELECT_REPO/conformance/app/tests" "$SELECT_REPO/envelope" "$SELECT_REPO/roles"
cp "$SELECTOR" "$LIGHTWEIGHT" "$SELECT_REPO/ci/"
for path in \
  scripts/linear-sync.py scripts/operator-console.py scripts/operator-snapshot.py \
  scripts/adapters/claude-kimi.sh scripts/lib/claude-kimi-output.py \
  scripts/lib/claude-kimi-secret.py scripts/lib/failed_attempt_handoff.py \
  scripts/reorder-test-fixes.sh scripts/lib/reorder_test_fixes.py \
  conformance/app/server.js conformance/app/tests/server.test.js \
  scripts/lib/effective_ticket.py scripts/ledger-view.py scripts/attempt-cancel.py \
  scripts/operator-state.py integrations/operator-console/app.js scripts/model-router.py \
  scripts/envelope-control.py scripts/dispatch-lease.sh scripts/ticket-state.sh \
  scripts/legacy-closeout.py conformance/app/package.json conformance/app/app.js \
  scripts/run-agent.sh roles/builder.md ci/test-all.sh; do
  mkdir -p "$SELECT_REPO/$(dirname "$path")"
  printf 'initial\n' > "$SELECT_REPO/$path"
done
printf 'initial\n' > "$SELECT_REPO/docs/guide.md"
SELECT_BASE="$(commit_all "$SELECT_REPO" "selection base")"

selection_case() {
  local path="$1" expected="$2" label="$3" base head
  base="$(git -C "$SELECT_REPO" rev-parse HEAD)"
  printf 'change\n' >> "$SELECT_REPO/$path"
  head="$(commit_all "$SELECT_REPO" "$label")"
  expect_selection "$expected" "$SELECT_REPO" "$base" "$head" "$label"
}

POLICY="ci-scope immutability artifact-policy"
selection_case docs/guide.md "metadata|inert metadata|" "metadata selection"
selection_case scripts/linear-sync.py "targeted|linear|linear $POLICY" "linear selection"
selection_case scripts/operator-console.py "targeted|operator-console|operator-console $POLICY" "operator console selection"
selection_case scripts/operator-snapshot.py "targeted|operator-console|operator-console $POLICY" "operator snapshot selection"
selection_case scripts/adapters/claude-kimi.sh "targeted|claude-kimi|claude-kimi $POLICY" "adapter wrapper selection"
selection_case scripts/lib/claude-kimi-output.py "targeted|claude-kimi|claude-kimi $POLICY" "adapter output selection"
selection_case scripts/lib/claude-kimi-secret.py "targeted|claude-kimi|claude-kimi $POLICY" "adapter secret selection"
selection_case scripts/lib/failed_attempt_handoff.py "targeted|failed-handoff|failed-handoff model-fallback $POLICY" "handoff selection"
selection_case scripts/reorder-test-fixes.sh "targeted|reorder-test-fixes|reorder-test-fixes hermes-contract $POLICY" "reorder wrapper selection"
selection_case scripts/lib/reorder_test_fixes.py "targeted|reorder-test-fixes|reorder-test-fixes hermes-contract $POLICY" "reorder implementation selection"
selection_case conformance/app/server.js "targeted|conformance|conformance $POLICY" "conformance server selection"
selection_case conformance/app/tests/server.test.js "targeted|conformance|conformance $POLICY" "conformance test selection"

for path in scripts/lib/effective_ticket.py scripts/ledger-view.py scripts/attempt-cancel.py \
  scripts/operator-state.py integrations/operator-console/app.js scripts/model-router.py \
  scripts/envelope-control.py scripts/dispatch-lease.sh scripts/ticket-state.sh \
  scripts/legacy-closeout.py conformance/app/package.json conformance/app/app.js \
  scripts/run-agent.sh roles/builder.md ci/test-all.sh; do
  selection_case "$path" "full|unknown or shared path|" "unsafe path $path"
done

MIXED_BASE="$(git -C "$SELECT_REPO" rev-parse HEAD)"
printf 'mixed\n' >> "$SELECT_REPO/scripts/linear-sync.py"
printf 'mixed\n' >> "$SELECT_REPO/scripts/operator-snapshot.py"
MIXED_HEAD="$(commit_all "$SELECT_REPO" "mixed components")"
expect_selection "full|multiple components|" "$SELECT_REPO" "$MIXED_BASE" "$MIXED_HEAD" "mixed selection"
UNKNOWN_BASE="$(git -C "$SELECT_REPO" rev-parse HEAD)"
printf 'new\n' > "$SELECT_REPO/scripts/new-tool.py"
UNKNOWN_HEAD="$(commit_all "$SELECT_REPO" "unknown new path")"
expect_selection "full|added, deleted, renamed, or type-changed path|" \
  "$SELECT_REPO" "$UNKNOWN_BASE" "$UNKNOWN_HEAD" "new path selection"

DELETE_BASE="$(git -C "$SELECT_REPO" rev-parse HEAD)"
git -C "$SELECT_REPO" rm -q scripts/linear-sync.py
DELETE_HEAD="$(commit_all "$SELECT_REPO" "deleted path")"
expect_selection "full|added, deleted, renamed, or type-changed path|" \
  "$SELECT_REPO" "$DELETE_BASE" "$DELETE_HEAD" "deleted path selection"
expect_selection "full|empty diff|" "$SELECT_REPO" "$DELETE_HEAD" "$DELETE_HEAD" "empty diff selection"
expect_selection "full|invalid base|" "$SELECT_REPO" missing "$DELETE_HEAD" "invalid ref selection"

RENAME_BASE="$(git -C "$SELECT_REPO" rev-parse HEAD)"
git -C "$SELECT_REPO" mv scripts/operator-console.py scripts/operator-console-renamed.py
RENAME_HEAD="$(commit_all "$SELECT_REPO" "renamed path")"
expect_selection "full|added, deleted, renamed, or type-changed path|" \
  "$SELECT_REPO" "$RENAME_BASE" "$RENAME_HEAD" "renamed path selection"

FORCED="$(cd "$SELECT_REPO" && CI_FORCE_FULL=1 bash ci/changed-test-suites.sh "$DELETE_BASE" "$DELETE_HEAD")"
if [[ "$FORCED" != "full|CI_FORCE_FULL|" ]]; then
  printf 'FAIL: force-full selection (got %s)\n' "$FORCED" >&2
  exit 1
fi

REGISTRY_IDS=" "
REGISTRY_ERROR=""
registry_check() {
  local id="$1"
  if [[ "$REGISTRY_IDS" == *" $id "* ]]; then
    REGISTRY_ERROR="duplicate suite ID: $id"
  fi
  REGISTRY_IDS="$REGISTRY_IDS$id "
}
. "$ROOT/ci/suite-registry.sh"
suite_registry registry_check
set -- $REGISTRY_IDS
if [[ -n "$REGISTRY_ERROR" || "$#" -eq 0 ]]; then
  printf 'FAIL: canonical suite registry (%s IDs; %s)\n' "$#" "$REGISTRY_ERROR" >&2
  exit 1
fi

RUNNER="$TMP/runner"
mkdir -p "$RUNNER/ci"
cp "$ROOT/ci/test-all.sh" "$RUNNER/ci/"
printf '%s\n' '#!/usr/bin/env bash' 'suite_registry() {' \
  '  local callback="$1"' \
  '  "$callback" pass "pass suite" bash "$ROOT/ci/pass.sh"' \
  '  "$callback" fail "fail suite" bash "$ROOT/ci/fail.sh"' \
  '  "$callback" ci-scope "scope suite" bash "$ROOT/ci/pass.sh"' \
  '  "$callback" immutability "immutability suite" bash "$ROOT/ci/pass.sh"' \
  '  "$callback" artifact-policy "artifact suite" bash "$ROOT/ci/pass.sh"' \
  '}' > "$RUNNER/ci/suite-registry.sh"
printf '%s\n' '#!/usr/bin/env bash' \
  'ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"' \
  'cat "$ROOT/selection"' > "$RUNNER/ci/changed-test-suites.sh"
printf '%s\n' '#!/usr/bin/env bash' 'exit 0' > "$RUNNER/ci/pass.sh"
printf '%s\n' '#!/usr/bin/env bash' '[[ "${FAIL_SECOND:-0}" != "1" ]]' > "$RUNNER/ci/fail.sh"

runner_case() {
  local selection="$1" fail_second="$2" expected_status="$3" fragment="$4" label="$5"
  local output status=0
  printf '%s\n' "$selection" > "$RUNNER/selection"
  output="$(cd "$RUNNER" && FAIL_SECOND="$fail_second" bash ci/test-all.sh --changed base head 2>&1)" || status=$?
  if [[ "$status" -ne "$expected_status" || "$output" != *"$fragment"* ]]; then
    printf 'FAIL: %s (status %s; output %s)\n' "$label" "$status" "$output" >&2
    exit 1
  fi
}

runner_case 'targeted|fixture|pass' 1 0 'component_state=targeted executed=targeted' \
  'active selection runs only selected suites'
runner_case 'targeted|fixture|unknown' 0 0 'selector returned unknown suite: unknown' \
  'unknown suite fails closed to full'
runner_case 'targeted|fixture|pass pass' 0 0 'selector returned duplicate suite: pass' \
  'duplicate suite fails closed to full'
runner_case 'targeted|fixture|' 0 0 'selector returned empty selection' \
  'empty selection fails closed to full'
runner_case 'invalid|fixture|pass' 0 0 'selector returned unknown mode' \
  'unknown mode fails closed to full'
runner_case 'shadow|fixture|pass' 1 1 'SHADOW_MISS: fail was not selected and failed its immediate recheck' \
  'shadow miss is reproducible and fails'

git -C "$RUNNER" init -q -b main
git -C "$RUNNER" config user.name "Runner test"
git -C "$RUNNER" config user.email "runner@example.invalid"
printf 'base\n' > "$RUNNER/code.txt"
git -C "$RUNNER" add .
git -C "$RUNNER" commit -qm base
RUNNER_BASE="$(git -C "$RUNNER" rev-parse HEAD)"
printf 'broad\n' >> "$RUNNER/code.txt"
git -C "$RUNNER" add code.txt
git -C "$RUNNER" commit -qm broad
RUNNER_HEAD="$(git -C "$RUNNER" rev-parse HEAD)"
printf '%s\n' 'full|unknown or shared path|' > "$RUNNER/selection"
status=0
output="$(cd "$RUNNER" && bash ci/test-all.sh --changed-or-defer "$RUNNER_BASE" "$RUNNER_HEAD" 2>&1)" || status=$?
if [[ "$status" -ne 75 || "$output" != *"CI_FULL_DEFERRED:"* ]]; then
  printf 'FAIL: broad local verification defers with status 75 (status %s; output %s)\n' \
    "$status" "$output" >&2
  exit 1
fi
printf '%s\n' 'invalid|fixture|pass' > "$RUNNER/selection"
status=0
output="$(cd "$RUNNER" && bash ci/test-all.sh --changed-or-defer "$RUNNER_BASE" "$RUNNER_HEAD" 2>&1)" || status=$?
if [[ "$status" -ne 0 || "$output" == *"CI_FULL_DEFERRED:"* ||
      "$output" != *"selector returned unknown mode"* ]]; then
  printf 'FAIL: malformed selection remains local full (status %s; output %s)\n' \
    "$status" "$output" >&2
  exit 1
fi
printf '%s\n' 'full|unknown or shared path|' > "$RUNNER/selection"
TRUST_BASE="$RUNNER_HEAD"
printf '# trust-root change\n' >> "$RUNNER/ci/suite-registry.sh"
git -C "$RUNNER" add ci/suite-registry.sh
git -C "$RUNNER" commit -qm trust-root
TRUST_HEAD="$(git -C "$RUNNER" rev-parse HEAD)"
status=0
output="$(cd "$RUNNER" && bash ci/test-all.sh --changed-or-defer "$TRUST_BASE" "$TRUST_HEAD" 2>&1)" || status=$?
if [[ "$status" -ne 0 || "$output" == *"CI_FULL_DEFERRED:"* ||
      "$output" != *"executed=full"* ]]; then
  printf 'FAIL: trust-root change remains local full (status %s; output %s)\n' \
    "$status" "$output" >&2
  exit 1
fi

printf 'PASS: CI scope classification\n'
