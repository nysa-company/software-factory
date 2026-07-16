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

ticket_state \
  --ticket T-700 --workdir "$PRODUCT" --action transition --state Planning >/dev/null
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
for operator in \
  '{"state":"Approved"}' \
  '{"state":"Approved","approval":"linear"}' \
  '{"state":"Approved","approval":"Manual"}'; do
  printf '{"tickets":{"T-700":{"operator":%s}}}\n' "$operator" \
    > "$PRODUCT/factory/linear-map.json"
  if ticket_state --ticket T-700 --workdir "$PRODUCT" --action materialize \
    >/dev/null 2>&1; then
    echo "FAIL: Approved overlay without exact Linear marker was materialized" >&2
    exit 1
  fi
  grep -q '^State: Awaiting Approval$' "$PRODUCT/factory/tickets/T-700.md"
  ! grep -q '^Operator-Approval:' "$PRODUCT/factory/tickets/T-700.md"
  [[ "$(git -C "$PRODUCT" rev-parse HEAD)" == "$BEFORE" ]]
  [[ "$(git --git-dir="$REMOTE" rev-parse refs/heads/ticket/T-700)" == "$BEFORE" ]]
done
printf 'Operator-Approval-Attestation:\n' >> \
  "$PRODUCT/factory/tickets/T-700.md"
git -C "$PRODUCT" add factory/tickets/T-700.md
git -C "$PRODUCT" -c user.name=test -c user.email=test@example.com \
  commit -qm "empty approval attestation fixture"
git -C "$PRODUCT" push -q "$REMOTE" HEAD:refs/heads/ticket/T-700
printf '{"tickets":{"T-700":{"operator":{"state":"Approved","approval":"Linear"}}}}\n' \
  > "$PRODUCT/factory/linear-map.json"
BEFORE="$(git -C "$PRODUCT" rev-parse HEAD)"
if ticket_state --ticket T-700 --workdir "$PRODUCT" --action materialize \
  >/dev/null 2>&1; then
  echo "FAIL: pre-existing empty approval attestation was accepted" >&2
  exit 1
fi
[[ "$(git -C "$PRODUCT" rev-parse HEAD)" == "$BEFORE" ]]
[[ "$(git --git-dir="$REMOTE" rev-parse refs/heads/ticket/T-700)" == "$BEFORE" ]]
grep -v '^Operator-Approval-Attestation:' \
  "$PRODUCT/factory/tickets/T-700.md" > "$TMP/ticket"
mv "$TMP/ticket" "$PRODUCT/factory/tickets/T-700.md"
git -C "$PRODUCT" add factory/tickets/T-700.md
git -C "$PRODUCT" -c user.name=test -c user.email=test@example.com \
  commit -qm "restore clean approval fixture"
git -C "$PRODUCT" push -q "$REMOTE" HEAD:refs/heads/ticket/T-700
sed -E 's/^State: .*/State: Approved/' \
  "$PRODUCT/factory/tickets/T-700.md" > "$TMP/ticket"
mv "$TMP/ticket" "$PRODUCT/factory/tickets/T-700.md"
printf 'Operator-Approval: Linear\n' >> "$PRODUCT/factory/tickets/T-700.md"
git -C "$PRODUCT" add factory/tickets/T-700.md
git -C "$PRODUCT" -c user.name=test -c user.email=test@example.com \
  commit -qm "manual approval laundering fixture"
git -C "$PRODUCT" push -q "$REMOTE" HEAD:refs/heads/ticket/T-700
printf '{"tickets":{"T-700":{"operator":{"state":"Approved","approval":"Linear"}}}}\n' \
  > "$PRODUCT/factory/linear-map.json"
BEFORE="$(git -C "$PRODUCT" rev-parse HEAD)"
if ticket_state --ticket T-700 --workdir "$PRODUCT" --action materialize \
  >/dev/null 2>&1; then
  echo "FAIL: manually committed approval consumed trusted operator evidence" >&2
  exit 1
fi
[[ "$(git -C "$PRODUCT" rev-parse HEAD)" == "$BEFORE" ]]
[[ "$(git --git-dir="$REMOTE" rev-parse refs/heads/ticket/T-700)" == "$BEFORE" ]]
python3 - "$PRODUCT/factory/linear-map.json" <<'PY'
import json, sys

operator = json.load(open(sys.argv[1]))["tickets"]["T-700"].get("operator")
assert operator == {"state": "Approved", "approval": "Linear"}
PY
grep -v '^Operator-Approval:' "$PRODUCT/factory/tickets/T-700.md" | \
  sed 's/^State: Approved$/State: Awaiting Approval/' > "$TMP/ticket"
mv "$TMP/ticket" "$PRODUCT/factory/tickets/T-700.md"
git -C "$PRODUCT" add factory/tickets/T-700.md
git -C "$PRODUCT" -c user.name=test -c user.email=test@example.com \
  commit -qm "restore trusted approval transition"
git -C "$PRODUCT" push -q "$REMOTE" HEAD:refs/heads/ticket/T-700
EXPECTED_APPROVAL_VERSION="$(python3 - "$ROOT/scripts/lib" \
  "$PRODUCT/factory/tickets/T-700.md" <<'PY'
import sys

sys.path.insert(0, sys.argv[1])
from effective_ticket import materialized_operator_version

text = open(sys.argv[2]).read().replace("State: Awaiting Approval", "State: Approved")
text += "Operator-Approval: Linear\n"
print(materialized_operator_version(text))
PY
)"
ticket_state --ticket T-700 --workdir "$PRODUCT" --action materialize >/dev/null
grep -q '^State: Approved$' "$PRODUCT/factory/tickets/T-700.md"
grep -q '^Operator-Approval: Linear$' "$PRODUCT/factory/tickets/T-700.md"
[[ "$(grep -c "^Operator-Approval-Attestation: sha256:$EXPECTED_APPROVAL_VERSION$" \
  "$PRODUCT/factory/tickets/T-700.md")" -eq 1 ]]
git --git-dir="$REMOTE" show \
  "refs/heads/ticket/T-700:factory/tickets/T-700.md" | \
  grep -q "^Operator-Approval-Attestation: sha256:$EXPECTED_APPROVAL_VERSION$"
[[ "$(git -C "$PRODUCT" log -1 --format=%s)" == \
  "T-700: materialize ticket state" ]]
[[ "$(git -C "$PRODUCT" log -1 --format='%an <%ae>')" == \
  "Software Factory <factory@local>" ]]
[[ "$(git -C "$PRODUCT" log -1 --format='%cn <%ce>')" == \
  "Software Factory <factory@local>" ]]

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

echo "PASS: ticket-state binds pushes, CAS tracking, and refuses evidence-sensitive transitions"
