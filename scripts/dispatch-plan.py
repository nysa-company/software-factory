#!/usr/bin/env python3
"""Atomically select, prepare, and claim one deterministic Ready ticket."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
from effective_ticket import (  # noqa: E402
    apply_operator_fields,
    load_mapping,
    operator_fields,
    ticket_branch_prefix,
)


SCHEMA = "nysa.software-factory.dispatch-plan/v1"
TICKET = re.compile(r"^T-([0-9]+)$")
PRIORITY = {"urgent": 0, "high": 1, "normal": 2, "low": 3, "none": 4}


class DispatchError(ValueError):
    pass


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def git(root: Path, *arguments: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *arguments],
        text=True,
        capture_output=True,
        check=False,
        timeout=120,
    )
    if check and result.returncode:
        raise DispatchError(result.stderr.strip() or "Git operation failed")
    return result.stdout


def certified_origin(product: Path) -> str:
    expected = os.environ.get("FACTORY_CERTIFIED_PRODUCT_ORIGIN", "")
    if not expected or any(character in expected for character in "\n\r\t"):
        raise DispatchError("certified product origin is unavailable")
    urls = [
        line for line in git(product, "remote", "get-url", "--push", "--all", "origin").splitlines()
        if line
    ]
    if urls != [expected]:
        raise DispatchError("product origin does not match certification")
    return expected


def safe_directory(path: Path, label: str, owner_only: bool = False) -> None:
    info = path.lstat()
    if (
        not path.is_absolute()
        or path.resolve(strict=True) != path
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_mode & 0o022
        or (owner_only and info.st_mode & 0o077)
    ):
        raise DispatchError(f"{label} is unsafe")


def safe_file(path: Path, label: str, maximum: int = 5_000_000) -> str:
    info = path.lstat()
    if (
        path.is_symlink()
        or not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_nlink != 1
        or info.st_mode & 0o022
        or info.st_size > maximum
    ):
        raise DispatchError(f"{label} is unsafe")
    return path.read_text(encoding="utf-8")


def field(text: str, name: str, default: str = "") -> str:
    values = re.findall(
        rf"^{re.escape(name)}:\s*(.*?)\s*$", text, re.MULTILINE | re.IGNORECASE
    )
    if len(values) > 1:
        raise DispatchError(f"ticket contains duplicate {name}")
    return values[0] if values else default


def capacity(factory: Path) -> int:
    descriptor = safe_file(factory / "PROJECT.env", "project descriptor", 100_000)
    values = []
    for raw in descriptor.splitlines():
        line = re.sub(r"^\s*export\s+", "", raw.strip())
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"MAX_CONCURRENT_TICKETS\s*=\s*['\"]?([0-9]+)['\"]?", line)
        if match:
            values.append(int(match.group(1)))
    if len(values) > 1:
        raise DispatchError("MAX_CONCURRENT_TICKETS is ambiguous")
    selected = values[0] if values else 1
    if not 1 <= selected <= 6:
        raise DispatchError("MAX_CONCURRENT_TICKETS is invalid")
    return selected


def fresh_mapping(path: Path, maximum_age: int) -> dict[str, Any]:
    raw = safe_file(path, "Linear operator map")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise DispatchError("Linear operator map is invalid")
    sync = value.get("_sync")
    if not isinstance(sync, dict):
        raise DispatchError("Linear reconciliation metadata is missing")
    error = sync.get("_last_error") or sync.get("last_error")
    if error:
        raise DispatchError("Linear reconciliation reports an error")
    timestamp = sync.get("last_success_at")
    if not isinstance(timestamp, str):
        raise DispatchError("Linear reconciliation timestamp is missing")
    try:
        observed = dt.datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DispatchError("Linear reconciliation timestamp is invalid") from exc
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=dt.timezone.utc)
    age = (dt.datetime.now(dt.timezone.utc) - observed).total_seconds()
    if age < -60 or age > maximum_age:
        raise DispatchError("Linear reconciliation is stale")
    return value


def lease_records(directory: Path) -> tuple[set[str], set[str]]:
    tickets: set[str] = set()
    leases: set[str] = set()
    if not directory.exists():
        return tickets, leases
    safe_directory(directory, "dispatcher lease directory")
    for path in sorted(directory.iterdir()):
        raw = safe_file(path, "dispatcher lease")
        value = json.loads(raw)
        ticket = value.get("ticket")
        lease = value.get("lease_id")
        if (
            value.get("schema_version") != 1
            or path.name != f"{ticket}.json"
            or not isinstance(ticket, str)
            or not TICKET.fullmatch(ticket)
            or not isinstance(lease, str)
            or not re.fullmatch(r"[0-9a-f]{64}", lease)
            or not isinstance(value.get("claimed_epoch"), int)
            or isinstance(value.get("claimed_epoch"), bool)
            or not isinstance(value.get("expires_epoch"), int)
            or isinstance(value.get("expires_epoch"), bool)
            or value["expires_epoch"] <= value["claimed_epoch"]
            or ticket in tickets
            or lease in leases
        ):
            raise DispatchError("dispatcher lease state is unsafe")
        tickets.add(ticket)
        leases.add(lease)
    return tickets, leases


def active_tickets(factory: Path) -> set[str]:
    result = set()
    root = factory / ".active-runs"
    if not root.exists():
        return result
    safe_directory(root, "active-run directory")
    for path in root.iterdir():
        match = re.match(r"^(T-[0-9]+)\.", path.name)
        if match:
            result.add(match.group(1))
    return result


def candidates(factory: Path, mapping: dict[str, Any], excluded: set[str]):
    result = []
    tickets = factory / "tickets"
    safe_directory(tickets, "ticket directory")
    pin = safe_file(factory / "KIT_PIN", "kit pin", 100).strip()
    if not re.fullmatch(r"[0-9a-f]{40}", pin):
        raise DispatchError("kit pin is invalid")
    for path in sorted(tickets.glob("T-*.md")):
        match = TICKET.fullmatch(path.stem)
        if not match or path.stem in excluded:
            continue
        text = safe_file(path, f"ticket {path.stem}")
        operator = operator_fields(mapping, path.stem)
        effective = apply_operator_fields(text, operator)
        ticket_pin = field(effective, "Kit-SHA")
        if ticket_pin and ticket_pin != pin:
            continue
        state = field(effective, "State").lower()
        resumable = (
            operator.get("state_base") == "blocked-escalated"
            and state in ("planning", "building", "review")
        )
        if state != "ready" and not resumable:
            continue
        initiative = field(effective, "Initiative")
        if not re.fullmatch(r"I-[0-9]+", initiative):
            continue
        priority = field(effective, "Priority", "none").lower()
        if priority not in PRIORITY:
            raise DispatchError(f"ticket {path.stem} priority is invalid")
        result.append(
            (
                PRIORITY[priority],
                int(match.group(1)),
                {
                    "initiative": initiative,
                    "priority": priority,
                    "resumable": resumable,
                    "state": field(effective, "State"),
                    "ticket": path.stem,
                },
            )
        )
    return [item[2] for item in sorted(result)]


def worktree_records(product: Path) -> list[dict[str, str]]:
    records = []
    current: dict[str, str] = {}
    for line in git(product, "worktree", "list", "--porcelain").splitlines() + [""]:
        if not line:
            if current:
                records.append(current)
                current = {}
            continue
        key, _, value = line.partition(" ")
        current[key] = value
    return records


def prepare_worktree(
    product: Path, worktree_root: Path, ticket: str, prefix: str, remote: str
) -> tuple[Path, bool, bool]:
    branch = prefix + ticket
    destination = worktree_root / ticket
    safe_directory(worktree_root, "worktree root", owner_only=True)
    records = worktree_records(product)
    git(product, "fetch", "--quiet", remote, "+main:refs/remotes/origin/main")
    main = git(product, "rev-parse", "origin/main").strip()
    remote_branch = git(
        product, "ls-remote", "--heads", remote, f"refs/heads/{branch}"
    ).split()
    if remote_branch and (len(remote_branch) != 2 or remote_branch[1] != f"refs/heads/{branch}"):
        raise DispatchError("ticket remote branch result is ambiguous")
    if destination.exists() or destination.is_symlink():
        safe_directory(destination, "ticket worktree")
        matching = [
            item for item in records if Path(item.get("worktree", "")).resolve() == destination
        ]
        if len(matching) != 1 or matching[0].get("branch") != f"refs/heads/{branch}":
            raise DispatchError("ticket worktree path collides with another worktree")
        if git(destination, "status", "--porcelain=v1", "-z"):
            raise DispatchError("ticket worktree is dirty")
        local = git(destination, "rev-parse", "HEAD").strip()
        expected = remote_branch[0] if remote_branch else main
        if local != expected:
            raise DispatchError("ticket worktree branch is divergent or unpushed")
        return destination, False, False
    if any(item.get("branch") == f"refs/heads/{branch}" for item in records):
        raise DispatchError("ticket branch is checked out at an unexpected path")
    if git(product, "show-ref", "--verify", f"refs/heads/{branch}", check=False):
        branch_sha = git(product, "rev-parse", branch).strip()
        if not remote_branch or remote_branch[0] != branch_sha:
            raise DispatchError("existing ticket branch is divergent or unpushed")
        git(product, "worktree", "add", "--quiet", str(destination), branch)
        branch_created = False
    else:
        git(
            product,
            "worktree",
            "add",
            "--quiet",
            "-b",
            branch,
            str(destination),
            main,
        )
        branch_created = True
    return destination, True, branch_created


def create_lease(
    directory: Path, ticket: str, existing: set[str], ttl: int
) -> dict[str, Any]:
    lease = secrets.token_hex(32)
    while lease in existing:
        lease = secrets.token_hex(32)
    now = int(time.time())
    value = {
        "claimed_epoch": now,
        "expires_epoch": now + ttl,
        "lease_id": lease,
        "schema_version": 1,
        "ticket": ticket,
    }
    destination = directory / f"{ticket}.json"
    descriptor, temporary = tempfile.mkstemp(prefix=".lease-", dir=directory)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            handle.write(canonical(value) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, destination)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        Path(temporary).unlink(missing_ok=True)
    return value


def lock(path: Path) -> None:
    for _ in range(100):
        try:
            path.mkdir(mode=0o700)
            return
        except FileExistsError:
            time.sleep(0.1)
    raise DispatchError("dispatcher lock is busy")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--factory-root", required=True, type=Path)
    parser.add_argument("--worktree-root", required=True, type=Path)
    parser.add_argument("--max-linear-age", type=int, default=600)
    parser.add_argument("--lease-ttl", type=int, default=900)
    parser.add_argument("action", choices=("shadow", "claim"))
    args = parser.parse_args()
    launch_lock = args.factory_root / "factory" / ".launch.lock"
    lease_lock = args.factory_root / "factory" / ".dispatch-leases.lock"
    held_launch = held_lease = False
    created_worktree: Path | None = None
    created_branch = ""
    lease_created = False
    try:
        product = args.factory_root.resolve(strict=True)
        if product != args.factory_root:
            raise DispatchError("factory root must be physical")
        safe_directory(product, "factory root")
        remote = certified_origin(product)
        factory = product / "factory"
        safe_directory(factory, "factory directory")
        if (factory / "KILL").exists() or (factory / "MAINTENANCE").exists():
            raise DispatchError("factory control blocks dispatch")
        if git(product, "status", "--porcelain=v1", "-z"):
            raise DispatchError("registered product checkout is dirty")
        mapping = fresh_mapping(factory / "linear-map.json", args.max_linear_age)
        maximum = capacity(factory)
        if maximum == 1:
            raise DispatchError("autonomous dispatch requires bounded concurrency")
        lease_dir = factory / ".dispatch-leases"
        if args.action == "claim":
            lock(launch_lock)
            held_launch = True
            lock(lease_lock)
            held_lease = True
            if (factory / "KILL").exists() or (factory / "MAINTENANCE").exists():
                raise DispatchError("factory control blocks dispatch")
            lease_dir.mkdir(mode=0o700, exist_ok=True)
            safe_directory(lease_dir, "dispatcher lease directory")
        leased, lease_ids = lease_records(lease_dir)
        if len(leased) >= maximum:
            print(canonical({
                "action": "WAIT", "reason_code": "capacity_full",
                "schema": SCHEMA, "status": "WAIT",
            }))
            return
        selected = candidates(factory, mapping, leased | active_tickets(factory))
        if not selected:
            print(canonical({
                "action": "WAIT", "reason_code": "no_candidate",
                "schema": SCHEMA, "status": "WAIT",
            }))
            return
        ticket = selected[0]
        if args.action == "shadow":
            print(canonical({
                **ticket, "action": "SHADOW", "schema": SCHEMA,
                "status": "SHADOW",
            }))
            return
        destination, created, branch_created = prepare_worktree(
            product, args.worktree_root, ticket["ticket"],
            ticket_branch_prefix(factory), remote,
        )
        if created:
            created_worktree = destination
        if branch_created:
            created_branch = ticket_branch_prefix(factory) + ticket["ticket"]
        lease = create_lease(
            lease_dir, ticket["ticket"], lease_ids, args.lease_ttl
        )
        lease_created = True
        print(
            canonical(
                {
                    **ticket,
                    "action": "START",
                    "branch": ticket_branch_prefix(factory) + ticket["ticket"],
                    "expires_epoch": lease["expires_epoch"],
                    "lease_id": lease["lease_id"],
                    "schema": SCHEMA,
                    "status": "CLAIMED",
                    "worktree": str(destination),
                }
            )
        )
    except (
        DispatchError,
        FileNotFoundError,
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        subprocess.SubprocessError,
        ValueError,
    ) as error:
        if created_worktree is not None and not lease_created:
            subprocess.run(
                ["git", "-C", str(args.factory_root), "worktree", "remove", "--force",
                 str(created_worktree)],
                capture_output=True,
                check=False,
            )
            if created_branch:
                subprocess.run(
                    ["git", "-C", str(args.factory_root), "branch", "-D", created_branch],
                    capture_output=True,
                    check=False,
                )
        print(canonical({
            "action": "ESCALATE", "error": str(error),
            "reason_code": "unsafe_state", "schema": SCHEMA, "status": "error",
        }))
        raise SystemExit(2)
    finally:
        if held_lease:
            lease_lock.rmdir()
        if held_launch:
            launch_lock.rmdir()


if __name__ == "__main__":
    main()
