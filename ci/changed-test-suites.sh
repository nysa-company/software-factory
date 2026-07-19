#!/usr/bin/env bash
# Classify a committed diff as metadata, one known component, or full CI.
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BASE="${1:-}"
HEAD="${2:-HEAD}"
GROUP=""
SUITES=""

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
  local next="$1" next_suites="$2"
  if [[ -n "$GROUP" && "$GROUP" != "$next" ]]; then
    full "multiple components"
  fi
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
      set_group linear "linear"
      ;;
    scripts/lib/effective_ticket.py)
      set_group effective-ticket "effective-ticket"
      ;;
    scripts/ledger-view.py)
      set_group ledger "ledger"
      ;;
    scripts/attempt-cancel.py)
      set_group attempt-cancel "attempt-cancel operator-console"
      ;;
    scripts/operator-console.py|scripts/operator-snapshot.py|scripts/operator-state.py|\
      integrations/operator-console/*)
      set_group operator-console "operator-console"
      ;;
    scripts/model-router.py|scripts/model-manager.py|scripts/model-control.sh|\
      scripts/model-fallback.py|scripts/model-routing/*|\
      scripts/lib/model-fallback-approval.py|scripts/lib/cursor-model-families.txt)
      set_group model-routing \
        "model-router model-manager model-control failed-handoff fallback-approval model-fallback"
      ;;
    scripts/envelope-control.py|envelope/*)
      set_group envelope-control "envelope-control operator-console"
      ;;
    scripts/adapters/claude-kimi.sh|scripts/lib/claude-kimi-output.py|\
      scripts/lib/claude-kimi-secret.py)
      set_group claude-kimi "claude-kimi"
      ;;
    scripts/lib/failed_attempt_handoff.py)
      set_group failed-handoff "failed-handoff model-fallback"
      ;;
    scripts/dispatch-lease.sh|scripts/lib/dispatch-leases.sh)
      set_group dispatch-leases "dispatch-leases preflight factory-scripts hermes-contract"
      ;;
    scripts/reorder-test-fixes.sh|scripts/lib/reorder_test_fixes.py)
      set_group reorder-test-fixes "reorder-test-fixes hermes-contract"
      ;;
    scripts/ticket-state.sh|scripts/ticket-attest.py|scripts/ticket-attest.sh)
      set_group ticket-evidence "ticket-state ticket-attest hermes-contract"
      ;;
    scripts/legacy-closeout.py|scripts/lib/legacy-closeout.py|\
      scripts/terminal-backfill.py|scripts/lib/terminal-backfill.py)
      set_group migrations "effective-ticket legacy-closeout terminal-backfill hermes-contract"
      ;;
    conformance/app/package.json)
      full "dependency manifest"
      ;;
    conformance/app/*)
      set_group conformance "conformance"
      ;;
    *)
      full "unknown or shared path"
      ;;
  esac
done < <(git -C "$ROOT" diff --name-status -z --no-renames "$MERGE_BASE" "$HEAD")

[[ -n "$GROUP" ]] || full "no recognized component"
printf 'targeted|%s|%s ci-scope immutability artifact-policy\n' "$GROUP" "$SUITES"
