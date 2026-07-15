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
PRODUCT_ROOT="${FACTORY_ROOT:-$WORKDIR}"
MAP="$PRODUCT_ROOT/factory/linear-map.json"
TICKET_FILE="$WORKDIR/factory/tickets/$TICKET.md"
[[ -f "$TICKET_FILE" ]] || { echo "ticket file missing from worktree" >&2; exit 1; }
[[ -z "$(git -C "$WORKDIR" status --porcelain)" ]] || { echo "ticket worktree is dirty" >&2; exit 1; }
BRANCH="$(git -C "$WORKDIR" symbolic-ref --quiet --short HEAD 2>/dev/null || true)"
[[ -n "$BRANCH" ]] || { echo "ticket worktree is detached" >&2; exit 1; }
OPERATOR_VERSION="$(python3 - "$MAP" "$TICKET" <<'PY'
import hashlib, json, sys
from pathlib import Path
path, ticket = Path(sys.argv[1]), sys.argv[2]
data = json.loads(path.read_text()) if path.is_file() else {}
value = data.get("tickets", {}).get(ticket, {}).get("operator")
print(hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest())
PY
)"

TMP="$(mktemp "${TMPDIR:-/tmp}/ticket-state.XXXXXX")"
trap 'rm -f "$TMP"' EXIT
python3 "$KIT_DIR/scripts/lib/effective_ticket.py" \
  --ticket-file "$TICKET_FILE" --operator-map "$MAP" --ticket "$TICKET" > "$TMP"

if [[ "$ACTION" == "transition" ]]; then
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
    ("review", "building"), ("review", "awaiting approval"), ("approved", "done"),
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
git -C "$WORKDIR" push --no-force origin "HEAD:refs/heads/$BRANCH" >/dev/null 2>&1
LOCAL_HEAD="$(git -C "$WORKDIR" rev-parse HEAD)"
REMOTE_HEAD="$(git -C "$WORKDIR" ls-remote --heads origin \
  "refs/heads/$BRANCH" 2>/dev/null | awk 'NR==1 {print $1; exit}')"
[[ "$REMOTE_HEAD" == "$LOCAL_HEAD" ]] || { echo "ticket-state remote verification failed" >&2; exit 1; }

python3 - "$MAP" "$TICKET" "$OPERATOR_VERSION" <<'PY'
import fcntl
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

path, ticket, expected = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
if not path.is_file():
    raise SystemExit(0)
lock = path.parent / ".linear-sync.lock"
with lock.open("a") as handle:
    fcntl.flock(handle, fcntl.LOCK_EX)
    data = json.loads(path.read_text())
    entry = data.get("tickets", {}).get(ticket, {})
    current = hashlib.sha256(
        json.dumps(entry.get("operator"), sort_keys=True).encode()
    ).hexdigest()
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
