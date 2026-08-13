#!/usr/bin/env bash
# Materialize operator-owned fields or commit one legal factory-owned state move.
set -euo pipefail
export PYTHONDONTWRITEBYTECODE=1

TICKET="" WORKDIR="" ACTION="" STATE="" ROLE=""
CONTRACT_VERSION="${FACTORY_RELEASE_CONTRACT_VERSION:-${FACTORY_CONTRACT_VERSION:-}}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --ticket) TICKET="$2"; shift 2 ;;
    --workdir) WORKDIR="$2"; shift 2 ;;
    --action) ACTION="$2"; shift 2 ;;
    --state) STATE="$2"; shift 2 ;;
    --role) ROLE="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
[[ "$TICKET" =~ ^T-[0-9]+$ && -n "$WORKDIR" ]] || { echo "invalid ticket-state arguments" >&2; exit 2; }
[[ "$ACTION" == "materialize" || "$ACTION" == "transition" ||
   "$ACTION" == "reviewer-reconcile" ||
   "$ACTION" == "qualification-backlog" ]] || { echo "invalid ticket-state action" >&2; exit 2; }
[[ "$ACTION" != "transition" || -n "$STATE" ]] || { echo "transition requires --state" >&2; exit 2; }
[[ -z "$ROLE" || "$ACTION" == "qualification-backlog" ]] ||
  { echo "--role is valid only for qualification backlog return" >&2; exit 2; }
[[ "$ACTION" != "reviewer-reconcile" ||
   "$CONTRACT_VERSION" == "1.7.0" || "$CONTRACT_VERSION" == "1.8.0" ||
   "$CONTRACT_VERSION" == "2.0.0" ]] || {
  echo "reviewer reconciliation requires contract 1.7.0" >&2
  exit 1
}
[[ "$ACTION" != "qualification-backlog" ||
   "$CONTRACT_VERSION" == "1.7.0" || "$CONTRACT_VERSION" == "1.8.0" ||
   "$CONTRACT_VERSION" == "2.0.0" ]] || {
  echo "qualification backlog return requires contract 1.7.0" >&2
  exit 1
}

KIT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
# shellcheck disable=SC1091
source "$KIT_DIR/scripts/lib/product-remote.sh"
unset FACTORY_TRUSTED_PRODUCT_ORIGIN
readonly FACTORY_TRUSTED_PRODUCT_ORIGIN="${FACTORY_CERTIFIED_PRODUCT_ORIGIN:-}"
unset FACTORY_CERTIFIED_PRODUCT_ORIGIN
PRODUCT_ROOT="${FACTORY_ROOT:-$WORKDIR}"
MAP="${FACTORY_OPERATOR_MAP:-$PRODUCT_ROOT/factory/operator-map.json}"
TICKET_FILE="$WORKDIR/factory/tickets/$TICKET.md"
[[ -f "$TICKET_FILE" ]] || { echo "ticket file missing from worktree" >&2; exit 1; }
INITIAL_STATE="$(python3 - "$TICKET_FILE" <<'PY'
import re
import sys

values = re.findall(
    r"^State:\s*(.*?)\s*$", open(sys.argv[1], encoding="utf-8").read(),
    re.IGNORECASE | re.MULTILINE,
)
if len(values) != 1:
    raise SystemExit("ticket State field is ambiguous")
print(values[0])
PY
)"
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
OPERATOR_ACTION_FILE="$(mktemp "${TMPDIR:-/tmp}/ticket-state-action.XXXXXX")"
trap 'rm -f "$TMP" "$OPERATOR_VERSION_FILE" "$OPERATOR_ACTION_FILE"' EXIT
python3 "$KIT_DIR/scripts/lib/effective_ticket.py" \
  --ticket-file "$TICKET_FILE" --operator-map "$MAP" --ticket "$TICKET" \
  --operator-version-file "$OPERATOR_VERSION_FILE" \
  --operator-action-file "$OPERATOR_ACTION_FILE" > "$TMP"
OPERATOR_VERSION="$(<"$OPERATOR_VERSION_FILE")"
OPERATOR_ACTION="$(<"$OPERATOR_ACTION_FILE")"

if [[ "$ACTION" == "materialize" ]]; then
  python3 - "$KIT_DIR/scripts/lib" "$TICKET_FILE" "$TMP" <<'PY'
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from ticket_state_transition import TransitionError, validate_materialization

current_path, effective_path = map(Path, sys.argv[2:])
try:
    validate_materialization(
        current_path.read_text(),
        effective_path.read_text(),
    )
except TransitionError as error:
    raise SystemExit(str(error))
PY
elif [[ "$ACTION" == "transition" ]]; then
  cmp -s "$TMP" "$TICKET_FILE" || {
    echo "pending operator fields require materialization before factory transition" >&2
    exit 1
  }
  python3 - "$KIT_DIR/scripts/lib" "$TMP" "$STATE" "$CONTRACT_VERSION" <<'PY'
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from ticket_state_transition import TransitionError, apply_factory_transition

path, target, contract = Path(sys.argv[2]), sys.argv[3], sys.argv[4]
try:
    path.write_text(
        apply_factory_transition(path.read_text(), target, contract)
    )
except TransitionError as error:
    raise SystemExit(str(error))
PY
elif [[ "$ACTION" == "reviewer-reconcile" ]]; then
  cmp -s "$TMP" "$TICKET_FILE" || {
    echo "pending operator fields require materialization before reviewer reconciliation" >&2
    exit 1
  }
  HEAD_BEFORE="$(git -C "$WORKDIR" rev-parse HEAD)" || {
    echo "ticket head cannot be resolved" >&2
    exit 1
  }
  if [[ -n "${FACTORY_DEV_PRODUCT_CHECKPOINT:-}" ]]; then
    python3 "$KIT_DIR/scripts/lib/reviewer-reconcile.py" \
      --runs-dir "$PRODUCT_ROOT/factory/runs" \
      --ticket-file "$TICKET_FILE" --ticket "$TICKET" \
      --head "$HEAD_BEFORE" \
      --contract-version "$CONTRACT_VERSION" \
      --checkpoint "$FACTORY_DEV_PRODUCT_CHECKPOINT" \
      --output "$TMP"
  else
    python3 "$KIT_DIR/scripts/lib/reviewer-reconcile.py" \
      --runs-dir "$PRODUCT_ROOT/factory/runs" \
      --ticket-file "$TICKET_FILE" --ticket "$TICKET" \
      --head "$HEAD_BEFORE" \
      --contract-version "$CONTRACT_VERSION" \
      --output "$TMP"
  fi
elif [[ "$ACTION" == "qualification-backlog" ]]; then
  cmp -s "$TMP" "$TICKET_FILE" || {
    echo "pending operator fields require materialization before backlog return" >&2
    exit 1
  }
  PINNED_KIT_SHA="$(git -C "$KIT_DIR" rev-parse --verify HEAD 2>/dev/null)" || {
    echo "pinned Factory SHA is unavailable" >&2
    exit 1
  }
  git -C "$WORKDIR" show \
    "refs/remotes/origin/main:factory/QUALIFICATION.json" > "$OPERATOR_VERSION_FILE" ||
    { echo "protected qualification manifest is unavailable" >&2; exit 1; }
  python3 - "$TMP" "$OPERATOR_VERSION_FILE" "$TICKET" \
    "$PRODUCT_ROOT/factory/runs" "$ROLE" "$PINNED_KIT_SHA" \
    "$CONTRACT_VERSION" <<'PY'
import json
import re
import stat
import sys
from pathlib import Path

ticket_path, qualification_path, ticket, runs_path, role, pinned_kit_sha, contract_version = (
    Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3], Path(sys.argv[4]),
    sys.argv[5], sys.argv[6], sys.argv[7]
)
text = ticket_path.read_text()
qualification = json.loads(qualification_path.read_text())
if (
    qualification.get("schema") != "nysa.software-factory.qualification/v1"
    or ticket not in qualification.get("tickets", [])
):
    raise SystemExit("protected qualification manifest does not authorize backlog return")
states = re.findall(r"^State:\s*(.*?)\s*$", text, re.I | re.M)
spec_failed = (
    states == ["Planning"]
    and re.search(
        r"^\s*SPEC-LINT:\s*FAIL(?:\s+—\s+.*)?\s*$", text, re.I | re.M
    )
)
contract_blocked = False
if role:
    if role not in {"planner", "test-author", "builder"} or states not in (
        ["Planning"], ["Building"]
    ):
        raise SystemExit("qualification contract blocker has invalid role or state")
    candidates = []
    for path in runs_path.glob("*.meta"):
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise SystemExit("unsafe role manifest")
        values = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition("=")
            if not separator or key in values:
                raise SystemExit("malformed role manifest")
            values[key] = value
        if values.get("ticket") == ticket and values.get("role") == role:
            candidates.append(values)
    if candidates:
        latest = max(candidates, key=lambda value: (
            value.get("started_at", ""), value.get("run_id", "")
        ))
        accounted = latest.get("accounting_state") == "completed" or (
            latest.get("accounting_state") == "abandoned_conservative"
            and latest.get("cost_basis") == "conservative_reservation"
            and latest.get("effective_cost") == latest.get("reserved_usd")
        )
        contract_blocked = accounted and all((
            latest.get("contract_version") == contract_version,
            latest.get("phase") == "completed",
            latest.get("exit_status") == "12",
            latest.get("role_exit") == "role_exit_contract_blocked",
            latest.get("kit_sha") == pinned_kit_sha,
        ))
if not spec_failed and not contract_blocked:
    raise SystemExit("qualification backlog return lacks authenticated failure evidence")
ticket_path.write_text(re.sub(
    r"^State:\s*.*$", "State: Backlog", text, count=1, flags=re.I | re.M,
))
PY
fi

python3 - "$KIT_DIR/scripts/lib" "$INITIAL_STATE" "$ACTION" "$TMP" <<'PY'
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from ticket_state_transition import (
    TransitionError,
    exact_state,
    validate_action_transition,
)

initial, action, path = sys.argv[2].strip().lower(), sys.argv[3], Path(sys.argv[4])
try:
    validate_action_transition(action, initial, exact_state(path.read_text()))
except TransitionError as error:
    raise SystemExit(str(error))
PY

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

python3 - "$KIT_DIR/scripts/lib" "$MAP" "$TICKET" "$OPERATOR_VERSION" \
  "$OPERATOR_ACTION" "$CONTRACT_VERSION" "${FACTORY_TRANSITION_STATE_DIR:-}" <<'PY'
import fcntl
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from effective_ticket import operator_action, operator_version
import operator_receipt

path, ticket, expected, action, contract, state_dir = (
    Path(sys.argv[2]), sys.argv[3], sys.argv[4], sys.argv[5], sys.argv[6],
    sys.argv[7],
)
if not path.is_file():
    raise SystemExit(0)
intents = path.parent / ".operator-clears"
intents.mkdir(mode=0o700, exist_ok=True)
intent = intents / f"{ticket}-{expected}.json"
if not intent.exists():
    try:
        fd = os.open(intent, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, "w") as output:
            json.dump({
                "operator_version": expected,
                "schema": "operator-clear/v1",
                "ticket": ticket,
            }, output, sort_keys=True, separators=(",", ":"))
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
lock = path.parent / ".operator-map.lock"
with lock.open("a") as handle:
    fcntl.flock(handle, fcntl.LOCK_EX)
    data = json.loads(path.read_text())
    entry = data.get("tickets", {}).get(ticket, {})
    current = operator_version(entry.get("operator") or {})
    if current != expected:
        raise SystemExit(0)
    operator = entry.get("operator") or {}
    if contract == "2.0.0" and action:
        current_action, binding = operator_action(operator)
        if current_action != action:
            raise SystemExit("operator action changed during materialization")
        operator_receipt.verify_consume_exact(
            Path(state_dir), ticket, action, operator["receipt_sha256"], binding,
        )
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
