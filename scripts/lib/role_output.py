#!/usr/bin/env python3
"""Bounded, owner-only role-output publication and hashing."""

from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys


MAX_BYTES = 8 * 1024 * 1024
CHUNK_BYTES = 1024 * 1024
FILE_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)


class RoleOutputError(ValueError):
    pass


class RoleOutputTooLarge(RoleOutputError):
    pass


def _validate_file(
    info: os.stat_result, path: Path, mode: int | None = None
) -> None:
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_nlink != 1
        or (mode is not None and stat.S_IMODE(info.st_mode) != mode)
    ):
        raise RoleOutputError(f"unsafe role output: {path.name}")
    if info.st_size > MAX_BYTES:
        raise RoleOutputTooLarge(
            f"role output exceeds {MAX_BYTES}-byte limit"
        )


def sha256(path: Path) -> str:
    """Hash one stable, bounded role-output artifact without loading it."""
    descriptor = os.open(path, FILE_FLAGS)
    try:
        before = os.fstat(descriptor)
        _validate_file(before, path, 0o600)
        digest = hashlib.sha256()
        total = 0
        while chunk := os.read(descriptor, CHUNK_BYTES):
            total += len(chunk)
            if total > MAX_BYTES:
                raise RoleOutputTooLarge(
                    f"role output exceeds {MAX_BYTES}-byte limit"
                )
            digest.update(chunk)
        after = os.fstat(descriptor)
        _validate_file(after, path, 0o600)
        if (
            total != before.st_size
            or (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            )
            != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            )
        ):
            raise RoleOutputError(f"role output changed while hashing: {path.name}")
        return digest.hexdigest()
    finally:
        os.close(descriptor)


def _stable_bytes(path: Path) -> bytes:
    descriptor = os.open(path, FILE_FLAGS)
    try:
        before = os.fstat(descriptor)
        _validate_file(before, path, 0o600)
        chunks = []
        total = 0
        while chunk := os.read(descriptor, CHUNK_BYTES):
            total += len(chunk)
            if total > MAX_BYTES:
                raise RoleOutputTooLarge(
                    f"role output exceeds {MAX_BYTES}-byte limit"
                )
            chunks.append(chunk)
        after = os.fstat(descriptor)
        _validate_file(after, path, 0o600)
        if (
            total != before.st_size
            or (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            )
            != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            )
        ):
            raise RoleOutputError(f"role output changed while reading: {path.name}")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def terminal_reason_code(path: Path, adapter: str) -> str:
    """Classify one exact, bounded provider terminal without copying its text."""
    if adapter != "claude-code":
        return ""

    def unique_object(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate JSON key")
            value[key] = item
        return value

    try:
        lines = _stable_bytes(path).decode("utf-8").splitlines()
        if (
            len(lines) != 2
            or re.fullmatch(
                r"turns=[0-9]{1,4}(?: cost_usd=[0-9]{1,7}(?:[.][0-9]{1,18})?)?",
                lines[1],
            ) is None
        ):
            return ""
        result = json.loads(lines[0], object_pairs_hook=unique_object)
    except (
        json.JSONDecodeError,
        OSError,
        RoleOutputError,
        UnicodeDecodeError,
        ValueError,
    ):
        return ""
    if not isinstance(result, dict):
        return ""
    message = result.get("result")
    status = result.get("api_error_status")
    if (
        result.get("type") != "result"
        or result.get("subtype") != "success"
        or result.get("is_error") is not True
        or result.get("stop_reason") != "stop_sequence"
        or result.get("terminal_reason") != "api_error"
        or isinstance(status, bool)
        or status != 429
        or not isinstance(message, str)
        or re.fullmatch(
            r"(?i)(?=[^\r\n]{1,256}\Z)"
            r"(?=[^\r\n]*(?:\bindividual spend limit\b|\borg's monthly spend limit\b))"
            r"you[^\r\n]*",
            message,
        ) is None
    ):
        return ""
    return "provider_spend_limit"


def publish(path: Path, source) -> str:
    """Stream a bounded artifact to an atomic owner-only file and return its hash."""
    path = Path(os.path.abspath(path))
    directory = os.open(path.parent, DIRECTORY_FLAGS)
    temporary = f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}"
    descriptor = -1
    try:
        parent = os.fstat(directory)
        if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.geteuid():
            raise RoleOutputError("unsafe role-output directory")
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory,
        )
        digest = hashlib.sha256()
        total = 0
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            while chunk := source.read(CHUNK_BYTES):
                total += len(chunk)
                if total > MAX_BYTES:
                    raise RoleOutputTooLarge(
                        f"role output exceeds {MAX_BYTES}-byte limit"
                    )
                handle.write(chunk)
                digest.update(chunk)
            handle.flush()
            os.fsync(handle.fileno())
            _validate_file(os.fstat(handle.fileno()), path, 0o600)
        os.replace(
            temporary,
            path.name,
            src_dir_fd=directory,
            dst_dir_fd=directory,
        )
        os.fsync(directory)
        return digest.hexdigest()
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass
        os.close(directory)


def main() -> None:
    if len(sys.argv) == 4 and sys.argv[1] == "terminal-reason-code":
        print(terminal_reason_code(Path(sys.argv[2]), sys.argv[3]))
        return
    if len(sys.argv) != 3 or sys.argv[1] != "publish":
        raise SystemExit(
            "usage: role_output.py publish TARGET | "
            "terminal-reason-code TARGET ADAPTER"
        )
    try:
        print(publish(Path(sys.argv[2]), sys.stdin.buffer))
    except RoleOutputTooLarge as error:
        marker = f"ROLE_OUTPUT_INVALID: {error}\n".encode()
        try:
            print(publish(Path(sys.argv[2]), io.BytesIO(marker)))
        except (OSError, RoleOutputError) as publish_error:
            print(f"ROLE_OUTPUT_INVALID: {publish_error}", file=sys.stderr)
        print(f"ROLE_OUTPUT_INVALID: {error}", file=sys.stderr)
        raise SystemExit(8)
    except (OSError, RoleOutputError) as error:
        print(f"ROLE_OUTPUT_INVALID: {error}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
