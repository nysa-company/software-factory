"""Strict certification-plan and runtime-tuple validation."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
from typing import Any


PLAN_SCHEMA = "nysa.software-factory.certification-plan/v2"
PREFLIGHT_SCHEMA = "nysa.software-factory.certification-preflight/v1"
NAME = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
SHA = re.compile(r"^[0-9a-f]{40}$")
NODE = re.compile(r"^v[1-9][0-9]*\.[0-9]+\.[0-9]+$")
NPM = re.compile(r"^[1-9][0-9]*\.[0-9]+\.[0-9]+$")
CONTRACT = re.compile(r"^[1-9][0-9]*\.[0-9]+\.[0-9]+$")
TUPLE_KEYS = (
    "factory_sha", "factory_tree", "product_sha", "product_tree",
    "contract_version", "node", "npm",
)


class PlanError(ValueError):
    pass


class TupleError(ValueError):
    def __init__(
        self, reason_code: str, field: str, expected: str, actual: str,
    ) -> None:
        super().__init__(f"{reason_code}: {field}")
        self.diagnostic = {
            "actual": actual,
            "expected": expected,
            "field": field,
            "reason_code": reason_code,
        }


def canonical(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode()


def safe_plan(path: Path) -> tuple[dict[str, Any], str]:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_size > 1_000_000
        ):
            raise PlanError("certification plan is unsafe")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            raw = stream.read()
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    value = json.loads(raw)
    if not isinstance(value, dict) or set(value) != {"schema", "phases", "runtime"}:
        raise PlanError("certification plan is malformed")
    runtime = value.get("runtime")
    if (
        value["schema"] != PLAN_SCHEMA
        or not isinstance(value["phases"], list)
        or not isinstance(runtime, dict)
        or set(runtime) != {"node", "npm"}
        or not NODE.fullmatch(runtime.get("node", ""))
        or not NPM.fullmatch(runtime.get("npm", ""))
    ):
        raise PlanError("certification plan schema is invalid")
    return value, hashlib.sha256(raw).hexdigest()


def validate_plan(plan: dict[str, Any], root: Path) -> dict[str, dict[str, Any]]:
    phases: dict[str, dict[str, Any]] = {}
    for phase in plan["phases"]:
        if not isinstance(phase, dict) or set(phase) not in ({
            "artifacts", "command", "depends_on", "name", "network"
        }, {
            "artifacts", "command", "depends_on", "name", "network", "reuse"
        }, {
            "artifacts", "command", "depends_on", "kind", "name", "network",
            "reuse"
        }):
            raise PlanError("certification phase is malformed")
        name = phase["name"]
        command = phase["command"]
        dependencies = phase["depends_on"]
        artifacts = phase["artifacts"]
        network = phase["network"]
        reuse = phase.get("reuse", "never")
        kind = phase.get("kind")
        if (
            not isinstance(name, str)
            or not NAME.fullmatch(name)
            or name in phases
            or not isinstance(command, list)
            or not command
            or not all(isinstance(item, str) and item for item in command)
            or not isinstance(dependencies, list)
            or not all(
                isinstance(item, str) and NAME.fullmatch(item)
                for item in dependencies
            )
            or len(set(dependencies)) != len(dependencies)
            or not isinstance(artifacts, list)
            or not all(isinstance(item, str) and item for item in artifacts)
            or network not in {"denied", "optional", "required"}
            or reuse not in {"never", "artifacts"}
            or (reuse == "artifacts" and not artifacts)
            or (reuse == "artifacts" and kind not in {"build", "dependencies"})
            or (reuse != "artifacts" and kind is not None)
        ):
            raise PlanError("certification phase values are invalid")
        for artifact in artifacts:
            candidate = root / artifact
            if (
                Path(artifact).is_absolute()
                or ".." in Path(artifact).parts
                or os.path.commonpath((root, candidate.resolve(strict=False)))
                != str(root)
            ):
                raise PlanError("certification artifact escapes the product")
        if reuse == "artifacts":
            normalized = [Path(item).parts for item in artifacts]
            if any(
                left == right
                or left[:len(right)] == right
                or right[:len(left)] == left
                for index, left in enumerate(normalized)
                for right in normalized[index + 1:]
            ):
                raise PlanError("reusable certification artifacts overlap")
        phases[name] = phase
    if not phases:
        raise PlanError("certification plan has no phases")
    for phase in phases.values():
        if any(dependency not in phases for dependency in phase["depends_on"]):
            raise PlanError("certification dependency is unknown")
    remaining = set(phases)
    resolved: set[str] = set()
    while remaining:
        ready = {
            name
            for name in remaining
            if set(phases[name]["depends_on"]).issubset(resolved)
        }
        if not ready:
            raise PlanError("certification dependency graph contains a cycle")
        resolved.update(ready)
        remaining -= ready
    return phases


def strict_tuple(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        raise TupleError(
            "runtime_tuple_invalid", "runtime_tuple", "object", "invalid",
        )
    missing = sorted(set(TUPLE_KEYS) - set(value))
    unknown = sorted(set(value) - set(TUPLE_KEYS))
    if missing:
        raise TupleError(
            "runtime_tuple_missing", missing[0], "present", "missing",
        )
    if unknown:
        raise TupleError(
            "runtime_tuple_unknown", unknown[0], "absent", "present",
        )
    validators = {
        "factory_sha": SHA,
        "factory_tree": SHA,
        "product_sha": SHA,
        "product_tree": SHA,
        "contract_version": CONTRACT,
        "node": NODE,
        "npm": NPM,
    }
    for field in TUPLE_KEYS:
        item = value[field]
        if not isinstance(item, str) or not validators[field].fullmatch(item):
            raise TupleError(
                "runtime_tuple_invalid", field, "valid exact value", "invalid",
            )
    return {field: value[field] for field in TUPLE_KEYS}


def tool_version(command: list[str]) -> str:
    try:
        return subprocess.run(
            command, text=True, capture_output=True, check=False
        ).stdout.strip()
    except OSError:
        return ""


def observed_tuple(identity: dict[str, str]) -> dict[str, str]:
    value = {
        **identity,
        "node": tool_version(["node", "--version"]),
        "npm": tool_version(["npm", "--version"]),
    }
    return {field: value.get(field, "") for field in TUPLE_KEYS}


def expected_tuple(identity: dict[str, str], plan: dict[str, Any]) -> dict[str, str]:
    return strict_tuple({**identity, **plan["runtime"]})


def compare_tuple(expected: dict[str, str], actual: dict[str, str]) -> None:
    for field in TUPLE_KEYS:
        if actual.get(field) != expected[field]:
            actual_value = actual.get(field)
            raise TupleError(
                "runtime_tuple_mismatch",
                field,
                expected[field],
                actual_value if isinstance(actual_value, str) and actual_value else "missing",
            )


def git_identity(root: Path) -> tuple[str, str]:
    def git(revision: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", revision],
            text=True, capture_output=True, check=False,
        )
        if result.returncode:
            raise TupleError(
                "runtime_tuple_invalid", "product_sha", "Git identity", "missing",
            )
        return result.stdout.strip()

    sha, tree = git("HEAD"), git("HEAD^{tree}")
    if not SHA.fullmatch(sha) or not SHA.fullmatch(tree):
        raise TupleError(
            "runtime_tuple_invalid", "product_sha", "Git identity", "invalid",
        )
    return sha, tree


def diagnostic(error: Exception) -> dict[str, Any]:
    if isinstance(error, TupleError):
        failure = error.diagnostic
    else:
        failure = {
            "actual": "invalid",
            "expected": "valid certification-plan/v2",
            "field": "certification_plan",
            "reason_code": "certification_plan_invalid",
        }
    return {"failure": failure, "schema": PREFLIGHT_SCHEMA, "status": "fail"}
