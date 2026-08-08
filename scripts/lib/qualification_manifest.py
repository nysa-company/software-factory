"""Shared strict validation for the Contract 1.8 qualification manifest."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Any


SCHEMA = "nysa.software-factory.qualification/v2"
SHA = re.compile(r"[0-9a-f]{40}\Z")
ZERO_SHA = "0" * 40
TICKET = re.compile(r"T-[0-9]+\Z")


class ManifestError(ValueError):
    pass


def command(root: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, check=False,
    )


def commit(root: Path, revision: str) -> str:
    result = command(
        root, "rev-parse", "--verify", "--end-of-options",
        f"{revision}^{{commit}}",
    )
    try:
        value = result.stdout.decode("ascii", "strict").strip()
    except UnicodeDecodeError:
        value = ""
    if not SHA.fullmatch(value):
        raise ManifestError("qualification manifest comparison commit is invalid")
    return value


def committed_blob(root: Path, revision: str, path: str, limit: int) -> bytes | None:
    result = command(root, "ls-tree", "-z", revision, "--", path)
    if result.returncode != 0:
        raise ManifestError("qualification manifest committed input is unavailable")
    if not result.stdout:
        return None
    if not result.stdout.endswith(b"\0") or result.stdout.count(b"\0") != 1:
        raise ManifestError("qualification manifest committed input is malformed")
    try:
        metadata, actual = result.stdout[:-1].split(b"\t", 1)
        mode, kind, object_id = metadata.decode("ascii").split()
        actual_path = actual.decode("utf-8", "strict")
    except (UnicodeDecodeError, ValueError) as error:
        raise ManifestError("qualification manifest committed input is malformed") from error
    if mode != "100644" or kind != "blob" or actual_path != path or not SHA.fullmatch(object_id):
        raise ManifestError("qualification manifest committed input is unsafe")
    size = command(root, "cat-file", "-s", object_id)
    try:
        byte_count = int(size.stdout) if size.returncode == 0 else limit + 1
    except ValueError:
        byte_count = limit + 1
    if byte_count < 0 or byte_count > limit:
        raise ManifestError("qualification manifest committed input is oversized")
    blob = command(root, "cat-file", "blob", object_id)
    if blob.returncode != 0 or len(blob.stdout) != byte_count:
        raise ManifestError("qualification manifest committed input is unavailable")
    return blob.stdout


def validate_committed(
    product_root: Path, base_revision: str, head_revision: str,
) -> str:
    root = product_root.resolve(strict=True)
    top = command(root, "rev-parse", "--show-toplevel")
    try:
        top_root = Path(top.stdout.decode("utf-8", "strict").strip()).resolve(
            strict=True,
        )
    except (OSError, UnicodeDecodeError):
        top_root = None
    if top.returncode != 0 or top_root != root:
        raise ManifestError("qualification manifest product is not a Git checkout")
    head = commit(root, head_revision)
    base = ZERO_SHA if base_revision == ZERO_SHA else commit(root, base_revision)
    if base == ZERO_SHA:
        raw = committed_blob(root, head, "factory/QUALIFICATION.json", 131_072)
        if raw is None:
            return "absent"
    else:
        changed = command(
            root, "diff", "--quiet", "--no-ext-diff", "--no-renames", base,
            head, "--", "factory/QUALIFICATION.json",
        )
        if changed.returncode == 0:
            return "unchanged"
        if changed.returncode != 1:
            raise ManifestError("qualification manifest committed diff is unavailable")
        raw = committed_blob(root, head, "factory/QUALIFICATION.json", 131_072)
        if raw is None:
            return "absent"
    pin_raw = committed_blob(root, head, "factory/KIT_PIN", 64)
    if pin_raw is None:
        raise ManifestError("factory/KIT_PIN is missing")
    try:
        pin = pin_raw.decode("ascii", "strict")
        value = json.loads(raw.decode("utf-8", "strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ManifestError("Contract 1.8 qualification manifest is invalid") from error
    if not re.fullmatch(r"[0-9a-f]{40}\n?", pin):
        raise ManifestError("factory/KIT_PIN must contain one lowercase full SHA")
    validate(value, pin.rstrip("\n"))
    return "validated"


def validate(
    value: Any, factory_sha: str, expected_capacity: int | None = None,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ManifestError("qualification manifest must be an object")
    successor = value.get("mode") == "successor"
    expected_keys = {
        "budget_usd", "capacity", "contract_version", "factory_sha",
        "generation", "per_run_budget_usd", "per_ticket_budget_usd",
        "schema", "target_done", "tickets",
    } | ({"mode", "source_factory_sha"} if successor else set())
    tickets = value.get("tickets")
    target = value.get("target_done")
    capacity = value.get("capacity")
    if (
        set(value) != expected_keys
        or value.get("schema") != SCHEMA
        or value.get("contract_version") != "1.8.0"
        or not SHA.fullmatch(factory_sha)
        or value.get("factory_sha") != factory_sha
        or capacity not in (3, 4)
        or expected_capacity is not None and capacity != expected_capacity
        or target not in (3, 4)
        or target > capacity
        or not isinstance(value.get("generation"), int)
        or isinstance(value.get("generation"), bool)
        or value["generation"] < 1
        or not isinstance(tickets, list)
        or len(tickets) != target
        or len(tickets) != len(set(tickets))
        or any(not isinstance(ticket, str) or not TICKET.fullmatch(ticket)
               for ticket in tickets)
        or successor and (
            capacity != 3
            or target != 3
            or value.get("budget_usd") != "300.000000"
            or value.get("per_ticket_budget_usd") != "100.000000"
            or value.get("per_run_budget_usd") != "10.000000"
            or not SHA.fullmatch(value.get("source_factory_sha", ""))
            or value["source_factory_sha"] == factory_sha
        )
        or not successor and (
            value.get("budget_usd") != "100.000000"
            or value.get("per_ticket_budget_usd") != "25.000000"
            or value.get("per_run_budget_usd") != "2.000000"
        )
    ):
        raise ManifestError("Contract 1.8 qualification manifest is invalid")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate a changed committed qualification manifest.",
    )
    parser.add_argument("--product-root", type=Path, required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    args = parser.parse_args()
    try:
        status = validate_committed(args.product_root, args.base, args.head)
    except (ManifestError, OSError) as error:
        print(f"QUALIFICATION MANIFEST FAIL: {error}", file=sys.stderr)
        return 1
    print(f"QUALIFICATION MANIFEST {status.upper()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
