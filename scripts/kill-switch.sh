#!/usr/bin/env bash
# kill-switch.sh — stop the factory. Stops, does not destroy:
#   1. Drops a KILL file anchored at the product repo's factory/ dir
#      (same anchor run-agent.sh checks — not $PWD-dependent).
#   2. Terminates factory-launched runs precisely, via the PID files the run
#      wrapper writes (factory/runs/*.pid) — NOT every claude/codex process
#      on the machine; the operator's own sessions are untouched.
#   3. Disables factory launchd schedules (com.factory.*, rollup exempt).
# Key rotation is NOT here — that is incident response for a suspected leak
# (see runbooks/operator.md). Console spend caps stay active regardless.
#
# Usage: kill-switch.sh [repo-root]   (default: git root of cwd)
# Resume: rm <repo-root>/factory/KILL and re-enable schedules.
set -uo pipefail

REPO_ROOT="${1:-$(git rev-parse --show-toplevel 2>/dev/null || echo "$PWD")}"
FACTORY_DIR="$REPO_ROOT/factory"
RUNS_DIR="$FACTORY_DIR/runs"

mkdir -p "$FACTORY_DIR"
date -u +"stopped_at=%FT%TZ" > "$FACTORY_DIR/KILL"
echo "KILL file written: $FACTORY_DIR/KILL (run-agent.sh will refuse new runs)"

if [[ -d "$RUNS_DIR" ]]; then
  for pidfile in "$RUNS_DIR"/*.pid; do
    [[ -e "$pidfile" ]] || continue
    PID="$(cat "$pidfile" 2>/dev/null || true)"
    if [[ -n "$PID" ]] && kill -0 "$PID" 2>/dev/null; then
      echo "terminating factory run pid $PID ($(basename "$pidfile"))"
      kill "$PID" 2>/dev/null || true
      sleep 2
      kill -0 "$PID" 2>/dev/null && kill -9 "$PID" 2>/dev/null || true
    fi
    rm -f "$pidfile"
  done
fi

# Disable factory schedules (label convention: com.factory.*)
if command -v launchctl >/dev/null; then
  for job in $(launchctl list 2>/dev/null | awk '/com\.factory\./ {print $3}'); do
    [[ "$job" == "com.factory.spend-rollup" ]] && continue
    launchctl bootout "gui/$(id -u)/$job" 2>/dev/null && echo "stopped schedule: $job"
    launchctl disable "gui/$(id -u)/$job" 2>/dev/null && echo "disabled schedule: $job"
  done
fi

echo "Factory stopped. To resume: rm $FACTORY_DIR/KILL and re-enable schedules."
