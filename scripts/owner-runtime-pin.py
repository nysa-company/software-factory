#!/usr/bin/env python3
"""Install and verify owner-local Node/npm/npx pins for the launcher PATH."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import uuid


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts/lib"))

from certification_plan import safe_plan, validate_plan  # noqa: E402


TOOLS = ("node", "npm", "npx")
SAFE_PATH_SUFFIX = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
PLAN_SCHEMA = "nysa.software-factory.owner-runtime-pin-plan/v1"
JOURNAL_SCHEMA = "nysa.software-factory.owner-runtime-pin-journal/v1"


class PinError(ValueError):
    pass


def canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def object_hash(value: object) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def secure_directory(path: Path, label: str) -> None:
    info = path.lstat()
    if (
        not path.is_absolute()
        or path.is_symlink()
        or not stat.S_ISDIR(info.st_mode)
        or path.resolve(strict=True) != path
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) & 0o022
    ):
        raise PinError(f"{label} is unsafe")


def executable(path: Path, label: str) -> Path:
    try:
        resolved = path.resolve(strict=True)
        info = resolved.stat()
    except OSError as error:
        raise PinError(f"{label} is missing") from error
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_IMODE(info.st_mode) & 0o022
        or not os.access(resolved, os.X_OK)
    ):
        raise PinError(f"{label} is unsafe")
    return resolved


def version(command: str, path: str, home: Path) -> str:
    try:
        result = subprocess.run(
            [command, "--version"],
            check=False,
            capture_output=True,
            env={"HOME": str(home), "PATH": path},
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def expected_runtime(product: Path) -> dict[str, str]:
    plan_path = product / "factory/certification-plan.json"
    try:
        plan, _ = safe_plan(plan_path)
        validate_plan(plan, product)
    except (OSError, TypeError, UnicodeError, ValueError) as error:
        raise PinError("product certification plan is invalid") from error
    return {**plan["runtime"], "npx": plan["runtime"]["npm"]}


def verify_runtime(
    expected: dict[str, str], commands: dict[str, str], path: str, home: Path
) -> dict[str, str]:
    observed = {tool: version(commands[tool], path, home) for tool in TOOLS}
    for tool in TOOLS:
        if observed[tool] != expected[tool]:
            raise PinError(
                f"runtime mismatch for {tool}: expected {expected[tool]}, "
                f"observed {observed[tool] or 'missing'}"
            )
    return observed


def atomic_link(path: Path, target: Path) -> None:
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}"
    try:
        temporary.symlink_to(target)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def atomic_json(path: Path, value: dict[str, object]) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(canonical(value))
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


def safe_json(path: Path, label: str) -> dict[str, object]:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_size > 1_000_000
        ):
            raise PinError(f"{label} is unsafe")
        with os.fdopen(descriptor, encoding="utf-8") as stream:
            descriptor = -1
            value = json.load(stream)
    except (json.JSONDecodeError, UnicodeError) as error:
        raise PinError(f"{label} is invalid") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not isinstance(value, dict):
        raise PinError(f"{label} is invalid")
    return value


def current_links(target: Path) -> dict[str, str | None]:
    if target.exists() or target.is_symlink():
        secure_directory(target, "runtime target")
    values: dict[str, str | None] = {}
    for tool in TOOLS:
        path = target / tool
        if path.is_symlink():
            if path.lstat().st_uid != os.geteuid():
                raise PinError(f"existing {tool} pin is unsafe")
            values[tool] = os.readlink(path)
        elif path.exists():
            raise PinError(f"existing {tool} pin is not a symlink")
        else:
            values[tool] = None
    return values


def transaction_plan(product: Path, runtime_bin: Path, target: Path) -> dict[str, object]:
    product = product.resolve(strict=True)
    secure_directory(product, "product")
    runtime_bin = runtime_bin.resolve(strict=True)
    secure_directory(runtime_bin, "runtime bin")
    if not target.is_absolute() or target.parent.resolve(strict=True) != target.parent:
        raise PinError("runtime target path is unsafe")
    secure_directory(target.parent, "runtime target parent")
    expected = expected_runtime(product)
    sources = {
        tool: executable(runtime_bin / tool, f"runtime {tool}") for tool in TOOLS
    }
    source_path = f"{runtime_bin}:{SAFE_PATH_SUFFIX}"
    verify_runtime(
        expected, {tool: str(sources[tool]) for tool in TOOLS}, source_path,
        Path(os.environ["HOME"]),
    )
    previous = current_links(target)
    candidate = {
        tool: {"path": str(sources[tool]), "sha256": file_hash(sources[tool])}
        for tool in TOOLS
    }
    body: dict[str, object] = {
        "action": "reuse" if all(
            previous[tool] == str(sources[tool]) for tool in TOOLS
        ) else "install",
        "candidate": candidate,
        "certification_plan_sha256": file_hash(
            product / "factory/certification-plan.json"
        ),
        "expected": expected,
        "previous": previous,
        "product_path": str(product),
        "runtime_bin": str(runtime_bin),
        "schema": PLAN_SCHEMA,
        "target_bin": str(target),
    }
    return {**body, "approval_sha256": object_hash(body)}


def validate_transaction_plan(value: dict[str, object]) -> None:
    keys = {
        "action", "approval_sha256", "candidate", "expected", "previous",
        "certification_plan_sha256", "product_path", "runtime_bin", "schema",
        "target_bin",
    }
    body = {key: item for key, item in value.items() if key != "approval_sha256"}
    if (
        set(value) != keys
        or value.get("schema") != PLAN_SCHEMA
        or value.get("action") not in {"install", "reuse"}
        or value.get("approval_sha256") != object_hash(body)
    ):
        raise PinError("runtime plan approval hash is invalid")


def signed_journal(value: dict[str, object]) -> dict[str, object]:
    body = {key: item for key, item in value.items() if key != "record_sha256"}
    return {**body, "record_sha256": object_hash(body)}


def valid_journal(value: dict[str, object], plan: dict[str, object]) -> None:
    body = {key: item for key, item in value.items() if key != "record_sha256"}
    if (
        set(value) != {"plan", "record_sha256", "schema", "status"}
        or value.get("schema") != JOURNAL_SCHEMA
        or value.get("plan") != plan
        or value.get("status") not in {"applying", "completed"}
        or value.get("record_sha256") != object_hash(body)
    ):
        raise PinError("runtime pin journal is invalid")


def apply_transaction(plan_path: Path, approval: str) -> dict[str, object]:
    plan = safe_json(plan_path, "runtime plan")
    validate_transaction_plan(plan)
    if approval != plan["approval_sha256"]:
        raise PinError("approved hash does not match exact runtime plan")
    product = Path(str(plan["product_path"]))
    runtime_bin = Path(str(plan["runtime_bin"]))
    target = Path(str(plan["target_bin"]))
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    secure_directory(target.parent, "runtime target parent")
    lock_path = target.parent / ".runtime-pin.lock"
    descriptor = os.open(
        lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600,
    )
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o077
        ):
            raise PinError("runtime pin lock is unsafe")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        journal_path = target.parent / "runtime-pin-journal.json"
        journal = None
        if journal_path.exists() or journal_path.is_symlink():
            journal = safe_json(journal_path, "runtime pin journal")
            existing_plan = journal.get("plan")
            if not isinstance(existing_plan, dict):
                raise PinError("runtime pin journal is invalid")
            valid_journal(journal, existing_plan)
            if existing_plan != plan:
                if journal.get("status") != "completed":
                    raise PinError("runtime pin transaction is incomplete")
                current = transaction_plan(product, runtime_bin, target)
                if current != plan:
                    raise PinError("runtime pin compare-and-swap conflict")
                journal = None
        if journal is None:
            current = transaction_plan(product, runtime_bin, target)
            if current != plan:
                raise PinError("runtime pin compare-and-swap conflict")
            atomic_json(journal_path, signed_journal({
                "plan": plan, "schema": JOURNAL_SCHEMA, "status": "applying",
            }))
        candidate = plan["candidate"]
        previous = plan["previous"]
        if not isinstance(candidate, dict) or not isinstance(previous, dict):
            raise PinError("runtime plan approval hash is invalid")
        if not target.exists():
            target.mkdir(mode=0o700)
        secure_directory(target, "runtime target")
        for tool in TOOLS:
            item = candidate.get(tool)
            if not isinstance(item, dict) or set(item) != {"path", "sha256"}:
                raise PinError("runtime plan approval hash is invalid")
            source = executable(Path(str(item["path"])), f"runtime {tool}")
            if file_hash(source) != item["sha256"]:
                raise PinError("runtime candidate changed after approval")
            path = target / tool
            current = os.readlink(path) if path.is_symlink() else None
            if path.exists() and not path.is_symlink():
                raise PinError("runtime pin compare-and-swap conflict")
            if current not in {previous.get(tool), str(source)}:
                raise PinError("runtime pin compare-and-swap conflict")
            if current != str(source):
                atomic_link(path, source)
            if os.environ.get("FACTORY_RUNTIME_PIN_TEST_FAIL_AFTER_TOOL") == tool:
                if os.environ.get("FACTORY_RUNTIME_PIN_TEST_MODE") != "1":
                    raise PinError("runtime pin fault injection is forbidden")
                raise PinError(f"injected runtime pin interruption after {tool}")
        observed = verify_runtime(
            plan["expected"], {tool: str(target / tool) for tool in TOOLS},
            f"{target}:{SAFE_PATH_SUFFIX}", Path(os.environ["HOME"]),
        )
        replay = journal is not None and journal.get("status") == "completed"
        atomic_json(journal_path, signed_journal({
            "plan": plan, "schema": JOURNAL_SCHEMA, "status": "completed",
        }))
        return {
            **observed, "path": str(target),
            "schema": "nysa.software-factory.owner-runtime-pin/v2",
            "status": "replayed" if replay else "applied",
        }
    finally:
        os.close(descriptor)


def check_transaction(journal_path: Path) -> dict[str, object]:
    journal = safe_json(journal_path, "runtime pin journal")
    plan = journal.get("plan")
    if not isinstance(plan, dict):
        raise PinError("runtime pin journal is invalid")
    valid_journal(journal, plan)
    if journal.get("status") != "completed":
        raise PinError("runtime pin transaction is incomplete")
    validate_transaction_plan(plan)
    product = Path(str(plan["product_path"]))
    target = Path(str(plan["target_bin"]))
    if (
        expected_runtime(product) != plan["expected"]
        or file_hash(product / "factory/certification-plan.json")
        != plan["certification_plan_sha256"]
    ):
        raise PinError("runtime declaration changed after approval")
    candidate = plan["candidate"]
    if not isinstance(candidate, dict):
        raise PinError("runtime plan approval hash is invalid")
    commands: dict[str, str] = {}
    for tool in TOOLS:
        item = candidate.get(tool)
        if not isinstance(item, dict) or set(item) != {"path", "sha256"}:
            raise PinError("runtime plan approval hash is invalid")
        source = executable(Path(str(item["path"])), f"runtime {tool}")
        path = target / tool
        if (
            file_hash(source) != item["sha256"]
            or not path.is_symlink()
            or path.lstat().st_uid != os.geteuid()
            or os.readlink(path) != str(source)
        ):
            raise PinError("runtime pin compare-and-swap conflict")
        commands[tool] = str(path)
    observed = verify_runtime(
        plan["expected"], commands, f"{target}:{SAFE_PATH_SUFFIX}",
        Path(os.environ["HOME"]),
    )
    return {
        **observed, "path": str(target),
        "schema": "nysa.software-factory.owner-runtime-pin-check/v1",
        "status": "ready",
    }


def restore(path: Path, previous: str | None) -> None:
    if previous is None:
        path.unlink(missing_ok=True)
    else:
        atomic_link(path, Path(previous))


def pin(product: Path, runtime_bin: Path, home: Path) -> dict[str, str]:
    secure_directory(home, "HOME")
    product = product.resolve(strict=True)
    secure_directory(product, "product")
    runtime_bin = runtime_bin.resolve(strict=True)
    secure_directory(runtime_bin, "runtime bin")

    expected = expected_runtime(product)
    sources = {
        tool: executable(runtime_bin / tool, f"runtime {tool}") for tool in TOOLS
    }
    source_path = f"{runtime_bin}:{SAFE_PATH_SUFFIX}"
    verify_runtime(
        expected,
        {tool: str(sources[tool]) for tool in TOOLS},
        source_path,
        home,
    )

    factory = home / ".factory"
    factory.mkdir(mode=0o700, exist_ok=True)
    secure_directory(factory, "owner Factory directory")
    target_bin = factory / "bin"
    target_bin.mkdir(mode=0o700, exist_ok=True)
    secure_directory(target_bin, "owner launcher bin")

    lock_path = factory / ".runtime-pin.lock"
    descriptor = os.open(
        lock_path,
        os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) & 0o077
        ):
            raise PinError("runtime pin lock is unsafe")
        fcntl.flock(descriptor, fcntl.LOCK_EX)

        previous: dict[str, str | None] = {}
        for tool in TOOLS:
            target = target_bin / tool
            if target.is_symlink():
                if target.lstat().st_uid != os.geteuid():
                    raise PinError(f"existing {tool} pin is unsafe")
                previous[tool] = os.readlink(target)
            elif target.exists():
                raise PinError(f"existing {tool} pin is not a symlink")
            else:
                previous[tool] = None

        try:
            for tool in TOOLS:
                atomic_link(target_bin / tool, sources[tool])
            safe_path = f"{target_bin}:{SAFE_PATH_SUFFIX}"
            observed = verify_runtime(
                expected, {tool: tool for tool in TOOLS}, safe_path, home
            )
        except Exception:
            for tool in TOOLS:
                restore(target_bin / tool, previous[tool])
            raise
    finally:
        os.close(descriptor)

    return observed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command")
    planned = commands.add_parser("plan")
    planned.add_argument("--product", required=True, type=Path)
    planned.add_argument("--runtime-bin", required=True, type=Path)
    planned.add_argument("--target-bin", required=True, type=Path)
    applied = commands.add_parser("apply")
    applied.add_argument("--plan", required=True, type=Path)
    applied.add_argument("--approve-hash", required=True)
    checked = commands.add_parser("check")
    checked.add_argument("--journal", required=True, type=Path)
    if sys.argv[1:2] not in (["plan"], ["apply"], ["check"]):
        parser.add_argument("--product", required=True, type=Path)
        parser.add_argument("--runtime-bin", required=True, type=Path)
    args = parser.parse_args()
    try:
        home_raw = os.environ.get("HOME", "")
        if not home_raw or not Path(home_raw).is_absolute():
            raise PinError("HOME must be absolute")
        home = Path(home_raw)
        if args.command == "plan":
            print(json.dumps(
                transaction_plan(args.product, args.runtime_bin, args.target_bin),
                sort_keys=True, separators=(",", ":"),
            ))
            return 0
        if args.command == "apply":
            print(json.dumps(
                apply_transaction(args.plan, args.approve_hash),
                sort_keys=True, separators=(",", ":"),
            ))
            return 0
        if args.command == "check":
            print(json.dumps(
                check_transaction(args.journal),
                sort_keys=True, separators=(",", ":"),
            ))
            return 0
        observed = pin(args.product, args.runtime_bin, home)
    except (OSError, RuntimeError, PinError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(json.dumps(
        {
            **observed,
            "path": str(home / ".factory/bin"),
            "schema": "nysa.software-factory.owner-runtime-pin/v1",
            "status": "ready",
        },
        sort_keys=True,
        separators=(",", ":"),
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
