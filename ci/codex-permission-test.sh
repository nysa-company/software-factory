#!/usr/bin/env bash
# Probe the real Codex sandbox when the CLI is installed; CI may skip it.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if ! command -v codex >/dev/null 2>&1; then
  echo "SKIP: Codex CLI is not installed"
  exit 0
fi

TMP="$ROOT/.context/codex-permission-test.$$"
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP"
printf '%s\n' 'test-only-placeholder' > "$TMP/.env"
# shellcheck disable=SC1091
source "$ROOT/.codex/factory-permissions.env"

codex sandbox -P "$CODEX_FACTORY_PERMISSION_NAME" \
  -c "$CODEX_FACTORY_PERMISSION_CONFIG" -C "$TMP" -- sh -c \
  'if cat .env >/dev/null 2>&1; then exit 20; fi; printf "%s\n" ok > ordinary.txt'
[[ "$(cat "$TMP/ordinary.txt")" == "ok" ]]

codex sandbox -P "$CODEX_FACTORY_PERMISSION_NAME" \
  -c "$CODEX_FACTORY_PERMISSION_CONFIG" -C "$TMP" -- python3 -c \
  'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); s.close()'

echo "PASS: Codex profile denies secret reads and permits workspace writes/loopback binding"
