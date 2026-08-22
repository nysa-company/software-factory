#!/usr/bin/env python3
"""Validate the operator-owned, provider-free ticket readiness contract."""

from __future__ import annotations

import argparse
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import shlex
import stat
import subprocess
import sys


sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
from ticket_state_transition import TransitionError, exact_state  # noqa: E402


class ReadinessError(ValueError):
    pass


PRIORITIES = frozenset({"none", "urgent", "high", "normal", "low"})


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
        rf"^{re.escape(name)}:[ \t]*(.*?)[ \t]*$",
        text, re.IGNORECASE | re.MULTILINE,
    )
    if len(values) != 1 or not values[0]:
        raise ReadinessError(f"ticket requires exactly one {name} field")
    return values[0]


def dependencies(text: str, ticket: str) -> tuple[str, ...]:
    raw = field(text, "Depends-On")
    if raw.casefold() == "none":
        return ()
    values = tuple(item.strip() for item in raw.split(","))
    if (
        not values
        or any(not re.fullmatch(r"T-[0-9]+", item) for item in values)
        or len(values) != len(set(values))
        or ticket in values
    ):
        raise ReadinessError("ticket Depends-On is invalid")
    return values


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
        try:
            info = candidate.lstat()
        except OSError as error:
            raise ReadinessError(f"{name} path is unavailable: {value}") from error
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
) -> set[str]:
    raw = field(text, "Protected-Test-Conflicts")
    if raw.casefold() == "none":
        return set()
    entries = [item.strip() for item in raw.split(",")]
    if not entries or len(entries) != len(set(entries)):
        raise ReadinessError("Protected-Test-Conflicts entries are duplicated")
    declared = set()
    for entry in entries:
        try:
            path, _literal = protected_test_conflict(entry)
        except ReadinessError as error:
            raise ReadinessError(f"Protected-Test-Conflicts: {error}") from error
        conflict_paths = paths(
            f"Protected-Test-Conflict-Path: {path}",
            "Protected-Test-Conflict-Path",
            workdir,
        )
        if conflict_paths[0] not in test_author_paths:
            raise ReadinessError(
                f"protected-test conflict lacks Test-author ownership: {entry}"
            )
        declared.add(path)
    return declared


def protected_source_collisions(
    workdir: Path, mutable_paths: list[str], test_paths: list[str]
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    tracked = subprocess.run(
        ["git", "-C", str(workdir), "ls-files", "-z"],
        capture_output=True, check=True,
    )
    mutable = set(mutable_paths)
    hash_collisions = set()
    assertion_collisions = set()
    sha256 = re.compile(r"\bcreateHash\(\s*(['\"`])sha256\1\s*\)", re.IGNORECASE)
    path_digest = re.compile(
        r"\[\s*(['\"`])(\.[^'\"`\r\n]+)\1\s*,\s*"
        r"(['\"`])[0-9a-f]{64}\3\s*,?\s*\]", re.IGNORECASE,
    )
    for raw in tracked.stdout.split(b"\0"):
        if not raw:
            continue
        relative = raw.decode("utf-8", errors="strict")
        if not re.search(r"(?:^|/)[^/]*(?:test|spec)\.[cm]?[jt]sx?$", relative):
            continue
        path = workdir / relative
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or path.is_symlink():
            raise ReadinessError(f"protected test is unsafe: {relative}")
        if info.st_size > 1_048_576:
            raise ReadinessError(f"protected test is oversized: {relative}")
        text = path.read_text(encoding="utf-8")
        test_parent = str(PurePosixPath(relative).parent)
        workspace_roots = {
            str(PurePosixPath(prefix).parent)
            for prefix in test_paths
            if relative == prefix or relative.startswith(prefix + "/")
        }
        reads = re.finditer(
            r"\b(?:const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)"
            r"(?:\s*:[^=;\r\n]+)?\s*=\s*readFileSync\s*\("
            r"(.{0,1000}?)\)\s*;",
            text, re.DOTALL,
        )
        for read in reads:
            variable, arguments = read.groups()
            assertion_scope = text[read.end():read.end() + 5000]
            test_end = assertion_scope.find("\n});")
            if test_end >= 0:
                assertion_scope = assertion_scope[:test_end]
            asserted = re.search(
                rf"\bexpect\(\s*{re.escape(variable)}\b", assertion_scope,
            ) or re.search(
                rf"\bassert(?:\.[A-Za-z_$][A-Za-z0-9_$]*)?\(\s*"
                rf"{re.escape(variable)}\b", assertion_scope,
            )
            if not asserted:
                continue
            literals = {
                posixpath.normpath(value)
                for _, value in re.findall(
                    r"(['\"`])([^'\"`\r\n]+)\1", arguments,
                )
            }
            for mutable_path in mutable:
                candidates = {
                    posixpath.normpath(mutable_path),
                    posixpath.relpath(mutable_path, test_parent),
                    *(posixpath.relpath(mutable_path, root)
                      for root in workspace_roots),
                }
                if literals & candidates:
                    assertion_collisions.add((relative, mutable_path))
        if "readFileSync" not in text or not sha256.search(text):
            continue
        parent = str(PurePosixPath(relative).parent)
        # ponytail: recognize exact static path/digest tables; semantic source
        # assertions stay with Planner and Spec-linter.
        for match in path_digest.finditer(text):
            candidate = posixpath.normpath(posixpath.join(parent, match.group(2)))
            if candidate in mutable:
                hash_collisions.add((relative, candidate))
    return sorted(hash_collisions), sorted(assertion_collisions)


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
    try:
        state = exact_state(text)
    except TransitionError as error:
        if str(error).endswith("is invalid"):
            raise ReadinessError("ticket State is invalid") from error
        # Doctor maps this established wording to ticket_state_conflict.
        raise ReadinessError("ticket requires exactly one State field") from error
    if field(text, "Priority").casefold() not in PRIORITIES:
        raise ReadinessError("ticket Priority is invalid")
    initiative = field(text, "Initiative")
    if not re.fullmatch(r"I-[0-9]+", initiative):
        raise ReadinessError("ticket Initiative is invalid")
    initiative_path = workdir / "factory" / "initiatives" / f"{initiative}.md"
    try:
        initiative_info = initiative_path.lstat()
    except OSError as error:
        raise ReadinessError("ticket Initiative record is unavailable") from error
    if (
        initiative_path.is_symlink()
        or not stat.S_ISREG(initiative_info.st_mode)
        or initiative_info.st_uid != os.geteuid()
        or subprocess.run(
            ["git", "-C", str(workdir), "ls-files", "--error-unmatch", "--",
             str(initiative_path.relative_to(workdir))],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        ).returncode
    ):
        raise ReadinessError("ticket Initiative record is unsafe")
    dependencies(text, ticket)
    merge_policies = re.findall(
        r"^Merge-Policy:[ \t]*(.*?)[ \t]*$", text, re.IGNORECASE | re.MULTILINE,
    )
    if len(merge_policies) > 1 or (
        merge_policies and merge_policies[0].casefold() not in {"manual", "auto"}
    ):
        raise ReadinessError("ticket Merge-Policy is invalid")
    approvals = re.findall(
        r"^Operator-Approval:[ \t]*(.*?)[ \t]*$",
        text, re.IGNORECASE | re.MULTILINE,
    )
    allowed_approvals = (
        {"linear", "receipt", "migration"} if state == "done"
        else {"linear", "receipt"} if state == "approved"
        else set()
    )
    if (
        len(approvals) > 1
        or (state in {"approved", "done"} and len(approvals) != 1)
        or (approvals and approvals[0].casefold() not in allowed_approvals)
    ):
        raise ReadinessError("ticket Operator-Approval is invalid")
    resume_states = re.findall(
        r"^Resume-State:[ \t]*(.*?)[ \t]*$", text, re.IGNORECASE | re.MULTILINE,
    )
    if len(resume_states) > 1 or (
        resume_states
        and resume_states[0].casefold()
        not in {"backlog", "ready", "planning", "building", "review"}
    ):
        raise ReadinessError("ticket Resume-State is invalid")
    kit_shas = re.findall(
        r"^Kit-SHA:\s*(.*?)\s*$", text, re.IGNORECASE | re.MULTILINE
    )
    if kit_shas:
        pin_path = workdir / "factory" / "KIT_PIN"
        pin_info = pin_path.lstat()
        if (
            len(kit_shas) != 1
            or not re.fullmatch(r"[0-9a-f]{40}", kit_shas[0])
            or not stat.S_ISREG(pin_info.st_mode)
            or pin_path.is_symlink()
            or pin_info.st_uid != os.geteuid()
            or pin_info.st_size > 100
        ):
            raise ReadinessError("ticket Kit-SHA or factory/KIT_PIN is invalid")
        pin = pin_path.read_text(encoding="utf-8").strip()
        if not re.fullmatch(r"[0-9a-f]{40}", pin):
            raise ReadinessError("ticket Kit-SHA or factory/KIT_PIN is invalid")
        if kit_shas[0] != pin:
            if state != "done":
                raise ReadinessError("ticket Kit-SHA does not match factory/KIT_PIN")
    if state == "done":
        from legacy_closeout import ValidationError, protected_terminal

        try:
            protected_terminal(workdir, ticket)
        except ValidationError as error:
            raise ReadinessError(
                "ticket protected terminal evidence is invalid"
            ) from error
    if field(text, "Product-Decisions").casefold() != "frozen":
        raise ReadinessError("product decisions are not frozen")
    builder = builder_paths(text)
    fixture_seams = paths(text, "Fixture-Seams", workdir)
    project_path = workdir / "factory" / "PROJECT.env"
    project_info = project_path.lstat()
    if (
        not stat.S_ISREG(project_info.st_mode)
        or project_path.is_symlink()
        or project_info.st_uid != os.geteuid()
    ):
        raise ReadinessError("repository TEST_PATHS is unsafe")
    values = re.findall(
        r"(?m)^TEST_PATHS=(.*)$", project_path.read_text(encoding="utf-8")
    )
    if len(values) != 1:
        raise ReadinessError("repository TEST_PATHS is missing or ambiguous")
    try:
        test_paths = " ".join(
            shlex.split(values[0], comments=False, posix=True)
        ).split()
    except ValueError as error:
        raise ReadinessError("repository TEST_PATHS is invalid") from error
    safe = re.compile(r"[A-Za-z0-9._][A-Za-z0-9._/-]*")
    normalized = [value.rstrip("/") for value in test_paths]
    if (
        not test_paths
        or len(test_paths) != len(set(test_paths))
        or any(
            not safe.fullmatch(value)
            or any(part in {"", ".", ".."} for part in value.split("/"))
            or value == "factory"
            or value.startswith("factory/")
            for value in normalized
        )
        or any(
            left == right
            or left.startswith(right + "/")
            or right.startswith(left + "/")
            for index, left in enumerate(normalized)
            for right in normalized[index + 1:]
        )
    ):
        raise ReadinessError("repository TEST_PATHS is invalid")

    def is_test_path(value: str) -> bool:
        return any(
            value == prefix or value.startswith(prefix + "/")
            for prefix in normalized
        )

    for value in builder:
        if is_test_path(value):
            raise ReadinessError(f"Builder ownership path is inside TEST_PATHS: {value}")
    for value in fixture_seams:
        if not is_test_path(value):
            raise ReadinessError(f"Fixture-Seams path is outside TEST_PATHS: {value}")
    paths(text, "Authentication-Seams", workdir)
    declared_conflicts = protected_test_conflicts(text, workdir, fixture_seams)
    hash_collisions, assertion_collisions = protected_source_collisions(
        workdir, builder + fixture_seams, normalized,
    )
    for protected_test, mutable_path in hash_collisions:
        if protected_test not in declared_conflicts:
            raise ReadinessError(
                "protected source hash collision: "
                f"{protected_test} => {mutable_path}"
            )
    for protected_test, mutable_path in assertion_collisions:
        if protected_test not in declared_conflicts:
            raise ReadinessError(
                "protected source assertion collision: "
                f"{protected_test} => {mutable_path}"
            )


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
