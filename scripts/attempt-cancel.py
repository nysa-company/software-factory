#!/usr/bin/env python3
"""Preview and CAS-apply cancellation of exactly one factory attempt."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
from decimal import Decimal
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
REQUEST_SCHEMA = "nysa.software-factory.attempt-cancel-request/v1"
RECEIPT_SCHEMA = "nysa.software-factory.attempt-cancellation/v1"
TERMINAL_STATES = frozenset(("launch_void", "cancelled_conservative"))


class CancelError(ValueError):
    pass


def canonical(value) -> bytes:
    return (json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n").encode()


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def timestamp() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


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
    return {
        "runs": runs,
        "manifest": runs / f"{run_id}.meta",
        "pid": runs / f"{run_id}.pid",
        "request": runs / f"{run_id}.cancel-request.json",
        "receipt": runs / f"{run_id}.cancel.json",
        "ledger": factory_root / "factory/runtime-ledger.csv",
    }


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
        manifest.get("phase") == state
        and manifest.get("accounting_state") == state
        and manifest.get("ticket") == plan["ticket"]
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


def unlink_regular(path: Path) -> None:
    try:
        before = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISREG(before.st_mode) or path.is_symlink() or before.st_nlink != 1:
        raise CancelError(f"unsafe stale attempt record: {path.name}")
    path.unlink()


def release_stale_claims(factory_root: Path, manifest: dict[str, str], now: int) -> None:
    ticket, role = manifest["ticket"], manifest["role"]
    claim = factory_root / f"factory/.active-runs/{ticket}.{role}.lock"
    if claim.exists() or claim.is_symlink():
        info = claim.lstat()
        entries = list(claim.iterdir()) if stat.S_ISDIR(info.st_mode) else []
        if (
            not stat.S_ISDIR(info.st_mode) or claim.is_symlink()
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
        (claim / "owner").unlink()
        claim.rmdir()
    lease = factory_root / f"factory/.dispatch-leases/{ticket}.json"
    if lease.exists() or lease.is_symlink():
        value, _ = secure_json(lease)
        if (
            set(value) != {
                "claimed_epoch", "expires_epoch", "lease_id",
                "schema_version", "ticket",
            }
            or value.get("schema_version") != 1
            or value.get("ticket") != ticket
            or not isinstance(value.get("expires_epoch"), int)
            or value["expires_epoch"] > now
        ):
            raise CancelError("dispatch lease is not expired for this ticket")
        lease.unlink()


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


def converge_provider_attempt(factory_root: Path, manifest: dict[str, str], plan: dict) -> None:
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
    if (
        status.get("attempt_id") != attempt_id
        or status.get("ticket_id") != plan["ticket"]
        or status.get("reserve_micro_usd") != int(
            Decimal(manifest["reserved_usd"]) * 1_000_000
        )
    ):
        raise CancelError("provider attempt identity disagrees with the run")
    if status.get("state") != "terminal":
        result = invoke([
            "terminalize",
            "--operation-id", f"cancel-reconcile-{plan['preview_hash'][:24]}",
            "--attempt-id", attempt_id,
            "--expected-version", str(status["version"]),
            "--result", "cancelled",
            "--charge-micro-usd", str(status["reserve_micro_usd"]),
        ])
        status = result.get("attempt", result)
    if (
        status.get("state") != "terminal"
        or status.get("terminal_result") != "cancelled"
        or status.get("charge_micro_usd") != status.get("reserve_micro_usd")
    ):
        raise CancelError("provider cancellation did not converge")


def converge_stale_attempt(factory_root: Path, plan: dict) -> dict:
    attempt = paths(factory_root, plan["run_id"])
    manifest_raw = IDENTITY.record_bytes(attempt["manifest"])
    manifest = IDENTITY.parse_fields(manifest_raw, "run manifest")
    if not terminal_matches_plan(manifest, plan):
        if digest(manifest_raw) != plan["manifest_sha256"]:
            raise CancelError("attempt changed after cancellation preview")
        if IDENTITY.group_alive(plan["pgid"]):
            raise CancelError("stale attempt process group became active")
        converge_provider_attempt(factory_root, manifest, plan)
        state = "launch_void" if plan["go_issued"] == "0" else "cancelled_conservative"
        manifest.update({
            "phase": state,
            "accounting_state": state,
            "terminal_at": timestamp(),
            "turns": manifest.get("turns") or "0",
            "effective_cost": "0" if state == "launch_void" else manifest["reserved_usd"],
            "exit_status": "130",
            "cost_basis": "launch_void" if state == "launch_void" else "conservative_reservation",
            "role_exit": "cancelled",
            "cancellation_reason": plan["reason"],
            "cancellation_preview_hash": plan["preview_hash"],
            "updated_at": timestamp(),
        })
        replace_fields(attempt["manifest"], manifest)
    for suffix in ("pid", "ready", "go", "gate", "submitted"):
        name = (
            f"{plan['run_id']}.{suffix}" if suffix == "pid"
            else f".{plan['run_id']}.{suffix}"
        )
        unlink_regular(attempt["runs"] / name)
    release_stale_claims(factory_root, manifest, int(time.time()))
    subprocess.run(
        [
            sys.executable, str(ROOT / "scripts/ledger-view.py"), "refresh",
            "--factory-root", str(factory_root),
            "--durable-ledger", str(factory_root / "factory/ledger.csv"),
            "--runtime-ledger", str(attempt["ledger"]),
            "--runs-dir", str(attempt["runs"]),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    return emit_receipt(factory_root, plan["ticket"], plan["run_id"])


def apply_plan(factory_root: Path, plan: dict, timeout: float) -> dict:
    attempt = paths(factory_root, plan["run_id"])
    replay = receipt_is_replay(attempt["receipt"], plan)
    if replay is not None:
        return replay
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
        or manifest.get("phase") != state
        or manifest.get("ticket") != ticket
        or manifest.get("run_id") != run_id
        or manifest.get("cancellation_reason") != plan["reason"]
        or manifest.get("cancellation_preview_hash") != plan["preview_hash"]
    ):
        raise CancelError("attempt cancellation is not terminal")
    if (state == "launch_void") != (manifest.get("go_issued") == "0"):
        raise CancelError("cancellation accounting does not match GO state")
    row = ledger_row(attempt["ledger"], run_id)
    expected_cost = "0" if state == "launch_void" else manifest.get("reserved_usd")
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
        result = emit_receipt(args.factory_root.resolve(), args.ticket, args.run_id)
    sys.stdout.buffer.write(canonical(result))


if __name__ == "__main__":
    try:
        main()
    except (OSError, CancelError, IDENTITY.IdentityError) as error:
        raise SystemExit(f"attempt-cancel: {error}")
