#!/usr/bin/env bash
set -euo pipefail

echo "role_policy_violation: Planner contract repair may not run $(basename "$0") or product suites" >&2
exit 126
