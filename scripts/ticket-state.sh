#!/usr/bin/env bash
# Materialize Linear-owned fields or commit one legal factory-owned state move.
set -euo pipefail

TICKET="" WORKDIR="" ACTION="" STATE=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --ticket) TICKET="$2"; shift 2 ;;
    --workdir) WORKDIR="$2"; shift 2 ;;
    --action) ACTION="$2"; shift 2 ;;
    --state) STATE="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
[[ "$TICKET" =~ ^T-[0-9]+$ && -n "$WORKDIR" ]] || { echo "invalid ticket-state arguments" >&2; exit 2; }
[[ "$ACTION" == "materialize" || "$ACTION" == "transition" ]] || { echo "invalid ticket-state action" >&2; exit 2; }
[[ "$ACTION" == "materialize" || -n "$STATE" ]] || { echo "transition requires --state" >&2; exit 2; }

KIT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
# shellcheck disable=SC1091
source "$KIT_DIR/scripts/lib/product-remote.sh"
unset FACTORY_TRUSTED_PRODUCT_ORIGIN
readonly FACTORY_TRUSTED_PRODUCT_ORIGIN="${FACTORY_CERTIFIED_PRODUCT_ORIGIN:-}"
unset FACTORY_CERTIFIED_PRODUCT_ORIGIN
PRODUCT_ROOT="${FACTORY_ROOT:-$WORKDIR}"
MAP="$PRODUCT_ROOT/factory/linear-map.json"
TICKET_FILE="$WORKDIR/factory/tickets/$TICKET.md"
[[ -f "$TICKET_FILE" ]] || { echo "ticket file missing from worktree" >&2; exit 1; }
WORKTREE_STATUS="$(git -C "$WORKDIR" status --porcelain --untracked-files=all \
  --ignore-submodules=none)" || { echo "ticket worktree cannot be inspected" >&2; exit 1; }
[[ -z "$WORKTREE_STATUS" ]] || { echo "ticket worktree is dirty" >&2; exit 1; }
BRANCH="$(git -C "$WORKDIR" symbolic-ref --quiet --short HEAD 2>/dev/null || true)"
[[ -n "$BRANCH" ]] || { echo "ticket worktree is detached" >&2; exit 1; }
PRODUCT_REMOTE="$(factory_capture_product_remote "$PRODUCT_ROOT" "$FACTORY_TRUSTED_PRODUCT_ORIGIN")" || {
  echo "certified product push destination validation failed" >&2
  exit 1
}

TMP="$(mktemp "${TMPDIR:-/tmp}/ticket-state.XXXXXX")"
OPERATOR_VERSION_FILE="$(mktemp "${TMPDIR:-/tmp}/ticket-state-version.XXXXXX")"
trap 'rm -f "$TMP" "$OPERATOR_VERSION_FILE"' EXIT
python3 "$KIT_DIR/scripts/lib/effective_ticket.py" \
  --ticket-file "$TICKET_FILE" --operator-map "$MAP" --ticket "$TICKET" \
  --operator-version-file "$OPERATOR_VERSION_FILE" > "$TMP"
OPERATOR_VERSION="$(<"$OPERATOR_VERSION_FILE")"

if [[ "$ACTION" == "materialize" ]]; then
  python3 - "$TICKET_FILE" "$TMP" "$KIT_DIR/scripts/lib" <<'PY'
import re
import sys
from pathlib import Path

current_path, effective_path = map(Path, sys.argv[1:3])
sys.path.insert(0, sys.argv[3])
from effective_ticket import materialized_operator_version

current_text = current_path.read_text()
effective_text = effective_path.read_text()

def field(text, name):
    match = re.search(rf"^{re.escape(name)}:\s*(.+)$", text, re.MULTILINE | re.IGNORECASE)
    return match.group(1).strip() if match else ""

current_state = field(current_text, "State").lower()
effective_state = field(effective_text, "State").lower()
if not current_state or not effective_state:
    raise SystemExit("ticket has no State field")
if current_state != effective_state and effective_state in {"awaiting approval", "done"}:
    raise SystemExit(
        f"evidence-sensitive state requires a dedicated attestation: {effective_state}"
    )
legal_approval = current_state == "awaiting approval" and effective_state == "approved"
if current_state != effective_state and effective_state == "approved" and not legal_approval:
    raise SystemExit("operator approval requires committed Awaiting Approval state")
current_approval = field(current_text, "Operator-Approval")
effective_approval = field(effective_text, "Operator-Approval")
current_attestation_count = len(re.findall(
    r"^Operator-Approval-Attestation:", current_text, re.MULTILINE | re.IGNORECASE
))
approval_changed = effective_approval != current_approval
if approval_changed and effective_approval != "Linear":
    raise SystemExit("operator approval marker must be Linear")
if approval_changed and not legal_approval:
    raise SystemExit("operator approval marker requires Awaiting Approval -> Approved")
if current_state != effective_state and effective_state == "approved" and effective_approval != "Linear":
    raise SystemExit("Approved requires Operator-Approval: Linear")
if effective_state == "approved" and effective_approval == "Linear" and not (
    legal_approval and approval_changed
):
    raise SystemExit("Approved materialization requires a fresh Awaiting Approval transition")
if legal_approval:
    if current_attestation_count:
        raise SystemExit("approval attestation already exists before materialization")
    materialized_version = materialized_operator_version(effective_text)
    effective_text = effective_text.rstrip("\n") + (
        f"\nOperator-Approval-Attestation: sha256:{materialized_version}\n"
    )
    effective_path.write_text(effective_text)
PY
elif [[ "$ACTION" == "transition" ]]; then
  cmp -s "$TMP" "$TICKET_FILE" || {
    echo "pending operator fields require materialization before factory transition" >&2
    exit 1
  }
  python3 - "$TMP" "$STATE" <<'PY'
import re
import sys
from pathlib import Path

path, target = Path(sys.argv[1]), sys.argv[2]
text = path.read_text()
match = re.search(r"^State:\s*(.+)$", text, re.MULTILINE | re.IGNORECASE)
if not match:
    raise SystemExit("ticket has no State field")
current = match.group(1).strip().lower()
target_key = target.strip().lower()
states = {
    "planning": "Planning", "building": "Building", "review": "Review",
    "awaiting approval": "Awaiting Approval", "blocked-escalated": "Blocked-Escalated",
    "done": "Done",
}
allowed = {
    ("ready", "planning"), ("planning", "building"), ("building", "review"),
    ("review", "building"),
}
if target_key == "blocked-escalated" and current in {
    "ready", "planning", "building", "review", "awaiting approval", "approved"
}:
    allowed.add((current, target_key))
if (current, target_key) not in allowed or target_key not in states:
    raise SystemExit(f"illegal factory transition: {current} -> {target_key}")
path.write_text(re.sub(r"^State:\s*.*$", f"State: {states[target_key]}", text, count=1, flags=re.MULTILINE | re.IGNORECASE))
PY
fi

CHANGED=0
if ! cmp -s "$TMP" "$TICKET_FILE"; then
  mv "$TMP" "$TICKET_FILE"
  CHANGED=1
  git -C "$WORKDIR" add -- "${TICKET_FILE#"$WORKDIR/"}"
  git -C "$WORKDIR" -c user.name="Software Factory" -c user.email="factory@local" \
    commit -m "$TICKET: ${ACTION//-/ } ticket state" >/dev/null
fi
factory_product_remote_matches "$PRODUCT_ROOT" "$PRODUCT_REMOTE" || {
  echo "$FACTORY_PRODUCT_REMOTE_ERROR" >&2
  exit 1
}
LOCAL_HEAD="$(git -C "$WORKDIR" rev-parse HEAD)"
TRACKING_HEAD="$(factory_remote_tracking_tip "$WORKDIR" "$BRANCH")"
git -C "$WORKDIR" push --no-force -- "$PRODUCT_REMOTE" \
  "$LOCAL_HEAD:refs/heads/$BRANCH" >/dev/null 2>&1
REMOTE_HEAD="$(git -C "$WORKDIR" ls-remote --heads -- "$PRODUCT_REMOTE" \
  "refs/heads/$BRANCH" 2>/dev/null | awk 'NR==1 {print $1; exit}')"
[[ "$REMOTE_HEAD" == "$LOCAL_HEAD" ]] || { echo "ticket-state remote verification failed" >&2; exit 1; }
factory_update_tracking_ref "$WORKDIR" "$BRANCH" "$LOCAL_HEAD" "$TRACKING_HEAD" || {
  echo "ticket-state remote tracking update failed" >&2
  exit 1
}

python3 - "$KIT_DIR/scripts/lib" "$MAP" "$TICKET" "$OPERATOR_VERSION" <<'PY'
import fcntl
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from effective_ticket import operator_version

path, ticket, expected = Path(sys.argv[2]), sys.argv[3], sys.argv[4]
if not path.is_file():
    raise SystemExit(0)
lock = path.parent / ".linear-sync.lock"
with lock.open("a") as handle:
    fcntl.flock(handle, fcntl.LOCK_EX)
    data = json.loads(path.read_text())
    entry = data.get("tickets", {}).get(ticket, {})
    current = operator_version(entry.get("operator") or {})
    if current != expected:
        raise SystemExit(0)
    entry.pop("operator", None)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    with os.fdopen(fd, "w") as output:
        json.dump(data, output, indent=2, sort_keys=True)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)
PY

python3 - "$TICKET" "$ACTION" "$STATE" "$CHANGED" "$LOCAL_HEAD" <<'PY'
import json, sys
ticket, action, state, changed, head = sys.argv[1:]
print(json.dumps({"ticket": ticket, "action": action, "state": state or None,
                  "changed": changed == "1", "head": head}, sort_keys=True))
PY
