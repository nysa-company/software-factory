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

if (cd "$ROOT" && bash ci/lightweight-change.sh "$MERGE_BASE" "$HEAD"); then
  printf 'metadata|inert metadata|\n'
  exit 0
fi

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
  SUITES="$next_suites"
}

while IFS= read -r -d '' status && IFS= read -r -d '' path; do
  [[ "$status" == "M" ]] || full "added, deleted, renamed, or type-changed path"
  case "$path" in
    docs/*|README.md|TODOS.md|context/memory.md|AGENTS.md|CLAUDE.md|\
      .github/pull_request_template.md|integrations/hermes/CHANGELOG.md|\
      conformance/SHAKEDOWN-REPORT.md)
      ;;
    scripts/linear-sync.py)
      set_group targeted linear "linear"
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
      set_group targeted reorder-test-fixes "reorder-test-fixes hermes-contract"
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
printf '%s|%s|%s ci-scope immutability artifact-policy\n' "$STATE" "$GROUP" "$SUITES"
