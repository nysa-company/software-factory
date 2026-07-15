#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/ticket-state-test.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT
PRODUCT="$TMP/product"
REMOTE="$TMP/product.git"
mkdir -p "$PRODUCT/factory/tickets"
cat > "$PRODUCT/factory/tickets/T-700.md" <<'EOF'
# T-700

State: Backlog
Initiative: I-001
Priority: none
EOF
cat > "$PRODUCT/factory/linear-map.json" <<'EOF'
{"tickets":{"T-700":{"operator":{"state":"Ready","priority":"high","observed_at":"2026-07-15T00:00:00Z"}}}}
EOF
printf 'factory/linear-map.json\nfactory/.linear-sync.lock\n' > "$PRODUCT/.gitignore"
git -C "$PRODUCT" init -q -b ticket/T-700
git -C "$PRODUCT" add .gitignore factory
git -C "$PRODUCT" -c user.name=test -c user.email=test@example.com commit -qm fixture
git init --bare -q "$REMOTE"
git -C "$PRODUCT" remote add origin "$REMOTE"
git -C "$PRODUCT" push -q -u origin ticket/T-700

FACTORY_ROOT="$PRODUCT" "$ROOT/scripts/ticket-state.sh" \
  --ticket T-700 --workdir "$PRODUCT" --action materialize >/dev/null
grep -q '^State: Ready$' "$PRODUCT/factory/tickets/T-700.md"
grep -q '^Priority: high$' "$PRODUCT/factory/tickets/T-700.md"
python3 - "$PRODUCT/factory/linear-map.json" <<'PY'
import json, sys
assert "operator" not in json.load(open(sys.argv[1]))["tickets"]["T-700"]
PY

FACTORY_ROOT="$PRODUCT" "$ROOT/scripts/ticket-state.sh" \
  --ticket T-700 --workdir "$PRODUCT" --action transition --state Planning >/dev/null
grep -q '^State: Planning$' "$PRODUCT/factory/tickets/T-700.md"
BEFORE="$(git -C "$PRODUCT" rev-parse HEAD)"
if FACTORY_ROOT="$PRODUCT" "$ROOT/scripts/ticket-state.sh" \
  --ticket T-700 --workdir "$PRODUCT" --action transition --state Review >/dev/null 2>&1; then
  echo "FAIL: illegal ticket transition was accepted" >&2
  exit 1
fi
[[ "$(git -C "$PRODUCT" rev-parse HEAD)" == "$BEFORE" ]]
[[ "$(git --git-dir="$REMOTE" rev-parse refs/heads/ticket/T-700)" == "$BEFORE" ]]
echo "PASS: ticket-state materializes operator fields and commits legal transitions"
