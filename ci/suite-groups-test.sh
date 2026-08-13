#!/usr/bin/env bash
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FIXTURE_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/suite-groups-test.XXXXXX")"
trap 'rm -rf "$FIXTURE_ROOT"' EXIT
. "$ROOT/ci/suite-registry.sh"
. "$ROOT/ci/suite-groups.sh"

FAIL=0
SEEN=" "
TOTAL=0
COUNT_factory_1=0 COUNT_factory_2=0 COUNT_factory_3=0 COUNT_factory_4=0
COUNT_hermes_1=0 COUNT_hermes_2=0 COUNT_hermes_3=0 COUNT_hermes_4=0
COUNT_release_1=0 COUNT_release_2=0 COUNT_release_3=0 COUNT_release_4=0

fail() {
  echo "FAIL: $*" >&2
  FAIL=1
}

check_suite() {
  local id="$1" shard group expected_command="" expected_group=""
  shard="$(suite_shard_for "$id")"
  group="$(suite_group_for "$id")"

  case "$shard" in factory|hermes|release) ;; *) fail "$id has invalid shard $shard"; return ;; esac
  case "$group" in 1|2|3|4) ;; *) fail "$id has invalid group $group"; return ;; esac
  if [[ "$SEEN" == *" $id "* ]]; then
    fail "$id is mapped more than once"
    return
  fi
  SEEN="$SEEN$id "
  TOTAL=$((TOTAL + 1))
  eval "COUNT_${shard}_${group}=\$((COUNT_${shard}_${group} + 1))"

  case "$id" in
    factory-scripts) expected_command="$ROOT/ci/test-factory-scripts.sh"; expected_group=1 ;;
    hermes-contract) expected_command="$ROOT/ci/hermes-contract-test.sh"; expected_group=2 ;;
    factory-kit) expected_command="$ROOT/ci/factory-kit-test.sh"; expected_group=3 ;;
    factory-controller|ticket-passport) expected_group=1 ;;
    ticket-pr) expected_group=2 ;;
    model-fallback) expected_group=3 ;;
    ticket-transition-policy) expected_group=3 ;;
  esac
  if [[ -n "$expected_group" && "$group" != "$expected_group" ]]; then
    fail "$id no longer has its timing-balanced group $expected_group"
  fi
  if [[ -n "$expected_command" && ( "$3" != "bash" || "$4" != "$expected_command" ) ]]; then
    fail "$id no longer preserves its canonical sequential lifecycle command in its pinned group"
  fi
}

suite_registry check_suite
[[ "$TOTAL" -gt 0 ]] || fail "suite registry is empty"
[[ "$SUITE_GROUP_COUNT" -eq 4 ]] || fail "suite orchestration must retain four groups"

for group in 1 2 3 4; do
  eval "count=\$((COUNT_factory_${group} + COUNT_hermes_${group} + COUNT_release_${group}))"
  [[ "$count" -gt 0 ]] || fail "global group $group is empty"
done

GROUP_ROOTS=" "
for group in 1 2 3 4; do
  child_root="$(suite_group_tmp "$FIXTURE_ROOT" "$group")"
  [[ "$child_root" == "$FIXTURE_ROOT"/group-* ]] || fail "group $group escaped its fixture root"
  [[ "$GROUP_ROOTS" != *" $child_root "* ]] || fail "group $group shares a fixture root"
  GROUP_ROOTS="$GROUP_ROOTS$child_root "
  mkdir -p "$child_root"
  printf '%s\n' "$group" > "$child_root/owner"
done

for group in 1 2 3 4; do
  child_root="$(suite_group_tmp "$FIXTURE_ROOT" "$group")"
  [[ "$(awk 'NR == 1 { print; exit }' "$child_root/owner")" == "$group" ]] || fail "group $group fixture root was overwritten"
done

if bash "$ROOT/ci/test-all.sh" --group 5 > /dev/null 2>&1; then
  fail "test-all accepted an invalid group"
fi

for group in 2 3; do
  child_root="$(suite_group_tmp "$FIXTURE_ROOT" "$group")"
  TMPDIR="$child_root" bash "$ROOT/ci/test-all.sh" --shard factory --group "$group" > "$child_root/direct.out" 2>&1 &
  printf '%s\n' "$!" > "$child_root/direct.pid"
done
for group in 2 3; do
  child_root="$(suite_group_tmp "$FIXTURE_ROOT" "$group")"
  pid="$(awk 'NR == 1 { print; exit }' "$child_root/direct.pid")"
  wait "$pid" || fail "direct concurrent factory group $group failed"
  grep -q "PASS: factory group $group test suite" "$child_root/direct.out" || fail "direct factory group $group omitted its result"
done

[[ "$FAIL" -eq 0 ]] || exit 1
echo "PASS: all $TOTAL suites have one complete shard/group mapping and isolated fixture roots"
