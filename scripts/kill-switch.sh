#!/usr/bin/env bash
# kill-switch.sh — stop the factory. Stops, does not destroy:
#   1. Drops a KILL file anchored at the product repo's factory/ dir
#      (same anchor run-agent.sh checks — not $PWD-dependent).
#   2. Terminates factory-launched process groups precisely, via the PID files
#      the run wrapper writes (factory/runs/*.pid) — NOT every agent process on
#      the machine; the operator's own sessions are untouched.
#   3. Disables factory launchd schedules (com.factory.*, rollup exempt).
# Key rotation is NOT here — that is incident response for a suspected leak
# (see docs/runbooks/operator.md). Console spend caps stay active regardless.
#
# Usage: kill-switch.sh [repo-root]   (default: git root of cwd)
# Resume: rm <repo-root>/factory/KILL and re-enable schedules.
set -uo pipefail

REPO_ROOT="${1:-$(git rev-parse --show-toplevel 2>/dev/null || echo "$PWD")}"
FACTORY_DIR="$REPO_ROOT/factory"
RUNS_DIR="$FACTORY_DIR/runs"
LAUNCH_LOCK="$FACTORY_DIR/.launch.lock"
PROVIDER_LOCK="$FACTORY_DIR/.provider.lock"
HELD_LAUNCH_LOCK=0
KIT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
# shellcheck disable=SC1091
source "$KIT_DIR/scripts/lib/dispatch-leases.sh"
DISPATCH_LOCK="$(factory_dispatch_lock_dir "$REPO_ROOT")"
HELD_DISPATCH_LOCK=0

process_start_identity() {
  ps -o lstart= -p "$1" 2>/dev/null | awk '{$1=$1; print; exit}'
}

process_group_alive() {
  ps -axo pgid=,stat= 2>/dev/null | awk -v expected="$1" '
    $1 == expected && $2 !~ /^Z/ { found=1 }
    END { exit(found ? 0 : 1) }
  '
}

terminate_exact_process() {
  local pid="$1" expected_start="$2" expected_pgid="$3" label="$4"
  local current_start current_pgid state target
  [[ "$pid" =~ ^[1-9][0-9]*$ && -n "$expected_start" ]] || return 1
  current_start="$(process_start_identity "$pid")"
  if [[ -n "$current_start" ]]; then
    state="$(ps -o stat= -p "$pid" 2>/dev/null | awk '{$1=$1; print; exit}')"
    current_pgid="$(ps -o pgid= -p "$pid" 2>/dev/null | awk '{$1=$1; print; exit}')"
    if [[ "$current_start" != "$expected_start" ||
          ( -n "$expected_pgid" && ( "$expected_pgid" != "$pid" ||
            "$current_pgid" != "$expected_pgid" ) ) ]]; then
      echo "WARNING: refusing stale or mismatched $label identity: pid=$pid" >&2
      return 1
    fi
  elif [[ -z "$expected_pgid" ]] || ! process_group_alive "$expected_pgid"; then
    return 0
  fi
  target="$pid"
  [[ -z "$expected_pgid" ]] || target="-$expected_pgid"
  kill -TERM -- "$target" 2>/dev/null || true
  for _stop_try in $(seq 1 100); do
    current_start="$(process_start_identity "$pid")"
    state="$(ps -o stat= -p "$pid" 2>/dev/null | awk '{$1=$1; print; exit}')"
    if [[ -n "$expected_pgid" ]]; then
      process_group_alive "$expected_pgid" || return 0
    else
      [[ -z "$current_start" || "$state" == Z* ]] && return 0
    fi
    sleep 0.02
  done
  current_pgid="$(ps -o pgid= -p "$pid" 2>/dev/null | awk '{$1=$1; print; exit}')"
  if [[ -n "$current_start" && ( "$current_start" != "$expected_start" ||
        ( -n "$expected_pgid" && "$current_pgid" != "$expected_pgid" ) ) ]]; then
    echo "WARNING: refusing changed $label identity before escalation: pid=$pid" >&2
    return 1
  fi
  kill -KILL -- "$target" 2>/dev/null || true
  for _stop_try in $(seq 1 100); do
    current_start="$(process_start_identity "$pid")"
    state="$(ps -o stat= -p "$pid" 2>/dev/null | awk '{$1=$1; print; exit}')"
    if [[ -n "$expected_pgid" ]]; then
      process_group_alive "$expected_pgid" || return 0
    else
      [[ -z "$current_start" || "$state" == Z* ]] && return 0
    fi
    sleep 0.02
  done
  echo "WARNING: exact $label survived bounded shutdown: pid=$pid" >&2
  return 1
}

cleanup_launch_lock() {
  [[ "$HELD_DISPATCH_LOCK" -eq 0 ]] || rmdir "$DISPATCH_LOCK" 2>/dev/null || true
  [[ "$HELD_LAUNCH_LOCK" -eq 0 ]] || rmdir "$LAUNCH_LOCK" 2>/dev/null || true
}
trap cleanup_launch_lock EXIT

mkdir -p "$FACTORY_DIR"
# Publish the stop condition first. A launcher holding the registration lock
# rechecks KILL before GO; a stale lock therefore cannot disable the stop.
date -u +"stopped_at=%FT%TZ" > "$FACTORY_DIR/KILL"
echo "KILL file written: $FACTORY_DIR/KILL (run-agent.sh will refuse new runs)"

for _lock_try in $(seq 1 "${FACTORY_LAUNCH_LOCK_ATTEMPTS:-100}"); do
  mkdir "$LAUNCH_LOCK" 2>/dev/null && { HELD_LAUNCH_LOCK=1; break; }
  sleep 0.1
done
if [[ "$HELD_LAUNCH_LOCK" -ne 1 ]]; then
  echo "WARNING: launch lock stuck; KILL is active and PID scan will proceed conservatively" >&2
fi

if [[ -d "$RUNS_DIR" ]]; then
  for pidfile in "$RUNS_DIR"/*.pid; do
    [[ -e "$pidfile" ]] || continue
    PID=""
    PGID=""
    RUN_ID=""
    PROCESS_START=""
    REMOVE_PID_FILE=0
    if grep -q '^pid=' "$pidfile" 2>/dev/null; then
      PID="$(sed -n 's/^pid=//p' "$pidfile" | awk 'NR==1 {print; exit}')"
      PGID="$(sed -n 's/^pgid=//p' "$pidfile" | awk 'NR==1 {print; exit}')"
      RUN_ID="$(sed -n 's/^run_id=//p' "$pidfile" | awk 'NR==1 {print; exit}')"
      PROCESS_START="$(sed -n 's/^process_start=//p' "$pidfile" | awk 'NR==1 {print; exit}')"
    else
      # Backward compatibility for pre-process-group PID files.
      PID="$(awk 'NR==1 {print; exit}' "$pidfile" 2>/dev/null || true)"
    fi

    if [[ "$PGID" =~ ^[0-9]+$ && "$RUN_ID" == "$(basename "$pidfile" .pid)" &&
          -n "$PROCESS_START" && -f "$RUNS_DIR/$RUN_ID.meta" ]]; then
      echo "terminating factory run group $PGID ($RUN_ID)"
      if python3 "$KIT_DIR/scripts/lib/process-identity.py" terminate \
          --runs-dir "$RUNS_DIR" --run-id "$RUN_ID" --timeout 2 >/dev/null; then
        REMOVE_PID_FILE=1
      else
        echo "WARNING: refusing stale or mismatched factory PID record, or surviving group: $pidfile" >&2
      fi
    elif [[ "$PID" =~ ^[0-9]+$ && "$PID" == "$(awk 'NR==1 {print; exit}' "$pidfile" 2>/dev/null)" ]]; then
      if kill -0 "$PID" 2>/dev/null; then
        echo "terminating legacy factory run pid $PID ($(basename "$pidfile"))"
        kill "$PID" 2>/dev/null || true
        sleep 2
        kill -0 "$PID" 2>/dev/null && kill -9 "$PID" 2>/dev/null || true
      fi
      kill -0 "$PID" 2>/dev/null || REMOVE_PID_FILE=1
    else
      echo "WARNING: malformed factory PID record retained: $pidfile" >&2
    fi
    [[ "$REMOVE_PID_FILE" -eq 0 ]] || rm -f "$pidfile"
  done
fi

if [[ -d "$RUNS_DIR" ]]; then
  for wrapper_file in "$RUNS_DIR"/*.wrapper; do
    [[ -e "$wrapper_file" ]] || continue
    WRAPPER_LINES="$(wc -l < "$wrapper_file" | tr -d ' ')"
    if [[ ! -f "$wrapper_file" || -L "$wrapper_file" ||
          ( "$WRAPPER_LINES" -ne 6 && "$WRAPPER_LINES" -ne 7 ) ]] ||
       ! grep -Eq '^run_id=[A-Za-z0-9._-]{1,200}$' "$wrapper_file" ||
       ! grep -Eq '^wrapper_pid=[1-9][0-9]*$' "$wrapper_file" ||
       { [[ "$WRAPPER_LINES" -eq 7 ]] &&
         ! grep -Eq '^wrapper_pgid=[1-9][0-9]*$' "$wrapper_file"; } ||
       ! grep -Eq '^wrapper_process_start=.+$' "$wrapper_file" ||
       ! grep -Eq '^heartbeat_pid=[1-9][0-9]*$' "$wrapper_file" ||
       ! grep -Eq '^heartbeat_pgid=[1-9][0-9]*$' "$wrapper_file" ||
       ! grep -Eq '^heartbeat_process_start=.+$' "$wrapper_file"; then
      echo "WARNING: malformed factory wrapper record retained: $wrapper_file" >&2
      continue
    fi
    WRAPPER_RUN_ID="$(sed -n 's/^run_id=//p' "$wrapper_file" | awk 'NR==1 {print; exit}')"
    WRAPPER_PID="$(sed -n 's/^wrapper_pid=//p' "$wrapper_file" | awk 'NR==1 {print; exit}')"
    WRAPPER_RECORDED_PGID="$(sed -n 's/^wrapper_pgid=//p' "$wrapper_file" | awk 'NR==1 {print; exit}')"
    WRAPPER_PROCESS_START="$(sed -n 's/^wrapper_process_start=//p' "$wrapper_file" | awk 'NR==1 {print; exit}')"
    HEARTBEAT_PID="$(sed -n 's/^heartbeat_pid=//p' "$wrapper_file" | awk 'NR==1 {print; exit}')"
    HEARTBEAT_PGID="$(sed -n 's/^heartbeat_pgid=//p' "$wrapper_file" | awk 'NR==1 {print; exit}')"
    HEARTBEAT_PROCESS_START="$(sed -n 's/^heartbeat_process_start=//p' "$wrapper_file" | awk 'NR==1 {print; exit}')"
    WRAPPER_CURRENT_START="$(process_start_identity "$WRAPPER_PID")"
    WRAPPER_PGID="$(ps -o pgid= -p "$WRAPPER_PID" 2>/dev/null | awk '{$1=$1; print; exit}')"
    WRAPPER_EXPECTED_PGID=""
    if [[ -n "$WRAPPER_RECORDED_PGID" ]]; then
      [[ "$WRAPPER_RECORDED_PGID" != "$WRAPPER_PID" ]] ||
        WRAPPER_EXPECTED_PGID="$WRAPPER_PID"
    elif [[ "$WRAPPER_CURRENT_START" == "$WRAPPER_PROCESS_START" &&
            "$WRAPPER_PGID" == "$WRAPPER_PID" ]]; then
      WRAPPER_EXPECTED_PGID="$WRAPPER_PID"
    elif [[ -z "$WRAPPER_CURRENT_START" ]] && process_group_alive "$WRAPPER_PID"; then
      echo "WARNING: run wrapper leader is absent while its group remains live: pid=$WRAPPER_PID" >&2
      continue
    fi
    if [[ "$WRAPPER_RUN_ID" != "$(basename "$wrapper_file" .wrapper)" ]]; then
      echo "WARNING: malformed factory wrapper record retained: $wrapper_file" >&2
      continue
    fi
    if ! terminate_exact_process "$HEARTBEAT_PID" "$HEARTBEAT_PROCESS_START" \
        "$HEARTBEAT_PGID" "dispatcher heartbeat"; then
      echo "WARNING: dispatcher heartbeat retained for operator reconciliation: $wrapper_file" >&2
      continue
    fi
    if terminate_exact_process "$WRAPPER_PID" "$WRAPPER_PROCESS_START" \
        "$WRAPPER_EXPECTED_PGID" \
        "run wrapper"; then
      rm -f "$wrapper_file" "$RUNS_DIR/$WRAPPER_RUN_ID.pid"
    else
      echo "WARNING: run wrapper retained for operator reconciliation: $wrapper_file" >&2
    fi
  done
fi

if [[ "$HELD_LAUNCH_LOCK" -eq 1 ]]; then
  if ! compgen -G "$RUNS_DIR/*.pid" >/dev/null; then
    PROVIDER_RECOVERY="$(python3 - "$PROVIDER_LOCK" "$RUNS_DIR" <<'PY'
import hashlib
import os
import pathlib
import re
import secrets
import stat
import subprocess
import sys
import time

lock, runs = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
if not lock.exists() and not lock.is_symlink():
    print("absent")
    raise SystemExit
try:
    lock_stat = lock.lstat()
    runs_stat = runs.lstat()
    if (stat.S_ISLNK(lock_stat.st_mode) or not stat.S_ISDIR(lock_stat.st_mode) or
            stat.S_ISLNK(runs_stat.st_mode) or not stat.S_ISDIR(runs_stat.st_mode)):
        raise ValueError
    owner = lock / "owner"
    owner_stat = owner.lstat()
    if (stat.S_ISLNK(owner_stat.st_mode) or
            not stat.S_ISREG(owner_stat.st_mode) or owner_stat.st_nlink != 1):
        raise ValueError
    owner_bytes = owner.read_bytes()
    if sorted(entry.name for entry in lock.iterdir()) != ["owner"]:
        raise ValueError
    lines = owner_bytes.decode("utf-8").splitlines()
    if (len(lines) != 3 or not re.fullmatch(r"pid=[1-9][0-9]*", lines[0]) or
            not lines[1].startswith("process_start=") or len(lines[1]) == 14 or
            not re.fullmatch(r"token=[0-9a-f]{32}", lines[2])):
        raise ValueError
    pid = int(lines[0][4:])
    process_start = lines[1][14:]
    try:
        os.kill(pid, 0)
        live = True
    except ProcessLookupError:
        live = False
    except PermissionError:
        print("ambiguous")
        raise SystemExit
    if live:
        try:
            current = subprocess.run(
                ["ps", "-o", "lstart=", "-p", str(pid)],
                check=False, capture_output=True, text=True,
            ).stdout
            current = " ".join(current.split())
        except OSError:
            current = ""
        if not current:
            print("ambiguous")
            raise SystemExit
        if current == process_start:
            print("live")
            raise SystemExit
    expected = (
        lock_stat.st_dev, lock_stat.st_ino,
        owner_stat.st_dev, owner_stat.st_ino,
        hashlib.sha256(owner_bytes).digest(),
    )
    lock_now, owner_now = lock.lstat(), owner.lstat()
    actual = (
        lock_now.st_dev, lock_now.st_ino,
        owner_now.st_dev, owner_now.st_ino,
        hashlib.sha256(owner.read_bytes()).digest(),
    )
    if actual != expected:
        print("changed")
        raise SystemExit
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    destination = runs / f".provider-lock-stale-{stamp}-{secrets.token_hex(8)}"
    os.rename(lock, destination)
    moved_owner = destination / "owner"
    moved_lock_stat, moved_owner_stat = destination.lstat(), moved_owner.lstat()
    moved = (
        moved_lock_stat.st_dev, moved_lock_stat.st_ino,
        moved_owner_stat.st_dev, moved_owner_stat.st_ino,
        hashlib.sha256(moved_owner.read_bytes()).digest(),
    )
    if moved != expected:
        if not lock.exists() and not lock.is_symlink():
            os.rename(destination, lock)
        print("changed")
        raise SystemExit
    runs_fd = os.open(runs, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(runs_fd)
    finally:
        os.close(runs_fd)
    print("quarantined")
except (OSError, UnicodeError, ValueError):
    print("malformed")
PY
)"
    case "$PROVIDER_RECOVERY" in
      absent) ;;
      quarantined) echo "quarantined stale provider lock under $RUNS_DIR" ;;
      live) echo "WARNING: live provider lock retained" >&2 ;;
      *) echo "WARNING: $PROVIDER_RECOVERY provider lock retained for operator reconciliation" >&2 ;;
    esac
    for _lease_lock_try in $(seq 1 100); do
      mkdir "$DISPATCH_LOCK" 2>/dev/null && { HELD_DISPATCH_LOCK=1; break; }
      sleep 0.1
    done
    if [[ "$HELD_DISPATCH_LOCK" -eq 1 ]]; then
      factory_dispatch_clear_leases "$REPO_ROOT" ||
        echo "WARNING: dispatcher leases are unsafe and were retained" >&2
      rmdir "$DISPATCH_LOCK"
      HELD_DISPATCH_LOCK=0
    else
      echo "WARNING: dispatcher lease lock stuck; leases were retained" >&2
    fi
  fi
  rmdir "$LAUNCH_LOCK"
  HELD_LAUNCH_LOCK=0
fi

# Disable factory schedules (label convention: com.factory.*)
if [[ "${FACTORY_SKIP_SCHEDULE_STOP:-0}" != "1" ]] && command -v launchctl >/dev/null; then
  for job in $(launchctl list 2>/dev/null | awk '/com\.factory\./ {print $3}'); do
    [[ "$job" == "com.factory.spend-rollup" ]] && continue
    launchctl bootout "gui/$(id -u)/$job" 2>/dev/null && echo "stopped schedule: $job"
    launchctl disable "gui/$(id -u)/$job" 2>/dev/null && echo "disabled schedule: $job"
  done
fi

echo "Factory stopped. To resume: rm $FACTORY_DIR/KILL and re-enable schedules."
