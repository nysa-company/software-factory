#!/usr/bin/env python3
"""Contract 1.8 non-agent ticket reconciliation controller."""

from __future__ import annotations

import argparse
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import tempfile
from threading import Lock
import time
from typing import Any
from urllib.parse import urlsplit


SCHEMA = "nysa.software-factory.controller/v1"
CLAIM_SCHEMA = "nysa.software-factory.controller-claim/v1"
EVENT_SCHEMA = "nysa.software-factory.controller-event/v1"
QUALIFICATION_SCHEMA = "nysa.software-factory.qualification/v2"
TICKET = re.compile(r"^T-[0-9]+$")
SHA = re.compile(r"^[0-9a-f]{40}$")
DIGEST = re.compile(r"^[0-9a-f]{64}$")
TERMINAL_ACCOUNTING = {
    "completed", "launch_void", "abandoned_conservative", "cancelled",
    "cancelled_conservative",
}
RECONCILE_INTERVAL_SECONDS = 15


class ControllerError(ValueError):
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
        raise ControllerError("controller directory is unsafe")
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
            or info.st_size > 1_000_000
        ):
            raise ControllerError("controller claim is unsafe")
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            descriptor = -1
            value = json.load(stream)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not isinstance(value, dict):
        raise ControllerError("controller claim is malformed")
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


def fields(path: Path) -> dict[str, str]:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) & 0o022
            or info.st_size > 1_000_000
        ):
            raise ControllerError("run manifest is unsafe")
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            descriptor = -1
            lines = stream.read().splitlines()
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    values: dict[str, str] = {}
    for line in lines:
        name, separator, value = line.partition("=")
        if not separator or name in values:
            raise ControllerError("run manifest is malformed")
        values[name] = value
    return values


def progress_summary(path: Path) -> dict[str, Any]:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_size > 10_000_000
        ):
            raise ControllerError("attempt progress journal is unsafe")
        chunks = []
        remaining = 10_000_001
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
    finally:
        os.close(descriptor)
    if len(raw) > 10_000_000:
        raise ControllerError("attempt progress journal is oversized")
    sequence = 0
    observed = -1
    latest_type = ""
    latest_subtype = ""
    for line in raw.splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError as error:
            raise ControllerError("attempt progress journal is malformed") from error
        if (
            not isinstance(record, dict)
            or set(record) != {
                "event_sha256", "observed_monotonic_ns", "sequence",
                "subtype", "type",
            }
            or record.get("sequence") != sequence + 1
            or not isinstance(record.get("observed_monotonic_ns"), int)
            or isinstance(record.get("observed_monotonic_ns"), bool)
            or record["observed_monotonic_ns"] <= observed
            or not DIGEST.fullmatch(record.get("event_sha256", ""))
            or record.get("type") not in {
                "assistant", "result", "system", "tool_call",
            }
            or not isinstance(record.get("subtype"), str)
            or len(record["subtype"]) > 64
            or not re.fullmatch(r"[A-Za-z0-9._:-]*", record["subtype"])
        ):
            raise ControllerError("attempt progress journal is malformed")
        sequence = record["sequence"]
        observed = record["observed_monotonic_ns"]
        latest_type = record["type"]
        latest_subtype = record["subtype"]
    return {
        "journal_bytes": len(raw),
        "journal_sha256": hashlib.sha256(raw).hexdigest(),
        "latest_subtype": latest_subtype,
        "latest_type": latest_type,
        "observed_monotonic_ns": observed if sequence else None,
        "progress_events": sequence,
    }


class Controller:
    def __init__(self, args: argparse.Namespace):
        self.launcher = args.launcher.resolve(strict=True)
        self.project = args.project
        self.product = args.product_root.resolve(strict=True)
        self.release_path = args.release_path.resolve(strict=True)
        self.state = safe_directory(args.state_dir)
        self.claims = self.state / "claims"
        safe_directory(self.claims, create=True)
        self.logs = self.state / "logs"
        safe_directory(self.logs, create=True)
        self.events = self.state / "events"
        safe_directory(self.events, create=True)
        self.capacity = self.read_capacity()
        self.qualification = self.read_qualification()
        self.fallback_lock = Lock()
        # ponytail: cells share one Git common directory; use per-cell refs only if refresh throughput matters.
        self.git_lock = Lock()

    def read_qualification(self) -> dict[str, Any] | None:
        path = self.product / "factory/QUALIFICATION.json"
        if not path.exists():
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
        if value.get("schema") != QUALIFICATION_SCHEMA:
            return None
        successor = value.get("mode") == "successor"
        tickets = value.get("tickets")
        target_done = value.get("target_done")
        if (
            set(value) != {
                "budget_usd", "capacity", "contract_version", "factory_sha",
                "generation", "per_run_budget_usd", "per_ticket_budget_usd",
                "schema", "target_done", "tickets",
            } | ({"mode", "source_factory_sha"} if successor else set())
            or value.get("contract_version") != "1.8.0"
            or value.get("capacity") not in (3, 4)
            or value.get("capacity") != self.capacity
            or target_done not in (3, 4)
            or target_done > value.get("capacity")
            or value.get("factory_sha") != self.release_path.name
            or (
                successor
                and (
                    target_done != 3
                    or value.get("capacity") != 3
                    or not SHA.fullmatch(value.get("source_factory_sha", ""))
                    or value["source_factory_sha"] == self.release_path.name
                    or value.get("budget_usd") != "300.000000"
                    or value.get("per_ticket_budget_usd") != "100.000000"
                    or value.get("per_run_budget_usd") != "10.000000"
                )
            )
            or (
                not successor
                and (
                    value.get("budget_usd") != "100.000000"
                    or value.get("per_ticket_budget_usd") != "25.000000"
                    or value.get("per_run_budget_usd") != "2.000000"
                )
            )
            or not isinstance(value.get("generation"), int)
            or isinstance(value.get("generation"), bool)
            or value["generation"] < 1
            or not isinstance(tickets, list)
            or len(tickets) != target_done
            or len(tickets) != len(set(tickets))
            or any(
                not isinstance(ticket, str) or not TICKET.fullmatch(ticket)
                for ticket in tickets
            )
        ):
            raise ControllerError("Contract 1.8 qualification manifest is invalid")
        return value

    def event(self, name: str, ticket: str = "", **details: Any) -> None:
        value = {
            "event": name,
            "factory_sha": self.release_path.name,
            "observed_at_epoch_ns": time.time_ns(),
            "schema": EVENT_SCHEMA,
            "ticket": ticket or None,
            **details,
        }
        raw = canonical(value).encode()
        value["event_sha256"] = hashlib.sha256(raw).hexdigest()
        path = self.events / (
            f"{value['observed_at_epoch_ns']}-{secrets.token_hex(8)}.json"
        )
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(canonical(value) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def marker(self, name: str, value: dict[str, Any] | None = None) -> bool:
        path = self.state / f"{name}.json"
        if value is None:
            return path.exists()
        try:
            descriptor = os.open(
                path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
        except FileExistsError:
            return False
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(canonical(value) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        return True

    def qualification_marker(self, name: str, create: bool = False) -> bool:
        if self.qualification is None:
            raise ControllerError("qualification marker requires qualification")
        expected = {
            "factory_sha": self.release_path.name,
            "schema": EVENT_SCHEMA,
            "tickets": sorted(self.qualification["tickets"]),
        }
        scoped = f"{name}-{self.release_path.name}"
        path = self.state / f"{scoped}.json"
        if not path.exists():
            if not create:
                return False
            if self.marker(scoped, expected):
                return True
        try:
            value = read(path)
        except (json.JSONDecodeError, OSError) as error:
            raise ControllerError("qualification marker is invalid") from error
        if value != expected:
            raise ControllerError("qualification marker is invalid")
        return True

    def read_capacity(self) -> int:
        values = re.findall(
            r"^(?:export\s+)?MAX_CONCURRENT_TICKETS\s*=\s*['\"]?([0-9]+)['\"]?\s*$",
            (self.product / "factory/PROJECT.env").read_text(encoding="utf-8"),
            re.M,
        )
        if len(values) > 1:
            raise ControllerError("MAX_CONCURRENT_TICKETS is ambiguous")
        capacity = int(values[0]) if values else 4
        if not 1 <= capacity <= 4:
            raise ControllerError("Contract 1.8 controller capacity must be 1 through 4")
        return capacity

    def call(self, *arguments: str, timeout: int | None = 300) -> subprocess.CompletedProcess:
        return subprocess.run(
            [str(self.launcher), self.project, *arguments],
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout,
        )

    def json_call(
        self, *arguments: str, allow: tuple[int, ...] = (0,),
        timeout: int | None = 300,
    ) -> dict[str, Any]:
        result = self.call(*arguments, timeout=timeout)
        if result.returncode not in allow:
            raise ControllerError(
                result.stderr.strip() or result.stdout.strip() or "launcher command failed"
            )
        try:
            value = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise ControllerError("launcher returned malformed JSON") from error
        if not isinstance(value, dict):
            raise ControllerError("launcher returned malformed JSON")
        return value

    def claim_path(self, ticket: str) -> Path:
        return self.claims / f"{ticket}.json"

    def envelope_digest(self) -> str:
        digest = hashlib.sha256(
            (self.product / "factory/ENVELOPE.env").read_bytes()
        )
        overrides = self.product / "factory/envelope-overrides"
        if overrides.is_dir():
            for path in sorted(overrides.glob("*.json")):
                digest.update(path.name.encode())
                digest.update(b"\0")
                digest.update(path.read_bytes())
        return digest.hexdigest()

    @staticmethod
    def runnable(claim: dict[str, Any]) -> bool:
        return claim["status"] not in {"blocked", "budget"}

    @staticmethod
    def typed_launch_void(terminal: dict[str, str]) -> bool:
        return (
            terminal.get("phase") == "abandoned"
            and terminal.get("accounting_state") == "launch_void"
            and terminal.get("go_issued") == "0"
            and terminal.get("task_submitted") == "0"
            and terminal.get("effective_cost") == "0"
            and terminal.get("cost_basis") == "launch_void"
        )

    @staticmethod
    def consumes_capacity(claim: dict[str, Any]) -> bool:
        return (
            claim["status"] in {"claimed", "running"}
            and claim.get("parked") is not True
            and claim.get("lease_released") is not True
            and bool(DIGEST.fullmatch(claim.get("lease", "")))
        )

    @staticmethod
    def parked(claim: dict[str, Any]) -> bool:
        return claim.get("parked") is True

    def save_claim(self, claim: dict[str, Any]) -> None:
        write(self.claim_path(claim["ticket"]), claim)

    def load_claims(self) -> list[dict[str, Any]]:
        result = []
        for path in sorted(self.claims.glob("T-*.json")):
            value = read(path)
            if (
                value.get("schema") != CLAIM_SCHEMA
                or path.name != f"{value.get('ticket')}.json"
                or not TICKET.fullmatch(value.get("ticket", ""))
                or (
                    not DIGEST.fullmatch(value.get("lease", ""))
                    and not (
                        value.get("lease") == ""
                        and value.get("parked") is True
                    )
                )
                or not Path(value.get("worktree", "")).is_absolute()
                or (
                    value.get("parked") not in {None, True}
                    or (
                        value.get("parked") is True
                        and (
                            Path(value["worktree"]).name != value["ticket"]
                            or Path(value["worktree"]).parent.name != "parked"
                        )
                    )
                )
                or value.get("status") not in {
                    "claimed", "running", "waiting", "blocked", "budget",
                }
            ):
                raise ControllerError("controller claim is malformed")
            if value["status"] == "budget":
                if not DIGEST.fullmatch(value.get("budget_sha256", "")):
                    raise ControllerError("budget wait claim is malformed")
                if value["budget_sha256"] != self.envelope_digest():
                    lease = self.json_call("claim", "--ticket", value["ticket"])
                    if (
                        lease.get("schema_version") != 1
                        or lease.get("ticket") != value["ticket"]
                        or not DIGEST.fullmatch(lease.get("lease_id", ""))
                    ):
                        raise ControllerError("reopened budget lease is invalid")
                    value.pop("budget_sha256")
                    value.update(
                        lease=lease["lease_id"], receipt="", role="",
                        status="claimed",
                    )
                    self.save_claim(value)
                    self.event("budget_reopened", value["ticket"])
            worktree = Path(value["worktree"])
            if not worktree.exists():
                matches = []
                current: dict[str, str] = {}
                output = subprocess.run(
                    ["git", "-C", str(self.product), "worktree", "list", "--porcelain"],
                    text=True, capture_output=True, check=True, timeout=120,
                ).stdout
                for line in output.splitlines() + [""]:
                    if line:
                        name, _, item = line.partition(" ")
                        current[name] = item
                        continue
                    if current.get("branch") == f"refs/heads/{value['branch']}":
                        matches.append(current.get("worktree", ""))
                    current = {}
                if len(matches) != 1 or not Path(matches[0]).is_absolute():
                    raise ControllerError("controller claim worktree is unavailable")
                value["worktree"] = matches[0]
                self.save_claim(value)
                self.event("worktree_recovered", value["ticket"], worktree=matches[0])
            result.append(value)
        return result

    def claim_new(
        self, existing: list[dict[str, Any]], reserved_capacity: int = 0
    ) -> list[dict[str, Any]]:
        claims = list(existing)
        if not 0 <= reserved_capacity <= self.capacity:
            raise ControllerError("reserved controller capacity is invalid")
        excluded = sorted(
            item["ticket"]
            for item in claims if not self.consumes_capacity(item)
        )
        while len(
            [item for item in claims if self.consumes_capacity(item)]
        ) + reserved_capacity < self.capacity:
            arguments = ["dispatch-plan", "--claim"]
            for ticket in excluded:
                arguments.extend(["--exclude-ticket", ticket])
            value = self.json_call(*arguments, "--json")
            if value.get("action") == "WAIT":
                break
            if (
                value.get("action") != "START"
                or not TICKET.fullmatch(value.get("ticket", ""))
                or not DIGEST.fullmatch(value.get("lease_id", ""))
            ):
                raise ControllerError("dispatch claim is malformed")
            claim = {
                "branch": value["branch"],
                "lease": value["lease_id"],
                "priority": value.get("priority", "none"),
                "publication_lease": "",
                "receipt": "",
                "role": "",
                "schema": CLAIM_SCHEMA,
                "status": "claimed",
                "ticket": value["ticket"],
                "worktree": value["worktree"],
            }
            self.save_claim(claim)
            self.event(
                "ticket_claimed", claim["ticket"], branch=claim["branch"],
                preprovider_reset_head=value.get("preprovider_reset_head"),
                worktree=claim["worktree"],
            )
            claims.append(claim)
        return claims

    def recover_missing_passport_claims(
        self, claims: list[dict[str, Any]]
    ) -> None:
        if not self.qualification:
            return
        passports = self.state / "passports"
        if not passports.is_dir():
            return
        claimed = {item["ticket"] for item in claims}
        records: dict[str, list[str]] = {}
        current: dict[str, str] = {}
        output = subprocess.run(
            ["git", "-C", str(self.product), "worktree", "list", "--porcelain"],
            text=True, capture_output=True, check=True, timeout=120,
        ).stdout
        for line in output.splitlines() + [""]:
            if line:
                name, _, item = line.partition(" ")
                current[name] = item
                continue
            branch = current.get("branch", "")
            if branch and current.get("worktree"):
                records.setdefault(branch, []).append(current["worktree"])
            current = {}
        for ticket in self.qualification["tickets"]:
            path = passports / f"{ticket}.json"
            if ticket in claimed or not path.exists() or self.active_run(ticket):
                continue
            passport = read(path)
            branch = f"ticket/{ticket}"
            worktrees = records.get(f"refs/heads/{branch}", [])
            if (
                passport.get("ticket") != ticket
                or passport.get("branch") != branch
                or passport.get("current_state") not in {
                    "Ready", "Planning", "Building", "Review",
                    "Awaiting Approval", "Approved",
                }
                or len(worktrees) != 1
                or not Path(worktrees[0]).is_absolute()
            ):
                continue
            claim = {
                "branch": branch,
                "lease": "",
                "priority": "normal",
                "publication_lease": "",
                "receipt": "",
                "role": "",
                "schema": CLAIM_SCHEMA,
                "status": "claimed",
                "ticket": ticket,
                "worktree": worktrees[0],
            }
            if (
                Path(worktrees[0]).name == ticket
                and Path(worktrees[0]).parent.name == "parked"
            ):
                claim["parked"] = True
            if not self.ticket_release_current(claim):
                continue
            try:
                lease = self.json_call("claim", "--ticket", ticket)
                if (
                    lease.get("schema_version") != 1
                    or lease.get("ticket") != ticket
                    or not DIGEST.fullmatch(lease.get("lease_id", ""))
                ):
                    raise ControllerError("recovered ticket lease is invalid")
                claim["lease"] = lease["lease_id"]
                self.migrate_passport(claim, "preserve")
            except ControllerError as error:
                if claim["lease"]:
                    self.json_call(
                        "release", "--ticket", ticket,
                        "--lease", claim["lease"],
                    )
                self.event(
                    "missing_claim_blocked", ticket, error=str(error),
                )
                continue
            self.save_claim(claim)
            claims.append(claim)
            claimed.add(ticket)
            self.event("missing_claim_recovered", ticket)

    def renew(self, claim: dict[str, Any]) -> None:
        self.json_call(
            "renew", "--ticket", claim["ticket"], "--lease", claim["lease"],
        )

    @staticmethod
    def route_path(claim: dict[str, Any]) -> Path:
        return Path(claim["worktree"]) / f"factory/route-plans/{claim['ticket']}.json"

    def pin_routes(self, claims: list[dict[str, Any]]) -> list[dict[str, str]]:
        missing = [claim for claim in claims if not self.route_path(claim).exists()]
        if not missing:
            return []
        arguments = ["models", "pin-batch"]
        for claim in missing:
            arguments.extend([
                "--ticket", claim["ticket"], "--workdir", claim["worktree"],
            ])
        pin = self.json_call(*arguments, "--json", allow=(0, 2), timeout=None)
        if pin.get("status") == "error":
            error = pin.get("error", "")
            if error != (
                "model pin resolution failed: profile_temporarily_unavailable"
            ):
                raise ControllerError(
                    error or "batch model pin returned an invalid error"
                )
            results = []
            for index, claim in enumerate(missing):
                claim["status"] = "waiting"
                self.save_claim(claim)
                self.event(
                    "model_pin_wait", claim["ticket"], shared=index != 0,
                )
                results.append({"status": "waiting", "ticket": claim["ticket"]})
            return results
        pins = pin.get("pins")
        if (
            pin.get("schema") != "model-pin-batch/v1"
            or pin.get("status") != "ok"
            or not isinstance(pins, list)
            or len(pins) != len(missing)
            or any(not self.route_path(claim).exists() for claim in missing)
        ):
            raise ControllerError("batch model pin returned malformed evidence")
        self.event("model_pin_batch", tickets=[claim["ticket"] for claim in missing])
        return []

    def release(self, claim: dict[str, Any]) -> None:
        self.withdraw_publication(claim)
        self.json_call(
            "release", "--ticket", claim["ticket"], "--lease", claim["lease"],
        )
        self.claim_path(claim["ticket"]).unlink(missing_ok=True)
        self.event("ticket_released", claim["ticket"])

    def release_publication(self, claim: dict[str, Any]) -> None:
        self.json_call(
            "publication", "release", "--ticket", claim["ticket"],
            "--lease", claim["publication_lease"], "--json",
        )
        claim["publication_lease"] = ""
        self.save_claim(claim)
        self.event("publication_released", claim["ticket"])

    def withdraw_publication(self, claim: dict[str, Any]) -> None:
        if claim.get("publication_lease"):
            self.release_publication(claim)
            return
        value = self.json_call(
            "publication", "withdraw", "--ticket", claim["ticket"], "--json",
        )
        if value.get("status") not in {"absent", "withdrawn"}:
            raise ControllerError("publication withdrawal returned invalid evidence")
        if value["status"] == "withdrawn":
            self.event("publication_withdrawn", claim["ticket"])

    def block(self, claim: dict[str, Any], reason: str) -> None:
        self.withdraw_publication(claim)
        self.release_ticket_lease(claim)
        claim["status"] = "blocked"
        self.save_claim(claim)
        self.event("ticket_blocked", claim["ticket"], reason=reason)

    def active_run(self, ticket: str) -> bool:
        active = self.product / "factory/.active-runs"
        return active.is_dir() and any(active.glob(f"{ticket}.*"))

    def role_active(self, claim: dict[str, Any]) -> bool:
        return self.active_run(claim["ticket"])

    def release_ticket_lease(self, claim: dict[str, Any]) -> None:
        if claim.get("lease_released") is True:
            return
        self.json_call(
            "release", "--ticket", claim["ticket"], "--lease", claim["lease"],
        )
        claim["lease_released"] = True
        self.save_claim(claim)

    def ensure_lease(self, claim: dict[str, Any], label: str) -> None:
        if (
            claim.get("lease_released") is not True
            and DIGEST.fullmatch(claim.get("lease", ""))
        ):
            try:
                self.renew(claim)
                return
            except ControllerError:
                if self.role_active(claim):
                    raise
        lease = self.json_call("claim", "--ticket", claim["ticket"])
        if (
            lease.get("schema_version") != 1
            or lease.get("ticket") != claim["ticket"]
            or not DIGEST.fullmatch(lease.get("lease_id", ""))
        ):
            raise ControllerError(f"{label} lease is invalid")
        claim["lease"] = lease["lease_id"]
        claim.pop("lease_released", None)
        self.save_claim(claim)
        self.event(
            "ticket_lease_recovered", claim["ticket"], recovery=label,
        )

    def park_claim(self, claim: dict[str, Any]) -> bool:
        """Release a clean checkpointed ticket from a disposable cell."""
        if self.role_active(claim):
            return False
        if self.parked(claim):
            if claim["status"] in {"claimed", "running"}:
                claim["status"] = "waiting"
            if DIGEST.fullmatch(claim.get("lease", "")):
                self.release_ticket_lease(claim)
                claim["lease"] = ""
                claim.pop("lease_released", None)
                self.save_claim(claim)
                self.event("parked_lease_released", claim["ticket"])
            else:
                self.save_claim(claim)
            return True
        source = Path(claim["worktree"])
        if (
            not re.fullmatch(r"cell-[1-6]", source.name)
            or not source.is_dir()
            or self.role_active(claim)
        ):
            return False
        try:
            clean = subprocess.run(
                ["git", "-C", str(source), "status", "--porcelain=v1", "-z"],
                text=True, capture_output=True, check=True, timeout=120,
            ).stdout == ""
            branch = subprocess.run(
                ["git", "-C", str(source), "symbolic-ref", "--short", "HEAD"],
                text=True, capture_output=True, check=True, timeout=120,
            ).stdout.strip()
            if (
                not clean
                or branch != claim["branch"]
                or not self.remote_passport_valid(claim)
            ):
                self.event(
                    "cell_parking_deferred", claim["ticket"],
                    reason="checkpoint_not_portable",
                )
                return False
            root = safe_directory(source.parent)
            parked_root = root / "parked"
            if not parked_root.exists():
                parked_root.mkdir(mode=0o700)
            safe_directory(parked_root)
            destination = parked_root / claim["ticket"]
            if destination.exists() or destination.is_symlink():
                raise ControllerError("parked ticket destination is occupied")
            with self.git_lock:
                subprocess.run(
                    [
                        "git", "-C", str(self.product), "worktree", "move",
                        str(source), str(destination),
                    ],
                    check=True, timeout=120,
                )
            claim.update(parked=True, worktree=str(destination))
            if claim["status"] in {"claimed", "running"}:
                claim["status"] = "waiting"
            self.save_claim(claim)
            if DIGEST.fullmatch(claim.get("lease", "")):
                self.release_ticket_lease(claim)
                claim["lease"] = ""
                claim.pop("lease_released", None)
                self.save_claim(claim)
            self.event(
                "cell_parked", claim["ticket"], from_worktree=str(source),
                parked_worktree=str(destination),
            )
            return True
        except (
            ControllerError,
            OSError,
            subprocess.SubprocessError,
        ) as error:
            self.event(
                "cell_parking_deferred", claim["ticket"], reason=str(error),
            )
            return False

    def ensure_execution_cell(self, claim: dict[str, Any]) -> None:
        """Move a portable parked ticket into a free execution cell."""
        if not self.parked(claim):
            return
        source = Path(claim["worktree"])
        root = safe_directory(source.parent.parent)
        if (
            source.parent != root / "parked"
            or source.name != claim["ticket"]
            or not source.is_dir()
            or not self.remote_passport_valid(claim)
        ):
            raise ControllerError("parked ticket checkpoint is not portable")
        with self.git_lock:
            output = subprocess.run(
                [
                    "git", "-C", str(self.product), "worktree", "list",
                    "--porcelain",
                ],
                text=True, capture_output=True, check=True, timeout=120,
            ).stdout
            occupied = {
                Path(line.partition(" ")[2]).resolve()
                for line in output.splitlines()
                if line.startswith("worktree ")
            }
            destination = next(
                (
                    root / f"cell-{number}"
                    for number in range(1, 7)
                    if root / f"cell-{number}" not in occupied
                    and not (root / f"cell-{number}").exists()
                    and not (root / f"cell-{number}").is_symlink()
                ),
                None,
            )
            if destination is None:
                raise ControllerError("no disposable execution cell is available")
            subprocess.run(
                [
                    "git", "-C", str(self.product), "worktree", "move",
                    str(source), str(destination),
                ],
                check=True, timeout=120,
            )
        claim.pop("parked", None)
        claim["worktree"] = str(destination)
        self.save_claim(claim)
        try:
            if not self.remote_passport_valid(claim):
                raise ControllerError(
                    "reattached ticket passport validation failed"
                )
        except (
            ControllerError,
            json.JSONDecodeError,
            OSError,
            subprocess.SubprocessError,
        ):
            with self.git_lock:
                subprocess.run(
                    [
                        "git", "-C", str(self.product), "worktree", "move",
                        str(destination), str(source),
                    ],
                    check=True, timeout=120,
                )
            claim.update(parked=True, worktree=str(source))
            self.save_claim(claim)
            raise
        self.event(
            "cell_reattached", claim["ticket"], from_worktree=str(source),
            to_worktree=str(destination),
        )

    def terminal_for_receipt(self, ticket: str, receipt: str) -> dict[str, str] | None:
        matches = []
        for path in (self.product / "factory/runs").glob("*.meta"):
            value = fields(path)
            if (
                value.get("ticket") == ticket
                and value.get("transition_receipt_sha256") == receipt
                and value.get("accounting_state") in TERMINAL_ACCOUNTING
            ):
                matches.append(value)
        if len(matches) > 1:
            launch_voids = all(self.typed_launch_void(item) for item in matches)
            identity = {
                (
                    item.get("role"), item.get("role_head_before"),
                    item.get("kit_sha"), item.get("terminal_reason_code", ""),
                )
                for item in matches
            }
            if not launch_voids or len(identity) != 1:
                raise ControllerError("receipt has ambiguous terminal run evidence")
            selected = dict(max(
                matches,
                key=lambda item: (
                    item.get("terminal_at", ""), item.get("run_id", ""),
                ),
            ))
            selected["duplicate_launch_void_count"] = str(len(matches))
            return selected
        return matches[0] if matches else None

    def attempt_manifest(self, claim: dict[str, Any]) -> dict[str, str] | None:
        matches = []
        for path in (self.product / "factory/runs").glob("*.meta"):
            value = fields(path)
            if (
                value.get("ticket") == claim["ticket"]
                and value.get("transition_receipt_sha256")
                == claim.get("receipt")
                and value.get("accounting_state") not in TERMINAL_ACCOUNTING
            ):
                matches.append(value)
        if len(matches) > 1:
            raise ControllerError("receipt has ambiguous active run evidence")
        return matches[0] if matches else self.terminal_for_receipt(
            claim["ticket"], claim["receipt"]
        )

    def observe_attempt(self, claim: dict[str, Any]) -> None:
        attempt = self.attempt_manifest(claim)
        if attempt is None:
            return
        run_id = attempt.get("run_id", "")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", run_id):
            raise ControllerError("attempt run identity is invalid")
        bound = {
            "adapter": attempt.get("adapter"),
            "go_issued": attempt.get("go_issued"),
            "head_sha": attempt.get("role_head_before"),
            "kit_sha": attempt.get("kit_sha"),
            "provider_attempt_id": attempt.get("provider_attempt_id"),
            "role": claim.get("role"),
            "route_id": attempt.get("route_id"),
            "run_id": run_id,
            "task_submitted": attempt.get("task_submitted"),
            "transition_receipt_sha256": claim.get("receipt"),
        }
        marker = "attempt-bound-" + hashlib.sha256(run_id.encode()).hexdigest()
        if self.marker(marker, {"schema": EVENT_SCHEMA, **bound}):
            self.event("attempt_bound", claim["ticket"], **bound)
        journal = self.product / "factory/runs" / f"{run_id}.progress.jsonl"
        if not journal.exists():
            return
        try:
            summary = progress_summary(journal)
        except (ControllerError, OSError) as error:
            invalid = "attempt-progress-invalid-" + hashlib.sha256(
                run_id.encode()
            ).hexdigest()
            if self.marker(invalid, {
                "run_id": run_id, "schema": EVENT_SCHEMA,
            }):
                self.event(
                    "attempt_progress_invalid", claim["ticket"],
                    error=str(error), run_id=run_id,
                )
            return
        sequence = summary["progress_events"]
        if not sequence:
            return
        progress = "attempt-progress-" + hashlib.sha256(
            f"{run_id}:{sequence}".encode()
        ).hexdigest()
        if self.marker(progress, {
            "progress_events": sequence, "run_id": run_id,
            "schema": EVENT_SCHEMA,
        }):
            self.event(
                "attempt_progress", claim["ticket"],
                head_sha=attempt.get("role_head_before"),
                provider_attempt_id=attempt.get("provider_attempt_id"),
                role=claim.get("role"), route_id=attempt.get("route_id"),
                run_id=run_id, transition_receipt_sha256=claim.get("receipt"),
                **summary,
            )

    def observe_attempt_safely(self, claim: dict[str, Any]) -> None:
        try:
            self.observe_attempt(claim)
        except (ControllerError, OSError) as error:
            receipt = claim.get("receipt", "")
            marker = "attempt-observation-invalid-" + hashlib.sha256(
                f"{claim['ticket']}:{receipt}".encode()
            ).hexdigest()
            if self.marker(marker, {
                "schema": EVENT_SCHEMA,
                "ticket": claim["ticket"],
                "transition_receipt_sha256": receipt,
            }):
                self.event(
                    "attempt_observation_invalid", claim["ticket"],
                    error=str(error), role=claim.get("role"),
                    transition_receipt_sha256=receipt,
                )

    def emit_attempt_terminal(
        self, claim: dict[str, Any], terminal: dict[str, str]
    ) -> None:
        run_id = terminal.get("run_id", "")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", run_id):
            raise ControllerError("terminal run identity is invalid")
        duplicate_count = terminal.get("duplicate_launch_void_count", "1")
        if not duplicate_count.isdigit() or not 1 <= int(duplicate_count) <= 10_000:
            raise ControllerError("terminal duplicate count is invalid")
        marker = "attempt-terminal-" + hashlib.sha256(run_id.encode()).hexdigest()
        details = {
            "accounting_state": terminal.get("accounting_state"),
            "duplicate_launch_void_count": int(duplicate_count),
            "exit_status": terminal.get("exit_status"),
            "go_issued": terminal.get("go_issued"),
            "head_sha": terminal.get("role_head_before"),
            "provider_attempt_id": terminal.get("provider_attempt_id"),
            "progress_events": terminal.get("progress_events"),
            "role": claim.get("role"),
            "role_exit": terminal.get("role_exit"),
            "route_id": terminal.get("route_id"),
            "run_id": run_id,
            "task_submitted": terminal.get("task_submitted"),
            "terminal_reason_code": terminal.get("terminal_reason_code", ""),
            "transition_receipt_sha256": claim.get("receipt"),
        }
        if self.marker(marker, {"schema": EVENT_SCHEMA, **details}):
            self.event("attempt_terminal", claim["ticket"], **details)

    def terminal_already_exported(
        self, claim: dict[str, Any], terminal: dict[str, str]
    ) -> bool:
        path = self.state / "passports" / f"{claim['ticket']}.json"
        if not path.exists():
            return False
        value = read(path)
        expected = (
            terminal.get("run_id"),
            claim.get("role"),
            claim.get("receipt"),
        )
        charges = [
            (
                item.get("run_id"),
                item.get("role"),
                item.get("transition_receipt_sha256"),
            )
            for item in value.get("charge_records", [])
        ]
        completed = [
            (
                item.get("run_id"),
                item.get("role"),
                item.get("transition_receipt_sha256"),
            )
            for item in value.get("completed_role_evidence", [])
        ]
        successful = (
            terminal.get("exit_status") == "0"
            and terminal.get("role_exit") == "ok"
        )
        return (
            value.get("transition_receipt_sha256") == claim.get("receipt")
            and charges.count(expected) == 1
            and (not successful or completed.count(expected) == 1)
        )

    def passport(self, claim: dict[str, Any], publication: str) -> None:
        self.json_call(
            "passport", "export", "--ticket", claim["ticket"],
            "--receipt", claim["receipt"], "--publication-state", publication,
            "--workdir", claim["worktree"], "--json",
        )

    def migrate_passport(self, claim: dict[str, Any], publication: str) -> None:
        path = self.state / "passports" / f"{claim['ticket']}.json"
        if not path.exists():
            return
        self.json_call(
            "passport", "migrate", "--ticket", claim["ticket"],
            "--publication-state", publication,
            "--workdir", claim["worktree"], "--json",
        )

    def ticket_release_current(self, claim: dict[str, Any]) -> bool:
        try:
            route = json.loads(self.route_path(claim).read_text(encoding="utf-8"))
            ticket = (
                Path(claim["worktree"])
                / "factory" / "tickets" / f"{claim['ticket']}.md"
            ).read_text(encoding="utf-8")
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return False
        leases = re.findall(r"^Kit-SHA:\s*(.*?)\s*$", ticket, re.M)
        return (
            isinstance(route, dict)
            and route.get("ticket") == claim["ticket"]
            and route.get("kit_sha") == self.release_path.name
            and leases == [self.release_path.name]
        )

    def remote_passport_valid(self, claim: dict[str, Any]) -> bool:
        validation = self.json_call(
            "passport", "validate", "--ticket", claim["ticket"],
            "--workdir", claim["worktree"], "--json",
        )
        passport = read(
            self.state / "passports" / f"{claim['ticket']}.json"
        )
        head = passport.get("head_sha", "")
        branch = passport.get("branch", "")
        if (
            validation.get("status") != "ok"
            or validation.get("passport") != passport.get("passport_sha256")
            or not SHA.fullmatch(head)
            or branch != claim["branch"]
        ):
            return False
        remote = subprocess.run(
            [
                "git", "-C", claim["worktree"], "ls-remote", "--exit-code",
                "origin", f"refs/heads/{branch}",
            ],
            text=True, capture_output=True, check=False, timeout=120,
        )
        return remote.returncode == 0 and remote.stdout == (
            f"{head}\trefs/heads/{branch}\n"
        )

    def recover_terminal_exports(self, claims: list[dict[str, Any]]) -> None:
        for claim in claims:
            if (
                claim["status"] not in {"blocked", "running"}
                or not claim.get("receipt")
                or not claim.get("role")
                or self.role_active(claim)
            ):
                continue
            terminal = self.terminal_for_receipt(
                claim["ticket"], claim["receipt"]
            )
            if (
                terminal is None
                or terminal.get("exit_status") != "0"
                or terminal.get("role_exit") != "ok"
            ):
                continue
            self.ensure_lease(claim, "terminal-export")
            publication = (
                "validating"
                if claim["role"] in {"reviewer", "narrator"}
                else "none"
            )
            if self.terminal_already_exported(claim, terminal):
                self.migrate_passport(claim, publication)
            else:
                self.passport(claim, publication)
            claim["status"] = "running"
            self.save_claim(claim)
            self.event(
                "terminal_export_retried", claim["ticket"],
                run_id=terminal.get("run_id"),
            )

    def recover_each(
        self,
        claims: list[dict[str, Any]],
        recovery: Any,
        name: str,
    ) -> None:
        for claim in claims:
            try:
                recovery([claim])
            except (
                ControllerError,
                json.JSONDecodeError,
                OSError,
                subprocess.SubprocessError,
            ) as error:
                claim["status"] = "blocked"
                self.save_claim(claim)
                self.event(
                    "ticket_recovery_failed",
                    claim["ticket"],
                    error=str(error),
                    recovery=name,
                )

    def recover_upgraded_claims(self, claims: list[dict[str, Any]]) -> None:
        for claim in claims:
            successor_budget = (
                claim["status"] == "budget"
                and self.qualification is not None
                and self.qualification.get("mode") == "successor"
            )
            if (
                claim["status"] not in {"blocked", "claimed", "waiting"}
                and not successor_budget
            ):
                continue
            path = self.state / "passports" / f"{claim['ticket']}.json"
            if not path.exists():
                continue
            prior = read(path).get("factory_sha", "")
            if not SHA.fullmatch(prior):
                raise ControllerError("blocked ticket passport has an invalid release")
            pending = (
                f"passport-route-migration-pending-{claim['ticket']}-"
                f"{self.release_path.name}"
            )
            completed = (
                f"passport-route-migration-complete-{claim['ticket']}-"
                f"{self.release_path.name}"
            )
            if (
                prior == self.release_path.name
                and (
                    not self.marker(pending)
                    or self.marker(completed)
                )
            ):
                continue
            if not self.ticket_release_current(claim):
                if prior != self.release_path.name:
                    created = self.marker(pending, {
                        "factory_sha": self.release_path.name,
                        "schema": EVENT_SCHEMA,
                        "ticket": claim["ticket"],
                    })
                    self.migrate_passport(claim, "preserve")
                    if created:
                        self.event(
                            "passport_migrated_awaiting_route", claim["ticket"],
                            from_factory_sha=prior,
                        )
                claim["status"] = "blocked"
                self.save_claim(claim)
                marker = (
                    f"route-migration-required-{claim['ticket']}-"
                    f"{self.release_path.name}"
                )
                if self.marker(marker, {
                    "factory_sha": self.release_path.name,
                    "schema": EVENT_SCHEMA,
                    "ticket": claim["ticket"],
                }):
                    self.event(
                        "route_migration_required", claim["ticket"],
                        from_factory_sha=prior,
                    )
                continue
            try:
                self.renew(claim)
            except ControllerError:
                lease = self.json_call("claim", "--ticket", claim["ticket"])
                if (
                    lease.get("schema_version") != 1
                    or lease.get("ticket") != claim["ticket"]
                    or not DIGEST.fullmatch(lease.get("lease_id", ""))
                ):
                    raise ControllerError("upgraded ticket lease is invalid")
                claim["lease"] = lease["lease_id"]
            claim.pop("lease_released", None)
            self.save_claim(claim)
            try:
                self.migrate_passport(claim, "preserve")
            except ControllerError:
                self.release_ticket_lease(claim)
                raise
            if (
                not claim.get("receipt")
                and self.restore_contract_blocker(claim)
            ):
                self.event(
                    "upgraded_claim_recovered", claim["ticket"],
                    from_factory_sha=prior,
                )
                self.marker(completed, {
                    "factory_sha": self.release_path.name,
                    "schema": EVENT_SCHEMA,
                    "ticket": claim["ticket"],
                })
                continue
            terminal = (
                self.terminal_for_receipt(claim["ticket"], claim["receipt"])
                if claim.get("receipt")
                else None
            )
            if terminal is not None:
                prior_release_launch_void = (
                    self.typed_launch_void(terminal)
                    and SHA.fullmatch(terminal.get("kit_sha", ""))
                    and terminal["kit_sha"] != self.release_path.name
                )
                claim["status"] = (
                    "running"
                    if (
                        prior_release_launch_void
                        or (
                            self.qualification
                            and terminal.get("role_exit") == "provider_failed"
                            and terminal.get("route_id", "").startswith("cursor-")
                        )
                    )
                    else "blocked"
                )
            else:
                claim.update(receipt="", role="", status="claimed")
                claim.pop("budget_sha256", None)
            self.save_claim(claim)
            self.event(
                "upgraded_claim_recovered", claim["ticket"],
                from_factory_sha=prior,
            )
            self.marker(completed, {
                "factory_sha": self.release_path.name,
                "schema": EVENT_SCHEMA,
                "ticket": claim["ticket"],
            })

    def restore_contract_blocker(self, claim: dict[str, Any]) -> bool:
        if (
            claim["status"] != "blocked"
            or claim.get("receipt")
            or claim.get("role")
            or self.role_active(claim)
        ):
            return False
        receipt_path = self.state / f"{claim['ticket']}.json"
        passport_path = self.state / "passports" / f"{claim['ticket']}.json"
        if not receipt_path.exists() or not passport_path.exists():
            return False
        receipt = read(receipt_path)
        passport = read(passport_path)
        receipt_digest = receipt.get("receipt_sha256", "")
        role = receipt.get("role", "")
        charges = passport.get("charge_records")
        completed = passport.get("completed_role_evidence")
        if (
            receipt.get("schema") != "nysa.software-factory.transition-receipt/v1"
            or receipt.get("ticket") != claim["ticket"]
            or receipt.get("branch") != claim["branch"]
            or not receipt.get("consumed")
            or not DIGEST.fullmatch(receipt_digest)
            or role not in {"planner", "test-author", "builder"}
            or passport.get("ticket") != claim["ticket"]
            or passport.get("branch") != claim["branch"]
            or passport.get("factory_sha") != self.release_path.name
            or not isinstance(charges, list)
            or not charges
            or not isinstance(completed, list)
        ):
            return False
        charge = charges[-1]
        if (
            not isinstance(charge, dict)
            or charge.get("transition_receipt_sha256") != receipt_digest
            or charge.get("factory_sha") != receipt.get("factory_sha")
            or charge.get("contract_version") != receipt.get("contract_version")
            or charge.get("role") != role
            or charge.get("head_before") != receipt.get("head_sha")
            or any(
                isinstance(item, dict)
                and item.get("transition_receipt_sha256") == receipt_digest
                for item in completed
            )
        ):
            return False
        terminal = self.terminal_for_receipt(claim["ticket"], receipt_digest)
        if (
            terminal is None
            or terminal.get("run_id") != charge.get("run_id")
            or terminal.get("role_exit") != "role_exit_contract_blocked"
            or terminal.get("exit_status") != "12"
            or terminal.get("role") != role
            or terminal.get("kit_sha") != receipt.get("factory_sha")
        ):
            return False
        if not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", terminal["run_id"]
        ):
            return False
        manifest = self.product / "factory/runs" / f"{terminal['run_id']}.meta"
        if (
            not manifest.is_file()
            or manifest.is_symlink()
            or hashlib.sha256(manifest.read_bytes()).hexdigest()
            != charge.get("manifest_sha256")
            or not self.remote_passport_valid(claim)
        ):
            return False
        self.ensure_lease(claim, "restored-contract-blocker")
        claim.update(receipt=receipt_digest, role=role, status="blocked")
        self.save_claim(claim)
        self.event(
            "contract_blocker_claim_restored", claim["ticket"],
            failed_run_id=terminal["run_id"],
        )
        return True

    def restore_recorded_contract_repair(self, claim: dict[str, Any]) -> bool:
        if (
            claim["status"] != "blocked"
            or self.role_active(claim)
        ):
            return False
        repair = self.state / "contract-repairs" / f"{claim['ticket']}.json"
        if not repair.is_file() or repair.is_symlink():
            return False
        recorded = read(repair)
        if (
            (claim.get("receipt") or claim.get("role"))
            and (
                claim.get("receipt") != recorded.get("blocked_receipt")
                or claim.get("role") != recorded.get("blocked_role")
            )
        ):
            return False
        try:
            if not self.remote_passport_valid(claim):
                return False
        except ControllerError:
            return False
        self.ensure_lease(claim, "recorded-contract-repair")
        claim.update(receipt="", role="", status="claimed")
        self.save_claim(claim)
        self.event("recorded_contract_repair_prepared", claim["ticket"])
        return True

    def recover_repaired_failures(self, claims: list[dict[str, Any]]) -> None:
        for claim in claims:
            if self.restore_recorded_contract_repair(claim):
                continue
            self.restore_contract_blocker(claim)
            if (
                claim["status"] != "blocked"
                or not claim.get("receipt")
                or self.role_active(claim)
            ):
                continue
            terminal = self.terminal_for_receipt(claim["ticket"], claim["receipt"])
            push_failure = (
                terminal is not None
                and terminal.get("role_exit") == "role_exit_push_failed"
            )
            history_rewrite = (
                terminal is not None
                and terminal.get("phase") == "completed"
                and terminal.get("accounting_state") == "abandoned_conservative"
                and terminal.get("go_issued") == "1"
                and terminal.get("task_submitted") == "1"
                and terminal.get("exit_status") == "11"
                and terminal.get("role_exit") == "role_exit_history_rewritten"
                and terminal.get("role") == claim.get("role")
                and claim.get("role") in {
                    "planner", "spec-linter", "builder", "narrator",
                }
                and terminal.get("cost_basis") == "conservative_reservation"
                and re.fullmatch(
                    r"(?:0|[1-9][0-9]{0,6})(?:\.[0-9]{1,18})?",
                    terminal.get("reserved_usd", ""),
                )
                and int(terminal["reserved_usd"].replace(".", "")) > 0
                and terminal.get("effective_cost")
                == terminal.get("reserved_usd")
                and SHA.fullmatch(terminal.get("role_head_before", ""))
                and SHA.fullmatch(terminal.get("kit_sha", ""))
                and terminal["kit_sha"] != self.release_path.name
            )
            interrupted_before_submission = (
                terminal is not None
                and terminal.get("phase") == "abandoned"
                and terminal.get("accounting_state") == "abandoned_conservative"
                and terminal.get("task_submitted") == "0"
                and terminal.get("exit_status") == "143"
                and not terminal.get("role_exit")
            )
            contract_blocked = (
                terminal is not None
                and terminal.get("role_exit") == "role_exit_contract_blocked"
                and terminal.get("exit_status") == "12"
            )
            submission_unconfirmed = (
                terminal is not None
                and terminal.get("phase") == "completed"
                and terminal.get("accounting_state") == "abandoned_conservative"
                and terminal.get("go_issued") == "1"
                and terminal.get("task_submitted") == "0"
                and terminal.get("exit_status") == "125"
                and terminal.get("role_exit") == "provider_failed"
                and (
                    terminal.get("terminal_reason_code", "")
                    == "adapter_submission_unconfirmed"
                    and DIGEST.fullmatch(terminal.get("output_sha256", ""))
                    or terminal.get("terminal_reason_code", "") == ""
                    and terminal.get("output_sha256")
                    == hashlib.sha256(b"").hexdigest()
                )
                and terminal.get("turns") == "0"
                and not terminal.get("progress_events")
                and terminal.get("cost_basis") == "conservative_reservation"
                and terminal.get("effective_cost")
                == terminal.get("reserved_usd")
                and SHA.fullmatch(terminal.get("kit_sha", ""))
                and terminal["kit_sha"] != self.release_path.name
            )
            if not (
                push_failure or interrupted_before_submission or contract_blocked
                or submission_unconfirmed or history_rewrite
            ):
                continue
            passport_path = self.state / "passports" / f"{claim['ticket']}.json"
            if not passport_path.exists():
                continue
            if push_failure or interrupted_before_submission:
                try:
                    if not self.terminal_already_exported(claim, terminal):
                        publication = read(passport_path).get(
                            "publication_state", ""
                        )
                        if publication not in {
                            "none", "validating", "ready", "merge-pending",
                            "merged", "repair",
                        }:
                            continue
                        try:
                            self.passport(claim, publication)
                        except ControllerError:
                            self.migrate_passport(claim, "preserve")
                            self.passport(claim, publication)
                except (
                    ControllerError, json.JSONDecodeError, OSError,
                ):
                    continue
            if (
                push_failure or interrupted_before_submission
                or submission_unconfirmed or history_rewrite
            ):
                try:
                    if not self.terminal_already_exported(claim, terminal):
                        continue
                except ControllerError:
                    continue
            if history_rewrite:
                try:
                    passport = read(passport_path)
                except (ControllerError, json.JSONDecodeError, OSError):
                    continue
                expected = (
                    terminal.get("run_id"), claim.get("role"), claim.get("receipt"),
                )
                completed = [
                    (
                        item.get("run_id"), item.get("role"),
                        item.get("transition_receipt_sha256"),
                    )
                    for item in passport.get("completed_role_evidence", [])
                ]
                if (
                    passport.get("head_sha") != terminal.get("role_head_before")
                    or completed.count(expected) != 0
                ):
                    continue
            if contract_blocked:
                self.ensure_lease(claim, "contract-block-resume")
                blocked = self.json_call(
                    "state-machine", "block", "--ticket", claim["ticket"],
                    "--lease", claim["lease"], "--receipt", claim["receipt"],
                    "--workdir", claim["worktree"], "--json",
                )
                if blocked.get("status") != "blocked":
                    raise ControllerError(
                        "state machine returned an invalid contract blocker"
                    )
                resumed = self.json_call(
                    "state-machine", "resume", "--ticket", claim["ticket"],
                    "--receipt", claim["receipt"],
                    "--workdir", claim["worktree"], "--json",
                )
                if resumed.get("status") == "waiting":
                    continue
                if resumed.get("status") != "ready":
                    raise ControllerError(
                        "state machine returned an invalid contract resume"
                    )
                try:
                    valid_passport = self.remote_passport_valid(claim)
                except ControllerError:
                    continue
                if not valid_passport:
                    continue
            try:
                valid_passport = self.remote_passport_valid(claim)
            except ControllerError:
                if not push_failure:
                    continue
                try:
                    self.migrate_passport(claim, "preserve")
                    valid_passport = self.remote_passport_valid(claim)
                except ControllerError:
                    continue
            if not valid_passport:
                continue
            self.ensure_lease(claim, "repaired-role")
            failed_run = terminal.get("run_id", "")
            claim.update(receipt="", role="", status="claimed")
            self.save_claim(claim)
            self.event(
                (
                    "push_failure_recovered"
                    if push_failure
                    else (
                        "contract_blocker_recovered"
                        if contract_blocked
                        else (
                            "submission_failure_recovered_by_release_upgrade"
                            if submission_unconfirmed
                            else (
                                "history_rewrite_recovered_by_release_upgrade"
                                if history_rewrite
                                else "interrupted_role_recovered"
                            )
                        )
                    )
                ),
                claim["ticket"],
                failed_run_id=failed_run,
            )

    def relocate_qualification_cell(self, claim: dict[str, Any]) -> None:
        if (
            not self.qualification
            or claim["ticket"] != self.qualification["tickets"][0]
            or self.marker("qualification-relocation")
        ):
            return
        source = Path(claim["worktree"])
        if subprocess.run(
            ["git", "-C", str(source), "status", "--porcelain=v1", "-z"],
            capture_output=True, check=True, timeout=120,
        ).stdout:
            raise ControllerError("qualification relocation requires a clean cell")
        destination = next(
            (
                source.parent / f"cell-{number}"
                for number in (5, 6)
                if not (source.parent / f"cell-{number}").exists()
            ),
            None,
        )
        if destination is None:
            raise ControllerError("qualification relocation has no clean destination")
        subprocess.run(
            ["git", "-C", str(self.product), "worktree", "move", str(source), str(destination)],
            check=True, timeout=120,
        )
        claim["worktree"] = str(destination)
        self.save_claim(claim)
        try:
            self.json_call(
                "passport", "validate", "--ticket", claim["ticket"],
                "--workdir", claim["worktree"], "--json",
            )
        except Exception:
            subprocess.run(
                [
                    "git", "-C", str(self.product), "worktree", "move",
                    str(destination), str(source),
                ],
                check=True, timeout=120,
            )
            claim["worktree"] = str(source)
            self.save_claim(claim)
            raise
        self.marker("qualification-relocation", {
            "from": str(source),
            "schema": EVENT_SCHEMA,
            "ticket": claim["ticket"],
            "to": str(destination),
        })
        self.event(
            "cell_relocated", claim["ticket"], from_worktree=str(source),
            to_worktree=str(destination),
        )

    def remove_cell(self, claim: dict[str, Any]) -> None:
        worktree = Path(claim["worktree"])
        if not worktree.exists():
            return
        subprocess.run(
            ["git", "-C", str(self.product), "worktree", "remove", str(worktree)],
            check=True, timeout=120,
        )
        self.event("cell_removed", claim["ticket"], worktree=str(worktree))

    def finish_pending_run(self, claim: dict[str, Any]) -> bool:
        if not claim.get("receipt"):
            return True
        if self.role_active(claim):
            self.observe_attempt_safely(claim)
            return False
        terminal = self.terminal_for_receipt(claim["ticket"], claim["receipt"])
        if terminal is None:
            claim.update(receipt="", role="", status="claimed")
            self.save_claim(claim)
            return True
        self.emit_attempt_terminal(claim, terminal)
        if terminal.get("accounting_state") == "launch_void":
            if (
                self.typed_launch_void(terminal)
                and SHA.fullmatch(terminal.get("kit_sha", ""))
                and terminal["kit_sha"] != self.release_path.name
            ):
                prior = terminal["kit_sha"]
                run_id = terminal.get("run_id")
                claim.update(receipt="", role="", status="claimed")
                self.save_claim(claim)
                self.event(
                    "pre_go_failure_recovered_by_release_upgrade",
                    claim["ticket"], failed_run_id=run_id,
                    from_factory_sha=prior,
                )
                return True
            reason = terminal.get("terminal_reason_code", "")
            if not re.fullmatch(r"[a-z0-9_]{0,64}", reason):
                reason = "invalid_pre_go_reason"
            claim["status"] = "blocked"
            self.save_claim(claim)
            self.release_ticket_lease(claim)
            self.event(
                "pre_go_failure_blocked", claim["ticket"],
                failed_run_id=terminal.get("run_id"),
                reason=reason or "pre_go_failure",
            )
            return False
        publication = (
            "validating"
            if claim["role"] in {"reviewer", "narrator"}
            else "none"
        )
        qualification_fallback = (
            self.qualification
            and terminal.get("role_exit") == "provider_failed"
            and terminal.get("route_id", "").startswith("cursor-")
        )
        if not qualification_fallback:
            if self.terminal_already_exported(claim, terminal):
                self.migrate_passport(claim, publication)
                self.event(
                    "terminal_export_recovered", claim["ticket"],
                    run_id=terminal.get("run_id"),
                )
            else:
                self.passport(claim, publication)
        if (
            terminal.get("accounting_state") in {"cancelled", "cancelled_conservative"}
            or terminal.get("role_exit") == "cancelled"
        ):
            claim["status"] = "cancelled"
            self.release(claim)
            self.remove_cell(claim)
            self.event("attempt_cancelled", claim["ticket"])
            return False
        if terminal.get("role_exit") == "role_exit_invalid_output":
            self.migrate_passport(claim, publication)
            rejected_role = claim["role"]
            claim.update(receipt="", role="", status="claimed")
            self.save_claim(claim)
            self.event(
                "role_output_rejected", claim["ticket"], role=rejected_role,
                run_id=terminal.get("run_id"),
            )
            return True
        if terminal.get("exit_status") != "0" or terminal.get("role_exit") != "ok":
            if qualification_fallback:
                with self.fallback_lock:
                    result = self.json_call(
                        "models", "fallback-auto", "--ticket", claim["ticket"],
                        "--failed-run", terminal["run_id"],
                        "--workdir", claim["worktree"],
                        "--reason", "provider_unavailable", "--json",
                    )
                if result.get("failed_run_id") != terminal["run_id"]:
                    raise ControllerError("provider fallback did not bind the failed run")
                self.migrate_passport(claim, publication)
                claim.update(receipt="", role="", status="claimed")
                self.save_claim(claim)
                self.event(
                    "provider_fallback", claim["ticket"],
                    failed_run_id=terminal["run_id"],
                    route_id=terminal["route_id"],
                )
                return True
            if terminal.get("role_exit") == "role_exit_contract_blocked":
                blocked = self.json_call(
                    "state-machine", "block", "--ticket", claim["ticket"],
                    "--lease", claim["lease"], "--receipt", claim["receipt"],
                    "--workdir", claim["worktree"], "--json",
                )
                if blocked.get("status") != "blocked":
                    raise ControllerError(
                        "state machine returned an invalid contract blocker"
                    )
            claim["status"] = "blocked"
            self.save_claim(claim)
            self.release_ticket_lease(claim)
            self.event("role_blocked", claim["ticket"], role=claim["role"])
            return False
        self.relocate_qualification_cell(claim)
        if claim["role"] == "reviewer":
            self.json_call(
                "ticket-state", "--ticket", claim["ticket"],
                "--workdir", claim["worktree"],
                "--action", "reviewer-reconcile", "--json",
            )
            self.migrate_passport(claim, "validating")
        claim.update(receipt="", role="", status="claimed")
        self.save_claim(claim)
        return True

    def ticket_pr(self, claim: dict[str, Any], receipt: str) -> dict[str, Any]:
        return self.json_call(
            "ticket-pr", "--ticket", claim["ticket"], "--lease", claim["lease"],
            "--receipt", receipt, "--workdir", claim["worktree"], "--json",
        )

    def refresh_dependency_tracking(self, claim: dict[str, Any]) -> bool:
        path = (
            Path(claim["worktree"])
            / "factory" / "tickets" / f"{claim['ticket']}.md"
        )
        try:
            ticket = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            # State-machine remains the authoritative fail-closed ticket
            # validator; focused controller callers may replace that boundary.
            return True
        values = re.findall(r"^Depends-On:\s*(.*?)\s*$", ticket, re.I | re.M)
        if len(values) > 1:
            raise ControllerError("ticket Depends-On is ambiguous")
        if not values or values[0].casefold() == "none":
            return True
        with self.git_lock:
            observed = subprocess.run(
                [
                    "git", "-C", claim["worktree"], "ls-remote", "--heads",
                    "origin", "refs/heads/main",
                ],
                text=True, capture_output=True, check=True, timeout=120,
            ).stdout.split()
            if (
                len(observed) != 2
                or not SHA.fullmatch(observed[0])
                or observed[1] != "refs/heads/main"
            ):
                raise ControllerError(
                    "protected main dependency observation is ambiguous"
                )
            subprocess.run(
                [
                    "git", "-C", claim["worktree"], "fetch", "--quiet",
                    "--no-tags", "origin",
                    "+refs/heads/main:refs/remotes/origin/main",
                ],
                check=True, timeout=120,
            )
            local = subprocess.run(
                [
                    "git", "-C", claim["worktree"], "rev-parse",
                    "--verify", "origin/main^{commit}",
                ],
                text=True, capture_output=True, check=True, timeout=120,
            ).stdout.strip()
            if local != observed[0]:
                self.event(
                    "dependency_tracking_moved", claim["ticket"],
                    observed=observed[0], fetched=local,
                )
                return False
        return True

    def retry_ci(
        self, claim: dict[str, Any], receipt: str, pr: dict[str, Any]
    ) -> bool:
        value = self.json_call(
            "ci-rerun", "--ticket", claim["ticket"], "--lease", claim["lease"],
            "--receipt", receipt, "--pr", str(pr["pr_number"]),
            "--workdir", claim["worktree"], "--json",
        )
        return value.get("status") == "rerun"

    def publication_repair(
        self, claim: dict[str, Any], receipt: str, pr: dict[str, Any]
    ) -> None:
        value = self.json_call(
            "publication-repair", "--ticket", claim["ticket"],
            "--lease", claim["lease"], "--receipt", receipt,
            "--pr", str(pr["pr_number"]), "--workdir", claim["worktree"],
            "--json",
        )
        if value.get("status") != "repair":
            raise ControllerError("publication repair was not materialized")
        self.withdraw_publication(claim)
        self.migrate_passport(claim, "repair")
        self.event(
            "publication_repair", claim["ticket"], owner=value.get("owner"),
        )

    def protected_base_current(self, claim: dict[str, Any], head: str) -> bool:
        subprocess.run(
            ["git", "-C", claim["worktree"], "fetch", "--quiet", "origin", "main"],
            check=True, timeout=120,
        )
        return subprocess.run(
            [
                "git", "-C", claim["worktree"], "merge-base", "--is-ancestor",
                "origin/main", head,
            ],
            check=False, timeout=120,
        ).returncode == 0

    def publication_ready(
        self, claim: dict[str, Any], receipt: str, head: str
    ) -> bool:
        with self.git_lock:
            if not self.protected_base_current(claim, head):
                self.withdraw_publication(claim)
                value = self.json_call(
                    "ticket-attest", "--ticket", claim["ticket"],
                    "--lease", claim["lease"], "--receipt", receipt,
                    "--workdir", claim["worktree"], "--action", "refresh", "--json",
                )
                if value.get("action") != "refresh":
                    raise ControllerError("protected-base refresh was not materialized")
                self.migrate_passport(claim, "validating")
                self.event(
                    "protected_base_refreshed", claim["ticket"],
                    head_sha=value.get("head"),
                )
                return False
        prior = claim.get("publication_lease", "")
        self.json_call(
            "publication", "ready", "--ticket", claim["ticket"],
            "--head", head, "--priority", claim["priority"], "--json",
        )
        lease = self.json_call(
            "publication", "acquire", "--ticket", claim["ticket"],
            "--head", head, "--priority", claim["priority"], "--json",
        )
        if lease.get("status") != "acquired":
            return False
        claim["publication_lease"] = lease["lease"]
        self.save_claim(claim)
        self.event(
            "publication_renewed" if prior == lease["lease"] else "publication_acquired",
            claim["ticket"], head_sha=head,
        )
        return True

    def ticket_merged(self, claim: dict[str, Any]) -> bool:
        repo_values = re.findall(
            r"^(?:export\s+)?GH_REPO\s*=\s*['\"]?([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)['\"]?\s*$",
            (self.product / "factory/PROJECT.env").read_text(encoding="utf-8"),
            re.M,
        )
        if len(repo_values) != 1:
            raise ControllerError("GH_REPO is missing or ambiguous")
        result = subprocess.run(
            [
                "gh", "pr", "list", "--repo", repo_values[0], "--state", "merged",
                "--head", claim["branch"], "--json", "headRefName,mergedAt,state",
            ],
            text=True, capture_output=True, check=False, timeout=120,
        )
        if result.returncode:
            raise ControllerError(result.stderr.strip() or "GitHub merge query failed")
        values = json.loads(result.stdout)
        return (
            isinstance(values, list)
            and len(values) == 1
            and values[0].get("state") == "MERGED"
            and bool(values[0].get("mergedAt"))
        )

    def closeout(self, claim: dict[str, Any]) -> bool:
        ticket = claim["ticket"]
        branch = f"chore/{ticket.lower().replace('-', '')}-closeout"
        root = Path(claim["worktree"]).parent
        worktree = root / f"closeout-{ticket}"
        with self.git_lock:
            subprocess.run(
                ["git", "-C", str(self.product), "fetch", "--quiet", "origin", "main"],
                check=True, timeout=120,
            )
        if not worktree.exists():
            exists = subprocess.run(
                [
                    "git", "-C", str(self.product), "show-ref", "--verify",
                    "--quiet", f"refs/heads/{branch}",
                ],
                check=False, timeout=120,
            ).returncode == 0
            subprocess.run(
                [
                    "git", "-C", str(self.product), "worktree", "add", "--quiet",
                    *(["-b", branch, str(worktree), "origin/main"] if not exists
                      else [str(worktree), branch]),
                ],
                check=True, timeout=120,
            )
        value = self.json_call(
            "ticket-attest", "--ticket", ticket, "--lease", claim["lease"],
            "--workdir", str(worktree), "--action", "done", "--json",
        )
        return value.get("closeout_pr_state") == "MERGED"

    def run_role(
        self, claim: dict[str, Any], role: str, receipt: str,
        failed_checks: list[str], publication: dict[str, Any] | None = None,
    ) -> None:
        self.ensure_execution_cell(claim)
        if role == "planner":
            preflight = self.json_call(
                "preflight", "--ticket", claim["ticket"], "--role", role,
                "--lease", claim["lease"], "--receipt", receipt,
                "--workdir", claim["worktree"], "--json",
                allow=(0, 1),
            )
            if preflight.get("status") != "ok" or preflight.get("exit_code") != 0:
                self.block(claim, "preflight")
                return
        claim.update(receipt=receipt, role=role, status="running")
        self.save_claim(claim)
        self.event(
            "attempt_started", claim["ticket"], role=role,
            transition_receipt_sha256=receipt,
        )
        task = f"Execute {role} for {claim['ticket']} from its frozen contract and repository state."
        if failed_checks:
            task += " Required GitHub checks failed: " + ", ".join(failed_checks)
        if role == "narrator":
            if publication is None:
                raise ControllerError("Narrator publication evidence is missing")
            preview_urls = publication.get("preview_urls")
            pr_number = publication.get("pr_number")
            pr_url = publication.get("url")
            head = publication.get("head")
            if (
                publication.get("status") != "ready"
                or not isinstance(pr_number, int)
                or pr_number <= 0
                or not isinstance(pr_url, str)
                or not re.fullmatch(r"https://github[.]com/[^\s]+/pull/[1-9][0-9]*", pr_url)
                or not SHA.fullmatch(head or "")
                or publication.get("checks") != []
                or not isinstance(preview_urls, list)
                or not preview_urls
            ):
                raise ControllerError("Narrator publication evidence is invalid")
            for preview_url in preview_urls:
                parsed = urlsplit(preview_url) if isinstance(preview_url, str) else None
                if (
                    parsed is None
                    or parsed.scheme != "https"
                    or not parsed.hostname
                    or not parsed.hostname.endswith(".up.railway.app")
                    or parsed.username is not None
                    or parsed.password is not None
                    or parsed.port is not None
                    or parsed.query
                    or parsed.fragment
                    or parsed.path not in ("", "/")
                ):
                    raise ControllerError("Narrator preview evidence is invalid")
            task += (
                f" Trusted publication evidence: PR #{pr_number} is {pr_url} at exact "
                f"head {head}; every configured required GitHub check passed. Trusted "
                f"preview endpoints: {', '.join(preview_urls)}. Verify the deployed head "
                "and exercise the frozen preview behavior, then capture the required "
                "screenshots. Use the existing Reviewer and protected-CI evidence. Do not "
                "run tests, builds, repo-check, secret-scan, or any broad verification suite."
            )
        command = [
            str(self.launcher), self.project, "run",
            "--role", role, "--ticket", claim["ticket"],
            "--lease", claim["lease"], "--receipt", receipt,
            "--prompt-file", str(self.release_path / f"roles/{role}.md"),
            "--workdir", claim["worktree"], "--", task,
        ]
        log_path = self.logs / f"{claim['ticket']}-{role}.log"
        with log_path.open("a", encoding="utf-8") as log:
            process = subprocess.Popen(command, stdout=log, stderr=log)
            while True:
                try:
                    exit_status = process.wait(
                        timeout=RECONCILE_INTERVAL_SECONDS
                    )
                    break
                except subprocess.TimeoutExpired:
                    self.observe_attempt_safely(claim)
        if self.terminal_for_receipt(claim["ticket"], receipt) is None:
            claim["status"] = "blocked"
            self.save_claim(claim)
            self.release_ticket_lease(claim)
            self.event(
                "role_launch_missing_terminal", claim["ticket"],
                exit_status=exit_status, role=role,
                transition_receipt_sha256=receipt,
            )
            return
        self.finish_pending_run(claim)

    def reconcile_ticket(self, claim: dict[str, Any]) -> dict[str, str]:
        try:
            if (self.product / "factory/MAINTENANCE").exists():
                return {"status": "maintenance", "ticket": claim["ticket"]}
            self.ensure_lease(claim, "reconciliation")
            if not self.finish_pending_run(claim):
                return {
                    "status": (
                        claim["status"]
                        if claim["status"] in {"blocked", "cancelled"}
                        else "active"
                    ),
                    "ticket": claim["ticket"],
                }
            if (
                claim.get("publication_lease")
                and self.ticket_merged(claim)
            ):
                self.release_publication(claim)
                self.migrate_passport(claim, "merged")
                self.closeout(claim)
                return {
                    "status": "progressed",
                    "ticket": claim["ticket"],
                }
            if not self.route_path(claim).exists():
                raise ControllerError("ticket route was not batch pinned")
            if not self.refresh_dependency_tracking(claim):
                claim["status"] = "waiting"
                self.save_claim(claim)
                return {"status": "waiting", "ticket": claim["ticket"]}
            transition = self.json_call(
                "state-machine", "--ticket", claim["ticket"],
                "--lease", claim["lease"], "--workdir", claim["worktree"],
                "--json",
                timeout=None,
            )
            stage = transition.get("stage", "")
            receipt = transition.get("receipt", "")
            role = transition.get("role")
            if (self.product / "factory/MAINTENANCE").exists():
                runnable_stage = (
                    re.fullmatch(
                        r"(?:RUN|FIX) "
                        r"(planner|spec-linter|test-author|builder|"
                        r"reviewer|narrator)",
                        stage,
                    )
                    if isinstance(stage, str)
                    else None
                )
                non_role_stage = (
                    isinstance(stage, str)
                    and (
                        stage.startswith("AWAIT-OPERATOR ")
                        or stage.startswith("AWAIT_DEPENDENCY ")
                        or stage.startswith("AWAIT_BUDGET ")
                        or stage.startswith("COMPLETE ")
                        or stage.startswith("ESCALATE ")
                        or stage.startswith("REFUSE ")
                        or stage.startswith((
                            "AWAIT-MERGE approval attested; "
                            "protected auto-merge request pending",
                            "AWAIT-MERGE protected auto-merge requested",
                            "AWAIT-MERGE closeout auto-merge pending",
                        ))
                    )
                )
                expected_role = (
                    runnable_stage[1] if runnable_stage else None
                )
                if (
                    not isinstance(stage, str)
                    or not stage
                    or not DIGEST.fullmatch(receipt)
                    or transition.get("schema")
                    != "nysa.software-factory.state-machine/v1"
                    or transition.get("status") != "ok"
                    or transition.get("ticket") != claim["ticket"]
                    or transition.get("action") != stage.partition(" ")[0]
                    or transition.get("detail")
                    != (stage.partition(" ")[2] or None)
                    or not (runnable_stage or non_role_stage)
                    or role != expected_role
                ):
                    raise ControllerError(
                        "maintenance boundary has invalid transition evidence"
                    )
                self.event(
                    "stage_resolution_paused",
                    claim["ticket"],
                    transition_receipt_sha256=receipt,
                )
                return {"status": "maintenance", "ticket": claim["ticket"]}
            if not (
                stage.startswith("AWAIT-OPERATOR Linear approval observed")
                or stage.startswith("AWAIT-MERGE protected auto-merge requested")
            ):
                self.withdraw_publication(claim)
            if role:
                failed_checks: list[str] = []
                if role in {"reviewer", "narrator"}:
                    pr = self.ticket_pr(claim, receipt)
                    if pr.get("status") == "wait":
                        return {"status": "waiting", "ticket": claim["ticket"]}
                    if pr.get("status") == "failed" and self.retry_ci(
                        claim, receipt, pr
                    ):
                        return {"status": "waiting", "ticket": claim["ticket"]}
                    if role == "narrator" and pr.get("status") != "ready":
                        self.block(claim, "narrator-pr-gate")
                        return {"status": "blocked", "ticket": claim["ticket"]}
                    if role == "reviewer" and pr.get("status") not in {
                        "prepared", "failed",
                    }:
                        raise ControllerError("Reviewer PR gate returned an invalid status")
                    if pr.get("status") == "failed":
                        failed_checks = list(pr.get("checks", []))
                if role == "narrator":
                    self.run_role(claim, role, receipt, failed_checks, pr)
                else:
                    self.run_role(claim, role, receipt, failed_checks)
                return {
                    "status": (
                        claim["status"]
                        if claim["status"] in {"blocked", "cancelled"}
                        else "progressed"
                    ),
                    "ticket": claim["ticket"],
                }
            if stage.startswith("AWAIT-OPERATOR bundle posted"):
                pr = self.ticket_pr(claim, receipt)
                if pr.get("status") == "failed" and self.retry_ci(
                    claim, receipt, pr
                ):
                    return {"status": "waiting", "ticket": claim["ticket"]}
                if pr.get("status") == "wait":
                    return {"status": "waiting", "ticket": claim["ticket"]}
                if pr.get("status") != "ready":
                    self.block(claim, "bundle-pr-gate")
                    return {"status": "blocked", "ticket": claim["ticket"]}
                attested = self.json_call(
                    "ticket-attest", "--ticket", claim["ticket"],
                    "--lease", claim["lease"], "--receipt", receipt,
                    "--workdir", claim["worktree"], "--action", "bundle", "--json",
                )
                self.migrate_passport(claim, "validating")
                return {"status": "progressed", "ticket": claim["ticket"]}
            if stage.startswith("AWAIT-OPERATOR Linear approval observed"):
                if claim.get("publication_lease"):
                    self.release_publication(claim)
                pr = self.ticket_pr(claim, receipt)
                if pr.get("status") == "failed" and self.retry_ci(
                    claim, receipt, pr
                ):
                    return {"status": "waiting", "ticket": claim["ticket"]}
                if pr.get("status") != "ready":
                    return {"status": "waiting", "ticket": claim["ticket"]}
                attested = self.json_call(
                    "ticket-attest", "--ticket", claim["ticket"],
                    "--lease", claim["lease"], "--receipt", receipt,
                    "--workdir", claim["worktree"], "--action", "approval",
                    "--attest-only", "--json",
                )
                if (
                    attested.get("action") != "approval-attested"
                    or attested.get("auto_merge") is not False
                    or not SHA.fullmatch(attested.get("head", ""))
                    or attested["head"] == pr["head"]
                    or attested.get("pr_number") != pr.get("pr_number")
                ):
                    raise ControllerError(
                        "approval attestation did not materialize exact H2"
                    )
                self.migrate_passport(claim, "merge-pending")
                self.event(
                    "approval_attested_before_publication",
                    claim["ticket"],
                    approved_head=attested["head"],
                    reviewed_head=pr["head"],
                )
                return {"status": "progressed", "ticket": claim["ticket"]}
            if stage.startswith(
                "AWAIT-MERGE approval attested; protected auto-merge request pending"
            ):
                pr = self.ticket_pr(claim, receipt)
                if pr.get("status") == "failed" and self.retry_ci(
                    claim, receipt, pr
                ):
                    return {"status": "waiting", "ticket": claim["ticket"]}
                if pr.get("status") != "ready":
                    return {"status": "waiting", "ticket": claim["ticket"]}
                if not self.publication_ready(claim, receipt, pr["head"]):
                    return {"status": "waiting", "ticket": claim["ticket"]}
                approval = self.json_call(
                    "ticket-attest", "--ticket", claim["ticket"],
                    "--lease", claim["lease"], "--receipt", receipt,
                    "--workdir", claim["worktree"], "--action", "approval",
                    "--json",
                )
                if (
                    approval.get("action") != "approval"
                    or approval.get("auto_merge") is not True
                    or approval.get("head") != pr["head"]
                    or approval.get("pr_number") != pr.get("pr_number")
                ):
                    raise ControllerError(
                        "protected auto-merge did not bind exact H2"
                    )
                self.migrate_passport(claim, "merge-pending")
                self.event(
                    "protected_auto_merge_requested",
                    claim["ticket"],
                    head_sha=pr["head"],
                )
                return {"status": "progressed", "ticket": claim["ticket"]}
            if stage.startswith("AWAIT_DEPENDENCY"):
                claim["status"] = "waiting"
                self.save_claim(claim)
                self.event(
                    "dependency_wait",
                    claim["ticket"],
                    dependencies=stage.partition(" ")[2],
                )
                return {"status": "waiting", "ticket": claim["ticket"]}
            if stage.startswith("AWAIT-OPERATOR"):
                claim["status"] = "waiting"
                self.save_claim(claim)
                return {"status": "waiting", "ticket": claim["ticket"]}
            if stage.startswith("AWAIT_BUDGET"):
                if claim.get("publication_lease"):
                    self.release_publication(claim)
                self.release_ticket_lease(claim)
                claim.update(
                    budget_sha256=self.envelope_digest(), receipt="",
                    role="", status="budget",
                )
                self.save_claim(claim)
                self.event("budget_wait", claim["ticket"])
                return {"status": "budget", "ticket": claim["ticket"]}
            if stage.startswith("AWAIT-MERGE protected auto-merge requested"):
                if self.ticket_merged(claim):
                    if claim.get("publication_lease"):
                        self.release_publication(claim)
                    self.migrate_passport(claim, "merged")
                    self.closeout(claim)
                    return {"status": "progressed", "ticket": claim["ticket"]}
                pr = self.ticket_pr(claim, receipt)
                if pr.get("status") == "failed" and self.retry_ci(
                    claim, receipt, pr
                ):
                    self.publication_ready(claim, receipt, pr["head"])
                    return {"status": "waiting", "ticket": claim["ticket"]}
                if pr.get("status") == "failed":
                    self.publication_repair(claim, receipt, pr)
                    return {"status": "progressed", "ticket": claim["ticket"]}
                if pr.get("status") in {"wait", "ready"}:
                    self.publication_ready(claim, receipt, pr["head"])
                    return {"status": "waiting", "ticket": claim["ticket"]}
                raise ControllerError("publication PR gate returned an invalid status")
            if stage.startswith("AWAIT-MERGE closeout auto-merge pending"):
                self.closeout(claim)
                return {"status": "waiting", "ticket": claim["ticket"]}
            if stage.startswith("COMPLETE"):
                self.event("ticket_complete", claim["ticket"])
                self.release(claim)
                return {"status": "complete", "ticket": claim["ticket"]}
            if stage.startswith("ESCALATE "):
                detail = stage.partition(" ")[2]
                self.block(claim, "state-machine-escalation")
                self.event(
                    "state_machine_escalated", claim["ticket"], detail=detail,
                )
                return {"status": "blocked", "ticket": claim["ticket"]}
            if stage in {
                "REFUSE refresh receipt was not committed directly after its merge",
                "REFUSE stale refresh receipt does not bind this branch history",
            }:
                with self.git_lock:
                    value = self.json_call(
                        "ticket-attest", "--ticket", claim["ticket"],
                        "--lease", claim["lease"], "--receipt", receipt,
                        "--workdir", claim["worktree"],
                        "--action", "refresh", "--json",
                    )
                    if value.get("action") != "refresh":
                        raise ControllerError(
                            "refresh topology repair was not materialized"
                        )
                    self.migrate_passport(claim, "validating")
                    self.event(
                        "refresh_topology_repaired", claim["ticket"],
                        head_sha=value.get("head"),
                    )
                return {"status": "progressed", "ticket": claim["ticket"]}
            dependency_refresh = re.fullmatch(
                r"REFUSE dependency refresh required; "
                r"dependencies=(T-[0-9]+(?:,T-[0-9]+)*); "
                r"protected-main=([0-9a-f]{40})",
                stage,
            )
            if dependency_refresh:
                receipt_record = read(self.state / f"{claim['ticket']}.json")
                if (
                    receipt_record.get("receipt_sha256") != receipt
                    or receipt_record.get("head_sha")
                    != subprocess.run(
                        ["git", "-C", claim["worktree"], "rev-parse", "HEAD"],
                        text=True, capture_output=True, check=True, timeout=120,
                    ).stdout.strip()
                ):
                    raise ControllerError(
                        "dependency refresh receipt does not bind the old head"
                    )
                value = self.json_call(
                    "ticket-attest", "--ticket", claim["ticket"],
                    "--lease", claim["lease"], "--receipt", receipt,
                    "--workdir", claim["worktree"],
                    "--action", "dependency-refresh", "--json",
                )
                if value.get("action") == "dependency-wait":
                    claim["status"] = "waiting"
                    self.save_claim(claim)
                    self.event(
                        "dependency_base_moved", claim["ticket"],
                        expected=value.get("expected_protected_head"),
                        observed=value.get("observed_protected_head"),
                    )
                    return {"status": "waiting", "ticket": claim["ticket"]}
                attestation = value.get("attestation")
                refreshed = value.get("head", "")
                if (
                    value.get("action") not in {
                        "dependency-refresh", "dependency-conflict-refresh",
                    }
                    or not isinstance(attestation, dict)
                    or attestation.get("old_head") != receipt_record["head_sha"]
                    or attestation.get("protected_head")
                    != dependency_refresh[2]
                    or not SHA.fullmatch(refreshed)
                    or subprocess.run(
                        [
                            "git", "-C", claim["worktree"], "merge-base",
                            "--is-ancestor", dependency_refresh[2], refreshed,
                        ],
                        check=False, timeout=120,
                    ).returncode != 0
                ):
                    raise ControllerError(
                        "dependency refresh did not bind protected main"
                    )
                self.migrate_passport(claim, "validating")
                self.event(
                    (
                        "dependency_conflict_routed"
                        if value.get("action") == "dependency-conflict-refresh"
                        else "dependency_base_refreshed"
                    ),
                    claim["ticket"],
                    dependencies=dependency_refresh[1],
                    old_head=receipt_record["head_sha"],
                    protected_main=dependency_refresh[2],
                    refreshed_head=refreshed,
                    **(
                        {
                            "repair_owner": attestation.get("repair_owner"),
                            "conflict_paths": [
                                item.get("path")
                                for item in attestation.get("conflicts", [])
                                if isinstance(item, dict)
                            ],
                        }
                        if value.get("action")
                        == "dependency-conflict-refresh"
                        else {}
                    ),
                )
                return {"status": "progressed", "ticket": claim["ticket"]}
            if stage.startswith("REFUSE"):
                self.block(claim, "state-machine-refusal")
                return {"status": "blocked", "ticket": claim["ticket"]}
            raise ControllerError(f"unsupported deterministic stage: {stage}")
        except (ControllerError, json.JSONDecodeError, OSError, subprocess.SubprocessError) as error:
            claim["status"] = "blocked"
            self.save_claim(claim)
            self.withdraw_publication(claim)
            if not self.role_active(claim):
                self.release_ticket_lease(claim)
            self.event("controller_error", claim["ticket"], error=str(error))
            return {"status": "error", "ticket": claim["ticket"], "error": str(error)}

    def reconcile_ticket_until_wait(self, claim: dict[str, Any]) -> dict[str, str]:
        while True:
            result = self.reconcile_ticket(claim)
            if result.get("status") != "progressed":
                if (
                    result.get("status") in {
                        "blocked", "budget", "error", "maintenance", "waiting",
                    }
                    and not self.role_active(claim)
                ):
                    self.park_claim(claim)
                return result

    def reconcile(self) -> dict[str, Any]:
        existing = self.load_claims()
        if self.qualification:
            tickets = set(self.qualification["tickets"])
            for claim in existing:
                if claim["ticket"] not in tickets:
                    self.withdraw_publication(claim)
            existing = [claim for claim in existing if claim["ticket"] in tickets]
        self.recover_missing_passport_claims(existing)
        self.recover_each(
            existing, self.recover_upgraded_claims, "release-upgrade",
        )
        self.recover_each(
            existing, self.recover_terminal_exports, "terminal-export",
        )
        self.recover_each(
            existing, self.recover_repaired_failures, "targeted-repair",
        )
        self.event(
            "controller_started", recovered_tickets=sorted(
                item["ticket"] for item in existing if self.runnable(item)
            ),
        )
        try:
            claims = self.claim_new(existing)
        except ControllerError as error:
            self.event("admission_blocked", error=str(error))
            claims = existing
        if (
            self.qualification
            and not self.qualification_marker("qualification-restart-boundary")
        ):
            active = sorted(
                item["ticket"] for item in claims if self.runnable(item)
            )
            target = self.qualification["target_done"]
            if len(active) == target:
                self.qualification_marker(
                    "qualification-restart-boundary", create=True,
                )
                self.event("restart_boundary", tickets=active)
                return {
                    "active": target,
                    "results": [],
                    "schema": SCHEMA,
                    "status": "restart_required",
                }
            return {
                "active": len(active),
                "results": [],
                "schema": SCHEMA,
                "status": "waiting_for_target",
            }
        if (
            self.qualification
            and not self.qualification_marker("qualification-recovered")
            and self.qualification_marker("qualification-restart-boundary")
        ):
            recovered = sorted(
                item["ticket"] for item in existing if self.runnable(item)
            )
            if recovered != sorted(self.qualification["tickets"]):
                raise ControllerError("qualification restart did not recover every target ticket")
            self.qualification_marker("qualification-recovered", create=True)
            self.event("controller_recovered", tickets=recovered)

        results: dict[str, dict[str, str]] = {}
        settled: set[str] = set()
        futures: dict[Future, dict[str, Any]] = {}
        worker_limit = min(4, self.capacity)
        executor = ThreadPoolExecutor(max_workers=worker_limit)

        def submit_ready(
            candidates: list[dict[str, Any]],
            all_claims: list[dict[str, Any]],
        ) -> None:
            available = worker_limit - len(futures)
            reserved_live = sum(
                not self.consumes_capacity(claim)
                for claim in futures.values()
            )
            capacity_slots = max(
                0,
                self.capacity
                - sum(self.consumes_capacity(claim) for claim in all_claims)
                - reserved_live,
            )
            for claim in sorted(
                candidates, key=lambda item: not self.consumes_capacity(item)
            ):
                if available <= 0:
                    break
                if not self.consumes_capacity(claim):
                    if capacity_slots <= 0:
                        continue
                    capacity_slots -= 1
                future = executor.submit(
                    self.reconcile_ticket_until_wait, claim
                )
                futures[future] = claim
                available -= 1

        try:
            while True:
                claims = self.load_claims()
                if self.qualification:
                    tickets = set(self.qualification["tickets"])
                    claims = [
                        claim for claim in claims if claim["ticket"] in tickets
                    ]
                busy = {claim["ticket"] for claim in futures.values()}
                idle = [
                    claim for claim in claims
                    if claim["ticket"] not in busy
                    and claim["ticket"] not in settled
                    and not self.role_active(claim)
                ]
                self.recover_missing_passport_claims(claims)
                self.recover_each(
                    idle, self.recover_upgraded_claims, "release-upgrade",
                )
                self.recover_each(
                    idle, self.recover_terminal_exports, "terminal-export",
                )
                self.recover_each(
                    idle, self.recover_repaired_failures, "targeted-repair",
                )
                # Existing pinned claims are scheduled before potentially slow
                # admission or batch route preparation.
                ready = [
                    claim for claim in idle
                    if self.runnable(claim)
                    and self.route_path(claim).exists()
                ]
                submit_ready(ready, claims)
                busy.update(claim["ticket"] for claim in futures.values())
                reserved_live = sum(
                    not self.consumes_capacity(claim)
                    for claim in futures.values()
                )
                try:
                    claims = (
                        self.claim_new(claims, reserved_live)
                        if reserved_live
                        else self.claim_new(claims)
                    )
                except ControllerError as error:
                    self.event("admission_blocked", error=str(error))
                new_idle = [
                    claim for claim in claims
                    if claim["ticket"] not in busy
                    and claim["ticket"] not in settled
                    and not self.role_active(claim)
                    and self.runnable(claim)
                ]
                pin_results = self.pin_routes(new_idle)
                pin_waiting = {item["ticket"] for item in pin_results}
                for item in pin_results:
                    results[item["ticket"]] = item
                    settled.add(item["ticket"])
                submit_ready(
                    [
                        claim for claim in new_idle
                        if claim["ticket"] not in pin_waiting
                        and claim["ticket"] not in busy
                    ],
                    claims,
                )
                if not futures:
                    break
                done, _ = wait(
                    tuple(futures),
                    timeout=RECONCILE_INTERVAL_SECONDS,
                    return_when=FIRST_COMPLETED,
                )
                if not done:
                    continue
                for future in done:
                    claim = futures.pop(future)
                    try:
                        item = future.result()
                    except Exception as error:
                        claim["status"] = "blocked"
                        self.save_claim(claim)
                        self.event(
                            "ticket_worker_failed",
                            claim["ticket"],
                            error=str(error),
                        )
                        item = {
                            "error": str(error),
                            "status": "error",
                            "ticket": claim["ticket"],
                        }
                    results[claim["ticket"]] = item
                    if item.get("status") in {
                        "active", "blocked", "budget", "error", "maintenance",
                        "waiting",
                    }:
                        settled.add(claim["ticket"])
        finally:
            executor.shutdown(wait=True)
        claims = self.load_claims()
        ordered = [results[ticket] for ticket in sorted(results)]
        return {
            "active": len(
                [item for item in claims if self.consumes_capacity(item)]
            ),
            "results": ordered,
            "schema": SCHEMA,
            "status": (
                "ok"
                if all(item["status"] != "error" for item in ordered)
                else "error"
            ),
        }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--launcher", required=True, type=Path)
    parser.add_argument("--project", required=True)
    parser.add_argument("--product-root", required=True, type=Path)
    parser.add_argument("--release-path", required=True, type=Path)
    parser.add_argument("--state-dir", required=True, type=Path)
    args = parser.parse_args()
    lock_descriptor = -1
    try:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", args.project):
            raise ControllerError("invalid project")
        state = safe_directory(args.state_dir)
        lock_descriptor = os.open(
            state / "reconcile.lock",
            os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            fcntl.flock(lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(canonical({"schema": SCHEMA, "status": "busy"}))
            return
        result = Controller(args).reconcile()
        print(canonical(result))
        if result["status"] == "error":
            raise SystemExit(1)
    except (
        FileNotFoundError, json.JSONDecodeError, OSError, ControllerError,
        subprocess.SubprocessError,
    ) as error:
        print(canonical({"error": str(error), "schema": SCHEMA, "status": "error"}))
        raise SystemExit(1)
    finally:
        if lock_descriptor >= 0:
            os.close(lock_descriptor)


if __name__ == "__main__":
    main()
