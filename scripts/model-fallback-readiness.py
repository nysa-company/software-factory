#!/usr/bin/env python3
"""Require a ready same-family native fallback for selected Cursor routes."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from pathlib import Path


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def version(value: object) -> str | None:
    if (
        value == "test"
        and os.environ.get("FACTORY_TEST_MODE") == "1"
        and os.environ.get("FACTORY_TRUSTED_TEST_HARNESS") == "1"
    ):
        return "test"
    match = re.search(r"(?<![0-9])[0-9]+[.][0-9]+[.][0-9]+(?![0-9])", str(value))
    return match.group(0) if match else None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--readiness", required=True, type=Path)
    parser.add_argument("--catalog", required=True, type=Path)
    parser.add_argument("--profiles", required=True, type=Path)
    args = parser.parse_args()
    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    readiness = json.loads(args.readiness.read_text(encoding="utf-8"))
    catalog = json.loads(args.catalog.read_text(encoding="utf-8"))
    profiles = json.loads(args.profiles.read_text(encoding="utf-8"))
    routes = {item["route_id"]: item for item in catalog["routes"]}
    profile = next(
        item for item in profiles["profiles"] if item["profile_id"] == plan["profile_id"]
    )
    portfolio = next(
        item for item in profile["portfolios"]
        if item["portfolio_id"] == plan["portfolio_id"]
    )
    policy_roles = plan.get("model_policy", {}).get("roles")
    checks = []
    for role, selected in sorted(plan["selections"].items()):
        if not selected["adapter"].startswith("cursor-"):
            continue
        if policy_roles is None:
            candidates = portfolio["roles"][role]["candidates"]
        else:
            policy = policy_roles[role]
            candidates = [policy["primary_route_id"], policy["secondary_route_id"]]
        selected_index = candidates.index(selected["route_id"])
        alternatives = [
            route for route_id in candidates[selected_index + 1 :]
            if (route := routes[route_id])["provider_family"] == selected["provider_family"]
            and route["adapter"] in {"codex", "claude-code"}
        ]
        fallback = next(
            (route for route in alternatives if readiness[route["route_id"]]["state"] == "READY"),
            alternatives[0] if alternatives else None,
        )
        state = readiness.get(fallback["route_id"], {}) if fallback else {}
        adapter = fallback.get("adapter") if fallback else None
        expected = (
            version(os.environ.get("CODEX_PINNED", "0.144.1"))
            if adapter == "codex" else
            version(os.environ.get("CLAUDE_CODE_PINNED", "2.1.223"))
            if adapter == "claude-code" else None
        )
        installed = version(state.get("adapter_version"))
        check_state = state.get("state", "INVALID")
        reason = state.get("reason", "same_family_native_fallback_missing")
        if check_state == "READY" and installed not in {expected, "test"}:
            check_state, reason = "INVALID", "version_mismatch"
        checks.append({
            "cursor_route_id": selected["route_id"],
            "fallback_route_id": fallback["route_id"] if fallback else None,
            "expected_version": expected,
            "installed_version": installed,
            "reason": reason,
            "role": role,
            "state": check_state,
        })
    status = "ready" if all(item["state"] == "READY" for item in checks) else "invalid"
    report = {
        "checks": checks,
        "profile_id": plan["profile_id"],
        "schema": "nysa.software-factory.qualification-fallback-readiness/v1",
        "status": status,
    }
    report["readiness_sha256"] = hashlib.sha256(canonical(report)).hexdigest()
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return 0 if status == "ready" else 1


if __name__ == "__main__":
    raise SystemExit(main())
