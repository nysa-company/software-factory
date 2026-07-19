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
      MANIFEST_RUN_ID="$(sed -n 's/^run_id=//p' "$RUNS_DIR/$RUN_ID.meta" | awk 'NR==1 {print; exit}')"
      MANIFEST_PGID="$(sed -n 's/^pgid=//p' "$RUNS_DIR/$RUN_ID.meta" | awk 'NR==1 {print; exit}')"
      CURRENT_PGID="$(ps -o pgid= -p "$PID" 2>/dev/null | tr -d ' ')"
      CURRENT_START="$(ps -o lstart= -p "$PID" 2>/dev/null | awk '{$1=$1; print; exit}')"
      if [[ "$MANIFEST_RUN_ID" != "$RUN_ID" || "$MANIFEST_PGID" != "$PGID" ||
            "$CURRENT_PGID" != "$PGID" || "$CURRENT_START" != "$PROCESS_START" ]]; then
        echo "WARNING: refusing stale or mismatched factory PID record: $pidfile" >&2
        continue
      fi
      if kill -0 -- "-$PGID" 2>/dev/null; then
        echo "terminating factory run group $PGID ($RUN_ID)"
        kill -TERM -- "-$PGID" 2>/dev/null || true
        sleep 2
        if kill -0 -- "-$PGID" 2>/dev/null; then
          kill -KILL -- "-$PGID" 2>/dev/null || true
          for _drain_try in $(seq 1 100); do
            kill -0 -- "-$PGID" 2>/dev/null || break
            sleep 0.05
          done
        fi
      fi
      if kill -0 -- "-$PGID" 2>/dev/null; then
        echo "WARNING: process group $PGID survived; retaining $pidfile" >&2
      else
        REMOVE_PID_FILE=1
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
