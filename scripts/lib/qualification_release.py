"""Validate the owner-only receipt chain for one qualification activation."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Any


DIGEST = re.compile(r"^[0-9a-f]{64}$")
SHA = re.compile(r"^[0-9a-f]{40}$")
CONTRACTS = frozenset({"1.8.0", "2.0.0"})


class ReceiptError(ValueError):
    pass


def _canonical(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode()


def _directory(path: Path) -> Path:
    try:
        info = path.lstat()
        if (
            not path.is_absolute()
            or path.is_symlink()
            or path.resolve(strict=True) != path
            or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o700
        ):
            raise ReceiptError("qualification release directory is unsafe")
        return path
    except OSError as error:
        raise ReceiptError("qualification release directory is unsafe") from error


def _read(path: Path) -> dict[str, Any]:
    descriptor = -1
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_size > 1_000_000
        ):
            raise ReceiptError("qualification release record is unsafe")
        raw = os.read(descriptor, 1_000_001)
        if len(raw) != info.st_size:
            raise ReceiptError("qualification release record changed while reading")
        value = json.loads(raw.decode("utf-8", "strict"))
        if not isinstance(value, dict):
            raise ReceiptError("qualification release record is invalid")
        return value
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ReceiptError("qualification release record is invalid") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def receipt_chain(
    release: Path, project: str, product: Path,
) -> list[tuple[str, str]]:
    """Return current-to-oldest validated activation receipt identities."""
    if (
        not release.is_absolute()
        or not SHA.fullmatch(release.name)
        or release.parent.name != "releases"
        or not product.is_absolute()
    ):
        raise ReceiptError("qualification release identity is invalid")
    root = release.parent.parent
    for path in (
        root, release.parent, root / "projects",
        root / "projects" / project, root / "receipts",
    ):
        _directory(path)
    active = _read(root / "projects" / project / "active.json")
    receipt_id = active.get("receipt_id")
    if (
        active.get("project") != project
        or active.get("kit_sha") != release.name
        or active.get("release_path") != str(release)
        or not isinstance(receipt_id, str)
        or not DIGEST.fullmatch(receipt_id)
    ):
        raise ReceiptError("qualification release activation is invalid")
    result: list[tuple[str, str]] = []
    seen: set[str] = set()
    while receipt_id:
        if receipt_id in seen or len(seen) >= 128:
            raise ReceiptError("qualification release receipt is invalid")
        seen.add(receipt_id)
        receipt = _read(root / "receipts" / f"{receipt_id}.json")
        unsigned = dict(receipt)
        embedded = unsigned.pop("receipt_id", "")
        previous = receipt.get("previous_receipt_id")
        kit_sha = receipt.get("kit_sha")
        if (
            embedded != receipt_id
            or receipt_id != hashlib.sha256(_canonical(unsigned)).hexdigest()
            or receipt.get("project") != project
            or receipt.get("product_path") != str(product)
            or receipt.get("contract_version") not in CONTRACTS
            or receipt.get("qualification_mode") != "isolated"
            or receipt.get("status") != "pass"
            or not isinstance(kit_sha, str)
            or not SHA.fullmatch(kit_sha)
            or not isinstance(receipt.get("kit_tree"), str)
            or not SHA.fullmatch(receipt["kit_tree"])
            or not isinstance(receipt.get("provider_policy_sha256"), str)
            or not DIGEST.fullmatch(receipt["provider_policy_sha256"])
            or (previous is not None and (
                not isinstance(previous, str) or not DIGEST.fullmatch(previous)
            ))
        ):
            raise ReceiptError("qualification release receipt is invalid")
        result.append((kit_sha, receipt_id))
        receipt_id = previous or ""
    if not result or result[0][0] != release.name:
        raise ReceiptError("qualification release receipt is invalid")
    return result
