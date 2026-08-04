#!/usr/bin/env python3
"""Run the independent factory-script test subsets in isolated process groups."""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


SUBSETS = (
    "model-policy",
    "runtime-routing",
    "launch-controls",
    "sequencer",
    "role-exit-git",
    "role-exit-policy",
)
WORKERS = 6


def group_members(pgid: int) -> list[int]:
    result = subprocess.run(
        ["ps", "-axo", "pid=,pgid="], capture_output=True, check=False, text=True
    )
    members = []
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) == 2 and int(fields[1]) == pgid:
            members.append(int(fields[0]))
    return members


def signal_groups(pgids: tuple[int, ...], signum: int) -> None:
    for pgid in pgids:
        for pid in group_members(pgid):
            try:
                os.kill(pid, signum)
            except ProcessLookupError:
                pass


def terminate_groups(pgids: tuple[int, ...]) -> None:
    signal_groups(pgids, signal.SIGTERM)
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if not any(group_members(pgid) for pgid in pgids):
            return
        time.sleep(0.02)
    signal_groups(pgids, signal.SIGKILL)


def terminate_group(pgid: int) -> None:
    terminate_groups((pgid,))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--script", required=True, type=Path)
    parser.add_argument("--temp-root", required=True, type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    assert len(SUBSETS) == WORKERS
    args.temp_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    running: dict[int, dict[str, object]] = {}
    results: dict[str, tuple[int, float, Path]] = {}
    interrupted = 0

    def stop(signum: int, _frame: object) -> None:
        nonlocal interrupted
        interrupted = signum
        terminate_groups(tuple(running))

    for handled in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
        signal.signal(handled, stop)

    for subset in SUBSETS:
        subset_root = args.temp_root / subset
        subset_root.mkdir(mode=0o700)
        log = subset_root / "output.log"
        stream = log.open("wb")
        environment = os.environ.copy()
        environment["TMPDIR"] = str(subset_root)
        started = time.monotonic()
        process = subprocess.Popen(
            ["bash", str(args.script), "--subset", subset],
            env=environment,
            stdout=stream,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        running[process.pid] = {
            "log": log,
            "process": process,
            "started": started,
            "stream": stream,
            "subset": subset,
        }

    first_failure = ""
    try:
        while running:
            pid, status = os.wait()
            active = running.pop(pid)
            process = active["process"]
            assert isinstance(process, subprocess.Popen)
            process.returncode = os.waitstatus_to_exitcode(status)
            stream = active["stream"]
            assert hasattr(stream, "close")
            stream.close()
            subset = str(active["subset"])
            log = active["log"]
            assert isinstance(log, Path)
            leaked = bool(group_members(pid))
            if leaked:
                terminate_group(pid)
                with log.open("ab") as output:
                    output.write(b"FAIL: subset leaked a child process\n")
            status_code = process.returncode or (1 if leaked else 0)
            results[subset] = (
                status_code,
                time.monotonic() - float(active["started"]),
                log,
            )
            if status_code and not first_failure:
                first_failure = subset
                terminate_groups(tuple(running))
    finally:
        terminate_groups(tuple(running))
        for _pid, active in tuple(running.items()):
            process = active["process"]
            assert isinstance(process, subprocess.Popen)
            process.wait()
            stream = active["stream"]
            assert hasattr(stream, "close")
            stream.close()

    if interrupted:
        return 128 + interrupted
    if first_failure:
        status_code, _elapsed, log = results[first_failure]
        sys.stdout.write(log.read_text(encoding="utf-8", errors="replace"))
        print(f"FAIL: factory-script subset {first_failure} exited {status_code}")
        return 1

    for subset in SUBSETS:
        status_code, elapsed, log = results[subset]
        sys.stdout.write(log.read_text(encoding="utf-8", errors="replace"))
        print(f"PASS: factory-script subset {subset} ({elapsed:.2f}s)")
        if status_code:
            return 1
    print("PASS: all factory-script tests")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
