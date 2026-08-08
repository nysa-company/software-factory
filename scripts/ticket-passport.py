#!/usr/bin/env python3
"""Export and validate lane-neutral Contract 1.8 ticket passports."""

from __future__ import annotations

import argparse
import fcntl
from decimal import Decimal, InvalidOperation
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
from release_lineage import (  # noqa: E402
    successor_release_lineage, valid_v2_migration,
)
from reorder_test_fixes import (  # noqa: E402
    verified_history_repair, verified_normalization_plan,
)
from role_output import RoleOutputError, sha256 as role_output_sha256
from cursor_model_identity import approved_reported_models
from failed_attempt_handoff import (  # noqa: E402
    HandoffError, RoleBoundaryPolicy, _validate_committed_changes,
)
from route_evidence import (  # noqa: E402
    RouteEvidenceError, exact_kit_sha_change, journal_extends, validate_route,
)


SCHEMA = "nysa.software-factory.ticket-passport/v1"
RECEIPT_SCHEMA = "nysa.software-factory.transition-receipt/v1"
TICKET = re.compile(r"^T-[0-9]+$")
SHA = re.compile(r"^[0-9a-f]{40}$")
DIGEST = re.compile(r"^[0-9a-f]{64}$")
TERMINAL_ACCOUNTING = {
    "completed", "abandoned_conservative", "cancelled", "cancelled_conservative",
}
INFLIGHT_SCHEMA = "nysa.software-factory.inflight-release-authorization/v1"
REWRITE_SCHEMA = "nysa.software-factory.ticket-rewrite-authorization/v1"
NORMALIZATION_SCHEMA = (
    "nysa.software-factory.ticket-history-normalization-authorization/v1"
)
HISTORY_REPAIR_SCHEMA = (
    "nysa.software-factory.ticket-history-repair-authorization/v1"
)
MIGRATION_SCHEMA = "nysa.software-factory.ticket-passport-migration/v2"
LINEAGE_SCHEMA = "nysa.software-factory.ticket-passport-lineage-authorization/v1"
COMPLETION_CORRECTION_SCHEMA = (
    "nysa.software-factory.completed-role-correction/v1"
)
PASSPORTLESS_MODEL_CORRECTION_SCHEMA = (
    "nysa.software-factory.completed-role-correction/v2"
)
COMPLETION_CORRECTION_ISSUE = (
    "https://github.com/nysa-company/software-factory/issues/218"
)
MODEL_IDENTITY_CORRECTION_ISSUE = (
    "https://github.com/nysa-company/software-factory/issues/390"
)
RECOVERABLE_ROLES = {
    "planner", "spec-linter", "test-author", "builder", "reviewer", "narrator",
}
RUN_ID = re.compile(r"^[A-Za-z0-9._-]{1,200}$")
INFLIGHT_STATES = {
    "Ready", "Planning", "Building", "Review", "Awaiting Approval", "Approved",
    "Blocked-Escalated",
}


class PassportError(ValueError):
    pass


def role_output_digest(path: Path) -> str:
    try:
        return role_output_sha256(path)
    except (OSError, RoleOutputError) as error:
        raise PassportError(str(error)) from None


def canonical(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode()


def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value = {}
    for name, item in pairs:
        if name in value:
            raise ValueError("duplicate key")
        value[name] = item
    return value


def git(root: Path, *arguments: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *arguments],
        text=True,
        capture_output=True,
        check=False,
        timeout=120,
    )
    if check and result.returncode:
        raise PassportError(result.stderr.strip() or "Git operation failed")
    return result.stdout.strip()


def safe_directory(path: Path, create: bool = False) -> Path:
    if create:
        path.mkdir(mode=0o700, parents=False, exist_ok=True)
    info = path.lstat()
    if (
        not path.is_absolute()
        or path.resolve(strict=True) != path
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise PassportError("passport directory is unsafe")
    return path


def read_regular(path: Path, mode: int | None = None, maximum: int = 5_000_000) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
            or (mode is not None and stat.S_IMODE(info.st_mode) != mode)
            or info.st_size > maximum
        ):
            raise PassportError(f"unsafe file: {path.name}")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            return stream.read()
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def write_atomic(path: Path, value: dict[str, Any]) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(canonical(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        Path(temporary).unlink(missing_ok=True)


def key(state_dir: Path) -> bytes:
    path = state_dir / "passport.key"
    lock = os.open(
        state_dir / ".passport-key.lock",
        os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        info = os.fstat(lock)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise PassportError("passport key lock is unsafe")
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not path.exists() and not path.is_symlink():
            descriptor, temporary = tempfile.mkstemp(
                prefix=".passport.key.", dir=state_dir
            )
            try:
                os.fchmod(descriptor, 0o600)
                with os.fdopen(descriptor, "wb") as stream:
                    descriptor = -1
                    stream.write(secrets.token_bytes(32))
                    stream.flush()
                    os.fsync(stream.fileno())
                try:
                    os.link(temporary, path)
                except FileExistsError:
                    pass
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
                Path(temporary).unlink(missing_ok=True)
        raw = read_regular(path, 0o600, 32)
        if len(raw) != 32:
            raise PassportError("passport authentication key is invalid")
        return raw
    finally:
        os.close(lock)


def authenticate(value: dict[str, Any], secret: bytes) -> dict[str, Any]:
    body = dict(value)
    body["authentication_sha256"] = hmac.new(
        secret, canonical(value), hashlib.sha256
    ).hexdigest()
    body["passport_sha256"] = hashlib.sha256(canonical(body)).hexdigest()
    return body


def load_passport(path: Path, secret: bytes) -> tuple[dict[str, Any], str]:
    raw = read_regular(path, 0o600)
    value = json.loads(raw)
    if not isinstance(value, dict) or value.get("schema") != SCHEMA:
        raise PassportError("passport is malformed")
    passport_digest = value.pop("passport_sha256", "")
    if passport_digest != hashlib.sha256(canonical(value)).hexdigest():
        raise PassportError("passport digest is invalid")
    authentication = value.pop("authentication_sha256", "")
    if not hmac.compare_digest(
        authentication, hmac.new(secret, canonical(value), hashlib.sha256).hexdigest()
    ):
        raise PassportError("passport authentication is invalid")
    value["authentication_sha256"] = authentication
    value["passport_sha256"] = passport_digest
    return value, hashlib.sha256(raw).hexdigest()


def manifest_fields(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in read_regular(path).decode("utf-8").splitlines():
        key_name, separator, value = line.partition("=")
        if not separator or not key_name or key_name in values:
            raise PassportError(f"run manifest is malformed: {path.name}")
        values[key_name] = value
    return values


def micro_usd(value: dict[str, str]) -> int:
    raw = value.get("effective_cost") or value.get("reserved_usd") or "0"
    try:
        amount = Decimal(raw)
    except InvalidOperation as error:
        raise PassportError("run charge is invalid") from error
    if amount < 0 or amount.as_tuple().exponent < -6:
        raise PassportError("run charge is invalid")
    return int(amount * 1_000_000)


def _run_evidence(
    factory: Path, ticket: str, validate_outputs: bool
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    completed, charges = [], []
    runs = factory / "runs"
    if not runs.exists():
        return completed, charges
    for path in sorted(runs.glob("*.meta")):
        value = manifest_fields(path)
        if value.get("ticket") != ticket or value.get("accounting_state") not in TERMINAL_ACCOUNTING:
            continue
        run_id = value.get("run_id", "")
        if not run_id or any(item["run_id"] == run_id for item in charges):
            raise PassportError("run evidence identity is ambiguous")
        manifest_digest = hashlib.sha256(read_regular(path)).hexdigest()
        charges.append({
            "accounting_state": value["accounting_state"],
            "charge_micro_usd": micro_usd(value),
            "contract_version": value.get("contract_version"),
            "factory_sha": value.get("kit_sha"),
            "head_before": value.get("role_head_before"),
            "manifest_sha256": manifest_digest,
            "role": value.get("role"),
            "run_id": run_id,
            "transition_receipt_sha256": value.get(
                "transition_receipt_sha256"
            ),
        })
        if (
            validate_outputs
            and value.get("exit_status") == "0"
            and value.get("role_exit") == "ok"
        ):
            output = path.with_suffix(".out")
            output_digest = role_output_digest(output)
            if value.get("output_sha256") != output_digest:
                raise PassportError("successful role output digest does not match")
            completed.append({
                "contract_version": value.get("contract_version"),
                "factory_sha": value.get("kit_sha"),
                "head_before": value.get("role_head_before"),
                "manifest_sha256": manifest_digest,
                "output_sha256": output_digest,
                "role": value.get("role"),
                "run_id": run_id,
                "transition_receipt_sha256": value.get(
                    "transition_receipt_sha256"
                ),
            })
    return completed, charges


def run_charges(factory: Path, ticket: str) -> list[dict[str, Any]]:
    return _run_evidence(factory, ticket, False)[1]


def run_evidence(
    factory: Path, ticket: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    return _run_evidence(factory, ticket, True)


def successful_progress(path: Path) -> tuple[int, str]:
    raw = read_regular(path, 0o600, 10_000_000)
    if not raw or not raw.endswith(b"\n"):
        raise PassportError("successful progress journal is incomplete")
    previous_observed = -1
    successful = 0
    latest = ("", "")
    allowed = {
        ("assistant", ""),
        ("result", "success"),
        ("system", "init"),
        ("system", "initialize"),
        ("tool_call", "completed"),
        ("tool_call", "started"),
    }
    for sequence, line in enumerate(raw.splitlines(), 1):
        try:
            value = json.loads(line, object_pairs_hook=unique_object)
        except (TypeError, ValueError) as error:
            raise PassportError("successful progress journal is malformed") from error
        observed = value.get("observed_monotonic_ns") if isinstance(value, dict) else None
        latest = (
            value.get("type", "") if isinstance(value, dict) else "",
            value.get("subtype", "") if isinstance(value, dict) else "",
        )
        if (
            not isinstance(value, dict)
            or set(value) != {
                "event_sha256", "observed_monotonic_ns", "sequence",
                "subtype", "type",
            }
            or value.get("sequence") != sequence
            or not isinstance(observed, int)
            or isinstance(observed, bool)
            or observed <= previous_observed
            or not DIGEST.fullmatch(value.get("event_sha256", ""))
            or latest not in allowed
        ):
            raise PassportError("successful progress journal is malformed")
        previous_observed = observed
        successful += latest == ("result", "success")
    if successful != 1 or latest != ("result", "success"):
        raise PassportError("progress journal does not prove terminal success")
    return sequence, hashlib.sha256(raw).hexdigest()


def ticket_state(workdir: Path, ticket: str) -> str:
    text = (workdir / "factory" / "tickets" / f"{ticket}.md").read_text(
        encoding="utf-8"
    )
    values = re.findall(r"^State:\s*(.*?)\s*$", text, re.I | re.M)
    if len(values) != 1:
        raise PassportError("ticket state is ambiguous")
    return values[0]


def authorized_inflight_rewrite(
    args: argparse.Namespace,
    previous: dict[str, Any],
    current: dict[str, Any],
    current_state: str,
    protected: str,
) -> bool:
    source = previous.get("factory_sha", "")
    target = args.factory_sha
    if (
        not SHA.fullmatch(source)
        or not SHA.fullmatch(target)
        or source == target
        or git(args.workdir, "status", "--porcelain=v1", "-z")
    ):
        return False
    relative = f"factory/migrations/inflight-release/{target}.json"
    raw = git(args.factory_root, "show", f"{protected}:{relative}", check=False)
    project = git(
        args.factory_root, "show", f"{protected}:factory/PROJECT.env", check=False
    )
    if not raw or len(raw.encode()) > 1_000_000 or not project:
        return False
    try:
        authorization = json.loads(raw, object_pairs_hook=unique_object)
    except (json.JSONDecodeError, ValueError):
        return False
    repositories = re.findall(
        r"^(?:export\s+)?GH_REPO\s*=\s*['\"]?([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)['\"]?\s*$",
        project,
        re.M,
    )
    if (
        not isinstance(authorization, dict)
        or set(authorization) != {
            "repository", "schema", "source_kit_sha", "target_kit_sha", "tickets",
        }
        or authorization.get("schema") != INFLIGHT_SCHEMA
        or repositories != [authorization.get("repository")]
        or authorization.get("source_kit_sha") != source
        or authorization.get("target_kit_sha") != target
        or not isinstance(authorization.get("tickets"), list)
        or not authorization["tickets"]
        or not DIGEST.fullmatch(previous.get("route_plan_sha256", ""))
        or previous["route_plan_sha256"] != route_digest(args.workdir, args.ticket)
    ):
        return False
    tickets, selected = [], None
    for item in authorization["tickets"]:
        if (
            not isinstance(item, dict)
            or set(item) != {"branch", "head", "state", "ticket"}
            or not TICKET.fullmatch(item.get("ticket", ""))
            or item.get("branch") != f"ticket/{item.get('ticket')}"
            or not SHA.fullmatch(item.get("head", ""))
            or item.get("state") not in INFLIGHT_STATES
        ):
            return False
        tickets.append(item["ticket"])
        if item["ticket"] == args.ticket:
            selected = item
    return (
        tickets == sorted(set(tickets))
        and selected == {
            "branch": current["branch"],
            "head": current["head_sha"],
            "state": current_state,
            "ticket": args.ticket,
        }
    )


def project_value(text: str, name: str) -> str | None:
    values = []
    assignment = re.compile(
        rf"(?:export[ \t]+)?{re.escape(name)}[ \t]*=[ \t]*(.*)"
    )
    for raw in text.splitlines():
        match = assignment.fullmatch(raw.strip())
        if not match:
            continue
        encoded = match.group(1)
        if encoded[:1] in {"'", '"'}:
            if (
                len(encoded) < 2
                or encoded[-1] != encoded[0]
                or encoded[0] in encoded[1:-1]
            ):
                return None
            encoded = encoded[1:-1]
        if any(fragment in encoded for fragment in ("`", "$(", "${", "\\", "\n", "\r")):
            return None
        values.append(encoded)
    return values[0] if len(values) == 1 else None


def rewrite_delta_allowed(
    workdir: Path, old_head: str, new_head: str, test_paths: str, ticket: str
) -> bool:
    roots = test_paths.split()
    if not roots or any(
        not re.fullmatch(r"[A-Za-z0-9._/-]+", root)
        or root.startswith("/")
        or ".." in root.split("/")
        or "//" in root
        for root in roots
    ):
        return False
    changed = git(
        workdir, "diff", "--name-status", "--no-renames",
        f"{old_head}^{{tree}}", f"{new_head}^{{tree}}",
    )
    seen_test = False
    for line in changed.splitlines():
        fields = line.split("\t")
        if len(fields) != 2 or fields[0] not in {"A", "M"}:
            return False
        status, path = fields
        if not re.fullmatch(r"[A-Za-z0-9._/-]+", path):
            return False
        is_test = any(
            path.startswith(root) if root.endswith("/") else path == root
            for root in roots
        )
        if path != f"factory/tickets/{ticket}.md" and not is_test:
            return False
        seen_test = seen_test or is_test
        current = git(workdir, "ls-tree", new_head, "--", path).split()
        if len(current) < 4 or current[:2] != ["100644", "blob"]:
            return False
        if status == "M":
            prior = git(workdir, "ls-tree", old_head, "--", path).split()
            if len(prior) < 4 or prior[:2] != ["100644", "blob"]:
                return False
    return seen_test


def ticket_log_append_allowed(
    workdir: Path, old_head: str, new_head: str, ticket: str
) -> bool:
    path = f"factory/tickets/{ticket}.md"
    if git(
        workdir, "diff", "--name-status", "--no-renames",
        f"{old_head}^{{tree}}", f"{new_head}^{{tree}}",
    ).splitlines() != [f"M\t{path}"]:
        return False
    old = git(workdir, "show", f"{old_head}:{path}", check=False)
    new = git(workdir, "show", f"{new_head}:{path}", check=False)
    current = git(workdir, "ls-tree", new_head, "--", path).split()
    return (
        bool(old)
        and new.startswith(old)
        and len(new) > len(old)
        and len(current) >= 4
        and current[:2] == ["100644", "blob"]
    )


def failed_rewrite_manifest(
    args: argparse.Namespace, previous: dict[str, Any], receipt_digest: str,
    factory_sha: str | None = None,
) -> bool:
    factory_sha = factory_sha or args.factory_sha
    matches = []
    for path in sorted((args.factory_root / "factory/runs").glob("*.meta")):
        value = manifest_fields(path)
        if (
            value.get("ticket") == args.ticket
            and value.get("role") == "test-author"
            and value.get("transition_receipt_sha256") == receipt_digest
        ):
            matches.append((path, value))
    if len(matches) != 1:
        return False
    path, value = matches[0]
    output = path.with_suffix(".out")
    return (
        value.get("phase") == "completed"
        and value.get("accounting_state") == "abandoned_conservative"
        and value.get("task_submitted") == "1"
        and value.get("exit_status") == "11"
        and value.get("role_exit") == "role_exit_push_failed"
        and value.get("role_head_before") == previous.get("head_sha")
        and value.get("kit_sha") == factory_sha
        and value.get("contract_version") == args.contract_version
        and DIGEST.fullmatch(value.get("output_sha256", "")) is not None
        and output.is_file()
        and not output.is_symlink()
        and role_output_digest(output) == value["output_sha256"]
    )


def authorized_ticket_rewrite(
    args: argparse.Namespace,
    previous: dict[str, Any],
    current: dict[str, Any],
    current_state: str,
    protected: str,
) -> str | None:
    if (
        previous.get("factory_sha") != args.factory_sha
        or git(args.workdir, "status", "--porcelain=v1", "-z")
    ):
        return None
    project = git(
        args.factory_root, "show", f"{protected}:factory/PROJECT.env", check=False
    )
    repository = project_value(project, "GH_REPO")
    test_paths = project_value(project, "TEST_PATHS")
    route = route_digest(args.workdir, args.ticket)
    if (
        not repository
        or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)
        or not test_paths
        or not DIGEST.fullmatch(route or "")
    ):
        return None
    relative = (
        "factory/migrations/ticket-rewrite/"
        f"{current['head_sha']}.json"
    )
    raw = git(args.factory_root, "show", f"{protected}:{relative}", check=False)
    if not raw or len(raw.encode()) > 1_000_000:
        return None
    try:
        authorization = json.loads(raw, object_pairs_hook=unique_object)
    except (json.JSONDecodeError, ValueError):
        return None
    if authorization.get("schema") == HISTORY_REPAIR_SCHEMA:
        return authorized_history_repair(
            args, previous, current, current_state, protected,
            authorization, raw, relative, repository, test_paths, route,
        )
    if authorization.get("schema") == NORMALIZATION_SCHEMA:
        return authorized_history_normalization(
            args, previous, current, current_state, protected,
            authorization, raw, relative, repository, test_paths, route,
        )
    if current_state != "Building":
        return None
    receipt_digest = authorization.get("transition_receipt_sha256", "")
    expected = {
        "branch": current["branch"],
        "factory_sha": args.factory_sha,
        "head": current["head_sha"],
        "passport_sha256": previous.get("passport_sha256"),
        "previous_head": previous.get("head_sha"),
        "repository": repository,
        "role": "test-author",
        "route_plan_sha256": route,
        "schema": REWRITE_SCHEMA,
        "state": current_state,
        "ticket": args.ticket,
        "transition_receipt_sha256": receipt_digest,
    }
    if authorization != expected or not DIGEST.fullmatch(receipt_digest):
        return None
    try:
        consumed = receipt(args.state_dir, args.ticket, receipt_digest)
    except (FileNotFoundError, json.JSONDecodeError, OSError, PassportError):
        return None
    if (
        consumed.get("factory_sha") != args.factory_sha
        or consumed.get("head_sha") != previous.get("head_sha")
        or consumed.get("project") != args.project
        or consumed.get("role") != "test-author"
        or consumed.get("stage") != "FIX test-author"
        or not failed_rewrite_manifest(args, previous, receipt_digest)
        or not rewrite_delta_allowed(
            args.workdir, previous["head_sha"], current["head_sha"],
            test_paths, args.ticket,
        )
    ):
        return None
    return hashlib.sha256(canonical(authorization)).hexdigest()


def authorized_history_repair(
    args: argparse.Namespace,
    previous: dict[str, Any],
    current: dict[str, Any],
    current_state: str,
    protected: str,
    authorization: dict[str, Any],
    raw: str,
    relative: str,
    repository: str,
    test_paths: str,
    route: str,
) -> str | None:
    authorization_parent = authorization.get("authorization_parent", "")
    replay_base = authorization.get("replay_base", "")
    receipt_digest = authorization.get("failed_test_receipt_sha256", "")
    failed_factory = authorization.get("failed_test_factory_sha", "")
    run_id = authorization.get("failed_test_run_id", "")
    issued = authorization.get("issued_at_epoch")
    expires = authorization.get("expires_at_epoch")
    issue = authorization.get("issue", "")
    operator = authorization.get("operator", "")
    expected = {
        "authorization_parent": authorization_parent,
        "branch": current["branch"],
        "expires_at_epoch": expires,
        "factory_sha": args.factory_sha,
        "failed_test_factory_sha": failed_factory,
        "failed_test_receipt_sha256": receipt_digest,
        "failed_test_run_id": run_id,
        "force_with_lease_head": previous.get("head_sha"),
        "head": current["head_sha"],
        "head_tree": current["head_tree"],
        "issue": issue,
        "issued_at_epoch": issued,
        "mode": "failed-push-history-repair",
        "operator": operator,
        "passport_sha256": previous.get("passport_sha256"),
        "previous_head": previous.get("head_sha"),
        "previous_tree": previous.get("head_tree"),
        "replay_base": replay_base,
        "repository": repository,
        "route_plan_sha256": route,
        "schema": HISTORY_REPAIR_SCHEMA,
        "state": current_state,
        "ticket": args.ticket,
    }
    now = int(time.time())
    if (
        authorization != expected
        or current_state != "Building"
        or not SHA.fullmatch(authorization_parent)
        or previous.get("protected_base_sha") != authorization_parent
        or not SHA.fullmatch(replay_base)
        or replay_base not in previous.get("base_history", [])
        or not DIGEST.fullmatch(receipt_digest)
        or not SHA.fullmatch(failed_factory)
        or {
            "contract_version": args.contract_version,
            "factory_sha": failed_factory,
        } not in previous.get("factory_release_history", [])
        or not RUN_ID.fullmatch(run_id)
        or not isinstance(issued, int)
        or isinstance(issued, bool)
        or not isinstance(expires, int)
        or isinstance(expires, bool)
        or not issued <= now < expires <= issued + 86_400
        or not re.fullmatch(
            r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/issues/[1-9][0-9]*",
            issue,
        )
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", operator)
        or raw.encode() + b"\n" != canonical(authorization)
    ):
        return None
    introductions = git(
        args.factory_root, "log", "--format=%H", "--diff-filter=A",
        protected, "--", relative,
    ).splitlines()
    if len(introductions) != 1:
        return None
    introduction = introductions[0]
    parents = git(
        args.factory_root, "show", "-s", "--format=%P", introduction
    ).split()
    changed = git(
        args.factory_root, "diff-tree", "--no-commit-id", "--name-status",
        "--no-renames", "-r", introduction,
    ).splitlines()
    if (
        parents != [authorization_parent]
        or changed != [f"A\t{relative}"]
        or subprocess.run(
            [
                "git", "-C", str(args.factory_root), "merge-base",
                "--is-ancestor", introduction, protected,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=120,
        ).returncode
    ):
        return None
    try:
        consumed = receipt(args.state_dir, args.ticket, receipt_digest)
    except (FileNotFoundError, json.JSONDecodeError, OSError, PassportError):
        return None
    if (
        consumed.get("factory_sha") != failed_factory
        or consumed.get("head_sha") != previous.get("head_sha")
        or consumed.get("project") != args.project
        or consumed.get("branch") != current["branch"]
        or consumed.get("role") != "test-author"
        or consumed.get("stage") not in {"RUN test-author", "FIX test-author"}
        or consumed.get("contract_version") != args.contract_version
        or not failed_rewrite_manifest(
            args, previous, receipt_digest, failed_factory
        )
        or not ticket_log_append_allowed(
            args.workdir, previous["head_sha"], current["head_sha"], args.ticket,
        )
        or not verified_history_repair(
            str(args.workdir), replay_base, previous["head_sha"],
            current["head_sha"], test_paths.split(),
            "factory/ conformance/factory/ .gitignore context/memory.md".split(),
        )
    ):
        return None
    terminal = [
        manifest_fields(path)
        for path in sorted((args.factory_root / "factory/runs").glob("*.meta"))
        if manifest_fields(path).get("transition_receipt_sha256")
        == receipt_digest
    ]
    if len(terminal) != 1 or terminal[0].get("run_id") != run_id:
        return None
    return hashlib.sha256(canonical(authorization)).hexdigest()


def authorized_history_normalization(
    args: argparse.Namespace,
    previous: dict[str, Any],
    current: dict[str, Any],
    current_state: str,
    protected: str,
    authorization: dict[str, Any],
    raw: str,
    relative: str,
    repository: str,
    test_paths: str,
    route: str,
) -> str | None:
    base = authorization.get("base", "")
    accepted_run = authorization.get("accepted_test_run_id", "")
    accepted_receipt = authorization.get("accepted_test_receipt_sha256", "")
    accepted_factory = authorization.get("accepted_test_factory_sha", "")
    expected = {
        "accepted_test_factory_sha": accepted_factory,
        "accepted_test_receipt_sha256": accepted_receipt,
        "accepted_test_run_id": accepted_run,
        "base": base,
        "branch": current["branch"],
        "factory_sha": args.factory_sha,
        "head": current["head_sha"],
        "head_tree": current["head_tree"],
        "mode": "accepted-push-history-normalization",
        "passport_sha256": previous.get("passport_sha256"),
        "previous_head": previous.get("head_sha"),
        "previous_tree": previous.get("head_tree"),
        "repository": repository,
        "route_plan_sha256": route,
        "schema": NORMALIZATION_SCHEMA,
        "state": current_state,
        "ticket": args.ticket,
    }
    if (
        authorization != expected
        or current_state not in {
            "Planning", "Building", "Review", "Blocked-Escalated",
        }
        or not SHA.fullmatch(base)
        or not SHA.fullmatch(accepted_factory)
        or not DIGEST.fullmatch(accepted_receipt)
        or not isinstance(accepted_run, str)
        or not accepted_run
        or previous.get("head_tree") != current.get("head_tree")
        or {
            "contract_version": args.contract_version,
            "factory_sha": accepted_factory,
        } not in previous.get("factory_release_history", [])
        or raw.encode() + b"\n" != canonical(authorization)
    ):
        return None
    introductions = git(
        args.factory_root, "log", "--format=%H", "--diff-filter=A",
        protected, "--", relative,
    ).splitlines()
    if len(introductions) != 1:
        return None
    introduction = introductions[0]
    parents = git(
        args.factory_root, "show", "-s", "--format=%P", introduction
    ).split()
    changed = git(
        args.factory_root, "diff-tree", "--no-commit-id", "--name-status",
        "--no-renames", "-r", introduction,
    ).splitlines()
    if (
        parents != [base]
        or changed != [f"A\t{relative}"]
        or subprocess.run(
            [
                "git", "-C", str(args.factory_root), "merge-base",
                "--is-ancestor", introduction, protected,
            ],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            check=False, timeout=120,
        ).returncode
    ):
        return None
    test_roots = test_paths.split()
    exempt = "factory/ conformance/factory/ .gitignore context/memory.md".split()
    plan = verified_normalization_plan(
        str(args.workdir), base, previous["head_sha"], current["head_sha"],
        test_roots, exempt,
    )
    if plan is None:
        return None
    late_parents = {
        git(args.workdir, "rev-parse", f"{commit.sha}^1")
        for commit in plan[1]
    }
    completed = [
        item for item in previous.get("completed_role_evidence", [])
        if item.get("run_id") == accepted_run
        and item.get("role") == "test-author"
        and item.get("transition_receipt_sha256") == accepted_receipt
        and item.get("factory_sha") == accepted_factory
        and item.get("contract_version") == args.contract_version
        and item.get("head_before") in late_parents
    ]
    charges = [
        item for item in previous.get("charge_records", [])
        if item.get("run_id") == accepted_run
        and item.get("role") == "test-author"
        and item.get("transition_receipt_sha256") == accepted_receipt
        and item.get("factory_sha") == accepted_factory
        and item.get("contract_version") == args.contract_version
        and item.get("head_before") in late_parents
    ]
    if len(completed) != 1 or len(charges) != 1:
        return None
    return hashlib.sha256(canonical(authorization)).hexdigest()


def route_digest(workdir: Path, ticket: str) -> str | None:
    path = workdir / "factory" / "route-plans" / f"{ticket}.json"
    return hashlib.sha256(read_regular(path)).hexdigest() if path.exists() else None


def receipt(state_dir: Path, ticket: str, expected: str) -> dict[str, Any]:
    value = json.loads(read_regular(state_dir / f"{ticket}.json", 0o600))
    immutable = {
        name: item for name, item in value.items()
        if name not in {"consumed", "consumed_at_epoch", "receipt_sha256"}
    }
    if (
        value.get("schema") != RECEIPT_SCHEMA
        or value.get("receipt_sha256") != expected
        or expected != hashlib.sha256(canonical(immutable)).hexdigest()
        or value.get("consumed") is not True
    ):
        raise PassportError("consumed transition receipt is invalid")
    return value


def identity(args: argparse.Namespace) -> dict[str, Any]:
    workdir = args.workdir.resolve(strict=True)
    origin = os.environ.get("FACTORY_CERTIFIED_PRODUCT_ORIGIN", "")
    if not origin or any(character in origin for character in "\n\r\t"):
        raise PassportError("certified product origin is unavailable")
    return {
        "branch": git(workdir, "symbolic-ref", "--quiet", "--short", "HEAD"),
        "head_sha": git(workdir, "rev-parse", "HEAD"),
        "head_tree": git(workdir, "rev-parse", "HEAD^{tree}"),
        "product_origin_sha256": hashlib.sha256(origin.encode()).hexdigest(),
        "project": args.project,
        "ticket": args.ticket,
        "ticket_blob": git(
            workdir, "rev-parse", f"HEAD:factory/tickets/{args.ticket}.md"
        ),
    }


def merge_records(
    prior: list[dict[str, Any]], current: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    records = {item["run_id"]: item for item in prior}
    if len(records) != len(prior):
        raise PassportError("prior passport run evidence is ambiguous")
    for item in current:
        existing = records.get(item["run_id"])
        if existing is not None and existing != item:
            raise PassportError("run evidence changed across passport export")
        records[item["run_id"]] = item
    return list(records.values())


def validate_completion_corrections(
    value: Any, completed: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise PassportError("completion correction history is invalid")
    identities = set()
    expected_v1 = {
        "failed_factory_sha", "issue", "output_head_sha", "progress_events",
        "progress_journal_sha256", "receipt_parent_file_sha256",
        "recovery_factory_sha", "run_id", "schema",
        "transition_receipt_sha256",
    }
    expected_v2 = {*expected_v1, "role"}
    for item in value:
        identity_key = (
            item.get("run_id", "") if isinstance(item, dict) else "",
            item.get("transition_receipt_sha256", "")
            if isinstance(item, dict) else "",
        )
        issue = item.get("issue") if isinstance(item, dict) else None
        schema = item.get("schema") if isinstance(item, dict) else None
        expected_role = (
            item.get("role", "")
            if schema == PASSPORTLESS_MODEL_CORRECTION_SCHEMA
            and issue == MODEL_IDENTITY_CORRECTION_ISSUE
            else "builder" if issue == COMPLETION_CORRECTION_ISSUE
            else "spec-linter" if issue == MODEL_IDENTITY_CORRECTION_ISSUE
            else ""
        )
        expected_keys = (
            expected_v2
            if schema == PASSPORTLESS_MODEL_CORRECTION_SCHEMA
            else expected_v1
        )
        parent = (
            item.get("receipt_parent_file_sha256")
            if isinstance(item, dict) else None
        )
        matches = [
            evidence for evidence in completed
            if isinstance(evidence, dict)
            and evidence.get("run_id") == identity_key[0]
            and evidence.get("transition_receipt_sha256") == identity_key[1]
            and evidence.get("role") == expected_role
            and evidence.get("factory_sha")
            == (item.get("failed_factory_sha") if isinstance(item, dict) else None)
        ]
        if (
            not isinstance(item, dict)
            or set(item) != expected_keys
            or schema not in {
                COMPLETION_CORRECTION_SCHEMA,
                PASSPORTLESS_MODEL_CORRECTION_SCHEMA,
            }
            or expected_role not in RECOVERABLE_ROLES
            or not SHA.fullmatch(item.get("failed_factory_sha", ""))
            or not SHA.fullmatch(item.get("recovery_factory_sha", ""))
            or (
                schema == COMPLETION_CORRECTION_SCHEMA
                and item["failed_factory_sha"] == item["recovery_factory_sha"]
            )
            or not SHA.fullmatch(item.get("output_head_sha", ""))
            or not DIGEST.fullmatch(item.get("progress_journal_sha256", ""))
            or not (
                DIGEST.fullmatch(parent or "")
                or (
                    schema == PASSPORTLESS_MODEL_CORRECTION_SCHEMA
                    and expected_role == "planner"
                    and parent is None
                )
            )
            or not DIGEST.fullmatch(identity_key[1])
            or not RUN_ID.fullmatch(identity_key[0])
            or not isinstance(item.get("progress_events"), int)
            or isinstance(item.get("progress_events"), bool)
            or item["progress_events"] <= 0
            or identity_key in identities
            or len(matches) != 1
        ):
            raise PassportError("completion correction history is invalid")
        identities.add(identity_key)
    return list(value)


def terminal_authorization_evidence(
    args: argparse.Namespace, consumed: dict[str, Any]
) -> dict[str, Any] | None:
    matches = []
    for path in sorted((args.factory_root / "factory/runs").glob("*.meta")):
        fields = manifest_fields(path)
        if (
            fields.get("ticket") == args.ticket
            and fields.get("transition_receipt_sha256")
            == consumed.get("receipt_sha256")
        ):
            matches.append((path, fields))
    if len(matches) != 1:
        return None
    path, fields = matches[0]
    output = path.with_suffix(".out")
    accounted = fields.get("accounting_state") == "completed" or (
        fields.get("accounting_state") == "abandoned_conservative"
        and fields.get("cost_basis") == "conservative_reservation"
        and fields.get("effective_cost") == fields.get("reserved_usd")
    )
    if (
        fields.get("phase") != "completed"
        or not accounted
        or fields.get("task_submitted") != "1"
        or fields.get("exit_status") != "0"
        or fields.get("role_exit") != "ok"
        or not isinstance(fields.get("run_id"), str)
        or not fields["run_id"]
        or fields.get("role") != consumed.get("role")
        or fields.get("role_head_before") != consumed.get("head_sha")
        or fields.get("kit_sha") != consumed.get("factory_sha")
        or fields.get("contract_version") != consumed.get("contract_version")
        or not DIGEST.fullmatch(fields.get("output_sha256", ""))
        or not output.is_file()
        or output.is_symlink()
        or role_output_digest(output) != fields["output_sha256"]
    ):
        return None
    return {
        "accounting_state": fields["accounting_state"],
        "charge_micro_usd": micro_usd(fields),
        "contract_version": fields["contract_version"],
        "cost_basis": fields.get("cost_basis"),
        "factory_sha": fields["kit_sha"],
        "head_before": fields["role_head_before"],
        "manifest_sha256": hashlib.sha256(read_regular(path)).hexdigest(),
        "output_sha256": fields["output_sha256"],
        "role": fields["role"],
        "run_id": fields.get("run_id"),
        "reserved_micro_usd": micro_usd({
            "reserved_usd": fields.get("reserved_usd", "0")
        }),
    }


def lineage_authorization_path(factory_sha: str, ticket: str) -> str:
    return (
        "factory/migrations/ticket-passport-lineage/"
        f"{factory_sha}/{ticket}.json"
    )


def lineage_authorization_value(
    args: argparse.Namespace,
    previous: dict[str, Any],
    parent_raw: str,
    current: dict[str, Any],
    target_protected_parent: str,
    target_route: str,
    consumed: dict[str, Any],
) -> dict[str, Any] | None:
    terminal = terminal_authorization_evidence(args, consumed)
    project = git(
        args.factory_root,
        "show",
        f"{target_protected_parent}:factory/PROJECT.env",
        check=False,
    )
    repository = project_value(project, "GH_REPO")
    migrations = previous.get("migration_history")
    if (
        terminal is None
        or not repository
        or not isinstance(migrations, list)
        or not migrations
        or not DIGEST.fullmatch(parent_raw)
        or not DIGEST.fullmatch(previous.get("passport_sha256", ""))
        or not DIGEST.fullmatch(previous.get("route_plan_sha256", ""))
        or not DIGEST.fullmatch(target_route)
        or not SHA.fullmatch(target_protected_parent)
    ):
        return None
    return {
        "branch": current["branch"],
        "contract_version": args.contract_version,
        "product_origin_sha256": current["product_origin_sha256"],
        "project": args.project,
        "receipt": {
            "factory_sha": consumed.get("factory_sha"),
            "head_sha": consumed.get("head_sha"),
            "parent_digest": consumed.get("parent_digest"),
            "passport_file_sha256": consumed.get("passport_sha256"),
            "receipt_sha256": consumed.get("receipt_sha256"),
            "role": consumed.get("role"),
            "stage": consumed.get("stage"),
        },
        "repository": repository,
        "schema": LINEAGE_SCHEMA,
        "source": {
            "factory_sha": previous.get("factory_sha"),
            "head_sha": previous.get("head_sha"),
            "migration_history_sha256": hashlib.sha256(
                canonical(migrations)
            ).hexdigest(),
            "passport_file_sha256": parent_raw,
            "passport_sha256": previous.get("passport_sha256"),
            "protected_base_sha": previous.get("protected_base_sha"),
            "route_plan_sha256": previous.get("route_plan_sha256"),
        },
        "target": {
            "factory_sha": args.factory_sha,
            "head_sha": current["head_sha"],
            "protected_base_parent_sha": target_protected_parent,
            "route_plan_sha256": target_route,
        },
        "terminal": terminal,
        "ticket": args.ticket,
    }


def lineage_authorization_metadata(
    args: argparse.Namespace,
    previous: dict[str, Any],
    parent_raw: str,
    current: dict[str, Any],
    protected: str,
) -> dict[str, str]:
    migrations = previous.get("migration_history", [])
    if not isinstance(migrations, list) or not any(
        not isinstance(item, dict) or item.get("schema") != MIGRATION_SCHEMA
        for item in migrations
    ):
        return {}
    relative = lineage_authorization_path(args.factory_sha, args.ticket)
    raw = git(args.factory_root, "show", f"{protected}:{relative}", check=False)
    if not raw:
        return {}
    try:
        value = json.loads(raw, object_pairs_hook=unique_object)
        consumed = receipt(
            args.state_dir, args.ticket,
            value.get("receipt", {}).get("receipt_sha256", ""),
        )
    except (AttributeError, FileNotFoundError, json.JSONDecodeError, OSError,
            PassportError, ValueError):
        raise PassportError("passport lineage authorization is invalid") from None
    parents = git(args.factory_root, "show", "-s", "--format=%P", protected).split()
    changed = git(
        args.factory_root, "diff-tree", "--no-commit-id", "--name-status",
        "--no-renames", "-r", protected,
    ).splitlines()
    blob = git(args.factory_root, "rev-parse", f"{protected}:{relative}", check=False)
    tree = git(args.factory_root, "ls-tree", protected, "--", relative).split()
    expected = (
        lineage_authorization_value(
            args, previous, parent_raw, current, parents[0],
            route_digest(args.workdir, args.ticket) or "", consumed,
        )
        if (
            len(parents) == 1
            and changed == [f"A\t{relative}"]
            and len(tree) >= 4
            and tree[:2] == ["100644", "blob"]
        )
        else None
    )
    if (
        value != expected
        or raw.encode() + b"\n" != canonical(value)
        or not SHA.fullmatch(blob)
    ):
        raise PassportError("passport lineage authorization is invalid")
    return {
        "lineage_authorization_blob": blob,
        "lineage_authorization_commit": protected,
        "lineage_authorization_path": relative,
        "lineage_authorization_sha256": hashlib.sha256(canonical(value)).hexdigest(),
    }


def valid_legacy_migration(item: Any) -> bool:
    fields = {
        "from_factory_sha", "from_head_sha", "from_protected_base_sha",
        "to_factory_sha", "to_head_sha", "to_protected_base_sha",
    }
    return (
        isinstance(item, dict)
        and set(item).issubset(fields | {"rewrite_authorization_sha256"})
        and fields.issubset(item)
        and all(
            isinstance(item.get(name), str) and SHA.fullmatch(item[name])
            for name in fields
        )
        and (
            "rewrite_authorization_sha256" not in item
            or (
                isinstance(item["rewrite_authorization_sha256"], str)
                and DIGEST.fullmatch(item["rewrite_authorization_sha256"])
            )
        )
    )


def semantic_migration(item: Any) -> bool:
    return valid_v2_migration(item) or valid_legacy_migration(item)


def validate_legacy_lineage_authorization(
    args: argparse.Namespace,
    previous: dict[str, Any],
    consumed: dict[str, Any],
    current: dict[str, Any],
    migrations: list[dict[str, Any]],
    authorization_index: int,
) -> bool:
    edge = migrations[authorization_index]
    relative = lineage_authorization_path(
        edge["to_factory_sha"], args.ticket
    )
    commit = edge.get("lineage_authorization_commit", "")
    if (
        edge.get("lineage_authorization_path") != relative
        or edge.get("lineage_authorization_commit")
        != edge.get("to_protected_base_sha")
        or not SHA.fullmatch(commit)
    ):
        return False
    protected = git(args.factory_root, "rev-parse", "origin/main")
    if subprocess.run(
        [
            "git", "-C", str(args.factory_root), "merge-base",
            "--is-ancestor", commit, protected,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=120,
    ).returncode:
        return False
    raw = git(args.factory_root, "show", f"{commit}:{relative}", check=False)
    blob = git(args.factory_root, "rev-parse", f"{commit}:{relative}", check=False)
    try:
        value = json.loads(raw, object_pairs_hook=unique_object)
    except (json.JSONDecodeError, ValueError):
        return False
    parents = git(args.factory_root, "show", "-s", "--format=%P", commit).split()
    changed = git(
        args.factory_root, "diff-tree", "--no-commit-id", "--name-status",
        "--no-renames", "-r", commit,
    ).splitlines()
    tree = git(args.factory_root, "ls-tree", commit, "--", relative).split()
    if (
        len(parents) != 1
        or changed != [f"A\t{relative}"]
        or len(tree) < 4
        or tree[:2] != ["100644", "blob"]
    ):
        return False
    source = {
        "factory_sha": edge["from_factory_sha"],
        "head_sha": edge["from_head_sha"],
        "migration_history": migrations[:authorization_index],
        "passport_sha256": edge["from_passport_sha256"],
        "protected_base_sha": edge["from_protected_base_sha"],
        "route_plan_sha256": edge["from_route_plan_sha256"],
    }
    target = {
        "branch": current["branch"],
        "head_sha": edge["to_head_sha"],
        "product_origin_sha256": current["product_origin_sha256"],
    }
    authorization_args = argparse.Namespace(**vars(args))
    authorization_args.factory_sha = edge["to_factory_sha"]
    expected = lineage_authorization_value(
        authorization_args,
        source,
        edge["from_passport_file_sha256"],
        target,
        parents[0],
        edge["to_route_plan_sha256"],
        consumed,
    )
    return (
        value == expected
        and raw.encode() + b"\n" == canonical(value)
        and blob == edge.get("lineage_authorization_blob")
        and hashlib.sha256(canonical(value)).hexdigest()
        == edge.get("lineage_authorization_sha256")
    )


def migrated_receipt_lineage(
    args: argparse.Namespace,
    previous: dict[str, Any],
    consumed: dict[str, Any],
    current: dict[str, Any],
) -> bool:
    migrations = previous.get("migration_history")
    old_factory = consumed.get("factory_sha", "")
    old_head = consumed.get("head_sha", "")
    if (
        not isinstance(migrations, list)
        or not migrations
        or not isinstance(old_factory, str)
        or not SHA.fullmatch(old_factory)
        or not isinstance(old_head, str)
        or not SHA.fullmatch(old_head)
        or previous.get("factory_sha") != args.factory_sha
        or previous.get("head_sha") != current["head_sha"]
        or not SHA.fullmatch(previous.get("protected_base_sha", ""))
    ):
        return False

    def ancestor(before: str, after: str) -> bool:
        result = subprocess.run(
            [
                "git", "-C", str(args.workdir), "merge-base",
                "--is-ancestor", before, after,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=120,
        )
        return result.returncode == 0

    starts = []
    for index, migration in enumerate(migrations):
        if (
            not semantic_migration(migration)
            or migration.get("from_factory_sha") != old_factory
            or not ancestor(old_head, migration.get("from_head_sha", ""))
        ):
            continue
        suffix = migrations[index:]
        if (
            all(semantic_migration(item) for item in suffix)
            and all(
                prior["to_factory_sha"] == following["from_factory_sha"]
                and prior["to_head_sha"] == following["from_head_sha"]
                and prior["to_protected_base_sha"]
                == following["from_protected_base_sha"]
                and (
                    not (
                        valid_v2_migration(prior)
                        and valid_v2_migration(following)
                    )
                    or prior["to_route_plan_sha256"]
                    == following["from_route_plan_sha256"]
                )
                for prior, following in zip(suffix, suffix[1:])
            )
            and suffix[-1]["to_factory_sha"] == args.factory_sha
            and suffix[-1]["to_head_sha"] == current["head_sha"]
            and suffix[-1]["to_protected_base_sha"]
            == previous["protected_base_sha"]
            and suffix[-1].get("to_route_plan_sha256")
            == previous.get("route_plan_sha256")
        ):
            starts.append(index)
    bound_passport = consumed.get("passport_sha256")
    bound_starts = [
        index for index in starts
        if valid_v2_migration(migrations[index])
        and DIGEST.fullmatch(bound_passport or "")
        and migrations[index]["from_passport_file_sha256"] == bound_passport
    ]
    if bound_starts:
        starts = bound_starts
    if len(starts) != 1:
        return False

    start = starts[0]
    suffix = migrations[start:]
    v2_lineage = (
        all(valid_v2_migration(item) for item in suffix)
        and isinstance(bound_passport, str)
        and DIGEST.fullmatch(bound_passport)
        and suffix[0]["from_passport_file_sha256"] == bound_passport
    )
    standard_lineage = v2_lineage
    if not standard_lineage:
        authorization_indexes = [
            index for index, item in enumerate(migrations)
            if isinstance(item, dict)
            and "lineage_authorization_sha256" in item
        ]
        authorization_index = (
            authorization_indexes[0]
            if len(authorization_indexes) == 1
            else -1
        )
        standard_lineage = (
            start < authorization_index
            and all(
                valid_legacy_migration(item)
                for item in migrations[start:authorization_index]
            )
            and all(
                valid_v2_migration(item)
                for item in migrations[authorization_index:]
            )
            and validate_legacy_lineage_authorization(
                args, previous, consumed, current, migrations,
                authorization_index,
            )
        )

    if ancestor(old_head, current["head_sha"]):
        return standard_lineage
    if (
        not all(valid_v2_migration(item) for item in suffix)
        or suffix[-1]["from_passport_file_sha256"]
        != previous.get("parent_file_sha256")
        or suffix[-1]["from_passport_sha256"]
        != previous.get("parent_digest")
    ):
        return False
    rewrites = []
    for edge in suffix:
        if ancestor(edge["from_head_sha"], edge["to_head_sha"]):
            continue
        try:
            same_tree = git(
                args.workdir, "rev-parse", f"{edge['from_head_sha']}^{{tree}}"
            ) == git(
                args.workdir, "rev-parse", f"{edge['to_head_sha']}^{{tree}}"
            )
        except PassportError:
            return False
        if (
            not DIGEST.fullmatch(edge.get("rewrite_authorization_sha256", ""))
            or (
                not same_tree
                and not ticket_log_append_allowed(
                    args.workdir, edge["from_head_sha"], edge["to_head_sha"],
                    args.ticket,
                )
            )
        ):
            return False
        rewrites.append(edge)
    return len(rewrites) == 1


def converged_success_migration_lineage(
    args: argparse.Namespace,
    previous: dict[str, Any],
    consumed: dict[str, Any],
    current: dict[str, Any],
) -> bool:
    """Authenticate the release suffix after Builder advanced the receipt head."""
    history = previous.get("factory_release_history")
    migrations = previous.get("migration_history")
    before = consumed.get("head_sha", "")
    source = consumed.get("factory_sha", "")
    if (
        not isinstance(migrations, list)
        or not SHA.fullmatch(before)
        or not SHA.fullmatch(source)
        or previous.get("factory_sha") != args.factory_sha
        or previous.get("head_sha") != current["head_sha"]
        or not SHA.fullmatch(previous.get("protected_base_sha", ""))
        or not DIGEST.fullmatch(previous.get("route_plan_sha256", ""))
        or not DIGEST.fullmatch(previous.get("parent_file_sha256", ""))
        or not DIGEST.fullmatch(previous.get("parent_digest", ""))
        or not successor_release_lineage(
            history, migrations, source, args.factory_sha, valid_v2_migration,
        )
    ):
        return False

    candidates = []
    for index, edge in enumerate(migrations):
        if (
            not valid_v2_migration(edge)
            or edge["from_factory_sha"] != source
            or edge["from_route_plan_sha256"]
            != consumed.get("route_plan_sha256")
            or edge["from_head_sha"] == before
            or subprocess.run(
                [
                    "git", "-C", str(args.workdir), "merge-base",
                    "--is-ancestor", before, edge["from_head_sha"],
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=120,
            ).returncode != 0
        ):
            continue
        suffix = migrations[index:]
        if (
            all(valid_v2_migration(item) for item in suffix)
            and all(
                prior["to_factory_sha"] == following["from_factory_sha"]
                and prior["to_head_sha"] == following["from_head_sha"]
                and prior["to_protected_base_sha"]
                == following["from_protected_base_sha"]
                and prior["to_route_plan_sha256"]
                == following["from_route_plan_sha256"]
                for prior, following in zip(suffix, suffix[1:])
            )
            and suffix[-1]["to_factory_sha"] == args.factory_sha
            and suffix[-1]["to_head_sha"] == current["head_sha"]
            and suffix[-1]["to_protected_base_sha"]
            == previous["protected_base_sha"]
            and suffix[-1]["to_route_plan_sha256"]
            == previous["route_plan_sha256"]
            and suffix[-1]["from_passport_file_sha256"]
            == previous["parent_file_sha256"]
            and suffix[-1]["from_passport_sha256"]
            == previous["parent_digest"]
        ):
            candidates.append(index)
    return len(candidates) == 1


def export(args: argparse.Namespace, secret: bytes) -> dict[str, Any]:
    passports = safe_directory(args.state_dir / "passports", create=True)
    destination = passports / f"{args.ticket}.json"
    if git(args.workdir, "status", "--porcelain=v1", "-z"):
        raise PassportError("passport export requires a clean execution cell")
    previous: dict[str, Any] = {}
    parent_raw = None
    if destination.exists() or destination.is_symlink():
        previous, parent_raw = load_passport(destination, secret)
    consumed = receipt(args.state_dir, args.ticket, args.receipt)
    current_identity = identity(args)
    protected = git(args.factory_root, "rev-parse", "origin/main")
    old_head = consumed.get("head_sha", "")
    bound_passport = consumed.get("passport_sha256")
    migrated_receipt = migrated_receipt_lineage(
        args, previous, consumed, current_identity
    )
    if (
        consumed.get("ticket") != args.ticket
        or consumed.get("project") != args.project
        or (
            consumed.get("factory_sha") != args.factory_sha
            and not migrated_receipt
        )
        or consumed.get("contract_version") != args.contract_version
        or consumed.get("branch") != current_identity["branch"]
        or (
            migrated_receipt
            and previous.get("protected_base_sha") != protected
        )
        or not SHA.fullmatch(old_head)
        or (
            not migrated_receipt
            and subprocess.run(
                ["git", "-C", str(args.workdir), "merge-base", "--is-ancestor",
                 old_head, current_identity["head_sha"]],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            ).returncode != 0
        )
        or (bound_passport != parent_raw and not migrated_receipt)
    ):
        raise PassportError("transition receipt is outside current ticket lineage")
    completed, charges = run_evidence(args.factory_root / "factory", args.ticket)
    completed = merge_records(previous.get("completed_role_evidence", []), completed)
    charges = merge_records(previous.get("charge_records", []), charges)
    correction_evidence: dict[str, Any] = {}
    if "completed_role_corrections" in previous:
        correction_evidence["completed_role_corrections"] = (
            validate_completion_corrections(
                previous["completed_role_corrections"], completed
            )
        )
    receipt_charges = [
        item for item in charges
        if item.get("transition_receipt_sha256") == args.receipt
    ]
    matching = [
        item for item in charges
        if item.get("transition_receipt_sha256") == args.receipt
        and item.get("role") == consumed.get("role")
        and item.get("head_before") == old_head
        and item.get("factory_sha") == consumed.get("factory_sha")
        and item.get("contract_version") == consumed.get("contract_version")
    ]
    if (
        consumed.get("role")
        and (len(receipt_charges) != 1 or len(matching) != 1)
    ):
        raise PassportError("receipt-bound terminal role evidence is missing")
    history = list(previous.get("factory_release_history", []))
    release = {
        "contract_version": args.contract_version,
        "factory_sha": args.factory_sha,
    }
    if release not in history:
        history.append(release)
    value = {
        **current_identity,
        "base_history": [
            *previous.get("base_history", []),
            *([] if protected in previous.get("base_history", []) else [protected]),
        ],
        "charge_records": charges,
        **correction_evidence,
        "completed_role_evidence": completed,
        "contract_version": args.contract_version,
        "cumulative_charges_micro_usd": sum(
            item["charge_micro_usd"] for item in charges
        ),
        "current_state": ticket_state(args.workdir, args.ticket),
        "current_stage": consumed["stage"],
        "factory_release_history": history,
        "factory_sha": args.factory_sha,
        "nonce": secrets.token_hex(16),
        "parent_digest": previous.get("passport_sha256"),
        "parent_file_sha256": parent_raw,
        "protected_base_sha": protected,
        "publication_state": (
            previous.get("publication_state", "none")
            if args.publication_state == "preserve"
            else args.publication_state
        ),
        "route_plan_sha256": route_digest(args.workdir, args.ticket),
        "schema": SCHEMA,
        "transition_receipt_sha256": args.receipt,
    }
    migrations = previous.get("migration_history", [])
    if not isinstance(migrations, list):
        raise PassportError("passport migration history is invalid")
    if not any(
        isinstance(item, dict) and "lineage_authorization_sha256" in item
        for item in migrations
    ):
        value["migration_history"] = list(migrations)
    signed = authenticate(value, secret)
    write_atomic(destination, signed)
    return signed


def completion_manifest(
    args: argparse.Namespace,
) -> tuple[Path, dict[str, str]]:
    matches = []
    for path in sorted((args.factory_root / "factory/runs").glob("*.meta")):
        value = manifest_fields(path)
        if (
            value.get("ticket") == args.ticket
            and value.get("transition_receipt_sha256") == args.receipt
        ):
            matches.append((path, value))
    if len(matches) != 1 or matches[0][0].stem != args.run_id:
        raise PassportError("completion correction run evidence is ambiguous")
    return matches[0]


def commit_parent(workdir: Path, revision: str) -> str:
    values = git(workdir, "rev-list", "--parents", "-n", "1", revision).split()
    if len(values) != 2 or values[0] != revision or not SHA.fullmatch(values[1]):
        raise PassportError("model identity recovery topology is invalid")
    return values[1]


def git_blob(workdir: Path, revision: str, path: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(workdir), "show", f"{revision}:{path}"],
        capture_output=True, check=False, timeout=120,
    )
    if result.returncode:
        raise PassportError("model identity recovery topology is invalid")
    return result.stdout


def merged_ticket_blob(
    workdir: Path, base: str, current: str, other: str, path: str
) -> bytes:
    with tempfile.TemporaryDirectory(prefix="factory-passport-merge-") as root:
        files = []
        for name, revision in (("current", current), ("base", base), ("other", other)):
            destination = Path(root) / name
            destination.write_bytes(git_blob(workdir, revision, path))
            files.append(str(destination))
        result = subprocess.run(
            ["git", "merge-file", "-p", *files],
            capture_output=True, check=False, timeout=120,
        )
    if result.returncode:
        raise PassportError("model identity recovery topology is invalid")
    return result.stdout


def active_journal_selection(journal: dict[str, Any], role: str) -> dict[str, Any]:
    for revision in reversed(journal["revisions"]):
        body = revision["body"]
        if "new_resolution" in body:
            return body["new_resolution"]["selections"][role]
        if "prior_resolution" in body:
            return body["prior_resolution"]["selections"][role]
        if body.get("kind") == "migration":
            return body["historical_selections"][role]
    raise KeyError(role)


def route_selection(plan: dict[str, Any], role: str) -> dict[str, Any]:
    if plan.get("schema") == "ticket-model-route-plan/v1":
        value = plan["resolution"]["selections"][role]
    elif plan.get("schema") == "ticket-model-route-journal/v2":
        value = active_journal_selection(plan, role)
    else:
        raise KeyError(role)
    if not isinstance(value, dict):
        raise KeyError(role)
    return value


def transition_context(
    args: argparse.Namespace, terminal: dict[str, str], current: dict[str, Any]
) -> dict[str, Any]:
    path = args.state_dir / f"{args.ticket}.json"
    value = json.loads(read_regular(path, 0o600), object_pairs_hook=unique_object)
    immutable = {
        name: item for name, item in value.items()
        if name not in {"consumed", "consumed_at_epoch", "receipt_sha256"}
    }
    digest = value.get("receipt_sha256", "") if isinstance(value, dict) else ""
    if (
        not isinstance(value, dict)
        or value.get("schema") != RECEIPT_SCHEMA
        or digest != hashlib.sha256(canonical(immutable)).hexdigest()
        or not isinstance(value.get("consumed"), bool)
    ):
        raise PassportError("model identity transition receipt is invalid")
    if digest == args.receipt and value["consumed"] is True:
        return value
    route = route_digest(args.workdir, args.ticket)
    if any((
        value["consumed"] is not False,
        value.get("parent_digest") != args.receipt,
        value.get("stage")
        != "REFUSE ticket Kit-SHA lease does not match the selected kit SHA",
        value.get("role") is not None,
        value.get("ticket") != args.ticket,
        value.get("project") != args.project,
        value.get("branch") != current["branch"],
        value.get("product_origin_sha256") != current["product_origin_sha256"],
        value.get("factory_sha") != args.factory_sha,
        value.get("contract_version") != args.contract_version,
        value.get("head_sha") != current["head_sha"],
        value.get("head_tree") != current["head_tree"],
        value.get("ticket_blob") != current["ticket_blob"],
        value.get("route_plan_sha256") != route,
    )):
        raise PassportError("model identity transition lineage is invalid")
    input_head = terminal.get("role_head_before", "")
    if not SHA.fullmatch(input_head):
        raise PassportError("model identity transition lineage is invalid")
    return {
        "branch": current["branch"],
        "contract_version": args.contract_version,
        "factory_sha": terminal.get("kit_sha"),
        "head_sha": input_head,
        "head_tree": git(args.workdir, "rev-parse", f"{input_head}^{{tree}}"),
        "passport_sha256": value.get("passport_sha256"),
        "product_origin_sha256": current["product_origin_sha256"],
        "project": args.project,
        "receipt_sha256": args.receipt,
        "role": terminal.get("role"),
        "route_plan_sha256": terminal.get("route_plan_sha256"),
        "stage": f"RUN {terminal.get('role', '')}",
        "ticket": args.ticket,
        "ticket_blob": git(
            args.workdir, "rev-parse",
            f"{input_head}:factory/tickets/{args.ticket}.md",
        ),
    }


def direct_model_output_topology(
    args: argparse.Namespace, terminal: dict[str, str], current: dict[str, Any]
) -> dict[str, Any]:
    input_head = terminal.get("role_head_before", "")
    current_head = current["head_sha"]
    ticket_path = f"factory/tickets/{args.ticket}.md"
    route_path = f"factory/route-plans/{args.ticket}.json"
    if (
        not SHA.fullmatch(input_head)
        or subprocess.run(
            ["git", "-C", str(args.workdir), "merge-base", "--is-ancestor",
             input_head, current_head],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            check=False, timeout=120,
        ).returncode != 0
    ):
        raise PassportError("model identity recovery topology is invalid")
    commits = git(
        args.workdir, "rev-list", "--reverse", "--ancestry-path",
        f"{input_head}..{current_head}",
    ).splitlines()
    if len(commits) > 64:
        raise PassportError("model identity recovery topology is invalid")
    parent = input_head
    changed: list[list[str]] = []
    for commit in commits:
        if commit_parent(args.workdir, commit) != parent:
            raise PassportError("model identity recovery topology is invalid")
        changed.append(sorted(git(
            args.workdir, "diff-tree", "--no-commit-id", "--name-only",
            "--no-renames", "-r", commit,
        ).splitlines()))
        parent = commit
    control = sorted((ticket_path, route_path))
    split = next(
        (index for index, paths in enumerate(changed)
         if paths == control and all(item == control for item in changed[index:])),
        len(commits),
    )
    output_commits = commits[:split]
    control_commits = commits[split:]
    role = terminal.get("role", "")
    if (
        role not in RECOVERABLE_ROLES
        or len(output_commits) > 32
        or len(control_commits) > 32
        or (role == "reviewer" and output_commits)
        or (role != "reviewer" and not output_commits)
    ):
        raise PassportError("model identity recovery topology is invalid")
    output_head = output_commits[-1] if output_commits else input_head
    previous_route = git_blob(args.workdir, output_head, route_path)
    previous_ticket = git_blob(args.workdir, output_head, ticket_path)
    for commit in control_commits:
        route_raw = git_blob(args.workdir, commit, route_path)
        ticket_raw = git_blob(args.workdir, commit, ticket_path)
        try:
            plan = json.loads(route_raw, object_pairs_hook=unique_object)
            kit = plan["kit_sha"]
            leases = re.findall(
                rb"^Kit-SHA:\s*([0-9a-f]{40})\s*$", ticket_raw, re.M,
            )
        except (KeyError, TypeError, ValueError) as error:
            raise PassportError("model identity recovery topology is invalid") from error
        checks = {
            "schema": plan.get("schema") == "ticket-model-route-journal/v2",
            "ticket": plan.get("ticket") == args.ticket,
            "kit": SHA.fullmatch(kit) is not None and leases == [kit.encode()],
            "route": route_raw != previous_route
            and journal_extends(previous_route, route_raw),
            "ticket_delta": exact_kit_sha_change(previous_ticket, ticket_raw),
            "ticket_mode": git(
                args.workdir, "ls-tree", commit, ticket_path
            ).split()[0] == "100644",
            "route_mode": git(
                args.workdir, "ls-tree", commit, route_path
            ).split()[0] == "100644",
        }
        if not all(checks.values()):
            failed = next(name for name, ok in checks.items() if not ok)
            raise PassportError(
                f"model identity recovery topology is invalid: {failed}"
            )
        previous_route = route_raw
        previous_ticket = ticket_raw
    try:
        validate_route(args.factory_root, args.workdir, args.ticket, args.factory_sha)
    except RouteEvidenceError as error:
        raise PassportError("model identity route evidence is invalid") from error
    try:
        policy_raw = json.loads(read_regular(
            Path(__file__).resolve().parent
            / "model-routing/handoff-boundaries-v1.json"
        ))
        policy = RoleBoundaryPolicy.from_dict(json.loads(
            json.dumps(policy_raw, sort_keys=True).replace("TICKET", args.ticket)
        ))
        _validate_committed_changes(
            args.workdir, input_head, output_head, role, policy,
            allow_spec_lint_append=True,
        )
    except (HandoffError, KeyError, TypeError, ValueError) as error:
        raise PassportError("model identity role output is invalid") from error
    if output_head != input_head:
        sentinel = subprocess.run(
            [sys.executable,
             str(Path(__file__).resolve().parent / "lib/lane-path-sentinel.py"),
             str(args.workdir), input_head, output_head],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            check=False, timeout=120,
        )
        if sentinel.returncode:
            raise PassportError("model identity role output is invalid")
    return {
        "control_commit_count": len(control_commits),
        "input_head": input_head,
        "output_head": output_head,
    }


def direct_model_identity_evidence(
    args: argparse.Namespace, current: dict[str, Any]
) -> dict[str, Any]:
    manifest, terminal = completion_manifest(args)
    output_path = manifest.with_suffix(".out")
    output_digest = role_output_digest(output_path)
    progress_events, progress_digest = successful_progress(
        manifest.with_suffix(".progress.jsonl")
    )
    transition = transition_context(args, terminal, current)
    topology = direct_model_output_topology(args, terminal, current)
    route_path = f"factory/route-plans/{args.ticket}.json"
    source_raw = git_blob(args.workdir, topology["input_head"], route_path)
    try:
        source_plan = json.loads(source_raw, object_pairs_hook=unique_object)
        selection = route_selection(source_plan, terminal["role"])
        catalog = json.loads(read_regular(
            Path(__file__).resolve().parent / "model-routing/catalog-v1.json"
        ), object_pairs_hook=unique_object)
        routes = [
            item for item in catalog["routes"]
            if isinstance(item, dict)
            and item.get("route_id") == terminal.get("route_id")
        ]
    except (KeyError, TypeError, ValueError) as error:
        raise PassportError("model identity recovery route evidence is invalid") from error
    if len(routes) != 1:
        raise PassportError("model identity recovery route evidence is invalid")
    route = routes[0]
    raw = read_regular(output_path, maximum=8 * 1024 * 1024)
    models, successes = [], 0
    for line in raw.splitlines():
        try:
            value = json.loads(line, object_pairs_hook=unique_object)
        except (TypeError, ValueError):
            continue
        if not isinstance(value, dict):
            continue
        if value.get("type") == "system" and value.get("subtype") == "init":
            models.append(value.get("model"))
        if value.get("type") == "result" and value.get("subtype") == "success":
            successes += 1
    actual = models[0] if len(models) == 1 and isinstance(models[0], str) else ""
    canonical_identity = route.get("expected_reported_identity", "")
    diagnostic = f"cursor reported unapproved model: {actual}".encode()
    manifest_route_fields = {
        "account_route_id": "account_route_id",
        "adapter": "adapter",
        "gateway_id": "gateway_id",
        "inference_provider_id": "inference_provider_id",
        "model_id": "selection_id",
        "provider_family": "provider_family",
        "route_id": "route_id",
        "transport": "transport",
    }
    if any((
        terminal.get("ticket") != args.ticket,
        terminal.get("run_id") != args.run_id,
        terminal.get("phase") != "completed",
        terminal.get("accounting_state") != "abandoned_conservative",
        terminal.get("go_issued") != "1",
        terminal.get("task_submitted") != "1",
        terminal.get("exit_status") != "9",
        terminal.get("role_exit") != "provider_failed",
        terminal.get("terminal_reason_code", "") != "",
        terminal.get("role") not in RECOVERABLE_ROLES,
        terminal.get("adapter") not in {"cursor-anthropic", "cursor-openai"},
        terminal.get("role_branch_before") != current["branch"],
        terminal.get("role_remote_before") != topology["input_head"],
        terminal.get("transition_receipt_sha256") != args.receipt,
        terminal.get("kit_sha") != source_plan.get("kit_sha"),
        terminal.get("contract_version") != args.contract_version,
        terminal.get("cost_basis") != "conservative_reservation",
        terminal.get("effective_cost") != terminal.get("reserved_usd"),
        micro_usd(terminal) <= 0,
        terminal.get("output_sha256") != output_digest,
        terminal.get("progress_events") != str(progress_events),
        terminal.get("progress_journal_sha256") != progress_digest,
        terminal.get("route_plan_sha256") != hashlib.sha256(source_raw).hexdigest(),
        transition.get("head_sha") != topology["input_head"],
        transition.get("role") != terminal.get("role"),
        transition.get("stage") != f"RUN {terminal.get('role', '')}",
        transition.get("factory_sha") != terminal.get("kit_sha"),
        transition.get("route_plan_sha256") != terminal.get("route_plan_sha256"),
        route.get("enabled") is not True,
        not isinstance(canonical_identity, str) or not canonical_identity,
        selection.get("reported_identity") != canonical_identity,
        selection.get("effort") != terminal.get("effort"),
        selection.get("adapter_version") != terminal.get("adapter_version"),
        terminal.get("selection_reason") not in {"pinned_route_plan", "route_journal"},
        any(
            terminal.get(manifest_name) != route.get(route_name)
            or selection.get(route_name) != route.get(route_name)
            for manifest_name, route_name in manifest_route_fields.items()
        ),
        actual not in approved_reported_models(
            route.get("selection_id", ""), canonical_identity,
        ),
        successes != 1,
        raw.splitlines().count(diagnostic) != 1,
        raw.splitlines().count(b"Cursor output validation/redaction failed") != 1,
    )):
        raise PassportError("run is not the typed model-identity success failure")
    if terminal["role"] == "reviewer":
        verdict = subprocess.run(
            [sys.executable,
             str(Path(__file__).resolve().parent / "lib/reviewer-verdict.py"),
             "--adapter", terminal["adapter"], "--input", str(output_path),
             "--contract-version", args.contract_version],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            check=False, timeout=120,
        )
        if verdict.returncode:
            raise PassportError("model identity Reviewer output is invalid")
    return {
        "manifest_digest": hashlib.sha256(read_regular(manifest)).hexdigest(),
        "output_digest": output_digest,
        "progress_digest": progress_digest,
        "progress_events": progress_events,
        "reported_identity": actual,
        "terminal": terminal,
        "topology": topology,
        "transition": transition,
    }


def recover_model_identity_success(
    args: argparse.Namespace, secret: bytes
) -> dict[str, Any]:
    if git(args.workdir, "status", "--porcelain=v1", "-z"):
        raise PassportError("model identity recovery requires a clean execution cell")
    current = identity(args)
    evidence = direct_model_identity_evidence(args, current)
    terminal = evidence["terminal"]
    transition = evidence["transition"]
    passports = args.state_dir / "passports"
    if not passports.exists() and not passports.is_symlink():
        passports.mkdir(mode=0o700)
    passports = safe_directory(passports)
    destination = passports / f"{args.ticket}.json"
    previous: dict[str, Any] = {}
    parent_raw = None
    if destination.exists() or destination.is_symlink():
        previous, parent_raw = load_passport(destination, secret)
    role = terminal["role"]
    migrated_parent = (
        bool(previous)
        and transition.get("factory_sha") != args.factory_sha
        and migrated_receipt_lineage(args, previous, transition, current)
    )
    existing_corrections = previous.get("completed_role_corrections", [])
    if any(
        isinstance(item, dict)
        and item.get("schema") == PASSPORTLESS_MODEL_CORRECTION_SCHEMA
        and item.get("run_id") == args.run_id
        and item.get("transition_receipt_sha256") == args.receipt
        for item in existing_corrections
    ):
        if (
            any(previous.get(name) != item for name, item in current.items())
            or previous.get("factory_sha") != args.factory_sha
            or previous.get("current_stage") != f"RUN {role}"
            or previous.get("transition_receipt_sha256") != args.receipt
        ):
            raise PassportError("model identity correction conflicts with prior evidence")
        validate_completion_corrections(
            existing_corrections, previous.get("completed_role_evidence", [])
        )
        return previous
    if previous:
        if any((
            transition.get("passport_sha256") != parent_raw
            and not migrated_parent,
            previous.get("ticket") != args.ticket,
            previous.get("project") != args.project,
            previous.get("branch") != current["branch"],
            previous.get("product_origin_sha256") != current["product_origin_sha256"],
            previous.get("contract_version") != args.contract_version,
            previous.get("factory_sha") != args.factory_sha,
            subprocess.run(
                ["git", "-C", str(args.workdir), "merge-base", "--is-ancestor",
                 previous.get("head_sha", ""), current["head_sha"]],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                check=False, timeout=120,
            ).returncode != 0,
        )):
            raise PassportError("model identity passport lineage is invalid")
    elif role != "planner" or transition.get("passport_sha256") is not None:
        raise PassportError("passportless model identity recovery is not a first role")
    normal_completed, charges = run_evidence(
        args.factory_root / "factory", args.ticket
    )
    matching_charges = [
        item for item in charges
        if item.get("run_id") == args.run_id
        and item.get("transition_receipt_sha256") == args.receipt
        and item.get("role") == role
        and item.get("head_before") == evidence["topology"]["input_head"]
        and item.get("factory_sha") == terminal.get("kit_sha")
        and item.get("contract_version") == args.contract_version
        and item.get("manifest_sha256") == evidence["manifest_digest"]
    ]
    if len(matching_charges) != 1:
        raise PassportError("authenticated run charge is missing")
    charges = merge_records(previous.get("charge_records", []), charges)
    completed = merge_records(
        previous.get("completed_role_evidence", []), normal_completed
    )
    completed_record = {
        "contract_version": args.contract_version,
        "factory_sha": terminal["kit_sha"],
        "head_before": evidence["topology"]["input_head"],
        "manifest_sha256": evidence["manifest_digest"],
        "output_sha256": evidence["output_digest"],
        "role": role,
        "run_id": args.run_id,
        "transition_receipt_sha256": args.receipt,
    }
    completed = merge_records(completed, [completed_record])
    corrections = previous.get("completed_role_corrections", [])
    if not isinstance(corrections, list):
        raise PassportError("completion correction history is invalid")
    correction = {
        "failed_factory_sha": terminal["kit_sha"],
        "issue": MODEL_IDENTITY_CORRECTION_ISSUE,
        "output_head_sha": evidence["topology"]["output_head"],
        "progress_events": evidence["progress_events"],
        "progress_journal_sha256": evidence["progress_digest"],
        "receipt_parent_file_sha256": parent_raw,
        "recovery_factory_sha": args.factory_sha,
        "role": role,
        "run_id": args.run_id,
        "schema": PASSPORTLESS_MODEL_CORRECTION_SCHEMA,
        "transition_receipt_sha256": args.receipt,
    }
    corrections = validate_completion_corrections(
        [*corrections, correction], completed
    )
    protected = git(args.factory_root, "rev-parse", "origin/main")
    history = list(previous.get("factory_release_history", []))
    for factory_sha in (terminal["kit_sha"], args.factory_sha):
        item = {"contract_version": args.contract_version, "factory_sha": factory_sha}
        if item not in history:
            history.append(item)
    value = {
        **current,
        "base_history": [
            *previous.get("base_history", []),
            *([] if protected in previous.get("base_history", []) else [protected]),
        ],
        "charge_records": charges,
        "completed_role_corrections": corrections,
        "completed_role_evidence": completed,
        "contract_version": args.contract_version,
        "cumulative_charges_micro_usd": sum(
            item["charge_micro_usd"] for item in charges
        ),
        "current_stage": f"RUN {role}",
        "current_state": ticket_state(args.workdir, args.ticket),
        "factory_release_history": history,
        "factory_sha": args.factory_sha,
        "migration_history": list(previous.get("migration_history", [])),
        "nonce": secrets.token_hex(16),
        "parent_digest": previous.get("passport_sha256"),
        "parent_file_sha256": parent_raw,
        "protected_base_sha": protected,
        "publication_state": previous.get("publication_state", "none"),
        "route_plan_sha256": route_digest(args.workdir, args.ticket),
        "schema": SCHEMA,
        "transition_receipt_sha256": args.receipt,
    }
    signed = authenticate(value, secret)
    write_atomic(destination, signed)
    return signed


def model_identity_recovery_topology(
    args: argparse.Namespace, consumed: dict[str, Any], current: dict[str, Any]
) -> dict[str, str]:
    input_head = consumed.get("head_sha", "")
    current_head = current["head_sha"]
    ticket_path = f"factory/tickets/{args.ticket}.md"
    route_path = f"factory/route-plans/{args.ticket}.json"

    def tree(revision: str) -> str:
        return git(args.workdir, "rev-parse", f"{revision}^{{tree}}")

    def changed(revision: str) -> list[str]:
        return git(
            args.workdir, "diff-tree", "--no-commit-id", "--name-only",
            "--no-renames", "-r", revision,
        ).splitlines()

    candidates = []
    for status, migration_head, restore_head in (
        ("restore-required", current_head, ""),
        ("restored", commit_parent(args.workdir, current_head), current_head),
    ):
        try:
            migration_count = 0
            revert_head = migration_head
            while (
                migration_count < 32
                and changed(revert_head) == [route_path, ticket_path]
            ):
                migration_count += 1
                revert_head = commit_parent(args.workdir, revert_head)
            output_head = commit_parent(args.workdir, revert_head)
            if (
                migration_count
                and commit_parent(args.workdir, output_head) == input_head
            ):
                candidates.append((
                    status, migration_head, migration_count, revert_head,
                    output_head, restore_head,
                ))
        except PassportError:
            pass

    valid = []
    for (
        status, migration_head, migration_count, revert_head, output_head,
        restore_head,
    ) in candidates:
        try:
            input_ticket = git_blob(args.workdir, input_head, ticket_path)
            output_ticket = git_blob(args.workdir, output_head, ticket_path)
            migration_ticket = git_blob(
                args.workdir, migration_head, ticket_path
            )
            restored_ticket = merged_ticket_blob(
                args.workdir, input_head, migration_head, output_head,
                ticket_path,
            )
            if (
                tree(revert_head) == tree(input_head)
                and tree(output_head) != tree(input_head)
                and output_ticket != input_ticket
                and restored_ticket != migration_ticket
                and changed(output_head) == [ticket_path]
                and changed(revert_head) == [ticket_path]
                and changed(migration_head) == [route_path, ticket_path]
                and git_blob(args.workdir, migration_head, route_path)
                != git_blob(args.workdir, revert_head, route_path)
                and (
                    status == "restore-required"
                    or (
                        changed(restore_head) == [ticket_path]
                        and git_blob(args.workdir, restore_head, ticket_path)
                        == restored_ticket
                    )
                )
            ):
                valid.append({
                    "migration_head": migration_head,
                    "migration_count": migration_count,
                    "input_head": input_head,
                    "output_head": output_head,
                    "output_tree": tree(output_head),
                    "recovery_base_head": migration_head,
                    "restore_head": restore_head,
                    "revert_head": revert_head,
                    "status": status,
                })
        except PassportError:
            continue
    if len(valid) != 1:
        raise PassportError("model identity recovery topology is invalid")
    return valid[0]


def model_identity_success_evidence(
    args: argparse.Namespace, consumed: dict[str, Any], current: dict[str, Any]
) -> dict[str, Any]:
    manifest, terminal = completion_manifest(args)
    output_path = manifest.with_suffix(".out")
    output_digest = role_output_digest(output_path)
    progress_events, progress_digest = successful_progress(
        manifest.with_suffix(".progress.jsonl")
    )
    topology = model_identity_recovery_topology(args, consumed, current)
    route_path = f"factory/route-plans/{args.ticket}.json"
    try:
        current_plan = json.loads(
            read_regular(
                args.workdir / route_path
            ),
            object_pairs_hook=unique_object,
        )
        source_plan_raw = git_blob(
            args.workdir, topology["revert_head"], route_path
        )
        source_plan = json.loads(
            source_plan_raw, object_pairs_hook=unique_object
        )
        catalog = json.loads(
            read_regular(
                Path(__file__).resolve().parent / "model-routing/catalog-v1.json"
            ),
            object_pairs_hook=unique_object,
        )
        source_selection = source_plan["resolution"]["selections"][
            "spec-linter"
        ]
        current_revision = current_plan["revisions"][-1]["body"]
        current_selection = active_journal_selection(current_plan, "spec-linter")
        routes = [
            item for item in catalog["routes"]
            if isinstance(item, dict)
            and item.get("route_id") == terminal.get("route_id")
        ]
    except (KeyError, TypeError, ValueError) as error:
        raise PassportError("model identity recovery route evidence is invalid") from error
    if len(routes) != 1:
        raise PassportError("model identity recovery route evidence is invalid")
    route = routes[0]
    planned_identity = source_selection.get("reported_identity")

    raw = read_regular(output_path, maximum=8 * 1024 * 1024)
    models = []
    successes = 0
    for line in raw.splitlines():
        try:
            value = json.loads(line, object_pairs_hook=unique_object)
        except (TypeError, ValueError):
            continue
        if not isinstance(value, dict):
            continue
        if value.get("type") == "system" and value.get("subtype") == "init":
            models.append(value.get("model"))
        if value.get("type") == "result" and value.get("subtype") == "success":
            successes += 1
    expected_identity = route.get("expected_reported_identity")
    diagnostic = f"cursor reported unapproved model: {expected_identity}".encode()
    if (
        terminal.get("run_id") != args.run_id
        or terminal.get("phase") != "completed"
        or terminal.get("accounting_state") != "abandoned_conservative"
        or terminal.get("go_issued") != "1"
        or terminal.get("task_submitted") != "1"
        or terminal.get("exit_status") != "9"
        or terminal.get("role_exit") != "provider_failed"
        or terminal.get("terminal_reason_code", "") != ""
        or terminal.get("role") != "spec-linter"
        or terminal.get("adapter") != "cursor-anthropic"
        or terminal.get("role_branch_before") != current["branch"]
        or terminal.get("role_head_before") != consumed["head_sha"]
        or terminal.get("kit_sha") != consumed["factory_sha"]
        or terminal.get("contract_version") != args.contract_version
        or terminal.get("cost_basis") != "conservative_reservation"
        or terminal.get("effective_cost") != terminal.get("reserved_usd")
        or micro_usd(terminal) <= 0
        or terminal.get("output_sha256") != output_digest
        or terminal.get("progress_events") != str(progress_events)
        or terminal.get("progress_journal_sha256") != progress_digest
        or terminal.get("route_plan_sha256")
        != consumed.get("route_plan_sha256")
        or terminal.get("route_plan_sha256")
        != hashlib.sha256(source_plan_raw).hexdigest()
        or route.get("enabled") is not True
        or route.get("adapter") != terminal.get("adapter")
        or source_selection.get("route_id") != terminal.get("route_id")
        or source_selection.get("adapter") != terminal.get("adapter")
        or source_plan.get("kit_sha") != terminal.get("kit_sha")
        or current_plan.get("schema") != "ticket-model-route-journal/v2"
        or current_plan.get("kit_sha") != args.factory_sha
        or current_revision.get("kind") != "release-migration"
        or current_revision.get("new_kit_sha") != args.factory_sha
        or current_selection.get("route_id") != terminal.get("route_id")
        or current_selection.get("adapter") != terminal.get("adapter")
        or current_selection.get("reported_identity") != expected_identity
        or not isinstance(expected_identity, str)
        or not expected_identity
        or not isinstance(planned_identity, str)
        or not planned_identity
        or planned_identity == expected_identity
        or models != [expected_identity]
        or successes != 1
        or raw.splitlines().count(diagnostic) != 1
        or raw.splitlines().count(b"Cursor output validation/redaction failed") != 1
    ):
        raise PassportError("run is not the typed model-identity success failure")
    return {
        "manifest": manifest,
        "manifest_digest": hashlib.sha256(read_regular(manifest)).hexdigest(),
        "output_digest": output_digest,
        "progress_digest": progress_digest,
        "progress_events": progress_events,
        "reported_identity": expected_identity,
        "terminal": terminal,
        "topology": topology,
    }


def verify_model_identity_success(
    args: argparse.Namespace, secret: bytes
) -> dict[str, Any]:
    previous, _ = load_passport(
        safe_directory(args.state_dir / "passports") / f"{args.ticket}.json",
        secret,
    )
    if git(args.workdir, "status", "--porcelain=v1", "-z"):
        raise PassportError("model identity recovery requires a clean execution cell")
    current = identity(args)
    consumed = receipt(args.state_dir, args.ticket, args.receipt)
    evidence = model_identity_success_evidence(args, consumed, current)
    topology = evidence["topology"]
    lineage_identity = dict(current)
    lineage_identity["head_sha"] = previous.get("head_sha", "")
    lineage_identity["head_tree"] = git(
        args.workdir, "rev-parse", f"{lineage_identity['head_sha']}^{{tree}}"
    )
    stage_is_parent = (
        previous.get("current_stage") == "RUN planner"
        and previous.get("transition_receipt_sha256") == consumed.get("parent_digest")
    )
    stage_is_current = (
        previous.get("current_stage") == "RUN spec-linter"
        and previous.get("transition_receipt_sha256") == args.receipt
    )
    if (
        previous.get("ticket") != args.ticket
        or previous.get("project") != args.project
        or previous.get("branch") != current["branch"]
        or previous.get("product_origin_sha256")
        != current["product_origin_sha256"]
        or previous.get("head_sha") not in {
            topology["recovery_base_head"], current["head_sha"],
        }
        or previous.get("route_plan_sha256") != route_digest(args.workdir, args.ticket)
        or previous.get("current_state") != ticket_state(args.workdir, args.ticket)
        or previous.get("contract_version") != args.contract_version
        or previous.get("factory_sha") != args.factory_sha
        or not (stage_is_parent or stage_is_current)
        or consumed.get("ticket") != args.ticket
        or consumed.get("project") != args.project
        or consumed.get("branch") != current["branch"]
        or consumed.get("product_origin_sha256")
        != current["product_origin_sha256"]
        or consumed.get("role") != "spec-linter"
        or consumed.get("stage") != "RUN spec-linter"
        or consumed.get("contract_version") != args.contract_version
        or consumed.get("receipt_sha256") != args.receipt
        or consumed.get("factory_sha") == args.factory_sha
        or not migrated_receipt_lineage(
            args, previous, consumed, lineage_identity
        )
    ):
        raise PassportError("model identity recovery is outside authenticated lineage")
    return {
        **{
            name: item for name, item in topology.items() if name != "status"
        },
        "recovery_status": topology["status"],
        "reported_identity": evidence["reported_identity"],
        "run_id": args.run_id,
        "schema": SCHEMA,
        "status": "ok",
        "ticket": args.ticket,
    }


def correct_converged_success(
    args: argparse.Namespace, secret: bytes
) -> dict[str, Any]:
    """Recover one exact, authenticated success misclassified by an old release."""
    passports = safe_directory(args.state_dir / "passports")
    destination = passports / f"{args.ticket}.json"
    previous, parent_raw = load_passport(destination, secret)
    if git(args.workdir, "status", "--porcelain=v1", "-z"):
        raise PassportError("completion correction requires a clean execution cell")
    current = identity(args)
    consumed = receipt(args.state_dir, args.ticket, args.receipt)
    current_route = route_digest(args.workdir, args.ticket)
    corrected_role = consumed.get("role", "")
    model_identity_success = corrected_role == "spec-linter"
    corrected_stage = f"RUN {corrected_role}"
    correction_issue = (
        MODEL_IDENTITY_CORRECTION_ISSUE
        if model_identity_success else COMPLETION_CORRECTION_ISSUE
    )
    prior_corrections = previous.get("completed_role_corrections", [])
    if not isinstance(prior_corrections, list):
        raise PassportError("completion correction history is invalid")
    prior_correction_bound = any(
        isinstance(item, dict)
        and item.get("schema") == COMPLETION_CORRECTION_SCHEMA
        and item.get("run_id") == args.run_id
        and item.get("transition_receipt_sha256") == args.receipt
        and item.get("receipt_parent_file_sha256")
        == consumed.get("passport_sha256")
        for item in prior_corrections
    )
    receipt_lineage = (
        consumed.get("passport_sha256") == previous.get("parent_file_sha256")
        or migrated_receipt_lineage(args, previous, consumed, current)
        or converged_success_migration_lineage(
            args, previous, consumed, current
        )
        or prior_correction_bound
    )
    if (
        any(previous.get(name) != item for name, item in current.items())
        or previous.get("route_plan_sha256") != current_route
        or previous.get("current_state") != ticket_state(args.workdir, args.ticket)
        or previous.get("current_stage") != corrected_stage
        or previous.get("transition_receipt_sha256") != args.receipt
        or previous.get("contract_version") != args.contract_version
        or previous.get("factory_sha") != args.factory_sha
        or consumed.get("ticket") != args.ticket
        or consumed.get("project") != args.project
        or consumed.get("branch") != current["branch"]
        or consumed.get("product_origin_sha256")
        != current["product_origin_sha256"]
        or consumed.get("role") != corrected_role
        or consumed.get("stage") != corrected_stage
        or consumed.get("contract_version") != args.contract_version
        or consumed.get("receipt_sha256") != args.receipt
        or not receipt_lineage
        or not SHA.fullmatch(consumed.get("factory_sha", ""))
        or consumed["factory_sha"] == args.factory_sha
        or not SHA.fullmatch(consumed.get("head_sha", ""))
        or consumed.get("head_tree")
        != git(args.workdir, "rev-parse", f"{consumed.get('head_sha')}^{{tree}}")
        or consumed["head_sha"] == current["head_sha"]
        or subprocess.run(
            [
                "git", "-C", str(args.workdir), "merge-base", "--is-ancestor",
                consumed["head_sha"], current["head_sha"],
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        ).returncode != 0
    ):
        raise PassportError("completion correction is outside authenticated lineage")

    if not RUN_ID.fullmatch(args.run_id):
        raise PassportError("completion correction run identity is invalid")
    manifest, terminal = completion_manifest(args)
    if model_identity_success:
        typed = model_identity_success_evidence(args, consumed, current)
        if typed["topology"]["status"] != "restored":
            raise PassportError("model identity success has not been restored")
        manifest_digest = typed["manifest_digest"]
        output_digest = typed["output_digest"]
        progress_events = typed["progress_events"]
        progress_digest = typed["progress_digest"]
    else:
        manifest_digest = hashlib.sha256(read_regular(manifest)).hexdigest()
        output_digest = role_output_digest(manifest.with_suffix(".out"))
        progress_events, progress_digest = successful_progress(
            manifest.with_suffix(".progress.jsonl")
        )
        if (
            terminal.get("run_id") != args.run_id
            or terminal.get("phase") != "abandoned"
            or terminal.get("accounting_state") != "abandoned_conservative"
            or terminal.get("go_issued") != "1"
            or terminal.get("task_submitted") != "1"
            or terminal.get("exit_status") != "128"
            or terminal.get("role_exit") != ""
            or terminal.get("terminal_reason_code", "") != ""
            or terminal.get("role") != "builder"
            or terminal.get("adapter") not in {
                "cursor-anthropic", "cursor-openai",
            }
            or terminal.get("role_branch_before") != current["branch"]
            or terminal.get("role_head_before") != consumed["head_sha"]
            or terminal.get("kit_sha") != consumed["factory_sha"]
            or terminal.get("contract_version") != args.contract_version
            or terminal.get("cost_basis") != "conservative_reservation"
            or terminal.get("effective_cost") != terminal.get("reserved_usd")
            or micro_usd(terminal) <= 0
            or terminal.get("output_sha256") != output_digest
            or terminal.get("progress_events", "") != ""
            or terminal.get("progress_journal_sha256", "") != ""
        ):
            raise PassportError("run is not the typed converged-success failure")

    charges = run_charges(args.factory_root / "factory", args.ticket)
    expected_charge = next(
        (item for item in charges if item.get("run_id") == args.run_id), None
    )
    passport_charges = previous.get("charge_records")
    completed = previous.get("completed_role_evidence")
    corrections = previous.get("completed_role_corrections", [])
    if (
        expected_charge is None
        or not isinstance(passport_charges, list)
        or passport_charges.count(expected_charge) != 1
        or sum(
            isinstance(item, dict)
            and item.get("transition_receipt_sha256") == args.receipt
            for item in passport_charges
        ) != 1
        or not isinstance(completed, list)
        or not isinstance(corrections, list)
    ):
        raise PassportError("authenticated run charge is missing")
    corrections = validate_completion_corrections(corrections, completed)

    evidence = {
        "contract_version": args.contract_version,
        "factory_sha": consumed["factory_sha"],
        "head_before": consumed["head_sha"],
        "manifest_sha256": manifest_digest,
        "output_sha256": output_digest,
        "role": corrected_role,
        "run_id": args.run_id,
        "transition_receipt_sha256": args.receipt,
    }
    correction = {
        "failed_factory_sha": consumed["factory_sha"],
        "issue": correction_issue,
        "output_head_sha": current["head_sha"],
        "progress_events": progress_events,
        "progress_journal_sha256": progress_digest,
        "recovery_factory_sha": args.factory_sha,
        "receipt_parent_file_sha256": consumed["passport_sha256"],
        "run_id": args.run_id,
        "schema": COMPLETION_CORRECTION_SCHEMA,
        "transition_receipt_sha256": args.receipt,
    }
    matching_completed = [
        item for item in completed
        if isinstance(item, dict) and (
            item.get("run_id") == args.run_id
            or item.get("transition_receipt_sha256") == args.receipt
        )
    ]
    matching_corrections = [
        item for item in corrections
        if isinstance(item, dict) and (
            item.get("run_id") == args.run_id
            or item.get("transition_receipt_sha256") == args.receipt
        )
    ]
    if matching_completed or matching_corrections:
        if matching_completed == [evidence] and matching_corrections == [correction]:
            return previous
        raise PassportError("completion correction conflicts with prior evidence")
    corrected_evidence = [*completed, evidence]
    corrected_history = validate_completion_corrections(
        [*corrections, correction], corrected_evidence
    )

    value = {
        **{
            name: item for name, item in previous.items()
            if name not in {
                "authentication_sha256", "passport_sha256", "parent_digest",
                "parent_file_sha256", "nonce",
            }
        },
        "completed_role_corrections": corrected_history,
        "completed_role_evidence": corrected_evidence,
        "nonce": secrets.token_hex(16),
        "parent_digest": previous["passport_sha256"],
        "parent_file_sha256": parent_raw,
    }
    signed = authenticate(value, secret)
    write_atomic(destination, signed)
    return signed


def validate(args: argparse.Namespace, secret: bytes) -> dict[str, Any]:
    path = args.state_dir / "passports" / f"{args.ticket}.json"
    value, _ = load_passport(path, secret)
    current = identity(args)
    for name, item in current.items():
        if value.get(name) != item:
            raise PassportError("passport does not match this clean execution cell")
    if git(args.workdir, "status", "--porcelain=v1", "-z"):
        raise PassportError("passport validation requires a clean execution cell")
    if value.get("route_plan_sha256") != route_digest(args.workdir, args.ticket):
        raise PassportError("passport route plan changed")
    return value


def authorize_lineage(
    args: argparse.Namespace, secret: bytes
) -> dict[str, Any]:
    passports = safe_directory(args.state_dir / "passports")
    previous, parent_raw = load_passport(
        passports / f"{args.ticket}.json", secret
    )
    if git(args.workdir, "status", "--porcelain=v1", "-z"):
        raise PassportError("lineage authorization requires a clean execution cell")
    current = identity(args)
    protected = git(args.factory_root, "rev-parse", "origin/main")
    consumed = receipt(args.state_dir, args.ticket, args.receipt)
    value = lineage_authorization_value(
        args,
        previous,
        parent_raw,
        current,
        protected,
        route_digest(args.workdir, args.ticket) or "",
        consumed,
    )
    if (
        value is None
        or not isinstance(previous.get("migration_history"), list)
        or not any(
            valid_legacy_migration(item)
            for item in previous["migration_history"]
        )
    ):
        raise PassportError("passport lineage authorization is unavailable")
    return value


def migrate(args: argparse.Namespace, secret: bytes) -> dict[str, Any]:
    passports = safe_directory(args.state_dir / "passports")
    destination = passports / f"{args.ticket}.json"
    previous, parent_raw = load_passport(destination, secret)
    current = identity(args)
    protected = git(args.factory_root, "rev-parse", "origin/main")
    publication = (
        previous.get("publication_state", "none")
        if args.publication_state == "preserve"
        else args.publication_state
    )
    current_ticket_state = ticket_state(args.workdir, args.ticket)
    current_route = route_digest(args.workdir, args.ticket)
    head_continuous = subprocess.run(
        [
            "git", "-C", str(args.workdir), "merge-base", "--is-ancestor",
            previous.get("head_sha", ""), current["head_sha"],
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    ).returncode == 0
    rewrite_authorization = (
        None
        if head_continuous
        else authorized_ticket_rewrite(
            args, previous, current, current_ticket_state, protected
        )
    )
    base_continuous = subprocess.run(
        [
            "git", "-C", str(args.factory_root), "merge-base", "--is-ancestor",
            previous.get("protected_base_sha", ""), protected,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    ).returncode == 0
    if (
        previous.get("ticket") != args.ticket
        or previous.get("project") != args.project
        or previous.get("branch") != current["branch"]
        or previous.get("product_origin_sha256") != current["product_origin_sha256"]
        or not base_continuous
        or (
            not head_continuous
            and not authorized_inflight_rewrite(
                args, previous, current, current_ticket_state, protected
            )
            and rewrite_authorization is None
        )
    ):
        raise PassportError("passport migration is outside authenticated lineage")
    if (
        all(previous.get(name) == item for name, item in current.items())
        and previous.get("protected_base_sha") == protected
        and previous.get("factory_sha") == args.factory_sha
        and previous.get("contract_version") == args.contract_version
        and previous.get("current_state") == current_ticket_state
        and previous.get("publication_state") == publication
        and previous.get("route_plan_sha256") == current_route
    ):
        return previous
    history = list(previous["factory_release_history"])
    release = {
        "contract_version": args.contract_version,
        "factory_sha": args.factory_sha,
    }
    if release not in history:
        history.append(release)
    migration = {
        "from_factory_sha": previous["factory_sha"],
        "from_head_sha": previous["head_sha"],
        "from_passport_file_sha256": parent_raw,
        "from_passport_sha256": previous["passport_sha256"],
        "from_protected_base_sha": previous["protected_base_sha"],
        "from_route_plan_sha256": previous["route_plan_sha256"],
        "schema": MIGRATION_SCHEMA,
        "to_factory_sha": args.factory_sha,
        "to_head_sha": current["head_sha"],
        "to_protected_base_sha": protected,
        "to_route_plan_sha256": current_route,
    }
    migration.update(
        lineage_authorization_metadata(
            args, previous, parent_raw, current, protected
        )
    )
    if rewrite_authorization is not None:
        migration["rewrite_authorization_sha256"] = rewrite_authorization
    evidence: dict[str, Any] = {}
    if rewrite_authorization is not None:
        completed, charges = run_evidence(args.factory_root / "factory", args.ticket)
        completed = merge_records(
            previous.get("completed_role_evidence", []), completed
        )
        charges = merge_records(previous.get("charge_records", []), charges)
        evidence = {
            "charge_records": charges,
            "completed_role_evidence": completed,
            "cumulative_charges_micro_usd": sum(
                item["charge_micro_usd"] for item in charges
            ),
        }
    value = {
        **{
            name: item for name, item in previous.items()
            if name not in {
                "authentication_sha256", "passport_sha256", "parent_digest",
                "parent_file_sha256", "nonce",
            }
        },
        **current,
        **evidence,
        "base_history": [
            *previous.get("base_history", []),
            *([] if protected in previous.get("base_history", []) else [protected]),
        ],
        "contract_version": args.contract_version,
        "current_state": current_ticket_state,
        "factory_release_history": history,
        "factory_sha": args.factory_sha,
        "migration_history": [
            *previous.get("migration_history", []),
            migration,
        ],
        "nonce": secrets.token_hex(16),
        "parent_digest": previous["passport_sha256"],
        "parent_file_sha256": parent_raw,
        "protected_base_sha": protected,
        "publication_state": publication,
        "route_plan_sha256": current_route,
        "schema": SCHEMA,
    }
    signed = authenticate(value, secret)
    write_atomic(destination, signed)
    return signed


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "action", choices=(
            "authorize-lineage", "correct-converged-success", "export",
            "migrate", "recover-model-identity-success", "validate",
            "verify-model-identity-success",
        )
    )
    parser.add_argument("--factory-root", required=True, type=Path)
    parser.add_argument("--workdir", required=True, type=Path)
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--ticket", required=True)
    parser.add_argument("--contract-version", required=True)
    parser.add_argument("--factory-sha", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--receipt", default="")
    parser.add_argument("--run-id", default="")
    parser.add_argument(
        "--publication-state",
        choices=(
            "preserve", "none", "validating", "ready", "merge-pending",
            "merged", "repair",
        ),
        default="preserve",
    )
    args = parser.parse_args()
    try:
        if (
            not TICKET.fullmatch(args.ticket)
            or args.contract_version != "1.8.0"
            or not SHA.fullmatch(args.factory_sha)
            or (
                args.action in {
                    "authorize-lineage", "correct-converged-success", "export",
                    "recover-model-identity-success", "verify-model-identity-success",
                }
                and not DIGEST.fullmatch(args.receipt)
            )
            or (
                args.action in {
                    "correct-converged-success", "recover-model-identity-success",
                    "verify-model-identity-success",
                }
                and not RUN_ID.fullmatch(args.run_id)
            )
        ):
            raise PassportError("invalid passport arguments")
        args.factory_root = args.factory_root.resolve(strict=True)
        args.workdir = args.workdir.resolve(strict=True)
        args.state_dir = safe_directory(args.state_dir)
        secret = key(args.state_dir)
        if args.action == "authorize-lineage":
            value = authorize_lineage(args, secret)
        elif args.action == "correct-converged-success":
            value = correct_converged_success(args, secret)
        elif args.action == "recover-model-identity-success":
            value = recover_model_identity_success(args, secret)
        elif args.action == "verify-model-identity-success":
            value = verify_model_identity_success(args, secret)
        elif args.action == "export":
            value = export(args, secret)
        elif args.action == "migrate":
            value = migrate(args, secret)
        else:
            value = validate(args, secret)
        print(
            canonical(value).decode().rstrip()
            if args.action in {
                "authorize-lineage", "verify-model-identity-success",
            }
            else json.dumps({
                "passport": value["passport_sha256"],
                "schema": SCHEMA,
                "status": "ok",
                "ticket": args.ticket,
            }, sort_keys=True)
        )
    except PassportError as error:
        print(json.dumps({
            "error": str(error), "error_kind": "evidence",
            "schema": SCHEMA, "status": "error",
            "ticket": args.ticket,
        }, sort_keys=True))
        raise SystemExit(1)
    except (
        FileNotFoundError, json.JSONDecodeError, OSError,
        subprocess.SubprocessError, UnicodeError,
    ) as error:
        print(json.dumps({
            "error": str(error), "error_kind": "operation",
            "schema": SCHEMA, "status": "error", "ticket": args.ticket,
        }, sort_keys=True))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
