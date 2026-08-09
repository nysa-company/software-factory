#!/usr/bin/env bash
# Exact provider CLI version parsers shared by readiness and task adapters.

factory_codex_version() {
  [[ "$1" =~ ^(codex|codex-cli)[[:space:]]+([A-Za-z0-9][A-Za-z0-9._+-]{0,127})$ ]] ||
    return 1
  printf '%s\n' "${BASH_REMATCH[2]}"
}
