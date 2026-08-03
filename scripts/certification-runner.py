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

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
from certification_plan import (  # noqa: E402
    PlanError, TupleError, compare_tuple, diagnostic, expected_tuple,
    observed_tuple, safe_plan, strict_tuple, validate_plan,
)

RESULT_SCHEMA = "nysa.software-factory.certification-result/v1"
SHA = re.compile(r"^[0-9a-f]{40}$")


def canonical(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode()


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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--result", required=True, type=Path)
    parser.add_argument("--workers", type=int, default=2)
    args = parser.parse_args()
    root = Path.cwd().resolve(strict=True)
    factory_sha = os.environ.get("FACTORY_KIT_SHA", "")
    factory_tree = os.environ.get("FACTORY_KIT_TREE", "")
    product_sha = os.environ.get("FACTORY_PRODUCT_SHA", "")
    product_tree = os.environ.get("FACTORY_PRODUCT_TREE", "")
    contract_version = os.environ.get("FACTORY_CONTRACT_VERSION", "")
    serialized_tuple = os.environ.get("FACTORY_CERTIFICATION_TUPLE", "")
    network_reviewed = os.environ.get("FACTORY_CERTIFICATION_NETWORK_REVIEWED", "0")
    if (
        not 1 <= args.workers <= 3
        or not SHA.fullmatch(factory_sha)
        or not SHA.fullmatch(factory_tree)
        or not SHA.fullmatch(product_sha)
        or not SHA.fullmatch(product_tree)
        or not args.result.is_absolute()
        or network_reviewed not in {"0", "1"}
    ):
        print("invalid certification runner boundary", file=sys.stderr)
        return 2
    try:
        plan, plan_digest = safe_plan(args.plan)
        phases = validate_plan(plan, root)
        identity = {
            "contract_version": contract_version,
            "factory_sha": factory_sha,
            "factory_tree": factory_tree,
            "product_sha": product_sha,
            "product_tree": product_tree,
        }
        if not serialized_tuple:
            raise TupleError(
                "runtime_tuple_missing", "runtime_tuple", "present", "missing",
            )
        runtime_tuple = strict_tuple(json.loads(serialized_tuple))
        compare_tuple(runtime_tuple, expected_tuple(identity, plan))
        compare_tuple(runtime_tuple, observed_tuple(identity))
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(json.dumps(diagnostic(error), sort_keys=True), file=sys.stderr)
        return 2

    runtime = {field: runtime_tuple[field] for field in ("node", "npm")}
    missing_network = sorted(
        name for name, phase in phases.items()
        if phase["network"] == "required" and network_reviewed != "1"
    )
    reason = (
        "reviewed_network_required" if missing_network else ""
    )
    if reason:
        observed = time.time()
        atomic_result(args.result, {
            "ended_at": iso(observed),
            "contract_version": contract_version,
            "factory_sha": factory_sha,
            "factory_tree": factory_tree,
            "failure": {"phases": missing_network, "reason_code": reason},
            "max_workers": args.workers,
            "network_reviewed": network_reviewed == "1",
            "phases": [],
            "plan_sha256": plan_digest,
            "product_sha": product_sha,
            "product_tree": product_tree,
            "runtime": runtime,
            "runtime_tuple": runtime_tuple,
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
                    "runtime_tuple": runtime_tuple,
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
        "contract_version": contract_version,
        "ended_at": iso(ended),
        "factory_sha": factory_sha,
        "factory_tree": factory_tree,
        "max_workers": args.workers,
        "network_reviewed": network_reviewed == "1",
        "phases": [completed[name] for name in sorted(completed)],
        "plan_sha256": plan_digest,
        "product_sha": product_sha,
        "product_tree": product_tree,
        "runtime": runtime,
        "runtime_tuple": runtime_tuple,
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
