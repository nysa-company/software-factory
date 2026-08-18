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
export FACTORY_CONTRACT_VERSION=1.6.0

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

mkdir "$PRODUCT/factory/.launch.lock"
( sleep 11; rmdir "$PRODUCT/factory/.launch.lock" ) &
TRANSIENT_LOCK_PID=$!
TRANSIENT_RC=0
TRANSIENT_CLAIM="$(FACTORY_ROOT="$PRODUCT" "$LEASE" claim --ticket T-901 \
  2> "$TMP/transient-claim.err")" || TRANSIENT_RC=$?
wait "$TRANSIENT_LOCK_PID"
TRANSIENT_LEASE=""
if [[ "$TRANSIENT_RC" -eq 0 ]]; then
  TRANSIENT_LEASE="$(printf '%s\n' "$TRANSIENT_CLAIM" | python3 -c \
    'import json,sys; print(json.load(sys.stdin)["lease_id"])')"
  FACTORY_ROOT="$PRODUCT" "$LEASE" release --ticket T-901 \
    --lease "$TRANSIENT_LEASE" >/dev/null
fi
if [[ "$TRANSIENT_RC" -eq 0 && "$TRANSIENT_LEASE" =~ ^[0-9a-f]{64}$ ]]; then
  pass "dispatcher claim outwaits transient provider launch setup"
else
  fail "dispatcher claim outwaits transient provider launch setup"
fi

mkdir "$PRODUCT/factory/.launch.lock"
( sleep 1; : > "$PRODUCT/factory/MAINTENANCE"; rmdir "$PRODUCT/factory/.launch.lock" ) &
MAINTENANCE_LOCK_PID=$!
MAINTENANCE_CLAIM_RC=0
FACTORY_ROOT="$PRODUCT" "$LEASE" claim --ticket T-901 >/dev/null 2>&1 ||
  MAINTENANCE_CLAIM_RC=$?
wait "$MAINTENANCE_LOCK_PID"
rm "$PRODUCT/factory/MAINTENANCE"
if [[ "$MAINTENANCE_CLAIM_RC" -eq 4 &&
      ! -e "$PRODUCT/factory/.dispatch-leases/T-901.json" ]]; then
  pass "maintenance appearing during claim wait wins before admission"
else
  fail "maintenance appearing during claim wait wins before admission"
fi

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

SERIAL="$TMP/serial"
mkdir -p "$SERIAL/factory/tickets"
printf '%s\n' 'MAX_CONCURRENT_TICKETS=1' > "$SERIAL/factory/PROJECT.env"
printf '# T-1\n' > "$SERIAL/factory/tickets/T-1.md"
printf '# T-2\n' > "$SERIAL/factory/tickets/T-2.md"
mkdir -p "$SERIAL/factory/.active-runs/T-9.planner.lock"
chmod 700 "$SERIAL/factory/.active-runs"
SERIAL_ACTIVE_RC=0
FACTORY_ROOT="$SERIAL" "$LEASE" claim --ticket T-1 \
  > "$TMP/serial-active.out" 2>&1 || SERIAL_ACTIVE_RC=$?
rmdir "$SERIAL/factory/.active-runs/T-9.planner.lock"
SERIAL_CLAIM="$(FACTORY_ROOT="$SERIAL" "$LEASE" claim --ticket T-1)"
SERIAL_LEASE="$(printf '%s\n' "$SERIAL_CLAIM" | python3 -c \
  'import json,sys; print(json.load(sys.stdin)["lease_id"])')"
FACTORY_ROOT="$SERIAL" "$LEASE" renew --ticket T-1 --lease "$SERIAL_LEASE" >/dev/null
SERIAL_MISSING_RC=0
bash -c '. "$1"; factory_dispatch_require_lease "$2" T-2 ""' \
  _ "$ROOT/scripts/lib/dispatch-leases.sh" "$SERIAL" \
  >/dev/null 2>&1 || SERIAL_MISSING_RC=$?
SERIAL_MATCH_RC=0
bash -c '. "$1"; factory_dispatch_require_lease "$2" T-1 "$3"' \
  _ "$ROOT/scripts/lib/dispatch-leases.sh" "$SERIAL" "$SERIAL_LEASE" \
  >/dev/null 2>&1 || SERIAL_MATCH_RC=$?
FACTORY_ROOT="$SERIAL" "$LEASE" release --ticket T-1 --lease "$SERIAL_LEASE" >/dev/null
SERIAL_SECOND="$(FACTORY_ROOT="$SERIAL" "$LEASE" claim --ticket T-2)"
SERIAL_SECOND_LEASE="$(printf '%s\n' "$SERIAL_SECOND" | python3 -c \
  'import json,sys; print(json.load(sys.stdin)["lease_id"])')"
python3 - "$SERIAL/factory/.dispatch-leases/T-2.json" <<'PY'
import json, pathlib, sys, time
path = pathlib.Path(sys.argv[1])
value = json.loads(path.read_text())
value["claimed_epoch"] = int(time.time()) - 901
value["expires_epoch"] = int(time.time()) - 1
path.write_text(json.dumps(value) + "\n")
PY
FACTORY_RELEASE_CONTRACT_VERSION=2.0.0 FACTORY_ROOT="$SERIAL" \
  "$LEASE" release-expired --ticket T-2 --lease "$SERIAL_SECOND_LEASE" >/dev/null
SERIAL_EMPTY_RC=0
bash -c '. "$1"; factory_dispatch_require_lease "$2" T-2 ""' \
  _ "$ROOT/scripts/lib/dispatch-leases.sh" "$SERIAL" \
  >/dev/null 2>&1 || SERIAL_EMPTY_RC=$?
if [[ "$SERIAL_ACTIVE_RC" -ne 0 &&
      "$(cat "$TMP/serial-active.out")" == "dispatcher capacity is full" &&
      "$SERIAL_MISSING_RC" -ne 0 && "$SERIAL_MATCH_RC" -eq 0 &&
      "$SERIAL_EMPTY_RC" -eq 0 &&
      -z "$(find "$SERIAL/factory/.dispatch-leases" -type f -print -quit)" ]]; then
  pass "capacity one serializes entry points and completes the lease lifecycle"
else
  fail "capacity one serializes entry points and completes the lease lifecycle" \
    "missing=$SERIAL_MISSING_RC matching=$SERIAL_MATCH_RC empty=$SERIAL_EMPTY_RC"
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
MOCK_SLEEP=5 FACTORY_DISPATCH_LEASE_ID="$FIRST_ID" FACTORY_ROOT="$PRODUCT" \
  FACTORY_GLOBAL_ENV="$TMP/no-global.env" FACTORY_TEST_MODE=1 \
  FACTORY_TRUSTED_TEST_HARNESS=1 FACTORY_ADAPTER_OVERRIDE=mock \
  FACTORY_TEST_LEASE_HEARTBEAT_SECONDS=1 \
  "$RUN" --role planner --ticket "$FIRST_TICKET" -- "bounded run" > "$TMP/bounded-run.out" 2>&1 &
RUN_PID=$!
for _try in $(seq 1 200); do
  compgen -G "$PRODUCT/factory/.active-runs/$FIRST_TICKET.*.lock" >/dev/null && break
  sleep 0.02
done
SUBMITTED=0
for _try in $(seq 1 1200); do
  if compgen -G "$PRODUCT/factory/runs/.*.submitted" >/dev/null; then
    SUBMITTED=1
    break
  fi
  sleep 0.02
done
[[ "$SUBMITTED" -eq 0 ]] || touch "$PRODUCT/factory/MAINTENANCE"
RUN_RC=0
wait "$RUN_PID" || RUN_RC=$?
[[ "$SUBMITTED" -eq 0 ]] || rm "$PRODUCT/factory/MAINTENANCE"
LEASE_EXPIRY_AFTER="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["expires_epoch"])' \
  "$PRODUCT/factory/.dispatch-leases/$FIRST_TICKET.json")"
if [[ "$SUBMITTED" -eq 1 && "$RUN_RC" -eq 0 &&
      "$LEASE_EXPIRY_AFTER" -gt "$LEASE_EXPIRY_BEFORE" ]] &&
   grep -q "mock adapter ran task" "$TMP/bounded-run.out" &&
   ! grep -q "lease leaked" "$TMP/bounded-run.out"; then
  pass "maintenance drains a live role without giving the adapter lease capability"
else
  fail "maintenance drains a live role without giving the adapter lease capability" \
    "submitted=$SUBMITTED run=$RUN_RC before=$LEASE_EXPIRY_BEFORE after=$LEASE_EXPIRY_AFTER"
fi

STUBBORN_STARTED="$(date +%s)"
STUBBORN_RC=0
MOCK_SLEEP=1 FACTORY_DISPATCH_LEASE_ID="$SECOND_ID" FACTORY_ROOT="$PRODUCT" \
  FACTORY_GLOBAL_ENV="$TMP/no-global.env" FACTORY_TEST_MODE=1 \
  FACTORY_TRUSTED_TEST_HARNESS=1 FACTORY_ADAPTER_OVERRIDE=mock \
  FACTORY_TEST_LEASE_HEARTBEAT_SECONDS=1 \
  FACTORY_TEST_LEASE_HEARTBEAT_IGNORE_TERM=1 \
  "$RUN" --role planner --ticket "$SECOND_TICKET" -- "stubborn heartbeat" \
  > "$TMP/stubborn-heartbeat.out" 2>&1 || STUBBORN_RC=$?
STUBBORN_ELAPSED=$(( $(date +%s) - STUBBORN_STARTED ))
STUBBORN_TERMINALS="$(python3 - "$PRODUCT/factory/runs" "$SECOND_TICKET" <<'PY'
import pathlib, sys
root, ticket = pathlib.Path(sys.argv[1]), sys.argv[2]
count = 0
for path in root.glob("*.meta"):
    fields = dict(line.split("=", 1) for line in path.read_text().splitlines() if "=" in line)
    count += fields.get("ticket") == ticket and fields.get("phase") == "completed"
print(count)
PY
)"
if [[ "$STUBBORN_RC" -eq 0 && "$STUBBORN_ELAPSED" -lt 20 &&
      "$STUBBORN_TERMINALS" -eq 1 &&
      ! -e "$PRODUCT/factory/runs/"*.wrapper &&
      ! -e "$PRODUCT/factory/.active-runs/$SECOND_TICKET.planner.lock" ]]; then
  pass "nonresponsive heartbeat is killed within a bounded wait and terminalizes once"
else
  fail "nonresponsive heartbeat is killed within a bounded wait and terminalizes once" \
    "status=$STUBBORN_RC elapsed=$STUBBORN_ELAPSED terminals=$STUBBORN_TERMINALS"
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

HEARTBEAT_READY="$TMP/heartbeat-renew-started"
HEARTBEAT_RENEW="$TMP/heartbeat-renew.sh"
printf '%s\n' '#!/usr/bin/env bash' \
  'touch "$HEARTBEAT_READY"' \
  'sleep 30' > "$HEARTBEAT_RENEW"
chmod 700 "$HEARTBEAT_RENEW"
HEARTBEAT_READY="$HEARTBEAT_READY" python3 "$ROOT/scripts/dispatch-lease-heartbeat.py" \
  --renew-script "$HEARTBEAT_RENEW" --factory-root "$PRODUCT" \
  --ticket "$FIRST_TICKET" --lease "$FIRST_ID" --interval 1 &
HEARTBEAT_PID=$!
for _try in $(seq 1 500); do
  [[ -f "$HEARTBEAT_READY" ]] && break
  sleep 0.02
done
kill -TERM -- "-$HEARTBEAT_PID" 2>/dev/null || true
HEARTBEAT_RC=0
wait "$HEARTBEAT_PID" || HEARTBEAT_RC=$?
if [[ -f "$HEARTBEAT_READY" && "$HEARTBEAT_RC" -eq 0 ]]; then
  pass "heartbeat stop during renewal exits cleanly"
else
  fail "heartbeat stop during renewal exits cleanly" "status=$HEARTBEAT_RC"
fi

HEARTBEAT_FAILURE_READY="$TMP/heartbeat-failure-started"
HEARTBEAT_FAILURE_RENEW="$TMP/heartbeat-failure.sh"
printf '%s\n' '#!/usr/bin/env bash' \
  'touch "$HEARTBEAT_FAILURE_READY"' \
  'sleep 1' \
  'exit 8' > "$HEARTBEAT_FAILURE_RENEW"
chmod 700 "$HEARTBEAT_FAILURE_RENEW"
HEARTBEAT_FAILURE_READY="$HEARTBEAT_FAILURE_READY" \
  python3 "$ROOT/scripts/dispatch-lease-heartbeat.py" \
  --renew-script "$HEARTBEAT_FAILURE_RENEW" --factory-root "$PRODUCT" \
  --ticket "$FIRST_TICKET" --lease "$FIRST_ID" --interval 1 &
HEARTBEAT_FAILURE_PID=$!
for _try in $(seq 1 500); do
  [[ -f "$HEARTBEAT_FAILURE_READY" ]] && break
  sleep 0.02
done
kill -TERM "$HEARTBEAT_FAILURE_PID" 2>/dev/null || true
HEARTBEAT_FAILURE_RC=0
wait "$HEARTBEAT_FAILURE_PID" || HEARTBEAT_FAILURE_RC=$?
if [[ -f "$HEARTBEAT_FAILURE_READY" && "$HEARTBEAT_FAILURE_RC" -eq 8 ]]; then
  pass "heartbeat stop preserves an ordinary renewal failure"
else
  fail "heartbeat stop preserves an ordinary renewal failure" \
    "status=$HEARTBEAT_FAILURE_RC"
fi

HEARTBEAT_IGNORE_READY="$TMP/heartbeat-ignore-started"
HEARTBEAT_IGNORE_LOCK="$TMP/heartbeat-ignore.lock"
HEARTBEAT_IGNORE_RENEW="$TMP/heartbeat-ignore.sh"
printf '%s\n' '#!/usr/bin/env bash' \
  'trap '\''rmdir "$HEARTBEAT_IGNORE_LOCK"; exit 143'\'' TERM' \
  'mkdir "$HEARTBEAT_IGNORE_LOCK"' \
  'touch "$HEARTBEAT_IGNORE_READY"' \
  'while :; do sleep 1; done' > "$HEARTBEAT_IGNORE_RENEW"
chmod 700 "$HEARTBEAT_IGNORE_RENEW"
HEARTBEAT_IGNORE_READY="$HEARTBEAT_IGNORE_READY" \
  HEARTBEAT_IGNORE_LOCK="$HEARTBEAT_IGNORE_LOCK" \
  FACTORY_TEST_MODE=1 FACTORY_TRUSTED_TEST_HARNESS=1 \
  FACTORY_TEST_LEASE_HEARTBEAT_IGNORE_TERM=1 \
  python3 "$ROOT/scripts/dispatch-lease-heartbeat.py" \
    --renew-script "$HEARTBEAT_IGNORE_RENEW" --factory-root "$PRODUCT" \
    --ticket "$FIRST_TICKET" --lease "$FIRST_ID" --interval 1 &
HEARTBEAT_IGNORE_PID=$!
for _try in $(seq 1 500); do
  [[ -f "$HEARTBEAT_IGNORE_READY" ]] && break
  sleep 0.02
done
kill -TERM -- "-$HEARTBEAT_IGNORE_PID" 2>/dev/null || true
for _try in $(seq 1 100); do
  kill -0 "$HEARTBEAT_IGNORE_PID" 2>/dev/null || break
  sleep 0.02
done
if kill -0 "$HEARTBEAT_IGNORE_PID" 2>/dev/null; then
  kill -KILL -- "-$HEARTBEAT_IGNORE_PID" 2>/dev/null || true
fi
HEARTBEAT_IGNORE_RC=0
wait "$HEARTBEAT_IGNORE_PID" || HEARTBEAT_IGNORE_RC=$?
if [[ -f "$HEARTBEAT_IGNORE_READY" && "$HEARTBEAT_IGNORE_RC" -eq 137 &&
      ! -e "$HEARTBEAT_IGNORE_LOCK" ]]; then
  pass "ignored heartbeat stop still lets an in-flight renewal clean up"
else
  fail "ignored heartbeat stop still lets an in-flight renewal clean up" \
    "status=$HEARTBEAT_IGNORE_RC"
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
if [[ "$BLOCKED_BUSY_RENEW_RC" -eq 0 ]]; then
  pass "maintenance permits exact renewal while the launch lock is busy"
else
  fail "maintenance permits exact renewal while the launch lock is busy" "status=$BLOCKED_BUSY_RENEW_RC"
fi

FACTORY_ROOT="$PRODUCT" "$LEASE" release --ticket "$FIRST_TICKET" --lease "$FIRST_ID" >/dev/null
ABSENT_RELEASE="$(
  FACTORY_ROOT="$PRODUCT" "$LEASE" release \
    --ticket "$FIRST_TICKET" --lease "$FIRST_ID"
)"
if [[ "$(
  printf '%s\n' "$ABSENT_RELEASE" |
    python3 -c 'import json,sys; print(json.load(sys.stdin).get("absent"))'
)" == "True" ]]; then
  pass "exact release is idempotent after the lease is already absent"
else
  fail "exact release is idempotent after the lease is already absent"
fi
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

cp "$PRODUCT/factory/PROJECT.env" "$TMP/project-before-expired.env"
printf '%s\n' 'MAX_CONCURRENT_TICKETS=4' > "$PRODUCT/factory/PROJECT.env"
LIVE_EXPIRED_RC=0
FACTORY_RELEASE_CONTRACT_VERSION=2.0.0 FACTORY_ROOT="$PRODUCT" \
  "$LEASE" release-expired --ticket "$THIRD_TICKET" --lease "$THIRD_ID" \
  > "$TMP/live-expired.out" 2>&1 || LIVE_EXPIRED_RC=$?
WRONG_EXPIRED_RC=0
FACTORY_RELEASE_CONTRACT_VERSION=2.0.0 FACTORY_ROOT="$PRODUCT" \
  "$LEASE" release-expired --ticket "$SECOND_TICKET" \
  --lease 0000000000000000000000000000000000000000000000000000000000000000 \
  > "$TMP/wrong-expired.out" 2>&1 || WRONG_EXPIRED_RC=$?
EXPIRED_RELEASE="$(
  FACTORY_RELEASE_CONTRACT_VERSION=2.0.0 FACTORY_ROOT="$PRODUCT" \
    "$LEASE" release-expired --ticket "$SECOND_TICKET" --lease "$SECOND_ID"
)"
mv "$TMP/project-before-expired.env" "$PRODUCT/factory/PROJECT.env"
if [[ "$LIVE_EXPIRED_RC" -ne 0 && "$WRONG_EXPIRED_RC" -ne 0 &&
      -f "$PRODUCT/factory/.dispatch-leases/$THIRD_TICKET.json" &&
      ! -e "$PRODUCT/factory/.dispatch-leases/$SECOND_TICKET.json" ]] &&
   [[ "$(printf '%s\n' "$EXPIRED_RELEASE" | python3 -c \
     'import json,sys; value=json.load(sys.stdin); print(value.get("expired"), value.get("ticket"))')" == \
      "True $SECOND_TICKET" ]]; then
  pass "exact expired lease recovery refuses live and mismatched owners"
else
  fail "exact expired lease recovery refuses live and mismatched owners" \
    "live=$LIVE_EXPIRED_RC wrong=$WRONG_EXPIRED_RC"
fi

touch "$PRODUCT/factory/MAINTENANCE"
RENEW_RC=0
FACTORY_ROOT="$PRODUCT" "$LEASE" renew --ticket "$THIRD_TICKET" --lease "$THIRD_ID" >/dev/null 2>&1 || RENEW_RC=$?
FACTORY_ROOT="$PRODUCT" "$LEASE" release --ticket "$SECOND_TICKET" --lease "$SECOND_ID" >/dev/null
FACTORY_ROOT="$PRODUCT" "$LEASE" release --ticket "$THIRD_TICKET" --lease "$THIRD_ID" >/dev/null
FACTORY_ROOT="$PRODUCT" "$LEASE" release --ticket "$FOURTH_TICKET" --lease "$FOURTH_ID" >/dev/null
FACTORY_ROOT="$PRODUCT" "$LEASE" release --ticket "$FIFTH_TICKET" --lease "$FIFTH_ID" >/dev/null
FACTORY_ROOT="$PRODUCT" "$LEASE" release --ticket "$SIXTH_TICKET" --lease "$SIXTH_ID" >/dev/null
FACTORY_ROOT="$PRODUCT" "$LEASE" release --ticket T-908 --lease "$RECLAIMED_ID" >/dev/null
if [[ "$RENEW_RC" -eq 0 && -z "$(find "$PRODUCT/factory/.dispatch-leases" -type f -print -quit)" ]]; then
  pass "maintenance permits exact renewal and lease drain"
else
  fail "maintenance permits exact renewal and lease drain"
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
FACTORY_CONTRACT_VERSION=1.5.0
export FACTORY_CONTRACT_VERSION
printf '%s\n' 'MAX_CONCURRENT_TICKETS=5' > "$PRODUCT/factory/PROJECT.env"
LEGACY_INVALID_RC=0
FACTORY_ROOT="$PRODUCT" "$LEASE" claim --ticket T-903 >/dev/null 2>&1 || LEGACY_INVALID_RC=$?
[[ "$LEGACY_INVALID_RC" -eq 3 ]] &&
  pass "Contract 1.5 retains the 1 through 4 capacity bound" ||
  fail "Contract 1.5 retains the 1 through 4 capacity bound" "status=$LEGACY_INVALID_RC"
FACTORY_CONTRACT_VERSION=1.6.0
export FACTORY_CONTRACT_VERSION
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

CHAIN_ROOT="$TMP/kill-wrapper-chain"
mkdir -p "$CHAIN_ROOT/factory/runs"
python3 -c 'import os,signal,time; os.setsid(); signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)' &
CHAIN_HEARTBEAT_PID=$!
for _try in $(seq 1 100); do
  CHAIN_HEARTBEAT_PGID="$(ps -o pgid= -p "$CHAIN_HEARTBEAT_PID" 2>/dev/null | tr -d ' ')"
  [[ "$CHAIN_HEARTBEAT_PGID" == "$CHAIN_HEARTBEAT_PID" ]] && break
  sleep 0.01
done
python3 -c 'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)' &
CHAIN_WRAPPER_PID=$!
python3 -c 'import time; time.sleep(30)' &
CHAIN_SIBLING_PID=$!
CHAIN_HEARTBEAT_START="$(ps -o lstart= -p "$CHAIN_HEARTBEAT_PID" | awk '{$1=$1; print; exit}')"
CHAIN_WRAPPER_START="$(ps -o lstart= -p "$CHAIN_WRAPPER_PID" | awk '{$1=$1; print; exit}')"
printf 'run_id=orphan-chain\nticket=T-901\npid=99999999\npgid=99999999\nprocess_start=absent\n' \
  > "$CHAIN_ROOT/factory/runs/orphan-chain.meta"
printf 'pid=99999999\npgid=99999999\nrun_id=orphan-chain\nprocess_start=absent\n' \
  > "$CHAIN_ROOT/factory/runs/orphan-chain.pid"
printf 'run_id=orphan-chain\nwrapper_pid=%s\nwrapper_process_start=%s\nheartbeat_pid=%s\nheartbeat_pgid=%s\nheartbeat_process_start=%s\n' \
  "$CHAIN_WRAPPER_PID" "$CHAIN_WRAPPER_START" "$CHAIN_HEARTBEAT_PID" \
  "$CHAIN_HEARTBEAT_PGID" "$CHAIN_HEARTBEAT_START" \
  > "$CHAIN_ROOT/factory/runs/orphan-chain.wrapper"
FACTORY_SKIP_SCHEDULE_STOP=1 "$KILL" "$CHAIN_ROOT" > "$TMP/kill-wrapper-chain.out" 2>&1
wait "$CHAIN_HEARTBEAT_PID" 2>/dev/null || true
wait "$CHAIN_WRAPPER_PID" 2>/dev/null || true
if [[ ! -e "$CHAIN_ROOT/factory/runs/orphan-chain.wrapper" &&
      ! -e "$CHAIN_ROOT/factory/runs/orphan-chain.pid" ]] &&
   kill -0 "$CHAIN_SIBLING_PID" 2>/dev/null; then
  pass "kill switch stops only the exact wrapper and heartbeat after provider identity is gone"
else
  fail "kill switch stops only the exact wrapper and heartbeat after provider identity is gone"
fi
FACTORY_SKIP_SCHEDULE_STOP=1 "$KILL" "$CHAIN_ROOT" >/dev/null
kill -TERM "$CHAIN_SIBLING_PID" 2>/dev/null || true
wait "$CHAIN_SIBLING_PID" 2>/dev/null || true
[[ ! -e "$CHAIN_ROOT/factory/runs/orphan-chain.wrapper" ]] &&
  pass "kill-switch replay keeps the drained wrapper chain absent" ||
  fail "kill-switch replay keeps the drained wrapper chain absent"

STALE_WRAPPER_ROOT="$TMP/stale-wrapper"
mkdir -p "$STALE_WRAPPER_ROOT/factory/runs"
python3 -c 'import time; time.sleep(30)' &
STALE_WRAPPER_PID=$!
printf 'run_id=stale-wrapper\nwrapper_pid=%s\nwrapper_process_start=not-the-real-start\nheartbeat_pid=99999999\nheartbeat_pgid=99999999\nheartbeat_process_start=absent\n' \
  "$STALE_WRAPPER_PID" > "$STALE_WRAPPER_ROOT/factory/runs/stale-wrapper.wrapper"
FACTORY_SKIP_SCHEDULE_STOP=1 "$KILL" "$STALE_WRAPPER_ROOT" \
  > "$TMP/stale-wrapper.out" 2>&1
if kill -0 "$STALE_WRAPPER_PID" 2>/dev/null &&
   [[ -f "$STALE_WRAPPER_ROOT/factory/runs/stale-wrapper.wrapper" ]] &&
   grep -q "refusing stale or mismatched run wrapper identity" "$TMP/stale-wrapper.out"; then
  pass "kill switch refuses a stale wrapper identity"
else
  fail "kill switch refuses a stale wrapper identity"
fi
kill -TERM "$STALE_WRAPPER_PID" 2>/dev/null || true
wait "$STALE_WRAPPER_PID" 2>/dev/null || true

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
