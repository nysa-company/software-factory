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
RECEIPT_SCHEMA=1
INSTALL_MANIFEST_SCHEMA=1
CERTIFICATION_TOOL_VERSION=1
DEFAULT_RECEIPT_TTL="${FACTORY_KIT_RECEIPT_TTL_SECONDS:-86400}"

HELD_LOCKS=""
TEMP_PATHS=""
PREPARED_COPY=""
PREPARED_PRODUCT=""
ISOLATED_HOME=""

say() { printf '%s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

cleanup() {
  local status=$? item path nonce
  trap - EXIT
  if [[ -n "$TEMP_PATHS" ]]; then
    printf '%s' "$TEMP_PATHS" | while IFS= read -r item; do
      [[ -z "$item" ]] || rm -rf "$item"
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
  TEMP_PATHS="${1}
${TEMP_PATHS}"
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
    [[ -d "$lock" && ! -L "$lock" ]] || die "$label lock path is unsafe"
    [[ ! -L "$lock/owner" ]] || die "$label lock owner is unsafe"
    owner_pid="$(awk -F= '$1=="pid" {print $2; exit}' "$lock/owner" 2>/dev/null || true)"
    owner_start="$(awk -F= '$1=="process_start" {print substr($0,index($0,"=")+1); exit}' "$lock/owner" 2>/dev/null || true)"
    owner_nonce="$(awk -F= '$1=="nonce" {print $2; exit}' "$lock/owner" 2>/dev/null || true)"
    if [[ -f "$lock/owner" ]]; then
      owner_hash="$(file_hash "$lock/owner")"
    else
      owner_hash="missing"
    fi
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
    --work-tree="$directory" add -A -- .
  tree="$(GIT_INDEX_FILE="$index" git --git-dir="$object_dir/repo.git" \
    --work-tree="$directory" write-tree)"
  rm -rf "$object_dir"
  printf '%s\n' "$tree"
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

run_kit_checks_isolated() {
  local checkout="$1" home="$2" scratch="$3" workspace="$4" phase="$5"
  shift 5
  local status=0
  local raw="$scratch/kit-checks.raw" redacted="$scratch/kit-checks.redacted"
  configure_phase_sandbox "$phase" "$workspace" "$@"
  python3 - "$checkout" "$home" "$scratch" "$raw" \
    "$SANDBOX_EXEC" "$SANDBOX_PROFILE" "${FACTORY_FIXTURE_DIRTY:-0}" \
    "${FACTORY_KIT_SANDBOX_CAPTURE:-}" \
    "${FACTORY_KIT_SANDBOX_DENY_SIBLING:-}" \
    "${FACTORY_KIT_SANDBOX_DENY_HOME:-}" <<'PY' || status=$?
import os, pathlib, subprocess, sys
(
    checkout, home, scratch, output, sandbox_exec, profile, dirty, capture,
    deny_sibling, deny_home,
) = sys.argv[1:]
root = pathlib.Path(checkout)
prefix = [sandbox_exec, "-f", profile] if profile else []
environment = {
    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    "HOME": home,
    "TMPDIR": scratch,
    "XDG_CACHE_HOME": os.path.join(scratch, "cache"),
    "npm_config_cache": os.path.join(scratch, "npm"),
}
if dirty == "1":
    environment["FACTORY_FIXTURE_DIRTY"] = "1"
if capture:
    environment["FACTORY_KIT_SANDBOX_CAPTURE"] = capture
if deny_sibling:
    environment["FACTORY_KIT_SANDBOX_DENY_SIBLING"] = deny_sibling
if deny_home:
    environment["FACTORY_KIT_SANDBOX_DENY_HOME"] = deny_home
commands = []
if (root / "ci/test-all.sh").is_file():
    commands.append(["bash", "ci/test-all.sh"])
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
  local origin
  origin="$(git -C "$1" remote get-url origin 2>/dev/null || true)"
  [[ -n "$origin" ]] || die "product repository has no origin remote"
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
    "/private/etc",
    "/private/var/db/timezone",
    "/private/var/select",
    "/Library/Apple",
    "/Library/Developer",
    "/Applications/Xcode.app/Contents/Developer",
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
lines.append("(allow signal (target self))\n")
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
    "${FACTORY_KIT_SANDBOX_CAPTURE:-}" \
    "${FACTORY_KIT_SANDBOX_DENY_SIBLING:-}" \
    "${FACTORY_KIT_SANDBOX_DENY_HOME:-}" <<'PY' || status=$?
import os, subprocess, sys
product, script, sha, release, home, scratch, timeout, output = sys.argv[1:9]
profile = sys.argv[9]
sandbox_exec = sys.argv[10]
capture = sys.argv[11]
deny_sibling = sys.argv[12]
deny_home = sys.argv[13]
prefix = [sandbox_exec, "-f", profile] if profile else []
environment = {
    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    "HOME": home,
    "TMPDIR": scratch,
    "XDG_CACHE_HOME": os.path.join(scratch, "cache"),
    "npm_config_cache": os.path.join(scratch, "npm"),
    "FACTORY_KIT_SHA": sha,
    "FACTORY_KIT_RELEASE": release,
    "FACTORY_PRODUCT_ROOT": product,
}
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
  for file in "$product/factory/.active-runs/"*.pid "$product/factory/runs/"*.pid; do
    [[ -e "$file" ]] && return 0
  done
  return 1
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
  local product="$1" sha="$2"
  python3 - "$product/factory/tickets" "$sha" <<'PY'
import pathlib, re, sys
tickets, candidate = pathlib.Path(sys.argv[1]), sys.argv[2]
if not tickets.is_dir():
    raise SystemExit(0)
for path in sorted(tickets.glob("*.md")):
    if path.is_symlink():
        raise SystemExit("ticket path is a symlink: %s" % path)
    text = path.read_text(errors="replace")
    states = re.findall(r"(?mi)^State:\s*(.*?)\s*$", text)
    leases = re.findall(r"(?mi)^Kit-SHA:\s*(.*?)\s*$", text)
    if len(states) != 1:
        raise SystemExit("%s must contain exactly one State field" % path.name)
    if len(leases) > 1:
        raise SystemExit("%s contains duplicate Kit-SHA fields" % path.name)
    state = states[0].strip()
    lease = leases[0].strip() if leases else ""
    if lease and not re.fullmatch(r"[0-9a-f]{40}", lease):
        raise SystemExit("%s has a noncanonical Kit-SHA" % path.name)
    if state.lower() == "done":
        continue
    if lease:
        if lease != candidate:
            raise SystemExit("%s is nonterminal and leased to a different kit" % path.name)
    elif state.lower() not in ("ready", "backlog"):
        raise SystemExit("%s is in progress without a Kit-SHA lease" % path.name)
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
  run_kit_checks_isolated "$checkout" "$workspace/home" "$workspace/tmp" \
    "$workspace" "install" "$source_top" ||
    die "kit checks failed in disposable checkout"
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
  git -C "$source_top" archive --format=tar "$sha" | (cd "$temp" && tar -xf -)
  verify_symlinks_contained "$temp" || die "candidate contains an escaping symlink"
  [[ "$(git_tree_for_directory "$temp")" == "$kit_tree" ]] ||
    die "materialized release does not match Git tree"
  chmod -R a-w "$temp"
  verify_read_only "$temp" || die "failed to seal release read-only"
  mv "$temp" "$release"
  TEMP_PATHS="$(printf '%s' "$TEMP_PATHS" | awk -v p="$temp" '$0 != p')"
  verify_release "$sha" "$kit_tree"
  write_install_manifest "$sha" "$origin_identity" "$kit_tree" "$release"
  verify_release_from_manifest "$sha" >/dev/null
  release_lock "$lock"
  say "INSTALL OK: $sha ($origin)"
}

cmd_certify() {
  local slug="$1" product="$2" sha="$3"
  local product_top release kit_tree pin product_git_tree product_repo contract manifest_values
  local writable script created expires receipt_id receipt previous_generation workspace
  local kit_pin_hash project_env_hash kit_origin
  validate_slug "$slug"
  validate_project_storage "$slug"
  validate_sha "$sha"
  ensure_managed_directories "$slug"
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
  prepare_writable_product_copy "$product_top" "$workspace"
  script="$(certify_script_path "$PREPARED_PRODUCT")" ||
    die "invalid product certification contract"
  run_kit_checks_isolated "$writable" "$ISOLATED_HOME" "$workspace/tmp" \
    "$workspace" "certification" "$product_top" "$release" ||
    die "kit certification checks failed"
  git -C "$writable" diff --quiet &&
    git -C "$writable" diff --cached --quiet ||
    die "kit certification checks modified the tracked candidate tree"
  run_product_certification "$PREPARED_PRODUCT" "$script" "$sha" "$writable" \
    "$workspace" "$product_top" "$release" ||
    die "product certification failed"
  verify_release_from_manifest "$sha" >/dev/null
  require_clean_product "$product_top"
  [[ "$(product_tree "$product_top")" == "$product_git_tree" ]] ||
    die "product tree changed during certification"

  [[ "$DEFAULT_RECEIPT_TTL" =~ ^[0-9]+$ && "$DEFAULT_RECEIPT_TTL" -gt 0 ]] ||
    die "receipt TTL must be a positive integer"
  created="$(now_epoch)"
  expires=$((created + DEFAULT_RECEIPT_TTL))
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
    "$CERTIFICATION_TOOL_VERSION" <<'PY' | atomic_json_from_stdin "$receipt"
import json, sys, time
(slug, sha, kit_tree, kit_origin, product_path, product_origin, product_tree,
 kit_pin_hash, project_env_hash, contract, host, os_name, architecture,
 created, expires, receipt_id, previous_generation, tool_version) = sys.argv[1:]
value = {
    "schema_version": 1,
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
  validate_ticket_leases "$product_top" "$sha"
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
      validate_ticket_leases "$product_top" "$sha" &&
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
  [[ "$(strict_product_pin "$product_top")" == "$previous_sha" ]] ||
    die "rollback requires product KIT_PIN already restored to previous SHA"
  require_clean_product "$product_top"
  [[ "$(product_tree "$product_top")" == "$previous_product_tree" ]] ||
    die "rollback requires product Git tree already restored to previous tree"
  validate_ticket_leases "$product_top" "$previous_sha"
  switch_active_from_journal "$journal" "$active" previous_record
  set_journal_phase "$journal" rolled_back
  release_lock "$launch_lock"
  release_lock "$project_lock"
  say "ROLLBACK OK: project=$slug restored_sha=$previous_sha; MAINTENANCE remains"
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
  prune) die "automatic prune is intentionally not implemented" ;;
  *) usage >&2; die "unknown command: $COMMAND" ;;
esac
