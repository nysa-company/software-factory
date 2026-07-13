#!/usr/bin/env bash
# One-command local suite. Each suite reports a single PASS/FAIL result.
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FAIL=0

if bash "$ROOT/ci/test-factory-scripts.sh"; then
  echo "PASS: factory script regression suite"
else
  echo "FAIL: factory script regression suite" >&2
  FAIL=1
fi

APP_OUTPUT="$ROOT/.test-app-output.$$"
if (cd "$ROOT/conformance/app" && npm test) > "$APP_OUTPUT" 2>&1; then
  echo "PASS: conformance app suite"
else
  echo "FAIL: conformance app suite" >&2
  printf '%s\n' "--- conformance app output ---" >&2
  awk '{print}' "$APP_OUTPUT" >&2
  FAIL=1
fi
rm -f "$APP_OUTPUT"

IMMUTABILITY_OUTPUT="$ROOT/.test-immutability-output.$$"
if BASE_REF="${BASE_REF:-origin/main}" TEST_PATHS="conformance/app/tests/" \
  bash "$ROOT/ci/test-immutability-check.sh" > "$IMMUTABILITY_OUTPUT" 2>&1; then
  echo "PASS: test immutability suite"
else
  echo "FAIL: test immutability suite" >&2
  awk '{print}' "$IMMUTABILITY_OUTPUT" >&2
  FAIL=1
fi
rm -f "$IMMUTABILITY_OUTPUT"

if [[ "$FAIL" -eq 0 ]]; then
  echo "PASS: full test suite"
else
  echo "FAIL: full test suite" >&2
fi
exit "$FAIL"
