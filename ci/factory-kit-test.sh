#!/usr/bin/env bash
# Adversarial, self-contained tests for scripts/factory-kit.sh.
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
KIT="$ROOT/scripts/factory-kit.sh"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/factory-kit-test.XXXXXX")"
TMP="$(cd "$TMP" && pwd -P)"
TEST_TMP="$TMP/tmp"
STATE="$TMP/kits"
CANONICAL="$TMP/canonical.git"
KIT_REPO="$TMP/kit-source"
STUB_BIN="$TMP/bin"
PINNED_SCANNER_STUB="$STUB_BIN/gitleaks"
GH_TRACE="$TMP/gh.trace"
FAILURES=0
LAST_OUTPUT=""
FIRST_PID=""
REAL_HOME_SANDBOX_SECRET=""

mkdir -p "$TEST_TMP" "$STUB_BIN"
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
  FACTORY_KIT_TEST_REMOTE_FULL_CI="${FACTORY_KIT_TEST_REMOTE_FULL_CI:-1}" \
  FACTORY_KIT_TEST_PINNED_SCANNER="$PINNED_SCANNER_STUB" \
  FACTORY_KIT_CANONICAL_ORIGIN="$CANONICAL" \
  FACTORY_KIT_GH_TRACE="$GH_TRACE" \
  FACTORY_KIT_LOCK_ATTEMPTS="${FACTORY_KIT_LOCK_ATTEMPTS:-20}" \
    bash "$KIT" "$@"
}

run_kit_with_state() {
  local state="$1"
  shift
  PATH="$STUB_BIN:$PATH" \
  TMPDIR="$TEST_TMP" \
  FACTORY_KITS_ROOT="$state" \
  FACTORY_KIT_TEST_MODE=1 \
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
assert migrated["revisions"][-1]["body"]["prior_resolution"] == manager.active_resolution(value)
assert manager.active_resolution(migrated) == migrated["revisions"][-1]["body"]["new_resolution"]
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
  local name="$1" path bare
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
python3 - "$FACTORY_CERTIFICATION_EVIDENCE" <<'PY'
import json, os, pathlib, subprocess, sys
path = pathlib.Path(sys.argv[1])
value = {
    "ended_at": "2026-07-29T00:00:01Z",
    "factory_sha": os.environ["FACTORY_KIT_SHA"],
    "max_workers": 2,
    "network_reviewed": os.environ.get("FACTORY_CERTIFICATION_NETWORK_REVIEWED") == "1",
    "phases": [{
        "artifact_sha256": "a" * 64,
        "cache_hit": True,
        "cache_record_sha256": "e" * 64,
        "command": ["fixture"],
        "ended_at": "2026-07-29T00:00:01Z",
        "exit_status": 0,
        "input_sha256": "b" * 64,
        "name": "fixture",
        "network_declared": "denied",
        "network_granted": False,
        "output_sha256": "d" * 64,
        "peak_memory_kb": 1,
        "started_at": "2026-07-29T00:00:00Z",
        "system_cpu_seconds": 0,
        "user_cpu_seconds": 0,
        "wall_seconds": 1,
    }],
    "plan_sha256": "c" * 64,
    "product_tree": os.environ["FACTORY_PRODUCT_TREE"],
    "runtime": {
        "node": subprocess.check_output(["node", "--version"], text=True).strip(),
        "npm": subprocess.check_output(["npm", "--version"], text=True).strip(),
    },
    "schema": "nysa.software-factory.certification-result/v1",
    "started_at": "2026-07-29T00:00:00Z",
    "status": "pass",
    "wall_seconds": 1,
}
path.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")
os.chmod(path, 0o600)
PY
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
mkdir -p "$KIT_REPO/ci" "$KIT_REPO/scripts" \
  "$KIT_REPO/integrations/hermes/bin"
mkdir -p "$KIT_REPO/scripts/model-routing"
cp "$ROOT/scripts/model-manager.py" "$ROOT/scripts/model-router.py" \
  "$KIT_REPO/scripts/"
cp "$ROOT/integrations/hermes/bin/factory-launch" \
  "$KIT_REPO/integrations/hermes/bin/factory-launch"
chmod +x "$KIT_REPO/integrations/hermes/bin/factory-launch"
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

# Every managed root/component rejects symlink traversal before state reads.
SYMLINK_TARGET="$TMP/symlink-target"
mkdir "$SYMLINK_TARGET"
RAW_LINK="$TMP/raw-kits-link"
ln -s "$SYMLINK_TARGET" "$RAW_LINK"
if run_kit_with_state "$RAW_LINK" status --project alpha >/dev/null 2>&1; then
  fail "raw state root symlink is rejected"
else
  pass "raw state root symlink is rejected"
fi
for component in releases manifests receipts projects; do
  SYM_STATE="$TMP/sym-$component"
  mkdir -p "$SYM_STATE"
  ln -s "$SYMLINK_TARGET" "$SYM_STATE/$component"
  if run_kit_with_state "$SYM_STATE" status --project alpha >/dev/null 2>&1; then
    fail "$component managed symlink is rejected"
  else
    pass "$component managed symlink is rejected"
  fi
done
SYM_PROJECT_STATE="$TMP/sym-project-dir"
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
cp "$2" "${FACTORY_KIT_SANDBOX_CAPTURE:?}"
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

PRODUCT_ONE="$(make_product product-one)"
set_pin "$PRODUCT_ONE" "$SHA_A"
MISMATCHED_LAUNCHER="$TMP/mismatched-factory-launch"
printf '%s\n' '#!/usr/bin/env bash' 'exit 1' > "$MISMATCHED_LAUNCHER"
chmod +x "$MISMATCHED_LAUNCHER"
export FACTORY_KIT_TEST_INSTALLED_LAUNCHER="$MISMATCHED_LAUNCHER"
expect_failure "certification rejects an installed launcher from another release" \
  certify --project alpha --product "$PRODUCT_ONE" --sha "$SHA_A"
unset FACTORY_KIT_TEST_INSTALLED_LAUNCHER
if [[ "$LAST_OUTPUT" == *"installed factory-launch does not match the sealed candidate"* ]]; then
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
CERTIFICATION_FAILURE="$(find "$STATE/receipts/failures" -type f -name '*.json' \
  -print -quit)"
CERTIFICATION_FAILURE_SAFE=0
if [[ -f "$CERTIFICATION_FAILURE" ]] &&
   [[ "$(json_value "$CERTIFICATION_FAILURE" status)" == "fail" ]] &&
   [[ "$(json_value "$CERTIFICATION_FAILURE" factory_sha)" == "$SHA_A" ]] &&
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
   grep -q 'allow file-write.*factory-kit-certification' "$CERT_SANDBOX_CAPTURE"; then
  pass "certification sandbox is filesystem and network default-deny"
else
  fail "certification sandbox is filesystem and network default-deny"
fi
RECEIPT_STALE="$(printf '%s\n' "$LAST_OUTPUT" | awk '/^\// {value=$0} END {print value}')"
RECEIPT_STALE_ID="$(json_value "$RECEIPT_STALE" receipt_id)"
if [[ "$(basename "$RECEIPT_STALE")" == "$RECEIPT_STALE_ID.json" &&
      "$(json_value "$RECEIPT_STALE" certification_tool_version)" == "5" &&
      "$(json_value "$RECEIPT_STALE" provider_concurrency_evidence.status)" == "not-required" &&
      "$(json_value "$RECEIPT_STALE" provider_concurrency_evidence.factory_sha)" == "$SHA_A" &&
      "$(json_value "$RECEIPT_STALE" provider_concurrency_evidence.factory_tree)" == "$(git -C "$KIT_REPO" rev-parse "$SHA_A^{tree}")" &&
      "$(json_value "$RECEIPT_STALE" checks.provider_concurrency)" == "pass" &&
      "$(json_value "$RECEIPT_STALE" product_certification_evidence.mode)" == "measured" &&
      -z "$(json_value "$RECEIPT_STALE" expected_previous_generation)" &&
      ! -e "$PRODUCT_ONE/factory/product-certification-marker" &&
      ! -e "$STATE/releases/$SHA_A/release-certification-marker" &&
      ! -e "$HOME/.factory-kit-certification-marker" ]]; then
  pass "receipt identity and isolated certification bindings are exact"
else
  fail "receipt identity and isolated certification bindings are exact"
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
/bin/sh -c '
  lock=$1
  mkdir "$lock"
  start=$(ps -o lstart= -p $$ |
    python3 -c "import sys; print(\" \".join(sys.stdin.read().split()))")
  {
    printf "pid=%s\n" "$$"
    printf "process_start=%s\n" "$start"
    printf "nonce=11111111111111111111111111111111\n"
    printf "created_epoch=1\n"
  } > "$lock/owner"
  sleep 2
  mv "$lock" "$lock.released"
  rm -rf "$lock.released"
' queued-launch "$PRODUCT_ONE/factory/.launch.lock" &
LAUNCH_HOLDER_PID=$!
for _i in $(seq 1 100); do
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
expect_success "operator recovers stale lease only under maintenance" \
  recover-lease --project alpha --product "$PRODUCT_ONE" --ticket T-004
[[ ! -e "$PRODUCT_ONE/factory/.dispatch-leases/T-004.json" ]] &&
  pass "stale lease recovery removes only the named ticket" ||
  fail "stale lease recovery removes only the named ticket"
rm -rf "$PRODUCT_ONE/factory/.dispatch-leases"

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
[[ "$(json_value "$RECEIPT_B" expected_previous_generation)" == "1" ]] &&
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
restore_product_tuple "$PRODUCT_ONE" "$SHA_A"
printf '%s\n\n' "$SHA_A" > "$PRODUCT_ONE/factory/KIT_PIN"
expect_failure "rollback rejects KIT_PIN blank-line extras" \
  rollback --project alpha --product "$PRODUCT_ONE"
printf '%s\n' "$SHA_A" > "$PRODUCT_ONE/factory/KIT_PIN"
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

expect_failure "invalid slug cannot traverse project state" status --project "../alpha"
expect_failure "automatic prune remains unavailable" prune

if [[ "$FAILURES" -gt 0 ]]; then
  printf 'FAIL: %s factory-kit test(s) failed\n' "$FAILURES" >&2
  exit 1
fi
printf 'PASS: all factory-kit tests\n'
