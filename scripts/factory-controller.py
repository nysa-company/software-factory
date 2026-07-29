#!/usr/bin/env python3
"""Contract 1.8 non-agent ticket reconciliation controller."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
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


SCHEMA = "nysa.software-factory.controller/v1"
CLAIM_SCHEMA = "nysa.software-factory.controller-claim/v1"
EVENT_SCHEMA = "nysa.software-factory.controller-event/v1"
QUALIFICATION_SCHEMA = "nysa.software-factory.qualification/v2"
TICKET = re.compile(r"^T-[0-9]+$")
SHA = re.compile(r"^[0-9a-f]{40}$")
DIGEST = re.compile(r"^[0-9a-f]{64}$")
TERMINAL_ACCOUNTING = {
    "completed", "abandoned_conservative", "cancelled", "cancelled_conservative",
}


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
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        name, separator, value = line.partition("=")
        if not separator or name in values:
            raise ControllerError("run manifest is malformed")
        values[name] = value
    return values


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
        tickets = value.get("tickets")
        target_done = value.get("target_done")
        if (
            set(value) != {
                "budget_usd", "capacity", "contract_version", "factory_sha",
                "generation", "per_run_budget_usd", "per_ticket_budget_usd",
                "schema", "target_done", "tickets",
            }
            or value.get("contract_version") != "1.8.0"
            or value.get("capacity") != 4
            or target_done not in {3, 4}
            or value.get("factory_sha") != self.release_path.name
            or value.get("budget_usd") != "100.000000"
            or value.get("per_ticket_budget_usd") != "25.000000"
            or value.get("per_run_budget_usd") != "2.000000"
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
            "factory_sha": (
                self.qualification["factory_sha"] if self.qualification else None
            ),
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
                or not DIGEST.fullmatch(value.get("lease", ""))
                or not Path(value.get("worktree", "")).is_absolute()
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

    def claim_new(self, existing: list[dict[str, Any]]) -> list[dict[str, Any]]:
        claims = list(existing)
        excluded = sorted(
            item["ticket"]
            for item in claims if item["status"] in {"blocked", "budget"}
        )
        while len([item for item in claims if self.runnable(item)]) < self.capacity:
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
        self.json_call(
            "release", "--ticket", claim["ticket"], "--lease", claim["lease"],
        )
        claim["status"] = "blocked"
        self.save_claim(claim)
        self.event("ticket_blocked", claim["ticket"], reason=reason)

    def active_run(self, ticket: str) -> bool:
        active = self.product / "factory/.active-runs"
        return active.is_dir() and any(active.glob(f"{ticket}.*"))

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
            raise ControllerError("receipt has ambiguous terminal run evidence")
        return matches[0] if matches else None

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

    def recover_upgraded_claims(self, claims: list[dict[str, Any]]) -> None:
        for claim in claims:
            if claim["status"] != "blocked":
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
            if prior == self.release_path.name and not self.marker(pending):
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
                self.save_claim(claim)
            try:
                self.migrate_passport(claim, "preserve")
            except ControllerError:
                self.json_call(
                    "release", "--ticket", claim["ticket"],
                    "--lease", claim["lease"],
                )
                raise
            claim.update(receipt="", role="", status="claimed")
            self.save_claim(claim)
            self.event(
                "upgraded_claim_recovered", claim["ticket"],
                from_factory_sha=prior,
            )

    def recover_repaired_failures(self, claims: list[dict[str, Any]]) -> None:
        for claim in claims:
            if (
                claim["status"] != "blocked"
                or not claim.get("receipt")
                or self.active_run(claim["ticket"])
            ):
                continue
            terminal = self.terminal_for_receipt(claim["ticket"], claim["receipt"])
            push_failure = (
                terminal is not None
                and terminal.get("role_exit") == "role_exit_push_failed"
            )
            interrupted_before_submission = (
                terminal is not None
                and terminal.get("phase") == "abandoned"
                and terminal.get("accounting_state") == "abandoned_conservative"
                and terminal.get("task_submitted") == "0"
                and terminal.get("exit_status") == "143"
                and not terminal.get("role_exit")
            )
            if not push_failure and not interrupted_before_submission:
                continue
            passport_path = self.state / "passports" / f"{claim['ticket']}.json"
            if not passport_path.exists():
                continue
            try:
                validation = self.json_call(
                    "passport", "validate", "--ticket", claim["ticket"],
                    "--workdir", claim["worktree"], "--json",
                )
            except ControllerError:
                if not push_failure:
                    continue
                try:
                    self.migrate_passport(claim, "preserve")
                    validation = self.json_call(
                        "passport", "validate", "--ticket", claim["ticket"],
                        "--workdir", claim["worktree"], "--json",
                    )
                except ControllerError:
                    continue
            passport = read(passport_path)
            head = passport.get("head_sha", "")
            branch = passport.get("branch", "")
            if (
                validation.get("status") != "ok"
                or validation.get("passport") != passport.get("passport_sha256")
                or not SHA.fullmatch(head)
                or branch != claim["branch"]
            ):
                continue
            remote = subprocess.run(
                [
                    "git", "-C", claim["worktree"], "ls-remote", "--exit-code",
                    "origin", f"refs/heads/{branch}",
                ],
                text=True, capture_output=True, check=False, timeout=120,
            )
            if remote.returncode != 0 or remote.stdout != (
                f"{head}\trefs/heads/{branch}\n"
            ):
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
                    raise ControllerError("repaired push lease is invalid")
                claim["lease"] = lease["lease_id"]
            failed_run = terminal.get("run_id", "")
            claim.update(receipt="", role="", status="claimed")
            self.save_claim(claim)
            self.event(
                (
                    "push_failure_recovered"
                    if push_failure
                    else "interrupted_role_recovered"
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
        if self.active_run(claim["ticket"]):
            return False
        terminal = self.terminal_for_receipt(claim["ticket"], claim["receipt"])
        if terminal is None:
            claim.update(receipt="", role="", status="claimed")
            self.save_claim(claim)
            return True
        publication = (
            "validating"
            if claim["role"] in {"reviewer", "narrator"}
            else "none"
        )
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
            if (
                self.qualification
                and terminal.get("role_exit") == "provider_failed"
                and terminal.get("route_id", "").startswith("cursor-")
            ):
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
            claim["status"] = "blocked"
            self.save_claim(claim)
            self.json_call(
                "release", "--ticket", claim["ticket"], "--lease", claim["lease"],
            )
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
        self, claim: dict[str, Any], role: str, receipt: str, failed_checks: list[str]
    ) -> None:
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
        task = f"Execute {role} for {claim['ticket']} from its frozen contract and repository state."
        if failed_checks:
            task += " Required GitHub checks failed: " + ", ".join(failed_checks)
        command = [
            str(self.launcher), self.project, "run",
            "--role", role, "--ticket", claim["ticket"],
            "--lease", claim["lease"], "--receipt", receipt,
            "--prompt-file", str(self.release_path / f"roles/{role}.md"),
            "--workdir", claim["worktree"], "--", task,
        ]
        log_path = self.logs / f"{claim['ticket']}-{role}.log"
        with log_path.open("a", encoding="utf-8") as log:
            subprocess.run(command, stdout=log, stderr=log, check=False)
        self.finish_pending_run(claim)

    def reconcile_ticket(self, claim: dict[str, Any]) -> dict[str, str]:
        try:
            if (self.product / "factory/MAINTENANCE").exists():
                return {"status": "maintenance", "ticket": claim["ticket"]}
            self.renew(claim)
            if not self.finish_pending_run(claim):
                return {
                    "status": (
                        claim["status"]
                        if claim["status"] in {"blocked", "cancelled"}
                        else "active"
                    ),
                    "ticket": claim["ticket"],
                }
            if not self.route_path(claim).exists():
                raise ControllerError("ticket route was not batch pinned")
            transition = self.json_call(
                "state-machine", "--ticket", claim["ticket"],
                "--lease", claim["lease"], "--workdir", claim["worktree"],
                "--json",
            )
            stage = transition.get("stage", "")
            receipt = transition.get("receipt", "")
            role = transition.get("role")
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
                self.json_call(
                    "ticket-attest", "--ticket", claim["ticket"],
                    "--lease", claim["lease"], "--receipt", receipt,
                    "--workdir", claim["worktree"], "--action", "bundle", "--json",
                )
                self.migrate_passport(claim, "validating")
                return {"status": "progressed", "ticket": claim["ticket"]}
            if stage.startswith("AWAIT-OPERATOR Linear approval observed"):
                pr = self.ticket_pr(claim, receipt)
                if pr.get("status") == "failed" and self.retry_ci(
                    claim, receipt, pr
                ):
                    return {"status": "waiting", "ticket": claim["ticket"]}
                if pr.get("status") != "ready":
                    return {"status": "waiting", "ticket": claim["ticket"]}
                if not self.publication_ready(claim, receipt, pr["head"]):
                    return {"status": "waiting", "ticket": claim["ticket"]}
                self.json_call(
                    "ticket-attest", "--ticket", claim["ticket"],
                    "--lease", claim["lease"], "--receipt", receipt,
                    "--workdir", claim["worktree"], "--action", "approval", "--json",
                )
                self.migrate_passport(claim, "merge-pending")
                return {"status": "progressed", "ticket": claim["ticket"]}
            if stage.startswith("AWAIT-OPERATOR"):
                claim["status"] = "waiting"
                self.save_claim(claim)
                return {"status": "waiting", "ticket": claim["ticket"]}
            if stage.startswith("AWAIT_BUDGET"):
                if claim.get("publication_lease"):
                    self.release_publication(claim)
                self.json_call(
                    "release", "--ticket", claim["ticket"], "--lease", claim["lease"],
                )
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
            if stage.startswith("REFUSE"):
                self.block(claim, "state-machine-refusal")
                return {"status": "blocked", "ticket": claim["ticket"]}
            raise ControllerError(f"unsupported deterministic stage: {stage}")
        except (ControllerError, json.JSONDecodeError, OSError, subprocess.SubprocessError) as error:
            claim["status"] = "blocked"
            self.save_claim(claim)
            self.withdraw_publication(claim)
            if not self.active_run(claim["ticket"]):
                self.json_call(
                    "release", "--ticket", claim["ticket"], "--lease", claim["lease"],
                )
            self.event("controller_error", claim["ticket"], error=str(error))
            return {"status": "error", "ticket": claim["ticket"], "error": str(error)}

    def reconcile(self) -> dict[str, Any]:
        existing = self.load_claims()
        if self.qualification:
            tickets = set(self.qualification["tickets"])
            for claim in existing:
                if claim["ticket"] not in tickets:
                    self.withdraw_publication(claim)
            existing = [claim for claim in existing if claim["ticket"] in tickets]
        self.recover_missing_passport_claims(existing)
        self.recover_upgraded_claims(existing)
        self.recover_repaired_failures(existing)
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
        if self.qualification and not self.marker("qualification-restart-boundary"):
            active = sorted(
                item["ticket"] for item in claims if self.runnable(item)
            )
            target = self.qualification["target_done"]
            if len(active) == target:
                self.marker("qualification-restart-boundary", {
                    "factory_sha": self.qualification["factory_sha"],
                    "schema": EVENT_SCHEMA,
                    "tickets": active,
                })
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
            and not self.marker("qualification-recovered")
            and self.marker("qualification-restart-boundary")
        ):
            recovered = sorted(
                item["ticket"] for item in existing if self.runnable(item)
            )
            if recovered != sorted(self.qualification["tickets"]):
                raise ControllerError("qualification restart did not recover every target ticket")
            self.marker("qualification-recovered", {
                "factory_sha": self.qualification["factory_sha"],
                "schema": EVENT_SCHEMA,
                "tickets": recovered,
            })
            self.event("controller_recovered", tickets=recovered)
        runnable = [item for item in claims if self.runnable(item)]
        results = self.pin_routes(runnable)
        waiting = {item["ticket"] for item in results}
        ready = [item for item in runnable if item["ticket"] not in waiting]
        with ThreadPoolExecutor(max_workers=min(4, len(ready) or 1)) as executor:
            results.extend(executor.map(self.reconcile_ticket, ready))
        return {
            "active": len(runnable),
            "results": results,
            "schema": SCHEMA,
            "status": "ok" if all(item["status"] != "error" for item in results) else "error",
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
