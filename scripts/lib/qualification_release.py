"""Validate the owner-only receipt chain for one qualification activation."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
from typing import Any


DIGEST = re.compile(r"^[0-9a-f]{64}$")
SHA = re.compile(r"^[0-9a-f]{40}$")
CONTRACTS = frozenset({"1.8.0", "2.0.0"})


class ReceiptError(ValueError):
    pass


def _require_ancestor(product: Path, older: str, newer: str) -> None:
    try:
        result = subprocess.run(
            [
                "/usr/bin/git", "-C", str(product),
                "merge-base", "--is-ancestor", older, newer,
            ],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=5, check=False,
            env={
                "GIT_CONFIG": os.devnull,
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_SYSTEM": os.devnull,
                "GIT_NO_LAZY_FETCH": "1",
                "GIT_NO_REPLACE_OBJECTS": "1",
                "GIT_OPTIONAL_LOCKS": "0",
                "GIT_PROTOCOL_FROM_USER": "0",
                "LANG": "C", "LC_ALL": "C", "PATH": "/usr/bin:/bin",
            },
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ReceiptError("qualification release product history is invalid") from error
    if result.returncode != 0:
        raise ReceiptError("qualification release product history is invalid")


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


def _activation_chain(
    release: Path, project: str, product: Path,
    *, receipt_id: str | None = None,
) -> tuple[dict[str, Any], list[tuple[str, str, dict[str, Any]]]]:
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
    active: dict[str, Any] = {}
    if receipt_id is None:
        active = _read(root / "projects" / project / "active.json")
        receipt_id = active.get("receipt_id")
        if (
            active.get("project") != project
            or active.get("kit_sha") != release.name
            or active.get("release_path") != str(release)
        ):
            raise ReceiptError("qualification release activation is invalid")
    if not isinstance(receipt_id, str) or not DIGEST.fullmatch(receipt_id):
        raise ReceiptError("qualification release activation is invalid")
    result: list[tuple[str, str, dict[str, Any]]] = []
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
        result.append((kit_sha, receipt_id, receipt))
        receipt_id = previous or ""
    if not result or result[0][0] != release.name:
        raise ReceiptError("qualification release receipt is invalid")
    return active, result


def receipt_chain(
    release: Path, project: str, product: Path,
) -> list[tuple[str, str]]:
    """Return current-to-oldest validated activation receipt identities."""
    _active, records = _activation_chain(release, project, product)
    return [(kit_sha, receipt_id) for kit_sha, receipt_id, _receipt in records]


def role_control_epoch(
    release: Path, project: str, product: Path, receipt_id: str,
    current_product_sha: str, current_product_tree: str,
) -> tuple[str, str]:
    """Return the immutable product SHA/tree where qualification began."""
    _active, records = _activation_chain(
        release, project, product, receipt_id=receipt_id,
    )
    if records[0][0] != release.name:
        raise ReceiptError("qualification release receipt is invalid")
    origin = records[0][2].get("product_origin")
    from legacy_closeout import _git_object_info

    for _kit_sha, _receipt_id, receipt in records:
        if (
            not isinstance(receipt.get("product_sha"), str)
            or not SHA.fullmatch(receipt["product_sha"])
            or not isinstance(receipt.get("product_tree"), str)
            or not SHA.fullmatch(receipt["product_tree"])
            or receipt.get("product_origin") != origin
            or not isinstance(origin, str)
            or not origin
        ):
            raise ReceiptError("qualification release receipt is invalid")
        commit = _git_object_info(product, receipt["product_sha"])
        tree = _git_object_info(product, f"{receipt['product_sha']}^{{tree}}")
        if (
            commit is None
            or commit[0] != receipt["product_sha"]
            or commit[1] != "commit"
            or tree is None
            or tree[0] != receipt["product_tree"]
            or tree[1] != "tree"
        ):
            raise ReceiptError("qualification release product history is invalid")
    for newer, older in zip(records, records[1:]):
        _require_ancestor(
            product, older[2]["product_sha"], newer[2]["product_sha"],
        )
    current = records[0][2]
    if (
        current.get("product_sha") != current_product_sha
        or current.get("product_tree") != current_product_tree
    ):
        raise ReceiptError("qualification release activation is invalid")
    oldest = records[-1][2]
    return oldest["product_sha"], oldest["product_tree"]
