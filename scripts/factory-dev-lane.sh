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
ROLES=planner,spec-linter,test-author,builder,reviewer,narrator
TEST_MODE=0
if [[ "${FACTORY_DEV_LANE_TEST_MODE:-0}" == 1 &&
      "${FACTORY_TRUSTED_TEST_HARNESS:-0}" == 1 ]]; then
  TEST_MODE=1
fi

die() { echo "factory-dev-lane: $*" >&2; exit 1; }
usage() {
  cat >&2 <<'EOF'
usage: factory-dev-lane.sh mock [--keep]
       factory-dev-lane.sh cursor-plan
       factory-dev-lane.sh cursor-run --root <absolute-lane-root> --approve-hash <sha256>
       factory-dev-lane.sh clean --root <absolute-lane-root>
EOF
  exit 2
}

physical() { (cd "$1" 2>/dev/null && pwd -P); }
sha256_file() { shasum -a 256 "$1" | awk '{print $1}'; }
sha256_text() { shasum -a 256 | awk '{print $1}'; }

cursor_approval_hash() {
  local root="$1" version="$2" route_plan cursor
  route_plan="$root/worktrees/$TICKET/factory/route-plans/$TICKET.json"
  cursor="$(python3 - "$root/home/agent" <<'PY'
import os, sys
print(os.path.realpath(sys.argv[1]))
PY
)"
  [[ "$cursor" == /* && -f "$cursor" && -x "$cursor" ]] ||
    die "Cursor binary binding is unavailable"
  {
    python3 - "$root/marker.json" <<'PY'
import json, sys
print(json.load(open(sys.argv[1], encoding="utf-8"))["kit_sha"])
PY
    git -C "$root/kit" rev-parse 'HEAD^{tree}'
    git -C "$root/worktrees/$TICKET" rev-parse 'HEAD^{tree}'
    printf '%s\n' "$version" "$cursor" "$(sha256_file "$cursor")" \
      "$(sha256_file "$route_plan")" "$(basename "$root")"
  } | sha256_text
}

assert_macos() {
  local os
  if [[ "$TEST_MODE" -eq 1 ]]; then
    os="${FACTORY_DEV_LANE_UNAME:-$(uname -s)}"
  else
    [[ -z "${FACTORY_DEV_LANE_UNAME:-}" &&
       -z "${FACTORY_DEV_LANE_SANDBOX_EXEC:-}" &&
       -z "${FACTORY_DEV_LANE_CURSOR_BIN:-}" ]] ||
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
  rm -rf -- "$root"
  echo "CLEANED=$root"
}

write_seatbelt_profiles() {
  local root="$1" cursor="$2" bridge="${3:-}"
  python3 - "$root" "$cursor" "$bridge" <<'PY'
import json, os, pathlib, sys
root, cursor, bridge = sys.argv[1:]
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
for item in system + tools + [root]:
    if item not in reads: reads.append(item)
metadata={"/"}
for item in reads:
    p=pathlib.Path(item); metadata.add(str(p)); metadata.update(map(str, p.parents))
if bridge:
    p=pathlib.Path(bridge); metadata.add(str(p)); metadata.update(map(str, p.parents))
base=["(version 1)\n", "(deny default)\n", "(allow process-fork)\n",
      "(allow process-info* (target same-sandbox))\n", "(allow sysctl-read)\n",
      "(allow mach-lookup)\n"]
for item in sorted(metadata):
    base += [f"(allow file-read-metadata (literal {json.dumps(item)}))\n",
             f"(allow file-read-data (literal {json.dumps(item)}))\n"]
for item in reads:
    base += [f"(allow file-read* (subpath {json.dumps(item)}))\n",
             f"(allow process-exec (subpath {json.dumps(item)}))\n"]
base += [f"(allow file-write* (subpath {json.dumps(root)}))\n",
         '(allow file-read* (literal "/dev/null"))\n',
         '(allow file-read* (literal "/dev/random"))\n',
         '(allow file-read* (literal "/dev/urandom"))\n',
         '(allow file-write* (literal "/dev/null"))\n',
         '(allow file-read-metadata (literal "/dev"))\n',
         '(allow file-read* (subpath "/dev/fd"))\n',
         '(allow file-write* (subpath "/dev/fd"))\n',
         '(allow signal (target same-sandbox))\n',
         '(deny mach-lookup (global-name "com.apple.securityd"))\n']
pathlib.Path(root, "runtime/mock.sb").write_text("".join(base) + "(deny network*)\n")
cursor_network = ('(allow network-bind (local ip "localhost:*"))\n'
                  '(allow network-inbound (local ip "localhost:*"))\n'
                  '(allow network-outbound (remote ip "localhost:*"))\n'
                  '(allow network-outbound)\n')
if bridge:
    cursor_network += (f"(allow file-read* (subpath {json.dumps(bridge)}))\n"
                       f"(allow file-write* (subpath {json.dumps(bridge)}))\n")
pathlib.Path(root, "runtime/cursor.sb").write_text("".join(base) + cursor_network)
PY
  chmod 600 "$root/runtime/"*.sb
}

create_lane() {
  local mode="$1" root sha tree nonce cursor developer tool timeout_bin tmp_parent bridge
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
  mkdir -p "$root/home/.factory" "$root/home/.hermes/profiles/factory-dev" \
    "$root/runtime/model-state" "$root/tmp" "$root/worktrees"
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

  mkdir -p "$root/product"
  git -C "$SOURCE_ROOT" archive "$sha" conformance/app | tar -x -C "$root/product"
  mv "$root/product/conformance/app" "$root/product/app"
  rmdir "$root/product/conformance"
  mkdir -p "$root/product/factory/tickets" "$root/product/factory/route-plans" \
    "$root/product/factory/runs"
  cat > "$root/product/factory/ENVELOPE.env" <<'EOF'
PER_RUN_BUDGET_USD=10.00
PER_TICKET_BUDGET_USD=100.00
PER_RUN_MAX_TURNS=15
PER_RUN_TIMEOUT_MIN=20
DAILY_CAP_USD=100.00
EOF
  cat > "$root/product/factory/PROJECT.env" <<EOF
PROJECT_NAME=factory-dev-lane
TICKET_BRANCH_PREFIX=ticket/
TEST_PATHS="app/tests/"
WORKTREES_DIR=$root/worktrees
EOF
  printf '%s\n' "$sha" > "$root/product/factory/KIT_PIN"
  printf '%s\n' 'date,time,ticket,role,adapter,prompt_version,turns,cost_usd,exit_status,run_id,provider_family,model_id,selection_reason,cost_basis,adapter_version' > "$root/product/factory/ledger.csv"
  cat > "$root/product/factory/tickets/$TICKET.md" <<EOF
# $TICKET — synthetic JSON health response

State: Ready
Priority: low
Risk class: low
External: no
Kit-SHA: $sha

## Description

Add a zero-dependency JSON representation to Relay's health response while preserving the existing response.

## Acceptance criteria

1. The health endpoint continues to report queue and approval counts.
2. Its response is valid JSON with the existing keys unchanged.
3. Existing and new focused tests pass.
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
  git -C "$root/product" init -q
  git -C "$root/product" branch -M main
  git -C "$root/product" add .
  git -C "$root/product" -c user.name='Factory Dev Lane' -c user.email=factory-dev@local \
    commit -qm 'Create disposable factory product'
  git init -q --bare "$root/origin.git"
  git -C "$root/product" remote add origin "$root/origin.git"
  git -C "$root/product" push -q -u origin main
  git -C "$root/product" worktree add -q -b "ticket/$TICKET" \
    "$root/worktrees/$TICKET" main
  git -C "$root/worktrees/$TICKET" push -q -u origin "ticket/$TICKET"
  if [[ "$mode" == cursor ]]; then
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
  else
    cursor=/usr/bin/true
    bridge=""
  fi
  ln -s "$cursor" "$root/home/agent"
  write_seatbelt_profiles "$root" "$cursor" "$bridge"
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
  local root="$1"; shift
  env -i HOME="$root/home" TMPDIR="$root/tmp" LANG=C LC_ALL=C \
    PATH="$root/home:/usr/bin:/bin:/usr/sbin:/sbin" \
    FACTORY_ROOT="$root/product" FACTORY_GLOBAL_ENV="$root/home/.factory/global.env" \
    FACTORY_MODEL_STATE_ROOT="$root/runtime/model-state" FACTORY_PROJECT=factory-dev-lane \
    FACTORY_CERTIFIED_PRODUCT_ORIGIN="$root/origin.git" \
    FACTORY_HERMES_CONTRACT_VERSION=1.6.0 "$@"
}

lane_cursor_env() {
  local root="$1"; shift
  printf '%s\n' "$CURSOR_API_KEY" | env -i HOME="$root/home" TMPDIR="$root/tmp" \
    LANG=C LC_ALL=C PATH="$root/home:/usr/bin:/bin:/usr/sbin:/sbin" \
    FACTORY_ROOT="$root/product" FACTORY_GLOBAL_ENV="$root/home/.factory/global.env" \
    FACTORY_MODEL_STATE_ROOT="$root/runtime/model-state" FACTORY_PROJECT=factory-dev-lane \
    FACTORY_CERTIFIED_PRODUCT_ORIGIN="$root/origin.git" \
    FACTORY_HERMES_CONTRACT_VERSION=1.6.0 \
    bash -c 'IFS= read -r CURSOR_API_KEY; export CURSOR_API_KEY; exec "$@"' _ "$@"
}

next_stage() {
  local root="$1"
  lane_env "$root" "$root/kit/scripts/next-stage.sh" --ticket "$TICKET" \
    --workdir "$root/worktrees/$TICKET"
}

run_mock_internal() {
  local root="$1" role expected
  require_lane_mode "$root" mock
  for role in planner spec-linter test-author builder reviewer narrator; do
    expected="RUN $role"
    [[ "$(next_stage "$root")" == "$expected" ]] ||
      die "sequencer did not authorize $role"
    lane_env "$root" FACTORY_TEST_MODE=1 FACTORY_TRUSTED_TEST_HARNESS=1 \
      FACTORY_ADAPTER_OVERRIDE=mock \
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

cursor_probe_and_pin() {
  local root="$1" version openai anthropic route_plan approval_hash
  IFS= read -r CURSOR_API_KEY || die "Cursor credential was not supplied"
  require_lane_mode "$root" cursor
  [[ -n "$CURSOR_API_KEY" ]] || die "Cursor credential was empty"
  export CURSOR_API_KEY
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
  local root="$1" supplied="$2" stored role output latest version
  IFS= read -r CURSOR_API_KEY || die "Cursor credential was not supplied"
  require_lane_mode "$root" cursor
  [[ -n "$CURSOR_API_KEY" ]] || die "Cursor credential was empty"
  export CURSOR_API_KEY
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
    lane_cursor_env "$root" \
      "$root/kit/scripts/run-agent.sh" --role "$role" --ticket "$TICKET" \
      --prompt-file "$root/kit/roles/$role.md" --workdir "$root/worktrees/$TICKET" -- \
      "Execute the authorized disposable lifecycle stage for $TICKET. Work only in this lane. Mutating roles must commit their scoped result; reviewer must remain read-only."
    if [[ "$role" == reviewer ]]; then
      latest="$(ls -t "$root/product/factory/runs/"*.out | head -n 1)"
      grep -Eq '(^|[^A-Z])APPROVE([^A-Z]|$)' "$latest" ||
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
    HOME="$root/home" TMPDIR="$root/tmp" \
      "$(sandbox_exec)" -f "$root/runtime/$profile.sb" \
        env -i HOME="$root/home" TMPDIR="$root/tmp" LANG=C LC_ALL=C \
          PATH="$root/home:/usr/bin:/bin:/usr/sbin:/sbin" \
          bash "$root/kit/scripts/factory-dev-lane.sh" "$@"
  )
}

cursor_tmp_bridge() {
  if [[ "$TEST_MODE" -eq 1 && -n "${FACTORY_DEV_LANE_CURSOR_TMP_BRIDGE:-}" ]]; then
    printf '%s\n' "$FACTORY_DEV_LANE_CURSOR_TMP_BRIDGE"
  else
    printf '%s\n' /private/tmp/.cursor
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
  cursor-plan)
    assert_macos
    [[ $# -eq 0 ]] || usage
    [[ -n "${CURSOR_API_KEY:-}" ]] || die "CURSOR_API_KEY is required"
    [[ "${FACTORY_DEV_CURSOR_CREDENTIAL:-}" == dedicated ]] ||
      die "set FACTORY_DEV_CURSOR_CREDENTIAL=dedicated to attest this is not a production credential"
    root="$(create_lane cursor)"
    echo "ROOT=$root"
    printf '%s\n' "$CURSOR_API_KEY" | run_in_sandbox "$root" cursor __cursor-plan --root "$root"
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
    [[ -n "${CURSOR_API_KEY:-}" ]] || die "CURSOR_API_KEY is required"
    [[ "${FACTORY_DEV_CURSOR_CREDENTIAL:-}" == dedicated ]] ||
      die "set FACTORY_DEV_CURSOR_CREDENTIAL=dedicated to attest this is not a production credential"
    printf '%s\n' "$CURSOR_API_KEY" | run_in_sandbox "$root" cursor __cursor-run \
      --root "$root" --approve-hash "$approve"
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
  *) usage ;;
esac
