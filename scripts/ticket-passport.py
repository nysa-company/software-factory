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
import sys
import tempfile
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
from role_output import RoleOutputError, sha256 as role_output_sha256


SCHEMA = "nysa.software-factory.ticket-passport/v1"
RECEIPT_SCHEMA = "nysa.software-factory.transition-receipt/v1"
TICKET = re.compile(r"^T-[0-9]+$")
SHA = re.compile(r"^[0-9a-f]{40}$")
DIGEST = re.compile(r"^[0-9a-f]{64}$")
TERMINAL_ACCOUNTING = {
    "completed", "abandoned_conservative", "cancelled", "cancelled_conservative",
}
INFLIGHT_SCHEMA = "nysa.software-factory.inflight-release-authorization/v1"
REWRITE_SCHEMA = "nysa.software-factory.ticket-rewrite-authorization/v1"
INFLIGHT_STATES = {
    "Ready", "Planning", "Building", "Review", "Awaiting Approval", "Approved",
}


class PassportError(ValueError):
    pass


def role_output_digest(path: Path) -> str:
    try:
        return role_output_sha256(path)
    except (OSError, RoleOutputError) as error:
        raise PassportError(str(error)) from None


def canonical(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode()


def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value = {}
    for name, item in pairs:
        if name in value:
            raise ValueError("duplicate key")
        value[name] = item
    return value


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


def _run_evidence(
    factory: Path, ticket: str, validate_outputs: bool
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
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
        if (
            validate_outputs
            and value.get("exit_status") == "0"
            and value.get("role_exit") == "ok"
        ):
            output = path.with_suffix(".out")
            output_digest = role_output_digest(output)
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


def run_charges(factory: Path, ticket: str) -> list[dict[str, Any]]:
    return _run_evidence(factory, ticket, False)[1]


def run_evidence(
    factory: Path, ticket: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    return _run_evidence(factory, ticket, True)


def ticket_state(workdir: Path, ticket: str) -> str:
    text = (workdir / "factory" / "tickets" / f"{ticket}.md").read_text(
        encoding="utf-8"
    )
    values = re.findall(r"^State:\s*(.*?)\s*$", text, re.I | re.M)
    if len(values) != 1:
        raise PassportError("ticket state is ambiguous")
    return values[0]


def authorized_inflight_rewrite(
    args: argparse.Namespace,
    previous: dict[str, Any],
    current: dict[str, Any],
    current_state: str,
    protected: str,
) -> bool:
    source = previous.get("factory_sha", "")
    target = args.factory_sha
    if (
        not SHA.fullmatch(source)
        or not SHA.fullmatch(target)
        or source == target
        or git(args.workdir, "status", "--porcelain=v1", "-z")
    ):
        return False
    relative = f"factory/migrations/inflight-release/{target}.json"
    raw = git(args.factory_root, "show", f"{protected}:{relative}", check=False)
    project = git(
        args.factory_root, "show", f"{protected}:factory/PROJECT.env", check=False
    )
    if not raw or len(raw.encode()) > 1_000_000 or not project:
        return False
    try:
        authorization = json.loads(raw, object_pairs_hook=unique_object)
    except (json.JSONDecodeError, ValueError):
        return False
    repositories = re.findall(
        r"^(?:export\s+)?GH_REPO\s*=\s*['\"]?([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)['\"]?\s*$",
        project,
        re.M,
    )
    if (
        not isinstance(authorization, dict)
        or set(authorization) != {
            "repository", "schema", "source_kit_sha", "target_kit_sha", "tickets",
        }
        or authorization.get("schema") != INFLIGHT_SCHEMA
        or repositories != [authorization.get("repository")]
        or authorization.get("source_kit_sha") != source
        or authorization.get("target_kit_sha") != target
        or not isinstance(authorization.get("tickets"), list)
        or not authorization["tickets"]
        or not DIGEST.fullmatch(previous.get("route_plan_sha256", ""))
        or previous["route_plan_sha256"] != route_digest(args.workdir, args.ticket)
    ):
        return False
    tickets, selected = [], None
    for item in authorization["tickets"]:
        if (
            not isinstance(item, dict)
            or set(item) != {"branch", "head", "state", "ticket"}
            or not TICKET.fullmatch(item.get("ticket", ""))
            or item.get("branch") != f"ticket/{item.get('ticket')}"
            or not SHA.fullmatch(item.get("head", ""))
            or item.get("state") not in INFLIGHT_STATES
        ):
            return False
        tickets.append(item["ticket"])
        if item["ticket"] == args.ticket:
            selected = item
    return (
        tickets == sorted(set(tickets))
        and selected == {
            "branch": current["branch"],
            "head": current["head_sha"],
            "state": current_state,
            "ticket": args.ticket,
        }
    )


def project_value(text: str, name: str) -> str | None:
    values = []
    assignment = re.compile(
        rf"(?:export[ \t]+)?{re.escape(name)}[ \t]*=[ \t]*(.*)"
    )
    for raw in text.splitlines():
        match = assignment.fullmatch(raw.strip())
        if not match:
            continue
        encoded = match.group(1)
        if encoded[:1] in {"'", '"'}:
            if (
                len(encoded) < 2
                or encoded[-1] != encoded[0]
                or encoded[0] in encoded[1:-1]
            ):
                return None
            encoded = encoded[1:-1]
        if any(fragment in encoded for fragment in ("`", "$(", "${", "\\", "\n", "\r")):
            return None
        values.append(encoded)
    return values[0] if len(values) == 1 else None


def rewrite_delta_allowed(
    workdir: Path, old_head: str, new_head: str, test_paths: str, ticket: str
) -> bool:
    roots = test_paths.split()
    if not roots or any(
        not re.fullmatch(r"[A-Za-z0-9._/-]+", root)
        or root.startswith("/")
        or ".." in root.split("/")
        or "//" in root
        for root in roots
    ):
        return False
    changed = git(
        workdir, "diff", "--name-status", "--no-renames",
        f"{old_head}^{{tree}}", f"{new_head}^{{tree}}",
    )
    seen_test = False
    for line in changed.splitlines():
        fields = line.split("\t")
        if len(fields) != 2 or fields[0] not in {"A", "M"}:
            return False
        status, path = fields
        if not re.fullmatch(r"[A-Za-z0-9._/-]+", path):
            return False
        is_test = any(
            path.startswith(root) if root.endswith("/") else path == root
            for root in roots
        )
        if path != f"factory/tickets/{ticket}.md" and not is_test:
            return False
        seen_test = seen_test or is_test
        current = git(workdir, "ls-tree", new_head, "--", path).split()
        if len(current) < 4 or current[:2] != ["100644", "blob"]:
            return False
        if status == "M":
            prior = git(workdir, "ls-tree", old_head, "--", path).split()
            if len(prior) < 4 or prior[:2] != ["100644", "blob"]:
                return False
    return seen_test


def failed_rewrite_manifest(
    args: argparse.Namespace, previous: dict[str, Any], receipt_digest: str
) -> bool:
    matches = []
    for path in sorted((args.factory_root / "factory/runs").glob("*.meta")):
        value = manifest_fields(path)
        if (
            value.get("ticket") == args.ticket
            and value.get("role") == "test-author"
            and value.get("transition_receipt_sha256") == receipt_digest
        ):
            matches.append((path, value))
    if len(matches) != 1:
        return False
    path, value = matches[0]
    output = path.with_suffix(".out")
    return (
        value.get("phase") == "completed"
        and value.get("accounting_state") == "abandoned_conservative"
        and value.get("task_submitted") == "1"
        and value.get("exit_status") == "11"
        and value.get("role_exit") == "role_exit_push_failed"
        and value.get("role_head_before") == previous.get("head_sha")
        and value.get("kit_sha") == args.factory_sha
        and value.get("contract_version") == args.contract_version
        and DIGEST.fullmatch(value.get("output_sha256", "")) is not None
        and output.is_file()
        and not output.is_symlink()
        and role_output_digest(output) == value["output_sha256"]
    )


def authorized_ticket_rewrite(
    args: argparse.Namespace,
    previous: dict[str, Any],
    current: dict[str, Any],
    current_state: str,
    protected: str,
) -> str | None:
    if (
        previous.get("factory_sha") != args.factory_sha
        or current_state != "Building"
        or git(args.workdir, "status", "--porcelain=v1", "-z")
    ):
        return None
    project = git(
        args.factory_root, "show", f"{protected}:factory/PROJECT.env", check=False
    )
    repository = project_value(project, "GH_REPO")
    test_paths = project_value(project, "TEST_PATHS")
    route = route_digest(args.workdir, args.ticket)
    if (
        not repository
        or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)
        or not test_paths
        or not DIGEST.fullmatch(route or "")
    ):
        return None
    relative = (
        "factory/migrations/ticket-rewrite/"
        f"{current['head_sha']}.json"
    )
    raw = git(args.factory_root, "show", f"{protected}:{relative}", check=False)
    if not raw or len(raw.encode()) > 1_000_000:
        return None
    try:
        authorization = json.loads(raw, object_pairs_hook=unique_object)
    except (json.JSONDecodeError, ValueError):
        return None
    receipt_digest = authorization.get("transition_receipt_sha256", "")
    expected = {
        "branch": current["branch"],
        "factory_sha": args.factory_sha,
        "head": current["head_sha"],
        "passport_sha256": previous.get("passport_sha256"),
        "previous_head": previous.get("head_sha"),
        "repository": repository,
        "role": "test-author",
        "route_plan_sha256": route,
        "schema": REWRITE_SCHEMA,
        "state": current_state,
        "ticket": args.ticket,
        "transition_receipt_sha256": receipt_digest,
    }
    if authorization != expected or not DIGEST.fullmatch(receipt_digest):
        return None
    try:
        consumed = receipt(args.state_dir, args.ticket, receipt_digest)
    except (FileNotFoundError, json.JSONDecodeError, OSError, PassportError):
        return None
    if (
        consumed.get("factory_sha") != args.factory_sha
        or consumed.get("head_sha") != previous.get("head_sha")
        or consumed.get("project") != args.project
        or consumed.get("role") != "test-author"
        or consumed.get("stage") != "FIX test-author"
        or not failed_rewrite_manifest(args, previous, receipt_digest)
        or not rewrite_delta_allowed(
            args.workdir, previous["head_sha"], current["head_sha"],
            test_paths, args.ticket,
        )
    ):
        return None
    return hashlib.sha256(canonical(authorization)).hexdigest()


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
    if git(args.workdir, "status", "--porcelain=v1", "-z"):
        raise PassportError("passport export requires a clean execution cell")
    previous: dict[str, Any] = {}
    parent_raw = None
    if destination.exists() or destination.is_symlink():
        previous, parent_raw = load_passport(destination, secret)
    consumed = receipt(args.state_dir, args.ticket, args.receipt)
    current_identity = identity(args)
    old_head = consumed.get("head_sha", "")
    bound_passport = consumed.get("passport_sha256")
    migrations = previous.get("migration_history", [])
    latest_migration = (
        migrations[-1]
        if isinstance(migrations, list)
        and migrations
        and isinstance(migrations[-1], dict)
        else {}
    )
    migrated_receipt = (
        bound_passport == previous.get("parent_file_sha256")
        and previous.get("factory_sha") == args.factory_sha
        and previous.get("head_sha") == current_identity["head_sha"]
        and latest_migration.get("from_factory_sha")
        == consumed.get("factory_sha")
        and latest_migration.get("from_head_sha") == old_head
        and latest_migration.get("to_factory_sha") == args.factory_sha
        and latest_migration.get("to_head_sha") == current_identity["head_sha"]
    )
    if (
        consumed.get("ticket") != args.ticket
        or consumed.get("project") != args.project
        or (
            consumed.get("factory_sha") != args.factory_sha
            and not migrated_receipt
        )
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
        or (bound_passport != parent_raw and not migrated_receipt)
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
    head_continuous = subprocess.run(
        [
            "git", "-C", str(args.workdir), "merge-base", "--is-ancestor",
            previous.get("head_sha", ""), current["head_sha"],
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    ).returncode == 0
    rewrite_authorization = (
        None
        if head_continuous
        else authorized_ticket_rewrite(
            args, previous, current, current_ticket_state, protected
        )
    )
    base_continuous = subprocess.run(
        [
            "git", "-C", str(args.factory_root), "merge-base", "--is-ancestor",
            previous.get("protected_base_sha", ""), protected,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    ).returncode == 0
    if (
        previous.get("ticket") != args.ticket
        or previous.get("project") != args.project
        or previous.get("branch") != current["branch"]
        or previous.get("product_origin_sha256") != current["product_origin_sha256"]
        or not base_continuous
        or (
            not head_continuous
            and not authorized_inflight_rewrite(
                args, previous, current, current_ticket_state, protected
            )
            and rewrite_authorization is None
        )
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
    migration = {
        "from_factory_sha": previous["factory_sha"],
        "from_head_sha": previous["head_sha"],
        "from_protected_base_sha": previous["protected_base_sha"],
        "to_factory_sha": args.factory_sha,
        "to_head_sha": current["head_sha"],
        "to_protected_base_sha": protected,
    }
    if rewrite_authorization is not None:
        migration["rewrite_authorization_sha256"] = rewrite_authorization
    evidence: dict[str, Any] = {}
    if rewrite_authorization is not None:
        completed, charges = run_evidence(args.factory_root / "factory", args.ticket)
        completed = merge_records(
            previous.get("completed_role_evidence", []), completed
        )
        charges = merge_records(previous.get("charge_records", []), charges)
        evidence = {
            "charge_records": charges,
            "completed_role_evidence": completed,
            "cumulative_charges_micro_usd": sum(
                item["charge_micro_usd"] for item in charges
            ),
        }
    value = {
        **{
            name: item for name, item in previous.items()
            if name not in {
                "authentication_sha256", "passport_sha256", "parent_digest",
                "parent_file_sha256", "nonce",
            }
        },
        **current,
        **evidence,
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
            migration,
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
