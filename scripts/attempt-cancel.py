#!/usr/bin/env python3
"""Preview and CAS-apply cancellation of exactly one factory attempt."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
from decimal import Decimal, InvalidOperation, ROUND_CEILING
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "process_identity", ROOT / "scripts/lib/process-identity.py"
)
IDENTITY = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = IDENTITY
SPEC.loader.exec_module(IDENTITY)

REASONS = frozenset(("budget_exhausted", "operator_requested"))
PLAN_SCHEMA = "nysa.software-factory.attempt-cancel-plan/v1"
PROVIDER_ONLY_PLAN_SCHEMA = (
    "nysa.software-factory.provider-only-attempt-cancel-plan/v1"
)
REQUEST_SCHEMA = "nysa.software-factory.attempt-cancel-request/v1"
RECEIPT_SCHEMA = "nysa.software-factory.attempt-cancellation/v1"
PROVIDER_ONLY_RECEIPT_SCHEMA = (
    "nysa.software-factory.provider-only-attempt-cancellation/v1"
)
PROVIDER_ATTEMPT_FIELDS = frozenset((
    "account_route", "admitted_at", "attempt_id", "budget_day",
    "cancellation_reason", "cancellation_requested_at", "charge_micro_usd",
    "go_at", "machine_daily_cap_micro_usd", "policy_sha256", "prepared_at",
    "product_daily_cap_micro_usd", "product_id", "provider_family",
    "reserve_micro_usd", "state", "submitted_at", "terminal_at",
    "terminal_result", "ticket_cap_micro_usd", "ticket_id", "updated_at",
    "version",
))
TERMINAL_STATES = frozenset((
    "completed", "abandoned_conservative", "launch_void",
    "cancelled_conservative",
))


class CancelError(ValueError):
    pass


def canonical(value) -> bytes:
    return (json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n").encode()


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def timestamp() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def micro_usd(value: str) -> int:
    try:
        amount = (Decimal(value) * 1_000_000).to_integral_value(
            rounding=ROUND_CEILING,
        )
        if amount < 0 or amount > 10**15:
            raise ValueError
        return int(amount)
    except (InvalidOperation, OverflowError, TypeError, ValueError) as error:
        raise CancelError("provider accounting amount is malformed") from error


def secure_json(path: Path) -> tuple[dict, bytes]:
    raw = IDENTITY.record_bytes(path)
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise CancelError(f"{path.name} is not valid JSON") from error
    if not isinstance(value, dict) or raw != canonical(value):
        raise CancelError(f"{path.name} is not canonical JSON")
    return value, raw


def durable_create(path: Path, raw: bytes) -> None:
    directory = path.parent
    descriptor = os.open(
        directory,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    temporary = f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}"
    opened = -1
    try:
        opened = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=descriptor,
        )
        with os.fdopen(opened, "wb") as handle:
            opened = -1
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(
            temporary, path.name,
            src_dir_fd=descriptor, dst_dir_fd=descriptor,
            follow_symlinks=False,
        )
        os.unlink(temporary, dir_fd=descriptor)
        os.fsync(descriptor)
    finally:
        if opened >= 0:
            os.close(opened)
        try:
            os.unlink(temporary, dir_fd=descriptor)
        except FileNotFoundError:
            pass
        os.close(descriptor)


def paths(factory_root: Path, run_id: str) -> dict[str, Path]:
    if not IDENTITY.RUN_ID.fullmatch(run_id):
        raise CancelError("invalid run identity")
    runs = factory_root / "factory/runs"
    ledger = bound_ledger(
        "FACTORY_LEDGER", factory_root / "factory/runtime-ledger.csv", "runtime",
    )
    return {
        "runs": runs,
        "manifest": runs / f"{run_id}.meta",
        "pid": runs / f"{run_id}.pid",
        "request": runs / f"{run_id}.cancel-request.json",
        "receipt": runs / f"{run_id}.cancel.json",
        "ledger": ledger,
        "durable_ledger": bound_ledger(
            "FACTORY_DURABLE_LEDGER", factory_root / "factory/ledger.csv", "durable",
        ),
        "active_runs": ledger.parent / ".active-runs",
    }


def bound_ledger(variable: str, fallback: Path, label: str) -> Path:
    configured = os.environ.get(variable)
    path = Path(configured) if configured else fallback
    if not path.is_absolute():
        raise CancelError(f"{label} ledger path is not absolute")
    try:
        info = path.lstat()
        resolved = path.resolve(strict=True)
    except FileNotFoundError as error:
        raise CancelError(f"{label} ledger is missing") from error
    if (
        resolved != path
        or path.is_symlink()
        or not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_nlink != 1
        or info.st_mode & 0o022
    ):
        raise CancelError(f"{label} ledger is unsafe")
    return path


def load_active_or_stale_identity(
    runs: Path, run_id: str, ticket: str,
) -> tuple[IDENTITY.AttemptIdentity, bool]:
    pid_values = IDENTITY.parse_fields(
        IDENTITY.record_bytes(runs / f"{run_id}.pid"), "PID record",
    )
    manifest = IDENTITY.parse_fields(
        IDENTITY.record_bytes(runs / f"{run_id}.meta"), "run manifest",
    )
    if (
        set(pid_values) != {"pid", "pgid", "run_id", "process_start"}
        or pid_values["run_id"] != run_id
        or manifest.get("run_id") != run_id
        or manifest.get("ticket") != ticket
        or not pid_values["pid"].isdigit()
        or not pid_values["pgid"].isdigit()
    ):
        raise CancelError("attempt identity records disagree")
    pid, pgid = int(pid_values["pid"]), int(pid_values["pgid"])
    started = pid_values["process_start"]
    if (
        pid <= 1 or pid != pgid or not started
        or manifest.get("pid") != str(pid)
        or manifest.get("pgid") != str(pgid)
        or manifest.get("process_start") != started
    ):
        raise CancelError("attempt identity records disagree")
    table = IDENTITY.process_table()
    leader = table.get(pid)
    if leader == IDENTITY.Process(pid, pgid, started):
        members = tuple(sorted(
            (process for process in table.values() if process.pgid == pgid),
            key=lambda process: process.pid,
        ))
        return IDENTITY.AttemptIdentity(run_id, ticket, leader, members), True
    if any(process.pgid == pgid for process in table.values()):
        raise CancelError("stale attempt process group was reused")
    return (
        IDENTITY.AttemptIdentity(
            run_id, ticket, IDENTITY.Process(pid, pgid, started), (),
        ),
        False,
    )


def calculate(
    factory_root: Path, ticket: str, run_id: str, reason: str, nonce: str | None,
) -> dict:
    if reason not in REASONS:
        raise CancelError("cancellation reason is not eligible")
    attempt = paths(factory_root, run_id)
    if not any(
        path.exists() or path.is_symlink()
        for path in (attempt["manifest"], attempt["pid"])
    ):
        return calculate_provider_only(
            factory_root, ticket, run_id, reason, nonce,
        )
    identity, _ = load_active_or_stale_identity(attempt["runs"], run_id, ticket)
    manifest_raw = IDENTITY.record_bytes(attempt["manifest"])
    manifest = IDENTITY.parse_fields(manifest_raw, "run manifest")
    pid_raw = IDENTITY.record_bytes(attempt["pid"])
    nonce = nonce or digest(
        manifest_raw + b"\0" + pid_raw + b"\0" + reason.encode()
    )[:32]
    if (
        manifest.get("accounting_schema") != "1"
        or manifest.get("accounting_state") != "reserved"
        or manifest.get("go_issued") not in ("0", "1")
    ):
        raise CancelError("attempt is not an active accounting reservation")
    plan = {
        "created_at": manifest.get("updated_at") or manifest["started_at"],
        "go_issued": manifest["go_issued"],
        "manifest_sha256": digest(manifest_raw),
        "nonce": nonce,
        "pgid": identity.leader.pgid,
        "pid": identity.leader.pid,
        "pid_record_sha256": digest(pid_raw),
        "process_start": identity.leader.started,
        "reason": reason,
        "run_id": run_id,
        "schema": PLAN_SCHEMA,
        "ticket": ticket,
    }
    plan["preview_hash"] = digest(canonical(plan))
    return plan


def validate_plan(value: dict, expected_hash: str) -> None:
    if value.get("schema") == PROVIDER_ONLY_PLAN_SCHEMA:
        validate_provider_only_plan(value, expected_hash)
        return
    if set(value) != {
        "created_at", "go_issued", "manifest_sha256", "nonce", "pgid", "pid",
        "pid_record_sha256", "preview_hash", "process_start", "reason", "run_id",
        "schema", "ticket",
    }:
        raise CancelError("cancel plan has unexpected fields")
    supplied = value["preview_hash"]
    unhashed = dict(value)
    del unhashed["preview_hash"]
    if (
        value["schema"] != PLAN_SCHEMA
        or value["reason"] not in REASONS
        or not IDENTITY.RUN_ID.fullmatch(value.get("run_id", ""))
        or not IDENTITY.TICKET.fullmatch(value.get("ticket", ""))
        or value.get("go_issued") not in ("0", "1")
        or not isinstance(value.get("pid"), int)
        or isinstance(value.get("pid"), bool)
        or not isinstance(value.get("pgid"), int)
        or isinstance(value.get("pgid"), bool)
        or value["pid"] <= 1
        or value["pid"] != value["pgid"]
        or not isinstance(value.get("process_start"), str)
        or not value["process_start"]
        or not re.fullmatch(r"[0-9a-f]{32}", value.get("nonce", ""))
        or not re.fullmatch(r"[0-9a-f]{64}", value.get("manifest_sha256", ""))
        or not re.fullmatch(r"[0-9a-f]{64}", value.get("pid_record_sha256", ""))
        or not re.fullmatch(r"[0-9a-f]{64}", supplied or "")
        or supplied != digest(canonical(unhashed))
        or supplied != expected_hash
    ):
        raise CancelError("cancel preview hash does not match the plan")


def read_request(factory_root: Path, ticket: str, run_id: str) -> dict:
    value, _ = secure_json(paths(factory_root, run_id)["request"])
    if (
        set(value) != {"plan", "requested_at", "schema"}
        or value.get("schema") != REQUEST_SCHEMA
        or not isinstance(value.get("plan"), dict)
    ):
        raise CancelError("cancellation request is malformed")
    plan = value["plan"]
    validate_plan(plan, plan.get("preview_hash", ""))
    if plan["ticket"] != ticket or plan["run_id"] != run_id:
        raise CancelError("cancellation request has the wrong owner")
    return value


def receipt_is_replay(path: Path, plan: dict) -> dict | None:
    try:
        value, _ = secure_json(path)
    except FileNotFoundError:
        return None
    if (
        value.get("schema") != RECEIPT_SCHEMA
        or value.get("ticket") != plan["ticket"]
        or value.get("run_id") != plan["run_id"]
        or value.get("reason") != plan["reason"]
        or value.get("preview_hash") != plan["preview_hash"]
    ):
        raise CancelError("existing cancellation receipt belongs to another request")
    return value


def terminal_matches_plan(manifest: dict[str, str], plan: dict) -> bool:
    state = "launch_void" if plan["go_issued"] == "0" else "cancelled_conservative"
    return (
        (
            manifest.get("phase") == state
            and manifest.get("accounting_state") == state
        )
        or (
            manifest.get("accounting_state")
            == manifest.get("terminal_intent_accounting_state")
            and manifest.get("phase") == manifest.get("terminal_intent_phase")
            and manifest.get("accounting_state") in TERMINAL_STATES
        )
    ) and (
        manifest.get("ticket") == plan["ticket"]
        and manifest.get("run_id") == plan["run_id"]
        and manifest.get("cancellation_reason") == plan["reason"]
        and manifest.get("cancellation_preview_hash") == plan["preview_hash"]
    )


def replace_fields(path: Path, values: dict[str, str]) -> None:
    raw = "".join(f"{key}={value}\n" for key, value in values.items()).encode()
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def validate_regular_or_absent(path: Path) -> bool:
    try:
        before = path.lstat()
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(before.st_mode) or path.is_symlink() or before.st_nlink != 1:
        raise CancelError(f"unsafe stale attempt record: {path.name}")
    return True


def unlink_regular(path: Path) -> None:
    if not validate_regular_or_absent(path):
        return
    path.unlink()


def validate_stale_claims(
    factory_root: Path, active_runs: Path, manifest: dict[str, str], now: int,
) -> tuple[int, int, int, int, bytes] | None:
    ticket, role = manifest["ticket"], manifest["role"]
    try:
        active_info = active_runs.lstat()
    except FileNotFoundError:
        active_info = None
    if active_info is not None and (
        active_runs.is_symlink()
        or not stat.S_ISDIR(active_info.st_mode)
        or active_info.st_uid != os.geteuid()
        or active_info.st_mode & 0o022
    ):
        raise CancelError("active-run state is unsafe")
    claim = active_runs / f"{ticket}.{role}.lock"
    if claim.exists() or claim.is_symlink():
        info = claim.lstat()
        entries = list(claim.iterdir()) if stat.S_ISDIR(info.st_mode) else []
        if (
            not stat.S_ISDIR(info.st_mode) or claim.is_symlink()
            or info.st_uid != os.geteuid() or info.st_mode & 0o022
            or [entry.name for entry in entries] != ["owner"]
        ):
            raise CancelError("active-run claim is unsafe")
        owner = IDENTITY.parse_fields(
            IDENTITY.record_bytes(claim / "owner"), "active-run owner",
        )
        if (
            set(owner) != {"pid", "process_start", "token"}
            or not owner["pid"].isdigit()
            or not owner["process_start"]
            or not re.fullmatch(r"[0-9a-f]{32}", owner["token"])
        ):
            raise CancelError("active-run owner is malformed")
        process = IDENTITY.process_table().get(int(owner["pid"]))
        if process is not None and process.started == owner["process_start"]:
            raise CancelError("active-run owner is still alive")
    lease = factory_root / f"factory/.dispatch-leases/{ticket}.json"
    if lease.exists() or lease.is_symlink():
        before = lease.lstat()
        value, raw = secure_json(lease)
        after = lease.lstat()
        if (
            (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            or before.st_uid != os.geteuid()
            or before.st_mode & 0o022
            or set(value) != {
                "claimed_epoch", "expires_epoch", "lease_id",
                "schema_version", "ticket",
            }
            or value.get("schema_version") != 1
            or value.get("ticket") != ticket
            or not isinstance(value.get("lease_id"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", value.get("lease_id", ""))
            or isinstance(value.get("claimed_epoch"), bool)
            or not isinstance(value.get("claimed_epoch"), int)
            or isinstance(value.get("expires_epoch"), bool)
            or not isinstance(value.get("expires_epoch"), int)
            or value["expires_epoch"] <= value["claimed_epoch"]
            or value["expires_epoch"] > now
        ):
            raise CancelError("dispatch lease is not expired for this ticket")
        return (
            before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, raw,
        )
    return None


def acquire_cleanup_lock(path: Path, label: str) -> None:
    for _ in range(100):
        try:
            path.mkdir(mode=0o700)
            return
        except FileExistsError:
            time.sleep(0.05)
    raise CancelError(f"{label} lock is busy")


def sealed_recovery_lock_held(path_value: str, descriptor_value: str) -> None:
    path = Path(path_value)
    if not path.is_absolute():
        raise CancelError("sealed recovery lock identity is invalid")
    try:
        inherited = int(descriptor_value)
        if inherited < 0:
            raise ValueError
        held = os.fstat(inherited)
        target = path.lstat()
    except (ValueError, OSError) as error:
        raise CancelError("sealed recovery lock capability is invalid") from error
    if (
        not stat.S_ISREG(held.st_mode)
        or not stat.S_ISREG(target.st_mode)
        or path.is_symlink()
        or (held.st_dev, held.st_ino) != (target.st_dev, target.st_ino)
        or held.st_uid != os.geteuid()
        or held.st_nlink != 1
        or stat.S_IMODE(held.st_mode) != 0o600
    ):
        raise CancelError("sealed recovery lock is unsafe")
    try:
        probe = os.open(path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
    except OSError as error:
        raise CancelError("sealed recovery lock capability is invalid") from error
    try:
        current = os.fstat(probe)
        if (current.st_dev, current.st_ino) != (held.st_dev, held.st_ino):
            raise CancelError("sealed recovery lock changed")
        try:
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        fcntl.flock(probe, fcntl.LOCK_UN)
        raise CancelError("sealed recovery lock is not held")
    finally:
        os.close(probe)


def sealed_recovery_locks_held(manifest: dict[str, str]) -> bool:
    names = (
        "FACTORY_CROSS_RELEASE_SOURCE_SHA",
        "FACTORY_CROSS_RELEASE_PRODUCT_ID",
        "FACTORY_DISPATCH_ADMISSION_LOCK",
        "FACTORY_DISPATCH_ADMISSION_LOCK_FD",
        "FACTORY_QUALIFICATION_CONTROLLER_LOCK",
        "FACTORY_QUALIFICATION_CONTROLLER_LOCK_FD",
    )
    values = {name: os.environ.get(name, "") for name in names}
    if not any(values.values()):
        return False
    if not all(values.values()):
        raise CancelError("sealed recovery admission identity is incomplete")
    source_sha = values["FACTORY_CROSS_RELEASE_SOURCE_SHA"]
    if (
        re.fullmatch(r"[0-9a-f]{40}", source_sha) is None
        or re.fullmatch(
            r"[A-Za-z0-9._:-]{1,200}",
            values["FACTORY_CROSS_RELEASE_PRODUCT_ID"],
        ) is None
        or manifest.get("kit_sha") != source_sha
        or manifest.get("contract_version") != "2.0.0"
    ):
        raise CancelError("sealed recovery admission identity is invalid")
    sealed_recovery_lock_held(
        values["FACTORY_DISPATCH_ADMISSION_LOCK"],
        values["FACTORY_DISPATCH_ADMISSION_LOCK_FD"],
    )
    sealed_recovery_lock_held(
        values["FACTORY_QUALIFICATION_CONTROLLER_LOCK"],
        values["FACTORY_QUALIFICATION_CONTROLLER_LOCK_FD"],
    )
    return True


def exact_lease(path: Path, expected: tuple[int, int, int, int, bytes]) -> None:
    before = path.lstat()
    raw = IDENTITY.record_bytes(path)
    after = path.lstat()
    current = (
        before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, raw,
    )
    if (
        current != expected
        or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    ):
        raise CancelError("dispatch lease changed before cleanup")


def release_stale_claims(
    factory_root: Path, active_runs: Path, manifest: dict[str, str], now: int,
) -> None:
    held: list[Path] = []
    try:
        if not sealed_recovery_locks_held(manifest):
            for path, label in (
                (factory_root / "factory/.launch.lock", "launch"),
                (factory_root / "factory/.dispatch-leases.lock", "dispatcher lease"),
            ):
                acquire_cleanup_lock(path, label)
                held.append(path)
        lease_identity = validate_stale_claims(
            factory_root, active_runs, manifest, now,
        )
        ticket, role = manifest["ticket"], manifest["role"]
        claim = active_runs / f"{ticket}.{role}.lock"
        lease = factory_root / f"factory/.dispatch-leases/{ticket}.json"
        if lease_identity is not None:
            exact_lease(lease, lease_identity)
        elif lease.exists() or lease.is_symlink():
            raise CancelError("dispatch lease appeared before cleanup")
        if claim.exists() or claim.is_symlink():
            (claim / "owner").unlink()
            claim.rmdir()
        if lease_identity is not None:
            exact_lease(lease, lease_identity)
            lease.unlink()
    finally:
        for path in reversed(held):
            path.rmdir()


def provider_database(factory_root: Path) -> Path:
    configured = os.environ.get("FACTORY_PROVIDER_DB")
    database = (
        Path(configured) if configured
        else factory_root.parent / "runtime/provider-state.sqlite3"
    )
    if not database.is_absolute():
        raise CancelError("provider accounting database path is not absolute")
    try:
        info = database.lstat()
    except FileNotFoundError as error:
        raise CancelError("provider accounting database is missing") from error
    if (
        database.is_symlink()
        or not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_nlink != 1
        or info.st_mode & 0o022
    ):
        raise CancelError("provider accounting database is unsafe")
    return database


def provider_account_lease(factory_root: Path, lease_id: str) -> dict | None:
    configured = os.environ.get("FACTORY_CURSOR_ACCOUNT_DB", "")
    database = (
        Path(configured) if configured
        else provider_database(factory_root).with_name("cursor-account.sqlite3")
    )
    if not database.is_absolute():
        raise CancelError("provider account database path is not absolute")
    if not database.exists() and not database.is_symlink():
        return None
    try:
        info = database.lstat()
        if (
            database.is_symlink()
            or not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
            or info.st_mode & 0o022
        ):
            raise CancelError("provider account database is unsafe")
        result = subprocess.run(
            [
                sys.executable, str(ROOT / "scripts/provider-coordinator.py"),
                "--db", str(provider_database(factory_root)),
                "--account-db", str(database), "account-status",
            ],
            check=True, capture_output=True, text=True,
        )
        value = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as error:
        raise CancelError("provider account lease lookup failed") from error
    leases = value.get("leases") if isinstance(value, dict) else None
    if not isinstance(leases, list) or any(not isinstance(item, dict) for item in leases):
        raise CancelError("provider account lease lookup failed")
    selected = [item for item in leases if item.get("lease_id") == lease_id]
    if len(selected) > 1:
        raise CancelError("provider account lease lookup failed")
    return selected[0] if selected else None


def provider_attempt(factory_root: Path, attempt_id: str) -> dict:
    try:
        result = subprocess.run(
            [
                sys.executable, str(ROOT / "scripts/provider-coordinator.py"),
                "--db", str(provider_database(factory_root)),
                "status", "--attempt-id", attempt_id,
            ],
            check=True, capture_output=True, text=True,
        )
        value = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as error:
        raise CancelError("provider attempt lookup failed") from error
    attempts = value.get("attempts") if isinstance(value, dict) else None
    if not isinstance(attempts, list) or len(attempts) != 1:
        raise CancelError("provider attempt lookup failed")
    return attempts[0]


def provider_only_product_id() -> str:
    project = os.environ.get("FACTORY_PROJECT", "")
    release = os.environ.get("FACTORY_RELEASE_SHA", "")
    product = os.environ.get("FACTORY_PROVIDER_PRODUCT_ID", "")
    if (
        re.fullmatch(r"[A-Za-z0-9._:-]{1,200}", project)
        and re.fullmatch(r"[0-9a-f]{40}", release)
        and product == f"{project}:{release}"
    ):
        return product
    source = os.environ.get("FACTORY_CROSS_RELEASE_SOURCE_SHA", "")
    product = os.environ.get("FACTORY_CROSS_RELEASE_PRODUCT_ID", "")
    if (
        re.fullmatch(r"[0-9a-f]{40}", source)
        and re.fullmatch(r"[A-Za-z0-9._:-]{1,200}", product)
        and product.endswith(f":{source}")
    ):
        return product
    raise CancelError(
        "provider-only cancellation requires sealed qualification identity"
    )


def validate_provider_only_initial(
    value: dict, ticket: str, run_id: str,
) -> None:
    product = provider_only_product_id()
    integers = (
        "machine_daily_cap_micro_usd", "prepared_at",
        "product_daily_cap_micro_usd", "reserve_micro_usd",
        "ticket_cap_micro_usd", "updated_at", "version",
    )
    if (
        set(value) != PROVIDER_ATTEMPT_FIELDS
        or not re.fullmatch(r"[0-9]{9,}-[1-9][0-9]*-cli", run_id)
        or value.get("attempt_id") != run_id
        or value.get("ticket_id") != ticket
        or value.get("product_id") != product
        or value.get("state") not in {"prepared", "reserved"}
        or value.get("version") != (
            1 if value.get("state") == "prepared" else 2
        )
        or any(
            not isinstance(value.get(name), int)
            or isinstance(value.get(name), bool)
            or value[name] < 0
            for name in integers
        )
        or value.get("reserve_micro_usd", 0) <= 0
        or any(
            value.get(name, -1) < value.get("reserve_micro_usd", 0)
            for name in (
                "machine_daily_cap_micro_usd",
                "product_daily_cap_micro_usd", "ticket_cap_micro_usd",
            )
        )
        or not re.fullmatch(r"[A-Za-z0-9._:@-]{1,200}", value.get("provider_family", ""))
        or not re.fullmatch(r"[A-Za-z0-9._:@-]{1,200}", value.get("account_route", ""))
        or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value.get("budget_day", ""))
        or value.get("go_at") is not None
        or value.get("submitted_at") is not None
        or value.get("terminal_at") is not None
        or value.get("terminal_result") is not None
        or value.get("charge_micro_usd") is not None
        or value.get("cancellation_requested_at") is not None
        or value.get("cancellation_reason") is not None
        or (
            value.get("state") == "prepared"
            and (
                value.get("admitted_at") is not None
                or value.get("policy_sha256") is not None
                or value.get("updated_at") != value.get("prepared_at")
            )
        )
        or (
            value.get("state") == "reserved"
            and (
                not isinstance(value.get("admitted_at"), int)
                or isinstance(value.get("admitted_at"), bool)
                or value.get("admitted_at") < value.get("prepared_at")
                or value.get("updated_at") != value.get("admitted_at")
                or not re.fullmatch(r"[0-9a-f]{64}", value.get("policy_sha256", ""))
            )
        )
    ):
        raise CancelError("provider-only attempt is not an exact pre-GO orphan")


def provider_only_records_absent(
    factory_root: Path, ticket: str, run_id: str, *, require_account_absent=True,
) -> None:
    attempt = paths(factory_root, run_id)
    match = re.fullmatch(r"([0-9]{9,})-([1-9][0-9]*)-cli", run_id)
    if match is None:
        raise CancelError("provider-only attempt identity is invalid")
    base_run = run_id[:-4]
    records = [
        attempt["runs"] / f"{selected}.{suffix}"
        for selected in (base_run, run_id)
        for suffix in ("meta", "pid", "out", "progress.jsonl", "wrapper")
    ] + [
        attempt["runs"] / f".{selected}.{suffix}"
        for selected in (base_run, run_id)
        for suffix in ("ready", "go", "gate", "submitted")
    ]
    if any(path.exists() or path.is_symlink() for path in records):
        raise CancelError("provider-only attempt has run evidence")
    if (
        require_account_absent
        and provider_account_lease(factory_root, f"{run_id}-account") is not None
    ):
        raise CancelError("provider-only attempt still has an account lease")
    validate_ledger_projection(factory_root, attempt)
    with attempt["ledger"].open(newline="", encoding="utf-8") as handle:
        if any(
            row.get("run_id") in {run_id, base_run}
            for row in csv.DictReader(handle)
        ):
            raise CancelError("provider-only attempt has runtime ledger evidence")
    active_runs = attempt["active_runs"]
    if active_runs.exists() or active_runs.is_symlink():
        info = active_runs.lstat()
        if (
            active_runs.is_symlink()
            or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o022
        ):
            raise CancelError("active-run state is unsafe")
        if any(active_runs.glob(f"{ticket}.*.lock")):
            raise CancelError("provider-only attempt still has an active run claim")
    wrapper_pid = int(match.group(2))
    table = IDENTITY.process_table()
    if wrapper_pid in table or any(
        process.pgid == wrapper_pid for process in table.values()
    ):
        raise CancelError("provider-only attempt wrapper is still live")
    runtime_root = os.environ.get("FACTORY_CLI_RUNTIME_ROOT", "")
    if runtime_root:
        root = Path(runtime_root)
        if not root.is_absolute():
            raise CancelError("provider CLI runtime root is invalid")
        for candidate in (root / "attempts" / run_id, root / "c" / run_id):
            if candidate.exists() or candidate.is_symlink():
                raise CancelError("provider-only attempt has worker runtime evidence")
    worker_root = os.environ.get("FACTORY_PROVIDER_ATTEMPT_ROOT", "")
    if worker_root:
        root = Path(worker_root)
        if not root.is_absolute():
            raise CancelError("provider worker root is invalid")
        candidate = root / run_id
        if candidate.exists() or candidate.is_symlink():
            raise CancelError("provider-only attempt has worker evidence")


def validate_provider_only_terminal(value: dict, plan: dict) -> None:
    initial = plan["provider_attempt"]
    expected = dict(initial)
    terminal_at = value.get("terminal_at")
    expected.update({
        "charge_micro_usd": 0,
        "state": "terminal",
        "terminal_at": terminal_at,
        "terminal_result": "failed_pre_go",
        "updated_at": terminal_at,
        "version": initial["version"] + 1,
    })
    if (
        not isinstance(terminal_at, int)
        or isinstance(terminal_at, bool)
        or terminal_at < initial["updated_at"]
        or value != expected
    ):
        raise CancelError("provider-only attempt terminal state does not match")


def validate_provider_only_plan(value: dict, expected_hash: str) -> None:
    if set(value) != {
        "created_at", "nonce", "preview_hash", "provider_attempt",
        "provider_attempt_sha256", "reason", "run_id", "schema", "ticket",
    }:
        raise CancelError("cancel plan has unexpected fields")
    supplied = value.get("preview_hash", "")
    unhashed = dict(value)
    unhashed.pop("preview_hash", None)
    attempt = value.get("provider_attempt")
    if (
        value.get("schema") != PROVIDER_ONLY_PLAN_SCHEMA
        or value.get("reason") not in REASONS
        or not isinstance(attempt, dict)
        or not re.fullmatch(r"[0-9a-f]{32}", value.get("nonce", ""))
        or not re.fullmatch(r"[0-9a-f]{64}", value.get("provider_attempt_sha256", ""))
        or value.get("provider_attempt_sha256") != digest(canonical(attempt))
        or attempt.get("attempt_id") != value.get("run_id")
        or attempt.get("ticket_id") != value.get("ticket")
        or not re.fullmatch(r"[0-9a-f]{64}", supplied)
        or supplied != digest(canonical(unhashed))
        or supplied != expected_hash
    ):
        raise CancelError("cancel preview hash does not match the plan")
    validate_provider_only_initial(attempt, value["ticket"], value["run_id"])


def provider_only_receipt_replay(path: Path, plan: dict) -> dict | None:
    try:
        value, _ = secure_json(path)
    except FileNotFoundError:
        return None
    if (
        set(value) != {
            "accounting_state", "charged_usd", "preview_hash",
            "provider_attempt_sha256", "reason", "run_id", "schema",
            "terminal_at", "ticket",
        }
        or value.get("schema") != PROVIDER_ONLY_RECEIPT_SCHEMA
        or value.get("accounting_state") != "failed_pre_go"
        or value.get("charged_usd") != "0"
        or value.get("preview_hash") != plan["preview_hash"]
        or value.get("reason") != plan["reason"]
        or value.get("run_id") != plan["run_id"]
        or value.get("ticket") != plan["ticket"]
        or not re.fullmatch(r"[0-9a-f]{64}", value.get("provider_attempt_sha256", ""))
    ):
        raise CancelError("existing cancellation receipt belongs to another request")
    return value


def calculate_provider_only(
    factory_root: Path, ticket: str, run_id: str, reason: str,
    nonce: str | None,
) -> dict:
    provider_only_records_absent(
        factory_root, ticket, run_id, require_account_absent=False,
    )
    attempt_paths = paths(factory_root, run_id)
    status = provider_attempt(factory_root, run_id)
    if attempt_paths["request"].exists() or attempt_paths["request"].is_symlink():
        request = read_request(factory_root, ticket, run_id)
        plan = request["plan"]
        if plan.get("schema") != PROVIDER_ONLY_PLAN_SCHEMA or plan["reason"] != reason:
            raise CancelError("existing cancellation request belongs to another plan")
        initial = plan["provider_attempt"]
        if status != initial:
            validate_provider_only_terminal(status, plan)
        replay = provider_only_receipt_replay(attempt_paths["receipt"], plan)
        if replay is not None and replay["provider_attempt_sha256"] != digest(canonical(status)):
            raise CancelError("existing cancellation receipt disagrees with the attempt")
        return plan
    validate_provider_only_initial(status, ticket, run_id)
    raw = canonical(status)
    nonce = nonce or digest(raw + b"\0" + reason.encode())[:32]
    created_at = dt.datetime.fromtimestamp(
        status["updated_at"], dt.timezone.utc,
    ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    plan = {
        "created_at": created_at,
        "nonce": nonce,
        "provider_attempt": status,
        "provider_attempt_sha256": digest(raw),
        "reason": reason,
        "run_id": run_id,
        "schema": PROVIDER_ONLY_PLAN_SCHEMA,
        "ticket": ticket,
    }
    plan["preview_hash"] = digest(canonical(plan))
    return plan


def apply_provider_only_plan(factory_root: Path, plan: dict) -> dict:
    validate_provider_only_plan(plan, plan["preview_hash"])
    ticket, run_id = plan["ticket"], plan["run_id"]
    provider_only_records_absent(factory_root, ticket, run_id)
    attempt_paths = paths(factory_root, run_id)
    request = {"plan": plan, "requested_at": timestamp(), "schema": REQUEST_SCHEMA}
    try:
        durable_create(attempt_paths["request"], canonical(request))
    except FileExistsError:
        existing, _ = secure_json(attempt_paths["request"])
        if existing.get("schema") != REQUEST_SCHEMA or existing.get("plan") != plan:
            raise CancelError("another cancellation request won the CAS")
    provider_only_records_absent(factory_root, ticket, run_id)
    status = provider_attempt(factory_root, run_id)
    if status == plan["provider_attempt"]:
        try:
            result = subprocess.run(
                [
                    sys.executable, str(ROOT / "scripts/provider-coordinator.py"),
                    "--db", str(provider_database(factory_root)), "terminalize",
                    "--operation-id", f"provider-only-cancel-{plan['preview_hash'][:24]}",
                    "--attempt-id", run_id,
                    "--expected-version", str(status["version"]),
                    "--result", "failed_pre_go", "--charge-micro-usd", "0",
                ],
                check=True, capture_output=True, text=True,
            )
            json.loads(result.stdout)
            status = provider_attempt(factory_root, run_id)
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as error:
            raise CancelError("provider-only cancellation CAS failed") from error
    validate_provider_only_terminal(status, plan)
    provider_only_records_absent(factory_root, ticket, run_id)
    replay = provider_only_receipt_replay(attempt_paths["receipt"], plan)
    if replay is not None:
        if replay["provider_attempt_sha256"] != digest(canonical(status)):
            raise CancelError("existing cancellation receipt disagrees with the attempt")
        return replay
    receipt = {
        "accounting_state": "failed_pre_go",
        "charged_usd": "0",
        "preview_hash": plan["preview_hash"],
        "provider_attempt_sha256": digest(canonical(status)),
        "reason": plan["reason"],
        "run_id": run_id,
        "schema": PROVIDER_ONLY_RECEIPT_SCHEMA,
        "terminal_at": dt.datetime.fromtimestamp(
            status["terminal_at"], dt.timezone.utc,
        ).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "ticket": ticket,
    }
    try:
        durable_create(attempt_paths["receipt"], canonical(receipt))
    except FileExistsError:
        replay = provider_only_receipt_replay(attempt_paths["receipt"], plan)
        if replay is None or replay != receipt:
            raise CancelError("cancellation receipt CAS failed")
        return replay
    return receipt


def terminal_intent(
    manifest: dict[str, str], status: dict | None = None,
) -> dict[str, str] | None:
    state = manifest.get("terminal_intent_accounting_state", "")
    phase = manifest.get("terminal_intent_phase", "")
    if manifest.get("phase") != "terminalizing" and not (
        manifest.get("phase") == phase
        and manifest.get("accounting_state") == state
    ):
        return None
    result = manifest.get("terminal_intent_result", "")
    charge = manifest.get("terminal_intent_charge_micro_usd", "")
    try:
        charge_micro = int(charge) if re.fullmatch(r"0|[1-9][0-9]{0,15}", charge) else -1
        reserve_micro = micro_usd(manifest["reserved_usd"])
        effective_micro = micro_usd(manifest["effective_cost"])
    except (CancelError, KeyError) as error:
        raise CancelError("provider terminal intent is malformed") from error
    exit_status = manifest.get("exit_status", "")
    if (
        state not in TERMINAL_STATES
        or phase not in {
            "completed", "abandoned", "launch_void", "cancelled_conservative",
        }
        or result not in {"succeeded", "failed", "failed_pre_go", "cancelled"}
        or charge_micro < 0
        or charge_micro != effective_micro
        or charge_micro > reserve_micro
        or not re.fullmatch(r"[0-9]{1,7}", manifest.get("turns", ""))
        or not re.fullmatch(r"[0-9]{1,7}(?:\.[0-9]{1,18})?", manifest.get("effective_cost", ""))
        or not re.fullmatch(r"[0-9]{1,3}", exit_status)
        or int(exit_status) > 255
        or not manifest.get("cost_basis")
        or (state == "completed" and phase != "completed")
        or (state == "abandoned_conservative" and phase not in {"completed", "abandoned"})
        or (state == "cancelled_conservative" and phase != "cancelled_conservative")
        or (state == "launch_void" and phase not in {"completed", "abandoned", "launch_void"})
        or (
            result == "succeeded"
            and (state not in {"completed", "abandoned_conservative"} or exit_status != "0")
        )
        or (
            result == "failed"
            and (state not in {"completed", "abandoned_conservative"} or exit_status == "0")
        )
        or (
            result == "cancelled"
            and state not in {"cancelled_conservative", "launch_void"}
        )
        or (
            result == "failed_pre_go"
            and (state != "launch_void" or exit_status == "0")
        )
        or (state == "launch_void") != (manifest.get("go_issued") == "0")
        or (state == "launch_void" and manifest.get("task_submitted") != "0")
        or (
            state == "launch_void"
            and (
                charge_micro != 0
                or manifest.get("effective_cost") != "0"
                or manifest.get("cost_basis") != "launch_void"
            )
        )
        or (
            state in {"abandoned_conservative", "cancelled_conservative"}
            and (
                charge_micro != reserve_micro
                or manifest.get("cost_basis") != "conservative_reservation"
            )
        )
    ):
        raise CancelError("provider terminal intent does not match the attempt")
    if status is not None and (
        status.get("terminal_result") != result
        or status.get("charge_micro_usd") != charge_micro
        or not isinstance(status.get("terminal_at"), int)
        or isinstance(status.get("terminal_at"), bool)
    ):
        raise CancelError("provider terminal intent does not match the attempt")
    return {
        "charge_micro_usd": str(charge_micro), "phase": phase,
        "result": result, "state": state,
    }


def converge_provider_attempt(
    factory_root: Path, manifest: dict[str, str], plan: dict,
) -> dict[str, str] | None:
    attempt_id = manifest.get("provider_attempt_id", "")
    if not attempt_id:
        return
    database = provider_database(factory_root)
    command = [
        sys.executable, str(ROOT / "scripts/provider-coordinator.py"),
        "--db", str(database),
    ]

    def invoke(arguments: list[str]) -> dict:
        try:
            result = subprocess.run(
                [*command, *arguments], check=True, capture_output=True, text=True,
            )
            value = json.loads(result.stdout)
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as error:
            raise CancelError("provider attempt reconciliation failed") from error
        if not isinstance(value, dict) or value.get("status") == "error":
            raise CancelError("provider attempt reconciliation failed")
        return value

    attempts = invoke(["status", "--attempt-id", attempt_id]).get("attempts")
    if not isinstance(attempts, list) or len(attempts) != 1:
        raise CancelError("provider attempt identity disagrees with the run")
    status = attempts[0]
    go_at = status.get("go_at")
    submitted_at = status.get("submitted_at")
    submitted_ns = manifest.get("submitted_at_epoch_ns", "")
    submitted_value = (
        int(submitted_ns)
        if re.fullmatch(r"[1-9][0-9]{0,19}", submitted_ns)
        else None
    )
    source_sha = os.environ.get("FACTORY_CROSS_RELEASE_SOURCE_SHA", "")
    legacy_product_id = os.environ.get("FACTORY_CROSS_RELEASE_PRODUCT_ID", "")
    legacy_identity = (
        re.fullmatch(r"[0-9a-f]{40}", source_sha) is not None
        and re.fullmatch(r"[A-Za-z0-9._:-]{1,200}", legacy_product_id) is not None
        and manifest.get("kit_sha") == source_sha
        and manifest.get("ticket_kit_sha") == source_sha
        and manifest.get("contract_version") == "2.0.0"
        and "provider_product_id" not in manifest
        and "submitted_at_epoch_ns" not in manifest
        and manifest.get("go_issued") == "1"
        and manifest.get("task_submitted") == "0"
        and status.get("product_id") == legacy_product_id
        and isinstance(go_at, int) and not isinstance(go_at, bool)
        and isinstance(submitted_at, int) and not isinstance(submitted_at, bool)
    )
    if (
        status.get("attempt_id") != attempt_id
        or status.get("ticket_id") != plan["ticket"]
        or (
            status.get("product_id") != manifest.get("provider_product_id")
            and not legacy_identity
        )
        or status.get("provider_family") != manifest.get("provider_family")
        or status.get("account_route") != manifest.get("account_route_id")
        or (
            status.get("admitted_at") is not None
            and status.get("policy_sha256")
            != manifest.get("activation_policy_sha256")
        )
        or (
            status.get("admitted_at") is None
            and status.get("policy_sha256") is not None
        )
        or any(isinstance(value, bool) for value in (go_at, submitted_at))
        or manifest.get("task_submitted") not in {"0", "1"}
        or (manifest.get("go_issued") == "1") != isinstance(go_at, int)
        or (
            (manifest.get("task_submitted") == "1") != isinstance(submitted_at, int)
            and not legacy_identity
        )
        or (isinstance(submitted_at, int) and not isinstance(go_at, int))
        or ((submitted_value is None) != (submitted_at is None) and not legacy_identity)
        or (
            isinstance(submitted_at, int)
            and not legacy_identity
            and not submitted_at * 1_000_000_000
            <= submitted_value
            <= (submitted_at + 1) * 1_000_000_000 - 1
        )
        or status.get("reserve_micro_usd") != micro_usd(manifest["reserved_usd"])
    ):
        raise CancelError("provider attempt identity disagrees with the run")
    intent = terminal_intent(manifest)
    if status.get("state") == "terminal":
        if intent is not None:
            terminal_intent(manifest, status)
            intent["terminal_at"] = dt.datetime.fromtimestamp(
                status["terminal_at"], dt.timezone.utc,
            ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            intent["terminal_at_epoch_ns"] = str(status["terminal_at"] * 1_000_000_000)
            return intent
    else:
        if intent is not None and (
            (
                manifest.get("go_issued") == "1"
                and status.get("state") not in {"GO", "submitted"}
            )
            or (
                manifest.get("go_issued") == "0"
                and status.get("state") not in {"prepared", "reserved"}
            )
        ):
            raise CancelError("provider terminal intent disagrees with GO state")
        result_name = intent["result"] if intent is not None else "cancelled"
        charge = (
            intent["charge_micro_usd"] if intent is not None
            else "0" if plan["go_issued"] == "0"
            else str(status["reserve_micro_usd"])
        )
        result = invoke([
            "terminalize",
            "--operation-id", f"cancel-reconcile-{plan['preview_hash'][:24]}",
            "--attempt-id", attempt_id,
            "--expected-version", str(status["version"]),
            "--result", result_name,
            "--charge-micro-usd", charge,
        ])
        status = result.get("attempt", result)
        if intent is not None:
            terminal_intent(manifest, status)
            intent["terminal_at"] = dt.datetime.fromtimestamp(
                status["terminal_at"], dt.timezone.utc,
            ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            intent["terminal_at_epoch_ns"] = str(status["terminal_at"] * 1_000_000_000)
            return intent
    expected_charge = 0 if plan["go_issued"] == "0" else status.get("reserve_micro_usd")
    if (
        status.get("state") != "terminal"
        or status.get("terminal_result") != "cancelled"
        or status.get("charge_micro_usd") != expected_charge
    ):
        raise CancelError("provider cancellation did not converge")
    return None


def validate_ledger_projection(factory_root: Path, attempt: dict[str, Path]) -> None:
    try:
        subprocess.run(
            [
                sys.executable, str(ROOT / "scripts/ledger-view.py"), "print",
                "--factory-root", str(factory_root),
                "--durable-ledger", str(attempt["durable_ledger"]),
                "--runtime-ledger", str(attempt["ledger"]),
                "--runs-dir", str(attempt["runs"]),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise CancelError("cancellation ledger projection is invalid") from error


def converge_stale_attempt(factory_root: Path, plan: dict) -> dict:
    attempt = paths(factory_root, plan["run_id"])
    manifest_raw = IDENTITY.record_bytes(attempt["manifest"])
    manifest = IDENTITY.parse_fields(manifest_raw, "run manifest")
    now = int(time.time())
    for suffix in ("pid", "ready", "go", "gate", "submitted"):
        name = (
            f"{plan['run_id']}.{suffix}" if suffix == "pid"
            else f".{plan['run_id']}.{suffix}"
        )
        validate_regular_or_absent(attempt["runs"] / name)
    validate_stale_claims(factory_root, attempt["active_runs"], manifest, now)
    validate_ledger_projection(factory_root, attempt)
    if not terminal_matches_plan(manifest, plan):
        if digest(manifest_raw) != plan["manifest_sha256"]:
            raise CancelError("attempt changed after cancellation preview")
        if IDENTITY.group_alive(plan["pgid"]):
            raise CancelError("stale attempt process group became active")
        intent = converge_provider_attempt(factory_root, manifest, plan)
        state = (
            intent["state"] if intent is not None
            else "launch_void" if plan["go_issued"] == "0"
            else "cancelled_conservative"
        )
        phase = intent["phase"] if intent is not None else state
        manifest.update({
            "phase": phase,
            "accounting_state": state,
            "terminal_at": intent["terminal_at"] if intent is not None else timestamp(),
            "terminal_at_epoch_ns": (
                intent["terminal_at_epoch_ns"] if intent is not None
                else manifest.get("terminal_at_epoch_ns", "")
            ),
            "turns": manifest.get("turns") or "0",
            "effective_cost": (
                manifest["effective_cost"] if intent is not None
                else "0" if state == "launch_void" else manifest["reserved_usd"]
            ),
            "exit_status": manifest["exit_status"] if intent is not None else "130",
            "cost_basis": (
                manifest["cost_basis"] if intent is not None
                else "launch_void" if state == "launch_void"
                else "conservative_reservation"
            ),
            "role_exit": manifest.get("role_exit", "") if intent is not None else "cancelled",
            "cancellation_reason": plan["reason"],
            "cancellation_preview_hash": plan["preview_hash"],
            "updated_at": timestamp(),
        })
        replace_fields(attempt["manifest"], manifest)
    else:
        converge_provider_attempt(factory_root, manifest, plan)
    for suffix in ("pid", "ready", "go", "gate", "submitted"):
        name = (
            f"{plan['run_id']}.{suffix}" if suffix == "pid"
            else f".{plan['run_id']}.{suffix}"
        )
        unlink_regular(attempt["runs"] / name)
    release_stale_claims(
        factory_root, attempt["active_runs"], manifest, int(time.time()),
    )
    subprocess.run(
        [
            sys.executable, str(ROOT / "scripts/ledger-view.py"), "refresh",
            "--factory-root", str(factory_root),
            "--durable-ledger", str(attempt["durable_ledger"]),
            "--runtime-ledger", str(attempt["ledger"]),
            "--runs-dir", str(attempt["runs"]),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    return emit_receipt(factory_root, plan["ticket"], plan["run_id"])


def apply_plan(factory_root: Path, plan: dict, timeout: float) -> dict:
    if plan.get("schema") == PROVIDER_ONLY_PLAN_SCHEMA:
        return apply_provider_only_plan(factory_root, plan)
    attempt = paths(factory_root, plan["run_id"])
    replay = receipt_is_replay(attempt["receipt"], plan)
    if replay is not None:
        manifest_raw = IDENTITY.record_bytes(attempt["manifest"])
        manifest = IDENTITY.parse_fields(manifest_raw, "run manifest")
        if (
            not terminal_matches_plan(manifest, plan)
            or replay.get("manifest_sha256") != digest(manifest_raw)
            or replay.get("accounting_state") != manifest.get("accounting_state")
            or replay.get("charged_usd") != manifest.get("effective_cost")
        ):
            raise CancelError("existing cancellation receipt disagrees with the attempt")
        return converge_stale_attempt(factory_root, plan)
    manifest = IDENTITY.parse_fields(
        IDENTITY.record_bytes(attempt["manifest"]), "run manifest",
    )
    if terminal_matches_plan(manifest, plan):
        return converge_stale_attempt(factory_root, plan)
    identity, active = load_active_or_stale_identity(
        attempt["runs"], plan["run_id"], plan["ticket"],
    )
    if (
        identity.leader.pid != plan["pid"]
        or identity.leader.pgid != plan["pgid"]
        or identity.leader.started != plan["process_start"]
        or digest(IDENTITY.record_bytes(attempt["manifest"])) != plan["manifest_sha256"]
        or digest(IDENTITY.record_bytes(attempt["pid"])) != plan["pid_record_sha256"]
    ):
        raise CancelError("attempt changed after cancellation preview")
    request = {"plan": plan, "requested_at": timestamp(), "schema": REQUEST_SCHEMA}
    request_raw = canonical(request)
    try:
        durable_create(attempt["request"], request_raw)
    except FileExistsError:
        existing, _ = secure_json(attempt["request"])
        if (
            existing.get("schema") != REQUEST_SCHEMA
            or existing.get("plan") != plan
        ):
            raise CancelError("another cancellation request won the CAS")
    if not active:
        return converge_stale_attempt(factory_root, plan)
    escalation = IDENTITY.terminate(identity, min(timeout, 2.0))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        replay = receipt_is_replay(attempt["receipt"], plan)
        if replay is not None:
            return replay
        time.sleep(0.05)
    raise CancelError(
        f"attempt received {escalation} but cancellation accounting did not converge"
    )


def ledger_row(path: Path, run_id: str) -> dict:
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise CancelError("runtime ledger is missing or unsafe")
    with path.open(newline="", encoding="utf-8") as handle:
        matches = [row for row in csv.DictReader(handle) if row.get("run_id") == run_id]
    if len(matches) != 1:
        raise CancelError("cancellation accounting is not uniquely materialized")
    return matches[0]


def emit_receipt(factory_root: Path, ticket: str, run_id: str) -> dict:
    attempt = paths(factory_root, run_id)
    request = read_request(factory_root, ticket, run_id)
    plan = request["plan"]
    replay = receipt_is_replay(attempt["receipt"], plan)
    if replay is not None:
        return replay
    if attempt["pid"].exists() or attempt["pid"].is_symlink():
        raise CancelError("attempt process record has not drained")
    manifest_raw = IDENTITY.record_bytes(attempt["manifest"])
    manifest = IDENTITY.parse_fields(manifest_raw, "run manifest")
    state = manifest.get("accounting_state")
    if (
        state not in TERMINAL_STATES
        or not terminal_matches_plan(manifest, plan)
    ):
        raise CancelError("attempt cancellation is not terminal")
    if (state == "launch_void") != (manifest.get("go_issued") == "0"):
        raise CancelError("cancellation accounting does not match GO state")
    row = ledger_row(attempt["ledger"], run_id)
    expected_cost = (
        "0" if state == "launch_void"
        else manifest.get("reserved_usd") if state == "cancelled_conservative"
        else manifest.get("effective_cost")
    )
    if (
        row.get("ticket") != ticket
        or row.get("cost_usd") != expected_cost
        or row.get("exit_status") != manifest.get("exit_status")
    ):
        raise CancelError("runtime ledger does not match cancellation accounting")
    receipt = {
        "accounting_state": state,
        "charged_usd": expected_cost,
        "manifest_sha256": digest(manifest_raw),
        "preview_hash": plan["preview_hash"],
        "reason": plan["reason"],
        "run_id": run_id,
        "schema": RECEIPT_SCHEMA,
        "terminal_at": manifest["terminal_at"],
        "ticket": ticket,
    }
    try:
        durable_create(attempt["receipt"], canonical(receipt))
    except FileExistsError:
        replay = receipt_is_replay(attempt["receipt"], plan)
        if replay is None:
            raise CancelError("cancellation receipt CAS failed")
        return replay
    return receipt


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser()
    subparsers = value.add_subparsers(dest="action", required=True)
    preview = subparsers.add_parser("preview")
    for child in (preview,):
        child.add_argument("--factory-root", required=True, type=Path)
        child.add_argument("--ticket", required=True)
        child.add_argument("--run-id", required=True)
    preview.add_argument("--reason", required=True, choices=sorted(REASONS))
    apply = subparsers.add_parser("apply")
    apply.add_argument("--factory-root", required=True, type=Path)
    apply.add_argument("--plan", required=True, type=Path)
    apply.add_argument("--preview-hash", required=True)
    apply.add_argument("--timeout", type=float, default=15.0)
    request = subparsers.add_parser("request")
    request.add_argument("--factory-root", required=True, type=Path)
    request.add_argument("--ticket", required=True)
    request.add_argument("--run-id", required=True)
    receipt = subparsers.add_parser("receipt")
    receipt.add_argument("--factory-root", required=True, type=Path)
    receipt.add_argument("--ticket", required=True)
    receipt.add_argument("--run-id", required=True)
    return value


def main() -> None:
    args = parser().parse_args()
    if args.action == "preview":
        result = calculate(
            args.factory_root.resolve(), args.ticket, args.run_id,
            args.reason, None,
        )
    elif args.action == "apply":
        plan, _ = secure_json(args.plan)
        validate_plan(plan, args.preview_hash)
        result = apply_plan(args.factory_root.resolve(), plan, args.timeout)
    elif args.action == "request":
        request = read_request(args.factory_root.resolve(), args.ticket, args.run_id)
        result = {
            "preview_hash": request["plan"]["preview_hash"],
            "reason": request["plan"]["reason"],
        }
    else:
        factory_root = args.factory_root.resolve()
        request = read_request(factory_root, args.ticket, args.run_id)
        if request["plan"].get("schema") == PROVIDER_ONLY_PLAN_SCHEMA:
            status = provider_attempt(factory_root, args.run_id)
            validate_provider_only_terminal(status, request["plan"])
            result = provider_only_receipt_replay(
                paths(factory_root, args.run_id)["receipt"], request["plan"],
            )
            if result is None:
                raise CancelError("provider-only cancellation receipt is missing")
            if result["provider_attempt_sha256"] != digest(canonical(status)):
                raise CancelError("existing cancellation receipt disagrees with the attempt")
        else:
            result = emit_receipt(factory_root, args.ticket, args.run_id)
    sys.stdout.buffer.write(canonical(result))


if __name__ == "__main__":
    try:
        main()
    except (OSError, CancelError, IDENTITY.IdentityError) as error:
        raise SystemExit(f"attempt-cancel: {error}")
