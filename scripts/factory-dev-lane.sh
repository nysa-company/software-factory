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
       factory-dev-lane.sh subscription-plan
       factory-dev-lane.sh subscription-run --root <absolute-lane-root> --approve-hash <sha256>
       factory-dev-lane.sh product-plan --source <absolute-repo> --base-sha <full-sha> --tickets <T-NNN,...>
       factory-dev-lane.sh product-run --root <absolute-lane-root> --approve-hash <sha256>
       factory-dev-lane.sh product-export --root <absolute-lane-root>
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

subscription_ready() {
  local root="$1" session_home="$1/session-home" cursor_home i
  cursor_home="$(cursor_session_home)"
  for i in 1 2 3; do
    HOME="$cursor_home" "$root/home/timeout" 10 "$root/home/agent" status >/dev/null 2>&1 && break
    [[ "$i" -lt 3 ]] || die "Cursor subscription authentication is unavailable"
  done
  for i in 1 2 3; do
    (cd "$root" && HOME="$session_home" "$root/home/timeout" 10 "$root/home/codex" login status >/dev/null 2>&1) && break
    [[ "$i" -lt 3 ]] || die "Codex subscription authentication is unavailable"
  done
  for i in 1 2 3; do
    (cd "$root" && HOME="$session_home" "$root/home/timeout" 10 "$root/home/claude" auth status >/dev/null 2>&1) && break
    [[ "$i" -lt 3 ]] || die "Claude subscription authentication is unavailable"
  done
}

subscription_approval_hash() {
  local root="$1" session_home real tool cursor_home
  session_home="$root/session-home"
  cursor_home="$(cursor_session_home)"
  {
    python3 - "$root/marker.json" <<'PY'
import json, sys
value=json.load(open(sys.argv[1], encoding="utf-8"))
print(value["kit_sha"]); print(value["nonce"])
PY
    git -C "$root/kit" rev-parse 'HEAD^{tree}'
    git -C "$root/product" rev-parse 'HEAD^{tree}'
    for tool in agent codex claude; do
      real="$(python3 - "$root/home/$tool" <<'PY'
import os, sys
print(os.path.realpath(sys.argv[1]))
PY
)"
      printf '%s\n' "$real" "$(sha256_file "$real")" "$(cd "$root" && "$root/home/$tool" --version 2>/dev/null | head -n1)"
    done
    (cd "$root" && HOME="$session_home" "$root/home/codex" login status 2>/dev/null) | sha256_text
    (cd "$root" && HOME="$session_home" "$root/home/claude" auth status 2>/dev/null) | sha256_text
    sha256_file "$root/runtime/provider-policy.json"
    sha256_file "$root/runtime/provider-activation.json"
    sha256_file "$root/home/record-provider-call"
    sha256_file "$root/home/.factory/global.env"
    sha256_file "$root/runtime/cursor.sb"
    sha256_file "$root/runtime/native.sb"
    sha256_file "$root/runtime/claude-settings.json"
    sha256_file "$cursor_home/.cursor/auth.json"
    sha256_file "$cursor_home/.cursor/cli-config.json"
    sha256_file "$session_home/.codex/auth.json"
    sha256_file "$session_home/.claude/.credentials.json"
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

create_lane() {
  local mode="$1" root sha tree nonce project cursor developer tool timeout_bin tmp_parent bridge session_home ticket port_a port_b resolved
  [[ -z "$(git -C "$SOURCE_ROOT" status --porcelain --untracked-files=all)" ]] ||
    die "Software Factory source must be clean and committed"
  sha="$(git -C "$SOURCE_ROOT" rev-parse HEAD)"
  tree="$(git -C "$SOURCE_ROOT" rev-parse 'HEAD^{tree}')"
  tmp_parent="$(physical "${TMPDIR:-/tmp}")"
  root="$(mktemp -d "$tmp_parent/nysa-sf-dev.XXXXXX")"
  root="$(physical "$root")"
  chmod 700 "$root"
  refuse_production_path "$root"
  nonce="$(basename "$root" | sed 's/^nysa-sf-dev\.//')"
  project="factory-dev-lane-$(printf '%s' "$nonce" | tr '[:upper:]' '[:lower:]')"
  mkdir -p "$root/home/.factory" "$root/home/.hermes/profiles/factory-dev-$(basename "$root")" \
    "$root/runtime/model-state" "$root/runtime/provider-attempts" \
    "$root/runtime/provider-locks" "$root/runtime/provider-inputs" \
    "$root/tmp" "$root/worktrees"
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
    [[ "${#lane_tickets[@]}" -eq 4 ]] || die "product lane requires exactly four tickets"
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
path=Path(sys.argv[1]); text=path.read_text(encoding="utf-8")
text=re.sub(r"(?m)^MAX_CONCURRENT_TICKETS=.*$", "MAX_CONCURRENT_TICKETS=4", text)
if "MAX_CONCURRENT_TICKETS=" not in text:
    text += "\nMAX_CONCURRENT_TICKETS=4\n"
text=re.sub(r'(?m)^WORKTREES_DIR=.*$', f'WORKTREES_DIR="{sys.argv[2]}"', text)
path.write_text(text, encoding="utf-8")
for ticket in sys.argv[3:]:
    pass
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
    python3 - "$root/runtime/product-source.json" "$PRODUCT_BASE" \
      "$(git -C "$PRODUCT_SOURCE" rev-parse "$PRODUCT_BASE^{tree}")" "$lane_control_sha" \
      "${lane_tickets[@]}" <<'PY'
import json, os, sys
path, base, tree, control, *tickets=sys.argv[1:]
with open(path, "w", encoding="utf-8") as stream:
    json.dump({"schema":"factory-dev-product-source/v1","base_sha":base,
               "base_tree":tree,"lane_control_sha":control,"tickets":tickets},
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
  if [[ "$mode" == cursor || "$mode" == subscription || "$mode" == product ]]; then
    cursor="$(cursor_bin)"
    timeout_bin="$(command -v timeout 2>/dev/null || true)"
    [[ "$timeout_bin" == /* && -x "$timeout_bin" ]] ||
      die "Cursor lane requires the installed timeout command"
    ln -s "$(python3 - "$timeout_bin" <<'PY'
import os, sys
print(os.path.realpath(sys.argv[1]))
PY
)" "$root/home/timeout"
    bridge="$(cursor_tmp_bridge)"
    session_home="$(cursor_session_home)"
    for tool in auth.json cli-config.json; do
      [[ -f "$session_home/.cursor/$tool" && ! -L "$session_home/.cursor/$tool" ]] ||
        die "Cursor CLI session file is unavailable: $tool"
    done
  else
    cursor=/usr/bin/true
    bridge=""
    session_home=""
  fi
  ln -s "$cursor" "$root/home/agent"
  if [[ "$mode" == subscription || "$mode" == product ]]; then
    mkdir -m 700 "$root/session-home"
    mkdir -m 700 "$root/session-home/.cursor" "$root/session-home/.codex" \
      "$root/session-home/.claude"
    cp "$session_home/.cursor/auth.json" "$root/session-home/.cursor/auth.json"
    cp "$session_home/.cursor/cli-config.json" "$root/session-home/.cursor/cli-config.json"
    [[ -f "$session_home/.codex/auth.json" && ! -L "$session_home/.codex/auth.json" ]] ||
      die "Codex subscription session file is unavailable"
    cp "$session_home/.codex/auth.json" "$root/session-home/.codex/auth.json"
    [[ -f "$session_home/.claude/.credentials.json" &&
       ! -L "$session_home/.claude/.credentials.json" ]] ||
      die "Claude subscription session file is unavailable"
    cp "$session_home/.claude/.credentials.json" \
      "$root/session-home/.claude/.credentials.json"
    chmod 600 "$root/session-home/.cursor/"*.json "$root/session-home/.codex/auth.json" \
      "$root/session-home/.claude/.credentials.json"
    for tool in codex claude; do
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
account_limit={"max_concurrent":2,"max_starts":32,"window_seconds":60}
policy={"schema":"factory-provider-concurrency-policy/v1","coupled_max_concurrent":4,
        "global":global_limit,"provider_families":{"mock":global_limit},
        "account_routes":{"test-mock-a":account_limit,"test-mock-b":account_limit}}
raw=json.dumps(policy, sort_keys=True, separators=(",",":"))
with open(policy_path, "w", encoding="utf-8") as handle: handle.write(raw+"\n")
routes={}
for number in range(900001,900005):
    ticket=f"T-{number}"
    routes[f"test-mock-{ticket}"]={"account_route":"test-mock-a" if number % 2 else "test-mock-b",
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
    for tool in codex claude; do
      cat >"$root/home/$tool" <<EOF
#!/usr/bin/env bash
exec "$(sandbox_exec)" -f "$root/runtime/native.sb" "$root/home/$tool-real" "\$@"
EOF
      chmod 700 "$root/home/$tool"
    done
  fi
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
  validate_runtime_paths "$root"
  printf '%s\n' "$root"
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
  local root="$1" project session_home cursor_home cursor_version codex_version claude_version
  shift
  project="factory-dev-lane-$(basename "$root" | sed 's/^nysa-sf-dev\.//' | tr '[:upper:]' '[:lower:]')"
  session_home="$root/session-home"
  cursor_home="$(cursor_session_home)"
  cursor_version="$("$root/home/agent" --version 2>/dev/null | awk 'NF {print $NF; exit}')"
  codex_version="$(cd "$root" && "$root/home/codex" --version 2>/dev/null | awk 'NF {print $NF; exit}')"
  claude_version="$(cd "$root" && "$root/home/claude" --version 2>/dev/null | awk 'NF {print $1; exit}')"
  env -i HOME="$session_home" TMPDIR="$root/tmp" LANG=C LC_ALL=C \
    PATH="$root/home:/usr/bin:/bin:/usr/sbin:/sbin" \
    FACTORY_ROOT="$root/product" FACTORY_GLOBAL_ENV="$root/home/.factory/global.env" \
    FACTORY_MODEL_STATE_ROOT="$root/runtime/model-state" FACTORY_PROJECT="$project" \
    FACTORY_PROVIDER_DB="$root/runtime/provider-state.sqlite3" \
    FACTORY_PROVIDER_POLICY="$root/runtime/provider-policy.json" \
    FACTORY_PROVIDER_ACTIVATION="$root/runtime/provider-activation.json" \
    FACTORY_CURSOR_SESSION_HOME="$cursor_home" FACTORY_CURSOR_INTERNAL_SANDBOX=1 \
    FACTORY_CLI_LANE_ROOT="$root" FACTORY_CLI_INTERNAL_SANDBOX=1 \
    FACTORY_CLAUDE_SETTINGS="$root/runtime/claude-settings.json" \
    FACTORY_CERTIFIED_PRODUCT_ORIGIN="$root/origin.git" \
    FACTORY_HERMES_CONTRACT_VERSION=1.7.0 \
    CURSOR_AGENT_VERSION="$cursor_version" CODEX_PINNED="$codex_version" \
    CLAUDE_CODE_PINNED="$claude_version" \
    "$@"
}

product_approval_hash() {
  local root="$1" ticket tool real session_home="$1/session-home" cursor_home
  cursor_home="$(cursor_session_home)"
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
        "$(cd "$root" && "$root/home/$tool" --version 2>/dev/null | head -n1)"
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
  local root="$1" line
  PRODUCT_TICKETS=()
  while IFS= read -r line; do PRODUCT_TICKETS+=("$line"); done < <(
    python3 - "$root/runtime/product-source.json" <<'PY'
import json, re, sys
value=json.load(open(sys.argv[1], encoding="utf-8"))
if value.get("schema") != "factory-dev-product-source/v1" or len(value.get("tickets", [])) != 4:
    raise SystemExit(1)
for ticket in value["tickets"]:
    if not re.fullmatch(r"T-[0-9]+", ticket): raise SystemExit(1)
    print(ticket)
PY
  ) || die "product source binding is malformed"
  [[ "${#PRODUCT_TICKETS[@]}" -eq 4 ]] || die "product source binding is incomplete"
}

product_probe_and_plan() {
  local root="$1" cursor_version codex_version claude_version ticket profile profile_hash approval_hash
  require_lane_mode "$root" product
  load_product_tickets "$root"
  validate_runtime_paths "$root"
  subscription_ready "$root"
  cursor_version="$("$root/home/agent" --version 2>/dev/null | awk 'NF {print $NF; exit}')"
  codex_version="$(cd "$root" && "$root/home/codex" --version 2>/dev/null | awk 'NF {print $NF; exit}')"
  claude_version="$(cd "$root" && "$root/home/claude" --version 2>/dev/null | awk 'NF {print $1; exit}')"
  [[ -n "$cursor_version" && -n "$codex_version" && -n "$claude_version" ]] ||
    die "product subscription CLI version probe was empty"
  cat >"$root/home/.factory/global.env" <<EOF
GLOBAL_DAILY_CAP_USD=500.00
FACTORY_CURSOR_FALLBACK_ENABLED=1
CURSOR_AGENT_VERSION=$cursor_version
CODEX_PINNED=$codex_version
CLAUDE_CODE_PINNED=$claude_version
CURSOR_OPENAI_MODEL=gpt-5.6-sol-high
CURSOR_ANTHROPIC_MODEL=claude-fable-5-thinking-medium
FACTORY_PROBE_CLAUDE_CODE=UNAVAILABLE:lane_sandbox_temp_collision
EOF
  chmod 600 "$root/home/.factory/global.env"
  for ticket in "${PRODUCT_TICKETS[@]}"; do
    profile=balanced-v2
    [[ "$ticket" != "${PRODUCT_TICKETS[0]}" ]] || profile=cursor-priority-v1
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
for index,ticket in enumerate(tickets):
    plan=json.loads(pathlib.Path(root,"worktrees",ticket,"factory","route-plans",ticket+".json").read_text())
    selections=plan["resolution"]["selections"]
    if any(selections[r]["provider_family"]==selections[c]["provider_family"] for r in production for c in checking):
        raise SystemExit("role-family separation failed")
    if index == 0:
        if any(not value["adapter"].startswith("cursor-") for value in selections.values()):
            raise SystemExit("cursor ticket route drifted")
    else:
        if any(selections[r]["adapter"] != "codex" for r in production):
            raise SystemExit("native production route drifted")
        if any(selections[r]["adapter"] != "cursor-anthropic" for r in checking):
            raise SystemExit("checking circuit breaker did not select Cursor Claude")
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
        "account_routes":{"cursor":limit(2,15),"codex-native":limit(2,9),
                          "claude-native":limit(2,1)}}
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
  approval_hash="$(product_approval_hash "$root")"
  printf 'approval_hash=%s\nused=0\n' "$approval_hash" >"$root/runtime/product-approval"
  chmod 600 "$root/runtime/product-approval"
  echo "APPROVE_HASH=$approval_hash"
  echo "TICKETS=${PRODUCT_TICKETS[*]}"
  echo "PROVIDER_LIMITS=global:4,cursor:2,codex:2,claude:2"
}

next_stage() {
  local root="$1" lease="${2:-}"
  if [[ -n "$lease" ]]; then
    lane_env "$root" FACTORY_DISPATCH_LEASE_ID="$lease" \
      "$root/kit/scripts/next-stage.sh" --ticket "$TICKET" --lease "$lease" \
      --workdir "$root/worktrees/$TICKET"
  else
    lane_env "$root" "$root/kit/scripts/next-stage.sh" --ticket "$TICKET" \
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
    case "$ticket" in *1|*3) account=test-mock-a ;; *) account=test-mock-b ;; esac
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
  local root="$1" cursor_version codex_version claude_version approval_hash
  require_lane_mode "$root" subscription
  validate_runtime_paths "$root"
  subscription_ready "$root"
  cursor_version="$("$root/home/agent" --version 2>/dev/null | awk 'NF {print $NF; exit}')"
  codex_version="$(cd "$root" && "$root/home/codex" --version 2>/dev/null | awk 'NF {print $NF; exit}')"
  claude_version="$(cd "$root" && "$root/home/claude" --version 2>/dev/null | awk 'NF {print $1; exit}')"
  [[ -n "$cursor_version" && -n "$codex_version" && -n "$claude_version" ]] ||
    die "subscription CLI version probe was empty"
  cat > "$root/home/.factory/global.env" <<EOF
GLOBAL_DAILY_CAP_USD=1.00
FACTORY_CURSOR_FALLBACK_ENABLED=1
CURSOR_AGENT_VERSION=$cursor_version
CODEX_PINNED=$codex_version
CLAUDE_CODE_PINNED=$claude_version
CURSOR_OPENAI_MODEL=gpt-5.6-sol-high
CURSOR_ANTHROPIC_MODEL=claude-fable-5-thinking-medium
EOF
  chmod 600 "$root/home/.factory/global.env"
  python3 - "$root/runtime/provider-policy.json" "$root/runtime/provider-activation.json" <<'PY'
import hashlib, json, os, sys

policy_path, activation_path=sys.argv[1:]
def limit(concurrent):
    return {"max_concurrent":concurrent,"max_starts":4,"window_seconds":60}
policy={
    "schema":"factory-provider-concurrency-policy/v1",
    "coupled_max_concurrent":4,
    "global":limit(4),
    "provider_families":{"openai":limit(3),"anthropic":limit(1)},
    "account_routes":{
        "lane-cursor-subscription":limit(1),
        "lane-codex-subscription":limit(2),
        "lane-claude-subscription":limit(1),
    },
}
raw=json.dumps(policy, sort_keys=True, separators=(",",":"))
routes={
    "lane-subscription-T-900001":{
        "account_route":"lane-cursor-subscription","adapter":"cursor-openai",
        "model":"gpt-5.6-sol-high","provider_family":"openai"},
    "lane-subscription-T-900002":{
        "account_route":"lane-codex-subscription","adapter":"codex",
        "model":"gpt-5.6-sol","provider_family":"openai"},
    "lane-subscription-T-900003":{
        "account_route":"lane-codex-subscription","adapter":"codex",
        "model":"gpt-5.6-sol","provider_family":"openai"},
    "lane-subscription-T-900004":{
        "account_route":"lane-claude-subscription","adapter":"claude-code",
        "model":"fable","provider_family":"anthropic"},
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
  echo "PROVIDER_SPLIT=cursor:1,codex:2,claude:1"
  echo "AGGREGATE_RESERVATION_USD=1.00"
}

run_subscription_internal() {
  local root="$1" supplied="$2" stored ticket adapter family account model attempt output pid
  local day start_ns end_ns result command_path terminal_result
  local -a pids=() attempts=() outputs=()
  require_lane_mode "$root" subscription
  validate_runtime_paths "$root"
  [[ -f "$root/runtime/subscription-approval" && ! -L "$root/runtime/subscription-approval" ]] ||
    die "subscription approval is missing or already used"
  stored="$(sed -n 's/^approval_hash=//p' "$root/runtime/subscription-approval")"
  [[ "$stored" == "$supplied" && "$(sed -n 's/^used=//p' "$root/runtime/subscription-approval")" == 0 ]] ||
    die "subscription approval hash does not match or was already used"
  [[ "$(subscription_approval_hash "$root")" == "$supplied" ]] ||
    die "subscription approval inputs drifted after planning"
  subscription_ready "$root"
  subscription_provider_idle || die "another subscription provider call is active"
  mv "$root/runtime/subscription-approval" "$root/runtime/subscription-approval.used"
  day="$(date -u +%F)"
  : > "$root/runtime/provider-timeline"
  chmod 600 "$root/runtime/provider-timeline"
  for ticket in "${TICKETS[@]}"; do
    case "$ticket" in
      T-900001)
        adapter=cursor-openai; family=openai; account=lane-cursor-subscription
        model=gpt-5.6-sol-high ;;
      T-900002|T-900003)
        adapter=codex; family=openai; account=lane-codex-subscription
        model=gpt-5.6-sol ;;
      T-900004)
        adapter=claude-code; family=anthropic; account=lane-claude-subscription
        model=fable ;;
      *) die "unexpected subscription canary ticket" ;;
    esac
    attempt="$ticket-subscription-canary"
    output="$root/runtime/provider-inputs/$ticket.out"
    command_path="$root/kit/scripts/adapters/$adapter.sh"
    subscription_env "$root" python3 "$root/kit/scripts/provider-cli-runtime.py" \
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
  echo "PROVIDER_CALLS=4"
  echo "PROVIDER_MODE=cli-concurrent-v1"
  echo "PROVIDER_SPLIT=cursor:1,codex:2,claude:1"
  echo "PROVIDER_OVERLAP_MILLISECONDS=$(( (end_ns - start_ns) / 1000000 ))"
  echo "ACCOUNTED_RESERVATION_USD=1.00"
}

product_role_run() {
  local root="$1" ticket="$2" lease="$3" role="$4" instruction latest
  instruction="Execute the authorized $role stage for $ticket. Work only in this ticket worktree. Follow the frozen ticket contract and repository instructions. Mutating roles must commit their scoped durable result locally. Never push or access another worktree, remote service, credential, or Factory control path."
  instruction="$instruction Node 22 is on PATH. For database-backed checks, load only the disposable lane variables with: set -a; source '$root/runtime/product-db/$ticket.env'; set +a. Never print those variables."
  if [[ "$role" == reviewer ]]; then
    instruction="$instruction Remain read-only. End with a standalone line containing exactly APPROVE or REQUEST CHANGES."
  fi
  subscription_env "$root" FACTORY_DISPATCH_LEASE_ID="$lease" \
    "$root/kit/scripts/run-agent.sh" --role "$role" --ticket "$ticket" \
    --prompt-file "$root/kit/roles/$role.md" --workdir "$root/worktrees/$ticket" -- \
    "$instruction" || return
  if [[ "$role" == reviewer ]]; then
    latest="$(python3 - "$root/product/factory/runs" "$ticket" <<'PY'
import pathlib, sys
root=pathlib.Path(sys.argv[1]); ticket=sys.argv[2]; matches=[]
for meta in root.glob("*.meta"):
    values={}
    for line in meta.read_text(errors="replace").splitlines():
        if "=" in line:
            key,value=line.split("=",1); values[key]=value
    if values.get("ticket") == ticket and values.get("role") == "reviewer":
        matches.append((meta.stat().st_mtime_ns, meta.with_suffix(".out")))
if not matches: raise SystemExit(1)
print(max(matches)[1])
PY
)" || return 1
    grep -Eiq '^[[:space:]#*]*(((Review[[:space:]]+)?Verdict:[[:space:]*]*)?APPROVE|Review[[:space:]]+verdict:[[:space:]]+T-[0-9]+[[:space:]]+—[[:space:]]+APPROVE)[*[:space:]]*$' \
      "$latest" || return 1
    (TICKET="$ticket"; append_commit_push "$root" 'reviewer round 1: APPROVE' \
      "$ticket: record isolated review") || return
  elif [[ "$role" == narrator ]]; then
    (TICKET="$ticket"; set_review_state "$root") || return
  fi
}

run_product_internal() {
  local root="$1" supplied="$2" stored day i ticket lease_json stage role account now
  local total_active done_count failed_count progress pid rc
  local -a leases pids accounts states renewals
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
  subscription_ready "$root"
  subscription_provider_idle || die "another subscription provider call is active"
  mv "$root/runtime/product-approval" "$root/runtime/product-approval.used"
  mkdir -p "$root/runtime/product-scheduler"
  chmod 700 "$root/runtime/product-scheduler"
  for i in 0 1 2 3; do
    ticket="${PRODUCT_TICKETS[$i]}"
    lease_json="$(subscription_env "$root" "$root/kit/scripts/dispatch-lease.sh" \
      claim --ticket "$ticket")" || die "could not claim product ticket lease: $ticket"
    leases[$i]="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["lease_id"])' \
      <<<"$lease_json")"
    pids[$i]=0; accounts[$i]=""; states[$i]=idle; renewals[$i]=0
  done
  done_count=0; failed_count=0
  while [[ "$done_count" -lt 4 && $((done_count + failed_count)) -lt 4 ]]; do
    progress=0
    for i in 0 1 2 3; do
      [[ "${states[$i]}" == running ]] || continue
      pid="${pids[$i]}"
      if ! kill -0 "$pid" 2>/dev/null; then
        rc=0; wait "$pid" || rc=$?
        if [[ "$rc" -eq 0 ]]; then states[$i]=idle; else states[$i]=failed; failed_count=$((failed_count + 1)); fi
        pids[$i]=0; accounts[$i]=""; progress=1
      fi
    done
    for i in 0 1 2 3; do
      [[ "${states[$i]}" == idle ]] || continue
      ticket="${PRODUCT_TICKETS[$i]}"; now="$(date +%s)"
      if [[ $((now - renewals[$i])) -ge 120 ]]; then
        subscription_env "$root" "$root/kit/scripts/dispatch-lease.sh" renew \
          --ticket "$ticket" --lease "${leases[$i]}" >/dev/null || {
            states[$i]=failed; failed_count=$((failed_count + 1)); continue;
          }
        renewals[$i]="$now"
      fi
      stage="$(TICKET="$ticket"; next_stage "$root" "${leases[$i]}")" || {
        states[$i]=failed; failed_count=$((failed_count + 1)); continue;
      }
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
      [[ "$stage" == RUN\ * ]] || { states[$i]=failed; failed_count=$((failed_count + 1)); continue; }
      role="${stage#RUN }"
      account="$(python3 - "$root/worktrees/$ticket/factory/route-plans/$ticket.json" "$role" <<'PY'
import json, sys
print(json.load(open(sys.argv[1]))["resolution"]["selections"][sys.argv[2]]["account_route_id"])
PY
)" || { states[$i]=failed; failed_count=$((failed_count + 1)); continue; }
      total_active=0; cursor_active=0; codex_active=0; claude_active=0
      for value in "${accounts[@]}"; do
        [[ -n "$value" ]] || continue; total_active=$((total_active + 1))
        case "$value" in cursor) cursor_active=$((cursor_active + 1));; codex-native) codex_active=$((codex_active + 1));; claude-native) claude_active=$((claude_active + 1));; esac
      done
      [[ "$total_active" -lt 4 ]] || continue
      case "$account" in
        cursor) [[ "$cursor_active" -lt 2 ]] || continue ;;
        codex-native) [[ "$codex_active" -lt 2 ]] || continue ;;
        claude-native) [[ "$claude_active" -lt 2 ]] || continue ;;
        *) states[$i]=failed; failed_count=$((failed_count + 1)); continue ;;
      esac
      product_role_run "$root" "$ticket" "${leases[$i]}" "$role" \
        >"$root/runtime/product-scheduler/$ticket-$role.log" 2>&1 &
      pids[$i]=$!; accounts[$i]="$account"; states[$i]=running; progress=1
    done
    [[ "$progress" -eq 1 ]] || sleep 1
  done
  for i in 0 1 2 3; do
    [[ "${states[$i]}" != running ]] || wait "${pids[$i]}" || true
    if [[ "${states[$i]}" == failed ]]; then
      subscription_env "$root" "$root/kit/scripts/dispatch-lease.sh" release \
        --ticket "${PRODUCT_TICKETS[$i]}" --lease "${leases[$i]}" >/dev/null || true
    fi
  done
  [[ "$done_count" -eq 4 && "$failed_count" -eq 0 ]] ||
    die "one or more product lifecycles failed; successful siblings were retained"
  subscription_provider_idle || die "product lifecycle left a provider process"
  echo "STATUS=AWAIT-OPERATOR"
  echo "TICKETS=${PRODUCT_TICKETS[*]}"
}

export_product_internal() {
  local root="$1" ticket base head branch export_dir
  require_lane_mode "$root" product
  load_product_tickets "$root"
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
  export_dir="$root/export"
  [[ ! -e "$export_dir" ]] || die "product export already exists"
  mkdir -m 700 "$export_dir"
  for ticket in "${PRODUCT_TICKETS[@]}"; do
    branch="ticket/$ticket"; head="$(git -C "$root/worktrees/$ticket" rev-parse HEAD)"
    [[ -z "$(git -C "$root/worktrees/$ticket" status --porcelain --untracked-files=all)" ]] ||
      die "product ticket worktree is dirty: $ticket"
    [[ "$head" == "$(git -C "$root/origin.git" rev-parse "refs/heads/$branch")" ]] ||
      die "product ticket remote does not match trusted host output: $ticket"
    grep -qx 'State: Review' "$root/worktrees/$ticket/factory/tickets/$ticket.md" ||
      die "product ticket is not in Review: $ticket"
    grep -Eq '^reviewer round [0-9]+: APPROVE$' \
      "$root/worktrees/$ticket/factory/tickets/$ticket.md" ||
      die "product ticket lacks an approved review: $ticket"
    python3 - "$root/product/factory/runs" "$ticket" <<'PY' ||
import pathlib, sys
root=pathlib.Path(sys.argv[1]); ticket=sys.argv[2]; roles={}
for path in root.glob("*.meta"):
    values=dict(line.split("=",1) for line in path.read_text(errors="replace").splitlines() if "=" in line)
    if values.get("ticket") == ticket and values.get("accounting_state") == "completed" and values.get("exit_status") == "0":
        roles[values.get("role")]=path
expected={"planner","spec-linter","test-author","builder","reviewer","narrator"}
if set(roles) != expected: raise SystemExit(1)
PY
      die "product ticket role evidence is incomplete: $ticket"
    git -C "$root/origin.git" bundle create "$export_dir/$ticket.bundle" \
      "refs/heads/$branch" >/dev/null
    git -C "$root/worktrees/$ticket" diff --binary "$base" "$head" -- . \
      ':(exclude)factory/KIT_PIN' ':(exclude)factory/PROJECT.env' \
      >"$export_dir/$ticket.patch"
    [[ -s "$export_dir/$ticket.patch" ]] || die "product ticket export is empty: $ticket"
  done
  python3 - "$root/runtime/product-source.json" "$export_dir/manifest.json" \
    "$root" "${PRODUCT_TICKETS[@]}" <<'PY'
import hashlib, json, os, pathlib, subprocess, sys
source_path, out_path, root, *tickets=sys.argv[1:]
source=json.load(open(source_path, encoding="utf-8")); records=[]
for ticket in tickets:
    work=pathlib.Path(root,"worktrees",ticket); export=pathlib.Path(root,"export")
    head=subprocess.check_output(["git","-C",str(work),"rev-parse","HEAD"],text=True).strip()
    tree=subprocess.check_output(["git","-C",str(work),"rev-parse","HEAD^{tree}"],text=True).strip()
    route=work/"factory"/"route-plans"/(ticket+".json")
    patch=export/(ticket+".patch"); bundle=export/(ticket+".bundle")
    digest=lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
    records.append({"ticket":ticket,"head_sha":head,"head_tree":tree,
                    "route_plan_sha256":digest(route),"patch_sha256":digest(patch),
                    "bundle_sha256":digest(bundle)})
value={"schema":"factory-dev-product-export/v1","base_sha":source["base_sha"],
       "base_tree":source["base_tree"],"factory_sha":subprocess.check_output(
       ["git","-C",str(pathlib.Path(root,"kit")),"rev-parse","HEAD"],text=True).strip(),
       "tickets":records}
pathlib.Path(out_path).write_text(json.dumps(value,sort_keys=True,separators=(",",":"))+"\n")
os.chmod(out_path,0o600)
PY
  chmod 600 "$export_dir/"*
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
  local root="$1" profile="$2"; shift 2
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
      else
        echo "factory-dev-lane: Cursor temporary bridge changed; refusing cleanup" >&2
        return 1
      fi
    }
    if [[ "$profile" == cursor ]]; then
      bridge="$(cursor_tmp_bridge)"
      [[ ! -e "$bridge" && ! -L "$bridge" ]] ||
        die "Cursor temporary bridge path is already in use"
      mkdir -p "$root/runtime/cursor-tmp"
      chmod 700 "$root/runtime/cursor-tmp"
      ln -s "$root/runtime/cursor-tmp" "$bridge"
      trap cleanup_bridge EXIT HUP INT TERM
    fi
    cd "$root"
    if [[ "$profile" == cursor ]]; then
      env -i HOME="$root/home" TMPDIR="$root/tmp" LANG=C LC_ALL=C \
        PATH="$root/home:/usr/bin:/bin:/usr/sbin:/sbin" \
        FACTORY_CURSOR_SESSION_HOME="$(cursor_session_home)" \
        bash "$root/kit/scripts/factory-dev-lane.sh" "$@"
    else
      HOME="$root/home" TMPDIR="$root/tmp" \
        "$(sandbox_exec)" -f "$root/runtime/$profile.sb" \
          env -i HOME="$root/home" TMPDIR="$root/tmp" LANG=C LC_ALL=C \
            PATH="$root/home:/usr/bin:/bin:/usr/sbin:/sbin" \
            FACTORY_CURSOR_SESSION_HOME="$(cursor_session_home)" \
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
    [[ $# -eq 0 ]] || usage
    root="$(create_lane subscription)"
    echo "ROOT=$root"
    if ! run_in_sandbox "$root" cursor __subscription-plan --root "$root"; then
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
    run_in_sandbox "$root" cursor __subscription-run \
      --root "$root" --approve-hash "$approve"
    ;;
  product-plan)
    assert_macos
    source_repo=""; base_sha=""; ticket_csv=""
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --source) source_repo="${2:-}"; shift 2 ;;
        --base-sha) base_sha="${2:-}"; shift 2 ;;
        --tickets) ticket_csv="${2:-}"; shift 2 ;;
        *) usage ;;
      esac
    done
    [[ "$source_repo" == /* && "$base_sha" =~ ^[0-9a-f]{40}$ && -n "$ticket_csv" ]] || usage
    PRODUCT_SOURCE="$source_repo"; PRODUCT_BASE="$base_sha"
    IFS=, read -r -a PRODUCT_TICKETS <<<"$ticket_csv"
    [[ "${#PRODUCT_TICKETS[@]}" -eq 4 ]] || usage
    root="$(create_lane product)"
    echo "ROOT=$root"
    if ! run_in_sandbox "$root" cursor __product-plan --root "$root"; then
      echo "ROOT=$root" >&2
      die "product planning failed; lane retained for inspection"
    fi
    ;;
  product-run)
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
    run_in_sandbox "$root" cursor __product-run --root "$root" --approve-hash "$approve"
    ;;
  product-export)
    root=""; [[ "${1:-}" == --root ]] && { root="${2:-}"; shift 2; } || usage
    [[ $# -eq 0 && -n "$root" ]] || usage
    root="$(validate_lane "$root")"
    run_in_sandbox "$root" mock __product-export --root "$root"
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
    root="$(validate_lane "${2:-}")"
    export_product_internal "$root"
    ;;
  *) usage ;;
esac
