#!/usr/bin/env python3
"""Validate the approved Cursor CLI authentication-status JSON snapshot."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 2:
        return 2
    try:
        raw = sys.stdin.read() if sys.argv[1] == "-" else Path(sys.argv[1]).read_text()
        value = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return 1
    if not isinstance(value, dict):
        return 1
    for key in ("authenticated", "isAuthenticated", "loggedIn"):
        if value.get(key) is True:
            return 0
    if str(value.get("status", "")).lower() in {
        "authenticated",
        "logged_in",
        "logged-in",
    }:
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
