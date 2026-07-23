#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LANE="${FACTORY_DEV_LANE_UNDER_TEST:-$ROOT/scripts/factory-dev-lane.sh}"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/factory-dev-lane-test.XXXXXX")"
TMP="$(cd "$TMP" && pwd -P)"
FAKE_SANDBOX="$TMP/sandbox-exec"
FAKE_CURSOR="$TMP/cursor-agent"
OUT="$TMP/out"
CALLER_HOME="$TMP/caller-home"

cleanup() {
  chmod -R u+w "$TMP" 2>/dev/null || true
  rm -rf "$TMP"
}
trap cleanup EXIT
trap 'status=$?; printf "FAIL: unexpected command at line %s (exit %s)\n" "${BASH_LINENO[0]:-$LINENO}" "$status" >&2; [[ ! -s "$OUT" ]] || sed -n "1,120p" "$OUT" >&2; exit "$status"' ERR

fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }

expect_failure() {
  local label="$1"
  shift
  if "$@" >"$OUT" 2>&1; then
    fail "$label unexpectedly succeeded"
  fi
}

test_env() {
  FACTORY_DEV_LANE_TEST_MODE=1 \
  FACTORY_TRUSTED_TEST_HARNESS=1 \
  FACTORY_DEV_LANE_UNAME=Darwin \
  FACTORY_DEV_LANE_SANDBOX_EXEC="$FAKE_SANDBOX" \
  FACTORY_DEV_LANE_ACCOUNT_HOME="$CALLER_HOME" \
  HOME="$CALLER_HOME" \
  TMPDIR="$TMP/lanes" \
  "$@"
}

cursor_env() {
  FACTORY_DEV_LANE_TEST_MODE=1 \
  FACTORY_TRUSTED_TEST_HARNESS=1 \
  FACTORY_DEV_LANE_UNAME=Darwin \
  FACTORY_DEV_LANE_SANDBOX_EXEC="$FAKE_SANDBOX" \
  FACTORY_DEV_LANE_CURSOR_BIN="$FAKE_CURSOR" \
  FACTORY_DEV_LANE_CURSOR_TMP_BRIDGE="$TMP/cursor-tmp-bridge" \
  FACTORY_DEV_LANE_CURSOR_SESSION_HOME="$CALLER_HOME" \
  HOME="$CALLER_HOME" \
  TMPDIR="$TMP/lanes" \
  "$@"
}

clean_cmd() { TMPDIR="$TMP/lanes" bash "$LANE" clean --root "$1"; }

mkdir -p "$TMP/lanes" "$CALLER_HOME/.factory" "$CALLER_HOME/.cursor" \
  "$CALLER_HOME/.hermes/profiles/factory" "$CALLER_HOME/Library/LaunchAgents" \
  "$CALLER_HOME/Projects/nysa-company/nysa-app"
printf 'factory production sentinel\n' >"$CALLER_HOME/.factory/sentinel"
printf 'profile production sentinel\n' >"$CALLER_HOME/.hermes/profiles/factory/sentinel"
printf 'service production sentinel\n' >"$CALLER_HOME/Library/LaunchAgents/sentinel"
printf 'product production sentinel\n' >"$CALLER_HOME/Projects/nysa-company/nysa-app/sentinel"
printf '{"accessToken":"test","refreshToken":"test"}\n' >"$CALLER_HOME/.cursor/auth.json"
printf '{}\n' >"$CALLER_HOME/.cursor/cli-config.json"
chmod 600 "$CALLER_HOME/.cursor/"*.json
sentinels_before="$(cksum "$CALLER_HOME/.factory/sentinel" \
  "$CALLER_HOME/.hermes/profiles/factory/sentinel" \
  "$CALLER_HOME/Library/LaunchAgents/sentinel" \
  "$CALLER_HOME/Projects/nysa-company/nysa-app/sentinel")"
cursor_session_before="$(cksum "$CALLER_HOME/.cursor/auth.json" \
  "$CALLER_HOME/.cursor/cli-config.json")"
cat >"$FAKE_SANDBOX" <<'EOF'
#!/usr/bin/env bash
set -eu
[[ "$1" == "-f" && -f "$2" ]] || exit 97
shift 2
exec "$@"
EOF
chmod +x "$FAKE_SANDBOX"
cat >"$FAKE_CURSOR" <<'EOF'
#!/usr/bin/env bash
case "${1:-}" in
  --version) printf '2026.07.17-test\n' ;;
  --help) printf '%s\n' --print --output-format --workspace --model --force --trust --sandbox ;;
  status) printf '{"authenticated":true}\n' ;;
  models) printf '%s\n' \
    gpt-5.6-sol-high claude-fable-5-thinking-medium claude-sonnet-5-thinking-high ;;
  *) printf '%s\n' "$@" >"$(dirname "$0")/cursor-args"; exit 42 ;;
esac
EOF
chmod +x "$FAKE_CURSOR"

[[ -x "$LANE" ]] || fail "development lane wrapper is not executable"

review_pattern='^[[:space:]#*]*(((Review[[:space:]]+)?Verdict:[[:space:]*]*)?APPROVE|Review[[:space:]]+verdict:[[:space:]]+T-[0-9]+[[:space:]]+—[[:space:]]+APPROVE)[*[:space:]]*$'
printf '%s\n' '## Verdict: Approve' | grep -Eiq "$review_pattern" ||
  fail "review verdict parser rejected a canonical approval"
printf '%s\n' '**Verdict: Approve**' | grep -Eiq "$review_pattern" ||
  fail "review verdict parser rejected a bold canonical approval"
printf '%s\n' '## Review verdict: **Approve**' | grep -Eiq "$review_pattern" ||
  fail "review verdict parser rejected a headed bold approval"
printf '%s\n' '## Review verdict: T-900001 — Approve' | grep -Eiq "$review_pattern" ||
  fail "review verdict parser rejected a ticket-qualified approval"
if printf '%s\n' 'Do not approve' | grep -Eiq "$review_pattern"; then
  fail "review verdict parser accepted approval prose"
fi
if printf '%s\n' 'I approve' | grep -Eiq "$review_pattern"; then
  fail "review verdict parser accepted first-person approval prose"
fi
if printf '%s\n' 'Review verdict: I cannot approve' | grep -Eiq "$review_pattern"; then
  fail "review verdict parser accepted a negative verdict"
fi
printf '%s\n' \
  'warning outside stream' \
  '{"type":"result","subtype":"success","result":"Reviewed safely.\n\nAPPROVE"}' |
  python3 "$ROOT/scripts/lib/cursor-result.py" |
  grep -Eiq "$review_pattern" ||
  fail "review verdict parser did not decode the terminal Cursor result"

# Production execution is macOS-only. On other systems no test override may
# accidentally turn a real invocation into a development lane.
if [[ "$(uname -s)" != "Darwin" ]]; then
  expect_failure "non-Darwin invocation" env -u FACTORY_DEV_LANE_TEST_MODE \
    -u FACTORY_TRUSTED_TEST_HARNESS bash "$LANE" mock
  grep -Eqi 'macOS|Darwin' "$OUT" || fail "non-Darwin invocation did not fail on platform"
fi

started=$SECONDS
test_env bash "$LANE" mock --keep >"$OUT"
elapsed=$((SECONDS - started))
[[ "$(cksum "$CALLER_HOME/.factory/sentinel" \
  "$CALLER_HOME/.hermes/profiles/factory/sentinel" \
  "$CALLER_HOME/Library/LaunchAgents/sentinel" \
  "$CALLER_HOME/Projects/nysa-company/nysa-app/sentinel")" == "$sentinels_before" ]] ||
  fail "mock changed caller production sentinels"
[[ "$(find "$CALLER_HOME/.factory" "$CALLER_HOME/.hermes/profiles/factory" \
  "$CALLER_HOME/Library/LaunchAgents" "$CALLER_HOME/Projects/nysa-company/nysa-app" \
  -type f | wc -l | tr -d ' ')" -eq 4 ]] ||
  fail "mock added caller production state"
lane_root="$(sed -n 's/^ROOT=//p' "$OUT")"
[[ "$lane_root" == "$TMP/lanes"/nysa-sf-dev.* ]] || fail "mock returned an unsafe root"
[[ "$(grep -c '^ROOT=' "$OUT")" -eq 1 ]] || fail "mock did not return one root"
grep -qx 'STATUS=AWAIT-OPERATOR' "$OUT" || fail "mock did not stop at operator approval"
grep -qx 'ROLES=planner,spec-linter,test-author,builder,reviewer,narrator' "$OUT" ||
  fail "mock role order changed"
reported_elapsed="$(sed -n 's/^ELAPSED_SECONDS=//p' "$OUT")"
[[ "$reported_elapsed" =~ ^[0-9]+$ && "$reported_elapsed" -lt 900 && "$elapsed" -lt 900 ]] ||
  fail "mock exceeded the 15-minute ceiling"

[[ -f "$lane_root/marker.json" ]] || fail "lane ownership marker is missing"
python3 - "$lane_root" "$TMP/lanes" <<'PY' || fail "lane ownership marker is not bound to its root"
import json, os, stat, sys
root, tmp_parent = sys.argv[1:]
r = os.lstat(root)
m = os.lstat(os.path.join(root, "marker.json"))
v = json.load(open(os.path.join(root, "marker.json"), encoding="utf-8"))
assert set(v) == {"schema", "root", "nonce", "kit_sha", "kit_tree", "mode",
                  "uid", "root_dev", "root_ino", "tmp_parent"}
assert v["root"] == root and v["uid"] == os.getuid()
assert v["root_dev"] == r.st_dev and v["root_ino"] == r.st_ino
assert v["tmp_parent"] == tmp_parent == os.path.dirname(root)
assert stat.S_ISDIR(r.st_mode) and stat.S_IMODE(r.st_mode) == 0o700
assert stat.S_ISREG(m.st_mode) and stat.S_IMODE(m.st_mode) == 0o600
assert m.st_uid == r.st_uid == os.getuid() and m.st_dev == r.st_dev and m.st_nlink == 1
assert m.st_ino != r.st_ino
PY
[[ -d "$lane_root/kit/.git" || -f "$lane_root/kit/.git" ]] || fail "lane-local kit is missing"
if find "$lane_root" -type f \( -name active.json -o -path '*/receipts/*.json' \) -print -quit |
   grep -q .; then
  fail "development lane created production activation evidence"
fi
[[ -d "$lane_root/origin.git" ]] || fail "local-only origin is missing"
[[ -d "$lane_root/worktrees/T-900001" ]] || fail "synthetic ticket worktree is missing"
grep -Fq 'ports `4781` and `4782`' \
  "$lane_root/worktrees/T-900001/factory/tickets/T-900001.md" ||
  fail "synthetic ticket does not reserve collision-free fixture ports"
grep -Fq 'JSON member order is not contractual' \
  "$lane_root/worktrees/T-900001/docs/engine-spec.md" ||
  fail "synthetic contract leaves JSON member order ambiguous"
grep -Fq 'duplicate webhook delivery' \
  "$lane_root/worktrees/T-900001/docs/acceptance/health-version.md" ||
  fail "synthetic contract leaves duplicate delivery in scope"
grep -qx 'State: Review' "$lane_root/worktrees/T-900001/factory/tickets/T-900001.md" ||
  fail "synthetic ticket did not remain in Review"
[[ -z "$(git -C "$lane_root/product" status --porcelain)" ]] || fail "synthetic product is dirty"
[[ -z "$(git -C "$lane_root/worktrees/T-900001" status --porcelain)" ]] ||
  fail "synthetic ticket worktree is dirty"
[[ "$(git -C "$lane_root/worktrees/T-900001" rev-parse HEAD)" == \
   "$(git -C "$lane_root/origin.git" rev-parse refs/heads/ticket/T-900001)" ]] ||
  fail "synthetic ticket branch was not pushed locally"

manifest_list="$TMP/manifests"
find "$lane_root/product/factory/runs" -type f -name '*.meta' -print | sort >"$manifest_list"
[[ "$(wc -l <"$manifest_list" | tr -d ' ')" -eq 6 ]] ||
  fail "mock did not retain exactly six role manifests"
python3 - "$manifest_list" <<'PY' || fail "role manifests are not successful and ordered"
import sys
expected = ["planner", "spec-linter", "test-author", "builder", "reviewer", "narrator"]
actual = []
with open(sys.argv[1], encoding="utf-8") as paths:
    for raw_path in paths:
        values = dict(line.rstrip("\n").split("=", 1) for line in open(raw_path.rstrip("\n"), encoding="utf-8") if "=" in line)
        assert values["accounting_state"] == "completed", raw_path
        assert values["exit_status"] == "0", raw_path
        actual.append(values["role"])
assert sorted(actual) == sorted(expected), actual
PY
python3 - "$lane_root/product/factory/runtime-ledger.csv" <<'PY' || fail "runtime ledger role order changed"
import csv, sys
expected = ["planner", "spec-linter", "test-author", "builder", "reviewer", "narrator"]
with open(sys.argv[1], newline="", encoding="utf-8") as handle:
    rows = list(csv.DictReader(handle))
assert [row["role"] for row in rows] == expected, rows
assert all(row["exit_status"] == "0" for row in rows), rows
PY

profile="$lane_root/runtime/mock.sb"
[[ -f "$profile" ]] || fail "Seatbelt profile was not retained"
grep -Eq '\(deny +default\)' "$profile" || fail "mock profile is not default-deny"
grep -Eq '\(deny +network' "$profile" || fail "mock profile does not deny network"
grep -Fq "$lane_root" "$profile" || fail "mock profile does not bind filesystem access to its lane"
for forbidden in "$CALLER_HOME/.factory" "$CALLER_HOME/.hermes/profiles/factory" \
  "$CALLER_HOME/Library/LaunchAgents" "$CALLER_HOME/Projects/nysa-company/nysa-app"; do
  grep -Fq "$forbidden" "$profile" && fail "mock profile names production path: $forbidden"
done

chmod 755 "$lane_root"
expect_failure "root mode drift cleanup" clean_cmd "$lane_root"
chmod 700 "$lane_root"
chmod 644 "$lane_root/marker.json"
expect_failure "marker mode drift cleanup" clean_cmd "$lane_root"
chmod 600 "$lane_root/marker.json"

ln "$lane_root/marker.json" "$TMP/marker.link"
expect_failure "marker link-count drift cleanup" clean_cmd "$lane_root"
rm "$TMP/marker.link"

root_saved="$TMP/root.saved"
mv "$lane_root" "$root_saved"
mkdir -m 700 "$lane_root"
mv "$root_saved/marker.json" "$lane_root/marker.json"
expect_failure "root inode drift cleanup" clean_cmd "$lane_root"
mv "$lane_root/marker.json" "$root_saved/marker.json"
rmdir "$lane_root"
mv "$root_saved" "$lane_root"

mkdir "$TMP/other-parent"
expect_failure "TMP parent drift cleanup" env TMPDIR="$TMP/other-parent" \
  bash "$LANE" clean --root "$lane_root"

clean_cmd "$lane_root"
[[ ! -e "$lane_root" ]] || fail "clean retained the lane"
expect_failure "repeat cleanup" clean_cmd "$lane_root"
expect_failure "relative cleanup" clean_cmd relative
unmarked="$TMP/lanes/nysa-sf-dev.unmarked"
forged="$TMP/lanes/nysa-sf-dev.forged"
lane_link="$TMP/lanes/nysa-sf-dev.link"
mkdir -p "$unmarked"
expect_failure "unmarked cleanup" clean_cmd "$unmarked"
ln -s "$unmarked" "$lane_link"
expect_failure "symlink cleanup" clean_cmd "$lane_link"
mkdir -p "$forged"
printf '{}\n' >"$forged/marker.json"
expect_failure "forged cleanup" clean_cmd "$forged"
[[ -d "$unmarked" && -d "$forged" ]] || fail "refused cleanup removed data"

started=$SECONDS
test_env bash "$LANE" mock-concurrency --keep >"$OUT"
elapsed=$((SECONDS - started))
concurrency_root="$(sed -n 's/^ROOT=//p' "$OUT")"
[[ "$concurrency_root" == "$TMP/lanes"/nysa-sf-dev.* ]] ||
  fail "concurrency mock returned an unsafe root"
grep -qx 'PROVIDER_CALLS=4' "$OUT" || fail "concurrency mock did not run four providers"
grep -qx 'PROVIDER_MODE=cli-concurrent-v1' "$OUT" ||
  fail "concurrency mock did not use the subscription CLI coordinator path"
overlap_ms="$(sed -n 's/^PROVIDER_OVERLAP_MILLISECONDS=//p' "$OUT")"
[[ "$overlap_ms" =~ ^[0-9]+$ && "$overlap_ms" -ge 2000 && "$overlap_ms" -lt 5000 ]] ||
  fail "four provider calls did not overlap for one bounded interval"
reported_elapsed="$(sed -n 's/^ELAPSED_SECONDS=//p' "$OUT")"
[[ "$reported_elapsed" =~ ^[0-9]+$ && "$reported_elapsed" -lt 900 && "$elapsed" -lt 900 ]] ||
  fail "four lifecycle batch exceeded the 15-minute ceiling"
[[ "$(cksum "$CALLER_HOME/.factory/sentinel" \
  "$CALLER_HOME/.hermes/profiles/factory/sentinel" \
  "$CALLER_HOME/Library/LaunchAgents/sentinel" \
  "$CALLER_HOME/Projects/nysa-company/nysa-app/sentinel")" == "$sentinels_before" ]] ||
  fail "concurrency mock changed production sentinels"
python3 "$concurrency_root/kit/scripts/provider-coordinator.py" \
  --db "$concurrency_root/runtime/provider-state.sqlite3" status | python3 -c '
import json, sys
value=json.load(sys.stdin)
assert value["counts"] == {"terminal":28}, value
assert value["active_reserve_micro_usd"] == 0, value
' || fail "concurrency mock retained provider capacity or reservations"
[[ "$(find "$concurrency_root/product/factory/runs" -type f -name '*.meta' | wc -l | tr -d ' ')" -eq 24 ]] ||
  fail "four lifecycle batch did not record 24 role runs"
if find "$concurrency_root/product/factory/runs" -type f -name '*.meta' -exec \
     grep -L '^provider_execution_mode=cli-concurrent-v1$' {} + | grep -q .; then
  fail "synthetic lifecycles did not use CLI concurrent admission"
fi
for ticket in T-900001 T-900002 T-900003 T-900004; do
  work="$concurrency_root/worktrees/$ticket"
  grep -qx 'State: Review' "$work/factory/tickets/$ticket.md" ||
    fail "$ticket did not complete in Review"
  [[ "$(git -C "$work" rev-parse HEAD)" == \
     "$(git -C "$concurrency_root/origin.git" rev-parse "refs/heads/ticket/$ticket")" ]] ||
    fail "$ticket trusted host output was not pushed locally"
done
if find "$concurrency_root/product/factory" -type f -name '*.pid' -print -quit | grep -q . ||
   find "$concurrency_root" -type f \( -name active.json -o -path '*/receipts/*.json' \) -print -quit | grep -q .; then
  fail "concurrency mock retained a live process or activation artifact"
fi
clean_cmd "$concurrency_root"
[[ ! -e "$concurrency_root" ]] || fail "concurrency cleanup retained its lane"

# Real Cursor cannot authenticate inside a nested Seatbelt profile. Its lane
# uses Cursor's own explicit sandbox; only mock mode invokes sandbox-exec.
mv "$FAKE_SANDBOX" "$FAKE_SANDBOX.disabled"
cursor_env bash "$LANE" cursor-plan >"$OUT"
[[ "$(cksum "$CALLER_HOME/.cursor/auth.json" "$CALLER_HOME/.cursor/cli-config.json")" == \
   "$cursor_session_before" ]] || fail "Cursor planning changed the normal session files"
[[ ! -e "$TMP/cursor-tmp-bridge" && ! -L "$TMP/cursor-tmp-bridge" ]] ||
  fail "Cursor temporary bridge remained after planning"
cursor_root="$(sed -n 's/^ROOT=//p' "$OUT")"
approval_hash="$(sed -n 's/^APPROVE_HASH=//p' "$OUT")"
[[ "$cursor_root" == "$TMP/lanes"/nysa-sf-dev.* ]] || fail "cursor plan returned an unsafe root"
[[ "$approval_hash" =~ ^[0-9a-f]{64}$ ]] || fail "cursor plan returned an invalid approval hash"
grep -Fq "$TMP/cursor-tmp-bridge" "$cursor_root/runtime/cursor.sb" ||
  fail "Cursor profile does not bind its ephemeral temporary bridge"
grep -Fq "$CALLER_HOME/.cursor/auth.json" "$cursor_root/runtime/cursor.sb" ||
  fail "Cursor profile does not bind the exact session file"
grep -Fq "$CALLER_HOME/Library/Keychains" "$cursor_root/runtime/cursor.sb" ||
  fail "Cursor profile does not bind the login Keychain database"
grep -Fq 'Shell(security)' "$cursor_root/worktrees/T-900001/.cursor/cli.json" ||
  fail "synthetic product does not deny agent-initiated Keychain commands"
grep -Fq '"allow":[]' "$cursor_root/worktrees/T-900001/.cursor/cli.json" ||
  fail "synthetic Cursor permissions omit the required allow list"
grep -Fq 'com.apple.securityd' "$cursor_root/runtime/cursor.sb" &&
  fail "Cursor profile blocks the authenticated CLI session"
bad_hash="${approval_hash%?}0"
[[ "$bad_hash" != "$approval_hash" ]] || bad_hash="${approval_hash%?}1"
expect_failure "wrong cursor approval hash" cursor_env bash "$LANE" cursor-run \
  --root "$cursor_root" --approve-hash "$bad_hash"
cp "$FAKE_CURSOR" "$TMP/cursor-original"
printf '\n# changed after approval\n' >>"$FAKE_CURSOR"
expect_failure "Cursor binary byte drift" cursor_env bash "$LANE" cursor-run \
  --root "$cursor_root" --approve-hash "$approval_hash"
[[ -f "$cursor_root/runtime/cursor-approval" ]] ||
  fail "Cursor binary drift consumed the approval"
cp "$TMP/cursor-original" "$FAKE_CURSOR"
chmod +x "$FAKE_CURSOR"
# The fake provider fails after authorization. The approval must still be
# consumed so a post-submission failure cannot be replayed.
expect_failure "fake cursor execution" cursor_env bash "$LANE" cursor-run \
  --root "$cursor_root" --approve-hash "$approval_hash"
grep -qx 'State: Ready' "$cursor_root/worktrees/T-900001/factory/tickets/T-900001.md" ||
  fail "failed provider output advanced ticket state"
[[ "$(git -C "$cursor_root/worktrees/T-900001" rev-parse HEAD)" == \
   "$(git -C "$cursor_root/origin.git" rev-parse refs/heads/ticket/T-900001)" ]] ||
  fail "failed provider output changed the trusted remote"
grep -qx -- '--sandbox' "$cursor_root/home/cursor-args" ||
  fail "real Cursor lane did not enable Cursor's internal sandbox"
grep -qx -- 'enabled' "$cursor_root/home/cursor-args" ||
  fail "real Cursor lane did not select the enabled sandbox mode"
[[ ! -e "$TMP/cursor-tmp-bridge" && ! -L "$TMP/cursor-tmp-bridge" ]] ||
  fail "Cursor temporary bridge remained after failed execution"
expect_failure "reused cursor approval hash" cursor_env bash "$LANE" cursor-run \
  --root "$cursor_root" --approve-hash "$approval_hash"
clean_cmd "$cursor_root"

printf 'PASS: isolated factory development lane\n'
