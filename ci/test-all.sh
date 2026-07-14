#!/usr/bin/env bash
# One-command local suite. Each suite reports a single PASS/FAIL result.
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FAIL=0

if "$ROOT/scripts/repo-check"; then
  echo "PASS: repository baseline"
else
  echo "FAIL: repository baseline" >&2
  FAIL=1
fi

if "$ROOT/scripts/secret-scan"; then
  echo "PASS: secret scan"
else
  echo "FAIL: secret scan" >&2
  FAIL=1
fi

if bash "$ROOT/ci/secret-scan-test.sh"; then
  echo "PASS: secret scanner regression suite"
else
  echo "FAIL: secret scanner regression suite" >&2
  FAIL=1
fi

if bash "$ROOT/ci/adapter-policy-test.sh"; then
  echo "PASS: adapter policy suite"
else
  echo "FAIL: adapter policy suite" >&2
  FAIL=1
fi

if bash "$ROOT/ci/codex-permission-test.sh"; then
  echo "PASS: Codex permission profile suite"
else
  echo "FAIL: Codex permission profile suite" >&2
  FAIL=1
fi

if bash "$ROOT/ci/test-factory-scripts.sh"; then
  echo "PASS: factory script regression suite"
else
  echo "FAIL: factory script regression suite" >&2
  FAIL=1
fi

if python3 "$ROOT/ci/linear-sync-test.py"; then
  echo "PASS: Linear reconciler regression suite"
else
  echo "FAIL: Linear reconciler regression suite" >&2
  FAIL=1
fi

if bash "$ROOT/ci/preflight-test.sh"; then
  echo "PASS: preflight regression suite"
else
  echo "FAIL: preflight regression suite" >&2
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
