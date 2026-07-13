#!/usr/bin/env bash
# reorder-test-fixes.sh — move test commits that landed after implementation
# started back to immediately before the first implementation commit, so the
# branch satisfies ci/test-immutability-check.sh's rule 2 (order).
#
# This is a thin wrapper: all logic lives in lib/reorder_test_fixes.py
# (python3 stdlib only) because the commit classification, cherry-pick loop,
# and conflict-resolution bookkeeping are easier to get right — and easier to
# unit-reason-about — in Python than in bash 3.2 (no associative arrays).
#
# Usage:
#   reorder-test-fixes.sh --base <ref> [--test-paths "<pathspecs>"] [--exempt-paths "<pathspecs>"]
#
# See lib/reorder_test_fixes.py for full behavior and safety guarantees.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if ! command -v python3 >/dev/null 2>&1; then
  echo "reorder-test-fixes.sh: python3 is required but was not found on PATH" >&2
  exit 1
fi

exec python3 "$DIR/lib/reorder_test_fixes.py" "$@"
