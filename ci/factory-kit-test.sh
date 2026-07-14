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
GH_TRACE="$TMP/gh.trace"
FAILURES=0
LAST_OUTPUT=""
FIRST_PID=""
REAL_HOME_SANDBOX_SECRET=""

mkdir -p "$TEST_TMP" "$STUB_BIN"

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
  file="$product/factory/tickets/T-PLANNING.md"
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
  mkdir -p "$path/factory/tickets"
  cat > "$path/factory/PROJECT.env" <<'EOF'
PROJECT_NAME=test-product
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
factory/.active-runs/
factory/runs/
EOF
  cat > "$path/factory/tickets/T-READY.md" <<'EOF'
State: Ready
EOF
  cat > "$path/factory/tickets/T-BACKLOG.md" <<'EOF'
State: Backlog
EOF
  cat > "$path/factory/tickets/T-DONE.md" <<'EOF'
State: Done
Kit-SHA: 0000000000000000000000000000000000000000
EOF
  cat > "$path/factory/tickets/T-PLANNING.md" <<'EOF'
State: Planning
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
mkdir -p "$KIT_REPO/ci" "$KIT_REPO/scripts"
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
printf 'fixture suite passed\n'
EOF
cat > "$KIT_REPO/scripts/repo-check" <<'EOF'
#!/usr/bin/env bash
set -eu
[[ "${1:-}" == "--root" && -d "${2:-}" ]]
EOF
cat > "$KIT_REPO/scripts/secret-scan" <<'EOF'
#!/usr/bin/env bash
set -eu
exit 0
EOF
chmod +x "$KIT_REPO/ci/test-all.sh" "$KIT_REPO/scripts/repo-check" \
  "$KIT_REPO/scripts/secret-scan"
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

if [[ "$(uname -s)" == "Darwin" && -x /usr/bin/sandbox-exec ]]; then
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
if [[ -f "$SANDBOX_CAPTURE" ]] &&
   grep -q '^(deny default)' "$SANDBOX_CAPTURE" &&
   ! grep -qx '(allow file-read\*)' "$SANDBOX_CAPTURE" &&
   ! grep -q '^(allow network' "$SANDBOX_CAPTURE" &&
   ! grep -qx '(allow process\*)' "$SANDBOX_CAPTURE" &&
   grep -q '^(allow process-fork)' "$SANDBOX_CAPTURE" &&
   grep -q 'allow file-read.*"/System"' "$SANDBOX_CAPTURE" &&
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
if [[ -f "$MANIFEST_A" && ! -L "$MANIFEST_A" ]] &&
   [[ "$(json_value "$MANIFEST_A" kit_sha)" == "$SHA_A" ]] &&
   [[ "$(json_value "$MANIFEST_A" git_tree)" == "$(git -C "$KIT_REPO" rev-parse "$SHA_A^{tree}")" ]] &&
   [[ "$(json_value "$MANIFEST_A" sealed_release_path)" == "$STATE/releases/$SHA_A" ]]; then
  pass "trusted external install manifest binds release"
else
  fail "trusted external install manifest binds release"
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
expect_success "second exact release publishes portably" \
  install --repo "$KIT_REPO" --sha "$SHA_B"
unset FACTORY_KIT_TEST_PUBLISH_TRACE
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
      "$LAST_OUTPUT" == *"[REDACTED]"* ]]; then
  pass "structured certification output never exposes secrets"
else
  fail "structured certification output never exposes secrets" "$LAST_OUTPUT"
fi
rm "$PRODUCT_ONE/factory/FAIL_CERTIFY"
commit_all "$PRODUCT_ONE" "restore certification"
push_main "$PRODUCT_ONE"

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
   ! grep -q '^(allow network' "$CERT_SANDBOX_CAPTURE" &&
   ! grep -qx '(allow process\*)' "$CERT_SANDBOX_CAPTURE" &&
   grep -q '^(allow process-fork)' "$CERT_SANDBOX_CAPTURE" &&
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
      "$(json_value "$RECEIPT_STALE" certification_tool_version)" == "1" &&
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

printf '%s\n' 'State: Ready' >> "$PRODUCT_ONE/factory/tickets/T-READY.md"
commit_all "$PRODUCT_ONE" "add duplicate ticket state fixture"
push_main "$PRODUCT_ONE"
expect_success "duplicate-state product tuple can certify" \
  certify --project alpha --product "$PRODUCT_ONE" --sha "$SHA_A"
RECEIPT_DUPLICATE_STATE="$(printf '%s\n' "$LAST_OUTPUT" | awk '/^\// {value=$0} END {print value}')"
expect_failure "duplicate ticket State fields block activation" \
  activate --project alpha --product "$PRODUCT_ONE" --sha "$SHA_A" \
  --receipt "$RECEIPT_DUPLICATE_STATE"
python3 - "$PRODUCT_ONE/factory/tickets/T-READY.md" <<'PY'
import pathlib, sys
path = pathlib.Path(sys.argv[1])
lines = path.read_text().splitlines()
lines.pop(max(i for i, line in enumerate(lines) if line == "State: Ready"))
path.write_text("\n".join(lines) + "\n")
PY
commit_all "$PRODUCT_ONE" "remove duplicate ticket state fixture"
push_main "$PRODUCT_ONE"

printf 'Kit-SHA: %s\n' "$SHA_A" >> "$PRODUCT_ONE/factory/tickets/T-PLANNING.md"
commit_all "$PRODUCT_ONE" "add duplicate ticket lease fixture"
push_main "$PRODUCT_ONE"
expect_success "duplicate-lease product tuple can certify" \
  certify --project alpha --product "$PRODUCT_ONE" --sha "$SHA_A"
RECEIPT_DUPLICATE_LEASE="$(printf '%s\n' "$LAST_OUTPUT" | awk '/^\// {value=$0} END {print value}')"
expect_failure "duplicate ticket Kit-SHA fields block activation" \
  activate --project alpha --product "$PRODUCT_ONE" --sha "$SHA_A" \
  --receipt "$RECEIPT_DUPLICATE_LEASE"
python3 - "$PRODUCT_ONE/factory/tickets/T-PLANNING.md" <<'PY'
import pathlib, sys
path = pathlib.Path(sys.argv[1])
lines = path.read_text().splitlines()
indices = [i for i, line in enumerate(lines) if line.startswith("Kit-SHA:")]
lines.pop(indices[-1])
path.write_text("\n".join(lines) + "\n")
PY
commit_all "$PRODUCT_ONE" "remove duplicate ticket lease fixture"
push_main "$PRODUCT_ONE"

printf '%s\n' 'State: Done' 'Kit-SHA: not-a-canonical-sha' \
  > "$PRODUCT_ONE/factory/tickets/T-DONE.md"
commit_all "$PRODUCT_ONE" "add invalid terminal lease fixture"
push_main "$PRODUCT_ONE"
expect_success "invalid-terminal-lease tuple can certify" \
  certify --project alpha --product "$PRODUCT_ONE" --sha "$SHA_A"
RECEIPT_INVALID_DONE="$(printf '%s\n' "$LAST_OUTPUT" | awk '/^\// {value=$0} END {print value}')"
expect_failure "terminal ticket lease is validated before state decision" \
  activate --project alpha --product "$PRODUCT_ONE" --sha "$SHA_A" \
  --receipt "$RECEIPT_INVALID_DONE"
printf '%s\n' 'State: Done' \
  'Kit-SHA: 0000000000000000000000000000000000000000' \
  > "$PRODUCT_ONE/factory/tickets/T-DONE.md"
commit_all "$PRODUCT_ONE" "restore canonical terminal lease"
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

set_pin "$PRODUCT_ONE" "$SHA_B"
expect_success "candidate with old ticket lease can certify" \
  certify --project alpha --product "$PRODUCT_ONE" --sha "$SHA_B"
RECEIPT_WRONG_LEASE="$(printf '%s\n' "$LAST_OUTPUT" | awk '/^\// {value=$0} END {print value}')"
expect_failure "different nonterminal ticket lease blocks activation" \
  activate --project alpha --product "$PRODUCT_ONE" --sha "$SHA_B" \
  --receipt "$RECEIPT_WRONG_LEASE"

set_ticket_lease "$PRODUCT_ONE" "$SHA_B"
commit_all "$PRODUCT_ONE" "lease planning ticket to release b"
push_main "$PRODUCT_ONE"
expect_success "upgraded product tuple certifies" \
  certify --project alpha --product "$PRODUCT_ONE" --sha "$SHA_B"
RECEIPT_B="$(printf '%s\n' "$LAST_OUTPUT" | awk '/^\// {value=$0} END {print value}')"
[[ "$(json_value "$RECEIPT_B" expected_previous_generation)" == "1" ]] &&
  pass "receipt binds expected previous generation" ||
  fail "receipt binds expected previous generation"

export FACTORY_KIT_FAIL_AFTER_PHASE=receipt_claimed
expect_failure "fault injection interrupts after receipt claim" \
  activate --project alpha --product "$PRODUCT_ONE" --sha "$SHA_B" \
  --receipt "$RECEIPT_B"
unset FACTORY_KIT_FAIL_AFTER_PHASE
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

expect_failure "rollback refuses unreverted product tuple" \
  rollback --project alpha --product "$PRODUCT_ONE"
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
