#!/usr/bin/env python3
"""Validate and consume one-use operator model-fallback approvals."""

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
import operator_receipt  # noqa: E402


SHA256 = re.compile(r"[0-9a-f]{64}\Z")
RUN_ID = re.compile(r"[A-Za-z0-9._-]{1,200}\Z")
TICKET = re.compile(r"T-[0-9]+\Z")
REASONS = frozenset((
    "budget_exhausted", "credits_exhausted", "operator_requested",
    "provider_unavailable",
))
APPROVAL_KEYS = frozenset((
    "approval_hash", "expires_at", "failed_run_id", "nonce", "observed_at",
    "operator_id", "reason", "receipt_sha256", "schema",
))


class ApprovalError(ValueError):
    pass


def timestamp(value, name):
    if not isinstance(value, str):
        raise ApprovalError(f"{name} must be a timestamp")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ApprovalError(f"{name} is invalid") from exc
    if parsed.tzinfo is None:
        raise ApprovalError(f"{name} must include a timezone")
    return parsed.astimezone(dt.timezone.utc)


def load_map(path):
    if path.is_symlink() or not path.is_file():
        raise ApprovalError("operator map is missing or unsafe")
    info = path.stat()
    if info.st_nlink != 1 or not stat.S_ISREG(info.st_mode):
        raise ApprovalError("operator map must be a single-link regular file")
    try:
        value = json.loads(path.read_text())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ApprovalError("operator map is malformed") from exc
    if not isinstance(value, dict) or not isinstance(value.get("tickets"), dict):
        raise ApprovalError("operator map has invalid tickets")
    return value, stat.S_IMODE(info.st_mode)


def validate(value, ticket, failed_run, reason, approval_hash=None, now=None):
    if not isinstance(value, dict) or set(value) != APPROVAL_KEYS:
        raise ApprovalError("fallback approval has invalid fields")
    if (
        value.get("schema") != "model-fallback-receipt-approval/v1"
        or not SHA256.fullmatch(value.get("approval_hash", ""))
        or not SHA256.fullmatch(value.get("receipt_sha256", ""))
        or not RUN_ID.fullmatch(value.get("failed_run_id", ""))
        or value.get("failed_run_id") != failed_run
        or value.get("reason") != reason
        or reason not in REASONS
        or not re.fullmatch(r"[0-9a-f]{32}", value.get("nonce", ""))
        or not isinstance(value.get("operator_id"), str)
        or not value["operator_id"]
    ):
        raise ApprovalError("fallback approval identity does not match")
    if approval_hash is not None and value["approval_hash"] != approval_hash:
        raise ApprovalError("fallback approval hash does not match preview")
    timestamp(value.get("observed_at"), "approval observation")
    expires = timestamp(value.get("expires_at"), "approval expiry")
    current = now or dt.datetime.now(dt.timezone.utc)
    if current > expires:
        raise ApprovalError("fallback approval is stale or expired")
    if not TICKET.fullmatch(ticket):
        raise ApprovalError("ticket identifier is invalid")
    return value


def approval_from_map(mapping, ticket, failed_run, reason, approval_hash=None, now=None):
    entry = mapping["tickets"].get(ticket)
    if not isinstance(entry, dict):
        raise ApprovalError("ticket is absent from operator map")
    approval = validate(
        entry.get("model_fallback_approval"),
        ticket,
        failed_run,
        reason,
        approval_hash,
        now,
    )
    consumed = entry.get("consumed_model_fallback_receipt_ids", [])
    if not isinstance(consumed, list) or any(not isinstance(item, str) for item in consumed):
        raise ApprovalError("consumed approval history is invalid")
    if approval["receipt_sha256"] in consumed:
        raise ApprovalError("fallback approval was already consumed")
    return approval


def atomic_write(path, value, mode):
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "w") as output:
            json.dump(value, output, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def consume(mapping, path, mode, ticket, approval, state_dir):
    try:
        operator_receipt.verify_consume(
            state_dir, ticket, "fallback",
            {"preview_sha256": approval["approval_hash"]},
        )
    except operator_receipt.OperatorReceiptError as exc:
        raise ApprovalError(f"authoritative fallback receipt was not consumed: {exc}") from exc
    entry = mapping["tickets"][ticket]
    consumed = list(entry.get("consumed_model_fallback_receipt_ids", []))
    consumed.append(approval["receipt_sha256"])
    entry["consumed_model_fallback_receipt_ids"] = consumed
    entry.pop("model_fallback_approval", None)
    atomic_write(path, mapping, mode)


def verify_consumed(mapping, ticket, receipt_sha256, approval_hash):
    if (
        not TICKET.fullmatch(ticket)
        or not SHA256.fullmatch(receipt_sha256 or "")
        or not SHA256.fullmatch(approval_hash or "")
    ):
        raise ApprovalError("consumed approval identity is invalid")
    entry = mapping["tickets"].get(ticket)
    if not isinstance(entry, dict):
        raise ApprovalError("ticket is absent from operator map")
    consumed = entry.get("consumed_model_fallback_receipt_ids", [])
    if (
        not isinstance(consumed, list)
        or any(not isinstance(item, str) for item in consumed)
        or receipt_sha256 not in consumed
    ):
        raise ApprovalError("committed fallback approval is not recorded as consumed")
    return {
        "approval_hash": approval_hash,
        "receipt_sha256": receipt_sha256,
        "schema": "model-fallback-consumed-approval/v1",
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("read", "consume", "verify-consumed"))
    parser.add_argument("--operator-map", required=True)
    parser.add_argument("--ticket", required=True)
    parser.add_argument("--failed-run", required=True)
    parser.add_argument("--reason", required=True, choices=sorted(REASONS))
    parser.add_argument("--approval-hash")
    parser.add_argument("--receipt-sha256")
    parser.add_argument("--state-dir")
    args = parser.parse_args(argv)
    path = Path(args.operator_map)
    mapping, mode = load_map(path)
    if args.action == "verify-consumed":
        print(json.dumps(
            verify_consumed(
                mapping, args.ticket, args.receipt_sha256, args.approval_hash
            ),
            sort_keys=True,
            separators=(",", ":"),
        ))
        return
    approval = approval_from_map(
        mapping, args.ticket, args.failed_run, args.reason, args.approval_hash
    )
    if args.action == "consume":
        if args.approval_hash is None:
            parser.error("consume requires --approval-hash")
        if not args.state_dir:
            parser.error("consume requires --state-dir")
        consume(mapping, path, mode, args.ticket, approval, Path(args.state_dir))
    print(json.dumps({
        "approval_hash": approval["approval_hash"],
        "failed_run_id": approval["failed_run_id"],
        "nonce": approval["nonce"],
        "operator_id": approval["operator_id"],
        "reason": approval["reason"],
        "receipt_sha256": approval["receipt_sha256"],
        "schema": approval["schema"],
    }, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    try:
        main()
    except ApprovalError as error:
        raise SystemExit(f"model-fallback-approval: {error}")
