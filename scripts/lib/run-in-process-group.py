#!/usr/bin/env python3
"""Prepare a new process group, publish readiness, then run one command."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

READINESS_TIMEOUT_SECONDS = 120


def wait_for_gate(go_path: Path) -> None:
    deadline = time.monotonic() + READINESS_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if go_path.exists():
            return
        time.sleep(0.01)
    raise SystemExit("wrapper did not acknowledge process-group readiness")


def group_members() -> list[int]:
    group = os.getpgrp()
    own_pid = os.getpid()
    result = subprocess.run(
        ["ps", "-axo", "pid=,pgid=,comm="],
        check=False,
        capture_output=True,
        text=True,
    )
    members: list[int] = []
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) < 3:
            continue
        pid, pgid = (int(fields[0]), int(fields[1]))
        command = os.path.basename(fields[2])
        if pgid == group and pid != own_pid and command != "ps":
            members.append(pid)
    return members


def terminate_remaining_members() -> None:
    members = group_members()
    if not members:
        return
    for pid in members:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline and group_members():
        time.sleep(0.02)
    for pid in group_members():
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def main() -> int:
    if len(sys.argv) < 8:
        raise SystemExit(
            "usage: run-in-process-group.py READY_FILE GO_FILE "
            "SUBMITTED_FILE KILL_FILE MAINTENANCE_FILE CANCEL_FILE "
            "COMMAND [ARG ...]"
        )
    ready_path = Path(sys.argv[1])
    go_path = Path(sys.argv[2])
    submitted_path = Path(sys.argv[3])
    stop_paths = tuple(Path(value) for value in sys.argv[4:7])
    os.setsid()
    ready_tmp = ready_path.with_name(f"{ready_path.name}.{os.getpid()}.tmp")
    ready_tmp.write_text(f"pid={os.getpid()}\npgid={os.getpgrp()}\n")
    os.replace(ready_tmp, ready_path)

    # The wrapper publishes the validated PID/PGID record before acknowledging.
    # If it crashes, no task is submitted.
    wait_for_gate(go_path)
    if (
        os.environ.get("FACTORY_TEST_MODE") == "1"
        and os.environ.get("FACTORY_TEST_AFTER_GATE_SLEEP")
    ):
        time.sleep(float(os.environ["FACTORY_TEST_AFTER_GATE_SLEEP"]))
    if any(path.exists() or path.is_symlink() for path in stop_paths):
        print("control stop appeared at adapter submission boundary", file=sys.stderr)
        return 123

    try:
        child = subprocess.Popen(sys.argv[7:])
    except OSError as error:
        print(f"could not start adapter: {error}", file=sys.stderr)
        return 126
    try:
        submitted_tmp = submitted_path.with_name(
            f"{submitted_path.name}.{os.getpid()}.tmp"
        )
        descriptor = os.open(
            submitted_tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
        )
        try:
            os.write(descriptor, f"pid={child.pid}\n".encode())
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(submitted_tmp, submitted_path)
        directory = os.open(submitted_path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError as error:
        print(f"could not persist adapter submission: {error}", file=sys.stderr)
        child.terminate()
        try:
            child.wait(timeout=1)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()
        terminate_remaining_members()
        try:
            submitted_tmp.unlink()
        except (NameError, FileNotFoundError):
            pass
        return 125
    return_code = child.wait()
    terminate_remaining_members()
    return return_code if return_code >= 0 else 128 + abs(return_code)


if __name__ == "__main__":
    raise SystemExit(main())
