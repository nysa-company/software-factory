#!/usr/bin/env bash
# ci/reorder-test-fixes-test.sh
#
# Synthetic-repo test suite for scripts/reorder-test-fixes.sh. Builds throwaway
# git repos under mktemp -d, exercises the reorder tool against them, and
# checks results against the *real* ci/test-immutability-check.sh gate (the
# same script CI runs) wherever a scenario's outcome should be gate-visible.
#
# Usage: bash ci/reorder-test-fixes-test.sh
#
# Prints one PASS/FAIL line per scenario and a final summary. Exits 0 only if
# every scenario passed.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
REORDER="$REPO_ROOT/scripts/reorder-test-fixes.sh"
GATE="$REPO_ROOT/ci/test-immutability-check.sh"

TEST_PATHS="conformance/app/tests/"
EXEMPT_PATHS="conformance/factory/"

PASS_TOTAL=0
FAIL_TOTAL=0
CLEANUP_DIRS=""

on_exit() {
  local d
  for d in $CLEANUP_DIRS; do
    [ -d "$d" ] && rm -rf "$d"
  done
}
trap on_exit EXIT

# ---------- repo-building helpers ----------

new_repo() {
  local d
  d="$(mktemp -d "${TMPDIR:-/tmp}/reorder-test-fixes.XXXXXX")"
  CLEANUP_DIRS="$CLEANUP_DIRS $d"
  git -C "$d" init -q
  git -C "$d" config user.email test@example.com
  git -C "$d" config user.name "Test Author"
  printf '%s\n' "$d"
}

write_file() { # write_file <repo> <relpath> <content...>
  local repo="$1" rel="$2"; shift 2
  mkdir -p "$(dirname "$repo/$rel")"
  printf '%s\n' "$@" > "$repo/$rel"
}

append_file() { # append_file <repo> <relpath> <line...>
  local repo="$1" rel="$2"; shift 2
  mkdir -p "$(dirname "$repo/$rel")"
  printf '%s\n' "$@" >> "$repo/$rel"
}

commit_all() { # commit_all <repo> <message>
  git -C "$1" add -A
  git -C "$1" commit -q -m "$2"
}

head_sha() { git -C "$1" rev-parse HEAD; }
head_tree() { git -C "$1" rev-parse HEAD^{tree}; }

# Log files live as siblings of the repo dir (outside the git worktree, e.g.
# "/tmp/tmp.XXXX.gate-out.log" next to "/tmp/tmp.XXXX/"), never inside it —
# writing them inside the repo would itself make the working tree "dirty" and
# trip the tool's own dirty-tree safety check.
gate_log() { printf '%s' "${1}.gate-out.log"; }
reorder_log() { printf '%s' "${1}.reorder-out.log"; }

run_gate() { # run_gate <repo> <base_sha> -> gate exit code, output in $(gate_log repo)
  ( cd "$1" && BASE_REF="$2" TEST_PATHS="$TEST_PATHS" EXEMPT_PATHS="$EXEMPT_PATHS" bash "$GATE" ) \
    >"$(gate_log "$1")" 2>&1
  return $?
}

run_reorder() { # run_reorder <repo> <base_sha> -> reorder exit code, output in $(reorder_log repo)
  ( cd "$1" && bash "$REORDER" --base "$2" --test-paths "$TEST_PATHS" --exempt-paths "$EXEMPT_PATHS" ) \
    >"$(reorder_log "$1")" 2>&1
  return $?
}

commit_order() { # commit_order <repo> <base_sha> -> newline list of subjects oldest->newest
  git -C "$1" log --reverse --format=%s "$2..HEAD"
}

# ---------- scenario 1: clean case ----------
# test fix after impl, no overlapping files -> reordered, gate passes, tree identical.
scenario_clean() {
  local repo base orig_tree rc
  repo="$(new_repo)"

  write_file "$repo" conformance/app/server.js "server v0"
  write_file "$repo" conformance/factory/tickets/T-CLEAN.md "L1"
  commit_all "$repo" "base"
  base="$(head_sha "$repo")"

  append_file "$repo" conformance/app/server.js "server v1: add feature"
  commit_all "$repo" "impl: add feature"

  write_file "$repo" conformance/app/tests/clean.test.js "test('clean', () => {});"
  commit_all "$repo" "test: fix coverage gap found by reviewer"

  append_file "$repo" conformance/factory/tickets/T-CLEAN.md "log: reviewer note"
  commit_all "$repo" "log: reviewer note"

  orig_tree="$(head_tree "$repo")"

  if run_gate "$repo" "$base"; then
    echo "  [clean] unexpected: gate already passes before reorder"
    return 1
  fi

  run_reorder "$repo" "$base"; rc=$?
  if [ "$rc" -ne 0 ]; then
    echo "  [clean] reorder exited $rc, expected 0"
    cat "$(reorder_log "$repo")"
    return 1
  fi
  if grep -q "NOTHING-TO-DO" "$(reorder_log "$repo")"; then
    echo "  [clean] reorder reported NOTHING-TO-DO, expected an actual reorder"
    return 1
  fi

  if [ "$(head_tree "$repo")" != "$orig_tree" ]; then
    echo "  [clean] tree changed after reorder (orig $orig_tree, new $(head_tree "$repo"))"
    return 1
  fi

  if ! run_gate "$repo" "$base"; then
    echo "  [clean] gate still fails after reorder"
    cat "$(gate_log "$repo")"
    return 1
  fi

  local order
  order="$(commit_order "$repo" "$base")"
  if [ "$(printf '%s\n' "$order" | grep -n . | grep 'test: fix coverage' | cut -d: -f1)" \
     -ge "$(printf '%s\n' "$order" | grep -n . | grep 'impl: add feature' | cut -d: -f1)" ]; then
    echo "  [clean] test commit does not precede impl commit after reorder"
    printf '%s\n' "$order"
    return 1
  fi

  return 0
}

# ---------- scenario 2: T-104-shaped ----------
# late test commit also touches an exempt bookkeeping file that later
# bookkeeping commits also touch -> forces a conflict on the exempt file only.
# Success (identical final tree, gate passes) is strongly preferred and is
# what this tool achieves; see reorder_test_fixes.py's resolve_exempt_conflict
# docstring for why the resolution converges correctly.
scenario_t104_shaped() {
  local repo base orig_tree rc
  repo="$(new_repo)"

  write_file "$repo" conformance/app/server.js "server v0"
  write_file "$repo" conformance/factory/tickets/T-104.md "L1: ticket opened"
  commit_all "$repo" "base"
  base="$(head_sha "$repo")"

  append_file "$repo" conformance/app/server.js "server v1: retry-dead-job route"
  commit_all "$repo" "impl: retry-dead-job route"

  append_file "$repo" conformance/factory/tickets/T-104.md "L2: log builder run"
  commit_all "$repo" "log: builder run"

  write_file "$repo" conformance/app/tests/retry-dead.test.js "test('retry dead', () => {});"
  append_file "$repo" conformance/factory/tickets/T-104.md "L3: log reviewer fix tests"
  commit_all "$repo" "test: reviewer-requested retry-dead coverage"

  append_file "$repo" conformance/factory/tickets/T-104.md "L4: log bk1"
  commit_all "$repo" "log: bk1"

  append_file "$repo" conformance/factory/tickets/T-104.md "L5: log bk2"
  commit_all "$repo" "log: bk2"

  orig_tree="$(head_tree "$repo")"

  if run_gate "$repo" "$base"; then
    echo "  [t104] unexpected: gate already passes before reorder"
    return 1
  fi

  run_reorder "$repo" "$base"; rc=$?
  if [ "$rc" -ne 0 ]; then
    echo "  [t104] reorder exited $rc (abort), expected success (0) for this scenario"
    cat "$(reorder_log "$repo")"
    return 1
  fi

  if [ "$(head_tree "$repo")" != "$orig_tree" ]; then
    echo "  [t104] tree changed after reorder (orig $orig_tree, new $(head_tree "$repo"))"
    return 1
  fi

  if ! run_gate "$repo" "$base"; then
    echo "  [t104] gate still fails after reorder"
    cat "$(gate_log "$repo")"
    return 1
  fi

  return 0
}

# ---------- scenario 3: genuine conflict on a TEST file ----------
# a rule-1-violating "mixed" commit (illegal, but the tool must not choke on
# it) edits the same line of a test file that the later test-fix commit also
# edits -> moving the test-fix commit earlier collides with real (non-exempt)
# content -> must abort cleanly and restore the original HEAD.
scenario_genuine_test_conflict() {
  local repo base orig_head rc
  repo="$(new_repo)"

  write_file "$repo" conformance/app/tests/shared.test.js "line1" "line2" "line3"
  write_file "$repo" conformance/app/server.js "server v0"
  commit_all "$repo" "base"
  base="$(head_sha "$repo")"

  append_file "$repo" conformance/app/server.js "server v1"
  commit_all "$repo" "impl: add feature"

  write_file "$repo" conformance/app/tests/shared.test.js "line1" "line2-mixed" "line3"
  append_file "$repo" conformance/app/server.js "server v2 bundled with a test edit"
  commit_all "$repo" "builder: illegal commit that also touches the test file"

  write_file "$repo" conformance/app/tests/shared.test.js "line1" "line2-mixed-fixed-by-reviewer" "line3"
  commit_all "$repo" "test: reviewer fix (conflicts with the illegal commit above)"

  orig_head="$(head_sha "$repo")"

  run_reorder "$repo" "$base"; rc=$?
  if [ "$rc" -eq 0 ]; then
    echo "  [conflict] reorder unexpectedly succeeded; expected a clean abort"
    cat "$(reorder_log "$repo")"
    return 1
  fi

  if [ "$(head_sha "$repo")" != "$orig_head" ]; then
    echo "  [conflict] HEAD moved after aborted reorder (orig $orig_head, now $(head_sha "$repo"))"
    return 1
  fi

  if [ -n "$(git -C "$repo" status --porcelain)" ]; then
    echo "  [conflict] working tree not clean after abort"
    git -C "$repo" status --porcelain
    return 1
  fi

  if [ -d "$repo/.git/CHERRY_PICK_HEAD" ] || [ -f "$repo/.git/CHERRY_PICK_HEAD" ]; then
    echo "  [conflict] a cherry-pick was left in progress after abort"
    return 1
  fi

  if ! grep -qi "abort\|non-exempt" "$(reorder_log "$repo")"; then
    echo "  [conflict] abort message did not clearly explain the non-exempt conflict"
    cat "$(reorder_log "$repo")"
    return 1
  fi

  return 0
}

# ---------- scenario 4: already-ordered branch ----------
scenario_already_ordered() {
  local repo base orig_head rc
  repo="$(new_repo)"

  write_file "$repo" conformance/app/server.js "server v0"
  commit_all "$repo" "base"
  base="$(head_sha "$repo")"

  write_file "$repo" conformance/app/tests/ordered.test.js "test('ordered', () => {});"
  commit_all "$repo" "test: add tests first"

  append_file "$repo" conformance/app/server.js "server v1"
  commit_all "$repo" "impl: implement"

  if ! run_gate "$repo" "$base"; then
    echo "  [ordered] precondition failed: gate should already pass"
    cat "$(gate_log "$repo")"
    return 1
  fi

  orig_head="$(head_sha "$repo")"

  run_reorder "$repo" "$base"; rc=$?
  if [ "$rc" -ne 0 ]; then
    echo "  [ordered] reorder exited $rc, expected 0"
    cat "$(reorder_log "$repo")"
    return 1
  fi

  if ! grep -q "NOTHING-TO-DO" "$(reorder_log "$repo")"; then
    echo "  [ordered] expected NOTHING-TO-DO in output"
    cat "$(reorder_log "$repo")"
    return 1
  fi

  if [ "$(head_sha "$repo")" != "$orig_head" ]; then
    echo "  [ordered] HEAD changed even though nothing should have moved"
    return 1
  fi

  return 0
}

# ---------- scenario 5: dirty working tree ----------
scenario_dirty_tree() {
  local repo base orig_head rc
  repo="$(new_repo)"

  write_file "$repo" conformance/app/server.js "server v0"
  commit_all "$repo" "base"
  base="$(head_sha "$repo")"

  append_file "$repo" conformance/app/server.js "server v1"
  commit_all "$repo" "impl: implement"

  write_file "$repo" conformance/app/tests/late.test.js "test('late', () => {});"
  commit_all "$repo" "test: late fix"

  orig_head="$(head_sha "$repo")"
  append_file "$repo" conformance/app/server.js "uncommitted local edit"

  run_reorder "$repo" "$base"; rc=$?
  if [ "$rc" -eq 0 ]; then
    echo "  [dirty] reorder unexpectedly succeeded on a dirty working tree"
    return 1
  fi

  if ! grep -qi "dirty\|clean" "$(reorder_log "$repo")"; then
    echo "  [dirty] refusal message did not mention the dirty working tree"
    cat "$(reorder_log "$repo")"
    return 1
  fi

  if [ "$(head_sha "$repo")" != "$orig_head" ]; then
    echo "  [dirty] HEAD changed despite refusal"
    return 1
  fi

  return 0
}

# ---------- scenario 6 (bonus): default pathspecs, no --test-paths/--exempt-paths ----------
scenario_default_paths() {
  local repo base orig_tree rc
  repo="$(new_repo)"

  write_file "$repo" src/app.js "app v0"
  commit_all "$repo" "base"
  base="$(head_sha "$repo")"

  append_file "$repo" src/app.js "app v1"
  commit_all "$repo" "impl: implement"

  write_file "$repo" tests/app.test.js "test('app', () => {});"
  commit_all "$repo" "test: late fix using default tests/ path"

  orig_tree="$(head_tree "$repo")"

  rc=0
  ( cd "$repo" && bash "$REORDER" --base "$base" ) >"$(reorder_log "$repo")" 2>&1 || rc=$?
  if [ "$rc" -ne 0 ]; then
    echo "  [defaults] reorder exited $rc, expected 0"
    cat "$(reorder_log "$repo")"
    return 1
  fi

  if [ "$(head_tree "$repo")" != "$orig_tree" ]; then
    echo "  [defaults] tree changed after reorder"
    return 1
  fi

  ( cd "$repo" && BASE_REF="$base" bash "$GATE" ) >"$(gate_log "$repo")" 2>&1
  if [ $? -ne 0 ]; then
    echo "  [defaults] gate (default TEST_PATHS/EXEMPT_PATHS) still fails after reorder"
    cat "$(gate_log "$repo")"
    return 1
  fi

  return 0
}

# ---------- scenario 7: narrow default bookkeeping exemptions ----------
# Exact bookkeeping files may precede tests without starting implementation;
# docs remain implementation and therefore force a later test fix to move.
scenario_targeted_default_exemptions() {
  local repo base orig_tree rc order
  repo="$(new_repo)"

  write_file "$repo" src/app.js "app v0"
  commit_all "$repo" "base"
  base="$(head_sha "$repo")"

  write_file "$repo" .gitignore "*.out"
  commit_all "$repo" "chore: ignore run output"

  write_file "$repo" context/memory.md "durable bookkeeping"
  commit_all "$repo" "docs: update memory"

  write_file "$repo" tests/first.test.js "test('first', () => {});"
  commit_all "$repo" "test: author initial contract"

  write_file "$repo" docs/contract.md "contract change"
  commit_all "$repo" "docs: change contract"

  write_file "$repo" tests/late.test.js "test('late', () => {});"
  commit_all "$repo" "test: late contract coverage"

  orig_tree="$(head_tree "$repo")"
  if ( cd "$repo" && BASE_REF="$base" bash "$GATE" ) >"$(gate_log "$repo")" 2>&1; then
    echo "  [targeted-defaults] gate passed before reorder; docs may have been broadly exempted"
    return 1
  fi

  rc=0
  ( cd "$repo" && bash "$REORDER" --base "$base" ) >"$(reorder_log "$repo")" 2>&1 || rc=$?
  if [ "$rc" -ne 0 ]; then
    echo "  [targeted-defaults] reorder exited $rc, expected 0"
    cat "$(reorder_log "$repo")"
    return 1
  fi
  if [ "$(head_tree "$repo")" != "$orig_tree" ]; then
    echo "  [targeted-defaults] tree changed after reorder"
    return 1
  fi
  if ! ( cd "$repo" && BASE_REF="$base" bash "$GATE" ) >"$(gate_log "$repo")" 2>&1; then
    echo "  [targeted-defaults] gate failed after reorder"
    cat "$(gate_log "$repo")"
    return 1
  fi

  order="$(commit_order "$repo" "$base")"
  if [ "$(printf '%s\n' "$order" | grep -n 'test: late contract coverage' | cut -d: -f1)" \
     -ge "$(printf '%s\n' "$order" | grep -n 'docs: change contract' | cut -d: -f1)" ]; then
    echo "  [targeted-defaults] late test did not move before non-exempt docs"
    return 1
  fi
  return 0
}

# ---------- runner ----------

run_scenario() { # run_scenario <name> <function>
  local name="$1" fn="$2" out rc
  out="$("$fn" 2>&1)"
  rc=$?
  if [ "$rc" -eq 0 ]; then
    echo "PASS: $name"
    PASS_TOTAL=$((PASS_TOTAL + 1))
  else
    echo "FAIL: $name"
    printf '%s\n' "$out" | sed 's/^/    /'
    FAIL_TOTAL=$((FAIL_TOTAL + 1))
  fi
}

echo "== reorder-test-fixes.sh synthetic-repo test suite =="
echo "reorder tool: $REORDER"
echo "gate script:  $GATE"
echo

run_scenario "scenario-1-clean-case"                scenario_clean
run_scenario "scenario-2-t104-shaped-exempt-conflict" scenario_t104_shaped
run_scenario "scenario-3-genuine-test-file-conflict"  scenario_genuine_test_conflict
run_scenario "scenario-4-already-ordered"             scenario_already_ordered
run_scenario "scenario-5-dirty-working-tree"          scenario_dirty_tree
run_scenario "scenario-6-default-pathspecs-bonus"     scenario_default_paths
run_scenario "scenario-7-targeted-default-exemptions" scenario_targeted_default_exemptions

echo
echo "== summary: $PASS_TOTAL passed, $FAIL_TOTAL failed =="

if [ "$FAIL_TOTAL" -ne 0 ]; then
  exit 1
fi
exit 0
