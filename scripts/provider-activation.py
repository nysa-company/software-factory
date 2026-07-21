#!/usr/bin/env python3
"""Validate the owner-local isolated-v1 activation and select one fixed route."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import stat


SCHEMA = "nysa.software-factory.provider-activation/v1"
OUTPUT_SCHEMA = "nysa.software-factory.provider-activation-selection/v1"
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,199}$")


class ActivationError(ValueError):
    pass


def canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--route-id", required=True)
    args = parser.parse_args()
    try:
        path = args.config
        if not path.is_absolute():
            raise ActivationError("activation path must be absolute")
        info = path.lstat()
        if (
            path.is_symlink()
            or not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
            or info.st_mode & 0o077
            or info.st_size > 1_000_000
        ):
            raise ActivationError("activation configuration is unsafe")
        raw = path.read_text(encoding="utf-8")
        value = json.loads(raw)
        if (
            not isinstance(value, dict)
            or set(value) != {"enabled", "routes", "schema"}
            or value.get("schema") != SCHEMA
            or value.get("enabled") is not True
            or raw != canonical(value) + "\n"
            or not isinstance(value["routes"], dict)
        ):
            raise ActivationError("activation configuration is invalid")
        route = value["routes"].get(args.route_id)
        required = {
            "account_route", "broker_path", "model", "protocol",
            "provider_family",
        }
        if not isinstance(route, dict) or set(route) != required:
            raise ActivationError("route is not activated for isolated-v1")
        if (
            any(
                not isinstance(route[field], str)
                or not SAFE_ID.fullmatch(route[field])
                for field in ("account_route", "model", "provider_family")
            )
            or route["protocol"]
            not in ("openai-chat", "openai-responses", "anthropic-messages")
            or not isinstance(route["broker_path"], str)
            or not route["broker_path"].startswith("/")
            or route["broker_path"].startswith("//")
            or "?" in route["broker_path"]
            or "#" in route["broker_path"]
        ):
            raise ActivationError("activated route is invalid")
        print(
            canonical(
                {
                    **route,
                    "route_id": args.route_id,
                    "schema": OUTPUT_SCHEMA,
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
        print(canonical({"error": str(error), "schema": OUTPUT_SCHEMA, "status": "disabled"}))
        raise SystemExit(2)


if __name__ == "__main__":
    main()
