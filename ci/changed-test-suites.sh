#!/usr/bin/env bash
# Classify a committed diff as metadata, one known component, or full CI.
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BASE="${1:-}"
HEAD="${2:-HEAD}"
GROUP=""
SUITES=""
STATE=""

full() {
  printf 'full|%s|\n' "$1"
  exit 0
}

[[ "${CI_FORCE_FULL:-0}" != "1" ]] || full "CI_FORCE_FULL"
[[ -n "$BASE" && $# -le 2 ]] || full "missing or invalid arguments"
git -C "$ROOT" cat-file -e "$BASE^{commit}" 2>/dev/null || full "invalid base"
git -C "$ROOT" cat-file -e "$HEAD^{commit}" 2>/dev/null || full "invalid head"
MERGE_BASE="$(git -C "$ROOT" merge-base "$BASE" "$HEAD" 2>/dev/null)" || full "no merge base"
git -C "$ROOT" diff --quiet "$MERGE_BASE" "$HEAD" && full "empty diff"

LIGHTWEIGHT_STATUS=0
(cd "$ROOT" && bash ci/lightweight-change.sh "$MERGE_BASE" "$HEAD") || LIGHTWEIGHT_STATUS=$?
case "$LIGHTWEIGHT_STATUS" in
  0)
    printf 'metadata|inert metadata|\n'
    exit 0
    ;;
  1) ;;
  *) exit "$LIGHTWEIGHT_STATUS" ;;
esac

set_group() {
  local next_state="$1" next="$2" next_suites="$3"
  if [[ -n "$GROUP" && "$GROUP" != "$next" ]]; then
    full "multiple components"
  fi
  if [[ -n "$STATE" && "$STATE" != "$next_state" ]]; then
    full "inconsistent component state"
  fi
  STATE="$next_state"
  GROUP="$next"
  for suite in $next_suites; do
    if [[ " $SUITES " != *" $suite "* ]]; then
      SUITES="${SUITES:+$SUITES }$suite"
    fi
  done
}

while IFS= read -r -d '' status && IFS= read -r -d '' path; do
  [[ "$status" == "M" ]] || full "added, deleted, renamed, or type-changed path"
  case "$path" in
    docs/*|README.md|TODOS.md|context/memory.md|AGENTS.md|CLAUDE.md|\
      .github/pull_request_template.md|\
      conformance/SHAKEDOWN-REPORT.md)
      ;;
    ci/factory-controller-test.py)
      set_group targeted state-machine "factory-controller"
      ;;
    ci/state-machine-test.py|scripts/state-machine.py)
      set_group targeted state-machine "state-machine"
      ;;
    ci/ticket-state-test.sh)
      set_group targeted state-machine "ticket-state"
      ;;
    ci/ticket-transition-policy-test.py)
      set_group targeted state-machine "ticket-transition-policy"
      ;;
    scripts/lib/ticket_state_transition.py)
      full "shared role-control projection"
      ;;
    scripts/ticket-state.sh)
      set_group targeted state-machine \
        "ticket-state ticket-transition-policy"
      ;;
    scripts/local-release-canary.py|ci/local-release-canary-test.py)
      set_group targeted local-release-canary "local-release-canary"
      ;;
    scripts/operator-console.py|scripts/operator-snapshot.py)
      set_group targeted operator-console "operator-console"
      ;;
    scripts/adapters/claude-kimi.sh|scripts/lib/claude-kimi-output.py|\
      scripts/lib/claude-kimi-secret.py)
      set_group targeted claude-kimi "claude-kimi"
      ;;
    scripts/lib/failed_attempt_handoff.py)
      set_group targeted failed-handoff "failed-handoff model-fallback"
      ;;
    scripts/reorder-test-fixes.sh|scripts/lib/reorder_test_fixes.py)
      set_group targeted reorder-test-fixes "reorder-test-fixes factory-contract"
      ;;
    conformance/app/server.js|conformance/app/tests/*)
      set_group targeted conformance "conformance"
      ;;
    *)
      full "unknown or shared path"
      ;;
  esac
done < <(git -C "$ROOT" diff --name-status -z --no-renames "$MERGE_BASE" "$HEAD")

[[ -n "$GROUP" ]] || full "no recognized component"
printf '%s|%s|%s ci-scope immutability artifact-policy external-runtime-dependency\n' "$STATE" "$GROUP" "$SUITES"
