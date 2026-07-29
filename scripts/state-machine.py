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
import stat
import subprocess
import tempfile
from typing import Any


SCHEMA = "nysa.software-factory.state-machine/v1"
RECEIPT_SCHEMA = "nysa.software-factory.transition-receipt/v1"
REPAIR_SCHEMA = "nysa.software-factory.contract-repair/v1"
PASSPORT_SCHEMA = "nysa.software-factory.ticket-passport/v1"
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


def load_repair(args: argparse.Namespace, secret: bytes) -> dict[str, Any] | None:
    path = repair_path(args)
    if not path.exists() and not path.is_symlink():
        return None
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
                selected.append((path.name, raw))
                output = path.with_suffix(".out")
                if output.is_file() and not output.is_symlink():
                    selected.append((output.name, output.read_bytes()))
    digest = hashlib.sha256()
    for name, raw in selected:
        digest.update(name.encode())
        digest.update(b"\0")
        digest.update(hashlib.sha256(raw).digest())
    return digest.hexdigest()


def stage_role(stage: str) -> str | None:
    action, separator, detail = stage.partition(" ")
    if action in {"RUN", "FIX"} and separator and ROLE.fullmatch(detail):
        return detail
    if action in {
        "AWAIT-OPERATOR", "AWAIT-MERGE", "AWAIT_BUDGET", "COMPLETE", "REFUSE",
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


def contract_blocked_receipt(args: argparse.Namespace) -> str:
    value = safe_receipt(args.state_dir / f"{args.ticket}.json")
    origin = os.environ.get("FACTORY_CERTIFIED_PRODUCT_ORIGIN", "")
    role = value.get("role", "")
    old_head = value.get("head_sha", "")
    if (
        not origin
        or value.get("receipt_sha256") != args.receipt
        or not value.get("consumed")
        or value.get("ticket") != args.ticket
        or value.get("contract_version") != args.contract_version
        or value.get("factory_sha") != args.factory_sha
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
            and value.get("lease_sha256")
            != hashlib.sha256(args.lease.encode()).hexdigest()
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
    matches = []
    for path in sorted((args.factory_root / "factory/runs").glob("*.meta")):
        fields, _ = run_manifest(path)
        if fields.get("transition_receipt_sha256") == args.receipt:
            matches.append(fields)
    if len(matches) != 1:
        raise StateError("contract blocker terminal evidence is ambiguous")
    terminal = matches[0]
    accounted = terminal.get("accounting_state") == "completed" or (
        terminal.get("accounting_state") == "abandoned_conservative"
        and terminal.get("cost_basis") == "conservative_reservation"
        and terminal.get("effective_cost") == terminal.get("reserved_usd")
    )
    if not accounted or any((
        terminal.get("ticket") != args.ticket,
        terminal.get("role") != role,
        terminal.get("contract_version") != args.contract_version,
        terminal.get("kit_sha") != args.factory_sha,
        terminal.get("phase") != "completed",
        terminal.get("go_issued") != "1",
        terminal.get("task_submitted") != "1",
        terminal.get("exit_status") != "12",
        terminal.get("role_exit") != "role_exit_contract_blocked",
        terminal.get("role_branch_before") != value.get("branch"),
        terminal.get("role_head_before") != value.get("head_sha"),
    )):
        raise StateError("contract blocker terminal evidence is invalid")
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
    if not directives:
        if git(args.workdir, "rev-parse", "HEAD") != prior_head:
            raise StateError("contract repair changed without an operator directive")
        return blocked_role
    repair_role = directives[0]
    directive = f"OPERATOR RESUME: {repair_role}"
    commits = git(
        args.workdir, "log", "--format=%H", f"-S{directive}", "--", relative
    ).splitlines()
    if len(commits) != 1 or not SHA.fullmatch(commits[0]):
        raise StateError("contract repair operator directive is invalid")
    commit = commits[0]
    parent = git(args.workdir, "rev-parse", f"{commit}^")
    before = git(args.workdir, "show", f"{parent}:{relative}") + "\n"
    after = git(args.workdir, "show", f"{commit}:{relative}") + "\n"
    expected = before.rstrip("\n") + f"\n\n{directive}\n"
    changed = git(args.workdir, "diff", "--name-only", f"{parent}..{commit}").splitlines()
    known_heads = {prior_head}
    for migration in passport.get("migration_history", []):
        if isinstance(migration, dict):
            known_heads.update(
                migration.get(name, "")
                for name in ("from_head_sha", "to_head_sha")
                if SHA.fullmatch(migration.get(name, ""))
            )
    current_head = git(args.workdir, "rev-parse", "HEAD")
    if (
        len(directives) != 1
        or after != expected
        or changed != [relative]
        or parent not in known_heads
        or subprocess.run(
            ["git", "-C", str(args.workdir), "merge-base", "--is-ancestor", commit, "HEAD"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            check=False, timeout=120,
        ).returncode != 0
        or current_head not in {commit, prior_head}
    ):
        raise StateError("contract repair operator directive is invalid")
    return repair_role


def contract_repair_stage(args: argparse.Namespace) -> tuple[str | None, bool]:
    text = (
        args.workdir / "factory" / "tickets" / f"{args.ticket}.md"
    ).read_text(encoding="utf-8")
    has_directive = bool(re.search(r"^OPERATOR RESUME:", text, re.M))
    path = args.state_dir / "contract-repairs" / f"{args.ticket}.json"
    if not has_directive and not path.exists() and not path.is_symlink():
        return None, False
    passport, secret = authenticated_passport(args)
    record = load_repair(args, secret)
    if record is None:
        if has_directive:
            raise StateError("operator resume lacks authenticated contract repair state")
        return None, False
    owner = record.get("repair_role", "")
    head = record.get("head_sha", "")
    if any((
        record.get("schema") != REPAIR_SCHEMA,
        record.get("ticket") != args.ticket,
        record.get("factory_sha") != args.factory_sha,
        record.get("branch") != passport.get("branch"),
        owner not in {"planner", "spec-linter", "test-author", "builder"},
        not SHA.fullmatch(head),
        subprocess.run(
            ["git", "-C", str(args.workdir), "merge-base", "--is-ancestor", head, "HEAD"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            check=False, timeout=120,
        ).returncode != 0,
    )):
        raise StateError("contract repair record is invalid")
    successes = []
    for path in sorted((args.factory_root / "factory/runs").glob("*.meta")):
        fields, _ = run_manifest(path)
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
            successes.append(fields.get("run_id", ""))
    if len(successes) > 1:
        raise StateError("contract repair has duplicate successful evidence")
    if not successes:
        return f"FIX {owner}", True
    stage = resolve(args)
    role = stage_role(stage)
    order = {"Planning": 1, "Building": 2, "Review": 3}
    if (
        role is not None
        and order[TARGET_STATE[role]] < order[current_state(args.workdir, args.ticket)]
    ):
        return stage, True
    return None, False


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
) -> str:
    result = subprocess.run(
        ["/bin/bash", str(args.kit_dir / "scripts" / script), *arguments],
        cwd=args.workdir,
        env={
            **os.environ,
            "FACTORY_ROOT": str(args.factory_root),
            "FACTORY_TRANSITION_STATE_DIR": str(args.state_dir),
            "FACTORY_RELEASE_SHA": args.factory_sha,
        },
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


def resolve(args: argparse.Namespace) -> str:
    command = ["--ticket", args.ticket]
    if args.lease:
        command.extend(["--lease", args.lease])
    command.extend(["--workdir", str(args.workdir)])
    output = run_helper(args, "next-stage.sh", *command, allow_refusal=True)
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
    repair_stage, repair_override = contract_repair_stage(args)
    stage = repair_stage or resolve(args)
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
        if (not repair_override or stage.startswith("RUN ")) and resolve(args) != stage:
            raise StateError("transition changed the resolved stage")
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


def block_transition(args: argparse.Namespace) -> dict[str, Any]:
    role = contract_blocked_receipt(args)
    target = TARGET_STATE[role]
    state = current_state(args.workdir, args.ticket)
    if state != "Blocked-Escalated":
        if state != target:
            raise StateError("contract blocker role state drifted")
        run_helper(
            args, "ticket-state.sh", "--ticket", args.ticket,
            "--workdir", str(args.workdir), "--action", "materialize",
        )
        if current_state(args.workdir, args.ticket) != target:
            raise StateError("operator overlay changed the contract blocker state")
        transition(args, "Blocked-Escalated")
    if (
        current_state(args.workdir, args.ticket) != "Blocked-Escalated"
        or ticket_field(args.workdir, args.ticket, "Resume-State") != target
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
    target = TARGET_STATE[role]
    if ticket_field(args.workdir, args.ticket, "Resume-State") != target:
        raise StateError("contract blocker resume target drifted")
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
        while state != repair_target:
            if state == "Planning" and repair_target == "Building":
                transition(args, "Building")
            elif state == "Building" and repair_target == "Review":
                transition(args, "Review")
            elif state == "Review" and repair_target == "Building":
                transition(args, "Building")
            else:
                raise StateError("contract repair cannot enter its owning state")
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
