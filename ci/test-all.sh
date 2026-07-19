#!/usr/bin/env bash
# One-command local suite. No arguments always runs every suite.
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/software-factory-tests.XXXXXX")"
FAIL=0
SHADOW_MISS=0
STARTED=$SECONDS
MODE="full"
PLANNED_MODE="full"
SELECTED=""
REASON="explicit full suite"
SHADOW=0
CHANGE_BASE="${BASE_REF:-origin/main}"
trap 'rm -rf "$TMP"' EXIT

summary() {
  printf '%s\n' "$*"
  if [[ -n "${CI_SUMMARY_FILE:-}" ]]; then
    printf -- '- %s\n' "$*" >> "$CI_SUMMARY_FILE"
  fi
}

if [[ $# -gt 0 ]]; then
  case "$1" in
    --changed|--shadow-changed)
      [[ "$1" != "--shadow-changed" ]] || SHADOW=1
      [[ $# -ge 2 && $# -le 3 ]] || { echo "usage: ci/test-all.sh [--changed|--shadow-changed BASE [HEAD]]" >&2; exit 2; }
      CHANGE_BASE="$2"
      CHANGE_HEAD="${3:-HEAD}"
      SELECTION="$(bash "$ROOT/ci/changed-test-suites.sh" "$CHANGE_BASE" "$CHANGE_HEAD")" || SELECTION="full|selector failed|"
      IFS='|' read -r PLANNED_MODE REASON SELECTED <<EOF
$SELECTION
EOF
      ;;
    *)
      echo "usage: ci/test-all.sh [--changed|--shadow-changed BASE [HEAD]]" >&2
      exit 2
      ;;
  esac
fi

ALL_IDS=" linear effective-ticket ledger attempt-cancel operator-console model-router model-manager model-control envelope-control claude-kimi failed-handoff fallback-approval model-fallback ci-scope factory-scripts dispatch-leases reorder-test-fixes preflight ticket-state ticket-attest legacy-closeout terminal-backfill hermes-contract factory-kit conformance immutability artifact-policy "
case "$PLANNED_MODE" in
  full) SELECTED="" ;;
  metadata) [[ -z "$SELECTED" ]] || PLANNED_MODE="full" ;;
  targeted)
    for id in $SELECTED; do
      [[ "$ALL_IDS" == *" $id "* ]] || { PLANNED_MODE="full"; REASON="selector returned unknown suite"; SELECTED=""; break; }
    done
    ;;
  *) PLANNED_MODE="full"; REASON="selector returned unknown mode"; SELECTED="" ;;
esac
MODE="$PLANNED_MODE"
[[ "$SHADOW" -eq 0 ]] || MODE="full"
DISPLAY_SUITES="$SELECTED"
[[ "$PLANNED_MODE" != "full" ]] || DISPLAY_SUITES="all"
[[ "$PLANNED_MODE" != "metadata" ]] || DISPLAY_SUITES="none"
summary "CI selection: planned=$PLANNED_MODE executed=$MODE reason=$REASON suites=$DISPLAY_SUITES"

selected() {
  [[ "$PLANNED_MODE" == "full" || " $SELECTED " == *" $1 "* ]]
}

should_run() {
  [[ "$MODE" == "full" || ( "$MODE" == "targeted" && " $SELECTED " == *" $1 "* ) ]]
}

run_suite() {
  ID="$1" LABEL="$2"
  shift 2
  should_run "$ID" || return 0
  OUTPUT="$TMP/${LABEL// /-}.out"
  SUITE_STARTED=$SECONDS
  if "$@" > "$OUTPUT" 2>&1; then
    summary "PASS: $LABEL ($((SECONDS - SUITE_STARTED))s)"
  else
    summary "FAIL: $LABEL ($((SECONDS - SUITE_STARTED))s)"
    awk '{print}' "$OUTPUT" >&2
    FAIL=1
    if [[ "$SHADOW" -eq 1 ]] && ! selected "$ID"; then
      summary "SHADOW_MISS: $ID was not selected"
      SHADOW_MISS=1
    fi
  fi
}

run_conformance() {
  (cd "$ROOT/conformance/app" && npm test)
}

run_immutability() {
  BASE_REF="${BASE_REF:-$CHANGE_BASE}" TEST_PATHS="conformance/app/tests/" \
    bash "$ROOT/ci/test-immutability-check.sh"
}

run_suite linear "Linear reconciler regression suite" python3 "$ROOT/ci/linear-sync-test.py"
run_suite effective-ticket "effective ticket overlay suite" python3 "$ROOT/ci/effective-ticket-test.py"
run_suite ledger "runtime ledger regression suite" python3 "$ROOT/ci/ledger-view-test.py"
run_suite attempt-cancel "targeted attempt cancellation suite" python3 "$ROOT/ci/attempt-cancel-test.py"
run_suite operator-console "operator console security suite" python3 "$ROOT/ci/operator-console-test.py"
run_suite model-router "model router regression suite" python3 "$ROOT/ci/model-router-test.py"
run_suite model-manager "model manager regression suite" python3 "$ROOT/ci/model-manager-test.py"
run_suite model-control "model control regression suite" python3 "$ROOT/ci/model-control-test.py"
run_suite envelope-control "envelope control regression suite" python3 "$ROOT/ci/envelope-control-test.py"
run_suite claude-kimi "disabled Claude Kimi adapter suite" python3 "$ROOT/ci/claude-kimi-adapter-test.py"
run_suite failed-handoff "failed-attempt handoff suite" python3 "$ROOT/ci/failed-attempt-handoff-test.py"
run_suite fallback-approval "model fallback approval suite" python3 "$ROOT/ci/model-fallback-approval-test.py"
run_suite model-fallback "model fallback transaction suite" python3 "$ROOT/ci/model-fallback-test.py"
run_suite ci-scope "selective CI scope" bash "$ROOT/ci/ci-scope-test.sh"
run_suite factory-scripts "factory script regression suite" bash "$ROOT/ci/test-factory-scripts.sh"
run_suite dispatch-leases "dispatcher lease suite" bash "$ROOT/ci/dispatch-leases-test.sh"
run_suite reorder-test-fixes "reorder test-fixes suite" bash "$ROOT/ci/reorder-test-fixes-test.sh"
run_suite preflight "preflight suite" bash "$ROOT/ci/preflight-test.sh"
run_suite ticket-state "ticket-state suite" bash "$ROOT/ci/ticket-state-test.sh"
run_suite ticket-attest "ticket attestation suite" python3 "$ROOT/ci/ticket-attest-test.py"
run_suite legacy-closeout "legacy closeout suite" python3 "$ROOT/ci/legacy-closeout-test.py"
run_suite terminal-backfill "terminal backfill suite" python3 "$ROOT/ci/terminal-backfill-test.py"
run_suite hermes-contract "Hermes contract suite" bash "$ROOT/ci/hermes-contract-test.sh"
run_suite factory-kit "factory kit release suite" bash "$ROOT/ci/factory-kit-test.sh"
run_suite conformance "conformance app suite" run_conformance
run_suite immutability "test immutability suite" run_immutability
run_suite artifact-policy "artifact policy self-test" "$ROOT/scripts/artifact-check" --self-test

if [[ "$FAIL" -eq 0 ]]; then
  summary "PASS: $MODE test suite ($((SECONDS - STARTED))s)"
else
  summary "FAIL: $MODE test suite ($((SECONDS - STARTED))s)"
fi
[[ "$SHADOW_MISS" -eq 0 ]] || summary "SHADOW_MISS: full verification exposed an unselected failure"
exit "$FAIL"
