#!/usr/bin/env python3
"""Export and validate lane-neutral Contract 1.8 ticket passports."""

from __future__ import annotations

import argparse
from decimal import Decimal, InvalidOperation
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


SCHEMA = "nysa.software-factory.ticket-passport/v1"
RECEIPT_SCHEMA = "nysa.software-factory.transition-receipt/v1"
TICKET = re.compile(r"^T-[0-9]+$")
SHA = re.compile(r"^[0-9a-f]{40}$")
DIGEST = re.compile(r"^[0-9a-f]{64}$")
TERMINAL_ACCOUNTING = {
    "completed", "abandoned_conservative", "cancelled", "cancelled_conservative",
}


class PassportError(ValueError):
    pass


def canonical(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode()


def git(root: Path, *arguments: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *arguments],
        text=True,
        capture_output=True,
        check=False,
        timeout=120,
    )
    if check and result.returncode:
        raise PassportError(result.stderr.strip() or "Git operation failed")
    return result.stdout.strip()


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
        raise PassportError("passport directory is unsafe")
    return path


def read_regular(path: Path, mode: int | None = None, maximum: int = 5_000_000) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
            or (mode is not None and stat.S_IMODE(info.st_mode) != mode)
            or info.st_size > maximum
        ):
            raise PassportError(f"unsafe file: {path.name}")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            return stream.read()
    finally:
        if descriptor >= 0:
            os.close(descriptor)


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


def key(state_dir: Path) -> bytes:
    path = state_dir / "passport.key"
    if not path.exists() and not path.is_symlink():
        try:
            descriptor = os.open(
                path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
        except FileExistsError:
            pass
        else:
            try:
                os.write(descriptor, secrets.token_bytes(32))
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    raw = read_regular(path, 0o600, 32)
    if len(raw) != 32:
        raise PassportError("passport authentication key is invalid")
    return raw


def authenticate(value: dict[str, Any], secret: bytes) -> dict[str, Any]:
    body = dict(value)
    body["authentication_sha256"] = hmac.new(
        secret, canonical(value), hashlib.sha256
    ).hexdigest()
    body["passport_sha256"] = hashlib.sha256(canonical(body)).hexdigest()
    return body


def load_passport(path: Path, secret: bytes) -> tuple[dict[str, Any], str]:
    raw = read_regular(path, 0o600)
    value = json.loads(raw)
    if not isinstance(value, dict) or value.get("schema") != SCHEMA:
        raise PassportError("passport is malformed")
    passport_digest = value.pop("passport_sha256", "")
    if passport_digest != hashlib.sha256(canonical(value)).hexdigest():
        raise PassportError("passport digest is invalid")
    authentication = value.pop("authentication_sha256", "")
    if not hmac.compare_digest(
        authentication, hmac.new(secret, canonical(value), hashlib.sha256).hexdigest()
    ):
        raise PassportError("passport authentication is invalid")
    value["authentication_sha256"] = authentication
    value["passport_sha256"] = passport_digest
    return value, hashlib.sha256(raw).hexdigest()


def manifest_fields(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in read_regular(path).decode("utf-8").splitlines():
        key_name, separator, value = line.partition("=")
        if not separator or not key_name or key_name in values:
            raise PassportError(f"run manifest is malformed: {path.name}")
        values[key_name] = value
    return values


def micro_usd(value: dict[str, str]) -> int:
    raw = value.get("effective_cost") or value.get("reserved_usd") or "0"
    try:
        amount = Decimal(raw)
    except InvalidOperation as error:
        raise PassportError("run charge is invalid") from error
    if amount < 0 or amount.as_tuple().exponent < -6:
        raise PassportError("run charge is invalid")
    return int(amount * 1_000_000)


def run_evidence(factory: Path, ticket: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    completed, charges = [], []
    runs = factory / "runs"
    if not runs.exists():
        return completed, charges
    for path in sorted(runs.glob("*.meta")):
        value = manifest_fields(path)
        if value.get("ticket") != ticket or value.get("accounting_state") not in TERMINAL_ACCOUNTING:
            continue
        run_id = value.get("run_id", "")
        if not run_id or any(item["run_id"] == run_id for item in charges):
            raise PassportError("run evidence identity is ambiguous")
        manifest_digest = hashlib.sha256(read_regular(path)).hexdigest()
        charges.append({
            "accounting_state": value["accounting_state"],
            "charge_micro_usd": micro_usd(value),
            "contract_version": value.get("contract_version"),
            "factory_sha": value.get("kit_sha"),
            "head_before": value.get("role_head_before"),
            "manifest_sha256": manifest_digest,
            "role": value.get("role"),
            "run_id": run_id,
            "transition_receipt_sha256": value.get(
                "transition_receipt_sha256"
            ),
        })
        if value.get("exit_status") == "0" and value.get("role_exit") == "ok":
            output = path.with_suffix(".out")
            output_digest = hashlib.sha256(read_regular(output)).hexdigest()
            if value.get("output_sha256") != output_digest:
                raise PassportError("successful role output digest does not match")
            completed.append({
                "contract_version": value.get("contract_version"),
                "factory_sha": value.get("kit_sha"),
                "head_before": value.get("role_head_before"),
                "manifest_sha256": manifest_digest,
                "output_sha256": output_digest,
                "role": value.get("role"),
                "run_id": run_id,
                "transition_receipt_sha256": value.get(
                    "transition_receipt_sha256"
                ),
            })
    return completed, charges


def ticket_state(workdir: Path, ticket: str) -> str:
    text = (workdir / "factory" / "tickets" / f"{ticket}.md").read_text(
        encoding="utf-8"
    )
    values = re.findall(r"^State:\s*(.*?)\s*$", text, re.I | re.M)
    if len(values) != 1:
        raise PassportError("ticket state is ambiguous")
    return values[0]


def route_digest(workdir: Path, ticket: str) -> str | None:
    path = workdir / "factory" / "route-plans" / f"{ticket}.json"
    return hashlib.sha256(read_regular(path)).hexdigest() if path.exists() else None


def receipt(state_dir: Path, ticket: str, expected: str) -> dict[str, Any]:
    value = json.loads(read_regular(state_dir / f"{ticket}.json", 0o600))
    immutable = {
        name: item for name, item in value.items()
        if name not in {"consumed", "consumed_at_epoch", "receipt_sha256"}
    }
    if (
        value.get("schema") != RECEIPT_SCHEMA
        or value.get("receipt_sha256") != expected
        or expected != hashlib.sha256(canonical(immutable)).hexdigest()
        or value.get("consumed") is not True
    ):
        raise PassportError("consumed transition receipt is invalid")
    return value


def identity(args: argparse.Namespace) -> dict[str, Any]:
    workdir = args.workdir.resolve(strict=True)
    origin = os.environ.get("FACTORY_CERTIFIED_PRODUCT_ORIGIN", "")
    if not origin or any(character in origin for character in "\n\r\t"):
        raise PassportError("certified product origin is unavailable")
    return {
        "branch": git(workdir, "symbolic-ref", "--quiet", "--short", "HEAD"),
        "head_sha": git(workdir, "rev-parse", "HEAD"),
        "head_tree": git(workdir, "rev-parse", "HEAD^{tree}"),
        "product_origin_sha256": hashlib.sha256(origin.encode()).hexdigest(),
        "project": args.project,
        "ticket": args.ticket,
        "ticket_blob": git(
            workdir, "rev-parse", f"HEAD:factory/tickets/{args.ticket}.md"
        ),
    }


def merge_records(
    prior: list[dict[str, Any]], current: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    records = {item["run_id"]: item for item in prior}
    if len(records) != len(prior):
        raise PassportError("prior passport run evidence is ambiguous")
    for item in current:
        existing = records.get(item["run_id"])
        if existing is not None and existing != item:
            raise PassportError("run evidence changed across passport export")
        records[item["run_id"]] = item
    return list(records.values())


def export(args: argparse.Namespace, secret: bytes) -> dict[str, Any]:
    passports = safe_directory(args.state_dir / "passports", create=True)
    destination = passports / f"{args.ticket}.json"
    previous: dict[str, Any] = {}
    parent_raw = None
    if destination.exists() or destination.is_symlink():
        previous, parent_raw = load_passport(destination, secret)
    consumed = receipt(args.state_dir, args.ticket, args.receipt)
    current_identity = identity(args)
    old_head = consumed.get("head_sha", "")
    bound_passport = consumed.get("passport_sha256")
    if (
        consumed.get("ticket") != args.ticket
        or consumed.get("project") != args.project
        or consumed.get("factory_sha") != args.factory_sha
        or consumed.get("contract_version") != args.contract_version
        or consumed.get("branch") != current_identity["branch"]
        or not SHA.fullmatch(old_head)
        or subprocess.run(
            ["git", "-C", str(args.workdir), "merge-base", "--is-ancestor",
             old_head, current_identity["head_sha"]],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        ).returncode != 0
        or bound_passport != parent_raw
    ):
        raise PassportError("transition receipt is outside current ticket lineage")
    completed, charges = run_evidence(args.factory_root / "factory", args.ticket)
    completed = merge_records(previous.get("completed_role_evidence", []), completed)
    charges = merge_records(previous.get("charge_records", []), charges)
    matching = [
        item for item in charges
        if item.get("transition_receipt_sha256") == args.receipt
        and item.get("role") == consumed.get("role")
        and item.get("head_before") == old_head
    ]
    if consumed.get("role") and len(matching) != 1:
        raise PassportError("receipt-bound terminal role evidence is missing")
    protected = git(args.factory_root, "rev-parse", "origin/main")
    history = list(previous.get("factory_release_history", []))
    release = {
        "contract_version": args.contract_version,
        "factory_sha": args.factory_sha,
    }
    if release not in history:
        history.append(release)
    value = {
        **current_identity,
        "base_history": [
            *previous.get("base_history", []),
            *([] if protected in previous.get("base_history", []) else [protected]),
        ],
        "charge_records": charges,
        "completed_role_evidence": completed,
        "contract_version": args.contract_version,
        "cumulative_charges_micro_usd": sum(
            item["charge_micro_usd"] for item in charges
        ),
        "current_state": ticket_state(args.workdir, args.ticket),
        "current_stage": consumed["stage"],
        "factory_release_history": history,
        "factory_sha": args.factory_sha,
        "nonce": secrets.token_hex(16),
        "parent_digest": previous.get("passport_sha256"),
        "parent_file_sha256": parent_raw,
        "protected_base_sha": protected,
        "publication_state": (
            previous.get("publication_state", "none")
            if args.publication_state == "preserve"
            else args.publication_state
        ),
        "route_plan_sha256": route_digest(args.workdir, args.ticket),
        "schema": SCHEMA,
        "transition_receipt_sha256": args.receipt,
    }
    signed = authenticate(value, secret)
    write_atomic(destination, signed)
    return signed


def validate(args: argparse.Namespace, secret: bytes) -> dict[str, Any]:
    path = args.state_dir / "passports" / f"{args.ticket}.json"
    value, _ = load_passport(path, secret)
    current = identity(args)
    for name, item in current.items():
        if value.get(name) != item:
            raise PassportError("passport does not match this clean execution cell")
    if git(args.workdir, "status", "--porcelain=v1", "-z"):
        raise PassportError("passport validation requires a clean execution cell")
    if value.get("route_plan_sha256") != route_digest(args.workdir, args.ticket):
        raise PassportError("passport route plan changed")
    return value


def migrate(args: argparse.Namespace, secret: bytes) -> dict[str, Any]:
    passports = safe_directory(args.state_dir / "passports")
    destination = passports / f"{args.ticket}.json"
    previous, parent_raw = load_passport(destination, secret)
    current = identity(args)
    protected = git(args.factory_root, "rev-parse", "origin/main")
    publication = (
        previous.get("publication_state", "none")
        if args.publication_state == "preserve"
        else args.publication_state
    )
    current_ticket_state = ticket_state(args.workdir, args.ticket)
    current_route = route_digest(args.workdir, args.ticket)
    if (
        previous.get("ticket") != args.ticket
        or previous.get("project") != args.project
        or previous.get("branch") != current["branch"]
        or previous.get("product_origin_sha256") != current["product_origin_sha256"]
        or subprocess.run(
            ["git", "-C", str(args.workdir), "merge-base", "--is-ancestor",
             previous.get("head_sha", ""), current["head_sha"]],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        ).returncode != 0
        or subprocess.run(
            ["git", "-C", str(args.factory_root), "merge-base", "--is-ancestor",
             previous.get("protected_base_sha", ""), protected],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        ).returncode != 0
    ):
        raise PassportError("passport migration is outside authenticated lineage")
    if (
        all(previous.get(name) == item for name, item in current.items())
        and previous.get("protected_base_sha") == protected
        and previous.get("factory_sha") == args.factory_sha
        and previous.get("contract_version") == args.contract_version
        and previous.get("current_state") == current_ticket_state
        and previous.get("publication_state") == publication
        and previous.get("route_plan_sha256") == current_route
    ):
        return previous
    history = list(previous["factory_release_history"])
    release = {
        "contract_version": args.contract_version,
        "factory_sha": args.factory_sha,
    }
    if release not in history:
        history.append(release)
    value = {
        **{
            name: item for name, item in previous.items()
            if name not in {
                "authentication_sha256", "passport_sha256", "parent_digest",
                "parent_file_sha256", "nonce",
            }
        },
        **current,
        "base_history": [
            *previous.get("base_history", []),
            *([] if protected in previous.get("base_history", []) else [protected]),
        ],
        "contract_version": args.contract_version,
        "current_state": current_ticket_state,
        "factory_release_history": history,
        "factory_sha": args.factory_sha,
        "migration_history": [
            *previous.get("migration_history", []),
            {
                "from_factory_sha": previous["factory_sha"],
                "from_head_sha": previous["head_sha"],
                "from_protected_base_sha": previous["protected_base_sha"],
                "to_factory_sha": args.factory_sha,
                "to_head_sha": current["head_sha"],
                "to_protected_base_sha": protected,
            },
        ],
        "nonce": secrets.token_hex(16),
        "parent_digest": previous["passport_sha256"],
        "parent_file_sha256": parent_raw,
        "protected_base_sha": protected,
        "publication_state": publication,
        "route_plan_sha256": current_route,
        "schema": SCHEMA,
    }
    signed = authenticate(value, secret)
    write_atomic(destination, signed)
    return signed


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("export", "migrate", "validate"))
    parser.add_argument("--factory-root", required=True, type=Path)
    parser.add_argument("--workdir", required=True, type=Path)
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--ticket", required=True)
    parser.add_argument("--contract-version", required=True)
    parser.add_argument("--factory-sha", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--receipt", default="")
    parser.add_argument(
        "--publication-state",
        choices=(
            "preserve", "none", "validating", "ready", "merge-pending",
            "merged", "repair",
        ),
        default="preserve",
    )
    args = parser.parse_args()
    try:
        if (
            not TICKET.fullmatch(args.ticket)
            or args.contract_version != "1.8.0"
            or not SHA.fullmatch(args.factory_sha)
            or (args.action == "export" and not DIGEST.fullmatch(args.receipt))
        ):
            raise PassportError("invalid passport arguments")
        args.factory_root = args.factory_root.resolve(strict=True)
        args.workdir = args.workdir.resolve(strict=True)
        args.state_dir = safe_directory(args.state_dir)
        secret = key(args.state_dir)
        if args.action == "export":
            value = export(args, secret)
        elif args.action == "migrate":
            value = migrate(args, secret)
        else:
            value = validate(args, secret)
        print(json.dumps({
            "passport": value["passport_sha256"],
            "schema": SCHEMA,
            "status": "ok",
            "ticket": args.ticket,
        }, sort_keys=True))
    except (
        FileNotFoundError,
        json.JSONDecodeError,
        OSError,
        PassportError,
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
