#!/usr/bin/env python3
"""Deterministic one-at-a-time publication lease over concurrent PR validation."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile
import time
from typing import Any


SCHEMA = "nysa.software-factory.publication-lease/v1"
TICKET = re.compile(r"^T-([0-9]+)$")
SHA = re.compile(r"^[0-9a-f]{40}$")
DIGEST = re.compile(r"^[0-9a-f]{64}$")
PRIORITY = {"urgent": 0, "high": 1, "normal": 2, "low": 3, "none": 4}


class LeaseError(ValueError):
    pass


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def safe_directory(path: Path, create: bool = False) -> Path:
    if create:
        path.mkdir(mode=0o700, parents=False, exist_ok=True)
    info = path.lstat()
    if (
        not path.is_absolute()
        or path.resolve(strict=True) != path
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise LeaseError("publication directory is unsafe")
    return path


def read(path: Path) -> dict[str, Any]:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_size > 100_000
        ):
            raise LeaseError("publication state is unsafe")
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            descriptor = -1
            value = json.load(stream)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not isinstance(value, dict) or value.get("schema") != SCHEMA:
        raise LeaseError("publication state is malformed")
    return value


def write(path: Path, value: dict[str, Any]) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = -1
            stream.write(canonical(value) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        Path(temporary).unlink(missing_ok=True)


def queue(publication: Path) -> list[dict[str, Any]]:
    values = []
    for path in sorted((publication / "queue").glob("T-*.json")):
        value = read(path)
        match = TICKET.fullmatch(value.get("ticket", ""))
        if (
            not match
            or path.name != f"{value['ticket']}.json"
            or value.get("priority") not in PRIORITY
            or not SHA.fullmatch(value.get("head_sha", ""))
            or not isinstance(value.get("publication_ready_at"), int)
        ):
            raise LeaseError("publication queue record is malformed")
        values.append(value)
    return sorted(
        values,
        key=lambda item: (
            PRIORITY[item["priority"]],
            item["publication_ready_at"],
            int(TICKET.fullmatch(item["ticket"]).group(1)),
        ),
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("ready", "acquire", "release"))
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--ticket", required=True)
    parser.add_argument("--head", default="")
    parser.add_argument("--priority", choices=tuple(PRIORITY), default="none")
    parser.add_argument("--lease", default="")
    parser.add_argument("--ttl", type=int, default=300)
    args = parser.parse_args()
    try:
        if (
            not TICKET.fullmatch(args.ticket)
            or (args.action != "release" and not SHA.fullmatch(args.head))
            or (args.action == "release" and not DIGEST.fullmatch(args.lease))
            or not 30 <= args.ttl <= 900
        ):
            raise LeaseError("invalid publication lease arguments")
        state = safe_directory(args.state_dir)
        publication = state / "publication"
        safe_directory(publication, create=True)
        queued = publication / "queue"
        safe_directory(queued, create=True)
        lock_path = publication / ".lock"
        descriptor = os.open(
            lock_path,
            os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        with os.fdopen(descriptor, "r+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            now = int(time.time())
            queue_path = queued / f"{args.ticket}.json"
            lease_path = publication / "active.json"
            if args.action == "ready":
                prior = read(queue_path) if queue_path.exists() else {}
                ready_at = (
                    prior.get("publication_ready_at", now)
                    if prior.get("head_sha") == args.head else now
                )
                write(queue_path, {
                    "head_sha": args.head,
                    "priority": args.priority,
                    "publication_ready_at": ready_at,
                    "schema": SCHEMA,
                    "ticket": args.ticket,
                })
                result = {"publication_ready_at": ready_at, "status": "queued"}
            elif args.action == "acquire":
                active = read(lease_path) if lease_path.exists() else None
                if active and active.get("expires_epoch", 0) <= now:
                    lease_path.unlink()
                    active = None
                if active:
                    result = {
                        "holder": active["ticket"],
                        "status": (
                            "acquired"
                            if active["ticket"] == args.ticket
                            and active["head_sha"] == args.head
                            else "queued"
                        ),
                    }
                    if result["status"] == "acquired":
                        active["expires_epoch"] = now + args.ttl
                        write(lease_path, active)
                        result["lease"] = active["lease"]
                        result["expires_epoch"] = active["expires_epoch"]
                else:
                    candidates = queue(publication)
                    if not candidates or candidates[0]["ticket"] != args.ticket:
                        result = {
                            "holder": candidates[0]["ticket"] if candidates else None,
                            "status": "queued",
                        }
                    elif candidates[0]["head_sha"] != args.head:
                        raise LeaseError("publication queue head changed")
                    else:
                        active = {
                            **candidates[0],
                            "expires_epoch": now + args.ttl,
                            "lease": secrets.token_hex(32),
                        }
                        write(lease_path, active)
                        result = {
                            "expires_epoch": active["expires_epoch"],
                            "lease": active["lease"],
                            "status": "acquired",
                        }
            else:
                active = read(lease_path) if lease_path.exists() else None
                if (
                    not active
                    or active.get("ticket") != args.ticket
                    or active.get("lease") != args.lease
                ):
                    raise LeaseError("publication lease does not match")
                lease_path.unlink()
                queue_path.unlink(missing_ok=True)
                result = {"status": "released"}
        print(canonical({
            **result, "schema": SCHEMA, "ticket": args.ticket,
        }))
    except (
        FileNotFoundError, json.JSONDecodeError, OSError, LeaseError,
    ) as error:
        print(canonical({
            "error": str(error), "schema": SCHEMA, "status": "error",
            "ticket": args.ticket,
        }))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
