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
DEFER_FULL=0
DEFER_AFTER=0
SHARD="all"
CHANGE_BASE="${BASE_REF:-origin/main}"
trap 'rm -rf "$TMP"' EXIT
. "$ROOT/ci/suite-registry.sh"

# Keep the slowest suites on separate runners; new suites default to release.
FACTORY_SHARD_IDS=" factory-scripts provider-executor provider-activation provider-artifact-controller "
HERMES_SHARD_IDS=" hermes-contract preflight ticket-attest provider-coordinator provider-credential-broker provider-recovery "

summary() {
  printf '%s\n' "$*"
  if [[ -n "${CI_SUMMARY_FILE:-}" ]]; then
    printf -- '- %s\n' "$*" >> "$CI_SUMMARY_FILE"
  fi
}

ALL_IDS=" "
REGISTRY_ERROR=""
collect_suite() {
  local id="$1"
  if [[ ! "$id" =~ ^[a-z0-9][a-z0-9-]*$ ]]; then
    REGISTRY_ERROR="malformed suite ID"
  elif [[ "$ALL_IDS" == *" $id "* ]]; then
    REGISTRY_ERROR="duplicate suite ID: $id"
  else
    ALL_IDS="$ALL_IDS$id "
  fi
}
suite_registry collect_suite
if [[ -n "$REGISTRY_ERROR" ]]; then
  echo "invalid suite registry: $REGISTRY_ERROR" >&2
  exit 2
fi

if [[ $# -gt 0 ]]; then
  case "$1" in
    --shard)
      [[ $# -eq 2 ]] || { echo "usage: ci/test-all.sh [--shard factory|hermes|release | --changed|--shadow-changed|--changed-or-defer BASE [HEAD]]" >&2; exit 2; }
      SHARD="$2"
      case "$SHARD" in factory|hermes|release) ;; *) echo "unknown suite shard: $SHARD" >&2; exit 2 ;; esac
      REASON="complete GitHub shard: $SHARD"
      ;;
    --changed|--shadow-changed|--changed-or-defer)
      [[ "$1" != "--shadow-changed" ]] || SHADOW=1
      [[ "$1" != "--changed-or-defer" ]] || DEFER_FULL=1
      [[ $# -ge 2 && $# -le 3 ]] || { echo "usage: ci/test-all.sh [--shard factory|hermes|release | --changed|--shadow-changed|--changed-or-defer BASE [HEAD]]" >&2; exit 2; }
      CHANGE_BASE="$2"
      CHANGE_HEAD="${3:-HEAD}"
      SELECTION="$(bash "$ROOT/ci/changed-test-suites.sh" "$CHANGE_BASE" "$CHANGE_HEAD")" || SELECTION="full|selector failed|"
      IFS='|' read -r PLANNED_MODE REASON SELECTED <<EOF
$SELECTION
EOF
      ;;
    *)
      echo "usage: ci/test-all.sh [--shard factory|hermes|release | --changed|--shadow-changed|--changed-or-defer BASE [HEAD]]" >&2
      exit 2
      ;;
  esac
fi

case "$PLANNED_MODE" in
  full) SELECTED="" ;;
  metadata)
    if [[ -n "$SELECTED" ]]; then
      PLANNED_MODE="full" REASON="metadata selection returned suites" SELECTED=""
    fi
    ;;
  targeted|shadow)
    if [[ -z "$SELECTED" ]]; then
      PLANNED_MODE="full" REASON="selector returned empty selection" SELECTED=""
    fi
    NORMALIZED=" "
    for id in $SELECTED; do
      if [[ "$ALL_IDS" != *" $id "* ]]; then
        PLANNED_MODE="full" REASON="selector returned unknown suite: $id" SELECTED=""
        break
      fi
      if [[ "$NORMALIZED" == *" $id "* ]]; then
        PLANNED_MODE="full" REASON="selector returned duplicate suite: $id" SELECTED=""
        break
      fi
      NORMALIZED="$NORMALIZED$id "
    done
    ;;
  *) PLANNED_MODE="full"; REASON="selector returned unknown mode"; SELECTED="" ;;
esac
MODE="$PLANNED_MODE"
COMPARE="$SHADOW"
if [[ "$PLANNED_MODE" == "shadow" ]]; then
  MODE="full"
  COMPARE=1
elif [[ "$SHADOW" -eq 1 ]]; then
  MODE="full"
fi

if [[ "$DEFER_FULL" -eq 1 && "$PLANNED_MODE" == "full" ]]; then
  MODE="targeted"
  SELECTED="ci-scope immutability artifact-policy"
  DEFER_AFTER=1
  REASON="deferred to required GitHub full CI: $REASON"
fi
DISPLAY_SUITES="$SELECTED"
[[ "$PLANNED_MODE" != "full" || "$DEFER_AFTER" -eq 1 ]] || DISPLAY_SUITES="all"
[[ "$PLANNED_MODE" != "metadata" ]] || DISPLAY_SUITES="none"
[[ "$SHARD" == "all" ]] || DISPLAY_SUITES="shard:$SHARD"
summary "CI selection: component_state=$PLANNED_MODE executed=$MODE shard=$SHARD reason=$REASON suites=$DISPLAY_SUITES"

selected() {
  [[ "$PLANNED_MODE" == "full" || " $SELECTED " == *" $1 "* ]]
}

should_run() {
  local id="$1"
  case "$SHARD" in
    factory) [[ "$FACTORY_SHARD_IDS" == *" $id "* ]] || return 1 ;;
    hermes) [[ "$HERMES_SHARD_IDS" == *" $id "* ]] || return 1 ;;
    release)
      [[ "$FACTORY_SHARD_IDS" != *" $id "* && "$HERMES_SHARD_IDS" != *" $id "* ]] || return 1
      ;;
  esac
  [[ "$MODE" == "full" || ( "$MODE" == "targeted" && " $SELECTED " == *" $id "* ) ]]
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
    if [[ "$COMPARE" -eq 1 ]] && ! selected "$ID"; then
      RECHECK_OUTPUT="$TMP/${LABEL// /-}.recheck.out"
      summary "SHADOW_RECHECK: reproducing unselected failure for $ID"
      if "$@" > "$RECHECK_OUTPUT" 2>&1; then
        summary "SHADOW_FLAKE: $ID passed its immediate recheck"
      else
        awk '{print}' "$RECHECK_OUTPUT" >&2
        summary "SHADOW_MISS: $ID was not selected and failed its immediate recheck"
        SHADOW_MISS=1
      fi
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

suite_registry run_suite

RUN_LABEL="$MODE"
[[ "$SHARD" == "all" ]] || RUN_LABEL="$SHARD shard"
if [[ "$FAIL" -eq 0 ]]; then
  summary "PASS: $RUN_LABEL test suite ($((SECONDS - STARTED))s)"
else
  summary "FAIL: $RUN_LABEL test suite ($((SECONDS - STARTED))s)"
fi
[[ "$SHADOW_MISS" -eq 0 ]] || summary "SHADOW_MISS: full verification exposed an unselected failure"
if [[ "$DEFER_AFTER" -eq 1 && "$FAIL" -eq 0 ]]; then
  summary "CI_FULL_DEFERRED: reason=$REASON"
fi
exit "$FAIL"
