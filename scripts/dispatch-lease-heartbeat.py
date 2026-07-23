#!/usr/bin/env python3
"""Renew one dispatcher lease until the trusted role wrapper stops us."""

import argparse
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading


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

    stopped = threading.Event()
    for selected in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(selected, lambda _signum, _frame: stopped.set())
    environment = os.environ.copy()
    environment["FACTORY_ROOT"] = str(args.factory_root)
    while not stopped.wait(args.interval):
        result = subprocess.run(
            ["bash", str(args.renew_script), "renew", "--ticket", args.ticket,
             "--lease", args.lease],
            env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            check=False, timeout=30,
        )
        if result.returncode:
            return result.returncode
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, subprocess.SubprocessError) as error:
        print(f"dispatch-lease-heartbeat: {error}", file=sys.stderr)
        raise SystemExit(8)
