#!/usr/bin/env bash
# spend-rollup.sh — daily cost summary from the ledger, printed to stdout.
# Schedule via launchd as com.factory.spend-rollup.
#
# Env: FACTORY_LEDGER (optional effective-ledger override)
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
