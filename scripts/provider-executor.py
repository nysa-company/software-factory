#!/usr/bin/env python3
"""Bounded provider execution with a Docker-compatible isolated transport."""

from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from typing import Any


REQUEST_SCHEMA = "nysa.software-factory.provider-execution-request/v1"
RESULT_SCHEMA = "nysa.software-factory.provider-execution-result/v1"
IDENTITY_SCHEMA = "nysa.software-factory.provider-container-identity/v1"
MODES = ("legacy-serialized", "isolated-v1")
SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
TICKET_ID = re.compile(r"T-[0-9]{1,12}\Z")
GIT_SHA = re.compile(r"[0-9a-f]{40}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
PINNED_IMAGE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/:+-]*@sha256:([0-9a-f]{64})\Z")
REQUEST_KEYS = frozenset(
    (
        "attempt_id", "base_sha", "command", "image", "input",
        "policy_sha256", "role", "route_id", "schema", "source", "ticket",
    )
)
DEFAULT_SOURCE_BYTES = 16 * 1024 * 1024
DEFAULT_INPUT_BYTES = 1024 * 1024
DEFAULT_ARTIFACT_BYTES = 16 * 1024 * 1024
DEFAULT_OUTPUT_BYTES = 64 * 1024
DEFAULT_RESULT_BYTES = 256 * 1024
MAX_TREE_ENTRIES = 4096


class ExecutorError(ValueError):
    """A request or transport failed closed."""


def no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ExecutorError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def canonical(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def read_json(path: Path, maximum: int) -> tuple[dict[str, Any], bytes]:
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_size > maximum:
        raise ExecutorError(f"{path.name} is missing, unsafe, or oversized")
    raw = path.read_bytes()
    after = path.lstat()
    if (
        before.st_dev != after.st_dev
        or before.st_ino != after.st_ino
        or before.st_size != after.st_size
    ):
        raise ExecutorError(f"{path.name} changed while reading")
    try:
        value = json.loads(raw, object_pairs_hook=no_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ExecutorError(f"{path.name} is not valid JSON") from error
    if not isinstance(value, dict):
        raise ExecutorError(f"{path.name} must contain a JSON object")
    return value, raw


def write_exclusive(path: Path, raw: bytes) -> None:
    descriptor = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600
    )
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())


def validate_request(value: dict[str, Any], mode: str) -> None:
    if set(value) != REQUEST_KEYS or value.get("schema") != REQUEST_SCHEMA:
        raise ExecutorError("request schema or fields are invalid")
    for field in ("attempt_id", "role", "route_id"):
        if not isinstance(value.get(field), str) or not SAFE_ID.fullmatch(value[field]):
            raise ExecutorError(f"invalid {field}")
    if not isinstance(value.get("ticket"), str) or not TICKET_ID.fullmatch(value["ticket"]):
        raise ExecutorError("invalid ticket")
    if not isinstance(value.get("base_sha"), str) or not GIT_SHA.fullmatch(value["base_sha"]):
        raise ExecutorError("invalid base_sha")
    if (
        not isinstance(value.get("policy_sha256"), str)
        or not SHA256.fullmatch(value["policy_sha256"])
    ):
        raise ExecutorError("invalid policy_sha256")
    command = value.get("command")
    if (
        not isinstance(command, list)
        or not command
        or len(command) > 128
        or any(
            not isinstance(item, str)
            or not item
            or len(item.encode("utf-8")) > 8192
            or "\x00" in item
            for item in command
        )
    ):
        raise ExecutorError("command must be a bounded non-empty string array")
    for field in ("source", "input"):
        if not isinstance(value.get(field), str) or "\x00" in value[field]:
            raise ExecutorError(f"invalid {field} path")
    image = value.get("image")
    if mode == "isolated-v1":
        if not isinstance(image, str) or not PINNED_IMAGE.fullmatch(image):
            raise ExecutorError("isolated-v1 requires an image pinned by sha256 digest")
    elif image is not None and not isinstance(image, str):
        raise ExecutorError("image must be a string or null")


def regular_file(path: Path, maximum: int) -> tuple[bytes, str]:
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_size > maximum:
        raise ExecutorError(f"{path.name} is unsafe or oversized")
    raw = path.read_bytes()
    after = path.lstat()
    if (before.st_dev, before.st_ino, before.st_size) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
    ):
        raise ExecutorError(f"{path.name} changed while reading")
    return raw, digest(raw)


def copy_source(source: Path, destination: Path, maximum: int) -> str:
    root_info = source.lstat()
    if not stat.S_ISDIR(root_info.st_mode):
        raise ExecutorError("source must be a real directory")
    destination.mkdir(mode=0o755)
    hashed: list[bytes] = []
    total = 0
    entries = 0
    for current, directories, files in os.walk(source, topdown=True, followlinks=False):
        current_path = Path(current)
        relative = current_path.relative_to(source)
        if ".git" in directories:
            if not stat.S_ISDIR((current_path / ".git").lstat().st_mode):
                raise ExecutorError("source contains an unsafe .git entry")
            directories.remove(".git")
        for name in list(directories):
            entries += 1
            if entries > MAX_TREE_ENTRIES:
                raise ExecutorError("source contains too many entries")
            candidate = current_path / name
            info = candidate.lstat()
            if not stat.S_ISDIR(info.st_mode):
                raise ExecutorError(f"source contains unsafe directory entry: {candidate}")
        target_directory = destination / relative
        target_directory.mkdir(mode=0o755, parents=True, exist_ok=True)
        for name in sorted(files):
            entries += 1
            if entries > MAX_TREE_ENTRIES:
                raise ExecutorError("source contains too many entries")
            candidate = current_path / name
            info = candidate.lstat()
            if not stat.S_ISREG(info.st_mode):
                raise ExecutorError(f"source contains unsafe file entry: {candidate}")
            total += info.st_size
            if total > maximum:
                raise ExecutorError("source exceeds configured size limit")
            raw = candidate.read_bytes()
            after = candidate.lstat()
            if (info.st_dev, info.st_ino, info.st_size) != (
                after.st_dev,
                after.st_ino,
                after.st_size,
            ):
                raise ExecutorError(f"source changed while copying: {candidate}")
            relative_file = candidate.relative_to(source)
            output = destination / relative_file
            output.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            write_exclusive(output, raw)
            os.chmod(output, info.st_mode & 0o111 | 0o444)
            relative_raw = relative_file.as_posix().encode("utf-8")
            hashed.append(
                len(relative_raw).to_bytes(8, "big")
                + relative_raw
                + len(raw).to_bytes(8, "big")
                + raw
            )
    return digest(b"".join(hashed))


def validate_artifacts(root: Path, maximum: int) -> tuple[int, str]:
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise ExecutorError("artifact output is not a real directory")
    total = 0
    entries = 0
    hashed: list[bytes] = []
    for current, directories, files in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        for name in directories:
            entries += 1
            if entries > MAX_TREE_ENTRIES:
                raise ExecutorError("artifact output contains too many entries")
            entry = current_path / name
            if not stat.S_ISDIR(entry.lstat().st_mode):
                raise ExecutorError("artifact output contains a symlink or unsafe directory")
        for name in sorted(files):
            entries += 1
            if entries > MAX_TREE_ENTRIES:
                raise ExecutorError("artifact output contains too many entries")
            entry = current_path / name
            before = entry.lstat()
            if not stat.S_ISREG(before.st_mode):
                raise ExecutorError("artifact output contains a symlink or unsafe file")
            total += before.st_size
            if total > maximum:
                raise ExecutorError("artifact output exceeds configured size limit")
            raw = entry.read_bytes()
            after = entry.lstat()
            if (before.st_dev, before.st_ino, before.st_size) != (
                after.st_dev,
                after.st_ino,
                after.st_size,
            ):
                raise ExecutorError("artifact output changed while validating")
            relative = entry.relative_to(root).as_posix().encode("utf-8")
            hashed.append(
                len(relative).to_bytes(8, "big")
                + relative
                + len(raw).to_bytes(8, "big")
                + raw
            )
    return total, digest(b"".join(hashed))


def payload_archive(payload: Path) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for entry in sorted(payload.rglob("*")):
            info = entry.lstat()
            if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                raise ExecutorError("prepared payload contains an unsafe entry")
            archive.add(
                entry,
                arcname=entry.relative_to(payload).as_posix(),
                recursive=False,
            )
    return output.getvalue()


def extract_artifact_archive(raw: bytes, destination: Path, maximum: int) -> Path:
    if len(raw) > maximum + 1024 * 1024:
        raise ExecutorError("artifact archive exceeds configured size limit")
    if any(destination.iterdir()):
        raise ExecutorError("artifact extraction directory is not empty")
    os.chmod(destination, 0o700)
    total = 0
    entries = 0
    seen: set[str] = set()
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:*") as archive:
            for member in archive:
                entries += 1
                if entries > MAX_TREE_ENTRIES:
                    raise ExecutorError("artifact archive contains too many entries")
                path = Path(member.name)
                parts = path.parts
                if (
                    not parts
                    or parts[0] != "artifacts"
                    or any(part in ("", ".", "..") for part in parts)
                    or path.is_absolute()
                    or member.name in seen
                ):
                    raise ExecutorError("artifact archive contains an unsafe path")
                seen.add(member.name)
                output = destination.joinpath(*parts)
                if member.isdir():
                    output.mkdir(mode=0o700, parents=True, exist_ok=True)
                    continue
                if not member.isfile():
                    raise ExecutorError("artifact archive contains a non-regular entry")
                total += member.size
                if total > maximum:
                    raise ExecutorError("artifact output exceeds configured size limit")
                source = archive.extractfile(member)
                if source is None:
                    raise ExecutorError("artifact archive member cannot be read")
                output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                write_exclusive(output, source.read())
    except (tarfile.TarError, EOFError) as error:
        raise ExecutorError("artifact archive is invalid") from error
    root = destination / "artifacts"
    if not root.is_dir():
        raise ExecutorError("artifact archive is missing its root directory")
    return root


def bounded_process(
    command: list[str],
    *,
    cwd: Path | None,
    timeout: float,
    output_limit: int,
    input_data: bytes | None = None,
) -> tuple[int, bytes, bytes, bool, bool]:
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            stdin=subprocess.PIPE if input_data is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as error:
        raise ExecutorError(f"could not launch command: {error}") from error
    buffers = [bytearray(), bytearray()]
    truncated = [False, False]

    def drain(stream: Any, index: int) -> None:
        while True:
            chunk = stream.read(65536)
            if not chunk:
                break
            remaining = output_limit - len(buffers[index])
            if remaining > 0:
                buffers[index].extend(chunk[:remaining])
            if len(chunk) > remaining:
                truncated[index] = True
        stream.close()

    threads = [
        threading.Thread(target=drain, args=(process.stdout, 0), daemon=True),
        threading.Thread(target=drain, args=(process.stderr, 1), daemon=True),
    ]
    for thread in threads:
        thread.start()
    try:
        if input_data is not None:
            assert process.stdin is not None
            process.stdin.write(input_data)
            process.stdin.close()
        return_code = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=min(2.0, timeout))
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
        raise ExecutorError("provider execution timed out")
    finally:
        for thread in threads:
            thread.join(timeout=2)
    return return_code, bytes(buffers[0]), bytes(buffers[1]), truncated[0], truncated[1]


def runtime_call(
    runtime: str,
    arguments: list[str],
    *,
    timeout: float,
    output_limit: int,
    check: bool = True,
    input_data: bytes | None = None,
) -> tuple[int, bytes, bytes, bool, bool]:
    result = bounded_process(
        [runtime, *arguments], cwd=None, timeout=timeout,
        output_limit=output_limit, input_data=input_data,
    )
    if check and result[0] != 0:
        message = result[2].decode("utf-8", "replace").strip()
        raise ExecutorError(f"container runtime {' '.join(arguments[:2])} failed: {message}")
    return result


def identity_for(
    request: dict[str, Any], input_hash: str, source_hash: str
) -> dict[str, Any]:
    image_match = PINNED_IMAGE.fullmatch(request["image"])
    assert image_match is not None
    core = {
        "attempt_id": request["attempt_id"],
        "base_sha": request["base_sha"],
        "command": request["command"],
        "image": request["image"],
        "image_digest": image_match.group(1),
        "input_sha256": input_hash,
        "policy_sha256": request["policy_sha256"],
        "role": request["role"],
        "route_id": request["route_id"],
        "schema": IDENTITY_SCHEMA,
        "source_sha256": source_hash,
        "ticket": request["ticket"],
    }
    binding = digest(canonical(core))
    return {
        **core,
        "binding_sha256": binding,
        "container_name": (
            f"sf-{request['attempt_id'][:40]}-{binding[:16]}".lower()
        ),
    }


def result_value(
    *,
    mode: str,
    identity: dict[str, Any],
    return_code: int,
    stdout: bytes,
    stderr: bytes,
    stdout_truncated: bool,
    stderr_truncated: bool,
    artifact_bytes: int,
    artifact_hash: str,
) -> dict[str, Any]:
    return {
        "artifact_bytes": artifact_bytes,
        "artifact_sha256": artifact_hash,
        "attempt_id": identity["attempt_id"],
        "base_sha": identity["base_sha"],
        "binding_sha256": identity["binding_sha256"],
        "container_name": identity.get("container_name"),
        "image_digest": identity.get("image_digest"),
        "input_sha256": identity["input_sha256"],
        "mode": mode,
        "policy_sha256": identity["policy_sha256"],
        "return_code": return_code,
        "role": identity["role"],
        "route_id": identity["route_id"],
        "schema": RESULT_SCHEMA,
        "source_sha256": identity["source_sha256"],
        "stderr": stderr.decode("utf-8", "replace"),
        "stderr_truncated": stderr_truncated,
        "stdout": stdout.decode("utf-8", "replace"),
        "stdout_truncated": stdout_truncated,
        "ticket": identity["ticket"],
    }


def checked_attempt_root(root: Path) -> Path:
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not stat.S_ISDIR(root.lstat().st_mode):
        raise ExecutorError("attempt root is unsafe")
    return root.resolve(strict=True)


def replay(
    attempt: Path,
    identity: dict[str, Any],
    result_maximum: int,
    artifact_maximum: int,
) -> dict[str, Any] | None:
    result_path = attempt / "result.json"
    if not result_path.exists():
        return None
    existing_identity, identity_raw = read_json(
        attempt / "identity.json", result_maximum
    )
    if existing_identity != identity:
        raise ExecutorError("attempt replay identity mismatch")
    if identity_raw != canonical(existing_identity):
        raise ExecutorError("attempt replay identity is not canonical")
    result, raw = read_json(result_path, result_maximum)
    artifact_bytes, artifact_hash = validate_artifacts(
        attempt / "artifacts", artifact_maximum
    )
    if (
        result.get("schema") != RESULT_SCHEMA
        or result.get("binding_sha256") != identity["binding_sha256"]
        or result.get("artifact_bytes") != artifact_bytes
        or result.get("artifact_sha256") != artifact_hash
        or raw != canonical(result)
    ):
        raise ExecutorError("attempt replay result mismatch")
    return result


def prepare_attempt(
    root: Path,
    request: dict[str, Any],
    input_raw: bytes,
    input_hash: str,
    source: Path,
    source_limit: int,
) -> tuple[Path, dict[str, Any]]:
    attempt = root / request["attempt_id"]
    staging = Path(tempfile.mkdtemp(prefix=f".{request['attempt_id']}.", dir=root))
    try:
        payload = staging / "payload"
        payload.mkdir(mode=0o755)
        source_hash = copy_source(source, payload / "source", source_limit)
        write_exclusive(payload / "input", input_raw)
        os.chmod(payload / "input", 0o444)
        identity = identity_for(request, input_hash, source_hash)
        write_exclusive(payload / "identity.json", canonical(identity))
        os.chmod(payload / "identity.json", 0o444)
        write_exclusive(staging / "identity.json", canonical(identity))
        try:
            staging.rename(attempt)
        except OSError as error:
            if error.errno not in (errno.EEXIST, errno.ENOTEMPTY):
                raise
            if not stat.S_ISDIR(attempt.lstat().st_mode):
                raise ExecutorError("attempt path already exists and is unsafe")
            existing, _ = read_json(attempt / "identity.json", DEFAULT_RESULT_BYTES)
            if existing != identity:
                raise ExecutorError("attempt replay identity mismatch")
            return attempt, existing
        return attempt, identity
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def isolated_execute_locked(
    args: argparse.Namespace, request: dict[str, Any]
) -> dict[str, Any]:
    requested_source = Path(request["source"])
    if requested_source.is_symlink():
        raise ExecutorError("source must not be a symlink")
    source = requested_source.resolve(strict=True)
    input_path = Path(request["input"])
    input_raw, input_hash = regular_file(input_path, args.input_bytes)
    root = checked_attempt_root(args.attempt_root)
    attempt, identity = prepare_attempt(
        root, request, input_raw, input_hash, source, args.source_bytes
    )
    expected = identity_for(request, input_hash, identity.get("source_sha256", ""))
    if identity != expected:
        raise ExecutorError("attempt replay identity mismatch")
    prior = replay(attempt, identity, args.result_bytes, args.artifact_bytes)
    if prior is not None:
        return prior

    name = identity["container_name"]
    labels = [
        "--label", f"nysa.factory.attempt={identity['attempt_id']}",
        "--label", f"nysa.factory.base={identity['base_sha']}",
        "--label", f"nysa.factory.binding={identity['binding_sha256']}",
        "--label", f"nysa.factory.image={identity['image_digest']}",
        "--label", f"nysa.factory.role={identity['role']}",
        "--label", f"nysa.factory.route={identity['route_id']}",
        "--label", f"nysa.factory.ticket={identity['ticket']}",
    ]
    create = [
        "create", "--name", name, *labels,
        "--network", "none",
        "--read-only",
        "--user", args.container_user,
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--pids-limit", str(args.pids_limit),
        "--memory", args.memory,
        "--cpus", str(args.cpus),
        "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=64m",
        "--tmpfs", "/workspace:rw,noexec,nosuid,nodev,size=64m,mode=1777",
        "--workdir", "/workspace",
        request["image"],
        "/bin/sh", "-c",
        "mkdir -p /workspace/payload /workspace/artifacts || exit 125; "
        "trap 'exit 0' TERM INT; while :; do sleep 3600; done",
    ]
    created = False
    try:
        runtime_call(
            args.runtime, create, timeout=args.runtime_timeout,
            output_limit=args.output_bytes,
        )
        created = True
        runtime_call(
            args.runtime, ["start", name],
            timeout=args.runtime_timeout,
            output_limit=args.output_bytes,
        )
        runtime_call(
            args.runtime,
            [
                "exec", "-i", name, "tar", "--no-same-owner",
                "--no-same-permissions", "-x", "-f", "-",
                "-C", "/workspace/payload",
            ],
            timeout=args.runtime_timeout,
            output_limit=args.output_bytes,
            input_data=payload_archive(attempt / "payload"),
        )
        code, stdout, stderr, stdout_truncated, stderr_truncated = runtime_call(
            args.runtime,
            ["exec", "--workdir", "/workspace/payload/source", name, *request["command"]],
            timeout=args.timeout,
            output_limit=args.output_bytes, check=False,
        )
        temporary = Path(tempfile.mkdtemp(prefix=".artifact-", dir=attempt))
        try:
            (
                archive_code,
                artifact_archive,
                archive_stderr,
                archive_stdout_truncated,
                _,
            ) = runtime_call(
                args.runtime,
                [
                    "exec", name, "tar", "-c", "-f", "-",
                    "-C", "/workspace", "artifacts",
                ],
                timeout=args.runtime_timeout,
                output_limit=args.artifact_bytes + 1024 * 1024,
                check=False,
            )
            if archive_code != 0 or archive_stdout_truncated:
                message = archive_stderr.decode("utf-8", "replace").strip()
                raise ExecutorError(f"artifact copy-out failed: {message}")
            copied_artifacts = extract_artifact_archive(
                artifact_archive, temporary, args.artifact_bytes
            )
            artifact_bytes, artifact_hash = validate_artifacts(
                copied_artifacts, args.artifact_bytes
            )
            artifacts = attempt / "artifacts"
            if artifacts.exists() or artifacts.is_symlink():
                raise ExecutorError("artifact destination already exists")
            copied_artifacts.rename(artifacts)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
        result = result_value(
            mode="isolated-v1",
            identity=identity,
            return_code=code,
            stdout=stdout,
            stderr=stderr,
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
            artifact_bytes=artifact_bytes,
            artifact_hash=artifact_hash,
        )
        raw = canonical(result)
        if len(raw) > args.result_bytes:
            raise ExecutorError("result exceeds configured size limit")
        write_exclusive(attempt / "result.json", raw)
        return result
    finally:
        if created:
            runtime_call(
                args.runtime, ["rm", "--force", name],
                timeout=args.runtime_timeout, output_limit=args.output_bytes,
                check=False,
            )


def isolated_execute(args: argparse.Namespace, request: dict[str, Any]) -> dict[str, Any]:
    root = checked_attempt_root(args.attempt_root)
    lock_path = root / f".{request['attempt_id']}.execution.lock"
    descriptor = os.open(
        lock_path,
        os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    with os.fdopen(descriptor, "r+b") as lock:
        info = os.fstat(lock.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
            raise ExecutorError("attempt execution lock is unsafe")
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        return isolated_execute_locked(args, request)


def legacy_execute(args: argparse.Namespace, request: dict[str, Any]) -> dict[str, Any]:
    requested_source = Path(request["source"])
    if requested_source.is_symlink():
        raise ExecutorError("source must not be a symlink")
    source = requested_source.resolve(strict=True)
    input_raw, input_hash = regular_file(Path(request["input"]), args.input_bytes)
    temporary = Path(tempfile.mkdtemp())
    try:
        source_hash = copy_source(source, temporary / "source", args.source_bytes)
    finally:
        shutil.rmtree(temporary)
    core = {
        "attempt_id": request["attempt_id"],
        "base_sha": request["base_sha"],
        "command": request["command"],
        "image": request["image"],
        "input_sha256": input_hash,
        "policy_sha256": request["policy_sha256"],
        "role": request["role"],
        "route_id": request["route_id"],
        "schema": IDENTITY_SCHEMA,
        "source_sha256": source_hash,
        "ticket": request["ticket"],
    }
    identity = {**core, "binding_sha256": digest(canonical(core))}
    root = checked_attempt_root(args.attempt_root)
    lock_path = root / ".legacy-serialized.lock"
    with lock_path.open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        code, stdout, stderr, stdout_truncated, stderr_truncated = bounded_process(
            request["command"], cwd=source, timeout=args.timeout,
            output_limit=args.output_bytes,
        )
    return result_value(
        mode="legacy-serialized",
        identity=identity,
        return_code=code,
        stdout=stdout,
        stderr=stderr,
        stdout_truncated=stdout_truncated,
        stderr_truncated=stderr_truncated,
        artifact_bytes=0,
        artifact_hash=digest(b""),
    )


def cancel(args: argparse.Namespace) -> dict[str, Any]:
    root = checked_attempt_root(args.attempt_root)
    if not SAFE_ID.fullmatch(args.attempt_id):
        raise ExecutorError("invalid attempt_id")
    attempt = root / args.attempt_id
    identity, _ = read_json(attempt / "identity.json", args.result_bytes)
    if (
        identity.get("schema") != IDENTITY_SCHEMA
        or identity.get("attempt_id") != args.attempt_id
        or not SHA256.fullmatch(identity.get("binding_sha256", ""))
        or not SAFE_ID.fullmatch(identity.get("container_name", ""))
    ):
        raise ExecutorError("container identity is invalid")
    if args.binding_sha256 and args.binding_sha256 != identity["binding_sha256"]:
        raise ExecutorError("cancellation binding mismatch")
    name = identity["container_name"]
    code, _, stderr, _, _ = runtime_call(
        args.runtime, ["rm", "--force", name], timeout=args.runtime_timeout,
        output_limit=args.output_bytes, check=False,
    )
    return {
        "attempt_id": args.attempt_id,
        "binding_sha256": identity["binding_sha256"],
        "container_name": name,
        "removed": code == 0,
        "runtime_stderr": stderr.decode("utf-8", "replace"),
        "schema": "nysa.software-factory.provider-container-cancellation/v1",
    }


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser()
    value.add_argument(
        "--runtime", default=os.environ.get("PROVIDER_EXECUTOR_RUNTIME", "docker")
    )
    value.add_argument("--attempt-root", required=True, type=Path)
    value.add_argument("--runtime-timeout", type=float, default=30.0)
    value.add_argument("--output-bytes", type=int, default=DEFAULT_OUTPUT_BYTES)
    value.add_argument("--result-bytes", type=int, default=DEFAULT_RESULT_BYTES)
    subparsers = value.add_subparsers(dest="action", required=True)
    execute = subparsers.add_parser("execute")
    execute.add_argument("--mode", choices=MODES, required=True)
    execute.add_argument("--request", required=True, type=Path)
    execute.add_argument("--timeout", type=float, default=900.0)
    execute.add_argument("--source-bytes", type=int, default=DEFAULT_SOURCE_BYTES)
    execute.add_argument("--input-bytes", type=int, default=DEFAULT_INPUT_BYTES)
    execute.add_argument("--artifact-bytes", type=int, default=DEFAULT_ARTIFACT_BYTES)
    execute.add_argument("--container-user", default="65532:65532")
    execute.add_argument("--pids-limit", type=int, default=128)
    execute.add_argument("--memory", default="1g")
    execute.add_argument("--cpus", type=float, default=1.0)
    cancellation = subparsers.add_parser("cancel")
    cancellation.add_argument("--attempt-id", required=True)
    cancellation.add_argument("--binding-sha256")
    return value


def positive_settings(args: argparse.Namespace) -> None:
    for field in ("runtime_timeout", "output_bytes", "result_bytes"):
        if getattr(args, field) <= 0:
            raise ExecutorError(f"{field.replace('_', '-')} must be positive")
    if args.action == "execute":
        for field in (
            "timeout", "source_bytes", "input_bytes", "artifact_bytes",
            "pids_limit", "cpus",
        ):
            if getattr(args, field) <= 0:
                raise ExecutorError(f"{field.replace('_', '-')} must be positive")


def main() -> None:
    args = parser().parse_args()
    positive_settings(args)
    if args.action == "cancel":
        result = cancel(args)
    else:
        request, _ = read_json(args.request, args.result_bytes)
        validate_request(request, args.mode)
        if args.mode == "isolated-v1":
            result = isolated_execute(args, request)
        else:
            result = legacy_execute(args, request)
    raw = canonical(result)
    if len(raw) > args.result_bytes:
        raise ExecutorError("result exceeds configured size limit")
    sys.stdout.buffer.write(raw)


if __name__ == "__main__":
    try:
        main()
    except (ExecutorError, OSError) as error:
        raise SystemExit(f"provider-executor: {error}")
