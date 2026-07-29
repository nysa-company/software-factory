#!/usr/bin/env python3
"""Run one subscription CLI behind the existing provider coordinator."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import signal
import subprocess
import sys
from typing import Any


class CliRuntimeError(RuntimeError):
    pass


def coordinator(args: argparse.Namespace, action: str, *values: str) -> dict[str, Any]:
    result = subprocess.run(
        [sys.executable, str(args.coordinator), "--db", str(args.db), action, *values],
        text=True,
        capture_output=True,
        check=False,
        timeout=60,
    )
    if result.returncode:
        raise CliRuntimeError(result.stderr.strip() or result.stdout.strip() or "provider coordinator failed")
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise CliRuntimeError("provider coordinator returned invalid JSON") from error
    if not isinstance(value, dict):
        raise CliRuntimeError("provider coordinator returned invalid output")
    return value


def operation(attempt: str, action: str) -> str:
    return f"{attempt}-{action}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--coordinator", required=True, type=Path)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--policy", required=True, type=Path)
    parser.add_argument("--attempt-id", required=True)
    parser.add_argument("--provider-family", required=True)
    parser.add_argument("--account-route", required=True)
    parser.add_argument("--reserve-micro-usd", required=True, type=int)
    parser.add_argument("--product-id", required=True)
    parser.add_argument("--ticket-id", required=True)
    parser.add_argument("--budget-day", required=True)
    parser.add_argument("--product-cap-micro-usd", required=True, type=int)
    parser.add_argument("--ticket-cap-micro-usd", required=True, type=int)
    parser.add_argument("--machine-cap-micro-usd", required=True, type=int)
    parser.add_argument("--pre-reserved", action="store_true")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        raise SystemExit("subscription CLI command is missing")

    attempt = args.attempt_id
    if not args.pre_reserved:
        reservation = coordinator(
            args,
            "reserve",
            "--operation-id", operation(attempt, "reserve"),
            "--attempt-id", attempt,
            "--provider-family", args.provider_family,
            "--account-route", args.account_route,
            "--reserve-micro-usd", str(args.reserve_micro_usd),
            "--product-id", args.product_id,
            "--ticket-id", args.ticket_id,
            "--budget-day", args.budget_day,
            "--product-daily-cap-micro-usd", str(args.product_cap_micro_usd),
            "--ticket-cap-micro-usd", str(args.ticket_cap_micro_usd),
            "--machine-daily-cap-micro-usd", str(args.machine_cap_micro_usd),
            "--policy", str(args.policy),
        )
        if reservation.get("admitted") is not True:
            coordinator(
                args, "terminalize",
                "--operation-id", operation(attempt, "capacity-denied"),
                "--attempt-id", attempt,
                "--expected-version", "1",
                "--result", "capacity_denied",
                "--charge-micro-usd", "0",
            )
            print("subscription CLI concurrency capacity refused", file=sys.stderr)
            return 8

    coordinator(
        args, "mark-go",
        "--operation-id", operation(attempt, "go"),
        "--attempt-id", attempt,
        "--expected-version", "2",
    )
    coordinator(
        args, "mark-submitted",
        "--operation-id", operation(attempt, "submitted"),
        "--attempt-id", attempt,
        "--expected-version", "3",
    )

    cancelled = False

    def cancel(_signum: int, _frame: object) -> None:
        nonlocal cancelled
        cancelled = True

    prior = {selected: signal.signal(selected, cancel) for selected in (signal.SIGINT, signal.SIGTERM)}
    try:
        completed = subprocess.run(command, check=False)
    finally:
        for selected, handler in prior.items():
            signal.signal(selected, handler)

    # The trusted host owns terminalization after process-group drainage,
    # output validation, remote checks, and any trusted push.
    if cancelled:
        return 130
    return completed.returncode


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (CliRuntimeError, OSError, subprocess.SubprocessError, ValueError) as error:
        print(f"provider-cli-runtime: {error}", file=sys.stderr)
        raise SystemExit(2)
