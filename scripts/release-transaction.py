#!/usr/bin/env python3
"""Plan and resume one exact Contract 2 production release."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any


PLAN_SCHEMA = "nysa.software-factory.release-plan/v1"
JOURNAL_SCHEMA = "nysa.software-factory.release-journal/v1"
RESULT_SCHEMA = "nysa.software-factory.release-result/v1"
SHA = re.compile(r"[0-9a-f]{40}\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")
SAFE_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}\Z")
PROJECT = re.compile(r"[a-z0-9][a-z0-9-]{0,62}\Z")
TICKET = re.compile(r"T-[0-9]+\Z")
TICKET_STATES = frozenset({
    "Awaiting Approval", "Approved", "Backlog", "Blocked-Escalated",
    "Building", "Canceled", "Done", "Planning", "Ready", "Review",
})


class ReleaseError(ValueError):
    pass


def account_home() -> Path:
    override = os.environ.get("FACTORY_RELEASE_TEST_HOME", "")
    if override:
        if os.environ.get("FACTORY_KIT_TEST_MODE") != "1":
            raise ReleaseError("release test home is forbidden outside Factory test mode")
        path = Path(override)
        if not path.is_absolute():
            raise ReleaseError("release test home is invalid")
        return secure_directory(path.resolve(strict=True))
    return Path.home().resolve(strict=True)


def canonical(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def secure_directory(path: Path, *, create: bool = False) -> Path:
    if create:
        missing = []
        cursor = path
        while not cursor.exists() and not cursor.is_symlink():
            missing.append(cursor)
            cursor = cursor.parent
        for item in reversed(missing):
            try:
                item.mkdir(mode=0o700)
            except FileExistsError:
                pass
            info = item.lstat()
            if (
                item.is_symlink() or not stat.S_ISDIR(info.st_mode)
                or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077
            ):
                raise ReleaseError("release state parent directory is unsafe")
    try:
        info = path.lstat()
    except OSError as error:
        raise ReleaseError("release state directory is unavailable") from error
    if (
        not path.is_absolute() or path.is_symlink() or not stat.S_ISDIR(info.st_mode)
        or path.resolve(strict=True) != path or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) & 0o022
    ):
        raise ReleaseError("release state directory is unsafe")
    return path


def safe_state(path: Path, label: str) -> dict[str, Any]:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_size > 2_000_000
        ):
            raise ReleaseError(f"{label} is unsafe")
        with os.fdopen(descriptor, encoding="utf-8") as stream:
            descriptor = -1
            value = json.load(stream)
    except (json.JSONDecodeError, UnicodeError) as error:
        raise ReleaseError(f"{label} is invalid") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not isinstance(value, dict):
        raise ReleaseError(f"{label} is invalid")
    return value


def atomic_bytes(path: Path, value: bytes, mode: int = 0o600) -> None:
    secure_directory(path.parent, create=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
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


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    atomic_bytes(path, canonical(value))


def secure_regular_bytes(path: Path, label: str, *, executable: bool = False) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid()
            or before.st_nlink != 1 or stat.S_IMODE(before.st_mode) & 0o022
            or before.st_size > 10_000_000
            or executable and not stat.S_IMODE(before.st_mode) & 0o100
        ):
            raise ReleaseError(f"{label} is unsafe")
        chunks = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1_048_576))
            if not chunk:
                raise ReleaseError(f"{label} changed while reading")
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
        if (
            (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        ):
            raise ReleaseError(f"{label} changed while reading")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def exact_local_file(path: Path, expected: bytes, label: str) -> str:
    if path.exists() or path.is_symlink():
        if secure_regular_bytes(path, label) != expected:
            raise ReleaseError(f"{label} conflicts with this release")
    else:
        atomic_bytes(path, expected)
    return hashlib.sha256(expected).hexdigest()


def controller_payload(project: str, product: Path) -> bytes:
    home = account_home()
    label = f"com.factory.controller.{project}"
    value = {
        "Label": label,
        "ProcessType": "Interactive",
        "ProgramArguments": [
            str(home / ".factory/bin/factory-launch"), project, "reconcile", "--json",
        ],
        "RunAtLoad": True,
        "StandardErrorPath": str(home / f".factory/logs/{project}-controller.error.log"),
        "StandardOutPath": str(home / f".factory/logs/{project}-controller.log"),
        "StartInterval": 15,
        "WatchPaths": [str(product / "factory/runs")],
    }
    return plistlib.dumps(value, fmt=plistlib.FMT_XML, sort_keys=True)


def prepare_controller(project: str, product: Path) -> dict[str, Any]:
    if sys.platform != "darwin" or os.environ.get("FACTORY_KIT_TEST_MODE") == "1":
        return {"platform": sys.platform, "status": "not-applicable"}
    root = secure_directory(account_home() / "Library/LaunchAgents", create=True)
    path = root / f"com.factory.controller.{project}.plist"
    raw = controller_payload(project, product)
    return {
        "label": f"com.factory.controller.{project}", "path": str(path),
        "platform": "darwin", "sha256": exact_local_file(path, raw, "controller job"),
    }


def active_inventory(kits_root: Path) -> list[dict[str, Any]]:
    projects = secure_directory(kits_root / "projects", create=True)
    values = []
    for root in sorted(projects.iterdir()):
        if root.is_symlink() or not root.is_dir():
            raise ReleaseError("Factory project state is unsafe")
        active = root / "active.json"
        if not active.exists() and not active.is_symlink():
            continue
        record = safe_state(active, "active release")
        product = Path(str(record.get("product_path", "")))
        if not PROJECT.fullmatch(root.name) or not product.is_absolute():
            raise ReleaseError("active Factory project identity is invalid")
        values.append({
            "active_sha256": file_digest(active), "product": str(product),
            "project": root.name,
            "contract_version": str(record.get("contract_version", "")),
            "kit_sha": str(record.get("kit_sha", "")),
        })
    return values


def validate_launcher_plan(value: dict[str, Any]) -> None:
    body = {key: item for key, item in value.items() if key != "approval_sha256"}
    if (
        set(value) != {
            "action", "active_projects", "approval_sha256", "candidate",
            "previous_sha256", "schema", "target",
        }
        or value.get("schema") != "nysa.software-factory.owner-launcher-pin-plan/v1"
        or value.get("action") != "apply"
        or value.get("approval_sha256") != digest(body)
        or not isinstance(value.get("active_projects"), list)
        or not isinstance(value.get("candidate"), dict)
        or set(value["candidate"]) != {"path", "sha256"}
        or not Path(str(value["candidate"].get("path", ""))).is_absolute()
        or not DIGEST.fullmatch(str(value["candidate"].get("sha256", "")))
        or not Path(str(value.get("target", ""))).is_absolute()
        or value.get("previous_sha256") is not None
        and not DIGEST.fullmatch(str(value.get("previous_sha256", "")))
    ):
        raise ReleaseError("launcher pin plan is invalid")
    projects = value["active_projects"]
    if len({item.get("project") for item in projects if isinstance(item, dict)}) != len(projects):
        raise ReleaseError("launcher pin plan is invalid")
    for item in projects:
        if (
            not isinstance(item, dict)
            or set(item) != {
                "active_sha256", "contract_version", "kit_sha", "product", "project",
            }
            or not DIGEST.fullmatch(str(item.get("active_sha256", "")))
            or not SHA.fullmatch(str(item.get("kit_sha", "")))
            or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", str(item.get("contract_version", "")))
            or not Path(str(item.get("product", ""))).is_absolute()
            or not PROJECT.fullmatch(str(item.get("project", "")))
        ):
            raise ReleaseError("launcher pin plan is invalid")


def launcher_plan(release: Path, kits_root: Path) -> dict[str, Any]:
    candidate = release / "scripts/factory-launch"
    target = account_home() / ".factory/bin/factory-launch"
    secure_directory(target.parent, create=True)
    candidate_sha = hashlib.sha256(
        secure_regular_bytes(candidate, "sealed launcher candidate", executable=True)
    ).hexdigest()
    previous = None
    if target.exists() or target.is_symlink():
        previous = hashlib.sha256(
            secure_regular_bytes(target, "installed launcher", executable=True)
        ).hexdigest()
    if previous == candidate_sha:
        return {"action": "reuse", "path": str(target), "sha256": candidate_sha}
    body = {
        "action": "apply", "active_projects": active_inventory(kits_root),
        "candidate": {"path": str(candidate), "sha256": candidate_sha},
        "previous_sha256": previous,
        "schema": "nysa.software-factory.owner-launcher-pin-plan/v1",
        "target": str(target),
    }
    return {**body, "approval_sha256": digest(body)}


def _apply_launcher_plan_locked(
    value: dict[str, Any], release: Path, kits_root: Path,
    cutover_sha: str | None = None,
) -> dict[str, Any]:
    validate_launcher_plan(value)
    current_plan = launcher_plan(release, kits_root)
    replay = current_plan.get("action") == "reuse"
    if replay:
        if current_plan["sha256"] != value["candidate"]["sha256"]:
            raise ReleaseError("launcher pin target changed")
    elif current_plan != value:
        current = active_inventory(kits_root)
        expected = {
            item["project"]: item["product"] for item in value["active_projects"]
        }
        if (
            cutover_sha is None
            or {item["project"]: item["product"] for item in current} != expected
            or any(
                item["kit_sha"] != cutover_sha
                or int(item["contract_version"].split(".", 1)[0]) < 2
                for item in current
            )
        ):
            raise ReleaseError("launcher pin basis changed after approval")
    kit = release / "scripts/factory-kit.sh"
    if not replay:
        for item in value["active_projects"]:
            product = Path(item["product"])
            if not (product / "factory/MAINTENANCE").is_file():
                raise ReleaseError(f"active project {item['project']} is not in maintenance")
            run(
                ["bash", str(kit), "pause", "--project", item["project"],
                 "--product", str(product)], f"active project drain for {item['project']}",
                environment=command_environment(kits_root),
            )
    target = Path(value["target"])
    candidate = Path(value["candidate"]["path"])
    root = secure_directory(target.parent.parent, create=True)
    journal_path = root / "launcher-pin-journal.json"
    journal = None
    if journal_path.exists() or journal_path.is_symlink():
        journal = safe_state(journal_path, "launcher pin journal")
        unsigned = {key: item for key, item in journal.items() if key != "record_sha256"}
        if (
            journal.get("schema") != "nysa.software-factory.owner-launcher-pin-journal/v1"
            or journal.get("status") not in {"applying", "completed"}
            or journal.get("record_sha256") != digest(unsigned)
        ):
            raise ReleaseError("launcher pin journal is invalid")
        if journal.get("plan") != value:
            if journal.get("status") != "completed":
                raise ReleaseError("launcher pin transaction is incomplete")
            journal = None
    if journal is None:
        journal = {
            "plan": value,
            "schema": "nysa.software-factory.owner-launcher-pin-journal/v1",
            "status": "applying",
        }
        atomic_json(journal_path, {**journal, "record_sha256": digest(journal)})
    current_bytes = (
        secure_regular_bytes(target, "installed launcher", executable=True)
        if target.exists() else None
    )
    current = hashlib.sha256(current_bytes).hexdigest() if current_bytes is not None else None
    if current not in {value["previous_sha256"], value["candidate"]["sha256"]}:
        raise ReleaseError("launcher pin target changed")
    if current != value["candidate"]["sha256"]:
        rollback = secure_directory(root / "launcher-rollbacks", create=True) / (
            f"{value['approval_sha256']}.factory-launch"
        )
        if current is not None and not rollback.exists():
            atomic_bytes(rollback, current_bytes, 0o700)
        atomic_bytes(
            target, secure_regular_bytes(candidate, "sealed launcher candidate", executable=True),
            0o700,
        )
    if hashlib.sha256(
        secure_regular_bytes(target, "installed launcher", executable=True)
    ).hexdigest() != value["candidate"]["sha256"]:
        raise ReleaseError("launcher pin verification failed")
    completed = {
        "plan": value,
        "schema": "nysa.software-factory.owner-launcher-pin-journal/v1",
        "status": "completed",
    }
    atomic_json(journal_path, {**completed, "record_sha256": digest(completed)})
    return {
        "action": "reuse", "path": str(target),
        "sha256": value["candidate"]["sha256"],
        "status": "replayed" if replay else "applied",
    }


def apply_launcher_plan(
    value: dict[str, Any], release: Path, kits_root: Path,
    cutover_sha: str | None = None,
) -> dict[str, Any]:
    validate_launcher_plan(value)
    root = secure_directory(Path(value["target"]).parent.parent, create=True)
    descriptor = os.open(
        root / ".launcher-pin.lock",
        os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600,
    )
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o077
        ):
            raise ReleaseError("launcher pin lock is unsafe")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        return _apply_launcher_plan_locked(value, release, kits_root, cutover_sha)
    finally:
        os.close(descriptor)


def run(
    arguments: list[str], label: str, *, environment: dict[str, str] | None = None,
) -> str:
    result = subprocess.run(
        arguments, text=True, capture_output=True, check=False, timeout=1800,
        env=environment,
    )
    if result.returncode:
        raise ReleaseError(f"{label} failed")
    return result.stdout


def run_json(
    arguments: list[str], label: str, *, environment: dict[str, str] | None = None,
) -> dict[str, Any]:
    output = run(arguments, label, environment=environment)
    try:
        value = json.loads(output)
    except (json.JSONDecodeError, UnicodeError) as error:
        raise ReleaseError(f"{label} returned invalid evidence") from error
    if not isinstance(value, dict):
        raise ReleaseError(f"{label} returned invalid evidence")
    return value


def git(root: Path, *arguments: str) -> str:
    return run(["git", "-C", str(root), *arguments], "Git identity").strip()


def clean_identity(root: Path, label: str) -> tuple[str, str, str]:
    physical = root.resolve(strict=True)
    if physical != root or git(root, "rev-parse", "--show-toplevel") != str(root):
        raise ReleaseError(f"{label} must be an exact physical Git root")
    if git(root, "status", "--porcelain=v1", "--untracked-files=all"):
        raise ReleaseError(f"{label} must be clean")
    sha = git(root, "rev-parse", "HEAD")
    tree = git(root, "rev-parse", "HEAD^{tree}")
    if not SHA.fullmatch(sha) or not SHA.fullmatch(tree):
        raise ReleaseError(f"{label} identity is invalid")
    origin = git(root, "remote", "get-url", "origin")
    if not origin or re.search(r"[A-Za-z][A-Za-z0-9+.-]*://[^/\s]+@", origin):
        raise ReleaseError(f"{label} origin is unsafe")
    return sha, tree, origin


def capacity(product: Path) -> int:
    values: list[int] = []
    for line in (product / "factory/PROJECT.env").read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"(?:export\s+)?MAX_CONCURRENT_TICKETS\s*=\s*([1-9][0-9]*)", line.strip())
        if match:
            values.append(int(match.group(1)))
        elif re.match(r"(?:export\s+)?MAX_CONCURRENT_TICKETS\s*=", line.strip()):
            raise ReleaseError("product ticket capacity is invalid")
    if len(values) != 1 or not 1 <= values[0] <= 4:
        raise ReleaseError("product ticket capacity is invalid")
    return values[0]


def ticket_inventory(product: Path) -> list[dict[str, str]]:
    directory = product / "factory/tickets"
    if not directory.exists():
        return []
    secure_directory(directory)
    result = []
    for path in sorted(directory.glob("T-*.md")):
        ticket = path.stem
        if not TICKET.fullmatch(ticket):
            raise ReleaseError("product ticket filename is invalid")
        try:
            text = secure_regular_bytes(path, f"ticket {ticket}").decode("utf-8")
        except UnicodeError as error:
            raise ReleaseError(f"ticket {ticket} is invalid") from error
        states = re.findall(r"^State:\s*(.*?)\s*$", text, re.I | re.M)
        state = next(
            (candidate for candidate in TICKET_STATES
             if len(states) == 1 and states[0].casefold() == candidate.casefold()),
            None,
        )
        blob = git(product, "rev-parse", f"HEAD:factory/tickets/{path.name}")
        if state is None or not SHA.fullmatch(blob):
            raise ReleaseError(f"ticket {ticket} identity is invalid")
        result.append({"blob": blob, "state": state, "ticket": ticket})
    return result


def prepare_product_runtime(product: Path) -> None:
    for relative in ("factory/runs", "factory/.active-runs"):
        ignored = subprocess.run(
            ["git", "-C", str(product), "check-ignore", "-q", "--no-index",
             f"{relative}/.factory-release-probe"],
            check=False, timeout=120,
        )
        if ignored.returncode:
            raise ReleaseError(f"release setup requires {relative}/ to be gitignored")
        secure_directory(product / relative, create=True)


def contract(release: Path) -> str:
    try:
        value = json.loads(
            (release / "factory-contract.json").read_text(encoding="utf-8")
        )["contract_version"]
    except (KeyError, OSError, json.JSONDecodeError) as error:
        raise ReleaseError("candidate contract is invalid") from error
    if value != "2.0.0":
        raise ReleaseError("release setup requires Contract 2.0.0")
    return value


def command_environment(kits_root: Path, runtime: Path | None = None) -> dict[str, str]:
    environment = os.environ.copy()
    environment["FACTORY_KITS_ROOT"] = str(kits_root)
    if runtime is not None:
        environment["PATH"] = f"{runtime}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    return environment


def launcher_environment(kits_root: Path, runtime: Path) -> dict[str, str]:
    environment = command_environment(kits_root, runtime)
    if (
        kits_root == Path.home().resolve() / ".factory/kits"
        and os.environ.get("FACTORY_LAUNCH_TEST_MODE") != "1"
    ):
        environment.pop("FACTORY_KITS_ROOT", None)
    return environment


def runtime_candidates(explicit: Path | None) -> list[Path]:
    if explicit is not None:
        return [explicit.resolve(strict=True)]
    home = Path.home().resolve(strict=True)
    candidates = [
        home / ".factory/bin", home / ".local/bin", Path("/opt/homebrew/bin"),
        Path("/usr/local/bin"), Path("/usr/bin"), Path("/bin"),
    ]
    result: list[Path] = []
    for candidate in candidates:
        try:
            physical = candidate.resolve(strict=True)
        except OSError:
            continue
        if physical not in result:
            result.append(physical)
    return result


def project_runtime_root(kits_root: Path, project: str) -> Path:
    return kits_root.parent / "project-runtimes" / project


def prepare_runtime(
    release: Path, product: Path, kits_root: Path, project: str,
    explicit: Path | None,
) -> dict[str, Any]:
    helper = release / "scripts/owner-runtime-pin.py"
    root = secure_directory(project_runtime_root(kits_root, project), create=True)
    target = root / "bin"
    journal_path = root / "runtime-pin-journal.json"
    if journal_path.exists() or journal_path.is_symlink():
        journal = safe_state(journal_path, "runtime pin journal")
        plan = journal.get("plan")
        candidates = runtime_candidates(explicit)
        if (
            journal.get("status") != "completed" or not isinstance(plan, dict)
            or not DIGEST.fullmatch(str(plan.get("approval_sha256", "")))
            or plan.get("product_path") != str(product)
            or plan.get("target_bin") != str(target)
            or Path(str(plan.get("runtime_bin", ""))) not in candidates
        ):
            raise ReleaseError("existing project runtime does not match release setup")
        evidence = run_json(
            [sys.executable, "-I", "-S", str(helper), "check", "--journal",
             str(journal_path)],
            "project runtime replay", environment=command_environment(kits_root),
        )
        if evidence.get("status") != "ready" or evidence.get("path") != str(target):
            raise ReleaseError("project runtime replay evidence is invalid")
        return {"evidence": evidence, "plan_sha256": plan["approval_sha256"]}
    plans: list[dict[str, Any]] = []
    for candidate in runtime_candidates(explicit):
        result = subprocess.run(
            [sys.executable, "-I", "-S", str(helper), "plan", "--product",
             str(product), "--runtime-bin", str(candidate), "--target-bin", str(target)],
            text=True, capture_output=True, check=False,
            env=command_environment(kits_root), timeout=60,
        )
        if result.returncode:
            continue
        try:
            value = json.loads(result.stdout)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and value not in plans:
            plans.append(value)
    identities = {
        tuple(item["candidate"][tool]["path"] for tool in ("node", "npm", "npx"))
        for item in plans
    }
    if len(identities) != 1:
        raise ReleaseError("runtime resolution requires one exact compatible candidate")
    plan = plans[0]
    plan_path = root / "runtime-plan.json"
    atomic_json(plan_path, plan)
    evidence = run_json(
        [sys.executable, "-I", "-S", str(helper), "apply", "--plan",
         str(plan_path), "--approve-hash", plan["approval_sha256"]],
        "project runtime preparation", environment=command_environment(kits_root),
    )
    if evidence.get("status") not in {"applied", "replayed"}:
        raise ReleaseError("project runtime evidence is invalid")
    return {"evidence": evidence, "plan_sha256": plan["approval_sha256"]}


def child_plan(
    kit: Path, kits_root: Path, sha: str, product_capacity: int,
    cli_paths: dict[str, str], operator: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    environment = command_environment(kits_root)
    concurrency_check = subprocess.run(
        ["bash", str(kit), "provider-concurrency", "check", "--sha", sha,
         "--capacity", str(product_capacity)], text=True, capture_output=True,
        check=False, env=environment, timeout=120,
    )
    if product_capacity == 1:
        concurrency = {"action": "not-required", "capacity": 1}
    elif concurrency_check.returncode == 0:
        concurrency = {"action": "reuse", "evidence": json.loads(concurrency_check.stdout)}
    else:
        concurrency = {"action": "apply", "plan": run_json(
            ["bash", str(kit), "provider-concurrency", "plan", "--sha", sha,
             "--capacity", str(product_capacity)], "provider concurrency preview",
            environment=environment,
        )}
    cli_check = subprocess.run(
        ["bash", str(kit), "provider-cli-pin", "check", "--sha", sha],
        text=True, capture_output=True, check=False, env=environment, timeout=120,
    )
    if cli_check.returncode == 0:
        cli = {"action": "reuse", "evidence": json.loads(cli_check.stdout)}
    else:
        if set(cli_paths) != {"claude", "codex", "cursor"}:
            raise ReleaseError("provider CLI pin requires three explicit executable paths")
        cli = {"action": "apply", "plan": run_json(
            ["bash", str(kit), "provider-cli-pin", "plan", "--sha", sha,
             "--claude-bin", cli_paths["claude"], "--codex-bin", cli_paths["codex"],
             "--cursor-bin", cli_paths["cursor"], "--operator-id", operator],
            "provider CLI preview", environment=environment,
        )}
    return concurrency, cli


def seal_plan(body: dict[str, Any]) -> dict[str, Any]:
    return {**body, "approval_sha256": digest(body)}


def valid_controller(value: Any, project: str) -> bool:
    return isinstance(value, dict) and (
        value.get("status") == "not-applicable"
        and set(value) == {"platform", "status"}
        or value.get("status") != "not-applicable"
        and set(value) == {"label", "path", "platform", "sha256"}
        and value.get("platform") == "darwin"
        and value.get("label") == f"com.factory.controller.{project}"
        and Path(str(value.get("path", ""))).is_absolute()
        and DIGEST.fullmatch(str(value.get("sha256", ""))) is not None
    )


def valid_host_cutover(value: Any) -> bool:
    if value is None:
        return True
    if not isinstance(value, list) or len(value) != len({
        item.get("project") for item in value if isinstance(item, dict)
    }):
        return False
    for item in value:
        if not isinstance(item, dict) or set(item) != {
            "controller", "incident", "product", "project", "receipt", "runtime",
            "source_active_sha256",
        }:
            return False
        project = item.get("project")
        receipt = item.get("receipt")
        runtime = item.get("runtime")
        incident = item.get("incident")
        if (
            not isinstance(project, str) or not PROJECT.fullmatch(project)
            or not Path(str(item.get("product", ""))).is_absolute()
            or not DIGEST.fullmatch(str(item.get("source_active_sha256", "")))
            or not valid_controller(item.get("controller"), project)
            or not isinstance(receipt, dict)
            or set(receipt) != {"path", "receipt_id", "sha256"}
            or not Path(str(receipt.get("path", ""))).is_absolute()
            or not DIGEST.fullmatch(str(receipt.get("receipt_id", "")))
            or not DIGEST.fullmatch(str(receipt.get("sha256", "")))
            or not isinstance(runtime, dict)
            or set(runtime) != {"evidence", "plan_sha256"}
            or not isinstance(runtime.get("evidence"), dict)
            or not Path(str(runtime["evidence"].get("path", ""))).is_absolute()
            or not DIGEST.fullmatch(str(runtime.get("plan_sha256", "")))
            or incident is not None and (
                not isinstance(incident, dict)
                or set(incident) != {"label", "path", "sha256"}
                or incident.get("label") != f"com.factory.incident-reporter.{project}"
                or not Path(str(incident.get("path", ""))).is_absolute()
                or not DIGEST.fullmatch(str(incident.get("sha256", "")))
            )
        ):
            return False
    return True


def validate_plan(value: dict[str, Any]) -> None:
    required = {
        "approval_sha256", "children", "created_epoch", "expires_epoch", "identity",
        "request", "schema", "stage", "status",
    }
    body = {key: item for key, item in value.items() if key != "approval_sha256"}
    identity = value.get("identity")
    request = value.get("request")
    children = value.get("children")
    if (
        set(value) != required or value.get("schema") != PLAN_SCHEMA
        or value.get("stage") not in {"prerequisites", "activation"}
        or value.get("status") != "approval-required"
        or not DIGEST.fullmatch(str(value.get("approval_sha256", "")))
        or value["approval_sha256"] != digest(body)
        or not isinstance(value.get("created_epoch"), int)
        or isinstance(value.get("created_epoch"), bool)
        or not isinstance(value.get("expires_epoch"), int)
        or isinstance(value.get("expires_epoch"), bool)
        or value["expires_epoch"] <= value["created_epoch"]
        or not isinstance(identity, dict) or not isinstance(request, dict)
        or not isinstance(children, dict)
    ):
        raise ReleaseError("release plan is invalid")
    identity_keys = {
        "capacity", "contract_version", "controller", "factory_origin", "factory_sha",
        "factory_tree", "mode", "previous", "product_origin", "product_path", "product_sha",
        "product_tree", "runtime", "tickets",
    }
    request_keys = {
        "cli_paths", "migrations", "operator_id", "product", "profile", "project",
        "repo", "runtime_bin", "sha",
    }
    runtime = identity.get("runtime")
    evidence = runtime.get("evidence") if isinstance(runtime, dict) else None
    controller = identity.get("controller")
    migrations = request.get("migrations")
    cli_paths = request.get("cli_paths")
    inventory = identity.get("tickets")
    if (
        set(identity) != identity_keys or set(request) != request_keys
        or identity.get("contract_version") != "2.0.0"
        or identity.get("mode") not in {"new", "upgrade"}
        or not isinstance(identity.get("capacity"), int)
        or isinstance(identity.get("capacity"), bool)
        or not 1 <= identity["capacity"] <= 4
        or any(not SHA.fullmatch(str(identity.get(key, ""))) for key in (
            "factory_sha", "factory_tree", "product_sha", "product_tree",
        ))
        or request.get("sha") != identity.get("factory_sha")
        or request.get("product") != identity.get("product_path")
        or not PROJECT.fullmatch(str(request.get("project", "")))
        or not Path(str(request.get("product", ""))).is_absolute()
        or not Path(str(request.get("repo", ""))).is_absolute()
        or not SAFE_ID.fullmatch(str(request.get("operator_id", "")))
        or request.get("operator_id") == "auto"
        or not SAFE_ID.fullmatch(str(request.get("profile", "")))
        or not isinstance(cli_paths, dict) or set(cli_paths) - {"claude", "codex", "cursor"}
        or not isinstance(migrations, list) or not 0 <= len(migrations) <= 4
        or not isinstance(inventory, list)
        or not isinstance(runtime, dict) or set(runtime) != {"evidence", "plan_sha256"}
        or not DIGEST.fullmatch(str(runtime.get("plan_sha256", "")))
        or not isinstance(evidence, dict)
        or not Path(str(evidence.get("path", ""))).is_absolute()
        or not valid_controller(controller, str(request.get("project", "")))
        or (identity["mode"] == "new" and identity.get("previous") is not None)
        or (identity["mode"] == "upgrade" and not isinstance(identity.get("previous"), dict))
    ):
        raise ReleaseError("release plan is invalid")
    inventory_tickets: set[str] = set()
    for item in inventory:
        if (
            not isinstance(item, dict) or set(item) != {"blob", "state", "ticket"}
            or not TICKET.fullmatch(str(item.get("ticket", "")))
            or not SHA.fullmatch(str(item.get("blob", "")))
            or item.get("state") not in TICKET_STATES
            or item["ticket"] in inventory_tickets
        ):
            raise ReleaseError("release plan is invalid")
        inventory_tickets.add(item["ticket"])
    previous = identity.get("previous")
    if previous is not None and (
        set(previous) != {"record", "sha256"}
        or not isinstance(previous.get("record"), dict)
        or not DIGEST.fullmatch(str(previous.get("sha256", "")))
    ):
        raise ReleaseError("release plan is invalid")
    tickets: set[str] = set()
    for item in migrations:
        if (
            not isinstance(item, dict) or set(item) != {"ticket", "workdir"}
            or not TICKET.fullmatch(str(item.get("ticket", "")))
            or not Path(str(item.get("workdir", ""))).is_absolute()
            or item["ticket"] in tickets
        ):
            raise ReleaseError("release plan is invalid")
        tickets.add(item["ticket"])
    provider_cli = children.get("provider_cli")
    provider_concurrency = children.get("provider_concurrency")
    launcher = children.get("launcher")
    if (
        not isinstance(launcher, dict)
        or launcher.get("action") not in {"apply", "reuse"}
        or (launcher.get("action") == "apply" and (
            not DIGEST.fullmatch(str(launcher.get("approval_sha256", "")))
        ))
        or (launcher.get("action") == "reuse" and (
            not Path(str(launcher.get("path", ""))).is_absolute()
            or not DIGEST.fullmatch(str(launcher.get("sha256", "")))
        ))
        or
        not isinstance(provider_cli, dict)
        or provider_cli.get("action") not in {"apply", "reuse"}
        or not isinstance(provider_concurrency, dict)
        or provider_concurrency.get("action") not in {"apply", "reuse", "not-required"}
    ):
        raise ReleaseError("release plan is invalid")
    if launcher["action"] == "apply":
        validate_launcher_plan(launcher)
    for child in (provider_cli, provider_concurrency):
        if child["action"] == "apply" and (
            not isinstance(child.get("plan"), dict)
            or not DIGEST.fullmatch(str(child["plan"].get("approval_sha256", "")))
        ):
            raise ReleaseError("release plan is invalid")
    if value["stage"] == "prerequisites":
        if (
            set(children) != {
                "host_cutover", "launcher", "provider_cli", "provider_concurrency",
            }
            or not valid_host_cutover(children.get("host_cutover"))
            or "apply" not in {
                launcher["action"], provider_cli["action"], provider_concurrency["action"],
            }
        ):
            raise ReleaseError("release plan is invalid")
    else:
        if set(children) != {
            "launcher", "migration", "model", "provider_cli", "provider_concurrency",
            "qualification", "receipt",
        }:
            raise ReleaseError("release plan is invalid")
        model = children.get("model")
        receipt = children.get("receipt")
        migration = children.get("migration")
        if (
            "apply" in {
                launcher["action"], provider_cli["action"], provider_concurrency["action"],
            }
            or
            not isinstance(model, dict)
            or model.get("profile_id") != request["profile"]
            or not DIGEST.fullmatch(str(model.get("profile_hash", "")))
            or not isinstance(model.get("profile_version"), int)
            or isinstance(model.get("profile_version"), bool)
            or not isinstance(receipt, dict)
            or set(receipt) != {"path", "receipt_id", "sha256"}
            or not Path(str(receipt.get("path", ""))).is_absolute()
            or not DIGEST.fullmatch(str(receipt.get("receipt_id", "")))
            or not DIGEST.fullmatch(str(receipt.get("sha256", "")))
            or bool(migrations) != (migration is not None)
            or (migration is not None and (
                not isinstance(migration, dict)
                or not DIGEST.fullmatch(str(migration.get("approval_sha256", "")))
            ))
        ):
            raise ReleaseError("release plan is invalid")


def plan_paths(kits_root: Path, project: str, sha: str) -> tuple[Path, Path]:
    root = secure_directory(kits_root / "projects" / project / "release-plans", create=True)
    return root / f"{sha}.json", root / "journals"


def write_plan(path: Path, plan: dict[str, Any]) -> None:
    validate_plan(plan)
    immutable = secure_directory(path.parent / path.stem, create=True) / f"{plan['approval_sha256']}.json"
    if immutable.exists() or immutable.is_symlink():
        if safe_state(immutable, "stored release plan") != plan:
            raise ReleaseError("stored release plan hash collision")
    else:
        atomic_json(immutable, plan)
    atomic_json(path, plan)


def create_seed(release: Path, product: Path, root: Path) -> Path:
    try:
        tickets = json.loads(
            (product / "factory/QUALIFICATION.json").read_text(encoding="utf-8")
        )["tickets"]
    except (KeyError, OSError, json.JSONDecodeError) as error:
        raise ReleaseError("qualification ticket manifest is invalid") from error
    if (
        not isinstance(tickets, list) or not tickets
        or len(tickets) != len(set(tickets))
        or any(not isinstance(ticket, str) or not TICKET.fullmatch(ticket) for ticket in tickets)
    ):
        raise ReleaseError("qualification ticket manifest is invalid")
    seed_root = secure_directory(root / "seed", create=True)
    seed = seed_root / "operator-map.json"
    state = secure_directory(seed_root / "controller", create=True)
    if seed.exists() or seed.is_symlink():
        safe_state(seed, "qualification operator seed")
        return seed
    environment = os.environ.copy()
    environment["FACTORY_OPERATOR_MAP"] = str(seed)
    environment["FACTORY_CONTROLLER_STATE_DIR"] = str(state)
    for ticket in tickets:
        run(
            [sys.executable, "-I", str(release / "scripts/operator-cli.py"),
             "--product", str(product), "--state-dir", str(state), "init",
             "--ticket", ticket], f"qualification operator initialization for {ticket}",
            environment=environment,
        )
    seed.chmod(0o600)
    return seed


def profile_plan(
    release: Path, state_root: Path, project: str, profile: str,
) -> dict[str, Any]:
    preview = secure_directory(state_root / "model-profile-preview", create=True)
    value = run_json(
        [sys.executable, "-I", "-S", str(release / "scripts/model-manager.py"),
         "profiles", "--state-root", str(preview), "--project", project],
        "candidate model profile preview",
    )
    profiles = value.get("profiles")
    matches = [
        item for item in profiles if isinstance(item, dict)
        and item.get("profile_id") == profile
    ] if isinstance(profiles, list) else []
    if (
        value.get("schema") != "model-manager-profiles/v1" or len(matches) != 1
        or set(matches[0]) != {"profile_hash", "profile_id", "profile_version"}
        or not DIGEST.fullmatch(str(matches[0].get("profile_hash", "")))
        or not isinstance(matches[0].get("profile_version"), int)
        or isinstance(matches[0].get("profile_version"), bool)
    ):
        raise ReleaseError("candidate model profile is invalid")
    return matches[0]


def qualification_plans(
    release: Path, repo: Path, product: Path, project: str, sha: str,
    profile: str, migrations: list[dict[str, str]], state_root: Path,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None]:
    qualification = Path(f"/private/tmp/nysa-sf-qualification.release-{project}-{sha[:12]}")
    if qualification.exists():
        secure_directory(qualification)
    seed = create_seed(release, product, state_root)
    environment = os.environ.copy()
    environment["FACTORY_QUALIFICATION_OPERATOR_MAP_SEED"] = str(seed)
    prepared = run_json(
        [sys.executable, "-I", str(release / "scripts/qualification-environment.py"),
         "--factory-root", str(repo), "--product-root", str(product),
         "--project", project, "--root", str(qualification),
         "--operator-map-seed", str(seed)],
        "sealed qualification preparation", environment=environment,
    )
    launcher = Path(str(prepared.get("launcher", "")))
    if not launcher.is_absolute() or not launcher.is_file():
        raise ReleaseError("qualification launcher evidence is invalid")
    model = run_json(
        [str(launcher), project, "models", "plan", "--profile", profile, "--json"],
        "candidate model preview", environment=environment,
    )
    migration = None
    if migrations:
        arguments = [str(launcher), project, "models", "migrate-batch-plan"]
        for item in migrations:
            arguments.extend(["--ticket", item["ticket"], "--workdir", item["workdir"]])
        arguments.append("--json")
        migration = run_json(arguments, "candidate migration preview", environment=environment)
    return prepared, model, migration


def find_receipt(kits_root: Path, project: str, sha: str) -> tuple[Path, dict[str, Any]]:
    candidates: list[tuple[int, Path, dict[str, Any]]] = []
    consumed = kits_root / "receipts/consumed"
    for path in (kits_root / "receipts").glob("*.json"):
        try:
            value = safe_state(path, "certification receipt")
            receipt_id = value.get("receipt_id")
            if (
                value.get("project") == project and value.get("kit_sha") == sha
                and isinstance(receipt_id, str) and DIGEST.fullmatch(receipt_id)
                and not (consumed / f"{receipt_id}.json").exists()
            ):
                candidates.append((int(value.get("created_epoch", 0)), path, value))
        except (ReleaseError, TypeError, ValueError):
            continue
    if not candidates:
        raise ReleaseError("exact unconsumed certification receipt is unavailable")
    _, path, value = max(candidates, key=lambda item: item[0])
    return path, value


def incident_identity(project: str) -> dict[str, str] | None:
    if sys.platform != "darwin" or os.environ.get("FACTORY_KIT_TEST_MODE") == "1":
        return None
    path = account_home() / "Library/LaunchAgents" / (
        f"com.factory.incident-reporter.{project}.plist"
    )
    if not path.exists() and not path.is_symlink():
        return None
    raw = secure_regular_bytes(path, "incident reporter job")
    return {
        "label": f"com.factory.incident-reporter.{project}",
        "path": str(path),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def prepare_host_cutover(
    release: Path, kits_root: Path, sha: str, launcher: dict[str, Any],
    explicit_runtime: Path | None, target_project: str,
    target_product: Path, target_runtime: dict[str, Any],
    target_controller: dict[str, Any],
) -> list[dict[str, Any]]:
    if launcher.get("action") != "apply":
        return []
    kit = release / "scripts/factory-kit.sh"
    entries: list[dict[str, Any]] = []
    for source in launcher["active_projects"]:
        project = source["project"]
        product = Path(source["product"]).resolve(strict=True)
        _, _, _ = clean_identity(product, f"active product {project}")
        if (product / "factory/KIT_PIN").read_text(encoding="utf-8") != sha + "\n":
            raise ReleaseError(
                f"active project {project} has not staged the Contract 2 release pin"
            )
        runtime = (
            target_runtime if project == target_project and product == target_product
            else prepare_runtime(release, product, kits_root, project, explicit_runtime)
        )
        controller = (
            target_controller if project == target_project and product == target_product
            else prepare_controller(project, product)
        )
        environment = command_environment(kits_root, Path(runtime["evidence"]["path"]))
        environment["FACTORY_CONTRACT_2_CUTOVER"] = "1"
        run(
            ["bash", str(kit), "pause", "--project", project,
             "--product", str(product)], f"host cutover drain for {project}",
            environment=environment,
        )
        run(
            ["bash", str(kit), "certify", "--project", project,
             "--product", str(product), "--sha", sha],
            f"host cutover certification for {project}", environment=environment,
        )
        receipt_path, receipt = find_receipt(kits_root, project, sha)
        run(
            ["bash", str(kit), "plan", "--project", project,
             "--product", str(product), "--sha", sha,
             "--receipt", str(receipt_path)],
            f"host cutover activation preview for {project}", environment=environment,
        )
        entries.append({
            "controller": controller,
            "incident": incident_identity(project),
            "product": str(product),
            "project": project,
            "receipt": {
                "path": str(receipt_path), "receipt_id": receipt["receipt_id"],
                "sha256": file_digest(receipt_path),
            },
            "runtime": runtime,
            "source_active_sha256": source["active_sha256"],
        })
    return entries


def setup(args: argparse.Namespace) -> dict[str, Any]:
    project = args.project
    sha = args.sha
    if not PROJECT.fullmatch(project) or not SHA.fullmatch(sha):
        raise ReleaseError("release identity is invalid")
    product = args.product.resolve(strict=True)
    repo = args.repo.resolve(strict=True)
    kits_root = args.kits_root.resolve()
    secure_directory(kits_root, create=True)
    factory_sha, factory_tree, factory_origin = clean_identity(repo, "Factory candidate")
    product_sha, product_tree, product_origin = clean_identity(product, "product")
    prepare_product_runtime(product)
    if clean_identity(product, "product") != (
        product_sha, product_tree, product_origin,
    ):
        raise ReleaseError("product changed during runtime preparation")
    if factory_sha != sha:
        raise ReleaseError("Factory candidate does not match release SHA")
    if (product / "factory/KIT_PIN").read_text(encoding="utf-8") != sha + "\n":
        raise ReleaseError("product pin does not match release SHA")
    source_kit = repo / "scripts/factory-kit.sh"
    environment = command_environment(kits_root)
    run(
        ["bash", str(source_kit), "install", "--sha", sha, "--repo", str(repo)],
        "sealed release installation", environment=environment,
    )
    release = kits_root / "releases" / sha
    sealed_kit = release / "scripts/factory-kit.sh"
    release_contract = contract(release)
    runtime = prepare_runtime(release, product, kits_root, project, args.runtime_bin)
    controller = prepare_controller(project, product)
    runtime_bin = Path(str(runtime["evidence"]["path"]))
    active = kits_root / "projects" / project / "active.json"
    previous = None
    if active.exists() or active.is_symlink():
        active_value = safe_state(active, "active release")
        if active_value.get("product_path") != str(product):
            raise ReleaseError("active release belongs to a different product")
        previous = {"record": active_value, "sha256": file_digest(active)}
    mode = "upgrade" if previous is not None else "new"
    if mode == "upgrade":
        run(
            ["bash", str(sealed_kit), "pause", "--project", project,
             "--product", str(product)], "release maintenance entry",
            environment=command_environment(kits_root, runtime_bin),
        )
    launcher = launcher_plan(release, kits_root)
    cli_paths = {
        key: str(value.resolve(strict=True)) for key, value in {
            "claude": args.claude_bin, "codex": args.codex_bin,
            "cursor": args.cursor_bin,
        }.items() if value is not None
    }
    concurrency, cli = child_plan(
        sealed_kit, kits_root, sha, capacity(product), cli_paths, args.operator_id,
    )
    provider_prerequisite = (
        concurrency["action"] == "apply" or cli["action"] == "apply"
    )
    host_cutover = None
    if launcher["action"] == "apply" and not provider_prerequisite:
        host_cutover = prepare_host_cutover(
            release, kits_root, sha, launcher, args.runtime_bin, project, product,
            runtime, controller,
        )
    migrations = [
        {"ticket": ticket, "workdir": str(Path(workdir).resolve(strict=True))}
        for ticket, workdir in args.ticket_workdir
    ]
    request = {
        "cli_paths": cli_paths, "migrations": migrations, "operator_id": args.operator_id,
        "product": str(product), "profile": args.profile, "project": project,
        "repo": str(repo), "runtime_bin": str(args.runtime_bin.resolve(strict=True))
        if args.runtime_bin is not None else None, "sha": sha,
    }
    identity = {
        "capacity": capacity(product), "contract_version": release_contract,
        "controller": controller,
        "factory_origin": factory_origin, "factory_sha": sha,
        "factory_tree": factory_tree, "mode": mode,
        "previous": previous,
        "product_origin": product_origin, "product_path": str(product),
        "product_sha": product_sha, "product_tree": product_tree,
        "runtime": runtime, "tickets": ticket_inventory(product),
    }
    now = int(time.time())
    if (
        launcher["action"] == "apply" or concurrency["action"] == "apply"
        or cli["action"] == "apply"
    ):
        plan = seal_plan({
            "children": {
                "host_cutover": host_cutover,
                "launcher": launcher, "provider_cli": cli,
                "provider_concurrency": concurrency,
            },
            "created_epoch": now, "expires_epoch": now + 7200,
            "identity": identity, "request": request, "schema": PLAN_SCHEMA,
            "stage": "prerequisites", "status": "approval-required",
        })
    else:
        certification_environment = command_environment(kits_root, runtime_bin)
        run(
            ["bash", str(sealed_kit), "certify", "--project", project,
             "--product", str(product), "--sha", sha], "product certification",
            environment=certification_environment,
        )
        run(
            ["bash", str(sealed_kit), "pause", "--project", project,
             "--product", str(product)], "release maintenance entry",
            environment=certification_environment,
        )
        receipt_path, receipt = find_receipt(kits_root, project, sha)
        run(
            ["bash", str(sealed_kit), "plan", "--project", project,
             "--product", str(product), "--sha", sha, "--receipt", str(receipt_path)],
            "activation preview", environment=certification_environment,
        )
        state_root = secure_directory(kits_root / "projects" / project / "release-plans", create=True)
        if migrations:
            qualification, model, migration = qualification_plans(
                release, repo, product, project, sha, args.profile, migrations, state_root,
            )
        else:
            qualification = {"status": "not-required"}
            model = profile_plan(release, state_root, project, args.profile)
            migration = None
        plan = seal_plan({
            "children": {
                "migration": migration, "model": model,
                "launcher": launcher,
                "provider_cli": cli, "provider_concurrency": concurrency,
                "qualification": qualification,
                "receipt": {
                    "path": str(receipt_path), "receipt_id": receipt["receipt_id"],
                    "sha256": file_digest(receipt_path),
                },
            },
            "created_epoch": now, "expires_epoch": now + 7200,
            "identity": identity, "request": request, "schema": PLAN_SCHEMA,
            "stage": "activation", "status": "approval-required",
        })
    path, _ = plan_paths(kits_root, project, sha)
    write_plan(path, plan)
    return plan


def signed_journal(value: dict[str, Any]) -> dict[str, Any]:
    body = {key: item for key, item in value.items() if key != "record_sha256"}
    return {**body, "record_sha256": digest(body)}


def read_journal(path: Path, plan: dict[str, Any]) -> dict[str, Any]:
    value = safe_state(path, "release journal")
    body = {key: item for key, item in value.items() if key != "record_sha256"}
    if (
        value.get("schema") != JOURNAL_SCHEMA or value.get("plan") != plan
        or value.get("record_sha256") != digest(body)
        or not isinstance(value.get("events"), list)
        or value.get("status") not in {"in-progress", "pass"}
        or ("result_approval_sha256" in value and not DIGEST.fullmatch(
            str(value["result_approval_sha256"])
        ))
        or (value.get("status") == "pass" and plan.get("stage") == "prerequisites"
            and "result_approval_sha256" not in value)
    ):
        raise ReleaseError("release journal is invalid")
    return value


def journal_update(
    path: Path, plan: dict[str, Any], phase: str, status: str,
    result_approval_sha256: str = "",
) -> dict[str, Any]:
    observed = int(time.time() * 1000)
    if path.exists() or path.is_symlink():
        value = read_journal(path, plan)
    else:
        value = {"events": [], "plan": plan, "schema": JOURNAL_SCHEMA}
    event = {"observed_epoch_ms": observed, "phase": phase, "status": status}
    if not value["events"] or any(
        value["events"][-1].get(key) != event[key] for key in ("phase", "status")
    ):
        value["events"].append(event)
    value["phase"] = phase
    value["status"] = (
        "pass" if phase in {"prerequisites_applied", "dispatch_started"}
        and status == "pass" else "in-progress"
    )
    if result_approval_sha256:
        if not DIGEST.fullmatch(result_approval_sha256):
            raise ReleaseError("release journal result is invalid")
        existing = value.get("result_approval_sha256")
        if existing not in {None, result_approval_sha256}:
            raise ReleaseError("release journal result changed")
        value["result_approval_sha256"] = result_approval_sha256
    value = signed_journal(value)
    atomic_json(path, value)
    return value


def plan_request(plan: dict[str, Any], kits_root: Path) -> argparse.Namespace:
    request = plan["request"]
    migrations = [(item["ticket"], item["workdir"]) for item in request["migrations"]]
    return argparse.Namespace(
        project=request["project"], product=Path(request["product"]),
        repo=Path(request["repo"]), sha=request["sha"], kits_root=kits_root,
        profile=request["profile"], operator_id=request["operator_id"],
        runtime_bin=Path(request["runtime_bin"]) if request["runtime_bin"] else None,
        claude_bin=Path(request["cli_paths"]["claude"]) if "claude" in request["cli_paths"] else None,
        codex_bin=Path(request["cli_paths"]["codex"]) if "codex" in request["cli_paths"] else None,
        cursor_bin=Path(request["cli_paths"]["cursor"]) if "cursor" in request["cli_paths"] else None,
        ticket_workdir=migrations,
    )


def service_prefix() -> tuple[list[str], str]:
    launchctl = Path("/bin/launchctl")
    if not launchctl.is_file() or not os.access(launchctl, os.X_OK):
        raise ReleaseError("native service manager is unavailable")
    domain = f"gui/{os.getuid()}"
    return [str(launchctl), "asuser", str(os.getuid()), str(launchctl)], domain


def unload_service(value: dict[str, Any] | None) -> None:
    if (
        value is None or value.get("status") == "not-applicable"
        or sys.platform != "darwin" or os.environ.get("FACTORY_KIT_TEST_MODE") == "1"
    ):
        return
    prefix, domain = service_prefix()
    service = f"{domain}/{value['label']}"
    current = subprocess.run(
        prefix + ["print", service], stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, check=False, timeout=30,
    )
    if current.returncode == 0:
        run(prefix + ["bootout", service], f"service unload for {value['label']}")
    if subprocess.run(
        prefix + ["print", service], stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, check=False, timeout=30,
    ).returncode == 0:
        raise ReleaseError(f"service {value['label']} did not unload")


def ensure_service(value: dict[str, Any] | None) -> None:
    if (
        value is None or value.get("status") == "not-applicable"
        or sys.platform != "darwin" or os.environ.get("FACTORY_KIT_TEST_MODE") == "1"
    ):
        return
    path = Path(value["path"])
    if hashlib.sha256(secure_regular_bytes(path, "native service job")).hexdigest() != value["sha256"]:
        raise ReleaseError("native service job changed after setup")
    prefix, domain = service_prefix()
    service = f"{domain}/{value['label']}"
    run(prefix + ["enable", service], f"service enable for {value['label']}")
    current = subprocess.run(
        prefix + ["print", service], stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, check=False, timeout=30,
    )
    if current.returncode:
        run(prefix + ["bootstrap", domain, str(path)], f"service load for {value['label']}")
    if subprocess.run(
        prefix + ["print", service], stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, check=False, timeout=30,
    ).returncode:
        raise ReleaseError(f"service {value['label']} did not load")


def cutover_update(
    path: Path, approval_sha256: str, phase: str, projects: list[str], status: str,
) -> None:
    body = {
        "approval_sha256": approval_sha256,
        "completed_projects": projects,
        "phase": phase,
        "schema": "nysa.software-factory.host-cutover-journal/v1",
        "status": status,
    }
    atomic_json(path, {**body, "record_sha256": digest(body)})
    if os.environ.get("FACTORY_RELEASE_FAIL_AFTER_CUTOVER_PHASE") == phase:
        raise ReleaseError(f"injected failure after host cutover phase {phase}")


def read_cutover(path: Path, approval_sha256: str) -> dict[str, Any]:
    if not path.exists() and not path.is_symlink():
        return {"completed_projects": [], "phase": "approved", "status": "in-progress"}
    value = safe_state(path, "host cutover journal")
    body = {key: item for key, item in value.items() if key != "record_sha256"}
    if value.get("status") == "pass" and value.get("approval_sha256") != approval_sha256:
        return {"completed_projects": [], "phase": "approved", "status": "in-progress"}
    if (
        value.get("schema") != "nysa.software-factory.host-cutover-journal/v1"
        or value.get("approval_sha256") != approval_sha256
        or value.get("record_sha256") != digest(body)
        or value.get("status") not in {"in-progress", "pass"}
        or not isinstance(value.get("completed_projects"), list)
    ):
        raise ReleaseError("host cutover journal is invalid")
    return value


def cutover_active_exact(
    kits_root: Path, item: dict[str, Any], sha: str,
) -> bool:
    path = kits_root / "projects" / item["project"] / "active.json"
    if not path.exists() or path.is_symlink():
        return False
    record = safe_state(path, "active release")
    return all(record.get(key) == expected for key, expected in {
        "contract_version": "2.0.0", "kit_sha": sha,
        "product_path": item["product"], "project": item["project"],
        "receipt_id": item["receipt"]["receipt_id"],
    }.items())


def apply_host_cutover(
    plan: dict[str, Any], release: Path, kits_root: Path,
) -> None:
    items = plan["children"]["host_cutover"]
    if not valid_host_cutover(items):
        raise ReleaseError("host cutover plan is invalid")
    journal_path = kits_root / "contract-cutover-journal.json"
    journal = read_cutover(journal_path, plan["approval_sha256"])
    if journal["status"] == "pass":
        return
    completed = list(journal["completed_projects"])
    kit = release / "scripts/factory-kit.sh"
    for item in items:
        project = item["project"]
        active = kits_root / "projects" / project / "active.json"
        if project in completed:
            if not cutover_active_exact(kits_root, item, plan["request"]["sha"]):
                raise ReleaseError("completed host cutover project changed")
            continue
        if file_digest(active) != item["source_active_sha256"]:
            if cutover_active_exact(kits_root, item, plan["request"]["sha"]):
                completed.append(project)
                cutover_update(
                    journal_path, plan["approval_sha256"], f"project:{project}",
                    completed, "in-progress",
                )
                continue
            raise ReleaseError("host cutover active basis changed")
        unload_service(item["controller"])
        unload_service(item["incident"])
        environment = command_environment(
            kits_root, Path(item["runtime"]["evidence"]["path"]),
        )
        environment["FACTORY_CONTRACT_2_CUTOVER"] = "1"
        receipt = Path(item["receipt"]["path"])
        if (
            file_digest(receipt) != item["receipt"]["sha256"]
            or safe_state(receipt, "certification receipt").get("receipt_id")
            != item["receipt"]["receipt_id"]
        ):
            raise ReleaseError("host cutover receipt changed")
        run(
            ["bash", str(kit), "activate", "--project", project,
             "--product", item["product"], "--sha", plan["request"]["sha"],
             "--receipt", str(receipt)], f"host cutover activation for {project}",
            environment=environment,
        )
        if not cutover_active_exact(kits_root, item, plan["request"]["sha"]):
            raise ReleaseError("host cutover activation did not commit")
        completed.append(project)
        cutover_update(
            journal_path, plan["approval_sha256"], f"project:{project}",
            completed, "in-progress",
        )
    cutover_update(
        journal_path, plan["approval_sha256"], "active_records_switched",
        completed, "in-progress",
    )
    apply_launcher_plan(
        plan["children"]["launcher"], release, kits_root, plan["request"]["sha"],
    )
    cutover_update(
        journal_path, plan["approval_sha256"], "launcher_installed",
        completed, "in-progress",
    )
    floor = kits_root / "contract-floor.json"
    expected_floor = {
        "minimum_major": 2,
        "schema": "nysa.software-factory.contract-floor/v1",
    }
    if floor.exists() or floor.is_symlink():
        if safe_state(floor, "contract floor") != expected_floor:
            raise ReleaseError("contract floor changed")
    else:
        atomic_json(floor, expected_floor)
    cutover_update(
        journal_path, plan["approval_sha256"], "contract_floor_committed",
        completed, "in-progress",
    )
    launcher = account_home() / ".factory/bin/factory-launch"
    for item in items:
        ensure_service(item["controller"])
        ensure_service(item["incident"])
        run_json(
            [str(launcher), item["project"], "doctor", "--json"],
            f"host cutover Doctor for {item['project']}",
            environment=launcher_environment(
                kits_root, Path(item["runtime"]["evidence"]["path"]),
            ),
        )
    cutover_update(
        journal_path, plan["approval_sha256"], "healthy", completed, "pass",
    )


def apply_prerequisites(plan: dict[str, Any], kits_root: Path, approved_by: str) -> dict[str, Any]:
    request = plan["request"]
    release = kits_root / "releases" / request["sha"]
    kit = release / "scripts/factory-kit.sh"
    environment = command_environment(kits_root)
    concurrency = plan["children"]["provider_concurrency"]
    if concurrency["action"] == "apply":
        child = concurrency["plan"]
        run_json(
            ["bash", str(kit), "provider-concurrency", "apply", "--sha", request["sha"],
             "--capacity", str(plan["identity"]["capacity"]), "--approve-hash",
             child["approval_sha256"]], "provider concurrency apply", environment=environment,
        )
    cli = plan["children"]["provider_cli"]
    if cli["action"] == "apply":
        child = cli["plan"]
        paths = request["cli_paths"]
        run_json(
            ["bash", str(kit), "provider-cli-pin", "apply", "--sha", request["sha"],
             "--claude-bin", paths["claude"], "--codex-bin", paths["codex"],
             "--cursor-bin", paths["cursor"], "--operator-id", request["operator_id"],
             "--approve-hash", child["approval_sha256"]], "provider CLI apply",
            environment=environment,
        )
    launcher = plan["children"]["launcher"]
    host_cutover = plan["children"]["host_cutover"]
    if launcher["action"] == "apply" and host_cutover is not None:
        apply_host_cutover(plan, release, kits_root)
    return setup(plan_request(plan, kits_root))


def active_exact(kits_root: Path, plan: dict[str, Any]) -> bool:
    path = kits_root / "projects" / plan["request"]["project"] / "active.json"
    if not path.exists():
        return False
    value = safe_state(path, "active release")
    identity = plan["identity"]
    return all(value.get(key) == expected for key, expected in {
        "kit_sha": identity["factory_sha"], "kit_tree": identity["factory_tree"],
        "product_path": identity["product_path"], "product_sha": identity["product_sha"],
        "product_tree": identity["product_tree"], "contract_version": "2.0.0",
    }.items())


def validate_live_basis(kits_root: Path, plan: dict[str, Any]) -> None:
    identity = plan["identity"]
    product = Path(identity["product_path"])
    product_sha, product_tree, product_origin = clean_identity(product, "product")
    if (
        product_sha != identity["product_sha"]
        or product_tree != identity["product_tree"]
        or product_origin != identity["product_origin"]
        or ticket_inventory(product) != identity["tickets"]
        or (product / "factory/KIT_PIN").read_text(encoding="utf-8")
        != identity["factory_sha"] + "\n"
    ):
        raise ReleaseError("product changed after release setup")
    release = kits_root / "releases" / identity["factory_sha"]
    if contract(release) != identity["contract_version"]:
        raise ReleaseError("installed release changed after setup")
    launcher = plan["children"]["launcher"]
    installed_launcher = account_home() / ".factory/bin/factory-launch"
    expected_launcher = (
        launcher["sha256"] if launcher["action"] == "reuse"
        else launcher["candidate"]["sha256"]
    )
    observed_launcher = None
    if installed_launcher.exists() or installed_launcher.is_symlink():
        if installed_launcher.is_symlink():
            raise ReleaseError("installed launcher changed after setup")
        observed_launcher = hashlib.sha256(secure_regular_bytes(
            installed_launcher, "installed launcher", executable=True,
        )).hexdigest()
    allowed_launchers = {expected_launcher}
    if plan["stage"] == "prerequisites" and launcher["action"] == "apply":
        allowed_launchers.add(launcher["previous_sha256"])
    if observed_launcher not in allowed_launchers:
        raise ReleaseError("installed launcher changed after setup")
    controller = identity["controller"]
    if controller.get("status") != "not-applicable":
        expected_controller = controller_payload(plan["request"]["project"], product)
        if (
            not Path(controller["path"]).exists() or Path(controller["path"]).is_symlink()
            or
            hashlib.sha256(expected_controller).hexdigest() != controller["sha256"]
            or exact_local_file(
                Path(controller["path"]), expected_controller, "controller job"
            ) != controller["sha256"]
        ):
            raise ReleaseError("controller job changed after setup")
    runtime_journal = project_runtime_root(
        kits_root, plan["request"]["project"]
    ) / "runtime-pin-journal.json"
    runtime_value = safe_state(runtime_journal, "runtime pin journal")
    runtime_plan = runtime_value.get("plan")
    if (
        not isinstance(runtime_plan, dict)
        or runtime_plan.get("approval_sha256") != identity["runtime"]["plan_sha256"]
    ):
        raise ReleaseError("runtime changed after release setup")
    run_json(
        [sys.executable, "-I", "-S", str(release / "scripts/owner-runtime-pin.py"),
         "check", "--journal", str(runtime_journal)],
        "project runtime check", environment=command_environment(kits_root),
    )
    if not active_exact(kits_root, plan) and identity["mode"] == "upgrade":
        active = kits_root / "projects" / plan["request"]["project"] / "active.json"
        previous = identity["previous"]
        if (
            not active.exists() or active.is_symlink()
            or file_digest(active) != previous.get("sha256")
            or safe_state(active, "active release") != previous.get("record")
        ):
            raise ReleaseError("active release changed after setup")


def barrier_value(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "approval_sha256": plan["approval_sha256"],
        "project": plan["request"]["project"],
        "schema": "nysa.software-factory.release-cutover-barrier/v1",
    }


def ensure_barrier(product: Path, plan: dict[str, Any]) -> None:
    marker = product / "factory/KILL"
    expected = barrier_value(plan)
    if marker.exists() or marker.is_symlink():
        try:
            value = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ReleaseError("existing KILL marker is not this release barrier") from error
        if marker.is_symlink() or value != expected:
            raise ReleaseError("existing KILL marker is not this release barrier")
        return
    if not (product / "factory/MAINTENANCE").is_file():
        raise ReleaseError("release cutover lost both maintenance and its dispatch barrier")
    atomic_json(marker, expected)


def remove_maintenance(product: Path, plan: dict[str, Any]) -> None:
    marker = product / "factory/MAINTENANCE"
    try:
        value = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ReleaseError("maintenance marker does not match release") from error
    if (
        marker.is_symlink() or value.get("project") != plan["request"]["project"]
        or value.get("product_path") != str(product)
    ):
        raise ReleaseError("maintenance marker does not match release")
    marker.unlink()


def model_ready(launcher: Path, plan: dict[str, Any], environment: dict[str, str]) -> bool:
    status = run_json(
        [str(launcher), plan["request"]["project"], "models", "status", "--json"],
        "model status", environment=environment,
    )
    active = status.get("active_profile")
    model = plan["children"]["model"]
    return isinstance(active, dict) and all(active.get(key) == model.get(key) for key in (
        "profile_id", "profile_hash", "profile_version",
    ))


def doctor(launcher: Path, plan: dict[str, Any], environment: dict[str, str]) -> dict[str, Any]:
    return run_json(
        [str(launcher), plan["request"]["project"], "doctor", "--json"],
        "Factory Doctor", environment=environment,
    )


def operator_map_ready(plan: dict[str, Any]) -> bool:
    try:
        mapping = safe_state(
            Path(plan["identity"]["product_path"]) / "factory/operator-map.json",
            "operator map",
        )
    except (OSError, ReleaseError):
        return False
    tickets = mapping.get("tickets")
    return isinstance(tickets, dict) and all(
        isinstance(tickets.get(item["ticket"]), dict)
        and tickets[item["ticket"]].get("operator_fields_initialized") is True
        for item in plan["identity"]["tickets"]
    )


def initialize_operator_map(
    release: Path, kits_root: Path, plan: dict[str, Any],
    environment: dict[str, str],
) -> None:
    product = Path(plan["identity"]["product_path"])
    inventory = plan["identity"]["tickets"]
    arguments = [
        sys.executable, "-I", str(release / "scripts/operator-cli.py"),
        "--product", str(product), "--state-dir",
        str(kits_root / "projects" / plan["request"]["project"] / "controller"),
        "initialize",
    ]
    for item in inventory:
        arguments.extend(["--ticket", item["ticket"]])
    evidence = run_json(
        arguments, "operator projection initialization", environment=environment,
    )
    if evidence != {
        "initialized": sorted(item["ticket"] for item in inventory),
        "status": "pass",
    }:
        raise ReleaseError("operator projection initialization evidence is invalid")
    if not operator_map_ready(plan):
        raise ReleaseError("operator projection initialization is incomplete")


def ensure_controller(plan: dict[str, Any]) -> None:
    controller = plan["identity"]["controller"]
    if controller.get("status") == "not-applicable":
        return
    path = Path(controller["path"])
    expected = controller_payload(
        plan["request"]["project"], Path(plan["identity"]["product_path"]),
    )
    if exact_local_file(path, expected, "controller job") != controller["sha256"]:
        raise ReleaseError("controller job changed after setup")
    secure_directory(account_home() / ".factory/logs", create=True)
    launchctl = Path("/bin/launchctl")
    if not launchctl.is_file() or not os.access(launchctl, os.X_OK):
        raise ReleaseError("native controller service manager is unavailable")
    domain = f"gui/{os.getuid()}"
    service = f"{domain}/{controller['label']}"
    prefix = [str(launchctl), "asuser", str(os.getuid()), str(launchctl)]
    run(prefix + ["enable", service], "controller enable")
    current = subprocess.run(
        prefix + ["print", service], stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, check=False, timeout=30,
    )
    if current.returncode:
        subprocess.run(
            prefix + ["bootstrap", domain, str(path)], stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, check=False, timeout=30,
        )
    current = subprocess.run(
        prefix + ["print", service], stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, check=False, timeout=30,
    )
    if current.returncode:
        raise ReleaseError("controller service did not load")


def migration_complete(kits_root: Path, plan: dict[str, Any], approved_by: str) -> bool:
    migration = plan["children"]["migration"]
    if migration is None:
        return True
    path = (
        kits_root / "projects" / plan["request"]["project"] / "controller"
        / "migration-batches" / f"{migration['approval_sha256']}.json"
    )
    if not path.exists():
        return False
    value = safe_state(path, "migration batch journal")
    body = {key: item for key, item in value.items() if key != "record_sha256"}
    tickets = {item["ticket"] for item in migration.get("items", [])}
    return (
        value.get("schema") == "nysa.software-factory.model-migration-batch-journal/v1"
        and value.get("status") == "pass" and value.get("plan") == migration
        and value.get("approved_by") == approved_by
        and value.get("record_sha256") == digest(body)
        and isinstance(value.get("results"), dict)
        and set(value["results"]) == tickets
    )


def completed_result(plan: dict[str, Any], replayed: bool) -> dict[str, Any]:
    return {
        "approval_sha256": plan["approval_sha256"],
        "factory_sha": plan["identity"]["factory_sha"],
        "project": plan["request"]["project"], "schema": RESULT_SCHEMA,
        "status": "replayed" if replayed else "pass",
    }


def apply_activation(
    plan: dict[str, Any], kits_root: Path, approved_by: str, journal: Path,
) -> dict[str, Any]:
    request = plan["request"]
    project = request["project"]
    product = Path(request["product"])
    release = kits_root / "releases" / request["sha"]
    kit = release / "scripts/factory-kit.sh"
    launcher = account_home() / ".factory/bin/factory-launch"
    runtime = Path(plan["identity"]["runtime"]["evidence"]["path"])
    environment = launcher_environment(kits_root, runtime)
    value = safe_state(journal, "release journal") if journal.exists() else None
    if value and value.get("status") == "pass":
        if (
            not active_exact(kits_root, plan)
            or (product / "factory/KILL").exists()
            or (product / "factory/MAINTENANCE").exists()
            or not model_ready(launcher, plan, environment)
            or not migration_complete(kits_root, plan, approved_by)
            or not operator_map_ready(plan)
        ):
            raise ReleaseError("completed release evidence no longer matches runtime state")
        doctor(launcher, plan, environment)
        return completed_result(plan, True)
    if value and value.get("phase") in {"doctor_pass", "dispatch_started"} and (
        active_exact(kits_root, plan) and not (product / "factory/KILL").exists()
        and not (product / "factory/MAINTENANCE").exists()
        and model_ready(launcher, plan, environment)
        and migration_complete(kits_root, plan, approved_by)
        and operator_map_ready(plan)
    ):
        doctor(launcher, plan, environment)
        journal_update(journal, plan, "dispatch_started", "pass")
        return completed_result(plan, True)
    if not active_exact(kits_root, plan):
        receipt = Path(plan["children"]["receipt"]["path"])
        bound_receipt = plan["children"]["receipt"]
        evidence_path = receipt
        if not evidence_path.exists():
            evidence_path = (
                kits_root / "receipts/consumed" / f"{bound_receipt['receipt_id']}.json"
            )
        receipt_value = safe_state(evidence_path, "certification receipt")
        identity = plan["identity"]
        if (
            file_digest(evidence_path) != bound_receipt["sha256"]
            or receipt_value.get("receipt_id") != bound_receipt["receipt_id"]
            or receipt_value.get("project") != project
            or receipt_value.get("kit_sha") != identity["factory_sha"]
            or receipt_value.get("kit_tree") != identity["factory_tree"]
            or receipt_value.get("product_sha") != identity["product_sha"]
            or receipt_value.get("product_tree") != identity["product_tree"]
        ):
            raise ReleaseError("certification receipt changed after approval")
        subprocess.run(
            ["bash", str(kit), "reconcile", "--project", project,
             "--product", str(product)], text=True, capture_output=True,
            check=False, env=environment, timeout=300,
        )
        if not active_exact(kits_root, plan):
            run(
                ["bash", str(kit), "activate", "--project", project,
                 "--product", str(product), "--sha", request["sha"],
                 "--receipt", str(receipt)], "release activation", environment=environment,
            )
        if not active_exact(kits_root, plan):
            raise ReleaseError("activation did not commit the approved release")
    journal_update(journal, plan, "activated", "pass")
    ensure_barrier(product, plan)
    journal_update(journal, plan, "cutover_barrier", "pass")
    maintenance_removed = False
    try:
        if (product / "factory/MAINTENANCE").exists():
            remove_maintenance(product, plan)
        maintenance_removed = True
        model = plan["children"]["model"]
        if not model_ready(launcher, plan, environment):
            run_json(
                [str(launcher), project, "models", "activate", "--profile",
                 model["profile_id"], "--approve-hash", model["profile_hash"],
                 "--approved-by", approved_by, "--json"],
                "model profile activation", environment=environment,
            )
        journal_update(journal, plan, "model_activated", "pass")
        migration = plan["children"]["migration"]
        if migration is not None:
            arguments = [
                str(launcher), project, "models", "migrate-batch", "--approve-hash",
                migration["approval_sha256"], "--approved-by", approved_by,
            ]
            for item in request["migrations"]:
                arguments.extend(["--ticket", item["ticket"], "--workdir", item["workdir"]])
            arguments.append("--json")
            run_json(arguments, "ticket migration batch", environment=environment)
        journal_update(journal, plan, "migrated", "pass")
        initialize_operator_map(release, kits_root, plan, environment)
        journal_update(journal, plan, "operator_initialized", "pass")
        ensure_controller(plan)
        journal_update(journal, plan, "controller_enabled", "pass")
        doctor(launcher, plan, environment)
        journal_update(journal, plan, "doctor_pass", "pass")
        marker = product / "factory/KILL"
        try:
            marker_value = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ReleaseError("release dispatch barrier changed") from error
        if marker_value != barrier_value(plan):
            raise ReleaseError("release dispatch barrier changed")
        marker.unlink()
        journal_update(journal, plan, "dispatch_started", "pass")
        return completed_result(plan, False)
    except Exception:
        if maintenance_removed:
            subprocess.run(
                ["bash", str(kit), "pause", "--project", project,
                 "--product", str(product)], stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, env=environment, timeout=300,
            )
        raise


def resume(args: argparse.Namespace) -> dict[str, Any]:
    if (
        not PROJECT.fullmatch(args.project) or not SHA.fullmatch(args.sha)
        or not DIGEST.fullmatch(args.approve_hash)
        or not SAFE_ID.fullmatch(args.approved_by) or args.approved_by == "auto"
    ):
        raise ReleaseError("release approval boundary is invalid")
    kits_root = args.kits_root.resolve(strict=True)
    latest, journals = plan_paths(kits_root, args.project, args.sha)
    path = latest.parent / latest.stem / f"{args.approve_hash}.json"
    if not path.exists() or path.is_symlink():
        raise ReleaseError("approved hash does not match a stored release plan")
    plan = safe_state(path, "release plan")
    validate_plan(plan)
    if plan["approval_sha256"] != args.approve_hash:
        raise ReleaseError("approved hash does not match exact release plan")
    if plan["request"]["operator_id"] != args.approved_by:
        raise ReleaseError("release approver does not match setup operator")
    secure_directory(journals, create=True)
    journal = journals / f"{args.approve_hash}.json"
    if plan["expires_epoch"] <= int(time.time()):
        if not journal.exists():
            raise ReleaseError("release plan is stale")
        value = read_journal(journal, plan)
        approved = [
            item for item in value["events"]
            if isinstance(item, dict) and item.get("phase") == "approved"
            and item.get("status") == "pass"
            and isinstance(item.get("observed_epoch_ms"), int)
        ]
        if not approved or approved[0]["observed_epoch_ms"] > plan["expires_epoch"] * 1000:
            raise ReleaseError("release plan is stale")
    lock_path = journals / ".release.lock"
    descriptor = os.open(
        lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600,
    )
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o077
        ):
            raise ReleaseError("release lock is unsafe")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        if journal.exists():
            value = read_journal(journal, plan)
        else:
            value = journal_update(journal, plan, "approved", "pass")
        validate_live_basis(kits_root, plan)
        if plan["stage"] == "prerequisites":
            if value.get("status") == "pass":
                result_hash = value.get("result_approval_sha256")
                if not isinstance(result_hash, str) or not DIGEST.fullmatch(result_hash):
                    raise ReleaseError("completed prerequisite journal has no result")
                result_path = latest.parent / latest.stem / f"{result_hash}.json"
                result = safe_state(result_path, "release plan")
                validate_plan(result)
                if result["approval_sha256"] != result_hash or result["stage"] != "activation":
                    raise ReleaseError("prerequisite result plan is invalid")
                return result
            next_plan = apply_prerequisites(plan, kits_root, args.approved_by)
            journal_update(
                journal, plan, "prerequisites_applied", "pass",
                next_plan["approval_sha256"],
            )
            return next_plan
        return apply_activation(plan, kits_root, args.approved_by, journal)
    finally:
        os.close(descriptor)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--kits-root", required=True, type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    setup_parser = commands.add_parser("setup")
    setup_parser.add_argument("--project", required=True)
    setup_parser.add_argument("--product", required=True, type=Path)
    setup_parser.add_argument("--repo", required=True, type=Path)
    setup_parser.add_argument("--sha", required=True)
    setup_parser.add_argument("--profile", required=True)
    setup_parser.add_argument("--operator-id", required=True)
    setup_parser.add_argument("--runtime-bin", type=Path)
    setup_parser.add_argument("--claude-bin", type=Path)
    setup_parser.add_argument("--codex-bin", type=Path)
    setup_parser.add_argument("--cursor-bin", type=Path)
    setup_parser.add_argument("--ticket-workdir", action="append", nargs=2, default=[])
    resume_parser = commands.add_parser("resume")
    resume_parser.add_argument("--project", required=True)
    resume_parser.add_argument("--sha", required=True)
    resume_parser.add_argument("--approve-hash", required=True)
    resume_parser.add_argument("--approved-by", required=True)
    args = parser.parse_args()
    try:
        if args.command == "setup":
            if (
                not SAFE_ID.fullmatch(args.operator_id) or args.operator_id == "auto"
                or not SAFE_ID.fullmatch(args.profile)
                or any(not TICKET.fullmatch(item[0]) for item in args.ticket_workdir)
                or len(args.ticket_workdir) > 4
                or len({item[0] for item in args.ticket_workdir}) != len(args.ticket_workdir)
            ):
                raise ReleaseError("release setup arguments are invalid")
            result = setup(args)
        else:
            result = resume(args)
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return 0
    except (FileNotFoundError, OSError, ReleaseError, subprocess.SubprocessError) as error:
        print(json.dumps({
            "error": str(error), "schema": RESULT_SCHEMA, "status": "error",
        }, sort_keys=True, separators=(",", ":")))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
