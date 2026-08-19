#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
git -C "$ROOT" check-ignore -q --no-index conformance/factory/operator-map.json || {
  echo "FAIL: operator overlay is not ignored" >&2
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
cat > "$PRODUCT/factory/operator-map.json" <<'EOF'
{"tickets":{"T-700":{"operator":{"state":"Ready","priority":"high","observed_at":"2026-07-15T00:00:00Z"}}}}
EOF
printf 'factory/operator-map.json\nfactory/.operator-map.lock\nfactory/.operator-clears/\n' > "$PRODUCT/.gitignore"
git -C "$PRODUCT" init -q -b ticket/T-700
git -C "$PRODUCT" add .gitignore factory
git -C "$PRODUCT" -c user.name=test -c user.email=test@example.com commit -qm fixture
git init --bare -q "$REMOTE"
git -C "$PRODUCT" remote add origin "$REMOTE"
git -C "$PRODUCT" push -q -u origin ticket/T-700
cat > "$REMOTE/hooks/pre-receive" <<EOF
#!/usr/bin/env bash
if [[ -f '$TMP/reject-next-push' ]]; then
  rm -f '$TMP/reject-next-push'
  exit 1
fi
python3 - '$PRODUCT/factory/operator-map.json' '$TMP/volatile-refreshed' <<'PY'
import json, sys
from pathlib import Path
path, marker = map(Path, sys.argv[1:])
data = json.loads(path.read_text())
operator = data["tickets"]["T-700"].get("operator")
if operator:
    operator["observed_at"] = "2026-07-15T00:01:00Z"
    path.write_text(json.dumps(data, sort_keys=True) + "\n")
    marker.touch()
PY
EOF
chmod +x "$REMOTE/hooks/pre-receive"

ticket_state() {
  FACTORY_CONTRACT_VERSION="${TEST_CONTRACT:-}" \
    FACTORY_CERTIFIED_PRODUCT_ORIGIN="$REMOTE" FACTORY_ROOT="$PRODUCT" \
    "$ROOT/scripts/ticket-state.sh" "$@"
}

ticket_state \
  --ticket T-700 --workdir "$PRODUCT" --action materialize >/dev/null
[[ -f "$TMP/volatile-refreshed" ]]
grep -q '^State: Ready$' "$PRODUCT/factory/tickets/T-700.md"
grep -q '^Priority: high$' "$PRODUCT/factory/tickets/T-700.md"
python3 - "$PRODUCT/factory/operator-map.json" <<'PY'
import json, sys
assert "operator" not in json.load(open(sys.argv[1]))["tickets"]["T-700"]
PY

printf '{"tickets":{"T-700":{"operator":{"initiative":null}}}}\n' \
  > "$PRODUCT/factory/operator-map.json"
ticket_state --ticket T-700 --workdir "$PRODUCT" --action materialize >/dev/null
! grep -q '^Initiative:' "$PRODUCT/factory/tickets/T-700.md"
git --git-dir="$REMOTE" show \
  "refs/heads/ticket/T-700:factory/tickets/T-700.md" | \
  grep -qv '^Initiative:'
python3 - "$PRODUCT/factory/operator-map.json" <<'PY'
import json, sys
assert "operator" not in json.load(open(sys.argv[1]))["tickets"]["T-700"]
PY

printf '{"tickets":{"T-700":{"operator":{"state":"Building","state_base":"ready"}}}}\n' \
  > "$PRODUCT/factory/operator-map.json"
BEFORE="$(git -C "$PRODUCT" rev-parse HEAD)"
if ticket_state --ticket T-700 --workdir "$PRODUCT" --action materialize \
  >/dev/null 2>&1; then
  echo "FAIL: operator overlay materialized a factory-owned state" >&2
  exit 1
fi
[[ "$(git -C "$PRODUCT" rev-parse HEAD)" == "$BEFORE" ]]
[[ "$(git --git-dir="$REMOTE" rev-parse refs/heads/ticket/T-700)" == "$BEFORE" ]]

printf '{"tickets":{"T-700":{"operator":{"priority":"low"}}}}\n' \
  > "$PRODUCT/factory/operator-map.json"
BEFORE="$(git -C "$PRODUCT" rev-parse HEAD)"
if ticket_state --ticket T-700 --workdir "$PRODUCT" --action transition \
  --state Planning >/dev/null 2>&1; then
  echo "FAIL: factory transition consumed pending operator fields" >&2
  exit 1
fi
[[ "$(git -C "$PRODUCT" rev-parse HEAD)" == "$BEFORE" ]]
[[ "$(git --git-dir="$REMOTE" rev-parse refs/heads/ticket/T-700)" == "$BEFORE" ]]
python3 - "$PRODUCT/factory/operator-map.json" <<'PY'
import json, sys
assert json.load(open(sys.argv[1]))["tickets"]["T-700"]["operator"] == {
    "priority": "low"
}
PY
ticket_state --ticket T-700 --workdir "$PRODUCT" --action materialize >/dev/null
grep -q '^Priority: low$' "$PRODUCT/factory/tickets/T-700.md"
python3 - "$PRODUCT/factory/operator-map.json" <<'PY'
import json, sys
assert "operator" not in json.load(open(sys.argv[1]))["tickets"]["T-700"]
PY

touch "$TMP/reject-next-push"
ticket_state \
  --ticket T-700 --workdir "$PRODUCT" --action transition --state Planning >/dev/null
[[ ! -e "$TMP/reject-next-push" ]]
grep -q '^State: Planning$' "$PRODUCT/factory/tickets/T-700.md"
# Keep the integration boundary on the active contract; the pure policy matrix
# separately exercises every Resume-State contract retained by the writer.
TEST_CONTRACT=2.0.0 ticket_state --ticket T-700 --workdir "$PRODUCT" --action transition \
  --state Blocked-Escalated >/dev/null
grep -q '^State: Blocked-Escalated$' "$PRODUCT/factory/tickets/T-700.md"
grep -q '^Resume-State: Planning$' "$PRODUCT/factory/tickets/T-700.md"
printf '{"tickets":{"T-700":{"operator":{"state":"Planning","state_base":"blocked-escalated"}}}}\n' \
  > "$PRODUCT/factory/operator-map.json"
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
    > "$PRODUCT/factory/operator-map.json"
  if ticket_state --ticket T-700 --workdir "$PRODUCT" --action materialize \
    >/dev/null 2>&1; then
    echo "FAIL: evidence-sensitive overlay state $target was materialized" >&2
    exit 1
  fi
  grep -q '^State: Review$' "$PRODUCT/factory/tickets/T-700.md"
  [[ "$(git -C "$PRODUCT" rev-parse HEAD)" == "$BEFORE" ]]
  [[ "$(git --git-dir="$REMOTE" rev-parse refs/heads/ticket/T-700)" == "$BEFORE" ]]
done

printf '{"tickets":{"T-700":{"operator":{"approval":"Receipt"}}}}\n' \
  > "$PRODUCT/factory/operator-map.json"
if ticket_state --ticket T-700 --workdir "$PRODUCT" --action materialize \
  >/dev/null 2>&1; then
  echo "FAIL: approval-only overlay was materialized" >&2
  exit 1
fi
grep -q '^State: Review$' "$PRODUCT/factory/tickets/T-700.md"
! grep -q '^Operator-Approval:' "$PRODUCT/factory/tickets/T-700.md"
[[ "$(git -C "$PRODUCT" rev-parse HEAD)" == "$BEFORE" ]]
[[ "$(git --git-dir="$REMOTE" rev-parse refs/heads/ticket/T-700)" == "$BEFORE" ]]

printf '{"tickets":{"T-700":{"operator":{"state":"Approved","approval":"Receipt"}}}}\n' \
  > "$PRODUCT/factory/operator-map.json"
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
  > "$PRODUCT/factory/operator-map.json"
if ticket_state --ticket T-700 --workdir "$PRODUCT" --action materialize \
  >/dev/null 2>&1; then
  echo "FAIL: already-Awaiting ticket accepted unrelated materialization" >&2
  exit 1
fi
grep -q '^State: Awaiting Approval$' "$PRODUCT/factory/tickets/T-700.md"
grep -q '^Priority: low$' "$PRODUCT/factory/tickets/T-700.md"
[[ "$(git -C "$PRODUCT" rev-parse HEAD)" == "$BEFORE" ]]
[[ "$(git --git-dir="$REMOTE" rev-parse refs/heads/ticket/T-700)" == "$BEFORE" ]]

printf '{"tickets":{"T-700":{"operator":{"state":"Approved","approval":"Receipt"}}}}\n' \
  > "$PRODUCT/factory/operator-map.json"
if ticket_state --ticket T-700 --workdir "$PRODUCT" --action materialize \
  >/dev/null 2>&1; then
  echo "FAIL: approval was materialized without a dedicated bundle attestation" >&2
  exit 1
fi
grep -q '^State: Awaiting Approval$' "$PRODUCT/factory/tickets/T-700.md"
! grep -q '^Operator-Approval:' "$PRODUCT/factory/tickets/T-700.md"
[[ "$(git -C "$PRODUCT" rev-parse HEAD)" == "$BEFORE" ]]
[[ "$(git --git-dir="$REMOTE" rev-parse refs/heads/ticket/T-700)" == "$BEFORE" ]]
python3 - "$PRODUCT/factory/operator-map.json" <<'PY'
import json, sys

operator = json.load(open(sys.argv[1]))["tickets"]["T-700"].get("operator")
assert operator == {"state": "Approved", "approval": "Receipt"}
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
  > "$PRODUCT/factory/operator-map.json"
BEFORE="$(git -C "$PRODUCT" rev-parse HEAD)"
if ticket_state --ticket T-700 --workdir "$PRODUCT" --action materialize \
  >/dev/null 2>&1; then
  echo "FAIL: mismatched blocked resume was materialized" >&2
  exit 1
fi
[[ "$(git -C "$PRODUCT" rev-parse HEAD)" == "$BEFORE" ]]
[[ "$(git --git-dir="$REMOTE" rev-parse refs/heads/ticket/T-700)" == "$BEFORE" ]]
printf '{"tickets":{"T-700":{"operator":{"state":"Building","state_base":"blocked-escalated"}}}}\n' \
  > "$PRODUCT/factory/operator-map.json"
ticket_state --ticket T-700 --workdir "$PRODUCT" --action materialize >/dev/null
grep -q '^State: Building$' "$PRODUCT/factory/tickets/T-700.md"
python3 - "$PRODUCT/factory/operator-map.json" <<'PY'
import json, sys
assert "operator" not in json.load(open(sys.argv[1]))["tickets"]["T-700"]
PY

# A crash after receipt consumption but before map clear must replay exactly.
sed -E 's/^State: .*/State: Blocked-Escalated/' \
  "$PRODUCT/factory/tickets/T-700.md" > "$TMP/ticket"
mv "$TMP/ticket" "$PRODUCT/factory/tickets/T-700.md"
git -C "$PRODUCT" add factory/tickets/T-700.md
git -C "$PRODUCT" -c user.name=test -c user.email=test@example.com \
  commit -qm "blocked replay fixture"
git -C "$PRODUCT" push -q "$REMOTE" HEAD:refs/heads/ticket/T-700
STATE_DIR="$(cd "$TMP" && pwd -P)/controller"
mkdir -m 700 "$STATE_DIR"
BLOCKED_RECEIPT="$(printf 'b%.0s' {1..64})"
RECEIPT_JSON="$(python3 "$ROOT/scripts/lib/operator_receipt.py" \
  --state-dir "$STATE_DIR" issue --ticket T-700 --action resume \
  --payload "{\"blocked_receipt_sha256\":\"$BLOCKED_RECEIPT\",\"resume_stage\":\"Building\"}")"
RECEIPT_SHA="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["receipt_sha256"])' \
  <<<"$RECEIPT_JSON")"
python3 "$ROOT/scripts/lib/operator_receipt.py" --state-dir "$STATE_DIR" \
  consume --ticket T-700 --action resume \
  --payload "{\"blocked_receipt_sha256\":\"$BLOCKED_RECEIPT\",\"resume_stage\":\"Building\"}" \
  >/dev/null
python3 - "$PRODUCT/factory/operator-map.json" "$RECEIPT_JSON" <<'PY'
import json, sys
path = sys.argv[1]
receipt = json.loads(sys.argv[2])
json.dump({"tickets": {"T-700": {"operator": {
    "observed_at": receipt["issued_at"],
    "receipt_sha256": receipt["receipt_sha256"],
    "state": "Building",
    "state_base": "blocked-escalated",
}}}}, open(path, "w"))
PY
FACTORY_BLOCKED_RECEIPT="$BLOCKED_RECEIPT" \
FACTORY_CERTIFIED_PRODUCT_ORIGIN="$REMOTE" FACTORY_CONTROLLER_STATE_DIR="$STATE_DIR" \
FACTORY_TRANSITION_STATE_DIR="$STATE_DIR" \
FACTORY_KIT_TRUST_SCOPE=qualification-candidate FACTORY_OPERATOR_MAP="$PRODUCT/factory/operator-map.json" \
FACTORY_QUALIFICATION_MODE=isolated FACTORY_QUALIFICATION_REPLAY=1 \
FACTORY_RELEASE_CONTRACT_VERSION=2.0.0 FACTORY_ROOT="$PRODUCT" \
  "$ROOT/scripts/ticket-state.sh" --ticket T-700 --workdir "$PRODUCT" \
  --action materialize >/dev/null
grep -q '^State: Building$' "$PRODUCT/factory/tickets/T-700.md"
python3 - "$PRODUCT/factory/operator-map.json" "$STATE_DIR" "$RECEIPT_SHA" <<'PY'
import json, sys
assert "operator" not in json.load(open(sys.argv[1]))["tickets"]["T-700"]
path = next(__import__("pathlib").Path(sys.argv[2]).glob("operator-receipts/T-700/resume-*.json"))
receipt = json.load(open(path))
assert receipt["receipt_sha256"] == sys.argv[3] and receipt["consumed"] is True
PY

printf '{"tickets":{"T-700":{}}}\n' > "$PRODUCT/factory/operator-map.json"

# Reviewer reconciliation without a portable checkpoint must work under the
# host Bash, including macOS Bash 3.2 with nounset enabled.
printf 'factory/runs/\n' >> "$PRODUCT/.gitignore"
git -C "$PRODUCT" add .gitignore
git -C "$PRODUCT" -c user.name=test -c user.email=test@example.com \
  commit -qm "ignore run evidence fixture"
git -C "$PRODUCT" push -q "$REMOTE" HEAD:refs/heads/ticket/T-700
ticket_state --ticket T-700 --workdir "$PRODUCT" \
  --action transition --state Review >/dev/null
REVIEW_HEAD="$(git -C "$PRODUCT" rev-parse HEAD)"
mkdir -p "$PRODUCT/factory/runs"
printf '%s\n' 'APPROVE' > "$PRODUCT/factory/runs/reviewer.out"
REVIEW_DIGEST="$(shasum -a 256 "$PRODUCT/factory/runs/reviewer.out" | awk '{print $1}')"
printf '%s\n' \
  'run_id=reviewer' 'ticket=T-700' 'role=reviewer' 'adapter=codex' \
  'contract_version=2.0.0' 'phase=completed' 'accounting_state=completed' \
  'exit_status=0' 'role_exit=ok' "role_head_before=$REVIEW_HEAD" \
  "role_remote_before=$REVIEW_HEAD" "output_sha256=$REVIEW_DIGEST" \
  'started_at=2026-07-27T00:00:00Z' \
  > "$PRODUCT/factory/runs/reviewer.meta"
TEST_CONTRACT=2.0.0 ticket_state --ticket T-700 --workdir "$PRODUCT" \
  --action reviewer-reconcile >/dev/null
grep -qx 'reviewer round 1: APPROVE' "$PRODUCT/factory/tickets/T-700.md"
QUALIFICATION_REVIEW_BASE="$(git -C "$PRODUCT" rev-parse HEAD)"
rm "$PRODUCT/factory/runs/reviewer.meta" "$PRODUCT/factory/runs/reviewer.out"

# The Reviewer back-edge is legal only through the shared action whitelist.
REVIEW_HEAD="$(git -C "$PRODUCT" rev-parse HEAD)"
printf '%s\n' 'REQUEST CHANGES' 'FIX-OWNER: builder' \
  > "$PRODUCT/factory/runs/reviewer-2.out"
REVIEW_DIGEST="$(shasum -a 256 "$PRODUCT/factory/runs/reviewer-2.out" | awk '{print $1}')"
printf '%s\n' \
  'run_id=reviewer-2' 'ticket=T-700' 'role=reviewer' 'adapter=codex' \
  'contract_version=2.0.0' 'phase=completed' 'accounting_state=completed' \
  'exit_status=0' 'role_exit=ok' "role_head_before=$REVIEW_HEAD" \
  "role_remote_before=$REVIEW_HEAD" "output_sha256=$REVIEW_DIGEST" \
  'started_at=2026-07-27T00:01:00Z' \
  > "$PRODUCT/factory/runs/reviewer-2.meta"
FACTORY_KIT_TRUST_SCOPE=qualification-candidate \
FACTORY_QUALIFICATION_PRODUCT_SHA="$QUALIFICATION_REVIEW_BASE" \
  TEST_CONTRACT=2.0.0 ticket_state --ticket T-700 --workdir "$PRODUCT" \
  --action reviewer-reconcile >/dev/null
grep -q '^State: Building$' "$PRODUCT/factory/tickets/T-700.md"
grep -qx 'reviewer round 1: APPROVE' \
  "$PRODUCT/factory/tickets/T-700.md"
grep -qx 'reviewer round 2: REQUEST CHANGES' \
  "$PRODUCT/factory/tickets/T-700.md"

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
# A third value must fail and remain unchanged.
if factory_update_tracking_ref "$PRODUCT" "$CAS_BRANCH" "$CAS_NEW" "$CAS_OLD" \
  >/dev/null 2>&1; then
  echo "FAIL: stale tracking compare-and-swap overwrote a concurrent update" >&2
  exit 1
fi
[[ "$(factory_remote_tracking_tip "$PRODUCT" "$CAS_BRANCH")" == "$CAS_RACE" ]]
# The expected old value may advance normally.
factory_update_tracking_ref "$PRODUCT" "$CAS_BRANCH" "$CAS_NEW" "$CAS_RACE"
[[ "$(factory_remote_tracking_tip "$PRODUCT" "$CAS_BRANCH")" == "$CAS_NEW" ]]
# A concurrent fetch may already have installed the exact desired SHA.
factory_update_tracking_ref "$PRODUCT" "$CAS_BRANCH" "$CAS_NEW" "$CAS_RACE"
[[ "$(factory_remote_tracking_tip "$PRODUCT" "$CAS_BRANCH")" == "$CAS_NEW" ]]
# An unexpectedly missing tracking ref must fail closed and remain absent.
git -C "$PRODUCT" update-ref -d "refs/remotes/origin/$CAS_BRANCH" "$CAS_NEW"
if factory_update_tracking_ref "$PRODUCT" "$CAS_BRANCH" "$CAS_NEW" "$CAS_NEW" \
  >/dev/null 2>&1; then
  echo "FAIL: unexpectedly missing tracking ref was recreated" >&2
  exit 1
fi
[[ -z "$(factory_remote_tracking_tip "$PRODUCT" "$CAS_BRANCH")" ]]
# An explicitly expected absence may initialize the tracking ref.
if ! factory_update_tracking_ref "$PRODUCT" "$CAS_BRANCH" "$CAS_NEW" ""; then
  echo "FAIL: expected-absent tracking ref was not initialized" >&2
  exit 1
fi
[[ "$(factory_remote_tracking_tip "$PRODUCT" "$CAS_BRANCH")" == "$CAS_NEW" ]]

# Qualification contract blockers return to Backlog only with a protected
# qualification manifest and a matching authenticated role result.
git -C "$PRODUCT" config --unset-all remote.origin.pushurl
git -C "$PRODUCT" config --add remote.origin.pushurl "$REMOTE"
git -C "$PRODUCT" fetch -q "$REMOTE" \
  refs/heads/ticket/T-700:refs/remotes/origin/ticket/T-700
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
PINNED_KIT_SHA="$(git -C "$ROOT" rev-parse HEAD)"
printf '%s\n' \
  'run_id=blocked-run' 'ticket=T-700' 'role=builder' \
  'contract_version=2.0.0' 'phase=completed' \
  'accounting_state=abandoned_conservative' \
  'reserved_usd=10.00' 'effective_cost=10.00' \
  'cost_basis=conservative_reservation' \
  'exit_status=12' 'role_exit=role_exit_contract_blocked' \
  'kit_sha=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb' \
  'started_at=2026-07-25T00:00:00Z' \
  > "$PRODUCT/factory/runs/blocked.meta"
if TEST_CONTRACT=2.0.0 ticket_state --ticket T-700 --workdir "$PRODUCT" \
    --action qualification-backlog --role builder >/dev/null 2>&1; then
  echo "FAIL: qualification accepted a role from an unpinned Factory SHA" >&2
  exit 1
fi
sed -i.bak "s/^kit_sha=.*/kit_sha=$PINNED_KIT_SHA/" \
  "$PRODUCT/factory/runs/blocked.meta"
rm "$PRODUCT/factory/runs/blocked.meta.bak"
TEST_CONTRACT=2.0.0 ticket_state --ticket T-700 --workdir "$PRODUCT" \
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
TEST_CONTRACT=2.0.0 ticket_state --ticket T-700 --workdir "$PRODUCT" \
  --action qualification-backlog >/dev/null
grep -q '^State: Backlog$' "$PRODUCT/factory/tickets/T-700.md"

# A protected historical spec failure is not a failure in this qualification
# epoch. The action requires a newly appended failure after the sealed base.
QUALIFICATION_FAILURE_BASE="$(git -C "$PRODUCT" rev-parse HEAD)"
sed -E 's/^State: .*/State: Planning/' \
  "$PRODUCT/factory/tickets/T-700.md" > "$TMP/ticket"
mv "$TMP/ticket" "$PRODUCT/factory/tickets/T-700.md"
git -C "$PRODUCT" add factory/tickets/T-700.md
git -C "$PRODUCT" -c user.name=test -c user.email=test@example.com \
  commit -qm "qualification historical failure fixture"
git -C "$PRODUCT" push -q "$REMOTE" HEAD:refs/heads/ticket/T-700
if FACTORY_KIT_TRUST_SCOPE=qualification-candidate \
    FACTORY_QUALIFICATION_PRODUCT_SHA="$QUALIFICATION_FAILURE_BASE" \
    TEST_CONTRACT=2.0.0 ticket_state --ticket T-700 --workdir "$PRODUCT" \
      --action qualification-backlog >/dev/null 2>&1; then
  echo "FAIL: qualification reused a protected historical spec failure" >&2
  exit 1
fi
printf '\nSPEC-LINT: FAIL — current qualification failure\n' \
  >> "$PRODUCT/factory/tickets/T-700.md"
git -C "$PRODUCT" add factory/tickets/T-700.md
git -C "$PRODUCT" -c user.name=test -c user.email=test@example.com \
  commit -qm "qualification current failure fixture"
git -C "$PRODUCT" push -q "$REMOTE" HEAD:refs/heads/ticket/T-700
FACTORY_KIT_TRUST_SCOPE=qualification-candidate \
FACTORY_QUALIFICATION_PRODUCT_SHA="$QUALIFICATION_FAILURE_BASE" \
  TEST_CONTRACT=2.0.0 ticket_state --ticket T-700 --workdir "$PRODUCT" \
    --action qualification-backlog >/dev/null
grep -q '^State: Backlog$' "$PRODUCT/factory/tickets/T-700.md"

echo "PASS: ticket-state binds pushes, qualification returns, CAS tracking, and evidence-sensitive transitions"
