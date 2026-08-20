"""Strict classification for transient GitHub transport failures."""

from __future__ import annotations

import re
import sys


def temporarily_unavailable(message: str) -> bool:
    lowered = message.casefold()
    return bool(re.search(r"\bHTTP 5[0-9]{2}\b", message, re.I)) or any(
        value in lowered for value in (
            "could not resolve host",
            "could not resolve hostname",
            "temporary failure in name resolution",
            "error connecting to api.github.com",
            "failed to connect to github.com",
            "connection reset by peer",
            "connection timed out",
            "network is unreachable",
            "operation timed out",
            "tls handshake timeout",
        )
    )


def remote_command(argv: list[str]) -> bool:
    return bool(argv) and (
        argv[0] == "gh"
        or argv[0] == "git"
        and any(value in {"fetch", "ls-remote", "push"} for value in argv[1:])
    )


if __name__ == "__main__":
    raise SystemExit(not temporarily_unavailable(sys.stdin.read(1_000_000)))
