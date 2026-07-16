#!/usr/bin/env python3
"""Atomically publish a file and its directory entry to durable storage."""

import os
from pathlib import Path
import secrets
import stat
import sys


def fsync_directory(path):
    flags = (os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) |
             getattr(os, "O_NOFOLLOW", 0))
    descriptor = os.open(path, flags)
    try:
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise NotADirectoryError(path)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def publish(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    # Persist the parent directory's own entry before publishing a file in it.
    # This covers the launcher's first factory/runs creation after a crash.
    if path.parent.parent != path.parent:
        fsync_directory(path.parent.parent)
    directory_flags = (os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) |
                       getattr(os, "O_NOFOLLOW", 0))
    directory = os.open(path.parent, directory_flags)
    temp = f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}"
    descriptor = -1
    try:
        descriptor = os.open(
            temp,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory,
        )
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path.name, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temp, dir_fd=directory)
        except FileNotFoundError:
            pass
        os.close(directory)


def main():
    if len(sys.argv) != 3 or sys.argv[1] not in {"write", "touch"}:
        raise SystemExit("usage: durable-file.py {write|touch} TARGET")
    content = sys.stdin.buffer.read() if sys.argv[1] == "write" else b""
    publish(Path(sys.argv[2]), content)


if __name__ == "__main__":
    main()
