#!/usr/bin/env bash
# Install, certify, and activate immutable software-factory kit releases.
# Compatible with the Bash 3.2 shipped by macOS.
set -euo pipefail

PROGRAM="$(basename "$0")"
SCRIPT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RAW_KITS_ROOT="${FACTORY_KITS_ROOT:-$HOME/.factory/kits}"
KITS_ROOT="$(python3 -c 'import os,sys; print(os.path.abspath(sys.argv[1]))' "$RAW_KITS_ROOT")"
RELEASES_DIR="$KITS_ROOT/releases"
MANIFESTS_DIR="$KITS_ROOT/manifests"
PROJECTS_DIR="$KITS_ROOT/projects"
RECEIPTS_DIR="$KITS_ROOT/receipts"
CONSUMED_DIR="$RECEIPTS_DIR/consumed"
CANONICAL_GITHUB_ORIGIN="github.com/nysa-company/software-factory"
RECEIPT_SCHEMA=2
INSTALL_MANIFEST_SCHEMA=1
SUITE_EVIDENCE_SCHEMA=2
CERTIFICATION_TOOL_VERSION=2
# Bump whenever run_kit_checks_isolated command composition or semantics change.
KIT_SUITE_DEFINITION="factory-kit-suite-v2"
DEFAULT_RECEIPT_TTL="${FACTORY_KIT_RECEIPT_TTL_SECONDS:-86400}"
DEFAULT_SUITE_EVIDENCE_TTL="${FACTORY_KIT_SUITE_EVIDENCE_TTL_SECONDS:-86400}"

# shellcheck disable=SC1091
source "$SCRIPT_ROOT/scripts/lib/dispatch-leases.sh"

HELD_LOCKS=""
TEMP_PATHS=""
PREPARED_COPY=""
PREPARED_PRODUCT=""
ISOLATED_HOME=""

say() { printf '%s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

cleanup() {
  local status=$? item path identity device inode kind nonce
  trap - EXIT
  if [[ -n "$TEMP_PATHS" ]]; then
    printf '%s' "$TEMP_PATHS" | while IFS= read -r item; do
      [[ -n "$item" ]] || continue
      kind="${item##*|}"
      identity="${item%|*}"
      inode="${identity##*|}"
      identity="${identity%|*}"
      device="${identity##*|}"
      path="${identity%|*}"
      safe_remove_owned_temp "$path" "$device" "$inode" "$kind" >/dev/null 2>&1 || true
    done
  fi
  if [[ -n "$HELD_LOCKS" ]]; then
    printf '%s' "$HELD_LOCKS" | while IFS= read -r item; do
      [[ -n "$item" ]] || continue
      path="${item%%|*}"
      nonce="${item#*|}"
      release_owned_lock "$path" "$nonce" >/dev/null 2>&1 || true
    done
  fi
  return "$status"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

remember_temp() {
  local path="$1" identity
  identity="$(python3 - "$path" <<'PY'
import os, stat, sys
value = os.lstat(sys.argv[1])
if stat.S_ISLNK(value.st_mode):
    raise SystemExit("remembered temporary path is a symlink")
if stat.S_ISDIR(value.st_mode):
    kind = "d"
elif stat.S_ISREG(value.st_mode):
    kind = "f"
else:
    raise SystemExit("remembered temporary path has an unsupported type")
print("%s|%s|%s" % (value.st_dev, value.st_ino, kind))
PY
)" || die "could not record temporary directory ownership: $path"
  TEMP_PATHS="${path}|${identity}
${TEMP_PATHS}"
}

forget_temp() {
  local target="$1" item next=""
  while IFS= read -r item; do
    [[ -n "$item" && "${item%|*|*|*}" != "$target" ]] &&
      next="${next}${item}
"
  done <<EOF
$TEMP_PATHS
EOF
  TEMP_PATHS="$next"
}

safe_remove_owned_temp() {
  python3 - "$1" "$2" "$3" "$4" <<'PY'
import os, pathlib, shutil, stat, sys
root = pathlib.Path(sys.argv[1])
expected_device, expected_inode = map(int, sys.argv[2:4])
expected_kind = sys.argv[4]
try:
    value = root.lstat()
except FileNotFoundError:
    raise SystemExit(0)
if (
    stat.S_ISLNK(value.st_mode) or
    value.st_dev != expected_device or value.st_ino != expected_inode
):
    raise SystemExit(0)
if expected_kind == "f":
    if not stat.S_ISREG(value.st_mode):
        raise SystemExit(0)
    os.chmod(str(root), stat.S_IMODE(value.st_mode) | stat.S_IRUSR | stat.S_IWUSR)
    value = root.lstat()
    if value.st_dev == expected_device and value.st_ino == expected_inode:
        root.unlink()
    raise SystemExit(0)
if expected_kind != "d" or not stat.S_ISDIR(value.st_mode):
    raise SystemExit(0)
for directory, names, files in os.walk(str(root), topdown=False, followlinks=False):
    for name in files:
        path = pathlib.Path(directory) / name
        try:
            mode = path.lstat().st_mode
            if not stat.S_ISLNK(mode):
                os.chmod(str(path), stat.S_IMODE(mode) | stat.S_IRUSR | stat.S_IWUSR)
        except FileNotFoundError:
            pass
    for name in names:
        path = pathlib.Path(directory) / name
        try:
            mode = path.lstat().st_mode
            if not stat.S_ISLNK(mode):
                os.chmod(
                    str(path),
                    stat.S_IMODE(mode) | stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR,
                )
        except FileNotFoundError:
            pass
    mode = os.lstat(directory).st_mode
    os.chmod(
        directory,
        stat.S_IMODE(mode) | stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR,
    )
value = root.lstat()
if value.st_dev != expected_device or value.st_ino != expected_inode:
    raise SystemExit(0)
shutil.rmtree(str(root))
PY
}

remember_lock() {
  HELD_LOCKS="${1}|${2}
${HELD_LOCKS}"
}

forget_lock() {
  local target="$1" nonce="$2" item next="" scratch
  scratch="${TMPDIR:-/tmp}/factory-kit-locks.$$.$nonce"
  printf '%s' "$HELD_LOCKS" | while IFS= read -r item; do
    [[ -z "$item" || "$item" == "$target|$nonce" ]] || printf '%s\n' "$item"
  done > "$scratch"
  if [[ -f "$scratch" ]]; then
    next="$(awk 'NF {print}' "$scratch")"
    rm -f "$scratch"
  fi
  HELD_LOCKS="$next"
}

usage() {
  cat <<EOF
Usage:
  $PROGRAM install   --sha FULL_SHA [--repo KIT_REPO] [--origin ORIGIN]
  $PROGRAM certify   --project SLUG --product PRODUCT_REPO --sha FULL_SHA
  $PROGRAM plan      --project SLUG --product PRODUCT_REPO --sha FULL_SHA [--receipt FILE]
  $PROGRAM pause     --project SLUG --product PRODUCT_REPO
  $PROGRAM activate  --project SLUG --product PRODUCT_REPO --sha FULL_SHA [--receipt FILE]
  $PROGRAM status    --project SLUG [--product PRODUCT_REPO] [--json]
  $PROGRAM reconcile --project SLUG [--product PRODUCT_REPO]
  $PROGRAM rollback  --project SLUG [--product PRODUCT_REPO]
  $PROGRAM recover-lease --project SLUG --product PRODUCT_REPO --ticket T-NNN

FACTORY_KITS_ROOT overrides the default state root (~/.factory/kits).
EOF
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"
}

validate_slug() {
  local slug="$1"
  [[ "$slug" =~ ^[a-z0-9][a-z0-9-]{0,62}$ ]] ||
    die "invalid project slug: $slug"
  [[ "$slug" != *".."* && "$slug" != *"/"* ]] ||
    die "invalid project slug: $slug"
}

reject_symlink_path_components() {
  python3 - "$1" <<'PY'
import os, pathlib, stat, sys
path = pathlib.Path(os.path.abspath(sys.argv[1]))
parts = path.parts
cursor = pathlib.Path(parts[0])
for part in parts[1:]:
    cursor = cursor / part
    try:
        mode = cursor.lstat().st_mode
    except FileNotFoundError:
        continue
    if stat.S_ISLNK(mode):
        raise SystemExit("managed path contains symlink: %s" % cursor)
PY
}

validate_managed_roots() {
  local slug="${1:-}" path
  reject_symlink_path_components "$RAW_KITS_ROOT" ||
    die "raw FACTORY_KITS_ROOT contains a symlink"
  for path in "$KITS_ROOT" "$RELEASES_DIR" "$MANIFESTS_DIR" \
    "$PROJECTS_DIR" "$RECEIPTS_DIR" "$CONSUMED_DIR"; do
    [[ ! -L "$path" ]] || die "managed state path may not be a symlink: $path"
  done
  if [[ -n "$slug" ]]; then
    [[ ! -L "$PROJECTS_DIR/$slug" ]] ||
      die "project state path may not be a symlink"
    [[ ! -L "$PROJECTS_DIR/$slug/activation-journal" ]] ||
      die "activation journal path may not be a symlink"
  fi
}

validate_managed_layout() {
  local slug="${1:-}"
  validate_managed_roots "$slug"
  if [[ -d "$KITS_ROOT" ]]; then
    python3 - "$KITS_ROOT" <<'PY' ||
import os, pathlib, sys
root = pathlib.Path(sys.argv[1])
for base, dirs, files in os.walk(str(root), followlinks=False):
    for name in dirs + files:
        if (pathlib.Path(base) / name).is_symlink():
            raise SystemExit("managed state symlink is forbidden")
PY
      die "managed state contains a symlink"
  fi
}

safe_create_directory() {
  python3 - "$1" <<'PY'
import os, pathlib, stat, sys
path = pathlib.Path(os.path.abspath(sys.argv[1]))
cursor = pathlib.Path(path.parts[0])
for part in path.parts[1:]:
    cursor = cursor / part
    try:
        st = cursor.lstat()
    except FileNotFoundError:
        try:
            cursor.mkdir(mode=0o700)
        except FileExistsError:
            pass
        st = cursor.lstat()
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise SystemExit("unsafe managed directory: %s" % cursor)
PY
}

ensure_managed_directories() {
  local slug="${1:-}"
  validate_managed_roots "$slug"
  safe_create_directory "$KITS_ROOT"
  safe_create_directory "$RELEASES_DIR"
  safe_create_directory "$MANIFESTS_DIR"
  safe_create_directory "$PROJECTS_DIR"
  safe_create_directory "$RECEIPTS_DIR"
  safe_create_directory "$CONSUMED_DIR"
  if [[ -n "$slug" ]]; then
    safe_create_directory "$PROJECTS_DIR/$slug"
    safe_create_directory "$PROJECTS_DIR/$slug/activation-journal"
  fi
  validate_managed_roots "$slug"
}

validate_project_storage() {
  local slug="$1"
  validate_managed_layout "$slug"
}

validate_sha() {
  local sha="$1"
  [[ "$sha" =~ ^[0-9a-f]{40}$ ]] ||
    die "kit SHA must be one full canonical lowercase 40-character SHA"
}

absolute_dir() {
  local path="$1"
  [[ -d "$path" ]] || die "directory not found: $path"
  (cd "$path" && pwd -P)
}

file_hash() {
  local path="$1"
  if [[ ! -f "$path" ]]; then
    printf 'missing\n'
  else
    shasum -a 256 "$path" | awk '{print $1}'
  fi
}

verify_restrictive_regular_file() {
  python3 - "$1" <<'PY'
import os, pathlib, stat, sys
path = pathlib.Path(sys.argv[1])
st = path.lstat()
if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
    raise SystemExit(1)
if st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) & 0o077:
    raise SystemExit(1)
PY
}

now_epoch() { date +%s; }
now_iso() { date -u '+%Y-%m-%dT%H:%M:%SZ'; }

host_name() {
  hostname 2>/dev/null || uname -n
}

canonical_origin_identity() {
  local origin="$1" value
  value="${origin%.git}"
  case "$value" in
    https://github.com/*) printf 'github.com/%s\n' "${value#https://github.com/}" ;;
    http://github.com/*) printf 'github.com/%s\n' "${value#http://github.com/}" ;;
    ssh://git@github.com/*) printf 'github.com/%s\n' "${value#ssh://git@github.com/}" ;;
    git@github.com:*) printf 'github.com/%s\n' "${value#git@github.com:}" ;;
    file://*) printf 'file://%s\n' "$(cd "${value#file://}" 2>/dev/null && pwd -P || printf '%s' "${value#file://}")" ;;
    /*) printf '%s\n' "$(cd "$value" 2>/dev/null && pwd -P || printf '%s' "$value")" ;;
    *) printf '%s\n' "$value" ;;
  esac
}

expected_origin_identity() {
  local configured="${FACTORY_KIT_CANONICAL_ORIGIN:-${FACTORY_KIT_ORIGIN:-}}"
  if [[ -n "$configured" ]]; then
    [[ "${FACTORY_KIT_TEST_MODE:-0}" == "1" ]] ||
      die "custom canonical origin is allowed only in FACTORY_KIT_TEST_MODE"
    canonical_origin_identity "$configured"
  else
    printf '%s\n' "$CANONICAL_GITHUB_ORIGIN"
  fi
}

validate_test_mode() {
  local configured="${FACTORY_KIT_CANONICAL_ORIGIN:-${FACTORY_KIT_ORIGIN:-}}"
  if [[ "${FACTORY_KIT_TEST_MODE:-0}" == "1" ]]; then
    [[ -n "$configured" ]] ||
      die "FACTORY_KIT_TEST_MODE requires an explicit local canonical origin"
    [[ "$(canonical_origin_identity "$configured")" != github.com/* ]] ||
      die "FACTORY_KIT_TEST_MODE may not target a GitHub production origin"
  fi
}

verify_origin() {
  local repo="$1" override="${2:-}" actual expected
  actual="$(git -C "$repo" remote get-url origin 2>/dev/null || true)"
  [[ -n "$actual" ]] || die "kit repository has no origin remote"
  [[ -z "$override" || "$(canonical_origin_identity "$override")" == "$(canonical_origin_identity "$actual")" ]] ||
    die "--origin does not match the repository origin"
  expected="$(expected_origin_identity)"
  [[ "$(canonical_origin_identity "$actual")" == "$expected" ]] ||
    die "wrong kit origin: expected $expected"
  printf '%s\n' "$actual"
}

verify_required_github_checks() {
  local sha="$1" data ruleset_id
  require_command gh
  data="$(mktemp -d "${TMPDIR:-/tmp}/factory-kit-github.XXXXXX")"
  remember_temp "$data"
  gh api --paginate --slurp \
    "repos/nysa-company/software-factory/rulesets?per_page=100" \
    > "$data/rulesets-pages.json" ||
    die "could not discover repository rulesets"
  if ! gh api --paginate --slurp \
      "repos/nysa-company/software-factory/branches/main/protection/required_status_checks" \
      > "$data/classic-pages.json" 2> "$data/classic-error"; then
    python3 - "$data/classic-error" <<'PY' ||
import pathlib, sys
raise SystemExit(0 if "404" in pathlib.Path(sys.argv[1]).read_text(errors="replace") else 1)
PY
      die "could not discover classic branch protection requirements"
    printf '[{}]\n' > "$data/classic-pages.json"
  fi
  python3 - "$data/rulesets-pages.json" <<'PY' > "$data/ruleset-ids"
import json, sys
pages = json.load(open(sys.argv[1]))
if pages and isinstance(pages[0], dict):
    pages = [pages]
for page in pages:
    for item in page:
        if item.get("enforcement") == "active" and item.get("target") == "branch":
            print(item["id"])
PY
  while IFS= read -r ruleset_id; do
    [[ -n "$ruleset_id" ]] || continue
    gh api "repos/nysa-company/software-factory/rulesets/$ruleset_id" \
      > "$data/ruleset-detail-$ruleset_id.json" ||
      die "could not read repository ruleset $ruleset_id"
  done < "$data/ruleset-ids"
  gh api --paginate --slurp \
    "repos/nysa-company/software-factory/commits/$sha/check-runs?per_page=100" \
    > "$data/check-run-pages.json" ||
    die "could not read GitHub check runs for $sha"
  gh api --paginate --slurp \
    "repos/nysa-company/software-factory/commits/$sha/statuses?per_page=100" \
    > "$data/status-pages.json" ||
    die "could not read GitHub commit statuses for $sha"
  python3 - "$data/classic-pages.json" "$data" \
    "$data/check-run-pages.json" "$data/status-pages.json" <<'PY' ||
import fnmatch, json, pathlib, sys

def pages(value):
    if not isinstance(value, list):
        return [value]
    return value

def ref_matches(pattern, ref):
    if pattern in ("~ALL", "~DEFAULT_BRANCH"):
        return True
    return fnmatch.fnmatchcase(ref, pattern)

classic_pages = pages(json.load(open(sys.argv[1])))
requirements = set()
for page in classic_pages:
    if not isinstance(page, dict):
        continue
    for context in page.get("contexts", []):
        if context:
            raise SystemExit("classic required status context is not app-bound: %s" % context)
    for item in page.get("checks", []):
        if item.get("context"):
            app_id = item.get("app_id")
            if app_id is None:
                raise SystemExit("classic required status check is not app-bound: %s" % item["context"])
            requirements.add((item["context"], int(app_id)))

applicable_rulesets = 0
has_pull_request_rule = False
has_required_status_rule = False
for detail in pathlib.Path(sys.argv[2]).glob("ruleset-detail-*.json"):
        value = json.loads(detail.read_text())
        conditions = value.get("conditions", {}).get("ref_name", {})
        include = conditions.get("include", [])
        exclude = conditions.get("exclude", [])
        applies = (not include or any(ref_matches(p, "refs/heads/main") for p in include))
        applies = applies and not any(ref_matches(p, "refs/heads/main") for p in exclude)
        if not applies:
            continue
        applicable_rulesets += 1
        if value.get("bypass_actors"):
            raise SystemExit("applicable main Ruleset has unsafe bypass actors")
        for rule in value.get("rules", []):
            if rule.get("type") == "pull_request":
                has_pull_request_rule = True
                continue
            if rule.get("type") != "required_status_checks":
                continue
            has_required_status_rule = True
            for item in rule.get("parameters", {}).get("required_status_checks", []):
                if item.get("context"):
                    integration_id = item.get("integration_id")
                    if integration_id is None:
                        raise SystemExit(
                            "Ruleset required status check is not app-bound: %s" %
                            item["context"]
                        )
                    requirements.add((item["context"], int(integration_id)))

if not applicable_rulesets:
    raise SystemExit("refs/heads/main has no applicable active branch Ruleset")
if not has_pull_request_rule:
    raise SystemExit("refs/heads/main Rulesets do not require pull requests")
if not has_required_status_rule:
    raise SystemExit("refs/heads/main Rulesets do not require status checks")
if not requirements:
    raise SystemExit("refs/heads/main has no required GitHub checks")

check_pages = pages(json.load(open(sys.argv[3])))
latest_checks = {}
for page in check_pages:
    if isinstance(page, dict):
        runs = page.get("check_runs", [])
    else:
        runs = page
    for run in runs:
        app_id = (run.get("app") or {}).get("id")
        key = (run.get("name"), app_id)
        if key[0] and (key not in latest_checks or int(run.get("id", 0)) > int(latest_checks[key].get("id", 0))):
            latest_checks[key] = run

# The statuses endpoint is fetched for complete, paginated policy evidence, but
# legacy commit statuses are deliberately never considered satisfiers.
pages(json.load(open(sys.argv[4])))

missing = []
for context, integration_id in sorted(requirements):
    run = latest_checks.get((context, integration_id))
    passed = bool(
        run and run.get("status") == "completed" and
        run.get("conclusion") == "success"
    )
    if not passed:
        missing.append("%s@%s" % (context, integration_id))
if missing:
    raise SystemExit("required GitHub checks are not successful: %s" % ", ".join(missing))
PY
    die "required GitHub checks are not successful for $sha"
}

verified_remote_full_ci() {
  local sha="$1" tree="$2" data
  if [[ "${FACTORY_KIT_TEST_MODE:-0}" == "1" ]]; then
    [[ "${FACTORY_KIT_TEST_REMOTE_FULL_CI:-0}" == "1" ]] || return 1
    printf '%s\n' "$(printf '%s' "test-remote-full-ci|$sha|$tree" | shasum -a 256 | awk '{print $1}')"
    return 0
  fi
  require_command gh
  data="$(mktemp -d "${TMPDIR:-/tmp}/factory-kit-remote-ci.XXXXXX")"
  remember_temp "$data"
  gh api --paginate --slurp \
    "repos/nysa-company/software-factory/actions/workflows/ci.yml/runs?head_sha=$sha&event=push&status=completed&branch=main&per_page=100" \
    > "$data/runs.json" || return 1
  python3 - "$data/runs.json" "$sha" <<'PY' > "$data/run-id" || return 1
import json, sys
pages = json.load(open(sys.argv[1]))
runs = []
for page in pages:
    runs.extend(page.get("workflow_runs", []))
valid = [run for run in runs if (
    run.get("head_sha") == sys.argv[2] and run.get("event") == "push" and
    run.get("head_branch") == "main" and run.get("status") == "completed" and
    run.get("conclusion") == "success" and
    run.get("path") == ".github/workflows/ci.yml"
)]
if not valid:
    raise SystemExit(1)
run = max(valid, key=lambda value: int(value.get("id", 0)))
print("%s\t%s" % (run["id"], run.get("run_attempt", 1)))
PY
  local run_id run_attempt
  run_id="$(awk -F'\t' '{print $1}' "$data/run-id")"
  run_attempt="$(awk -F'\t' '{print $2}' "$data/run-id")"
  gh api --paginate --slurp \
    "repos/nysa-company/software-factory/actions/runs/$run_id/attempts/$run_attempt/jobs?per_page=100" \
    > "$data/jobs.json" || return 1
  python3 - "$data/jobs.json" "$sha" "$tree" "$run_id" "$run_attempt" <<'PY'
import hashlib, json, sys
pages = json.load(open(sys.argv[1]))
jobs = []
for page in pages:
    jobs.extend(page.get("jobs", []))
latest = {}
for job in jobs:
    name = job.get("name")
    if name and (name not in latest or int(job.get("id", 0)) > int(latest[name].get("id", 0))):
        latest[name] = job
sharded = (
    "linux-factory", "linux-hermes", "linux-release",
    "macos-bash-3-factory", "macos-bash-3-hermes", "macos-bash-3-release",
)
legacy = ("linux", "macos-bash-3")
# A partial shard topology must never fall back to the legacy two-job proof.
platform = sharded if any(name in latest for name in sharded) else legacy
required = platform + ("ci", "test-immutability")
if any(latest.get(name, {}).get("conclusion") != "success" for name in required):
    raise SystemExit(1)
value = {
    "repository": "nysa-company/software-factory",
    "workflow": ".github/workflows/ci.yml",
    "event": "push",
    "ref": "refs/heads/main",
    "sha": sys.argv[2],
    "tree": sys.argv[3],
    "run_id": int(sys.argv[4]),
    "run_attempt": int(sys.argv[5]),
    "successful_jobs": list(required),
}
payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
print(hashlib.sha256(payload).hexdigest())
PY
}

process_start_identity() {
  local pid="$1"
  ps -o lstart= -p "$pid" 2>/dev/null | awk '{$1=$1; print; exit}'
}

random_nonce() {
  local nonce
  nonce="$(od -An -N16 -tx1 /dev/urandom 2>/dev/null | tr -d ' \n')"
  [[ "$nonce" =~ ^[0-9a-f]{32}$ ]] ||
    nonce="$(printf '%s|%s|%s\n' "$$" "$(now_epoch)" "$RANDOM" |
      shasum -a 256 | awk '{print substr($1,1,32)}')"
  printf '%s\n' "$nonce"
}

quarantine_stale_lock() {
  local lock="$1" expected_hash="$2" grace="$3" require_grace="$4" quarantine
  quarantine="$lock.stale.$(random_nonce)"
  python3 - "$lock" "$expected_hash" "$grace" "$require_grace" "$quarantine" <<'PY' || return 1
import hashlib, os, pathlib, stat, sys, time
lock = pathlib.Path(sys.argv[1])
expected = sys.argv[2]
grace = float(sys.argv[3])
require_grace = sys.argv[4] == "1"
quarantine = pathlib.Path(sys.argv[5])
st = lock.lstat()
if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
    raise SystemExit(1)
owner = lock / "owner"
if owner.exists() or owner.is_symlink():
    owner_st = owner.lstat()
    if stat.S_ISLNK(owner_st.st_mode) or not stat.S_ISREG(owner_st.st_mode):
        raise SystemExit(1)
    actual = hashlib.sha256(owner.read_bytes()).hexdigest()
else:
    actual = "missing"
age_anchor = st.st_mtime
if owner.exists() and not owner.is_symlink():
    age_anchor = max(age_anchor, owner.lstat().st_mtime)
if require_grace and time.time() - age_anchor < grace:
    raise SystemExit(1)
if actual != expected:
    raise SystemExit(1)
os.rename(str(lock), str(quarantine))
PY
  rm -rf "$quarantine"
}

release_owned_lock() {
  local lock="$1" nonce="$2" start quarantine
  start="$(process_start_identity "$$")"
  quarantine="$lock.release.$nonce"
  python3 - "$lock" "$nonce" "$$" "$start" "$quarantine" <<'PY' || return 1
import os, pathlib, stat, sys
lock, nonce, pid, start, quarantine = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4], pathlib.Path(sys.argv[5])
if not lock.exists() or lock.is_symlink() or not lock.is_dir():
    raise SystemExit(1)
owner = lock / "owner"
if not owner.is_file() or owner.is_symlink():
    raise SystemExit(1)
values = {}
for line in owner.read_text().splitlines():
    if "=" in line:
        key, value = line.split("=", 1)
        values[key] = value
if values.get("nonce") != nonce or values.get("pid") != pid or values.get("process_start") != start:
    raise SystemExit(1)
os.rename(str(lock), str(quarantine))
PY
  rm -rf "$quarantine"
}

acquire_lock() {
  local lock="$1" label="$2" attempts="${FACTORY_KIT_LOCK_ATTEMPTS:-100}"
  local grace="${FACTORY_KIT_LOCK_OWNER_GRACE_SECONDS:-2}" i=0
  local nonce start owner_pid owner_start owner_nonce owner_hash
  local owner_temp require_grace
  [[ "$attempts" =~ ^[0-9]+$ && "$attempts" -gt 0 ]] ||
    die "lock attempts must be positive"
  [[ "$grace" =~ ^[0-9]+([.][0-9]+)?$ ]] ||
    die "lock owner grace must be numeric"
  [[ -d "$(dirname "$lock")" && ! -L "$(dirname "$lock")" ]] ||
    die "$label lock parent is unsafe"
  nonce="$(random_nonce)"
  start="$(process_start_identity "$$")"
  [[ -n "$start" ]] || die "cannot determine lock process identity"
  while ! mkdir "$lock" 2>/dev/null; do
    # A valid holder may atomically rename and remove the lock after our mkdir
    # loses but before inspection. Absence means retry, not an unsafe path.
    if [[ ! -e "$lock" && ! -L "$lock" ]]; then
      continue
    fi
    [[ -d "$lock" && ! -L "$lock" ]] || die "$label lock path is unsafe"
    [[ ! -L "$lock/owner" ]] || die "$label lock owner is unsafe"
    owner_pid="$(awk -F= '$1=="pid" {print $2; exit}' "$lock/owner" 2>/dev/null || true)"
    owner_start="$(awk -F= '$1=="process_start" {print substr($0,index($0,"=")+1); exit}' "$lock/owner" 2>/dev/null || true)"
    owner_nonce="$(awk -F= '$1=="nonce" {print $2; exit}' "$lock/owner" 2>/dev/null || true)"
    # A concurrent holder may release the lock between the existence check
    # and the hash read; treat a vanished owner file as missing and retry.
    owner_hash="$(file_hash "$lock/owner" 2>/dev/null || true)"
    [[ -n "$owner_hash" ]] || owner_hash="missing"
    if [[ "$owner_pid" =~ ^[0-9]+$ && "$owner_nonce" =~ ^[0-9a-f]{32}$ &&
          -n "$owner_start" ]] &&
       kill -0 "$owner_pid" 2>/dev/null &&
       [[ "$(process_start_identity "$owner_pid")" == "$owner_start" ]]; then
      :
    else
      require_grace=1
      if [[ "$owner_pid" =~ ^[0-9]+$ && "$owner_nonce" =~ ^[0-9a-f]{32}$ &&
            -n "$owner_start" ]]; then
        require_grace=0
      fi
      if quarantine_stale_lock "$lock" "$owner_hash" "$grace" "$require_grace" 2>/dev/null; then
        continue
      fi
    fi
    i=$((i + 1))
    [[ "$i" -lt "$attempts" ]] || die "$label lock is busy"
    sleep 0.05
  done
  owner_temp="$lock/.owner.$nonce"
  {
    printf 'pid=%s\n' "$$"
    printf 'process_start=%s\n' "$start"
    printf 'nonce=%s\n' "$nonce"
    printf 'created_epoch=%s\n' "$(now_epoch)"
  } > "$owner_temp"
  chmod 600 "$owner_temp"
  mv "$owner_temp" "$lock/owner"
  python3 - "$lock/owner" "$lock" <<'PY'
import os, sys
with open(sys.argv[1], "rb") as stream:
    os.fsync(stream.fileno())
directory_fd = os.open(sys.argv[2], os.O_RDONLY)
try:
    os.fsync(directory_fd)
finally:
    os.close(directory_fd)
PY
  remember_lock "$lock" "$nonce"
  LAST_LOCK_NONCE="$nonce"
}

release_lock() {
  local lock="$1" item nonce=""
  while IFS= read -r item; do
    [[ "${item%%|*}" == "$lock" ]] && nonce="${item#*|}"
  done <<EOF
$HELD_LOCKS
EOF
  [[ -n "$nonce" ]] || die "refusing to release unowned lock: $lock"
  release_owned_lock "$lock" "$nonce" ||
    die "lock ownership changed before release: $lock"
  forget_lock "$lock" "$nonce"
}

git_tree_for_directory() {
  local directory="$1" object_dir index tree
  object_dir="$(mktemp -d "${TMPDIR:-/tmp}/factory-kit-objects.XXXXXX")"
  index="$object_dir/index"
  git init --bare -q "$object_dir/repo.git"
  git --git-dir="$object_dir/repo.git" config core.bare false
  GIT_INDEX_FILE="$index" git --git-dir="$object_dir/repo.git" \
    --work-tree="$directory" read-tree --empty
  GIT_INDEX_FILE="$index" git --git-dir="$object_dir/repo.git" \
    --work-tree="$directory" add -f -A -- .
  tree="$(GIT_INDEX_FILE="$index" git --git-dir="$object_dir/repo.git" \
    --work-tree="$directory" write-tree)"
  rm -rf "$object_dir"
  printf '%s\n' "$tree"
}

materialize_git_tree() {
  local source="$1" sha="$2" destination="$3"
  python3 - "$source" "$sha" "$destination" <<'PY'
import os
import pathlib
import re
import stat
import subprocess
import sys

source, sha, destination = sys.argv[1:]
root = pathlib.Path(destination).resolve()
listing = subprocess.run(
    ["git", "-C", source, "ls-tree", "-rz", "--full-tree", sha],
    check=True,
    stdout=subprocess.PIPE,
).stdout
for record in listing.split(b"\0"):
    if not record:
        continue
    try:
        metadata, raw_path = record.split(b"\t", 1)
        mode, kind, object_id = metadata.decode("ascii").split(" ")
    except (ValueError, UnicodeDecodeError):
        raise SystemExit("invalid Git tree entry")
    if kind != "blob" or not re.fullmatch(r"[0-9a-f]{40,64}", object_id):
        raise SystemExit("unsupported Git tree entry")
    path = pathlib.PurePosixPath(os.fsdecode(raw_path))
    if path.is_absolute() or not path.parts or any(part in ("", ".", "..") for part in path.parts):
        raise SystemExit("unsafe path in Git tree")
    target = root.joinpath(*path.parts)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink() or target.exists():
        raise SystemExit("duplicate or unsafe path in Git tree")
    content = subprocess.run(
        ["git", "-C", source, "cat-file", "blob", object_id],
        check=True,
        stdout=subprocess.PIPE,
    ).stdout
    if mode == "120000":
        os.symlink(os.fsdecode(content), target)
    elif mode in ("100644", "100755"):
        target.write_bytes(content)
        target.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)
        if mode == "100755":
            target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    else:
        raise SystemExit("unsupported mode in Git tree")
PY
}

verify_symlinks_contained() {
  python3 - "$1" <<'PY'
import os
import pathlib
import sys

root = pathlib.Path(sys.argv[1]).resolve()
for base, dirs, files in os.walk(str(root), followlinks=False):
    for name in dirs + files:
        path = pathlib.Path(base) / name
        if path.is_symlink():
            raise SystemExit("symlink is forbidden in managed release: %s" % path)
PY
}

verify_read_only() {
  python3 - "$1" <<'PY'
import os
import pathlib
import stat
import sys

root = pathlib.Path(sys.argv[1])
for path in [root] + list(root.rglob("*")):
    if path.is_symlink():
        continue
    if stat.S_IMODE(path.lstat().st_mode) & 0o222:
        raise SystemExit("writable installed path: %s" % path)
PY
}

seal_release_contents_for_publish() {
  python3 - "$1" <<'PY'
import os, pathlib, stat, sys
root = pathlib.Path(sys.argv[1])
paths = list(root.rglob("*"))
for path in reversed(paths):
    if path.is_symlink():
        raise SystemExit("symlink is forbidden in managed release: %s" % path)
    mode = stat.S_IMODE(path.lstat().st_mode)
    os.chmod(str(path), mode & ~0o222)
root_mode = stat.S_IMODE(root.lstat().st_mode)
os.chmod(str(root), (root_mode & ~0o022) | stat.S_IWUSR)
PY
}

verify_release_publish_ready() {
  python3 - "$1" <<'PY'
import pathlib, stat, sys
root = pathlib.Path(sys.argv[1])
mode = stat.S_IMODE(root.lstat().st_mode)
if not mode & stat.S_IWUSR or mode & (stat.S_IWGRP | stat.S_IWOTH):
    raise SystemExit("staging release root is not owner-writable only")
for path in root.rglob("*"):
    if path.is_symlink():
        raise SystemExit("symlink is forbidden in managed release: %s" % path)
    if stat.S_IMODE(path.lstat().st_mode) & 0o222:
        raise SystemExit("writable staged release content: %s" % path)
PY
}

fsync_directory() {
  python3 - "$1" <<'PY'
import os, sys
descriptor = os.open(sys.argv[1], os.O_RDONLY)
try:
    os.fsync(descriptor)
finally:
    os.close(descriptor)
PY
}

record_publish_phase() {
  local phase="$1" root="$2" release="$3" manifest="$4" trace
  trace="${FACTORY_KIT_TEST_PUBLISH_TRACE:-}"
  [[ -n "$trace" ]] || return 0
  [[ "${FACTORY_KIT_TEST_MODE:-0}" == "1" ]] ||
    die "publish tracing requires FACTORY_KIT_TEST_MODE"
  python3 - "$trace" "$phase" "$root" "$release" "$manifest" <<'PY'
import json, pathlib, stat, sys
trace, phase, root, release, manifest = map(pathlib.Path, sys.argv[1:])
root_mode = stat.S_IMODE(root.lstat().st_mode)
writable_descendants = 0
for path in root.rglob("*"):
    if not path.is_symlink() and stat.S_IMODE(path.lstat().st_mode) & 0o222:
        writable_descendants += 1
with trace.open("a") as stream:
    stream.write(json.dumps({
        "phase": str(phase),
        "root_owner_writable": bool(root_mode & stat.S_IWUSR),
        "root_any_writable": bool(root_mode & 0o222),
        "writable_descendants": writable_descendants,
        "release_exists": release.exists(),
        "manifest_exists": manifest.exists(),
    }, sort_keys=True) + "\n")
PY
}

maybe_fail_publish_phase() {
  local phase="$1" owned_path="${2:-}" requested quarantine
  requested="${FACTORY_KIT_TEST_FAIL_PUBLISH_PHASE:-}"
  [[ -z "$requested" || "${FACTORY_KIT_TEST_MODE:-0}" == "1" ]] ||
    die "publish fault injection requires FACTORY_KIT_TEST_MODE"
  [[ "$requested" == "$phase" ]] || return 0
  quarantine="${FACTORY_KIT_TEST_REPLACE_TEMP_BEFORE_CLEANUP:-}"
  if [[ -n "$quarantine" ]]; then
    [[ "$phase" == "contents_sealed" && -n "$owned_path" ]] ||
      die "temporary replacement hook is valid only for sealed contents"
    [[ ! -e "$quarantine" && ! -L "$quarantine" ]] ||
      die "temporary replacement quarantine already exists"
    mv "$owned_path" "$quarantine"
    mkdir "$owned_path"
    printf 'foreign replacement\n' > "$owned_path/foreign-marker"
    chmod a-w "$owned_path/foreign-marker" "$owned_path"
  fi
  die "injected failure after publish phase $phase"
}

verify_release() {
  local sha="$1" expected_tree="$2" release actual_tree
  release="$RELEASES_DIR/$sha"
  [[ -d "$release" ]] || die "release is not installed: $sha"
  [[ ! -L "$release" ]] || die "release path may not be a symlink"
  [[ ! -e "$release/.git" ]] || die "release contains unexpected Git metadata"
  verify_symlinks_contained "$release" ||
    die "release contains an unsafe symlink"
  actual_tree="$(git_tree_for_directory "$release")"
  [[ "$actual_tree" == "$expected_tree" ]] ||
    die "release tree mismatch for $sha"
  verify_read_only "$release" ||
    die "release is not sealed read-only"
}

manifest_file_for() {
  printf '%s/%s.json\n' "$MANIFESTS_DIR" "$1"
}

suite_evidence_file_for() {
  printf '%s/%s.suite.json\n' "$MANIFESTS_DIR" "$1"
}

validate_suite_evidence_ttl() {
  [[ "$DEFAULT_SUITE_EVIDENCE_TTL" =~ ^[0-9]+$ &&
     "$DEFAULT_SUITE_EVIDENCE_TTL" -gt 0 ]] ||
    die "kit-suite evidence TTL must be a positive integer"
}

remove_symlinked_suite_evidence() {
  local evidence="$1"
  if [[ -L "$evidence" ]]; then
    rm "$evidence" || die "could not remove unsafe kit-suite evidence symlink"
  fi
}

write_suite_evidence() {
  local sha="$1" origin="$2" tree="$3" release="$4"
  local verification_source="${5:-local-full}" remote_evidence_id="${6:-}"
  local evidence created expires
  evidence="$(suite_evidence_file_for "$sha")"
  created="$(now_epoch)"
  expires=$((created + DEFAULT_SUITE_EVIDENCE_TTL))
  python3 - "$sha" "$origin" "$tree" "$release" "$tree" \
    "$(host_name)" "$(uname -s)" "$(uname -m)" "$KIT_SUITE_DEFINITION" \
    "$CERTIFICATION_TOOL_VERSION" "$created" "$expires" \
    "$DEFAULT_SUITE_EVIDENCE_TTL" "$SUITE_EVIDENCE_SCHEMA" \
    "$verification_source" "$remote_evidence_id" \
    <<'PY' | atomic_json_from_stdin "$evidence"
import hashlib, json, sys, time
(sha, origin, tree, release, release_tree, host, os_name, architecture,
 suite_definition, tool_version, created, expires, ttl, schema,
 verification_source, remote_evidence_id) = sys.argv[1:]
value = {
    "schema_version": int(schema),
    "status": "pass",
    "kit_sha": sha,
    "kit_tree": tree,
    "canonical_origin": origin,
    "sealed_release_path": release,
    "release_tree": release_tree,
    "host": host,
    "os": os_name,
    "architecture": architecture,
    "suite_definition": suite_definition,
    "certification_tool_version": int(tool_version),
    "created_epoch": int(created),
    "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(created))),
    "expires_epoch": int(expires),
    "expires_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(expires))),
    "evidence_ttl_seconds": int(ttl),
    "verification_source": verification_source,
    "remote_evidence_id": remote_evidence_id or None,
}
payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
value["evidence_id"] = hashlib.sha256(payload).hexdigest()
print(json.dumps(value))
PY
  chmod 600 "$evidence"
}

validated_suite_evidence() {
  local sha="$1" origin="$2" tree="$3" release="$4" evidence
  evidence="$(suite_evidence_file_for "$sha")"
  python3 - "$evidence" "$sha" "$origin" "$tree" "$release" "$tree" \
    "$(host_name)" "$(uname -s)" "$(uname -m)" "$KIT_SUITE_DEFINITION" \
    "$CERTIFICATION_TOOL_VERSION" "$DEFAULT_SUITE_EVIDENCE_TTL" \
    "$SUITE_EVIDENCE_SCHEMA" "$(now_epoch)" <<'PY'
import hashlib, json, os, pathlib, stat, sys
(raw_path, sha, origin, tree, release, release_tree, host, os_name,
 architecture, suite_definition, tool_version, ttl, schema, now) = sys.argv[1:]
path = pathlib.Path(raw_path)
try:
    st = path.lstat()
    if (stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode) or
            st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) & 0o077 or
            st.st_nlink != 1):
        raise ValueError
    raw = path.read_bytes()
    value = json.loads(raw)
    expected = {
        "schema_version": int(schema),
        "status": "pass",
        "kit_sha": sha,
        "kit_tree": tree,
        "canonical_origin": origin,
        "sealed_release_path": release,
        "release_tree": release_tree,
        "host": host,
        "os": os_name,
        "architecture": architecture,
        "suite_definition": suite_definition,
        "certification_tool_version": int(tool_version),
        "evidence_ttl_seconds": int(ttl),
    }
    if any(value.get(key) != expected_value for key, expected_value in expected.items()):
        raise ValueError
    source = value.get("verification_source")
    remote_id = value.get("remote_evidence_id")
    if source == "local-full":
        if remote_id is not None:
            raise ValueError
    elif source == "github-actions-full":
        if not isinstance(remote_id, str) or len(remote_id) != 64 or any(c not in "0123456789abcdef" for c in remote_id):
            raise ValueError
    else:
        raise ValueError
    created = value.get("created_epoch")
    expires = value.get("expires_epoch")
    if (not isinstance(created, int) or isinstance(created, bool) or
            not isinstance(expires, int) or isinstance(expires, bool) or
            created > int(now) or expires != created + int(ttl) or expires <= int(now)):
        raise ValueError
    evidence_id = value.get("evidence_id")
    payload = dict(value)
    payload.pop("evidence_id", None)
    calculated_id = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if evidence_id != calculated_id:
        raise ValueError
    digest = hashlib.sha256(raw).hexdigest()
except (OSError, ValueError, TypeError, json.JSONDecodeError):
    raise SystemExit(1)
print("%s\t%s\t%s\t%s\t%s" % (evidence_id, digest, created, expires, source))
PY
}

record_certification_trace() {
  local event="$1" trace="${FACTORY_KIT_TEST_CERTIFICATION_TRACE:-}"
  [[ -n "$trace" ]] || return 0
  [[ "${FACTORY_KIT_TEST_MODE:-0}" == "1" ]] ||
    die "certification tracing requires FACTORY_KIT_TEST_MODE"
  printf '%s\n' "$event" >> "$trace"
}

write_install_manifest() {
  local sha="$1" origin="$2" tree="$3" release="$4" manifest
  manifest="$(manifest_file_for "$sha")"
  [[ ! -e "$manifest" && ! -L "$manifest" ]] ||
    die "install manifest already exists: $manifest"
  python3 - "$sha" "$origin" "$tree" "$release" "$(now_iso)" <<'PY' | atomic_json_from_stdin "$manifest"
import json, sys
sha, origin, tree, release, created = sys.argv[1:]
print(json.dumps({
    "schema_version": 1,
    "kit_sha": sha,
    "canonical_origin": origin,
    "git_tree": tree,
    "sealed_release_path": release,
    "created_at": created,
}))
PY
  chmod 600 "$manifest"
}

verify_release_from_manifest() {
  local sha="$1" manifest release tree origin
  validate_sha "$sha"
  validate_managed_layout
  manifest="$(manifest_file_for "$sha")"
  release="$RELEASES_DIR/$sha"
  [[ -f "$manifest" && ! -L "$manifest" ]] ||
    die "trusted install manifest is missing or unsafe for $sha"
  verify_restrictive_regular_file "$manifest" ||
    die "trusted install manifest ownership or permissions are unsafe"
  [[ "$(basename "$manifest")" == "$sha.json" ]] ||
    die "install manifest filename does not match SHA"
  [[ "$(json_get "$manifest" schema_version)" == "$INSTALL_MANIFEST_SCHEMA" ]] ||
    die "unsupported install manifest schema"
  [[ "$(json_get "$manifest" kit_sha)" == "$sha" ]] ||
    die "install manifest SHA mismatch"
  origin="$(json_get "$manifest" canonical_origin)"
  [[ "$origin" == "$(expected_origin_identity)" ]] ||
    die "install manifest origin mismatch"
  tree="$(json_get "$manifest" git_tree)"
  [[ "$tree" =~ ^[0-9a-f]{40}$ ]] ||
    die "install manifest Git tree is invalid"
  [[ "$(json_get "$manifest" sealed_release_path)" == "$release" ]] ||
    die "install manifest release path mismatch"
  verify_release "$sha" "$tree"
  printf '%s\t%s\t%s\n' "$tree" "$origin" "$release"
}

contract_version() {
  local release="$1" value=""
  if [[ -f "$release/integrations/hermes/contract.json" ]]; then
    value="$(python3 - "$release/integrations/hermes/contract.json" <<'PY'
import json, sys
try:
    value = json.load(open(sys.argv[1]))
except Exception:
    raise SystemExit(1)
for key in ("contract_version", "version", "schema_version"):
    if key in value:
        print(value[key])
        break
PY
)" || die "invalid Hermes contract manifest"
  elif [[ -f "$release/integrations/hermes/CONTRACT_VERSION" ]]; then
    value="$(awk 'NF {print; exit}' "$release/integrations/hermes/CONTRACT_VERSION")"
  elif [[ -f "$release/integrations/hermes/contract-version" ]]; then
    value="$(awk 'NF {print; exit}' "$release/integrations/hermes/contract-version")"
  else
    value="1"
  fi
  [[ "$value" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] ||
    die "invalid Hermes contract version"
  printf '%s\n' "$value"
}

prepare_writable_release_copy() {
  local release="$1" workspace="$2"
  PREPARED_COPY="$workspace/release"
  mkdir "$PREPARED_COPY"
  (cd "$release" && tar -cf - .) | (cd "$PREPARED_COPY" && tar -xf -)
  chmod -R u+w "$PREPARED_COPY"
  git -C "$PREPARED_COPY" init -q
  git -C "$PREPARED_COPY" add -A
  GIT_AUTHOR_NAME=factory-kit GIT_AUTHOR_EMAIL=factory-kit@invalid \
    GIT_COMMITTER_NAME=factory-kit GIT_COMMITTER_EMAIL=factory-kit@invalid \
    git -C "$PREPARED_COPY" commit -qm "certification fixture"
  git -C "$PREPARED_COPY" update-ref refs/remotes/origin/main HEAD
}

prepare_writable_product_copy() {
  local product="$1" workspace="$2"
  PREPARED_PRODUCT="$workspace/product"
  mkdir "$PREPARED_PRODUCT"
  git -C "$product" archive --format=tar HEAD | (cd "$PREPARED_PRODUCT" && tar -xf -)
  git -C "$PREPARED_PRODUCT" init -q
  git -C "$PREPARED_PRODUCT" add -A
  GIT_AUTHOR_NAME=factory-kit GIT_AUTHOR_EMAIL=factory-kit@invalid \
    GIT_COMMITTER_NAME=factory-kit GIT_COMMITTER_EMAIL=factory-kit@invalid \
    git -C "$PREPARED_PRODUCT" commit -qm "product certification fixture"
}

prepare_pinned_scanner() {
  local source="$1" checkout="$2" scratch="$3"
  local scanner="$checkout/scripts/secret-scan"
  [[ -f "$scanner" ]] || return 0
  if [[ -n "${FACTORY_KIT_TEST_PINNED_SCANNER:-}" ]]; then
    [[ "${FACTORY_KIT_TEST_MODE:-0}" == "1" ]] ||
      die "pinned scanner test override requires test mode"
    [[ -f "$FACTORY_KIT_TEST_PINNED_SCANNER" &&
       ! -L "$FACTORY_KIT_TEST_PINNED_SCANNER" &&
       -x "$FACTORY_KIT_TEST_PINNED_SCANNER" ]] ||
      die "pinned scanner test fixture is unsafe"
    mkdir -p "$checkout/.context/tools/gitleaks/8.30.1"
    cp "$FACTORY_KIT_TEST_PINNED_SCANNER" \
      "$checkout/.context/tools/gitleaks/8.30.1/gitleaks"
    return 0
  fi
  grep -Fq 'VERSION = "8.30.1"' "$scanner" || return 0
  python3 - "$source" "$checkout" "$scratch" <<'PY'
import hashlib
import io
import os
import pathlib
import platform
import tarfile
import urllib.request
import sys

source, checkout, scratch = map(pathlib.Path, sys.argv[1:])
source = source.resolve()
checkout = checkout.resolve()
scratch = scratch.resolve()
version = "8.30.1"
artifacts = {
    ("Darwin", "arm64"): ("darwin_arm64", "b40ab0ae55c505963e365f271a8d3846efbc170aa17f2607f13df610a9aeb6a5"),
    ("Darwin", "x86_64"): ("darwin_x64", "dfe101a4db2255fc85120ac7f3d25e4342c3c20cf749f2c20a18081af1952709"),
    ("Linux", "aarch64"): ("linux_arm64", "e4a487ee7ccd7d3a7f7ec08657610aa3606637dab924210b3aee62570fb4b080"),
    ("Linux", "x86_64"): ("linux_x64", "551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb"),
}
key = (platform.system(), platform.machine())
if key not in artifacts:
    raise SystemExit("unsupported platform for pinned scanner")
suffix, expected = artifacts[key]
source_archive = source / ".context" / "tools" / "gitleaks" / version / "gitleaks.tar.gz"
archive_bytes = b""
if source_archive.is_file():
    candidate = source_archive.read_bytes()
    if hashlib.sha256(candidate).hexdigest() == expected:
        archive_bytes = candidate
if not archive_bytes:
    url = (
        "https://github.com/gitleaks/gitleaks/releases/download/"
        f"v{version}/gitleaks_{version}_{suffix}.tar.gz"
    )
    download = pathlib.Path(scratch) / "gitleaks-download.tar.gz"
    try:
        with urllib.request.urlopen(url, timeout=60) as response:
            archive_bytes = response.read(100 * 1024 * 1024 + 1)
    except OSError as exc:
        raise SystemExit(f"could not download pinned scanner: {exc}")
    if len(archive_bytes) > 100 * 1024 * 1024:
        raise SystemExit("pinned scanner download exceeded size limit")
    if hashlib.sha256(archive_bytes).hexdigest() != expected:
        raise SystemExit("pinned scanner download checksum mismatch")
    download.write_bytes(archive_bytes)
with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as bundle:
    members = [
        member for member in bundle.getmembers()
        if pathlib.PurePosixPath(member.name).name == "gitleaks" and member.isfile()
    ]
    if len(members) != 1:
        raise SystemExit("pinned scanner archive has an unexpected layout")
    extracted = bundle.extractfile(members[0])
    if extracted is None:
        raise SystemExit("could not extract pinned scanner")
    binary = extracted.read()
destination = checkout
for component in (".context", "tools", "gitleaks", version):
    candidate = destination / component
    if os.path.lexists(candidate):
        if candidate.is_symlink() or not candidate.is_dir():
            raise SystemExit("pinned scanner cache path is unsafe")
    else:
        candidate.mkdir()
    destination = candidate
if checkout not in destination.resolve().parents:
    raise SystemExit("pinned scanner cache escaped disposable checkout")
archive = destination / "gitleaks.tar.gz"
target = destination / "gitleaks"
for path in (archive, target):
    if os.path.lexists(path) and (path.is_symlink() or not path.is_file()):
        raise SystemExit("pinned scanner cache file is unsafe")
archive.write_bytes(archive_bytes)
target.write_bytes(binary)
target.chmod(0o755)
PY
}

run_kit_checks_isolated() {
  local checkout="$1" home="$2" scratch="$3" workspace="$4" phase="$5"
  local check_mode="${6:-full}"
  shift 6
  local status=0
  local raw="$scratch/kit-checks.raw" redacted="$scratch/kit-checks.redacted"
  if [[ "${FACTORY_KIT_TEST_SUITE_FAIL:-0}" != "0" ||
        "${FACTORY_KIT_TEST_SUITE_SLEEP_SECONDS:-0}" != "0" ]]; then
    [[ "${FACTORY_KIT_TEST_MODE:-0}" == "1" ]] ||
      die "kit-suite test controls require FACTORY_KIT_TEST_MODE"
  fi
  configure_phase_sandbox "$phase" "$workspace" "$@"
  python3 - "$checkout" "$home" "$scratch" "$raw" \
    "$SANDBOX_EXEC" "$SANDBOX_PROFILE" "$SCRIPT_ROOT/scripts/lib/sandbox-ps.py" \
    "${FACTORY_FIXTURE_DIRTY:-0}" \
    "${FACTORY_KIT_SANDBOX_CAPTURE:-}" \
    "${FACTORY_KIT_SANDBOX_DENY_SIBLING:-}" \
    "${FACTORY_KIT_SANDBOX_DENY_HOME:-}" \
    "${FACTORY_KIT_TEST_SUITE_FAIL:-0}" \
    "${FACTORY_KIT_TEST_SUITE_SLEEP_SECONDS:-0}" "$check_mode" <<'PY' || status=$?
import os, pathlib, subprocess, sys
(
    checkout, home, scratch, output, sandbox_exec, profile, sandbox_ps, dirty,
    capture, deny_sibling, deny_home, test_fail, test_sleep, check_mode,
) = sys.argv[1:]
root = pathlib.Path(checkout)
prefix = [sandbox_exec, "-f", profile] if profile else []
path_value = os.environ.get("PATH", "/usr/bin:/bin")
tool_environment = {}
if os.path.isfile("/usr/bin/xcode-select"):
    selected = subprocess.run(
        ["/usr/bin/xcode-select", "-p"], text=True, capture_output=True
    )
    if selected.returncode == 0:
        developer_root = selected.stdout.strip()
        developer_bin = os.path.join(developer_root, "usr", "bin")
        developer_git = os.path.join(developer_bin, "git")
        if os.path.isfile(developer_git):
            override_bin = os.path.join(scratch, "factory-tools")
            os.makedirs(override_bin, exist_ok=True)
            for name in ("git", "git-receive-pack", "git-upload-pack", "python3"):
                target = os.path.join(developer_bin, name)
                link = os.path.join(override_bin, name)
                if not os.path.isfile(target):
                    raise SystemExit("selected developer tool is missing")
                if os.path.lexists(link):
                    if not os.path.islink(link) or os.readlink(link) != target:
                        raise SystemExit("sandbox Git override path is unsafe")
                else:
                    os.symlink(target, link)
            ps_wrapper = pathlib.Path(sandbox_ps).read_text()
            ps_path = os.path.join(override_bin, "ps")
            if os.path.exists(ps_path):
                if os.path.islink(ps_path) or pathlib.Path(ps_path).read_text() != ps_wrapper:
                    raise SystemExit("sandbox ps override path is unsafe")
            else:
                pathlib.Path(ps_path).write_text(ps_wrapper)
            os.chmod(ps_path, 0o755)
            path_value = override_bin + os.pathsep + path_value
            tool_environment = {
                "DEVELOPER_DIR": developer_root,
                "GIT_EXEC_PATH": os.path.join(developer_root, "usr", "libexec", "git-core"),
                "GIT_TEMPLATE_DIR": os.path.join(developer_root, "usr", "share", "git-core", "templates"),
            }
environment = {
    "PATH": path_value,
    "HOME": home,
    "TMPDIR": scratch,
    "XDG_CACHE_HOME": os.path.join(scratch, "cache"),
    "npm_config_cache": os.path.join(scratch, "npm"),
    "FACTORY_KIT_OUTER_SANDBOX": "1",
}
environment.update(tool_environment)
if dirty == "1":
    environment["FACTORY_FIXTURE_DIRTY"] = "1"
if capture:
    environment["FACTORY_KIT_SANDBOX_CAPTURE"] = capture
if deny_sibling:
    environment["FACTORY_KIT_SANDBOX_DENY_SIBLING"] = deny_sibling
if deny_home:
    environment["FACTORY_KIT_SANDBOX_DENY_HOME"] = deny_home
if test_fail != "0":
    environment["FACTORY_KIT_TEST_SUITE_FAIL"] = test_fail
if test_sleep != "0":
    environment["FACTORY_KIT_TEST_SUITE_SLEEP_SECONDS"] = test_sleep
commands = []
if check_mode == "full" and (root / "ci/test-all.sh").is_file():
    commands.append(["bash", "ci/test-all.sh"])
elif check_mode == "platform-smoke":
    syntax_paths = [path for path in ("ci/test-all.sh", "scripts/factory-kit.sh") if (root / path).is_file()]
    if syntax_paths:
        commands.append(["bash", "-n"] + syntax_paths)
else:
    raise SystemExit("invalid kit check mode")
if (root / "scripts/repo-check").is_file():
    commands.append(["scripts/repo-check", "--root", checkout])
if (root / "scripts/secret-scan").is_file():
    commands.append(["scripts/secret-scan"])
with open(output, "wb") as stream:
    for command in commands:
        result = subprocess.run(
            prefix + command,
            cwd=checkout,
            env=environment,
            stdout=stream,
            stderr=subprocess.STDOUT,
        )
        if result.returncode:
            raise SystemExit(result.returncode)
PY
  redact_output "$raw" "$redacted"
  rm -f "$raw"
  if [[ "$status" -ne 0 ]]; then
    awk '{print "  | " $0}' "$redacted" >&2
    return "$status"
  fi
}

strict_product_pin() {
  local product="$1" pin_file
  pin_file="$product/factory/KIT_PIN"
  [[ -f "$pin_file" && ! -L "$pin_file" ]] ||
    die "product is missing factory/KIT_PIN"
  python3 - "$pin_file" <<'PY' ||
import pathlib, re, sys
value = pathlib.Path(sys.argv[1]).read_bytes()
if not re.fullmatch(rb"[0-9a-f]{40}\n?", value):
    raise SystemExit(1)
sys.stdout.write(value.rstrip(b"\n").decode("ascii") + "\n")
PY
    die "factory/KIT_PIN must contain exactly one physical lowercase full-SHA line"
}

product_tree() {
  git -C "$1" rev-parse 'HEAD^{tree}' 2>/dev/null ||
    die "product is not a Git repository"
}

product_origin() {
  local origin count scheme authority userinfo normalized_userinfo
  origin="$(git -C "$1" remote get-url --push --all origin 2>/dev/null || true)"
  count="$(printf '%s\n' "$origin" | awk 'NF {count++} END {print count+0}')"
  [[ "$count" == "1" ]] || die "product repository must have one push destination"
  [[ "$origin" != *$'\n'* && "$origin" != *$'\r'* && "$origin" != *$'\t'* ]] ||
    die "product push destination is unsafe"
  case "$origin" in
    /*) ;;
    *://*)
      [[ "$origin" =~ ^[A-Za-z][A-Za-z0-9+.-]*:// ]] ||
        die "product push destination is unsafe"
      scheme="$(printf '%s' "${origin%%://*}" | tr '[:upper:]' '[:lower:]')"
      authority="${origin#*://}"
      authority="${authority%%/*}"
      authority="${authority%%\?*}"
      authority="${authority%%\#*}"
      if [[ "$scheme" == "http" || "$scheme" == "https" ]]; then
        [[ "$authority" != *@* ]] ||
          die "product HTTP push destination must not contain credentials"
      elif [[ "$authority" == *@* ]]; then
        userinfo="${authority%@*}"
        normalized_userinfo="$(printf '%s' "$userinfo" | tr '[:upper:]' '[:lower:]')"
        [[ "$userinfo" != *:* && "$normalized_userinfo" != *%3a* ]] ||
          die "product push destination must not contain password credentials"
      fi
      ;;
    *:*)
      [[ "$origin" =~ ^([A-Za-z0-9][A-Za-z0-9._-]*@)?[A-Za-z0-9][A-Za-z0-9._-]*:[A-Za-z0-9._/~+-]+$ ]] ||
        die "product push destination must be absolute, URL, or scp-like"
      ;;
    *) die "product push destination must be absolute, URL, or scp-like" ;;
  esac
  printf '%s\n' "$origin"
}

require_clean_product() {
  [[ -z "$(git -C "$1" status --porcelain --untracked-files=all)" ]] ||
    die "product working tree is dirty"
}

certify_script_path() {
  python3 - "$1/factory/PROJECT.env" "$1" <<'PY'
import os
import pathlib
import shlex
import sys

env_file = pathlib.Path(sys.argv[1])
root = pathlib.Path(sys.argv[2]).resolve()
if not env_file.is_file():
    raise SystemExit("factory/PROJECT.env is required")
value = None
for raw in env_file.read_text().splitlines():
    line = raw.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    key, candidate = line.split("=", 1)
    if key.strip() == "CERTIFY_SCRIPT":
        words = shlex.split(candidate, comments=False, posix=True)
        if len(words) != 1:
            raise SystemExit("CERTIFY_SCRIPT must be one repository-contained path")
        value = words[0]
        break
if not value:
    raise SystemExit("PROJECT.env must define CERTIFY_SCRIPT")
path = pathlib.Path(value)
if path.is_absolute() or ".." in path.parts or "." in path.parts:
    raise SystemExit("CERTIFY_SCRIPT must be a contained relative path")
resolved = (root / path).resolve()
try:
    resolved.relative_to(root)
except ValueError:
    raise SystemExit("CERTIFY_SCRIPT escapes the product repository")
if not resolved.is_file() or not os.access(str(resolved), os.X_OK):
    raise SystemExit("CERTIFY_SCRIPT is not executable")
print(str(resolved))
PY
}

redact_output() {
  python3 - "$1" "$2" <<'PY'
import pathlib, re, sys
source, destination = map(pathlib.Path, sys.argv[1:])
text = source.read_text(errors="replace")
text = re.sub(
    r"([A-Za-z][A-Za-z0-9+.-]*://)([^/\s:@]+):([^@\s/]+)@",
    r"\1[REDACTED]@",
    text,
)
text = re.sub(
    r"(?im)(\b(?:proxy-)?authorization\s*[:=]\s*)[^\r\n]*",
    r"\1[REDACTED]",
    text,
)
key = r"[A-Za-z0-9_.-]*(?:key|token|secret|password|url|dsn|conn|auth)[A-Za-z0-9_.-]*"
text = re.sub(
    r"(?i)([?&](?:" + key + r")=)([^&#\s\"']*)",
    lambda match: match.group(1) + "[REDACTED]",
    text,
)
quoted = re.compile(
    r"(?is)(?P<prefix>[\"']" + key + r"[\"']\s*:\s*)"
    r"(?P<quote>[\"'])(?:\\.|(?!(?P=quote)).)*(?P=quote)"
)
text = quoted.sub(
    lambda match: match.group("prefix") + match.group("quote") +
    "[REDACTED]" + match.group("quote"),
    text,
)
key_line = re.compile(
    r"(?i)^(?P<prefix>.*?[\"']?" + key +
    r"[\"']?\s*[:=]\s*)(?P<value>.*)$"
)
redacted = []
continuation_indent = None
for line in text.splitlines(keepends=True):
    content = line.rstrip("\r\n")
    ending = line[len(content):]
    indent = len(content) - len(content.lstrip(" \t"))
    if continuation_indent is not None:
        if not content.strip() or indent > continuation_indent:
            redacted.append(content[:indent] + "[REDACTED]" + ending)
            continue
        continuation_indent = None
    match = key_line.match(content)
    if match:
        redacted.append(match.group("prefix") + "[REDACTED]" + ending)
        # Structured logs commonly continue scalar values on indented lines.
        # Keep redacting until a nonblank line returns to this key's indent.
        continuation_indent = indent
        continue
    redacted.append(line)
text = "".join(redacted)
destination.write_text(text)
PY
}

write_sandbox_profile() {
  local profile="$1" workspace="$2" allow_network="$3"
  shift 3
  python3 - "$profile" "$workspace" "$PATH" "$allow_network" "$@" <<'PY'
import json, os, pathlib, sys
profile, workspace, path_value, allow_network, *extra_denied = sys.argv[1:]
quote = json.dumps
workspace = str(pathlib.Path(workspace).resolve())
system_roots = [
    "/System",
    "/bin",
    "/sbin",
    "/usr/bin",
    "/usr/lib",
    "/usr/libexec",
    "/usr/share",
    "/etc",
    "/private/etc",
    "/private/var/db/timezone",
    "/Library/Apple",
    "/Library/Developer",
    "/Applications/Xcode.app/Contents/Developer",
    # Apple command shims such as /usr/bin/git read this public developer
    # selection path using the /var spelling even though /var aliases
    # /private/var. Seatbelt matches the requested spelling, so permit both.
    "/var/select",
    "/private/var/select",
]
toolchain_roots = []
for item in path_value.split(os.pathsep):
    if not item or not os.path.isabs(item) or not os.path.isdir(item):
        continue
    resolved = str(pathlib.Path(item).resolve())
    if resolved not in toolchain_roots:
        toolchain_roots.append(resolved)
for item in ("/opt/homebrew", "/usr/local"):
    if os.path.isdir(item) and item not in toolchain_roots:
        toolchain_roots.append(item)
read_roots = []
for item in system_roots + toolchain_roots + [workspace]:
    if item not in read_roots:
        read_roots.append(item)
metadata_paths = {"/"}
for item in read_roots:
    current = pathlib.Path(item)
    metadata_paths.add(str(current))
    metadata_paths.update(str(parent) for parent in current.parents)
lines = ["""(version 1)
(deny default)
(allow process-fork)
(allow process-info* (target same-sandbox))
(allow sysctl-read)
(allow mach-lookup)
"""]
# Parent traversal permits metadata only; file contents remain default-denied.
for path in sorted(metadata_paths):
    lines.append("(allow file-read-metadata (literal %s))\n" % quote(path))
    lines.append("(allow file-read-data (literal %s))\n" % quote(path))
for path in read_roots:
    lines.append("(allow file-read* (subpath %s))\n" % quote(path))
    lines.append("(allow process-exec (subpath %s))\n" % quote(path))
lines.append("(allow file-write* (subpath %s))\n" % quote(workspace))
for path in ("/dev/null", "/dev/random", "/dev/urandom"):
    lines.append("(allow file-read* (literal %s))\n" % quote(path))
lines.append('(allow file-write* (literal "/dev/null"))\n')
lines.append('(allow file-read-metadata (literal "/dev"))\n')
lines.append('(allow file-read* (subpath "/dev/fd"))\n')
lines.append('(allow file-write* (subpath "/dev/fd"))\n')
# Test runners must terminate the child servers they create. Every descendant
# inherits this profile, so same-sandbox signaling cannot reach live services.
lines.append("(allow signal (target same-sandbox))\n")
# Product and kit suites may bind ephemeral local servers, but they do not
# need DNS or external connectivity. Keep loopback separate from the reviewed
# certification-only network opt-in below.
lines.append('(allow network-bind (local ip "localhost:*"))\n')
lines.append('(allow network-inbound (local ip "localhost:*"))\n')
lines.append('(allow network-outbound (remote ip "localhost:*"))\n')
for path in extra_denied:
    if path:
        lines.append("(deny file-read* (subpath %s))\n" % quote(str(pathlib.Path(path).resolve())))
        lines.append("(deny file-write* (subpath %s))\n" % quote(str(pathlib.Path(path).resolve())))
if allow_network == "1":
    lines.append("(allow network-outbound)\n")
pathlib.Path(profile).write_text("".join(lines))
PY
}

configure_phase_sandbox() {
  local phase="$1" workspace="$2" network_opt_in
  shift 2
  SANDBOX_EXEC=""
  SANDBOX_PROFILE=""
  network_opt_in="${FACTORY_KIT_CERTIFICATION_NETWORK_REVIEWED:-0}"
  [[ "$network_opt_in" == "0" || "$network_opt_in" == "1" ]] ||
    die "certification network review opt-in must be 0 or 1"
  if [[ "$network_opt_in" == "1" && "$phase" != "certification" ]]; then
    die "reviewed network opt-in is allowed only for certification"
  fi
  if [[ "${FACTORY_KIT_TEST_FORCE_PRODUCTION_SANDBOX:-0}" == "1" ]]; then
    [[ "${FACTORY_KIT_TEST_MODE:-0}" == "1" ]] ||
      die "sandbox test overrides require FACTORY_KIT_TEST_MODE"
    SANDBOX_EXEC="${FACTORY_KIT_SANDBOX_EXEC:-/usr/bin/sandbox-exec}"
  elif [[ "${FACTORY_KIT_TEST_MODE:-0}" == "1" ]]; then
    return
  else
    [[ "$(uname -s)" == "Darwin" ]] ||
      die "production $phase requires a supported platform sandbox"
    SANDBOX_EXEC="/usr/bin/sandbox-exec"
  fi
  [[ -x "$SANDBOX_EXEC" ]] ||
    die "production macOS $phase requires sandbox-exec"
  SANDBOX_PROFILE="$workspace/$phase.sb"
  write_sandbox_profile "$SANDBOX_PROFILE" "$workspace" "$network_opt_in" "$@"
}

run_product_certification() {
  local product_copy="$1" script="$2" sha="$3" release_copy="$4"
  local workspace="$5" real_product="$6" real_release="$7"
  local raw="$workspace/certification.raw" redacted="$workspace/certification.redacted"
  local timeout status=0
  timeout="${FACTORY_KIT_CERTIFY_TIMEOUT_SECONDS:-900}"
  [[ "$timeout" =~ ^[0-9]+$ && "$timeout" -gt 0 ]] ||
    die "certification timeout must be positive"
  configure_phase_sandbox "certification" "$workspace" "$real_product" "$real_release"
  python3 - "$product_copy" "$script" "$sha" "$release_copy" "$workspace/home" \
    "$workspace/tmp" "$timeout" "$raw" "$SANDBOX_PROFILE" "$SANDBOX_EXEC" \
    "$SCRIPT_ROOT/scripts/lib/sandbox-ps.py" \
    "${FACTORY_KIT_SANDBOX_CAPTURE:-}" \
    "${FACTORY_KIT_SANDBOX_DENY_SIBLING:-}" \
    "${FACTORY_KIT_SANDBOX_DENY_HOME:-}" <<'PY' || status=$?
import os, pathlib, subprocess, sys
product, script, sha, release, home, scratch, timeout, output = sys.argv[1:9]
profile = sys.argv[9]
sandbox_exec = sys.argv[10]
sandbox_ps = sys.argv[11]
capture = sys.argv[12]
deny_sibling = sys.argv[13]
deny_home = sys.argv[14]
prefix = [sandbox_exec, "-f", profile] if profile else []
path_value = os.environ.get("PATH", "/usr/bin:/bin")
tool_environment = {}
if os.path.isfile("/usr/bin/xcode-select"):
    selected = subprocess.run(
        ["/usr/bin/xcode-select", "-p"], text=True, capture_output=True
    )
    if selected.returncode == 0:
        developer_root = selected.stdout.strip()
        developer_bin = os.path.join(developer_root, "usr", "bin")
        developer_git = os.path.join(developer_bin, "git")
        if os.path.isfile(developer_git):
            override_bin = os.path.join(scratch, "factory-tools")
            os.makedirs(override_bin, exist_ok=True)
            for name in ("git", "git-receive-pack", "git-upload-pack", "python3"):
                target = os.path.join(developer_bin, name)
                link = os.path.join(override_bin, name)
                if not os.path.isfile(target):
                    raise SystemExit("selected developer tool is missing")
                if os.path.lexists(link):
                    if not os.path.islink(link) or os.readlink(link) != target:
                        raise SystemExit("sandbox Git override path is unsafe")
                else:
                    os.symlink(target, link)
            ps_wrapper = pathlib.Path(sandbox_ps).read_text()
            ps_path = os.path.join(override_bin, "ps")
            if os.path.exists(ps_path):
                if os.path.islink(ps_path) or pathlib.Path(ps_path).read_text() != ps_wrapper:
                    raise SystemExit("sandbox ps override path is unsafe")
            else:
                pathlib.Path(ps_path).write_text(ps_wrapper)
            os.chmod(ps_path, 0o755)
            path_value = override_bin + os.pathsep + path_value
            tool_environment = {
                "DEVELOPER_DIR": developer_root,
                "GIT_EXEC_PATH": os.path.join(developer_root, "usr", "libexec", "git-core"),
                "GIT_TEMPLATE_DIR": os.path.join(developer_root, "usr", "share", "git-core", "templates"),
            }
environment = {
    "PATH": path_value,
    "HOME": home,
    "TMPDIR": scratch,
    "XDG_CACHE_HOME": os.path.join(scratch, "cache"),
    "npm_config_cache": os.path.join(scratch, "npm"),
    "FACTORY_KIT_SHA": sha,
    "FACTORY_KIT_RELEASE": release,
    "FACTORY_PRODUCT_ROOT": product,
    "FACTORY_KIT_OUTER_SANDBOX": "1",
}
environment.update(tool_environment)
if capture:
    environment["FACTORY_KIT_SANDBOX_CAPTURE"] = capture
if deny_sibling:
    environment["FACTORY_KIT_SANDBOX_DENY_SIBLING"] = deny_sibling
if deny_home:
    environment["FACTORY_KIT_SANDBOX_DENY_HOME"] = deny_home
with open(output, "wb") as stream:
    try:
        result = subprocess.run(
            prefix + [script],
            cwd=product,
            env=environment,
            stdout=stream,
            stderr=subprocess.STDOUT,
            timeout=int(timeout),
        )
    except subprocess.TimeoutExpired:
        raise SystemExit(124)
raise SystemExit(result.returncode)
PY
  redact_output "$raw" "$redacted"
  rm -f "$raw"
  if [[ "$status" -ne 0 ]]; then
    awk '{print "  | " $0}' "$redacted" >&2
    return "$status"
  fi
}

json_get() {
  python3 - "$1" "$2" <<'PY'
import json, sys
value = json.load(open(sys.argv[1]))
for part in sys.argv[2].split("."):
    if part:
        value = value[part]
if value is None:
    print("")
elif isinstance(value, bool):
    print("true" if value else "false")
elif isinstance(value, (dict, list)):
    print(json.dumps(value, sort_keys=True, separators=(",", ":")))
else:
    print(value)
PY
}

atomic_json_from_stdin() {
  local destination="$1"
  python3 -c '
import json, os, pathlib, sys, tempfile
destination = pathlib.Path(sys.argv[1])
value = json.load(sys.stdin)
if destination.parent.is_symlink() or not destination.parent.is_dir():
    raise SystemExit("atomic JSON destination parent is unsafe")
fd, temporary = tempfile.mkstemp(prefix=".%s." % destination.name, dir=str(destination.parent))
try:
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream, sort_keys=True, separators=(",", ":"))
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, destination)
    directory_fd = os.open(str(destination.parent), os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
finally:
    if os.path.exists(temporary):
        os.unlink(temporary)
' "$destination"
}

find_receipt() {
  local slug="$1" sha="$2"
  [[ -d "$RECEIPTS_DIR" ]] || return 1
  python3 - "$RECEIPTS_DIR" "$CONSUMED_DIR" "$slug" "$sha" <<'PY'
import json
import pathlib
import sys

root, consumed, slug, sha = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]), sys.argv[3], sys.argv[4]
candidates = []
for path in root.glob("*.json"):
    try:
        value = json.loads(path.read_text())
        receipt_id = value.get("receipt_id")
        if (value.get("project") == slug and value.get("kit_sha") == sha and
                receipt_id and not (consumed / (receipt_id + ".json")).exists()):
            candidates.append((int(value.get("created_epoch", 0)), str(path)))
    except Exception:
        continue
if not candidates:
    raise SystemExit(1)
print(max(candidates)[1])
PY
}

validate_receipt_snapshot() {
  local receipt="$1" slug="$2" product="$3" sha="$4" expected_previous="$5"
  local expected_id="${6:-}" release="$RELEASES_DIR/$sha"
  local expected_tree manifest_values manifest_origin pin contract receipt_id
  local product_top product_git_tree kit_pin_hash project_env_hash
  local evidence_created evidence_expires
  [[ -f "$receipt" ]] || die "certification receipt not found: $receipt"
  [[ ! -L "$receipt" ]] || die "certification receipt may not be a symlink"
  [[ "$(json_get "$receipt" schema_version)" == "$RECEIPT_SCHEMA" ]] ||
    die "unsupported certification receipt schema"
  [[ "$(json_get "$receipt" certification_tool_version)" == "$CERTIFICATION_TOOL_VERSION" ]] ||
    die "unsupported certification tool version"
  receipt_id="$(json_get "$receipt" receipt_id)"
  [[ "$receipt_id" =~ ^[0-9a-f]{64}$ ]] || die "receipt ID is invalid"
  [[ -z "$expected_id" || "$receipt_id" == "$expected_id" ]] ||
    die "receipt snapshot ID mismatch"
  if [[ "$(dirname "$receipt")" == "$RECEIPTS_DIR" ]]; then
    [[ "$(basename "$receipt")" == "$receipt_id.json" ]] ||
      die "receipt filename does not match receipt ID"
  fi
  [[ "$(json_get "$receipt" status)" == "pass" ]] ||
    die "certification receipt did not pass"
  [[ "$(json_get "$receipt" project)" == "$slug" ]] ||
    die "receipt project does not match"
  [[ "$(json_get "$receipt" kit_sha)" == "$sha" ]] ||
    die "receipt kit SHA does not match"
  [[ "$(json_get "$receipt" expected_previous_generation)" == "$expected_previous" ]] ||
    die "receipt expected previous generation does not match"
  [[ "$(json_get "$receipt" expires_epoch)" =~ ^[0-9]+$ ]] &&
    [[ "$(json_get "$receipt" expires_epoch)" -gt "$(now_epoch)" ]] ||
    die "certification receipt is stale"
  if ! verify_restrictive_regular_file "$receipt"; then
    die "certification receipt permissions are too broad"
  fi

  manifest_values="$(verify_release_from_manifest "$sha")"
  expected_tree="$(printf '%s' "$manifest_values" | awk -F'\t' '{print $1}')"
  manifest_origin="$(printf '%s' "$manifest_values" | awk -F'\t' '{print $2}')"
  [[ "$(json_get "$receipt" kit_tree)" == "$expected_tree" ]] ||
    die "receipt kit tree does not match trusted install manifest"
  [[ "$(json_get "$receipt" kit_origin)" == "$manifest_origin" ]] ||
    die "receipt kit origin does not match trusted install manifest"

  product_top="$(absolute_dir "$product")"
  [[ "$product_top" == "$(json_get "$receipt" product_path)" ]] ||
    die "receipt product path does not match"
  require_clean_product "$product_top"
  product_git_tree="$(product_tree "$product_top")"
  [[ "$product_git_tree" == "$(json_get "$receipt" product_tree)" ]] ||
    die "product Git tree drifted since certification"
  [[ "$(product_origin "$product_top")" == "$(json_get "$receipt" product_origin)" ]] ||
    die "product origin drifted since certification"
  pin="$(strict_product_pin "$product_top")"
  [[ "$pin" == "$sha" ]] || die "product pin does not match candidate SHA"
  kit_pin_hash="$(file_hash "$product_top/factory/KIT_PIN")"
  project_env_hash="$(file_hash "$product_top/factory/PROJECT.env")"
  [[ "$kit_pin_hash" == "$(json_get "$receipt" hashes.kit_pin)" ]] ||
    die "KIT_PIN hash drifted since certification"
  [[ "$project_env_hash" == "$(json_get "$receipt" hashes.project_env)" ]] ||
    die "PROJECT.env hash drifted since certification"
  contract="$(contract_version "$release")"
  [[ "$contract" == "$(json_get "$receipt" contract_version)" ]] ||
    die "Hermes contract version drifted"
  [[ "$(host_name)" == "$(json_get "$receipt" host)" ]] ||
    die "receipt was certified on a different host"
  [[ "$(uname -s)" == "$(json_get "$receipt" os)" &&
     "$(uname -m)" == "$(json_get "$receipt" architecture)" ]] ||
    die "receipt OS or architecture does not match"
  [[ "$(json_get "$receipt" kit_suite_evidence.evidence_id)" =~ ^[0-9a-f]{64}$ &&
     "$(json_get "$receipt" kit_suite_evidence.digest)" =~ ^[0-9a-f]{64}$ ]] ||
    die "receipt kit-suite evidence identity is invalid"
  [[ "$(json_get "$receipt" kit_suite_evidence.status)" == "pass" &&
     "$(json_get "$receipt" kit_suite_evidence.kit_sha)" == "$sha" &&
     "$(json_get "$receipt" kit_suite_evidence.kit_tree)" == "$expected_tree" &&
     "$(json_get "$receipt" kit_suite_evidence.canonical_origin)" == "$manifest_origin" &&
     "$(json_get "$receipt" kit_suite_evidence.sealed_release_path)" == "$release" &&
     "$(json_get "$receipt" kit_suite_evidence.release_tree)" == "$expected_tree" ]] ||
    die "receipt kit-suite evidence release binding is invalid"
  [[ "$(json_get "$receipt" kit_suite_evidence.host)" == "$(host_name)" &&
     "$(json_get "$receipt" kit_suite_evidence.os)" == "$(uname -s)" &&
     "$(json_get "$receipt" kit_suite_evidence.architecture)" == "$(uname -m)" &&
     "$(json_get "$receipt" kit_suite_evidence.suite_definition)" == "$KIT_SUITE_DEFINITION" &&
     "$(json_get "$receipt" kit_suite_evidence.certification_tool_version)" == "$CERTIFICATION_TOOL_VERSION" &&
     "$(json_get "$receipt" kit_suite_evidence.evidence_ttl_seconds)" == "$DEFAULT_SUITE_EVIDENCE_TTL" ]] ||
    die "receipt kit-suite evidence environment binding is invalid"
  case "$(json_get "$receipt" kit_suite_evidence.verification_source)" in
    local-full|github-actions-full) ;;
    *) die "receipt kit-suite evidence verification source is invalid" ;;
  esac
  evidence_created="$(json_get "$receipt" kit_suite_evidence.created_epoch)"
  evidence_expires="$(json_get "$receipt" kit_suite_evidence.expires_epoch)"
  [[ "$evidence_created" =~ ^[0-9]+$ && "$evidence_expires" =~ ^[0-9]+$ &&
     "$evidence_expires" -eq $((evidence_created + DEFAULT_SUITE_EVIDENCE_TTL)) &&
     "$evidence_expires" -gt "$(now_epoch)" &&
     "$(json_get "$receipt" expires_epoch)" -le "$evidence_expires" ]] ||
    die "receipt kit-suite evidence lifetime is invalid"
  case "$(json_get "$receipt" kit_suite_evidence.reused)" in
    true|false) ;;
    *) die "receipt kit-suite evidence reuse marker is invalid" ;;
  esac
  [[ "$(json_get "$receipt" checks.kit_suite)" == "pass" &&
     "$(json_get "$receipt" checks.github_required)" == "pass" &&
     "$(json_get "$receipt" checks.repo_check)" == "pass" &&
     "$(json_get "$receipt" checks.secret_scan)" == "pass" &&
     "$(json_get "$receipt" checks.product_certification)" == "pass" ]] ||
    die "receipt is missing required passing checks"
}

validate_receipt() {
  validate_receipt_snapshot "$1" "$2" "$3" "$4" "$5" ""
}

copy_receipt_snapshot() {
  local source="$1" destination="$2"
  python3 - "$source" "$destination" <<'PY'
import os, pathlib, stat, sys, tempfile
source, destination = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
st = source.lstat()
if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
    raise SystemExit("receipt source is unsafe")
data = source.read_bytes()
fd = os.open(str(destination), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "wb") as stream:
    stream.write(data)
    stream.flush()
    os.fsync(stream.fileno())
PY
}

claim_receipt() {
  local receipt="$1" transaction="$2" receipt_id claim
  receipt_id="$(json_get "$receipt" receipt_id)"
  claim="$CONSUMED_DIR/$receipt_id.json"
  [[ ! -L "$claim" ]] || die "receipt consumption path is unsafe"
  python3 - "$claim" "$receipt_id" "$transaction" "$(now_iso)" <<'PY'
import json, os, sys
path, receipt_id, transaction, timestamp = sys.argv[1:]
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w") as stream:
    json.dump({
        "receipt_id": receipt_id,
        "transaction_id": transaction,
        "consumed_at": timestamp,
    }, stream, sort_keys=True, separators=(",", ":"))
    stream.write("\n")
    stream.flush()
    os.fsync(stream.fileno())
directory_fd = os.open(os.path.dirname(path), os.O_RDONLY)
try:
    os.fsync(directory_fd)
finally:
    os.close(directory_fd)
PY
}

require_receipt_consumption() {
  local receipt_id="$1" transaction="$2" claim
  claim="$CONSUMED_DIR/$receipt_id.json"
  [[ -f "$claim" && ! -L "$claim" ]] ||
    die "receipt consumption record is missing or unsafe"
  verify_restrictive_regular_file "$claim" ||
    die "receipt consumption record permissions are unsafe"
  [[ "$(json_get "$claim" receipt_id)" == "$receipt_id" &&
     "$(json_get "$claim" transaction_id)" == "$transaction" ]] ||
    die "receipt consumption record does not match transaction"
}

snapshot_receipt_from_journal() {
  local journal="$1" destination="$2"
  python3 - "$journal" <<'PY' | atomic_json_from_stdin "$destination"
import json, sys
value = json.load(open(sys.argv[1]))
print(json.dumps(value["receipt_snapshot"]))
PY
  chmod 600 "$destination"
  [[ "$(file_hash "$destination")" == "$(json_get "$journal" receipt_hash)" ]] ||
    die "journal receipt snapshot hash mismatch"
}

has_active_runs() {
  local product="$1" file
  for file in "$product/factory/.active-runs/"*.pid \
    "$product/factory/.active-runs/"*.lock "$product/factory/runs/"*.pid; do
    [[ -e "$file" || -L "$file" ]] && return 0
  done
  return 1
}

require_dispatch_drained() {
  factory_dispatch_has_leases "$1" &&
    die "product has dispatcher leases; release them before maintenance operations"
  return 0
}

maintenance_file_for() { printf '%s/factory/MAINTENANCE\n' "$1"; }

write_maintenance_marker() {
  local slug="$1" product="$2" marker
  marker="$(maintenance_file_for "$product")"
  [[ ! -L "$marker" ]] || die "MAINTENANCE may not be a symlink"
  python3 - "$marker" "$slug" "$product" "$(now_iso)" <<'PY' | atomic_json_from_stdin "$marker"
import json, sys
marker, slug, product, timestamp = sys.argv[1:]
print(json.dumps({
    "schema_version": 1,
    "project": slug,
    "product_path": product,
    "published_at": timestamp,
}))
PY
}

require_maintenance_after_lock() {
  local slug="$1" product="$2" marker lock
  marker="$(maintenance_file_for "$product")"
  lock="$product/factory/.launch.lock"
  [[ -d "$lock" && ! -L "$lock" ]] ||
    die "launch lock must be held before maintenance validation"
  [[ -f "$marker" && ! -L "$marker" ]] ||
    die "activation requires MAINTENANCE published by factory-kit pause"
  [[ "$(json_get "$marker" schema_version)" == "1" &&
     "$(json_get "$marker" project)" == "$slug" &&
     "$(json_get "$marker" product_path)" == "$product" ]] ||
    die "MAINTENANCE marker is invalid"
}

validate_ticket_leases() {
  local product="$1" sha="$2" origin="$3"
  python3 - "$product/factory" "$sha" "$SCRIPT_ROOT/scripts/lib" \
    "$RELEASES_DIR/$sha/scripts" "$origin" <<'PY'
import importlib.util, json, pathlib, re, subprocess, sys
factory, candidate, lib, candidate_scripts, origin = (
    pathlib.Path(sys.argv[1]), sys.argv[2], pathlib.Path(sys.argv[3]),
    pathlib.Path(sys.argv[4]), sys.argv[5],
)
sys.path.insert(0, sys.argv[3])
from effective_ticket import ticket_branch_prefix
from legacy_closeout import ValidationError, protected_terminal

authorization = None
authorized = {}
used_authorizations = set()
migration_policy = None

def load_migration_policy():
    global migration_policy
    if migration_policy is not None:
        return migration_policy
    spec = importlib.util.spec_from_file_location(
        "factory_inflight_model_manager", candidate_scripts / "model-manager.py",
    )
    if spec is None or spec.loader is None:
        raise SystemExit("candidate model migration validator is unavailable")
    manager = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(manager)
        catalog, routes, _, profiles = manager.ROUTER.load_policy(
            candidate_scripts / "model-routing" / "catalog-v1.json",
            candidate_scripts / "model-routing" / "profiles-v1.json",
        )
    except Exception:
        raise SystemExit("candidate model migration policy is invalid")
    migration_policy = manager, catalog, routes, profiles
    return migration_policy

def no_duplicates(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate key")
        value[key] = item
    return value

def load_inflight_authorization():
    global authorization, authorized
    if authorization is not None:
        return
    relative = "factory/migrations/inflight-release/%s.json" % candidate
    result = subprocess.run(
        ["git", "-C", str(repo), "show", "HEAD:" + relative],
        text=True, capture_output=True,
    )
    if result.returncode:
        raise SystemExit("nonterminal ticket uses another kit without an exact in-flight release authorization")
    if len(result.stdout.encode("utf-8")) > 1024 * 1024:
        raise SystemExit("in-flight release authorization is malformed")
    head = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True,
    ).strip()
    remote_main = subprocess.check_output([
        "git", "-C", str(repo), "ls-remote", "--heads", "--", origin,
        "refs/heads/main",
    ], text=True).split()
    if not remote_main or remote_main[0] != head:
        raise SystemExit("in-flight release authorization is not on protected main")

    try:
        value = json.loads(result.stdout, object_pairs_hook=no_duplicates)
    except (json.JSONDecodeError, ValueError):
        raise SystemExit("in-flight release authorization is malformed")
    expected = {
        "schema", "repository", "source_kit_sha", "target_kit_sha", "tickets",
    }
    if (
        not isinstance(value, dict)
        or set(value) != expected
        or value.get("schema") != "nysa.software-factory.inflight-release-authorization/v1"
    ):
        raise SystemExit("in-flight release authorization is malformed")
    project = factory / "PROJECT.env"
    repositories = []
    for raw in project.read_text().splitlines():
        match = re.fullmatch(
            r"\s*(?:export\s+)?GH_REPO\s*=\s*['\"]?([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)['\"]?\s*",
            raw,
        )
        if match:
            repositories.append(match.group(1))
    if repositories != [value.get("repository")]:
        raise SystemExit("in-flight release authorization repository does not match the product")
    source = value.get("source_kit_sha", "")
    if (
        not re.fullmatch(r"[0-9a-f]{40}", source)
        or source == candidate
        or value.get("target_kit_sha") != candidate
        or not isinstance(value.get("tickets"), list)
        or not value["tickets"]
    ):
        raise SystemExit("in-flight release authorization kit binding is invalid")
    entries = {}
    ordered = []
    allowed_states = {"Ready", "Planning", "Building", "Review", "Awaiting Approval", "Approved"}
    for item in value["tickets"]:
        if not isinstance(item, dict) or set(item) != {"ticket", "branch", "head", "state"}:
            raise SystemExit("in-flight release authorization ticket entry is malformed")
        ticket_id = item.get("ticket", "")
        if (
            not re.fullmatch(r"T-[0-9]+", ticket_id)
            or item.get("branch") != prefix + ticket_id
            or not re.fullmatch(r"[0-9a-f]{40}", item.get("head", ""))
            or item.get("state") not in allowed_states
            or ticket_id in entries
        ):
            raise SystemExit("in-flight release authorization ticket entry is invalid")
        entries[ticket_id] = item
        ordered.append(ticket_id)
    if ordered != sorted(ordered):
        raise SystemExit("in-flight release authorization tickets are not canonical")
    authorization = value
    authorized = entries

def authorize_inflight(ticket_id, branch, remote_tip, source_ref, state, lease):
    load_inflight_authorization()
    item = authorized.get(ticket_id)
    if (
        lease != authorization["source_kit_sha"]
        or not remote_tip
        or source_ref == "HEAD"
        or item is None
        or item["branch"] != branch
        or item["head"] != remote_tip
        or item["state"] != state
    ):
        raise SystemExit("nonterminal ticket does not match its exact in-flight release authorization")
    plan_path = "factory/route-plans/%s.json" % ticket_id
    result = subprocess.run(
        ["git", "-C", str(repo), "show", remote_tip + ":" + plan_path],
        text=True, capture_output=True,
    )
    if result.returncode or len(result.stdout.encode("utf-8")) > 1024 * 1024:
        raise SystemExit("authorized in-flight ticket lacks a safe migratable v1 route plan")
    try:
        plan = json.loads(result.stdout, object_pairs_hook=no_duplicates)
        manager, catalog, routes, profiles = load_migration_policy()
        if (
            set(plan) != {"schema", "ticket", "kit_sha", "created_at", "resolution"}
            or plan.get("schema") != "ticket-model-route-plan/v1"
            or plan.get("ticket") != ticket_id
            or plan.get("kit_sha") != authorization["source_kit_sha"]
        ):
            raise ValueError("route plan identity mismatch")
        manager._validate_pin(
            plan, catalog, routes, profiles, allow_historical_catalog=True,
        )
    except Exception:
        raise SystemExit("authorized in-flight ticket route plan is not migratable by the candidate")
    used_authorizations.add(ticket_id)

def protected_legacy_approval(ticket_id, lease, source_ref, text):
    if source_ref != "HEAD":
        return False
    approvals = re.findall(r"(?mi)^Operator-Approval:\s*(.*?)\s*$", text)
    if approvals != ["Linear"]:
        return False
    head = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True,
    ).strip()
    remote_main = subprocess.check_output([
        "git", "-C", str(repo), "ls-remote", "--heads", "--", origin,
        "refs/heads/main",
    ], text=True).split()
    if not remote_main or remote_main[0] != head:
        return False
    root = "factory/attestations/%s" % ticket_id
    values = []
    for name in ("bundle.json", "approval.json"):
        path = root + "/" + name
        result = subprocess.run(
            ["git", "-C", str(repo), "show", "HEAD:" + path],
            text=True, capture_output=True,
        )
        if result.returncode:
            return False
        try:
            values.append(json.loads(result.stdout))
        except json.JSONDecodeError:
            return False
    bundle, approval = values
    branch = prefix + ticket_id
    bundle_blob = subprocess.check_output([
        "git", "-C", str(repo), "rev-parse", "HEAD:" + root + "/bundle.json",
    ], text=True).strip()
    return (
        bundle.get("schema") == "nysa.software-factory.ticket-bundle/v1"
        and approval.get("schema") == "nysa.software-factory.ticket-approval/v1"
        and bundle.get("ticket") == approval.get("ticket") == ticket_id
        and bundle.get("branch") == approval.get("branch") == branch
        and bundle.get("kit_sha") == approval.get("kit_sha") == lease
        and bundle.get("repository") == approval.get("repository")
        and bundle.get("pr_number") == approval.get("pr_number")
        and bundle.get("reviewed_sha") == approval.get("reviewed_sha")
        and bundle.get("bundle_blob") == approval.get("bundle_blob")
        and approval.get("bundle_attestation_blob") == bundle_blob
        and re.fullmatch(r"[0-9a-f]{40}", bundle.get("reviewed_sha", ""))
        and re.fullmatch(r"[0-9a-f]{40}", bundle.get("bundle_blob", ""))
    )

tickets = factory / "tickets"
repo = factory.parent
prefix = ticket_branch_prefix(factory)
ticket_ids = set()
if tickets.is_dir():
    for path in tickets.glob("T-*.md"):
        if re.fullmatch(r"T-[0-9]+\.md", path.name):
            if path.is_symlink():
                raise SystemExit("ticket path is a symlink: %s" % path)
            ticket_ids.add(path.stem)
refs = subprocess.check_output([
    "git", "-C", str(repo), "for-each-ref", "--format=%(refname)",
    "refs/remotes/origin/" + prefix, "refs/heads/" + prefix,
], text=True).splitlines()
remote_tips = {}
remote_lines = subprocess.check_output([
    "git", "-C", str(repo), "ls-remote", "--heads", "--", origin,
    "refs/heads/" + prefix + "T-*",
], text=True).splitlines()
for line in remote_lines:
    tip, ref = line.split()
    match = re.fullmatch(r"refs/heads/" + re.escape(prefix) + r"(T-[0-9]+)", ref)
    if not match or not re.fullmatch(r"[0-9a-f]{40}", tip):
        raise SystemExit("remote ticket ref is malformed")
    ticket_id = match.group(1)
    if ticket_id in remote_tips:
        raise SystemExit("remote ticket ref is duplicated: %s" % ticket_id)
    remote_tips[ticket_id] = tip
    ticket_ids.add(ticket_id)
for ref in refs:
    branch = re.sub(r"^refs/(?:remotes/origin|heads)/", "", ref)
    match = re.fullmatch(re.escape(prefix) + r"(T-[0-9]+)", branch)
    if match:
        ticket_ids.add(match.group(1))
for ticket_id in sorted(ticket_ids):
    branch = prefix + ticket_id
    remote_ref = "refs/remotes/origin/" + branch
    local_ref = "refs/heads/" + branch
    remote_tip = remote_tips.get(ticket_id, "")
    tracking = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", remote_ref],
        text=True, capture_output=True,
    )
    tracking_tip = tracking.stdout.strip() if tracking.returncode == 0 else ""
    if tracking_tip and remote_tip != tracking_tip:
        raise SystemExit("%s remote ticket ref is stale or unverified" % ticket_id)
    audit_ref = ""
    if remote_tip:
        if tracking_tip:
            source_ref = remote_ref
        else:
            audit_ref = "refs/factory/lease-audit/" + ticket_id
            fetched = subprocess.run([
                "git", "-C", str(repo), "fetch", "--quiet", "--no-tags", origin,
                "refs/heads/" + branch + ":" + audit_ref,
            ])
            fetched_tip = subprocess.run(
                ["git", "-C", str(repo), "rev-parse", "--verify", audit_ref],
                text=True, capture_output=True,
            )
            if fetched.returncode != 0 or fetched_tip.stdout.strip() != remote_tip:
                subprocess.run(
                    ["git", "-C", str(repo), "update-ref", "-d", audit_ref],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                raise SystemExit("%s remote ticket ref could not be verified" % ticket_id)
            source_ref = audit_ref
    elif subprocess.run(
        ["git", "-C", str(repo), "show-ref", "--verify", "--quiet", local_ref]
    ).returncode == 0:
        raise SystemExit("%s has an unverified local-only ticket branch" % ticket_id)
    else:
        source_ref = "HEAD"
    relative = "factory/tickets/%s.md" % ticket_id
    content = subprocess.run(
        ["git", "-C", str(repo), "show", source_ref + ":" + relative],
        text=True, capture_output=True,
    )
    if audit_ref:
        subprocess.run(
            ["git", "-C", str(repo), "update-ref", "-d", audit_ref], check=True
        )
    if content.returncode != 0:
        raise SystemExit("%s is missing from its committed ticket source" % ticket_id)
    text = content.stdout
    states = re.findall(r"(?mi)^State:\s*(.*?)\s*$", text)
    leases = re.findall(r"(?mi)^Kit-SHA:\s*(.*?)\s*$", text)
    if len(states) != 1:
        raise SystemExit("%s must contain exactly one State field" % ticket_id)
    if len(leases) > 1:
        raise SystemExit("%s contains duplicate Kit-SHA fields" % ticket_id)
    state = states[0].strip()
    lease = leases[0].strip() if leases else ""
    if lease and not re.fullmatch(r"[0-9a-f]{40}", lease):
        raise SystemExit("%s has a noncanonical Kit-SHA" % ticket_id)
    if state.lower() == "done":
        try:
            terminal = protected_terminal(repo, ticket_id)
        except ValidationError as error:
            raise SystemExit(
                "%s claims Done without valid protected-main terminal evidence: %s"
                % (ticket_id, error)
            )
        if terminal.get("basis") not in (
            "attested-done", "validated-legacy-closeout",
            "validated-terminal-backfill",
            "validated-protected-merge-reconciliation",
        ):
            raise SystemExit("%s has an unknown terminal basis" % ticket_id)
        continue
    if lease:
        if lease != candidate:
            if state.lower() == "approved" and protected_legacy_approval(
                ticket_id, lease, source_ref, text,
            ):
                continue
            authorize_inflight(
                ticket_id, branch, remote_tip, source_ref, state, lease,
            )
    elif state.lower() not in ("ready", "backlog", "blocked-escalated"):
        raise SystemExit("%s from %s is in progress without a Kit-SHA lease" % (ticket_id, source_ref))
if authorization is not None and used_authorizations != set(authorized):
    raise SystemExit("in-flight release authorization contains an unused ticket")
PY
}

cmd_pause() {
  local slug="$1" product="$2" product_top launch_lock
  validate_slug "$slug"
  validate_managed_layout "$slug"
  product_top="$(absolute_dir "$product")"
  [[ -d "$product_top/factory" && ! -L "$product_top/factory" ]] ||
    die "product factory directory is unsafe"
  [[ ! -L "$product_top/factory/.launch.lock" ]] ||
    die "product launch lock path is unsafe"
  launch_lock="$product_top/factory/.launch.lock"
  write_maintenance_marker "$slug" "$product_top"
  acquire_lock "$launch_lock" "product launch"
  require_maintenance_after_lock "$slug" "$product_top"
  if has_active_runs "$product_top"; then
    die "product has active runs; MAINTENANCE remains published"
  fi
  require_dispatch_drained "$product_top"
  if [[ "${FACTORY_KIT_TEST_HOLD_LAUNCH_LOCK_SECONDS:-0}" != "0" ]]; then
    sleep "$FACTORY_KIT_TEST_HOLD_LAUNCH_LOCK_SECONDS"
  fi
  release_lock "$launch_lock"
  say "PAUSE OK: project=$slug"
}

active_file_for() { printf '%s/%s/active.json\n' "$PROJECTS_DIR" "$1"; }
journal_dir_for() { printf '%s/%s/activation-journal\n' "$PROJECTS_DIR" "$1"; }

latest_open_journal() {
  local directory="$1"
  if [[ ! -d "$directory" ]]; then
    printf '\n'
    return 0
  fi
  python3 - "$directory" <<'PY'
import json
import pathlib
import sys

items = []
for path in pathlib.Path(sys.argv[1]).glob("*.json"):
    try:
        value = json.loads(path.read_text())
        phase = value.get("phase")
        if phase not in ("committed", "rolled_back"):
            items.append((int(value.get("generation", 0)), str(path)))
    except Exception:
        raise SystemExit("invalid activation journal: %s" % path)
if not items:
    print("")
else:
    print(max(items)[1])
PY
}

next_generation() {
  local slug="$1" active="$2" directory
  directory="$(journal_dir_for "$slug")"
  python3 - "$active" "$directory" <<'PY'
import json, os, pathlib, sys
highest = 0
if os.path.isfile(sys.argv[1]):
    try:
        highest = max(highest, int(json.load(open(sys.argv[1])).get("generation", 0)))
    except Exception:
        raise SystemExit("invalid active record")
directory = pathlib.Path(sys.argv[2])
if directory.is_dir():
    for path in directory.glob("*.json"):
        try:
            highest = max(highest, int(json.loads(path.read_text()).get("generation", 0)))
        except Exception:
            raise SystemExit("invalid activation journal")
print(highest + 1)
PY
}

set_journal_phase() {
  local journal="$1" phase="$2"
  python3 - "$journal" "$phase" <<'PY' | atomic_json_from_stdin "$journal"
import json, sys, time
path, phase = sys.argv[1:]
value = json.load(open(path))
value["phase"] = phase
value["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
history = value.setdefault("phase_history", [])
if not history or history[-1] != phase:
    history.append(phase)
print(json.dumps(value))
PY
  if [[ "${FACTORY_KIT_FAIL_AFTER_PHASE:-}" == "$phase" ]]; then
    die "injected failure after phase $phase"
  fi
}

switch_active_from_journal() {
  local journal="$1" active="$2" which="$3"
  python3 - "$journal" "$active" "$which" <<'PY'
import json, os, pathlib, sys, tempfile
journal, active, which = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]), sys.argv[3]
value = json.load(open(journal))
record = value[which]
if record is None:
    raise SystemExit("journal has no record to restore")
if active.parent.is_symlink() or not active.parent.is_dir():
    raise SystemExit("active record parent is unsafe")
fd, temporary = tempfile.mkstemp(prefix=".%s." % active.name, dir=str(active.parent))
try:
    with os.fdopen(fd, "w") as stream:
        json.dump(record, stream, sort_keys=True, separators=(",", ":"))
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, active)
    directory_fd = os.open(str(active.parent), os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
finally:
    if os.path.exists(temporary):
        os.unlink(temporary)
PY
}

remove_active_if_no_previous() {
  local journal="$1" active="$2"
  if [[ -z "$(json_get "$journal" previous_record)" ]]; then
    rm -f "$active"
    return 0
  fi
  return 1
}

cmd_install() {
  local sha="$1" source="$2" origin_override="$3"
  local source_top canonical_sha kit_tree release temp checkout origin origin_identity lock manifest
  local workspace
  case "${FACTORY_KIT_TEST_FAIL_PUBLISH_PHASE:-}" in
    ""|contents_sealed|release_verified) ;;
    *) die "unknown publish fault-injection phase" ;;
  esac
  validate_sha "$sha"
  validate_managed_layout
  source_top="$(absolute_dir "$source")"
  git -C "$source_top" rev-parse --git-dir >/dev/null 2>&1 ||
    die "kit source is not a Git repository"
  origin="$(verify_origin "$source_top" "$origin_override")"
  git -C "$source_top" fetch -q origin main ||
    die "failed to fetch origin/main"
  canonical_sha="$(git -C "$source_top" rev-parse "$sha^{commit}" 2>/dev/null || true)"
  [[ "$canonical_sha" == "$sha" ]] ||
    die "SHA is not the requested canonical commit"
  git -C "$source_top" merge-base --is-ancestor "$sha" refs/remotes/origin/main ||
    die "SHA is not an ancestor of origin/main"
  verify_required_github_checks "$sha"
  kit_tree="$(git -C "$source_top" rev-parse "$sha^{tree}")"
  origin_identity="$(canonical_origin_identity "$origin")"

  safe_create_directory "$KITS_ROOT"
  validate_managed_layout
  lock="$KITS_ROOT/.install.lock"
  acquire_lock "$lock" "global install"
  ensure_managed_directories
  validate_managed_layout
  release="$RELEASES_DIR/$sha"
  manifest="$(manifest_file_for "$sha")"
  [[ ! -L "$release" ]] || die "release path may not be a symlink"
  if [[ -e "$release" ]]; then
    [[ -d "$release" ]] || die "partial install exists at $release"
    [[ -f "$manifest" && ! -L "$manifest" ]] ||
      die "partial install has no trusted manifest: $sha"
    verify_release_from_manifest "$sha" >/dev/null ||
      die "partial or corrupt release exists at $release"
    [[ "$(json_get "$manifest" git_tree)" == "$kit_tree" &&
       "$(json_get "$manifest" canonical_origin)" == "$origin_identity" ]] ||
      die "existing install manifest does not match requested source"
    release_lock "$lock"
    say "INSTALL OK: $sha already installed"
    return
  fi
  [[ ! -e "$manifest" && ! -L "$manifest" ]] ||
    die "partial install manifest exists without release: $sha"

  workspace="$(mktemp -d "${TMPDIR:-/tmp}/factory-kit-install.XXXXXX")"
  remember_temp "$workspace"
  mkdir "$workspace/home" "$workspace/tmp"
  checkout="$workspace/checkout"
  git clone -q --no-local "$source_top" "$checkout" ||
    die "could not create disposable candidate clone"
  git -C "$checkout" remote set-url origin "$origin"
  git -C "$checkout" checkout -q --detach "$sha"
  prepare_pinned_scanner "$source_top" "$checkout" "$workspace/tmp" ||
    die "could not stage the pinned scanner for isolated checks"
  local remote_evidence_id verification_source="github-actions-full" check_mode="platform-smoke"
  remote_evidence_id="$(verified_remote_full_ci "$sha" "$kit_tree" 2>/dev/null)" ||
    die "exact successful main GitHub CI evidence is required for install: $sha"
  say "REMOTE CI VERIFIED: $sha; running local platform smoke only"
  run_kit_checks_isolated "$checkout" "$workspace/home" "$workspace/tmp" \
    "$workspace" "install" "$check_mode" "$source_top" ||
    die "kit checks failed in disposable checkout"
  record_certification_trace "kit-suite:install:$verification_source"
  # Checks run in a disposable workspace and may create caches or reports.
  # Only tracked-tree mutation is disqualifying; the sealed release is built
  # afterward from the verified Git object, never from this workspace.
  git -C "$checkout" diff --quiet &&
    git -C "$checkout" diff --cached --quiet ||
    die "kit checks modified the tracked candidate tree"
  [[ "$(git -C "$checkout" rev-parse HEAD)" == "$sha" &&
     "$(git -C "$checkout" rev-parse 'HEAD^{tree}')" == "$kit_tree" ]] ||
    die "disposable checkout changed from the requested tree"

  temp="$RELEASES_DIR/.install-$sha-$$"
  [[ ! -e "$temp" && ! -L "$temp" ]] || die "partial temporary install already exists"
  mkdir "$temp"
  remember_temp "$temp"
  materialize_git_tree "$source_top" "$sha" "$temp" ||
    die "failed to materialize exact candidate tree"
  verify_symlinks_contained "$temp" || die "candidate contains an escaping symlink"
  [[ "$(git_tree_for_directory "$temp")" == "$kit_tree" ]] ||
    die "materialized release does not match Git tree"
  seal_release_contents_for_publish "$temp"
  verify_release_publish_ready "$temp" ||
    die "failed to seal staged release contents"
  record_publish_phase contents_sealed "$temp" "$release" "$manifest"
  maybe_fail_publish_phase contents_sealed "$temp"
  mv "$temp" "$release"
  chmod a-w "$release"
  forget_temp "$temp"
  remember_temp "$release"
  record_publish_phase renamed_root_sealed "$release" "$release" "$manifest"
  fsync_directory "$release"
  fsync_directory "$RELEASES_DIR"
  record_publish_phase parent_fsynced "$release" "$release" "$manifest"
  verify_read_only "$release" || die "failed to seal published release read-only"
  verify_release "$sha" "$kit_tree"
  record_publish_phase release_verified "$release" "$release" "$manifest"
  maybe_fail_publish_phase release_verified "$release"
  write_install_manifest "$sha" "$origin_identity" "$kit_tree" "$release"
  forget_temp "$release"
  record_publish_phase manifest_written "$release" "$release" "$manifest"
  verify_release_from_manifest "$sha" >/dev/null
  validate_suite_evidence_ttl
  write_suite_evidence "$sha" "$origin_identity" "$kit_tree" "$release" \
    "$verification_source" "$remote_evidence_id"
  release_lock "$lock"
  say "INSTALL OK: $sha ($origin)"
}

cmd_certify() {
  local slug="$1" product="$2" sha="$3"
  local product_top release kit_tree pin product_git_tree product_repo contract manifest_values
  local writable writable_head script created expires receipt_id receipt previous_generation workspace
  local kit_pin_hash project_env_hash kit_origin lock evidence_values evidence_id
  local evidence_digest evidence_created evidence_expires evidence_source suite_reused
  local refresh_source refresh_mode refresh_remote_id
  validate_slug "$slug"
  validate_sha "$sha"
  validate_suite_evidence_ttl
  validate_managed_roots "$slug"
  safe_create_directory "$KITS_ROOT"
  lock="$KITS_ROOT/.install.lock"
  acquire_lock "$lock" "global install"
  ensure_managed_directories "$slug"
  remove_symlinked_suite_evidence "$(suite_evidence_file_for "$sha")"
  validate_project_storage "$slug"
  product_top="$(absolute_dir "$product")"
  release="$RELEASES_DIR/$sha"
  manifest_values="$(verify_release_from_manifest "$sha")"
  kit_tree="$(printf '%s' "$manifest_values" | awk -F'\t' '{print $1}')"
  kit_origin="$(printf '%s' "$manifest_values" | awk -F'\t' '{print $2}')"
  pin="$(strict_product_pin "$product_top")"
  [[ "$pin" == "$sha" ]] || die "product pin does not match candidate SHA"
  require_clean_product "$product_top"
  product_git_tree="$(product_tree "$product_top")"
  product_repo="$(product_origin "$product_top")"
  contract="$(contract_version "$release")"
  workspace="$(mktemp -d "${TMPDIR:-/tmp}/factory-kit-certification.XXXXXX")"
  remember_temp "$workspace"
  mkdir "$workspace/home" "$workspace/tmp"
  ISOLATED_HOME="$workspace/home"
  prepare_writable_release_copy "$release" "$workspace"
  writable="$PREPARED_COPY"
  writable_head="$(git -C "$writable" rev-parse HEAD)"
  prepare_writable_product_copy "$product_top" "$workspace"
  script="$(certify_script_path "$PREPARED_PRODUCT")" ||
    die "invalid product certification contract"
  suite_reused=true
  if evidence_values="$(validated_suite_evidence \
      "$sha" "$kit_origin" "$kit_tree" "$release" 2>/dev/null)"; then
    record_certification_trace "kit-suite:reused"
  else
    suite_reused=false
    refresh_source="github-actions-full"
    refresh_mode="platform-smoke"
    refresh_remote_id="$(verified_remote_full_ci "$sha" "$kit_tree" 2>/dev/null)" ||
      die "exact successful main GitHub CI evidence is required to refresh certification: $sha"
    say "REMOTE CI VERIFIED: $sha; refreshing evidence with local platform smoke only"
    prepare_pinned_scanner "$release" "$writable" "$workspace/tmp" ||
      die "could not stage the pinned scanner for isolated certification"
    run_kit_checks_isolated "$writable" "$ISOLATED_HOME" "$workspace/tmp" \
      "$workspace" "certification" "$refresh_mode" "$product_top" "$release" ||
      die "kit certification checks failed"
    record_certification_trace "kit-suite:certification"
    git -C "$writable" diff --quiet &&
      git -C "$writable" diff --cached --quiet ||
      die "kit certification checks modified the tracked candidate tree"
    [[ "$(git -C "$writable" rev-parse HEAD)" == "$writable_head" ]] ||
      die "kit certification checks changed the candidate commit"
    verify_release_from_manifest "$sha" >/dev/null
    write_suite_evidence "$sha" "$kit_origin" "$kit_tree" "$release" \
      "$refresh_source" "$refresh_remote_id"
    evidence_values="$(validated_suite_evidence \
      "$sha" "$kit_origin" "$kit_tree" "$release")" ||
      die "fresh kit-suite evidence failed validation"
  fi
  evidence_id="$(printf '%s' "$evidence_values" | awk -F'\t' '{print $1}')"
  evidence_digest="$(printf '%s' "$evidence_values" | awk -F'\t' '{print $2}')"
  evidence_created="$(printf '%s' "$evidence_values" | awk -F'\t' '{print $3}')"
  evidence_expires="$(printf '%s' "$evidence_values" | awk -F'\t' '{print $4}')"
  evidence_source="$(printf '%s' "$evidence_values" | awk -F'\t' '{print $5}')"
  release_lock "$lock"
  run_product_certification "$PREPARED_PRODUCT" "$script" "$sha" "$writable" \
    "$workspace" "$product_top" "$release" ||
    die "product certification failed"
  record_certification_trace "product-certification"
  verify_release_from_manifest "$sha" >/dev/null
  require_clean_product "$product_top"
  [[ "$(product_tree "$product_top")" == "$product_git_tree" ]] ||
    die "product tree changed during certification"

  [[ "$DEFAULT_RECEIPT_TTL" =~ ^[0-9]+$ && "$DEFAULT_RECEIPT_TTL" -gt 0 ]] ||
    die "receipt TTL must be a positive integer"
  created="$(now_epoch)"
  expires=$((created + DEFAULT_RECEIPT_TTL))
  [[ "$expires" -le "$evidence_expires" ]] || expires="$evidence_expires"
  [[ "$expires" -gt "$created" ]] ||
    die "kit-suite evidence expired during product certification"
  kit_pin_hash="$(file_hash "$product_top/factory/KIT_PIN")"
  project_env_hash="$(file_hash "$product_top/factory/PROJECT.env")"
  previous_generation=""
  if [[ -f "$(active_file_for "$slug")" ]]; then
    [[ ! -L "$(active_file_for "$slug")" &&
       "$(json_get "$(active_file_for "$slug")" product_path)" == "$product_top" ]] ||
      die "existing activation record belongs to a different product"
    previous_generation="$(json_get "$(active_file_for "$slug")" generation)"
  fi
  receipt_id="$(printf '%s\n' "$slug|$sha|$kit_tree|$product_git_tree|$created|$previous_generation|$CERTIFICATION_TOOL_VERSION|$(random_nonce)" |
    shasum -a 256 | awk '{print $1}')"
  receipt="$RECEIPTS_DIR/$receipt_id.json"
  [[ ! -e "$receipt" && ! -L "$receipt" ]] || die "receipt ID collision"
  umask 077
  python3 - "$slug" "$sha" "$kit_tree" "$kit_origin" \
    "$product_top" "$product_repo" "$product_git_tree" "$kit_pin_hash" \
    "$project_env_hash" "$contract" "$(host_name)" "$(uname -s)" "$(uname -m)" \
    "$created" "$expires" "$receipt_id" "$previous_generation" \
    "$CERTIFICATION_TOOL_VERSION" "$evidence_id" "$evidence_digest" \
    "$evidence_created" "$evidence_expires" "$DEFAULT_SUITE_EVIDENCE_TTL" \
    "$KIT_SUITE_DEFINITION" "$suite_reused" "$release" "$evidence_source" \
    <<'PY' | atomic_json_from_stdin "$receipt"
import json, sys, time
(slug, sha, kit_tree, kit_origin, product_path, product_origin, product_tree,
 kit_pin_hash, project_env_hash, contract, host, os_name, architecture,
 created, expires, receipt_id, previous_generation, tool_version, evidence_id,
 evidence_digest, evidence_created, evidence_expires, evidence_ttl,
 suite_definition, suite_reused, release, evidence_source) = sys.argv[1:]
value = {
    "schema_version": 2,
    "certification_tool_version": int(tool_version),
    "receipt_id": receipt_id,
    "status": "pass",
    "project": slug,
    "kit_sha": sha,
    "kit_tree": kit_tree,
    "kit_origin": kit_origin,
    "product_path": product_path,
    "product_origin": product_origin,
    "product_tree": product_tree,
    "hashes": {
        "kit_pin": kit_pin_hash,
        "project_env": project_env_hash,
    },
    "contract_version": contract,
    "host": host,
    "os": os_name,
    "architecture": architecture,
    "created_epoch": int(created),
    "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(created))),
    "expires_epoch": int(expires),
    "expires_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(expires))),
    "expected_previous_generation": int(previous_generation) if previous_generation else None,
    "kit_suite_evidence": {
        "evidence_id": evidence_id,
        "digest": evidence_digest,
        "status": "pass",
        "kit_sha": sha,
        "kit_tree": kit_tree,
        "canonical_origin": kit_origin,
        "sealed_release_path": release,
        "release_tree": kit_tree,
        "host": host,
        "os": os_name,
        "architecture": architecture,
        "suite_definition": suite_definition,
        "certification_tool_version": int(tool_version),
        "evidence_ttl_seconds": int(evidence_ttl),
        "created_epoch": int(evidence_created),
        "expires_epoch": int(evidence_expires),
        "reused": suite_reused == "true",
        "verification_source": evidence_source,
    },
    "checks": {
        "kit_suite": "pass",
        "github_required": "pass",
        "repo_check": "pass",
        "secret_scan": "pass",
        "product_certification": "pass",
        "release_tree": "pass",
        "product_tree": "pass",
        "pin_and_config": "pass",
    },
}
print(json.dumps(value))
PY
  chmod 600 "$receipt"
  say "CERTIFY OK: $receipt_id"
  say "$receipt"
}

resolve_receipt() {
  local requested="$1" slug="$2" sha="$3" receipt_dir absolute_receipt
  if [[ -n "$requested" ]]; then
    [[ "$requested" != *".."* ]] || die "receipt path traversal is forbidden"
    receipt_dir="$(cd "$(dirname "$requested")" 2>/dev/null && pwd -P)" ||
      die "receipt directory does not exist"
    [[ "$receipt_dir" == "$RECEIPTS_DIR" ]] ||
      die "receipt must be inside $RECEIPTS_DIR"
    [[ "$(basename "$requested")" =~ ^[0-9a-f]{64}\.json$ ]] ||
      die "receipt filename is invalid"
    absolute_receipt="$receipt_dir/$(basename "$requested")"
    printf '%s\n' "$absolute_receipt"
  else
    find_receipt "$slug" "$sha" || die "no certification receipt for $slug at $sha"
  fi
}

plan_activation() {
  local slug="$1" product="$2" sha="$3" requested_receipt="$4"
  local product_top receipt active previous generation
  validate_slug "$slug"
  validate_project_storage "$slug"
  validate_sha "$sha"
  product_top="$(absolute_dir "$product")"
  [[ -f "$(maintenance_file_for "$product_top")" &&
     ! -L "$(maintenance_file_for "$product_top")" ]] ||
    die "activation requires MAINTENANCE published by factory-kit pause"
  [[ "$(strict_product_pin "$product_top")" == "$sha" ]] ||
    die "product pin does not match candidate SHA"
  active="$(active_file_for "$slug")"
  previous=""
  generation="$(next_generation "$slug" "$active")"
  if [[ -f "$active" ]]; then
    [[ "$(json_get "$active" product_path)" == "$product_top" ]] ||
      die "project activation record belongs to a different product path"
    previous="$(json_get "$active" generation)"
  fi
  receipt="$(resolve_receipt "$requested_receipt" "$slug" "$sha")"
  validate_receipt "$receipt" "$slug" "$product_top" "$sha" "$previous"
  [[ ! -e "$CONSUMED_DIR/$(json_get "$receipt" receipt_id).json" ]] ||
    die "certification receipt has already been consumed"
  printf '%s\t%s\t%s\t%s\n' "$receipt" "$generation" "$previous" "$product_top"
}

cmd_plan() {
  local slug="$1" product="$2" sha="$3" receipt="$4" values
  values="$(plan_activation "$slug" "$product" "$sha" "$receipt")"
  say "PLAN OK: project=$slug sha=$sha generation=$(printf '%s' "$values" | awk -F'\t' '{print $2}')"
  say "No files were changed."
}

create_journal() {
  local journal="$1" active="$2" slug="$3" sha="$4" receipt="$5"
  local product="$6" generation="$7" previous="$8" transaction="$9"
  local release tree contract receipt_id receipt_hash
  release="$RELEASES_DIR/$sha"
  tree="$(json_get "$receipt" kit_tree)"
  contract="$(json_get "$receipt" contract_version)"
  receipt_id="$(json_get "$receipt" receipt_id)"
  receipt_hash="$(file_hash "$receipt")"
  python3 - "$active" "$slug" "$sha" "$tree" "$receipt_id" \
    "$(json_get "$receipt" product_tree)" "$contract" "$previous" \
    "$generation" "$product" "$release" "$(now_iso)" "$receipt" \
    "$receipt_hash" "$transaction" <<'PY' | atomic_json_from_stdin "$journal"
import json, os, sys
(active_path, slug, sha, tree, receipt_id, product_tree, contract,
 previous, generation, product_path, release_path, timestamp, receipt_path,
 receipt_hash, transaction) = sys.argv[1:]
previous_record = None
if os.path.isfile(active_path):
    previous_record = json.load(open(active_path))
receipt_snapshot = json.load(open(receipt_path))
candidate = {
    "generation": int(generation),
    "project": slug,
    "kit_sha": sha,
    "kit_tree": tree,
    "receipt_id": receipt_id,
    "product_tree": product_tree,
    "contract_version": contract,
    "previous_generation": int(previous) if previous else None,
    "timestamp": timestamp,
    "product_path": product_path,
    "release_path": release_path,
}
journal = {
    "schema_version": 1,
    "transaction_id": transaction,
    "project": slug,
    "generation": int(generation),
    "phase": "prepared",
    "phase_history": ["prepared"],
    "created_at": timestamp,
    "updated_at": timestamp,
    "previous_record": previous_record,
    "candidate_record": candidate,
    "receipt_hash": receipt_hash,
    "receipt_snapshot": receipt_snapshot,
}
print(json.dumps(journal))
PY
}

cmd_activate() {
  local slug="$1" product="$2" sha="$3" requested_receipt="$4"
  local receipt generation previous product_top project_dir journal_dir journal
  local active project_lock launch_lock phase snapshot receipt_id transaction open
  validate_slug "$slug"
  validate_sha "$sha"
  product_top="$(absolute_dir "$product")"
  ensure_managed_directories "$slug"
  project_dir="$PROJECTS_DIR/$slug"
  journal_dir="$(journal_dir_for "$slug")"
  active="$(active_file_for "$slug")"
  project_lock="$project_dir/.activation.lock"
  acquire_lock "$project_lock" "project activation"
  if [[ "${FACTORY_KIT_TEST_HOLD_PROJECT_LOCK_SECONDS:-0}" != "0" ]]; then
    sleep "$FACTORY_KIT_TEST_HOLD_PROJECT_LOCK_SECONDS"
  fi
  validate_managed_layout "$slug"
  [[ ! -L "$active" ]] || die "active record may not be a symlink"
  open="$(latest_open_journal "$journal_dir")" ||
    die "activation journal is invalid"
  if [[ -n "$open" ]]; then
    die "project has an interrupted activation; run reconcile"
  fi
  [[ "$(strict_product_pin "$product_top")" == "$sha" ]] ||
    die "product pin does not match candidate SHA"
  previous=""
  if [[ -f "$active" ]]; then
    [[ "$(json_get "$active" product_path)" == "$product_top" ]] ||
      die "project activation record belongs to a different product path"
    previous="$(json_get "$active" generation)"
  fi
  generation="$(next_generation "$slug" "$active")"
  receipt="$(resolve_receipt "$requested_receipt" "$slug" "$sha")"
  receipt_id="$(json_get "$receipt" receipt_id)"
  [[ "$(basename "$receipt")" == "$receipt_id.json" ]] ||
    die "receipt filename does not match receipt ID"
  snapshot="$project_dir/.receipt-snapshot-$receipt_id-$(random_nonce).json"
  copy_receipt_snapshot "$receipt" "$snapshot"
  remember_temp "$snapshot"
  validate_receipt_snapshot "$snapshot" "$slug" "$product_top" "$sha" "$previous" "$receipt_id"
  [[ ! -e "$CONSUMED_DIR/$receipt_id.json" && ! -L "$CONSUMED_DIR/$receipt_id.json" ]] ||
    die "certification receipt has already been consumed"
  launch_lock="$product_top/factory/.launch.lock"
  acquire_lock "$launch_lock" "product launch"
  require_maintenance_after_lock "$slug" "$product_top"
  if has_active_runs "$product_top"; then
    die "product acquired launch lock with active runs"
  fi
  require_dispatch_drained "$product_top"
  validate_ticket_leases "$product_top" "$sha" "$(json_get "$snapshot" product_origin)"
  validate_receipt_snapshot "$snapshot" "$slug" "$product_top" "$sha" "$previous" "$receipt_id"
  transaction="$(printf '%020d-%s-%s' "$generation" "$sha" "$receipt_id")"
  journal="$journal_dir/$(printf '%020d' "$generation")-$sha.json"
  [[ ! -e "$journal" && ! -L "$journal" ]] || die "activation journal collision"
  create_journal "$journal" "$active" "$slug" "$sha" "$snapshot" \
    "$product_top" "$generation" "$previous" "$transaction"
  if [[ "${FACTORY_KIT_FAIL_AFTER_PHASE:-}" == "prepared" ]]; then
    die "injected failure after phase prepared"
  fi
  claim_receipt "$snapshot" "$transaction" ||
    die "certification receipt has already been consumed"
  set_journal_phase "$journal" receipt_claimed
  for phase in maintenance_published launch_drained services_stopped; do
    set_journal_phase "$journal" "$phase"
  done
  switch_active_from_journal "$journal" "$active" candidate_record
  set_journal_phase "$journal" activation_record_switched
  for phase in integration_bundle_switched services_started; do
    set_journal_phase "$journal" "$phase"
  done
  validate_receipt_snapshot "$snapshot" "$slug" "$product_top" "$sha" "$previous" "$receipt_id"
  set_journal_phase "$journal" healthy
  set_journal_phase "$journal" committed
  rm -f "$snapshot"
  release_lock "$launch_lock"
  release_lock "$project_lock"
  say "ACTIVATE OK: project=$slug generation=$generation sha=$sha"
}

infer_product_path() {
  local requested="$1" active="$2" journal="$3" value="" requested_top
  if [[ -n "$journal" && -f "$journal" ]]; then
    value="$(json_get "$journal" candidate_record.product_path 2>/dev/null || true)"
  fi
  if [[ -z "$value" && -f "$active" ]]; then
    value="$(json_get "$active" product_path 2>/dev/null || true)"
  fi
  if [[ -n "$requested" ]]; then
    requested_top="$(absolute_dir "$requested")"
    [[ -z "$value" || "$value" == "$requested_top" ]] ||
      die "stored activation state belongs to a different product path"
    printf '%s\n' "$requested_top"
    return
  fi
  [[ -n "$value" ]] || die "--product is required because no stored product path exists"
  absolute_dir "$value"
}

rollback_journal() {
  local journal="$1" active="$2"
  if ! remove_active_if_no_previous "$journal" "$active"; then
    switch_active_from_journal "$journal" "$active" previous_record
  fi
  set_journal_phase "$journal" rolled_back
}

active_matches_journal_record() {
  local journal="$1" active="$2" which="$3"
  python3 - "$journal" "$active" "$which" <<'PY'
import json, pathlib, sys
journal, active, which = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]), sys.argv[3]
expected = json.loads(journal.read_text())[which]
if expected is None:
    raise SystemExit(0 if not active.exists() else 1)
if active.is_symlink() or not active.is_file():
    raise SystemExit(1)
raise SystemExit(0 if json.loads(active.read_text()) == expected else 1)
PY
}

cmd_reconcile() {
  local slug="$1" product="$2" project_dir journal_dir journal active product_top
  local project_lock launch_lock phase sha receipt_id snapshot previous transaction
  local claim pre_pointer=0 valid=0
  validate_slug "$slug"
  ensure_managed_directories "$slug"
  project_dir="$PROJECTS_DIR/$slug"
  journal_dir="$(journal_dir_for "$slug")"
  active="$(active_file_for "$slug")"
  project_lock="$project_dir/.activation.lock"
  acquire_lock "$project_lock" "project activation"
  validate_managed_layout "$slug"
  [[ ! -L "$active" ]] || die "active record may not be a symlink"
  journal="$(latest_open_journal "$journal_dir")" ||
    die "activation journal is invalid"
  if [[ -z "$journal" ]]; then
    release_lock "$project_lock"
    say "RECONCILE OK: no interrupted transaction"
    return
  fi
  [[ -f "$journal" && ! -L "$journal" ]] || die "activation journal is unsafe"
  product_top="$(infer_product_path "$product" "$active" "$journal")"
  launch_lock="$product_top/factory/.launch.lock"
  acquire_lock "$launch_lock" "product launch"
  require_maintenance_after_lock "$slug" "$product_top"
  if has_active_runs "$product_top"; then
    die "product has active runs"
  fi
  require_dispatch_drained "$product_top"
  phase="$(json_get "$journal" phase)"
  sha="$(json_get "$journal" candidate_record.kit_sha)"
  receipt_id="$(json_get "$journal" candidate_record.receipt_id)"
  transaction="$(json_get "$journal" transaction_id)"
  previous="$(json_get "$journal" candidate_record.previous_generation)"
  snapshot="$project_dir/.reconcile-receipt-$receipt_id-$(random_nonce).json"
  snapshot_receipt_from_journal "$journal" "$snapshot"
  remember_temp "$snapshot"
  case "$phase" in
    prepared|receipt_claimed|maintenance_published|launch_drained|services_stopped)
      pre_pointer=1
      ;;
    activation_record_switched|integration_bundle_switched|services_started|healthy)
      pre_pointer=0
      ;;
    *) die "unknown activation journal phase: $phase" ;;
  esac
  if (trap - EXIT; HELD_LOCKS=""; \
      validate_receipt_snapshot "$snapshot" "$slug" "$product_top" "$sha" \
        "$previous" "$receipt_id" &&
      validate_ticket_leases "$product_top" "$sha" \
        "$(json_get "$snapshot" product_origin)" &&
      { [[ "$pre_pointer" == "0" ]] ||
        active_matches_journal_record "$journal" "$active" previous_record; } &&
      { [[ "$pre_pointer" == "1" ]] ||
        active_matches_journal_record "$journal" "$active" candidate_record; }); then
    valid=1
  fi
  if [[ "$valid" == "1" ]]; then
    claim="$CONSUMED_DIR/$receipt_id.json"
    if [[ "$phase" == "prepared" ]]; then
      if [[ -e "$claim" || -L "$claim" ]]; then
        require_receipt_consumption "$receipt_id" "$transaction"
      else
        claim_receipt "$snapshot" "$transaction" ||
          die "could not recover certification receipt claim"
      fi
      set_journal_phase "$journal" receipt_claimed
    else
      require_receipt_consumption "$receipt_id" "$transaction"
    fi
    if [[ "$pre_pointer" == "1" ]]; then
      set_journal_phase "$journal" maintenance_published
      set_journal_phase "$journal" launch_drained
      set_journal_phase "$journal" services_stopped
      switch_active_from_journal "$journal" "$active" candidate_record
      set_journal_phase "$journal" activation_record_switched
    fi
    set_journal_phase "$journal" integration_bundle_switched
    set_journal_phase "$journal" services_started
    set_journal_phase "$journal" healthy
    set_journal_phase "$journal" committed
    say "RECONCILE OK: committed interrupted activation"
  else
    rollback_journal "$journal" "$active"
    say "RECONCILE OK: restored previous generation"
  fi
  rm -f "$snapshot"
  release_lock "$launch_lock"
  release_lock "$project_lock"
}

find_committed_journal_for_generation() {
  local directory="$1" generation="$2"
  python3 - "$directory" "$generation" <<'PY'
import json, pathlib, sys
matches = []
for path in pathlib.Path(sys.argv[1]).glob("*.json"):
    try:
        value = json.loads(path.read_text())
        if int(value.get("generation", -1)) == int(sys.argv[2]):
            matches.append(str(path))
    except Exception:
        continue
if not matches:
    raise SystemExit(1)
print(sorted(matches)[-1])
PY
}

cmd_rollback() {
  local slug="$1" product="$2" active journal_dir generation journal product_top
  local project_lock launch_lock previous_sha previous_tree previous_product_tree open
  validate_slug "$slug"
  ensure_managed_directories "$slug"
  active="$(active_file_for "$slug")"
  journal_dir="$(journal_dir_for "$slug")"
  project_lock="$PROJECTS_DIR/$slug/.activation.lock"
  acquire_lock "$project_lock" "project activation"
  validate_managed_layout "$slug"
  [[ -f "$active" && ! -L "$active" ]] || die "project has no safe active generation"
  open="$(latest_open_journal "$journal_dir")" ||
    die "activation journal is invalid"
  [[ -z "$open" ]] ||
    die "project has an interrupted transaction; run reconcile first"
  generation="$(json_get "$active" generation)"
  journal="$(find_committed_journal_for_generation "$journal_dir" "$generation" || true)"
  [[ -n "$journal" && -f "$journal" && ! -L "$journal" ]] ||
    die "activation journal for generation $generation is missing or unsafe"
  [[ -n "$(json_get "$journal" previous_record)" ]] ||
    die "active generation has no previous generation"
  product_top="$(infer_product_path "$product" "$active" "$journal")"
  previous_sha="$(json_get "$journal" previous_record.kit_sha)"
  previous_tree="$(json_get "$journal" previous_record.kit_tree)"
  previous_product_tree="$(json_get "$journal" previous_record.product_tree)"
  validate_sha "$previous_sha"
  [[ "$(printf '%s' "$(verify_release_from_manifest "$previous_sha")" | awk -F'\t' '{print $1}')" == "$previous_tree" ]] ||
    die "previous release does not match its trusted install manifest"

  launch_lock="$product_top/factory/.launch.lock"
  acquire_lock "$launch_lock" "product launch"
  require_maintenance_after_lock "$slug" "$product_top"
  if has_active_runs "$product_top"; then
    die "product has active runs"
  fi
  require_dispatch_drained "$product_top"
  [[ "$(strict_product_pin "$product_top")" == "$previous_sha" ]] ||
    die "rollback requires product KIT_PIN already restored to previous SHA"
  require_clean_product "$product_top"
  [[ "$(product_tree "$product_top")" == "$previous_product_tree" ]] ||
    die "rollback requires product Git tree already restored to previous tree"
  validate_ticket_leases "$product_top" "$previous_sha" \
    "$(json_get "$journal" receipt_snapshot.product_origin)"
  switch_active_from_journal "$journal" "$active" previous_record
  set_journal_phase "$journal" rolled_back
  release_lock "$launch_lock"
  release_lock "$project_lock"
  say "ROLLBACK OK: project=$slug restored_sha=$previous_sha; MAINTENANCE remains"
}

cmd_recover_lease() {
  local slug="$1" product="$2" ticket="$3" product_top launch_lock lease_lock lease_file
  validate_slug "$slug"
  [[ "$ticket" =~ ^T-[0-9]+$ ]] || die "invalid ticket identifier"
  validate_managed_layout "$slug"
  product_top="$(absolute_dir "$product")"
  launch_lock="$product_top/factory/.launch.lock"
  acquire_lock "$launch_lock" "product launch"
  require_maintenance_after_lock "$slug" "$product_top"
  # ponytail: block recovery on any run; narrow to ticket identity only if recovery throughput matters.
  has_active_runs "$product_top" && die "product has active runs"
  lease_lock="$(factory_dispatch_lock_dir "$product_top")"
  acquire_lock "$lease_lock" "dispatcher lease"
  lease_file="$(factory_dispatch_lease_file "$product_top" "$ticket")"
  python3 - "$lease_file" "$ticket" <<'PY' || die "dispatcher lease is missing or unsafe"
import json, pathlib, stat, sys
path, ticket = pathlib.Path(sys.argv[1]), sys.argv[2]
value = path.lstat()
if path.is_symlink() or not stat.S_ISREG(value.st_mode):
    raise SystemExit(1)
record = json.loads(path.read_text())
if record.get("schema_version") != 1 or record.get("ticket") != ticket:
    raise SystemExit(1)
path.unlink()
PY
  release_lock "$lease_lock"
  release_lock "$launch_lock"
  say "RECOVER LEASE OK: project=$slug ticket=$ticket; MAINTENANCE remains"
}

cmd_status() {
  local slug="$1" product="$2" json="$3" active journal_dir open product_top=""
  validate_slug "$slug"
  validate_project_storage "$slug"
  active="$(active_file_for "$slug")"
  journal_dir="$(journal_dir_for "$slug")"
  [[ ! -L "$active" && ! -L "$journal_dir" ]] ||
    die "project state contains a symlink"
  open="$(latest_open_journal "$journal_dir")" ||
    die "activation journal is invalid"
  if [[ -n "$product" ]]; then
    product_top="$(absolute_dir "$product")"
  elif [[ -f "$active" ]]; then
    product_top="$(json_get "$active" product_path 2>/dev/null || true)"
  fi
  if [[ -f "$active" ]]; then
    verify_release_from_manifest "$(json_get "$active" kit_sha)" >/dev/null
    if [[ -n "$product_top" ]]; then
      product_top="$(absolute_dir "$product_top")"
      [[ "$(strict_product_pin "$product_top")" == "$(json_get "$active" kit_sha)" ]] ||
        die "active runtime tuple has a mismatched product pin"
      [[ "$(product_tree "$product_top")" == "$(json_get "$active" product_tree)" ]] ||
        die "active runtime tuple has a mismatched product tree"
    fi
  fi
  if [[ "$json" == "1" ]]; then
    python3 - "$slug" "$active" "$open" "$product_top" <<'PY'
import json, os, sys
slug, active_path, journal_path, product = sys.argv[1:]
active = json.load(open(active_path)) if os.path.isfile(active_path) else None
journal = json.load(open(journal_path)) if journal_path and os.path.isfile(journal_path) else None
maintenance = bool(product and os.path.isfile(os.path.join(product, "factory", "MAINTENANCE")))
print(json.dumps({
    "project": slug,
    "active": active,
    "interrupted_transaction": journal,
    "maintenance": maintenance,
}, sort_keys=True, separators=(",", ":")))
PY
  else
    if [[ -f "$active" ]]; then
      say "ACTIVE: project=$slug generation=$(json_get "$active" generation) sha=$(json_get "$active" kit_sha)"
    else
      say "ACTIVE: project=$slug none"
    fi
    if [[ -n "$open" ]]; then
      say "JOURNAL: interrupted phase=$(json_get "$open" phase)"
    else
      say "JOURNAL: clean"
    fi
    if [[ -n "$product_top" && -f "$product_top/factory/MAINTENANCE" ]]; then
      say "MAINTENANCE: present"
    else
      say "MAINTENANCE: absent"
    fi
  fi
}

require_command git
require_command python3
require_command shasum
require_command tar
validate_test_mode
validate_managed_roots

[[ $# -gt 0 ]] || { usage >&2; exit 2; }
COMMAND="$1"
shift

SHA=""
REPO="$SCRIPT_ROOT"
ORIGIN_OVERRIDE=""
PROJECT=""
PRODUCT=""
RECEIPT=""
TICKET=""
JSON=0
POSITIONALS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --sha|--release) [[ $# -ge 2 ]] || die "$1 requires a value"; SHA="$2"; shift 2 ;;
    --repo|--source|--kit-repo) [[ $# -ge 2 ]] || die "$1 requires a value"; REPO="$2"; shift 2 ;;
    --origin) [[ $# -ge 2 ]] || die "$1 requires a value"; ORIGIN_OVERRIDE="$2"; shift 2 ;;
    --project|--slug) [[ $# -ge 2 ]] || die "$1 requires a value"; PROJECT="$2"; shift 2 ;;
    --product|--product-repo) [[ $# -ge 2 ]] || die "$1 requires a value"; PRODUCT="$2"; shift 2 ;;
    --receipt) [[ $# -ge 2 ]] || die "$1 requires a value"; RECEIPT="$2"; shift 2 ;;
    --ticket) [[ $# -ge 2 ]] || die "$1 requires a value"; TICKET="$2"; shift 2 ;;
    --json) JSON=1; shift ;;
    --help|-h) usage; exit 0 ;;
    --*) die "unknown option: $1" ;;
    *) POSITIONALS[${#POSITIONALS[@]}]="$1"; shift ;;
  esac
done

case "$COMMAND" in
  install)
    [[ -n "$SHA" ]] || SHA="${POSITIONALS[0]:-}"
    [[ "$REPO" != "$SCRIPT_ROOT" || -z "${POSITIONALS[1]:-}" ]] || REPO="${POSITIONALS[1]}"
    [[ -n "$SHA" ]] || { usage >&2; exit 2; }
    cmd_install "$SHA" "$REPO" "$ORIGIN_OVERRIDE"
    ;;
  certify)
    [[ -n "$PROJECT" ]] || PROJECT="${POSITIONALS[0]:-}"
    [[ -n "$PRODUCT" ]] || PRODUCT="${POSITIONALS[1]:-}"
    [[ -n "$SHA" ]] || SHA="${POSITIONALS[2]:-}"
    [[ -n "$PROJECT" && -n "$PRODUCT" && -n "$SHA" ]] || { usage >&2; exit 2; }
    cmd_certify "$PROJECT" "$PRODUCT" "$SHA"
    ;;
  plan)
    [[ -n "$PROJECT" ]] || PROJECT="${POSITIONALS[0]:-}"
    [[ -n "$PRODUCT" ]] || PRODUCT="${POSITIONALS[1]:-}"
    [[ -n "$SHA" ]] || SHA="${POSITIONALS[2]:-}"
    [[ -n "$PROJECT" && -n "$PRODUCT" && -n "$SHA" ]] || { usage >&2; exit 2; }
    cmd_plan "$PROJECT" "$PRODUCT" "$SHA" "$RECEIPT"
    ;;
  pause)
    [[ -n "$PROJECT" ]] || PROJECT="${POSITIONALS[0]:-}"
    [[ -n "$PRODUCT" ]] || PRODUCT="${POSITIONALS[1]:-}"
    [[ -n "$PROJECT" && -n "$PRODUCT" ]] || { usage >&2; exit 2; }
    cmd_pause "$PROJECT" "$PRODUCT"
    ;;
  activate)
    [[ -n "$PROJECT" ]] || PROJECT="${POSITIONALS[0]:-}"
    [[ -n "$PRODUCT" ]] || PRODUCT="${POSITIONALS[1]:-}"
    [[ -n "$SHA" ]] || SHA="${POSITIONALS[2]:-}"
    [[ -n "$PROJECT" && -n "$PRODUCT" && -n "$SHA" ]] || { usage >&2; exit 2; }
    cmd_activate "$PROJECT" "$PRODUCT" "$SHA" "$RECEIPT"
    ;;
  status)
    [[ -n "$PROJECT" ]] || PROJECT="${POSITIONALS[0]:-}"
    [[ -n "$PRODUCT" ]] || PRODUCT="${POSITIONALS[1]:-}"
    [[ -n "$PROJECT" ]] || { usage >&2; exit 2; }
    cmd_status "$PROJECT" "$PRODUCT" "$JSON"
    ;;
  reconcile)
    [[ -n "$PROJECT" ]] || PROJECT="${POSITIONALS[0]:-}"
    [[ -n "$PRODUCT" ]] || PRODUCT="${POSITIONALS[1]:-}"
    [[ -n "$PROJECT" ]] || { usage >&2; exit 2; }
    cmd_reconcile "$PROJECT" "$PRODUCT"
    ;;
  rollback)
    [[ -n "$PROJECT" ]] || PROJECT="${POSITIONALS[0]:-}"
    [[ -n "$PRODUCT" ]] || PRODUCT="${POSITIONALS[1]:-}"
    [[ -n "$PROJECT" ]] || { usage >&2; exit 2; }
    cmd_rollback "$PROJECT" "$PRODUCT"
    ;;
  recover-lease)
    [[ -n "$PROJECT" ]] || PROJECT="${POSITIONALS[0]:-}"
    [[ -n "$PRODUCT" ]] || PRODUCT="${POSITIONALS[1]:-}"
    [[ -n "$TICKET" ]] || TICKET="${POSITIONALS[2]:-}"
    [[ -n "$PROJECT" && -n "$PRODUCT" && -n "$TICKET" ]] || { usage >&2; exit 2; }
    cmd_recover_lease "$PROJECT" "$PRODUCT" "$TICKET"
    ;;
  prune) die "automatic prune is intentionally not implemented" ;;
  *) usage >&2; die "unknown command: $COMMAND" ;;
esac
