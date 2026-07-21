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
trap 'status=$?; printf "FAIL: unexpected command at line %s (exit %s)\n" "${BASH_LINENO[0]:-$LINENO}" "$status" >&2; [[ ! -s "$OUT" ]] || sed "s/factory-dev-lane-dummy-key/[redacted]/g" "$OUT" >&2; exit "$status"' ERR

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
  FACTORY_DEV_CURSOR_CREDENTIAL=dedicated \
  CURSOR_API_KEY=factory-dev-lane-dummy-key \
  HOME="$CALLER_HOME" \
  TMPDIR="$TMP/lanes" \
  "$@"
}

clean_cmd() { TMPDIR="$TMP/lanes" bash "$LANE" clean --root "$1"; }

mkdir -p "$TMP/lanes" "$CALLER_HOME/.factory" \
  "$CALLER_HOME/.hermes/profiles/factory" "$CALLER_HOME/Library/LaunchAgents"
printf 'factory production sentinel\n' >"$CALLER_HOME/.factory/sentinel"
printf 'profile production sentinel\n' >"$CALLER_HOME/.hermes/profiles/factory/sentinel"
printf 'service production sentinel\n' >"$CALLER_HOME/Library/LaunchAgents/sentinel"
sentinels_before="$(cksum "$CALLER_HOME/.factory/sentinel" \
  "$CALLER_HOME/.hermes/profiles/factory/sentinel" \
  "$CALLER_HOME/Library/LaunchAgents/sentinel")"
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
  --help) printf '%s\n' --print --output-format --workspace --model --force --trust ;;
  status) printf '{"authenticated":true}\n' ;;
  models) printf '%s\n' gpt-5.6-sol-high claude-fable-5-thinking-medium \
    claude-sonnet-5-thinking-high ;;
  *) exit 42 ;;
esac
EOF
chmod +x "$FAKE_CURSOR"

[[ -x "$LANE" ]] || fail "development lane wrapper is not executable"

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
  "$CALLER_HOME/Library/LaunchAgents/sentinel")" == "$sentinels_before" ]] ||
  fail "mock changed caller production sentinels"
[[ "$(find "$CALLER_HOME/.factory" "$CALLER_HOME/.hermes/profiles/factory" \
  "$CALLER_HOME/Library/LaunchAgents" -type f | wc -l | tr -d ' ')" -eq 3 ]] ||
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
[[ -d "$lane_root/kit/.git" || -f "$lane_root/kit/.git" ]] || fail "lane-local kit is missing"
if find "$lane_root" -type f \( -name active.json -o -path '*/receipts/*.json' \) -print -quit |
   grep -q .; then
  fail "development lane created production activation evidence"
fi
[[ -d "$lane_root/origin.git" ]] || fail "local-only origin is missing"
[[ -d "$lane_root/worktrees/T-900001" ]] || fail "synthetic ticket worktree is missing"
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

profile="$lane_root/runtime/mock.sb"
[[ -f "$profile" ]] || fail "Seatbelt profile was not retained"
grep -Eq '\(deny +default\)' "$profile" || fail "mock profile is not default-deny"
grep -Eq '\(deny +network' "$profile" || fail "mock profile does not deny network"
grep -Fq "$lane_root" "$profile" || fail "mock profile does not bind filesystem access to its lane"
for forbidden in "$CALLER_HOME/.factory" "$CALLER_HOME/.hermes/profiles/factory" \
  "$CALLER_HOME/Library/LaunchAgents" "/Users/sofiagonzalez/Projects/nysa-company/nysa-app"; do
  grep -Fq "$forbidden" "$profile" && fail "mock profile names production path: $forbidden"
done

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

expect_failure "cursor plan without credential" test_env bash "$LANE" cursor-plan
expect_failure "cursor plan with caller credential" env CURSOR_API_KEY=fake \
  FACTORY_DEV_LANE_TEST_MODE=1 FACTORY_TRUSTED_TEST_HARNESS=1 \
  FACTORY_DEV_LANE_UNAME=Darwin FACTORY_DEV_LANE_SANDBOX_EXEC="$FAKE_SANDBOX" \
  TMPDIR="$TMP/lanes" bash "$LANE" cursor-plan

cursor_env bash "$LANE" cursor-plan >"$OUT"
cursor_root="$(sed -n 's/^ROOT=//p' "$OUT")"
approval_hash="$(sed -n 's/^APPROVE_HASH=//p' "$OUT")"
[[ "$cursor_root" == "$TMP/lanes"/nysa-sf-dev.* ]] || fail "cursor plan returned an unsafe root"
[[ "$approval_hash" =~ ^[0-9a-f]{64}$ ]] || fail "cursor plan returned an invalid approval hash"
if grep -R -Fq 'factory-dev-lane-dummy-key' "$cursor_root"; then
  fail "cursor credential was persisted in the lane"
fi
bad_hash="${approval_hash%?}0"
[[ "$bad_hash" != "$approval_hash" ]] || bad_hash="${approval_hash%?}1"
expect_failure "wrong cursor approval hash" cursor_env bash "$LANE" cursor-run \
  --root "$cursor_root" --approve-hash "$bad_hash"
# The fake provider fails after authorization. The approval must still be
# consumed so a post-submission failure cannot be replayed.
expect_failure "fake cursor execution" cursor_env bash "$LANE" cursor-run \
  --root "$cursor_root" --approve-hash "$approval_hash"
expect_failure "reused cursor approval hash" cursor_env bash "$LANE" cursor-run \
  --root "$cursor_root" --approve-hash "$approval_hash"
clean_cmd "$cursor_root"

printf 'PASS: isolated factory development lane\n'
