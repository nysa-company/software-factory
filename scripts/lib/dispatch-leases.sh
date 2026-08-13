#!/usr/bin/env bash
# Shared validation for bounded dispatcher ticket leases.

factory_dispatch_capacity_limit() {
  local contract="${1:-${FACTORY_RELEASE_CONTRACT_VERSION:-${FACTORY_CONTRACT_VERSION:-}}}"
  if [[ "$contract" == "1.6.0" ]]; then
    printf '6\n'
  else
    printf '4\n'
  fi
}

factory_dispatch_capacity_error() {
  local contract="${1:-}"
  printf 'MAX_CONCURRENT_TICKETS must be defined at most once as an integer from 1 through %s\n' \
    "$(factory_dispatch_capacity_limit "$contract")"
}

factory_dispatch_max_tickets() {
  local contract="${2:-}" default=1
  [[ "$contract" != "1.6.0" ]] || default=4
  python3 - "$1/factory/PROJECT.env" "$(factory_dispatch_capacity_limit "$contract")" "$default" <<'PY'
import pathlib, re, sys

path = pathlib.Path(sys.argv[1])
maximum = int(sys.argv[2])
value = sys.argv[3]
seen = 0
if path.exists() or path.is_symlink():
    if path.is_symlink() or not path.is_file():
        raise SystemExit(1)
    for raw in path.read_text(errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"(?:export\s+)?MAX_CONCURRENT_TICKETS\s*=\s*([1-9][0-9]*)", line)
        if match:
            if int(match.group(1)) > maximum:
                raise SystemExit(1)
            seen += 1
            value = match.group(1)
        elif re.match(r"(?:export\s+)?MAX_CONCURRENT_TICKETS\s*=", line):
            raise SystemExit(1)
if seen > 1:
    raise SystemExit(1)
print(value)
PY
}

factory_dispatch_lease_dir() {
  printf '%s/factory/.dispatch-leases\n' "$1"
}

factory_dispatch_lock_dir() {
  printf '%s/factory/.dispatch-leases.lock\n' "$1"
}

factory_dispatch_lease_file() {
  printf '%s/%s.json\n' "$(factory_dispatch_lease_dir "$1")" "$2"
}

factory_dispatch_require_lease() {
  local root="$1" ticket="$2" lease_id="${3:-}" maximum file
  maximum="$(factory_dispatch_max_tickets "$root" 2>/dev/null)" || {
    FACTORY_DISPATCH_LEASE_ERROR="$(factory_dispatch_capacity_error)"
    return 1
  }
  [[ "$maximum" -gt 1 ]] || return 0
  [[ "$lease_id" =~ ^[0-9a-f]{64}$ ]] || {
    FACTORY_DISPATCH_LEASE_ERROR="a canonical dispatcher lease is required while concurrency is enabled"
    return 1
  }
  file="$(factory_dispatch_lease_file "$root" "$ticket")"
  python3 - "$file" "$ticket" "$lease_id" <<'PY' || {
import json, os, pathlib, re, stat, sys, time

path, ticket, lease_id = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3]
try:
    value = path.lstat()
    if not stat.S_ISREG(value.st_mode) or path.is_symlink():
        raise ValueError
    record = json.loads(path.read_text())
    if record.get("schema_version") != 1 or record.get("ticket") != ticket:
        raise ValueError
    if record.get("lease_id") != lease_id:
        raise ValueError
    if not isinstance(record.get("expires_epoch"), int):
        raise ValueError
    if record["expires_epoch"] <= int(time.time()):
        raise SystemExit(2)
except SystemExit:
    raise
except Exception:
    raise SystemExit(1)
PY
    case "$?" in
      2) FACTORY_DISPATCH_LEASE_ERROR="dispatcher lease is stale; matching owner must renew or the operator must recover it under maintenance" ;;
      *) FACTORY_DISPATCH_LEASE_ERROR="dispatcher lease is missing, unsafe, or does not match this ticket" ;;
    esac
    return 1
  }
  return 0
}

factory_dispatch_has_leases() {
  python3 - "$(factory_dispatch_lease_dir "$1")" <<'PY'
import pathlib, sys

root = pathlib.Path(sys.argv[1])
if root.is_symlink() or (root.exists() and not root.is_dir()):
    raise SystemExit(0)
raise SystemExit(0 if root.exists() and any(root.iterdir()) else 1)
PY
}

factory_dispatch_has_ticket_run() {
  local root="$1" ticket="$2" file
  for file in "$root/factory/runs/"*.pid; do
    [[ -e "$file" ]] || continue
    grep -Fqx "ticket=$ticket" "$file" 2>/dev/null && return 0
  done
  for file in "$root/factory/.active-runs/$ticket."*.pid; do
    [[ -e "$file" ]] && return 0
  done
  for file in "$root/factory/.active-runs/$ticket."*.lock; do
    [[ -d "$file" && ! -L "$file" && -f "$file/owner" && ! -L "$file/owner" ]] && return 0
  done
  return 1
}

factory_dispatch_clear_leases() {
  python3 - "$(factory_dispatch_lease_dir "$1")" <<'PY'
import pathlib, re, stat, sys

root = pathlib.Path(sys.argv[1])
if not root.exists():
    raise SystemExit(0)
if root.is_symlink() or not root.is_dir():
    raise SystemExit(1)
for path in root.iterdir():
    value = path.lstat()
    if not stat.S_ISREG(value.st_mode) or path.is_symlink() or not re.fullmatch(r"T-[0-9]+\.json", path.name):
        raise SystemExit(1)
for path in root.iterdir():
    path.unlink()
PY
}
