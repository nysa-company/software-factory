#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
git -C "$ROOT" check-ignore -q --no-index conformance/factory/linear-map.json || {
  echo "FAIL: Linear operator overlay is not ignored" >&2
  exit 1
}
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
cat > "$REMOTE/hooks/pre-receive" <<EOF
#!/usr/bin/env bash
python3 - '$PRODUCT/factory/linear-map.json' '$TMP/volatile-refreshed' <<'PY'
import json, sys
from pathlib import Path
path, marker = map(Path, sys.argv[1:])
data = json.loads(path.read_text())
operator = data["tickets"]["T-700"].get("operator")
if operator:
    operator["observed_at"] = "2026-07-15T00:01:00Z"
    operator["linear_updated_at"] = "2026-07-15T00:01:00Z"
    path.write_text(json.dumps(data, sort_keys=True) + "\n")
    marker.touch()
PY
EOF
chmod +x "$REMOTE/hooks/pre-receive"

ticket_state() {
  FACTORY_HERMES_CONTRACT_VERSION="${TEST_CONTRACT:-}" \
    FACTORY_CERTIFIED_PRODUCT_ORIGIN="$REMOTE" FACTORY_ROOT="$PRODUCT" \
    "$ROOT/scripts/ticket-state.sh" "$@"
}

ticket_state \
  --ticket T-700 --workdir "$PRODUCT" --action materialize >/dev/null
[[ -f "$TMP/volatile-refreshed" ]]
grep -q '^State: Ready$' "$PRODUCT/factory/tickets/T-700.md"
grep -q '^Priority: high$' "$PRODUCT/factory/tickets/T-700.md"
python3 - "$PRODUCT/factory/linear-map.json" <<'PY'
import json, sys
assert "operator" not in json.load(open(sys.argv[1]))["tickets"]["T-700"]
PY

printf '{"tickets":{"T-700":{"operator":{"initiative":null}}}}\n' \
  > "$PRODUCT/factory/linear-map.json"
ticket_state --ticket T-700 --workdir "$PRODUCT" --action materialize >/dev/null
! grep -q '^Initiative:' "$PRODUCT/factory/tickets/T-700.md"
git --git-dir="$REMOTE" show \
  "refs/heads/ticket/T-700:factory/tickets/T-700.md" | \
  grep -qv '^Initiative:'
python3 - "$PRODUCT/factory/linear-map.json" <<'PY'
import json, sys
assert "operator" not in json.load(open(sys.argv[1]))["tickets"]["T-700"]
PY

printf '{"tickets":{"T-700":{"operator":{"state":"Building","state_base":"ready"}}}}\n' \
  > "$PRODUCT/factory/linear-map.json"
BEFORE="$(git -C "$PRODUCT" rev-parse HEAD)"
if ticket_state --ticket T-700 --workdir "$PRODUCT" --action materialize \
  >/dev/null 2>&1; then
  echo "FAIL: operator overlay materialized a factory-owned state" >&2
  exit 1
fi
[[ "$(git -C "$PRODUCT" rev-parse HEAD)" == "$BEFORE" ]]
[[ "$(git --git-dir="$REMOTE" rev-parse refs/heads/ticket/T-700)" == "$BEFORE" ]]

printf '{"tickets":{"T-700":{"operator":{"priority":"low"}}}}\n' \
  > "$PRODUCT/factory/linear-map.json"
BEFORE="$(git -C "$PRODUCT" rev-parse HEAD)"
if ticket_state --ticket T-700 --workdir "$PRODUCT" --action transition \
  --state Planning >/dev/null 2>&1; then
  echo "FAIL: factory transition consumed pending operator fields" >&2
  exit 1
fi
[[ "$(git -C "$PRODUCT" rev-parse HEAD)" == "$BEFORE" ]]
[[ "$(git --git-dir="$REMOTE" rev-parse refs/heads/ticket/T-700)" == "$BEFORE" ]]
python3 - "$PRODUCT/factory/linear-map.json" <<'PY'
import json, sys
assert json.load(open(sys.argv[1]))["tickets"]["T-700"]["operator"] == {
    "priority": "low"
}
PY
ticket_state --ticket T-700 --workdir "$PRODUCT" --action materialize >/dev/null
grep -q '^Priority: low$' "$PRODUCT/factory/tickets/T-700.md"
python3 - "$PRODUCT/factory/linear-map.json" <<'PY'
import json, sys
assert "operator" not in json.load(open(sys.argv[1]))["tickets"]["T-700"]
PY

ticket_state \
  --ticket T-700 --workdir "$PRODUCT" --action transition --state Planning >/dev/null
grep -q '^State: Planning$' "$PRODUCT/factory/tickets/T-700.md"
TEST_CONTRACT=1.7.0 ticket_state --ticket T-700 --workdir "$PRODUCT" --action transition \
  --state Blocked-Escalated >/dev/null
grep -q '^State: Blocked-Escalated$' "$PRODUCT/factory/tickets/T-700.md"
grep -q '^Resume-State: Planning$' "$PRODUCT/factory/tickets/T-700.md"
printf '{"tickets":{"T-700":{"operator":{"state":"Planning","state_base":"blocked-escalated"}}}}\n' \
  > "$PRODUCT/factory/linear-map.json"
ticket_state --ticket T-700 --workdir "$PRODUCT" --action materialize >/dev/null
grep -q '^State: Planning$' "$PRODUCT/factory/tickets/T-700.md"
BEFORE="$(git -C "$PRODUCT" rev-parse HEAD)"
if ticket_state \
  --ticket T-700 --workdir "$PRODUCT" --action transition --state Review >/dev/null 2>&1; then
  echo "FAIL: illegal ticket transition was accepted" >&2
  exit 1
fi
[[ "$(git -C "$PRODUCT" rev-parse HEAD)" == "$BEFORE" ]]
[[ "$(git --git-dir="$REMOTE" rev-parse refs/heads/ticket/T-700)" == "$BEFORE" ]]

for state in Review Approved; do
  sed -E "s/^State: .*/State: $state/" "$PRODUCT/factory/tickets/T-700.md" > "$TMP/ticket"
  mv "$TMP/ticket" "$PRODUCT/factory/tickets/T-700.md"
  git -C "$PRODUCT" add factory/tickets/T-700.md
  git -C "$PRODUCT" -c user.name=test -c user.email=test@example.com \
    commit -qm "$state fixture"
  git -C "$PRODUCT" push -q "$REMOTE" HEAD:refs/heads/ticket/T-700
  BEFORE="$(git -C "$PRODUCT" rev-parse HEAD)"
  target="Awaiting Approval"
  [[ "$state" == "Review" ]] || target="Done"
  if ticket_state --ticket T-700 --workdir "$PRODUCT" --action transition \
    --state "$target" >/dev/null 2>&1; then
    echo "FAIL: evidence-sensitive $state -> $target transition was accepted" >&2
    exit 1
  fi
  grep -q "^State: $state$" "$PRODUCT/factory/tickets/T-700.md"
  [[ "$(git -C "$PRODUCT" rev-parse HEAD)" == "$BEFORE" ]]
  [[ "$(git --git-dir="$REMOTE" rev-parse refs/heads/ticket/T-700)" == "$BEFORE" ]]
done

sed -E 's/^State: .*/State: Review/' "$PRODUCT/factory/tickets/T-700.md" > "$TMP/ticket"
mv "$TMP/ticket" "$PRODUCT/factory/tickets/T-700.md"
git -C "$PRODUCT" add factory/tickets/T-700.md
git -C "$PRODUCT" -c user.name=test -c user.email=test@example.com \
  commit -qm "materialization evidence fixture"
git -C "$PRODUCT" push -q "$REMOTE" HEAD:refs/heads/ticket/T-700
BEFORE="$(git -C "$PRODUCT" rev-parse HEAD)"
for target in "Awaiting Approval" Done; do
  printf '{"tickets":{"T-700":{"operator":{"state":"%s"}}}}\n' "$target" \
    > "$PRODUCT/factory/linear-map.json"
  if ticket_state --ticket T-700 --workdir "$PRODUCT" --action materialize \
    >/dev/null 2>&1; then
    echo "FAIL: evidence-sensitive overlay state $target was materialized" >&2
    exit 1
  fi
  grep -q '^State: Review$' "$PRODUCT/factory/tickets/T-700.md"
  [[ "$(git -C "$PRODUCT" rev-parse HEAD)" == "$BEFORE" ]]
  [[ "$(git --git-dir="$REMOTE" rev-parse refs/heads/ticket/T-700)" == "$BEFORE" ]]
done

printf '{"tickets":{"T-700":{"operator":{"approval":"Linear"}}}}\n' \
  > "$PRODUCT/factory/linear-map.json"
if ticket_state --ticket T-700 --workdir "$PRODUCT" --action materialize \
  >/dev/null 2>&1; then
  echo "FAIL: approval-only overlay was materialized" >&2
  exit 1
fi
grep -q '^State: Review$' "$PRODUCT/factory/tickets/T-700.md"
! grep -q '^Operator-Approval:' "$PRODUCT/factory/tickets/T-700.md"
[[ "$(git -C "$PRODUCT" rev-parse HEAD)" == "$BEFORE" ]]
[[ "$(git --git-dir="$REMOTE" rev-parse refs/heads/ticket/T-700)" == "$BEFORE" ]]

printf '{"tickets":{"T-700":{"operator":{"state":"Approved","approval":"Linear"}}}}\n' \
  > "$PRODUCT/factory/linear-map.json"
if ticket_state --ticket T-700 --workdir "$PRODUCT" --action materialize \
  >/dev/null 2>&1; then
  echo "FAIL: Review -> Approved overlay was materialized" >&2
  exit 1
fi
grep -q '^State: Review$' "$PRODUCT/factory/tickets/T-700.md"
! grep -q '^Operator-Approval:' "$PRODUCT/factory/tickets/T-700.md"
[[ "$(git -C "$PRODUCT" rev-parse HEAD)" == "$BEFORE" ]]
[[ "$(git --git-dir="$REMOTE" rev-parse refs/heads/ticket/T-700)" == "$BEFORE" ]]

sed -E 's/^State: .*/State: Awaiting Approval/' \
  "$PRODUCT/factory/tickets/T-700.md" > "$TMP/ticket"
mv "$TMP/ticket" "$PRODUCT/factory/tickets/T-700.md"
git -C "$PRODUCT" add factory/tickets/T-700.md
git -C "$PRODUCT" -c user.name=test -c user.email=test@example.com \
  commit -qm "operator approval fixture"
git -C "$PRODUCT" push -q "$REMOTE" HEAD:refs/heads/ticket/T-700
BEFORE="$(git -C "$PRODUCT" rev-parse HEAD)"
printf '{"tickets":{"T-700":{"operator":{"priority":"normal"}}}}\n' \
  > "$PRODUCT/factory/linear-map.json"
if ticket_state --ticket T-700 --workdir "$PRODUCT" --action materialize \
  >/dev/null 2>&1; then
  echo "FAIL: already-Awaiting ticket accepted unrelated materialization" >&2
  exit 1
fi
grep -q '^State: Awaiting Approval$' "$PRODUCT/factory/tickets/T-700.md"
grep -q '^Priority: low$' "$PRODUCT/factory/tickets/T-700.md"
[[ "$(git -C "$PRODUCT" rev-parse HEAD)" == "$BEFORE" ]]
[[ "$(git --git-dir="$REMOTE" rev-parse refs/heads/ticket/T-700)" == "$BEFORE" ]]

printf '{"tickets":{"T-700":{"operator":{"state":"Approved","approval":"Linear"}}}}\n' \
  > "$PRODUCT/factory/linear-map.json"
if ticket_state --ticket T-700 --workdir "$PRODUCT" --action materialize \
  >/dev/null 2>&1; then
  echo "FAIL: approval was materialized without a dedicated bundle attestation" >&2
  exit 1
fi
grep -q '^State: Awaiting Approval$' "$PRODUCT/factory/tickets/T-700.md"
! grep -q '^Operator-Approval:' "$PRODUCT/factory/tickets/T-700.md"
[[ "$(git -C "$PRODUCT" rev-parse HEAD)" == "$BEFORE" ]]
[[ "$(git --git-dir="$REMOTE" rev-parse refs/heads/ticket/T-700)" == "$BEFORE" ]]
python3 - "$PRODUCT/factory/linear-map.json" <<'PY'
import json, sys

operator = json.load(open(sys.argv[1]))["tickets"]["T-700"].get("operator")
assert operator == {"state": "Approved", "approval": "Linear"}
PY

grep -vE '^(Operator-Approval|Resume-State):' \
  "$PRODUCT/factory/tickets/T-700.md" | \
  sed 's/^State: Awaiting Approval$/State: Blocked-Escalated/' > "$TMP/ticket"
printf 'Resume-State: Building\n' >> "$TMP/ticket"
mv "$TMP/ticket" "$PRODUCT/factory/tickets/T-700.md"
git -C "$PRODUCT" add factory/tickets/T-700.md
git -C "$PRODUCT" -c user.name=test -c user.email=test@example.com \
  commit -qm "blocked resume fixture"
git -C "$PRODUCT" push -q "$REMOTE" HEAD:refs/heads/ticket/T-700
printf '{"tickets":{"T-700":{"operator":{"state":"Planning","state_base":"blocked-escalated"}}}}\n' \
  > "$PRODUCT/factory/linear-map.json"
BEFORE="$(git -C "$PRODUCT" rev-parse HEAD)"
if ticket_state --ticket T-700 --workdir "$PRODUCT" --action materialize \
  >/dev/null 2>&1; then
  echo "FAIL: mismatched blocked resume was materialized" >&2
  exit 1
fi
[[ "$(git -C "$PRODUCT" rev-parse HEAD)" == "$BEFORE" ]]
[[ "$(git --git-dir="$REMOTE" rev-parse refs/heads/ticket/T-700)" == "$BEFORE" ]]
printf '{"tickets":{"T-700":{"operator":{"state":"Building","state_base":"blocked-escalated"}}}}\n' \
  > "$PRODUCT/factory/linear-map.json"
ticket_state --ticket T-700 --workdir "$PRODUCT" --action materialize >/dev/null
grep -q '^State: Building$' "$PRODUCT/factory/tickets/T-700.md"
python3 - "$PRODUCT/factory/linear-map.json" <<'PY'
import json, sys
assert "operator" not in json.load(open(sys.argv[1]))["tickets"]["T-700"]
PY

printf '{"tickets":{"T-700":{}}}\n' > "$PRODUCT/factory/linear-map.json"

DECOY="$TMP/decoy.git"
git init --bare -q "$DECOY"
sed -E 's/^State: .*/State: Planning/' "$PRODUCT/factory/tickets/T-700.md" > "$TMP/ticket"
mv "$TMP/ticket" "$PRODUCT/factory/tickets/T-700.md"
git -C "$PRODUCT" add factory/tickets/T-700.md
git -C "$PRODUCT" -c user.name=test -c user.email=test@example.com \
  commit -qm "remote drift fixture"
git -C "$PRODUCT" push -q "$REMOTE" HEAD:refs/heads/ticket/T-700
git -C "$PRODUCT" config remote.origin.pushurl "$DECOY"
BEFORE="$(git -C "$PRODUCT" rev-parse HEAD)"
if ticket_state --ticket T-700 --workdir "$PRODUCT" --action transition \
  --state Building >/dev/null 2>&1; then
  echo "FAIL: drifted product push destination was accepted" >&2
  exit 1
fi
grep -q '^State: Planning$' "$PRODUCT/factory/tickets/T-700.md"
[[ "$(git -C "$PRODUCT" rev-parse HEAD)" == "$BEFORE" ]]
[[ "$(git --git-dir="$REMOTE" rev-parse refs/heads/ticket/T-700)" == "$BEFORE" ]]
! git --git-dir="$DECOY" show-ref --verify --quiet refs/heads/ticket/T-700

git -C "$PRODUCT" config --unset-all remote.origin.pushurl
git -C "$PRODUCT" config --add remote.origin.pushurl "$REMOTE"
git -C "$PRODUCT" config --add remote.origin.pushurl "$DECOY"
if ticket_state --ticket T-700 --workdir "$PRODUCT" --action transition \
  --state Building >/dev/null 2>&1; then
  echo "FAIL: multiple product push destinations were accepted" >&2
  exit 1
fi
grep -q '^State: Planning$' "$PRODUCT/factory/tickets/T-700.md"
[[ "$(git -C "$PRODUCT" rev-parse HEAD)" == "$BEFORE" ]]
[[ "$(git --git-dir="$REMOTE" rev-parse refs/heads/ticket/T-700)" == "$BEFORE" ]]
! git --git-dir="$DECOY" show-ref --verify --quiet refs/heads/ticket/T-700

# Tracking projection is compare-and-swap: a concurrent ref update wins rather
# than being silently overwritten after the verified remote push.
# shellcheck disable=SC1091
source "$ROOT/scripts/lib/product-remote.sh"
CAS_BRANCH="ticket/T-700"
CAS_OLD="$(factory_remote_tracking_tip "$PRODUCT" "$CAS_BRANCH")"
CAS_RACE="$(git -C "$PRODUCT" rev-parse HEAD)"
CAS_NEW="$(git -C "$PRODUCT" rev-parse HEAD^)"
[[ -n "$CAS_OLD" && "$CAS_RACE" != "$CAS_NEW" ]]
git -C "$PRODUCT" update-ref "refs/remotes/origin/$CAS_BRANCH" \
  "$CAS_RACE" "$CAS_OLD"
if factory_update_tracking_ref "$PRODUCT" "$CAS_BRANCH" "$CAS_NEW" "$CAS_OLD" \
  >/dev/null 2>&1; then
  echo "FAIL: stale tracking compare-and-swap overwrote a concurrent update" >&2
  exit 1
fi
[[ "$(factory_remote_tracking_tip "$PRODUCT" "$CAS_BRANCH")" == "$CAS_RACE" ]]
factory_update_tracking_ref "$PRODUCT" "$CAS_BRANCH" "$CAS_NEW" "$CAS_RACE"
[[ "$(factory_remote_tracking_tip "$PRODUCT" "$CAS_BRANCH")" == "$CAS_NEW" ]]

# Qualification contract blockers return to Backlog only with a protected
# qualification manifest and a matching authenticated role result.
git -C "$PRODUCT" config --unset-all remote.origin.pushurl
git -C "$PRODUCT" config --add remote.origin.pushurl "$REMOTE"
git -C "$PRODUCT" fetch -q "$REMOTE" \
  refs/heads/ticket/T-700:refs/remotes/origin/ticket/T-700
printf 'factory/runs/\n' >> "$PRODUCT/.gitignore"
cat > "$PRODUCT/factory/QUALIFICATION.json" <<'EOF'
{"factory_sha":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","final_capacity":4,"generation":1,"initial_capacity":3,"ramp_after_done":3,"schema":"nysa.software-factory.qualification/v1","target_done":10,"tickets":["T-700"]}
EOF
git -C "$PRODUCT" add .gitignore factory/QUALIFICATION.json
git -C "$PRODUCT" -c user.name=test -c user.email=test@example.com \
  commit -qm "qualification fixture"
git -C "$PRODUCT" push -q "$REMOTE" HEAD:refs/heads/main
git -C "$PRODUCT" fetch -q "$REMOTE" main:refs/remotes/origin/main
grep -v '^Resume-State:' "$PRODUCT/factory/tickets/T-700.md" |
  sed -E 's/^State: .*/State: Building/' > "$TMP/ticket"
printf '\nROLE-ESCALATE: CONTRACT-BLOCKED\n' >> "$TMP/ticket"
mv "$TMP/ticket" "$PRODUCT/factory/tickets/T-700.md"
git -C "$PRODUCT" add factory/tickets/T-700.md
git -C "$PRODUCT" -c user.name=test -c user.email=test@example.com \
  commit -qm "contract blocker fixture"
mkdir -p "$PRODUCT/factory/runs"
printf '%s\n' \
  'run_id=blocked-run' 'ticket=T-700' 'role=builder' \
  'contract_version=1.7.0' 'phase=completed' \
  'accounting_state=abandoned_conservative' \
  'reserved_usd=10.00' 'effective_cost=10.00' \
  'cost_basis=conservative_reservation' \
  'exit_status=12' 'role_exit=role_exit_contract_blocked' \
  'kit_sha=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' \
  'started_at=2026-07-25T00:00:00Z' \
  > "$PRODUCT/factory/runs/blocked.meta"
TEST_CONTRACT=1.7.0 ticket_state --ticket T-700 --workdir "$PRODUCT" \
  --action qualification-backlog --role builder >/dev/null
grep -q '^State: Backlog$' "$PRODUCT/factory/tickets/T-700.md"
git --git-dir="$REMOTE" show refs/heads/ticket/T-700:factory/tickets/T-700.md |
  grep -q '^State: Backlog$'

# Indented Markdown log markers are the normal role output and carry the same
# authenticated semantic failure evidence.
sed -E 's/^State: .*/State: Planning/' \
  "$PRODUCT/factory/tickets/T-700.md" > "$TMP/ticket"
printf '\n  SPEC-LINT: FAIL — missing acceptance case\n' >> "$TMP/ticket"
mv "$TMP/ticket" "$PRODUCT/factory/tickets/T-700.md"
git -C "$PRODUCT" add factory/tickets/T-700.md
git -C "$PRODUCT" -c user.name=test -c user.email=test@example.com \
  commit -qm "indented spec failure fixture"
TEST_CONTRACT=1.7.0 ticket_state --ticket T-700 --workdir "$PRODUCT" \
  --action qualification-backlog >/dev/null
grep -q '^State: Backlog$' "$PRODUCT/factory/tickets/T-700.md"

echo "PASS: ticket-state binds pushes, qualification returns, CAS tracking, and evidence-sensitive transitions"
