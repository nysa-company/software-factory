#!/usr/bin/env python3
"""Prepare and verify owner-local subscription CLI concurrency state."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import sys
import tempfile
from typing import Any


POLICY_SCHEMA = "factory-provider-concurrency-policy/v1"
ACTIVATION_SCHEMA = "nysa.software-factory.provider-activation/v2"
PLAN_SCHEMA = "nysa.software-factory.provider-concurrency-plan/v1"
STATUS_SCHEMA = "nysa.software-factory.provider-concurrency-status/v1"
CLI_ADAPTERS = frozenset(("claude-code", "codex", "cursor-anthropic", "cursor-openai"))
REQUIRED_ADAPTERS = CLI_ADAPTERS
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,199}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
APPLICATION_ID = 0x4E595343
MAX_JSON = 1_000_000


class ConfigError(ValueError):
    pass


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def secure_directory(
    path: Path, label: str, *, create: bool = False, owner_only: bool = True
) -> None:
    if not path.is_absolute():
        raise ConfigError(f"{label} path must be absolute")
    if create:
        path.mkdir(mode=0o700, parents=False, exist_ok=True)
    info = path.lstat()
    if (
        path.is_symlink()
        or not stat.S_ISDIR(info.st_mode)
        or path.resolve(strict=True) != path
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) & (0o077 if owner_only else 0o022)
    ):
        raise ConfigError(f"{label} is unsafe")


def secure_regular(path: Path, label: str, *, owner_only: bool = True) -> bytes:
    info = path.lstat()
    if (
        not path.is_absolute()
        or path.is_symlink()
        or not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_nlink != 1
        or info.st_size > MAX_JSON
        or stat.S_IMODE(info.st_mode) & (0o077 if owner_only else 0o022)
    ):
        raise ConfigError(f"{label} is unsafe")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(descriptor)
        if (
            (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino)
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
        ):
            raise ConfigError(f"{label} changed while opening")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            return handle.read(MAX_JSON + 1)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def read_json(path: Path, label: str, *, owner_only: bool = True) -> tuple[bytes, Any]:
    raw = secure_regular(path, label, owner_only=owner_only)
    if len(raw) > MAX_JSON:
        raise ConfigError(f"{label} is too large")
    try:
        return raw, json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ConfigError(f"{label} is invalid") from error


def catalog_routes(release: Path) -> dict[str, dict[str, str]]:
    secure_directory(release, "release", owner_only=False)
    raw, catalog = read_json(
        release / "scripts/model-routing/catalog-v1.json",
        "route catalog",
        owner_only=False,
    )
    if raw != (canonical(catalog) + "\n").encode():
        raise ConfigError("route catalog is not canonical")
    if (
        not isinstance(catalog, dict)
        or catalog.get("schema") != "model-route-catalog/v1"
        or catalog.get("version") != 1
        or not isinstance(catalog.get("routes"), list)
    ):
        raise ConfigError("route catalog is invalid")
    routes: dict[str, dict[str, str]] = {}
    for route in catalog["routes"]:
        if not isinstance(route, dict) or route.get("enabled") is not True:
            continue
        if route.get("adapter") not in CLI_ADAPTERS:
            continue
        selected = {
            "account_route": route.get("account_route_id"),
            "adapter": route.get("adapter"),
            "model": route.get("selection_id"),
            "provider_family": route.get("provider_family"),
        }
        route_id = route.get("route_id")
        if (
            not isinstance(route_id, str)
            or not SAFE_ID.fullmatch(route_id)
            or any(not isinstance(value, str) or not SAFE_ID.fullmatch(value)
                   for value in selected.values())
            or route_id in routes
        ):
            raise ConfigError("route catalog contains an invalid CLI route")
        routes[route_id] = selected
    adapters = {route["adapter"] for route in routes.values()}
    if adapters != REQUIRED_ADAPTERS:
        raise ConfigError("route catalog does not cover Cursor, Claude Code, and Codex")
    return routes


def desired_configuration(
    release: Path, capacity: int
) -> tuple[dict[str, Any], dict[str, Any]]:
    if isinstance(capacity, bool) or not 2 <= capacity <= 4:
        raise ConfigError("concurrency capacity must be from 2 through 4")
    routes = catalog_routes(release)
    limit = {
        "max_concurrent": capacity,
        "max_starts": max(24, capacity * 6),
        "window_seconds": 60,
    }
    policy = {
        "account_routes": {
            name: dict(limit)
            for name in sorted({route["account_route"] for route in routes.values()})
        },
        "coupled_max_concurrent": capacity,
        "global": dict(limit),
        "provider_families": {
            name: dict(limit)
            for name in sorted({route["provider_family"] for route in routes.values()})
        },
        "schema": POLICY_SCHEMA,
    }
    activation = {
        "enabled": True,
        "mode": "cli-concurrent-v1",
        "policy_sha256": digest(policy),
        "routes": routes,
        "schema": ACTIVATION_SCHEMA,
    }
    return policy, activation


def paths(root: Path, activation: Path | None = None) -> dict[str, Path]:
    activation = activation or root / "isolated-v1.enabled"
    if not activation.is_absolute() or activation.parent != root:
        raise ConfigError("provider activation path must be directly under the state root")
    return {
        "activation": activation,
        "apply_root": root / "provider-apply-locks",
        "attempt_root": root / "provider-attempts",
        "cli_root": root / "cli-runtimes",
        "configuration_lock": root / "provider-configuration.lock",
        "database": root / "accounting/state-v2.sqlite3",
        "policy": root / "provider-policy.json",
    }


def validate_cursor_runtime_path(root: Path) -> None:
    runtime = root / "cli-runtimes/c" / ("0" * 22) / "data"
    if len(str(runtime)) > 75 or len(str(runtime / "projects")) > 84:
        raise ConfigError("owner-local root is too long for isolated Cursor scratch")


def plan(release: Path, root: Path, capacity: int) -> dict[str, Any]:
    if not root.is_absolute():
        raise ConfigError("provider state root path must be absolute")
    validate_cursor_runtime_path(root)
    policy, activation = desired_configuration(release, capacity)
    value = {
        "activation": activation,
        "capacity": capacity,
        "paths": {name: str(path) for name, path in paths(root).items()},
        "policy": policy,
        "release": str(release),
        "schema": PLAN_SCHEMA,
    }
    return {**value, "approval_sha256": digest(value)}


def atomic_write(path: Path, value: dict[str, Any]) -> None:
    raw = (canonical(value) + "\n").encode()
    if path.exists() or path.is_symlink():
        current = secure_regular(path, path.name)
        if current == raw:
            return
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def validate_database(database: Path, *, require_idle: bool) -> None:
    if not database.exists() and not database.is_symlink():
        if require_idle:
            return
        raise ConfigError("provider database is missing")
    secure_regular(database, "provider database")
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        connection.execute("PRAGMA query_only=ON")
        if (
            connection.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID
            or connection.execute("PRAGMA user_version").fetchone()[0] != 2
            or connection.execute(
                "SELECT value FROM metadata WHERE key='schema'"
            ).fetchone() != ("factory-provider-state/v2",)
        ):
            raise ConfigError("provider database identity is invalid")
        active = connection.execute(
            "SELECT count(*) FROM attempts "
            "WHERE state IN ('reserved','GO','submitted')"
        ).fetchone()[0]
        legacy = connection.execute("SELECT count(*) FROM legacy_intervals").fetchone()[0]
        if require_idle and (active or legacy):
            raise ConfigError("provider configuration cannot change while work is active")
    except sqlite3.Error as error:
        raise ConfigError("provider database is invalid") from error
    finally:
        connection.close()


def apply(release: Path, root: Path, capacity: int, approval: str) -> dict[str, Any]:
    secure_directory(root, "provider state root")
    selected = paths(root)
    lock_descriptor = os.open(
        selected["configuration_lock"],
        os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        lock_info = os.fstat(lock_descriptor)
        if (
            not stat.S_ISREG(lock_info.st_mode)
            or lock_info.st_uid != os.geteuid()
            or lock_info.st_nlink != 1
            or stat.S_IMODE(lock_info.st_mode) != 0o600
        ):
            raise ConfigError("provider configuration lock is unsafe")
        fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
        expected = plan(release, root, capacity)
        if (
            not SHA256.fullmatch(approval)
            or approval != expected["approval_sha256"]
        ):
            raise ConfigError("provider configuration approval hash does not match")
        validate_database(selected["database"], require_idle=True)
        for name in (
            "accounting",
            "provider-attempts",
            "provider-apply-locks",
            "cli-runtimes",
        ):
            secure_directory(root / name, name, create=True)
        result = subprocess.run(
            [
                sys.executable,
                str(release / "scripts/provider-coordinator.py"),
                "--db",
                str(selected["database"]),
                "status",
            ],
            text=True,
            capture_output=True,
            check=False,
            timeout=60,
        )
        if result.returncode:
            raise ConfigError("provider database initialization failed")
        atomic_write(selected["policy"], expected["policy"])
        atomic_write(selected["activation"], expected["activation"])
        return check(release, root, capacity)
    finally:
        os.close(lock_descriptor)


def validate_limit(value: Any, label: str, capacity: int) -> None:
    if (
        not isinstance(value, dict)
        or set(value) != {"max_concurrent", "max_starts", "window_seconds"}
        or not isinstance(value.get("max_concurrent"), int)
        or isinstance(value.get("max_concurrent"), bool)
        or value["max_concurrent"] != capacity
        or not isinstance(value.get("max_starts"), int)
        or isinstance(value.get("max_starts"), bool)
        or value["max_starts"] < value["max_concurrent"]
        or not isinstance(value.get("window_seconds"), int)
        or isinstance(value.get("window_seconds"), bool)
        or value["window_seconds"] < 1
    ):
        raise ConfigError(f"{label} cannot sustain configured ticket concurrency")


def check(
    release: Path, root: Path, capacity: int, activation: Path | None = None
) -> dict[str, Any]:
    validate_cursor_runtime_path(root)
    routes = catalog_routes(release)
    secure_directory(root, "provider state root")
    selected = paths(root, activation)
    policy_raw, policy = read_json(selected["policy"], "provider policy")
    activation_raw, activation = read_json(
        selected["activation"], "provider activation"
    )
    expected_policy, expected_activation = desired_configuration(
        release, capacity
    )
    if policy_raw != (canonical(policy) + "\n").encode():
        raise ConfigError("provider policy is not canonical")
    if activation_raw != (canonical(activation) + "\n").encode():
        raise ConfigError("provider activation is not canonical")
    if policy != expected_policy or activation != expected_activation:
        raise ConfigError(
            "provider concurrency state does not match the approved configuration"
        )
    if (
        not isinstance(policy, dict)
        or set(policy)
        != {"account_routes", "coupled_max_concurrent", "global",
            "provider_families", "schema"}
        or policy.get("schema") != POLICY_SCHEMA
        or not isinstance(policy.get("coupled_max_concurrent"), int)
        or isinstance(policy.get("coupled_max_concurrent"), bool)
        or policy["coupled_max_concurrent"] != capacity
    ):
        raise ConfigError("provider policy cannot sustain configured ticket concurrency")
    validate_limit(policy.get("global"), "global policy", capacity)
    for scope, field in (
        ("provider_families", "provider_family"),
        ("account_routes", "account_route"),
    ):
        values = policy.get(scope)
        required_names = {route[field] for route in routes.values()}
        if not isinstance(values, dict) or set(values) != required_names:
            raise ConfigError(f"{scope} policy is invalid")
        for name in required_names:
            validate_limit(values.get(name), f"{scope}.{name}", capacity)
    if (
        not isinstance(activation, dict)
        or set(activation)
        != {"enabled", "mode", "policy_sha256", "routes", "schema"}
        or activation.get("schema") != ACTIVATION_SCHEMA
        or activation.get("enabled") is not True
        or activation.get("mode") != "cli-concurrent-v1"
        or activation.get("policy_sha256") != digest(policy)
        or activation.get("routes") != routes
    ):
        raise ConfigError("provider activation is invalid")
    for route_id, expected in routes.items():
        if activation["routes"].get(route_id) != expected:
            raise ConfigError(f"provider activation does not cover {route_id}")
        result = subprocess.run(
            [
                sys.executable,
                str(release / "scripts/provider-activation.py"),
                "--config",
                str(selected["activation"]),
                "--policy",
                str(selected["policy"]),
                "--contract-version",
                "1.8.0",
                "--route-id",
                route_id,
            ],
            text=True,
            capture_output=True,
            check=False,
            timeout=60,
        )
        if result.returncode:
            raise ConfigError(f"provider activation rejected {route_id}")
        try:
            resolved = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise ConfigError("provider activation returned invalid output") from error
        if any(resolved.get(field) != value for field, value in expected.items()):
            raise ConfigError(f"provider activation changed {route_id}")
    for name in ("attempt_root", "apply_root", "cli_root"):
        secure_directory(selected[name], name)
    secure_regular(selected["configuration_lock"], "provider configuration lock")
    validate_database(selected["database"], require_idle=False)
    runtime_info = selected["cli_root"].lstat()
    return {
        "activated_routes": sorted(routes),
        "adapters": sorted({route["adapter"] for route in routes.values()}),
        "capacity": policy["coupled_max_concurrent"],
        "policy_capacities": {
            "account_routes": {
                name: value["max_concurrent"]
                for name, value in sorted(policy["account_routes"].items())
            },
            "coupled": policy["coupled_max_concurrent"],
            "global": policy["global"]["max_concurrent"],
            "provider_families": {
                name: value["max_concurrent"]
                for name, value in sorted(policy["provider_families"].items())
            },
        },
        "policy_sha256": digest(policy),
        "release_path": str(release),
        "required_capacity": capacity,
        "runtime_root": {
            "device": runtime_info.st_dev,
            "inode": runtime_info.st_ino,
            "mode": format(stat.S_IMODE(runtime_info.st_mode), "04o"),
            "owner_uid": runtime_info.st_uid,
            "path": str(selected["cli_root"]),
        },
        "schema": STATUS_SCHEMA,
        "status": "ready",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--release", required=True, type=Path)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--capacity", required=True, type=int)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("plan")
    apply_parser = commands.add_parser("apply")
    apply_parser.add_argument("--approve-hash", required=True)
    check_parser = commands.add_parser("check")
    check_parser.add_argument("--activation", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "plan":
            value = plan(args.release, args.root, args.capacity)
        elif args.command == "apply":
            value = apply(
                args.release, args.root, args.capacity, args.approve_hash
            )
        else:
            value = check(
                args.release, args.root, args.capacity, args.activation
            )
        print(canonical(value))
    except (
        ConfigError,
        FileNotFoundError,
        OSError,
        sqlite3.Error,
        subprocess.SubprocessError,
    ) as error:
        print(f"provider-concurrency-config: {error}", file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
