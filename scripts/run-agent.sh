#!/usr/bin/env bash
# run-agent.sh — the only sanctioned way to start a factory agent run.
# Enforces per-run, per-ticket, and daily budgets; serializes cap checks with a
# lock; anchors run artifacts to the caller's product root while routing the
# ledger to the main working tree; logs every run to the cost ledger; enforces
# the cross-family role→adapter mapping; rejects overlapping ticket+role runs.
#
# Usage:
#   run-agent.sh --role builder --ticket T-123 --prompt-file factory/roles/builder.md \
#                [--adapter claude-code] [--workdir /path/to/worktree] -- "task text"
#
# Envelope (factory/ENVELOPE.env at the repo root):
#   PER_RUN_BUDGET_USD, PER_TICKET_BUDGET_USD, PER_RUN_MAX_TURNS,
#   PER_RUN_TIMEOUT_MIN, DAILY_CAP_USD
set -euo pipefail

KIT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# --- anchor factory state to the repo root, never to $PWD ---
REPO_ROOT="${FACTORY_ROOT:-$(git rev-parse --show-toplevel 2>/dev/null || echo "$PWD")}"
FACTORY_DIR="$REPO_ROOT/factory"

# A linked worktree has its own copy of factory/, but costs must have one
# source of truth. Map the caller's FACTORY_ROOT to the same relative path
# under the main working tree. An explicit FACTORY_LEDGER always wins.
canonical_ledger() {
  local root="$1" root_abs worktree_root common_dir main_root relative
  root_abs="$(cd "$root" 2>/dev/null && pwd -P || printf '%s' "$root")"
  if worktree_root="$(git -C "$root" rev-parse --show-toplevel 2>/dev/null)" &&
     common_dir="$(git -C "$root" rev-parse --git-common-dir 2>/dev/null)"; then
    worktree_root="$(cd "$worktree_root" && pwd -P)"
    # A relative --git-common-dir is relative to git's cwd ($root), NOT the
    # worktree root — resolving against the wrong base broke main-clone
    # subdirectory roots (e.g. FACTORY_ROOT=<repo>/conformance).
    case "$common_dir" in
      /*) ;;
      *) common_dir="$root_abs/$common_dir" ;;
    esac
    if ! main_root="$(cd "$common_dir/.." 2>/dev/null && pwd -P)"; then
      printf '%s/factory/ledger.csv\n' "$root_abs"
      return
    fi
    if [[ "$root_abs" == "$worktree_root" ]]; then
      relative=""
    elif [[ "$root_abs" == "$worktree_root/"* ]]; then
      relative="${root_abs#"$worktree_root/"}"
    else
      printf '%s/factory/ledger.csv\n' "$root_abs"
      return
    fi
    printf '%s%s/factory/ledger.csv\n' "$main_root" "${relative:+/$relative}"
  else
    printf '%s/factory/ledger.csv\n' "$root_abs"
  fi
}

LEDGER="${FACTORY_LEDGER:-$(canonical_ledger "$REPO_ROOT")}"
ENV_FILE="${FACTORY_ENVELOPE:-$FACTORY_DIR/ENVELOPE.env}"
LEDGER_DIR="$(dirname "$LEDGER")"
LOCK_DIR="$LEDGER_DIR/.ledger.lock"
RUNS_DIR="$FACTORY_DIR/runs"
ACTIVE_RUN_FILE=""
ACTIVE_RUN_TEMP=""
OWNS_ACTIVE_RUN=0
HELD_LEDGER_LOCK=0
HELD_GLOBAL_LOCK=0

cleanup() {
  [[ "$HELD_GLOBAL_LOCK" -eq 0 ]] || rmdir "$GLOBAL_LOCK" 2>/dev/null || true
  [[ "$HELD_LEDGER_LOCK" -eq 0 ]] || rmdir "$LOCK_DIR" 2>/dev/null || true
  [[ -z "$ACTIVE_RUN_TEMP" ]] || rm -f "$ACTIVE_RUN_TEMP"
  if [[ "$OWNS_ACTIVE_RUN" -eq 1 ]]; then
    rm -f "$ACTIVE_RUN_FILE"
  fi
}
trap cleanup EXIT

ROLE="" TICKET="" PROMPT_FILE="" ADAPTER="" WORKDIR="$PWD"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --role) ROLE="$2"; shift 2;;
    --ticket) TICKET="$2"; shift 2;;
    --prompt-file) PROMPT_FILE="$2"; shift 2;;
    --adapter) ADAPTER="$2"; shift 2;;
    --workdir) WORKDIR="$2"; shift 2;;
    --) shift; break;;
    *) echo "unknown arg: $1" >&2; exit 2;;
  esac
done
TASK="${*:-}"
[[ -n "$ROLE" && -n "$TICKET" && -n "$TASK" ]] || { echo "missing required args" >&2; exit 2; }
[[ -f "$ENV_FILE" ]] || { echo "envelope not found: $ENV_FILE — fill ENVELOPE.md and write ENVELOPE.env first" >&2; exit 3; }
# shellcheck disable=SC1090
source "$ENV_FILE"
PER_TICKET_BUDGET_USD="${PER_TICKET_BUDGET_USD:-$PER_RUN_BUDGET_USD}"

# --- optional machine-level cap across all factories on this machine ---
# ~/.factory/global.env defines GLOBAL_DAILY_CAP_USD; every run on the machine
# then also reserves against ~/.factory/global-ledger.csv, so N projects can't
# multiply the daily budget silently. Absent file = single-project behavior.
GLOBAL_ENV="${FACTORY_GLOBAL_ENV:-$HOME/.factory/global.env}"
GLOBAL_LEDGER="" GLOBAL_LOCK=""
if [[ -f "$GLOBAL_ENV" ]]; then
  # shellcheck disable=SC1090
  source "$GLOBAL_ENV"
  GLOBAL_LEDGER="${GLOBAL_LEDGER:-$(dirname "$GLOBAL_ENV")/global-ledger.csv}"
  GLOBAL_LOCK="$(dirname "$GLOBAL_ENV")/.ledger.lock"
  [[ -n "${GLOBAL_DAILY_CAP_USD:-}" ]] || { echo "global env $GLOBAL_ENV exists but GLOBAL_DAILY_CAP_USD is unset" >&2; exit 3; }
fi

# --- cross-family role→adapter mapping (mechanical, not a prompt rule) ---
# Override only via FACTORY_ADAPTER_OVERRIDE (used for mock in kit tests).
# 2026-07-13 flip (operator decision): production roles (planner, builder,
# narrator) run on codex; checking roles (spec-linter, test-author, reviewer)
# run on claude-code. Rationale: the token-heavy runs land on the cheaper
# family while the cross-family invariant — checkers never share a family
# with producers — is preserved.
case "$ROLE" in
  builder|planner) DEFAULT_ADAPTER="codex";;
  test-author|reviewer|spec-linter) DEFAULT_ADAPTER="claude-code";;
  narrator) DEFAULT_ADAPTER="codex";;
  *) echo "unknown role: $ROLE" >&2; exit 2;;
esac
if [[ -n "${FACTORY_ADAPTER_OVERRIDE:-}" ]]; then
  ADAPTER="$FACTORY_ADAPTER_OVERRIDE"
elif [[ -z "$ADAPTER" ]]; then
  ADAPTER="$DEFAULT_ADAPTER"
elif [[ "$ADAPTER" != "$DEFAULT_ADAPTER" ]]; then
  echo "role '$ROLE' must run on adapter '$DEFAULT_ADAPTER' (cross-family rule); got '$ADAPTER'" >&2
  exit 2
fi

# --- kill switch check (anchored) ---
if [[ -f "$FACTORY_DIR/KILL" ]]; then
  echo "KILL file present ($FACTORY_DIR/KILL) — factory is stopped. Remove it to resume." >&2
  exit 4
fi

ADAPTER_SH="$KIT_DIR/scripts/adapters/$ADAPTER.sh"
[[ -x "$ADAPTER_SH" ]] || { echo "no adapter: $ADAPTER_SH" >&2; exit 6; }

mkdir -p "$FACTORY_DIR" "$RUNS_DIR" "$LEDGER_DIR"
[[ -f "$LEDGER" ]] || echo "date,time,ticket,role,adapter,prompt_version,turns,cost_usd,exit_status" > "$LEDGER"
TODAY="$(date +%F)"

# --- serialized cap check with budget reservation ---
# mkdir is atomic: it is the lock. Reservation counts this run's full per-run
# budget against the caps, so N concurrent runs cannot all squeeze past.
for i in $(seq 1 50); do mkdir "$LOCK_DIR" 2>/dev/null && { HELD_LEDGER_LOCK=1; break; }; sleep 0.2; [[ $i -eq 50 ]] && { echo "ledger lock stuck — see runbook" >&2; exit 8; }; done

# --- one live run per ticket+role across all linked worktrees ---
# Acquisition is serialized by the ledger lock above, including stale cleanup.
ACTIVE_RUNS_DIR="$LEDGER_DIR/.active-runs"
GUARD_KEY="$(printf '%s.%s' "$TICKET" "$ROLE" | tr -c 'A-Za-z0-9._-' '_')"
ACTIVE_RUN_FILE="$ACTIVE_RUNS_DIR/$GUARD_KEY.pid"
ACTIVE_RUN_TEMP="$ACTIVE_RUNS_DIR/.$GUARD_KEY.$$.pid"
mkdir -p "$ACTIVE_RUNS_DIR"
echo "$$" > "$ACTIVE_RUN_TEMP"
while ! ln "$ACTIVE_RUN_TEMP" "$ACTIVE_RUN_FILE" 2>/dev/null; do
  EXISTING_PID="$(cat "$ACTIVE_RUN_FILE" 2>/dev/null || true)"
  if [[ "$EXISTING_PID" =~ ^[0-9]+$ ]] && kill -0 "$EXISTING_PID" 2>/dev/null; then
    echo "live run already exists for $TICKET role $ROLE (wrapper pid $EXISTING_PID) — refusing duplicate launch" >&2
    exit 7
  fi
  rm -f "$ACTIVE_RUN_FILE"
done
OWNS_ACTIVE_RUN=1
rm -f "$ACTIVE_RUN_TEMP"
ACTIVE_RUN_TEMP=""

SPENT_TODAY="$(awk -F, -v d="$TODAY" 'NR>1 && $1==d {s+=$8} END {printf "%.4f", s+0}' "$LEDGER")"
SPENT_TICKET="$(awk -F, -v t="$TICKET" 'NR>1 && $3==t {s+=$8} END {printf "%.4f", s+0}' "$LEDGER")"
if awk -v s="$SPENT_TODAY" -v r="$PER_RUN_BUDGET_USD" -v cap="$DAILY_CAP_USD" 'BEGIN{exit !((s+r)>cap)}'; then
  echo "daily cap would be exceeded (spent \$$SPENT_TODAY + reserve \$$PER_RUN_BUDGET_USD > \$$DAILY_CAP_USD) — refusing. See runbooks/operator.md." >&2
  exit 5
fi
if awk -v s="$SPENT_TICKET" -v r="$PER_RUN_BUDGET_USD" -v cap="$PER_TICKET_BUDGET_USD" 'BEGIN{exit !((s+r)>cap)}'; then
  echo "ticket budget would be exceeded for $TICKET (spent \$$SPENT_TICKET + reserve \$$PER_RUN_BUDGET_USD > \$$PER_TICKET_BUDGET_USD) — move ticket to Blocked-Escalated." >&2
  exit 5
fi
# Global cap check + reservation (own lock, taken while holding the repo
# lock — lock order is always repo → global, so no deadlock is possible).
RUN_ID="$(date +%s)-$$"
if [[ -n "$GLOBAL_LEDGER" ]]; then
  mkdir -p "$(dirname "$GLOBAL_LEDGER")"
  [[ -f "$GLOBAL_LEDGER" ]] || echo "date,time,repo,ticket,role,adapter,prompt_version,turns,cost_usd,exit_status" > "$GLOBAL_LEDGER"
  for i in $(seq 1 50); do mkdir "$GLOBAL_LOCK" 2>/dev/null && { HELD_GLOBAL_LOCK=1; break; }; sleep 0.2; [[ $i -eq 50 ]] && { echo "global ledger lock stuck — see runbook" >&2; rmdir "$LOCK_DIR"; HELD_LEDGER_LOCK=0; exit 8; }; done
  SPENT_GLOBAL="$(awk -F, -v d="$TODAY" 'NR>1 && $1==d {s+=$9} END {printf "%.4f", s+0}' "$GLOBAL_LEDGER")"
  if awk -v s="$SPENT_GLOBAL" -v r="$PER_RUN_BUDGET_USD" -v cap="$GLOBAL_DAILY_CAP_USD" 'BEGIN{exit !((s+r)>cap)}'; then
    echo "MACHINE daily cap would be exceeded across all factories (spent \$$SPENT_GLOBAL + reserve \$$PER_RUN_BUDGET_USD > \$$GLOBAL_DAILY_CAP_USD) — refusing. See runbooks/operator.md." >&2
    rmdir "$GLOBAL_LOCK" "$LOCK_DIR"; HELD_GLOBAL_LOCK=0; HELD_LEDGER_LOCK=0; exit 5
  fi
  echo "$TODAY,$(date +%T),$REPO_ROOT,$TICKET,$ROLE,$ADAPTER,reserved,0,$PER_RUN_BUDGET_USD,reserved-$RUN_ID" >> "$GLOBAL_LEDGER"
  rmdir "$GLOBAL_LOCK"
  HELD_GLOBAL_LOCK=0
fi

# Reserve: write a provisional ledger row at full per-run budget; replaced with
# the real cost after the run. A crash leaves the conservative row in place.
echo "$TODAY,$(date +%T),$TICKET,$ROLE,$ADAPTER,reserved,0,$PER_RUN_BUDGET_USD,reserved-$RUN_ID" >> "$LEDGER"
rmdir "$LOCK_DIR"; HELD_LEDGER_LOCK=0

PROMPT_VERSION="unversioned"
[[ -n "$PROMPT_FILE" && -f "$PROMPT_FILE" ]] && PROMPT_VERSION="$(grep -m1 '^Version:' "$PROMPT_FILE" | awk '{print $2}' || echo unversioned)"

# --- run; record PID so the kill switch can target factory runs precisely ---
set +e
"$ADAPTER_SH" \
  --budget "$PER_RUN_BUDGET_USD" \
  --max-turns "$PER_RUN_MAX_TURNS" \
  --timeout-min "$PER_RUN_TIMEOUT_MIN" \
  --prompt-file "${PROMPT_FILE:-/dev/null}" \
  --workdir "$WORKDIR" \
  -- "$TASK" > "$RUNS_DIR/$RUN_ID.out" 2>&1 &
ADAPTER_PID=$!
echo "$ADAPTER_PID" > "$RUNS_DIR/$RUN_ID.pid"
wait "$ADAPTER_PID"
STATUS=$?
set -e
rm -f "$RUNS_DIR/$RUN_ID.pid"
RESULT="$(cat "$RUNS_DIR/$RUN_ID.out")"

METRICS_LINE="$(printf '%s\n' "$RESULT" | tail -n1)"
TURNS="$(sed -n 's/.*turns=\([0-9][0-9]*\).*/\1/p' <<<"$METRICS_LINE")"
COST="$(sed -n 's/.*cost_usd=\([0-9.][0-9.]*\).*/\1/p' <<<"$METRICS_LINE")"
# Unparsable cost: keep the conservative full-budget reservation and say so
# loudly — never silently log $0 against the caps.
if [[ -z "$COST" ]]; then
  echo "WARNING: run cost unparsable — ledger keeps conservative reservation of \$$PER_RUN_BUDGET_USD for this run. Reconcile with the provider console." >&2
  COST="$PER_RUN_BUDGET_USD"
  TURNS="${TURNS:-0}"
fi

# Replace the reservation row with the real result (under lock).
for i in $(seq 1 50); do mkdir "$LOCK_DIR" 2>/dev/null && { HELD_LEDGER_LOCK=1; break; }; sleep 0.2; done
TMP_LEDGER="$LEDGER.tmp.$$"
grep -v ",reserved-$RUN_ID\$" "$LEDGER" > "$TMP_LEDGER" || true
echo "$TODAY,$(date +%T),$TICKET,$ROLE,$ADAPTER,$PROMPT_VERSION,${TURNS:-0},$COST,$STATUS" >> "$TMP_LEDGER"
mv "$TMP_LEDGER" "$LEDGER"
rmdir "$LOCK_DIR"; HELD_LEDGER_LOCK=0

# Same replacement in the machine-global ledger, if configured.
if [[ -n "$GLOBAL_LEDGER" && -f "$GLOBAL_LEDGER" ]]; then
  for i in $(seq 1 50); do mkdir "$GLOBAL_LOCK" 2>/dev/null && { HELD_GLOBAL_LOCK=1; break; }; sleep 0.2; done
  TMP_GLOBAL="$GLOBAL_LEDGER.tmp.$$"
  grep -v ",reserved-$RUN_ID\$" "$GLOBAL_LEDGER" > "$TMP_GLOBAL" || true
  echo "$TODAY,$(date +%T),$REPO_ROOT,$TICKET,$ROLE,$ADAPTER,$PROMPT_VERSION,${TURNS:-0},$COST,$STATUS" >> "$TMP_GLOBAL"
  mv "$TMP_GLOBAL" "$GLOBAL_LEDGER"
  rmdir "$GLOBAL_LOCK"; HELD_GLOBAL_LOCK=0
fi

printf '%s\n' "$RESULT"
exit "$STATUS"
