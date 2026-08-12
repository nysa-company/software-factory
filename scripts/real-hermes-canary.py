#!/usr/bin/env python3
"""Prepare a credential-free, test-mode-only real-Hermes release canary."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import stat
import subprocess
import sys
import time


SHA = re.compile(r"[0-9a-f]{40}")
ROOT = re.compile(r"/(?:private/)?tmp/nysa-sf-canary\.[A-Za-z0-9._-]+")
PROJECT = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
CURSOR_DATA_PATH_LIMIT = 75
CURSOR_ATTEMPT_PLACEHOLDER = "0000000000-0000000-cli"
SCHEMA = "nysa.software-factory.real-hermes-canary/v1"
ATTEMPT_SCHEMA = "nysa.software-factory.real-hermes-canary-attempt/v1"
COMPLETION_SCHEMA = "nysa.software-factory.real-hermes-canary-completion/v1"


class Refusal(RuntimeError):
    pass


def command(*args: str, cwd: Path | None = None) -> str:
    result = subprocess.run(
        args, cwd=cwd, text=True, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, check=False,
    )
    if result.returncode:
        reason = result.stderr.strip().splitlines()
        raise Refusal(reason[-1] if reason else f"command failed: {args[0]}")
    return result.stdout.strip()


def canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write(path: Path, data: str | bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = data.encode() if isinstance(data, str) else data
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(payload)


def protected_paths(home: Path) -> tuple[Path, ...]:
    return (
        home / ".factory",
        home / ".hermes/profiles/factory",
        home / "Library/LaunchAgents",
        home / "Projects/nysa-company/nysa-app",
    )


def overlaps(path: Path, candidate: Path) -> bool:
    try:
        path.relative_to(candidate)
        return True
    except ValueError:
        return False


def validate_inputs(args: argparse.Namespace) -> tuple[Path, Path, Path, str, str, str, str, str]:
    failures: list[str] = []
    home = Path.home().resolve()
    factory = args.factory_root.resolve()
    raw_root = args.root.absolute()
    root = Path(os.path.realpath(raw_root))
    hermes = Path(os.path.realpath(args.hermes_bin))

    if not factory.is_dir() or factory.is_symlink():
        failures.append("Factory root must be an existing non-symlink directory")
    if not ROOT.fullmatch(str(root)):
        failures.append("canary root must match /tmp/nysa-sf-canary.<id>")
    if len(str(root / "c" / CURSOR_ATTEMPT_PLACEHOLDER / "data")) > CURSOR_DATA_PATH_LIMIT:
        failures.append("canary root is too long for isolated Cursor scratch")
    if any(overlaps(root, path.resolve(strict=False)) for path in protected_paths(home)):
        failures.append("canary root overlaps production state")
    if not hermes.is_absolute() or not hermes.is_file() or hermes.is_symlink():
        failures.append("Hermes binary must be an absolute regular file")
    elif not os.access(hermes, os.X_OK):
        failures.append("Hermes binary is not executable")
    if any(overlaps(hermes, path.resolve(strict=False)) for path in protected_paths(home)):
        failures.append("Hermes binary overlaps production state")
    for name in ("git", "node", "npm", "shasum"):
        if shutil.which(name) is None:
            failures.append(f"required command is missing: {name}")

    sha = tree = contract = hermes_version = ""
    if factory.is_dir():
        try:
            dirty = command("git", "status", "--porcelain", "--untracked-files=all", cwd=factory)
            head = command("git", "rev-parse", "HEAD", cwd=factory)
            sha = args.sha or head
            tree = command("git", "rev-parse", f"{sha}^{{tree}}", cwd=factory)
            if dirty:
                failures.append("Factory candidate must be clean")
            if not SHA.fullmatch(sha) or sha != head:
                failures.append("Factory SHA must be the clean checkout HEAD")
            if not SHA.fullmatch(tree):
                failures.append("Factory tree is invalid")
            contract_data = json.loads(
                (factory / "integrations/hermes/contract.json").read_text(encoding="utf-8")
            )
            contract = contract_data.get("contract_version", "")
            if contract not in {f"1.{minor}.0" for minor in range(5, 9)}:
                failures.append("candidate Hermes contract is unsupported by the canary")
            if 2 not in contract_data.get("concurrency", {}).get("enabled_values", []):
                failures.append("candidate contract does not permit canary ticket capacity 2")
            if hermes.is_file() and os.access(hermes, os.X_OK):
                try:
                    version = command(str(hermes), "--version")
                    hermes_version = version.splitlines()[0] if version else ""
                    supported = contract_data.get("supported_hermes", [])
                    if not any(
                        item.get("agent_version", "") in version
                        and item.get("build_version", "") in version
                        for item in supported
                    ):
                        failures.append(
                            "Hermes binary version is not certified by the candidate contract"
                        )
                except Refusal as error:
                    failures.append(f"Hermes version check failed: {error}")
            required = (
                "scripts/factory-kit.sh",
                "integrations/hermes/bin/factory-launch",
                "integrations/hermes/templates/profile/SOUL.md",
                "integrations/hermes/templates/profile/skills/factory-dispatch/SKILL.md",
                "conformance/app/package.json",
            )
            for relative in required:
                if not (factory / relative).is_file():
                    failures.append(f"candidate is missing {relative}")
        except Refusal as error:
            failures.append(str(error))
        except (OSError, ValueError) as error:
            failures.append(f"candidate contract is unreadable: {error}")

    project = args.project or (f"factory-canary-{sha[:12]}" if sha else "")
    if not PROJECT.fullmatch(project):
        failures.append("project slug is invalid")
    if raw_root.exists():
        info = raw_root.lstat()
        if (
            raw_root.is_symlink()
            or not raw_root.is_dir()
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700
        ):
            failures.append("existing canary root is unsafe")
    if args.action == "run" and (
        sys.platform != "darwin" or not Path("/bin/launchctl").is_file()
    ):
        failures.append("real-Hermes execution requires macOS launchctl")
    if failures:
        raise Refusal("; ".join(failures))
    return factory, root, hermes, sha, tree, contract, hermes_version, project


def marker_value(
    root: Path, factory: Path, hermes: Path, sha: str, tree: str,
    contract: str, hermes_version: str, project: str,
) -> dict:
    return {
        "factory_sha": sha,
        "factory_tree": tree,
        "contract_version": contract,
        "hermes_bin": str(hermes),
        "hermes_sha256": digest(hermes),
        "hermes_version": hermes_version,
        "linear": "disabled",
        "project": project,
        "root": str(root),
        "schema": SCHEMA,
        "source_factory": str(factory),
    }


def validate_marker(path: Path, expected: dict) -> None:
    try:
        info = path.lstat()
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise Refusal("canary identity marker is missing or invalid") from error
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o600
        or value != expected
    ):
        raise Refusal("canary identity drifted")


def attempt_value(root: Path, expected: dict) -> dict:
    return {
        "factory_sha": expected["factory_sha"],
        "factory_tree": expected["factory_tree"],
        "identity_sha256": digest(root / "marker.json"),
        "project": expected["project"],
        "root": expected["root"],
        "schema": ATTEMPT_SCHEMA,
    }


def validate_attempt(root: Path, expected: dict) -> bool:
    path = root / "attempt.json"
    if not path.exists() and not path.is_symlink():
        return False
    try:
        info = path.lstat()
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise Refusal("canary attempt marker is invalid") from error
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o600
        or value != attempt_value(root, expected)
    ):
        raise Refusal("canary attempt marker does not match the exact candidate")
    return True


def retire_failed_attempt(root: Path) -> None:
    evidence = root / "evidence"
    complete = evidence / "hook-complete"
    if complete.exists():
        return
    start = evidence / "hook-start"
    if start.exists() or start.is_symlink():
        if start.is_symlink() or not start.is_file():
            raise Refusal("prior canary start marker is unsafe")
        start.unlink()
    failure = evidence / "failure"
    if not failure.exists() and not failure.is_symlink():
        return
    if failure.is_symlink() or not failure.is_file():
        raise Refusal("prior canary failure marker is unsafe")
    archive = evidence / "prior-failures"
    archive.mkdir(mode=0o700, exist_ok=True)
    info = archive.lstat()
    if archive.is_symlink() or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise Refusal("prior canary failure archive is unsafe")
    destination = archive / f"{digest(failure)}.txt"
    if destination.exists():
        if destination.is_symlink() or digest(destination) != digest(failure):
            raise Refusal("prior canary failure archive is unsafe")
        failure.unlink()
    else:
        failure.replace(destination)


def validate_completion(root: Path, expected: dict) -> bool:
    evidence = root / "evidence"
    completion = evidence / "hook-complete"
    if not completion.exists() and not completion.is_symlink():
        return False
    if not validate_attempt(root, expected):
        raise Refusal("canary completion evidence has no exact attempt marker")
    try:
        info = completion.lstat()
        value = json.loads(completion.read_text(encoding="utf-8"))
        files = value["evidence"]
        load = lambda name: json.loads((evidence / name).read_text(encoding="utf-8"))
        contract, doctor = load("contract.json"), load("doctor.json")
        manifest, certification = load("manifest-summary.json"), load(
            "certification-summary.json"
        )
        release = load("release.json")
    except (OSError, KeyError, TypeError, ValueError) as error:
        raise Refusal("canary completion evidence is invalid") from error
    identity = {
        "attempt_sha256": digest(root / "attempt.json"),
        "contract_version": expected["contract_version"],
        "factory_sha": expected["factory_sha"],
        "factory_tree": expected["factory_tree"],
        "profile": expected["project"],
        "schema": COMPLETION_SCHEMA,
    }
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o600
        or not isinstance(files, dict)
        or {key: value.get(key) for key in identity} != identity
    ):
        raise Refusal("canary completion evidence does not match the exact attempt")
    required = {
        "contract.json", "doctor.json", "hermes-hook-payload.sha256",
        "hook-start", "lease.sha256", "manifest-summary.json", "model-pin.json",
        "preflight.json", "release.json",
        "transition.json" if expected["contract_version"] == "1.8.0" else "next-stage.json",
    }
    if set(files) != required:
        raise Refusal("canary completion evidence inventory is incomplete")
    for name, expected_digest in files.items():
        path = evidence / name
        if not path.is_file() or path.is_symlink() or digest(path) != expected_digest:
            raise Refusal(f"canary completion evidence drifted: {name}")
    hex40, hex64 = re.compile(r"[0-9a-f]{40}"), re.compile(r"[0-9a-f]{64}")
    if (
        contract.get("contract_version") != expected["contract_version"]
        or doctor.get("checks", {}).get("kit", {}).get("full_sha")
        != expected["factory_sha"]
        or doctor.get("checks", {}).get("kit_pin", {}).get("matches_kit") is not True
        or doctor.get("overall_status") == "error"
    ):
        raise Refusal("canary contract or Doctor evidence does not match the candidate")
    receipt = manifest.get("transition_receipt_sha256", "")
    if (
        manifest.get("adapter") != "mock"
        or manifest.get("role") != "planner"
        or manifest.get("ticket") != "T-900001"
        or manifest.get("kit_sha") != expected["factory_sha"]
        or manifest.get("kit_tree") != expected["factory_tree"]
        or manifest.get("contract_version") != expected["contract_version"]
        or not hex40.fullmatch(manifest.get("product_tree", ""))
        or (
            expected["contract_version"] == "1.8.0"
            and not hex64.fullmatch(receipt)
        )
        or (expected["contract_version"] != "1.8.0" and receipt)
    ):
        raise Refusal("canary mock Planner manifest evidence is invalid")
    if expected["contract_version"] == "1.8.0":
        transition = json.loads(
            (evidence / "transition.json").read_text(encoding="utf-8")
        )
        if transition.get("receipt") != receipt:
            raise Refusal("canary transition receipt evidence does not match the mock run")
    if (
        release != {"expired": False, "released": True, "ticket": "T-900001"}
        or certification.get("status") != "pass"
        or certification.get("project") != expected["project"]
        or certification.get("kit_sha") != expected["factory_sha"]
        or certification.get("kit_tree") != expected["factory_tree"]
        or certification.get("contract_version") != expected["contract_version"]
        or certification.get("product_path") != str(root / "product")
        or certification.get("product_tree") != manifest.get("product_tree")
        or not hex40.fullmatch(certification.get("product_sha", ""))
        or not hex40.fullmatch(certification.get("product_tree", ""))
        or not hex64.fullmatch(certification.get("receipt_id", ""))
    ):
        raise Refusal("canary certification or lease-release evidence is invalid")
    try:
        version = (evidence / "hermes-version.txt").read_text(
            encoding="utf-8"
        ).splitlines()
        timings = (root / "timings.csv").read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise Refusal("canary completion timing or Hermes evidence is missing") from error
    if not version or version[0] != expected["hermes_version"]:
        raise Refusal("canary Hermes version evidence does not match the candidate")
    if not any(re.fullmatch(r"hermes-hook,[0-9]+", line) for line in timings):
        raise Refusal("canary Hermes hook timing evidence is missing")
    return True


def render_launcher(root: Path, project: str, sha: str) -> str:
    release = root / f"home/.factory/kits/releases/{sha}/integrations/hermes/bin/factory-launch"
    return f"""#!/bin/bash
set -eu
exec /usr/bin/env -i HOME={root}/home TMPDIR={root}/tmp \\
  PATH={root}/home/.factory/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin \\
  FACTORY_LAUNCH_TEST_MODE=1 FACTORY_LAUNCH_TEST_HOME={root}/home \\
  FACTORY_LAUNCH_TEST_ACCOUNT_HOME={root}/home \\
  FACTORY_KITS_ROOT={root}/home/.factory/kits \\
  HERMES_FACTORY_PROFILE={root}/home/.hermes/profiles/{project} \\
  {release} \"$@\"
"""


def render_hook(root: Path, project: str, sha: str, tree: str, contract: str) -> str:
    release = root / f"home/.factory/kits/releases/{sha}"
    worktree = root / "worktrees/T-900001"
    evidence = root / "evidence"
    if contract == "1.8.0":
        sequence = f"""run_launcher state-machine --ticket \"$ticket\" --lease \"$lease\" \\
  --workdir \"$worktree\" --json > \"$evidence/transition.json\"
role=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["role"])' \"$evidence/transition.json\")
receipt=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["receipt"])' \"$evidence/transition.json\")
stage=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["stage"])' \"$evidence/transition.json\")
[[ \"$role\" == planner && \"$stage\" == \"RUN planner\" ]]
run_launcher preflight --ticket \"$ticket\" --role \"$role\" --lease \"$lease\" \\
  --receipt \"$receipt\" --workdir \"$worktree\" --json > \"$evidence/preflight.json\"
run_launcher run --role \"$role\" --ticket \"$ticket\" --lease \"$lease\" \\
  --receipt \"$receipt\" --prompt-file \"{release}/roles/$role.md\" \\
  --workdir \"$worktree\" -- \"Return the isolated Hermes canary marker only.\" \\
  > \"$evidence/mock-role.out\" 2> \"$evidence/mock-role.err\""""
    else:
        sequence = f"""run_launcher next-stage --ticket \"$ticket\" --lease \"$lease\" \\
  --workdir \"$worktree\" --json > \"$evidence/next-stage.json\"
role=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["detail"])' \"$evidence/next-stage.json\")
[[ \"$role\" == planner ]]
run_launcher preflight --ticket \"$ticket\" --role \"$role\" --lease \"$lease\" \\
  --workdir \"$worktree\" --json > \"$evidence/preflight.json\"
run_launcher run --role \"$role\" --ticket \"$ticket\" --lease \"$lease\" \\
  --prompt-file \"{release}/roles/$role.md\" --workdir \"$worktree\" -- \\
  \"Return the isolated Hermes canary marker only.\" \\
  > \"$evidence/mock-role.out\" 2> \"$evidence/mock-role.err\""""
    return f"""#!/bin/bash
set -Eeuo pipefail
umask 077
ticket=T-900001
worktree={worktree}
evidence={evidence}
lease=""
mkdir -p "$evidence"
rm -f "$evidence/failure" "$evidence/hook-complete"
date +%s > "$evidence/hook-start"
trap 'status=$?; printf "line=%s status=%s\\n" "$LINENO" "$status" > "$evidence/failure"' ERR
cleanup() {{
  [[ -z "$lease" ]] || run_launcher release --ticket "$ticket" --lease "$lease" \\
    > "$evidence/release-cleanup.json" 2>/dev/null || true
}}
trap cleanup EXIT
payload=$(cat)
printf '%s' "$payload" | shasum -a 256 | awk '{{print $1}}' > "$evidence/hermes-hook-payload.sha256"
unset payload
run_launcher() {{ {root}/home/.factory/bin/factory-launch {project} "$@"; }}
run_launcher contract --json > "$evidence/contract.json"
run_launcher doctor --json > "$evidence/doctor.json"
python3 - "$evidence/contract.json" "$evidence/doctor.json" <<'PY'
import json,sys
contract,doctor=(json.load(open(path)) for path in sys.argv[1:])
assert contract["contract_version"] == "{contract}"
assert doctor["checks"]["kit"]["full_sha"] == "{sha}"
assert doctor["checks"]["kit_pin"]["matches_kit"] is True
assert doctor["overall_status"] != "error"
PY
claim=$(run_launcher claim --ticket "$ticket")
lease=$(printf '%s' "$claim" | python3 -c 'import json,sys; print(json.load(sys.stdin)["lease_id"])')
printf '%s' "$lease" | shasum -a 256 | awk '{{print $1}}' > "$evidence/lease.sha256"
unset claim
run_launcher models pin --ticket "$ticket" --workdir "$worktree" --json > "$evidence/model-pin.json"
{sequence}
run_launcher release --ticket "$ticket" --lease "$lease" > "$evidence/release.json"
lease=""
latest=$(ls -t {root}/product/factory/runs/*.meta | head -1)
python3 - "$latest" "$evidence/manifest-summary.json" <<'PY'
import json,pathlib,sys
source,destination=map(pathlib.Path,sys.argv[1:])
allowed={{"adapter","contract_version","kit_sha","kit_tree","phase","physical_kit_path","product_tree","role","ticket","ticket_kit_sha","transition_receipt_sha256"}}
values={{}}
for line in source.read_text().splitlines():
    key,separator,value=line.partition("=")
    if separator and key in allowed: values[key]=value
assert values["adapter"] == "mock"
assert values["contract_version"] == "{contract}"
assert values["kit_sha"] == "{sha}"
assert values["kit_tree"] == "{tree}"
assert values["physical_kit_path"] == "{release}"
assert values["ticket"] == "T-900001"
destination.write_text(json.dumps(values,sort_keys=True)+"\\n")
PY
python3 - "$evidence" "{root}/attempt.json" "$evidence/hook-complete" <<'PY'
import hashlib,json,os,pathlib,sys
evidence,attempt,destination=map(pathlib.Path,sys.argv[1:])
names={{"contract.json","doctor.json","hermes-hook-payload.sha256","hook-start","lease.sha256",
       "manifest-summary.json","model-pin.json","preflight.json","release.json",
       "transition.json" if "{contract}" == "1.8.0" else "next-stage.json"}}
digest=lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
value={{
    "attempt_sha256":digest(attempt),
    "contract_version":"{contract}",
    "evidence":{{name:digest(evidence/name) for name in names}},
    "factory_sha":"{sha}",
    "factory_tree":"{tree}",
    "profile":"{project}",
    "schema":"{COMPLETION_SCHEMA}",
}}
destination.write_text(json.dumps(value,sort_keys=True,separators=(",",":"))+"\\n")
os.chmod(destination,0o600)
PY
rm -f "$evidence/failure"
printf '{{}}\n'
"""


def render_driver(root: Path, project: str, sha: str, hermes: Path) -> str:
    kit = root / "factory/scripts/factory-kit.sh"
    product = root / "product"
    state = root / "home/.factory/kits"
    origin = root / "factory-origin.git"
    launcher = root / f"home/.factory/kits/releases/{sha}/integrations/hermes/bin/factory-launch"
    return f"""#!/bin/bash
set -euo pipefail
export HOME={root}/home TMPDIR={root}/tmp
export PATH={root}/home/.factory/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin
export FACTORY_KITS_ROOT={state}
export FACTORY_KIT_TEST_MODE=1
export FACTORY_KIT_TEST_SKIP_PROVIDER_CLI_PIN=1
export FACTORY_KIT_TEST_REMOTE_FULL_CI=1
export FACTORY_KIT_CANONICAL_ORIGIN={origin}
export FACTORY_KIT_TEST_INSTALLED_LAUNCHER={launcher}
mkdir -p {root}/evidence
run_phase() {{
  phase="$1"; shift
  start=$(date +%s)
  "$@"
  printf '%s,%s\n' "$phase" "$(( $(date +%s) - start ))" >> {root}/timings.csv
}}
if [[ ! -f {state}/manifests/{sha}.json ]]; then
  run_phase install bash {kit} install --repo {root}/factory --sha {sha}
fi
if [[ ! -f {root}/provider-ready ]]; then
  plan=$(bash {kit} provider-concurrency plan --sha {sha} --capacity 2)
  approval=$(printf '%s' "$plan" | python3 -c 'import json,sys; print(json.load(sys.stdin)["approval_sha256"])')
  run_phase provider bash {kit} provider-concurrency apply --sha {sha} --capacity 2 --approve-hash "$approval"
  : > {root}/provider-ready
fi
if ! find {state}/receipts -maxdepth 1 -name '*.json' -print -quit 2>/dev/null | grep -q .; then
  run_phase certify bash {kit} certify --project {project} --product {product} --sha {sha}
fi
python3 - {state}/receipts {root}/evidence/certification-summary.json {project} {sha} {product} <<'PY'
import json,pathlib,sys
receipts,destination,project,sha,product=sys.argv[1:]
matches=[]
for path in pathlib.Path(receipts).glob("*.json"):
    value=json.loads(path.read_text())
    if (value.get("status"),value.get("project"),value.get("kit_sha"),value.get("product_path")) == ("pass",project,sha,product):
        matches.append(value)
assert matches
value=max(matches,key=lambda item:item["created_epoch"])
allowed={{"contract_version","created_at","expires_at","kit_sha","kit_tree","product_path","product_sha","product_tree","project","receipt_id","status"}}
pathlib.Path(destination).write_text(json.dumps({{key:value[key] for key in allowed}},sort_keys=True)+"\\n")
PY
if [[ ! -f {state}/projects/{project}/active.json ]]; then
  bash {kit} pause --project {project} --product {product}
  run_phase activate bash {kit} activate --project {project} --product {product} --sha {sha}
fi
rm -f {product}/factory/MAINTENANCE
run_phase contract {root}/home/.factory/bin/factory-launch {project} contract --json
run_phase doctor {root}/home/.factory/bin/factory-launch {project} doctor --json
run_phase hermes-version {hermes} --version > {root}/evidence/hermes-version.txt
if [[ $(uname -s) != Darwin || ! -x /bin/launchctl ]]; then
  echo 'real-Hermes execution requires macOS launchctl' >&2
  exit 2
fi
domain=gui/$(id -u)
label=com.nysa.hermes-factory-canary.{sha[:12]}
canary_loaded=0
cleanup_agent() {{
  [[ "$canary_loaded" == 0 ]] || \\
    /bin/launchctl bootout "$domain/$label" >/dev/null 2>&1 || true
}}
trap cleanup_agent EXIT
if [[ "${{FACTORY_CANARY_COMPLETION_VALID:-0}}" != 1 ]]; then
  if /bin/launchctl print "$domain/$label" >/dev/null 2>&1; then
    canary_loaded=1
  else
    run_phase hermes-start /bin/launchctl bootstrap "$domain" {root}/hermes-launch-agent.plist
    canary_loaded=1
  fi
  for ignored in $(seq 1 30); do
    [[ -f {root}/evidence/hook-start || -f {root}/evidence/hook-complete ]] && break
    if [[ -f {root}/evidence/failure ]]; then
      printf 'HERMES_BLOCKER=%s\n' "$(cat {root}/evidence/failure)" >&2
      exit 1
    fi
    sleep 1
  done
  [[ -f {root}/evidence/hook-start || -f {root}/evidence/hook-complete ]] || \
    {{ echo 'HERMES_BLOCKER=startup-timeout' >&2; exit 1; }}
  for ignored in $(seq 1 240); do
    [[ ! -f {root}/evidence/hook-complete ]] || break
    if [[ -f {root}/evidence/failure ]]; then
      printf 'HERMES_BLOCKER=%s\n' "$(cat {root}/evidence/failure)" >&2
      exit 1
    fi
    sleep 1
  done
  [[ -f {root}/evidence/hook-complete ]] || {{ echo 'HERMES_BLOCKER=hook-timeout' >&2; exit 1; }}
  hook_started=$(cat {root}/evidence/hook-start)
  [[ "$hook_started" =~ ^[0-9]+$ ]] || {{ echo 'HERMES_BLOCKER=hook-timing' >&2; exit 1; }}
  printf 'hermes-hook,%s\n' "$(( $(date +%s) - hook_started ))" >> {root}/timings.csv
  /bin/launchctl bootout "$domain/$label" >/dev/null 2>&1 || true
  canary_loaded=0
fi
printf 'READY=%s\n' {root}
"""


def scaffold(
    factory: Path, root: Path, hermes: Path, sha: str, tree: str,
    contract: str, hermes_version: str, project: str,
) -> None:
    expected = marker_value(
        root, factory, hermes, sha, tree, contract, hermes_version, project
    )
    if root.exists():
        validate_marker(root / "marker.json", expected)
        return
    root.mkdir(mode=0o700)
    write(root / "marker.json", canonical(expected))
    for directory in (root / "home", root / "tmp"):
        directory.mkdir(mode=0o700)

    command("git", "clone", "--bare", "--no-local", str(factory), str(root / "factory-origin.git"))
    command("git", "update-ref", "refs/heads/main", sha, cwd=root / "factory-origin.git")
    command("git", "symbolic-ref", "HEAD", "refs/heads/main", cwd=root / "factory-origin.git")
    command("git", "clone", "--no-local", str(root / "factory-origin.git"), str(root / "factory"))
    command("git", "checkout", "--detach", sha, cwd=root / "factory")

    product = root / "product"
    shutil.copytree(root / "factory/conformance/app", product / "app")
    (product / "factory/tickets").mkdir(parents=True)
    (product / "factory/initiatives").mkdir()
    (product / "docs/acceptance").mkdir(parents=True)
    write(product / "factory/KIT_PIN", sha + "\n", 0o644)
    write(product / "factory/ENVELOPE.env", """PER_RUN_BUDGET_USD=2.00
PER_TICKET_BUDGET_USD=10.00
PER_RUN_MAX_TURNS=4
PER_RUN_TIMEOUT_MIN=5
DAILY_CAP_USD=20.00
""", 0o644)
    write(product / "factory/PROJECT.env", f"""PROJECT_NAME={project}
GH_REPO=local/{project}
DONE_REQUIRED_CHECKS=ci
AUTO_MERGE_METHOD=squash
CERTIFY_SCRIPT=factory/certify.sh
PREVIEW_PROVIDER=none
NONVISUAL_PATHS=app/
MAX_CONCURRENT_TICKETS=2
TEST_PATHS=app/tests/
TICKET_BRANCH_PREFIX=ticket/
WORKTREES_DIR={root}/worktrees
""", 0o644)
    write(product / "factory/certify.sh", """#!/bin/bash
set -eu
npm --prefix app test
""", 0o755)
    write(product / "factory/linear-state.json", canonical({
        "enabled": False,
        "reason": "credential-free-canary",
        "schema": "nysa.software-factory.canary-linear/v1",
    }), 0o644)
    write(product / "factory/initiatives/I-900001.md", """# I-900001 — Isolated canary

This local-only initiative exists solely to exercise the candidate release.
""", 0o644)
    write(product / "factory/tickets/T-900001.md", f"""# T-900001 — Canary planner

State: Ready
Priority: low
Risk class: low
External: no
Kit-SHA: {sha}
Product-Decisions: frozen
Depends-On: none
Initiative: I-900001
Builder ownership: app/server.js only
Fixture-Seams: app/tests/conformance.test.js
Authentication-Seams: none
Protected-Test-Conflicts: none

## Description

Plan one no-op compatibility check for the isolated conformance product.

## Acceptance criteria

1. The mock Planner completes through the exact candidate launcher.
""", 0o644)
    write(product / ".gitignore", """factory/MAINTENANCE
factory/linear-map.json
factory/runs/
factory/.active-runs/
factory/.dispatch-leases/
factory/.dispatch-leases.lock/
factory/.launch.lock/
factory/.provider.lock/
factory/runtime-ledger.csv
app/data/
""", 0o644)
    command("git", "init", "-b", "main", cwd=product)
    command("git", "config", "user.name", "Factory Canary", cwd=product)
    command("git", "config", "user.email", "factory-canary@local", cwd=product)
    command("git", "add", ".", cwd=product)
    command("git", "commit", "-m", "Create isolated Factory canary", cwd=product)
    command("git", "init", "--bare", str(root / "product-origin.git"))
    command("git", "remote", "add", "origin", str(root / "product-origin.git"), cwd=product)
    command("git", "push", "-u", "origin", "main", cwd=product)
    write(product / "factory/linear-map.json", canonical({
        "_sync": {"last_success_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
        "tickets": {},
    }), 0o600)
    worktree = root / "worktrees/T-900001"
    command("git", "worktree", "add", "-b", "ticket/T-900001", str(worktree), "main", cwd=product)
    command("git", "push", "-u", "origin", "ticket/T-900001", cwd=worktree)

    profile = root / f"home/.hermes/profiles/{project}"
    shutil.copytree(root / "factory/integrations/hermes/templates/profile", profile)
    write(profile / f"projects/{project}.env", f"PRODUCT_ROOT={product}\n", 0o600)
    hook = profile / "hooks/run-factory-canary.sh"
    write(hook, render_hook(root, project, sha, tree, contract), 0o700)
    write(profile / "config.yaml", f"""hooks:
  on_session_start:
    - command: {hook}
      timeout: 240
hooks_auto_accept: true
""", 0o600)
    write(profile / "profile.yaml", """name: Factory release canary
description: Credential-free isolated real-Hermes release compatibility check
""", 0o600)
    (root / "home/.factory/bin").mkdir(parents=True)
    write(root / "home/.factory/bin/factory-launch", render_launcher(root, project, sha), 0o700)
    write(root / "run.sh", render_driver(root, project, sha, hermes), 0o700)

    label = f"com.nysa.hermes-factory-canary.{sha[:12]}"
    payload = {
        "Label": label,
        "ProgramArguments": [str(hermes), "--profile", project, "gateway", "run"],
        "WorkingDirectory": str(root / "home"),
        "EnvironmentVariables": {
            "HOME": str(root / "home"),
            "PATH": f"{root}/home/.factory/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        },
        "RunAtLoad": True,
        "KeepAlive": False,
        "ProcessType": "Interactive",
        "StandardOutPath": str(root / "hermes.log"),
        "StandardErrorPath": str(root / "hermes.err.log"),
    }
    write(root / "hermes-launch-agent.plist", plistlib.dumps(payload), 0o600)

    tracked = {
        str(path.relative_to(root)): digest(path)
        for path in (
            product / "factory/PROJECT.env",
            product / "factory/linear-state.json",
            product / "factory/tickets/T-900001.md",
            profile / f"projects/{project}.env",
            profile / "SOUL.md",
            profile / "skills/factory-dispatch/SKILL.md",
            profile / "config.yaml",
            profile / "profile.yaml",
            hook,
            root / "home/.factory/bin/factory-launch",
            root / "hermes-launch-agent.plist",
            root / "run.sh",
        )
    }
    write(root / "descriptor-digests.json", canonical(tracked))


def check(root: Path, expected: dict, resume: bool = False) -> dict:
    validate_marker(root / "marker.json", expected)
    failures: list[str] = []
    try:
        tracked = json.loads((root / "descriptor-digests.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        tracked = {}
        failures.append("descriptor digest inventory is missing")
    if not isinstance(tracked, dict):
        tracked = {}
        failures.append("descriptor digest inventory is malformed")
    project = expected["project"]
    required_tracked = {
        "product/factory/PROJECT.env",
        "product/factory/linear-state.json",
        "product/factory/tickets/T-900001.md",
        f"home/.hermes/profiles/{project}/projects/{project}.env",
        f"home/.hermes/profiles/{project}/SOUL.md",
        f"home/.hermes/profiles/{project}/skills/factory-dispatch/SKILL.md",
        f"home/.hermes/profiles/{project}/config.yaml",
        f"home/.hermes/profiles/{project}/profile.yaml",
        f"home/.hermes/profiles/{project}/hooks/run-factory-canary.sh",
        "home/.factory/bin/factory-launch",
        "hermes-launch-agent.plist",
        "run.sh",
    }
    if set(tracked) != required_tracked:
        failures.append("descriptor digest inventory does not name the exact canary surface")
    for relative, expected_digest in tracked.items():
        path = root / relative
        if not path.is_file() or path.is_symlink():
            failures.append(f"missing or unsafe: {relative}")
        elif digest(path) != expected_digest:
            failures.append(f"changed: {relative}")
    project_env = root / "product/factory/PROJECT.env"
    text = project_env.read_text(encoding="utf-8") if project_env.is_file() else ""
    required = {
        "PREVIEW_PROVIDER=none",
        "NONVISUAL_PATHS=app/",
        "MAX_CONCURRENT_TICKETS=2",
        "CERTIFY_SCRIPT=factory/certify.sh",
    }
    for line in sorted(required):
        if text.splitlines().count(line) != 1:
            failures.append(f"PROJECT.env requires exactly {line}")
    try:
        linear_state = json.loads(
            (root / "product/factory/linear-state.json").read_text(encoding="utf-8")
        )
        if linear_state != {
            "enabled": False,
            "reason": "credential-free-canary",
            "schema": "nysa.software-factory.canary-linear/v1",
        }:
            failures.append("explicit disabled Linear state is invalid")
    except (OSError, ValueError, json.JSONDecodeError):
        failures.append("explicit disabled Linear state is missing")
    try:
        if command("git", "remote", "get-url", "origin", cwd=root / "product") != str(
            root / "product-origin.git"
        ):
            failures.append("product canonical origin is not the isolated local origin")
        command(
            "git", "cat-file", "-e", f'{expected["factory_sha"]}^{{commit}}',
            cwd=root / "factory-origin.git",
        )
        local_tree = command(
            "git", "rev-parse", f'{expected["factory_sha"]}^{{tree}}',
            cwd=root / "factory-origin.git",
        )
        if local_tree != expected["factory_tree"]:
            failures.append("candidate local origin does not contain the exact Factory tree")
        for repository in (root / "product", root / "worktrees/T-900001"):
            if command("git", "status", "--porcelain", cwd=repository):
                failures.append(f"isolated product checkout is dirty: {repository.name}")
        local_ticket = command("git", "rev-parse", "HEAD", cwd=root / "worktrees/T-900001")
        remote_ticket = command(
            "git", "rev-parse", "refs/heads/ticket/T-900001",
            cwd=root / "product-origin.git",
        )
        if local_ticket != remote_ticket:
            failures.append("isolated ticket branch does not match its local origin")
    except Refusal as error:
        failures.append(f"isolated local origin is invalid: {error}")
    complete = validate_completion(root, expected)
    if complete and (root / "evidence/failure").exists():
        raise Refusal("canary completion conflicts with retained failure evidence")
    for directory in (root / "product/factory/runs", root / "product/factory/.active-runs"):
        if directory.exists() and not (resume or complete):
            failures.append(f"runtime state exists before canary start: {directory.name}")
    if failures:
        raise Refusal("; ".join(failures))
    return {
        "factory_sha": expected["factory_sha"],
        "factory_tree": expected["factory_tree"],
        "linear": "disabled",
        "project": expected["project"],
        "root": expected["root"],
        "status": "complete" if complete else "ready",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("prepare", "run", "check"))
    parser.add_argument("--factory-root", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--hermes-bin", type=Path, required=True)
    parser.add_argument("--sha", default="")
    parser.add_argument("--project", default="")
    args = parser.parse_args()
    started = time.monotonic()
    factory, root, hermes, sha, tree, contract, hermes_version, project = validate_inputs(args)
    expected = marker_value(
        root, factory, hermes, sha, tree, contract, hermes_version, project
    )
    if args.action in ("prepare", "run"):
        scaffold(factory, root, hermes, sha, tree, contract, hermes_version, project)
    attempted = validate_attempt(root, expected) if args.action == "run" else False
    result = check(root, expected, resume=attempted)
    result["preflight_seconds"] = round(time.monotonic() - started, 3)
    if args.action == "run":
        if not attempted:
            write(root / "attempt.json", canonical(attempt_value(root, expected)))
        retire_failed_attempt(root)
        environment = os.environ.copy()
        environment["FACTORY_CANARY_COMPLETION_VALID"] = (
            "1" if result["status"] == "complete" else "0"
        )
        subprocess.run((str(root / "run.sh"),), check=True, env=environment)
        result["status"] = "activated"
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (Refusal, subprocess.CalledProcessError) as error:
        print(f"real-hermes-canary: {error}", file=sys.stderr)
        raise SystemExit(2)
