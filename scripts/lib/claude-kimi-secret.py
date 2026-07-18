#!/usr/bin/env python3
"""Validate the fixed Kimi credential without disclosing its path or value."""

import argparse
import os
from pathlib import Path
import stat
import sys


MAX_TOKEN_BYTES = 4096


def fail() -> None:
    print("Kimi credential failed secure-file validation", file=sys.stderr)
    raise SystemExit(2)


def validate(path_text: str) -> bytes:
    path = Path(path_text)
    if not path.is_absolute():
        fail()

    parts = path.parts[1:]
    if not parts:
        fail()
    descriptors: list[int] = []
    try:
        descriptor = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
        descriptors.append(descriptor)
        for index, part in enumerate(parts):
            if part in ("", ".", ".."):
                fail()
            flags = os.O_RDONLY | os.O_NOFOLLOW
            if index != len(parts) - 1:
                flags |= os.O_DIRECTORY
            descriptor = os.open(part, flags, dir_fd=descriptor)
            descriptors.append(descriptor)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_nlink != 1
        ):
            fail()
        raw = os.read(descriptor, MAX_TOKEN_BYTES + 2)
    except OSError:
        fail()
    finally:
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass

    if raw.endswith(b"\n"):
        raw = raw[:-1]
    if not (1 <= len(raw) <= MAX_TOKEN_BYTES):
        fail()
    if b"\x00" in raw or b"\n" in raw or b"\r" in raw:
        fail()
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        fail()
    return raw


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("path")
    arguments = parser.parse_args()
    token = validate(arguments.path)
    if not arguments.check:
        # The caller captures this through a pipe; diagnostics never include it.
        os.write(sys.stdout.fileno(), token)


if __name__ == "__main__":
    main()
