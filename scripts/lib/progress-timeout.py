#!/usr/bin/env python3
"""Terminate one process after authenticated inactivity or an absolute limit."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import signal
import stat
import tempfile
import time


DIGEST = re.compile(r"^[0-9a-f]{64}$")
PROGRESS_EVENTS = {
    ("assistant", ""),
    ("result", "success"),
    ("system", "init"),
    ("system", "initialize"),
    ("tool_call", "completed"),
    ("tool_call", "started"),
}


def write(path: Path, reason: str, detail: str = "") -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = -1
            json.dump({
                "detail": detail,
                "reason": reason,
                "schema": "factory-progress-timeout/v1",
            }, stream)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        Path(temporary).unlink(missing_ok=True)


def journal(path: Path, previous: bytes) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_size > 5_000_000
        ):
            raise ValueError("unsafe progress journal")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            raw = stream.read()
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not raw.startswith(previous):
        raise ValueError("progress journal was rewritten")
    if raw and not raw.endswith(b"\n"):
        return previous
    latest = -1.0
    for sequence, line in enumerate(raw.splitlines(), 1):
        value = json.loads(line)
        if (
            not isinstance(value, dict)
            or set(value) != {
                "event_sha256", "observed_monotonic_ns", "sequence", "subtype", "type"
            }
            or value["sequence"] != sequence
            or not isinstance(value["observed_monotonic_ns"], int)
            or not DIGEST.fullmatch(value["event_sha256"])
            or not all(isinstance(value[name], str) for name in ("type", "subtype"))
            or (value["type"], value["subtype"]) not in PROGRESS_EVENTS
        ):
            raise ValueError("progress journal is malformed")
        observed = value["observed_monotonic_ns"] / 1_000_000_000
        if observed < 0 or observed < latest:
            raise ValueError("progress journal clock is invalid")
        latest = observed
    return raw


def terminate(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pid", required=True, type=int)
    parser.add_argument("--journal", required=True, type=Path)
    parser.add_argument("--marker", required=True, type=Path)
    parser.add_argument("--soft-seconds", required=True, type=int)
    parser.add_argument("--hard-seconds", required=True, type=int)
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    args = parser.parse_args()
    if (
        args.pid <= 1
        or args.soft_seconds <= 0
        or args.hard_seconds <= args.soft_seconds
        or not 0.01 <= args.poll_seconds <= 10
    ):
        return 2
    start = time.monotonic()
    last_progress = start
    observed = b""
    while True:
        try:
            os.kill(args.pid, 0)
        except ProcessLookupError:
            return 0
        now = time.monotonic()
        if args.journal.exists() or args.journal.is_symlink():
            try:
                current = journal(args.journal, observed)
            except (OSError, ValueError, json.JSONDecodeError) as error:
                write(args.marker, "invalid_progress", str(error))
                terminate(args.pid)
                return 0
            if len(current) > len(observed):
                observed, last_progress = current, now
        reason = (
            "hard_timeout"
            if now - start >= args.hard_seconds
            else "soft_timeout"
            if now - last_progress >= args.soft_seconds
            else ""
        )
        if reason:
            write(args.marker, reason)
            terminate(args.pid)
            return 0
        time.sleep(args.poll_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
