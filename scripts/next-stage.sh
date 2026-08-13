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
OPERATOR_MAP="${FACTORY_OPERATOR_MAP:-$FACTORY_DIR/operator-map.json}"
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
REFRESH_RUNTIME_LEDGER="${FACTORY_REFRESH_RUNTIME_LEDGER:-0}"
[[ "$REFRESH_RUNTIME_LEDGER" == "0" || "$REFRESH_RUNTIME_LEDGER" == "1" ]] || {
  echo "REFUSE runtime ledger refresh policy is invalid"
  exit 1
}
ROLE_EVIDENCE="${FACTORY_AUTHENTICATED_ROLE_EVIDENCE:-}"
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
  --ticket-file "$SOURCE_TICKET_FILE" --operator-map "$OPERATOR_MAP" \
  --ticket "$TICKET" > "$EFFECTIVE_TICKET" || {
    echo "REFUSE effective ticket state could not be resolved"
    exit 1
  }
TICKET_FILE="$EFFECTIVE_TICKET"
CONTRACT_VERSION="${FACTORY_RELEASE_CONTRACT_VERSION:-${FACTORY_CONTRACT_VERSION:-1.2.0}}"
if [[ -n "$ROLE_EVIDENCE" ]]; then
  [[ "$CONTRACT_VERSION" == "1.8.0" || "$CONTRACT_VERSION" == "2.0.0" ]] || {
    echo "REFUSE authenticated role evidence requires contract 1.8"
    exit 1
  }
  python3 - "$ROLE_EVIDENCE" "$TICKET" "$CONTRACT_VERSION" <<'PY' || {
import json
import os
import pathlib
import re
import stat
import sys

path = pathlib.Path(sys.argv[1])
info = path.lstat()
if (
    not path.is_absolute()
    or path.is_symlink()
    or not stat.S_ISREG(info.st_mode)
    or info.st_uid != os.geteuid()
    or info.st_nlink != 1
    or stat.S_IMODE(info.st_mode) != 0o600
    or info.st_size > 5_000_000
):
    raise SystemExit(1)
value = json.loads(path.read_text(encoding="utf-8"))
sha = re.compile(r"[0-9a-f]{40}")
digest = re.compile(r"[0-9a-f]{64}")
run_id = re.compile(r"[A-Za-z0-9._-]{1,200}")
roles = {"planner", "spec-linter", "test-author", "builder", "reviewer", "narrator"}
expected = {
    "contract_version", "factory_sha", "head_before", "manifest_sha256",
    "output_sha256", "role", "run_id", "transition_receipt_sha256",
}
records = value.get("records")
if (
    set(value) != {"passport_sha256", "records", "schema", "ticket"}
    or value.get("schema") != "nysa.software-factory.completed-role-sequence/v1"
    or value.get("ticket") != sys.argv[2]
    or not digest.fullmatch(value.get("passport_sha256", ""))
    or not isinstance(records, list)
):
    raise SystemExit(1)
seen_runs = set()
seen_receipts = set()
for item in records:
    if (
        not isinstance(item, dict)
        or set(item) != expected
        or item.get("contract_version") != sys.argv[3]
        or not sha.fullmatch(item.get("factory_sha", ""))
        or not sha.fullmatch(item.get("head_before", ""))
        or not digest.fullmatch(item.get("manifest_sha256", ""))
        or not digest.fullmatch(item.get("output_sha256", ""))
        or item.get("role") not in roles
        or not run_id.fullmatch(item.get("run_id", ""))
        or not digest.fullmatch(item.get("transition_receipt_sha256", ""))
        or item["run_id"] in seen_runs
        or item["transition_receipt_sha256"] in seen_receipts
    ):
        raise SystemExit(1)
    seen_runs.add(item["run_id"])
    seen_receipts.add(item["transition_receipt_sha256"])
PY
    echo "REFUSE authenticated passport role evidence is invalid"
    exit 1
  }
fi
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
if [[ "$CONTRACT_VERSION" == "1.3.0" || "$CONTRACT_VERSION" == "1.4.0" ||
      "$CONTRACT_VERSION" == "1.5.0" || "$CONTRACT_VERSION" == "1.6.0" ||
      "$CONTRACT_VERSION" == "1.7.0" || "$CONTRACT_VERSION" == "1.8.0" ||
      "$CONTRACT_VERSION" == "2.0.0" ]]; then
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
if [[ "$TERMINAL_BASIS" == "validated-protected-merge-reconciliation" ]]; then
  echo "COMPLETE validated protected-merge reconciliation is on protected main; no historical lease is implied"
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

# A sealed base refresh invalidates review/narration evidence unless the exact
# protected-base delta contains only authenticated, non-semantic control files.
# The receipt is committed by ticket-attest; fail closed if it is edited,
# malformed, or no longer belongs to this history.
REFRESH_RECEIPT="$CONTENT_ROOT/factory/attestations/$TICKET/refresh.json"
REFRESH_ACTIVE=0
REFRESH_PRESERVE_REVIEW=0
REFRESH_PRESERVE_NARRATOR=0
REFRESH_REVALIDATION_BUDGET=0
REFRESH_REVALIDATION_FACTORY=""
REFRESH_REVALIDATION_GENERATION=0
if [[ -e "$REFRESH_RECEIPT" ]]; then
  [[ -f "$REFRESH_RECEIPT" && ! -L "$REFRESH_RECEIPT" ]] || {
    echo "REFUSE refresh receipt is not a regular file"
    exit 1
  }
  REFRESH_RELATIVE="factory/attestations/$TICKET/refresh.json"
  COMMITTED_REFRESH="$(mktemp "${TMPDIR:-/tmp}/committed-refresh.XXXXXX")"
  trap 'rm -f "$COMMITTED_TICKET_FILE" "$EFFECTIVE_TICKET" "$COMMITTED_REFRESH"' EXIT
  if [[ -z "$TICKET_WORKTREE_ROOT" ]] ||
     ! git -C "$TICKET_WORKTREE_ROOT" show "$COMMITTED_HEAD:$REFRESH_RELATIVE" \
       > "$COMMITTED_REFRESH" 2>/dev/null ||
     ! cmp -s "$REFRESH_RECEIPT" "$COMMITTED_REFRESH"; then
    echo "REFUSE refresh receipt is not committed unchanged at HEAD"
    exit 1
  fi
  REFRESH_STATUS=0
  REFRESH_VALUES="$(python3 - "$KIT_DIR/scripts/ticket-attest.py" \
    "$TICKET_WORKTREE_ROOT" "$TICKET" <<'PY'
import importlib.util
from pathlib import Path
import sys

script = Path(sys.argv[1]).resolve(strict=True)
sys.path.insert(0, str(script.parent))
spec = importlib.util.spec_from_file_location("factory_ticket_attest", script)
if spec is None or spec.loader is None:
    raise SystemExit(1)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
try:
    value, _ = module.load_refresh_receipt(Path(sys.argv[2]), sys.argv[3])
except module.Refusal as error:
    print(
        "stale"
        if str(error) in {
            "refresh receipt commit topology is invalid",
            "refresh merge topology is invalid",
        }
        else "malformed"
    )
    raise SystemExit(1)
except (OSError, TypeError, ValueError):
    print("malformed")
    raise SystemExit(1)
heads = [value[name] for name in ("old_head", "base_head", "merge_head")]
counts = [value[name] for name in (
    "prior_reviewer_runs", "prior_approve_verdicts",
    "prior_request_changes_verdicts", "prior_narrator_runs",
)]
if module.refresh_receipt_version(value, sys.argv[3]) == 2:
    factory = value["revalidation_factory_sha"]
    reservation_generation = value["revalidation_generation"]
    budget = value["revalidation_budget_micro_usd"]
else:
    factory, reservation_generation, budget = "", 0, 0
print("|".join(map(str, heads + counts + [factory, reservation_generation, budget])))
PY
)" || REFRESH_STATUS=$?
  if [[ "$REFRESH_STATUS" -ne 0 ]]; then
    if [[ "$REFRESH_VALUES" == "stale" ]]; then
      echo "REFUSE stale refresh receipt does not bind this branch history"
    else
      echo "REFUSE malformed refresh receipt"
    fi
    exit 1
  fi
  IFS='|' read -r REFRESH_OLD_HEAD REFRESH_BASE_HEAD REFRESH_MERGE_HEAD \
    REFRESH_REVIEWERS REFRESH_APPROVES REFRESH_REQUESTS REFRESH_NARRATORS \
    REFRESH_REVALIDATION_FACTORY REFRESH_REVALIDATION_GENERATION \
    REFRESH_REVALIDATION_BUDGET \
    <<< "$REFRESH_VALUES"
  read -r -a REFRESH_PARENTS <<< "$(git -C "$TICKET_WORKTREE_ROOT" \
    rev-list --parents -n 1 "$REFRESH_MERGE_HEAD" 2>/dev/null || true)"
  if [[ "${#REFRESH_PARENTS[@]}" -ne 3 ||
        "${REFRESH_PARENTS[1]}" != "$REFRESH_OLD_HEAD" ||
        "${REFRESH_PARENTS[2]}" != "$REFRESH_BASE_HEAD" ]] ||
     ! git -C "$TICKET_WORKTREE_ROOT" merge-base --is-ancestor \
       "$REFRESH_MERGE_HEAD" "$COMMITTED_HEAD" 2>/dev/null; then
    echo "REFUSE stale refresh receipt does not bind this branch history"
    exit 1
  fi
  REFRESH_COMMIT="$(git -C "$TICKET_WORKTREE_ROOT" log -1 --format=%H \
    "$COMMITTED_HEAD" -- "$REFRESH_RELATIVE" 2>/dev/null || true)"
  read -r -a REFRESH_COMMIT_PARENTS <<< "$(git -C "$TICKET_WORKTREE_ROOT" \
    rev-list --parents -n 1 "$REFRESH_COMMIT" 2>/dev/null || true)"
  if [[ "${#REFRESH_COMMIT_PARENTS[@]}" -ne 2 ||
        "${REFRESH_COMMIT_PARENTS[1]}" != "$REFRESH_MERGE_HEAD" ]]; then
    echo "REFUSE refresh receipt was not committed directly after its merge"
    exit 1
  fi
  if ! REFRESH_CLASSIFICATION="$(python3 \
    "$KIT_DIR/scripts/lib/refresh_semantics.py" \
    --repo "$TICKET_WORKTREE_ROOT" \
    --old-head "$REFRESH_OLD_HEAD" \
    --base-head "$REFRESH_BASE_HEAD")"; then
    echo "REFUSE protected-base semantic classification failed"
    exit 1
  fi
  case "$REFRESH_CLASSIFICATION" in
    PRESERVE)
      REFRESH_PRESERVE_REVIEW=1
      REFRESH_REVALIDATION_FACTORY=""
      REFRESH_REVALIDATION_BUDGET=0
      ;;
    INVALIDATE) ;;
    *)
      echo "REFUSE protected-base semantic classification was invalid"
      exit 1
      ;;
  esac
  REFRESH_PATHS="$(git -C "$TICKET_WORKTREE_ROOT" diff-tree --no-commit-id \
    --name-status -r "$REFRESH_COMMIT" 2>/dev/null || true)"
  REFRESH_TICKET_CHANGED="$(REFRESH_PATHS_INPUT="$REFRESH_PATHS" python3 - "$TICKET" <<'PY'
import os
import sys

ticket = sys.argv[1]
required = {
    f"factory/attestations/{ticket}/refresh.json": {"A", "M"},
}
optional = {
    f"factory/tickets/{ticket}.md": {"A", "M"},
    f"factory/attestations/{ticket}/bundle.json": {"D"},
    f"factory/attestations/{ticket}/approval.json": {"D"},
}
seen = set()
for line in os.environ["REFRESH_PATHS_INPUT"].splitlines():
    parts = line.split("\t")
    if len(parts) != 2:
        raise SystemExit(1)
    status, path = parts
    allowed = required.get(path, optional.get(path))
    if allowed is None or status not in allowed or path in seen:
        raise SystemExit(1)
    seen.add(path)
if not set(required).issubset(seen):
    raise SystemExit(1)
print(int(f"factory/tickets/{ticket}.md" in seen))
PY
)" || {
    echo "REFUSE refresh commit changed paths outside the sealed reset"
    exit 1
  }
  OLD_TICKET="$(mktemp "${TMPDIR:-/tmp}/old-ticket.XXXXXX")"
  REFRESH_COMMIT_TICKET="$(mktemp "${TMPDIR:-/tmp}/refresh-ticket.XXXXXX")"
  trap 'rm -f "$COMMITTED_TICKET_FILE" "$EFFECTIVE_TICKET" "$COMMITTED_REFRESH" "$OLD_TICKET" "$REFRESH_COMMIT_TICKET"' EXIT
  if ! git -C "$TICKET_WORKTREE_ROOT" show \
    "$REFRESH_OLD_HEAD:factory/tickets/$TICKET.md" > "$OLD_TICKET" 2>/dev/null; then
    echo "REFUSE refresh old head lacks the ticket baseline"
    exit 1
  fi
  if [[ "$REFRESH_TICKET_CHANGED" -eq 0 ]]; then
    if ! git -C "$TICKET_WORKTREE_ROOT" show \
         "$REFRESH_COMMIT:factory/tickets/$TICKET.md" > "$REFRESH_COMMIT_TICKET" 2>/dev/null ||
       ! python3 - "$REFRESH_COMMIT_TICKET" <<'PY'
import re
import sys

text = open(sys.argv[1], encoding="utf-8").read()
states = re.findall(r"^State:\s*(.*?)\s*$", text, re.I | re.M)
if len(states) != 1 or states[0].lower() != "review":
    raise SystemExit(1)
if re.search(r"^Operator-Approval:", text, re.I | re.M):
    raise SystemExit(1)
for label in ("Evidence bundle posted", "Operator approved"):
    if len(re.findall(rf"^- \[ \] {re.escape(label)}\s*$", text, re.M)) != 1:
        raise SystemExit(1)
PY
    then
      echo "REFUSE omitted refresh ticket change was not an exact no-op reset"
      exit 1
    fi
  fi
  OLD_BASELINES="$(python3 - "$OLD_TICKET" <<'PY'
import re
import sys

lines = open(sys.argv[1], encoding="utf-8").read().splitlines()
approve = sum(bool(re.fullmatch(r"\s*reviewer round\s+\d+:\s*APPROVE\s*", line, re.I)) for line in lines)
request = sum(bool(re.fullmatch(r"\s*reviewer round\s+\d+:\s*REQUEST CHANGES(?:\s+—\s+.*)?\s*", line, re.I)) for line in lines)
voids = set()
for line in lines:
    match = re.fullmatch(
        r"\s*OPERATOR NOTE:\s*reviewer run\s*(\d+)\s+void[^A-Za-z0-9]*duplicate\s*",
        line, re.I,
    )
    if match:
        voids.add(int(match.group(1)))
raw_reviewers = approve + request + len(voids)
if any(number < 1 or number > raw_reviewers for number in voids):
    raise SystemExit(1)
print(f"{approve}|{request}|{len(voids)}|{','.join(map(str, sorted(voids)))}")
PY
)" || {
    echo "REFUSE old ticket has malformed reviewer void evidence"
    exit 1
  }
  IFS='|' read -r OLD_APPROVES OLD_REQUESTS OLD_VOID_COUNT OLD_VOID_RUNS \
    <<< "$OLD_BASELINES"
  if [[ "$REFRESH_APPROVES" -ne "$OLD_APPROVES" ||
        "$REFRESH_REQUESTS" -ne "$OLD_REQUESTS" ||
        "$REFRESH_REVIEWERS" -ne $((OLD_APPROVES + OLD_REQUESTS)) ]]; then
    echo "REFUSE refresh receipt baselines do not match the old ticket"
    exit 1
  fi
  if ! python3 - "$OLD_TICKET" "$COMMITTED_TICKET_FILE" <<'PY'
import re
import sys

verdict_pattern = re.compile(
    r"^\s*reviewer round\s+(\d+):\s*(APPROVE|REQUEST CHANGES(?:\s+—\s+.*)?)\s*$",
    re.I | re.M,
)
void_pattern = re.compile(
    r"^\s*OPERATOR NOTE:\s*reviewer run\s*(\d+)\s+void[^A-Za-z0-9]*duplicate\s*$",
    re.I | re.M,
)
def sequences(path):
    text = open(path, encoding="utf-8").read()
    verdicts = [f"{int(round_number)}:{' '.join(verdict.split()).upper()}"
                for round_number, verdict in verdict_pattern.findall(text)]
    voids = [int(ordinal) for ordinal in void_pattern.findall(text)]
    return verdicts, voids

old_verdicts, old_voids = sequences(sys.argv[1])
current_verdicts, current_voids = sequences(sys.argv[2])
if (current_verdicts[:len(old_verdicts)] != old_verdicts
        or current_voids[:len(old_voids)] != old_voids):
    raise SystemExit(1)
PY
  then
    echo "REFUSE old reviewer verdict or void-note sequence is not an unchanged prefix"
    exit 1
  fi
  REFRESH_RAW_REVIEWERS=$((REFRESH_REVIEWERS + OLD_VOID_COUNT))
  REFRESH_ACTIVE=1
fi
if [[ -n "$TERMINAL_BASIS" ]]; then
  if [[ "$TERMINAL_BASIS" == "attested-done" ||
        "$TERMINAL_BASIS" == "attested-emergency-closeout" ]]; then
    echo "COMPLETE attested Done is on protected main; release the matching lease"
  else
    echo "REFUSE protected main returned an unknown terminal basis"
    exit 1
  fi
  exit 0
fi
if [[ "$CONTRACT_VERSION" == "1.3.0" || "$CONTRACT_VERSION" == "1.4.0" ||
      "$CONTRACT_VERSION" == "1.5.0" || "$CONTRACT_VERSION" == "1.6.0" ||
      "$CONTRACT_VERSION" == "1.7.0" || "$CONTRACT_VERSION" == "1.8.0" ||
      "$CONTRACT_VERSION" == "2.0.0" ]]; then
  EFFECTIVE_STATE="$(awk -F: 'tolower($1)=="state" {sub(/^[^:]*:[[:space:]]*/, ""); print tolower($0); exit}' "$TICKET_FILE")"
  COMMITTED_STATE="$(awk -F: 'tolower($1)=="state" {sub(/^[^:]*:[[:space:]]*/, ""); print tolower($0); exit}' "$COMMITTED_TICKET_FILE")"
  if [[ "$COMMITTED_STATE" == "done" ]]; then
    echo "AWAIT-MERGE closeout auto-merge pending; Done is not yet on protected main"
    exit 0
  fi
  if [[ "$EFFECTIVE_STATE" == "approved" && "$COMMITTED_STATE" == "awaiting approval" ]]; then
    echo "AWAIT-OPERATOR operator approval observed; trusted approval attestation is required"
    exit 0
  fi
  if [[ "$COMMITTED_STATE" == "approved" ]]; then
    if python3 - "$OPERATOR_MAP" "$TICKET" <<'PY'
import json
import sys
try:
    value = json.load(open(sys.argv[1], encoding="utf-8"))
except FileNotFoundError:
    raise SystemExit(1)
operator = value.get("tickets", {}).get(sys.argv[2], {}).get("operator") or {}
if not (
    operator.get("state") == "Approved"
    and operator.get("approval") == "Receipt"
    and operator.get("state_base") == "awaiting approval"
):
    raise SystemExit(1)
PY
    then
      echo "AWAIT-MERGE approval attested; protected auto-merge request pending"
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
    echo "AWAIT-OPERATOR bundle attested; await operator approval"
    exit 0
  fi
fi
if [[ -z "${FACTORY_LEDGER:-}" || "$REFRESH_RUNTIME_LEDGER" == "1" ]] &&
   ! python3 "$KIT_DIR/scripts/ledger-view.py" refresh \
     --factory-root "$REPO_ROOT" \
     --durable-ledger "$DURABLE_LEDGER" \
     --runtime-ledger "$LEDGER" >/dev/null; then
  echo "REFUSE effective ledger could not be reduced"
  exit 1
fi
SEMANTIC_SPEC_FAILURES="$(grep -ciE '^[[:space:]]*SPEC-LINT:[[:space:]]*FAIL([[:space:]]+—[[:space:]]+.*)?[[:space:]]*$' "$TICKET_FILE" || true)"
SEMANTIC_SPEC_FAILURES="${SEMANTIC_SPEC_FAILURES:-0}"
SEMANTIC_AUTHORIZATIONS="$(grep -cFx 'OPERATOR AUTHORIZATION: spec-linter round 3' "$TICKET_FILE" || true)"
SEMANTIC_AUTHORIZATIONS="${SEMANTIC_AUTHORIZATIONS:-0}"
emit_stage() {
  local stage="$1"
  local budget_stage="AVAILABLE"
  if [[ "$stage" == "RUN planner" && "$SEMANTIC_SPEC_FAILURES" -ge 3 ]]; then
    printf 'ESCALATE planner-spec-linter loop cap reached; attempts=%s; limit=3\n' \
      "$SEMANTIC_SPEC_FAILURES"
    exit 0
  elif [[ ( "$stage" == "RUN planner" || "$stage" == "RUN spec-linter" ) &&
        "$SEMANTIC_SPEC_FAILURES" -eq 2 ]]; then
    if [[ "$SEMANTIC_AUTHORIZATIONS" -eq 0 ]]; then
      printf '%s\n' "AWAIT-OPERATOR semantic-round authorization required; add exact line: OPERATOR AUTHORIZATION: spec-linter round 3"
      exit 0
    elif [[ "$SEMANTIC_AUTHORIZATIONS" -ne 1 ]]; then
      printf '%s\n' "AWAIT-OPERATOR semantic-round authorization invalid; keep exactly one line: OPERATOR AUTHORIZATION: spec-linter round 3"
      exit 0
    fi
  fi
  if [[ ( "$CONTRACT_VERSION" == "1.8.0" || "$CONTRACT_VERSION" == "2.0.0" ) &&
        ( "$stage" == RUN\ * || "$stage" == FIX\ * ) ]]; then
    budget_stage="$(python3 -B "$KIT_DIR/scripts/budget-stage.py" \
      "$REPO_ROOT" "$TICKET" "$FACTORY_RELEASE_SHA" "$stage" \
      "$REFRESH_REVALIDATION_FACTORY" \
      "$REFRESH_REVALIDATION_BUDGET")" || {
        echo "REFUSE ticket budget could not be reduced"
        exit 1
      }
    [[ "$budget_stage" == "AVAILABLE" ||
       "$budget_stage" == AWAIT_BUDGET* ]] || {
      echo "REFUSE ticket budget reducer returned an invalid stage"
      exit 1
    }
  fi
  if [[ "$budget_stage" == AWAIT_BUDGET* ]]; then
    printf '%s\n' "$budget_stage"
    exit 0
  fi
  printf '%s\n' "$stage"
  exit 0
}

if [[ "$CONTRACT_VERSION" == "1.8.0" || "$CONTRACT_VERSION" == "2.0.0" ]]; then
  if [[ -n "${FACTORY_TRANSITION_STATE_DIR:-}" ]]; then
    REPAIR_STAGE="$(python3 -B "$KIT_DIR/scripts/publication-repair.py" stage \
      --factory-root "$REPO_ROOT" --workdir "$CONTENT_ROOT" \
      --state-dir "$FACTORY_TRANSITION_STATE_DIR" --kit-dir "$KIT_DIR" \
      --ticket "$TICKET" --factory-sha "$FACTORY_RELEASE_SHA" \
      --ledger "$LEDGER")" || {
        echo "REFUSE publication repair stage could not be reduced"
        exit 1
    }
    if [[ "$REPAIR_STAGE" != "INACTIVE" ]]; then
      emit_stage "$REPAIR_STAGE"
    fi
    if grep -q '^FACTORY PUBLICATION REPAIR:' "$TICKET_FILE"; then
      echo "REFUSE publication repair directive lacks authenticated controller state"
      exit 1
    fi
  fi
fi

# Successful (exit_status 0) runs per role, in ledger (completion) order.
# (cat the ledger defensively: a missing ledger means zero runs, not an error.)
count_ok() {
  if [[ -n "$ROLE_EVIDENCE" ]]; then
    python3 - "$ROLE_EVIDENCE" "$1" <<'PY'
import json, sys
value = json.load(open(sys.argv[1], encoding="utf-8"))
print(sum(item["role"] == sys.argv[2] for item in value["records"]))
PY
  else
    { cat "$LEDGER" 2>/dev/null || true; } |
      awk -F, -v t="$TICKET" -v r="$1" \
        'NR>1 && $3==t && $4==r && $9=="0"' |
      wc -l | tr -d ' '
  fi
}
P="$(count_ok planner)"; SL="$(count_ok spec-linter)"; TA="$(count_ok test-author)"
B="$(count_ok builder)"; R="$(count_ok reviewer)"; N="$(count_ok narrator)"
LOCAL_P="$P"; LOCAL_SL="$SL"; LOCAL_TA="$TA"
LOCAL_B="$B"; LOCAL_R="$R"; LOCAL_N="$N"
CHECKPOINT_P=0; CHECKPOINT_SL=0; CHECKPOINT_TA=0
CHECKPOINT_B=0; CHECKPOINT_R=0; CHECKPOINT_N=0
CHECKPOINT_N_AFTER_LATEST_R=0
CHECKPOINT_NEXT_STAGE=""
CHECKPOINT_AWAIT_REOPENED=0
if [[ -n "${FACTORY_DEV_PRODUCT_CHECKPOINT:-}" ]]; then
  CHECKPOINT_COUNTS="$(python3 - "$FACTORY_DEV_PRODUCT_CHECKPOINT" \
    "${FACTORY_CLI_LANE_ROOT:-}" "$CONTENT_ROOT" "$TICKET" "$COMMITTED_HEAD" \
    "$TICKET_FILE" <<'PY'
import json, os, pathlib, re, stat, subprocess, sys
checkpoint, lane, work, ticket, head, ticket_file=sys.argv[1:]
path=pathlib.Path(checkpoint); lane_path=pathlib.Path(lane)
if (not lane_path.is_absolute() or not lane_path.name.startswith("nysa-sf-dev.") or
    path.resolve() != (lane_path/"runtime/product-checkpoint-import.json").resolve()):
    raise SystemExit(1)
for item, mode in ((lane_path/"marker.json",0o600),(path,0o600)):
    info=item.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
        info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != mode):
        raise SystemExit(1)
marker=json.load(open(lane_path/"marker.json",encoding="utf-8"))
value=json.load(open(path,encoding="utf-8"))
if (marker.get("mode") != "product" or
    set(value) != {"schema","checkpoint_sha256","tickets"} or
    value.get("schema") not in {
        "factory-dev-product-checkpoint-import/v1",
        "factory-dev-product-checkpoint-import/v2",
    }):
    raise SystemExit(1)
records=[item for item in value["tickets"] if item.get("ticket") == ticket]
if not records:
    print("0\t0\t0\t0\t0\t0\t")
    raise SystemExit(0)
if len(records) != 1: raise SystemExit(1)
record=records[0]
if set(record) != {"ticket","import_head","import_tree","roles",
                   "spec_verdicts","expected_next_stage"}:
    raise SystemExit(1)
sha=lambda item: isinstance(item,str) and re.fullmatch(r"[0-9a-f]{40}",item)
if (not sha(record["import_head"]) or not sha(record["import_tree"]) or
    subprocess.run(["git","-C",work,"merge-base","--is-ancestor",
                    record["import_head"],head]).returncode != 0):
    raise SystemExit(1)
actual=subprocess.check_output(
    ["git","-C",work,"rev-parse",record["import_head"]+"^{tree}"],text=True).strip()
if actual != record["import_tree"]: raise SystemExit(1)
roles=record["roles"]
if (not isinstance(roles,list) or not roles or
    any(role not in {
        "planner","spec-linter","test-author","builder","reviewer","narrator"
    }
        for role in roles)):
    raise SystemExit(1)
text=pathlib.Path(ticket_file).read_text(encoding="utf-8")
specs=[
    line.strip() for line in re.findall(
        r"^\s*SPEC-LINT: (?:PASS|FAIL(?: — .+)?)$", text, re.M
    )
]
checkpoint_specs=record["spec_verdicts"]
if (not isinstance(checkpoint_specs,list) or
    specs[:len(checkpoint_specs)] != checkpoint_specs):
    raise SystemExit(1)
print("\t".join([
    *(str(roles.count(role)) for role in
      ("planner","spec-linter","test-author","builder","reviewer","narrator")),
    record["expected_next_stage"],
]))
PY
  )" || { echo "REFUSE development checkpoint binding is invalid"; exit 1; }
  IFS=$'\t' read -r CHECKPOINT_P CHECKPOINT_SL CHECKPOINT_TA CHECKPOINT_B \
    CHECKPOINT_R CHECKPOINT_N CHECKPOINT_NEXT_STAGE <<<"$CHECKPOINT_COUNTS"
  CHECKPOINT_N_AFTER_LATEST_R="$CHECKPOINT_N"
  [[ "$CHECKPOINT_NEXT_STAGE" != "RUN narrator" ]] || \
    CHECKPOINT_N_AFTER_LATEST_R=0
  P=$((P + CHECKPOINT_P)); SL=$((SL + CHECKPOINT_SL))
  TA=$((TA + CHECKPOINT_TA)); B=$((B + CHECKPOINT_B))
  R=$((R + CHECKPOINT_R)); N=$((N + CHECKPOINT_N))
  if [[ "$CHECKPOINT_NEXT_STAGE" == "FIX test-author" && "$LOCAL_TA" -eq 0 ]] ||
     [[ "$CHECKPOINT_NEXT_STAGE" == "RUN test-author" && "$LOCAL_TA" -eq 0 ]] ||
     [[ "$CHECKPOINT_NEXT_STAGE" == "FIX builder" && "$LOCAL_B" -eq 0 ]] ||
     [[ "$CHECKPOINT_NEXT_STAGE" == "RUN builder" && "$LOCAL_B" -eq 0 ]] ||
     [[ "$CHECKPOINT_NEXT_STAGE" == "RUN planner" && "$LOCAL_P" -eq 0 ]] ||
     [[ "$CHECKPOINT_NEXT_STAGE" == "RUN spec-linter" && "$LOCAL_SL" -eq 0 ]] ||
     [[ "$CHECKPOINT_NEXT_STAGE" == "RUN reviewer" && "$LOCAL_R" -eq 0 ]] ||
     [[ "$CHECKPOINT_NEXT_STAGE" == "RUN narrator" && "$LOCAL_N" -eq 0 ]]; then
    emit_stage "$CHECKPOINT_NEXT_STAGE"
  fi
  if { [[ "$CHECKPOINT_NEXT_STAGE" == "FIX test-author" && "$LOCAL_TA" -gt 0 ]] ||
       [[ "$CHECKPOINT_NEXT_STAGE" == "FIX builder" && "$LOCAL_B" -gt 0 ]]; }; then
    grep -qxE 'OPERATOR PUBLICATION REPAIR: (test-author|builder)' \
      "$TICKET_FILE" ||
      { echo "REFUSE repaired checkpoint lacks a publication repair directive"; exit 1; }
    CHECKPOINT_AWAIT_REOPENED=1
  fi
  if [[ "$CHECKPOINT_NEXT_STAGE" == AWAIT-OPERATOR* ]]; then
    if [[ "$LOCAL_TA" -eq 0 && "$LOCAL_B" -eq 0 ]]; then
      emit_stage "$CHECKPOINT_NEXT_STAGE"
    fi
    grep -qxE 'OPERATOR PUBLICATION REPAIR: (test-author|builder)' \
      "$TICKET_FILE" ||
      { echo "REFUSE operator-await checkpoint changed without a publication repair directive"; exit 1; }
    CHECKPOINT_AWAIT_REOPENED=1
  fi
fi

evidence_bundle_is_valid() {
  local bundle="$CONTENT_ROOT/factory/tickets/$TICKET-bundle.md"
  [[ -f "$bundle" && ! -L "$bundle" ]] || return 1
  python3 - "$bundle" <<'PY'
import re
import sys

text = open(sys.argv[1], encoding="utf-8").read()
if re.search(r"\bNOT\s+APPROVABLE\s*:", text, re.I):
    raise SystemExit(1)
required = (
    "What this does", "Preview", "Screenshots", "Acceptance criteria",
    "Risk", "Cost", "Rollback",
)
if any(not re.search(rf"^#+\s+.*{re.escape(section)}", text, re.I | re.M)
       for section in required):
    raise SystemExit(1)
if not re.search(r"approve to merge", text, re.I):
    raise SystemExit(1)
PY
}

evidence_bundle_is_not_approvable() {
  local bundle="$CONTENT_ROOT/factory/tickets/$TICKET-bundle.md"
  [[ -f "$bundle" && ! -L "$bundle" ]] || return 1
  python3 - "$bundle" <<'PY'
import sys

text = open(sys.argv[1], encoding="utf-8").read()
raise SystemExit(0 if text.startswith("NOT APPROVABLE:") else 1)
PY
}

narrator_bundle_stage() {
  local narrator_runs="$1" authorization_line authorization_count
  local attestation="$CONTENT_ROOT/factory/attestations/$TICKET/bundle.json"
  if [[ "$narrator_runs" -eq 0 ]]; then
    emit_stage "RUN narrator"
  elif [[ ! -e "$attestation" && ! -L "$attestation" ]] &&
       evidence_bundle_is_not_approvable; then
    emit_stage "FIX builder"
  elif [[ ! -e "$attestation" && ! -L "$attestation" ]] &&
       ! evidence_bundle_is_valid; then
    if [[ "$narrator_runs" -eq 1 ]]; then
      emit_stage "RUN narrator"
    elif [[ "$narrator_runs" -gt 1 ]]; then
      authorization_line="OPERATOR AUTHORIZATION: narrator round $((narrator_runs + 1))"
      authorization_count="$(grep -Fxc "$authorization_line" "$TICKET_FILE" || true)"
      if [[ "$authorization_count" -eq 1 ]]; then
        emit_stage "RUN narrator"
      elif [[ "$authorization_count" -eq 0 ]]; then
        emit_stage "AWAIT-OPERATOR semantic-round authorization required; add exact line: $authorization_line"
      else
        emit_stage "AWAIT-OPERATOR semantic-round authorization invalid; keep exactly one line: $authorization_line"
      fi
    else
      emit_stage "REFUSE Narrator run count is invalid"
    fi
  else
    echo "AWAIT-OPERATOR bundle posted; operator approval + merge is the next step"
  fi
}

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

# Evidence bundles belong to the latest effective Reviewer generation. A
# Narrator result before a later Reviewer remains immutable history, but it
# cannot decide the new generation. Count only successful Narrators after the
# latest non-void Reviewer so a repaired/re-reviewed head gets exactly one
# fresh deployed-preview pass without replaying Narrator on an unchanged
# reviewed generation.
narrators_after_latest_reviewer() {
  if [[ -n "$ROLE_EVIDENCE" ]]; then
    python3 - "$ROLE_EVIDENCE" "$VOID_RUNS" "$CHECKPOINT_R" \
      "$CHECKPOINT_N_AFTER_LATEST_R" <<'PY'
import json
import sys

path, ignored, imported_reviewers, imported_narrators = sys.argv[1:]
ignored = {int(item) for item in ignored.split(",") if item}
reviewer_ordinal = int(imported_reviewers)
latest_reviewer = reviewer_ordinal > 0
narrators = int(imported_narrators) if latest_reviewer else 0
for item in json.load(open(path, encoding="utf-8"))["records"]:
    role = item["role"]
    if role == "reviewer":
        reviewer_ordinal += 1
        if reviewer_ordinal not in ignored:
            latest_reviewer = True
            narrators = 0
    elif role == "narrator" and latest_reviewer:
        narrators += 1
print(narrators)
PY
    return
  fi
  awk -F, -v t="$TICKET" -v void_list="$VOID_RUNS" \
    -v imported_reviewers="$CHECKPOINT_R" \
    -v imported_narrators="$CHECKPOINT_N_AFTER_LATEST_R" '
  BEGIN {
    voids="," void_list ","
    reviewer_ordinal=imported_reviewers
    if (imported_reviewers>0) {
      latest_reviewer=1
      narrators=imported_narrators
    }
  }
  NR>1 && $3==t && $9=="0" {
    if ($4=="reviewer") {
      reviewer_ordinal++
      if (index(voids, "," reviewer_ordinal ",")==0) {
        latest_reviewer=1
        narrators=0
      }
    }
    else if ($4=="narrator" && latest_reviewer) narrators++
  }
  END { print narrators+0 }' "$LEDGER"
}
if [[ "$REFRESH_ACTIVE" -eq 1 ]] &&
   { [[ "$REVIEWER_RUNS" -lt "$REFRESH_REVIEWERS" ]] ||
     [[ "$A" -lt "$REFRESH_APPROVES" ]] ||
     [[ "$RC" -lt "$REFRESH_REQUESTS" ]] ||
     [[ "$N" -lt "$REFRESH_NARRATORS" ]]; }; then
  echo "REFUSE refresh receipt baselines exceed current durable evidence"
  exit 1
fi

if [[ "$P" -eq 0 ]]; then emit_stage "RUN planner"; fi

# --- spec-lint gate: plan → lint → (replan on FAIL) → tests ---
# The spec-linter appends its own verdict line (SPEC-LINT: PASS/FAIL) to the
# ticket; each planner run must be followed by one lint run, each FAIL by one
# replan. An authenticated Planner run after Test-author starts one new
# tests-first epoch; immutable earlier role evidence remains history but cannot
# skip the new spec-lint, tests, or Builder boundaries.
TEST_FIRST_EPOCH="0|complete"
if [[ -n "$ROLE_EVIDENCE" ]]; then
  TEST_FIRST_EPOCH="$(python3 - "$ROLE_EVIDENCE" <<'PY'
import json
import sys

roles = [item["role"] for item in json.load(open(sys.argv[1], encoding="utf-8"))["records"]]
planner = next((index for index in range(len(roles) - 1, -1, -1)
                if roles[index] == "planner" and "test-author" in roles[:index]), -1)
if planner < 0:
    print("0|complete")
else:
    spec = next((index for index in range(len(roles) - 1, planner, -1)
                 if roles[index] == "spec-linter"), -1)
    test = next((index for index in range(len(roles) - 1, planner, -1)
                 if roles[index] == "test-author"), -1)
    builder = next((index for index in range(len(roles) - 1, test, -1)
                    if roles[index] == "builder"), -1) if test >= 0 else -1
    phase = "spec" if spec < 0 else "test" if test < 0 else "builder" if builder < 0 else "complete"
    print(f"1|{phase}")
PY
)"
fi
IFS='|' read -r REOPENED_TEST_FIRST_EPOCH TEST_FIRST_PHASE <<<"$TEST_FIRST_EPOCH"
SLP="$(grep -ciE '^[[:space:]]*SPEC-LINT:[[:space:]]*PASS[[:space:]]*$' "$TICKET_FILE" || true)"; SLP="${SLP:-0}"
SLF="$(grep -ciE '^[[:space:]]*SPEC-LINT:[[:space:]]*FAIL([[:space:]]+—[[:space:]]+.*)?[[:space:]]*$' "$TICKET_FILE" || true)"; SLF="${SLF:-0}"
if [[ "$TA" -eq 0 || "$REOPENED_TEST_FIRST_EPOCH" -eq 1 ]]; then
  if [[ "$SL" -gt $((SLP + SLF)) ]]; then
    echo "REFUSE spec-linter has $SL successful run(s) but only $((SLP + SLF)) SPEC-LINT verdict(s) on $TICKET_FILE — the lint run must end with a 'SPEC-LINT: PASS' or 'SPEC-LINT: FAIL' line"
    exit 1
  fi
  if [[ "$SL" -lt $((SLP + SLF)) ]]; then
    echo "REFUSE ticket logs $((SLP + SLF)) SPEC-LINT verdict(s) but the ledger has only $SL successful spec-linter run(s) — correct the ticket bookkeeping"
    exit 1
  fi
fi
if [[ "$REOPENED_TEST_FIRST_EPOCH" -eq 1 && "$TEST_FIRST_PHASE" != "complete" ]]; then
  case "$TEST_FIRST_PHASE" in
    spec) emit_stage "RUN spec-linter" ;;
    test)
      LATEST_SPEC_VERDICT="$(grep -iE '^[[:space:]]*SPEC-LINT:[[:space:]]*(PASS|FAIL)' "$TICKET_FILE" | tail -1)"
      if grep -qiE 'SPEC-LINT:[[:space:]]*FAIL' <<<"$LATEST_SPEC_VERDICT"; then
        emit_stage "RUN planner"
      else
        emit_stage "RUN test-author"
      fi
      ;;
    builder) emit_stage "RUN builder" ;;
  esac
  exit 0
fi
if [[ "$TA" -eq 0 ]]; then
  if [[ "$P" -lt $((SLF + 1)) ]]; then emit_stage "RUN planner"; fi
  if [[ "$SL" -lt "$P" ]]; then emit_stage "RUN spec-linter"; fi
fi

if [[ "$TA" -eq 0 ]]; then emit_stage "RUN test-author"; fi
if [[ "$B" -eq 0 ]]; then emit_stage "RUN builder"; fi
if [[ "$REVIEWER_RUNS" -eq 0 ]]; then emit_stage "RUN reviewer"; fi

if [[ "$REVIEWER_RUNS" -gt "$VERDICTS" ]]; then
  echo "REFUSE reviewer has $REVIEWER_RUNS non-void successful run(s) but only $VERDICTS verdict(s) are logged on $TICKET_FILE — record the missing verdict, or mark a duplicate successful row with 'OPERATOR NOTE: reviewer run <ledger ordinal> void — duplicate'"
  exit 1
fi
if [[ "$REVIEWER_RUNS" -lt "$VERDICTS" ]]; then
  echo "REFUSE ticket logs $VERDICTS reviewer verdict(s) but the ledger has only $REVIEWER_RUNS non-void successful reviewer run(s) — correct the ticket bookkeeping"
  exit 1
fi

refresh_manifest_rows() { # role raw-baseline ignored-reviewer-ordinals
  if [[ -n "$ROLE_EVIDENCE" ]]; then
    python3 - "$ROLE_EVIDENCE" "$1" "$2" "$3" <<'PY'
import json
import sys

path, role, baseline, ignored = sys.argv[1:]
baseline = int(baseline)
ignored = {int(item) for item in ignored.split(",") if item}
value = json.load(open(path, encoding="utf-8"))
ordinal = 0
for index, item in enumerate(value["records"], 1):
    if item["role"] != role:
        continue
    ordinal += 1
    if ordinal <= baseline or (role == "reviewer" and ordinal in ignored):
        continue
    print(f"{index}|{item['head_before']}")
PY
    return
  fi
  python3 - "$LEDGER" "$FACTORY_DIR/runs" "$TICKET" "$1" "$2" "$3" <<'PY'
import csv
import os
import re
import stat
import sys

ledger, runs, ticket, role, baseline, ignored = sys.argv[1:]
baseline = int(baseline)
ignored = {int(item) for item in ignored.split(",") if item}
with open(ledger, newline="", encoding="utf-8") as handle:
    rows = list(csv.DictReader(handle))
selected = []
ordinal = 0
for index, row in enumerate(rows, 1):
    if row.get("ticket") != ticket or row.get("role") != role or row.get("exit_status") != "0":
        continue
    ordinal += 1
    if ordinal <= baseline or (role == "reviewer" and ordinal in ignored):
        continue
    run_id = row.get("run_id", "")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", run_id):
        raise SystemExit(1)
    selected.append((index, run_id))

manifests = {}
directory = os.stat(runs, follow_symlinks=False)
if not stat.S_ISDIR(directory.st_mode):
    raise SystemExit(1)
for name in os.listdir(runs):
    if not name.endswith(".meta"):
        continue
    path = os.path.join(runs, name)
    info = os.stat(path, follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise SystemExit(1)
    values = {}
    with open(path, encoding="utf-8") as handle:
        for line in handle.read().splitlines():
            if not line or "=" not in line:
                raise SystemExit(1)
            key, value = line.split("=", 1)
            if key in values:
                raise SystemExit(1)
            values[key] = value
    run_id = values.get("run_id", "")
    if run_id in manifests:
        raise SystemExit(1)
    manifests[run_id] = values

for index, run_id in selected:
    value = manifests.get(run_id, {})
    head = value.get("role_head_before", "")
    if (
        value.get("ticket") != ticket
        or value.get("role") != role
        or value.get("phase") != "completed"
        or value.get("accounting_schema") != "1"
        or value.get("accounting_state") not in {"completed", "abandoned_conservative"}
        or value.get("go_issued") != "1"
        or value.get("task_submitted") != "1"
        or value.get("exit_status") != "0"
        or value.get("role_exit") != "ok"
        or (
            value.get("accounting_state") == "abandoned_conservative"
            and value.get("cost_basis") != "conservative_reservation"
        )
        or not re.fullmatch(r"[0-9a-f]{40}", head)
    ):
        raise SystemExit(1)
    print(f"{index}|{head}")
PY
}

# A control-only base advance preserves only the effective evidence that
# actually belongs to the receipt-bound old head. Earlier successful rows left
# behind by an authorized force-push remain auditable, but do not invalidate a
# later valid Reviewer/Narrator pair on the surviving lineage.
if [[ "$REFRESH_ACTIVE" -eq 1 && "$REFRESH_PRESERVE_REVIEW" -eq 1 ]]; then
  if ! PRESERVED_REVIEW_ROWS="$(refresh_manifest_rows reviewer 0 "$OLD_VOID_RUNS")" ||
     ! PRESERVED_NARRATOR_ROWS="$(refresh_manifest_rows narrator 0 "")"; then
    echo "REFUSE preserved refresh evidence lacks an exact successful run manifest"
    exit 1
  fi
  PRESERVED_INDEX=0
  PRESERVED_REVIEW_LEDGER=0
  PRESERVED_REVIEW_HEAD=""
  while IFS='|' read -r _index evidence_head; do
    [[ -n "$evidence_head" ]] || continue
    PRESERVED_INDEX=$((PRESERVED_INDEX + 1))
    if [[ "$PRESERVED_INDEX" -le "$REFRESH_REVIEWERS" ]]; then
      PRESERVED_REVIEW_LEDGER="$_index"
      PRESERVED_REVIEW_HEAD="$evidence_head"
    fi
  done <<< "$PRESERVED_REVIEW_ROWS"
  if [[ "$REFRESH_REVIEWERS" -gt 0 ]] &&
     { [[ -z "$PRESERVED_REVIEW_HEAD" ]] ||
       ! git -C "$TICKET_WORKTREE_ROOT" merge-base --is-ancestor \
         "$PRESERVED_REVIEW_HEAD" "$REFRESH_OLD_HEAD" 2>/dev/null; }; then
    REFRESH_PRESERVE_REVIEW=0
  fi
  PRESERVED_INDEX=0
  PRESERVED_NARRATOR_LEDGER=0
  PRESERVED_NARRATOR_HEAD=""
  while IFS='|' read -r _index evidence_head; do
    [[ -n "$evidence_head" ]] || continue
    PRESERVED_INDEX=$((PRESERVED_INDEX + 1))
    if [[ "$PRESERVED_INDEX" -le "$REFRESH_NARRATORS" ]]; then
      PRESERVED_NARRATOR_LEDGER="$_index"
      PRESERVED_NARRATOR_HEAD="$evidence_head"
    fi
  done <<< "$PRESERVED_NARRATOR_ROWS"
  if [[ "$REFRESH_PRESERVE_REVIEW" -eq 1 &&
        "$REFRESH_NARRATORS" -gt 0 &&
        -n "$PRESERVED_NARRATOR_HEAD" ]] &&
     git -C "$TICKET_WORKTREE_ROOT" merge-base --is-ancestor \
       "$PRESERVED_NARRATOR_HEAD" "$REFRESH_OLD_HEAD" 2>/dev/null &&
     [[ "$PRESERVED_NARRATOR_LEDGER" -gt "$PRESERVED_REVIEW_LEDGER" ]]; then
    REFRESH_PRESERVE_NARRATOR=1
  fi
fi

if [[ "$REFRESH_ACTIVE" -eq 1 && "$REFRESH_PRESERVE_REVIEW" -eq 0 ]]; then
  if ! FRESH_REVIEW_ROWS="$(refresh_manifest_rows reviewer \
    "$REFRESH_RAW_REVIEWERS" "$VOID_RUNS")" ||
     ! FRESH_NARRATOR_ROWS="$(refresh_manifest_rows narrator \
    "$REFRESH_NARRATORS" "")"; then
    echo "REFUSE post-refresh evidence lacks an exact successful run manifest"
    exit 1
  fi
  while IFS='|' read -r _index evidence_head; do
    [[ -n "$evidence_head" ]] || continue
    if ! git -C "$TICKET_WORKTREE_ROOT" merge-base --is-ancestor \
         "$REFRESH_COMMIT" "$evidence_head" 2>/dev/null ||
       ! git -C "$TICKET_WORKTREE_ROOT" merge-base --is-ancestor \
         "$evidence_head" "$COMMITTED_HEAD" 2>/dev/null; then
      echo "REFUSE post-refresh run manifest is not bound to refreshed branch history"
      exit 1
    fi
  done <<< "$FRESH_REVIEW_ROWS"$'\n'"$FRESH_NARRATOR_ROWS"
fi

# A Builder or Test-author run after the latest non-void Reviewer invalidates
# that review, including after a protected-base evidence refresh.
if [[ -n "$ROLE_EVIDENCE" ]]; then
  FIX_AFTER="$(python3 - "$ROLE_EVIDENCE" "$VOID_RUNS" "$CHECKPOINT_R" <<'PY'
import json
import sys

value = json.load(open(sys.argv[1], encoding="utf-8"))
voids = {int(item) for item in sys.argv[2].split(",") if item}
reviewer_run = int(sys.argv[3])
last_reviewer = reviewer_run > 0
planner = builder = test_author = builder_after_test = False
planner_head = ""
for item in value["records"]:
    role = item["role"]
    if role == "reviewer":
        reviewer_run += 1
        if reviewer_run not in voids:
            last_reviewer = True
        planner = builder = test_author = builder_after_test = False
        planner_head = ""
    elif role == "planner" and last_reviewer:
        planner = True
        planner_head = item.get("head_before", "")
    elif role == "builder" and last_reviewer:
        builder = True
        if test_author:
            builder_after_test = True
    elif role == "test-author" and last_reviewer:
        test_author = True
print(f"{int(planner)}|{int(builder)}|{int(test_author)}|{int(builder_after_test)}|{planner_head}")
PY
)"
else
  FIX_AFTER="$(awk -F, -v t="$TICKET" -v void_list="$VOID_RUNS" \
    -v imported_reviewers="$CHECKPOINT_R" '
  BEGIN {
    voids="," void_list ","
    reviewer_run=imported_reviewers
    if (imported_reviewers>0) last_r=1
  }
  NR>1 && $3==t && $9=="0" {
    if ($4=="reviewer") {
      reviewer_run++
      if (index(voids, "," reviewer_run ",")==0) {
        last_r=NR; planner=0; builder=0; test_author=0; builder_after_test=0
      }
    }
    else if ($4=="planner" && last_r>0) planner=1
    else if ($4=="builder" && last_r>0) {
      builder=1
      if (test_author) builder_after_test=1
    }
    else if ($4=="test-author" && last_r>0) test_author=1
  }
  END { print planner+0 "|" builder+0 "|" test_author+0 "|" builder_after_test+0 "|" }' "$LEDGER")"
fi
IFS='|' read -r FIX_PLANNER FIX_BUILDER FIX_TEST_AUTHOR FIX_BUILDER_AFTER_TEST FIX_PLANNER_HEAD <<<"$FIX_AFTER"

LATEST_VERDICT=""
LATEST_FIX_OWNER=""
CONTRACT17_FIX_ACTION=""
if [[ ( "$CONTRACT_VERSION" == "1.7.0" || "$CONTRACT_VERSION" == "1.8.0" ||
        "$CONTRACT_VERSION" == "2.0.0" ) &&
      "$VERDICTS" -gt 0 ]]; then
  OWNER_DATA="$(python3 - "$TICKET_FILE" <<'PY'
import re
import sys

verdicts = {}
owners = {}
for line in open(sys.argv[1], encoding="utf-8"):
    match = re.fullmatch(
        r"\s*reviewer round\s+(\d+):\s*(APPROVE|REQUEST CHANGES(?:\s+—\s+.*)?)\s*",
        line, re.I,
    )
    if match:
        round_number = int(match.group(1))
        verdict = match.group(2).upper()
        verdict = "REQUEST CHANGES" if verdict.startswith("REQUEST CHANGES") else verdict
        if round_number in verdicts:
            raise SystemExit(1)
        verdicts[round_number] = verdict
        continue
    match = re.fullmatch(
        r"\s*reviewer round\s+(\d+)\s+FIX-OWNER:\s*(builder|test-author|both)\s*",
        line, re.I,
    )
    if match:
        round_number = int(match.group(1))
        if round_number in owners:
            raise SystemExit(1)
        owners[round_number] = match.group(2).lower()

if not verdicts or set(owners) - set(verdicts):
    raise SystemExit(1)
for round_number, verdict in verdicts.items():
    if (verdict == "REQUEST CHANGES") != (round_number in owners):
        raise SystemExit(1)
latest = max(verdicts)
print(f"{verdicts[latest]}|{owners.get(latest, '')}")
PY
  )" || {
    echo "REFUSE contract 1.7 reviewer verdicts require exact, unambiguous FIX-OWNER records"
    exit 1
  }
  IFS='|' read -r LATEST_VERDICT LATEST_FIX_OWNER <<<"$OWNER_DATA"
fi

if [[ ( "$CONTRACT_VERSION" == "1.7.0" || "$CONTRACT_VERSION" == "1.8.0" ||
        "$CONTRACT_VERSION" == "2.0.0" ) &&
      "$LATEST_VERDICT" == "REQUEST CHANGES" ]]; then
  case "$LATEST_FIX_OWNER" in
    builder)
      if [[ "$FIX_BUILDER" -eq 0 ]]; then
        CONTRACT17_FIX_ACTION="FIX builder"
      else
        emit_stage "RUN reviewer"
      fi
      ;;
    test-author)
      if [[ ( "$CONTRACT_VERSION" == "1.8.0" || "$CONTRACT_VERSION" == "2.0.0" ) && "$FIX_PLANNER" -eq 0 ]]; then
        CONTRACT17_FIX_ACTION="FIX planner"
      elif [[ "$FIX_TEST_AUTHOR" -eq 0 ]]; then
        CONTRACT17_FIX_ACTION="FIX test-author"
      else
        emit_stage "RUN reviewer"
      fi
      ;;
    both)
      if [[ ( "$CONTRACT_VERSION" == "1.8.0" || "$CONTRACT_VERSION" == "2.0.0" ) && "$FIX_PLANNER" -eq 0 ]]; then
        CONTRACT17_FIX_ACTION="FIX planner"
      elif [[ "$FIX_TEST_AUTHOR" -eq 0 ]]; then
        CONTRACT17_FIX_ACTION="FIX test-author"
      elif [[ "$FIX_BUILDER_AFTER_TEST" -eq 0 ]]; then
        CONTRACT17_FIX_ACTION="FIX builder"
      else
        emit_stage "RUN reviewer"
      fi
      ;;
  esac
elif [[ "$FIX_BUILDER" -eq 1 || "$FIX_TEST_AUTHOR" -eq 1 ]]; then
  emit_stage "RUN reviewer"
fi

if [[ "$REFRESH_ACTIVE" -eq 1 && "$REFRESH_PRESERVE_REVIEW" -eq 0 ]]; then
  FRESH_REVIEWERS=$((REVIEWER_RUNS - REFRESH_REVIEWERS))
  FRESH_APPROVES=$((A - REFRESH_APPROVES))
  FRESH_REQUESTS=$((RC - REFRESH_REQUESTS))
  FRESH_VERDICTS=$((FRESH_APPROVES + FRESH_REQUESTS))
  FRESH_REVIEW_ROW_COUNT="$(printf '%s\n' "$FRESH_REVIEW_ROWS" | \
    awk -F'|' 'NF==2 { count++ } END { print count+0 }')"
  if [[ "$FRESH_REVIEW_ROW_COUNT" -ne "$FRESH_REVIEWERS" ]]; then
    echo "REFUSE fresh Reviewer manifest selection does not match sequenced evidence"
    exit 1
  fi
  if [[ "$FRESH_REVIEWERS" -eq 0 ]]; then emit_stage "RUN reviewer"; fi
  if [[ "$FRESH_REVIEWERS" -ne "$FRESH_VERDICTS" ]]; then
    echo "REFUSE refreshed reviewer has $FRESH_REVIEWERS successful run(s) but $FRESH_VERDICTS post-refresh verdict(s) — record the missing verdict"
    exit 1
  fi
  LATEST_FRESH_VERDICT="$(awk -v skip="$((REFRESH_APPROVES + REFRESH_REQUESTS))" '
    /^[[:space:]]*reviewer round[[:space:]]+[0-9]+:[[:space:]]*APPROVE[[:space:]]*$/ { if (++seen > skip) latest="APPROVE" }
    /^[[:space:]]*reviewer round[[:space:]]+[0-9]+:[[:space:]]*REQUEST CHANGES([[:space:]]+—[[:space:]]+.*)?[[:space:]]*$/ { if (++seen > skip) latest="REQUEST CHANGES" }
    END { print latest }
  ' "$TICKET_FILE")"
  if [[ "$LATEST_FRESH_VERDICT" == "APPROVE" ]]; then
    LAST_FRESH_REVIEW_INDEX="$(printf '%s\n' "$FRESH_REVIEW_ROWS" | \
      awk -F'|' 'NF==2 { value=$1 } END { print value+0 }')"
    NARRATOR_AFTER_REVIEWER="$(printf '%s\n' "$FRESH_NARRATOR_ROWS" | \
      awk -F'|' -v review="$LAST_FRESH_REVIEW_INDEX" 'NF==2 && $1>review { count++ } END { print count+0 }')"
    narrator_bundle_stage "$NARRATOR_AFTER_REVIEWER"
    exit 0
  fi
  # A post-refresh rejection must use the ordinary fix/re-review path below;
  # an approval from the invalidated generation cannot short-circuit it.
elif [[ "$A" -ge 1 &&
        ( ( "$CONTRACT_VERSION" != "1.7.0" &&
            "$CONTRACT_VERSION" != "1.8.0" &&
            "$CONTRACT_VERSION" != "2.0.0" ) ||
          "$LATEST_VERDICT" == "APPROVE" ) ]]; then
  if [[ "$REFRESH_ACTIVE" -eq 1 &&
        "$REFRESH_PRESERVE_REVIEW" -eq 1 &&
        "$REFRESH_PRESERVE_NARRATOR" -eq 0 ]]; then
    emit_stage "RUN narrator"
  fi
  if [[ "$CHECKPOINT_AWAIT_REOPENED" -eq 1 && "$LOCAL_N" -eq 0 ]]; then
    emit_stage "RUN narrator"
  fi
  # Approval is evidence-sensitive: an ignored operator overlay may inform the
  # future bundle-attestation path. Contract 1.2 stops before that boundary;
  # it refuses both live Receipt headers and historical Linear-era headers.
  if [[ "$CONTRACT_VERSION" == "1.2.0" ]] &&
     grep -qiE '^Operator-Approval:[[:space:]]*(Linear|Receipt)[[:space:]]*$' "$TICKET_FILE"; then
    echo "REFUSE contract 1.2 has no trusted bundle-attestation path for approval"
    exit 1
  fi
  NARRATORS_AFTER_LATEST_REVIEWER="$(narrators_after_latest_reviewer)" || {
    echo "REFUSE latest Reviewer/Narrator generation could not be reduced"
    exit 1
  }
  narrator_bundle_stage "$NARRATORS_AFTER_LATEST_REVIEWER"
  exit 0
fi

if [[ ( "$CONTRACT_VERSION" == "1.8.0" || "$CONTRACT_VERSION" == "2.0.0" ) &&
      ( "$LATEST_FIX_OWNER" == "test-author" || "$LATEST_FIX_OWNER" == "both" ) &&
      "$FIX_PLANNER" -eq 1 && "$CONTRACT17_FIX_ACTION" != "FIX planner" ]]; then
  python3 - "$TICKET_WORKTREE_ROOT" "$SOURCE_TICKET_FILE" "$COMMITTED_HEAD" <<'PY' || {
import pathlib
import re
import subprocess
import sys

repo, ticket, head = sys.argv[1:]
if not re.fullmatch(r"[0-9a-f]{40}", head):
    raise SystemExit(1)
relative = pathlib.Path(ticket).resolve().relative_to(pathlib.Path(repo).resolve()).as_posix()
files = subprocess.run(
    ["git", "-C", repo, "diff-tree", "--no-commit-id", "--name-only", "-r", head],
    text=True, capture_output=True, check=True,
).stdout.splitlines()
if files != [relative]:
    raise SystemExit(1)
diff = subprocess.run(
    ["git", "-C", repo, "diff", "--unified=0", f"{head}^", head, "--", relative],
    text=True, capture_output=True, check=True,
).stdout.splitlines()
headers = [
    int(match.group(1)) for line in diff if line.startswith("+")
    if (match := re.fullmatch(r"#{2,3} Frozen contract — version ([1-9][0-9]*)", line[1:]))
]
passes = [
    int(match.group(1)) for line in diff if line.startswith("+")
    if (match := re.fullmatch(
        r"- \*\*Freeze result:\*\* PASS\. Contract version ([1-9][0-9]*) is frozen\.",
        line[1:],
    ))
]
prior = subprocess.run(
    ["git", "-C", repo, "show", f"{head}^:{relative}"],
    text=True, capture_output=True, check=True,
).stdout.splitlines()
versions = [
    int(match.group(1)) for line in prior
    if (match := re.fullmatch(r"#{2,3} Frozen contract — version ([1-9][0-9]*)", line))
]
if len(headers) != 1 or headers != passes or headers[0] <= max(versions, default=0):
    raise SystemExit(1)
PY
    echo "REFUSE Planner repair did not open one authenticated test-first contract epoch"
    exit 1
  }
fi

emit_stage "${CONTRACT17_FIX_ACTION:-FIX builder-or-test-author}"
