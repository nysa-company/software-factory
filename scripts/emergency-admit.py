#!/usr/bin/env python3
"""One-use, hash-approved fallback for an exact pre-provider role receipt."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import hmac
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import time
from typing import Any


PLAN_SCHEMA = "nysa.software-factory.emergency-role-admission-plan/v1"
AUTH_SCHEMA = "nysa.software-factory.emergency-role-admission/v1"
RESERVATION_SCHEMA = "nysa.software-factory.emergency-role-reservation/v1"
CONSUMPTION_SCHEMA = "nysa.software-factory.emergency-role-consumption/v1"
ARCHIVE_SCHEMA = "nysa.software-factory.emergency-role-archive/v1"
REQUEST_SCHEMA = "nysa.software-factory.emergency-role-admission-request/v1"
EVENT_SCHEMA = "nysa.software-factory.controller-event/v1"
QUALIFICATION_SCHEMA = "nysa.software-factory.qualification/v2"
ROLE = re.compile(r"^(planner|spec-linter|test-author|builder|reviewer|narrator)$")
DIGEST = re.compile(r"^[0-9a-f]{64}$")
SHA = re.compile(r"^[0-9a-f]{40}$")
ISSUE = re.compile(
    r"^https://github[.]com/nysa-company/software-factory/issues/([1-9][0-9]*)$"
)
REQUEST_KEYS = {
    "schema", "issue", "operator_id", "reason", "issued_at", "expires_at",
}
TERMINAL_ACCOUNTING = {
    "completed", "launch_void", "abandoned_conservative", "cancelled",
    "cancelled_conservative",
}


class Refusal(ValueError):
    pass


def canonical(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode()


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for name, item in pairs:
        if name in value:
            raise ValueError("duplicate JSON field")
        value[name] = item
    return value


def read_regular(
    path: Path, label: str, *, mode: int = 0o600, maximum: int = 1_000_000,
) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != mode
            or info.st_size > maximum
        ):
            raise Refusal(f"{label} is unsafe")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            return stream.read()
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def read_json(path: Path, label: str, *, maximum: int = 1_000_000) -> tuple[bytes, dict[str, Any]]:
    try:
        raw = read_regular(path, label, maximum=maximum)
        value = json.loads(raw, object_pairs_hook=unique_object)
    except (OSError, UnicodeError, ValueError) as error:
        if isinstance(error, Refusal):
            raise
        raise Refusal(f"{label} is invalid") from error
    if not isinstance(value, dict):
        raise Refusal(f"{label} is invalid")
    return raw, value


def safe_directory(path: Path, label: str, *, create: bool = False) -> Path:
    if create:
        path.mkdir(mode=0o700, exist_ok=True)
    info = path.lstat()
    if (
        not path.is_absolute()
        or path.resolve(strict=True) != path
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise Refusal(f"{label} is unsafe")
    return path


def create_record(path: Path, value: dict[str, Any]) -> bool:
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
    except FileExistsError:
        _, existing = read_json(path, path.name)
        if existing != value:
            raise Refusal(f"{path.name} conflicts with existing evidence")
        return False
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(canonical(value))
        stream.flush()
        os.fsync(stream.fileno())
    return True


def signed(value: dict[str, Any], secret: bytes) -> dict[str, Any]:
    result = dict(value)
    result["authentication_sha256"] = hmac.new(
        secret, canonical(value), hashlib.sha256
    ).hexdigest()
    result["record_sha256"] = digest(result)
    return result


def load_signed(path: Path, secret: bytes, schema: str) -> dict[str, Any]:
    _, value = read_json(path, path.name)
    record_digest = value.pop("record_sha256", "")
    if not hmac.compare_digest(record_digest, digest(value)):
        raise Refusal("emergency admission record digest is invalid")
    authentication = value.pop("authentication_sha256", "")
    if (
        value.get("schema") != schema
        or not hmac.compare_digest(
            authentication, hmac.new(secret, canonical(value), hashlib.sha256).hexdigest()
        )
    ):
        raise Refusal("emergency admission record authentication is invalid")
    value["authentication_sha256"] = authentication
    value["record_sha256"] = record_digest
    return value


def parse_time(value: Any, label: str) -> datetime:
    if not isinstance(value, str):
        raise Refusal(f"{label} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise Refusal(f"{label} is invalid") from error
    if parsed.tzinfo is None or parsed.microsecond:
        raise Refusal(f"{label} is invalid")
    return parsed.astimezone(timezone.utc)


def request(path: Path, *, current: bool) -> dict[str, Any]:
    if not path.is_absolute() or path.is_symlink() or path.resolve(strict=True) != path:
        raise Refusal("emergency admission request is unsafe")
    _, value = read_json(path, "emergency admission request", maximum=64_000)
    issued = parse_time(value.get("issued_at"), "issued_at")
    expires = parse_time(value.get("expires_at"), "expires_at")
    now = datetime.now(timezone.utc)
    if (
        set(value) != REQUEST_KEYS
        or value.get("schema") != REQUEST_SCHEMA
        or expires <= issued
        or (expires - issued).total_seconds() > 24 * 60 * 60
        or issued > now
        or now >= expires
        or (current and (now - issued).total_seconds() > 15 * 60)
        or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", value.get("operator_id", "")
        )
        or value.get("operator_id") == "auto"
        or not isinstance(value.get("reason"), str)
        or not 20 <= len(value["reason"]) <= 500
        or not ISSUE.fullmatch(value.get("issue", ""))
    ):
        raise Refusal("emergency admission authority is invalid or expired")
    return value


def validate_issue(url: str) -> None:
    match = ISSUE.fullmatch(url)
    if match is None:
        raise Refusal("emergency admission issue is invalid")
    result = subprocess.run(
        ["gh", "api", f"repos/nysa-company/software-factory/issues/{match.group(1)}"],
        text=True, capture_output=True, check=False, timeout=120,
    )
    if result.returncode:
        raise Refusal("emergency admission issue could not be verified")
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise Refusal("emergency admission issue evidence is invalid") from error
    if (
        value.get("number") != int(match.group(1))
        or value.get("html_url") != url
        or value.get("state") != "open"
        or "pull_request" in value
    ):
        raise Refusal("emergency admission requires one exact open Factory issue")


def state_machine_module():
    path = Path(__file__).with_name("state-machine.py")
    spec = importlib.util.spec_from_file_location("emergency_admit_state_machine", path)
    if spec is None or spec.loader is None:
        raise Refusal("state-machine validator is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def git(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *arguments],
        text=True, capture_output=True, check=False, timeout=120,
    )
    if result.returncode:
        raise Refusal(result.stderr.strip() or "Git validation failed")
    return result.stdout.strip()


def claim_identity(args: argparse.Namespace, *, running: bool) -> tuple[dict[str, Any], str]:
    requested_lease = args.lease
    raw, claim = read_json(
        args.state_dir / "claims" / f"{args.ticket}.json", "controller claim",
        maximum=64_000,
    )
    claim_worktree = claim.get("worktree")
    try:
        physical_worktree = (
            Path(claim_worktree).resolve(strict=True)
            if isinstance(claim_worktree, str) and Path(claim_worktree).is_absolute()
            else None
        )
    except OSError:
        physical_worktree = None
    stable = {
        "branch": claim.get("branch"),
        "lease_sha256": hashlib.sha256(claim.get("lease", "").encode()).hexdigest(),
        "priority": claim.get("priority"),
        "ticket": claim.get("ticket"),
        "worktree": claim_worktree,
    }
    expected_status = "running" if running else "claimed"
    if (
        claim.get("schema") != "nysa.software-factory.controller-claim/v1"
        or set(claim) != {
            "branch", "lease", "priority", "publication_lease", "receipt",
            "role", "schema", "status", "ticket", "worktree",
        }
        or stable["ticket"] != args.ticket
        or stable["branch"] != f"ticket/{args.ticket}"
        or physical_worktree != args.workdir
        or claim.get("status") != expected_status
        or claim.get("priority") not in {"urgent", "high", "normal", "low", "none"}
        or claim.get("publication_lease") != ""
        or claim.get("parked") is not None
        or claim.get("lease_released") is not None
        or not DIGEST.fullmatch(claim.get("lease", ""))
        or (requested_lease and claim.get("lease") != requested_lease)
        or (running and (
            claim.get("receipt") != args.receipt or claim.get("role") != args.role
        ))
        or (not running and (claim.get("receipt") != "" or claim.get("role") != ""))
    ):
        raise Refusal("controller claim is not at the exact pre-provider boundary")
    args.lease = claim["lease"]
    return stable, hashlib.sha256(raw).hexdigest()


def no_attempt_evidence(args: argparse.Namespace) -> None:
    active = args.factory_root / "factory" / ".active-runs"
    if active.exists() or active.is_symlink():
        info = active.lstat()
        if active.is_symlink() or not stat.S_ISDIR(info.st_mode):
            raise Refusal("active run state is unsafe")
        if any(active.glob(f"{args.ticket}.*")):
            raise Refusal("emergency admission refuses an active provider attempt")
    runs = args.factory_root / "factory" / "runs"
    if not runs.exists():
        return
    for path in runs.glob("*.meta"):
        try:
            fields = {}
            for line in path.read_text(encoding="utf-8").splitlines():
                name, separator, value = line.partition("=")
                if not separator or not name or name in fields:
                    raise ValueError
                fields[name] = value
        except (OSError, UnicodeError, ValueError) as error:
            raise Refusal("run evidence is unsafe") from error
        if fields.get("transition_receipt_sha256") == args.receipt:
            raise Refusal("emergency admission refuses existing run evidence")


def resume_state(args: argparse.Namespace) -> str | None:
    text = (
        args.workdir / "factory" / "tickets" / f"{args.ticket}.md"
    ).read_text(encoding="utf-8")
    values = re.findall(r"^Resume-State:\s*(.*?)\s*$", text, re.I | re.M)
    if len(values) > 1:
        raise Refusal("ticket Resume-State is ambiguous")
    return values[0] if values else None


def protected_identity(args: argparse.Namespace, head: str) -> dict[str, str]:
    origin = os.environ.get("FACTORY_CERTIFIED_PRODUCT_ORIGIN", "")
    if not origin or any(character in origin for character in "\n\r\t"):
        raise Refusal("certified product origin is unavailable")
    main = git(args.workdir, "rev-parse", "origin/main")
    remote_main = git(args.workdir, "ls-remote", "--heads", "--", origin, "refs/heads/main")
    remote_branch = git(
        args.workdir, "ls-remote", "--heads", "--", origin,
        f"refs/heads/ticket/{args.ticket}",
    )
    if remote_main != f"{main}\trefs/heads/main":
        raise Refusal("origin/main is not the authoritative protected product tip")
    if remote_branch != f"{head}\trefs/heads/ticket/{args.ticket}":
        raise Refusal("ticket head is not the exact certified remote branch")
    base = git(args.workdir, "merge-base", main, head)
    if not SHA.fullmatch(base):
        raise Refusal("ticket head lacks protected product ancestry")
    return {"base": base, "main": main, "main_tree": git(args.workdir, "rev-parse", f"{main}^{{tree}}")}


def current_identity(
    args: argparse.Namespace, *, running: bool,
) -> tuple[dict[str, Any], dict[str, Any], bytes, str]:
    if (args.factory_root / "factory" / "MAINTENANCE").exists():
        raise Refusal("emergency admission refuses maintenance")
    no_attempt_evidence(args)
    state_machine = state_machine_module()
    try:
        receipt = state_machine.safe_receipt(args.state_dir / f"{args.ticket}.json")
        passport, secret = state_machine.authenticated_passport(args)
        stable_claim, claim_digest = claim_identity(args, running=running)
        state_machine.require_current_lease(args)
        expected = state_machine.core(
            args, receipt["stage"], receipt.get("role"), receipt.get("loop")
        )
    except (OSError, ValueError) as error:
        raise Refusal(str(error)) from error
    actual = {
        key: item for key, item in receipt.items()
        if key not in {
            "consumed", "consumed_at_epoch", "nonce", "parent_digest",
            "receipt_sha256",
        }
    }
    current_state = state_machine.current_state(args.workdir, args.ticket)
    if (
        receipt.get("receipt_sha256") != args.receipt
        or receipt.get("consumed") is not False
        or receipt.get("role") != args.role
        or receipt.get("stage") not in {f"RUN {args.role}", f"FIX {args.role}"}
        or expected != actual
        or current_state != state_machine.TARGET_STATE[args.role]
        or (isinstance(receipt.get("loop"), dict) and receipt["loop"].get("capped") is True)
        or passport.get("ticket") != args.ticket
        or passport.get("branch") != f"ticket/{args.ticket}"
        or passport.get("contract_version") != args.contract_version
        or passport.get("factory_sha") != args.factory_sha
        or passport.get("head_sha") != receipt.get("head_sha")
        or passport.get("current_stage") != receipt.get("stage")
        or passport.get("current_state") != current_state
        or passport.get("project") != args.project
        or passport.get("publication_state") not in {"none", "validating"}
    ):
        raise Refusal("transition is not an exact unconsumed role admission")
    head = receipt["head_sha"]
    identity = {
        "branch": receipt["branch"],
        "claim": stable_claim,
        "claim_sha256": claim_digest,
        "contract_version": args.contract_version,
        "current_state": current_state,
        "factory_sha": args.factory_sha,
        "factory_tree": os.environ.get("FACTORY_RELEASE_TREE", ""),
        "head_sha": head,
        "head_tree": receipt["head_tree"],
        "kit_trust_scope": os.environ.get("FACTORY_KIT_TRUST_SCOPE", ""),
        "passport_sha256": passport["passport_sha256"],
        "passport_file_sha256": receipt["passport_sha256"],
        "product_origin_sha256": receipt["product_origin_sha256"],
        "project": args.project,
        "protected_product": protected_identity(args, head),
        "receipt_file_sha256": hashlib.sha256(
            read_regular(args.state_dir / f"{args.ticket}.json", "transition receipt")
        ).hexdigest(),
        "resume_state": resume_state(args),
        "role": args.role,
        "route_plan_sha256": receipt["route_plan_sha256"],
        "stage": receipt["stage"],
        "ticket": args.ticket,
        "ticket_blob": receipt["ticket_blob"],
        "transition_receipt_sha256": args.receipt,
    }
    if (
        not SHA.fullmatch(identity["factory_tree"])
        or identity["kit_trust_scope"] not in {
            "production-certified", "qualification-candidate",
        }
    ):
        raise Refusal("Factory release identity is invalid")
    return identity, receipt, secret, claim_digest


def build_plan(args: argparse.Namespace, authority: dict[str, Any]) -> tuple[dict[str, Any], bytes]:
    validate_issue(authority["issue"])
    identity, _, secret, _ = current_identity(args, running=False)
    ticket_root = args.state_dir / "emergency-admissions" / args.ticket
    if ticket_root.exists() or ticket_root.is_symlink():
        safe_directory(ticket_root, "ticket emergency admission root")
        if (
            any(ticket_root.glob("*.reservation.json"))
            or any(ticket_root.glob("*.consumption.json"))
        ):
            raise Refusal("emergency admission refuses an active reservation")
    plan = {
        "schema": PLAN_SCHEMA,
        **{name: value for name, value in authority.items() if name != "schema"},
        **identity,
    }
    return plan, secret


def admission_directory(args: argparse.Namespace) -> Path:
    root = safe_directory(
        args.state_dir / "emergency-admissions", "emergency admission root",
        create=True,
    )
    return safe_directory(root / args.ticket, "ticket emergency admission root", create=True)


def authorization_event(
    args: argparse.Namespace, record: dict[str, Any]
) -> None:
    value = {
        "approval_sha256": record["approval_sha256"],
        "event": "emergency_admission_authorized",
        "factory_sha": args.factory_sha,
        "observed_at_epoch_ns": record["applied_at_epoch"] * 1_000_000_000,
        "operator_id": record["plan"]["operator_id"],
        "role": args.role,
        "schema": EVENT_SCHEMA,
        "ticket": args.ticket,
        "transition_receipt_sha256": args.receipt,
    }
    qualification_path = args.factory_root / "factory" / "QUALIFICATION.json"
    if qualification_path.exists():
        try:
            qualification = json.loads(qualification_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise Refusal("qualification manifest is invalid") from error
        if (
            not isinstance(qualification, dict)
            or qualification.get("schema") != QUALIFICATION_SCHEMA
            or qualification.get("factory_sha") != args.factory_sha
            or isinstance(qualification.get("generation"), bool)
            or not isinstance(qualification.get("generation"), int)
        ):
            raise Refusal("qualification manifest is invalid")
        value.update({
            "qualification_generation": qualification["generation"],
            "qualification_manifest_sha256": hashlib.sha256(
                canonical(qualification).rstrip(b"\n")
            ).hexdigest(),
        })
    value["event_sha256"] = hashlib.sha256(
        canonical(value).rstrip(b"\n")
    ).hexdigest()
    events = safe_directory(args.state_dir / "events", "controller event root", create=True)
    create_record(
        events / f"emergency-admission-authorized-{record['approval_sha256']}.json",
        value,
    )


def apply(args: argparse.Namespace, authority: dict[str, Any]) -> dict[str, Any]:
    plan, secret = build_plan(args, authority)
    approval = digest(plan)
    if approval != args.approve_hash:
        raise Refusal("approval hash does not match the exact emergency admission plan")
    root = admission_directory(args)
    record: dict[str, Any] | None = None
    for path in root.glob("*.authorization.json"):
        existing = load_signed(path, secret, AUTH_SCHEMA)
        existing_plan = existing.get("plan")
        if not isinstance(existing_plan, dict):
            raise Refusal("emergency admission authorization plan is invalid")
        expires = parse_time(existing_plan.get("expires_at"), "expires_at")
        if (
            existing_plan.get("transition_receipt_sha256") == args.receipt
            and existing.get("approval_sha256") != approval
            and datetime.now(timezone.utc) < expires
        ):
            raise Refusal("another authorization already binds this transition receipt")
        if existing.get("approval_sha256") == approval:
            if existing_plan != plan:
                raise Refusal("emergency admission authorization identity is invalid")
            record = existing
    if record is None:
        record = signed({
            "schema": AUTH_SCHEMA,
            "approval_sha256": approval,
            "applied_at_epoch": int(time.time()),
            "plan": plan,
        }, secret)
        created = create_record(root / f"{approval}.authorization.json", record)
    else:
        created = False
    authorization_event(args, record)
    return {
        "action": "apply", "approval_sha256": approval,
        "created": created, "schema": AUTH_SCHEMA, "status": "applied",
        "ticket": args.ticket,
    }


def matching_authorization(
    args: argparse.Namespace, secret: bytes,
) -> tuple[Path, dict[str, Any]]:
    root = admission_directory(args)
    matches: list[tuple[Path, dict[str, Any]]] = []
    for path in root.glob("*.authorization.json"):
        value = load_signed(path, secret, AUTH_SCHEMA)
        plan = value.get("plan")
        if not isinstance(plan, dict):
            raise Refusal("emergency admission authorization plan is invalid")
        if (
            plan.get("transition_receipt_sha256") == args.receipt
            and plan.get("role") == args.role
            and datetime.now(timezone.utc) < parse_time(plan.get("expires_at"), "expires_at")
        ):
            matches.append((path, value))
    if len(matches) != 1:
        raise Refusal("emergency admission authorization is missing or ambiguous")
    path, value = matches[0]
    plan = value["plan"]
    if (
        plan.get("schema") != PLAN_SCHEMA
        or value.get("approval_sha256") != digest(plan)
        or path.name != f"{value.get('approval_sha256')}.authorization.json"
    ):
        raise Refusal("emergency admission authorization identity is invalid")
    return path, value


def consume(args: argparse.Namespace) -> dict[str, Any]:
    state_machine = state_machine_module()
    lock_descriptor = os.open(
        args.state_dir / ".lock",
        os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600,
    )
    try:
        with os.fdopen(lock_descriptor, "r+") as lock:
            lock_descriptor = -1
            fcntl.flock(lock, fcntl.LOCK_EX)
            identity, receipt, secret, _ = current_identity(args, running=True)
            auth_path, authorization = matching_authorization(args, secret)
            plan = authorization["plan"]
            validate_issue(plan["issue"])
            if any(plan.get(name) != value for name, value in identity.items() if name != "claim_sha256"):
                raise Refusal("emergency admission inputs changed after approval")
            approval = authorization["approval_sha256"]
            root = auth_path.parent
            reservation = signed({
                "schema": RESERVATION_SCHEMA,
                "approval_sha256": approval,
                "reserved_at_epoch": int(time.time()),
                "transition_receipt_sha256": args.receipt,
            }, secret)
            create_record(root / f"{approval}.reservation.json", reservation)
            if (root / f"{approval}.consumption.json").exists():
                raise Refusal("emergency admission was already consumed")
            receipt["consumed"] = True
            receipt["consumed_at_epoch"] = reservation["reserved_at_epoch"]
            state_machine.write_atomic(args.state_dir / f"{args.ticket}.json", receipt)
            consumption = signed({
                "schema": CONSUMPTION_SCHEMA,
                "approval_sha256": approval,
                "consumed_at_epoch": reservation["reserved_at_epoch"],
                "reservation_sha256": reservation["record_sha256"],
                "transition_receipt_sha256": args.receipt,
            }, secret)
            create_record(root / f"{approval}.consumption.json", consumption)
            return {
                "action": "consume", "approval_sha256": approval,
                "schema": CONSUMPTION_SCHEMA, "status": "consumed",
                "ticket": args.ticket,
            }
    finally:
        if lock_descriptor >= 0:
            os.close(lock_descriptor)


def manifest_fields(path: Path) -> tuple[dict[str, str], str]:
    info = path.lstat()
    if path.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise Refusal("run manifest is unsafe")
    raw = path.read_bytes()
    values: dict[str, str] = {}
    for line in raw.decode("utf-8").splitlines():
        name, separator, value = line.partition("=")
        if not separator or not name or name in values:
            raise Refusal("run manifest is invalid")
        values[name] = value
    return values, hashlib.sha256(raw).hexdigest()


def archive(args: argparse.Namespace) -> dict[str, Any]:
    state_machine = state_machine_module()
    try:
        passport, secret = state_machine.authenticated_passport(args)
    except (OSError, ValueError) as error:
        raise Refusal(str(error)) from error
    root = admission_directory(args)
    consumptions = []
    for path in root.glob("*.consumption.json"):
        value = load_signed(path, secret, CONSUMPTION_SCHEMA)
        if value.get("transition_receipt_sha256") == args.receipt:
            consumptions.append((path, value))
    if not consumptions:
        return {"action": "archive", "status": "absent", "ticket": args.ticket}
    if len(consumptions) != 1:
        raise Refusal("emergency admission consumption is ambiguous")
    _, consumption = consumptions[0]
    approval = consumption["approval_sha256"]
    archive_path = root / f"{approval}.archive.json"
    if archive_path.exists():
        value = load_signed(archive_path, secret, ARCHIVE_SCHEMA)
        return {
            "action": "archive", "approval_sha256": approval,
            "record_sha256": value["record_sha256"], "status": "archived",
            "ticket": args.ticket,
        }
    auth = load_signed(root / f"{approval}.authorization.json", secret, AUTH_SCHEMA)
    plan = auth.get("plan")
    if not isinstance(plan, dict):
        raise Refusal("emergency admission authorization plan is invalid")
    reservation = load_signed(
        root / f"{approval}.reservation.json", secret, RESERVATION_SCHEMA
    )
    if (
        auth.get("approval_sha256") != digest(plan)
        or plan.get("schema") != PLAN_SCHEMA
        or consumption.get("reservation_sha256") != reservation.get("record_sha256")
    ):
        raise Refusal("emergency admission archive authority is invalid")
    matches = []
    for path in (args.factory_root / "factory" / "runs").glob("*.meta"):
        fields, raw_digest = manifest_fields(path)
        if fields.get("transition_receipt_sha256") == args.receipt:
            matches.append((fields, raw_digest))
    if len(matches) != 1:
        raise Refusal("emergency admission terminal evidence is missing or ambiguous")
    terminal, manifest_digest = matches[0]
    run_id = terminal.get("run_id", "")
    expected = (
        run_id, args.role, args.receipt,
    )
    charge_records = [
        item for item in passport.get("charge_records", [])
        if isinstance(item, dict) and (
            item.get("run_id"), item.get("role"),
            item.get("transition_receipt_sha256"),
        ) == expected
    ]
    completed = [
        (item.get("run_id"), item.get("role"), item.get("transition_receipt_sha256"))
        for item in passport.get("completed_role_evidence", []) if isinstance(item, dict)
    ]
    successful = terminal.get("exit_status") == "0" and terminal.get("role_exit") == "ok"
    if (
        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", run_id)
        or terminal.get("ticket") != args.ticket
        or terminal.get("role") != args.role
        or terminal.get("accounting_state") not in TERMINAL_ACCOUNTING
        or terminal.get("kit_sha") != plan.get("factory_sha")
        or terminal.get("role_head_before") != plan.get("head_sha")
        or len(charge_records) != 1
        or charge_records[0].get("contract_version") != plan.get("contract_version")
        or charge_records[0].get("factory_sha") != plan.get("factory_sha")
        or charge_records[0].get("head_before") != plan.get("head_sha")
        or charge_records[0].get("manifest_sha256") != manifest_digest
        or (successful and completed.count(expected) != 1)
    ):
        raise Refusal("emergency admission result evidence is invalid")
    record = signed({
        "schema": ARCHIVE_SCHEMA,
        "approval_sha256": approval,
        "archived_at_epoch": int(time.time()),
        "consumption_sha256": consumption["record_sha256"],
        "manifest_sha256": manifest_digest,
        "passport_sha256": passport["passport_sha256"],
        "role": args.role,
        "run_id": run_id,
        "successful": successful,
        "ticket": args.ticket,
        "transition_receipt_sha256": args.receipt,
    }, secret)
    create_record(archive_path, record)
    return {
        "action": "archive", "approval_sha256": approval,
        "record_sha256": record["record_sha256"], "status": "archived",
        "ticket": args.ticket,
    }


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser()
    value.add_argument("action", choices=("plan", "apply", "consume", "archive"))
    value.add_argument("--factory-root", required=True, type=Path)
    value.add_argument("--workdir", required=True, type=Path)
    value.add_argument("--kit-dir", required=True, type=Path)
    value.add_argument("--state-dir", required=True, type=Path)
    value.add_argument("--ticket", required=True)
    value.add_argument("--contract-version", required=True)
    value.add_argument("--factory-sha", required=True)
    value.add_argument("--project", required=True)
    value.add_argument("--receipt", required=True)
    value.add_argument("--role", required=True)
    value.add_argument("--lease", default="")
    value.add_argument("--request", type=Path)
    value.add_argument("--approve-hash", default="")
    return value


def main() -> None:
    args = parser().parse_args()
    try:
        if (
            not re.fullmatch(r"T-[0-9]+", args.ticket)
            or not ROLE.fullmatch(args.role)
            or not DIGEST.fullmatch(args.receipt)
            or args.contract_version not in ("1.8.0", "1.9.0")
            or not SHA.fullmatch(args.factory_sha)
            or (args.lease and not DIGEST.fullmatch(args.lease))
            or (args.action in {"plan", "apply"}) != (args.request is not None)
            or (args.action == "apply") != bool(args.approve_hash)
            or (args.approve_hash and not DIGEST.fullmatch(args.approve_hash))
        ):
            raise Refusal("invalid emergency admission arguments")
        args.factory_root = args.factory_root.resolve(strict=True)
        args.workdir = args.workdir.resolve(strict=True)
        args.kit_dir = args.kit_dir.resolve(strict=True)
        args.state_dir = safe_directory(args.state_dir.resolve(strict=True), "controller state")
        authority = request(args.request, current=True) if args.request else None
        if args.action == "plan":
            plan, _ = build_plan(args, authority)
            result = {
                "action": "plan", "approval_sha256": digest(plan),
                "plan": plan, "schema": PLAN_SCHEMA, "status": "planned",
                "ticket": args.ticket,
            }
        elif args.action == "apply":
            result = apply(args, authority)
        elif args.action == "consume":
            result = consume(args)
        else:
            result = archive(args)
        print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    except (OSError, Refusal, subprocess.SubprocessError, ValueError) as error:
        print(f"emergency-admit: {error}", file=os.sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
