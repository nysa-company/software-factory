#!/usr/bin/env bash
# Focused checks for bounded dispatcher ticket leases.
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LEASE="$ROOT/scripts/dispatch-lease.sh"
NEXT="$ROOT/scripts/next-stage.sh"
RUN="$ROOT/scripts/run-agent.sh"
KILL="$ROOT/scripts/kill-switch.sh"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/dispatch-leases-test.XXXXXX")"
PRODUCT="$TMP/product"
FAILURES=0

cleanup() { rm -rf "$TMP"; }
trap cleanup EXIT HUP INT TERM
pass() { printf 'PASS: %s\n' "$1"; }
fail() { printf 'FAIL: %s%s\n' "$1" "${2:+ — $2}" >&2; FAILURES=$((FAILURES + 1)); }

mkdir -p "$PRODUCT/factory/tickets" "$PRODUCT/factory/runs"
printf '%s\n' 'MAX_CONCURRENT_TICKETS=2' > "$PRODUCT/factory/PROJECT.env"
printf '%s\n' \
  'PER_RUN_BUDGET_USD=1.00' \
  'PER_TICKET_BUDGET_USD=10.00' \
  'PER_RUN_MAX_TURNS=3' \
  'PER_RUN_TIMEOUT_MIN=1' \
  'DAILY_CAP_USD=20.00' \
  > "$PRODUCT/factory/ENVELOPE.env"
printf '%s\n' "$(git -C "$ROOT" rev-parse HEAD)" > "$PRODUCT/factory/KIT_PIN"
printf '%s\n' 'date,time,ticket,role,adapter,prompt_version,turns,cost_usd,exit_status,run_id,provider_family,model_id,selection_reason,cost_basis,adapter_version' \
  > "$PRODUCT/factory/ledger.csv"
printf '%s\n' \
  'factory/runtime-ledger.csv' \
  'factory/runs/' \
  'factory/.active-runs/' \
  'factory/.launch.lock/' \
  'factory/.ledger.lock/' \
  'factory/.dispatch-leases/' \
  'factory/.dispatch-leases.lock/' > "$PRODUCT/.gitignore"
for ticket in T-901 T-902 T-903; do
  printf '# %s\n\nState: Ready\n' "$ticket" > "$PRODUCT/factory/tickets/$ticket.md"
done
git -C "$PRODUCT" init -q -b main
git -C "$PRODUCT" config user.email dispatch-test@example.invalid
git -C "$PRODUCT" config user.name dispatch-test
git -C "$PRODUCT" add .gitignore factory
git -C "$PRODUCT" commit -qm fixture

pids=""
for ticket in T-901 T-902 T-903; do
  FACTORY_ROOT="$PRODUCT" "$LEASE" claim --ticket "$ticket" \
    > "$TMP/$ticket.json" 2> "$TMP/$ticket.err" &
  pids="$pids $!"
done
successes=0
for pid in $pids; do
  wait "$pid" && successes=$((successes + 1))
done
if [[ "$successes" -eq 2 && "$(find "$PRODUCT/factory/.dispatch-leases" -type f | wc -l | tr -d ' ')" -eq 2 ]]; then
  pass "atomic claims cap three simultaneous tickets at two"
else
  fail "atomic claims cap three simultaneous tickets at two" "successes=$successes"
fi

CLAIMED="$(python3 - "$TMP" <<'PY'
import json, pathlib, sys
for path in sorted(pathlib.Path(sys.argv[1]).glob("T-*.json")):
    try:
        value = json.loads(path.read_text())
        print(value["ticket"] + " " + value["lease_id"])
    except Exception:
        pass
PY
)"
FIRST_TICKET="$(printf '%s\n' "$CLAIMED" | awk 'NR==1 {print $1}')"
FIRST_ID="$(printf '%s\n' "$CLAIMED" | awk 'NR==1 {print $2}')"
SECOND_TICKET="$(printf '%s\n' "$CLAIMED" | awk 'NR==2 {print $1}')"
SECOND_ID="$(printf '%s\n' "$CLAIMED" | awk 'NR==2 {print $2}')"

DUPLICATE_RC=0
FACTORY_ROOT="$PRODUCT" "$LEASE" claim --ticket "$FIRST_TICKET" >/dev/null 2>&1 || DUPLICATE_RC=$?
[[ "$DUPLICATE_RC" -ne 0 ]] && pass "duplicate ticket claim is refused" || fail "duplicate ticket claim is refused"

WRONG_STAGE="$(FACTORY_ROOT="$PRODUCT" "$NEXT" --ticket "$FIRST_TICKET" \
  --lease 0000000000000000000000000000000000000000000000000000000000000000 2>&1)"
RIGHT_STAGE="$(FACTORY_ROOT="$PRODUCT" "$NEXT" --ticket "$FIRST_TICKET" --lease "$FIRST_ID" 2>&1)"
if [[ "$WRONG_STAGE" == "REFUSE dispatcher lease is missing"* && "$RIGHT_STAGE" == "RUN planner" ]]; then
  pass "sequencing requires the matching lease"
else
  fail "sequencing requires the matching lease" "wrong=$WRONG_STAGE right=$RIGHT_STAGE"
fi

MOCK_SLEEP=2 FACTORY_DISPATCH_LEASE_ID="$FIRST_ID" FACTORY_ROOT="$PRODUCT" \
  FACTORY_GLOBAL_ENV="$TMP/no-global.env" FACTORY_TEST_MODE=1 \
  FACTORY_TRUSTED_TEST_HARNESS=1 FACTORY_ADAPTER_OVERRIDE=mock \
  "$RUN" --role planner --ticket "$FIRST_TICKET" -- "bounded run" > "$TMP/bounded-run.out" 2>&1 &
RUN_PID=$!
for _try in $(seq 1 200); do
  compgen -G "$PRODUCT/factory/.active-runs/$FIRST_TICKET.*.lock" >/dev/null && break
  sleep 0.02
done
LIVE_RELEASE_RC=0
FACTORY_ROOT="$PRODUCT" "$LEASE" release --ticket "$FIRST_TICKET" --lease "$FIRST_ID" \
  > "$TMP/live-release.out" 2>&1 || LIVE_RELEASE_RC=$?
RUN_RC=0
wait "$RUN_PID" || RUN_RC=$?
if [[ "$RUN_RC" -eq 0 && "$LIVE_RELEASE_RC" -eq 7 ]] &&
   grep -q "mock adapter ran task" "$TMP/bounded-run.out" &&
   ! grep -q "lease leaked" "$TMP/bounded-run.out"; then
  pass "live role keeps its lease while the task adapter receives no lease capability"
else
  fail "live role keeps its lease while the task adapter receives no lease capability" \
    "run=$RUN_RC release=$LIVE_RELEASE_RC"
fi

python3 - "$PRODUCT/factory/.dispatch-leases/$FIRST_TICKET.json" <<'PY'
import json, pathlib, time, sys
path = pathlib.Path(sys.argv[1])
value = json.loads(path.read_text())
value["expires_epoch"] = int(time.time()) - 1
path.write_text(json.dumps(value) + "\n")
PY
STALE_STAGE="$(FACTORY_ROOT="$PRODUCT" "$NEXT" --ticket "$FIRST_TICKET" --lease "$FIRST_ID" 2>&1)"
FACTORY_ROOT="$PRODUCT" "$LEASE" renew --ticket "$FIRST_TICKET" --lease "$FIRST_ID" >/dev/null
RENEWED_STAGE="$(FACTORY_ROOT="$PRODUCT" "$NEXT" --ticket "$FIRST_TICKET" --lease "$FIRST_ID" 2>&1)"
if [[ "$STALE_STAGE" == "REFUSE dispatcher lease is stale"* && "$RENEWED_STAGE" == "RUN spec-linter" ]]; then
  pass "stale lease blocks work until its owner renews"
else
  fail "stale lease blocks work until its owner renews" "stale=$STALE_STAGE renewed=$RENEWED_STAGE"
fi

touch "$PRODUCT/factory/MAINTENANCE"
RENEW_RC=0
FACTORY_ROOT="$PRODUCT" "$LEASE" renew --ticket "$FIRST_TICKET" --lease "$FIRST_ID" >/dev/null 2>&1 || RENEW_RC=$?
FACTORY_ROOT="$PRODUCT" "$LEASE" release --ticket "$FIRST_TICKET" --lease "$FIRST_ID" >/dev/null
FACTORY_ROOT="$PRODUCT" "$LEASE" release --ticket "$SECOND_TICKET" --lease "$SECOND_ID" >/dev/null
if [[ "$RENEW_RC" -eq 4 && -z "$(find "$PRODUCT/factory/.dispatch-leases" -type f -print -quit)" ]]; then
  pass "maintenance blocks renewal while permitting lease drain"
else
  fail "maintenance blocks renewal while permitting lease drain"
fi
rm "$PRODUCT/factory/MAINTENANCE"

printf '%s\n' 'MAX_CONCURRENT_TICKETS=3' > "$PRODUCT/factory/PROJECT.env"
INVALID_RC=0
FACTORY_ROOT="$PRODUCT" "$LEASE" claim --ticket T-903 >/dev/null 2>&1 || INVALID_RC=$?
[[ "$INVALID_RC" -eq 3 ]] && pass "invalid concurrency configuration fails closed" || fail "invalid concurrency configuration fails closed" "status=$INVALID_RC"
printf '%s\n' 'MAX_CONCURRENT_TICKETS=2' > "$PRODUCT/factory/PROJECT.env"
FACTORY_ROOT="$PRODUCT" "$LEASE" claim --ticket T-903 >/dev/null
FACTORY_SKIP_SCHEDULE_STOP=1 "$KILL" "$PRODUCT" >/dev/null
if [[ -f "$PRODUCT/factory/KILL" && -z "$(find "$PRODUCT/factory/.dispatch-leases" -type f -print -quit)" ]]; then
  pass "kill switch blocks claims and drains dispatcher leases"
else
  fail "kill switch blocks claims and drains dispatcher leases"
fi

if [[ "$FAILURES" -gt 0 ]]; then
  echo "$FAILURES dispatch lease test(s) failed" >&2
  exit 1
fi
echo "All dispatch lease tests passed."
