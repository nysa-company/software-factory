#!/usr/bin/env bash
# next-stage.sh — mechanical stage sequencer. Given a ticket, computes the
# single legal next action from the cost ledger (successful runs) and the
# ticket file (recorded reviewer verdicts). The dispatcher calls this instead
# of reasoning about pipeline order: transition legality is mechanism, not
# prompt. Prints one of:
#   RUN <role>            — launch this role via the factory-launch run route
#   FIX <builder|test-author> — reviewer requested changes; dispatcher picks
#                           which role per the feedback, then reviewer rerun
#   AWAIT-OPERATOR        — bundle posted; operator approval/merge is next
#   AWAIT-MERGE           — reserved for a future trusted approval boundary
#   COMPLETE              — attested Done is on protected main; release lease
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
export PYTHONDONTWRITEBYTECODE=1

TICKET="" LEASE_ID="" WORKDIR=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --ticket) TICKET="$2"; shift 2;;
    --lease) LEASE_ID="$2"; shift 2;;
    --workdir) WORKDIR="$2"; shift 2;;
    *) echo "unknown arg: $1" >&2; exit 2;;
  esac
done
[[ -n "$TICKET" ]] || { echo "usage: next-stage.sh --ticket T-NNN" >&2; exit 2; }
[[ "$TICKET" =~ ^T-[0-9]+$ ]] || { echo "invalid ticket identifier" >&2; exit 2; }

KIT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
# shellcheck disable=SC1091
source "$KIT_DIR/scripts/lib/kit-pin.sh"
# shellcheck disable=SC1091
source "$KIT_DIR/scripts/lib/dispatch-leases.sh"
REPO_ROOT="${FACTORY_ROOT:-$(git rev-parse --show-toplevel 2>/dev/null || echo "$PWD")}"
FACTORY_DIR="$REPO_ROOT/factory"
CONTENT_ROOT="${WORKDIR:-$REPO_ROOT}"
if ! factory_validate_runtime_overrides; then
  echo "REFUSE $FACTORY_RUNTIME_OVERRIDE_ERROR"
  exit 1
fi

canonical_factory_file() {
  local root="$1" name="$2" root_abs worktree_root common_dir main_root relative
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
      printf '%s/factory/%s\n' "$root_abs" "$name"
      return
    fi
    if [[ "$root_abs" == "$worktree_root" ]]; then
      relative=""
    elif [[ "$root_abs" == "$worktree_root/"* ]]; then
      relative="${root_abs#"$worktree_root/"}"
    else
      printf '%s/factory/%s\n' "$root_abs" "$name"
      return
    fi
    printf '%s%s/factory/%s\n' "$main_root" "${relative:+/$relative}" "$name"
  else
    printf '%s/factory/%s\n' "$root_abs" "$name"
  fi
}

LEDGER="${FACTORY_LEDGER:-$(canonical_factory_file "$REPO_ROOT" runtime-ledger.csv)}"
DURABLE_LEDGER="${FACTORY_DURABLE_LEDGER:-$(canonical_factory_file "$REPO_ROOT" ledger.csv)}"
TICKETS_DIR="$CONTENT_ROOT/factory/tickets"

TICKET_FILE="$TICKETS_DIR/$TICKET.md"
[[ -f "$TICKET_FILE" ]] || { echo "REFUSE no ticket file at $TICKET_FILE"; exit 1; }
SOURCE_TICKET_FILE="$(cd "$(dirname "$TICKET_FILE")" && pwd -P)/$(basename "$TICKET_FILE")"
COMMITTED_TICKET_FILE="$(mktemp "${TMPDIR:-/tmp}/committed-ticket.XXXXXX")"
EFFECTIVE_TICKET="$(mktemp "${TMPDIR:-/tmp}/effective-ticket.XXXXXX")"
trap 'rm -f "$COMMITTED_TICKET_FILE" "$EFFECTIVE_TICKET"' EXIT
TICKET_WORKTREE_ROOT="" TICKET_RELATIVE="" COMMITTED_HEAD=""
if WORKTREE_ROOT="$(git -C "$CONTENT_ROOT" rev-parse --show-toplevel 2>/dev/null)"; then
  WORKTREE_ROOT="$(cd "$WORKTREE_ROOT" && pwd -P)"
  TICKET_WORKTREE_ROOT="$WORKTREE_ROOT"
  COMMITTED_HEAD="$(git -C "$WORKTREE_ROOT" rev-parse HEAD 2>/dev/null || true)"
  case "$SOURCE_TICKET_FILE" in
    "$WORKTREE_ROOT"/*) TICKET_RELATIVE="${SOURCE_TICKET_FILE#"$WORKTREE_ROOT/"}" ;;
    *) TICKET_RELATIVE="" ;;
  esac
  if [[ -z "$TICKET_RELATIVE" ]] ||
     ! git -C "$WORKTREE_ROOT" show "$COMMITTED_HEAD:$TICKET_RELATIVE" \
       > "$COMMITTED_TICKET_FILE" 2>/dev/null; then
    : > "$COMMITTED_TICKET_FILE"
  fi
else
  # Non-Git roots are retained for sealed conformance fixtures only; they have
  # no durable branch evidence.
  : > "$COMMITTED_TICKET_FILE"
fi
python3 "$KIT_DIR/scripts/lib/effective_ticket.py" \
  --ticket-file "$SOURCE_TICKET_FILE" --operator-map "$FACTORY_DIR/linear-map.json" \
  --ticket "$TICKET" > "$EFFECTIVE_TICKET" || {
    echo "REFUSE effective ticket state could not be resolved"
    exit 1
  }
TICKET_FILE="$EFFECTIVE_TICKET"
CONTRACT_VERSION="${FACTORY_RELEASE_CONTRACT_VERSION:-${FACTORY_HERMES_CONTRACT_VERSION:-1.2.0}}"
if [[ "$CONTRACT_VERSION" == "1.2.0" ]] &&
   { grep -qiE '^State:[[:space:]]*(Awaiting Approval|Approved)[[:space:]]*$' "$TICKET_FILE" ||
     grep -qiE '^Operator-Approval:' "$TICKET_FILE"; }; then
  echo "REFUSE contract 1.2 has no trusted bundle-attestation path for approval"
  exit 1
fi
if [[ -f "$FACTORY_DIR/MAINTENANCE" ]]; then
  echo "REFUSE MAINTENANCE file present — factory control plane is paused"
  exit 1
fi
if ! factory_validate_kit_pin "$KIT_DIR" "$REPO_ROOT"; then
  echo "REFUSE $FACTORY_KIT_PIN_ERROR"
  exit 1
fi
TERMINAL_BASIS=""
if [[ "$CONTRACT_VERSION" == "1.3.0" || "$CONTRACT_VERSION" == "1.4.0" ]]; then
  TERMINAL_BASIS="$(python3 "$KIT_DIR/scripts/lib/effective_ticket.py" \
    --factory-dir "$CONTENT_ROOT/factory" --ticket "$TICKET" \
    --terminal-basis 2>/dev/null || true)"
fi
if [[ -z "$TERMINAL_BASIS" ]] &&
   ! factory_validate_ticket_kit_sha "$TICKET_FILE" "$FACTORY_KIT_SHA"; then
  echo "REFUSE $FACTORY_TICKET_KIT_ERROR"
  exit 1
fi
if [[ "$TERMINAL_BASIS" == "validated-legacy-closeout" ]]; then
  echo "COMPLETE validated legacy closeout is on protected main; no historical lease is implied"
  exit 0
fi
if [[ "$TERMINAL_BASIS" == "validated-terminal-backfill" ]]; then
  echo "COMPLETE validated pre-contract terminal backfill is on protected main; no historical lease is implied"
  exit 0
fi
if ! factory_dispatch_require_lease "$REPO_ROOT" "$TICKET" "$LEASE_ID"; then
  echo "REFUSE $FACTORY_DISPATCH_LEASE_ERROR"
  exit 1
fi
if [[ -n "$TERMINAL_BASIS" ]]; then
  if [[ "$TERMINAL_BASIS" == "attested-done" ]]; then
    echo "COMPLETE attested Done is on protected main; release the matching lease"
  else
    echo "REFUSE protected main returned an unknown terminal basis"
    exit 1
  fi
  exit 0
fi
if [[ "$CONTRACT_VERSION" == "1.3.0" || "$CONTRACT_VERSION" == "1.4.0" ]]; then
  EFFECTIVE_STATE="$(awk -F: 'tolower($1)=="state" {sub(/^[^:]*:[[:space:]]*/, ""); print tolower($0); exit}' "$TICKET_FILE")"
  COMMITTED_STATE="$(awk -F: 'tolower($1)=="state" {sub(/^[^:]*:[[:space:]]*/, ""); print tolower($0); exit}' "$COMMITTED_TICKET_FILE")"
  if [[ "$COMMITTED_STATE" == "done" ]]; then
    echo "AWAIT-MERGE closeout auto-merge pending; Done is not yet on protected main"
    exit 0
  fi
  if [[ "$EFFECTIVE_STATE" == "approved" && "$COMMITTED_STATE" == "awaiting approval" ]]; then
    echo "AWAIT-OPERATOR Linear approval observed; trusted approval attestation is required"
    exit 0
  fi
  if [[ "$COMMITTED_STATE" == "approved" ]]; then
    if python3 - "$FACTORY_DIR/linear-map.json" "$TICKET" <<'PY'
import json
import sys
try:
    value = json.load(open(sys.argv[1], encoding="utf-8"))
except FileNotFoundError:
    raise SystemExit(1)
operator = value.get("tickets", {}).get(sys.argv[2], {}).get("operator") or {}
if not (
    operator.get("state") == "Approved"
    and operator.get("approval") == "Linear"
    and operator.get("state_base") == "awaiting approval"
):
    raise SystemExit(1)
PY
    then
      echo "AWAIT-OPERATOR approval attested; protected auto-merge request must be confirmed"
      exit 0
    fi
    APPROVAL_ATTESTATION="$CONTENT_ROOT/factory/attestations/$TICKET/approval.json"
    python3 - "$APPROVAL_ATTESTATION" "$TICKET" <<'PY' || {
import json
import sys
value = json.load(open(sys.argv[1], encoding="utf-8"))
if value.get("schema") != "nysa.software-factory.ticket-approval/v1" or value.get("ticket") != sys.argv[2]:
    raise SystemExit(1)
PY
      echo "REFUSE Approved ticket lacks a valid approval attestation"
      exit 1
    }
    echo "AWAIT-MERGE protected auto-merge requested; await merge and closeout"
    exit 0
  fi
  if [[ "$COMMITTED_STATE" == "awaiting approval" ]]; then
    echo "AWAIT-OPERATOR bundle attested; await Linear approval"
    exit 0
  fi
fi
if [[ -z "${FACTORY_LEDGER:-}" ]] &&
   ! python3 "$KIT_DIR/scripts/ledger-view.py" refresh \
     --factory-root "$REPO_ROOT" \
     --durable-ledger "$DURABLE_LEDGER" \
     --runtime-ledger "$LEDGER" >/dev/null; then
  echo "REFUSE effective ledger could not be reduced"
  exit 1
fi

# Successful (exit_status 0) runs per role, in ledger (completion) order.
# (cat the ledger defensively: a missing ledger means zero runs, not an error.)
count_ok() { { cat "$LEDGER" 2>/dev/null || true; } | awk -F, -v t="$TICKET" -v r="$1" 'NR>1 && $3==t && $4==r && $9=="0"' | wc -l | tr -d ' '; }
count_authorization() { # role semantic-round
  grep -ciE "^[[:space:]]*OPERATOR AUTHORIZATION:[[:space:]]*$1 round[[:space:]]*$2[[:space:]]*$" "$TICKET_FILE" || true
}
P="$(count_ok planner)"; SL="$(count_ok spec-linter)"; TA="$(count_ok test-author)"
B="$(count_ok builder)"; R="$(count_ok reviewer)"; N="$(count_ok narrator)"

# Reviewer verdicts must be recorded on the ticket file by the dispatcher.
# Count them; they are the only stage input outside the ledger.
A="$(grep -ciE '^[[:space:]]*reviewer round[[:space:]]+[0-9]+:[[:space:]]*APPROVE[[:space:]]*$' "$TICKET_FILE" || true)"; A="${A:-0}"
RC="$(grep -ciE '^[[:space:]]*reviewer round[[:space:]]+[0-9]+:[[:space:]]*REQUEST CHANGES([[:space:]]+—[[:space:]]+.*)?[[:space:]]*$' "$TICKET_FILE" || true)"; RC="${RC:-0}"
VERDICTS=$((A + RC))

# A void note names the one-based ordinal among successful reviewer rows.
# Ignore duplicate notes, out-of-range ordinals, and prose that does not match
# the exact auditable form documented above.
VOID_DATA="$(awk -v max="$R" '
  {
    line=tolower($0)
    # Separator between "void" and "duplicate" is matched loosely: an em dash
    # inside a bracket expression is locale-dependent in awk (worked in a UTF-8
    # login shell, failed over ssh with C.UTF-8), so accept any non-alnum run.
    if (line ~ /^[[:space:]]*operator note:[[:space:]]*reviewer run[[:space:]]*[0-9]+[[:space:]]+void[^a-z0-9]*duplicate[[:space:]]*$/) {
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

# --- spec-lint gate: plan → lint → (replan on FAIL) → tests ---
# The spec-linter appends its own verdict line (SPEC-LINT: PASS/FAIL) to the
# ticket; each planner run must be followed by one lint run, each FAIL by one
# replan. The gate applies only before the test-author has run, so tickets
# already past planning (including all pre-gate tickets) are unaffected.
if [[ "$TA" -eq 0 ]]; then
  SLP="$(grep -ciE '^[[:space:]]*SPEC-LINT:[[:space:]]*PASS[[:space:]]*$' "$TICKET_FILE" || true)"; SLP="${SLP:-0}"
  SLF="$(grep -ciE '^[[:space:]]*SPEC-LINT:[[:space:]]*FAIL([[:space:]]+—[[:space:]]+.*)?[[:space:]]*$' "$TICKET_FILE" || true)"; SLF="${SLF:-0}"
  if [[ "$SL" -gt $((SLP + SLF)) ]]; then
    echo "REFUSE spec-linter has $SL successful run(s) but only $((SLP + SLF)) SPEC-LINT verdict(s) on $TICKET_FILE — the lint run must end with a 'SPEC-LINT: PASS' or 'SPEC-LINT: FAIL' line"
    exit 1
  fi
  if [[ "$SL" -lt $((SLP + SLF)) ]]; then
    echo "REFUSE ticket logs $((SLP + SLF)) SPEC-LINT verdict(s) but the ledger has only $SL successful spec-linter run(s) — correct the ticket bookkeeping"
    exit 1
  fi
  SPEC_VERDICTS=$((SLP + SLF))
  if [[ "$SLF" -ge 2 && "$SLF" -eq "$SPEC_VERDICTS" ]]; then
    NEXT_SPEC_ROUND=$((SPEC_VERDICTS + 1))
    SPEC_AUTH="$(count_authorization spec-linter "$NEXT_SPEC_ROUND")"; SPEC_AUTH="${SPEC_AUTH:-0}"
    if [[ "$SPEC_AUTH" -eq 0 ]]; then
      echo "ESCALATE spec-lint failed twice — the spec keeps failing its own checklist; operator decides (an extra round needs an 'OPERATOR AUTHORIZATION: spec-linter round $NEXT_SPEC_ROUND' line on the ticket, written on explicit operator instruction)"
      exit 0
    fi
  fi
  if [[ "$P" -lt $((SLF + 1)) ]]; then echo "RUN planner"; exit 0; fi
  if [[ "$SL" -lt "$P" ]]; then echo "RUN spec-linter"; exit 0; fi
fi

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
  # Approval is evidence-sensitive: an ignored Linear overlay may inform the
  # future bundle-attestation path. Contract 1.2 stops before that boundary.
  if [[ "$CONTRACT_VERSION" == "1.2.0" ]] &&
     grep -qiE '^Operator-Approval:[[:space:]]*Linear[[:space:]]*$' "$TICKET_FILE"; then
    echo "REFUSE contract 1.2 has no trusted bundle-attestation path for approval"
    exit 1
  fi
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
  AUTH="$(count_authorization reviewer "$NEXT_ROUND")"; AUTH="${AUTH:-0}"
  if [[ "$AUTH" -lt 1 ]]; then
    echo "ESCALATE reviewer requested changes twice — two-round limit reached, operator decides (an extra round needs an 'OPERATOR AUTHORIZATION: reviewer round $NEXT_ROUND' line on the ticket, written on explicit operator instruction)"
    exit 0
  fi
fi

# After the latest rejection, was a fix (test-author or builder success) completed
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
  echo "FIX builder-or-test-author"
fi
