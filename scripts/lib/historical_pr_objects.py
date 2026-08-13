"""Hydrate immutable Git objects named by committed terminal migrations."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import resource
import shutil
import stat
import subprocess
import tempfile
from typing import Any
from urllib.parse import urlsplit

from legacy_closeout import _git_object, _git_object_info


SHA = re.compile(r"[0-9a-f]{40}\Z")
MAX_EVIDENCE_FILES = 512
MAX_EVIDENCE_BYTES = 1024 * 1024
MAX_TOTAL_EVIDENCE_BYTES = 16 * 1024 * 1024
MAX_OBJECTS = 512
MAX_OBJECT_BYTES = 8 * 1024 * 1024
MAX_FETCH_BYTES = 256 * 1024 * 1024
FETCH_CHUNK = 64
GITHUB_CLI_CANDIDATES = tuple(map(Path, (
    "/opt/homebrew/bin/gh", "/usr/local/bin/gh", "/usr/bin/gh",
)))


class HistoricalObjectError(ValueError):
    pass


def github_auth(origin: str) -> tuple[str, str] | None:
    try:
        parsed = urlsplit(origin)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme != "https" or parsed.hostname != "github.com"
        or port is not None or parsed.username is not None
        or parsed.password is not None
        or parsed.query or parsed.fragment
    ):
        return None
    helper = next((
        resolved
        for candidate in GITHUB_CLI_CANDIDATES
        for resolved in (candidate.resolve(),)
        if candidate.exists() and resolved.is_file() and os.access(resolved, os.X_OK)
    ), None)
    home = Path(os.environ.get("HOME", ""))
    if helper is None or not home.is_absolute():
        return None
    config_parent = home / ".config"
    config = config_parent / "gh"
    hosts = config / "hosts.yml"
    try:
        helper_metadata = helper.lstat()
        helper_parent = helper.parent.lstat()
        if (
            helper.resolve() != helper or not stat.S_ISREG(helper_metadata.st_mode)
            or helper_metadata.st_nlink != 1
            or helper_metadata.st_uid not in {0, os.geteuid()}
            or stat.S_IMODE(helper_metadata.st_mode) & 0o022
            or not os.access(helper, os.X_OK)
            or not re.fullmatch(r"/[A-Za-z0-9_./+-]+", str(helper))
            or not stat.S_ISDIR(helper_parent.st_mode)
            or helper_parent.st_uid not in {0, os.geteuid()}
            or stat.S_IMODE(helper_parent.st_mode) & 0o022
        ):
            raise OSError
        for directory in (home, config_parent, config):
            metadata = directory.lstat()
            if (
                directory.is_symlink() or not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) & 0o022
            ):
                raise OSError
        metadata = hosts.lstat()
        if (
            hosts.is_symlink() or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid() or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) & 0o077
        ):
            raise OSError
    except OSError:
        return None
    return str(helper), str(config)


def _repository(product: Path) -> str:
    descriptor = product / "factory/PROJECT.env"
    if descriptor.is_symlink() or descriptor.stat().st_size > MAX_EVIDENCE_BYTES:
        raise HistoricalObjectError("historical product descriptor is unsafe")
    values = re.findall(
        r"^(?:export\s+)?GH_REPO\s*=\s*['\"]?([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)['\"]?\s*$",
        descriptor.read_text(encoding="utf-8"),
        re.M,
    )
    if len(values) != 1:
        raise HistoricalObjectError("historical product repository is ambiguous")
    return values[0]


def _transport(origin: str) -> str:
    if not origin or any(character in origin for character in "\x00\r\n\t"):
        raise HistoricalObjectError("historical product origin is unsafe")
    if origin.startswith("/"):
        path = Path(origin).resolve(strict=True)
        if not path.is_dir():
            raise HistoricalObjectError("historical local product origin is unavailable")
        return str(path)
    if origin.startswith("file://"):
        parsed = urlsplit(origin)
        if parsed.netloc or parsed.query or parsed.fragment:
            raise HistoricalObjectError("historical local product origin is unsafe")
        path = Path(parsed.path).resolve(strict=True)
        if not path.is_dir():
            raise HistoricalObjectError("historical local product origin is unavailable")
        return "file://" + str(path)
    if origin.startswith(("https://", "ssh://")):
        parsed = urlsplit(origin)
        if not parsed.hostname or parsed.password is not None:
            raise HistoricalObjectError("historical product origin is unsafe")
        return origin
    if re.fullmatch(
        r"(?:[A-Za-z0-9][A-Za-z0-9._-]*@)?"
        r"[A-Za-z0-9][A-Za-z0-9._-]*:[A-Za-z0-9._/~+-]+",
        origin,
    ):
        return origin
    raise HistoricalObjectError("historical product origin uses an unsafe transport")


def _git_environment(
    overrides: dict[str, str] | None = None,
    auth: tuple[str, str] | None = None,
) -> dict[str, str]:
    values = {
        "GIT_ASKPASS": "/usr/bin/false",
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_CONFIG": os.devnull,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_PROTOCOL_FROM_USER": "0",
        "GIT_SSH_COMMAND": "/usr/bin/ssh -F /dev/null -oBatchMode=yes",
        "GIT_TERMINAL_PROMPT": "0",
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": "/usr/bin:/bin",
        "SSH_ASKPASS": "/usr/bin/false",
    }
    for name in ("HOME", "SSH_AUTH_SOCK", "TMPDIR"):
        if os.environ.get(name):
            values[name] = os.environ[name]
    if overrides:
        allowed = {"GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_OBJECT_DIRECTORY"}
        if set(overrides) - allowed:
            raise HistoricalObjectError("historical Git environment override is unsafe")
        values.update(overrides)
    if auth:
        values.update({"GH_CONFIG_DIR": auth[1], "GH_PROMPT_DISABLED": "1"})
    return values


def _git_command(
    product: Path | None, *arguments: str, auth: tuple[str, str] | None = None,
) -> list[str]:
    command = [
        "/usr/bin/git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null",
        "-c", "credential.helper=", "-c", "credential.interactive=never",
        "-c", "core.askPass=/usr/bin/false", "-c", "diff.external=",
        "-c", "interactive.diffFilter=", "-c", "protocol.allow=never",
        "-c", "protocol.file.allow=always",
        "-c", "protocol.https.allow=always", "-c", "protocol.ssh.allow=always",
        "-c", "core.sshCommand=/usr/bin/ssh -F /dev/null -oBatchMode=yes",
    ]
    if auth:
        command.extend((
            "-c",
            f"credential.https://github.com.helper=!{auth[0]} auth git-credential",
        ))
    command.extend(("-C", str(product)) if product is not None else ("-C", "/"))
    return [*command, *arguments]


def run_git(
    product: Path, *arguments: str, environment: dict[str, str] | None = None,
    timeout: int = 120,
) -> subprocess.CompletedProcess[str]:
    value = None
    output = None
    object_revision = (
        len(arguments) == 2 and arguments[0] == "rev-parse"
        and not arguments[1].startswith("-")
    ) or (
        len(arguments) == 3 and arguments[:2] == ("rev-parse", "--verify")
    )
    if environment is None:
        if len(arguments) == 2 and arguments[0] == "show" and ":" in arguments[1]:
            value = _git_object(product, arguments[1])
            output = value[2].decode() if value is not None and value[1] == "blob" else None
        elif len(arguments) == 3 and arguments[:2] in {
            ("cat-file", "-e"), ("cat-file", "-s"), ("cat-file", "-t"),
        }:
            value = _git_object_info(product, arguments[2])
            if value is not None:
                output = {
                    "-e": "", "-s": str(value[2]) + "\n",
                    "-t": value[1] + "\n",
                }[arguments[1]]
        elif object_revision:
            value = _git_object_info(product, arguments[-1])
            output = value[0] + "\n" if value is not None else None
    eligible = (
        len(arguments) == 2 and arguments[0] == "show" and ":" in arguments[1]
        or len(arguments) == 3 and arguments[:2] in {
            ("cat-file", "-e"), ("cat-file", "-s"), ("cat-file", "-t"),
        }
        or object_revision
    )
    if environment is None and eligible:
        return subprocess.CompletedProcess(
            arguments, 0 if output is not None else 1, output or "", "",
        )
    return subprocess.run(
        _git_command(product, *arguments), text=True, capture_output=True, check=False,
        timeout=timeout, env=_git_environment(environment),
    )


def run_git_remote(
    *arguments: str, environment: dict[str, str] | None = None,
    auth: tuple[str, str] | None = None, timeout: int = 120,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        _git_command(None, *arguments, auth=auth), text=True,
        capture_output=True, check=False, timeout=timeout,
        env=_git_environment(environment, auth),
    )


def commit_present(product: Path, sha: str) -> bool:
    return run_git(product, "cat-file", "-e", f"{sha}^{{commit}}").returncode == 0


def _blob_present(product: Path, sha: str) -> bool:
    result = run_git(product, "cat-file", "-t", sha)
    return result.returncode == 0 and result.stdout.strip() == "blob"


def _json_at(product: Path, sha: str, path: str) -> dict[str, Any] | None:
    size = run_git(product, "cat-file", "-s", f"{sha}:{path}")
    if size.returncode:
        return None
    try:
        length = int(size.stdout.strip())
    except ValueError as error:
        raise HistoricalObjectError(f"historical evidence size is invalid: {path}") from error
    if length > MAX_EVIDENCE_BYTES:
        raise HistoricalObjectError(f"historical evidence is too large: {path}")
    result = run_git(product, "show", f"{sha}:{path}")
    if result.returncode or len(result.stdout.encode()) != length:
        raise HistoricalObjectError(f"historical evidence is unavailable: {path}")
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise HistoricalObjectError(f"historical evidence is malformed: {path}") from error
    if not isinstance(value, dict):
        raise HistoricalObjectError(f"historical evidence is malformed: {path}")
    return value


def _done_at(product: Path, ticket: str) -> bool:
    path = f"factory/tickets/{ticket}.md"
    size = run_git(product, "cat-file", "-s", f"HEAD:{path}")
    try:
        length = int(size.stdout.strip()) if not size.returncode else 0
    except ValueError:
        return False
    if not 0 < length <= MAX_EVIDENCE_BYTES:
        return False
    result = run_git(product, "show", f"HEAD:{path}")
    states = re.findall(r"(?mi)^State:\s*(.*?)\s*$", result.stdout)
    return (
        result.returncode == 0 and len(result.stdout.encode()) == length
        and len(states) == 1 and states[0].strip().lower() == "done"
    )


def _object_root(product: Path) -> Path:
    result = run_git(product, "rev-parse", "--git-path", "objects")
    if result.returncode or not result.stdout.strip():
        raise HistoricalObjectError("historical Git object root is unavailable")
    value = Path(result.stdout.strip())
    if not value.is_absolute():
        value = product / value
    root = value.resolve(strict=True)
    if not root.is_dir() or root.is_symlink():
        raise HistoricalObjectError("historical Git object root is unsafe")
    return root


def _directory_bytes(root: Path) -> int:
    total = 0
    for path in root.rglob("*"):
        if path.is_symlink():
            raise HistoricalObjectError("historical fetched object path is unsafe")
        if path.is_file():
            total += path.stat().st_size
    return total


def _copy_objects(source: Path, target: Path) -> None:
    loose = re.compile(r"[0-9a-f]{2}/[0-9a-f]{38}")
    packed = re.compile(r"pack/pack-[0-9a-f]{40}\.(?:pack|idx|rev)")
    for path in sorted(source.rglob("*")):
        if not path.is_file():
            continue
        relative = str(path.relative_to(source))
        if not loose.fullmatch(relative) and not packed.fullmatch(relative):
            raise HistoricalObjectError("historical fetched object layout is unsafe")
        destination = target / relative
        destination.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        if destination.exists():
            continue
        temporary = destination.with_name(destination.name + f".factory-{os.getpid()}")
        shutil.copyfile(path, temporary)
        os.chmod(temporary, 0o444)
        os.replace(temporary, destination)


def fetch_objects(
    product: Path, origin: str, commits: set[str], blobs: set[str],
) -> None:
    origin = _transport(origin)
    auth = github_auth(origin)
    expected = {**{value: "commit" for value in commits}, **{value: "blob" for value in blobs}}
    if any(not SHA.fullmatch(value) for value in expected):
        raise HistoricalObjectError("historical evidence object ID is invalid")
    missing = sorted(
        value for value, kind in expected.items()
        if not (commit_present(product, value) if kind == "commit" else _blob_present(product, value))
    )
    if not missing:
        return
    if len(expected) > MAX_OBJECTS:
        raise HistoricalObjectError("historical evidence object inventory is too large")
    target = _object_root(product)
    imported = 0
    for offset in range(0, len(missing), FETCH_CHUNK):
        chunk = missing[offset:offset + FETCH_CHUNK]
        with tempfile.TemporaryDirectory(prefix="factory-history-", dir=str(target.parent)) as raw:
            repository = Path(raw) / "fetch.git"
            initialized = subprocess.run(
                _git_command(
                    None, "init", "--bare", "--quiet", str(repository),
                ),
                text=True, capture_output=True, check=False, timeout=30,
                env=_git_environment(),
            )
            if initialized.returncode:
                raise HistoricalObjectError("historical evidence staging failed")
            objects = repository / "objects"
            remaining = MAX_FETCH_BYTES - imported
            if remaining <= 0:
                raise HistoricalObjectError("historical evidence fetch exceeds its quota")

            def limit() -> None:
                resource.setrlimit(resource.RLIMIT_FSIZE, (remaining, remaining))

            environment = {
                "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(target),
            }
            command = _git_command(
                None, f"--git-dir={repository}", "-c", "fetch.fsckObjects=true",
                "-c", "transfer.fsckObjects=true",
                "-c", "gc.auto=0", "-c", "maintenance.auto=false",
                "fetch", "--quiet", "--no-tags", "--no-write-fetch-head",
                origin, *chunk,
                auth=auth,
            )
            fetched = subprocess.run(
                command, text=True, capture_output=True, check=False, timeout=120,
                env=_git_environment(environment, auth), preexec_fn=limit,
            )
            if fetched.returncode:
                raise HistoricalObjectError("historical evidence object fetch failed")
            size = _directory_bytes(objects)
            if size > remaining:
                raise HistoricalObjectError("historical evidence fetch exceeds its quota")
            for value in chunk:
                result = run_git_remote(
                    f"--git-dir={repository}", "cat-file", "-t", value,
                    environment=environment, auth=auth,
                )
                length = run_git_remote(
                    f"--git-dir={repository}", "cat-file", "-s", value,
                    environment=environment, auth=auth,
                )
                if (
                    result.returncode or result.stdout.strip() != expected[value]
                    or length.returncode
                ):
                    raise HistoricalObjectError("historical evidence object type is invalid")
                try:
                    object_size = int(length.stdout.strip())
                except ValueError as error:
                    raise HistoricalObjectError(
                        "historical evidence object size is invalid"
                    ) from error
                if object_size > MAX_OBJECT_BYTES:
                    raise HistoricalObjectError("historical evidence object is too large")
            _copy_objects(objects, target)
            imported += size
    absent = [
        value for value, kind in expected.items()
        if not (commit_present(product, value) if kind == "commit" else _blob_present(product, value))
    ]
    if absent:
        raise HistoricalObjectError("historical evidence object import failed")


def hydrate(product: Path, origin: str) -> int:
    migrations = product / "factory/migrations"
    origin = _transport(origin)
    auth = github_auth(origin)
    supported = {
        "nysa.software-factory.legacy-closeout/v1": ("pr",),
        "nysa.software-factory.terminal-backfill/v1": (
            "implementation_pr", "closeout_pr",
        ),
        "nysa.software-factory.protected-merge-reconciliation/v1": (
            "original_pr", "adoption_pr",
        ),
    }
    repository: str | None = None
    requirements: dict[tuple[int, str], dict[str, Any]] = {}
    reconciliation: list[tuple[str, str, str, dict[str, Any]]] = []
    direct: set[str] = set()
    blobs: set[str] = set()
    migration_paths = (
        sorted(migrations.glob("**/*.json")) if migrations.is_dir() else []
    )
    if len(migration_paths) > MAX_EVIDENCE_FILES:
        raise HistoricalObjectError("historical migration inventory is too large")
    migration_bytes = 0
    for path in migration_paths:
        if path.is_symlink() or not path.is_file():
            raise HistoricalObjectError(
                f"historical object record is unsafe: {path.relative_to(product)}"
            )
        size = path.stat().st_size
        migration_bytes += size
        if size > MAX_EVIDENCE_BYTES or migration_bytes > MAX_TOTAL_EVIDENCE_BYTES:
            raise HistoricalObjectError("historical migration evidence is too large")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise HistoricalObjectError(
                f"historical object record is malformed: {path.relative_to(product)}"
            ) from error
        keys = supported.get(value.get("schema")) if isinstance(value, dict) else None
        if not keys:
            continue
        if repository is None:
            repository = _repository(product)
        relative = str(path.relative_to(product))
        if value.get("repository") != repository:
            raise HistoricalObjectError(
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
                raise HistoricalObjectError(
                    f"historical PR record is malformed: {relative} {key}"
                )
            identity = record["number"], record["head"]
            item = requirements.setdefault(identity, {"commits": set(), "paths": set()})
            item["commits"].add(record["head"])
            item["paths"].add(relative)
            merge_commit = record.get("merge_commit")
            if merge_commit is not None:
                if not SHA.fullmatch(merge_commit):
                    raise HistoricalObjectError(
                        f"historical PR merge commit is malformed: {relative} {key}"
                    )
                direct.add(merge_commit)
            if (
                value.get("schema")
                == "nysa.software-factory.protected-merge-reconciliation/v1"
                and key == "original_pr"
            ):
                evidence = value.get("evidence_head", "")
                if not SHA.fullmatch(evidence):
                    raise HistoricalObjectError(
                        f"historical evidence head is malformed: {relative}"
                    )
                item["commits"].add(evidence)
                reconciliation.append((relative, path.stem, evidence, value))

    requirement_commits = set()
    for (number, head), item in sorted(requirements.items()):
        requirement_commits.update(item["commits"])
        if any(not commit_present(product, sha) for sha in item["commits"]):
            reference = f"refs/pull/{number}/head"
            observed = run_git_remote(
                "ls-remote", "--refs", "--", origin, reference, auth=auth,
            )
            fields = observed.stdout.split()
            relative = sorted(item["paths"])[0]
            if observed.returncode or fields != [head, reference]:
                raise HistoricalObjectError(
                    f"historical PR head unavailable: {relative} PR #{number} expected {head}"
                )
    fetch_objects(product, origin, requirement_commits, set())
    for (number, head), item in sorted(requirements.items()):
        absent = sorted(
            sha for sha in item["commits"] if not commit_present(product, sha)
        )
        if absent:
            raise HistoricalObjectError(
                f"historical commit object missing: {sorted(item['paths'])[0]} "
                f"PR #{number} expected {absent[0]}"
            )
        for sha in item["commits"]:
            if sha != head and run_git(
                product, "merge-base", "--is-ancestor", sha, head,
            ).returncode:
                raise HistoricalObjectError(
                    f"historical commit is not in PR: {sorted(item['paths'])[0]} "
                    f"PR #{number} expected {sha}"
                )

    for relative, ticket, evidence, receipt in reconciliation:
        legacy = receipt.get("legacy_review")
        if legacy is not None:
            if not isinstance(legacy, dict):
                raise HistoricalObjectError(
                    f"historical legacy review is malformed: {relative}"
                )
            for key in ("reviewed_sha", "verdict_commit"):
                value = legacy.get(key, "")
                if not SHA.fullmatch(value):
                    raise HistoricalObjectError(
                        f"historical legacy review is malformed: {relative} {key}"
                    )
                direct.add(value)
        for name, keys in (
            ("bundle.json", ("branch_head", "reviewed_sha")),
            ("approval.json", ("parent_head", "reviewed_sha")),
        ):
            evidence_path = f"factory/attestations/{ticket}/{name}"
            value = _json_at(product, evidence, evidence_path)
            if value is None:
                continue
            for key in keys:
                commit = value.get(key, "")
                if not SHA.fullmatch(commit):
                    raise HistoricalObjectError(
                        f"historical evidence commit is malformed: {evidence_path} {key}"
                    )
                direct.add(commit)
    listing = run_git(
        product, "ls-tree", "-r", "--name-only", "HEAD",
        "--", "factory/attestations",
    )
    if listing.returncode:
        raise HistoricalObjectError("historical attestation inventory is unavailable")
    attestation_paths = listing.stdout.splitlines()
    if (
        len(attestation_paths) > MAX_EVIDENCE_FILES * 4
        or len(listing.stdout.encode()) > MAX_TOTAL_EVIDENCE_BYTES
    ):
        raise HistoricalObjectError("historical attestation inventory is too large")
    for path in attestation_paths:
        matched = re.fullmatch(
            r"factory/attestations/(T-[0-9]+)/bundle\.json", path,
        )
        if not matched or not _done_at(product, matched.group(1)):
            continue
        root = str(Path(path).parent)
        for name, commit_keys, blob_keys in (
            ("bundle.json", ("branch_head", "reviewed_sha"), ("route_plan_blob",)),
            ("approval.json", ("parent_head", "reviewed_sha"), ()),
            (
                "done.json",
                (
                    "approved_pr_head", "reviewed_sha", "merge_commit",
                    "approval_parent_head", "closeout_parent",
                ),
                (),
            ),
        ):
            evidence_path = f"{root}/{name}"
            value = _json_at(product, "HEAD", evidence_path)
            if value is None:
                continue
            for key in commit_keys:
                if key not in value:
                    continue
                commit = value.get(key, "")
                if not SHA.fullmatch(commit):
                    raise HistoricalObjectError(
                        f"historical evidence commit is malformed: {evidence_path} {key}"
                    )
                direct.add(commit)
            for key in blob_keys:
                if key not in value:
                    continue
                blob = value.get(key, "")
                if not SHA.fullmatch(blob):
                    raise HistoricalObjectError(
                        f"historical evidence blob is malformed: {evidence_path} {key}"
                    )
                blobs.add(blob)
    if len(requirement_commits | direct | blobs) > MAX_OBJECTS:
        raise HistoricalObjectError("historical evidence object inventory is too large")
    fetch_objects(product, origin, direct, blobs)
    return len(requirements)
