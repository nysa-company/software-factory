#!/usr/bin/env python3
"""Run a measured product-certification DAG with bounded parallelism."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
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
    NAME, PlanError, TupleError, compare_tuple, diagnostic, expected_tuple,
    observed_tuple, safe_plan, strict_tuple, validate_plan,
)
from certification_cache import CacheError, restore_phase, stage_phase  # noqa: E402

RESULT_SCHEMA = "nysa.software-factory.certification-result/v1"
CACHE_SCHEMA = "nysa.software-factory.certification-phase-evidence/v1"
SHA = re.compile(r"^[0-9a-f]{40}$")
DIGEST = re.compile(r"^[0-9a-f]{64}$")
TIMESTAMP = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$"
)
PHASE_RESULT_KEYS = {
    "artifact_sha256", "cache_hit", "cache_overhead_seconds", "command",
    "ended_at", "exit_status", "input_sha256", "name", "network_declared",
    "network_granted", "output_sha256", "peak_memory_kb",
    "saved_phase_wall_seconds", "started_at", "system_cpu_seconds",
    "user_cpu_seconds", "wall_seconds",
}


def canonical(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode()


def artifact_digest(root: Path, paths: list[str], log: Path) -> str:
    selected: list[dict[str, Any]] = []
    if not paths:
        return hashlib.sha256(log.read_bytes()).hexdigest()
    for relative in paths:
        path = root / relative
        if not path.exists() and not path.is_symlink():
            raise PlanError(f"certification artifact is missing: {relative}")
        candidates = [path]
        if path.is_dir() and not path.is_symlink():
            candidates.extend(path.rglob("*"))
        for item in candidates:
            info = item.lstat()
            name = item.relative_to(root).as_posix()
            mode = stat.S_IMODE(info.st_mode)
            if item.is_symlink():
                raise PlanError(f"certification artifact is unsafe: {relative}")
            if stat.S_ISDIR(info.st_mode):
                selected.append({"mode": mode, "path": name, "type": "directory"})
            elif stat.S_ISREG(info.st_mode):
                raw = item.read_bytes()
                selected.append({
                    "mode": mode,
                    "path": name,
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "size": len(raw),
                    "type": "file",
                })
            else:
                raise PlanError(f"certification artifact is unsafe: {relative}")
    return hashlib.sha256(canonical(sorted(selected, key=lambda item: item["path"]))).hexdigest()


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


def safe_directory(path: Path) -> None:
    if not path.exists() and not path.is_symlink():
        path.mkdir(mode=0o700, parents=True)
    info = path.lstat()
    if (
        path.is_symlink()
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise PlanError("certification phase evidence directory is unsafe")


def safe_file_digest(path: Path) -> str:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_size > 100_000_000
        ):
            raise PlanError("certification phase output is unsafe")
        digest = hashlib.sha256()
        while raw := os.read(descriptor, 1_048_576):
            digest.update(raw)
        return digest.hexdigest()
    finally:
        os.close(descriptor)


def load_phase_evidence(path: Path) -> dict[str, Any] | None:
    if not path.exists() and not path.is_symlink():
        return None
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_size > 1_000_000
        ):
            raise PlanError("certification phase evidence is unsafe")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            value = json.load(stream)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not isinstance(value, dict) or set(value) != {
        "phase", "record_sha256", "schema"
    } or value["schema"] != CACHE_SCHEMA:
        raise PlanError("certification phase evidence is malformed")
    expected = hashlib.sha256(canonical({
        "phase": value["phase"], "schema": value["schema"]
    })).hexdigest()
    phase = value["phase"]
    if (
        value["record_sha256"] != expected
        or not isinstance(phase, dict)
        or set(phase) != PHASE_RESULT_KEYS
        or phase["cache_hit"] is not False
        or type(phase.get("exit_status")) is not int
        or phase["exit_status"] != 0
        or not isinstance(phase.get("name"), str)
        or not NAME.fullmatch(phase["name"])
        or not isinstance(phase.get("command"), list)
        or not phase["command"]
        or not all(isinstance(item, str) and item for item in phase["command"])
        or phase.get("network_declared") not in {"denied", "optional", "required"}
        or not isinstance(phase.get("network_granted"), bool)
        or (
            phase["network_declared"] == "denied"
            and phase["network_granted"]
        )
        or not TIMESTAMP.fullmatch(phase.get("started_at", ""))
        or not TIMESTAMP.fullmatch(phase.get("ended_at", ""))
        or phase["ended_at"] < phase["started_at"]
        or type(phase.get("peak_memory_kb")) is not int
        or any(
            isinstance(phase.get(key), bool)
            or not isinstance(phase.get(key), (int, float))
            or phase[key] < 0
            for key in (
                "cache_overhead_seconds", "saved_phase_wall_seconds",
                "system_cpu_seconds", "user_cpu_seconds", "wall_seconds",
            )
        )
        or phase["peak_memory_kb"] < 0
        or not DIGEST.fullmatch(phase.get("input_sha256", ""))
        or not DIGEST.fullmatch(phase.get("artifact_sha256", ""))
        or not DIGEST.fullmatch(phase.get("output_sha256", ""))
    ):
        raise PlanError("certification phase evidence is invalid")
    return value


def write_phase_evidence(path: Path, phase: dict[str, Any]) -> None:
    value = {"phase": phase, "schema": CACHE_SCHEMA}
    value["record_sha256"] = hashlib.sha256(canonical(value)).hexdigest()
    atomic_result(path, value)


def secure_log(path: Path):
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    info = os.fstat(descriptor)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_nlink != 1
    ):
        os.close(descriptor)
        raise PlanError("certification phase output is unsafe")
    os.fchmod(descriptor, 0o600)
    return os.fdopen(descriptor, "wb")


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
    cache_input_raw = os.environ.get("FACTORY_CERTIFICATION_CACHE_INPUT", "")
    cache_output_raw = os.environ.get("FACTORY_CERTIFICATION_CACHE_OUTPUT", "")
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
    cache_input = Path(cache_input_raw).resolve(strict=False) if cache_input_raw else None
    cache_output = Path(cache_output_raw).resolve(strict=False) if cache_output_raw else None
    try:
        result_parent = args.result.parent.resolve(
            strict=cache_input is not None or cache_output is not None
        )
        invalid_cache = any(
            not path.is_absolute()
            or path.parent.resolve(strict=True) != result_parent
            for path in (cache_input, cache_output)
            if path is not None
        )
    except OSError:
        invalid_cache = True
    if invalid_cache:
        print("invalid certification runner cache boundary", file=sys.stderr)
        return 2
    try:
        args.result.unlink(missing_ok=True)
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
    try:
        safe_directory(run_root)
        cache_records: dict[str, dict[str, Any] | None] = {}
        for name in phases:
            phase_root = run_root / name
            if phase_root.exists() or phase_root.is_symlink():
                safe_directory(phase_root)
            cache_records[name] = load_phase_evidence(phase_root / "evidence.json")
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(str(error), file=sys.stderr)
        return 2
    started = time.time()
    pending = set(phases)
    running: dict[int, dict[str, Any]] = {}
    completed: dict[str, dict[str, Any]] = {}
    failed = False
    failure_log: Path | None = None
    interrupted_signal: int | None = None

    runner_runtime = {
        "architecture": platform.machine(),
        "os": platform.system(),
        "python": platform.python_version(),
    }
    cache_context = {
        **identity,
        "plan_sha256": plan_digest,
        "runtime_tuple": runtime_tuple,
        "runner_runtime": runner_runtime,
    }

    def launch(name: str) -> bool:
        phase = phases[name]
        phase_root = run_root / name
        safe_directory(phase_root)
        log = phase_root / "output.log"
        reusable_phase = phase.get("reuse", "never") != "never"
        lookup_started = time.perf_counter()
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
                    "runner_runtime": runner_runtime,
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
        evidence_path = phase_root / "evidence.json"
        granted = (
            network_reviewed == "1"
            and phase["network"] in {"optional", "required"}
        )
        cached = cache_records[name]
        if reusable_phase and cached is not None:
            record = cached["phase"]
            if record["input_sha256"] == input_digest:
                try:
                    reusable = (
                        record["name"] == name
                        and record["command"] == phase["command"]
                        and record["network_declared"] == phase["network"]
                        and record["network_granted"] == (
                            network_reviewed == "1"
                            and phase["network"] in {"optional", "required"}
                        )
                        and safe_file_digest(log) == record["output_sha256"]
                        and artifact_digest(root, phase["artifacts"], log)
                        == record["artifact_sha256"]
                    )
                except (OSError, PlanError):
                    reusable = False
                if reusable:
                    observed = time.time()
                    overhead = round(time.perf_counter() - lookup_started, 6)
                    completed[name] = {
                        **record,
                        "cache_hit": True,
                        "cache_overhead_seconds": overhead,
                        "cache_record_sha256": cached["record_sha256"],
                        "ended_at": iso(observed),
                        "peak_memory_kb": 0,
                        "started_at": iso(observed),
                        "system_cpu_seconds": 0,
                        "user_cpu_seconds": 0,
                        "saved_phase_wall_seconds": record["wall_seconds"],
                        "wall_seconds": overhead,
                    }
                    pending.remove(name)
                    return False
            evidence_path.unlink()
            cache_records[name] = None
        elif cached is not None:
            evidence_path.unlink()
            cache_records[name] = None

        if reusable_phase and cache_input is not None:
            restored = restore_phase(
                root, log, phase, cache_context, dependencies, input_digest,
                granted, cache_input,
            )
            if restored is not None:
                observed = time.time()
                overhead = round(time.perf_counter() - lookup_started, 6)
                completed[name] = {
                    "artifact_sha256": restored["artifact_sha256"],
                    "cache_hit": True,
                    "cache_overhead_seconds": overhead,
                    "cache_record_sha256": restored["authentication_sha256"],
                    "command": phase["command"],
                    "ended_at": iso(observed),
                    "exit_status": 0,
                    "input_sha256": input_digest,
                    "name": name,
                    "network_declared": phase["network"],
                    "network_granted": granted,
                    "output_sha256": safe_file_digest(log),
                    "peak_memory_kb": 0,
                    "saved_phase_wall_seconds": restored["phase_wall_seconds"],
                    "started_at": iso(observed),
                    "system_cpu_seconds": 0,
                    "user_cpu_seconds": 0,
                    "wall_seconds": overhead,
                }
                pending.remove(name)
                return False

        stream = secure_log(log)
        environment = os.environ.copy()
        environment["TMPDIR"] = tempfile.mkdtemp(prefix=".tmp-", dir=phase_root)
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
            "cache_overhead_seconds": (
                round(time.perf_counter() - lookup_started, 6)
                if reusable_phase else 0
            ),
            "input_sha256": input_digest,
            "log": log,
            "name": name,
            "network_declared": phase["network"],
            "network_granted": granted,
            "process": process,
            "started": time.time(),
            "stream": stream,
        }
        if interrupted_signal is not None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        pending.remove(name)
        return True

    def interrupt(signum: int, _frame: Any) -> None:
        nonlocal interrupted_signal
        interrupted_signal = signum
        for active in running.values():
            try:
                os.killpg(active["process"].pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

    for handled_signal in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
        signal.signal(handled_signal, interrupt)

    try:
        while pending or running:
            if not failed and interrupted_signal is None:
                ready = sorted(
                    name
                    for name in pending
                    if set(phases[name]["depends_on"]).issubset(completed)
                )
                reused = False
                while ready and len(running) < args.workers:
                    reused = not launch(ready.pop(0)) or reused
            if not running:
                if interrupted_signal is not None:
                    failed = True
                    break
                if not failed and reused:
                    continue
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
            phase_result = {
                "artifact_sha256": artifact,
                "cache_hit": False,
                "cache_overhead_seconds": active["cache_overhead_seconds"],
                "command": phases[active["name"]]["command"],
                "ended_at": iso(ended),
                "exit_status": process.returncode,
                "input_sha256": active["input_sha256"],
                "name": active["name"],
                "network_declared": active["network_declared"],
                "network_granted": active["network_granted"],
                "output_sha256": safe_file_digest(active["log"]),
                "peak_memory_kb": peak,
                "saved_phase_wall_seconds": 0,
                "started_at": iso(active["started"]),
                "system_cpu_seconds": round(usage.ru_stime, 6),
                "user_cpu_seconds": round(usage.ru_utime, 6),
                "wall_seconds": round(ended - active["started"], 6),
            }
            if (
                process.returncode == 0
                and phases[active["name"]].get("reuse", "never") != "never"
            ):
                try:
                    write_phase_evidence(
                        run_root / active["name"] / "evidence.json", phase_result
                    )
                    if cache_output is not None:
                        stage_phase(
                            root,
                            active["log"],
                            phases[active["name"]],
                            cache_context,
                            {
                                item: completed[item]["artifact_sha256"]
                                for item in phases[active["name"]]["depends_on"]
                            },
                            active["input_sha256"],
                            active["network_granted"],
                            phase_result["artifact_sha256"],
                            phase_result["output_sha256"],
                            phase_result["wall_seconds"],
                            cache_output,
                        )
                except (CacheError, OSError, PlanError) as error:
                    process.returncode = 125
                    phase_result["exit_status"] = 125
                    with active["log"].open("ab") as stream:
                        stream.write((f"\n{error}\n").encode())
                    phase_result["artifact_sha256"] = artifact_digest(
                        root, phases[active["name"]]["artifacts"], active["log"]
                    )
                    phase_result["output_sha256"] = safe_file_digest(active["log"])
            completed[active["name"]] = phase_result
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
                    "cache_overhead_seconds": 0,
                    "command": phases[name]["command"],
                    "ended_at": None,
                    "exit_status": None,
                    "input_sha256": "",
                    "name": name,
                    "network_declared": phases[name]["network"],
                    "network_granted": False,
                    "output_sha256": "",
                    "peak_memory_kb": 0,
                    "saved_phase_wall_seconds": 0,
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
                f"cache_overhead={phase['cache_overhead_seconds']:.3f}s "
                f"saved_phase={phase['saved_phase_wall_seconds']:.3f}s "
                f"peak_kb={phase['peak_memory_kb']} "
                f"cache_hit={'true' if phase['cache_hit'] else 'false'}"
            )
    if failure_log is not None:
        print("failed-phase-output:")
        print(failure_log.read_text(encoding="utf-8", errors="replace"), end="")
    if interrupted_signal is not None:
        return 128 + interrupted_signal
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
