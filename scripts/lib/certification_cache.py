"""Authenticated cross-workspace certification artifact cache."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import platform
import re
import secrets
import stat
import tempfile
import time
from typing import Any

from certification_plan import safe_plan, strict_tuple, validate_plan


CANDIDATE_SCHEMA = "nysa.software-factory.certification-artifact-candidate/v1"
ENTRY_SCHEMA = "nysa.software-factory.certification-artifact/v1"
DIGEST = re.compile(r"^[0-9a-f]{64}$")
SHA = re.compile(r"^[0-9a-f]{40}$")
TTL_SECONDS = 86_400
MAX_ENTRIES = 32
MAX_FILES = 10_000
MAX_FILE_BYTES = 100_000_000
MAX_ENTRY_BYTES = 500_000_000


class CacheError(ValueError):
    pass


def canonical(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode()


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def safe_directory(path: Path, *, create: bool = False) -> Path:
    if create and not path.exists() and not path.is_symlink():
        path.mkdir(mode=0o700, parents=True)
    info = path.lstat()
    if (
        path.is_symlink()
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) & 0o077
    ):
        raise CacheError("certification artifact cache directory is unsafe")
    return path


def _safe_file(
    path: Path, limit: int = MAX_FILE_BYTES, *, owner_only: bool = True,
) -> tuple[os.stat_result, bytes]:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
            or (owner_only and stat.S_IMODE(info.st_mode) & 0o077)
            or info.st_size > limit
        ):
            raise CacheError("certification artifact cache file is unsafe")
        chunks = []
        total = 0
        while raw := os.read(descriptor, min(1_048_576, limit + 1 - total)):
            chunks.append(raw)
            total += len(raw)
            if total > limit:
                raise CacheError("certification artifact cache file is oversized")
        return info, b"".join(chunks)
    finally:
        os.close(descriptor)


def _json(path: Path) -> dict[str, Any]:
    _, raw = _safe_file(path, 1_000_000)
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise CacheError("certification artifact cache record is malformed") from error
    if not isinstance(value, dict):
        raise CacheError("certification artifact cache record is malformed")
    return value


def _relative(value: str) -> Path:
    if not isinstance(value, str):
        raise CacheError("certification artifact cache path is invalid")
    path = Path(value)
    if (
        not value
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise CacheError("certification artifact cache path is invalid")
    return path


def _write_file(path: Path, raw: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        view = memoryview(raw)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
        os.fchmod(descriptor, mode)
    finally:
        os.close(descriptor)


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(canonical(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        Path(temporary).unlink(missing_ok=True)


def _copy_regular(
    source: Path, destination: Path, *, source_owner_only: bool = True,
) -> tuple[int, int, str]:
    info, raw = _safe_file(source, owner_only=source_owner_only)
    _write_file(destination, raw)
    return stat.S_IMODE(info.st_mode), len(raw), digest(raw)


def _private_parent(root: Path, target: Path) -> None:
    missing = []
    cursor = target.parent
    while cursor != root:
        missing.append(cursor)
        cursor = cursor.parent
    for directory in reversed(missing):
        if not directory.exists() and not directory.is_symlink():
            directory.mkdir(mode=0o700)
        safe_directory(directory)


def _safe_remove(path: Path) -> None:
    if not path.exists() and not path.is_symlink():
        return
    info = path.lstat()
    if path.is_symlink() or not stat.S_ISDIR(info.st_mode):
        raise CacheError("certification artifact cache entry is unsafe")
    for child in sorted(path.iterdir(), reverse=True):
        child_info = child.lstat()
        if child.is_symlink():
            raise CacheError("certification artifact cache entry contains a symlink")
        if stat.S_ISDIR(child_info.st_mode):
            _safe_remove(child)
        elif stat.S_ISREG(child_info.st_mode) and child_info.st_nlink == 1:
            child.chmod(stat.S_IMODE(child_info.st_mode) | stat.S_IWUSR)
            child.unlink()
        else:
            raise CacheError("certification artifact cache entry is unsafe")
    path.chmod(stat.S_IMODE(info.st_mode) | stat.S_IRWXU)
    path.rmdir()


def _artifact_entries(root: Path, artifacts: list[str]) -> list[dict[str, Any]]:
    entries: dict[str, dict[str, Any]] = {}
    count = 0
    total = 0
    for declared in artifacts:
        relative = _relative(declared)
        source = root / relative
        if not source.exists() and not source.is_symlink():
            raise CacheError(f"certification artifact is missing: {declared}")
        candidates = [source]
        if source.is_dir() and not source.is_symlink():
            candidates.extend(sorted(source.rglob("*"), key=lambda item: item.as_posix()))
        for candidate in candidates:
            name = candidate.relative_to(root).as_posix()
            if name in entries:
                raise CacheError("certification artifacts overlap")
            info = candidate.lstat()
            mode = stat.S_IMODE(info.st_mode)
            if candidate.is_symlink():
                raise CacheError("certification artifact is unsafe")
            if stat.S_ISDIR(info.st_mode):
                entries[name] = {"mode": mode, "path": name, "type": "directory"}
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise CacheError("certification artifact is unsafe")
            _, raw = _safe_file(candidate, owner_only=False)
            count += 1
            total += len(raw)
            if count > MAX_FILES or total > MAX_ENTRY_BYTES:
                raise CacheError("certification artifact cache entry is oversized")
            entries[name] = {
                "mode": mode,
                "path": name,
                "sha256": digest(raw),
                "size": len(raw),
                "type": "file",
            }
    return [entries[name] for name in sorted(entries)]


def _manifest_digest(entries: list[dict[str, Any]], artifacts: list[str]) -> str:
    selected = []
    for declared in artifacts:
        name = _relative(declared).as_posix()
        selected.extend(
            item for item in entries
            if item["path"] == name or item["path"].startswith(f"{name}/")
        )
    return digest(canonical(sorted(selected, key=lambda item: item["path"])))


def _validate_common(value: dict[str, Any], entry: Path) -> dict[str, Any]:
    phase = value.get("phase")
    context = value.get("context")
    dependencies = value.get("dependencies")
    entries = value.get("entries")
    if (
        not isinstance(phase, dict)
        or phase.get("reuse") != "artifacts"
        or not isinstance(phase.get("artifacts"), list)
        or not phase["artifacts"]
        or not isinstance(context, dict)
        or not isinstance(context.get("runner_runtime"), dict)
        or set(context["runner_runtime"]) != {"architecture", "os", "python"}
        or not isinstance(dependencies, dict)
        or not all(isinstance(name, str) and DIGEST.fullmatch(item)
                   for name, item in dependencies.items())
        or not DIGEST.fullmatch(value.get("input_sha256", ""))
        or not DIGEST.fullmatch(value.get("artifact_sha256", ""))
        or not DIGEST.fullmatch(value.get("output_sha256", ""))
        or isinstance(value.get("phase_wall_seconds"), bool)
        or not isinstance(value.get("phase_wall_seconds"), (int, float))
        or value["phase_wall_seconds"] < 0
        or not isinstance(value.get("network_granted"), bool)
        or (
            phase.get("network") == "denied"
            and value["network_granted"]
        )
        or (
            phase.get("network") == "required"
            and not value["network_granted"]
        )
        or not isinstance(entries, list)
        or len(entries) > MAX_FILES * 2
    ):
        raise CacheError("certification artifact cache record is invalid")
    names: set[str] = set()
    expected_files = {"record.json"}
    expected_directories = {"artifacts"}
    total = 0
    for item in entries:
        if not isinstance(item, dict) or item.get("type") not in {"directory", "file"}:
            raise CacheError("certification artifact manifest is invalid")
        relative = _relative(item.get("path", ""))
        name = relative.as_posix()
        mode = item.get("mode")
        if (
            name in names
            or type(mode) is not int
            or not 0 <= mode <= 0o7777
        ):
            raise CacheError("certification artifact manifest is invalid")
        names.add(name)
        target = entry / "artifacts" / relative
        if item["type"] == "directory":
            if set(item) != {"mode", "path", "type"}:
                raise CacheError("certification artifact manifest is invalid")
            safe_directory(target)
            expected_directories.add((Path("artifacts") / relative).as_posix())
        else:
            if (
                set(item) != {"mode", "path", "sha256", "size", "type"}
                or type(item.get("size")) is not int
                or not 0 <= item["size"] <= MAX_FILE_BYTES
                or not DIGEST.fullmatch(item.get("sha256", ""))
            ):
                raise CacheError("certification artifact manifest is invalid")
            _, raw = _safe_file(target)
            if len(raw) != item["size"] or digest(raw) != item["sha256"]:
                raise CacheError("certification artifact cache bytes are invalid")
            total += len(raw)
            expected_files.add((Path("artifacts") / relative).as_posix())
        for parent in (Path("artifacts") / relative).parents:
            if parent != Path("."):
                expected_directories.add(parent.as_posix())
    if (
        any(_relative(item).as_posix() not in names for item in phase["artifacts"])
        or total > MAX_ENTRY_BYTES
        or _manifest_digest(entries, phase["artifacts"])
        != value["artifact_sha256"]
    ):
        raise CacheError("certification artifact cache bytes are invalid")
    observed_files: set[str] = set()
    observed_directories: set[str] = set()
    for directory, names_in_dir, files in os.walk(entry, followlinks=False):
        base = Path(directory)
        for name in names_in_dir:
            path = base / name
            if path.is_symlink():
                raise CacheError("certification artifact cache entry contains a symlink")
            safe_directory(path)
            observed_directories.add(path.relative_to(entry).as_posix())
        for name in files:
            path = base / name
            _safe_file(path)
            observed_files.add(path.relative_to(entry).as_posix())
    if observed_files != expected_files or observed_directories != expected_directories:
        raise CacheError("certification artifact cache entry is incomplete")
    return value


def load_candidate(entry: Path) -> dict[str, Any]:
    safe_directory(entry)
    value = _json(entry / "record.json")
    if (
        set(value) != {
            "artifact_sha256", "context", "dependencies", "entries",
            "input_sha256", "network_granted", "output_sha256", "phase",
            "phase_wall_seconds", "schema",
        }
        or value.get("schema") != CANDIDATE_SCHEMA
    ):
        raise CacheError("certification artifact candidate is invalid")
    return _validate_common(value, entry)


def load_entry(entry: Path, secret: bytes | None = None) -> dict[str, Any]:
    safe_directory(entry)
    value = _json(entry / "record.json")
    authentication = value.pop("authentication_sha256", "")
    if secret is not None and not hmac.compare_digest(
        authentication, hmac.new(secret, canonical(value), hashlib.sha256).hexdigest()
    ):
        raise CacheError("certification artifact cache authentication is invalid")
    if (
        set(value) != {
            "artifact_sha256", "context", "created_epoch", "dependencies",
            "entries", "expires_epoch", "input_sha256", "output_sha256",
            "network_granted", "phase", "phase_wall_seconds", "schema",
        }
        or value.get("schema") != ENTRY_SCHEMA
        or not DIGEST.fullmatch(authentication)
        or type(value.get("created_epoch")) is not int
        or type(value.get("expires_epoch")) is not int
        or value["created_epoch"] <= 0
        or value["expires_epoch"] != value["created_epoch"] + TTL_SECONDS
    ):
        raise CacheError("certification artifact cache record is invalid")
    value["authentication_sha256"] = authentication
    return _validate_common(value, entry)


def stage_phase(
    root: Path,
    log: Path,
    phase: dict[str, Any],
    context: dict[str, Any],
    dependencies: dict[str, str],
    input_sha256: str,
    network_granted: bool,
    artifact_sha256: str,
    output_sha256: str,
    phase_wall_seconds: float,
    destination: Path,
) -> None:
    safe_directory(destination, create=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{input_sha256}.", dir=destination))
    try:
        artifacts_root = temporary / "artifacts"
        artifacts_root.mkdir(mode=0o700)
        entries = _artifact_entries(root, phase["artifacts"])
        for item in entries:
            target = artifacts_root / _relative(item["path"])
            _private_parent(artifacts_root, target)
            if item["type"] == "directory":
                target.mkdir(mode=0o700, parents=True, exist_ok=True)
            else:
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                _copy_regular(
                    root / _relative(item["path"]), target,
                    source_owner_only=False,
                )
        value = {
            "artifact_sha256": artifact_sha256,
            "context": context,
            "dependencies": dependencies,
            "entries": entries,
            "input_sha256": input_sha256,
            "network_granted": network_granted,
            "output_sha256": output_sha256,
            "phase": phase,
            "phase_wall_seconds": phase_wall_seconds,
            "schema": CANDIDATE_SCHEMA,
        }
        _atomic_json(temporary / "record.json", value)
        load_candidate(temporary)
        target = destination / input_sha256
        if target.exists() or target.is_symlink():
            _safe_remove(target)
        os.replace(temporary, target)
    finally:
        if temporary.exists() or temporary.is_symlink():
            _safe_remove(temporary)


def restore_phase(
    root: Path,
    log: Path,
    phase: dict[str, Any],
    context: dict[str, Any],
    dependencies: dict[str, str],
    input_sha256: str,
    network_granted: bool,
    source: Path,
) -> dict[str, Any] | None:
    try:
        entry = source / input_sha256
        value = load_entry(entry)
        if (
            value["expires_epoch"] <= int(time.time())
            or value["context"] != context
            or value["dependencies"] != dependencies
            or value["phase"] != phase
            or value["input_sha256"] != input_sha256
            or value["network_granted"] != network_granted
            or any((root / _relative(item)).exists() or (root / _relative(item)).is_symlink()
                   for item in phase["artifacts"])
        ):
            return None
    except (CacheError, FileExistsError, FileNotFoundError, OSError):
        return None
    for declared in phase["artifacts"]:
        relative = _relative(declared)
        cursor = root
        for part in relative.parts[:-1]:
            cursor /= part
            if not cursor.exists() and not cursor.is_symlink():
                cursor.mkdir(mode=0o700)
            info = cursor.lstat()
            if cursor.is_symlink() or not stat.S_ISDIR(info.st_mode):
                raise CacheError("certification artifact restore path is unsafe")
    temporary = Path(tempfile.mkdtemp(prefix=".certification-cache-restore.", dir=root))
    try:
        for item in value["entries"]:
            target = temporary / _relative(item["path"])
            if item["type"] == "directory":
                target.mkdir(mode=0o700, parents=True, exist_ok=True)
            else:
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                _, raw = _safe_file(entry / "artifacts" / _relative(item["path"]))
                _write_file(target, raw, item["mode"])
        for item in sorted(
            (entry for entry in value["entries"] if entry["type"] == "directory"),
            key=lambda entry: len(Path(entry["path"]).parts),
            reverse=True,
        ):
            (temporary / _relative(item["path"])).chmod(item["mode"])
        for declared in phase["artifacts"]:
            relative = _relative(declared)
            os.replace(temporary / relative, root / relative)
        _write_file(log, b"persistent certification artifact cache hit\n")
        return value
    finally:
        if temporary.exists() or temporary.is_symlink():
            _safe_remove(temporary)


def _key(store: Path, *, create: bool) -> bytes | None:
    path = store / "authentication.key"
    if not path.exists() and not path.is_symlink():
        if not create:
            return None
        _write_file(path, secrets.token_bytes(32))
    _, raw = _safe_file(path, 32)
    if len(raw) != 32:
        raise CacheError("certification artifact cache key is invalid")
    return raw


class _Lock:
    def __init__(self, store: Path) -> None:
        self.path = store / ".lock"
        self.descriptor = -1

    def __enter__(self) -> None:
        self.descriptor = os.open(
            self.path,
            os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        info = os.fstat(self.descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            os.close(self.descriptor)
            raise CacheError("certification artifact cache lock is unsafe")
        fcntl.flock(self.descriptor, fcntl.LOCK_EX)

    def __exit__(self, *_args: Any) -> None:
        os.close(self.descriptor)


def _copy_entry(source: Path, destination: Path, record: dict[str, Any]) -> None:
    if destination.exists() or destination.is_symlink():
        safe_directory(destination)
        if any(destination.iterdir()):
            raise CacheError("certification artifact cache destination is not empty")
    else:
        destination.mkdir(mode=0o700)
    (destination / "artifacts").mkdir(mode=0o700)
    for item in record["entries"]:
        target = destination / "artifacts" / _relative(item["path"])
        _private_parent(destination / "artifacts", target)
        if item["type"] == "directory":
            target.mkdir(mode=0o700, parents=True, exist_ok=True)
        else:
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            _copy_regular(source / "artifacts" / _relative(item["path"]), target)
    _atomic_json(destination / "record.json", record)


def _readonly(path: Path) -> None:
    for directory, names, files in os.walk(path, topdown=False, followlinks=False):
        for name in files:
            item = Path(directory) / name
            item.chmod(stat.S_IMODE(item.lstat().st_mode) & ~0o200)
        for name in names:
            item = Path(directory) / name
            item.chmod(stat.S_IMODE(item.lstat().st_mode) & ~0o200)
    path.chmod(stat.S_IMODE(path.lstat().st_mode) & ~0o200)


def prepare(store: Path, destination: Path) -> None:
    safe_directory(store)
    with _Lock(store):
        secret = _key(store, create=False)
        if destination.exists() or destination.is_symlink():
            _safe_remove(destination)
        destination.mkdir(mode=0o700, parents=True)
        if secret is None:
            _readonly(destination)
            return
        entries = safe_directory(store / "entries", create=True)
        valid: list[tuple[int, Path, dict[str, Any]]] = []
        now = int(time.time())
        for item in sorted(entries.iterdir()):
            if not DIGEST.fullmatch(item.name):
                continue
            try:
                record = load_entry(item, secret)
                if record["input_sha256"] != item.name:
                    raise CacheError("certification artifact cache key is invalid")
                if record["expires_epoch"] <= now:
                    _safe_remove(item)
                    continue
                valid.append((record["created_epoch"], item, record))
            except (CacheError, FileNotFoundError, OSError):
                continue
        for _, item, _ in sorted(valid)[:-MAX_ENTRIES]:
            _safe_remove(item)
        for _, item, record in sorted(valid)[-MAX_ENTRIES:]:
            _copy_entry(item, destination / item.name, record)
        _readonly(destination)


def publish(
    store: Path,
    source: Path,
    plan_path: Path,
    expected_context: dict[str, Any],
) -> None:
    safe_directory(store)
    if not source.exists() and not source.is_symlink():
        return
    safe_directory(source)
    plan, plan_sha256 = safe_plan(plan_path)
    phases = validate_plan(plan, plan_path.parent.parent)
    if expected_context["plan_sha256"] != plan_sha256:
        raise CacheError("certification artifact cache plan drifted")
    with _Lock(store):
        secret = _key(store, create=True)
        assert secret is not None
        entries = safe_directory(store / "entries", create=True)
        for item in sorted(source.iterdir()):
            if not DIGEST.fullmatch(item.name):
                raise CacheError("certification artifact candidate name is invalid")
            candidate = load_candidate(item)
            if candidate["input_sha256"] != item.name:
                raise CacheError("certification artifact candidate key is invalid")
            context = candidate["context"]
            if (
                set(context) != set(expected_context) | {"runner_runtime"}
                or any(context.get(name) != value for name, value in expected_context.items())
                or candidate["phase"] != phases.get(candidate["phase"].get("name"))
            ):
                raise CacheError("certification artifact candidate inputs drifted")
            now = int(time.time())
            record = {
                **candidate,
                "created_epoch": now,
                "expires_epoch": now + TTL_SECONDS,
                "schema": ENTRY_SCHEMA,
            }
            record["authentication_sha256"] = hmac.new(
                secret, canonical(record), hashlib.sha256
            ).hexdigest()
            temporary = Path(tempfile.mkdtemp(prefix=f".{item.name}.", dir=entries))
            target = entries / item.name
            try:
                _copy_entry(item, temporary, record)
                load_entry(temporary, secret)
                if target.exists() or target.is_symlink():
                    _safe_remove(target)
                os.replace(temporary, target)
            finally:
                if temporary.exists() or temporary.is_symlink():
                    _safe_remove(temporary)
        valid = []
        for item in entries.iterdir():
            if DIGEST.fullmatch(item.name):
                try:
                    record = load_entry(item, secret)
                    valid.append((record["created_epoch"], item))
                except (CacheError, FileNotFoundError, OSError):
                    pass
        for _, item in sorted(valid)[:-MAX_ENTRIES]:
            _safe_remove(item)


def _context(args: argparse.Namespace) -> dict[str, Any]:
    tuple_value = strict_tuple(json.loads(args.runtime_tuple))
    values = {
        "contract_version": args.contract_version,
        "factory_sha": args.factory_sha,
        "factory_tree": args.factory_tree,
        "product_sha": args.product_sha,
        "product_tree": args.product_tree,
    }
    if tuple_value != {**values, "node": tuple_value["node"], "npm": tuple_value["npm"]}:
        raise CacheError("certification artifact runtime tuple drifted")
    _, plan_sha256 = safe_plan(args.plan)
    return {**values, "plan_sha256": plan_sha256, "runtime_tuple": tuple_value}


def main() -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="action", required=True)
    prepare_parser = commands.add_parser("prepare")
    prepare_parser.add_argument("--store", required=True, type=Path)
    prepare_parser.add_argument("--destination", required=True, type=Path)
    publish_parser = commands.add_parser("publish")
    publish_parser.add_argument("--store", required=True, type=Path)
    publish_parser.add_argument("--source", required=True, type=Path)
    publish_parser.add_argument("--plan", required=True, type=Path)
    publish_parser.add_argument("--factory-sha", required=True)
    publish_parser.add_argument("--factory-tree", required=True)
    publish_parser.add_argument("--product-sha", required=True)
    publish_parser.add_argument("--product-tree", required=True)
    publish_parser.add_argument("--contract-version", required=True)
    publish_parser.add_argument("--runtime-tuple", required=True)
    args = parser.parse_args()
    try:
        if not args.store.is_absolute():
            raise CacheError("certification artifact cache store must be absolute")
        args.store = args.store.resolve(strict=True)
        if args.action == "prepare":
            if not args.destination.is_absolute():
                raise CacheError("certification artifact cache destination must be absolute")
            args.destination = args.destination.resolve(strict=False)
            prepare(args.store, args.destination)
        else:
            if (
                not args.source.is_absolute()
                or not SHA.fullmatch(args.factory_sha)
                or not SHA.fullmatch(args.factory_tree)
                or not SHA.fullmatch(args.product_sha)
                or not SHA.fullmatch(args.product_tree)
            ):
                raise CacheError("certification artifact cache boundary is invalid")
            args.source = args.source.resolve(strict=False)
            args.plan = args.plan.resolve(strict=True)
            publish(args.store, args.source, args.plan, _context(args))
        return 0
    except (CacheError, FileNotFoundError, json.JSONDecodeError, OSError, ValueError) as error:
        print(str(error), file=os.sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
