#!/usr/bin/env bash
# Exit 0 only when a non-empty diff is limited to inert repository metadata.
set -u

BASE="${1:-}"
HEAD="${2:-HEAD}"

[[ -n "$BASE" ]] || exit 1
git cat-file -e "$BASE^{commit}" 2>/dev/null || exit 1
git cat-file -e "$HEAD^{commit}" 2>/dev/null || exit 1

# Empty and ambiguous diffs run full CI. Disabling rename detection ensures
# moving executable content into an allowed path still exposes its deletion.
git diff --quiet --no-renames "$BASE" "$HEAD" && exit 1
git diff --quiet --no-renames "$BASE" "$HEAD" -- \
  . \
  ':(exclude)docs/**' \
  ':(exclude)README.md' \
  ':(exclude)TODOS.md' \
  ':(exclude)context/memory.md' \
  ':(exclude)AGENTS.md' \
  ':(exclude)CLAUDE.md' \
  ':(exclude).github/pull_request_template.md' \
  ':(exclude)integrations/hermes/CHANGELOG.md' \
  ':(exclude)conformance/SHAKEDOWN-REPORT.md'
