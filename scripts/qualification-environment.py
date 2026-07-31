#!/usr/bin/env python3
"""Prepare one sealed, non-production Contract 1.8 qualification release."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib.util
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
CURSOR_DATA_PATH_LIMIT = 75
CURSOR_ATTEMPT_PLACEHOLDER = "0000000000-0000000-cli"


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


def replace(path: Path, value: dict[str, Any]) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(canonical(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        Path(temporary).unlink(missing_ok=True)


def read(path: Path) -> dict[str, Any]:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_size > 131_072
        ):
            raise EnvironmentError("qualification state file is unsafe")
        with os.fdopen(descriptor, encoding="utf-8") as stream:
            descriptor = -1
            value = json.load(stream)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not isinstance(value, dict):
        raise EnvironmentError("qualification state file is malformed")
    return value


def provider_configuration(release: Path) -> tuple[dict[str, Any], dict[str, Any], str]:
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
    activation = {
        "enabled": True,
        "mode": "cli-concurrent-v1",
        "policy_sha256": policy_hash,
        "routes": routes,
        "schema": ACTIVATION_SCHEMA,
    }
    return policy, activation, policy_hash


def prepare_provider(release: Path, root: Path) -> str:
    policy, activation, policy_hash = provider_configuration(release)
    provider = root / "provider"
    provider.mkdir(mode=0o700)
    for name in (
        "accounting",
        "cli-runtimes",
        "provider-attempts",
        "provider-apply-locks",
    ):
        (provider / name).mkdir(mode=0o700)
    configuration_lock = provider / "provider-configuration.lock"
    descriptor = os.open(
        configuration_lock,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    os.close(descriptor)
    policy_path = provider / "provider-policy.json"
    activation_path = provider / "provider-activation.json"
    write(policy_path, policy)
    write(activation_path, activation)
    command(
        "/usr/bin/python3",
        str(release / "scripts/provider-activation.py"),
        "--config", str(activation_path),
        "--policy", str(policy_path),
        "--contract-version", "1.8.0",
        "--status",
    )
    command(
        "/usr/bin/python3",
        str(release / "scripts/provider-coordinator.py"),
        "--db",
        str(provider / "accounting/state-v2.sqlite3"),
        "status",
    )
    return policy_hash


def takeover_source(
    factory: Path, product: Path, project: str, source_project: str | None,
) -> dict[str, str] | None:
    if source_project is None:
        return None
    if source_project != project:
        raise EnvironmentError("qualification takeover project must match the source")
    try:
        manifest = json.loads(
            (product / "factory/QUALIFICATION.json").read_text(encoding="utf-8")
        )
    except (FileNotFoundError, json.JSONDecodeError) as error:
        raise EnvironmentError("takeover qualification manifest is unavailable") from error
    if (
        manifest.get("schema") != "nysa.software-factory.qualification/v2"
        or manifest.get("mode") != "successor"
        or manifest.get("capacity") != 3
        or manifest.get("target_done") != 3
        or manifest.get("budget_usd") != "300.000000"
        or manifest.get("per_ticket_budget_usd") != "100.000000"
        or manifest.get("per_run_budget_usd") != "10.000000"
        or not SHA.fullmatch(manifest.get("source_factory_sha", ""))
        or not isinstance(manifest.get("tickets"), list)
        or len(manifest["tickets"]) != 3
    ):
        raise EnvironmentError("takeover qualification manifest is invalid")

    kits = safe_directory(Path.home().resolve(strict=True) / ".factory/kits")
    source = safe_directory(kits / f"projects/{source_project}")
    state = safe_directory(source / "controller")
    active = read(source / "active.json")
    if (
        active.get("project") != source_project
        or active.get("product_path") != str(product)
        or active.get("contract_version") != "1.8.0"
        or active.get("kit_sha") != manifest["source_factory_sha"]
        or not SHA.fullmatch(active.get("kit_tree", ""))
    ):
        raise EnvironmentError("takeover source activation does not match the manifest")
    lock = os.open(
        state / "reconcile.lock",
        os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise EnvironmentError("takeover source controller is active") from error
        if any((product / "factory/.active-runs").glob("*")) or any(
            (product / "factory/runs").glob("*.pid")
        ):
            raise EnvironmentError("takeover source has an active provider run")
        passport_spec = importlib.util.spec_from_file_location(
            "qualification_takeover_passport", factory / "scripts/ticket-passport.py"
        )
        if not passport_spec or not passport_spec.loader:
            raise EnvironmentError("takeover passport verifier is unavailable")
        passport = importlib.util.module_from_spec(passport_spec)
        passport_spec.loader.exec_module(passport)
        if not (state / "passport.key").is_file():
            raise EnvironmentError("takeover passport key is unavailable")
        secret = passport.key(state)
        for ticket in manifest["tickets"]:
            value, _ = passport.load_passport(
                state / f"passports/{ticket}.json", secret
            )
            if (
                value.get("ticket") != ticket
                or value.get("factory_sha") != manifest["source_factory_sha"]
                or value.get("project") != project
            ):
                raise EnvironmentError("takeover passport does not match the source")
    except (AttributeError, OSError, ValueError) as error:
        if isinstance(error, EnvironmentError):
            raise
        raise EnvironmentError("takeover source state is invalid") from error
    finally:
        os.close(lock)

    provider = safe_directory(Path.home().resolve(strict=True) / ".factory")
    activation_path = provider / "isolated-v1.enabled"
    policy_path = provider / "provider-policy.json"
    activation = read(activation_path)
    policy_hash = activation.get("policy_sha256", "")
    if not re.fullmatch(r"[0-9a-f]{64}", policy_hash):
        raise EnvironmentError("takeover provider activation is invalid")
    command(
        "/usr/bin/python3",
        str(factory / "scripts/provider-activation.py"),
        "--config", str(activation_path),
        "--policy", str(policy_path),
        "--contract-version", "1.8.0",
        "--status",
    )
    provider_status = json.loads(command(
        "/usr/bin/python3",
        str(factory / "scripts/provider-coordinator.py"),
        "--db", str(provider / "accounting/state-v2.sqlite3"),
        "status",
    ))
    attempts = provider_status.get("attempts")
    if (
        not isinstance(attempts, list)
        or provider_status.get("active_reserve_micro_usd") != 0
        or provider_status.get("legacy_intervals") != []
        or any(
            not isinstance(item, dict) or item.get("state") != "terminal"
            for item in attempts
        )
    ):
        raise EnvironmentError("takeover provider state is not drained")
    return {
        "mode": "takeover",
        "provider_policy_sha256": policy_hash,
        "takeover_kits_root": str(kits),
    }


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
    if len(str(root / "c" / CURSOR_ATTEMPT_PLACEHOLDER / "data")) > CURSOR_DATA_PATH_LIMIT:
        raise EnvironmentError(
            "qualification root is too long for isolated Cursor scratch"
        )
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
    takeover = takeover_source(
        factory, product, args.project, getattr(args, "takeover_project", None)
    )

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
    provider_policy_sha256 = (
        takeover["provider_policy_sha256"]
        if takeover else prepare_provider(release, root)
    )
    qualification_mode = takeover["mode"] if takeover else "isolated"

    receipt_value = {
        "contract_version": contract,
        "kit_sha": sha,
        "kit_tree": tree,
        "product_origin": origin,
        "product_path": str(product),
        "product_tree": product_tree,
        "project": args.project,
        "provider_policy_sha256": provider_policy_sha256,
        "qualification_mode": qualification_mode,
        "status": "pass",
    }
    if takeover:
        receipt_value["takeover_kits_root"] = takeover["takeover_kits_root"]
    receipt_id = hashlib.sha256(canonical(receipt_value)).hexdigest()
    receipt_value["receipt_id"] = receipt_id
    write(receipts / f"{receipt_id}.json", receipt_value)
    active_value = {
        "contract_version": contract,
        "generation": 1,
        "kit_sha": sha,
        "kit_tree": tree,
        "product_path": str(product),
        "product_tree": product_tree,
        "project": args.project,
        "provider_policy_sha256": provider_policy_sha256,
        "qualification_mode": qualification_mode,
        "receipt_id": receipt_id,
        "release_path": str(release),
    }
    if takeover:
        active_value["takeover_kits_root"] = takeover["takeover_kits_root"]
    write(active, active_value)
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
        "qualification_mode": qualification_mode,
        "root": str(root),
        "schema": SCHEMA,
        "status": "prepared",
    }
    write(root / "environment.json", result)
    return result


def upgrade(args: argparse.Namespace) -> dict[str, Any]:
    root = Path(os.path.realpath(args.root))
    if not ROOT.fullmatch(str(root)):
        raise EnvironmentError("qualification root must be under /private/tmp")
    safe_directory(root)
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

    marker = read(root / "marker.json")
    active_path = root / f"projects/{args.project}/active.json"
    active = read(active_path)
    qualification_mode = active.get("qualification_mode")
    if (
        marker != {"mode": "qualification", "schema": SCHEMA}
        or active.get("project") != args.project
        or active.get("product_path") != str(product)
        or active.get("contract_version") != contract
        or not SHA.fullmatch(active.get("kit_sha", ""))
        or not isinstance(active.get("generation"), int)
        or isinstance(active.get("generation"), bool)
        or active["generation"] < 1
        or qualification_mode not in {"isolated", "takeover"}
    ):
        raise EnvironmentError("existing qualification activation is invalid")
    if qualification_mode == "takeover":
        raise EnvironmentError("takeover qualification requires one frozen candidate")

    controller = safe_directory(root / f"projects/{args.project}/controller")
    lock = os.open(
        controller / "reconcile.lock",
        os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise EnvironmentError("qualification controller is active") from error
        if any((product / "factory/.active-runs").glob("*")) or any(
            (product / "factory/runs").glob("*.pid")
        ):
            raise EnvironmentError("qualification has an active provider run")

        releases = safe_directory(root / "releases")
        release = releases / sha
        if release.exists():
            if git_tree(release) != tree:
                raise EnvironmentError("existing successor release tree is invalid")
        else:
            materialize(factory, sha, release)
        if git_tree(release) != tree:
            raise EnvironmentError("sealed qualification tree does not match the candidate")
        policy, activation, policy_hash = provider_configuration(release)
        if (
            read(root / "provider/provider-policy.json") != policy
            or read(root / "provider/provider-activation.json") != activation
        ):
            raise EnvironmentError("successor changes the active provider policy")

        product_tree = command("git", "-C", str(product), "rev-parse", "HEAD^{tree}")
        origins = command(
            "git", "-C", str(product), "remote", "get-url", "--push", "--all", "origin"
        ).splitlines()
        if len(origins) != 1 or not origins[0]:
            raise EnvironmentError("qualification product origin is ambiguous")
        receipt_value = {
            "contract_version": contract,
            "kit_sha": sha,
            "kit_tree": tree,
            "previous_receipt_id": active.get("receipt_id"),
            "product_origin": origins[0],
            "product_path": str(product),
            "product_tree": product_tree,
            "project": args.project,
            "provider_policy_sha256": policy_hash,
            "qualification_mode": qualification_mode,
            "status": "pass",
        }
        receipt_id = hashlib.sha256(canonical(receipt_value)).hexdigest()
        receipt_value["receipt_id"] = receipt_id
        receipt = root / f"receipts/{receipt_id}.json"
        if receipt.exists():
            if read(receipt) != receipt_value:
                raise EnvironmentError("successor receipt conflicts")
        else:
            write(receipt, receipt_value)
        generation = active["generation"] + (active["kit_sha"] != sha)
        next_active = {
            "contract_version": contract,
            "generation": generation,
            "kit_sha": sha,
            "kit_tree": tree,
            "product_path": str(product),
            "product_tree": product_tree,
            "project": args.project,
            "provider_policy_sha256": policy_hash,
            "qualification_mode": qualification_mode,
            "receipt_id": receipt_id,
            "release_path": str(release),
        }
        replace(active_path, next_active)
        result = {
            "factory_sha": sha,
            "factory_tree": tree,
            "launcher": str(release / "integrations/hermes/bin/factory-launch"),
            "product_tree": product_tree,
            "project": args.project,
            "provider_policy_sha256": policy_hash,
            "qualification_mode": qualification_mode,
            "root": str(root),
            "schema": SCHEMA,
            "status": "upgraded",
        }
        replace(root / "environment.json", result)
        return result
    finally:
        os.close(lock)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--factory-root", required=True, type=Path)
    parser.add_argument("--product-root", required=True, type=Path)
    parser.add_argument("--project", required=True)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--takeover-project")
    parser.add_argument("--upgrade", action="store_true")
    args = parser.parse_args()
    try:
        if not PROJECT.fullmatch(args.project):
            raise EnvironmentError("invalid qualification project")
        print(json.dumps(upgrade(args) if args.upgrade else prepare(args), sort_keys=True))
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
