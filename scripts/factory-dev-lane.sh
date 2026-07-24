#!/usr/bin/env bash
# Disposable, branch-local factory lifecycle. This is deliberately not wired
# into factory-kit, the installed launcher, a registry, or launchd.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
SOURCE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd -P)"
ACCOUNT_HOME="$(python3 - <<'PY'
import os, pwd
print(pwd.getpwuid(os.getuid()).pw_dir)
PY
)"
TICKET=T-900001
TICKETS=(T-900001 T-900002 T-900003 T-900004)
PRODUCT_SOURCE=""
PRODUCT_BASE=""
PRODUCT_SEED_BUNDLE=""
PRODUCT_SEED_ACCOUNTING=""
PRODUCT_SEED_LINEAGE=""
PRODUCT_SEED_CHECKPOINT=""
PRODUCT_TICKETS=()
ROLES=planner,spec-linter,test-author,builder,reviewer,narrator
TEST_MODE=0
if [[ "${FACTORY_DEV_LANE_TEST_MODE:-0}" == 1 &&
      "${FACTORY_TRUSTED_TEST_HARNESS:-0}" == 1 ]]; then
  TEST_MODE=1
  if [[ -n "${FACTORY_DEV_LANE_ACCOUNT_HOME:-}" ]]; then
    ACCOUNT_HOME="$(cd "$FACTORY_DEV_LANE_ACCOUNT_HOME" && pwd -P)"
  fi
fi

die() { echo "factory-dev-lane: $*" >&2; exit 1; }
usage() {
  cat >&2 <<'EOF'
usage: factory-dev-lane.sh mock [--keep]
       factory-dev-lane.sh mock-concurrency [--keep]
       factory-dev-lane.sh cursor-plan
       factory-dev-lane.sh cursor-run --root <absolute-lane-root> --approve-hash <sha256>
       factory-dev-lane.sh subscription-plan [--adapter codex|claude]
       factory-dev-lane.sh subscription-run --root <absolute-lane-root> --approve-hash <sha256>
       factory-dev-lane.sh product-seed-lineage --accounting <absolute-json> --output <absolute-json> [--parent-accounting <absolute-json>]
       factory-dev-lane.sh product-plan --source <absolute-repo> --base-sha <full-sha> --tickets <one-to-four-T-NNN,...> [--seed-bundle <absolute-bundle> --seed-accounting <absolute-json> --seed-lineage <absolute-json> --seed-checkpoint <absolute-json>]
       factory-dev-lane.sh product-resume-plan --root <absolute-lane-root> --tickets <T-NNN,...>
       factory-dev-lane.sh product-run --root <absolute-lane-root> --approve-hash <sha256>
       factory-dev-lane.sh product-export --root <absolute-lane-root> [--tickets <T-NNN,...>] [--output <absolute-new-lane-local-directory>]
       factory-dev-lane.sh product-checkpoint-export --root <absolute-lane-root> --tickets <T-NNN,...> --output <absolute-new-directory>
       factory-dev-lane.sh clean --root <absolute-lane-root>
EOF
  exit 2
}

physical() { (cd "$1" 2>/dev/null && pwd -P); }
sha256_file() { shasum -a 256 "$1" | awk '{print $1}'; }
sha256_text() { shasum -a 256 | awk '{print $1}'; }

cursor_approval_hash() {
  local root="$1" version="$2" route_plan cursor session_home
  route_plan="$root/worktrees/$TICKET/factory/route-plans/$TICKET.json"
  cursor="$(python3 - "$root/home/agent" <<'PY'
import os, sys
print(os.path.realpath(sys.argv[1]))
PY
)"
  [[ "$cursor" == /* && -f "$cursor" && -x "$cursor" ]] ||
    die "Cursor binary binding is unavailable"
  session_home="$(cursor_session_home)"
  {
    python3 - "$root/marker.json" <<'PY'
import json, sys
print(json.load(open(sys.argv[1], encoding="utf-8"))["kit_sha"])
PY
    git -C "$root/kit" rev-parse 'HEAD^{tree}'
    git -C "$root/worktrees/$TICKET" rev-parse 'HEAD^{tree}'
    printf '%s\n' "$version" "$cursor" "$(sha256_file "$cursor")" \
      "$(sha256_file "$session_home/.cursor/auth.json")" \
      "$(sha256_file "$session_home/.cursor/cli-config.json")" \
      "$(sha256_file "$route_plan")" "$(basename "$root")"
  } | sha256_text
}

subscription_base_env() {
  local root="$1" project
  shift
  project="factory-dev-lane-$(basename "$root" | sed 's/^nysa-sf-dev\.//' | tr '[:upper:]' '[:lower:]')"
  (
    cd "$root"
    env -i HOME="$root/session-home" TMPDIR="$root/tmp" LANG=C LC_ALL=C \
      PATH="$root/home:/usr/bin:/bin:/usr/sbin:/sbin" \
      AGENT_CLI_CREDENTIAL_STORE=file \
      FACTORY_ROOT="$root/product" FACTORY_GLOBAL_ENV="$root/home/.factory/global.env" \
      FACTORY_MODEL_STATE_ROOT="$root/runtime/model-state" FACTORY_PROJECT="$project" \
      FACTORY_PROVIDER_DB="$root/runtime/provider-state.sqlite3" \
      FACTORY_PROVIDER_POLICY="$root/runtime/provider-policy.json" \
      FACTORY_PROVIDER_ACTIVATION="$root/runtime/provider-activation.json" \
      FACTORY_CURSOR_SESSION_HOME="$root/session-home" FACTORY_CURSOR_INTERNAL_SANDBOX=1 \
      FACTORY_CURSOR_REPEATED_TOOL_ERROR_LIMIT=2 \
      FACTORY_CLI_LANE_ROOT="$root" FACTORY_CLI_INTERNAL_SANDBOX=1 \
      FACTORY_CLAUDE_SETTINGS="$root/runtime/claude-settings.json" \
      FACTORY_CERTIFIED_PRODUCT_ORIGIN="$root/origin.git" \
      FACTORY_HERMES_CONTRACT_VERSION=1.7.0 "$@"
  )
}

subscription_ready() {
  local root="$1" i claude_ready=0
  for i in 1 2 3; do
    subscription_base_env "$root" "$root/home/timeout" 10 \
      "$root/home/agent" status >/dev/null 2>&1 && break
    [[ "$i" -lt 3 ]] || die "Cursor subscription authentication is unavailable"
    sleep 1
  done
  for i in 1 2 3; do
    subscription_base_env "$root" "$root/home/timeout" 10 \
      "$root/home/codex" login status >/dev/null 2>&1 && break
    [[ "$i" -lt 3 ]] || die "Codex subscription authentication is unavailable"
    sleep 1
  done
  for i in 1 2 3; do
    claude_subscription_probe "$root" && {
      claude_ready=1
      break
    }
    sleep 1
  done
  if [[ "$claude_ready" == 0 ]]; then
    echo "DEVELOPMENT_PROVIDER_FALLBACK=claude-code->cursor-anthropic" >&2
  fi
}

codex_subscription_ready() {
  local root="$1" i
  for i in 1 2 3; do
    subscription_base_env "$root" "$root/home/timeout" 10 \
      "$root/home/codex" login status >/dev/null 2>&1 && return
    [[ "$i" -lt 3 ]] || die "Codex subscription authentication is unavailable"
    sleep 1
  done
}

claude_subscription_ready() {
  local root="$1" i
  for i in 1 2 3; do
    claude_subscription_probe "$root" && return
    [[ "$i" -lt 3 ]] || die "Claude subscription authentication is unavailable"
    sleep 1
  done
}

claude_subscription_probe() {
  local root="$1"
  subscription_base_env "$root" bash -c '
    source "$1"
    factory_probe_adapter claude-code
    [[ "$PROBE_STATE" == READY ]]
  ' _ "$root/kit/scripts/lib/backend-policy.sh"
}

ensure_cursor_file_credential_config() {
  local config="$1/home/.factory/global.env"
  if grep -q '^AGENT_CLI_CREDENTIAL_STORE=' "$config"; then
    grep -qx 'AGENT_CLI_CREDENTIAL_STORE=file' "$config" ||
      die "lane Cursor credential store is not file-backed"
  else
    printf '%s\n' 'AGENT_CLI_CREDENTIAL_STORE=file' >>"$config"
  fi
}

subscription_approval_hash() {
  local root="$1" session_home real adapter tool credential
  session_home="$root/session-home"
  adapter="$(cat "$root/subscription-adapter")"
  case "$adapter" in
    codex) tool=codex; credential="$session_home/.codex/auth.json" ;;
    claude) tool=claude; credential="$session_home/.claude/.credentials.json" ;;
    *) return 1 ;;
  esac
  {
    python3 - "$root/marker.json" <<'PY'
import json, sys
value=json.load(open(sys.argv[1], encoding="utf-8"))
print(value["kit_sha"]); print(value["nonce"])
PY
    git -C "$root/kit" rev-parse 'HEAD^{tree}'
    git -C "$root/product" rev-parse 'HEAD^{tree}'
    printf '%s\n' "$adapter"
    real="$(python3 - "$root/home/$tool" <<'PY'
import os, sys
print(os.path.realpath(sys.argv[1]))
PY
)"
    printf '%s\n' "$real" "$(sha256_file "$real")" \
      "$(subscription_base_env "$root" "$root/home/$tool" --version 2>/dev/null | head -n1)"
    if [[ "$adapter" == codex ]]; then
      subscription_base_env "$root" "$root/home/codex" login status 2>/dev/null |
        sha256_text
    else
      subscription_base_env "$root" "$root/home/claude" auth status 2>/dev/null |
        sha256_text
    fi
    sha256_file "$root/runtime/provider-policy.json"
    sha256_file "$root/runtime/provider-activation.json"
    sha256_file "$root/home/record-provider-call"
    sha256_file "$root/home/.factory/global.env"
    sha256_file "$root/runtime/native.sb"
    sha256_file "$credential"
    [[ "$adapter" != claude ]] || sha256_file "$root/runtime/claude-settings.json"
  } | sha256_text
}

subscription_provider_idle() {
  python3 - <<'PY'
import subprocess

rows=subprocess.run(
    ["/bin/ps", "-axo", "command="], text=True, capture_output=True,
    check=True, timeout=10,
).stdout.splitlines()
for row in rows:
    words=row.strip().split()
    if not words:
        continue
    executable=words[0].rsplit("/", 1)[-1]
    if executable == "agent" and "--print" in words:
        raise SystemExit(1)
    if executable == "claude" and "-p" in words:
        raise SystemExit(1)
    if executable == "codex" and "exec" in words[1:]:
        raise SystemExit(1)
PY
}

cleanup_empty_cursor_bridge() {
  python3 - "$1" <<'PY'
import os, pathlib, stat, sys
root=pathlib.Path(sys.argv[1])
info=root.lstat()
if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or
    stat.S_IMODE(info.st_mode) & 0o022):
    raise SystemExit(1)
directories=[]
for path in root.rglob("*"):
    current=path.lstat()
    if (not stat.S_ISDIR(current.st_mode) or current.st_uid != os.getuid() or
        stat.S_IMODE(current.st_mode) & 0o022):
        raise SystemExit(1)
    directories.append(path)
for path in sorted(directories,key=lambda item:len(item.parts),reverse=True):
    path.rmdir()
root.rmdir()
PY
}

assert_macos() {
  local os
  if [[ "$TEST_MODE" -eq 1 ]]; then
    os="${FACTORY_DEV_LANE_UNAME:-$(uname -s)}"
  else
    [[ -z "${FACTORY_DEV_LANE_UNAME:-}" &&
       -z "${FACTORY_DEV_LANE_SANDBOX_EXEC:-}" &&
       -z "${FACTORY_DEV_LANE_CURSOR_BIN:-}" &&
       -z "${FACTORY_DEV_LANE_ACCOUNT_HOME:-}" ]] ||
      die "development-lane test overrides require the trusted test harness"
    os="$(uname -s)"
  fi
  [[ "$os" == Darwin ]] || die "v1 requires macOS Seatbelt"
}

sandbox_exec() {
  if [[ "$TEST_MODE" -eq 1 && -n "${FACTORY_DEV_LANE_SANDBOX_EXEC:-}" ]]; then
    printf '%s\n' "$FACTORY_DEV_LANE_SANDBOX_EXEC"
  else
    printf '%s\n' /usr/bin/sandbox-exec
  fi
}

cursor_bin() {
  local value resolved
  if [[ "$TEST_MODE" -eq 1 && -n "${FACTORY_DEV_LANE_CURSOR_BIN:-}" ]]; then
    value="$FACTORY_DEV_LANE_CURSOR_BIN"
  else
    value="$(command -v agent 2>/dev/null || true)"
  fi
  [[ "$value" == /* && -x "$value" ]] || die "Cursor agent binary is unavailable"
  refuse_production_path "$value"
  resolved="$(python3 - "$value" <<'PY'
import os, sys
print(os.path.realpath(sys.argv[1]))
PY
)"
  refuse_production_path "$resolved"
  printf '%s\n' "$resolved"
}

refuse_production_path() {
  local candidate="$1" lexical resolved forbidden
  lexical="$(python3 - "$candidate" <<'PY'
import os, sys
print(os.path.abspath(sys.argv[1]))
PY
)"
  resolved="$(python3 - "$candidate" <<'PY'
import os, sys
print(os.path.realpath(sys.argv[1]))
PY
)"
  for forbidden in \
    "$ACCOUNT_HOME/.factory" \
    "$ACCOUNT_HOME/.hermes/profiles/factory" \
    "$ACCOUNT_HOME/Library/LaunchAgents" \
    "$ACCOUNT_HOME/Projects/nysa-company/nysa-app"; do
    forbidden="$(python3 - "$forbidden" <<'PY'
import os, sys
print(os.path.realpath(sys.argv[1]))
PY
)"
    case "$lexical" in
      "$forbidden"|"$forbidden"/*) die "path overlaps protected production path: $forbidden" ;;
    esac
    case "$resolved" in
      "$forbidden"|"$forbidden"/*) die "path overlaps protected production path: $forbidden" ;;
    esac
  done
}

require_lane_path() {
  local root="$1" candidate="$2" lexical resolved
  lexical="$(python3 - "$candidate" <<'PY'
import os, sys
print(os.path.abspath(sys.argv[1]))
PY
)"
  resolved="$(python3 - "$candidate" <<'PY'
import os, sys
print(os.path.realpath(sys.argv[1]))
PY
)"
  refuse_production_path "$lexical"
  refuse_production_path "$resolved"
  case "$lexical" in "$root"|"$root"/*) ;; *) die "lane path escapes root: $lexical" ;; esac
  case "$resolved" in "$root"|"$root"/*) ;; *) die "lane path resolves outside root: $resolved" ;; esac
}

validate_runtime_paths() {
  local root="$1" path
  for path in \
    "$root/kit" "$root/product" "$root/origin.git" "$root/worktrees" \
    "$root/runtime" "$root/runtime/provider-state.sqlite3" \
    "$root/runtime/provider-policy.json" "$root/runtime/provider-attempts" \
    "$root/runtime/provider-locks" "$root/runtime/provider-inputs" \
    "$root/home" "$root/home/.factory" "$root/home/.hermes/profiles/factory-dev-$(basename "$root")" \
    "$root/tmp"; do
    require_lane_path "$root" "$path"
  done
}

validate_lane() {
  local root="$1" schema
  [[ "$root" == /* && -d "$root" && ! -L "$root" ]] ||
    die "lane root must be an existing absolute, non-symlink directory"
  root="$(physical "$root")"
  case "$(basename "$root")" in nysa-sf-dev.*) ;; *) die "refusing non-lane root" ;; esac
  schema="$(python3 - "$root/marker.json" "$root" <<'PY'
import json, os, stat, sys
try:
    root_info=os.lstat(sys.argv[2])
    marker_info=os.lstat(sys.argv[1])
    if not stat.S_ISDIR(root_info.st_mode) or stat.S_IMODE(root_info.st_mode) != 0o700:
        raise ValueError
    if root_info.st_uid != os.getuid(): raise ValueError
    if (not stat.S_ISREG(marker_info.st_mode) or marker_info.st_nlink != 1 or
        marker_info.st_uid != os.getuid() or stat.S_IMODE(marker_info.st_mode) != 0o600):
        raise ValueError
    v=json.load(open(sys.argv[1], encoding="utf-8"))
    if set(v) != {"schema","root","nonce","kit_sha","kit_tree","mode",
                  "uid","root_dev","root_ino","tmp_parent"}:
        raise ValueError
    if (v["root"] != sys.argv[2] or v["uid"] != os.getuid() or
        v["root_dev"] != root_info.st_dev or v["root_ino"] != root_info.st_ino or
        os.path.dirname(sys.argv[2]) != v["tmp_parent"]):
        raise ValueError
    print(v["schema"])
except Exception:
    raise SystemExit(1)
PY
  )" || die "lane ownership marker is malformed"
  [[ "$schema" == nysa.software-factory.dev-lane/v1 ]] ||
    die "lane ownership marker does not bind this root"
  refuse_production_path "$root"
  printf '%s\n' "$root"
}

require_lane_mode() {
  local root="$1" expected="$2"
  [[ "$(python3 - "$root/marker.json" <<'PY'
import json, sys
print(json.load(open(sys.argv[1], encoding="utf-8"))["mode"])
PY
)" == "$expected" ]] || die "lane mode does not authorize this command"
}

clean_lane() {
  local root current_tmp
  root="$(validate_lane "$1")"
  current_tmp="$(physical "${TMPDIR:-/tmp}")"
  [[ "$(dirname "$root")" == "$current_tmp" ]] ||
    die "cleanup requires the lane's creation TMPDIR"
  python3 - "$root" <<'PY' || exit 1
import json, os, stat, sys
root=sys.argv[1]; marker=os.path.join(root, "marker.json")
r=os.lstat(root); m=os.lstat(marker); v=json.load(open(marker, encoding="utf-8"))
if (not stat.S_ISDIR(r.st_mode) or stat.S_IMODE(r.st_mode) != 0o700 or
    r.st_uid != os.getuid() or r.st_dev != v["root_dev"] or r.st_ino != v["root_ino"] or
    not stat.S_ISREG(m.st_mode) or m.st_nlink != 1 or m.st_uid != os.getuid() or
    stat.S_IMODE(m.st_mode) != 0o600):
    raise SystemExit("lane changed immediately before cleanup")
PY
  if [[ -f "$root/runtime/provider-state.sqlite3" ]]; then
    python3 "$root/kit/scripts/provider-coordinator.py" \
      --db "$root/runtime/provider-state.sqlite3" status | python3 -c '
import json, sys
value=json.load(sys.stdin)
if value.get("active_reserve_micro_usd") != 0:
    raise SystemExit("provider reservations are still active")
if any(name != "terminal" for name in value.get("counts", {})):
    raise SystemExit("provider attempts have not reached terminal state")
' || die "cleanup refused while provider reservations remain"
  fi
  python3 - "$root" <<'PY' || die "cleanup refused while a lane process remains"
import os, pathlib, re, sys
root=pathlib.Path(sys.argv[1])
for path in root.rglob("*.pid"):
    try:
        text=path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise SystemExit(f"unreadable pid evidence: {path}")
    match=re.search(r"(?:^|\n)pid=([1-9][0-9]*)(?:\n|$)", text)
    if match is None and text.strip().isdigit():
        match=re.match(r"([1-9][0-9]*)", text.strip())
    if match is None:
        continue
    try:
        os.kill(int(match.group(1)), 0)
    except ProcessLookupError:
        continue
    except PermissionError:
        raise SystemExit(f"cannot prove pid is stopped: {path}")
    raise SystemExit(f"live pid evidence: {path}")
PY
  if [[ -f "$root/runtime/product-containers.json" ]]; then
    while IFS= read -r container; do
      [[ -n "$container" ]] || continue
      label="$(DOCKER_HOST="$(cat "$root/runtime/docker-host")" "$root/runtime/docker" \
        inspect --format '{{ index .Config.Labels "nysa.factory.dev-lane-root" }}' \
        "$container" 2>/dev/null || true)"
      [[ -z "$label" || "$label" == "$root" ]] ||
        die "cleanup refused a container whose lane label drifted"
      [[ -z "$label" ]] || DOCKER_HOST="$(cat "$root/runtime/docker-host")" \
        "$root/runtime/docker" rm -f "$container" >/dev/null ||
        die "cleanup could not remove a lane container"
    done < <(python3 - "$root/runtime/product-containers.json" <<'PY'
import json, sys
value=json.load(open(sys.argv[1], encoding="utf-8"))
if value.get("schema") != "factory-dev-product-containers/v1": raise SystemExit(1)
for item in value.get("containers", []): print(item["name"])
PY
    )
  fi
  rm -rf -- "$root"
  echo "CLEANED=$root"
}

write_seatbelt_profiles() {
  local root="$1" cursor="$2" bridge="${3:-}" session_home="${4:-}" native_auth_home="${5:-}"
  python3 - "$root" "$cursor" "$bridge" "$session_home" "$native_auth_home" <<'PY'
import json, os, pathlib, sys
root, cursor, bridge, session_home, native_auth_home = sys.argv[1:]
system = [
    "/System", "/bin", "/sbin", "/usr/bin", "/usr/lib", "/usr/libexec",
    "/usr/share", "/etc", "/private/etc", "/private/var/db/timezone",
    "/Library/Apple", "/var/select", "/private/var/select",
]
tools=[]
for entry in pathlib.Path(root, "home").iterdir():
    if not entry.is_symlink(): continue
    target=entry.resolve(); parent=str(target.parent)
    if parent not in tools: tools.append(parent)
    if entry.name == "python3":
        framework=str(target.parent.parent)
        if framework not in tools: tools.append(framework)
    if entry.name == "git" and target.parent.name == "bin" and target.parent.parent.name == "usr":
        developer=target.parent.parent.parent
        for relative in ("usr/libexec/git-core", "usr/share/git-core/templates"):
            item=developer / relative
            if item.is_dir() and str(item) not in tools: tools.append(str(item))
if pathlib.Path(root, "home/node").is_symlink():
    for item in ("/opt/homebrew", "/usr/local"):
        if os.path.isdir(item) and item not in tools: tools.append(item)
reads=[]
session=[] if not session_home else [
    str(pathlib.Path(session_home, ".cursor", name).resolve())
    for name in ("auth.json", "cli-config.json")
] + [str(pathlib.Path(session_home, "Library", "Keychains").resolve())]
for item in system + tools + [root]:
    if item not in reads: reads.append(item)
metadata={"/"}
for item in reads + session:
    p=pathlib.Path(item); metadata.add(str(p)); metadata.update(map(str, p.parents))
bridge_paths=[] if not bridge else [bridge]
if bridge == "/private/tmp/.cursor": bridge_paths.append("/tmp/.cursor")
for item in bridge_paths:
    p=pathlib.Path(item); metadata.add(str(p)); metadata.update(map(str, p.parents))
base=["(version 1)\n", "(deny default)\n", "(allow process-fork)\n",
      "(allow process-info* (target same-sandbox))\n", "(allow sysctl-read)\n",
      "(allow mach-lookup)\n"]
for item in sorted(metadata):
    base += [f"(allow file-read-metadata (literal {json.dumps(item)}))\n",
             f"(allow file-read-data (literal {json.dumps(item)}))\n"]
for item in reads:
    base += [f"(allow file-read* (subpath {json.dumps(item)}))\n",
             f"(allow process-exec (subpath {json.dumps(item)}))\n"]
for item in session:
    base += [f"(allow file-read* (subpath {json.dumps(item)}))\n"]
base += [f"(allow file-write* (subpath {json.dumps(root)}))\n",
         '(allow file-read* (literal "/dev/null"))\n',
         '(allow file-read* (literal "/dev/random"))\n',
         '(allow file-read* (literal "/dev/urandom"))\n',
         '(allow file-write* (literal "/dev/null"))\n',
         '(allow file-read-metadata (literal "/dev"))\n',
         '(allow file-read* (subpath "/dev/fd"))\n',
         '(allow file-write* (subpath "/dev/fd"))\n',
         '(allow signal (target same-sandbox))\n']
pathlib.Path(root, "runtime/mock.sb").write_text(
    "".join(base) + '(deny mach-lookup (global-name "com.apple.securityd"))\n'
    + "(deny network*)\n")
cursor_network = ('(allow network-bind (local ip "localhost:*"))\n'
                  '(allow network-inbound (local ip "localhost:*"))\n'
                  '(allow network-outbound (remote ip "localhost:*"))\n'
                  '(allow network-outbound)\n')
for item in bridge_paths:
    cursor_network += (f"(allow file-read* (subpath {json.dumps(item)}))\n"
                       f"(allow file-write* (subpath {json.dumps(item)}))\n")
pathlib.Path(root, "runtime/cursor.sb").write_text("".join(base) + cursor_network)
native_auth=[] if not native_auth_home else [
    str(pathlib.Path(native_auth_home, "Library", "Keychains").resolve())
]
native_extra=""
for item in native_auth:
    p=pathlib.Path(item)
    for parent in [p, *p.parents]:
        native_extra += f"(allow file-read-metadata (literal {json.dumps(str(parent))}))\n"
    native_extra += f"(allow file-read* (subpath {json.dumps(item)}))\n"
pathlib.Path(root, "runtime/native.sb").write_text("".join(base) + cursor_network + native_extra)
PY
  chmod 600 "$root/runtime/"*.sb
}

prepare_product_dependencies() {
  local root="$1" ticket work
  shift
  [[ -f "$root/product/package-lock.json" &&
     ! -L "$root/product/package-lock.json" ]] || return 0
  ( cd "$root"
    HOME="$root/home" TMPDIR="$root/tmp" \
      "$(sandbox_exec)" -f "$root/runtime/native.sb" \
        env -i HOME="$root/home" TMPDIR="$root/tmp" LANG=C LC_ALL=C \
          PATH="$root/home:/usr/bin:/bin:/usr/sbin:/sbin" \
          "$root/home/node" -e \
            'if (process.versions.node.split(".")[0] !== "22") process.exit(1)' ) ||
    die "product dependency installation requires the pinned Node 22 runtime"
  for ticket in "$@"; do
    work="$root/worktrees/$ticket"
    [[ -f "$work/package.json" && ! -L "$work/package.json" &&
       -f "$work/package-lock.json" && ! -L "$work/package-lock.json" ]] ||
      die "product dependency manifests are missing or unsafe: $ticket"
    ( cd "$work"
      HOME="$root/home" TMPDIR="$root/tmp" \
        "$(sandbox_exec)" -f "$root/runtime/native.sb" \
          env -i HOME="$root/home" TMPDIR="$root/tmp" LANG=C LC_ALL=C \
            PATH="$root/home:/usr/bin:/bin:/usr/sbin:/sbin" \
            npm_config_cache="$root/runtime/npm-cache" \
            "$root/home/npm" ci --prefix "$work" --no-audit --no-fund \
            >"$root/runtime/npm-ci-$ticket.log" 2>&1 ) ||
      die "pinned product dependency installation failed: $ticket"
    [[ -z "$(git -C "$work" status --porcelain --untracked-files=all)" ]] ||
      die "product dependency installation changed the tracked worktree: $ticket"
  done
}

create_lane() {
  local mode="$1" root sha tree nonce project cursor developer tool timeout_bin tmp_parent bridge session_home ticket port_a port_b resolved seed_hash="" accounting_hash="" lineage_hash="" checkpoint_hash="" cleanup_trap subscription_adapter
  local -a subscription_tools
  subscription_adapter="${FACTORY_SUBSCRIPTION_ADAPTER:-codex}"
  [[ "$mode" != subscription || "$subscription_adapter" == codex ||
     "$subscription_adapter" == claude ]] ||
    die "unsupported subscription canary adapter"
  [[ -z "$(git -C "$SOURCE_ROOT" status --porcelain --untracked-files=all)" ]] ||
    die "Software Factory source must be clean and committed"
  sha="$(git -C "$SOURCE_ROOT" rev-parse HEAD)"
  tree="$(git -C "$SOURCE_ROOT" rev-parse 'HEAD^{tree}')"
  tmp_parent="$(physical "${TMPDIR:-/tmp}")"
  root="$(mktemp -d "$tmp_parent/nysa-sf-dev.XXXXXX")"
  root="$(physical "$root")"
  chmod 700 "$root"
  refuse_production_path "$root"
  if [[ "$mode" == subscription ]]; then
    printf '%s\n' "$subscription_adapter" >"$root/subscription-adapter"
    chmod 600 "$root/subscription-adapter"
  fi
  nonce="$(basename "$root" | sed 's/^nysa-sf-dev\.//')"
  project="factory-dev-lane-$(printf '%s' "$nonce" | tr '[:upper:]' '[:lower:]')"
  mkdir -p "$root/home/.factory" "$root/home/.hermes/profiles/factory-dev-$(basename "$root")" \
    "$root/runtime/model-state" "$root/runtime/provider-attempts" \
    "$root/runtime/provider-locks" "$root/runtime/provider-inputs" \
    "$root/tmp" "$root/worktrees"
  python3 - "$root/marker.json" "$root" "$nonce" "$sha" "$tree" "$mode" \
    "$tmp_parent" <<'PY'
import json, os, sys
path, root, nonce, sha, tree, mode, tmp_parent = sys.argv[1:]
info=os.lstat(root)
with open(path, "w", encoding="utf-8") as f:
    json.dump({"schema":"nysa.software-factory.dev-lane/v1","root":root,
               "nonce":nonce,"kit_sha":sha,"kit_tree":tree,"mode":mode,
               "uid":os.getuid(),"root_dev":info.st_dev,"root_ino":info.st_ino,
               "tmp_parent":tmp_parent}, f,
              sort_keys=True, separators=(",",":"))
    f.write("\n")
os.chmod(path, 0o600)
PY
  printf -v cleanup_trap \
    'status=$?; trap - EXIT; clean_lane %q >/dev/null 2>&1 || printf "factory-dev-lane: incomplete lane retained for inspection: %%s\\n" %q >&2; exit "$status"' \
    "$root" "$root"
  trap "$cleanup_trap" EXIT
  if [[ -x /usr/bin/xcode-select ]]; then
    developer="$(/usr/bin/xcode-select -p 2>/dev/null || true)"
    if [[ -n "$developer" && -x "$developer/usr/bin/git" &&
          -x "$developer/usr/bin/python3" ]]; then
      ln -s "$developer/usr/bin/git" "$root/home/git"
      ln -s "$developer/usr/bin/python3" "$root/home/python3"
      for tool in git-receive-pack git-upload-pack; do
        [[ -x "$developer/usr/bin/$tool" ]] && ln -s "$developer/usr/bin/$tool" "$root/home/$tool"
      done
    fi
  fi
  git clone -q --no-local --no-hardlinks "$SOURCE_ROOT" "$root/kit"
  git -C "$root/kit" checkout -q --detach "$sha"
  cp "$root/kit/scripts/lib/sandbox-ps.py" "$root/home/ps"
  chmod 755 "$root/home/ps"

  if [[ "$mode" == product ]]; then
    [[ "$PRODUCT_SOURCE" == /* && -d "$PRODUCT_SOURCE" && ! -L "$PRODUCT_SOURCE" ]] ||
      die "product source must be an absolute, non-symlink repository"
    refuse_production_path "$PRODUCT_SOURCE"
    [[ "$PRODUCT_BASE" =~ ^[0-9a-f]{40}$ ]] || die "product base must be a full commit SHA"
    [[ -z "$(git -C "$PRODUCT_SOURCE" status --porcelain --untracked-files=all)" ]] ||
      die "product source must be clean"
    [[ "$(git -C "$PRODUCT_SOURCE" rev-parse HEAD)" == "$PRODUCT_BASE" ]] ||
      die "product source HEAD does not match the approved base"
    git clone -q --no-local --no-hardlinks "$PRODUCT_SOURCE" "$root/product"
    git -C "$root/product" checkout -q --detach "$PRODUCT_BASE"
    git -C "$root/product" remote remove origin
    git -C "$root/product" branch -f main "$PRODUCT_BASE"
    lane_tickets=("${PRODUCT_TICKETS[@]}")
    [[ "${#lane_tickets[@]}" -ge 1 && "${#lane_tickets[@]}" -le 4 ]] ||
      die "product lane requires one to four tickets"
    mkdir -p "$root/product/factory/route-plans" "$root/product/factory/runs"
    for ticket in "${lane_tickets[@]}"; do
      [[ "$ticket" =~ ^T-[0-9]+$ ]] || die "invalid product ticket"
      [[ -f "$root/product/factory/tickets/$ticket.md" &&
         ! -L "$root/product/factory/tickets/$ticket.md" ]] ||
        die "product ticket is missing or unsafe: $ticket"
      git -C "$root/product" ls-files --error-unmatch "factory/tickets/$ticket.md" >/dev/null ||
        die "product ticket is not committed: $ticket"
      grep -Eq '^State: (Backlog|Ready)$' "$root/product/factory/tickets/$ticket.md" ||
        die "product ticket is not at a plannable boundary: $ticket"
      [[ ! -e "$root/product/factory/route-plans/$ticket.json" ]] ||
        die "product ticket already has a route plan: $ticket"
    done
    python3 - "$root/product/factory/PROJECT.env" "$root/worktrees" <<'PY'
from pathlib import Path
import re, sys
path=Path(sys.argv[1]); worktrees=sys.argv[2]
text=path.read_text(encoding="utf-8")
text=re.sub(r"(?m)^MAX_CONCURRENT_TICKETS=.*$", "MAX_CONCURRENT_TICKETS=4", text)
if "MAX_CONCURRENT_TICKETS=" not in text:
    text += "\nMAX_CONCURRENT_TICKETS=4\n"
text=re.sub(r'(?m)^WORKTREES_DIR=.*$', f'WORKTREES_DIR="{worktrees}"', text)
path.write_text(text, encoding="utf-8")
PY
    printf '%s\n' "$sha" >"$root/product/factory/KIT_PIN"
    for ticket in "${lane_tickets[@]}"; do
      python3 - "$root/product/factory/tickets/$ticket.md" <<'PY'
from pathlib import Path
import re, sys
path=Path(sys.argv[1]); text=path.read_text(encoding="utf-8")
text, count=re.subn(r"(?m)^State: (?:Backlog|Ready)$", "State: Ready", text, count=1)
if count != 1: raise SystemExit(1)
path.write_text(text, encoding="utf-8")
PY
    done
    git -C "$root/product" add factory/PROJECT.env factory/KIT_PIN factory/tickets
    git -C "$root/product" -c user.name='Factory Dev Lane' -c user.email=factory-dev@local \
      commit -qm 'Configure isolated Contract 1.7 product lane'
    lane_control_sha="$(git -C "$root/product" rev-parse HEAD)"
    git init -q --bare "$root/origin.git"
    git -C "$root/product" remote add origin "$root/origin.git"
    git -C "$root/product" switch -q main
    git -C "$root/product" reset -q --hard "$lane_control_sha"
    git -C "$root/product" push -q -u origin main
    for ticket in "${lane_tickets[@]}"; do
      git -C "$root/product" worktree add -q -b "ticket/$ticket" \
        "$root/worktrees/$ticket" "$lane_control_sha"
      git -C "$root/worktrees/$ticket" push -q -u origin "ticket/$ticket"
    done
    if [[ -n "$PRODUCT_SEED_BUNDLE" ]]; then
      seed_product_worktrees "$root" "$PRODUCT_SEED_BUNDLE" "$PRODUCT_BASE" \
        "${lane_tickets[@]}"
      seed_hash="$(sha256_file "$PRODUCT_SEED_BUNDLE")"
      prepare_product_seed_accounting "$root" "$PRODUCT_SEED_ACCOUNTING" \
        "$PRODUCT_SEED_BUNDLE" "$PRODUCT_BASE" \
        "${lane_tickets[@]}"
      accounting_hash="$(sha256_file "$PRODUCT_SEED_ACCOUNTING")"
      lineage_hash="$(sha256_file "$PRODUCT_SEED_LINEAGE")"
      if [[ -n "$PRODUCT_SEED_CHECKPOINT" ]]; then
        checkpoint_hash="$(sha256_file "$PRODUCT_SEED_CHECKPOINT")"
        write_product_checkpoint_import "$root" "$PRODUCT_SEED_CHECKPOINT"
      fi
    fi
    python3 - "$root/runtime/product-source.json" "$PRODUCT_BASE" \
      "$(git -C "$PRODUCT_SOURCE" rev-parse "$PRODUCT_BASE^{tree}")" "$lane_control_sha" \
      "$seed_hash" "$accounting_hash" "$lineage_hash" "$checkpoint_hash" -- \
      "${lane_tickets[@]}" <<'PY'
import json, os, sys
path, base, tree, control, seed, accounting, lineage, checkpoint, separator, *tickets=sys.argv[1:]
if separator != "--": raise SystemExit(1)
with open(path, "w", encoding="utf-8") as stream:
    json.dump({"schema":"factory-dev-product-source/v1","base_sha":base,
               "base_tree":tree,"lane_control_sha":control,
               "seed_bundle_sha256":seed or None,
               "seed_accounting_sha256":accounting or None,
               "seed_lineage_sha256":lineage or None,
               "seed_checkpoint_sha256":checkpoint or None,"tickets":tickets},
              stream, sort_keys=True, separators=(",",":")); stream.write("\n")
os.chmod(path, 0o600)
PY
  else
  mkdir -p "$root/product"
  git -C "$SOURCE_ROOT" archive "$sha" conformance/app | tar -x -C "$root/product"
  mv "$root/product/conformance/app" "$root/product/app"
  rmdir "$root/product/conformance"
  mkdir -p "$root/product/factory/tickets" "$root/product/factory/route-plans" \
    "$root/product/factory/runs" "$root/product/docs/acceptance"
  cat > "$root/product/factory/ENVELOPE.env" <<'EOF'
PER_RUN_BUDGET_USD=10.00
PER_TICKET_BUDGET_USD=100.00
PER_RUN_MAX_TURNS=15
PER_RUN_TIMEOUT_MIN=20
DAILY_CAP_USD=1000.00
EOF
  cat > "$root/product/factory/PROJECT.env" <<EOF
PROJECT_NAME=$project
TICKET_BRANCH_PREFIX=ticket/
TEST_PATHS="app/tests/"
WORKTREES_DIR=$root/worktrees
EOF
  printf '%s\n' "$sha" > "$root/product/factory/KIT_PIN"
  printf '%s\n' 'date,time,ticket,role,adapter,prompt_version,turns,cost_usd,exit_status,run_id,provider_family,model_id,selection_reason,cost_basis,adapter_version' > "$root/product/factory/ledger.csv"
  lane_tickets=("$TICKET")
  [[ "$mode" == mock-concurrency || "$mode" == subscription ]] && lane_tickets=("${TICKETS[@]}")
  for ticket in "${lane_tickets[@]}"; do
    port_a=$((4781 + 2 * (10#${ticket#T-} - 900001)))
    port_b=$((port_a + 1))
    cat > "$root/product/factory/tickets/$ticket.md" <<EOF
# $ticket — version the JSON health response

State: Ready
Priority: low
Risk class: low
External: no
Kit-SHA: $sha

## Description

Add the required top-level \`schemaVersion: 1\` field to Relay's existing \`GET /health\` JSON response. Preserve the existing status, content type, queue counts, and approval counts. JSON member order is not contractual, so the test-author may convert existing complete-body assertions to parsed-object equality. Follow [the engine rule](../../docs/engine-spec.md#health-response), [acceptance spec](../../docs/acceptance/health-version.md), and [conventions](../../docs/conventions.md).

## Acceptance criteria

1. A fresh \`GET /health\` returns HTTP 200, \`Content-Type: application/json\`, and top-level \`schemaVersion\` equal to integer \`1\` alongside the existing \`ok\`, \`queue\`, and \`approvals\` fields.
2. After one accepted event completes, \`GET /health\` still returns \`schemaVersion: 1\`, \`queue.done: 1\`, and \`approvals.pending: 1\`.
3. Focused tests in \`app/tests/health-version.test.js\` use the reserved, otherwise-unused ports \`$port_a\` and \`$port_b\`, and pass with \`node --test app/tests/health-version.test.js\`.
4. The complete app suite passes with \`npm --prefix app test\`; test-author ownership includes complete-body expectation helpers in \`app/tests/health.test.js\` and \`app/tests/health-approvals.test.js\`.
EOF
  done
  cat > "$root/product/docs/engine-spec.md" <<'EOF'
# Relay engine spec

## Health response

`GET /health` is an unauthenticated same-origin JSON endpoint. Every successful response returns HTTP 200, `Content-Type: application/json`, and the exact top-level fields `schemaVersion`, `ok`, `queue`, and `approvals`. JSON member order is not contractual. `schemaVersion` is the integer `1`; `ok` remains `true`; queue and approval counters retain their existing shapes and values.
EOF
  cat > "$root/product/docs/acceptance/health-version.md" <<'EOF'
# Versioned health response

A fresh server and a server with one completed event both report `schemaVersion: 1` from `GET /health`. The completed-event response also reports `queue.done: 1` and `approvals.pending: 1`. Focused coverage belongs in `app/tests/health-version.test.js` and uses the reserved ports `4781` and `4782`; test-author ownership also includes complete-body expectation helpers in `app/tests/health.test.js` and `app/tests/health-approvals.test.js`. Implementation belongs in `app/server.js`.

Failure responses, duplicate webhook delivery, authentication, cookies, CORS, content negotiation, additional schema versions, and UI selectors are out of scope.
EOF
  cat > "$root/product/docs/conventions.md" <<'EOF'
# Conventions

Use Node.js built-ins and `node:test`; add no dependency. Preserve exact JSON field names and integer types.
EOF
  cat > "$root/product/.gitignore" <<'EOF'
factory/runtime-ledger.csv
factory/runs/
factory/.active-runs/
factory/.launch.lock/
factory/.provider.lock/
factory/.ledger.lock/
factory/linear-map.json
app/data/
EOF
  mkdir -p "$root/product/.cursor"
  printf '%s\n' '{"permissions":{"allow":[],"deny":["Shell(security)"]}}' > \
    "$root/product/.cursor/cli.json"
  git -C "$root/product" init -q
  git -C "$root/product" branch -M main
  git -C "$root/product" add .
  git -C "$root/product" -c user.name='Factory Dev Lane' -c user.email=factory-dev@local \
    commit -qm 'Create disposable factory product'
  git init -q --bare "$root/origin.git"
  git -C "$root/product" remote add origin "$root/origin.git"
  git -C "$root/product" push -q -u origin main
  for ticket in "${lane_tickets[@]}"; do
    git -C "$root/product" worktree add -q -b "ticket/$ticket" \
      "$root/worktrees/$ticket" main
    git -C "$root/worktrees/$ticket" push -q -u origin "ticket/$ticket"
  done
  fi
  if [[ "$mode" == cursor || "$mode" == product ]]; then
    cursor="$(cursor_bin)"
    bridge="$(cursor_tmp_bridge)"
    session_home="$(cursor_session_home)"
    for tool in auth.json cli-config.json; do
      [[ -f "$session_home/.cursor/$tool" && ! -L "$session_home/.cursor/$tool" ]] ||
        die "Cursor CLI session file is unavailable: $tool"
    done
  else
    cursor=/usr/bin/true
    bridge=""
    session_home="$ACCOUNT_HOME"
  fi
  if [[ "$mode" == cursor || "$mode" == subscription || "$mode" == product ]]; then
    timeout_bin="$(command -v timeout 2>/dev/null || true)"
    [[ "$timeout_bin" == /* && -x "$timeout_bin" ]] ||
      die "subscription lane requires the installed timeout command"
    ln -s "$(python3 - "$timeout_bin" <<'PY'
import os, sys
print(os.path.realpath(sys.argv[1]))
PY
)" "$root/home/timeout"
  fi
  ln -s "$cursor" "$root/home/agent"
  if [[ "$mode" == subscription || "$mode" == product ]]; then
    mkdir -m 700 "$root/session-home"
    subscription_tools=()
    if [[ "$mode" == product || "$subscription_adapter" == codex ]]; then
      mkdir -m 700 "$root/session-home/.codex"
      [[ -f "$session_home/.codex/auth.json" && ! -L "$session_home/.codex/auth.json" ]] ||
        die "Codex subscription session file is unavailable"
      cp "$session_home/.codex/auth.json" "$root/session-home/.codex/auth.json"
      chmod 600 "$root/session-home/.codex/auth.json"
      subscription_tools+=(codex)
    fi
    if [[ "$mode" == product || "$subscription_adapter" == claude ]]; then
      mkdir -m 700 "$root/session-home/.claude"
      [[ -f "$session_home/.claude/.credentials.json" &&
         ! -L "$session_home/.claude/.credentials.json" ]] ||
        die "Claude subscription session file is unavailable"
      cp "$session_home/.claude/.credentials.json" \
        "$root/session-home/.claude/.credentials.json"
      chmod 600 "$root/session-home/.claude/.credentials.json"
      subscription_tools+=(claude)
    fi
    if [[ "$mode" == product ]]; then
      mkdir -m 700 "$root/session-home/.cursor"
      cp "$session_home/.cursor/auth.json" "$root/session-home/.cursor/auth.json"
      cp "$session_home/.cursor/cli-config.json" "$root/session-home/.cursor/cli-config.json"
      chmod 600 "$root/session-home/.cursor/"*.json \
        "$root/session-home/.claude/.credentials.json"
    fi
    for tool in "${subscription_tools[@]}"; do
      resolved="$(command -v "$tool" 2>/dev/null || true)"
      [[ "$resolved" == /* && -x "$resolved" ]] || die "$tool CLI is unavailable"
      refuse_production_path "$resolved"
      resolved="$(python3 - "$resolved" <<'PY'
import os, sys
print(os.path.realpath(sys.argv[1]))
PY
)"
      refuse_production_path "$resolved"
      ln -s "$resolved" "$root/home/$tool-real"
    done
  fi
  if [[ "$mode" == product ]]; then
    for tool in node npm npx; do
      resolved="/opt/homebrew/opt/node@22/bin/$tool"
      [[ -x "$resolved" || -f "$resolved" ]] || die "Node 22 $tool is unavailable"
      ln -s "$resolved" "$root/home/$tool"
    done
    resolved="$(command -v docker 2>/dev/null || true)"
    [[ "$resolved" == /* && -x "$resolved" ]] || die "Docker is unavailable"
    resolved="$(python3 - "$resolved" <<'PY'
import os, sys
print(os.path.realpath(sys.argv[1]))
PY
)"
    ln -s "$resolved" "$root/runtime/docker"
    docker context inspect --format '{{.Endpoints.docker.Host}}' >"$root/runtime/docker-host" ||
      die "Docker context is unavailable"
    chmod 600 "$root/runtime/docker-host"
  fi
  if [[ "$mode" == mock-concurrency ]]; then
    cat > "$root/home/mock-provider-cli" <<'PY'
#!/usr/bin/env python3
import os, time
from pathlib import Path
timeline=Path(os.environ["FACTORY_DEV_LANE_TIMELINE"])
with timeline.open("a", encoding="utf-8") as handle:
    handle.write(f"start {os.getpid()} {time.monotonic_ns()}\n")
time.sleep(2)
with timeline.open("a", encoding="utf-8") as handle:
    handle.write(f"end {os.getpid()} {time.monotonic_ns()}\n")
PY
    chmod 700 "$root/home/mock-provider-cli"
    python3 - "$root/runtime/provider-policy.json" "$root/runtime/provider-activation.json" <<'PY'
import hashlib, json, os, sys
policy_path, activation_path=sys.argv[1:]
global_limit={"max_concurrent":4,"max_starts":32,"window_seconds":60}
account_limit={"max_concurrent":4,"max_starts":32,"window_seconds":60}
policy={"schema":"factory-provider-concurrency-policy/v1","coupled_max_concurrent":4,
        "global":global_limit,"provider_families":{"mock":global_limit},
        "account_routes":{"test-mock-a":account_limit,"test-mock-b":account_limit}}
raw=json.dumps(policy, sort_keys=True, separators=(",",":"))
with open(policy_path, "w", encoding="utf-8") as handle: handle.write(raw+"\n")
routes={}
for number in range(900001,900005):
    ticket=f"T-{number}"
    routes[f"test-mock-{ticket}"]={"account_route":"test-mock-a",
        "adapter":"mock","model":"test-mock-model","provider_family":"mock"}
activation={"enabled":True,"mode":"cli-concurrent-v1",
            "policy_sha256":hashlib.sha256(raw.encode()).hexdigest(),"routes":routes,
            "schema":"nysa.software-factory.provider-activation/v2"}
with open(activation_path, "w", encoding="utf-8") as handle:
    json.dump(activation, handle, sort_keys=True, separators=(",",":")); handle.write("\n")
os.chmod(policy_path, 0o600); os.chmod(activation_path, 0o600)
PY
  fi
  [[ "$mode" != subscription && "$mode" != product ]] || session_home="$root/session-home"
  write_seatbelt_profiles "$root" "$cursor" "$bridge" "$session_home" ""
  if [[ "$mode" == subscription || "$mode" == product ]]; then
    python3 - "$root/runtime/claude-settings.json" "$ACCOUNT_HOME" <<'PY'
import json, os, sys
path, home=sys.argv[1:]
denied=[
    f"Read({home}/.factory/**)", f"Read({home}/.hermes/**)",
    f"Read({home}/Projects/nysa-company/nysa-app/**)",
    "Bash(security *)", "Bash(ssh *)", "Bash(scp *)",
]
value={"permissions":{"deny":denied},"sandbox":{"enabled":True,
       "failIfUnavailable":True,"autoAllowBashIfSandboxed":True,
       "allowUnsandboxedCommands":False}}
with open(path,"w",encoding="utf-8") as stream:
    json.dump(value,stream,sort_keys=True,separators=(",",":")); stream.write("\n")
os.chmod(path,0o600)
PY
    if [[ "$mode" == subscription ]]; then
      subscription_tools=("$subscription_adapter")
    else
      subscription_tools=(codex claude)
    fi
    for tool in "${subscription_tools[@]}"; do
      cat >"$root/home/$tool" <<EOF
#!/usr/bin/env bash
exec "$(sandbox_exec)" -f "$root/runtime/native.sb" "$root/home/$tool-real" "\$@"
EOF
      chmod 700 "$root/home/$tool"
    done
  fi
  [[ "$mode" != product ]] ||
    prepare_product_dependencies "$root" "${lane_tickets[@]}"
  validate_runtime_paths "$root"
  trap - EXIT
  printf '%s\n' "$root"
}

validate_product_checkpoint() {
  local checkpoint="$1" bundle="$2" base="$3"; shift 3
  [[ "$checkpoint" == /* && -f "$checkpoint" && ! -L "$checkpoint" &&
     "$(stat -f '%Su:%Lp:%l' "$checkpoint")" == "$(id -un):600:1" ]] ||
    die "product seed checkpoint must be an owner-only regular file"
  refuse_production_path "$checkpoint"
  python3 - "$checkpoint" "$(sha256_file "$bundle")" "$base" "$@" <<'PY' ||
import json, re, sys
path, bundle, base, *tickets=sys.argv[1:]
value=json.load(open(path, encoding="utf-8"))
sha40=lambda item: isinstance(item,str) and re.fullmatch(r"[0-9a-f]{40}",item)
sha256=lambda item: isinstance(item,str) and re.fullmatch(r"[0-9a-f]{64}",item)
if (set(value) != {"schema","base_sha","base_tree","source_factory_sha",
                   "source_factory_tree","source_marker_sha256",
                   "source_product_sha256","prior_accounting_sha256",
                   "seed_bundle_sha256","lane_charges_micro_usd","tickets"} or
    value.get("schema") != "factory-dev-product-checkpoint/v1" or
    value.get("base_sha") != base or not sha40(value.get("base_tree")) or
    not sha40(value.get("source_factory_sha")) or
    not sha40(value.get("source_factory_tree")) or
    not sha256(value.get("source_marker_sha256")) or
    not sha256(value.get("source_product_sha256")) or
    value.get("prior_accounting_sha256") is not None and
        not sha256(value.get("prior_accounting_sha256")) or
    value.get("seed_bundle_sha256") != bundle or
    not isinstance(value.get("tickets"),list) or not value["tickets"] or
    len({item.get("ticket") for item in value["tickets"]}) != len(value["tickets"]) or
    not set(item.get("ticket") for item in value["tickets"]) <= set(tickets) or
    not isinstance(value.get("lane_charges_micro_usd"),dict) or
    not set(tickets) <= set(value["lane_charges_micro_usd"]) or
    any(not isinstance(ticket,str) or
        not re.fullmatch(r"T-[0-9]+",ticket)
        for ticket in value["lane_charges_micro_usd"]) or
    any(not isinstance(amount,int) or isinstance(amount,bool) or amount < 0
        for amount in value["lane_charges_micro_usd"].values())):
    raise SystemExit(1)
allowed=("planner","spec-linter","test-author","builder")
for item in value["tickets"]:
    if set(item) != {"ticket","head_sha","head_tree","ticket_blob",
                    "route_plan_sha256","next_stage","state","roles",
                    "spec_verdicts"}:
        raise SystemExit(1)
    if (not sha40(item["head_sha"]) or not sha40(item["head_tree"]) or
        not sha40(item["ticket_blob"]) or not sha256(item["route_plan_sha256"]) or
        item["next_stage"] not in {"RUN planner","RUN spec-linter",
                                   "RUN test-author","RUN builder","RUN reviewer"} or
        item["state"] not in {"Ready","Planning","Building","Review"} or
        not isinstance(item["roles"],list) or not item["roles"]):
        raise SystemExit(1)
    roles=[]
    for run in item["roles"]:
        if (set(run) != {"role","run_id","manifest_sha256","output_sha256",
                        "role_head_before"} or run["role"] not in allowed or
            not re.fullmatch(r"[A-Za-z0-9._-]+",run["run_id"]) or
            not sha256(run["manifest_sha256"]) or
            not sha256(run["output_sha256"]) or
            not sha40(run["role_head_before"])):
            raise SystemExit(1)
        roles.append(run["role"])
    if any(role in {"reviewer","narrator"} for role in roles):
        raise SystemExit(1)
    specs=item["spec_verdicts"]
    if (not isinstance(specs,list) or any(not isinstance(line,str) or
        not re.fullmatch(r"SPEC-LINT: (?:PASS|FAIL(?: — .+)?)",line)
        for line in specs)):
        raise SystemExit(1)
    failures=sum(line.startswith("SPEC-LINT: FAIL") for line in specs)
    prefix=[]
    for _ in range(failures): prefix += ["planner","spec-linter"]
    if item["next_stage"] != "RUN planner": prefix += ["planner"]
    if item["next_stage"] not in {"RUN planner","RUN spec-linter"}:
        if len(specs) != failures+1 or specs[-1] != "SPEC-LINT: PASS":
            raise SystemExit(1)
        prefix += ["spec-linter"]
    if item["next_stage"] in {"RUN builder","RUN reviewer"}:
        prefix += ["test-author"]
    if item["next_stage"] == "RUN reviewer":
        prefix += ["builder"]
    if roles != prefix:
        raise SystemExit(1)
PY
    die "product seed checkpoint is malformed or detached"
}

seed_product_worktrees() {
  local root="$1" bundle="$2" base="$3" ticket commit subject index previous route_count
  local -a commits parents
  shift 3
  [[ "$bundle" == /* && -f "$bundle" && ! -L "$bundle" ]] ||
    die "product seed bundle must be an absolute regular file"
  refuse_production_path "$bundle"
  [[ "$(stat -f '%Su:%Lp' "$bundle")" == "$(id -un):600" ]] ||
    die "product seed bundle must be owner-only"
  git -C "$root/product" bundle verify "$bundle" >/dev/null 2>&1 ||
    die "product seed bundle is invalid"
  for ticket in "$@"; do
    if [[ -n "$PRODUCT_SEED_CHECKPOINT" ]] &&
       ! python3 - "$PRODUCT_SEED_CHECKPOINT" "$ticket" <<'PY'
import json, sys
raise SystemExit(0 if sys.argv[2] in {
    item["ticket"] for item in json.load(open(sys.argv[1]))["tickets"]
} else 1)
PY
    then
      continue
    fi
    git -C "$root/worktrees/$ticket" fetch -q "$bundle" \
      "refs/heads/ticket/$ticket:refs/retry/$ticket" ||
      die "product seed bundle is missing $ticket"
    if [[ -n "$PRODUCT_SEED_CHECKPOINT" ]]; then
      python3 - "$PRODUCT_SEED_CHECKPOINT" "$root/worktrees/$ticket" \
        "$ticket" <<'PY' || die "product checkpoint branch binding drifted: $ticket"
import hashlib, json, subprocess, sys
path, work, ticket=sys.argv[1:]
item=next(item for item in json.load(open(path,encoding="utf-8"))["tickets"]
          if item["ticket"] == ticket)
git=lambda *args: subprocess.check_output(
    ["git","-C",work,*args],text=True).strip()
route=subprocess.check_output(
    ["git","-C",work,"show","refs/retry/"+ticket+
     ":factory/route-plans/"+ticket+".json"])
if (git("rev-parse","refs/retry/"+ticket) != item["head_sha"] or
    git("rev-parse","refs/retry/"+ticket+"^{tree}") != item["head_tree"] or
    git("rev-parse","refs/retry/"+ticket+":factory/tickets/"+ticket+".md")
        != item["ticket_blob"] or
    hashlib.sha256(route).hexdigest() != item["route_plan_sha256"]):
    raise SystemExit(1)
PY
    fi
    git -C "$root/worktrees/$ticket" merge-base --is-ancestor \
      "$base" "refs/retry/$ticket" || die "product seed does not descend from the approved base"
    commits=()
    while IFS= read -r commit; do commits+=("$commit"); done < <(
      git -C "$root/worktrees/$ticket" rev-list --reverse \
        "$base..refs/retry/$ticket"
    )
    [[ "${#commits[@]}" -ge 2 ]] || die "product seed history is incomplete: $ticket"
    previous="$base"
    for commit in "${commits[@]}"; do
      read -r -a parents <<<"$(git -C "$root/worktrees/$ticket" \
        rev-list --parents -n 1 "$commit")"
      [[ "${#parents[@]}" -eq 2 && "${parents[1]}" == "$previous" ]] ||
        die "product seed history is not linear: $ticket"
      previous="$commit"
    done
    [[ "$(git -C "$root/worktrees/$ticket" show -s --format=%s "${commits[0]}")" == \
       'Configure isolated Contract 1.7 product lane' ]] ||
      die "product seed control boundary is unrecognized: $ticket"
    [[ "$(git -C "$root/worktrees/$ticket" show -s \
      --format='%an <%ae>%n%cn <%ce>' "${commits[0]}")" == \
      $'Factory Dev Lane <factory-dev@local>\nFactory Dev Lane <factory-dev@local>' ]] ||
      die "product seed control identity is unrecognized: $ticket"
    python3 - "$root/worktrees/$ticket" "${commits[0]}" <<'PY' ||
import subprocess, sys
worktree, commit=sys.argv[1:]
raw=subprocess.check_output(
    ["git","-C",worktree,"diff-tree","--no-commit-id","--raw","-r",commit],
    text=True).splitlines()
if any(line.split("\t",1)[0].split()[1] in {"120000","160000"} for line in raw):
    raise SystemExit(1)
paths=subprocess.check_output(
    ["git","-C",worktree,"diff-tree","--no-commit-id","--name-only","-r",commit],
    text=True).splitlines()
if (not paths or any(
    path not in {"factory/KIT_PIN","factory/PROJECT.env"} and
    not (path.startswith("factory/tickets/T-") and path.endswith(".md"))
    for path in paths
)): raise SystemExit(1)
PY
      die "product seed control boundary crosses an unsafe path: $ticket"
    python3 "$SOURCE_ROOT/scripts/lib/lane-path-sentinel.py" \
      "$root/worktrees/$ticket" "${commits[0]}" "refs/retry/$ticket" ||
      die "product seed contains a lane-local absolute path: $ticket"
    route_count=0
    index=1
    while [[ "$index" -lt "${#commits[@]}" ]]; do
      commit="${commits[$index]}"
      subject="$(git -C "$root/worktrees/$ticket" show -s --format=%s "$commit")"
      if [[ "$subject" == "$ticket: pin kit and model route plan" ]]; then
        route_count=$((route_count + 1))
        [[ "$route_count" -eq 1 &&
          "$(git -C "$root/worktrees/$ticket" show -s \
            --format='%an <%ae>%n%cn <%ce>' "$commit")" == \
          $'Software Factory <factory@local>\nSoftware Factory <factory@local>' ]] ||
          die "product seed route identity is unrecognized: $ticket"
        python3 - "$root/worktrees/$ticket" "$commit" "$ticket" <<'PY' ||
import re, subprocess, sys
worktree, commit, ticket=sys.argv[1:]
raw=subprocess.check_output(
    ["git","-C",worktree,"diff-tree","--no-commit-id","--raw","-r",commit],
    text=True).splitlines()
paths=subprocess.check_output(
    ["git","-C",worktree,"diff-tree","--no-commit-id","--name-status","-r",commit],
    text=True).splitlines()
route=f"factory/route-plans/{ticket}.json"
ticket_path=f"factory/tickets/{ticket}.md"
statuses={path: status for status, path in
          (line.split("\t",1) for line in paths)}
if (len(raw) != len(paths) or
    any(line.split("\t",1)[0].split()[1] in {"120000","160000"}
        for line in raw) or
    statuses.get(route) != "A" or
    set(statuses) not in ({route},{route,ticket_path}) or
    (ticket_path in statuses and statuses[ticket_path] != "M")):
    raise SystemExit(1)
if ticket_path in statuses:
    before=subprocess.check_output(
        ["git","-C",worktree,"show",commit+"^:"+ticket_path],text=True)
    after=subprocess.check_output(
        ["git","-C",worktree,"show",commit+":"+ticket_path],text=True)
    pattern=re.compile(r"^Kit-SHA:\s*([0-9a-f]{40})\s*$",re.I)
    def without_kit(text, require_one):
        lines=text.splitlines()
        matches=[line for line in lines if line.lower().startswith("kit-sha:")]
        if ((require_one and len(matches) != 1) or len(matches) > 1 or
            any(not pattern.fullmatch(line) for line in matches)):
            raise SystemExit(1)
        lines=[line for line in lines if not line.lower().startswith("kit-sha:")]
        while lines and not lines[-1].strip():
            lines.pop()
        return lines
    if without_kit(before,False) != without_kit(after,True):
        raise SystemExit(1)
PY
          die "product seed route boundary crosses an unsafe path: $ticket"
        index=$((index + 1))
        continue
      fi
      if ! python3 - "$root/worktrees/$ticket" "$commit" "$ticket" <<'PY'
import subprocess, sys
worktree, commit, ticket=sys.argv[1:]
raw=subprocess.check_output(
    ["git","-C",worktree,"diff-tree","--no-commit-id","--raw","-r",commit],
    text=True).splitlines()
if any(line.split("\t",1)[0].split()[1] in {"120000","160000"} for line in raw):
    raise SystemExit(1)
paths=subprocess.check_output(
    ["git","-C",worktree,"diff-tree","--no-commit-id","--name-only","-r",commit],
    text=True).splitlines()
for path in paths:
    if (path in {"factory/KIT_PIN","factory/PROJECT.env","factory/ledger.csv"} or
        path.startswith("factory/route-plans/") or
        path.startswith("factory/runs/") or
        (path.startswith("factory/tickets/") and path != f"factory/tickets/{ticket}.md")):
        raise SystemExit(1)
PY
      then
        die "product seed commit crosses a control boundary: $ticket"
      fi
      git -C "$root/worktrees/$ticket" -c user.name='Factory Dev Lane' \
        -c user.email=factory-dev@local cherry-pick -X theirs "$commit" >/dev/null ||
        die "product seed commit did not apply cleanly: $ticket"
      index=$((index + 1))
    done
    [[ "$route_count" -eq 1 ]] ||
      die "product seed route boundary is unrecognized: $ticket"
    [[ ! -e "$root/worktrees/$ticket/factory/route-plans/$ticket.json" ]] ||
      die "product seed replay retained a stale route plan: $ticket"
    require_lane_path "$root" "$root/worktrees/$ticket/factory/tickets/$ticket.md"
    [[ -f "$root/worktrees/$ticket/factory/tickets/$ticket.md" &&
       ! -L "$root/worktrees/$ticket/factory/tickets/$ticket.md" ]] ||
      die "product seed ticket file is unsafe: $ticket"
    python3 - "$root/worktrees/$ticket/factory/tickets/$ticket.md" \
      "$(git -C "$root/kit" rev-parse HEAD)" "$PRODUCT_SEED_CHECKPOINT" \
      "$ticket" <<'PY'
from pathlib import Path
import json, re, sys
p=Path(sys.argv[1]); kit_sha=sys.argv[2]; checkpoint=sys.argv[3]
ticket=sys.argv[4]; lines=[]; kit_written=False
record=None
if checkpoint:
    record=next(item for item in json.load(open(checkpoint,encoding="utf-8"))["tickets"]
                if item["ticket"] == ticket)
for line in p.read_text(encoding="utf-8").splitlines():
    if re.fullmatch(r"\s*SPEC-LINT:\s*(?:PASS|FAIL)(?:\s+—\s+.*)?\s*", line, re.I):
        continue
    elif re.fullmatch(r"\s*reviewer round\s+\d+(?::.*|\s+FIX-OWNER:\s*.*)", line, re.I):
        continue
    elif re.match(r"^Kit-SHA:\s*", line):
        if not kit_written:
            lines.append("Kit-SHA: " + kit_sha)
            kit_written=True
    else:
        lines.append(re.sub(r"^State:\s*.*$",
                            "State: "+(record["state"] if record else "Ready"),line))
if not kit_written:
    lines.append("Kit-SHA: " + kit_sha)
if record:
    lines.extend(record["spec_verdicts"])
p.write_text("\n".join(lines)+"\n", encoding="utf-8")
PY
    git -C "$root/worktrees/$ticket" add "factory/tickets/$ticket.md"
    if ! git -C "$root/worktrees/$ticket" diff --cached --quiet; then
      git -C "$root/worktrees/$ticket" -c user.name='Factory Dev Lane' \
        -c user.email=factory-dev@local commit -qm "$ticket: prepare retained retry evidence"
    fi
    git -C "$root/worktrees/$ticket" push -q origin "HEAD:refs/heads/ticket/$ticket"
  done
}

write_product_checkpoint_import() {
  local root="$1" checkpoint="$2"
  python3 - "$checkpoint" "$root/runtime/product-checkpoint-import.json" \
    "$root/runtime/product-checkpoint-source.json" "$root" \
    "${PRODUCT_TICKETS[@]}" <<'PY'
import hashlib, json, os, pathlib, subprocess, sys
checkpoint_path, output, retained, root, *_tickets=sys.argv[1:]
raw=open(checkpoint_path,"rb").read()
source=json.loads(raw)
records=[]
for item in source["tickets"]:
    ticket=item["ticket"]
    work=pathlib.Path(root,"worktrees",ticket)
    git=lambda *args: subprocess.check_output(
        ["git","-C",str(work),*args],text=True).strip()
    records.append({
        "ticket":ticket,
        "import_head":git("rev-parse","HEAD"),
        "import_tree":git("rev-parse","HEAD^{tree}"),
        "roles":[run["role"] for run in item["roles"]],
        "spec_verdicts":item["spec_verdicts"],
        "expected_next_stage":item["next_stage"],
    })
value={
    "schema":"factory-dev-product-checkpoint-import/v1",
    "checkpoint_sha256":hashlib.sha256(raw).hexdigest(),
    "tickets":records,
}
retained_path=pathlib.Path(retained)
fd=os.open(retained_path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
with os.fdopen(fd,"wb") as stream: stream.write(raw)
path=pathlib.Path(output)
path.write_text(json.dumps(value,sort_keys=True,separators=(",",":"))+"\n",
                encoding="utf-8")
os.chmod(path,0o600)
PY
}

validate_product_seed_accounting() {
  local manifest="$1" bundle="$2" base="$3"; shift 3
  [[ "$manifest" == /* && -f "$manifest" && ! -L "$manifest" ]] ||
    die "product seed accounting must be an absolute regular file"
  refuse_production_path "$manifest"
  [[ "$(stat -f '%Su:%Lp:%l' "$manifest")" == "$(id -un):600:1" ]] || {
    die "product seed accounting must be owner-only"; return 1;
  }
  [[ "$bundle" == /* && -f "$bundle" && ! -L "$bundle" ]] ||
    die "product seed bundle must be an absolute regular file"
  refuse_production_path "$bundle"
  [[ "$(stat -f '%Su:%Lp:%l' "$bundle")" == "$(id -un):600:1" ]] || {
    die "product seed bundle must be owner-only"; return 1;
  }
  if ! python3 - "$manifest" "$(sha256_file "$bundle")" "$base" "$@" <<'PY'
import json, re, sys
path, bundle_sha, base, *tickets=sys.argv[1:]
value=json.load(open(path, encoding="utf-8"))
common={"schema","seed_bundle_sha256","base_sha","reserved_micro_usd"}
if (value.get("seed_bundle_sha256") != bundle_sha or
    value.get("base_sha") != base):
    raise SystemExit(1)
if value.get("schema") == "factory-dev-product-seed-accounting/v2":
    if set(value) != common: raise SystemExit(1)
    ticket_cap, aggregate_cap=100_000_000, 500_000_000
elif value.get("schema") == "factory-dev-product-seed-accounting/v3":
    if set(value) != common | {
        "ticket_cap_micro_usd","aggregate_cap_micro_usd","authorized_by",
        "authorization_nonce","budget_day"
    }: raise SystemExit(1)
    ticket_cap=value["ticket_cap_micro_usd"]
    aggregate_cap=value["aggregate_cap_micro_usd"]
    if (ticket_cap != 200_000_000 or aggregate_cap != 700_000_000 or
        value["authorized_by"] != "operator" or
        not re.fullmatch(r"[0-9a-f]{64}", value["authorization_nonce"]) or
        value["budget_day"] != __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        ).date().isoformat()):
        raise SystemExit(1)
elif value.get("schema") in (
    "factory-dev-product-seed-accounting/v4",
    "factory-dev-product-seed-accounting/v5",
):
    extra=set()
    if value["schema"].endswith("/v5"):
        extra={"checkpoint_sha256","parent_manifest_sha256",
               "checkpoint_charges_micro_usd"}
    if set(value) != common | {
        "ticket_caps_micro_usd","aggregate_cap_micro_usd","authorized_by",
        "authorization_nonce","budget_day"
    } | extra: raise SystemExit(1)
    ticket_caps=value["ticket_caps_micro_usd"]
    aggregate_cap=value["aggregate_cap_micro_usd"]
    if (not isinstance(ticket_caps, dict) or
        aggregate_cap not in (1_000_000_000, 1_500_000_000) or
        value["authorized_by"] != "operator" or
        not re.fullmatch(r"[0-9a-f]{64}", value["authorization_nonce"]) or
        value["budget_day"] != __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        ).date().isoformat()):
        raise SystemExit(1)
    if value["schema"].endswith("/v5"):
        digest=lambda item: isinstance(item,str) and re.fullmatch(r"[0-9a-f]{64}",item)
        charges=value["checkpoint_charges_micro_usd"]
        if (not digest(value["checkpoint_sha256"]) or
            value["parent_manifest_sha256"] is not None and
                not digest(value["parent_manifest_sha256"]) or
            not isinstance(charges,dict) or set(charges) != set(value["reserved_micro_usd"]) or
            any(not isinstance(amount,int) or isinstance(amount,bool) or amount < 0
                for amount in charges.values())):
            raise SystemExit(1)
else:
    raise SystemExit(1)
amounts=value.get("reserved_micro_usd")
if (not isinstance(amounts, dict) or not set(tickets) <= set(amounts) or
    any(not isinstance(ticket, str) or
        not re.fullmatch(r"T-[0-9]+", ticket) for ticket in amounts)):
    raise SystemExit(1)
for ticket, amount in amounts.items():
    if (not isinstance(amount, int) or isinstance(amount, bool) or
        amount < 0):
        raise SystemExit(1)
if value["schema"].endswith(("/v4","/v5")):
    if (set(ticket_caps) != set(amounts) or
        any(not isinstance(cap, int) or isinstance(cap, bool) or
            cap < 1 or cap > 350_000_000 for cap in ticket_caps.values())):
        raise SystemExit(1)
    if any(amount > ticket_caps[ticket] for ticket, amount in amounts.items()):
        raise SystemExit(1)
if value["schema"].endswith("/v3") and any(
    amount > ticket_cap for amount in amounts.values()
): raise SystemExit(1)
if any(amounts[ticket] >= (
    ticket_caps[ticket] if value["schema"].endswith(("/v4","/v5")) else ticket_cap
) for ticket in tickets): raise SystemExit(1)
if sum(amounts.values()) >= aggregate_cap: raise SystemExit(1)
PY
  then
    die "product seed accounting is invalid or exhausted"
  fi
}

validate_checkpoint_accounting() {
  local manifest="$1" checkpoint="$2"
  python3 - "$manifest" "$checkpoint" <<'PY' ||
import hashlib, json, sys
accounting=json.load(open(sys.argv[1],encoding="utf-8"))
checkpoint=json.load(open(sys.argv[2],encoding="utf-8"))
charges=checkpoint["lane_charges_micro_usd"]
if (accounting.get("schema") != "factory-dev-product-seed-accounting/v5" or
    accounting.get("checkpoint_sha256") !=
        hashlib.sha256(open(sys.argv[2],"rb").read()).hexdigest() or
    accounting.get("checkpoint_charges_micro_usd") != charges or
    accounting.get("parent_manifest_sha256") !=
        checkpoint.get("prior_accounting_sha256")):
    raise SystemExit(1)
PY
    die "product checkpoint accounting is detached or underreported"
}

prepare_product_seed_accounting() {
  local root="$1" manifest="$2" bundle="$3" base="$4"; shift 4
  validate_product_seed_accounting "$manifest" "$bundle" "$base" "$@"
  mkdir -m 700 "$root/runtime/product-envelope"
  python3 - "$manifest" "$root/product/factory/ENVELOPE.env" \
    "$root/runtime/product-envelope" "$@" <<'PY'
import json, os, pathlib, re, sys
manifest, base_path, output, *tickets=sys.argv[1:]
value=json.load(open(manifest, encoding="utf-8"))
amounts=value["reserved_micro_usd"]
if value["schema"].endswith(("/v3", "/v4", "/v5")):
    if (value["schema"].endswith("/v3") and
        (value.get("ticket_cap_micro_usd") != 200_000_000 or
         value.get("aggregate_cap_micro_usd") != 700_000_000) or
        value["schema"].endswith(("/v4", "/v5")) and
         value.get("aggregate_cap_micro_usd") not in
            (1_000_000_000, 1_500_000_000) or
        value.get("authorized_by") != "operator"):
        raise SystemExit(1)
    ticket_caps=value.get("ticket_caps_micro_usd")
    ticket_cap=value.get("ticket_cap_micro_usd")
    aggregate_cap=value["aggregate_cap_micro_usd"]
    day=pathlib.Path(output,"budget-day")
    day.write_text(value["budget_day"]+"\n",encoding="utf-8"); os.chmod(day,0o600)
else:
    ticket_caps=None
    ticket_cap,aggregate_cap=100_000_000,500_000_000
base=pathlib.Path(base_path).read_text(encoding="utf-8")
for ticket in tickets:
    cap=ticket_caps[ticket] if ticket_caps is not None else ticket_cap
    remaining=(cap-amounts[ticket])/1_000_000
    text,count=re.subn(r"(?m)^PER_TICKET_BUDGET_USD=.*$",
                       f"PER_TICKET_BUDGET_USD={remaining:.6f}",base,count=1)
    if count != 1: raise SystemExit(1)
    path=pathlib.Path(output,ticket+".env")
    path.write_text(text,encoding="utf-8"); os.chmod(path,0o600)
remaining=aggregate_cap-sum(amounts.values())
path=pathlib.Path(output,"global.env")
path.write_text(f"GLOBAL_DAILY_CAP_USD={remaining/1_000_000:.6f}\n",encoding="utf-8")
os.chmod(path,0o600)
PY
}

consume_product_seed_authorization() {
  local manifest="$1" expected="$2" lineage_record="$3"
  local parent root digest nonce day marker lineage_id lineage_parent lineage_values
  local accounting_values expected_lineage lineage lock head checkpoint_digest accounting_parent
  [[ "$lineage_record" == /* && -f "$lineage_record" && ! -L "$lineage_record" &&
     "$(stat -f '%Su:%Lp:%l' "$lineage_record")" == "$(id -un):600:1" ]] ||
    die "product seed lineage must be an owner-only regular file"
  refuse_production_path "$lineage_record"
  parent="$(physical "$(dirname "$lineage_record")")"
  refuse_production_path "$parent"
  [[ "$(stat -f '%Su:%Lp' "$parent")" == "$(id -un):700" ]] ||
    die "product seed lineage parent must be owner-only"
  digest="$(sha256_file "$manifest")"
  [[ "$digest" == "$expected" ]] ||
    die "product seed authorization drifted after lane creation"
  lineage_values="$(python3 - "$lineage_record" "$digest" <<'PY'
import json, sys
value=json.load(open(sys.argv[1], encoding="utf-8"))
sha=lambda item: isinstance(item,str) and len(item)==64 and all(c in "0123456789abcdef" for c in item)
if (set(value) != {"schema","lineage_id","parent_manifest_sha256","manifest_sha256"} or
    value.get("schema") != "factory-dev-product-seed-lineage/v1" or
    not sha(value.get("lineage_id")) or
    value.get("parent_manifest_sha256") is not None and
        not sha(value.get("parent_manifest_sha256")) or
    value.get("manifest_sha256") != sys.argv[2]):
    raise SystemExit(1)
print(value["lineage_id"], value["parent_manifest_sha256"] or "none")
PY
  )" || die "product seed lineage is malformed or detached"
  read -r lineage_id lineage_parent <<<"$lineage_values"
  expected_lineage="$(product_seed_lineage_id "$manifest")" ||
    die "product seed accounting scope is malformed"
  [[ "$lineage_id" == "$expected_lineage" ]] ||
    die "product seed lineage identity does not match accounting scope"
  accounting_values="$(python3 - "$manifest" "$digest" <<'PY'
import hashlib, json, sys
value=json.load(open(sys.argv[1], encoding="utf-8"))
print(value.get("authorization_nonce", hashlib.sha256(sys.argv[2].encode()).hexdigest()),
      value.get("budget_day", "legacy"), value.get("checkpoint_sha256","none"),
      value.get("parent_manifest_sha256") or "none",
      "v5" if value.get("schema","").endswith("/v5") else "legacy")
PY
  )" || die "product seed accounting is malformed"
  read -r nonce day checkpoint_digest accounting_parent accounting_schema <<<"$accounting_values"
  [[ "$accounting_schema" != v5 || "$accounting_parent" == "$lineage_parent" ]] ||
    die "product seed accounting parent does not match its lineage"
  root="$parent/.seed-accounting-lineages"
  if [[ ! -e "$root" ]]; then mkdir -m 700 "$root" 2>/dev/null || true; fi
  [[ -d "$root" && ! -L "$root" &&
     "$(stat -f '%Su:%Lp' "$root")" == "$(id -un):700" ]] ||
    die "product seed accounting lineage root is unsafe"
  lock="$root/$lineage_id.lock"
  mkdir -m 700 "$lock" 2>/dev/null ||
    die "product seed accounting lineage is busy"
  if ! (
    trap 'rmdir "$lock" 2>/dev/null || true' EXIT
    lineage="$root/$lineage_id"
    if [[ ! -e "$lineage" ]]; then
      [[ "$lineage_parent" == none ]] || exit 1
      mkdir -m 700 "$lineage"
      mkdir -m 700 "$lineage/nonces"
      mkdir -m 700 "$lineage/checkpoints"
    fi
    if [[ ! -e "$lineage/checkpoints" ]]; then
      mkdir -m 700 "$lineage/checkpoints" || exit 1
    fi
    [[ -d "$lineage" && ! -L "$lineage" &&
       "$(stat -f '%Su:%Lp' "$lineage")" == "$(id -un):700" &&
       -d "$lineage/nonces" && ! -L "$lineage/nonces" &&
       "$(stat -f '%Su:%Lp' "$lineage/nonces")" == "$(id -un):700" &&
       -d "$lineage/checkpoints" && ! -L "$lineage/checkpoints" &&
       "$(stat -f '%Su:%Lp' "$lineage/checkpoints")" == "$(id -un):700" ]] || exit 1
    head="$lineage/head"
    if [[ "$lineage_parent" == none ]]; then
      [[ ! -e "$head" ]] || exit 1
    else
      [[ -f "$head" && ! -L "$head" &&
         "$(stat -f '%Su:%Lp:%l' "$head")" == "$(id -un):600:1" &&
         "$(sed -n '1p' "$head")" == "$lineage_parent" ]] || exit 1
    fi
    marker="$lineage/nonces/$nonce.used"
    mkdir -m 700 "$marker" 2>/dev/null || exit 1
    if [[ "$checkpoint_digest" != none ]]; then
      mkdir -m 700 "$lineage/checkpoints/$checkpoint_digest.used" 2>/dev/null ||
        exit 1
      printf 'schema=factory-dev-product-checkpoint-consumption/v1\ncheckpoint_sha256=%s\nmanifest_sha256=%s\n' \
        "$checkpoint_digest" "$digest" \
        >"$lineage/checkpoints/$checkpoint_digest.used/receipt"
      chmod 600 "$lineage/checkpoints/$checkpoint_digest.used/receipt"
    fi
    printf 'schema=factory-dev-product-seed-authorization-consumption/v1\nmanifest_sha256=%s\nbudget_day=%s\n' \
      "$digest" "$day" >"$marker/receipt"
    chmod 600 "$marker/receipt"
    printf '%s\n' "$digest" >"$marker/head"
    chmod 600 "$marker/head"
    mv "$marker/head" "$head"
  ); then
    die "product seed accounting lineage is stale or already consumed"
  fi
}

product_seed_lineage_id() {
  python3 - "$1" <<'PY'
import hashlib, json, re, sys
value=json.load(open(sys.argv[1], encoding="utf-8"))
base=value.get("base_sha")
reserved=value.get("reserved_micro_usd")
if (not isinstance(base,str) or not re.fullmatch(r"[0-9a-f]{40}",base) or
    not isinstance(reserved,dict) or not reserved or
    any(not re.fullmatch(r"T-[0-9]+", ticket) for ticket in reserved)):
    raise SystemExit(1)
scope=json.dumps({"base_sha":base,"tickets":sorted(reserved)},
                 sort_keys=True,separators=(",",":"))
print(hashlib.sha256(("factory-dev-product-seed-lineage/v1\0"+scope).encode()).hexdigest())
PY
}

write_product_seed_lineage() {
  local manifest="$1" output="$2" parent_manifest="${3:-}"
  local artifact_dir digest lineage_id parent_digest=null parent_lineage
  [[ "$manifest" == /* && -f "$manifest" && ! -L "$manifest" &&
     "$(stat -f '%Su:%Lp:%l' "$manifest")" == "$(id -un):600:1" ]] ||
    die "product seed accounting must be an owner-only regular file"
  [[ "$output" == /* && ! -e "$output" && ! -L "$output" ]] ||
    die "product seed lineage output must be a new absolute path"
  refuse_production_path "$manifest"; refuse_production_path "$output"
  artifact_dir="$(physical "$(dirname "$output")")"
  [[ "$artifact_dir" == "$(physical "$(dirname "$manifest")")" &&
     "$(stat -f '%Su:%Lp' "$artifact_dir")" == "$(id -un):700" ]] ||
    die "product seed lineage artifacts must share one owner-only directory"
  digest="$(sha256_file "$manifest")"
  lineage_id="$(product_seed_lineage_id "$manifest")" ||
    die "product seed accounting scope is malformed"
  if [[ -n "$parent_manifest" ]]; then
    [[ "$parent_manifest" == /* && -f "$parent_manifest" && ! -L "$parent_manifest" &&
       "$(stat -f '%Su:%Lp:%l' "$parent_manifest")" == "$(id -un):600:1" &&
       "$(physical "$(dirname "$parent_manifest")")" == "$artifact_dir" ]] ||
      die "parent seed accounting must be an owner-only sibling file"
    refuse_production_path "$parent_manifest"
    parent_lineage="$(product_seed_lineage_id "$parent_manifest")" ||
      die "parent seed accounting scope is malformed"
    [[ "$parent_lineage" == "$lineage_id" ]] ||
      die "parent seed accounting belongs to a different lineage"
    parent_digest="\"$(sha256_file "$parent_manifest")\""
  fi
  python3 - "$manifest" "$parent_manifest" <<'PY' ||
import hashlib, json, sys
current=json.load(open(sys.argv[1],encoding="utf-8"))
if current.get("schema") != "factory-dev-product-seed-accounting/v5":
    raise SystemExit(0)
parent_path=sys.argv[2]
expected=current["parent_manifest_sha256"]
charges=current["checkpoint_charges_micro_usd"]
if parent_path:
    raw=open(parent_path,"rb").read()
    if expected != hashlib.sha256(raw).hexdigest(): raise SystemExit(1)
    parent=json.loads(raw)
    previous=parent["reserved_micro_usd"]
else:
    if expected is not None: raise SystemExit(1)
    previous={ticket:0 for ticket in current["reserved_micro_usd"]}
if (set(previous) != set(current["reserved_micro_usd"]) or
    any(current["reserved_micro_usd"][ticket] != previous[ticket]+charges[ticket]
        for ticket in previous)):
    raise SystemExit(1)
PY
    die "checkpoint accounting is not the exact cumulative successor"
  (umask 077
   set -o noclobber
   printf '%s\n' \
     "{\"schema\":\"factory-dev-product-seed-lineage/v1\",\"lineage_id\":\"$lineage_id\",\"parent_manifest_sha256\":$parent_digest,\"manifest_sha256\":\"$digest\"}" \
     >"$output") || die "could not create product seed lineage"
  chmod 600 "$output"
}

append_commit_push() {
  local root="$1" line="$2" message="$3" work
  work="$root/worktrees/$TICKET"
  printf '%s\n' "$line" >> "$work/factory/tickets/$TICKET.md"
  git -C "$work" add "factory/tickets/$TICKET.md"
  git -C "$work" -c user.name='Factory Dev Lane' -c user.email=factory-dev@local \
    commit -qm "$message"
  git -C "$work" push -q origin "HEAD:refs/heads/ticket/$TICKET"
}

set_review_state() {
  local root="$1" work ticket
  work="$root/worktrees/$TICKET"
  ticket="$work/factory/tickets/$TICKET.md"
  python3 - "$ticket" <<'PY'
from pathlib import Path
import re, sys
p=Path(sys.argv[1]); text=p.read_text()
text, n=re.subn(r"^State:\s*.*$", "State: Review", text, count=1, flags=re.M)
if n != 1: raise SystemExit(1)
p.write_text(text)
PY
  if [[ -n "$(git -C "$work" status --porcelain -- "factory/tickets/$TICKET.md")" ]]; then
    git -C "$work" add "factory/tickets/$TICKET.md"
    git -C "$work" -c user.name='Factory Dev Lane' -c user.email=factory-dev@local \
      commit -qm "$TICKET: enter review"
    git -C "$work" push -q origin "HEAD:refs/heads/ticket/$TICKET"
  fi
}

lane_env() {
  local root="$1" project; shift
  project="factory-dev-lane-$(basename "$root" | sed 's/^nysa-sf-dev\.//' | tr '[:upper:]' '[:lower:]')"
  env -i HOME="$root/home" TMPDIR="$root/tmp" LANG=C LC_ALL=C \
    PATH="$root/home:/usr/bin:/bin:/usr/sbin:/sbin" \
    FACTORY_ROOT="$root/product" FACTORY_GLOBAL_ENV="$root/home/.factory/global.env" \
    FACTORY_MODEL_STATE_ROOT="$root/runtime/model-state" FACTORY_PROJECT="$project" \
    FACTORY_PROVIDER_DB="$root/runtime/provider-state.sqlite3" \
    FACTORY_PROVIDER_POLICY="$root/runtime/provider-policy.json" \
    FACTORY_PROVIDER_ACTIVATION="$root/runtime/provider-activation.json" \
    FACTORY_CLI_LANE_ROOT="$root" FACTORY_CLI_INTERNAL_SANDBOX=1 \
    FACTORY_CERTIFIED_PRODUCT_ORIGIN="$root/origin.git" \
    FACTORY_HERMES_CONTRACT_VERSION=1.7.0 "$@"
}

lane_cursor_env() {
  local root="$1" project; shift
  project="factory-dev-lane-$(basename "$root" | sed 's/^nysa-sf-dev\.//' | tr '[:upper:]' '[:lower:]')"
  env -i HOME="$root/home" TMPDIR="$root/tmp" \
    LANG=C LC_ALL=C PATH="$root/home:/usr/bin:/bin:/usr/sbin:/sbin" \
    FACTORY_ROOT="$root/product" FACTORY_GLOBAL_ENV="$root/home/.factory/global.env" \
    FACTORY_MODEL_STATE_ROOT="$root/runtime/model-state" FACTORY_PROJECT="$project" \
    FACTORY_CURSOR_SESSION_HOME="${FACTORY_CURSOR_SESSION_HOME:-}" \
    FACTORY_CURSOR_INTERNAL_SANDBOX=1 \
    FACTORY_CERTIFIED_PRODUCT_ORIGIN="$root/origin.git" \
    FACTORY_HERMES_CONTRACT_VERSION=1.6.0 "$@"
}

subscription_env() {
  local root="$1" cursor_version codex_version claude_version
  shift
  cursor_version="$(subscription_base_env "$root" \
    "$root/home/agent" --version 2>/dev/null | awk 'NF {print $NF; exit}')"
  codex_version="$(subscription_base_env "$root" \
    "$root/home/codex" --version 2>/dev/null | awk 'NF {print $NF; exit}')"
  claude_version="$(subscription_base_env "$root" \
    "$root/home/claude" --version 2>/dev/null | awk 'NF {print $1; exit}')"
  subscription_base_env "$root" env \
    CURSOR_AGENT_VERSION="$cursor_version" CODEX_PINNED="$codex_version" \
    CLAUDE_CODE_PINNED="$claude_version" \
    "$@"
}

codex_subscription_env() {
  local root="$1" codex_version
  shift
  codex_version="$(subscription_base_env "$root" \
    "$root/home/codex" --version 2>/dev/null | awk 'NF {print $NF; exit}')"
  subscription_base_env "$root" env CODEX_PINNED="$codex_version" "$@"
}

claude_subscription_env() {
  local root="$1" claude_version
  shift
  claude_version="$(subscription_base_env "$root" \
    "$root/home/claude" --version 2>/dev/null | awk 'NF {print $1; exit}')"
  subscription_base_env "$root" env CLAUDE_CODE_PINNED="$claude_version" "$@"
}

product_approval_hash() {
  local root="$1" ticket tool real session_home="$1/session-home" cursor_home
  cursor_home="$session_home"
  {
    sha256_file "$root/marker.json"
    sha256_file "$root/runtime/product-source.json"
    git -C "$root/kit" rev-parse HEAD 'HEAD^{tree}'
    sha256_file "$root/home/.factory/global.env"
    sha256_file "$root/runtime/provider-policy.json"
    sha256_file "$root/runtime/provider-activation.json"
    sha256_file "$root/runtime/cursor.sb"
    sha256_file "$root/runtime/native.sb"
    sha256_file "$root/runtime/claude-settings.json"
    sha256_file "$root/runtime/product-containers.json"
    sha256_file "$root/runtime/docker-host"
    [[ ! -f "$root/runtime/product-envelope/budget-day" ]] ||
      sha256_file "$root/runtime/product-envelope/budget-day"
    [[ ! -f "$root/runtime/product-checkpoint-import.json" ]] ||
      sha256_file "$root/runtime/product-checkpoint-import.json"
    [[ ! -f "$root/runtime/product-checkpoint-source.json" ]] ||
      sha256_file "$root/runtime/product-checkpoint-source.json"
    printf '%s\n' "$(python3 - "$root/runtime/docker" <<'PY'
import os, sys
print(os.path.realpath(sys.argv[1]))
PY
)" "$(sha256_file "$root/runtime/docker")"
    for tool in agent codex claude; do
      real="$(python3 - "$root/home/$tool" <<'PY'
import os, sys
print(os.path.realpath(sys.argv[1]))
PY
)"
      printf '%s\n' "$real" "$(sha256_file "$real")" \
        "$(subscription_base_env "$root" "$root/home/$tool" --version 2>/dev/null | head -n1)"
    done
    sha256_file "$cursor_home/.cursor/auth.json"
    sha256_file "$cursor_home/.cursor/cli-config.json"
    sha256_file "$session_home/.codex/auth.json"
    sha256_file "$session_home/.claude/.credentials.json"
    for ticket in "${PRODUCT_TICKETS[@]}"; do
      git -C "$root/worktrees/$ticket" rev-parse HEAD 'HEAD^{tree}'
      sha256_file "$root/worktrees/$ticket/factory/tickets/$ticket.md"
      sha256_file "$root/worktrees/$ticket/factory/route-plans/$ticket.json"
      sha256_file "$root/runtime/product-db/$ticket.env"
      [[ ! -f "$root/runtime/product-envelope/$ticket.env" ]] ||
        sha256_file "$root/runtime/product-envelope/$ticket.env"
    done
  } | sha256_text
}

provision_product_databases() {
  local root="$1" ticket nonce name password database port env_file
  local -a names=()
  [[ -x "$root/runtime/docker" && -f "$root/runtime/docker-host" ]] ||
    die "Docker is unavailable for isolated product databases"
  mkdir -m 700 "$root/runtime/product-db"
  nonce="$(basename "$root" | sed 's/^nysa-sf-dev\.//' | tr '[:upper:]' '[:lower:]')"
  for ticket in "${PRODUCT_TICKETS[@]}"; do
    name="nysa-sfdev-$nonce-$(printf '%s' "$ticket" | tr '[:upper:]' '[:lower:]')"
    password="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
    database="nysa_$(printf '%s' "${ticket#T-}" | tr -cd '0-9')"
    env_file="$root/runtime/product-db/$ticket.env"
    printf 'POSTGRES_PASSWORD=%s\nPOSTGRES_DB=%s\n' "$password" "$database" >"$env_file"
    chmod 600 "$env_file"
    DOCKER_HOST="$(cat "$root/runtime/docker-host")" "$root/runtime/docker" run -d --name "$name" \
      --label "nysa.factory.dev-lane-root=$root" --env-file "$env_file" \
      -p 127.0.0.1::5432 pgvector/pgvector:0.8.5-pg16 >/dev/null ||
      die "could not start isolated database for $ticket"
    names+=("$name")
    port="$(DOCKER_HOST="$(cat "$root/runtime/docker-host")" "$root/runtime/docker" \
      port "$name" 5432/tcp | sed -n 's/.*://p' | tail -n1)"
    [[ "$port" =~ ^[0-9]+$ ]] || die "could not resolve isolated database port"
    printf 'POSTGRES_PORT=%s\nDATABASE_URL=postgresql://postgres:%s@127.0.0.1:%s/%s\n' \
      "$port" "$password" "$port" "$database" >>"$env_file"
  done
  python3 - "$root/runtime/product-containers.json" "$root" "${names[@]}" <<'PY'
import json, os, sys
path, root, *names=sys.argv[1:]
with open(path,"w",encoding="utf-8") as stream:
    json.dump({"schema":"factory-dev-product-containers/v1","root":root,
               "containers":[{"name":name} for name in names]},stream,
              sort_keys=True,separators=(",",":")); stream.write("\n")
os.chmod(path,0o600)
PY
}

load_product_tickets() {
  local root="$1" line serialized
  PRODUCT_TICKETS=()
  serialized="$(python3 - "$root/runtime/product-source.json" <<'PY'
import json, re, sys
value=json.load(open(sys.argv[1], encoding="utf-8"))
tickets=value.get("tickets", [])
if (value.get("schema") != "factory-dev-product-source/v1" or
    not 1 <= len(tickets) <= 4 or len(set(tickets)) != len(tickets) or
    any(not isinstance(ticket, str) or
        not re.fullmatch(r"T-[0-9]+", ticket) for ticket in tickets)):
    raise SystemExit(1)
for ticket in tickets:
    print(ticket)
PY
  )" || die "product source binding is malformed"
  while IFS= read -r line; do
    [[ -n "$line" ]] && PRODUCT_TICKETS+=("$line")
  done <<<"$serialized"
  [[ "${#PRODUCT_TICKETS[@]}" -ge 1 && "${#PRODUCT_TICKETS[@]}" -le 4 ]] ||
    die "product source binding is incomplete"
}

load_product_resume_original_tickets() {
  local root="$1" line serialized
  PRODUCT_RESUME_ORIGINAL_TICKETS=()
  serialized="$(python3 - "$root/runtime/product-source.json" <<'PY'
import json, re, sys
value=json.load(open(sys.argv[1], encoding="utf-8"))
tickets=value.get("resume_original_tickets", value.get("tickets", []))
if (not 1 <= len(tickets) <= 4 or len(set(tickets)) != len(tickets) or
    any(not isinstance(ticket, str) or
        not re.fullmatch(r"T-[0-9]+", ticket) for ticket in tickets)):
    raise SystemExit(1)
for ticket in tickets: print(ticket)
PY
  )" || die "product resume original ticket binding is malformed"
  while IFS= read -r line; do
    [[ -n "$line" ]] && PRODUCT_RESUME_ORIGINAL_TICKETS+=("$line")
  done <<<"$serialized"
}

product_resume_evidence_digest() {
  local root="$1"
  python3 - "$root" <<'PY'
import hashlib, pathlib, sys
root=pathlib.Path(sys.argv[1]); digest=hashlib.sha256()
paths=[]
for item in (
    root/"product/factory/runtime-ledger.csv",
    root/"runtime/product-approval.used",
):
    if item.is_file(): paths.append(item)
for pattern in (
    "product/factory/runs/*",
    "runtime/product-discarded/*",
):
    paths.extend(path for path in root.glob(pattern) if path.is_file())
for path in sorted(paths, key=lambda item: str(item.relative_to(root))):
    relative=str(path.relative_to(root)).encode()
    digest.update(len(relative).to_bytes(8,"big")); digest.update(relative)
    data=path.read_bytes()
    digest.update(len(data).to_bytes(8,"big")); digest.update(data)
print(digest.hexdigest())
PY
}

product_resume_stage() {
  local root="$1" ticket="$2" lease_json lease stage rc=0
  lease_json="$(subscription_env "$root" "$root/kit/scripts/dispatch-lease.sh" \
    claim --ticket "$ticket")" || return 1
  lease="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["lease_id"])' \
    <<<"$lease_json")" || return 1
  stage="$(
    TICKET="$ticket"
    product_reconcile_reviewer "$root" "$ticket" "$lease" &&
      next_stage "$root" "$lease"
  )" || rc=$?
  subscription_env "$root" "$root/kit/scripts/dispatch-lease.sh" release \
    --ticket "$ticket" --lease "$lease" >/dev/null || return 1
  [[ "$rc" -eq 0 ]] || return "$rc"
  printf '%s\n' "$stage"
}

product_resume_envelope_binding() {
  local root="$1" ticket="$2"
  python3 - "$root" "$ticket" <<'PY'
import hashlib, os, stat, sys
root, ticket=sys.argv[1:]
override=os.path.join(root,"runtime","product-envelope",ticket+".env")
fallback=os.path.join(root,"runtime","product-envelope","global.env")
path=override if os.path.lexists(override) else fallback
try:
    before=os.lstat(path)
    if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or
        stat.S_IMODE(before.st_mode) != 0o600 or before.st_nlink != 1):
        raise ValueError
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    with os.fdopen(fd,"rb") as stream:
        after=os.fstat(stream.fileno())
        if (before.st_dev,before.st_ino,before.st_mode,before.st_nlink) != (
            after.st_dev,after.st_ino,after.st_mode,after.st_nlink
        ): raise ValueError
        digest=hashlib.sha256(stream.read()).hexdigest()
except (AttributeError, OSError, ValueError):
    raise SystemExit(1)
print("envelope_source="+os.path.relpath(path,root))
print("envelope_sha256="+digest)
PY
}

product_resume_basis_hash() {
  local root="$1"; shift
  local ticket stage status evidence
  local -a selected=("$@")
  [[ -z "$(git -C "$SOURCE_ROOT" status --porcelain --untracked-files=all)" ]] ||
    return 1
  if [[ "${#selected[@]}" -eq 0 ]]; then
    load_product_tickets "$root"
    selected=("${PRODUCT_TICKETS[@]}")
  fi
  load_product_resume_original_tickets "$root"
  status="$(python3 "$root/kit/scripts/provider-coordinator.py" \
    --db "$root/runtime/provider-state.sqlite3" status | python3 -c '
import json, sys
value=json.load(sys.stdin)
print(json.dumps(value,sort_keys=True,separators=(",",":")))
')" || return 1
  evidence="$(product_resume_evidence_digest "$root")" || return 1
  {
    printf 'schema=factory-dev-product-resume-basis/v1\n'
    printf 'resume_controller=%s\nresume_controller_tree=%s\n' \
      "$(git -C "$SOURCE_ROOT" rev-parse HEAD)" \
      "$(git -C "$SOURCE_ROOT" rev-parse 'HEAD^{tree}')"
    python3 - "$root/runtime/product-source.json" <<'PY'
import json, sys
v=json.load(open(sys.argv[1], encoding="utf-8"))
for key in ("schema","base_sha","base_tree","lane_control_sha",
            "seed_bundle_sha256","seed_accounting_sha256","seed_lineage_sha256",
            "seed_checkpoint_sha256"):
    print(f"{key}={json.dumps(v.get(key),sort_keys=True,separators=(',',':'))}")
PY
    printf 'provider_status=%s\nevidence_sha256=%s\n' "$status" "$evidence"
    for path in \
      "$root/runtime/provider-policy.json" \
      "$root/runtime/provider-activation.json" \
      "$root/runtime/product-containers.json" \
      "$root/runtime/product-envelope/global.env" \
      "$root/runtime/product-envelope/budget-day" \
      "$root/runtime/product-checkpoint-import.json" \
      "$root/runtime/product-checkpoint-source.json"; do
      [[ -f "$path" ]] || continue
      printf '%s=%s\n' "${path#$root/}" "$(sha256_file "$path")"
    done
    for ticket in "${PRODUCT_RESUME_ORIGINAL_TICKETS[@]}"; do
      [[ -z "$(git -C "$root/worktrees/$ticket" \
        status --porcelain --untracked-files=all)" ]] || return 1
      [[ "$(git -C "$root/worktrees/$ticket" rev-parse HEAD)" == \
        "$(git -C "$root/origin.git" rev-parse "refs/heads/ticket/$ticket")" ]] ||
        return 1
      printf 'ticket=%s\nhead=%s\norigin=%s\ntree=%s\n' \
        "$ticket" \
        "$(git -C "$root/worktrees/$ticket" rev-parse HEAD)" \
        "$(git -C "$root/origin.git" rev-parse "refs/heads/ticket/$ticket")" \
        "$(git -C "$root/worktrees/$ticket" rev-parse 'HEAD^{tree}')"
      if printf '%s\n' "${selected[@]}" | grep -Fxq "$ticket"; then
        stage="$(product_resume_stage "$root" "$ticket")" || return 1
        printf 'selected=1\nstage=%s\n' "$stage"
      else
        printf 'selected=0\n'
      fi
      printf 'ticket_file=%s\nroute_plan=%s\n' \
        "$(sha256_file "$root/worktrees/$ticket/factory/tickets/$ticket.md")" \
        "$(sha256_file "$root/worktrees/$ticket/factory/route-plans/$ticket.json")"
      product_resume_envelope_binding "$root" "$ticket" || return 1
    done
  } | sha256_text
}

product_resume_drained() {
  local root="$1" approval_ready="${2:-0}" container label state
  [[ -f "$root/runtime/product-approval.used" &&
     ! -L "$root/runtime/product-approval.used" &&
     "$(stat -f '%Su:%Lp:%l' "$root/runtime/product-approval.used")" == \
       "$(id -un):600:1" ]] || return 1
  python3 - "$root/runtime/product-approval.used" <<'PY' || return 1
import re, sys
value=dict(line.split("=",1) for line in open(sys.argv[1],encoding="utf-8").read().splitlines())
if (set(value) != {"approval_hash","used"} or
    not re.fullmatch(r"[0-9a-f]{64}", value["approval_hash"]) or
    value["used"] != "0"): raise SystemExit(1)
PY
  if [[ "$approval_ready" -eq 1 ]]; then
    [[ -f "$root/runtime/product-approval" &&
       ! -L "$root/runtime/product-approval" &&
       "$(stat -f '%Su:%Lp:%l' "$root/runtime/product-approval")" == \
         "$(id -un):600:1" ]] || return 1
  else
    [[ ! -e "$root/runtime/product-approval" ]] || return 1
  fi
  python3 "$root/kit/scripts/provider-coordinator.py" \
    --db "$root/runtime/provider-state.sqlite3" status | python3 -c '
import json, sys
v=json.load(sys.stdin)
assert v.get("active_reserve_micro_usd") == 0, v
assert all(item.get("state") == "terminal" for item in v.get("attempts", [])), v
assert all(name == "terminal" for name in v.get("counts", {})), v
' || return 1
  [[ ! -d "$root/product/factory/.dispatch-leases" ||
     -z "$(find "$root/product/factory/.dispatch-leases" -type f -print -quit)" ]] ||
    return 1
  [[ ! -d "$root/product/factory/.active-runs" ||
     -z "$(find "$root/product/factory/.active-runs" -mindepth 1 -print -quit)" ]] ||
    return 1
  python3 - "$root" <<'PY' || return 1
import os, pathlib, re, sys
for path in pathlib.Path(sys.argv[1]).rglob("*.pid"):
    try: text=path.read_text(encoding="utf-8")
    except (OSError, UnicodeError): raise SystemExit(1)
    match=re.search(r"(?:^|\n)pid=([1-9][0-9]*)(?:\n|$)", text)
    if match is None and text.strip().isdigit():
        match=re.match(r"([1-9][0-9]*)", text.strip())
    if match is None: continue
    try: os.kill(int(match.group(1)), 0)
    except ProcessLookupError: continue
    except PermissionError: raise SystemExit(1)
    raise SystemExit(1)
PY
  [[ "$(cat "$root/runtime/product-envelope/budget-day")" == "$(date -u +%F)" ]] ||
    return 1
  while IFS= read -r container; do
    [[ -n "$container" ]] || continue
    read -r label state < <(DOCKER_HOST="$(cat "$root/runtime/docker-host")" \
      "$root/runtime/docker" inspect --format \
      '{{ index .Config.Labels "nysa.factory.dev-lane-root" }} {{.State.Status}}' \
      "$container" 2>/dev/null) || return 1
    [[ "$label" == "$root" && "$state" == running ]] || return 1
  done < <(python3 - "$root/runtime/product-containers.json" <<'PY'
import json, sys
v=json.load(open(sys.argv[1], encoding="utf-8"))
for item in v.get("containers", []): print(item["name"])
PY
  )
  subscription_provider_idle
}

validate_product_resume_basis() {
  local root="$1" approval_ready="${2:-0}" expected actual
  [[ -f "$root/runtime/product-resume.json" &&
     ! -L "$root/runtime/product-resume.json" &&
     "$(stat -f '%Su:%Lp:%l' "$root/runtime/product-resume.json")" == \
       "$(id -un):600:1" ]] || return 1
  expected="$(python3 - "$root/runtime/product-source.json" \
    "$root/runtime/product-resume.json" <<'PY'
import hashlib, json, sys
source=json.load(open(sys.argv[1],encoding="utf-8"))
raw=open(sys.argv[2],"rb").read()
value=json.loads(raw)
if (value.get("schema") != "factory-dev-product-resume/v1" or
    source.get("resume_sha256") != hashlib.sha256(raw).hexdigest() or
    source.get("tickets") != value.get("selected_tickets") or
    source.get("resume_original_tickets") != value.get("original_tickets")):
    raise SystemExit(1)
print(value["basis_sha256"])
PY
  )" || return 1
  product_resume_drained "$root" "$approval_ready" || return 1
  actual="$(product_resume_basis_hash "$root")" || return 1
  [[ "$actual" == "$expected" ]]
}

product_resume_plan() {
  local root="$1"; shift
  local selected_csv="$1" ticket stage basis approval_hash
  local -a selected
  require_lane_mode "$root" product
  load_product_tickets "$root"
  load_product_resume_original_tickets "$root"
  validate_runtime_paths "$root"
  IFS=, read -r -a selected <<<"$selected_csv"
  [[ "${#selected[@]}" -ge 1 &&
     "${#selected[@]}" -le "${#PRODUCT_RESUME_ORIGINAL_TICKETS[@]}" ]] ||
    die "product resume selection is empty or wider than the active lane"
  python3 - "${PRODUCT_RESUME_ORIGINAL_TICKETS[@]}" -- "${selected[@]}" <<'PY' ||
import re, sys
original=sys.argv[1:sys.argv.index("--")]; selected=sys.argv[sys.argv.index("--")+1:]
if (len(set(selected)) != len(selected) or not set(selected) <= set(original) or
    any(not re.fullmatch(r"T-[0-9]+", ticket) for ticket in selected)):
    raise SystemExit(1)
PY
    die "product resume selection is not a strict active-lane subset"
  product_resume_drained "$root" ||
    die "product resume requires a fully drained, current-day lane"
  ensure_cursor_file_credential_config "$root"
  subscription_ready "$root"
  for ticket in "${selected[@]}"; do
    stage="$(product_resume_stage "$root" "$ticket")" ||
      die "product resume could not resolve the current stage: $ticket"
    product_role_for_stage "$stage" >/dev/null ||
      die "selected product resume ticket is not runnable: $ticket"
  done
  basis="$(product_resume_basis_hash "$root" "${selected[@]}")" ||
    die "product resume basis could not be proven"
  python3 - "$root/runtime/product-source.json" \
    "$root/runtime/product-resume.json" "$basis" -- \
    "${PRODUCT_RESUME_ORIGINAL_TICKETS[@]}" -- "${selected[@]}" <<'PY'
import json, os, pathlib, sys
source_path, resume_path, basis, sep1, *rest=sys.argv[1:]
split=rest.index("--"); original=rest[:split]; selected=rest[split+1:]
if sep1 != "--": raise SystemExit(1)
source=json.load(open(source_path,encoding="utf-8"))
prior=source.get("resume_sha256")
value={"schema":"factory-dev-product-resume/v1","basis_sha256":basis,
       "original_tickets":original,"selected_tickets":selected,
       "prior_resume_sha256":prior}
raw=json.dumps(value,sort_keys=True,separators=(",",":"))+"\n"
path=pathlib.Path(resume_path); tmp=path.with_name(path.name+".tmp")
tmp.write_text(raw,encoding="utf-8"); os.chmod(tmp,0o600); os.replace(tmp,path)
source["resume_original_tickets"]=original
source["tickets"]=selected
source["resume_sha256"]=__import__("hashlib").sha256(raw.encode()).hexdigest()
raw=json.dumps(source,sort_keys=True,separators=(",",":"))+"\n"
path=pathlib.Path(source_path); tmp=path.with_name(path.name+".tmp")
tmp.write_text(raw,encoding="utf-8"); os.chmod(tmp,0o600); os.replace(tmp,path)
PY
  load_product_tickets "$root"
  validate_product_resume_basis "$root" ||
    die "product resume basis drifted while planning"
  approval_hash="$(product_approval_hash "$root")"
  python3 - "$root/runtime/product-approval" "$approval_hash" <<'PY' ||
import os, sys
fd=os.open(sys.argv[1],os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
with os.fdopen(fd,"w",encoding="utf-8") as stream:
    stream.write(f"approval_hash={sys.argv[2]}\nused=0\n")
PY
    die "product resume approval already exists"
  echo "APPROVE_HASH=$approval_hash"
  echo "TICKETS=${PRODUCT_TICKETS[*]}"
}

restore_product_resume_source() {
  local root="$1"
  python3 - "$root/runtime/product-source.json" <<'PY'
import json, os, pathlib, sys
path=pathlib.Path(sys.argv[1]); value=json.load(open(path,encoding="utf-8"))
original=value.pop("resume_original_tickets",None)
value.pop("resume_sha256",None)
if original is None: raise SystemExit(1)
value["tickets"]=original
raw=json.dumps(value,sort_keys=True,separators=(",",":"))+"\n"
tmp=path.with_name(path.name+".tmp")
tmp.write_text(raw,encoding="utf-8"); os.chmod(tmp,0o600); os.replace(tmp,path)
PY
}

ensure_product_budget_day() {
  local root="$1" daily_cap="${2:-}"
  python3 - "$root/runtime/product-envelope" "$(date -u +%F)" "$daily_cap" <<'PY'
import os, pathlib, re, stat, sys
directory=pathlib.Path(sys.argv[1]); today=sys.argv[2]; daily_cap=sys.argv[3]
if not directory.exists():
    directory.mkdir(mode=0o700)
info=directory.lstat()
if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or
    stat.S_IMODE(info.st_mode) != 0o700):
    raise SystemExit(1)
path=directory/"budget-day"
if path.exists() or path.is_symlink():
    info=path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
        info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600 or
        path.read_text(encoding="utf-8") != today+"\n"):
        raise SystemExit(1)
else:
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,"w",encoding="utf-8") as stream:
        stream.write(today+"\n"); stream.flush(); os.fsync(stream.fileno())
global_path=directory/"global.env"
if global_path.exists() or global_path.is_symlink():
    info=global_path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
        info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600 or
        not re.fullmatch(r"GLOBAL_DAILY_CAP_USD=[0-9]+(?:\.[0-9]+)?\n",
                         global_path.read_text(encoding="utf-8"))):
        raise SystemExit(1)
elif daily_cap:
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?",daily_cap):
        raise SystemExit(1)
    fd=os.open(global_path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,"w",encoding="utf-8") as stream:
        stream.write(f"GLOBAL_DAILY_CAP_USD={daily_cap}\n")
        stream.flush(); os.fsync(stream.fileno())
PY
}

product_probe_and_plan() {
  local root="$1" cursor_version codex_version claude_version ticket profile profile_hash approval_hash expected
  require_lane_mode "$root" product
  load_product_tickets "$root"
  validate_runtime_paths "$root"
  source "$root/kit/scripts/lib/plain-config.sh"
  factory_load_plain_config "$root/product/factory/ENVELOPE.env" envelope \
    "$FACTORY_ENVELOPE_CONFIG_KEYS" "$FACTORY_ENVELOPE_REQUIRED_KEYS" ||
    die "product envelope is invalid"
  ensure_product_budget_day "$root" "$DAILY_CAP_USD" ||
    die "product budget day is missing, stale, or unsafe"
  subscription_ready "$root"
  cursor_version="$(subscription_base_env "$root" \
    "$root/home/agent" --version 2>/dev/null | awk 'NF {print $NF; exit}')"
  codex_version="$(subscription_base_env "$root" \
    "$root/home/codex" --version 2>/dev/null | awk 'NF {print $NF; exit}')"
  claude_version="$(subscription_base_env "$root" \
    "$root/home/claude" --version 2>/dev/null | awk 'NF {print $1; exit}')"
  [[ -n "$cursor_version" && -n "$codex_version" && -n "$claude_version" ]] ||
    die "product subscription CLI version probe was empty"
  cat >"$root/home/.factory/global.env" <<EOF
$(cat "$root/runtime/product-envelope/global.env" 2>/dev/null || printf 'GLOBAL_DAILY_CAP_USD=%s\n' "$DAILY_CAP_USD")
FACTORY_CURSOR_FALLBACK_ENABLED=1
AGENT_CLI_CREDENTIAL_STORE=file
CURSOR_AGENT_VERSION=$cursor_version
CODEX_PINNED=$codex_version
CLAUDE_CODE_PINNED=$claude_version
CURSOR_OPENAI_MODEL=gpt-5.6-sol-high
CURSOR_ANTHROPIC_MODEL=claude-fable-5-thinking-medium
EOF
  chmod 600 "$root/home/.factory/global.env"
  for ticket in "${PRODUCT_TICKETS[@]}"; do
    profile=balanced-v2
    profile_hash="$(python3 - "$root/kit/scripts/model-routing/profiles-v1.json" "$profile" <<'PY'
import hashlib, json, sys
value=json.load(open(sys.argv[1], encoding="utf-8"))
profile=next(item for item in value["profiles"] if item["profile_id"] == sys.argv[2])
raw=json.dumps(profile, ensure_ascii=False, sort_keys=True, separators=(",",":"))
print(hashlib.sha256(raw.encode()).hexdigest())
PY
)" || die "could not bind product routing profile"
    subscription_env "$root" "$root/kit/scripts/model-control.sh" activate \
      --profile "$profile" --approve-hash "$profile_hash" \
      --approved-by factory-dev-lane >/dev/null
    subscription_env "$root" "$root/kit/scripts/model-control.sh" pin \
      --ticket "$ticket" --workdir "$root/worktrees/$ticket" >/dev/null
  done
  python3 - "$root" "${PRODUCT_TICKETS[@]}" <<'PY' ||
import json, pathlib, sys
root, *tickets=sys.argv[1:]
production={"planner","builder","narrator"}; checking={"spec-linter","test-author","reviewer"}
for ticket in tickets:
    plan=json.loads(pathlib.Path(root,"worktrees",ticket,"factory","route-plans",ticket+".json").read_text())
    selections=plan["resolution"]["selections"]
    if any(selections[r]["provider_family"]==selections[c]["provider_family"] for r in production for c in checking):
        raise SystemExit("role-family separation failed")
    if any(selections[r]["adapter"] != "codex" for r in production):
        raise SystemExit("native production route drifted")
    if any(selections[r]["adapter"] not in {"claude-code","cursor-anthropic"} for r in checking):
        raise SystemExit("checking route drifted outside approved Anthropic adapters")
PY
    die "product route-family or circuit-breaker validation failed"
  python3 - "$root/runtime/provider-policy.json" \
    "$root/runtime/provider-activation.json" "$root" "${PRODUCT_TICKETS[@]}" <<'PY'
import hashlib, json, os, pathlib, sys
policy_path, activation_path, root, *tickets=sys.argv[1:]
def limit(concurrent, starts):
    return {"max_concurrent":concurrent,"max_starts":starts,"window_seconds":60}
policy={"schema":"factory-provider-concurrency-policy/v1","coupled_max_concurrent":4,
        "global":limit(4,24),
        "provider_families":{"openai":limit(4,24),"anthropic":limit(4,24)},
        "account_routes":{"cursor":limit(2,15),"codex-native":limit(4,18),
                          "claude-native":limit(4,18)}}
raw=json.dumps(policy, sort_keys=True, separators=(",",":"))
routes={}
for ticket in tickets:
    plan=json.loads(pathlib.Path(root,"worktrees",ticket,"factory","route-plans",ticket+".json").read_text())
    for value in plan["resolution"]["selections"].values():
        routes[value["route_id"]]={"account_route":value["account_route_id"],
            "adapter":value["adapter"],"model":value["selection_id"],
            "provider_family":value["provider_family"]}
activation={"enabled":True,"mode":"cli-concurrent-v1",
            "policy_sha256":hashlib.sha256(raw.encode()).hexdigest(),"routes":routes,
            "schema":"nysa.software-factory.provider-activation/v2"}
pathlib.Path(policy_path).write_text(raw+"\n")
pathlib.Path(activation_path).write_text(json.dumps(activation,sort_keys=True,separators=(",",":"))+"\n")
os.chmod(policy_path,0o600); os.chmod(activation_path,0o600)
PY
  python3 "$root/kit/scripts/provider-activation.py" \
    --config "$root/runtime/provider-activation.json" \
    --policy "$root/runtime/provider-policy.json" \
    --contract-version 1.7.0 --status >/dev/null || die "product activation policy is invalid"
  provision_product_databases "$root"
  if [[ -f "$root/runtime/product-checkpoint-import.json" ]]; then
    while IFS=$'\t' read -r ticket expected; do
      [[ -n "$ticket" ]] || continue
      expected="$expected"
      [[ "$(product_resume_stage "$root" "$ticket")" == "$expected" ]] ||
        die "imported checkpoint did not reproduce its exact next stage: $ticket"
    done < <(python3 - "$root/runtime/product-checkpoint-import.json" <<'PY'
import json, sys
value=json.load(open(sys.argv[1],encoding="utf-8"))
for item in value["tickets"]:
    print(item["ticket"]+"\t"+item["expected_next_stage"])
PY
)
  fi
  approval_hash="$(product_approval_hash "$root")"
  printf 'approval_hash=%s\nused=0\n' "$approval_hash" >"$root/runtime/product-approval"
  chmod 600 "$root/runtime/product-approval"
  echo "APPROVE_HASH=$approval_hash"
  echo "TICKETS=${PRODUCT_TICKETS[*]}"
  echo "PROVIDER_LIMITS=global:4,cursor:2,codex:4,claude:4"
}

next_stage() {
  local root="$1" lease="${2:-}" checkpoint=""
  [[ ! -f "$root/runtime/product-checkpoint-import.json" ]] ||
    checkpoint="$root/runtime/product-checkpoint-import.json"
  if [[ -n "$lease" ]]; then
    lane_env "$root" FACTORY_DISPATCH_LEASE_ID="$lease" \
      FACTORY_DEV_PRODUCT_CHECKPOINT="$checkpoint" \
      "$root/kit/scripts/next-stage.sh" --ticket "$TICKET" --lease "$lease" \
      --workdir "$root/worktrees/$TICKET"
  else
    lane_env "$root" \
      FACTORY_DEV_PRODUCT_CHECKPOINT="$checkpoint" \
      "$root/kit/scripts/next-stage.sh" --ticket "$TICKET" \
      --workdir "$root/worktrees/$TICKET"
  fi
}

run_mock_internal() {
  local root="$1" role expected mode mock_sleep=0
  mode="$(python3 - "$root/marker.json" <<'PY'
import json, sys
print(json.load(open(sys.argv[1], encoding="utf-8"))["mode"])
PY
)"
  [[ "$mode" == mock || "$mode" == mock-concurrency ]] ||
    die "lane mode does not authorize mock lifecycle"
  [[ "$mode" != mock-concurrency ]] || mock_sleep=2
  for role in planner spec-linter test-author builder reviewer narrator; do
    expected="RUN $role"
    [[ "$(next_stage "$root")" == "$expected" ]] ||
      die "sequencer did not authorize $role"
    lane_env "$root" FACTORY_TEST_MODE=1 FACTORY_TRUSTED_TEST_HARNESS=1 \
      FACTORY_ADAPTER_OVERRIDE=mock \
      MOCK_SLEEP="$mock_sleep" \
      "$root/kit/scripts/run-agent.sh" --role "$role" --ticket "$TICKET" \
      --prompt-file "$root/kit/roles/$role.md" --workdir "$root/worktrees/$TICKET" \
      -- "Execute the disposable development-lane $role stage."
    case "$role" in
      spec-linter) append_commit_push "$root" 'SPEC-LINT: PASS' "$TICKET: record synthetic spec lint" ;;
      reviewer) append_commit_push "$root" 'reviewer round 1: APPROVE' "$TICKET: record synthetic review" ;;
    esac
  done
  set_review_state "$root"
  [[ "$(next_stage "$root")" == AWAIT-OPERATOR* ]] ||
    die "synthetic lifecycle did not reach AWAIT-OPERATOR"
  [[ -z "$(git -C "$root/worktrees/$TICKET" status --porcelain --untracked-files=all)" ]] ||
    die "synthetic worktree is dirty"
  [[ "$(git -C "$root/worktrees/$TICKET" rev-parse HEAD)" == \
     "$(git -C "$root/worktrees/$TICKET" rev-parse "origin/ticket/$TICKET")" ]] ||
    die "synthetic ticket branch is not pushed"
}

run_mock_concurrency_internal() {
  local root="$1" ticket output pid day start_ns end_ns attempt account
  local -a pids=()
  require_lane_mode "$root" mock-concurrency
  validate_runtime_paths "$root"
  day="$(date -u +%F)"
  : > "$root/runtime/provider-timeline"
  chmod 600 "$root/runtime/provider-timeline"
  for ticket in "${TICKETS[@]}"; do
    attempt="$ticket-builder-lane"
    account=test-mock-a
    output="$root/runtime/provider-inputs/$ticket.out"
    lane_env "$root" FACTORY_DEV_LANE_TIMELINE="$root/runtime/provider-timeline" \
      python3 "$root/kit/scripts/provider-cli-runtime.py" \
        --coordinator "$root/kit/scripts/provider-coordinator.py" \
        --db "$root/runtime/provider-state.sqlite3" \
        --policy "$root/runtime/provider-policy.json" \
        --attempt-id "$attempt" \
        --provider-family mock --account-route "$account" \
        --reserve-micro-usd 1000000 --product-id "factory-dev-lane-$(basename "$root")" \
        --ticket-id "$ticket" --budget-day "$day" \
        --product-cap-micro-usd 4000000 --ticket-cap-micro-usd 1000000 \
        --machine-cap-micro-usd 4000000 -- "$root/home/mock-provider-cli" \
        >"$output" 2>&1 &
    pids+=("$!")
  done
  for pid in "${pids[@]}"; do wait "$pid" || die "subscription CLI mock provider failed"; done
  read -r start_ns end_ns < <(python3 - "$root/runtime/provider-timeline" <<'PY'
import sys
events=[line.split() for line in open(sys.argv[1], encoding="utf-8")]
starts=[int(row[2]) for row in events if row[0]=="start"]
ends=[int(row[2]) for row in events if row[0]=="end"]
if len(starts)!=4 or len(ends)!=4 or max(starts)>=min(ends):
    raise SystemExit("four provider calls did not overlap")
print(min(starts), max(ends))
PY
  ) || die "four provider calls did not overlap"
  for ticket in "${TICKETS[@]}"; do
    attempt="$ticket-builder-lane"
    python3 "$root/kit/scripts/provider-coordinator.py" \
      --db "$root/runtime/provider-state.sqlite3" terminalize \
      --operation-id "$attempt-host-terminal" --attempt-id "$attempt" \
      --expected-version 4 --result succeeded --charge-micro-usd 1000000 >/dev/null ||
      die "trusted host could not terminalize $attempt"
  done
  python3 "$root/kit/scripts/provider-coordinator.py" \
    --db "$root/runtime/provider-state.sqlite3" status | python3 -c '
import json, sys
value=json.load(sys.stdin)
assert value["counts"] == {"terminal":4}, value
assert value["active_reserve_micro_usd"] == 0, value
' || die "isolated provider reservations did not drain"
  # The provider overlap proof uses one activated account. Synthetic role
  # fixtures retain their historical alternating mock identities.
  python3 - "$root/runtime/provider-activation.json" <<'PY'
import json, os, pathlib, sys
path=pathlib.Path(sys.argv[1])
value=json.loads(path.read_text())
for number in range(900001,900005):
    ticket=f"T-{number}"
    value["routes"][f"test-mock-{ticket}"]["account_route"] = (
        "test-mock-a" if number % 2 else "test-mock-b"
    )
path.write_text(json.dumps(value,sort_keys=True,separators=(",",":"))+"\n")
os.chmod(path,0o600)
PY
  pids=()
  for ticket in "${TICKETS[@]}"; do
    (TICKET="$ticket"; run_mock_internal "$root") &
    pids+=("$!")
  done
  for pid in "${pids[@]}"; do wait "$pid" || die "synthetic lifecycle failed"; done
  echo "PROVIDER_CALLS=4"
  echo "PROVIDER_MODE=cli-concurrent-v1"
  echo "PROVIDER_OVERLAP_MILLISECONDS=$(( (end_ns - start_ns) / 1000000 ))"
}

subscription_probe_and_plan() {
  local root="$1" adapter version approval_hash pin_key
  require_lane_mode "$root" subscription
  validate_runtime_paths "$root"
  adapter="$(cat "$root/subscription-adapter")"
  case "$adapter" in
    codex)
      codex_subscription_ready "$root"
      version="$(subscription_base_env "$root" \
        "$root/home/codex" --version 2>/dev/null | awk 'NF {print $NF; exit}')"
      pin_key=CODEX_PINNED
      ;;
    claude)
      claude_subscription_ready "$root"
      version="$(subscription_base_env "$root" \
        "$root/home/claude" --version 2>/dev/null | awk 'NF {print $1; exit}')"
      pin_key=CLAUDE_CODE_PINNED
      ;;
    *) die "unsupported subscription canary adapter" ;;
  esac
  [[ -n "$version" ]] ||
    die "subscription CLI version probe was empty"
  cat > "$root/home/.factory/global.env" <<EOF
GLOBAL_DAILY_CAP_USD=1.00
$pin_key=$version
EOF
  chmod 600 "$root/home/.factory/global.env"
  python3 - "$root/runtime/provider-policy.json" \
    "$root/runtime/provider-activation.json" "$adapter" <<'PY'
import hashlib, json, os, sys

policy_path, activation_path, selected=sys.argv[1:]
values={
    "codex":("codex","gpt-5.6-sol","openai","lane-codex-subscription"),
    "claude":("claude-code","sonnet","anthropic","lane-claude-subscription"),
}
adapter, model, family, account=values[selected]
def limit(concurrent):
    return {"max_concurrent":concurrent,"max_starts":4,"window_seconds":60}
policy={
    "schema":"factory-provider-concurrency-policy/v1",
    "coupled_max_concurrent":4,
    "global":limit(4),
    "provider_families":{family:limit(4)},
    "account_routes":{account:limit(4)},
}
raw=json.dumps(policy, sort_keys=True, separators=(",",":"))
routes={
    f"lane-subscription-T-{number}":{
        "account_route":account,"adapter":adapter,
        "model":model,"provider_family":family}
    for number in range(900001, 900005)
}
activation={"enabled":True,"mode":"cli-concurrent-v1",
            "policy_sha256":hashlib.sha256(raw.encode()).hexdigest(),
            "routes":routes,
            "schema":"nysa.software-factory.provider-activation/v2"}
with open(policy_path, "w", encoding="utf-8") as handle:
    handle.write(raw+"\n")
with open(activation_path, "w", encoding="utf-8") as handle:
    json.dump(activation, handle, sort_keys=True, separators=(",",":")); handle.write("\n")
os.chmod(policy_path, 0o600); os.chmod(activation_path, 0o600)
PY
  python3 "$root/kit/scripts/provider-activation.py" \
    --config "$root/runtime/provider-activation.json" \
    --policy "$root/runtime/provider-policy.json" \
    --contract-version 1.7.0 --status >/dev/null ||
    die "subscription activation policy is invalid"
  cat > "$root/home/record-provider-call" <<'PY'
#!/usr/bin/env python3
import os, subprocess, sys, time

timeline, ticket, *command=sys.argv[1:]
with open(timeline, "a", encoding="utf-8") as handle:
    handle.write(f"start {ticket} {time.monotonic_ns()}\n")
    handle.flush(); os.fsync(handle.fileno())
completed=subprocess.run(command, check=False)
with open(timeline, "a", encoding="utf-8") as handle:
    handle.write(f"end {ticket} {time.monotonic_ns()} {completed.returncode}\n")
    handle.flush(); os.fsync(handle.fileno())
raise SystemExit(completed.returncode)
PY
  chmod 700 "$root/home/record-provider-call"
  approval_hash="$(subscription_approval_hash "$root")"
  printf 'approval_hash=%s\nused=0\n' "$approval_hash" > "$root/runtime/subscription-approval"
  chmod 600 "$root/runtime/subscription-approval"
  echo "APPROVE_HASH=$approval_hash"
  echo "PROVIDER_SPLIT=$adapter:4"
  echo "AGGREGATE_RESERVATION_USD=1.00"
}

run_subscription_internal() {
  local root="$1" supplied="$2" stored ticket adapter family account model attempt output pid
  local day start_ns end_ns result command_path terminal_result selected attempt_root
  local -a pids=() attempts=() outputs=() attempt_roots=()
  require_lane_mode "$root" subscription
  validate_runtime_paths "$root"
  [[ -f "$root/runtime/subscription-approval" && ! -L "$root/runtime/subscription-approval" ]] ||
    die "subscription approval is missing or already used"
  stored="$(sed -n 's/^approval_hash=//p' "$root/runtime/subscription-approval")"
  [[ "$stored" == "$supplied" && "$(sed -n 's/^used=//p' "$root/runtime/subscription-approval")" == 0 ]] ||
    die "subscription approval hash does not match or was already used"
  [[ "$(subscription_approval_hash "$root")" == "$supplied" ]] ||
    die "subscription approval inputs drifted after planning"
  selected="$(cat "$root/subscription-adapter")"
  case "$selected" in
    codex) codex_subscription_ready "$root" ;;
    claude) claude_subscription_ready "$root" ;;
    *) die "unsupported subscription canary adapter" ;;
  esac
  subscription_provider_idle || die "another subscription provider call is active"
  mv "$root/runtime/subscription-approval" "$root/runtime/subscription-approval.used"
  day="$(date -u +%F)"
  : > "$root/runtime/provider-timeline"
  chmod 600 "$root/runtime/provider-timeline"
  for ticket in "${TICKETS[@]}"; do
    [[ "$ticket" =~ ^T-90000[1-4]$ ]] ||
      die "unexpected subscription canary ticket"
    if [[ "$selected" == codex ]]; then
      adapter=codex; family=openai; account=lane-codex-subscription
      model=gpt-5.6-sol
    else
      adapter=claude-code; family=anthropic; account=lane-claude-subscription
      model=sonnet
    fi
    attempt="$ticket-subscription-canary"
    output="$root/runtime/provider-inputs/$ticket.out"
    command_path="$root/kit/scripts/adapters/$adapter.sh"
    if [[ "$selected" == claude ]]; then
      attempt_root="$root/runtime/cli-attempts/$attempt"
      mkdir -p "$root/runtime/cli-attempts"
      chmod 700 "$root/runtime/cli-attempts"
      mkdir -m 700 "$attempt_root" "$attempt_root/home" \
        "$attempt_root/config" "$attempt_root/tmp"
      printf '%s\n' "$attempt" >"$attempt_root/owner"
      cp "$root/session-home/.claude/.credentials.json" \
        "$attempt_root/config/.credentials.json"
      chmod 600 "$attempt_root/owner" "$attempt_root/config/.credentials.json"
      attempt_roots+=("$attempt_root")
      claude_subscription_env "$root" env \
        HOME="$attempt_root/home" TMPDIR="$attempt_root/tmp" \
        CLAUDE_CONFIG_DIR="$attempt_root/config" \
        CLAUDE_CODE_TMPDIR="$attempt_root/tmp" \
        FACTORY_CLI_INTERNAL_SANDBOX=1 FACTORY_CLI_ATTEMPT_ID="$attempt" \
        FACTORY_CLAUDE_SETTINGS="$root/runtime/claude-settings.json" \
        python3 "$root/kit/scripts/provider-cli-runtime.py" \
      --coordinator "$root/kit/scripts/provider-coordinator.py" \
      --db "$root/runtime/provider-state.sqlite3" \
      --policy "$root/runtime/provider-policy.json" \
      --attempt-id "$attempt" --provider-family "$family" --account-route "$account" \
      --reserve-micro-usd 250000 --product-id "factory-dev-lane-$(basename "$root")" \
      --ticket-id "$ticket" --budget-day "$day" \
      --product-cap-micro-usd 1000000 --ticket-cap-micro-usd 250000 \
      --machine-cap-micro-usd 1000000 -- \
      "$root/home/record-provider-call" "$root/runtime/provider-timeline" "$ticket" \
      "$command_path" --budget 0.25 --max-turns 1 --timeout-min 3 \
      --prompt-file /dev/null --workdir "$root/worktrees/$ticket" \
      --model "$model" --effort low -- \
      "Reply with exactly CANARY_OK. Do not read, execute, or modify files." \
      >"$output" 2>&1 &
    else
      codex_subscription_env "$root" python3 "$root/kit/scripts/provider-cli-runtime.py" \
      --coordinator "$root/kit/scripts/provider-coordinator.py" \
      --db "$root/runtime/provider-state.sqlite3" \
      --policy "$root/runtime/provider-policy.json" \
      --attempt-id "$attempt" --provider-family "$family" --account-route "$account" \
      --reserve-micro-usd 250000 --product-id "factory-dev-lane-$(basename "$root")" \
      --ticket-id "$ticket" --budget-day "$day" \
      --product-cap-micro-usd 1000000 --ticket-cap-micro-usd 250000 \
      --machine-cap-micro-usd 1000000 -- \
      "$root/home/record-provider-call" "$root/runtime/provider-timeline" "$ticket" \
      "$command_path" --budget 0.25 --max-turns 1 --timeout-min 3 \
      --prompt-file /dev/null --workdir "$root/worktrees/$ticket" \
      --model "$model" --effort low -- \
      "Reply with exactly CANARY_OK. Do not read, execute, or modify files." \
      >"$output" 2>&1 &
    fi
    pids+=("$!"); attempts+=("$attempt"); outputs+=("$output")
  done
  result=0
  for pid in "${pids[@]}"; do
    wait "$pid" || result=1
  done
  for attempt in "${attempts[@]}"; do
    if [[ "$result" -eq 0 ]]; then terminal_result=succeeded; else terminal_result=failed; fi
    python3 "$root/kit/scripts/provider-coordinator.py" \
      --db "$root/runtime/provider-state.sqlite3" terminalize \
      --operation-id "$attempt-host-terminal" --attempt-id "$attempt" \
      --expected-version 4 --result "$terminal_result" --charge-micro-usd 250000 >/dev/null ||
      die "trusted host could not terminalize $attempt"
  done
  [[ "$result" -eq 0 ]] || die "one or more subscription canary calls failed"
  for output in "${outputs[@]}"; do
    grep -q 'CANARY_OK' "$output" || die "subscription canary output validation failed"
  done
  read -r start_ns end_ns < <(python3 - "$root/runtime/provider-timeline" <<'PY'
import sys
events=[line.split() for line in open(sys.argv[1], encoding="utf-8")]
starts=[int(row[2]) for row in events if row[0]=="start"]
ends=[int(row[2]) for row in events if row[0]=="end"]
if len(starts)!=4 or len(ends)!=4 or max(starts)>=min(ends):
    raise SystemExit(1)
print(min(starts), max(ends))
PY
  ) || die "four subscription provider calls did not overlap"
  for ticket in "${TICKETS[@]}"; do
    [[ -z "$(git -C "$root/worktrees/$ticket" status --porcelain --untracked-files=all)" ]] ||
      die "subscription canary modified its synthetic worktree"
  done
  python3 "$root/kit/scripts/provider-coordinator.py" \
    --db "$root/runtime/provider-state.sqlite3" status | python3 -c '
import json, sys
value=json.load(sys.stdin)
assert value["counts"] == {"terminal":4}, value
assert value["active_reserve_micro_usd"] == 0, value
' || die "subscription canary reservations did not drain"
  subscription_provider_idle || die "subscription canary left a provider process"
  if [[ "$selected" == claude ]]; then
    for attempt_root in "${attempt_roots[@]}"; do
      python3 - "$attempt_root" "$root" <<'PY' ||
import pathlib, shutil, sys
path=pathlib.Path(sys.argv[1]); root=pathlib.Path(sys.argv[2])
if path.parent != root/"runtime"/"cli-attempts" or path.is_symlink():
    raise SystemExit(1)
shutil.rmtree(path)
PY
        die "subscription canary Claude runtime cleanup failed"
    done
  fi
  echo "PROVIDER_CALLS=4"
  echo "PROVIDER_MODE=cli-concurrent-v1"
  echo "PROVIDER_SPLIT=$selected:4"
  echo "PROVIDER_OVERLAP_MILLISECONDS=$(( (end_ns - start_ns) / 1000000 ))"
  echo "ACCOUNTED_RESERVATION_USD=1.00"
}

validate_product_dev_bundle() {
  python3 - "$1" <<'PY'
import pathlib, re, sys
text=pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")
required=("What this does","Preview","Screenshots","Acceptance criteria",
          "Risk","Cost","Rollback")
if any(not re.search(rf"^#+\s+.*{re.escape(name)}",text,re.I|re.M)
       for name in required):
    raise SystemExit("development evidence bundle is incomplete")
headings=list(re.finditer(r"^#+\s+(.+?)\s*$",text,re.M))
def section(name):
    for index, heading in enumerate(headings):
        if name.casefold() in heading.group(1).casefold():
            end=headings[index+1].start() if index+1 < len(headings) else len(text)
            return text[heading.end():end]
    return ""
preview=section("Preview")
screenshots=section("Screenshots")
backend_na=r"(?m)^Not applicable — backend-only contract(?:[.]|$)"
preview_deferred=(
    re.search(r"\b(?:unavailable|pending)\b",preview,re.I)
    and re.search(r"\b(?:PR|deploy|publication)\b",preview,re.I)
)
screenshots_deferred=(
    re.search(r"\bunavailable\b",screenshots,re.I)
    and re.search(r"\bno\s+(?:UI|browser|visual(?:\s+surface)?)\b",screenshots,re.I)
)
if not re.search(backend_na,screenshots) and not (
    preview_deferred and screenshots_deferred
):
    raise SystemExit("development evidence bundle lacks backend-only screenshot evidence")
if not re.search(backend_na,preview) and not preview_deferred:
    raise SystemExit("development evidence bundle lacks backend-only preview evidence")
if not re.search(r"development-only",text,re.I) or \
   not re.search(r"not a production attestation",text,re.I) or \
   not re.search(r"approve to merge",text,re.I):
    raise SystemExit("development evidence bundle lacks status or approval question")
PY
}

product_role_run() {
  local root="$1" ticket="$2" lease="$3" role="$4" instruction envelope evidence checkpoint="" rc=0
  instruction="Execute the authorized $role stage for $ticket. Work only in this ticket worktree. Follow the frozen ticket contract and repository instructions. Mutating roles must commit their scoped durable result locally. Never push or access another worktree, remote service, credential, or Factory control path."
  instruction="$instruction Node 22 is on PATH. For database-backed checks, load only the disposable lane variables from the ticket worktree with: set -a; source \"\$(git rev-parse --show-toplevel)/../../runtime/product-db/$ticket.env\"; set +a. Never print those variables."
  if [[ "$role" == reviewer ]]; then
    instruction="$instruction Remain read-only. End with a standalone line containing exactly APPROVE or REQUEST CHANGES. If requesting changes, add exactly one standalone FIX-OWNER: builder, FIX-OWNER: test-author, or FIX-OWNER: both line; approvals must not include FIX-OWNER."
  elif [[ "$role" == narrator ]]; then
    evidence="$(python3 - "$root/product/factory/runtime-ledger.csv" "$ticket" <<'PY'
import csv, sys
rows=[row for row in csv.DictReader(open(sys.argv[1],encoding="utf-8"))
      if row.get("ticket")==sys.argv[2]]
print(f"attempts={len(rows)} cost_usd={sum(float(row['cost_usd']) for row in rows):.2f}")
PY
)" || return
    instruction="$instruction Trusted host marker: FACTORY_DEV_PRLESS_EVIDENCE_V1. This isolated development lane has no GitHub PR, deploy, or network. If the frozen contract explicitly has no browser or visual surface, including a backend-only HTTP API, write all standard bundle sections and begin Preview and Screenshots with 'Not applicable — backend-only contract'; an immediate period and explanation are allowed. For an HTTP API, Preview may instead state that it is unavailable or pending until the PR/deploy publication gate, provided Screenshots explicitly says unavailable and that the contract has no UI or visual surface. Label the bundle development-only and not a production attestation, and summarize the already committed Reviewer-approved deterministic evidence. The PR/deploy preview is a later publication gate and must not block this development bundle. Do not rerun tests or commands. Trusted accounting: $evidence."
  fi
  envelope="$root/product/factory/ENVELOPE.env"
  [[ ! -f "$root/runtime/product-envelope/$ticket.env" ]] ||
    envelope="$root/runtime/product-envelope/$ticket.env"
  [[ ! -f "$root/runtime/product-checkpoint-import.json" ]] ||
    checkpoint="$root/runtime/product-checkpoint-import.json"
  subscription_env "$root" FACTORY_DISPATCH_LEASE_ID="$lease" \
    FACTORY_ENVELOPE="$envelope" \
    FACTORY_DEV_PRODUCT_CHECKPOINT="$checkpoint" \
    FACTORY_DEV_BUDGET_DAY="$(cat "$root/runtime/product-envelope/budget-day" 2>/dev/null || true)" \
    FACTORY_DEV_PROVIDER_WAIT_SECONDS=900 \
    "$root/kit/scripts/run-agent.sh" --role "$role" --ticket "$ticket" \
    --prompt-file "$root/kit/roles/$role.md" --workdir "$root/worktrees/$ticket" -- \
    "$instruction" || rc=$?
  if [[ "$rc" -ne 0 ]]; then
    if [[ "$rc" -eq 12 ]]; then
      product_transition_contract_blocked "$root" "$ticket" "$role" || return 11
    fi
    return "$rc"
  fi
  if [[ "$role" == narrator ]]; then
    validate_product_dev_bundle \
      "$root/worktrees/$ticket/factory/tickets/$ticket-bundle.md" || return
  elif [[ "$role" == reviewer ]]; then
    (TICKET="$ticket"; product_reconcile_reviewer "$root" "$ticket" "$lease") || return
  fi
}

product_transition_contract_blocked() {
  local root="$1" ticket="$2" role="$3"
  python3 - "$root/product/factory/runs" "$ticket" "$role" <<'PY' || return
import pathlib, stat, sys

runs=pathlib.Path(sys.argv[1]); ticket=sys.argv[2]; role=sys.argv[3]
candidates=[]
for path in runs.glob("*.meta"):
    info=path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise SystemExit("unsafe role manifest")
    values={}
    for line in path.read_text(encoding="utf-8").splitlines():
        key, separator, value=line.partition("=")
        if not separator or key in values:
            raise SystemExit("malformed role manifest")
        values[key]=value
    if values.get("ticket") == ticket and values.get("role") == role:
        candidates.append(values)
if not candidates:
    raise SystemExit("contract blocker manifest is missing")
latest=max(candidates, key=lambda value: (
    value.get("started_at",""), value.get("run_id","")))
required={
    "contract_version":"1.7.0",
    "phase":"completed",
    "accounting_state":"completed",
    "exit_status":"12",
    "role_exit":"role_exit_contract_blocked",
}
if any(latest.get(key) != value for key, value in required.items()):
    raise SystemExit("contract blocker manifest is invalid")
PY
  lane_env "$root" "$root/kit/scripts/ticket-state.sh" \
    --ticket "$ticket" --workdir "$root/worktrees/$ticket" \
    --action transition --state Blocked-Escalated >/dev/null
}

product_reconcile_reviewer() {
  local root="$1" ticket="$2" lease="${3:-}"
  [[ "$lease" =~ ^[0-9a-f]{64}$ ]] || return 1
  python3 - "$root/product/factory/runs" "$ticket" <<'PY' || return 0
import pathlib, sys
runs=pathlib.Path(sys.argv[1]); ticket=sys.argv[2]
for path in runs.glob("*.meta"):
    values={}
    for line in path.read_text(encoding="utf-8").splitlines():
        key, separator, value=line.partition("=")
        if separator: values[key]=value
    if (values.get("ticket")==ticket and values.get("role")=="reviewer" and
        values.get("phase")=="completed" and values.get("exit_status")=="0"):
        raise SystemExit(0)
raise SystemExit(1)
PY
  lane_env "$root" FACTORY_DISPATCH_LEASE_ID="$lease" \
    "$SOURCE_ROOT/scripts/ticket-state.sh" \
    --ticket "$ticket" --workdir "$root/worktrees/$ticket" \
    --action reviewer-reconcile >/dev/null
}

product_resume_reason() {
  local log="$1"
  [[ -f "$log" && ! -L "$log" ]] || {
    printf '%s\n' role-failed
    return
  }
  if grep -Fxq 'Resolved Cursor model is unavailable' "$log" ||
    grep -Eq "^pinned route unavailable or drifted for role '[a-z-]+': pinned_route_UNAVAILABLE_(authentication|model)_unavailable; no task was submitted$" \
      "$log"; then
    printf '%s\n' pinned-route-readiness
  else
    printf '%s\n' role-failed
  fi
}

product_role_for_stage() {
  case "$1" in
    "FIX builder") printf '%s\n' builder ;;
    "FIX test-author") printf '%s\n' test-author ;;
    RUN\ *) printf '%s\n' "${1#RUN }" ;;
    *) return 1 ;;
  esac
}

product_prepare_role_state() {
  local root="$1" ticket="$2" role="$3" target current
  case "$role" in
    planner|spec-linter) target=Planning ;;
    test-author|builder) target=Building ;;
    reviewer|narrator) target=Review ;;
    *) return 1 ;;
  esac
  current="$(python3 - "$root/worktrees/$ticket/factory/tickets/$ticket.md" <<'PY'
import re, sys
matches=re.findall(r"^State:\s*(\S(?:.*\S)?)\s*$",
                   open(sys.argv[1],encoding="utf-8").read(),re.I|re.M)
if len(matches) != 1 or matches[0] not in {"Ready", "Planning", "Building", "Review", "Blocked-Escalated"}:
    raise SystemExit(1)
print(matches[0])
PY
)" || return
  if [[ "$current" == "Blocked-Escalated" && "$target" == "Planning" ]]; then
    lane_env "$root" "$SOURCE_ROOT/scripts/ticket-state.sh" \
      --ticket "$ticket" --workdir "$root/worktrees/$ticket" \
      --action materialize >/dev/null || return
    current="$(sed -n 's/^State:[[:space:]]*//p' \
      "$root/worktrees/$ticket/factory/tickets/$ticket.md")"
    [[ "$current" == "Planning" ]] || return
  fi
  while [[ "$current" != "$target" ]]; do
    case "$current:$target" in
      Ready:Planning|Ready:Building)
        current=Planning
        ;;
      Planning:Building)
        current=Building
        ;;
      Building:Review)
        current=Review
        ;;
      *)
        return 1
        ;;
    esac
    lane_env "$root" "$SOURCE_ROOT/scripts/ticket-state.sh" \
      --ticket "$ticket" --workdir "$root/worktrees/$ticket" \
      --action transition --state "$current" >/dev/null || return
  done
}

product_completed_roles() {
  local root="$1" ticket="$2"
  python3 - "$root/product/factory/runs" "$ticket" \
    "$root/runtime/product-checkpoint-import.json" <<'PY'
import json, pathlib, sys
runs=pathlib.Path(sys.argv[1]); ticket=sys.argv[2]; checkpoint=pathlib.Path(sys.argv[3])
order=("planner","spec-linter","test-author","builder","reviewer","narrator")
completed=set()
if checkpoint.is_file():
    value=json.load(open(checkpoint,encoding="utf-8"))
    records=[item for item in value["tickets"] if item["ticket"] == ticket]
    if records: completed.update(records[0]["roles"])
for path in runs.glob("*.meta"):
    values={}
    for line in path.read_text(encoding="utf-8").splitlines():
        key, separator, value=line.partition("=")
        if separator: values[key]=value
    if (values.get("ticket")==ticket and values.get("phase")=="completed" and
        values.get("exit_status")=="0"):
        completed.add(values.get("role"))
print(",".join(role for role in order if role in completed) or "none")
PY
}

product_remaining_budget() {
  local root="$1" ticket="$2" envelope
  envelope="$root/product/factory/ENVELOPE.env"
  [[ ! -f "$root/runtime/product-envelope/$ticket.env" ]] ||
    envelope="$root/runtime/product-envelope/$ticket.env"
  python3 - "$root/kit/scripts/provider-coordinator.py" \
    "$root/runtime/provider-state.sqlite3" "$envelope" "$ticket" <<'PY'
import json, pathlib, subprocess, sys
coordinator, database, envelope_path, ticket=sys.argv[1:]
envelope=pathlib.Path(envelope_path)
values={}
for line in envelope.read_text(encoding="utf-8").splitlines():
    key, separator, value=line.partition("=")
    if separator: values[key]=value
cap=round(float(values["PER_TICKET_BUDGET_USD"])*1_000_000)
status=json.loads(subprocess.check_output(
    [sys.executable,coordinator,"--db",database,"status"],text=True))
used=0
for attempt in status["attempts"]:
    if attempt["ticket_id"] != ticket: continue
    used += (attempt["charge_micro_usd"] if attempt["state"]=="terminal"
             else attempt["reserve_micro_usd"])
print(f"{max(0,cap-used)/1_000_000:.6f}")
PY
}

product_write_timing_report() {
  local root="$1" started="$2" finished="$3"
  python3 - "$root/kit/scripts/provider-coordinator.py" \
    "$root/runtime/provider-state.sqlite3" "$root/runtime/product-timing.json" \
    "$root/product/factory/runs" "$started" "$finished" <<'PY'
import json, os, pathlib, subprocess, sys, tempfile
coordinator, database, output_path, runs_path, started_value, finished_value=sys.argv[1:]
output=pathlib.Path(output_path); runs=pathlib.Path(runs_path)
started=int(started_value); finished=int(finished_value)
status=json.loads(subprocess.check_output(
    [sys.executable,coordinator,"--db",database,"status"],text=True))
attempts=status["attempts"]
events=[]
for attempt in attempts:
    if attempt["go_at"] is not None and attempt["terminal_at"] is not None:
        events.extend(((attempt["go_at"],1),(attempt["terminal_at"],-1)))
active=maximum=0
for _, delta in sorted(events, key=lambda item:(item[0],-item[1])):
    active += delta
    maximum=max(maximum,active)
successful={}; successful_heads={}
for path in runs.glob("*.meta"):
    values={}
    for line in path.read_text(encoding="utf-8").splitlines():
        key, separator, value=line.partition("=")
        if separator: values[key]=value
    if values.get("phase")=="completed" and values.get("exit_status")=="0":
        key=(values.get("ticket"),values.get("role"))
        successful[key]=successful.get(key,0)+1
        head_key=key+(values.get("role_head_before"),)
        successful_heads[head_key]=successful_heads.get(head_key,0)+1
report={
  "schema":"factory-dev-product-timing/v1",
  "batch_started_at":started,
  "batch_terminal_at":finished,
  "elapsed_seconds":max(0,finished-started),
  "maximum_provider_overlap":maximum,
  "successful_role_replay_count":sum(
      max(0,count-1) for count in successful_heads.values()),
  "repeat_role_call_count":sum(max(0,count-1) for count in successful.values()),
  "attempts":[{
    key:attempt[key] for key in (
      "attempt_id","ticket_id","prepared_at","admitted_at","go_at",
      "submitted_at","terminal_at","terminal_result","reserve_micro_usd",
      "charge_micro_usd")
  } for attempt in attempts],
}
fd, temporary=tempfile.mkstemp(prefix=output.name+".",dir=output.parent)
try:
    os.fchmod(fd,0o600)
    with os.fdopen(fd,"w",encoding="utf-8") as stream:
        json.dump(report,stream,sort_keys=True,separators=(",",":"))
        stream.write("\n"); stream.flush(); os.fsync(stream.fileno())
    os.replace(temporary,output)
finally:
    if os.path.exists(temporary): os.unlink(temporary)
PY
}

run_product_internal() {
  local root="$1" supplied="$2" readiness_proven="${3:-0}"
  local stored day i ticket lease_json stage role account family now
  local done_count failed_count blocked_count progress pid rc prior
  local rollback_failed resume_csv blocked_csv
  local batch_started batch_finished completed remaining
  local -a leases pids states renewals roles resume_reasons failed_stages
  require_lane_mode "$root" product
  load_product_tickets "$root"
  validate_runtime_paths "$root"
  [[ -f "$root/runtime/product-approval" && ! -L "$root/runtime/product-approval" ]] ||
    die "product approval is missing or already used"
  stored="$(sed -n 's/^approval_hash=//p' "$root/runtime/product-approval")"
  [[ "$stored" == "$supplied" && \
     "$(sed -n 's/^used=//p' "$root/runtime/product-approval")" == 0 ]] ||
    die "product approval hash does not match or was already used"
  [[ "$(product_approval_hash "$root")" == "$supplied" ]] ||
    die "product approval inputs drifted after planning"
  [[ "$readiness_proven" == 0 || "$readiness_proven" == 1 ]] ||
    die "product readiness proof is invalid"
  [[ "$readiness_proven" == 1 ]] || subscription_ready "$root"
  subscription_provider_idle || die "another subscription provider call is active"
  mv "$root/runtime/product-approval" "$root/runtime/product-approval.used"
  batch_started="$(date +%s)"
  mkdir -p "$root/runtime/product-scheduler"
  chmod 700 "$root/runtime/product-scheduler"
  for i in "${!PRODUCT_TICKETS[@]}"; do
    ticket="${PRODUCT_TICKETS[$i]}"
    lease_json="$(subscription_env "$root" "$root/kit/scripts/dispatch-lease.sh" \
      claim --ticket "$ticket")" || {
        rollback_failed=0
        prior=0
        while [[ "$prior" -lt "$i" ]]; do
          subscription_env "$root" "$root/kit/scripts/dispatch-lease.sh" release \
            --ticket "${PRODUCT_TICKETS[$prior]}" --lease "${leases[$prior]}" \
            >/dev/null || rollback_failed=1
          prior=$((prior + 1))
        done
        [[ "$rollback_failed" -eq 0 ]] ||
          die "could not claim $ticket or release prior product ticket leases"
        die "could not claim product ticket lease: $ticket"
      }
    leases[$i]="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["lease_id"])' \
      <<<"$lease_json")"
    pids[$i]=0; states[$i]=idle; renewals[$i]=0
    roles[$i]=""; resume_reasons[$i]=control-boundary-failure
    failed_stages[$i]=control-boundary
  done
  done_count=0; failed_count=0; blocked_count=0
  while [[ "$done_count" -lt "${#PRODUCT_TICKETS[@]}" &&
           $((done_count + failed_count + blocked_count)) -lt "${#PRODUCT_TICKETS[@]}" ]]; do
    progress=0
    for i in "${!PRODUCT_TICKETS[@]}"; do
      [[ "${states[$i]}" == running ]] || continue
      pid="${pids[$i]}"
      if ! kill -0 "$pid" 2>/dev/null; then
        rc=0; wait "$pid" || rc=$?
        if [[ "$rc" -eq 0 ]]; then
          states[$i]=idle
        elif [[ "$rc" -eq 12 ]]; then
          states[$i]=blocked; blocked_count=$((blocked_count + 1))
        else
          resume_reasons[$i]="$(product_resume_reason \
            "$root/runtime/product-scheduler/${PRODUCT_TICKETS[$i]}-${roles[$i]}.log")"
          states[$i]=failed; failed_count=$((failed_count + 1))
        fi
        pids[$i]=0; progress=1
      fi
    done
    for i in "${!PRODUCT_TICKETS[@]}"; do
      [[ "${states[$i]}" == idle ]] || continue
      ticket="${PRODUCT_TICKETS[$i]}"; now="$(date +%s)"
      if [[ $((now - renewals[$i])) -ge 120 ]]; then
        subscription_env "$root" "$root/kit/scripts/dispatch-lease.sh" renew \
          --ticket "$ticket" --lease "${leases[$i]}" >/dev/null || {
            states[$i]=failed; failed_count=$((failed_count + 1)); continue;
          }
        renewals[$i]="$now"
      fi
      (TICKET="$ticket"; product_reconcile_reviewer \
        "$root" "$ticket" "${leases[$i]}") || {
        failed_stages[$i]=reviewer-reconcile
        states[$i]=failed; failed_count=$((failed_count + 1)); continue;
      }
      stage="$(TICKET="$ticket"; next_stage "$root" "${leases[$i]}")" || {
        failed_stages[$i]=sequencing
        states[$i]=failed; failed_count=$((failed_count + 1)); continue;
      }
      failed_stages[$i]="$stage"
      if [[ "$stage" == AWAIT-OPERATOR* ]]; then
        [[ -z "$(git -C "$root/worktrees/$ticket" status --porcelain --untracked-files=all)" ]] || {
          states[$i]=failed; failed_count=$((failed_count + 1)); continue;
        }
        subscription_env "$root" "$root/kit/scripts/dispatch-lease.sh" release \
          --ticket "$ticket" --lease "${leases[$i]}" >/dev/null || {
            states[$i]=failed; failed_count=$((failed_count + 1)); continue;
          }
        states[$i]=done; done_count=$((done_count + 1)); progress=1; continue
      fi
      role="$(product_role_for_stage "$stage")" ||
        { states[$i]=failed; failed_count=$((failed_count + 1)); continue; }
      roles[$i]="$role"
      failed_stages[$i]="$role"
      product_prepare_role_state "$root" "$ticket" "$role" || {
        failed_stages[$i]=state-transition
        states[$i]=failed; failed_count=$((failed_count + 1)); continue
      }
      read -r account family < <(python3 - "$root/worktrees/$ticket/factory/route-plans/$ticket.json" "$role" <<'PY'
import json, sys
selection=json.load(open(sys.argv[1]))["resolution"]["selections"][sys.argv[2]]
print(selection["account_route_id"], selection["provider_family"])
PY
)
      [[ -n "$account" && -n "$family" ]] || {
        states[$i]=failed; failed_count=$((failed_count + 1)); continue;
      }
      case "$account" in
        cursor|codex-native|claude-native) ;;
        *) states[$i]=failed; failed_count=$((failed_count + 1)); continue ;;
      esac
      case "$family" in openai|anthropic) ;; *)
        states[$i]=failed; failed_count=$((failed_count + 1)); continue ;;
      esac
      product_role_run "$root" "$ticket" "${leases[$i]}" "$role" \
        >"$root/runtime/product-scheduler/$ticket-$role.log" 2>&1 &
      pids[$i]=$!; states[$i]=running; progress=1
    done
    [[ "$progress" -eq 1 ]] || sleep 1
  done
  for i in "${!PRODUCT_TICKETS[@]}"; do
    [[ "${states[$i]}" != running ]] || wait "${pids[$i]}" || true
    if [[ "${states[$i]}" == failed || "${states[$i]}" == blocked ]]; then
      subscription_env "$root" "$root/kit/scripts/dispatch-lease.sh" release \
        --ticket "${PRODUCT_TICKETS[$i]}" --lease "${leases[$i]}" >/dev/null || true
    fi
  done
  batch_finished="$(date +%s)"
  product_write_timing_report "$root" "$batch_started" "$batch_finished" ||
    die "could not persist product timing evidence"
  if [[ "$done_count" -ne "${#PRODUCT_TICKETS[@]}" ||
        "$failed_count" -ne 0 || "$blocked_count" -ne 0 ]]; then
    resume_csv=""
    for i in "${!PRODUCT_TICKETS[@]}"; do
      [[ "${states[$i]}" == failed ]] || continue
      resume_csv="${resume_csv:+$resume_csv,}${PRODUCT_TICKETS[$i]}"
      completed="$(product_completed_roles "$root" "${PRODUCT_TICKETS[$i]}")" ||
        completed=unknown
      remaining="$(product_remaining_budget "$root" "${PRODUCT_TICKETS[$i]}")" ||
        remaining=unknown
      printf 'RESUME_REASON=%s:%s\n' "${PRODUCT_TICKETS[$i]}" \
        "${resume_reasons[$i]}" >&2
      printf 'FAILED_STAGE=%s:%s\n' "${PRODUCT_TICKETS[$i]}" \
        "${failed_stages[$i]}" >&2
      printf 'COMPLETED_ROLES=%s:%s\n' "${PRODUCT_TICKETS[$i]}" \
        "$completed" >&2
      printf 'REMAINING_BUDGET_USD=%s:%s\n' "${PRODUCT_TICKETS[$i]}" \
        "$remaining" >&2
    done
    if [[ -n "$resume_csv" ]]; then
      printf 'STATUS=RESUME-REQUIRED\nRESUME_RECOMMENDED=1\nRESUME_TICKETS=%s\n' \
        "$resume_csv" >&2
      printf 'RESUME_NEXT=product-resume-plan\n' >&2
      printf "RESUME_COMMAND=bash '%s/scripts/factory-dev-lane.sh' product-resume-plan --root '%s' --tickets '%s'\n" \
        "$SOURCE_ROOT" "$root" "$resume_csv" >&2
    fi
    blocked_csv=""
    for i in "${!PRODUCT_TICKETS[@]}"; do
      [[ "${states[$i]}" == blocked ]] || continue
      blocked_csv="${blocked_csv:+$blocked_csv,}${PRODUCT_TICKETS[$i]}"
      printf 'BLOCKED_STAGE=%s:%s\n' "${PRODUCT_TICKETS[$i]}" \
        "${failed_stages[$i]}" >&2
    done
    if [[ -n "$blocked_csv" ]]; then
      printf 'STATUS=BLOCKED-ESCALATED\nBLOCKED_TICKETS=%s\n' \
        "$blocked_csv" >&2
    fi
    printf 'RETAINED_ROOT=%s\n' "$root" >&2
    die "one or more product lifecycles stopped; successful siblings were retained"
  fi
  subscription_provider_idle || die "product lifecycle left a provider process"
  echo "STATUS=AWAIT-OPERATOR"
  echo "TICKETS=${PRODUCT_TICKETS[*]}"
  echo "TIMING_REPORT=$root/runtime/product-timing.json"
  echo "ELAPSED_SECONDS=$((batch_finished - batch_started))"
}

product_export_patch() {
  local root="$1" ticket="$2" base="$3" head="$4" output="$5"
  local reviewed temporary
  reviewed="$(FACTORY_LEDGER="$root/product/factory/runtime-ledger.csv" \
    python3 - "$root/kit/scripts/ticket-pr.py" "$root/product" "$ticket" <<'PY'
import importlib.util, pathlib, sys
spec=importlib.util.spec_from_file_location("ticket_pr", sys.argv[1])
module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
print(module.latest_reviewer_head(pathlib.Path(sys.argv[2]), sys.argv[3]))
PY
  )" || return 1
  python3 - "$root/worktrees/$ticket/factory/tickets/$ticket.md" <<'PY' ||
import re, sys
verdicts=re.findall(
    r"^\s*reviewer round\s+\d+:\s*(APPROVE|REQUEST CHANGES)\s*$",
    open(sys.argv[1],encoding="utf-8").read(), re.I|re.M)
if not verdicts or verdicts[-1].upper() != "APPROVE": raise SystemExit(1)
PY
    return 1
  git -C "$root/worktrees/$ticket" merge-base --is-ancestor "$base" "$reviewed" &&
    git -C "$root/worktrees/$ticket" merge-base --is-ancestor "$reviewed" "$head" ||
    return 1
  python3 - "$root/worktrees/$ticket" "$base" "$reviewed" "$head" <<'PY' ||
import subprocess, sys
work, base, reviewed, head=sys.argv[1:]
git=lambda *args: subprocess.check_output(
    ["git","-C",work,*args],text=True).splitlines()
if any(not path.startswith("factory/")
       for path in git("diff","--name-only",reviewed,head)):
    raise SystemExit(1)
for line in git("diff","--raw","--no-abbrev",base,reviewed,
                "--",".",":(exclude)factory"):
    fields=line.split("\t",1)[0].split()
    if len(fields) < 2 or fields[0][1:] in {"120000","160000"} or \
       fields[1] in {"120000","160000"}:
        raise SystemExit(1)
for line in git("diff","--name-status","-M",base,reviewed):
    fields=line.split("\t")
    if fields[0].startswith(("R","C")) and len(fields) == 3 and \
       (fields[1].startswith("factory/") != fields[2].startswith("factory/")):
        raise SystemExit(1)
PY
    return 1
  temporary="$(mktemp "$output.tmp.XXXXXX")" || return 1
  if ! git -C "$root/worktrees/$ticket" diff --binary "$base" "$reviewed" -- . \
      ':(exclude)factory' >"$temporary" || [[ ! -s "$temporary" ]]; then
    rm -f "$temporary"
    return 1
  fi
  mv -f "$temporary" "$output" || { rm -f "$temporary"; return 1; }
  printf '%s\n' "$reviewed"
}

product_export_mbox() {
  local root="$1" ticket="$2" base="$3" reviewed="$4" output="$5"
  local temporary
  temporary="$(mktemp "$output.tmp.XXXXXX")" || return 1
  if ! python3 "$SOURCE_ROOT/scripts/lib/product-export-mbox.py" \
      --repo "$root/worktrees/$ticket" --ticket "$ticket" \
      --base "$base" --reviewed "$reviewed" --output "$temporary" ||
      [[ ! -s "$temporary" ]]; then
    rm -f "$temporary"
    return 1
  fi
  mv -f "$temporary" "$output" || { rm -f "$temporary"; return 1; }
}

validate_product_export_output() {
  local root="$1" output="$2" parent
  [[ "$output" == "$root"/* && ! -e "$output" && ! -L "$output" ]] ||
    die "product export output must be a new strict child of the lane root"
  require_lane_path "$root" "$output"
  case "$output" in
    "$root/kit"|"$root/kit"/*|"$root/product"|"$root/product"/*|\
    "$root/worktrees"|"$root/worktrees"/*|"$root/runtime"|"$root/runtime"/*|\
    "$root/session"|"$root/session"/*|"$root/session-home"|"$root/session-home"/*|\
    "$root/home"|"$root/home"/*|"$root/tmp"|"$root/tmp"/*|\
    "$root/credentials"|"$root/credentials"/*)
      die "product export output overlaps a sensitive lane path" ;;
  esac
  parent="$(dirname "$output")"
  [[ -d "$parent" && ! -L "$parent" && "$(physical "$parent")" == "$parent" &&
     "$(stat -f '%Su:%Lp' "$parent")" == "$(id -un):700" ]] ||
    die "product export output parent must be an owner-only physical directory"
}

select_product_export_tickets() {
  local selected_csv="$1"
  local -a selected
  [[ -n "$selected_csv" ]] || return 0
  IFS=, read -r -a selected <<<"$selected_csv"
  python3 - "${PRODUCT_TICKETS[@]}" -- "${selected[@]}" <<'PY' ||
import re, sys
current=sys.argv[1:sys.argv.index("--")]; selected=sys.argv[sys.argv.index("--")+1:]
if (not selected or len(set(selected)) != len(selected) or
    not set(selected) <= set(current) or
    any(not re.fullmatch(r"T-[0-9]+", ticket) for ticket in selected)):
    raise SystemExit(1)
PY
    die "product export selection is invalid"
  PRODUCT_TICKETS=("${selected[@]}")
}

export_product_checkpoint_internal() {
  local root="$1" selected_csv="$2" output="$3" ticket branch head base cleanup=1
  local -a refs=()
  require_lane_mode "$root" product
  load_product_tickets "$root"
  select_product_export_tickets "$selected_csv"
  validate_runtime_paths "$root"
  product_resume_drained "$root" ||
    die "product checkpoint requires a fully drained current-day lane"
  [[ "$output" == /* && ! -e "$output" ]] ||
    die "product checkpoint output must be a new absolute directory"
  refuse_production_path "$output"
  [[ "$(stat -f '%Su:%Lp' "$(dirname "$output")")" == "$(id -un):700" ]] ||
    die "product checkpoint parent must be owner-only"
  mkdir -m 700 "$output"
  trap '[[ "$cleanup" -eq 0 ]] || rm -rf "$output"' RETURN
  subscription_env "$root" python3 "$root/kit/scripts/ledger-view.py" refresh \
    --factory-root "$root/product" \
    --durable-ledger "$root/product/factory/ledger.csv" \
    --runtime-ledger "$root/product/factory/runtime-ledger.csv" \
    --runs-dir "$root/product/factory/runs" >/dev/null ||
    die "product checkpoint accounting could not be reduced"
  base="$(python3 - "$root/runtime/product-source.json" <<'PY'
import json, sys
print(json.load(open(sys.argv[1],encoding="utf-8"))["lane_control_sha"])
PY
)" || die "product checkpoint source is invalid"
  for ticket in "${PRODUCT_TICKETS[@]}"; do
    branch="ticket/$ticket"
    head="$(git -C "$root/origin.git" rev-parse "refs/heads/$branch")"
    [[ -z "$(git -C "$root/worktrees/$ticket" status --porcelain --untracked-files=all)" ]] &&
      git -C "$root/worktrees/$ticket" merge-base --is-ancestor "$head" HEAD ||
      die "product checkpoint ticket is dirty or has no trusted-host prefix: $ticket"
    python3 "$SOURCE_ROOT/scripts/lib/lane-path-sentinel.py" \
      "$root/worktrees/$ticket" "$base" "$head" ||
      die "product checkpoint contains a lane-local absolute path: $ticket"
    refs+=("refs/heads/$branch")
  done
  git -C "$root/origin.git" bundle create "$output/seed.bundle" "${refs[@]}" >/dev/null ||
    die "product checkpoint bundle could not be created"
  chmod 600 "$output/seed.bundle"
  write_product_checkpoint "$root" "$output/seed.bundle" \
    "$output/checkpoint.json" "${PRODUCT_TICKETS[@]}" ||
    die "product checkpoint evidence is incomplete or ambiguous"
  cleanup=0
  trap - RETURN
  echo "CHECKPOINT=$output/checkpoint.json"
  echo "SEED_BUNDLE=$output/seed.bundle"
  echo "TICKETS=${PRODUCT_TICKETS[*]}"
}

write_product_checkpoint() {
  local root="$1" bundle="$2" output="$3"; shift 3
  python3 - "$root" "$bundle" \
    "$output" "$@" <<'PY'
import csv, hashlib, json, os, pathlib, re, stat, subprocess, sys
from decimal import Decimal
root=pathlib.Path(sys.argv[1]); bundle=pathlib.Path(sys.argv[2])
output=pathlib.Path(sys.argv[3]); tickets=sys.argv[4:]
source_path=root/"runtime/product-source.json"
source=json.load(open(source_path,encoding="utf-8"))
marker_path=root/"marker.json"; marker=json.load(open(marker_path,encoding="utf-8"))
import_path=root/"runtime/product-checkpoint-import.json"
retained_path=root/"runtime/product-checkpoint-source.json"
prior={}; checkpoint=None
if import_path.exists() or retained_path.exists():
    imported_info=import_path.lstat()
    if (not stat.S_ISREG(imported_info.st_mode) or imported_info.st_nlink != 1 or
        stat.S_IMODE(imported_info.st_mode) != 0o600 or
        imported_info.st_uid != os.getuid()):
        raise SystemExit(1)
    info=retained_path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or
        stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.getuid()):
        raise SystemExit(1)
    imported=json.load(open(import_path,encoding="utf-8"))
    retained_raw=retained_path.read_bytes()
    retained_sha=hashlib.sha256(retained_raw).hexdigest()
    checkpoint=json.loads(retained_raw)
    if (imported.get("schema") != "factory-dev-product-checkpoint-import/v1" or
        imported.get("checkpoint_sha256") != retained_sha or
        source.get("seed_checkpoint_sha256") != retained_sha or
        checkpoint.get("schema") != "factory-dev-product-checkpoint/v1"):
        raise SystemExit(1)
    checkpoint_records={item["ticket"]:item for item in checkpoint["tickets"]}
    if (len(checkpoint_records) != len(checkpoint["tickets"]) or
        {item.get("ticket") for item in imported.get("tickets",[])} !=
            set(checkpoint_records)):
        raise SystemExit(1)
    for item in imported.get("tickets",[]):
        old=checkpoint_records.get(item.get("ticket"))
        if (not old or item.get("roles") != [run["role"] for run in old["roles"]] or
            item.get("spec_verdicts") != old["spec_verdicts"] or
            item.get("expected_next_stage") != old["next_stage"]):
            raise SystemExit(1)
        prior[item["ticket"]] = (item,old)
runs=root/"product/factory/runs"
ledger=root/"product/factory/runtime-ledger.csv"
manifests={}
for path in runs.glob("*.meta"):
    info=path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1: raise SystemExit(1)
    values=dict(line.split("=",1) for line in
                path.read_text(encoding="utf-8").splitlines() if "=" in line)
    manifests[values.get("run_id")]=(path,values)
rows=list(csv.DictReader(open(ledger,encoding="utf-8",newline="")))
history=(list(checkpoint["lane_charges_micro_usd"]) if checkpoint else
         source.get("resume_original_tickets",source["tickets"]))
charges={ticket:0 for ticket in history}
for _path,values in manifests.values():
    ticket=values.get("ticket")
    if ticket not in charges: continue
    state=values.get("accounting_state")
    if state in {"completed","abandoned_conservative","cancelled_conservative"}:
        amount=values.get("effective_cost") or values.get("reserved_usd")
        charges[ticket] += int(Decimal(amount)*1_000_000)
records=[]
for ticket in tickets:
    work=root/"worktrees"/ticket; ref="refs/remotes/origin/ticket/"+ticket
    successful=list(prior.get(ticket,({},{}))[1].get("roles",[]))
    imported=prior.get(ticket,({},{}))[0]
    if imported:
        git=lambda *args: subprocess.check_output(
            ["git","-C",str(work),*args],text=True).strip()
        if (git("rev-parse",imported["import_head"]) != imported["import_head"] or
            git("rev-parse",imported["import_head"]+"^{tree}") !=
                imported["import_tree"] or
            subprocess.call(["git","-C",str(work),"merge-base","--is-ancestor",
                             imported["import_head"],ref]) != 0):
            raise SystemExit(1)
    for row in rows:
        if row.get("ticket") != ticket or row.get("exit_status") != "0": continue
        role=row.get("role"); run_id=row.get("run_id")
        if role in {"reviewer","narrator"}: continue
        if role not in {
            "planner","spec-linter","test-author","builder"
        }: raise SystemExit(1)
        path,values=manifests.get(run_id,(None,{}))
        out=path.with_suffix(".out") if path else None
        if (not path or not out.is_file() or out.is_symlink() or
            values.get("phase") != "completed" or
            values.get("accounting_state") not in {"completed","abandoned_conservative"} or
            values.get("contract_version") != "1.7.0" or
            values.get("exit_status") != "0" or values.get("role_exit") != "ok" or
            values.get("task_submitted") != "1" or values.get("go_issued") != "1" or
            values.get("output_sha256") != hashlib.sha256(out.read_bytes()).hexdigest() or
            not re.fullmatch(r"[0-9a-f]{40}",values.get("role_head_before",""))):
            raise SystemExit(1)
        successful.append({
            "role":role,"run_id":run_id,
            "manifest_sha256":hashlib.sha256(path.read_bytes()).hexdigest(),
            "output_sha256":values["output_sha256"],
            "role_head_before":values["role_head_before"],
        })
    if not successful: raise SystemExit(1)
    git=lambda *args: subprocess.check_output(
        ["git","-C",str(work),*args],text=True).strip()
    text=subprocess.check_output(
        ["git","-C",str(work),"show",ref+":factory/tickets/"+ticket+".md"],
        text=True)
    state=re.findall(r"^State:\s*(Ready|Planning|Building|Review)\s*$",text,re.I|re.M)
    if len(state) != 1: raise SystemExit(1)
    all_specs=[line.strip() for line in re.findall(
        r"^\s*SPEC-LINT: (?:PASS|FAIL(?: — .+)?)\s*$",text,re.M)]
    role_names=[run["role"] for run in successful]
    sl=role_names.count("spec-linter")
    if len(all_specs) < sl: raise SystemExit(1)
    specs=all_specs[:sl]
    failures=sum(line.startswith("SPEC-LINT: FAIL") for line in specs)
    prefix=[]
    for _ in range(failures): prefix += ["planner","spec-linter"]
    if role_names == prefix:
        stage="RUN planner"
    else:
        prefix += ["planner"]
        if role_names == prefix:
            stage="RUN spec-linter"
        else:
            if len(specs) != failures+1 or specs[-1] != "SPEC-LINT: PASS":
                raise SystemExit(1)
            prefix += ["spec-linter"]
            if role_names == prefix:
                stage="RUN test-author"
            else:
                prefix += ["test-author"]
                if role_names == prefix:
                    stage="RUN builder"
                else:
                    prefix += ["builder"]
                    if role_names != prefix: raise SystemExit(1)
                    stage="RUN reviewer"
    route=subprocess.check_output(
        ["git","-C",str(work),"show",ref+":factory/route-plans/"+ticket+".json"])
    records.append({
        "ticket":ticket,"head_sha":git("rev-parse",ref),
        "head_tree":git("rev-parse",ref+"^{tree}"),
        "ticket_blob":git("rev-parse",ref+":factory/tickets/"+ticket+".md"),
        "route_plan_sha256":hashlib.sha256(route).hexdigest(),
        "next_stage":stage,"state":state[0].title(),
        "roles":successful,"spec_verdicts":specs,
    })
value={
    "schema":"factory-dev-product-checkpoint/v1",
    "base_sha":source["base_sha"],"base_tree":source["base_tree"],
    "source_factory_sha":marker["kit_sha"],"source_factory_tree":marker["kit_tree"],
    "source_marker_sha256":hashlib.sha256(marker_path.read_bytes()).hexdigest(),
    "source_product_sha256":hashlib.sha256(source_path.read_bytes()).hexdigest(),
    "prior_accounting_sha256":source.get("seed_accounting_sha256"),
    "seed_bundle_sha256":hashlib.sha256(bundle.read_bytes()).hexdigest(),
    "lane_charges_micro_usd":charges,
    "tickets":records,
}
output.write_text(json.dumps(value,sort_keys=True,separators=(",",":"))+"\n",
                  encoding="utf-8")
os.chmod(output,0o600)
PY
}

product_export_roles_complete() {
  local root="$1" ticket="$2"
  python3 - "$root/product/factory/runs" "$ticket" \
    "$root/runtime/product-checkpoint-import.json" <<'PY'
import json, pathlib, sys
root=pathlib.Path(sys.argv[1]); ticket=sys.argv[2]; checkpoint=pathlib.Path(sys.argv[3])
roles={}; current=set()
if checkpoint.is_file():
    value=json.load(open(checkpoint,encoding="utf-8"))
    records=[item for item in value["tickets"] if item["ticket"] == ticket]
    if records:
        roles.update({role:"checkpoint" for role in records[0]["roles"]})
for path in root.glob("*.meta"):
    values=dict(line.split("=",1) for line in path.read_text(errors="replace").splitlines()
                if "=" in line)
    if (values.get("ticket") == ticket and
        values.get("accounting_state") in {"completed", "abandoned_conservative"} and
        values.get("exit_status") == "0"):
        roles[values.get("role")]=path; current.add(values.get("role"))
expected={"planner","spec-linter","test-author","builder","reviewer","narrator"}
if set(roles) != expected or not {"reviewer","narrator"} <= current:
    raise SystemExit(1)
PY
}

export_product_internal() {
  local root="$1" selected_csv="${2:-}" requested_output="${3:-}"
  local ticket base head branch export_dir reviewed cleanup=1
  require_lane_mode "$root" product
  load_product_tickets "$root"
  select_product_export_tickets "$selected_csv"
  validate_runtime_paths "$root"
  [[ ! -e "$root/runtime/product-approval" ]] || die "product run approval is still unused"
  python3 "$root/kit/scripts/provider-coordinator.py" \
    --db "$root/runtime/provider-state.sqlite3" status | python3 -c '
import json, sys
value=json.load(sys.stdin)
assert value.get("active_reserve_micro_usd") == 0, value
assert all(name == "terminal" for name in value.get("counts", {})), value
' || die "product provider attempts have not drained"
  [[ ! -d "$root/product/factory/.dispatch-leases" ||
     -z "$(find "$root/product/factory/.dispatch-leases" -type f -print -quit)" ]] ||
    die "product dispatcher leases have not drained"
  [[ ! -d "$root/product/factory/.active-runs" ||
     -z "$(find "$root/product/factory/.active-runs" -mindepth 1 -print -quit)" ]] ||
    die "product active-run claims have not drained"
  base="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["base_sha"])' \
    "$root/runtime/product-source.json")"
  export_dir="${requested_output:-$root/export}"
  validate_product_export_output "$root" "$export_dir"
  mkdir -m 700 "$export_dir" ||
    die "product export output could not be claimed"
  trap 'status=$?; [[ "$cleanup" -eq 0 ]] || rm -rf -- "$export_dir"; exit "$status"' EXIT
  trap '[[ "$cleanup" -eq 0 ]] || rm -rf -- "$export_dir"' RETURN
  for ticket in "${PRODUCT_TICKETS[@]}"; do
    branch="ticket/$ticket"; head="$(git -C "$root/worktrees/$ticket" rev-parse HEAD)"
    [[ -z "$(git -C "$root/worktrees/$ticket" status --porcelain --untracked-files=all)" ]] ||
      die "product ticket worktree is dirty: $ticket"
    [[ "$head" == "$(git -C "$root/origin.git" rev-parse "refs/heads/$branch")" ]] ||
      die "product ticket remote does not match trusted host output: $ticket"
    grep -qx 'State: Review' "$root/worktrees/$ticket/factory/tickets/$ticket.md" ||
      die "product ticket is not in Review: $ticket"
    product_export_roles_complete "$root" "$ticket" ||
      die "product ticket role evidence is incomplete: $ticket"
    git -C "$root/origin.git" bundle create "$export_dir/$ticket.bundle" \
      "refs/heads/$branch" >/dev/null
    reviewed="$(product_export_patch "$root" "$ticket" "$base" "$head" \
      "$export_dir/$ticket.patch")" ||
      die "product ticket has no safe approved application patch: $ticket"
    product_export_mbox "$root" "$ticket" "$base" "$reviewed" \
      "$export_dir/$ticket.mbox" ||
      die "product ticket has no safe role-preserving application mailbox: $ticket"
    printf '%s\n' "$reviewed" >"$export_dir/$ticket.reviewed"
  done
  python3 - "$root/runtime/product-source.json" "$export_dir/manifest.json" \
    "$root" "$export_dir" "${PRODUCT_TICKETS[@]}" <<'PY'
import hashlib, json, os, pathlib, subprocess, sys
source_path, out_path, root, export_dir, *tickets=sys.argv[1:]
source=json.load(open(source_path, encoding="utf-8")); records=[]
for ticket in tickets:
    work=pathlib.Path(root,"worktrees",ticket); export=pathlib.Path(export_dir)
    head=subprocess.check_output(["git","-C",str(work),"rev-parse","HEAD"],text=True).strip()
    tree=subprocess.check_output(["git","-C",str(work),"rev-parse","HEAD^{tree}"],text=True).strip()
    route=work/"factory"/"route-plans"/(ticket+".json")
    patch=export/(ticket+".patch"); mbox=export/(ticket+".mbox")
    bundle=export/(ticket+".bundle")
    reviewed=(export/(ticket+".reviewed")).read_text(encoding="utf-8").strip()
    reviewed_tree=subprocess.check_output(
        ["git","-C",str(work),"rev-parse",reviewed+"^{tree}"],text=True).strip()
    digest=lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
    records.append({"ticket":ticket,"head_sha":head,"head_tree":tree,
                    "reviewed_sha":reviewed,"reviewed_tree":reviewed_tree,
                    "route_plan_sha256":digest(route),"patch_sha256":digest(patch),
                    "mbox_sha256":digest(mbox),
                    "bundle_sha256":digest(bundle)})
value={"schema":"factory-dev-product-export/v3","base_sha":source["base_sha"],
       "base_tree":source["base_tree"],"factory_sha":subprocess.check_output(
       ["git","-C",str(pathlib.Path(root,"kit")),"rev-parse","HEAD"],text=True).strip(),
       "tickets":records}
pathlib.Path(out_path).write_text(json.dumps(value,sort_keys=True,separators=(",",":"))+"\n")
os.chmod(out_path,0o600)
PY
  chmod 600 "$export_dir/"*
  cleanup=0
  trap - EXIT RETURN
  echo "EXPORT_ROOT=$export_dir"
  echo "TICKETS=${PRODUCT_TICKETS[*]}"
}

cursor_probe_and_pin() {
  local root="$1" version openai anthropic route_plan approval_hash
  require_lane_mode "$root" cursor
  version="$("$root/home/agent" --version | awk 'NF {print $NF; exit}')"
  [[ -n "$version" ]] || die "Cursor version probe was empty"
  openai="$(python3 - "$root/kit/scripts/model-routing/catalog-v1.json" <<'PY'
import json, sys
v=json.load(open(sys.argv[1]))
for r in v["routes"]:
    if r["route_id"] == "cursor-gpt-5.6-sol-high": print(r["selection_id"])
PY
)"
  anthropic="$(python3 - "$root/kit/scripts/model-routing/catalog-v1.json" <<'PY'
import json, sys
v=json.load(open(sys.argv[1]))
for r in v["routes"]:
    if r["route_id"] == "cursor-claude-fable-5-thinking-medium": print(r["selection_id"])
PY
)"
  [[ -n "$openai" && -n "$anthropic" ]] || die "Cursor catalog routes are missing"
  cat > "$root/home/.factory/global.env" <<EOF
GLOBAL_DAILY_CAP_USD=100.00
FACTORY_CURSOR_FALLBACK_ENABLED=1
AGENT_CLI_CREDENTIAL_STORE=file
CURSOR_AGENT_VERSION=$version
CURSOR_OPENAI_MODEL=$openai
CURSOR_ANTHROPIC_MODEL=$anthropic
EOF
  chmod 600 "$root/home/.factory/global.env"
  lane_cursor_env "$root" \
    "$root/kit/scripts/model-control.sh" pin --ticket "$TICKET" \
    --workdir "$root/worktrees/$TICKET" >/dev/null
  route_plan="$root/worktrees/$TICKET/factory/route-plans/$TICKET.json"
  python3 - "$route_plan" <<'PY'
import json, sys
v=json.load(open(sys.argv[1]))
roles=v["resolution"]["selections"]
expected={"planner","spec-linter","test-author","builder","reviewer","narrator"}
if set(roles) != expected or any(not x["adapter"].startswith("cursor-") for x in roles.values()):
    raise SystemExit("route plan is not Cursor-only")
PY
  approval_hash="$(cursor_approval_hash "$root" "$version")"
  printf 'approval_hash=%s\nused=0\n' "$approval_hash" > "$root/runtime/cursor-approval"
  chmod 600 "$root/runtime/cursor-approval"
  echo "APPROVE_HASH=$approval_hash"
}

run_cursor_internal() {
  local root="$1" supplied="$2" stored role output latest version instruction
  require_lane_mode "$root" cursor
  [[ -f "$root/runtime/cursor-approval" && ! -L "$root/runtime/cursor-approval" ]] ||
    die "Cursor approval is missing or already used"
  stored="$(sed -n 's/^approval_hash=//p' "$root/runtime/cursor-approval")"
  [[ "$stored" == "$supplied" && "$(sed -n 's/^used=//p' "$root/runtime/cursor-approval")" == 0 ]] ||
    die "Cursor approval hash does not match or was already used"
  version="$("$root/home/agent" --version | awk 'NF {print $NF; exit}')"
  [[ "$(cursor_approval_hash "$root" "$version")" == "$supplied" ]] ||
    die "Cursor approval inputs drifted after planning"
  mv "$root/runtime/cursor-approval" "$root/runtime/cursor-approval.used"
  for role in planner spec-linter test-author builder reviewer narrator; do
    [[ "$(next_stage "$root")" == "RUN $role" ]] || die "sequencer did not authorize $role"
    instruction="Execute the authorized disposable lifecycle stage for $TICKET. Work only in this lane. Mutating roles must commit their scoped result; reviewer must remain read-only."
    if [[ "$role" == reviewer ]]; then
      instruction="$instruction End the final response with a separate line containing exactly APPROVE or REQUEST CHANGES."
    fi
    lane_cursor_env "$root" \
      "$root/kit/scripts/run-agent.sh" --role "$role" --ticket "$TICKET" \
      --prompt-file "$root/kit/roles/$role.md" --workdir "$root/worktrees/$TICKET" -- \
      "$instruction"
    if [[ "$role" == reviewer ]]; then
      latest="$(ls -t "$root/product/factory/runs/"*.out | head -n 1)"
      python3 "$root/kit/scripts/lib/cursor-result.py" < "$latest" |
        grep -Eiq '^[[:space:]#*]*(((Review[[:space:]]+)?Verdict:[[:space:]*]*)?APPROVE|Review[[:space:]]+verdict:[[:space:]]+T-[0-9]+[[:space:]]+—[[:space:]]+APPROVE)[*[:space:]]*$' ||
        die "Reviewer did not return an unambiguous APPROVE verdict"
      append_commit_push "$root" 'reviewer round 1: APPROVE' "$TICKET: record Cursor review"
    fi
  done
  set_review_state "$root"
  output="$(next_stage "$root")"
  [[ "$output" == AWAIT-OPERATOR* ]] || die "Cursor lifecycle did not reach AWAIT-OPERATOR"
  echo "STATUS=AWAIT-OPERATOR"
  echo "ROLES=$ROLES"
}

run_in_sandbox() {
  local root="$1" profile="$2" cursor_home
  shift 2
  if [[ -d "$root/session-home" ]]; then
    cursor_home="$root/session-home"
  else
    cursor_home="$(cursor_session_home)"
  fi
  (
    bridge=""
    cleanup_bridge() {
      [[ -n "$bridge" ]] || return 0
      target="$(python3 - "$bridge" <<'PY' 2>/dev/null || true
import os, sys
print(os.path.realpath(sys.argv[1]))
PY
)"
      if [[ -L "$bridge" && "$target" == "$root/runtime/cursor-tmp" ]]; then
        rm -f -- "$bridge"
      elif [[ -d "$bridge" && ! -L "$bridge" ]] &&
           subscription_provider_idle &&
           cleanup_empty_cursor_bridge "$bridge"; then
        :
      else
        echo "factory-dev-lane: Cursor temporary bridge changed; refusing cleanup" >&2
        return 1
      fi
    }
    if [[ "$profile" == cursor ]]; then
      bridge="$(cursor_tmp_bridge)"
      if [[ -e "$bridge" || -L "$bridge" ]]; then
        [[ -d "$bridge" && ! -L "$bridge" ]] &&
          subscription_provider_idle &&
          cleanup_empty_cursor_bridge "$bridge" ||
          die "Cursor temporary bridge path is already in use"
      fi
      mkdir -p "$root/runtime/cursor-tmp"
      chmod 700 "$root/runtime/cursor-tmp"
      ln -s "$root/runtime/cursor-tmp" "$bridge" ||
        die "Cursor temporary bridge claim failed"
      trap cleanup_bridge EXIT HUP INT TERM
    fi
    cd "$root"
    if [[ "$profile" == cursor || "$profile" == subscription ]]; then
      env -i HOME="$root/home" TMPDIR="$root/tmp" LANG=C LC_ALL=C \
        PATH="$root/home:/usr/bin:/bin:/usr/sbin:/sbin" \
        FACTORY_CURSOR_SESSION_HOME="$cursor_home" \
        bash "$root/kit/scripts/factory-dev-lane.sh" "$@"
    else
      HOME="$root/home" TMPDIR="$root/tmp" \
        "$(sandbox_exec)" -f "$root/runtime/$profile.sb" \
          env -i HOME="$root/home" TMPDIR="$root/tmp" LANG=C LC_ALL=C \
            PATH="$root/home:/usr/bin:/bin:/usr/sbin:/sbin" \
            FACTORY_CURSOR_SESSION_HOME="$cursor_home" \
            bash "$root/kit/scripts/factory-dev-lane.sh" "$@"
    fi
  )
}

cursor_tmp_bridge() {
  if [[ "$TEST_MODE" -eq 1 && -n "${FACTORY_DEV_LANE_CURSOR_TMP_BRIDGE:-}" ]]; then
    printf '%s\n' "$FACTORY_DEV_LANE_CURSOR_TMP_BRIDGE"
  else
    printf '%s\n' /private/tmp/.cursor
  fi
}

cursor_session_home() {
  if [[ -n "${FACTORY_CURSOR_SESSION_HOME:-}" ]]; then
    physical "$FACTORY_CURSOR_SESSION_HOME"
  elif [[ "$TEST_MODE" -eq 1 && -n "${FACTORY_DEV_LANE_CURSOR_SESSION_HOME:-}" ]]; then
    physical "$FACTORY_DEV_LANE_CURSOR_SESSION_HOME"
  else
    printf '%s\n' "$ACCOUNT_HOME"
  fi
}

command="${1:-}"; [[ $# -gt 0 ]] && shift || true
case "$command" in
  mock)
    assert_macos
    keep=0
    case "${1:-}" in --keep) keep=1; shift ;; "") ;; *) usage ;; esac
    [[ $# -eq 0 ]] || usage
    start="$(date +%s)"; root="$(create_lane mock)"
    if run_in_sandbox "$root" mock __mock-run --root "$root"; then
      elapsed=$(( $(date +%s) - start ))
      echo "ROOT=$root"
      echo "STATUS=AWAIT-OPERATOR"
      echo "ROLES=$ROLES"
      echo "ELAPSED_SECONDS=$elapsed"
      if [[ "$keep" -eq 0 ]]; then clean_lane "$root" >/dev/null; fi
    else
      echo "ROOT=$root" >&2
      die "mock lifecycle failed; lane retained for inspection"
    fi
    ;;
  mock-concurrency)
    assert_macos
    keep=0
    case "${1:-}" in --keep) keep=1; shift ;; "") ;; *) usage ;; esac
    [[ $# -eq 0 ]] || usage
    start="$(date +%s)"; root="$(create_lane mock-concurrency)"
    if run_in_sandbox "$root" mock __mock-concurrency-run --root "$root"; then
      elapsed=$(( $(date +%s) - start ))
      echo "ROOT=$root"
      echo "STATUS=AWAIT-OPERATOR"
      echo "TICKETS=${TICKETS[*]}"
      echo "ELAPSED_SECONDS=$elapsed"
      if [[ "$keep" -eq 0 ]]; then clean_lane "$root" >/dev/null; fi
    else
      echo "ROOT=$root" >&2
      die "mock concurrency lifecycle failed; lane retained for inspection"
    fi
    ;;
  cursor-plan)
    assert_macos
    [[ $# -eq 0 ]] || usage
    root="$(create_lane cursor)"
    echo "ROOT=$root"
    run_in_sandbox "$root" cursor __cursor-plan --root "$root"
    ;;
  cursor-run)
    assert_macos
    root=""; approve=""
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --root) root="${2:-}"; shift 2 ;;
        --approve-hash) approve="${2:-}"; shift 2 ;;
        *) usage ;;
      esac
    done
    [[ -n "$root" && "$approve" =~ ^[0-9a-f]{64}$ ]] || usage
    root="$(validate_lane "$root")"
    run_in_sandbox "$root" cursor __cursor-run \
      --root "$root" --approve-hash "$approve"
    ;;
  subscription-plan)
    assert_macos
    adapter=codex
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --adapter) adapter="${2:-}"; shift 2 ;;
        *) usage ;;
      esac
    done
    [[ "$adapter" == codex || "$adapter" == claude ]] || usage
    root="$(export FACTORY_SUBSCRIPTION_ADAPTER="$adapter"; create_lane subscription)"
    echo "ROOT=$root"
    if ! run_in_sandbox "$root" subscription __subscription-plan --root "$root"; then
      echo "ROOT=$root" >&2
      die "subscription canary planning failed; lane retained for inspection"
    fi
    ;;
  subscription-run)
    assert_macos
    root=""; approve=""
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --root) root="${2:-}"; shift 2 ;;
        --approve-hash) approve="${2:-}"; shift 2 ;;
        *) usage ;;
      esac
    done
    [[ -n "$root" && "$approve" =~ ^[0-9a-f]{64}$ ]] || usage
    root="$(validate_lane "$root")"
    run_in_sandbox "$root" subscription __subscription-run \
      --root "$root" --approve-hash "$approve"
    ;;
  product-seed-lineage)
    accounting=""; output=""; parent_accounting=""
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --accounting) accounting="${2:-}"; shift 2 ;;
        --output) output="${2:-}"; shift 2 ;;
        --parent-accounting) parent_accounting="${2:-}"; shift 2 ;;
        *) usage ;;
      esac
    done
    [[ -n "$accounting" && -n "$output" ]] || usage
    write_product_seed_lineage "$accounting" "$output" "$parent_accounting"
    echo "SEED_LINEAGE=$output"
    ;;
  product-plan)
    assert_macos
    source_repo=""; base_sha=""; ticket_csv=""; seed_bundle=""; seed_accounting=""
    seed_lineage=""; seed_checkpoint=""
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --source) source_repo="${2:-}"; shift 2 ;;
        --base-sha) base_sha="${2:-}"; shift 2 ;;
        --tickets) ticket_csv="${2:-}"; shift 2 ;;
        --seed-bundle) seed_bundle="${2:-}"; shift 2 ;;
        --seed-accounting) seed_accounting="${2:-}"; shift 2 ;;
        --seed-lineage) seed_lineage="${2:-}"; shift 2 ;;
        --seed-checkpoint) seed_checkpoint="${2:-}"; shift 2 ;;
        *) usage ;;
      esac
    done
    [[ "$source_repo" == /* && "$base_sha" =~ ^[0-9a-f]{40}$ && -n "$ticket_csv" ]] || usage
    refuse_production_path "$source_repo"
    PRODUCT_SOURCE="$source_repo"; PRODUCT_BASE="$base_sha"; PRODUCT_SEED_BUNDLE="$seed_bundle"
    IFS=, read -r -a PRODUCT_TICKETS <<<"$ticket_csv"
    [[ "${#PRODUCT_TICKETS[@]}" -ge 1 && "${#PRODUCT_TICKETS[@]}" -le 4 ]] || usage
    python3 - "${PRODUCT_TICKETS[@]}" <<'PY' || usage
import re, sys
tickets=sys.argv[1:]
if len(set(tickets)) != len(tickets) or any(not re.fullmatch(r"T-[0-9]+", t) for t in tickets):
    raise SystemExit(1)
PY
    if [[ -n "$seed_bundle" || -n "$seed_accounting" || -n "$seed_lineage" ||
          -n "$seed_checkpoint" ]]; then
      [[ -n "$seed_bundle" && -n "$seed_accounting" && -n "$seed_lineage" ]] || usage
      validate_product_seed_accounting "$seed_accounting" "$seed_bundle" "$base_sha" \
        "${PRODUCT_TICKETS[@]}"
      if [[ -n "$seed_checkpoint" ]]; then
        validate_product_checkpoint "$seed_checkpoint" "$seed_bundle" "$base_sha" \
          "${PRODUCT_TICKETS[@]}"
        validate_checkpoint_accounting "$seed_accounting" "$seed_checkpoint"
      else
        python3 - "$seed_accounting" <<'PY' ||
import json, sys
raise SystemExit(1 if json.load(open(sys.argv[1])).get("schema","").endswith("/v5") else 0)
PY
          die "checkpoint accounting requires its checkpoint"
      fi
    fi
    PRODUCT_SEED_ACCOUNTING="$seed_accounting"; PRODUCT_SEED_LINEAGE="$seed_lineage"
    PRODUCT_SEED_CHECKPOINT="$seed_checkpoint"
    root="$(create_lane product)"
    plan_output=""
    if ! plan_output="$(run_in_sandbox "$root" cursor __product-plan --root "$root")"; then
      echo "ROOT=$root" >&2
      die "product planning failed; lane retained for inspection"
    fi
    if [[ -n "$seed_accounting" ]] &&
       ! (consume_product_seed_authorization "$seed_accounting" \
           "$(sha256_file "$seed_accounting")" "$seed_lineage"); then
      clean_lane "$root" ||
        die "seed authorization lost its race and lane cleanup failed: $root"
      die "product seed accounting lineage is stale or already consumed"
    fi
    echo "ROOT=$root"
    printf '%s\n' "$plan_output"
    ;;
  product-resume-plan)
    assert_macos
    root=""; ticket_csv=""
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --root) root="${2:-}"; shift 2 ;;
        --tickets) ticket_csv="${2:-}"; shift 2 ;;
        *) usage ;;
      esac
    done
    [[ -n "$root" && -n "$ticket_csv" ]] || usage
    root="$(validate_lane "$root")"
    product_resume_plan "$root" "$ticket_csv"
    ;;
  product-run)
    assert_macos
    root=""; approve=""; resume=0
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --root) root="${2:-}"; shift 2 ;;
        --approve-hash) approve="${2:-}"; shift 2 ;;
        *) usage ;;
      esac
    done
    [[ -n "$root" && "$approve" =~ ^[0-9a-f]{64}$ ]] || usage
    root="$(validate_lane "$root")"
    if python3 - "$root/runtime/product-source.json" <<'PY'
import json, sys
raise SystemExit(0 if "resume_sha256" in json.load(open(sys.argv[1])) else 1)
PY
    then
      resume=1
      subscription_ready "$root"
      validate_product_resume_basis "$root" 1 ||
        die "product resume basis drifted before execution"
    fi
    if [[ "$resume" -eq 1 ]]; then
      if run_product_internal "$root" "$approve" 1; then
        restore_product_resume_source "$root"
      else
        exit $?
      fi
    elif run_in_sandbox "$root" cursor __product-run \
      --root "$root" --approve-hash "$approve"; then
      :
    else
      exit $?
    fi
    ;;
  product-export)
    root=""; ticket_csv=""; output=""
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --root) root="${2:-}"; shift 2 ;;
        --tickets) ticket_csv="${2:-}"; shift 2 ;;
        --output) output="${2:-}"; shift 2 ;;
        *) usage ;;
      esac
    done
    [[ -n "$root" ]] || usage
    root="$(validate_lane "$root")"
    args=(__product-export --root "$root")
    [[ -z "$ticket_csv" ]] || args+=(--tickets "$ticket_csv")
    [[ -z "$output" ]] || args+=(--output "$output")
    run_in_sandbox "$root" mock "${args[@]}"
    ;;
  product-checkpoint-export)
    root=""; ticket_csv=""; output=""
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --root) root="${2:-}"; shift 2 ;;
        --tickets) ticket_csv="${2:-}"; shift 2 ;;
        --output) output="${2:-}"; shift 2 ;;
        *) usage ;;
      esac
    done
    [[ -n "$root" && -n "$ticket_csv" && -n "$output" ]] || usage
    root="$(validate_lane "$root")"
    export_product_checkpoint_internal "$root" "$ticket_csv" "$output"
    ;;
  clean)
    root=""; [[ "${1:-}" == --root ]] && { root="${2:-}"; shift 2; } || usage
    [[ $# -eq 0 && -n "$root" ]] || usage
    clean_lane "$root"
    ;;
  __mock-run)
    [[ "${1:-}" == --root ]] || usage; root="$(validate_lane "${2:-}")"
    run_mock_internal "$root"
    ;;
  __mock-concurrency-run)
    [[ "${1:-}" == --root ]] || usage; root="$(validate_lane "${2:-}")"
    run_mock_concurrency_internal "$root"
    ;;
  __cursor-plan)
    [[ "${1:-}" == --root ]] || usage; root="$(validate_lane "${2:-}")"
    cursor_probe_and_pin "$root"
    ;;
  __cursor-run)
    [[ "${1:-}" == --root && "${3:-}" == --approve-hash ]] || usage
    root="$(validate_lane "${2:-}")"; approve="${4:-}"
    [[ "$approve" =~ ^[0-9a-f]{64}$ ]] || usage
    run_cursor_internal "$root" "$approve"
    ;;
  __subscription-plan)
    [[ "${1:-}" == --root ]] || usage
    root="$(validate_lane "${2:-}")"
    subscription_probe_and_plan "$root"
    ;;
  __subscription-run)
    [[ "${1:-}" == --root && "${3:-}" == --approve-hash ]] || usage
    root="$(validate_lane "${2:-}")"; approve="${4:-}"
    [[ "$approve" =~ ^[0-9a-f]{64}$ ]] || usage
    run_subscription_internal "$root" "$approve"
    ;;
  __product-plan)
    [[ "${1:-}" == --root ]] || usage
    root="$(validate_lane "${2:-}")"
    product_probe_and_plan "$root"
    ;;
  __product-run)
    [[ "${1:-}" == --root && "${3:-}" == --approve-hash ]] || usage
    root="$(validate_lane "${2:-}")"; approve="${4:-}"
    [[ "$approve" =~ ^[0-9a-f]{64}$ ]] || usage
    run_product_internal "$root" "$approve"
    ;;
  __product-export)
    [[ "${1:-}" == --root ]] || usage
    root="$(validate_lane "${2:-}")"; shift 2; ticket_csv=""; output=""
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --tickets) ticket_csv="${2:-}"; shift 2 ;;
        --output) output="${2:-}"; shift 2 ;;
        *) usage ;;
      esac
    done
    [[ $# -eq 0 ]] || usage
    export_product_internal "$root" "$ticket_csv" "$output"
    ;;
  *) usage ;;
esac
