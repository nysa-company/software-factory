#!/usr/bin/env python3
"""Prepare one sealed, non-production Factory qualification release."""

from __future__ import annotations

import argparse
from decimal import Decimal, InvalidOperation
import fcntl
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path, PurePosixPath
import pwd
import re
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from typing import Any

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
from release_lineage import successor_release_lineage  # noqa: E402
from legacy_closeout import (  # noqa: E402
    ValidationError as TerminalError,
    protected_dependency,
    protected_terminal,
)
from qualification_artifacts import (  # noqa: E402
    ArtifactError as QualificationArtifactError,
    ensure_ticket as ensure_qualification_artifacts,
)
from qualification_manifest import (  # noqa: E402
    ManifestError as QualificationManifestError,
    validate as validate_qualification_manifest,
)
from historical_pr_objects import (  # noqa: E402
    HistoricalObjectError,
    _repository as historical_product_repository,
    commit_present,
    hydrate as hydrate_historical_pr_objects,
)
from effective_ticket import committed_ticket  # noqa: E402
from inflight_release import (  # noqa: E402
    AuthorizationError as InflightAuthorizationError,
    parse_authorization as parse_inflight_authorization,
    ticket_source_kit,
    verify_migration as verify_inflight_migration,
)
import operator_receipt  # noqa: E402


SCHEMA = "nysa.software-factory.qualification-environment/v1"
AUTHORITY_SCHEMA = "nysa.software-factory.qualification-authority/v1"
OPERATOR_BOOTSTRAP_SCHEMA = "nysa.software-factory.qualification-operator-bootstrap/v1"
PREPROVIDER_HANDOFF_SCHEMA = (
    "nysa.software-factory.qualification-preprovider-handoff/v1"
)
PREPROVIDER_RESET_SCHEMA = "nysa.software-factory.preprovider-branch-resets/v1"
TRANSITION_RECEIPT_SCHEMA = "nysa.software-factory.transition-receipt/v1"
ACTIVATION_SCHEMA = "nysa.software-factory.provider-activation/v2"
CONTROLLER_EVENT_SCHEMA = "nysa.software-factory.controller-event/v1"
ACTIVATION_START_SCHEMA = "nysa.software-factory.qualification-activation-start/v1"
POLICY_SCHEMA = "factory-provider-concurrency-policy/v1"
SHA = re.compile(r"^[0-9a-f]{40}$")
PROJECT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
ROOT = re.compile(r"^/private/tmp/nysa-sf-qualification\.[A-Za-z0-9._-]+$")
FACTORY_ISSUE = re.compile(
    r"^https://github[.]com/[A-Za-z0-9_.-]+/software-factory/issues/[1-9][0-9]*$"
)
CURSOR_DATA_PATH_LIMIT = 75
CURSOR_ATTEMPT_PLACEHOLDER = "0000000000-0000000-cli"
SUPPORTED_CONTRACTS = ("1.8.0", "1.9.0", "2.0.0")
SUPPORTED_UPGRADES = {("1.9.0", "2.0.0")}
TRANSACTION_ROOT = Path(__file__).resolve().parents[1]


class EnvironmentError(ValueError):
    def __init__(self, message: str, reason_code: str | None = None):
        super().__init__(message)
        self.reason_code = reason_code


def command(
    *arguments: str, cwd: Path | None = None,
    environment: dict[str, str] | None = None,
) -> str:
    result = subprocess.run(
        arguments, cwd=cwd, text=True, capture_output=True, check=False, timeout=120,
        env=environment,
    )
    if result.returncode:
        raise EnvironmentError(
            result.stderr.strip() or result.stdout.strip() or "command failed"
        )
    return result.stdout.strip()


def validate_provider_cli_pins(factory: Path, sha: str) -> None:
    try:
        result = subprocess.run(
            [
                "bash", str(factory / "scripts/factory-kit.sh"),
                "provider-cli-pin", "check", "--sha", sha,
            ],
            text=True, capture_output=True, check=False, timeout=120,
        )
        value = json.loads(result.stdout)
        items = value.get("items")
        ready = (
            not result.returncode
            and value.get("schema")
            == "nysa.software-factory.provider-cli-pin-status/v1"
            and value.get("status") == "ready"
            and value.get("requested_release", {}).get("factory_sha") == sha
            and isinstance(items, list)
            and len(items) == 4
            and {item.get("name") for item in items} == {
                "claude", "codex", "codex-code-mode-host", "agent",
            }
            and all(
                item.get("status") == "ok"
                and item.get("reason") == "exact_pin_ready"
                for item in items
            )
        )
    except (
        AttributeError, json.JSONDecodeError, OSError,
        subprocess.TimeoutExpired, TypeError,
    ):
        ready = False
    if not ready:
        raise EnvironmentError(
            "qualification candidate provider CLI pins are not ready"
        )


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


def sealed_directory(path: Path) -> Path:
    info = path.lstat()
    if (
        path.resolve(strict=True) != path
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o555
    ):
        raise EnvironmentError("sealed qualification release is unsafe")
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


def write_exact(path: Path, value: dict[str, Any]) -> None:
    if path.exists() or path.is_symlink():
        if config_bytes(path) != canonical(value):
            raise EnvironmentError("qualification preparation artifact changed")
        return
    write(path, value)


def write_bytes(path: Path, raw: bytes) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def write_bytes_exact(path: Path, raw: bytes) -> None:
    if path.exists() or path.is_symlink():
        if config_bytes(path) != raw:
            raise EnvironmentError("qualification preparation artifact changed")
        return
    write_bytes(path, raw)


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


def config_bytes(path: Path) -> bytes:
    if not path.is_absolute() or path.resolve(strict=True) != path:
        raise EnvironmentError("qualification global config is unsafe")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_size > 131_072
        ):
            raise EnvironmentError("qualification global config is unsafe")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            return stream.read()
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def install_config(path: Path, raw: bytes) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(raw)
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


def snapshot_global_config(args: argparse.Namespace, root: Path) -> None:
    target = root / "global.env"
    supplied = getattr(args, "global_env", None)
    if supplied is None and (target.exists() or target.is_symlink()):
        config_bytes(target)
        return
    if supplied is not None and not supplied.is_absolute():
        raise EnvironmentError("qualification global config must be absolute")
    source = (
        supplied
        if supplied is not None
        else Path.home().resolve(strict=True) / ".factory/global.env"
    )
    raw = config_bytes(source) if source.exists() or source.is_symlink() else b""
    install_config(target, raw)


def prepare_global_config(args: argparse.Namespace, root: Path) -> bytes:
    target = root / "global.env"
    supplied = getattr(args, "global_env", None)
    if supplied is not None and not supplied.is_absolute():
        raise EnvironmentError("qualification global config must be absolute")
    source = (
        supplied
        if supplied is not None
        else Path.home().resolve(strict=True) / ".factory/global.env"
    )
    raw = config_bytes(source) if source.exists() or source.is_symlink() else b""
    if target.exists() or target.is_symlink():
        if config_bytes(target) != raw:
            raise EnvironmentError("qualification preparation artifact changed")
    return raw


def qualification_global_cap(raw: bytes) -> Decimal:
    values: list[str] = []
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeDecodeError as error:
        raise EnvironmentError("qualification global config is invalid") from error
    for raw_line in lines:
        if not raw_line or raw_line.startswith("#"):
            continue
        line = raw_line.removeprefix("export ")
        match = re.fullmatch(r"([A-Z][A-Z0-9_]*)=(.*)", line)
        if not match:
            raise EnvironmentError("qualification global config is invalid")
        key, value = match.groups()
        if value[:1] in {"\"", "'"}:
            if len(value) < 2 or value[-1] != value[0]:
                raise EnvironmentError("qualification global config is invalid")
            value = value[1:-1]
        if not re.fullmatch(r"[A-Za-z0-9._:/+@%~-]*", value):
            raise EnvironmentError("qualification global config is invalid")
        if key == "GLOBAL_DAILY_CAP_USD":
            values.append(value)
    if len(values) != 1:
        raise EnvironmentError("qualification global daily cap is missing or repeated")
    if not re.fullmatch(r"[0-9]{1,7}(?:[.][0-9]{1,6})?", values[0]):
        raise EnvironmentError("qualification global daily cap is invalid")
    try:
        cap = Decimal(values[0])
    except InvalidOperation as error:
        raise EnvironmentError("qualification global daily cap is invalid") from error
    if not cap.is_finite() or cap <= 0 or cap > 1_000_000:
        raise EnvironmentError("qualification global daily cap is invalid")
    return cap


def validate_qualification_budget(
    factory: Path, product: Path, manifest: dict[str, Any], global_config: bytes,
) -> None:
    spec = importlib.util.spec_from_file_location(
        "qualification_envelope", factory / "scripts/envelope-control.py"
    )
    if not spec or not spec.loader:
        raise EnvironmentError("qualification envelope verifier is unavailable")
    envelope = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(envelope)
    try:
        values = envelope.read_runtime_state(product)[-1]
        run_cap = Decimal(manifest["per_run_budget_usd"])
        ticket_cap = Decimal(manifest["per_ticket_budget_usd"])
        total_cap = Decimal(manifest["budget_usd"])
        role_budgets = {
            role: Decimal(envelope.effective_role(values, role)["PER_RUN_BUDGET_USD"])
            for role in envelope.ROLES
        }
        product_ticket = Decimal(values["PER_TICKET_BUDGET_USD"])
        product_daily = Decimal(values["DAILY_CAP_USD"])
    except (
        AttributeError, InvalidOperation, KeyError, OSError, TypeError,
        ValueError, envelope.ControlError,
    ) as error:
        raise EnvironmentError("qualification budget inputs are invalid") from error
    if any(value > run_cap for value in role_budgets.values()):
        raise EnvironmentError("qualification product per-run budget exceeds the manifest")
    if product_ticket != ticket_cap:
        raise EnvironmentError("qualification product ticket budget does not match the manifest")
    if product_daily < total_cap:
        raise EnvironmentError("qualification product daily cap is below the manifest budget")
    if qualification_global_cap(global_config) < total_cap:
        raise EnvironmentError("qualification machine daily cap is below the manifest budget")


def qualification_fallback_readiness(
    release: Path, root: Path, project: str, product: Path,
    model_state_root: Path | None = None,
    timeout: float = 120,
    bundle_path: Path | None = None,
) -> tuple[dict[str, Any], str]:
    state_root = (
        safe_directory(model_state_root)
        if model_state_root is not None else root / "projects"
    )
    result = subprocess.run(
        [
            str(release / "scripts/model-control.sh"),
            "qualification-bundle" if bundle_path else "qualification-readiness",
        ],
        env={
            "HOME": str(Path.home().resolve(strict=True)),
            "PATH": f"{Path.home()}/.factory/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
            "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
            "FACTORY_GLOBAL_ENV": str(root / "global.env"),
            "FACTORY_KIT_TRUST_SCOPE": "qualification-candidate",
            "FACTORY_MODEL_POLICY_FILE": str(product / "factory/model-policy.json"),
            "FACTORY_MODEL_STATE_ROOT": str(state_root),
            "FACTORY_PROJECT": project,
            "FACTORY_ROOT": str(product),
        },
        text=True, capture_output=True, check=False, timeout=timeout,
    )
    try:
        report = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise EnvironmentError("qualification fallback readiness is unavailable") from error
    if bundle_path is not None and result.returncode == 0 and isinstance(report, dict):
        body = {
            key: item for key, item in report.items()
            if key != "bundle_sha256"
        }
        bundle_digest = hashlib.sha256(canonical(body)).hexdigest()
        if (
            report.get("schema")
            != "nysa.software-factory.qualification-model-bundle/v1"
            or report.get("bundle_sha256") != bundle_digest
            or not isinstance(report.get("resolution"), dict)
            or not isinstance(report.get("readiness"), dict)
            or not isinstance(report.get("fallback_readiness"), dict)
        ):
            raise EnvironmentError("qualification model bundle is invalid")
        write_exact(bundle_path, report)
        report = report["fallback_readiness"]
    digest = report.get("readiness_sha256", "") if isinstance(report, dict) else ""
    if (
        result.returncode
        or report.get("schema")
        != "nysa.software-factory.qualification-fallback-readiness/v1"
        or report.get("status") != "ready"
        or not re.fullmatch(r"[0-9a-f]{64}", digest)
    ):
        checks = report.get("checks", []) if isinstance(report, dict) else []
        failed = next((
            item for item in checks
            if isinstance(item, dict) and item.get("state") != "READY"
        ), None)
        reason_code = failed.get("reason") if failed else report.get("reason_code")
        if not isinstance(reason_code, str) or not re.fullmatch(
            r"[a-z0-9][a-z0-9._-]{0,127}", reason_code,
        ):
            error = report.get("error", "") if isinstance(report, dict) else ""
            match = re.search(r"(?:^|: )([a-z0-9][a-z0-9._-]{0,127})$", error)
            reason_code = match.group(1) if match else "invalid"
        detail = (
            f"{failed.get('fallback_route_id') or failed.get('cursor_route_id')}:"
            f"{reason_code}:expected={failed.get('expected_version') or 'unknown'}:"
            f"installed={failed.get('installed_version') or 'unknown'}"
            if failed else reason_code
        )
        raise EnvironmentError(
            f"qualification fallback readiness refused: {detail}", reason_code,
        )
    return report, digest


def read(path: Path) -> dict[str, Any]:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
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


def transition_receipt(path: Path) -> dict[str, Any]:
    value = read(path)
    immutable = {
        key: item for key, item in value.items()
        if key not in {"consumed", "consumed_at_epoch", "receipt_sha256"}
    }
    if (
        value.get("schema") != TRANSITION_RECEIPT_SCHEMA
        or value.get("receipt_sha256")
        != hashlib.sha256(canonical(immutable)).hexdigest()
        or value.get("consumed") is not False
    ):
        raise EnvironmentError("pre-provider transition receipt is invalid")
    return value


def worktree_records(product: Path) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    current: dict[str, str] = {}
    output = command("git", "-C", str(product), "worktree", "list", "--porcelain")
    for line in output.splitlines() + [""]:
        if line:
            name, _, item = line.partition(" ")
            current[name] = item
        elif current:
            result.append(current)
            current = {}
    return result


def qualification_lane(root_value: Path, project: str) -> dict[str, Any]:
    root = Path(os.path.realpath(root_value))
    if not ROOT.fullmatch(str(root)):
        raise EnvironmentError("qualification root must be under /private/tmp")
    safe_directory(root)
    if read(root / "marker.json") != {"mode": "qualification", "schema": SCHEMA}:
        raise EnvironmentError("qualification marker is invalid")
    project_root = safe_directory(safe_directory(root / "projects") / project)
    active = read(project_root / "active.json")
    receipt_id = active.get("receipt_id", "")
    if not re.fullmatch(r"[0-9a-f]{64}", receipt_id):
        raise EnvironmentError("qualification activation receipt is invalid")
    receipt = read(safe_directory(root / "receipts") / f"{receipt_id}.json")
    unsigned = dict(receipt)
    if (
        unsigned.pop("receipt_id", "") != receipt_id
        or hashlib.sha256(canonical(unsigned)).hexdigest() != receipt_id
    ):
        raise EnvironmentError("qualification activation receipt is invalid")
    shared = (
        "contract_version", "kit_sha", "kit_tree", "product_path",
        "product_sha", "product_tree", "project", "provider_policy_sha256",
        "fallback_readiness_sha256",
        "qualification_mode", "operator_map_path", "controller_state_path",
        "provider_state_path", "runtime_ledger_path",
    )
    if (
        active.get("project") != project
        or active.get("contract_version") not in SUPPORTED_CONTRACTS
        or active.get("qualification_mode") != "isolated"
        or receipt.get("status") != "pass"
        or receipt.get("product_origin") is None
        or any(
            key not in active or key not in receipt or active[key] != receipt[key]
            for key in shared
        )
        or (
            ("model_bundle_sha256" in active)
            != ("model_bundle_sha256" in receipt)
        )
        or (
            "model_bundle_sha256" in active
            and active["model_bundle_sha256"]
            != receipt["model_bundle_sha256"]
        )
        or ("runtime_tuple" in active) != ("runtime_tuple" in receipt)
        or active.get("runtime_tuple") != receipt.get("runtime_tuple")
    ):
        raise EnvironmentError("qualification activation is inconsistent")
    kit_sha = active.get("kit_sha", "")
    kit_tree = active.get("kit_tree", "")
    product_path = active.get("product_path", "")
    if (
        not SHA.fullmatch(kit_sha)
        or not SHA.fullmatch(kit_tree)
        or not isinstance(product_path, str)
        or not Path(product_path).is_absolute()
    ):
        raise EnvironmentError("qualification activation identity is invalid")
    product = Path(product_path)
    if product.resolve(strict=True) != product:
        raise EnvironmentError("qualification product path is unsafe")
    release = sealed_directory(safe_directory(root / "releases") / kit_sha)
    authority = authority_root(project)
    controller = safe_directory(authority / "controller")
    provider = safe_directory(authority / "provider")
    if (
        active.get("release_path") != str(release)
        or active.get("controller_state_path") != str(controller)
        or active.get("provider_state_path") != str(provider)
        or command("git", "-C", str(product), "rev-parse", "HEAD")
        != active.get("product_sha")
        or command("git", "-C", str(product), "rev-parse", "HEAD^{tree}")
        != active.get("product_tree")
        or product_origin(product) != receipt.get("product_origin")
        or git_tree(release) != kit_tree
        or command(
            "git", "-C", str(product), "status", "--porcelain",
            "--untracked-files=all",
        )
    ):
        raise EnvironmentError("qualification activation content changed")
    manifest = qualification_manifest(product, kit_sha)
    operator_map = authority / "operator/operator-map.json"
    runtime_ledger = authority / "operator/runtime-ledger.csv"
    if (
        active.get("operator_map_path") != str(operator_map)
        or active.get("runtime_ledger_path") != str(runtime_ledger)
    ):
        raise EnvironmentError("qualification operator authority path changed")
    identity = authority_identity(
        project, active["contract_version"], kit_sha, kit_tree, product,
        active["product_sha"],
        active["product_tree"], receipt["product_origin"],
        active.get("runtime_tuple"), str(operator_map), str(runtime_ledger),
    )
    stored_identity = read(authority / "authority.json")
    if stored_identity != identity:
        legacy_identity = authority_identity(
            project, "1.8.0", kit_sha, kit_tree, product,
            active["product_sha"], active["product_tree"],
            receipt["product_origin"], active.get("runtime_tuple"),
            str(operator_map), str(runtime_ledger),
        )
        if (
            active["contract_version"] != "1.9.0"
            or stored_identity != legacy_identity
        ):
            raise EnvironmentError(
                "qualification authority does not match activation"
            )
    resumed_map, resumed_ledger = resume_operator_state(
        authority, identity, manifest["tickets"],
    )
    if resumed_map != operator_map or resumed_ledger != runtime_ledger:
        raise EnvironmentError("qualification operator authority path changed")
    validate_runtime_ledger(runtime_ledger)
    return {
        "active": active,
        "authority": authority,
        "controller": controller,
        "manifest": manifest,
        "product": product,
        "provider": provider,
        "receipt": receipt,
        "release": release,
        "root": root,
    }


def preprovider_reset_authorizations(
    product: Path, factory_sha: str, tickets: list[str],
) -> dict[str, str]:
    path = product / "factory/qualification/preprovider-branch-resets.json"
    try:
        info = path.lstat()
        if (
            path.is_symlink()
            or not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o022
        ):
            raise EnvironmentError(
                "successor pre-provider reset authorization is unsafe"
            )
        relative = "factory/qualification/preprovider-branch-resets.json"
        tree_entry = command(
            "git", "-C", str(product), "ls-tree", "HEAD", "--", relative
        ).split(None, 3)
        if len(tree_entry) != 4 or tree_entry[:2] != ["100644", "blob"]:
            raise EnvironmentError(
                "successor pre-provider reset authorization is not a sealed file"
            )
        sealed = subprocess.run(
            ["git", "-C", str(product), "show", f"HEAD:{relative}"],
            capture_output=True, check=True, timeout=120,
        ).stdout
        if path.read_bytes() != sealed:
            raise EnvironmentError(
                "successor pre-provider reset authorization differs from sealed HEAD"
            )
        value = json.loads(sealed)
    except (FileNotFoundError, json.JSONDecodeError) as error:
        raise EnvironmentError(
            "successor pre-provider branch reset authorization is unavailable"
        ) from error
    resets = value.get("resets")
    if (
        value.get("schema") != PREPROVIDER_RESET_SCHEMA
        or set(value) != {"factory_sha", "resets", "schema"}
        or value.get("factory_sha") != factory_sha
        or not isinstance(resets, list)
    ):
        raise EnvironmentError("successor pre-provider reset authorization is invalid")
    result: dict[str, str] = {}
    for item in resets:
        if not isinstance(item, dict):
            raise EnvironmentError("successor pre-provider reset entry is invalid")
        ticket = item.get("ticket")
        head = item.get("head")
        if (
            set(item) != {"branch", "head", "ticket"}
            or ticket not in tickets
            or item.get("branch") != f"ticket/{ticket}"
            or not isinstance(head, str)
            or not SHA.fullmatch(head)
            or ticket in result
        ):
            raise EnvironmentError("successor pre-provider reset entry is invalid")
        result[ticket] = head
    if set(result) != set(tickets):
        raise EnvironmentError("successor pre-provider reset cohort is incomplete")
    return result


def lock_controllers(*controllers: Path) -> list[int]:
    descriptors: list[int] = []
    try:
        for controller in sorted(set(controllers), key=str):
            path = controller / "reconcile.lock"
            descriptor = os.open(
                path,
                os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600
            ):
                os.close(descriptor)
                raise EnvironmentError("qualification controller lock is unsafe")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                os.close(descriptor)
                raise EnvironmentError("qualification controller is active") from error
            descriptors.append(descriptor)
        return descriptors
    except Exception:
        for descriptor in descriptors:
            os.close(descriptor)
        raise


def handoff_worktree_root(lane: dict[str, Any], *, create: bool) -> Path:
    parent = lane["root"] / "worktrees"
    project = parent / lane["active"]["project"]
    for path in (parent, project):
        if path.exists() or path.is_symlink():
            safe_directory(path)
        elif create:
            path.mkdir(mode=0o700)
        else:
            raise EnvironmentError("pre-provider trusted worktree root is unavailable")
    return project


def lock_dispatch_admission(
    source: dict[str, Any], target: dict[str, Any],
) -> list[int]:
    descriptors: list[int] = []
    try:
        admission_paths = sorted({
            handoff_worktree_root(source, create=False) / ".dispatch-admission.lock",
            handoff_worktree_root(target, create=True) / ".dispatch-admission.lock",
        }, key=str)
        for path in admission_paths:
            descriptor = os.open(
                path,
                os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600
            ):
                os.close(descriptor)
                raise EnvironmentError("pre-provider admission lock is unsafe")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                os.close(descriptor)
                raise EnvironmentError("pre-provider dispatch admission is active") from error
            descriptors.append(descriptor)
        return descriptors
    except Exception:
        for descriptor in descriptors:
            os.close(descriptor)
        raise


def lock_dispatch_boundaries(
    source: dict[str, Any], target: dict[str, Any],
) -> tuple[list[int], list[Path]]:
    descriptors: list[int] = []
    directories: list[Path] = []
    try:
        descriptors = lock_dispatch_admission(source, target)
        products = sorted({source["product"], target["product"]}, key=str)
        for name in (".launch.lock", ".dispatch-leases.lock"):
            for product in products:
                path = product / "factory" / name
                try:
                    path.mkdir(mode=0o700)
                except FileExistsError as error:
                    raise EnvironmentError("pre-provider dispatch boundary is active") from error
                directories.append(path)
        return descriptors, directories
    except Exception:
        for path in reversed(directories):
            path.rmdir()
        for descriptor in descriptors:
            os.close(descriptor)
        raise


def unlock_dispatch_boundaries(
    descriptors: list[int], directories: list[Path],
) -> None:
    for path in reversed(directories):
        path.rmdir()
    for descriptor in descriptors:
        os.close(descriptor)


def provider_drained(lane: dict[str, Any]) -> None:
    product = lane["product"]
    active_runs = product / "factory/.active-runs"
    runs = product / "factory/runs"
    for path in (active_runs, runs):
        if path.exists() or path.is_symlink():
            info = path.lstat()
            if (
                path.is_symlink()
                or not stat.S_ISDIR(info.st_mode)
                or info.st_uid != os.geteuid()
                or info.st_mode & 0o022
            ):
                raise EnvironmentError("qualification provider runtime is unsafe")
    if (
        active_runs.is_dir() and any(active_runs.iterdir())
        or runs.is_dir() and any(runs.glob("*.pid"))
    ):
        raise EnvironmentError("qualification has an active provider run")
    validate_provider(
        lane["release"], lane["authority"], lane["manifest"]["capacity"],
        lane["active"]["contract_version"],
    )


def journal_value(value: dict[str, Any]) -> dict[str, Any]:
    if (
        set(value) != {
            "authorization_sha256", "entries", "journal_sha256", "moved",
            "schema", "source_factory_sha", "source_project",
            "source_receipt_id", "source_root", "status",
            "target_factory_sha", "target_project", "target_receipt_id",
            "target_root",
        }
        or value.get("schema") != PREPROVIDER_HANDOFF_SCHEMA
    ):
        raise EnvironmentError("pre-provider handoff journal is invalid")
    unsigned = dict(value)
    digest = unsigned.pop("journal_sha256", "")
    if digest != hashlib.sha256(canonical(unsigned)).hexdigest():
        raise EnvironmentError("pre-provider handoff journal digest is invalid")
    immutable = {
        key: item for key, item in unsigned.items() if key not in {"moved", "status"}
    }
    if unsigned.get("authorization_sha256") != hashlib.sha256(
        canonical({
            key: item for key, item in immutable.items()
            if key != "authorization_sha256"
        })
    ).hexdigest():
        raise EnvironmentError("pre-provider handoff authorization is invalid")
    return value


def seal_journal(value: dict[str, Any]) -> dict[str, Any]:
    result = dict(value)
    result.pop("journal_sha256", None)
    result["journal_sha256"] = hashlib.sha256(canonical(result)).hexdigest()
    return result


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


def lock_preparation(project: str) -> int:
    if not PROJECT.fullmatch(project):
        raise EnvironmentError("qualification project is invalid")
    factory = Path.home().resolve(strict=True) / ".factory"
    safe_directory(factory)
    qualification = factory / "qualification"
    try:
        qualification.mkdir(mode=0o700)
    except FileExistsError:
        pass
    safe_directory(qualification)
    descriptor = os.open(
        qualification / f".prepare-{project}.lock",
        os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    info = os.fstat(descriptor)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_nlink != 1
        or stat.S_IMODE(info.st_mode) != 0o600
    ):
        os.close(descriptor)
        raise EnvironmentError("qualification preparation lock is unsafe")
    fcntl.flock(descriptor, fcntl.LOCK_EX)
    return descriptor


def activation_start(args: argparse.Namespace, observed_epoch_ns: int) -> tuple[Path, int]:
    base = Path.home().resolve(strict=True) / ".factory/qualification"
    path = base / f".activation-start-{args.project}.json"
    value = {
        "factory_root": str(args.factory_root.resolve(strict=True)),
        "observed_at_epoch_ns": observed_epoch_ns,
        "product_root": str(args.product_root.resolve(strict=True)),
        "project": args.project,
        "qualification_root": str(Path(os.path.realpath(args.root))),
        "schema": ACTIVATION_START_SCHEMA,
    }
    if path.exists() or path.is_symlink():
        stored = read(path)
        digest = stored.pop("record_sha256", "")
        expected = {**value, "observed_at_epoch_ns": stored.get("observed_at_epoch_ns")}
        if (
            stored != expected
            or digest != hashlib.sha256(canonical(stored)).hexdigest()
            or not isinstance(stored["observed_at_epoch_ns"], int)
            or isinstance(stored["observed_at_epoch_ns"], bool)
            or stored["observed_at_epoch_ns"] < 1
        ):
            raise EnvironmentError("qualification activation start changed")
        return path, stored["observed_at_epoch_ns"]
    value["record_sha256"] = hashlib.sha256(canonical(value)).hexdigest()
    replace(path, value)
    return path, observed_epoch_ns


def remove_activation_start(path: Path) -> None:
    path.unlink(missing_ok=True)
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def preparation_state(root: Path, authority: Path | None, project: str) -> str:
    authority_exists = bool(
        authority and (authority.exists() or authority.is_symlink())
    )
    if root.exists() or root.is_symlink():
        safe_directory(root)
        root_populated = any(root.iterdir())
        active = root / "projects" / project / "active.json"
        complete = active.exists() or active.is_symlink()
    else:
        root_populated = False
        complete = False
    if complete:
        return "exact-complete"
    if authority_exists or root_populated:
        return "exact-incomplete"
    return "fresh"


def partial_authority_root(project: str) -> Path:
    """Create or resume only the pre-publication operator bootstrap boundary."""
    factory = Path.home().resolve(strict=True) / ".factory"
    safe_directory(factory)
    base = factory / "qualification"
    if not base.exists():
        base.mkdir(mode=0o700)
    safe_directory(base)
    root = base / project
    if not root.exists() and not root.is_symlink():
        safe_directory(root, create=True)
        return root
    safe_directory(root)
    if (root / "authority.json").exists() or (root / "authority.json").is_symlink():
        raise EnvironmentError("qualification environment already exists")
    allowed = {"operator", "operator-bootstrap.json"}
    if any(path.name not in allowed for path in root.iterdir()):
        raise EnvironmentError("partial qualification authority is invalid")
    return root


def authority_identity(
    project: str,
    contract: str,
    factory_sha: str,
    factory_tree: str,
    product: Path,
    product_sha: str,
    product_tree: str,
    product_origin_value: str,
    runtime_tuple: dict[str, str] | None,
    operator_map_path: str = "",
    runtime_ledger_path: str = "",
) -> dict[str, Any]:
    manifest = product / "factory/QUALIFICATION.json"
    value = {
        "contract_version": contract,
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
    if operator_map_path:
        value["operator_map_path"] = operator_map_path
        value["runtime_ledger_path"] = runtime_ledger_path
    value["authority_sha256"] = hashlib.sha256(canonical(value)).hexdigest()
    return value


def validate_upgrade_authority(
    current: dict[str, Any], active: dict[str, Any], target: dict[str, Any],
) -> None:
    if current == target:
        return
    unsigned = dict(current)
    digest = unsigned.pop("authority_sha256", "")
    active_contract = active["contract_version"]
    valid_contracts = {active_contract}
    if active_contract == "1.9.0":
        valid_contracts.add("1.8.0")
    shared = (
        "controller_state_path", "operator_map_path", "product_path",
        "product_tree", "project", "provider_state_path", "runtime_ledger_path",
    )
    if (
        digest != hashlib.sha256(canonical(unsigned)).hexdigest()
        or current.get("schema") != AUTHORITY_SCHEMA
        or current.get("contract_version") not in valid_contracts
        or any(current.get(key) != active.get(key) for key in shared)
        or current.get("factory_sha") != active.get("kit_sha")
        or current.get("factory_tree") != active.get("kit_tree")
        or (
            "product_sha" in active
            and current.get("product_sha") != active["product_sha"]
        )
    ):
        raise EnvironmentError("existing qualification authority is invalid")


def validate_upgrade_environment(
    current: dict[str, Any], active: dict[str, Any], target: dict[str, Any],
) -> None:
    if current == target:
        return
    expected = {
        "factory_sha": active["kit_sha"],
        "factory_tree": active["kit_tree"],
        "launcher": str(Path(active["release_path"]) / "scripts/factory-launch"),
        "product_tree": active["product_tree"],
        "project": active["project"],
        "provider_policy_sha256": active["provider_policy_sha256"],
        "qualification_mode": active["qualification_mode"],
        "root": target["root"],
        "schema": SCHEMA,
    }
    if (
        current.get("status") not in {"prepared", "restored", "upgraded"}
        or any(current.get(key) != value for key, value in expected.items())
        or (
            "product_sha" in active
            and current.get("product_sha") != active["product_sha"]
        )
        or (
            "runtime_tuple" in active
            and current.get("runtime_tuple") != active["runtime_tuple"]
        )
    ):
        raise EnvironmentError("existing qualification environment is invalid")


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


def qualification_manifest(product: Path, factory_sha: str) -> dict[str, Any]:
    path = product / "factory/QUALIFICATION.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return validate_qualification_manifest(value, factory_sha)
    except (OSError, json.JSONDecodeError, QualificationManifestError) as error:
        raise EnvironmentError(str(error)) from error
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


def validate_selected_contracts(
    product: Path, manifest: dict[str, Any] | None = None,
) -> None:
    """Reject non-canonical metadata and dependent qualification cohorts early."""
    if manifest is None:
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
    readiness_spec = importlib.util.spec_from_file_location(
        "qualification_ticket_readiness", Path(__file__).with_name("ticket-readiness.py")
    )
    preview_spec = importlib.util.spec_from_file_location(
        "qualification_ticket_pr", Path(__file__).with_name("ticket-pr.py")
    )
    if not readiness_spec or not readiness_spec.loader or not preview_spec or not preview_spec.loader:
        raise EnvironmentError("qualification admission helpers are unavailable")
    readiness_module = importlib.util.module_from_spec(readiness_spec)
    preview_module = importlib.util.module_from_spec(preview_spec)
    readiness_spec.loader.exec_module(readiness_module)
    preview_spec.loader.exec_module(preview_module)
    try:
        preview_provider = preview_module.project_preview_provider(product / "factory")
        nonvisual_paths = preview_module.project_nonvisual_paths(product / "factory")
    except (OSError, UnicodeError, preview_module.Refusal) as error:
        raise EnvironmentError(str(error)) from error
    selected_ownership: dict[str, list[str]] = {}
    for ticket in selected:
        path = product / "factory/tickets" / f"{ticket}.md"
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as error:
            raise EnvironmentError(f"qualification ticket is unavailable: {path}") from error

        def values(name: str) -> list[str]:
            return re.findall(
                rf"^{re.escape(name)}:[ \t]*(.*?)[ \t]*$",
                text, re.MULTILINE | re.IGNORECASE,
            )

        states = values("State")
        if len(states) != 1:
            raise EnvironmentError(f"{path}: State must appear exactly once")
        if states[0].lower() == "done":
            continue
        try:
            semantic_paths = readiness_module.builder_paths(text)
        except readiness_module.ReadinessError as error:
            raise EnvironmentError(f"{path}: {error}") from error
        for semantic_path in semantic_paths:
            selected_ownership.setdefault(semantic_path, []).append(ticket)
        if preview_provider == "none" and (
            not nonvisual_paths
            or any(
                not any(item.startswith(prefix) for prefix in nonvisual_paths)
                for item in semantic_paths
            )
        ):
            raise EnvironmentError(f"{ticket}: preview_capability_missing")
        decisions = values("Product-Decisions")
        if decisions != ["frozen"]:
            raise EnvironmentError(f"{path}: Product-Decisions must be exactly frozen")
        try:
            dependency_items = readiness_module.dependencies(text, ticket)
        except readiness_module.ReadinessError as error:
            raise EnvironmentError(f"{path}: {error}") from error
        try:
            readiness_module.validate(ticket, product)
        except (OSError, UnicodeError, readiness_module.ReadinessError) as error:
            raise EnvironmentError(
                f"{path}: READINESS BLOCKED: {error}"
            ) from error
        ticket_dependencies = set(dependency_items)
        internal = sorted(ticket_dependencies & cohort)
        if internal:
            raise EnvironmentError(
                f"qualification cohort dependency {ticket} -> {internal[0]}; "
                "use independent tickets or sequential generations"
            )
        for dependency in sorted(ticket_dependencies - cohort):
            try:
                protected_dependency(product, dependency)
            except TerminalError as error:
                raise EnvironmentError(
                    f"{ticket}: qualification dependency lacks protected "
                    f"evidence: {dependency}"
                ) from error
    if manifest.get("mode") != "successor":
        for path, owners in selected_ownership.items():
            if len(owners) > 1:
                raise EnvironmentError(
                    f"qualification Builder ownership conflict {path}: "
                    f"{','.join(sorted(owners))}"
                )
        tickets_dir = product / "factory/tickets"
        for path in sorted(tickets_dir.glob("T-*.md")):
            other = path.stem
            if other in cohort or not re.fullmatch(r"T-[0-9]+", other):
                continue
            try:
                text = path.read_text(encoding="utf-8")
                if readiness_module.field(text, "State").casefold() != "ready":
                    continue
                other_paths = readiness_module.builder_paths(text)
            except (OSError, UnicodeError, readiness_module.ReadinessError):
                continue
            for semantic_path in sorted(
                set(other_paths) & selected_ownership.keys()
            ):
                raise EnvironmentError(
                    f"qualification Builder ownership conflict {semantic_path}: "
                    f"{','.join(sorted(selected_ownership[semantic_path] + [other]))}"
                )
    for ticket in selected:
        path = f"factory/tickets/{ticket}.md"
        try:
            control_blob = command(
                "git", "-C", str(product), "rev-parse", f"HEAD:{path}"
            )
            protected_blob = command(
                "git", "-C", str(product), "rev-parse",
                f"refs/remotes/origin/main:{path}",
            )
        except EnvironmentError as error:
            raise EnvironmentError(
                f"{ticket}: protected dispatch ticket source is unavailable"
            ) from error
        if control_blob != protected_blob:
            raise EnvironmentError(
                f"{ticket}: qualification ticket source differs from protected dispatch"
            )


def validate_operator_map(value: dict[str, Any]) -> None:
    if set(value) != {"_config", "_sync", "initiatives", "tickets"} or any(
        not isinstance(value[key], dict)
        for key in ("_config", "_sync", "initiatives", "tickets")
    ):
        raise EnvironmentError("qualification operator map is malformed")
    sensitive = re.compile(r"(?:token|secret|password|api[_-]?key|authorization)", re.I)

    def reject_secrets(item: Any) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                if sensitive.search(str(key)):
                    raise EnvironmentError("qualification operator map contains secret material")
                reject_secrets(child)
        elif isinstance(item, list):
            for child in item:
                reject_secrets(child)

    reject_secrets(value)


def operator_seed(args: argparse.Namespace) -> tuple[Path, dict[str, Any], str]:
    argument = getattr(args, "operator_map_seed", None)
    configured = os.environ.get("FACTORY_QUALIFICATION_OPERATOR_MAP_SEED", "").strip()
    if argument and configured and Path(argument).expanduser() != Path(configured).expanduser():
        raise EnvironmentError("qualification operator map seed is ambiguous")
    raw = argument or configured
    if not raw:
        raise EnvironmentError("qualification operator map seed is required")
    source = Path(raw).expanduser()
    if not source.is_absolute():
        raise EnvironmentError("qualification operator map seed must be absolute")
    try:
        if source.is_symlink():
            raise EnvironmentError("qualification operator map seed is unsafe")
        source = source.resolve(strict=True)
        metadata = source.lstat()
        if metadata.st_nlink != 1:
            raise EnvironmentError("qualification operator map seed is unsafe")
        value = read(source)
        validate_operator_map(value)
    except (FileNotFoundError, OSError, json.JSONDecodeError) as error:
        raise EnvironmentError("qualification operator map seed is unsafe") from error
    digest = hashlib.sha256(canonical(value)).hexdigest()
    return source, value, digest


def validate_runtime_ledger(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as error:
        raise EnvironmentError("qualification runtime ledger is unsafe") from error
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise EnvironmentError("qualification runtime ledger is unsafe")
    finally:
        os.close(descriptor)


def operator_authority_sha256(
    identity: dict[str, Any], selected: list[str],
) -> str:
    return hashlib.sha256(canonical({
        "operator_map_path": identity["operator_map_path"],
        "product_origin": identity["product_origin"],
        "product_path": identity["product_path"],
        "project": identity["project"],
        "runtime_ledger_path": identity["runtime_ledger_path"],
        "selected_tickets": selected,
    })).hexdigest()


def prepare_operator_state(
    authority: Path,
    identity: dict[str, Any],
    selected: list[str],
    seed: tuple[Path, dict[str, Any], str],
) -> tuple[Path, Path]:
    source, value, source_sha256 = seed
    operator = authority / "operator"
    if operator.exists() or operator.is_symlink():
        safe_directory(operator)
    else:
        operator.mkdir(mode=0o700)
    map_path = operator / "operator-map.json"
    ledger_path = operator / "runtime-ledger.csv"
    bootstrap = authority / "operator-bootstrap.json"
    expected = {
        "operator_authority_sha256": operator_authority_sha256(identity, selected),
        "operator_map_path": str(map_path),
        "runtime_ledger_path": str(ledger_path),
        "schema": OPERATOR_BOOTSTRAP_SCHEMA,
        "selected_tickets": selected,
        "source_path": str(source),
        "source_sha256": source_sha256,
    }
    if bootstrap.exists() or bootstrap.is_symlink():
        if read(bootstrap) != expected:
            raise EnvironmentError("qualification operator bootstrap changed")
        validate_operator_map(read(map_path))
        return map_path, ledger_path
    if map_path.exists() or map_path.is_symlink():
        if read(map_path) != value:
            raise EnvironmentError("partial qualification operator map is invalid")
    else:
        write(map_path, value)
    write(bootstrap, expected)
    return map_path, ledger_path


def resume_operator_state(
    authority: Path, identity: dict[str, Any], selected: list[str],
) -> tuple[Path, Path]:
    map_path = authority / "operator/operator-map.json"
    ledger_path = authority / "operator/runtime-ledger.csv"
    value = read(authority / "operator-bootstrap.json")
    if (
        value.get("schema") != OPERATOR_BOOTSTRAP_SCHEMA
        or value.get("operator_authority_sha256")
        != operator_authority_sha256(identity, selected)
        or value.get("selected_tickets") != selected
        or value.get("operator_map_path") != str(map_path)
        or value.get("runtime_ledger_path") != str(ledger_path)
        or not isinstance(value.get("source_path"), str)
        or not Path(value["source_path"]).is_absolute()
        or any(character in value["source_path"] for character in "\r\n\t")
        or not re.fullmatch(r"[0-9a-f]{64}", value.get("source_sha256", ""))
    ):
        raise EnvironmentError("qualification operator bootstrap changed")
    safe_directory(authority / "operator")
    validate_operator_map(read(map_path))
    return map_path, ledger_path


def initialize_selected_operator(
    factory: Path, product: Path, map_path: Path, ledger_path: Path,
    state_dir: Path, *, refresh: bool = False,
) -> None:
    try:
        mapping = read(map_path)
        validate_operator_map(mapping)
        selected = json.loads(
            (product / "factory/QUALIFICATION.json").read_text(encoding="utf-8")
        )["tickets"]
    except (KeyError, OSError, json.JSONDecodeError) as error:
        raise EnvironmentError("qualification operator map is malformed") from error
    environment = {
        **os.environ,
        "FACTORY_OPERATOR_MAP": str(map_path),
        "FACTORY_CONTROLLER_STATE_DIR": str(state_dir),
    }
    for ticket in selected:
        mapping = read(map_path)
        if not refresh and selected_operator_ready(mapping, ticket):
            continue
        text, _source = committed_ticket(product / "factory", ticket)
        states = re.findall(r"(?mi)^State:\s*(.*?)\s*$", text or "")
        action = (
            "ready"
            if len(states) == 1 and states[0].casefold() == "backlog"
            else "init"
        )
        result = subprocess.run(
            [
                sys.executable, str(factory / "scripts/operator-cli.py"),
                "--product", str(product), "--state-dir", str(state_dir),
                action, "--ticket", ticket,
            ],
            text=True, capture_output=True, check=False, timeout=120,
            env=environment,
        )
        if result.returncode:
            raise EnvironmentError(
                f"{ticket}: selected-ticket operator initialization failed: "
                f"{result.stdout.strip() or result.stderr.strip()}"
            )
        mapping = read(map_path)
        validate_operator_map(mapping)
        if not selected_operator_ready(mapping, ticket):
            raise EnvironmentError(
                f"{ticket}: selected-ticket operator initialization was not durable"
            )
    ledger = subprocess.run(
        [
            sys.executable, str(factory / "scripts/ledger-view.py"), "refresh",
            "--factory-root", str(product),
            "--runtime-ledger", str(ledger_path),
        ],
        text=True, capture_output=True, check=False, timeout=120,
        env=environment,
    )
    if ledger.returncode:
        raise EnvironmentError(
            "qualification runtime ledger refresh failed: "
            f"{ledger.stdout.strip() or ledger.stderr.strip()}"
        )


def selected_operator_ready(mapping: dict[str, Any], ticket: str) -> bool:
    entry = mapping["tickets"].get(ticket)
    initialized = mapping["_sync"].get("selected_ticket_success_at", {})
    return bool(
        isinstance(entry, dict)
        and entry.get("operator_fields_initialized") is True
        and not (
            isinstance(entry.get("operator"), dict)
            and entry["operator"].get("state") == "Ready"
            and entry["operator"].get("state_base") == "backlog"
        )
        and isinstance(initialized, dict)
        and isinstance(initialized.get(ticket), str)
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


def validate_prepare_provider(
    release: Path, root: Path, capacity: int,
) -> tuple[dict[str, Any], dict[str, Any], str]:
    policy, activation, policy_hash = provider_configuration(release, capacity)
    provider = root / "provider"
    if not (provider.exists() or provider.is_symlink()):
        return policy, activation, policy_hash
    safe_directory(provider)
    allowed = {
        "accounting", "cli-runtimes", "provider-activation.json",
        "provider-apply-locks", "provider-attempts",
        "provider-configuration.lock", "provider-policy.json",
    }
    if any(path.name not in allowed for path in provider.iterdir()):
        raise EnvironmentError("partial qualification provider is invalid")
    directories = (
        "accounting",
        "cli-runtimes",
        "provider-attempts",
        "provider-apply-locks",
    )
    for name in directories:
        path = provider / name
        if path.exists() or path.is_symlink():
            safe_directory(path)
    if any(
        (provider / name).is_dir() and any((provider / name).iterdir())
        for name in (
        "cli-runtimes", "provider-attempts", "provider-apply-locks",
        )
    ):
        raise EnvironmentError("partial qualification provider is active")
    accounting = provider / "accounting"
    if accounting.is_dir() and any(
        path.name != "state-v2.sqlite3" for path in accounting.iterdir()
    ):
        raise EnvironmentError("partial qualification provider is invalid")
    configuration_lock = provider / "provider-configuration.lock"
    prefix = [provider / name for name in directories] + [
        configuration_lock,
        provider / "provider-policy.json",
        provider / "provider-activation.json",
        accounting / "state-v2.sqlite3",
    ]
    present = [path.exists() or path.is_symlink() for path in prefix]
    if present != sorted(present, reverse=True):
        raise EnvironmentError("qualification preparation state is torn")
    if (
        configuration_lock.exists() or configuration_lock.is_symlink()
    ) and config_bytes(configuration_lock):
        raise EnvironmentError("qualification preparation artifact changed")
    for path, value in (
        (provider / "provider-policy.json", policy),
        (provider / "provider-activation.json", activation),
    ):
        if (
            path.exists() or path.is_symlink()
        ) and config_bytes(path) != canonical(value):
            raise EnvironmentError("qualification preparation artifact changed")
    return policy, activation, policy_hash


def prepare_provider(
    release: Path, root: Path, capacity: int, contract: str,
) -> str:
    policy, activation, _ = validate_prepare_provider(release, root, capacity)
    provider = root / "provider"
    ensure_directory(provider)
    for name in (
        "accounting", "cli-runtimes", "provider-attempts",
        "provider-apply-locks",
    ):
        ensure_directory(provider / name)
    configuration_lock = provider / "provider-configuration.lock"
    write_bytes_exact(configuration_lock, b"")
    policy_path = provider / "provider-policy.json"
    activation_path = provider / "provider-activation.json"
    write_exact(policy_path, policy)
    write_exact(activation_path, activation)
    command(
        "/usr/bin/python3",
        str(release / "scripts/provider-activation.py"),
        "--config", str(activation_path),
        "--policy", str(policy_path),
        "--contract-version", contract,
        "--status",
    )
    command(
        "/usr/bin/python3",
        str(release / "scripts/provider-coordinator.py"),
        "--db",
        str(provider / "accounting/state-v2.sqlite3"),
        "status",
    )
    return validate_provider(release, root, capacity, contract, pristine=True)


def validate_provider(
    release: Path, root: Path, capacity: int, contract: str,
    *, pristine: bool = False,
) -> str:
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
        "--contract-version", contract, "--status",
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
        or (pristine and attempts)
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


def qualification_publication_origin(product: Path, origin: str) -> None:
    try:
        repository = historical_product_repository(product)
    except HistoricalObjectError as error:
        raise EnvironmentError(str(error)) from error
    value = origin[:-4] if origin.endswith(".git") else origin
    if value not in {
        f"https://github.com/{repository}",
        f"git@github.com:{repository}",
        f"ssh://git@github.com/{repository}",
    }:
        raise EnvironmentError(
            "qualification requires the protected GitHub product origin"
        )


def historical_pr_objects(product: Path, origin: str) -> int:
    try:
        return hydrate_historical_pr_objects(product, origin)
    except HistoricalObjectError as error:
        raise EnvironmentError(str(error)) from error


def validate_selected_remote_branches(
    factory: Path, product: Path, manifest: dict[str, Any], origin: str,
    *, exact_authorizations: bool, mapping_path: Path | None = None,
    controller: Path | None = None,
) -> dict[str, str]:
    spec = importlib.util.spec_from_file_location(
        "qualification_prepare_dispatch", factory / "scripts/dispatch-plan.py",
    )
    if not spec or not spec.loader:
        raise EnvironmentError("qualification dispatch validator is unavailable")
    dispatch = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dispatch)
    try:
        prepared = (
            dispatch.authenticated_prepared_ready_receipts(
                mapping_path, controller, manifest,
            )
            if mapping_path is not None
            else {}
        )
        return dispatch.inspect_selected_preprovider_branches(
            product, product / "factory", manifest, origin,
            exact_authorizations=exact_authorizations,
            prepared_ready_receipts=prepared,
        )
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise EnvironmentError(
            f"qualification selected branch is not dispatchable: {error}"
        ) from error


def certification_preflight(
    factory: Path, product: Path, sha: str, tree: str, contract: str,
    runtime_bin: Path | None = None,
) -> dict[str, str] | None:
    plan = product / "factory/certification-plan.json"
    if not plan.exists() and not plan.is_symlink():
        return None
    environment = None
    if runtime_bin is not None:
        environment = {
            **os.environ,
            "PATH": f"{runtime_bin}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        }
    result = command(
        "/usr/bin/python3",
        str(factory / "scripts/certification-preflight.py"),
        "--plan", str(plan),
        "--factory-sha", sha,
        "--factory-tree", tree,
        "--product-root", str(product),
        "--contract-version", contract,
        cwd=product, environment=environment,
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


def prepare_qualification_runtime(
    release: Path, product: Path, root: Path, project: str,
    explicit: Path | None,
) -> Path:
    spec = importlib.util.spec_from_file_location(
        "qualification_release_transaction",
        Path(__file__).resolve().with_name("release-transaction.py"),
    )
    if not spec or not spec.loader:
        raise EnvironmentError("qualification runtime helper is unavailable")
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    try:
        result = helper.prepare_runtime(
            release, product, root / "kits", project, explicit,
        )
    except (OSError, RuntimeError, ValueError) as error:
        if str(error).startswith("runtime_tuple_mismatch:"):
            raise EnvironmentError(str(error), "runtime_tuple_mismatch") from error
        raise EnvironmentError("qualification runtime pin failed") from error
    path = Path(str(result.get("evidence", {}).get("path", "")))
    expected = root / "project-runtimes" / project / "bin"
    if path != expected:
        raise EnvironmentError("qualification runtime pin is invalid")
    return path


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
    source = source_product / "factory/operator-map.json"
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
        or manifest.get("contract_version") not in SUPPORTED_CONTRACTS
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
        or active.get("contract_version") not in SUPPORTED_CONTRACTS
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
        "--contract-version", manifest["contract_version"],
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


def successor_terminal_reconciliations(
    factory: Path, product: Path, controller: Path, active_product_sha: str,
    manifest: dict[str, Any], absent: set[str],
) -> None:
    """Accept only the source lane's exact protected-terminal reconciliations."""
    source = manifest["source_factory_sha"]
    if not SHA.fullmatch(active_product_sha):
        raise EnvironmentError("successor source product identity is invalid")
    spec = importlib.util.spec_from_file_location(
        "qualification_upgrade_reducer", factory / "scripts/qualification-reducer.py"
    )
    if not spec or not spec.loader:
        raise EnvironmentError("successor reconciliation verifier is unavailable")
    reducer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(reducer)
    source_manifest = validate_qualification_manifest(
        json.loads(command(
            "git", "-C", str(product), "show",
            f"{active_product_sha}:factory/QUALIFICATION.json",
        )),
        source,
    )
    if source_manifest["tickets"] != manifest["tickets"]:
        raise EnvironmentError("successor source cohort changed")
    events = reducer.qualification_events(
        reducer.event_records(safe_directory(controller / "events")),
        source_manifest,
    )
    reconciliations = reducer.protected_reconciliations(events, source)
    if set(reconciliations) != absent or any(
        (controller / "passports" / f"{ticket}.json").exists()
        or (controller / "passports" / f"{ticket}.json").is_symlink()
        for ticket in reconciliations
    ):
        raise EnvironmentError("successor terminal reconciliation set changed")
    protected = command(
        "git", "-C", str(product), "rev-parse", "refs/remotes/origin/main",
    )
    command(
        "git", "-C", str(product), "merge-base", "--is-ancestor",
        active_product_sha, protected,
    )
    allowed = {
        "done_sha256", "event", "event_sha256", "factory_sha",
        "observed_at_epoch_ns", "protected_main_sha", "protected_main_tree",
        "protected_ticket_blob", "qualification_charge_micro_usd",
        "qualification_generation", "qualification_manifest_sha256",
        "reconciliation_schema", "schema", "terminal_basis", "ticket",
    }
    for ticket, event in reconciliations.items():
        ticket_path = f"factory/tickets/{ticket}.md"
        done_path = f"factory/attestations/{ticket}/done.json"
        observed = event.get("protected_main_sha", "")
        observed_done = json.loads(command(
            "git", "-C", str(product), "show", f"{observed}:{done_path}",
        ))
        current_done = json.loads(command(
            "git", "-C", str(product), "show", f"{protected}:{done_path}",
        ))
        terminal = protected_terminal(product, ticket, protected)
        if (
            set(event) != allowed
            or event.get("schema") != reducer.EVENT_SCHEMA
            or event.get("event") != "protected_terminal_reconciled"
            or event.get("factory_sha") != source
            or event.get("ticket") != ticket
            or event.get("reconciliation_schema")
            != reducer.PROTECTED_TERMINAL_RECONCILIATION_SCHEMA
            or event.get("terminal_basis") not in {
                "attested-done", "attested-emergency-closeout",
            }
            or event.get("qualification_charge_micro_usd") != 0
            or event.get("protected_main_sha") != active_product_sha
            or not isinstance(event.get("observed_at_epoch_ns"), int)
            or isinstance(event["observed_at_epoch_ns"], bool)
            or event["observed_at_epoch_ns"] < 1
            or command(
                "git", "-C", str(product), "rev-parse", f"{observed}^{{tree}}",
            ) != event.get("protected_main_tree")
            or command(
                "git", "-C", str(product), "rev-parse", f"{observed}:{ticket_path}",
            ) != event.get("protected_ticket_blob")
            or command(
                "git", "-C", str(product), "rev-parse", f"{protected}:{ticket_path}",
            ) != event.get("protected_ticket_blob")
            or current_done != observed_done
            or hashlib.sha256(
                reducer.canonical(observed_done).encode()
            ).hexdigest() != event.get("done_sha256")
            or terminal.get("ticket") != ticket
            or terminal.get("basis") != event.get("terminal_basis")
        ):
            raise EnvironmentError(
                f"{ticket}: successor terminal reconciliation changed"
            )


def conservative_success_evidence(
    passport: Any, product: Path, ticket: str,
    charge: dict[str, Any], completed: dict[str, Any],
) -> bool:
    """Recheck the retained run proof compressed into a conservative passport."""
    matches = []
    runs = passport.safe_directory(product.resolve(strict=True) / "factory/runs")
    for path in runs.glob("*.meta"):
        raw = passport.read_regular(path)
        if hashlib.sha256(raw).hexdigest() != charge["manifest_sha256"]:
            continue
        fields = passport.manifest_fields(path)
        output = path.with_suffix(".out")
        if (
            fields.get("ticket") == ticket
            and fields.get("accounting_state") == "abandoned_conservative"
            and fields.get("cost_basis") == "conservative_reservation"
            and fields.get("effective_cost") == fields.get("reserved_usd")
            and fields.get("phase") == "completed"
            and fields.get("task_submitted") == "1"
            and fields.get("exit_status") == "0"
            and fields.get("role_exit") == "ok"
            and fields.get("run_id") == charge["run_id"]
            and fields.get("contract_version") == charge["contract_version"]
            and fields.get("kit_sha") == charge["factory_sha"]
            and fields.get("role_head_before") == charge["head_before"]
            and fields.get("role") == charge["role"]
            and fields.get("transition_receipt_sha256")
            == charge["transition_receipt_sha256"]
            and passport.micro_usd(fields) == charge["charge_micro_usd"]
            and fields.get("output_sha256") == completed["output_sha256"]
            and passport.role_output_digest(output) == completed["output_sha256"]
        ):
            matches.append(path)
    return len(matches) == 1


def completed_charge_matches(
    passport: Any, product: Path, ticket: str,
    charge: dict[str, Any], completed: dict[str, Any],
    corrected: set[tuple[str, str, str]] | None = None,
) -> bool:
    return (
        charge.get("run_id") == completed["run_id"]
        and charge.get("factory_sha") == completed["factory_sha"]
        and charge.get("head_before") == completed["head_before"]
        and charge.get("manifest_sha256") == completed["manifest_sha256"]
        and charge.get("role") == completed["role"]
        and charge.get("transition_receipt_sha256")
        == completed["transition_receipt_sha256"]
        and (
            charge.get("accounting_state") == "completed"
            or charge.get("accounting_state") == "abandoned_conservative"
            and (
                (
                    completed["run_id"],
                    completed["transition_receipt_sha256"],
                    completed["factory_sha"],
                ) in (corrected or ())
                or conservative_success_evidence(
                    passport, product, ticket, charge, completed,
                )
            )
        )
    )


def completed_role_gap(
    factory: Path, product: Path, passport: Any, ticket: str,
    charges: list[dict[str, Any]], completed: list[dict[str, Any]],
    start: str, end: str, source: str,
    corrected: set[tuple[str, str, str]] | None = None,
) -> bool:
    """Bind a migration head gap to exact successful role-owned commits."""
    if not SHA.fullmatch(start) or not SHA.fullmatch(end) or start == end:
        return False
    ancestry = command(
        "git", "-C", str(product), "rev-list", "--reverse", "--ancestry-path",
        f"{start}..{end}",
    ).splitlines()
    if not ancestry or ancestry[-1] != end or len(ancestry) > 64:
        return False
    positions = {head: index for index, head in enumerate([start, *ancestry])}
    chain = [
        item for item in completed
        if isinstance(item, dict)
        and item.get("factory_sha") == source
        and item.get("head_before") in positions
        and item.get("head_before") != end
    ]
    chain.sort(key=lambda item: positions[item["head_before"]])
    if (
        not chain
        or chain[0].get("head_before") != start
        or len({item.get("head_before") for item in chain}) != len(chain)
    ):
        return False
    policy_raw = json.loads(passport.read_regular(
        factory / "scripts/model-routing/handoff-boundaries-v1.json"
    ))
    policy = passport.RoleBoundaryPolicy.from_dict(json.loads(
        json.dumps(policy_raw, sort_keys=True).replace("TICKET", ticket)
    ))
    for index, item in enumerate(chain):
        following = (
            chain[index + 1]["head_before"] if index + 1 < len(chain) else end
        )
        if (
            positions[following] <= positions[item["head_before"]]
            or sum(
                completed_charge_matches(
                    passport, product, ticket, charge, item, corrected,
                )
                for charge in charges
            ) != 1
        ):
            return False
        parent = item["head_before"]
        for commit in ancestry[
            positions[parent]:positions[following]
        ]:
            if command(
                "git", "-C", str(product), "show", "-s", "--format=%P", commit,
            ).split() != [parent]:
                return False
            parent = commit
        passport._validate_committed_changes(
            product, item["head_before"], following, item["role"], policy,
            allow_spec_lint_append=True,
        )
        sentinel = subprocess.run(
            [
                sys.executable,
                str(factory / "scripts/lib/lane-path-sentinel.py"),
                str(product), item["head_before"], following,
            ],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            check=False, timeout=120,
        )
        if sentinel.returncode:
            return False
    return True


def successor_checkpoint_authorizations(
    product: Path, manifest: dict[str, Any], protected: str | None = None,
) -> tuple[dict[str, Any] | None, dict[str, dict[str, Any]]]:
    """Load one exact protected checkpoint set for a successor cohort."""
    target = manifest["factory_sha"]
    protected = protected or command("git", "-C", str(product), "rev-parse", "HEAD")
    if not SHA.fullmatch(protected):
        raise EnvironmentError("successor checkpoint authorization is invalid")
    relative = f"factory/migrations/inflight-release/{target}.json"
    entry = command(
        "git", "-C", str(product), "ls-tree", protected, "--", relative,
    )
    if not entry:
        return None, {}
    if not re.fullmatch(
        rf"100644 blob [0-9a-f]{{40}}\t{re.escape(relative)}", entry,
    ):
        raise EnvironmentError("successor checkpoint authorization is unsafe")
    try:
        authorization, entries = parse_inflight_authorization(
            command("git", "-C", str(product), "show", f"{protected}:{relative}"),
            command(
                "git", "-C", str(product), "show",
                f"{protected}:factory/PROJECT.env",
            ),
            target,
        )
    except (InflightAuthorizationError, OSError, ValueError) as error:
        raise EnvironmentError(
            "successor checkpoint authorization is invalid"
        ) from error
    if (
        authorization.get("source_kit_sha") != manifest["source_factory_sha"]
        or set(entries) != set(manifest["tickets"])
    ):
        raise EnvironmentError("successor checkpoint authorization is not exact")
    return authorization, entries


def validate_successor_upgrade_cohort(
    factory: Path, product: Path, controller: Path, project: str,
    active_factory_sha: str, active_product_sha: str,
    manifest: dict[str, Any],
) -> None:
    """Require every in-place successor target to descend from its source."""
    if manifest.get("mode") != "successor":
        return
    ticket = "selected cohort"
    try:
        passport_spec = importlib.util.spec_from_file_location(
            "qualification_upgrade_passport", factory / "scripts/ticket-passport.py"
        )
        if not passport_spec or not passport_spec.loader:
            raise EnvironmentError("successor passport verifier is unavailable")
        passport = importlib.util.module_from_spec(passport_spec)
        passport_spec.loader.exec_module(passport)
        passports = safe_directory(controller / "passports")
        secret = passport.read_regular(controller / "passport.key", 0o600, 32)
        if len(secret) != 32:
            raise EnvironmentError("successor passport key is invalid")
        source = manifest["source_factory_sha"]
        checkpoint_authorization, checkpoint_entries = (
            successor_checkpoint_authorizations(product, manifest)
        )
        absent = {
            ticket for ticket in manifest["tickets"]
            if not (passports / f"{ticket}.json").exists()
            and not (passports / f"{ticket}.json").is_symlink()
        }
        if absent:
            successor_terminal_reconciliations(
                factory, product, controller, active_product_sha,
                manifest, absent,
            )
        for ticket in manifest["tickets"]:
            if ticket in absent:
                continue
            value, _ = passport.load_passport(passports / f"{ticket}.json", secret)
            passport_factory = value.get("factory_sha", "")
            if not isinstance(passport_factory, str) or not SHA.fullmatch(
                passport_factory
            ):
                raise EnvironmentError("successor passport release is invalid")
            passport_release_authorized = passport_factory in {
                source, active_factory_sha,
            }
            checkpoint_bridge = False
            checkpoint = checkpoint_entries.get(ticket)
            if (
                not passport_release_authorized
                and active_factory_sha == source
                and checkpoint_authorization is not None
                and checkpoint is not None
                and ticket_source_kit(
                    checkpoint_authorization, checkpoint,
                ) == passport_factory
            ):
                source_manifest = validate_qualification_manifest(
                    json.loads(command(
                        "git", "-C", str(product), "show",
                        f"{active_product_sha}:factory/QUALIFICATION.json",
                    )),
                    source,
                )
                source_authorization, source_entries = (
                    successor_checkpoint_authorizations(
                        product, source_manifest, active_product_sha,
                    )
                )
                source_checkpoint = source_entries.get(ticket)
                checkpoint_bridge = (
                    source_manifest["tickets"] == manifest["tickets"]
                    and source_authorization is not None
                    and source_authorization.get("schema")
                    == "nysa.software-factory.inflight-release-authorization/v2"
                    and checkpoint_authorization.get("schema")
                    == "nysa.software-factory.inflight-release-authorization/v2"
                    and source_entries == checkpoint_entries
                    and source_checkpoint is not None
                    and ticket_source_kit(
                        source_authorization, source_checkpoint,
                    ) == passport_factory
                )
            if not passport_release_authorized and not checkpoint_bridge:
                prior_manifest = dict(manifest, factory_sha=passport_factory)
                prior, _ = successor_checkpoint_authorizations(
                    product, prior_manifest, active_product_sha,
                )
                if prior is None:
                    raise EnvironmentError(
                        "successor passport release is not authorized"
                    )
                passport_release_authorized = True
            valid_sha = lambda item: (  # noqa: E731
                isinstance(item, str) and SHA.fullmatch(item) is not None
            )
            valid_digest = lambda item: (  # noqa: E731
                isinstance(item, str)
                and passport.DIGEST.fullmatch(item) is not None
            )
            history = value.get("factory_release_history")
            migrations = value.get("migration_history")
            releases = [
                item.get("factory_sha")
                for item in history or []
                if isinstance(item, dict)
                and set(item) == {"contract_version", "factory_sha"}
                and item.get("contract_version") in SUPPORTED_CONTRACTS
                and valid_sha(item.get("factory_sha"))
            ]
            charges = value.get("charge_records")
            completed = value.get("completed_role_evidence")
            charge_keys = {
                "accounting_state", "charge_micro_usd", "contract_version",
                "factory_sha", "head_before", "manifest_sha256", "role",
                "run_id", "transition_receipt_sha256",
            }
            completed_keys = {
                "contract_version", "factory_sha", "head_before",
                "manifest_sha256", "output_sha256", "role", "run_id",
                "transition_receipt_sha256",
            }
            candidate = manifest["factory_sha"]
            charge_ids = {
                item.get("run_id") for item in charges or []
                if isinstance(item, dict)
                and isinstance(item.get("run_id"), str)
            }
            completed_ids = {
                item.get("run_id") for item in completed or []
                if isinstance(item, dict)
                and isinstance(item.get("run_id"), str)
            }
            corrections = value.get("completed_role_corrections", [])
            try:
                corrections = passport.validate_completion_corrections(
                    corrections, completed if isinstance(completed, list) else []
                )
            except (AttributeError, ValueError):
                corrections = None
            corrected = {
                (
                    item["run_id"], item["transition_receipt_sha256"],
                    item["failed_factory_sha"],
                )
                for item in corrections or []
            }
            core_valid = (
                value.get("schema") == "nysa.software-factory.ticket-passport/v1"
                and value.get("contract_version") in SUPPORTED_CONTRACTS
                and value.get("branch") == f"ticket/{ticket}"
                and valid_sha(value.get("head_sha"))
                and valid_sha(value.get("head_tree"))
                and valid_sha(value.get("ticket_blob"))
                and valid_digest(value.get("product_origin_sha256"))
                and valid_sha(value.get("protected_base_sha"))
                and valid_digest(value.get("route_plan_sha256"))
                and valid_digest(value.get("transition_receipt_sha256"))
                and isinstance(value.get("nonce"), str)
                and re.fullmatch(r"[0-9a-f]{32}", value["nonce"])
                and value.get("current_state") in {
                    "Ready", "Planning", "Building", "Review",
                    "Awaiting Approval", "Approved", "Blocked-Escalated",
                }
                and isinstance(value.get("current_stage"), str)
                and bool(value["current_stage"])
                and value.get("publication_state") in {
                    "none", "validating", "ready", "merge-pending",
                    "merged", "repair",
                }
                and isinstance(value.get("base_history"), list)
                and value["base_history"]
                and all(
                    valid_sha(item)
                    for item in value["base_history"]
                )
                and len(value["base_history"]) == len(set(value["base_history"]))
                and value["protected_base_sha"] in value["base_history"]
                and (
                    (
                        value.get("parent_digest"),
                        value.get("parent_file_sha256"),
                    ) == (None, None)
                    or (
                        valid_digest(value.get("parent_digest"))
                        and valid_digest(value.get("parent_file_sha256"))
                    )
                )
            )
            release_history_valid = (
                isinstance(history, list)
                and releases
                and len(releases) == len(history) == len(set(releases))
                and releases[-1] == value.get("factory_sha")
                and isinstance(migrations, list)
                and all(passport.valid_v2_migration(item) for item in migrations)
                and (
                    not migrations and len(releases) == 1
                    or bool(migrations)
                    and migrations[0]["from_factory_sha"] == releases[0]
                    and migrations[-1]["to_factory_sha"] == releases[-1]
                    and [
                        (item["from_factory_sha"], item["to_factory_sha"])
                        for item in migrations
                        if item["from_factory_sha"] != item["to_factory_sha"]
                    ] == list(zip(releases, releases[1:]))
                    and migrations[-1]["to_protected_base_sha"]
                    == value.get("protected_base_sha")
                    and migrations[-1]["to_route_plan_sha256"]
                    == value.get("route_plan_sha256")
                )
            )
            release_lineage_valid = (
                release_history_valid
                and source in releases
                and passport_release_authorized
                and successor_release_lineage(
                    history, migrations, source,
                    value.get("factory_sha", ""), passport.valid_v2_migration,
                )
            )
            checkpoint_bridge_valid = (
                release_history_valid
                and checkpoint_bridge
                and successor_release_lineage(
                    history, migrations, passport_factory,
                    value.get("factory_sha", ""), passport.valid_v2_migration,
                )
            )
            try:
                strict_lineage_valid = (
                    release_lineage_valid
                    and (
                        not migrations and len(releases) == 1
                        or bool(migrations)
                        and all(
                            prior["to_factory_sha"]
                            == following["from_factory_sha"]
                            and prior["to_protected_base_sha"]
                            == following["from_protected_base_sha"]
                            and prior["to_route_plan_sha256"]
                            == following["from_route_plan_sha256"]
                            and (
                                prior["to_head_sha"]
                                == following["from_head_sha"]
                                or isinstance(charges, list)
                                and isinstance(completed, list)
                                and completed_role_gap(
                                    factory, product, passport, ticket,
                                    charges, completed, prior["to_head_sha"],
                                    following["from_head_sha"],
                                    prior["to_factory_sha"], corrected,
                                )
                            )
                            for prior, following in zip(
                                migrations, migrations[1:]
                            )
                        )
                        and migrations[-1]["to_head_sha"]
                        == value.get("head_sha")
                        and migrations[-1]["from_passport_sha256"]
                        == value.get("parent_digest")
                        and migrations[-1]["from_passport_file_sha256"]
                        == value.get("parent_file_sha256")
                    )
                )
            except (OSError, TypeError, ValueError, passport.PassportError):
                strict_lineage_valid = False
            checkpoint_valid = False
            if checkpoint_authorization is not None and checkpoint is not None:
                try:
                    passport_route = subprocess.run(
                        [
                            "git", "-C", str(product), "show",
                            f"{value.get('head_sha', '')}:factory/route-plans/"
                            f"{ticket}.json",
                        ],
                        capture_output=True, check=False, timeout=120,
                    )
                    checkpoint_route = subprocess.run(
                        [
                            "git", "-C", str(product), "show",
                            f"{checkpoint['head']}:factory/route-plans/"
                            f"{ticket}.json",
                        ],
                        capture_output=True, check=False, timeout=120,
                    )
                    passport_route_sha256 = hashlib.sha256(
                        passport_route.stdout
                    ).hexdigest()
                    checkpoint_route_sha256 = hashlib.sha256(
                        checkpoint_route.stdout
                    ).hexdigest()
                    route_checkpoint_valid = (
                        passport_route_sha256 == value.get("route_plan_sha256")
                        and (
                            checkpoint_route_sha256
                            == value.get("route_plan_sha256")
                            or value.get("factory_sha") in releases
                            and verify_inflight_migration(
                                product, active_product_sha,
                                value["factory_sha"], ticket,
                                value.get("branch", ""), checkpoint["head"],
                            ) == "replay"
                        )
                    )
                    checkpoint_valid = (
                        (release_lineage_valid or checkpoint_bridge_valid)
                        and ticket_source_kit(
                            checkpoint_authorization, checkpoint,
                        ) in releases
                        and verify_inflight_migration(
                            product,
                            command(
                                "git", "-C", str(product), "rev-parse", "HEAD",
                            ),
                            candidate, ticket, value.get("branch", ""),
                            checkpoint["head"],
                        ) == "exact"
                        and subprocess.run(
                            [
                                "git", "-C", str(product), "merge-base",
                                "--is-ancestor", value.get("head_sha", ""),
                                checkpoint["head"],
                            ],
                            capture_output=True, check=False, timeout=120,
                        ).returncode == 0
                        and command(
                            "git", "-C", str(product), "rev-parse",
                            f"{value.get('head_sha', '')}^{{tree}}",
                        ) == value.get("head_tree")
                        and command(
                            "git", "-C", str(product), "rev-parse",
                            f"{value.get('head_sha', '')}:factory/tickets/"
                            f"{ticket}.md",
                        ) == value.get("ticket_blob")
                        and passport_route.returncode == 0
                        and checkpoint_route.returncode == 0
                        and route_checkpoint_valid
                    )
                except InflightAuthorizationError:
                    checkpoint_valid = False
            lineage_valid = strict_lineage_valid or checkpoint_valid
            evidence_valid = (
                isinstance(charges, list)
                and isinstance(completed, list)
                and len(charge_ids) == len(charges)
                and len(completed_ids) == len(completed)
                and all(
                    isinstance(item, dict)
                    and set(item) == charge_keys
                    and item.get("accounting_state") in passport.TERMINAL_ACCOUNTING
                    and isinstance(item.get("charge_micro_usd"), int)
                    and not isinstance(item["charge_micro_usd"], bool)
                    and item["charge_micro_usd"] >= 0
                    and item.get("contract_version") in SUPPORTED_CONTRACTS
                    and item.get("factory_sha") in releases
                    and (
                        active_factory_sha == candidate
                        or item.get("factory_sha") != candidate
                    )
                    and valid_sha(item.get("head_before"))
                    and valid_digest(item.get("manifest_sha256"))
                    and item.get("role") in passport.RECOVERABLE_ROLES
                    and isinstance(item.get("run_id"), str)
                    and passport.RUN_ID.fullmatch(item["run_id"])
                    and valid_digest(item.get("transition_receipt_sha256"))
                    for item in charges
                )
                and all(
                    isinstance(item, dict)
                    and set(item) == completed_keys
                    and item.get("contract_version") in SUPPORTED_CONTRACTS
                    and item.get("factory_sha") in releases
                    and (
                        active_factory_sha == candidate
                        or item.get("factory_sha") != candidate
                    )
                    and valid_sha(item.get("head_before"))
                    and valid_digest(item.get("manifest_sha256"))
                    and valid_digest(item.get("output_sha256"))
                    and item.get("role") in passport.RECOVERABLE_ROLES
                    and isinstance(item.get("run_id"), str)
                    and passport.RUN_ID.fullmatch(item["run_id"])
                    and valid_digest(item.get("transition_receipt_sha256"))
                    and sum(
                        completed_charge_matches(
                            passport, product, ticket, charge, item, corrected,
                        )
                        for charge in charges
                    ) == 1
                    for item in completed
                )
                and isinstance(value.get("cumulative_charges_micro_usd"), int)
                and not isinstance(value["cumulative_charges_micro_usd"], bool)
                and value["cumulative_charges_micro_usd"] >= 0
                and value.get("cumulative_charges_micro_usd") == sum(
                    item["charge_micro_usd"] for item in charges
                )
            )
            if (
                value.get("ticket") != ticket
                or value.get("project") != project
                or not core_valid
                or not lineage_valid
                or not evidence_valid
                or corrections is None
                or any(
                    item.get("failed_factory_sha") not in releases
                    or item.get("recovery_factory_sha") not in releases
                    or (
                        active_factory_sha != candidate
                        and candidate in {
                            item.get("failed_factory_sha"),
                            item.get("recovery_factory_sha"),
                        }
                    )
                    for item in corrections
                )
            ):
                raise EnvironmentError("successor passport is not source-bound")
    except (
        AttributeError, FileNotFoundError, KeyError, OSError, TypeError, ValueError,
    ) as error:
        if isinstance(error, EnvironmentError) and str(error).startswith(
            "successor qualification requires"
        ):
            raise
        raise EnvironmentError(
            f"{ticket}: successor qualification requires every selected ticket "
            "to be bound to its source Factory; use a fresh ordinary qualification"
        ) from error


def claim_for_handoff(
    controller: Path, entry: dict[str, Any], authorization: str,
) -> dict[str, Any]:
    claim = read(controller / f"claims/{entry['ticket']}.json")
    original = dict(claim)
    original.pop("handoff_sha256", None)
    original.pop("handoff_target_worktree", None)
    if not entry["source_lease_released"] and original.get("lease_released") is True:
        original.pop("lease_released")
    if claim.get("blocked_reason") == "preprovider-handoff":
        original["blocked_reason"] = "worker-error"
        original["worktree"] = entry["source_worktree"]
        if (
            claim.get("handoff_sha256") != authorization
            or claim.get("handoff_target_worktree") != entry["target_worktree"]
        ):
            raise EnvironmentError("pre-provider handoff claim changed")
    if hashlib.sha256(canonical(original)).hexdigest() != entry["claim_sha256"]:
        raise EnvironmentError("pre-provider source claim changed")
    if (
        original.get("schema") != "nysa.software-factory.controller-claim/v1"
        or original.get("ticket") != entry["ticket"]
        or original.get("branch") != entry["branch"]
        or original.get("status") != "blocked"
        or original.get("blocked_reason") != "worker-error"
        or original.get("receipt") != ""
        or original.get("role") != ""
        or original.get("publication_lease") != ""
        or original.get("parked") is not None
        or bool(original.get("lease_released") is True)
        != entry["source_lease_released"]
        or original.get("worktree") != entry["source_worktree"]
        or not re.fullmatch(r"[0-9a-f]{64}", original.get("lease", ""))
        or hashlib.sha256(original["lease"].encode()).hexdigest()
        != entry["lease_sha256"]
    ):
        raise EnvironmentError("pre-provider source claim is not recoverable")
    return claim


def validate_handoff_lease(
    source: dict[str, Any], entry: dict[str, Any], claim: dict[str, Any],
) -> Path:
    path = source["product"] / f"factory/.dispatch-leases/{entry['ticket']}.json"
    if not (path.exists() or path.is_symlink()):
        return path
    parent = path.parent
    info = parent.lstat()
    value = read(path)
    if (
        parent.is_symlink()
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_mode & 0o022
        or entry["source_lease_released"]
        or value.get("schema_version") != 1
        or value.get("ticket") != entry["ticket"]
        or value.get("lease_id") != claim.get("lease")
    ):
        raise EnvironmentError("pre-provider dispatcher lease changed")
    return path


def no_ticket_runtime(
    lane: dict[str, Any], ticket: str, *, allow_dispatch_lease: bool = False,
) -> None:
    controller = lane["controller"]
    product = lane["product"]
    absent = [
        controller / f"passports/{ticket}.json",
        controller / f"publication/queue/{ticket}.json",
    ]
    if not allow_dispatch_lease:
        absent.append(product / f"factory/.dispatch-leases/{ticket}.json")
    if any(path.exists() or path.is_symlink() for path in absent):
        raise EnvironmentError("pre-provider source has runtime or publication evidence")
    publication = controller / "publication/active.json"
    if publication.exists() or publication.is_symlink():
        if read(publication).get("ticket") == ticket:
            raise EnvironmentError("pre-provider source has publication evidence")
    runs = product / "factory/runs"
    if runs.is_dir():
        for path in runs.glob("*.meta"):
            if path.is_symlink() or not path.is_file():
                raise EnvironmentError("pre-provider run evidence is unsafe")
            fields = dict(
                line.split("=", 1)
                for line in path.read_text(encoding="utf-8").splitlines()
                if "=" in line
            )
            if fields.get("ticket") == ticket:
                raise EnvironmentError("pre-provider source has terminal run evidence")


def release_handoff_lease(
    source: dict[str, Any], entry: dict[str, Any], claim: dict[str, Any],
) -> dict[str, Any]:
    ticket = entry["ticket"]
    path = validate_handoff_lease(source, entry, claim)
    if path.exists() or path.is_symlink():
        parent = path.parent
        path.unlink()
        directory = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    if path.exists() or path.is_symlink():
        raise EnvironmentError("pre-provider dispatcher lease remains active")
    if claim.get("lease_released") is not True:
        claim["lease_released"] = True
        replace(source["controller"] / f"claims/{ticket}.json", claim)
    return claim


def validate_handoff_entry(
    source: dict[str, Any], target: dict[str, Any], entry: dict[str, Any],
    authorization: str,
) -> tuple[dict[str, Any], Path]:
    ticket = entry.get("ticket", "")
    if (
        set(entry) != {
            "branch", "claim_sha256", "head_sha", "head_tree", "lease_sha256",
            "route_plan_sha256", "source_worktree", "target_worktree", "ticket",
            "ticket_blob", "transition_receipt_sha256", "source_lease_released",
        }
        or not re.fullmatch(r"T-[0-9]+", ticket)
        or not isinstance(entry.get("source_lease_released"), bool)
        or entry.get("branch") != f"ticket/{ticket}"
        or any(
            not re.fullmatch(r"[0-9a-f]{64}", entry.get(key, ""))
            for key in (
                "claim_sha256", "lease_sha256", "route_plan_sha256",
                "transition_receipt_sha256",
            )
        )
        or any(
            not SHA.fullmatch(entry.get(key, ""))
            for key in ("head_sha", "head_tree", "ticket_blob")
        )
    ):
        raise EnvironmentError("pre-provider handoff entry is invalid")
    source_cell = Path(entry["source_worktree"])
    target_cell = Path(entry["target_worktree"])
    if (
        source_cell.parent
        != source["root"] / f"worktrees/{source['active']['project']}"
        or target_cell.parent
        != target["root"] / f"worktrees/{target['active']['project']}"
        or not re.fullmatch(r"cell-[1-6]", source_cell.name)
        or not re.fullmatch(r"cell-[1-6]", target_cell.name)
    ):
        raise EnvironmentError("pre-provider handoff cell is outside its trusted root")
    claim = claim_for_handoff(source["controller"], entry, authorization)
    receipt = transition_receipt(source["controller"] / f"{ticket}.json")
    route = None
    registrations = [
        item for item in worktree_records(source["product"])
        if item.get("branch") == f"refs/heads/{entry['branch']}"
    ]
    if len(registrations) != 1:
        raise EnvironmentError("pre-provider branch registration is ambiguous")
    registered = Path(registrations[0].get("worktree", ""))
    if registered not in {source_cell, target_cell}:
        raise EnvironmentError("pre-provider branch is outside the handoff cells")
    if registered.resolve(strict=True) != registered:
        raise EnvironmentError("pre-provider handoff cell is unsafe")
    safe_directory(registered)
    route = registered / f"factory/route-plans/{ticket}.json"
    route_info = route.lstat()
    if (
        route.is_symlink()
        or not stat.S_ISREG(route_info.st_mode)
        or route_info.st_uid != os.geteuid()
        or route_info.st_nlink != 1
        or route_info.st_mode & 0o022
    ):
        raise EnvironmentError("pre-provider route plan is unavailable")
    remote = command(
        "git", "-C", str(source["product"]), "ls-remote", "--heads", "origin",
        f"refs/heads/{entry['branch']}",
    ).split()
    if (
        command("git", "-C", str(registered), "status", "--porcelain=v1", "-z")
        or command("git", "-C", str(registered), "symbolic-ref", "--short", "HEAD")
        != entry["branch"]
        or command("git", "-C", str(registered), "rev-parse", "HEAD")
        != entry["head_sha"]
        or command("git", "-C", str(registered), "rev-parse", "HEAD^{tree}")
        != entry["head_tree"]
        or command(
            "git", "-C", str(registered), "rev-parse",
            f"HEAD:factory/tickets/{ticket}.md",
        ) != entry["ticket_blob"]
        or hashlib.sha256(route.read_bytes()).hexdigest()
        != entry["route_plan_sha256"]
        or remote != [entry["head_sha"], f"refs/heads/{entry['branch']}"]
        or receipt.get("ticket") != ticket
        or receipt.get("branch") != entry["branch"]
        or receipt.get("project") != source["active"]["project"]
        or receipt.get("factory_sha") != source["active"]["kit_sha"]
        or receipt.get("contract_version") not in SUPPORTED_CONTRACTS
        or receipt.get("stage") != "RUN planner"
        or receipt.get("role") != "planner"
        or receipt.get("loop") is not None
        or "parent_digest" in receipt
        or receipt.get("head_sha") != entry["head_sha"]
        or receipt.get("head_tree") != entry["head_tree"]
        or receipt.get("ticket_blob") != entry["ticket_blob"]
        or receipt.get("route_plan_sha256") != entry["route_plan_sha256"]
        or receipt.get("lease_sha256") != entry["lease_sha256"]
        or receipt.get("passport_sha256") is not None
        or receipt.get("product_origin_sha256")
        != hashlib.sha256(product_origin(source["product"]).encode()).hexdigest()
        or receipt.get("receipt_sha256") != entry["transition_receipt_sha256"]
    ):
        raise EnvironmentError("pre-provider source evidence changed")
    spec = importlib.util.spec_from_file_location(
        "qualification_handoff_dispatch",
        target["release"] / "scripts/dispatch-plan.py",
    )
    if not spec or not spec.loader:
        raise EnvironmentError("sealed pre-provider validator is unavailable")
    dispatch = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dispatch)
    protected = command(
        "git", "-C", str(target["product"]), "rev-parse",
        "refs/remotes/origin/main",
    )
    if subprocess.run(
        [
            "git", "-C", str(target["product"]), "merge-base", "--is-ancestor",
            protected, entry["head_sha"],
        ],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        timeout=120,
    ).returncode == 0:
        raise EnvironmentError("pre-provider branch does not require successor reset")
    try:
        dispatch.validate_preprovider_branch(
            target["product"], ticket, entry["branch"], protected,
            entry["head_sha"], entry["head_sha"],
        )
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise EnvironmentError(f"pre-provider reset is unusable: {error}") from error
    no_ticket_runtime(source, ticket, allow_dispatch_lease=True)
    validate_handoff_lease(source, entry, claim)
    target_controller = target["controller"]
    if any(
        path.exists() or path.is_symlink()
        for path in (
            target_controller / f"claims/{ticket}.json",
            target_controller / f"passports/{ticket}.json",
            target_controller / f"{ticket}.json",
        )
    ):
        raise EnvironmentError("successor already has ticket runtime state")
    no_ticket_runtime(target, ticket)
    return claim, registered


def build_handoff_journal(
    source: dict[str, Any], target: dict[str, Any], resets: dict[str, str],
) -> dict[str, Any]:
    source_worktrees = handoff_worktree_root(source, create=False)
    target_worktrees = handoff_worktree_root(target, create=True)
    occupied = {
        Path(item["worktree"])
        for item in worktree_records(target["product"])
        if item.get("worktree")
    }
    available = [
        target_worktrees / f"cell-{number}" for number in range(1, 7)
        if target_worktrees / f"cell-{number}" not in occupied
        and not (target_worktrees / f"cell-{number}").exists()
        and not (target_worktrees / f"cell-{number}").is_symlink()
    ]
    if len(available) < len(resets):
        raise EnvironmentError("successor has no trusted cells for pre-provider handoff")
    records = worktree_records(source["product"])
    entries = []
    for ticket, target_cell in zip(sorted(resets), available):
        branch = f"ticket/{ticket}"
        matching = [
            item for item in records
            if item.get("branch") == f"refs/heads/{branch}"
        ]
        if len(matching) != 1:
            raise EnvironmentError("pre-provider source branch is unavailable")
        source_cell = Path(matching[0].get("worktree", ""))
        if (
            source_cell.parent != source_worktrees
            or not re.fullmatch(r"cell-[1-6]", source_cell.name)
            or source_cell.resolve(strict=True) != source_cell
        ):
            raise EnvironmentError("pre-provider source branch is outside its trusted root")
        claim = read(source["controller"] / f"claims/{ticket}.json")
        receipt = transition_receipt(source["controller"] / f"{ticket}.json")
        route = source_cell / f"factory/route-plans/{ticket}.json"
        if not route.is_file() or route.is_symlink():
            raise EnvironmentError("pre-provider route plan is unavailable")
        entry = {
            "branch": branch,
            "claim_sha256": hashlib.sha256(canonical(claim)).hexdigest(),
            "head_sha": command("git", "-C", str(source_cell), "rev-parse", "HEAD"),
            "head_tree": command(
                "git", "-C", str(source_cell), "rev-parse", "HEAD^{tree}"
            ),
            "lease_sha256": hashlib.sha256(claim.get("lease", "").encode()).hexdigest(),
            "route_plan_sha256": hashlib.sha256(route.read_bytes()).hexdigest(),
            "source_lease_released": claim.get("lease_released") is True,
            "source_worktree": str(source_cell),
            "target_worktree": str(target_cell),
            "ticket": ticket,
            "ticket_blob": command(
                "git", "-C", str(source_cell), "rev-parse",
                f"HEAD:factory/tickets/{ticket}.md",
            ),
            "transition_receipt_sha256": receipt.get("receipt_sha256", ""),
        }
        entries.append(entry)
    value = {
        "entries": entries,
        "moved": [],
        "schema": PREPROVIDER_HANDOFF_SCHEMA,
        "source_factory_sha": source["active"]["kit_sha"],
        "source_project": source["active"]["project"],
        "source_receipt_id": source["active"]["receipt_id"],
        "source_root": str(source["root"]),
        "status": "prepared",
        "target_factory_sha": target["active"]["kit_sha"],
        "target_project": target["active"]["project"],
        "target_receipt_id": target["active"]["receipt_id"],
        "target_root": str(target["root"]),
    }
    value["authorization_sha256"] = hashlib.sha256(canonical({
        key: item for key, item in value.items() if key not in {"moved", "status"}
    })).hexdigest()
    return seal_journal(value)


def handoff_preprovider(args: argparse.Namespace) -> dict[str, Any]:
    source_project = args.preprovider_source_project
    if (
        not PROJECT.fullmatch(source_project)
        or source_project == args.project
        or args.upgrade or args.restore or args.takeover_project
    ):
        raise EnvironmentError("pre-provider handoff arguments are invalid")
    source = qualification_lane(args.preprovider_source_root, source_project)
    target = qualification_lane(args.root, args.project)
    factory = args.factory_root.resolve(strict=True)
    product = args.product_root.resolve(strict=True)
    sealed_helper = target["release"] / "scripts/qualification-environment.py"
    if (
        target["product"] != product
        or command(
            "git", "-C", str(factory), "status", "--porcelain",
            "--untracked-files=all",
        )
        or command("git", "-C", str(factory), "rev-parse", "HEAD")
        != target["active"]["kit_sha"]
        or command("git", "-C", str(factory), "rev-parse", "HEAD^{tree}")
        != target["active"]["kit_tree"]
        or command(
            "git", "-C", str(source["product"]), "rev-parse",
            "--path-format=absolute", "--git-common-dir",
        ) != command(
            "git", "-C", str(target["product"]), "rev-parse",
            "--path-format=absolute", "--git-common-dir",
        )
        or product_origin(source["product"]) != product_origin(target["product"])
        or not sealed_helper.is_file()
        or sealed_helper.is_symlink()
        or Path(__file__).resolve().read_bytes() != sealed_helper.read_bytes()
    ):
        raise EnvironmentError("pre-provider handoff repositories do not match")
    manifest = target["manifest"]
    if (
        manifest.get("mode") != "successor"
        or manifest.get("source_factory_sha") != source["active"]["kit_sha"]
        or source["manifest"].get("tickets") != manifest.get("tickets")
    ):
        raise EnvironmentError("pre-provider handoff requires the exact successor")
    tickets = manifest["tickets"]
    resets = preprovider_reset_authorizations(
        target["product"], target["active"]["kit_sha"], tickets
    )
    locks = lock_controllers(source["controller"], target["controller"])
    dispatch_locks: tuple[list[int], list[Path]] | None = None
    try:
        locked_source = qualification_lane(
            args.preprovider_source_root, source_project
        )
        locked_target = qualification_lane(args.root, args.project)
        if locked_source != source or locked_target != target:
            raise EnvironmentError("qualification activation changed before handoff lock")
        source, target = locked_source, locked_target
        resets = preprovider_reset_authorizations(
            target["product"], target["active"]["kit_sha"], tickets
        )
        dispatch_locks = lock_dispatch_boundaries(source, target)
        provider_drained(source)
        provider_drained(target)
        command(
            "git", "-C", str(target["product"]), "fetch", "--quiet", "origin",
            "+main:refs/remotes/origin/main",
        )
        journal_path = target["controller"] / "preprovider-handoff.json"
        new_journal = not (journal_path.exists() or journal_path.is_symlink())
        if not new_journal:
            journal = journal_value(read(journal_path))
        else:
            journal = build_handoff_journal(source, target, resets)
        expected_context = {
            "source_factory_sha": source["active"]["kit_sha"],
            "source_project": source_project,
            "source_receipt_id": source["active"]["receipt_id"],
            "source_root": str(source["root"]),
            "target_factory_sha": target["active"]["kit_sha"],
            "target_project": args.project,
            "target_receipt_id": target["active"]["receipt_id"],
            "target_root": str(target["root"]),
        }
        entries = journal.get("entries")
        moved = journal.get("moved")
        if (
            any(journal.get(key) != value for key, value in expected_context.items())
            or not isinstance(entries, list)
            or [item.get("ticket") for item in entries] != sorted(tickets)
            or not isinstance(moved, list)
            or moved != [item["ticket"] for item in entries[:len(moved)]]
            or journal.get("status") not in {"prepared", "completed"}
            or (journal.get("status") == "completed") != (len(moved) == len(entries))
            or any(resets.get(item.get("ticket")) != item.get("head_sha") for item in entries)
        ):
            raise EnvironmentError("pre-provider handoff journal conflicts")
        authorization = journal["authorization_sha256"]
        claims = []
        for entry in entries:
            claim, registered = validate_handoff_entry(
                source, target, entry, authorization
            )
            claims.append((entry, claim, registered))
        moved_count = len(journal["moved"])
        for index, (entry, _, registered) in enumerate(claims):
            expected_source = Path(entry["source_worktree"])
            expected_target = Path(entry["target_worktree"])
            if (
                index < moved_count and registered != expected_target
                or index == moved_count and registered not in {
                    expected_source, expected_target,
                }
                or index > moved_count and registered != expected_source
            ):
                raise EnvironmentError("pre-provider handoff physical state conflicts")
        if new_journal:
            write(journal_path, journal)
        claims = [
            (entry, release_handoff_lease(source, entry, claim))
            for entry, claim, _ in claims
        ]
        for entry, _ in claims:
            no_ticket_runtime(source, entry["ticket"])
        for entry, claim in claims:
            if claim.get("blocked_reason") != "preprovider-handoff":
                claim.update({
                    "blocked_reason": "preprovider-handoff",
                    "handoff_sha256": authorization,
                    "handoff_target_worktree": entry["target_worktree"],
                    "status": "blocked",
                })
                replace(source["controller"] / f"claims/{entry['ticket']}.json", claim)
        for index, entry in enumerate(entries):
            claim, registered = validate_handoff_entry(
                source, target, entry, authorization
            )
            source_cell = Path(entry["source_worktree"])
            target_cell = Path(entry["target_worktree"])
            if registered == source_cell:
                if target_cell.exists() or target_cell.is_symlink():
                    raise EnvironmentError("pre-provider handoff destination is occupied")
                command(
                    "git", "-C", str(target["product"]), "worktree", "move",
                    str(source_cell), str(target_cell),
                )
                target_cell.chmod(0o700)
            elif source_cell.exists() or source_cell.is_symlink():
                raise EnvironmentError("pre-provider handoff source still exists")
            claim["worktree"] = str(target_cell)
            replace(source["controller"] / f"claims/{entry['ticket']}.json", claim)
            if index >= len(journal["moved"]):
                journal["moved"].append(entry["ticket"])
                journal = seal_journal(journal)
                replace(journal_path, journal)
        if journal["status"] != "completed":
            journal["status"] = "completed"
            journal = seal_journal(journal)
            replace(journal_path, journal)
        return {
            "factory_sha": target["active"]["kit_sha"],
            "handoff_sha256": authorization,
            "project": args.project,
            "schema": SCHEMA,
            "source_project": source_project,
            "status": "preprovider-handed-off",
            "tickets": tickets,
        }
    finally:
        if dispatch_locks is not None:
            unlock_dispatch_boundaries(*dispatch_locks)
        for descriptor in locks:
            os.close(descriptor)


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


def ensure_directory(path: Path) -> Path:
    if path.exists() or path.is_symlink():
        return safe_directory(path)
    return safe_directory(path, create=True)


def ensure_release(factory: Path, sha: str, tree: str, releases: Path) -> Path:
    release = releases / sha
    temporary = releases / f".{sha}.partial"
    if temporary.exists() or temporary.is_symlink():
        raise EnvironmentError("partial qualification release is torn")
    if release.exists() or release.is_symlink():
        sealed_directory(release)
    else:
        materialize(factory, sha, temporary)
        temporary.chmod(0o700)
        os.rename(temporary, release)
        release.chmod(0o555)
        descriptor = os.open(releases, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    if git_tree(release) != tree:
        raise EnvironmentError("sealed qualification tree does not match the candidate")
    return release


def validate_prepare_root(root: Path, sha: str, project: str) -> None:
    allowed = {
        "environment.json", "global.env", "marker.json", "projects",
        "project-runtimes", "receipts", "releases",
    }
    if any(path.name not in allowed for path in root.iterdir()):
        raise EnvironmentError("partial qualification environment is invalid")
    releases = root / "releases"
    if releases.exists() or releases.is_symlink():
        safe_directory(releases)
        if any(path.name != sha for path in releases.iterdir()):
            raise EnvironmentError("partial qualification release is invalid")
    projects = root / "projects"
    if projects.exists() or projects.is_symlink():
        safe_directory(projects)
        if any(path.name != project for path in projects.iterdir()):
            raise EnvironmentError("partial qualification project is invalid")
        project_root = projects / project
        if project_root.exists() or project_root.is_symlink():
            safe_directory(project_root)
            allowed_project = {"active.json", f"model-bundle-{sha}.json"}
            if any(path.name not in allowed_project for path in project_root.iterdir()):
                raise EnvironmentError("partial qualification activation is invalid")
    runtimes = root / "project-runtimes"
    if runtimes.exists() or runtimes.is_symlink():
        safe_directory(runtimes)
        if any(path.name != project for path in runtimes.iterdir()):
            raise EnvironmentError("partial qualification runtime is invalid")
def validate_authority_prepare_shape(
    authority: Path, selected: list[str],
) -> None:
    allowed = {
        "authority.json", "controller", "operator", "operator-bootstrap.json",
        "provider",
    }
    if any(path.name not in allowed for path in authority.iterdir()):
        raise EnvironmentError("partial qualification authority is invalid")
    controller = authority / "controller"
    advanced = any(
        (authority / name).exists() or (authority / name).is_symlink()
        for name in ("authority.json", "provider")
    )
    if advanced and not controller.exists():
        raise EnvironmentError("qualification preparation state is torn")
    if controller.exists() or controller.is_symlink():
        safe_directory(controller)
        entries = {path.name: path for path in controller.iterdir()}
        if set(entries) - {
            ".operator-apply-lock", ".operator-lock", "operator-receipts",
        }:
            raise EnvironmentError("qualification controller is active")
        for name in (".operator-apply-lock", ".operator-lock"):
            path = entries.get(name)
            if path is None:
                continue
            info = path.lstat()
            if (
                path.is_symlink() or not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid() or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size
            ):
                raise EnvironmentError("qualification controller is active")
        receipts = entries.get("operator-receipts")
        if receipts is not None:
            safe_directory(receipts)
            mapping = read(authority / "operator/operator-map.json")
            for ticket_dir in receipts.iterdir():
                if ticket_dir.name not in selected:
                    raise EnvironmentError("qualification controller is active")
                safe_directory(ticket_dir)
                paths = list(ticket_dir.iterdir())
                if len(paths) != 1 or paths[0].name != "ready-1.json":
                    raise EnvironmentError("qualification controller is active")
                try:
                    receipt = operator_receipt.safe_receipt(paths[0])
                except (OSError, ValueError) as error:
                    raise EnvironmentError(
                        "qualification controller is active"
                    ) from error
                if (
                    receipt.get("ticket") != ticket_dir.name
                    or receipt.get("action") != "ready"
                    or receipt.get("sequence") != 1
                    or receipt.get("payload") != {}
                    or not (
                        receipt.get("consumed") is True
                        and isinstance(receipt.get("consumed_at_epoch"), int)
                        or receipt.get("consumed") is False
                        and "consumed_at_epoch" not in receipt
                        and mapping["tickets"].get(ticket_dir.name, {}).get(
                            "operator"
                        ) == {
                            "observed_at": receipt.get("issued_at"),
                            "receipt_sha256": receipt.get("receipt_sha256"),
                            "state": "Ready",
                            "state_base": "backlog",
                        }
                    )
                ):
                    raise EnvironmentError("qualification controller is active")


def validate_prepare_phase(
    root: Path, authority: Path | None, sha: str, project: str,
) -> None:
    provider = bool(
        authority and (
            (authority / "provider").exists()
            or (authority / "provider").is_symlink()
        )
    )
    if not (root.exists() or root.is_symlink()):
        authority_state = bool(
            authority and (
                (authority / "authority.json").exists()
                or (authority / "authority.json").is_symlink()
            )
        )
        if provider or authority_state:
            raise EnvironmentError("qualification preparation state is torn")
        return
    if authority is not None and any(
        path.name != "project-runtimes" for path in root.iterdir()
    ) and not (
        (authority / "controller").exists()
        or (authority / "controller").is_symlink()
    ):
        raise EnvironmentError("qualification preparation state is torn")
    structural = (
        root / "releases", root / "projects", root / "receipts",
        root / f"projects/{project}",
    )
    phases = structural + (
        root / "global.env", root / "marker.json", root / f"releases/{sha}",
    )
    present = [path.exists() or path.is_symlink() for path in phases]
    if present != sorted(present, reverse=True):
        raise EnvironmentError("qualification preparation state is torn")
    release = root / f"releases/{sha}"
    released = release.exists() or release.is_symlink()
    downstream = bool(
        authority and (
            (authority / "authority.json").exists()
            or (authority / "authority.json").is_symlink()
        )
    ) or any(
        path.exists() or path.is_symlink() for path in (
            root / "environment.json", root / f"projects/{project}/active.json",
        )
    ) or bool((root / "receipts").is_dir() and any((root / "receipts").iterdir()))
    if provider and not released or downstream and (
        not released or authority is not None and not provider
    ):
        raise EnvironmentError("qualification preparation state is torn")
    if downstream and authority is not None and any(
        not path.exists() for path in (
            authority / "provider/accounting/state-v2.sqlite3",
            authority / "provider/cli-runtimes",
            authority / "provider/provider-activation.json",
            authority / "provider/provider-apply-locks",
            authority / "provider/provider-attempts",
            authority / "provider/provider-configuration.lock",
            authority / "provider/provider-policy.json",
        )
    ):
        raise EnvironmentError("qualification preparation state is torn")


def validate_existing_publication_prefix(
    root: Path, authority: Path | None, project: str,
) -> None:
    if not (root.exists() or root.is_symlink()):
        return
    receipt_paths: list[Path] = []
    receipts = root / "receipts"
    if receipts.exists() or receipts.is_symlink():
        safe_directory(receipts)
        receipt_paths = list(receipts.iterdir())
        if (
            len(receipt_paths) > 1
            or receipt_paths
            and not re.fullmatch(r"[0-9a-f]{64}[.]json", receipt_paths[0].name)
        ):
            raise EnvironmentError("qualification preparation receipt is unexpected")
    paths: list[Path | None] = [receipt_paths[0] if receipt_paths else None]
    if authority is not None:
        paths.append(authority / "authority.json")
    paths.extend((
        root / "environment.json",
        root / f"projects/{project}/active.json",
    ))
    present = [
        bool(path and (path.exists() or path.is_symlink())) for path in paths
    ]
    if present != sorted(present, reverse=True):
        raise EnvironmentError("qualification preparation state is torn")
    for path in paths:
        if path and (path.exists() or path.is_symlink()):
            value = read(path)
            if config_bytes(path) != canonical(value):
                raise EnvironmentError("qualification preparation artifact changed")


def validate_publication_prefix(
    receipt: Path, authority_state: Path | None, active: Path,
    environment: Path,
) -> None:
    paths = [receipt]
    if authority_state is not None:
        paths.append(authority_state)
    paths.extend((environment, active))
    present = [path.exists() or path.is_symlink() for path in paths]
    if present != sorted(present, reverse=True):
        raise EnvironmentError("qualification preparation state is torn")
    receipts = receipt.parent
    if any(path != receipt for path in receipts.iterdir()):
        raise EnvironmentError("qualification preparation receipt is unexpected")


def register_human_target(
    release: Path, launcher: Path, project: str, root: Path,
) -> None:
    cli = release / "scripts/factory-cli.py"
    if not cli.exists():
        return
    target = f"qualification-{hashlib.sha256(str(root).encode()).hexdigest()[:24]}"
    result = subprocess.run(
        ["/usr/bin/python3", "-I", "-S", str(cli), "register",
         target, str(launcher), project],
        text=True, capture_output=True, check=False, timeout=120,
        env={
            "FACTORY_INTERNAL_REGISTER": "1",
            "HOME": pwd.getpwuid(os.getuid()).pw_dir,
            "PATH": "/usr/bin:/bin",
        },
    )
    if result.returncode:
        raise EnvironmentError("qualification human target registration failed")


def prime_qualification(release: Path, project: str) -> None:
    result = subprocess.run(
        [
            str(release / "scripts/factory-launch"), project,
            "qualification-prime", "--json",
        ],
        text=True, capture_output=True, check=False,
    )
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise EnvironmentError(
            "qualification Planner prime returned invalid evidence"
        ) from error
    if (
        isinstance(value, dict)
        and value.get("error") == "qualification prime has execution residue"
    ):
        raise EnvironmentError("qualification controller is active")
    if (
        result.returncode
        or not isinstance(value, dict)
        or value.get("schema") != "nysa.software-factory.controller/v1"
        or value.get("status") != "restart_required"
        or value.get("results") != []
    ):
        raise EnvironmentError("qualification Planner prime failed")


def activation_events(
    controller: Path, manifest: dict[str, Any], factory_sha: str,
) -> tuple[Path, str, int, list[dict[str, Any]]]:
    events = safe_directory(controller / "events")
    manifest_sha256 = hashlib.sha256(canonical(manifest)[:-1]).hexdigest()
    generation = manifest.get("generation")
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
        raise EnvironmentError("qualification activation timing boundary is invalid")
    selected = []
    for path in events.glob("*.json"):
        value = read(path)
        unsigned = dict(value)
        digest = unsigned.pop("event_sha256", "")
        if digest != hashlib.sha256(canonical(unsigned)[:-1]).hexdigest():
            raise EnvironmentError("qualification controller event is invalid")
        if (
            value.get("factory_sha") == factory_sha
            and value.get("qualification_generation") == generation
            and value.get("qualification_manifest_sha256") == manifest_sha256
            and value.get("event") in {"activation_complete", "restart_boundary"}
        ):
            selected.append(value)
    return events, manifest_sha256, generation, selected


def record_activation_completion(
    controller: Path, manifest: dict[str, Any], receipt: dict[str, Any],
    factory_tree: str, product_sha: str, product_tree: str,
) -> None:
    events, manifest_sha256, generation, selected = activation_events(
        controller, manifest, receipt["kit_sha"],
    )
    boundaries = [item for item in selected if item["event"] == "restart_boundary"]
    completions = [item for item in selected if item["event"] == "activation_complete"]
    if (
        len(boundaries) != 1 or len(completions) > 1
        or boundaries[0].get("tickets") != sorted(manifest["tickets"])
    ):
        raise EnvironmentError("qualification activation timing boundary is invalid")
    boundary = boundaries[0]
    started = receipt.get("activation_started_epoch_ns")
    if not isinstance(started, int) or isinstance(started, bool) or started < 1:
        raise EnvironmentError("qualification activation timing boundary is invalid")
    if completions:
        completion = completions[0]
        expected = {
            "activation_receipt_id": receipt["receipt_id"],
            "activation_started_epoch_ns": started,
            "event": "activation_complete",
            "event_sha256": completion.get("event_sha256"),
            "factory_sha": receipt["kit_sha"],
            "factory_tree": factory_tree,
            "observed_at_epoch_ns": completion.get("observed_at_epoch_ns"),
            "product_sha": product_sha,
            "product_tree": product_tree,
            "qualification_generation": generation,
            "qualification_manifest_sha256": manifest_sha256,
            "restart_boundary_event_sha256": boundary["event_sha256"],
            "schema": CONTROLLER_EVENT_SCHEMA,
            "ticket": None,
        }
        if completion != expected or completion["observed_at_epoch_ns"] < started:
            raise EnvironmentError("qualification activation timing boundary changed")
        return
    completed = max(time.time_ns(), boundary["observed_at_epoch_ns"] + 1)
    if completed < started:
        raise EnvironmentError("qualification activation timing clock moved backwards")
    value = {
        "activation_receipt_id": receipt["receipt_id"],
        "activation_started_epoch_ns": started,
        "event": "activation_complete",
        "factory_sha": receipt["kit_sha"],
        "factory_tree": factory_tree,
        "observed_at_epoch_ns": completed,
        "product_sha": product_sha,
        "product_tree": product_tree,
        "qualification_generation": generation,
        "qualification_manifest_sha256": manifest_sha256,
        "restart_boundary_event_sha256": boundary["event_sha256"],
        "schema": CONTROLLER_EVENT_SCHEMA,
        "ticket": None,
    }
    value["event_sha256"] = hashlib.sha256(canonical(value)[:-1]).hexdigest()
    replace(events / f"{completed}-{os.urandom(8).hex()}.json", value)


def _prepare(args: argparse.Namespace, started_epoch_ns: int) -> dict[str, Any]:
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
    sha = command("git", "-C", str(factory), "rev-parse", "HEAD")
    tree = command("git", "-C", str(factory), "rev-parse", "HEAD^{tree}")
    if not SHA.fullmatch(sha) or not SHA.fullmatch(tree):
        raise EnvironmentError("Factory candidate identity is invalid")
    if (product / "factory/KIT_PIN").read_text(encoding="utf-8") != sha + "\n":
        raise EnvironmentError("qualification product is not pinned to the candidate")
    contract = json.loads(
        (factory / "factory-contract.json").read_text(encoding="utf-8")
    ).get("contract_version")
    if contract not in SUPPORTED_CONTRACTS:
        raise EnvironmentError("qualification requires a supported Factory Contract")
    manifest = qualification_manifest(product, sha)
    validate_provider_cli_pins(factory, sha)
    capacity = manifest["capacity"]
    origin = product_origin(product)
    qualification_publication_origin(product, origin)
    restoring = bool(getattr(args, "restore", False))
    takeover_requested = getattr(args, "takeover_project", None) is not None
    global_config = b""
    if not restoring:
        global_config = prepare_global_config(args, root)
        if not takeover_requested:
            validate_qualification_budget(factory, product, manifest, global_config)
    expected_authority_path = Path.home().resolve(strict=True) / (
        f".factory/qualification/{args.project}"
    )
    active_path = root / f"projects/{args.project}/active.json"
    if (
        not restoring and not takeover_requested
        and (active_path.exists() or active_path.is_symlink())
    ):
        validate_existing_publication_prefix(
            root, expected_authority_path, args.project,
        )
        lane = qualification_lane(root, args.project)
        if (
            lane["active"].get("kit_sha") != sha
            or lane["active"].get("kit_tree") != tree
            or lane["active"].get("product_sha")
            != command("git", "-C", str(product), "rev-parse", "HEAD")
        ):
            raise EnvironmentError("existing qualification activation changed")
        environment = read(root / "environment.json")
        expected_environment = bind_runtime_tuple({
            "factory_sha": sha,
            "factory_tree": tree,
            "authority_root": str(lane["authority"]),
            "historical_pr_objects": historical_pr_objects(product, origin),
            "launcher": str(lane["release"] / "scripts/factory-launch"),
            "product_sha": lane["active"]["product_sha"],
            "product_tree": lane["active"]["product_tree"],
            "project": args.project,
            "provider_policy_sha256": lane["active"]["provider_policy_sha256"],
            "qualification_mode": "isolated",
            "root": str(root),
            "schema": SCHEMA,
            "status": "prepared",
        }, lane["active"].get("runtime_tuple"))
        if environment != expected_environment:
            raise EnvironmentError("qualification preparation artifact changed")
        qualification_fallback_readiness(
            lane["release"], root, args.project, product,
            bundle_path=(
                root / f"projects/{args.project}/model-bundle-{sha}.json"
            ),
        )
        try:
            validate_provider(
                lane["release"], lane["authority"], capacity, contract,
                pristine=True,
            )
        except EnvironmentError as error:
            if str(error) != "durable qualification provider policy changed":
                raise
            raise EnvironmentError(
                "qualification preparation artifact changed"
            ) from error
        prime_qualification(lane["release"], args.project)
        record_activation_completion(
            lane["controller"], manifest, lane["receipt"], tree,
            lane["active"]["product_sha"], lane["active"]["product_tree"],
        )
        return environment
    historical_objects = historical_pr_objects(product, origin)
    if not (expected_authority_path / "operator-bootstrap.json").exists():
        validate_selected_remote_branches(
            factory, product, manifest, origin,
            exact_authorizations=not (
                root / f"projects/{args.project}/active.json"
            ).exists(),
        )
    validate_selected_contracts(product, manifest)
    prepare_product_runtime(product)
    if command("git", "-C", str(product), "status", "--porcelain", "--untracked-files=all"):
        raise EnvironmentError("qualification product runtime contract is not ignored")
    takeover = takeover_source(
        factory, product, args.project, getattr(args, "takeover_project", None)
    )
    if restoring and takeover:
        raise EnvironmentError("takeover qualification cannot restore isolated authority")
    bootstrap_exists = bool(not takeover and (
        (expected_authority_path / "operator-bootstrap.json").exists()
        or (expected_authority_path / "operator-bootstrap.json").is_symlink()
    ))
    seed = (
        operator_seed(args)
        if not takeover and not restoring and not bootstrap_exists else None
    )
    state = (
        "restore" if restoring
        else preparation_state(
            root, None if takeover else expected_authority_path,
            args.project,
        )
    )
    product_tree = command("git", "-C", str(product), "rev-parse", "HEAD^{tree}")
    product_sha = command("git", "-C", str(product), "rev-parse", "HEAD")
    runtime_bin = None
    if (product / "factory/certification-plan.json").exists():
        safe_directory(root, create=not root.exists())
        runtime_bin = prepare_qualification_runtime(
            factory, product, root, args.project,
            getattr(args, "runtime_bin", None),
        )
    runtime_tuple = certification_preflight(
        factory, product, sha, tree, contract, runtime_bin,
    )
    authority: Path | None = None
    controller_state_path = ""
    provider_state_path = ""
    operator_map_path = ""
    runtime_ledger_path = ""
    if takeover:
        operator_map_path = takeover["operator_map_path"]
    else:
        qualification = Path.home().resolve(strict=True) / ".factory/qualification"
        operator_map_path = str(qualification / args.project / "operator/operator-map.json")
        runtime_ledger_path = str(qualification / args.project / "operator/runtime-ledger.csv")
    identity = authority_identity(
        args.project, contract, sha, tree, product, product_sha, product_tree, origin,
        runtime_tuple, operator_map_path, runtime_ledger_path,
    )
    expected_authority = None if takeover else expected_authority_path
    map_path: Path | None = None
    ledger_path: Path | None = None
    if not takeover:
        if restoring or bootstrap_exists:
            authority = authority_root(args.project)
            map_path, ledger_path = resume_operator_state(
                authority, identity, manifest["tickets"],
            )
        else:
            if root.exists() and any(
                path.name != "project-runtimes" for path in root.iterdir()
            ):
                raise EnvironmentError("qualification preparation state is torn")
            if state != "fresh" and not expected_authority.exists():
                raise EnvironmentError("partial qualification authority is missing")
            if seed is None:
                raise EnvironmentError("qualification operator seed is unavailable")
            authority = partial_authority_root(args.project)
            map_path, ledger_path = prepare_operator_state(
                authority, identity, manifest["tickets"], seed,
            )
        if restoring:
            initialize_selected_operator(
                factory, product, map_path, ledger_path,
                authority / "controller", refresh=True,
            )
            validate_runtime_ledger(ledger_path)
            if command(
                "git", "-C", str(product), "status", "--porcelain",
                "--untracked-files=all",
            ):
                raise EnvironmentError(
                    "qualification operator initialization dirtied product"
                )
            controller = authority / "controller"
            durable_identity = read(authority / "authority.json")
            legacy_identity = authority_identity(
                args.project, "1.8.0", sha, tree, product, product_sha,
                product_tree, origin, runtime_tuple, operator_map_path,
                runtime_ledger_path,
            )
            if durable_identity != identity and (
                contract != "1.9.0" or durable_identity != legacy_identity
            ):
                raise EnvironmentError("durable qualification authority changed")
            safe_directory(controller)
            validate_paused_authority(factory, product, controller, identity)
            controller_state_path = str(controller)
            provider_state_path = str(authority / "provider")

    if not restoring:
        marker = {"mode": "qualification", "schema": SCHEMA}
        if authority is not None:
            validate_authority_prepare_shape(authority, manifest["tickets"])
            validate_selected_remote_branches(
                factory, product, manifest, origin,
                exact_authorizations=not (
                    root / f"projects/{args.project}/active.json"
                ).exists(),
                mapping_path=map_path,
                controller=authority / "controller",
            )
        validate_prepare_phase(root, authority, sha, args.project)
        if root.exists() or root.is_symlink():
            validate_prepare_root(root, sha, args.project)
            marker_path = root / "marker.json"
            if (
                marker_path.exists() or marker_path.is_symlink()
            ) and config_bytes(marker_path) != canonical(marker):
                raise EnvironmentError("qualification preparation artifact changed")
            release = root / f"releases/{sha}"
            if release.exists() or release.is_symlink():
                sealed_directory(release)
                if git_tree(release) != tree:
                    raise EnvironmentError(
                        "sealed qualification tree does not match the candidate"
                    )
            validate_existing_publication_prefix(root, authority, args.project)
        if authority is not None:
            validate_prepare_provider(
                root / f"releases/{sha}"
                if (root / f"releases/{sha}").exists() else factory,
                authority, capacity,
            )
        if not takeover:
            mapping = read(map_path)
            advanced = (
                root.exists() and any(
                    path.name != "project-runtimes" for path in root.iterdir()
                )
            ) or any(
                (authority / name).exists() or (authority / name).is_symlink()
                for name in ("authority.json", "provider")
            )
            if advanced and not all(
                selected_operator_ready(mapping, ticket)
                for ticket in manifest["tickets"]
            ):
                raise EnvironmentError("qualification preparation state is torn")
            if state != "exact-complete":
                initialize_selected_operator(
                    factory, product, map_path, ledger_path,
                    authority / "controller",
                )
            validate_authority_prepare_shape(authority, manifest["tickets"])
            validate_selected_remote_branches(
                factory, product, manifest, origin,
                exact_authorizations=not (
                    root / f"projects/{args.project}/active.json"
                ).exists(),
                mapping_path=map_path,
                controller=authority / "controller",
            )
            validate_runtime_ledger(ledger_path)
            if command(
                "git", "-C", str(product), "status", "--porcelain",
                "--untracked-files=all",
            ):
                raise EnvironmentError(
                    "qualification operator initialization dirtied product"
                )
            controller = ensure_directory(authority / "controller")
            controller_state_path = str(controller)
            provider_state_path = str(authority / "provider")

    safe_directory(root, create=not root.exists())
    releases = root / "releases"
    projects = root / "projects"
    receipts = root / "receipts"
    for path in (releases, projects, receipts):
        ensure_directory(path)
    project = projects / args.project
    ensure_directory(project)
    if restoring:
        snapshot_global_config(args, root)
    else:
        write_bytes_exact(root / "global.env", global_config)
    active = project / "active.json"
    marker = {"mode": "qualification", "schema": SCHEMA}
    if restoring:
        write_exact(root / "marker.json", marker)
        release = ensure_release(factory, sha, tree, releases)
    else:
        write_exact(root / "marker.json", marker)
        release = ensure_release(factory, sha, tree, releases)
    model_bundle_path = project / f"model-bundle-{sha}.json"
    fallback_readiness, fallback_readiness_sha256 = qualification_fallback_readiness(
        release, root, args.project, product, bundle_path=model_bundle_path,
    )
    model_bundle_sha256 = read(model_bundle_path)["bundle_sha256"]
    provider_policy_sha256 = (
        takeover["provider_policy_sha256"]
        if takeover else (
            validate_provider(release, authority, capacity, contract)
            if restoring else
            prepare_provider(release, authority, capacity, contract)
        )
    )
    qualification_mode = takeover["mode"] if takeover else "isolated"

    if restoring and qualification_mode == "isolated":
        _events, _digest, _generation, timing = activation_events(
            Path(controller_state_path), manifest, sha,
        )
        completions = [item for item in timing if item["event"] == "activation_complete"]
        if len(completions) != 1:
            raise EnvironmentError("qualification activation timing boundary is invalid")
        started_epoch_ns = completions[0].get("activation_started_epoch_ns")

    prior_receipts = list(receipts.iterdir())
    if prior_receipts:
        if (
            len(prior_receipts) != 1
            or not re.fullmatch(r"[0-9a-f]{64}[.]json", prior_receipts[0].name)
        ):
            raise EnvironmentError("qualification activation receipt is invalid")
        prior_receipt_id = prior_receipts[0].stem
        prior_receipt = read(prior_receipts[0])
        unsigned = dict(prior_receipt)
        if (
            unsigned.pop("receipt_id", "") != prior_receipt_id
            or hashlib.sha256(canonical(unsigned)).hexdigest() != prior_receipt_id
        ):
            raise EnvironmentError("qualification activation receipt is invalid")
        started_epoch_ns = prior_receipt.get("activation_started_epoch_ns")
    if (
        not isinstance(started_epoch_ns, int) or isinstance(started_epoch_ns, bool)
        or started_epoch_ns < 1
    ):
        raise EnvironmentError("qualification activation start is invalid")
    receipt_value = bind_runtime_tuple({
        "activation_started_epoch_ns": started_epoch_ns,
        "contract_version": contract,
        "kit_sha": sha,
        "kit_tree": tree,
        "product_origin": origin,
        "product_path": str(product),
        "product_sha": product_sha,
        "product_tree": product_tree,
        "project": args.project,
        "provider_policy_sha256": provider_policy_sha256,
        "fallback_readiness": fallback_readiness,
        "fallback_readiness_sha256": fallback_readiness_sha256,
        "model_bundle_sha256": model_bundle_sha256,
        "qualification_mode": qualification_mode,
        "operator_map_path": operator_map_path,
        "status": "pass",
    }, runtime_tuple)
    if runtime_ledger_path:
        receipt_value["runtime_ledger_path"] = runtime_ledger_path
    if takeover:
        receipt_value["takeover_kits_root"] = takeover["takeover_kits_root"]
    else:
        receipt_value["controller_state_path"] = controller_state_path
        receipt_value["provider_state_path"] = provider_state_path
    receipt_id = hashlib.sha256(canonical(receipt_value)).hexdigest()
    receipt_value["receipt_id"] = receipt_id
    receipt_path = receipts / f"{receipt_id}.json"
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
        "fallback_readiness_sha256": fallback_readiness_sha256,
        "model_bundle_sha256": model_bundle_sha256,
        "qualification_mode": qualification_mode,
        "operator_map_path": operator_map_path,
        "receipt_id": receipt_id,
        "release_path": str(release),
    }, runtime_tuple)
    if runtime_ledger_path:
        active_value["runtime_ledger_path"] = runtime_ledger_path
    if takeover:
        active_value["takeover_kits_root"] = takeover["takeover_kits_root"]
    else:
        active_value["controller_state_path"] = controller_state_path
        active_value["provider_state_path"] = provider_state_path
    result = bind_runtime_tuple({
        "factory_sha": sha,
        "factory_tree": tree,
        "authority_root": str(authority) if authority else None,
        "historical_pr_objects": historical_objects,
        "launcher": str(release / "scripts/factory-launch"),
        "product_sha": product_sha,
        "product_tree": product_tree,
        "project": args.project,
        "provider_policy_sha256": provider_policy_sha256,
        "qualification_mode": qualification_mode,
        "root": str(root),
        "schema": SCHEMA,
        "status": "restored" if restoring else "prepared",
    }, runtime_tuple)
    environment = root / "environment.json"
    if restoring:
        validate_publication_prefix(receipt_path, None, active, environment)
        write_exact(receipt_path, receipt_value)
        write_exact(environment, result)
        write_exact(active, active_value)
    else:
        authority_state = authority / "authority.json" if authority else None
        validate_publication_prefix(
            receipt_path, authority_state, active, environment,
        )
        write_exact(receipt_path, receipt_value)
        if authority_state is not None:
            write_exact(authority_state, identity)
        write_exact(environment, result)
        write_exact(active, active_value)
    if qualification_mode == "isolated":
        register_human_target(
            release, release / "scripts/factory-launch", args.project, root,
        )
        prime_qualification(release, args.project)
        record_activation_completion(
            Path(controller_state_path), manifest, receipt_value, tree,
            product_sha, product_tree,
        )
    return result


def prepare(args: argparse.Namespace) -> dict[str, Any]:
    observed_epoch_ns = time.time_ns()
    descriptor = lock_preparation(args.project)
    try:
        start_path, started_epoch_ns = activation_start(args, observed_epoch_ns)
        result = _prepare(args, started_epoch_ns)
        remove_activation_start(start_path)
        return result
    finally:
        os.close(descriptor)


def validate_upgrade_source(
    root: Path, project: str, factory: Path, product: Path, sha: str,
    contract: str, manifest: dict[str, Any],
) -> dict[str, Any]:
    active_path = root / f"projects/{project}/active.json"
    active = read(active_path)
    marker = read(root / "marker.json")
    qualification_mode = active.get("qualification_mode")
    active_contract = active.get("contract_version")
    if (
        marker != {"mode": "qualification", "schema": SCHEMA}
        or active.get("project") != project
        or active.get("product_path") != str(product)
        or active_contract not in SUPPORTED_CONTRACTS
        or (
            active_contract != contract
            and (active_contract, contract) not in SUPPORTED_UPGRADES
        )
        or not SHA.fullmatch(active.get("kit_sha", ""))
        or not isinstance(active.get("generation"), int)
        or isinstance(active.get("generation"), bool)
        or active["generation"] < 1
        or qualification_mode != "isolated"
    ):
        raise EnvironmentError("existing qualification activation is invalid")
    previous_receipt_id = active.get("receipt_id", "")
    if not re.fullmatch(r"[0-9a-f]{64}", previous_receipt_id):
        raise EnvironmentError("existing qualification activation is invalid")
    previous_receipt = read(root / f"receipts/{previous_receipt_id}.json")
    unsigned_receipt = dict(previous_receipt)
    if (
        unsigned_receipt.pop("receipt_id", "") != previous_receipt_id
        or hashlib.sha256(canonical(unsigned_receipt)).hexdigest()
        != previous_receipt_id
        or previous_receipt.get("status") != "pass"
        or any(
            previous_receipt.get(key) != active.get(key)
            for key in (
                "contract_version", "controller_state_path",
                "fallback_readiness_sha256", "model_bundle_sha256",
                "kit_sha", "kit_tree",
                "operator_map_path", "product_path", "product_tree", "project",
                "provider_policy_sha256", "provider_state_path",
                "qualification_mode", "runtime_ledger_path",
            )
        )
    ):
        raise EnvironmentError("existing qualification activation is invalid")
    authority = authority_root(project)
    controller = safe_directory(Path(active.get("controller_state_path", "")))
    provider = safe_directory(Path(active.get("provider_state_path", "")))
    operator_map_path = Path(str(active.get("operator_map_path", "")))
    runtime_ledger_path = Path(str(active.get("runtime_ledger_path", "")))
    if (
        controller != authority / "controller"
        or provider != authority / "provider"
        or operator_map_path != authority / "operator/operator-map.json"
        or runtime_ledger_path != authority / "operator/runtime-ledger.csv"
    ):
        raise EnvironmentError("durable qualification authority path changed")
    validate_successor_upgrade_cohort(
        factory, product, controller, project, active["kit_sha"],
        active.get("product_sha", ""), manifest,
    )
    validate_operator_map(read(operator_map_path))
    validate_runtime_ledger(runtime_ledger_path)
    if any((product / "factory/.active-runs").glob("*")) or any(
        (product / "factory/runs").glob("*.pid")
    ):
        raise EnvironmentError("qualification has an active provider run")
    validate_provider(
        Path(active["release_path"]), authority, manifest["capacity"], active_contract,
    )
    return {
        "active": active, "active_contract": active_contract,
        "active_path": active_path, "authority": authority,
        "controller": controller, "operator_map_path": operator_map_path,
        "previous_receipt": previous_receipt, "provider": provider,
        "qualification_mode": qualification_mode,
        "runtime_ledger_path": runtime_ledger_path,
    }


def upgrade(args: argparse.Namespace) -> dict[str, Any]:
    root = Path(os.path.realpath(args.root))
    if not ROOT.fullmatch(str(root)):
        raise EnvironmentError("qualification root must be under /private/tmp")
    safe_directory(root)
    factory = args.factory_root.resolve(strict=True)
    transaction = TRANSACTION_ROOT
    product = args.product_root.resolve(strict=True)
    prepare_product_runtime(product, create=False)
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
        (transaction / "factory-contract.json").read_text(encoding="utf-8")
    ).get("contract_version")
    if contract not in SUPPORTED_CONTRACTS:
        raise EnvironmentError("qualification requires a supported Factory Contract")
    manifest = qualification_manifest(product, sha)
    validate_provider_cli_pins(transaction, sha)
    capacity = manifest["capacity"]
    validate_selected_contracts(product, manifest)
    active_path = root / f"projects/{args.project}/active.json"
    active = read(active_path)
    if active.get("kit_sha") != sha and manifest.get("mode") != "successor":
        for ticket in manifest["tickets"]:
            try:
                protected_terminal(product, ticket)
            except TerminalError:
                continue
            raise EnvironmentError(
                f"{ticket}: terminal qualification target requires a successor lane; "
                "normal in-place upgrade refused"
            )
    prepare_product_runtime(product)
    if command("git", "-C", str(product), "status", "--porcelain", "--untracked-files=all"):
        raise EnvironmentError("qualification product runtime contract is not ignored")
    product_sha = command("git", "-C", str(product), "rev-parse", "HEAD")
    product_tree = command("git", "-C", str(product), "rev-parse", "HEAD^{tree}")
    origin = product_origin(product)
    historical_objects = historical_pr_objects(product, origin)
    source = validate_upgrade_source(
        root, args.project, transaction, product, sha, contract, manifest,
    )
    active = source["active"]
    active_contract = source["active_contract"]
    authority = source["authority"]
    controller = source["controller"]
    provider = source["provider"]
    operator_map_path = source["operator_map_path"]
    runtime_ledger_path = source["runtime_ledger_path"]
    qualification_mode = source["qualification_mode"]
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
        validate_provider(
            Path(active["release_path"]), authority, capacity, active_contract,
        )

        releases = safe_directory(root / "releases")
        release = releases / sha
        if release.exists():
            if git_tree(release) != tree:
                raise EnvironmentError("existing successor release tree is invalid")
        else:
            materialize(factory, sha, release)
        if git_tree(release) != tree:
            raise EnvironmentError("sealed qualification tree does not match the candidate")
        runtime_bin = prepare_qualification_runtime(
            release, product, root, args.project,
            getattr(args, "runtime_bin", None),
        )
        runtime_tuple = certification_preflight(
            transaction, product, sha, tree, contract, runtime_bin,
        )
        identity = authority_identity(
            args.project, contract, sha, tree, product, product_sha, product_tree,
            origin, runtime_tuple, str(operator_map_path), str(runtime_ledger_path),
        )
        validate_upgrade_authority(
            read(authority / "authority.json"), active, identity,
        )
        if active_contract == contract and active["kit_sha"] == sha:
            if active.get("release_path") != str(release):
                raise EnvironmentError("existing qualification activation is invalid")
            result = bind_runtime_tuple({
                "factory_sha": sha,
                "factory_tree": tree,
                "historical_pr_objects": historical_objects,
                "launcher": str(release / "scripts/factory-launch"),
                "product_sha": product_sha,
                "product_tree": product_tree,
                "project": args.project,
                "provider_policy_sha256": active["provider_policy_sha256"],
                "qualification_mode": qualification_mode,
                "root": str(root),
                "schema": SCHEMA,
                "status": "upgraded",
            }, runtime_tuple)
            if read(root / "environment.json") != result:
                raise EnvironmentError("existing qualification environment is invalid")
            register_human_target(
                release, release / "scripts/factory-launch", args.project, root,
            )
            return result
        resume_operator_state(authority, identity, manifest["tickets"])
        initialize_selected_operator(
            transaction, product, operator_map_path, runtime_ledger_path, controller,
        )
        validate_runtime_ledger(runtime_ledger_path)
        if command(
            "git", "-C", str(product), "status", "--porcelain",
            "--untracked-files=all",
        ):
            raise EnvironmentError("qualification operator initialization dirtied product")

        policy, activation, policy_hash = provider_configuration(
            release, capacity
        )
        if (
            read(provider / "provider-policy.json") != policy
            or read(provider / "provider-activation.json") != activation
        ):
            raise EnvironmentError("successor changes the active provider policy")
        snapshot_global_config(args, root)
        model_bundle_path = (
            root / "projects" / args.project / f"model-bundle-{sha}.json"
        )
        fallback_readiness, fallback_readiness_sha256 = qualification_fallback_readiness(
            release, root, args.project, product, bundle_path=model_bundle_path,
        )
        model_bundle_sha256 = read(model_bundle_path)["bundle_sha256"]

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
            "fallback_readiness": fallback_readiness,
            "fallback_readiness_sha256": fallback_readiness_sha256,
            "model_bundle_sha256": model_bundle_sha256,
            "controller_state_path": str(controller),
            "provider_state_path": str(provider),
            "operator_map_path": str(operator_map_path),
            "qualification_mode": qualification_mode,
            "runtime_ledger_path": str(runtime_ledger_path),
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
            "fallback_readiness_sha256": fallback_readiness_sha256,
            "model_bundle_sha256": model_bundle_sha256,
            "controller_state_path": str(controller),
            "provider_state_path": str(provider),
            "operator_map_path": str(operator_map_path),
            "qualification_mode": qualification_mode,
            "receipt_id": receipt_id,
            "release_path": str(release),
            "runtime_ledger_path": str(runtime_ledger_path),
        }, runtime_tuple)
        result = bind_runtime_tuple({
            "factory_sha": sha,
            "factory_tree": tree,
            "historical_pr_objects": historical_objects,
            "launcher": str(release / "scripts/factory-launch"),
            "product_sha": product_sha,
            "product_tree": product_tree,
            "project": args.project,
            "provider_policy_sha256": policy_hash,
            "qualification_mode": qualification_mode,
            "root": str(root),
            "schema": SCHEMA,
            "status": "upgraded",
        }, runtime_tuple)
        validate_upgrade_environment(
            read(root / "environment.json"), active, result,
        )
        replace(authority / "authority.json", identity)
        replace(root / "environment.json", result)
        replace(active_path, next_active)
        register_human_target(
            release, release / "scripts/factory-launch", args.project, root,
        )
        return result
    finally:
        os.close(lock)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--factory-root", required=True, type=Path)
    parser.add_argument("--product-root", required=True, type=Path)
    parser.add_argument("--project", required=True)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--global-env", type=Path)
    parser.add_argument("--operator-map-seed", type=Path)
    parser.add_argument("--runtime-bin", type=Path)
    parser.add_argument("--takeover-project")
    parser.add_argument("--preprovider-source-root", type=Path)
    parser.add_argument("--preprovider-source-project")
    parser.add_argument("--upgrade", action="store_true")
    parser.add_argument("--restore", action="store_true")
    args = parser.parse_args()
    try:
        if not PROJECT.fullmatch(args.project):
            raise EnvironmentError("invalid qualification project")
        if args.upgrade and args.restore:
            raise EnvironmentError("qualification restore and upgrade are exclusive")
        handoff = (
            args.preprovider_source_root is not None
            or args.preprovider_source_project is not None
        )
        if handoff and (
            args.preprovider_source_root is None
            or args.preprovider_source_project is None
        ):
            raise EnvironmentError("pre-provider handoff source is incomplete")
        result = (
            handoff_preprovider(args) if handoff else
            upgrade(args) if args.upgrade else prepare(args)
        )
        print(json.dumps(result, sort_keys=True))
    except (
        FileNotFoundError, json.JSONDecodeError, OSError, EnvironmentError,
        subprocess.SubprocessError, tarfile.TarError,
    ) as error:
        result = {"error": str(error), "schema": SCHEMA, "status": "error"}
        reason_code = getattr(error, "reason_code", None)
        if isinstance(reason_code, str):
            result["reason_code"] = reason_code
        print(json.dumps(result, sort_keys=True))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
