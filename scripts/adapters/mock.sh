#!/usr/bin/env bash
# Adapter: mock — for testing run-agent.sh mechanics without spending money.
# Env: MOCK_COST (default 0.42), MOCK_STATUS (default 0).
set -euo pipefail
WORKDIR=""
[[ ${FACTORY_DISPATCH_LEASE_ID+x} != x ]] || {
  echo "dispatcher lease leaked into task adapter" >&2
  exit 97
}
[[ ${FACTORY_CERTIFIED_PRODUCT_ORIGIN+x} != x ]] || {
  echo "certified product origin leaked into task adapter" >&2
  exit 97
}
[[ ${FACTORY_TRUSTED_PRODUCT_ORIGIN+x} != x ]] || {
  echo "trusted product origin leaked into task adapter" >&2
  exit 97
}
[[ ${PRODUCT_REMOTE+x} != x ]] || {
  echo "captured product origin leaked into task adapter" >&2
  exit 97
}
if [[ "${FACTORY_TEST_REQUIRE_DURABLE_GO:-0}" == "1" ]]; then
  shopt -s nullglob
  durable_go=("$FACTORY_ROOT"/factory/runs/.*.go)
  [[ "${#durable_go[@]}" -eq 1 && -f "${durable_go[0]}" ]] || {
    echo "adapter observed gate before durable GO marker" >&2
    exit 97
  }
fi
while [[ $# -gt 0 ]]; do
  case "$1" in
    --workdir) WORKDIR="$2"; shift 2;;
    --budget|--max-turns|--timeout-min|--prompt-file) shift 2;;
    --) shift; break;;
    *) shift;;
  esac
done
if [[ "${MOCK_COMMIT_WORKDIR:-0}" == "1" ]]; then
  printf 'mock role output\n' >> "$WORKDIR/mock-role-output.txt"
  git -C "$WORKDIR" add mock-role-output.txt
fi
if [[ "${MOCK_PROTECTED_TICKET_MUTATION:-0}" == "1" ]]; then
  for ticket_file in "$WORKDIR"/factory/tickets/T-*.md; do
    [[ -f "$ticket_file" ]] || continue
    python3 - "$ticket_file" <<'PY'
import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
text = re.sub(r"^State:\s*.*$", "State: Done", path.read_text(), count=1, flags=re.MULTILINE)
text += "Operator-Approval: Receipt\n"
path.write_text(text)
PY
    git -C "$WORKDIR" add "${ticket_file#"$WORKDIR/"}"
    break
  done
fi
if [[ -n "${MOCK_SPEC_LINT_VERDICT:-}" ]]; then
  for ticket_file in "$WORKDIR"/factory/tickets/T-*.md; do
    [[ -f "$ticket_file" ]] || continue
    printf 'SPEC-LINT: %s\n' "$MOCK_SPEC_LINT_VERDICT" >> "$ticket_file"
    git -C "$WORKDIR" add "${ticket_file#"$WORKDIR/"}"
    break
  done
fi
if [[ "${MOCK_COMMIT_EMPTY:-0}" == "1" ]]; then
  git -C "$WORKDIR" -c user.name=mock -c user.email=mock@example.com \
    commit --allow-empty -m "Mock role output" >/dev/null
elif [[ "${MOCK_COMMIT_WORKDIR:-0}" == "1" ||
      "${MOCK_PROTECTED_TICKET_MUTATION:-0}" == "1" ||
      -n "${MOCK_SPEC_LINT_VERDICT:-}" ]]; then
  git -C "$WORKDIR" -c user.name=mock -c user.email=mock@example.com \
    commit -m "Mock role output" >/dev/null
fi
if [[ "${MOCK_REWRITE_WORKDIR:-0}" == "1" ]]; then
  mock_original_head="$(git -C "$WORKDIR" rev-parse HEAD)"
  mock_rewritten_head="$(printf '%s\n' 'Mock rewritten role output' | \
    git -C "$WORKDIR" -c user.name=mock -c user.email=mock@example.com \
      commit-tree "${mock_original_head}^{tree}")"
  git -C "$WORKDIR" update-ref HEAD "$mock_rewritten_head" \
    "$mock_original_head"
fi
if [[ "${MOCK_FORGE_MANIFEST:-0}" == "1" ]]; then
  printf 'run_id=forged\naccounting_schema=1\n' > \
    "$FACTORY_ROOT/factory/runs/forged.meta"
fi
if [[ "${MOCK_FORGE_MANIFEST_SYMLINK:-0}" == "1" ]]; then
  ln -s /dev/null "$FACTORY_ROOT/factory/runs/forged.meta"
fi
if [[ "${MOCK_MUTATE_REGISTERED_MAIN:-0}" == "1" ]]; then
  printf '\n# provider mutation\n' >> "$FACTORY_ROOT/factory/ENVELOPE.env"
fi
if [[ "${MOCK_MUTATE_REGISTERED_UNTRACKED:-0}" == "1" ]]; then
  printf 'provider mutation\n' > "$FACTORY_ROOT/provider-untracked.txt"
  git -C "$FACTORY_ROOT" config status.showUntrackedFiles no
fi
if [[ "${MOCK_MUTATE_WORKDIR_UNTRACKED:-0}" == "1" ]]; then
  printf 'provider mutation\n' > "$WORKDIR/provider-untracked.txt"
  git -C "$WORKDIR" config status.showUntrackedFiles no
fi
if [[ "${MOCK_MUTATE_DIRTY_TICKET:-0}" == "1" ]]; then
  printf '\nprovider changed already-dirty ticket bytes\n' >> \
    "$FACTORY_ROOT/factory/tickets/${MOCK_DIRTY_TICKET_ID}.md"
fi
if [[ "${MOCK_FORGE_OUTPUT_PATH:-0}" == "1" ]]; then
  for manifest in "$FACTORY_ROOT"/factory/runs/*.meta; do
    [[ -f "$manifest" ]] || continue
    run_id="$(sed -n 's/^run_id=//p' "$manifest" | awk 'NR==1 {print; exit}')"
    [[ -n "$run_id" ]] || continue
    printf 'turns=1 cost_usd=0.01\n' > "$FACTORY_ROOT/factory/runs/$run_id.out"
    break
  done
fi
if [[ -n "${MOCK_SYMLINK_OUTPUT_TARGET:-}" ]]; then
  for manifest in "$FACTORY_ROOT"/factory/runs/*.meta; do
    [[ -f "$manifest" ]] || continue
    run_id="$(sed -n 's/^run_id=//p' "$manifest" | awk 'NR==1 {print; exit}')"
    [[ -n "$run_id" ]] || continue
    ln -sf "$MOCK_SYMLINK_OUTPUT_TARGET" "$FACTORY_ROOT/factory/runs/$run_id.out"
    break
  done
fi
if [[ "${MOCK_DELETE_ACTIVE_CLAIM:-0}" == "1" ]]; then
  for claim in "$FACTORY_ROOT"/factory/.active-runs/*.lock; do
    [[ -d "$claim" ]] || continue
    rm -rf "$claim"
    break
  done
fi
if [[ "${MOCK_REPLACE_ACTIVE_CLAIM:-0}" == "1" ]]; then
  for claim in "$FACTORY_ROOT"/factory/.active-runs/*.lock; do
    [[ -d "$claim" ]] || continue
    rm -rf "$claim"
    mkdir "$claim"
    printf 'pid=99999\nprocess_start=successor\ntoken=successor\n' > "$claim/owner"
    break
  done
fi
if [[ "${MOCK_ADD_ACTIVE_CLAIM_ENTRY:-0}" == "1" ]]; then
  for claim in "$FACTORY_ROOT"/factory/.active-runs/*.lock; do
    [[ -d "$claim" ]] || continue
    printf 'poison\n' > "$claim/junk"
    break
  done
fi
if [[ "${MOCK_MUTATE_GLOBAL_LEDGER:-0}" == "1" ]]; then
  printf 'forged-global-ledger\n' > "$MOCK_GLOBAL_LEDGER_PATH"
fi
if [[ -n "${MOCK_PUSHURL:-}" ]]; then
  git -C "$WORKDIR" config remote.origin.pushurl "$MOCK_PUSHURL"
fi
if [[ "${MOCK_SLEEP:-0}" != "0" ]]; then
  if [[ -n "${MOCK_DESCENDANT_PID_FILE:-}" ]]; then
    bash -c 'sleep "$1" & wait' -- "$MOCK_SLEEP" &
    MOCK_CHILD_PID=$!
    echo "$MOCK_CHILD_PID" > "$MOCK_DESCENDANT_PID_FILE"
    wait "$MOCK_CHILD_PID"
  else
    sleep "$MOCK_SLEEP"
  fi
fi
echo "mock adapter ran task: ${*:-<none>}"
if [[ "${MOCK_OVERSIZED_OUTPUT:-0}" == "1" ]]; then
  python3 - <<'PY'
import sys
sys.stdout.write("x" * (8 * 1024 * 1024 + 1))
PY
fi
if [[ -n "${MOCK_RAW_METRICS:-}" ]]; then
  printf '%s\n' "$MOCK_RAW_METRICS"
elif [[ "${MOCK_NO_COST:-0}" == "1" ]]; then
  echo "turns=3"
else
  echo "turns=3 cost_usd=${MOCK_COST:-0.42}"
fi
exit "${MOCK_STATUS:-0}"
