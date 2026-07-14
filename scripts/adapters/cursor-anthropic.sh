#!/usr/bin/env bash
# Family-typed Cursor fallback for checking roles.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FACTORY_CURSOR_FAMILY=anthropic exec "$SCRIPT_DIR/cursor-agent.sh" "$@"
