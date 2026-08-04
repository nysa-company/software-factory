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
import sys
import tarfile
import tempfile
from typing import Any

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
from release_lineage import successor_release_lineage  # noqa: E402
from legacy_closeout import ValidationError as TerminalError, protected_terminal  # noqa: E402
from qualification_artifacts import (  # noqa: E402
    ArtifactError as QualificationArtifactError,
    ensure_ticket as ensure_qualification_artifacts,
)


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


def qualification_capacity(product: Path) -> int:
    path = product / "factory/QUALIFICATION.json"
    if not path.exists():
        return 4
    value = json.loads(path.read_text(encoding="utf-8"))
    capacity = value.get("capacity")
    tickets = value.get("tickets")
    target = value.get("target_done")
    if (
        value.get("schema") != "nysa.software-factory.qualification/v2"
        or capacity not in (3, 4)
        or target not in (3, 4)
        or not isinstance(tickets, list)
        or len(tickets) != target
        or target > capacity
    ):
        raise EnvironmentError("qualification capacity is invalid")
    return capacity


def provider_configuration(
    release: Path, capacity: int = 4,
) -> tuple[dict[str, Any], dict[str, Any], str]:
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
    limit = {
        "max_concurrent": capacity,
        "max_starts": max(24, capacity * 6),
        "window_seconds": 60,
    }
    policy = {
        "account_routes": {
            route["account_route"]: limit for route in routes.values()
        },
        "coupled_max_concurrent": capacity,
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


def prepare_provider(release: Path, root: Path, capacity: int) -> str:
    policy, activation, policy_hash = provider_configuration(release, capacity)
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


def product_origin(product: Path) -> str:
    origins = command(
        "git", "-C", str(product), "remote", "get-url", "--push", "--all", "origin"
    ).splitlines()
    if len(origins) != 1 or not origins[0]:
        raise EnvironmentError("qualification product origin is ambiguous")
    return origins[0]


def certification_preflight(
    factory: Path, product: Path, sha: str, tree: str, contract: str,
) -> dict[str, str] | None:
    plan = product / "factory/certification-plan.json"
    if not plan.exists() and not plan.is_symlink():
        return None
    result = command(
        "/usr/bin/python3",
        str(factory / "scripts/certification-preflight.py"),
        "--plan", str(plan),
        "--factory-sha", sha,
        "--factory-tree", tree,
        "--product-root", str(product),
        "--contract-version", contract,
        cwd=product,
    )
    try:
        value = json.loads(result)
    except json.JSONDecodeError as error:
        raise EnvironmentError("qualification runtime preflight is malformed") from error
    runtime_tuple = value.get("runtime_tuple")
    if (
        value.get("schema") != "nysa.software-factory.certification-preflight/v1"
        or value.get("status") != "pass"
        or not isinstance(runtime_tuple, dict)
    ):
        raise EnvironmentError("qualification runtime preflight did not pass")
    return runtime_tuple


def bind_runtime_tuple(
    value: dict[str, Any], runtime_tuple: dict[str, str] | None,
) -> dict[str, Any]:
    if runtime_tuple is not None:
        value["runtime_tuple"] = runtime_tuple
    return value


def without_dependency_line(value: str) -> str:
    lines = value.splitlines()
    if sum(line.startswith("Depends-On:") for line in lines) != 1:
        raise EnvironmentError("qualification ticket dependency line is invalid")
    return "\n".join(line for line in lines if not line.startswith("Depends-On:"))


def validate_takeover_product(
    source_product: Path,
    product: Path,
    active: dict[str, Any],
    manifest: dict[str, Any],
) -> None:
    if command(
        "git", "-C", str(source_product), "status", "--porcelain", "--untracked-files=all"
    ):
        raise EnvironmentError("takeover source product must be clean")
    common = command(
        "git", "-C", str(product), "rev-parse", "--path-format=absolute", "--git-common-dir"
    )
    source_common = command(
        "git", "-C", str(source_product), "rev-parse", "--path-format=absolute", "--git-common-dir"
    )
    if common != source_common or product_origin(product) != product_origin(source_product):
        raise EnvironmentError(
            "takeover qualification product is not a linked canonical worktree"
        )

    protected = "refs/remotes/origin/main"
    source_sha = command("git", "-C", str(source_product), "rev-parse", "HEAD")
    source_tree = command(
        "git", "-C", str(source_product), "rev-parse", "HEAD^{tree}"
    )
    if active.get("product_tree") != source_tree:
        raise EnvironmentError("takeover source product does not match active product")
    try:
        command(
            "git", "-C", str(product), "merge-base", "--is-ancestor",
            source_sha, protected,
        )
    except EnvironmentError as error:
        raise EnvironmentError(
            "takeover protected main does not contain the active product"
        ) from error
    try:
        command(
            "git", "-C", str(product), "merge-base", "--is-ancestor", protected, "HEAD"
        )
    except EnvironmentError as error:
        raise EnvironmentError(
            "takeover qualification product is not based on protected main"
        ) from error

    statuses: dict[str, str] = {}
    raw_status = command(
        "git", "-C", str(product), "diff", "--name-status", "--no-renames",
        protected, "HEAD", "--",
    )
    for line in raw_status.splitlines():
        fields = line.split("\t")
        if len(fields) != 2 or fields[0] not in {"A", "M"}:
            raise EnvironmentError("takeover qualification control diff is invalid")
        statuses[fields[1]] = fields[0]
    qualification = "factory/QUALIFICATION.json"
    pin = "factory/KIT_PIN"
    ticket_paths = {f"factory/tickets/{ticket}.md" for ticket in manifest["tickets"]}
    if (
        statuses.get(qualification) not in {"A", "M"}
        or statuses.get(pin) != "M"
        or not set(statuses).issubset({qualification, pin, *ticket_paths})
    ):
        raise EnvironmentError("takeover qualification changes non-control product files")
    for path in ticket_paths & statuses.keys():
        if statuses[path] != "M" or without_dependency_line(command(
            "git", "-C", str(product), "show", f"{protected}:{path}"
        )) != without_dependency_line(command(
            "git", "-C", str(product), "show", f"HEAD:{path}"
        )):
            raise EnvironmentError(
                "takeover qualification changes a ticket beyond dependency ordering"
            )


def operator_map(source_product: Path) -> str:
    source = source_product / "factory/linear-map.json"
    read(source)
    return str(source)


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
        or manifest.get("contract_version") != "1.8.0"
        or manifest.get("factory_sha")
        != command("git", "-C", str(factory), "rev-parse", "HEAD")
        or not SHA.fullmatch(manifest.get("source_factory_sha", ""))
        or manifest.get("source_factory_sha") == manifest.get("factory_sha")
        or not isinstance(manifest.get("generation"), int)
        or isinstance(manifest.get("generation"), bool)
        or manifest.get("generation", 0) < 1
        or not isinstance(manifest.get("tickets"), list)
        or len(manifest["tickets"]) != 3
        or len(set(manifest["tickets"])) != 3
        or any(
            not isinstance(ticket, str) or not re.fullmatch(r"T-[0-9]+", ticket)
            for ticket in manifest["tickets"]
        )
    ):
        raise EnvironmentError("takeover qualification manifest is invalid")

    kits = safe_directory(Path.home().resolve(strict=True) / ".factory/kits")
    source = safe_directory(kits / f"projects/{source_project}")
    state = safe_directory(source / "controller")
    active = read(source / "active.json")
    source_product_path = active.get("product_path")
    if (
        active.get("project") != source_project
        or not isinstance(source_product_path, str)
        or not Path(source_product_path).is_absolute()
        or active.get("contract_version") != "1.8.0"
        or active.get("kit_sha") != manifest["source_factory_sha"]
        or not SHA.fullmatch(active.get("kit_tree", ""))
        or not SHA.fullmatch(active.get("product_tree", ""))
    ):
        raise EnvironmentError("takeover source activation does not match the manifest")
    source_product = Path(source_product_path).resolve(strict=True)
    validate_takeover_product(source_product, product, active, manifest)
    operator_map_path = operator_map(source_product)
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
        if any((source_product / "factory/.active-runs").glob("*")) or any(
            (source_product / "factory/runs").glob("*.pid")
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
                or value.get("project") != project
                or not successor_release_lineage(
                    value.get("factory_release_history"),
                    value.get("migration_history"),
                    manifest["source_factory_sha"],
                    value.get("factory_sha", ""),
                    passport.valid_v2_migration,
                )
            ):
                raise EnvironmentError("takeover passport does not match the source")
        for ticket in manifest["tickets"]:
            try:
                protected_terminal(product, ticket)
            except TerminalError:
                ensure_qualification_artifacts(
                    product, state, ticket, sources=(source_product,)
                )
    except QualificationArtifactError as error:
        raise EnvironmentError(str(error)) from error
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
        "operator_map_path": operator_map_path,
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
    product_sha = command("git", "-C", str(product), "rev-parse", "HEAD")
    runtime_tuple = certification_preflight(
        factory, product, sha, tree, contract,
    )
    origin = product_origin(product)
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
    if not takeover:
        controller = project / "controller"
        if controller.exists():
            safe_directory(controller)
        else:
            controller.mkdir(mode=0o700)
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
        if takeover else prepare_provider(
            release, root, qualification_capacity(product)
        )
    )
    qualification_mode = takeover["mode"] if takeover else "isolated"

    receipt_value = bind_runtime_tuple({
        "contract_version": contract,
        "kit_sha": sha,
        "kit_tree": tree,
        "product_origin": origin,
        "product_path": str(product),
        "product_sha": product_sha,
        "product_tree": product_tree,
        "project": args.project,
        "provider_policy_sha256": provider_policy_sha256,
        "qualification_mode": qualification_mode,
        "status": "pass",
    }, runtime_tuple)
    if takeover:
        receipt_value["operator_map_path"] = takeover["operator_map_path"]
        receipt_value["takeover_kits_root"] = takeover["takeover_kits_root"]
    receipt_id = hashlib.sha256(canonical(receipt_value)).hexdigest()
    receipt_value["receipt_id"] = receipt_id
    write(receipts / f"{receipt_id}.json", receipt_value)
    active_value = bind_runtime_tuple({
        "contract_version": contract,
        "generation": 1,
        "kit_sha": sha,
        "kit_tree": tree,
        "product_path": str(product),
        "product_sha": product_sha,
        "product_tree": product_tree,
        "project": args.project,
        "provider_policy_sha256": provider_policy_sha256,
        "qualification_mode": qualification_mode,
        "receipt_id": receipt_id,
        "release_path": str(release),
    }, runtime_tuple)
    if takeover:
        active_value["operator_map_path"] = takeover["operator_map_path"]
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
    result = bind_runtime_tuple({
        "factory_sha": sha,
        "factory_tree": tree,
        "launcher": str(release / "integrations/hermes/bin/factory-launch"),
        "product_sha": product_sha,
        "product_tree": product_tree,
        "project": args.project,
        "provider_policy_sha256": provider_policy_sha256,
        "qualification_mode": qualification_mode,
        "root": str(root),
        "schema": SCHEMA,
        "status": "prepared",
    }, runtime_tuple)
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
    product_sha = command("git", "-C", str(product), "rev-parse", "HEAD")
    product_tree = command("git", "-C", str(product), "rev-parse", "HEAD^{tree}")
    runtime_tuple = certification_preflight(
        factory, product, sha, tree, contract,
    )

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
        policy, activation, policy_hash = provider_configuration(
            release, qualification_capacity(product)
        )
        if (
            read(root / "provider/provider-policy.json") != policy
            or read(root / "provider/provider-activation.json") != activation
        ):
            raise EnvironmentError("successor changes the active provider policy")

        origins = command(
            "git", "-C", str(product), "remote", "get-url", "--push", "--all", "origin"
        ).splitlines()
        if len(origins) != 1 or not origins[0]:
            raise EnvironmentError("qualification product origin is ambiguous")
        receipt_value = bind_runtime_tuple({
            "contract_version": contract,
            "kit_sha": sha,
            "kit_tree": tree,
            "previous_receipt_id": active.get("receipt_id"),
            "product_origin": origins[0],
            "product_path": str(product),
            "product_sha": product_sha,
            "product_tree": product_tree,
            "project": args.project,
            "provider_policy_sha256": policy_hash,
            "qualification_mode": qualification_mode,
            "status": "pass",
        }, runtime_tuple)
        receipt_id = hashlib.sha256(canonical(receipt_value)).hexdigest()
        receipt_value["receipt_id"] = receipt_id
        receipt = root / f"receipts/{receipt_id}.json"
        if receipt.exists():
            if read(receipt) != receipt_value:
                raise EnvironmentError("successor receipt conflicts")
        else:
            write(receipt, receipt_value)
        generation = active["generation"] + (active["kit_sha"] != sha)
        next_active = bind_runtime_tuple({
            "contract_version": contract,
            "generation": generation,
            "kit_sha": sha,
            "kit_tree": tree,
            "product_path": str(product),
            "product_sha": product_sha,
            "product_tree": product_tree,
            "project": args.project,
            "provider_policy_sha256": policy_hash,
            "qualification_mode": qualification_mode,
            "receipt_id": receipt_id,
            "release_path": str(release),
        }, runtime_tuple)
        replace(active_path, next_active)
        result = bind_runtime_tuple({
            "factory_sha": sha,
            "factory_tree": tree,
            "launcher": str(release / "integrations/hermes/bin/factory-launch"),
            "product_sha": product_sha,
            "product_tree": product_tree,
            "project": args.project,
            "provider_policy_sha256": policy_hash,
            "qualification_mode": qualification_mode,
            "root": str(root),
            "schema": SCHEMA,
            "status": "upgraded",
        }, runtime_tuple)
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
