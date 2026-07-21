#!/usr/bin/env python3
"""Validate and apply one identity-bound provider patch on the trusted host."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
from typing import Any


ARTIFACT_SCHEMA = "nysa.software-factory.provider-patch-artifact/v1"
IDENTITY_SCHEMA = "nysa.software-factory.provider-container-identity/v2"
RESULT_SCHEMA = "nysa.software-factory.provider-execution-result/v2"
OUTPUT_SCHEMA = "nysa.software-factory.provider-artifact-controller/v1"
POLICY_SCHEMA = "nysa.software-factory.provider-artifact-policy/v1"
SHA256 = re.compile(r"^[0-9a-f]{64}$")
GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
SAFE_VALUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,255}$")
MAX_JSON = 1_000_000
MAX_PATCH = 10_000_000


class ArtifactError(ValueError):
    pass


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def canonical_document(value: Any) -> str:
    return canonical(value) + "\n"


def secure_directory(path: Path, label: str) -> None:
    if not path.is_absolute():
        raise ArtifactError(f"{label} path must be absolute")
    info = path.lstat()
    if (
        path.resolve(strict=True) != path
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_mode & 0o022
    ):
        raise ArtifactError(f"{label} is unsafe")


def secure_file(path: Path, label: str, maximum: int) -> tuple[bytes, os.stat_result]:
    if not path.is_absolute():
        raise ArtifactError(f"{label} path must be absolute")
    before = path.lstat()
    if (
        path.is_symlink()
        or not stat.S_ISREG(before.st_mode)
        or before.st_uid != os.geteuid()
        or before.st_nlink != 1
        or before.st_mode & 0o022
        or before.st_size > maximum
    ):
        raise ArtifactError(f"{label} is unsafe")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        actual = os.fstat(descriptor)
        if (actual.st_dev, actual.st_ino) != (before.st_dev, before.st_ino):
            raise ArtifactError(f"{label} changed while opening")
        raw = b""
        while len(raw) <= maximum:
            block = os.read(descriptor, min(65536, maximum + 1 - len(raw)))
            if not block:
                break
            raw += block
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if len(raw) > maximum or (
        before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns
    ) != (
        after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns
    ):
        raise ArtifactError(f"{label} changed while reading")
    return raw, before


def json_file(path: Path, label: str) -> tuple[dict[str, Any], bytes]:
    raw, _ = secure_file(path, label, MAX_JSON)
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ArtifactError(f"{label} is invalid JSON") from error
    if not isinstance(value, dict):
        raise ArtifactError(f"{label} must contain an object")
    return value, raw


def load_policy(path: Path) -> dict[str, Any]:
    value, raw = json_file(path, "artifact policy")
    if raw.decode("utf-8") != canonical_document(value):
        raise ArtifactError("artifact policy is not canonical")
    if set(value) != {"schema", "protected_paths"} or value.get("schema") != POLICY_SCHEMA:
        raise ArtifactError("artifact policy schema is unsupported")
    paths = value["protected_paths"]
    if (
        not isinstance(paths, list)
        or len(paths) != len(set(paths))
        or any(
            not isinstance(item, str)
            or not item
            or item.startswith("/")
            or item.endswith("/")
            or item in (".", "..")
            or any(part in ("", ".", "..") for part in item.split("/"))
            for item in paths
        )
    ):
        raise ArtifactError("artifact policy protected_paths is invalid")
    return value


def git(worktree: Path, *arguments: str, env: dict[str, str] | None = None) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(worktree), *arguments],
        env=env,
        capture_output=True,
        check=False,
        timeout=60,
    )
    if result.returncode:
        raise ArtifactError(
            f"Git validation failed: {result.stderr.decode('utf-8', 'replace').strip()}"
        )
    return result.stdout


def artifact_tree(artifact_root: Path) -> tuple[int, str]:
    secure_directory(artifact_root, "artifact directory")
    entries = sorted(artifact_root.iterdir(), key=lambda item: item.name)
    if [item.name for item in entries] != ["artifact.json", "changes.patch"]:
        raise ArtifactError("artifact directory must contain exactly artifact.json and changes.patch")
    total = 0
    hashed: list[bytes] = []
    for entry in entries:
        maximum = MAX_JSON if entry.name == "artifact.json" else MAX_PATCH
        raw, _ = secure_file(entry, f"artifact {entry.name}", maximum)
        total += len(raw)
        relative = entry.name.encode("utf-8")
        hashed.append(
            len(relative).to_bytes(8, "big")
            + relative
            + len(raw).to_bytes(8, "big")
            + raw
        )
    return total, hashlib.sha256(b"".join(hashed)).hexdigest()


IDENTITY_BINDING = (
    "attempt_id", "base_sha", "binding_sha256", "image_digest", "input_sha256",
    "policy_sha256", "role", "route_id", "source_sha256", "ticket",
    "worker_sha256",
)


def validate_bundle(args: argparse.Namespace) -> tuple[dict[str, Any], bytes, list[str]]:
    secure_directory(args.attempt, "attempt directory")
    identity, identity_raw = json_file(args.attempt / "identity.json", "worker identity")
    result, result_raw = json_file(args.attempt / "result.json", "execution result")
    artifact_root = args.attempt / "artifacts"
    artifact, artifact_raw = json_file(artifact_root / "artifact.json", "patch artifact")
    patch, _ = secure_file(artifact_root / "changes.patch", "patch", args.max_patch_bytes)
    if (
        identity_raw.decode("utf-8") != canonical_document(identity)
        or result_raw.decode("utf-8") != canonical_document(result)
        or artifact_raw.decode("utf-8") != canonical_document(artifact)
    ):
        raise ArtifactError("identity, result, and artifact JSON must be canonical")
    if identity.get("schema") != IDENTITY_SCHEMA or result.get("schema") != RESULT_SCHEMA:
        raise ArtifactError("worker identity or result schema is unsupported")
    if result.get("return_code") != 0 or result.get("mode") != "isolated-v1":
        raise ArtifactError("only successful isolated execution can produce an applicable artifact")
    artifact_bytes, artifact_sha256 = artifact_tree(artifact_root)
    if (
        result.get("artifact_bytes") != artifact_bytes
        or result.get("artifact_sha256") != artifact_sha256
    ):
        raise ArtifactError("execution result does not bind the artifact tree")
    required = {
        "schema", *IDENTITY_BINDING, "patch_path", "patch_sha256", "files", "telemetry"
    }
    if set(artifact) != required or artifact.get("schema") != ARTIFACT_SCHEMA:
        raise ArtifactError("patch artifact schema is unsupported")
    for field in IDENTITY_BINDING:
        if artifact.get(field) != identity.get(field) or result.get(field) != identity.get(field):
            raise ArtifactError(f"artifact identity mismatch for {field}")
    if artifact["patch_path"] != "changes.patch":
        raise ArtifactError("patch_path is invalid")
    patch_hash = hashlib.sha256(patch).hexdigest()
    if artifact.get("patch_sha256") != patch_hash or not SHA256.fullmatch(patch_hash):
        raise ArtifactError("patch digest mismatch")
    files = artifact["files"]
    if (
        not isinstance(files, list)
        or not files
        or files != sorted(set(files))
        or any(
            not isinstance(item, str)
            or item.startswith("/")
            or any(part in ("", ".", "..") for part in item.split("/"))
            for item in files
        )
    ):
        raise ArtifactError("artifact files are invalid")
    telemetry = artifact["telemetry"]
    if not isinstance(telemetry, dict) or set(telemetry) != {
        "charge_micro_usd", "duration_ms", "input_tokens", "output_tokens",
        "provider_request_id",
    }:
        raise ArtifactError("artifact telemetry is malformed")
    for field in ("charge_micro_usd", "duration_ms", "input_tokens", "output_tokens"):
        value = telemetry[field]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ArtifactError("artifact telemetry is malformed")
    if telemetry["charge_micro_usd"] > args.reserve_micro_usd:
        raise ArtifactError("artifact charge exceeds its reservation")
    request_id = telemetry["provider_request_id"]
    if request_id is not None and (
        not isinstance(request_id, str) or not SAFE_VALUE.fullmatch(request_id)
    ):
        raise ArtifactError("artifact telemetry is malformed")
    return artifact, patch, files


def changed_paths(worktree: Path, patch: bytes) -> tuple[list[str], dict[str, str]]:
    git_dir = Path(git(worktree, "rev-parse", "--git-dir").decode().strip())
    if not git_dir.is_absolute():
        git_dir = (worktree / git_dir).resolve()
    index = git_dir / "index"
    with tempfile.TemporaryDirectory(prefix="factory-artifact-index-") as temporary:
        temporary_index = Path(temporary) / "index"
        if index.exists():
            shutil.copyfile(index, temporary_index)
        environment = {**os.environ, "GIT_INDEX_FILE": str(temporary_index)}
        process = subprocess.run(
            ["git", "-C", str(worktree), "apply", "--cached", "--whitespace=error-all", "-"],
            input=patch,
            env=environment,
            capture_output=True,
            check=False,
            timeout=60,
        )
        if process.returncode:
            raise ArtifactError(
                f"patch cannot be applied: {process.stderr.decode('utf-8', 'replace').strip()}"
            )
        names = git(worktree, "diff", "--cached", "--name-only", "-z", env=environment)
        paths = [item.decode("utf-8") for item in names.split(b"\0") if item]
        modes_raw = git(worktree, "ls-files", "--stage", "-z", env=environment)
        modes: dict[str, str] = {}
        for record in modes_raw.split(b"\0"):
            if not record:
                continue
            metadata, name = record.split(b"\t", 1)
            path = name.decode("utf-8")
            if path in paths:
                modes[path] = metadata.split(b" ", 1)[0].decode("ascii")
    return sorted(paths), modes


def protected(path: str, prefixes: list[str]) -> bool:
    return any(path == prefix or path.startswith(prefix + "/") for prefix in prefixes)


def verify_worktree(args: argparse.Namespace, patch: bytes, files: list[str]) -> None:
    secure_directory(args.worktree, "worktree")
    branch = git(args.worktree, "symbolic-ref", "--short", "HEAD").decode().strip()
    head = git(args.worktree, "rev-parse", "HEAD").decode().strip()
    if branch != args.expected_branch or head != args.base_sha:
        raise ArtifactError("ticket branch or base SHA drifted")
    if git(args.worktree, "status", "--porcelain=v1", "-z"):
        raise ArtifactError("ticket worktree must be clean before artifact application")
    actual_files, modes = changed_paths(args.worktree, patch)
    if actual_files != files:
        raise ArtifactError("patch paths do not match the artifact manifest")
    policy = load_policy(args.policy)
    if any(protected(path, policy["protected_paths"]) for path in actual_files):
        raise ArtifactError("patch modifies a protected path")
    if any(mode not in ("100644", "100755") for mode in modes.values()):
        raise ArtifactError("patch creates a symlink, gitlink, or unsupported file mode")


def write_receipt(path: Path, value: dict[str, Any]) -> None:
    raw = canonical(value).encode("utf-8")
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        os.write(descriptor, raw)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def handle(args: argparse.Namespace) -> dict[str, Any]:
    if not GIT_SHA.fullmatch(args.base_sha):
        raise ArtifactError("base_sha is invalid")
    if not 0 <= args.reserve_micro_usd <= 10**15:
        raise ArtifactError("reserve_micro_usd is invalid")
    artifact, patch, files = validate_bundle(args)
    if artifact["base_sha"] != args.base_sha:
        raise ArtifactError("artifact is not bound to the expected base SHA")
    if not args.expected_branch.endswith("/" + artifact["ticket"]):
        raise ArtifactError("expected branch is not bound to the artifact ticket")
    secure_directory(args.lock.parent, "apply lock directory")
    descriptor = os.open(
        args.lock,
        os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
            or info.st_mode & 0o077
        ):
            raise ArtifactError("apply lock is unsafe")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        receipt_path = args.attempt / "applied.json"
        if receipt_path.exists() or receipt_path.is_symlink():
            raise ArtifactError("artifact was already applied or requires reconciliation")
        verify_worktree(args, patch, files)
        if args.action == "apply":
            process = subprocess.run(
                ["git", "-C", str(args.worktree), "apply", "--index", "--whitespace=error-all", "-"],
                input=patch,
                capture_output=True,
                check=False,
                timeout=60,
            )
            if process.returncode:
                raise ArtifactError("patch application failed after validation; reconcile manually")
            commit = subprocess.run(
                [
                    "git", "-C", str(args.worktree),
                    "-c", "user.name=Software Factory",
                    "-c", "user.email=factory@local",
                    "-c", "core.hooksPath=/dev/null",
                    "commit", "--no-gpg-sign",
                    "-m", f"{artifact['ticket']}: apply {artifact['role']} artifact",
                ],
                capture_output=True,
                check=False,
                timeout=60,
            )
            if commit.returncode:
                raise ArtifactError(
                    "artifact commit failed after application; reconcile manually"
                )
            commit_sha = git(args.worktree, "rev-parse", "HEAD").decode().strip()
            parent_sha = git(args.worktree, "rev-parse", "HEAD^").decode().strip()
            if parent_sha != args.base_sha or git(
                args.worktree, "status", "--porcelain=v1", "-z"
            ):
                raise ArtifactError(
                    "artifact commit does not have the validated parent or clean tree"
                )
            receipt = {
                "attempt_id": artifact["attempt_id"],
                "base_sha": args.base_sha,
                "binding_sha256": artifact["binding_sha256"],
                "charge_micro_usd": artifact["telemetry"]["charge_micro_usd"],
                "commit_sha": commit_sha,
                "files": files,
                "patch_sha256": artifact["patch_sha256"],
                "schema": OUTPUT_SCHEMA,
                "status": "applied",
            }
            write_receipt(receipt_path, receipt)
            return receipt
        return {
            "attempt_id": artifact["attempt_id"],
            "base_sha": args.base_sha,
            "binding_sha256": artifact["binding_sha256"],
            "charge_micro_usd": artifact["telemetry"]["charge_micro_usd"],
            "files": files,
            "patch_sha256": artifact["patch_sha256"],
            "schema": OUTPUT_SCHEMA,
            "status": "valid",
        }
    finally:
        os.close(descriptor)


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser()
    value.add_argument("--attempt", required=True, type=Path)
    value.add_argument("--worktree", required=True, type=Path)
    value.add_argument("--policy", required=True, type=Path)
    value.add_argument("--lock", required=True, type=Path)
    value.add_argument("--expected-branch", required=True)
    value.add_argument("--base-sha", required=True)
    value.add_argument("--reserve-micro-usd", required=True, type=int)
    value.add_argument("--max-patch-bytes", type=int, default=MAX_PATCH)
    value.add_argument("action", choices=("validate", "apply"))
    return value


def main() -> None:
    try:
        result = handle(parser().parse_args())
        print(canonical(result))
    except (ArtifactError, OSError, subprocess.SubprocessError, ValueError) as error:
        print(canonical({"error": str(error), "schema": OUTPUT_SCHEMA, "status": "error"}))
        raise SystemExit(2)


if __name__ == "__main__":
    main()
