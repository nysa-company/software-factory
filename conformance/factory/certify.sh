#!/usr/bin/env bash
# Product-side certification entrypoint used by scripts/factory-kit.sh.
set -euo pipefail

PRODUCT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"

[[ -n "${FACTORY_KIT_SHA:-}" ]] || {
  echo "FACTORY_KIT_SHA is required" >&2
  exit 2
}
[[ -d "${FACTORY_KIT_RELEASE:-}" ]] || {
  echo "FACTORY_KIT_RELEASE is required" >&2
  exit 2
}

(cd "$PRODUCT_ROOT/app" && npm test)
