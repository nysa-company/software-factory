#!/usr/bin/env python3
"""Snapshot and restore launcher-owned run manifests around an agent process."""

import base64
import json
import os
from pathlib import Path
import secrets
import stat
import sys
import tempfile


def manifests(directory):
    result = {}
    for path in sorted(directory.glob("*.meta")):
        mode = path.lstat().st_mode
        if not stat.S_ISREG(mode) or path.is_symlink():
            raise ValueError(f"nonregular run manifest: {path.name}")
        result[path.name] = base64.b64encode(path.read_bytes()).decode("ascii")
    return result


def durable_write(path, content):
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def quarantine(path):
    target = path.with_name(f"{path.name}.rejected-role-mutation-{secrets.token_hex(6)}")
    os.replace(path, target)


def check(directory, expected):
    try:
        actual = manifests(directory)
    except (OSError, ValueError):
        actual = {}
    if actual == expected:
        return True

    for path in sorted(directory.glob("*.meta")):
        try:
            quarantine(path)
        except FileNotFoundError:
            pass
    for name, encoded in expected.items():
        durable_write(directory / name, base64.b64decode(encoded, validate=True))
    directory_fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return False


def main():
    if len(sys.argv) != 3 or sys.argv[1] not in {"snapshot", "check"}:
        raise SystemExit("usage: runs-integrity.py {snapshot|check} RUNS_DIR")
    directory = Path(sys.argv[2])
    if sys.argv[1] == "snapshot":
        print(json.dumps(manifests(directory), sort_keys=True, separators=(",", ":")))
        return
    expected = json.load(sys.stdin)
    if not check(directory, expected):
        print("role_exit_control_plane_mutation: run manifests changed during provider execution", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"runs-integrity: {error}", file=sys.stderr)
        raise SystemExit(1)
