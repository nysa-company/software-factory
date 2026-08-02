#!/usr/bin/env python3
"""Contract 1.8 deterministic transition resolver and one-use receipts."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
from legacy_closeout import ValidationError, protected_dependency  # noqa: E402
from role_output import RoleOutputError, sha256 as role_output_sha256  # noqa: E402


SCHEMA = "nysa.software-factory.state-machine/v1"
RECEIPT_SCHEMA = "nysa.software-factory.transition-receipt/v1"
REPAIR_SCHEMA = "nysa.software-factory.contract-repair/v1"
ROLE_EVIDENCE_SCHEMA = "nysa.software-factory.completed-role-sequence/v1"
DEPENDENCY_CONFLICT_SCHEMA = "nysa.software-factory.dependency-refresh/v2"
DEPENDENCY_CONFLICT_SOURCE = "dependency-conflict"
PASSPORT_SCHEMA = "nysa.software-factory.ticket-passport/v1"
PASSPORT_MIGRATION_SCHEMA = "nysa.software-factory.ticket-passport-migration/v2"
TICKET = re.compile(r"^T-[0-9]+$")
SHA = re.compile(r"^[0-9a-f]{40}$")
DIGEST = re.compile(r"^[0-9a-f]{64}$")
ROLE = re.compile(r"^(planner|spec-linter|test-author|builder|reviewer|narrator)$")
CONTRACT_BLOCK_ROLES = frozenset(("planner", "test-author", "builder"))
TARGET_STATE = {
    "planner": "Planning",
    "spec-linter": "Planning",
    "test-author": "Building",
    "builder": "Building",
    "reviewer": "Review",
    "narrator": "Review",
}
DEPENDENCY_CONFLICT_KEYS = {
    "schema", "ticket", "generation", "dependencies", "dependency_terminals",
    "old_head", "old_head_tree", "prior_base_head", "protected_head",
    "protected_head_tree", "protected_project_blob", "protected_delta_sha256",
    "test_paths", "test_paths_sha256", "conflicts", "repair_owner",
    "resolution", "merge_head", "merge_head_tree", "preserved_state",
    "transition_receipt_sha256", "factory_sha", "contract_version",
    "refreshed_at",
}


class StateError(ValueError):
    pass


def canonical(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode()


def git(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *arguments],
        text=True,
        capture_output=True,
        check=False,
        timeout=120,
    )
    if result.returncode:
        raise StateError(result.stderr.strip() or "Git operation failed")
    return result.stdout.strip()


def safe_state_dir(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=False, exist_ok=True)
    info = path.lstat()
    if (
        not path.is_absolute()
        or path.resolve(strict=True) != path
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise StateError("controller state directory is unsafe")
    return path


def safe_receipt(path: Path) -> dict[str, Any]:
    info = path.lstat()
    if (
        path.is_symlink()
        or not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_nlink != 1
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_size > 1_000_000
    ):
        raise StateError("transition receipt is unsafe")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema") != RECEIPT_SCHEMA:
        raise StateError("transition receipt is malformed")
    immutable = {key: item for key, item in value.items() if key not in {
        "consumed", "consumed_at_epoch", "receipt_sha256",
    }}
    digest = hashlib.sha256(canonical(immutable)).hexdigest()
    if value.get("receipt_sha256") != digest:
        raise StateError("transition receipt digest is invalid")
    if not isinstance(value.get("consumed"), bool):
        raise StateError("transition receipt consumption state is invalid")
    return value


def authenticated_passport(args: argparse.Namespace) -> tuple[dict[str, Any], bytes]:
    key_path = args.state_dir / "passport.key"
    passport_path = args.state_dir / "passports" / f"{args.ticket}.json"
    for path, size in ((key_path, 32), (passport_path, 5_000_000)):
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_size > size
            ):
                raise StateError("passport state is unsafe")
            with os.fdopen(descriptor, "rb") as stream:
                descriptor = -1
                raw = stream.read()
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        if path == key_path:
            secret = raw
        else:
            passport = json.loads(raw)
    if (
        len(secret) != 32
        or not isinstance(passport, dict)
        or passport.get("schema") != PASSPORT_SCHEMA
    ):
        raise StateError("passport state is invalid")
    passport_digest = passport.pop("passport_sha256", "")
    if passport_digest != hashlib.sha256(canonical(passport)).hexdigest():
        raise StateError("passport digest is invalid")
    authentication = passport.pop("authentication_sha256", "")
    if not hmac.compare_digest(
        authentication, hmac.new(secret, canonical(passport), hashlib.sha256).hexdigest()
    ):
        raise StateError("passport authentication is invalid")
    passport["authentication_sha256"] = authentication
    passport["passport_sha256"] = passport_digest
    return passport, secret


def repair_path(args: argparse.Namespace) -> Path:
    directory = args.state_dir / "contract-repairs"
    directory.mkdir(mode=0o700, exist_ok=True)
    info = directory.lstat()
    if (
        directory.is_symlink()
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise StateError("contract repair directory is unsafe")
    return directory / f"{args.ticket}.json"


def signed_repair(value: dict[str, Any], secret: bytes) -> dict[str, Any]:
    result = dict(value)
    result["authentication_sha256"] = hmac.new(
        secret, canonical(value), hashlib.sha256
    ).hexdigest()
    result["repair_sha256"] = hashlib.sha256(canonical(result)).hexdigest()
    return result


def load_signed_repair(path: Path, secret: bytes) -> dict[str, Any]:
    info = path.lstat()
    if (
        path.is_symlink()
        or not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_nlink != 1
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_size > 1_000_000
    ):
        raise StateError("contract repair record is unsafe")
    value = json.loads(path.read_text(encoding="utf-8"))
    repair_digest = value.pop("repair_sha256", "")
    if repair_digest != hashlib.sha256(canonical(value)).hexdigest():
        raise StateError("contract repair digest is invalid")
    authentication = value.pop("authentication_sha256", "")
    if not hmac.compare_digest(
        authentication, hmac.new(secret, canonical(value), hashlib.sha256).hexdigest()
    ):
        raise StateError("contract repair authentication is invalid")
    value["authentication_sha256"] = authentication
    value["repair_sha256"] = repair_digest
    return value


def load_repair(args: argparse.Namespace, secret: bytes) -> dict[str, Any] | None:
    path = repair_path(args)
    if not path.exists() and not path.is_symlink():
        return None
    return load_signed_repair(path, secret)


def completed_repair_matches_directive(
    args: argparse.Namespace,
    passport: dict[str, Any],
    secret: bytes,
    role: str,
    receipt: str,
) -> bool:
    directory = repair_path(args).parent / "completed"
    if not directory.exists() and not directory.is_symlink():
        return False
    info = directory.lstat()
    if (
        directory.is_symlink()
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise StateError("completed contract repair directory is unsafe")
    matched = False
    for path in sorted(directory.glob(f"{args.ticket}-*.json")):
        record = load_signed_repair(path, secret)
        digest = record.get("repair_sha256", "")
        if path.name != f"{args.ticket}-{digest}.json":
            raise StateError("completed contract repair record conflicts")
        if (
            record.get("schema") != REPAIR_SCHEMA
            or record.get("ticket") != args.ticket
            or record.get("branch") != passport.get("branch")
            or record.get("repair_role")
            not in {"planner", "spec-linter", "test-author", "builder"}
            or not SHA.fullmatch(record.get("head_sha", ""))
        ):
            raise StateError("completed contract repair record is invalid")
        if (
            record["repair_role"] != role
            or record.get("blocked_receipt") != receipt
        ):
            continue
        if branch_contains(args, record["head_sha"]):
            matched = True
    return matched


def write_atomic(path: Path, value: dict[str, Any]) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(canonical(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        Path(temporary).unlink(missing_ok=True)


def run_manifest(path: Path) -> tuple[dict[str, str], bytes]:
    info = path.lstat()
    if (
        path.is_symlink()
        or not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
    ):
        raise StateError("run manifest is unsafe")
    raw = path.read_bytes()
    values: dict[str, str] = {}
    for line in raw.decode("utf-8").splitlines():
        key, separator, value = line.partition("=")
        if not separator or not key or key in values:
            raise StateError("run manifest is malformed")
        values[key] = value
    return values, raw


def require_current_lease(args: argparse.Namespace) -> None:
    directory = args.factory_root / "factory" / ".dispatch-leases"
    path = directory / f"{args.ticket}.json"
    try:
        directory_info = directory.lstat()
        info = path.lstat()
        record = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        raise StateError("current dispatcher lease is invalid") from None
    if (
        directory.is_symlink()
        or not stat.S_ISDIR(directory_info.st_mode)
        or path.is_symlink()
        or not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_nlink != 1
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_size > 1_000_000
        or not isinstance(record, dict)
        or record.get("schema_version") != 1
        or record.get("ticket") != args.ticket
        or record.get("lease_id") != args.lease
        or not isinstance(record.get("claimed_epoch"), int)
        or isinstance(record.get("claimed_epoch"), bool)
        or not isinstance(record.get("expires_epoch"), int)
        or isinstance(record.get("expires_epoch"), bool)
        or record["expires_epoch"] <= record["claimed_epoch"]
        or record["expires_epoch"] <= int(time.time())
    ):
        raise StateError("current dispatcher lease is invalid")


def ticket_evidence_digest(factory: Path, ticket: str) -> str:
    selected: list[tuple[str, bytes]] = []
    runs = factory / "runs"
    if runs.exists():
        info = runs.lstat()
        if not stat.S_ISDIR(info.st_mode) or runs.is_symlink():
            raise StateError("run manifest directory is unsafe")
        for path in sorted(runs.glob("*.meta")):
            values, raw = run_manifest(path)
            if (
                values.get("ticket") == ticket
                and values.get("accounting_state") not in {None, "", "reserved"}
            ):
                selected.append((path.name, hashlib.sha256(raw).digest()))
                output = path.with_suffix(".out")
                if output.is_file() and not output.is_symlink():
                    try:
                        output_digest = bytes.fromhex(
                            role_output_sha256(output)
                        )
                    except (OSError, RoleOutputError, ValueError) as error:
                        raise StateError(
                            "run role output is unsafe"
                        ) from error
                    selected.append((output.name, output_digest))
    digest = hashlib.sha256()
    for name, item_digest in selected:
        digest.update(name.encode())
        digest.update(b"\0")
        digest.update(item_digest)
    return digest.hexdigest()


def stage_role(stage: str) -> str | None:
    action, separator, detail = stage.partition(" ")
    if action in {"RUN", "FIX"} and separator and ROLE.fullmatch(detail):
        return detail
    if action in {
        "AWAIT-OPERATOR", "AWAIT-MERGE", "AWAIT_BUDGET", "AWAIT_DEPENDENCY",
        "COMPLETE", "ESCALATE", "REFUSE",
    }:
        return None
    raise StateError("state resolver returned an unsupported transition")


def ticket_field(workdir: Path, ticket: str, name: str) -> str:
    text = (workdir / "factory" / "tickets" / f"{ticket}.md").read_text(
        encoding="utf-8"
    )
    values = re.findall(
        rf"^{re.escape(name)}:\s*(.*?)\s*$", text,
        re.IGNORECASE | re.MULTILINE,
    )
    if len(values) != 1:
        raise StateError(
            "ticket state is ambiguous"
            if name == "State"
            else f"ticket {name} is ambiguous"
        )
    return values[0]


def current_state(workdir: Path, ticket: str) -> str:
    return ticket_field(workdir, ticket, "State")


def declared_dependencies(args: argparse.Namespace) -> tuple[str, ...]:
    text = (
        args.workdir / "factory" / "tickets" / f"{args.ticket}.md"
    ).read_text(encoding="utf-8")
    values = re.findall(r"^Depends-On:\s*(.*?)\s*$", text, re.I | re.M)
    if len(values) > 1:
        raise StateError("ticket Depends-On is ambiguous")
    raw = values[0] if values else "none"
    if raw.casefold() == "none":
        return ()
    dependencies = tuple(item.strip() for item in raw.split(","))
    if (
        not dependencies
        or len(dependencies) != len(set(dependencies))
        or args.ticket in dependencies
        or any(not TICKET.fullmatch(item) for item in dependencies)
    ):
        raise StateError("ticket dependencies are invalid")
    return dependencies


def unresolved_dependencies(
    args: argparse.Namespace, dependencies: tuple[str, ...] | None = None
) -> tuple[str, ...]:
    dependencies = dependencies or declared_dependencies(args)
    unresolved = []
    for dependency in dependencies:
        try:
            protected_dependency(args.factory_root, dependency)
        except ValidationError:
            unresolved.append(dependency)
    return tuple(unresolved)


def protected_base_sha(args: argparse.Namespace) -> str:
    value = git(args.workdir, "rev-parse", "--verify", "origin/main^{commit}")
    if not SHA.fullmatch(value):
        raise StateError("protected main tracking ref is invalid")
    return value


def branch_contains(args: argparse.Namespace, commit: str) -> bool:
    result = subprocess.run(
        [
            "git", "-C", str(args.workdir), "merge-base", "--is-ancestor",
            commit, "HEAD",
        ],
        text=True,
        capture_output=True,
        check=False,
        timeout=120,
    )
    if result.returncode not in {0, 1}:
        raise StateError(
            result.stderr.strip() or "protected-base ancestry check failed"
        )
    return result.returncode == 0


def migrated_contract_block(
    args: argparse.Namespace, receipt: dict[str, Any]
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if receipt.get("factory_sha") == args.factory_sha:
        return None, None
    passport, _ = authenticated_passport(args)
    history = passport.get("factory_release_history")
    charges = passport.get("charge_records")
    completed = passport.get("completed_role_evidence")
    receipt_factory = receipt.get("factory_sha", "")
    receipt_digest = receipt.get("receipt_sha256", "")
    if (
        passport.get("ticket") != args.ticket
        or passport.get("branch") != receipt.get("branch")
        or passport.get("factory_sha") != args.factory_sha
        or not isinstance(history, list)
        or not isinstance(charges, list)
        or not isinstance(completed, list)
        or not SHA.fullmatch(receipt_factory)
    ):
        raise StateError("contract blocker release lineage is invalid")
    releases = [
        item.get("factory_sha")
        for item in history
        if isinstance(item, dict)
        and item.get("contract_version") == args.contract_version
        and SHA.fullmatch(item.get("factory_sha", ""))
    ]
    if (
        len(releases) != len(history)
        or len(releases) != len(set(releases))
        or receipt_factory not in releases
        or args.factory_sha not in releases
        or releases.index(receipt_factory) >= releases.index(args.factory_sha)
    ):
        raise StateError("contract blocker release lineage is invalid")
    matches = [
        item for item in charges
        if isinstance(item, dict)
        and item.get("transition_receipt_sha256") == receipt_digest
        and item.get("factory_sha") == receipt_factory
        and item.get("contract_version") == args.contract_version
        and item.get("ticket", args.ticket) == args.ticket
        and item.get("role") == receipt.get("role")
        and item.get("head_before") == receipt.get("head_sha")
        and DIGEST.fullmatch(item.get("manifest_sha256", ""))
        and isinstance(item.get("run_id"), str)
        and item.get("run_id")
    ]
    if len(matches) != 1 or any(
        isinstance(item, dict)
        and item.get("transition_receipt_sha256") == receipt_digest
        for item in completed
    ):
        raise StateError("contract blocker passport evidence is invalid")
    return passport, matches[0]


def materialized_contract_block(
    args: argparse.Namespace, receipt: dict[str, Any], role: str
) -> bool:
    try:
        passport, _ = authenticated_passport(args)
    except (OSError, ValueError, StateError):
        return False
    passport_head = passport.get("head_sha", "")
    receipt_head = receipt.get("head_sha", "")
    charges = passport.get("charge_records")
    completed = passport.get("completed_role_evidence")
    if (
        passport.get("ticket") != args.ticket
        or passport.get("branch") != receipt.get("branch")
        or passport.get("project") != args.project
        or passport.get("contract_version") != args.contract_version
        or passport.get("factory_sha") != args.factory_sha
        or passport.get("current_state") != "Blocked-Escalated"
        or passport.get("current_stage") != receipt.get("stage")
        or passport.get("transition_receipt_sha256")
        != receipt.get("receipt_sha256")
        or not SHA.fullmatch(passport_head)
        or not SHA.fullmatch(receipt_head)
        or not isinstance(charges, list)
        or not isinstance(completed, list)
        or current_state(args.workdir, args.ticket) != "Blocked-Escalated"
    ):
        return False
    try:
        contract_block_resume_state(
            args,
            role,
            ticket_field(args.workdir, args.ticket, "Resume-State"),
        )
    except StateError:
        return False
    matches = [
        item for item in charges
        if isinstance(item, dict)
        and item.get("transition_receipt_sha256")
        == receipt.get("receipt_sha256")
        and item.get("factory_sha") == args.factory_sha
        and item.get("contract_version") == args.contract_version
        and item.get("role") == role
        and item.get("head_before") == receipt_head
    ]
    if len(matches) != 1 or any(
        isinstance(item, dict)
        and item.get("transition_receipt_sha256")
        == receipt.get("receipt_sha256")
        for item in completed
    ):
        return False
    for ancestor, descendant in (
        (receipt_head, passport_head),
        (passport_head, git(args.workdir, "rev-parse", "HEAD")),
    ):
        result = subprocess.run(
            [
                "git", "-C", str(args.workdir), "merge-base",
                "--is-ancestor", ancestor, descendant,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=120,
        )
        if result.returncode != 0:
            return False
    return True


def contract_block_terminal(
    args: argparse.Namespace,
    receipt: dict[str, Any],
    charge: dict[str, Any] | None = None,
) -> dict[str, str]:
    matches = []
    for path in sorted((args.factory_root / "factory/runs").glob("*.meta")):
        fields, raw = run_manifest(path)
        if fields.get("transition_receipt_sha256") == receipt.get(
            "receipt_sha256"
        ):
            matches.append((fields, raw))
    if len(matches) != 1:
        raise StateError("contract blocker terminal evidence is ambiguous")
    terminal, raw = matches[0]
    accounted = terminal.get("accounting_state") == "completed" or (
        terminal.get("accounting_state") == "abandoned_conservative"
        and terminal.get("cost_basis") == "conservative_reservation"
        and terminal.get("effective_cost") == terminal.get("reserved_usd")
    )
    if not accounted or any((
        terminal.get("ticket") != args.ticket,
        terminal.get("role") != receipt.get("role"),
        terminal.get("contract_version") != args.contract_version,
        terminal.get("kit_sha") != receipt.get("factory_sha"),
        terminal.get("phase") != "completed",
        terminal.get("go_issued") != "1",
        terminal.get("task_submitted") != "1",
        terminal.get("exit_status") != "12",
        terminal.get("role_exit") != "role_exit_contract_blocked",
        terminal.get("role_branch_before") != receipt.get("branch"),
        terminal.get("role_head_before") != receipt.get("head_sha"),
    )):
        raise StateError("contract blocker terminal evidence is invalid")
    if charge is not None and (
        charge.get("run_id") != terminal.get("run_id")
        or charge.get("accounting_state") != terminal.get("accounting_state")
        or charge.get("manifest_sha256") != hashlib.sha256(raw).hexdigest()
    ):
        raise StateError("contract blocker passport evidence is invalid")
    return terminal


def contract_blocked_receipt(args: argparse.Namespace) -> str:
    value = safe_receipt(args.state_dir / f"{args.ticket}.json")
    origin = os.environ.get("FACTORY_CERTIFIED_PRODUCT_ORIGIN", "")
    role = value.get("role", "")
    old_head = value.get("head_sha", "")
    passport, charge = migrated_contract_block(args, value)
    migrated = passport is not None
    if migrated and args.action == "block":
        require_current_lease(args)
    recovered_lease = (
        args.action == "block"
        and not migrated
        and value.get("lease_sha256")
        != hashlib.sha256(args.lease.encode()).hexdigest()
        and role in CONTRACT_BLOCK_ROLES
        and materialized_contract_block(args, value, role)
    )
    if recovered_lease:
        require_current_lease(args)
    if (
        not origin
        or value.get("receipt_sha256") != args.receipt
        or not value.get("consumed")
        or value.get("ticket") != args.ticket
        or value.get("contract_version") != args.contract_version
        or (value.get("factory_sha") != args.factory_sha and not migrated)
        or value.get("project") != args.project
        or role not in CONTRACT_BLOCK_ROLES
        or not SHA.fullmatch(old_head)
        or value.get("branch") != git(
            args.workdir, "symbolic-ref", "--quiet", "--short", "HEAD"
        )
        or value.get("product_origin_sha256")
        != hashlib.sha256(origin.encode()).hexdigest()
        or (
            args.action == "block"
            and not migrated
            and value.get("lease_sha256")
            != hashlib.sha256(args.lease.encode()).hexdigest()
            and not recovered_lease
        )
    ):
        raise StateError("contract blocker receipt is invalid")
    if subprocess.run(
        [
            "git", "-C", str(args.workdir), "merge-base", "--is-ancestor",
            old_head, "HEAD",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=120,
    ).returncode:
        raise StateError("contract blocker is outside receipt lineage")
    contract_block_terminal(args, value, charge if migrated else None)
    return role


def operator_resume_role(
    args: argparse.Namespace, passport: dict[str, Any], blocked_role: str
) -> str:
    relative = f"factory/tickets/{args.ticket}.md"
    prior_head = passport.get("head_sha", "")
    if (
        passport.get("ticket") != args.ticket
        or passport.get("factory_sha") != args.factory_sha
        or passport.get("branch")
        != git(args.workdir, "symbolic-ref", "--quiet", "--short", "HEAD")
        or not SHA.fullmatch(prior_head)
    ):
        raise StateError("contract repair passport is invalid")
    current = git(args.workdir, "show", f"HEAD:{relative}") + "\n"
    directives = re.findall(
        r"^OPERATOR RESUME: (planner|spec-linter|test-author|builder)$",
        current, re.M,
    )
    receipt_directives = re.findall(
        r"^OPERATOR RESUME RECEIPT: ([0-9a-f]{64})$",
        current,
        re.M,
    )
    if (
        len(directives) != 1
        or receipt_directives != [args.receipt]
    ):
        raise StateError("contract repair requires a receipt-bound operator directive")
    repair_role = directives[0]
    directive = f"OPERATOR RESUME: {repair_role}"
    receipt_directive = f"OPERATOR RESUME RECEIPT: {args.receipt}"
    known_heads = {prior_head}
    for migration in passport.get("migration_history", []):
        if isinstance(migration, dict):
            known_heads.update(
                migration.get(name, "")
                for name in ("from_head_sha", "to_head_sha")
                if SHA.fullmatch(migration.get(name, ""))
            )
    commits = git(
        args.workdir,
        "log",
        "--format=%H",
        f"-S{receipt_directive}",
        "--",
        relative,
    ).splitlines()
    candidates: list[tuple[str, str]] = []
    for candidate in commits:
        if not SHA.fullmatch(candidate):
            continue
        candidate_ticket = git(
            args.workdir, "show", f"{candidate}:{relative}"
        ) + "\n"
        if (
            re.findall(
                r"^OPERATOR RESUME: "
                r"(planner|spec-linter|test-author|builder)$",
                candidate_ticket,
                re.M,
            ) != [repair_role]
            or re.findall(
                r"^OPERATOR RESUME RECEIPT: ([0-9a-f]{64})$",
                candidate_ticket,
                re.M,
            ) != [args.receipt]
        ):
            continue
        ancestry = git(
            args.workdir, "rev-list", "--parents", "-n", "1", candidate
        ).split()
        if (
            len(ancestry) != 2
            or ancestry[0] != candidate
            or ancestry[1] not in known_heads
            or subprocess.run(
                [
                    "git", "-C", str(args.workdir),
                    "merge-base", "--is-ancestor", candidate, "HEAD",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=120,
            ).returncode != 0
        ):
            continue
        candidates.append((candidate, ancestry[1]))
    if len(candidates) != 1:
        raise StateError("contract repair operator directive is invalid")
    commit, parent = candidates[0]
    before = git(args.workdir, "show", f"{parent}:{relative}") + "\n"
    after = git(args.workdir, "show", f"{commit}:{relative}") + "\n"
    prior_directives = re.findall(
        r"^OPERATOR RESUME: (planner|spec-linter|test-author|builder)$",
        before,
        re.M,
    )
    prior_receipts = re.findall(
        r"^OPERATOR RESUME RECEIPT: ([0-9a-f]{64})$",
        before,
        re.M,
    )
    if not prior_directives and not prior_receipts:
        expected = (
            before.rstrip("\n")
            + f"\n\n{directive}\n{receipt_directive}\n"
        )
    elif len(prior_directives) == 1 and not prior_receipts:
        expected = re.sub(
            r"^OPERATOR RESUME: (planner|spec-linter|test-author|builder)$",
            f"{directive}\n{receipt_directive}",
            before,
            count=1,
            flags=re.M,
        )
    elif len(prior_directives) == 1 and len(prior_receipts) == 1:
        expected = re.sub(
            r"^OPERATOR RESUME: (planner|spec-linter|test-author|builder)$",
            directive,
            before,
            count=1,
            flags=re.M,
        )
        expected = re.sub(
            r"^OPERATOR RESUME RECEIPT: [0-9a-f]{64}$",
            receipt_directive,
            expected,
            count=1,
            flags=re.M,
        )
    else:
        raise StateError("contract repair operator directive is invalid")
    changed = git(args.workdir, "diff", "--name-only", f"{parent}..{commit}").splitlines()
    current_head = git(args.workdir, "rev-parse", "HEAD")
    if (
        len(directives) != 1
        or after != expected
        or changed != [relative]
        or current_head not in {commit, prior_head}
    ):
        raise StateError("contract repair operator directive is invalid")
    return repair_role


def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate key")
        value[key] = item
    return value


def safe_test_paths(project: str) -> list[str]:
    values = re.findall(r"(?m)^TEST_PATHS=(.*)$", project)
    if len(values) != 1:
        raise StateError("protected PROJECT.env TEST_PATHS is ambiguous")
    try:
        paths = " ".join(
            shlex.split(values[0], comments=False, posix=True)
        ).split()
    except ValueError as error:
        raise StateError("protected PROJECT.env TEST_PATHS is invalid") from error
    safe = re.compile(r"[A-Za-z0-9._][A-Za-z0-9._/-]*")
    if (
        not paths
        or len(paths) != len(set(paths))
        or any(
            not safe.fullmatch(path.rstrip("/"))
            or any(part in {"", ".", ".."} for part in path.rstrip("/").split("/"))
            or path.rstrip("/") == "factory"
            or path.rstrip("/").startswith("factory/")
            for path in paths
        )
    ):
        raise StateError("protected PROJECT.env TEST_PATHS is invalid")
    normalized = [path.rstrip("/") for path in paths]
    if any(
        left == right
        or left.startswith(right + "/")
        or right.startswith(left + "/")
        for index, left in enumerate(normalized)
        for right in normalized[index + 1:]
    ):
        raise StateError("protected PROJECT.env TEST_PATHS overlaps")
    return paths


def tree_entry(workdir: Path, commit: str, path: str) -> tuple[str, str]:
    raw = git(workdir, "ls-tree", commit, "--", path)
    metadata, separator, observed = raw.partition("\t")
    fields = metadata.split()
    if (
        not separator
        or observed != path
        or len(fields) != 3
        or fields[1] != "blob"
        or not re.fullmatch(r"[0-7]{6}", fields[0])
        or not SHA.fullmatch(fields[2])
    ):
        raise StateError("dependency conflict tree entry is invalid")
    return fields[0], fields[2]


def dependency_conflict_receipt(
    args: argparse.Namespace, *, require_current_base: bool = True,
) -> tuple[dict[str, Any], str, str] | None:
    relative = f"factory/attestations/{args.ticket}/dependency-refresh.json"
    path = args.workdir / relative
    if not path.exists() and not path.is_symlink():
        return None
    info = path.lstat()
    if (
        path.is_symlink()
        or not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or info.st_size > 1_000_000
    ):
        raise StateError("dependency conflict receipt is unsafe")
    raw = path.read_bytes()
    try:
        value = json.loads(
            raw.decode("utf-8"), object_pairs_hook=unique_object,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise StateError("dependency conflict receipt is malformed") from error
    if value.get("schema") != DEPENDENCY_CONFLICT_SCHEMA:
        return None
    generation = value.get("generation")
    dependencies = value.get("dependencies")
    conflicts = value.get("conflicts")
    test_paths = value.get("test_paths")
    if (
        set(value) != DEPENDENCY_CONFLICT_KEYS
        or value.get("ticket") != args.ticket
        or isinstance(generation, bool)
        or not isinstance(generation, int)
        or generation < 1
        or not isinstance(dependencies, list)
        or not dependencies
        or dependencies != list(declared_dependencies(args))
        or len(dependencies) != len(set(dependencies))
        or not isinstance(test_paths, list)
        or safe_test_paths(
            git(
                args.workdir, "show",
                f"{value.get('protected_head', '')}:factory/PROJECT.env",
            )
        ) != test_paths
        or value.get("test_paths_sha256")
        != hashlib.sha256(json.dumps(
            test_paths, ensure_ascii=True, separators=(",", ":"),
        ).encode()).hexdigest()
        or value.get("repair_owner") != "test-author"
        or value.get("resolution")
        != "protected-baseline-before-test-author"
        or value.get("preserved_state") != "Building"
        or value.get("contract_version") != args.contract_version
        or not isinstance(value.get("refreshed_at"), str)
        or not re.fullmatch(
            r"20[0-9]{2}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z",
            value["refreshed_at"],
        )
        or not isinstance(conflicts, list)
        or not conflicts
    ):
        raise StateError("dependency conflict receipt is malformed")
    sha_fields = (
        "old_head", "old_head_tree", "prior_base_head", "protected_head",
        "protected_head_tree", "protected_project_blob", "merge_head",
        "merge_head_tree", "factory_sha",
    )
    digest_fields = (
        "protected_delta_sha256", "test_paths_sha256",
        "transition_receipt_sha256",
    )
    if (
        any(not SHA.fullmatch(value.get(name, "")) for name in sha_fields)
        or any(not DIGEST.fullmatch(value.get(name, "")) for name in digest_fields)
    ):
        raise StateError("dependency conflict receipt binding is invalid")
    current_base = protected_base_sha(args)
    base_check = subprocess.run(
        [
            "git", "-C", str(args.workdir), "merge-base", "--is-ancestor",
            value["protected_head"], current_base,
        ],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        check=False, timeout=120,
    )
    if base_check.returncode not in {0, 1}:
        raise StateError("dependency conflict protected-base check failed")
    if (
        (
            value["protected_head"] != current_base
            if require_current_base
            else base_check.returncode != 0
        )
        or not branch_contains(args, value["protected_head"])
        or git(
            args.workdir, "rev-parse", f"{value['old_head']}^{{tree}}"
        ) != value["old_head_tree"]
        or git(
            args.workdir, "rev-parse", f"{value['protected_head']}^{{tree}}"
        ) != value["protected_head_tree"]
        or git(
            args.workdir, "rev-parse", f"{value['merge_head']}^{{tree}}"
        ) != value["merge_head_tree"]
        or tree_entry(
            args.workdir, value["protected_head"], "factory/PROJECT.env",
        )[1] != value["protected_project_blob"]
    ):
        raise StateError("dependency conflict receipt binding is invalid")
    delta = subprocess.run(
        [
            "git", "-C", str(args.workdir), "diff", "--name-status", "-z",
            value["prior_base_head"], value["protected_head"],
        ],
        capture_output=True, check=True, timeout=120,
    ).stdout
    if hashlib.sha256(delta).hexdigest() != value["protected_delta_sha256"]:
        raise StateError("dependency conflict protected delta changed")
    expected_conflict_keys = {
        "path", "base_blob", "base_mode", "ticket_blob", "ticket_mode",
        "protected_blob", "protected_mode",
    }
    observed_paths: list[str] = []
    for item in conflicts:
        path_value = item.get("path", "") if isinstance(item, dict) else ""
        if (
            not isinstance(item, dict)
            or set(item) != expected_conflict_keys
            or not re.fullmatch(
                r"[A-Za-z0-9._][A-Za-z0-9._/@+-]*", path_value,
            )
            or any(part in {"", ".", ".."} for part in path_value.split("/"))
            or any(item.get(name) != "100644" for name in (
                "base_mode", "ticket_mode", "protected_mode",
            ))
            or any(not SHA.fullmatch(item.get(name, "")) for name in (
                "base_blob", "ticket_blob", "protected_blob",
            ))
            or not any(
                path_value.startswith(prefix)
                if prefix.endswith("/") else path_value == prefix
                for prefix in test_paths
            )
            or tree_entry(
                args.workdir, value["prior_base_head"], path_value,
            ) != (item["base_mode"], item["base_blob"])
            or tree_entry(
                args.workdir, value["old_head"], path_value,
            ) != (item["ticket_mode"], item["ticket_blob"])
            or tree_entry(
                args.workdir, value["protected_head"], path_value,
            ) != (item["protected_mode"], item["protected_blob"])
            or tree_entry(
                args.workdir, value["merge_head"], path_value,
            ) != (item["protected_mode"], item["protected_blob"])
        ):
            raise StateError("dependency conflict evidence is invalid")
        observed_paths.append(path_value)
    if observed_paths != sorted(observed_paths) or len(observed_paths) != len(
        set(observed_paths)
    ):
        raise StateError("dependency conflict paths are ambiguous")
    parents = git(
        args.workdir, "rev-list", "--parents", "-n", "1",
        value["merge_head"],
    ).split()
    if parents != [
        value["merge_head"], value["old_head"], value["protected_head"],
    ]:
        raise StateError("dependency conflict merge topology is invalid")
    receipt_commit = git(
        args.workdir, "log", "-1", "--format=%H", "HEAD", "--", relative,
    )
    if (
        not SHA.fullmatch(receipt_commit)
        or git(args.workdir, "rev-parse", f"{receipt_commit}^")
        != value["merge_head"]
        or git(
            args.workdir, "diff", "--name-only",
            f"{value['merge_head']}..{receipt_commit}",
        ).splitlines() != [relative]
        or git(args.workdir, "rev-parse", f"{receipt_commit}:{relative}")
        != git(args.workdir, "hash-object", str(path))
    ):
        raise StateError("dependency conflict receipt topology is invalid")
    unchanged = subprocess.run(
        [
            "git", "-C", str(args.workdir), "diff", "--quiet",
            receipt_commit, "HEAD", "--", relative,
        ],
        check=False, timeout=120,
    )
    if unchanged.returncode != 0:
        raise StateError("dependency conflict receipt changed after creation")
    terminals = value.get("dependency_terminals")
    if (
        not isinstance(terminals, list)
        or len(terminals) != len(dependencies)
    ):
        raise StateError("dependency conflict terminal evidence is invalid")
    for dependency, terminal in zip(dependencies, terminals):
        try:
            expected_terminal = protected_dependency(
                args.factory_root, dependency, value["protected_head"],
            )
        except ValidationError as error:
            raise StateError(
                "dependency conflict terminal evidence changed"
            ) from error
        expected_digest = hashlib.sha256(json.dumps(
            expected_terminal, ensure_ascii=True, sort_keys=True,
            separators=(",", ":"),
        ).encode()).hexdigest()
        if terminal != {
            "ticket": dependency,
            "terminal_sha256": expected_digest,
        }:
            raise StateError("dependency conflict terminal evidence is invalid")
    return value, hashlib.sha256(raw).hexdigest(), receipt_commit


def validate_dependency_conflict_transition(
    args: argparse.Namespace, receipt: dict[str, Any],
) -> None:
    transition = safe_receipt(args.state_dir / f"{args.ticket}.json")
    expected_stage = (
        "REFUSE dependency refresh required; "
        f"dependencies={','.join(receipt['dependencies'])}; "
        f"protected-main={receipt['protected_head']}"
    )
    if (
        transition.get("receipt_sha256")
        != receipt["transition_receipt_sha256"]
        or transition.get("consumed") is not True
        or transition.get("stage") != expected_stage
        or transition.get("role") is not None
        or transition.get("ticket") != args.ticket
        or transition.get("project") != args.project
        or transition.get("factory_sha") != receipt["factory_sha"]
        or transition.get("contract_version") != receipt["contract_version"]
        or transition.get("branch")
        != git(args.workdir, "symbolic-ref", "--quiet", "--short", "HEAD")
        or transition.get("head_sha") != receipt["old_head"]
        or transition.get("head_tree") != receipt["old_head_tree"]
    ):
        raise StateError("dependency conflict transition receipt is invalid")


def dependency_conflict_migrated(
    args: argparse.Namespace,
    passport: dict[str, Any],
    receipt: dict[str, Any],
    receipt_commit: str,
) -> bool:
    current_head = git(args.workdir, "rev-parse", "HEAD")
    if receipt.get("factory_sha") == args.factory_sha:
        return branch_contains(args, receipt_commit)
    history = passport.get("factory_release_history")
    migrations = passport.get("migration_history")
    if not isinstance(history, list) or not isinstance(migrations, list):
        return False
    releases = [
        item.get("factory_sha")
        for item in history
        if isinstance(item, dict)
        and item.get("contract_version") == args.contract_version
        and SHA.fullmatch(item.get("factory_sha", ""))
    ]
    if (
        len(releases) != len(history)
        or len(releases) != len(set(releases))
        or receipt["factory_sha"] not in releases
        or args.factory_sha not in releases
        or releases.index(receipt["factory_sha"])
        >= releases.index(args.factory_sha)
    ):
        return False
    starts = []
    for index, edge in enumerate(migrations):
        if (
            not isinstance(edge, dict)
            or edge.get("schema") != PASSPORT_MIGRATION_SCHEMA
            or edge.get("from_factory_sha") != receipt["factory_sha"]
            or edge.get("from_head_sha") != receipt_commit
        ):
            continue
        suffix = migrations[index:]
        if (
            all(
                isinstance(item, dict)
                and item.get("schema") == PASSPORT_MIGRATION_SCHEMA
                and all(
                    SHA.fullmatch(item.get(name, ""))
                    for name in (
                        "from_factory_sha", "from_head_sha",
                        "from_protected_base_sha", "to_factory_sha",
                        "to_head_sha", "to_protected_base_sha",
                    )
                )
                and all(
                    DIGEST.fullmatch(item.get(name, ""))
                    for name in (
                        "from_passport_file_sha256",
                        "from_passport_sha256", "from_route_plan_sha256",
                        "to_route_plan_sha256",
                    )
                )
                for item in suffix
            )
            and all(
                prior["to_factory_sha"] == following["from_factory_sha"]
                and prior["to_head_sha"] == following["from_head_sha"]
                and prior["to_protected_base_sha"]
                == following["from_protected_base_sha"]
                for prior, following in zip(suffix, suffix[1:])
            )
            and suffix[-1].get("to_factory_sha") == args.factory_sha
            and suffix[-1].get("to_head_sha") == current_head
            and suffix[-1].get("to_protected_base_sha")
            == passport.get("protected_base_sha")
            and suffix[-1].get("to_route_plan_sha256")
            == passport.get("route_plan_sha256")
        ):
            starts.append(index)
    return len(starts) == 1


def completed_dependency_conflict_records(
    args: argparse.Namespace, secret: bytes,
) -> list[dict[str, Any]]:
    completed = repair_path(args).parent / "completed"
    records = []
    if not completed.exists() and not completed.is_symlink():
        return records
    info = completed.lstat()
    if (
        completed.is_symlink()
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise StateError("completed contract repair directory is unsafe")
    for item in sorted(completed.glob(f"{args.ticket}-*.json")):
        record = load_signed_repair(item, secret)
        if record.get("repair_source") == DEPENDENCY_CONFLICT_SOURCE:
            records.append(record)
    return records


def completed_dependency_conflict_repairs(
    args: argparse.Namespace,
    secret: bytes,
    receipt_digest: str,
    receipt_commit: str,
) -> list[dict[str, Any]]:
    matches = [
        record for record in completed_dependency_conflict_records(args, secret)
        if record.get("dependency_refresh_sha256") == receipt_digest
        and record.get("dependency_refresh_commit") == receipt_commit
    ]
    if len(matches) > 1:
        raise StateError("completed dependency conflict repair is ambiguous")
    return matches


def completed_dependency_conflict_migration_head(
    args: argparse.Namespace,
    passport: dict[str, Any],
    record: dict[str, Any],
) -> str | None:
    if record.get("repair_source") != DEPENDENCY_CONFLICT_SOURCE:
        return None
    successes = contract_repair_successes(
        args, record.get("repair_role", ""), record.get("head_sha", ""),
    )
    if len(successes) != 1:
        return None
    try:
        transition = safe_receipt(args.state_dir / f"{args.ticket}.json")
    except (FileNotFoundError, json.JSONDecodeError, OSError, StateError):
        return None
    success = successes[0]
    before = success.get("role_head_before", "")
    history = passport.get("factory_release_history")
    migrations = passport.get("migration_history")
    current_head = git(args.workdir, "rev-parse", "HEAD")
    if (
        not isinstance(history, list)
        or not isinstance(migrations, list)
        or transition.get("consumed") is not True
        or transition.get("ticket") != args.ticket
        or transition.get("project") != args.project
        or transition.get("branch") != passport.get("branch")
        or transition.get("contract_version") != args.contract_version
        or transition.get("stage") != "FIX test-author"
        or transition.get("role") != "test-author"
        or transition.get("factory_sha") != record.get("factory_sha")
        or transition.get("head_sha") != record.get("head_sha")
        or before != record.get("head_sha")
        or success.get("kit_sha") != transition.get("factory_sha")
        or success.get("contract_version") != args.contract_version
        or success.get("transition_receipt_sha256")
        != transition.get("receipt_sha256")
        or passport.get("ticket") != args.ticket
        or passport.get("factory_sha") != args.factory_sha
        or passport.get("head_sha") != current_head
    ):
        return None
    releases = [
        item.get("factory_sha")
        for item in history
        if isinstance(item, dict)
        and item.get("contract_version") == args.contract_version
        and SHA.fullmatch(item.get("factory_sha", ""))
    ]
    if (
        len(releases) != len(history)
        or len(releases) != len(set(releases))
        or transition.get("factory_sha") not in releases
        or args.factory_sha not in releases
        or releases.index(transition["factory_sha"])
        > releases.index(args.factory_sha)
    ):
        return None
    starts = []
    for index, edge in enumerate(migrations):
        terminal_head = (
            edge.get("from_head_sha", "") if isinstance(edge, dict) else ""
        )
        if (
            not isinstance(edge, dict)
            or edge.get("schema") != PASSPORT_MIGRATION_SCHEMA
            or edge.get("from_factory_sha") != success.get("kit_sha")
            or not SHA.fullmatch(terminal_head)
            or terminal_head == before
            or subprocess.run(
                [
                    "git", "-C", str(args.workdir), "merge-base",
                    "--is-ancestor", before, terminal_head,
                ],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                check=False, timeout=120,
            ).returncode != 0
        ):
            continue
        suffix = migrations[index:]
        if (
            all(
                isinstance(item, dict)
                and item.get("schema") == PASSPORT_MIGRATION_SCHEMA
                and "lineage_authorization_sha256" not in item
                and all(
                    SHA.fullmatch(item.get(name, ""))
                    for name in (
                        "from_factory_sha", "from_head_sha",
                        "from_protected_base_sha", "to_factory_sha",
                        "to_head_sha", "to_protected_base_sha",
                    )
                )
                and all(
                    DIGEST.fullmatch(item.get(name, ""))
                    for name in (
                        "from_passport_file_sha256",
                        "from_passport_sha256", "from_route_plan_sha256",
                        "to_route_plan_sha256",
                    )
                )
                for item in suffix
            )
            and all(
                prior["to_factory_sha"] == following["from_factory_sha"]
                and prior["to_head_sha"] == following["from_head_sha"]
                and prior["to_protected_base_sha"]
                == following["from_protected_base_sha"]
                for prior, following in zip(suffix, suffix[1:])
            )
            and suffix[-1].get("to_factory_sha") == args.factory_sha
            and suffix[-1].get("to_head_sha") == current_head
            and suffix[-1].get("to_protected_base_sha")
            == passport.get("protected_base_sha")
            and suffix[-1].get("to_route_plan_sha256")
            == passport.get("route_plan_sha256")
            and suffix[-1].get("from_passport_file_sha256")
            == passport.get("parent_file_sha256")
            and suffix[-1].get("from_passport_sha256")
            == passport.get("parent_digest")
        ):
            starts.append(terminal_head)
    return starts[0] if len(starts) == 1 else None


def ensure_dependency_conflict_repair(args: argparse.Namespace) -> None:
    receipt_path = (
        args.workdir / "factory" / "attestations" / args.ticket
        / "dependency-refresh.json"
    )
    completed_dir = args.state_dir / "contract-repairs" / "completed"
    has_completed_candidate = False
    if completed_dir.exists() or completed_dir.is_symlink():
        info = completed_dir.lstat()
        if (
            completed_dir.is_symlink()
            or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o700
        ):
            raise StateError("completed contract repair directory is unsafe")
        has_completed_candidate = any(
            completed_dir.glob(f"{args.ticket}-*.json")
        )
    if not os.path.lexists(receipt_path) and not has_completed_candidate:
        return
    passport, secret = authenticated_passport(args)
    found = dependency_conflict_receipt(args, require_current_base=False)
    if found is None:
        if (
            completed_dependency_conflict_records(args, secret)
            and not receipt_path.exists()
            and not receipt_path.is_symlink()
        ):
            raise StateError(
                "completed dependency conflict receipt was deleted"
            )
        return
    receipt, receipt_digest, receipt_commit = found
    if completed_dependency_conflict_repairs(
        args, secret, receipt_digest, receipt_commit,
    ):
        return
    current_head = git(args.workdir, "rev-parse", "HEAD")
    if (
        passport.get("factory_sha") != args.factory_sha
        or passport.get("head_sha") != current_head
    ):
        migrate_passport(args)
        passport, secret = authenticated_passport(args)
    current_base = protected_base_sha(args)
    passport_base = passport.get("protected_base_sha", "")
    receipt_to_passport = subprocess.run(
        [
            "git", "-C", str(args.workdir), "merge-base", "--is-ancestor",
            receipt["protected_head"], passport_base,
        ],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        check=False, timeout=120,
    )
    passport_to_current = subprocess.run(
        [
            "git", "-C", str(args.workdir), "merge-base", "--is-ancestor",
            passport_base, current_base,
        ],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        check=False, timeout=120,
    )
    active = load_repair(args, secret)
    active_matches = (
        active is not None
        and active.get("repair_source") == DEPENDENCY_CONFLICT_SOURCE
        and active.get("dependency_refresh_sha256") == receipt_digest
        and active.get("dependency_refresh_commit") == receipt_commit
    )
    active_migrated = (
        active_matches and migrated_contract_repair(args, passport, active)
    )
    active_completed_migrated = (
        active_matches
        and completed_dependency_conflict_migration_head(
            args, passport, active,
        ) is not None
    )
    if (
        passport.get("ticket") != args.ticket
        or passport.get("branch")
        != git(args.workdir, "symbolic-ref", "--quiet", "--short", "HEAD")
        or passport.get("factory_sha") != args.factory_sha
        or passport.get("head_sha") != current_head
        or not SHA.fullmatch(passport_base)
        or receipt_to_passport.returncode != 0
        or passport_to_current.returncode != 0
        or (
            not active_completed_migrated
            and not branch_contains(args, passport_base)
        )
        or (
            not active_migrated
            and not dependency_conflict_migrated(
                args, passport, receipt, receipt_commit,
            )
        )
    ):
        raise StateError("dependency conflict passport is invalid")
    path = repair_path(args)
    if active is not None:
        if active_matches and active_migrated:
            return
        raise StateError("dependency conflict repair conflicts with active repair")
    validate_dependency_conflict_transition(args, receipt)
    write_atomic(
        path,
        signed_repair({
            "blocked_receipt": receipt["transition_receipt_sha256"],
            "blocked_role": "dependency-refresh",
            "branch": passport["branch"],
            "dependency_refresh_commit": receipt_commit,
            "dependency_refresh_sha256": receipt_digest,
            "factory_sha": args.factory_sha,
            "head_sha": current_head,
            "head_tree": git(
                args.workdir, "rev-parse", f"{current_head}^{{tree}}",
            ),
            "passport_sha256": passport["passport_sha256"],
            "protected_base_sha": passport_base,
            "repair_role": "test-author",
            "repair_source": DEPENDENCY_CONFLICT_SOURCE,
            "schema": REPAIR_SCHEMA,
            "ticket": args.ticket,
        }, secret),
    )


def completed_repair_migration_split(
    args: argparse.Namespace,
    passport: dict[str, Any],
    success: dict[str, str],
    transition: dict[str, Any],
) -> int | None:
    """Locate a trusted migration suffix created after one repair success."""
    migrations = passport.get("migration_history")
    current_head = git(args.workdir, "rev-parse", "HEAD")
    before = success.get("role_head_before", "")
    if (
        not isinstance(migrations, list)
        or not SHA.fullmatch(before)
        or success.get("kit_sha") != transition.get("factory_sha")
    ):
        return None
    if (
        passport.get("factory_sha") == transition.get("factory_sha")
        and passport.get("head_sha") == current_head
        and passport.get("parent_file_sha256")
        == transition.get("passport_sha256")
    ):
        return len(migrations)
    candidates = []
    for index, edge in enumerate(migrations):
        if (
            not isinstance(edge, dict)
            or edge.get("schema") != PASSPORT_MIGRATION_SCHEMA
            or edge.get("from_factory_sha") != transition.get("factory_sha")
            or edge.get("from_head_sha") == before
            or subprocess.run(
                [
                    "git", "-C", str(args.workdir), "merge-base",
                    "--is-ancestor", before, edge.get("from_head_sha", ""),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=120,
            ).returncode != 0
        ):
            continue
        suffix = migrations[index:]
        if (
            all(
                isinstance(item, dict)
                and item.get("schema") == PASSPORT_MIGRATION_SCHEMA
                and all(
                    isinstance(item.get(name), str)
                    and SHA.fullmatch(item[name])
                    for name in (
                        "from_factory_sha", "from_head_sha",
                        "from_protected_base_sha", "to_factory_sha",
                        "to_head_sha", "to_protected_base_sha",
                    )
                )
                and all(
                    isinstance(item.get(name), str)
                    and DIGEST.fullmatch(item[name])
                    for name in (
                        "from_passport_file_sha256",
                        "from_passport_sha256", "from_route_plan_sha256",
                        "to_route_plan_sha256",
                    )
                )
                for item in suffix
            )
            and all(
                prior["to_factory_sha"] == following["from_factory_sha"]
                and prior["to_head_sha"] == following["from_head_sha"]
                and prior["to_protected_base_sha"]
                == following["from_protected_base_sha"]
                and prior["to_route_plan_sha256"]
                == following["from_route_plan_sha256"]
                for prior, following in zip(suffix, suffix[1:])
            )
            and suffix[-1]["to_factory_sha"] == args.factory_sha
            and suffix[-1]["to_head_sha"] == current_head
            and suffix[-1]["to_protected_base_sha"]
            == passport.get("protected_base_sha")
            and suffix[-1]["to_route_plan_sha256"]
            == passport.get("route_plan_sha256")
            and suffix[-1]["from_passport_file_sha256"]
            == passport.get("parent_file_sha256")
            and suffix[-1]["from_passport_sha256"]
            == passport.get("parent_digest")
        ):
            candidates.append(index)
    return candidates[0] if len(candidates) == 1 else None


def migrated_contract_repair(
    args: argparse.Namespace,
    passport: dict[str, Any],
    record: dict[str, Any],
    success: dict[str, str] | None = None,
    authenticated_head: str | None = None,
) -> bool:
    if record.get("factory_sha") == args.factory_sha:
        return True
    history = passport.get("factory_release_history")
    migrations = passport.get("migration_history")
    charges = passport.get("charge_records")
    completed = passport.get("completed_role_evidence")
    record_factory = record.get("factory_sha", "")
    record_head = record.get("head_sha", "")
    record_passport = record.get("passport_sha256", "")
    current_head = authenticated_head or git(args.workdir, "rev-parse", "HEAD")
    migration_target_head = current_head
    migration_target_factory = args.factory_sha
    migration_target_base = passport.get("protected_base_sha")
    migration_target_route = passport.get("route_plan_sha256")
    migration_limit = len(migrations) if isinstance(migrations, list) else 0
    if success is not None:
        if (
            record.get("repair_source") is not None
            or not completed_migrated_contract_repair(
                args, passport, record, success,
            )
        ):
            return False
        try:
            transition = safe_receipt(
                args.state_dir / f"{args.ticket}.json"
            )
        except (FileNotFoundError, json.JSONDecodeError, OSError, StateError):
            return False
        split = completed_repair_migration_split(
            args, passport, success, transition,
        )
        if split is None:
            return False
        migration_limit = split
        migration_target_head = success["role_head_before"]
        migration_target_factory = transition.get("factory_sha")
        if split < len(migrations):
            migration_target_base = migrations[split].get(
                "from_protected_base_sha"
            )
            migration_target_route = migrations[split].get(
                "from_route_plan_sha256"
            )
    if record.get("repair_source") == DEPENDENCY_CONFLICT_SOURCE:
        try:
            transition = safe_receipt(
                args.state_dir / f"{args.ticket}.json"
            )
        except (FileNotFoundError, json.JSONDecodeError, OSError, StateError):
            return False
        if (
            transition.get("factory_sha") == args.factory_sha
            and transition.get("stage") == "FIX test-author"
            and transition.get("role") == "test-author"
            and SHA.fullmatch(transition.get("head_sha", ""))
        ):
            migration_target_head = transition["head_sha"]
    if (
        not isinstance(history, list)
        or not isinstance(migrations, list)
        or not isinstance(charges, list)
        or not isinstance(completed, list)
        or not isinstance(record_passport, str)
    ):
        return False
    releases = [
        item.get("factory_sha")
        for item in history
        if isinstance(item, dict)
        and item.get("contract_version") == args.contract_version
        and isinstance(item.get("factory_sha"), str)
        and SHA.fullmatch(item["factory_sha"])
    ]
    if (
        len(releases) != len(history)
        or len(releases) != len(set(releases))
        or record_factory not in releases
        or args.factory_sha not in releases
        or releases.index(record_factory) >= releases.index(args.factory_sha)
        or not isinstance(migrations, list)
        or not isinstance(charges, list)
        or not isinstance(completed, list)
        or not DIGEST.fullmatch(record_passport)
        or passport.get("ticket") != args.ticket
        or passport.get("factory_sha") != args.factory_sha
        or passport.get("head_sha") != current_head
    ):
        return False
    blocked_repair_migration = False
    if success is None and record.get("repair_source") is None:
        try:
            transition = safe_receipt(
                args.state_dir / f"{args.ticket}.json"
            )
            migrated_passport, charge = migrated_contract_block(
                args, transition
            )
            owner = record.get("repair_role", "")
            if (
                migrated_passport == passport
                and charge is not None
                and transition.get("consumed") is True
                and transition.get("factory_sha") == record_factory
                and transition.get("head_sha") == record_head
                and transition.get("parent_digest")
                == record.get("blocked_receipt")
                and transition.get("role") == owner
                and transition.get("stage") == f"FIX {owner}"
                and passport.get("current_stage") == f"FIX {owner}"
                and passport.get("transition_receipt_sha256")
                == transition.get("receipt_sha256")
            ):
                contract_block_terminal(args, transition, charge)
                blocked_repair_migration = True
        except (
            FileNotFoundError, json.JSONDecodeError, OSError, StateError,
        ):
            pass
    starts = []
    for index, edge in enumerate(migrations):
        direct_start = (
            isinstance(edge, dict)
            and edge.get("from_head_sha") == record_head
            and edge.get("from_passport_sha256") == record_passport
        )
        blocked_start = (
            blocked_repair_migration
            and isinstance(edge, dict)
            and SHA.fullmatch(edge.get("from_head_sha", ""))
            and subprocess.run(
                [
                    "git", "-C", str(args.workdir), "merge-base",
                    "--is-ancestor", record_head, edge["from_head_sha"],
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=120,
            ).returncode == 0
        )
        if (
            not isinstance(edge, dict)
            or edge.get("schema") != PASSPORT_MIGRATION_SCHEMA
            or edge.get("from_factory_sha") != record_factory
            or not (direct_start or blocked_start)
        ):
            continue
        suffix = migrations[index:migration_limit]
        if (
            suffix
            and all(
                isinstance(item, dict)
                and item.get("schema") == PASSPORT_MIGRATION_SCHEMA
                and all(
                    isinstance(item.get(name), str)
                    and SHA.fullmatch(item[name])
                    for name in (
                        "from_factory_sha", "from_head_sha",
                        "from_protected_base_sha", "to_factory_sha",
                        "to_head_sha", "to_protected_base_sha",
                    )
                )
                and all(
                    isinstance(item.get(name), str)
                    and DIGEST.fullmatch(item[name])
                    for name in (
                        "from_passport_file_sha256",
                        "from_passport_sha256", "from_route_plan_sha256",
                        "to_route_plan_sha256",
                    )
                )
                for item in suffix
            )
            and all(
                prior["to_factory_sha"] == following["from_factory_sha"]
                and prior["to_head_sha"] == following["from_head_sha"]
                and prior["to_protected_base_sha"]
                == following["from_protected_base_sha"]
                and prior["to_route_plan_sha256"]
                == following["from_route_plan_sha256"]
                for prior, following in zip(suffix, suffix[1:])
            )
            and suffix[-1]["to_factory_sha"] == migration_target_factory
            and suffix[-1]["to_head_sha"] == migration_target_head
            and suffix[-1]["to_protected_base_sha"]
            == migration_target_base
            and suffix[-1]["to_route_plan_sha256"] == migration_target_route
        ):
            starts.append(index)
    if record.get("repair_source") == DEPENDENCY_CONFLICT_SOURCE:
        return (
            len(starts) == 1
            or completed_dependency_conflict_migration_head(
                args, passport, record,
            ) is not None
        )
    blocked = [
        item for item in charges
        if isinstance(item, dict)
        and item.get("transition_receipt_sha256")
        == record.get("blocked_receipt")
        and item.get("role") == record.get("blocked_role")
    ]
    return (
        len(starts) == 1
        and len(blocked) == 1
        and not any(
            isinstance(item, dict)
            and item.get("transition_receipt_sha256")
            == record.get("blocked_receipt")
            for item in completed
        )
    )


def completed_migrated_contract_repair(
    args: argparse.Namespace,
    passport: dict[str, Any],
    record: dict[str, Any],
    success: dict[str, str],
) -> bool:
    """Bind one migrated repair success to its exact role input and passport."""
    try:
        transition = safe_receipt(args.state_dir / f"{args.ticket}.json")
    except (FileNotFoundError, json.JSONDecodeError, OSError, StateError):
        return False
    completed = passport.get("completed_role_evidence")
    charges = passport.get("charge_records")
    current_head = git(args.workdir, "rev-parse", "HEAD")
    before = success.get("role_head_before", "")
    branch = git(
        args.workdir, "symbolic-ref", "--quiet", "--short", "HEAD",
    )
    owner = record.get("repair_role", "")
    stage = f"FIX {owner}"
    origin = os.environ.get("FACTORY_CERTIFIED_PRODUCT_ORIGIN", "")
    origin_digest = (
        hashlib.sha256(origin.encode()).hexdigest() if origin else ""
    )
    accounted = success.get("accounting_state") == "completed" or (
        success.get("accounting_state") == "abandoned_conservative"
        and success.get("cost_basis") == "conservative_reservation"
        and success.get("effective_cost") == success.get("reserved_usd")
    )
    migration_split = completed_repair_migration_split(
        args, passport, success, transition,
    )
    if (
        not isinstance(completed, list)
        or not isinstance(charges, list)
        or not SHA.fullmatch(before)
        or before == current_head
        or passport.get("ticket") != args.ticket
        or passport.get("project") != args.project
        or passport.get("contract_version") != args.contract_version
        or passport.get("product_origin_sha256") != origin_digest
        or passport.get("branch") != branch
        or passport.get("factory_sha") != args.factory_sha
        or passport.get("head_sha") != current_head
        or not DIGEST.fullmatch(transition.get("passport_sha256", ""))
        or migration_split is None
        or passport.get("current_stage") != stage
        or passport.get("transition_receipt_sha256")
        != transition.get("receipt_sha256")
        or transition.get("consumed") is not True
        or transition.get("ticket") != args.ticket
        or transition.get("project") != args.project
        or transition.get("branch") != branch
        or not SHA.fullmatch(transition.get("factory_sha", ""))
        or transition.get("contract_version") != args.contract_version
        or transition.get("product_origin_sha256") != origin_digest
        or transition.get("role") != owner
        or transition.get("stage") != stage
        or transition.get("head_sha") != before
        or transition.get("head_tree")
        != git(args.workdir, "rev-parse", f"{before}^{{tree}}")
        or success.get("transition_receipt_sha256")
        != transition.get("receipt_sha256")
        or success.get("kit_sha") != transition.get("factory_sha")
        or success.get("contract_version") != args.contract_version
        or success.get("role_branch_before") != branch
        or not isinstance(success.get("run_id"), str)
        or not success.get("run_id")
        or not DIGEST.fullmatch(success.get("manifest_sha256", ""))
        or not DIGEST.fullmatch(success.get("output_sha256", ""))
        or success.get("go_issued") != "1"
        or success.get("task_submitted") != "1"
        or not accounted
        or not branch_contains(args, before)
    ):
        return False
    matching_completed = [
        item for item in completed
        if isinstance(item, dict)
        and item.get("contract_version") == args.contract_version
        and item.get("factory_sha") == transition.get("factory_sha")
        and item.get("head_before") == before
        and item.get("manifest_sha256") == success["manifest_sha256"]
        and item.get("output_sha256") == success["output_sha256"]
        and item.get("role") == owner
        and item.get("run_id") == success["run_id"]
        and item.get("transition_receipt_sha256")
        == transition["receipt_sha256"]
    ]
    matching_charges = [
        item for item in charges
        if isinstance(item, dict)
        and isinstance(item.get("charge_micro_usd"), int)
        and not isinstance(item.get("charge_micro_usd"), bool)
        and item["charge_micro_usd"] >= 0
        and item.get("accounting_state") == success.get("accounting_state")
        and item.get("contract_version") == args.contract_version
        and item.get("factory_sha") == transition.get("factory_sha")
        and item.get("head_before") == before
        and item.get("manifest_sha256") == success["manifest_sha256"]
        and item.get("role") == owner
        and item.get("run_id") == success["run_id"]
        and item.get("transition_receipt_sha256")
        == transition["receipt_sha256"]
    ]
    blocked = [
        item for item in charges
        if isinstance(item, dict)
        and item.get("transition_receipt_sha256")
        == record.get("blocked_receipt")
        and item.get("role") == record.get("blocked_role")
    ]
    return (
        len(matching_completed) == 1
        and len(matching_charges) == 1
        and len(blocked) == 1
        and not any(
            isinstance(item, dict)
            and item.get("transition_receipt_sha256")
            == record.get("blocked_receipt")
            for item in completed
        )
    )


def contract_repair_successes(
    args: argparse.Namespace, owner: str, head: str
) -> list[dict[str, str]]:
    successes = []
    for path in sorted((args.factory_root / "factory/runs").glob("*.meta")):
        fields, raw = run_manifest(path)
        before = fields.get("role_head_before", "")
        if (
            fields.get("ticket") == args.ticket
            and fields.get("role") == owner
            and fields.get("phase") == "completed"
            and fields.get("exit_status") == "0"
            and fields.get("role_exit") == "ok"
            and SHA.fullmatch(before)
            and subprocess.run(
                [
                    "git", "-C", str(args.workdir), "merge-base",
                    "--is-ancestor", head, before,
                ],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                check=False, timeout=120,
            ).returncode == 0
        ):
            successes.append({
                **fields,
                "manifest_sha256": hashlib.sha256(raw).hexdigest(),
            })
    return successes


def dependency_conflict_successes(
    args: argparse.Namespace,
    record: dict[str, Any],
    receipt: dict[str, Any],
    successes: list[dict[str, str]],
    passport: dict[str, Any],
    migrated: bool,
) -> list[dict[str, str]]:
    try:
        transition = safe_receipt(args.state_dir / f"{args.ticket}.json")
    except (FileNotFoundError, json.JSONDecodeError, OSError, StateError):
        if successes:
            raise StateError(
                "dependency conflict repair success lacks its FIX receipt"
            ) from None
        return []
    candidates = [
        item for item in successes
        if item.get("transition_receipt_sha256")
        == transition.get("receipt_sha256")
    ]
    if not candidates:
        if successes and git(args.workdir, "rev-parse", "HEAD") != record["head_sha"]:
            raise StateError(
                "dependency conflict repair success used the wrong receipt"
            )
        return []
    if len(candidates) != 1:
        raise StateError("dependency conflict repair success is ambiguous")
    success = candidates[0]
    before = success.get("role_head_before", "")
    current_head = git(args.workdir, "rev-parse", "HEAD")
    migrated_repair_head = completed_dependency_conflict_migration_head(
        args, passport, record,
    ) if migrated else None
    repair_head = migrated_repair_head or current_head
    branch = git(
        args.workdir, "symbolic-ref", "--quiet", "--short", "HEAD",
    )
    accounted = success.get("accounting_state") == "completed" or (
        success.get("accounting_state") == "abandoned_conservative"
        and success.get("cost_basis") == "conservative_reservation"
        and success.get("effective_cost") == success.get("reserved_usd")
    )
    if (
        transition.get("consumed") is not True
        or transition.get("ticket") != args.ticket
        or transition.get("project") != args.project
        or transition.get("branch") != branch
        or transition.get("factory_sha") != success.get("kit_sha")
        or transition.get("contract_version") != args.contract_version
        or transition.get("stage") != "FIX test-author"
        or transition.get("role") != "test-author"
        or transition.get("head_sha") != before
        or (
            before != record.get("head_sha")
            and not (
                record.get("factory_sha") != args.factory_sha
                and migrated
            )
        )
        or transition.get("head_tree")
        != git(args.workdir, "rev-parse", f"{before}^{{tree}}")
        or (
            success.get("kit_sha") != args.factory_sha
            and migrated_repair_head is None
        )
        or success.get("contract_version") != args.contract_version
        or success.get("role_branch_before") != branch
        or not isinstance(success.get("run_id"), str)
        or not success.get("run_id")
        or not DIGEST.fullmatch(success.get("manifest_sha256", ""))
        or success.get("go_issued") != "1"
        or success.get("task_submitted") != "1"
        or not accounted
        or not branch_contains(args, before)
        or repair_head == before
    ):
        raise StateError("dependency conflict repair success is invalid")
    completed = passport.get("completed_role_evidence")
    charges = passport.get("charge_records")
    expected = {
        "contract_version": args.contract_version,
        "factory_sha": success.get("kit_sha"),
        "head_before": before,
        "manifest_sha256": success["manifest_sha256"],
        "role": "test-author",
        "run_id": success.get("run_id"),
        "transition_receipt_sha256": transition["receipt_sha256"],
    }
    matching_completed = [
        item for item in completed
        if isinstance(item, dict)
        and all(item.get(key) == value for key, value in expected.items())
        and DIGEST.fullmatch(item.get("output_sha256", ""))
    ] if isinstance(completed, list) else []
    matching_charges = [
        item for item in charges
        if isinstance(item, dict)
        and all(item.get(key) == value for key, value in expected.items())
        and item.get("accounting_state") == success.get("accounting_state")
        and isinstance(item.get("charge_micro_usd"), int)
        and not isinstance(item.get("charge_micro_usd"), bool)
        and item.get("charge_micro_usd", -1) >= 0
    ] if isinstance(charges, list) else []
    if (
        passport.get("ticket") != args.ticket
        or passport.get("branch") != branch
        or passport.get("factory_sha") != args.factory_sha
        or passport.get("head_sha") != current_head
        or passport.get("current_stage") != "FIX test-author"
        or passport.get("transition_receipt_sha256")
        != transition["receipt_sha256"]
        or len(matching_completed) != 1
        or len(matching_charges) != 1
    ):
        raise StateError(
            "dependency conflict repair passport evidence is invalid"
        )
    raw_status = subprocess.run(
        [
            "git", "-C", str(args.workdir), "diff", "--name-status",
            "--no-renames", "-z", before, repair_head,
        ],
        capture_output=True, check=True, timeout=120,
    ).stdout.split(b"\0")
    if raw_status and raw_status[-1] == b"":
        raw_status.pop()
    if not raw_status or len(raw_status) % 2:
        raise StateError("dependency conflict repair diff is invalid")
    changed = []
    for index in range(0, len(raw_status), 2):
        try:
            status = raw_status[index].decode("ascii")
            path = raw_status[index + 1].decode("ascii")
        except UnicodeDecodeError as error:
            raise StateError(
                "dependency conflict repair diff is invalid"
            ) from error
        if status != "M":
            raise StateError(
                "dependency conflict repair changed an unauthorized path"
            )
        changed.append(path)
    allowed = {
        f"factory/tickets/{args.ticket}.md",
        *(
            item["path"]
            for item in receipt["conflicts"]
            if isinstance(item, dict) and isinstance(item.get("path"), str)
        ),
    }
    if (
        not changed
        or changed != sorted(changed)
        or len(changed) != len(set(changed))
        or set(changed) - allowed
        or any(
            tree_entry(args.workdir, before, path)[0] != "100644"
            or tree_entry(args.workdir, repair_head, path)[0] != "100644"
            for path in changed
        )
    ):
        raise StateError("dependency conflict repair changed an unauthorized path")
    return candidates


def completed_repair_after_lost_migration_history(
    args: argparse.Namespace,
    passport: dict[str, Any],
    record: dict[str, Any],
    successes: list[dict[str, str]],
) -> bool:
    """Recognize the exact terminal export that dropped migration history.

    Contract 1.8 exports before this repair rebuilt the passport without
    carrying a consumed legacy migration history. Recovery is safe only after
    the already-authorized repair role completed: the consumed FIX receipt
    binds the terminal evidence, and the current authenticated passport is
    either that direct export or its contiguous v2 migration successor.
    """
    if len(successes) != 1:
        return False
    try:
        transition = safe_receipt(args.state_dir / f"{args.ticket}.json")
    except (FileNotFoundError, json.JSONDecodeError, OSError, StateError):
        return False
    success = successes[0]
    completed = passport.get("completed_role_evidence")
    charges = passport.get("charge_records")
    history = passport.get("factory_release_history")
    migrations = passport.get("migration_history")
    current_head = git(args.workdir, "rev-parse", "HEAD")
    direct_export = (
        "migration_history" not in passport
        and passport.get("factory_sha") == transition.get("factory_sha")
        and passport.get("parent_file_sha256")
        == transition.get("passport_sha256")
    )
    migrated_export = (
        isinstance(migrations, list)
        and bool(migrations)
        and all(
            isinstance(item, dict)
            and item.get("schema") == PASSPORT_MIGRATION_SCHEMA
            and "lineage_authorization_sha256" not in item
            and all(
                SHA.fullmatch(item.get(name, ""))
                for name in (
                    "from_factory_sha", "from_head_sha",
                    "from_protected_base_sha", "to_factory_sha",
                    "to_head_sha", "to_protected_base_sha",
                )
            )
            and all(
                DIGEST.fullmatch(item.get(name, ""))
                for name in (
                    "from_passport_file_sha256",
                    "from_passport_sha256", "from_route_plan_sha256",
                    "to_route_plan_sha256",
                )
            )
            for item in migrations
        )
        and all(
            prior["to_factory_sha"] == following["from_factory_sha"]
            and prior["to_head_sha"] == following["from_head_sha"]
            and prior["to_protected_base_sha"]
            == following["from_protected_base_sha"]
            for prior, following in zip(migrations, migrations[1:])
        )
        and migrations[0].get("from_factory_sha")
        == transition.get("factory_sha")
        and subprocess.run(
            [
                "git", "-C", str(args.workdir), "merge-base", "--is-ancestor",
                success.get("role_head_before", ""),
                migrations[0].get("from_head_sha", ""),
            ],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            check=False, timeout=120,
        ).returncode == 0
        and migrations[-1].get("to_factory_sha") == args.factory_sha
        and migrations[-1].get("to_head_sha") == current_head
        and migrations[-1].get("to_protected_base_sha")
        == passport.get("protected_base_sha")
        and migrations[-1].get("to_route_plan_sha256")
        == passport.get("route_plan_sha256")
        and migrations[-1].get("from_passport_file_sha256")
        == passport.get("parent_file_sha256")
        and migrations[-1].get("from_passport_sha256")
        == passport.get("parent_digest")
    )
    if (
        not isinstance(completed, list)
        or not isinstance(charges, list)
        or not isinstance(history, list)
        or not (direct_export or migrated_export)
        or passport.get("ticket") != args.ticket
        or passport.get("factory_sha") != args.factory_sha
        or passport.get("head_sha") != current_head
        or passport.get("current_stage")
        != f"FIX {record.get('repair_role', '')}"
        or passport.get("transition_receipt_sha256")
        != transition.get("receipt_sha256")
        or transition.get("consumed") is not True
        or transition.get("ticket") != args.ticket
        or transition.get("project") != args.project
        or transition.get("branch") != passport.get("branch")
        or not SHA.fullmatch(transition.get("factory_sha", ""))
        or transition.get("role") != record.get("repair_role")
        or transition.get("stage")
        != f"FIX {record.get('repair_role', '')}"
        or transition.get("head_sha") != success.get("role_head_before")
        or success.get("transition_receipt_sha256")
        != transition.get("receipt_sha256")
        or success.get("kit_sha") != transition.get("factory_sha")
        or subprocess.run(
            [
                "git", "-C", str(args.workdir), "merge-base", "--is-ancestor",
                success.get("role_head_before", ""), current_head,
            ],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            check=False, timeout=120,
        ).returncode != 0
    ):
        return False
    releases = [
        item.get("factory_sha")
        for item in history
        if isinstance(item, dict)
        and item.get("contract_version") == args.contract_version
        and SHA.fullmatch(item.get("factory_sha", ""))
    ]
    if (
        len(releases) != len(history)
        or len(releases) != len(set(releases))
        or record.get("factory_sha") not in releases
        or transition.get("factory_sha") not in releases
        or args.factory_sha not in releases
        or releases.index(record["factory_sha"])
        >= releases.index(transition["factory_sha"])
        or releases.index(transition["factory_sha"]) > releases.index(args.factory_sha)
    ):
        return False
    matching_completed = [
        item for item in completed
        if isinstance(item, dict)
        and item.get("role") == record.get("repair_role")
        and item.get("run_id") == success.get("run_id")
        and item.get("transition_receipt_sha256")
        == transition.get("receipt_sha256")
        and item.get("manifest_sha256") == success.get("manifest_sha256")
        and item.get("factory_sha") == transition.get("factory_sha")
        and item.get("head_before") == success.get("role_head_before")
    ]
    matching_charges = [
        item for item in charges
        if isinstance(item, dict)
        and item.get("role") == record.get("repair_role")
        and item.get("run_id") == success.get("run_id")
        and item.get("transition_receipt_sha256")
        == transition.get("receipt_sha256")
        and item.get("manifest_sha256") == success.get("manifest_sha256")
        and item.get("factory_sha") == transition.get("factory_sha")
        and item.get("head_before") == success.get("role_head_before")
    ]
    blocked = [
        item for item in charges
        if isinstance(item, dict)
        and item.get("transition_receipt_sha256")
        == record.get("blocked_receipt")
        and item.get("role") == record.get("blocked_role")
    ]
    return (
        len(matching_completed) == 1
        and len(matching_charges) == 1
        and len(blocked) == 1
        and not any(
            isinstance(item, dict)
            and item.get("transition_receipt_sha256")
            == record.get("blocked_receipt")
            for item in completed
        )
    )


def retire_contract_repair(
    args: argparse.Namespace, record: dict[str, Any]
) -> None:
    source = repair_path(args)
    archive = source.parent / "completed"
    archive.mkdir(mode=0o700, exist_ok=True)
    info = archive.lstat()
    if (
        archive.is_symlink()
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise StateError("completed contract repair directory is unsafe")
    digest = record.get("repair_sha256", "")
    if not DIGEST.fullmatch(digest):
        raise StateError("contract repair record is invalid")
    destination = archive / f"{args.ticket}-{digest}.json"
    if destination.exists() or destination.is_symlink():
        if (
            destination.is_symlink()
            or json.loads(destination.read_text(encoding="utf-8")) != record
        ):
            raise StateError("completed contract repair record conflicts")
        source.unlink(missing_ok=True)
        return
    os.replace(source, destination)


def contract_repair_stage(args: argparse.Namespace) -> tuple[str | None, bool]:
    text = (
        args.workdir / "factory" / "tickets" / f"{args.ticket}.md"
    ).read_text(encoding="utf-8")
    has_directive = bool(
        re.search(r"^OPERATOR RESUME(?::| RECEIPT:)", text, re.M)
    )
    path = args.state_dir / "contract-repairs" / f"{args.ticket}.json"
    if not has_directive and not path.exists() and not path.is_symlink():
        return None, False
    passport, secret = authenticated_passport(args)
    record = load_repair(args, secret)
    if record is None:
        if has_directive:
            directives = re.findall(
                r"^OPERATOR RESUME: (planner|spec-linter|test-author|builder)$",
                text,
                re.M,
            )
            receipts = re.findall(
                r"^OPERATOR RESUME RECEIPT: ([0-9a-f]{64})$",
                text,
                re.M,
            )
            if (
                len(directives) != 1
                or len(receipts) != 1
                or not completed_repair_matches_directive(
                    args, passport, secret, directives[0], receipts[0]
                )
            ):
                raise StateError(
                    "operator resume lacks authenticated contract repair state"
                )
            current = current_state(args.workdir, args.ticket)
            if directives[0] == "planner" and current in {"Building", "Review"}:
                stage = resolve(args)
                role = stage_role(stage)
                order = {"Planning": 1, "Building": 2, "Review": 3}
                if (
                    role is not None
                    and order[TARGET_STATE[role]] < order[current]
                ):
                    return stage, True
        return None, False
    owner = record.get("repair_role", "")
    head = record.get("head_sha", "")
    source = record.get("repair_source")
    if any((
        record.get("schema") != REPAIR_SCHEMA,
        record.get("ticket") != args.ticket,
        record.get("branch") != passport.get("branch"),
        owner not in {"planner", "spec-linter", "test-author", "builder"},
        source not in {None, DEPENDENCY_CONFLICT_SOURCE},
        not SHA.fullmatch(head),
        subprocess.run(
            ["git", "-C", str(args.workdir), "merge-base", "--is-ancestor", head, "HEAD"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            check=False, timeout=120,
        ).returncode != 0,
    )):
        raise StateError("contract repair record is invalid")
    if source == DEPENDENCY_CONFLICT_SOURCE:
        expected_keys = {
            "authentication_sha256", "blocked_receipt", "blocked_role",
            "branch", "dependency_refresh_commit",
            "dependency_refresh_sha256", "factory_sha", "head_sha",
            "head_tree", "passport_sha256", "protected_base_sha",
            "repair_role", "repair_sha256", "repair_source", "schema",
            "ticket",
        }
        found = dependency_conflict_receipt(
            args, require_current_base=False,
        )
        if found is None:
            raise StateError("dependency conflict repair receipt is missing")
        conflict, conflict_digest, conflict_commit = found
        protected_lineage = subprocess.run(
            [
                "git", "-C", str(args.workdir), "merge-base",
                "--is-ancestor", conflict["protected_head"],
                record.get("protected_base_sha", ""),
            ],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            check=False, timeout=120,
        )
        if (
            set(record) != expected_keys
            or has_directive
            or owner != "test-author"
            or record.get("blocked_role") != "dependency-refresh"
            or record.get("blocked_receipt")
            != conflict.get("transition_receipt_sha256")
            or record.get("dependency_refresh_sha256") != conflict_digest
            or record.get("dependency_refresh_commit") != conflict_commit
            or record.get("head_tree")
            != git(
                args.workdir, "rev-parse",
                f"{record.get('head_sha', '')}^{{tree}}",
            )
            or not branch_contains(args, conflict_commit)
            or protected_lineage.returncode != 0
            or not DIGEST.fullmatch(record.get("passport_sha256", ""))
        ):
            raise StateError("dependency conflict repair record is invalid")
    successes = contract_repair_successes(args, owner, head)
    if source is None and len(successes) > 1:
        raise StateError("contract repair has duplicate successful evidence")
    authenticated_head = None
    if passport.get("head_sha") != git(args.workdir, "rev-parse", "HEAD"):
        if not has_directive or not args.receipt:
            raise StateError("contract repair record is invalid")
        operator_resume_role(args, passport, owner)
        authenticated_head = passport.get("head_sha")
    migrated = migrated_contract_repair(
        args,
        passport,
        record,
        successes[0] if source is None and successes else None,
        authenticated_head=authenticated_head,
    )
    if not migrated and (
        source == DEPENDENCY_CONFLICT_SOURCE
        or not completed_repair_after_lost_migration_history(
            args, passport, record, successes
        )
    ):
        raise StateError("contract repair record is invalid")
    if source == DEPENDENCY_CONFLICT_SOURCE:
        successes = dependency_conflict_successes(
            args, record, conflict, successes, passport, migrated,
        )
    if len(successes) > 1:
        raise StateError("contract repair has duplicate successful evidence")
    if not successes:
        return f"FIX {owner}", True
    retire_contract_repair(args, record)
    stage = resolve(args)
    role = stage_role(stage)
    order = {"Planning": 1, "Building": 2, "Review": 3}
    if (
        role is not None
        and order[TARGET_STATE[role]] < order[current_state(args.workdir, args.ticket)]
    ):
        return stage, True
    return stage, False


def core(
    args: argparse.Namespace, stage: str, role: str | None
) -> dict[str, Any]:
    workdir = args.workdir.resolve(strict=True)
    factory = args.factory_root.resolve(strict=True) / "factory"
    branch = git(workdir, "symbolic-ref", "--quiet", "--short", "HEAD")
    route = workdir / "factory" / "route-plans" / f"{args.ticket}.json"
    passport = args.state_dir / "passports" / f"{args.ticket}.json"
    origin = os.environ.get("FACTORY_CERTIFIED_PRODUCT_ORIGIN", "")
    if not origin or any(character in origin for character in "\n\r\t"):
        raise StateError("certified product origin is unavailable")
    return {
        "branch": branch,
        "contract_version": args.contract_version,
        "evidence_sha256": ticket_evidence_digest(factory, args.ticket),
        "factory_sha": args.factory_sha,
        "head_sha": git(workdir, "rev-parse", "HEAD"),
        "head_tree": git(workdir, "rev-parse", "HEAD^{tree}"),
        "lease_sha256": (
            hashlib.sha256(args.lease.encode()).hexdigest()
            if args.lease else None
        ),
        "passport_sha256": (
            hashlib.sha256(passport.read_bytes()).hexdigest()
            if passport.is_file() and not passport.is_symlink()
            else None
        ),
        "product_origin_sha256": hashlib.sha256(origin.encode()).hexdigest(),
        "project": args.project,
        "role": role,
        "route_plan_sha256": (
            hashlib.sha256(route.read_bytes()).hexdigest()
            if route.is_file() and not route.is_symlink()
            else None
        ),
        "schema": RECEIPT_SCHEMA,
        "stage": stage,
        "ticket": args.ticket,
        "ticket_blob": git(
            workdir, "rev-parse", f"HEAD:factory/tickets/{args.ticket}.md"
        ),
    }


def issue(args: argparse.Namespace, stage: str) -> dict[str, Any]:
    role = stage_role(stage)
    value = core(args, stage, role)
    path = args.state_dir / f"{args.ticket}.json"
    prior: dict[str, Any] | None = None
    if path.exists() or path.is_symlink():
        prior = safe_receipt(path)
        prior_core = {
            key: item for key, item in prior.items()
            if key not in {
                "consumed", "consumed_at_epoch", "nonce", "parent_digest",
                "receipt_sha256",
            }
        }
        if prior_core == value and not prior["consumed"]:
            return prior
        value["parent_digest"] = prior["receipt_sha256"]
    value["nonce"] = secrets.token_hex(16)
    immutable = dict(value)
    value["receipt_sha256"] = hashlib.sha256(canonical(immutable)).hexdigest()
    value["consumed"] = False
    write_atomic(path, value)
    return value


def verify(args: argparse.Namespace, *, consume: bool) -> dict[str, Any]:
    path = args.state_dir / f"{args.ticket}.json"
    lock_path = args.state_dir / ".lock"
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        with os.fdopen(descriptor, "r+") as lock:
            descriptor = -1
            fcntl.flock(lock, fcntl.LOCK_EX)
            value = safe_receipt(path)
            if value.get("receipt_sha256") != args.receipt:
                raise StateError("transition receipt does not match")
            expected = core(args, value["stage"], value.get("role"))
            actual = {
                key: item for key, item in value.items()
                if key not in {
                    "consumed", "consumed_at_epoch", "nonce", "parent_digest",
                    "receipt_sha256",
                }
            }
            if actual != expected:
                raise StateError("transition receipt inputs drifted")
            if args.role and value.get("role") != args.role:
                raise StateError("transition receipt does not authorize the role")
            if consume:
                if value["consumed"]:
                    raise StateError("transition receipt was already consumed")
                value["consumed"] = True
                value["consumed_at_epoch"] = int(__import__("time").time())
                write_atomic(path, value)
            elif args.require_used and not value["consumed"]:
                raise StateError("transition receipt has not been consumed")
            elif not args.require_used and value["consumed"]:
                raise StateError("transition receipt was already consumed")
            return value
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def run_helper(
    args: argparse.Namespace, script: str, *arguments: str,
    allow_refusal: bool = False,
    extra_environment: dict[str, str] | None = None,
) -> str:
    environment = {
        **os.environ,
        "FACTORY_ROOT": str(args.factory_root),
        "FACTORY_TRANSITION_STATE_DIR": str(args.state_dir),
        "FACTORY_RELEASE_SHA": args.factory_sha,
    }
    # This is an internal state-machine-to-resolver capability. Never accept
    # a caller-supplied path in place of authenticated passport evidence.
    environment.pop("FACTORY_AUTHENTICATED_ROLE_EVIDENCE", None)
    if extra_environment:
        environment.update(extra_environment)
    result = subprocess.run(
        ["/bin/bash", str(args.kit_dir / "scripts" / script), *arguments],
        cwd=args.workdir,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
        timeout=300,
    )
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    accepted_refusal = (
        allow_refusal
        and result.returncode == 1
        and not result.stderr.strip()
        and len(lines) == 1
        and lines[0].startswith("REFUSE ")
    )
    if result.returncode and not accepted_refusal:
        raise StateError(result.stderr.strip() or result.stdout.strip())
    return result.stdout.strip()


def authenticated_role_evidence(
    args: argparse.Namespace,
) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
    path = args.state_dir / "passports" / f"{args.ticket}.json"
    if not path.exists() and not path.is_symlink():
        return None
    passport, _ = authenticated_passport(args)
    workdir = args.workdir.resolve(strict=True)
    route = workdir / "factory" / "route-plans" / f"{args.ticket}.json"
    branch = git(workdir, "symbolic-ref", "--quiet", "--short", "HEAD")
    head = git(workdir, "rev-parse", "HEAD")
    route_digest = (
        hashlib.sha256(route.read_bytes()).hexdigest()
        if route.is_file() and not route.is_symlink()
        else None
    )
    completed = passport.get("completed_role_evidence")
    if (
        passport.get("ticket") != args.ticket
        or passport.get("project") != args.project
        or passport.get("contract_version") != args.contract_version
        or passport.get("factory_sha") != args.factory_sha
        or passport.get("branch") != branch
        or passport.get("head_sha") != head
        or passport.get("route_plan_sha256") != route_digest
        or not isinstance(completed, list)
    ):
        raise StateError("passport role evidence is outside current ticket identity")
    expected = {
        "contract_version", "factory_sha", "head_before", "manifest_sha256",
        "output_sha256", "role", "run_id", "transition_receipt_sha256",
    }
    run_ids: set[str] = set()
    receipts: set[str] = set()
    for item in completed:
        if (
            not isinstance(item, dict)
            or set(item) != expected
            or item.get("contract_version") != args.contract_version
            or not SHA.fullmatch(item.get("factory_sha", ""))
            or not SHA.fullmatch(item.get("head_before", ""))
            or not DIGEST.fullmatch(item.get("manifest_sha256", ""))
            or not DIGEST.fullmatch(item.get("output_sha256", ""))
            or not ROLE.fullmatch(item.get("role", ""))
            or not re.fullmatch(r"[A-Za-z0-9._-]{1,200}", item.get("run_id", ""))
            or not DIGEST.fullmatch(item.get("transition_receipt_sha256", ""))
            or item["run_id"] in run_ids
            or item["transition_receipt_sha256"] in receipts
        ):
            raise StateError("passport completed-role evidence is invalid")
        run_ids.add(item["run_id"])
        receipts.add(item["transition_receipt_sha256"])
    return passport, completed


def resolve(args: argparse.Namespace) -> str:
    command = ["--ticket", args.ticket]
    if args.lease:
        command.extend(["--lease", args.lease])
    command.extend(["--workdir", str(args.workdir)])
    evidence = authenticated_role_evidence(args)
    evidence_path: Path | None = None
    try:
        extra_environment = None
        if evidence is not None:
            passport, completed = evidence
            descriptor, temporary = tempfile.mkstemp(
                prefix=f".role-evidence-{args.ticket}.", dir=args.state_dir,
            )
            evidence_path = Path(temporary)
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(canonical({
                    "passport_sha256": passport["passport_sha256"],
                    "records": completed,
                    "schema": ROLE_EVIDENCE_SCHEMA,
                    "ticket": args.ticket,
                }))
                stream.flush()
                os.fsync(stream.fileno())
            extra_environment = {
                "FACTORY_AUTHENTICATED_ROLE_EVIDENCE": str(evidence_path),
            }
        output = run_helper(
            args, "next-stage.sh", *command, allow_refusal=True,
            extra_environment=extra_environment,
        )
    finally:
        if evidence_path is not None:
            evidence_path.unlink(missing_ok=True)
    return next((line.strip() for line in output.splitlines() if line.strip()), "")


def transition(args: argparse.Namespace, state: str) -> None:
    run_helper(
        args, "ticket-state.sh", "--ticket", args.ticket,
        "--workdir", str(args.workdir), "--action", "transition", "--state", state,
    )


def migrate_passport(args: argparse.Namespace) -> None:
    passport = args.state_dir / "passports" / f"{args.ticket}.json"
    if not passport.exists() and not passport.is_symlink():
        return
    result = subprocess.run(
        [
            __import__("sys").executable,
            "-B",
            str(args.kit_dir / "scripts" / "ticket-passport.py"),
            "migrate",
            "--factory-root", str(args.factory_root),
            "--workdir", str(args.workdir),
            "--state-dir", str(args.state_dir),
            "--ticket", args.ticket,
            "--contract-version", args.contract_version,
            "--factory-sha", args.factory_sha,
            "--project", args.project,
            "--publication-state", "preserve",
        ],
        text=True,
        capture_output=True,
        check=False,
        timeout=120,
    )
    if result.returncode:
        raise StateError("authenticated passport migration failed")


def next_transition(args: argparse.Namespace) -> dict[str, Any]:
    current = current_state(args.workdir, args.ticket)
    if current.casefold() in {"backlog", "blocked-escalated"}:
        run_helper(
            args, "ticket-state.sh", "--ticket", args.ticket,
            "--workdir", str(args.workdir), "--action", "materialize",
        )
    declared = declared_dependencies(args)
    dependencies = unresolved_dependencies(args, declared)
    if not dependencies:
        ensure_dependency_conflict_repair(args)
    repair_stage, repair_override = contract_repair_stage(args)
    if dependencies:
        stage = f"AWAIT_DEPENDENCY {','.join(dependencies)}"
    elif declared:
        base = protected_base_sha(args)
        stage = (
            (
                repair_stage
                if repair_stage is not None
                else resolve(args)
            )
            if branch_contains(args, base)
            else (
                "REFUSE dependency refresh required; "
                f"dependencies={','.join(declared)}; protected-main={base}"
            )
        )
    else:
        stage = (
            repair_stage
            if repair_stage is not None
            else resolve(args)
        )
    role = stage_role(stage)
    if role:
        current = current_state(args.workdir, args.ticket)
        target = TARGET_STATE[role]
        if not repair_override:
            while current != target:
                if current == "Ready":
                    transition(args, "Planning")
                elif current == "Planning" and target in {"Building", "Review"}:
                    transition(args, "Building")
                elif current == "Building" and target == "Review":
                    transition(args, "Review")
                elif current == "Review" and target == "Building":
                    transition(args, "Building")
                else:
                    raise StateError(
                        f"state machine cannot enter {target} from {current}"
                    )
                current = current_state(args.workdir, args.ticket)
    if not stage.startswith("REFUSE "):
        migrate_passport(args)
    receipt = issue(args, stage)
    return {
        "action": stage.partition(" ")[0],
        "detail": stage.partition(" ")[2] or None,
        "receipt": receipt["receipt_sha256"],
        "role": role,
        "schema": SCHEMA,
        "stage": stage,
        "status": "ok",
        "ticket": args.ticket,
    }


def contract_block_resume_state(
    args: argparse.Namespace, role: str, state: str
) -> str:
    target = TARGET_STATE[role]
    if state == target:
        return target
    repair_stage, repair_override = contract_repair_stage(args)
    order = {"Planning": 1, "Building": 2, "Review": 3}
    if (
        repair_override
        and repair_stage == f"FIX {role}"
        and state in order
        and order[state] > order[target]
    ):
        return state
    raise StateError("contract blocker role state drifted")


def block_transition(args: argparse.Namespace) -> dict[str, Any]:
    role = contract_blocked_receipt(args)
    state = current_state(args.workdir, args.ticket)
    if state != "Blocked-Escalated":
        resume_state = contract_block_resume_state(args, role, state)
        if state == TARGET_STATE[role]:
            run_helper(
                args, "ticket-state.sh", "--ticket", args.ticket,
                "--workdir", str(args.workdir), "--action", "materialize",
            )
            if current_state(args.workdir, args.ticket) != resume_state:
                raise StateError(
                    "operator overlay changed the contract blocker state"
                )
        transition(args, "Blocked-Escalated")
    else:
        resume_state = ticket_field(args.workdir, args.ticket, "Resume-State")
        contract_block_resume_state(args, role, resume_state)
    if (
        current_state(args.workdir, args.ticket) != "Blocked-Escalated"
        or ticket_field(args.workdir, args.ticket, "Resume-State")
        != resume_state
    ):
        raise StateError("contract blocker transition is invalid")
    migrate_passport(args)
    return {
        "action": "block",
        "head": git(args.workdir, "rev-parse", "HEAD"),
        "role": role,
        "schema": SCHEMA,
        "status": "blocked",
        "ticket": args.ticket,
    }


def resume_transition(args: argparse.Namespace) -> dict[str, Any]:
    role = contract_blocked_receipt(args)
    passport, secret = authenticated_passport(args)
    repair_role = operator_resume_role(args, passport, role)
    target = contract_block_resume_state(
        args, role, ticket_field(args.workdir, args.ticket, "Resume-State")
    )
    state = current_state(args.workdir, args.ticket)
    if state == "Blocked-Escalated":
        run_helper(
            args, "ticket-state.sh", "--ticket", args.ticket,
            "--workdir", str(args.workdir), "--action", "materialize",
        )
        state = current_state(args.workdir, args.ticket)
    if state == "Blocked-Escalated":
        status = "waiting"
    elif state == target:
        repair_target = TARGET_STATE[repair_role]
        order = {"Planning": 1, "Building": 2, "Review": 3}
        if order[repair_target] >= order[state]:
            while state != repair_target:
                if state == "Planning" and repair_target == "Building":
                    transition(args, "Building")
                elif state == "Building" and repair_target == "Review":
                    transition(args, "Review")
                elif state == "Review" and repair_target == "Building":
                    transition(args, "Building")
                else:
                    raise StateError(
                        "contract repair cannot enter its owning state"
                    )
                state = current_state(args.workdir, args.ticket)
        migrate_passport(args)
        passport, secret = authenticated_passport(args)
        head = git(args.workdir, "rev-parse", "HEAD")
        write_atomic(
            repair_path(args),
            signed_repair({
                "blocked_receipt": args.receipt,
                "blocked_role": role,
                "branch": passport["branch"],
                "factory_sha": args.factory_sha,
                "head_sha": head,
                "head_tree": git(args.workdir, "rev-parse", "HEAD^{tree}"),
                "passport_sha256": passport["passport_sha256"],
                "repair_role": repair_role,
                "schema": REPAIR_SCHEMA,
                "ticket": args.ticket,
            }, secret),
        )
        status = "ready"
    else:
        raise StateError("contract blocker resumed to an invalid state")
    return {
        "action": "resume",
        "head": git(args.workdir, "rev-parse", "HEAD"),
        "repair_role": repair_role,
        "role": role,
        "schema": SCHEMA,
        "status": status,
        "ticket": args.ticket,
    }


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser()
    value.add_argument(
        "action", choices=("next", "verify", "consume", "block", "resume")
    )
    value.add_argument("--factory-root", required=True, type=Path)
    value.add_argument("--workdir", required=True, type=Path)
    value.add_argument("--kit-dir", required=True, type=Path)
    value.add_argument("--state-dir", required=True, type=Path)
    value.add_argument("--ticket", required=True)
    value.add_argument("--contract-version", required=True)
    value.add_argument("--factory-sha", required=True)
    value.add_argument("--project", required=True)
    value.add_argument("--lease", default="")
    value.add_argument("--receipt", default="")
    value.add_argument("--role", default="")
    value.add_argument("--require-used", action="store_true")
    return value


def main() -> None:
    args = parser().parse_args()
    try:
        if (
            not TICKET.fullmatch(args.ticket)
            or args.contract_version != "1.8.0"
            or not SHA.fullmatch(args.factory_sha)
            or (args.receipt and not DIGEST.fullmatch(args.receipt))
            or (args.role and not ROLE.fullmatch(args.role))
            or (args.action in {"block", "resume"} and not args.receipt)
            or (args.action == "block" and not args.lease)
        ):
            raise StateError("invalid state-machine arguments")
        args.factory_root = args.factory_root.resolve(strict=True)
        args.workdir = args.workdir.resolve(strict=True)
        args.kit_dir = args.kit_dir.resolve(strict=True)
        args.state_dir = safe_state_dir(args.state_dir)
        if args.action == "next":
            result = next_transition(args)
        elif args.action == "block":
            result = block_transition(args)
        elif args.action == "resume":
            result = resume_transition(args)
        else:
            if not args.receipt:
                raise StateError("receipt is required")
            receipt = verify(args, consume=args.action == "consume")
            result = {
                "receipt": receipt["receipt_sha256"],
                "schema": SCHEMA,
                "stage": receipt["stage"],
                "status": "ok",
                "ticket": args.ticket,
            }
        print(json.dumps(result, sort_keys=True))
    except (
        FileNotFoundError,
        json.JSONDecodeError,
        OSError,
        StateError,
        subprocess.SubprocessError,
        UnicodeError,
    ) as error:
        print(json.dumps({
            "error": str(error), "schema": SCHEMA, "status": "error",
            "ticket": args.ticket,
        }, sort_keys=True))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
