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
export FACTORY_HERMES_CONTRACT_VERSION=1.6.0

cleanup() { rm -rf "$TMP"; }
trap cleanup EXIT HUP INT TERM
pass() { printf 'PASS: %s\n' "$1"; }
fail() { printf 'FAIL: %s%s\n' "$1" "${2:+ — $2}" >&2; FAILURES=$((FAILURES + 1)); }

mkdir -p "$PRODUCT/factory/tickets" "$PRODUCT/factory/runs"
printf '%s\n' 'MAX_CONCURRENT_TICKETS=6' > "$PRODUCT/factory/PROJECT.env"
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
  'factory/.provider.lock/' \
  'factory/.ledger.lock/' \
  'factory/.dispatch-leases/' \
  'factory/.dispatch-leases.lock/' > "$PRODUCT/.gitignore"
for ticket in T-901 T-902 T-903 T-904 T-905 T-906 T-907 T-908 T-909; do
  printf '# %s\n\nState: Ready\n' "$ticket" > "$PRODUCT/factory/tickets/$ticket.md"
done
git -C "$PRODUCT" init -q -b main
git -C "$PRODUCT" config user.email dispatch-test@example.invalid
git -C "$PRODUCT" config user.name dispatch-test
git -C "$PRODUCT" add .gitignore factory
git -C "$PRODUCT" commit -qm fixture

pids=""
for ticket in T-901 T-902 T-903 T-904 T-905 T-906 T-907; do
  FACTORY_ROOT="$PRODUCT" "$LEASE" claim --ticket "$ticket" \
    > "$TMP/$ticket.json" 2> "$TMP/$ticket.err" &
  pids="$pids $!"
done
successes=0
for pid in $pids; do
  wait "$pid" && successes=$((successes + 1))
done
if [[ "$successes" -eq 6 && "$(find "$PRODUCT/factory/.dispatch-leases" -type f | wc -l | tr -d ' ')" -eq 6 ]]; then
  pass "Contract 1.6 atomic claims cap seven simultaneous tickets at six"
else
  fail "Contract 1.6 atomic claims cap seven simultaneous tickets at six" "successes=$successes"
fi
if grep -Fqx "dispatcher capacity is full" "$TMP"/T-*.err; then
  pass "seventh concurrent lease gets deterministic capacity refusal"
else
  fail "seventh concurrent lease gets deterministic capacity refusal"
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
THIRD_TICKET="$(printf '%s\n' "$CLAIMED" | awk 'NR==3 {print $1}')"
THIRD_ID="$(printf '%s\n' "$CLAIMED" | awk 'NR==3 {print $2}')"
FOURTH_TICKET="$(printf '%s\n' "$CLAIMED" | awk 'NR==4 {print $1}')"
FOURTH_ID="$(printf '%s\n' "$CLAIMED" | awk 'NR==4 {print $2}')"
FIFTH_TICKET="$(printf '%s\n' "$CLAIMED" | awk 'NR==5 {print $1}')"
FIFTH_ID="$(printf '%s\n' "$CLAIMED" | awk 'NR==5 {print $2}')"
SIXTH_TICKET="$(printf '%s\n' "$CLAIMED" | awk 'NR==6 {print $1}')"
SIXTH_ID="$(printf '%s\n' "$CLAIMED" | awk 'NR==6 {print $2}')"

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

LEASE_EXPIRY_BEFORE="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["expires_epoch"])' \
  "$PRODUCT/factory/.dispatch-leases/$FIRST_TICKET.json")"
MOCK_SLEEP=2 FACTORY_DISPATCH_LEASE_ID="$FIRST_ID" FACTORY_ROOT="$PRODUCT" \
  FACTORY_GLOBAL_ENV="$TMP/no-global.env" FACTORY_TEST_MODE=1 \
  FACTORY_TRUSTED_TEST_HARNESS=1 FACTORY_ADAPTER_OVERRIDE=mock \
  FACTORY_TEST_LEASE_HEARTBEAT_SECONDS=1 \
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
LEASE_EXPIRY_AFTER="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["expires_epoch"])' \
  "$PRODUCT/factory/.dispatch-leases/$FIRST_TICKET.json")"
if [[ "$RUN_RC" -eq 0 &&
      ( "$LIVE_RELEASE_RC" -eq 7 || "$LIVE_RELEASE_RC" -eq 8 ) &&
      "$LEASE_EXPIRY_AFTER" -gt "$LEASE_EXPIRY_BEFORE" ]] &&
   grep -q "mock adapter ran task" "$TMP/bounded-run.out" &&
   ! grep -q "lease leaked" "$TMP/bounded-run.out"; then
  pass "live role renews its lease without giving the adapter lease capability"
else
  fail "live role renews its lease without giving the adapter lease capability" \
    "run=$RUN_RC release=$LIVE_RELEASE_RC before=$LEASE_EXPIRY_BEFORE after=$LEASE_EXPIRY_AFTER"
fi

python3 - "$PRODUCT/factory/.dispatch-leases/$FIRST_TICKET.json" <<'PY'
import json, pathlib, time, sys
path = pathlib.Path(sys.argv[1])
value = json.loads(path.read_text())
value["claimed_epoch"] = int(time.time()) - 901
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

mkdir "$PRODUCT/factory/.launch.lock"
BUSY_RENEW_RC=0
FACTORY_ROOT="$PRODUCT" "$LEASE" renew --ticket "$FIRST_TICKET" --lease "$FIRST_ID" \
  > "$TMP/busy-renew.out" 2>&1 || BUSY_RENEW_RC=$?
if [[ "$BUSY_RENEW_RC" -eq 0 && -d "$PRODUCT/factory/.launch.lock" ]]; then
  pass "lease renewal does not wait for an unrelated provider launch lock"
else
  fail "lease renewal does not wait for an unrelated provider launch lock" "status=$BUSY_RENEW_RC"
fi
cp "$PRODUCT/factory/.dispatch-leases/$FIRST_TICKET.json" "$TMP/busy-lease-before.json"
WRONG_BUSY_RENEW_RC=0
FACTORY_ROOT="$PRODUCT" "$LEASE" renew --ticket "$FIRST_TICKET" \
  --lease 0000000000000000000000000000000000000000000000000000000000000000 \
  >/dev/null 2>&1 || WRONG_BUSY_RENEW_RC=$?
if [[ "$WRONG_BUSY_RENEW_RC" -ne 0 ]] &&
   cmp -s "$TMP/busy-lease-before.json" "$PRODUCT/factory/.dispatch-leases/$FIRST_TICKET.json"; then
  pass "busy launch lock does not weaken exact renewal ownership"
else
  fail "busy launch lock does not weaken exact renewal ownership" "status=$WRONG_BUSY_RENEW_RC"
fi
touch "$PRODUCT/factory/MAINTENANCE"
BLOCKED_BUSY_RENEW_RC=0
FACTORY_ROOT="$PRODUCT" "$LEASE" renew --ticket "$FIRST_TICKET" --lease "$FIRST_ID" \
  >/dev/null 2>&1 || BLOCKED_BUSY_RENEW_RC=$?
rm "$PRODUCT/factory/MAINTENANCE"
rmdir "$PRODUCT/factory/.launch.lock"
if [[ "$BLOCKED_BUSY_RENEW_RC" -eq 4 ]]; then
  pass "maintenance still blocks renewal while the launch lock is busy"
else
  fail "maintenance still blocks renewal while the launch lock is busy" "status=$BLOCKED_BUSY_RENEW_RC"
fi

FACTORY_ROOT="$PRODUCT" "$LEASE" release --ticket "$FIRST_TICKET" --lease "$FIRST_ID" >/dev/null
RECLAIMED="$(FACTORY_ROOT="$PRODUCT" "$LEASE" claim --ticket T-908)"
RECLAIMED_ID="$(printf '%s\n' "$RECLAIMED" | python3 -c 'import json,sys; print(json.load(sys.stdin)["lease_id"])')"
python3 - "$PRODUCT/factory/.dispatch-leases/$SECOND_TICKET.json" <<'PY'
import json, pathlib, time, sys
path = pathlib.Path(sys.argv[1])
value = json.loads(path.read_text())
value["claimed_epoch"] = int(time.time()) - 901
value["expires_epoch"] = int(time.time()) - 1
path.write_text(json.dumps(value) + "\n")
PY
RECLAIM_RC=0
FACTORY_ROOT="$PRODUCT" "$LEASE" claim --ticket T-909 > "$TMP/stale-capacity.out" 2>&1 || RECLAIM_RC=$?
if [[ "$RECLAIM_RC" -ne 0 ]] &&
   grep -Fqx "dispatcher capacity is full" "$TMP/stale-capacity.out"; then
  pass "release reclaims one slot and stale records still consume capacity"
else
  fail "release reclaims one slot and stale records still consume capacity" "status=$RECLAIM_RC"
fi

touch "$PRODUCT/factory/MAINTENANCE"
RENEW_RC=0
FACTORY_ROOT="$PRODUCT" "$LEASE" renew --ticket "$SECOND_TICKET" --lease "$SECOND_ID" >/dev/null 2>&1 || RENEW_RC=$?
FACTORY_ROOT="$PRODUCT" "$LEASE" release --ticket "$SECOND_TICKET" --lease "$SECOND_ID" >/dev/null
FACTORY_ROOT="$PRODUCT" "$LEASE" release --ticket "$THIRD_TICKET" --lease "$THIRD_ID" >/dev/null
FACTORY_ROOT="$PRODUCT" "$LEASE" release --ticket "$FOURTH_TICKET" --lease "$FOURTH_ID" >/dev/null
FACTORY_ROOT="$PRODUCT" "$LEASE" release --ticket "$FIFTH_TICKET" --lease "$FIFTH_ID" >/dev/null
FACTORY_ROOT="$PRODUCT" "$LEASE" release --ticket "$SIXTH_TICKET" --lease "$SIXTH_ID" >/dev/null
FACTORY_ROOT="$PRODUCT" "$LEASE" release --ticket T-908 --lease "$RECLAIMED_ID" >/dev/null
if [[ "$RENEW_RC" -eq 4 && -z "$(find "$PRODUCT/factory/.dispatch-leases" -type f -print -quit)" ]]; then
  pass "maintenance blocks renewal while permitting lease drain"
else
  fail "maintenance blocks renewal while permitting lease drain"
fi
rm "$PRODUCT/factory/MAINTENANCE"

for maximum in 1 2 3 4 5 6; do
  printf 'MAX_CONCURRENT_TICKETS=%s\n' "$maximum" > "$PRODUCT/factory/PROJECT.env"
  parsed="$(bash -c '. "$1"; factory_dispatch_max_tickets "$2"' _ \
    "$ROOT/scripts/lib/dispatch-leases.sh" "$PRODUCT")"
  [[ "$parsed" == "$maximum" ]] ||
    fail "project concurrency value $maximum is accepted" "parsed=$parsed"
done
pass "Contract 1.6 project concurrency values 1 through 6 are accepted"

printf '%s\n' 'MAX_CONCURRENT_TICKETS=7' > "$PRODUCT/factory/PROJECT.env"
INVALID_RC=0
FACTORY_ROOT="$PRODUCT" "$LEASE" claim --ticket T-903 >/dev/null 2>&1 || INVALID_RC=$?
[[ "$INVALID_RC" -eq 3 ]] && pass "invalid concurrency configuration fails closed" || fail "invalid concurrency configuration fails closed" "status=$INVALID_RC"
FACTORY_HERMES_CONTRACT_VERSION=1.5.0
export FACTORY_HERMES_CONTRACT_VERSION
printf '%s\n' 'MAX_CONCURRENT_TICKETS=5' > "$PRODUCT/factory/PROJECT.env"
LEGACY_INVALID_RC=0
FACTORY_ROOT="$PRODUCT" "$LEASE" claim --ticket T-903 >/dev/null 2>&1 || LEGACY_INVALID_RC=$?
[[ "$LEGACY_INVALID_RC" -eq 3 ]] &&
  pass "Contract 1.5 retains the 1 through 4 capacity bound" ||
  fail "Contract 1.5 retains the 1 through 4 capacity bound" "status=$LEGACY_INVALID_RC"
FACTORY_HERMES_CONTRACT_VERSION=1.6.0
export FACTORY_HERMES_CONTRACT_VERSION
printf '%s\n' 'MAX_CONCURRENT_TICKETS=6' > "$PRODUCT/factory/PROJECT.env"
mv "$PRODUCT/factory/PROJECT.env" "$PRODUCT/factory/PROJECT.env.real"
ln -s PROJECT.env.real "$PRODUCT/factory/PROJECT.env"
UNSAFE_PROJECT_RC=0
FACTORY_ROOT="$PRODUCT" "$LEASE" claim --ticket T-903 >/dev/null 2>&1 ||
  UNSAFE_PROJECT_RC=$?
if [[ "$UNSAFE_PROJECT_RC" -eq 3 ]]; then
  pass "unsafe project configuration fails closed"
else
  fail "unsafe project configuration fails closed" "status=$UNSAFE_PROJECT_RC"
fi
rm "$PRODUCT/factory/PROJECT.env"
mv "$PRODUCT/factory/PROJECT.env.real" "$PRODUCT/factory/PROJECT.env"

mkdir -p "$PRODUCT/factory/.dispatch-leases"
NOW="$(date +%s)"
python3 - "$PRODUCT/factory/.dispatch-leases" "$NOW" <<'PY'
import json, pathlib, sys
root, now = pathlib.Path(sys.argv[1]), int(sys.argv[2])
lease_id = "a" * 64
for ticket in ("T-901", "T-902"):
    (root / f"{ticket}.json").write_text(json.dumps({
        "schema_version": 1, "ticket": ticket, "lease_id": lease_id,
        "claimed_epoch": now, "expires_epoch": now + 900,
    }) + "\n")
PY
DUPLICATE_LEASE_RC=0
FACTORY_ROOT="$PRODUCT" "$LEASE" claim --ticket T-903 > "$TMP/duplicate-lease.out" 2>&1 ||
  DUPLICATE_LEASE_RC=$?
if [[ "$DUPLICATE_LEASE_RC" -ne 0 ]] &&
   grep -Fqx "dispatcher lease state is unsafe" "$TMP/duplicate-lease.out"; then
  pass "duplicate lease identity makes allocation fail closed"
else
  fail "duplicate lease identity makes allocation fail closed" "status=$DUPLICATE_LEASE_RC"
fi
rm -f "$PRODUCT/factory/.dispatch-leases/"*.json

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
