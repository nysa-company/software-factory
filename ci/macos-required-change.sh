#!/usr/bin/env bash
# Exit 0 when a diff needs the macOS system-Bash compatibility suite.
set -u

BASE="${1:-}"
HEAD="${2:-HEAD}"

# Missing, empty, or unreadable comparisons fail closed to macOS.
[[ -n "$BASE" ]] || exit 0
git cat-file -e "$BASE^{commit}" 2>/dev/null || exit 0
git cat-file -e "$HEAD^{commit}" 2>/dev/null || exit 0
git diff --quiet --no-renames "$BASE" "$HEAD"
case "$?" in
  0) exit 0 ;;
  1) ;;
  *) exit 0 ;;
esac

# Linux remains required for every non-lightweight change. macOS is added for
# shell, CI, deployment, launcher, and shared script surfaces.
git diff --quiet --no-renames "$BASE" "$HEAD" -- \
  '*.sh' \
  '.github/workflows/**' \
  'ci/**' \
  'deploy/**' \
  'scripts/**' \
  'integrations/hermes/bin/**'
case "$?" in
  0) exit 1 ;;
  *) exit 0 ;;
esac
