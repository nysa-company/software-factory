#!/usr/bin/env bash
# spend-rollup.sh — daily cost summary from the ledger, posted to Linear
# (or printed if no Linear config). Schedule via launchd as com.factory.spend-rollup.
#
# Env: FACTORY_LEDGER (optional effective-ledger override)
#      LINEAR_API_KEY + LINEAR_ROLLUP_ISSUE_ID — optional; posts a comment when both set.
set -euo pipefail

ROOT="${FACTORY_ROOT:-$PWD}"
LEDGER="${FACTORY_LEDGER:-$ROOT/factory/runtime-ledger.csv}"
DAY="${1:-$(date +%F)}"
if [[ -z "${FACTORY_LEDGER:-}" ]]; then
  KIT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
  python3 "$KIT_DIR/scripts/ledger-view.py" refresh --factory-root "$ROOT" >/dev/null || {
    echo "effective ledger could not be reduced" >&2
    exit 1
  }
fi
[[ -f "$LEDGER" ]] || { echo "no ledger at $LEDGER"; exit 0; }

SUMMARY="$(awk -F, -v d="$DAY" '
  NR>1 && $1==d { total+=$8; runs++; byrole[$4]+=$8; if ($9!=0) fails++ }
  END {
    printf "Factory spend %s: $%.2f across %d runs (%d failed)\n", d, total+0, runs+0, fails+0
    for (r in byrole) printf "  %s: $%.2f\n", r, byrole[r]
  }' "$LEDGER")"

echo "$SUMMARY"

if [[ -n "${LINEAR_API_KEY:-}" && -n "${LINEAR_ROLLUP_ISSUE_ID:-}" ]]; then
  BODY="$(printf '%s' "$SUMMARY" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')"
  curl -sS -X POST https://api.linear.app/graphql \
    -H "Authorization: $LINEAR_API_KEY" -H "Content-Type: application/json" \
    -d "{\"query\":\"mutation(\$input: CommentCreateInput!){commentCreate(input:\$input){success}}\",\"variables\":{\"input\":{\"issueId\":\"$LINEAR_ROLLUP_ISSUE_ID\",\"body\":$BODY}}}" \
    >/dev/null && echo "posted to Linear"
fi
