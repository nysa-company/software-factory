#!/usr/bin/env python3
"""Endorse a sealed Factory release without changing exact provider CLI pins."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import datetime
import importlib.util
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
from typing import Any, Iterator


PLAN_SCHEMA = "nysa.software-factory.provider-cli-pin-endorsement-plan/v1"
RESULT_SCHEMA = "nysa.software-factory.provider-cli-pin-endorsement-result/v1"
IDENTITY_KEYS = ("contract_version", "factory_sha", "factory_tree", "release_path")


def pin_module():
    path = Path(__file__).with_name("owner-provider-cli-pin.py")
    spec = importlib.util.spec_from_file_location("factory_provider_cli_pin", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("provider CLI pin authority is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PIN = pin_module()


def identity(value: dict[str, Any]) -> dict[str, str]:
    if not isinstance(value, dict) or any(
        not isinstance(value.get(key), str) for key in IDENTITY_KEYS
    ):
        raise PIN.PinError("provider CLI pin release identity is invalid")
    return {key: value[key] for key in IDENTITY_KEYS}


def validate_release(kits_root: Path, value: dict[str, Any]) -> dict[str, str]:
    item = identity(value)
    observed = PIN.release_identity(
        kits_root, Path(item["release_path"]).resolve(strict=True),
        item["factory_sha"], item["factory_tree"],
    )
    if observed != item:
        raise PIN.PinError("provider CLI pin release identity changed")
    return item


def endorsement_history(evidence: dict[str, Any]) -> list[dict[str, Any]]:
    value = evidence.get("endorsements", [])
    if not isinstance(value, list) or any(
        not isinstance(item, dict) for item in value
    ):
        raise PIN.PinError("provider CLI pin endorsement history is invalid")
    return value


def contract_fingerprint(prior: dict[str, str], target: dict[str, str]) -> str:
    contract_files = tuple(dict.fromkeys((
        *PIN.REQUIRED_RELEASE_FILES,
        "scripts/lib/provider-cli-version.sh",
        "scripts/owner-provider-cli-pin.py",
    )))
    values = []
    for relative in contract_files:
        old = PIN.sha256(PIN.secure_regular(
            Path(prior["release_path"]) / relative, "prior provider CLI contract",
        ))
        new = PIN.sha256(PIN.secure_regular(
            Path(target["release_path"]) / relative, "target provider CLI contract",
        ))
        if old != new:
            raise PIN.PinError("provider CLI contract changed; full maintenance is required")
        values.append((relative, old))
    return PIN.object_hash(values)


@contextmanager
def endorsement_guard(home_factory: Path) -> Iterator[None]:
    with PIN.flock(
        home_factory / ".provider-cli-pin.lock", "provider CLI pin lock",
    ), PIN.flock(
        home_factory / "provider-configuration.lock", "provider configuration lock",
    ):
        yield


def build_plan(
    home_factory: Path, kits_root: Path, target: dict[str, str], operator: str,
) -> dict[str, Any]:
    if not PIN.SAFE_OPERATOR.fullmatch(operator) or operator == "auto":
        raise PIN.PinError("operator ID is invalid")
    if PIN.read_journal(home_factory) is not None:
        raise PIN.PinError("provider CLI pin transaction recovery is required")
    receipt_path = home_factory / "provider-cli-pin.json"
    raw = PIN.secure_regular(receipt_path, "provider CLI pin receipt")
    if stat.S_IMODE(receipt_path.lstat().st_mode) != 0o600:
        raise PIN.PinError("provider CLI pin receipt is unsafe")
    evidence = PIN.receipt(receipt_path)
    if evidence is None or evidence.get("status") != "applied":
        raise PIN.PinError("provider CLI pin receipt is not applied")
    prior = validate_release(kits_root, evidence.get("candidate_release", {}))
    allowed_value = evidence.get("compatible_releases", [])
    if not isinstance(allowed_value, list):
        raise PIN.PinError("provider CLI pin compatible releases are invalid")
    allowed = [validate_release(kits_root, item) for item in allowed_value]
    if prior not in allowed:
        raise PIN.PinError("provider CLI pin authority is not approved")
    active = PIN.active_projects(kits_root)
    active_identities = [identity(item) for item in active]
    if any(item not in allowed for item in active_identities):
        raise PIN.PinError("an active release is not approved by the current receipt")
    status = PIN.check_status(home_factory, kits_root, prior, prior)
    if status["status"] != "ready":
        raise PIN.PinError("current provider CLI pins are not ready")
    fingerprint = contract_fingerprint(prior, target)
    compatible = sorted(
        {tuple(item[key] for key in IDENTITY_KEYS) for item in (*allowed, target)},
    )
    compatible_releases = [dict(zip(IDENTITY_KEYS, item)) for item in compatible]
    value = {
        "active_projects": active,
        "compatible_releases": compatible_releases,
        "contract_fingerprint": fingerprint,
        "current_links_sha256": PIN.object_hash(PIN.links(home_factory / "bin")),
        "global_config_sha256": evidence.get("global_config_sha256"),
        "operator_id": operator,
        "schema": PLAN_SCHEMA,
        "source_receipt_sha256": PIN.sha256(raw),
        "target_release": target,
    }
    return {**value, "approval_sha256": PIN.object_hash(value)}


def apply_plan(
    home_factory: Path, kits_root: Path, target: dict[str, str],
    operator: str, approval: str,
) -> dict[str, Any]:
    existing = PIN.receipt(home_factory / "provider-cli-pin.json")
    if existing is not None:
        for endorsement in endorsement_history(existing):
            if (
                isinstance(endorsement, dict)
                and endorsement.get("target_release") == target
                and endorsement.get("operator_id") == operator
                and endorsement.get("approval_sha256") == approval
            ):
                authority = validate_release(
                    kits_root, existing.get("candidate_release", {}),
                )
                status = PIN.check_status(home_factory, kits_root, target, authority)
                if status["status"] == "ready":
                    return {
                        "receipt_sha256": existing["receipt_sha256"],
                        "schema": RESULT_SCHEMA, "status": "endorsed",
                        "target_release": target,
                    }
    plan = build_plan(home_factory, kits_root, target, operator)
    if not PIN.SHA256.fullmatch(approval) or approval != plan["approval_sha256"]:
        raise PIN.PinError("provider CLI pin endorsement approval hash does not match")
    receipt_path = home_factory / "provider-cli-pin.json"
    evidence = PIN.receipt(receipt_path)
    if evidence is None:
        raise PIN.PinError("provider CLI pin receipt is unavailable")
    unsigned = dict(evidence)
    unsigned.pop("receipt_sha256")
    endorsements = endorsement_history(unsigned)
    record = {
        "approval_sha256": approval,
        "applied_at": datetime.datetime.now(datetime.timezone.utc).replace(
            microsecond=0,
        ).isoformat().replace("+00:00", "Z"),
        "operator_id": operator,
        "source_receipt_sha256": plan["source_receipt_sha256"],
        "target_release": target,
    }
    unsigned.update(
        compatible_releases=plan["compatible_releases"],
        endorsements=[*endorsements, record],
    )
    updated = {**unsigned, "receipt_sha256": PIN.object_hash(unsigned)}
    # The durable receipt replace is the commit point. Its exact endorsement
    # record makes a lost-output retry distinguishable from a new mutation.
    PIN.atomic_write(receipt_path, PIN.canonical(updated) + b"\n")
    PIN.fail_after("endorsement-receipt")
    return {
        "receipt_sha256": updated["receipt_sha256"],
        "schema": RESULT_SCHEMA, "status": "endorsed",
        "target_release": target,
    }


def roots(args: argparse.Namespace) -> tuple[Path, Path]:
    return PIN.roots(args)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kits-root", required=True, type=Path)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--tree", required=True)
    parser.add_argument("--release", required=True, type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan")
    plan.add_argument("--operator-id", required=True)
    apply = commands.add_parser("apply")
    apply.add_argument("--operator-id", required=True)
    apply.add_argument("--approve-hash", required=True)
    args = parser.parse_args()
    try:
        home_factory, kits_root = roots(args)
        target = PIN.release_identity(
            kits_root, args.release.resolve(strict=True), args.sha, args.tree,
            candidate=True,
        )
        if target != PIN.authority_identity(kits_root):
            raise PIN.PinError("endorsement target does not match the sealed authority helper")
        with endorsement_guard(home_factory):
            result = (
                build_plan(home_factory, kits_root, target, args.operator_id)
                if args.command == "plan"
                else apply_plan(
                    home_factory, kits_root, target, args.operator_id,
                    args.approve_hash,
                )
            )
        print(PIN.canonical(result).decode())
        return 0
    except (
        OSError, PIN.PinError, RuntimeError, sqlite3.Error,
        subprocess.SubprocessError,
    ) as error:
        print(f"provider-cli-pin endorsement: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
