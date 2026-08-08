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


def protected_test_conflict(entry: str) -> tuple[str, str]:
    path, separator, literal = entry.partition(" => ")
    candidate = PurePosixPath(path)
    if (
        not separator
        or candidate.is_absolute()
        or not candidate.parts
        or any(part in {"", ".", ".."} for part in candidate.parts)
        or not re.fullmatch(r"[A-Za-z0-9._/@+-]+", path)
        or not re.fullmatch(r"[A-Za-z0-9._/@:+-]{1,200}", literal)
    ):
        raise ReadinessError("protected-test conflict declaration is invalid")
    return path, literal


def protected_text_collisions(workdir: Path, literal: str) -> list[str]:
    if not literal or len(literal) > 500 or any(ord(char) < 32 for char in literal):
        raise ReadinessError("global text literal is invalid")
    result = subprocess.run(
        ["git", "-C", str(workdir), "ls-files", "-z"],
        capture_output=True, check=True,
    )
    collisions = []
    # ponytail: static string assertions only; add a parser if dynamic protected
    # text expressions become common enough to justify one.
    assertion = re.compile(
        r"(?:get|query|find)(?:All)?ByText\(\s*(['\"])(.*?)\1"
        r"(?P<options>[^)]*)\)", re.DOTALL,
    )
    for raw in result.stdout.split(b"\0"):
        if not raw:
            continue
        relative = raw.decode("utf-8", errors="strict")
        if not re.search(r"(?:^|/)[^/]*(?:test|spec)\.[cm]?[jt]sx?$", relative):
            continue
        path = workdir / relative
        if path.stat().st_size > 1_048_576:
            raise ReadinessError(f"protected test is oversized: {relative}")
        text = path.read_text(encoding="utf-8")
        for match in assertion.finditer(text):
            expected = match.group(2)
            exact_false = re.search(r"\bexact\s*:\s*false\b", match.group("options"))
            if expected == literal or exact_false and expected in literal:
                line = text.count("\n", 0, match.start()) + 1
                collisions.append(f"{relative}:{line} => {expected}")
    return collisions


def field(text: str, name: str) -> str:
    values = re.findall(
        rf"^{re.escape(name)}:\s*(.*?)\s*$", text, re.IGNORECASE | re.MULTILINE
    )
    if len(values) != 1 or not values[0]:
        raise ReadinessError(f"ticket requires exactly one {name} field")
    return values[0]


def builder_paths(text: str) -> list[str]:
    raw = field(text, "Builder ownership")
    if not raw.endswith(" only"):
        raise ReadinessError("Builder ownership must end with only")
    values = [item.strip() for item in raw[:-5].split(",")]
    if not values or len(values) != len(set(values)):
        raise ReadinessError("Builder ownership paths are empty or duplicated")
    for value in values:
        path = PurePosixPath(value)
        if (
            path.is_absolute()
            or not path.parts
            or value.endswith("/")
            or any(part in {"", ".", ".."} for part in path.parts)
            or not re.fullmatch(r"[A-Za-z0-9._/@+-]+", value)
        ):
            raise ReadinessError("Builder ownership contains an unsafe path")
    return values


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
        path, _literal = protected_test_conflict(entry)
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
    builder_paths(text)
    fixture_seams = paths(text, "Fixture-Seams", workdir)
    paths(text, "Authentication-Seams", workdir)
    protected_test_conflicts(text, workdir, fixture_seams)


def main() -> None:
    parser = argparse.ArgumentParser()
    choice = parser.add_mutually_exclusive_group(required=True)
    choice.add_argument("--ticket")
    choice.add_argument("--conflict-entry")
    choice.add_argument("--global-literal")
    parser.add_argument("--workdir", type=Path)
    args = parser.parse_args()
    try:
        if args.conflict_entry:
            protected_test_conflict(args.conflict_entry)
            print("CONFLICT DECLARATION PASS")
            return
        if args.global_literal:
            if args.workdir is None:
                raise ReadinessError("--workdir is required with --global-literal")
            collisions = protected_text_collisions(
                args.workdir.resolve(strict=True), args.global_literal,
            )
            if collisions:
                raise ReadinessError(
                    "global protected-test text collision: " + ", ".join(collisions)
                )
            print("GLOBAL TEXT PASS")
            return
        if args.workdir is None:
            raise ReadinessError("--workdir is required with --ticket")
        validate(args.ticket, args.workdir)
    except (OSError, UnicodeError, ReadinessError) as error:
        print(f"READINESS BLOCKED: {error}")
        raise SystemExit(1)
    print("READINESS PASS")


if __name__ == "__main__":
    main()
