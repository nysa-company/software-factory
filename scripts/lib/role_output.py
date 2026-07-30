#!/usr/bin/env python3
"""Bounded, owner-only role-output publication and hashing."""

from __future__ import annotations

import hashlib
import io
import os
from pathlib import Path
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
    if len(sys.argv) != 3 or sys.argv[1] != "publish":
        raise SystemExit("usage: role_output.py publish TARGET")
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
