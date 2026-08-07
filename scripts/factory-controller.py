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
import sys
import tempfile
from threading import Lock
import time
from typing import Any
from urllib.parse import urlsplit


sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
from release_lineage import (  # noqa: E402
    passport_head_lineage, successor_release_lineage,
)
from qualification_artifacts import (  # noqa: E402
    ArtifactError as QualificationArtifactError,
    ensure_ticket as ensure_qualification_artifacts,
)
from legacy_closeout import (  # noqa: E402
    ValidationError as ProtectedTerminalError,
    protected_terminal,
)


SCHEMA = "nysa.software-factory.controller/v1"
CLAIM_SCHEMA = "nysa.software-factory.controller-claim/v1"
EVENT_SCHEMA = "nysa.software-factory.controller-event/v1"
QUALIFICATION_SCHEMA = "nysa.software-factory.qualification/v2"
TICKET = re.compile(r"^T-[0-9]+$")
SHA = re.compile(r"^[0-9a-f]{40}$")
DIGEST = re.compile(r"^[0-9a-f]{64}$")
FACTORY_ISSUE = re.compile(
    r"^https://github[.]com/[A-Za-z0-9_.-]+/software-factory/issues/[1-9][0-9]*$"
)
TERMINAL_ACCOUNTING = {
    "completed", "launch_void", "abandoned_conservative", "cancelled",
    "cancelled_conservative",
}
CONTRACT_RESUME_REFUSALS = frozenset({
    "resume_ancestry_invalid",
    "resume_commit_content_mismatch",
    "resume_commit_not_pushed",
    "resume_directives_ambiguous",
    "resume_receipt_mismatch",
})
INFLIGHT_STATES = frozenset({
    "Ready", "Planning", "Building", "Review", "Awaiting Approval",
    "Approved", "Blocked-Escalated",
})
RECONCILE_INTERVAL_SECONDS = 15
PREVIEW_IDENTITY_WAIT_SECONDS = 900
COMPLETION_CORRECTION_SCHEMA = (
    "nysa.software-factory.completed-role-correction/v1"
)
TERMINAL_ADOPTION_SCHEMA = (
    "nysa.software-factory.qualification-terminal-adoption/v2"
)
PROTECTED_TERMINAL_RECONCILIATION_SCHEMA = (
    "nysa.software-factory.qualification-protected-terminal-reconciliation/v1"
)
EMERGENCY_TERMINAL_RECONCILIATION_SCHEMA = (
    "nysa.software-factory.qualification-emergency-terminal-reconciliation/v1"
)


class ControllerError(ValueError):
    pass


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def valid_transition_evidence(value: dict[str, Any], ticket: str) -> bool:
    stage = value.get("stage")
    if not isinstance(stage, str) or not stage:
        return False
    runnable = re.fullmatch(
        r"(?:RUN|FIX) "
        r"(planner|spec-linter|test-author|builder|reviewer|narrator)",
        stage,
    )
    non_role = (
        stage.startswith("AWAIT-OPERATOR ")
        or stage.startswith("AWAIT_DEPENDENCY ")
        or stage.startswith("AWAIT_BUDGET ")
        or stage.startswith("COMPLETE ")
        or stage.startswith("ESCALATE ")
        or stage.startswith("REFUSE ")
        or stage.startswith((
            "AWAIT-MERGE approval attested; protected auto-merge request pending",
            "AWAIT-MERGE protected auto-merge requested",
            "AWAIT-MERGE closeout auto-merge pending",
        ))
    )
    loop = value.get("loop")
    valid_loop = loop is None or (
        isinstance(loop, dict)
        and set(loop) == {"attempt", "capped", "kind", "limit"}
        and isinstance(loop.get("attempt"), int)
        and not isinstance(loop.get("attempt"), bool)
        and loop["attempt"] >= 1
        and isinstance(loop.get("capped"), bool)
        and loop.get("kind") in {
            "builder-reviewer", "contract-repair", "planner-spec-linter",
        }
        and loop.get("limit") == 3
    )
    expected_role = runnable[1] if runnable else None
    return not any((
        not DIGEST.fullmatch(value.get("receipt", "")),
        value.get("schema") != "nysa.software-factory.state-machine/v1",
        value.get("status") != "ok",
        value.get("ticket") != ticket,
        value.get("action") != stage.partition(" ")[0],
        value.get("detail") != (stage.partition(" ")[2] or None),
        not valid_loop,
        not (runnable or non_role),
        value.get("role") != expected_role,
    ))


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
        self.qualification_manifest_sha256 = (
            hashlib.sha256(canonical(self.qualification).encode()).hexdigest()
            if self.qualification else ""
        )
        self.admission_refusals: dict[str, dict[str, str]] = {}
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
        if self.qualification:
            value.update({
                "qualification_generation": self.qualification["generation"],
                "qualification_manifest_sha256": (
                    self.qualification_manifest_sha256
                ),
            })
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

    def event_once(self, name: str, ticket: str, **details: Any) -> None:
        for path in sorted(self.events.glob("*.json")):
            value = read(path)
            digest = value.pop("event_sha256", "")
            if digest != hashlib.sha256(canonical(value).encode()).hexdigest():
                raise ControllerError("controller event evidence is invalid")
            if (
                value.get("event") == name
                and value.get("factory_sha") == self.release_path.name
                and value.get("ticket") == ticket
                and (
                    not self.qualification
                    or (
                        value.get("qualification_generation")
                        == self.qualification["generation"]
                        and value.get("qualification_manifest_sha256")
                        == self.qualification_manifest_sha256
                    )
                )
                and all(value.get(key) == item for key, item in details.items())
            ):
                return
        self.event(name, ticket, **details)

    def record_contract_resume_refusal(
        self, claim: dict[str, Any], reason_code: str, evidence: dict[str, Any]
    ) -> None:
        if (
            reason_code not in CONTRACT_RESUME_REFUSALS
            or not DIGEST.fullmatch(claim.get("receipt", ""))
        ):
            raise ControllerError("contract resume refusal reason is invalid")
        allowed = {
            "actual_bytes", "changed_path_count", "expected_bytes",
            "first_differing_line", "local_head", "remote_head",
        }
        if set(evidence) - allowed:
            raise ControllerError("contract resume refusal evidence is invalid")
        for key in ("local_head", "remote_head"):
            if key in evidence and evidence[key] not in {None, ""} and not SHA.fullmatch(
                evidence[key]
            ):
                raise ControllerError("contract resume refusal evidence is invalid")
        for key in (
            "actual_bytes", "changed_path_count", "expected_bytes",
            "first_differing_line",
        ):
            if key in evidence and evidence[key] is not None and (
                isinstance(evidence[key], bool)
                or not isinstance(evidence[key], int)
                or evidence[key] < 0
            ):
                raise ControllerError("contract resume refusal evidence is invalid")
        self.event_once(
            "contract_resume_refused", claim["ticket"],
            blocked_receipt_sha256=claim["receipt"], reason_code=reason_code,
            **evidence,
        )

    def adopt_qualification_terminal(self, ticket: str) -> dict[str, Any]:
        if (
            not self.qualification
            or self.qualification.get("mode") != "successor"
        ):
            raise ControllerError("terminal adoption requires successor qualification")
        source = self.qualification["source_factory_sha"]
        passport_path = self.state / "passports" / f"{ticket}.json"
        done_path = (
            self.product / "factory" / "attestations" / ticket / "done.json"
        )
        try:
            passport = read(passport_path)
            done_info = done_path.lstat()
            if (
                not stat.S_ISREG(done_info.st_mode)
                or stat.S_IMODE(done_info.st_mode) & 0o022
                or done_info.st_size > 1_000_000
            ):
                raise ControllerError("qualification terminal attestation is unsafe")
            done = json.loads(done_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError) as error:
            raise ControllerError("qualification terminal adoption is incomplete") from error
        marker_name = (
            f"qualification-terminal-adoption-{self.release_path.name}-{ticket}"
        )
        marker_path = self.state / f"{marker_name}.json"
        if passport.get("factory_sha") != self.release_path.name:
            if not successor_release_lineage(
                passport.get("factory_release_history"),
                passport.get("migration_history"),
                source,
                passport.get("factory_sha", ""),
            ):
                raise ControllerError(
                    "qualification terminal passport has unknown release"
                )
            branch = f"refs/heads/ticket/{ticket}"
            worktrees = self.worktrees_by_branch().get(branch, [])
            if len(worktrees) != 1 or marker_path.exists():
                raise ControllerError("qualification terminal adoption cell is ambiguous")
            claim = {
                "branch": f"ticket/{ticket}",
                "ticket": ticket,
                "worktree": worktrees[0],
            }
            validation = self.json_call(
                "passport", "validate", "--ticket", ticket,
                "--workdir", worktrees[0], "--json",
            )
            if (
                validation.get("status") != "ok"
                or validation.get("passport") != passport.get("passport_sha256")
            ):
                raise ControllerError("qualification terminal passport is invalid")
            source_history = {
                item.get("factory_sha")
                for item in passport.get("factory_release_history", [])
                if isinstance(item, dict)
            }
            source_evidence = [
                passport.get("charge_records"),
                passport.get("completed_role_evidence"),
            ]
            if (
                passport.get("ticket") != ticket
                or passport.get("branch") != f"ticket/{ticket}"
                or passport.get("current_state") != "Approved"
                or passport.get("publication_state") != "merged"
                or any(
                    not isinstance(records, list) for records in source_evidence
                )
                or any(
                    item.get("factory_sha") == self.release_path.name
                    for records in source_evidence if isinstance(records, list)
                    for item in records if isinstance(item, dict)
                )
                or not DIGEST.fullmatch(passport.get("passport_sha256", ""))
                or source not in source_history
                or self.release_path.name in source_history
                or done.get("schema")
                != "nysa.software-factory.ticket-done/v1"
                or done.get("ticket") != ticket
                or done.get("kit_sha") not in source_history
                or not passport_head_lineage(
                    passport, done.get("approved_pr_head", "")
                )
                or not isinstance(done.get("pr_number"), int)
                or isinstance(done.get("pr_number"), bool)
                or not SHA.fullmatch(done.get("approved_pr_head", ""))
                or not SHA.fullmatch(done.get("merge_commit", ""))
            ):
                raise ControllerError("qualification source terminal is invalid")
            migrated = self.migrate_passport(claim, "preserve")
            passport = read(passport_path)
            if (
                migrated.get("status") != "ok"
                or migrated.get("passport") != passport.get("passport_sha256")
            ):
                raise ControllerError("qualification terminal passport migration failed")
        elif not marker_path.exists():
            branch = f"refs/heads/ticket/{ticket}"
            worktrees = self.worktrees_by_branch().get(branch, [])
            if len(worktrees) != 1:
                raise ControllerError("qualification terminal adoption cell is ambiguous")
            validation = self.json_call(
                "passport", "validate", "--ticket", ticket,
                "--workdir", worktrees[0], "--json",
            )
            if (
                validation.get("status") != "ok"
                or validation.get("passport") != passport.get("passport_sha256")
            ):
                raise ControllerError("qualification terminal passport is invalid")

        migrations = passport.get("migration_history")
        history = passport.get("factory_release_history")
        edge = migrations[-1] if isinstance(migrations, list) and migrations else {}
        source_passport = edge.get("from_passport_sha256")
        evidence = [
            passport.get("charge_records"), passport.get("completed_role_evidence")
        ]
        candidate_records = [
            item for records in evidence if isinstance(records, list)
            for item in records
            if isinstance(item, dict)
            and item.get("factory_sha") == self.release_path.name
        ]
        history_shas = {
            item.get("factory_sha") for item in history or []
            if isinstance(item, dict)
        }
        pre_candidate_history = {
            item.get("factory_sha") for item in history or []
            if isinstance(item, dict)
            and item.get("factory_sha") != self.release_path.name
        }
        if (
            passport.get("ticket") != ticket
            or passport.get("branch") != f"ticket/{ticket}"
            or passport.get("factory_sha") != self.release_path.name
            or passport.get("current_state") != "Approved"
            or passport.get("publication_state") != "merged"
            or any(not isinstance(records, list) for records in evidence)
            or not DIGEST.fullmatch(passport.get("passport_sha256", ""))
            or passport.get("parent_digest") != source_passport
            or edge.get("schema")
            != "nysa.software-factory.ticket-passport-migration/v2"
            or edge.get("from_factory_sha") not in pre_candidate_history
            or edge.get("to_factory_sha") != self.release_path.name
            or not DIGEST.fullmatch(source_passport or "")
            or not {source, self.release_path.name}.issubset(history_shas)
            or not successor_release_lineage(
                history, migrations, source, self.release_path.name,
            )
            or candidate_records
            or done.get("schema") != "nysa.software-factory.ticket-done/v1"
            or done.get("ticket") != ticket
            or done.get("kit_sha") not in pre_candidate_history
            or not passport_head_lineage(
                passport, done.get("approved_pr_head", "")
            )
            or not isinstance(done.get("pr_number"), int)
            or isinstance(done.get("pr_number"), bool)
            or not SHA.fullmatch(done.get("approved_pr_head", ""))
            or not SHA.fullmatch(done.get("merge_commit", ""))
        ):
            raise ControllerError("qualification terminal adoption is invalid")
        value = {
            "approved_pr_head": done["approved_pr_head"],
            "candidate_passport_sha256": passport["passport_sha256"],
            "done_sha256": hashlib.sha256(canonical(done).encode()).hexdigest(),
            "factory_sha": self.release_path.name,
            "merge_commit": done["merge_commit"],
            "pr_number": done["pr_number"],
            "passport_source_factory_sha": edge["from_factory_sha"],
            "schema": TERMINAL_ADOPTION_SCHEMA,
            "source_current_state": "Approved",
            "source_factory_sha": source,
            "source_passport_sha256": source_passport,
            "source_publication_state": "merged",
            "ticket": ticket,
        }
        if marker_path.exists():
            if read(marker_path) != value:
                raise ControllerError("qualification terminal adoption marker changed")
        elif not self.marker(marker_name, value):
            raise ControllerError("qualification terminal adoption marker raced")
        return value

    def qualification_protected_terminal(
        self, ticket: str,
    ) -> dict[str, Any]:
        done_path = f"factory/attestations/{ticket}/done.json"
        ticket_path = f"factory/tickets/{ticket}.md"
        try:
            terminal = protected_terminal(self.product, ticket)
            result = subprocess.run(
                [
                    "git", "-C", str(self.product), "rev-parse",
                    "refs/remotes/origin/main",
                    "refs/remotes/origin/main^{tree}",
                    f"refs/remotes/origin/main:{ticket_path}",
                ],
                text=True, capture_output=True, check=True, timeout=120,
            )
            protected_main, protected_tree, ticket_blob = result.stdout.splitlines()
            done = json.loads(subprocess.run(
                [
                    "git", "-C", str(self.product), "show",
                    f"refs/remotes/origin/main:{done_path}",
                ],
                text=True, capture_output=True, check=True, timeout=120,
            ).stdout)
        except (
            ProtectedTerminalError, json.JSONDecodeError, OSError,
            subprocess.SubprocessError, ValueError,
        ) as error:
            raise ControllerError(
                "qualification protected terminal is invalid"
            ) from error
        if (
            terminal.get("ticket") != ticket
            or terminal.get("basis") not in {
                "attested-done", "attested-emergency-closeout",
            }
            or not SHA.fullmatch(protected_main)
            or not SHA.fullmatch(protected_tree)
            or not SHA.fullmatch(ticket_blob)
        ):
            raise ControllerError("qualification protected terminal is invalid")
        return {
            "done_sha256": hashlib.sha256(canonical(done).encode()).hexdigest(),
            "protected_main_sha": protected_main,
            "protected_main_tree": protected_tree,
            "protected_ticket_blob": ticket_blob,
            "qualification_charge_micro_usd": 0,
            "reconciliation_schema": PROTECTED_TERMINAL_RECONCILIATION_SCHEMA,
            "terminal_basis": terminal["basis"],
        }

    def qualification_release_receipts(self) -> dict[str, str]:
        root = safe_directory(self.release_path.parent.parent)
        if self.release_path.parent.name != "releases":
            raise ControllerError("qualification release path is invalid")
        projects = safe_directory(root / "projects")
        active = read(safe_directory(projects / self.project) / "active.json")
        receipts = safe_directory(root / "receipts")
        receipt_id = active.get("receipt_id")
        if (
            active.get("project") != self.project
            or active.get("kit_sha") != self.release_path.name
            or active.get("release_path") != str(self.release_path)
            or not DIGEST.fullmatch(receipt_id or "")
        ):
            raise ControllerError("qualification release receipt is invalid")
        result: dict[str, str] = {}
        seen: set[str] = set()
        while receipt_id:
            if receipt_id in seen or len(seen) >= 128:
                raise ControllerError("qualification release receipt is invalid")
            seen.add(receipt_id)
            receipt = read(receipts / f"{receipt_id}.json")
            unsigned = dict(receipt)
            embedded = unsigned.pop("receipt_id", "")
            previous = receipt.get("previous_receipt_id")
            kit_sha = receipt.get("kit_sha")
            if (
                embedded != receipt_id
                or receipt_id
                != hashlib.sha256((canonical(unsigned) + "\n").encode()).hexdigest()
                or receipt.get("project") != self.project
                or receipt.get("contract_version") != "1.8.0"
                or receipt.get("qualification_mode") != "isolated"
                or receipt.get("product_path") != str(self.product)
                or receipt.get("status") != "pass"
                or not SHA.fullmatch(kit_sha or "")
                or not SHA.fullmatch(receipt.get("kit_tree") or "")
                or not DIGEST.fullmatch(receipt.get("provider_policy_sha256") or "")
                or (previous is not None and not DIGEST.fullmatch(previous or ""))
            ):
                raise ControllerError("qualification release receipt is invalid")
            result.setdefault(kit_sha, receipt_id)
            receipt_id = previous
        if self.release_path.name not in result:
            raise ControllerError("qualification release receipt is invalid")
        return result

    def qualification_emergency_terminal(self, ticket: str) -> dict[str, Any]:
        if (
            not self.qualification
            or self.qualification.get("mode") != "successor"
        ):
            raise ControllerError(
                "emergency terminal reconciliation requires successor qualification"
            )
        terminal = self.qualification_protected_terminal(ticket)
        done = json.loads((
            self.product / "factory/attestations" / ticket / "done.json"
        ).read_text(encoding="utf-8"))
        plan = done.get("plan") if isinstance(done, dict) else None
        passport_basis = plan.get("passport") if isinstance(plan, dict) else None
        claim_basis = plan.get("claim") if isinstance(plan, dict) else None
        passport = read(self.state / "passports" / f"{ticket}.json")
        pause_path = self.state / f"pause-{ticket}.json"
        pause = read(pause_path)
        pause_raw = (canonical(pause) + "\n").encode()
        signed_pause = dict(pause)
        pause_receipt = signed_pause.pop("pause_sha256", "")
        expected_passport = {
            name: passport.get(name)
            for name in (
                "passport_sha256", "current_state", "publication_state",
                "factory_sha", "head_sha",
            )
        }
        release_receipts = self.qualification_release_receipts()
        terminal_factory_sha = done.get("kit_sha")
        if (
            terminal.get("terminal_basis") != "attested-emergency-closeout"
            or done.get("schema")
            != "nysa.software-factory.ticket-emergency-done/v1"
            or not SHA.fullmatch(terminal_factory_sha or "")
            or terminal_factory_sha not in release_receipts
            or not isinstance(plan, dict)
            or plan.get("kit_sha") != terminal_factory_sha
            or plan.get("execution_basis") != "authenticated-passport"
            or passport_basis != expected_passport
            or passport.get("factory_sha")
            != self.qualification.get("source_factory_sha")
            or not isinstance(claim_basis, dict)
            or claim_basis.get("status") != "blocked"
            or claim_basis.get("role") != "factory-paused"
            or claim_basis.get("blocked_reason") != "factory-issue-pause"
            or claim_basis.get("parked") is not True
            or claim_basis.get("sha256")
            != hashlib.sha256(pause_raw).hexdigest()
            or claim_basis.get("receipt") != pause_receipt
            or (self.state / "claims" / f"{ticket}.json").exists()
            or pause.get("schema") != "nysa.software-factory.ticket-pause/v2"
            or pause.get("ticket") != ticket
            or pause.get("branch") != passport.get("branch")
            or pause.get("head_sha") != passport.get("head_sha")
            or pause.get("passport_sha256") != passport.get("passport_sha256")
            or pause.get("passport_factory_sha") != passport.get("factory_sha")
            or pause.get("current_state") != passport.get("current_state")
            or pause_receipt != hashlib.sha256(
                canonical(signed_pause).encode()
            ).hexdigest()
            or not DIGEST.fullmatch(pause_receipt)
            or (
                pause.get("status") == "budget"
                and not DIGEST.fullmatch(pause.get("budget_sha256") or "")
            )
            or pause.get("status") not in {"blocked", "budget", "claimed", "waiting"}
        ):
            raise ControllerError(
                "qualification emergency terminal evidence is invalid"
            )
        return {
            **terminal,
            "pause_file_sha256": claim_basis["sha256"],
            "pause_receipt_sha256": pause_receipt,
            "reconciliation_schema": EMERGENCY_TERMINAL_RECONCILIATION_SCHEMA,
            "source_current_state": passport["current_state"],
            "source_factory_sha": passport["factory_sha"],
            "source_head_sha": passport["head_sha"],
            "source_passport_sha256": passport["passport_sha256"],
            "source_publication_state": passport["publication_state"],
            "terminal_factory_sha": terminal_factory_sha,
            "terminal_release_receipt_id": release_receipts[terminal_factory_sha],
        }

    def record_qualification_done_targets(self) -> None:
        if not self.qualification:
            return
        completed = {
            ticket for ticket in self.qualification["tickets"]
            if self.product_ticket_done(ticket)
        }
        if not completed:
            return
        records = []
        for path in sorted(self.events.glob("*.json")):
            value = read(path)
            digest = value.pop("event_sha256", "")
            if (
                value.get("schema") != EVENT_SCHEMA
                or digest != hashlib.sha256(canonical(value).encode()).hexdigest()
            ):
                raise ControllerError("controller event evidence is invalid")
            records.append(value)
        for ticket in sorted(completed):
            matching_complete = [
                item for item in records
                if item.get("factory_sha") == self.release_path.name
                and item.get("event") == "ticket_complete"
                and item.get("ticket") == ticket
            ]
            matching_adoption = [
                item for item in records
                if item.get("factory_sha") == self.release_path.name
                and item.get("event") == "terminal_adopted"
                and item.get("ticket") == ticket
            ]
            matching_reconciliation = [
                item for item in records
                if item.get("factory_sha") == self.release_path.name
                and item.get("event") == "protected_terminal_reconciled"
                and item.get("ticket") == ticket
            ]
            matching_emergency = [
                item for item in records
                if item.get("factory_sha") == self.release_path.name
                and item.get("event") == "emergency_terminal_reconciled"
                and item.get("ticket") == ticket
            ]
            if (
                len(matching_complete) > 1
                or len(matching_adoption) > 1
                or len(matching_reconciliation) > 1
                or len(matching_emergency) > 1
            ):
                raise ControllerError("qualification terminal evidence was duplicated")
            passport_path = self.state / "passports" / f"{ticket}.json"
            if not passport_path.exists():
                if matching_adoption:
                    raise ControllerError(
                        "qualification terminal adoption is unexpected"
                    )
                reconciliation = self.qualification_protected_terminal(ticket)
                if matching_reconciliation:
                    stable = {
                        "done_sha256", "protected_ticket_blob",
                        "qualification_charge_micro_usd",
                        "reconciliation_schema", "terminal_basis",
                    }
                    if any(
                        matching_reconciliation[0].get(name)
                        != reconciliation[name]
                        for name in stable
                    ):
                        raise ControllerError(
                            "qualification protected terminal evidence changed"
                        )
                else:
                    self.event(
                        "protected_terminal_reconciled", ticket,
                        **reconciliation,
                    )
                if not matching_complete:
                    self.event_once("ticket_complete", ticket)
                continue
            if matching_reconciliation:
                raise ControllerError(
                    "qualification passport conflicts with protected terminal reconciliation"
                )
            try:
                done = json.loads((
                    self.product / "factory/attestations" / ticket / "done.json"
                ).read_text(encoding="utf-8"))
            except FileNotFoundError:
                done = {}
            except (json.JSONDecodeError, OSError) as error:
                raise ControllerError(
                    "qualification terminal adoption is incomplete"
                ) from error
            if done.get("schema") == "nysa.software-factory.ticket-emergency-done/v1":
                reconciliation = self.qualification_emergency_terminal(ticket)
                stable = {
                    "done_sha256", "pause_file_sha256", "pause_receipt_sha256",
                    "protected_ticket_blob", "qualification_charge_micro_usd",
                    "reconciliation_schema", "source_current_state",
                    "source_factory_sha", "source_head_sha",
                    "source_passport_sha256", "source_publication_state",
                    "terminal_basis", "terminal_factory_sha",
                    "terminal_release_receipt_id",
                }
                if matching_emergency and any(
                    matching_emergency[0].get(name) != reconciliation[name]
                    for name in stable
                ):
                    raise ControllerError(
                        "qualification emergency terminal evidence changed"
                    )
                if not matching_emergency:
                    self.event(
                        "emergency_terminal_reconciled", ticket,
                        **reconciliation,
                    )
                if not matching_complete:
                    self.event_once("ticket_complete", ticket)
                continue
            if matching_emergency:
                raise ControllerError(
                    "qualification emergency terminal reconciliation is unexpected"
                )
            adoption = None
            if self.qualification.get("mode") == "successor":
                passport = read(
                    self.state / "passports" / f"{ticket}.json"
                )
                edge = (
                    passport.get("migration_history", [])[-1]
                    if passport.get("migration_history") else {}
                )
                candidate_evidence = any(
                    item.get("factory_sha") == self.release_path.name
                    for name in ("charge_records", "completed_role_evidence")
                    for item in passport.get(name, []) or []
                    if isinstance(item, dict)
                )
                candidate_publication = any(
                    item.get("factory_sha") == self.release_path.name
                    and item.get("ticket") == ticket
                    and item.get("event") in {
                        "publication_acquired", "publication_released",
                    }
                    for item in records
                )
                adoption_marker = self.state / (
                    "qualification-terminal-adoption-"
                    f"{self.release_path.name}-{ticket}.json"
                )
                source = self.qualification["source_factory_sha"]
                if (
                    passport.get("factory_sha") != self.release_path.name
                    or adoption_marker.exists()
                    or matching_adoption
                    or (
                        passport.get("factory_sha") == self.release_path.name
                        and not candidate_evidence
                        and not candidate_publication
                        and successor_release_lineage(
                            passport.get("factory_release_history"),
                            passport.get("migration_history"),
                            source,
                            self.release_path.name,
                        )
                    )
                ):
                    adoption = self.adopt_qualification_terminal(ticket)
            if adoption:
                details = {
                    "adoption_schema": adoption["schema"],
                    "approved_pr_head": adoption["approved_pr_head"],
                    "candidate_passport_sha256": adoption[
                        "candidate_passport_sha256"
                    ],
                    "done_sha256": adoption["done_sha256"],
                    "merge_commit": adoption["merge_commit"],
                    "passport_source_factory_sha": adoption[
                        "passport_source_factory_sha"
                    ],
                    "pr_number": adoption["pr_number"],
                    "source_current_state": adoption["source_current_state"],
                    "source_factory_sha": adoption["source_factory_sha"],
                    "source_passport_sha256": adoption[
                        "source_passport_sha256"
                    ],
                    "source_publication_state": adoption[
                        "source_publication_state"
                    ],
                }
                if matching_adoption and any(
                    matching_adoption[0].get(name) != value
                    for name, value in details.items()
                ):
                    raise ControllerError("qualification terminal evidence changed")
                if not matching_adoption:
                    self.event("terminal_adopted", ticket, **details)
            elif matching_adoption:
                raise ControllerError("qualification terminal adoption is unexpected")
            if not matching_complete:
                self.event_once("ticket_complete", ticket)

    def record_admission_failure(
        self, error: ControllerError, claims: list[dict[str, Any]]
    ) -> None:
        raw = str(error)
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            decoded = {"error": raw, "reason_code": "unsafe_state"}
        if not isinstance(decoded, dict):
            decoded = {"error": raw, "reason_code": "unsafe_state"}
        evidence = {
            "error": str(decoded.get("error", raw))[:4096],
            "reason_code": str(decoded.get("reason_code", "unsafe_state"))[:64],
        }
        ticket = decoded.get("ticket", "")
        if isinstance(ticket, str) and TICKET.fullmatch(ticket):
            evidence["ticket"] = ticket
        digest = hashlib.sha256(canonical(evidence).encode()).hexdigest()
        path = self.state / "admission-incident.json"
        now = time.time_ns()
        previous = read(path) if path.exists() else {}
        same = previous.get("incident_sha256") == digest
        value = {
            "count": int(previous.get("count", 0)) + 1 if same else 1,
            "first_seen_epoch_ns": (
                previous.get("first_seen_epoch_ns", now) if same else now
            ),
            "incident_sha256": digest,
            "last_seen_epoch_ns": now,
            "next_reminder_epoch_ns": (
                previous.get("next_reminder_epoch_ns", now + 900_000_000_000)
                if same else now + 900_000_000_000
            ),
            "schema": "nysa.software-factory.admission-incident/v1",
            **evidence,
        }
        reminder = same and now >= value["next_reminder_epoch_ns"]
        if reminder:
            value["next_reminder_epoch_ns"] = now + 900_000_000_000
        write(path, value)
        if not same or reminder:
            name = (
                "admission_blocked_reminder" if reminder else "admission_blocked"
            )
            details = dict(
                error=evidence["error"],
                existing_claims=sorted(item["ticket"] for item in claims),
                incident_sha256=digest,
                reason_code=evidence["reason_code"],
            )
            if "ticket" in evidence:
                self.event(name, evidence["ticket"], **details)
            else:
                self.event(name, **details)

    def clear_admission_failure(self) -> None:
        if not self.admission_refusals:
            (self.state / "admission-incident.json").unlink(missing_ok=True)

    def record_dispatch_refusal(
        self, refusal: Any, claims: list[dict[str, Any]]
    ) -> None:
        errors = {
            "initiative_missing": "ticket initiative is missing",
            "invalid_ticket_contract": "ticket dependencies are invalid",
        }
        if (
            not isinstance(refusal, dict)
            or set(refusal) != {"error", "reason_code", "ticket"}
            or errors.get(refusal.get("reason_code")) != refusal.get("error")
            or not TICKET.fullmatch(refusal.get("ticket", ""))
        ):
            raise ControllerError("dispatch admission refusal is malformed")
        ticket = refusal["ticket"]
        if ticket in self.admission_refusals:
            return
        result = {**refusal, "status": "skipped"}
        self.admission_refusals[ticket] = result
        self.record_admission_failure(
            ControllerError(canonical(refusal)), claims
        )

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

    @staticmethod
    def preflight_refusal_evidence(value: dict[str, Any]) -> dict[str, Any]:
        output = value.get("output")
        exit_code = value.get("exit_code")
        if (
            value.get("status") != "error"
            or isinstance(exit_code, bool)
            or not isinstance(exit_code, int)
            or not 1 <= exit_code <= 255
            or not isinstance(output, str)
            or len(output.encode("utf-8")) > 1_048_576
        ):
            raise ControllerError("preflight refusal evidence is malformed or oversized")
        lines = []
        for raw in output.replace("\x00", "").splitlines():
            line = raw.strip()
            if not re.search(r"(?:^|\b)(?:FAIL:|PREFLIGHT FAIL|READINESS BLOCKED:)", line):
                continue
            line = re.sub(
                r"(?i)([A-Za-z0-9_.-]*(?:key|token|secret|password|auth)"
                r"[A-Za-z0-9_.-]*\s*[:=]\s*)\S+",
                r"\1[redacted]", line,
            )
            line = re.sub(
                r"(?i)\b[A-Za-z][A-Za-z0-9+.-]*://\S+", "[redacted-url]", line,
            )
            lines.append(line[:500])
            if len(lines) == 8:
                break
        return {
            "preflight_exit_code": exit_code,
            "preflight_failure_lines": lines,
            "preflight_output_sha256": hashlib.sha256(output.encode()).hexdigest(),
            "preflight_reason_code": "deterministic_refusal" if lines else "unclassified_refusal",
        }

    def claim_path(self, ticket: str) -> Path:
        return self.claims / f"{ticket}.json"

    def pause_path(self, ticket: str) -> Path:
        return self.state / f"pause-{ticket}.json"

    @staticmethod
    def resume_state(worktree: str, ticket: str, current_state: str) -> str | None:
        path = Path(worktree) / "factory" / "tickets" / f"{ticket}.md"
        values = re.findall(
            r"^Resume-State:\s*(.*?)\s*$",
            path.read_text(encoding="utf-8"), re.I | re.M,
        )
        if len(values) > 1 or (
            current_state == "Blocked-Escalated"
            and (
                len(values) != 1
                or values[0] not in {
                    "Backlog", "Ready", "Planning", "Building", "Review",
                }
            )
        ):
            raise ControllerError("ticket Resume-State is invalid")
        return values[0] if values else None

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

    def worktrees_by_branch(self) -> dict[str, list[str]]:
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
        return records

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
            if "admission_refusal" in value:
                self.record_dispatch_refusal(value["admission_refusal"], claims)
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

    def product_ticket_done(self, ticket: str) -> bool:
        if self.qualification and ticket in self.qualification["tickets"]:
            protected = subprocess.run(
                [
                    "git", "-C", str(self.product), "show",
                    f"refs/remotes/origin/main:factory/tickets/{ticket}.md",
                ],
                text=True, capture_output=True, check=False, timeout=120,
            )
            if protected.returncode == 0:
                states = re.findall(
                    r"^State:\s*(.*?)\s*$", protected.stdout, re.I | re.M,
                )
                if len(states) == 1 and states[0].casefold() == "done":
                    self.qualification_protected_terminal(ticket)
                    return True
                return False
        try:
            text = (
                self.product / "factory" / "tickets" / f"{ticket}.md"
            ).read_text(encoding="utf-8")
        except (FileNotFoundError, OSError):
            return False
        states = re.findall(r"^State:\s*(.*?)\s*$", text, re.I | re.M)
        return len(states) == 1 and states[0].casefold() == "done"

    def recover_missing_passport_claims(
        self, claims: list[dict[str, Any]]
    ) -> None:
        if not self.qualification:
            return
        targets = [
            ticket for ticket in self.qualification["tickets"]
            if not self.product_ticket_done(ticket)
        ]
        if not targets:
            return
        passports = self.state / "passports"
        if not passports.is_dir():
            return
        claimed = {item["ticket"] for item in claims}
        records = self.worktrees_by_branch()
        for ticket in targets:
            path = passports / f"{ticket}.json"
            if (
                ticket in claimed or not path.exists() or self.active_run(ticket)
                or self.pause_path(ticket).exists()
            ):
                continue
            passport = read(path)
            branch = f"ticket/{ticket}"
            worktrees = records.get(f"refs/heads/{branch}", [])
            if (
                passport.get("ticket") != ticket
                or passport.get("branch") != branch
                or passport.get("current_state") not in INFLIGHT_STATES
                or passport.get("current_state") == "Blocked-Escalated"
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

    def settled_contract_blocker(
        self, claim: dict[str, Any]
    ) -> dict[str, str] | None:
        receipt = claim.get("receipt", "")
        role = claim.get("role", "")
        if (
            claim.get("status") != "blocked"
            or not DIGEST.fullmatch(receipt)
            or role not in {"planner", "spec-linter", "test-author", "builder"}
            or claim.get("publication_lease")
            or self.role_active(claim)
        ):
            return None
        terminal = self.terminal_for_receipt(claim["ticket"], receipt)
        if (
            terminal is None
            or terminal.get("role") != role
            or terminal.get("role_exit") != "role_exit_contract_blocked"
            or terminal.get("exit_status") != "12"
            or terminal.get("task_submitted") != "1"
        ):
            return None
        return terminal

    def pause_ticket(self, ticket: str, blocking_issue: str) -> dict[str, Any]:
        if not TICKET.fullmatch(ticket) or not FACTORY_ISSUE.fullmatch(blocking_issue):
            raise ControllerError("ticket pause requires a Software Factory issue URL")
        path = self.state / "passports" / f"{ticket}.json"
        if not path.exists() or self.active_run(ticket):
            raise ControllerError("ticket pause requires an idle passport")
        claims = {item["ticket"]: item for item in self.load_claims()}
        claim = claims.get(ticket)
        settled_blocker = self.settled_contract_blocker(claim) if claim else None
        if (
            claim
            and (claim.get("receipt") or claim.get("role"))
            and settled_blocker is None
        ):
            raise ControllerError("ticket pause requires a pre-provider boundary")
        passport = read(path)
        current_state = passport.get("current_state")
        if (
            current_state not in INFLIGHT_STATES
            or passport.get("publication_state") == "merged"
            or self.product_ticket_done(ticket)
        ):
            raise ControllerError("ticket pause requires an in-flight passport")
        existing_intent = (
            read(self.pause_path(ticket)) if self.pause_path(ticket).exists() else None
        )
        branch = passport.get("branch", "")
        worktrees = self.worktrees_by_branch().get(f"refs/heads/{branch}", [])
        if (
            passport.get("ticket") != ticket
            or branch != f"ticket/{ticket}"
            or not SHA.fullmatch(passport.get("head_sha", ""))
            or not DIGEST.fullmatch(passport.get("passport_sha256", ""))
            or len(worktrees) != 1
        ):
            raise ControllerError("ticket pause passport is ambiguous")
        if claim:
            worktree = claim["worktree"]
            status = claim["status"]
            budget = claim.get("budget_sha256", "")
        else:
            worktree = (
                existing_intent.get("worktree") if existing_intent else worktrees[0]
            )
            status = (
                existing_intent.get("status") if existing_intent else
                "blocked" if current_state == "Blocked-Escalated"
                else "claimed"
            )
            budget = existing_intent.get("budget_sha256") if existing_intent else ""
        probe = {
            "branch": branch, "ticket": ticket, "worktree": worktree,
        }
        if not self.remote_passport_valid(probe):
            if settled_blocker is None:
                raise ControllerError("ticket pause passport is not portable")
            self.migrate_passport(claim, "preserve")
            passport = read(path)
            current_state = passport.get("current_state")
            branch = passport.get("branch", "")
            worktrees = self.worktrees_by_branch().get(
                f"refs/heads/{branch}", []
            )
            if (
                current_state not in INFLIGHT_STATES
                or passport.get("publication_state") == "merged"
                or self.product_ticket_done(ticket)
                or passport.get("ticket") != ticket
                or branch != f"ticket/{ticket}"
                or not SHA.fullmatch(passport.get("head_sha", ""))
                or not DIGEST.fullmatch(passport.get("passport_sha256", ""))
                or len(worktrees) != 1
            ):
                raise ControllerError("ticket pause passport is not portable")
            worktree = claim["worktree"]
            probe = {
                "branch": branch, "ticket": ticket, "worktree": worktree,
            }
            if not self.remote_passport_valid(probe):
                raise ControllerError("ticket pause passport is not portable")
        if claim:
            if claim.get("publication_lease"):
                self.withdraw_publication(claim)
            if not self.park_claim(claim):
                raise ControllerError("ticket pause could not park a clean checkpoint")
            if DIGEST.fullmatch(claim.get("lease", "")):
                self.release_ticket_lease(claim)
            worktree = claim["worktree"]
        value = {
            "blocking_issue": blocking_issue,
            "branch": branch,
            "budget_sha256": budget or None,
            "created_at_epoch": (
                existing_intent.get("created_at_epoch")
                if existing_intent else int(time.time())
            ),
            "current_stage": passport.get("current_stage"),
            "current_state": current_state,
            "factory_sha": self.release_path.name,
            "head_sha": passport["head_sha"],
            "passport_sha256": passport["passport_sha256"],
            "passport_factory_sha": passport.get("factory_sha"),
            "resume_state": self.resume_state(worktree, ticket, current_state),
            "run_snapshot_sha256": self.ticket_run_snapshot(ticket),
            "schema": "nysa.software-factory.ticket-pause/v2",
            "status": status,
            "ticket": ticket,
            "worktree": worktree,
        }
        value["pause_sha256"] = hashlib.sha256(
            canonical(value).encode()
        ).hexdigest()
        destination = self.pause_path(ticket)
        if destination.exists():
            if read(destination) != value:
                raise ControllerError("ticket pause intent conflicts")
        else:
            write(destination, value)
        self.claim_path(ticket).unlink(missing_ok=True)
        self.event(
            "ticket_paused", ticket, blocking_issue=blocking_issue,
            head_sha=passport["head_sha"], pause_sha256=value["pause_sha256"],
        )
        return {"schema": SCHEMA, "status": "paused", "ticket": ticket}

    def resume_ticket(self, ticket: str, factory_sha: str) -> dict[str, Any]:
        path = self.pause_path(ticket)
        if (
            not TICKET.fullmatch(ticket)
            or not SHA.fullmatch(factory_sha)
            or factory_sha != self.release_path.name
            or not path.exists()
        ):
            raise ControllerError("ticket resume intent is unavailable")
        intent = read(path)
        intent_digest = intent.get("pause_sha256", "")
        signed_intent = dict(intent)
        signed_intent.pop("pause_sha256", None)
        legacy = intent.get("schema") == "nysa.software-factory.ticket-pause/v1"
        if not legacy and intent_digest != hashlib.sha256(
            canonical(signed_intent).encode()
        ).hexdigest():
            raise ControllerError("ticket pause intent digest is invalid")
        passport_path = self.state / "passports" / f"{ticket}.json"
        if not passport_path.exists() or self.active_run(ticket):
            raise ControllerError("ticket resume requires an idle passport")
        passport = read(passport_path)
        current_state = passport.get("current_state")
        if (
            current_state not in INFLIGHT_STATES
            or passport.get("publication_state") == "merged"
            or self.product_ticket_done(ticket)
        ):
            raise ControllerError("ticket resume requires an in-flight passport")
        lineage = passport.get("migration_history", [])
        authorized_passports = {passport.get("passport_sha256")}
        authorized_passports.update(
            item.get("from_passport_sha256")
            for item in lineage if isinstance(item, dict)
        )
        if (
            intent.get("schema") not in {
                "nysa.software-factory.ticket-pause/v1",
                "nysa.software-factory.ticket-pause/v2",
            }
            or intent.get("ticket") != ticket
            or intent.get("branch") != f"ticket/{ticket}"
            or intent.get("current_state") != current_state
            or intent.get("passport_sha256") not in authorized_passports
            or passport.get("ticket") != ticket
            or passport.get("branch") != intent.get("branch")
            or passport.get("head_sha") != intent.get("head_sha")
            or passport.get("factory_sha") != factory_sha
            or intent.get("worktree") not in self.worktrees_by_branch().get(
                f"refs/heads/{intent.get('branch')}", []
            )
            or self.claim_path(ticket).exists()
            or (
                not legacy
                and (
                    not FACTORY_ISSUE.fullmatch(intent.get("blocking_issue", ""))
                    or intent.get("current_stage") != passport.get("current_stage")
                    or intent.get("resume_state") != self.resume_state(
                        intent["worktree"], ticket, current_state
                    )
                    or not DIGEST.fullmatch(intent.get("run_snapshot_sha256", ""))
                    or intent.get("run_snapshot_sha256")
                    != self.ticket_run_snapshot(ticket)
                    or not DIGEST.fullmatch(intent_digest)
                )
            )
        ):
            raise ControllerError("ticket resume intent does not match the passport")
        archived: Path | None = None
        if not legacy:
            repros = self.state / "repros"
            safe_directory(repros, create=True)
            archived = repros / f"{ticket}-{intent_digest}.json"
            if archived.exists() and read(archived) != intent:
                raise ControllerError("ticket repro record conflicts")
        claim = {
            "branch": intent["branch"],
            "lease": "",
            "priority": "normal",
            "publication_lease": "",
            "receipt": "",
            "role": "",
            "schema": CLAIM_SCHEMA,
            "status": intent.get("status", "claimed"),
            "ticket": ticket,
            "worktree": intent["worktree"],
        }
        if Path(claim["worktree"]).parent.name == "parked":
            claim["parked"] = True
        if claim["status"] == "budget" and DIGEST.fullmatch(
            intent.get("budget_sha256") or ""
        ):
            claim["budget_sha256"] = intent["budget_sha256"]
        elif claim["status"] not in {"blocked", "claimed", "waiting"}:
            claim["status"] = "claimed"
        if not self.remote_passport_valid(claim):
            raise ControllerError("ticket resume passport is not portable")
        self.ensure_lease(claim, "paused-ticket-resume")
        if not self.ticket_release_current(claim):
            claim["status"] = "blocked"
        self.save_claim(claim)
        if legacy:
            path.unlink()
        else:
            if archived is None:
                raise ControllerError("ticket repro archive is unavailable")
            if archived.exists():
                path.unlink()
            else:
                os.replace(path, archived)
        self.event(
            "ticket_resumed", ticket,
            blocking_issue=intent.get("blocking_issue"),
            head_sha=passport["head_sha"],
            source_factory_sha=intent.get("factory_sha"),
            target_factory_sha=factory_sha,
        )
        return {"schema": SCHEMA, "status": "resumed", "ticket": ticket}

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
        claim["blocked_reason"] = reason
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

    def release_expired_successor_lease(self, claim: dict[str, Any]) -> bool:
        if (
            self.qualification is None
            or self.qualification.get("mode") != "successor"
            or not self.parked(claim)
            or claim.get("lease") != ""
            or claim.get("publication_lease")
            or self.role_active(claim)
            or not self.ticket_release_current(claim)
        ):
            return False
        try:
            if not self.remote_passport_valid(claim):
                return False
        except ControllerError:
            return False
        passport = read(
            self.state / "passports" / f"{claim['ticket']}.json"
        )
        if (
            passport.get("ticket") != claim["ticket"]
            or passport.get("branch") != claim["branch"]
            or passport.get("factory_sha") != self.release_path.name
        ):
            raise ControllerError("expired lease recovery passport is not exact")
        directory = self.product / "factory" / ".dispatch-leases"
        try:
            info = directory.lstat()
        except FileNotFoundError:
            return False
        if (
            directory.is_symlink()
            or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.geteuid()
        ):
            raise ControllerError("dispatcher lease state is unsafe")
        records: dict[str, dict[str, Any]] = {}
        lease_ids: set[str] = set()
        for path in sorted(directory.iterdir()):
            if not re.fullmatch(r"T-[0-9]+[.]json", path.name):
                raise ControllerError("dispatcher lease state is unsafe")
            value = read(path)
            ticket = value.get("ticket", "")
            lease_id = value.get("lease_id", "")
            claimed = value.get("claimed_epoch")
            expires = value.get("expires_epoch")
            if (
                set(value) != {
                    "schema_version", "ticket", "lease_id",
                    "claimed_epoch", "expires_epoch",
                }
                or value.get("schema_version") != 1
                or ticket != path.stem
                or not TICKET.fullmatch(ticket)
                or not DIGEST.fullmatch(lease_id)
                or ticket in records
                or lease_id in lease_ids
                or isinstance(claimed, bool)
                or not isinstance(claimed, int)
                or isinstance(expires, bool)
                or not isinstance(expires, int)
                or expires <= claimed
            ):
                raise ControllerError("dispatcher lease state is unsafe")
            records[ticket] = value
            lease_ids.add(lease_id)
        record = records.get(claim["ticket"])
        if record is None or record["expires_epoch"] > int(time.time()):
            return False
        result = self.json_call(
            "release-expired", "--ticket", claim["ticket"],
            "--lease", record["lease_id"],
        )
        if result != {
            "expired": True, "released": True, "ticket": claim["ticket"],
        }:
            raise ControllerError("expired dispatcher lease release is invalid")
        self.event_once(
            "expired_ticket_lease_released", claim["ticket"],
            expired_lease_sha256=hashlib.sha256(
                record["lease_id"].encode()
            ).hexdigest(),
        )
        return True

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
            parked_root = self.state / "parked" if self.qualification else root / "parked"
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
        root = (
            self.state / "cells" if self.qualification else source.parent.parent
        )
        if self.qualification and not root.exists():
            root.mkdir(mode=0o700)
        root = safe_directory(root)
        if (
            source.parent != (
                self.state / "parked" if self.qualification else root / "parked"
            )
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

    def archive_emergency_admission(
        self, claim: dict[str, Any], terminal: dict[str, str]
    ) -> None:
        if not (
            self.state / "emergency-admissions" / claim["ticket"]
        ).is_dir():
            return
        value = self.json_call(
            "emergency-admit", "archive", "--ticket", claim["ticket"],
            "--role", claim["role"], "--receipt", claim["receipt"],
            "--workdir", claim["worktree"], "--json",
        )
        if value.get("status") == "absent":
            return
        if (
            value.get("action") != "archive"
            or value.get("status") != "archived"
            or value.get("ticket") != claim["ticket"]
            or not DIGEST.fullmatch(value.get("approval_sha256", ""))
            or not DIGEST.fullmatch(value.get("record_sha256", ""))
        ):
            raise ControllerError("emergency admission archive is invalid")
        self.event_once(
            "emergency_admission_archived", claim["ticket"],
            approval_sha256=value["approval_sha256"],
            archive_sha256=value["record_sha256"],
            role=claim["role"], run_id=terminal.get("run_id"),
            transition_receipt_sha256=claim["receipt"],
        )

    def migrate_passport(
        self, claim: dict[str, Any], publication: str
    ) -> dict[str, Any]:
        path = self.state / "passports" / f"{claim['ticket']}.json"
        if not path.exists():
            return {}
        return self.json_call(
            "passport", "migrate", "--ticket", claim["ticket"],
            "--publication-state", publication,
            "--workdir", claim["worktree"], "--json",
        )

    def correct_converged_success(
        self, claim: dict[str, Any], terminal: dict[str, str]
    ) -> None:
        result = self.json_call(
            "passport", "correct-converged-success",
            "--ticket", claim["ticket"],
            "--receipt", claim["receipt"],
            "--run-id", terminal["run_id"],
            "--workdir", claim["worktree"], "--json",
        )
        if result.get("status") != "ok":
            raise ControllerError("passport completion correction failed")

    def restore_model_identity_success(
        self, claim: dict[str, Any], terminal: dict[str, str]
    ) -> None:
        evidence = self.json_call(
            "passport", "verify-model-identity-success",
            "--ticket", claim["ticket"], "--receipt", claim["receipt"],
            "--run-id", terminal["run_id"],
            "--workdir", claim["worktree"], "--json",
        )
        expected = {
            name: evidence.get(name) for name in (
                "input_head", "output_head", "output_tree", "restore_head",
                "revert_head", "recovery_status",
            )
        }
        if (
            evidence.get("schema") != SCHEMA
            or evidence.get("ticket") != claim["ticket"]
            or evidence.get("run_id") != terminal.get("run_id")
            or evidence.get("status") != "ok"
            or evidence.get("recovery_status")
            not in {"restore-required", "restored"}
            or any(
                not SHA.fullmatch(expected[name] or "")
                for name in (
                    "input_head", "output_head", "output_tree", "revert_head",
                )
            )
            or (
                evidence["recovery_status"] == "restored"
                and not SHA.fullmatch(expected["restore_head"] or "")
            )
            or (
                evidence["recovery_status"] == "restore-required"
                and expected["restore_head"] != ""
            )
        ):
            raise ControllerError("model identity recovery evidence is invalid")

        self.ensure_lease(claim, "model-identity-success-recovery")
        head_status, local_head, remote_head = self.remote_cell_head_status(claim)
        if evidence["recovery_status"] == "restore-required":
            if (
                local_head != evidence["revert_head"]
                or remote_head != evidence["input_head"]
                or head_status != "resume_commit_not_pushed"
            ):
                raise ControllerError("model identity recovery remote moved")
            with self.git_lock:
                restored = subprocess.run(
                    [
                        "git", "-C", claim["worktree"],
                        "-c", "user.name=Factory Controller",
                        "-c", "user.email=factory-controller@local",
                        "revert", "--no-edit", evidence["revert_head"],
                    ],
                    text=True, capture_output=True, check=False, timeout=120,
                )
                if restored.returncode:
                    subprocess.run(
                        ["git", "-C", claim["worktree"], "revert", "--abort"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        check=False, timeout=120,
                    )
                    raise ControllerError(
                        restored.stderr.strip()
                        or "model identity output restore failed"
                    )
            evidence = self.json_call(
                "passport", "verify-model-identity-success",
                "--ticket", claim["ticket"], "--receipt", claim["receipt"],
                "--run-id", terminal["run_id"],
                "--workdir", claim["worktree"], "--json",
            )
            if (
                evidence.get("status") != "ok"
                or evidence.get("recovery_status") != "restored"
            ):
                raise ControllerError("model identity output restore is invalid")

        head_status, local_head, remote_head = self.remote_cell_head_status(claim)
        if local_head != evidence.get("restore_head"):
            raise ControllerError("model identity recovery head changed")
        if remote_head == evidence.get("input_head"):
            if head_status != "resume_commit_not_pushed":
                raise ControllerError("model identity recovery ancestry is invalid")
            pushed = subprocess.run(
                [
                    "git", "-C", claim["worktree"], "push", "--no-force", "--",
                    "origin", f"{local_head}:refs/heads/{claim['branch']}",
                ],
                text=True, capture_output=True, check=False, timeout=120,
            )
            if pushed.returncode:
                raise ControllerError(
                    pushed.stderr.strip() or "model identity output push failed"
                )
        elif remote_head != local_head or head_status != "pushed":
            raise ControllerError("model identity recovery remote moved")
        if not self.remote_cell_head_valid(claim):
            raise ControllerError("model identity recovery push is unverified")

        publication = read(
            self.state / "passports" / f"{claim['ticket']}.json"
        ).get("publication_state", "")
        if publication not in {
            "none", "validating", "ready", "merge-pending", "merged", "repair",
        }:
            raise ControllerError("model identity recovery publication is invalid")
        migrated = self.migrate_passport(claim, "preserve")
        if migrated.get("status") != "ok":
            raise ControllerError("model identity recovery migration failed")
        if not self.terminal_already_exported(claim, terminal):
            self.passport(claim, publication)
        self.correct_converged_success(claim, terminal)

    def converged_success_exported(
        self, claim: dict[str, Any], terminal: dict[str, str]
    ) -> bool:
        value = read(self.state / "passports" / f"{claim['ticket']}.json")
        expected = (
            terminal.get("run_id"), claim.get("role"), claim.get("receipt"),
        )
        completed = [
            (
                item.get("run_id"), item.get("role"),
                item.get("transition_receipt_sha256"),
            )
            for item in value.get("completed_role_evidence", [])
            if isinstance(item, dict)
        ]
        corrections = [
            (
                item.get("run_id"), item.get("schema"),
                item.get("transition_receipt_sha256"),
                item.get("recovery_factory_sha"),
            )
            for item in value.get("completed_role_corrections", [])
            if isinstance(item, dict)
        ]
        return (
            completed.count(expected) == 1
            and corrections.count((
                terminal.get("run_id"), COMPLETION_CORRECTION_SCHEMA,
                claim.get("receipt"), self.release_path.name,
            )) == 1
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

    def remote_cell_head_status(
        self, claim: dict[str, Any]
    ) -> tuple[str, str, str]:
        head = subprocess.run(
            ["git", "-C", claim["worktree"], "rev-parse", "HEAD"],
            text=True, capture_output=True, check=False, timeout=120,
        ).stdout.strip()
        if not SHA.fullmatch(head):
            return "remote_unavailable", "", ""
        remote = subprocess.run(
            [
                "git", "-C", claim["worktree"], "ls-remote", "--exit-code",
                "origin", f"refs/heads/{claim['branch']}",
            ],
            text=True, capture_output=True, check=False, timeout=120,
        )
        if remote.returncode == 2 and not remote.stdout:
            return "resume_commit_not_pushed", head, ""
        match = re.fullmatch(
            rf"([0-9a-f]{{40}})\trefs/heads/{re.escape(claim['branch'])}\n",
            remote.stdout,
        )
        if remote.returncode != 0 or match is None:
            return "remote_unavailable", head, ""
        remote_head = match.group(1)
        if remote_head == head:
            return "pushed", head, remote_head
        ancestor = subprocess.run(
            [
                "git", "-C", claim["worktree"], "merge-base", "--is-ancestor",
                remote_head, head,
            ],
            text=True, capture_output=True, check=False, timeout=120,
        )
        reason = (
            "resume_commit_not_pushed"
            if ancestor.returncode == 0
            else (
                "resume_ancestry_invalid"
                if ancestor.returncode == 1
                else "remote_unavailable"
            )
        )
        return reason, head, remote_head

    def remote_cell_head_valid(self, claim: dict[str, Any]) -> bool:
        return self.remote_cell_head_status(claim)[0] == "pushed"

    @staticmethod
    def contract_resume_directive_status(ticket_text: str, receipt: str) -> str:
        attempts = re.findall(r"^OPERATOR RESUME(?: RECEIPT)?:", ticket_text, re.M)
        if not attempts:
            return "waiting"
        roles = re.findall(
            r"^OPERATOR RESUME: (planner|spec-linter|test-author|builder)$",
            ticket_text, re.M,
        )
        receipts = re.findall(
            r"^OPERATOR RESUME RECEIPT: ([0-9a-f]{64})$", ticket_text, re.M,
        )
        if len(roles) != 1 or len(receipts) != 1:
            return "resume_directives_ambiguous"
        if receipts[0] != receipt:
            return "resume_receipt_mismatch"
        return "ready"

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
            self.archive_emergency_admission(claim, terminal)
            claim["status"] = "running"
            self.save_claim(claim)
            self.event(
                "terminal_export_retried", claim["ticket"],
                run_id=terminal.get("run_id"),
            )

    def ticket_run_snapshot(self, ticket: str) -> str:
        selected = []
        for path in sorted((self.product / "factory/runs").glob("*.meta")):
            value = fields(path)
            if value.get("ticket") == ticket:
                selected.append((path.name, hashlib.sha256(path.read_bytes()).hexdigest()))
        return hashlib.sha256(canonical(selected).encode()).hexdigest()

    def reconciliation_marker(self, ticket: str) -> Path:
        return self.state / f"reconciling-{ticket}.json"

    def mark_reconciling(self, claim: dict[str, Any]) -> None:
        passport_path = self.state / "passports" / f"{claim['ticket']}.json"
        if claim.get("receipt") or claim.get("role") or not passport_path.exists():
            return
        passport = read(passport_path)
        value = {
            "branch": claim["branch"],
            "factory_sha": self.release_path.name,
            "head_sha": passport.get("head_sha"),
            "passport_sha256": passport.get("passport_sha256"),
            "run_snapshot_sha256": self.ticket_run_snapshot(claim["ticket"]),
            "schema": "nysa.software-factory.reconciliation-boundary/v1",
            "ticket": claim["ticket"],
        }
        path = self.reconciliation_marker(claim["ticket"])
        if path.exists() and read(path) != value:
            raise ControllerError("ticket reconciliation boundary conflicts")
        if not path.exists():
            write(path, value)

    def recover_interrupted_claims(self, claims: list[dict[str, Any]]) -> None:
        for claim in claims:
            marker_path = self.reconciliation_marker(claim["ticket"])
            passport_path = self.state / "passports" / f"{claim['ticket']}.json"
            if (
                claim["status"] != "blocked"
                or claim.get("receipt")
                or claim.get("role")
                or claim.get("blocked_reason")
                or self.pause_path(claim["ticket"]).exists()
                or self.role_active(claim)
                or not marker_path.exists()
                or not passport_path.exists()
                or not self.ticket_release_current(claim)
            ):
                continue
            marker = read(marker_path)
            passport = read(passport_path)
            if marker != {
                "branch": claim["branch"],
                "factory_sha": self.release_path.name,
                "head_sha": passport.get("head_sha"),
                "passport_sha256": passport.get("passport_sha256"),
                "run_snapshot_sha256": self.ticket_run_snapshot(claim["ticket"]),
                "schema": "nysa.software-factory.reconciliation-boundary/v1",
                "ticket": claim["ticket"],
            }:
                continue
            worktree = Path(claim["worktree"])
            if subprocess.run(
                ["git", "-C", str(worktree), "status", "--porcelain=v1", "-z"],
                text=True, capture_output=True, check=True, timeout=120,
            ).stdout:
                continue
            try:
                if not self.remote_passport_valid(claim):
                    continue
                self.ensure_lease(claim, "interrupted-reconciliation")
            except ControllerError:
                continue
            claim.update(receipt="", role="", status="claimed")
            claim.pop("blocked_reason", None)
            self.save_claim(claim)
            marker_path.unlink()
            self.event("interrupted_claim_recovered", claim["ticket"])

    def recover_missing_terminals(self, claims: list[dict[str, Any]]) -> None:
        for claim in claims:
            if (
                claim.get("status") != "blocked"
                or claim.get("blocked_reason") != "missing-terminal"
                or not DIGEST.fullmatch(claim.get("receipt", ""))
                or claim.get("role") not in {
                    "planner", "spec-linter", "test-author", "builder",
                    "reviewer", "narrator",
                }
                or claim.get("lease_released") is not True
                or self.role_active(claim)
            ):
                continue
            receipt = claim["receipt"]
            terminal = self.terminal_for_receipt(claim["ticket"], receipt)
            if (
                terminal is None
                or terminal.get("role") != claim["role"]
                or terminal.get("kit_sha") != self.release_path.name
            ):
                continue
            self.ensure_lease(claim, "missing-terminal")
            self.finish_pending_run(claim)
            self.event(
                "missing_terminal_recovered", claim["ticket"],
                run_id=terminal.get("run_id"),
                transition_receipt_sha256=receipt,
            )

    def recover_terminal_requests(self, claims: list[dict[str, Any]]) -> None:
        for claim in claims:
            passport_path = self.state / "passports" / f"{claim['ticket']}.json"
            request_path = self.terminal_request_path(claim["ticket"])
            if (
                claim.get("status") != "blocked"
                or claim.get("blocked_reason") != "controller-error"
                or claim.get("receipt")
                or claim.get("role")
                or claim.get("publication_lease")
                or claim.get("parked") is not True
                or self.role_active(claim)
                or not passport_path.exists()
                or not request_path.exists()
            ):
                continue
            passport = read(passport_path)
            worktree = Path(claim["worktree"])
            if (
                passport.get("ticket") != claim["ticket"]
                or passport.get("publication_state") != "merged"
                or subprocess.run(
                    ["git", "-C", str(worktree), "status", "--porcelain=v1", "-z"],
                    text=True, capture_output=True, check=False, timeout=120,
                ).stdout
                or not self.remote_passport_valid(claim)
            ):
                continue
            closeout_branch = f"chore/{claim['ticket'].lower().replace('-', '')}-closeout"
            try:
                if self.terminal_request(
                    claim, closeout_branch, create=False
                ) is None:
                    continue
                self.ensure_lease(claim, "terminal-replay")
            except (ControllerError, OSError, subprocess.SubprocessError):
                continue
            claim.update(status="claimed")
            claim.pop("blocked_reason", None)
            self.save_claim(claim)
            self.event_once("terminal_replay_recovered", claim["ticket"])

    def recover_each(
        self,
        claims: list[dict[str, Any]],
        recovery: Any,
        name: str,
        concurrent: bool = False,
    ) -> None:
        def recover(claim: dict[str, Any]) -> None:
            try:
                recovery([claim])
            except (
                ControllerError,
                json.JSONDecodeError,
                OSError,
                subprocess.SubprocessError,
            ) as error:
                claim["status"] = "blocked"
                claim["blocked_reason"] = f"recovery:{name}"
                self.save_claim(claim)
                self.event(
                    "ticket_recovery_failed",
                    claim["ticket"],
                    error=str(error),
                    recovery=name,
                )

        if concurrent and len(claims) > 1:
            with ThreadPoolExecutor(
                max_workers=min(self.capacity, len(claims))
            ) as executor:
                list(executor.map(recover, claims))
        else:
            for claim in claims:
                recover(claim)

    def recover_preflight_blocks(self, claims: list[dict[str, Any]]) -> None:
        for claim in claims:
            receipt_path = self.state / f"{claim['ticket']}.json"
            passport_path = self.state / "passports" / f"{claim['ticket']}.json"
            if (
                (
                    self.qualification is not None
                    and claim["ticket"] not in self.qualification["tickets"]
                )
                or claim["status"] != "blocked"
                or claim.get("blocked_reason") != "preflight"
                or claim.get("receipt")
                or claim.get("role")
                or claim.get("publication_lease")
                or claim.get("lease_released") is not True
                or self.role_active(claim)
                or passport_path.exists()
                or passport_path.is_symlink()
                or not receipt_path.is_file()
                or receipt_path.is_symlink()
            ):
                continue
            receipt = read(receipt_path)
            if (
                receipt.get("schema")
                != "nysa.software-factory.transition-receipt/v1"
                or receipt.get("ticket") != claim["ticket"]
                or receipt.get("branch") != claim["branch"]
                or receipt.get("stage") != "RUN planner"
                or receipt.get("role") != "planner"
                or receipt.get("consumed") is not False
                or not DIGEST.fullmatch(receipt.get("receipt_sha256", ""))
                or any(
                    fields(path).get("ticket") == claim["ticket"]
                    for path in (self.product / "factory/runs").glob("*.meta")
                )
                or subprocess.run(
                    [
                        "git", "-C", claim["worktree"], "status",
                        "--porcelain=v1", "-z",
                    ],
                    text=True, capture_output=True, check=True, timeout=120,
                ).stdout
                or not self.ticket_release_current(claim)
                or not self.remote_cell_head_valid(claim)
            ):
                continue
            self.ensure_lease(claim, "preflight-retry")
            try:
                try:
                    if self.qualification:
                        ensure_qualification_artifacts(
                            self.product, self.state, claim["ticket"]
                        )
                except QualificationArtifactError as error:
                    raise ControllerError(str(error)) from error
                transition = self.json_call(
                    "state-machine", "--ticket", claim["ticket"],
                    "--lease", claim["lease"], "--workdir", claim["worktree"],
                    "--json", timeout=None,
                )
                if (
                    not valid_transition_evidence(transition, claim["ticket"])
                    or transition.get("role") != "planner"
                ):
                    raise ControllerError(
                        "preflight retry transition evidence is invalid"
                    )
                preflight = self.json_call(
                    "preflight", "--ticket", claim["ticket"],
                    "--role", "planner", "--lease", claim["lease"],
                    "--receipt", transition["receipt"],
                    "--workdir", claim["worktree"], "--json", allow=(0, 1),
                )
            except (
                ControllerError, json.JSONDecodeError, OSError,
                subprocess.SubprocessError,
            ):
                self.release_ticket_lease(claim)
                raise
            if preflight.get("status") != "ok" or preflight.get("exit_code") != 0:
                try:
                    evidence = self.preflight_refusal_evidence(preflight)
                except ControllerError as error:
                    self.event(
                        "preflight_refusal_invalid", claim["ticket"], error=str(error),
                        transition_receipt_sha256=transition["receipt"],
                    )
                    self.release_ticket_lease(claim)
                    claim["blocked_reason"] = "preflight-evidence"
                    self.save_claim(claim)
                    continue
                self.event(
                    "preflight_retry_blocked", claim["ticket"], **evidence,
                    transition_receipt_sha256=transition["receipt"],
                )
                self.release_ticket_lease(claim)
                continue
            claim.update(receipt="", role="", status="claimed")
            claim.pop("blocked_reason", None)
            self.save_claim(claim)
            self.event(
                "preflight_retry_recovered", claim["ticket"],
                transition_receipt_sha256=transition["receipt"],
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
                claim["blocked_reason"] = "route-migration-required"
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
                self.release_expired_successor_lease(claim)
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
            or role not in {"planner", "spec-linter", "test-author", "builder"}
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
            model_identity_success = (
                terminal is not None
                and claim.get("role") == "spec-linter"
                and terminal.get("role") == "spec-linter"
                and terminal.get("phase") == "completed"
                and terminal.get("accounting_state") == "abandoned_conservative"
                and terminal.get("go_issued") == "1"
                and terminal.get("task_submitted") == "1"
                and terminal.get("exit_status") == "9"
                and terminal.get("role_exit") == "provider_failed"
                and terminal.get("terminal_reason_code", "") == ""
                and terminal.get("adapter") == "cursor-anthropic"
                and terminal.get("route_id", "").startswith("cursor-")
                and terminal.get("role_branch_before") == claim.get("branch")
                and terminal.get("cost_basis") == "conservative_reservation"
                and terminal.get("effective_cost") == terminal.get("reserved_usd")
                and re.fullmatch(
                    r"(?:0|[1-9][0-9]{0,6})(?:\.[0-9]{1,18})?",
                    terminal.get("reserved_usd", ""),
                )
                and int(terminal["reserved_usd"].replace(".", "")) > 0
                and SHA.fullmatch(terminal.get("role_head_before", ""))
                and SHA.fullmatch(terminal.get("kit_sha", ""))
                and terminal["kit_sha"] != self.release_path.name
                and DIGEST.fullmatch(terminal.get("output_sha256", ""))
                and terminal.get("progress_events", "").isdigit()
                and int(terminal["progress_events"]) > 0
                and DIGEST.fullmatch(
                    terminal.get("progress_journal_sha256", "")
                )
                and re.fullmatch(
                    r"[A-Za-z0-9._:-]{1,200}",
                    terminal.get("provider_attempt_id", ""),
                )
                and re.fullmatch(
                    r"[A-Za-z0-9._-]{1,200}", terminal.get("run_id", ""),
                )
            )
            if (
                self.qualification
                and terminal is not None
                and terminal.get("task_submitted") == "1"
                and terminal.get("role_exit") == "provider_failed"
                and terminal.get("route_id", "").startswith("cursor-")
                and not model_identity_success
            ):
                self.ensure_lease(claim, "provider-fallback-recovery")
                self.finish_pending_run(claim)
                continue
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
            converged_success = (
                terminal is not None
                and claim.get("role") == "builder"
                and terminal.get("role") == "builder"
                and terminal.get("phase") == "abandoned"
                and terminal.get("accounting_state") == "abandoned_conservative"
                and terminal.get("go_issued") == "1"
                and terminal.get("task_submitted") == "1"
                and terminal.get("exit_status") == "128"
                and terminal.get("role_exit") == ""
                and terminal.get("terminal_reason_code", "") == ""
                and terminal.get("adapter") in {
                    "cursor-anthropic", "cursor-openai",
                }
                and terminal.get("contract_version") == "1.8.0"
                and terminal.get("role_branch_before") == claim.get("branch")
                and terminal.get("cost_basis") == "conservative_reservation"
                and terminal.get("effective_cost") == terminal.get("reserved_usd")
                and re.fullmatch(
                    r"(?:0|[1-9][0-9]{0,6})(?:\.[0-9]{1,18})?",
                    terminal.get("reserved_usd", ""),
                )
                and int(terminal["reserved_usd"].replace(".", "")) > 0
                and SHA.fullmatch(terminal.get("role_head_before", ""))
                and SHA.fullmatch(terminal.get("kit_sha", ""))
                and terminal["kit_sha"] != self.release_path.name
                and DIGEST.fullmatch(terminal.get("output_sha256", ""))
                and terminal.get("progress_events", "") == ""
                and terminal.get("progress_journal_sha256", "") == ""
                and re.fullmatch(
                    r"[A-Za-z0-9._:-]{1,200}",
                    terminal.get("provider_attempt_id", ""),
                )
                and re.fullmatch(
                    r"[A-Za-z0-9._-]{1,200}", terminal.get("run_id", ""),
                )
            )
            if not (
                push_failure or interrupted_before_submission or contract_blocked
                or submission_unconfirmed or history_rewrite
                or converged_success or model_identity_success
            ):
                continue
            passport_path = self.state / "passports" / f"{claim['ticket']}.json"
            if not passport_path.exists():
                continue
            if contract_blocked:
                migrated = False
                try:
                    passport_valid = self.remote_passport_valid(claim)
                except ControllerError:
                    passport_valid = False
                if not passport_valid:
                    try:
                        head_status, local_head, remote_head = (
                            self.remote_cell_head_status(claim)
                        )
                        if head_status != "pushed":
                            try:
                                ticket_text = (
                                    Path(claim["worktree"]) / "factory" / "tickets"
                                    / f"{claim['ticket']}.md"
                                ).read_text(encoding="utf-8")
                            except (FileNotFoundError, OSError):
                                ticket_text = ""
                            if (
                                head_status in CONTRACT_RESUME_REFUSALS
                                and self.contract_resume_directive_status(
                                    ticket_text, claim["receipt"]
                                ) != "waiting"
                            ):
                                self.record_contract_resume_refusal(
                                    claim, head_status, {
                                        "local_head": local_head,
                                        "remote_head": remote_head or None,
                                    },
                                )
                            continue
                        self.migrate_passport(claim, "preserve")
                        migrated = True
                        if not self.remote_passport_valid(claim):
                            continue
                    except (
                        ControllerError, json.JSONDecodeError, OSError,
                        subprocess.SubprocessError,
                    ):
                        continue
                if migrated:
                    self.event(
                        "contract_block_passport_migrated", claim["ticket"],
                        failed_run_id=terminal.get("run_id"),
                    )
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
            if model_identity_success:
                try:
                    self.restore_model_identity_success(claim, terminal)
                    if (
                        not self.remote_passport_valid(claim)
                        or not self.converged_success_exported(claim, terminal)
                    ):
                        continue
                except (
                    ControllerError, json.JSONDecodeError, OSError,
                    subprocess.SubprocessError,
                ):
                    continue
            if converged_success:
                try:
                    if not self.remote_passport_valid(claim):
                        continue
                    self.correct_converged_success(claim, terminal)
                    if (
                        not self.remote_passport_valid(claim)
                        or not self.converged_success_exported(claim, terminal)
                    ):
                        continue
                except (
                    ControllerError, json.JSONDecodeError, OSError,
                    subprocess.SubprocessError,
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
                try:
                    ticket_text = (
                        Path(claim["worktree"]) / "factory" / "tickets"
                        / f"{claim['ticket']}.md"
                    ).read_text(encoding="utf-8")
                except (FileNotFoundError, OSError):
                    continue
                directive_status = self.contract_resume_directive_status(
                    ticket_text, claim["receipt"]
                )
                if directive_status == "waiting":
                    continue
                if directive_status != "ready":
                    self.record_contract_resume_refusal(
                        claim, directive_status, {}
                    )
                    continue
                head_status, local_head, remote_head = self.remote_cell_head_status(
                    claim
                )
                if head_status != "pushed":
                    if head_status in CONTRACT_RESUME_REFUSALS:
                        self.record_contract_resume_refusal(
                            claim, head_status, {
                                "local_head": local_head,
                                "remote_head": remote_head or None,
                            },
                        )
                    continue
                resumed = self.json_call(
                    "state-machine", "resume", "--ticket", claim["ticket"],
                    "--receipt", claim["receipt"],
                    "--workdir", claim["worktree"], "--json",
                    allow=(0, 1),
                )
                if resumed.get("status") == "error":
                    reason_code = resumed.get("reason_code")
                    if reason_code in CONTRACT_RESUME_REFUSALS:
                        self.record_contract_resume_refusal(
                            claim, reason_code, {
                                key: resumed[key]
                                for key in (
                                    "actual_bytes", "changed_path_count",
                                    "expected_bytes", "first_differing_line",
                                )
                                if key in resumed
                            },
                        )
                        continue
                if resumed.get("status") == "waiting":
                    continue
                if resumed.get("status") != "ready":
                    raise ControllerError(
                        "state machine returned an invalid contract resume"
                    )
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
                                else (
                                    "model_identity_success_recovered_by_release_upgrade"
                                    if model_identity_success
                                    else (
                                        "converged_success_recovered_by_release_upgrade"
                                        if converged_success
                                        else "interrupted_role_recovered"
                                    )
                                )
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
            claim["blocked_reason"] = "pre-go-failure"
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
            and terminal.get("task_submitted") == "1"
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
            self.archive_emergency_admission(claim, terminal)
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
            claim["blocked_reason"] = "role-failure"
            self.save_claim(claim)
            self.release_ticket_lease(claim)
            self.event("role_blocked", claim["ticket"], role=claim["role"])
            return False
        if claim["role"] == "reviewer":
            self.json_call(
                "ticket-state", "--ticket", claim["ticket"],
                "--workdir", claim["worktree"],
                "--action", "reviewer-reconcile", "--json",
            )
            self.migrate_passport(claim, "validating")
        self.relocate_qualification_cell(claim)
        claim.update(receipt="", role="", status="claimed")
        self.save_claim(claim)
        return True

    def ticket_pr(self, claim: dict[str, Any], receipt: str) -> dict[str, Any]:
        return self.json_call(
            "ticket-pr", "--ticket", claim["ticket"], "--lease", claim["lease"],
            "--receipt", receipt, "--workdir", claim["worktree"], "--json",
        )

    def wait_for_preview_identity(
        self, claim: dict[str, Any], pr: dict[str, Any]
    ) -> bool:
        identity = pr.get("preview_identity")
        preflight = pr.get("preview_preflight")
        head = pr.get("head", "")
        identity_wait = (
            isinstance(identity, dict)
            and identity.get("status") == "wait"
            and identity.get("expected") == head
        )
        preflight_wait = (
            isinstance(preflight, dict)
            and preflight.get("status") == "wait"
            and preflight.get("head") == head
        )
        if (
            not (identity_wait or preflight_wait)
            or not SHA.fullmatch(head)
        ):
            raise ControllerError("preview identity wait evidence is invalid")
        now = int(time.time())
        if claim.get("preview_wait_head") != head:
            claim["preview_wait_head"] = head
            claim["preview_wait_started_epoch"] = now
            self.event_once("preview_identity_wait", claim["ticket"], expected=head)
        started = claim.get("preview_wait_started_epoch")
        if isinstance(started, bool) or not isinstance(started, int) or started > now:
            raise ControllerError("preview identity wait boundary is invalid")
        if now - started >= PREVIEW_IDENTITY_WAIT_SECONDS:
            self.block(claim, "preview-identity-timeout")
            self.event_once(
                "preview_identity_timeout", claim["ticket"], expected=head,
                observed=(
                    preflight.get("evidence", {})
                    if preflight_wait else identity.get("observed", [])
                ),
            )
            return False
        self.save_claim(claim)
        return True

    def clear_preview_identity_wait(self, claim: dict[str, Any]) -> None:
        if "preview_wait_head" not in claim:
            return
        claim.pop("preview_wait_head", None)
        claim.pop("preview_wait_started_epoch", None)
        self.save_claim(claim)

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

    def request_protected_auto_merge(
        self, claim: dict[str, Any], receipt: str, pr: dict[str, Any]
    ) -> bool:
        if not self.publication_ready(claim, receipt, pr["head"]):
            return False
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
        return True

    def project_repository(self) -> str:
        values = re.findall(
            r"^(?:export\s+)?GH_REPO\s*=\s*['\"]?([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)['\"]?\s*$",
            (self.product / "factory/PROJECT.env").read_text(encoding="utf-8"),
            re.M,
        )
        if len(values) != 1:
            raise ControllerError("GH_REPO is missing or ambiguous")
        return values[0]

    def approval_pr_number(self, claim: dict[str, Any]) -> int:
        path = (
            Path(claim["worktree"]) / "factory" / "attestations"
            / claim["ticket"] / "approval.json"
        )
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ControllerError("approval PR identity is unavailable") from error
        if not isinstance(value, dict):
            raise ControllerError("approval PR identity is malformed")
        number = value.get("pr_number")
        if (
            value.get("schema") != "nysa.software-factory.ticket-approval/v1"
            or value.get("ticket") != claim["ticket"]
            or value.get("repository") != self.project_repository()
            or value.get("branch") != claim["branch"]
            or isinstance(number, bool)
            or not isinstance(number, int)
            or number <= 0
        ):
            raise ControllerError("approval PR identity is malformed")
        return number

    def merged_pr_identity(
        self, branch: str, number: int | None = None,
    ) -> dict[str, Any] | None:
        command = (
            ["gh", "pr", "view", str(number), "--repo", self.project_repository()]
            if number is not None else
            [
                "gh", "pr", "list", "--repo", self.project_repository(),
                "--state", "merged", "--head", branch,
            ]
        )
        result = subprocess.run(
            [
                *command, "--json",
                "number,headRefName,baseRefName,headRefOid,mergeCommit,state,mergedAt",
            ],
            text=True, capture_output=True, check=False, timeout=120,
        )
        if result.returncode:
            raise ControllerError(result.stderr.strip() or "GitHub merge query failed")
        try:
            evidence = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise ControllerError("GitHub merge query returned malformed evidence") from error
        values = [evidence] if number is not None else evidence
        if not values:
            return None
        if not isinstance(values, list) or len(values) != 1:
            raise ControllerError("merged PR identity is ambiguous")
        value = values[0]
        if not isinstance(value, dict):
            raise ControllerError("merged PR identity is malformed")
        merge = value.get("mergeCommit")
        if (
            number is not None and value.get("number") != number
            or value.get("headRefName") != branch
            or value.get("baseRefName") != "main"
            or isinstance(value.get("number"), bool)
            or not isinstance(value.get("number"), int)
            or value["number"] <= 0
            or not SHA.fullmatch(value.get("headRefOid", ""))
        ):
            raise ControllerError("merged PR identity is malformed")
        if value.get("state") != "MERGED":
            if not value.get("mergedAt") and merge is None:
                return None
            raise ControllerError("merged PR identity is malformed")
        if (
            not value.get("mergedAt")
            or not isinstance(merge, dict)
            or not SHA.fullmatch(merge.get("oid", ""))
        ):
            raise ControllerError("merged PR identity is malformed")
        return {
            "head": value["headRefOid"],
            "merge_commit": merge["oid"],
            "number": value["number"],
        }

    def terminal_request_path(self, ticket: str) -> Path:
        return self.state / f"terminal-request-{ticket}.json"

    def terminal_request_main_is_safe(
        self, request: dict[str, Any], current: str,
    ) -> bool:
        expected = request.get("protected_main", "")
        if expected == current:
            return True
        if (
            request.get("schema")
            != "nysa.software-factory.terminal-request/v2"
            or not SHA.fullmatch(expected)
            or not SHA.fullmatch(request.get("protected_ticket_blob", ""))
        ):
            return False
        with self.git_lock:
            subprocess.run(
                [
                    "git", "-C", str(self.product), "fetch", "--quiet",
                    "--no-tags", "origin", "+refs/heads/main:refs/remotes/origin/main",
                ],
                check=True, timeout=120,
            )
            commits = [
                expected,
                request.get("implementation_pr", {}).get("merge_commit", ""),
                request.get("closeout_pr", {}).get("merge_commit", ""),
            ]
            if any(
                not SHA.fullmatch(commit)
                or subprocess.run(
                    [
                        "git", "-C", str(self.product), "merge-base",
                        "--is-ancestor", commit, current,
                    ],
                    check=False, timeout=120,
                ).returncode
                for commit in commits
            ):
                return False
            path = f"factory/tickets/{request['ticket']}.md"
            blobs = [
                subprocess.run(
                    ["git", "-C", str(self.product), "rev-parse", f"{commit}:{path}"],
                    text=True, capture_output=True, check=True, timeout=120,
                ).stdout.strip()
                for commit in (expected, current)
            ]
        return blobs == [request["protected_ticket_blob"]] * 2

    def terminal_request(
        self, claim: dict[str, Any], closeout_branch: str, create: bool,
    ) -> dict[str, Any] | None:
        branch = claim.get("branch", "")
        passport_path = self.state / "passports" / f"{claim['ticket']}.json"
        if branch != f"ticket/{claim['ticket']}" or not passport_path.exists():
            return None
        implementation = self.merged_pr_identity(
            branch, self.approval_pr_number(claim),
        )
        closeout = self.merged_pr_identity(closeout_branch)
        if implementation is None or closeout is None:
            return None
        passport = read(passport_path)
        if (
            passport.get("ticket") != claim["ticket"]
            or passport.get("branch") != branch
            or passport.get("factory_sha") != self.release_path.name
            or passport.get("publication_state") != "merged"
            or not DIGEST.fullmatch(passport.get("passport_sha256", ""))
        ):
            raise ControllerError("terminal request passport is invalid")
        remote = subprocess.run(
            [
                "git", "-C", str(self.product), "ls-remote", "--heads", "origin",
                "refs/heads/main",
            ],
            text=True, capture_output=True, check=False, timeout=120,
        )
        fields = remote.stdout.split()
        if remote.returncode or len(fields) != 2 or not SHA.fullmatch(fields[0]):
            raise ControllerError("protected main terminal expectation is unavailable")
        with self.git_lock:
            subprocess.run(
                [
                    "git", "-C", str(self.product), "fetch", "--quiet",
                    "--no-tags", "origin", "+refs/heads/main:refs/remotes/origin/main",
                ],
                check=True, timeout=120,
            )
            ticket_blob = subprocess.run(
                [
                    "git", "-C", str(self.product), "rev-parse",
                    f"{fields[0]}:factory/tickets/{claim['ticket']}.md",
                ],
                text=True, capture_output=True, check=True, timeout=120,
            ).stdout.strip()
        if not SHA.fullmatch(ticket_blob):
            raise ControllerError("protected terminal ticket is unavailable")
        value = {
            "action": "done",
            "branch": branch,
            "closeout_pr": closeout,
            "factory_sha": self.release_path.name,
            "implementation_pr": implementation,
            "passport_sha256": passport.get("passport_sha256"),
            "protected_main": fields[0],
            "protected_ticket_blob": ticket_blob,
            "schema": "nysa.software-factory.terminal-request/v2",
            "ticket": claim["ticket"],
        }
        value["request_sha256"] = hashlib.sha256(
            canonical(value).encode()
        ).hexdigest()
        path = self.terminal_request_path(claim["ticket"])
        if path.exists():
            existing = read(path)
            existing_digest = existing.get("request_sha256", "")
            existing_payload = dict(existing)
            existing_payload.pop("request_sha256", None)
            stable = {"protected_main", "request_sha256"}
            if (
                existing_digest
                != hashlib.sha256(canonical(existing_payload).encode()).hexdigest()
                or {key: item for key, item in existing.items() if key not in stable}
                != {key: item for key, item in value.items() if key not in stable}
                or not self.terminal_request_main_is_safe(existing, fields[0])
            ):
                raise ControllerError("terminal request identity changed")
            return existing
        elif create:
            write(path, value)
        else:
            return None
        return value

    def ticket_merged(self, claim: dict[str, Any]) -> bool:
        return self.merged_pr_identity(
            claim["branch"], self.approval_pr_number(claim),
        ) is not None

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
        self.terminal_request(claim, branch, create=True)
        try:
            value = self.json_call(
                "ticket-attest", "--ticket", ticket, "--lease", claim["lease"],
                "--workdir", str(worktree), "--action", "done", "--json",
            )
        except ControllerError as error:
            detail = str(error).removeprefix("ticket-attest: ")
            if detail.startswith((
                "required post-merge check is missing: ",
                "required post-merge check is pending: ",
            )):
                self.event("post_merge_check_wait", ticket, reason=detail)
                return False
            raise
        if value.get("closeout_pr_state") != "MERGED":
            return False
        terminal = value.get("terminal")
        linear = terminal.get("linear") if isinstance(terminal, dict) else None
        if (
            not isinstance(terminal, dict)
            or terminal.get("basis") not in {
                "attested-done", "attested-emergency-closeout",
            }
            or not SHA.fullmatch(terminal.get("protected_main", ""))
            or not isinstance(linear, dict)
            or linear.get("state") != "Done"
            or linear.get("source_ref") != "refs/remotes/origin/main"
            or not isinstance(linear.get("identifier"), str)
            or not linear["identifier"]
            or not isinstance(linear.get("issue_id"), str)
            or not linear["issue_id"]
            or not isinstance(linear.get("state_id"), str)
            or not linear["state_id"]
        ):
            raise ControllerError("closeout lacks exact protected terminal Linear evidence")
        self.event_once(
            "linear_terminal_synced", ticket,
            linear_identifier=linear["identifier"],
            linear_issue_id=linear["issue_id"],
            linear_state_id=linear["state_id"],
            protected_main=terminal["protected_main"],
            terminal_basis=terminal["basis"],
        )
        return True

    def run_role(
        self, claim: dict[str, Any], role: str, receipt: str,
        failed_checks: list[str], publication: dict[str, Any] | None = None,
    ) -> None:
        self.ensure_execution_cell(claim)
        if self.qualification:
            try:
                ensure_qualification_artifacts(
                    self.product, self.state, claim["ticket"]
                )
            except QualificationArtifactError as error:
                raise ControllerError(str(error)) from error
        if role == "planner":
            preflight = self.json_call(
                "preflight", "--ticket", claim["ticket"], "--role", role,
                "--lease", claim["lease"], "--receipt", receipt,
                "--workdir", claim["worktree"], "--json",
                allow=(0, 1),
            )
            if preflight.get("status") != "ok" or preflight.get("exit_code") != 0:
                try:
                    evidence = self.preflight_refusal_evidence(preflight)
                except ControllerError as error:
                    self.event(
                        "preflight_refusal_invalid", claim["ticket"], error=str(error),
                        transition_receipt_sha256=receipt,
                    )
                    self.block(claim, "preflight-evidence")
                    return
                self.event(
                    "preflight_refused", claim["ticket"], **evidence,
                    transition_receipt_sha256=receipt,
                )
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
            claim["blocked_reason"] = "missing-terminal"
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
            passport_path = (
                self.state / "passports" / f"{claim['ticket']}.json"
            )
            if (
                (
                    claim.get("publication_lease")
                    or passport_path.exists()
                    and read(passport_path).get("publication_state") == "merged"
                )
                and self.ticket_merged(claim)
            ):
                if claim.get("publication_lease"):
                    self.release_publication(claim)
                self.migrate_passport(claim, "merged")
                if self.closeout(claim):
                    self.event_once("ticket_complete", claim["ticket"])
                    self.release(claim)
                    return {"status": "complete", "ticket": claim["ticket"]}
                return {"status": "waiting", "ticket": claim["ticket"]}
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
            maintenance = (self.product / "factory/MAINTENANCE").exists()
            if not valid_transition_evidence(transition, claim["ticket"]):
                raise ControllerError(
                    "maintenance boundary has invalid transition evidence"
                    if maintenance else
                    "state-machine returned invalid transition evidence"
                )
            stage = transition.get("stage", "")
            receipt = transition.get("receipt", "")
            role = transition.get("role")
            loop = transition.get("loop")
            if loop is not None:
                self.event_once(
                    "loop_attempt", claim["ticket"],
                    stage=stage,
                    transition_receipt_sha256=receipt,
                    **loop,
                )
            if maintenance:
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
                        if (
                            role == "narrator"
                            and isinstance(pr.get("preview_identity"), dict)
                            and not self.wait_for_preview_identity(claim, pr)
                        ):
                            return {"status": "blocked", "ticket": claim["ticket"]}
                        return {
                            "status": "waiting", "ticket": claim["ticket"],
                            "wait_reason": "pr-gate",
                        }
                    if (
                        role == "narrator"
                        and pr.get("status") == "failed"
                        and isinstance(pr.get("preview_preflight"), dict)
                        and pr["preview_preflight"].get("status") == "fail"
                    ):
                        self.block(claim, "preview-preflight")
                        self.event_once(
                            "preview_preflight_blocked", claim["ticket"],
                            expected=pr.get("head"),
                            reason=pr["preview_preflight"].get("reason"),
                        )
                        return {"status": "blocked", "ticket": claim["ticket"]}
                    if pr.get("status") == "failed" and self.retry_ci(
                        claim, receipt, pr
                    ):
                        return {
                            "status": "waiting", "ticket": claim["ticket"],
                            "wait_reason": "pr-gate",
                        }
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
                    self.clear_preview_identity_wait(claim)
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
                    return {
                        "status": "waiting", "ticket": claim["ticket"],
                        "wait_reason": "pr-gate",
                    }
                if pr.get("status") == "wait":
                    return {
                        "status": "waiting", "ticket": claim["ticket"],
                        "wait_reason": "pr-gate",
                    }
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
                if not self.request_protected_auto_merge(claim, receipt, pr):
                    return {"status": "waiting", "ticket": claim["ticket"]}
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
                try:
                    pr = self.ticket_pr(claim, receipt)
                except ControllerError:
                    if not self.ticket_merged(claim):
                        raise
                    if claim.get("publication_lease"):
                        self.release_publication(claim)
                    self.migrate_passport(claim, "merged")
                    self.closeout(claim)
                    return {"status": "progressed", "ticket": claim["ticket"]}
                if pr.get("status") == "failed" and self.retry_ci(
                    claim, receipt, pr
                ):
                    self.publication_ready(claim, receipt, pr["head"])
                    return {"status": "waiting", "ticket": claim["ticket"]}
                if pr.get("status") == "failed":
                    self.publication_repair(claim, receipt, pr)
                    return {"status": "progressed", "ticket": claim["ticket"]}
                if pr.get("status") == "wait":
                    self.publication_ready(claim, receipt, pr["head"])
                    return {"status": "waiting", "ticket": claim["ticket"]}
                if pr.get("status") == "ready":
                    self.request_protected_auto_merge(claim, receipt, pr)
                    return {"status": "waiting", "ticket": claim["ticket"]}
                raise ControllerError("publication PR gate returned an invalid status")
            if stage.startswith("AWAIT-MERGE closeout auto-merge pending"):
                self.closeout(claim)
                return {"status": "waiting", "ticket": claim["ticket"]}
            if stage.startswith("COMPLETE"):
                self.event_once("ticket_complete", claim["ticket"])
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
            claim["blocked_reason"] = "controller-error"
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
        self.admission_refusals = {}
        existing = self.load_claims()
        if self.qualification:
            tickets = set(self.qualification["tickets"])
            for claim in existing:
                if claim["ticket"] not in tickets:
                    self.withdraw_publication(claim)
            existing = [claim for claim in existing if claim["ticket"] in tickets]
        completed = [
            claim for claim in existing
            if self.product_ticket_done(claim["ticket"])
        ]
        for claim in completed:
            self.ensure_lease(claim, "terminal-cleanup")
            self.release(claim)
        existing = [claim for claim in existing if claim not in completed]
        self.record_qualification_done_targets()
        self.recover_missing_passport_claims(existing)
        self.recover_terminal_requests(existing)
        self.recover_each(
            existing, self.recover_interrupted_claims, "interrupted-reconciliation",
        )
        self.recover_each(
            existing, self.recover_missing_terminals, "missing-terminal",
        )
        self.recover_each(
            existing, self.recover_preflight_blocks, "preflight-retry",
            concurrent=True,
        )
        self.recover_each(
            existing, self.recover_upgraded_claims, "release-upgrade",
            concurrent=True,
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
            self.clear_admission_failure()
        except ControllerError as error:
            self.record_admission_failure(error, existing)
            claims = existing
        if (
            self.qualification
            and not self.qualification_marker("qualification-restart-boundary")
        ):
            active = sorted(
                item["ticket"] for item in claims if self.runnable(item)
            )
            accounted = sorted({item["ticket"] for item in claims} | {
                ticket for ticket in self.qualification["tickets"]
                if self.product_ticket_done(ticket)
            })
            target = self.qualification["target_done"]
            if len(accounted) == target:
                self.qualification_marker(
                    "qualification-restart-boundary", create=True,
                )
                self.event("restart_boundary", tickets=accounted)
                return {
                    "active": len(active),
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
            recovered = sorted(item["ticket"] for item in existing)
            accounted = sorted(set(recovered) | {
                ticket for ticket in self.qualification["tickets"]
                if self.product_ticket_done(ticket)
            })
            if accounted != sorted(self.qualification["tickets"]):
                raise ControllerError("qualification restart did not recover every target ticket")
            self.qualification_marker("qualification-recovered", create=True)
            self.event("controller_recovered", tickets=accounted)

        results: dict[str, dict[str, str]] = {}
        settled: set[str] = set()
        retry_after: dict[str, float] = {}
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
                self.mark_reconciling(claim)
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
                    and time.monotonic() >= retry_after.get(claim["ticket"], 0)
                    and not self.role_active(claim)
                ]
                self.recover_missing_passport_claims(claims)
                self.recover_terminal_requests(idle)
                self.recover_each(
                    idle, self.recover_interrupted_claims,
                    "interrupted-reconciliation",
                )
                self.recover_each(
                    idle, self.recover_missing_terminals, "missing-terminal",
                )
                self.recover_each(
                    idle, self.recover_preflight_blocks, "preflight-retry",
                    concurrent=True,
                )
                self.recover_each(
                    idle, self.recover_upgraded_claims, "release-upgrade",
                    concurrent=True,
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
                    self.clear_admission_failure()
                except ControllerError as error:
                    self.record_admission_failure(error, claims)
                new_idle = [
                    claim for claim in claims
                    if claim["ticket"] not in busy
                    and claim["ticket"] not in settled
                    and time.monotonic() >= retry_after.get(claim["ticket"], 0)
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
                        claim["blocked_reason"] = "worker-error"
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
                    self.reconciliation_marker(claim["ticket"]).unlink(
                        missing_ok=True
                    )
                    results[claim["ticket"]] = item
                    if (
                        item.get("status") == "waiting"
                        and item.get("wait_reason") == "pr-gate"
                        and futures
                    ):
                        retry_after[claim["ticket"]] = (
                            time.monotonic() + RECONCILE_INTERVAL_SECONDS
                        )
                    elif item.get("status") in {
                        "active", "blocked", "budget", "error", "maintenance",
                        "waiting",
                    }:
                        settled.add(claim["ticket"])
        finally:
            executor.shutdown(wait=True)
        claims = self.load_claims()
        for ticket, refusal in self.admission_refusals.items():
            results.setdefault(ticket, refusal)
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
    parser.add_argument(
        "--action", choices=("reconcile", "pause", "resume"),
        default="reconcile",
    )
    parser.add_argument("--ticket", default="")
    parser.add_argument("--issue", default="")
    parser.add_argument("--factory-sha", default="")
    args = parser.parse_args()
    lock_descriptor = -1
    try:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", args.project):
            raise ControllerError("invalid project")
        if (
            (args.action == "reconcile" and any((
                args.ticket, args.issue, args.factory_sha,
            )))
            or (args.action == "pause" and (args.factory_sha or not args.issue))
            or (args.action == "resume" and (args.issue or not args.factory_sha))
        ):
            raise ControllerError("controller action arguments are invalid")
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
        controller = Controller(args)
        if args.action == "pause":
            result = controller.pause_ticket(args.ticket, args.issue)
        elif args.action == "resume":
            result = controller.resume_ticket(args.ticket, args.factory_sha)
        else:
            if args.ticket:
                raise ControllerError("reconcile does not accept a ticket")
            result = controller.reconcile()
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
