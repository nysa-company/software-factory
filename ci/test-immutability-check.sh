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
#   commit in its frozen-contract epoch. A later, append-only numbered frozen
#   contract reopens Test-author ownership; prose or an incomplete marker does
#   not. Within each epoch, tests remain frozen once implementation starts.
#
# A builder pushing a late test-only commit is caught by Rule 2. The reviewer
# additionally verifies session/PR provenance of the test commits.
#
# Factory bookkeeping (ticket logs, ledgers) is neither test nor
# implementation: dispatcher log commits interleave with every stage and must
# not trip the ordering rule. EXEMPT_PATHS names those pathspecs; files under
# them are ignored entirely by both rules. Entries ending in `/` name
# directory prefixes; other entries name exact files.
#
# Env: BASE_REF (default origin/main), TEST_PATHS (space-separated pathspecs,
# default "tests/"), EXEMPT_PATHS (default
# "factory/ conformance/factory/ .gitignore context/memory.md").
# Wire as a required GitHub Actions status on every PR.
set -euo pipefail

BASE_REF="${BASE_REF:-origin/main}"
TEST_PATHS="${TEST_PATHS:-tests/}"
EXEMPT_PATHS="${EXEMPT_PATHS:-factory/ conformance/factory/ .gitignore context/memory.md}"
FAIL=0
SEEN_IMPL=0

contract_epoch_reset() { # commit -> 0 only for one authenticated frozen-contract epoch
  local commit="$1" files ticket diff versions passes version pass prior_max
  local removed_versions removed_passes removed_version removed_pass
  files="$(git diff-tree --no-commit-id --name-only -r "$commit")"
  [[ "$(printf '%s\n' "$files" | sed '/^$/d' | wc -l | tr -d ' ')" == 1 ]] ||
    return 1
  ticket="$files"
  [[ "$ticket" =~ ^(factory|conformance/factory)/tickets/T-[^/]+\.md$ ]] ||
    return 1
  diff="$(git diff --no-ext-diff --unified=0 "$commit^" "$commit" -- "$ticket")" ||
    return 1
  versions="$(printf '%s\n' "$diff" | sed -n \
    's/^+###\{0,1\} Frozen contract — version \([1-9][0-9]*\)$/\1/p')"
  passes="$(printf '%s\n' "$diff" | sed -n \
    -e 's/^+- \*\*Freeze result — PASS\.\*\* Contract version \([1-9][0-9]*\) is frozen\.$/\1/p' \
    -e 's/^+- \*\*Freeze result:\*\* PASS\. Contract version \([1-9][0-9]*\) is frozen\([.;].*\)\{0,1\}$/\1/p' \
    -e 's/^+- \*\*Freeze result:\*\* PASS\. Contract version \([1-9][0-9]*\) supersedes \(contract \)\{0,1\}versions\{0,1\} [1-9][0-9]*.*$/\1/p')"
  removed_versions="$(printf '%s\n' "$diff" | sed -n \
    's/^-###\{0,1\} Frozen contract — version \([1-9][0-9]*\)$/\1/p')"
  removed_passes="$(printf '%s\n' "$diff" | sed -n \
    -e 's/^-- \*\*Freeze result — PASS\.\*\* Contract version \([1-9][0-9]*\) is frozen\.$/\1/p' \
    -e 's/^-- \*\*Freeze result:\*\* PASS\. Contract version \([1-9][0-9]*\) is frozen\([.;].*\)\{0,1\}$/\1/p' \
    -e 's/^-- \*\*Freeze result:\*\* PASS\. Contract version \([1-9][0-9]*\) supersedes \(contract \)\{0,1\}versions\{0,1\} [1-9][0-9]*.*$/\1/p')"
  if [[ "$(printf '%s\n' "$versions" | sed '/^$/d' | wc -l | tr -d ' ')" != 1 ||
        "$(printf '%s\n' "$passes" | sed '/^$/d' | wc -l | tr -d ' ')" != 1 ]]; then
    if [[ -z "$versions" && -n "$passes" ]]; then
      echo "FAIL (contract epoch): $commit has a PASS marker without exactly one ##/### Frozen contract heading" >&2
      FAIL=1
    fi
    return 1
  fi
  version="$versions"; pass="$passes"
  [[ "$version" == "$pass" ]] || return 1
  prior_max="$(git show "$commit^:$ticket" 2>/dev/null | sed -n \
    's/^###\{0,1\} Frozen contract — version \([1-9][0-9]*\)$/\1/p' | sort -n | tail -1)"
  prior_max="${prior_max:-0}"
  if [[ -n "$removed_versions" || -n "$removed_passes" ]]; then
    [[ "$(printf '%s\n' "$removed_versions" | sed '/^$/d' | wc -l | tr -d ' ')" == 1 &&
       "$(printf '%s\n' "$removed_passes" | sed '/^$/d' | wc -l | tr -d ' ')" == 1 ]] ||
      return 1
    removed_version="$removed_versions"; removed_pass="$removed_passes"
    [[ "$removed_version" == "$removed_pass" && "$removed_version" == "$prior_max" ]] ||
      return 1
  fi
  [[ "$version" -gt "$prior_max" ]]
}

is_exempt() { # path -> 0 if under any exempt prefix
  local f="$1" p
  for p in $EXEMPT_PATHS; do
    if [[ "$p" == */ ]]; then
      [[ "$f" == "$p"* ]] && return 0
    else
      [[ "$f" == "$p" ]] && return 0
    fi
  done
  return 1
}

# oldest → newest, so ordering can be checked in one pass
for COMMIT in $(git rev-list --reverse "$BASE_REF"..HEAD); do
  MSG="$(git log -1 --format=%s "$COMMIT")"
  if contract_epoch_reset "$COMMIT"; then
    SEEN_IMPL=0
  fi
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
