#!/usr/bin/env python3
"""Prepare one sealed, non-production Contract 1.8 qualification release."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import tarfile
import tempfile
from typing import Any


SCHEMA = "nysa.software-factory.qualification-environment/v1"
ACTIVATION_SCHEMA = "nysa.software-factory.provider-activation/v2"
POLICY_SCHEMA = "factory-provider-concurrency-policy/v1"
SHA = re.compile(r"^[0-9a-f]{40}$")
PROJECT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
ROOT = re.compile(r"^/private/tmp/nysa-sf-qualification\.[A-Za-z0-9._-]+$")


class EnvironmentError(ValueError):
    pass


def command(*arguments: str, cwd: Path | None = None) -> str:
    result = subprocess.run(
        arguments, cwd=cwd, text=True, capture_output=True, check=False, timeout=120,
    )
    if result.returncode:
        raise EnvironmentError(
            result.stderr.strip() or result.stdout.strip() or "command failed"
        )
    return result.stdout.strip()


def canonical(value: dict[str, Any]) -> bytes:
    return (
        json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode()


def safe_directory(path: Path, create: bool = False) -> Path:
    if create:
        path.mkdir(mode=0o700, parents=False, exist_ok=False)
    info = path.lstat()
    if (
        path.resolve(strict=True) != path
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise EnvironmentError("qualification root is unsafe")
    return path


def write(path: Path, value: dict[str, Any]) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(canonical(value))
        stream.flush()
        os.fsync(stream.fileno())


def prepare_provider(release: Path, root: Path) -> str:
    catalog = json.loads(
        (release / "scripts/model-routing/catalog-v1.json").read_text(
            encoding="utf-8"
        )
    )
    routes = {
        route["route_id"]: {
            "account_route": route["account_route_id"],
            "adapter": route["adapter"],
            "model": route["selection_id"],
            "provider_family": route["provider_family"],
        }
        for route in catalog["routes"]
        if route["enabled"]
    }
    if not routes:
        raise EnvironmentError("qualification provider catalog has no enabled route")
    limit = {"max_concurrent": 4, "max_starts": 24, "window_seconds": 60}
    policy = {
        "account_routes": {
            route["account_route"]: limit for route in routes.values()
        },
        "coupled_max_concurrent": 4,
        "global": limit,
        "provider_families": {
            route["provider_family"]: limit for route in routes.values()
        },
        "schema": POLICY_SCHEMA,
    }
    policy_raw = canonical(policy).rstrip(b"\n")
    policy_hash = hashlib.sha256(policy_raw).hexdigest()
    provider = root / "provider"
    provider.mkdir(mode=0o700)
    for name in ("accounting", "provider-attempts", "provider-apply-locks"):
        (provider / name).mkdir(mode=0o700)
    policy_path = provider / "provider-policy.json"
    activation_path = provider / "provider-activation.json"
    write(policy_path, policy)
    write(activation_path, {
        "enabled": True,
        "mode": "cli-concurrent-v1",
        "policy_sha256": policy_hash,
        "routes": routes,
        "schema": ACTIVATION_SCHEMA,
    })
    command(
        "/usr/bin/python3",
        str(release / "scripts/provider-activation.py"),
        "--config", str(activation_path),
        "--policy", str(policy_path),
        "--contract-version", "1.8.0",
        "--status",
    )
    return policy_hash


def git_tree(path: Path) -> str:
    with tempfile.TemporaryDirectory(prefix="qualification-tree.") as raw:
        repository = Path(raw) / "repo.git"
        index = Path(raw) / "index"
        command("git", "init", "--bare", "-q", str(repository))
        command("git", "--git-dir", str(repository), "config", "core.bare", "false")
        environment = {**os.environ, "GIT_INDEX_FILE": str(index)}
        for arguments in (
            ("read-tree", "--empty"),
            ("add", "-f", "-A", "--", "."),
        ):
            result = subprocess.run(
                ["git", "--git-dir", str(repository), "--work-tree", str(path), *arguments],
                text=True, capture_output=True, check=False, env=environment, timeout=120,
            )
            if result.returncode:
                raise EnvironmentError(result.stderr.strip() or "tree inspection failed")
        result = subprocess.run(
            ["git", "--git-dir", str(repository), "--work-tree", str(path), "write-tree"],
            text=True, capture_output=True, check=False, env=environment, timeout=120,
        )
        if result.returncode:
            raise EnvironmentError(result.stderr.strip() or "tree inspection failed")
        return result.stdout.strip()


def materialize(factory: Path, sha: str, release: Path) -> None:
    archive = subprocess.run(
        ["git", "-C", str(factory), "archive", "--format=tar", sha],
        capture_output=True, check=False, timeout=120,
    )
    if archive.returncode:
        raise EnvironmentError(archive.stderr.decode(errors="replace").strip())
    release.mkdir(mode=0o700)
    with tarfile.open(fileobj=io.BytesIO(archive.stdout), mode="r:") as bundle:
        members = bundle.getmembers()
        for member in members:
            name = PurePosixPath(member.name)
            if name.is_absolute() or ".." in name.parts:
                raise EnvironmentError("candidate archive path is unsafe")
            if member.issym() or member.islnk():
                target = name.parent / member.linkname
                if PurePosixPath(member.linkname).is_absolute() or ".." in target.parts:
                    raise EnvironmentError("candidate archive link is unsafe")
        bundle.extractall(release)
    for base, directories, files in os.walk(release, topdown=False, followlinks=False):
        for name in files:
            path = Path(base) / name
            if not path.is_symlink():
                path.chmod(0o555 if path.stat().st_mode & 0o111 else 0o444)
        for name in directories:
            path = Path(base) / name
            if not path.is_symlink():
                path.chmod(0o555)
    release.chmod(0o555)


def prepare(args: argparse.Namespace) -> dict[str, Any]:
    root = Path(os.path.realpath(args.root))
    if not ROOT.fullmatch(str(root)):
        raise EnvironmentError("qualification root must be under /private/tmp")
    factory = args.factory_root.resolve(strict=True)
    product = args.product_root.resolve(strict=True)
    if command("git", "-C", str(factory), "status", "--porcelain", "--untracked-files=all"):
        raise EnvironmentError("Factory candidate must be clean")
    if command("git", "-C", str(product), "status", "--porcelain", "--untracked-files=all"):
        raise EnvironmentError("qualification product must be clean")
    sha = command("git", "-C", str(factory), "rev-parse", "HEAD")
    tree = command("git", "-C", str(factory), "rev-parse", "HEAD^{tree}")
    if not SHA.fullmatch(sha) or not SHA.fullmatch(tree):
        raise EnvironmentError("Factory candidate identity is invalid")
    if (product / "factory/KIT_PIN").read_text(encoding="utf-8") != sha + "\n":
        raise EnvironmentError("qualification product is not pinned to the candidate")
    contract = json.loads(
        (factory / "integrations/hermes/contract.json").read_text(encoding="utf-8")
    ).get("contract_version")
    if contract != "1.8.0":
        raise EnvironmentError("qualification requires Contract 1.8.0")
    product_tree = command("git", "-C", str(product), "rev-parse", "HEAD^{tree}")
    origins = command(
        "git", "-C", str(product), "remote", "get-url", "--push", "--all", "origin"
    ).splitlines()
    if len(origins) != 1 or not origins[0]:
        raise EnvironmentError("qualification product origin is ambiguous")
    origin = origins[0]

    safe_directory(root, create=not root.exists())
    releases = root / "releases"
    projects = root / "projects"
    receipts = root / "receipts"
    profile = root / "profile"
    profile_projects = profile / "projects"
    for path in (releases, projects, receipts, profile):
        if path.exists():
            safe_directory(path)
        else:
            path.mkdir(mode=0o700)
    if profile_projects.exists():
        safe_directory(profile_projects)
    else:
        profile_projects.mkdir(mode=0o700)
    project = projects / args.project
    if project.exists():
        safe_directory(project)
    else:
        project.mkdir(mode=0o700)
    release = releases / sha
    active = project / "active.json"
    if release.exists() or active.exists():
        raise EnvironmentError("qualification environment already exists")
    write(root / "marker.json", {
        "mode": "qualification",
        "schema": SCHEMA,
    })
    materialize(factory, sha, release)
    if git_tree(release) != tree:
        raise EnvironmentError("sealed qualification tree does not match the candidate")
    provider_policy_sha256 = prepare_provider(release, root)

    receipt_value = {
        "contract_version": contract,
        "kit_sha": sha,
        "kit_tree": tree,
        "product_origin": origin,
        "product_path": str(product),
        "product_tree": product_tree,
        "project": args.project,
        "provider_policy_sha256": provider_policy_sha256,
        "status": "pass",
    }
    receipt_id = hashlib.sha256(canonical(receipt_value)).hexdigest()
    receipt_value["receipt_id"] = receipt_id
    write(receipts / f"{receipt_id}.json", receipt_value)
    write(active, {
        "contract_version": contract,
        "generation": 1,
        "kit_sha": sha,
        "kit_tree": tree,
        "product_path": str(product),
        "product_tree": product_tree,
        "project": args.project,
        "provider_policy_sha256": provider_policy_sha256,
        "receipt_id": receipt_id,
        "release_path": str(release),
    })
    registry = profile_projects / f"{args.project}.env"
    descriptor = os.open(
        registry,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(f"PRODUCT_ROOT={product}\n")
        stream.flush()
        os.fsync(stream.fileno())
    result = {
        "factory_sha": sha,
        "factory_tree": tree,
        "launcher": str(release / "integrations/hermes/bin/factory-launch"),
        "product_tree": product_tree,
        "project": args.project,
        "provider_policy_sha256": provider_policy_sha256,
        "root": str(root),
        "schema": SCHEMA,
        "status": "prepared",
    }
    write(root / "environment.json", result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--factory-root", required=True, type=Path)
    parser.add_argument("--product-root", required=True, type=Path)
    parser.add_argument("--project", required=True)
    parser.add_argument("--root", required=True, type=Path)
    args = parser.parse_args()
    try:
        if not PROJECT.fullmatch(args.project):
            raise EnvironmentError("invalid qualification project")
        print(json.dumps(prepare(args), sort_keys=True))
    except (
        FileNotFoundError, json.JSONDecodeError, OSError, EnvironmentError,
        subprocess.SubprocessError, tarfile.TarError,
    ) as error:
        print(json.dumps({
            "error": str(error), "schema": SCHEMA, "status": "error",
        }, sort_keys=True))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
