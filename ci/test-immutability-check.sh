#!/usr/bin/env bash
# test-immutability-check.sh — CI gate: builder commits must not touch test files.
# Wire as a required GitHub Actions status on every PR.
#
# Convention: test-author commits carry "[test-author]" in the commit message
# (or a distinct git author, configured at instantiation); every other commit
# on the branch is treated as builder work and may not modify TEST_PATHS.
#
# Env: BASE_REF (default origin/main), TEST_PATHS (default "tests/ **/*.test.* **/*.spec.*")
set -euo pipefail

BASE_REF="${BASE_REF:-origin/main}"
TEST_PATHS="${TEST_PATHS:-tests/}"
FAIL=0

for COMMIT in $(git rev-list "$BASE_REF"..HEAD); do
  MSG="$(git log -1 --format=%s "$COMMIT")"
  AUTHOR="$(git log -1 --format=%an "$COMMIT")"
  if [[ "$MSG" == *"[test-author]"* || "$AUTHOR" == "test-author"* ]]; then
    continue  # test-author commits may create/modify tests
  fi
  TOUCHED="$(git diff-tree --no-commit-id --name-only -r "$COMMIT" -- $TEST_PATHS)"
  if [[ -n "$TOUCHED" ]]; then
    echo "FAIL: builder commit $COMMIT ('$MSG') touches test files:" >&2
    echo "$TOUCHED" >&2
    FAIL=1
  fi
done

if [[ $FAIL -eq 0 ]]; then
  echo "test immutability holds: no builder commit touches $TEST_PATHS"
fi
exit $FAIL
