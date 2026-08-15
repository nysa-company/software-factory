#!/usr/bin/env python3
"""Drive one sealed qualification to its next deterministic boundary."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import time
from typing import Any


SCHEMA = "nysa.software-factory.qualification-run/v1"
CONTROLLER_SCHEMA = "nysa.software-factory.controller/v1"
DOCTOR_SCHEMA = "nysa.software-factory.doctor/v2"
REPORT_SCHEMA = "nysa.software-factory.qualification-report/v1"
PROJECT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


class QualificationRunError(RuntimeError):
    pass


def canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
    ).encode()


def launcher_path(path: Path) -> Path:
    if not path.is_absolute() or path.is_symlink():
        raise QualificationRunError("qualification launcher is unsafe")
    try:
        resolved = path.resolve(strict=True)
        info = resolved.stat()
    except OSError as error:
        raise QualificationRunError("qualification launcher is unavailable") from error
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_nlink != 1
        or info.st_mode & 0o022
        or not os.access(resolved, os.X_OK)
    ):
        raise QualificationRunError("qualification launcher is unsafe")
    return resolved


def invoke(
    launcher: Path, project: str, action: str, phases: list[dict[str, Any]],
) -> tuple[int, dict[str, Any]]:
    started_epoch_ms = time.time_ns() // 1_000_000
    started = time.monotonic()
    result = subprocess.run(
        [str(launcher), project, action, "--json"],
        capture_output=True, check=False, text=True,
    )
    phases.append({
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "name": action,
        "started_epoch_ms": started_epoch_ms,
    })
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise QualificationRunError(f"{action} returned invalid JSON") from error
    if not isinstance(value, dict):
        raise QualificationRunError(f"{action} returned invalid JSON")
    return result.returncode, value


def controller_result(value: dict[str, Any]) -> None:
    status = value.get("status")
    if value.get("schema") != CONTROLLER_SCHEMA or status not in {
        "busy", "error", "ok", "restart_required", "waiting_for_target",
    }:
        raise QualificationRunError("controller returned an invalid result")
    if status != "busy" and (
        not isinstance(value.get("active"), int)
        or isinstance(value.get("active"), bool)
        or value["active"] < 0
        or not isinstance(value.get("results"), list)
    ):
        raise QualificationRunError("controller returned an invalid result")


def report_result(value: dict[str, Any]) -> None:
    unsigned = dict(value)
    digest = unsigned.pop("report_sha256", "")
    if (
        value.get("schema") != REPORT_SCHEMA
        or value.get("status") != "green"
        or digest != hashlib.sha256(canonical(unsigned)).hexdigest()
    ):
        raise QualificationRunError("qualification reducer returned invalid evidence")


def execute(args: argparse.Namespace) -> dict[str, Any]:
    if not PROJECT.fullmatch(args.project):
        raise QualificationRunError("invalid qualification project")
    launcher = launcher_path(args.launcher)
    phases: list[dict[str, Any]] = []
    started = time.monotonic()
    code, doctor = invoke(launcher, args.project, "doctor", phases)
    if (
        code != 0 or doctor.get("schema") != DOCTOR_SCHEMA
        or doctor.get("overall_status") != "ok"
    ):
        return {
            "doctor_status": doctor.get("overall_status"),
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "phases": phases,
            "project": args.project,
            "reason": "doctor_not_ready",
            "restarts": 0,
            "schema": SCHEMA,
            "status": "blocked",
        }

    restarts = 0
    while True:
        code, controller = invoke(launcher, args.project, "reconcile", phases)
        controller_result(controller)
        if code != 0 or controller["status"] == "error":
            return {
                "controller": controller,
                "doctor_status": "ok",
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "phases": phases,
                "project": args.project,
                "reason": "controller_error",
                "restarts": restarts,
                "schema": SCHEMA,
                "status": "error",
            }
        if controller["status"] != "restart_required":
            break
        restarts += 1
        if restarts > 1:
            raise QualificationRunError("qualification restart did not converge")

    base = {
        "controller": controller,
        "doctor_status": "ok",
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "phases": phases,
        "project": args.project,
        "restarts": restarts,
        "schema": SCHEMA,
    }
    if controller["status"] == "busy":
        return {**base, "reason": "controller_busy", "status": "waiting"}
    if controller["status"] == "waiting_for_target":
        return {**base, "reason": "cohort_not_accounted", "status": "waiting"}

    if any(
        not isinstance(item, dict) or not isinstance(item.get("status"), str)
        for item in controller["results"]
    ):
        raise QualificationRunError("controller returned invalid ticket results")
    result_statuses = {item["status"] for item in controller["results"]}
    if result_statuses - {
        "active", "blocked", "budget", "cancelled", "complete",
        "maintenance", "waiting",
    }:
        raise QualificationRunError("controller returned an unknown ticket status")
    if "error" in result_statuses:
        raise QualificationRunError("controller reported a ticket error")
    if result_statuses & {"blocked", "budget", "cancelled", "maintenance"}:
        return {**base, "reason": "ticket_blocked", "status": "blocked"}
    if result_statuses - {"complete"} or controller["active"]:
        return {**base, "reason": "authenticated_wait", "status": "waiting"}

    code, report = invoke(launcher, args.project, "qualification", phases)
    if code != 0:
        raise QualificationRunError("qualification reduction failed")
    report_result(report)
    return {
        **base,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "report": report,
        "status": "green",
    }


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--launcher", required=True, type=Path)
    parser.add_argument("--project", required=True)
    parser.add_argument("--json", action="store_true", required=True)
    return parser.parse_args()


def main() -> int:
    try:
        result = execute(arguments())
        code = 0 if result["status"] == "green" else (
            2 if result["status"] == "error" else 3
        )
    except (OSError, QualificationRunError, subprocess.SubprocessError) as error:
        result = {"error": str(error), "schema": SCHEMA, "status": "error"}
        code = 2
    print(canonical(result).decode())
    return code


if __name__ == "__main__":
    raise SystemExit(main())
