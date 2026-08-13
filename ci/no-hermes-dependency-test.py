#!/usr/bin/env python3
"""Reject the removed external runtime from tracked current text."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]
REMOVED = "her" + "mes"
ALLOW_TEXT = frozenset({
    "TODOS.md",
    "conformance/factory/runs/1783911942-73937.out",
    "conformance/factory/runs/1783912438-76221.out",
    "conformance/factory/runs/1783912635-77319.out",
    "conformance/factory/runs/1783912741-77991.out",
    "conformance/factory/runs/1783919498-394.out",
    "conformance/factory/runs/1783919699-1499.out",
    "conformance/factory/runs/1783919714-1700.out",
    "conformance/factory/tickets/T-102.md",
    "docs/evidence/2026-07-25-sandbox-factory-session-handoff.md",
    "docs/evidence/2026-07-26-sandbox-factory-rolling-ten-recovery-handoff.md",
    "docs/evidence/2026-07-27-sandbox-factory-successor-candidate.md",
    "docs/evidence/contract-1.7-development-concurrency-2026-07-24.md",
    "docs/evidence/software-factory-improvement-log.md",
    "docs/factory-contract-changelog.md",
    "docs/plans/2026-07-29-cursor-bugbot-feedback.md",
    f"docs/plans/2026-08-12-remove-{REMOVED}-dependency.md",
})
MEMORY = "context/memory.md"
LOG_MARKER = "\n## Log\n"


def tracked_files(root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"],
        capture_output=True,
        check=True,
    )
    return sorted(os.fsdecode(path) for path in result.stdout.split(b"\0") if path)


def current_text(path: str, raw: bytes) -> str | None:
    if path in ALLOW_TEXT or b"\0" in raw:
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if path == MEMORY:
        if LOG_MARKER not in text:
            return text + "\n" + REMOVED
        return text.split(LOG_MARKER, 1)[0]
    return text


def violations(files: dict[str, bytes]) -> list[tuple[str, int]]:
    found: list[tuple[str, int]] = []
    needle = REMOVED.casefold()
    for path in sorted(files):
        text = current_text(path, files[path])
        if text is None:
            continue
        found.extend(
            (path, number)
            for number, line in enumerate(text.splitlines(), 1)
            if needle in line.casefold()
        )
    return found


def self_test() -> None:
    assert violations({"scripts/live.py": f"use {REMOVED}\n".encode()}) == [
        ("scripts/live.py", 1)
    ]
    assert not violations({"TODOS.md": f"retired {REMOVED}\n".encode()})
    memory = f"# Memory\n\n## Current truth\nclean\n\n## Log\nretired {REMOVED}\n"
    assert not violations({MEMORY: memory.encode()})
    bad_memory = memory.replace("clean", REMOVED)
    assert violations({MEMORY: bad_memory.encode()}) == [(MEMORY, 4)]


def main() -> int:
    self_test()
    files: dict[str, bytes] = {}
    for relative in tracked_files(ROOT):
        path = ROOT / relative
        if path.is_symlink():
            files[relative] = os.readlink(path).encode()
        elif path.is_file():
            files[relative] = path.read_bytes()
    found = violations(files)
    for path, line in found:
        print(f"FAIL: removed runtime reference: {path}:{line}")
    if found:
        return 1
    print("removed-runtime-dependency: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
