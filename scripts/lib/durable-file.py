#!/usr/bin/env python3
"""Atomically publish a file and its directory entry to durable storage."""

import os
from pathlib import Path
import sys
import tempfile


def fsync_directory(path):
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def publish(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        fsync_directory(path.parent)
    finally:
        try:
            os.unlink(temp)
        except FileNotFoundError:
            pass


def main():
    if len(sys.argv) != 3 or sys.argv[1] not in {"write", "touch"}:
        raise SystemExit("usage: durable-file.py {write|touch} TARGET")
    content = sys.stdin.buffer.read() if sys.argv[1] == "write" else b""
    publish(Path(sys.argv[2]), content)


if __name__ == "__main__":
    main()
