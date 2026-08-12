#!/usr/bin/env python3
"""Validate owner-local provider activation and select one fixed route."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat


SCHEMA_V1 = "nysa.software-factory.provider-activation/v1"
SCHEMA_V2 = "nysa.software-factory.provider-activation/v2"
OUTPUT_SCHEMA_V1 = "nysa.software-factory.provider-activation-selection/v1"
OUTPUT_SCHEMA_V2 = "nysa.software-factory.provider-activation-selection/v2"
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,199}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
CLI_ADAPTERS = frozenset(("claude-code", "codex", "cursor-anthropic", "cursor-openai"))
POLICY_SCHEMA = "factory-provider-concurrency-policy/v1"


class ActivationError(ValueError):
    pass


def canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def valid_identity(route):
    return all(
        isinstance(route.get(field), str) and SAFE_ID.fullmatch(route[field])
        for field in ("account_route", "model", "provider_family")
    )


def valid_v1_route(route):
    return (
        isinstance(route, dict)
        and set(route)
        == {"account_route", "broker_path", "model", "protocol", "provider_family"}
        and valid_identity(route)
        and route["protocol"]
        in ("openai-chat", "openai-responses", "anthropic-messages")
        and isinstance(route["broker_path"], str)
        and route["broker_path"].startswith("/")
        and not route["broker_path"].startswith("//")
        and "?" not in route["broker_path"]
        and "#" not in route["broker_path"]
    )


def valid_v2_route(route):
    allowed = CLI_ADAPTERS
    if (os.environ.get("FACTORY_TEST_MODE") == "1" and
            os.environ.get("FACTORY_TRUSTED_TEST_HARNESS") == "1"):
        allowed = allowed | {"mock"}
    return (
        isinstance(route, dict)
        and set(route) == {"account_route", "adapter", "model", "provider_family"}
        and valid_identity(route)
        and route.get("adapter") in allowed
    )


def read_secure(path, label, owner_only=False):
    if not path.is_absolute():
        raise ActivationError(f"{label} path must be absolute")
    info = path.lstat()
    if (
        path.is_symlink()
        or not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_nlink != 1
        or info.st_mode & (0o077 if owner_only else 0o022)
        or info.st_size > 1_000_000
    ):
        raise ActivationError(f"{label} is unsafe")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(descriptor)
        if ((opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino) or
                not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1 or
                opened.st_size > 1_000_000):
            raise ActivationError(f"{label} changed while opening")
        with os.fdopen(descriptor, encoding="utf-8") as handle:
            descriptor = -1
            raw = handle.read(1_000_001)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(raw.encode("utf-8")) > 1_000_000:
        raise ActivationError(f"{label} is too large")
    return raw, json.loads(raw)


def validate_cli_policy(policy, routes, expected_hash):
    def capacity(value, maximum):
        return (isinstance(value, int) and not isinstance(value, bool) and
                1 <= value <= maximum)

    required = {"schema", "coupled_max_concurrent", "global", "provider_families", "account_routes"}
    if not isinstance(policy, dict) or set(policy) != required or policy.get("schema") != POLICY_SCHEMA:
        raise ActivationError("CLI concurrency policy is invalid")
    if hashlib.sha256(canonical(policy).encode()).hexdigest() != expected_hash:
        raise ActivationError("CLI concurrency policy digest does not match activation")
    if (not capacity(policy["coupled_max_concurrent"], 4) or
            not isinstance(policy.get("global"), dict) or
            not capacity(policy["global"].get("max_concurrent"), 4)):
        raise ActivationError("CLI concurrency global capacity must be at most four")
    account_maximum = {}
    for route in routes.values():
        family = policy.get("provider_families", {}).get(route["provider_family"], {})
        account = policy.get("account_routes", {}).get(route["account_route"], {})
        adapter_maximum = 4
        account_maximum[route["account_route"]] = min(
            account_maximum.get(route["account_route"], adapter_maximum),
            adapter_maximum,
        )
        if (not capacity(family.get("max_concurrent"), 4) or
                not capacity(
                    account.get("max_concurrent"),
                    account_maximum[route["account_route"]],
                )):
            raise ActivationError("CLI concurrency route capacity is unsafe")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--policy", type=Path)
    parser.add_argument(
        "--contract-version", required=True, choices=("1.6.0", "1.7.0", "1.8.0", "1.9.0")
    )
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--route-id")
    selection.add_argument("--status", action="store_true")
    args = parser.parse_args()
    output_schema = (
        OUTPUT_SCHEMA_V1 if args.contract_version == "1.6.0" else OUTPUT_SCHEMA_V2
    )
    try:
        if args.route_id is not None and not SAFE_ID.fullmatch(args.route_id):
            raise ActivationError("route id is invalid")
        path = args.config
        if not path.is_absolute():
            raise ActivationError("activation path must be absolute")
        raw, value = read_secure(path, "activation configuration", owner_only=True)
        common_invalid = (
            not isinstance(value, dict)
            or value.get("enabled") is not True
            or raw != canonical(value) + "\n"
            or not isinstance(value.get("routes"), dict)
        )
        if common_invalid:
            raise ActivationError("activation configuration is invalid")

        schema = value.get("schema")
        if schema == SCHEMA_V1:
            if set(value) != {"enabled", "routes", "schema"}:
                raise ActivationError("activation configuration is invalid")
            output_schema = OUTPUT_SCHEMA_V1
            route_validator = valid_v1_route
            execution_mode = "api-isolated-v1"
        elif schema == SCHEMA_V2:
            if args.contract_version not in {"1.7.0", "1.8.0", "1.9.0"}:
                raise ActivationError("contract 1.6 does not support CLI activation")
            if (
                set(value)
                != {"enabled", "mode", "policy_sha256", "routes", "schema"}
                or value.get("mode") != "cli-concurrent-v1"
                or not isinstance(value.get("policy_sha256"), str)
                or not SHA256.fullmatch(value["policy_sha256"])
            ):
                raise ActivationError("activation configuration is invalid")
            output_schema = OUTPUT_SCHEMA_V2
            route_validator = valid_v2_route
            execution_mode = "cli-concurrent-v1"
        else:
            raise ActivationError("activation configuration is invalid")

        if any(
            not isinstance(route_id, str)
            or not SAFE_ID.fullmatch(route_id)
            or not route_validator(configured_route)
            for route_id, configured_route in value["routes"].items()
        ):
            raise ActivationError("activated route is invalid")
        if schema == SCHEMA_V2:
            if args.policy is None:
                raise ActivationError("CLI concurrency policy is required")
            _, policy = read_secure(args.policy, "CLI concurrency policy")
            validate_cli_policy(policy, value["routes"], value["policy_sha256"])
        if args.status:
            print(
                canonical(
                    {
                        "execution_mode": execution_mode,
                        **(
                            {"policy_sha256": value["policy_sha256"]}
                            if schema == SCHEMA_V2
                            else {}
                        ),
                        "schema": output_schema,
                        "status": "enabled",
                    }
                )
            )
            return

        route = value["routes"].get(args.route_id)
        if route is None:
            raise ActivationError("route is not activated")
        print(
            canonical(
                {
                    **route,
                    **(
                        {
                            "execution_mode": "cli-concurrent-v1",
                            "policy_sha256": value["policy_sha256"],
                        }
                        if schema == SCHEMA_V2
                        else {}
                    ),
                    "route_id": args.route_id,
                    "schema": output_schema,
                    "status": "enabled",
                }
            )
        )
    except (
        ActivationError,
        FileNotFoundError,
        OSError,
        UnicodeError,
        json.JSONDecodeError,
    ) as error:
        print(
            canonical(
                {
                    "error": str(error),
                    "schema": output_schema,
                    "status": "disabled",
                }
            )
        )
        raise SystemExit(2)


if __name__ == "__main__":
    main()
