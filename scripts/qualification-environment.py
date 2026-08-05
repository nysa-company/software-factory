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
AUTHORITY_SCHEMA = "nysa.software-factory.qualification-authority/v1"
ACTIVATION_SCHEMA = "nysa.software-factory.provider-activation/v2"
POLICY_SCHEMA = "factory-provider-concurrency-policy/v1"
SHA = re.compile(r"^[0-9a-f]{40}$")
PROJECT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
ROOT = re.compile(r"^/private/tmp/nysa-sf-qualification\.[A-Za-z0-9._-]+$")
FACTORY_ISSUE = re.compile(
    r"^https://github[.]com/[A-Za-z0-9_.-]+/software-factory/issues/[1-9][0-9]*$"
)
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


def authority_root(project: str, create: bool = False) -> Path:
    factory = Path.home().resolve(strict=True) / ".factory"
    safe_directory(factory)
    qualification = factory / "qualification"
    if not qualification.exists():
        qualification.mkdir(mode=0o700)
    safe_directory(qualification)
    root = qualification / project
    if create:
        if root.exists() or root.is_symlink():
            raise EnvironmentError("qualification environment already exists")
        safe_directory(root, create=True)
    else:
        safe_directory(root)
    return root


def authority_identity(
    project: str,
    factory_sha: str,
    factory_tree: str,
    product: Path,
    product_sha: str,
    product_tree: str,
    product_origin_value: str,
    runtime_tuple: dict[str, str] | None,
) -> dict[str, Any]:
    manifest = product / "factory/QUALIFICATION.json"
    value = {
        "contract_version": "1.8.0",
        "controller_state_path": str(
            Path.home().resolve(strict=True)
            / ".factory/qualification" / project / "controller"
        ),
        "factory_sha": factory_sha,
        "factory_tree": factory_tree,
        "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
        "product_origin": product_origin_value,
        "product_path": str(product),
        "product_sha": product_sha,
        "product_tree": product_tree,
        "project": project,
        "provider_state_path": str(
            Path.home().resolve(strict=True)
            / ".factory/qualification" / project / "provider"
        ),
        "runtime_tuple": runtime_tuple or {},
        "schema": AUTHORITY_SCHEMA,
    }
    value["authority_sha256"] = hashlib.sha256(canonical(value)).hexdigest()
    return value


def validate_paused_authority(
    factory: Path, product: Path, controller: Path, identity: dict[str, Any],
) -> None:
    claims = controller / "claims"
    if claims.is_dir() and any(claims.glob("T-*.json")):
        raise EnvironmentError("qualification restore requires paused claims")
    pauses = sorted(controller.glob("pause-T-*.json"))
    if not pauses or not (controller / "passport.key").is_file():
        raise EnvironmentError("qualification restore requires a signed safe pause")
    spec = importlib.util.spec_from_file_location(
        "qualification_restore_passport", factory / "scripts/ticket-passport.py"
    )
    if not spec or not spec.loader:
        raise EnvironmentError("qualification restore passport verifier is unavailable")
    passport = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(passport)
    secret = passport.key(controller)
    for pause_path in pauses:
        intent = read(pause_path)
        ticket = intent.get("ticket", "")
        worktree = Path(intent.get("worktree", ""))
        passport_path = controller / "passports" / f"{ticket}.json"
        signed_intent = dict(intent)
        pause_digest = signed_intent.pop("pause_sha256", "")
        if (
            intent.get("schema") != "nysa.software-factory.ticket-pause/v2"
            or not re.fullmatch(r"T-[0-9]+", ticket)
            or intent.get("factory_sha") != identity["factory_sha"]
            or not FACTORY_ISSUE.fullmatch(intent.get("blocking_issue", ""))
            or not worktree.is_absolute()
            or not passport_path.is_file()
            or pause_digest != hashlib.sha256(json.dumps(
                signed_intent, ensure_ascii=True, sort_keys=True,
                separators=(",", ":"),
            ).encode()).hexdigest()
        ):
            raise EnvironmentError("qualification safe-pause evidence is invalid")
        value, _ = passport.load_passport(passport_path, secret)
        selected = []
        for path in sorted((product / "factory/runs").glob("*.meta")):
            fields = dict(
                line.split("=", 1)
                for line in path.read_text(encoding="utf-8").splitlines()
                if "=" in line
            )
            if fields.get("ticket") == ticket:
                selected.append((
                    path.name, hashlib.sha256(path.read_bytes()).hexdigest(),
                ))
        run_snapshot = hashlib.sha256(json.dumps(
            selected, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
        ).encode()).hexdigest()
        if (
            value.get("ticket") != ticket
            or value.get("factory_sha") != identity["factory_sha"]
            or value.get("head_sha") != intent.get("head_sha")
            or value.get("current_state") != intent.get("current_state")
            or value.get("current_stage") != intent.get("current_stage")
            or value.get("passport_sha256") != intent.get("passport_sha256")
            or intent.get("run_snapshot_sha256") != run_snapshot
            or not worktree.is_dir()
            or command("git", "-C", str(worktree), "status", "--porcelain=v1", "-z")
            or command("git", "-C", str(worktree), "symbolic-ref", "--short", "HEAD")
            != intent.get("branch")
            or command("git", "-C", str(worktree), "rev-parse", "HEAD")
            != intent.get("head_sha")
        ):
            raise EnvironmentError("qualification safe-pause evidence changed")


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


def prepare_product_runtime(product: Path, create: bool = True) -> None:
    """Create the one ignored runtime root a clean worktree cannot contain."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    factory = os.open(product / "factory", flags)
    try:
        parent = os.fstat(factory)
        if parent.st_uid != os.geteuid() or stat.S_IMODE(parent.st_mode) & 0o022:
            raise EnvironmentError("qualification product factory directory is unsafe")
        try:
            value = os.stat("runs", dir_fd=factory, follow_symlinks=False)
        except FileNotFoundError:
            if not create:
                return
            os.mkdir("runs", 0o700, dir_fd=factory)
            os.fsync(factory)
            value = os.stat("runs", dir_fd=factory, follow_symlinks=False)
        if (
            not stat.S_ISDIR(value.st_mode)
            or value.st_uid != os.geteuid()
            or stat.S_IMODE(value.st_mode) != 0o700
        ):
            raise EnvironmentError("qualification product factory/runs is unsafe")
        runs = os.open("runs", flags, dir_fd=factory)
        try:
            os.fsync(runs)
        finally:
            os.close(runs)
    finally:
        os.close(factory)


def validate_selected_contracts(product: Path) -> None:
    """Reject non-canonical metadata and dependent qualification cohorts early."""
    manifest = json.loads(
        (product / "factory/QUALIFICATION.json").read_text(encoding="utf-8")
    )
    selected = manifest.get("tickets")
    if not isinstance(selected, list) or any(
        not isinstance(ticket, str) or not re.fullmatch(r"T-[0-9]+", ticket)
        for ticket in selected
    ):
        raise EnvironmentError("qualification tickets are invalid")
    cohort = set(selected)
    for ticket in selected:
        path = product / "factory/tickets" / f"{ticket}.md"
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as error:
            raise EnvironmentError(f"qualification ticket is unavailable: {path}") from error

        def values(name: str) -> list[str]:
            return re.findall(
                rf"^{re.escape(name)}:\s*(.*?)\s*$", text, re.MULTILINE | re.IGNORECASE,
            )

        states = values("State")
        if len(states) != 1:
            raise EnvironmentError(f"{path}: State must appear exactly once")
        if states[0].lower() == "done":
            continue
        decisions = values("Product-Decisions")
        if decisions != ["frozen"]:
            raise EnvironmentError(f"{path}: Product-Decisions must be exactly frozen")
        dependencies = set(re.findall(r"T-[0-9]+", " ".join(values("Depends-On"))))
        internal = sorted(dependencies & cohort)
        if internal:
            raise EnvironmentError(
                f"qualification cohort dependency {ticket} -> {internal[0]}; "
                "use independent tickets or sequential generations"
            )


def initialize_selected_linear(factory: Path, product: Path) -> None:
    map_path = Path(os.environ.get(
        "FACTORY_OPERATOR_MAP", product / "factory/linear-map.json",
    ))
    if not map_path.is_file() or map_path.is_symlink():
        return
    try:
        mapping = json.loads(map_path.read_text(encoding="utf-8"))
        selected = json.loads(
            (product / "factory/QUALIFICATION.json").read_text(encoding="utf-8")
        )["tickets"]
    except (KeyError, OSError, json.JSONDecodeError) as error:
        raise EnvironmentError("qualification Linear map is malformed") from error
    tickets = mapping.get("tickets", {}) if isinstance(mapping, dict) else {}
    missing = [
        ticket for ticket in selected
        if not isinstance(tickets.get(ticket), dict)
        or tickets[ticket].get("operator_fields_initialized") is not True
    ]
    for ticket in missing:
        result = subprocess.run(
            [
                sys.executable, str(factory / "scripts/linear-sync.py"),
                "--factory-root", str(product), "--ticket", ticket, "--initialize",
            ],
            text=True, capture_output=True, check=False, timeout=120,
        )
        if result.returncode:
            raise EnvironmentError(
                f"{ticket}: selected-ticket Linear initialization failed: "
                f"{result.stdout.strip() or result.stderr.strip()}"
            )


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


def validate_provider(release: Path, root: Path, capacity: int) -> str:
    policy, activation, policy_hash = provider_configuration(release, capacity)
    provider = safe_directory(root / "provider")
    if (
        read(provider / "provider-policy.json") != policy
        or read(provider / "provider-activation.json") != activation
    ):
        raise EnvironmentError("durable qualification provider policy changed")
    command(
        "/usr/bin/python3", str(release / "scripts/provider-activation.py"),
        "--config", str(provider / "provider-activation.json"),
        "--policy", str(provider / "provider-policy.json"),
        "--contract-version", "1.8.0", "--status",
    )
    status = json.loads(command(
        "/usr/bin/python3", str(release / "scripts/provider-coordinator.py"),
        "--db", str(provider / "accounting/state-v2.sqlite3"), "status",
    ))
    attempts = status.get("attempts")
    if (
        not isinstance(attempts, list)
        or status.get("active_reserve_micro_usd") != 0
        or status.get("legacy_intervals") != []
        or any(
            not isinstance(item, dict) or item.get("state") != "terminal"
            for item in attempts
        )
    ):
        raise EnvironmentError("durable qualification provider is not drained")
    return policy_hash


def product_origin(product: Path) -> str:
    origins = command(
        "git", "-C", str(product), "remote", "get-url", "--push", "--all", "origin"
    ).splitlines()
    if len(origins) != 1 or not origins[0]:
        raise EnvironmentError("qualification product origin is ambiguous")
    return origins[0]


def configured_repository(product: Path) -> str:
    values = re.findall(
        r"^(?:export\s+)?GH_REPO\s*=\s*['\"]?([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)['\"]?\s*$",
        (product / "factory/PROJECT.env").read_text(encoding="utf-8"),
        re.M,
    )
    if len(values) != 1:
        raise EnvironmentError("qualification product repository is ambiguous")
    return values[0]


def commit_present(product: Path, sha: str) -> bool:
    return subprocess.run(
        ["git", "-C", str(product), "cat-file", "-e", f"{sha}^{{commit}}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=120,
    ).returncode == 0


def historical_pr_objects(product: Path) -> int:
    """Hydrate immutable PR heads needed by committed terminal migrations."""
    migrations = product / "factory/migrations"
    if not migrations.is_dir():
        return 0
    supported = {
        "nysa.software-factory.legacy-closeout/v1": ("pr",),
        "nysa.software-factory.terminal-backfill/v1": (
            "implementation_pr", "closeout_pr",
        ),
        "nysa.software-factory.protected-merge-reconciliation/v1": (
            "original_pr", "adoption_pr",
        ),
    }
    repository = configured_repository(product)
    requirements: dict[tuple[int, str], dict[str, Any]] = {}
    for path in sorted(migrations.glob("**/*.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise EnvironmentError(
                f"historical object record is malformed: {path.relative_to(product)}"
            ) from error
        keys = supported.get(value.get("schema")) if isinstance(value, dict) else None
        if not keys:
            continue
        relative = str(path.relative_to(product))
        if value.get("repository") != repository:
            raise EnvironmentError(
                f"historical object repository mismatch: {relative}"
            )
        for key in keys:
            record = value.get(key)
            if record is None:
                continue
            if (
                not isinstance(record, dict)
                or isinstance(record.get("number"), bool)
                or not isinstance(record.get("number"), int)
                or record["number"] <= 0
                or not SHA.fullmatch(record.get("head", ""))
            ):
                raise EnvironmentError(
                    f"historical PR record is malformed: {relative} {key}"
                )
            identity = (record["number"], record["head"])
            item = requirements.setdefault(identity, {
                "commits": set(), "paths": set(),
            })
            item["commits"].add(record["head"])
            item["paths"].add(relative)
            if (
                value.get("schema")
                == "nysa.software-factory.protected-merge-reconciliation/v1"
                and key == "original_pr"
            ):
                evidence = value.get("evidence_head", "")
                if not SHA.fullmatch(evidence):
                    raise EnvironmentError(
                        f"historical evidence head is malformed: {relative}"
                    )
                item["commits"].add(evidence)

    for (number, head), item in sorted(requirements.items()):
        missing = sorted(
            sha for sha in item["commits"] if not commit_present(product, sha)
        )
        if missing:
            reference = f"refs/pull/{number}/head"
            observed = subprocess.run(
                ["git", "-C", str(product), "ls-remote", "--refs", "origin", reference],
                text=True, capture_output=True, check=False, timeout=120,
            )
            fields = observed.stdout.split()
            relative = sorted(item["paths"])[0]
            if observed.returncode or fields != [head, reference]:
                raise EnvironmentError(
                    f"historical PR head unavailable: {relative} PR #{number} "
                    f"expected {head}"
                )
            fetched = subprocess.run(
                [
                    "git", "-C", str(product), "fetch", "--quiet", "--no-tags",
                    "--no-write-fetch-head", "origin", reference,
                ],
                text=True, capture_output=True, check=False, timeout=120,
            )
            if fetched.returncode:
                raise EnvironmentError(
                    f"historical PR head fetch failed: {relative} PR #{number} "
                    f"expected {head}"
                )
        absent = sorted(
            sha for sha in item["commits"] if not commit_present(product, sha)
        )
        if absent:
            raise EnvironmentError(
                f"historical commit object missing: {sorted(item['paths'])[0]} "
                f"PR #{number} expected {absent[0]}"
            )
        for sha in item["commits"]:
            if sha != head and subprocess.run(
                ["git", "-C", str(product), "merge-base", "--is-ancestor", sha, head],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=120,
            ).returncode:
                raise EnvironmentError(
                    f"historical commit is not in PR: {sorted(item['paths'])[0]} "
                    f"PR #{number} expected {sha}"
                )
    return len(requirements)


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
    prepare_product_runtime(product, create=False)
    if command("git", "-C", str(factory), "status", "--porcelain", "--untracked-files=all"):
        raise EnvironmentError("Factory candidate must be clean")
    if command("git", "-C", str(product), "status", "--porcelain", "--untracked-files=all"):
        raise EnvironmentError("qualification product must be clean")
    validate_selected_contracts(product)
    prepare_product_runtime(product)
    initialize_selected_linear(factory, product)
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
    historical_objects = historical_pr_objects(product)
    restoring = bool(getattr(args, "restore", False))
    takeover = takeover_source(
        factory, product, args.project, getattr(args, "takeover_project", None)
    )
    if restoring and takeover:
        raise EnvironmentError("takeover qualification cannot restore isolated authority")
    identity = authority_identity(
        args.project, sha, tree, product, product_sha, product_tree, origin,
        runtime_tuple,
    )
    authority: Path | None = None
    controller_state_path = ""
    provider_state_path = ""
    if not takeover:
        authority = authority_root(args.project, create=not restoring)
        controller = authority / "controller"
        if restoring:
            if read(authority / "authority.json") != identity:
                raise EnvironmentError("durable qualification authority changed")
            safe_directory(controller)
            validate_paused_authority(factory, product, controller, identity)
        else:
            controller.mkdir(mode=0o700)
        controller_state_path = str(controller)
        provider_state_path = str(authority / "provider")

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
        if takeover else (
            validate_provider(release, authority, qualification_capacity(product))
            if restoring else
            prepare_provider(release, authority, qualification_capacity(product))
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
    else:
        receipt_value["controller_state_path"] = controller_state_path
        receipt_value["provider_state_path"] = provider_state_path
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
    else:
        active_value["controller_state_path"] = controller_state_path
        active_value["provider_state_path"] = provider_state_path
        if not restoring:
            write(authority / "authority.json", identity)
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
        "authority_root": str(authority) if authority else None,
        "historical_pr_objects": historical_objects,
        "launcher": str(release / "integrations/hermes/bin/factory-launch"),
        "product_sha": product_sha,
        "product_tree": product_tree,
        "project": args.project,
        "provider_policy_sha256": provider_policy_sha256,
        "qualification_mode": qualification_mode,
        "root": str(root),
        "schema": SCHEMA,
        "status": "restored" if restoring else "prepared",
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
    prepare_product_runtime(product, create=False)
    if command("git", "-C", str(factory), "status", "--porcelain", "--untracked-files=all"):
        raise EnvironmentError("Factory candidate must be clean")
    if command("git", "-C", str(product), "status", "--porcelain", "--untracked-files=all"):
        raise EnvironmentError("qualification product must be clean")
    validate_selected_contracts(product)
    prepare_product_runtime(product)
    initialize_selected_linear(factory, product)
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
    historical_objects = historical_pr_objects(product)
    origin = product_origin(product)
    identity = authority_identity(
        args.project, sha, tree, product, product_sha, product_tree, origin,
        runtime_tuple,
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

    authority = authority_root(args.project)
    controller = safe_directory(Path(active.get("controller_state_path", "")))
    provider = safe_directory(Path(active.get("provider_state_path", "")))
    if (
        controller != authority / "controller"
        or provider != authority / "provider"
    ):
        raise EnvironmentError("durable qualification authority path changed")
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
            read(provider / "provider-policy.json") != policy
            or read(provider / "provider-activation.json") != activation
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
            "controller_state_path": str(controller),
            "provider_state_path": str(provider),
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
            "controller_state_path": str(controller),
            "provider_state_path": str(provider),
            "qualification_mode": qualification_mode,
            "receipt_id": receipt_id,
            "release_path": str(release),
        }, runtime_tuple)
        replace(active_path, next_active)
        replace(authority / "authority.json", identity)
        result = bind_runtime_tuple({
            "factory_sha": sha,
            "factory_tree": tree,
            "historical_pr_objects": historical_objects,
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
    parser.add_argument("--restore", action="store_true")
    args = parser.parse_args()
    try:
        if not PROJECT.fullmatch(args.project):
            raise EnvironmentError("invalid qualification project")
        if args.upgrade and args.restore:
            raise EnvironmentError("qualification restore and upgrade are exclusive")
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
