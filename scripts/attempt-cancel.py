#!/usr/bin/env python3
"""Preview and CAS-apply cancellation of exactly one factory attempt."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import stat
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


def calculate(factory_root: Path, ticket: str, run_id: str, reason: str, nonce: str) -> dict:
    if reason not in REASONS:
        raise CancelError("cancellation reason is not eligible")
    attempt = paths(factory_root, run_id)
    identity = IDENTITY.load_identity(
        attempt["runs"], run_id, expected_ticket=ticket,
    )
    manifest_raw = IDENTITY.record_bytes(attempt["manifest"])
    manifest = IDENTITY.parse_fields(manifest_raw, "run manifest")
    pid_raw = IDENTITY.record_bytes(attempt["pid"])
    if (
        manifest.get("accounting_schema") != "1"
        or manifest.get("accounting_state") != "reserved"
        or manifest.get("go_issued") not in ("0", "1")
    ):
        raise CancelError("attempt is not an active accounting reservation")
    plan = {
        "created_at": timestamp(),
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


def apply_plan(factory_root: Path, plan: dict, timeout: float) -> dict:
    attempt = paths(factory_root, plan["run_id"])
    replay = receipt_is_replay(attempt["receipt"], plan)
    if replay is not None:
        return replay
    identity = IDENTITY.load_identity(
        attempt["runs"], plan["run_id"], expected_ticket=plan["ticket"],
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
            args.reason, secrets.token_hex(16),
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
