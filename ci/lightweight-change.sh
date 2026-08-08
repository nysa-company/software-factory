#!/usr/bin/env bash
# Exit 0 only when a non-empty diff is limited to inert repository metadata.
set -u

BASE="${1:-}"
HEAD="${2:-HEAD}"
ZERO_SHA=0000000000000000000000000000000000000000

[[ -n "$BASE" ]] || exit 1
if [[ "$BASE" == "$ZERO_SHA" ]]; then
  DIFF_BASE="$BASE"
else
  git cat-file -e "$BASE^{commit}" 2>/dev/null || exit 1
  DIFF_BASE="$BASE"
fi
git cat-file -e "$HEAD^{commit}" 2>/dev/null || exit 1

diff_quiet() {
  git diff --quiet --no-ext-diff --no-renames "$DIFF_BASE" "$HEAD" -- "$@"
}

validate_qualification_manifest() {
  local pin parser parser_directory release expected_release output status=0
  pin="$(git show "$HEAD:factory/KIT_PIN" 2>/dev/null)" || return 2
  [[ "${#pin}" -eq 40 && -z "${pin//[0-9a-f]/}" ]] || return 2
  parser="${FACTORY_QUALIFICATION_PARSER:-}"
  if [[ -z "$parser" ]]; then
    expected_release="$HOME/.factory/kits/releases/$pin"
    parser="$expected_release/scripts/lib/qualification_manifest.py"
  fi
  [[ "$parser" == /* && -f "$parser" && ! -L "$parser" ]] || return 2
  parser_directory="$(cd "$(dirname "$parser")" 2>/dev/null && pwd -P)" || return 2
  parser="$parser_directory/$(basename "$parser")"
  release="$(cd "$parser_directory/../.." 2>/dev/null && pwd -P)" || return 2
  [[ "$parser" == "$release/scripts/lib/qualification_manifest.py" ]] || return 2
  if [[ -n "${FACTORY_QUALIFICATION_PARSER:-}" ]]; then
    [[ "$(git -C "$release" rev-parse --show-toplevel 2>/dev/null)" == "$release" &&
       "$(git -C "$release" rev-parse HEAD 2>/dev/null)" == "$pin" &&
       -z "$(git -C "$release" status --porcelain --untracked-files=all 2>/dev/null)" ]] || return 2
  else
    expected_release="$(cd "$HOME/.factory/kits/releases" 2>/dev/null && pwd -P)/$pin" || return 2
    [[ "$release" == "$expected_release" ]] || return 2
  fi
  output="$(python3 "$parser" --product-root "$(git rev-parse --show-toplevel)" \
    --base "$BASE" --head "$HEAD" 2>/dev/null)" || status=$?
  [[ "$status" -eq 0 && "$output" == "QUALIFICATION MANIFEST VALIDATED" ]] || return 2
}

if [[ "$BASE" == "$ZERO_SHA" ]]; then
  if git cat-file -e "$HEAD:factory/QUALIFICATION.json" 2>/dev/null; then
    validate_qualification_manifest || {
      echo "qualification manifest validation failed" >&2
      exit 2
    }
  fi
  exit 1
fi

MANIFEST_STATUS=0
diff_quiet factory/QUALIFICATION.json || MANIFEST_STATUS=$?
case "$MANIFEST_STATUS" in
  0) ;;
  1)
    if git cat-file -e "$HEAD:factory/QUALIFICATION.json" 2>/dev/null; then
      validate_qualification_manifest || {
        echo "qualification manifest validation failed" >&2
        exit 2
      }
    fi
    ;;
  *) exit 2 ;;
esac

# Empty and ambiguous diffs run full CI. Disabling rename detection ensures
# moving executable content into an allowed path still exposes its deletion.
diff_quiet && exit 1
diff_quiet \
  . \
  ':(exclude)docs/**' \
  ':(exclude)README.md' \
  ':(exclude)TODOS.md' \
  ':(exclude)context/memory.md' \
  ':(exclude)AGENTS.md' \
  ':(exclude)CLAUDE.md' \
  ':(exclude).github/pull_request_template.md' \
  ':(exclude)integrations/hermes/CHANGELOG.md' \
  ':(exclude)conformance/SHAKEDOWN-REPORT.md' \
  ':(exclude)factory/QUALIFICATION.json'
