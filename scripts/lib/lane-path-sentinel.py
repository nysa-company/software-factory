#!/usr/bin/env python3
"""Reject lane-local absolute paths added between two Git revisions."""

from __future__ import annotations

import re
import subprocess
import sys


LANE_PATH = re.compile(
    rb"/(?:[^/\r\n]+/)*nysa-sf-dev\.[A-Za-z0-9._-]+(?:/[^\s'\"`]*)?"
)


def main() -> int:
    if len(sys.argv) != 4:
        raise SystemExit("usage: lane-path-sentinel.py REPO BASE HEAD")
    repo, base, head = sys.argv[1:]
    process = subprocess.Popen(
        [
            "git",
            "-C",
            repo,
            "diff",
            "--no-ext-diff",
            "--no-color",
            "--unified=0",
            base,
            head,
            "--",
        ],
        stdout=subprocess.PIPE,
    )
    assert process.stdout is not None
    leaked = False
    for line in process.stdout:
        if (
            line.startswith(b"+")
            and not line.startswith(b"+++")
            and LANE_PATH.search(line)
        ):
            leaked = True
    process.stdout.close()
    if process.wait() != 0:
        raise SystemExit("could not validate development role paths")
    if leaked:
        raise SystemExit("lane-local absolute path detected in role output")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
