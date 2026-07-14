#!/usr/bin/env python3
"""Prepare a new process group, publish readiness, then run one command."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path


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
    if len(sys.argv) < 4:
        raise SystemExit(
            "usage: run-in-process-group.py READY_FILE GO_FILE COMMAND [ARG ...]"
        )
    ready_path = Path(sys.argv[1])
    go_path = Path(sys.argv[2])
    os.setsid()
    ready_tmp = ready_path.with_name(f"{ready_path.name}.{os.getpid()}.tmp")
    ready_tmp.write_text(f"pid={os.getpid()}\npgid={os.getpgrp()}\n")
    os.replace(ready_tmp, ready_path)

    # The wrapper publishes the validated PID/PGID record before acknowledging.
    # If it crashes, no task is submitted.
    deadline = time.monotonic() + 10
    while not go_path.exists():
        if time.monotonic() >= deadline:
            raise SystemExit("wrapper did not acknowledge process-group readiness")
        time.sleep(0.01)

    try:
        child = subprocess.Popen(sys.argv[3:])
    except OSError as error:
        print(f"could not start adapter: {error}", file=sys.stderr)
        return 126
    return_code = child.wait()
    terminate_remaining_members()
    return return_code if return_code >= 0 else 128 + abs(return_code)


if __name__ == "__main__":
    raise SystemExit(main())
