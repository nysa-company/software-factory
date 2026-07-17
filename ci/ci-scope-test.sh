#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LIGHTWEIGHT="$ROOT/ci/lightweight-change.sh"
MACOS="$ROOT/ci/macos-required-change.sh"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/ci-scope.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT

new_repo() {
  local repo="$1"
  git init -q -b main "$repo"
  git -C "$repo" config user.name "CI scope test"
  git -C "$repo" config user.email "ci-scope@example.invalid"
}

commit_all() {
  local repo="$1" message="$2"
  git -C "$repo" add -A
  git -C "$repo" commit -qm "$message"
  git -C "$repo" rev-parse HEAD
}

expect_status() {
  local expected="$1" check="$2" repo="$3" base="$4" head="$5" label="$6"
  local status=0
  (cd "$repo" && bash "$check" "$base" "$head") || status=$?
  if [[ "$status" -ne "$expected" ]]; then
    printf 'FAIL: %s (expected %s, got %s)\n' "$label" "$expected" "$status" >&2
    exit 1
  fi
}

REPO="$TMP/main"
new_repo "$REPO"
mkdir -p "$REPO/docs" "$REPO/context" "$REPO/.github/workflows" \
  "$REPO/integrations/hermes" "$REPO/conformance/app" "$REPO/roles" "$REPO/scripts"
printf 'initial\n' > "$REPO/docs/guide.md"
printf 'initial\n' > "$REPO/README.md"
printf 'initial\n' > "$REPO/context/memory.md"
printf 'initial\n' > "$REPO/AGENTS.md"
printf '@AGENTS.md\n' > "$REPO/CLAUDE.md"
printf 'initial\n' > "$REPO/.github/pull_request_template.md"
printf 'initial\n' > "$REPO/integrations/hermes/CHANGELOG.md"
printf 'initial\n' > "$REPO/conformance/SHAKEDOWN-REPORT.md"
printf 'initial\n' > "$REPO/conformance/app/app.js"
printf 'initial\n' > "$REPO/roles/builder.md"
printf 'initial\n' > "$REPO/scripts/tool.py"
printf 'initial\n' > "$REPO/.github/workflows/ci.yml"
BASE="$(commit_all "$REPO" "initial")"

for path in docs/guide.md README.md context/memory.md AGENTS.md CLAUDE.md \
  .github/pull_request_template.md integrations/hermes/CHANGELOG.md \
  conformance/SHAKEDOWN-REPORT.md; do
  printf 'metadata update\n' >> "$REPO/$path"
done
LIGHT="$(commit_all "$REPO" "lightweight metadata")"
expect_status 0 "$LIGHTWEIGHT" "$REPO" "$BASE" "$LIGHT" "explicit lightweight allowlist"
expect_status 1 "$MACOS" "$REPO" "$BASE" "$LIGHT" "metadata does not need macOS"
expect_status 1 "$LIGHTWEIGHT" "$REPO" "$LIGHT" "$LIGHT" "empty diff runs Linux"
expect_status 0 "$MACOS" "$REPO" "$LIGHT" "$LIGHT" "empty diff runs macOS"
expect_status 1 "$LIGHTWEIGHT" "$REPO" missing "$LIGHT" "invalid base runs Linux"
expect_status 0 "$MACOS" "$REPO" missing "$LIGHT" "invalid base runs macOS"

printf 'prompt update\n' >> "$REPO/roles/builder.md"
LINUX="$(commit_all "$REPO" "runtime prompt")"
expect_status 1 "$LIGHTWEIGHT" "$REPO" "$LIGHT" "$LINUX" "runtime prompt runs Linux"
expect_status 1 "$MACOS" "$REPO" "$LIGHT" "$LINUX" "runtime prompt skips PR macOS"

printf 'app update\n' >> "$REPO/conformance/app/app.js"
APP="$(commit_all "$REPO" "app")"
expect_status 1 "$LIGHTWEIGHT" "$REPO" "$LINUX" "$APP" "application code runs Linux"
expect_status 1 "$MACOS" "$REPO" "$LINUX" "$APP" "application code skips PR macOS"

printf 'script update\n' >> "$REPO/scripts/tool.py"
SCRIPT="$(commit_all "$REPO" "script")"
expect_status 0 "$MACOS" "$REPO" "$APP" "$SCRIPT" "shared scripts run macOS"

printf 'workflow update\n' >> "$REPO/.github/workflows/ci.yml"
WORKFLOW="$(commit_all "$REPO" "workflow")"
expect_status 0 "$MACOS" "$REPO" "$SCRIPT" "$WORKFLOW" "workflows run macOS"

RENAME_REPO="$TMP/rename"
new_repo "$RENAME_REPO"
mkdir -p "$RENAME_REPO/docs"
printf 'executable\n' > "$RENAME_REPO/tool.sh"
RENAME_BASE="$(commit_all "$RENAME_REPO" "initial")"
git -C "$RENAME_REPO" mv tool.sh docs/tool.md
RENAME_HEAD="$(commit_all "$RENAME_REPO" "move code into docs")"
expect_status 1 "$LIGHTWEIGHT" "$RENAME_REPO" "$RENAME_BASE" "$RENAME_HEAD" \
  "code renamed into docs runs Linux"
expect_status 0 "$MACOS" "$RENAME_REPO" "$RENAME_BASE" "$RENAME_HEAD" \
  "shell renamed into docs runs macOS"

printf 'PASS: CI scope classification\n'
