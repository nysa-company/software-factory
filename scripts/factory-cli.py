#!/usr/bin/python3
"""Small human interface for exact Factory launcher targets."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import shlex
import stat
import subprocess
import sys
import tempfile
import unicodedata


TARGET = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}\Z")
PROJECT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
TICKET = re.compile(r"T-[0-9]{1,12}\Z")
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
DOCTOR_CHECKS = {
    "active_binding", "authenticated_artifacts", "clis", "contract_resume",
    "controller", "credentials", "fallback_readiness", "isolated_provider",
    "kit", "kit_pin", "model_readiness", "provider_cli_pins",
    "qualification_identity", "qualification_ticket_readiness", "runtime",
    "transition_receipts",
}
DOCTOR_STATUSES = {"error", "ok", "warning"}
DOCTOR_CHECK_STATUSES = {*DOCTOR_STATUSES, "not_applicable", "unknown"}
PRIORITY = {"urgent": 0, "high": 1, "normal": 2, "low": 3, "none": 4}
STATES = {
    "Approved", "Awaiting Approval", "Backlog", "Blocked-Escalated",
    "Building", "Canceled", "Done", "Planning", "Ready", "Review",
}
MAX_OUTPUT = 4_000_000
SUPPORTED_CONTRACTS = {"1.8.0", "1.9.0", "2.0.0"}
QUALIFICATION_LAUNCHER = re.compile(
    r"(?P<root>/private/tmp/nysa-sf-qualification\.[A-Za-z0-9._-]+)/releases/"
    r"(?P<sha>[0-9a-f]{40})/scripts/factory-launch\Z"
)


class CliError(ValueError):
    pass


class LauncherRefused(CliError):
    def __init__(self, message: str, value: dict | None = None):
        super().__init__(message)
        self.value = value


class ExactLauncher:
    def __init__(self, path: Path, descriptor: int, identity: tuple[int, ...]):
        self.path = path
        self.descriptor = descriptor
        self.identity = identity
        self.lock_path = None
        self.qualification = None

    def check(self) -> None:
        try:
            path_info = self.path.lstat()
            open_info = os.fstat(self.descriptor)
        except OSError as error:
            raise CliError("target launcher changed; run factory use") from error
        observed = (
            open_info.st_dev, open_info.st_ino, open_info.st_size,
            open_info.st_mtime_ns, stat.S_IMODE(open_info.st_mode),
        )
        if (
            self.path.is_symlink()
            or (path_info.st_dev, path_info.st_ino) != (open_info.st_dev, open_info.st_ino)
            or observed != self.identity
        ):
            raise CliError("target launcher changed; run factory use")

    def close(self) -> None:
        if self.descriptor >= 0:
            os.close(self.descriptor)
            self.descriptor = -1

    def acquire_lock(self) -> int:
        if self.lock_path is None:
            return -1
        descriptor = -1
        try:
            probe = self.lock_path.lstat()
            descriptor = os.open(
                self.lock_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            )
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600
                or (probe.st_dev, probe.st_ino) != (info.st_dev, info.st_ino)
            ):
                raise CliError("Factory launcher lock is unsafe")
            fcntl.flock(descriptor, fcntl.LOCK_SH)
            current = self.lock_path.lstat()
            if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                raise CliError("Factory launcher lock changed")
            return descriptor
        except OSError as error:
            if descriptor >= 0:
                os.close(descriptor)
            raise CliError("Factory launcher lock is unavailable") from error
        except CliError:
            if descriptor >= 0:
                os.close(descriptor)
            raise


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise CliError("duplicate JSON key")
        value[key] = item
    return value


def _regular(path: Path, label: str, maximum: int) -> bytes:
    try:
        probe = path.lstat()
    except OSError as error:
        raise CliError(f"{label} is unavailable; run factory use") from error
    if stat.S_ISLNK(probe.st_mode):
        raise CliError(f"{label} is unsafe")
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as error:
        raise CliError(f"{label} is unavailable; run factory use") from error
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_size > maximum
            or (probe.st_dev, probe.st_ino) != (before.st_dev, before.st_ino)
        ):
            raise CliError(f"{label} is unsafe")
        raw = os.read(descriptor, before.st_size + 1)
        after = os.fstat(descriptor)
        if len(raw) != before.st_size or (
            before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns
        ) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise CliError(f"{label} changed while reading")
        return raw
    finally:
        os.close(descriptor)


def _directory(path: Path, label: str, create: bool = False) -> Path:
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        info = path.lstat()
    except OSError as error:
        raise CliError(f"{label} is unavailable") from error
    if (
        not path.is_absolute()
        or path.is_symlink()
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o700
        or path.resolve(strict=True) != path
    ):
        raise CliError(f"{label} is unsafe")
    return path


def _atomic(path: Path, raw: bytes) -> None:
    _directory(path.parent, "Factory preference directory", create=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
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


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _checked_directory(path: Path, descriptor: int, label: str) -> None:
    try:
        current = path.lstat()
        opened = os.fstat(descriptor)
    except OSError as error:
        raise CliError(f"{label} changed") from error
    if (
        not stat.S_ISDIR(opened.st_mode) or opened.st_uid != os.geteuid()
        or stat.S_IMODE(opened.st_mode) != 0o700
        or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)
    ):
        raise CliError(f"{label} changed")


def _atomic_at(directory: int, name: str, raw: bytes) -> None:
    temporary = f".{name}.{secrets.token_hex(16)}"
    descriptor = -1
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory,
        )
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass


def _regular_at(
    directory: int, name: str, label: str, maximum: int,
) -> tuple[bytes, tuple[int, ...]]:
    descriptor = -1
    try:
        descriptor = os.open(
            name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=directory,
        )
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid()
            or before.st_nlink != 1 or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_size > maximum
        ):
            raise CliError(f"{label} is unsafe")
        raw = os.read(descriptor, before.st_size + 1)
        after = os.fstat(descriptor)
        identity = (
            before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns,
        )
        if len(raw) != before.st_size or identity != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns,
        ):
            raise CliError(f"{label} changed while reading")
        return raw, identity
    except OSError as error:
        raise CliError(f"{label} is unavailable; run factory use") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _identity_at(directory: int, name: str) -> tuple[int, ...]:
    info = os.stat(name, dir_fd=directory, follow_symlinks=False)
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _registry_lock(targets: Path) -> tuple[int, int]:
    _directory(targets.parent, "Factory preference directory")
    _directory(targets, "target directory", create=True)
    parent = directory = -1
    try:
        parent = os.open(
            targets.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        _checked_directory(targets.parent, parent, "Factory preference directory")
        fcntl.flock(parent, fcntl.LOCK_EX)
        _checked_directory(targets.parent, parent, "Factory preference directory")
        directory = os.open(
            targets.name,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent,
        )
        _checked_directory(targets, directory, "Factory target directory")
        return parent, directory
    except (OSError, CliError):
        if directory >= 0:
            os.close(directory)
        if parent >= 0:
            os.close(parent)
        raise


def _account_home() -> Path:
    try:
        return Path(pwd.getpwuid(os.getuid()).pw_dir).resolve(strict=True)
    except (KeyError, OSError) as error:
        raise CliError("account home is unavailable") from error


def _secure_parent(path: Path, label: str, *, owner: bool = True) -> None:
    try:
        info = path.lstat()
    except OSError as error:
        raise CliError(f"{label} is unavailable") from error
    if (
        path.is_symlink() or not stat.S_ISDIR(info.st_mode)
        or path.resolve(strict=True) != path
        or owner and info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) & 0o022
    ):
        raise CliError(f"{label} is unsafe")


def _exact_directory(path: Path, mode: int, label: str) -> None:
    _secure_parent(path, label)
    if stat.S_IMODE(path.lstat().st_mode) != mode:
        raise CliError(f"{label} is unsafe")


def _canonical(value: dict) -> bytes:
    return (
        json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode()


def _git_tree(path: Path) -> str:
    with tempfile.TemporaryDirectory(prefix="factory-target-tree.") as raw:
        repository = Path(raw) / "repo.git"
        environment = {
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_INDEX_FILE": str(Path(raw) / "index"),
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "LC_ALL": "C",
            "PATH": "/usr/bin:/bin",
        }

        def run(*arguments: str) -> str:
            try:
                result = subprocess.run(
                    ["/usr/bin/git", *arguments], text=True, capture_output=True,
                    check=False, env=environment, timeout=120,
                )
            except (OSError, UnicodeError, subprocess.TimeoutExpired) as error:
                raise CliError("qualification target trust evidence is unavailable") from error
            if result.returncode:
                raise CliError("qualification target trust evidence is invalid")
            return result.stdout.strip()

        run("init", "--bare", "-q", str(repository))
        run("--git-dir", str(repository), "config", "core.bare", "false")
        run("--git-dir", str(repository), "read-tree", "--empty")
        run(
            "--git-dir", str(repository), "--work-tree", str(path),
            "add", "-f", "-A", "--", ".",
        )
        return run(
            "--git-dir", str(repository), "--work-tree", str(path),
            "write-tree",
        )


def _trusted_qualification_launcher(
    launcher: ExactLauncher, project: str, match: re.Match[str],
) -> None:
    root = Path(match.group("root"))
    sha = match.group("sha")
    release = launcher.path.parents[1]
    _exact_directory(root, 0o700, "qualification root")
    _exact_directory(root / "releases", 0o700, "qualification release directory")
    _exact_directory(release, 0o555, "sealed qualification release")
    _exact_directory(root / "projects", 0o700, "qualification project directory")
    _exact_directory(
        root / "projects" / project, 0o700, "qualification project state",
    )
    _exact_directory(root / "receipts", 0o700, "qualification receipt directory")
    try:
        marker = json.loads(
            _regular(root / "marker.json", "qualification marker", 4096),
            object_pairs_hook=_unique,
        )
        active = json.loads(
            _regular(
                root / "projects" / project / "active.json",
                "qualification active release", 131_072,
            ),
            object_pairs_hook=_unique,
        )
        receipt_id = active.get("receipt_id", "") if isinstance(active, dict) else ""
        if not re.fullmatch(r"[0-9a-f]{64}", str(receipt_id)):
            raise CliError("qualification target trust evidence is invalid")
        receipt = json.loads(
            _regular(
                root / "receipts" / f"{receipt_id}.json",
                "qualification activation receipt", 131_072,
            ),
            object_pairs_hook=_unique,
        )
    except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise CliError("qualification target trust evidence is invalid") from error
    unsigned = dict(receipt) if isinstance(receipt, dict) else {}
    observed_receipt = unsigned.pop("receipt_id", "")
    shared = (
        "contract_version", "kit_sha", "kit_tree", "product_path",
        "product_sha", "product_tree", "project", "provider_policy_sha256",
        "fallback_readiness_sha256", "qualification_mode",
        "operator_map_path", "controller_state_path", "provider_state_path",
        "runtime_ledger_path",
    )
    product_path = active.get("product_path", "") if isinstance(active, dict) else ""
    product = Path(product_path) if isinstance(product_path, str) else Path()
    digests = (
        active.get("provider_policy_sha256", ""),
        active.get("fallback_readiness_sha256", ""),
    ) if isinstance(active, dict) else ()
    bound_paths = tuple(
        active.get(key) for key in (
            "operator_map_path", "controller_state_path", "provider_state_path",
            "runtime_ledger_path",
        )
    ) if isinstance(active, dict) else ()
    if (
        marker != {
            "mode": "qualification",
            "schema": "nysa.software-factory.qualification-environment/v1",
        }
        or not isinstance(active, dict) or not isinstance(receipt, dict)
        or active.get("project") != project or active.get("kit_sha") != sha
        or active.get("qualification_mode") != "isolated"
        or active.get("release_path") != str(release)
        or active.get("receipt_id") != observed_receipt
        or receipt.get("status") != "pass"
        or observed_receipt != hashlib.sha256(_canonical(unsigned)).hexdigest()
        or any(
            key not in active or key not in receipt or active[key] != receipt[key]
            for key in shared
        )
        or active.get("contract_version") not in SUPPORTED_CONTRACTS
        or not isinstance(receipt.get("product_origin"), str)
        or not receipt.get("product_origin")
        or ("model_bundle_sha256" in active) != ("model_bundle_sha256" in receipt)
        or active.get("model_bundle_sha256") != receipt.get("model_bundle_sha256")
        or ("runtime_tuple" in active) != ("runtime_tuple" in receipt)
        or active.get("runtime_tuple") != receipt.get("runtime_tuple")
        or not isinstance(product_path, str) or not product.is_absolute()
        or not all(re.fullmatch(r"[0-9a-f]{64}", str(item)) for item in digests)
        or not all(
            isinstance(item, str) and Path(item).is_absolute()
            for item in bound_paths
        )
        or "runtime_tuple" in active and not isinstance(active["runtime_tuple"], dict)
        or not re.fullmatch(r"[0-9a-f]{40}", str(active.get("product_sha", "")))
        or not re.fullmatch(r"[0-9a-f]{40}", str(active.get("product_tree", "")))
        or not re.fullmatch(r"[0-9a-f]{40}", str(active.get("kit_tree", "")))
    ):
        raise CliError("qualification target trust evidence is invalid")
    operator_map, controller, provider, runtime_ledger = map(Path, bound_paths)
    authority = controller.parent
    if (
        provider.parent != authority
        or operator_map != authority / "operator/operator-map.json"
        or runtime_ledger != authority / "operator/runtime-ledger.csv"
        or controller != authority / "controller"
        or provider != authority / "provider"
    ):
        raise CliError("qualification target trust evidence is invalid")
    _exact_directory(authority, 0o700, "qualification authority")
    _exact_directory(controller, 0o700, "qualification controller state")
    _exact_directory(provider, 0o700, "qualification provider state")
    _exact_directory(authority / "operator", 0o700, "qualification operator state")
    try:
        authority_state = json.loads(
            _regular(
                authority / "authority.json", "qualification authority identity",
                131_072,
            ),
            object_pairs_hook=_unique,
        )
        _regular(operator_map, "qualification operator map", 131_072)
        _regular(runtime_ledger, "qualification runtime ledger", 4_000_000)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise CliError("qualification target trust evidence is invalid") from error
    authority_unsigned = dict(authority_state) if isinstance(authority_state, dict) else {}
    authority_digest = authority_unsigned.pop("authority_sha256", "")
    authority_expected = {
        "contract_version": active["contract_version"],
        "controller_state_path": active["controller_state_path"],
        "factory_sha": active["kit_sha"],
        "factory_tree": active["kit_tree"],
        "operator_map_path": active["operator_map_path"],
        "product_origin": receipt["product_origin"],
        "product_path": active["product_path"],
        "product_sha": active["product_sha"],
        "product_tree": active["product_tree"],
        "project": project,
        "provider_state_path": active["provider_state_path"],
        "runtime_ledger_path": active["runtime_ledger_path"],
        "runtime_tuple": active.get("runtime_tuple"),
    }
    if (
        not isinstance(authority_state, dict)
        or authority_state.get("schema")
        != "nysa.software-factory.qualification-authority/v1"
        or not re.fullmatch(r"[0-9a-f]{64}", str(authority_state.get("manifest_sha256", "")))
        or any(authority_state.get(key) != value for key, value in authority_expected.items())
        or authority_digest != hashlib.sha256(_canonical(authority_unsigned)).hexdigest()
    ):
        raise CliError("qualification target trust evidence is invalid")
    environment = {
        "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_NO_LAZY_FETCH": "1", "GIT_NO_REPLACE_OBJECTS": "1",
        "LC_ALL": "C", "PATH": "/usr/bin:/bin",
    }
    try:
        if product.resolve(strict=True) != product:
            raise CliError("qualification target trust evidence is invalid")
        identity = subprocess.run(
            ["/usr/bin/git", "-C", str(product), "rev-parse", "HEAD", "HEAD^{tree}"],
            text=True, capture_output=True, check=False, timeout=120,
            env=environment,
        )
        dirty = subprocess.run(
            [
                "/usr/bin/git", "-C", str(product), "status", "--porcelain",
                "--untracked-files=all",
            ],
            text=True, capture_output=True, check=False, timeout=120,
            env=environment,
        )
        origin = subprocess.run(
            [
                "/usr/bin/git", "-C", str(product), "remote", "get-url",
                "--push", "--all", "origin",
            ],
            text=True, capture_output=True, check=False, timeout=120,
            env=environment,
        )
    except (OSError, UnicodeError, subprocess.TimeoutExpired) as error:
        raise CliError("qualification target trust evidence is unavailable") from error
    lines = identity.stdout.splitlines()
    for path in release.rglob("*"):
        info = path.lstat()
        if path.is_symlink():
            target = Path(os.path.realpath(path))
            if target != release and release not in target.parents:
                raise CliError("sealed qualification release is unsafe")
        elif stat.S_IMODE(info.st_mode) & 0o222:
            raise CliError("sealed qualification release is unsafe")
    if (
        identity.returncode or dirty.returncode or origin.returncode or dirty.stdout
        or lines != [active["product_sha"], active["product_tree"]]
        or origin.stdout.splitlines() != [receipt["product_origin"]]
        or _git_tree(release) != active.get("kit_tree")
    ):
        raise CliError("qualification target trust evidence is invalid")
    launcher.check()


def _launcher(path: object) -> ExactLauncher:
    if not isinstance(path, str):
        raise CliError("target launcher is invalid")
    candidate = Path(path)
    if not candidate.is_absolute():
        raise CliError("target launcher is invalid")
    try:
        before = candidate.lstat()
        resolved = candidate.resolve(strict=True)
    except OSError as error:
        raise CliError("target launcher is unavailable; run factory use") from error
    if (
        candidate.is_symlink()
        or resolved != candidate
        or not stat.S_ISREG(before.st_mode)
        or before.st_uid != os.geteuid()
        or before.st_nlink != 1
        or stat.S_IMODE(before.st_mode) & 0o022
        or not stat.S_IMODE(before.st_mode) & 0o100
    ):
        raise CliError("target launcher is unsafe")
    _secure_parent(candidate.parent, "target launcher directory")
    try:
        descriptor = os.open(candidate, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as error:
        raise CliError("target launcher is unavailable; run factory use") from error
    opened = os.fstat(descriptor)
    if (
        (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
        or not stat.S_ISREG(opened.st_mode) or opened.st_uid != os.geteuid()
        or opened.st_nlink != 1 or not stat.S_IMODE(opened.st_mode) & 0o100
        or stat.S_IMODE(opened.st_mode) & 0o022
    ):
        os.close(descriptor)
        raise CliError("target launcher is unsafe")
    return ExactLauncher(candidate, descriptor, (
        opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns,
        stat.S_IMODE(opened.st_mode),
    ))


def _target(targets: Path, target_id: str, trusted: bool = False) -> tuple[ExactLauncher, str]:
    if not TARGET.fullmatch(target_id):
        raise CliError("selected target is invalid; run factory use")
    try:
        raw = _regular(targets / f"{target_id}.json", "target", 4096)
        value = json.loads(raw, object_pairs_hook=_unique)
    except CliError as error:
        if "unavailable" in str(error):
            raise CliError("selected target is unavailable; run factory use") from error
        raise
    except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise CliError("target is invalid; run factory use") from error
    if (
        not isinstance(value, dict)
        or set(value) != {"launcher", "project"}
        or not isinstance(value.get("project"), str)
        or not PROJECT.fullmatch(value["project"])
    ):
        raise CliError("target is invalid; run factory use")
    launcher = _launcher(value["launcher"])
    try:
        if trusted:
            _trusted_launcher(launcher, value["project"])
    except Exception:
        launcher.close()
        raise
    return launcher, value["project"]


def _selected(targets: Path, selection: Path, trusted: bool = False) -> tuple[ExactLauncher, str]:
    try:
        target_id = _regular(selection, "selection", 256).decode("ascii").strip()
    except UnicodeError as error:
        raise CliError("selection is invalid; run factory use") from error
    return _target(targets, target_id, trusted)


def _trusted_launcher(launcher: ExactLauncher, project: str) -> None:
    factory = _account_home() / ".factory"
    installed = factory / "bin/factory-launch"
    releases = factory / "kits/releases"
    try:
        production = launcher.path.relative_to(releases).parts
    except ValueError:
        production = ()
    qualification = QUALIFICATION_LAUNCHER.fullmatch(str(launcher.path))
    if launcher.path == installed:
        _secure_parent(installed.parent.parent, "Factory state directory")
        _secure_parent(installed.parent, "Factory command directory")
        launcher.lock_path = installed.parent.parent / ".launcher-pin.lock"
        descriptor = launcher.acquire_lock()
        try:
            launcher.check()
        finally:
            os.close(descriptor)
    elif (
        len(production) == 3 and re.fullmatch(r"[0-9a-f]{40}", production[0])
        and production[1:] == ("scripts", "factory-launch")
    ):
        sha = production[0]
        release = releases / sha
        _secure_parent(factory, "Factory state directory")
        _secure_parent(factory / "kits", "Factory kits directory")
        _secure_parent(releases, "Factory release directory")
        _secure_parent(release, "sealed production release")
        active_path = factory / "kits/projects" / project / "active.json"
        manifest_path = factory / "kits/manifests" / f"{sha}.json"
        try:
            active = json.loads(_regular(active_path, "active release", 64_000), object_pairs_hook=_unique)
            manifest = json.loads(_regular(manifest_path, "install manifest", 64_000), object_pairs_hook=_unique)
        except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
            raise CliError("production target trust evidence is invalid") from error
        raw = os.pread(launcher.descriptor, launcher.identity[2], 0)
        if (
            not isinstance(active, dict) or active.get("project") != project
            or active.get("kit_sha") != sha
            or not isinstance(manifest, dict) or manifest.get("schema_version") != 1
            or manifest.get("kit_sha") != sha
            or manifest.get("sealed_release_path") != str(release)
            or not re.fullmatch(r"[0-9a-f]{40}", str(manifest.get("git_tree", "")))
            or manifest.get("launcher_sha256") != hashlib.sha256(raw).hexdigest()
        ):
            raise CliError("production target trust evidence is invalid")
        launcher.lock_path = factory / ".launcher-pin.lock"
        descriptor = launcher.acquire_lock()
        try:
            launcher.check()
        finally:
            os.close(descriptor)
    elif qualification is not None:
        _trusted_qualification_launcher(launcher, project, qualification)
        launcher.qualification = (project, qualification)
    else:
        raise CliError("target launcher is outside a Factory trust root")


def _invoke(launcher: ExactLauncher, project: str, arguments: list[str]) -> tuple[dict, int]:
    lock = launcher.acquire_lock()
    try:
        launcher.check()
        if launcher.qualification is not None:
            _trusted_qualification_launcher(launcher, *launcher.qualification)
        try:
            result = subprocess.run(
                [str(launcher.path), project, *arguments],
                stdin=subprocess.DEVNULL,
                text=True,
                capture_output=True,
                timeout=720,
                check=False,
            )
        except UnicodeError as error:
            raise CliError("launcher returned invalid text") from error
        except (OSError, subprocess.TimeoutExpired) as error:
            raise CliError("selected target is unavailable; run factory use") from error
        launcher.check()
        if launcher.qualification is not None:
            _trusted_qualification_launcher(launcher, *launcher.qualification)
    finally:
        if lock >= 0:
            os.close(lock)
    if len(result.stdout.encode()) > MAX_OUTPUT:
        raise CliError("launcher output is too large")
    try:
        value = json.loads(result.stdout, object_pairs_hook=_unique)
    except (json.JSONDecodeError, RecursionError) as error:
        if result.returncode:
            raise LauncherRefused("launcher_refused") from error
        raise CliError("launcher returned invalid JSON") from error
    if not isinstance(value, dict):
        raise CliError("launcher returned invalid JSON")
    return value, result.returncode


def _call(launcher: ExactLauncher, project: str, arguments: list[str]) -> dict:
    value, code = _invoke(launcher, project, arguments)
    if code:
        raise LauncherRefused("launcher_refused", value)
    return value


def _evidence_command(
    launcher: ExactLauncher, project: str, arguments: list[str],
) -> str:
    return " ".join(
        shlex.quote(str(value)) for value in (launcher.path, project, *arguments)
    )


def _safe_title(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 1000:
        raise CliError("ticket title is invalid")
    value = ANSI.sub("", value)
    value = "".join(" " if unicodedata.category(character).startswith("C") else character for character in value)
    value = " ".join(value.split())
    if not value:
        raise CliError("ticket title is invalid")
    return value


def _workflow(launcher: ExactLauncher, project: str) -> dict:
    arguments = ["operator-snapshot", "workflow", "--json"]
    evidence = _evidence_command(launcher, project, arguments)

    def invalid(condition: str) -> CliError:
        return CliError(
            f"{condition}. Impact: do not continue Factory mutations. "
            f"Evidence: {evidence}. Recovery: run factory doctor"
        )

    try:
        value = _call(launcher, project, arguments)
    except CliError as error:
        if "run factory use" in str(error):
            raise
        raise invalid("workflow snapshot could not be produced") from error
    tickets = value.get("tickets")
    if (
        value.get("schema") != "factory-operator-workflow/v1"
        or value.get("project") != project
        or not isinstance(value.get("mode"), str)
        or value.get("mode") not in {"production", "qualification"}
        or not isinstance(value.get("label"), str)
        or not value["label"].strip()
        or not isinstance(tickets, list)
        or len(tickets) > 10_000
    ):
        raise invalid("workflow snapshot is invalid")
    try:
        label = _safe_title(value["label"])
    except CliError as error:
        raise invalid("workflow label is invalid") from error
    seen = set()
    normalized = []
    for item in tickets:
        if not isinstance(item, dict) or not TICKET.fullmatch(str(item.get("ticket", ""))):
            raise invalid("workflow ticket identifier is invalid")
        dependencies = item.get("depends_on")
        if (
            item["ticket"] in seen
            or not isinstance(item.get("priority"), str)
            or item.get("priority") not in PRIORITY
            or not isinstance(item.get("state"), str)
            or item.get("state") not in STATES
            or not isinstance(dependencies, list)
            or any(not isinstance(entry, str) or not TICKET.fullmatch(entry) for entry in dependencies)
            or len(dependencies) != len(set(dependencies))
        ):
            raise invalid("workflow ticket snapshot is invalid")
        seen.add(item["ticket"])
        try:
            title = _safe_title(item.get("title"))
        except CliError as error:
            raise invalid("workflow ticket title is invalid") from error
        normalized.append({**item, "title": title})
    return {**value, "label": label, "tickets": normalized}


def _rank(ticket: dict, states: dict[str, str] | None = None) -> tuple[int, bool, int]:
    blocked = bool(ticket["depends_on"]) if states is None else any(
        states.get(dependency) != "Done" for dependency in ticket["depends_on"]
    )
    return PRIORITY[ticket["priority"]], blocked, int(ticket["ticket"][2:])


def _choose(stdin, stdout, count: int) -> int:
    stdout.write("Select: ")
    answer = stdin.readline().strip()
    if not answer.isdigit() or not 1 <= int(answer) <= count:
        raise CliError("selection is invalid")
    return int(answer) - 1


def _confirm(stdin, stdout) -> None:
    stdout.write("Proceed? ")
    if stdin.readline().strip().lower() != "yes":
        raise CliError("action canceled")


def _use(targets: Path, selection: Path, stdin, stdout, trusted: bool) -> None:
    _directory(targets, "target directory")
    choices = []
    ignored = 0
    for path in sorted(targets.glob("*.json")):
        if not TARGET.fullmatch(path.stem):
            ignored += 1
            continue
        launcher = None
        try:
            launcher, project = _target(targets, path.stem, trusted)
            workflow = _workflow(launcher, project)
        except CliError:
            ignored += 1
            continue
        finally:
            if launcher is not None:
                launcher.close()
        suffix = "Production" if workflow["mode"] == "production" else f"Qualification · {len(workflow['tickets'])} tickets"
        choices.append((path.stem, f"{workflow['label']} · {suffix}"))
    if not choices:
        raise CliError(
            "no valid targets; rerun the same supported Factory setup or "
            "qualification preparation command"
        )
    if ignored:
        stdout.write(f"Ignored {ignored} unavailable or invalid target records.\n\n")
    stdout.write("Choose a project:\n\n")
    for number, (_, label) in enumerate(choices, 1):
        stdout.write(f"{number}  {label}\n")
    selected = choices[_choose(stdin, stdout, len(choices))][0]
    _atomic(selection, f"{selected}\n".encode("ascii"))


def _backlog(workflow: dict, stdout) -> None:
    states = {item["ticket"]: item["state"] for item in workflow["tickets"]}
    tickets = sorted(
        (item for item in workflow["tickets"] if item["state"] == "Backlog"),
        key=lambda item: _rank(item, states),
    )
    stdout.write(f"{workflow['label']} · Backlog\n\nRank  Ticket  Title  Priority  State  Depends on\n")
    for number, ticket in enumerate(tickets, 1):
        dependencies = ",".join(ticket["depends_on"]) or "none"
        stdout.write(
            f"{number}  {ticket['ticket']}  {ticket['title']}  "
            f"{ticket['priority']}  {ticket['state']}  {dependencies}\n"
        )


def _doctor(launcher: ExactLauncher, project: str, stdout) -> None:
    evidence = _evidence_command(launcher, project, ["doctor", "--json"])

    def unavailable(condition: str) -> CliError:
        return CliError(
            f"{condition}. Impact: do not continue Factory mutations. "
            f"Evidence: {evidence}. Recovery: run the evidence command and do "
            "not mutate until Doctor returns a valid green report"
        )

    try:
        value, code = _invoke(launcher, project, ["doctor", "--json"])
    except CliError as error:
        if "run factory use" in str(error):
            raise
        raise unavailable("Doctor could not produce a report") from error
    checks = value.get("checks")
    if (
        value.get("schema") != "nysa.software-factory.doctor/v2"
        or "project" in value and value.get("project") != project
        or not isinstance(checks, dict)
        or not isinstance(value.get("overall_status"), str)
        or value.get("overall_status") not in DOCTOR_STATUSES
        or code != {"error": 1, "ok": 0, "warning": 0}[value["overall_status"]]
        or any(
            not isinstance(check, dict)
            or not isinstance(check.get("status"), str)
            or check.get("status") not in DOCTOR_CHECK_STATUSES
            for check in checks.values()
        )
        or value.get("overall_status") == "ok" and any(
            check["status"] not in {"ok", "not_applicable"}
            for check in checks.values()
        )
    ):
        raise unavailable("Doctor report is invalid")
    if code or value.get("overall_status") != "ok":
        supplied_status = value.get("overall_status")
        status = supplied_status
        failures = []
        for name in sorted(checks):
            check = checks[name]
            check_name = name if name in DOCTOR_CHECKS else "unknown_check"
            if not isinstance(check, dict):
                failures.append(f"{check_name}=invalid")
                continue
            check_status = check.get("status")
            if isinstance(check_status, str) and check_status in {
                "ok", "not_applicable",
            }:
                continue
            failures.append(
                f"{check_name}="
                f"{check_status if isinstance(check_status, str) and check_status in DOCTOR_CHECK_STATUSES else 'invalid'}"
            )
        summary = ", ".join(failures[:3]) or status
        if len(failures) > 3:
            summary += f" (+{len(failures) - 3} more)"
        raise CliError(
            f"Doctor {status}: {summary}. Impact: do not continue Factory "
            f"mutations. Evidence: {evidence}"
        )
    isolated = checks.get("isolated_provider", {})
    runtime = checks.get("runtime", {})
    unknown_workers = (
        isolated.get("unknown_workers") if isinstance(isolated, dict) else None
    )
    capacity = (
        runtime.get("max_concurrent_tickets") if isinstance(runtime, dict) else None
    )
    if (
        unknown_workers is not None and (
            isinstance(unknown_workers, bool)
            or not isinstance(unknown_workers, int) or unknown_workers < 0
        )
        or capacity is not None and (
            isinstance(capacity, bool)
            or not isinstance(capacity, int) or capacity < 0
        )
    ):
        raise unavailable("Doctor report is invalid")
    details = []
    if unknown_workers is not None:
        details.append(f"{unknown_workers} unknown workers")
    if capacity is not None:
        details.append(f"capacity {capacity}")
    stdout.write("Doctor passed" + (" · " + " · ".join(details) if details else "") + "\n")


def _next(
    launcher: ExactLauncher, project: str, workflow: dict, stdin, stdout,
    trusted: bool,
) -> None:
    tickets = workflow["tickets"]
    states = {item["ticket"]: item["state"] for item in tickets}
    if workflow["mode"] == "qualification":
        cohort = sorted((item for item in tickets if item["state"] != "Done"), key=_rank)
        if not cohort:
            stdout.write("No action needs you.\n")
            return
        ready_to_close = all(item["state"] == "Awaiting Approval" for item in cohort)
        verb = "Close" if ready_to_close else "Continue"
        stdout.write(f"{workflow['label']} · Qualification\n\n1  {verb} {len(cohort)} tickets\n")
        for ticket in cohort:
            stdout.write(f"   {ticket['ticket']} · {ticket['title']}\n")
        _choose(stdin, stdout, 1)
        _confirm(stdin, stdout)
        try:
            result, code = _invoke(
                launcher, project, ["qualification-finish", "--json"],
            )
        except (CliError, OSError, UnicodeError, RecursionError) as error:
            raise CliError(
                "qualification mutation outcome is unknown; do not repeat; "
                "run factory doctor"
            ) from error
        status = result.get("status")
        project_matches = result.get("project") == project or (
            trusted and status == "error" and "project" not in result
        )
        if (
            not project_matches
            or trusted and result.get("schema")
            != "nysa.software-factory.qualification-run/v1"
            or not isinstance(status, str)
            or status not in {"green", "waiting", "blocked", "error"}
            or code != {"blocked": 3, "error": 2, "green": 0, "waiting": 3}[status]
        ):
            raise CliError(
                "qualification result is invalid; mutation outcome is unknown; "
                "do not repeat; run factory doctor"
            )
        if code or status != "green":
            raise CliError(
                f"Qualification is {status}. Outcome: known non-green; run "
                "factory doctor; after it passes and the condition is resolved, "
                "rerun factory"
            )
        stdout.write("Qualification closed.\n")
        return
    approvals = sorted((item for item in tickets if item["state"] == "Awaiting Approval"), key=_rank)
    if approvals:
        choices = approvals
        action = "approve"
        verb = "Approve"
    else:
        choices = sorted(
            (
                item for item in tickets
                if item["state"] == "Backlog" and not _rank(item, states)[1]
            ),
            key=lambda item: _rank(item, states),
        )
        action = "ready"
        verb = "Mark ready"
    if not choices:
        stdout.write("No action needs you.\n")
        return
    stdout.write(f"{workflow['label']} · Next\n\n")
    for number, ticket in enumerate(choices, 1):
        stdout.write(f"{number}  {verb} {ticket['ticket']} · {ticket['title']}\n")
    ticket = choices[_choose(stdin, stdout, len(choices))]["ticket"]
    _confirm(stdin, stdout)
    try:
        result = _call(
            launcher, project,
            ["operator", action, "--ticket", ticket, "--json"],
        )
    except (CliError, OSError, UnicodeError, RecursionError) as error:
        raise CliError(
            "operator mutation outcome is unknown; do not repeat; "
            "run factory doctor"
        ) from error
    valid = (
        result.get("schema") == "nysa.software-factory.operator-receipt/v1"
        and result.get("ticket") == ticket and result.get("action") == action
        if trusted else result.get("project") == project
        and result.get("ticket") == ticket and result.get("status") == "pass"
    )
    if not valid:
        raise CliError(
            "operator result is invalid; mutation outcome is unknown; "
            "do not repeat; run factory doctor"
        )
    stdout.write(f"{ticket} updated.\n")


def register(target_id: str, launcher: str, project: str, targets_dir: Path) -> None:
    if not TARGET.fullmatch(target_id) or not PROJECT.fullmatch(project):
        raise CliError("target identity is invalid")
    candidate = _launcher(launcher)
    parent = directory = -1
    published = False
    retirements = []
    try:
        _trusted_launcher(candidate, project)
        parent, directory = _registry_lock(targets_dir)
        candidate.check()
        _checked_directory(targets_dir.parent, parent, "Factory preference directory")
        _checked_directory(targets_dir, directory, "Factory target directory")
        candidate_name = f"{target_id}.json"
        entries = set(os.listdir(directory))
        qualification = QUALIFICATION_LAUNCHER.fullmatch(str(candidate.path))
        if qualification is not None:
            for name in sorted(
                entry for entry in entries
                if entry.endswith(".json") and (
                    entry.startswith("qualification-")
                    or TARGET.fullmatch(entry[:-5])
                )
            ):
                raw, identity = _regular_at(
                    directory, name, "Factory target", 4096,
                )
                try:
                    value = json.loads(
                        raw,
                        object_pairs_hook=_unique,
                    )
                except (
                    CliError, UnicodeError, json.JSONDecodeError, RecursionError,
                ) as error:
                    if name == candidate_name:
                        continue
                    if name.startswith("qualification-"):
                        retirements.append((name, identity))
                        continue
                    raise CliError("qualification target is invalid") from error
                old = (
                    QUALIFICATION_LAUNCHER.fullmatch(str(value.get("launcher", "")))
                    if isinstance(value, dict)
                    and set(value) == {"launcher", "project"}
                    and isinstance(value.get("launcher"), str)
                    and isinstance(value.get("project"), str)
                    and PROJECT.fullmatch(value["project"])
                    else None
                )
                if old is None:
                    if name != candidate_name and name.startswith("qualification-"):
                        retirements.append((name, identity))
                    continue
                if name != candidate_name and (
                    old.group("root") == qualification.group("root")
                    or value.get("project") == project
                ):
                    retirements.append((name, identity))
        raw_candidate = (
            json.dumps(
                {"launcher": str(candidate.path), "project": project},
                sort_keys=True, separators=(",", ":"),
            ) + "\n"
        ).encode()
        _atomic_at(directory, candidate_name, raw_candidate)
        published = True
        _checked_directory(targets_dir.parent, parent, "Factory preference directory")
        _checked_directory(targets_dir, directory, "Factory target directory")
        candidate.check()
        if any(_identity_at(directory, name) != identity for name, identity in retirements):
            raise CliError("qualification target changed before retirement")
        for name, _ in retirements:
            os.unlink(name, dir_fd=directory)
        os.fsync(directory)
        _checked_directory(targets_dir.parent, parent, "Factory preference directory")
        _checked_directory(targets_dir, directory, "Factory target directory")
    except Exception as error:
        if published:
            raise CliError(
                "target registration outcome is unknown; rerun the same exact "
                "Factory preparation"
            ) from error
        raise
    finally:
        if directory >= 0:
            os.close(directory)
        if parent >= 0:
            os.close(parent)
        candidate.close()


def run(
    arguments: list[str], *, targets_dir: Path | None = None,
    selection_file: Path | None = None, stdin=sys.stdin, stdout=sys.stdout,
    stderr=sys.stderr,
) -> int:
    launcher = None
    try:
        command = arguments[0] if arguments else "next"
        if len(arguments) > 1 or command not in {"backlog", "doctor", "next", "use"}:
            raise CliError("usage: factory [use|backlog|doctor|next]")
        home = _account_home() if targets_dir is None or selection_file is None else None
        trusted = targets_dir is None
        targets = targets_dir or home / ".factory/targets"
        selection = selection_file or home / ".factory/current-target"
        try:
            _directory(targets, "target directory")
            _directory(selection.parent, "Factory preference directory")
        except CliError as error:
            if str(error).endswith("is unavailable"):
                raise CliError(
                    f"{error}; rerun the same supported Factory setup or "
                    "qualification preparation command"
                ) from error
            raise
        if command == "use":
            _use(targets, selection, stdin, stdout, trusted)
            return 0
        try:
            launcher, project = _selected(targets, selection, trusted)
        except CliError as error:
            if "run factory use" in str(error):
                raise
            raise CliError("selected target is unusable; run factory use") from error
        if command == "doctor":
            _doctor(launcher, project, stdout)
            return 0
        workflow = _workflow(launcher, project)
        if command == "backlog":
            _backlog(workflow, stdout)
        else:
            _next(launcher, project, workflow, stdin, stdout, trusted)
        return 0
    except (CliError, OSError, UnicodeError) as error:
        print(f"factory: {error}", file=stderr)
        return 2
    finally:
        if launcher is not None:
            launcher.close()


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "register":
        if len(sys.argv) != 5 or os.environ.get("FACTORY_INTERNAL_REGISTER") != "1":
            print("factory: internal registration is invalid", file=sys.stderr)
            return 2
        try:
            register(sys.argv[2], sys.argv[3], sys.argv[4], _account_home() / ".factory/targets")
            return 0
        except (CliError, OSError) as error:
            print(f"factory: {error}", file=sys.stderr)
            return 2
    return run(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
