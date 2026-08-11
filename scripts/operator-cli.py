#!/usr/bin/env python3
"""Operator authority CLI: issue receipts and project the operator map.

Replaces Linear ingestion. Each verb issues a one-use authoritative receipt in
the controller state directory (see scripts/lib/operator_receipt.py), projects
the decision into the gitignored operator map that sequencing reads, and writes
a zero-authority audit copy under factory/receipts/ in the product checkout.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat
import subprocess
import sys
import tempfile

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
import operator_receipt  # noqa: E402
from operator_receipt import OperatorReceiptError  # noqa: E402

MAP_TOP_LEVEL = ("_config", "_sync", "initiatives", "tickets")
PRIORITIES = ("none", "urgent", "high", "normal", "low")
STATES = (
    "Backlog", "Ready", "Planning", "Building", "Review",
    "Awaiting Approval", "Approved", "Blocked-Escalated", "Done", "Canceled",
)
FALLBACK_REASONS = (
    "budget_exhausted", "credits_exhausted", "operator_requested",
    "provider_unavailable",
)
FALLBACK_SCHEMA = "model-fallback-receipt-approval/v1"


class OperatorCliError(ValueError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")


def operator_map_path(product: Path) -> Path:
    path = Path(os.environ.get(
        "FACTORY_OPERATOR_MAP", product / "factory" / "operator-map.json"
    ))
    if not path.is_absolute():
        raise OperatorCliError("operator map path is invalid")
    return path


def load_map(path: Path) -> dict:
    if path.is_symlink():
        raise OperatorCliError("operator map is unsafe")
    if not path.is_file():
        return {
            "_config": None, "_sync": {}, "initiatives": {}, "tickets": {},
        }
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or any(
        not isinstance(value.get(key), dict)
        for key in MAP_TOP_LEVEL if key != "_config"
    ):
        raise OperatorCliError("operator map is malformed")
    return value


def write_map(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".operator-map.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as output:
            json.dump(value, output, indent=2, sort_keys=True)
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


def ticket_entry(mapping: dict, ticket: str) -> dict:
    tickets = mapping.setdefault("tickets", {})
    entry = tickets.setdefault(ticket, {})
    entry["operator_fields_initialized"] = True
    return entry


def committed_state(product: Path, ticket: str) -> str:
    ticket_path = product / "factory" / "tickets" / f"{ticket}.md"
    if not ticket_path.is_file() or ticket_path.is_symlink():
        raise OperatorCliError(f"ticket file is missing: {ticket}")
    for line in ticket_path.read_text(encoding="utf-8").splitlines():
        key, _, rest = line.partition(":")
        if key.strip().lower() == "state":
            return rest.strip().lower()
    raise OperatorCliError(f"ticket has no State field: {ticket}")


def bundle_attestation_blob(product: Path, ticket: str) -> str:
    path = product / "factory" / "attestations" / ticket / "bundle.json"
    if not path.is_file() or path.is_symlink():
        raise OperatorCliError(
            f"bundle attestation is missing for {ticket}; approve after attestation"
        )
    result = subprocess.run(
        ["git", "-C", str(product), "hash-object", str(path)],
        text=True, capture_output=True, check=False,
    )
    if result.returncode:
        raise OperatorCliError(result.stderr.strip() or "git hash-object failed")
    return result.stdout.strip()


def audit_copy(product: Path, receipt: dict) -> Path:
    directory = product / "factory" / "receipts" / receipt["ticket"]
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{receipt['action']}-{receipt['sequence']}.json"
    # Zero authority: verification reads only the controller state directory.
    value = {key: item for key, item in receipt.items() if key != "nonce"}
    value["audit"] = "no-authority"
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return path


def commit_audit(product: Path, path: Path, message: str) -> None:
    if os.environ.get("FACTORY_OPERATOR_AUDIT_COMMIT", "1") != "1":
        return
    inside = subprocess.run(
        ["git", "-C", str(product), "rev-parse", "--is-inside-work-tree"],
        text=True, capture_output=True, check=False,
    )
    if inside.returncode or inside.stdout.strip() != "true":
        return
    relative = path.relative_to(product)
    add = subprocess.run(
        ["git", "-C", str(product), "add", "--", str(relative)],
        text=True, capture_output=True, check=False,
    )
    if add.returncode:
        raise OperatorCliError(add.stderr.strip() or "git add failed")
    commit = subprocess.run(
        [
            "git", "-C", str(product), "-c", "user.name=Factory Operator",
            "-c", "user.email=operator@local", "commit", "--quiet",
            "-m", message, "--", str(relative),
        ],
        text=True, capture_output=True, check=False,
    )
    if commit.returncode and "nothing to commit" not in (
        commit.stdout + commit.stderr
    ):
        raise OperatorCliError(commit.stderr.strip() or "git commit failed")


def stamp_sync(mapping: dict, ticket: str | None) -> None:
    sync = mapping.setdefault("_sync", {})
    now = utc_now()
    sync["last_success_at"] = now
    sync.pop("last_error", None)
    sync.pop("_last_error", None)
    if ticket:
        sync.setdefault("selected_ticket_success_at", {})[ticket] = now


def project(entry: dict, operator: dict) -> None:
    existing = entry.get("operator")
    merged = dict(existing) if isinstance(existing, dict) else {}
    merged.update(operator)
    entry["operator"] = merged


def cmd_ticket_action(args: argparse.Namespace) -> dict:
    product = Path(args.product).resolve()
    state_dir = Path(args.state_dir)
    map_path = operator_map_path(product)
    mapping = load_map(map_path)
    entry = ticket_entry(mapping, args.ticket)
    current = committed_state(product, args.ticket)
    now = utc_now()
    action = args.command
    payload: dict = {}
    operator: dict = {"observed_at": now}
    if action == "ready":
        if current != "backlog":
            raise OperatorCliError(
                f"ready requires a Backlog ticket; {args.ticket} is {current}"
            )
        operator.update({"state": "Ready", "state_base": "backlog"})
    elif action == "cancel":
        if current != "backlog":
            raise OperatorCliError(
                f"cancel requires a Backlog ticket; {args.ticket} is {current}"
            )
        operator.update({"state": "Canceled", "state_base": "backlog"})
    elif action == "approve":
        if current != "awaiting approval":
            raise OperatorCliError(
                f"approve requires Awaiting Approval; {args.ticket} is {current}"
            )
        blob = bundle_attestation_blob(product, args.ticket)
        payload = {"bundle_attestation_blob": blob}
        operator.update({
            "state": "Approved",
            "state_base": "awaiting approval",
            "approval": "Receipt",
        })
    elif action == "resume":
        if current != "blocked-escalated":
            raise OperatorCliError(
                f"resume requires Blocked-Escalated; {args.ticket} is {current}"
            )
        if args.stage not in STATES:
            raise OperatorCliError(f"resume stage is invalid: {args.stage}")
        payload = {"resume_stage": args.stage}
        operator.update({
            "state": args.stage, "state_base": "blocked-escalated",
        })
    elif action == "priority":
        if args.priority not in PRIORITIES:
            raise OperatorCliError(f"priority is invalid: {args.priority}")
        payload = {"priority": args.priority}
        operator["priority"] = args.priority
    else:
        raise OperatorCliError(f"unknown action: {action}")
    receipt = operator_receipt.issue(state_dir, args.ticket, action, payload)
    if action == "approve":
        operator["receipt_sha256"] = receipt["receipt_sha256"]
        operator["observed_at"] = receipt["issued_at"]
    project(entry, operator)
    stamp_sync(mapping, args.ticket)
    write_map(map_path, mapping)
    path = audit_copy(product, receipt)
    commit_audit(
        product, path,
        f"{args.ticket}: operator {action} receipt {receipt['sequence']}",
    )
    return receipt


def cmd_fallback_approve(args: argparse.Namespace) -> dict:
    product = Path(args.product).resolve()
    state_dir = Path(args.state_dir)
    map_path = operator_map_path(product)
    mapping = load_map(map_path)
    entry = ticket_entry(mapping, args.ticket)
    if args.reason not in FALLBACK_REASONS:
        raise OperatorCliError(f"fallback reason is invalid: {args.reason}")
    if not operator_receipt.DIGEST.fullmatch(args.preview_hash or ""):
        raise OperatorCliError("fallback preview hash is invalid")
    receipt = operator_receipt.issue(
        state_dir, args.ticket, "fallback",
        {
            "preview_sha256": args.preview_hash,
            "failed_run_id": args.failed_run,
            "reason": args.reason,
        },
    )
    expires = (
        datetime.now(timezone.utc) + timedelta(minutes=args.expires_minutes)
    ).isoformat(timespec="seconds").replace("+00:00", "Z")
    entry["model_fallback_approval"] = {
        "approval_hash": args.preview_hash,
        "expires_at": expires,
        "failed_run_id": args.failed_run,
        "nonce": secrets.token_hex(16),
        "observed_at": receipt["issued_at"],
        "operator_id": args.operator_id or os.environ.get("USER", "operator"),
        "reason": args.reason,
        "receipt_sha256": receipt["receipt_sha256"],
        "schema": FALLBACK_SCHEMA,
    }
    stamp_sync(mapping, args.ticket)
    write_map(map_path, mapping)
    path = audit_copy(product, receipt)
    commit_audit(
        product, path,
        f"{args.ticket}: operator fallback receipt {receipt['sequence']}",
    )
    return receipt


def cmd_init(args: argparse.Namespace) -> dict:
    product = Path(args.product).resolve()
    map_path = operator_map_path(product)
    mapping = load_map(map_path)
    committed_state(product, args.ticket)
    ticket_entry(mapping, args.ticket)
    stamp_sync(mapping, args.ticket)
    write_map(map_path, mapping)
    return {"ticket": args.ticket, "initialized": True}


def cmd_pending(args: argparse.Namespace) -> dict:
    product = Path(args.product).resolve()
    tickets_dir = product / "factory" / "tickets"
    waiting = {"awaiting approval": [], "blocked-escalated": []}
    if tickets_dir.is_dir():
        for path in sorted(tickets_dir.glob("T-*.md")):
            if "-bundle" in path.stem:
                continue
            try:
                state = committed_state(product, path.stem)
            except OperatorCliError:
                continue
            if state in waiting:
                waiting[state].append(path.stem)
    open_receipts = operator_receipt.pending(Path(args.state_dir))
    return {
        "awaiting_approval": waiting["awaiting approval"],
        "blocked_escalated": waiting["blocked-escalated"],
        "open_receipts": [
            {key: value[key] for key in ("ticket", "action", "issued_at")}
            for value in open_receipts
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--product", required=True)
    parser.add_argument("--state-dir", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("ready", "approve", "cancel", "init"):
        sub = commands.add_parser(name)
        sub.add_argument("--ticket", required=True)
    resume = commands.add_parser("resume")
    resume.add_argument("--ticket", required=True)
    resume.add_argument("--stage", required=True)
    priority = commands.add_parser("priority")
    priority.add_argument("--ticket", required=True)
    priority.add_argument("--priority", required=True, choices=PRIORITIES)
    fallback = commands.add_parser("fallback-approve")
    fallback.add_argument("--ticket", required=True)
    fallback.add_argument("--preview-hash", required=True)
    fallback.add_argument("--failed-run", required=True)
    fallback.add_argument("--reason", required=True, choices=FALLBACK_REASONS)
    fallback.add_argument("--expires-minutes", type=int, default=60)
    fallback.add_argument("--operator-id")
    commands.add_parser("pending")
    args = parser.parse_args(argv)
    try:
        if args.command == "pending":
            result = cmd_pending(args)
        elif args.command == "init":
            result = cmd_init(args)
        elif args.command == "fallback-approve":
            result = cmd_fallback_approve(args)
        else:
            result = cmd_ticket_action(args)
    except (OperatorCliError, OperatorReceiptError, OSError,
            json.JSONDecodeError) as error:
        print(f"REFUSE {error}", file=sys.stderr)
        return 1
    json.dump(result, sys.stdout, ensure_ascii=True, sort_keys=True, indent=2)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
