#!/usr/bin/env python3
"""Deterministic read-only evidence before Factory certification."""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Any

sys.dont_write_bytecode = True
LIB = Path(__file__).resolve().parent / "lib"
sys.path.insert(0, str(LIB))
from certification_plan import (  # noqa: E402
    NODE, NPM, PlanError, TupleError, compare_tuple, expected_tuple,
    observed_tuple, safe_plan, validate_plan,
)
from activation_preflight import validate as validate_activation  # noqa: E402
from historical_pr_objects import github_auth, run_git, run_git_remote  # noqa: E402

SCHEMA = "nysa.software-factory.operator-preflight-report/v1"
SHA = re.compile(r"[0-9a-f]{40}\Z")
TICKET = re.compile(r"T-[0-9]+\Z")


def ticket_module() -> Any:
    path = Path(__file__).resolve().parent / "ticket-readiness.py"
    spec = importlib.util.spec_from_file_location("factory_ticket_readiness", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("ticket readiness validator is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def git(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return run_git(root, *arguments)


def text(result: subprocess.CompletedProcess[str]) -> str:
    return result.stdout.strip()


def product_snapshot(product: Path, origin: str) -> dict[str, Any]:
    snapshot = {
        "branch": None,
        "clean": None,
        "exact_repository": False,
        "identity_changed": False,
        "identity_stable": False,
        "remote_main_sha": None,
        "sha": None,
        "tree": None,
    }

    def identity() -> tuple[str, str] | None:
        result = git(product, "rev-parse", "HEAD", "HEAD^{tree}")
        rows = text(result).splitlines() if result.returncode == 0 else []
        if len(rows) != 2 or not all(SHA.fullmatch(item) for item in rows):
            return None
        return rows[0], rows[1]

    try:
        top = git(product, "rev-parse", "--show-toplevel")
        snapshot["exact_repository"] = (
            top.returncode == 0
            and Path(text(top)).resolve(strict=True) == product
        )
        before = identity()
        remote = run_git_remote(
            "ls-remote", "--exit-code", "--", origin, "refs/heads/main",
            auth=github_auth(origin),
        )
        rows = text(remote).splitlines() if remote.returncode == 0 else []
        if len(rows) == 1:
            fields = rows[0].split("\t")
            if (
                len(fields) == 2
                and fields[1] == "refs/heads/main"
                and SHA.fullmatch(fields[0])
            ):
                snapshot["remote_main_sha"] = fields[0]
        branch = git(product, "symbolic-ref", "--quiet", "--short", "HEAD")
        snapshot["branch"] = text(branch) if branch.returncode == 0 else None
        clean = git(
            product, "status", "--porcelain=v1", "-z", "--untracked-files=all",
        )
        snapshot["clean"] = clean.returncode == 0 and not clean.stdout
        after = identity()
        snapshot["identity_changed"] = before != after
        snapshot["identity_stable"] = before is not None and before == after
        if after is not None:
            snapshot["sha"], snapshot["tree"] = after
    except (OSError, UnicodeError, ValueError):
        pass
    return snapshot


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--product", required=True, type=Path)
    parser.add_argument("--factory-sha", required=True)
    parser.add_argument("--factory-tree", required=True)
    parser.add_argument("--contract-version", required=True)
    parser.add_argument("--product-pin", required=True)
    parser.add_argument("--product-origin", required=True)
    parser.add_argument("--network-reviewed", required=True)
    parser.add_argument("--ticket", action="append", default=[])
    parser.add_argument("--certified-previous-tree", default="")
    args = parser.parse_args()

    blockers: list[dict[str, str]] = []

    def block(reason: str, scope: str) -> None:
        item = {"reason_code": reason, "scope": scope}
        if item not in blockers:
            blockers.append(item)

    product = args.product.resolve(strict=True)
    factory = {
        "contract_version": args.contract_version,
        "sha": args.factory_sha,
        "tree": args.factory_tree,
    }
    if not SHA.fullmatch(args.factory_sha) or not SHA.fullmatch(args.factory_tree):
        block("factory_identity_invalid", "factory")

    def apply_snapshot(snapshot: dict[str, Any]) -> None:
        if not snapshot["exact_repository"] or snapshot["sha"] is None:
            block("product_identity_invalid", "product")
        if snapshot["identity_changed"]:
            block("identity_changed", "product")
        if snapshot["branch"] != "main":
            block("product_branch_not_main", "product")
        if snapshot["clean"] is not True:
            block("product_dirty", "product")
        if snapshot["remote_main_sha"] is None:
            block("product_remote_main_unavailable", "product")
        elif snapshot["remote_main_sha"] != snapshot["sha"]:
            block("product_head_not_remote_main", "product")

    initial_snapshot = product_snapshot(product, args.product_origin)
    apply_snapshot(initial_snapshot)
    product_sha = initial_snapshot["sha"]
    product_tree = initial_snapshot["tree"]

    if args.product_pin != args.factory_sha:
        block("product_kit_pin_mismatch", "product")
    qualification = product / "factory" / "QUALIFICATION.json"
    if qualification.exists() or qualification.is_symlink():
        block("qualification_manifest_present", "product")

    activation_blockers, _, _ = validate_activation(
        product,
        args.factory_sha,
        Path(__file__).resolve().parent,
        args.product_origin,
        args.certified_previous_tree,
    )
    for item in activation_blockers:
        block(item["reason_code"], item["scope"])

    runtime = {
        "expected": {"node": None, "npm": None},
        "observed": {"node": None, "npm": None},
        "status": "blocked",
    }
    network = {
        "required": None,
        "required_phases": [],
        "reviewed": args.network_reviewed == "1",
        "status": "blocked",
    }
    plan_digest: str | None = None
    try:
        if args.network_reviewed not in {"0", "1"}:
            raise PlanError("network review value is invalid")
        plan, plan_digest = safe_plan(product / "factory" / "certification-plan.json")
        phases = validate_plan(plan, product)
        required = sorted(
            name for name, phase in phases.items() if phase["network"] == "required"
        )
        network.update({
            "required": bool(required),
            "required_phases": required,
            "status": (
                "authorization-required"
                if required and args.network_reviewed != "1" else "pass"
            ),
        })
        if product_sha is None or product_tree is None:
            raise TupleError(
                "runtime_tuple_invalid", "product_sha", "Git identity", "missing",
            )
        identity = {
            "contract_version": args.contract_version,
            "factory_sha": args.factory_sha,
            "factory_tree": args.factory_tree,
            "product_sha": product_sha,
            "product_tree": product_tree,
        }
        expected = expected_tuple(identity, plan)
        observed = observed_tuple(identity)
        runtime["expected"] = {key: expected[key] for key in ("node", "npm")}
        runtime["observed"] = {
            "node": observed["node"] if NODE.fullmatch(observed["node"]) else None,
            "npm": observed["npm"] if NPM.fullmatch(observed["npm"]) else None,
        }
        compare_tuple(expected, observed)
        runtime["status"] = "pass"
    except (
        FileNotFoundError, json.JSONDecodeError, OSError, PlanError, UnicodeError,
    ):
        block("certification_plan_invalid", "certification")
    except TupleError:
        block("runtime_tuple_mismatch", "certification")

    readiness = ticket_module()
    ticket_results = []
    ownership: dict[str, list[str]] = {}
    seen: set[str] = set()
    for ticket in args.ticket:
        if not TICKET.fullmatch(ticket) or ticket in seen:
            block("ticket_set_invalid", "tickets")
            continue
        seen.add(ticket)
        paths: list[str] = []
        state_ready = False
        status = "pass"
        try:
            ticket_path = product / "factory" / "tickets" / f"{ticket}.md"
            ticket_text = ticket_path.read_text(encoding="utf-8")
            ticket_state = readiness.field(ticket_text, "State")
            if ticket_state != "Ready":
                raise readiness.ReadinessError("ticket is not Ready")
            state_ready = True
            readiness.validate(ticket, product)
            paths = readiness.builder_paths(ticket_text)
            for path in paths:
                ownership.setdefault(path, []).append(ticket)
        except (
            OSError, UnicodeError, readiness.ReadinessError,
            subprocess.SubprocessError,
        ):
            status = "blocked"
            block("ticket_readiness_invalid", ticket)
        ticket_results.append({
            "builder_paths": paths, "state_ready": state_ready, "status": status,
            "ticket": ticket,
        })
    selected_paths = set(ownership)
    for path in sorted((product / "factory/tickets").glob("T-*.md")):
        ticket = path.stem
        if ticket in seen or not TICKET.fullmatch(ticket):
            continue
        try:
            ticket_text = path.read_text(encoding="utf-8")
            if readiness.field(ticket_text, "State").casefold() != "ready":
                continue
            for builder_path in readiness.builder_paths(ticket_text):
                if builder_path in selected_paths:
                    ownership[builder_path].append(ticket)
        except (OSError, UnicodeError, readiness.ReadinessError):
            continue
    conflicts = [
        {"path": path, "tickets": sorted(tickets)}
        for path, tickets in sorted(ownership.items()) if len(tickets) > 1
    ]
    for conflict in conflicts:
        block("builder_ownership_conflict", ",".join(conflict["tickets"]))

    final_snapshot = product_snapshot(product, args.product_origin)
    if final_snapshot != initial_snapshot:
        block("identity_changed", "product")
    apply_snapshot(final_snapshot)

    blockers.sort(key=lambda item: (item["scope"], item["reason_code"]))
    authorizations = (
        ["certification_network_review"]
        if network["status"] == "authorization-required" else []
    )
    status = "blocked" if blockers else (
        "authorization-required" if authorizations else "pass"
    )
    report = {
        "authorizations_required": authorizations,
        "blockers": blockers,
        "certification": {
            "network_review": network,
            "plan_sha256": plan_digest,
            "runtime": runtime,
        },
        "factory": factory,
        "ownership_conflicts": conflicts,
        "product": {
            "branch": final_snapshot["branch"],
            "clean": final_snapshot["clean"],
            "head_equals_remote_main": (
                final_snapshot["sha"] is not None
                and final_snapshot["sha"] == final_snapshot["remote_main_sha"]
            ),
            "identity_stable": (
                final_snapshot["identity_stable"]
                and final_snapshot == initial_snapshot
            ),
            "kit_pin": args.product_pin,
            "path": str(product),
            "remote_main_sha": final_snapshot["remote_main_sha"],
            "sha": final_snapshot["sha"],
            "tree": final_snapshot["tree"],
        },
        "project": args.project,
        "schema": SCHEMA,
        "status": status,
        "tickets": ticket_results,
    }
    print(json.dumps(report, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
    return {"pass": 0, "authorization-required": 3, "blocked": 2}[status]


if __name__ == "__main__":
    raise SystemExit(main())
