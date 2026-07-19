#!/usr/bin/env python3
"""Validated read-only snapshots for the local operator console."""

import argparse
import csv
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import sys


ROOT = Path(__file__).resolve().parents[1]
ROLES = (
    ("planner", "Production", "Turns a Ready ticket into a testable frozen contract."),
    ("spec-linter", "Checking", "Checks specification quality and contract coverage."),
    ("test-author", "Checking", "Writes failing tests before implementation."),
    ("builder", "Production", "Implements the frozen contract without editing tests."),
    ("reviewer", "Checking", "Reviews test adequacy and specification conformance."),
    ("narrator", "Production", "Builds the operator-facing evidence bundle."),
)
TICKET = re.compile(r"^T-[0-9]+$")


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ROUTER = load_module("operator_model_router", ROOT / "scripts/model-router.py")
MANAGER = load_module("operator_model_manager", ROOT / "scripts/model-manager.py")
ENVELOPE = load_module("operator_envelope", ROOT / "scripts/envelope-control.py")


class SnapshotError(ValueError):
    pass


def physical_directory(path):
    resolved = path.resolve(strict=True)
    info = resolved.lstat()
    if (
        resolved != path
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_mode & 0o022
    ):
        raise SnapshotError("product root is unsafe")
    return resolved


def safe_text(path, maximum=1_000_000):
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or path.is_symlink()
        or info.st_uid != os.geteuid()
        or info.st_mode & 0o022
        or info.st_nlink != 1
        or info.st_size > maximum
    ):
        raise SnapshotError("snapshot input is unsafe")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise SnapshotError("snapshot input changed")
        with os.fdopen(descriptor, encoding="utf-8") as handle:
            descriptor = -1
            return handle.read()
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def ticket_rows(factory):
    directory = factory / "tickets"
    if not directory.exists():
        return []
    physical_directory(directory)
    result = []
    for path in sorted(directory.glob("T-*.md")):
        ticket = path.stem
        if not TICKET.fullmatch(ticket):
            continue
        text = safe_text(path)
        state = next(
            (
                line.split(":", 1)[1].strip()
                for line in text.splitlines()
                if line.startswith("State:")
            ),
            "Unknown",
        )
        result.append({"ticket": ticket, "state": state})
    return result


def selected_profile(product, state_root, project, routes, profile_map):
    policy_path = product / "factory/model-policy.json"
    policy = MANAGER._load_model_policy(
        policy_path if policy_path.exists() else None, routes
    )
    if policy is not None:
        return ROUTER.model_policy_profile(policy, routes), "project-policy"
    active_path = state_root / project / "routing/active.json"
    active = MANAGER._load_active(active_path, project, profile_map)
    profile_id = active["profile_id"] if active else MANAGER.DEFAULT_PROFILE
    return profile_map[profile_id], profile_id


def role_routes(product, state_root, project):
    catalog, routes, _, profile_map = ROUTER.load_policy()
    profile, source = selected_profile(
        product, state_root, project, routes, profile_map
    )
    portfolio = profile["portfolios"][0]
    values = []
    descriptions = {role: (lane, purpose) for role, lane, purpose in ROLES}
    for role in ROUTER.ROLES:
        role_policy = portfolio["roles"][role]
        primary, secondary = role_policy["candidates"][:2]
        lane, purpose = descriptions[role]
        values.append(
            {
                "role": role,
                "lane": lane,
                "purpose": purpose,
                "effort": role_policy["effort"],
                "primary": {
                    "route_id": primary,
                    "model": routes[primary]["selection_id"],
                    "family": routes[primary]["provider_family"],
                    "adapter": routes[primary]["adapter"],
                },
                "secondary": {
                    "route_id": secondary,
                    "model": routes[secondary]["selection_id"],
                    "family": routes[secondary]["provider_family"],
                    "adapter": routes[secondary]["adapter"],
                },
            }
        )
    return values, source, ROUTER.content_hash(catalog)


def workflow_snapshot(product, state_root, project):
    roles, source, catalog_hash = role_routes(
        product, state_root, project
    )
    return {
        "catalog_hash": catalog_hash,
        "profile": source,
        "project": project,
        "roles": roles,
        "schema": "factory-operator-workflow/v1",
        "tickets": ticket_rows(product / "factory"),
    }


def envelope_snapshot(product, project):
    state = ENVELOPE.read_state(product)
    values = state[-1]
    return {
        "env_sha256": ENVELOPE.digest(state[4]),
        "markdown_sha256": ENVELOPE.digest(state[5]),
        "project": project,
        "roles": {
            role: ENVELOPE.effective_role(values, role)
            for role in ENVELOPE.ROLES
        },
        "schema": "factory-operator-envelope/v1",
        "values": values,
    }


def spend_snapshot(product, project):
    path = product / "factory/runtime-ledger.csv"
    by_role = {}
    total = 0.0
    runs = 0
    if path.exists():
        text = safe_text(path, 10_000_000)
        reader = csv.DictReader(text.splitlines())
        today = datetime.now(timezone.utc).date().isoformat()
        for row in reader:
            if row.get("date") != today:
                continue
            try:
                cost = float(row.get("cost_usd", ""))
            except (TypeError, ValueError):
                raise SnapshotError("runtime ledger contains invalid cost")
            role = row.get("role", "unknown")
            by_role[role] = by_role.get(role, 0.0) + cost
            total += cost
            runs += 1
    return {
        "by_role_usd": dict(
            (role, format(cost, ".6f")) for role, cost in sorted(by_role.items())
        ),
        "date": datetime.now(timezone.utc).date().isoformat(),
        "project": project,
        "runs": runs,
        "schema": "factory-operator-spend/v1",
        "total_usd": format(total, ".6f"),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("view", choices=("workflow", "envelope", "spend"))
    args = parser.parse_args()
    project = os.environ.get("FACTORY_PROJECT", "")
    product_value = os.environ.get("FACTORY_ROOT", "")
    state_value = os.environ.get("FACTORY_MODEL_STATE_ROOT", "")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", project):
        raise SnapshotError("project context is invalid")
    if not product_value or not state_value:
        raise SnapshotError("factory context is incomplete")
    product = physical_directory(Path(product_value))
    state_root = physical_directory(Path(state_value))
    if args.view == "workflow":
        value = workflow_snapshot(product, state_root, project)
    elif args.view == "envelope":
        value = envelope_snapshot(product, project)
    else:
        value = spend_snapshot(product, project)
    print(json.dumps(value, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    try:
        main()
    except (SnapshotError, OSError, ValueError, json.JSONDecodeError) as error:
        print(f"operator-state: {error}", file=sys.stderr)
        raise SystemExit(2)
