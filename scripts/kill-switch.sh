#!/usr/bin/env bash
# kill-switch.sh — stop the factory. Stops, does not destroy:
#   1. Drops a KILL file that run-agent.sh checks before every run.
#   2. Terminates running claude/codex CLI processes.
#   3. Disables factory launchd/cron schedules (spend rollup keeps running).
# Key rotation is NOT here — that is incident response for a suspected leak
# (see runbooks/operator.md). Console spend caps stay active regardless.
#
# Resume: remove factory/KILL and re-enable schedules.
set -uo pipefail

FACTORY_DIR="${1:-$PWD/factory}"
mkdir -p "$FACTORY_DIR"
date -u +"stopped_at=%FT%TZ" > "$FACTORY_DIR/KILL"
echo "KILL file written: $FACTORY_DIR/KILL (run-agent.sh will refuse new runs)"

for proc in claude codex; do
  PIDS="$(pgrep -x "$proc" || true)"
  if [[ -n "$PIDS" ]]; then
    echo "terminating $proc: $PIDS"
    kill $PIDS 2>/dev/null || true
    sleep 3
    pgrep -x "$proc" >/dev/null && kill -9 $(pgrep -x "$proc") 2>/dev/null || true
  fi
done

# Disable factory schedules (label convention: com.factory.*)
if command -v launchctl >/dev/null; then
  for job in $(launchctl list 2>/dev/null | awk '/com\.factory\./ {print $3}'); do
    [[ "$job" == "com.factory.spend-rollup" ]] && continue
    launchctl disable "gui/$(id -u)/$job" 2>/dev/null && echo "disabled schedule: $job"
  done
fi

echo "Factory stopped. To resume: rm $FACTORY_DIR/KILL and re-enable schedules."
