#!/usr/bin/env python3
"""Validate the operator-owned, provider-free ticket readiness contract."""

from __future__ import annotations

import argparse
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess


class ReadinessError(ValueError):
    pass


def field(text: str, name: str) -> str:
    values = re.findall(
        rf"^{re.escape(name)}:\s*(.*?)\s*$", text, re.IGNORECASE | re.MULTILINE
    )
    if len(values) != 1 or not values[0]:
        raise ReadinessError(f"ticket requires exactly one {name} field")
    return values[0]


def paths(text: str, name: str, workdir: Path) -> list[str]:
    raw = field(text, name)
    if raw.casefold() == "none":
        return []
    result = [item.strip() for item in raw.split(",")]
    if not result or len(result) != len(set(result)):
        raise ReadinessError(f"{name} paths are empty or duplicated")
    for value in result:
        path = PurePosixPath(value)
        if (
            path.is_absolute()
            or not path.parts
            or any(part in {"", ".", ".."} for part in path.parts)
            or not re.fullmatch(r"[A-Za-z0-9._/@+-]+", value)
        ):
            raise ReadinessError(f"{name} contains an unsafe path")
        candidate = workdir / value
        info = candidate.lstat()
        if not stat.S_ISREG(info.st_mode) or candidate.is_symlink():
            raise ReadinessError(f"{name} path is not a regular file: {value}")
        tracked = subprocess.run(
            ["git", "-C", str(workdir), "ls-files", "--error-unmatch", "--", value],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if tracked.returncode:
            raise ReadinessError(f"{name} path is not tracked: {value}")
    return result


def protected_test_conflicts(
    text: str, workdir: Path, test_author_paths: list[str]
) -> None:
    raw = field(text, "Protected-Test-Conflicts")
    if raw.casefold() == "none":
        return
    entries = [item.strip() for item in raw.split(",")]
    if not entries or len(entries) != len(set(entries)):
        raise ReadinessError("Protected-Test-Conflicts entries are duplicated")
    for entry in entries:
        path, separator, literal = entry.partition(" => ")
        if (
            not separator
            or not re.fullmatch(r"[A-Za-z0-9._/@:+-]{1,200}", literal)
        ):
            raise ReadinessError("protected-test conflict declaration is invalid")
        conflict_paths = paths(
            f"Protected-Test-Conflict-Path: {path}",
            "Protected-Test-Conflict-Path",
            workdir,
        )
        if conflict_paths[0] not in test_author_paths:
            raise ReadinessError(
                f"protected-test conflict lacks Test-author ownership: {entry}"
            )


def validate(ticket: str, workdir: Path) -> None:
    if not re.fullmatch(r"T-[0-9]+", ticket):
        raise ReadinessError("invalid ticket identifier")
    workdir = workdir.resolve(strict=True)
    ticket_path = workdir / "factory" / "tickets" / f"{ticket}.md"
    info = ticket_path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or ticket_path.is_symlink()
        or info.st_uid != os.geteuid()
    ):
        raise ReadinessError("ticket contract is unsafe")
    text = ticket_path.read_text(encoding="utf-8")
    if field(text, "Product-Decisions").casefold() != "frozen":
        raise ReadinessError("product decisions are not frozen")
    fixture_seams = paths(text, "Fixture-Seams", workdir)
    paths(text, "Authentication-Seams", workdir)
    protected_test_conflicts(text, workdir, fixture_seams)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ticket", required=True)
    parser.add_argument("--workdir", required=True, type=Path)
    args = parser.parse_args()
    try:
        validate(args.ticket, args.workdir)
    except (OSError, UnicodeError, ReadinessError) as error:
        print(f"READINESS BLOCKED: {error}")
        raise SystemExit(1)
    print("READINESS PASS")


if __name__ == "__main__":
    main()
