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
# Review rounds are semantic, not ledger-row ordinals: the next round is the
# number of recorded APPROVE/REQUEST CHANGES verdicts plus one. Every
# successful reviewer row must still resolve to either a verdict or an
# explicit void note:
#   OPERATOR NOTE: reviewer run <ledger ordinal> void — duplicate
# A void note discounts that successful reviewer row from the cross-check and
# fix-order calculation. This keeps an overlap from renumbering or wedging the
# pipeline without letting an unrecorded verdict disappear silently. Crashed
# (nonzero-status) runs never enter the successful-run count.
#
# Usage: next-stage.sh --ticket T-NNN   (FACTORY_ROOT anchors the factory dir)
set -euo pipefail

REPO_ROOT="${FACTORY_ROOT:-$(git rev-parse --show-toplevel 2>/dev/null || echo "$PWD")}"
FACTORY_DIR="$REPO_ROOT/factory"

canonical_ledger() {
  local root="$1" root_abs worktree_root common_dir main_root relative
  root_abs="$(cd "$root" 2>/dev/null && pwd -P || printf '%s' "$root")"
  if worktree_root="$(git -C "$root" rev-parse --show-toplevel 2>/dev/null)" &&
     common_dir="$(git -C "$root" rev-parse --git-common-dir 2>/dev/null)"; then
    worktree_root="$(cd "$worktree_root" && pwd -P)"
    case "$common_dir" in
      /*) ;;
      *) common_dir="$worktree_root/$common_dir" ;;
    esac
    main_root="$(cd "$common_dir/.." && pwd -P)"
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
# Count them; they are the only stage input outside the ledger.
A="$(grep -ciE 'reviewer.*(: *|verdict *:? *)APPROVE' "$TICKET_FILE" || true)"; A="${A:-0}"
RC="$(grep -ciE 'reviewer.*REQUEST CHANGES' "$TICKET_FILE" || true)"; RC="${RC:-0}"
VERDICTS=$((A + RC))

# A void note names the one-based ordinal among successful reviewer rows.
# Ignore duplicate notes, out-of-range ordinals, and prose that does not match
# the exact auditable form documented above.
VOID_DATA="$(awk -v max="$R" '
  {
    line=tolower($0)
    if (line ~ /^[[:space:]]*operator note:[[:space:]]*reviewer run[[:space:]]*[0-9]+[[:space:]]+void[[:space:]]*[-—][[:space:]]*duplicate[[:space:]]*$/) {
      sub(/^.*reviewer run[[:space:]]*/, "", line)
      sub(/[[:space:]].*$/, "", line)
      n=line+0
      if (n>=1 && n<=max && !seen[n]++) {
        count++
        list=list (list ? "," : "") n
      }
    }
  }
  END { print count+0 "|" list }
' "$TICKET_FILE")"
VOID_COUNT="${VOID_DATA%%|*}"
VOID_RUNS="${VOID_DATA#*|}"
REVIEWER_RUNS=$((R - VOID_COUNT))

if [[ "$P" -eq 0 ]]; then echo "RUN planner"; exit 0; fi
if [[ "$TA" -eq 0 ]]; then echo "RUN test-author"; exit 0; fi
if [[ "$B" -eq 0 ]]; then echo "RUN builder"; exit 0; fi
if [[ "$REVIEWER_RUNS" -eq 0 ]]; then echo "RUN reviewer"; exit 0; fi

if [[ "$REVIEWER_RUNS" -gt "$VERDICTS" ]]; then
  echo "REFUSE reviewer has $REVIEWER_RUNS non-void successful run(s) but only $VERDICTS verdict(s) are logged on $TICKET_FILE — record the missing verdict, or mark a duplicate successful row with 'OPERATOR NOTE: reviewer run <ledger ordinal> void — duplicate'"
  exit 1
fi
if [[ "$REVIEWER_RUNS" -lt "$VERDICTS" ]]; then
  echo "REFUSE ticket logs $VERDICTS reviewer verdict(s) but the ledger has only $REVIEWER_RUNS non-void successful reviewer run(s) — correct the ticket bookkeeping"
  exit 1
fi

if [[ "$A" -ge 1 ]]; then
  if [[ "$N" -eq 0 ]]; then echo "RUN narrator"; exit 0; fi
  echo "AWAIT-OPERATOR bundle posted; operator approval + merge is the next step"
  exit 0
fi

# All recorded verdicts are REQUEST CHANGES.
if [[ "$RC" -ge 2 ]]; then
  # Operator override: after the two-round limit, the operator can authorize
  # exactly one extra reviewer round by recording a ticket line of the form
  #   OPERATOR AUTHORIZATION: reviewer round <N>
  # where N is the next semantic round (recorded verdicts + 1). The dispatcher may
  # never write this line on its own initiative — only on an explicit
  # operator instruction, which the escalation that got the operator here
  # provides the audit trail for.
  NEXT_ROUND=$((VERDICTS + 1))
  AUTH="$(grep -ciE "^[[:space:]]*OPERATOR AUTHORIZATION:[[:space:]]*reviewer round[[:space:]]*$NEXT_ROUND([[:space:]]|$)" "$TICKET_FILE" || true)"; AUTH="${AUTH:-0}"
  if [[ "$AUTH" -ge 1 ]]; then
    echo "RUN reviewer"
    exit 0
  fi
  echo "ESCALATE reviewer requested changes twice — two-round limit reached, operator decides (an extra round needs an 'OPERATOR AUTHORIZATION: reviewer round $NEXT_ROUND' line on the ticket, written on explicit operator instruction)"
  exit 0
fi

# One rejection round: was a fix (test-author or builder success) completed
# after the last successful reviewer run? Ledger order = completion order.
FIX_AFTER="$(awk -F, -v t="$TICKET" -v void_list="$VOID_RUNS" '
  BEGIN { voids="," void_list ","; reviewer_run=0 }
  NR>1 && $3==t && $9=="0" {
    if ($4=="reviewer") {
      reviewer_run++
      if (index(voids, "," reviewer_run ",")==0) { last_r=NR; fix=0 }
    }
    else if (($4=="builder" || $4=="test-author") && last_r>0) fix=1
  }
  END { print fix+0 }' "$LEDGER")"

if [[ "$FIX_AFTER" -eq 1 ]]; then
  echo "RUN reviewer"
else
  echo "FIX builder-or-test-author — reviewer round 1 requested changes; launch test-author if the tests were faulted, builder if the code was; then reviewer again"
fi
