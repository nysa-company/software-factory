#!/usr/bin/env python3
"""Plan and resume one exact Contract 2 production release."""

from __future__ import annotations

import argparse
import csv
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import pwd
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
from historical_pr_objects import run_git as hardened_git  # noqa: E402
from certification_plan import (  # noqa: E402
    PlanError, safe_plan, validate_plan as validate_certification_plan,
)


PLAN_SCHEMA = "nysa.software-factory.release-plan/v1"
JOURNAL_SCHEMA = "nysa.software-factory.release-journal/v1"
RESULT_SCHEMA = "nysa.software-factory.release-result/v1"
QUALIFICATION_PLAN_SCHEMA = "nysa.software-factory.qualification-migration-plan/v1"
QUALIFICATION_JOURNAL_SCHEMA = "nysa.software-factory.qualification-migration-journal/v1"
QUALIFICATION_RESULT_SCHEMA = "nysa.software-factory.qualification-migration-result/v1"
QUALIFICATION_RECEIPT_SCHEMA = "nysa.software-factory.qualification-migration-receipt/v1"
QUALIFICATION_RECOVERY_PLAN_SCHEMA = (
    "nysa.software-factory.qualification-attempt-recovery-plan/v1"
)
QUALIFICATION_RECOVERY_RECEIPT_SCHEMA = (
    "nysa.software-factory.qualification-attempt-recovery-receipt/v1"
)
QUALIFICATION_RECOVERY_RESULT_SCHEMA = (
    "nysa.software-factory.qualification-attempt-recovery-result/v1"
)
QUALIFICATION_BUDGET_MS = 60_000
SHA = re.compile(r"[0-9a-f]{40}\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")
SAFE_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}\Z")
PROJECT = re.compile(r"[a-z0-9][a-z0-9-]{0,62}\Z")
TICKET = re.compile(r"T-[0-9]+\Z")
RUN_ID = re.compile(r"[A-Za-z0-9._-]{1,200}\Z")
TICKET_STATES = frozenset({
    "Awaiting Approval", "Approved", "Backlog", "Blocked-Escalated",
    "Building", "Canceled", "Done", "Planning", "Ready", "Review",
})
_RETIRED_RUNTIME = "her" + "mes"
_CUTOVER_LOCK_FD: int | None = None
_PROCESS_STARTED = time.monotonic()


class ReleaseError(ValueError):
    def __init__(self, message: str, reason_code: str | None = None):
        super().__init__(message)
        self.reason_code = reason_code


def validate_optional_test_request(product: Path, requested: bool) -> None:
    if not requested:
        return
    try:
        plan, _ = safe_plan(product / "factory/certification-plan.json")
        phases = validate_certification_plan(plan, product)
    except (FileNotFoundError, OSError, PlanError, json.JSONDecodeError) as error:
        raise ReleaseError("optional-test certification request is invalid") from error
    if not any(phase.get("optional") is True for phase in phases.values()):
        raise ReleaseError("product certification plan has no optional tests")


def account_home() -> Path:
    override = os.environ.get("FACTORY_RELEASE_TEST_HOME", "")
    if os.environ.get("FACTORY_KIT_TEST_MODE") == "1" and not override:
        raise ReleaseError("Factory test mode requires an explicit isolated release test home")
    if override:
        if os.environ.get("FACTORY_KIT_TEST_MODE") != "1":
            raise ReleaseError("release test home is forbidden outside Factory test mode")
        path = Path(override)
        if not path.is_absolute():
            raise ReleaseError("release test home is invalid")
        path = secure_directory(path.resolve(strict=True))
        real_home = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve(strict=True)
        if (
            path == real_home or real_home in path.parents
            or stat.S_IMODE(path.stat().st_mode) != 0o700
        ):
            raise ReleaseError("release test home must be owner-only and outside the real account home")
        return path
    return Path.home().resolve(strict=True)


def require_test_layout(kits_root: Path) -> None:
    if os.environ.get("FACTORY_KIT_TEST_MODE") == "1":
        expected = account_home() / ".factory/kits"
        if kits_root != expected:
            raise ReleaseError("Factory test kits root must be inside the isolated test home")


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


def acquire_cutover_lock(kits_root: Path) -> int:
    global _CUTOVER_LOCK_FD
    root = secure_directory(kits_root, create=True)
    path = root / ".contract-cutover.lock"
    descriptor = os.open(
        path,
        os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600,
    )
    try:
        info = os.fstat(descriptor)
        current = os.stat(path, follow_symlinks=False)
        if (
            not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o077
            or (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino)
        ):
            raise ReleaseError("host cutover lock is unsafe")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
    except Exception:
        os.close(descriptor)
        raise
    os.set_inheritable(descriptor, True)
    _CUTOVER_LOCK_FD = descriptor
    return descriptor


def release_cutover_lock(descriptor: int) -> None:
    global _CUTOVER_LOCK_FD
    if _CUTOVER_LOCK_FD == descriptor:
        _CUTOVER_LOCK_FD = None
    os.close(descriptor)


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


def secure_regular_digest(path: Path, label: str) -> str:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid()
            or before.st_nlink != 1 or stat.S_IMODE(before.st_mode) & 0o022
        ):
            raise ReleaseError(f"{label} is unsafe")
        value = hashlib.sha256()
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1_048_576))
            if not chunk:
                raise ReleaseError(f"{label} changed while reading")
            value.update(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
        if (
            (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        ):
            raise ReleaseError(f"{label} changed while reading")
        return value.hexdigest()
    finally:
        os.close(descriptor)


def exact_local_file(path: Path, expected: bytes, label: str) -> str:
    if path.exists() or path.is_symlink():
        if secure_regular_bytes(path, label) != expected:
            raise ReleaseError(f"{label} conflicts with this release")
    else:
        atomic_bytes(path, expected)
    return hashlib.sha256(expected).hexdigest()


def controller_payload(
    project: str, product: Path, launcher: Path | None = None,
) -> bytes:
    home = account_home()
    launcher = launcher or home / ".factory/bin/factory-launch"
    label = f"com.factory.controller.{project}"
    value = {
        "Label": label,
        "ProcessType": "Interactive",
        "ProgramArguments": [
            str(launcher), project, "reconcile", "--json",
        ],
        "RunAtLoad": True,
        "StandardErrorPath": str(home / f".factory/logs/{project}-controller.error.log"),
        "StandardOutPath": str(home / f".factory/logs/{project}-controller.log"),
        "StartInterval": 15,
        "WatchPaths": [str(product / "factory/runs")],
    }
    return plistlib.dumps(value, fmt=plistlib.FMT_XML, sort_keys=True)


def prepare_controller(
    project: str, product: Path, launcher: Path | None = None,
) -> dict[str, Any]:
    if sys.platform != "darwin" or os.environ.get("FACTORY_KIT_TEST_MODE") == "1":
        return {"platform": sys.platform, "status": "not-applicable"}
    root = secure_directory(account_home() / "Library/LaunchAgents", create=True)
    path = root / f"com.factory.controller.{project}.plist"
    raw = controller_payload(project, product, launcher)
    if launcher is not None and launcher != account_home() / ".factory/bin/factory-launch":
        previous = None
        if path.exists() or path.is_symlink():
            previous = hashlib.sha256(
                secure_regular_bytes(path, "controller job")
            ).hexdigest()
        return {
            "action": "reuse" if previous == hashlib.sha256(raw).hexdigest() else "apply",
            "label": f"com.factory.controller.{project}", "path": str(path),
            "platform": "darwin", "previous_sha256": previous,
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
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
    paired = value.get("schema") == "nysa.software-factory.owner-launcher-pin-plan/v2"
    scoped = value.get("schema") == "nysa.software-factory.owner-launcher-pin-plan/v3"
    expected = {
        "action", "active_projects", "approval_sha256", "candidate",
        "previous_sha256", "schema", "target",
    } | ({"human_cli"} if paired or scoped else set())
    if (
        set(value) != expected
        or value.get("schema") not in {
            "nysa.software-factory.owner-launcher-pin-plan/v1",
            "nysa.software-factory.owner-launcher-pin-plan/v2",
            "nysa.software-factory.owner-launcher-pin-plan/v3",
        }
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
    if paired or scoped:
        human = value.get("human_cli")
        candidate = human.get("candidate") if isinstance(human, dict) else False
        if (
            not isinstance(human, dict)
            or set(human) != {"candidate", "previous_sha256", "target"}
            or not Path(str(human.get("target", ""))).is_absolute()
            or Path(str(human.get("target", "")))
            != account_home() / ".factory/bin/factory"
            or human.get("previous_sha256") is not None
            and not DIGEST.fullmatch(str(human.get("previous_sha256", "")))
            or candidate is not None and (
                not isinstance(candidate, dict)
                or set(candidate) != {"path", "sha256"}
                or not Path(str(candidate.get("path", ""))).is_absolute()
                or Path(str(candidate.get("path", "")))
                != Path(str(value["candidate"]["path"])).with_name("factory-cli.py")
                or not DIGEST.fullmatch(str(candidate.get("sha256", "")))
            )
        ):
            raise ReleaseError("launcher pin plan is invalid")
    if scoped:
        target = Path(value["target"])
        releases = account_home() / ".factory/kits/releases"
        if (
            target.name != "factory-launch" or target.parent.name != "scripts"
            or target.parent.parent.parent != releases
            or not SHA.fullmatch(target.parent.parent.name)
            or Path(value["candidate"]["path"]) != target
            or value["previous_sha256"] != value["candidate"]["sha256"]
            or value["active_projects"] != []
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


def validate_launcher_reuse(value: dict[str, Any]) -> None:
    paired = "human_cli" in value
    human = value.get("human_cli")
    if (
        set(value) != {"action", "path", "sha256"} | ({"human_cli"} if paired else set())
        or value.get("action") != "reuse"
        or not Path(str(value.get("path", ""))).is_absolute()
        or not DIGEST.fullmatch(str(value.get("sha256", "")))
        or paired and (
            not isinstance(human, dict) or set(human) != {"path", "sha256"}
            or not Path(str(human.get("path", ""))).is_absolute()
            or Path(str(human.get("path", "")))
            != account_home() / ".factory/bin/factory"
            or human.get("sha256") is not None
            and not DIGEST.fullmatch(str(human.get("sha256", "")))
        )
    ):
        raise ReleaseError("launcher pin reuse is invalid")


def pinned_human_cli() -> str | None:
    root = account_home() / ".factory"
    command = root / "bin/factory"
    journal_path = root / "launcher-pin-journal.json"
    if not command.exists() or not journal_path.exists():
        return None
    try:
        observed = hashlib.sha256(secure_regular_bytes(
            command, "installed human CLI", executable=True,
        )).hexdigest()
        journal = safe_state(journal_path, "launcher pin journal")
        unsigned = {key: item for key, item in journal.items() if key != "record_sha256"}
        plan = journal.get("plan")
        if (
            journal.get("schema") != "nysa.software-factory.owner-launcher-pin-journal/v1"
            or journal.get("status") != "completed"
            or journal.get("record_sha256") != digest(unsigned)
            or not isinstance(plan, dict)
            or plan.get("schema") != "nysa.software-factory.owner-launcher-pin-plan/v3"
        ):
            return None
        validate_launcher_plan(plan)
        human = plan["human_cli"]
        candidate = human.get("candidate")
        if (
            Path(human["target"]) != command or not isinstance(candidate, dict)
            or candidate.get("sha256") != observed
        ):
            return None
        return observed
    except ReleaseError:
        return None


def launcher_plan(
    release: Path, kits_root: Path, project: str | None = None,
) -> dict[str, Any]:
    candidate = release / "scripts/factory-launch"
    target = candidate if project is not None else account_home() / ".factory/bin/factory-launch"
    secure_directory(target.parent, create=True)
    candidate_sha = hashlib.sha256(
        secure_regular_bytes(candidate, "sealed launcher candidate", executable=True)
    ).hexdigest()
    previous = None
    if target.exists() or target.is_symlink():
        previous = hashlib.sha256(
            secure_regular_bytes(target, "installed launcher", executable=True)
        ).hexdigest()
    human_candidate = release / "scripts/factory-cli.py"
    human_target = account_home() / ".factory/bin/factory"
    human_present = human_target.exists() or human_target.is_symlink()
    candidate_present = human_candidate.exists() or human_candidate.is_symlink()
    if project is not None and not candidate_present:
        raise ReleaseError("project-scoped activation requires the human CLI")
    human_previous = None
    if human_present:
        human_previous = hashlib.sha256(secure_regular_bytes(
            human_target, "installed human CLI", executable=True,
        )).hexdigest()
    human_candidate_value = None
    if candidate_present:
        human_candidate_value = {
            "path": str(human_candidate),
            "sha256": hashlib.sha256(secure_regular_bytes(
                human_candidate, "sealed human CLI candidate", executable=True,
            )).hexdigest(),
        }
    paired = human_present or candidate_present
    pinned_human = pinned_human_cli() if project is not None else None
    desired_human = pinned_human or (
        human_candidate_value["sha256"] if human_candidate_value is not None else None
    )
    human_matches = human_previous == desired_human
    if previous == candidate_sha and human_matches:
        reuse = {"action": "reuse", "path": str(target), "sha256": candidate_sha}
        if paired:
            reuse["human_cli"] = {"path": str(human_target), "sha256": desired_human}
        return reuse
    body = {
        "action": "apply",
        "active_projects": [] if project is not None else active_inventory(kits_root),
        "candidate": {"path": str(candidate), "sha256": candidate_sha},
        "previous_sha256": previous,
        "schema": (
            "nysa.software-factory.owner-launcher-pin-plan/v3" if project is not None
            else "nysa.software-factory.owner-launcher-pin-plan/v2" if paired
            else "nysa.software-factory.owner-launcher-pin-plan/v1"
        ),
        "target": str(target),
    }
    if paired:
        body["human_cli"] = {
            "candidate": human_candidate_value,
            "previous_sha256": human_previous,
            "target": str(human_target),
        }
    return {**body, "approval_sha256": digest(body)}


def launcher_path(value: dict[str, Any]) -> Path:
    return Path(value["target"] if value["action"] == "apply" else value["path"])


def project_launcher(value: dict[str, Any]) -> bool:
    path = launcher_path(value)
    releases = account_home() / ".factory/kits/releases"
    return (
        path.name == "factory-launch" and path.parent.name == "scripts"
        and path.parent.parent.parent == releases
        and SHA.fullmatch(path.parent.parent.name) is not None
    )


def _apply_launcher_plan_locked(
    value: dict[str, Any], release: Path, kits_root: Path,
    cutover_sha: str | None = None,
) -> dict[str, Any]:
    validate_launcher_plan(value)
    target = Path(value["target"])
    root = secure_directory(account_home() / ".factory", create=True)
    journal_path = root / "launcher-pin-journal.json"
    recovering = False
    if journal_path.exists() or journal_path.is_symlink():
        pending = safe_state(journal_path, "launcher pin journal")
        unsigned = {key: item for key, item in pending.items() if key != "record_sha256"}
        if (
            pending.get("schema")
            != "nysa.software-factory.owner-launcher-pin-journal/v1"
            or pending.get("status") not in {"applying", "completed"}
            or pending.get("record_sha256") != digest(unsigned)
        ):
            raise ReleaseError("launcher pin journal is invalid")
        recovering = pending.get("status") == "applying" and pending.get("plan") == value
    current_plan = launcher_plan(
        release, kits_root, "project" if project_launcher(value) else None,
    )
    replay = current_plan.get("action") == "reuse" or recovering
    if current_plan.get("action") == "reuse":
        expected_human = value.get("human_cli", {}).get("candidate")
        if (
            current_plan["sha256"] != value["candidate"]["sha256"]
            or value.get("human_cli") and current_plan.get("human_cli", {}).get("sha256")
            != (expected_human["sha256"] if expected_human else None)
        ):
            raise ReleaseError("launcher pin target changed")
    elif current_plan != value and not recovering:
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
    candidate = Path(value["candidate"]["path"])
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
    rollback = secure_directory(root / "launcher-rollbacks", create=True) / (
        f"{value['approval_sha256']}.factory-launch"
    )
    if rollback.exists() or rollback.is_symlink():
        if (
            value["previous_sha256"] is None
            or hashlib.sha256(secure_regular_bytes(
                rollback, "launcher rollback", executable=True,
            )).hexdigest() != value["previous_sha256"]
        ):
            raise ReleaseError("launcher rollback is invalid")
    if current != value["candidate"]["sha256"]:
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
    human_plan = value.get("human_cli")
    if human_plan is not None:
        human_target = Path(human_plan["target"])
        human_candidate = human_plan["candidate"]
        human_current_bytes = (
            secure_regular_bytes(human_target, "installed human CLI", executable=True)
            if human_target.exists() or human_target.is_symlink() else None
        )
        human_current = (
            hashlib.sha256(human_current_bytes).hexdigest()
            if human_current_bytes is not None else None
        )
        human_desired = human_candidate["sha256"] if human_candidate else None
        if human_current not in {human_plan["previous_sha256"], human_desired}:
            raise ReleaseError("human CLI pin target changed")
        rollback = secure_directory(root / "launcher-rollbacks", create=True) / (
            f"{value['approval_sha256']}.factory"
        )
        if rollback.exists() or rollback.is_symlink():
            if (
                human_plan["previous_sha256"] is None
                or hashlib.sha256(secure_regular_bytes(
                    rollback, "human CLI rollback", executable=True,
                )).hexdigest() != human_plan["previous_sha256"]
            ):
                raise ReleaseError("human CLI rollback is invalid")
        if human_current != human_desired:
            if human_current is not None and not rollback.exists():
                atomic_bytes(rollback, human_current_bytes, 0o700)
            if human_candidate is None:
                human_target.unlink()
                sync_directory(human_target.parent)
            else:
                atomic_bytes(human_target, secure_regular_bytes(
                    Path(human_candidate["path"]), "sealed human CLI candidate",
                    executable=True,
                ), 0o700)
        if human_desired is None:
            if human_target.exists() or human_target.is_symlink():
                raise ReleaseError("human CLI removal failed")
        elif hashlib.sha256(secure_regular_bytes(
            human_target, "installed human CLI", executable=True,
        )).hexdigest() != human_desired:
            raise ReleaseError("human CLI installation failed")
    completed = {
        "plan": value,
        "schema": "nysa.software-factory.owner-launcher-pin-journal/v1",
        "status": "completed",
    }
    atomic_json(journal_path, {**completed, "record_sha256": digest(completed)})
    result = {
        "action": "reuse", "path": str(target),
        "sha256": value["candidate"]["sha256"],
        "status": "replayed" if replay else "applied",
    }
    if human_plan is not None:
        result["human_cli"] = {
            "path": human_plan["target"],
            "sha256": (
                human_plan["candidate"]["sha256"]
                if human_plan["candidate"] else None
            ),
        }
    return result


def apply_launcher_plan(
    value: dict[str, Any], release: Path, kits_root: Path,
    cutover_sha: str | None = None,
) -> dict[str, Any]:
    validate_launcher_plan(value)
    root = secure_directory(account_home() / ".factory", create=True)
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
    timeout: float = 1800,
) -> str:
    pass_fds = (
        (_CUTOVER_LOCK_FD,)
        if _CUTOVER_LOCK_FD is not None and environment is not None
        and environment.get("FACTORY_HOST_CUTOVER_LOCK_FD") == str(_CUTOVER_LOCK_FD)
        else ()
    )
    result = subprocess.run(
        arguments, text=True, capture_output=True, check=False, timeout=timeout,
        env=environment, pass_fds=pass_fds,
    )
    if result.returncode:
        raise ReleaseError(f"{label} failed")
    return result.stdout


def run_json(
    arguments: list[str], label: str, *, environment: dict[str, str] | None = None,
    timeout: float = 1800,
) -> dict[str, Any]:
    output = run(arguments, label, environment=environment, timeout=timeout)
    try:
        value = json.loads(output)
    except (json.JSONDecodeError, UnicodeError) as error:
        raise ReleaseError(f"{label} returned invalid evidence") from error
    if not isinstance(value, dict):
        raise ReleaseError(f"{label} returned invalid evidence")
    return value


def release_preflight(
    kit: Path, kits_root: Path, runtime_bin: Path, project: str,
    product: Path, sha: str,
) -> dict[str, Any]:
    arguments = [
        "bash", str(kit), "preflight-report", "--project", project,
        "--product", str(product), "--sha", sha, "--json",
    ]
    result = subprocess.run(
        arguments, text=True, capture_output=True, check=False,
        env=command_environment(kits_root, runtime_bin), timeout=1800,
    )
    try:
        report = json.loads(result.stdout)
    except (json.JSONDecodeError, UnicodeError) as error:
        raise ReleaseError("activation readiness returned invalid evidence") from error
    if not isinstance(report, dict) or report.get("status") not in {
        "pass", "blocked", "authorization-required",
    }:
        raise ReleaseError("activation readiness returned invalid evidence")
    if result.returncode == 0 and report["status"] == "pass":
        return report
    if report["status"] == "authorization-required":
        raise ReleaseError("activation readiness requires certification network review")
    blockers = report.get("blockers")
    details = ", ".join(
        f"{item.get('scope')}:{item.get('reason_code')}"
        for item in blockers if isinstance(item, dict)
    ) if isinstance(blockers, list) else ""
    raise ReleaseError(f"activation readiness blocked{': ' + details if details else ''}")


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
        if re.fullmatch(r"T-[0-9]{3}-bundle\.md", path.name):
            continue
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


def validate_product_runtime_contract(
    product: Path, *, require_idle_dispatch: bool = True,
) -> None:
    for relative in (
        "factory/runs", "factory/.active-runs", "factory/.dispatch-leases",
        "factory/.dispatch-leases.lock", "factory/.operator-clears",
    ):
        tracked = hardened_git(product, "ls-files", "--", relative)
        if tracked.returncode != 0 or tracked.stdout:
            raise ReleaseError(f"release setup requires {relative} to be untracked")
    lease_dir = product / "factory/.dispatch-leases"
    if lease_dir.exists() or lease_dir.is_symlink():
        try:
            info = lease_dir.lstat()
        except OSError as error:
            raise ReleaseError("release setup dispatcher lease state is invalid") from error
        if (
            lease_dir.is_symlink() or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700
            or (require_idle_dispatch and any(lease_dir.iterdir()))
        ):
            raise ReleaseError("release setup requires factory/.dispatch-leases to be empty or absent")
    lease_lock = product / "factory/.dispatch-leases.lock"
    if lease_lock.exists() or lease_lock.is_symlink():
        raise ReleaseError("release setup requires factory/.dispatch-leases.lock to be absent")
    operator_clears = product / "factory/.operator-clears"
    if operator_clears.exists() or operator_clears.is_symlink():
        secure_directory(operator_clears)
    for relative in (
        "factory/runs/.factory-release-probe",
        "factory/.active-runs/.factory-release-probe",
        "factory/operator-map.json",
        "factory/.operator-map.lock",
        "factory/.operator-clears/.factory-release-probe",
        "factory/.dispatch-leases/.factory-release-probe",
        "factory/.dispatch-leases.lock/.factory-release-probe",
    ):
        tracked = hardened_git(
            product, "ls-files", "--error-unmatch", "--", relative,
        )
        if tracked.returncode != 1:
            raise ReleaseError(f"release setup requires {relative} to be untracked")
        ignored = hardened_git(
            product, "check-ignore", "-v", "--no-index", "--stdin", "-z",
            input_text=relative + "\0",
        )
        fields = ignored.stdout.split("\0")
        if (
            ignored.returncode != 0 or ignored.stderr or len(fields) != 5
            or fields[4] or fields[3] != relative or not fields[1].isdigit()
            or not fields[2] or fields[2].startswith("!")
        ):
            raise ReleaseError(f"release setup requires {relative} to be gitignored")
        source_path = product / fields[0]
        try:
            source_info = source_path.lstat()
            source_relative = str(
                source_path.resolve(strict=True).relative_to(product)
            )
        except (OSError, ValueError) as error:
            raise ReleaseError("release setup ignore authority is invalid") from error
        source_tracked = hardened_git(
            product, "ls-files", "--error-unmatch", "--", source_relative,
        )
        head_blob = hardened_git(product, "rev-parse", f"HEAD:{source_relative}")
        worktree_blob = hardened_git(
            product, "hash-object", "--no-filters", "--", source_relative,
        )
        if (
            source_path.is_symlink() or not stat.S_ISREG(source_info.st_mode)
            or source_tracked.returncode != 0
            or source_tracked.stdout.strip() != source_relative
            or head_blob.returncode != 0 or worktree_blob.returncode != 0
            or head_blob.stdout.strip() != worktree_blob.stdout.strip()
        ):
            raise ReleaseError("release setup ignore authority is invalid")


def prepare_product_runtime(product: Path) -> None:
    for relative in ("factory/runs", "factory/.active-runs"):
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


def command_environment(
    kits_root: Path, runtime: Path | None = None, *, cutover_lock: bool = False,
) -> dict[str, str]:
    environment = os.environ.copy()
    environment.pop("FACTORY_CONTRACT_2_CUTOVER", None)
    environment.pop("FACTORY_HOST_CUTOVER_RESERVATION", None)
    environment.pop("FACTORY_MAINTENANCE_OWNER", None)
    environment.pop("FACTORY_HOST_CUTOVER_LOCK_FD", None)
    environment["FACTORY_KITS_ROOT"] = str(kits_root)
    if cutover_lock and _CUTOVER_LOCK_FD is not None:
        environment["FACTORY_HOST_CUTOVER_LOCK_FD"] = str(_CUTOVER_LOCK_FD)
    if runtime is not None:
        environment["PATH"] = f"{runtime}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    return environment


def launcher_environment(kits_root: Path, runtime: Path) -> dict[str, str]:
    environment = command_environment(kits_root, runtime)
    if os.environ.get("FACTORY_KIT_TEST_MODE") == "1":
        home = account_home()
        if kits_root != home / ".factory/kits":
            raise ReleaseError("Factory test launcher root is outside the isolated home")
        environment.update({
            "FACTORY_LAUNCH_TEST_HOME": str(home),
            "FACTORY_LAUNCH_TEST_MODE": "1",
        })
    elif kits_root == account_home() / ".factory/kits":
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
    failures: list[str] = []
    for candidate in runtime_candidates(explicit):
        result = subprocess.run(
            [sys.executable, "-I", "-S", str(helper), "plan", "--product",
             str(product), "--runtime-bin", str(candidate), "--target-bin", str(target)],
            text=True, capture_output=True, check=False,
            env=command_environment(kits_root), timeout=60,
        )
        if result.returncode:
            detail = result.stderr.strip().removeprefix("ERROR: ").strip()
            if detail:
                failures.append(detail)
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
        mismatch = next(
            (failure for failure in failures if failure.startswith("runtime mismatch for ")),
            None,
        )
        if not plans and mismatch:
            raise ReleaseError(f"runtime_tuple_mismatch: {mismatch}")
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
    cli_paths: dict[str, str], operator: str, *, timeout: float = 120,
) -> tuple[dict[str, Any], dict[str, Any]]:
    environment = command_environment(kits_root)
    concurrency_check = subprocess.run(
        ["bash", str(kit), "provider-concurrency", "check", "--sha", sha,
         "--capacity", str(product_capacity)], text=True, capture_output=True,
        check=False, env=environment, timeout=timeout,
    )
    if product_capacity == 1:
        concurrency = {"action": "not-required", "capacity": 1}
    elif concurrency_check.returncode == 0:
        concurrency = {"action": "reuse", "evidence": json.loads(concurrency_check.stdout)}
    else:
        concurrency = {"action": "apply", "plan": run_json(
            ["bash", str(kit), "provider-concurrency", "plan", "--sha", sha,
             "--capacity", str(product_capacity)], "provider concurrency preview",
            environment=environment, timeout=timeout,
        )}
    cli_check = subprocess.run(
        ["bash", str(kit), "provider-cli-pin", "check", "--sha", sha],
        text=True, capture_output=True, check=False, env=environment, timeout=timeout,
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
            "provider CLI preview", environment=environment, timeout=timeout,
        )}
    return concurrency, cli


def seal_plan(body: dict[str, Any]) -> dict[str, Any]:
    return {**body, "approval_sha256": digest(body)}


def valid_controller(value: Any, project: str) -> bool:
    planned = isinstance(value, dict) and "action" in value
    return isinstance(value, dict) and (
        value.get("status") == "not-applicable"
        and set(value) == {"platform", "status"}
        or value.get("status") != "not-applicable"
        and set(value) == {"label", "path", "platform", "sha256"} | (
            {"action", "previous_sha256"} if planned else set()
        )
        and value.get("platform") == "darwin"
        and value.get("label") == f"com.factory.controller.{project}"
        and Path(str(value.get("path", ""))) == account_home() / (
            f"Library/LaunchAgents/com.factory.controller.{project}.plist"
        )
        and DIGEST.fullmatch(str(value.get("sha256", ""))) is not None
        and (not planned or value.get("action") in {"apply", "reuse"})
        and (not planned or value.get("previous_sha256") is None
             or DIGEST.fullmatch(str(value.get("previous_sha256"))) is not None)
        and (not planned or (value["action"] == "reuse")
             == (value.get("previous_sha256") == value.get("sha256")))
    )


def valid_ticket_inventory(value: Any) -> bool:
    if not isinstance(value, list):
        return False
    tickets: set[str] = set()
    for item in value:
        if (
            not isinstance(item, dict) or set(item) != {"blob", "state", "ticket"}
            or not TICKET.fullmatch(str(item.get("ticket", "")))
            or not SHA.fullmatch(str(item.get("blob", "")))
            or item.get("state") not in TICKET_STATES
            or item["ticket"] in tickets
        ):
            return False
        tickets.add(item["ticket"])
    return True


def valid_host_cutover(value: Any) -> bool:
    if value is None:
        return True
    if not isinstance(value, list) or len(value) != len({
        item.get("project") for item in value if isinstance(item, dict)
    }):
        return False
    for item in value:
        if not isinstance(item, dict) or set(item) != {
            "controller", "incident", "maintenance", "product", "project",
            "receipt", "runtime", "source_active_sha256", "tickets",
        }:
            return False
        project = item.get("project")
        receipt = item.get("receipt")
        runtime = item.get("runtime")
        incident = item.get("incident")
        maintenance = item.get("maintenance")
        if (
            not isinstance(project, str) or not PROJECT.fullmatch(project)
            or not Path(str(item.get("product", ""))).is_absolute()
            or not DIGEST.fullmatch(str(item.get("source_active_sha256", "")))
            or not valid_ticket_inventory(item.get("tickets"))
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
            or not isinstance(maintenance, dict)
            or set(maintenance) != {"cutover_sha256", "prior", "reservation_id"}
            or not DIGEST.fullmatch(str(maintenance.get("cutover_sha256", "")))
            or not DIGEST.fullmatch(str(maintenance.get("reservation_id", "")))
            or maintenance.get("prior") is not None and (
                not isinstance(maintenance.get("prior"), dict)
                or set(maintenance["prior"]) != {"path", "sha256"}
                or not Path(str(maintenance["prior"].get("path", ""))).is_absolute()
                or not DIGEST.fullmatch(str(maintenance["prior"].get("sha256", "")))
            )
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


def valid_retired_runtime(value: Any) -> bool:
    if not isinstance(value, dict) or set(value) != {"action", "profile", "services"}:
        return False
    profile = value.get("profile")
    services = value.get("services")
    if (
        value.get("action") not in {"apply", "reuse"}
        or not isinstance(profile, dict)
        or set(profile) != {"path", "tree_sha256"}
        or not Path(str(profile.get("path", ""))).is_absolute()
        or profile.get("tree_sha256") is not None
        and not DIGEST.fullmatch(str(profile.get("tree_sha256")))
        or not isinstance(services, list) or len(services) != 2
    ):
        return False
    expected = {
        f"com.nysa.{_RETIRED_RUNTIME}-factory-gateway",
        f"com.nysa.{_RETIRED_RUNTIME}-dashboard",
    }
    labels: set[str] = set()
    for service in services:
        if (
            not isinstance(service, dict)
            or set(service) != {"label", "loaded", "path", "sha256"}
            or not isinstance(service.get("loaded"), bool)
            or not Path(str(service.get("path", ""))).is_absolute()
            or service.get("sha256") is not None
            and not DIGEST.fullmatch(str(service.get("sha256")))
        ):
            return False
        labels.add(str(service.get("label")))
    changed = profile["tree_sha256"] is not None or any(
        service["loaded"] or service["sha256"] is not None for service in services
    )
    return labels == expected and value["action"] == ("apply" if changed else "reuse")


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
        or value.get("status") != "authorized"
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
        "factory_tree", "maintenance_prior", "mode", "previous", "product_origin",
        "product_path", "product_sha", "product_tree", "runtime", "tickets",
    }
    request_keys = {
        "cli_paths", "migrations", "operator_id", "product", "profile", "project",
        "repo", "runtime_bin", "sha", "skip_optional_tests",
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
        or not isinstance(request.get("skip_optional_tests"), bool)
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
    if not valid_ticket_inventory(inventory):
        raise ReleaseError("release plan is invalid")
    previous = identity.get("previous")
    if previous is not None and (
        set(previous) != {"record", "sha256"}
        or not isinstance(previous.get("record"), dict)
        or not DIGEST.fullmatch(str(previous.get("sha256", "")))
    ):
        raise ReleaseError("release plan is invalid")
    maintenance_prior = identity.get("maintenance_prior")
    if maintenance_prior is not None and (
        not isinstance(maintenance_prior, dict)
        or set(maintenance_prior) != {"path", "sha256"}
        or not Path(str(maintenance_prior.get("path", ""))).is_absolute()
        or not DIGEST.fullmatch(str(maintenance_prior.get("sha256", "")))
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
        or
        not isinstance(provider_cli, dict)
        or provider_cli.get("action") not in {"apply", "reuse"}
        or not isinstance(provider_concurrency, dict)
        or provider_concurrency.get("action") not in {"apply", "reuse", "not-required"}
    ):
        raise ReleaseError("release plan is invalid")
    if launcher["action"] == "apply":
        validate_launcher_plan(launcher)
    else:
        validate_launcher_reuse(launcher)
    if project_launcher(launcher):
        expected_launcher = account_home() / ".factory/kits/releases" / (
            identity["factory_sha"]
        ) / "scripts/factory-launch"
        if (
            launcher_path(launcher) != expected_launcher
            or launcher.get("active_projects", []) != []
            or launcher["action"] == "apply" and (
                Path(launcher["candidate"]["path"]) != expected_launcher
                or launcher["previous_sha256"] != launcher["candidate"]["sha256"]
            )
        ):
            raise ReleaseError("release plan is invalid")
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
                "retired_runtime",
            }
            or not valid_host_cutover(children.get("host_cutover"))
            or not valid_retired_runtime(children.get("retired_runtime"))
            or "apply" not in {
                launcher["action"], provider_cli["action"], provider_concurrency["action"],
                children["retired_runtime"]["action"],
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


def current_plan(path: Path) -> dict[str, Any]:
    plan = safe_state(path, "current release plan")
    validate_plan(plan)
    immutable = path.parent / path.stem / f"{plan['approval_sha256']}.json"
    if (
        not immutable.exists() or immutable.is_symlink()
        or safe_state(immutable, "stored release plan") != plan
    ):
        raise ReleaseError("current release plan does not match its sealed copy")
    return plan


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


def retired_tree_digest(root: Path) -> str:
    secure_directory(root)
    entries: list[dict[str, Any]] = []
    for directory, names, files in os.walk(root, followlinks=False):
        current = Path(directory)
        info = current.lstat()
        if (
            current.is_symlink() or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o022
        ):
            raise ReleaseError("retired Factory profile is unsafe")
        names.sort()
        files.sort()
        entries.append({
            "kind": "directory", "mode": stat.S_IMODE(info.st_mode),
            "path": str(current.relative_to(root)),
        })
        for name in files:
            path = current / name
            entries.append({
                "kind": "file", "mode": stat.S_IMODE(path.stat().st_mode),
                "path": str(path.relative_to(root)),
                "sha256": secure_regular_digest(path, "retired Factory profile file"),
            })
        if len(entries) > 10_000:
            raise ReleaseError("retired Factory profile is too large")
    return digest(entries)


def service_loaded(label: str) -> bool:
    if sys.platform != "darwin" or os.environ.get("FACTORY_KIT_TEST_MODE") == "1":
        return False
    prefix, domain = service_prefix()
    return subprocess.run(
        prefix + ["print", f"{domain}/{label}"], stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, check=False, timeout=30,
    ).returncode == 0


def retired_runtime_plan() -> dict[str, Any]:
    home = account_home()
    profile = home / f".{_RETIRED_RUNTIME}/profiles/factory"
    profile_sha = retired_tree_digest(profile) if profile.exists() or profile.is_symlink() else None
    services = []
    for label in (
        f"com.nysa.{_RETIRED_RUNTIME}-dashboard",
        f"com.nysa.{_RETIRED_RUNTIME}-factory-gateway",
    ):
        path = home / "Library/LaunchAgents" / f"{label}.plist"
        sha256 = None
        if path.exists() or path.is_symlink():
            sha256 = hashlib.sha256(
                secure_regular_bytes(path, "retired Factory service job")
            ).hexdigest()
        services.append({
            "label": label, "loaded": service_loaded(label),
            "path": str(path), "sha256": sha256,
        })
    changed = profile_sha is not None or any(
        service["loaded"] or service["sha256"] is not None for service in services
    )
    value = {
        "action": "apply" if changed else "reuse",
        "profile": {"path": str(profile), "tree_sha256": profile_sha},
        "services": services,
    }
    if not valid_retired_runtime(value):
        raise ReleaseError("retired runtime plan is invalid")
    return value


def sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def retired_runtime_matches(
    value: dict[str, Any], approval_sha256: str, *, require_absent: bool = False,
    check_profile: bool = True,
) -> bool:
    if not valid_retired_runtime(value) or not DIGEST.fullmatch(approval_sha256):
        return False
    home = account_home()
    profile = Path(value["profile"]["path"])
    if profile != home / f".{_RETIRED_RUNTIME}/profiles/factory":
        return False
    archive = home / ".factory/retired-runtime" / approval_sha256
    profile_archive = archive / "profile"
    expected_profile = value["profile"]["tree_sha256"]
    if check_profile:
        if expected_profile is None:
            if profile.exists() or profile.is_symlink() or profile_archive.exists() or profile_archive.is_symlink():
                return False
        elif profile.exists() or profile.is_symlink():
            if require_absent or retired_tree_digest(profile) != expected_profile:
                return False
        elif not profile_archive.exists() or retired_tree_digest(profile_archive) != expected_profile:
            return False
    for service in value["services"]:
        label = service["label"]
        source = Path(service["path"])
        expected_path = home / "Library/LaunchAgents" / f"{label}.plist"
        target = archive / "services" / source.name
        if source != expected_path or label not in {
            f"com.nysa.{_RETIRED_RUNTIME}-dashboard",
            f"com.nysa.{_RETIRED_RUNTIME}-factory-gateway",
        }:
            return False
        expected_sha = service["sha256"]
        if expected_sha is None:
            if source.exists() or source.is_symlink() or target.exists() or target.is_symlink():
                return False
        elif source.exists() or source.is_symlink():
            if require_absent or hashlib.sha256(
                secure_regular_bytes(source, "retired Factory service job")
            ).hexdigest() != expected_sha:
                return False
        elif not target.exists() or hashlib.sha256(
            secure_regular_bytes(target, "archived Factory service job")
        ).hexdigest() != expected_sha:
            return False
        if (require_absent or not service["loaded"]) and service_loaded(label):
            return False
    return True


def archive_retired_runtime(value: dict[str, Any], approval_sha256: str) -> None:
    if not retired_runtime_matches(value, approval_sha256):
        raise ReleaseError("retired runtime changed after approval")
    home = account_home()
    archive = secure_directory(
        home / ".factory/retired-runtime" / approval_sha256, create=True,
    )
    services = secure_directory(archive / "services", create=True)
    for service in value["services"]:
        unload_service(service)
        source = Path(service["path"])
        target = services / source.name
        if service["sha256"] is not None and (source.exists() or source.is_symlink()):
            if target.exists() or target.is_symlink():
                raise ReleaseError("retired Factory service archive conflicts")
            os.replace(source, target)
            sync_directory(source.parent)
            sync_directory(target.parent)
    profile = Path(value["profile"]["path"])
    profile_archive = archive / "profile"
    if value["profile"]["tree_sha256"] is not None and (
        profile.exists() or profile.is_symlink()
    ):
        if profile_archive.exists() or profile_archive.is_symlink():
            raise ReleaseError("retired Factory profile archive conflicts")
        os.replace(profile, profile_archive)
        sync_directory(profile.parent)
        sync_directory(archive)
    if not retired_runtime_matches(value, approval_sha256, require_absent=True):
        raise ReleaseError("retired runtime removal did not converge")


def reservation_path(kits_root: Path) -> Path:
    return kits_root / "contract-cutover-reservation.json"


def reservation_basis(
    kits_root: Path, launcher: dict[str, Any], retired_runtime: dict[str, Any],
    product: Path, project: str, sha: str,
) -> dict[str, Any]:
    body = {
        "active_projects": launcher.get("active_projects", active_inventory(kits_root)),
        "candidate_launcher_sha256": (
            launcher["candidate"]["sha256"] if launcher["action"] == "apply"
            else launcher["sha256"]
        ),
        "project": project,
        "product": str(product),
        "retired_runtime_sha256": digest(retired_runtime),
        "schema": "nysa.software-factory.host-cutover-reservation/v1",
        "sha": sha,
    }
    return {**body, "reservation_id": digest(body)}


def read_reservation(path: Path) -> dict[str, Any]:
    value = safe_state(path, "host cutover reservation")
    body = {key: item for key, item in value.items() if key != "record_sha256"}
    basis_keys = {
        "active_projects", "candidate_launcher_sha256", "product", "project",
        "retired_runtime_sha256", "schema", "sha",
    }
    basis = {key: value.get(key) for key in basis_keys}
    active_projects = value.get("active_projects")
    if (
        set(value) != basis_keys | {
            "approval_sha256", "record_sha256", "reservation_id", "status",
        }
        or value.get("schema") != "nysa.software-factory.host-cutover-reservation/v1"
        or value.get("status") not in {"preparing", "prepared"}
        or value.get("reservation_id") != digest(basis)
        or value.get("status") == "preparing" and value.get("approval_sha256") is not None
        or value.get("status") == "prepared"
        and not DIGEST.fullmatch(str(value.get("approval_sha256", "")))
        or not isinstance(active_projects, list)
        or any(
            not isinstance(item, dict)
            or set(item) != {
                "active_sha256", "contract_version", "kit_sha", "product", "project",
            }
            or not DIGEST.fullmatch(str(item.get("active_sha256", "")))
            or not PROJECT.fullmatch(str(item.get("project", "")))
            or not Path(str(item.get("product", ""))).is_absolute()
            for item in active_projects
        )
        or not DIGEST.fullmatch(str(value.get("candidate_launcher_sha256", "")))
        or not DIGEST.fullmatch(str(value.get("retired_runtime_sha256", "")))
        or not PROJECT.fullmatch(str(value.get("project", "")))
        or not Path(str(value.get("product", ""))).is_absolute()
        or not SHA.fullmatch(str(value.get("sha", "")))
        or value.get("record_sha256") != digest(body)
    ):
        raise ReleaseError("host cutover reservation is invalid")
    return value


def reserve_cutover(kits_root: Path, basis: dict[str, Any]) -> dict[str, Any]:
    path = reservation_path(kits_root)
    if path.exists() or path.is_symlink():
        current = read_reservation(path)
        if current.get("reservation_id") != basis["reservation_id"]:
            raise ReleaseError("another host cutover is already reserved")
        if current["status"] == "prepared":
            raise ReleaseError("host cutover is prepared; resume its approved plan")
        return current
    value = {
        **basis, "approval_sha256": None, "status": "preparing",
    }
    atomic_json(path, {**value, "record_sha256": digest(value)})
    return value


def finalize_reservation(
    kits_root: Path, reservation_id: str, approval_sha256: str,
) -> None:
    path = reservation_path(kits_root)
    current = read_reservation(path)
    if (
        current["reservation_id"] != reservation_id
        or current["status"] != "preparing"
    ):
        raise ReleaseError("host cutover reservation changed")
    body = {
        **{key: item for key, item in current.items() if key != "record_sha256"},
        "approval_sha256": approval_sha256, "status": "prepared",
    }
    atomic_json(path, {**body, "record_sha256": digest(body)})


def require_reservation(kits_root: Path, plan: dict[str, Any]) -> None:
    path = reservation_path(kits_root)
    if not path.exists() or path.is_symlink():
        raise ReleaseError("host cutover reservation is missing")
    value = read_reservation(path)
    launcher = plan["children"]["launcher"]
    candidate_sha = (
        launcher["candidate"]["sha256"] if launcher["action"] == "apply"
        else launcher["sha256"]
    )
    expected_actives = {
        (item["project"], item["product"], item["source_active_sha256"])
        for item in plan["children"]["host_cutover"]
    }
    reserved_actives = {
        (item.get("project"), item.get("product"), item.get("active_sha256"))
        for item in value["active_projects"]
    }
    if (
        value["status"] != "prepared"
        or value["approval_sha256"] != plan["approval_sha256"]
        or plan["children"]["host_cutover"] and value.get("reservation_id") not in {
            item["maintenance"]["reservation_id"]
            for item in plan["children"]["host_cutover"]
        }
        or value.get("candidate_launcher_sha256") != candidate_sha
        or reserved_actives != expected_actives
        or value.get("project") != plan["request"]["project"]
        or value.get("product") != plan["request"].get("product")
        or value.get("retired_runtime_sha256")
        != digest(plan["children"]["retired_runtime"])
        or value.get("sha") != plan["request"]["sha"]
    ):
        raise ReleaseError("host cutover reservation does not match approval")


def clear_reservation(kits_root: Path, plan: dict[str, Any]) -> None:
    path = reservation_path(kits_root)
    if not path.exists() and not path.is_symlink():
        return
    require_reservation(kits_root, plan)
    path.unlink()
    sync_directory(path.parent)


def capture_maintenance(
    kits_root: Path, product: Path, project: str, reservation_id: str,
) -> dict[str, str] | None:
    marker = product / "factory/MAINTENANCE"
    if not marker.exists() and not marker.is_symlink():
        return None
    root = secure_directory(
        kits_root / "contract-cutover-reservations" / reservation_id, create=True,
    )
    snapshot = root / f"{project}.maintenance"
    current = safe_state(marker, "pre-cutover maintenance marker")
    if current.get("cutover_owner") == reservation_id:
        if not snapshot.exists() and not snapshot.is_symlink():
            return None
        raw = secure_regular_bytes(snapshot, "pre-cutover maintenance snapshot")
        return {"path": str(snapshot), "sha256": hashlib.sha256(raw).hexdigest()}
    raw = secure_regular_bytes(marker, "pre-cutover maintenance marker")
    exact_local_file(snapshot, raw, "pre-cutover maintenance snapshot")
    return {"path": str(snapshot), "sha256": hashlib.sha256(raw).hexdigest()}


def snapshot_maintenance(kits_root: Path, product: Path, project: str) -> dict[str, str] | None:
    marker = product / "factory/MAINTENANCE"
    if not marker.exists() and not marker.is_symlink():
        return None
    raw = secure_regular_bytes(marker, "pre-release maintenance marker")
    sha256 = hashlib.sha256(raw).hexdigest()
    root = secure_directory(
        kits_root / "projects" / project / "release-plans" / "maintenance", create=True,
    )
    snapshot = root / f"{sha256}.marker"
    exact_local_file(snapshot, raw, "pre-release maintenance snapshot")
    return {"path": str(snapshot), "sha256": sha256}


def prepare_host_cutover(
    release: Path, kits_root: Path, sha: str, launcher: dict[str, Any],
    explicit_runtime: Path | None, target_project: str,
    target_product: Path, target_runtime: dict[str, Any],
    target_controller: dict[str, Any], reservation_id: str,
    skip_optional_tests: bool,
) -> list[dict[str, Any]]:
    kit = release / "scripts/factory-kit.sh"
    entries: list[dict[str, Any]] = []
    sources = launcher.get("active_projects", active_inventory(kits_root))
    if launcher["action"] != "apply" and any(
        int(item["contract_version"].split(".", 1)[0]) < 2 for item in sources
    ):
        raise ReleaseError(
            "retired runtime removal requires every active project on Contract 2"
        )
    for source in sources:
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
        environment = command_environment(
            kits_root, Path(runtime["evidence"]["path"]), cutover_lock=True,
        )
        environment["FACTORY_CONTRACT_2_CUTOVER"] = "1"
        environment["FACTORY_HOST_CUTOVER_RESERVATION"] = reservation_id
        environment["FACTORY_MAINTENANCE_OWNER"] = reservation_id
        prior_maintenance = capture_maintenance(
            kits_root, product, project, reservation_id,
        )
        run(
            ["bash", str(kit), "pause", "--project", project,
             "--product", str(product)], f"host cutover drain for {project}",
            environment=environment,
        )
        maintenance = product / "factory/MAINTENANCE"
        cutover_maintenance_sha = hashlib.sha256(
            secure_regular_bytes(maintenance, "host cutover maintenance marker")
        ).hexdigest()
        if launcher["action"] == "apply":
            certify = [
                "bash", str(kit), "certify", "--project", project,
                "--product", str(product), "--sha", sha,
            ]
            if (
                skip_optional_tests
                and project == target_project and product == target_product
            ):
                certify.append("--skip-optional-tests")
            run(
                certify,
                f"host cutover certification for {project}", environment=environment,
            )
            receipt_path, receipt = find_receipt(kits_root, project, sha)
            preview_environment = command_environment(
                kits_root, Path(runtime["evidence"]["path"]),
            )
            run(
                ["bash", str(kit), "plan", "--project", project,
                 "--product", str(product), "--sha", sha,
                 "--receipt", str(receipt_path)],
                f"host cutover activation preview for {project}",
                environment=preview_environment,
            )
        else:
            active_record = safe_state(
                kits_root / "projects" / project / "active.json", "active release",
            )
            receipt_id = active_record.get("receipt_id")
            if not DIGEST.fullmatch(str(receipt_id)):
                raise ReleaseError("active Contract 2 receipt identity is invalid")
            receipt_path = kits_root / "receipts" / f"{receipt_id}.json"
            receipt = safe_state(receipt_path, "certification receipt")
            if receipt.get("receipt_id") != receipt_id:
                raise ReleaseError("active Contract 2 receipt changed")
        entries.append({
            "controller": controller,
            "incident": incident_identity(project),
            "maintenance": {
                "cutover_sha256": cutover_maintenance_sha,
                "prior": prior_maintenance, "reservation_id": reservation_id,
            },
            "product": str(product),
            "project": project,
            "receipt": {
                "path": str(receipt_path), "receipt_id": receipt["receipt_id"],
                "sha256": file_digest(receipt_path),
            },
            "runtime": runtime,
            "source_active_sha256": source["active_sha256"],
            "tickets": ticket_inventory(product),
        })
    return entries


def _setup_locked(args: argparse.Namespace) -> dict[str, Any]:
    project = args.project
    sha = args.sha
    if not PROJECT.fullmatch(project) or not SHA.fullmatch(sha):
        raise ReleaseError("release identity is invalid")
    product = args.product.resolve(strict=True)
    repo = args.repo.resolve(strict=True)
    kits_root = args.kits_root.resolve()
    secure_directory(kits_root, create=True)
    require_test_layout(kits_root)
    pending_reservation = reservation_path(kits_root)
    if pending_reservation.exists() or pending_reservation.is_symlink():
        pending = read_reservation(pending_reservation)
        if (
            pending["status"] == "prepared"
            or pending.get("project") != project
            or pending.get("product") != str(product)
            or pending.get("sha") != sha
        ):
            raise ReleaseError("another host cutover is already reserved")
    factory_sha, factory_tree, factory_origin = clean_identity(repo, "Factory candidate")
    product_sha, product_tree, product_origin = clean_identity(product, "product")
    if factory_sha != sha:
        raise ReleaseError("Factory candidate does not match release SHA")
    if (product / "factory/KIT_PIN").read_text(encoding="utf-8") != sha + "\n":
        raise ReleaseError("product pin does not match release SHA")
    validate_product_runtime_contract(product)
    validate_optional_test_request(product, args.skip_optional_tests)
    source_kit = repo / "scripts/factory-kit.sh"
    environment = command_environment(kits_root)
    environment.pop("FACTORY_KIT_CERTIFICATION_NETWORK_REVIEWED", None)
    run(
        ["bash", str(source_kit), "install", "--sha", sha, "--repo", str(repo)],
        "sealed release installation", environment=environment,
    )
    release = kits_root / "releases" / sha
    sealed_kit = release / "scripts/factory-kit.sh"
    release_contract = contract(release)
    active = kits_root / "projects" / project / "active.json"
    previous = None
    if active.exists() or active.is_symlink():
        active_value = safe_state(active, "active release")
        if active_value.get("product_path") != str(product):
            raise ReleaseError("active release belongs to a different product")
        previous = {"record": active_value, "sha256": file_digest(active)}
    runtime = prepare_runtime(release, product, kits_root, project, args.runtime_bin)
    runtime_bin = Path(str(runtime["evidence"]["path"]))
    release_preflight(sealed_kit, kits_root, runtime_bin, project, product, sha)
    prepare_product_runtime(product)
    if clean_identity(product, "product") != (
        product_sha, product_tree, product_origin,
    ):
        raise ReleaseError("product changed during runtime preparation")
    mode = "upgrade" if previous is not None else "new"
    maintenance_prior = (
        args.maintenance_prior if hasattr(args, "maintenance_prior")
        else snapshot_maintenance(kits_root, product, project)
    )
    launcher = launcher_plan(release, kits_root, project)
    launcher_path = Path(
        launcher["target"] if launcher["action"] == "apply" else launcher["path"]
    )
    controller = prepare_controller(project, product, launcher_path)
    retired_runtime = retired_runtime_plan()
    cli_paths = {
        key: str(value.resolve(strict=True)) for key, value in {
            "claude": args.claude_bin, "codex": args.codex_bin,
            "cursor": args.cursor_bin,
        }.items() if value is not None
    }
    concurrency, cli = child_plan(
        sealed_kit, kits_root, sha, capacity(product), cli_paths, args.operator_id,
    )
    scoped_launcher = project_launcher(launcher)
    preparation_required = (
        concurrency["action"] == "apply" or cli["action"] == "apply"
        or (scoped_launcher and launcher["action"] == "apply")
        or (not scoped_launcher and any(
            service["loaded"] for service in retired_runtime["services"]
        ))
    )
    host_cutover = None
    reservation = None
    global_change = not scoped_launcher and (
        launcher["action"] == "apply" or retired_runtime["action"] == "apply"
    )
    if global_change and not preparation_required:
        basis = reservation_basis(
            kits_root, launcher, retired_runtime, product, project, sha,
        )
        reservation = reserve_cutover(kits_root, basis)
        host_cutover = prepare_host_cutover(
            release, kits_root, sha, launcher, args.runtime_bin, project, product,
            runtime, controller, basis["reservation_id"],
            args.skip_optional_tests,
        )
    elif not preparation_required and mode == "upgrade":
        run(
            ["bash", str(sealed_kit), "pause", "--project", project,
             "--product", str(product)], "release maintenance entry",
            environment=command_environment(
                kits_root, runtime_bin, cutover_lock=True,
            ),
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
        "skip_optional_tests": args.skip_optional_tests,
    }
    identity = {
        "capacity": capacity(product), "contract_version": release_contract,
        "controller": controller,
        "factory_origin": factory_origin, "factory_sha": sha,
        "factory_tree": factory_tree,
        "maintenance_prior": maintenance_prior, "mode": mode,
        "previous": previous,
        "product_origin": product_origin, "product_path": str(product),
        "product_sha": product_sha, "product_tree": product_tree,
        "runtime": runtime, "tickets": ticket_inventory(product),
    }
    now = int(time.time())
    if preparation_required or global_change:
        plan = seal_plan({
            "children": {
                "host_cutover": host_cutover,
                "launcher": launcher, "provider_cli": cli,
                "provider_concurrency": concurrency,
                "retired_runtime": retired_runtime,
            },
            "created_epoch": now, "expires_epoch": now + 7200,
            "identity": identity, "request": request, "schema": PLAN_SCHEMA,
            "stage": "prerequisites", "status": "authorized",
        })
    else:
        certification_environment = command_environment(
            kits_root, runtime_bin, cutover_lock=True,
        )
        certify = [
            "bash", str(sealed_kit), "certify", "--project", project,
            "--product", str(product), "--sha", sha,
        ]
        if args.skip_optional_tests:
            certify.append("--skip-optional-tests")
        run(
            certify, "product certification",
            environment=certification_environment,
        )
        run(
            ["bash", str(sealed_kit), "pause", "--project", project,
             "--product", str(product)], "release maintenance entry",
            environment=certification_environment,
        )
        receipt_path, receipt = find_receipt(kits_root, project, sha)
        preview_environment = command_environment(kits_root, runtime_bin)
        run(
            ["bash", str(sealed_kit), "plan", "--project", project,
             "--product", str(product), "--sha", sha, "--receipt", str(receipt_path)],
            "activation preview", environment=preview_environment,
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
            "stage": "activation", "status": "authorized",
        })
    path, _ = plan_paths(kits_root, project, sha)
    write_plan(path, plan)
    if reservation is not None:
        finalize_reservation(
            kits_root, reservation["reservation_id"], plan["approval_sha256"],
        )
    return plan


def clear_preparing_reservation(kits_root: Path, reservation: dict[str, Any]) -> None:
    expected = {
        (
            item["project"], item["product"], item["active_sha256"],
            item["contract_version"], item["kit_sha"],
        )
        for item in reservation["active_projects"]
    }
    current = {
        (
            item["project"], item["product"], item["active_sha256"],
            item["contract_version"], item["kit_sha"],
        )
        for item in active_inventory(kits_root)
    }
    if current != expected:
        raise ReleaseError("failed host preparation changed active project state")
    items = []
    for source in reservation["active_projects"]:
        marker = Path(source["product"]) / "factory/MAINTENANCE"
        if not marker.exists() and not marker.is_symlink():
            continue
        value = safe_state(marker, "host cutover maintenance marker")
        if value.get("cutover_owner") != reservation["reservation_id"]:
            continue
        raw = secure_regular_bytes(marker, "host cutover maintenance marker")
        snapshot = (
            kits_root / "contract-cutover-reservations"
            / reservation["reservation_id"] / f"{source['project']}.maintenance"
        )
        prior = None
        if snapshot.exists() or snapshot.is_symlink():
            prior_raw = secure_regular_bytes(
                snapshot, "pre-cutover maintenance snapshot",
            )
            prior = {
                "path": str(snapshot),
                "sha256": hashlib.sha256(prior_raw).hexdigest(),
            }
        items.append({
            "maintenance": {
                "cutover_sha256": hashlib.sha256(raw).hexdigest(),
                "prior": prior,
                "reservation_id": reservation["reservation_id"],
            },
            "product": source["product"],
            "project": source["project"],
        })
    for item in items:
        cutover_maintenance_restore(item)
    for item in items:
        clear_cutover_maintenance(item)
    path = reservation_path(kits_root)
    current_reservation = read_reservation(path)
    if (
        current_reservation["status"] != "preparing"
        or current_reservation["reservation_id"] != reservation["reservation_id"]
    ):
        raise ReleaseError("failed host preparation reservation changed")
    path.unlink()
    sync_directory(path.parent)


def setup(args: argparse.Namespace) -> dict[str, Any]:
    kits_root = args.kits_root.resolve()
    descriptor = acquire_cutover_lock(kits_root) if _CUTOVER_LOCK_FD is None else None
    try:
        try:
            return _setup_locked(args)
        except Exception:
            path = reservation_path(kits_root)
            if path.exists() or path.is_symlink():
                reservation = read_reservation(path)
                if (
                    reservation["status"] == "preparing"
                    and reservation["project"] == args.project
                    and reservation["product"] == str(args.product.resolve(strict=True))
                    and reservation["sha"] == args.sha
                ):
                    try:
                        clear_preparing_reservation(kits_root, reservation)
                    except Exception as cleanup_error:
                        raise ReleaseError(
                            "failed host preparation could not be restored"
                        ) from cleanup_error
            raise
    finally:
        if descriptor is not None:
            release_cutover_lock(descriptor)


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
        maintenance_prior=next((
            item["maintenance"]["prior"]
            for item in plan["children"].get("host_cutover") or []
            if item["project"] == request["project"]
        ), plan["identity"]["maintenance_prior"]),
        ticket_workdir=migrations,
        skip_optional_tests=request["skip_optional_tests"],
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
        stderr=subprocess.DEVNULL, check=False, timeout=5,
    )
    if current.returncode == 0:
        run(
            prefix + ["bootout", service], f"service unload for {value['label']}",
            timeout=5,
        )
    deadline = time.monotonic() + 5
    while (remaining := deadline - time.monotonic()) > 0:
        try:
            current = subprocess.run(
                prefix + ["print", service], stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, check=False,
                timeout=max(0.001, min(1, remaining)),
            )
        except subprocess.TimeoutExpired:
            continue
        if current.returncode != 0:
            return
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(0.1, remaining))
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
    floor_required = completed_cutover_exists(path) or phase in {
        "active_records_switched", "contract_floor_committed", "launcher_installed",
        "operator_initialized", "retired_runtime_removed", "healthy",
    }
    if path.exists() or path.is_symlink():
        current = safe_state(path, "host cutover journal")
        floor_required = floor_required or current.get("floor_required") is True
    body = {
        "approval_sha256": approval_sha256,
        "completed_projects": projects,
        "floor_required": floor_required,
        "phase": phase,
        "schema": "nysa.software-factory.host-cutover-journal/v1",
        "status": status,
    }
    atomic_json(path, {**body, "record_sha256": digest(body)})
    if os.environ.get("FACTORY_RELEASE_FAIL_AFTER_CUTOVER_PHASE") == phase:
        raise ReleaseError(f"injected failure after host cutover phase {phase}")


def read_cutover(path: Path, approval_sha256: str) -> dict[str, Any]:
    if not path.exists() and not path.is_symlink():
        return {
            "completed_projects": [], "floor_required": False,
            "phase": "approved", "status": "in-progress",
        }
    value = safe_state(path, "host cutover journal")
    body = {key: item for key, item in value.items() if key != "record_sha256"}
    if (
        value.get("schema") != "nysa.software-factory.host-cutover-journal/v1"
        or not DIGEST.fullmatch(str(value.get("approval_sha256", "")))
        or value.get("record_sha256") != digest(body)
        or value.get("status") not in {"in-progress", "pass"}
        or not isinstance(value.get("completed_projects"), list)
        or not isinstance(value.get("floor_required"), bool)
    ):
        raise ReleaseError("host cutover journal is invalid")
    if value["approval_sha256"] != approval_sha256:
        if value["status"] == "pass":
            return {
                "completed_projects": [], "floor_required": True,
                "phase": "approved", "status": "in-progress",
            }
        raise ReleaseError("host cutover journal is invalid")
    return value


def completed_cutover_exists(path: Path) -> bool:
    if not path.exists() and not path.is_symlink():
        return False
    value = safe_state(path, "host cutover journal")
    approval = str(value.get("approval_sha256", ""))
    if not DIGEST.fullmatch(approval):
        raise ReleaseError("host cutover journal is invalid")
    return read_cutover(path, approval)["status"] == "pass"


def cutover_terminal_exact(
    kits_root: Path, item: dict[str, Any], sha: str, release: Path,
) -> bool:
    path = kits_root / "projects" / item["project"] / "active.json"
    if not path.exists() or path.is_symlink():
        return False
    record = safe_state(path, "active release")
    receipt_path = Path(item["receipt"]["path"])
    if (
        not receipt_path.exists() or receipt_path.is_symlink()
        or file_digest(receipt_path) != item["receipt"]["sha256"]
    ):
        return False
    receipt = safe_state(receipt_path, "certification receipt")
    expected = {
        "contract_version": "2.0.0", "kit_sha": sha,
        "kit_tree": receipt.get("kit_tree"), "product_path": item["product"],
        "product_sha": receipt.get("product_sha"),
        "product_tree": receipt.get("product_tree"), "project": item["project"],
        "receipt_id": item["receipt"]["receipt_id"], "release_path": str(release),
    }
    if receipt.get("runtime_tuple") is not None:
        expected["runtime_tuple"] = receipt["runtime_tuple"]
    generation = record.get("generation")
    if (
        receipt.get("receipt_id") != item["receipt"]["receipt_id"]
        or not isinstance(generation, int) or isinstance(generation, bool)
        or generation < 1
        or any(record.get(key) != value for key, value in expected.items())
    ):
        return False
    journals = kits_root / "projects" / item["project"] / "activation-journal"
    matches = sorted(journals.glob(f"{generation:020d}-*.json"))
    if len(matches) != 1 or matches[0].is_symlink():
        return False
    for candidate in journals.glob("*.json"):
        if candidate.is_symlink() or safe_state(
            candidate, "activation journal",
        ).get("phase") not in {"committed", "rolled_back"}:
            return False
    journal = safe_state(matches[0], "activation journal")
    consumed = kits_root / "receipts/consumed" / f"{item['receipt']['receipt_id']}.json"
    if not consumed.exists() or consumed.is_symlink():
        return False
    claim = safe_state(consumed, "receipt consumption")
    return (
        journal.get("phase") == "committed"
        and journal.get("candidate_record") == record
        and journal.get("receipt_snapshot") == receipt
        and journal.get("receipt_hash") == item["receipt"]["sha256"]
        and claim.get("receipt_id") == item["receipt"]["receipt_id"]
        and claim.get("transaction_id") == journal.get("transaction_id")
    )


def ensure_contract_floor(kits_root: Path) -> None:
    floor = kits_root / "contract-floor.json"
    expected = {
        "minimum_major": 2,
        "schema": "nysa.software-factory.contract-floor/v1",
    }
    if floor.exists() or floor.is_symlink():
        if safe_state(floor, "contract floor") != expected:
            raise ReleaseError("contract floor changed")
    else:
        atomic_json(floor, expected)


def cutover_maintenance_restore(item: dict[str, Any]) -> bytes | None:
    marker = Path(item["product"]) / "factory/MAINTENANCE"
    maintenance = item["maintenance"]
    prior = maintenance["prior"]
    if prior is not None:
        snapshot = Path(prior["path"])
        raw = secure_regular_bytes(snapshot, "pre-cutover maintenance snapshot")
        if hashlib.sha256(raw).hexdigest() != prior["sha256"]:
            raise ReleaseError("pre-cutover maintenance snapshot changed")
        if marker.exists() and not marker.is_symlink() and hashlib.sha256(
            secure_regular_bytes(marker, "maintenance marker")
        ).hexdigest() == prior["sha256"]:
            return raw
    elif not marker.exists() and not marker.is_symlink():
        return None
    if (
        not marker.exists() or marker.is_symlink()
        or hashlib.sha256(secure_regular_bytes(
            marker, "maintenance marker",
        )).hexdigest() != maintenance["cutover_sha256"]
    ):
        raise ReleaseError("host cutover maintenance marker changed")
    value = safe_state(marker, "maintenance marker")
    if (
        value.get("project") != item["project"]
        or value.get("product_path") != item["product"]
        or value.get("cutover_owner") != maintenance["reservation_id"]
    ):
        raise ReleaseError("host cutover maintenance marker changed")
    return raw if prior is not None else None


def clear_cutover_maintenance(item: dict[str, Any]) -> None:
    marker = Path(item["product"]) / "factory/MAINTENANCE"
    raw = cutover_maintenance_restore(item)
    prior = item["maintenance"]["prior"]
    if prior is None and not marker.exists() and not marker.is_symlink():
        return
    if prior is not None and marker.exists() and not marker.is_symlink() and hashlib.sha256(
        secure_regular_bytes(marker, "maintenance marker")
    ).hexdigest() == prior["sha256"]:
        return
    if prior is None:
        marker.unlink()
        sync_directory(marker.parent)
    else:
        atomic_bytes(marker, raw)


def validate_host_item_basis(
    item: dict[str, Any], release: Path, kits_root: Path,
) -> None:
    product = Path(item["product"])
    if ticket_inventory(product) != item["tickets"]:
        raise ReleaseError(f"host cutover ticket inventory changed for {item['project']}")
    root = project_runtime_root(kits_root, item["project"])
    target = root / "bin"
    if Path(item["runtime"]["evidence"]["path"]) != target:
        raise ReleaseError(f"host cutover runtime path changed for {item['project']}")
    try:
        journal = safe_state(root / "runtime-pin-journal.json", "runtime pin journal")
    except (OSError, ReleaseError) as error:
        raise ReleaseError(
            f"host cutover runtime changed for {item['project']}"
        ) from error
    runtime_plan = journal.get("plan")
    if (
        journal.get("status") != "completed" or not isinstance(runtime_plan, dict)
        or runtime_plan.get("approval_sha256") != item["runtime"]["plan_sha256"]
        or runtime_plan.get("product_path") != item["product"]
        or runtime_plan.get("target_bin") != str(target)
    ):
        raise ReleaseError(f"host cutover runtime changed for {item['project']}")
    evidence = run_json(
        [sys.executable, "-I", "-S", str(release / "scripts/owner-runtime-pin.py"),
         "check", "--journal", str(root / "runtime-pin-journal.json")],
        f"host cutover runtime check for {item['project']}",
        environment=command_environment(kits_root),
    )
    if evidence.get("status") != "ready" or evidence.get("path") != str(target):
        raise ReleaseError(f"host cutover runtime evidence is invalid for {item['project']}")


def validate_host_runtime(
    plan: dict[str, Any], release: Path, kits_root: Path, *, require_retired: bool,
) -> None:
    items = plan["children"]["host_cutover"]
    sha = plan["request"]["sha"]
    if any(not cutover_terminal_exact(kits_root, item, sha, release) for item in items):
        raise ReleaseError("completed host cutover activation is not terminal")
    launcher_plan_value = plan["children"]["launcher"]
    launcher_path = Path(
        launcher_plan_value["target"] if launcher_plan_value["action"] == "apply"
        else launcher_plan_value["path"]
    )
    launcher_sha = (
        launcher_plan_value["candidate"]["sha256"]
        if launcher_plan_value["action"] == "apply" else launcher_plan_value["sha256"]
    )
    if hashlib.sha256(secure_regular_bytes(
        launcher_path, "installed launcher", executable=True,
    )).hexdigest() != launcher_sha:
        raise ReleaseError("completed host cutover launcher changed")
    human = launcher_plan_value.get("human_cli")
    if human is not None:
        human_path = Path(
            human["target"] if launcher_plan_value["action"] == "apply"
            else human["path"]
        )
        human_sha = (
            human["candidate"]["sha256"]
            if launcher_plan_value["action"] == "apply"
            and human["candidate"] is not None else human.get("sha256")
        )
        if human_sha is None:
            if human_path.exists() or human_path.is_symlink():
                raise ReleaseError("completed host cutover human CLI changed")
        elif hashlib.sha256(secure_regular_bytes(
            human_path, "installed human CLI", executable=True,
        )).hexdigest() != human_sha:
            raise ReleaseError("completed host cutover human CLI changed")
    ensure_contract_floor(kits_root)
    retired = plan["children"]["retired_runtime"]
    if require_retired and not retired_runtime_matches(
        retired, plan["approval_sha256"], require_absent=True,
    ):
        raise ReleaseError("retired runtime removal is incomplete")
    for item in items:
        validate_host_item_basis(item, release, kits_root)
        if not operator_inventory_ready(Path(item["product"]), item["tickets"]):
            raise ReleaseError(
                f"host cutover operator inventory changed for {item['project']}"
            )
        ensure_service(item["controller"])
        ensure_service(item["incident"])
        doctor = run_json(
            [str(launcher_path), item["project"], "doctor", "--json"],
            f"host cutover Doctor for {item['project']}",
            environment=launcher_environment(
                kits_root, Path(item["runtime"]["evidence"]["path"]),
            ),
        )
        checks = doctor.get("checks")
        identity_matches = (
            doctor.get("schema") == "nysa.software-factory.doctor/v2"
            and doctor.get("schema_version") == 2
            and doctor.get("contract_version") == "2.0.0"
            and doctor.get("project") == item["project"]
            and isinstance(checks, dict)
        )
        all_ok = identity_matches and doctor.get("overall_status") == "ok" and all(
            isinstance(check, dict) and check.get("status") == "ok"
            for check in checks.values()
        )
        runtime = checks.get("runtime", {}) if isinstance(checks, dict) else {}
        maintenance_only = (
            identity_matches
            and doctor.get("overall_status") == "warning"
            and isinstance(runtime, dict)
            and runtime.get("status") == "warning"
            and runtime.get("maintenance") is True
            and runtime.get("provider_lock_state") == "absent"
            and isinstance(runtime.get("locks"), dict)
            and not any(runtime["locks"].values())
            and all(runtime.get(key) == 0 for key in (
                "run_records", "active_runs", "stale_runs", "malformed_runs",
                "active_run_claims", "malformed_active_run_claims",
                "dispatch_lease_records", "stale_dispatch_leases",
                "malformed_dispatch_leases",
            ))
            and runtime.get("active_run_tickets") == []
            and all(
                name == "runtime" or isinstance(check, dict)
                and check.get("status") == "ok"
                for name, check in checks.items()
            )
        )
        if not (all_ok or maintenance_only):
            raise ReleaseError(f"host cutover Doctor did not pass for {item['project']}")


def clear_host_maintenance(plan: dict[str, Any]) -> None:
    for item in plan["children"]["host_cutover"]:
        if item["project"] != plan["request"]["project"]:
            clear_cutover_maintenance(item)


def _apply_host_cutover_locked(
    plan: dict[str, Any], release: Path, kits_root: Path,
) -> None:
    items = plan["children"]["host_cutover"]
    journal_path = kits_root / "contract-cutover-journal.json"
    if completed_cutover_exists(journal_path):
        ensure_contract_floor(kits_root)
    journal = read_cutover(journal_path, plan["approval_sha256"])
    if journal.get("phase") in {
        "active_records_switched", "contract_floor_committed", "launcher_installed",
        "operator_initialized", "retired_runtime_removed", "healthy",
    }:
        ensure_contract_floor(kits_root)
    if journal["status"] == "pass":
        validate_host_runtime(plan, release, kits_root, require_retired=True)
        clear_host_maintenance(plan)
        clear_reservation(kits_root, plan)
        return
    require_reservation(kits_root, plan)
    completed = list(journal["completed_projects"])
    kit = release / "scripts/factory-kit.sh"
    for item in items:
        validate_host_item_basis(item, release, kits_root)
    for item in items:
        if item["project"] in completed or cutover_terminal_exact(
            kits_root, item, plan["request"]["sha"], release,
        ):
            continue
        active = kits_root / "projects" / item["project"] / "active.json"
        claim = kits_root / "receipts/consumed" / f"{item['receipt']['receipt_id']}.json"
        if file_digest(active) != item["source_active_sha256"] or claim.exists() or claim.is_symlink():
            raise ReleaseError("host cutover basis changed before activation")
    for item in items:
        project = item["project"]
        if project in completed:
            if not cutover_terminal_exact(kits_root, item, plan["request"]["sha"], release):
                raise ReleaseError("completed host cutover project changed")
            continue
        environment = command_environment(
            kits_root, Path(item["runtime"]["evidence"]["path"]),
            cutover_lock=True,
        )
        environment["FACTORY_CONTRACT_2_CUTOVER"] = "1"
        environment["FACTORY_HOST_CUTOVER_RESERVATION"] = item["maintenance"][
            "reservation_id"
        ]
        run(
            ["bash", str(kit), "reconcile", "--project", project,
             "--product", item["product"]],
            f"host cutover activation reconcile for {project}", environment=environment,
        )
        if cutover_terminal_exact(kits_root, item, plan["request"]["sha"], release):
            completed.append(project)
            cutover_update(
                journal_path, plan["approval_sha256"], f"project:{project}",
                completed, "in-progress",
            )
            continue
        active = kits_root / "projects" / project / "active.json"
        if file_digest(active) != item["source_active_sha256"]:
            raise ReleaseError("host cutover active basis changed")
        claim = kits_root / "receipts/consumed" / f"{item['receipt']['receipt_id']}.json"
        if claim.exists() or claim.is_symlink():
            raise ReleaseError("host cutover reconciliation requires fresh certification")
        unload_service(item["controller"])
        unload_service(item["incident"])
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
        if not cutover_terminal_exact(kits_root, item, plan["request"]["sha"], release):
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
    ensure_contract_floor(kits_root)
    cutover_update(
        journal_path, plan["approval_sha256"], "contract_floor_committed",
        completed, "in-progress",
    )
    if plan["children"]["launcher"]["action"] == "apply":
        apply_launcher_plan(
            plan["children"]["launcher"], release, kits_root, plan["request"]["sha"],
        )
    cutover_update(
        journal_path, plan["approval_sha256"], "launcher_installed",
        completed, "in-progress",
    )
    initialize_host_operator_maps(release, kits_root, items)
    cutover_update(
        journal_path, plan["approval_sha256"], "operator_initialized",
        completed, "in-progress",
    )
    validate_host_runtime(plan, release, kits_root, require_retired=False)
    archive_retired_runtime(
        plan["children"]["retired_runtime"], plan["approval_sha256"],
    )
    cutover_update(
        journal_path, plan["approval_sha256"], "retired_runtime_removed",
        completed, "in-progress",
    )
    validate_host_runtime(plan, release, kits_root, require_retired=True)
    clear_host_maintenance(plan)
    cutover_update(
        journal_path, plan["approval_sha256"], "healthy", completed, "pass",
    )
    clear_reservation(kits_root, plan)


def apply_host_cutover(
    plan: dict[str, Any], release: Path, kits_root: Path,
) -> None:
    items = plan["children"]["host_cutover"]
    if (
        not valid_host_cutover(items)
        or not valid_retired_runtime(plan["children"].get("retired_runtime"))
    ):
        raise ReleaseError("host cutover plan is invalid")
    descriptor = acquire_cutover_lock(kits_root) if _CUTOVER_LOCK_FD is None else None
    try:
        _apply_host_cutover_locked(plan, release, kits_root)
    finally:
        if descriptor is not None:
            release_cutover_lock(descriptor)


def apply_prerequisites(plan: dict[str, Any], kits_root: Path, approved_by: str) -> dict[str, Any]:
    request = plan["request"]
    release = kits_root / "releases" / request["sha"]
    kit = release / "scripts/factory-kit.sh"
    environment = command_environment(kits_root, cutover_lock=True)
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
    retired_runtime = plan["children"]["retired_runtime"]
    launcher = plan["children"]["launcher"]
    scoped_launcher = project_launcher(launcher)
    if scoped_launcher and launcher["action"] == "apply":
        apply_launcher_plan(launcher, release, kits_root, request["sha"])
    if not scoped_launcher and any(
        service["loaded"] for service in retired_runtime["services"]
    ):
        if not retired_runtime_matches(
            retired_runtime, plan["approval_sha256"], check_profile=False,
        ):
            raise ReleaseError("retired runtime services changed after approval")
        for service in retired_runtime["services"]:
            unload_service(service)
    host_cutover = plan["children"]["host_cutover"]
    if host_cutover is not None and (
        launcher["action"] == "apply" or retired_runtime["action"] == "apply"
    ):
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


def validate_live_basis(
    kits_root: Path, plan: dict[str, Any], *, require_idle_dispatch: bool = True,
) -> None:
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
    validate_product_runtime_contract(
        product, require_idle_dispatch=require_idle_dispatch,
    )
    release = kits_root / "releases" / identity["factory_sha"]
    if contract(release) != identity["contract_version"]:
        raise ReleaseError("installed release changed after setup")
    launcher = plan["children"]["launcher"]
    installed_launcher = launcher_path(launcher)
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
    human = launcher.get("human_cli")
    if human is not None and not project_launcher(launcher):
        installed_human = account_home() / ".factory/bin/factory"
        if launcher["action"] == "reuse":
            allowed_humans = {human["sha256"]}
        else:
            desired_human = (
                human["candidate"]["sha256"] if human["candidate"] else None
            )
            allowed_humans = {desired_human}
            if plan["stage"] == "prerequisites":
                allowed_humans.add(human["previous_sha256"])
        observed_human = None
        if installed_human.exists() or installed_human.is_symlink():
            observed_human = hashlib.sha256(secure_regular_bytes(
                installed_human, "installed human CLI", executable=True,
            )).hexdigest()
        if observed_human not in allowed_humans:
            raise ReleaseError("installed human CLI changed after setup")
    controller = identity["controller"]
    if controller.get("status") != "not-applicable":
        expected_controller = controller_payload(
            plan["request"]["project"], product, installed_launcher,
        )
        path = Path(controller["path"])
        observed = None
        if path.exists() or path.is_symlink():
            observed = hashlib.sha256(
                secure_regular_bytes(path, "controller job")
            ).hexdigest()
        allowed = {controller["sha256"]}
        if controller.get("action") == "apply":
            allowed.add(controller.get("previous_sha256"))
        if hashlib.sha256(expected_controller).hexdigest() != controller["sha256"] or (
            observed not in allowed
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
    if plan["stage"] == "prerequisites":
        retired_runtime = plan["children"]["retired_runtime"]
        if not retired_runtime_matches(
            retired_runtime, plan["approval_sha256"], check_profile=not any(
                service["loaded"] for service in retired_runtime["services"]
            ),
        ):
            raise ReleaseError("retired runtime changed after release setup")
    prior = identity["maintenance_prior"]
    if prior is not None and hashlib.sha256(secure_regular_bytes(
        Path(prior["path"]), "pre-cutover maintenance snapshot",
    )).hexdigest() != prior["sha256"]:
        raise ReleaseError("pre-cutover maintenance snapshot changed")


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


def restore_activation_maintenance(plan: dict[str, Any]) -> None:
    prior = plan["identity"]["maintenance_prior"]
    if prior is None:
        return
    raw = secure_regular_bytes(Path(prior["path"]), "pre-cutover maintenance snapshot")
    if hashlib.sha256(raw).hexdigest() != prior["sha256"]:
        raise ReleaseError("pre-cutover maintenance snapshot changed")
    atomic_bytes(Path(plan["request"]["product"]) / "factory/MAINTENANCE", raw)


def activation_maintenance_matches(plan: dict[str, Any]) -> bool:
    marker = Path(plan["request"]["product"]) / "factory/MAINTENANCE"
    prior = plan["identity"]["maintenance_prior"]
    if prior is None:
        return not marker.exists() and not marker.is_symlink()
    try:
        return hashlib.sha256(secure_regular_bytes(
            marker, "maintenance marker",
        )).hexdigest() == prior["sha256"]
    except (OSError, ReleaseError):
        return False


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


def operator_inventory_ready(product: Path, inventory: list[dict[str, Any]]) -> bool:
    try:
        mapping = safe_state(
            product / "factory/operator-map.json",
            "operator map",
        )
    except (OSError, ReleaseError):
        return False
    tickets = mapping.get("tickets")
    return isinstance(tickets, dict) and all(
        isinstance(tickets.get(item["ticket"]), dict)
        and tickets[item["ticket"]].get("operator_fields_initialized") is True
        for item in inventory
    )


def initialize_operator_inventory(
    release: Path, kits_root: Path, project: str, product: Path,
    inventory: list[dict[str, Any]],
    environment: dict[str, str],
) -> None:
    arguments = [
        sys.executable, "-I", str(release / "scripts/operator-cli.py"),
        "--product", str(product), "--state-dir",
        str(kits_root / "projects" / project / "controller"),
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
    if not operator_inventory_ready(product, inventory):
        raise ReleaseError("operator projection initialization is incomplete")


def operator_map_ready(plan: dict[str, Any]) -> bool:
    return operator_inventory_ready(
        Path(plan["identity"]["product_path"]), plan["identity"]["tickets"],
    )


def initialize_operator_map(
    release: Path, kits_root: Path, plan: dict[str, Any],
    environment: dict[str, str],
) -> None:
    initialize_operator_inventory(
        release, kits_root, plan["request"]["project"],
        Path(plan["identity"]["product_path"]), plan["identity"]["tickets"],
        environment,
    )


def initialize_host_operator_maps(
    release: Path, kits_root: Path, items: list[dict[str, Any]],
) -> None:
    for item in items:
        initialize_operator_inventory(
            release, kits_root, item["project"], Path(item["product"]), item["tickets"],
            command_environment(kits_root, Path(item["runtime"]["evidence"]["path"])),
        )


def ensure_controller(plan: dict[str, Any]) -> None:
    controller = plan["identity"]["controller"]
    if controller.get("status") == "not-applicable":
        return
    path = Path(controller["path"])
    expected = controller_payload(
        plan["request"]["project"], Path(plan["identity"]["product_path"]),
        launcher_path(plan["children"]["launcher"]),
    )
    desired = hashlib.sha256(expected).hexdigest()
    if desired != controller["sha256"]:
        raise ReleaseError("controller job changed after setup")
    if "action" in controller:
        current = None
        if path.exists() or path.is_symlink():
            current = hashlib.sha256(
                secure_regular_bytes(path, "controller job")
            ).hexdigest()
        if current not in {controller.get("previous_sha256"), desired}:
            raise ReleaseError("controller job changed after setup")
        unload_service(controller)
        if current != desired:
            atomic_bytes(path, expected)
    elif exact_local_file(path, expected, "controller job") != desired:
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


def register_production_target(
    release: Path, project: str, launcher: Path,
) -> None:
    candidate = release / "scripts/factory-cli.py"
    if not candidate.exists():
        return
    target = f"production-{hashlib.sha256(project.encode()).hexdigest()[:16]}"
    run(
        [str(candidate), "register", target,
         str(launcher), project],
        "human CLI production target registration",
        environment={
            "FACTORY_INTERNAL_REGISTER": "1",
            "HOME": str(account_home()),
            "PATH": "/usr/bin:/bin",
        },
    )


def apply_activation(
    plan: dict[str, Any], kits_root: Path, approved_by: str, journal: Path,
) -> dict[str, Any]:
    request = plan["request"]
    project = request["project"]
    product = Path(request["product"])
    release = kits_root / "releases" / request["sha"]
    kit = release / "scripts/factory-kit.sh"
    launcher = launcher_path(plan["children"]["launcher"])
    runtime = Path(plan["identity"]["runtime"]["evidence"]["path"])
    environment = launcher_environment(kits_root, runtime)
    kit_environment = command_environment(
        kits_root, runtime, cutover_lock=True,
    )
    value = safe_state(journal, "release journal") if journal.exists() else None
    if value and value.get("status") == "pass":
        if (
            not active_exact(kits_root, plan)
            or (product / "factory/KILL").exists()
            or not activation_maintenance_matches(plan)
            or not model_ready(launcher, plan, environment)
            or not migration_complete(kits_root, plan, approved_by)
            or not operator_map_ready(plan)
        ):
            raise ReleaseError("completed release evidence no longer matches runtime state")
        doctor(launcher, plan, environment)
        register_production_target(release, project, launcher)
        return completed_result(plan, True)
    if value and value.get("phase") in {"doctor_pass", "dispatch_started"} and (
        active_exact(kits_root, plan) and not (product / "factory/KILL").exists()
        and activation_maintenance_matches(plan)
        and model_ready(launcher, plan, environment)
        and migration_complete(kits_root, plan, approved_by)
        and operator_map_ready(plan)
    ):
        doctor(launcher, plan, environment)
        journal_update(journal, plan, "dispatch_started", "pass")
        register_production_target(release, project, launcher)
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
            check=False, env=kit_environment, timeout=300,
            pass_fds=(_CUTOVER_LOCK_FD,) if _CUTOVER_LOCK_FD is not None else (),
        )
        if not active_exact(kits_root, plan):
            run(
                ["bash", str(kit), "activate", "--project", project,
                 "--product", str(product), "--sha", request["sha"],
                 "--receipt", str(receipt)], "release activation",
                environment=kit_environment,
            )
        if not active_exact(kits_root, plan):
            raise ReleaseError("activation did not commit the approved release")
    journal_update(journal, plan, "activated", "pass")
    ensure_barrier(product, plan)
    journal_update(journal, plan, "cutover_barrier", "pass")
    maintenance_removed = False
    maintenance_finalized = False
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
        restore_activation_maintenance(plan)
        maintenance_finalized = True
        marker = product / "factory/KILL"
        try:
            marker_value = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ReleaseError("release dispatch barrier changed") from error
        if marker_value != barrier_value(plan):
            raise ReleaseError("release dispatch barrier changed")
        marker.unlink()
        journal_update(journal, plan, "dispatch_started", "pass")
        register_production_target(release, project, launcher)
        return completed_result(plan, False)
    except Exception:
        if maintenance_removed and not maintenance_finalized:
            subprocess.run(
                ["bash", str(kit), "pause", "--project", project,
                 "--product", str(product)], stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, env=kit_environment, timeout=300,
                pass_fds=(_CUTOVER_LOCK_FD,) if _CUTOVER_LOCK_FD is not None else (),
            )
        raise


def _resume_locked(args: argparse.Namespace, kits_root: Path) -> dict[str, Any]:
    latest, journals = plan_paths(kits_root, args.project, args.sha)
    plan = current_plan(latest)
    approval = plan["approval_sha256"]
    if plan["request"]["operator_id"] != args.approved_by:
        raise ReleaseError("release approver does not match setup operator")
    secure_directory(journals, create=True)
    journal = journals / f"{approval}.json"
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
        dispatched = (
            value.get("phase") in {"dispatch_started", "complete"}
            or value.get("status") == "pass"
            or (
                value.get("phase") == "doctor_pass"
                and active_exact(kits_root, plan)
                and not (Path(plan["identity"]["product_path"]) / "factory/KILL").exists()
            )
        )
        validate_live_basis(
            kits_root, plan, require_idle_dispatch=not dispatched,
        )
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


def resume(args: argparse.Namespace) -> dict[str, Any]:
    if (
        not PROJECT.fullmatch(args.project) or not SHA.fullmatch(args.sha)
        or not SAFE_ID.fullmatch(args.approved_by) or args.approved_by == "auto"
    ):
        raise ReleaseError("release approval boundary is invalid")
    kits_root = args.kits_root.resolve(strict=True)
    require_test_layout(kits_root)
    descriptor = acquire_cutover_lock(kits_root)
    try:
        return _resume_locked(args, kits_root)
    finally:
        release_cutover_lock(descriptor)


def abort(args: argparse.Namespace) -> dict[str, Any]:
    if (
        not PROJECT.fullmatch(args.project) or not SHA.fullmatch(args.sha)
        or not SAFE_ID.fullmatch(args.approved_by) or args.approved_by == "auto"
    ):
        raise ReleaseError("release abort boundary is invalid")
    kits_root = args.kits_root.resolve(strict=True)
    require_test_layout(kits_root)
    descriptor = acquire_cutover_lock(kits_root)
    try:
        latest, _ = plan_paths(kits_root, args.project, args.sha)
        plan = current_plan(latest)
        items = plan["children"].get("host_cutover")
        if (
            plan["request"]["operator_id"] != args.approved_by
            or plan["stage"] != "prerequisites" or not items
        ):
            raise ReleaseError("release plan cannot be aborted")
        require_reservation(kits_root, plan)
        journal = read_cutover(
            kits_root / "contract-cutover-journal.json", plan["approval_sha256"],
        )
        if journal["phase"] != "approved" or journal["completed_projects"]:
            raise ReleaseError("host cutover passed its abort boundary")
        validate_live_basis(kits_root, plan)
        for item in items:
            active = kits_root / "projects" / item["project"] / "active.json"
            claim = kits_root / "receipts/consumed" / f"{item['receipt']['receipt_id']}.json"
            if file_digest(active) != item["source_active_sha256"] or claim.exists() or claim.is_symlink():
                raise ReleaseError("host cutover changed after approval")
            cutover_maintenance_restore(item)
        for item in items:
            clear_cutover_maintenance(item)
        clear_reservation(kits_root, plan)
        return {
            "approval_sha256": plan["approval_sha256"], "project": args.project,
            "schema": RESULT_SCHEMA, "status": "aborted",
        }
    finally:
        release_cutover_lock(descriptor)


class QualificationTimer:
    def __init__(
        self, prior: list[dict[str, Any]] | None = None, prior_ms: int | None = None,
        started: float | None = None,
    ):
        self.started = time.monotonic() if started is None else started
        self.timings = list(prior or [])
        self.prior_ms = (
            prior_ms if prior_ms is not None
            else sum(item["duration_ms"] for item in self.timings)
        )

    def phase(self, name: str, operation: Any) -> Any:
        started = time.monotonic()
        try:
            return operation()
        finally:
            self.timings.append({
                "duration_ms": max(0, round((time.monotonic() - started) * 1000)),
                "phase": name,
            })
            self.check()

    def elapsed_ms(self) -> int:
        return self.prior_ms + max(0, round((time.monotonic() - self.started) * 1000))

    def remaining_seconds(self) -> float:
        return max(0.001, (QUALIFICATION_BUDGET_MS - self.elapsed_ms()) / 1000)

    def check(self) -> None:
        if self.elapsed_ms() > QUALIFICATION_BUDGET_MS:
            slowest = max(self.timings, key=lambda item: item["duration_ms"], default={})
            raise ReleaseError(
                "qualification migration exceeded 60 seconds"
                + (f" during {slowest.get('phase')}" if slowest else "")
            )


def qualification_module(repo: Path) -> Any:
    helper = repo / "scripts/qualification-environment.py"
    spec = importlib.util.spec_from_file_location(
        f"qualification_environment_{hashlib.sha256(str(repo).encode()).hexdigest()[:12]}",
        helper,
    )
    if not spec or not spec.loader:
        raise ReleaseError("qualification environment helper is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def qualification_state(kits_root: Path, project: str, sha: str) -> Path:
    return kits_root.parent / "qualification-migrations" / project / sha


def qualification_provider_snapshot(path: Path) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for name in ("cli-runtimes", "provider-apply-locks", "provider-attempts"):
        root = secure_directory(path / name)
        for item in sorted(root.rglob("*"), key=str):
            relative = str(item.relative_to(path))
            if item.is_symlink():
                raise ReleaseError("qualification provider state is unsafe")
            if item.is_file():
                snapshot[relative] = file_digest(item)
    return snapshot


def qualification_fallback(
    module: Any, repo: Path, root: Path, project: str, product: Path,
    scratch: Path, timeout: float,
) -> tuple[dict[str, Any], str]:
    try:
        return module.qualification_fallback_readiness(
            repo, root, project, product, scratch, timeout,
        )
    except ValueError as error:
        raise ReleaseError(
            str(error), getattr(error, "reason_code", None),
        ) from error


def qualification_basis(
    project: str, root: Path, product: Path, repo: Path, sha: str,
) -> tuple[dict[str, Any], Any]:
    if not PROJECT.fullmatch(project) or not SHA.fullmatch(sha):
        raise ReleaseError("qualification migration identity is invalid")
    root = Path(os.path.realpath(root))
    if not re.fullmatch(
        r"/private/tmp/nysa-sf-qualification[.][A-Za-z0-9._-]+", str(root),
    ):
        raise ReleaseError("qualification root must be under /private/tmp")
    repo = repo.resolve(strict=True)
    product = product.resolve(strict=True)
    factory_sha, factory_tree, factory_origin = clean_identity(repo, "Factory candidate")
    product_sha, product_tree, product_origin = clean_identity(product, "product")
    if factory_sha != sha:
        raise ReleaseError("Factory candidate does not match qualification SHA")
    if (
        os.environ.get("FACTORY_KIT_TEST_MODE") != "1"
        and (
            git(repo, "rev-parse", "refs/remotes/origin/main") != factory_sha
            or git(product, "rev-parse", "refs/remotes/origin/main") != product_sha
        )
    ):
        raise ReleaseError("qualification migration inputs are not exact protected main")
    if secure_regular_bytes(product / "factory/KIT_PIN", "product KIT_PIN") != (sha + "\n").encode():
        raise ReleaseError("product pin does not match qualification SHA")
    module = qualification_module(repo)
    try:
        contract_version = json.loads(
            (repo / "factory-contract.json").read_text(encoding="utf-8")
        ).get("contract_version")
        manifest = module.qualification_manifest(product, sha)
        module.validate_selected_contracts(product, manifest)
        source = module.validate_upgrade_source(
            root, project, repo, product, sha, contract_version, manifest,
        )
    except (OSError, ValueError) as error:
        raise ReleaseError(str(error)) from error
    active_path = root / f"projects/{project}/active.json"
    environment_path = root / "environment.json"
    receipt_id = source["active"].get("receipt_id", "")
    if not DIGEST.fullmatch(str(receipt_id)):
        raise ReleaseError("qualification active receipt is invalid")
    receipt_path = root / "receipts" / f"{receipt_id}.json"
    authorization_relative = f"factory/migrations/inflight-release/{sha}.json"
    authorization = product / authorization_relative
    if (
        git(product, "ls-files", "--error-unmatch", "--", authorization_relative)
        != authorization_relative
        or git(product, "rev-parse", f"HEAD:{authorization_relative}")
        != git(product, "hash-object", "--no-filters", "--", authorization_relative)
    ):
        raise ReleaseError("qualification migration authorization is not sealed")
    secure_regular_bytes(authorization, "qualification migration authorization")
    authority = source["authority"]
    basis = {
        "active": {
            "generation": source["active"]["generation"],
            "kit_sha": source["active"]["kit_sha"],
            "path": str(active_path),
            "sha256": file_digest(active_path),
        },
        "authorization_sha256": file_digest(authorization),
        "authority_sha256": file_digest(authority / "authority.json"),
        "certification_plan_sha256": file_digest(product / "factory/certification-plan.json"),
        "environment": {"path": str(environment_path), "sha256": file_digest(environment_path)},
        "factory_origin": factory_origin,
        "factory_sha": factory_sha,
        "factory_tree": factory_tree,
        "kit_pin_sha256": file_digest(product / "factory/KIT_PIN"),
        "manifest_sha256": file_digest(product / "factory/QUALIFICATION.json"),
        "operator_identities": {
            "map_sha256": file_digest(source["operator_map_path"]),
            "runtime_ledger_sha256": file_digest(source["runtime_ledger_path"]),
        },
        "previous_receipt": {"path": str(receipt_path), "sha256": file_digest(receipt_path)},
        "product_origin": product_origin,
        "product_path": str(product),
        "product_sha": product_sha,
        "product_tree": product_tree,
        "provider_state": qualification_provider_snapshot(source["provider"]),
        "qualification_root": str(root),
        "selected_tickets": manifest["tickets"],
    }
    return basis, module


def qualification_runtime_child(
    repo: Path, product: Path, root: Path, kits_root: Path, project: str,
    runtime_bin: Path, *, timeout: float = 60,
) -> dict[str, Any]:
    runtime_bin = runtime_bin.resolve(strict=True)
    runtime_root = secure_directory(root / "project-runtimes" / project)
    journal = runtime_root / "runtime-pin-journal.json"
    if journal.exists() or journal.is_symlink():
        current = safe_state(journal, "qualification runtime journal")
        if current.get("status") != "completed":
            raise ReleaseError("qualification runtime transaction is incomplete")
        evidence = run_json([
            sys.executable, "-I", "-S",
            str(repo / "scripts/owner-runtime-pin.py"), "check",
            "--journal", str(journal),
        ], "qualification runtime replay", environment=command_environment(kits_root),
            timeout=timeout)
        previous = current.get("plan")
        if (
            isinstance(previous, dict)
            and previous.get("product_path") == str(product)
            and previous.get("runtime_bin") == str(runtime_bin)
            and previous.get("target_bin") == str(runtime_root / "bin")
        ):
            return {"action": "reuse", "evidence": evidence, "plan": previous}
    result = subprocess.run([
        sys.executable, "-I", "-S", str(repo / "scripts/owner-runtime-pin.py"),
        "plan", "--product", str(product), "--runtime-bin", str(runtime_bin),
        "--target-bin", str(runtime_root / "bin"),
    ], text=True, capture_output=True, check=False,
        env=command_environment(kits_root), timeout=timeout)
    if result.returncode:
        detail = result.stderr.strip().removeprefix("ERROR: ").strip()
        if detail.startswith("runtime mismatch for "):
            raise ReleaseError(
                f"runtime_tuple_mismatch: {detail}", "runtime_tuple_mismatch",
            )
        raise ReleaseError("qualification runtime preview failed")
    try:
        plan = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise ReleaseError("qualification runtime preview returned invalid evidence") from error
    if not isinstance(plan, dict):
        raise ReleaseError("qualification runtime preview returned invalid evidence")
    return {"action": "apply", "plan": plan}


def qualification_provider_paths() -> dict[str, str]:
    receipt = safe_state(
        account_home() / ".factory/provider-cli-pin.json", "provider CLI pin receipt",
    )
    unsigned = dict(receipt)
    supplied = unsigned.pop("receipt_sha256", "")
    if (
        receipt.get("schema") != "nysa.software-factory.provider-cli-pin-receipt/v1"
        or supplied != digest(unsigned)
        or not isinstance(receipt.get("candidates"), list)
    ):
        raise ReleaseError("provider CLI pin receipt is invalid")
    candidates = {
        item.get("name"): item.get("physical_path")
        for item in receipt["candidates"] if isinstance(item, dict)
    }
    if any(not isinstance(candidates.get(name), str) for name in ("claude", "codex", "agent")):
        raise ReleaseError("provider CLI pin receipt lacks exact executable paths")
    return {
        "claude": candidates["claude"], "codex": candidates["codex"],
        "cursor": candidates["agent"],
    }


def qualification_provider_child(
    kit: Path, kits_root: Path, sha: str, operator: str, *, timeout: float = 60,
) -> dict[str, Any]:
    checked = subprocess.run(
        ["bash", str(kit), "provider-cli-pin", "check", "--sha", sha],
        text=True, capture_output=True, check=False,
        env=command_environment(kits_root), timeout=timeout,
    )
    if checked.returncode == 0:
        try:
            evidence = json.loads(checked.stdout)
        except json.JSONDecodeError as error:
            raise ReleaseError("provider CLI check returned invalid evidence") from error
        return {"action": "reuse", "evidence": evidence}
    _, child = child_plan(
        kit, kits_root, sha, 1, qualification_provider_paths(), operator,
        timeout=timeout,
    )
    return child


def validate_qualification_plan(plan: dict[str, Any]) -> None:
    body = {key: value for key, value in plan.items() if key != "approval_sha256"}
    request = plan.get("request")
    children = plan.get("children")
    identity = plan.get("identity")
    timings = plan.get("preview_timings")
    fallback = plan.get("fallback_readiness")
    if (
        set(plan) != {
            "approval_required", "approval_sha256", "children", "created_epoch",
            "expires_epoch", "fallback_readiness", "identity", "preview_elapsed_ms",
            "preview_timings", "request", "schema", "status",
        }
        or plan.get("schema") != QUALIFICATION_PLAN_SCHEMA
        or plan.get("approval_sha256") != digest(body)
        or plan.get("status") != "planned"
        or not isinstance(plan.get("approval_required"), bool)
        or not isinstance(plan.get("created_epoch"), int)
        or not isinstance(plan.get("expires_epoch"), int)
        or plan["expires_epoch"] <= plan["created_epoch"]
        or not isinstance(plan.get("preview_elapsed_ms"), int)
        or isinstance(plan.get("preview_elapsed_ms"), bool)
        or not 0 <= plan["preview_elapsed_ms"] <= QUALIFICATION_BUDGET_MS
        or not isinstance(timings, list)
        or any(
            not isinstance(item, dict) or set(item) != {"duration_ms", "phase"}
            or not isinstance(item["duration_ms"], int) or item["duration_ms"] < 0
            or not isinstance(item["phase"], str)
            for item in timings
        )
        or not isinstance(request, dict)
        or set(request) != {
            "operator_id", "product", "project", "repo", "root", "runtime_bin", "sha",
        }
        or not PROJECT.fullmatch(str(request.get("project", "")))
        or not SHA.fullmatch(str(request.get("sha", "")))
        or not SAFE_ID.fullmatch(str(request.get("operator_id", "")))
        or request.get("operator_id") == "auto"
        or any(
            not isinstance(request.get(key), str) or not Path(request[key]).is_absolute()
            for key in ("product", "repo", "root", "runtime_bin")
        )
        or not isinstance(children, dict) or set(children) != {"provider_cli", "runtime"}
        or any(
            not isinstance(child, dict) or child.get("action") not in {"apply", "reuse"}
            for child in children.values()
        )
        or plan.get("approval_required") != any(
            child["action"] == "apply" for child in children.values()
        )
        or not isinstance(identity, dict)
        or not isinstance(identity.get("selected_tickets"), list)
        or any(not TICKET.fullmatch(str(ticket)) for ticket in identity["selected_tickets"])
        or len(set(identity["selected_tickets"])) != len(identity["selected_tickets"])
        or not isinstance(fallback, dict)
        or set(fallback) != {"evidence", "sha256"}
        or not isinstance(fallback.get("evidence"), dict)
        or not DIGEST.fullmatch(str(fallback.get("sha256", "")))
        or fallback["evidence"].get("readiness_sha256") != fallback["sha256"]
    ):
        raise ReleaseError("qualification migration plan is invalid")
    for child in children.values():
        if child["action"] == "apply" and (
            not isinstance(child.get("plan"), dict)
            or not DIGEST.fullmatch(str(child["plan"].get("approval_sha256", "")))
        ) or child["action"] == "reuse" and not isinstance(
            child.get("evidence"), dict,
        ):
            raise ReleaseError("qualification migration plan is invalid")
    if any(
        child["action"] == "reuse" and child["evidence"].get("status") != "ready"
        for child in children.values()
    ):
        raise ReleaseError("qualification migration plan is invalid")
    provider = children["provider_cli"]
    if provider["action"] == "apply":
        candidates = provider["plan"].get("candidates")
        by_name = {
            item.get("name"): item for item in candidates or []
            if isinstance(item, dict)
        }
        if (
            not isinstance(candidates, list) or len(by_name) != len(candidates)
            or not {"agent", "claude", "codex"} <= set(by_name)
            or any(
                not Path(str(by_name[name].get("physical_path", ""))).is_absolute()
                for name in ("agent", "claude", "codex")
            )
        ):
            raise ReleaseError("qualification migration plan is invalid")


def qualification_plan_key(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        key: plan[key] for key in (
            "approval_required", "children", "fallback_readiness", "identity", "request",
        )
    }


def qualification_basis_matches(
    current: dict[str, Any], expected: dict[str, Any], target_sha: str,
) -> bool:
    if current == expected:
        return True
    if set(current) != set(expected):
        return False
    active = current.get("active")
    previous = expected.get("active")
    environment = current.get("environment")
    previous_environment = expected.get("environment")
    return bool(
        isinstance(active, dict) and isinstance(previous, dict)
        and set(active) == set(previous) == {"generation", "kit_sha", "path", "sha256"}
        and isinstance(active["generation"], int)
        and isinstance(previous["generation"], int)
        and not isinstance(active["generation"], bool)
        and not isinstance(previous["generation"], bool)
        and active["kit_sha"] == target_sha
        and active["generation"] == previous["generation"] + 1
        and active["path"] == previous["path"]
        and isinstance(environment, dict) and isinstance(previous_environment, dict)
        and environment.get("path") == previous_environment.get("path")
        and all(
            current[key] == expected[key]
            for key in current
            if key not in {
                "active", "authority_sha256", "environment", "previous_receipt",
            }
        )
    )


def store_qualification_plan(
    state: Path, body: dict[str, Any], preview_timings: list[dict[str, Any]],
    preview_elapsed_ms: int,
) -> dict[str, Any]:
    latest = state / "latest.json"
    candidate_key = {key: body[key] for key in (
        "approval_required", "children", "fallback_readiness", "identity", "request",
    )}
    if latest.exists() or latest.is_symlink():
        current = safe_state(latest, "qualification migration plan")
        validate_qualification_plan(current)
        if qualification_plan_key(current) == candidate_key:
            return current
    now = int(time.time())
    plan = seal_plan({
        **body, "created_epoch": now, "expires_epoch": now + 7200,
        "preview_elapsed_ms": preview_elapsed_ms, "preview_timings": preview_timings,
        "schema": QUALIFICATION_PLAN_SCHEMA,
        "status": "planned",
    })
    validate_qualification_plan(plan)
    secure_directory(state / "plans", create=True)
    immutable = state / "plans" / f"{plan['approval_sha256']}.json"
    if immutable.exists() or immutable.is_symlink():
        if safe_state(immutable, "qualification migration plan") != plan:
            raise ReleaseError("qualification migration plan conflicts")
    else:
        atomic_json(immutable, plan)
    atomic_json(latest, plan)
    return plan


def read_qualification_journal(
    path: Path, plan: dict[str, Any],
) -> dict[str, Any]:
    value = safe_state(path, "qualification migration journal")
    unsigned = {key: item for key, item in value.items() if key != "record_sha256"}
    timings = value.get("timings")
    if (
        value.get("schema") != QUALIFICATION_JOURNAL_SCHEMA
        or value.get("plan") != plan
        or ("record_sha256" in value and value["record_sha256"] != digest(unsigned))
        or not isinstance(value.get("events"), list)
        or not isinstance(timings, list)
        or any(
            not isinstance(item, dict) or set(item) != {"duration_ms", "phase"}
            or not isinstance(item["duration_ms"], int) or item["duration_ms"] < 0
            or not isinstance(item["phase"], str)
            for item in timings
        )
    ):
        raise ReleaseError("qualification migration journal is invalid")
    return value


def qualification_journal_update(
    path: Path, plan: dict[str, Any], phase: str, timings: list[dict[str, Any]],
) -> dict[str, Any]:
    value = (
        read_qualification_journal(path, plan)
        if path.exists() or path.is_symlink() else {
            "events": [], "plan": plan, "schema": QUALIFICATION_JOURNAL_SCHEMA,
        }
    )
    if not value["events"] or value["events"][-1].get("phase") != phase:
        value["events"].append({"observed_epoch_ms": int(time.time() * 1000), "phase": phase})
    value.update(phase=phase, status="pass" if phase == "complete" else "in-progress", timings=timings)
    value = signed_journal(value)
    atomic_json(path, value)
    return value


def qualification_fail_after(phase: str) -> None:
    if (
        os.environ.get("FACTORY_KIT_TEST_MODE") == "1"
        and os.environ.get("FACTORY_TRUSTED_TEST_HARNESS") == "1"
        and os.environ.get("FACTORY_QUALIFICATION_MIGRATION_FAIL_AFTER") == phase
    ):
        raise ReleaseError(f"injected qualification migration failure after {phase}")


def qualification_completion(
    path: Path, plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    receipt = safe_state(path, "qualification migration completion")
    unsigned = dict(receipt)
    supplied = unsigned.pop("completion_sha256", "")
    if (
        receipt.get("schema") != QUALIFICATION_RECEIPT_SCHEMA
        or supplied != digest(unsigned)
        or receipt.get("status") != "doctor_ready"
        or not isinstance(receipt.get("total_duration_ms"), int)
        or isinstance(receipt.get("total_duration_ms"), bool)
        or not 0 <= receipt["total_duration_ms"] <= QUALIFICATION_BUDGET_MS
        or not isinstance(receipt.get("timings"), list)
        or not isinstance(receipt.get("slowest_phase"), dict)
        or not isinstance(receipt.get("generation"), int)
        or isinstance(receipt.get("generation"), bool)
        or plan is not None and (
            receipt.get("approval_sha256") != plan["approval_sha256"]
            or receipt.get("factory_sha") != plan["request"]["sha"]
            or receipt.get("project") != plan["request"]["project"]
        )
    ):
        raise ReleaseError("qualification migration completion is invalid")
    return receipt


def apply_qualification_plan(
    plan: dict[str, Any], kits_root: Path, approved_by: str | None,
    *, started: float | None = None,
) -> dict[str, Any]:
    validate_qualification_plan(plan)
    request = plan["request"]
    if plan["approval_required"]:
        if approved_by != request["operator_id"]:
            raise ReleaseError("qualification migration approver does not match operator")
    elif approved_by not in {None, request["operator_id"]}:
        raise ReleaseError("qualification migration operator changed")
    state = qualification_state(kits_root, request["project"], request["sha"])
    completion = state / "completion.json"
    if completion.exists() or completion.is_symlink():
        return qualification_completion(completion, plan)
    journal_path = state / "journal.json"
    lock = os.open(
        state / ".migration.lock", os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        info = os.fstat(lock)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o077:
            raise ReleaseError("qualification migration lock is unsafe")
        fcntl.flock(lock, fcntl.LOCK_EX)
        if completion.exists():
            return qualification_completion(completion, plan)
        timings = plan["preview_timings"]
        prior_ms = plan["preview_elapsed_ms"]
        if journal_path.exists() or journal_path.is_symlink():
            prior = read_qualification_journal(journal_path, plan)
            prior_timings = prior.get("timings")
            if (
                not isinstance(prior_timings, list)
                or prior_timings[:len(timings)] != timings
            ):
                raise ReleaseError("qualification migration journal is invalid")
            timings = prior_timings
            prior_ms += sum(
                item["duration_ms"] for item in timings[len(plan["preview_timings"]):]
            )
        timer = QualificationTimer(timings, prior_ms, started)
        timer.check()

        def basis() -> tuple[dict[str, Any], Any]:
            return qualification_basis(
                request["project"], Path(request["root"]), Path(request["product"]),
                Path(request["repo"]), request["sha"],
            )

        current, module = timer.phase("revalidate", basis)
        target_active = current["active"]["kit_sha"] == request["sha"]
        if not qualification_basis_matches(current, plan["identity"], request["sha"]):
            changed = ",".join(sorted(
                (set(current) | set(plan["identity"]))
                - {key for key in set(current) & set(plan["identity"])
                   if current[key] == plan["identity"][key]}
            ))
            raise ReleaseError(
                f"qualification migration inputs changed after preview: {changed}"
            )
        qualification_journal_update(journal_path, plan, "validated", timer.timings)

        runtime = plan["children"]["runtime"]
        sealed_release = kits_root / "releases" / request["sha"]
        if runtime["action"] == "apply":
            runtime_plan = state / "runtime-plan.json"
            if runtime_plan.exists() or runtime_plan.is_symlink():
                if safe_state(runtime_plan, "qualification runtime plan") != runtime["plan"]:
                    raise ReleaseError("qualification runtime plan changed")
            else:
                atomic_json(runtime_plan, runtime["plan"])
            timer.phase("runtime", lambda: run_json([
                sys.executable, "-I", "-S",
                str(sealed_release / "scripts/owner-runtime-pin.py"),
                "apply", "--plan", str(runtime_plan), "--approve-hash",
                runtime["plan"]["approval_sha256"],
            ], "qualification runtime apply", environment=command_environment(kits_root),
                timeout=timer.remaining_seconds()))
        else:
            timer.phase("runtime", lambda: run_json([
                sys.executable, "-I", "-S",
                str(sealed_release / "scripts/owner-runtime-pin.py"),
                "check", "--journal", str(
                    Path(request["root"]) / "project-runtimes"
                    / request["project"] / "runtime-pin-journal.json"
                ),
            ], "qualification runtime replay", environment=command_environment(
                kits_root,
            ), timeout=timer.remaining_seconds()))
        qualification_journal_update(journal_path, plan, "runtime_ready", timer.timings)
        qualification_fail_after("runtime")

        provider = plan["children"]["provider_cli"]

        def prepare_provider() -> None:
            if provider["action"] == "apply":
                candidates = {
                    item["name"]: item["physical_path"]
                    for item in provider["plan"]["candidates"]
                }
                run_json([
                    "bash", str(sealed_release / "scripts/factory-kit.sh"),
                    "provider-cli-pin", "apply", "--sha", request["sha"],
                    "--claude-bin", candidates["claude"],
                    "--codex-bin", candidates["codex"],
                    "--cursor-bin", candidates["agent"],
                    "--operator-id", request["operator_id"],
                    "--approve-hash", provider["plan"]["approval_sha256"],
                ], "qualification provider CLI apply", environment=command_environment(
                    kits_root, cutover_lock=True,
                ), timeout=timer.remaining_seconds())
            evidence = run_json([
                "bash", str(sealed_release / "scripts/factory-kit.sh"),
                "provider-cli-pin", "check", "--sha", request["sha"],
            ], "qualification provider CLI replay", environment=command_environment(
                kits_root, cutover_lock=True,
            ), timeout=timer.remaining_seconds())
            if provider["action"] == "reuse" and evidence != provider["evidence"]:
                raise ReleaseError("qualification provider CLI evidence changed")

        timer.phase("provider_cli", prepare_provider)
        qualification_journal_update(journal_path, plan, "provider_cli_ready", timer.timings)
        qualification_fail_after("provider_cli")

        current, module = timer.phase("preapply_revalidate", basis)
        target_active = current["active"]["kit_sha"] == request["sha"]
        if not qualification_basis_matches(current, plan["identity"], request["sha"]):
            raise ReleaseError("qualification migration inputs changed before apply")
        if not target_active:
            fallback, fallback_sha = timer.phase(
                "fallback_readiness",
                lambda: qualification_fallback(
                    module, Path(request["repo"]), Path(request["root"]),
                    request["project"], Path(request["product"]),
                    state / "fallback-scratch", timer.remaining_seconds(),
                ),
            )
            if fallback_sha != plan["fallback_readiness"]["sha256"]:
                raise ReleaseError("qualification fallback readiness changed before apply")

        arguments = [
            sys.executable, "-I", str(sealed_release / "scripts/qualification-environment.py"),
            "--factory-root", request["repo"], "--product-root", request["product"],
            "--project", request["project"], "--root", request["root"],
            "--runtime-bin", request["runtime_bin"], "--upgrade",
        ]

        def upgrade() -> dict[str, Any]:
            result = subprocess.run(
                arguments, text=True, capture_output=True, check=False,
                timeout=timer.remaining_seconds(),
                env=command_environment(kits_root, Path(request["root"]) / "project-runtimes" / request["project"] / "bin"),
            )
            try:
                value = json.loads(result.stdout)
            except json.JSONDecodeError as error:
                raise ReleaseError("qualification environment upgrade returned invalid evidence") from error
            if result.returncode or value.get("status") != "upgraded":
                reason = value.get("reason_code") or value.get("error") or "invalid"
                raise ReleaseError(
                    f"qualification environment upgrade failed: {reason}",
                    value.get("reason_code"),
                )
            return value

        upgraded = timer.phase("environment_upgrade", upgrade)
        qualification_journal_update(journal_path, plan, "environment_upgraded", timer.timings)
        qualification_fail_after("environment_upgrade")
        launcher = Path(upgraded["launcher"])

        def doctor() -> dict[str, Any]:
            value = run_json(
                [str(launcher), request["project"], "doctor", "--json"],
                "qualification Doctor", timeout=timer.remaining_seconds(),
            )
            checks = value.get("checks")
            if (
                value.get("schema") != "nysa.software-factory.doctor/v2"
                or value.get("overall_status") != "ok"
                or not isinstance(checks, dict)
                or any(
                    not isinstance(item, dict) or item.get("status") == "error"
                    for item in checks.values()
                )
            ):
                raise ReleaseError("qualification Doctor did not pass")
            return value

        doctor_result = timer.phase("doctor", doctor)
        qualification_journal_update(journal_path, plan, "doctor_ready", timer.timings)
        qualification_fail_after("doctor")
        final_basis, final_module = timer.phase("completion_validation", basis)
        if not qualification_basis_matches(
            final_basis, plan["identity"], request["sha"],
        ):
            raise ReleaseError("qualification migration inputs changed before completion")
        if final_basis["active"]["kit_sha"] != request["sha"]:
            raise ReleaseError("qualification migration did not activate the candidate")
        if final_basis["provider_state"] != plan["identity"]["provider_state"]:
            raise ReleaseError("qualification migration changed provider state")
        final_module.provider_drained(final_module.qualification_lane(Path(request["root"]), request["project"]))
        timer.check()
        slowest = max(timer.timings, key=lambda item: item["duration_ms"])
        unsigned = {
            "active_sha256": final_basis["active"]["sha256"],
            "approval_sha256": plan["approval_sha256"],
            "doctor_sha256": digest(doctor_result),
            "environment_sha256": final_basis["environment"]["sha256"],
            "factory_sha": request["sha"],
            "generation": final_basis["active"]["generation"],
            "project": request["project"],
            "schema": QUALIFICATION_RECEIPT_SCHEMA,
            "slowest_phase": slowest,
            "status": "doctor_ready",
            "timings": timer.timings,
            "total_duration_ms": timer.elapsed_ms(),
        }
        receipt = {**unsigned, "completion_sha256": digest(unsigned)}
        atomic_json(completion, receipt)
        qualification_journal_update(journal_path, plan, "complete", timer.timings)
        return receipt
    finally:
        os.close(lock)


def _qualification_upgrade_locked(args: argparse.Namespace) -> dict[str, Any]:
    timer = QualificationTimer(started=getattr(args, "process_started", None))
    root = Path(os.path.realpath(args.root))
    product = args.product.resolve(strict=True)
    repo = args.repo.resolve(strict=True)
    runtime_bin = args.runtime_bin.resolve(strict=True)
    state = qualification_state(args.kits_root.resolve(), args.project, args.sha)
    completion = state / "completion.json"
    expected_request = {
        "operator_id": args.operator_id, "product": str(product),
        "project": args.project, "repo": str(repo), "root": str(root),
        "runtime_bin": str(runtime_bin), "sha": args.sha,
    }
    if completion.exists() or completion.is_symlink():
        plan = safe_state(state / "latest.json", "qualification migration plan")
        validate_qualification_plan(plan)
        if plan["request"] != expected_request:
            raise ReleaseError("qualification migration replay inputs changed")
        return qualification_completion(completion, plan)
    journal = state / "journal.json"
    if journal.exists() or journal.is_symlink():
        plan = safe_state(state / "latest.json", "qualification migration plan")
        validate_qualification_plan(plan)
        read_qualification_journal(journal, plan)
        if plan["request"] != expected_request:
            raise ReleaseError("qualification migration replay inputs changed")
        return apply_qualification_plan(
            plan, args.kits_root.resolve(),
            args.operator_id if plan["approval_required"] else None,
            started=getattr(args, "process_started", None),
        )
    identity, module = timer.phase(
        "validation", lambda: qualification_basis(
            args.project, root, product, repo, args.sha,
        ),
    )
    runtime = timer.phase(
        "runtime_preview", lambda: qualification_runtime_child(
            repo, product, root, args.kits_root.resolve(), args.project, runtime_bin,
            timeout=timer.remaining_seconds(),
        ),
    )
    timer.phase("sealed_install", lambda: run([
        "bash", str(repo / "scripts/factory-kit.sh"), "install", "--sha", args.sha,
        "--repo", str(repo),
    ], "sealed qualification candidate install",
        environment=command_environment(args.kits_root.resolve()),
        timeout=timer.remaining_seconds()))
    kit = args.kits_root.resolve() / "releases" / args.sha / "scripts/factory-kit.sh"
    provider = timer.phase(
        "provider_cli_preview", lambda: qualification_provider_child(
            kit, args.kits_root.resolve(), args.sha, args.operator_id,
            timeout=timer.remaining_seconds(),
        ),
    )
    secure_directory(state, create=True)
    fallback_scratch = secure_directory(state / "fallback-scratch", create=True)
    fallback, fallback_sha = timer.phase(
        "fallback_readiness", lambda: qualification_fallback(
            module, repo, root, args.project, product, fallback_scratch,
            timer.remaining_seconds(),
        ),
    )
    approval_required = runtime["action"] == "apply" or provider["action"] == "apply"
    plan = store_qualification_plan(state, {
        "approval_required": approval_required,
        "children": {"provider_cli": provider, "runtime": runtime},
        "fallback_readiness": {"evidence": fallback, "sha256": fallback_sha},
        "identity": identity,
        "request": expected_request,
    }, timer.timings, timer.elapsed_ms())
    if approval_required:
        return {
            "approval_sha256": plan["approval_sha256"],
            "changes": sorted(
                name for name, child in plan["children"].items()
                if child["action"] == "apply"
            ),
            "plan": plan,
            "project": args.project,
            "schema": QUALIFICATION_RESULT_SCHEMA,
            "status": "approval_required",
        }
    return apply_qualification_plan(plan, args.kits_root.resolve(), None)


def qualification_upgrade(args: argparse.Namespace) -> dict[str, Any]:
    kits_root = args.kits_root.resolve()
    descriptor = acquire_cutover_lock(kits_root)
    try:
        return _qualification_upgrade_locked(args)
    finally:
        release_cutover_lock(descriptor)


def _qualification_resume_locked(args: argparse.Namespace) -> dict[str, Any]:
    if (
        not PROJECT.fullmatch(args.project) or not SHA.fullmatch(args.sha)
        or not SAFE_ID.fullmatch(args.approved_by) or args.approved_by == "auto"
    ):
        raise ReleaseError("qualification migration approval boundary is invalid")
    state = qualification_state(args.kits_root.resolve(strict=True), args.project, args.sha)
    plan = safe_state(state / "latest.json", "qualification migration plan")
    validate_qualification_plan(plan)
    if (
        plan["request"]["operator_id"] != args.approved_by
        or not plan["approval_required"]
    ):
        raise ReleaseError("qualification migration approver does not match")
    if plan["expires_epoch"] <= int(time.time()) and not (state / "journal.json").exists():
        raise ReleaseError("qualification migration approval plan is stale")
    options = (
        {"started": args.process_started}
        if hasattr(args, "process_started") else {}
    )
    return apply_qualification_plan(
        plan, args.kits_root.resolve(strict=True), args.approved_by, **options,
    )


def qualification_resume(args: argparse.Namespace) -> dict[str, Any]:
    kits_root = args.kits_root.resolve(strict=True)
    descriptor = acquire_cutover_lock(kits_root)
    try:
        return _qualification_resume_locked(args)
    finally:
        release_cutover_lock(descriptor)


def qualification_recovery_state(
    root: Path, project: str, sha: str, ticket: str, run_id: str,
) -> Path:
    return root / "recoveries" / project / sha / ticket / run_id


def qualification_recovery_environment(lane: dict[str, Any]) -> dict[str, str]:
    environment = {
        "HOME": str(Path.home().resolve(strict=True)),
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "FACTORY_PROVIDER_DB": str(
            lane["provider"] / "accounting/state-v2.sqlite3"
        ),
        "FACTORY_LEDGER": str(lane["active"]["runtime_ledger_path"]),
        "FACTORY_DURABLE_LEDGER": str(lane["product"] / "factory/ledger.csv"),
    }
    if "TMPDIR" in os.environ:
        environment["TMPDIR"] = os.environ["TMPDIR"]
    return environment


def qualification_attempt_cancel(
    repo: Path, lane: dict[str, Any], arguments: list[str], label: str,
) -> dict[str, Any]:
    return run_json(
        [sys.executable, str(repo / "scripts/attempt-cancel.py"), *arguments],
        label, environment=qualification_recovery_environment(lane), timeout=30,
    )


def qualification_recovery_manifest(path: Path) -> dict[str, str]:
    try:
        text = secure_regular_bytes(path, "qualification recovery manifest").decode()
    except UnicodeError as error:
        raise ReleaseError("qualification recovery manifest is invalid") from error
    values: dict[str, str] = {}
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if not separator or not key or key in values:
            raise ReleaseError("qualification recovery manifest is invalid")
        values[key] = value
    return values


def qualification_recovery_row(path: Path, run_id: str) -> dict[str, str]:
    try:
        text = secure_regular_bytes(path, "qualification runtime ledger").decode()
        rows = [row for row in csv.DictReader(text.splitlines()) if row.get("run_id") == run_id]
    except (UnicodeError, csv.Error) as error:
        raise ReleaseError("qualification runtime ledger is invalid") from error
    if len(rows) != 1:
        raise ReleaseError("qualification recovery run is not uniquely recorded")
    return rows[0]


def qualification_recovery_optional_digest(path: Path, label: str) -> str | None:
    if not path.exists() and not path.is_symlink():
        return None
    return hashlib.sha256(secure_regular_bytes(path, label)).hexdigest()


def qualification_recovery_identity(
    args: argparse.Namespace,
) -> tuple[dict[str, Any], Any, dict[str, Any], Path]:
    if (
        not PROJECT.fullmatch(args.project) or not SHA.fullmatch(args.sha)
        or not TICKET.fullmatch(args.ticket) or not RUN_ID.fullmatch(args.failed_run)
        or not SAFE_ID.fullmatch(args.operator_id) or args.operator_id == "auto"
    ):
        raise ReleaseError("qualification recovery identity is invalid")
    repo = args.repo.resolve(strict=True)
    root = Path(os.path.realpath(args.root))
    product = args.product.resolve(strict=True)
    candidate_sha, candidate_tree, candidate_origin = clean_identity(
        repo, "Factory recovery candidate",
    )
    if candidate_sha != args.sha or contract(repo) != "2.0.0":
        raise ReleaseError("Factory recovery candidate identity changed")
    if (
        os.environ.get("FACTORY_KIT_TEST_MODE") != "1"
        and git(repo, "rev-parse", "refs/remotes/origin/main") != candidate_sha
    ):
        raise ReleaseError("Factory recovery candidate is not exact protected main")
    module = qualification_module(repo)
    try:
        lane = module.qualification_lane(root, args.project)
    except (OSError, ValueError) as error:
        raise ReleaseError(str(error)) from error
    if lane["product"] != product or args.ticket not in lane["manifest"]["tickets"]:
        raise ReleaseError("qualification recovery does not belong to the active cohort")
    source_sha = lane["active"].get("kit_sha", "")
    source_tree = lane["active"].get("kit_tree", "")
    ancestry = subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", source_sha, candidate_sha],
        capture_output=True, text=True, check=False,
    )
    if (
        not SHA.fullmatch(source_sha) or not SHA.fullmatch(source_tree)
        or git(repo, "rev-parse", f"{source_sha}^{{tree}}") != source_tree
        or ancestry.returncode != 0 or contract(lane["release"]) != "2.0.0"
    ):
        raise ReleaseError("Factory recovery candidate is not a valid source successor")
    active_path = root / f"projects/{args.project}/active.json"
    receipt_path = root / "receipts" / f"{lane['active']['receipt_id']}.json"
    authority_path = lane["authority"] / "authority.json"
    immutable = {
        "active_sha256": file_digest(active_path),
        "authority_sha256": file_digest(authority_path),
        "candidate_origin": candidate_origin,
        "candidate_sha": candidate_sha,
        "candidate_tree": candidate_tree,
        "contract_version": "2.0.0",
        "durable_ledger_path": str(product / "factory/ledger.csv"),
        "kit_pin_sha256": file_digest(product / "factory/KIT_PIN"),
        "manifest_sha256": file_digest(product / "factory/QUALIFICATION.json"),
        "product_origin": lane["receipt"]["product_origin"],
        "product_path": str(product),
        "product_sha": lane["active"]["product_sha"],
        "product_tree": lane["active"]["product_tree"],
        "provider_database_path": str(lane["provider"] / "accounting/state-v2.sqlite3"),
        "receipt_sha256": file_digest(receipt_path),
        "runtime_ledger_path": lane["active"]["runtime_ledger_path"],
        "source_sha": source_sha,
        "source_tree": source_tree,
    }
    return immutable, module, lane, repo


def qualification_recovery_attempt(
    repo: Path, lane: dict[str, Any], ticket: str, run_id: str,
) -> dict[str, Any]:
    runs = lane["product"] / "factory/runs"
    request_path = runs / f"{run_id}.cancel-request.json"
    receipt_path = runs / f"{run_id}.cancel.json"
    if request_path.exists() or request_path.is_symlink():
        if not receipt_path.exists() or receipt_path.is_symlink():
            raise ReleaseError("qualification cancellation replay is incomplete")
        request = safe_state(request_path, "attempt cancellation request")
        nested = request.get("plan")
        receipt = qualification_attempt_cancel(repo, lane, [
            "receipt", "--factory-root", str(lane["product"]), "--ticket", ticket,
            "--run-id", run_id,
        ], "qualification cancellation receipt")
        if (
            not isinstance(nested, dict)
            or receipt.get("preview_hash") != nested.get("preview_hash")
        ):
            raise ReleaseError("qualification cancellation replay is invalid")
    elif receipt_path.exists() or receipt_path.is_symlink():
        raise ReleaseError("qualification cancellation replay is incomplete")
    else:
        nested = qualification_attempt_cancel(repo, lane, [
            "preview", "--factory-root", str(lane["product"]), "--ticket", ticket,
            "--run-id", run_id, "--reason", "operator_requested",
        ], "qualification cancellation preview")
    manifest = qualification_recovery_manifest(
        runs / f"{run_id}.meta",
    )
    provider_attempt = manifest.get("provider_attempt_id", "")
    if not SAFE_ID.fullmatch(provider_attempt):
        raise ReleaseError("qualification provider attempt identity is invalid")
    status = run_json([
        sys.executable, str(repo / "scripts/provider-coordinator.py"),
        "--db", str(lane["provider"] / "accounting/state-v2.sqlite3"),
        "status", "--attempt-id", provider_attempt,
    ], "qualification provider attempt status",
        environment=qualification_recovery_environment(lane), timeout=30)
    attempts = status.get("attempts")
    if (
        not isinstance(attempts, list) or len(attempts) != 1
        or not isinstance(attempts[0], dict)
        or attempts[0].get("attempt_id") != provider_attempt
    ):
        raise ReleaseError("qualification provider attempt identity is invalid")
    runtime = Path(lane["active"]["runtime_ledger_path"])
    claim = runtime.parent / ".active-runs" / f"{ticket}.{manifest.get('role', '')}.lock/owner"
    lease = lane["product"] / f"factory/.dispatch-leases/{ticket}.json"
    return {
        "active_claim_sha256": qualification_recovery_optional_digest(
            claim, "qualification active-run owner",
        ),
        "dispatch_lease_sha256": qualification_recovery_optional_digest(
            lease, "qualification dispatch lease",
        ),
        "nested_plan": nested,
        "provider_attempt": attempts[0],
        "provider_attempt_sha256": digest(attempts[0]),
        "runtime_ledger_row": qualification_recovery_row(runtime, run_id),
    }


def validate_qualification_recovery_plan(plan: dict[str, Any]) -> None:
    body = {key: value for key, value in plan.items() if key != "approval_sha256"}
    request = plan.get("request")
    if (
        set(plan) != {
            "approval_sha256", "attempt", "created_epoch", "expires_epoch",
            "identity", "request", "schema", "status",
        }
        or plan.get("schema") != QUALIFICATION_RECOVERY_PLAN_SCHEMA
        or plan.get("status") != "planned"
        or not DIGEST.fullmatch(str(plan.get("approval_sha256", "")))
        or plan["approval_sha256"] != digest(body)
        or not isinstance(request, dict)
        or set(request) != {
            "operator_id", "product", "project", "repo", "root", "sha",
            "ticket", "failed_run",
        }
        or not isinstance(plan.get("identity"), dict)
        or not isinstance(plan.get("attempt"), dict)
        or not isinstance(plan.get("created_epoch"), int)
        or not isinstance(plan.get("expires_epoch"), int)
        or plan["expires_epoch"] <= plan["created_epoch"]
    ):
        raise ReleaseError("qualification recovery plan is invalid")


def qualification_recovery_plan(args: argparse.Namespace) -> dict[str, Any]:
    identity, _module, lane, repo = qualification_recovery_identity(args)
    state = qualification_recovery_state(
        lane["root"], args.project, args.sha, args.ticket, args.failed_run,
    )
    latest = state / "latest.json"
    if latest.exists() or latest.is_symlink():
        plan = safe_state(latest, "qualification recovery plan")
        validate_qualification_recovery_plan(plan)
        if plan["request"] != {
            "operator_id": args.operator_id, "product": str(lane["product"]),
            "project": args.project, "repo": str(repo), "root": str(lane["root"]),
            "sha": args.sha, "ticket": args.ticket, "failed_run": args.failed_run,
        }:
            raise ReleaseError("qualification recovery plan belongs to another request")
        runs = lane["product"] / "factory/runs"
        begun = any(
            (runs / f"{args.failed_run}.{suffix}.json").exists()
            for suffix in ("cancel-request", "cancel")
        )
        if plan["expires_epoch"] > int(time.time()) or begun:
            return {"plan": plan, "schema": QUALIFICATION_RECOVERY_RESULT_SCHEMA,
                    "status": "approval_required"}
    now = int(time.time())
    plan = seal_plan({
        "attempt": qualification_recovery_attempt(repo, lane, args.ticket, args.failed_run),
        "created_epoch": now, "expires_epoch": now + 900,
        "identity": identity,
        "request": {
            "operator_id": args.operator_id, "product": str(lane["product"]),
            "project": args.project, "repo": str(repo), "root": str(lane["root"]),
            "sha": args.sha, "ticket": args.ticket, "failed_run": args.failed_run,
        },
        "schema": QUALIFICATION_RECOVERY_PLAN_SCHEMA, "status": "planned",
    })
    validate_qualification_recovery_plan(plan)
    immutable = state / "plans" / f"{plan['approval_sha256']}.json"
    if immutable.exists() or immutable.is_symlink():
        if safe_state(immutable, "qualification recovery plan") != plan:
            raise ReleaseError("qualification recovery plan conflicts")
    else:
        atomic_json(immutable, plan)
    atomic_json(latest, plan)
    return {"plan": plan, "schema": QUALIFICATION_RECOVERY_RESULT_SCHEMA,
            "status": "approval_required"}


def qualification_recovery_receipt(
    state: Path, plan: dict[str, Any], nested: dict[str, Any], nested_path: Path,
) -> dict[str, Any]:
    body = {
        "approval_sha256": plan["approval_sha256"],
        "candidate_sha": plan["identity"]["candidate_sha"],
        "nested_receipt_sha256": file_digest(nested_path),
        "nested_result": nested,
        "product_sha": plan["identity"]["product_sha"],
        "run_id": plan["request"]["failed_run"],
        "schema": QUALIFICATION_RECOVERY_RECEIPT_SCHEMA,
        "source_sha": plan["identity"]["source_sha"],
        "ticket": plan["request"]["ticket"],
    }
    receipt = {**body, "receipt_sha256": digest(body)}
    path = state / "receipt.json"
    if path.exists() or path.is_symlink():
        if safe_state(path, "qualification recovery receipt") != receipt:
            raise ReleaseError("qualification recovery receipt changed")
    else:
        atomic_json(path, receipt)
    return receipt


def qualification_recovery_apply(args: argparse.Namespace) -> dict[str, Any]:
    identity, module, lane, repo = qualification_recovery_identity(args)
    state = qualification_recovery_state(
        lane["root"], args.project, args.sha, args.ticket, args.failed_run,
    )
    if not DIGEST.fullmatch(args.approve_hash):
        raise ReleaseError("qualification recovery approval does not match")
    plan = safe_state(
        state / "plans" / f"{args.approve_hash}.json",
        "qualification recovery plan",
    )
    validate_qualification_recovery_plan(plan)
    if plan["approval_sha256"] != args.approve_hash:
        raise ReleaseError("qualification recovery approval does not match")
    if plan["request"] != {
        "operator_id": args.operator_id, "product": str(lane["product"]),
        "project": args.project, "repo": str(repo), "root": str(lane["root"]),
        "sha": args.sha, "ticket": args.ticket, "failed_run": args.failed_run,
    } or plan["identity"] != identity:
        raise ReleaseError("qualification recovery immutable inputs changed")
    controllers: list[int] = []
    admission: list[int] = []
    try:
        controllers = module.lock_controllers(lane["controller"])
        admission = module.lock_dispatch_admission(lane, lane)
        identity, _module, lane, repo = qualification_recovery_identity(args)
        if plan["identity"] != identity:
            raise ReleaseError("qualification recovery immutable inputs changed")
        runs = lane["product"] / "factory/runs"
        request_path = runs / f"{args.failed_run}.cancel-request.json"
        nested_receipt_path = runs / f"{args.failed_run}.cancel.json"
        begun = request_path.exists() and not request_path.is_symlink()
        completed = nested_receipt_path.exists() and not nested_receipt_path.is_symlink()
        if not begun and not completed:
            if plan["expires_epoch"] <= int(time.time()):
                raise ReleaseError("qualification recovery approval plan is stale")
            if qualification_recovery_attempt(
                repo, lane, args.ticket, args.failed_run,
            ) != plan["attempt"]:
                raise ReleaseError("qualification recovery attempt changed after approval")
        elif begun:
            request = safe_state(request_path, "attempt cancellation request")
            if request.get("plan") != plan["attempt"].get("nested_plan"):
                raise ReleaseError("attempt cancellation request belongs to another plan")
        nested_plan_path = state / "nested-plan.json"
        atomic_json(nested_plan_path, plan["attempt"]["nested_plan"])
        try:
            nested = qualification_attempt_cancel(repo, lane, [
                "apply", "--factory-root", str(lane["product"]),
                "--plan", str(nested_plan_path), "--preview-hash",
                plan["attempt"]["nested_plan"]["preview_hash"],
            ], "qualification cancellation apply")
        finally:
            nested_plan_path.unlink(missing_ok=True)
        receipt = qualification_recovery_receipt(
            state, plan, nested, nested_receipt_path,
        )
        return {"receipt": receipt, "schema": QUALIFICATION_RECOVERY_RESULT_SCHEMA,
                "status": "recovered"}
    finally:
        for descriptor in admission:
            os.close(descriptor)
        for descriptor in controllers:
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
    setup_parser.add_argument("--skip-optional-tests", action="store_true")
    resume_parser = commands.add_parser("resume")
    resume_parser.add_argument("--project", required=True)
    resume_parser.add_argument("--sha", required=True)
    resume_parser.add_argument("--approved-by", required=True)
    abort_parser = commands.add_parser("abort")
    abort_parser.add_argument("--project", required=True)
    abort_parser.add_argument("--sha", required=True)
    abort_parser.add_argument("--approved-by", required=True)
    qualification_parser = commands.add_parser("qualification-upgrade")
    qualification_parser.add_argument("--project", required=True)
    qualification_parser.add_argument("--root", required=True, type=Path)
    qualification_parser.add_argument("--product", required=True, type=Path)
    qualification_parser.add_argument("--repo", required=True, type=Path)
    qualification_parser.add_argument("--sha", required=True)
    qualification_parser.add_argument("--runtime-bin", required=True, type=Path)
    qualification_parser.add_argument("--operator-id", required=True)
    qualification_resume_parser = commands.add_parser("qualification-resume")
    qualification_resume_parser.add_argument("--project", required=True)
    qualification_resume_parser.add_argument("--sha", required=True)
    qualification_resume_parser.add_argument("--approved-by", required=True)
    for name in ("qualification-recover-plan", "qualification-recover-apply"):
        recovery_parser = commands.add_parser(name)
        recovery_parser.add_argument("--project", required=True)
        recovery_parser.add_argument("--root", required=True, type=Path)
        recovery_parser.add_argument("--product", required=True, type=Path)
        recovery_parser.add_argument("--repo", required=True, type=Path)
        recovery_parser.add_argument("--sha", required=True)
        recovery_parser.add_argument("--operator-id", required=True)
        recovery_parser.add_argument("--ticket", required=True)
        recovery_parser.add_argument("--failed-run", required=True)
        if name.endswith("apply"):
            recovery_parser.add_argument("--approve-hash", required=True)
    args = parser.parse_args()
    args.process_started = _PROCESS_STARTED
    qualification_command = args.command.startswith("qualification-")
    if qualification_command:
        def qualification_timeout(_signal: int, _frame: Any) -> None:
            raise ReleaseError("qualification migration exceeded 60 seconds")

        signal.signal(signal.SIGALRM, qualification_timeout)
        signal.setitimer(
            signal.ITIMER_REAL,
            max(0.001, QUALIFICATION_BUDGET_MS / 1000 - (time.monotonic() - _PROCESS_STARTED)),
        )
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
        elif args.command == "resume":
            result = resume(args)
        elif args.command == "abort":
            result = abort(args)
        elif args.command == "qualification-upgrade":
            if (
                not SAFE_ID.fullmatch(args.operator_id)
                or args.operator_id == "auto"
            ):
                raise ReleaseError("qualification migration operator is invalid")
            result = qualification_upgrade(args)
        elif args.command == "qualification-resume":
            result = qualification_resume(args)
        elif args.command == "qualification-recover-plan":
            result = qualification_recovery_plan(args)
        else:
            result = qualification_recovery_apply(args)
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return 0
    except (FileNotFoundError, OSError, ReleaseError, subprocess.SubprocessError) as error:
        result = {
            "error": str(error),
            "schema": (
                QUALIFICATION_RECOVERY_RESULT_SCHEMA
                if args.command.startswith("qualification-recover-")
                else QUALIFICATION_RESULT_SCHEMA if qualification_command else RESULT_SCHEMA
            ),
            "status": "error",
        }
        if isinstance(getattr(error, "reason_code", None), str):
            result["reason_code"] = error.reason_code
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return 2
    finally:
        if qualification_command:
            signal.setitimer(signal.ITIMER_REAL, 0)


if __name__ == "__main__":
    raise SystemExit(main())
