#!/usr/bin/env python3
"""Validate and signal one factory-owned process group without PID-reuse risk."""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import time


RUN_ID = re.compile(r"[A-Za-z0-9._-]{1,200}")
TICKET = re.compile(r"T-[0-9]+")


class IdentityError(ValueError):
    """The process record is malformed, stale, or does not own the process."""


@dataclasses.dataclass(frozen=True)
class Process:
    pid: int
    pgid: int
    started: str


@dataclasses.dataclass(frozen=True)
class AttemptIdentity:
    run_id: str
    ticket: str
    leader: Process
    members: tuple[Process, ...]


def _regular_bytes_at(directory: int, name: str) -> bytes:
    before = os.stat(name, dir_fd=directory, follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise IdentityError(f"unsafe factory record: {name}")
    descriptor = os.open(
        name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=directory,
    )
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            raise IdentityError(f"factory record changed while opening: {name}")
        chunks = []
        total = 0
        while True:
            chunk = os.read(descriptor, 65536)
            if not chunk:
                break
            total += len(chunk)
            if total > 1024 * 1024:
                raise IdentityError(f"oversized factory record: {name}")
            chunks.append(chunk)
        after = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise IdentityError(f"factory record changed while reading: {name}")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def record_bytes(path: Path) -> bytes:
    parent_before = path.parent.lstat()
    if not stat.S_ISDIR(parent_before.st_mode):
        raise IdentityError("factory record parent must be a real directory")
    directory = os.open(
        path.parent,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        parent_after = os.fstat(directory)
        if (parent_before.st_dev, parent_before.st_ino) != (
            parent_after.st_dev,
            parent_after.st_ino,
        ):
            raise IdentityError("factory record parent changed while opening")
        return _regular_bytes_at(directory, path.name)
    finally:
        os.close(directory)


def parse_fields(raw: bytes, label: str) -> dict[str, str]:
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeDecodeError as error:
        raise IdentityError(f"{label} is not UTF-8") from error
    values: dict[str, str] = {}
    for line in lines:
        if not line or "=" not in line:
            raise IdentityError(f"{label} is malformed")
        key, value = line.split("=", 1)
        if key in values:
            raise IdentityError(f"{label} contains duplicate field: {key}")
        values[key] = value
    return values


def process_table() -> dict[int, Process]:
    result = subprocess.run(
        ["ps", "-axo", "pid=,pgid=,lstart="],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise IdentityError("could not inspect the process table")
    table: dict[int, Process] = {}
    for line in result.stdout.splitlines():
        fields = line.split(maxsplit=2)
        if len(fields) != 3 or not fields[0].isdigit() or not fields[1].isdigit():
            continue
        pid, pgid = int(fields[0]), int(fields[1])
        table[pid] = Process(pid, pgid, " ".join(fields[2].split()))
    return table


def load_identity(
    runs_dir: Path,
    run_id: str,
    *,
    expected_ticket: str | None = None,
    table: dict[int, Process] | None = None,
) -> AttemptIdentity:
    if not RUN_ID.fullmatch(run_id):
        raise IdentityError("invalid run identity")
    directory = runs_dir.lstat()
    if not stat.S_ISDIR(directory.st_mode):
        raise IdentityError("runs root must be a real directory")
    pid_values = parse_fields(record_bytes(runs_dir / f"{run_id}.pid"), "PID record")
    manifest = parse_fields(record_bytes(runs_dir / f"{run_id}.meta"), "run manifest")
    if set(pid_values) != {"pid", "pgid", "run_id", "process_start"}:
        raise IdentityError("PID record has unexpected fields")
    if (
        pid_values["run_id"] != run_id
        or manifest.get("run_id") != run_id
        or not pid_values["pid"].isdigit()
        or not pid_values["pgid"].isdigit()
        or not pid_values["process_start"]
    ):
        raise IdentityError("PID record does not match the run")
    ticket = manifest.get("ticket", "")
    if not TICKET.fullmatch(ticket) or (
        expected_ticket is not None and ticket != expected_ticket
    ):
        raise IdentityError("run is owned by a different ticket")
    pid, pgid = int(pid_values["pid"]), int(pid_values["pgid"])
    if pid <= 1 or pgid <= 1 or pid != pgid:
        raise IdentityError("factory process group leader is invalid")
    if (
        manifest.get("pid") != str(pid)
        or manifest.get("pgid") != str(pgid)
        or manifest.get("process_start") != pid_values["process_start"]
    ):
        raise IdentityError("manifest and PID identity disagree")
    table = process_table() if table is None else table
    leader = table.get(pid)
    if (
        leader is None
        or leader.pgid != pgid
        or leader.started != pid_values["process_start"]
    ):
        raise IdentityError("factory process identity is stale or mismatched")
    members = tuple(sorted(
        (process for process in table.values() if process.pgid == pgid),
        key=lambda process: process.pid,
    ))
    if not members:
        raise IdentityError("factory process group is empty")
    return AttemptIdentity(run_id, ticket, leader, members)


def group_alive(pgid: int, *, table: dict[int, Process] | None = None) -> bool:
    table = process_table() if table is None else table
    return any(process.pgid == pgid for process in table.values())


def revalidate_members(identity: AttemptIdentity) -> bool:
    current = process_table()
    expected = {process.pid: process for process in identity.members}
    members = [process for process in current.values() if process.pgid == identity.leader.pgid]
    if not members:
        return False
    for process in members:
        prior = expected.get(process.pid)
        if prior is None or prior.started != process.started:
            raise IdentityError("process group membership changed before escalation")
    return True


def signal_group(identity: AttemptIdentity, sig: int) -> None:
    if sig == signal.SIGTERM:
        # The leader must still match immediately before the first signal.
        current = process_table()
        leader = current.get(identity.leader.pid)
        if leader != identity.leader:
            raise IdentityError("factory process identity changed before TERM")
    elif not revalidate_members(identity):
        return
    try:
        os.killpg(identity.leader.pgid, sig)
    except ProcessLookupError:
        return
    except PermissionError as error:
        raise IdentityError("permission denied signaling factory process group") from error


def terminate(identity: AttemptIdentity, timeout: float) -> str:
    signal_group(identity, signal.SIGTERM)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not group_alive(identity.leader.pgid):
            return "TERM"
        time.sleep(0.02)
    signal_group(identity, signal.SIGKILL)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not group_alive(identity.leader.pgid):
            return "KILL"
        time.sleep(0.02)
    raise IdentityError("factory process group survived TERM and KILL")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("validate", "terminate"))
    parser.add_argument("--runs-dir", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--ticket")
    parser.add_argument("--timeout", type=float, default=2.0)
    args = parser.parse_args()
    identity = load_identity(
        args.runs_dir, args.run_id, expected_ticket=args.ticket,
    )
    value = {
        "pgid": identity.leader.pgid,
        "pid": identity.leader.pid,
        "process_start": identity.leader.started,
        "run_id": identity.run_id,
        "ticket": identity.ticket,
    }
    if args.action == "terminate":
        value["signal"] = terminate(identity, args.timeout)
    print(json.dumps(value, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    try:
        main()
    except (OSError, IdentityError) as error:
        raise SystemExit(f"process-identity: {error}")
