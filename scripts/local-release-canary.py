#!/usr/bin/env python3
"""Run one fully isolated, non-promotable local release canary."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

SHA = re.compile(r"[0-9a-f]{40}")
DIGEST = re.compile(r"[0-9a-f]{64}")
SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
TICKET = re.compile(r"T-[0-9]+")
SCHEMA = "nysa.software-factory.local-release-canary/v1"
EVENT_SCHEMA = "nysa.software-factory.controller-event/v1"
PLAN_SCHEMA = "nysa.software-factory.release-plan/v1"
RESULT_SCHEMA = "nysa.software-factory.release-result/v1"
GIT = "/usr/bin/git"
EVIDENCE_ROOT: Path | None = None


class CanaryError(RuntimeError):
    pass


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def owner_regular(path: Path, label: str, executable: bool = False) -> Path:
    try:
        value = path.resolve(strict=True)
        info = value.stat()
    except OSError as error:
        raise CanaryError(f"{label} is missing") from error
    if (
        not value.is_file() or info.st_uid != os.geteuid()
        or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o022
        or (executable and not os.access(value, os.X_OK))
    ):
        raise CanaryError(f"{label} is unsafe")
    return value


def source_identity(path: Path, label: str) -> tuple[Path, str]:
    if not path.is_absolute() or path.is_symlink():
        raise CanaryError(f"{label} must be a physical absolute path")
    try:
        physical = path.resolve(strict=True)
    except OSError as error:
        raise CanaryError(f"{label} is missing") from error
    info = physical.stat()
    if not physical.is_dir() or info.st_uid != os.geteuid():
        raise CanaryError(f"{label} is unsafe")
    top = git_text(physical, "rev-parse", "--show-toplevel")
    if Path(top).resolve(strict=True) != physical:
        raise CanaryError(f"{label} is not a Git root")
    if git_bytes(physical, "status", "--porcelain=v1", "-z"):
        raise CanaryError(f"{label} must be clean")
    head = git_text(physical, "rev-parse", "HEAD")
    if not SHA.fullmatch(head):
        raise CanaryError(f"{label} HEAD is invalid")
    return physical, head


def git_environment() -> dict[str, str]:
    return {
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0", "GIT_PROTOCOL_FROM_USER": "0",
        "HOME": "/var/empty", "LC_ALL": "C", "PATH": "/usr/bin:/bin",
    }


def git_bytes(repo: Path | None, *arguments: str) -> bytes:
    command = [GIT]
    if repo is not None:
        command.extend(["-C", str(repo)])
    result = subprocess.run(
        [*command, "-c", "credential.helper=", "-c", "protocol.file.allow=always",
         *arguments], capture_output=True, check=False, env=git_environment(),
        timeout=120,
    )
    if result.returncode:
        raise CanaryError("local Git preparation failed")
    return result.stdout


def git_text(repo: Path | None, *arguments: str) -> str:
    return git_bytes(repo, *arguments).decode("utf-8").strip()


def safe_json(path: Path, label: str) -> dict[str, Any]:
    try:
        info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or info.st_nlink != 1 or info.st_size > 1024 * 1024
            or stat.S_IMODE(info.st_mode) & 0o077
        ):
            raise CanaryError(f"{label} is unsafe")
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise CanaryError(f"{label} is invalid") from error
    if not isinstance(value, dict):
        raise CanaryError(f"{label} is invalid")
    return value


def prepare_root(requested: Path | None) -> Path:
    if requested is None:
        parent = Path("/private/tmp") if Path("/private/tmp").is_dir() else Path("/tmp")
        root = Path(tempfile.mkdtemp(prefix="r.", dir=parent))
    else:
        if not requested.is_absolute() or requested.is_symlink():
            raise CanaryError("canary root must be a physical absolute path")
        root = requested
        if root.exists():
            if any(root.iterdir()):
                raise CanaryError("canary root must be empty")
        else:
            root.mkdir(mode=0o700)
    os.chmod(root, 0o700)
    physical = root.resolve(strict=True)
    info = physical.stat()
    real_home = Path(os.path.expanduser("~")).resolve(strict=True)
    if (
        physical != root or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o700
        or physical == real_home or real_home in physical.parents
    ):
        raise CanaryError("canary root must be owner-only and outside the account home")
    return physical


def local_origin(source: Path, origin: Path, checkout: Path, head: str) -> None:
    git_bytes(None, "clone", "--bare", "--no-local", str(source), str(origin))
    refs = git_text(origin, "for-each-ref", "--format=%(refname)", "refs/heads").splitlines()
    git_bytes(origin, "update-ref", "refs/heads/main", head)
    for ref in refs:
        if ref != "refs/heads/main":
            git_bytes(origin, "update-ref", "-d", ref)
    git_bytes(origin, "symbolic-ref", "HEAD", "refs/heads/main")
    git_bytes(None, "clone", "--no-local", "--single-branch", "--branch", "main",
              str(origin), str(checkout))


class Runner:
    def __init__(self, root: Path, environment: dict[str, str], deadline: float):
        self.root = root
        self.environment = environment
        self.deadline = deadline
        self.phases: list[dict[str, Any]] = []
        self.commands: list[dict[str, Any]] = []
        self.log = root / "commands"
        self.log.mkdir(mode=0o700)

    def run(self, name: str, command: list[str], *, json_result: bool = False) -> Any:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise CanaryError("canary exceeded its time limit")
        started = time.time_ns()
        monotonic = time.monotonic()
        process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=self.environment, start_new_session=True,
        )
        try:
            stdout, stderr = process.communicate(timeout=remaining)
        except subprocess.TimeoutExpired as error:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            raise CanaryError(f"{name} exceeded the canary time limit") from error
        elapsed = round(time.monotonic() - monotonic, 3)
        index = len(self.commands) + 1
        stdout_path = self.log / f"{index:02d}-{name}.stdout"
        stderr_path = self.log / f"{index:02d}-{name}.stderr"
        stdout_path.write_text(stdout, encoding="utf-8")
        stderr_path.write_text(stderr, encoding="utf-8")
        os.chmod(stdout_path, 0o600)
        os.chmod(stderr_path, 0o600)
        self.commands.append({
            "argv": command, "elapsed_seconds": elapsed, "name": name,
            "returncode": process.returncode,
        })
        self.phases.append({
            "elapsed_seconds": elapsed, "ended_epoch_ms": time.time_ns() // 1_000_000,
            "name": name, "started_epoch_ms": started // 1_000_000,
        })
        if process.returncode:
            error = ""
            if json_result:
                try:
                    error = str(json.loads(stdout).get("error", ""))
                except (AttributeError, json.JSONDecodeError):
                    pass
            raise CanaryError(f"{name} failed{': ' + error if error else ''}")
        if not json_result:
            return stdout
        try:
            value = json.loads(stdout)
        except json.JSONDecodeError as error:
            raise CanaryError(f"{name} returned invalid JSON") from error
        if not isinstance(value, dict):
            raise CanaryError(f"{name} returned invalid JSON")
        return value


def validate_event(path: Path, event: str, ticket: str, sha: str) -> dict[str, Any]:
    value = safe_json(path, f"{event} event")
    unsigned = {key: item for key, item in value.items() if key != "event_sha256"}
    if (
        value.get("schema") != EVENT_SCHEMA or value.get("event") != event
        or value.get("ticket") != ticket or value.get("factory_sha") != sha
        or value.get("event_sha256") != hashlib.sha256(canonical(unsigned)).hexdigest()
    ):
        raise CanaryError(f"{event} evidence is invalid")
    return value


def event_for(events: Path, name: str, ticket: str, sha: str) -> tuple[Path, dict[str, Any]]:
    matches: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(events.glob("*.json")):
        value = safe_json(path, "controller event")
        if value.get("event") == name and value.get("ticket") == ticket:
            matches.append((path, validate_event(path, name, ticket, sha)))
    if len(matches) != 1:
        raise CanaryError(f"expected exactly one {name} event")
    return matches[0]


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--factory", required=True, type=Path)
    parser.add_argument("--product", required=True, type=Path)
    parser.add_argument("--project", required=True)
    parser.add_argument("--ticket", required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--operator-id", required=True)
    parser.add_argument("--runtime-bin", required=True, type=Path)
    parser.add_argument("--gitleaks-bin", required=True, type=Path)
    parser.add_argument("--claude-bin", required=True, type=Path)
    parser.add_argument("--codex-bin", required=True, type=Path)
    parser.add_argument("--cursor-bin", required=True, type=Path)
    parser.add_argument("--root", type=Path)
    parser.add_argument("--max-seconds", type=int, default=900)
    parser.add_argument("--allow-certification-network", action="store_true")
    return parser.parse_args()


def execute(args: argparse.Namespace) -> dict[str, Any]:
    global EVIDENCE_ROOT
    EVIDENCE_ROOT = None
    started = time.monotonic()
    prepare_epoch = time.time_ns()
    if (
        not SAFE_ID.fullmatch(args.project) or not SAFE_ID.fullmatch(args.profile)
        or not SAFE_ID.fullmatch(args.operator_id) or args.operator_id == "auto"
        or not TICKET.fullmatch(args.ticket) or not 1 <= args.max_seconds <= 3600
    ):
        raise CanaryError("canary arguments are invalid")
    factory_source, _ = source_identity(args.factory, "Factory source")
    product_source, product_source_sha = source_identity(args.product, "product source")
    try:
        ticket_text = (product_source / f"factory/tickets/{args.ticket}.md").read_text(
            encoding="utf-8",
        )
    except (OSError, UnicodeError) as error:
        raise CanaryError("canary ticket is missing") from error
    states = re.findall(r"^State:\s*(.*?)\s*$", ticket_text, re.I | re.M)
    if states != ["Ready"]:
        raise CanaryError("canary ticket must have exactly one Ready state")
    try:
        candidate = (product_source / "factory/KIT_PIN").read_text(encoding="utf-8").strip()
    except OSError as error:
        raise CanaryError("product KIT_PIN is missing") from error
    if not SHA.fullmatch(candidate):
        raise CanaryError("product KIT_PIN is invalid")
    try:
        candidate_type = git_text(factory_source, "cat-file", "-t", candidate)
    except CanaryError:
        candidate_type = ""
    if candidate_type != "commit":
        raise CanaryError("Factory source does not contain the pinned release")
    runtime_source = owner_regular(args.runtime_bin / "node", "Node runtime", True).parent
    owner_regular(runtime_source / "npm", "npm runtime", True)
    owner_regular(runtime_source / "npx", "npx runtime", True)
    gitleaks = owner_regular(args.gitleaks_bin, "gitleaks", True)
    provider_bins = [
        owner_regular(args.claude_bin, "Claude CLI", True),
        owner_regular(args.codex_bin, "Codex CLI", True),
        owner_regular(args.cursor_bin, "Cursor CLI", True),
    ]
    root = prepare_root(args.root)
    EVIDENCE_ROOT = root
    factory_origin, factory = root / "factory-origin.git", root / "factory"
    product_origin, product = root / "product-origin.git", root / "product"
    local_origin(factory_source, factory_origin, factory, candidate)
    local_origin(product_source, product_origin, product, product_source_sha)
    if git_text(product, "rev-parse", "HEAD") != product_source_sha:
        raise CanaryError("fresh product clone changed identity")
    runtime = root / "runtime/node"
    shutil.copytree(runtime_source.parent, runtime, symlinks=True)
    tools = root / "tools"
    tools.mkdir(mode=0o700)
    shutil.copy2(gitleaks, tools / "gitleaks", follow_symlinks=False)
    os.chmod(tools / "gitleaks", 0o700)
    binary = root / "bin"
    binary.mkdir(mode=0o700)
    gh_fixture = owner_regular(factory / "ci/fixtures/gh-protected-checks", "GitHub fixture", True)
    shutil.copy2(gh_fixture, binary / "gh", follow_symlinks=False)
    os.chmod(binary / "gh", 0o700)
    tmp = root / "tmp"
    tmp.mkdir(mode=0o700)
    kits = root / ".factory/kits"
    environment = {
        "FACTORY_KIT_CANONICAL_ORIGIN": str(factory_origin),
        "FACTORY_KIT_TEST_MODE": "1", "FACTORY_KIT_TEST_REMOTE_FULL_CI": "1",
        "FACTORY_LAUNCH_TEST_HOME": str(root), "FACTORY_LAUNCH_TEST_MODE": "1",
        "FACTORY_KITS_ROOT": str(kits), "FACTORY_RELEASE_TEST_HOME": str(root),
        "GITLEAKS_BIN": str(tools / "gitleaks"), "HOME": str(root),
        "LANG": "C.UTF-8", "LC_ALL": "C", "TMPDIR": str(tmp),
        "PATH": ":".join([
            str(binary), str(runtime / "bin"), "/opt/homebrew/bin",
            "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin",
        ]),
    }
    if args.allow_certification_network:
        environment["FACTORY_KIT_CERTIFICATION_NETWORK_REVIEWED"] = "1"
    runner = Runner(root, environment, started + args.max_seconds)
    runner.phases.append({
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "ended_epoch_ms": time.time_ns() // 1_000_000,
        "name": "local-preparation", "started_epoch_ms": prepare_epoch // 1_000_000,
    })
    kit = factory / "scripts/factory-kit.sh"
    base = [
        "bash", str(kit), "release", "setup", "--project", args.project,
        "--product", str(product), "--sha", candidate, "--repo", str(factory),
        "--profile", args.profile, "--operator-id", args.operator_id,
        "--runtime-bin", str(runtime / "bin"), "--claude-bin", str(provider_bins[0]),
        "--codex-bin", str(provider_bins[1]), "--cursor-bin", str(provider_bins[2]),
    ]
    value = runner.run("release-setup", base, json_result=True)
    resumes = 0
    while value.get("schema") == PLAN_SCHEMA:
        if value.get("status") != "authorized" or value.get("stage") not in {
            "prerequisites", "activation",
        }:
            raise CanaryError("release setup returned an invalid plan")
        resumes += 1
        if resumes > 6:
            raise CanaryError("release did not converge")
        sealed = kits / "releases" / candidate / "scripts/factory-kit.sh"
        value = runner.run(
            f"release-resume-{resumes}",
            ["bash", str(sealed), "release", "resume", "--project", args.project,
             "--sha", candidate, "--approved-by", args.operator_id],
            json_result=True,
        )
    if (
        value.get("schema") != RESULT_SCHEMA or value.get("status") not in {"pass", "replayed"}
        or value.get("factory_sha") != candidate or value.get("project") != args.project
    ):
        raise CanaryError("release activation did not pass")
    sealed = kits / "releases" / candidate / "scripts/factory-kit.sh"
    launcher = root / ".factory/bin/factory-launch"
    reconcile = runner.run(
        "controller-reconcile", [str(launcher), args.project, "reconcile", "--json"],
        json_result=True,
    )
    if (
        reconcile.get("schema") != "nysa.software-factory.controller/v1"
        or reconcile.get("status") != "ok"
        or reconcile.get("results")
        != [{"status": "planner-complete", "ticket": args.ticket}]
    ):
        raise CanaryError("controller did not complete the requested mock planner")
    events = kits / "projects" / args.project / "controller/events"
    planning_path, planning = event_for(
        events, "repository_test_planning", args.ticket, candidate,
    )
    complete_path, complete = event_for(
        events, "repository_test_planner_completed", args.ticket, candidate,
    )
    elapsed = round(time.monotonic() - started, 3)
    if elapsed > args.max_seconds:
        raise CanaryError("canary exceeded its time limit")
    return {
        "commands": runner.commands, "elapsed_seconds": elapsed,
        "events": {
            "planning": {"path": str(planning_path), "sha256": planning["event_sha256"]},
            "planner_completed": {"path": str(complete_path), "sha256": complete["event_sha256"]},
        },
        "factory_sha": candidate, "max_seconds": args.max_seconds,
        "phases": runner.phases, "product_sha": product_source_sha,
        "production_evidence": False, "project": args.project, "resumes": resumes,
        "root": str(root), "schema": SCHEMA, "status": "pass", "ticket": args.ticket,
        "trust_scope": "repository-test",
    }


def main() -> int:
    try:
        value = execute(arguments())
        status = 0
    except (CanaryError, OSError, subprocess.SubprocessError) as error:
        value = {"error": str(error), "production_evidence": False,
                 "schema": SCHEMA, "status": "error"}
        if EVIDENCE_ROOT is not None:
            value["root"] = str(EVIDENCE_ROOT)
        status = 2
    print(json.dumps(value, sort_keys=True, separators=(",", ":")))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
