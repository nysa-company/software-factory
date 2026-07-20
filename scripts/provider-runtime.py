#!/usr/bin/env python3
"""Couple transactional admission to one isolated provider execution."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from typing import Any


OUTPUT_SCHEMA = "nysa.software-factory.provider-runtime/v1"
REQUEST_SCHEMA = "nysa.software-factory.provider-execution-request/v1"
MAX_JSON = 1_000_000


class RuntimeError(ValueError):
    pass


def canonical(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    )


def read_json(path: Path, label: str) -> dict[str, Any]:
    if not path.is_absolute():
        raise RuntimeError(f"{label} path must be absolute")
    before = path.lstat()
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_uid != os.geteuid()
        or before.st_mode & 0o022
        or before.st_size > MAX_JSON
    ):
        raise RuntimeError(f"{label} is unsafe")
    raw = path.read_bytes()
    after = path.lstat()
    if (before.st_dev, before.st_ino, before.st_size) != (
        after.st_dev, after.st_ino, after.st_size
    ):
        raise RuntimeError(f"{label} changed while reading")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"{label} is invalid JSON") from error
    if not isinstance(value, dict):
        raise RuntimeError(f"{label} must contain an object")
    return value


def policy_hash(path: Path) -> str:
    value = read_json(path, "provider policy")
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def command_json(command: list[str], label: str) -> dict[str, Any]:
    result = subprocess.run(
        command, text=True, capture_output=True, check=False, timeout=1200
    )
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError(
            f"{label} returned invalid JSON: {result.stderr.strip()}"
        ) from error
    if result.returncode:
        message = value.get("error") if isinstance(value, dict) else None
        raise RuntimeError(f"{label} failed: {message or result.stderr.strip()}")
    if not isinstance(value, dict):
        raise RuntimeError(f"{label} returned a non-object")
    return value


def coordinator(args: argparse.Namespace, *arguments: str) -> dict[str, Any]:
    return command_json(
        [
            sys.executable, str(args.coordinator), "--db", str(args.db),
            *arguments,
        ],
        "provider coordinator",
    )


def operation(attempt_id: str, step: str) -> str:
    return f"{attempt_id}:{step}"


def execute(args: argparse.Namespace) -> dict[str, Any]:
    request = read_json(args.request, "execution request")
    if request.get("schema") != REQUEST_SCHEMA:
        raise RuntimeError("execution request schema is unsupported")
    attempt_id = request.get("attempt_id")
    if not isinstance(attempt_id, str):
        raise RuntimeError("execution request attempt_id is invalid")
    expected_policy = policy_hash(args.policy)
    if request.get("policy_sha256") != expected_policy:
        raise RuntimeError("execution request is not bound to the active provider policy")

    reservation = coordinator(
        args,
        "reserve",
        "--operation-id", operation(attempt_id, "reserve"),
        "--attempt-id", attempt_id,
        "--provider-family", args.provider_family,
        "--account-route", args.account_route,
        "--reserve-micro-usd", str(args.reserve_micro_usd),
        "--policy", str(args.policy),
    )
    if not reservation.get("admitted"):
        return {
            "admitted": False,
            "attempt_id": attempt_id,
            "denials": reservation.get("denials", []),
            "schema": OUTPUT_SCHEMA,
        }
    coordinator(
        args,
        "mark-go",
        "--operation-id", operation(attempt_id, "go"),
        "--attempt-id", attempt_id,
        "--expected-version", "2",
    )
    coordinator(
        args,
        "mark-submitted",
        "--operation-id", operation(attempt_id, "submitted"),
        "--attempt-id", attempt_id,
        "--expected-version", "3",
    )

    executor_command = [
        sys.executable,
        str(args.executor),
        "--runtime", args.container_runtime,
        "--attempt-root", str(args.attempt_root),
        "--runtime-timeout", str(args.runtime_timeout),
        "execute",
        "--mode", "isolated-v1",
        "--request", str(args.request),
        "--timeout", str(args.timeout),
        "--memory", args.memory,
        "--cpus", str(args.cpus),
        "--pids-limit", str(args.pids_limit),
    ]
    execution = command_json(executor_command, "provider executor")
    terminal_result = "succeeded" if execution.get("return_code") == 0 else "failed"
    terminal = coordinator(
        args,
        "terminalize",
        "--operation-id", operation(attempt_id, "terminal"),
        "--attempt-id", attempt_id,
        "--expected-version", "4",
        "--result", terminal_result,
    )
    return {
        "admitted": True,
        "attempt_id": attempt_id,
        "execution": execution,
        "schema": OUTPUT_SCHEMA,
        "terminal": terminal,
    }


def cancel(args: argparse.Namespace) -> dict[str, Any]:
    status = coordinator(args, "status", "--attempt-id", args.attempt_id)
    attempts = status.get("attempts")
    if not isinstance(attempts, list) or len(attempts) != 1:
        raise RuntimeError("coordinator returned an invalid attempt status")
    attempt = attempts[0]
    if attempt.get("state") == "terminal":
        return {
            "attempt_id": args.attempt_id,
            "cancelled": attempt.get("terminal_result") == "cancelled",
            "schema": OUTPUT_SCHEMA,
            "terminal": attempt,
        }
    if attempt.get("state") != "submitted":
        raise RuntimeError(
            f"attempt cancellation requires submitted state, got {attempt.get('state')}"
        )
    executor_command = [
        sys.executable,
        str(args.executor),
        "--runtime", args.container_runtime,
        "--attempt-root", str(args.attempt_root),
        "--runtime-timeout", str(args.runtime_timeout),
        "cancel",
        "--attempt-id", args.attempt_id,
    ]
    if args.binding_sha256:
        executor_command.extend(["--binding-sha256", args.binding_sha256])
    cancellation = command_json(executor_command, "provider executor cancellation")
    if cancellation.get("removed") is not True:
        raise RuntimeError(
            "container termination was not proven; reservation and slot retained"
        )
    terminal = coordinator(
        args,
        "terminalize",
        "--operation-id", operation(args.attempt_id, "cancelled"),
        "--attempt-id", args.attempt_id,
        "--expected-version", str(attempt["version"]),
        "--result", "cancelled",
    )
    return {
        "attempt_id": args.attempt_id,
        "cancellation": cancellation,
        "cancelled": True,
        "schema": OUTPUT_SCHEMA,
        "terminal": terminal,
    }


def parser() -> argparse.ArgumentParser:
    root = Path(__file__).resolve().parent
    value = argparse.ArgumentParser()
    value.add_argument("--db", required=True, type=Path)
    value.add_argument("--policy", required=True, type=Path)
    value.add_argument(
        "--coordinator", type=Path, default=root / "provider-coordinator.py"
    )
    value.add_argument("--executor", type=Path, default=root / "provider-executor.py")
    value.add_argument("--container-runtime", default="docker")
    command = value.add_subparsers(dest="command", required=True)
    run = command.add_parser("execute")
    run.add_argument("--request", required=True, type=Path)
    run.add_argument("--attempt-root", required=True, type=Path)
    run.add_argument("--provider-family", required=True)
    run.add_argument("--account-route", required=True)
    run.add_argument("--reserve-micro-usd", required=True, type=int)
    run.add_argument("--runtime-timeout", type=float, default=30)
    run.add_argument("--timeout", type=float, default=900)
    run.add_argument("--memory", default="1g")
    run.add_argument("--cpus", type=float, default=1)
    run.add_argument("--pids-limit", type=int, default=128)
    run.set_defaults(handler=execute)
    cancellation = command.add_parser("cancel")
    cancellation.add_argument("--attempt-id", required=True)
    cancellation.add_argument("--attempt-root", required=True, type=Path)
    cancellation.add_argument("--binding-sha256")
    cancellation.add_argument("--runtime-timeout", type=float, default=30)
    cancellation.set_defaults(handler=cancel)
    return value


def main() -> None:
    try:
        args = parser().parse_args()
        result = args.handler(args)
        print(canonical(result))
    except (
        RuntimeError, OSError, subprocess.SubprocessError, ValueError
    ) as error:
        print(canonical({
            "error": str(error), "schema": OUTPUT_SCHEMA, "status": "error",
        }))
        raise SystemExit(2)


if __name__ == "__main__":
    main()
