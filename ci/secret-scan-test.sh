#!/usr/bin/env bash
# Exercise the real pinned scanner without committing a secret-like fixture.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/sf-secret-scan.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT
git -C "$TMP" init -q

if command -v gitleaks >/dev/null 2>&1; then
  GITLEAKS="$(command -v gitleaks)"
else
  GITLEAKS="$ROOT/.context/tools/gitleaks/8.30.1/gitleaks"
fi
[[ -x "$GITLEAKS" ]] || { echo "FAIL: pinned Gitleaks binary unavailable" >&2; exit 1; }

GITLEAKS_BIN="$GITLEAKS" "$ROOT/scripts/secret-scan" --root "$TMP" --mode directory >/dev/null

# The value is random, exists only in the temporary repo, and is never printed.
printf 'password = "%s"\n' "$(openssl rand -hex 32)" > "$TMP/intentional-fixture.txt"
if GITLEAKS_BIN="$GITLEAKS" "$ROOT/scripts/secret-scan" --root "$TMP" --mode directory >"$TMP/output" 2>&1; then
  echo "FAIL: scanner accepted intentional secret-like fixture" >&2
  exit 1
fi
if ! grep -qi 'redact\|redacted' "$TMP/output"; then
  echo "FAIL: scanner finding did not confirm redaction" >&2
  exit 1
fi

echo "PASS: secret scanner blocks an ephemeral fixture with redacted output"
