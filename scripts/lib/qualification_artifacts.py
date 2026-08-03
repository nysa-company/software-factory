"""Retain and restore the exact historical role artifacts a passport names."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Any, Iterable


SCHEMA = "nysa.software-factory.qualification-artifact-retention/v1"
PASSPORT_SCHEMA = "nysa.software-factory.ticket-passport/v1"
DIGEST = re.compile(r"[0-9a-f]{64}\Z")
RUN_ID = re.compile(r"[A-Za-z0-9._-]{1,200}\Z")
TICKET = re.compile(r"T-[0-9]+\Z")
ROLE = re.compile(r"(?:planner|spec-linter|test-author|builder|reviewer|narrator)\Z")
LIMITS = {"meta": 131_072, "out": 8 * 1024 * 1024, "progress.jsonl": 10_000_000}


class ArtifactError(ValueError):
    pass


def canonical(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode()


def _read(path: Path, maximum: int, mode: int = 0o600) -> bytes:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as error:
        raise ArtifactError(f"unsafe artifact: {path.name}") from error
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) != mode
            or before.st_size > maximum
        ):
            raise ArtifactError(f"unsafe artifact: {path.name}")
        raw = b""
        while chunk := os.read(descriptor, min(1_048_576, maximum + 1 - len(raw))):
            raw += chunk
            if len(raw) > maximum:
                raise ArtifactError(f"oversized artifact: {path.name}")
        after = os.fstat(descriptor)
        if (
            before.st_dev, before.st_ino, before.st_size,
            before.st_mtime_ns, before.st_ctime_ns,
        ) != (
            after.st_dev, after.st_ino, after.st_size,
            after.st_mtime_ns, after.st_ctime_ns,
        ):
            raise ArtifactError(f"artifact changed while reading: {path.name}")
        return raw
    finally:
        os.close(descriptor)


def _directory(path: Path, create: bool = False) -> Path:
    if create and not path.exists() and not path.is_symlink():
        path.mkdir(mode=0o700)
    info = path.lstat()
    if (
        path.resolve(strict=True) != path
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise ArtifactError("artifact retention directory is unsafe")
    return path


def _write(path: Path, raw: bytes) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        parent = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        Path(temporary).unlink(missing_ok=True)


def _passport(state: Path, ticket: str) -> tuple[dict[str, Any], bytes]:
    secret = _read(state / "passport.key", 32)
    if len(secret) != 32:
        raise ArtifactError("passport authentication key is invalid")
    try:
        value = json.loads(
            _read(state / "passports" / f"{ticket}.json", 5_000_000)
        )
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ArtifactError(f"{ticket} passport is malformed") from error
    if not isinstance(value, dict) or value.get("schema") != PASSPORT_SCHEMA:
        raise ArtifactError(f"{ticket} passport is malformed")
    passport_digest = value.pop("passport_sha256", "")
    if passport_digest != hashlib.sha256(canonical(value)).hexdigest():
        raise ArtifactError(f"{ticket} passport digest is invalid")
    authentication = value.pop("authentication_sha256", "")
    if not hmac.compare_digest(
        authentication, hmac.new(secret, canonical(value), hashlib.sha256).hexdigest()
    ):
        raise ArtifactError(f"{ticket} passport authentication is invalid")
    value.update(
        authentication_sha256=authentication,
        passport_sha256=passport_digest,
    )
    return value, secret


def _manifest(raw: bytes) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeDecodeError as error:
        raise ArtifactError("run manifest is malformed") from error
    for line in lines:
        name, separator, value = line.partition("=")
        if not separator or not name or name in values:
            raise ArtifactError("run manifest is malformed")
        values[name] = value
    return values


def _requirements(passport: dict[str, Any], ticket: str) -> list[dict[str, Any]]:
    evidence = passport.get("completed_role_evidence")
    if (
        passport.get("ticket") != ticket
        or not isinstance(evidence, list)
        or any(not isinstance(item, dict) for item in evidence)
    ):
        raise ArtifactError(f"{ticket} passport role evidence is invalid")
    result = []
    seen = set()
    for item in evidence:
        run_id, role = item.get("run_id"), item.get("role")
        if (
            set(item) != {
                "contract_version", "factory_sha", "head_before",
                "manifest_sha256", "output_sha256", "role", "run_id",
                "transition_receipt_sha256",
            }
            or not RUN_ID.fullmatch(run_id or "")
            or not ROLE.fullmatch(role or "")
            or run_id in seen
            or any(
                not DIGEST.fullmatch(item.get(name, ""))
                for name in (
                    "manifest_sha256", "output_sha256",
                    "transition_receipt_sha256",
                )
            )
        ):
            raise ArtifactError(f"{ticket} passport role evidence is invalid")
        seen.add(run_id)
        result.append(item)
    return result


def _candidate(
    sources: Iterable[Path], run_id: str, kind: str, expected: str,
) -> bytes | None:
    values = []
    for source in sources:
        path = source / "factory/runs" / f"{run_id}.{kind}"
        if path.exists() or path.is_symlink():
            raw = _read(path, LIMITS[kind])
            if hashlib.sha256(raw).hexdigest() != expected:
                raise ArtifactError(f"{run_id} {kind} digest mismatch")
            values.append(raw)
    if not values:
        return None
    if any(value != values[0] for value in values[1:]):
        raise ArtifactError(f"{run_id} {kind} retention is ambiguous")
    return values[0]


def ensure_ticket(
    product: Path,
    state: Path,
    ticket: str,
    sources: Iterable[Path] = (),
) -> dict[str, int]:
    """Provision one authenticated ticket's exact historical artifact closure."""
    if not TICKET.fullmatch(ticket):
        raise ArtifactError("ticket identifier is invalid")
    passport_path = state / "passports" / f"{ticket}.json"
    if not passport_path.exists() and not passport_path.is_symlink():
        return {"artifacts": 0, "runs": 0}
    passport, secret = _passport(state, ticket)
    requirements = _requirements(passport, ticket)
    if not requirements:
        return {"artifacts": 0, "runs": 0}
    retention = _directory(state / "retained-runs", create=True)
    runs = product / "factory/runs"
    if runs.is_symlink() or not runs.is_dir() or runs.resolve(strict=True) != runs:
        raise ArtifactError("qualification run directory is unsafe")
    roots = (product, *tuple(sources))
    artifact_count = 0
    for item in requirements:
        run_id, role = item["run_id"], item["role"]
        retained_meta = retention / f"{run_id}.json"
        retained: dict[str, Any] | None = None
        if retained_meta.exists() or retained_meta.is_symlink():
            try:
                retained = json.loads(_read(retained_meta, 131_072))
            except (json.JSONDecodeError, UnicodeDecodeError) as error:
                raise ArtifactError(
                    f"{ticket} {run_id} retention metadata is invalid"
                ) from error
            if not isinstance(retained, dict):
                raise ArtifactError(
                    f"{ticket} {run_id} retention metadata is invalid"
                )
            authentication = retained.pop("authentication_sha256", "")
            if (
                retained.get("schema") != SCHEMA
                or retained.get("ticket") != ticket
                or retained.get("run_id") != run_id
                or retained.get("role") != role
                or not hmac.compare_digest(
                    authentication,
                    hmac.new(secret, canonical(retained), hashlib.sha256).hexdigest(),
                )
            ):
                raise ArtifactError(f"{ticket} {run_id} retention metadata is invalid")
        meta_raw = _candidate(roots, run_id, "meta", item["manifest_sha256"])
        if meta_raw is None and retained is not None:
            retained_manifest = retention / f"{run_id}.meta"
            if retained_manifest.exists() or retained_manifest.is_symlink():
                meta_raw = _read(retained_manifest, LIMITS["meta"])
                if hashlib.sha256(meta_raw).hexdigest() != item["manifest_sha256"]:
                    raise ArtifactError(
                        f"{ticket} {run_id} {role} retained meta mismatch"
                    )
        if meta_raw is None:
            raise ArtifactError(f"{ticket} {run_id} {role} missing meta")
        manifest = _manifest(meta_raw)
        if any((
            manifest.get("run_id") != run_id,
            manifest.get("ticket") != ticket,
            manifest.get("role") != role,
            manifest.get("kit_sha") != item.get("factory_sha"),
            manifest.get("contract_version") != item.get("contract_version"),
            manifest.get("role_head_before") != item.get("head_before"),
            manifest.get("transition_receipt_sha256")
            != item.get("transition_receipt_sha256"),
            manifest.get("exit_status") != "0",
            manifest.get("role_exit") != "ok",
            manifest.get("output_sha256") != item.get("output_sha256"),
        )):
            raise ArtifactError(f"{ticket} {run_id} {role} manifest identity mismatch")
        expected = {
            "meta": item["manifest_sha256"],
            "out": item["output_sha256"],
        }
        progress = manifest.get("progress_journal_sha256", "")
        events = manifest.get("progress_events", "")
        if progress or events:
            if (
                not DIGEST.fullmatch(progress)
                or not re.fullmatch(r"[1-9][0-9]{0,5}", events)
            ):
                raise ArtifactError(f"{ticket} {run_id} {role} progress identity mismatch")
            expected["progress.jsonl"] = progress
        files = {}
        for kind, digest in expected.items():
            raw = (
                meta_raw
                if kind == "meta"
                else _candidate(roots, run_id, kind, digest)
            )
            retained_path = retention / f"{run_id}.{kind}"
            if raw is None and retained_path.exists():
                raw = _read(retained_path, LIMITS[kind])
                if hashlib.sha256(raw).hexdigest() != digest:
                    raise ArtifactError(f"{ticket} {run_id} {role} retained {kind} mismatch")
            if raw is None:
                raise ArtifactError(f"{ticket} {run_id} {role} missing {kind}")
            if retained_path.exists() or retained_path.is_symlink():
                if _read(retained_path, LIMITS[kind]) != raw:
                    raise ArtifactError(f"{ticket} {run_id} {role} retained {kind} mismatch")
            else:
                _write(retained_path, raw)
            target = runs / f"{run_id}.{kind}"
            if target.exists() or target.is_symlink():
                if _read(target, LIMITS[kind]) != raw:
                    raise ArtifactError(f"{ticket} {run_id} {role} qualification {kind} mismatch")
            else:
                _write(target, raw)
            files[kind] = {
                "logical_path": f"factory/runs/{run_id}.{kind}",
                "sha256": digest,
                "size": len(raw),
            }
            artifact_count += 1
        record = {
            "files": files,
            "passport_sha256": passport["passport_sha256"],
            "role": role,
            "run_id": run_id,
            "schema": SCHEMA,
            "ticket": ticket,
        }
        record["authentication_sha256"] = hmac.new(
            secret, canonical(record), hashlib.sha256
        ).hexdigest()
        _write(retained_meta, canonical(record))
    return {"artifacts": artifact_count, "runs": len(requirements)}
