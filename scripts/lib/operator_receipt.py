#!/usr/bin/env python3
"""One-use operator receipts: the six operator authorities without Linear.

Authoritative records live in the controller state directory (0700, uid-owned,
0600 files, sha256 digest, nonce, consume-under-flock) — the same discipline as
transition receipts in state-machine.py. Any copy of a receipt committed to a
git repository is an audit artifact and carries no authority.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import tempfile
import time
from typing import Any

SCHEMA = "nysa.software-factory.operator-receipt/v1"
ACTIONS = ("ready", "approve", "resume", "cancel", "priority", "fallback")
TICKET = re.compile(r"^T-[0-9]+$")
DIGEST = re.compile(r"^[0-9a-f]{64}$")
# Binding keys each action must carry in its payload. The verifier re-checks
# every binding value at consumption time, so a receipt issued against one
# artifact can never authorize another.
REQUIRED_PAYLOAD = {
    "ready": (),
    "approve": ("bundle_attestation_blob",),
    "resume": ("resume_stage",),
    "cancel": (),
    "priority": ("priority",),
    "fallback": ("preview_sha256",),
}


class OperatorReceiptError(ValueError):
    pass


def canonical(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode()


def safe_state_dir(path: Path) -> Path:
    if not path.is_absolute():
        raise OperatorReceiptError("operator receipt state directory is unsafe")
    path.mkdir(mode=0o700, parents=False, exist_ok=True)
    info = path.lstat()
    if (
        path.resolve(strict=True) != path
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise OperatorReceiptError("operator receipt state directory is unsafe")
    return path


def receipt_root(state_dir: Path) -> Path:
    root = safe_state_dir(state_dir) / "operator-receipts"
    root.mkdir(mode=0o700, exist_ok=True)
    return safe_state_dir(root)


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
        raise OperatorReceiptError("operator receipt is unsafe")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema") != SCHEMA:
        raise OperatorReceiptError("operator receipt is malformed")
    immutable = {
        key: item for key, item in value.items()
        if key not in {"consumed", "consumed_at_epoch", "receipt_sha256"}
    }
    if value.get("receipt_sha256") != hashlib.sha256(
        canonical(immutable)
    ).hexdigest():
        raise OperatorReceiptError("operator receipt digest is invalid")
    if not isinstance(value.get("consumed"), bool):
        raise OperatorReceiptError("operator receipt consumption state is invalid")
    return value


def write_atomic(path: Path, value: dict[str, Any]) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".receipt.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as output:
            json.dump(value, output, ensure_ascii=True, sort_keys=True, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _locked(state_dir: Path):
    lock_path = safe_state_dir(state_dir) / ".operator-lock"
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    stream = os.fdopen(descriptor, "r+")
    fcntl.flock(stream, fcntl.LOCK_EX)
    return stream


def _validate_identity(ticket: str, action: str) -> None:
    if not TICKET.fullmatch(ticket):
        raise OperatorReceiptError("operator receipt ticket is invalid")
    if action not in ACTIONS:
        raise OperatorReceiptError("operator receipt action is unknown")


def issue(
    state_dir: Path, ticket: str, action: str, payload: dict[str, Any],
) -> dict[str, Any]:
    _validate_identity(ticket, action)
    if not isinstance(payload, dict) or any(
        not isinstance(key, str) for key in payload
    ):
        raise OperatorReceiptError("operator receipt payload is invalid")
    for key in REQUIRED_PAYLOAD[action]:
        if not payload.get(key):
            raise OperatorReceiptError(
                f"operator {action} receipt requires payload key: {key}"
            )
    with _locked(state_dir):
        ticket_dir = receipt_root(state_dir) / ticket
        ticket_dir.mkdir(mode=0o700, exist_ok=True)
        existing = sorted(ticket_dir.glob(f"{action}-*.json"))
        for path in existing:
            prior = safe_receipt(path)
            if not prior["consumed"] and prior.get("payload") == payload:
                return prior
        sequence = len(existing) + 1
        value: dict[str, Any] = {
            "action": action,
            "issued_at": datetime.now(timezone.utc).isoformat(
                timespec="seconds"
            ).replace("+00:00", "Z"),
            "nonce": secrets.token_hex(16),
            "payload": payload,
            "schema": SCHEMA,
            "sequence": sequence,
            "ticket": ticket,
        }
        value["receipt_sha256"] = hashlib.sha256(canonical(value)).hexdigest()
        value["consumed"] = False
        write_atomic(ticket_dir / f"{action}-{sequence}.json", value)
        return value


def _latest_open(
    state_dir: Path, ticket: str, action: str,
) -> tuple[Path, dict[str, Any]] | None:
    ticket_dir = receipt_root(state_dir) / ticket
    if not ticket_dir.is_dir():
        return None
    candidates = []
    for path in sorted(ticket_dir.glob(f"{action}-*.json")):
        value = safe_receipt(path)
        if not value["consumed"]:
            candidates.append((path, value))
    return candidates[-1] if candidates else None


def _exact(
    state_dir: Path, ticket: str, action: str, receipt_sha256: str,
) -> tuple[Path, dict[str, Any]] | None:
    if not DIGEST.fullmatch(receipt_sha256):
        raise OperatorReceiptError("operator receipt digest is invalid")
    ticket_dir = receipt_root(state_dir) / ticket
    if not ticket_dir.is_dir():
        return None
    matches = []
    for path in sorted(ticket_dir.glob(f"{action}-*.json")):
        value = safe_receipt(path)
        if value["receipt_sha256"] == receipt_sha256:
            matches.append((path, value))
    if len(matches) > 1:
        raise OperatorReceiptError("operator receipt digest is ambiguous")
    return matches[0] if matches else None


def peek(
    state_dir: Path, ticket: str, action: str, binding: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Return the newest unconsumed matching receipt without consuming it."""
    _validate_identity(ticket, action)
    with _locked(state_dir):
        found = _latest_open(state_dir, ticket, action)
        if not found:
            return None
        _, value = found
        for key, expected in (binding or {}).items():
            if value["payload"].get(key) != expected:
                return None
        return value


def peek_exact(
    state_dir: Path, ticket: str, action: str, receipt_sha256: str,
    binding: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Return one exact unconsumed receipt bound to a map projection."""
    _validate_identity(ticket, action)
    with _locked(state_dir):
        found = _exact(state_dir, ticket, action, receipt_sha256)
        if not found:
            return None
        _, value = found
        if value["consumed"] or any(
            value["payload"].get(key) != expected
            for key, expected in (binding or {}).items()
        ):
            return None
        return value


def read_exact(
    state_dir: Path, ticket: str, action: str, receipt_sha256: str,
    binding: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Return one exact receipt, including a consumed crash-recovery record."""
    _validate_identity(ticket, action)
    with _locked(state_dir):
        found = _exact(state_dir, ticket, action, receipt_sha256)
        if not found:
            return None
        _, value = found
        if any(
            value["payload"].get(key) != expected
            for key, expected in (binding or {}).items()
        ):
            return None
        return value


def verify_consume(
    state_dir: Path, ticket: str, action: str, binding: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Consume the newest unconsumed receipt for (ticket, action).

    Every key in ``binding`` must match the receipt payload exactly;
    fail-closed on any mismatch, absence, or unreadable state.
    """
    _validate_identity(ticket, action)
    with _locked(state_dir):
        found = _latest_open(state_dir, ticket, action)
        if not found:
            raise OperatorReceiptError(
                f"no unconsumed operator {action} receipt for {ticket}"
            )
        path, value = found
        for key, expected in (binding or {}).items():
            if value["payload"].get(key) != expected:
                raise OperatorReceiptError(
                    f"operator {action} receipt binding mismatch: {key}"
                )
        value["consumed"] = True
        value["consumed_at_epoch"] = int(time.time())
        write_atomic(path, value)
        return value


def verify_consume_exact(
    state_dir: Path, ticket: str, action: str, receipt_sha256: str,
    binding: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Consume the exact receipt named by an operator-map projection."""
    _validate_identity(ticket, action)
    with _locked(state_dir):
        found = _exact(state_dir, ticket, action, receipt_sha256)
        if not found:
            raise OperatorReceiptError(
                f"operator {action} receipt is unavailable for {ticket}"
            )
        path, value = found
        if value["consumed"]:
            raise OperatorReceiptError("operator receipt was already consumed")
        for key, expected in (binding or {}).items():
            if value["payload"].get(key) != expected:
                raise OperatorReceiptError(
                    f"operator {action} receipt binding mismatch: {key}"
                )
        value["consumed"] = True
        value["consumed_at_epoch"] = int(time.time())
        write_atomic(path, value)
        return value


def pending(state_dir: Path) -> list[dict[str, Any]]:
    with _locked(state_dir):
        root = receipt_root(state_dir)
        values = []
        for ticket_dir in sorted(root.iterdir()):
            if not ticket_dir.is_dir() or not TICKET.fullmatch(ticket_dir.name):
                continue
            for path in sorted(ticket_dir.glob("*.json")):
                value = safe_receipt(path)
                if not value["consumed"]:
                    values.append(value)
        return values


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", required=True, type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("issue", "peek", "consume"):
        sub = commands.add_parser(name)
        sub.add_argument("--ticket", required=True)
        sub.add_argument("--action", required=True, choices=ACTIONS)
        sub.add_argument(
            "--payload", default="{}",
            help="JSON object of binding fields" if name == "issue"
            else "JSON object of binding fields that must match",
        )
    commands.add_parser("pending")
    args = parser.parse_args(argv)
    try:
        if args.command == "pending":
            result: Any = pending(args.state_dir)
        else:
            payload = json.loads(args.payload)
            if args.command == "issue":
                result = issue(args.state_dir, args.ticket, args.action, payload)
            elif args.command == "peek":
                result = peek(args.state_dir, args.ticket, args.action, payload)
            else:
                result = verify_consume(
                    args.state_dir, args.ticket, args.action, payload
                )
    except (OperatorReceiptError, json.JSONDecodeError, OSError) as error:
        print(f"REFUSE {error}", file=sys.stderr)
        return 1
    json.dump(result, sys.stdout, ensure_ascii=True, sort_keys=True, indent=2)
    print()
    return 0


if __name__ == "__main__":
    sys.dont_write_bytecode = True
    raise SystemExit(main())
