#!/usr/bin/env bash
# Adversarial, self-contained tests for scripts/factory-kit.sh.
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
KIT="$ROOT/scripts/factory-kit.sh"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/factory-kit-test.XXXXXX")"
TMP="$(cd "$TMP" && pwd -P)"
TEST_TMP="$TMP/tmp"
CANONICAL="$TMP/canonical.git"
KIT_REPO="$TMP/kit-source"
STUB_BIN="$TMP/bin"
RELEASE_TEST_HOME="$TMP"
STATE="$RELEASE_TEST_HOME/.factory/kits"
PINNED_SCANNER_STUB="$STUB_BIN/gitleaks"
GH_TRACE="$TMP/gh.trace"
FAILURES=0
LAST_OUTPUT=""
FIRST_PID=""
REAL_HOME_SANDBOX_SECRET=""

mkdir -p "$TEST_TMP" "$STUB_BIN"
chmod 700 "$RELEASE_TEST_HOME"
cat > "$PINNED_SCANNER_STUB" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
chmod +x "$PINNED_SCANNER_STUB"

cleanup() {
  if [[ -n "$FIRST_PID" ]] && kill -0 "$FIRST_PID" 2>/dev/null; then
    kill "$FIRST_PID" 2>/dev/null || true
    wait "$FIRST_PID" 2>/dev/null || true
  fi
  if [[ -n "$REAL_HOME_SANDBOX_SECRET" ]]; then
    rm -f "$REAL_HOME_SANDBOX_SECRET"
  fi
  chmod -R u+w "$TMP" 2>/dev/null || true
  rm -rf "$TMP"
}
trap cleanup EXIT

pass() { printf 'PASS: %s\n' "$1"; }
fail() {
  printf 'FAIL: %s%s\n' "$1" "${2:+ — $2}" >&2
  FAILURES=$((FAILURES + 1))
}

git_identity() {
  export GIT_AUTHOR_NAME="factory-kit test"
  export GIT_AUTHOR_EMAIL="factory-kit-test@example.invalid"
  export GIT_COMMITTER_NAME="$GIT_AUTHOR_NAME"
  export GIT_COMMITTER_EMAIL="$GIT_AUTHOR_EMAIL"
}

run_kit() {
  PATH="$STUB_BIN:$PATH" \
  TMPDIR="$TEST_TMP" \
  FACTORY_KITS_ROOT="$STATE" \
  FACTORY_KIT_TEST_MODE=1 \
  FACTORY_RELEASE_TEST_HOME="$RELEASE_TEST_HOME" \
  FACTORY_KIT_TEST_SKIP_PROVIDER_CLI_PIN=1 \
  FACTORY_KIT_TEST_REMOTE_FULL_CI="${FACTORY_KIT_TEST_REMOTE_FULL_CI:-1}" \
  FACTORY_KIT_TEST_PINNED_SCANNER="$PINNED_SCANNER_STUB" \
  FACTORY_KIT_CANONICAL_ORIGIN="$CANONICAL" \
  FACTORY_KIT_GH_TRACE="$GH_TRACE" \
  FACTORY_KIT_LOCK_ATTEMPTS="${FACTORY_KIT_LOCK_ATTEMPTS:-20}" \
    bash "$KIT" "$@"
}

run_kit_with_state() {
  local state="$1" test_home
  shift
  test_home="$(dirname "$(dirname "$state")")"
  PATH="$STUB_BIN:$PATH" \
  TMPDIR="$TEST_TMP" \
  FACTORY_KITS_ROOT="$state" \
  FACTORY_KIT_TEST_MODE=1 \
  FACTORY_RELEASE_TEST_HOME="$test_home" \
  FACTORY_KIT_TEST_SKIP_PROVIDER_CLI_PIN=1 \
  FACTORY_KIT_TEST_REMOTE_FULL_CI="${FACTORY_KIT_TEST_REMOTE_FULL_CI:-1}" \
  FACTORY_KIT_TEST_PINNED_SCANNER="$PINNED_SCANNER_STUB" \
  FACTORY_KIT_CANONICAL_ORIGIN="$CANONICAL" \
  FACTORY_KIT_GH_TRACE="$GH_TRACE" \
  FACTORY_KIT_LOCK_ATTEMPTS="${FACTORY_KIT_LOCK_ATTEMPTS:-20}" \
    bash "$KIT" "$@"
}

expect_success() {
  local label="$1"
  shift
  if LAST_OUTPUT="$(run_kit "$@" 2>&1)"; then
    pass "$label"
    return 0
  fi
  fail "$label" "$LAST_OUTPUT"
  return 1
}

expect_failure() {
  local label="$1"
  shift
  if LAST_OUTPUT="$(run_kit "$@" 2>&1)"; then
    fail "$label" "command unexpectedly succeeded: $LAST_OUTPUT"
    return 1
  fi
  pass "$label"
  return 0
}

json_value() {
  python3 - "$1" "$2" <<'PY'
import json, sys
value = json.load(open(sys.argv[1]))
for part in sys.argv[2].split("."):
    value = value[part]
print("" if value is None else value)
PY
}

project_receipt_count() {
  python3 - "$STATE/receipts" "$1" "$2" <<'PY'
import json, pathlib, sys
root, project, factory_sha = pathlib.Path(sys.argv[1]), *sys.argv[2:]
print(sum(
    1 for path in root.glob("*.json")
    if (value := json.loads(path.read_text())).get("project") == project
    and value.get("kit_sha") == factory_sha
))
PY
}

failure_receipt_valid() {
  python3 - "$1" <<'PY'
import hashlib, json, pathlib, sys
path = pathlib.Path(sys.argv[1])
value = json.loads(path.read_text())
identity = {key: value[key] for key in (
    "certification_exit_status", "driver_exit_status",
    "driver_output_sha256", "evidence_sha256", "factory_sha",
    "failure_stage", "host_load_sha256", "product_output_sha256",
    "product_tree",
)}
canonical = lambda item: json.dumps(
    item, sort_keys=True, separators=(",", ":")
).encode()
assert value["failure_id"] == hashlib.sha256(canonical(identity)).hexdigest()
assert path.stem == value["failure_id"]
assert value["host_load_sha256"] == hashlib.sha256(canonical(
    value["product_certification_host_load"]
)).hexdigest()
assert value["redacted_output_sha256"] == hashlib.sha256(
    value["redacted_output"].encode()
).hexdigest()
record = dict(value)
digest = record.pop("record_sha256")
assert digest == hashlib.sha256(canonical(record)).hexdigest()
PY
}

product_certification_host_load_valid() {
  python3 - "$1" <<'PY'
import json, math, re, sys
value = json.load(open(sys.argv[1]))["product_certification_host_load"]
assert set(value) == {"end", "start"}
timestamp = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
for observation in value.values():
    assert set(observation) == {
        "load_average_1m", "load_average_5m", "load_average_15m",
        "logical_cpu_count", "observed_at",
    }
    assert timestamp.fullmatch(observation["observed_at"])
    assert type(observation["logical_cpu_count"]) is int
    assert observation["logical_cpu_count"] > 0
    for key in ("load_average_1m", "load_average_5m", "load_average_15m"):
        item = observation[key]
        assert type(item) in (int, float) and math.isfinite(item) and item >= 0
assert value["end"]["observed_at"] >= value["start"]["observed_at"]
PY
}

set_evidence_value() {
  local path="$1" key="$2" value="$3"
  python3 - "$path" "$key" "$value" <<'PY'
import hashlib, json, pathlib, sys
path, key, raw = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3]
value = json.loads(path.read_text())
value[key] = json.loads(raw)
value.pop("evidence_id", None)
payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
value["evidence_id"] = hashlib.sha256(payload).hexdigest()
path.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")
PY
  chmod 600 "$path"
}

state_snapshot() {
  python3 - "$STATE" <<'PY'
import hashlib, os, pathlib, stat, sys
root = pathlib.Path(sys.argv[1])
rows = []
if root.exists():
    for path in sorted([root] + list(root.rglob("*"))):
        relative = "." if path == root else str(path.relative_to(root))
        mode = stat.S_IMODE(path.lstat().st_mode)
        if path.is_symlink():
            payload = "link:" + os.readlink(str(path))
        elif path.is_file():
            payload = hashlib.sha256(path.read_bytes()).hexdigest()
        else:
            payload = "dir"
        rows.append("%s|%o|%s" % (relative, mode, payload))
print("\n".join(rows))
PY
}

commit_all() {
  local repo="$1" message="$2"
  git -C "$repo" add -A
  git -C "$repo" commit -qm "$message"
}

push_main() {
  git -C "$1" push -q origin main
}

set_pin() {
  local product="$1" sha="$2"
  printf '%s\n' "$sha" > "$product/factory/KIT_PIN"
  commit_all "$product" "pin $sha"
  push_main "$product"
}

set_ticket_lease() {
  local product="$1" sha="$2" file
  file="$product/factory/tickets/T-004.md"
  python3 - "$file" "$sha" <<'PY'
import pathlib, re, sys
path, sha = pathlib.Path(sys.argv[1]), sys.argv[2]
text = path.read_text()
if re.search(r"(?m)^Kit-SHA:", text):
    text = re.sub(r"(?m)^Kit-SHA:.*$", "Kit-SHA: " + sha, text)
else:
    text += "\nKit-SHA: " + sha + "\n"
path.write_text(text)
PY
}

write_inflight_authorization() {
  local product="$1" source="$2" target="$3" ticket="$4" head="$5" state="$6"
  local path="$product/factory/migrations/inflight-release/$target.json"
  mkdir -p "$(dirname "$path")"
  python3 - "$path" "$source" "$target" "$ticket" "$head" "$state" <<'PY'
import json, pathlib, sys
path = pathlib.Path(sys.argv[1])
source, target, ticket, head, state = sys.argv[2:]
path.write_text(json.dumps({
    "repository": "example/test-product",
    "schema": "nysa.software-factory.inflight-release-authorization/v1",
    "source_kit_sha": source,
    "target_kit_sha": target,
    "tickets": [{
        "branch": "ticket/" + ticket, "head": head,
        "state": state, "ticket": ticket,
    }],
}, sort_keys=True, separators=(",", ":")) + "\n")
PY
}

migrate_v2_fixture() {
  local path="$1" target="$2"
  python3 - "$ROOT/scripts/model-manager.py" "$path" "$target" <<'PY'
import importlib.util, json, pathlib, sys
manager_path, journal_path, target = sys.argv[1:]
spec = importlib.util.spec_from_file_location("fixture_manager", manager_path)
manager = importlib.util.module_from_spec(spec)
spec.loader.exec_module(manager)
path = pathlib.Path(journal_path)
value = json.loads(path.read_text())
catalog, routes, _, profiles = manager.ROUTER.load_policy()
readiness = {
    route_id: {
        "adapter_version": "test-current", "reason": "test",
        "reported_identity": route["expected_reported_identity"],
        "state": "READY",
    }
    for route_id, route in routes.items() if route["enabled"]
}
migrated = manager.migrate_v2_journal(
    value, "5" * 40, target, "2026-07-21T00:01:00Z",
    catalog, routes, profiles, readiness,
)
assert migrated["revisions"][:-1] == value["revisions"]
body = migrated["revisions"][-1]["body"]
assert body["prior_resolution_sha256"] == manager.ROUTER.content_hash(manager.active_resolution(value))
assert manager.active_resolution(migrated) == body.get("new_resolution", manager.active_resolution(value))
path.write_text(json.dumps(migrated, sort_keys=True, separators=(",", ":")) + "\n")
PY
}

restore_product_tuple() {
  local product="$1" sha="$2"
  printf '%s\n' "$sha" > "$product/factory/KIT_PIN"
  set_ticket_lease "$product" "$sha"
  commit_all "$product" "restore previous certified product tree"
  push_main "$product"
}

make_product() {
  local name="$1" path bare node_version npm_version
  path="$TMP/$name"
  bare="$TMP/$name.git"
  git init --bare -q "$bare"
  git init -q -b main "$path"
  git -C "$path" remote add origin "$bare"
  mkdir -p "$path/factory/tickets" "$path/scripts"
  cat > "$path/scripts/secret-scan" <<'EOF'
#!/usr/bin/env python3
VERSION = "8.30.1"
EOF
  chmod +x "$path/scripts/secret-scan"
  cat > "$path/factory/PROJECT.env" <<'EOF'
PROJECT_NAME=test-product
GH_REPO=example/test-product
CERTIFY_SCRIPT=factory/certify.sh
PREVIEW_PROVIDER=none
NONVISUAL_PATHS=app/tools/,app/tests/
TEST_PATHS=app/tests/
EOF
  node_version="$(node --version)"
  npm_version="$(npm --version)"
  cat > "$path/factory/certification-plan.json" <<EOF
{"phases":[{"artifacts":[],"command":["true"],"depends_on":[],"name":"fixture","network":"denied"}],"runtime":{"node":"$node_version","npm":"$npm_version"},"schema":"nysa.software-factory.certification-plan/v2"}
EOF
  cat > "$path/factory/certify.sh" <<'EOF'
#!/usr/bin/env bash
set -eu
case "$HOME" in
  *factory-kit-certification*) ;;
  *) echo "api_token=supersecret https://user:pass@example.invalid"; exit 43 ;;
esac
[[ "$(pwd -P)" == "$FACTORY_PRODUCT_ROOT" ]]
[[ "$FACTORY_KIT_RELEASE" == *factory-kit-certification*/release ]]
[[ -x .context/tools/gitleaks/8.30.1/gitleaks ]]
if [[ -f factory/SLEEP_CERTIFY ]]; then
  sleep 2
fi
python3 "$FACTORY_KIT_RELEASE/scripts/certification-runner.py" \
  --plan factory/certification-plan.json \
  --result "$FACTORY_CERTIFICATION_EVIDENCE" \
  --workers 2
if [[ -f factory/FAIL_CERTIFY ]]; then
  printf '%s\n' \
    'api_token=supersecret multi token tail' \
    'Authorization: Bearer bearer-one bearer-two bearer-three' \
    'Proxy-Authorization: Digest digest-one digest-two' \
    '{"service.auth-token":"json-one json-two","safe":"visible"}' \
    '"database-password": "line-one
line-two line-three"' \
    'client-secret = hyphen-one hyphen-two' \
    'service.token: |-' \
    '  yaml-one yaml-two' \
    '  yaml-three' \
    'auth-key: continuation-head' \
    '    continuation-one continuation-two' \
    'https://example.invalid/path?safe=visible&access_token=query-one&api-key=query-two' \
    'https://user:pass@example.invalid'
  exit 42
fi
touch "$HOME/.factory-kit-certification-marker"
touch factory/product-certification-marker
touch "$FACTORY_KIT_RELEASE/release-certification-marker"
EOF
  chmod +x "$path/factory/certify.sh"
  cat > "$path/.gitignore" <<'EOF'
factory/MAINTENANCE
factory/.launch.lock/
factory/.provider.lock/
factory/.active-runs/
factory/runs/
factory/.dispatch-leases/
factory/.dispatch-leases.lock/
factory/.linear-sync-cycle.lock
factory/.linear-sync.lock
EOF
  cat > "$path/factory/tickets/T-001.md" <<'EOF'
State: Ready
EOF
  cat > "$path/factory/tickets/T-002.md" <<'EOF'
State: Backlog
EOF
  cat > "$path/factory/tickets/T-003.md" <<'EOF'
State: Backlog
EOF
  cat > "$path/factory/tickets/T-004.md" <<'EOF'
State: Planning
EOF
  cat > "$path/factory/tickets/T-005.md" <<'EOF'
State: Blocked-Escalated
EOF
  cat > "$path/factory/tickets/T-006.md" <<'EOF'
State: Canceled
EOF
  cat > "$path/factory/tickets/T-101-bundle.md" <<'EOF'
# Evidence bundle

This is supporting evidence, not a canonical ticket record.
EOF
  printf '%s\n' "placeholder" > "$path/factory/KIT_PIN"
  commit_all "$path" "product fixture"
  git -C "$path" push -qu origin main
  printf '%s\n' "$path"
}

cat > "$STUB_BIN/gh" <<'EOF'
#!/usr/bin/env bash
set -eu
printf '%s\n' "$*" >> "${FACTORY_KIT_GH_TRACE:?}"
endpoint=""
for arg in "$@"; do
  case "$arg" in repos/*) endpoint="$arg" ;; esac
done
case "$endpoint" in
  repos/nysa-company/software-factory/rulesets\?*)
    if [[ "${GH_NO_APPLICABLE_RULESET:-0}" == "1" ]]; then
      printf '%s\n' '[[{"id":102,"enforcement":"active","target":"branch"}]]'
    else
      printf '%s\n' '[[{"id":101,"enforcement":"active","target":"branch"},{"id":102,"enforcement":"active","target":"branch"}]]'
    fi
    ;;
  repos/nysa-company/software-factory/rulesets/101)
    bypass='[]'
    pull_request='{"type":"pull_request"},'
    integration_id=7
    [[ "${GH_UNSAFE_BYPASS:-0}" == "1" ]] &&
      bypass='[{"actor_id":1,"actor_type":"RepositoryRole","bypass_mode":"always"}]'
    [[ "${GH_NO_PULL_REQUEST:-0}" == "1" ]] && pull_request=''
    [[ "${GH_UNBOUND_RULESET_CHECK:-0}" == "1" ]] && integration_id=null
    printf '{"conditions":{"ref_name":{"include":["refs/heads/main"],"exclude":[]}},"bypass_actors":%s,"rules":[%s{"type":"required_status_checks","parameters":{"required_status_checks":[{"context":"ruleset-ci","integration_id":%s}]}}]}\n' \
      "$bypass" "$pull_request" "$integration_id"
    ;;
  repos/nysa-company/software-factory/rulesets/102)
    printf '%s\n' '{"conditions":{"ref_name":{"include":["refs/heads/*"],"exclude":["refs/heads/main"]}},"rules":[{"type":"required_status_checks","parameters":{"required_status_checks":[{"context":"excluded-ci","integration_id":99}]}}]}'
    ;;
  repos/nysa-company/software-factory/branches/main/protection/required_status_checks)
    printf '%s\n' '[{"contexts":[],"checks":[{"context":"app-ci","app_id":42}]}]'
    ;;
  repos/nysa-company/software-factory/commits/*/check-runs\?*)
    app_conclusion=success
    rule_conclusion=success
    [[ "${GH_FAIL_APP:-0}" == "1" ]] && app_conclusion=failure
    [[ "${GH_FAIL_RULESET:-0}" == "1" ]] && rule_conclusion=failure
    printf '[
{"check_runs":[
{"id":10,"name":"app-ci","status":"completed","conclusion":"success","app":{"id":999}},
{"id":11,"name":"app-ci","status":"completed","conclusion":"%s","app":{"id":42}},
{"id":12,"name":"ruleset-ci","status":"completed","conclusion":"%s","app":{"id":7}}
]}]\n' "$app_conclusion" "$rule_conclusion"
    ;;
  repos/nysa-company/software-factory/commits/*/statuses\?*)
    printf '%s\n' '[[{"context":"classic-ci","state":"success"},{"context":"app-ci","state":"success"},{"context":"ruleset-ci","state":"success"}]]'
    ;;
  *) printf 'unexpected gh endpoint: %s\n' "$endpoint" >&2; exit 2 ;;
esac
EOF
chmod +x "$STUB_BIN/gh"

git_identity
git init --bare -q "$CANONICAL"
git init -q -b main "$KIT_REPO"
git -C "$KIT_REPO" remote add origin "$CANONICAL"
if FACTORY_KIT_TEST_MODE=1 FACTORY_KIT_CANONICAL_ORIGIN="$CANONICAL" \
   FACTORY_KITS_ROOT="$STATE" bash "$KIT" --help >/dev/null 2>&1; then
  fail "Factory test mode requires an explicit test home"
else
  pass "Factory test mode requires an explicit test home"
fi
if FACTORY_KIT_TEST_MODE=1 FACTORY_KIT_CANONICAL_ORIGIN="$CANONICAL" \
   FACTORY_RELEASE_TEST_HOME="$RELEASE_TEST_HOME" \
   FACTORY_KITS_ROOT="$HOME/.factory/kits" bash "$KIT" --help >/dev/null 2>&1; then
  fail "Factory test mode refuses the real kits root"
else
  pass "Factory test mode refuses the real kits root"
fi
mkdir -p "$KIT_REPO/ci" "$KIT_REPO/scripts/lib" "$KIT_REPO/scripts/launchd"
mkdir -p "$KIT_REPO/scripts/model-routing"
cp "$ROOT/scripts/model-manager.py" "$ROOT/scripts/model-router.py" \
  "$ROOT/scripts/certification-runner.py" \
  "$ROOT/scripts/certification-preflight.py" \
  "$ROOT/scripts/operator-preflight-report.py" \
  "$ROOT/scripts/ticket-readiness.py" \
  "$KIT_REPO/scripts/"
cp "$ROOT/scripts/lib/certification_plan.py" \
  "$ROOT/scripts/lib/activation_preflight.py" \
  "$ROOT/scripts/lib/certification_cache.py" \
  "$ROOT/scripts/lib/effective_ticket.py" \
  "$ROOT/scripts/lib/historical_pr_objects.py" \
  "$ROOT/scripts/lib/inflight_release.py" \
  "$ROOT/scripts/lib/legacy_closeout.py" \
  "$ROOT/scripts/lib/operator_receipt.py" \
  "$ROOT/scripts/lib/protected_merge_reconciliation.py" \
  "$ROOT/scripts/lib/terminal_backfill.py" \
  "$ROOT/scripts/lib/approval_evidence.py" \
  "$ROOT/scripts/lib/ticket_state_transition.py" \
  "$KIT_REPO/scripts/lib/"
cp "$ROOT/scripts/factory-launch" \
  "$KIT_REPO/scripts/factory-launch"
cp "$ROOT/factory-contract.json" "$KIT_REPO/factory-contract.json"
cp "$ROOT/scripts/factory-incident-reporter.py" \
  "$KIT_REPO/scripts/factory-incident-reporter.py"
cp "$ROOT/scripts/launchd/com.factory.incident-reporter.plist.template" \
  "$KIT_REPO/scripts/launchd/com.factory.incident-reporter.plist.template"
chmod +x "$KIT_REPO/scripts/factory-launch"
cp "$ROOT/scripts/model-routing/catalog-v1.json" \
  "$ROOT/scripts/model-routing/profiles-v1.json" \
  "$KIT_REPO/scripts/model-routing/"
cat > "$KIT_REPO/ci/test-all.sh" <<'EOF'
#!/usr/bin/env bash
set -eu
if [[ "${FACTORY_FIXTURE_DIRTY:-0}" == "1" ]]; then
  printf 'mutated by test\n' > payload.txt
fi
case "$HOME" in
  *factory-kit-install*|*factory-kit-certification*) ;;
  *) printf 'install/certification HOME was not isolated\n' >&2; exit 44 ;;
esac
if ! ps -axo 'pid=,pgid=,lstart=' |
  awk -v pid="$$" '$1 == pid && $2 > 1 && NF >= 3 { found=1 } END { exit !found }'; then
  printf 'sandbox process table omitted the test runner\n' >&2
  exit 48
fi
if [[ -n "${FACTORY_KIT_SANDBOX_DENY_SIBLING:-}" ]] &&
   /bin/cat "$FACTORY_KIT_SANDBOX_DENY_SIBLING" >/dev/null 2>&1; then
  printf 'sandbox read sibling secret\n' >&2
  exit 45
fi
if [[ -n "${FACTORY_KIT_SANDBOX_DENY_HOME:-}" ]] &&
   /bin/cat "$FACTORY_KIT_SANDBOX_DENY_HOME" >/dev/null 2>&1; then
  printf 'sandbox read real home secret\n' >&2
  exit 46
fi
[[ "${FACTORY_KIT_TEST_SUITE_SLEEP_SECONDS:-0}" == "0" ]] ||
  sleep "$FACTORY_KIT_TEST_SUITE_SLEEP_SECONDS"
if [[ "${FACTORY_KIT_TEST_SUITE_FAIL:-0}" != "0" ]]; then
  printf 'fixture suite failed\n' >&2
  exit 47
fi
printf 'fixture suite passed\n'
EOF
cat > "$KIT_REPO/scripts/repo-check" <<'EOF'
#!/usr/bin/env bash
set -eu
[[ "${1:-}" == "--root" && -d "${2:-}" ]]
if [[ -n "${FACTORY_KIT_SANDBOX_DENY_SIBLING:-}" ]] &&
   /bin/cat "$FACTORY_KIT_SANDBOX_DENY_SIBLING" >/dev/null 2>&1; then
  echo "sandbox read sibling secret" >&2
  exit 45
fi
if [[ -n "${FACTORY_KIT_SANDBOX_DENY_HOME:-}" ]] &&
   /bin/cat "$FACTORY_KIT_SANDBOX_DENY_HOME" >/dev/null 2>&1; then
  echo "sandbox read real home secret" >&2
  exit 46
fi
[[ "${FACTORY_FIXTURE_DIRTY:-0}" != "1" ]] || printf 'mutated by smoke\n' > payload.txt
[[ "${FACTORY_KIT_TEST_SUITE_SLEEP_SECONDS:-0}" == "0" ]] ||
  sleep "$FACTORY_KIT_TEST_SUITE_SLEEP_SECONDS"
[[ "${FACTORY_KIT_TEST_SUITE_FAIL:-0}" == "0" ]] || exit 47
EOF
cat > "$KIT_REPO/scripts/secret-scan" <<'EOF'
#!/usr/bin/env bash
set -eu
test -x .context/tools/gitleaks/8.30.1/gitleaks
exit 0
EOF
chmod +x "$KIT_REPO/ci/test-all.sh" "$KIT_REPO/scripts/repo-check" \
  "$KIT_REPO/scripts/secret-scan"
printf '*.out export-ignore\n' > "$KIT_REPO/.gitattributes"
printf '*.out\n' > "$KIT_REPO/.gitignore"
printf 'tracked release evidence\n' > "$KIT_REPO/tracked.out"
git -C "$KIT_REPO" add -f tracked.out
printf 'release-a\n' > "$KIT_REPO/payload.txt"
commit_all "$KIT_REPO" "release a"
SHA_A="$(git -C "$KIT_REPO" rev-parse HEAD)"
git -C "$KIT_REPO" push -qu origin main
printf 'release-b\n' > "$KIT_REPO/payload.txt"
commit_all "$KIT_REPO" "release b"
SHA_B="$(git -C "$KIT_REPO" rev-parse HEAD)"
push_main "$KIT_REPO"
printf 'unmerged\n' > "$KIT_REPO/unmerged.txt"
commit_all "$KIT_REPO" "unmerged candidate"
UNMERGED_SHA="$(git -C "$KIT_REPO" rev-parse HEAD)"

# Every kit mutation route shares the same pre-state ticket boundary.
malformed_index=0
for malformed_ticket in \
  '../T-1' 'T-1 ' ' T-1' 'T‐1' 'T-١' $'T-1\n'; do
  malformed_index=$((malformed_index + 1))
  expect_failure "malformed ticket $malformed_index is rejected before state" \
    operator ready --project alpha --product "$TMP/missing-product" \
    --ticket "$malformed_ticket"
  [[ "$LAST_OUTPUT" == *"invalid ticket identifier"* ]] ||
    fail "malformed ticket reports the shared boundary" "$LAST_OUTPUT"
  [[ ! -e "$STATE" ]] ||
    fail "malformed ticket created Factory state"
done
expect_failure "malformed release ticket workdir is rejected before state" \
  release setup --ticket-workdir '../T-1' "$TMP/missing-worktree"
[[ "$LAST_OUTPUT" == *"invalid ticket identifier"* && ! -e "$STATE" ]] ||
  fail "release ticket workdir reports the shared boundary" "$LAST_OUTPUT"

# The installer accepts any clean checkout of the canonical repository, not
# only one whose local branches already contain the fetched origin/main tip.
REMOTE_ONLY_REPO="$TMP/remote-only-source"
git clone -q --branch main "$CANONICAL" "$REMOTE_ONLY_REPO"
printf 'remote-only release\n' > "$REMOTE_ONLY_REPO/remote-only.txt"
commit_all "$REMOTE_ONLY_REPO" "remote-only release"
REMOTE_ONLY_SHA="$(git -C "$REMOTE_ONLY_REPO" rev-parse HEAD)"
push_main "$REMOTE_ONLY_REPO"
git -C "$KIT_REPO" fetch -q origin main
if git -C "$KIT_REPO" for-each-ref --format='%(refname)' --contains "$REMOTE_ONLY_SHA" refs/heads/ |
   grep -q .; then
  fail "remote-only install fixture is absent from every local branch"
else
  pass "remote-only install fixture is absent from every local branch"
fi

# Every managed root/component rejects symlink traversal before state reads.
SYMLINK_TARGET="$TMP/symlink-target"
mkdir "$SYMLINK_TARGET"
RAW_LINK="$TMP/raw-kits-link"
ln -s "$SYMLINK_TARGET" "$RAW_LINK"
RAW_HOME="$TMP/raw-home"
mkdir -m 700 "$RAW_HOME" "$RAW_HOME/.factory"
RAW_STATE="$RAW_HOME/.factory/kits"
ln -s "$SYMLINK_TARGET" "$RAW_STATE"
if run_kit_with_state "$RAW_STATE" status --project alpha >/dev/null 2>&1; then
  fail "raw state root symlink is rejected"
else
  pass "raw state root symlink is rejected"
fi
for component in releases manifests receipts projects; do
  SYM_HOME="$TMP/sym-$component-home"
  mkdir -m 700 "$SYM_HOME"
  mkdir "$SYM_HOME/.factory"
  SYM_STATE="$SYM_HOME/.factory/kits"
  mkdir -p "$SYM_STATE"
  ln -s "$SYMLINK_TARGET" "$SYM_STATE/$component"
  if run_kit_with_state "$SYM_STATE" status --project alpha >/dev/null 2>&1; then
    fail "$component managed symlink is rejected"
  else
    pass "$component managed symlink is rejected"
  fi
done
SYM_PROJECT_HOME="$TMP/sym-project-home"
mkdir -m 700 "$SYM_PROJECT_HOME"
mkdir "$SYM_PROJECT_HOME/.factory"
SYM_PROJECT_STATE="$SYM_PROJECT_HOME/.factory/kits"
mkdir -p "$SYM_PROJECT_STATE/projects"
ln -s "$SYMLINK_TARGET" "$SYM_PROJECT_STATE/projects/alpha"
if run_kit_with_state "$SYM_PROJECT_STATE" status --project alpha >/dev/null 2>&1; then
  fail "per-project symlink is rejected"
else
  pass "per-project symlink is rejected"
fi

expect_failure "abbreviated SHA is rejected" \
  install --repo "$KIT_REPO" --sha "${SHA_A:0:12}"
expect_failure "SHA outside origin/main is rejected" \
  install --repo "$KIT_REPO" --sha "$UNMERGED_SHA"

OTHER_BARE="$TMP/other.git"
OTHER_REPO="$TMP/other-source"
git init --bare -q "$OTHER_BARE"
git init -q -b main "$OTHER_REPO"
git -C "$OTHER_REPO" remote add origin "$OTHER_BARE"
printf 'wrong origin\n' > "$OTHER_REPO/payload"
commit_all "$OTHER_REPO" "wrong origin"
OTHER_SHA="$(git -C "$OTHER_REPO" rev-parse HEAD)"
git -C "$OTHER_REPO" push -qu origin main
expect_failure "wrong canonical origin is rejected" \
  install --repo "$OTHER_REPO" --sha "$OTHER_SHA"

if [[ "${FACTORY_KIT_OUTER_SANDBOX:-0}" == "1" ]]; then
  pass "real Seatbelt denial probe is covered by the enclosing release sandbox"
elif [[ "$(uname -s)" == "Darwin" && -x /usr/bin/sandbox-exec ]]; then
  SIBLING_SANDBOX_SECRET="$TMP/sibling-sandbox-secret"
  REAL_HOME_SANDBOX_SECRET="$HOME/.factory-kit-sandbox-secret.$$"
  printf 'sibling-secret-must-not-be-readable\n' > "$SIBLING_SANDBOX_SECRET"
  printf 'home-secret-must-not-be-readable\n' > "$REAL_HOME_SANDBOX_SECRET"
  export FACTORY_FIXTURE_DIRTY=1
  export FACTORY_KIT_TEST_FORCE_PRODUCTION_SANDBOX=1
  export FACTORY_KIT_SANDBOX_EXEC=/usr/bin/sandbox-exec
  export FACTORY_KIT_SANDBOX_DENY_SIBLING="$SIBLING_SANDBOX_SECRET"
  export FACTORY_KIT_SANDBOX_DENY_HOME="$REAL_HOME_SANDBOX_SECRET"
  if REAL_SANDBOX_OUTPUT="$(run_kit install --repo "$KIT_REPO" --sha "$SHA_A" 2>&1)"; then
    fail "real Seatbelt denies sibling and home secrets" "install unexpectedly succeeded"
  elif [[ "$REAL_SANDBOX_OUTPUT" == *"tracked candidate tree"* &&
          "$REAL_SANDBOX_OUTPUT" != *"sandbox read"* ]]; then
    pass "real Seatbelt denies sibling and home secrets"
  else
    fail "real Seatbelt denies sibling and home secrets" "$REAL_SANDBOX_OUTPUT"
  fi
  unset FACTORY_FIXTURE_DIRTY
  unset FACTORY_KIT_TEST_FORCE_PRODUCTION_SANDBOX
  unset FACTORY_KIT_SANDBOX_EXEC
  unset FACTORY_KIT_SANDBOX_DENY_SIBLING
  unset FACTORY_KIT_SANDBOX_DENY_HOME
  rm -f "$REAL_HOME_SANDBOX_SECRET"
  REAL_HOME_SANDBOX_SECRET=""
else
  pass "real Seatbelt denial probe skipped when unavailable"
fi

SANDBOX_CAPTURE="$TMP/install-sandbox.profile"
cat > "$TMP/fake-sandbox-exec" <<'EOF'
#!/usr/bin/env bash
set -eu
[[ "$1" == "-f" ]]
if [[ "${FACTORY_KIT_FAKE_SANDBOX_ACTIVE:-0}" == "1" ]]; then
  echo "sandbox-exec: sandbox_apply: Operation not permitted" >&2
  exit 71
fi
export FACTORY_KIT_FAKE_SANDBOX_ACTIVE=1
cp "$2" "${FACTORY_KIT_SANDBOX_CAPTURE:?}"
if grep -qx '(allow network-outbound)' "$2"; then
  printf 'allow\n' >> "${FACTORY_KIT_SANDBOX_CAPTURE}.network"
else
  printf 'deny\n' >> "${FACTORY_KIT_SANDBOX_CAPTURE}.network"
fi
printf '%s\n' "$PATH" > "${FACTORY_KIT_SANDBOX_CAPTURE}.path"
command -v git > "${FACTORY_KIT_SANDBOX_CAPTURE}.git"
readlink "$(command -v git)" > "${FACTORY_KIT_SANDBOX_CAPTURE}.git-target" 2>/dev/null ||
  : > "${FACTORY_KIT_SANDBOX_CAPTURE}.git-target"
command -v python3 > "${FACTORY_KIT_SANDBOX_CAPTURE}.python"
readlink "$(command -v python3)" > "${FACTORY_KIT_SANDBOX_CAPTURE}.python-target" 2>/dev/null ||
  : > "${FACTORY_KIT_SANDBOX_CAPTURE}.python-target"
command -v ps > "${FACTORY_KIT_SANDBOX_CAPTURE}.ps"
printf '%s\n' "${DEVELOPER_DIR:-}" > "${FACTORY_KIT_SANDBOX_CAPTURE}.developer-dir"
printf '%s\n' "${GIT_EXEC_PATH:-}" > "${FACTORY_KIT_SANDBOX_CAPTURE}.git-exec-path"
printf '%s\n' "${GIT_TEMPLATE_DIR:-}" > "${FACTORY_KIT_SANDBOX_CAPTURE}.git-template-dir"
shift 2
exec "$@"
EOF
chmod +x "$TMP/fake-sandbox-exec"
export FACTORY_FIXTURE_DIRTY=1
export FACTORY_KIT_TEST_FORCE_PRODUCTION_SANDBOX=1
export FACTORY_KIT_SANDBOX_EXEC="$TMP/fake-sandbox-exec"
export FACTORY_KIT_SANDBOX_CAPTURE="$SANDBOX_CAPTURE"
expect_failure "sandboxed tracked test mutation blocks install" \
  install --repo "$KIT_REPO" --sha "$SHA_A"
unset FACTORY_FIXTURE_DIRTY
unset FACTORY_KIT_TEST_FORCE_PRODUCTION_SANDBOX
unset FACTORY_KIT_SANDBOX_EXEC
unset FACTORY_KIT_SANDBOX_CAPTURE
DEVELOPER_PATH_OK=1
if [[ "$(uname -s)" == "Darwin" ]]; then
  DEVELOPER_ROOT="$(xcode-select -p 2>/dev/null || true)"
  if [[ -x "$DEVELOPER_ROOT/usr/bin/git" ]]; then
    SANDBOX_GIT="$(<"${SANDBOX_CAPTURE}.git")"
    SANDBOX_GIT_TARGET="$(<"${SANDBOX_CAPTURE}.git-target")"
    SANDBOX_PYTHON="$(<"${SANDBOX_CAPTURE}.python")"
    SANDBOX_PYTHON_TARGET="$(<"${SANDBOX_CAPTURE}.python-target")"
    SANDBOX_PS="$(<"${SANDBOX_CAPTURE}.ps")"
    SANDBOX_DEVELOPER_DIR="$(<"${SANDBOX_CAPTURE}.developer-dir")"
    SANDBOX_GIT_EXEC_PATH="$(<"${SANDBOX_CAPTURE}.git-exec-path")"
    SANDBOX_GIT_TEMPLATE_DIR="$(<"${SANDBOX_CAPTURE}.git-template-dir")"
    if [[ "$SANDBOX_GIT" != */factory-tools/git ||
          "$SANDBOX_GIT_TARGET" != "$DEVELOPER_ROOT/usr/bin/git" ||
          "$SANDBOX_PYTHON" != */factory-tools/python3 ||
          "$SANDBOX_PYTHON_TARGET" != "$DEVELOPER_ROOT/usr/bin/python3" ||
          "$SANDBOX_PS" != */factory-tools/ps ||
          "$SANDBOX_DEVELOPER_DIR" != "$DEVELOPER_ROOT" ||
          "$SANDBOX_GIT_EXEC_PATH" != "$DEVELOPER_ROOT/usr/libexec/git-core" ||
          "$SANDBOX_GIT_TEMPLATE_DIR" != "$DEVELOPER_ROOT/usr/share/git-core/templates" ]]; then
      DEVELOPER_PATH_OK=0
    fi
  fi
fi
if [[ -f "$SANDBOX_CAPTURE" ]] &&
   [[ "$DEVELOPER_PATH_OK" == "1" ]] &&
   grep -q '^(deny default)' "$SANDBOX_CAPTURE" &&
   ! grep -qx '(allow file-read\*)' "$SANDBOX_CAPTURE" &&
   ! grep -qx '(allow network\*)' "$SANDBOX_CAPTURE" &&
   ! grep -qx '(allow network-outbound)' "$SANDBOX_CAPTURE" &&
   grep -Fqx '(allow network-bind (local ip "localhost:*"))' "$SANDBOX_CAPTURE" &&
   grep -Fqx '(allow network-inbound (local ip "localhost:*"))' "$SANDBOX_CAPTURE" &&
   grep -Fqx '(allow network-outbound (remote ip "localhost:*"))' "$SANDBOX_CAPTURE" &&
   ! grep -qx '(allow process\*)' "$SANDBOX_CAPTURE" &&
   grep -q '^(allow process-fork)' "$SANDBOX_CAPTURE" &&
   grep -Fqx '(allow process-info* (target same-sandbox))' "$SANDBOX_CAPTURE" &&
   grep -Fqx '(allow signal (target same-sandbox))' "$SANDBOX_CAPTURE" &&
   ! grep -Fqx '(allow signal (target others))' "$SANDBOX_CAPTURE" &&
   grep -Fqx '(allow file-read* (subpath "/dev/fd"))' "$SANDBOX_CAPTURE" &&
   grep -Fqx '(allow file-write* (subpath "/dev/fd"))' "$SANDBOX_CAPTURE" &&
   grep -q 'allow file-read.*"/System"' "$SANDBOX_CAPTURE" &&
   grep -q 'allow file-read.*"/etc"' "$SANDBOX_CAPTURE" &&
   grep -q 'allow file-read.*"/var/select"' "$SANDBOX_CAPTURE" &&
   grep -q 'allow file-read.*"/private/var/select"' "$SANDBOX_CAPTURE" &&
   grep -q "$KIT_REPO" "$SANDBOX_CAPTURE" &&
   grep -q 'allow file-write.*factory-kit-install' "$SANDBOX_CAPTURE"; then
  pass "install sandbox is filesystem and network default-deny"
else
  fail "install sandbox is filesystem and network default-deny"
fi

export FACTORY_KIT_TEST_FORCE_PRODUCTION_SANDBOX=1
export FACTORY_KIT_SANDBOX_EXEC="$TMP/fake-sandbox-exec"
export FACTORY_KIT_CERTIFICATION_NETWORK_REVIEWED=1
expect_failure "install cannot use certification network opt-in" \
  install --repo "$KIT_REPO" --sha "$SHA_A"
unset FACTORY_KIT_TEST_FORCE_PRODUCTION_SANDBOX
unset FACTORY_KIT_SANDBOX_EXEC
unset FACTORY_KIT_CERTIFICATION_NETWORK_REVIEWED

export FACTORY_KIT_TEST_FORCE_PRODUCTION_SANDBOX=1
export FACTORY_KIT_SANDBOX_EXEC="$TMP/missing-sandbox-exec"
expect_failure "production install fails closed without sandbox" \
  install --repo "$KIT_REPO" --sha "$SHA_A"
unset FACTORY_KIT_TEST_FORCE_PRODUCTION_SANDBOX
unset FACTORY_KIT_SANDBOX_EXEC

expect_success "exact ancestor installs with policy discovery" \
  install --repo "$KIT_REPO" --sha "$SHA_A"
expect_success "fetched origin/main commit installs from a non-main source checkout" \
  install --repo "$KIT_REPO" --sha "$REMOTE_ONLY_SHA"
if [[ "$(<"$STATE/releases/$REMOTE_ONLY_SHA/remote-only.txt")" == "remote-only release" ]]; then
  pass "remote-only origin/main tree is materialized exactly"
else
  fail "remote-only origin/main tree is materialized exactly"
fi
if [[ "$(<"$STATE/releases/$SHA_A/tracked.out")" == "tracked release evidence" ]]; then
  pass "tracked export-ignored files remain in exact release tree"
else
  fail "tracked export-ignored files remain in exact release tree"
fi
if grep -q -- '--paginate --slurp repos/nysa-company/software-factory/rulesets' "$GH_TRACE" &&
   grep -q 'branches/main/protection/required_status_checks' "$GH_TRACE" &&
   grep -q 'rulesets/101' "$GH_TRACE" &&
   grep -q 'rulesets/102' "$GH_TRACE" &&
   grep -q 'check-runs' "$GH_TRACE" &&
   grep -q 'statuses' "$GH_TRACE"; then
  pass "GitHub classic and applicable Rulesets are paginated and evaluated"
else
  fail "GitHub classic and applicable Rulesets are paginated and evaluated"
fi

MANIFEST_A="$STATE/manifests/$SHA_A.json"
EVIDENCE_A="$STATE/manifests/$SHA_A.suite.json"
if [[ -f "$MANIFEST_A" && ! -L "$MANIFEST_A" ]] &&
   [[ "$(json_value "$MANIFEST_A" kit_sha)" == "$SHA_A" ]] &&
   [[ "$(json_value "$MANIFEST_A" git_tree)" == "$(git -C "$KIT_REPO" rev-parse "$SHA_A^{tree}")" ]] &&
   [[ "$(json_value "$MANIFEST_A" launcher_sha256)" == "$(shasum -a 256 "$STATE/releases/$SHA_A/scripts/factory-launch" | awk '{print $1}')" ]] &&
   [[ "$(json_value "$MANIFEST_A" sealed_release_path)" == "$STATE/releases/$SHA_A" ]]; then
  pass "trusted external install manifest binds release"
else
  fail "trusted external install manifest binds release"
fi
if [[ -f "$EVIDENCE_A" && ! -L "$EVIDENCE_A" ]] &&
   [[ "$(json_value "$EVIDENCE_A" status)" == "pass" ]] &&
   [[ "$(json_value "$EVIDENCE_A" kit_sha)" == "$SHA_A" ]] &&
   [[ "$(json_value "$EVIDENCE_A" release_tree)" == "$(git -C "$KIT_REPO" rev-parse "$SHA_A^{tree}")" ]] &&
   [[ "$(json_value "$EVIDENCE_A" suite_definition)" == "factory-kit-suite-v2" ]] &&
   [[ "$(json_value "$EVIDENCE_A" verification_source)" == "github-actions-full" ]] &&
   [[ "$(json_value "$EVIDENCE_A" evidence_ttl_seconds)" == "86400" ]]; then
  pass "install publishes bound reusable kit-suite evidence"
else
  fail "install publishes bound reusable kit-suite evidence"
fi

FIRST_SNAPSHOT="$(state_snapshot)"
expect_success "manifest-backed install is idempotent" \
  install --repo "$KIT_REPO" --sha "$SHA_A"
[[ "$FIRST_SNAPSHOT" == "$(state_snapshot)" ]] &&
  pass "idempotent install leaves state unchanged" ||
  fail "idempotent install leaves state unchanged"

chmod u+w "$STATE/releases/$SHA_A/payload.txt"
printf 'tampered\n' > "$STATE/releases/$SHA_A/payload.txt"
expect_failure "release drift is checked against external manifest" \
  install --repo "$KIT_REPO" --sha "$SHA_A"
printf 'release-a\n' > "$STATE/releases/$SHA_A/payload.txt"
chmod -R a-w "$STATE/releases/$SHA_A"

mv "$MANIFEST_A" "$TMP/manifest-a.saved"
expect_failure "release without manifest is a partial install" \
  install --repo "$KIT_REPO" --sha "$SHA_A"
mv "$TMP/manifest-a.saved" "$MANIFEST_A"

export GH_FAIL_APP=1
expect_failure "app-bound check rejects successful status and wrong app" \
  install --repo "$KIT_REPO" --sha "$SHA_B"
unset GH_FAIL_APP
export GH_UNSAFE_BYPASS=1
expect_failure "unsafe Ruleset bypass actor blocks install" \
  install --repo "$KIT_REPO" --sha "$SHA_B"
unset GH_UNSAFE_BYPASS
export GH_NO_PULL_REQUEST=1
expect_failure "Ruleset without pull request rule blocks install" \
  install --repo "$KIT_REPO" --sha "$SHA_B"
unset GH_NO_PULL_REQUEST
export GH_UNBOUND_RULESET_CHECK=1
expect_failure "unbound Ruleset check blocks install" \
  install --repo "$KIT_REPO" --sha "$SHA_B"
unset GH_UNBOUND_RULESET_CHECK
export GH_NO_APPLICABLE_RULESET=1
expect_failure "main requires an applicable active Ruleset" \
  install --repo "$KIT_REPO" --sha "$SHA_B"
unset GH_NO_APPLICABLE_RULESET

mkdir -p "$STATE/releases/$SHA_B"
printf 'partial\n' > "$STATE/releases/$SHA_B/partial"
expect_failure "partial release without manifest is rejected" \
  install --repo "$KIT_REPO" --sha "$SHA_B"
chmod -R u+w "$STATE/releases/$SHA_B"
rm -rf "$STATE/releases/$SHA_B"

FOREIGN_QUARANTINE="$TMP/owned-sealed-release"
export FACTORY_KIT_TEST_FAIL_PUBLISH_PHASE=contents_sealed
export FACTORY_KIT_TEST_REPLACE_TEMP_BEFORE_CLEANUP="$FOREIGN_QUARANTINE"
expect_failure "cleanup refuses a replaced remembered temp path" \
  install --repo "$KIT_REPO" --sha "$SHA_B"
unset FACTORY_KIT_TEST_FAIL_PUBLISH_PHASE
unset FACTORY_KIT_TEST_REPLACE_TEMP_BEFORE_CLEANUP
REPLACEMENT_TEMP="$(compgen -G "$STATE/releases/.install-$SHA_B-*" | awk 'NR == 1 {print}')"
if [[ -n "$REPLACEMENT_TEMP" &&
      -f "$REPLACEMENT_TEMP/foreign-marker" &&
      -d "$FOREIGN_QUARANTINE" ]] &&
   python3 - "$REPLACEMENT_TEMP" <<'PY'
import pathlib, stat, sys
raise SystemExit(
    0 if not stat.S_IMODE(pathlib.Path(sys.argv[1]).lstat().st_mode) & stat.S_IWUSR else 1
)
PY
then
  pass "cleanup leaves inode-mismatched paths untouched"
else
  fail "cleanup leaves inode-mismatched paths untouched"
fi
chmod -R u+w "$REPLACEMENT_TEMP" "$FOREIGN_QUARANTINE"
rm -rf "$REPLACEMENT_TEMP" "$FOREIGN_QUARANTINE"

export FACTORY_KIT_TEST_FAIL_PUBLISH_PHASE=contents_sealed
expect_failure "cleanup removes an owned sealed staging tree" \
  install --repo "$KIT_REPO" --sha "$SHA_B"
unset FACTORY_KIT_TEST_FAIL_PUBLISH_PHASE
if [[ ! -e "$STATE/releases/$SHA_B" &&
      ! -e "$STATE/manifests/$SHA_B.json" ]] &&
   ! compgen -G "$STATE/releases/.install-$SHA_B-*" >/dev/null; then
  pass "owned sealed staging cleanup restores an empty publication slot"
else
  fail "owned sealed staging cleanup restores an empty publication slot"
fi

export FACTORY_KIT_TEST_FAIL_PUBLISH_PHASE=release_verified
expect_failure "untrusted renamed release is cleaned before manifest" \
  install --repo "$KIT_REPO" --sha "$SHA_B"
unset FACTORY_KIT_TEST_FAIL_PUBLISH_PHASE
if [[ ! -e "$STATE/releases/$SHA_B" &&
      ! -e "$STATE/manifests/$SHA_B.json" ]] &&
   ! compgen -G "$STATE/releases/.install-$SHA_B-*" >/dev/null; then
  pass "pre-manifest publication failure has no trusted visibility"
else
  fail "pre-manifest publication failure has no trusted visibility"
fi

PUBLISH_TRACE="$TMP/publish-order.jsonl"
export FACTORY_KIT_TEST_PUBLISH_TRACE="$PUBLISH_TRACE"
export FACTORY_KIT_TEST_REMOTE_FULL_CI=0
expect_failure "install refuses missing remote CI evidence without running local full" \
  install --repo "$KIT_REPO" --sha "$SHA_B"
if [[ "$LAST_OUTPUT" == *"exact successful main GitHub CI evidence is required"* &&
      "$LAST_OUTPUT" != *"fixture suite failed"* ]]; then
  pass "missing install evidence fails before local suite execution"
else
  fail "missing install evidence fails before local suite execution" "$LAST_OUTPUT"
fi
export FACTORY_KIT_TEST_REMOTE_FULL_CI=1
expect_success "second exact release publishes portably" \
  install --repo "$KIT_REPO" --sha "$SHA_B"
unset FACTORY_KIT_TEST_PUBLISH_TRACE
unset FACTORY_KIT_TEST_REMOTE_FULL_CI
if [[ "$(json_value "$STATE/manifests/$SHA_B.suite.json" verification_source)" == "github-actions-full" ]] &&
   [[ "$(json_value "$STATE/manifests/$SHA_B.suite.json" remote_evidence_id)" =~ ^[0-9a-f]{64}$ ]]; then
  pass "verified remote full CI replaces only the local full suite"
else
  fail "verified remote full CI replaces only the local full suite"
fi
if python3 - "$PUBLISH_TRACE" <<'PY'
import json, pathlib, sys
rows = [json.loads(line) for line in pathlib.Path(sys.argv[1]).read_text().splitlines()]
expected = [
    "contents_sealed",
    "renamed_root_sealed",
    "parent_fsynced",
    "release_verified",
    "manifest_written",
]
assert [row["phase"] for row in rows] == expected
assert rows[0]["root_owner_writable"]
assert rows[0]["root_any_writable"]
assert rows[0]["writable_descendants"] == 0
assert not rows[0]["release_exists"]
assert not rows[0]["manifest_exists"]
for row in rows[1:4]:
    assert not row["root_any_writable"]
    assert row["writable_descendants"] == 0
    assert row["release_exists"]
    assert not row["manifest_exists"]
assert not rows[4]["root_any_writable"]
assert rows[4]["release_exists"]
assert rows[4]["manifest_exists"]
PY
then
  pass "publication seals contents before rename and manifests last"
else
  fail "publication seals contents before rename and manifests last"
fi

preflight_setup_blocked_json() {
  python3 - "$1" <<'PY'
import json, sys
raw = sys.argv[1]
assert len(raw.splitlines()) == 1
value = json.loads(raw)
assert set(value) == {
    "authorizations_required", "blockers", "certification", "factory",
    "ownership_conflicts", "product", "project", "schema", "status", "tickets",
}
assert value["schema"] == "nysa.software-factory.operator-preflight-report/v1"
assert value["status"] == "blocked"
assert value["blockers"] == [{
    "reason_code": "preflight_setup_invalid", "scope": "preflight",
}]
PY
}

PRODUCT_PREFLIGHT="$(make_product product-preflight)"
set_pin "$PRODUCT_PREFLIGHT" "$SHA_A"
mkdir -p "$PRODUCT_PREFLIGHT/app/tests" "$PRODUCT_PREFLIGHT/factory/initiatives"
printf '%s\n' '# I-001' 'Status: planned' \
  > "$PRODUCT_PREFLIGHT/factory/initiatives/I-001.md"
printf 'fixture\n' > "$PRODUCT_PREFLIGHT/app/tests/one.test.js"
printf 'fixture\n' > "$PRODUCT_PREFLIGHT/app/tests/two.test.js"
printf 'fixture\n' > "$PRODUCT_PREFLIGHT/app/tests/three.test.js"
printf 'State: Backlog\n' > "$PRODUCT_PREFLIGHT/factory/tickets/T-004.md"
cat > "$PRODUCT_PREFLIGHT/factory/tickets/T-001.md" <<'EOF'
State: Ready
Initiative: I-001
Priority: normal
Depends-On: none
Product-Decisions: frozen
Builder ownership: app/one.js only
Fixture-Seams: app/tests/one.test.js
Authentication-Seams: factory/certify.sh
Protected-Test-Conflicts: none
EOF
cat > "$PRODUCT_PREFLIGHT/factory/tickets/T-002.md" <<'EOF'
State: Ready
Initiative: I-001
Priority: normal
Depends-On: none
Product-Decisions: frozen
Builder ownership: app/two.js only
Fixture-Seams: app/tests/two.test.js
Authentication-Seams: factory/certify.sh
Protected-Test-Conflicts: none
EOF
cat > "$PRODUCT_PREFLIGHT/factory/tickets/T-003.md" <<'EOF'
State: Backlog
Initiative: I-001
Priority: normal
Depends-On: none
Product-Decisions: frozen
Builder ownership: app/three.js only
Fixture-Seams: app/tests/three.test.js
Authentication-Seams: factory/certify.sh
Protected-Test-Conflicts: none
EOF
python3 - "$PRODUCT_PREFLIGHT/factory/certification-plan.json" <<'PY'
import json, pathlib, sys
path = pathlib.Path(sys.argv[1])
value = json.loads(path.read_text())
value["phases"][0]["network"] = "required"
path.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")
PY
commit_all "$PRODUCT_PREFLIGHT" "prepare operator preflight fixture"
push_main "$PRODUCT_PREFLIGHT"
PREFLIGHT_BAD_MANIFEST_OUTPUT="$(run_kit preflight-report --project preflight \
  --product "$PRODUCT_PREFLIGHT" --sha "$(printf 'c%.0s' {1..40})" \
  --ticket T-001 --json 2>&1)"
PREFLIGHT_BAD_MANIFEST_STATUS=$?
if [[ "$PREFLIGHT_BAD_MANIFEST_STATUS" -eq 2 ]] &&
   preflight_setup_blocked_json "$PREFLIGHT_BAD_MANIFEST_OUTPUT"; then
  pass "preflight report closes invalid manifest failures as blocked JSON"
else
  fail "preflight report closes invalid manifest failures as blocked JSON" \
    "$PREFLIGHT_BAD_MANIFEST_OUTPUT"
fi
printf '%s\n' invalid > "$PRODUCT_PREFLIGHT/factory/KIT_PIN"
PREFLIGHT_BAD_PIN_OUTPUT="$(run_kit preflight-report --project preflight \
  --product "$PRODUCT_PREFLIGHT" --sha "$SHA_A" --ticket T-001 --json 2>&1)"
PREFLIGHT_BAD_PIN_STATUS=$?
printf '%s\n' "$SHA_A" > "$PRODUCT_PREFLIGHT/factory/KIT_PIN"
if [[ "$PREFLIGHT_BAD_PIN_STATUS" -eq 2 ]] &&
   preflight_setup_blocked_json "$PREFLIGHT_BAD_PIN_OUTPUT"; then
  pass "preflight report closes invalid pin failures as blocked JSON"
else
  fail "preflight report closes invalid pin failures as blocked JSON" \
    "$PREFLIGHT_BAD_PIN_OUTPUT"
fi
cp "$PRODUCT_PREFLIGHT/factory/PROJECT.env" "$TMP/preflight-project-env"
awk '!/^CERTIFY_SCRIPT=/' "$TMP/preflight-project-env" > \
  "$PRODUCT_PREFLIGHT/factory/PROJECT.env"
PREFLIGHT_BAD_CERTIFY_OUTPUT="$(run_kit preflight-report --project preflight \
  --product "$PRODUCT_PREFLIGHT" --sha "$SHA_A" --ticket T-001 --json 2>&1)"
PREFLIGHT_BAD_CERTIFY_STATUS=$?
mv "$TMP/preflight-project-env" "$PRODUCT_PREFLIGHT/factory/PROJECT.env"
if [[ "$PREFLIGHT_BAD_CERTIFY_STATUS" -eq 2 ]] &&
   preflight_setup_blocked_json "$PREFLIGHT_BAD_CERTIFY_OUTPUT"; then
  pass "preflight report closes missing CERTIFY_SCRIPT as blocked JSON"
else
  fail "preflight report closes missing CERTIFY_SCRIPT as blocked JSON" \
    "$PREFLIGHT_BAD_CERTIFY_OUTPUT"
fi
PREFLIGHT_BAD_SETUP_OUTPUT="$(run_kit preflight-report --project preflight \
  --product "$TMP/missing-preflight-product" --sha "$SHA_A" \
  --ticket T-001 --json 2>&1)"
PREFLIGHT_BAD_SETUP_STATUS=$?
if [[ "$PREFLIGHT_BAD_SETUP_STATUS" -eq 2 ]] &&
   preflight_setup_blocked_json "$PREFLIGHT_BAD_SETUP_OUTPUT"; then
  pass "preflight report closes reporter setup failures as blocked JSON"
else
  fail "preflight report closes reporter setup failures as blocked JSON" \
    "$PREFLIGHT_BAD_SETUP_OUTPUT"
fi
PREFLIGHT_STATE_BEFORE="$(state_snapshot)"
PREFLIGHT_HEAD_BEFORE="$(git -C "$PRODUCT_PREFLIGHT" rev-parse HEAD HEAD^{tree})"
PREFLIGHT_AUTH_OUTPUT="$(run_kit preflight-report --project preflight \
  --product "$PRODUCT_PREFLIGHT" --sha "$SHA_A" \
  --ticket T-001 --ticket T-002 --json 2>&1)"
PREFLIGHT_AUTH_STATUS=$?
if [[ "$PREFLIGHT_AUTH_STATUS" -eq 3 ]] &&
   python3 - "$PREFLIGHT_AUTH_OUTPUT" <<'PY'
import json, sys
value = json.loads(sys.argv[1])
assert value["schema"] == "nysa.software-factory.operator-preflight-report/v1"
assert value["status"] == "authorization-required"
assert value["blockers"] == []
assert value["authorizations_required"] == ["certification_network_review"]
assert value["certification"]["runtime"]["status"] == "pass"
assert value["product"]["head_equals_remote_main"] is True
assert value["product"]["identity_stable"] is True
assert value["product"]["kit_pin"] == value["factory"]["sha"]
assert all(item["status"] == "pass" for item in value["tickets"])
assert all(item["state_ready"] is True for item in value["tickets"])
assert value["ownership_conflicts"] == []
PY
then
  pass "preflight report requests reviewed network before certification"
else
  fail "preflight report requests reviewed network before certification" \
    "$PREFLIGHT_AUTH_OUTPUT"
fi
printf '%s\n' '#!/usr/bin/env bash' 'printf "%s\n" v99.99.99' > "$STUB_BIN/node"
printf '%s\n' '#!/usr/bin/env bash' 'printf "%s\n" 99.99.99' > "$STUB_BIN/npm"
chmod +x "$STUB_BIN/node" "$STUB_BIN/npm"
export FACTORY_KIT_CERTIFICATION_NETWORK_REVIEWED=1
PREFLIGHT_RUNTIME_OUTPUT="$(run_kit preflight-report --project preflight \
  --product "$PRODUCT_PREFLIGHT" --sha "$SHA_A" --ticket T-001 --json 2>&1)"
PREFLIGHT_RUNTIME_STATUS=$?
unset FACTORY_KIT_CERTIFICATION_NETWORK_REVIEWED
rm -f "$STUB_BIN/node" "$STUB_BIN/npm"
if [[ "$PREFLIGHT_RUNTIME_STATUS" -eq 2 ]] &&
   python3 - "$PREFLIGHT_RUNTIME_OUTPUT" <<'PY'
import json, sys
value = json.loads(sys.argv[1])
assert value["status"] == "blocked"
assert value["certification"]["runtime"]["status"] == "blocked"
assert value["certification"]["runtime"]["observed"] == {
    "node": "v99.99.99", "npm": "99.99.99",
}
assert value["blockers"] == [{
    "reason_code": "runtime_tuple_mismatch", "scope": "certification",
}]
PY
then
  pass "preflight report blocks the wrong Node/npm PATH tuple"
else
  fail "preflight report blocks the wrong Node/npm PATH tuple" \
    "$PREFLIGHT_RUNTIME_OUTPUT"
fi
export FACTORY_KIT_CERTIFICATION_NETWORK_REVIEWED=1
PREFLIGHT_PASS_OUTPUT="$(run_kit preflight-report --project preflight \
  --product "$PRODUCT_PREFLIGHT" --sha "$SHA_A" \
  --ticket T-001 --ticket T-002 --json 2>&1)"
PREFLIGHT_PASS_STATUS=$?
unset FACTORY_KIT_CERTIFICATION_NETWORK_REVIEWED
if [[ "$PREFLIGHT_PASS_STATUS" -eq 0 ]] &&
   python3 - "$PREFLIGHT_PASS_OUTPUT" "$SHA_A" <<'PY'
import json, sys
value = json.loads(sys.argv[1])
assert value["status"] == "pass"
assert value["factory"]["sha"] == sys.argv[2]
assert value["certification"]["network_review"]["status"] == "pass"
PY
   [[ "$PREFLIGHT_STATE_BEFORE" == "$(state_snapshot)" ]] &&
   [[ "$PREFLIGHT_HEAD_BEFORE" == \
      "$(git -C "$PRODUCT_PREFLIGHT" rev-parse HEAD HEAD^{tree})" ]] &&
   [[ -z "$(git -C "$PRODUCT_PREFLIGHT" status --porcelain --untracked-files=all)" ]]; then
  pass "preflight report passes exact inputs without mutating product or owner state"
else
  fail "preflight report passes exact inputs without mutating product or owner state" \
    "$PREFLIGHT_PASS_OUTPUT"
fi
PREFLIGHT_PUSH_ORIGIN="$(git -C "$PRODUCT_PREFLIGHT" remote get-url --push origin)"
PREFLIGHT_FETCH_ONLY="$TMP/preflight-fetch-only.git"
git init --bare -q "$PREFLIGHT_FETCH_ONLY"
git -C "$PRODUCT_PREFLIGHT" remote set-url origin "$PREFLIGHT_FETCH_ONLY"
git -C "$PRODUCT_PREFLIGHT" remote set-url --push origin "$PREFLIGHT_PUSH_ORIGIN"
export FACTORY_KIT_CERTIFICATION_NETWORK_REVIEWED=1
PREFLIGHT_AUTHORITY_OUTPUT="$(run_kit preflight-report --project preflight \
  --product "$PRODUCT_PREFLIGHT" --sha "$SHA_A" --ticket T-001 --json 2>&1)"
PREFLIGHT_AUTHORITY_STATUS=$?
unset FACTORY_KIT_CERTIFICATION_NETWORK_REVIEWED
if [[ "$PREFLIGHT_AUTHORITY_STATUS" -eq 0 ]] &&
   python3 - "$PREFLIGHT_AUTHORITY_OUTPUT" <<'PY'
import json, sys
value = json.loads(sys.argv[1])
assert value["status"] == "pass"
assert value["product"]["sha"] == value["product"]["remote_main_sha"]
PY
then
  pass "preflight report binds remote-main evidence to validated push authority"
else
  fail "preflight report binds remote-main evidence to validated push authority" \
    "$PREFLIGHT_AUTHORITY_OUTPUT"
fi
git -C "$PRODUCT_PREFLIGHT" remote set-url --push origin \
  https://example.invalid/product.git
export FACTORY_KIT_CERTIFICATION_NETWORK_REVIEWED=1
PREFLIGHT_GITHUB_PUSH_OUTPUT="$(run_kit preflight-report --project preflight \
  --product "$PRODUCT_PREFLIGHT" --sha "$SHA_A" --ticket T-001 --json 2>&1)"
PREFLIGHT_GITHUB_PUSH_STATUS=$?
unset FACTORY_KIT_CERTIFICATION_NETWORK_REVIEWED
git -C "$PRODUCT_PREFLIGHT" remote set-url --push origin "$PREFLIGHT_PUSH_ORIGIN"
if [[ "$PREFLIGHT_GITHUB_PUSH_STATUS" -eq 2 ]] &&
   preflight_setup_blocked_json "$PREFLIGHT_GITHUB_PUSH_OUTPUT"; then
  pass "Factory test mode refuses a GitHub product push origin"
else
  fail "Factory test mode refuses a GitHub product push origin" \
    "$PREFLIGHT_GITHUB_PUSH_OUTPUT"
fi
export FACTORY_KIT_CERTIFICATION_NETWORK_REVIEWED=1
PREFLIGHT_BACKLOG_OUTPUT="$(run_kit preflight-report --project preflight \
  --product "$PRODUCT_PREFLIGHT" --sha "$SHA_A" --ticket T-003 --json 2>&1)"
PREFLIGHT_BACKLOG_STATUS=$?
unset FACTORY_KIT_CERTIFICATION_NETWORK_REVIEWED
if [[ "$PREFLIGHT_BACKLOG_STATUS" -eq 2 ]] &&
   python3 - "$PREFLIGHT_BACKLOG_OUTPUT" <<'PY'
import json, sys
value = json.loads(sys.argv[1])
assert value["status"] == "blocked"
assert value["tickets"] == [{
    "builder_paths": [], "state_ready": False, "status": "blocked",
    "ticket": "T-003",
}]
assert value["blockers"] == [{
    "reason_code": "ticket_readiness_invalid", "scope": "T-003",
}]
PY
then
  pass "preflight report requires selected tickets to be Ready"
else
  fail "preflight report requires selected tickets to be Ready" \
    "$PREFLIGHT_BACKLOG_OUTPUT"
fi
for ticket in T-004 T-005; do
  printf 'State: Done\n' > "$PRODUCT_PREFLIGHT/factory/tickets/$ticket.md"
done
printf 'State: Planning\n' > "$PRODUCT_PREFLIGHT/factory/tickets/T-006.md"
commit_all "$PRODUCT_PREFLIGHT" "add invalid terminal preflight fixtures"
push_main "$PRODUCT_PREFLIGHT"
PREFLIGHT_TERMINAL_STATE_BEFORE="$(state_snapshot)"
export FACTORY_KIT_CERTIFICATION_NETWORK_REVIEWED=1
PREFLIGHT_TERMINAL_OUTPUT="$(run_kit preflight-report --project preflight \
  --product "$PRODUCT_PREFLIGHT" --sha "$SHA_A" --json 2>&1)"
PREFLIGHT_TERMINAL_STATUS=$?
unset FACTORY_KIT_CERTIFICATION_NETWORK_REVIEWED
if [[ "$PREFLIGHT_TERMINAL_STATUS" -eq 2 ]] &&
   python3 - "$PREFLIGHT_TERMINAL_OUTPUT" <<'PY'
import json, sys
value = json.loads(sys.argv[1])
assert value["blockers"] == [
    {"reason_code": "activation_terminal_invalid", "scope": "T-004"},
    {"reason_code": "activation_terminal_invalid", "scope": "T-005"},
    {"reason_code": "activation_ticket_lease_missing", "scope": "T-006"},
]
PY
   [[ "$PREFLIGHT_TERMINAL_STATE_BEFORE" == "$(state_snapshot)" ]] &&
   [[ -z "$(git -C "$PRODUCT_PREFLIGHT" status --porcelain --untracked-files=all)" ]]; then
  pass "preflight report aggregates authoritative activation blockers without owner-state mutation"
else
  fail "preflight report aggregates authoritative activation blockers without owner-state mutation" \
    "$PREFLIGHT_TERMINAL_OUTPUT"
fi
git -C "$PRODUCT_PREFLIGHT" rm -q \
  factory/tickets/T-004.md factory/tickets/T-005.md factory/tickets/T-006.md
commit_all "$PRODUCT_PREFLIGHT" "remove invalid terminal preflight fixtures"
push_main "$PRODUCT_PREFLIGHT"
sed 's|Builder ownership: app/two.js only|Builder ownership: app/one.js only|' \
  "$PRODUCT_PREFLIGHT/factory/tickets/T-002.md" > "$TMP/preflight-conflict-ticket"
mv "$TMP/preflight-conflict-ticket" "$PRODUCT_PREFLIGHT/factory/tickets/T-002.md"
commit_all "$PRODUCT_PREFLIGHT" "create preflight ownership conflict"
push_main "$PRODUCT_PREFLIGHT"
export FACTORY_KIT_CERTIFICATION_NETWORK_REVIEWED=1
PREFLIGHT_CONFLICT_OUTPUT="$(run_kit preflight-report --project preflight \
  --product "$PRODUCT_PREFLIGHT" --sha "$SHA_A" \
  --ticket T-001 --ticket T-002 --json 2>&1)"
PREFLIGHT_CONFLICT_STATUS=$?
unset FACTORY_KIT_CERTIFICATION_NETWORK_REVIEWED
if [[ "$PREFLIGHT_CONFLICT_STATUS" -eq 2 ]] &&
   python3 - "$PREFLIGHT_CONFLICT_OUTPUT" <<'PY'
import json, sys
value = json.loads(sys.argv[1])
assert value["status"] == "blocked"
assert value["ownership_conflicts"] == [
    {"path": "app/one.js", "tickets": ["T-001", "T-002"]}
]
assert {item["reason_code"] for item in value["blockers"]} == {
    "builder_ownership_conflict"
}
PY
then
  pass "preflight report blocks pairwise Builder ownership conflicts"
else
  fail "preflight report blocks pairwise Builder ownership conflicts" \
    "$PREFLIGHT_CONFLICT_OUTPUT"
fi
sed 's|Builder ownership: app/one.js only|Builder ownership: app/two.js only|' \
  "$PRODUCT_PREFLIGHT/factory/tickets/T-002.md" > "$TMP/preflight-restored-ticket"
mv "$TMP/preflight-restored-ticket" "$PRODUCT_PREFLIGHT/factory/tickets/T-002.md"
cat > "$PRODUCT_PREFLIGHT/factory/tickets/T-007.md" <<'EOF'
State: Ready
Initiative: I-001
Priority: normal
Depends-On: none
Product-Decisions: frozen
Builder ownership: app/one.js only
Fixture-Seams: app/tests/one.test.js
Authentication-Seams: factory/certify.sh
Protected-Test-Conflicts: none
EOF
cp "$PRODUCT_PREFLIGHT/factory/tickets/T-007.md" \
  "$PRODUCT_PREFLIGHT/factory/tickets/T-010.md"
commit_all "$PRODUCT_PREFLIGHT" "create protected Ready ownership conflict"
push_main "$PRODUCT_PREFLIGHT"
export FACTORY_KIT_CERTIFICATION_NETWORK_REVIEWED=1
PREFLIGHT_READY_CONFLICT_OUTPUT="$(run_kit preflight-report --project preflight \
  --product "$PRODUCT_PREFLIGHT" --sha "$SHA_A" \
  --ticket T-001 --ticket T-002 --json 2>&1)"
PREFLIGHT_READY_CONFLICT_STATUS=$?
unset FACTORY_KIT_CERTIFICATION_NETWORK_REVIEWED
if [[ "$PREFLIGHT_READY_CONFLICT_STATUS" -eq 2 ]] &&
   python3 - "$PREFLIGHT_READY_CONFLICT_OUTPUT" <<'PY'
import json, sys
value = json.loads(sys.argv[1])
assert value["ownership_conflicts"] == [
    {"path": "app/one.js", "tickets": ["T-001", "T-007", "T-010"]}
]
assert {item["reason_code"] for item in value["blockers"]} == {
    "builder_ownership_conflict"
}
PY
then
  pass "preflight report blocks protected Ready Builder ownership conflicts"
else
  fail "preflight report blocks protected Ready Builder ownership conflicts" \
    "$PREFLIGHT_READY_CONFLICT_OUTPUT"
fi
for ticket in T-007 T-010; do
  sed -i.bak 's/State: Ready/State: Backlog/' \
    "$PRODUCT_PREFLIGHT/factory/tickets/$ticket.md"
  rm "$PRODUCT_PREFLIGHT/factory/tickets/$ticket.md.bak"
done
for ticket in T-008 T-009; do
  cat > "$PRODUCT_PREFLIGHT/factory/tickets/$ticket.md" <<'EOF'
State: Ready
Initiative: I-001
Priority: normal
Depends-On: none
Product-Decisions: frozen
Builder ownership: app/three.js only
Fixture-Seams: app/tests/three.test.js
Authentication-Seams: factory/certify.sh
Protected-Test-Conflicts: none
EOF
done
commit_all "$PRODUCT_PREFLIGHT" "retire protected ownership conflict"
push_main "$PRODUCT_PREFLIGHT"
export FACTORY_KIT_CERTIFICATION_NETWORK_REVIEWED=1
PREFLIGHT_RETIRED_OUTPUT="$(run_kit preflight-report --project preflight \
  --product "$PRODUCT_PREFLIGHT" --sha "$SHA_A" \
  --ticket T-001 --ticket T-002 --json 2>&1)"
PREFLIGHT_RETIRED_STATUS=$?
unset FACTORY_KIT_CERTIFICATION_NETWORK_REVIEWED
if [[ "$PREFLIGHT_RETIRED_STATUS" -eq 0 ]] &&
   python3 - "$PREFLIGHT_RETIRED_OUTPUT" <<'PY'
import json, sys
value = json.loads(sys.argv[1])
assert value["status"] == "pass"
assert value["ownership_conflicts"] == []
PY
then
  pass "preflight report ignores non-Ready and unrelated Ready ownership"
else
  fail "preflight report ignores non-Ready and unrelated Ready ownership" \
    "$PREFLIGHT_RETIRED_OUTPUT"
fi
if python3 - "$ROOT" "$PRODUCT_PREFLIGHT" "$PREFLIGHT_PUSH_ORIGIN" <<'PY'
import importlib.util, pathlib, subprocess, sys
from unittest import mock
root, product, origin = map(pathlib.Path, sys.argv[1:])
spec = importlib.util.spec_from_file_location(
    "operator_preflight_report", root / "scripts/operator-preflight-report.py",
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
real = module.run_git_remote
def mutate(*arguments, **keywords):
    subprocess.run(
        ["/usr/bin/git", "-C", str(product), "commit", "--allow-empty",
         "-qm", "concurrent preflight mutation"], check=True,
    )
    return real(*arguments, **keywords)
with mock.patch.object(module, "run_git_remote", side_effect=mutate):
    snapshot = module.product_snapshot(product, str(origin))
assert snapshot["identity_stable"] is False
assert snapshot["identity_changed"] is True
PY
then
  pass "preflight report blocks a product identity change during evidence reads"
else
  fail "preflight report blocks a product identity change during evidence reads"
fi

PRODUCT_OPTIONAL="$(make_product product-optional)"
set_pin "$PRODUCT_OPTIONAL" "$SHA_A"
node_version="$(node --version)"
npm_version="$(npm --version)"
cat > "$PRODUCT_OPTIONAL/factory/certification-plan.json" <<EOF
{"phases":[{"artifacts":[],"command":["true"],"depends_on":[],"name":"required-check","network":"denied"},{"artifacts":[],"command":["false"],"depends_on":["required-check"],"kind":"test","name":"application-tests","network":"denied","optional":true}],"runtime":{"node":"$node_version","npm":"$npm_version"},"schema":"nysa.software-factory.certification-plan/v2"}
EOF
commit_all "$PRODUCT_OPTIONAL" "declare optional application tests"
push_main "$PRODUCT_OPTIONAL"
expect_success "certification skips only app-declared optional tests" \
  certify --project optional --product "$PRODUCT_OPTIONAL" --sha "$SHA_A" \
  --skip-optional-tests
OPTIONAL_RECEIPT="$(printf '%s\n' "$LAST_OUTPUT" | awk '/^\// {value=$0} END {print value}')"
if python3 - "$OPTIONAL_RECEIPT" <<'PY'
import json, sys
result = json.load(open(sys.argv[1]))["product_certification_evidence"]["result"]
assert result["optional_tests"] == {
    "requested": True, "skipped": ["application-tests"],
}
assert [phase["name"] for phase in result["phases"]] == ["required-check"]
PY
then
  pass "certification receipt records the exact optional test omission"
else
  fail "certification receipt records the exact optional test omission"
fi
OPTIONAL_TAMPER="$TMP/optional-receipt-tamper.json"
python3 - "$OPTIONAL_RECEIPT" "$OPTIONAL_TAMPER" <<'PY'
import hashlib, json, pathlib, sys
source, target = map(pathlib.Path, sys.argv[1:])
value = json.loads(source.read_text())
evidence = value["product_certification_evidence"]
evidence["result"]["optional_tests"]["skipped"] = ["invented-tests"]
raw = (json.dumps(
    evidence["result"], ensure_ascii=True, sort_keys=True, separators=(",", ":"),
) + "\n").encode()
evidence["digest"] = hashlib.sha256(raw).hexdigest()
target.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")
target.chmod(0o600)
PY
expect_failure "activation rejects a receipt with a resealed skipped-test inventory" \
  plan --project optional --product "$PRODUCT_OPTIONAL" --sha "$SHA_A" \
  --receipt "$OPTIONAL_TAMPER"
expect_failure "certification runs optional tests by default" \
  certify --project optional --product "$PRODUCT_OPTIONAL" --sha "$SHA_A"

PRODUCT_ONE="$(make_product product-one)"
set_pin "$PRODUCT_ONE" "$SHA_A"
expect_failure "certification refuses an undeclared optional-test skip" \
  certify --project alpha --product "$PRODUCT_ONE" --sha "$SHA_A" \
  --skip-optional-tests
printf '%s\n' 'PREVIEW_PROVIDER=none' >> "$PRODUCT_ONE/factory/PROJECT.env"
commit_all "$PRODUCT_ONE" "add duplicate preview provider"
push_main "$PRODUCT_ONE"
expect_failure "certification rejects a duplicate preview provider after CERTIFY_SCRIPT" \
  certify --project alpha --product "$PRODUCT_ONE" --sha "$SHA_A"
if [[ "$LAST_OUTPUT" == *"PREVIEW_PROVIDER must be exactly railway or none"* ]]; then
  pass "certification parses provider declarations after CERTIFY_SCRIPT"
else
  fail "certification reports duplicate preview providers" "$LAST_OUTPUT"
fi
python3 - "$PRODUCT_ONE/factory/PROJECT.env" <<'PY'
import pathlib, sys
path = pathlib.Path(sys.argv[1])
lines = path.read_text().splitlines()
path.write_text("\n".join(lines[:-1]) + "\n")
PY
commit_all "$PRODUCT_ONE" "restore preview provider"
push_main "$PRODUCT_ONE"
python3 - "$PRODUCT_ONE/factory/PROJECT.env" <<'PY'
import pathlib, sys
path = pathlib.Path(sys.argv[1])
path.write_text("\n".join(
    line for line in path.read_text().splitlines()
    if not line.startswith("NONVISUAL_PATHS=")
) + "\n")
PY
commit_all "$PRODUCT_ONE" "remove nonvisual policy"
push_main "$PRODUCT_ONE"
expect_failure "certification refuses a nonvisual-only product without strict paths" \
  certify --project alpha --product "$PRODUCT_ONE" --sha "$SHA_A"
if [[ "$LAST_OUTPUT" == *"PREVIEW_PROVIDER=none requires strict NONVISUAL_PATHS"* ]]; then
  pass "certification binds nonvisual-only products to strict paths"
else
  fail "certification reports missing nonvisual paths" "$LAST_OUTPUT"
fi
printf '%s\n' 'NONVISUAL_PATHS=app/tools/,app/tests/' >> \
  "$PRODUCT_ONE/factory/PROJECT.env"
commit_all "$PRODUCT_ONE" "restore nonvisual policy"
push_main "$PRODUCT_ONE"
MISMATCHED_LAUNCHER="$TMP/mismatched-factory-launch"
printf '%s\n' '#!/usr/bin/env bash' 'exit 1' > "$MISMATCHED_LAUNCHER"
chmod +x "$MISMATCHED_LAUNCHER"
export FACTORY_KIT_TEST_INSTALLED_LAUNCHER="$MISMATCHED_LAUNCHER"
expect_failure "certification rejects an installed launcher from another release" \
  certify --project alpha --product "$PRODUCT_ONE" --sha "$SHA_A"
unset FACTORY_KIT_TEST_INSTALLED_LAUNCHER
if [[ "$LAST_OUTPUT" == *"installed factory-launch does not match the sealed candidate"* &&
      "$LAST_OUTPUT" == *"signed Contract 2 release transaction"* ]]; then
  pass "launcher drift fails before product certification"
else
  fail "launcher drift reports its exact activation boundary" "$LAST_OUTPUT"
fi
printf '%s\n\n' "$SHA_A" > "$PRODUCT_ONE/factory/KIT_PIN"
expect_failure "KIT_PIN rejects a blank physical line" \
  certify --project alpha --product "$PRODUCT_ONE" --sha "$SHA_A"
printf '%s\n' "$(printf '%s' "$SHA_A" | tr 'a-f' 'A-F')" > "$PRODUCT_ONE/factory/KIT_PIN"
expect_failure "KIT_PIN rejects uppercase SHA text" \
  certify --project alpha --product "$PRODUCT_ONE" --sha "$SHA_A"
printf '%s\n%s\n' "$SHA_A" "$SHA_A" > "$PRODUCT_ONE/factory/KIT_PIN"
expect_failure "KIT_PIN rejects multiple physical SHA lines" \
  certify --project alpha --product "$PRODUCT_ONE" --sha "$SHA_A"
printf '%s\n' "$SHA_A" > "$PRODUCT_ONE/factory/KIT_PIN"
printf 'fail\n' > "$PRODUCT_ONE/factory/FAIL_CERTIFY"
commit_all "$PRODUCT_ONE" "force certification failure"
push_main "$PRODUCT_ONE"
expect_failure "failed certification output is redacted" \
  certify --project alpha --product "$PRODUCT_ONE" --sha "$SHA_A"
CERTIFICATION_FAILURE=""
for candidate in "$STATE/receipts/failures/"*.json; do
  if [[ "$(json_value "$candidate" failure_stage 2>/dev/null)" == "product" &&
        "$(json_value "$candidate" driver_exit_status 2>/dev/null)" == "42" ]]; then
    CERTIFICATION_FAILURE="$candidate"
    break
  fi
done
CERTIFICATION_FAILURE_SAFE=0
if [[ -f "$CERTIFICATION_FAILURE" ]] &&
   [[ "$(json_value "$CERTIFICATION_FAILURE" status)" == "fail" ]] &&
   [[ "$(json_value "$CERTIFICATION_FAILURE" schema)" == \
      "nysa.software-factory.certification-failure/v2" ]] &&
   [[ "$(json_value "$CERTIFICATION_FAILURE" failure_stage)" == "product" ]] &&
   [[ "$(json_value "$CERTIFICATION_FAILURE" driver_exit_status)" == "42" ]] &&
   [[ "$(json_value "$CERTIFICATION_FAILURE" certification_exit_status)" == "42" ]] &&
   [[ "$(json_value "$CERTIFICATION_FAILURE" factory_sha)" == "$SHA_A" ]] &&
   failure_receipt_valid "$CERTIFICATION_FAILURE" &&
   ! grep -qE 'supersecret|user:pass|bearer-one|digest-one|json-one|line-one|query-one' \
     "$CERTIFICATION_FAILURE"; then
  CERTIFICATION_FAILURE_SAFE=1
fi
if [[ "$LAST_OUTPUT" != *"supersecret"* && "$LAST_OUTPUT" != *"user:pass"* &&
      "$LAST_OUTPUT" != *"bearer-one"* && "$LAST_OUTPUT" != *"bearer-two"* &&
      "$LAST_OUTPUT" != *"digest-one"* && "$LAST_OUTPUT" != *"digest-two"* &&
      "$LAST_OUTPUT" != *"json-one"* && "$LAST_OUTPUT" != *"json-two"* &&
      "$LAST_OUTPUT" != *"line-one"* && "$LAST_OUTPUT" != *"line-two"* &&
      "$LAST_OUTPUT" != *"hyphen-one"* && "$LAST_OUTPUT" != *"hyphen-two"* &&
      "$LAST_OUTPUT" != *"yaml-one"* && "$LAST_OUTPUT" != *"yaml-three"* &&
      "$LAST_OUTPUT" != *"continuation-head"* &&
      "$LAST_OUTPUT" != *"continuation-one"* &&
      "$LAST_OUTPUT" != *"query-one"* && "$LAST_OUTPUT" != *"query-two"* &&
      "$LAST_OUTPUT" == *"[REDACTED]"* &&
      "$CERTIFICATION_FAILURE_SAFE" -eq 1 ]]; then
  pass "structured certification output never exposes secrets"
else
  fail "structured certification output never exposes secrets" "$LAST_OUTPUT"
fi
rm "$PRODUCT_ONE/factory/FAIL_CERTIFY"
commit_all "$PRODUCT_ONE" "restore certification"
push_main "$PRODUCT_ONE"

python3 - "$PRODUCT_ONE/factory/certification-plan.json" <<'PY'
import json, pathlib, sys
path = pathlib.Path(sys.argv[1])
value = json.loads(path.read_text())
value["phases"][0]["command"] = ["false"]
path.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")
PY
commit_all "$PRODUCT_ONE" "force certification phase failure"
push_main "$PRODUCT_ONE"
expect_failure "phase failure preserves phase evidence" \
  certify --project alpha --product "$PRODUCT_ONE" --sha "$SHA_A"
PHASE_FAILURE=""
for candidate in "$STATE/receipts/failures/"*.json; do
  if [[ "$(json_value "$candidate" failure_stage 2>/dev/null)" == "phases" ]]; then
    PHASE_FAILURE="$candidate"
    break
  fi
done
if [[ -f "$PHASE_FAILURE" ]] &&
   [[ "$(json_value "$PHASE_FAILURE" driver_exit_status)" == "1" ]] &&
   [[ "$(json_value "$PHASE_FAILURE" certification_exit_status)" == "1" ]] &&
   [[ "$(json_value "$PHASE_FAILURE" result.status)" == "fail" ]] &&
   product_certification_host_load_valid "$PHASE_FAILURE" &&
   failure_receipt_valid "$PHASE_FAILURE"; then
  pass "phase failure retains its exact boundary and result"
else
  fail "phase failure retains its exact boundary and result" "$LAST_OUTPUT"
fi
python3 - "$PRODUCT_ONE/factory/certification-plan.json" <<'PY'
import json, pathlib, sys
path = pathlib.Path(sys.argv[1])
value = json.loads(path.read_text())
value["phases"][0]["command"] = ["true"]
path.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")
PY
commit_all "$PRODUCT_ONE" "restore certification phase"
push_main "$PRODUCT_ONE"

printf 'sleep\n' > "$PRODUCT_ONE/factory/SLEEP_CERTIFY"
commit_all "$PRODUCT_ONE" "force outer certification timeout"
push_main "$PRODUCT_ONE"
export FACTORY_KIT_CERTIFY_TIMEOUT_SECONDS=1
expect_failure "outer timeout preserves host load without a product result" \
  certify --project alpha --product "$PRODUCT_ONE" --sha "$SHA_A"
unset FACTORY_KIT_CERTIFY_TIMEOUT_SECONDS
TIMEOUT_FAILURE=""
for candidate in "$STATE/receipts/failures/"*.json; do
  if [[ "$(json_value "$candidate" failure_stage 2>/dev/null)" == "product" &&
        "$(json_value "$candidate" driver_exit_status 2>/dev/null)" == "124" ]]; then
    TIMEOUT_FAILURE="$candidate"
    break
  fi
done
if [[ -f "$TIMEOUT_FAILURE" ]] &&
   [[ "$(json_value "$TIMEOUT_FAILURE" certification_exit_status)" == "124" ]] &&
   [[ "$(json_value "$TIMEOUT_FAILURE" result)" == "" ]] &&
   product_certification_host_load_valid "$TIMEOUT_FAILURE" &&
   failure_receipt_valid "$TIMEOUT_FAILURE"; then
  pass "outer timeout retains bounded host load with null result"
else
  fail "outer timeout retains bounded host load with null result" "$LAST_OUTPUT"
fi
rm "$PRODUCT_ONE/factory/SLEEP_CERTIFY"
commit_all "$PRODUCT_ONE" "restore certification timeout fixture"
push_main "$PRODUCT_ONE"

export FACTORY_KIT_TEST_CERTIFICATION_DRIVER_SETUP_FAIL=1
expect_failure "setup-stage certification preserves a typed diagnostic" \
  certify --project alpha --product "$PRODUCT_ONE" --sha "$SHA_A"
unset FACTORY_KIT_TEST_CERTIFICATION_DRIVER_SETUP_FAIL
SETUP_FAILURE=""
for candidate in "$STATE/receipts/failures/"*.json; do
  if [[ "$(json_value "$candidate" failure_stage 2>/dev/null)" == "setup" ]]; then
    SETUP_FAILURE="$candidate"
    break
  fi
done
if [[ -n "$SETUP_FAILURE" && -f "$SETUP_FAILURE" ]] &&
   [[ "$(json_value "$SETUP_FAILURE" schema)" == \
      "nysa.software-factory.certification-failure/v2" ]] &&
   [[ "$(json_value "$SETUP_FAILURE" driver_exit_status)" == "73" ]] &&
   [[ "$(json_value "$SETUP_FAILURE" certification_exit_status)" == "73" ]] &&
   [[ "$(json_value "$SETUP_FAILURE" result)" == "" ]] &&
   [[ "$(json_value "$SETUP_FAILURE" failure_reason)" == \
      "driver exited 73 before product launch" ]] &&
   grep -q '\[REDACTED\]' "$SETUP_FAILURE" &&
   ! grep -q 'factory-setup-fixture' "$SETUP_FAILURE" &&
   failure_receipt_valid "$SETUP_FAILURE" &&
   [[ "$LAST_OUTPUT" == *"[REDACTED]"* ]] &&
   [[ "$LAST_OUTPUT" != *"factory-setup-fixture"* ]]; then
  pass "setup-stage certification failure is actionable and redacted"
else
  fail "setup-stage certification failure is actionable and redacted" \
    "$LAST_OUTPUT"
fi

export FACTORY_KIT_TEST_CERTIFICATION_CACHE_PUBLISH_FAIL=1
expect_failure "post-driver cache failure preserves the driver result" \
  certify --project alpha --product "$PRODUCT_ONE" --sha "$SHA_A"
unset FACTORY_KIT_TEST_CERTIFICATION_CACHE_PUBLISH_FAIL
CACHE_FAILURE=""
for candidate in "$STATE/receipts/failures/"*.json; do
  if [[ "$(json_value "$candidate" failure_stage 2>/dev/null)" == "cache" ]]; then
    CACHE_FAILURE="$candidate"
    break
  fi
done
if [[ -f "$CACHE_FAILURE" ]] &&
   [[ "$(json_value "$CACHE_FAILURE" driver_exit_status)" == "0" ]] &&
   [[ "$(json_value "$CACHE_FAILURE" certification_exit_status)" == "125" ]] &&
   [[ "$(json_value "$CACHE_FAILURE" result.status)" == "pass" ]] &&
   [[ "$(json_value "$CACHE_FAILURE" failure_reason)" == \
      "certification cache publication failed after driver success" ]] &&
   failure_receipt_valid "$CACHE_FAILURE"; then
  pass "cache failure retains separate driver and certification status"
else
  fail "cache failure retains separate driver and certification status" \
    "$LAST_OUTPUT"
fi

CERTIFICATION_TRACE="$TMP/certification.trace"
export FACTORY_KIT_TEST_CERTIFICATION_TRACE="$CERTIFICATION_TRACE"
: > "$CERTIFICATION_TRACE"
expect_success "first certification reuses install suite evidence" \
  certify --project alpha --product "$PRODUCT_ONE" --sha "$SHA_A"
REUSED_RECEIPT_ONE="$(printf '%s\n' "$LAST_OUTPUT" | awk '/^\// {value=$0} END {print value}')"
expect_success "successive certification reuses suite evidence" \
  certify --project alpha --product "$PRODUCT_ONE" --sha "$SHA_A"
REUSED_RECEIPT_TWO="$(printf '%s\n' "$LAST_OUTPUT" | awk '/^\// {value=$0} END {print value}')"
if [[ "$(grep -c '^kit-suite:reused$' "$CERTIFICATION_TRACE")" == "2" &&
      "$(grep -c '^product-certification$' "$CERTIFICATION_TRACE")" == "2" &&
      "$(json_value "$REUSED_RECEIPT_ONE" receipt_id)" != "$(json_value "$REUSED_RECEIPT_TWO" receipt_id)" &&
      "$(json_value "$REUSED_RECEIPT_ONE" kit_suite_evidence.reused)" == "True" &&
      "$(json_value "$REUSED_RECEIPT_TWO" kit_suite_evidence.reused)" == "True" ]]; then
  pass "reuse still runs product certification and issues fresh receipts"
else
  fail "reuse still runs product certification and issues fresh receipts"
fi

printf '{malformed\n' > "$EVIDENCE_A"
chmod 600 "$EVIDENCE_A"
: > "$CERTIFICATION_TRACE"
export FACTORY_KIT_TEST_REMOTE_FULL_CI=0
expect_failure "malformed suite evidence and unavailable GitHub proof fail closed" \
  certify --project alpha --product "$PRODUCT_ONE" --sha "$SHA_A"
if [[ "$LAST_OUTPUT" == *"exact successful main GitHub CI evidence is required"* &&
      "$LAST_OUTPUT" != *"fixture suite failed"* &&
      ! -s "$CERTIFICATION_TRACE" ]]; then
  pass "missing certification evidence fails before local suite execution"
else
  fail "missing certification evidence fails before local suite execution" "$LAST_OUTPUT"
fi
export FACTORY_KIT_TEST_REMOTE_FULL_CI=1
expect_success "malformed suite evidence falls back to a fresh suite" \
  certify --project alpha --product "$PRODUCT_ONE" --sha "$SHA_A"
unset FACTORY_KIT_TEST_REMOTE_FULL_CI
if grep -qx 'kit-suite:certification' "$CERTIFICATION_TRACE" &&
   [[ "$(json_value "$EVIDENCE_A" status)" == "pass" ]]; then
  pass "fresh suite refreshes malformed evidence"
else
  fail "fresh suite refreshes malformed evidence"
fi

chmod 644 "$EVIDENCE_A"
: > "$CERTIFICATION_TRACE"
expect_success "broad-mode suite evidence falls back safely" \
  certify --project alpha --product "$PRODUCT_ONE" --sha "$SHA_A"
if grep -qx 'kit-suite:certification' "$CERTIFICATION_TRACE" &&
   [[ "$(python3 -c 'import os,sys; print(oct(os.stat(sys.argv[1]).st_mode & 0o777)[2:])' "$EVIDENCE_A")" == "600" ]]; then
  pass "fresh suite restores restrictive evidence permissions"
else
  fail "fresh suite restores restrictive evidence permissions"
fi

mv "$EVIDENCE_A" "$TMP/evidence-target"
ln -s "$TMP/evidence-target" "$EVIDENCE_A"
: > "$CERTIFICATION_TRACE"
expect_success "symlinked suite evidence falls back safely" \
  certify --project alpha --product "$PRODUCT_ONE" --sha "$SHA_A"
if [[ -f "$EVIDENCE_A" && ! -L "$EVIDENCE_A" ]] &&
   grep -qx 'kit-suite:certification' "$CERTIFICATION_TRACE"; then
  pass "symlinked evidence is replaced without following it"
else
  fail "symlinked evidence is replaced without following it"
fi

set_evidence_value "$EVIDENCE_A" created_epoch 1
set_evidence_value "$EVIDENCE_A" expires_epoch 86401
: > "$CERTIFICATION_TRACE"
export FACTORY_KIT_TEST_REMOTE_FULL_CI=1
expect_success "stale suite evidence refreshes from remote full CI" \
  certify --project alpha --product "$PRODUCT_ONE" --sha "$SHA_A"
unset FACTORY_KIT_TEST_REMOTE_FULL_CI
if grep -qx 'kit-suite:certification' "$CERTIFICATION_TRACE" &&
   [[ "$(json_value "$EVIDENCE_A" verification_source)" == "github-actions-full" ]]; then
  pass "stale evidence uses remote proof plus smoke instead of local full"
else
  fail "stale evidence uses remote proof plus smoke instead of local full"
fi

for binding in host os architecture; do
  set_evidence_value "$EVIDENCE_A" "$binding" '"mismatch"'
  : > "$CERTIFICATION_TRACE"
  expect_success "$binding mismatch reruns the suite" \
    certify --project alpha --product "$PRODUCT_ONE" --sha "$SHA_A"
  grep -qx 'kit-suite:certification' "$CERTIFICATION_TRACE" &&
    pass "$binding mismatch is never reused" ||
    fail "$binding mismatch is never reused"
done

set_evidence_value "$EVIDENCE_A" verification_source '"untrusted"'
: > "$CERTIFICATION_TRACE"
expect_success "unknown verification source reruns the suite" \
  certify --project alpha --product "$PRODUCT_ONE" --sha "$SHA_A"
if grep -qx 'kit-suite:certification' "$CERTIFICATION_TRACE" &&
   [[ "$(json_value "$EVIDENCE_A" verification_source)" == "github-actions-full" ]]; then
  pass "unknown verification source is never reused"
else
  fail "unknown verification source is never reused"
fi

set_evidence_value "$EVIDENCE_A" release_tree '"0000000000000000000000000000000000000000"'
: > "$CERTIFICATION_TRACE"
expect_success "release-tree evidence mismatch reruns the suite" \
  certify --project alpha --product "$PRODUCT_ONE" --sha "$SHA_A"
grep -qx 'kit-suite:certification' "$CERTIFICATION_TRACE" &&
  pass "release-tree mismatch is never reused" ||
  fail "release-tree mismatch is never reused"

set_evidence_value "$EVIDENCE_A" suite_definition '"factory-kit-suite-v0"'
: > "$CERTIFICATION_TRACE"
expect_success "suite-definition mismatch reruns the suite" \
  certify --project alpha --product "$PRODUCT_ONE" --sha "$SHA_A"
grep -qx 'kit-suite:certification' "$CERTIFICATION_TRACE" &&
  pass "suite-definition mismatch is never reused" ||
  fail "suite-definition mismatch is never reused"

export FACTORY_KIT_SUITE_EVIDENCE_TTL_SECONDS=60
export FACTORY_KIT_RECEIPT_TTL_SECONDS=3600
: > "$CERTIFICATION_TRACE"
expect_success "configured evidence lifetime change reruns the suite" \
  certify --project alpha --product "$PRODUCT_ONE" --sha "$SHA_A"
CAPPED_RECEIPT="$(printf '%s\n' "$LAST_OUTPUT" | awk '/^\// {value=$0} END {print value}')"
if grep -qx 'kit-suite:certification' "$CERTIFICATION_TRACE" &&
   [[ "$(json_value "$CAPPED_RECEIPT" expires_epoch)" == \
      "$(json_value "$CAPPED_RECEIPT" kit_suite_evidence.expires_epoch)" ]] &&
   [[ "$(json_value "$CAPPED_RECEIPT" kit_suite_evidence.evidence_ttl_seconds)" == "60" ]]; then
  pass "receipt expiry is capped and bound to suite evidence"
else
  fail "receipt expiry is capped and bound to suite evidence"
fi
unset FACTORY_KIT_SUITE_EVIDENCE_TTL_SECONDS
unset FACTORY_KIT_RECEIPT_TTL_SECONDS

set_evidence_value "$EVIDENCE_A" status '"fail"'
FAILED_EVIDENCE_HASH="$(shasum -a 256 "$EVIDENCE_A" | awk '{print $1}')"
export FACTORY_KIT_TEST_SUITE_FAIL=1
expect_failure "failed fresh suite does not publish passing evidence" \
  certify --project alpha --product "$PRODUCT_ONE" --sha "$SHA_A"
unset FACTORY_KIT_TEST_SUITE_FAIL
if [[ "$FAILED_EVIDENCE_HASH" == "$(shasum -a 256 "$EVIDENCE_A" | awk '{print $1}')" &&
      "$(json_value "$EVIDENCE_A" status)" == "fail" ]]; then
  pass "failed fresh suite leaves prior nonpassing evidence unchanged"
else
  fail "failed fresh suite leaves prior nonpassing evidence unchanged"
fi
expect_success "successful fresh suite repairs nonpassing evidence" \
  certify --project alpha --product "$PRODUCT_ONE" --sha "$SHA_A"

chmod u+w "$STATE/releases/$SHA_A/payload.txt"
printf 'drifted\n' > "$STATE/releases/$SHA_A/payload.txt"
: > "$CERTIFICATION_TRACE"
expect_failure "physical release drift fails before evidence reuse" \
  certify --project alpha --product "$PRODUCT_ONE" --sha "$SHA_A"
if ! grep -q '^kit-suite:' "$CERTIFICATION_TRACE"; then
  pass "drifted physical release cannot consume suite evidence"
else
  fail "drifted physical release cannot consume suite evidence"
fi
printf 'release-a\n' > "$STATE/releases/$SHA_A/payload.txt"
chmod -R a-w "$STATE/releases/$SHA_A"

printf '{partial' > "$EVIDENCE_A"
chmod 600 "$EVIDENCE_A"
: > "$CERTIFICATION_TRACE"
export FACTORY_KIT_TEST_SUITE_SLEEP_SECONDS=1
export FACTORY_KIT_LOCK_ATTEMPTS=200
run_kit certify --project alpha --product "$PRODUCT_ONE" --sha "$SHA_A" \
  > "$TMP/concurrent-certify-one.out" 2>&1 &
CONCURRENT_ONE=$!
for _ in $(seq 1 40); do
  [[ -f "$STATE/.install.lock/owner" ]] && break
  sleep 0.05
done
[[ -f "$STATE/.install.lock/owner" ]] ||
  fail "concurrent certification fixture observes the held install lock"
run_kit certify --project alpha --product "$PRODUCT_ONE" --sha "$SHA_A" \
  > "$TMP/concurrent-certify-two.out" 2>&1 &
CONCURRENT_TWO=$!
wait "$CONCURRENT_ONE"; CONCURRENT_ONE_STATUS=$?
wait "$CONCURRENT_TWO"; CONCURRENT_TWO_STATUS=$?
unset FACTORY_KIT_TEST_SUITE_SLEEP_SECONDS
unset FACTORY_KIT_LOCK_ATTEMPTS
if [[ "$CONCURRENT_ONE_STATUS" == "0" && "$CONCURRENT_TWO_STATUS" == "0" &&
      "$(grep -c '^kit-suite:certification$' "$CERTIFICATION_TRACE")" == "1" &&
      "$(grep -c '^kit-suite:reused$' "$CERTIFICATION_TRACE")" == "1" &&
      "$(grep -c '^product-certification$' "$CERTIFICATION_TRACE")" == "2" ]] &&
   python3 -m json.tool "$EVIDENCE_A" >/dev/null; then
  pass "concurrent certifications serialize evidence refresh atomically"
else
  fail "concurrent certifications serialize evidence refresh atomically" \
    "$(<"$TMP/concurrent-certify-one.out") $(<"$TMP/concurrent-certify-two.out")"
fi
unset FACTORY_KIT_TEST_CERTIFICATION_TRACE

PRODUCT_ONE_ORIGIN="$(git -C "$PRODUCT_ONE" remote get-url origin)"
PRODUCT_ONE_DECOY="$TMP/product-one-decoy.git"
git init --bare -q "$PRODUCT_ONE_DECOY"
git -C "$PRODUCT_ONE" config --add remote.origin.pushurl "$PRODUCT_ONE_ORIGIN"
git -C "$PRODUCT_ONE" config --add remote.origin.pushurl "$PRODUCT_ONE_DECOY"
expect_failure "certification rejects multiple product push destinations" \
  certify --project alpha --product "$PRODUCT_ONE" --sha "$SHA_A"
git -C "$PRODUCT_ONE" config --unset-all remote.origin.pushurl

git -C "$PRODUCT_ONE" config remote.origin.pushurl ../relative-product.git
expect_failure "certification rejects a relative product push destination" \
  certify --project alpha --product "$PRODUCT_ONE" --sha "$SHA_A"
git -C "$PRODUCT_ONE" config remote.origin.pushurl \
  'https://factory-user:factory-password@example.invalid/product.git'
expect_failure "certification rejects credentials in an HTTP push destination" \
  certify --project alpha --product "$PRODUCT_ONE" --sha "$SHA_A"
if [[ "$LAST_OUTPUT" != *"factory-user"* &&
      "$LAST_OUTPUT" != *"factory-password"* ]]; then
  pass "credential-bearing product push destination failure is redacted"
else
  fail "credential-bearing product push destination failure is redacted" "$LAST_OUTPUT"
fi
git -C "$PRODUCT_ONE" config remote.origin.pushurl \
  'ssh://factory-user:factory-password@example.invalid/product.git'
expect_failure "certification rejects password userinfo in a non-HTTP push destination" \
  certify --project alpha --product "$PRODUCT_ONE" --sha "$SHA_A"
if [[ "$LAST_OUTPUT" != *"factory-user"* &&
      "$LAST_OUTPUT" != *"factory-password"* ]]; then
  pass "non-HTTP credential failure is redacted"
else
  fail "non-HTTP credential failure is redacted" "$LAST_OUTPUT"
fi
git -C "$PRODUCT_ONE" config remote.origin.pushurl \
  'ssh://factory-user%3Afactory-password@example.invalid/product.git'
expect_failure "certification rejects encoded password userinfo in a push destination" \
  certify --project alpha --product "$PRODUCT_ONE" --sha "$SHA_A"
if [[ "$LAST_OUTPUT" != *"factory-user"* &&
      "$LAST_OUTPUT" != *"factory-password"* ]]; then
  pass "encoded credential failure is redacted"
else
  fail "encoded credential failure is redacted" "$LAST_OUTPUT"
fi
git -C "$PRODUCT_ONE" config remote.origin.pushurl \
  'git@github.com:org/repo with space.git'
expect_failure "certification rejects whitespace in an scp-like push destination" \
  certify --project alpha --product "$PRODUCT_ONE" --sha "$SHA_A"
git -C "$PRODUCT_ONE" config --unset-all remote.origin.pushurl

export FACTORY_KIT_TEST_FORCE_PRODUCTION_SANDBOX=1
export FACTORY_KIT_SANDBOX_EXEC="$TMP/missing-sandbox-exec"
expect_failure "production certification fails closed without sandbox" \
  certify --project alpha --product "$PRODUCT_ONE" --sha "$SHA_A"
unset FACTORY_KIT_TEST_FORCE_PRODUCTION_SANDBOX
unset FACTORY_KIT_SANDBOX_EXEC

CERT_SANDBOX_CAPTURE="$TMP/certification-sandbox.profile"
export FACTORY_KIT_TEST_FORCE_PRODUCTION_SANDBOX=1
export FACTORY_KIT_SANDBOX_EXEC="$TMP/fake-sandbox-exec"
export FACTORY_KIT_SANDBOX_CAPTURE="$CERT_SANDBOX_CAPTURE"
expect_success "certification uses production sandbox and disposable trees" \
  certify --project alpha --product "$PRODUCT_ONE" --sha "$SHA_A"
unset FACTORY_KIT_TEST_FORCE_PRODUCTION_SANDBOX
unset FACTORY_KIT_SANDBOX_EXEC
unset FACTORY_KIT_SANDBOX_CAPTURE
if [[ -f "$CERT_SANDBOX_CAPTURE" ]] &&
   grep -qx 'deny' "${CERT_SANDBOX_CAPTURE}.network" &&
   ! grep -qx 'allow' "${CERT_SANDBOX_CAPTURE}.network" &&
   grep -q '^(deny default)' "$CERT_SANDBOX_CAPTURE" &&
   ! grep -qx '(allow file-read\*)' "$CERT_SANDBOX_CAPTURE" &&
   ! grep -qx '(allow network\*)' "$CERT_SANDBOX_CAPTURE" &&
   ! grep -qx '(allow network-outbound)' "$CERT_SANDBOX_CAPTURE" &&
   grep -Fqx '(allow network-bind (local ip "localhost:*"))' "$CERT_SANDBOX_CAPTURE" &&
   grep -Fqx '(allow network-inbound (local ip "localhost:*"))' "$CERT_SANDBOX_CAPTURE" &&
   grep -Fqx '(allow network-outbound (remote ip "localhost:*"))' "$CERT_SANDBOX_CAPTURE" &&
   ! grep -qx '(allow process\*)' "$CERT_SANDBOX_CAPTURE" &&
   grep -q '^(allow process-fork)' "$CERT_SANDBOX_CAPTURE" &&
   grep -Fqx '(allow process-info* (target same-sandbox))' "$CERT_SANDBOX_CAPTURE" &&
   grep -Fqx '(allow signal (target same-sandbox))' "$CERT_SANDBOX_CAPTURE" &&
   ! grep -Fqx '(allow signal (target others))' "$CERT_SANDBOX_CAPTURE" &&
   grep -Fqx '(allow file-read* (subpath "/dev/fd"))' "$CERT_SANDBOX_CAPTURE" &&
   grep -Fqx '(allow file-write* (subpath "/dev/fd"))' "$CERT_SANDBOX_CAPTURE" &&
   grep -q "$PRODUCT_ONE" "$CERT_SANDBOX_CAPTURE" &&
   grep -q "$STATE/releases/$SHA_A" "$CERT_SANDBOX_CAPTURE" &&
   grep -q 'allow file-write.*factory-kit-certification' "$CERT_SANDBOX_CAPTURE" &&
   grep -q 'deny file-write.*certification-cache-input' "$CERT_SANDBOX_CAPTURE"; then
  pass "certification sandbox is filesystem and network default-deny"
else
  fail "certification sandbox is filesystem and network default-deny"
fi
RECEIPT_STALE="$(printf '%s\n' "$LAST_OUTPUT" | awk '/^\// {value=$0} END {print value}')"
RECEIPT_STALE_ID="$(json_value "$RECEIPT_STALE" receipt_id)"
if [[ "$(basename "$RECEIPT_STALE")" == "$RECEIPT_STALE_ID.json" &&
      "$(json_value "$RECEIPT_STALE" certification_tool_version)" == "7" &&
      "$(json_value "$RECEIPT_STALE" provider_concurrency_evidence.status)" == "not-required" &&
      "$(json_value "$RECEIPT_STALE" provider_concurrency_evidence.factory_sha)" == "$SHA_A" &&
      "$(json_value "$RECEIPT_STALE" provider_concurrency_evidence.factory_tree)" == "$(git -C "$KIT_REPO" rev-parse "$SHA_A^{tree}")" &&
      "$(json_value "$RECEIPT_STALE" checks.provider_concurrency)" == "pass" &&
      "$(json_value "$RECEIPT_STALE" product_certification_evidence.mode)" == "measured" ]] &&
   product_certification_host_load_valid "$RECEIPT_STALE" &&
   [[ -z "$(json_value "$RECEIPT_STALE" expected_previous_generation)" &&
      ! -e "$PRODUCT_ONE/factory/product-certification-marker" &&
      ! -e "$STATE/releases/$SHA_A/release-certification-marker" &&
      ! -e "$HOME/.factory-kit-certification-marker" ]]; then
  pass "receipt identity and isolated certification bindings are exact"
else
  fail "receipt identity and isolated certification bindings are exact"
fi

if [[ "${FACTORY_KIT_OUTER_SANDBOX:-0}" != "1" &&
      "$(uname -s)" == "Darwin" && -x /usr/bin/sandbox-exec ]]; then
  export FACTORY_KIT_TEST_FORCE_PRODUCTION_SANDBOX=1
  export FACTORY_KIT_SANDBOX_EXEC=/usr/bin/sandbox-exec
  expect_success "real Seatbelt certification applies one phase sandbox" \
    certify --project alpha --product "$PRODUCT_ONE" --sha "$SHA_A"
  unset FACTORY_KIT_TEST_FORCE_PRODUCTION_SANDBOX
  unset FACTORY_KIT_SANDBOX_EXEC
else
  pass "real Seatbelt certification probe skipped when already sandboxed or unavailable"
fi

chmod u+w "$RECEIPT_STALE"
python3 - "$RECEIPT_STALE" <<'PY'
import json, pathlib, sys
path = pathlib.Path(sys.argv[1])
value = json.loads(path.read_text())
value["expires_epoch"] = 1
value["expires_at"] = "1970-01-01T00:00:01Z"
path.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")
PY
chmod 600 "$RECEIPT_STALE"
printf '{}\n' > "$PRODUCT_ONE/factory/MAINTENANCE"
expect_failure "stale receipt is rejected before activation" \
  activate --project alpha --product "$PRODUCT_ONE" --sha "$SHA_A" \
  --receipt "$RECEIPT_STALE"
rm "$PRODUCT_ONE/factory/MAINTENANCE"

expect_success "pause publishes maintenance through launch lock" \
  pause --project alpha --product "$PRODUCT_ONE"
expect_failure "in-progress ticket without lease blocks activation" \
  activate --project alpha --product "$PRODUCT_ONE" --sha "$SHA_A"

set_ticket_lease "$PRODUCT_ONE" "$SHA_A"
commit_all "$PRODUCT_ONE" "lease planning ticket to release a"
push_main "$PRODUCT_ONE"
expect_success "revised product tuple certifies" \
  certify --project alpha --product "$PRODUCT_ONE" --sha "$SHA_A"
RECEIPT_A="$(printf '%s\n' "$LAST_OUTPUT" | awk '/^\// {value=$0} END {print value}')"
RECEIPTS_BEFORE_QUALIFICATION="$(
  find "$STATE/receipts" -maxdepth 1 -type f | wc -l | tr -d ' '
)"
JOURNALS_BEFORE_QUALIFICATION="$(
  find "$STATE/projects/alpha/activation-journal" -type f 2>/dev/null |
    wc -l | tr -d ' '
)"
for qualification_sha in "$SHA_B" "$SHA_A"; do
  printf '%s\n' \
    "{\"budget_usd\":\"100.000000\",\"capacity\":4,\"contract_version\":\"1.8.0\",\"factory_sha\":\"$qualification_sha\",\"generation\":1,\"per_run_budget_usd\":\"2.000000\",\"per_ticket_budget_usd\":\"25.000000\",\"schema\":\"nysa.software-factory.qualification/v2\",\"target_done\":4,\"tickets\":[\"T-001\",\"T-002\",\"T-003\",\"T-004\"]}" \
    > "$PRODUCT_ONE/factory/QUALIFICATION.json"
  expect_failure "production certification rejects qualification manifest $qualification_sha" \
    certify --project alpha --product "$PRODUCT_ONE" --sha "$SHA_A"
  [[ "$LAST_OUTPUT" == *"production product contains qualification-only"* ]] ||
    fail "production certification reports qualification-only product shape" "$LAST_OUTPUT"
done
expect_failure "activation plan rejects qualification-only product before receipt use" \
  plan --project alpha --product "$PRODUCT_ONE" --sha "$SHA_A" \
  --receipt "$RECEIPT_A"
expect_failure "activation rejects qualification-only product before journal creation" \
  activate --project alpha --product "$PRODUCT_ONE" --sha "$SHA_A" \
  --receipt "$RECEIPT_A"
rm "$PRODUCT_ONE/factory/QUALIFICATION.json"
RECEIPTS_AFTER_QUALIFICATION="$(
  find "$STATE/receipts" -maxdepth 1 -type f | wc -l | tr -d ' '
)"
JOURNALS_AFTER_QUALIFICATION="$(
  find "$STATE/projects/alpha/activation-journal" -type f 2>/dev/null |
    wc -l | tr -d ' '
)"
if [[ "$RECEIPTS_BEFORE_QUALIFICATION" == "$RECEIPTS_AFTER_QUALIFICATION" &&
      "$JOURNALS_BEFORE_QUALIFICATION" == "$JOURNALS_AFTER_QUALIFICATION" ]]; then
  pass "qualification-only refusal creates no receipt or activation journal"
else
  fail "qualification-only refusal creates no receipt or activation journal"
fi
BAD_RECEIPT_NAME="$STATE/receipts/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.json"
mv "$RECEIPT_A" "$BAD_RECEIPT_NAME"
expect_failure "receipt filename must match bound receipt ID" \
  activate --project alpha --product "$PRODUCT_ONE" --sha "$SHA_A" \
  --receipt "$BAD_RECEIPT_NAME"
mv "$BAD_RECEIPT_NAME" "$RECEIPT_A"
cp "$RECEIPT_A" "$TMP/receipt-a.saved"
chmod u+w "$RECEIPT_A"
python3 - "$RECEIPT_A" <<'PY'
import json, pathlib, sys
path = pathlib.Path(sys.argv[1])
value = json.loads(path.read_text())
value["certification_tool_version"] = 999
path.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")
PY
chmod 600 "$RECEIPT_A"
expect_failure "receipt tool version is enforced" \
  activate --project alpha --product "$PRODUCT_ONE" --sha "$SHA_A" \
  --receipt "$RECEIPT_A"
cp "$TMP/receipt-a.saved" "$RECEIPT_A"
chmod 600 "$RECEIPT_A"
printf '{}\n' > "$PRODUCT_ONE/factory/MAINTENANCE"
expect_failure "handwritten maintenance marker is unsupported" \
  activate --project alpha --product "$PRODUCT_ONE" --sha "$SHA_A" \
  --receipt "$RECEIPT_A"
expect_success "pause republishes a valid maintenance marker" \
  pause --project alpha --product "$PRODUCT_ONE"

printf '%s\n\n' "$SHA_A" > "$PRODUCT_ONE/factory/KIT_PIN"
expect_failure "activate rejects KIT_PIN blank-line extras" \
  activate --project alpha --product "$PRODUCT_ONE" --sha "$SHA_A" \
  --receipt "$RECEIPT_A"
printf '%s\n' "$SHA_A" > "$PRODUCT_ONE/factory/KIT_PIN"

printf '%s\n' 'State: Ready' >> "$PRODUCT_ONE/factory/tickets/T-001.md"
commit_all "$PRODUCT_ONE" "add duplicate ticket state fixture"
push_main "$PRODUCT_ONE"
expect_success "duplicate-state product tuple can certify" \
  certify --project alpha --product "$PRODUCT_ONE" --sha "$SHA_A"
RECEIPT_DUPLICATE_STATE="$(printf '%s\n' "$LAST_OUTPUT" | awk '/^\// {value=$0} END {print value}')"
expect_failure "duplicate ticket State fields block activation" \
  activate --project alpha --product "$PRODUCT_ONE" --sha "$SHA_A" \
  --receipt "$RECEIPT_DUPLICATE_STATE"
python3 - "$PRODUCT_ONE/factory/tickets/T-001.md" <<'PY'
import pathlib, sys
path = pathlib.Path(sys.argv[1])
lines = path.read_text().splitlines()
lines.pop(max(i for i, line in enumerate(lines) if line == "State: Ready"))
path.write_text("\n".join(lines) + "\n")
PY
commit_all "$PRODUCT_ONE" "remove duplicate ticket state fixture"
push_main "$PRODUCT_ONE"

printf 'Kit-SHA: %s\n' "$SHA_A" >> "$PRODUCT_ONE/factory/tickets/T-004.md"
commit_all "$PRODUCT_ONE" "add duplicate ticket lease fixture"
push_main "$PRODUCT_ONE"
expect_success "duplicate-lease product tuple can certify" \
  certify --project alpha --product "$PRODUCT_ONE" --sha "$SHA_A"
RECEIPT_DUPLICATE_LEASE="$(printf '%s\n' "$LAST_OUTPUT" | awk '/^\// {value=$0} END {print value}')"
expect_failure "duplicate ticket Kit-SHA fields block activation" \
  activate --project alpha --product "$PRODUCT_ONE" --sha "$SHA_A" \
  --receipt "$RECEIPT_DUPLICATE_LEASE"
python3 - "$PRODUCT_ONE/factory/tickets/T-004.md" <<'PY'
import pathlib, sys
path = pathlib.Path(sys.argv[1])
lines = path.read_text().splitlines()
indices = [i for i, line in enumerate(lines) if line.startswith("Kit-SHA:")]
lines.pop(indices[-1])
path.write_text("\n".join(lines) + "\n")
PY
commit_all "$PRODUCT_ONE" "remove duplicate ticket lease fixture"
push_main "$PRODUCT_ONE"

printf 'State: Canceled\nKit-SHA: %s\n' "$SHA_A" \
  > "$PRODUCT_ONE/factory/tickets/T-006.md"
commit_all "$PRODUCT_ONE" "add canceled ticket lease fixture"
push_main "$PRODUCT_ONE"
expect_success "candidate with canceled ticket lease can certify" \
  certify --project alpha --product "$PRODUCT_ONE" --sha "$SHA_A"
RECEIPT_CANCELED_LEASE="$(printf '%s\n' "$LAST_OUTPUT" | awk '/^\// {value=$0} END {print value}')"
expect_failure "canceled ticket lease blocks activation" \
  activate --project alpha --product "$PRODUCT_ONE" --sha "$SHA_A" \
  --receipt "$RECEIPT_CANCELED_LEASE"
printf '%s\n' 'State: Canceled' > "$PRODUCT_ONE/factory/tickets/T-006.md"
commit_all "$PRODUCT_ONE" "restore lease-free canceled ticket fixture"
push_main "$PRODUCT_ONE"

printf '%s\n' 'State: Done' 'Kit-SHA: not-a-canonical-sha' \
  > "$PRODUCT_ONE/factory/tickets/T-003.md"
commit_all "$PRODUCT_ONE" "add invalid terminal lease fixture"
push_main "$PRODUCT_ONE"
expect_success "invalid-terminal-lease tuple can certify" \
  certify --project alpha --product "$PRODUCT_ONE" --sha "$SHA_A"
RECEIPT_INVALID_DONE="$(printf '%s\n' "$LAST_OUTPUT" | awk '/^\// {value=$0} END {print value}')"
expect_failure "terminal ticket lease is validated before state decision" \
  activate --project alpha --product "$PRODUCT_ONE" --sha "$SHA_A" \
  --receipt "$RECEIPT_INVALID_DONE"
printf '%s\n' 'State: Done' "Kit-SHA: $SHA_A" \
  > "$PRODUCT_ONE/factory/tickets/T-003.md"
commit_all "$PRODUCT_ONE" "add unattested Done fixture"
push_main "$PRODUCT_ONE"
expect_success "plain-Done product tuple can certify" \
  certify --project alpha --product "$PRODUCT_ONE" --sha "$SHA_A"
RECEIPT_PLAIN_DONE="$(printf '%s\n' "$LAST_OUTPUT" | awk '/^\// {value=$0} END {print value}')"
expect_failure "plain Done without protected terminal evidence blocks activation" \
  activate --project alpha --product "$PRODUCT_ONE" --sha "$SHA_A" \
  --receipt "$RECEIPT_PLAIN_DONE"
printf '%s\n' 'State: Backlog' > "$PRODUCT_ONE/factory/tickets/T-003.md"
commit_all "$PRODUCT_ONE" "restore nonterminal backlog fixture"
push_main "$PRODUCT_ONE"
printf '%s\n' '# T-005' 'State: Approved' 'Operator-Approval: Linear' \
  "Kit-SHA: $SHA_A" > "$PRODUCT_ONE/factory/tickets/T-005.md"
mkdir -p "$PRODUCT_ONE/factory/attestations/T-005"
python3 - "$PRODUCT_ONE/factory/attestations/T-005/bundle.json" "$SHA_A" <<'PY'
import json, pathlib, sys
path, sha = pathlib.Path(sys.argv[1]), sys.argv[2]
path.write_text(json.dumps({
    "schema": "nysa.software-factory.ticket-bundle/v1",
    "ticket": "T-005",
    "repository": "example/product-one",
    "branch": "ticket/T-005",
    "pr_number": 5,
    "kit_sha": sha,
    "reviewed_sha": "1" * 40,
    "bundle_blob": "2" * 40,
}, sort_keys=True, separators=(",", ":")) + "\n")
PY
APPROVED_BUNDLE_BLOB="$(git -C "$PRODUCT_ONE" hash-object \
  "$PRODUCT_ONE/factory/attestations/T-005/bundle.json")"
python3 - "$PRODUCT_ONE/factory/attestations/T-005/approval.json" \
  "$SHA_A" "$APPROVED_BUNDLE_BLOB" <<'PY'
import json, pathlib, sys
path = pathlib.Path(sys.argv[1])
sha, attestation_blob = sys.argv[2:]
path.write_text(json.dumps({
    "schema": "nysa.software-factory.ticket-approval/v1",
    "ticket": "T-005",
    "repository": "example/product-one",
    "branch": "ticket/T-005",
    "pr_number": 5,
    "kit_sha": sha,
    "reviewed_sha": "1" * 40,
    "bundle_blob": "2" * 40,
    "bundle_attestation_blob": attestation_blob,
}, sort_keys=True, separators=(",", ":")) + "\n")
PY
commit_all "$PRODUCT_ONE" "protect old approved ticket evidence"
push_main "$PRODUCT_ONE"
expect_success "clean ticket tuple recertifies" \
  certify --project alpha --product "$PRODUCT_ONE" --sha "$SHA_A"
RECEIPT_A="$(printf '%s\n' "$LAST_OUTPUT" | awk '/^\// {value=$0} END {print value}')"

# The barrier is visible before pause queues behind an in-flight launch.
rm -f "$PRODUCT_ONE/factory/MAINTENANCE"
LAUNCH_HOLDER_RELEASE="$TMP/queued-launch.release"
/bin/sh -c '
  lock=$1
  release=$2
  mkdir "$lock"
  start=$(ps -o lstart= -p $$ |
    python3 -c "import sys; print(\" \".join(sys.stdin.read().split()))")
  {
    printf "pid=%s\n" "$$"
    printf "process_start=%s\n" "$start"
    printf "nonce=11111111111111111111111111111111\n"
    printf "created_epoch=1\n"
  } > "$lock/owner"
  for _i in $(seq 1 500); do
    [ -f "$release" ] && break
    sleep 0.02
  done
  mv "$lock" "$lock.released"
  rm -rf "$lock.released"
' queued-launch "$PRODUCT_ONE/factory/.launch.lock" "$LAUNCH_HOLDER_RELEASE" &
LAUNCH_HOLDER_PID=$!
for _i in $(seq 1 500); do
  [[ -f "$PRODUCT_ONE/factory/.launch.lock/owner" ]] && break
  sleep 0.02
done
export FACTORY_KIT_LOCK_ATTEMPTS=100
run_kit pause --project alpha --product "$PRODUCT_ONE" \
  > "$TMP/queued-pause.out" 2>&1 &
QUEUED_PAUSE_PID=$!
for _i in $(seq 1 100); do
  [[ -f "$PRODUCT_ONE/factory/MAINTENANCE" ]] && break
  sleep 0.02
done
if [[ -f "$PRODUCT_ONE/factory/MAINTENANCE" &&
      -d "$PRODUCT_ONE/factory/.launch.lock" ]] &&
   kill -0 "$QUEUED_PAUSE_PID" 2>/dev/null &&
   [[ "$(json_value "$PRODUCT_ONE/factory/MAINTENANCE" project)" == "alpha" ]]; then
  pass "pause publishes valid maintenance before waiting on launch lock"
else
  fail "pause publishes valid maintenance before waiting on launch lock"
fi
touch "$LAUNCH_HOLDER_RELEASE"
if wait "$QUEUED_PAUSE_PID"; then
  pass "queued pause acquires launch lock and drains"
else
  fail "queued pause acquires launch lock and drains" "$(<"$TMP/queued-pause.out")"
fi
wait "$LAUNCH_HOLDER_PID" || fail "launch holder exits cleanly"
unset FACTORY_KIT_LOCK_ATTEMPTS

mkdir "$PRODUCT_ONE/factory/.launch.lock"
export FACTORY_KIT_LOCK_ATTEMPTS=2
export FACTORY_KIT_LOCK_OWNER_GRACE_SECONDS=10
expect_failure "young ownerless partial lock observes grace" \
  pause --project alpha --product "$PRODUCT_ONE"
unset FACTORY_KIT_LOCK_ATTEMPTS
export FACTORY_KIT_LOCK_OWNER_GRACE_SECONDS=0
expect_success "ownerless stale lock is atomically recovered" \
  pause --project alpha --product "$PRODUCT_ONE"
unset FACTORY_KIT_LOCK_OWNER_GRACE_SECONDS

rm -f "$PRODUCT_ONE/factory/MAINTENANCE"
mkdir "$PRODUCT_ONE/factory/.launch.lock"
cat > "$PRODUCT_ONE/factory/.launch.lock/owner" <<'EOF'
pid=999999
process_start=stale-process
nonce=22222222222222222222222222222222
created_epoch=1
EOF
export FACTORY_KIT_LOCK_OWNER_GRACE_SECONDS=0
expect_success "dead lock identity is quarantined and recovered" \
  pause --project alpha --product "$PRODUCT_ONE"
unset FACTORY_KIT_LOCK_OWNER_GRACE_SECONDS

mkdir -p "$PRODUCT_ONE/factory/.active-runs"
printf 'pid=999999\n' > "$PRODUCT_ONE/factory/.active-runs/run.pid"
expect_failure "pause leaves maintenance while refusing active run" \
  pause --project alpha --product "$PRODUCT_ONE"
[[ -f "$PRODUCT_ONE/factory/MAINTENANCE" ]] &&
  pass "pause publishes maintenance before drain check" ||
  fail "pause publishes maintenance before drain check"
rm "$PRODUCT_ONE/factory/.active-runs/run.pid"
rmdir "$PRODUCT_ONE/factory/.active-runs"
expect_success "pause is idempotent after drain" \
  pause --project alpha --product "$PRODUCT_ONE"

mkdir -p "$PRODUCT_ONE/factory/.dispatch-leases"
python3 - "$PRODUCT_ONE/factory/.dispatch-leases/T-004.json" <<'PY'
import json, pathlib, time, sys
path = pathlib.Path(sys.argv[1])
path.write_text(json.dumps({
    "schema_version": 1,
    "ticket": "T-004",
    "lease_id": "a" * 64,
    "claimed_epoch": int(time.time()) - 1000,
    "expires_epoch": int(time.time()) - 100,
}) + "\n")
PY
expect_failure "pause refuses an undrained dispatcher lease" \
  pause --project alpha --product "$PRODUCT_ONE"
[[ "$LAST_OUTPUT" == *"MAINTENANCE remains published"* &&
   "$LAST_OUTPUT" == *"recover-lease"* ]] ||
  fail "pause lease refusal names the supported recovery sequence" "$LAST_OUTPUT"
mkdir "$PRODUCT_ONE/factory/.dispatch-leases.lock"
export FACTORY_KIT_LOCK_OWNER_GRACE_SECONDS=0
expect_failure "unpause refuses an undrained dispatcher lease" \
  unpause --project alpha --product "$PRODUCT_ONE"
[[ -f "$PRODUCT_ONE/factory/MAINTENANCE" &&
   -f "$PRODUCT_ONE/factory/.dispatch-leases/T-004.json" ]] ||
  fail "failed unpause preserves maintenance and dispatcher lease"
mkdir "$PRODUCT_ONE/factory/.dispatch-leases.lock"
expect_success "operator recovers stale lease only under maintenance" \
  recover-lease --project alpha --product "$PRODUCT_ONE" --ticket T-004
unset FACTORY_KIT_LOCK_OWNER_GRACE_SECONDS
[[ ! -e "$PRODUCT_ONE/factory/.dispatch-leases/T-004.json" ]] &&
  pass "stale lease recovery removes only the named ticket" ||
  fail "stale lease recovery removes only the named ticket"
[[ ! -e "$PRODUCT_ONE/factory/.dispatch-leases.lock" ]] &&
  pass "stale lease recovery reclaims an abandoned dispatcher lock" ||
  fail "stale lease recovery reclaims an abandoned dispatcher lock"
rm -rf "$PRODUCT_ONE/factory/.dispatch-leases"

mkdir "$PRODUCT_ONE/factory/.provider.lock"
expect_failure "unpause refuses an active provider lock" \
  unpause --project alpha --product "$PRODUCT_ONE"
[[ -f "$PRODUCT_ONE/factory/MAINTENANCE" ]] ||
  fail "provider-lock refusal preserves maintenance"
rmdir "$PRODUCT_ONE/factory/.provider.lock"
mkdir -p "$PRODUCT_ONE/factory/.active-runs"
printf 'pid=999999\n' > "$PRODUCT_ONE/factory/.active-runs/run.pid"
expect_failure "unpause refuses an active run" \
  unpause --project alpha --product "$PRODUCT_ONE"
[[ -f "$PRODUCT_ONE/factory/MAINTENANCE" ]] ||
  fail "failed unpause preserves maintenance"
rm "$PRODUCT_ONE/factory/.active-runs/run.pid"
rmdir "$PRODUCT_ONE/factory/.active-runs"
python3 - "$PRODUCT_ONE/factory/MAINTENANCE" <<'PY'
import json, pathlib, sys
path = pathlib.Path(sys.argv[1])
value = json.loads(path.read_text())
value["cutover_owner"] = "b" * 64
path.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")
PY
expect_failure "unpause preserves release-owned maintenance" \
  unpause --project alpha --product "$PRODUCT_ONE"
python3 - "$PRODUCT_ONE/factory/MAINTENANCE" <<'PY'
import json, pathlib, sys
path = pathlib.Path(sys.argv[1])
value = json.loads(path.read_text())
value.pop("cutover_owner")
path.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")
PY
expect_success "unpause resumes a fully drained product" \
  unpause --project alpha --product "$PRODUCT_ONE"
[[ ! -e "$PRODUCT_ONE/factory/MAINTENANCE" &&
   ! -e "$PRODUCT_ONE/factory/.launch.lock" &&
   ! -e "$PRODUCT_ONE/factory/.dispatch-leases.lock" ]] &&
  pass "unpause removes only its maintenance and locks" ||
  fail "unpause removes only its maintenance and locks"
expect_failure "unpause requires an exact maintenance marker" \
  unpause --project alpha --product "$PRODUCT_ONE"
expect_success "pause can follow a completed unpause" \
  pause --project alpha --product "$PRODUCT_ONE"

# First activation holds the project lock; a concurrent replay cannot read/advance state.
export FACTORY_KIT_TEST_HOLD_PROJECT_LOCK_SECONDS=1
run_kit activate --project alpha --product "$PRODUCT_ONE" --sha "$SHA_A" \
  --receipt "$RECEIPT_A" > "$TMP/activate-first.out" 2>&1 &
FIRST_PID=$!
for _i in $(seq 1 100); do
  [[ -d "$STATE/projects/alpha/.activation.lock" ]] && break
  sleep 0.02
done
unset FACTORY_KIT_TEST_HOLD_PROJECT_LOCK_SECONDS
export FACTORY_KIT_LOCK_ATTEMPTS=2
expect_failure "concurrent activation cannot race receipt or generation reads" \
  activate --project alpha --product "$PRODUCT_ONE" --sha "$SHA_A" \
  --receipt "$RECEIPT_A"
unset FACTORY_KIT_LOCK_ATTEMPTS
if wait "$FIRST_PID"; then
  pass "lock-owning activation completes"
else
  fail "lock-owning activation completes" "$(cat "$TMP/activate-first.out")"
fi
FIRST_PID=""
expect_failure "consumed receipt cannot be replayed" \
  activate --project alpha --product "$PRODUCT_ONE" --sha "$SHA_A" \
  --receipt "$RECEIPT_A"

ACTIVE_ALPHA="$STATE/projects/alpha/active.json"
[[ "$(json_value "$ACTIVE_ALPHA" kit_sha)" == "$SHA_A" ]] &&
  pass "first active generation is release a" ||
  fail "first active generation is release a"

PRODUCT_ONE_ORIGIN="$(git -C "$PRODUCT_ONE" remote get-url origin)"
if python3 - "$ROOT/scripts/lib" <<'PY'
import pathlib, sys
sys.path.insert(0, sys.argv[1])
from historical_pr_objects import same_repository_transition

canonical = "https://github.com/nysa-company/relay-factory.git"
assert same_repository_transition(
    "git@github.com-relay-factory:nysa-company/relay-factory.git", canonical,
)
assert same_repository_transition(
    "ssh://git@github.com/nysa-company/relay-factory", canonical,
)
for unsafe in (
    "git@github.com-relay-factory:nysa-company/different.git",
    "git@github.example:nysa-company/relay-factory.git",
    "/tmp/nysa-company/relay-factory.git",
):
    assert not same_repository_transition(unsafe, canonical)
for unsafe in (
    "git@github.com:nysa-company/relay-factory.git",
    "https://github.com.evil/nysa-company/relay-factory.git",
    "https://user@github.com/nysa-company/relay-factory.git",
    "https://github.com/nysa-company/relay%2dfactory.git",
):
    assert not same_repository_transition(
        "git@github.com-relay-factory:nysa-company/relay-factory.git", unsafe,
    )
PY
then
  pass "origin migration accepts only canonical same-repository transport changes"
else
  fail "origin migration accepts only canonical same-repository transport changes"
fi
git -C "$PRODUCT_ONE" remote set-url origin "file://$PRODUCT_ONE_ORIGIN"
REBOUND_PRODUCT_ORIGIN="file://$PRODUCT_ONE_ORIGIN"
expect_success "active preflight accepts an equivalent certified origin" \
  preflight-report --project alpha --product "$PRODUCT_ONE" --sha "$SHA_A" --json
expect_success "rebound product recertifies against its signed repository" \
  certify --project alpha --product "$PRODUCT_ONE" --sha "$SHA_A"
REBOUND_RECEIPT="$(printf '%s\n' "$LAST_OUTPUT" | awk '/^\// {value=$0} END {print value}')"
[[ "$(json_value "$REBOUND_RECEIPT" product_origin)" == "$REBOUND_PRODUCT_ORIGIN" ]] &&
  pass "rebound receipt binds the new literal origin" ||
  fail "rebound receipt binds the new literal origin"
expect_success "rebound product enters maintenance" \
  pause --project alpha --product "$PRODUCT_ONE"
expect_success "rebound product activates its measured receipt" \
  activate --project alpha --product "$PRODUCT_ONE" --sha "$SHA_A" \
  --receipt "$REBOUND_RECEIPT"
python3 - "$STATE/projects/alpha" "$REBOUND_PRODUCT_ORIGIN" <<'PY' &&
import json, pathlib, sys
state, expected = pathlib.Path(sys.argv[1]), sys.argv[2]
active = json.loads((state / "active.json").read_text())
matches = list((state / "activation-journal").glob(
    f"{active['generation']:020d}-*.json"
))
assert len(matches) == 1
journal = json.loads(matches[0].read_text())
assert journal["phase"] == "committed"
assert journal["receipt_snapshot"]["product_origin"] == expected
PY
  pass "committed activation adopts the new literal origin" ||
  fail "committed activation adopts the new literal origin"
expect_success "subsequent preflight matches the adopted origin exactly" \
  preflight-report --project alpha --product "$PRODUCT_ONE" --sha "$SHA_A" --json
DIFFERENT_PRODUCT_ORIGIN="$TMP/different-product.git"
git init --bare -q "$DIFFERENT_PRODUCT_ORIGIN"
git -C "$PRODUCT_ONE" remote set-url origin "file://$DIFFERENT_PRODUCT_ORIGIN"
expect_failure "active preflight rejects a different repository origin" \
  preflight-report --project alpha --product "$PRODUCT_ONE" --sha "$SHA_A" --json
git -C "$PRODUCT_ONE" remote set-url origin "$PRODUCT_ONE_ORIGIN"

OPERATOR_PRODUCT="$TMP/operator-product"
OPERATOR_REMOTE="$TMP/operator-product.git"
mkdir -p "$OPERATOR_PRODUCT/factory/tickets"
git init --bare -q "$OPERATOR_REMOTE"
git -C "$OPERATOR_PRODUCT" init -q -b main
git -C "$OPERATOR_PRODUCT" config user.name "Factory Test"
git -C "$OPERATOR_PRODUCT" config user.email "test@local"
git -C "$OPERATOR_PRODUCT" remote add origin "$OPERATOR_REMOTE"
printf '%s\n' 'factory/operator-map.json' 'factory/.operator-map.lock' \
  'factory/.operator-clears/' > "$OPERATOR_PRODUCT/.gitignore"
printf '%s\n' '# T-777' 'State: Backlog' 'Priority: normal' > \
  "$OPERATOR_PRODUCT/factory/tickets/T-777.md"
git -C "$OPERATOR_PRODUCT" add -A
git -C "$OPERATOR_PRODUCT" commit -q -m "seed backlog ticket for operator authority"
git -C "$OPERATOR_PRODUCT" push -qu origin main
expect_success "operator ready issues a one-use receipt and projects the map" \
  operator ready --project alpha --product "$OPERATOR_PRODUCT" --ticket T-777
python3 - "$STATE/projects/alpha/controller" "$OPERATOR_PRODUCT" <<'PY'
import json, pathlib, sys
state, product = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
receipt_path = state / "operator-receipts/T-777/ready-1.json"
receipt = json.loads(receipt_path.read_text())
assert receipt["schema"] == "nysa.software-factory.operator-receipt/v1"
assert receipt["consumed"] is True
mapping = json.loads((product / "factory/operator-map.json").read_text())
assert sorted(mapping) == ["_config", "_sync", "initiatives", "tickets"]
assert "operator" not in mapping["tickets"]["T-777"]
import subprocess
audit = json.loads(subprocess.run([
    "git", "-C", str(product), "show",
    "refs/remotes/origin/ticket/T-777:factory/receipts/T-777/ready-1.json",
], check=True, capture_output=True, text=True).stdout)
assert audit["audit"] == "no-authority"
assert "nonce" not in audit
assert audit["receipt_sha256"] == receipt["receipt_sha256"]
ticket = subprocess.run([
    "git", "-C", str(product), "show",
    "refs/remotes/origin/ticket/T-777:factory/tickets/T-777.md",
], check=True, capture_output=True, text=True).stdout
assert "State: Ready" in ticket
PY
expect_success "operator priority remains a pending one-use receipt" \
  operator priority --project alpha --product "$OPERATOR_PRODUCT" \
  --ticket T-777 --priority high
expect_success "operator pending lists the open receipt" \
  operator pending --project alpha --product "$OPERATOR_PRODUCT"
[[ "$LAST_OUTPUT" == *'"ticket": "T-777"'* ]] &&
  pass "pending output names the open receipt" ||
  fail "pending output names the open receipt" "$LAST_OUTPUT"
expect_failure "operator approve refuses a ticket outside Awaiting Approval" \
  operator approve --project alpha --product "$PRODUCT_ONE" --ticket T-777

NONCANONICAL_PRODUCT="$TMP/product-one-noncanonical"
git -C "$PRODUCT_ONE" worktree add -q --detach "$NONCANONICAL_PRODUCT" main
expect_failure "active product certification rejects a noncanonical worktree in preflight" \
  certify --project alpha --product "$NONCANONICAL_PRODUCT" --sha "$SHA_A"
[[ "$LAST_OUTPUT" == *"certification_preflight_product_binding"* ]] &&
  pass "active product path mismatch is typed before product phases" ||
  fail "active product path mismatch is typed before product phases" "$LAST_OUTPUT"
git -C "$PRODUCT_ONE" worktree remove -f "$NONCANONICAL_PRODUCT"

set_ticket_lease "$PRODUCT_ONE" "$SHA_B"
printf '%s\n' '# T-009' 'State: Approved' 'Operator-Approval: Linear' \
  "Kit-SHA: $SHA_A" > "$PRODUCT_ONE/factory/tickets/T-009.md"
commit_all "$PRODUCT_ONE" "lease registered planning ticket to release b"
push_main "$PRODUCT_ONE"
LEASE_BRANCH_WORKTREE="$TMP/product-one-ticket-T-006"
git -C "$PRODUCT_ONE" worktree add -q -b ticket/T-006 \
  "$LEASE_BRANCH_WORKTREE" main
printf '%s\n' '# T-006' 'State: Planning' "Kit-SHA: $SHA_A" > \
  "$LEASE_BRANCH_WORKTREE/factory/tickets/T-006.md"
mkdir -p "$LEASE_BRANCH_WORKTREE/factory/route-plans"
python3 - "$ROOT/scripts/model-router.py" \
  "$LEASE_BRANCH_WORKTREE/factory/route-plans/T-006.json" "$SHA_A" <<'PY'
import importlib.util, json, pathlib, sys
spec = importlib.util.spec_from_file_location("fixture_router", sys.argv[1])
router = importlib.util.module_from_spec(spec)
spec.loader.exec_module(router)
catalog, routes, _, profiles = router.load_policy()
readiness = {
    route_id: {
        "adapter_version": "test-v1", "reason": "test",
        "reported_identity": route["expected_reported_identity"], "state": "READY",
    }
    for route_id, route in routes.items() if route["enabled"]
}
resolution = router.resolve_policy(
    catalog, routes, profiles["legacy-balanced-v1"], readiness,
)
path = pathlib.Path(sys.argv[2])
path.write_text(json.dumps({
    "created_at": "2026-07-21T00:00:00Z", "kit_sha": sys.argv[3],
    "resolution": resolution, "schema": "ticket-model-route-plan/v1",
    "ticket": "T-006",
}, sort_keys=True, separators=(",", ":")) + "\n")
PY
commit_all "$LEASE_BRANCH_WORKTREE" "add branch-only ticket lease fixture"
git -C "$LEASE_BRANCH_WORKTREE" push -q -u origin ticket/T-006
set_pin "$PRODUCT_ONE" "$SHA_B"
expect_success "candidate with unattested old approved ticket can certify" \
  certify --project alpha --product "$PRODUCT_ONE" --sha "$SHA_B"
RECEIPT_UNATTESTED_APPROVED="$(printf '%s\n' "$LAST_OUTPUT" | awk '/^\// {value=$0} END {print value}')"
expect_failure "old approved ticket without protected attestations blocks activation" \
  activate --project alpha --product "$PRODUCT_ONE" --sha "$SHA_B" \
  --receipt "$RECEIPT_UNATTESTED_APPROVED"

git -C "$PRODUCT_ONE" rm -q factory/tickets/T-009.md
commit_all "$PRODUCT_ONE" "remove unattested approved ticket fixture"
push_main "$PRODUCT_ONE"
expect_success "candidate with old ticket lease can certify" \
  certify --project alpha --product "$PRODUCT_ONE" --sha "$SHA_B"
RECEIPT_WRONG_LEASE="$(printf '%s\n' "$LAST_OUTPUT" | awk '/^\// {value=$0} END {print value}')"
STALE_TERMINAL_REF="$(
  git -C "$PRODUCT_ONE" ls-remote --heads origin refs/heads/ticket/T-006 |
    awk '{print $1}'
)"
expect_success "protected canceled truth ignores a stale nonterminal ticket ref" \
  plan --project alpha --product "$PRODUCT_ONE" --sha "$SHA_B" \
  --receipt "$RECEIPT_WRONG_LEASE"
[[ "$STALE_TERMINAL_REF" == "$(
    git -C "$PRODUCT_ONE" ls-remote --heads origin refs/heads/ticket/T-006 |
      awk '{print $1}'
  )" ]] ||
  fail "terminal truth validation must not mutate a qualification ticket ref"
TERMINAL_TICKET_FIXTURE="$TMP/t006-terminal.md"
cp "$PRODUCT_ONE/factory/tickets/T-006.md" "$TERMINAL_TICKET_FIXTURE"
printf '%s\n' 'State: Ready' > "$PRODUCT_ONE/factory/tickets/T-006.md"
commit_all "$PRODUCT_ONE" "make ticket branch authoritative again"
push_main "$PRODUCT_ONE"
expect_success "nonterminal protected ticket tuple recertifies" \
  certify --project alpha --product "$PRODUCT_ONE" --sha "$SHA_B"
RECEIPT_WRONG_LEASE="$(printf '%s\n' "$LAST_OUTPUT" | awk '/^\// {value=$0} END {print value}')"
expect_failure "different nonterminal ticket lease blocks activation" \
  activate --project alpha --product "$PRODUCT_ONE" --sha "$SHA_B" \
  --receipt "$RECEIPT_WRONG_LEASE"

git -C "$PRODUCT_ONE" worktree remove -f "$LEASE_BRANCH_WORKTREE"
git -C "$PRODUCT_ONE" branch -D ticket/T-006 >/dev/null
git -C "$PRODUCT_ONE" update-ref -d refs/remotes/origin/ticket/T-006
expect_failure "remote-only nonterminal ticket lease blocks activation" \
  activate --project alpha --product "$PRODUCT_ONE" --sha "$SHA_B" \
  --receipt "$RECEIPT_WRONG_LEASE"
git -C "$PRODUCT_ONE" fetch -q origin \
  refs/heads/ticket/T-006:refs/heads/ticket/T-006
git -C "$PRODUCT_ONE" worktree add -q "$LEASE_BRANCH_WORKTREE" ticket/T-006
git -C "$LEASE_BRANCH_WORKTREE" branch --set-upstream-to=origin/ticket/T-006 \
  ticket/T-006 >/dev/null 2>&1 || true
sed "s/^Kit-SHA: .*$/Kit-SHA: $SHA_B/" \
  "$LEASE_BRANCH_WORKTREE/factory/tickets/T-006.md" > \
  "$LEASE_BRANCH_WORKTREE/factory/tickets/T-006.tmp"
mv "$LEASE_BRANCH_WORKTREE/factory/tickets/T-006.tmp" \
  "$LEASE_BRANCH_WORKTREE/factory/tickets/T-006.md"
commit_all "$LEASE_BRANCH_WORKTREE" "move authoritative ticket lease to release b"
git -C "$LEASE_BRANCH_WORKTREE" push -q origin ticket/T-006
LEASE_BRANCH_REMOTE="$(git -C "$PRODUCT_ONE" remote get-url origin)"
sed "s/^Kit-SHA: .*$/Kit-SHA: $SHA_A/" \
  "$LEASE_BRANCH_WORKTREE/factory/tickets/T-006.md" > \
  "$LEASE_BRANCH_WORKTREE/factory/tickets/T-006.tmp"
mv "$LEASE_BRANCH_WORKTREE/factory/tickets/T-006.tmp" \
  "$LEASE_BRANCH_WORKTREE/factory/tickets/T-006.md"
commit_all "$LEASE_BRANCH_WORKTREE" "advance remote outside trusted tracking update"
git -C "$LEASE_BRANCH_WORKTREE" push -q origin \
  HEAD:refs/heads/factory-test-stale-ticket
git --git-dir="$LEASE_BRANCH_REMOTE" update-ref refs/heads/ticket/T-006 \
  "$(git -C "$LEASE_BRANCH_WORKTREE" rev-parse HEAD)"
git --git-dir="$LEASE_BRANCH_REMOTE" update-ref -d \
  refs/heads/factory-test-stale-ticket
expect_failure "stale remote-tracking ticket lease blocks activation" \
  activate --project alpha --product "$PRODUCT_ONE" --sha "$SHA_B" \
  --receipt "$RECEIPT_WRONG_LEASE"
sed "s/^Kit-SHA: .*$/Kit-SHA: $SHA_B/" \
  "$LEASE_BRANCH_WORKTREE/factory/tickets/T-006.md" > \
  "$LEASE_BRANCH_WORKTREE/factory/tickets/T-006.tmp"
mv "$LEASE_BRANCH_WORKTREE/factory/tickets/T-006.tmp" \
  "$LEASE_BRANCH_WORKTREE/factory/tickets/T-006.md"
commit_all "$LEASE_BRANCH_WORKTREE" "restore verified authoritative ticket lease"
git -C "$LEASE_BRANCH_WORKTREE" push -q origin ticket/T-006
git -C "$PRODUCT_ONE" branch ticket/T-007 main
expect_failure "local-only ticket branch blocks activation" \
  activate --project alpha --product "$PRODUCT_ONE" --sha "$SHA_B" \
  --receipt "$RECEIPT_WRONG_LEASE"
git -C "$PRODUCT_ONE" branch -D ticket/T-007 >/dev/null
git -C "$PRODUCT_ONE" branch ticket/T-008 main
git -C "$PRODUCT_ONE" push -q -u origin ticket/T-008
expect_failure "ticket branch missing its canonical file blocks activation" \
  activate --project alpha --product "$PRODUCT_ONE" --sha "$SHA_B" \
  --receipt "$RECEIPT_WRONG_LEASE"
git -C "$PRODUCT_ONE" push -q origin --delete ticket/T-008
git -C "$PRODUCT_ONE" branch -D ticket/T-008 >/dev/null

# A protected-main authorization may bridge only the exact old pinned branch
# heads named for one candidate. It does not relax the lease drain barrier.
sed "s/^Kit-SHA: .*$/Kit-SHA: $SHA_A/" \
  "$LEASE_BRANCH_WORKTREE/factory/tickets/T-006.md" > \
  "$LEASE_BRANCH_WORKTREE/factory/tickets/T-006.tmp"
mv "$LEASE_BRANCH_WORKTREE/factory/tickets/T-006.tmp" \
  "$LEASE_BRANCH_WORKTREE/factory/tickets/T-006.md"
commit_all "$LEASE_BRANCH_WORKTREE" "restore old ticket for authorized migration"
git -C "$LEASE_BRANCH_WORKTREE" push -q origin ticket/T-006
INFLIGHT_AUTH="$PRODUCT_ONE/factory/migrations/inflight-release/$SHA_B.json"
write_inflight_authorization \
  "$PRODUCT_ONE" "$SHA_A" "$SHA_B" T-006 "$(printf '0%.0s' {1..40})" Planning
commit_all "$PRODUCT_ONE" "add wrong-head in-flight authorization fixture"
push_main "$PRODUCT_ONE"
expect_success "wrong-head in-flight tuple certifies" \
  certify --project alpha --product "$PRODUCT_ONE" --sha "$SHA_B"
RECEIPT_WRONG_AUTH="$(printf '%s\n' "$LAST_OUTPUT" | awk '/^\// {value=$0} END {print value}')"
expect_failure "wrong authorized remote head blocks activation" \
  activate --project alpha --product "$PRODUCT_ONE" --sha "$SHA_B" \
  --receipt "$RECEIPT_WRONG_AUTH"
expect_failure "plan and activation reject the same wrong authorized remote head" \
  plan --project alpha --product "$PRODUCT_ONE" --sha "$SHA_B" \
  --receipt "$RECEIPT_WRONG_AUTH"
if [[ "$LAST_OUTPUT" == *"T-006 does not match its exact in-flight release authorization"* &&
      "$LAST_OUTPUT" == *"expected branch=ticket/T-006"* &&
      "$LAST_OUTPUT" == *"head=$(printf '0%.0s' {1..40})"* &&
      "$LAST_OUTPUT" == *"state=Planning"* &&
      "$LAST_OUTPUT" == *"source_kit_sha=$SHA_A"* ]]; then
  pass "in-flight mismatch names the exact remediation inputs"
else
  fail "in-flight mismatch reports an actionable exact plan" "$LAST_OUTPUT"
fi

VALID_INFLIGHT_PLAN="$TMP/t006-valid-route-plan.json"
cp "$LEASE_BRANCH_WORKTREE/factory/route-plans/T-006.json" "$VALID_INFLIGHT_PLAN"
git -C "$LEASE_BRANCH_WORKTREE" rm -q factory/route-plans/T-006.json
commit_all "$LEASE_BRANCH_WORKTREE" "remove in-flight route plan fixture"
git -C "$LEASE_BRANCH_WORKTREE" push -q origin ticket/T-006
INFLIGHT_HEAD="$(git -C "$LEASE_BRANCH_WORKTREE" rev-parse HEAD)"
write_inflight_authorization \
  "$PRODUCT_ONE" "$SHA_A" "$SHA_B" T-006 "$INFLIGHT_HEAD" Planning
commit_all "$PRODUCT_ONE" "authorize missing in-flight route plan fixture"
push_main "$PRODUCT_ONE"
expect_success "missing-plan in-flight tuple certifies" \
  certify --project alpha --product "$PRODUCT_ONE" --sha "$SHA_B"
RECEIPT_MISSING_PLAN="$(printf '%s\n' "$LAST_OUTPUT" | awk '/^\// {value=$0} END {print value}')"
expect_failure "authorized ticket missing its v1 route plan blocks activation" \
  activate --project alpha --product "$PRODUCT_ONE" --sha "$SHA_B" \
  --receipt "$RECEIPT_MISSING_PLAN"

mkdir -p "$LEASE_BRANCH_WORKTREE/factory/route-plans"
printf '{"kit_sha":"%s","revisions":[],"schema":"ticket-model-route-journal/v2","ticket":"T-006"}\n' \
  "$SHA_A" > "$LEASE_BRANCH_WORKTREE/factory/route-plans/T-006.json"
commit_all "$LEASE_BRANCH_WORKTREE" "add v2 in-flight route journal fixture"
git -C "$LEASE_BRANCH_WORKTREE" push -q origin ticket/T-006
INFLIGHT_HEAD="$(git -C "$LEASE_BRANCH_WORKTREE" rev-parse HEAD)"
write_inflight_authorization \
  "$PRODUCT_ONE" "$SHA_A" "$SHA_B" T-006 "$INFLIGHT_HEAD" Planning
commit_all "$PRODUCT_ONE" "authorize v2 in-flight route journal fixture"
push_main "$PRODUCT_ONE"
expect_success "v2-plan in-flight tuple certifies" \
  certify --project alpha --product "$PRODUCT_ONE" --sha "$SHA_B"
RECEIPT_V2_PLAN="$(printf '%s\n' "$LAST_OUTPUT" | awk '/^\// {value=$0} END {print value}')"
expect_failure "authorized ticket with a malformed v2 journal blocks cutover" \
  activate --project alpha --product "$PRODUCT_ONE" --sha "$SHA_B" \
  --receipt "$RECEIPT_V2_PLAN"

cp "$VALID_INFLIGHT_PLAN" \
  "$LEASE_BRANCH_WORKTREE/factory/route-plans/T-006.json"
python3 - "$LEASE_BRANCH_WORKTREE/factory/route-plans/T-006.json" "$SHA_B" <<'PY'
import json, pathlib, sys
path = pathlib.Path(sys.argv[1])
value = json.loads(path.read_text())
value["kit_sha"] = sys.argv[2]
path.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")
PY
commit_all "$LEASE_BRANCH_WORKTREE" "add wrong-source in-flight route plan fixture"
git -C "$LEASE_BRANCH_WORKTREE" push -q origin ticket/T-006
INFLIGHT_HEAD="$(git -C "$LEASE_BRANCH_WORKTREE" rev-parse HEAD)"
write_inflight_authorization \
  "$PRODUCT_ONE" "$SHA_A" "$SHA_B" T-006 "$INFLIGHT_HEAD" Planning
commit_all "$PRODUCT_ONE" "authorize wrong-source in-flight route plan fixture"
push_main "$PRODUCT_ONE"
expect_success "wrong-source-plan in-flight tuple certifies" \
  certify --project alpha --product "$PRODUCT_ONE" --sha "$SHA_B"
RECEIPT_WRONG_PLAN_SOURCE="$(printf '%s\n' "$LAST_OUTPUT" | awk '/^\// {value=$0} END {print value}')"
expect_failure "authorized route plan from another source kit blocks activation" \
  activate --project alpha --product "$PRODUCT_ONE" --sha "$SHA_B" \
  --receipt "$RECEIPT_WRONG_PLAN_SOURCE"

python3 - "$ROOT/scripts/model-manager.py" "$VALID_INFLIGHT_PLAN" \
  "$LEASE_BRANCH_WORKTREE/factory/route-plans/T-006.json" "$SHA_A" <<'PY'
import importlib.util, json, pathlib, sys
manager_path, source, output, target = sys.argv[1:]
spec = importlib.util.spec_from_file_location("fixture_manager", manager_path)
manager = importlib.util.module_from_spec(spec)
spec.loader.exec_module(manager)
legacy = json.loads(pathlib.Path(source).read_text())
legacy["kit_sha"] = "3" * 40
catalog, routes, _, profiles = manager.ROUTER.load_policy()
resolution = legacy["resolution"]
resolution["selections"]["spec-linter"]["adapter_version"] = "test-old"
resolution["selections"]["spec-linter"]["reported_identity"] = (
    "Historical Cursor identity"
)
profile = profiles[resolution["profile_id"]]
portfolio = next(
    item for item in profile["portfolios"]
    if item["portfolio_id"] == resolution["portfolio_id"]
)
resolution["policy_hash"] = manager.ROUTER._policy_hash(
    resolution["catalog_hash"], resolution["profile_hash"], portfolio,
    resolution["selections"],
)
raw = (json.dumps(legacy, sort_keys=True, separators=(",", ":")) + "\n").encode()
journal = manager.migrate_v1_plan(
    raw, "4" * 40, target, "2026-07-21T00:00:00Z",
    catalog, routes, profiles,
)
pathlib.Path(output).write_text(
    json.dumps(journal, sort_keys=True, separators=(",", ":")) + "\n"
)
PY
sed 's/^State: Planning$/State: Blocked-Escalated/' \
  "$LEASE_BRANCH_WORKTREE/factory/tickets/T-006.md" > \
  "$LEASE_BRANCH_WORKTREE/factory/tickets/T-006.tmp"
mv "$LEASE_BRANCH_WORKTREE/factory/tickets/T-006.tmp" \
  "$LEASE_BRANCH_WORKTREE/factory/tickets/T-006.md"
printf '%s\n' 'Resume-State: Planning' >> \
  "$LEASE_BRANCH_WORKTREE/factory/tickets/T-006.md"
commit_all "$LEASE_BRANCH_WORKTREE" "add blocked migratable v2 in-flight ticket"
git -C "$LEASE_BRANCH_WORKTREE" push -q origin ticket/T-006
INFLIGHT_HEAD="$(git -C "$LEASE_BRANCH_WORKTREE" rev-parse HEAD)"
write_inflight_authorization \
  "$PRODUCT_ONE" "$SHA_A" "$SHA_B" T-006 "$INFLIGHT_HEAD" Blocked-Escalated
commit_all "$PRODUCT_ONE" "authorize exact in-flight ticket head"
push_main "$PRODUCT_ONE"
expect_success "authorized historical-identity v2 in-flight tuple certifies" \
  certify --project alpha --product "$PRODUCT_ONE" --sha "$SHA_B"
RECEIPT_B="$(printf '%s\n' "$LAST_OUTPUT" | awk '/^\// {value=$0} END {print value}')"
[[ "$(json_value "$RECEIPT_B" expected_previous_generation)" == "$(json_value "$ACTIVE_ALPHA" generation)" ]] &&
  pass "receipt binds expected previous generation" ||
  fail "receipt binds expected previous generation"

git -C "$PRODUCT_ONE" branch ticket/T-092-slice main
git -C "$PRODUCT_ONE" push -q origin ticket/T-092-slice
expect_success "plan ignores noncanonical slice branches outside deterministic ticket state" \
  plan --project alpha --product "$PRODUCT_ONE" --sha "$SHA_B" \
  --receipt "$RECEIPT_B"

mkdir -p "$PRODUCT_ONE/factory/.dispatch-leases"
printf '{}\n' > "$PRODUCT_ONE/factory/.dispatch-leases/T-006.json"
expect_failure "in-flight authorization cannot retain a dispatcher lease" \
  activate --project alpha --product "$PRODUCT_ONE" --sha "$SHA_B" \
  --receipt "$RECEIPT_B"
rm "$PRODUCT_ONE/factory/.dispatch-leases/T-006.json"
rmdir "$PRODUCT_ONE/factory/.dispatch-leases"

export FACTORY_KIT_FAIL_AFTER_PHASE=receipt_claimed
expect_failure "fault injection interrupts after receipt claim" \
  activate --project alpha --product "$PRODUCT_ONE" --sha "$SHA_B" \
  --receipt "$RECEIPT_B"
unset FACTORY_KIT_FAIL_AFTER_PHASE
git -C "$PRODUCT_ONE" push -q origin --delete ticket/T-092-slice
git -C "$PRODUCT_ONE" branch -D ticket/T-092-slice >/dev/null
RECEIPT_B_ID="$(json_value "$RECEIPT_B" receipt_id)"
if [[ "$(json_value "$ACTIVE_ALPHA" kit_sha)" == "$SHA_A" &&
      -f "$STATE/receipts/consumed/$RECEIPT_B_ID.json" ]]; then
  pass "receipt claim is journaled before active pointer switch"
else
  fail "receipt claim is journaled before active pointer switch"
fi
expect_success "reconcile completes claimed pre-pointer transaction" \
  reconcile --project alpha --product "$PRODUCT_ONE"
[[ "$(json_value "$ACTIVE_ALPHA" kit_sha)" == "$SHA_B" ]] &&
  pass "reconcile commits release b" ||
  fail "reconcile commits release b"

# Existing sealed models migrate performs this step after activation.
migrate_v2_fixture \
  "$LEASE_BRANCH_WORKTREE/factory/route-plans/T-006.json" "$SHA_B"
sed "s/^Kit-SHA: .*$/Kit-SHA: $SHA_B/" \
  "$LEASE_BRANCH_WORKTREE/factory/tickets/T-006.md" > \
  "$LEASE_BRANCH_WORKTREE/factory/tickets/T-006.tmp"
mv "$LEASE_BRANCH_WORKTREE/factory/tickets/T-006.tmp" \
  "$LEASE_BRANCH_WORKTREE/factory/tickets/T-006.md"
commit_all "$LEASE_BRANCH_WORKTREE" "simulate sealed ticket route migration"
git -C "$LEASE_BRANCH_WORKTREE" push -q origin ticket/T-006
grep -q '^State: Blocked-Escalated$' \
  "$LEASE_BRANCH_WORKTREE/factory/tickets/T-006.md" &&
  grep -q '^Resume-State: Planning$' \
    "$LEASE_BRANCH_WORKTREE/factory/tickets/T-006.md" &&
  pass "in-flight migration preserves blocked state" ||
  fail "in-flight migration preserves blocked state"
python3 - "$LEASE_BRANCH_WORKTREE/factory/route-plans/T-006.json" "$SHA_B" <<'PY' &&
import json, sys
value = json.load(open(sys.argv[1]))
assert value["kit_sha"] == sys.argv[2]
assert value["revisions"][-1]["body"]["kind"] == "release-migration"
PY
  pass "in-flight v2 migration appends release affinity" ||
  fail "in-flight v2 migration appends release affinity"

expect_failure "rollback refuses unreverted product tuple" \
  rollback --project alpha --product "$PRODUCT_ONE"
migrate_v2_fixture \
  "$LEASE_BRANCH_WORKTREE/factory/route-plans/T-006.json" "$SHA_A"
sed "s/^Kit-SHA: .*$/Kit-SHA: $SHA_A/" \
  "$LEASE_BRANCH_WORKTREE/factory/tickets/T-006.md" > \
  "$LEASE_BRANCH_WORKTREE/factory/tickets/T-006.tmp"
mv "$LEASE_BRANCH_WORKTREE/factory/tickets/T-006.tmp" \
  "$LEASE_BRANCH_WORKTREE/factory/tickets/T-006.md"
commit_all "$LEASE_BRANCH_WORKTREE" "restore authoritative ticket lease for rollback"
git -C "$LEASE_BRANCH_WORKTREE" push -q origin ticket/T-006
git -C "$PRODUCT_ONE" rm -q \
  "factory/migrations/inflight-release/$SHA_B.json"
cp "$TERMINAL_TICKET_FIXTURE" "$PRODUCT_ONE/factory/tickets/T-006.md"
restore_product_tuple "$PRODUCT_ONE" "$SHA_A"
printf '%s\n\n' "$SHA_A" > "$PRODUCT_ONE/factory/KIT_PIN"
expect_failure "rollback rejects KIT_PIN blank-line extras" \
  rollback --project alpha --product "$PRODUCT_ONE"
printf '%s\n' "$SHA_A" > "$PRODUCT_ONE/factory/KIT_PIN"
python3 - "$STATE/contract-cutover-journal.json" <<'PY'
import hashlib, json, os, pathlib, sys
body = {
    "approval_sha256": "a" * 64,
    "completed_projects": ["alpha"],
    "floor_required": True,
    "phase": "healthy",
    "schema": "nysa.software-factory.host-cutover-journal/v1",
    "status": "pass",
}
body["record_sha256"] = hashlib.sha256(
    (json.dumps(body, sort_keys=True, separators=(",", ":")) + "\n").encode()
).hexdigest()
path = pathlib.Path(sys.argv[1])
path.write_text(json.dumps(body, sort_keys=True) + "\n")
os.chmod(path, 0o600)
PY
RACE_LOCK_READY="$TMP/cutover-lock-ready"
RACE_LOCK_RELEASE="$TMP/cutover-lock-release"
python3 - "$STATE/.contract-cutover.lock" "$RACE_LOCK_READY" "$RACE_LOCK_RELEASE" <<'PY' &
import fcntl, os, pathlib, time, sys
path, ready, release = map(pathlib.Path, sys.argv[1:])
descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
fcntl.flock(descriptor, fcntl.LOCK_EX)
ready.touch()
while not release.exists():
    time.sleep(0.01)
os.close(descriptor)
PY
LOCK_HOLDER_PID=$!
for _i in $(seq 1 100); do
  [[ -e "$RACE_LOCK_READY" ]] && break
  sleep 0.01
done
exec 8<> "$STATE/.contract-cutover.lock"
FACTORY_HOST_CUTOVER_LOCK_FD=8 expect_failure \
  "an unlocked inherited descriptor cannot forge the host lock capability" \
  pause --project alpha --product "$PRODUCT_ONE"
[[ "$LAST_OUTPUT" == *"host cutover lock capability is invalid"* ]] ||
  fail "fake host lock capability reports the exact boundary" "$LAST_OUTPUT"
exec 8>&-
run_kit pause --project alpha --product "$PRODUCT_ONE" \
  > "$TMP/cutover-race.out" 2>&1 &
FIRST_PID=$!
sleep 0.1
kill -0 "$FIRST_PID" 2>/dev/null &&
  pass "public project mutation waits for the host cutover lock" ||
  fail "public project mutation waits for the host cutover lock" \
    "$(cat "$TMP/cutover-race.out")"
python3 - "$STATE/contract-cutover-reservation.json" <<'PY'
import hashlib, json, os, pathlib, sys
body = {
    "active_projects": [{"project": "alpha"}],
    "approval_sha256": "b" * 64,
    "reservation_id": "c" * 64,
    "schema": "nysa.software-factory.host-cutover-reservation/v1",
    "status": "prepared",
}
body["record_sha256"] = hashlib.sha256(
    (json.dumps(body, sort_keys=True, separators=(",", ":")) + "\n").encode()
).hexdigest()
path = pathlib.Path(sys.argv[1])
path.write_text(json.dumps(body, sort_keys=True) + "\n")
os.chmod(path, 0o600)
PY
touch "$RACE_LOCK_RELEASE"
wait "$LOCK_HOLDER_PID"
if wait "$FIRST_PID"; then
  fail "reservation publication wins the setup-versus-mutation race" \
    "command unexpectedly succeeded"
else
  [[ "$(cat "$TMP/cutover-race.out")" == *"host cutover reservation blocks project mutation"* ]] &&
    pass "reservation publication wins the setup-versus-mutation race" ||
    fail "reservation publication wins the setup-versus-mutation race" \
      "$(cat "$TMP/cutover-race.out")"
fi
FIRST_PID=""
expect_failure "host reservation blocks direct project rollback" \
  rollback --project alpha --product "$PRODUCT_ONE"
[[ "$LAST_OUTPUT" == *"host cutover reservation blocks project mutation"* ]] ||
  fail "rollback reports the host reservation boundary" "$LAST_OUTPUT"
rm "$STATE/contract-cutover-reservation.json"
rm -f "$STATE/contract-floor.json"
expect_failure "rollback fails closed when a completed cutover loses its floor" \
  rollback --project alpha --product "$PRODUCT_ONE"
python3 - "$STATE/contract-floor.json" <<'PY'
import json, os, pathlib, sys
path = pathlib.Path(sys.argv[1])
path.write_text(json.dumps({
    "minimum_major": 2,
    "schema": "nysa.software-factory.contract-floor/v1",
}, sort_keys=True) + "\n")
os.chmod(path, 0o600)
PY
expect_success "rollback accepts normally committed previous product tree" \
  rollback --project alpha --product "$PRODUCT_ONE"
if [[ "$(json_value "$ACTIVE_ALPHA" kit_sha)" == "$SHA_A" ]] &&
   run_kit status --project alpha --product "$PRODUCT_ONE" --json >/dev/null 2>&1; then
  pass "rollback restores a launcher-valid runtime tuple"
else
  fail "rollback restores a launcher-valid runtime tuple"
fi

PRODUCT_TWO="$(make_product product-two)"
printf '%s\n' "$SHA_B" > "$PRODUCT_TWO/factory/KIT_PIN"
set_ticket_lease "$PRODUCT_TWO" "$SHA_B"
commit_all "$PRODUCT_TWO" "prepare independent product b tuple"
push_main "$PRODUCT_TWO"
expect_success "second product certifies independently" \
  certify --project beta --product "$PRODUCT_TWO" --sha "$SHA_B"
RECEIPT_TWO="$(printf '%s\n' "$LAST_OUTPUT" | awk '/^\// {value=$0} END {print value}')"
expect_success "second product pauses independently" \
  pause --project beta --product "$PRODUCT_TWO"
RECEIPT_TWO_COPY="$TMP/receipt-two.copy"
cp "$RECEIPT_TWO" "$RECEIPT_TWO_COPY"
rm "$RECEIPT_TWO"
ln -s "$RECEIPT_TWO_COPY" "$RECEIPT_TWO"
expect_failure "receipt symlink escape is rejected" \
  activate --project beta --product "$PRODUCT_TWO" --sha "$SHA_B" \
  --receipt "$RECEIPT_TWO"
rm "$RECEIPT_TWO"
cp "$RECEIPT_TWO_COPY" "$RECEIPT_TWO"
chmod 600 "$RECEIPT_TWO"
export FACTORY_KIT_FAIL_AFTER_PHASE=prepared
expect_failure "fault injection interrupts before receipt claim" \
  activate --project beta --product "$PRODUCT_TWO" --sha "$SHA_B" \
  --receipt "$RECEIPT_TWO"
unset FACTORY_KIT_FAIL_AFTER_PHASE
RECEIPT_TWO_ID="$(json_value "$RECEIPT_TWO" receipt_id)"
if [[ ! -e "$STATE/receipts/consumed/$RECEIPT_TWO_ID.json" ]]; then
  pass "prepared journal is durable before receipt consumption"
else
  fail "prepared journal is durable before receipt consumption"
fi
expect_success "reconcile claims receipt from prepared journal" \
  reconcile --project beta --product "$PRODUCT_TWO"
ACTIVE_BETA="$STATE/projects/beta/active.json"
if [[ "$(json_value "$ACTIVE_ALPHA" kit_sha)" == "$SHA_A" &&
      "$(json_value "$ACTIVE_BETA" kit_sha)" == "$SHA_B" ]]; then
  pass "per-product activation state is isolated"
else
  fail "per-product activation state is isolated"
fi

expect_success "fresh beta tuple recertifies for read-only plan" \
  certify --project beta --product "$PRODUCT_TWO" --sha "$SHA_B"
RECEIPT_TWO_PLAN="$(printf '%s\n' "$LAST_OUTPUT" | awk '/^\// {value=$0} END {print value}')"
BEFORE_READ_ONLY="$(state_snapshot)"
expect_success "plan remains read-only" \
  plan --project beta --product "$PRODUCT_TWO" --sha "$SHA_B" \
  --receipt "$RECEIPT_TWO_PLAN"
run_kit status --project beta --product "$PRODUCT_TWO" --json >/dev/null 2>&1 ||
  fail "status validates manifest-backed active release"
AFTER_READ_ONLY="$(state_snapshot)"
[[ "$BEFORE_READ_ONLY" == "$AFTER_READ_ONLY" ]] &&
  pass "plan and status do not mutate managed state" ||
  fail "plan and status do not mutate managed state"

PRODUCT_REPLAY="$(make_product product-replay)"
set_pin "$PRODUCT_REPLAY" "$SHA_A"
set_ticket_lease "$PRODUCT_REPLAY" "$SHA_A"
commit_all "$PRODUCT_REPLAY" "prepare replay product"
push_main "$PRODUCT_REPLAY"
expect_success "replay product certifies fully once" \
  certify --project replay --product "$PRODUCT_REPLAY" --sha "$SHA_A"
REPLAY_BASE_RECEIPT="$(printf '%s\n' "$LAST_OUTPUT" | awk '/^\// {value=$0} END {print value}')"
expect_success "replay product enters maintenance" \
  pause --project replay --product "$PRODUCT_REPLAY"
expect_success "replay product activates its measured receipt" \
  activate --project replay --product "$PRODUCT_REPLAY" --sha "$SHA_A" \
  --receipt "$REPLAY_BASE_RECEIPT"

printf '%s\n' 'Planning note: control-only update.' \
  >> "$PRODUCT_REPLAY/factory/tickets/T-002.md"
commit_all "$PRODUCT_REPLAY" "update ticket control only"
push_main "$PRODUCT_REPLAY"
CERTIFICATION_TRACE="$TMP/replay-certification.trace"
export FACTORY_KIT_TEST_CERTIFICATION_TRACE="$CERTIFICATION_TRACE"
: > "$CERTIFICATION_TRACE"
expect_success "ticket-control descendant reuses measured product certification" \
  certify --project replay --product "$PRODUCT_REPLAY" --sha "$SHA_A"
REPLAY_RECEIPT="$(printf '%s\n' "$LAST_OUTPUT" | awk '/^\// {value=$0} END {print value}')"
if [[ "$(json_value "$REPLAY_RECEIPT" product_certification_evidence.mode)" == "ticket-control-replay" &&
      "$(json_value "$REPLAY_RECEIPT" checks.product_certification)" == "reused" ]]; then
  pass "ticket-control replay skips the full product suite"
else
  fail "ticket-control replay skips the full product suite" \
    "mode=$(json_value "$REPLAY_RECEIPT" product_certification_evidence.mode), check=$(json_value "$REPLAY_RECEIPT" checks.product_certification), trace=$(tr '\n' ',' < "$CERTIFICATION_TRACE")"
fi
expect_success "ticket-control replay receipt revalidates for activation" \
  pause --project replay --product "$PRODUCT_REPLAY"
expect_success "ticket-control replay activates" \
  activate --project replay --product "$PRODUCT_REPLAY" --sha "$SHA_A" \
  --receipt "$REPLAY_RECEIPT"

printf '%s\n' '# executable change must recertify' \
  >> "$PRODUCT_REPLAY/scripts/secret-scan"
commit_all "$PRODUCT_REPLAY" "change executable product input"
push_main "$PRODUCT_REPLAY"
: > "$CERTIFICATION_TRACE"
expect_success "executable descendant falls back to full product certification" \
  certify --project replay --product "$PRODUCT_REPLAY" --sha "$SHA_A"
REPLAY_FALLBACK_RECEIPT="$(printf '%s\n' "$LAST_OUTPUT" | awk '/^\// {value=$0} END {print value}')"
if grep -qx 'product-certification' "$CERTIFICATION_TRACE" &&
   ! grep -q 'ticket-control-replay' "$CERTIFICATION_TRACE" &&
   [[ "$(json_value "$REPLAY_FALLBACK_RECEIPT" product_certification_evidence.mode)" == "measured" ]]; then
  pass "non-ticket changes retain full product certification"
else
  fail "non-ticket changes retain full product certification"
fi
unset FACTORY_KIT_TEST_CERTIFICATION_TRACE

PRODUCT_BOOTSTRAP_UNSAFE="$(make_product product-bootstrap-unsafe)"
set_pin "$PRODUCT_BOOTSTRAP_UNSAFE" "$SHA_A"
set_ticket_lease "$PRODUCT_BOOTSTRAP_UNSAFE" "$SHA_A"
commit_all "$PRODUCT_BOOTSTRAP_UNSAFE" "prepare unsafe bootstrap product"
push_main "$PRODUCT_BOOTSTRAP_UNSAFE"
mkdir "$PRODUCT_BOOTSTRAP_UNSAFE/factory/runs"
chmod 777 "$PRODUCT_BOOTSTRAP_UNSAFE/factory/runs"
expect_failure "bootstrap rejects a broadly writable empty-run authority" \
  bootstrap --project bootstrap-unsafe --product "$PRODUCT_BOOTSTRAP_UNSAFE" \
  --sha "$SHA_A" --repo "$KIT_REPO"
[[ "$LAST_OUTPUT" == *"product runtime directory is unsafe"* ]] ||
  fail "bootstrap reports the unsafe empty-run authority" "$LAST_OUTPUT"

PRODUCT_BOOTSTRAP="$(make_product product-bootstrap)"
set_pin "$PRODUCT_BOOTSTRAP" "$SHA_A"
set_ticket_lease "$PRODUCT_BOOTSTRAP" "$SHA_A"
commit_all "$PRODUCT_BOOTSTRAP" "lease bootstrap product to release a"
push_main "$PRODUCT_BOOTSTRAP"
export FACTORY_KIT_TEST_FAIL_BOOTSTRAP_AFTER_PHASE=install
expect_failure "bootstrap interruption preserves installed release progress" \
  bootstrap --project bootstrap --product "$PRODUCT_BOOTSTRAP" \
  --sha "$SHA_A" --repo "$KIT_REPO"
unset FACTORY_KIT_TEST_FAIL_BOOTSTRAP_AFTER_PHASE
BOOTSTRAP_JOURNAL="$STATE/projects/bootstrap/release-journal/$SHA_A.json"
if python3 - "$PRODUCT_BOOTSTRAP" <<'PY'
import os, pathlib, stat, sys
root = pathlib.Path(sys.argv[1])
for relative in ("factory/runs", "factory/.active-runs"):
    path = root / relative
    info = path.lstat()
    assert not path.is_symlink() and stat.S_ISDIR(info.st_mode)
    assert info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o700
    assert not any(path.iterdir())
PY
then
  pass "bootstrap provisions secure empty-run authority"
else
  fail "bootstrap provisions secure empty-run authority"
fi
if [[ "$(json_value "$BOOTSTRAP_JOURNAL" phases.install.status)" == "pass" &&
      "$(project_receipt_count bootstrap "$SHA_A")" == "0" ]]; then
  pass "install checkpoint resumes before certification"
else
  fail "install checkpoint resumes before certification"
fi
export FACTORY_KIT_TEST_FAIL_BOOTSTRAP_AFTER_PHASE=certify
expect_failure "bootstrap interruption preserves certified release progress" \
  bootstrap --project bootstrap --product "$PRODUCT_BOOTSTRAP" \
  --sha "$SHA_A" --repo "$KIT_REPO"
unset FACTORY_KIT_TEST_FAIL_BOOTSTRAP_AFTER_PHASE
if python3 - "$BOOTSTRAP_JOURNAL" <<'PY'
import hashlib, json, pathlib, sys
value = json.loads(pathlib.Path(sys.argv[1]).read_text())
body = {key: item for key, item in value.items() if key != "record_sha256"}
canonical = (json.dumps(body, sort_keys=True, separators=(",", ":")) + "\n").encode()
assert value["record_sha256"] == hashlib.sha256(canonical).hexdigest()
assert value["phases"]["install"]["status"] == "pass"
assert value["phases"]["certify"]["status"] == "pass"
assert "pause" not in value["phases"]
PY
then
  pass "bootstrap journal binds recoverable phase progress"
else
  fail "bootstrap journal binds recoverable phase progress"
fi
[[ "$(project_receipt_count bootstrap "$SHA_A")" == "1" ]] &&
  pass "interrupted bootstrap creates one certification receipt" ||
  fail "interrupted bootstrap creates one certification receipt"
export FACTORY_KIT_TEST_FAIL_BOOTSTRAP_AFTER_PHASE=pause
expect_failure "bootstrap interruption preserves drained release progress" \
  bootstrap --project bootstrap --product "$PRODUCT_BOOTSTRAP" \
  --sha "$SHA_A" --repo "$KIT_REPO"
unset FACTORY_KIT_TEST_FAIL_BOOTSTRAP_AFTER_PHASE
if [[ "$(json_value "$BOOTSTRAP_JOURNAL" phases.pause.status)" == "pass" &&
      ! -e "$STATE/projects/bootstrap/active.json" ]]; then
  pass "pause checkpoint resumes before activation"
else
  fail "pause checkpoint resumes before activation"
fi
expect_success "bootstrap resumes without repeating certification" \
  bootstrap --project bootstrap --product "$PRODUCT_BOOTSTRAP" \
  --sha "$SHA_A" --repo "$KIT_REPO"
BOOTSTRAP_ACTIVE="$STATE/projects/bootstrap/active.json"
if python3 - "$BOOTSTRAP_JOURNAL" "$BOOTSTRAP_ACTIVE" "$SHA_A" <<'PY'
import json, pathlib, sys
journal = json.loads(pathlib.Path(sys.argv[1]).read_text())
active = json.loads(pathlib.Path(sys.argv[2]).read_text())
factory_sha = sys.argv[3]
assert journal["phases"]["bootstrap"]["status"] == "pass"
assert all(journal["phases"][phase]["status"] == "pass" for phase in (
    "install", "certify", "pause", "activate",
))
epochs = [event["observed_epoch_ms"] for event in journal["events"]]
assert epochs == sorted(epochs)
assert active["kit_sha"] == factory_sha and active["generation"] == 1
activation = next(pathlib.Path(sys.argv[2]).parent.joinpath("activation-journal").glob("*.json"))
activation_value = json.loads(activation.read_text())
assert activation_value["phase"] == "committed"
assert [event["phase"] for event in activation_value["phase_events"]] == [
    "prepared", "receipt_claimed", "maintenance_published", "launch_drained",
    "services_stopped", "activation_record_switched", "integration_bundle_switched",
    "services_started", "healthy", "committed",
]
assert [event["observed_epoch_ms"] for event in activation_value["phase_events"]] == sorted(
    event["observed_epoch_ms"] for event in activation_value["phase_events"]
)
PY
then
  pass "bootstrap trace and activation trace are complete and ordered"
else
  fail "bootstrap trace and activation trace are complete and ordered"
fi
BOOTSTRAP_STATE_BEFORE="$(state_snapshot)"
expect_success "completed bootstrap is idempotent" \
  bootstrap --project bootstrap --product "$PRODUCT_BOOTSTRAP" \
  --sha "$SHA_A" --repo "$KIT_REPO"
BOOTSTRAP_STATE_AFTER="$(state_snapshot)"
[[ "$(project_receipt_count bootstrap "$SHA_A")" == "1" &&
   "$(json_value "$BOOTSTRAP_ACTIVE" generation)" == "1" &&
   "$BOOTSTRAP_STATE_BEFORE" == "$BOOTSTRAP_STATE_AFTER" ]] &&
  pass "bootstrap replay creates no receipt or activation generation" ||
  fail "bootstrap replay creates no receipt or activation generation"
expect_success "bootstrap status returns the signed release trace" \
  bootstrap-status --project bootstrap --sha "$SHA_A" --json
if python3 - "$LAST_OUTPUT" "$SHA_A" <<'PY'
import json, sys
value = json.loads(sys.argv[1])
assert value["identity"]["factory_sha"] == sys.argv[2]
assert value["phases"]["bootstrap"]["status"] == "pass"
PY
then
  pass "bootstrap JSON status identifies the completed candidate"
else
  fail "bootstrap JSON status identifies the completed candidate" "$LAST_OUTPUT"
fi
PRODUCT_BOOTSTRAP_LEGACY="$(make_product product-bootstrap-legacy)"
git -C "$PRODUCT_BOOTSTRAP_LEGACY" rm -q factory/certification-plan.json
cat > "$PRODUCT_BOOTSTRAP_LEGACY/factory/certify.sh" <<'EOF'
#!/usr/bin/env bash
set -eu
[[ "$HOME" == *factory-kit-certification* ]]
[[ "$FACTORY_KIT_RELEASE" == *factory-kit-certification*/release ]]
EOF
set_pin "$PRODUCT_BOOTSTRAP_LEGACY" "$SHA_A"
set_ticket_lease "$PRODUCT_BOOTSTRAP_LEGACY" "$SHA_A"
commit_all "$PRODUCT_BOOTSTRAP_LEGACY" "prepare legacy bootstrap product"
push_main "$PRODUCT_BOOTSTRAP_LEGACY"
expect_success "bootstrap accepts product without an optional runtime tuple" \
  bootstrap --project bootstrap-legacy --product "$PRODUCT_BOOTSTRAP_LEGACY" \
  --sha "$SHA_A" --repo "$KIT_REPO"
LEGACY_ACTIVE="$STATE/projects/bootstrap-legacy/active.json"
LEGACY_RECEIPT="$STATE/receipts/consumed/$(json_value "$LEGACY_ACTIVE" receipt_id).json"
if python3 - "$LEGACY_ACTIVE" "$LEGACY_RECEIPT" <<'PY'
import json, pathlib, sys
active = json.loads(pathlib.Path(sys.argv[1]).read_text())
receipt = json.loads(pathlib.Path(sys.argv[2]).read_text())
assert "runtime_tuple" not in active
assert "runtime_tuple" not in receipt
PY
then
  pass "legacy bootstrap preserves absent optional runtime tuple"
else
  fail "legacy bootstrap preserves absent optional runtime tuple"
fi
cp "$BOOTSTRAP_JOURNAL" "$TMP/bootstrap-journal.saved"
chmod u+w "$BOOTSTRAP_JOURNAL"
python3 - "$BOOTSTRAP_JOURNAL" <<'PY'
import json, pathlib, sys
path = pathlib.Path(sys.argv[1])
value = json.loads(path.read_text())
value["phases"]["bootstrap"]["status"] = "failed"
path.write_text(json.dumps(value) + "\n")
PY
chmod 600 "$BOOTSTRAP_JOURNAL"
expect_failure "bootstrap status rejects a tampered release trace" \
  bootstrap-status --project bootstrap --sha "$SHA_A" --json
cp "$TMP/bootstrap-journal.saved" "$BOOTSTRAP_JOURNAL"
chmod 600 "$BOOTSTRAP_JOURNAL"
printf '%s\n' "$SHA_B" > "$PRODUCT_BOOTSTRAP/factory/KIT_PIN"
set_ticket_lease "$PRODUCT_BOOTSTRAP" "$SHA_B"
commit_all "$PRODUCT_BOOTSTRAP" "prepare a different bootstrap candidate"
push_main "$PRODUCT_BOOTSTRAP"
expect_failure "bootstrap refuses to replace an active release" \
  bootstrap --project bootstrap --product "$PRODUCT_BOOTSTRAP" \
  --sha "$SHA_B" --repo "$KIT_REPO"
[[ "$LAST_OUTPUT" == *"use the upgrade runbook"* ]] ||
  fail "bootstrap reports its initial-release boundary" "$LAST_OUTPUT"

expect_failure "invalid slug cannot traverse project state" status --project "../alpha"
expect_failure "automatic prune remains unavailable" prune

if [[ "$FAILURES" -gt 0 ]]; then
  printf 'FAIL: %s factory-kit test(s) failed\n' "$FAILURES" >&2
  exit 1
fi
printf 'PASS: all factory-kit tests\n'
