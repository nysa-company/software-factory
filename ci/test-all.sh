#!/usr/bin/env bash
# One-command local suite. Each suite reports a single PASS/FAIL result.
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/software-factory-tests.XXXXXX")"
FAIL=0
trap 'rm -rf "$TMP"' EXIT

run_suite() {
  LABEL="$1"
  shift
  OUTPUT="$TMP/${LABEL// /-}.out"
  if "$@" > "$OUTPUT" 2>&1; then
    echo "PASS: $LABEL"
  else
    echo "FAIL: $LABEL" >&2
    awk '{print}' "$OUTPUT" >&2
    FAIL=1
  fi
}

run_conformance() {
  (cd "$ROOT/conformance/app" && npm test)
}

run_immutability() {
  BASE_REF="${BASE_REF:-origin/main}" TEST_PATHS="conformance/app/tests/" \
    bash "$ROOT/ci/test-immutability-check.sh"
}

run_suite "Linear reconciler regression suite" python3 "$ROOT/ci/linear-sync-test.py"
run_suite "factory script regression suite" bash "$ROOT/ci/test-factory-scripts.sh"
run_suite "dispatcher lease suite" bash "$ROOT/ci/dispatch-leases-test.sh"
run_suite "reorder test-fixes suite" bash "$ROOT/ci/reorder-test-fixes-test.sh"
run_suite "preflight suite" bash "$ROOT/ci/preflight-test.sh"
run_suite "Hermes contract suite" bash "$ROOT/ci/hermes-contract-test.sh"
run_suite "factory kit release suite" bash "$ROOT/ci/factory-kit-test.sh"
run_suite "conformance app suite" run_conformance
run_suite "test immutability suite" run_immutability
run_suite "artifact policy self-test" "$ROOT/scripts/artifact-check" --self-test

if [[ "$FAIL" -eq 0 ]]; then
  echo "PASS: full test suite"
else
  echo "FAIL: full test suite" >&2
fi
exit "$FAIL"
