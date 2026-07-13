#!/usr/bin/env bash
# next-stage.sh — mechanical stage sequencer. Given a ticket, computes the
# single legal next action from the cost ledger (successful runs) and the
# ticket file (recorded reviewer verdicts). The dispatcher calls this instead
# of reasoning about pipeline order: transition legality is mechanism, not
# prompt. Prints one of:
#   RUN <role>            — launch this role via run-agent.sh
#   FIX <builder|test-author> — reviewer requested changes; dispatcher picks
#                           which role per the feedback, then reviewer rerun
#   AWAIT-OPERATOR        — bundle posted; operator approval/merge is next
#   ESCALATE <reason>     — stop; a human decision is required
#   REFUSE <reason>       — bookkeeping incomplete; fix the record first
#
# Usage: next-stage.sh --ticket T-NNN   (FACTORY_ROOT anchors the factory dir)
set -euo pipefail

REPO_ROOT="${FACTORY_ROOT:-$(git rev-parse --show-toplevel 2>/dev/null || echo "$PWD")}"
FACTORY_DIR="$REPO_ROOT/factory"
LEDGER="${FACTORY_LEDGER:-$FACTORY_DIR/ledger.csv}"
TICKETS_DIR="$FACTORY_DIR/tickets"

TICKET=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --ticket) TICKET="$2"; shift 2;;
    *) echo "unknown arg: $1" >&2; exit 2;;
  esac
done
[[ -n "$TICKET" ]] || { echo "usage: next-stage.sh --ticket T-NNN" >&2; exit 2; }

TICKET_FILE="$TICKETS_DIR/$TICKET.md"
[[ -f "$TICKET_FILE" ]] || { echo "REFUSE no ticket file at $TICKET_FILE"; exit 1; }

# Successful (exit_status 0) runs per role, in ledger (completion) order.
# (cat the ledger defensively: a missing ledger means zero runs, not an error.)
count_ok() { { cat "$LEDGER" 2>/dev/null || true; } | awk -F, -v t="$TICKET" -v r="$1" 'NR>1 && $3==t && $4==r && $9=="0"' | wc -l | tr -d ' '; }
P="$(count_ok planner)"; TA="$(count_ok test-author)"; B="$(count_ok builder)"
R="$(count_ok reviewer)"; N="$(count_ok narrator)"

# Reviewer verdicts must be recorded on the ticket file by the dispatcher.
# Count them; they are the only non-ledger input, and we cross-check the
# count against the ledger so an unrecorded verdict blocks progress loudly.
A="$(grep -ciE 'reviewer.*(: *|verdict *:? *)APPROVE' "$TICKET_FILE" || true)"; A="${A:-0}"
RC="$(grep -ciE 'reviewer.*REQUEST CHANGES' "$TICKET_FILE" || true)"; RC="${RC:-0}"

if [[ "$P" -eq 0 ]]; then echo "RUN planner"; exit 0; fi
if [[ "$TA" -eq 0 ]]; then echo "RUN test-author"; exit 0; fi
if [[ "$B" -eq 0 ]]; then echo "RUN builder"; exit 0; fi
if [[ "$R" -eq 0 ]]; then echo "RUN reviewer"; exit 0; fi

if [[ "$R" -gt $((A + RC)) ]]; then
  echo "REFUSE reviewer ran $R time(s) but only $((A + RC)) verdict(s) are logged on $TICKET_FILE — record the verdict line (e.g. 'reviewer round $R: APPROVE' or 'reviewer round $R: REQUEST CHANGES — <reason>') before anything else"
  exit 1
fi

if [[ "$A" -ge 1 ]]; then
  if [[ "$N" -eq 0 ]]; then echo "RUN narrator"; exit 0; fi
  echo "AWAIT-OPERATOR bundle posted; operator approval + merge is the next step"
  exit 0
fi

# All recorded verdicts are REQUEST CHANGES.
if [[ "$RC" -ge 2 ]]; then
  echo "ESCALATE reviewer requested changes twice — two-round limit reached, operator decides"
  exit 0
fi

# One rejection round: was a fix (test-author or builder success) completed
# after the last successful reviewer run? Ledger order = completion order.
FIX_AFTER="$(awk -F, -v t="$TICKET" '
  NR>1 && $3==t && $9=="0" {
    if ($4=="reviewer") { last_r=NR; fix=0 }
    else if (($4=="builder" || $4=="test-author") && last_r>0) fix=1
  }
  END { print fix+0 }' "$LEDGER")"

if [[ "$FIX_AFTER" -eq 1 ]]; then
  echo "RUN reviewer"
else
  echo "FIX builder-or-test-author — reviewer round 1 requested changes; launch test-author if the tests were faulted, builder if the code was; then reviewer again"
fi
