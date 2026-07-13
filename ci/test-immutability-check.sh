#!/usr/bin/env bash
# test-immutability-check.sh — CI gate enforcing two mechanical rules that
# need no trusted commit identity (identity markers are agent-controlled and
# were a self-exemption bypass in v1 of this script):
#
#   Rule 1 — separation: a commit either touches only test paths (a "test
#   commit") or touches no test paths (an "implementation commit"). A commit
#   mixing both fails: that is a builder bundling test edits with code.
#
#   Rule 2 — order: every test commit must precede every implementation
#   commit on the branch. Tests are authored first and frozen; any test
#   change after implementation started fails the gate.
#
# A builder pushing a late test-only commit is caught by Rule 2. The reviewer
# additionally verifies session/PR provenance of the test commits.
#
# Factory bookkeeping (ticket logs, ledgers) is neither test nor
# implementation: dispatcher log commits interleave with every stage and must
# not trip the ordering rule. EXEMPT_PATHS names those pathspecs; files under
# them are ignored entirely by both rules.
#
# Env: BASE_REF (default origin/main), TEST_PATHS (space-separated pathspecs,
# default "tests/"), EXEMPT_PATHS (default "factory/ conformance/factory/").
# Wire as a required GitHub Actions status on every PR.
set -euo pipefail

BASE_REF="${BASE_REF:-origin/main}"
TEST_PATHS="${TEST_PATHS:-tests/}"
EXEMPT_PATHS="${EXEMPT_PATHS:-factory/ conformance/factory/}"
FAIL=0
SEEN_IMPL=0

is_exempt() { # path -> 0 if under any exempt prefix
  local f="$1" p
  for p in $EXEMPT_PATHS; do
    [[ "$f" == "$p"* ]] && return 0
  done
  return 1
}

# oldest → newest, so ordering can be checked in one pass
for COMMIT in $(git rev-list --reverse "$BASE_REF"..HEAD); do
  MSG="$(git log -1 --format=%s "$COMMIT")"
  ALL_FILES=""
  while IFS= read -r F; do
    [[ -z "$F" ]] && continue
    is_exempt "$F" || ALL_FILES+="$F"$'\n'
  done < <(git diff-tree --no-commit-id --name-only -r "$COMMIT")
  ALL_FILES="${ALL_FILES%$'\n'}"
  # shellcheck disable=SC2086
  TEST_FILES="$(git diff-tree --no-commit-id --name-only -r "$COMMIT" -- $TEST_PATHS)"
  NONTEST_FILES="$(comm -23 <(sort <<<"$ALL_FILES") <(sort <<<"$TEST_FILES") | sed '/^$/d')"

  if [[ -n "$TEST_FILES" && -n "$NONTEST_FILES" ]]; then
    echo "FAIL (rule 1, separation): commit $COMMIT ('$MSG') mixes test and non-test changes:" >&2
    echo "$TEST_FILES" | sed 's/^/  test: /' >&2
    echo "$NONTEST_FILES" | sed 's/^/  impl: /' >&2
    FAIL=1
  elif [[ -n "$TEST_FILES" ]]; then
    if [[ $SEEN_IMPL -eq 1 ]]; then
      echo "FAIL (rule 2, order): test commit $COMMIT ('$MSG') comes after implementation started — tests are frozen once building begins:" >&2
      echo "$TEST_FILES" | sed 's/^/  test: /' >&2
      FAIL=1
    fi
  elif [[ -n "$NONTEST_FILES" ]]; then
    SEEN_IMPL=1
  fi
done

if [[ $FAIL -eq 0 ]]; then
  echo "test immutability holds: test commits are pure and all precede implementation ($TEST_PATHS)"
fi
exit $FAIL
