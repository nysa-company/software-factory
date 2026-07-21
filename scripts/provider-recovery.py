#!/usr/bin/env python3
"""Redacted status, reconciliation, and rollback gates for isolated-v1."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import time
from typing import Any

SCHEMA = "nysa.software-factory.provider-recovery/v1"
ACTIVE = frozenset(("reserved", "GO", "submitted"))


class RecoveryError(ValueError):
    pass


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def command_json(command: list[str], label: str, allow_failure: bool = False):
    result = subprocess.run(
        command, text=True, capture_output=True, check=False, timeout=120
    )
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        if allow_failure:
            return None
        raise RecoveryError(f"{label} returned invalid JSON") from error
    if result.returncode and not allow_failure:
        raise RecoveryError(f"{label} failed")
    return value


def collect(args: argparse.Namespace) -> dict[str, Any]:
    coordinator = command_json(
        [
            sys.executable, str(args.coordinator), "--db", str(args.db), "status",
        ],
        "provider coordinator",
    )
    attempts = coordinator.get("attempts")
    if not isinstance(attempts, list):
        raise RecoveryError("provider coordinator returned invalid status")
    workers = []
    for attempt in attempts:
        if not isinstance(attempt, dict) or attempt.get("state") not in ACTIVE:
            continue
        observed = command_json(
            [
                sys.executable, str(args.executor),
                "--runtime", args.container_runtime,
                "--attempt-root", str(args.attempt_root),
                "status", "--attempt-id", str(attempt.get("attempt_id", "")),
            ],
            "provider executor status",
            allow_failure=True,
        )
        if not isinstance(observed, dict) or observed.get("status") == "error":
            observed = {
                "attempt_id": attempt.get("attempt_id"),
                "container_exists": None,
                "container_running": None,
                "observation": "unknown",
            }
        workers.append(observed)
    broker = command_json(
        [
            sys.executable, str(args.credential_broker),
            "--db", str(args.broker_db),
            "--credentials", str(args.broker_credentials),
            "status",
        ],
        "provider credential broker",
        allow_failure=True,
    )
    broker_observed = isinstance(broker, dict) and broker.get("status") != "error"
    if not broker_observed:
        broker = {"status": "unknown", "tokens": []}
    tokens = broker.get("tokens")
    if not isinstance(tokens, list):
        broker_observed = False
        tokens = []
    return {
        "active_reserve_micro_usd": coordinator.get("active_reserve_micro_usd"),
        "attempts": attempts,
        "counts": coordinator.get("counts", {}),
        "legacy_intervals": coordinator.get("legacy_intervals", []),
        "broker_observed": broker_observed,
        "schema": SCHEMA,
        "tokens": tokens,
        "workers": workers,
    }


def status(args: argparse.Namespace) -> dict[str, Any]:
    value = collect(args)
    unknown = sum(
        1 for worker in value["workers"] if worker.get("container_exists") is None
    )
    value.update(
        {
            "active_tokens": sum(
                1 for token in value["tokens"] if token.get("active") is True
            ),
            "health": "error" if unknown or not value["broker_observed"] else "ok",
            "status": "observed",
            "unknown_workers": unknown,
        }
    )
    return value


def reconcile_plan(args: argparse.Namespace) -> dict[str, Any]:
    value = collect(args)
    workers = {
        item.get("attempt_id"): item
        for item in value["workers"]
        if isinstance(item, dict)
    }
    actions = []
    for attempt in value["attempts"]:
        state = attempt.get("state")
        if state not in ACTIVE:
            continue
        worker = workers.get(attempt.get("attempt_id"), {})
        exists = worker.get("container_exists")
        running = worker.get("container_running")
        if exists is True and running is True:
            disposition = "active_no_action"
        elif state in ("GO", "submitted"):
            disposition = "unknown_retain_reservation_and_slot"
        elif exists is False:
            disposition = "pre_go_operator_terminalization_required"
        else:
            disposition = "unknown_retain_reservation_and_slot"
        actions.append(
            {
                "attempt_id": attempt.get("attempt_id"),
                "container_exists": exists,
                "container_running": running,
                "disposition": disposition,
                "state": state,
            }
        )
    return {"actions": actions, "schema": SCHEMA, "status": "plan"}


def rollback_check(args: argparse.Namespace) -> dict[str, Any]:
    value = collect(args)
    blockers = []
    if any(item.get("state") in ACTIVE for item in value["attempts"]):
        blockers.append("active_or_unknown_attempts")
    if value["legacy_intervals"]:
        blockers.append("legacy_intervals")
    if any(item.get("active") is True for item in value["tokens"]):
        blockers.append("active_broker_tokens")
    if not value["broker_observed"]:
        blockers.append("broker_unobservable")
    if any(
        item.get("container_exists") is not False for item in value["workers"]
    ):
        blockers.append("live_or_unknown_workers")
    return {
        "blockers": blockers,
        "safe_to_disable": not blockers,
        "schema": SCHEMA,
        "status": "rollback-check",
    }


def disable(args: argparse.Namespace) -> dict[str, Any]:
    if not rollback_check(args)["safe_to_disable"]:
        raise RecoveryError("rollback is blocked by active or unknown provider state")
    path = args.activation
    before = path.lstat()
    if (
        not path.is_absolute()
        or path.is_symlink()
        or not stat.S_ISREG(before.st_mode)
        or before.st_uid != os.geteuid()
        or before.st_nlink != 1
        or before.st_mode & 0o077
    ):
        raise RecoveryError("activation configuration is unsafe")
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != args.expected_sha256:
        raise RecoveryError("activation configuration changed")
    destination = path.with_name(
        f"{path.name}.disabled.{int(time.time())}.{digest[:12]}"
    )
    os.rename(path, destination)
    return {
        "activation_sha256": digest,
        "evidence_path": str(destination),
        "schema": SCHEMA,
        "status": "disabled",
    }


def parser() -> argparse.ArgumentParser:
    root = Path(__file__).resolve().parent
    value = argparse.ArgumentParser()
    value.add_argument("--db", required=True, type=Path)
    value.add_argument("--broker-db", required=True, type=Path)
    value.add_argument("--broker-credentials", required=True, type=Path)
    value.add_argument("--attempt-root", required=True, type=Path)
    value.add_argument("--container-runtime", default="docker")
    value.add_argument(
        "--coordinator", type=Path, default=root / "provider-coordinator.py"
    )
    value.add_argument(
        "--executor", type=Path, default=root / "provider-executor.py"
    )
    value.add_argument(
        "--credential-broker",
        type=Path,
        default=root / "provider-credential-broker.py",
    )
    commands = value.add_subparsers(dest="command", required=True)
    commands.add_parser("status").set_defaults(handler=status)
    commands.add_parser("reconcile-plan").set_defaults(handler=reconcile_plan)
    commands.add_parser("rollback-check").set_defaults(handler=rollback_check)
    disabled = commands.add_parser("disable")
    disabled.add_argument("--activation", required=True, type=Path)
    disabled.add_argument("--expected-sha256", required=True)
    disabled.set_defaults(handler=disable)
    return value


def main() -> None:
    try:
        args = parser().parse_args()
        print(canonical(args.handler(args)))
    except (
        RecoveryError,
        OSError,
        UnicodeError,
        subprocess.SubprocessError,
        ValueError,
    ) as error:
        print(canonical({"error": str(error), "schema": SCHEMA, "status": "error"}))
        raise SystemExit(2)


if __name__ == "__main__":
    main()
