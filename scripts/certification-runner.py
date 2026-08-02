#!/usr/bin/env python3
"""Run a measured product-certification DAG with bounded parallelism."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any


SCHEMA = "nysa.software-factory.certification-plan/v2"
RESULT_SCHEMA = "nysa.software-factory.certification-result/v1"
NAME = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
SHA = re.compile(r"^[0-9a-f]{40}$")


class PlanError(ValueError):
    pass


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
        value["schema"] != SCHEMA
        or not isinstance(value["phases"], list)
        or not isinstance(runtime, dict)
        or set(runtime) != {"node", "npm"}
        or not re.fullmatch(r"v[1-9][0-9]*\.[0-9]+\.[0-9]+", runtime.get("node", ""))
        or not re.fullmatch(r"[1-9][0-9]*\.[0-9]+\.[0-9]+", runtime.get("npm", ""))
    ):
        raise PlanError("certification plan schema is invalid")
    return value, hashlib.sha256(raw).hexdigest()


def validate_plan(plan: dict[str, Any], root: Path) -> dict[str, dict[str, Any]]:
    phases: dict[str, dict[str, Any]] = {}
    for phase in plan["phases"]:
        if not isinstance(phase, dict) or set(phase) != {
            "artifacts", "command", "depends_on", "name", "network"
        }:
            raise PlanError("certification phase is malformed")
        name = phase["name"]
        command = phase["command"]
        dependencies = phase["depends_on"]
        artifacts = phase["artifacts"]
        network = phase["network"]
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


def artifact_digest(root: Path, paths: list[str], log: Path) -> str:
    selected: list[tuple[str, bytes]] = []
    if not paths:
        selected.append(("log", log.read_bytes()))
    for relative in paths:
        path = root / relative
        if not path.exists() and not path.is_symlink():
            raise PlanError(f"certification artifact is missing: {relative}")
        candidates = [path]
        if path.is_dir() and not path.is_symlink():
            candidates = sorted(
                (item for item in path.rglob("*") if not item.is_dir()),
                key=lambda item: item.as_posix(),
            )
        for item in candidates:
            info = item.lstat()
            if item.is_symlink() or not stat.S_ISREG(info.st_mode):
                raise PlanError(f"certification artifact is unsafe: {relative}")
            selected.append((item.relative_to(root).as_posix(), item.read_bytes()))
    digest = hashlib.sha256()
    for name, raw in selected:
        digest.update(name.encode())
        digest.update(b"\0")
        digest.update(hashlib.sha256(raw).digest())
    return digest.hexdigest()


def atomic_result(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(canonical(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        Path(temporary).unlink(missing_ok=True)


def iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def tool_version(command: list[str]) -> str:
    try:
        return subprocess.run(
            command, text=True, capture_output=True, check=False
        ).stdout.strip()
    except OSError:
        return ""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--result", required=True, type=Path)
    parser.add_argument("--workers", type=int, default=2)
    args = parser.parse_args()
    root = Path.cwd().resolve(strict=True)
    factory_sha = os.environ.get("FACTORY_KIT_SHA", "")
    product_tree = os.environ.get("FACTORY_PRODUCT_TREE", "")
    network_reviewed = os.environ.get("FACTORY_CERTIFICATION_NETWORK_REVIEWED", "0")
    if (
        not 1 <= args.workers <= 3
        or not SHA.fullmatch(factory_sha)
        or not SHA.fullmatch(product_tree)
        or not args.result.is_absolute()
        or network_reviewed not in {"0", "1"}
    ):
        print("invalid certification runner boundary", file=sys.stderr)
        return 2
    try:
        plan, plan_digest = safe_plan(args.plan)
        phases = validate_plan(plan, root)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(str(error), file=sys.stderr)
        return 2

    runtime = {
        "node": tool_version(["node", "--version"]),
        "npm": tool_version(["npm", "--version"]),
    }
    missing_network = sorted(
        name for name, phase in phases.items()
        if phase["network"] == "required" and network_reviewed != "1"
    )
    reason = (
        "runtime_identity_mismatch"
        if runtime != plan["runtime"]
        else "reviewed_network_required" if missing_network else ""
    )
    if reason:
        observed = time.time()
        atomic_result(args.result, {
            "ended_at": iso(observed),
            "factory_sha": factory_sha,
            "failure": {"phases": missing_network, "reason_code": reason},
            "max_workers": args.workers,
            "network_reviewed": network_reviewed == "1",
            "phases": [],
            "plan_sha256": plan_digest,
            "product_tree": product_tree,
            "runtime": runtime,
            "schema": RESULT_SCHEMA,
            "started_at": iso(observed),
            "status": "fail",
            "wall_seconds": 0,
        })
        print(f"certification preflight failed: {reason}", file=sys.stderr)
        return 2

    run_root = args.result.parent / "certification-phases"
    run_root.mkdir(mode=0o700, parents=True, exist_ok=False)
    started = time.time()
    pending = set(phases)
    running: dict[int, dict[str, Any]] = {}
    completed: dict[str, dict[str, Any]] = {}
    failed = False
    failure_log: Path | None = None

    def launch(name: str) -> None:
        phase = phases[name]
        phase_root = run_root / name
        phase_root.mkdir(mode=0o700)
        log = phase_root / "output.log"
        stream = log.open("wb")
        dependencies = {
            item: completed[item]["artifact_sha256"]
            for item in phase["depends_on"]
        }
        input_digest = hashlib.sha256(
            canonical(
                {
                    "dependencies": dependencies,
                    "factory_sha": factory_sha,
                    "phase": phase,
                    "plan_sha256": plan_digest,
                    "product_tree": product_tree,
                    "runtime": runtime,
                    "network": {
                        "declared": phase["network"],
                        "granted": (
                            network_reviewed == "1"
                            and phase["network"] in {"optional", "required"}
                        ),
                    },
                }
            )
        ).hexdigest()
        environment = os.environ.copy()
        environment["TMPDIR"] = str(phase_root / "tmp")
        Path(environment["TMPDIR"]).mkdir(mode=0o700)
        granted = (
            network_reviewed == "1"
            and phase["network"] in {"optional", "required"}
        )
        command = list(phase["command"])
        deny_prefix = os.environ.get("FACTORY_CERTIFICATION_NETWORK_DENY_PREFIX", "")
        if not granted and deny_prefix:
            try:
                prefix = json.loads(deny_prefix)
            except json.JSONDecodeError as error:
                raise PlanError("certification network deny prefix is invalid") from error
            if not isinstance(prefix, list) or not all(
                isinstance(item, str) and item for item in prefix
            ):
                raise PlanError("certification network deny prefix is invalid")
            command = prefix + command
        process = subprocess.Popen(
            command,
            cwd=root,
            env=environment,
            stdout=stream,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        running[process.pid] = {
            "input_sha256": input_digest,
            "log": log,
            "name": name,
            "network_declared": phase["network"],
            "network_granted": granted,
            "process": process,
            "started": time.time(),
            "stream": stream,
        }
        pending.remove(name)

    try:
        while pending or running:
            if not failed:
                ready = sorted(
                    name
                    for name in pending
                    if set(phases[name]["depends_on"]).issubset(completed)
                )
                while ready and len(running) < args.workers:
                    launch(ready.pop(0))
            if not running:
                break
            pid, status, usage = os.wait4(-1, 0)
            active = running.pop(pid)
            active["stream"].close()
            process = active["process"]
            process.returncode = os.waitstatus_to_exitcode(status)
            ended = time.time()
            if (
                process.returncode != 0
                and phases[active["name"]]["command"][0] == "npm"
            ):
                logs = Path(os.environ.get("npm_config_cache", "")) / "_logs"
                with active["log"].open("ab") as stream:
                    for debug in (
                        sorted(logs.glob("*-debug-0.log")) if logs.is_dir() else []
                    ):
                        stream.write(f"\n--- preserved {debug.name} ---\n".encode())
                        stream.write(debug.read_bytes()[:1_000_000])
            try:
                artifact = artifact_digest(
                    root, phases[active["name"]]["artifacts"], active["log"]
                )
            except (OSError, PlanError) as error:
                artifact = ""
                process.returncode = process.returncode or 125
                with active["log"].open("ab") as stream:
                    stream.write((f"\n{error}\n").encode())
            peak = (
                int(usage.ru_maxrss / 1024)
                if sys.platform == "darwin"
                else int(usage.ru_maxrss)
            )
            completed[active["name"]] = {
                "artifact_sha256": artifact,
                "cache_hit": False,
                "command": phases[active["name"]]["command"],
                "ended_at": iso(ended),
                "exit_status": process.returncode,
                "input_sha256": active["input_sha256"],
                "name": active["name"],
                "network_declared": active["network_declared"],
                "network_granted": active["network_granted"],
                "output_sha256": hashlib.sha256(active["log"].read_bytes()).hexdigest(),
                "peak_memory_kb": peak,
                "started_at": iso(active["started"]),
                "system_cpu_seconds": round(usage.ru_stime, 6),
                "user_cpu_seconds": round(usage.ru_utime, 6),
                "wall_seconds": round(ended - active["started"], 6),
            }
            if process.returncode != 0:
                if not failed:
                    failure_log = active["log"]
                failed = True
                for sibling in running.values():
                    try:
                        os.killpg(sibling["process"].pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
        if failed:
            for name in sorted(pending):
                completed[name] = {
                    "artifact_sha256": "",
                    "cache_hit": False,
                    "command": phases[name]["command"],
                    "ended_at": None,
                    "exit_status": None,
                    "input_sha256": "",
                    "name": name,
                    "network_declared": phases[name]["network"],
                    "network_granted": False,
                    "output_sha256": "",
                    "peak_memory_kb": 0,
                    "started_at": None,
                    "system_cpu_seconds": 0,
                    "user_cpu_seconds": 0,
                    "wall_seconds": 0,
                }
    finally:
        for active in running.values():
            active["stream"].close()

    ended = time.time()
    value = {
        "ended_at": iso(ended),
        "factory_sha": factory_sha,
        "max_workers": args.workers,
        "network_reviewed": network_reviewed == "1",
        "phases": [completed[name] for name in sorted(completed)],
        "plan_sha256": plan_digest,
        "product_tree": product_tree,
        "runtime": runtime,
        "schema": RESULT_SCHEMA,
        "started_at": iso(started),
        "status": "fail" if failed else "pass",
        "wall_seconds": round(ended - started, 6),
    }
    atomic_result(args.result, value)
    for phase in value["phases"]:
        if phase["started_at"] is not None:
            print(
                f"{phase['name']}: status={phase['exit_status']} "
                f"wall={phase['wall_seconds']:.3f}s "
                f"peak_kb={phase['peak_memory_kb']} cache_hit=false"
            )
    if failure_log is not None:
        print("failed-phase-output:")
        print(failure_log.read_text(encoding="utf-8", errors="replace"), end="")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
