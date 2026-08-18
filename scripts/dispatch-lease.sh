#!/usr/bin/env bash
# Claim, renew, or release one bounded dispatcher ticket lease.
set -euo pipefail

OPERATION="${1:-}"
shift || true
TICKET="" LEASE_ID=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --ticket) TICKET="$2"; shift 2 ;;
    --lease) LEASE_ID="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
[[ "$OPERATION" == "claim" || "$OPERATION" == "renew" || "$OPERATION" == "release" ||
   "$OPERATION" == "release-expired" ]] || {
  echo "usage: dispatch-lease.sh <claim|renew|release|release-expired> --ticket T-NNN [--lease ID]" >&2
  exit 2
}
[[ "$OPERATION" != "release-expired" ||
   "${FACTORY_RELEASE_CONTRACT_VERSION:-${FACTORY_CONTRACT_VERSION:-}}" == "1.8.0" ||
   "${FACTORY_RELEASE_CONTRACT_VERSION:-${FACTORY_CONTRACT_VERSION:-}}" == "2.0.0" ]] || {
  echo "expired lease recovery requires contract 1.8.0 or 2.0.0" >&2
  exit 3
}
[[ "$TICKET" =~ ^T-[0-9]+$ ]] || { echo "invalid ticket identifier" >&2; exit 2; }

KIT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
# shellcheck disable=SC1091
source "$KIT_DIR/scripts/lib/dispatch-leases.sh"
ROOT="${FACTORY_ROOT:-$(git rev-parse --show-toplevel 2>/dev/null || echo "$PWD")}"
FACTORY_DIR="$ROOT/factory"
LEASE_DIR="$(factory_dispatch_lease_dir "$ROOT")"
LEASE_LOCK="$(factory_dispatch_lock_dir "$ROOT")"
LAUNCH_LOCK="$FACTORY_DIR/.launch.lock"
LEASE_FILE="$(factory_dispatch_lease_file "$ROOT" "$TICKET")"
HELD_LAUNCH=0 HELD_LEASE=0

cleanup() {
  [[ "$HELD_LEASE" -eq 0 ]] || rmdir "$LEASE_LOCK" 2>/dev/null || true
  [[ "$HELD_LAUNCH" -eq 0 ]] || rmdir "$LAUNCH_LOCK" 2>/dev/null || true
}
trap cleanup EXIT HUP INT TERM

acquire() {
  local path="$1" label="$2" attempts="${3:-100}" i
  for i in $(seq 1 "$attempts"); do
    mkdir "$path" 2>/dev/null && return 0
    sleep 0.1
  done
  echo "$label lock stuck" >&2
  return 1
}

MAXIMUM="$(factory_dispatch_max_tickets "$ROOT" 2>/dev/null)" || {
  factory_dispatch_capacity_error >&2
  exit 3
}
[[ -f "$FACTORY_DIR/tickets/$TICKET.md" && ! -L "$FACTORY_DIR/tickets/$TICKET.md" ]] || {
  echo "ticket file is missing or unsafe" >&2
  exit 3
}

if [[ "$OPERATION" != "renew" ]]; then
  LAUNCH_ATTEMPTS=100
  [[ "$OPERATION" != "claim" ]] || LAUNCH_ATTEMPTS=600
  acquire "$LAUNCH_LOCK" "launch" "$LAUNCH_ATTEMPTS" || exit 8
  HELD_LAUNCH=1
fi
if [[ "$OPERATION" == "claim" ]]; then
  [[ ! -e "$FACTORY_DIR/KILL" ]] || { echo "KILL file present; lease refused" >&2; exit 4; }
  [[ ! -e "$FACTORY_DIR/MAINTENANCE" ]] || { echo "MAINTENANCE file present; lease refused" >&2; exit 4; }
elif [[ "$OPERATION" == release* ]] && factory_dispatch_has_ticket_run "$ROOT" "$TICKET"; then
  echo "ticket has an active run; lease release refused" >&2
  exit 7
fi
mkdir -p "$LEASE_DIR"
[[ ! -L "$LEASE_DIR" && ! -L "$LEASE_LOCK" ]] || { echo "dispatcher lease state is unsafe" >&2; exit 3; }
acquire "$LEASE_LOCK" "dispatcher lease" || exit 8
HELD_LEASE=1
if [[ "$OPERATION" == "renew" ]]; then
  [[ ! -e "$FACTORY_DIR/KILL" ]] || { echo "KILL file present; lease refused" >&2; exit 4; }
fi

case "$OPERATION" in
  claim)
    [[ -z "$LEASE_ID" ]] || { echo "claim does not accept --lease" >&2; exit 2; }
    python3 - "$LEASE_DIR" "$LEASE_FILE" "$TICKET" "$MAXIMUM" \
      "$FACTORY_DIR/.active-runs" <<'PY'
import json, os, pathlib, re, secrets, stat, sys, tempfile, time

root, destination = map(pathlib.Path, sys.argv[1:3])
ticket, maximum, active_root = sys.argv[3], int(sys.argv[4]), pathlib.Path(sys.argv[5])
entries = sorted(root.iterdir(), key=lambda path: path.name)
lease_ids = set()
record_tickets = set()
for path in entries:
    value = path.lstat()
    if not stat.S_ISREG(value.st_mode) or path.is_symlink() or not re.fullmatch(r"T-[0-9]+\.json", path.name):
        raise SystemExit("dispatcher lease state is unsafe")
    try:
        record = json.loads(path.read_text())
    except Exception:
        raise SystemExit("dispatcher lease state is unsafe")
    record_ticket = record.get("ticket")
    record_lease = record.get("lease_id")
    if (
        record.get("schema_version") != 1
        or record_ticket != path.stem
        or not re.fullmatch(r"T-[0-9]+", record_ticket or "")
        or not re.fullmatch(r"[0-9a-f]{64}", record_lease or "")
        or not isinstance(record.get("claimed_epoch"), int)
        or isinstance(record.get("claimed_epoch"), bool)
        or not isinstance(record.get("expires_epoch"), int)
        or isinstance(record.get("expires_epoch"), bool)
        or record["expires_epoch"] <= record["claimed_epoch"]
        or record_ticket in record_tickets
        or record_lease in lease_ids
    ):
        raise SystemExit("dispatcher lease state is unsafe")
    record_tickets.add(record_ticket)
    lease_ids.add(record_lease)
active_tickets = set()
if active_root.exists() or active_root.is_symlink():
    value = active_root.lstat()
    if (
        active_root.is_symlink() or not stat.S_ISDIR(value.st_mode)
        or value.st_uid != os.geteuid() or stat.S_IMODE(value.st_mode) & 0o022
    ):
        raise SystemExit("active-run state is unsafe")
    for path in active_root.iterdir():
        match = re.match(r"^(T-[0-9]+)\.", path.name)
        if match:
            active_tickets.add(match.group(1))
if destination.exists():
    raise SystemExit("ticket already has a dispatcher lease")
if ticket in active_tickets or len(record_tickets | active_tickets) >= maximum:
    raise SystemExit("dispatcher capacity is full")
now = int(time.time())
lease_id = secrets.token_hex(32)
while lease_id in lease_ids:
    lease_id = secrets.token_hex(32)
record = {
    "schema_version": 1,
    "ticket": ticket,
    "lease_id": lease_id,
    "claimed_epoch": now,
    "expires_epoch": now + 900,
}
fd, temporary = tempfile.mkstemp(prefix=".lease-", dir=str(root))
try:
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(record, stream, sort_keys=True, separators=(",", ":"))
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.link(temporary, destination)
finally:
    pathlib.Path(temporary).unlink(missing_ok=True)
print(json.dumps(record, sort_keys=True))
PY
    ;;
  renew)
    [[ "$LEASE_ID" =~ ^[0-9a-f]{64}$ ]] || { echo "renew requires a canonical --lease" >&2; exit 2; }
    python3 - "$LEASE_FILE" "$TICKET" "$LEASE_ID" <<'PY'
import json, os, pathlib, stat, sys, tempfile, time

path, ticket, lease_id = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3]
value = path.lstat()
if not stat.S_ISREG(value.st_mode) or path.is_symlink():
    raise SystemExit("dispatcher lease is unsafe")
record = json.loads(path.read_text())
if record.get("schema_version") != 1 or record.get("ticket") != ticket or record.get("lease_id") != lease_id:
    raise SystemExit("dispatcher lease does not match")
record["expires_epoch"] = int(time.time()) + 900
fd, temporary = tempfile.mkstemp(prefix=".lease-", dir=str(path.parent))
try:
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(record, stream, sort_keys=True, separators=(",", ":"))
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
finally:
    pathlib.Path(temporary).unlink(missing_ok=True)
print(json.dumps(record, sort_keys=True))
PY
    [[ ! -e "$FACTORY_DIR/KILL" ]] || { echo "KILL file appeared during renewal; lease refused" >&2; exit 4; }
    ;;
  release|release-expired)
    [[ "$LEASE_ID" =~ ^[0-9a-f]{64}$ ]] || { echo "release requires a canonical --lease" >&2; exit 2; }
    python3 - "$LEASE_FILE" "$TICKET" "$LEASE_ID" "$OPERATION" <<'PY'
import json, pathlib, stat, sys, time

path, ticket, lease_id, operation = pathlib.Path(sys.argv[1]), *sys.argv[2:]
if not path.exists() and not path.is_symlink():
    if operation == "release-expired":
        raise SystemExit("expired dispatcher lease is absent")
    print('{"absent":true,"ticket":%s}' % json.dumps(ticket))
    raise SystemExit
value = path.lstat()
if not stat.S_ISREG(value.st_mode) or path.is_symlink():
    raise SystemExit("dispatcher lease is unsafe")
record = json.loads(path.read_text())
if record.get("schema_version") != 1 or record.get("ticket") != ticket or record.get("lease_id") != lease_id:
    raise SystemExit("dispatcher lease does not match")
if operation == "release-expired" and (
    not isinstance(record.get("claimed_epoch"), int)
    or isinstance(record.get("claimed_epoch"), bool)
    or not isinstance(record.get("expires_epoch"), int)
    or isinstance(record.get("expires_epoch"), bool)
    or record["expires_epoch"] <= record["claimed_epoch"]
    or record["expires_epoch"] > int(time.time())
):
    raise SystemExit("dispatcher lease is not an exact expired lease")
path.unlink()
print(json.dumps({
    "expired": operation == "release-expired", "released": True, "ticket": ticket,
}, sort_keys=True))
PY
    ;;
esac
