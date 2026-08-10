#!/usr/bin/env python3
"""Plan, apply, and verify exact owner-local provider CLI pins."""

from __future__ import annotations

import argparse
import base64
from contextlib import ExitStack, contextmanager
import datetime
import fcntl
import hashlib
from itertools import chain
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any, Iterator
import uuid


PLAN_SCHEMA = "nysa.software-factory.provider-cli-pin-plan/v1"
RECEIPT_SCHEMA = "nysa.software-factory.provider-cli-pin-receipt/v1"
STATUS_SCHEMA = "nysa.software-factory.provider-cli-pin-status/v1"
JOURNAL_SCHEMA = "nysa.software-factory.provider-cli-pin-transaction/v1"
SHA = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
SAFE_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
SAFE_OPERATOR = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
SAFE_PROJECT = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
CONFIG_LINE = re.compile(r"^(?:export )?([A-Z][A-Z0-9_]*)=(.*)$")
SAFE_CONFIG_VALUE = re.compile(r"^[A-Za-z0-9._:/+@%~-]*$")
SENSITIVE = re.compile(
    r"(?i)(?:[A-Za-z][A-Za-z0-9+.-]*://|"
    r"(?:key|token|secret|password|url|dsn|conn|auth)[A-Za-z0-9_.-]*\s*[:=])"
)
MAX_CONFIG = 131_072
MAX_JSON = 1_000_000
SAFE_PATH_SUFFIX = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
TOOLS = {
    "claude": {
        "pin": "CLAUDE_CODE_PINNED", "help": ("--help",),
        "flags": ("--max-budget-usd", "--output-format", "--append-system-prompt", "--model", "--effort"),
    },
    "codex": {
        "pin": "CODEX_PINNED", "help": ("exec", "--help"),
        "flags": ("--json", "--model"),
    },
    "agent": {
        "pin": "CURSOR_AGENT_VERSION", "help": ("--help",),
        "flags": ("--print", "--output-format", "--workspace", "--model", "--force", "--trust"),
    },
}
PIN_KEYS = tuple(value["pin"] for value in TOOLS.values())
REQUIRED_RELEASE_FILES = (
    "integrations/hermes/bin/factory-launch",
    "scripts/lib/plain-config.sh",
    "scripts/lib/backend-policy.sh",
    "scripts/adapters/claude-code.sh",
    "scripts/adapters/codex.sh",
    "scripts/adapters/cursor-agent.sh",
)
CANONICAL_ORIGIN = "github.com/nysa-company/software-factory"


class PinError(ValueError):
    pass


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def object_hash(value: Any) -> str:
    return sha256(canonical(value))


def secure_directory(path: Path, label: str, *, writable: bool = True) -> None:
    try:
        info = path.lstat()
    except OSError as error:
        raise PinError(f"{label} is unavailable") from error
    if (
        not path.is_absolute() or path.is_symlink()
        or path.resolve(strict=True) != path or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o022
        or (writable and stat.S_IMODE(info.st_mode) & 0o200 == 0)
    ):
        raise PinError(f"{label} is unsafe")


def secure_regular(path: Path, label: str, *, maximum: int = MAX_JSON) -> bytes:
    try:
        info = path.lstat()
    except OSError as error:
        raise PinError(f"{label} is unavailable") from error
    if (
        not path.is_absolute() or path.is_symlink() or not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid() or info.st_nlink != 1
        or stat.S_IMODE(info.st_mode) & 0o022 or info.st_size > maximum
    ):
        raise PinError(f"{label} is unsafe")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise PinError(f"{label} changed while opening")
        raw = os.read(descriptor, maximum + 1)
    finally:
        os.close(descriptor)
    if len(raw) > maximum:
        raise PinError(f"{label} is oversized")
    return raw


def read_json(path: Path, label: str) -> tuple[bytes, Any]:
    raw = secure_regular(path, label)
    try:
        return raw, json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise PinError(f"{label} is invalid") from error


def executable(path: Path, label: str) -> tuple[Path, str]:
    if not path.is_absolute():
        raise PinError(f"{label} path must be absolute")
    try:
        resolved = path.resolve(strict=True)
        info = resolved.stat()
    except OSError as error:
        raise PinError(f"{label} is unavailable") from error
    if (
        not stat.S_ISREG(info.st_mode) or info.st_uid not in {0, os.geteuid()}
        or info.st_nlink != 1
        or stat.S_IMODE(info.st_mode) & 0o022 or not os.access(resolved, os.X_OK)
    ):
        raise PinError(f"{label} is unsafe")
    cursor = resolved.parent
    while True:
        parent = cursor.lstat()
        if (
            cursor.is_symlink() or not stat.S_ISDIR(parent.st_mode)
            or parent.st_uid not in {0, os.geteuid()}
            or stat.S_IMODE(parent.st_mode) & 0o022
        ):
            raise PinError(f"{label} parent is unsafe")
        if cursor.parent == cursor:
            break
        cursor = cursor.parent
    file_hash = hashlib.sha256()
    with resolved.open("rb") as handle:
        for chunk in iter(lambda: handle.read(131_072), b""):
            file_hash.update(chunk)
    return resolved, file_hash.hexdigest()


def probe(command: list[str], env: dict[str, str]) -> tuple[bytes, bytes]:
    """Run a provider CLI probe and return its stdout and its whole output.

    Callers parse the stream a tool actually prints on and scan the combined
    output for refusal checks. A provider CLI may legitimately write a warning
    to stderr: codex reports that it will not create PATH aliases because this
    probe deliberately points HOME and TMPDIR at a temporary directory. Parsing
    the merged streams made that benign warning indistinguishable from the
    version line.
    """
    try:
        result = subprocess.run(command, capture_output=True, env=env, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise PinError("provider CLI probe failed") from error
    combined = result.stdout + result.stderr
    if result.returncode or len(combined) > MAX_JSON or b"\0" in combined:
        raise PinError("provider CLI probe failed")
    return result.stdout, combined


def parsed_version(name: str, line: str) -> str:
    if name == "claude":
        version = line.split(maxsplit=1)[0] if line else ""
    elif name == "codex":
        match = re.fullmatch(r"(?:codex|codex-cli) ([A-Za-z0-9][A-Za-z0-9._+-]{0,127})", line)
        version = match.group(1) if match else ""
    else:
        fields = line.split()
        version = fields[-1] if fields else ""
    if not SAFE_VERSION.fullmatch(version):
        raise PinError(f"{name} version probe is invalid")
    return version


def probe_candidate(name: str, source: Path, factory_bin: Path) -> dict[str, str]:
    resolved, binary_hash = executable(source, f"{name} candidate")
    with tempfile.TemporaryDirectory(prefix="provider-cli-pin-probe.") as scratch_raw:
        scratch = Path(scratch_raw)
        scratch.chmod(0o700)
        (scratch / ".claude").mkdir(mode=0o700)
        env = {
            "CLAUDE_CONFIG_DIR": str(scratch / ".claude"), "HOME": str(scratch),
            "PATH": f"{factory_bin}:{SAFE_PATH_SUFFIX}", "TMPDIR": str(scratch),
        }
        version_stdout, version_all = probe([str(resolved), "--version"], env)
        try:
            lines = version_stdout.decode().strip().splitlines()
        except UnicodeError as error:
            raise PinError(f"{name} version probe is invalid") from error
        # The version is parsed from stdout only, but the refusal scan still
        # covers stderr so a credential-bearing warning cannot slip through.
        if (
            len(lines) != 1
            or len(lines[0]) > 4096
            or SENSITIVE.search(version_all.decode(errors="replace"))
        ):
            raise PinError(f"{name} version probe is invalid")
        version = parsed_version(name, lines[0])
        help_stdout, help_all = probe([str(resolved), *TOOLS[name]["help"]], env)
        # The flag contract is searched across the whole output so a tool that
        # documents on stderr still satisfies it, but the recorded digest covers
        # stdout alone. Merged output is not reproducible: codex's stderr
        # warning embeds the randomly named temporary directory this probe
        # creates, so a merged digest changed on every run and no planned
        # approval hash could ever match at apply time.
        help_text = help_all.decode(errors="replace")
        if any(
            re.search(rf"(?<![A-Za-z0-9_-]){re.escape(flag)}(?![A-Za-z0-9_-])", help_text) is None
            for flag in TOOLS[name]["flags"]
        ):
            raise PinError(f"{name} contract probe failed")
    return {
        "executable_sha256": binary_hash, "help_sha256": sha256(help_stdout),
        "link_target": str(resolved), "name": name, "physical_path": str(resolved),
        "pin_key": TOOLS[name]["pin"], "version": version,
    }


def parse_config(raw: bytes) -> dict[str, str]:
    if len(raw) > MAX_CONFIG or any(char in raw for char in (b"\0", b"\r", b"\t")):
        raise PinError("global config is unsafe")
    try:
        lines = raw.decode().splitlines()
    except UnicodeError as error:
        raise PinError("global config is invalid") from error
    values: dict[str, str] = {}
    for line in lines:
        if not line or line.startswith("#"):
            continue
        match = CONFIG_LINE.fullmatch(line)
        if not match:
            raise PinError("global config contains a non-data line")
        key, value = match.groups()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif "\"" in value or "'" in value:
            raise PinError("global config contains malformed quoting")
        if key in values or not SAFE_CONFIG_VALUE.fullmatch(value):
            raise PinError("global config contains invalid or repeated data")
        values[key] = value
    return values


def config_snapshot(
    path: Path, release: Path
) -> tuple[dict[str, Any], bytes | None, dict[str, str]]:
    if not path.exists() and not path.is_symlink():
        return {"sha256": None, "state": "absent"}, None, {}
    raw = secure_regular(path, "global config", maximum=MAX_CONFIG)
    if stat.S_IMODE(path.lstat().st_mode) & 0o077:
        raise PinError("global config is unsafe")
    result = subprocess.run(
        [
            "/bin/bash", "-c",
            'source "$1"; factory_load_plain_config "$2" global "$FACTORY_GLOBAL_CONFIG_KEYS" "" 0',
            "_", str(release / "scripts/lib/plain-config.sh"), str(path),
        ],
        capture_output=True, timeout=20,
        env={"HOME": str(Path.home()), "PATH": SAFE_PATH_SUFFIX},
    )
    if result.returncode:
        raise PinError("global config is invalid")
    return {"sha256": sha256(raw), "state": "present"}, raw, parse_config(raw)


def cursor_setting(values: dict[str, str], home_factory: Path) -> str | None:
    value = values.get("CURSOR_AGENT_BIN")
    if value not in {None, "agent", str(home_factory / "bin/agent")}:
        raise PinError("CURSOR_AGENT_BIN does not name the managed agent link")
    return value


def render_config(raw: bytes | None, desired: dict[str, str]) -> bytes:
    lines = (raw or b"").decode().splitlines(keepends=True)
    seen: set[str] = set()
    output: list[str] = []
    for line in lines:
        content = line[:-1] if line.endswith("\n") else line
        match = CONFIG_LINE.fullmatch(content)
        if match and match.group(1) in desired:
            key = match.group(1)
            output.append(f"export {key}={desired[key]}" + ("\n" if line.endswith("\n") else ""))
            seen.add(key)
        else:
            output.append(line)
    if output and not output[-1].endswith("\n"):
        output[-1] += "\n"
    output.extend(f"export {key}={desired[key]}\n" for key in PIN_KEYS if key not in seen)
    result = "".join(output).encode()
    values = parse_config(result)
    if any(values.get(key) != desired[key] for key in PIN_KEYS):
        raise PinError("global config pin update is invalid")
    return result


def inspect_link(path: Path) -> dict[str, Any]:
    if not path.exists() and not path.is_symlink():
        return {"link_target": None, "physical_path": None, "state": "absent"}
    try:
        info = path.lstat()
        if not path.is_symlink() or info.st_uid != os.geteuid():
            raise PinError("managed provider CLI link is unsafe")
        target = os.readlink(path)
        if not target:
            raise PinError("managed provider CLI link is unsafe")
        candidate = Path(target) if Path(target).is_absolute() else path.parent / target
        try:
            resolved, binary_hash = executable(candidate, "managed provider CLI target")
        except PinError:
            if candidate.exists():
                raise
            return {"link_target": target, "physical_path": None, "state": "dangling"}
        return {
            "executable_sha256": binary_hash, "link_target": target,
            "physical_path": str(resolved), "state": "linked",
        }
    except OSError as error:
        raise PinError("managed provider CLI link is unsafe") from error


def links(factory_bin: Path) -> list[dict[str, Any]]:
    return [
        {"name": name, "path": str(factory_bin / name), **inspect_link(factory_bin / name)}
        for name in TOOLS
    ]


def release_identity(
    kits_root: Path, release: Path, release_sha: str, tree: str, *, candidate: bool = False
) -> dict[str, str]:
    expected = kits_root / "releases" / release_sha
    if (
        not SHA.fullmatch(release_sha) or not SHA.fullmatch(tree)
        or release != expected or release.name != release_sha
    ):
        raise PinError("sealed release identity is invalid")
    secure_directory(release, "sealed release", writable=False)
    manifest_path = kits_root / "manifests" / f"{release_sha}.json"
    _, manifest = read_json(manifest_path, "sealed release manifest")
    if stat.S_IMODE(manifest_path.lstat().st_mode) != 0o600:
        raise PinError("sealed release manifest is unsafe")
    if (
        not isinstance(manifest, dict) or manifest.get("schema_version") != 1
        or manifest.get("kit_sha") != release_sha
        or manifest.get("git_tree") != tree
        or manifest.get("canonical_origin") != CANONICAL_ORIGIN
        or manifest.get("sealed_release_path") != str(release)
    ):
        raise PinError("sealed release manifest is invalid")
    for path in chain((release,), release.rglob("*")):
        info = path.lstat()
        if (
            path.is_symlink() or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) & 0o222
        ):
            raise PinError("sealed release is not read-only")
    _, contract = read_json(release / "integrations/hermes/contract.json", "sealed release contract")
    version = contract.get("contract_version") if isinstance(contract, dict) else None
    if (
        not isinstance(version, str) or not SAFE_VERSION.fullmatch(version)
        or (candidate and version != "1.8.0")
    ):
        raise PinError("sealed release contract is incompatible")
    for relative in REQUIRED_RELEASE_FILES:
        secure_regular(release / relative, "sealed provider CLI contract")
    if candidate:
        for relative in (
            "scripts/lib/provider-cli-version.sh",
            "scripts/owner-provider-cli-pin.py",
        ):
            secure_regular(release / relative, "sealed exact provider CLI contract")
    return {
        "contract_version": version, "factory_sha": release_sha,
        "factory_tree": tree, "release_path": str(release),
    }


def authority_identity(kits_root: Path) -> dict[str, str]:
    helper = Path(__file__).resolve(strict=True)
    release = helper.parent.parent
    if helper != release / "scripts/owner-provider-cli-pin.py":
        raise PinError("provider CLI pin authority helper is not sealed")
    _, manifest = read_json(
        kits_root / "manifests" / f"{release.name}.json", "pin authority manifest"
    )
    tree = manifest.get("git_tree", "") if isinstance(manifest, dict) else ""
    return release_identity(kits_root, release, release.name, tree, candidate=True)


def active_projects(kits_root: Path) -> list[dict[str, Any]]:
    root = kits_root / "projects"
    if not root.exists() and not root.is_symlink():
        return []
    secure_directory(root, "projects root")
    result = []
    for project_root in project_directories(kits_root):
        active = project_root / "active.json"
        if not active.exists() and not active.is_symlink():
            continue
        raw, value = read_json(active, "active project record")
        if not isinstance(value, dict) or value.get("project") != project_root.name:
            raise PinError("active project record is invalid")
        sha = value.get("kit_sha", "")
        tree = value.get("kit_tree", "")
        product_raw = value.get("product_path", "")
        release_raw = value.get("release_path", "")
        if not isinstance(product_raw, str) or not Path(product_raw).is_absolute():
            raise PinError("active project record is invalid")
        product = Path(product_raw).resolve(strict=True)
        release = Path(release_raw).resolve(strict=True) if isinstance(release_raw, str) else Path()
        identity = release_identity(kits_root, release, sha, tree)
        if value.get("contract_version") != identity["contract_version"]:
            raise PinError("active project contract is invalid")
        secure_directory(product, "active product", writable=False)
        result.append({
            "active_sha256": sha256(raw), "product_path": str(product),
            "project": project_root.name, **identity,
        })
    return result


def project_directories(kits_root: Path) -> list[Path]:
    root = kits_root / "projects"
    if not root.exists() and not root.is_symlink():
        return []
    secure_directory(root, "projects root")
    result = []
    for path in sorted(root.iterdir(), key=lambda item: item.name):
        if SAFE_PROJECT.fullmatch(path.name):
            secure_directory(path, "project state")
            result.append(path)
        else:
            secure_regular(path, "projects root artifact")
    return result


def compatible(candidate: dict[str, str], projects: list[dict[str, Any]]) -> list[dict[str, str]]:
    keys = ("contract_version", "factory_sha", "factory_tree", "release_path")
    values = {tuple(candidate[key] for key in keys)}
    values.update(tuple(project[key] for key in keys) for project in projects)
    return [dict(zip(keys, value)) for value in sorted(values)]


def qualification_controllers(home_factory: Path) -> dict[str, list[Path]]:
    base = Path("/private/tmp")
    if (
        os.environ.get("FACTORY_TEST_MODE") == "1"
        and os.environ.get("FACTORY_TRUSTED_TEST_HARNESS") == "1"
        and os.environ.get("FACTORY_PROVIDER_CLI_PIN_TEST_QUALIFICATION_ROOT")
    ):
        base = Path(os.environ["FACTORY_PROVIDER_CLI_PIN_TEST_QUALIFICATION_ROOT"])
        secure_directory(base, "test qualification root")
    result: dict[str, list[Path]] = {}
    for root in sorted(base.glob("nysa-sf-qualification.*"), key=str):
        marker = root / "marker.json"
        environment = root / "environment.json"
        projects = root / "projects"
        if not marker.exists() or not environment.exists() or not projects.exists():
            continue
        secure_directory(root, "qualification root")
        _, value = read_json(marker, "qualification marker")
        if value != {"mode": "qualification", "schema": "nysa.software-factory.qualification-environment/v1"}:
            raise PinError("qualification marker is invalid")
        read_json(environment, "qualification environment")
        controllers = []
        for active in sorted(projects.glob("*/active.json"), key=str):
            _, activation = read_json(active, "qualification active record")
            project = active.parent.name
            if (
                not isinstance(activation, dict)
                or activation.get("project") != project
            ):
                raise PinError("qualification active record is invalid")
            if activation.get("qualification_mode") == "isolated":
                controller = home_factory / "qualification" / project / "controller"
                valid = activation.get("controller_state_path") == str(controller)
            elif activation.get("qualification_mode") == "takeover":
                controller = home_factory / "kits" / "projects" / project / "controller"
                valid = (
                    activation.get("takeover_kits_root") == str(home_factory / "kits")
                    and "controller_state_path" not in activation
                )
            else:
                valid = False
            if not valid:
                raise PinError("qualification active record is invalid")
            secure_directory(controller, "qualification controller")
            controllers.append(controller / "reconcile.lock")
        if controllers:
            result[str(root)] = controllers
    return result


def validate_maintenance(project: dict[str, Any]) -> None:
    product = Path(project["product_path"])
    factory = product / "factory"
    _, marker = read_json(factory / "MAINTENANCE", "maintenance marker")
    if (
        not isinstance(marker, dict) or marker.get("schema_version") != 1
        or marker.get("project") != project["project"]
        or marker.get("product_path") != str(product)
    ):
        raise PinError("machine-wide maintenance is incomplete")
    for name in (".active-runs", "runs", ".dispatch-leases"):
        path = factory / name
        if not path.exists() and not path.is_symlink():
            continue
        secure_directory(path, f"product {name}")
        if any(path.glob("*.pid")) if name == "runs" else any(path.iterdir()):
            raise PinError("controller or provider work is active")
    if (factory / ".provider.lock").exists() or (factory / ".provider.lock").is_symlink():
        raise PinError("controller or provider work is active")


def database(path: Path, application: int, version: int) -> sqlite3.Connection:
    secure_regular(path, "provider database")
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.execute("PRAGMA query_only=ON")
    if (
        connection.execute("PRAGMA application_id").fetchone()[0] != application
        or connection.execute("PRAGMA user_version").fetchone()[0] != version
    ):
        connection.close()
        raise PinError("provider database identity is invalid")
    return connection


def validate_provider_drained(factory: Path) -> None:
    accounting = factory / "accounting"
    if not accounting.exists() and not accounting.is_symlink():
        return
    secure_directory(accounting, "provider accounting state")
    checks = (
        ("state-v2.sqlite3", 0x4E595343, 2,
         ("SELECT count(*) FROM attempts WHERE state IN ('reserved','GO','submitted')", "SELECT count(*) FROM legacy_intervals")),
        ("cursor-account-admission-v1.sqlite3", 0x4E594341, 1,
         ("SELECT count(*) FROM account_leases",)),
    )
    for filename, app, version, queries in checks:
        path = accounting / filename
        if not path.exists() and not path.is_symlink():
            continue
        connection = database(path, app, version)
        try:
            if any(connection.execute(query).fetchone()[0] for query in queries):
                raise PinError("provider work is active")
        finally:
            connection.close()
    broker = accounting / "credential-broker.sqlite3"
    if broker.exists() or broker.is_symlink():
        connection = database(broker, 0x4E595342, 1)
        try:
            query = """SELECT count(*) FROM tokens t
                WHERE revoked_at IS NULL AND expires_at > ? AND
                (used_requests < max_requests OR EXISTS (
                  SELECT 1 FROM requests r WHERE r.token_sha256=t.token_sha256
                  AND r.completed_at IS NULL))"""
            if connection.execute(query, (int(time.time()),)).fetchone()[0]:
                raise PinError("provider broker work is active")
        finally:
            connection.close()


def process_start() -> str:
    result = subprocess.run(
        ["/bin/ps", "-o", "lstart=", "-p", str(os.getpid())],
        capture_output=True, text=True, check=False, timeout=10,
    )
    value = " ".join(result.stdout.split())
    if result.returncode or not value:
        raise PinError("cannot determine provider pin lock identity")
    return value


def claim_directory_lock(path: Path, start: str, label: str) -> str:
    nonce = secrets.token_hex(16)
    try:
        path.mkdir(mode=0o700)
    except FileExistsError as error:
        raise PinError(f"{label} is busy or unsafe") from error
    owner = path / "owner"
    raw = (
        f"pid={os.getpid()}\nprocess_start={start}\nnonce={nonce}\n"
        f"created_epoch={int(time.time())}\n"
    ).encode()
    try:
        descriptor = os.open(
            owner, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        directory = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return nonce
    except Exception:
        owner.unlink(missing_ok=True)
        path.rmdir()
        raise


def release_directory_lock(path: Path, nonce: str, start: str, label: str) -> None:
    expected = (
        f"pid={os.getpid()}\nprocess_start={start}\nnonce={nonce}\n"
    ).encode()
    raw = secure_regular(path / "owner", f"{label} owner")
    if not raw.startswith(expected):
        raise PinError(f"{label} ownership changed")
    quarantine = path.parent / f".{path.name}.release.{nonce}"
    os.replace(path, quarantine)
    (quarantine / "owner").unlink()
    quarantine.rmdir()


@contextmanager
def flock(path: Path, label: str, *, create: bool = True) -> Iterator[None]:
    flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    if create:
        flags |= os.O_CREAT
    descriptor = os.open(path, flags, 0o600)
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise PinError(f"{label} is unsafe")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise PinError(f"{label} is busy") from error
        yield
    finally:
        os.close(descriptor)


@contextmanager
def operation_guard(home_factory: Path, kits_root: Path) -> Iterator[list[dict[str, Any]]]:
    initial_projects = project_directories(kits_root)
    start = process_start()
    with ExitStack() as stack:
        stack.enter_context(flock(home_factory / "provider-configuration.lock", "provider configuration lock"))
        for project in initial_projects:
            lock = project / ".activation.lock"
            nonce = claim_directory_lock(lock, start, "project activation lock")
            stack.callback(release_directory_lock, lock, nonce, start, "project activation lock")
        if project_directories(kits_root) != initial_projects:
            raise PinError("managed project inventory changed")
        initial = active_projects(kits_root)
        controller_locks = {}
        for project in initial_projects:
            controller = project / "controller"
            if controller.exists() or controller.is_symlink():
                secure_directory(controller, "controller state")
                controller_locks[controller / "reconcile.lock"] = "controller reconcile lock"
        qualification = qualification_controllers(home_factory)
        for controllers in qualification.values():
            for lock in controllers:
                controller_locks.setdefault(lock, "qualification controller lock")
        for lock, label in sorted(
            controller_locks.items(), key=lambda item: str(item[0])
        ):
            stack.enter_context(flock(lock, label))
        for product in sorted({item["product_path"] for item in initial}):
            factory = Path(product) / "factory"
            secure_directory(factory, "active product Factory state")
            launch = factory / ".launch.lock"
            nonce = claim_directory_lock(launch, start, "product launch lock")
            stack.callback(release_directory_lock, launch, nonce, start, "product launch lock")
        current = active_projects(kits_root)
        if current != initial:
            raise PinError("active project releases changed")
        process_list = subprocess.run(
            ["/bin/ps", "-axo", "command="], capture_output=True, text=True,
            check=False, timeout=10,
        )
        if process_list.returncode or any(root in process_list.stdout for root in qualification):
            raise PinError("a sealed qualification still consumes shared provider links")
        for project in current:
            validate_maintenance(project)
        validate_provider_drained(home_factory)
        yield current


def atomic_write(path: Path, raw: bytes, mode: int = 0o600) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, mode)
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def atomic_link(path: Path, target: str) -> None:
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}"
    try:
        temporary.symlink_to(target)
        os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def file_snapshot(path: Path, label: str) -> dict[str, Any]:
    if not path.exists() and not path.is_symlink():
        return {"mode": None, "raw": None}
    raw = secure_regular(path, label)
    mode = stat.S_IMODE(path.lstat().st_mode)
    if mode & 0o077:
        raise PinError(f"{label} is unsafe")
    return {"mode": mode, "raw": base64.b64encode(raw).decode()}


def journal_value(home_factory: Path) -> dict[str, Any]:
    unsigned = {
        "config": file_snapshot(home_factory / "global.env", "global config"),
        "links": {name: inspect_link(home_factory / "bin" / name)["link_target"] for name in TOOLS},
        "phase": "prepared", "receipt": file_snapshot(home_factory / "provider-cli-pin.json", "provider CLI pin receipt"),
        "schema": JOURNAL_SCHEMA,
    }
    return {**unsigned, "journal_sha256": object_hash(unsigned)}


def decoded_snapshot(snapshot: dict[str, Any]) -> tuple[bytes | None, int | None]:
    if not isinstance(snapshot, dict) or set(snapshot) != {"mode", "raw"}:
        raise PinError("provider CLI pin transaction is invalid")
    raw = snapshot.get("raw")
    mode = snapshot.get("mode")
    if raw is None and mode is None:
        return None, None
    if not isinstance(raw, str) or not isinstance(mode, int) or mode & 0o077:
        raise PinError("provider CLI pin transaction is invalid")
    try:
        decoded = base64.b64decode(raw, validate=True)
    except ValueError as error:
        raise PinError("provider CLI pin transaction is invalid") from error
    return decoded, mode


def restore_file(path: Path, snapshot: dict[str, Any]) -> None:
    decoded, mode = decoded_snapshot(snapshot)
    if decoded is None:
        path.unlink(missing_ok=True)
        return
    atomic_write(path, decoded, mode)


def read_journal(home_factory: Path) -> dict[str, Any] | None:
    path = home_factory / "provider-cli-pin.transaction.json"
    if not path.exists() and not path.is_symlink():
        return None
    raw = secure_regular(path, "provider CLI pin transaction")
    if stat.S_IMODE(path.lstat().st_mode) != 0o600:
        raise PinError("provider CLI pin transaction is unsafe")
    try:
        value = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise PinError("provider CLI pin transaction is invalid") from error
    unsigned = dict(value) if isinstance(value, dict) else {}
    supplied = unsigned.pop("journal_sha256", "")
    if (
        set(unsigned) != {"config", "links", "phase", "receipt", "schema"}
        or unsigned.get("schema") != JOURNAL_SCHEMA
        or unsigned.get("phase") not in {"prepared", "committed"}
        or supplied != object_hash(unsigned)
    ):
        raise PinError("provider CLI pin transaction is invalid")
    decoded_snapshot(unsigned["config"])
    decoded_snapshot(unsigned["receipt"])
    links_value = unsigned["links"]
    if (
        not isinstance(links_value, dict) or set(links_value) != set(TOOLS)
        or any(target is not None and (not isinstance(target, str) or not target)
               for target in links_value.values())
    ):
        raise PinError("provider CLI pin transaction is invalid")
    return unsigned


def recover(home_factory: Path) -> None:
    path = home_factory / "provider-cli-pin.transaction.json"
    unsigned = read_journal(home_factory)
    if unsigned is None:
        return
    if unsigned.get("phase") != "committed":
        restore_file(home_factory / "global.env", unsigned["config"])
        for name, target in unsigned["links"].items():
            link = home_factory / "bin" / name
            if target is None:
                link.unlink(missing_ok=True)
            elif isinstance(target, str) and target:
                atomic_link(link, target)
            else:
                raise PinError("provider CLI pin transaction is invalid")
        restore_file(home_factory / "provider-cli-pin.json", unsigned["receipt"])
    path.unlink()


def receipt(path: Path) -> dict[str, Any] | None:
    if not path.exists() and not path.is_symlink():
        return None
    raw = secure_regular(path, "provider CLI pin receipt")
    if stat.S_IMODE(path.lstat().st_mode) != 0o600:
        raise PinError("provider CLI pin receipt is unsafe")
    try:
        value = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise PinError("provider CLI pin receipt is invalid") from error
    unsigned = dict(value) if isinstance(value, dict) else {}
    supplied = unsigned.pop("receipt_sha256", "")
    if unsigned.get("schema") != RECEIPT_SCHEMA or supplied != object_hash(unsigned):
        raise PinError("provider CLI pin receipt is invalid")
    return value


def build_plan(
    home_factory: Path, kits_root: Path, candidate_release: dict[str, str],
    candidates: dict[str, Path], operator: str, projects: list[dict[str, Any]],
) -> dict[str, Any]:
    if not SAFE_OPERATOR.fullmatch(operator) or operator == "auto":
        raise PinError("operator ID is invalid")
    release = Path(candidate_release["release_path"])
    config, raw, values = config_snapshot(home_factory / "global.env", release)
    cursor = cursor_setting(values, home_factory)
    probed = [probe_candidate(name, candidates[name], home_factory / "bin") for name in TOOLS]
    if len({item["physical_path"] for item in probed}) != len(TOOLS):
        raise PinError("provider CLI candidates must be distinct")
    desired = {item["pin_key"]: item["version"] for item in probed}
    after = render_config(raw, desired)
    prior = home_factory / "provider-cli-pin.json"
    value = {
        "candidate_release": candidate_release, "candidates": probed,
        "compatible_releases": compatible(candidate_release, projects),
        "current_links": links(home_factory / "bin"),
        "drain_snapshot": projects,
        "global_config": {**config, "cursor_agent_bin": cursor, "desired_sha256": sha256(after)},
        "operator_id": operator,
        "prior_receipt_sha256": sha256(secure_regular(prior, "provider CLI pin receipt")) if prior.exists() or prior.is_symlink() else None,
        "schema": PLAN_SCHEMA, "status": "planned",
    }
    return {**value, "approval_sha256": object_hash(value)}


def check_status(
    home_factory: Path, kits_root: Path, requested_release: dict[str, str],
    authority_release: dict[str, str], *, allow_transaction: bool = False,
) -> dict[str, Any]:
    try:
        pending = read_journal(home_factory)
        release = Path(requested_release["release_path"])
        config, _, values = config_snapshot(home_factory / "global.env", release)
        cursor = cursor_setting(values, home_factory)
        projects = active_projects(kits_root)
        evidence = receipt(home_factory / "provider-cli-pin.json")
        global_reason = "transaction_recovery_required" if pending and not allow_transaction else ""
    except (OSError, PinError, sqlite3.Error):
        config, values, projects, evidence, cursor = {"sha256": None}, {}, [], None, None
        global_reason = "owner_pin_state_invalid"
    expected = {item.get("name"): item for item in (evidence or {}).get("candidates", []) if isinstance(item, dict)}
    allowed = (evidence or {}).get("compatible_releases", [])
    active_identities = [
        {key: item[key] for key in ("contract_version", "factory_sha", "factory_tree", "release_path")}
        for item in projects
    ]
    if evidence is not None and not global_reason:
        if evidence.get("candidate_release") != authority_release:
            global_reason = "authority_release_mismatch"
        elif evidence.get("global_config_sha256") != config.get("sha256") or evidence.get("cursor_agent_bin") != cursor:
            global_reason = "global_config_drift"
        elif requested_release not in allowed:
            global_reason = "requested_release_not_approved"
        elif any(item not in allowed for item in active_identities):
            global_reason = "active_release_not_approved"
    items = []
    for name in TOOLS:
        expected_pin = values.get(TOOLS[name]["pin"])
        item = {
            "expected_version": expected_pin, "managed_state": "unsafe", "name": name,
            "reason": "managed_pin_unsafe", "status": "error", "target": None, "version": None,
        }
        try:
            link = inspect_link(home_factory / "bin" / name)
            item["managed_state"] = link["state"]
            item["target"] = link["link_target"]
            managed = evidence is not None or expected_pin is not None
            if link["state"] == "absent":
                item.update(reason="managed_pin_absent" if managed else "provider_cli_unmanaged", status="error" if managed else "warning")
            elif link["state"] == "dangling":
                item["reason"] = "managed_pin_target_missing"
            elif expected_pin is None:
                item["reason"] = "exact_pin_missing"
            else:
                observed = probe_candidate(name, Path(link["physical_path"]), home_factory / "bin")
                item["version"] = observed["version"]
                wanted = expected.get(name)
                if observed["version"] != expected_pin:
                    item["reason"] = "version_mismatch"
                elif evidence is None:
                    item["reason"] = "receipt_missing"
                elif wanted != observed or link["link_target"] != wanted.get("link_target"):
                    item["reason"] = "receipt_drift"
                else:
                    item.update(reason="exact_pin_ready", status="ok")
        except (OSError, PinError, subprocess.SubprocessError):
            item["reason"] = "contract_probe_failed" if item["managed_state"] == "linked" else "managed_pin_unsafe"
        if global_reason:
            item.update(reason=global_reason, status="error")
        items.append(item)
    ready = evidence is not None and not global_reason and all(item["status"] == "ok" for item in items)
    return {
        "active_releases": active_identities, "global_config_sha256": config.get("sha256"),
        "items": items, "receipt_sha256": (evidence or {}).get("receipt_sha256"),
        "requested_release": requested_release, "schema": STATUS_SCHEMA,
        "status": "ready" if ready else "unready",
    }


def fail_after(point: str) -> None:
    trusted_test = (
        os.environ.get("FACTORY_TEST_MODE") == "1"
        and os.environ.get("FACTORY_TRUSTED_TEST_HARNESS") == "1"
    )
    if trusted_test and os.environ.get("FACTORY_PROVIDER_CLI_PIN_TEST_EXIT_AFTER") == point:
        os._exit(91)
    if (
        trusted_test
        and os.environ.get("FACTORY_PROVIDER_CLI_PIN_TEST_FAIL_AFTER") == point
    ):
        raise PinError("injected provider CLI pin failure")


def apply_plan(
    home_factory: Path, kits_root: Path, candidate_release: dict[str, str],
    candidates: dict[str, Path], operator: str, approval: str,
    projects: list[dict[str, Any]],
) -> dict[str, Any]:
    planned = build_plan(home_factory, kits_root, candidate_release, candidates, operator, projects)
    if not SHA256.fullmatch(approval) or approval != planned["approval_sha256"]:
        raise PinError("provider CLI pin approval hash does not match")
    release = Path(candidate_release["release_path"])
    _, config_raw, _ = config_snapshot(home_factory / "global.env", release)
    desired = {item["pin_key"]: item["version"] for item in planned["candidates"]}
    after = render_config(config_raw, desired)
    journal_path = home_factory / "provider-cli-pin.transaction.json"
    atomic_write(journal_path, canonical(journal_value(home_factory)) + b"\n")
    try:
        atomic_write(home_factory / "global.env", after)
        fail_after("global-config")
        for item in planned["candidates"]:
            atomic_link(home_factory / "bin" / item["name"], item["link_target"])
            fail_after(item["name"])
        unsigned = {
            "applied_at": datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "approval_sha256": approval, "candidate_release": candidate_release,
            "candidates": planned["candidates"],
            "compatible_releases": planned["compatible_releases"],
            "cursor_agent_bin": planned["global_config"]["cursor_agent_bin"],
            "global_config_sha256": sha256(after), "operator_id": operator,
            "schema": RECEIPT_SCHEMA, "status": "applied",
        }
        evidence = {**unsigned, "receipt_sha256": object_hash(unsigned)}
        atomic_write(home_factory / "provider-cli-pin.json", canonical(evidence) + b"\n")
        fail_after("receipt")
        status = check_status(
            home_factory, kits_root, candidate_release, candidate_release,
            allow_transaction=True,
        )
        if status["status"] != "ready":
            raise PinError("provider CLI pin verification failed")
        committed = journal_value(home_factory)
        committed["phase"] = "committed"
        unsigned_journal = dict(committed)
        unsigned_journal.pop("journal_sha256")
        committed["journal_sha256"] = object_hash(unsigned_journal)
        atomic_write(journal_path, canonical(committed) + b"\n")
        journal_path.unlink()
        return status
    except BaseException:
        recover(home_factory)
        raise


def roots(args: argparse.Namespace) -> tuple[Path, Path]:
    home_raw = os.environ.get("HOME", "")
    if not home_raw or not Path(home_raw).is_absolute():
        raise PinError("HOME must be absolute")
    home = Path(home_raw).resolve(strict=True)
    secure_directory(home, "HOME")
    home_factory = home / ".factory"
    secure_directory(home_factory, "owner Factory directory")
    secure_directory(home_factory / "bin", "owner launcher bin")
    kits_root = args.kits_root.resolve(strict=True)
    if kits_root != home_factory / "kits":
        raise PinError("provider CLI pin root must be the owner Factory kits root")
    secure_directory(kits_root, "Factory kits root")
    secure_directory(kits_root / "releases", "Factory releases root")
    secure_directory(kits_root / "manifests", "Factory manifests root")
    projects = kits_root / "projects"
    if projects.exists() or projects.is_symlink():
        secure_directory(projects, "Factory projects root")
    return home_factory, kits_root


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kits-root", required=True, type=Path)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--tree", required=True)
    parser.add_argument("--release", required=True, type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("plan", "apply"):
        sub = commands.add_parser(command)
        sub.add_argument("--claude-bin", required=True, type=Path)
        sub.add_argument("--codex-bin", required=True, type=Path)
        sub.add_argument("--cursor-bin", required=True, type=Path)
        sub.add_argument("--operator-id", required=True)
        if command == "apply":
            sub.add_argument("--approve-hash", required=True)
    commands.add_parser("check")
    args = parser.parse_args()
    try:
        home_factory, kits_root = roots(args)
        requested = release_identity(
            kits_root, args.release.resolve(strict=True), args.sha, args.tree
        )
        authority = authority_identity(kits_root)
        lock = home_factory / ".provider-cli-pin.lock"
        if args.command == "check":
            if lock.exists() or lock.is_symlink():
                with flock(lock, "provider CLI pin lock", create=False):
                    status = check_status(home_factory, kits_root, requested, authority)
            else:
                status = check_status(home_factory, kits_root, requested, authority)
            print(canonical(status).decode())
            return 0 if status["status"] == "ready" else 2
        if requested != authority:
            raise PinError("plan/apply candidate does not match the sealed authority helper")
        with flock(lock, "provider CLI pin lock"):
            candidates = {"claude": args.claude_bin, "codex": args.codex_bin, "agent": args.cursor_bin}
            with operation_guard(home_factory, kits_root) as projects:
                recover(home_factory)
                if args.command == "plan":
                    result = build_plan(home_factory, kits_root, requested, candidates, args.operator_id, projects)
                else:
                    result = apply_plan(home_factory, kits_root, requested, candidates, args.operator_id, args.approve_hash, projects)
            print(canonical(result).decode())
            return 0
    except (OSError, PinError, RuntimeError, sqlite3.Error, subprocess.SubprocessError) as error:
        print(f"provider-cli-pin: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
