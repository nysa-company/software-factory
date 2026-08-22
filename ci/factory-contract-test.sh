#!/usr/bin/env bash
# Focused Contract 2 launcher and Doctor boundary test.
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LAUNCHER="$ROOT/scripts/factory-launch"
DOCTOR="$ROOT/scripts/factory-doctor.sh"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/factory-contract-test.XXXXXX")"
TMP="$(cd "$TMP" && pwd -P)"
TEST_HOME="$TMP/home"
KITS_ROOT="$TEST_HOME/.factory/kits"
PRODUCT="$TEST_HOME/product"
PRODUCT_ORIGIN="$TEST_HOME/product-origin.git"
TEST_BIN="$TEST_HOME/.factory/bin"
LAUNCH_TMP="$TMP/launcher-tmp"
PROJECT=contracttest
RACE_PID=""
QUALIFICATION_ROOT=""

cleanup() {
  if [[ -n "$RACE_PID" ]]; then
    kill -TERM "$RACE_PID" 2>/dev/null || true
    wait "$RACE_PID" 2>/dev/null || true
  fi
  if [[ -n "$QUALIFICATION_ROOT" ]]; then
    chmod -R u+w "$QUALIFICATION_ROOT" 2>/dev/null || true
    rm -rf "$QUALIFICATION_ROOT"
  fi
  chmod -R u+w "$TMP" 2>/dev/null || true
  rm -rf "$TMP"
}
trap cleanup EXIT
trap 'status=$?; echo "FAIL: unexpected command at line ${BASH_LINENO[0]:-$LINENO} (exit $status)" >&2; exit "$status"' ERR

fail() {
  echo "FAIL: $1" >&2
  exit 1
}

grep -Fqx -- '- Keep failing-test commits test-only. Commit required ticket-log comments separately as bookkeeping-only commits; never mix `factory/tickets/` paths with test paths in one commit.' \
  "$ROOT/roles/test-author.md" || fail "Test-author may mix ticket bookkeeping with tests"
grep -Fqx -- '- Run locally only the narrowest existing command that executes the added or changed tests. Never run a root repository-wide, workspace-wide, or full test suite, build, `repo-check`, `secret-scan`, or other broad verification from this role. If no scoped command exists, record that broad verification is deferred to protected CI and final certification.' \
  "$ROOT/roles/test-author.md" || fail "Test-author may run broad verification"
grep -Fqx -- '- Keep implementation commits implementation-only. Commit required ticket-log notes separately as bookkeeping-only commits; never mix `factory/tickets/` paths with implementation paths in one commit.' \
  "$ROOT/roles/builder.md" || fail "Builder may mix ticket bookkeeping with implementation"
grep -Fqx -- '- Run locally only the narrowest existing commands that cover the changed behavior. Never run a root repository-wide, workspace-wide, or full test suite from this role. If no scoped test, lint, or typecheck command exists, record that broad verification is deferred to protected CI and final certification.' \
  "$ROOT/roles/builder.md" || fail "Builder may run broad verification"
grep -Fqx -- '- Do not rerun tests, builds, repository checks, or broad verification. Inspect' \
  "$ROOT/roles/reviewer.md" || fail "Reviewer may rerun verification"
grep -Fqx -- 'READINESS_TIMEOUT_SECONDS="${FACTORY_DOCTOR_READINESS_TIMEOUT_SECONDS:-120}"' \
  "$DOCTOR" || fail "Doctor readiness timeout differs from qualification preparation"
PYTHONWARNINGS=error python3 "$ROOT/scripts/secret-scan" --help >/dev/null ||
  fail "secret-scan is not warning-free under the supported Python runtime"

assert_no_secret() {
  ! grep -Fq 'caller-secret-must-not-pass' "$1" ||
    fail "caller secret reached launcher output"
}

expect_refused() {
  local label="$1" output="$TMP/refused-$1.out"
  shift
  if "$@" > "$output" 2>&1; then
    fail "$label was accepted"
  fi
  assert_no_secret "$output"
}

tree_for_directory() {
  local directory="$1" object_root index
  object_root="$(mktemp -d "$TMP/tree.XXXXXX")"
  index="$object_root/index"
  git init --bare -q "$object_root/repo.git"
  git --git-dir="$object_root/repo.git" config core.bare false
  GIT_INDEX_FILE="$index" git --git-dir="$object_root/repo.git" \
    --work-tree="$directory" read-tree --empty
  GIT_INDEX_FILE="$index" git --git-dir="$object_root/repo.git" \
    --work-tree="$directory" add -f -A -- .
  GIT_INDEX_FILE="$index" git --git-dir="$object_root/repo.git" \
    --work-tree="$directory" write-tree
  rm -rf "$object_root"
}

create_release() {
  local release="$1" marker="$2"
  mkdir -p "$release/scripts/lib"
  python3 - "$release/factory-contract.json" "$marker" <<'PY'
import json
import pathlib
import sys

path, marker = pathlib.Path(sys.argv[1]), sys.argv[2]
value = {
    "contract": "nysa.software-factory",
    "contract_version": "2.0.0",
    "doctor_schema": "nysa.software-factory.doctor/v2",
    "fixture_release": marker,
    "launcher": {
        "source": "scripts/factory-launch",
        "commands": {"run": {"role_whitelist": ["planner"]}},
    },
}
path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
  cp "$DOCTOR" "$release/scripts/factory-doctor-real.sh"
  cp "$ROOT/scripts/ticket-readiness.py" "$release/scripts/ticket-readiness.py"
  cp "$ROOT/scripts/lib/ticket_state_transition.py" \
    "$release/scripts/lib/ticket_state_transition.py"
  cp "$ROOT/scripts/lib/qualification_manifest.py" \
    "$release/scripts/lib/qualification_manifest.py"
  cp "$ROOT/scripts/lib/qualification_artifacts.py" \
    "$release/scripts/lib/qualification_artifacts.py"
  for library in approval_evidence inflight_release legacy_closeout \
      protected_merge_reconciliation terminal_backfill; do
    cp "$ROOT/scripts/lib/$library.py" "$release/scripts/lib/$library.py"
  done
  cp "$ROOT/scripts/lib/dispatch-leases.sh" \
    "$release/scripts/lib/dispatch-leases.sh"
  cat > "$release/scripts/factory-doctor.sh" <<'EOF'
#!/usr/bin/env bash
env | LC_ALL=C sort > "$FACTORY_ROOT/factory/doctor-helper.env"
exec /bin/bash "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/factory-doctor-real.sh" "$@"
EOF
  cat > "$release/scripts/model-control.sh" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' '{"portfolio_id":"fixture","profile_hash":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","profile_id":"fixture","schema":"model-resolution-plan/v1","selections":{"builder":{},"narrator":{},"planner":{},"reviewer":{},"spec-linter":{},"test-author":{}}}'
EOF
  cat > "$release/scripts/provider-concurrency-config.py" <<'EOF'
#!/usr/bin/env python3
raise SystemExit(0)
EOF
  chmod 700 "$release/scripts/factory-doctor.sh" \
    "$release/scripts/factory-doctor-real.sh" \
    "$release/scripts/model-control.sh" \
    "$release/scripts/provider-concurrency-config.py"
}

write_binding() {
  local sha="$1" tree="$2" release="$3" contract="${4:-2.0.0}"
  local active="$KITS_ROOT/projects/$PROJECT/active.json"
  RECEIPT_ID="$(python3 - "$sha" <<'PY'
import hashlib
import sys
print(hashlib.sha256(sys.argv[1].encode()).hexdigest())
PY
)"
  python3 - "$active" "$KITS_ROOT/receipts/$RECEIPT_ID.json" \
    "$RECEIPT_ID" "$PROJECT" "$sha" "$tree" "$PRODUCT" "$PRODUCT_TREE" \
    "$release" "$contract" "$PRODUCT_ORIGIN" <<'PY'
import json
import os
import pathlib
import sys

(
    active_path, receipt_path, receipt_id, project, kit_sha, kit_tree,
    product_path, product_tree, release_path, contract, product_origin,
) = sys.argv[1:]
receipt = {
    "receipt_id": receipt_id,
    "status": "pass",
    "project": project,
    "kit_sha": kit_sha,
    "kit_tree": kit_tree,
    "product_path": os.path.realpath(product_path),
    "product_tree": product_tree,
    "product_origin": os.path.realpath(product_origin),
    "contract_version": contract,
}
path = pathlib.Path(receipt_path)
path.write_text(json.dumps(receipt, sort_keys=True) + "\n", encoding="utf-8")
os.chmod(path, 0o600)
active = {
    "generation": 1,
    "project": project,
    "kit_sha": kit_sha,
    "kit_tree": kit_tree,
    "contract_version": contract,
    "product_path": os.path.realpath(product_path),
    "release_path": os.path.realpath(release_path),
    "receipt_id": receipt_id,
    "product_tree": product_tree,
}
path = pathlib.Path(active_path)
temporary = path.with_suffix(".tmp")
temporary.write_text(json.dumps(active, sort_keys=True) + "\n", encoding="utf-8")
os.chmod(temporary, 0o600)
os.replace(temporary, path)
PY
}

run_launcher() {
  mkdir -p "$LAUNCH_TMP"
  HOME="$TEST_HOME" TMPDIR="$LAUNCH_TMP" \
    FACTORY_LAUNCH_TEST_MODE=1 FACTORY_LAUNCH_TEST_HOME="$TEST_HOME" \
    FACTORY_KITS_ROOT="$KITS_ROOT" \
    CALLER_SENTINEL=caller-secret-must-not-pass \
    GH_TOKEN=caller-secret-must-not-pass \
    PYTHONPATH="$TMP/python-path-must-not-pass" \
    GIT_CONFIG_GLOBAL="$TMP/git-config-must-not-pass" \
    /bin/bash "$TEST_BIN/factory-launch" "$PROJECT" "$@"
}

mkdir -p "$TEST_BIN" "$LAUNCH_TMP" "$KITS_ROOT/projects/$PROJECT" \
  "$KITS_ROOT/releases" "$KITS_ROOT/receipts" "$PRODUCT/factory"
chmod 700 "$TEST_HOME"
cp "$LAUNCHER" "$TEST_BIN/factory-launch"
chmod 700 "$TEST_BIN/factory-launch"
git init --bare -q "$PRODUCT_ORIGIN"

ACCOUNT_REAL_HOME="$(python3 - <<'PY'
import os, pwd
print(os.path.realpath(pwd.getpwuid(os.getuid()).pw_dir))
PY
)"
expect_refused repository-real-home env \
  FACTORY_LAUNCH_TEST_MODE=1 \
  FACTORY_LAUNCH_TEST_HOME="$ACCOUNT_REAL_HOME" \
  FACTORY_KITS_ROOT="$ACCOUNT_REAL_HOME/.factory/kits" \
  /bin/bash "$LAUNCHER" "$PROJECT" contract --json

# Qualification roots are a macOS-only production boundary fixed under
# /private/tmp. Linux still exercises the repository launcher below.
if [[ "$(uname -s)" == Darwin ]]; then
  QUALIFICATION_ROOT="$(mktemp -d /private/tmp/nysa-sf-qualification.launcher.XXXXXX)"
  QUALIFICATION_SHA=9999999999999999999999999999999999999999
  QUALIFICATION_LAUNCHER="$QUALIFICATION_ROOT/releases/$QUALIFICATION_SHA/scripts/factory-launch"
  mkdir -p "$(dirname "$QUALIFICATION_LAUNCHER")" \
    "$QUALIFICATION_ROOT/projects/qualification-test"
  cp "$LAUNCHER" "$QUALIFICATION_LAUNCHER"
  chmod 700 "$QUALIFICATION_LAUNCHER"
  if HOME="$TEST_HOME" /bin/bash "$QUALIFICATION_LAUNCHER" \
    qualification-test contract --json >"$TMP/qualification-launcher.out" 2>&1; then
    fail "qualification launcher accepted an incomplete active binding"
  fi
  grep -q 'project active record is missing' "$TMP/qualification-launcher.out" ||
    fail "qualification launcher did not recognize its sealed release path"
fi

for tool in git python3 ps; do
  resolved="$(command -v "$tool")"
  [[ "$resolved" == /* && -x "$resolved" ]] || fail "$tool is unavailable"
  ln -s "$resolved" "$TEST_BIN/$tool"
done
for cli in factory claude codex agent gh; do
  cat > "$TEST_BIN/$cli" <<'EOF'
#!/usr/bin/env bash
printf '%s fixture\n' "$(basename "$0")"
EOF
  chmod 700 "$TEST_BIN/$cli"
done

git -C "$PRODUCT" init -q -b main
git -C "$PRODUCT" remote add origin "$PRODUCT_ORIGIN"
git -C "$PRODUCT" config user.name "Factory contract test"
git -C "$PRODUCT" config user.email "factory-contract@example.invalid"
printf 'fixture\n' > "$PRODUCT/README.md"
printf 'MAX_CONCURRENT_TICKETS=3\n' > "$PRODUCT/factory/PROJECT.env"
printf '%040d\n' 0 > "$PRODUCT/factory/KIT_PIN"
mkdir -p "$PRODUCT/factory/tickets"
printf '%s\n' '# T-1' '' 'State: Ready' > "$PRODUCT/factory/tickets/T-1.md"
git -C "$PRODUCT" add -A
git -C "$PRODUCT" commit -qm "seed product"
PRODUCT_TREE="$(git -C "$PRODUCT" rev-parse 'HEAD^{tree}')"

SHA_A=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
SHA_B=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
RELEASE_A="$KITS_ROOT/releases/$SHA_A"
RELEASE_B="$KITS_ROOT/releases/$SHA_B"
create_release "$RELEASE_A" A
create_release "$RELEASE_B" B
TREE_A="$(tree_for_directory "$RELEASE_A")"
TREE_B="$(tree_for_directory "$RELEASE_B")"

python3 - "$KITS_ROOT/contract-floor.json" <<'PY'
import json
import os
import pathlib
import sys
path = pathlib.Path(sys.argv[1])
path.write_text(json.dumps({
    "minimum_major": 2,
    "schema": "nysa.software-factory.contract-floor/v1",
}, sort_keys=True) + "\n", encoding="utf-8")
os.chmod(path, 0o600)
PY

# The only accepted activation is the fixed owner-only active record.
write_binding "$SHA_A" "$TREE_A" "$RELEASE_A"
RECEIPT_A="$RECEIPT_ID"
printf '%s\n' "$SHA_A" > "$PRODUCT/factory/KIT_PIN"
run_launcher contract --json > "$TMP/contract-a.json"
expect_refused repository-test-qualification-run run_launcher qualification-run --json
grep -Fxq 'factory-launch: qualification run requires a sealed qualification launcher' \
  "$TMP/refused-repository-test-qualification-run.out" ||
  fail "repository-test qualification-run did not reach the sealed-lane guard"
expect_refused repository-test-qualification-finish run_launcher qualification-finish --json
grep -Fxq 'factory-launch: qualification finish requires a sealed isolated qualification launcher' \
  "$TMP/refused-repository-test-qualification-finish.out" ||
  fail "repository-test qualification-finish did not reach the sealed-lane guard"
expect_refused repository-test-qualification-resume run_launcher qualification-resume \
  --ticket T-1 --blocked-receipt "$(printf 'a%.0s' {1..64})" --json
grep -Fxq 'factory-launch: qualification resume requires a sealed isolated qualification launcher' \
  "$TMP/refused-repository-test-qualification-resume.out" ||
  fail "repository-test qualification-resume did not reach the sealed-lane guard"
expect_refused repository-test-qualification-history-repair run_launcher \
  qualification-history-repair --ticket T-1 \
  --blocked-receipt "$(printf 'a%.0s' {1..64})" --json
grep -Fxq 'factory-launch: qualification history repair requires a sealed isolated qualification launcher' \
  "$TMP/refused-repository-test-qualification-history-repair.out" ||
  fail "repository-test qualification-history-repair did not reach the sealed-lane guard"
for command in incident-report publication-repair ci-rerun ticket-pr ticket-attest; do
  expect_refused "repository-test-$command" run_launcher "$command"
  grep -Fxq 'factory-launch: repository test mode refuses GitHub-mutating commands' \
    "$TMP/refused-repository-test-$command.out" ||
    fail "repository-test-$command did not reach the capability guard"
done
python3 - "$TMP/contract-a.json" <<'PY'
import json
import sys
value = json.load(open(sys.argv[1], encoding="utf-8"))
assert value["contract_version"] == "2.0.0"
assert value["fixture_release"] == "A"
assert value["launcher"]["source"] == "scripts/factory-launch"
PY

ACTIVE="$KITS_ROOT/projects/$PROJECT/active.json"
chmod 644 "$ACTIVE"
expect_refused active-record-mode run_launcher contract --json
chmod 600 "$ACTIVE"

# The active record must match its exact certification receipt.
python3 - "$KITS_ROOT/receipts/$RECEIPT_A.json" <<'PY'
import json
import sys
path = sys.argv[1]
value = json.load(open(path, encoding="utf-8"))
value["kit_tree"] = "0" * 40
with open(path, "w", encoding="utf-8") as handle:
    json.dump(value, handle, sort_keys=True)
    handle.write("\n")
PY
expect_refused receipt-mismatch run_launcher contract --json
write_binding "$SHA_A" "$TREE_A" "$RELEASE_A"

# The owner floor rejects pre-Contract-2 activations before helper selection.
write_binding "$SHA_A" "$TREE_A" "$RELEASE_A" 1.8.0
expect_refused contract-floor run_launcher contract --json
write_binding "$SHA_A" "$TREE_A" "$RELEASE_A"

# A completed host cutover makes a deleted floor fail closed.
python3 - "$KITS_ROOT/contract-cutover-journal.json" <<'PY'
import hashlib
import json
import os
import pathlib
import sys
body = {
    "approval_sha256": "a" * 64,
    "completed_projects": ["contracttest"],
    "floor_required": True,
    "phase": "healthy",
    "schema": "nysa.software-factory.host-cutover-journal/v1",
    "status": "pass",
}
body["record_sha256"] = hashlib.sha256(
    (json.dumps(body, sort_keys=True, separators=(",", ":")) + "\n").encode()
).hexdigest()
path = pathlib.Path(sys.argv[1])
path.write_text(json.dumps(body, sort_keys=True) + "\n", encoding="utf-8")
os.chmod(path, 0o600)
PY
rm "$KITS_ROOT/contract-floor.json"
expect_refused missing-floor-after-cutover run_launcher contract --json
python3 - "$KITS_ROOT/contract-floor.json" <<'PY'
import json, os, pathlib, sys
path = pathlib.Path(sys.argv[1])
path.write_text(json.dumps({
    "minimum_major": 2,
    "schema": "nysa.software-factory.contract-floor/v1",
}, sort_keys=True) + "\n", encoding="utf-8")
os.chmod(path, 0o600)
PY
python3 - "$KITS_ROOT/contract-cutover-journal.json" <<'PY'
import hashlib, json, pathlib, sys
path = pathlib.Path(sys.argv[1])
value = json.loads(path.read_text())
value.update(phase="launcher_installed", status="in-progress")
value.pop("record_sha256")
value["record_sha256"] = hashlib.sha256(
    (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
).hexdigest()
path.write_text(json.dumps(value, sort_keys=True) + "\n")
PY
expect_refused host-cutover-barrier run_launcher preflight
grep -q 'host cutover is in progress' "$TMP/refused-host-cutover-barrier.out" ||
  fail "launcher did not report the host cutover barrier"
python3 - "$KITS_ROOT/contract-cutover-journal.json" <<'PY'
import hashlib, json, pathlib, sys
path = pathlib.Path(sys.argv[1])
value = json.loads(path.read_text())
value.update(phase="healthy", status="pass")
value.pop("record_sha256")
value["record_sha256"] = hashlib.sha256(
    (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
).hexdigest()
path.write_text(json.dumps(value, sort_keys=True) + "\n")
PY

# A sealed release is selected once and its complete tree must stay unchanged.
touch "$RELEASE_A/unsealed-file"
expect_refused release-tree run_launcher contract --json
rm "$RELEASE_A/unsealed-file"

PARSED_MARKER="$LAUNCH_TMP/active-parsed"
PARSED_GATE="$LAUNCH_TMP/active-gate"
HOME="$TEST_HOME" TMPDIR="$LAUNCH_TMP" \
  FACTORY_LAUNCH_TEST_MODE=1 FACTORY_LAUNCH_TEST_HOME="$TEST_HOME" \
  FACTORY_KITS_ROOT="$KITS_ROOT" \
  FACTORY_LAUNCH_TEST_ACTIVE_PARSED_MARKER="$PARSED_MARKER" \
  FACTORY_LAUNCH_TEST_ACTIVE_PARSED_GATE="$PARSED_GATE" \
  /bin/bash "$LAUNCHER" "$PROJECT" contract --json \
  > "$TMP/contract-race.json" 2> "$TMP/contract-race.err" &
RACE_PID=$!
for _try in $(seq 1 500); do
  [[ -e "$PARSED_MARKER" ]] && break
  sleep 0.01
done
[[ -e "$PARSED_MARKER" ]] || fail "launcher did not parse the active record"
write_binding "$SHA_B" "$TREE_B" "$RELEASE_B"
printf '%s\n' "$SHA_B" > "$PRODUCT/factory/KIT_PIN"
touch "$PARSED_GATE"
if ! wait "$RACE_PID"; then
  RACE_PID=""
  awk '{print}' "$TMP/contract-race.err" >&2
  fail "fixed active-record launch failed"
fi
RACE_PID=""
python3 - "$TMP/contract-race.json" <<'PY'
import json
import sys
assert json.load(open(sys.argv[1], encoding="utf-8"))["fixture_release"] == "A"
PY

# Doctor is the selected Contract 2 helper and receives only the clean boundary.
DOCTOR_RC=0
run_launcher doctor --json > "$TMP/doctor.json" || DOCTOR_RC=$?
assert_no_secret "$TMP/doctor.json"
if [[ "$DOCTOR_RC" -ne 0 ]]; then
  python3 - "$TMP/doctor.json" <<'PY' >&2
import json
import sys
value = json.load(open(sys.argv[1], encoding="utf-8"))
print("Doctor error checks:", ", ".join(
    name for name, check in value["checks"].items()
    if check["status"] == "error"
))
PY
  fail "Doctor rejected the valid Contract 2 binding"
fi
python3 - "$TMP/doctor.json" "$SHA_B" "$RELEASE_B" "$PRODUCT" <<'PY'
import json
import os
import sys

path, sha, release, product = sys.argv[1:]
value = json.load(open(path, encoding="utf-8"))
assert set(value) == {
    "schema", "schema_version", "contract_version", "overall_status",
    "project", "checks",
}
assert value["schema"] == "nysa.software-factory.doctor/v2"
assert value["schema_version"] == 2
assert value["contract_version"] == "2.0.0"
assert value["project"] == "contracttest"
assert value["overall_status"] == "ok"
checks = value["checks"]
assert set(checks) == {
    "active_binding", "authenticated_artifacts", "kit", "kit_pin", "runtime", "clis",
    "provider_cli_pins", "fallback_readiness", "model_readiness",
    "credentials", "contract_resume", "transition_receipts", "controller",
    "isolated_provider", "qualification_identity", "qualification_ticket_readiness",
}
assert checks["active_binding"] == {
    "status": "ok",
    "reason_code": None,
    "kit_dir": os.path.realpath(release),
    "product_root": os.path.realpath(product),
}
assert checks["kit"] == {"status": "ok", "full_sha": sha}
assert checks["kit_pin"]["status"] == "ok"
assert checks["kit_pin"]["matches_kit"] is True
assert checks["model_readiness"] == {
    "status": "ok",
    "report": {
        "adapter": "mock",
        "schema": "nysa.software-factory.doctor-repository-test-readiness/v1",
        "status": "ready",
        "trust_scope": "repository-test",
    },
}
assert checks["credentials"]["status"] == "ok"
assert checks["isolated_provider"]["concurrency_required"] is False
assert checks["isolated_provider"]["concurrency_ready"] is False
assert checks["qualification_ticket_readiness"] == {
    "reason_code": None, "status": "not_applicable", "tickets": [],
}
assert checks["qualification_identity"] == {
    "reason_code": None, "status": "not_applicable",
}
assert checks["authenticated_artifacts"] == {
    "reason_code": None, "status": "not_applicable",
}
assert "registry" not in checks
PY

python3 - "$PRODUCT/factory/doctor-helper.env" "$SHA_B" "$TREE_B" \
  "$RELEASE_B" "$PRODUCT" "$TEST_HOME" <<'PY'
import os
import sys

path, sha, tree, release, product, home = sys.argv[1:]
environment = {}
for line in open(path, encoding="utf-8"):
    key, _, value = line.rstrip("\n").partition("=")
    environment[key] = value
assert environment["HOME"] == os.path.realpath(home)
assert environment["FACTORY_ROOT"] == os.path.realpath(product)
assert environment["FACTORY_RELEASE_SHA"] == sha
assert environment["FACTORY_RELEASE_TREE"] == tree
assert environment["FACTORY_RELEASE_PATH"] == os.path.realpath(release)
assert environment["FACTORY_RELEASE_CONTRACT_VERSION"] == "2.0.0"
assert environment["FACTORY_KIT_TRUST_SCOPE"] == "repository-test"
assert environment["FACTORY_TEST_ENFORCE_ROLE_EXIT"] == "1"
assert environment["MOCK_COMMIT_EMPTY"] == "1"
for forbidden in (
    "CALLER_SENTINEL", "GH_TOKEN", "PYTHONPATH", "GIT_CONFIG_GLOBAL",
    "FACTORY_KITS_ROOT", "FACTORY_KIT_TEST_MODE", "FACTORY_RELEASE_TEST_HOME",
    "FACTORY_LAUNCH_TEST_MODE",
    "FACTORY_LAUNCH_TEST_HOME", "FACTORY_ACTIVE_RECORD",
):
    assert forbidden not in environment, forbidden
assert "caller-secret-must-not-pass" not in "\n".join(environment.values())
PY

# Production readiness probes are bounded even when a sealed helper hangs.
cp "$RELEASE_B/scripts/model-control.sh" "$TMP/model-control.saved"
cat > "$RELEASE_B/scripts/model-control.sh" <<'EOF'
#!/usr/bin/env bash
sleep 30
EOF
chmod 700 "$RELEASE_B/scripts/model-control.sh"
HANG_STARTED="$(python3 -c 'import time; print(time.monotonic())')"
HANG_RC=0
HOME="$TEST_HOME" PATH="$TEST_BIN:/usr/bin:/bin" \
  FACTORY_TEST_MODE=1 FACTORY_TRUSTED_TEST_HARNESS=1 \
  FACTORY_DOCTOR_TIMEOUT_SECONDS=1 \
  FACTORY_DOCTOR_READINESS_TIMEOUT_SECONDS=1 \
  FACTORY_KIT_TRUST_SCOPE=production-certified \
  FACTORY_MODEL_STATE_ROOT="$KITS_ROOT/projects" \
  /bin/bash "$RELEASE_B/scripts/factory-doctor-real.sh" --json \
    --project "$PROJECT" --kit-dir "$RELEASE_B" \
    --product-root "$PRODUCT" --kit-sha "$SHA_B" \
    > "$TMP/hanging-doctor.json" || HANG_RC=$?
HANG_ENDED="$(python3 -c 'import time; print(time.monotonic())')"
mv "$TMP/model-control.saved" "$RELEASE_B/scripts/model-control.sh"
python3 - "$TMP/hanging-doctor.json" "$HANG_RC" "$HANG_STARTED" "$HANG_ENDED" <<'PY'
import json, sys
value = json.load(open(sys.argv[1], encoding="utf-8"))
assert int(sys.argv[2]) == 1
assert float(sys.argv[4]) - float(sys.argv[3]) < 5
assert value["checks"]["model_readiness"]["status"] == "error"
PY

# Qualification readiness gets a longer bounded window than cheap CLI probes,
# and a timeout still produces typed Doctor JSON instead of a traceback.
FACTORY_ROOT="$PRODUCT" FACTORY_RELEASE_CONTRACT_VERSION=2.0.0 \
  "$ROOT/scripts/dispatch-lease.sh" claim --ticket T-1 \
  > "$TMP/qualification-lease.json"
mkdir -p "$TMP/provider"
printf '%s\n' '{}' > "$TMP/provider/provider-policy.json"
cp "$RELEASE_B/scripts/model-control.sh" "$TMP/model-control.saved"
cat > "$RELEASE_B/scripts/model-control.sh" <<'EOF'
#!/usr/bin/env bash
sleep 2
printf '%s\n' '{"checks":[],"readiness_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","schema":"nysa.software-factory.qualification-fallback-readiness/v1","status":"ready"}'
EOF
chmod 700 "$RELEASE_B/scripts/model-control.sh"
QUALIFICATION_READY_RC=0
HOME="$TEST_HOME" PATH="$TEST_BIN:/usr/bin:/bin" \
  FACTORY_TEST_MODE=1 FACTORY_TRUSTED_TEST_HARNESS=1 \
  FACTORY_DOCTOR_TIMEOUT_SECONDS=1 \
  FACTORY_DOCTOR_READINESS_TIMEOUT_SECONDS=5 \
  FACTORY_KIT_TRUST_SCOPE=qualification-candidate \
  FACTORY_PROVIDER_POLICY="$TMP/provider/provider-policy.json" \
  /bin/bash "$RELEASE_B/scripts/factory-doctor-real.sh" --json \
    --project "$PROJECT" --kit-dir "$RELEASE_B" \
    --product-root "$PRODUCT" --kit-sha "$SHA_B" \
    > "$TMP/qualification-ready-doctor.json" \
    2> "$TMP/qualification-ready-doctor.err" || QUALIFICATION_READY_RC=$?
python3 - "$TMP/qualification-ready-doctor.json" \
  "$TMP/qualification-ready-doctor.err" "$QUALIFICATION_READY_RC" <<'PY'
import json, pathlib, sys
value = json.load(open(sys.argv[1], encoding="utf-8"))
assert pathlib.Path(sys.argv[2]).read_bytes() == b""
assert int(sys.argv[3]) in {0, 1}, (
    sys.argv[3], {name: check["status"] for name, check in value["checks"].items()},
)
assert value["checks"]["fallback_readiness"]["status"] == "ok"
assert value["checks"]["fallback_readiness"]["report"]["status"] == "ready"
assert value["overall_status"] == "warning", {
    name: check["status"] for name, check in value["checks"].items()
}
assert value["checks"]["runtime"]["dispatch_leases"] == [
    {"state": "active", "ticket": "T-1"},
]
PY

mkdir -p "$TMP/qualification-controller/events"
chmod 700 "$TMP/qualification-controller" "$TMP/qualification-controller/events"
python3 - "$TMP/qualification-controller/events/recovered.json" "$SHA_B" <<'PY'
import hashlib, json, os, pathlib, sys
path, factory = pathlib.Path(sys.argv[1]), sys.argv[2]
value = {
    "event": "upgraded_claim_recovered",
    "factory_sha": factory,
    "from_factory_sha": factory,
    "observed_at_epoch_ns": 1,
    "qualification_generation": 1,
    "qualification_manifest_sha256": "a" * 64,
    "schema": "nysa.software-factory.controller-event/v1",
    "ticket": "T-1",
}
value["event_sha256"] = hashlib.sha256(json.dumps(
    value, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
).encode()).hexdigest()
path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
os.chmod(path, 0o600)
PY
HOME="$TEST_HOME" PATH="$TEST_BIN:/usr/bin:/bin" \
  FACTORY_TEST_MODE=1 FACTORY_TRUSTED_TEST_HARNESS=1 \
  FACTORY_DOCTOR_TIMEOUT_SECONDS=1 \
  FACTORY_DOCTOR_READINESS_TIMEOUT_SECONDS=5 \
  FACTORY_KIT_TRUST_SCOPE=qualification-candidate \
  FACTORY_CONTROLLER_STATE_DIR="$TMP/qualification-controller" \
  FACTORY_PROVIDER_POLICY="$TMP/provider/provider-policy.json" \
  /bin/bash "$RELEASE_B/scripts/factory-doctor-real.sh" --json \
    --project "$PROJECT" --kit-dir "$RELEASE_B" \
    --product-root "$PRODUCT" --kit-sha "$SHA_B" \
    > "$TMP/qualification-recovered-doctor.json"
python3 - "$TMP/qualification-recovered-doctor.json" <<'PY'
import json, sys
checks = json.load(open(sys.argv[1], encoding="utf-8"))["checks"]
assert checks["contract_resume"] == {"incidents": [], "status": "ok"}
assert checks["transition_receipts"] == {"incidents": [], "status": "ok"}
assert checks["authenticated_artifacts"] == {
    "reason_code": None, "status": "ok",
}
PY

# The deterministic qualification driver refuses that real Doctor result
# before reconciliation because this fixture has no exact provider pin receipt.
python3 - "$TMP/qualification-manifest.json" "$SHA_B" <<'PY'
import json, os, pathlib, sys
path, sha = pathlib.Path(sys.argv[1]), sys.argv[2]
path.write_text(json.dumps({
    "budget_usd": "100.000000",
    "capacity": 3,
    "contract_version": "2.0.0",
    "factory_sha": sha,
    "generation": 1,
    "per_run_budget_usd": "2.000000",
    "per_ticket_budget_usd": "25.000000",
    "schema": "nysa.software-factory.qualification/v2",
    "target_done": 3,
    "tickets": ["T-1", "T-2", "T-3"],
}, sort_keys=True) + "\n", encoding="utf-8")
os.chmod(path, 0o600)
PY

# Qualification Doctor repeats the shared ticket validator before provider
# readiness, exposing only typed ticket IDs when protected source evidence
# contradicts a selected ticket.
QUALIFICATION_PRODUCT="$TMP/qualification-product"
mkdir -p "$QUALIFICATION_PRODUCT/app" "$QUALIFICATION_PRODUCT/tests" \
  "$QUALIFICATION_PRODUCT/factory/tickets" \
  "$QUALIFICATION_PRODUCT/factory/initiatives"
cp "$TMP/qualification-manifest.json" \
  "$QUALIFICATION_PRODUCT/factory/QUALIFICATION.json"
printf '%s\n' "$SHA_B" > "$QUALIFICATION_PRODUCT/factory/KIT_PIN"
printf '%s\n' 'MAX_CONCURRENT_TICKETS=3' 'TEST_PATHS="tests/"' \
  > "$QUALIFICATION_PRODUCT/factory/PROJECT.env"
printf '%s\n' '# Qualification initiative' '' 'Status: active' \
  > "$QUALIFICATION_PRODUCT/factory/initiatives/I-1.md"
printf '%s\n' 'fixture' > "$QUALIFICATION_PRODUCT/README.md"
printf '%s\n' '<button data-testid="reload-app">Reload</button>' \
  > "$QUALIFICATION_PRODUCT/app/main.tsx"
printf '%s\n' 'fixture' > "$QUALIFICATION_PRODUCT/tests/fixture.txt"
cat > "$QUALIFICATION_PRODUCT/tests/main-boundary.test.tsx" <<'EOF'
import { readFileSync } from 'node:fs';
const source = readFileSync('app/main.tsx', 'utf8');
expect(source).toContain('<button data-testid="reload-app"');
EOF
for ticket in T-1 T-2 T-3; do
  owner=README.md
  [[ "$ticket" != T-2 ]] || owner=app/main.tsx
  cat > "$QUALIFICATION_PRODUCT/factory/tickets/$ticket.md" <<EOF
# $ticket — qualification Doctor fixture

State: Ready
Priority: normal
Initiative: I-1
Depends-On: none
Product-Decisions: frozen
Builder ownership: $owner only
Fixture-Seams: tests/fixture.txt
Authentication-Seams: none
Protected-Test-Conflicts: none
EOF
done
git -C "$QUALIFICATION_PRODUCT" init -q -b main
git -C "$QUALIFICATION_PRODUCT" config user.name "Qualification Doctor"
git -C "$QUALIFICATION_PRODUCT" config user.email "doctor@example.invalid"
git -C "$QUALIFICATION_PRODUCT" add -A
git -C "$QUALIFICATION_PRODUCT" commit -qm "seed qualification Doctor"
cp "$RELEASE_B/scripts/model-control.sh" "$TMP/model-control-ticket.saved"
cat > "$RELEASE_B/scripts/model-control.sh" <<EOF
#!/usr/bin/env bash
: > "$TMP/qualification-ticket-provider-probed"
printf '%s\n' '{"checks":[],"readiness_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","schema":"nysa.software-factory.qualification-fallback-readiness/v1","status":"ready"}'
EOF
chmod 700 "$RELEASE_B/scripts/model-control.sh"
QUALIFICATION_TICKET_DOCTOR_RC=0
HOME="$TEST_HOME" PATH="$TEST_BIN:/usr/bin:/bin" \
  FACTORY_TEST_MODE=1 FACTORY_TRUSTED_TEST_HARNESS=1 \
  FACTORY_DOCTOR_TIMEOUT_SECONDS=1 \
  FACTORY_DOCTOR_READINESS_TIMEOUT_SECONDS=5 \
  FACTORY_KIT_TRUST_SCOPE=qualification-candidate \
  FACTORY_QUALIFICATION_MANIFEST="$QUALIFICATION_PRODUCT/factory/QUALIFICATION.json" \
  FACTORY_PROVIDER_POLICY="$TMP/provider/provider-policy.json" \
  /bin/bash "$RELEASE_B/scripts/factory-doctor-real.sh" --json \
    --project "$PROJECT" --kit-dir "$RELEASE_B" \
    --product-root "$QUALIFICATION_PRODUCT" --kit-sha "$SHA_B" \
    > "$TMP/qualification-ticket-doctor.json" || QUALIFICATION_TICKET_DOCTOR_RC=$?
mv "$TMP/model-control-ticket.saved" "$RELEASE_B/scripts/model-control.sh"
python3 - "$TMP/qualification-ticket-doctor.json" \
  "$QUALIFICATION_TICKET_DOCTOR_RC" \
  "$TMP/qualification-ticket-provider-probed" <<'PY'
import json, pathlib, sys
value = json.load(open(sys.argv[1], encoding="utf-8"))
assert int(sys.argv[2]) == 1
assert value["overall_status"] == "error"
assert value["checks"]["qualification_ticket_readiness"] == {
    "reason_code": "protected_source_conflict",
    "status": "error",
    "tickets": [
        {"reason_code": None, "status": "ok", "ticket": "T-1"},
        {
            "reason_code": "protected_source_conflict",
            "status": "error",
            "ticket": "T-2",
        },
        {"reason_code": None, "status": "ok", "ticket": "T-3"},
    ],
}
assert value["checks"]["fallback_readiness"] == {
    "report": None, "status": "not_applicable",
}
assert not pathlib.Path(sys.argv[3]).exists()
PY

# Persisted protected artifact drift is reported before provider readiness.
mkdir -p "$QUALIFICATION_PRODUCT/factory/migrations/contract-1.3-terminal-backfill"
printf '%s\n' '{}' > \
  "$QUALIFICATION_PRODUCT/factory/migrations/contract-1.3-terminal-backfill/authorization.json"
git -C "$QUALIFICATION_PRODUCT" add -A
git -C "$QUALIFICATION_PRODUCT" commit -qm "tamper protected terminal batch"
PROTECTED_ARTIFACT_STATE="$TMP/protected-artifact-controller"
mkdir -m 700 "$PROTECTED_ARTIFACT_STATE"
cp "$RELEASE_B/scripts/model-control.sh" "$TMP/model-control-protected.saved"
cat > "$RELEASE_B/scripts/model-control.sh" <<EOF
#!/usr/bin/env bash
: > "$TMP/protected-artifact-provider-probed"
exit 1
EOF
chmod 700 "$RELEASE_B/scripts/model-control.sh"
PROTECTED_ARTIFACT_DOCTOR_RC=0
HOME="$TEST_HOME" PATH="$TEST_BIN:/usr/bin:/bin" \
  FACTORY_TEST_MODE=1 FACTORY_TRUSTED_TEST_HARNESS=1 \
  FACTORY_DOCTOR_TIMEOUT_SECONDS=1 \
  FACTORY_DOCTOR_READINESS_TIMEOUT_SECONDS=5 \
  FACTORY_KIT_TRUST_SCOPE=qualification-candidate \
  FACTORY_CONTROLLER_STATE_DIR="$PROTECTED_ARTIFACT_STATE" \
  FACTORY_QUALIFICATION_MANIFEST="$QUALIFICATION_PRODUCT/factory/QUALIFICATION.json" \
  FACTORY_PROVIDER_POLICY="$TMP/provider/provider-policy.json" \
  /bin/bash "$RELEASE_B/scripts/factory-doctor-real.sh" --json \
    --project "$PROJECT" --kit-dir "$RELEASE_B" \
    --product-root "$QUALIFICATION_PRODUCT" --kit-sha "$SHA_B" \
    > "$TMP/protected-artifact-doctor.json" || PROTECTED_ARTIFACT_DOCTOR_RC=$?
mv "$TMP/model-control-protected.saved" "$RELEASE_B/scripts/model-control.sh"
python3 - "$TMP/protected-artifact-doctor.json" \
  "$PROTECTED_ARTIFACT_DOCTOR_RC" \
  "$TMP/protected-artifact-provider-probed" <<'PY'
import json, pathlib, sys
value = json.load(open(sys.argv[1], encoding="utf-8"))
assert int(sys.argv[2]) == 1
assert value["overall_status"] == "error"
assert value["checks"]["authenticated_artifacts"] == {
    "reason_code": "protected_artifact_invalid", "status": "error",
}
assert value["checks"]["fallback_readiness"] == {
    "report": None, "status": "not_applicable",
}
assert not pathlib.Path(sys.argv[3]).exists()
PY
cat > "$TMP/qualification-driver-launcher" <<'EOF'
#!/usr/bin/env bash
case "$2" in
  doctor) cat "$QUALIFICATION_DOCTOR_JSON" ;;
  reconcile)
    : > "$QUALIFICATION_RECONCILE_MARKER"
    printf '%s\n' '{"active":1,"results":[],"schema":"nysa.software-factory.controller/v1","status":"waiting_for_target"}'
    ;;
  *) exit 2 ;;
esac
EOF
chmod 700 "$TMP/qualification-driver-launcher"
QUALIFICATION_DRIVER_RC=0
FACTORY_QUALIFICATION_MANIFEST="$TMP/qualification-manifest.json" \
  FACTORY_RELEASE_SHA="$SHA_B" \
  QUALIFICATION_DOCTOR_JSON="$TMP/qualification-ready-doctor.json" \
  QUALIFICATION_RECONCILE_MARKER="$TMP/qualification-reconcile-called" \
  python3 "$ROOT/scripts/qualification-run.py" \
    --launcher "$TMP/qualification-driver-launcher" \
    --project "$PROJECT" --json > "$TMP/qualification-driver.json" \
    || QUALIFICATION_DRIVER_RC=$?
python3 - "$TMP/qualification-driver.json" "$QUALIFICATION_DRIVER_RC" \
  "$TMP/qualification-reconcile-called" <<'PY'
import json, pathlib, sys
value = json.load(open(sys.argv[1], encoding="utf-8"))
assert int(sys.argv[2]) == 3
assert not pathlib.Path(sys.argv[3]).exists()
assert value["doctor_status"] == "warning"
assert value["reason"] == "doctor_not_ready"
assert value["status"] == "blocked"
PY

mkdir -p "$PRODUCT/factory/.active-runs/T-1.planner.lock"
chmod 700 "$PRODUCT/factory/.active-runs" \
  "$PRODUCT/factory/.active-runs/T-1.planner.lock"
printf '%s\n' "pid=$$" 'process_start=fixture' \
  'token=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' \
  > "$PRODUCT/factory/.active-runs/T-1.planner.lock/owner"
chmod 600 "$PRODUCT/factory/.active-runs/T-1.planner.lock/owner"
HOME="$TEST_HOME" PATH="$TEST_BIN:/usr/bin:/bin" \
  FACTORY_TEST_MODE=1 FACTORY_TRUSTED_TEST_HARNESS=1 \
  FACTORY_DOCTOR_TIMEOUT_SECONDS=1 \
  FACTORY_DOCTOR_READINESS_TIMEOUT_SECONDS=5 \
  FACTORY_KIT_TRUST_SCOPE=qualification-candidate \
  FACTORY_PROVIDER_POLICY="$TMP/provider/provider-policy.json" \
  /bin/bash "$RELEASE_B/scripts/factory-doctor-real.sh" --json \
    --project "$PROJECT" --kit-dir "$RELEASE_B" \
    --product-root "$PRODUCT" --kit-sha "$SHA_B" \
    > "$TMP/qualification-active-claim-doctor.json"
rm -f "$TMP/qualification-reconcile-called"
QUALIFICATION_DRIVER_RC=0
FACTORY_QUALIFICATION_MANIFEST="$TMP/qualification-manifest.json" \
  FACTORY_RELEASE_SHA="$SHA_B" \
  QUALIFICATION_DOCTOR_JSON="$TMP/qualification-active-claim-doctor.json" \
  QUALIFICATION_RECONCILE_MARKER="$TMP/qualification-reconcile-called" \
  python3 "$ROOT/scripts/qualification-run.py" \
    --launcher "$TMP/qualification-driver-launcher" \
    --project "$PROJECT" --json > "$TMP/qualification-active-claim-driver.json" \
    || QUALIFICATION_DRIVER_RC=$?
python3 - "$TMP/qualification-active-claim-doctor.json" \
  "$TMP/qualification-active-claim-driver.json" "$QUALIFICATION_DRIVER_RC" \
  "$TMP/qualification-reconcile-called" <<'PY'
import json, pathlib, sys
doctor = json.load(open(sys.argv[1], encoding="utf-8"))
driver = json.load(open(sys.argv[2], encoding="utf-8"))
assert doctor["checks"]["runtime"]["active_run_claims"] == 1
assert doctor["checks"]["runtime"]["active_run_tickets"] == ["T-1"]
assert doctor["checks"]["runtime"]["run_records"] == 0
assert int(sys.argv[3]) == 3
assert not pathlib.Path(sys.argv[4]).exists()
assert driver["reason"] == "doctor_not_ready"
assert driver["status"] == "blocked"
PY
rm -rf "$PRODUCT/factory/.active-runs"

mkdir "$TMP/foreign-active-runs"
ln -s "$TMP/foreign-active-runs" "$PRODUCT/factory/.active-runs"
HOME="$TEST_HOME" PATH="$TEST_BIN:/usr/bin:/bin" \
  FACTORY_TEST_MODE=1 FACTORY_TRUSTED_TEST_HARNESS=1 \
  FACTORY_DOCTOR_TIMEOUT_SECONDS=1 \
  FACTORY_DOCTOR_READINESS_TIMEOUT_SECONDS=5 \
  FACTORY_KIT_TRUST_SCOPE=qualification-candidate \
  FACTORY_PROVIDER_POLICY="$TMP/provider/provider-policy.json" \
  /bin/bash "$RELEASE_B/scripts/factory-doctor-real.sh" --json \
    --project "$PROJECT" --kit-dir "$RELEASE_B" \
    --product-root "$PRODUCT" --kit-sha "$SHA_B" \
    > "$TMP/qualification-malformed-claim-doctor.json" || true
rm -f "$TMP/qualification-reconcile-called"
QUALIFICATION_DRIVER_RC=0
FACTORY_QUALIFICATION_MANIFEST="$TMP/qualification-manifest.json" \
  FACTORY_RELEASE_SHA="$SHA_B" \
  QUALIFICATION_DOCTOR_JSON="$TMP/qualification-malformed-claim-doctor.json" \
  QUALIFICATION_RECONCILE_MARKER="$TMP/qualification-reconcile-called" \
  python3 "$ROOT/scripts/qualification-run.py" \
    --launcher "$TMP/qualification-driver-launcher" \
    --project "$PROJECT" --json > "$TMP/qualification-malformed-claim-driver.json" \
    || QUALIFICATION_DRIVER_RC=$?
python3 - "$TMP/qualification-malformed-claim-doctor.json" \
  "$TMP/qualification-malformed-claim-driver.json" "$QUALIFICATION_DRIVER_RC" \
  "$TMP/qualification-reconcile-called" <<'PY'
import json, pathlib, sys
doctor = json.load(open(sys.argv[1], encoding="utf-8"))
driver = json.load(open(sys.argv[2], encoding="utf-8"))
runtime = doctor["checks"]["runtime"]
assert doctor["overall_status"] == "error"
assert runtime["active_run_claims"] == 0
assert runtime["active_run_tickets"] == []
assert runtime["malformed_active_run_claims"] == 1
assert int(sys.argv[3]) == 3
assert not pathlib.Path(sys.argv[4]).exists()
assert driver["reason"] == "doctor_not_ready"
assert driver["status"] == "blocked"
PY
rm "$PRODUCT/factory/.active-runs"

cat > "$RELEASE_B/scripts/model-control.sh" <<'EOF'
#!/usr/bin/env bash
sleep 30
EOF
chmod 700 "$RELEASE_B/scripts/model-control.sh"
QUALIFICATION_HANG_STARTED="$(python3 -c 'import time; print(time.monotonic())')"
QUALIFICATION_HANG_RC=0
HOME="$TEST_HOME" PATH="$TEST_BIN:/usr/bin:/bin" \
  FACTORY_TEST_MODE=1 FACTORY_TRUSTED_TEST_HARNESS=1 \
  FACTORY_DOCTOR_TIMEOUT_SECONDS=1 \
  FACTORY_DOCTOR_READINESS_TIMEOUT_SECONDS=1 \
  FACTORY_KIT_TRUST_SCOPE=qualification-candidate \
  /bin/bash "$RELEASE_B/scripts/factory-doctor-real.sh" --json \
    --project "$PROJECT" --kit-dir "$RELEASE_B" \
    --product-root "$PRODUCT" --kit-sha "$SHA_B" \
    > "$TMP/qualification-hanging-doctor.json" \
    2> "$TMP/qualification-hanging-doctor.err" || QUALIFICATION_HANG_RC=$?
QUALIFICATION_HANG_ENDED="$(python3 -c 'import time; print(time.monotonic())')"
mv "$TMP/model-control.saved" "$RELEASE_B/scripts/model-control.sh"
python3 - "$TMP/qualification-hanging-doctor.json" \
  "$TMP/qualification-hanging-doctor.err" "$QUALIFICATION_HANG_RC" \
  "$QUALIFICATION_HANG_STARTED" "$QUALIFICATION_HANG_ENDED" <<'PY'
import json, pathlib, sys
value = json.load(open(sys.argv[1], encoding="utf-8"))
assert pathlib.Path(sys.argv[2]).read_bytes() == b""
assert int(sys.argv[3]) == 1, sys.argv[3]
assert float(sys.argv[5]) - float(sys.argv[4]) < 5, (
    float(sys.argv[5]) - float(sys.argv[4])
)
assert value["checks"]["fallback_readiness"] == {
    "status": "error", "report": None,
}
PY

python3 - "$LAUNCHER" <<'PY'
import pathlib, sys
source = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")
for name in (
    "FACTORY_KIT_CANONICAL_ORIGIN",
    "FACTORY_KIT_ORIGIN",
    "FACTORY_KITS_ROOT",
    "FACTORY_RELEASE_TEST_HOME",
):
    assert f'"{name}"' in source
assert 'name.startswith("FACTORY_KIT_TEST_")' in source
assert 'value.get("kit_origin") != "github.com/nysa-company/software-factory"' in source
assert 'suite.get("verification_source") != "github-actions-full"' in source
PY

echo "PASS: Contract 2 launcher and Doctor boundary"
