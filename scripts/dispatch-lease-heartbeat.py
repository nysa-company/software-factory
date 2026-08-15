#!/usr/bin/env python3
"""Renew one dispatcher lease until the trusted role wrapper stops us."""

import argparse
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--renew-script", required=True, type=Path)
    parser.add_argument("--factory-root", required=True, type=Path)
    parser.add_argument("--ticket", required=True)
    parser.add_argument("--lease", required=True)
    parser.add_argument("--interval", type=int, default=300)
    args = parser.parse_args()
    if not 1 <= args.interval <= 300:
        raise SystemExit("heartbeat interval must be from 1 through 300 seconds")

    os.setsid()
    stopped = False

    def stop(_signum, _frame):
        nonlocal stopped
        stopped = True

    handler = stop
    if (os.environ.get("FACTORY_TEST_MODE") == "1" and
            os.environ.get("FACTORY_TRUSTED_TEST_HARNESS") == "1" and
            os.environ.get("FACTORY_TEST_LEASE_HEARTBEAT_IGNORE_TERM") == "1"):
        def wait_for_kill(_signum, _frame):
            while True:
                time.sleep(1)

        handler = wait_for_kill
    for selected in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(selected, handler)
    environment = os.environ.copy()
    environment["FACTORY_ROOT"] = str(args.factory_root)
    deadline = time.monotonic() + args.interval
    while not stopped:
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(remaining, 0.1))
            continue
        result = subprocess.run(
            ["bash", str(args.renew_script), "renew", "--ticket", args.ticket,
             "--lease", args.lease],
            env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            check=False, timeout=30,
        )
        if stopped and result.returncode in {
            -signal.SIGTERM, 128 + signal.SIGTERM,
        }:
            return 0
        if result.returncode:
            return result.returncode
        deadline = time.monotonic() + args.interval
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, subprocess.SubprocessError) as error:
        print(f"dispatch-lease-heartbeat: {error}", file=sys.stderr)
        raise SystemExit(8)
