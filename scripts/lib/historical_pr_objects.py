"""Hydrate immutable Git objects named by committed terminal migrations."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import resource
import shutil
import subprocess
import tempfile
from typing import Any
from urllib.parse import urlsplit


SHA = re.compile(r"[0-9a-f]{40}\Z")
MAX_EVIDENCE_FILES = 512
MAX_EVIDENCE_BYTES = 1024 * 1024
MAX_TOTAL_EVIDENCE_BYTES = 16 * 1024 * 1024
MAX_OBJECTS = 512
MAX_OBJECT_BYTES = 8 * 1024 * 1024
MAX_FETCH_BYTES = 256 * 1024 * 1024
FETCH_CHUNK = 64


class HistoricalObjectError(ValueError):
    pass


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


def _git(product: Path, *arguments: str, environment: dict[str, str] | None = None,
         timeout: int = 120) -> subprocess.CompletedProcess[str]:
    command = [
        "git", "-c", "protocol.allow=never", "-c", "protocol.file.allow=always",
        "-c", "protocol.https.allow=always", "-c", "protocol.ssh.allow=always",
        "-c", "credential.interactive=never", "-C", str(product), *arguments,
    ]
    values = os.environ.copy()
    values.update({"GIT_PROTOCOL_FROM_USER": "0", "GIT_TERMINAL_PROMPT": "0"})
    if environment:
        values.update(environment)
    return subprocess.run(
        command, text=True, capture_output=True, check=False,
        timeout=timeout, env=values,
    )


def commit_present(product: Path, sha: str) -> bool:
    return _git(product, "cat-file", "-e", f"{sha}^{{commit}}").returncode == 0


def _blob_present(product: Path, sha: str) -> bool:
    result = _git(product, "cat-file", "-t", sha)
    return result.returncode == 0 and result.stdout.strip() == "blob"


def _json_at(product: Path, sha: str, path: str) -> dict[str, Any] | None:
    size = _git(product, "cat-file", "-s", f"{sha}:{path}")
    if size.returncode:
        return None
    try:
        length = int(size.stdout.strip())
    except ValueError as error:
        raise HistoricalObjectError(f"historical evidence size is invalid: {path}") from error
    if length > MAX_EVIDENCE_BYTES:
        raise HistoricalObjectError(f"historical evidence is too large: {path}")
    result = _git(product, "show", f"{sha}:{path}")
    if result.returncode or len(result.stdout.encode()) != length:
        raise HistoricalObjectError(f"historical evidence is unavailable: {path}")
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise HistoricalObjectError(f"historical evidence is malformed: {path}") from error
    if not isinstance(value, dict):
        raise HistoricalObjectError(f"historical evidence is malformed: {path}")
    return value


def _object_root(product: Path) -> Path:
    result = _git(product, "rev-parse", "--git-path", "objects")
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


def _fetch_objects(
    product: Path, origin: str, commits: set[str], blobs: set[str],
) -> None:
    expected = {**{value: "commit" for value in commits}, **{value: "blob" for value in blobs}}
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
            objects = Path(raw) / "objects"
            (objects / "info").mkdir(parents=True)
            (objects / "pack").mkdir()
            remaining = MAX_FETCH_BYTES - imported
            if remaining <= 0:
                raise HistoricalObjectError("historical evidence fetch exceeds its quota")

            def limit() -> None:
                resource.setrlimit(resource.RLIMIT_FSIZE, (remaining, remaining))

            environment = {
                "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(target),
                "GIT_OBJECT_DIRECTORY": str(objects),
            }
            command = [
                "git", "-c", "protocol.allow=never", "-c", "protocol.file.allow=always",
                "-c", "protocol.https.allow=always", "-c", "protocol.ssh.allow=always",
                "-c", "fetch.fsckObjects=true", "-c", "transfer.fsckObjects=true",
                "-c", "credential.interactive=never", "-C", str(product),
                "fetch", "--quiet", "--no-tags", "--no-write-fetch-head",
                origin, *chunk,
            ]
            values = os.environ.copy()
            values.update(environment)
            values.update({"GIT_PROTOCOL_FROM_USER": "0", "GIT_TERMINAL_PROMPT": "0"})
            fetched = subprocess.run(
                command, text=True, capture_output=True, check=False, timeout=120,
                env=values, preexec_fn=limit,
            )
            if fetched.returncode:
                raise HistoricalObjectError("historical evidence object fetch failed")
            size = _directory_bytes(objects)
            if size > remaining:
                raise HistoricalObjectError("historical evidence fetch exceeds its quota")
            for value in chunk:
                result = _git(product, "cat-file", "-t", value, environment=environment)
                length = _git(product, "cat-file", "-s", value, environment=environment)
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
    supported = {
        "nysa.software-factory.legacy-closeout/v1": ("pr",),
        "nysa.software-factory.terminal-backfill/v1": (
            "implementation_pr", "closeout_pr",
        ),
        "nysa.software-factory.protected-merge-reconciliation/v1": (
            "original_pr", "adoption_pr",
        ),
    }
    repository = _repository(product)
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
            observed = _git(product, "ls-remote", "--refs", "--", origin, reference)
            fields = observed.stdout.split()
            relative = sorted(item["paths"])[0]
            if observed.returncode or fields != [head, reference]:
                raise HistoricalObjectError(
                    f"historical PR head unavailable: {relative} PR #{number} expected {head}"
                )
    _fetch_objects(product, origin, requirement_commits, set())
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
            if sha != head and _git(
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
    listing = _git(
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
        if not re.fullmatch(r"factory/attestations/T-[0-9]+/bundle\.json", path):
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
                commit = value.get(key, "")
                if not SHA.fullmatch(commit):
                    raise HistoricalObjectError(
                        f"historical evidence commit is malformed: {evidence_path} {key}"
                    )
                direct.add(commit)
            for key in blob_keys:
                blob = value.get(key, "")
                if not SHA.fullmatch(blob):
                    raise HistoricalObjectError(
                        f"historical evidence blob is malformed: {evidence_path} {key}"
                    )
                blobs.add(blob)
    if len(requirement_commits | direct | blobs) > MAX_OBJECTS:
        raise HistoricalObjectError("historical evidence object inventory is too large")
    _fetch_objects(product, origin, direct, blobs)
    return len(requirements)
