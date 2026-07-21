#!/usr/bin/env python3
"""Prepare one Git-bound broker request and invoke the Contract 1.6 runtime."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tarfile
import tempfile
from typing import Any


OUTPUT_SCHEMA = "nysa.software-factory.provider-isolated-run/v1"
MAX_CONTEXT = 1_000_000


class IsolatedRunError(ValueError):
    pass


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def command(command: list[str], *, input_data: bytes | None = None) -> bytes:
    result = subprocess.run(
        command,
        input=input_data,
        capture_output=True,
        check=False,
        timeout=1200,
    )
    if result.returncode:
        raise IsolatedRunError(
            result.stderr.decode("utf-8", "replace").strip()
            or "isolated runtime command failed"
        )
    return result.stdout


def git(worktree: Path, *arguments: str) -> bytes:
    return command(["git", "-C", str(worktree), *arguments])


def safe_worktree(path: Path, branch: str, base_sha: str) -> None:
    if not path.is_absolute():
        raise IsolatedRunError("worktree must be absolute")
    info = path.lstat()
    if (
        path.resolve(strict=True) != path
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_mode & 0o022
    ):
        raise IsolatedRunError("worktree is unsafe")
    if git(path, "symbolic-ref", "--short", "HEAD").decode().strip() != branch:
        raise IsolatedRunError("worktree branch drifted")
    if git(path, "rev-parse", "HEAD").decode().strip() != base_sha:
        raise IsolatedRunError("worktree base drifted")
    if git(path, "status", "--porcelain=v1", "-z"):
        raise IsolatedRunError("worktree must be clean")


def extract_snapshot(worktree: Path, destination: Path) -> list[tuple[str, str]]:
    archive = command(["git", "-C", str(worktree), "archive", "--format=tar", "HEAD"])
    archive_path = destination.parent / "source.tar"
    archive_path.write_bytes(archive)
    files: list[tuple[str, str]] = []
    with tarfile.open(archive_path, "r:") as value:
        for member in value:
            path = Path(member.name)
            if (
                path.is_absolute()
                or any(part in ("", ".", "..", ".git") for part in path.parts)
                or not (member.isdir() or member.isfile())
            ):
                raise IsolatedRunError("Git snapshot contains an unsafe entry")
            output = destination.joinpath(*path.parts)
            if member.isdir():
                output.mkdir(mode=0o755, parents=True, exist_ok=True)
                continue
            source = value.extractfile(member)
            if source is None:
                raise IsolatedRunError("Git snapshot member is unreadable")
            raw = source.read()
            output.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            output.write_bytes(raw)
            os.chmod(output, member.mode & 0o111 | 0o444)
            if len(raw) <= 200_000:
                try:
                    files.append((path.as_posix(), raw.decode("utf-8")))
                except UnicodeDecodeError:
                    pass
    archive_path.unlink()
    return files


def prompt_text(args: argparse.Namespace, files: list[tuple[str, str]]) -> str:
    role_prompt = args.prompt_file.read_text(encoding="utf-8")
    sections = [
        role_prompt,
        "",
        f"Ticket: {args.ticket}",
        f"Role: {args.role}",
        f"Task: {args.task}",
        "",
        "Return only compact JSON with exactly two keys:",
        '{"files":["sorted/changed/path"],"patch":"git unified diff"}',
        "The patch must apply to the supplied base with git apply --index.",
        "Do not use markdown fences. Do not modify .git or factory control paths.",
        "",
        "Tracked source snapshot:",
    ]
    used = sum(len(item.encode("utf-8")) for item in sections)
    for name, content in files:
        section = f"\n--- {name} ---\n{content}"
        size = len(section.encode("utf-8"))
        if used + size > MAX_CONTEXT:
            break
        sections.append(section)
        used += size
    return "\n".join(sections)


def provider_request(protocol: str, model: str, prompt: str) -> dict[str, Any]:
    if protocol == "openai-chat":
        return {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
        }
    if protocol == "openai-responses":
        return {"model": model, "input": prompt}
    if protocol == "anthropic-messages":
        return {
            "model": model,
            "max_tokens": 8192,
            "messages": [{"role": "user", "content": prompt}],
        }
    raise IsolatedRunError("provider protocol is unsupported")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", required=True, type=Path)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--policy", required=True, type=Path)
    parser.add_argument("--broker-db", required=True, type=Path)
    parser.add_argument("--broker-credentials", required=True, type=Path)
    parser.add_argument("--broker-url", required=True)
    parser.add_argument("--broker-path", required=True)
    parser.add_argument("--broker-ca", type=Path)
    parser.add_argument(
        "--broker-allow-http-loopback", action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--protocol",
        choices=("openai-chat", "openai-responses", "anthropic-messages"),
        required=True,
    )
    parser.add_argument("--model", required=True)
    parser.add_argument("--attempt-root", required=True, type=Path)
    parser.add_argument("--artifact-policy", required=True, type=Path)
    parser.add_argument("--apply-lock", required=True, type=Path)
    parser.add_argument("--worktree", required=True, type=Path)
    parser.add_argument("--branch", required=True)
    parser.add_argument("--base-sha", required=True)
    parser.add_argument("--ticket", required=True)
    parser.add_argument("--role", required=True)
    parser.add_argument("--route-id", required=True)
    parser.add_argument("--provider-family", required=True)
    parser.add_argument("--account-route", required=True)
    parser.add_argument("--product-id", required=True)
    parser.add_argument("--budget-day", required=True)
    parser.add_argument("--reserve-micro-usd", required=True, type=int)
    parser.add_argument("--product-cap-micro-usd", required=True, type=int)
    parser.add_argument("--ticket-cap-micro-usd", required=True, type=int)
    parser.add_argument("--machine-cap-micro-usd", required=True, type=int)
    parser.add_argument("--prompt-file", required=True, type=Path)
    parser.add_argument("--task", required=True)
    parser.add_argument("--image-lock", required=True, type=Path)
    args = parser.parse_args()
    try:
        safe_worktree(args.worktree, args.branch, args.base_sha)
        lock = json.loads(args.image_lock.read_text(encoding="utf-8"))
        worker_program = args.image_lock.parent.parent / lock["worker_program"]
        if hashlib.sha256(worker_program.read_bytes()).hexdigest() != lock["worker_sha256"]:
            raise IsolatedRunError("worker image lock drifted")
        policy = json.loads(args.policy.read_text(encoding="utf-8"))
        policy_hash = hashlib.sha256(canonical(policy).encode("utf-8")).hexdigest()
        attempt_id = (
            f"{args.ticket}-{args.role}-"
            + hashlib.sha256(
                f"{args.base_sha}:{args.route_id}:{args.task}".encode()
            ).hexdigest()[:24]
        )
        with tempfile.TemporaryDirectory(prefix="factory-isolated-run.") as temporary:
            root = Path(temporary).resolve()
            os.chmod(root, 0o700)
            source = root / "source"
            source.mkdir(mode=0o700)
            files = extract_snapshot(args.worktree, source)
            provider_path = root / "provider-request.json"
            provider_path.write_text(
                canonical(
                    provider_request(
                        args.protocol,
                        args.model,
                        prompt_text(args, files),
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            os.chmod(provider_path, 0o600)
            input_path = root / "worker-input.json"
            request_path = root / "execution-request.json"
            request = {
                "attempt_id": attempt_id,
                "base_sha": args.base_sha,
                "command": ["node", "/workspace/payload/worker"],
                "image": lock["image_reference"],
                "input": str(input_path),
                "policy_sha256": policy_hash,
                "role": args.role,
                "route_id": args.route_id,
                "schema": "nysa.software-factory.provider-execution-request/v3",
                "source": str(source),
                "ticket": args.ticket,
                "worker_program": str(worker_program),
                "worker_sha256": lock["worker_sha256"],
            }
            request_path.write_text(canonical(request) + "\n", encoding="utf-8")
            os.chmod(request_path, 0o600)
            runtime = [
                sys.executable,
                str(args.runtime),
                "--db", str(args.db),
                "--policy", str(args.policy),
                "execute",
                "--request", str(request_path),
                "--attempt-root", str(args.attempt_root),
                "--provider-family", args.provider_family,
                "--account-route", args.account_route,
                "--reserve-micro-usd", str(args.reserve_micro_usd),
                "--product-id", args.product_id,
                "--budget-day", args.budget_day,
                "--product-daily-cap-micro-usd", str(args.product_cap_micro_usd),
                "--ticket-cap-micro-usd", str(args.ticket_cap_micro_usd),
                "--machine-daily-cap-micro-usd", str(args.machine_cap_micro_usd),
                "--artifact-mode", "patch-v1",
                "--worktree", str(args.worktree),
                "--artifact-policy", str(args.artifact_policy),
                "--apply-lock", str(args.apply_lock),
                "--expected-branch", args.branch,
                "--provider-transport", "broker",
                "--broker-db", str(args.broker_db),
                "--broker-credentials", str(args.broker_credentials),
                "--broker-url", args.broker_url,
                "--broker-path", args.broker_path,
                "--broker-model", args.model,
                "--provider-request", str(provider_path),
            ]
            if args.broker_ca:
                runtime.extend(["--broker-ca", str(args.broker_ca)])
            if args.broker_allow_http_loopback:
                runtime.append("--broker-allow-http-loopback")
            result = json.loads(command(runtime))
        print(canonical({"runtime": result, "schema": OUTPUT_SCHEMA, "status": "ok"}))
        charge = result.get("application", {}).get(
            "charge_micro_usd", args.reserve_micro_usd
        )
        print(
            f"turns=1 cost_usd={charge / 1_000_000:.6f} "
            "cost_basis=broker_conservative"
        )
    except (
        IsolatedRunError,
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        KeyError,
        subprocess.SubprocessError,
        ValueError,
    ) as error:
        print(canonical({"error": str(error), "schema": OUTPUT_SCHEMA, "status": "error"}))
        raise SystemExit(2)


if __name__ == "__main__":
    main()
