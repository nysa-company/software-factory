#!/usr/bin/env python3
"""Contract 1.8+ non-agent ticket reconciliation controller."""

from __future__ import annotations

import argparse
from contextlib import nullcontext
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import stat
import subprocess
import sys
import tempfile
from threading import Event, Lock, local
import time
from typing import Any
from urllib.parse import urlsplit


sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
from release_lineage import (  # noqa: E402
    MIGRATION_SCHEMA as PASSPORT_MIGRATION_SCHEMA,
    passport_head_lineage, release_source_base, successor_release_lineage,
    valid_v2_migration,
)
from qualification_artifacts import (  # noqa: E402
    ArtifactError as QualificationArtifactError,
    ensure_ticket as ensure_qualification_artifacts,
    retained_passport_digest_authenticated,
)
from qualification_manifest import (  # noqa: E402
    ManifestError as QualificationManifestError,
    committed_blob as committed_qualification_blob,
    validate as validate_qualification_manifest,
)
from qualification_release import ReceiptError, receipt_chain  # noqa: E402
from inflight_release import (  # noqa: E402
    AuthorizationError as InflightAuthorizationError,
    parse_authorization as parse_inflight_authorization,
    ticket_source_kit,
)
from effective_ticket import operator_action, operator_fields  # noqa: E402
from legacy_closeout import (  # noqa: E402
    ValidationError as ProtectedTerminalError,
    protected_terminal,
)
from failed_attempt_handoff import (  # noqa: E402
    HandoffError, validate_committed_output,
)
from route_evidence import (  # noqa: E402
    RouteEvidenceError, _handoff_policy, authenticated_fallback_head,
    exact_kit_sha_change, journal_extends, validate_route,
)
import operator_receipt  # noqa: E402
from role_output import RoleOutputError, sha256 as role_output_sha256  # noqa: E402
from ticket_state_transition import (  # noqa: E402
    TransitionError as TicketTransitionError,
    fresh_resume_text,
    planner_spec_linter_authorization,
    qualification_epoch_text,
)
from reorder_test_fixes import (  # noqa: E402
    Fail as HistoryReconstructionError,
    create_test_snapshot_reconstruction,
    verified_test_snapshot_reconstruction,
)
from external_transport import temporarily_unavailable  # noqa: E402


SCHEMA = "nysa.software-factory.controller/v1"
CLAIM_SCHEMA = "nysa.software-factory.controller-claim/v1"
EVENT_SCHEMA = "nysa.software-factory.controller-event/v1"
REFUSAL_READMISSION_SCHEMA = (
    "nysa.software-factory.refusal-readmission-attempt/v1"
)
QUALIFICATION_SCHEMA = "nysa.software-factory.qualification/v2"
CONTROLLER_CONTRACTS = frozenset({"1.8.0", "2.0.0"})
TICKET = re.compile(r"^T-[0-9]+$")
SHA = re.compile(r"^[0-9a-f]{40}$")
DIGEST = re.compile(r"^[0-9a-f]{64}$")
OPERATOR_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
SAFE_MODEL_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
SAFE_MODEL_TEXT = re.compile(r"^[^\x00-\x1f\x7f]{0,500}$")
MODEL_RESOLUTION_ERROR_SCHEMA = (
    "nysa.software-factory.model-resolution-error/v1"
)
FACTORY_ISSUE = re.compile(
    r"^https://github[.]com/[A-Za-z0-9_.-]+/software-factory/issues/[1-9][0-9]*$"
)
TERMINAL_ACCOUNTING = {
    "completed", "launch_void", "abandoned_conservative", "cancelled",
    "cancelled_conservative",
}
CONTRACT_RESUME_REFUSALS = frozenset({
    "resume_ancestry_invalid",
    "resume_commit_content_mismatch",
    "resume_commit_not_pushed",
    "resume_directives_ambiguous",
    "resume_parent_not_migrated",
    "resume_receipt_mismatch",
})
BUNDLE_REFRESH_PRIOR_STAGES = frozenset({
    "AWAIT-OPERATOR bundle attested; await operator approval",
    "AWAIT-OPERATOR operator approval observed; trusted approval attestation is required",
})
BUNDLE_REFRESH_APPROVED_STAGE = (
    "AWAIT-MERGE approval attested; protected auto-merge request pending"
)
PREFLIGHT_CORRECTION_FIELDS = (
    "Product-Decisions", "Builder ownership", "Fixture-Seams",
    "Authentication-Seams",
)
INFLIGHT_STATES = frozenset({
    "Ready", "Planning", "Building", "Review", "Awaiting Approval",
    "Approved", "Blocked-Escalated",
})
RECONCILE_INTERVAL_SECONDS = 15
RECONCILE_LOCK_WAIT_SECONDS = 30
RETRYABLE_RECONCILE_WAITS = frozenset({
    "closeout", "pr-gate", "protected-merge", "publication-lease",
})
PREVIEW_IDENTITY_WAIT_SECONDS = 900
RECOVERY_ATTEMPT_LIMIT = 3
COMPLETION_CORRECTION_SCHEMA = (
    "nysa.software-factory.completed-role-correction/v1"
)
QUALIFICATION_HISTORY_RECONSTRUCTION_SCHEMA = (
    "nysa.software-factory.qualification-history-reconstruction/v1"
)
MODEL_IDENTITY_CORRECTION_SCHEMA = (
    "nysa.software-factory.completed-role-correction/v2"
)
TERMINAL_ADOPTION_SCHEMA = (
    "nysa.software-factory.qualification-terminal-adoption/v2"
)
PROTECTED_TERMINAL_RECONCILIATION_SCHEMA = (
    "nysa.software-factory.qualification-protected-terminal-reconciliation/v1"
)
EMERGENCY_TERMINAL_RECONCILIATION_SCHEMA = (
    "nysa.software-factory.qualification-emergency-terminal-reconciliation/v1"
)
SEMANTIC_AUTHORIZATION_WAIT = re.compile(
    r"^AWAIT-OPERATOR semantic-round authorization "
    r"(?:required; add exact line|invalid; keep exactly one line): "
    r"(OPERATOR AUTHORIZATION: "
    r"(planner|spec-linter|test-author|builder|narrator) round ([1-9][0-9]*))$"
)
T198_FACTORY_SHA = "f165a5851dd0b0e84922b57735f26a586e967c66"
T198_RUN_ID = "1786262312-97243"
T198_RECEIPT = "baa1255fbb0dcc5be2cfa7315ba7610af6fffbb427e1d13e8ebe0fa24ac87aa7"


class ControllerError(ValueError):
    pass


class ExternalUnavailable(RuntimeError):
    pass


class FactoryDefect(ControllerError):
    def __init__(self, reason_code: str, message: str):
        super().__init__(message)
        self.reason_code = reason_code


class ModelIdentityEvidenceError(ControllerError):
    pass


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def canonical_document(value: Any) -> bytes:
    return (canonical(value) + "\n").encode()


def safe_error(error: BaseException) -> str:
    detail = str(error).replace("\x00", "")
    detail = re.sub(
        r"(?im)(authorization\s*:\s*)(?:bearer|basic|token)?\s*[^\r\n]*",
        lambda match: match.group(1) + "[redacted]",
        detail,
    )
    detail = re.sub(
        r"(?i)\b[A-Za-z][A-Za-z0-9+.-]*://\S+", "[redacted-url]", detail,
    )
    sensitive = (
        r"[A-Za-z0-9_.-]*(?:key|token|secret|password|url|dsn|conn|auth)"
        r"[A-Za-z0-9_.-]*"
    )
    quoted = re.compile(
        rf"(?is)(?P<prefix>['\"]?{sensitive}['\"]?\s*[:=]\s*)"
        rf"(?P<quote>['\"])(?:\\.|(?!(?P=quote)).)*(?P=quote)"
    )
    detail = quoted.sub(
        lambda match: match.group("prefix") + "[redacted]", detail,
    )
    key_line = re.compile(
        rf"(?i)^(?P<prefix>.*?['\"]?{sensitive}['\"]?\s*[:=]\s*)"
        r"(?P<value>.*)$"
    )
    redacted: list[str] = []
    continuation_indent: int | None = None
    for line in detail.splitlines(keepends=True):
        content = line.rstrip("\r\n")
        ending = line[len(content):]
        indent = len(content) - len(content.lstrip(" \t"))
        if continuation_indent is not None:
            if not content.strip() or indent > continuation_indent:
                redacted.append(content[:indent] + "[redacted]" + ending)
                continue
            continuation_indent = None
        match = key_line.match(content)
        if match:
            value = match.group("value").strip()
            redacted.append(match.group("prefix") + "[redacted]" + ending)
            if value in {"", "|", ">", "|-", ">-"}:
                continuation_indent = indent
            continue
        redacted.append(line)
    detail = "".join(redacted)
    return " ".join(detail.split())[:500] or "recovery refused"


def require_external_result(
    result: subprocess.CompletedProcess, label: str,
) -> subprocess.CompletedProcess:
    if result.returncode:
        if temporarily_unavailable(result.stderr or result.stdout):
            raise ExternalUnavailable("external service is temporarily unavailable")
        raise ControllerError(label)
    return result


def run_external(command: list[str], label: str) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(
            command, text=True, capture_output=True, check=False, timeout=120,
        )
    except subprocess.TimeoutExpired as error:
        raise ExternalUnavailable(
            "external service is temporarily unavailable"
        ) from error
    return require_external_result(result, label)


def push_exact_head(
    worktree: str, branch: str, head: str, expected_remote: str,
) -> None:
    command = [
        "git", "-C", worktree, "push",
        f"--force-with-lease=refs/heads/{branch}:{expected_remote}", "--",
        "origin", f"{head}:refs/heads/{branch}",
    ]
    try:
        pushed = subprocess.run(
            command, text=True, capture_output=True, check=False, timeout=120,
        )
    except subprocess.TimeoutExpired:
        pushed = None
    observed = run_external(
        [
            "git", "-C", worktree, "ls-remote", "--exit-code", "origin",
            f"refs/heads/{branch}",
        ],
        "role output remote verification failed",
    )
    if observed.stdout != f"{head}\trefs/heads/{branch}\n":
        if pushed is None or temporarily_unavailable(
            pushed.stderr or pushed.stdout
        ):
            raise ExternalUnavailable(
                "external service is temporarily unavailable"
            )
        raise ControllerError("role output push was not accepted")
    tracking = f"refs/remotes/origin/{branch}"
    current = subprocess.run(
        ["git", "-C", worktree, "rev-parse", "--verify", tracking],
        text=True, capture_output=True, check=False, timeout=120,
    ).stdout.strip()
    if current == expected_remote:
        subprocess.run(
            ["git", "-C", worktree, "update-ref", tracking, head, current],
            check=True, timeout=120,
        )
    elif current != head:
        raise ControllerError("role output remote tracking changed")


def semantic_authorization_wait(stage: str) -> tuple[str, str, int] | None:
    match = SEMANTIC_AUTHORIZATION_WAIT.fullmatch(stage)
    return (match[1], match[2], int(match[3])) if match else None


def semantic_authorization_context(
    stage: str, loop: Any,
) -> tuple[str, str, int, str] | None:
    wait = semantic_authorization_wait(stage)
    if wait is None or not isinstance(loop, dict):
        return None
    line, role, semantic_round = wait
    kind = loop.get("kind")
    attempt = semantic_round - 1
    if (
        set(loop) != {"attempt", "capped", "kind", "limit"}
        or loop.get("attempt") != attempt
        or kind not in {
            "planner-spec-linter", "contract-repair", "narrator-bundle",
        }
        or loop.get("limit") != (2 if kind == "narrator-bundle" else 3)
        or loop.get("capped") is not (
            attempt >= (2 if kind == "narrator-bundle" else 3)
        )
        or kind == "planner-spec-linter" and role != "spec-linter"
        or kind == "narrator-bundle" and role != "narrator"
    ):
        return None
    return line, role, semantic_round, kind


def semantic_block_reason(role: str, semantic_round: int) -> str:
    return f"semantic-round-authorization:{role}:{semantic_round}"


def semantic_authorization_event(
    stage: str, head: str, receipt: str, reason_code: str | None = None,
) -> tuple[str, tuple[str, ...], dict[str, Any]] | None:
    wait = semantic_authorization_wait(stage)
    if wait is None:
        return None
    invalid = reason_code is not None or " authorization invalid;" in stage
    details: dict[str, Any] = {
        "head_sha": head, "role": wait[1],
        "semantic_round": wait[2],
        "transition_receipt_sha256": receipt,
    }
    if invalid:
        details["reason_code"] = reason_code or "authorization_count_invalid"
    return (
        (
            "semantic_round_authorization_invalid"
            if invalid else "semantic_round_authorization_wait"
        ),
        (
            ("head_sha", "reason_code", "role", "semantic_round")
            if invalid else ("head_sha", "role", "semantic_round")
        ),
        details,
    )


def valid_transition_evidence(value: dict[str, Any], ticket: str) -> bool:
    stage = value.get("stage")
    if not isinstance(stage, str) or not stage:
        return False
    runnable = re.fullmatch(
        r"(?:RUN|FIX) "
        r"(planner|spec-linter|test-author|builder|reviewer|narrator)",
        stage,
    )
    non_role = (
        stage.startswith("AWAIT-OPERATOR ")
        or stage.startswith("AWAIT_DEPENDENCY ")
        or stage.startswith("AWAIT_BUDGET ")
        or stage.startswith("COMPLETE ")
        or stage.startswith("ESCALATE ")
        or stage.startswith("REFUSE ")
        or stage.startswith((
            "AWAIT-MERGE approval attested; protected auto-merge request pending",
            "AWAIT-MERGE protected auto-merge requested",
            "AWAIT-MERGE closeout auto-merge pending",
        ))
    )
    loop = value.get("loop")
    valid_loop = loop is None or (
        isinstance(loop, dict)
        and set(loop) == {"attempt", "capped", "kind", "limit"}
        and isinstance(loop.get("attempt"), int)
        and not isinstance(loop.get("attempt"), bool)
        and loop["attempt"] >= 1
        and isinstance(loop.get("capped"), bool)
        and loop.get("kind") in {
            "builder-reviewer", "contract-repair", "planner-spec-linter",
        }
        and loop.get("limit") == 3
    )
    expected_role = runnable[1] if runnable else None
    return not any((
        not DIGEST.fullmatch(value.get("receipt", "")),
        value.get("schema") != "nysa.software-factory.state-machine/v1",
        value.get("status") != "ok",
        value.get("ticket") != ticket,
        value.get("action") != stage.partition(" ")[0],
        value.get("detail") != (stage.partition(" ")[2] or None),
        not valid_loop,
        not (runnable or non_role),
        value.get("role") != expected_role,
    ))


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
        raise ControllerError("controller directory is unsafe")
    return path


def read(path: Path) -> dict[str, Any]:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_size > 1_000_000
        ):
            raise ControllerError("controller claim is unsafe")
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            descriptor = -1
            value = json.load(stream)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not isinstance(value, dict):
        raise ControllerError("controller claim is malformed")
    return value


def write(path: Path, value: dict[str, Any]) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = -1
            stream.write(canonical(value) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        Path(temporary).unlink(missing_ok=True)


def fields(path: Path) -> dict[str, str]:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) & 0o022
            or info.st_size > 1_000_000
        ):
            raise ControllerError("run manifest is unsafe")
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            descriptor = -1
            lines = stream.read().splitlines()
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    values: dict[str, str] = {}
    for line in lines:
        name, separator, value = line.partition("=")
        if not separator or name in values:
            raise ControllerError("run manifest is malformed")
        values[name] = value
    return values


def progress_summary(path: Path) -> dict[str, Any]:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_size > 10_000_000
        ):
            raise ControllerError("attempt progress journal is unsafe")
        chunks = []
        remaining = 10_000_001
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
    finally:
        os.close(descriptor)
    if len(raw) > 10_000_000:
        raise ControllerError("attempt progress journal is oversized")
    sequence = 0
    observed = -1
    latest_type = ""
    latest_subtype = ""
    for line in raw.splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError as error:
            raise ControllerError("attempt progress journal is malformed") from error
        if (
            not isinstance(record, dict)
            or set(record) != {
                "event_sha256", "observed_monotonic_ns", "sequence",
                "subtype", "type",
            }
            or record.get("sequence") != sequence + 1
            or not isinstance(record.get("observed_monotonic_ns"), int)
            or isinstance(record.get("observed_monotonic_ns"), bool)
            or record["observed_monotonic_ns"] <= observed
            or not DIGEST.fullmatch(record.get("event_sha256", ""))
            or record.get("type") not in {
                "assistant", "result", "system", "tool_call",
            }
            or not isinstance(record.get("subtype"), str)
            or len(record["subtype"]) > 64
            or not re.fullmatch(r"[A-Za-z0-9._:-]*", record["subtype"])
        ):
            raise ControllerError("attempt progress journal is malformed")
        sequence = record["sequence"]
        observed = record["observed_monotonic_ns"]
        latest_type = record["type"]
        latest_subtype = record["subtype"]
    return {
        "journal_bytes": len(raw),
        "journal_sha256": hashlib.sha256(raw).hexdigest(),
        "latest_subtype": latest_subtype,
        "latest_type": latest_type,
        "observed_monotonic_ns": observed if sequence else None,
        "progress_events": sequence,
    }


class Controller:
    def __init__(self, args: argparse.Namespace):
        self.launcher = args.launcher.resolve(strict=True)
        self.project = args.project
        self.product = args.product_root.resolve(strict=True)
        self.release_path = args.release_path.resolve(strict=True)
        self.state = safe_directory(args.state_dir)
        self.wait_seconds = getattr(args, "wait_seconds", 0)
        worktree_root = getattr(args, "worktree_root", None)
        if (
            worktree_root is None
            and os.environ.get("FACTORY_RELEASE_PATH") == str(self.release_path)
            and SHA.fullmatch(self.release_path.name)
        ):
            release_root = self.release_path.parent
            lane_root = release_root.parent
            if release_root == Path.home() / ".factory/kits/releases":
                worktree_root = Path.home() / ".factory/worktrees" / self.project
            elif (
                release_root.name == "releases"
                and lane_root.parent == Path("/private/tmp")
                and re.fullmatch(
                    r"nysa-sf-qualification[.][A-Za-z0-9._-]+",
                    lane_root.name,
                )
            ):
                worktree_root = lane_root / "worktrees" / self.project
        self.worktree_root = Path(worktree_root) if worktree_root else None
        self.claims = self.state / "claims"
        safe_directory(self.claims, create=True)
        self.logs = self.state / "logs"
        safe_directory(self.logs, create=True)
        self.events = self.state / "events"
        safe_directory(self.events, create=True)
        event_epochs = [
            int(match[1])
            for path in self.events.glob("*.json")
            if (match := re.fullmatch(
                r"([1-9][0-9]{0,20})-[0-9a-f]{16}[.]json", path.name
            ))
        ]
        self.event_epoch_ns = max(event_epochs, default=0)
        self.event_lock = Lock()
        self.capacity = self.read_capacity()
        self.qualification = self.read_qualification()
        repository_test = os.environ.get("FACTORY_KIT_TRUST_SCOPE") == "repository-test"
        if repository_test and (
            os.environ.get("FACTORY_TEST_MODE"),
            os.environ.get("FACTORY_TRUSTED_TEST_HARNESS"),
            os.environ.get("FACTORY_ADAPTER_OVERRIDE"),
        ) != ("1", "1", "mock"):
            raise ControllerError("repository-test controller authority is invalid")
        self.repository_test = repository_test
        self.legacy_dispatch_fixture = getattr(args, "legacy_dispatch_fixture", False)
        self.qualification_manifest_sha256 = (
            hashlib.sha256(canonical(self.qualification).encode()).hexdigest()
            if self.qualification else ""
        )
        self.qualification_fallback_readiness_sha256 = os.environ.get(
            "FACTORY_QUALIFICATION_FALLBACK_READINESS_SHA256", ""
        )
        self.model_admission_outcome: dict[str, Any] | None = None
        self.admission_refusals: dict[str, dict[str, str]] = {}
        self.invalid_transition_tickets: set[str] = set()
        self.prior_transition_tickets: set[str] = set()
        self.fallback_lock = Lock()
        self.publication_lock = Lock()
        self.qualification_cohort_error = Event()
        self.qualification_launch_lock = Lock()
        # ponytail: cells share one Git common directory; use per-cell refs only if refresh throughput matters.
        self.git_lock = Lock()
        # ponytail: closeouts are rare; serialize them until throughput requires a queue.
        self.closeout_lock = Lock()
        self.recovery_context = local()

    def epoch_ticket(self, ticket: str, text: str) -> str:
        try:
            return qualification_epoch_text(self.product, ticket, text)
        except TicketTransitionError as error:
            raise ControllerError(str(error)) from error

    def read_qualification(self) -> dict[str, Any] | None:
        path = self.product / "factory/QUALIFICATION.json"
        if not path.exists():
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
        if value.get("schema") != QUALIFICATION_SCHEMA:
            return None
        try:
            return validate_qualification_manifest(
                value, self.release_path.name, self.capacity,
            )
        except QualificationManifestError as error:
            raise ControllerError(str(error)) from error

    def event(self, name: str, ticket: str = "", **details: Any) -> None:
        with self.event_lock:
            self.event_epoch_ns = max(time.time_ns(), self.event_epoch_ns + 1)
            value = {
                "event": name,
                "factory_sha": self.release_path.name,
                "observed_at_epoch_ns": self.event_epoch_ns,
                "schema": EVENT_SCHEMA,
                "ticket": ticket or None,
                **details,
            }
            if self.qualification:
                value.update({
                    "qualification_generation": self.qualification["generation"],
                    "qualification_manifest_sha256": (
                        self.qualification_manifest_sha256
                    ),
                })
            raw = canonical(value).encode()
            value["event_sha256"] = hashlib.sha256(raw).hexdigest()
            path = self.events / (
                f"{value['observed_at_epoch_ns']}-{secrets.token_hex(8)}.json"
            )
            temporary = self.state / (
                f".controller-event-{secrets.token_hex(8)}.tmp"
            )
            descriptor = -1
            try:
                descriptor = os.open(
                    temporary,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL
                    | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                )
                with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                    descriptor = -1
                    stream.write(canonical(value) + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
                directory = os.open(
                    self.events, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                )
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
                temporary.unlink(missing_ok=True)

    def passport_sha256(self, ticket: str) -> str | None:
        path = self.state / "passports" / f"{ticket}.json"
        if not path.exists():
            return None
        digest = read(path).get("passport_sha256", "")
        if not DIGEST.fullmatch(digest):
            raise ControllerError("ticket passport digest is invalid")
        return digest

    def event_once(
        self, name: str, ticket: str, *,
        dedupe_fields: tuple[str, ...] | None = None,
        **details: Any,
    ) -> None:
        match_details = (
            {key: details[key] for key in dedupe_fields}
            if dedupe_fields is not None else details
        )
        for path in sorted(self.events.glob("*.json")):
            value = read(path)
            digest = value.pop("event_sha256", "")
            if digest != hashlib.sha256(canonical(value).encode()).hexdigest():
                raise ControllerError("controller event evidence is invalid")
            if (
                value.get("event") == name
                and value.get("factory_sha") == self.release_path.name
                and value.get("ticket") == ticket
                and (
                    not self.qualification
                    or (
                        value.get("qualification_generation")
                        == self.qualification["generation"]
                        and value.get("qualification_manifest_sha256")
                        == self.qualification_manifest_sha256
                    )
                )
                and all(
                    value.get(key) == item
                    for key, item in match_details.items()
                )
            ):
                return
        self.event(name, ticket, **details)

    def authenticated_operator_passport(
        self, ticket: str,
    ) -> dict[str, Any] | None:
        key_path = self.state / "passport.key"
        passport_path = self.state / "passports" / f"{ticket}.json"
        if not key_path.exists() and not key_path.is_symlink():
            if passport_path.exists() or passport_path.is_symlink():
                raise ControllerError("operator passport authentication is unavailable")
            return None
        descriptor = os.open(
            key_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_size != 32
            ):
                raise ControllerError("operator passport key is unsafe")
            secret = os.read(descriptor, 33)
        finally:
            os.close(descriptor)
        if not passport_path.exists() and not passport_path.is_symlink():
            return None
        value = read(passport_path)
        passport_digest = value.pop("passport_sha256", "")
        if passport_digest != hashlib.sha256(canonical_document(value)).hexdigest():
            raise ControllerError("operator passport digest is invalid")
        authentication = value.pop("authentication_sha256", "")
        if not hmac.compare_digest(
            authentication,
            hmac.new(
                secret, canonical_document(value), hashlib.sha256,
            ).hexdigest(),
        ):
            raise ControllerError("operator passport authentication is invalid")
        value.update(
            authentication_sha256=authentication,
            passport_sha256=passport_digest,
        )
        if (
            value.get("schema") != "nysa.software-factory.ticket-passport/v1"
            or value.get("ticket") != ticket
            or value.get("project") != self.project
            or value.get("contract_version") not in CONTROLLER_CONTRACTS
        ):
            raise ControllerError("operator passport identity is invalid")
        return value

    def transition_receipt(
        self, claim: dict[str, Any], *, allow_prior: bool = False,
        record: bool = True,
    ) -> dict[str, Any] | None:
        path = self.state / f"{claim['ticket']}.json"
        if not path.exists() and not path.is_symlink():
            return None
        try:
            value = read(path)
        except (
            ControllerError, json.JSONDecodeError, OSError, UnicodeError,
        ):
            if record:
                self.invalid_transition_tickets.add(claim["ticket"])
                self.event_once(
                    "transition_receipt_invalid", claim["ticket"],
                    reason_code="receipt_unreadable",
                )
            return None
        digest = value.get("receipt_sha256", "")
        immutable = {
            key: item for key, item in value.items()
            if key not in {"consumed", "consumed_at_epoch", "receipt_sha256"}
        }
        if (
            not isinstance(digest, str)
            or DIGEST.fullmatch(digest) is None
            or digest != hashlib.sha256(canonical_document(immutable)).hexdigest()
        ):
            if record:
                self.invalid_transition_tickets.add(claim["ticket"])
                self.event_once(
                    "transition_receipt_invalid", claim["ticket"],
                    reason_code="receipt_digest_invalid",
                )
            return None
        factory_sha = value.get("factory_sha", "")
        if any((
            value.get("schema")
            != "nysa.software-factory.transition-receipt/v1",
            value.get("ticket") != claim["ticket"],
            value.get("branch") != claim["branch"],
            not isinstance(factory_sha, str) or SHA.fullmatch(factory_sha) is None,
            value.get("project") != self.project,
            value.get("contract_version") not in CONTROLLER_CONTRACTS,
            not isinstance(value.get("consumed"), bool),
        )):
            if record:
                self.invalid_transition_tickets.add(claim["ticket"])
                self.event_once(
                    "transition_receipt_invalid", claim["ticket"],
                    reason_code="receipt_identity_invalid",
                )
            return None
        if factory_sha != self.release_path.name:
            if record:
                self.prior_transition_tickets.add(claim["ticket"])
                self.event_once(
                    "prior_kit_transition_receipt_observed", claim["ticket"],
                    active_factory_sha=self.release_path.name,
                    receipt_factory_sha=factory_sha,
                    transition_receipt_sha256=digest,
                )
            return value if allow_prior else None
        return value

    def operator_transition(
        self, claim: dict[str, Any],
    ) -> dict[str, Any] | None:
        return self.transition_receipt(claim)

    def quarantine_invalid_transition_claims(
        self, claims: list[dict[str, Any]],
    ) -> None:
        for claim in claims:
            if (
                claim["ticket"] in self.invalid_transition_tickets
                and DIGEST.fullmatch(claim.get("lease", ""))
                and claim.get("lease_released") is not True
                and not self.role_active(claim)
            ):
                try:
                    self.release_ticket_lease(claim)
                except (
                    ControllerError, json.JSONDecodeError, OSError,
                    subprocess.SubprocessError, UnicodeError,
                ):
                    self.event_once(
                        "transition_receipt_quarantine_waiting",
                        claim["ticket"], reason_code="lease_release_refused",
                    )

    def dependency_publication_replay_transition(
        self, claim: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Recognize only the exact post-push dependency refresh boundary."""
        refresh_path = (
            Path(claim["worktree"]) / "factory" / "attestations"
            / claim["ticket"] / "refresh.json"
        )
        try:
            if not stat.S_ISREG(refresh_path.lstat().st_mode):
                return None
            candidate = self.operator_transition(claim)
        except (ControllerError, OSError, json.JSONDecodeError, UnicodeError):
            return None
        if candidate is None:
            return None
        stage = candidate.get("stage", "")
        if not (
            candidate.get("schema")
            == "nysa.software-factory.transition-receipt/v1"
            and candidate.get("consumed") is True
            and re.fullmatch(
                r"REFUSE dependency refresh required; "
                r"dependencies=T-[0-9]+(?:,T-[0-9]+)*; "
                r"protected-main=[0-9a-f]{40}",
                stage,
            ) is not None
            and SHA.fullmatch(candidate.get("head_sha", ""))
        ):
            return None
        current = subprocess.run(
            ["git", "-C", claim["worktree"], "rev-parse", "HEAD"],
            text=True, capture_output=True, check=False, timeout=120,
        )
        refresh_commit = subprocess.run(
            [
                "git", "-C", claim["worktree"], "log", "-1",
                "--format=%H", "HEAD", "--",
                f"factory/attestations/{claim['ticket']}/refresh.json",
            ],
            text=True, capture_output=True, check=False, timeout=120,
        )
        current_head = current.stdout.strip()
        if (
            current.returncode
            or refresh_commit.returncode
            or not SHA.fullmatch(current_head)
            or refresh_commit.stdout.strip() != current_head
            or candidate["head_sha"] == current_head
        ):
            return None
        passport = self.authenticated_operator_passport(claim["ticket"])
        if (
            passport is None
            or passport.get("branch") != claim["branch"]
            or passport.get("factory_sha") != self.release_path.name
        ):
            raise ControllerError(
                "dependency refresh replay passport is invalid"
            )
        passport_head = passport.get("head_sha", "") if passport else ""
        if passport_head == current_head:
            return None
        if passport_head != candidate["head_sha"]:
            raise ControllerError(
                "dependency refresh replay passport is invalid"
            )
        transition = self.operator_transition(claim)
        if (
            transition is None
            or transition.get("stage") != stage
            or transition.get("consumed") is not True
            or transition.get("head_sha") != candidate["head_sha"]
        ):
            raise ControllerError(
                "dependency refresh replay transition changed"
            )
        return {
            "action": stage.partition(" ")[0],
            "detail": stage.partition(" ")[2] or None,
            "loop": transition.get("loop"),
            "receipt": transition["receipt_sha256"],
            "role": transition.get("role"),
            "schema": "nysa.software-factory.state-machine/v1",
            "stage": stage,
            "status": "ok",
            "ticket": claim["ticket"],
        }

    def qualification_primed_planner_transition(
        self, claim: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Replay only the fresh qualification pre-restart Planner receipt."""
        passports = self.state / "passports"
        passport_path = passports / f"{claim['ticket']}.json"
        if (
            not self.qualification
            or "mode" in self.qualification
            or self.repository_test
            or claim["ticket"] not in self.qualification["tickets"]
            or not self.qualification_marker("qualification-restart-boundary")
            or set(claim) != {
                "branch", "lease", "priority", "publication_lease", "receipt",
                "role", "schema", "status", "ticket", "worktree",
            }
            or claim.get("schema") != CLAIM_SCHEMA
            or claim.get("branch") != f"ticket/{claim['ticket']}"
            or claim.get("status") != "claimed"
            or claim.get("receipt") != ""
            or claim.get("role") != ""
            or claim.get("publication_lease") != ""
            or not DIGEST.fullmatch(claim.get("lease", ""))
            or self.role_active(claim)
        ):
            return None
        try:
            safe_directory(passports)
        except (ControllerError, OSError):
            return None
        if passport_path.exists() or passport_path.is_symlink():
            return None
        receipt = self.transition_receipt(claim, record=False)
        branch = self.cell_git(
            claim, "symbolic-ref", "--quiet", "--short", "HEAD",
        )
        status = self.cell_git(claim, "status", "--porcelain=v1", "-z")
        if (
            receipt is None
            or receipt.get("stage") != "RUN planner"
            or receipt.get("role") != "planner"
            or receipt.get("loop") is not None
            or receipt.get("consumed") is not False
            or receipt.get("passport_sha256") is not None
            or branch.returncode
            or branch.stdout.strip() != claim["branch"]
            or status.returncode
            or status.stdout
            or not self.ticket_release_current(claim)
            or not self.exact_passportless_planner_receipt(claim, receipt)
        ):
            return None
        return {
            "action": "RUN",
            "detail": "planner",
            "loop": None,
            "receipt": receipt["receipt_sha256"],
            "role": "planner",
            "schema": "nysa.software-factory.state-machine/v1",
            "stage": "RUN planner",
            "status": "ok",
            "ticket": claim["ticket"],
        }

    def recover_operator_action_events(
        self, claims: list[dict[str, Any]],
    ) -> None:
        """Backfill crash-lost operator events from exact durable boundaries."""
        candidates = [
            claim for claim in claims
            if claim.get("status") in {"blocked", "budget", "claimed", "waiting"}
        ]
        if not candidates:
            return
        inventory: list[dict[str, Any]] = []
        for path in self.events.glob("*.json"):
            value = read(path)
            digest = value.get("event_sha256", "")
            unsigned = dict(value)
            unsigned.pop("event_sha256", None)
            if (
                value.get("schema") != EVENT_SCHEMA
                or digest != hashlib.sha256(canonical(unsigned).encode()).hexdigest()
            ):
                raise ControllerError("controller event evidence is invalid")
            inventory.append(value)

        def recorded(
            name: str, ticket: str, details: dict[str, Any],
        ) -> bool:
            return any(
                value.get("event") == name
                and value.get("factory_sha") == self.release_path.name
                and value.get("ticket") == ticket
                and (
                    not self.qualification
                    or (
                        value.get("qualification_generation")
                        == self.qualification["generation"]
                        and value.get("qualification_manifest_sha256")
                        == self.qualification_manifest_sha256
                    )
                )
                and all(
                    value.get(key) == item
                    for key, item in details.items()
                )
                for value in inventory
            )

        def emit(
            name: str, ticket: str, *,
            dedupe_fields: tuple[str, ...] | None = None,
            **details: Any,
        ) -> None:
            match_details = (
                {key: details[key] for key in dedupe_fields}
                if dedupe_fields is not None else details
            )
            if recorded(name, ticket, match_details):
                return
            self.event(name, ticket, **details)
            inventory.append({
                "event": name, "factory_sha": self.release_path.name,
                "ticket": ticket, **details,
                **(
                    {
                        "qualification_generation": self.qualification["generation"],
                        "qualification_manifest_sha256": (
                            self.qualification_manifest_sha256
                        ),
                    }
                    if self.qualification else {}
                ),
            })

        for claim in candidates:
            ticket = claim["ticket"]
            if claim.get("status") == "budget":
                transition = self.operator_transition(claim)
                passport = self.authenticated_operator_passport(ticket)
                passport_digest = (
                    passport.get("passport_sha256")
                    if passport is not None else None
                )
                if (
                    transition is not None
                    and transition.get("stage", "").startswith("AWAIT_BUDGET ")
                    and transition.get("role") is None
                    and claim.get("receipt") == ""
                    and claim.get("role") == ""
                    and claim.get("budget_sha256") == self.envelope_digest()
                    and (
                        passport is None
                        or (
                            passport.get("branch") == claim["branch"]
                            and passport.get("factory_sha")
                            == self.release_path.name
                        )
                    )
                ):
                    emit("budget_wait", ticket, passport_sha256=passport_digest)
                continue
            if (
                claim.get("status") in {"claimed", "waiting"}
            ):
                transition = self.operator_transition(claim)
                semantic_event = semantic_authorization_event(
                    transition.get("stage", "") if transition else "",
                    transition.get("head_sha", "") if transition else "",
                    transition.get("receipt_sha256", "") if transition else "",
                )
                if semantic_event is not None:
                    event, dedupe, details = semantic_event
                    emit(event, ticket, dedupe_fields=dedupe, **details)
                    continue
                if not (
                    transition is not None
                    and transition.get("stage", "").startswith(
                        "AWAIT-OPERATOR bundle posted"
                    )
                ):
                    continue
                passport = self.authenticated_operator_passport(ticket)
                passport_digest = (
                    passport.get("passport_sha256")
                    if passport is not None else None
                )
            if (
                claim.get("status") in {"claimed", "waiting"}
                and transition is not None
                and transition.get("stage", "").startswith(
                    "AWAIT-OPERATOR bundle posted"
                )
                and transition.get("role") is None
                and claim.get("receipt") == ""
                and claim.get("role") == ""
                and passport is not None
                and passport.get("branch") == claim["branch"]
                and passport.get("factory_sha") == self.release_path.name
                and passport.get("current_state") == "Awaiting Approval"
                and passport.get("publication_state") == "validating"
            ):
                emit(
                    "awaiting_approval", ticket,
                    passport_sha256=passport_digest,
                    question="Approve this ticket to merge.",
                )
                continue
            if claim.get("status") != "blocked":
                continue
            blocked_reason = claim.get("blocked_reason", "")
            terminal = (
                self.terminal_for_receipt(ticket, claim.get("receipt", ""))
                if blocked_reason == "role-failure" else None
            )
            migration_event = {
                "failed_run_id": terminal.get("run_id") if terminal else "",
            }
            if (
                terminal is not None
                and terminal.get("ticket") == ticket
                and terminal.get("role") == claim.get("role")
                and terminal.get("transition_receipt_sha256")
                == claim.get("receipt")
                and terminal.get("exit_status") == "12"
                and terminal.get("role_exit") == "role_exit_contract_blocked"
                and re.fullmatch(
                    r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}",
                    terminal.get("run_id", ""),
                )
                and not recorded(
                    "contract_block_passport_migrated", ticket,
                    migration_event,
                )
            ):
                try:
                    passport = self.authenticated_operator_passport(ticket)
                    remote_valid = self.remote_passport_valid(claim)
                    checked = self.json_call(
                        "state-machine", "repair-check", "--ticket", ticket,
                        "--receipt", claim["receipt"], "--workdir",
                        claim["worktree"], "--json", allow=(0, 1),
                    )
                except (
                    ControllerError, json.JSONDecodeError, OSError,
                    subprocess.SubprocessError, UnicodeError,
                ):
                    passport = None
                    remote_valid = False
                    checked = {}
                if (
                    passport is not None
                    and remote_valid
                    and passport.get("branch") == claim.get("branch")
                    and passport.get("factory_sha") == self.release_path.name
                    and checked.get("action") == "repair-check"
                    and checked.get("head") == passport.get("head_sha")
                    and checked.get("role") == claim.get("role")
                    and checked.get("schema")
                    == "nysa.software-factory.state-machine/v1"
                    and checked.get("status") in {"ready", "waiting"}
                    and checked.get("ticket") == ticket
                ):
                    emit(
                        "contract_block_passport_migrated", ticket,
                        **migration_event,
                    )
            fallback_refusal = re.fullmatch(
                r"qualification-fallback-refused:"
                r"(readiness|manifest|attempt_count|handoff|route_policy|provenance|unknown):"
                r"([0-9a-f]{40})",
                blocked_reason,
            )
            if fallback_refusal is not None:
                if fallback_refusal.group(2) == self.release_path.name:
                    emit(
                        "typed_recovery_refused", ticket,
                        reason=fallback_refusal.group(1),
                        recovery_kind="qualification_fallback",
                    )
                continue
            if blocked_reason == "role-failure":
                transition = self.operator_transition(claim)
                passport = self.authenticated_operator_passport(ticket)
                passport_digest = (
                    passport.get("passport_sha256")
                    if passport is not None else None
                )
                terminal = self.terminal_for_receipt(ticket, claim.get("receipt", ""))
                if (
                    transition is not None
                    and transition.get("receipt_sha256") == claim.get("receipt")
                    and transition.get("role") == claim.get("role")
                    and transition.get("stage")
                    in {f"RUN {claim.get('role')}", f"FIX {claim.get('role')}"}
                    and SHA.fullmatch(transition.get("head_sha", ""))
                    and terminal is not None
                    and terminal.get("ticket") == ticket
                    and terminal.get("role") == claim.get("role")
                    and terminal.get("kit_sha") == self.release_path.name
                    and terminal.get("transition_receipt_sha256")
                    == claim.get("receipt")
                    and terminal.get("role_branch_before") == claim["branch"]
                    and terminal.get("role_head_before")
                    == transition.get("head_sha")
                    and terminal.get("phase") == "completed"
                    and terminal.get("go_issued") == "1"
                    and terminal.get("task_submitted") == "1"
                    and terminal.get("exit_status") != "0"
                    and terminal.get("role_exit") != "ok"
                    and re.fullmatch(
                        r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}",
                        terminal.get("run_id", ""),
                    )
                    and passport is not None
                    and passport.get("branch") == claim["branch"]
                    and passport.get("factory_sha") == self.release_path.name
                    and passport.get("transition_receipt_sha256")
                    == claim.get("receipt")
                ):
                    emit(
                        "role_blocked", ticket,
                        passport_sha256=passport_digest,
                        role=claim["role"], role_exit=terminal.get("role_exit"),
                        run_id=terminal.get("run_id"),
                        terminal_reason_code=terminal.get(
                            "terminal_reason_code", ""
                        ),
                    )
                continue
            if blocked_reason == "pre-go-failure":
                transition = self.operator_transition(claim)
                terminal = self.terminal_for_receipt(ticket, claim.get("receipt", ""))
                if (
                    transition is not None
                    and transition.get("receipt_sha256") == claim.get("receipt")
                    and transition.get("role") == claim.get("role")
                    and SHA.fullmatch(transition.get("head_sha", ""))
                    and terminal is not None
                    and self.typed_launch_void(terminal)
                    and terminal.get("role") == claim.get("role")
                    and terminal.get("kit_sha") == self.release_path.name
                    and terminal.get("role_branch_before") == claim["branch"]
                    and terminal.get("role_head_before")
                    == transition.get("head_sha")
                    and re.fullmatch(
                        r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}",
                        terminal.get("run_id", ""),
                    )
                ):
                    reason = terminal.get("terminal_reason_code", "")
                    emit(
                        "pre_go_failure_blocked", ticket,
                        failed_run_id=terminal.get("run_id"),
                        reason=(
                            reason if re.fullmatch(r"[a-z0-9_]{1,64}", reason)
                            else "pre_go_failure"
                        ),
                    )
                continue
            known_block = blocked_reason in {
                "bundle-pr-gate", "narrator-pr-gate", "preflight",
                "preflight-evidence", "preview-identity-timeout",
                "preview-preflight", "route-migration-required",
                "state-machine-escalation", "state-machine-refusal",
            } or blocked_reason == (
                "model-identity-recovery-refused:" + self.release_path.name
            )
            if not known_block:
                continue
            if blocked_reason == "state-machine-escalation":
                transition = self.operator_transition(claim)
                passport = self.authenticated_operator_passport(ticket)
                passport_digest = (
                    passport.get("passport_sha256")
                    if passport is not None else None
                )
                if (
                    transition is not None
                    and transition.get("role") is None
                    and transition.get("stage", "").startswith("ESCALATE ")
                    and (
                        passport is None
                        or (
                            passport.get("branch") == claim["branch"]
                            and passport.get("factory_sha")
                            == self.release_path.name
                        )
                    )
                ):
                    emit("ticket_blocked", ticket, reason=blocked_reason)
                    emit(
                        "state_machine_escalated", ticket,
                        detail=transition["stage"].partition(" ")[2],
                        passport_sha256=passport_digest,
                    )
                continue
            emit("ticket_blocked", ticket, reason=blocked_reason)

    def record_contract_resume_refusal(
        self, claim: dict[str, Any], reason_code: str, evidence: dict[str, Any]
    ) -> None:
        if (
            reason_code not in CONTRACT_RESUME_REFUSALS
            or not DIGEST.fullmatch(claim.get("receipt", ""))
        ):
            raise ControllerError("contract resume refusal reason is invalid")
        allowed = {
            "actual_bytes", "changed_path_count", "expected_bytes",
            "first_differing_line", "local_head", "offending_parent",
            "remote_head",
        }
        if set(evidence) - allowed:
            raise ControllerError("contract resume refusal evidence is invalid")
        for key in ("local_head", "offending_parent", "remote_head"):
            if key in evidence and evidence[key] not in {None, ""} and not SHA.fullmatch(
                evidence[key]
            ):
                raise ControllerError("contract resume refusal evidence is invalid")
        for key in (
            "actual_bytes", "changed_path_count", "expected_bytes",
            "first_differing_line",
        ):
            if key in evidence and evidence[key] is not None and (
                isinstance(evidence[key], bool)
                or not isinstance(evidence[key], int)
                or evidence[key] < 0
            ):
                raise ControllerError("contract resume refusal evidence is invalid")
        self.event_once(
            "contract_resume_refused", claim["ticket"],
            blocked_receipt_sha256=claim["receipt"], reason_code=reason_code,
            **evidence,
        )

    def adopt_qualification_terminal(self, ticket: str) -> dict[str, Any]:
        if (
            not self.qualification
            or self.qualification.get("mode") != "successor"
        ):
            raise ControllerError("terminal adoption requires successor qualification")
        source = self.qualification["source_factory_sha"]
        passport_path = self.state / "passports" / f"{ticket}.json"
        done_path = (
            self.product / "factory" / "attestations" / ticket / "done.json"
        )
        try:
            passport = read(passport_path)
            done_info = done_path.lstat()
            if (
                not stat.S_ISREG(done_info.st_mode)
                or stat.S_IMODE(done_info.st_mode) & 0o022
                or done_info.st_size > 1_000_000
            ):
                raise ControllerError("qualification terminal attestation is unsafe")
            done = json.loads(done_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError) as error:
            raise ControllerError("qualification terminal adoption is incomplete") from error
        marker_name = (
            f"qualification-terminal-adoption-{self.release_path.name}-{ticket}"
        )
        marker_path = self.state / f"{marker_name}.json"
        if passport.get("factory_sha") != self.release_path.name:
            if not successor_release_lineage(
                passport.get("factory_release_history"),
                passport.get("migration_history"),
                source,
                passport.get("factory_sha", ""),
            ):
                raise ControllerError(
                    "qualification terminal passport has unknown release"
                )
            branch = f"refs/heads/ticket/{ticket}"
            worktrees = self.worktrees_by_branch().get(branch, [])
            if len(worktrees) != 1 or marker_path.exists():
                raise ControllerError("qualification terminal adoption cell is ambiguous")
            claim = {
                "branch": f"ticket/{ticket}",
                "ticket": ticket,
                "worktree": worktrees[0],
            }
            validation = self.json_call(
                "passport", "validate", "--ticket", ticket,
                "--workdir", worktrees[0], "--json",
            )
            if (
                validation.get("status") != "ok"
                or validation.get("passport") != passport.get("passport_sha256")
            ):
                raise ControllerError("qualification terminal passport is invalid")
            source_history = {
                item.get("factory_sha")
                for item in passport.get("factory_release_history", [])
                if isinstance(item, dict)
            }
            source_evidence = [
                passport.get("charge_records"),
                passport.get("completed_role_evidence"),
            ]
            if (
                passport.get("ticket") != ticket
                or passport.get("branch") != f"ticket/{ticket}"
                or passport.get("current_state") != "Approved"
                or passport.get("publication_state") != "merged"
                or any(
                    not isinstance(records, list) for records in source_evidence
                )
                or any(
                    item.get("factory_sha") == self.release_path.name
                    for records in source_evidence if isinstance(records, list)
                    for item in records if isinstance(item, dict)
                )
                or not DIGEST.fullmatch(passport.get("passport_sha256", ""))
                or source not in source_history
                or self.release_path.name in source_history
                or done.get("schema")
                != "nysa.software-factory.ticket-done/v1"
                or done.get("ticket") != ticket
                or done.get("kit_sha") not in source_history
                or not passport_head_lineage(
                    passport, done.get("approved_pr_head", "")
                )
                or not isinstance(done.get("pr_number"), int)
                or isinstance(done.get("pr_number"), bool)
                or not SHA.fullmatch(done.get("approved_pr_head", ""))
                or not SHA.fullmatch(done.get("merge_commit", ""))
            ):
                raise ControllerError("qualification source terminal is invalid")
            migrated = self.migrate_passport(claim, "preserve")
            passport = read(passport_path)
            if (
                migrated.get("status") != "ok"
                or migrated.get("passport") != passport.get("passport_sha256")
            ):
                raise ControllerError("qualification terminal passport migration failed")
        elif not marker_path.exists():
            branch = f"refs/heads/ticket/{ticket}"
            worktrees = self.worktrees_by_branch().get(branch, [])
            if len(worktrees) != 1:
                raise ControllerError("qualification terminal adoption cell is ambiguous")
            validation = self.json_call(
                "passport", "validate", "--ticket", ticket,
                "--workdir", worktrees[0], "--json",
            )
            if (
                validation.get("status") != "ok"
                or validation.get("passport") != passport.get("passport_sha256")
            ):
                raise ControllerError("qualification terminal passport is invalid")

        migrations = passport.get("migration_history")
        history = passport.get("factory_release_history")
        edge = migrations[-1] if isinstance(migrations, list) and migrations else {}
        source_passport = edge.get("from_passport_sha256")
        evidence = [
            passport.get("charge_records"), passport.get("completed_role_evidence")
        ]
        candidate_records = [
            item for records in evidence if isinstance(records, list)
            for item in records
            if isinstance(item, dict)
            and item.get("factory_sha") == self.release_path.name
        ]
        history_shas = {
            item.get("factory_sha") for item in history or []
            if isinstance(item, dict)
        }
        pre_candidate_history = {
            item.get("factory_sha") for item in history or []
            if isinstance(item, dict)
            and item.get("factory_sha") != self.release_path.name
        }
        if (
            passport.get("ticket") != ticket
            or passport.get("branch") != f"ticket/{ticket}"
            or passport.get("factory_sha") != self.release_path.name
            or passport.get("current_state") != "Approved"
            or passport.get("publication_state") != "merged"
            or any(not isinstance(records, list) for records in evidence)
            or not DIGEST.fullmatch(passport.get("passport_sha256", ""))
            or passport.get("parent_digest") != source_passport
            or edge.get("schema")
            != "nysa.software-factory.ticket-passport-migration/v2"
            or edge.get("from_factory_sha") not in pre_candidate_history
            or edge.get("to_factory_sha") != self.release_path.name
            or not DIGEST.fullmatch(source_passport or "")
            or not {source, self.release_path.name}.issubset(history_shas)
            or not successor_release_lineage(
                history, migrations, source, self.release_path.name,
            )
            or candidate_records
            or done.get("schema") != "nysa.software-factory.ticket-done/v1"
            or done.get("ticket") != ticket
            or done.get("kit_sha") not in pre_candidate_history
            or not passport_head_lineage(
                passport, done.get("approved_pr_head", "")
            )
            or not isinstance(done.get("pr_number"), int)
            or isinstance(done.get("pr_number"), bool)
            or not SHA.fullmatch(done.get("approved_pr_head", ""))
            or not SHA.fullmatch(done.get("merge_commit", ""))
        ):
            raise ControllerError("qualification terminal adoption is invalid")
        value = {
            "approved_pr_head": done["approved_pr_head"],
            "candidate_passport_sha256": passport["passport_sha256"],
            "done_sha256": hashlib.sha256(canonical(done).encode()).hexdigest(),
            "factory_sha": self.release_path.name,
            "merge_commit": done["merge_commit"],
            "pr_number": done["pr_number"],
            "passport_source_factory_sha": edge["from_factory_sha"],
            "schema": TERMINAL_ADOPTION_SCHEMA,
            "source_current_state": "Approved",
            "source_factory_sha": source,
            "source_passport_sha256": source_passport,
            "source_publication_state": "merged",
            "ticket": ticket,
        }
        if marker_path.exists():
            if read(marker_path) != value:
                raise ControllerError("qualification terminal adoption marker changed")
        elif not self.marker(marker_name, value):
            raise ControllerError("qualification terminal adoption marker raced")
        return value

    def qualification_protected_terminal(
        self, ticket: str,
    ) -> dict[str, Any]:
        done_path = f"factory/attestations/{ticket}/done.json"
        ticket_path = f"factory/tickets/{ticket}.md"
        try:
            terminal = protected_terminal(self.product, ticket)
            result = subprocess.run(
                [
                    "git", "-C", str(self.product), "rev-parse",
                    "refs/remotes/origin/main",
                    "refs/remotes/origin/main^{tree}",
                    f"refs/remotes/origin/main:{ticket_path}",
                ],
                text=True, capture_output=True, check=True, timeout=120,
            )
            protected_main, protected_tree, ticket_blob = result.stdout.splitlines()
            done = json.loads(subprocess.run(
                [
                    "git", "-C", str(self.product), "show",
                    f"refs/remotes/origin/main:{done_path}",
                ],
                text=True, capture_output=True, check=True, timeout=120,
            ).stdout)
        except (
            ProtectedTerminalError, json.JSONDecodeError, OSError,
            subprocess.SubprocessError, ValueError,
        ) as error:
            raise ControllerError(
                "qualification protected terminal is invalid"
            ) from error
        if (
            terminal.get("ticket") != ticket
            or terminal.get("basis") not in {
                "attested-done", "attested-emergency-closeout",
            }
            or not SHA.fullmatch(protected_main)
            or not SHA.fullmatch(protected_tree)
            or not SHA.fullmatch(ticket_blob)
        ):
            raise ControllerError("qualification protected terminal is invalid")
        return {
            "done_sha256": hashlib.sha256(canonical(done).encode()).hexdigest(),
            "protected_main_sha": protected_main,
            "protected_main_tree": protected_tree,
            "protected_ticket_blob": ticket_blob,
            "qualification_charge_micro_usd": 0,
            "reconciliation_schema": PROTECTED_TERMINAL_RECONCILIATION_SCHEMA,
            "terminal_basis": terminal["basis"],
        }

    def qualification_release_receipts(self) -> dict[str, str]:
        try:
            chain = receipt_chain(self.release_path, self.project, self.product)
        except ReceiptError as error:
            raise ControllerError("qualification release receipt is invalid") from error
        result: dict[str, str] = {}
        for kit_sha, receipt_id in chain:
            result.setdefault(kit_sha, receipt_id)
        return result

    def qualification_emergency_terminal(self, ticket: str) -> dict[str, Any]:
        if (
            not self.qualification
            or self.qualification.get("mode") != "successor"
        ):
            raise ControllerError(
                "emergency terminal reconciliation requires successor qualification"
            )
        terminal = self.qualification_protected_terminal(ticket)
        done = json.loads((
            self.product / "factory/attestations" / ticket / "done.json"
        ).read_text(encoding="utf-8"))
        plan = done.get("plan") if isinstance(done, dict) else None
        passport_basis = plan.get("passport") if isinstance(plan, dict) else None
        claim_basis = plan.get("claim") if isinstance(plan, dict) else None
        passport = read(self.state / "passports" / f"{ticket}.json")
        pause_path = self.state / f"pause-{ticket}.json"
        pause = read(pause_path)
        pause_raw = (canonical(pause) + "\n").encode()
        signed_pause = dict(pause)
        pause_receipt = signed_pause.pop("pause_sha256", "")
        expected_passport = {
            name: passport.get(name)
            for name in (
                "passport_sha256", "current_state", "publication_state",
                "factory_sha", "head_sha",
            )
        }
        release_receipts = self.qualification_release_receipts()
        terminal_factory_sha = done.get("kit_sha")
        if (
            terminal.get("terminal_basis") != "attested-emergency-closeout"
            or done.get("schema") not in {
                "nysa.software-factory.ticket-emergency-done/v1",
                "nysa.software-factory.ticket-emergency-done/v2",
            }
            or not SHA.fullmatch(terminal_factory_sha or "")
            or terminal_factory_sha not in release_receipts
            or not isinstance(plan, dict)
            or plan.get("kit_sha") != terminal_factory_sha
            or plan.get("execution_basis") != "authenticated-passport"
            or passport_basis != expected_passport
            or passport.get("factory_sha")
            != self.qualification.get("source_factory_sha")
            or not isinstance(claim_basis, dict)
            or claim_basis.get("status") != "blocked"
            or claim_basis.get("role") != "factory-paused"
            or claim_basis.get("blocked_reason") != "factory-issue-pause"
            or claim_basis.get("parked") is not True
            or claim_basis.get("sha256")
            != hashlib.sha256(pause_raw).hexdigest()
            or claim_basis.get("receipt") != pause_receipt
            or (self.state / "claims" / f"{ticket}.json").exists()
            or pause.get("schema") != "nysa.software-factory.ticket-pause/v2"
            or pause.get("ticket") != ticket
            or pause.get("branch") != passport.get("branch")
            or pause.get("head_sha") != passport.get("head_sha")
            or pause.get("passport_sha256") != passport.get("passport_sha256")
            or pause.get("passport_factory_sha") != passport.get("factory_sha")
            or pause.get("current_state") != passport.get("current_state")
            or pause_receipt != hashlib.sha256(
                canonical(signed_pause).encode()
            ).hexdigest()
            or not DIGEST.fullmatch(pause_receipt)
            or (
                pause.get("status") == "budget"
                and not DIGEST.fullmatch(pause.get("budget_sha256") or "")
            )
            or pause.get("status") not in {"blocked", "budget", "claimed", "waiting"}
        ):
            raise ControllerError(
                "qualification emergency terminal evidence is invalid"
            )
        return {
            **terminal,
            "pause_file_sha256": claim_basis["sha256"],
            "pause_receipt_sha256": pause_receipt,
            "reconciliation_schema": EMERGENCY_TERMINAL_RECONCILIATION_SCHEMA,
            "source_current_state": passport["current_state"],
            "source_factory_sha": passport["factory_sha"],
            "source_head_sha": passport["head_sha"],
            "source_passport_sha256": passport["passport_sha256"],
            "source_publication_state": passport["publication_state"],
            "terminal_factory_sha": terminal_factory_sha,
            "terminal_release_receipt_id": release_receipts[terminal_factory_sha],
        }

    def record_qualification_done_targets(self) -> None:
        if not self.qualification:
            return
        completed = {
            ticket for ticket in self.qualification["tickets"]
            if self.product_ticket_done(ticket)
        }
        if not completed:
            return
        records = []
        for path in sorted(self.events.glob("*.json")):
            value = read(path)
            digest = value.pop("event_sha256", "")
            if (
                value.get("schema") != EVENT_SCHEMA
                or digest != hashlib.sha256(canonical(value).encode()).hexdigest()
            ):
                raise ControllerError("controller event evidence is invalid")
            records.append(value)
        for ticket in sorted(completed):
            matching_complete = [
                item for item in records
                if item.get("factory_sha") == self.release_path.name
                and item.get("event") == "ticket_complete"
                and item.get("ticket") == ticket
            ]
            matching_adoption = [
                item for item in records
                if item.get("factory_sha") == self.release_path.name
                and item.get("event") == "terminal_adopted"
                and item.get("ticket") == ticket
            ]
            matching_reconciliation = [
                item for item in records
                if item.get("factory_sha") == self.release_path.name
                and item.get("event") == "protected_terminal_reconciled"
                and item.get("ticket") == ticket
            ]
            matching_emergency = [
                item for item in records
                if item.get("factory_sha") == self.release_path.name
                and item.get("event") == "emergency_terminal_reconciled"
                and item.get("ticket") == ticket
            ]
            if (
                len(matching_complete) > 1
                or len(matching_adoption) > 1
                or len(matching_reconciliation) > 1
                or len(matching_emergency) > 1
            ):
                raise ControllerError("qualification terminal evidence was duplicated")
            passport_path = self.state / "passports" / f"{ticket}.json"
            if not passport_path.exists():
                if matching_adoption:
                    raise ControllerError(
                        "qualification terminal adoption is unexpected"
                    )
                reconciliation = self.qualification_protected_terminal(ticket)
                if matching_reconciliation:
                    stable = {
                        "done_sha256", "protected_ticket_blob",
                        "qualification_charge_micro_usd",
                        "reconciliation_schema", "terminal_basis",
                    }
                    if any(
                        matching_reconciliation[0].get(name)
                        != reconciliation[name]
                        for name in stable
                    ):
                        raise ControllerError(
                            "qualification protected terminal evidence changed"
                        )
                else:
                    self.event(
                        "protected_terminal_reconciled", ticket,
                        **reconciliation,
                    )
                if not matching_complete:
                    self.event_once("ticket_complete", ticket)
                continue
            if matching_reconciliation:
                raise ControllerError(
                    "qualification passport conflicts with protected terminal reconciliation"
                )
            try:
                done = json.loads((
                    self.product / "factory/attestations" / ticket / "done.json"
                ).read_text(encoding="utf-8"))
            except FileNotFoundError:
                done = {}
            except (json.JSONDecodeError, OSError) as error:
                raise ControllerError(
                    "qualification terminal adoption is incomplete"
                ) from error
            if done.get("schema") in {
                "nysa.software-factory.ticket-emergency-done/v1",
                "nysa.software-factory.ticket-emergency-done/v2",
            }:
                reconciliation = self.qualification_emergency_terminal(ticket)
                stable = {
                    "done_sha256", "pause_file_sha256", "pause_receipt_sha256",
                    "protected_ticket_blob", "qualification_charge_micro_usd",
                    "reconciliation_schema", "source_current_state",
                    "source_factory_sha", "source_head_sha",
                    "source_passport_sha256", "source_publication_state",
                    "terminal_basis", "terminal_factory_sha",
                    "terminal_release_receipt_id",
                }
                if matching_emergency and any(
                    matching_emergency[0].get(name) != reconciliation[name]
                    for name in stable
                ):
                    raise ControllerError(
                        "qualification emergency terminal evidence changed"
                    )
                if not matching_emergency:
                    self.event(
                        "emergency_terminal_reconciled", ticket,
                        **reconciliation,
                    )
                if not matching_complete:
                    self.event_once("ticket_complete", ticket)
                continue
            if matching_emergency:
                raise ControllerError(
                    "qualification emergency terminal reconciliation is unexpected"
                )
            adoption = None
            if self.qualification.get("mode") == "successor":
                passport = read(
                    self.state / "passports" / f"{ticket}.json"
                )
                edge = (
                    passport.get("migration_history", [])[-1]
                    if passport.get("migration_history") else {}
                )
                candidate_evidence = any(
                    item.get("factory_sha") == self.release_path.name
                    for name in ("charge_records", "completed_role_evidence")
                    for item in passport.get(name, []) or []
                    if isinstance(item, dict)
                )
                candidate_publication = any(
                    item.get("factory_sha") == self.release_path.name
                    and item.get("ticket") == ticket
                    and item.get("event") in {
                        "publication_acquired", "publication_released",
                    }
                    for item in records
                )
                adoption_marker = self.state / (
                    "qualification-terminal-adoption-"
                    f"{self.release_path.name}-{ticket}.json"
                )
                source = self.qualification["source_factory_sha"]
                if (
                    passport.get("factory_sha") != self.release_path.name
                    or adoption_marker.exists()
                    or matching_adoption
                    or (
                        passport.get("factory_sha") == self.release_path.name
                        and not candidate_evidence
                        and not candidate_publication
                        and successor_release_lineage(
                            passport.get("factory_release_history"),
                            passport.get("migration_history"),
                            source,
                            self.release_path.name,
                        )
                    )
                ):
                    adoption = self.adopt_qualification_terminal(ticket)
            if adoption:
                details = {
                    "adoption_schema": adoption["schema"],
                    "approved_pr_head": adoption["approved_pr_head"],
                    "candidate_passport_sha256": adoption[
                        "candidate_passport_sha256"
                    ],
                    "done_sha256": adoption["done_sha256"],
                    "merge_commit": adoption["merge_commit"],
                    "passport_source_factory_sha": adoption[
                        "passport_source_factory_sha"
                    ],
                    "pr_number": adoption["pr_number"],
                    "source_current_state": adoption["source_current_state"],
                    "source_factory_sha": adoption["source_factory_sha"],
                    "source_passport_sha256": adoption[
                        "source_passport_sha256"
                    ],
                    "source_publication_state": adoption[
                        "source_publication_state"
                    ],
                }
                if matching_adoption and any(
                    matching_adoption[0].get(name) != value
                    for name, value in details.items()
                ):
                    raise ControllerError("qualification terminal evidence changed")
                if not matching_adoption:
                    self.event("terminal_adopted", ticket, **details)
            elif matching_adoption:
                raise ControllerError("qualification terminal adoption is unexpected")
            if not matching_complete:
                self.event_once("ticket_complete", ticket)

    def record_admission_failure(
        self, error: ControllerError, claims: list[dict[str, Any]]
    ) -> None:
        raw = str(error)
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            decoded = {"error": raw, "reason_code": "unsafe_state"}
        if not isinstance(decoded, dict):
            decoded = {"error": raw, "reason_code": "unsafe_state"}
        evidence = {
            "error": str(decoded.get("error", raw))[:4096],
            "reason_code": str(decoded.get("reason_code", "unsafe_state"))[:64],
        }
        if decoded.get("schema") == MODEL_RESOLUTION_ERROR_SCHEMA:
            try:
                prefix = (
                    "model pin resolution failed"
                    if str(decoded.get("error", "")).startswith(
                        "model pin resolution failed:"
                    )
                    else "model plan failed"
                )
                model_failure = self.model_resolution_failure(
                    {key: value for key, value in decoded.items() if key != "ticket"},
                    prefix,
                )
            except ControllerError:
                evidence = {
                    "error": "model resolution failure is malformed",
                    "reason_code": "unsafe_state",
                }
            else:
                evidence.update(
                    profile_id=model_failure["profile_id"],
                    readiness=model_failure["readiness"],
                )
        ticket = decoded.get("ticket", "")
        if isinstance(ticket, str) and TICKET.fullmatch(ticket):
            evidence["ticket"] = ticket
        digest = hashlib.sha256(canonical(evidence).encode()).hexdigest()
        path = self.state / "admission-incident.json"
        now = time.time_ns()
        previous = read(path) if path.exists() else {}
        same = previous.get("incident_sha256") == digest
        value = {
            "count": int(previous.get("count", 0)) + 1 if same else 1,
            "first_seen_epoch_ns": (
                previous.get("first_seen_epoch_ns", now) if same else now
            ),
            "incident_sha256": digest,
            "last_seen_epoch_ns": now,
            "next_reminder_epoch_ns": (
                previous.get("next_reminder_epoch_ns", now + 900_000_000_000)
                if same else now + 900_000_000_000
            ),
            "schema": "nysa.software-factory.admission-incident/v1",
            **evidence,
        }
        reminder = same and now >= value["next_reminder_epoch_ns"]
        if reminder:
            value["next_reminder_epoch_ns"] = now + 900_000_000_000
        write(path, value)
        if not same or reminder:
            name = (
                "admission_blocked_reminder" if reminder else "admission_blocked"
            )
            details = dict(
                error=evidence["error"],
                existing_claims=sorted(item["ticket"] for item in claims),
                incident_sha256=digest,
                reason_code=evidence["reason_code"],
            )
            if "readiness" in evidence:
                details.update(
                    profile_id=evidence["profile_id"],
                    readiness=evidence["readiness"],
                )
            if "ticket" in evidence:
                self.event(name, evidence["ticket"], **details)
            else:
                self.event(name, **details)

    def clear_admission_failure(self) -> None:
        if not self.admission_refusals:
            (self.state / "admission-incident.json").unlink(missing_ok=True)

    def record_dispatch_refusal(
        self, refusal: Any, claims: list[dict[str, Any]]
    ) -> None:
        errors = {
            "initiative_missing": {"ticket initiative is missing"},
            "invalid_ticket_contract": {
                "ticket dependencies are invalid",
                "provider-free ticket readiness contract is not executable",
            },
        }
        if (
            not isinstance(refusal, dict)
            or set(refusal) != {"error", "reason_code", "ticket"}
            or refusal.get("error") not in errors.get(refusal.get("reason_code"), set())
            or not TICKET.fullmatch(refusal.get("ticket", ""))
        ):
            raise ControllerError("dispatch admission refusal is malformed")
        ticket = refusal["ticket"]
        if ticket in self.admission_refusals:
            return
        result = {**refusal, "status": "skipped"}
        self.admission_refusals[ticket] = result
        self.record_admission_failure(
            ControllerError(canonical(refusal)), claims
        )

    def marker(self, name: str, value: dict[str, Any] | None = None) -> bool:
        path = self.state / f"{name}.json"
        if value is None:
            return path.exists()
        try:
            descriptor = os.open(
                path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
        except FileExistsError:
            return False
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(canonical(value) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        return True

    def qualification_marker(self, name: str, create: bool = False) -> bool:
        if self.qualification is None:
            raise ControllerError("qualification marker requires qualification")
        expected = {
            "factory_sha": self.release_path.name,
            "schema": EVENT_SCHEMA,
            "tickets": sorted(self.qualification["tickets"]),
        }
        scoped = f"{name}-{self.release_path.name}"
        path = self.state / f"{scoped}.json"
        if not path.exists():
            if not create:
                return False
            if self.marker(scoped, expected):
                return True
        try:
            value = read(path)
        except (json.JSONDecodeError, OSError) as error:
            raise ControllerError("qualification marker is invalid") from error
        if value != expected:
            raise ControllerError("qualification marker is invalid")
        return True

    def read_capacity(self) -> int:
        values = re.findall(
            r"^(?:export\s+)?MAX_CONCURRENT_TICKETS\s*=\s*['\"]?([0-9]+)['\"]?\s*$",
            (self.product / "factory/PROJECT.env").read_text(encoding="utf-8"),
            re.M,
        )
        if len(values) > 1:
            raise ControllerError("MAX_CONCURRENT_TICKETS is ambiguous")
        capacity = int(values[0]) if values else 4
        if not 1 <= capacity <= 4:
            raise ControllerError("Contract 1.8 controller capacity must be 1 through 4")
        return capacity

    def call(self, *arguments: str, timeout: int | None = 300) -> subprocess.CompletedProcess:
        return subprocess.run(
            [str(self.launcher), self.project, *arguments],
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout,
        )

    def json_call(
        self, *arguments: str, allow: tuple[int, ...] = (0,),
        timeout: int | None = 300,
    ) -> dict[str, Any]:
        result = self.call(*arguments, timeout=timeout)
        if result.returncode == 75:
            try:
                wait = json.loads(result.stdout or result.stderr)
            except json.JSONDecodeError as error:
                raise ControllerError(
                    "external wait response is malformed"
                ) from error
            if wait != {
                "reason_code": "external_unavailable", "status": "wait",
            }:
                raise ControllerError("external wait response is malformed")
            raise ExternalUnavailable("external service is temporarily unavailable")
        if result.returncode not in allow:
            if temporarily_unavailable(result.stderr or result.stdout):
                raise ExternalUnavailable(
                    "external service is temporarily unavailable"
                )
            raise ControllerError(
                result.stderr.strip() or result.stdout.strip() or "launcher command failed"
            )
        try:
            value = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise ControllerError("launcher returned malformed JSON") from error
        if not isinstance(value, dict):
            raise ControllerError("launcher returned malformed JSON")
        return value

    @staticmethod
    def model_resolution_failure(
        value: dict[str, Any], prefix: str,
    ) -> dict[str, Any]:
        if set(value) != {
            "error", "profile_id", "readiness", "reason_code", "schema",
            "status",
        }:
            raise ControllerError("model resolution failure is malformed")
        reason = value.get("reason_code")
        profile = value.get("profile_id")
        readiness = value.get("readiness")
        if (
            value.get("schema") != MODEL_RESOLUTION_ERROR_SCHEMA
            or value.get("status") != "error"
            or not isinstance(reason, str)
            or not SAFE_MODEL_ID.fullmatch(reason)
            or not isinstance(profile, str)
            or not SAFE_MODEL_ID.fullmatch(profile)
            or value.get("error") != f"{prefix}: {reason}"
            or not isinstance(readiness, dict)
            or len(readiness) > 64
            or (
                reason in {
                    "profile_resolution_failed",
                    "profile_temporarily_unavailable",
                }
                and not readiness
            )
        ):
            raise ControllerError("model resolution failure is malformed")
        expected = {"adapter_version", "reason", "reported_identity", "state"}
        for route_id, evidence in readiness.items():
            if (
                not isinstance(route_id, str)
                or not SAFE_MODEL_ID.fullmatch(route_id)
                or not isinstance(evidence, dict)
                or set(evidence) != expected
                or evidence.get("state")
                not in {"READY", "UNAVAILABLE", "INVALID", "UNKNOWN"}
                or not isinstance(evidence.get("reason"), str)
                or not SAFE_MODEL_ID.fullmatch(evidence["reason"])
            ):
                raise ControllerError("model resolution failure is malformed")
            for name in ("adapter_version", "reported_identity"):
                text = evidence.get(name)
                if (
                    not isinstance(text, str)
                    or not SAFE_MODEL_TEXT.fullmatch(text)
                    or re.search(r"(?i)\b[A-Za-z][A-Za-z0-9+.-]*://", text)
                    or re.search(
                        r"(?i)[A-Za-z0-9_.-]*"
                        r"(?:key|token|secret|password|url|dsn|conn|auth)"
                        r"[A-Za-z0-9_.-]*\s*[:=]", text,
                    )
                ):
                    raise ControllerError("model resolution failure is malformed")
        return value

    @staticmethod
    def preflight_refusal_evidence(value: dict[str, Any]) -> dict[str, Any]:
        output = value.get("output")
        exit_code = value.get("exit_code")
        if (
            value.get("status") != "error"
            or isinstance(exit_code, bool)
            or not isinstance(exit_code, int)
            or not 1 <= exit_code <= 255
            or not isinstance(output, str)
            or len(output.encode("utf-8")) > 1_048_576
        ):
            raise ControllerError("preflight refusal evidence is malformed or oversized")
        lines = []
        for raw in output.replace("\x00", "").splitlines():
            line = raw.strip()
            if not re.search(r"(?:^|\b)(?:FAIL:|PREFLIGHT FAIL|READINESS BLOCKED:)", line):
                continue
            line = re.sub(
                r"(?i)([A-Za-z0-9_.-]*(?:key|token|secret|password|auth)"
                r"[A-Za-z0-9_.-]*\s*[:=]\s*)\S+",
                r"\1[redacted]", line,
            )
            line = re.sub(
                r"(?i)\b[A-Za-z][A-Za-z0-9+.-]*://\S+", "[redacted-url]", line,
            )
            lines.append(line[:500])
            if len(lines) == 8:
                break
        return {
            "preflight_exit_code": exit_code,
            "preflight_failure_lines": lines,
            "preflight_output_sha256": hashlib.sha256(output.encode()).hexdigest(),
            "preflight_reason_code": "deterministic_refusal" if lines else "unclassified_refusal",
        }

    def claim_path(self, ticket: str) -> Path:
        return self.claims / f"{ticket}.json"

    def pause_path(self, ticket: str) -> Path:
        return self.state / f"pause-{ticket}.json"

    @staticmethod
    def resume_state(worktree: str, ticket: str, current_state: str) -> str | None:
        path = Path(worktree) / "factory" / "tickets" / f"{ticket}.md"
        values = re.findall(
            r"^Resume-State:\s*(.*?)\s*$",
            path.read_text(encoding="utf-8"), re.I | re.M,
        )
        if len(values) > 1 or (
            current_state == "Blocked-Escalated"
            and (
                len(values) != 1
                or values[0] not in {
                    "Backlog", "Ready", "Planning", "Building", "Review",
                }
            )
        ):
            raise ControllerError("ticket Resume-State is invalid")
        return values[0] if values else None

    def envelope_digest(self) -> str:
        digest = hashlib.sha256(
            (self.product / "factory/ENVELOPE.env").read_bytes()
        )
        overrides = self.product / "factory/envelope-overrides"
        if overrides.is_dir():
            for path in sorted(overrides.glob("*.json")):
                digest.update(path.name.encode())
                digest.update(b"\0")
                digest.update(path.read_bytes())
        return digest.hexdigest()

    @staticmethod
    def runnable(claim: dict[str, Any]) -> bool:
        return (
            claim["status"] not in {"blocked", "budget"}
            and not (
                claim["status"] == "waiting"
                and str(claim.get("blocked_reason", "")).startswith(
                    "semantic-round-authorization:"
                )
            )
        )

    @staticmethod
    def typed_launch_void(terminal: dict[str, str]) -> bool:
        return (
            terminal.get("phase") == "abandoned"
            and terminal.get("accounting_state") == "launch_void"
            and terminal.get("go_issued") == "0"
            and terminal.get("task_submitted") == "0"
            and terminal.get("effective_cost") == "0"
            and terminal.get("cost_basis") == "launch_void"
        )

    @staticmethod
    def consumes_capacity(claim: dict[str, Any]) -> bool:
        return (
            claim["status"] in {"claimed", "running"}
            and claim.get("parked") is not True
            and claim.get("lease_released") is not True
            and bool(DIGEST.fullmatch(claim.get("lease", "")))
        )

    def qualification_cohort_accounted(
        self, claims: list[dict[str, Any]],
    ) -> bool:
        if not self.qualification:
            return False
        selected = set(self.qualification["tickets"])
        claimed = {claim["ticket"] for claim in claims}
        return claimed <= selected and all(
            ticket in claimed or self.product_ticket_done(ticket)
            for ticket in selected
        )

    @staticmethod
    def parked(claim: dict[str, Any]) -> bool:
        return claim.get("parked") is True

    def save_claim(self, claim: dict[str, Any]) -> None:
        context = getattr(self.recovery_context, "value", None)
        if context and context["ticket"] == claim.get("ticket"):
            if context["attempt"] is None:
                claim.pop("recovery_attempt", None)
            else:
                claim["recovery_attempt"] = dict(context["attempt"])
        write(self.claim_path(claim["ticket"]), claim)

    def wait_for_recovery_receipt(self, claim: dict[str, Any]) -> bool:
        context = getattr(self.recovery_context, "value", None)
        if context is None or context.get("ticket") != claim.get("ticket"):
            return False
        receipt = claim.get("receipt", "")
        current = self.operator_transition(claim)
        if (
            not DIGEST.fullmatch(receipt)
            or current is None
            or current.get("receipt_sha256") != receipt
            or current.get("consumed") is not False
        ):
            return False
        context["attempt"] = context["prior_attempt"]
        context["waiting_receipt_sha256"] = receipt
        self.save_claim(claim)
        return True

    @staticmethod
    def valid_recovery_attempt(value: Any) -> bool:
        if not isinstance(value, dict) or set(value) != {
            "count", "factory_sha", "input_sha256", "outcome_sha256",
            "phase", "recovery", "retry_reason", "retry_status",
        }:
            return False
        count = value.get("count")
        phase = value.get("phase")
        return (
            all(isinstance(value.get(key), str) for key in {
                "factory_sha", "input_sha256", "outcome_sha256", "phase",
                "recovery", "retry_reason", "retry_status",
            })
            and SHA.fullmatch(value.get("factory_sha", "")) is not None
            and re.fullmatch(r"[a-z][a-z0-9-]{0,63}", value.get("recovery", ""))
            is not None
            and re.fullmatch(
                r"[A-Za-z0-9._:-]{0,256}", value.get("retry_reason", "")
            ) is not None
            and value.get("retry_status") in {
                "claimed", "running", "waiting", "blocked", "budget",
            }
            and DIGEST.fullmatch(value.get("input_sha256", "")) is not None
            and (
                value.get("outcome_sha256") == ""
                or DIGEST.fullmatch(value.get("outcome_sha256", "")) is not None
            )
            and isinstance(count, int)
            and not isinstance(count, bool)
            and 0 <= count <= RECOVERY_ATTEMPT_LIMIT
            and ((count == 0) == (value["outcome_sha256"] == ""))
            and phase in {"pending", "settled", "abandoning", "abandoned"}
            and (phase != "pending" or count < RECOVERY_ATTEMPT_LIMIT)
            and (phase != "settled" or 0 < count < RECOVERY_ATTEMPT_LIMIT)
            and (
                phase not in {"abandoning", "abandoned"}
                or count == RECOVERY_ATTEMPT_LIMIT
            )
        )

    @staticmethod
    def recovery_outcome_sha256(
        claim: dict[str, Any], error: str = "",
    ) -> str:
        return hashlib.sha256(canonical({
            "blocked_reason": claim.get("blocked_reason", ""),
            "error": error,
            "parked": claim.get("parked") is True,
            "status": claim.get("status", ""),
        }).encode()).hexdigest()

    def targeted_recovery_evidence_sha256(
        self, claim: dict[str, Any],
    ) -> str:
        evidence: dict[str, str] = {}
        mapping_path = Path(os.environ.get("FACTORY_OPERATOR_MAP", ""))
        ticket_receipts = (
            self.state / "operator-receipts" / claim["ticket"]
        )
        expected_map = self.state.parent / "operator/operator-map.json"
        if (
            (self.qualification or {}).get("mode") == "successor"
            and os.environ.get("FACTORY_QUALIFICATION_MODE") == "isolated"
            and mapping_path.is_absolute()
            and mapping_path == expected_map
            and ticket_receipts.is_dir()
        ):
            try:
                info = ticket_receipts.lstat()
                if (
                    ticket_receipts.is_symlink()
                    or ticket_receipts.resolve(strict=True) != ticket_receipts
                    or not stat.S_ISDIR(info.st_mode)
                    or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) != 0o700
                ):
                    raise ControllerError(
                        "qualification resume receipt state is unsafe"
                    )
                operator = operator_fields(
                    read(mapping_path), claim["ticket"],
                )
                if operator:
                    action, binding = operator_action(operator)
                    receipt = operator_receipt.peek_exact(
                        self.state, claim["ticket"], action,
                        operator["receipt_sha256"], {
                            **binding,
                            "blocked_receipt_sha256": claim.get("receipt", ""),
                        },
                    )
                    if action == "resume" and receipt is not None:
                        evidence["operator_resume"] = receipt["receipt_sha256"]
            except (OSError, TypeError, ValueError) as error:
                raise ControllerError(
                    "qualification resume recovery evidence is invalid"
                ) from error
        return (
            hashlib.sha256(canonical(evidence).encode()).hexdigest()
            if evidence else ""
        )

    def recovery_input_sha256(
        self, claim: dict[str, Any], recovery: str,
    ) -> str:
        ticket = claim["ticket"]
        passport_path = self.state / "passports" / f"{ticket}.json"
        passport_sha256 = ""
        if passport_path.exists() or passport_path.is_symlink():
            passport_sha256 = hashlib.sha256(
                canonical(read(passport_path)).encode()
            ).hexdigest()
        worktree = Path(claim.get("worktree", ""))
        git_evidence = {
            "branch": "", "head": "", "status_sha256": "",
            "ticket_blob": "", "ticket_sha256": "",
        }
        if worktree.is_absolute() and worktree.is_dir():
            head = subprocess.run(
                ["git", "-C", str(worktree), "rev-parse", "HEAD"],
                text=True, capture_output=True, check=False, timeout=120,
            )
            branch = subprocess.run(
                [
                    "git", "-C", str(worktree), "symbolic-ref", "--quiet",
                    "--short", "HEAD",
                ],
                text=True, capture_output=True, check=False, timeout=120,
            )
            status = subprocess.run(
                ["git", "-C", str(worktree), "status", "--porcelain=v1", "-z"],
                text=True, capture_output=True, check=False, timeout=120,
            )
            ticket_path = (
                worktree / "factory" / "tickets" / f"{ticket}.md"
            )
            ticket_sha256 = ""
            if ticket_path.exists() or ticket_path.is_symlink():
                info = ticket_path.lstat()
                if (
                    not stat.S_ISREG(info.st_mode)
                    or ticket_path.is_symlink()
                    or info.st_uid != os.geteuid()
                    or info.st_nlink != 1
                    or stat.S_IMODE(info.st_mode) & 0o022
                    or info.st_size > 1_000_000
                ):
                    raise ControllerError("recovery ticket evidence is unsafe")
                ticket_sha256 = hashlib.sha256(ticket_path.read_bytes()).hexdigest()
            ticket_blob = subprocess.run(
                [
                    "git", "-C", str(worktree), "rev-parse",
                    f"HEAD:factory/tickets/{ticket}.md",
                ],
                text=True, capture_output=True, check=False, timeout=120,
            )
            git_evidence = {
                "branch": branch.stdout.strip() if branch.returncode == 0 else "",
                "head": head.stdout.strip() if head.returncode == 0 else "",
                "status_sha256": (
                    hashlib.sha256(status.stdout.encode()).hexdigest()
                    if status.returncode == 0 else ""
                ),
                "ticket_blob": (
                    ticket_blob.stdout.strip()
                    if ticket_blob.returncode == 0 else ""
                ),
                "ticket_sha256": ticket_sha256,
            }
        value = {
            "branch": claim.get("branch", ""),
            "blocked_reason": claim.get("blocked_reason", ""),
            "factory_sha": self.release_path.name,
            "git": git_evidence,
            "passport_sha256": passport_sha256,
            "priority": claim.get("priority", ""),
            "qualification_manifest_sha256": self.qualification_manifest_sha256,
            "receipt": claim.get("receipt", ""),
            "recovery": recovery,
            "role": claim.get("role", ""),
            "run_snapshot_sha256": self.ticket_run_snapshot(ticket),
            "status": claim.get("status", ""),
            "ticket": ticket,
            "worktree": str(worktree),
        }
        if recovery == "targeted-repair":
            external = self.targeted_recovery_evidence_sha256(claim)
            if external:
                value["external_evidence_sha256"] = external
        return hashlib.sha256(canonical(value).encode()).hexdigest()

    def worktree_records(self) -> list[dict[str, str]]:
        records: list[dict[str, str]] = []
        current: dict[str, str] = {}
        output = subprocess.run(
            ["git", "-C", str(self.product), "worktree", "list", "--porcelain"],
            text=True, capture_output=True, check=True, timeout=120,
        ).stdout
        for line in output.splitlines() + [""]:
            if line:
                name, _, item = line.partition(" ")
                current[name] = item
                continue
            if current:
                records.append(current)
            current = {}
        return records

    def worktrees_by_branch(self) -> dict[str, list[str]]:
        result: dict[str, list[str]] = {}
        for record in self.worktree_records():
            branch = record.get("branch", "")
            worktree = record.get("worktree", "")
            if branch and worktree:
                result.setdefault(branch, []).append(worktree)
        return result

    def dispatcher_lease_records(self) -> dict[str, dict[str, Any]]:
        directory = self.product / "factory/.dispatch-leases"
        try:
            info = directory.lstat()
        except FileNotFoundError:
            return {}
        if (
            directory.is_symlink()
            or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o022
        ):
            raise ControllerError("dispatcher lease state is unsafe")
        records: dict[str, dict[str, Any]] = {}
        lease_ids: set[str] = set()
        for path in sorted(directory.iterdir()):
            if not re.fullmatch(r"T-[0-9]+[.]json", path.name):
                raise ControllerError("dispatcher lease state is unsafe")
            value = read(path)
            ticket = value.get("ticket", "")
            lease_id = value.get("lease_id", "")
            claimed = value.get("claimed_epoch")
            expires = value.get("expires_epoch")
            if (
                set(value) != {
                    "schema_version", "ticket", "lease_id",
                    "claimed_epoch", "expires_epoch",
                }
                or value.get("schema_version") != 1
                or ticket != path.stem
                or not TICKET.fullmatch(ticket)
                or not DIGEST.fullmatch(lease_id)
                or ticket in records
                or lease_id in lease_ids
                or isinstance(claimed, bool)
                or not isinstance(claimed, int)
                or isinstance(expires, bool)
                or not isinstance(expires, int)
                or expires <= claimed
            ):
                raise ControllerError("dispatcher lease state is unsafe")
            records[ticket] = value
            lease_ids.add(lease_id)
        return records

    def active_run_tickets(self) -> set[str]:
        directory = self.product / "factory/.active-runs"
        try:
            info = directory.lstat()
        except FileNotFoundError:
            return set()
        if (
            directory.is_symlink()
            or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o022
        ):
            raise ControllerError("active-run state is unsafe")
        tickets = set()
        for path in sorted(directory.iterdir()):
            match = re.fullmatch(
                r"(T-[0-9]+)[.][A-Za-z0-9_-]+[.](lock|pid)", path.name,
            )
            if match is None:
                raise ControllerError("active-run state is unsafe")
            item = path.lstat()
            if (
                path.is_symlink()
                or item.st_uid != os.geteuid()
                or item.st_mode & 0o022
                or (
                    match.group(2) == "lock"
                    and not stat.S_ISDIR(item.st_mode)
                )
                or (
                    match.group(2) == "pid"
                    and (
                        not stat.S_ISREG(item.st_mode)
                        or item.st_nlink != 1
                        or item.st_size > 10_000
                    )
                )
            ):
                raise ControllerError("active-run state is unsafe")
            tickets.add(match.group(1))
        return tickets

    def reclaim_orphaned_execution_cells(
        self, claims: list[dict[str, Any]],
    ) -> None:
        """Reclaim clean bounded cells after their durable claim is gone."""
        roots = []
        if self.worktree_root is not None:
            roots.append(self.worktree_root)
        if self.qualification:
            roots.append(self.state / "cells")
        claimed = {
            Path(claim["worktree"]).resolve()
            for claim in claims
        }
        for candidate in dict.fromkeys(roots):
            if not candidate.exists() and not candidate.is_symlink():
                continue
            root = safe_directory(candidate)
            descriptor = os.open(
                root / ".dispatch-admission.lock",
                os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            try:
                info = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.geteuid()
                    or info.st_nlink != 1
                    or stat.S_IMODE(info.st_mode) != 0o600
                ):
                    raise ControllerError("admission lock is unsafe")
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                with self.git_lock:
                    leased = set(self.dispatcher_lease_records())
                    active = self.active_run_tickets()
                    for record in self.worktree_records():
                        value = record.get("worktree", "")
                        if not value:
                            continue
                        worktree = Path(value)
                        if (
                            not worktree.is_absolute()
                            or worktree.parent != root
                            or not re.fullmatch(r"cell-[1-6]", worktree.name)
                            or "locked" in record
                            or "prunable" in record
                        ):
                            continue
                        branch = record.get("branch", "")
                        match = re.fullmatch(
                            r"refs/heads/ticket/(T-[0-9]+)", branch,
                        )
                        ticket = match.group(1) if match else ""
                        if not ticket and "detached" not in record:
                            continue
                        cell = worktree.resolve(strict=True)
                        cell_info = cell.lstat()
                        if (
                            worktree != cell
                            or not stat.S_ISDIR(cell_info.st_mode)
                            or cell_info.st_uid != os.geteuid()
                            or cell_info.st_mode & 0o022
                            or cell in claimed
                            or (
                                ticket
                                and (ticket in leased or ticket in active)
                            )
                            or (not ticket and (leased or active))
                        ):
                            continue
                        clean = subprocess.run(
                            [
                                "git", "-C", str(cell), "status",
                                "--porcelain=v1", "-z",
                            ],
                            text=True, capture_output=True, check=True, timeout=120,
                        ).stdout == ""
                        if not clean:
                            continue
                        subprocess.run(
                            [
                                "git", "-C", str(self.product), "worktree",
                                "remove", str(cell),
                            ],
                            check=True, timeout=120,
                        )
                        self.event(
                            "execution_cell_reclaimed", ticket,
                            worktree=str(cell),
                        )
            finally:
                os.close(descriptor)

    def valid_claim_document(self, path: Path, value: dict[str, Any]) -> bool:
        lease = value.get("lease")
        return not (
            value.get("schema") != CLAIM_SCHEMA
            or path.name != f"{value.get('ticket')}.json"
            or not TICKET.fullmatch(value.get("ticket", ""))
            or (
                not isinstance(lease, str)
                or (
                    DIGEST.fullmatch(lease) is None
                    and value.get("parked") is not True
                )
            )
            or not Path(value.get("worktree", "")).is_absolute()
            or (
                value.get("parked") not in {None, True}
                or (
                    value.get("parked") is True
                    and (
                        Path(value["worktree"]).name != value["ticket"]
                        or Path(value["worktree"]).parent.name != "parked"
                    )
                )
            )
            or value.get("status") not in {
                "claimed", "running", "waiting", "blocked", "budget",
            }
            or (
                "recovery_attempt" in value
                and not self.valid_recovery_attempt(value["recovery_attempt"])
            )
        )

    def load_claims(self) -> list[dict[str, Any]]:
        result = []
        for path in sorted(self.claims.glob("T-*.json")):
            value = read(path)
            lease = value.get("lease")
            if not self.valid_claim_document(path, value):
                raise ControllerError("controller claim is malformed")
            if lease and DIGEST.fullmatch(lease) is None:
                self.invalid_transition_tickets.add(value["ticket"])
                self.event_once(
                    "controller_claim_invalid", value["ticket"],
                    reason_code="lease_invalid",
                )
            if value["status"] == "budget":
                if not DIGEST.fullmatch(value.get("budget_sha256", "")):
                    raise ControllerError("budget wait claim is malformed")
                if value["budget_sha256"] != self.envelope_digest():
                    lease = self.json_call("claim", "--ticket", value["ticket"])
                    if (
                        lease.get("schema_version") != 1
                        or lease.get("ticket") != value["ticket"]
                        or not DIGEST.fullmatch(lease.get("lease_id", ""))
                    ):
                        raise ControllerError("reopened budget lease is invalid")
                    value.pop("budget_sha256")
                    value.update(
                        lease=lease["lease_id"], receipt="", role="",
                        status="claimed",
                    )
                    self.save_claim(value)
                    self.event("budget_reopened", value["ticket"])
            worktree = Path(value["worktree"])
            if not worktree.exists():
                matches = []
                current: dict[str, str] = {}
                output = subprocess.run(
                    ["git", "-C", str(self.product), "worktree", "list", "--porcelain"],
                    text=True, capture_output=True, check=True, timeout=120,
                ).stdout
                for line in output.splitlines() + [""]:
                    if line:
                        name, _, item = line.partition(" ")
                        current[name] = item
                        continue
                    if current.get("branch") == f"refs/heads/{value['branch']}":
                        matches.append(current.get("worktree", ""))
                    current = {}
                if len(matches) != 1 or not Path(matches[0]).is_absolute():
                    raise ControllerError("controller claim worktree is unavailable")
                value["worktree"] = matches[0]
                self.save_claim(value)
                self.event("worktree_recovered", value["ticket"], worktree=matches[0])
            result.append(value)
        return result

    def qualification_admission_preflight(
        self, existing: list[dict[str, Any]],
    ) -> dict[str, str] | None:
        if (
            not self.qualification
            or self.qualification_cohort_accounted(existing)
        ):
            return None
        try:
            value = self.json_call(
                "dispatch-plan", "--shadow", "--json", allow=(0, 2),
            )
            pair = (value.get("status"), value.get("action"))
            valid = (
                value.get("schema")
                == "nysa.software-factory.dispatch-plan/v1"
                and (
                    pair == ("WAIT", "WAIT")
                    or (
                        pair == ("SHADOW", "SHADOW")
                        and isinstance(value.get("ticket"), str)
                        and TICKET.fullmatch(value.get("ticket", ""))
                    )
                    or (
                        pair == ("error", "ESCALATE")
                        and value.get("reason_code") == "unsafe_state"
                        and isinstance(value.get("error"), str)
                        and 0 < len(value["error"]) <= 500
                    )
                )
            )
            if not valid:
                raise ControllerError(
                    "qualification admission preflight response is malformed"
                )
            if pair != ("error", "ESCALATE"):
                return None
        except (ControllerError, OSError, subprocess.SubprocessError):
            pass
        failure = {
            "error": "qualification admission preflight failed",
            "reason_code": "qualification_admission_preflight_failed",
            "status": "error",
        }
        self.record_admission_failure(
            ControllerError(canonical(failure)), existing,
        )
        return failure

    def claim_new(
        self, existing: list[dict[str, Any]], reserved_capacity: int = 0
    ) -> list[dict[str, Any]]:
        claims = list(existing)
        if not 0 <= reserved_capacity <= self.capacity:
            raise ControllerError("reserved controller capacity is invalid")
        cohort_ack_path = self.state / "qualification-claim-ack.json"
        pending_ack = None
        if self.qualification and cohort_ack_path.exists():
            pending_ack = read(cohort_ack_path)
            if (
                set(pending_ack) != {"schema", "transaction_sha256"}
                or pending_ack.get("schema")
                != "nysa.software-factory.qualification-claim-ack/v1"
                or not DIGEST.fullmatch(pending_ack.get("transaction_sha256", ""))
            ):
                raise ControllerError("qualification claim acknowledgement is malformed")
        if (
            pending_ack is not None
            and self.qualification_cohort_accounted(claims)
        ):
            acknowledged = self.json_call(
                "dispatch-plan", "--claim", "--cohort-ack",
                pending_ack["transaction_sha256"], "--json",
            )
            if (
                acknowledged.get("schema")
                != "nysa.software-factory.dispatch-plan/v1"
                or acknowledged.get("action") != "ACK"
                or acknowledged.get("status") != "ACKNOWLEDGED"
                or acknowledged.get("transaction_sha256")
                != pending_ack["transaction_sha256"]
            ):
                raise ControllerError("qualification claim acknowledgement is malformed")
            cohort_ack_path.unlink()
        if self.qualification_cohort_accounted(claims):
            return claims
        if not self.qualification:
            self.model_admission_outcome = None
        if self.qualification:
            readiness = self.json_call("models", "qualification-readiness", "--json")
            if (
                readiness.get("schema")
                != "nysa.software-factory.qualification-fallback-readiness/v1"
                or readiness.get("status") != "ready"
                or not DIGEST.fullmatch(readiness.get("readiness_sha256", ""))
                or (
                    self.qualification_fallback_readiness_sha256
                    and readiness.get("readiness_sha256")
                    != self.qualification_fallback_readiness_sha256
                )
            ):
                raise ControllerError("qualification fallback readiness drifted")
        excluded = sorted(
            self.invalid_transition_tickets | self.prior_transition_tickets | {
            item["ticket"]
            for item in claims if not self.consumes_capacity(item)
            }
        )
        capacity_used = len([
            item for item in claims
            if self.consumes_capacity(item)
            and item["ticket"] not in self.invalid_transition_tickets
        ]) + reserved_capacity
        if not self.qualification and capacity_used < self.capacity:
            shadow_arguments = ["dispatch-plan", "--shadow"]
            for ticket in excluded:
                shadow_arguments.extend(["--exclude-ticket", ticket])
            shadow = self.json_call(*shadow_arguments, "--json")
            if "admission_refusal" in shadow:
                self.record_dispatch_refusal(shadow["admission_refusal"], claims)
            if shadow.get("action") == "WAIT":
                return claims
            if (
                shadow.get("action") != "SHADOW"
                or not TICKET.fullmatch(shadow.get("ticket", ""))
            ):
                raise ControllerError("dispatch shadow is malformed")
            plan = None if self.repository_test else self.json_call(
                "models", "plan", "--json", allow=(0, 2), timeout=None,
            )
            if plan is not None and plan.get("status") == "error":
                failure = self.model_resolution_failure(
                    plan, "model plan failed",
                )
                if failure["reason_code"] == "profile_temporarily_unavailable":
                    self.model_admission_outcome = {
                        "profile_id": failure["profile_id"],
                        "readiness": failure["readiness"],
                        "reason_code": failure["reason_code"],
                        "status": "waiting",
                        "ticket": shadow["ticket"],
                    }
                    self.event(
                        "model_admission_wait", shadow["ticket"],
                        profile_id=failure["profile_id"],
                        reason_code=failure["reason_code"],
                        readiness=failure["readiness"],
                    )
                    return claims
                self.model_admission_outcome = {
                    "profile_id": failure["profile_id"],
                    "readiness": failure["readiness"],
                    "reason_code": failure["reason_code"],
                    "status": "error",
                    "ticket": shadow["ticket"],
                }
                raise ControllerError(canonical({
                    **failure, "ticket": shadow["ticket"],
                }))
            if plan is not None and (
                plan.get("schema")
                not in {"model-resolution-plan/v1", "model-resolution-plan/v2"}
                or not isinstance(plan.get("profile_id"), str)
                or not SAFE_MODEL_ID.fullmatch(plan["profile_id"])
                or not DIGEST.fullmatch(plan.get("profile_hash", ""))
                or not isinstance(plan.get("selections"), dict)
                or set(plan["selections"])
                != {
                    "planner", "builder", "narrator", "spec-linter",
                    "test-author", "reviewer",
                }
            ):
                raise ControllerError("model admission plan is malformed")
        if self.qualification:
            available = self.capacity - capacity_used
            if available <= 0:
                return claims
            arguments = [
                "dispatch-plan", "--claim", "--cohort", "--cohort-limit",
                str(available),
            ]
            for ticket in excluded:
                arguments.extend(["--exclude-ticket", ticket])
            value = self.json_call(*arguments, "--json")
            if value.get("action") == "WAIT":
                return claims
            if self.legacy_dispatch_fixture and value.get("action") == "START":
                # Older component fixtures model the pre-batch dispatcher one
                # START at a time. Keep that test-only seam while sealed lanes
                # require the durable START_BATCH transaction below.
                while True:
                    if (
                        not TICKET.fullmatch(value.get("ticket", ""))
                        or not DIGEST.fullmatch(value.get("lease_id", ""))
                    ):
                        raise ControllerError("dispatch claim is malformed")
                    claim = {
                        "branch": value["branch"], "lease": value["lease_id"],
                        "priority": value.get("priority", "none"),
                        "publication_lease": "", "receipt": "", "role": "",
                        "schema": CLAIM_SCHEMA, "status": "claimed",
                        "ticket": value["ticket"], "worktree": value["worktree"],
                    }
                    self.save_claim(claim)
                    self.event(
                        "ticket_claimed", claim["ticket"], branch=claim["branch"],
                        preprovider_reset_head=value.get("preprovider_reset_head"),
                        worktree=claim["worktree"],
                    )
                    claims.append(claim)
                    excluded.append(claim["ticket"])
                    if len([
                        item for item in claims if self.consumes_capacity(item)
                        and item["ticket"] not in self.invalid_transition_tickets
                    ]) + reserved_capacity >= self.capacity:
                        return claims
                    next_arguments = ["dispatch-plan", "--claim"]
                    for ticket in sorted(set(excluded)):
                        next_arguments.extend(["--exclude-ticket", ticket])
                    value = self.json_call(*next_arguments, "--json")
                    if value.get("action") == "WAIT":
                        return claims
                    if value.get("action") != "START":
                        raise ControllerError("dispatch claim is malformed")
            batch = value.get("claims")
            transaction_sha256 = value.get("transaction_sha256", "")
            existing_tickets = {item["ticket"] for item in claims}
            if (
                value.get("schema") != "nysa.software-factory.dispatch-plan/v1"
                or value.get("action") != "START_BATCH"
                or value.get("status") != "CLAIMED"
                or not DIGEST.fullmatch(transaction_sha256)
                or not isinstance(batch, list)
                or not batch
                or sum(
                    isinstance(item, dict)
                    and item.get("ticket") not in existing_tickets
                    for item in batch
                ) > available
                or len({item.get("ticket") for item in batch if isinstance(item, dict)})
                != len(batch)
            ):
                raise ControllerError("qualification cohort claim is malformed")
            if pending_ack is not None and (
                pending_ack["transaction_sha256"] != transaction_sha256
            ):
                raise ControllerError("qualification cohort replay drifted")
            write(cohort_ack_path, {
                "schema": "nysa.software-factory.qualification-claim-ack/v1",
                "transaction_sha256": transaction_sha256,
            })
            for item in batch:
                if (
                    not isinstance(item, dict)
                    or item.get("action") != "START"
                    or not TICKET.fullmatch(item.get("ticket", ""))
                    or item["ticket"] not in self.qualification["tickets"]
                    or not DIGEST.fullmatch(item.get("lease_id", ""))
                    or not isinstance(item.get("branch"), str)
                    or not isinstance(item.get("worktree"), str)
                ):
                    raise ControllerError("qualification cohort claim is malformed")
                if item["ticket"] in existing_tickets:
                    prior = next(
                        claim for claim in claims if claim["ticket"] == item["ticket"]
                    )
                    if (
                        prior.get("lease") != item["lease_id"]
                        or prior.get("branch") != item["branch"]
                        or prior.get("worktree") != item["worktree"]
                    ):
                        raise ControllerError("qualification cohort replay drifted")
                    continue
                claim = {
                    "branch": item["branch"],
                    "lease": item["lease_id"],
                    "priority": item.get("priority", "none"),
                    "publication_lease": "",
                    "receipt": "",
                    "role": "",
                    "schema": CLAIM_SCHEMA,
                    "status": "claimed",
                    "ticket": item["ticket"],
                    "worktree": item["worktree"],
                }
                self.save_claim(claim)
                self.event(
                    "ticket_claimed", claim["ticket"], branch=claim["branch"],
                    claim_transaction_sha256=transaction_sha256,
                    preprovider_reset_head=item.get("preprovider_reset_head"),
                    worktree=claim["worktree"],
                )
                claims.append(claim)
                existing_tickets.add(claim["ticket"])
            acknowledged = self.json_call(
                "dispatch-plan", "--claim", "--cohort-ack",
                transaction_sha256, "--json",
            )
            if (
                acknowledged.get("action") != "ACK"
                or acknowledged.get("status") != "ACKNOWLEDGED"
                or acknowledged.get("transaction_sha256") != transaction_sha256
            ):
                raise ControllerError("qualification claim acknowledgement is malformed")
            cohort_ack_path.unlink()
            return claims
        while len(
            [
                item for item in claims
                if self.consumes_capacity(item)
                and item["ticket"] not in self.invalid_transition_tickets
            ]
        ) + reserved_capacity < self.capacity:
            arguments = ["dispatch-plan", "--claim"]
            for ticket in excluded:
                arguments.extend(["--exclude-ticket", ticket])
            value = self.json_call(*arguments, "--json")
            if "admission_refusal" in value:
                self.record_dispatch_refusal(value["admission_refusal"], claims)
            if value.get("action") == "WAIT":
                break
            if (
                value.get("action") != "START"
                or not TICKET.fullmatch(value.get("ticket", ""))
                or not DIGEST.fullmatch(value.get("lease_id", ""))
            ):
                raise ControllerError("dispatch claim is malformed")
            claim = {
                "branch": value["branch"],
                "lease": value["lease_id"],
                "priority": value.get("priority", "none"),
                "publication_lease": "",
                "receipt": "",
                "role": "",
                "schema": CLAIM_SCHEMA,
                "status": "claimed",
                "ticket": value["ticket"],
                "worktree": value["worktree"],
            }
            self.save_claim(claim)
            self.event(
                "ticket_claimed", claim["ticket"], branch=claim["branch"],
                preprovider_reset_head=value.get("preprovider_reset_head"),
                worktree=claim["worktree"],
            )
            claims.append(claim)
        return claims

    def product_ticket_done(self, ticket: str) -> bool:
        if self.qualification and ticket in self.qualification["tickets"]:
            protected = subprocess.run(
                [
                    "git", "-C", str(self.product), "show",
                    f"refs/remotes/origin/main:factory/tickets/{ticket}.md",
                ],
                text=True, capture_output=True, check=False, timeout=120,
            )
            if protected.returncode == 0:
                states = re.findall(
                    r"^State:\s*(.*?)\s*$", protected.stdout, re.I | re.M,
                )
                if len(states) == 1 and states[0].casefold() == "done":
                    self.qualification_protected_terminal(ticket)
                    return True
                return False
        try:
            text = (
                self.product / "factory" / "tickets" / f"{ticket}.md"
            ).read_text(encoding="utf-8")
        except (FileNotFoundError, OSError):
            return False
        states = re.findall(r"^State:\s*(.*?)\s*$", text, re.I | re.M)
        return len(states) == 1 and states[0].casefold() == "done"

    def protected_main_head(self) -> str:
        observed = run_external(
            [
                "git", "-C", str(self.product), "ls-remote", "--heads",
                "origin", "refs/heads/main",
            ],
            "protected main cancellation authority is unavailable",
        )
        fields = observed.stdout.split()
        if (
            len(fields) != 2
            or not SHA.fullmatch(fields[0])
            or fields[1] != "refs/heads/main"
        ):
            raise ControllerError("protected main cancellation authority is unavailable")
        with self.git_lock:
            run_external(
                [
                    "git", "-C", str(self.product), "fetch", "--quiet",
                    "--no-tags", "origin",
                    "+refs/heads/main:refs/remotes/origin/main",
                ],
                "protected main cancellation fetch failed",
            )
            fetched = subprocess.run(
                [
                    "git", "-C", str(self.product), "rev-parse", "--verify",
                    "refs/remotes/origin/main^{commit}",
                ],
                text=True, capture_output=True, check=True, timeout=120,
            ).stdout.strip()
        if fetched != fields[0]:
            raise ControllerError("protected main cancellation authority changed")
        return fetched

    def product_ticket_canceled(
        self, ticket: str, protected_main: str | None = None,
    ) -> bool:
        if self.qualification:
            return False
        protected_main = protected_main or self.protected_main_head()
        if not SHA.fullmatch(protected_main):
            raise ControllerError("protected main cancellation authority is invalid")
        observed = subprocess.run(
            [
                "git", "-C", str(self.product), "show",
                f"{protected_main}:factory/tickets/{ticket}.md",
            ],
            text=True, capture_output=True, check=False, timeout=120,
        )
        if observed.returncode != 0:
            return False
        states = re.findall(
            r"^State:\s*(.*?)\s*$", observed.stdout, re.I | re.M,
        )
        return len(states) == 1 and states[0].casefold() == "canceled"

    def cancellation_authority(
        self, claims: list[dict[str, Any]],
    ) -> str | None:
        if self.qualification or not claims:
            return None
        return self.protected_main_head()

    def retire_canceled_claims(
        self, claims: list[dict[str, Any]], protected_main: str | None = None,
    ) -> list[dict[str, Any]]:
        if claims and not self.qualification and protected_main is None:
            protected_main = self.cancellation_authority(claims)
        if protected_main is None:
            return claims
        retained = []
        for claim in claims:
            if (
                not self.product_ticket_canceled(claim["ticket"], protected_main)
                or self.role_active(claim)
            ):
                retained.append(claim)
                continue
            try:
                self.withdraw_publication(claim)
            except (
                ControllerError, json.JSONDecodeError, OSError,
                subprocess.SubprocessError, UnicodeError,
            ):
                self.event_once(
                    "canceled_ticket_retirement_waiting", claim["ticket"],
                    reason_code="publication_withdraw_refused",
                )
                retained.append(claim)
                continue
            if claim.get("lease_released") is not True:
                lease = claim.get("lease")
                if lease != "":
                    if not isinstance(lease, str) or not DIGEST.fullmatch(lease):
                        self.event_once(
                            "canceled_ticket_retirement_waiting",
                            claim["ticket"], reason_code="lease_invalid",
                        )
                        retained.append(claim)
                        continue
                    try:
                        self.release_ticket_lease(claim)
                    except (
                        ControllerError, json.JSONDecodeError, OSError,
                        subprocess.SubprocessError, UnicodeError,
                    ):
                        self.event_once(
                            "canceled_ticket_retirement_waiting",
                            claim["ticket"],
                            reason_code="lease_release_refused",
                        )
                        retained.append(claim)
                        continue
            self.event_once(
                "ticket_retired", claim["ticket"], reason="canceled",
            )
            self.claim_path(claim["ticket"]).unlink(missing_ok=True)
        return retained

    def recover_missing_passport_claims(
        self, claims: list[dict[str, Any]]
    ) -> None:
        if not self.qualification:
            return
        targets = [
            ticket for ticket in self.qualification["tickets"]
            if not self.product_ticket_done(ticket)
        ]
        if not targets:
            return
        passports = self.state / "passports"
        if not passports.is_dir():
            return
        claimed = {item["ticket"] for item in claims}
        records = self.worktrees_by_branch()
        for ticket in targets:
            path = passports / f"{ticket}.json"
            if (
                ticket in claimed or not path.exists() or self.active_run(ticket)
                or self.pause_path(ticket).exists()
            ):
                continue
            passport = read(path)
            branch = f"ticket/{ticket}"
            worktrees = records.get(f"refs/heads/{branch}", [])
            if (
                passport.get("ticket") != ticket
                or passport.get("branch") != branch
                or passport.get("current_state") not in INFLIGHT_STATES
                or passport.get("current_state") == "Blocked-Escalated"
                or len(worktrees) != 1
                or not Path(worktrees[0]).is_absolute()
            ):
                continue
            claim = {
                "branch": branch,
                "lease": "",
                "priority": "normal",
                "publication_lease": "",
                "receipt": "",
                "role": "",
                "schema": CLAIM_SCHEMA,
                "status": "claimed",
                "ticket": ticket,
                "worktree": worktrees[0],
            }
            if (
                Path(worktrees[0]).name == ticket
                and Path(worktrees[0]).parent.name == "parked"
            ):
                claim["parked"] = True
            if not self.ticket_release_current(claim):
                continue
            try:
                lease = self.json_call("claim", "--ticket", ticket)
                if (
                    lease.get("schema_version") != 1
                    or lease.get("ticket") != ticket
                    or not DIGEST.fullmatch(lease.get("lease_id", ""))
                ):
                    raise ControllerError("recovered ticket lease is invalid")
                claim["lease"] = lease["lease_id"]
                self.migrate_passport(claim, "preserve")
            except ControllerError as error:
                if claim["lease"]:
                    self.json_call(
                        "release", "--ticket", ticket,
                        "--lease", claim["lease"],
                    )
                self.event(
                    "missing_claim_blocked", ticket, error=str(error),
                )
                continue
            self.save_claim(claim)
            claims.append(claim)
            claimed.add(ticket)
            self.event("missing_claim_recovered", ticket)

    def settled_contract_blocker(
        self, claim: dict[str, Any]
    ) -> dict[str, str] | None:
        receipt = claim.get("receipt", "")
        role = claim.get("role", "")
        if (
            claim.get("status") != "blocked"
            or not DIGEST.fullmatch(receipt)
            or role not in {"planner", "spec-linter", "test-author", "builder"}
            or claim.get("publication_lease")
            or self.role_active(claim)
        ):
            return None
        terminal = self.terminal_for_receipt(claim["ticket"], receipt)
        if (
            terminal is None
            or terminal.get("role") != role
            or terminal.get("role_exit") != "role_exit_contract_blocked"
            or terminal.get("exit_status") != "12"
            or terminal.get("task_submitted") != "1"
        ):
            return None
        return terminal

    def pause_ticket(self, ticket: str, blocking_issue: str) -> dict[str, Any]:
        if not TICKET.fullmatch(ticket) or not FACTORY_ISSUE.fullmatch(blocking_issue):
            raise ControllerError("ticket pause requires a Software Factory issue URL")
        path = self.state / "passports" / f"{ticket}.json"
        if not path.exists() or self.active_run(ticket):
            raise ControllerError("ticket pause requires an idle passport")
        claims = {item["ticket"]: item for item in self.load_claims()}
        claim = claims.get(ticket)
        settled_blocker = self.settled_contract_blocker(claim) if claim else None
        missing_terminal = bool(
            claim
            and claim.get("status") == "blocked"
            and claim.get("blocked_reason") == "missing-terminal"
            and DIGEST.fullmatch(claim.get("receipt", ""))
            and claim.get("role") in {
                "planner", "spec-linter", "test-author", "builder",
                "reviewer", "narrator",
            }
            and not claim.get("publication_lease")
            and not self.role_active(claim)
            and self.terminal_for_receipt(
                claim["ticket"], claim["receipt"]
            ) is None
        )
        if missing_terminal:
            missing_terminal = (
                claim.get("lease") == ""
                and claim["ticket"] not in self.dispatcher_lease_records()
            )
        if (
            claim
            and (claim.get("receipt") or claim.get("role"))
            and settled_blocker is None
            and not missing_terminal
        ):
            raise ControllerError("ticket pause requires a pre-provider boundary")
        passport = read(path)
        current_state = passport.get("current_state")
        if (
            current_state not in INFLIGHT_STATES
            or passport.get("publication_state") == "merged"
            or self.product_ticket_done(ticket)
        ):
            raise ControllerError("ticket pause requires an in-flight passport")
        existing_intent = (
            read(self.pause_path(ticket)) if self.pause_path(ticket).exists() else None
        )
        branch = passport.get("branch", "")
        worktrees = self.worktrees_by_branch().get(f"refs/heads/{branch}", [])
        if (
            passport.get("ticket") != ticket
            or branch != f"ticket/{ticket}"
            or not SHA.fullmatch(passport.get("head_sha", ""))
            or not DIGEST.fullmatch(passport.get("passport_sha256", ""))
            or len(worktrees) != 1
        ):
            raise ControllerError("ticket pause passport is ambiguous")
        if claim:
            worktree = claim["worktree"]
            status = claim["status"]
            budget = claim.get("budget_sha256", "")
        else:
            worktree = (
                existing_intent.get("worktree") if existing_intent else worktrees[0]
            )
            status = (
                existing_intent.get("status") if existing_intent else
                "blocked" if current_state == "Blocked-Escalated"
                else "claimed"
            )
            budget = existing_intent.get("budget_sha256") if existing_intent else ""
        probe = {
            "branch": branch, "ticket": ticket, "worktree": worktree,
        }
        if not self.remote_passport_valid(probe):
            if settled_blocker is None:
                raise ControllerError("ticket pause passport is not portable")
            self.migrate_passport(claim, "preserve")
            passport = read(path)
            current_state = passport.get("current_state")
            branch = passport.get("branch", "")
            worktrees = self.worktrees_by_branch().get(
                f"refs/heads/{branch}", []
            )
            if (
                current_state not in INFLIGHT_STATES
                or passport.get("publication_state") == "merged"
                or self.product_ticket_done(ticket)
                or passport.get("ticket") != ticket
                or branch != f"ticket/{ticket}"
                or not SHA.fullmatch(passport.get("head_sha", ""))
                or not DIGEST.fullmatch(passport.get("passport_sha256", ""))
                or len(worktrees) != 1
            ):
                raise ControllerError("ticket pause passport is not portable")
            worktree = claim["worktree"]
            probe = {
                "branch": branch, "ticket": ticket, "worktree": worktree,
            }
            if not self.remote_passport_valid(probe):
                raise ControllerError("ticket pause passport is not portable")
        if claim:
            if claim.get("publication_lease"):
                self.withdraw_publication(claim)
            if not self.park_claim(claim):
                raise ControllerError("ticket pause could not park a clean checkpoint")
            if DIGEST.fullmatch(claim.get("lease", "")):
                self.release_ticket_lease(claim)
            worktree = claim["worktree"]
        value = {
            "blocking_issue": blocking_issue,
            "branch": branch,
            "budget_sha256": budget or None,
            "created_at_epoch": (
                existing_intent.get("created_at_epoch")
                if existing_intent else int(time.time())
            ),
            "current_stage": passport.get("current_stage"),
            "current_state": current_state,
            "factory_sha": self.release_path.name,
            "head_sha": passport["head_sha"],
            "passport_sha256": passport["passport_sha256"],
            "passport_factory_sha": passport.get("factory_sha"),
            "resume_state": self.resume_state(worktree, ticket, current_state),
            "run_snapshot_sha256": self.ticket_run_snapshot(ticket),
            "schema": "nysa.software-factory.ticket-pause/v2",
            "status": status,
            "ticket": ticket,
            "worktree": worktree,
        }
        value["pause_sha256"] = hashlib.sha256(
            canonical(value).encode()
        ).hexdigest()
        destination = self.pause_path(ticket)
        if destination.exists():
            if read(destination) != value:
                raise ControllerError("ticket pause intent conflicts")
        else:
            write(destination, value)
        self.claim_path(ticket).unlink(missing_ok=True)
        self.event(
            "ticket_paused", ticket, blocking_issue=blocking_issue,
            head_sha=passport["head_sha"], pause_sha256=value["pause_sha256"],
        )
        return {"schema": SCHEMA, "status": "paused", "ticket": ticket}

    def resume_ticket(self, ticket: str, factory_sha: str) -> dict[str, Any]:
        path = self.pause_path(ticket)
        if (
            not TICKET.fullmatch(ticket)
            or not SHA.fullmatch(factory_sha)
            or factory_sha != self.release_path.name
            or not path.exists()
        ):
            raise ControllerError("ticket resume intent is unavailable")
        intent = read(path)
        intent_digest = intent.get("pause_sha256", "")
        signed_intent = dict(intent)
        signed_intent.pop("pause_sha256", None)
        legacy = intent.get("schema") == "nysa.software-factory.ticket-pause/v1"
        if not legacy and intent_digest != hashlib.sha256(
            canonical(signed_intent).encode()
        ).hexdigest():
            raise ControllerError("ticket pause intent digest is invalid")
        passport_path = self.state / "passports" / f"{ticket}.json"
        if not passport_path.exists() or self.active_run(ticket):
            raise ControllerError("ticket resume requires an idle passport")
        passport = read(passport_path)
        current_state = passport.get("current_state")
        if (
            current_state not in INFLIGHT_STATES
            or passport.get("publication_state") == "merged"
            or self.product_ticket_done(ticket)
        ):
            raise ControllerError("ticket resume requires an in-flight passport")
        lineage = passport.get("migration_history", [])
        authorized_passports = {passport.get("passport_sha256")}
        authorized_passports.update(
            item.get("from_passport_sha256")
            for item in lineage if isinstance(item, dict)
        )
        branch = intent.get("branch", "")
        worktrees = self.worktrees_by_branch().get(f"refs/heads/{branch}", [])
        probe = {
            "branch": branch, "ticket": ticket,
            "worktree": intent.get("worktree", ""),
        }
        remote_status, local_head, remote_head = (
            self.remote_cell_head_status(probe)
            if (
                not legacy
                and len(worktrees) == 1
                and worktrees[0] == probe["worktree"]
            )
            else ("remote_unavailable", "", "")
        )
        route_migration = (
            not legacy
            and SHA.fullmatch(intent.get("head_sha", "")) is not None
            and SHA.fullmatch(local_head) is not None
            and local_head != intent.get("head_sha")
            and remote_status == "pushed"
            and remote_head == local_head
            and self.ticket_release_current(probe)
            and self.exact_route_migration_commit(
                probe, intent["head_sha"], local_head,
            )
        )
        passport_position_matches = (
            (
                passport.get("head_sha") == intent.get("head_sha")
                and passport.get("factory_sha") in {
                    factory_sha, intent.get("passport_factory_sha"),
                }
            )
            or (
                route_migration
                and passport.get("head_sha") == local_head
                and passport.get("factory_sha") == factory_sha
            )
        )
        if (
            intent.get("schema") not in {
                "nysa.software-factory.ticket-pause/v1",
                "nysa.software-factory.ticket-pause/v2",
            }
            or intent.get("ticket") != ticket
            or intent.get("branch") != f"ticket/{ticket}"
            or intent.get("current_state") != current_state
            or intent.get("passport_sha256") not in authorized_passports
            or passport.get("ticket") != ticket
            or passport.get("branch") != intent.get("branch")
            or not passport_position_matches
            or intent.get("worktree") not in worktrees
            or self.claim_path(ticket).exists()
            or (
                not legacy
                and (
                    not FACTORY_ISSUE.fullmatch(intent.get("blocking_issue", ""))
                    or intent.get("current_stage") != passport.get("current_stage")
                    or intent.get("resume_state") != self.resume_state(
                        intent["worktree"], ticket, current_state
                    )
                    or not DIGEST.fullmatch(intent.get("run_snapshot_sha256", ""))
                    or intent.get("run_snapshot_sha256")
                    != self.ticket_run_snapshot(ticket)
                    or not DIGEST.fullmatch(intent_digest)
                )
            )
        ):
            raise ControllerError("ticket resume intent does not match the passport")
        if route_migration and passport.get("head_sha") != local_head:
            migrated = self.migrate_passport(
                probe, "preserve", expected_head=local_head,
            )
            passport = read(passport_path)
            if (
                migrated.get("status") != "ok"
                or migrated.get("passport") != passport.get("passport_sha256")
            ):
                raise ControllerError("ticket resume passport migration failed")
        lineage = passport.get("migration_history", [])
        authorized_passports = {passport.get("passport_sha256")}
        authorized_passports.update(
            item.get("from_passport_sha256")
            for item in lineage if isinstance(item, dict)
        )
        current_state = passport.get("current_state")
        expected_head = local_head if route_migration else intent.get("head_sha")
        if (
            current_state not in INFLIGHT_STATES
            or passport.get("publication_state") == "merged"
            or self.product_ticket_done(ticket)
            or passport.get("ticket") != ticket
            or passport.get("branch") != branch
            or passport.get("factory_sha") != factory_sha
            or passport.get("head_sha") != expected_head
            or not passport_head_lineage(passport, intent.get("head_sha", ""))
            or intent.get("passport_sha256") not in authorized_passports
            or intent.get("current_state") != current_state
            or passport.get("current_stage") != intent.get("current_stage")
            or self.claim_path(ticket).exists()
            or (
                not legacy
                and (
                    intent.get("resume_state") != self.resume_state(
                        intent["worktree"], ticket, current_state
                    )
                    or intent.get("run_snapshot_sha256")
                    != self.ticket_run_snapshot(ticket)
                )
            )
        ):
            raise ControllerError("ticket resume intent does not match the passport")
        archived: Path | None = None
        if not legacy:
            repros = self.state / "repros"
            safe_directory(repros, create=True)
            archived = repros / f"{ticket}-{intent_digest}.json"
            if archived.exists() and read(archived) != intent:
                raise ControllerError("ticket repro record conflicts")
        claim = {
            "branch": intent["branch"],
            "lease": "",
            "priority": "normal",
            "publication_lease": "",
            "receipt": "",
            "role": "",
            "schema": CLAIM_SCHEMA,
            "status": intent.get("status", "claimed"),
            "ticket": ticket,
            "worktree": intent["worktree"],
        }
        if Path(claim["worktree"]).parent.name == "parked":
            claim["parked"] = True
        if claim["status"] == "budget" and DIGEST.fullmatch(
            intent.get("budget_sha256") or ""
        ):
            claim["budget_sha256"] = intent["budget_sha256"]
        elif claim["status"] not in {"blocked", "claimed", "waiting"}:
            claim["status"] = "claimed"
        if not self.remote_passport_valid(claim):
            raise ControllerError("ticket resume passport is not portable")
        self.ensure_lease(claim, "paused-ticket-resume")
        if not self.ticket_release_current(claim):
            claim["status"] = "blocked"
        elif (
            claim["status"] == "blocked"
            and current_state != "Blocked-Escalated"
        ):
            claim["status"] = "claimed"
        self.save_claim(claim)
        if legacy:
            path.unlink()
        else:
            if archived is None:
                raise ControllerError("ticket repro archive is unavailable")
            if archived.exists():
                path.unlink()
            else:
                os.replace(path, archived)
        self.event(
            "ticket_resumed", ticket,
            blocking_issue=intent.get("blocking_issue"),
            head_sha=passport["head_sha"],
            source_factory_sha=intent.get("factory_sha"),
            target_factory_sha=factory_sha,
        )
        return {"schema": SCHEMA, "status": "resumed", "ticket": ticket}

    def renew(self, claim: dict[str, Any]) -> None:
        self.json_call(
            "renew", "--ticket", claim["ticket"], "--lease", claim["lease"],
        )

    @staticmethod
    def route_path(claim: dict[str, Any]) -> Path:
        return Path(claim["worktree"]) / f"factory/route-plans/{claim['ticket']}.json"

    def pin_routes(self, claims: list[dict[str, Any]]) -> list[dict[str, str]]:
        missing = [claim for claim in claims if not self.route_path(claim).exists()]
        if not missing:
            return []
        arguments = ["models", "pin-batch"]
        for claim in missing:
            arguments.extend([
                "--ticket", claim["ticket"], "--workdir", claim["worktree"],
            ])
        pin = self.json_call(*arguments, "--json", allow=(0, 2), timeout=None)
        if pin.get("status") == "error":
            failure = self.model_resolution_failure(
                pin, "model pin resolution failed",
            )
            if failure["reason_code"] != "profile_temporarily_unavailable":
                error = ControllerError(canonical({
                    **failure, "ticket": missing[0]["ticket"],
                }))
                self.record_admission_failure(error, claims)
                raise error
            results = []
            for index, claim in enumerate(missing):
                claim["status"] = "waiting"
                self.save_claim(claim)
                self.event(
                    "model_pin_wait", claim["ticket"], shared=index != 0,
                )
                results.append({"status": "waiting", "ticket": claim["ticket"]})
            return results
        pins = pin.get("pins")
        if (
            pin.get("schema") != "model-pin-batch/v1"
            or pin.get("status") != "ok"
            or not isinstance(pins, list)
            or len(pins) != len(missing)
            or any(not self.route_path(claim).exists() for claim in missing)
        ):
            raise ControllerError("batch model pin returned malformed evidence")
        self.event("model_pin_batch", tickets=[claim["ticket"] for claim in missing])
        return []

    def qualification_provider_pristine(self) -> None:
        if not self.qualification:
            raise ControllerError("qualification prime requires qualification mode")
        database = os.environ.get("FACTORY_PROVIDER_DB", "")
        if not database or not Path(database).is_absolute():
            raise ControllerError("qualification provider state is unavailable")
        result = subprocess.run(
            [
                sys.executable, "-I", "-S",
                str(self.release_path / "scripts/provider-coordinator.py"),
                "--db", database, "status",
            ],
            text=True, capture_output=True, check=False, timeout=120,
        )
        try:
            status = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise ControllerError(
                "qualification provider state is malformed"
            ) from error
        if (
            result.returncode
            or not isinstance(status, dict)
            or status.get("schema") != "factory-provider-coordinator/v1"
            or status.get("attempts") != []
            or status.get("counts") != {}
            or status.get("legacy_intervals") != []
            or status.get("active_reserve_micro_usd") != 0
        ):
            raise ControllerError("qualification provider state is not pristine")

    def prime_planner_transition(self, claim: dict[str, Any]) -> None:
        if (
            claim.get("status") != "claimed"
            or claim.get("receipt")
            or claim.get("role")
            or claim.get("publication_lease")
            or claim.get("lease_released") is True
            or self.role_active(claim)
            or self.route_path(claim).is_symlink()
            or not self.route_path(claim).is_file()
        ):
            raise ControllerError("qualification Planner prime state is invalid")
        path = self.state / f"{claim['ticket']}.json"
        transition = self.transition_receipt(claim, record=False)
        if (path.exists() or path.is_symlink()) and transition is None:
            raise ControllerError("qualification Planner receipt is invalid")
        if transition is None:
            result = self.json_call(
                "state-machine", "--ticket", claim["ticket"],
                "--lease", claim["lease"], "--workdir", claim["worktree"],
                "--json", timeout=None,
            )
            if (
                not valid_transition_evidence(result, claim["ticket"])
                or result.get("stage") != "RUN planner"
                or result.get("role") != "planner"
            ):
                raise ControllerError(
                    "qualification Planner transition is invalid"
                )
            transition = self.transition_receipt(claim, record=False)
        head = self.cell_git(claim, "rev-parse", "HEAD").stdout.strip()
        if (
            transition is None
            or transition.get("stage") != "RUN planner"
            or transition.get("role") != "planner"
            or transition.get("consumed") is not False
            or transition.get("head_sha") != head
            or transition.get("lease_sha256")
            != hashlib.sha256(claim["lease"].encode()).hexdigest()
        ):
            raise ControllerError("qualification Planner receipt is invalid")

    def qualification_prime_state_shape(self) -> None:
        if not self.qualification:
            raise ControllerError("qualification prime requires qualification mode")
        selected = set(self.qualification["tickets"])
        allowed = {
            ".lock", ".operator-apply-lock", ".operator-lock", ".passport-key.lock",
            "claims", "events", "logs",
            "operator-receipts", "passports", "passport.key", "reconcile.lock",
            f"qualification-restart-boundary-{self.release_path.name}.json",
            *(f"{ticket}.json" for ticket in selected),
        }
        bundle = os.environ.get("FACTORY_QUALIFICATION_MODEL_BUNDLE_SHA256", "")
        if DIGEST.fullmatch(bundle):
            allowed.add(f"model-bundle-consumed-{bundle}")
        if any(path.name not in allowed for path in self.state.iterdir()):
            raise ControllerError("qualification prime has execution residue")
        for name in (
            ".lock", ".operator-apply-lock", ".operator-lock", ".passport-key.lock",
            "passport.key", "reconcile.lock",
        ):
            path = self.state / name
            if path.exists() or path.is_symlink():
                info = path.lstat()
                if (
                    path.is_symlink() or not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.geteuid() or info.st_nlink != 1
                    or stat.S_IMODE(info.st_mode) != 0o600
                ):
                    raise ControllerError("qualification prime has execution residue")
        consumed = self.state / f"model-bundle-consumed-{bundle}"
        if bundle and (consumed.exists() or consumed.is_symlink()):
            descriptor = os.open(
                consumed, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            )
            try:
                info = os.fstat(descriptor)
                raw = os.read(descriptor, 66)
            finally:
                os.close(descriptor)
            if (
                not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600
                or raw != f"{bundle}\n".encode()
            ):
                raise ControllerError("qualification prime has execution residue")
        claims = safe_directory(self.state / "claims")
        if {path.name for path in claims.iterdir()} != {
            f"{ticket}.json" for ticket in selected
        }:
            raise ControllerError("qualification prime has execution residue")
        logs = safe_directory(self.state / "logs")
        if any(logs.iterdir()):
            raise ControllerError("qualification prime has execution residue")
        passports = self.state / "passports"
        if passports.exists() or passports.is_symlink():
            if any(safe_directory(passports).iterdir()):
                raise ControllerError("qualification prime has execution residue")

    def prime_qualification(self, claims: list[dict[str, Any]]) -> None:
        if not self.qualification:
            raise ControllerError("qualification prime requires qualification mode")
        selected = sorted(self.qualification["tickets"])
        if (
            sorted(claim["ticket"] for claim in claims) != selected
            or self.active_run_tickets()
        ):
            raise ControllerError("qualification prime has execution residue")
        self.qualification_prime_state_shape()
        self.qualification_provider_pristine()
        safe_directory(self.state / "passports", create=True)
        pin_results = self.pin_routes(claims)
        if pin_results:
            raise ControllerError("qualification Planner routes are not ready")
        for claim in claims:
            if claim.get("status") == "waiting" and not claim.get("blocked_reason"):
                claim["status"] = "claimed"
                self.save_claim(claim)
        with ThreadPoolExecutor(max_workers=min(4, len(claims))) as executor:
            list(executor.map(self.prime_planner_transition, claims))
        self.qualification_provider_pristine()
        self.qualification_prime_state_shape()
        if self.active_run_tickets():
            raise ControllerError("qualification prime has execution residue")

    def release(self, claim: dict[str, Any]) -> None:
        self.withdraw_publication(claim)
        self.json_call(
            "release", "--ticket", claim["ticket"], "--lease", claim["lease"],
        )
        self.claim_path(claim["ticket"]).unlink(missing_ok=True)
        self.event("ticket_released", claim["ticket"])

    def release_publication(self, claim: dict[str, Any]) -> None:
        with self.publication_lock:
            lease_sha256 = hashlib.sha256(
                claim["publication_lease"].encode()
            ).hexdigest()
            try:
                self.json_call(
                    "publication", "release", "--ticket", claim["ticket"],
                    "--lease", claim["publication_lease"], "--json",
                )
            except ControllerError as error:
                recovered = self.json_call(
                    "publication", "withdraw", "--ticket", claim["ticket"],
                    "--json",
                )
                if recovered.get("status") != "absent":
                    raise error
            self.event_once(
                "publication_released", claim["ticket"],
                publication_lease_sha256=lease_sha256,
            )
            claim["publication_lease"] = ""
            self.save_claim(claim)

    def withdraw_publication(self, claim: dict[str, Any]) -> None:
        if claim.get("publication_lease"):
            self.release_publication(claim)
            return
        value = self.json_call(
            "publication", "withdraw", "--ticket", claim["ticket"], "--json",
        )
        if value.get("status") not in {"absent", "withdrawn"}:
            raise ControllerError("publication withdrawal returned invalid evidence")
        if value["status"] == "withdrawn":
            self.event("publication_withdrawn", claim["ticket"])

    def block(self, claim: dict[str, Any], reason: str) -> None:
        self.withdraw_publication(claim)
        self.release_ticket_lease(claim)
        claim["status"] = "blocked"
        claim["blocked_reason"] = reason
        self.save_claim(claim)
        self.event("ticket_blocked", claim["ticket"], reason=reason)

    def active_run(self, ticket: str) -> bool:
        active = self.product / "factory/.active-runs"
        return active.is_dir() and any(active.glob(f"{ticket}.*"))

    def role_active(self, claim: dict[str, Any]) -> bool:
        return self.active_run(claim["ticket"])

    @staticmethod
    def live_role_wait(claim: dict[str, Any]) -> dict[str, str]:
        return {
            "role": claim["role"],
            "status": "waiting",
            "ticket": claim["ticket"],
            "transition_receipt_sha256": claim["receipt"],
            "wait_reason": "live-role",
        }

    def release_ticket_lease(self, claim: dict[str, Any]) -> None:
        if claim.get("lease_released") is True:
            return
        self.json_call(
            "release", "--ticket", claim["ticket"], "--lease", claim["lease"],
        )
        claim["lease_released"] = True
        self.save_claim(claim)

    def ensure_lease(self, claim: dict[str, Any], label: str) -> None:
        if (
            claim.get("lease_released") is not True
            and DIGEST.fullmatch(claim.get("lease", ""))
        ):
            try:
                self.renew(claim)
                return
            except ControllerError:
                if self.role_active(claim):
                    raise
                self.release_expired_successor_lease(claim)
        lease = self.json_call("claim", "--ticket", claim["ticket"])
        if (
            lease.get("schema_version") != 1
            or lease.get("ticket") != claim["ticket"]
            or not DIGEST.fullmatch(lease.get("lease_id", ""))
        ):
            raise ControllerError(f"{label} lease is invalid")
        claim["lease"] = lease["lease_id"]
        claim.pop("lease_released", None)
        self.save_claim(claim)
        self.event(
            "ticket_lease_recovered", claim["ticket"], recovery=label,
        )

    def release_inactive_ticket_leases(
        self, claims: list[dict[str, Any]],
    ) -> None:
        for claim in claims:
            if (
                claim.get("status") in {"blocked", "budget", "waiting"}
                and DIGEST.fullmatch(claim.get("lease", ""))
                and claim.get("lease_released") is not True
                and not self.role_active(claim)
                and not self.semantic_handoff_pending(claim)
                and not self.bundle_refresh_handoff_pending(claim)
            ):
                status = claim["status"]
                try:
                    self.release_ticket_lease(claim)
                except (
                    ControllerError, json.JSONDecodeError, OSError,
                    subprocess.SubprocessError, UnicodeError,
                ):
                    self.event_once(
                        "inactive_ticket_lease_release_waiting", claim["ticket"],
                        status=status,
                    )
                else:
                    self.event_once(
                        "inactive_ticket_lease_released", claim["ticket"],
                        status=status,
                    )

    def maintain_successor_leases(self, claims: list[dict[str, Any]]) -> None:
        if not self.qualification or self.qualification.get("mode") != "successor":
            return
        for claim in claims:
            if self.role_active(claim):
                continue
            if (
                self.parked(claim)
                and claim.get("status") in {"claimed", "running", "waiting"}
                and DIGEST.fullmatch(claim.get("lease", ""))
            ):
                self.park_claim(claim)
            elif self.consumes_capacity(claim):
                self.ensure_lease(claim, "successor-cohort")

    def semantic_handoff_state(self, claim: dict[str, Any]) -> str:
        if (
            claim.get("status") != "blocked"
            or claim.get("ticket") != "T-198"
            or claim.get("receipt") != T198_RECEIPT
            or claim.get("role") != "spec-linter"
            or claim.get("blocked_reason") != "role-failure"
            or not DIGEST.fullmatch(claim.get("receipt", ""))
            or not DIGEST.fullmatch(claim.get("lease", ""))
        ):
            return "invalid"
        try:
            transition = self.operator_transition(claim)
            passport = self.authenticated_operator_passport(claim["ticket"])
            terminal = self.terminal_for_receipt(
                claim["ticket"], claim["receipt"],
            )
            passport_path = (
                self.state / "passports" / f"{claim['ticket']}.json"
            )
            local_exact = bool(
                transition is not None
                and passport is not None
                and terminal is not None
                and transition.get("factory_sha") == self.release_path.name
                and transition.get("stage") == "RUN spec-linter"
                and transition.get("role") == "spec-linter"
                and transition.get("loop") == {
                    "attempt": 2, "capped": False,
                    "kind": "planner-spec-linter", "limit": 3,
                }
                and transition.get("parent_digest") == claim["receipt"]
                and transition.get("consumed") is False
                and transition.get("head_sha") == passport.get("head_sha")
                and transition.get("route_plan_sha256")
                == passport.get("route_plan_sha256")
                and transition.get("passport_sha256")
                == hashlib.sha256(passport_path.read_bytes()).hexdigest()
                and transition.get("lease_sha256")
                == hashlib.sha256(claim["lease"].encode()).hexdigest()
            )
            if not local_exact:
                return "invalid"
            try:
                locally_valid = self.locally_valid_operator_passport(claim)
            except (ControllerError, subprocess.SubprocessError):
                return "transient"
            if (
                locally_valid is None
                or not self.exact_semantic_authorization_recovery(
                    claim, terminal, validate_remote=False,
                )
            ):
                return "invalid"
            try:
                status, local, remote = self.remote_cell_head_status(claim)
            except subprocess.SubprocessError:
                return "transient"
            if status == "remote_unavailable":
                return "transient"
            return (
                "ready" if status == "pushed"
                and local == remote == passport.get("head_sha") else "invalid"
            )
        except (ControllerError, OSError, json.JSONDecodeError, UnicodeError):
            return "invalid"

    def semantic_handoff_pending(self, claim: dict[str, Any]) -> bool:
        return self.semantic_handoff_state(claim) != "invalid"

    def release_expired_successor_lease(self, claim: dict[str, Any]) -> bool:
        lease_id = claim.get("lease", "")
        if (
            self.qualification is None
            or self.qualification.get("mode") != "successor"
            or not (
                self.parked(claim) and lease_id == ""
                or DIGEST.fullmatch(lease_id)
            )
            or claim.get("publication_lease")
            or self.role_active(claim)
            or not self.ticket_release_current(claim)
        ):
            return False
        try:
            if not self.remote_passport_valid(claim):
                return False
        except ControllerError:
            return False
        passport = read(
            self.state / "passports" / f"{claim['ticket']}.json"
        )
        if (
            passport.get("ticket") != claim["ticket"]
            or passport.get("branch") != claim["branch"]
            or passport.get("factory_sha") != self.release_path.name
        ):
            raise ControllerError("expired lease recovery passport is not exact")
        records = self.dispatcher_lease_records()
        record = records.get(claim["ticket"])
        if record is None or record["expires_epoch"] > int(time.time()):
            return False
        if lease_id and record["lease_id"] != lease_id:
            raise ControllerError("expired dispatcher lease identity changed")
        result = self.json_call(
            "release-expired", "--ticket", claim["ticket"],
            "--lease", record["lease_id"],
        )
        if result != {
            "expired": True, "released": True, "ticket": claim["ticket"],
        }:
            raise ControllerError("expired dispatcher lease release is invalid")
        self.event_once(
            "expired_ticket_lease_released", claim["ticket"],
            expired_lease_sha256=hashlib.sha256(
                record["lease_id"].encode()
            ).hexdigest(),
        )
        if lease_id:
            claim["lease"] = ""
            claim.pop("lease_released", None)
            self.save_claim(claim)
        return True

    def park_claim(self, claim: dict[str, Any]) -> bool:
        """Release a clean checkpointed ticket from a disposable cell."""
        if self.role_active(claim):
            return False
        if self.parked(claim):
            if claim["status"] in {"claimed", "running"}:
                claim["status"] = "waiting"
            if DIGEST.fullmatch(claim.get("lease", "")):
                try:
                    self.release_ticket_lease(claim)
                except ControllerError:
                    if not self.release_expired_successor_lease(claim):
                        raise
                claim["lease"] = ""
                claim.pop("lease_released", None)
                self.save_claim(claim)
                self.event("parked_lease_released", claim["ticket"])
            else:
                self.save_claim(claim)
            return True
        source = Path(claim["worktree"])
        if (
            not re.fullmatch(r"cell-[1-6]", source.name)
            or not source.is_dir()
            or self.role_active(claim)
        ):
            return False
        try:
            clean = subprocess.run(
                ["git", "-C", str(source), "status", "--porcelain=v1", "-z"],
                text=True, capture_output=True, check=True, timeout=120,
            ).stdout == ""
            branch = subprocess.run(
                ["git", "-C", str(source), "symbolic-ref", "--short", "HEAD"],
                text=True, capture_output=True, check=True, timeout=120,
            ).stdout.strip()
            if (
                not clean
                or branch != claim["branch"]
                or not self.remote_passport_valid(claim)
            ):
                self.event(
                    "cell_parking_deferred", claim["ticket"],
                    reason="checkpoint_not_portable",
                )
                return False
            root = safe_directory(source.parent)
            parked_root = self.state / "parked" if self.qualification else root / "parked"
            safe_directory(parked_root, create=True)
            destination = parked_root / claim["ticket"]
            if destination.exists() or destination.is_symlink():
                raise ControllerError("parked ticket destination is occupied")
            with self.git_lock:
                subprocess.run(
                    [
                        "git", "-C", str(self.product), "worktree", "move",
                        str(source), str(destination),
                    ],
                    check=True, timeout=120,
                )
            claim.update(parked=True, worktree=str(destination))
            if claim["status"] in {"claimed", "running"}:
                claim["status"] = "waiting"
            self.save_claim(claim)
            if DIGEST.fullmatch(claim.get("lease", "")):
                self.release_ticket_lease(claim)
                claim["lease"] = ""
                claim.pop("lease_released", None)
                self.save_claim(claim)
            self.event(
                "cell_parked", claim["ticket"], from_worktree=str(source),
                parked_worktree=str(destination),
            )
            return True
        except (
            ControllerError,
            OSError,
            subprocess.SubprocessError,
        ) as error:
            self.event(
                "cell_parking_deferred", claim["ticket"], reason=str(error),
            )
            return False

    def ensure_execution_cell(self, claim: dict[str, Any]) -> None:
        """Move a portable parked ticket into a free execution cell."""
        if not self.parked(claim):
            return
        source = Path(claim["worktree"])
        root = (
            self.state / "cells" if self.qualification else source.parent.parent
        )
        root = safe_directory(root, create=bool(self.qualification))
        if (
            source.parent != (
                self.state / "parked" if self.qualification else root / "parked"
            )
            or source.name != claim["ticket"]
            or not source.is_dir()
            or not self.remote_passport_valid(claim)
        ):
            raise ControllerError("parked ticket checkpoint is not portable")
        with self.git_lock:
            output = subprocess.run(
                [
                    "git", "-C", str(self.product), "worktree", "list",
                    "--porcelain",
                ],
                text=True, capture_output=True, check=True, timeout=120,
            ).stdout
            occupied = {
                Path(line.partition(" ")[2]).resolve()
                for line in output.splitlines()
                if line.startswith("worktree ")
            }
            destination = next(
                (
                    root / f"cell-{number}"
                    for number in range(1, 7)
                    if root / f"cell-{number}" not in occupied
                    and not (root / f"cell-{number}").exists()
                    and not (root / f"cell-{number}").is_symlink()
                ),
                None,
            )
            if destination is None:
                raise ControllerError("no disposable execution cell is available")
            subprocess.run(
                [
                    "git", "-C", str(self.product), "worktree", "move",
                    str(source), str(destination),
                ],
                check=True, timeout=120,
            )
        claim.pop("parked", None)
        claim["worktree"] = str(destination)
        self.save_claim(claim)
        try:
            if not self.remote_passport_valid(claim):
                raise ControllerError(
                    "reattached ticket passport validation failed"
                )
        except (
            ControllerError,
            json.JSONDecodeError,
            OSError,
            subprocess.SubprocessError,
        ):
            with self.git_lock:
                subprocess.run(
                    [
                        "git", "-C", str(self.product), "worktree", "move",
                        str(destination), str(source),
                    ],
                    check=True, timeout=120,
                )
            claim.update(parked=True, worktree=str(source))
            self.save_claim(claim)
            raise
        self.event(
            "cell_reattached", claim["ticket"], from_worktree=str(source),
            to_worktree=str(destination),
        )

    def terminal_for_receipt(self, ticket: str, receipt: str) -> dict[str, str] | None:
        matches = []
        for path in (self.product / "factory/runs").glob("*.meta"):
            value = fields(path)
            if (
                value.get("ticket") == ticket
                and value.get("transition_receipt_sha256") == receipt
                and value.get("accounting_state") in TERMINAL_ACCOUNTING
            ):
                matches.append(value)
        if len(matches) > 1:
            launch_voids = all(self.typed_launch_void(item) for item in matches)
            identity = {
                (
                    item.get("role"), item.get("role_head_before"),
                    item.get("kit_sha"), item.get("terminal_reason_code", ""),
                )
                for item in matches
            }
            if not launch_voids or len(identity) != 1:
                raise ControllerError("receipt has ambiguous terminal run evidence")
            selected = dict(max(
                matches,
                key=lambda item: (
                    item.get("terminal_at", ""), item.get("run_id", ""),
                ),
            ))
            selected["duplicate_launch_void_count"] = str(len(matches))
            return selected
        return matches[0] if matches else None

    def attempt_manifest(self, claim: dict[str, Any]) -> dict[str, str] | None:
        matches = []
        for path in (self.product / "factory/runs").glob("*.meta"):
            value = fields(path)
            if (
                value.get("ticket") == claim["ticket"]
                and value.get("transition_receipt_sha256")
                == claim.get("receipt")
                and value.get("accounting_state") not in TERMINAL_ACCOUNTING
            ):
                matches.append(value)
        if len(matches) > 1:
            raise ControllerError("receipt has ambiguous active run evidence")
        return matches[0] if matches else self.terminal_for_receipt(
            claim["ticket"], claim["receipt"]
        )

    def observe_attempt(self, claim: dict[str, Any]) -> None:
        attempt = self.attempt_manifest(claim)
        if attempt is None:
            return
        run_id = attempt.get("run_id", "")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", run_id):
            raise ControllerError("attempt run identity is invalid")
        bound = {
            "adapter": attempt.get("adapter"),
            "go_issued": attempt.get("go_issued"),
            "head_sha": attempt.get("role_head_before"),
            "kit_sha": attempt.get("kit_sha"),
            "provider_attempt_id": attempt.get("provider_attempt_id"),
            "role": claim.get("role"),
            "route_id": attempt.get("route_id"),
            "run_id": run_id,
            "task_submitted": attempt.get("task_submitted"),
            "transition_receipt_sha256": claim.get("receipt"),
        }
        marker = "attempt-bound-" + hashlib.sha256(run_id.encode()).hexdigest()
        if self.marker(marker, {"schema": EVENT_SCHEMA, **bound}):
            self.event("attempt_bound", claim["ticket"], **bound)
        journal = self.product / "factory/runs" / f"{run_id}.progress.jsonl"
        if not journal.exists():
            return
        try:
            summary = progress_summary(journal)
        except (ControllerError, OSError) as error:
            invalid = "attempt-progress-invalid-" + hashlib.sha256(
                run_id.encode()
            ).hexdigest()
            if self.marker(invalid, {
                "run_id": run_id, "schema": EVENT_SCHEMA,
            }):
                self.event(
                    "attempt_progress_invalid", claim["ticket"],
                    error=str(error), run_id=run_id,
                )
            return
        sequence = summary["progress_events"]
        if not sequence:
            return
        progress = "attempt-progress-" + hashlib.sha256(
            f"{run_id}:{sequence}".encode()
        ).hexdigest()
        if self.marker(progress, {
            "progress_events": sequence, "run_id": run_id,
            "schema": EVENT_SCHEMA,
        }):
            self.event(
                "attempt_progress", claim["ticket"],
                head_sha=attempt.get("role_head_before"),
                provider_attempt_id=attempt.get("provider_attempt_id"),
                role=claim.get("role"), route_id=attempt.get("route_id"),
                run_id=run_id, transition_receipt_sha256=claim.get("receipt"),
                **summary,
            )

    def observe_attempt_safely(self, claim: dict[str, Any]) -> None:
        try:
            self.observe_attempt(claim)
        except (ControllerError, OSError) as error:
            receipt = claim.get("receipt", "")
            marker = "attempt-observation-invalid-" + hashlib.sha256(
                f"{claim['ticket']}:{receipt}".encode()
            ).hexdigest()
            if self.marker(marker, {
                "schema": EVENT_SCHEMA,
                "ticket": claim["ticket"],
                "transition_receipt_sha256": receipt,
            }):
                self.event(
                    "attempt_observation_invalid", claim["ticket"],
                    error=str(error), role=claim.get("role"),
                    transition_receipt_sha256=receipt,
                )

    def emit_attempt_terminal(
        self, claim: dict[str, Any], terminal: dict[str, str]
    ) -> None:
        run_id = terminal.get("run_id", "")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", run_id):
            raise ControllerError("terminal run identity is invalid")
        duplicate_count = terminal.get("duplicate_launch_void_count", "1")
        if not duplicate_count.isdigit() or not 1 <= int(duplicate_count) <= 10_000:
            raise ControllerError("terminal duplicate count is invalid")
        submitted_at = terminal.get("submitted_at_epoch_ns", "")
        submitted_at_valid = re.fullmatch(r"[1-9][0-9]{0,19}", submitted_at)
        terminal_at = terminal.get("terminal_at_epoch_ns", "")
        terminal_at_valid = re.fullmatch(r"[1-9][0-9]{0,19}", terminal_at)
        details = {
            "accounting_state": terminal.get("accounting_state"),
            "duplicate_launch_void_count": int(duplicate_count),
            "exit_status": terminal.get("exit_status"),
            "go_issued": terminal.get("go_issued"),
            "head_sha": terminal.get("role_head_before"),
            "provider_attempt_id": terminal.get("provider_attempt_id"),
            "progress_events": terminal.get("progress_events"),
            "role": claim.get("role"),
            "role_exit": terminal.get("role_exit"),
            "route_id": terminal.get("route_id"),
            "run_id": run_id,
            "submitted_at_epoch_ns": int(submitted_at) if submitted_at_valid else None,
            "task_submitted": terminal.get("task_submitted"),
            "terminal_at_epoch_ns": int(terminal_at) if terminal_at_valid else None,
            "terminal_reason_code": terminal.get("terminal_reason_code", ""),
            "transition_receipt_sha256": claim.get("receipt"),
        }
        self.event_once("attempt_terminal", claim["ticket"], **details)

    def terminal_already_exported(
        self, claim: dict[str, Any], terminal: dict[str, str]
    ) -> bool:
        path = self.state / "passports" / f"{claim['ticket']}.json"
        if not path.exists():
            return False
        value = read(path)
        expected = (
            terminal.get("run_id"),
            claim.get("role"),
            claim.get("receipt"),
        )
        charges = [
            (
                item.get("run_id"),
                item.get("role"),
                item.get("transition_receipt_sha256"),
            )
            for item in value.get("charge_records", [])
        ]
        completed = [
            (
                item.get("run_id"),
                item.get("role"),
                item.get("transition_receipt_sha256"),
            )
            for item in value.get("completed_role_evidence", [])
        ]
        successful = (
            terminal.get("exit_status") == "0"
            and terminal.get("role_exit") == "ok"
        )
        return (
            value.get("transition_receipt_sha256") == claim.get("receipt")
            and charges.count(expected) == 1
            and (not successful or completed.count(expected) == 1)
        )

    def passport(self, claim: dict[str, Any], publication: str) -> None:
        self.json_call(
            "passport", "export", "--ticket", claim["ticket"],
            "--receipt", claim["receipt"], "--publication-state", publication,
            "--workdir", claim["worktree"], "--json",
        )

    def archive_emergency_admission(
        self, claim: dict[str, Any], terminal: dict[str, str]
    ) -> None:
        if not (
            self.state / "emergency-admissions" / claim["ticket"]
        ).is_dir():
            return
        value = self.json_call(
            "emergency-admit", "archive", "--ticket", claim["ticket"],
            "--role", claim["role"], "--receipt", claim["receipt"],
            "--workdir", claim["worktree"], "--json",
        )
        if value.get("status") == "absent":
            return
        if (
            value.get("action") != "archive"
            or value.get("status") != "archived"
            or value.get("ticket") != claim["ticket"]
            or not DIGEST.fullmatch(value.get("approval_sha256", ""))
            or not DIGEST.fullmatch(value.get("record_sha256", ""))
        ):
            raise ControllerError("emergency admission archive is invalid")
        self.event_once(
            "emergency_admission_archived", claim["ticket"],
            approval_sha256=value["approval_sha256"],
            archive_sha256=value["record_sha256"],
            role=claim["role"], run_id=terminal.get("run_id"),
            transition_receipt_sha256=claim["receipt"],
        )

    def migrate_passport(
        self, claim: dict[str, Any], publication: str, expected_head: str = "",
    ) -> dict[str, Any]:
        path = self.state / "passports" / f"{claim['ticket']}.json"
        if not path.exists():
            return {}
        arguments = [
            "passport", "migrate", "--ticket", claim["ticket"],
            "--publication-state", publication,
            "--workdir", claim["worktree"],
        ]
        if expected_head:
            arguments.extend(("--expected-head", expected_head))
        return self.json_call(*arguments, "--json")

    def correct_converged_success(
        self, claim: dict[str, Any], terminal: dict[str, str]
    ) -> None:
        result = self.json_call(
            "passport", "correct-converged-success",
            "--ticket", claim["ticket"],
            "--receipt", claim["receipt"],
            "--run-id", terminal["run_id"],
            "--workdir", claim["worktree"], "--json",
        )
        if result.get("status") != "ok":
            raise ControllerError("passport completion correction failed")

    def restore_model_identity_success(
        self, claim: dict[str, Any], terminal: dict[str, str]
    ) -> None:
        evidence = self.json_call(
            "passport", "verify-model-identity-success",
            "--ticket", claim["ticket"], "--receipt", claim["receipt"],
            "--run-id", terminal["run_id"],
            "--workdir", claim["worktree"], "--json",
        )
        expected = {
            name: evidence.get(name) for name in (
                "input_head", "migration_count", "migration_head", "output_head",
                "output_tree", "recovery_base_head", "restore_head", "revert_head",
                "recovery_status",
            )
        }
        if (
            evidence.get("schema") != "nysa.software-factory.ticket-passport/v1"
            or evidence.get("ticket") != claim["ticket"]
            or evidence.get("run_id") != terminal.get("run_id")
            or evidence.get("status") != "ok"
            or evidence.get("recovery_status")
            not in {"restore-required", "restored"}
            or isinstance(expected["migration_count"], bool)
            or not isinstance(expected["migration_count"], int)
            or not 1 <= expected["migration_count"] <= 32
            or expected["migration_head"] != expected["recovery_base_head"]
            or any(
                not SHA.fullmatch(expected[name] or "")
                for name in (
                    "input_head", "migration_head", "output_head", "output_tree",
                    "recovery_base_head", "revert_head",
                )
            )
            or (
                evidence["recovery_status"] == "restored"
                and not SHA.fullmatch(expected["restore_head"] or "")
            )
            or (
                evidence["recovery_status"] == "restore-required"
                and expected["restore_head"] != ""
            )
        ):
            raise ControllerError("model identity recovery evidence is invalid")

        self.ensure_lease(claim, "model-identity-success-recovery")
        head_status, local_head, remote_head = self.remote_cell_head_status(claim)
        if evidence["recovery_status"] == "restore-required":
            if (
                local_head != evidence["recovery_base_head"]
                or remote_head != evidence["recovery_base_head"]
                or head_status != "pushed"
            ):
                raise ControllerError("model identity recovery remote moved")
            with self.git_lock:
                restored = subprocess.run(
                    [
                        "git", "-C", claim["worktree"],
                        "-c", "user.name=Factory Controller",
                        "-c", "user.email=factory-controller@local",
                        "revert", "--no-edit", evidence["revert_head"],
                    ],
                    text=True, capture_output=True, check=False, timeout=120,
                )
                if restored.returncode:
                    subprocess.run(
                        ["git", "-C", claim["worktree"], "revert", "--abort"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        check=False, timeout=120,
                    )
                    raise ControllerError(
                        restored.stderr.strip()
                        or "model identity output restore failed"
                    )
            evidence = self.json_call(
                "passport", "verify-model-identity-success",
                "--ticket", claim["ticket"], "--receipt", claim["receipt"],
                "--run-id", terminal["run_id"],
                "--workdir", claim["worktree"], "--json",
            )
            if (
                evidence.get("status") != "ok"
                or evidence.get("recovery_status") != "restored"
            ):
                raise ControllerError("model identity output restore is invalid")

        head_status, local_head, remote_head = self.remote_cell_head_status(claim)
        if local_head != evidence.get("restore_head"):
            raise ControllerError("model identity recovery head changed")
        if remote_head == evidence.get("recovery_base_head"):
            if head_status != "resume_commit_not_pushed":
                raise ControllerError("model identity recovery ancestry is invalid")
            pushed = subprocess.run(
                [
                    "git", "-C", claim["worktree"], "push", "--no-force", "--",
                    "origin", f"{local_head}:refs/heads/{claim['branch']}",
                ],
                text=True, capture_output=True, check=False, timeout=120,
            )
            if pushed.returncode:
                raise ControllerError(
                    pushed.stderr.strip() or "model identity output push failed"
                )
        elif remote_head != local_head or head_status != "pushed":
            raise ControllerError("model identity recovery remote moved")
        if not self.remote_cell_head_valid(claim):
            raise ControllerError("model identity recovery push is unverified")

        publication = read(
            self.state / "passports" / f"{claim['ticket']}.json"
        ).get("publication_state", "")
        if publication not in {
            "none", "validating", "ready", "merge-pending", "merged", "repair",
        }:
            raise ControllerError("model identity recovery publication is invalid")
        migrated = self.migrate_passport(claim, "preserve")
        if migrated.get("status") != "ok":
            raise ControllerError("model identity recovery migration failed")
        if not self.terminal_already_exported(claim, terminal):
            self.passport(claim, publication)
        self.correct_converged_success(claim, terminal)

    def converged_success_exported(
        self, claim: dict[str, Any], terminal: dict[str, str]
    ) -> bool:
        value = read(self.state / "passports" / f"{claim['ticket']}.json")
        expected = (
            terminal.get("run_id"), claim.get("role"), claim.get("receipt"),
        )
        completed = [
            (
                item.get("run_id"), item.get("role"),
                item.get("transition_receipt_sha256"),
            )
            for item in value.get("completed_role_evidence", [])
            if isinstance(item, dict)
        ]
        corrections = [
            (
                item.get("run_id"), item.get("schema"),
                item.get("transition_receipt_sha256"),
                item.get("recovery_factory_sha"),
            )
            for item in value.get("completed_role_corrections", [])
            if isinstance(item, dict)
        ]
        return (
            completed.count(expected) == 1
            and sum(
                item == (
                    terminal.get("run_id"), schema, claim.get("receipt"),
                    self.release_path.name,
                )
                for item in corrections
                for schema in {
                    COMPLETION_CORRECTION_SCHEMA,
                    MODEL_IDENTITY_CORRECTION_SCHEMA,
                }
            ) == 1
        )

    def direct_model_identity_candidate(
        self, claim: dict[str, Any], terminal: dict[str, str] | None,
        receipt: str,
    ) -> bool:
        if terminal is None:
            return False
        run_id = terminal.get("run_id", "")
        if (
            not re.fullmatch(r"[A-Za-z0-9._-]{1,200}", run_id)
            or terminal.get("ticket") != claim["ticket"]
            or terminal.get("transition_receipt_sha256") != receipt
            or terminal.get("phase") != "completed"
            or terminal.get("accounting_state") != "abandoned_conservative"
            or terminal.get("go_issued") != "1"
            or terminal.get("task_submitted") != "1"
            or terminal.get("exit_status") != "9"
            or terminal.get("role_exit") != "provider_failed"
            or terminal.get("terminal_reason_code", "") != ""
            or terminal.get("role") not in {
                "planner", "spec-linter", "test-author", "builder", "reviewer",
                "narrator",
            }
            or claim.get("role", "") not in {"", terminal.get("role")}
            or terminal.get("adapter") not in {"cursor-anthropic", "cursor-openai"}
            or terminal.get("route_id", "").startswith("cursor-") is not True
            or not DIGEST.fullmatch(terminal.get("output_sha256", ""))
            or not terminal.get("progress_events", "").isdigit()
            or int(terminal["progress_events"]) <= 0
            or not DIGEST.fullmatch(terminal.get("progress_journal_sha256", ""))
        ):
            return False
        output = self.product / "factory/runs" / f"{run_id}.out"
        try:
            info = output.lstat()
            raw = output.read_bytes()
        except OSError:
            return False
        return (
            stat.S_ISREG(info.st_mode)
            and info.st_uid == os.geteuid()
            and info.st_nlink == 1
            and len(raw) <= 8 * 1024 * 1024
            and raw.count(b"cursor reported unapproved model: ") == 1
            and raw.count(b"Cursor output validation/redaction failed") == 1
        )

    def recover_direct_model_identity_success(
        self, claim: dict[str, Any], terminal: dict[str, str], receipt: str,
    ) -> None:
        response = self.call(
            "passport", "recover-model-identity-success",
            "--ticket", claim["ticket"], "--receipt", receipt,
            "--run-id", terminal["run_id"],
            "--workdir", claim["worktree"], "--json",
        )
        if response.returncode and temporarily_unavailable(
            response.stderr or response.stdout
        ):
            raise ExternalUnavailable(
                "external service is temporarily unavailable"
            )
        try:
            result = json.loads(response.stdout)
        except json.JSONDecodeError as error:
            raise ControllerError("model identity recovery returned malformed JSON") from error
        if not isinstance(result, dict):
            raise ControllerError("model identity recovery returned malformed JSON")
        if response.returncode == 75:
            if result == {
                "reason_code": "external_unavailable", "status": "wait",
            }:
                raise ExternalUnavailable(
                    "external service is temporarily unavailable"
                )
            raise ControllerError("external wait response is malformed")
        if response.returncode:
            if (
                result.get("schema") == "nysa.software-factory.ticket-passport/v1"
                and result.get("status") == "error"
                and result.get("error_kind") == "evidence"
            ):
                raise ModelIdentityEvidenceError(
                    "model identity recovery evidence was refused"
                )
            raise ControllerError("model identity recovery operation failed")
        if (
            result.get("schema") != "nysa.software-factory.ticket-passport/v1"
            or result.get("status") != "ok"
            or result.get("ticket") != claim["ticket"]
            or not DIGEST.fullmatch(result.get("passport", ""))
        ):
            raise ControllerError("model identity correction result is invalid")
        passport = read(
            self.state / "passports" / f"{claim['ticket']}.json"
        )
        if (
            passport.get("passport_sha256") != result["passport"]
            or passport.get("head_sha")
            != subprocess.run(
                ["git", "-C", claim["worktree"], "rev-parse", "HEAD"],
                text=True, capture_output=True, check=True, timeout=120,
            ).stdout.strip()
        ):
            raise ControllerError("model identity correction passport is invalid")
        self.ensure_lease(claim, "model-identity-success-recovery")
        status, local_head, remote_head = self.remote_cell_head_status(claim)
        input_head = terminal.get("role_head_before", "")
        if status == "resume_commit_not_pushed" and remote_head in {"", input_head}:
            push_exact_head(
                claim["worktree"], claim["branch"], local_head, remote_head,
            )
        elif status != "pushed" or remote_head != local_head:
            raise ControllerError("model identity recovery remote moved")
        if not self.remote_passport_valid(claim):
            raise ControllerError("model identity recovery push is unverified")
        claim.update(receipt="", role="", status="claimed")
        claim.pop("blocked_reason", None)
        self.save_claim(claim)
        self.event_once(
            "model_identity_success_recovered", claim["ticket"],
            failed_run_id=terminal["run_id"],
            transition_receipt_sha256=receipt,
        )

    def ticket_release_current(self, claim: dict[str, Any]) -> bool:
        try:
            route = json.loads(self.route_path(claim).read_text(encoding="utf-8"))
            pin_path = Path(claim["worktree"]) / "factory" / "KIT_PIN"
            pin_info = pin_path.lstat()
            if (
                not stat.S_ISREG(pin_info.st_mode)
                or pin_path.is_symlink()
                or pin_info.st_uid != os.geteuid()
                or pin_info.st_size > 100
            ):
                return False
            pin = pin_path.read_text(encoding="utf-8")
            ticket = (
                Path(claim["worktree"])
                / "factory" / "tickets" / f"{claim['ticket']}.md"
            ).read_text(encoding="utf-8")
        except (FileNotFoundError, json.JSONDecodeError, OSError, UnicodeError):
            return False
        leases = re.findall(r"^Kit-SHA:\s*(.*?)\s*$", ticket, re.M)
        return (
            isinstance(route, dict)
            and route.get("ticket") == claim["ticket"]
            and route.get("kit_sha") == self.release_path.name
            and pin == self.release_path.name + "\n"
            and leases == [self.release_path.name]
        )

    def exact_passportless_planner_receipt(
        self, claim: dict[str, Any], receipt: dict[str, Any]
    ) -> bool:
        worktree = Path(claim["worktree"])
        route_path = self.route_path(claim)
        ticket_path = (
            worktree / "factory" / "tickets" / f"{claim['ticket']}.md"
        )
        try:
            route_info = route_path.lstat()
            ticket_info = ticket_path.lstat()
            if any(
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) & 0o022
                or info.st_size > 1_000_000
                for info in (route_info, ticket_info)
            ):
                return False
            route_raw = route_path.read_bytes()
            route = json.loads(route_raw)
            if not isinstance(route, dict):
                return False
            ticket = ticket_path.read_text(encoding="utf-8")
            head = subprocess.run(
                ["git", "-C", str(worktree), "rev-parse", "HEAD"],
                text=True, capture_output=True, check=True, timeout=120,
            ).stdout.strip()
            tree = subprocess.run(
                ["git", "-C", str(worktree), "rev-parse", "HEAD^{tree}"],
                text=True, capture_output=True, check=True, timeout=120,
            ).stdout.strip()
            ticket_blob = subprocess.run(
                [
                    "git", "-C", str(worktree), "rev-parse",
                    f"HEAD:factory/tickets/{claim['ticket']}.md",
                ],
                text=True, capture_output=True, check=True, timeout=120,
            ).stdout.strip()
            product_common = subprocess.run(
                [
                    "git", "-C", str(self.product), "rev-parse",
                    "--path-format=absolute", "--git-common-dir",
                ],
                text=True, capture_output=True, check=True, timeout=120,
            ).stdout.strip()
            worktree_common = subprocess.run(
                [
                    "git", "-C", str(worktree), "rev-parse",
                    "--path-format=absolute", "--git-common-dir",
                ],
                text=True, capture_output=True, check=True, timeout=120,
            ).stdout.strip()
            origins = subprocess.run(
                [
                    "git", "-C", str(self.product), "remote", "get-url",
                    "--push", "--all", "origin",
                ],
                text=True, capture_output=True, check=True, timeout=120,
            ).stdout.splitlines()
        except (
            FileNotFoundError, json.JSONDecodeError, OSError,
            subprocess.SubprocessError,
        ):
            return False
        digest = receipt.get("receipt_sha256", "")
        immutable = {
            key: value for key, value in receipt.items()
            if key not in {
                "consumed", "consumed_at_epoch", "receipt_sha256",
            }
        }
        states = re.findall(r"^State:\s*(.*?)\s*$", ticket, re.I | re.M)
        kit_shas = re.findall(r"^Kit-SHA:\s*(.*?)\s*$", ticket, re.M)
        return not any((
            hashlib.sha256(canonical_document(immutable)).hexdigest() != digest,
            receipt.get("contract_version") not in CONTROLLER_CONTRACTS,
            receipt.get("factory_sha") != self.release_path.name,
            receipt.get("head_sha") != head,
            receipt.get("head_tree") != tree,
            receipt.get("lease_sha256")
            != hashlib.sha256(claim["lease"].encode()).hexdigest(),
            receipt.get("passport_sha256") is not None,
            receipt.get("project") != self.project,
            receipt.get("product_origin_sha256")
            != (
                hashlib.sha256(origins[0].encode()).hexdigest()
                if len(origins) == 1 and origins[0] else ""
            ),
            receipt.get("route_plan_sha256")
            != hashlib.sha256(route_raw).hexdigest(),
            receipt.get("ticket_blob") != ticket_blob,
            not product_common or not worktree_common
            or Path(product_common).resolve() != Path(worktree_common).resolve(),
            route.get("schema") != "ticket-model-route-plan/v1",
            route.get("ticket") != claim["ticket"],
            route.get("kit_sha") != self.release_path.name,
            states != ["Planning"],
            kit_shas != [self.release_path.name],
        ))

    def exact_passportless_route_migration_refusal(
        self, claim: dict[str, Any], receipt: dict[str, Any]
    ) -> bool:
        worktree = Path(claim["worktree"])
        ticket_path = f"factory/tickets/{claim['ticket']}.md"
        route_path = f"factory/route-plans/{claim['ticket']}.json"
        pin_path = "factory/KIT_PIN"
        old_head = receipt.get("head_sha", "")
        try:
            current_head = subprocess.run(
                ["git", "-C", str(worktree), "rev-parse", "HEAD"],
                text=True, capture_output=True, check=True, timeout=120,
            ).stdout.strip()
            clean = subprocess.run(
                ["git", "-C", str(worktree), "status", "--porcelain=v1", "-z"],
                text=True, capture_output=True, check=True, timeout=120,
            ).stdout
            changed = subprocess.run(
                ["git", "-C", str(worktree), "diff", "--name-only", old_head, current_head],
                text=True, capture_output=True, check=True, timeout=120,
            ).stdout.splitlines()
            commits = subprocess.run(
                ["git", "-C", str(worktree), "rev-list", "--count", f"{old_head}..{current_head}"],
                text=True, capture_output=True, check=True, timeout=120,
            ).stdout.strip()
            lineage = subprocess.run(
                ["git", "-C", str(worktree), "rev-list", "--reverse",
                 "--ancestry-path", f"{old_head}..{current_head}"],
                text=True, capture_output=True, check=True, timeout=120,
            ).stdout.splitlines()
            merges = subprocess.run(
                ["git", "-C", str(worktree), "rev-list", "--merges", f"{old_head}..{current_head}"],
                text=True, capture_output=True, check=True, timeout=120,
            ).stdout.strip()
            old_tree = subprocess.run(
                ["git", "-C", str(worktree), "rev-parse", f"{old_head}^{{tree}}"],
                text=True, capture_output=True, check=True, timeout=120,
            ).stdout.strip()
            old_ticket_blob = subprocess.run(
                ["git", "-C", str(worktree), "rev-parse", f"{old_head}:{ticket_path}"],
                text=True, capture_output=True, check=True, timeout=120,
            ).stdout.strip()
            old_route = subprocess.run(
                ["git", "-C", str(worktree), "show", f"{old_head}:{route_path}"],
                capture_output=True, check=True, timeout=120,
            ).stdout
            authenticated_fallback_head(
                self.product, worktree, claim["ticket"], claim["branch"],
                old_head, old_route,
            )
            parent = old_head
            for commit in lineage:
                if not self.exact_route_migration_commit(
                    claim, parent, commit,
                ):
                    return False
                parent = commit
            branch = subprocess.run(
                ["git", "-C", str(worktree), "symbolic-ref", "--quiet", "--short", "HEAD"],
                text=True, capture_output=True, check=True, timeout=120,
            ).stdout.strip()
            common = subprocess.run(
                ["git", "-C", str(worktree), "rev-parse", "--path-format=absolute",
                 "--git-common-dir"],
                text=True, capture_output=True, check=True, timeout=120,
            ).stdout.strip()
            product_common = subprocess.run(
                ["git", "-C", str(self.product), "rev-parse", "--path-format=absolute",
                 "--git-common-dir"],
                text=True, capture_output=True, check=True, timeout=120,
            ).stdout.strip()
            origins = subprocess.run(
                ["git", "-C", str(worktree), "remote", "get-url", "--push", "--all", "origin"],
                text=True, capture_output=True, check=True, timeout=120,
            ).stdout.splitlines()
            validate_route(self.product, worktree, claim["ticket"], self.release_path.name)
        except (OSError, RouteEvidenceError, subprocess.SubprocessError, ValueError):
            return False
        return not any((
            receipt.get("stage")
            != "REFUSE ticket Kit-SHA lease does not match the selected kit SHA",
            receipt.get("role") is not None,
            receipt.get("consumed") is not False,
            receipt.get("head_tree") != old_tree,
            receipt.get("ticket_blob") != old_ticket_blob,
            receipt.get("route_plan_sha256") != hashlib.sha256(old_route).hexdigest(),
            receipt.get("lease_sha256")
            != hashlib.sha256(claim.get("lease", "").encode()).hexdigest(),
            receipt.get("passport_sha256") is not None,
            branch != claim.get("branch"),
            not common or not product_common
            or Path(common).resolve() != Path(product_common).resolve(),
            len(origins) != 1 or receipt.get("product_origin_sha256")
            != hashlib.sha256(origins[0].encode()).hexdigest(),
            not SHA.fullmatch(old_head) or not SHA.fullmatch(current_head),
            bool(clean),
            not commits.isdigit() or not 1 <= int(commits) <= 32,
            len(lineage) != (int(commits) if commits.isdigit() else -1),
            bool(merges),
            set(changed) != {ticket_path, route_path}
            and set(changed) != {pin_path, ticket_path, route_path},
            not self.ticket_release_current(claim),
            not self.remote_cell_head_valid(claim),
        ))

    def release_bundle_refreshable(
        self, claim: dict[str, Any], passport: dict[str, Any]
    ) -> bool:
        bundle = (
            Path(claim["worktree"]) / "factory" / "attestations"
            / claim["ticket"] / "bundle.json"
        )
        try:
            info = bundle.lstat()
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_size > 1_000_000
            ):
                return False
            value = json.loads(bundle.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return False
        history = {
            item.get("factory_sha")
            for item in passport.get("factory_release_history", [])
            if isinstance(item, dict)
        }
        return (
            passport.get("factory_sha") == self.release_path.name
            and passport.get("current_state") in {"Awaiting Approval", "Approved"}
            and passport.get("publication_state") != "merged"
            and value.get("kit_sha") in history
            and value.get("kit_sha") != self.release_path.name
            and not claim.get("receipt")
            and not claim.get("role")
            and not claim.get("publication_lease")
            and not self.role_active(claim)
            and self.ticket_release_current(claim)
            and self.remote_passport_valid(claim)
            and not self.protected_base_current(claim, passport.get("head_sha", ""))
        )

    def bundle_refresh_migration_suffix(
        self, claim: dict[str, Any], passport: dict[str, Any],
        source_factory: str, source_passport_file: str,
    ) -> list[dict[str, Any]] | None:
        migrations = passport.get("migration_history")
        head = passport.get("head_sha", "")
        route = passport.get("route_plan_sha256", "")
        protected = passport.get("protected_base_sha", "")
        if (
            not isinstance(migrations, list)
            or not SHA.fullmatch(source_factory)
            or not DIGEST.fullmatch(source_passport_file)
            or not SHA.fullmatch(head)
            or not DIGEST.fullmatch(route)
            or not SHA.fullmatch(protected)
            or not DIGEST.fullmatch(passport.get("parent_file_sha256", ""))
            or not DIGEST.fullmatch(passport.get("parent_digest", ""))
            or not successor_release_lineage(
                passport.get("factory_release_history"), migrations,
                source_factory, passport.get("factory_sha", ""),
                valid_v2_migration,
            )
        ):
            return None
        starts = [
            index for index, edge in enumerate(migrations)
            if valid_v2_migration(edge)
            and edge["from_factory_sha"] == source_factory
            and edge["from_passport_file_sha256"] == source_passport_file
        ]
        if len(starts) != 1:
            return None
        suffix = migrations[starts[0]:]
        release_edges = suffix[:-1]
        final = suffix[-1] if suffix else {}
        return suffix if (
            bool(release_edges)
            and all(
                valid_v2_migration(item)
                and (
                    item["from_factory_sha"] != item["to_factory_sha"]
                    or self.exact_route_migration_commit(
                        claim, item["from_head_sha"], item["to_head_sha"],
                        migration=item,
                    )
                )
                for item in release_edges
            )
            and valid_v2_migration(final)
            and final["from_factory_sha"] == final["to_factory_sha"]
            == passport.get("factory_sha")
            and final["to_head_sha"] == head
            and final["from_head_sha"] != final["to_head_sha"]
            and final["to_route_plan_sha256"] == route
            and final["from_route_plan_sha256"]
            != final["to_route_plan_sha256"]
            and final["from_protected_base_sha"]
            == final["to_protected_base_sha"] == protected
            and all(
                prior["to_factory_sha"] == following["from_factory_sha"]
                and prior["to_head_sha"] == following["from_head_sha"]
                and prior["to_protected_base_sha"]
                == following["from_protected_base_sha"]
                and prior["to_route_plan_sha256"]
                == following["from_route_plan_sha256"]
                for prior, following in zip(suffix, suffix[1:])
            )
            and final["from_passport_file_sha256"]
            == passport["parent_file_sha256"]
            and final["from_passport_sha256"]
            == passport["parent_digest"]
        ) else None

    def bundle_refresh_handoff_pending(
        self, claim: dict[str, Any], *, rotated_lease: bool = False,
    ) -> bool:
        if (
            (
                claim.get("status") != "blocked"
                if not rotated_lease else
                claim.get("status") not in {"blocked", "claimed", "waiting"}
            )
            or claim.get("receipt")
            or claim.get("role")
            or not DIGEST.fullmatch(claim.get("lease", ""))
            or not rotated_lease and claim.get("lease_released") is True
            or claim.get("publication_lease")
            or self.role_active(claim)
        ):
            return False
        marker_name = (
            f"bundle-refresh-transition-{claim['ticket']}-"
            f"{self.release_path.name}"
        )
        passport_path = self.state / "passports" / f"{claim['ticket']}.json"
        try:
            marker = read(self.state / f"{marker_name}.json")
            receipt = self.transition_receipt(claim, allow_prior=True)
            passport = self.authenticated_operator_passport(claim["ticket"])
            passport_file = hashlib.sha256(passport_path.read_bytes()).hexdigest()
        except (
            ControllerError, FileNotFoundError, json.JSONDecodeError, OSError,
            UnicodeError,
        ):
            return False
        suffix = (
            self.bundle_refresh_migration_suffix(
                claim, passport, marker.get("from_factory_sha", ""),
                marker.get("from_passport_file_sha256", ""),
            )
            if passport is not None else None
        )
        current_lease = hashlib.sha256(claim["lease"].encode()).hexdigest()
        handoff_lease = (
            marker.get("lease_sha256") if rotated_lease else current_lease
        )
        if not DIGEST.fullmatch(handoff_lease or ""):
            return False
        stage = receipt.get("stage", "") if receipt else ""
        expected = {
            "factory_sha": self.release_path.name,
            "from_factory_sha": suffix[0]["from_factory_sha"] if suffix else None,
            "from_passport_file_sha256": (
                suffix[0]["from_passport_file_sha256"] if suffix else None
            ),
            "from_receipt_sha256": marker.get("from_receipt_sha256"),
            "head_sha": passport.get("head_sha") if passport else None,
            "lease_sha256": handoff_lease,
            "passport_file_sha256": passport_file,
            "route_plan_sha256": (
                passport.get("route_plan_sha256") if passport else None
            ),
            "schema": EVENT_SCHEMA,
            "ticket": claim["ticket"],
        }
        prior = receipt is not None and receipt.get("factory_sha") != self.release_path.name
        current = receipt is not None and receipt.get("factory_sha") == self.release_path.name
        return bool(
            suffix
            and receipt is not None
            and marker == expected
            and passport.get("factory_sha") == self.release_path.name
            and passport.get("branch") == claim["branch"]
            and passport.get("current_state") in {
                "Awaiting Approval", "Approved",
            }
            and passport.get("publication_state") != "merged"
            and (prior or receipt.get("consumed") is False)
            and receipt.get("role") is None
            and (
                (
                    prior
                    and (
                        stage in BUNDLE_REFRESH_PRIOR_STAGES
                        or stage == BUNDLE_REFRESH_APPROVED_STAGE
                        and passport.get("current_state") == "Approved"
                    )
                    and receipt.get("factory_sha")
                    == marker["from_factory_sha"]
                    and receipt.get("passport_sha256")
                    == marker["from_passport_file_sha256"]
                    and receipt.get("receipt_sha256")
                    == marker["from_receipt_sha256"]
                    and DIGEST.fullmatch(receipt.get("lease_sha256", ""))
                    and receipt.get("head_sha")
                    == suffix[0]["from_head_sha"]
                    and receipt.get("route_plan_sha256")
                    == suffix[0]["from_route_plan_sha256"]
                )
                or (
                    current
                    and not stage.startswith(
                        "REFUSE dependency refresh required"
                    )
                    and (
                        stage.startswith("REFUSE ")
                        or stage.startswith("AWAIT-")
                    )
                    and receipt.get("parent_digest")
                    == marker["from_receipt_sha256"]
                    and receipt.get("lease_sha256") == handoff_lease
                    and receipt.get("passport_sha256") == passport_file
                    and receipt.get("head_sha") == marker["head_sha"]
                    and receipt.get("route_plan_sha256")
                    == marker["route_plan_sha256"]
                )
            )
        )

    def refresh_prior_release_receipt(self, claim: dict[str, Any]) -> str:
        """Issue or recover the exact current receipt for bundle refresh."""
        receipt = self.transition_receipt(claim, allow_prior=True)
        if receipt is None:
            return ""
        passport = self.authenticated_operator_passport(claim["ticket"])
        passport_path = (
            self.state / "passports" / f"{claim['ticket']}.json"
        )
        lease = claim.get("lease", "")
        passport_file = (
            hashlib.sha256(passport_path.read_bytes()).hexdigest()
            if passport is not None else ""
        )
        marker_name = (
            f"bundle-refresh-transition-{claim['ticket']}-"
            f"{self.release_path.name}"
        )
        marker_path = self.state / f"{marker_name}.json"
        expected: dict[str, Any] | None = None
        if receipt.get("factory_sha") == self.release_path.name:
            try:
                expected = read(marker_path)
            except (FileNotFoundError, json.JSONDecodeError, OSError) as error:
                raise ControllerError(
                    "bundle-refresh transition marker is unavailable"
                ) from error
            source_factory = expected.get("from_factory_sha", "")
            source_passport_file = expected.get(
                "from_passport_file_sha256", ""
            )
        else:
            source_factory = receipt.get("factory_sha", "")
            source_passport_file = receipt.get("passport_sha256", "")
        suffix = (
            self.bundle_refresh_migration_suffix(
                claim, passport, source_factory, source_passport_file,
            )
            if passport is not None else None
        )
        if (
            passport is None
            or passport.get("factory_sha") != self.release_path.name
            or passport.get("branch") != claim["branch"]
            or passport.get("current_state") not in {
                "Awaiting Approval", "Approved",
            }
            or passport.get("publication_state") == "merged"
            or not SHA.fullmatch(passport.get("head_sha", ""))
            or not DIGEST.fullmatch(passport.get("route_plan_sha256", ""))
            or suffix is None
            or not DIGEST.fullmatch(lease)
            or claim.get("lease_released") is True
            or claim.get("publication_lease")
            or self.role_active(claim)
        ):
            raise ControllerError("bundle-refresh receipt authority is invalid")

        first = suffix[0]
        current_lease = hashlib.sha256(lease.encode()).hexdigest()
        transition: dict[str, Any] | None = None
        if receipt.get("factory_sha") != self.release_path.name:
            expected = {
                "factory_sha": self.release_path.name,
                "from_factory_sha": first["from_factory_sha"],
                "from_passport_file_sha256": first[
                    "from_passport_file_sha256"
                ],
                "from_receipt_sha256": receipt.get("receipt_sha256"),
                "head_sha": passport["head_sha"],
                "lease_sha256": current_lease,
                "passport_file_sha256": passport_file,
                "route_plan_sha256": passport["route_plan_sha256"],
                "schema": EVENT_SCHEMA,
                "ticket": claim["ticket"],
            }
            if (
                receipt.get("factory_sha") != first["from_factory_sha"]
                or receipt.get("passport_sha256")
                != first["from_passport_file_sha256"]
                or receipt.get("head_sha") != first["from_head_sha"]
                or receipt.get("route_plan_sha256")
                != first["from_route_plan_sha256"]
                or not DIGEST.fullmatch(receipt.get("lease_sha256", ""))
                or receipt.get("role") is not None
                or not (
                    receipt.get("stage") in BUNDLE_REFRESH_PRIOR_STAGES
                    or receipt.get("stage") == BUNDLE_REFRESH_APPROVED_STAGE
                    and passport.get("current_state") == "Approved"
                )
            ):
                raise ControllerError(
                    "bundle-refresh prior receipt is invalid"
                )
            self.marker(marker_name, expected)
            if read(marker_path) != expected:
                raise ControllerError(
                    "bundle-refresh transition marker is invalid"
                )
            transition = self.json_call(
                "state-machine", "--ticket", claim["ticket"],
                "--lease", lease, "--workdir", claim["worktree"],
                "--json", timeout=None,
            )
            receipt = self.transition_receipt(claim, allow_prior=True)

        expected_marker = {
            "factory_sha": self.release_path.name,
            "from_factory_sha": first["from_factory_sha"],
            "from_passport_file_sha256": first[
                "from_passport_file_sha256"
            ],
            "from_receipt_sha256": (
                receipt.get("parent_digest") if receipt else None
            ),
            "head_sha": passport["head_sha"],
            "lease_sha256": current_lease,
            "passport_file_sha256": passport_file,
            "route_plan_sha256": passport["route_plan_sha256"],
            "schema": EVENT_SCHEMA,
            "ticket": claim["ticket"],
        }
        stage = receipt.get("stage", "") if receipt else ""
        if (
            receipt is None
            or expected != expected_marker
            or receipt.get("factory_sha") != self.release_path.name
            or receipt.get("ticket") != claim["ticket"]
            or receipt.get("branch") != claim["branch"]
            or receipt.get("head_sha") != passport.get("head_sha")
            or receipt.get("route_plan_sha256")
            != passport.get("route_plan_sha256")
            or receipt.get("lease_sha256") != expected["lease_sha256"]
            or receipt.get("passport_sha256") != passport_file
            or receipt.get("consumed") is not False
            or receipt.get("role") is not None
            or stage.startswith("REFUSE dependency refresh required")
            or not (stage.startswith("REFUSE ") or stage.startswith("AWAIT-"))
            or transition is not None
            and (
                not valid_transition_evidence(transition, claim["ticket"])
                or transition.get("receipt")
                != receipt.get("receipt_sha256")
                or transition.get("stage") != stage
                or transition.get("role") is not None
            )
        ):
            raise ControllerError("bundle-refresh receipt reissue is invalid")
        return receipt["receipt_sha256"]

    def locally_valid_operator_passport(
        self, claim: dict[str, Any],
    ) -> dict[str, Any] | None:
        validation = self.json_call(
            "passport", "validate", "--ticket", claim["ticket"],
            "--workdir", claim["worktree"], "--json",
        )
        passport = read(
            self.state / "passports" / f"{claim['ticket']}.json"
        )
        head = passport.get("head_sha", "")
        branch = passport.get("branch", "")
        if (
            validation.get("status") != "ok"
            or validation.get("passport") != passport.get("passport_sha256")
            or not SHA.fullmatch(head)
            or branch != claim["branch"]
        ):
            return None
        return passport

    def remote_passport_valid(self, claim: dict[str, Any]) -> bool:
        passport = self.locally_valid_operator_passport(claim)
        if passport is None:
            return False
        head = passport["head_sha"]
        branch = passport["branch"]
        try:
            remote = subprocess.run(
                [
                    "git", "-C", claim["worktree"], "ls-remote", "--exit-code",
                    "origin", f"refs/heads/{branch}",
                ],
                text=True, capture_output=True, check=False, timeout=120,
            )
        except subprocess.TimeoutExpired as error:
            raise ExternalUnavailable(
                "external service is temporarily unavailable"
            ) from error
        if remote.returncode and temporarily_unavailable(
            remote.stderr or remote.stdout
        ):
            raise ExternalUnavailable(
                "external service is temporarily unavailable"
            )
        return remote.returncode == 0 and remote.stdout == (
            f"{head}\trefs/heads/{branch}\n"
        )

    def remote_cell_head_status(
        self, claim: dict[str, Any]
    ) -> tuple[str, str, str]:
        head = subprocess.run(
            ["git", "-C", claim["worktree"], "rev-parse", "HEAD"],
            text=True, capture_output=True, check=False, timeout=120,
        ).stdout.strip()
        if not SHA.fullmatch(head):
            return "remote_unavailable", "", ""
        try:
            remote = subprocess.run(
                [
                    "git", "-C", claim["worktree"], "ls-remote", "--exit-code",
                    "origin", f"refs/heads/{claim['branch']}",
                ],
                text=True, capture_output=True, check=False, timeout=120,
            )
        except subprocess.TimeoutExpired as error:
            raise ExternalUnavailable(
                "external service is temporarily unavailable"
            ) from error
        if remote.returncode and temporarily_unavailable(
            remote.stderr or remote.stdout
        ):
            raise ExternalUnavailable(
                "external service is temporarily unavailable"
            )
        if remote.returncode == 2 and not remote.stdout:
            return "resume_commit_not_pushed", head, ""
        match = re.fullmatch(
            rf"([0-9a-f]{{40}})\trefs/heads/{re.escape(claim['branch'])}\n",
            remote.stdout,
        )
        if remote.returncode != 0 or match is None:
            return "remote_unavailable", head, ""
        remote_head = match.group(1)
        if remote_head == head:
            return "pushed", head, remote_head
        ancestor = subprocess.run(
            [
                "git", "-C", claim["worktree"], "merge-base", "--is-ancestor",
                remote_head, head,
            ],
            text=True, capture_output=True, check=False, timeout=120,
        )
        reason = (
            "resume_commit_not_pushed"
            if ancestor.returncode == 0
            else (
                "resume_ancestry_invalid"
                if ancestor.returncode == 1
                else "remote_unavailable"
            )
        )
        return reason, head, remote_head

    def remote_cell_head_valid(self, claim: dict[str, Any]) -> bool:
        return self.remote_cell_head_status(claim)[0] == "pushed"

    @staticmethod
    def contract_resume_directive_status(
        ticket_text: str, receipt: str, baseline_text: str = "",
    ) -> str:
        if baseline_text:
            try:
                ticket_text = fresh_resume_text(ticket_text, baseline_text)
            except TicketTransitionError:
                return "resume_directives_ambiguous"
        attempts = re.findall(r"^OPERATOR RESUME(?: RECEIPT)?:", ticket_text, re.M)
        if not attempts:
            return "waiting"
        roles = re.findall(
            r"^OPERATOR RESUME: (planner|spec-linter|test-author|builder)$",
            ticket_text, re.M,
        )
        receipts = re.findall(
            r"^OPERATOR RESUME RECEIPT: ([0-9a-f]{64})$", ticket_text, re.M,
        )
        if len(roles) != 1 or len(receipts) != 1:
            return "resume_directives_ambiguous"
        if receipts[0] != receipt:
            answer_attempts = re.findall(
                r"^OPERATOR ANSWER(?: RECEIPT)?:", ticket_text, re.M
            )
            answers = re.findall(
                r"^OPERATOR ANSWER: [^\r\n]+$", ticket_text, re.M
            )
            answer_receipts = re.findall(
                r"^OPERATOR ANSWER RECEIPT: ([0-9a-f]{64})$",
                ticket_text,
                re.M,
            )
            if (
                len(answer_attempts) == 2
                and len(answers) == 1
                and answer_receipts == [receipt]
            ):
                return "waiting"
            return "resume_receipt_mismatch"
        return "ready"

    def contract_resume_status(
        self, claim: dict[str, Any], ticket_text: str,
        transition: dict[str, Any] | None = None,
    ) -> str:
        if not (
            os.environ.get("FACTORY_KIT_TRUST_SCOPE")
            == "qualification-candidate"
            and os.environ.get("FACTORY_QUALIFICATION_MODE") == "isolated"
            and self.qualification
            and self.qualification.get("mode") == "successor"
        ):
            return self.contract_resume_directive_status(
                ticket_text, claim.get("receipt", ""),
            )
        transition = transition or self.transition_receipt(
            claim, allow_prior=True, record=False,
        )
        head = transition.get("head_sha", "") if transition else ""
        baseline = self.cell_git(
            claim, "show", f"{head}:factory/tickets/{claim['ticket']}.md",
        ) if SHA.fullmatch(head) else None
        if baseline is None or baseline.returncode:
            return "resume_ancestry_invalid"
        return self.contract_resume_directive_status(
            ticket_text, claim.get("receipt", ""), baseline.stdout,
        )

    def recover_terminal_exports(self, claims: list[dict[str, Any]]) -> None:
        for claim in claims:
            if (
                claim["status"] not in {"blocked", "running"}
                or not claim.get("receipt")
                or not claim.get("role")
                or self.role_active(claim)
            ):
                continue
            terminal = self.terminal_for_receipt(
                claim["ticket"], claim["receipt"]
            )
            if (
                terminal is None
                or terminal.get("exit_status") != "0"
                or terminal.get("role_exit") != "ok"
            ):
                continue
            self.ensure_lease(claim, "terminal-export")
            publication = (
                "validating"
                if claim["role"] in {"reviewer", "narrator"}
                else "none"
            )
            if self.terminal_already_exported(claim, terminal):
                self.migrate_passport(claim, publication)
            else:
                self.passport(claim, publication)
            self.archive_emergency_admission(claim, terminal)
            claim["status"] = "running"
            self.save_claim(claim)
            self.event(
                "terminal_export_retried", claim["ticket"],
                run_id=terminal.get("run_id"),
            )

    def ticket_run_inventory(self, ticket: str) -> list[tuple[str, str]]:
        selected = []
        for path in sorted((self.product / "factory/runs").glob("*.meta")):
            value = fields(path)
            if value.get("ticket") == ticket:
                selected.append((path.name, hashlib.sha256(path.read_bytes()).hexdigest()))
        return selected

    def ticket_run_snapshot(self, ticket: str) -> str:
        return hashlib.sha256(
            canonical(self.ticket_run_inventory(ticket)).encode()
        ).hexdigest()

    def reconciliation_marker(self, ticket: str) -> Path:
        return self.state / f"reconciling-{ticket}.json"

    def prepublication_retry_path(self, ticket: str) -> Path:
        return self.state / f"prepublication-retry-{ticket}.json"

    @staticmethod
    def bundle_sha256(claim: dict[str, Any]) -> str:
        path = (
            Path(claim["worktree"]) / "factory" / "attestations"
            / claim["ticket"] / "bundle.json"
        )
        info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or path.is_symlink()
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) & 0o022
            or info.st_size > 1_000_000
        ):
            raise ControllerError("prepublication bundle is unsafe")
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def mark_prepublication_retry(
        self, claim: dict[str, Any], pr: dict[str, Any]
    ) -> None:
        passport = read(self.state / "passports" / f"{claim['ticket']}.json")
        if (
            pr.get("status") != "ready"
            or not SHA.fullmatch(pr.get("head", ""))
            or isinstance(pr.get("pr_number"), bool)
            or not isinstance(pr.get("pr_number"), int)
            or pr["pr_number"] <= 0
            or passport.get("ticket") != claim["ticket"]
            or passport.get("branch") != claim["branch"]
            or passport.get("factory_sha") != self.release_path.name
            or not SHA.fullmatch(passport.get("head_sha", ""))
            or not DIGEST.fullmatch(passport.get("passport_sha256", ""))
        ):
            raise ControllerError("prepublication retry boundary is invalid")
        value = {
            "branch": claim["branch"],
            "bundle_sha256": self.bundle_sha256(claim),
            "factory_sha": self.release_path.name,
            "head_sha": passport["head_sha"],
            "passport_sha256": passport["passport_sha256"],
            "pr_number": pr["pr_number"],
            "reviewed_head": pr["head"],
            "run_snapshot_sha256": self.ticket_run_snapshot(claim["ticket"]),
            "schema": "nysa.software-factory.prepublication-retry/v1",
            "ticket": claim["ticket"],
        }
        path = self.prepublication_retry_path(claim["ticket"])
        if path.exists() and read(path) != value:
            raise ControllerError("prepublication retry boundary conflicts")
        if not path.exists():
            write(path, value)

    def recover_pushed_publication_refresh(
        self, claim: dict[str, Any],
    ) -> bool:
        marker_path = self.reconciliation_marker(claim["ticket"])
        passport_path = self.state / "passports" / f"{claim['ticket']}.json"
        if (
            claim.get("status") != "blocked"
            or claim.get("blocked_reason") not in {
                "controller-error", "external-unavailable",
            }
            or claim.get("receipt")
            or claim.get("role")
            or claim.get("publication_lease")
            or self.role_active(claim)
            or not marker_path.exists()
            or not passport_path.exists()
            or not self.ticket_release_current(claim)
        ):
            return False
        refresh = (
            Path(claim["worktree"]) / "factory" / "attestations"
            / claim["ticket"] / "refresh.json"
        )
        try:
            info = refresh.lstat()
        except OSError:
            return False
        if (
            not stat.S_ISREG(info.st_mode)
            or refresh.is_symlink()
            or info.st_nlink != 1
            or info.st_size > 1_000_000
        ):
            return False
        try:
            passport = self.authenticated_operator_passport(claim["ticket"])
            marker = read(marker_path)
        except (
            ControllerError, FileNotFoundError, json.JSONDecodeError, OSError,
            UnicodeError,
        ):
            return False
        if passport is None or marker != {
            "branch": claim["branch"],
            "factory_sha": self.release_path.name,
            "head_sha": passport.get("head_sha"),
            "passport_sha256": passport.get("passport_sha256"),
            "run_snapshot_sha256": self.ticket_run_snapshot(claim["ticket"]),
            "schema": "nysa.software-factory.reconciliation-boundary/v1",
            "ticket": claim["ticket"],
        }:
            return False
        status, head, remote = self.remote_cell_head_status(claim)
        refresh_head = self.cell_git(
            claim, "log", "-n", "1", "--format=%H", "--",
            f"factory/attestations/{claim['ticket']}/refresh.json",
        )
        clean = self.cell_git(claim, "status", "--porcelain=v1", "-z")
        if (
            refresh_head.returncode
            or head != refresh_head.stdout.strip()
            or head == passport.get("head_sha")
            or clean.returncode
            or clean.stdout
            or status not in {"pushed", "resume_commit_not_pushed"}
            or status == "pushed" and remote != head
            or status == "resume_commit_not_pushed"
            and remote != passport.get("head_sha")
        ):
            return False
        self.ensure_lease(claim, "publication-refresh-replay")
        value = self.json_call(
            "ticket-attest", "--ticket", claim["ticket"],
            "--lease", claim["lease"], "--workdir", claim["worktree"],
            "--action", "dependency-refresh-replay", "--json",
        )
        if (
            value.get("action") != "dependency-publication-refresh"
            or not SHA.fullmatch(value.get("head", ""))
        ):
            raise ControllerError("publication refresh replay was not exact")
        self.migrate_passport(claim, "validating")
        if not self.remote_passport_valid(claim):
            raise ControllerError("publication refresh replay passport is invalid")
        claim.update(receipt="", role="", status="claimed")
        claim.pop("blocked_reason", None)
        claim.pop("release_refresh_required", None)
        self.save_claim(claim)
        marker_path.unlink()
        self.event_once(
            "publication_refresh_recovered", claim["ticket"],
            head_sha=value["head"],
        )
        return True

    def recover_pushed_prepublication_attestation(
        self, claim: dict[str, Any],
    ) -> bool:
        marker_path = self.reconciliation_marker(claim["ticket"])
        passport_path = self.state / "passports" / f"{claim['ticket']}.json"
        if (
            claim.get("status") != "blocked"
            or claim.get("blocked_reason") not in {
                "controller-error", "external-unavailable",
            }
            or claim.get("receipt")
            or claim.get("role")
            or claim.get("publication_lease")
            or self.role_active(claim)
            or not marker_path.exists()
            or not passport_path.exists()
            or not self.ticket_release_current(claim)
        ):
            return False
        try:
            passport = self.authenticated_operator_passport(claim["ticket"])
            marker = read(marker_path)
            transition = self.transition_receipt(claim, record=False)
        except (
            ControllerError, FileNotFoundError, json.JSONDecodeError, OSError,
            UnicodeError,
        ):
            return False
        if passport is None or transition is None or marker != {
            "branch": claim["branch"],
            "factory_sha": self.release_path.name,
            "head_sha": passport.get("head_sha"),
            "passport_sha256": passport.get("passport_sha256"),
            "run_snapshot_sha256": self.ticket_run_snapshot(claim["ticket"]),
            "schema": "nysa.software-factory.reconciliation-boundary/v1",
            "ticket": claim["ticket"],
        }:
            return False
        status, head, remote = self.remote_cell_head_status(claim)
        clean = self.cell_git(claim, "status", "--porcelain=v1", "-z")
        if (
            head == passport.get("head_sha")
            or clean.returncode
            or clean.stdout
            or status not in {"pushed", "resume_commit_not_pushed"}
            or status == "pushed" and remote != head
            or status == "resume_commit_not_pushed"
            and remote != passport.get("head_sha")
        ):
            return False
        if passport.get("current_state") == "Review":
            action, publication, recovery = "bundle", "validating", "bundle"
        elif passport.get("current_state") == "Awaiting Approval":
            action, publication, recovery = (
                "approval", "merge-pending", "approval",
            )
        else:
            return False
        self.ensure_lease(claim, "pushed-prepublication-attestation")
        arguments = [
            "ticket-attest", "--ticket", claim["ticket"], "--lease",
            claim["lease"], "--receipt", transition["receipt_sha256"],
            "--workdir", claim["worktree"], "--action", action,
        ]
        if action == "approval":
            arguments.append("--attest-only")
        value = self.json_call(*arguments, "--json")
        if (
            value.get("action")
            != ("approval-attested" if action == "approval" else "bundle")
            or value.get("head") != head
        ):
            raise ControllerError("prepublication attestation replay was not exact")
        migrated = self.migrate_passport(
            claim, publication, expected_head=head,
        )
        if migrated.get("status") != "ok" or not self.remote_passport_valid(claim):
            raise ControllerError("pushed prepublication passport is invalid")
        claim.update(receipt="", role="", status="claimed")
        claim.pop("blocked_reason", None)
        self.save_claim(claim)
        marker_path.unlink()
        self.event_once(
            "pushed_prepublication_attestation_recovered", claim["ticket"],
            head_sha=head, recovery=recovery,
        )
        return True

    def recover_prepublication_attestations(
        self, claims: list[dict[str, Any]]
    ) -> None:
        for claim in claims:
            if (
                self.recover_pushed_publication_refresh(claim)
                or self.recover_pushed_prepublication_attestation(claim)
            ):
                continue
            marker_path = self.prepublication_retry_path(claim["ticket"])
            passport_path = self.state / "passports" / f"{claim['ticket']}.json"
            if (
                claim.get("status") != "blocked"
                or claim.get("blocked_reason") not in {
                    "controller-error", "external-unavailable",
                }
                or claim.get("receipt")
                or claim.get("role")
                or claim.get("publication_lease")
                or claim.get("parked") is not True
                or self.role_active(claim)
                or not passport_path.exists()
                or not self.ticket_release_current(claim)
            ):
                continue
            if not marker_path.exists():
                continue
            marker = read(marker_path)
            passport = read(passport_path)
            expected = {
                "branch": claim["branch"],
                "bundle_sha256": self.bundle_sha256(claim),
                "factory_sha": self.release_path.name,
                "head_sha": passport.get("head_sha"),
                "passport_sha256": passport.get("passport_sha256"),
                "pr_number": marker.get("pr_number"),
                "reviewed_head": marker.get("reviewed_head"),
                "run_snapshot_sha256": self.ticket_run_snapshot(claim["ticket"]),
                "schema": "nysa.software-factory.prepublication-retry/v1",
                "ticket": claim["ticket"],
            }
            worktree = Path(claim["worktree"])
            head = subprocess.run(
                ["git", "-C", str(worktree), "rev-parse", "HEAD"],
                text=True, capture_output=True, check=False, timeout=120,
            )
            status = subprocess.run(
                ["git", "-C", str(worktree), "status", "--porcelain=v1", "-z"],
                text=True, capture_output=True, check=False, timeout=120,
            )
            if (
                marker != expected
                or passport.get("ticket") != claim["ticket"]
                or passport.get("branch") != claim["branch"]
                or passport.get("factory_sha") != self.release_path.name
                or passport.get("publication_state") != "validating"
                or not SHA.fullmatch(marker.get("reviewed_head", ""))
                or isinstance(marker.get("pr_number"), bool)
                or not isinstance(marker.get("pr_number"), int)
                or marker["pr_number"] <= 0
                or head.returncode != 0
                or head.stdout.strip() != marker["reviewed_head"]
                or status.returncode != 0
                or status.stdout
                or not self.remote_passport_valid(claim)
            ):
                continue
            self.ensure_lease(claim, "prepublication-attestation")
            claim.update(receipt="", role="", status="claimed")
            claim.pop("blocked_reason", None)
            self.save_claim(claim)
            marker_path.unlink()
            self.event_once("prepublication_attestation_recovered", claim["ticket"])

    def mark_reconciling(
        self, claim: dict[str, Any], *, after_progress: bool = False,
    ) -> None:
        passport_path = self.state / "passports" / f"{claim['ticket']}.json"
        if claim.get("receipt") or claim.get("role") or not passport_path.exists():
            return
        passport = read(passport_path)
        value = {
            "branch": claim["branch"],
            "factory_sha": self.release_path.name,
            "head_sha": passport.get("head_sha"),
            "passport_sha256": passport.get("passport_sha256"),
            "run_snapshot_sha256": self.ticket_run_snapshot(claim["ticket"]),
            "schema": "nysa.software-factory.reconciliation-boundary/v1",
            "ticket": claim["ticket"],
        }
        path = self.reconciliation_marker(claim["ticket"])
        if path.exists():
            marker = read(path)
            if marker != value:
                if after_progress:
                    write(path, value)
                    self.event(
                        "reconciliation_boundary_progressed",
                        claim["ticket"],
                        from_head_sha=marker.get("head_sha"),
                        from_passport_sha256=marker.get("passport_sha256"),
                        head_sha=value["head_sha"],
                        passport_sha256=value["passport_sha256"],
                    )
                elif not self.reconciliation_boundary_successor(
                    claim, marker, value, passport,
                ):
                    raise ControllerError("ticket reconciliation boundary conflicts")
                else:
                    self.event_once(
                        "reconciliation_boundary_refresh_authorized",
                        claim["ticket"],
                        from_head_sha=marker["head_sha"],
                        from_passport_sha256=marker["passport_sha256"],
                        head_sha=value["head_sha"],
                        passport_sha256=value["passport_sha256"],
                    )
                    write(path, value)
        else:
            write(path, value)

    def reconciliation_boundary_successor(
        self, claim: dict[str, Any], marker: dict[str, Any],
        current: dict[str, Any], passport: dict[str, Any],
    ) -> bool:
        migrations = passport.get("migration_history")
        starts = [
            index for index, edge in enumerate(migrations or [])
            if valid_v2_migration(edge)
            and edge["from_factory_sha"] == marker.get("factory_sha")
            and edge["from_head_sha"] == marker.get("head_sha")
            and edge["from_passport_sha256"]
            == marker.get("passport_sha256")
        ] if isinstance(migrations, list) else []
        completed_role_start = False
        if not starts and isinstance(migrations, list):
            advanced = [
                index for index, edge in enumerate(migrations)
                if valid_v2_migration(edge)
                and edge["from_factory_sha"] == marker.get("factory_sha")
                and edge["from_head_sha"] == marker.get("head_sha")
                and self.reconciliation_completed_role_successor(
                    claim, marker, current, passport, edge,
                )
            ]
            if len(advanced) == 1:
                starts = advanced
                completed_role_start = True
        suffix = migrations[starts[0]:] if len(starts) == 1 else []
        final = suffix[-1] if suffix else {}
        if (
            set(marker) != {
                "branch", "factory_sha", "head_sha", "passport_sha256",
                "run_snapshot_sha256", "schema", "ticket",
            }
            or marker.get("schema")
            != "nysa.software-factory.reconciliation-boundary/v1"
            or marker.get("ticket") != claim["ticket"]
            or marker.get("branch") != claim["branch"]
            or (
                not completed_role_start
                and current.get("run_snapshot_sha256")
                != marker.get("run_snapshot_sha256")
            )
            or current.get("factory_sha") != self.release_path.name
            or current.get("head_sha") != passport.get("head_sha")
            or current.get("passport_sha256")
            != passport.get("passport_sha256")
            or passport.get("ticket") != claim["ticket"]
            or passport.get("branch") != claim["branch"]
            or passport.get("factory_sha") != self.release_path.name
            or not suffix
            or not all(valid_v2_migration(edge) for edge in suffix)
            or not all(
                prior["to_factory_sha"] == following["from_factory_sha"]
                and prior["to_head_sha"] == following["from_head_sha"]
                and prior["to_protected_base_sha"]
                == following["from_protected_base_sha"]
                and prior["to_route_plan_sha256"]
                == following["from_route_plan_sha256"]
                for prior, following in zip(suffix, suffix[1:])
            )
            or not passport_head_lineage(passport, marker.get("head_sha", ""))
            or not successor_release_lineage(
                passport.get("factory_release_history"), migrations,
                marker.get("factory_sha", ""), self.release_path.name,
                valid_v2_migration,
            )
            or final.get("to_factory_sha") != self.release_path.name
            or final.get("to_head_sha") != passport.get("head_sha")
            or final.get("to_protected_base_sha")
            != passport.get("protected_base_sha")
            or final.get("to_route_plan_sha256")
            != passport.get("route_plan_sha256")
            or passport.get("parent_digest")
            != final.get("from_passport_sha256")
            or passport.get("parent_file_sha256")
            != final.get("from_passport_file_sha256")
        ):
            return False
        status = subprocess.run(
            ["git", "-C", claim["worktree"], "status", "--porcelain=v1", "-z"],
            text=True, capture_output=True, check=False, timeout=120,
        )
        return (
            status.returncode == 0
            and not status.stdout
            and self.remote_passport_valid(claim)
        )

    def reconciliation_completed_role_successor(
        self, claim: dict[str, Any], marker: dict[str, Any],
        current: dict[str, Any], passport: dict[str, Any],
        edge: dict[str, Any],
    ) -> bool:
        """Authenticate one role export between an old marker and its suffix."""
        if (
            not self.qualification
            or marker.get("factory_sha") == self.release_path.name
            or edge.get("from_passport_sha256")
            == marker.get("passport_sha256")
        ):
            return False
        try:
            authenticated = self.authenticated_operator_passport(claim["ticket"])
            receipt = self.transition_receipt(
                claim, allow_prior=True, record=False,
            )
            inventory = self.ticket_run_inventory(claim["ticket"])
        except (
            AttributeError, ControllerError, QualificationArtifactError,
            FileNotFoundError, json.JSONDecodeError, OSError, UnicodeError,
        ):
            return False
        if authenticated != passport or receipt is None:
            return False
        receipt_digest = receipt.get("receipt_sha256", "")
        completed_records = passport.get("completed_role_evidence")
        charge_records = passport.get("charge_records")
        if not isinstance(completed_records, list) or not isinstance(
            charge_records, list,
        ):
            return False
        completed = [
            item for item in completed_records
            if isinstance(item, dict)
            and item.get("factory_sha") == marker.get("factory_sha")
            and item.get("head_before") == marker.get("head_sha")
            and item.get("transition_receipt_sha256") == receipt_digest
        ]
        if len(completed) != 1:
            return False
        evidence = completed[0]
        run_id = evidence.get("run_id", "")
        if not isinstance(run_id, str):
            return False
        charges = [
            item for item in charge_records
            if isinstance(item, dict)
            and item.get("run_id") == run_id
            and item.get("role") == evidence.get("role")
            and item.get("transition_receipt_sha256") == receipt_digest
        ]
        if len(charges) != 1:
            return False
        charge = charges[0]
        try:
            retained = retained_passport_digest_authenticated(
                self.state, claim["ticket"], marker.get("passport_sha256", ""),
            )
            terminal = self.terminal_for_receipt(
                claim["ticket"], receipt_digest,
            )
            manifest = self.product / "factory/runs" / f"{run_id}.meta"
            manifest_digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
            output = manifest.with_suffix(".out")
            output_digest = role_output_sha256(output)
        except (
            ControllerError, QualificationArtifactError, FileNotFoundError,
            OSError, RoleOutputError, UnicodeError,
        ):
            return False
        entry = (manifest.name, manifest_digest)
        prior_inventory = list(inventory)
        if prior_inventory.count(entry) == 1:
            prior_inventory.remove(entry)
        else:
            return False
        return bool(
            retained
            and terminal is not None
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", run_id)
            and isinstance(receipt_digest, str)
            and DIGEST.fullmatch(receipt_digest)
            and isinstance(receipt.get("passport_sha256"), str)
            and DIGEST.fullmatch(receipt["passport_sha256"])
            and receipt.get("ticket") == claim["ticket"]
            and receipt.get("branch") == claim["branch"]
            and receipt.get("factory_sha") == marker.get("factory_sha")
            and receipt.get("head_sha") == marker.get("head_sha")
            and receipt.get("role") == evidence.get("role")
            and receipt.get("stage") in {
                f"RUN {evidence.get('role')}", f"FIX {evidence.get('role')}",
            }
            and receipt.get("consumed") is True
            and evidence.get("contract_version")
            == receipt.get("contract_version")
            and isinstance(evidence.get("manifest_sha256"), str)
            and DIGEST.fullmatch(evidence["manifest_sha256"])
            and isinstance(evidence.get("output_sha256"), str)
            and DIGEST.fullmatch(evidence["output_sha256"])
            and charge.get("factory_sha") == evidence.get("factory_sha")
            and charge.get("head_before") == evidence.get("head_before")
            and charge.get("contract_version")
            == evidence.get("contract_version")
            and charge.get("manifest_sha256")
            == evidence.get("manifest_sha256") == manifest_digest
            and terminal.get("run_id") == run_id
            and terminal.get("ticket") == claim["ticket"]
            and terminal.get("role") == evidence.get("role")
            and terminal.get("kit_sha") == marker.get("factory_sha")
            and terminal.get("contract_version")
            == evidence.get("contract_version")
            and terminal.get("role_branch_before") == claim["branch"]
            and terminal.get("role_head_before") == marker.get("head_sha")
            and terminal.get("transition_receipt_sha256") == receipt_digest
            and terminal.get("phase") == "completed"
            and terminal.get("go_issued") == "1"
            and terminal.get("task_submitted") == "1"
            and terminal.get("exit_status") == "0"
            and terminal.get("role_exit") == "ok"
            and terminal.get("output_sha256") == evidence.get("output_sha256")
            and output_digest == evidence.get("output_sha256")
            and current.get("run_snapshot_sha256")
            == hashlib.sha256(canonical(inventory).encode()).hexdigest()
            and marker.get("run_snapshot_sha256")
            == hashlib.sha256(canonical(prior_inventory).encode()).hexdigest()
        )

    def prior_maintenance_receipt_successor(
        self, claim: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Admit one exact in-flight prior-release maintenance refusal."""
        if (
            (self.product / "factory/MAINTENANCE").exists()
            or claim.get("status") != "claimed"
            or claim.get("parked") is not True
            or claim.get("role")
            or claim.get("receipt")
            or claim.get("publication_lease")
            or claim.get("lease_released") is True
            or DIGEST.fullmatch(claim.get("lease", "")) is None
            or self.role_active(claim)
        ):
            return None
        complete_path = self.state / (
            f"passport-route-migration-complete-{claim['ticket']}-"
            f"{self.release_path.name}.json"
        )
        boundary_path = self.reconciliation_marker(claim["ticket"])
        try:
            receipt = self.transition_receipt(
                claim, allow_prior=True, record=False,
            )
            passport = self.authenticated_operator_passport(claim["ticket"])
            complete = read(complete_path)
            boundary = read(boundary_path)
            old_tree = subprocess.run(
                [
                    "git", "-C", claim["worktree"], "rev-parse",
                    f"{receipt.get('head_sha', '')}^{{tree}}",
                ],
                text=True, capture_output=True, check=True, timeout=120,
            ).stdout.strip()
            old_ticket = subprocess.run(
                [
                    "git", "-C", claim["worktree"], "rev-parse",
                    f"{receipt.get('head_sha', '')}:factory/tickets/"
                    f"{claim['ticket']}.md",
                ],
                text=True, capture_output=True, check=True, timeout=120,
            ).stdout.strip()
            old_route = subprocess.run(
                [
                    "git", "-C", claim["worktree"], "show",
                    f"{receipt.get('head_sha', '')}:factory/route-plans/"
                    f"{claim['ticket']}.json",
                ],
                capture_output=True, check=True, timeout=120,
            ).stdout
            clean = subprocess.run(
                [
                    "git", "-C", claim["worktree"], "status",
                    "--porcelain=v1", "-z",
                ],
                text=True, capture_output=True, check=True, timeout=120,
            ).stdout
        except (
            AttributeError, ControllerError, FileNotFoundError,
            json.JSONDecodeError, OSError, subprocess.SubprocessError,
            UnicodeError,
        ):
            return None
        migrations = passport.get("migration_history") if passport else None
        starts = [
            index for index, edge in enumerate(migrations or [])
            if valid_v2_migration(edge)
            and edge["from_factory_sha"] == receipt.get("factory_sha")
            and edge["from_head_sha"] == receipt.get("head_sha")
            and edge["from_passport_file_sha256"]
            == receipt.get("passport_sha256")
            and edge["from_route_plan_sha256"]
            == receipt.get("route_plan_sha256")
        ] if isinstance(migrations, list) else []
        suffix = migrations[starts[0]:] if len(starts) == 1 else []
        final = suffix[-1] if suffix else {}
        current = {
            "branch": claim["branch"],
            "factory_sha": self.release_path.name,
            "head_sha": passport.get("head_sha") if passport else None,
            "passport_sha256": (
                passport.get("passport_sha256") if passport else None
            ),
            "run_snapshot_sha256": self.ticket_run_snapshot(claim["ticket"]),
            "schema": "nysa.software-factory.reconciliation-boundary/v1",
            "ticket": claim["ticket"],
        }
        if (
            receipt is None
            or passport is None
            or receipt.get("factory_sha") == self.release_path.name
            or receipt.get("stage")
            != "REFUSE MAINTENANCE file present — factory control plane is paused"
            or receipt.get("role") is not None
            or receipt.get("loop") is not None
            or receipt.get("consumed") is not False
            or DIGEST.fullmatch(receipt.get("receipt_sha256", "")) is None
            or DIGEST.fullmatch(receipt.get("passport_sha256", "")) is None
            or old_tree != receipt.get("head_tree")
            or old_ticket != receipt.get("ticket_blob")
            or hashlib.sha256(old_route).hexdigest()
            != receipt.get("route_plan_sha256")
            or passport.get("ticket") != claim["ticket"]
            or passport.get("branch") != claim["branch"]
            or passport.get("factory_sha") != self.release_path.name
            or passport.get("current_state") != "Review"
            or passport.get("current_stage") != "RUN reviewer"
            or passport.get("publication_state") != "validating"
            or complete != {
                "factory_sha": self.release_path.name,
                "schema": EVENT_SCHEMA,
                "ticket": claim["ticket"],
            }
            or not suffix
            or not all(valid_v2_migration(edge) for edge in suffix)
            or not all(
                prior["to_factory_sha"] == following["from_factory_sha"]
                and prior["to_head_sha"] == following["from_head_sha"]
                and prior["to_protected_base_sha"]
                == following["from_protected_base_sha"]
                and prior["to_route_plan_sha256"]
                == following["from_route_plan_sha256"]
                for prior, following in zip(suffix, suffix[1:])
            )
            or not passport_head_lineage(
                passport, receipt.get("head_sha", ""),
            )
            or not successor_release_lineage(
                passport.get("factory_release_history"), migrations,
                receipt.get("factory_sha", ""), self.release_path.name,
                valid_v2_migration,
            )
            or final.get("to_factory_sha") != self.release_path.name
            or final.get("to_head_sha") != passport.get("head_sha")
            or final.get("to_protected_base_sha")
            != passport.get("protected_base_sha")
            or final.get("to_route_plan_sha256")
            != passport.get("route_plan_sha256")
            or final.get("from_passport_file_sha256")
            != passport.get("parent_file_sha256")
            or final.get("from_passport_sha256")
            != passport.get("parent_digest")
            or not self.ticket_release_current(claim)
            or bool(clean)
            or not self.remote_passport_valid(claim)
            or (
                boundary != current
                and not self.reconciliation_boundary_successor(
                    claim, boundary, current, passport,
                )
            )
        ):
            return None
        return receipt

    def recover_prior_maintenance_receipts(
        self, claims: list[dict[str, Any]],
    ) -> None:
        for claim in claims:
            if claim["ticket"] not in self.prior_transition_tickets:
                continue
            try:
                receipt = self.prior_maintenance_receipt_successor(claim)
            except (
                ControllerError, json.JSONDecodeError, OSError,
                subprocess.SubprocessError, UnicodeError,
            ):
                continue
            if receipt is None:
                continue
            try:
                self.ensure_lease(claim, "prior-maintenance-receipt")
                receipt = self.prior_maintenance_receipt_successor(claim)
                passport = self.authenticated_operator_passport(
                    claim["ticket"]
                )
                passport_path = (
                    self.state / "passports" / f"{claim['ticket']}.json"
                )
                if receipt is None or passport is None:
                    continue
                passport_file = hashlib.sha256(
                    passport_path.read_bytes()
                ).hexdigest()
            except (
                ControllerError, json.JSONDecodeError, OSError,
                subprocess.SubprocessError, UnicodeError,
            ):
                continue
            try:
                self.event_once(
                    "prior_maintenance_receipt_recovery_authorized",
                    claim["ticket"],
                    factory_sha=self.release_path.name,
                    from_factory_sha=receipt["factory_sha"],
                    head_sha=passport["head_sha"],
                    lease_sha256=hashlib.sha256(
                        claim["lease"].encode()
                    ).hexdigest(),
                    passport_file_sha256=passport_file,
                    route_plan_sha256=passport["route_plan_sha256"],
                    transition_receipt_sha256=receipt["receipt_sha256"],
                )
            except (
                ControllerError, json.JSONDecodeError, OSError,
                subprocess.SubprocessError, UnicodeError,
            ):
                continue
            transition = None
            try:
                transition = self.json_call(
                    "state-machine", "--ticket", claim["ticket"],
                    "--lease", claim["lease"], "--workdir",
                    claim["worktree"], "--json", timeout=None,
                )
            except (
                ControllerError, json.JSONDecodeError, OSError,
                subprocess.SubprocessError, UnicodeError,
            ):
                pass
            current = self.transition_receipt(
                claim, allow_prior=True, record=False,
            )
            stage = current.get("stage", "") if current else ""
            if (
                current is None
                or current.get("factory_sha") != self.release_path.name
                or current.get("parent_digest")
                != receipt["receipt_sha256"]
                or current.get("head_sha") != passport["head_sha"]
                or current.get("route_plan_sha256")
                != passport["route_plan_sha256"]
                or current.get("passport_sha256") != passport_file
                or current.get("lease_sha256")
                != hashlib.sha256(claim["lease"].encode()).hexdigest()
                or current.get("consumed") is not False
                or stage not in {
                    "RUN reviewer",
                    "REFUSE MAINTENANCE file present — "
                    "factory control plane is paused",
                }
                or current.get("role") != (
                    "reviewer" if stage == "RUN reviewer" else None
                )
                or current.get("loop") is not None
                or transition is not None
                and (
                    not valid_transition_evidence(
                        transition, claim["ticket"]
                    )
                    or transition.get("receipt")
                    != current.get("receipt_sha256")
                    or transition.get("stage") != stage
                )
            ):
                continue
            self.prior_transition_tickets.discard(claim["ticket"])

    def recover_interrupted_claims(self, claims: list[dict[str, Any]]) -> None:
        for claim in claims:
            worker_error = claim.get("blocked_reason") == "worker-error"
            controller_error = claim.get("blocked_reason") == "controller-error"
            external_wait = claim.get("blocked_reason") == "external-unavailable"
            marker_path = self.reconciliation_marker(claim["ticket"])
            passport_path = self.state / "passports" / f"{claim['ticket']}.json"
            if (
                claim["status"] != "blocked"
                or claim.get("receipt")
                or claim.get("role")
                or claim.get("blocked_reason") not in {
                    None, "controller-error", "external-unavailable",
                    "worker-error",
                }
                or worker_error and claim.get("parked") is not True
                or self.pause_path(claim["ticket"]).exists()
                or self.role_active(claim)
                or not marker_path.exists()
                or not passport_path.exists()
                or not self.ticket_release_current(claim)
            ):
                continue
            marker = read(marker_path)
            passport = read(passport_path)
            if marker != {
                "branch": claim["branch"],
                "factory_sha": self.release_path.name,
                "head_sha": passport.get("head_sha"),
                "passport_sha256": passport.get("passport_sha256"),
                "run_snapshot_sha256": self.ticket_run_snapshot(claim["ticket"]),
                "schema": "nysa.software-factory.reconciliation-boundary/v1",
                "ticket": claim["ticket"],
            }:
                continue
            worktree = Path(claim["worktree"])
            if subprocess.run(
                ["git", "-C", str(worktree), "status", "--porcelain=v1", "-z"],
                text=True, capture_output=True, check=True, timeout=120,
            ).stdout:
                continue
            try:
                if not self.remote_passport_valid(claim):
                    continue
                self.ensure_lease(claim, "interrupted-reconciliation")
            except ControllerError:
                continue
            claim.update(receipt="", role="", status="claimed")
            claim.pop("blocked_reason", None)
            self.save_claim(claim)
            marker_path.unlink()
            self.event_once(
                "worker_error_recovered" if worker_error else
                "controller_error_recovered" if controller_error else
                "external_service_recovered" if external_wait else
                "interrupted_claim_recovered",
                claim["ticket"],
            )

    def prior_provider_failure_successor(
        self, claim: dict[str, Any],
    ) -> bool:
        if (
            os.environ.get("FACTORY_KIT_TRUST_SCOPE")
            != "qualification-candidate"
            or os.environ.get("FACTORY_QUALIFICATION_MODE") != "isolated"
            or not self.qualification
            or self.qualification.get("mode") != "successor"
            or claim["ticket"] not in self.prior_transition_tickets
            or claim.get("status") != "running"
            or claim.get("role") not in {
                "planner", "spec-linter", "test-author", "builder",
                "reviewer", "narrator",
            }
            or not DIGEST.fullmatch(claim.get("receipt", ""))
            or not DIGEST.fullmatch(claim.get("lease", ""))
            or claim.get("lease_released") is True
            or claim.get("parked") is True
            or claim.get("publication_lease")
            or self.role_active(claim)
        ):
            return False
        try:
            receipt = self.transition_receipt(
                claim, allow_prior=True, record=False,
            )
            passport = self.authenticated_operator_passport(claim["ticket"])
            terminal = self.terminal_for_receipt(
                claim["ticket"], claim["receipt"],
            )
            clean = subprocess.run(
                [
                    "git", "-C", claim["worktree"], "status",
                    "--porcelain=v1", "-z",
                ],
                text=True, capture_output=True, check=True, timeout=120,
            ).stdout
        except (
            ControllerError, json.JSONDecodeError, OSError,
            subprocess.SubprocessError, UnicodeError,
        ):
            return False
        migrations = passport.get("migration_history") if passport else None
        starts = [
            edge for edge in migrations or []
            if valid_v2_migration(edge)
            and edge["from_factory_sha"] == receipt.get("factory_sha")
            and edge["from_head_sha"] == receipt.get("head_sha")
            and edge["from_passport_file_sha256"]
            == receipt.get("passport_sha256")
            and edge["from_route_plan_sha256"]
            == receipt.get("route_plan_sha256")
        ] if receipt is not None and isinstance(migrations, list) else []
        role = claim["role"]
        if (
            receipt is None
            or passport is None
            or terminal is None
            or len(starts) != 1
            or receipt.get("receipt_sha256") != claim["receipt"]
            or receipt.get("factory_sha") == self.release_path.name
            or receipt.get("stage") != f"RUN {role}"
            or receipt.get("role") != role
            or receipt.get("consumed") is not True
            or terminal.get("kit_sha") != receipt.get("factory_sha")
            or terminal.get("role") != role
            or terminal.get("role_head_before") != receipt.get("head_sha")
            or terminal.get("transition_receipt_sha256") != claim["receipt"]
            or terminal.get("role_exit") != "provider_failed"
            or terminal.get("task_submitted") != "1"
            or not terminal.get("route_id", "").startswith("cursor-")
            or passport.get("ticket") != claim["ticket"]
            or passport.get("branch") != claim["branch"]
            or passport.get("factory_sha") != self.release_path.name
            or passport.get("current_state") not in INFLIGHT_STATES
            or passport.get("publication_state") == "merged"
            or bool(clean)
            or not self.ticket_release_current(claim)
            or not self.route_migrated_failed_role(
                claim, terminal, passport,
            )
        ):
            return False
        return True

    def readmit_prior_provider_failures(
        self, claims: list[dict[str, Any]],
    ) -> None:
        for claim in claims:
            if self.prior_provider_failure_successor(claim):
                self.prior_transition_tickets.discard(claim["ticket"])

    def retire_refusal_readmission_attempt(
        self, claim: dict[str, Any], path: Path,
    ) -> bool:
        if not path.exists() and not path.is_symlink():
            return True
        try:
            record = self.dispatcher_lease_records().get(claim["ticket"])
        except (
            ControllerError, json.JSONDecodeError, OSError, UnicodeError,
        ):
            return False
        released = True
        if record is not None:
            lease = record["lease_id"]
            releasable = (
                claim.get("parked") is True
                and not claim.get("receipt")
                and not claim.get("role")
                and not claim.get("publication_lease")
                and not self.role_active(claim)
            )
            if releasable:
                try:
                    self.json_call(
                        "release", "--ticket", claim["ticket"],
                        "--lease", lease,
                    )
                except (
                    ControllerError, json.JSONDecodeError, OSError,
                    subprocess.SubprocessError, UnicodeError,
                ):
                    releasable = False
            if not releasable:
                retained = claim.get("lease", "")
                if (
                    DIGEST.fullmatch(retained)
                    and claim.get("lease_released") is not True
                    and retained != lease
                ):
                    return False
                claim.update(
                    blocked_reason="state-machine-refusal-cleanup",
                    lease=lease, status="blocked",
                )
                claim.pop("lease_released", None)
                try:
                    self.save_claim(claim)
                except OSError:
                    return False
                released = False
        try:
            path.unlink(missing_ok=True)
        except OSError:
            return False
        return released

    def reconcile_refusal_readmission_markers(
        self, claims: list[dict[str, Any]], protected_main: str | None,
    ) -> set[str]:
        pending: set[str] = set()
        for claim in claims:
            path = self.state / f"refusal-readmission-{claim['ticket']}.json"
            if not path.exists() and not path.is_symlink():
                continue
            candidate = (
                claim.get("status") == "blocked"
                and claim.get("blocked_reason") == "state-machine-refusal"
                and claim.get("parked") is True
                and not claim.get("receipt")
                and not claim.get("role")
                and not claim.get("publication_lease")
            )
            canceled = (
                candidate
                and protected_main is not None
                and self.product_ticket_canceled(
                    claim["ticket"], protected_main,
                )
            )
            if (
                not candidate
                or canceled
                or self.product_ticket_done(claim["ticket"])
            ) and not self.retire_refusal_readmission_attempt(claim, path):
                pending.add(claim["ticket"])
        return pending

    def recover_changed_state_machine_refusals(
        self, claims: list[dict[str, Any]], protected_main: str | None,
    ) -> None:
        for claim in claims:
            lease = ""
            attempt_path = (
                self.state / f"refusal-readmission-{claim['ticket']}.json"
            )
            if claim.get("status") != "blocked":
                self.retire_refusal_readmission_attempt(claim, attempt_path)
                continue
            if protected_main is None or not SHA.fullmatch(protected_main):
                self.retire_refusal_readmission_attempt(claim, attempt_path)
                continue
            passport_path = self.state / "passports" / f"{claim['ticket']}.json"
            released = (
                claim.get("lease") == "" and "lease_released" not in claim
                or DIGEST.fullmatch(claim.get("lease", "")) is not None
                and claim.get("lease_released") is True
            )
            if (
                claim.get("status") != "blocked"
                or claim.get("blocked_reason") != "state-machine-refusal"
                or claim.get("parked") is not True
                or not released
                or claim.get("receipt")
                or claim.get("role")
                or claim.get("publication_lease")
                or self.role_active(claim)
                or self.pause_path(claim["ticket"]).exists()
                or self.reconciliation_marker(claim["ticket"]).exists()
                or not passport_path.exists()
                or not self.ticket_release_current(claim)
            ):
                self.retire_refusal_readmission_attempt(claim, attempt_path)
                continue
            ticket_path = f"factory/tickets/{claim['ticket']}.md"
            try:
                current = self.transition_receipt(claim, record=False)
                attempt = read(attempt_path) if attempt_path.exists() else None
                if attempt is not None and (
                    set(attempt) != {
                        "factory_sha", "lease", "protected_base_sha",
                        "refusal", "refused_protected_base_sha", "schema",
                        "ticket",
                    }
                    or attempt.get("schema") != REFUSAL_READMISSION_SCHEMA
                    or attempt.get("factory_sha") != self.release_path.name
                    or attempt.get("ticket") != claim["ticket"]
                    or attempt.get("protected_base_sha") != protected_main
                    or not isinstance(attempt.get("refusal"), dict)
                    or not isinstance(attempt.get("lease"), str)
                    or attempt.get("lease") != ""
                    and not DIGEST.fullmatch(attempt["lease"])
                ):
                    self.retire_refusal_readmission_attempt(claim, attempt_path)
                    continue
                receipt = attempt["refusal"] if attempt is not None else current
                passport = self.authenticated_operator_passport(claim["ticket"])
                head = self.cell_git(claim, "rev-parse", "HEAD")
                tree = self.cell_git(claim, "rev-parse", "HEAD^{tree}")
                ticket_blob = self.cell_git(
                    claim, "rev-parse", f"HEAD:{ticket_path}",
                )
                branch = self.cell_git(
                    claim, "symbolic-ref", "--quiet", "--short", "HEAD",
                )
                tracking = self.cell_git(
                    claim, "rev-parse", "--verify",
                    "refs/remotes/origin/main^{commit}",
                )
                status = self.cell_git(claim, "status", "--porcelain=v1", "-z")
                route_digest = hashlib.sha256(
                    self.route_path(claim).read_bytes()
                ).hexdigest()
                passport_file = hashlib.sha256(
                    passport_path.read_bytes()
                ).hexdigest()
            except (
                ControllerError, FileNotFoundError, json.JSONDecodeError,
                OSError, subprocess.SubprocessError, UnicodeError,
            ):
                self.retire_refusal_readmission_attempt(claim, attempt_path)
                continue
            refused_base = (
                receipt.get("protected_base_sha", "")
                if receipt is not None and "protected_base_sha" in receipt
                else passport.get("protected_base_sha", "")
                if passport is not None else ""
            )
            if (
                receipt is None
                or current is None
                or passport is None
                or receipt.get("schema")
                != "nysa.software-factory.transition-receipt/v1"
                or receipt.get("ticket") != claim["ticket"]
                or receipt.get("branch") != claim["branch"]
                or receipt.get("project") != self.project
                or receipt.get("contract_version") not in CONTROLLER_CONTRACTS
                or receipt.get("receipt_sha256") != hashlib.sha256(
                    canonical_document({
                        key: item for key, item in receipt.items()
                        if key not in {
                            "consumed", "consumed_at_epoch", "receipt_sha256",
                        }
                    })
                ).hexdigest()
                or receipt.get("factory_sha") != self.release_path.name
                or not receipt.get("stage", "").startswith("REFUSE ")
                or receipt.get("consumed") is not False
                or receipt.get("role") is not None
                or receipt.get("loop") is not None
                or not SHA.fullmatch(refused_base)
                or refused_base == protected_main
                or receipt.get("head_sha") != passport.get("head_sha")
                or receipt.get("head_sha") != head.stdout.strip()
                or receipt.get("head_tree") != tree.stdout.strip()
                or receipt.get("ticket_blob") != ticket_blob.stdout.strip()
                or receipt.get("route_plan_sha256") != route_digest
                or receipt.get("route_plan_sha256")
                != passport.get("route_plan_sha256")
                or receipt.get("passport_sha256") != passport_file
                or passport.get("factory_sha") != self.release_path.name
                or passport.get("branch") != claim["branch"]
                or branch.stdout.strip() != claim["branch"]
                or tracking.stdout.strip() != protected_main
                or any(item.returncode for item in (
                    head, tree, ticket_blob, branch, tracking, status,
                ))
                or status.stdout
            ):
                self.retire_refusal_readmission_attempt(claim, attempt_path)
                continue
            expected_attempt = {
                "factory_sha": self.release_path.name,
                "lease": attempt["lease"] if attempt is not None else "",
                "protected_base_sha": protected_main,
                "refusal": receipt,
                "refused_protected_base_sha": refused_base,
                "schema": REFUSAL_READMISSION_SCHEMA,
                "ticket": claim["ticket"],
            }
            if attempt is not None and attempt != expected_attempt:
                self.retire_refusal_readmission_attempt(claim, attempt_path)
                continue
            try:
                if not self.remote_passport_valid(claim):
                    self.retire_refusal_readmission_attempt(claim, attempt_path)
                    continue
                records = self.dispatcher_lease_records()
                record = records.get(claim["ticket"])
                if attempt is None:
                    if record is not None:
                        continue
                    attempt = expected_attempt
                    write(attempt_path, attempt)
                lease = attempt["lease"]
                child = (
                    current is not None
                    and current.get("receipt_sha256")
                    != receipt.get("receipt_sha256")
                )
                if lease:
                    if record is None:
                        if child:
                            self.retire_refusal_readmission_attempt(
                                claim, attempt_path,
                            )
                            continue
                        lease = ""
                    elif record.get("lease_id") != lease:
                        self.retire_refusal_readmission_attempt(
                            claim, attempt_path,
                        )
                        continue
                elif record is not None:
                    lease = record["lease_id"]
                if not lease:
                    leased = self.json_call(
                        "claim", "--ticket", claim["ticket"],
                    )
                    lease = leased.get("lease_id", "")
                    if (
                        leased.get("schema_version") != 1
                        or leased.get("ticket") != claim["ticket"]
                        or not DIGEST.fullmatch(lease)
                    ):
                        raise ControllerError(
                            "state-machine refusal lease is invalid"
                        )
                if attempt["lease"] != lease:
                    attempt["lease"] = lease
                    write(attempt_path, attempt)
                transition = None
                if not child:
                    transition = self.json_call(
                        "state-machine", "--ticket", claim["ticket"],
                        "--lease", lease, "--workdir", claim["worktree"],
                        "--json", timeout=None,
                    )
                    current = self.transition_receipt(claim, record=False)
                stage = current.get("stage", "") if current is not None else ""
                evidence = transition or {
                    "action": stage.partition(" ")[0],
                    "detail": stage.partition(" ")[2] or None,
                    "loop": current.get("loop") if current is not None else None,
                    "receipt": (
                        current.get("receipt_sha256", "")
                        if current is not None else ""
                    ),
                    "role": current.get("role") if current is not None else None,
                    "schema": "nysa.software-factory.state-machine/v1",
                    "stage": stage, "status": "ok", "ticket": claim["ticket"],
                }
                if (
                    not valid_transition_evidence(evidence, claim["ticket"])
                    or current is None
                    or evidence.get("receipt") != current.get("receipt_sha256")
                    or current.get("parent_digest")
                    != receipt.get("receipt_sha256")
                    or current.get("stage") == receipt.get("stage")
                    or current.get("factory_sha") != self.release_path.name
                    or current.get("head_sha") != receipt.get("head_sha")
                    or current.get("head_tree") != receipt.get("head_tree")
                    or current.get("ticket_blob") != receipt.get("ticket_blob")
                    or current.get("route_plan_sha256")
                    != receipt.get("route_plan_sha256")
                    or current.get("product_origin_sha256")
                    != receipt.get("product_origin_sha256")
                    or current.get("evidence_sha256")
                    != receipt.get("evidence_sha256")
                    or (
                        current.get("stage", "").startswith("REFUSE ")
                        and current.get("protected_base_sha") != protected_main
                    )
                    or current.get("lease_sha256")
                    != hashlib.sha256(lease.encode()).hexdigest()
                    or current.get("consumed") is not False
                ):
                    raise ControllerError(
                        "changed state-machine refusal was not accepted"
                    )
            except (
                ControllerError, json.JSONDecodeError, OSError,
                subprocess.SubprocessError, UnicodeError,
            ):
                try:
                    if DIGEST.fullmatch(lease):
                        self.json_call(
                            "release", "--ticket", claim["ticket"],
                            "--lease", lease,
                        )
                        attempt["lease"] = ""
                        write(attempt_path, attempt)
                except (
                    ControllerError, json.JSONDecodeError, OSError,
                    subprocess.SubprocessError, UnicodeError,
                ):
                    claim.update(lease=lease)
                    claim.pop("lease_released", None)
                    self.save_claim(claim)
                continue
            claim.update(lease=lease, receipt="", role="", status="claimed")
            claim.pop("lease_released", None)
            claim.pop("blocked_reason", None)
            self.save_claim(claim)
            try:
                attempt_path.unlink(missing_ok=True)
            except OSError:
                pass
            self.event_once(
                "state_machine_refusal_readmitted", claim["ticket"],
                from_protected_base_sha=refused_base,
                protected_base_sha=protected_main,
                refused_receipt_sha256=receipt["receipt_sha256"],
                transition_receipt_sha256=current["receipt_sha256"],
            )

    def recover_missing_terminals(self, claims: list[dict[str, Any]]) -> None:
        for claim in claims:
            if (
                claim.get("status") != "blocked"
                or claim.get("blocked_reason") != "missing-terminal"
                or not DIGEST.fullmatch(claim.get("receipt", ""))
                or claim.get("role") not in {
                    "planner", "spec-linter", "test-author", "builder",
                    "reviewer", "narrator",
                }
                or claim.get("lease_released") is not True
                or self.role_active(claim)
            ):
                continue
            receipt = claim["receipt"]
            terminal = self.terminal_for_receipt(claim["ticket"], receipt)
            if (
                terminal is None
                or terminal.get("role") != claim["role"]
                or terminal.get("kit_sha") != self.release_path.name
            ):
                continue
            self.ensure_lease(claim, "missing-terminal")
            self.finish_pending_run(claim)
            self.event(
                "missing_terminal_recovered", claim["ticket"],
                run_id=terminal.get("run_id"),
                transition_receipt_sha256=receipt,
            )

    def recover_terminal_requests(self, claims: list[dict[str, Any]]) -> None:
        for claim in claims:
            passport_path = self.state / "passports" / f"{claim['ticket']}.json"
            request_path = self.terminal_request_path(claim["ticket"])
            if (
                claim.get("status") != "blocked"
                or claim.get("blocked_reason") not in {
                    "controller-error", "external-unavailable",
                }
                or claim.get("receipt")
                or claim.get("role")
                or claim.get("publication_lease")
                or claim.get("parked") is not True
                or self.role_active(claim)
                or not passport_path.exists()
                or not request_path.exists()
            ):
                continue
            passport = read(passport_path)
            worktree = Path(claim["worktree"])
            if (
                passport.get("ticket") != claim["ticket"]
                or passport.get("publication_state") != "merged"
                or subprocess.run(
                    ["git", "-C", str(worktree), "status", "--porcelain=v1", "-z"],
                    text=True, capture_output=True, check=False, timeout=120,
                ).stdout
                or not self.remote_passport_valid(claim)
            ):
                continue
            closeout_branch = f"chore/{claim['ticket'].lower().replace('-', '')}-closeout"
            try:
                if self.terminal_request(
                    claim, closeout_branch, create=False
                ) is None:
                    continue
                self.ensure_lease(claim, "terminal-replay")
            except (ControllerError, OSError, subprocess.SubprocessError):
                continue
            claim.update(status="claimed")
            claim.pop("blocked_reason", None)
            self.save_claim(claim)
            self.event_once("terminal_replay_recovered", claim["ticket"])

    def emit_recovery_abandoned(self, claim: dict[str, Any]) -> None:
        attempt = claim["recovery_attempt"]
        self.event_once(
            "ticket_recovery_abandoned", claim["ticket"],
            attempts=attempt["count"],
            input_sha256=attempt["input_sha256"],
            outcome_sha256=attempt["outcome_sha256"],
            recovery=attempt["recovery"],
        )

    def finish_recovery_abandonment(self, claim: dict[str, Any]) -> bool:
        attempt = claim["recovery_attempt"]
        try:
            self.withdraw_publication(claim)
            if (
                DIGEST.fullmatch(claim.get("lease", ""))
                and claim.get("lease_released") is not True
                and not self.role_active(claim)
            ):
                self.release_ticket_lease(claim)
        except (
            ControllerError, json.JSONDecodeError, OSError,
            subprocess.SubprocessError, UnicodeError,
        ):
            self.event_once(
                "ticket_recovery_abandonment_cleanup_waiting",
                claim["ticket"], recovery=attempt["recovery"],
            )
            return False
        self.emit_recovery_abandoned(claim)
        attempt["phase"] = "abandoned"
        self.save_claim(claim)
        return True

    def settle_recovery_attempt(
        self, claim: dict[str, Any], error: str = "",
    ) -> bool:
        attempt = claim.get("recovery_attempt")
        if not self.valid_recovery_attempt(attempt) or attempt["phase"] != "pending":
            return False
        current_input = self.recovery_input_sha256(
            claim, attempt["recovery"],
        )
        if (
            attempt["factory_sha"] != self.release_path.name
            or attempt["input_sha256"] != current_input
        ):
            claim.pop("recovery_attempt", None)
            self.save_claim(claim)
            return False
        outcome = self.recovery_outcome_sha256(claim, error)
        count = (
            attempt["count"] + 1
            if attempt["outcome_sha256"] == outcome else 1
        )
        attempt.update(
            count=count, outcome_sha256=outcome,
            phase=(
                "abandoning" if count >= RECOVERY_ATTEMPT_LIMIT else "settled"
            ),
        )
        if attempt["phase"] == "abandoning":
            claim["status"] = "blocked"
            claim["blocked_reason"] = (
                f"recovery-abandoned:{attempt['recovery']}"
            )
            attempt["input_sha256"] = self.recovery_input_sha256(
                claim, attempt["recovery"],
            )
        self.save_claim(claim)
        if attempt["phase"] == "abandoning":
            self.finish_recovery_abandonment(claim)
            return True
        return False

    def recovery_blocked(self, claim: dict[str, Any], name: str) -> bool:
        attempt = claim.get("recovery_attempt")
        if not attempt:
            return False
        if attempt["factory_sha"] != self.release_path.name:
            claim["status"] = attempt["retry_status"]
            if attempt["retry_reason"]:
                claim["blocked_reason"] = attempt["retry_reason"]
            else:
                claim.pop("blocked_reason", None)
            claim.pop("recovery_attempt", None)
            self.save_claim(claim)
            return False
        if (
            attempt["phase"] == "pending"
            and claim.get("status") in {"claimed", "running"}
        ):
            if (
                claim.get("status") == "claimed"
                and attempt["recovery"] == name == "targeted-repair"
                and claim["ticket"] in self.prior_transition_tickets
                and not claim.get("receipt")
                and not claim.get("role")
            ):
                try:
                    if self.remote_passport_valid(claim):
                        self.prior_transition_tickets.discard(claim["ticket"])
                except ControllerError:
                    pass
            return True
        if attempt["recovery"] != name:
            return True
        current_input = self.recovery_input_sha256(
            claim, attempt["recovery"],
        )
        if (
            attempt["input_sha256"] != current_input
        ):
            claim["status"] = attempt["retry_status"]
            if attempt["retry_reason"]:
                claim["blocked_reason"] = attempt["retry_reason"]
            else:
                claim.pop("blocked_reason", None)
            claim.pop("recovery_attempt", None)
            self.save_claim(claim)
            return False
        if attempt["phase"] == "abandoned":
            if self.readmit_stranded_route_upgrade(claim, name):
                return False
            return True
        if attempt["phase"] == "abandoning":
            if attempt["recovery"] == name:
                self.finish_recovery_abandonment(claim)
            return True
        if attempt["phase"] == "pending":
            if claim.get("status") in {"blocked", "budget", "waiting"}:
                if self.settle_recovery_attempt(claim):
                    return True
                attempt = claim.get("recovery_attempt")
            else:
                return True
        return bool(attempt and attempt["recovery"] != name)

    def readmit_stranded_route_upgrade(
        self, claim: dict[str, Any], name: str,
    ) -> bool:
        if (
            name != "release-upgrade"
            or not self.stranded_route_upgrade_wait(claim)
        ):
            return False
        pending = (
            f"passport-route-migration-pending-{claim['ticket']}-"
            f"{self.release_path.name}"
        )
        completed = (
            f"passport-route-migration-complete-{claim['ticket']}-"
            f"{self.release_path.name}"
        )
        try:
            marker = read(self.state / f"{pending}.json")
            terminal = self.terminal_for_receipt(
                claim["ticket"], claim.get("receipt", ""),
            )
            passport = self.authenticated_operator_passport(claim["ticket"])
            authorization = (
                self.migrated_stranded_semantic_authorization(
                    claim, terminal, passport,
                )
                if terminal is not None and passport is not None else None
            )
            ordinary_route = authorization is None and passport is not None
            if (
                ordinary_route
                and passport.get("factory_sha") == self.release_path.name
                and SHA.fullmatch(passport.get("head_sha", ""))
            ):
                authorization = passport["head_sha"]
            remote_status, local_head, remote_head = (
                self.remote_cell_head_status(claim)
            )
        except (
            ControllerError, json.JSONDecodeError, OSError,
            subprocess.SubprocessError, UnicodeError,
        ):
            return False
        expected_marker = {
            "factory_sha": self.release_path.name,
            "schema": EVENT_SCHEMA,
            "ticket": claim["ticket"],
        }
        route_ready = (
            authorization is not None
            and local_head != authorization
            and self.ticket_release_current(claim)
            and self.exact_route_migration_commit(
                claim, authorization, local_head,
            )
        )
        if (
            marker != expected_marker
            or (self.state / f"{completed}.json").exists()
            or remote_status != "pushed"
            or remote_head != local_head
            or authorization is None
            or local_head != authorization and not route_ready
        ):
            return False
        claim.update(
            status="blocked", blocked_reason="route-migration-required",
        )
        claim.pop("recovery_attempt")
        self.save_claim(claim)
        self.event_once(
            "stranded_route_upgrade_readmitted", claim["ticket"],
            authorization_head=authorization,
        )
        return True

    def stranded_route_upgrade_wait(self, claim: dict[str, Any]) -> bool:
        attempt = claim.get("recovery_attempt")
        return (
            self.valid_recovery_attempt(attempt)
            and attempt["factory_sha"] == self.release_path.name
            and attempt["phase"] == "abandoned"
            and attempt["recovery"] == "release-upgrade"
            and attempt["retry_reason"] == "route-migration-required"
            and attempt["retry_status"] == "blocked"
            and claim.get("status") == "blocked"
            and claim.get("blocked_reason")
            == "recovery-abandoned:release-upgrade"
            and (
                claim.get("lease_released") is True
                or claim.get("parked") is True
                and claim.get("lease", "") == ""
            )
            and not claim.get("publication_lease")
        )

    def recover_each(
        self,
        claims: list[dict[str, Any]],
        recovery: Any,
        name: str,
        concurrent: bool = False,
    ) -> None:
        def recover(claim: dict[str, Any]) -> None:
            if (
                self.role_active(claim)
                or claim["ticket"] in self.invalid_transition_tickets
            ):
                return
            before_lease = claim.get("lease", "")
            before_released = claim.get("lease_released") is True
            prepared: dict[str, Any] = {}
            prior_attempt: dict[str, Any] | None = None
            context_set = False
            error_detail = ""
            waiting_receipt = ""
            try:
                if self.recovery_blocked(claim, name):
                    return
                prior = claim.get("recovery_attempt", {})
                if prior:
                    prior_attempt = dict(prior)
                prepared = {
                    "count": prior.get("count", 0),
                    "factory_sha": self.release_path.name,
                    "input_sha256": self.recovery_input_sha256(claim, name),
                    "outcome_sha256": prior.get("outcome_sha256", ""),
                    "phase": "pending",
                    "recovery": name,
                    "retry_reason": prior.get(
                        "retry_reason", claim.get("blocked_reason") or ""
                    ),
                    "retry_status": prior.get(
                        "retry_status", claim.get("status", "blocked")
                    ),
                }
                self.recovery_context.value = {
                    "attempt": prepared,
                    "prior_attempt": prior_attempt,
                    "ticket": claim["ticket"],
                }
                context_set = True
                recovery([claim])
            except ExternalUnavailable:
                prepared = {}
                self.recovery_context.value["attempt"] = prior_attempt
                claim["status"] = "blocked"
                claim["blocked_reason"] = "external-unavailable"
                self.save_claim(claim)
                self.event_once(
                    "external_service_wait", claim["ticket"],
                    reason_code="external_unavailable",
                )
            except (
                ControllerError,
                json.JSONDecodeError,
                OSError,
                subprocess.SubprocessError,
                UnicodeError,
            ) as error:
                error_detail = safe_error(error)
                claim["status"] = "blocked"
                claim["blocked_reason"] = f"recovery:{name}"
                self.save_claim(claim)
                self.event_once(
                    "ticket_recovery_failed",
                    claim["ticket"],
                    error=error_detail,
                    recovery=name,
                )
            finally:
                if context_set:
                    waiting_receipt = self.recovery_context.value.get(
                        "waiting_receipt_sha256", ""
                    )
                    self.recovery_context.value = None
            if prepared and error_detail and claim.get("recovery_attempt") == prepared:
                try:
                    prepared["input_sha256"] = self.recovery_input_sha256(
                        claim, name,
                    )
                except (
                    ControllerError, json.JSONDecodeError, OSError,
                    subprocess.SubprocessError, UnicodeError,
                ):
                    pass
                else:
                    claim["recovery_attempt"] = dict(prepared)
                    self.save_claim(claim)
            waiting = False
            if waiting_receipt and not error_detail:
                try:
                    current = self.operator_transition(claim)
                    waiting = (
                        claim.get("receipt") == waiting_receipt
                        and current is not None
                        and current.get("receipt_sha256") == waiting_receipt
                        and current.get("consumed") is False
                    )
                except (
                    ControllerError, json.JSONDecodeError, OSError,
                    subprocess.SubprocessError, UnicodeError,
                ):
                    waiting = False
                if not waiting:
                    claim["recovery_attempt"] = dict(prepared)
                    try:
                        self.save_claim(claim)
                    except (
                        ControllerError, json.JSONDecodeError, OSError,
                        subprocess.SubprocessError, UnicodeError,
                    ):
                        pass
            attempted = (
                not waiting and bool(prepared)
                and claim.get("recovery_attempt") == prepared
            )
            if attempted and claim.get("status") in {"blocked", "budget", "waiting"}:
                try:
                    self.settle_recovery_attempt(claim, error_detail)
                except (
                    ControllerError, json.JSONDecodeError, OSError,
                    subprocess.SubprocessError, UnicodeError,
                ):
                    self.event_once(
                        "ticket_recovery_settlement_waiting", claim["ticket"],
                        recovery=name,
                    )
            acquired = (
                DIGEST.fullmatch(claim.get("lease", ""))
                and claim.get("lease_released") is not True
                and (
                    before_released
                    or claim.get("lease") != before_lease
                )
            )
            if (
                acquired
                and not self.role_active(claim)
                and claim.get("status") in {"blocked", "budget", "waiting"}
            ):
                try:
                    self.release_ticket_lease(claim)
                except (
                    ControllerError, json.JSONDecodeError, OSError,
                    subprocess.SubprocessError, UnicodeError,
                ):
                    self.event_once(
                        "ticket_recovery_lease_release_waiting", claim["ticket"],
                        recovery=name,
                    )

        if concurrent and len(claims) > 1:
            with ThreadPoolExecutor(
                max_workers=min(self.capacity, len(claims))
            ) as executor:
                list(executor.map(recover, claims))
        else:
            for claim in claims:
                recover(claim)

    def preflight_refusal_events(
        self, ticket: str, receipt: str,
    ) -> set[str]:
        matches = set()
        for path in self.events.glob("*.json"):
            value = read(path)
            digest = value.get("event_sha256", "")
            unsigned = dict(value)
            unsigned.pop("event_sha256", None)
            if digest != hashlib.sha256(canonical(unsigned).encode()).hexdigest():
                raise ControllerError("controller event evidence is invalid")
            if (
                value.get("schema") == EVENT_SCHEMA
                and value.get("event") == "preflight_refused"
                and value.get("factory_sha") == self.release_path.name
                and value.get("ticket") == ticket
                and value.get("transition_receipt_sha256") == receipt
                and (
                    not self.qualification
                    or (
                        value.get("qualification_generation")
                        == self.qualification["generation"]
                        and value.get("qualification_manifest_sha256")
                        == self.qualification_manifest_sha256
                    )
                )
            ):
                matches.add(digest)
        return matches

    def preflight_correction_valid(
        self, claim: dict[str, Any], source: str, target: str,
        receipt: str, events: set[str],
    ) -> bool:
        relative = f"factory/tickets/{claim['ticket']}.md"

        def git(*arguments: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                ["git", "-C", claim["worktree"], *arguments],
                text=True, capture_output=True, check=False, timeout=120,
            )

        ancestry = git("rev-list", "--parents", "-n", "1", target)
        changed = git("diff", "--name-only", f"{source}..{target}")
        before = git("show", f"{source}:{relative}")
        after = git("show", f"{target}:{relative}")
        if (
            any(item.returncode for item in (ancestry, changed, before, after))
            or ancestry.stdout.split() != [target, source]
            or changed.stdout.splitlines() != [relative]
        ):
            return False
        expected = before.stdout
        changed_field = False
        for name in PREFLIGHT_CORRECTION_FIELDS:
            pattern = re.compile(
                rf"^{re.escape(name)}:[^\r\n]*$", re.IGNORECASE | re.MULTILINE,
            )
            old = pattern.findall(before.stdout)
            new = pattern.findall(after.stdout)
            if len(old) != 1 or len(new) != 1:
                return False
            if old != new:
                expected = pattern.sub(new[0], expected, count=1)
                changed_field = True
        receipt_pattern = re.compile(
            r"^OPERATOR PREFLIGHT RECEIPT: ([0-9a-f]{64})$", re.MULTILINE,
        )
        event_pattern = re.compile(
            r"^OPERATOR PREFLIGHT FAILURE EVENT: ([0-9a-f]{64})$", re.MULTILINE,
        )
        old_receipts = receipt_pattern.findall(before.stdout)
        old_events = event_pattern.findall(before.stdout)
        new_receipts = receipt_pattern.findall(after.stdout)
        new_events = event_pattern.findall(after.stdout)
        if (
            len(old_receipts) not in {0, 1}
            or len(old_events) != len(old_receipts)
            or new_receipts != [receipt]
            or len(new_events) != 1
            or new_events[0] not in events
            or not changed_field
        ):
            return False
        if old_receipts:
            expected = receipt_pattern.sub(
                f"OPERATOR PREFLIGHT RECEIPT: {receipt}", expected, count=1,
            )
            expected = event_pattern.sub(
                f"OPERATOR PREFLIGHT FAILURE EVENT: {new_events[0]}",
                expected, count=1,
            )
        else:
            expected = (
                expected.rstrip("\n")
                + f"\n\nOPERATOR PREFLIGHT RECEIPT: {receipt}\n"
                + "OPERATOR PREFLIGHT FAILURE EVENT: "
                + f"{new_events[0]}\n"
            )
        if after.stdout != expected:
            return False
        readiness = subprocess.run(
            [
                sys.executable, "-I", "-S",
                str(self.release_path / "scripts/ticket-readiness.py"),
                "--ticket", claim["ticket"], "--workdir", claim["worktree"],
            ],
            text=True, capture_output=True, check=False, timeout=120,
        )
        return (
            readiness.returncode == 0
            and readiness.stdout.strip() == "READINESS PASS"
        )

    def recover_passport_preflight_blocks(
        self, claims: list[dict[str, Any]],
    ) -> None:
        for claim in claims:
            if (
                claim.get("status") != "blocked"
                or claim.get("blocked_reason") != "preflight"
                or claim.get("publication_lease")
                or claim.get("lease_released") is not True
                or claim.get("receipt", "") not in {""} and not DIGEST.fullmatch(
                    claim.get("receipt", "")
                )
                or claim.get("role", "") not in {"", "planner"}
                or self.role_active(claim)
            ):
                continue
            try:
                transition = self.operator_transition(claim)
                passport = self.authenticated_operator_passport(claim["ticket"])
                if transition is None or passport is None:
                    continue
                receipt = transition.get("receipt_sha256", "")
                source = transition.get("head_sha", "")
                passport_path = (
                    self.state / "passports" / f"{claim['ticket']}.json"
                )
                events = self.preflight_refusal_events(claim["ticket"], receipt)
                failure_receipt = receipt
                successor_issued = False
                parent = transition.get("parent_digest", "")
                if not events and DIGEST.fullmatch(parent):
                    parent_events = self.preflight_refusal_events(
                        claim["ticket"], parent,
                    )
                    if (
                        parent_events
                        and claim.get("receipt", "") in {parent, receipt}
                    ):
                        events = parent_events
                        failure_receipt = parent
                        successor_issued = True
                if (
                    transition.get("stage") != "RUN planner"
                    or transition.get("role") != "planner"
                    or transition.get("consumed") is not False
                    or not DIGEST.fullmatch(receipt)
                    or not SHA.fullmatch(source)
                    or not DIGEST.fullmatch(
                        transition.get("passport_sha256", "")
                    )
                    or passport.get("branch") != claim.get("branch")
                    or passport.get("factory_sha") != self.release_path.name
                    or not passport_head_lineage(passport, source)
                    or not events
                    or claim.get("receipt", "")
                    not in {"", failure_receipt, receipt}
                    or claim.get("role", "") not in {"", "planner"}
                ):
                    continue
                if passport.get("head_sha") == source:
                    if transition["passport_sha256"] != hashlib.sha256(
                        passport_path.read_bytes()
                    ).hexdigest():
                        continue
                else:
                    edges = [
                        item for item in passport.get("migration_history", [])
                        if valid_v2_migration(item)
                        and item["from_head_sha"] == source
                        and item["from_passport_file_sha256"]
                        == transition["passport_sha256"]
                        and item["from_route_plan_sha256"]
                        == transition.get("route_plan_sha256")
                    ]
                    if len(edges) != 1:
                        continue
                if not claim.get("receipt") or successor_issued:
                    claim.update(receipt=receipt, role="planner")
                    self.save_claim(claim)
                head_status, local_head, _remote_head = (
                    self.remote_cell_head_status(claim)
                )
                if head_status != "pushed":
                    continue
                if not successor_issued and local_head == source:
                    self.wait_for_recovery_receipt(claim)
                    continue
                if (
                    not successor_issued
                    and not self.preflight_correction_valid(
                        claim, source, local_head, receipt, events,
                    )
                ):
                    self.event_once(
                        "passport_preflight_correction_refused",
                        claim["ticket"], correction_head=local_head,
                        transition_receipt_sha256=receipt,
                    )
                    self.wait_for_recovery_receipt(claim)
                    continue
                if successor_issued and (
                    local_head != source or not self.remote_passport_valid(claim)
                ):
                    continue
                self.ensure_lease(claim, "preflight-retry")
                head_status, checked_head, _remote_head = (
                    self.remote_cell_head_status(claim)
                )
                if head_status != "pushed" or checked_head != local_head:
                    self.release_ticket_lease(claim)
                    continue
                predecessor = receipt
                result = self.json_call(
                    "state-machine", "--ticket", claim["ticket"],
                    "--lease", claim["lease"], "--workdir", claim["worktree"],
                    "--expected-head", local_head, "--json", timeout=None,
                )
                current = self.operator_transition(claim)
                if (
                    not valid_transition_evidence(result, claim["ticket"])
                    or result.get("role") is None
                    or current is None
                    or current.get("receipt_sha256") != result.get("receipt")
                    or current.get("stage") != result.get("stage")
                    or current.get("role") != result.get("role")
                    or (
                        current.get("receipt_sha256") == predecessor
                        and current.get("parent_digest") != failure_receipt
                    )
                    or (
                        current.get("receipt_sha256") != predecessor
                        and current.get("parent_digest") != predecessor
                    )
                    or current.get("head_sha") != local_head
                    or current.get("consumed") is not False
                    or current.get("lease_sha256")
                    != hashlib.sha256(claim["lease"].encode()).hexdigest()
                    or not self.remote_passport_valid(claim)
                ):
                    raise ControllerError(
                        "passport preflight successor is invalid"
                    )
                role = result["role"]
                claim.update(receipt=current["receipt_sha256"], role=role)
                self.save_claim(claim)
                preflight = self.json_call(
                    "preflight", "--ticket", claim["ticket"],
                    "--role", role, "--lease", claim["lease"],
                    "--receipt", claim["receipt"], "--workdir",
                    claim["worktree"], "--json", allow=(0, 1),
                )
                if (
                    preflight.get("status") != "ok"
                    or preflight.get("exit_code") != 0
                ):
                    evidence = self.preflight_refusal_evidence(preflight)
                    self.event(
                        "preflight_refused", claim["ticket"], **evidence,
                        transition_receipt_sha256=claim["receipt"],
                    )
                    self.release_ticket_lease(claim)
                    self.wait_for_recovery_receipt(claim)
                    continue
            except (
                ControllerError, json.JSONDecodeError, OSError,
                subprocess.SubprocessError, UnicodeError,
            ):
                if (
                    DIGEST.fullmatch(claim.get("lease", ""))
                    and claim.get("lease_released") is not True
                    and not self.role_active(claim)
                ):
                    self.release_ticket_lease(claim)
                continue
            claim.update(receipt="", role="", status="claimed")
            claim.pop("blocked_reason", None)
            claim.pop("lease_released", None)
            self.save_claim(claim)
            self.event_once(
                "passport_preflight_recovered", claim["ticket"],
                correction_head=local_head,
                refused_receipt_sha256=failure_receipt,
                transition_receipt_sha256=current["receipt_sha256"],
            )

    def recover_preflight_blocks(self, claims: list[dict[str, Any]]) -> None:
        for claim in claims:
            receipt_path = self.state / f"{claim['ticket']}.json"
            passport_path = self.state / "passports" / f"{claim['ticket']}.json"
            worker_error = claim.get("blocked_reason") == "worker-error"
            if (
                (
                    self.qualification is not None
                    and claim["ticket"] not in self.qualification["tickets"]
                )
                or claim["status"] != "blocked"
                or claim.get("blocked_reason") not in {"preflight", "worker-error"}
                or claim.get("receipt")
                or claim.get("role")
                or claim.get("publication_lease")
                or (
                    worker_error
                    and not DIGEST.fullmatch(claim.get("lease", ""))
                )
                or (
                    not worker_error
                    and claim.get("lease_released") is not True
                )
                or self.role_active(claim)
                or passport_path.exists()
                or passport_path.is_symlink()
                or not receipt_path.is_file()
                or receipt_path.is_symlink()
            ):
                continue
            receipt = self.operator_transition(claim)
            if receipt is None:
                continue
            if (
                receipt.get("schema")
                != "nysa.software-factory.transition-receipt/v1"
                or receipt.get("ticket") != claim["ticket"]
                or receipt.get("branch") != claim["branch"]
                or receipt.get("stage") != "RUN planner"
                or receipt.get("role") != "planner"
                or receipt.get("consumed") is not False
                or not DIGEST.fullmatch(receipt.get("receipt_sha256", ""))
                or worker_error
                and not self.exact_passportless_planner_receipt(claim, receipt)
                or any(
                    fields(path).get("ticket") == claim["ticket"]
                    for path in (self.product / "factory/runs").glob("*.meta")
                )
                or subprocess.run(
                    [
                        "git", "-C", claim["worktree"], "status",
                        "--porcelain=v1", "-z",
                    ],
                    text=True, capture_output=True, check=True, timeout=120,
                ).stdout
                or not self.ticket_release_current(claim)
                or not self.remote_cell_head_valid(claim)
            ):
                continue
            self.ensure_lease(claim, "preflight-retry")
            try:
                try:
                    if self.qualification:
                        ensure_qualification_artifacts(
                            self.product, self.state, claim["ticket"]
                        )
                except QualificationArtifactError as error:
                    raise ControllerError(str(error)) from error
                transition = self.json_call(
                    "state-machine", "--ticket", claim["ticket"],
                    "--lease", claim["lease"], "--workdir", claim["worktree"],
                    "--json", timeout=None,
                )
                if (
                    not valid_transition_evidence(transition, claim["ticket"])
                    or transition.get("role") != "planner"
                ):
                    raise ControllerError(
                        "preflight retry transition evidence is invalid"
                    )
                preflight = self.json_call(
                    "preflight", "--ticket", claim["ticket"],
                    "--role", "planner", "--lease", claim["lease"],
                    "--receipt", transition["receipt"],
                    "--workdir", claim["worktree"], "--json", allow=(0, 1),
                )
            except (
                ControllerError, json.JSONDecodeError, OSError,
                subprocess.SubprocessError,
            ):
                self.release_ticket_lease(claim)
                raise
            if preflight.get("status") != "ok" or preflight.get("exit_code") != 0:
                try:
                    evidence = self.preflight_refusal_evidence(preflight)
                except ControllerError as error:
                    self.event(
                        "preflight_refusal_invalid", claim["ticket"], error=str(error),
                        transition_receipt_sha256=transition["receipt"],
                    )
                    self.release_ticket_lease(claim)
                    claim["blocked_reason"] = "preflight-evidence"
                    self.save_claim(claim)
                    continue
                self.event(
                    "preflight_retry_blocked", claim["ticket"], **evidence,
                    transition_receipt_sha256=transition["receipt"],
                )
                self.release_ticket_lease(claim)
                if worker_error:
                    claim["blocked_reason"] = "preflight"
                    self.save_claim(claim)
                continue
            claim.update(receipt="", role="", status="claimed")
            claim.pop("blocked_reason", None)
            self.save_claim(claim)
            self.event(
                (
                    "preflight_worker_error_recovered"
                    if worker_error else "preflight_retry_recovered"
                ),
                claim["ticket"],
                transition_receipt_sha256=transition["receipt"],
            )

    def recover_passportless_route_migrations(
        self, claims: list[dict[str, Any]],
    ) -> None:
        for claim in claims:
            passport = self.state / "passports" / f"{claim['ticket']}.json"
            if (
                claim.get("status") != "blocked"
                or claim.get("blocked_reason") not in {
                    "state-machine-refusal",
                    "model-identity-delivery-retry:" + self.release_path.name,
                }
                or claim.get("receipt")
                or claim.get("role")
                or claim.get("publication_lease")
                or claim.get("lease_released") not in {None, True}
                or self.role_active(claim)
                or passport.is_symlink()
            ):
                continue
            receipt = self.operator_transition(claim)
            if receipt is None or not self.exact_passportless_route_migration_refusal(
                claim, receipt,
            ):
                continue
            failed_receipt = receipt.get("parent_digest", "")
            terminal = (
                self.terminal_for_receipt(claim["ticket"], failed_receipt)
                if DIGEST.fullmatch(failed_receipt) else None
            )
            if self.direct_model_identity_candidate(
                claim, terminal, failed_receipt,
            ):
                try:
                    self.recover_direct_model_identity_success(
                        claim, terminal, failed_receipt,
                    )
                except ModelIdentityEvidenceError as error:
                    reason = "model-identity-recovery-refused:" + self.release_path.name
                    self.block(claim, reason)
                    self.release_ticket_lease(claim)
                    self.event_once(
                        "typed_recovery_refused", claim["ticket"],
                        recovery_kind="model_identity_success",
                        reason=safe_error(str(error)),
                    )
                except (ControllerError, OSError, subprocess.SubprocessError) as error:
                    self.block(
                        claim,
                        "model-identity-delivery-retry:" + self.release_path.name,
                    )
                    self.release_ticket_lease(claim)
                    self.event_once(
                        "typed_recovery_refused", claim["ticket"],
                        recovery_kind="model_identity_delivery",
                        reason=safe_error(str(error)),
                    )
                continue
            if passport.exists():
                continue
            self.ensure_lease(claim, "passportless-route-migration")
            claim.update(receipt="", role="", status="claimed")
            claim.pop("blocked_reason", None)
            self.save_claim(claim)
            self.event_once(
                "passportless_route_migration_recovered", claim["ticket"],
                refused_receipt_sha256=receipt["receipt_sha256"],
            )

    def quarantine_legacy_protected_mutation(
        self, claim: dict[str, Any], terminal: dict[str, str]
    ) -> bool:
        if (
            terminal.get("role_exit") != "role_exit_protected_ticket_mutation"
            or terminal.get("kit_sha") == self.release_path.name
        ):
            return False
        passport = read(
            self.state / "passports" / f"{claim['ticket']}.json"
        )
        input_head = terminal.get("role_head_before", "")
        output_head = passport.get("head_sha", "")
        expected = (
            terminal.get("run_id"), claim.get("role"), claim.get("receipt"),
        )
        completed = [
            (
                item.get("run_id"), item.get("role"),
                item.get("transition_receipt_sha256"),
            )
            for item in passport.get("completed_role_evidence", [])
        ]
        if output_head == input_head:
            return False
        if (
            claim.get("status") != "blocked"
            or claim.get("role") not in {
                "planner", "spec-linter", "builder", "narrator",
            }
            or terminal.get("role") != claim.get("role")
            or terminal.get("phase") != "completed"
            or terminal.get("accounting_state") != "abandoned_conservative"
            or terminal.get("go_issued") != "1"
            or terminal.get("task_submitted") != "1"
            or terminal.get("exit_status") != "11"
            or terminal.get("cost_basis") != "conservative_reservation"
            or terminal.get("effective_cost") != terminal.get("reserved_usd")
            or terminal.get("role_branch_before") != claim.get("branch")
            or terminal.get("role_remote_before") != input_head
            or passport.get("ticket") != claim.get("ticket")
            or passport.get("branch") != claim.get("branch")
            or passport.get("factory_sha") != terminal.get("kit_sha")
            or not SHA.fullmatch(input_head)
            or not SHA.fullmatch(output_head)
            or not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}",
                terminal.get("run_id", ""),
            )
            or completed.count(expected) != 0
            or not self.terminal_already_exported(claim, terminal)
        ):
            raise ControllerError(
                "protected ticket mutation recovery evidence is invalid"
            )
        worktree = claim["worktree"]
        diagnostic = (
            f"refs/factory/failed-role/{claim['ticket']}/{terminal['run_id']}"
        )

        def git(*arguments: str, check: bool = True) -> subprocess.CompletedProcess:
            return subprocess.run(
                ["git", "-C", worktree, *arguments], text=True,
                capture_output=True, check=check, timeout=120,
            )

        with self.git_lock:
            branch = git(
                "symbolic-ref", "--quiet", "--short", "HEAD"
            ).stdout.strip()
            head = git("rev-parse", "HEAD").stdout.strip()
            remote = git(
                "ls-remote", "--exit-code", "origin",
                f"refs/heads/{claim['branch']}", check=False,
            )
            ancestor = git(
                "merge-base", "--is-ancestor", input_head, output_head,
                check=False,
            )
            existing = git("rev-parse", diagnostic, check=False)
            if (
                branch != claim["branch"]
                or head != output_head
                or git("status", "--porcelain=v1", "-z").stdout
                or remote.returncode != 0
                or remote.stdout
                != f"{input_head}\trefs/heads/{claim['branch']}\n"
                or ancestor.returncode != 0
                or existing.returncode not in {0, 128}
                or (
                    existing.returncode == 0
                    and existing.stdout.strip() != output_head
                )
            ):
                raise ControllerError(
                    "protected ticket mutation recovery topology is invalid"
                )
            if existing.returncode == 128:
                git("update-ref", diagnostic, output_head, "0" * 40)
            git(
                "update-ref", f"refs/heads/{claim['branch']}",
                input_head, output_head,
            )
            git(
                "restore", "--source", input_head, "--staged", "--worktree",
                "--", ".",
            )
            remote = git(
                "ls-remote", "--exit-code", "origin",
                f"refs/heads/{claim['branch']}", check=False,
            )
            if (
                git("rev-parse", "HEAD").stdout.strip() != input_head
                or git("rev-parse", diagnostic).stdout.strip() != output_head
                or git("status", "--porcelain=v1", "-z").stdout
                or remote.returncode != 0
                or remote.stdout
                != f"{input_head}\trefs/heads/{claim['branch']}\n"
            ):
                raise ControllerError(
                    "protected ticket mutation recovery could not restore the input"
                )
        self.event(
            "protected_ticket_mutation_quarantined", claim["ticket"],
            failed_run_id=terminal["run_id"],
        )
        return True

    def recover_upgraded_claims(self, claims: list[dict[str, Any]]) -> None:
        for claim in claims:
            successor_budget = (
                claim["status"] == "budget"
                and self.qualification is not None
                and self.qualification.get("mode") == "successor"
            )
            if (
                claim["status"] not in {"blocked", "claimed", "waiting"}
                and not successor_budget
            ):
                continue
            path = self.state / "passports" / f"{claim['ticket']}.json"
            if not path.exists():
                continue
            if claim.get("parked") is True:
                branch = subprocess.run(
                    [
                        "git", "-C", claim["worktree"], "symbolic-ref",
                        "--quiet", "--short", "HEAD",
                    ],
                    text=True, capture_output=True, check=False, timeout=120,
                )
                if branch.returncode != 0 or branch.stdout.strip() != claim["branch"]:
                    if (
                        DIGEST.fullmatch(claim.get("lease", ""))
                        and claim.get("lease_released") is not True
                        and not self.role_active(claim)
                    ):
                        self.release_ticket_lease(claim)
                    self.event_once(
                        "release_upgrade_waiting", claim["ticket"],
                        reason=(
                            "detached_worktree"
                            if branch.returncode == 1 else "worktree_branch_invalid"
                        ),
                    )
                    continue
            terminal = (
                self.terminal_for_receipt(claim["ticket"], claim["receipt"])
                if claim.get("receipt")
                else None
            )
            passport = read(path)
            migrated_authorization = None
            if terminal is not None:
                try:
                    authenticated = self.authenticated_operator_passport(
                        claim["ticket"]
                    )
                    migrated_authorization = (
                        authenticated is not None
                        and self.migrated_stranded_semantic_authorization(
                            claim, terminal, authenticated,
                        )
                    )
                except (ControllerError, OSError, subprocess.SubprocessError):
                    pass
                if (
                    not migrated_authorization
                    and passport.get("factory_sha") == terminal.get("kit_sha")
                ):
                    self.quarantine_legacy_protected_mutation(claim, terminal)
            if (
                migrated_authorization
                and claim.get("blocked_reason") == "recovery:release-upgrade"
            ):
                claim["blocked_reason"] = "route-migration-required"
                self.save_claim(claim)
            prior = passport.get("factory_sha", "")
            if not SHA.fullmatch(prior):
                raise ControllerError("blocked ticket passport has an invalid release")
            pending = (
                f"passport-route-migration-pending-{claim['ticket']}-"
                f"{self.release_path.name}"
            )
            completed = (
                f"passport-route-migration-complete-{claim['ticket']}-"
                f"{self.release_path.name}"
            )
            migration_complete = (
                prior == self.release_path.name and self.marker(completed)
            )
            completion_recovery = (
                migration_complete
                and not self.marker(pending)
                and claim.get("blocked_reason") == "route-migration-required"
            )
            if completion_recovery:
                try:
                    completed_marker = read(self.state / f"{completed}.json")
                    authenticated = self.authenticated_operator_passport(
                        claim["ticket"]
                    )
                except (ControllerError, OSError, json.JSONDecodeError):
                    raise ControllerError("route migration completion is invalid") from None
                if (
                    completed_marker != {
                        "factory_sha": self.release_path.name,
                        "schema": EVENT_SCHEMA,
                        "ticket": claim["ticket"],
                    }
                    or authenticated != passport
                    or authenticated.get("factory_sha") != self.release_path.name
                ):
                    raise ControllerError("route migration completion is invalid")
            merged_closeout_candidate = (
                passport.get("current_state") == "Approved"
                and passport.get("publication_state") == "merged"
                and not claim.get("receipt")
                and not claim.get("role")
                and not claim.get("publication_lease")
                and not self.role_active(claim)
            )
            if migrated_authorization and self.marker(pending):
                self.event_once(
                    "semantic_round_authorization_imported_by_release_upgrade",
                    claim["ticket"], head_sha=migrated_authorization,
                    role="spec-linter", semantic_round=3,
                )
            if (
                prior == self.release_path.name
                and (
                    not self.marker(pending) and not completion_recovery
                    or migration_complete
                    and (
                        claim.get("blocked_reason") != "route-migration-required"
                        or not self.ticket_release_current(claim)
                        and not merged_closeout_candidate
                    )
                )
            ):
                continue
            merged_closeout = False
            bundle_refresh = False
            route_passport_pending = (
                prior == self.release_path.name
                and not migration_complete
                and self.ticket_release_current(claim)
            )
            if prior == self.release_path.name and not route_passport_pending:
                if (
                    merged_closeout_candidate
                    and self.ticket_merged(claim)
                ):
                    validation = self.json_call(
                        "passport", "validate", "--ticket", claim["ticket"],
                        "--workdir", claim["worktree"], "--json",
                    )
                    merged_closeout = (
                        validation.get("status") == "ok"
                        and validation.get("passport")
                        == passport.get("passport_sha256")
                    )
                else:
                    bundle_refresh = self.release_bundle_refreshable(
                        claim, passport
                    )
                    if (
                        claim.get("release_refresh_required") is True
                        and not bundle_refresh
                    ):
                        claim.pop("release_refresh_required", None)
                        self.save_claim(claim)
            if (
                migrated_authorization
                and prior == self.release_path.name
                and self.marker(pending)
                and not migration_complete
            ):
                self.migrate_stranded_route_upgrade(
                    claim, migrated_authorization, pending,
                )
            if (
                not self.ticket_release_current(claim)
                and not merged_closeout
                and not bundle_refresh
            ):
                if prior != self.release_path.name:
                    semantic_target = None
                    local_head = subprocess.run(
                        ["git", "-C", claim["worktree"], "rev-parse", "HEAD"],
                        text=True, capture_output=True, check=False, timeout=120,
                    ).stdout.strip()
                    if (
                        terminal is not None
                        and terminal.get("role_exit")
                        == "role_exit_protected_ticket_mutation"
                        and local_head != passport.get("head_sha")
                    ):
                        authenticated = self.authenticated_operator_passport(
                            claim["ticket"]
                        )
                        if authenticated is None:
                            continue
                        semantic_target = (
                            self.exact_stranded_semantic_authorization(
                                claim, terminal, authenticated,
                            )
                        )
                        if semantic_target is None:
                            continue
                    created = self.marker(pending, {
                        "factory_sha": self.release_path.name,
                        "schema": EVENT_SCHEMA,
                        "ticket": claim["ticket"],
                    })
                    self.migrate_passport(claim, "preserve")
                    if semantic_target is not None:
                        migrated = self.authenticated_operator_passport(
                            claim["ticket"]
                        )
                        if (
                            migrated is None
                            or not self.semantic_import_migration(
                                passport, migrated, semantic_target,
                                self.release_path.name,
                            )
                            or not passport_head_lineage(
                                migrated, passport.get("head_sha", ""),
                            )
                            or not self.remote_passport_valid(claim)
                        ):
                            raise ControllerError(
                                "stranded semantic authorization migration "
                                "is invalid"
                            )
                        self.event_once(
                            "semantic_round_authorization_imported_by_release_upgrade",
                            claim["ticket"], head_sha=semantic_target,
                            role="spec-linter", semantic_round=3,
                        )
                    if created:
                        self.event(
                            "passport_migrated_awaiting_route", claim["ticket"],
                            from_factory_sha=prior,
                        )
                claim["status"] = "blocked"
                claim["blocked_reason"] = "route-migration-required"
                context = getattr(self.recovery_context, "value", None)
                if context and context.get("ticket") == claim["ticket"]:
                    context["attempt"].update(
                        retry_reason="route-migration-required",
                        retry_status="blocked",
                    )
                self.save_claim(claim)
                marker = (
                    f"route-migration-required-{claim['ticket']}-"
                    f"{self.release_path.name}"
                )
                if self.marker(marker, {
                    "factory_sha": self.release_path.name,
                    "schema": EVENT_SCHEMA,
                    "ticket": claim["ticket"],
                }):
                    self.event(
                        "route_migration_required", claim["ticket"],
                        from_factory_sha=prior,
                    )
                continue
            try:
                self.renew(claim)
            except ControllerError:
                self.release_expired_successor_lease(claim)
                lease = self.json_call("claim", "--ticket", claim["ticket"])
                if (
                    lease.get("schema_version") != 1
                    or lease.get("ticket") != claim["ticket"]
                    or not DIGEST.fullmatch(lease.get("lease_id", ""))
                ):
                    raise ControllerError("upgraded ticket lease is invalid")
                claim["lease"] = lease["lease_id"]
            claim.pop("lease_released", None)
            self.save_claim(claim)
            if (
                not migration_complete
                and not merged_closeout
                and not bundle_refresh
            ):
                try:
                    self.migrate_passport(claim, "preserve")
                except ControllerError:
                    self.release_ticket_lease(claim)
                    raise
                passport = read(path)
                if not (
                    claim.get("release_refresh_required") is True
                    and self.bundle_refresh_handoff_pending(
                        claim, rotated_lease=True,
                    )
                ):
                    bundle_refresh = self.release_bundle_refreshable(
                        claim, passport,
                    )
            if merged_closeout:
                claim.update(receipt="", role="", status="claimed")
                if claim.get("blocked_reason") == "route-migration-required":
                    claim.pop("blocked_reason")
                self.save_claim(claim)
                self.event(
                    "upgraded_merged_claim_recovered", claim["ticket"],
                )
                if migration_complete:
                    self.event_once("route_migration_cleared", claim["ticket"])
                self.marker(completed, {
                    "factory_sha": self.release_path.name,
                    "schema": EVENT_SCHEMA,
                    "ticket": claim["ticket"],
                })
                continue
            if bundle_refresh:
                refreshed = self.refresh_prior_release_receipt(claim)
                if refreshed:
                    self.prior_transition_tickets.discard(claim["ticket"])
                    self.event_once(
                        "prior_release_receipt_refreshed", claim["ticket"],
                        transition_receipt_sha256=refreshed,
                    )
                claim.update(
                    receipt="", role="", status="claimed",
                    release_refresh_required=True,
                )
                claim.pop("blocked_reason", None)
                self.save_claim(claim)
                self.event(
                    "upgraded_bundle_refresh_recovered", claim["ticket"],
                )
                if migration_complete:
                    self.event_once("route_migration_cleared", claim["ticket"])
                continue
            if (
                not claim.get("receipt")
                and self.restore_contract_blocker(claim)
            ):
                claim.pop("blocked_reason", None)
                self.save_claim(claim)
                self.event(
                    "upgraded_claim_recovered", claim["ticket"],
                    from_factory_sha=prior,
                )
                if migration_complete:
                    self.event_once("route_migration_cleared", claim["ticket"])
                self.marker(completed, {
                    "factory_sha": self.release_path.name,
                    "schema": EVENT_SCHEMA,
                    "ticket": claim["ticket"],
                })
                continue
            if (
                claim.get("receipt")
                and not migrated_authorization
                and terminal is not None
                and terminal.get("role_exit")
                == "role_exit_protected_ticket_mutation"
            ):
                self.recover_repaired_failures([claim])
            terminal = (
                self.terminal_for_receipt(claim["ticket"], claim["receipt"])
                if claim.get("receipt")
                else None
            )
            if terminal is not None:
                prior_release_launch_void = (
                    self.typed_launch_void(terminal)
                    and SHA.fullmatch(terminal.get("kit_sha", ""))
                    and terminal["kit_sha"] != self.release_path.name
                )
                if prior_release_launch_void:
                    self.prior_transition_tickets.discard(claim["ticket"])
                claim["status"] = (
                    "running"
                    if (
                        prior_release_launch_void
                        or (
                            self.qualification
                            and terminal.get("role_exit") == "provider_failed"
                            and terminal.get("route_id", "").startswith("cursor-")
                        )
                    )
                    else "blocked"
                )
            else:
                claim.update(receipt="", role="", status="claimed")
                claim.pop("budget_sha256", None)
                self.prior_transition_tickets.discard(claim["ticket"])
            if claim.get("blocked_reason") == "route-migration-required":
                claim.pop("blocked_reason")
            self.save_claim(claim)
            self.event(
                "upgraded_claim_recovered", claim["ticket"],
                from_factory_sha=prior,
            )
            if migration_complete:
                self.event_once("route_migration_cleared", claim["ticket"])
            self.marker(completed, {
                "factory_sha": self.release_path.name,
                "schema": EVENT_SCHEMA,
                "ticket": claim["ticket"],
            })

    def restore_contract_blocker(self, claim: dict[str, Any]) -> bool:
        if (
            claim["status"] != "blocked"
            or claim.get("receipt")
            or claim.get("role")
            or self.role_active(claim)
        ):
            return False
        receipt_path = self.state / f"{claim['ticket']}.json"
        passport_path = self.state / "passports" / f"{claim['ticket']}.json"
        if not receipt_path.exists() or not passport_path.exists():
            return False
        receipt = self.transition_receipt(claim, allow_prior=True)
        if receipt is None:
            return False
        passport = read(passport_path)
        receipt_digest = receipt.get("receipt_sha256", "")
        role = receipt.get("role", "")
        charges = passport.get("charge_records")
        completed = passport.get("completed_role_evidence")
        if (
            receipt.get("schema") != "nysa.software-factory.transition-receipt/v1"
            or receipt.get("ticket") != claim["ticket"]
            or receipt.get("branch") != claim["branch"]
            or not receipt.get("consumed")
            or not DIGEST.fullmatch(receipt_digest)
            or role not in {"planner", "spec-linter", "test-author", "builder"}
            or passport.get("ticket") != claim["ticket"]
            or passport.get("branch") != claim["branch"]
            or passport.get("factory_sha") != self.release_path.name
            or not isinstance(charges, list)
            or not charges
            or not isinstance(completed, list)
        ):
            return False
        charge = charges[-1]
        if (
            not isinstance(charge, dict)
            or charge.get("transition_receipt_sha256") != receipt_digest
            or charge.get("factory_sha") != receipt.get("factory_sha")
            or charge.get("contract_version") != receipt.get("contract_version")
            or charge.get("role") != role
            or charge.get("head_before") != receipt.get("head_sha")
            or any(
                isinstance(item, dict)
                and item.get("transition_receipt_sha256") == receipt_digest
                for item in completed
            )
        ):
            return False
        terminal = self.terminal_for_receipt(claim["ticket"], receipt_digest)
        if (
            terminal is None
            or terminal.get("run_id") != charge.get("run_id")
            or terminal.get("role_exit") != "role_exit_contract_blocked"
            or terminal.get("exit_status") != "12"
            or terminal.get("role") != role
            or terminal.get("kit_sha") != receipt.get("factory_sha")
        ):
            return False
        if not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", terminal["run_id"]
        ):
            return False
        manifest = self.product / "factory/runs" / f"{terminal['run_id']}.meta"
        if (
            not manifest.is_file()
            or manifest.is_symlink()
            or hashlib.sha256(manifest.read_bytes()).hexdigest()
            != charge.get("manifest_sha256")
            or not self.remote_passport_valid(claim)
        ):
            return False
        self.ensure_lease(claim, "restored-contract-blocker")
        claim.update(receipt=receipt_digest, role=role, status="blocked")
        self.save_claim(claim)
        self.event(
            "contract_blocker_claim_restored", claim["ticket"],
            failed_run_id=terminal["run_id"],
        )
        return True

    def restore_recorded_contract_repair(self, claim: dict[str, Any]) -> bool:
        if (
            claim["status"] != "blocked"
            or self.role_active(claim)
        ):
            return False
        repair = self.state / "contract-repairs" / f"{claim['ticket']}.json"
        if not repair.is_file() or repair.is_symlink():
            return False
        recorded = read(repair)
        if (
            (claim.get("receipt") or claim.get("role"))
            and (
                claim.get("receipt") != recorded.get("blocked_receipt")
                or claim.get("role") != recorded.get("blocked_role")
            )
        ):
            return False
        try:
            if not self.remote_passport_valid(claim):
                return False
        except ControllerError:
            return False
        self.ensure_lease(claim, "recorded-contract-repair")
        claim.update(receipt="", role="", status="claimed")
        self.save_claim(claim)
        self.prior_transition_tickets.discard(claim["ticket"])
        self.event("recorded_contract_repair_prepared", claim["ticket"])
        return True

    @staticmethod
    def cell_git(
        claim: dict[str, Any], *arguments: str,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", claim["worktree"], *arguments], text=True,
            capture_output=True, check=False, timeout=120,
        )

    def exact_ticket_commit(
        self, claim: dict[str, Any], before: str, after: str, *,
        authorization: bool = False, authorization_role: str = "spec-linter",
        semantic_round: int = 3, semantic_kind: str = "planner-spec-linter",
    ) -> bool:
        ticket_path = f"factory/tickets/{claim['ticket']}.md"
        parents = self.cell_git(claim, "show", "-s", "--format=%P", after)
        paths = self.cell_git(
            claim, "diff-tree", "--no-commit-id", "--name-status",
            "--no-renames", "-r", after,
        )
        mode = self.cell_git(claim, "ls-tree", after, "--", ticket_path)
        if (
            any(item.returncode for item in (parents, paths, mode))
            or parents.stdout.split() != [before]
            or paths.stdout.splitlines() != [f"M\t{ticket_path}"]
            or mode.stdout.split()[:2] != ["100644", "blob"]
        ):
            return False
        if not authorization:
            return True
        old = self.cell_git(claim, "show", f"{before}:{ticket_path}")
        new = self.cell_git(claim, "show", f"{after}:{ticket_path}")
        authorization_line = (
            f"OPERATOR AUTHORIZATION: {authorization_role} "
            f"round {semantic_round}"
        )
        required = authorization_line + "\n"
        exact_lines = lambda text: [
            line[:-1] if line.endswith("\n") else line
            for line in text.splitlines(keepends=True)
        ]
        try:
            old_epoch = self.epoch_ticket(claim["ticket"], old.stdout)
            new_epoch = self.epoch_ticket(claim["ticket"], new.stdout)
        except ControllerError:
            return False
        old_count = exact_lines(old_epoch).count(authorization_line)
        new_count = exact_lines(new_epoch).count(authorization_line)
        without_grants = lambda text: "".join(
            line for line in text.splitlines(keepends=True)
            if (line[:-1] if line.endswith("\n") else line)
            != authorization_line
        )
        appended = lambda text: {
            text + authorization_line,
            text + required,
            text + "\n" + authorization_line,
            text + "\n" + required,
        }
        old_authorization = planner_spec_linter_authorization(old_epoch)
        new_authorization = planner_spec_linter_authorization(new_epoch)
        normalized = (
            os.environ.get("FACTORY_KIT_TRUST_SCOPE")
            != "qualification-candidate"
            and old_count > 1
            and new_count == 1
            and new.stdout in appended(without_grants(old.stdout))
        )
        return (
            old.returncode == new.returncode == 0
            and (
                semantic_kind != "planner-spec-linter"
                or new_authorization == (semantic_round, "authorized")
                and (
                    old_authorization == (semantic_round, "required")
                    or normalized
                    and old_authorization == (semantic_round, "invalid")
                )
            )
            and new_count == 1
            and (
                old_count == 0
                and new.stdout in appended(old.stdout)
                or normalized
            )
        )

    def semantic_authorization_head(
        self, claim: dict[str, Any], passport: dict[str, Any],
        role: str = "spec-linter", semantic_round: int = 3,
        semantic_kind: str = "planner-spec-linter",
    ) -> tuple[str | None, str]:
        local = self.cell_git(claim, "rev-parse", "HEAD").stdout.strip()
        branch = self.cell_git(
            claim, "symbolic-ref", "--quiet", "--short", "HEAD",
        )
        dirty = self.cell_git(claim, "status", "--porcelain=v1", "-z")
        if dirty.returncode or dirty.stdout:
            return None, "dirty_uncommitted"
        if branch.returncode or branch.stdout.strip() != claim["branch"]:
            return None, "branch_invalid"
        if (
            passport.get("ticket") != claim["ticket"]
            or passport.get("project") != self.project
            or passport.get("contract_version") not in CONTROLLER_CONTRACTS
            or passport.get("branch") != claim["branch"]
            or local == passport.get("head_sha")
            or not self.exact_ticket_commit(
                claim, passport.get("head_sha", ""), local,
                authorization=True, authorization_role=role,
                semantic_round=semantic_round, semantic_kind=semantic_kind,
            )
        ):
            return None, "authorization_content_invalid"
        status, observed_local, remote = self.remote_cell_head_status(claim)
        if observed_local != local:
            return None, ""
        if status == "resume_commit_not_pushed":
            return None, "commit_not_pushed"
        if status == "resume_ancestry_invalid":
            return None, "remote_moved"
        if status != "pushed" or remote != local:
            return None, ""
        return local, ""

    def operator_ticket_change_status(
        self, claim: dict[str, Any], passport: dict[str, Any], after: str,
        exact_commit: Any, label: str,
    ) -> str:
        ticket_path = f"factory/tickets/{claim['ticket']}.md"
        ticket_file = Path(claim["worktree"]) / ticket_path
        try:
            ticket_info = ticket_file.lstat()
        except OSError as error:
            raise ControllerError(f"{label} ticket is unsafe") from error
        if (
            not stat.S_ISREG(ticket_info.st_mode)
            or ticket_info.st_uid != os.geteuid()
            or ticket_info.st_nlink != 1
        ):
            raise ControllerError(f"{label} ticket is unsafe")
        local = self.cell_git(claim, "rev-parse", "HEAD").stdout.strip()
        branch = self.cell_git(
            claim, "symbolic-ref", "--quiet", "--short", "HEAD",
        )
        dirty = self.cell_git(claim, "status", "--porcelain=v1", "-z")
        entries = [item for item in dirty.stdout.split("\0") if item]
        partial = (
            local == passport["head_sha"]
            and len(entries) == 1
            and entries[0][:2] in {" M", "M ", "MM"}
            and entries[0][3:] == ticket_path
            and ticket_file.read_text(encoding="utf-8") == after
        )
        remote_status, observed, remote = self.remote_cell_head_status(claim)
        if (
            branch.returncode
            or branch.stdout.strip() != claim["branch"]
            or dirty.returncode
        ):
            raise ControllerError(f"{label} cell is invalid")
        if local == passport["head_sha"]:
            if entries and not partial:
                raise ControllerError(f"{label} cell is dirty")
            if not (
                remote_status == "pushed"
                and observed == remote == passport["head_sha"]
            ):
                raise ControllerError(f"{label} remote moved")
            return "prepared" if partial else "planned"
        current = self.cell_git(claim, "show", f"{local}:{ticket_path}")
        if (
            entries
            or current.returncode
            or hashlib.sha256(current.stdout.encode()).hexdigest()
            != hashlib.sha256(after.encode()).hexdigest()
            or not exact_commit(passport["head_sha"], local)
        ):
            raise ControllerError(f"{label} commit is invalid")
        if remote_status == "pushed" and observed == remote == local:
            return "applied"
        if (
            remote_status == "resume_commit_not_pushed"
            and observed == local and remote == passport["head_sha"]
        ):
            return "commit_not_pushed"
        raise ControllerError(f"{label} remote moved")

    def apply_operator_ticket_change(
        self, claim: dict[str, Any], plan: dict[str, Any], after: str,
        observed_status: str, operator_id: str, message: str,
        exact_commit: Any, label: str,
    ) -> str:
        if observed_status == "applied":
            return self.cell_git(claim, "rev-parse", "HEAD").stdout.strip()
        ticket_path = f"factory/tickets/{claim['ticket']}.md"
        if observed_status in {"planned", "prepared"}:
            path = Path(claim["worktree"]) / ticket_path
            info = path.lstat()
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or info.st_nlink != 1
            ):
                raise ControllerError(f"{label} ticket is unsafe")
            if observed_status == "planned":
                path.write_text(after, encoding="utf-8")
            added = self.cell_git(claim, "add", "--", ticket_path)
            committed = self.cell_git(
                claim, "-c", f"user.name={operator_id}",
                "-c", "user.email=operator@local", "-c", "commit.gpgsign=false",
                "commit", "-m", message, "--", ticket_path,
            )
            if added.returncode or committed.returncode:
                raise ControllerError(f"{label} commit failed")
        head = self.cell_git(claim, "rev-parse", "HEAD").stdout.strip()
        if not exact_commit(plan["parent_sha"], head):
            raise ControllerError(f"{label} commit is invalid")
        status, observed, remote = self.remote_cell_head_status(claim)
        if (
            status == "resume_commit_not_pushed"
            and observed == head and remote == plan["parent_sha"]
        ):
            pushed = self.cell_git(
                claim, "push", "--porcelain", "origin",
                f"HEAD:refs/heads/{claim['branch']}",
            )
            status, observed, remote = self.remote_cell_head_status(claim)
            if pushed.returncode and not (
                status == "pushed" and observed == remote == head
            ):
                raise ControllerError(f"{label} push failed")
        if status != "pushed" or observed != remote or remote != head:
            raise ControllerError(f"{label} push failed")
        return head

    def operator_control_claim(
        self, ticket: str, label: str,
    ) -> dict[str, Any]:
        claim_path = self.claim_path(ticket)
        try:
            claim = read(claim_path)
        except (
            ControllerError, FileNotFoundError, json.JSONDecodeError, OSError,
            UnicodeError,
        ) as error:
            raise ControllerError(f"{label} authority is unavailable") from error
        if (
            not self.valid_claim_document(claim_path, claim)
            or not Path(claim["worktree"]).exists()
        ):
            raise ControllerError(f"{label} authority is unavailable")
        self.operator_control_worktree(claim, label)
        return claim

    def operator_control_worktree(
        self, claim: dict[str, Any], label: str,
    ) -> Path:
        worktree = Path(claim.get("worktree", ""))
        try:
            info = worktree.lstat()
            resolved = worktree.resolve(strict=True)
            registered = self.worktrees_by_branch().get(
                f"refs/heads/{claim.get('branch', '')}", [],
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise ControllerError(f"{label} worktree is unsafe") from error
        if (
            not worktree.is_absolute()
            or resolved != worktree
            or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o022
            or registered != [str(worktree)]
        ):
            raise ControllerError(f"{label} worktree is unsafe")
        return worktree

    def retry_preview_timeout(
        self, ticket: str, operator_id: str,
    ) -> dict[str, Any]:
        if (
            not TICKET.fullmatch(ticket)
            or not OPERATOR_ID.fullmatch(operator_id)
            or operator_id == "auto"
        ):
            raise ControllerError("preview timeout retry request is invalid")
        claim = self.operator_control_claim(ticket, "preview timeout retry")
        transition = self.operator_transition(claim)
        passport = self.authenticated_operator_passport(ticket)
        passport_path = self.state / "passports" / f"{ticket}.json"
        status, local, remote = self.remote_cell_head_status(claim)
        dirty = self.cell_git(
            claim, "status", "--porcelain=v1", "-z",
        ).stdout
        started = claim.get("preview_wait_started_epoch")
        now = int(time.time())
        lease_released = claim.get("lease_released") is True or (
            self.parked(claim)
            and claim.get("lease") == ""
            and "lease_released" not in claim
        )
        if (
            claim.get("status") != "blocked"
            or claim.get("blocked_reason") != "preview-identity-timeout"
            or claim.get("receipt")
            or claim.get("role")
            or claim.get("publication_lease")
            or not lease_released
            or self.role_active(claim)
            or transition is None
            or transition.get("factory_sha") != self.release_path.name
            or transition.get("consumed") is not False
            or transition.get("stage") != "RUN narrator"
            or transition.get("role") != "narrator"
            or not DIGEST.fullmatch(transition.get("receipt_sha256", ""))
            or passport is None
            or passport.get("ticket") != ticket
            or passport.get("project") != self.project
            or passport.get("contract_version") not in CONTROLLER_CONTRACTS
            or passport.get("factory_sha") != self.release_path.name
            or passport.get("branch") != claim.get("branch")
            or transition.get("head_sha") != passport.get("head_sha")
            or claim.get("preview_wait_head") != transition.get("head_sha")
            or transition.get("passport_sha256")
            != hashlib.sha256(passport_path.read_bytes()).hexdigest()
            or transition.get("route_plan_sha256")
            != passport.get("route_plan_sha256")
            or isinstance(started, bool)
            or not isinstance(started, int)
            or started > now
            or now - started < PREVIEW_IDENTITY_WAIT_SECONDS
            or status != "pushed"
            or local != remote
            or local != transition.get("head_sha")
            or dirty
            or not self.ticket_release_current(claim)
            or not self.remote_passport_valid(claim)
        ):
            raise ControllerError("preview timeout retry authority is unavailable")
        claim["preview_wait_started_epoch"] = now
        claim["status"] = "claimed"
        claim.pop("blocked_reason", None)
        self.save_claim(claim)
        self.event(
            "preview_identity_timeout_retry_authorized", ticket,
            expected=local, operator_id=operator_id,
            transition_receipt_sha256=transition["receipt_sha256"],
        )
        return {
            "expected": local, "schema": SCHEMA,
            "status": "retry-authorized", "ticket": ticket,
        }

    def semantic_authorization_plan(
        self, ticket: str, role: str, semantic_round: int, operator_id: str,
    ) -> tuple[dict[str, Any], dict[str, Any], str, str]:
        if (
            not TICKET.fullmatch(ticket)
            or role not in {
                "planner", "spec-linter", "test-author", "builder", "narrator",
            }
            or semantic_round < 3
            or not OPERATOR_ID.fullmatch(operator_id)
            or operator_id == "auto"
        ):
            raise ControllerError("semantic authorization request is invalid")
        claim = self.operator_control_claim(ticket, "semantic authorization")
        if not (
            DIGEST.fullmatch(claim.get("lease", ""))
            or self.parked(claim) and claim.get("lease") == ""
        ):
            raise ControllerError("semantic authorization authority is unavailable")
        transition = self.transition_receipt(claim, record=False)
        passport = self.authenticated_operator_passport(ticket)
        line = f"OPERATOR AUTHORIZATION: {role} round {semantic_round}"
        context = (
            semantic_authorization_context(
                transition.get("stage", ""), transition.get("loop"),
            )
            if transition is not None else None
        )
        passport_path = self.state / "passports" / f"{ticket}.json"
        if (
            claim.get("status") != "waiting"
            or claim.get("blocked_reason")
            != semantic_block_reason(role, semantic_round)
            or claim.get("receipt")
            or claim.get("role")
            or claim.get("publication_lease")
            or self.role_active(claim)
            or transition is None
            or transition.get("factory_sha") != self.release_path.name
            or transition.get("consumed") is not False
            or transition.get("role") is not None
            or context is None
            or context[:3] != (line, role, semantic_round)
            or not DIGEST.fullmatch(transition.get("receipt_sha256", ""))
            or passport is None
            or passport.get("ticket") != ticket
            or passport.get("project") != self.project
            or passport.get("contract_version") not in CONTROLLER_CONTRACTS
            or passport.get("factory_sha") != self.release_path.name
            or passport.get("branch") != claim.get("branch")
            or transition.get("head_sha") != passport.get("head_sha")
            or transition.get("passport_sha256")
            != hashlib.sha256(passport_path.read_bytes()).hexdigest()
            or transition.get("route_plan_sha256")
            != passport.get("route_plan_sha256")
        ):
            raise ControllerError("semantic authorization authority is unavailable")
        ticket_path = f"factory/tickets/{ticket}.md"
        before = self.cell_git(
            claim, "show", f"{passport['head_sha']}:{ticket_path}",
        )
        if before.returncode:
            raise ControllerError("semantic authorization ticket is unavailable")
        epoch_before = self.epoch_ticket(ticket, before.stdout)
        old_lines = epoch_before.splitlines()
        if (
            old_lines.count(line) != 0
            or context[3] == "planner-spec-linter"
            and planner_spec_linter_authorization(epoch_before)
            != (semantic_round, "required")
        ):
            raise ControllerError("semantic authorization ticket is invalid")
        after = before.stdout + ("" if before.stdout.endswith("\n") else "\n") + line + "\n"
        observed_status = self.operator_ticket_change_status(
            claim, passport, after,
            lambda parent, child: self.exact_ticket_commit(
                claim, parent, child, authorization=True,
                authorization_role=role, semantic_round=semantic_round,
                semantic_kind=context[3],
            ),
            "semantic authorization",
        )
        plan = {
            "authorization_line": line,
            "branch": claim["branch"],
            "claim_sha256": hashlib.sha256(canonical_document(claim)).hexdigest(),
            "factory_sha": self.release_path.name,
            "operator_id": operator_id,
            "parent_sha": passport["head_sha"],
            "passport_sha256": passport["passport_sha256"],
            "project": self.project,
            "role": role,
            "schema": SCHEMA,
            "semantic_kind": context[3],
            "semantic_round": semantic_round,
            "ticket": ticket,
            "ticket_after_sha256": hashlib.sha256(after.encode()).hexdigest(),
            "ticket_before_sha256": hashlib.sha256(before.stdout.encode()).hexdigest(),
            "transition_receipt_sha256": transition["receipt_sha256"],
            "worktree": claim["worktree"],
        }
        plan["approval_hash"] = hashlib.sha256(canonical_document(plan)).hexdigest()
        return plan, claim, after, observed_status

    def plan_semantic_authorization(
        self, ticket: str, role: str, semantic_round: int, operator_id: str,
    ) -> dict[str, Any]:
        plan, _claim, _after, observed_status = self.semantic_authorization_plan(
            ticket, role, semantic_round, operator_id,
        )
        return {**plan, "status": observed_status}

    def apply_semantic_authorization(
        self, ticket: str, role: str, semantic_round: int, operator_id: str,
        approve_hash: str,
    ) -> dict[str, Any]:
        plan, claim, after, observed_status = self.semantic_authorization_plan(
            ticket, role, semantic_round, operator_id,
        )
        if (
            not DIGEST.fullmatch(approve_hash)
            or approve_hash != plan["approval_hash"]
        ):
            raise ControllerError("semantic authorization approval hash does not match")
        if not DIGEST.fullmatch(claim.get("lease", "")):
            self.ensure_lease(claim, "semantic-round-authorization")
        head = self.apply_operator_ticket_change(
            claim, plan, after, observed_status, operator_id,
            f"Authorize {role} round {semantic_round} for {ticket}",
            lambda parent, child: self.exact_ticket_commit(
                claim, parent, child, authorization=True,
                authorization_role=role, semantic_round=semantic_round,
                semantic_kind=plan["semantic_kind"],
            ),
            "semantic authorization",
        )
        return {
            "approval_hash": approve_hash, "authorization_head": head,
            "schema": SCHEMA, "status": "applied", "ticket": ticket,
        }

    def exact_ticket_only_lineage(
        self, claim: dict[str, Any], before: str, after: str,
    ) -> bool:
        if before == after:
            return SHA.fullmatch(before) is not None
        if not SHA.fullmatch(before) or not SHA.fullmatch(after):
            return False
        history = self.cell_git(
            claim, "rev-list", "--reverse", "--parents", f"{before}..{after}",
        )
        rows = history.stdout.splitlines()
        if history.returncode or not 1 <= len(rows) <= 8:
            return False
        previous = before
        ticket_path = f"factory/tickets/{claim['ticket']}.md"
        for row in rows:
            commit = row.split()
            paths = self.cell_git(
                claim, "diff-tree", "--no-commit-id", "--name-only",
                "--no-renames", "-r", commit[0],
            )
            if (
                len(commit) != 2
                or commit[1] != previous
                or paths.returncode
                or paths.stdout.splitlines() != [ticket_path]
            ):
                return False
            previous = commit[0]
        return previous == after

    def historical_contract_source(
        self, ticket: str, claim: dict[str, Any],
        transition: dict[str, Any] | None, terminal: dict[str, Any] | None,
        passport: dict[str, Any] | None,
    ) -> bool:
        evidence_factory = transition.get("factory_sha", "") if transition else ""
        migrations = passport.get("migration_history") if passport else None
        starts = [
            index for index, edge in enumerate(migrations or [])
            if valid_v2_migration(edge)
            and edge["from_factory_sha"] == evidence_factory
            and edge["to_factory_sha"] != evidence_factory
        ]
        suffix = (
            migrations[starts[0]:]
            if isinstance(migrations, list) and len(starts) == 1 else []
        )
        release_edges = [
            edge for edge in suffix
            if edge["from_factory_sha"] != edge["to_factory_sha"]
        ]
        source = self.qualification_ticket_source(ticket)
        reconstruction = self.qualification_reconstruction_edge(
            ticket, claim, passport, suffix[-1] if suffix else {},
        )
        return bool(
            os.environ.get("FACTORY_KIT_TRUST_SCOPE")
            == "qualification-candidate"
            and os.environ.get("FACTORY_QUALIFICATION_MODE") == "isolated"
            and self.qualification
            and self.qualification.get("mode") == "successor"
            and terminal is not None
            and terminal.get("kit_sha") == evidence_factory
            and passport is not None
            and passport.get("factory_sha") == self.release_path.name
            and suffix
            and all(valid_v2_migration(edge) for edge in suffix)
            and release_edges
            and release_edges[-1]["from_factory_sha"] == source
            and release_edges[-1]["to_factory_sha"] == self.release_path.name
            and suffix[0]["from_route_plan_sha256"]
            == transition.get("route_plan_sha256")
            and self.exact_ticket_only_lineage(
                claim, transition.get("head_sha", ""),
                suffix[0]["from_head_sha"],
            )
            and passport_head_lineage(passport, suffix[0]["from_head_sha"])
            and all(
                left["to_factory_sha"] == right["from_factory_sha"]
                and left["to_head_sha"] == right["from_head_sha"]
                and left["to_protected_base_sha"]
                == right["from_protected_base_sha"]
                and left["to_route_plan_sha256"]
                == right["from_route_plan_sha256"]
                for left, right in zip(suffix, suffix[1:])
            )
            and suffix[-1]["to_factory_sha"] == passport.get("factory_sha")
            and suffix[-1]["to_head_sha"] == passport.get("head_sha")
            and suffix[-1]["to_protected_base_sha"]
            == passport.get("protected_base_sha")
            and suffix[-1]["to_route_plan_sha256"]
            == passport.get("route_plan_sha256")
            and suffix[-1]["from_passport_file_sha256"]
            == passport.get("parent_file_sha256")
            and suffix[-1]["from_passport_sha256"]
            == passport.get("parent_digest")
            and all(
                edge["from_head_sha"] == edge["to_head_sha"]
                or self.exact_route_migration_commit(
                    claim, edge["from_head_sha"], edge["to_head_sha"],
                )
                or index == len(suffix) - 1 and reconstruction
                for index, edge in enumerate(suffix)
            )
            and successor_release_lineage(
                passport.get("factory_release_history"), migrations,
                evidence_factory, self.release_path.name, valid_v2_migration,
            )
            and successor_release_lineage(
                passport.get("factory_release_history"), migrations,
                source, self.release_path.name, valid_v2_migration,
            )
        )

    def qualification_ticket_source(self, ticket: str) -> str:
        revision = os.environ.get("FACTORY_QUALIFICATION_PRODUCT_SHA", "")
        if (
            not self.qualification
            or self.qualification.get("mode") != "successor"
            or not SHA.fullmatch(revision)
        ):
            return ""
        try:
            authorization_raw = committed_qualification_blob(
                self.product, revision,
                f"factory/migrations/inflight-release/{self.release_path.name}.json",
                1_048_576,
            )
            project_raw = committed_qualification_blob(
                self.product, revision, "factory/PROJECT.env", 131_072,
            )
            if authorization_raw is None or project_raw is None:
                return ""
            authorization, entries = parse_inflight_authorization(
                authorization_raw.decode("utf-8", "strict"),
                project_raw.decode("utf-8", "strict"), self.release_path.name,
            )
        except (
            InflightAuthorizationError, QualificationManifestError,
            UnicodeDecodeError,
        ):
            return ""
        if (
            authorization.get("source_kit_sha")
            != self.qualification.get("source_factory_sha")
            or set(entries) != set(self.qualification.get("tickets", []))
            or ticket not in entries
        ):
            return ""
        return ticket_source_kit(authorization, entries[ticket])

    def contract_repair_blocked(self, claim: dict[str, Any]) -> bool:
        attempt = claim.get("recovery_attempt")
        return bool(
            claim.get("blocked_reason") == "role-failure"
            or self.valid_recovery_attempt(attempt)
            and attempt.get("recovery") == "targeted-repair"
            and (
                claim.get("blocked_reason")
                == "recovery-abandoned:targeted-repair"
                and attempt.get("phase") == "abandoned"
                or not claim.get("blocked_reason")
                and attempt.get("phase") == "settled"
                and attempt.get("factory_sha") == self.release_path.name
                and attempt.get("retry_reason") == ""
                and attempt.get("retry_status") == "blocked"
            )
        )

    def contract_repair_plan(
        self, ticket: str, role: str, operator_id: str,
    ) -> tuple[dict[str, Any], dict[str, Any], str, str]:
        if (
            not TICKET.fullmatch(ticket)
            or role not in {"planner", "spec-linter", "test-author", "builder"}
            or not OPERATOR_ID.fullmatch(operator_id)
            or operator_id == "auto"
        ):
            raise ControllerError("contract repair request is invalid")
        claim = self.operator_control_claim(ticket, "contract repair")
        receipt = claim.get("receipt", "")
        transition = self.transition_receipt(
            claim, allow_prior=True, record=False,
        )
        passport = self.authenticated_operator_passport(ticket)
        terminal = self.terminal_for_receipt(ticket, receipt)
        evidence_factory = transition.get("factory_sha", "") if transition else ""
        source_evidence = self.historical_contract_source(
            ticket, claim, transition, terminal, passport,
        )
        if (
            claim.get("status") != "blocked"
            or not self.contract_repair_blocked(claim)
            or not DIGEST.fullmatch(receipt)
            or not claim.get("role")
            or claim.get("publication_lease")
            or self.role_active(claim)
            or not (
                DIGEST.fullmatch(claim.get("lease", ""))
                or self.parked(claim) and claim.get("lease") == ""
            )
            or transition is None
            or not (
                evidence_factory == self.release_path.name
                and terminal is not None
                and terminal.get("kit_sha") == self.release_path.name
                or source_evidence
            )
            or transition.get("receipt_sha256") != receipt
            or transition.get("role") != claim["role"]
            or transition.get("stage") not in {
                f"RUN {claim['role']}", f"FIX {claim['role']}",
            }
            or passport is None
            or passport.get("ticket") != ticket
            or passport.get("project") != self.project
            or passport.get("factory_sha") != self.release_path.name
            or passport.get("branch") != claim.get("branch")
            or passport.get("current_state") != "Blocked-Escalated"
            or passport.get("current_stage") != transition.get("stage")
            or passport.get("transition_receipt_sha256") != receipt
            or terminal is None
            or terminal.get("ticket") != ticket
            or terminal.get("role") != claim["role"]
            or terminal.get("transition_receipt_sha256") != receipt
            or terminal.get("exit_status") != "12"
            or terminal.get("role_exit") != "role_exit_contract_blocked"
            or not self.remote_passport_valid(claim)
        ):
            raise ControllerError("contract repair authority is unavailable")
        ticket_path = f"factory/tickets/{ticket}.md"
        before = self.cell_git(claim, "show", f"{passport['head_sha']}:{ticket_path}")
        if (
            before.returncode
            or self.contract_resume_status(claim, before.stdout, transition)
            != "waiting"
        ):
            raise ControllerError("contract repair ticket is invalid")
        after = (
            before.stdout.rstrip("\n")
            + f"\n\nOPERATOR RESUME: {role}\n"
            + f"OPERATOR RESUME RECEIPT: {receipt}\n"
        )
        observed_status = self.operator_ticket_change_status(
            claim, passport, after,
            lambda parent, child: self.exact_ticket_commit(claim, parent, child),
            "contract repair",
        )
        plan = {
            "blocked_receipt": receipt,
            "blocked_role": claim["role"],
            "branch": claim["branch"],
            "claim_sha256": hashlib.sha256(canonical_document({
                key: value for key, value in claim.items()
                if key not in {"lease", "lease_released"}
            })).hexdigest(),
            "factory_sha": self.release_path.name,
            "operator_id": operator_id,
            "parent_sha": passport["head_sha"],
            "passport_sha256": passport["passport_sha256"],
            "project": self.project,
            "repair_role": role,
            "schema": SCHEMA,
            "ticket": ticket,
            "ticket_after_sha256": hashlib.sha256(after.encode()).hexdigest(),
            "ticket_before_sha256": hashlib.sha256(before.stdout.encode()).hexdigest(),
        }
        plan["approval_hash"] = hashlib.sha256(canonical_document(plan)).hexdigest()
        return plan, claim, after, observed_status

    def qualification_reconstruction_edge(
        self, ticket: str, claim: dict[str, Any], passport: dict[str, Any] | None,
        edge: dict[str, Any],
    ) -> bool:
        if (
            not passport
            or os.environ.get("FACTORY_KIT_TRUST_SCOPE")
            != "qualification-candidate"
            or os.environ.get("FACTORY_QUALIFICATION_MODE") != "isolated"
            or not self.qualification
            or self.qualification.get("mode") != "successor"
            or not valid_v2_migration(edge)
            or edge.get("from_factory_sha") != self.release_path.name
            or edge.get("to_factory_sha") != self.release_path.name
            or edge.get("from_head_sha") == edge.get("to_head_sha")
            or edge.get("to_head_sha") != passport.get("head_sha")
            or edge.get("from_protected_base_sha")
            != edge.get("to_protected_base_sha")
            or edge.get("to_protected_base_sha")
            != passport.get("protected_base_sha")
            or edge.get("from_route_plan_sha256")
            != edge.get("to_route_plan_sha256")
            or edge.get("to_route_plan_sha256")
            != passport.get("route_plan_sha256")
            or edge.get("from_passport_file_sha256")
            != passport.get("parent_file_sha256")
            or edge.get("from_passport_sha256")
            != passport.get("parent_digest")
        ):
            return False
        try:
            root = safe_directory(self.state / "history-reconstructions")
            record = read(root / f"{ticket}.json")
        except (ControllerError, FileNotFoundError, OSError, ValueError):
            return False
        digest = record.get("record_sha256", "")
        transition = self.transition_receipt(
            claim, allow_prior=True, record=False,
        )
        reconstruction_base = release_source_base(
            passport.get("migration_history"),
            transition.get("factory_sha", "") if transition else "",
        )
        unsigned = {
            key: value for key, value in record.items()
            if key != "record_sha256"
        }
        expected_keys = {
            "branch", "configured_test_paths", "factory_sha", "new_head",
            "new_tree", "old_head", "old_tree", "passport_sha256",
            "product_origin_sha256", "project", "protected_base_sha",
            "record_sha256", "schema", "state", "ticket",
            "transition_receipt_sha256",
        }
        configured = record.get("configured_test_paths")
        return bool(
            set(record) == expected_keys
            and DIGEST.fullmatch(digest)
            and digest == hashlib.sha256(canonical_document(unsigned)).hexdigest()
            and digest == edge.get("rewrite_authorization_sha256")
            and record.get("schema")
            == QUALIFICATION_HISTORY_RECONSTRUCTION_SCHEMA
            and record.get("ticket") == ticket
            and record.get("project") == self.project
            and record.get("branch") == claim.get("branch")
            and record.get("factory_sha") == self.release_path.name
            and record.get("old_head") == edge.get("from_head_sha")
            and record.get("new_head") == passport.get("head_sha")
            and record.get("old_tree") == record.get("new_tree")
            == passport.get("head_tree")
            and record.get("product_origin_sha256")
            == passport.get("product_origin_sha256")
            and record.get("protected_base_sha")
            == reconstruction_base
            and record.get("state") == "Blocked-Escalated"
            and record.get("transition_receipt_sha256") == claim.get("receipt")
            and isinstance(configured, list) and configured
            and all(isinstance(path, str) for path in configured)
            and verified_test_snapshot_reconstruction(
                claim["worktree"], record["protected_base_sha"],
                record["old_head"], record["new_head"], configured, ticket,
            )
        )

    def plan_contract_repair(
        self, ticket: str, role: str, operator_id: str,
    ) -> dict[str, Any]:
        plan, _claim, _after, observed = self.contract_repair_plan(
            ticket, role, operator_id,
        )
        return {**plan, "status": observed}

    def apply_contract_repair(
        self, ticket: str, role: str, operator_id: str, approve_hash: str,
    ) -> dict[str, Any]:
        plan, claim, after, observed = self.contract_repair_plan(
            ticket, role, operator_id,
        )
        if not DIGEST.fullmatch(approve_hash) or approve_hash != plan["approval_hash"]:
            raise ControllerError("contract repair approval hash does not match")
        self.ensure_lease(claim, "contract-repair")
        head = self.apply_operator_ticket_change(
            claim, plan, after, observed, operator_id,
            f"Route {ticket} contract repair to {role}",
            lambda parent, child: self.exact_ticket_commit(claim, parent, child),
            "contract repair",
        )
        self.release_ticket_lease(claim)
        return {
            "approval_hash": approve_hash, "repair_head": head,
            "schema": SCHEMA, "status": "applied", "ticket": ticket,
        }

    def qualification_history_repair(
        self, ticket: str, blocked_receipt: str,
    ) -> dict[str, Any]:
        """Rebuild one blocked test-author branch without changing its tree."""
        if (
            os.environ.get("FACTORY_KIT_TRUST_SCOPE")
            != "qualification-candidate"
            or os.environ.get("FACTORY_QUALIFICATION_MODE") != "isolated"
            or not self.qualification
            or self.qualification.get("mode") != "successor"
            or ticket not in self.qualification.get("tickets", [])
            or not TICKET.fullmatch(ticket)
            or not DIGEST.fullmatch(blocked_receipt)
        ):
            raise ControllerError(
                "qualification history reconstruction authority is unavailable"
            )
        records_path = self.state / "history-reconstructions"
        records = (
            safe_directory(records_path)
            if records_path.exists() or records_path.is_symlink() else None
        )
        record_path = records_path / f"{ticket}.json"
        record = (
            read(record_path)
            if record_path.exists() or record_path.is_symlink() else None
        )

        if record is None:
            plan, claim, _after, observed = self.contract_repair_plan(
                ticket, "test-author", "qualification-history-repair",
            )
            if (
                plan.get("blocked_receipt") != blocked_receipt
                or plan.get("blocked_role") != "test-author"
                or observed != "planned"
            ):
                raise ControllerError(
                    "qualification history reconstruction authority is unavailable"
                )
            passport = self.authenticated_operator_passport(ticket)
            if passport is None:
                raise ControllerError(
                    "qualification history reconstruction passport is unavailable"
                )
            transition = self.transition_receipt(
                claim, allow_prior=True, record=False,
            )
        else:
            claim = self.operator_control_claim(
                ticket, "qualification history reconstruction",
            )
            passport = self.authenticated_operator_passport(ticket)
            transition = self.transition_receipt(
                claim, allow_prior=True, record=False,
            )
            terminal = self.terminal_for_receipt(ticket, blocked_receipt)
            evidence_factory = transition.get("factory_sha", "") if transition else ""
            source_valid = self.historical_contract_source(
                ticket, claim, transition, terminal, passport,
            )
            if (
                claim.get("status") != "blocked"
                or not self.contract_repair_blocked(claim)
                or claim.get("receipt") != blocked_receipt
                or claim.get("role") != "test-author"
                or claim.get("publication_lease")
                or self.role_active(claim)
                or not (
                    DIGEST.fullmatch(claim.get("lease", ""))
                    or self.parked(claim) and claim.get("lease") == ""
                )
                or transition is None
                or transition.get("receipt_sha256") != blocked_receipt
                or transition.get("role") != "test-author"
                or transition.get("stage")
                not in {"RUN test-author", "FIX test-author"}
                or transition.get("consumed") is not True
                or not (
                    evidence_factory == self.release_path.name or source_valid
                )
                or passport is None
                or passport.get("ticket") != ticket
                or passport.get("project") != self.project
                or passport.get("factory_sha") != self.release_path.name
                or passport.get("branch") != claim.get("branch")
                or passport.get("current_state") != "Blocked-Escalated"
                or passport.get("current_stage") != transition.get("stage")
                or passport.get("transition_receipt_sha256") != blocked_receipt
                or terminal is None
                or terminal.get("ticket") != ticket
                or terminal.get("role") != "test-author"
                or terminal.get("transition_receipt_sha256") != blocked_receipt
                or terminal.get("exit_status") != "12"
                or terminal.get("role_exit") != "role_exit_contract_blocked"
            ):
                raise ControllerError(
                    "qualification history reconstruction authority is unavailable"
                )

        worktree = Path(claim["worktree"])
        reconstruction_base = (
            passport["protected_base_sha"]
            if transition.get("factory_sha") == self.release_path.name
            else release_source_base(
                passport.get("migration_history"),
                transition.get("factory_sha", ""),
            )
        )
        old_head = record.get("old_head", "") if record else passport["head_sha"]
        new_head = record.get("new_head", "") if record else ""
        base = record.get("protected_base_sha", "") if record else reconstruction_base
        project = self.cell_git(
            claim, "show", f"{base}:factory/PROJECT.env",
        )
        values = re.findall(r"(?m)^TEST_PATHS=(.*)$", project.stdout)
        try:
            configured = " ".join(
                shlex.split(values[0], comments=False, posix=True)
            ).split()
        except (IndexError, ValueError) as error:
            raise ControllerError(
                "qualification history reconstruction test paths are invalid"
            ) from error
        safe_path = re.compile(r"[A-Za-z0-9._][A-Za-z0-9._/-]*")
        if (
            project.returncode
            or len(values) != 1
            or not configured
            or len(configured) != len(set(configured))
            or any(
                not safe_path.fullmatch(path.rstrip("/"))
                or any(
                    part in {"", ".", ".."}
                    for part in path.rstrip("/").split("/")
                )
                or path.rstrip("/") == "factory"
                or path.rstrip("/").startswith("factory/")
                for path in configured
            )
        ):
            raise ControllerError(
                "qualification history reconstruction test paths are invalid"
            )
        normalized = [path.rstrip("/") for path in configured]
        if any(
            left == right
            or left.startswith(right + "/")
            or right.startswith(left + "/")
            for index, left in enumerate(normalized)
            for right in normalized[index + 1:]
        ):
            raise ControllerError(
                "qualification history reconstruction test paths overlap"
            )
        certified = os.environ.get("FACTORY_CERTIFIED_PRODUCT_ORIGIN", "")
        origin = self.cell_git(claim, "remote", "get-url", "--push", "origin")

        def remote_head(branch: str) -> str:
            observed = subprocess.run(
                [
                    "git", "-C", str(worktree), "ls-remote", "--exit-code",
                    certified, f"refs/heads/{branch}",
                ],
                text=True, capture_output=True, check=False, timeout=120,
            )
            match = re.fullmatch(
                rf"([0-9a-f]{{40}})\trefs/heads/{re.escape(branch)}\n",
                observed.stdout,
            )
            if observed.returncode or match is None:
                raise ControllerError(
                    "qualification history reconstruction remote is unavailable"
                )
            return match.group(1)

        branch = claim["branch"]
        local_branch = self.cell_git(
            claim, "symbolic-ref", "--quiet", "--short", "HEAD",
        )
        local = self.cell_git(claim, "rev-parse", "HEAD").stdout.strip()
        tracking_ref = f"refs/remotes/origin/{branch}"
        tracking_result = self.cell_git(
            claim, "rev-parse", "--verify", tracking_ref,
        )
        tracking = tracking_result.stdout.strip()
        dirty = self.cell_git(
            claim, "status", "--porcelain=v1", "-z", "--untracked-files=all",
            "--ignore-submodules=none",
        )
        base_continuous = self.cell_git(
            claim, "merge-base", "--is-ancestor", base,
            passport.get("protected_base_sha", ""),
        )
        if (
            not certified
            or any(character in certified for character in "\n\r\t")
            or origin.returncode
            or origin.stdout.rstrip("\n") != certified
            or local_branch.returncode
            or local_branch.stdout.strip() != branch
            or dirty.returncode
            or dirty.stdout
            or not SHA.fullmatch(base)
            or base_continuous.returncode
            or remote_head("main") != passport.get("protected_base_sha")
            or tracking_result.returncode
            or tracking not in {old_head, new_head}
            or local not in {old_head, new_head}
            or remote_head(branch) not in {old_head, new_head}
        ):
            raise ControllerError(
                "qualification history reconstruction repository moved"
            )

        if record is None:
            if local != old_head or tracking != old_head or remote_head(branch) != old_head:
                raise ControllerError(
                    "qualification history reconstruction repository moved"
                )
            try:
                reconstruction = create_test_snapshot_reconstruction(
                    str(worktree), base, old_head, configured, ticket,
                )
            except HistoryReconstructionError as error:
                raise ControllerError(str(error)) from error
            new_head = reconstruction["new_head"]
            with tempfile.TemporaryDirectory(
                prefix=f"history-reconstruction-{ticket}.", dir=self.state,
            ) as temporary:
                gate_cell = Path(temporary) / "cell"
                added = subprocess.run(
                    [
                        "git", "-C", str(self.product), "worktree", "add",
                        "--detach", str(gate_cell), new_head,
                    ],
                    text=True, capture_output=True, check=False, timeout=120,
                )
                if added.returncode:
                    raise ControllerError(
                        "qualification history reconstruction gate cell failed"
                    )
                try:
                    environment = {
                        "BASE_REF": base,
                        "EXEMPT_PATHS": "factory/",
                        "HOME": str(Path.home()),
                        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                        "TEST_PATHS": " ".join(configured),
                        "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
                    }
                    for relative in (
                        ".github/scripts/test-immutability-check.sh",
                        ".github/scripts/builder-confinement-check.sh",
                    ):
                        gate = gate_cell / relative
                        info = gate.lstat()
                        if not stat.S_ISREG(info.st_mode) or gate.is_symlink():
                            raise ControllerError(
                                "qualification history reconstruction gate is unsafe"
                            )
                        checked = subprocess.run(
                            ["/bin/bash", str(gate)], cwd=gate_cell,
                            env=environment, text=True, capture_output=True,
                            check=False, timeout=120,
                        )
                        if checked.returncode:
                            raise ControllerError(
                                "qualification history reconstruction gate failed"
                            )
                finally:
                    subprocess.run(
                        [
                            "git", "-C", str(self.product), "worktree",
                            "remove", "--force", str(gate_cell),
                        ],
                        text=True, capture_output=True, check=False, timeout=120,
                    )
            unsigned = {
                "branch": branch,
                "configured_test_paths": configured,
                "factory_sha": self.release_path.name,
                "new_head": new_head,
                "new_tree": reconstruction["old_tree"],
                "old_head": old_head,
                "old_tree": reconstruction["old_tree"],
                "passport_sha256": passport["passport_sha256"],
                "product_origin_sha256": passport["product_origin_sha256"],
                "project": self.project,
                "protected_base_sha": base,
                "schema": QUALIFICATION_HISTORY_RECONSTRUCTION_SCHEMA,
                "state": "Blocked-Escalated",
                "ticket": ticket,
                "transition_receipt_sha256": blocked_receipt,
            }
            record = {
                **unsigned,
                "record_sha256": hashlib.sha256(
                    canonical_document(unsigned),
                ).hexdigest(),
            }
            if records is None:
                records_path.mkdir(mode=0o700)
                records = safe_directory(records_path)
                record_path = records / f"{ticket}.json"
            write(record_path, record)

        unsigned = {
            key: value for key, value in record.items()
            if key != "record_sha256"
        }
        record_digest = record.get("record_sha256", "")
        expected_keys = {
            "branch", "configured_test_paths", "factory_sha", "new_head",
            "new_tree", "old_head", "old_tree", "passport_sha256",
            "product_origin_sha256", "project", "protected_base_sha",
            "record_sha256", "schema", "state", "ticket",
            "transition_receipt_sha256",
        }
        if (
            set(record) != expected_keys
            or not DIGEST.fullmatch(record_digest)
            or record_digest
            != hashlib.sha256(canonical_document(unsigned)).hexdigest()
            or record.get("schema")
            != QUALIFICATION_HISTORY_RECONSTRUCTION_SCHEMA
            or record.get("ticket") != ticket
            or record.get("project") != self.project
            or record.get("branch") != branch
            or record.get("factory_sha") != self.release_path.name
            or record.get("configured_test_paths") != configured
            or record.get("protected_base_sha") != base
            or record.get("protected_base_sha") != reconstruction_base
            or record.get("transition_receipt_sha256") != blocked_receipt
            or record.get("state") != "Blocked-Escalated"
            or record.get("old_tree") != record.get("new_tree")
            or record.get("product_origin_sha256")
            != passport.get("product_origin_sha256")
            or not verified_test_snapshot_reconstruction(
                str(worktree), base, record.get("old_head", ""),
                record.get("new_head", ""), configured, ticket,
            )
        ):
            raise ControllerError(
                "qualification history reconstruction record is invalid"
            )
        old_head = record["old_head"]
        new_head = record["new_head"]
        migration = passport.get("migration_history", [])[-1:]
        passport_old = (
            passport.get("head_sha") == old_head
            and passport.get("passport_sha256") == record["passport_sha256"]
        )
        passport_new = bool(
            passport.get("head_sha") == new_head
            and migration
            and migration[0].get("from_head_sha") == old_head
            and migration[0].get("to_head_sha") == new_head
            and migration[0].get("rewrite_authorization_sha256")
            == record_digest
        )
        if not (passport_old or passport_new):
            raise ControllerError(
                "qualification history reconstruction passport moved"
            )
        local = self.cell_git(claim, "rev-parse", "HEAD").stdout.strip()
        tracking = self.cell_git(
            claim, "rev-parse", "--verify", tracking_ref,
        ).stdout.strip()
        remote = remote_head(branch)
        if any(head not in {old_head, new_head} for head in (local, tracking, remote)):
            raise ControllerError(
                "qualification history reconstruction repository moved"
            )
        if remote == old_head:
            pushed = self.cell_git(
                claim, "push", "--porcelain",
                f"--force-with-lease=refs/heads/{branch}:{old_head}",
                certified, f"{new_head}:refs/heads/{branch}",
            )
            if pushed.returncode and remote_head(branch) != new_head:
                raise ControllerError(
                    "qualification history reconstruction push failed"
                )
        if remote_head(branch) != new_head:
            raise ControllerError(
                "qualification history reconstruction remote moved"
            )
        if local == old_head and self.cell_git(
            claim, "update-ref", f"refs/heads/{branch}", new_head, old_head,
        ).returncode:
            raise ControllerError(
                "qualification history reconstruction local update failed"
            )
        if tracking == old_head and self.cell_git(
            claim, "update-ref", tracking_ref, new_head, old_head,
        ).returncode:
            raise ControllerError(
                "qualification history reconstruction tracking update failed"
            )
        if not passport_new:
            migrated = self.migrate_passport(
                claim, "preserve", expected_head=new_head,
            )
            passport = self.authenticated_operator_passport(ticket)
            if (
                migrated.get("status") != "ok"
                or passport is None
                or passport.get("head_sha") != new_head
                or passport.get("migration_history", [])[-1].get(
                    "rewrite_authorization_sha256"
                ) != record_digest
            ):
                raise ControllerError(
                    "qualification history reconstruction passport migration failed"
                )
        final_status = self.cell_git(
            claim, "status", "--porcelain=v1", "-z", "--untracked-files=all",
            "--ignore-submodules=none",
        )
        if final_status.returncode or final_status.stdout:
            raise ControllerError(
                "qualification history reconstruction left a dirty cell"
            )
        self.event_once(
            "qualification_history_reconstructed", ticket,
            old_head=old_head, new_head=new_head,
            reconstruction_sha256=record_digest,
            transition_receipt_sha256=blocked_receipt,
        )
        return {
            "new_head": new_head, "old_head": old_head,
            "record_sha256": record_digest, "schema": SCHEMA,
            "status": "repaired", "ticket": ticket,
        }

    @staticmethod
    def reviewer_void_records(text: str) -> list[int]:
        broad = [
            int(item) for item in re.findall(
                r"^\s*OPERATOR NOTE:\s*reviewer run\s*(\d+)\s+"
                r"void[^A-Za-z0-9]*duplicate\s*$",
                text, re.I | re.M,
            )
        ]
        exact = [
            int(item) for item in re.findall(
                r"^OPERATOR NOTE: reviewer run ([1-9][0-9]*) "
                r"void — duplicate$",
                text, re.M,
            )
        ]
        if broad != exact or len(exact) != len(set(exact)):
            raise ControllerError("reviewer void ticket is invalid")
        return exact

    def reviewer_void_evidence(
        self, transition: dict[str, Any], passport: dict[str, Any],
        before: str, ordinal: int,
    ) -> dict[str, Any]:
        stage = re.fullmatch(
            r"REFUSE reviewer has ([1-9][0-9]*) non-void successful "
            r"run\(s\) but only ([0-9]+) verdict\(s\) are logged on .+ — "
            r"record the missing verdict, or mark a duplicate successful row "
            r"with 'OPERATOR NOTE: reviewer run <ledger ordinal> void — "
            r"duplicate'",
            transition.get("stage", ""),
        )
        all_verdicts = [
            int(number) for number, _verdict in re.findall(
                r"^\s*reviewer round\s+([1-9][0-9]*):\s*"
                r"(APPROVE|REQUEST CHANGES(?:\s+—\s+.*)?)\s*$",
                before, re.I | re.M,
            )
        ]
        epoch = self.epoch_ticket(transition["ticket"], before)
        verdicts = [
            int(number) for number, _verdict in re.findall(
                r"^\s*reviewer round\s+([1-9][0-9]*):\s*"
                r"(APPROVE|REQUEST CHANGES(?:\s+—\s+.*)?)\s*$",
                epoch, re.I | re.M,
            )
        ]
        baseline = len(all_verdicts) - len(verdicts)
        voids = self.reviewer_void_records(epoch)
        completed = passport.get("completed_role_evidence")
        expected = {
            "contract_version", "factory_sha", "head_before",
            "manifest_sha256", "output_sha256", "role", "run_id",
            "transition_receipt_sha256",
        }
        roles = {
            "planner", "spec-linter", "test-author", "builder", "reviewer",
            "narrator",
        }
        run_ids: set[str] = set()
        receipts: set[str] = set()
        if not isinstance(completed, list):
            raise ControllerError("reviewer void evidence is invalid")
        for item in completed:
            if (
                not isinstance(item, dict)
                or set(item) != expected
                or item.get("contract_version") not in CONTROLLER_CONTRACTS
                or not SHA.fullmatch(item.get("factory_sha", ""))
                or not SHA.fullmatch(item.get("head_before", ""))
                or not DIGEST.fullmatch(item.get("manifest_sha256", ""))
                or not DIGEST.fullmatch(item.get("output_sha256", ""))
                or item.get("role") not in roles
                or not re.fullmatch(
                    r"[A-Za-z0-9._-]{1,200}", item.get("run_id", ""),
                )
                or not DIGEST.fullmatch(
                    item.get("transition_receipt_sha256", ""),
                )
                or item["run_id"] in run_ids
                or item["transition_receipt_sha256"] in receipts
            ):
                raise ControllerError("reviewer void evidence is invalid")
            run_ids.add(item["run_id"])
            receipts.add(item["transition_receipt_sha256"])
        reviewers = [item for item in completed if item["role"] == "reviewer"]
        if (
            stage is None
            or all_verdicts != list(range(1, len(all_verdicts) + 1))
            or verdicts != list(range(baseline + 1, len(all_verdicts) + 1))
            or any(item < 1 or item > len(reviewers) for item in voids)
            or len(reviewers) - len(voids) != int(stage[1])
            or int(stage[1]) != int(stage[2]) + 1
            or int(stage[2]) != len(verdicts)
            or isinstance(ordinal, bool)
            or ordinal < 1
            or ordinal > len(reviewers)
            or ordinal in voids
        ):
            raise ControllerError("reviewer void evidence is invalid")
        return reviewers[ordinal - 1]

    def exact_reviewer_void_commit(
        self, claim: dict[str, Any], before: str, after: str, ordinal: int,
    ) -> bool:
        if not self.exact_ticket_commit(claim, before, after):
            return False
        ticket_path = f"factory/tickets/{claim['ticket']}.md"
        old = self.cell_git(claim, "show", f"{before}:{ticket_path}")
        new = self.cell_git(claim, "show", f"{after}:{ticket_path}")
        line = f"OPERATOR NOTE: reviewer run {ordinal} void — duplicate"
        appended = {
            old.stdout + line,
            old.stdout + line + "\n",
            old.stdout + "\n" + line,
            old.stdout + "\n" + line + "\n",
        }
        try:
            old_voids = self.reviewer_void_records(
                self.epoch_ticket(claim["ticket"], old.stdout)
            )
            new_voids = self.reviewer_void_records(
                self.epoch_ticket(claim["ticket"], new.stdout)
            )
        except ControllerError:
            return False
        return (
            old.returncode == new.returncode == 0
            and ordinal not in old_voids
            and new_voids == [*old_voids, ordinal]
            and new.stdout in appended
        )

    def reviewer_void_plan(
        self, ticket: str, ordinal: int, operator_id: str,
    ) -> tuple[dict[str, Any], dict[str, Any], str, str]:
        if (
            not TICKET.fullmatch(ticket)
            or isinstance(ordinal, bool)
            or ordinal < 1
            or not OPERATOR_ID.fullmatch(operator_id)
            or operator_id == "auto"
        ):
            raise ControllerError("reviewer void request is invalid")
        claim = self.operator_control_claim(ticket, "reviewer void")
        released = (
            claim.get("lease") == "" and "lease_released" not in claim
            or DIGEST.fullmatch(claim.get("lease", "")) is not None
            and claim.get("lease_released") is True
        )
        transition = self.transition_receipt(claim, record=False)
        passport = self.authenticated_operator_passport(ticket)
        passport_path = self.state / "passports" / f"{ticket}.json"
        if (
            claim.get("status") != "blocked"
            or claim.get("blocked_reason") != "state-machine-refusal"
            or claim.get("parked") is not True
            or not released
            or claim.get("receipt")
            or claim.get("role")
            or claim.get("publication_lease")
            or self.role_active(claim)
            or transition is None
            or transition.get("factory_sha") != self.release_path.name
            or transition.get("consumed") is not False
            or transition.get("role") is not None
            or transition.get("loop") is not None
            or not DIGEST.fullmatch(transition.get("lease_sha256", ""))
            or (
                DIGEST.fullmatch(claim.get("lease", "")) is not None
                and transition["lease_sha256"]
                != hashlib.sha256(claim["lease"].encode()).hexdigest()
            )
            or passport is None
            or passport.get("ticket") != ticket
            or passport.get("project") != self.project
            or passport.get("contract_version") not in CONTROLLER_CONTRACTS
            or passport.get("factory_sha") != self.release_path.name
            or passport.get("current_state") != "Review"
            or passport.get("branch") != claim.get("branch")
            or transition.get("head_sha") != passport.get("head_sha")
            or transition.get("passport_sha256")
            != hashlib.sha256(passport_path.read_bytes()).hexdigest()
            or transition.get("route_plan_sha256")
            != passport.get("route_plan_sha256")
        ):
            raise ControllerError("reviewer void authority is unavailable")
        ticket_path = f"factory/tickets/{ticket}.md"
        before = self.cell_git(
            claim, "show", f"{passport['head_sha']}:{ticket_path}",
        )
        if before.returncode:
            raise ControllerError("reviewer void ticket is unavailable")
        selected = self.reviewer_void_evidence(
            transition, passport, before.stdout, ordinal,
        )
        line = f"OPERATOR NOTE: reviewer run {ordinal} void — duplicate"
        after = before.stdout + ("" if before.stdout.endswith("\n") else "\n") + line + "\n"
        observed_status = self.operator_ticket_change_status(
            claim, passport, after,
            lambda parent, child: self.exact_reviewer_void_commit(
                claim, parent, child, ordinal,
            ),
            "reviewer void",
        )
        plan = {
            "branch": claim["branch"],
            "claim_sha256": hashlib.sha256(canonical_document(claim)).hexdigest(),
            "factory_sha": self.release_path.name,
            "operator_id": operator_id,
            "parent_sha": passport["head_sha"],
            "passport_file_sha256": hashlib.sha256(
                passport_path.read_bytes()
            ).hexdigest(),
            "passport_sha256": passport["passport_sha256"],
            "project": self.project,
            "reviewer_run": selected,
            "run_ordinal": ordinal,
            "schema": SCHEMA,
            "ticket": ticket,
            "ticket_after_sha256": hashlib.sha256(after.encode()).hexdigest(),
            "ticket_before_sha256": hashlib.sha256(before.stdout.encode()).hexdigest(),
            "transition_lease_sha256": transition["lease_sha256"],
            "transition_receipt_sha256": transition["receipt_sha256"],
            "void_line": line,
            "worktree": claim["worktree"],
        }
        plan["approval_hash"] = hashlib.sha256(canonical_document(plan)).hexdigest()
        return plan, claim, after, observed_status

    def plan_reviewer_void(
        self, ticket: str, ordinal: int, operator_id: str,
    ) -> dict[str, Any]:
        plan, _claim, _after, observed_status = self.reviewer_void_plan(
            ticket, ordinal, operator_id,
        )
        return {**plan, "status": observed_status}

    def apply_reviewer_void(
        self, ticket: str, ordinal: int, operator_id: str, approve_hash: str,
    ) -> dict[str, Any]:
        plan, claim, after, observed_status = self.reviewer_void_plan(
            ticket, ordinal, operator_id,
        )
        if (
            not DIGEST.fullmatch(approve_hash)
            or approve_hash != plan["approval_hash"]
        ):
            raise ControllerError("reviewer void approval hash does not match")
        head = self.apply_operator_ticket_change(
            claim, plan, after, observed_status, operator_id,
            f"Mark reviewer run {ordinal} void for {ticket}",
            lambda parent, child: self.exact_reviewer_void_commit(
                claim, parent, child, ordinal,
            ),
            "reviewer void",
        )
        return {
            "approval_hash": approve_hash, "run_ordinal": ordinal,
            "schema": SCHEMA, "status": "applied", "ticket": ticket,
            "void_head": head,
        }

    def stranded_semantic_evidence(
        self, claim: dict[str, Any], terminal: dict[str, str],
        passport: dict[str, Any],
    ) -> tuple[str, str] | None:
        input_head = terminal.get("role_head_before", "")
        run_id = terminal.get("run_id", "")
        expected = (run_id, "spec-linter", claim.get("receipt"))
        records = lambda name: [
            (
                item.get("run_id"), item.get("role"),
                item.get("transition_receipt_sha256"),
            )
            for item in passport.get(name, [])
            if isinstance(item, dict)
        ]
        charges = [
            item for item in passport.get("charge_records", [])
            if isinstance(item, dict)
            and (item.get("run_id"), item.get("role"),
                 item.get("transition_receipt_sha256")) == expected
        ]
        manifest = self.product / "factory/runs" / f"{run_id}.meta"
        try:
            manifest_info = manifest.lstat()
            manifest_digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
        except OSError:
            manifest_info = None
            manifest_digest = ""
        migrations = passport.get("migration_history")
        release_history = passport.get("factory_release_history")
        source_release = {
            "contract_version": "1.8.0", "factory_sha": T198_FACTORY_SHA,
        }
        current_release = {
            "contract_version": "1.8.0",
            "factory_sha": self.release_path.name,
        }
        starts = [
            item for item in migrations or []
            if isinstance(item, dict)
            and item.get("from_head_sha") == input_head
        ]
        if passport.get("head_sha") == input_head:
            authorization, _reason = self.semantic_authorization_head(
                claim, passport,
            )
        else:
            authorization = (
                starts[0].get("to_head_sha") if len(starts) == 1 else None
            )
        diagnostic = self.cell_git(
            claim, "rev-parse", "--verify",
            f"refs/factory/failed-role/{claim['ticket']}/{run_id}^{{commit}}",
        )
        diagnostic_head = diagnostic.stdout.strip()
        migrated = passport.get("head_sha") != input_head
        first = starts[0] if len(starts) == 1 else {}
        if (
            claim.get("status") != "blocked"
            or claim.get("ticket") != "T-198"
            or claim.get("receipt") != T198_RECEIPT
            or claim.get("role") != "spec-linter"
            or terminal.get("role") != "spec-linter"
            or terminal.get("kit_sha") != T198_FACTORY_SHA
            or run_id != T198_RUN_ID
            or terminal.get("role_branch_before") != claim.get("branch")
            or terminal.get("role_remote_before") != input_head
            or terminal.get("kit_sha") == self.release_path.name
            or terminal.get("phase") != "completed"
            or terminal.get("accounting_state") != "abandoned_conservative"
            or terminal.get("go_issued") != "1"
            or terminal.get("task_submitted") != "1"
            or terminal.get("exit_status") != "11"
            or terminal.get("role_exit")
            != "role_exit_protected_ticket_mutation"
            or terminal.get("cost_basis") != "conservative_reservation"
            or terminal.get("effective_cost") != terminal.get("reserved_usd")
            or not re.fullmatch(
                r"(?:0|[1-9][0-9]{0,6})(?:\.[0-9]{1,18})?",
                terminal.get("reserved_usd", ""),
            )
            or int(terminal.get("reserved_usd", "0").replace(".", "")) <= 0
            or not SHA.fullmatch(input_head)
            or not SHA.fullmatch(terminal.get("kit_sha", ""))
            or not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", run_id,
            )
            or passport.get("branch") != claim.get("branch")
            or passport.get("transition_receipt_sha256") != claim.get("receipt")
            or records("charge_records").count(expected) != 1
            or len(charges) != 1
            or manifest_info is None
            or manifest.is_symlink()
            or not stat.S_ISREG(manifest_info.st_mode)
            or manifest_info.st_nlink != 1
            or charges[0].get("factory_sha") != terminal.get("kit_sha")
            or charges[0].get("contract_version") != "1.8.0"
            or charges[0].get("head_before") != input_head
            or charges[0].get("manifest_sha256") != manifest_digest
            or records("completed_role_evidence").count(expected) != 0
            or authorization is None
            or diagnostic.returncode
            or not SHA.fullmatch(diagnostic_head)
            or diagnostic_head == authorization
            or not self.exact_ticket_commit(
                claim, input_head, authorization, authorization=True,
            )
            or not self.exact_ticket_commit(
                claim, input_head, diagnostic_head,
            )
            or (
                migrated
                and (
                    release_history != [source_release, current_release]
                    or not isinstance(migrations, list)
                    or not 1 <= len(migrations) <= 2
                    or migrations[0] != first
                    or passport.get("factory_sha") != self.release_path.name
                    or first.get("from_factory_sha") != terminal.get("kit_sha")
                    or first.get("to_factory_sha") != self.release_path.name
                    or first.get("from_route_plan_sha256")
                    != first.get("to_route_plan_sha256")
                )
            )
            or (
                not migrated
                and (
                    passport.get("factory_sha") != terminal.get("kit_sha")
                    or migrations != []
                    or release_history != [source_release]
                )
            )
        ):
            return None
        return input_head, authorization

    def exact_stranded_semantic_authorization(
        self, claim: dict[str, Any], terminal: dict[str, str],
        passport: dict[str, Any],
    ) -> str | None:
        evidence = self.stranded_semantic_evidence(claim, terminal, passport)
        return (
            evidence[1]
            if evidence and passport.get("head_sha") == evidence[0] else None
        )

    def migrated_stranded_semantic_authorization(
        self, claim: dict[str, Any], terminal: dict[str, str],
        passport: dict[str, Any],
    ) -> str | None:
        evidence = self.stranded_semantic_evidence(claim, terminal, passport)
        return (
            evidence[1]
            if evidence and passport.get("head_sha") != evidence[0] else None
        )

    @staticmethod
    def semantic_import_migration(
        before: dict[str, Any], after: dict[str, Any], target: str,
        factory_sha: str,
    ) -> bool:
        old = before.get("migration_history")
        new = after.get("migration_history")
        edge = (
            new[-1]
            if isinstance(old, list) and isinstance(new, list)
            and len(new) == len(old) + 1 and new[:-1] == old else {}
        )
        return (
            isinstance(edge, dict)
            and edge.get("schema") == PASSPORT_MIGRATION_SCHEMA
            and edge.get("from_factory_sha") == before.get("factory_sha")
            and edge.get("from_head_sha") == before.get("head_sha")
            and edge.get("from_route_plan_sha256")
            == edge.get("to_route_plan_sha256")
            == before.get("route_plan_sha256")
            and edge.get("to_factory_sha") == after.get("factory_sha")
            == factory_sha
            and edge.get("to_head_sha") == after.get("head_sha") == target
            and after.get("charge_records") == before.get("charge_records")
            and after.get("completed_role_evidence")
            == before.get("completed_role_evidence")
        )

    @staticmethod
    def operator_import_migration(
        passport: dict[str, Any], source: str, target: str, factory_sha: str,
        source_file_sha256: str, route_plan_sha256: str,
    ) -> bool:
        history = passport.get("migration_history")
        edge = history[-1] if isinstance(history, list) and history else {}
        return (
            isinstance(edge, dict)
            and edge.get("schema") == PASSPORT_MIGRATION_SCHEMA
            and edge.get("from_factory_sha")
            == edge.get("to_factory_sha")
            == passport.get("factory_sha")
            == factory_sha
            and edge.get("from_head_sha") == source
            and edge.get("to_head_sha") == passport.get("head_sha") == target
            and edge.get("from_protected_base_sha")
            == edge.get("to_protected_base_sha")
            == passport.get("protected_base_sha")
            and edge.get("from_route_plan_sha256")
            == edge.get("to_route_plan_sha256")
            == passport.get("route_plan_sha256")
            == route_plan_sha256
            and edge.get("from_passport_file_sha256")
            == passport.get("parent_file_sha256")
            == source_file_sha256
            and edge.get("from_passport_sha256")
            == passport.get("parent_digest")
        )

    def exact_route_migration_commit(
        self, claim: dict[str, Any], before: str, after: str,
        *, migration: dict[str, Any] | None = None,
    ) -> bool:
        ticket_path = f"factory/tickets/{claim['ticket']}.md"
        route_path = f"factory/route-plans/{claim['ticket']}.json"
        pin_path = "factory/KIT_PIN"
        parents = self.cell_git(
            claim, "rev-list", "--parents", "-n", "1", after,
        )
        paths = self.cell_git(
            claim, "diff-tree", "--no-commit-id", "--name-only",
            "--no-renames", "-r", after,
        )
        old_route = self.cell_git(claim, "show", f"{before}:{route_path}")
        route = self.cell_git(claim, "show", f"{after}:{route_path}")
        old_ticket = self.cell_git(claim, "show", f"{before}:{ticket_path}")
        ticket = self.cell_git(claim, "show", f"{after}:{ticket_path}")
        author = self.cell_git(
            claim, "show", "-s", "--format=%an%x00%ae", after,
        )
        try:
            route_value = json.loads(route.stdout)
            if not isinstance(route_value, dict):
                return False
            current_migration = (
                route_value.get("kit_sha") == self.release_path.name
            )
            old_pin = (
                self.cell_git(claim, "show", f"{before}:{pin_path}")
                if current_migration
                else subprocess.CompletedProcess((), 0, "", "")
            )
            expected_paths = (
                (ticket_path, route_path, pin_path)
                if current_migration
                and old_pin.stdout != self.release_path.name + "\n"
                else (ticket_path, route_path)
            )
            controlled_paths = (
                (ticket_path, route_path, pin_path)
                if current_migration else expected_paths
            )
            modes = self.cell_git(
                claim, "ls-tree", after, "--", *controlled_paths,
            )
            pin = (
                self.cell_git(claim, "show", f"{after}:{pin_path}")
                if current_migration
                else subprocess.CompletedProcess((), 0, "", "")
            )
            old_route_value = (
                json.loads(old_route.stdout) if migration is not None else None
            )
            if migration is None:
                validate_route(
                    self.product, Path(claim["worktree"]), claim["ticket"],
                    self.release_path.name,
                )
        except (json.JSONDecodeError, OSError, RouteEvidenceError, ValueError):
            return False
        ticket_raw = ticket.stdout.encode()
        return (
            not any(item.returncode for item in (
                parents, paths, modes, old_route, route, old_ticket, ticket,
                old_pin, pin, author,
            ))
            and parents.stdout.split() == [after, before]
            and author.stdout.rstrip("\n")
            == "Software Factory\x00factory@local"
            and sorted(paths.stdout.splitlines())
            == sorted(expected_paths)
            and len(modes.stdout.splitlines()) == len(controlled_paths)
            and all(
                line.startswith("100644 blob ")
                for line in modes.stdout.splitlines()
            )
            and journal_extends(
                old_route.stdout.encode(), route.stdout.encode(),
            )
            and exact_kit_sha_change(
                old_ticket.stdout.encode(), ticket_raw,
            )
            and re.findall(
                rb"^Kit-SHA:\s*([0-9a-f]{40})\s*$", ticket_raw, re.M,
            ) == [route_value.get("kit_sha", "").encode()]
            and (
                not current_migration
                or (
                    SHA.fullmatch(old_pin.stdout.rstrip("\n")) is not None
                    and old_pin.stdout.count("\n") == 1
                    and pin.stdout == route_value.get("kit_sha", "") + "\n"
                )
            )
            and (
                migration is None
                or valid_v2_migration(migration)
                and migration["from_factory_sha"]
                == migration["to_factory_sha"]
                and migration["from_head_sha"] == before
                and migration["to_head_sha"] == after
                and migration["from_protected_base_sha"]
                == migration["to_protected_base_sha"]
                and migration["from_route_plan_sha256"]
                == hashlib.sha256(old_route.stdout.encode()).hexdigest()
                and migration["to_route_plan_sha256"]
                == hashlib.sha256(route.stdout.encode()).hexdigest()
                and isinstance(old_route_value, dict)
                and old_route_value.get("ticket")
                == route_value.get("ticket") == claim["ticket"]
                and route_value.get("kit_sha")
                == migration["to_factory_sha"]
                and re.findall(
                    rb"^Kit-SHA:\s*([0-9a-f]{40})\s*$",
                    old_ticket.stdout.encode(), re.M,
                ) == [old_route_value.get("kit_sha", "").encode()]
            )
        )

    def migrate_stranded_route_upgrade(
        self, claim: dict[str, Any], authorization: str, pending: str,
    ) -> str:
        route_path = f"factory/route-plans/{claim['ticket']}.json"
        ticket_path = f"factory/tickets/{claim['ticket']}.md"
        authorized_route = self.cell_git(
            claim, "show", f"{authorization}:{route_path}",
        )
        authorized_ticket = self.cell_git(
            claim, "show", f"{authorization}:{ticket_path}",
        )
        local = self.cell_git(claim, "rev-parse", "HEAD")
        local_head = local.stdout.strip()
        before_status, observed_head, before_remote = (
            self.remote_cell_head_status(claim)
        )
        replay = (
            SHA.fullmatch(local_head) is not None
            and local_head != authorization
            and self.ticket_release_current(claim)
            and self.exact_route_migration_commit(
                claim, authorization, local_head,
            )
        )
        if local_head == authorization:
            source_route = authorized_route
            expected_remote = (
                before_status == "pushed"
                and observed_head == before_remote == authorization
            )
        elif replay:
            source_route = self.cell_git(
                claim, "show", f"{local_head}:{route_path}",
            )
            expected_remote = (
                before_status == "resume_commit_not_pushed"
                and observed_head == local_head
                and before_remote == authorization
                or before_status == "pushed"
                and observed_head == before_remote == local_head
            )
        else:
            raise ControllerError("stranded route migration source is invalid")
        try:
            route = json.loads(source_route.stdout)
            authorized = json.loads(authorized_route.stdout)
            marker = read(self.state / f"{pending}.json")
        except (json.JSONDecodeError, OSError, UnicodeError) as error:
            raise ControllerError(
                "stranded route migration source is invalid"
            ) from error
        if not isinstance(route, dict) or not isinstance(authorized, dict):
            raise ControllerError("stranded route migration source is invalid")
        revisions = route.get("revisions")
        if not replay and route.get("schema") == "ticket-model-route-plan/v1":
            revision_count = 2
        elif (
            route.get("schema") == "ticket-model-route-journal/v2"
            and isinstance(revisions, list) and revisions
        ):
            revision_count = len(revisions) if replay else len(revisions) + 1
        else:
            raise ControllerError("stranded route migration source is invalid")
        source_raw = source_route.stdout.encode()
        expected_marker = {
            "factory_sha": self.release_path.name,
            "schema": EVENT_SCHEMA,
            "ticket": claim["ticket"],
        }
        if (
            local.returncode
            or not expected_remote
            or source_route.returncode
            or authorized_route.returncode
            or authorized_ticket.returncode
            or not SHA.fullmatch(authorization)
            or authorized.get("ticket") != claim["ticket"]
            or authorized.get("kit_sha") != T198_FACTORY_SHA
            or route.get("ticket") != claim["ticket"]
            or route.get("kit_sha")
            != (self.release_path.name if replay else T198_FACTORY_SHA)
            or re.findall(
                r"^Kit-SHA:\s*([0-9a-f]{40})\s*$",
                authorized_ticket.stdout, re.M,
            ) != [T198_FACTORY_SHA]
            or marker != expected_marker
        ):
            raise ControllerError("stranded route migration source is invalid")
        preview = self.json_call(
            "models", "migrate-plan", "--ticket", claim["ticket"],
            "--workdir", claim["worktree"], "--json", timeout=None,
        )
        preview_keys = {
            "journal_kit_sha", "journal_revision_count",
            "journal_tail_sha256", "preview_hash", "readiness_sha256",
            "schema", "source_document_sha256", "ticket",
        }
        if (
            set(preview) != preview_keys
            or preview.get("schema")
            != "ticket-model-route-migration-preview/v1"
            or preview.get("ticket") != claim["ticket"]
            or preview.get("journal_kit_sha") != self.release_path.name
            or preview.get("journal_revision_count") != revision_count
            or preview.get("source_document_sha256")
            != hashlib.sha256(source_raw).hexdigest()
            or any(
                DIGEST.fullmatch(preview.get(key, "")) is None
                for key in (
                    "journal_tail_sha256", "preview_hash",
                    "readiness_sha256", "source_document_sha256",
                )
            )
        ):
            raise ControllerError(
                "stranded route migration preview is invalid"
            )
        result = self.json_call(
            "models", "migrate", "--ticket", claim["ticket"],
            "--workdir", claim["worktree"],
            "--approve-hash", preview["preview_hash"],
            "--readiness-hash", preview["readiness_sha256"],
            "--approved-by", "release-upgrade", "--json", timeout=None,
        )
        result_keys = preview_keys | {"approved_by", "commit_sha"}
        if result.get("recovered") is True:
            result_keys.add("recovered")
        commit = result.get("commit_sha", "")
        status, local_head, remote_head = self.remote_cell_head_status(claim)
        if (
            set(result) != result_keys
            or any(result.get(key) != value for key, value in preview.items())
            or result.get("approved_by") != "release-upgrade"
            or not SHA.fullmatch(commit)
            or status != "pushed"
            or local_head != remote_head
            or commit != local_head
            or not self.ticket_release_current(claim)
            or not self.exact_route_migration_commit(
                claim, authorization, commit,
            )
        ):
            raise ControllerError("stranded route migration result is invalid")
        self.event_once(
            "stranded_route_migrated_by_release_upgrade", claim["ticket"],
            authorization_head=authorization, migration_head=commit,
            preview_hash=preview["preview_hash"],
            readiness_sha256=preview["readiness_sha256"],
        )
        return commit

    def recover_semantic_authorizations(
        self, claims: list[dict[str, Any]],
    ) -> None:
        for claim in claims:
            if (
                claim.get("status") not in {"claimed", "waiting"}
                or claim.get("status") == "waiting"
                and not str(claim.get("blocked_reason", "")).startswith(
                    "semantic-round-authorization:"
                )
                or claim.get("status") == "claimed"
                and claim.get("blocked_reason") is not None
                or claim.get("receipt")
                or claim.get("role")
                or claim.get("publication_lease")
                or self.role_active(claim)
            ):
                continue
            try:
                self.operator_control_worktree(claim, "semantic authorization")
            except ControllerError:
                continue
            transition = self.operator_transition(claim)
            context = (
                semantic_authorization_context(
                    transition.get("stage", ""), transition.get("loop"),
                )
                if transition is not None else None
            )
            if (
                transition is None
                or transition.get("factory_sha") != self.release_path.name
                or transition.get("consumed") is not False
                or transition.get("role") is not None
                or context is None
                or claim.get("status") == "waiting"
                and claim.get("blocked_reason")
                != semantic_block_reason(context[1], context[2])
            ):
                continue
            _line, role, semantic_round, semantic_kind = context
            passport = self.authenticated_operator_passport(claim["ticket"])
            if (
                passport is None
                or passport.get("factory_sha") != self.release_path.name
            ):
                continue
            path = self.state / "passports" / f"{claim['ticket']}.json"
            passport_file = hashlib.sha256(path.read_bytes()).hexdigest()
            local = self.cell_git(claim, "rev-parse", "HEAD").stdout.strip()
            dirty = self.cell_git(
                claim, "status", "--porcelain=v1", "-z",
            ).stdout
            branch = self.cell_git(
                claim, "symbolic-ref", "--quiet", "--short", "HEAD",
            )
            if branch.returncode or branch.stdout.strip() != claim["branch"]:
                if SHA.fullmatch(local):
                    event = semantic_authorization_event(
                        transition["stage"], local,
                        transition["receipt_sha256"], "branch_invalid",
                    )
                    if event is not None:
                        self.event_once(
                            event[0], claim["ticket"],
                            dedupe_fields=event[1], **event[2],
                        )
                continue
            if transition.get("head_sha") != passport.get("head_sha"):
                history = passport.get("migration_history")
                edge = (
                    history[-1]
                    if isinstance(history, list) and history else {}
                )
                target = passport.get("head_sha", "")
                if (
                    not isinstance(edge, dict)
                    or edge.get("schema") != PASSPORT_MIGRATION_SCHEMA
                    or not (
                        edge.get("from_factory_sha")
                        == edge.get("to_factory_sha")
                        == self.release_path.name
                    )
                    or edge.get("from_head_sha") != transition.get("head_sha")
                    or edge.get("to_head_sha") != target
                    or edge.get("from_passport_file_sha256")
                    != transition.get("passport_sha256")
                    or not (
                        edge.get("from_route_plan_sha256")
                        == edge.get("to_route_plan_sha256")
                        == transition.get("route_plan_sha256")
                    )
                    or not self.exact_ticket_commit(
                        claim, transition.get("head_sha", ""), target,
                        authorization=True, authorization_role=role,
                        semantic_round=semantic_round,
                        semantic_kind=semantic_kind,
                    )
                    or not self.operator_import_migration(
                        passport, transition.get("head_sha", ""), target,
                        self.release_path.name,
                        transition.get("passport_sha256", ""),
                        transition.get("route_plan_sha256", ""),
                    )
                    or not self.remote_passport_valid(claim)
                ):
                    continue
                self.event_once(
                    "semantic_round_authorization_imported", claim["ticket"],
                    head_sha=target, role=role,
                    semantic_round=semantic_round,
                )
                claim.update(status="claimed")
                claim.pop("blocked_reason", None)
                self.save_claim(claim)
                continue
            if local == passport.get("head_sha") and not dirty:
                continue
            target, reason_code = self.semantic_authorization_head(
                claim, passport, role, semantic_round, semantic_kind,
            )
            if (
                transition.get("passport_sha256") != passport_file
                or transition.get("route_plan_sha256")
                != passport.get("route_plan_sha256")
            ):
                continue
            if target is None:
                if SHA.fullmatch(local) and reason_code:
                    event = semantic_authorization_event(
                        transition["stage"], local,
                        transition["receipt_sha256"], reason_code,
                    )
                    if event is not None:
                        self.event_once(
                            event[0], claim["ticket"],
                            dedupe_fields=event[1], **event[2],
                        )
                continue
            self.ensure_lease(claim, "semantic-round-authorization")
            self.migrate_passport(claim, "preserve")
            migrated = self.authenticated_operator_passport(claim["ticket"])
            if (
                migrated is None
                or not self.semantic_import_migration(
                    passport, migrated, target, self.release_path.name,
                )
                or not self.operator_import_migration(
                    migrated, transition.get("head_sha", ""), target,
                    self.release_path.name,
                    transition.get("passport_sha256", ""),
                    transition.get("route_plan_sha256", ""),
                )
                or not self.remote_passport_valid(claim)
            ):
                raise ControllerError(
                    "semantic-round authorization passport migration is invalid"
                )
            self.event_once(
                "semantic_round_authorization_imported", claim["ticket"],
                head_sha=target, role=role, semantic_round=semantic_round,
            )
            claim.update(status="claimed")
            claim.pop("blocked_reason", None)
            self.save_claim(claim)

    def recover_reviewer_voids(self, claims: list[dict[str, Any]]) -> None:
        for claim in claims:
            released = (
                claim.get("lease") == "" and "lease_released" not in claim
                or DIGEST.fullmatch(claim.get("lease", "")) is not None
                and claim.get("lease_released") is True
            )
            if (
                claim.get("status") != "blocked"
                or claim.get("blocked_reason") != "state-machine-refusal"
                or claim.get("parked") is not True
                or not released
                or claim.get("receipt")
                or claim.get("role")
                or claim.get("publication_lease")
                or self.role_active(claim)
            ):
                continue
            try:
                self.operator_control_worktree(claim, "reviewer void")
            except ControllerError:
                continue
            transition = self.operator_transition(claim)
            passport = self.authenticated_operator_passport(claim["ticket"])
            if (
                transition is None
                or transition.get("factory_sha") != self.release_path.name
                or transition.get("consumed") is not False
                or transition.get("role") is not None
                or transition.get("loop") is not None
                or not DIGEST.fullmatch(transition.get("lease_sha256", ""))
                or (
                    DIGEST.fullmatch(claim.get("lease", "")) is not None
                    and transition["lease_sha256"]
                    != hashlib.sha256(claim["lease"].encode()).hexdigest()
                )
                or passport is None
                or passport.get("project") != self.project
                or passport.get("contract_version") not in CONTROLLER_CONTRACTS
                or passport.get("factory_sha") != self.release_path.name
                or passport.get("current_state") != "Review"
                or passport.get("branch") != claim.get("branch")
                or passport.get("route_plan_sha256")
                != transition.get("route_plan_sha256")
            ):
                continue
            parent = transition.get("head_sha", "")
            local = self.cell_git(claim, "rev-parse", "HEAD").stdout.strip()
            branch = self.cell_git(
                claim, "symbolic-ref", "--quiet", "--short", "HEAD",
            )
            dirty = self.cell_git(
                claim, "status", "--porcelain=v1", "-z",
            )
            remote_status, observed, remote = self.remote_cell_head_status(claim)
            ticket_path = f"factory/tickets/{claim['ticket']}.md"
            old = self.cell_git(claim, "show", f"{parent}:{ticket_path}")
            new = self.cell_git(claim, "show", f"{local}:{ticket_path}")
            try:
                old_voids = self.reviewer_void_records(
                    self.epoch_ticket(claim["ticket"], old.stdout)
                )
                new_voids = self.reviewer_void_records(
                    self.epoch_ticket(claim["ticket"], new.stdout)
                )
            except ControllerError:
                continue
            added = (
                new_voids[-1]
                if len(new_voids) == len(old_voids) + 1
                and new_voids[:-1] == old_voids else 0
            )
            if (
                not SHA.fullmatch(parent)
                or not SHA.fullmatch(local)
                or parent == local
                or branch.returncode
                or branch.stdout.strip() != claim["branch"]
                or dirty.returncode
                or dirty.stdout
                or remote_status != "pushed"
                or observed != remote
                or remote != local
                or old.returncode
                or new.returncode
                or not added
                or not self.exact_reviewer_void_commit(
                    claim, parent, local, added,
                )
            ):
                continue
            try:
                selected = self.reviewer_void_evidence(
                    transition, passport, old.stdout, added,
                )
            except ControllerError:
                continue
            passport_path = (
                self.state / "passports" / f"{claim['ticket']}.json"
            )
            before = passport
            if passport.get("head_sha") == parent:
                if (
                    transition.get("passport_sha256")
                    != hashlib.sha256(passport_path.read_bytes()).hexdigest()
                ):
                    continue
                migrated = self.migrate_passport(claim, "preserve")
                passport = self.authenticated_operator_passport(claim["ticket"])
                if (
                    migrated.get("status") != "ok"
                    or passport is None
                    or migrated.get("passport") != passport.get("passport_sha256")
                    or not self.semantic_import_migration(
                        before, passport, local, self.release_path.name,
                    )
                ):
                    raise ControllerError("reviewer void passport migration is invalid")
            elif passport.get("head_sha") == local:
                history = passport.get("migration_history")
                edge = history[-1] if isinstance(history, list) and history else {}
                if (
                    not isinstance(edge, dict)
                    or edge.get("schema") != PASSPORT_MIGRATION_SCHEMA
                    or not (
                        edge.get("from_factory_sha")
                        == edge.get("to_factory_sha")
                        == self.release_path.name
                    )
                    or edge.get("from_head_sha") != parent
                    or edge.get("to_head_sha") != local
                    or edge.get("from_passport_file_sha256")
                    != transition.get("passport_sha256")
                    or edge.get("from_route_plan_sha256")
                    != edge.get("to_route_plan_sha256")
                    or edge.get("to_route_plan_sha256")
                    != transition.get("route_plan_sha256")
                    or passport.get("parent_file_sha256")
                    != transition.get("passport_sha256")
                    or passport.get("parent_digest")
                    != edge.get("from_passport_sha256")
                ):
                    continue
            else:
                continue
            if (
                not self.operator_import_migration(
                    passport, parent, local, self.release_path.name,
                    transition.get("passport_sha256", ""),
                    transition.get("route_plan_sha256", ""),
                )
                or not self.remote_passport_valid(claim)
            ):
                raise ControllerError("reviewer void passport migration is invalid")
            self.event_once(
                "reviewer_run_void_imported", claim["ticket"],
                head_sha=local, run_id=selected["run_id"], run_ordinal=added,
            )
            claim["status"] = "claimed"
            claim.pop("blocked_reason", None)
            self.save_claim(claim)

    def exact_semantic_authorization_recovery(
        self, claim: dict[str, Any], terminal: dict[str, str], *,
        validate_remote: bool = True,
    ) -> bool:
        passport = self.authenticated_operator_passport(claim["ticket"])
        evidence = (
            self.stranded_semantic_evidence(claim, terminal, passport)
            if passport is not None else None
        )
        if evidence is None:
            return False
        input_head, authorization = evidence
        migrations = passport.get("migration_history")
        starts = [
            index for index, edge in enumerate(migrations or [])
            if isinstance(edge, dict)
            and edge.get("from_head_sha") == input_head
        ]
        suffix = migrations[starts[0]:] if len(starts) == 1 else []
        current = passport.get("head_sha", "")
        return (
            len(suffix) == 2
            and suffix[0].get("to_head_sha") == authorization
            and suffix[1].get("from_factory_sha")
            == suffix[1].get("to_factory_sha")
            == self.release_path.name
            and suffix[1].get("from_head_sha") == authorization
            and suffix[1].get("to_head_sha") == current
            and passport_head_lineage(passport, input_head)
            and successor_release_lineage(
                passport.get("factory_release_history"), migrations,
                terminal.get("kit_sha", ""), self.release_path.name,
            )
            and self.exact_route_migration_commit(
                claim, authorization, current,
            )
            and (
                not validate_remote or self.remote_passport_valid(claim)
            )
        )

    def route_migrated_failed_role(
        self, claim: dict[str, Any], terminal: dict[str, str],
        passport: dict[str, Any],
    ) -> bool:
        source = terminal.get("role_head_before", "")
        source_factory = terminal.get("kit_sha", "")
        migrations = passport.get("migration_history")
        starts = [
            index for index, edge in enumerate(migrations or [])
            if valid_v2_migration(edge)
            and edge["from_factory_sha"] == source_factory
            and edge["to_factory_sha"] != source_factory
            and edge["from_head_sha"] == source
        ]
        suffix = (
            migrations[starts[0]:]
            if isinstance(migrations, list) and len(starts) == 1 else []
        )
        final = suffix[-1] if suffix else {}
        return (
            bool(suffix)
            and all(valid_v2_migration(edge) for edge in suffix)
            and all(
                prior["to_factory_sha"] == following["from_factory_sha"]
                and prior["to_head_sha"] == following["from_head_sha"]
                and prior["to_protected_base_sha"]
                == following["from_protected_base_sha"]
                and prior["to_route_plan_sha256"]
                == following["from_route_plan_sha256"]
                for prior, following in zip(suffix, suffix[1:])
            )
            and all(
                edge["from_head_sha"] == edge["to_head_sha"]
                and edge["from_route_plan_sha256"]
                == edge["to_route_plan_sha256"]
                or edge["from_head_sha"] != edge["to_head_sha"]
                and self.exact_route_migration_commit(
                    claim, edge["from_head_sha"], edge["to_head_sha"],
                )
                or index == 0
                and self.failed_role_output_handoff(claim, terminal, edge)
                for index, edge in enumerate(suffix)
            )
            and successor_release_lineage(
                passport.get("factory_release_history"), migrations,
                source_factory, self.release_path.name, valid_v2_migration,
            )
            and final.get("to_factory_sha") == passport.get("factory_sha")
            == self.release_path.name
            and final.get("to_head_sha") == passport.get("head_sha")
            and final.get("to_protected_base_sha")
            == passport.get("protected_base_sha")
            and final.get("to_route_plan_sha256")
            == passport.get("route_plan_sha256")
            and final.get("from_passport_file_sha256")
            == passport.get("parent_file_sha256")
            and final.get("from_passport_sha256")
            == passport.get("parent_digest")
            and self.remote_passport_valid(claim)
        )

    def failed_role_output_handoff(
        self, claim: dict[str, Any], terminal: dict[str, str], edge: dict[str, Any],
    ) -> bool:
        if (
            terminal.get("role_exit") != "provider_failed"
            or terminal.get("task_submitted") != "1"
            or not terminal.get("route_id", "").startswith("cursor-")
            or terminal.get("role") != claim.get("role")
            or edge.get("from_factory_sha") != terminal.get("kit_sha")
            or edge.get("from_head_sha") != terminal.get("role_head_before")
            or edge.get("from_head_sha") == edge.get("to_head_sha")
            or edge.get("from_route_plan_sha256")
            != edge.get("to_route_plan_sha256")
        ):
            return False
        try:
            validate_committed_output(
                Path(claim["worktree"]),
                baseline=edge["from_head_sha"], head=edge["to_head_sha"],
                role=claim["role"], policy=_handoff_policy(claim["ticket"]),
            )
        except (HandoffError, OSError, TypeError, ValueError):
            return False
        return True

    def resume_push_failed_role(
        self, claim: dict[str, Any], terminal: dict[str, str],
    ) -> bool:
        status, local, remote = self.remote_cell_head_status(claim)
        after = terminal.get("role_head_after", "")
        if (
            after and local != after
            or terminal.get("kit_sha") == self.release_path.name
            and not SHA.fullmatch(after)
            or subprocess.run(
                [
                    "git", "-C", claim["worktree"], "status",
                    "--porcelain=v1", "-z",
                ],
                capture_output=True, check=False, timeout=120,
            ).stdout
        ):
            return False
        if status == "pushed":
            return local == remote
        before = terminal.get("role_head_before", "")
        if (
            status != "resume_commit_not_pushed"
            or terminal.get("role_branch_before") != claim["branch"]
            or terminal.get("role_remote_before") != before
            or not SHA.fullmatch(before)
            or remote != before
            or subprocess.run(
                [
                    "git", "-C", claim["worktree"], "merge-base",
                    "--is-ancestor", before, local,
                ],
                check=False, timeout=120,
            ).returncode
        ):
            return False
        push_exact_head(claim["worktree"], claim["branch"], local, before)
        return True

    def recover_repaired_failures(self, claims: list[dict[str, Any]]) -> None:
        for claim in claims:
            if (
                claim.get("blocked_reason") == "route-migration-required"
                and not self.ticket_release_current(claim)
            ):
                continue
            if self.restore_recorded_contract_repair(claim):
                continue
            self.restore_contract_blocker(claim)
            if (
                claim["status"] not in {"blocked", "claimed", "running"}
                or not claim.get("receipt")
                or self.role_active(claim)
            ):
                continue
            terminal = self.terminal_for_receipt(claim["ticket"], claim["receipt"])
            direct_model_identity_success = self.direct_model_identity_candidate(
                claim, terminal, claim["receipt"],
            )
            model_identity_success = (
                terminal is not None
                and claim.get("role") == "spec-linter"
                and terminal.get("role") == "spec-linter"
                and terminal.get("phase") == "completed"
                and terminal.get("accounting_state") == "abandoned_conservative"
                and terminal.get("go_issued") == "1"
                and terminal.get("task_submitted") == "1"
                and terminal.get("exit_status") == "9"
                and terminal.get("role_exit") == "provider_failed"
                and terminal.get("terminal_reason_code", "") == ""
                and terminal.get("adapter") == "cursor-anthropic"
                and terminal.get("route_id", "").startswith("cursor-")
                and terminal.get("role_branch_before") == claim.get("branch")
                and terminal.get("cost_basis") == "conservative_reservation"
                and terminal.get("effective_cost") == terminal.get("reserved_usd")
                and re.fullmatch(
                    r"(?:0|[1-9][0-9]{0,6})(?:\.[0-9]{1,18})?",
                    terminal.get("reserved_usd", ""),
                )
                and int(terminal["reserved_usd"].replace(".", "")) > 0
                and SHA.fullmatch(terminal.get("role_head_before", ""))
                and SHA.fullmatch(terminal.get("kit_sha", ""))
                and terminal["kit_sha"] != self.release_path.name
                and DIGEST.fullmatch(terminal.get("output_sha256", ""))
                and terminal.get("progress_events", "").isdigit()
                and int(terminal["progress_events"]) > 0
                and DIGEST.fullmatch(
                    terminal.get("progress_journal_sha256", "")
                )
                and re.fullmatch(
                    r"[A-Za-z0-9._:-]{1,200}",
                    terminal.get("provider_attempt_id", ""),
                )
                and re.fullmatch(
                    r"[A-Za-z0-9._-]{1,200}", terminal.get("run_id", ""),
                )
            )
            model_recovery_block = (
                "model-identity-recovery-refused:" + self.release_path.name
            )
            fallback_refusal = re.fullmatch(
                r"qualification-fallback-refused:"
                r"(?:readiness|manifest|attempt_count|handoff|route_policy|provenance|unknown):"
                + re.escape(self.release_path.name),
                claim.get("blocked_reason", ""),
            )
            if fallback_refusal is not None:
                continue
            if (
                (model_identity_success or direct_model_identity_success)
                and claim.get("blocked_reason") == model_recovery_block
            ):
                continue
            if direct_model_identity_success:
                try:
                    self.recover_direct_model_identity_success(
                        claim, terminal, claim["receipt"],
                    )
                except ModelIdentityEvidenceError as error:
                    if not model_identity_success:
                        self.block(claim, model_recovery_block)
                        self.release_ticket_lease(claim)
                        self.event_once(
                            "typed_recovery_refused", claim["ticket"],
                            recovery_kind="model_identity_success",
                            reason=safe_error(str(error)),
                        )
                        continue
                except (ControllerError, OSError, subprocess.SubprocessError) as error:
                    self.block(
                        claim,
                        "model-identity-delivery-retry:" + self.release_path.name,
                    )
                    self.release_ticket_lease(claim)
                    self.event_once(
                        "typed_recovery_refused", claim["ticket"],
                        recovery_kind="model_identity_delivery",
                        reason=safe_error(str(error)),
                    )
                    continue
                else:
                    continue
            if (
                self.qualification
                and terminal is not None
                and terminal.get("task_submitted") == "1"
                and terminal.get("role_exit") == "provider_failed"
                and terminal.get("route_id", "").startswith("cursor-")
                and not model_identity_success
            ):
                self.ensure_lease(claim, "provider-fallback-recovery")
                self.finish_pending_run(claim)
                continue
            push_failure = (
                terminal is not None
                and terminal.get("role_exit") == "role_exit_push_failed"
            )
            if push_failure:
                try:
                    if not self.resume_push_failed_role(claim, terminal):
                        continue
                except ExternalUnavailable:
                    claim["status"] = "blocked"
                    claim["blocked_reason"] = "external-unavailable"
                    self.save_claim(claim)
                    self.event_once(
                        "external_service_wait", claim["ticket"],
                        reason_code="external_unavailable",
                    )
                    continue
            quarantined_role_failure = (
                terminal is not None
                and terminal.get("phase") == "completed"
                and terminal.get("accounting_state") == "abandoned_conservative"
                and terminal.get("go_issued") == "1"
                and terminal.get("task_submitted") == "1"
                and terminal.get("exit_status") == "11"
                and terminal.get("role_exit") in {
                    "role_exit_history_rewritten",
                    "role_exit_protected_ticket_mutation",
                }
                and terminal.get("role") == claim.get("role")
                and claim.get("role") in {
                    "planner", "spec-linter", "builder", "narrator",
                }
                and terminal.get("cost_basis") == "conservative_reservation"
                and re.fullmatch(
                    r"(?:0|[1-9][0-9]{0,6})(?:\.[0-9]{1,18})?",
                    terminal.get("reserved_usd", ""),
                )
                and int(terminal["reserved_usd"].replace(".", "")) > 0
                and terminal.get("effective_cost")
                == terminal.get("reserved_usd")
                and SHA.fullmatch(terminal.get("role_head_before", ""))
                and SHA.fullmatch(terminal.get("kit_sha", ""))
                and terminal["kit_sha"] != self.release_path.name
            )
            history_rewrite = (
                quarantined_role_failure
                and terminal.get("role_exit") == "role_exit_history_rewritten"
            )
            protected_mutation = (
                quarantined_role_failure
                and terminal.get("role_exit")
                == "role_exit_protected_ticket_mutation"
            )
            interrupted_before_submission = (
                terminal is not None
                and terminal.get("phase") == "abandoned"
                and terminal.get("accounting_state") == "abandoned_conservative"
                and terminal.get("task_submitted") == "0"
                and terminal.get("exit_status") == "143"
                and not terminal.get("role_exit")
            )
            contract_blocked = (
                terminal is not None
                and terminal.get("role_exit") == "role_exit_contract_blocked"
                and terminal.get("exit_status") == "12"
            )
            submission_unconfirmed = (
                terminal is not None
                and terminal.get("phase") == "completed"
                and terminal.get("accounting_state") == "abandoned_conservative"
                and terminal.get("go_issued") == "1"
                and terminal.get("task_submitted") == "0"
                and terminal.get("exit_status") == "125"
                and terminal.get("role_exit") == "provider_failed"
                and (
                    terminal.get("terminal_reason_code", "")
                    == "adapter_submission_unconfirmed"
                    and DIGEST.fullmatch(terminal.get("output_sha256", ""))
                    or terminal.get("terminal_reason_code", "") == ""
                    and terminal.get("output_sha256")
                    == hashlib.sha256(b"").hexdigest()
                )
                and terminal.get("turns") == "0"
                and not terminal.get("progress_events")
                and terminal.get("cost_basis") == "conservative_reservation"
                and terminal.get("effective_cost")
                == terminal.get("reserved_usd")
                and SHA.fullmatch(terminal.get("kit_sha", ""))
                and terminal["kit_sha"] != self.release_path.name
            )
            converged_success = (
                terminal is not None
                and claim.get("role") == "builder"
                and terminal.get("role") == "builder"
                and terminal.get("phase") == "abandoned"
                and terminal.get("accounting_state") == "abandoned_conservative"
                and terminal.get("go_issued") == "1"
                and terminal.get("task_submitted") == "1"
                and terminal.get("exit_status") == "128"
                and terminal.get("role_exit") == ""
                and terminal.get("terminal_reason_code", "") == ""
                and terminal.get("adapter") in {
                    "cursor-anthropic", "cursor-openai",
                }
                and terminal.get("contract_version") == "1.8.0"
                and terminal.get("role_branch_before") == claim.get("branch")
                and terminal.get("cost_basis") == "conservative_reservation"
                and terminal.get("effective_cost") == terminal.get("reserved_usd")
                and re.fullmatch(
                    r"(?:0|[1-9][0-9]{0,6})(?:\.[0-9]{1,18})?",
                    terminal.get("reserved_usd", ""),
                )
                and int(terminal["reserved_usd"].replace(".", "")) > 0
                and SHA.fullmatch(terminal.get("role_head_before", ""))
                and SHA.fullmatch(terminal.get("kit_sha", ""))
                and terminal["kit_sha"] != self.release_path.name
                and DIGEST.fullmatch(terminal.get("output_sha256", ""))
                and terminal.get("progress_events", "") == ""
                and terminal.get("progress_journal_sha256", "") == ""
                and re.fullmatch(
                    r"[A-Za-z0-9._:-]{1,200}",
                    terminal.get("provider_attempt_id", ""),
                )
                and re.fullmatch(
                    r"[A-Za-z0-9._-]{1,200}", terminal.get("run_id", ""),
                )
            )
            if not (
                push_failure or interrupted_before_submission or contract_blocked
                or submission_unconfirmed or history_rewrite
                or protected_mutation or converged_success
                or model_identity_success
            ):
                continue
            passport_path = self.state / "passports" / f"{claim['ticket']}.json"
            if not passport_path.exists():
                continue
            if contract_blocked:
                migrated = False
                try:
                    passport_valid = self.remote_passport_valid(claim)
                except ControllerError:
                    passport_valid = False
                if not passport_valid:
                    try:
                        head_status, local_head, remote_head = (
                            self.remote_cell_head_status(claim)
                        )
                        try:
                            ticket_text = (
                                Path(claim["worktree"]) / "factory" / "tickets"
                                / f"{claim['ticket']}.md"
                            ).read_text(encoding="utf-8")
                        except (FileNotFoundError, OSError):
                            ticket_text = ""
                        directive_status = self.contract_resume_status(
                            claim, ticket_text,
                        )
                        if head_status != "pushed":
                            if (
                                head_status in CONTRACT_RESUME_REFUSALS
                                and directive_status != "waiting"
                            ):
                                self.record_contract_resume_refusal(
                                    claim, head_status, {
                                        "local_head": local_head,
                                        "remote_head": remote_head or None,
                                    },
                                )
                            continue
                        if directive_status not in {"ready", "waiting"}:
                            self.record_contract_resume_refusal(
                                claim, directive_status, {
                                    "local_head": local_head,
                                    "remote_head": remote_head,
                                },
                            )
                            continue
                        checked = self.json_call(
                            "state-machine", "repair-check",
                            "--ticket", claim["ticket"],
                            "--receipt", claim["receipt"],
                            "--workdir", claim["worktree"], "--json",
                            allow=(0, 1),
                        )
                        if checked.get("status") == "error":
                            reason_code = checked.get("reason_code")
                            if reason_code in CONTRACT_RESUME_REFUSALS:
                                self.record_contract_resume_refusal(
                                    claim, reason_code, {
                                        key: checked[key]
                                        for key in (
                                            "actual_bytes", "changed_path_count",
                                            "expected_bytes", "first_differing_line",
                                            "offending_parent",
                                        )
                                        if key in checked
                                    },
                                )
                                continue
                        if (
                            checked.get("action") != "repair-check"
                            or checked.get("head") != local_head
                            or checked.get("role") != claim["role"]
                            or checked.get("schema")
                            != "nysa.software-factory.state-machine/v1"
                            or checked.get("status") != directive_status
                            or checked.get("ticket") != claim["ticket"]
                        ):
                            raise ControllerError(
                                "contract repair validation is invalid"
                            )
                        self.migrate_passport(
                            claim, "preserve", checked["head"],
                        )
                        migrated = True
                        if not self.remote_passport_valid(claim):
                            continue
                    except (
                        ControllerError, json.JSONDecodeError, OSError,
                        subprocess.SubprocessError,
                    ):
                        continue
                if migrated:
                    self.event_once(
                        "contract_block_passport_migrated", claim["ticket"],
                        failed_run_id=terminal.get("run_id"),
                    )
            if push_failure or interrupted_before_submission:
                try:
                    if not self.terminal_already_exported(claim, terminal):
                        publication = read(passport_path).get(
                            "publication_state", ""
                        )
                        if publication not in {
                            "none", "validating", "ready", "merge-pending",
                            "merged", "repair",
                        }:
                            continue
                        try:
                            self.passport(claim, publication)
                        except ControllerError:
                            self.migrate_passport(claim, "preserve")
                            self.passport(claim, publication)
                except (
                    ControllerError, json.JSONDecodeError, OSError,
                ):
                    continue
            if (
                push_failure or interrupted_before_submission
                or submission_unconfirmed or history_rewrite
                or protected_mutation
            ):
                try:
                    if not self.terminal_already_exported(claim, terminal):
                        continue
                except ControllerError:
                    continue
            if history_rewrite or protected_mutation:
                try:
                    passport = read(passport_path)
                except (ControllerError, json.JSONDecodeError, OSError):
                    continue
                expected = (
                    terminal.get("run_id"), claim.get("role"), claim.get("receipt"),
                )
                completed = [
                    (
                        item.get("run_id"), item.get("role"),
                        item.get("transition_receipt_sha256"),
                    )
                    for item in passport.get("completed_role_evidence", [])
                ]
                semantic_authorization = (
                    protected_mutation
                    and passport.get("head_sha")
                    != terminal.get("role_head_before")
                    and self.exact_semantic_authorization_recovery(
                        claim, terminal,
                    )
                )
                route_migrated_failure = (
                    not semantic_authorization
                    and passport.get("head_sha")
                    != terminal.get("role_head_before")
                    and self.route_migrated_failed_role(
                        claim, terminal, passport,
                    )
                )
                if (
                    passport.get("head_sha") != terminal.get("role_head_before")
                    and not semantic_authorization
                    and not route_migrated_failure
                    or completed.count(expected) != 0
                ):
                    continue
            else:
                semantic_authorization = False
            if model_identity_success:
                try:
                    self.restore_model_identity_success(claim, terminal)
                    if (
                        not self.remote_passport_valid(claim)
                        or not self.converged_success_exported(claim, terminal)
                    ):
                        continue
                except (
                    ControllerError, json.JSONDecodeError, OSError,
                    subprocess.SubprocessError,
                ) as error:
                    detail = safe_error(error)
                    self.block(claim, model_recovery_block)
                    self.event_once(
                        "typed_recovery_refused", claim["ticket"],
                        failed_run_id=terminal["run_id"],
                        reason=detail,
                        recovery_kind="model_identity_success",
                    )
                    continue
            if converged_success:
                try:
                    if not self.remote_passport_valid(claim):
                        continue
                    self.correct_converged_success(claim, terminal)
                    if (
                        not self.remote_passport_valid(claim)
                        or not self.converged_success_exported(claim, terminal)
                    ):
                        continue
                except (
                    ControllerError, json.JSONDecodeError, OSError,
                    subprocess.SubprocessError,
                ):
                    continue
            if contract_blocked:
                self.ensure_lease(claim, "contract-block-resume")
                blocked = self.json_call(
                    "state-machine", "block", "--ticket", claim["ticket"],
                    "--lease", claim["lease"], "--receipt", claim["receipt"],
                    "--workdir", claim["worktree"], "--json",
                )
                if blocked.get("status") != "blocked":
                    raise ControllerError(
                        "state machine returned an invalid contract blocker"
                    )
                try:
                    ticket_text = (
                        Path(claim["worktree"]) / "factory" / "tickets"
                        / f"{claim['ticket']}.md"
                    ).read_text(encoding="utf-8")
                except (FileNotFoundError, OSError):
                    continue
                directive_status = self.contract_resume_status(
                    claim, ticket_text,
                )
                if directive_status == "waiting":
                    self.wait_for_recovery_receipt(claim)
                    continue
                if directive_status != "ready":
                    self.record_contract_resume_refusal(
                        claim, directive_status, {}
                    )
                    continue
                head_status, local_head, remote_head = self.remote_cell_head_status(
                    claim
                )
                if head_status != "pushed":
                    if head_status in CONTRACT_RESUME_REFUSALS:
                        self.record_contract_resume_refusal(
                            claim, head_status, {
                                "local_head": local_head,
                                "remote_head": remote_head or None,
                            },
                        )
                    continue
                resumed = self.json_call(
                    "state-machine", "resume", "--ticket", claim["ticket"],
                    "--receipt", claim["receipt"],
                    "--workdir", claim["worktree"], "--json",
                    allow=(0, 1),
                )
                if resumed.get("status") == "error":
                    reason_code = resumed.get("reason_code")
                    if reason_code in CONTRACT_RESUME_REFUSALS:
                        self.record_contract_resume_refusal(
                            claim, reason_code, {
                                key: resumed[key]
                                for key in (
                                    "actual_bytes", "changed_path_count",
                                    "expected_bytes", "first_differing_line",
                                    "offending_parent",
                                )
                                if key in resumed
                            },
                        )
                        continue
                if resumed.get("status") == "waiting":
                    self.wait_for_recovery_receipt(claim)
                    continue
                if resumed.get("status") != "ready":
                    raise ControllerError(
                        "state machine returned an invalid contract resume"
                    )
            try:
                valid_passport = self.remote_passport_valid(claim)
            except ControllerError:
                if not push_failure:
                    continue
                try:
                    self.migrate_passport(claim, "preserve")
                    valid_passport = self.remote_passport_valid(claim)
                except ControllerError:
                    continue
            if not valid_passport:
                continue
            reuse_handoff = (
                semantic_authorization
                and self.semantic_handoff_state(claim) == "ready"
            )
            persisted = (
                self.operator_transition(claim)
                if semantic_authorization else None
            )
            if (
                semantic_authorization
                and not reuse_handoff
                and persisted is not None
                and persisted.get("factory_sha") == self.release_path.name
            ):
                continue
            if not reuse_handoff:
                self.ensure_lease(claim, "repaired-role")
            failed_run = terminal.get("run_id", "")
            if semantic_authorization:
                transition = None
                if not reuse_handoff:
                    transition = self.json_call(
                        "state-machine", "--ticket", claim["ticket"],
                        "--lease", claim["lease"], "--workdir",
                        claim["worktree"], "--json", timeout=None,
                    )
                    persisted = self.operator_transition(claim)
                if (
                    persisted is None
                    or persisted.get("loop") != {
                        "attempt": 2, "capped": False,
                        "kind": "planner-spec-linter", "limit": 3,
                    }
                    or persisted.get("factory_sha") != self.release_path.name
                    or persisted.get("stage") != "RUN spec-linter"
                    or persisted.get("role") != "spec-linter"
                    or persisted.get("parent_digest") != claim.get("receipt")
                    or persisted.get("consumed") is not False
                    or (
                        transition is not None
                        and (
                            not valid_transition_evidence(
                                transition, claim["ticket"]
                            )
                            or transition.get("stage") != "RUN spec-linter"
                            or transition.get("role") != "spec-linter"
                            or transition.get("loop") != persisted.get("loop")
                            or persisted.get("receipt_sha256")
                            != transition.get("receipt")
                        )
                    )
                ):
                    raise ControllerError(
                        "semantic-round recovery transition is invalid"
                    )
                self.event_once(
                    "semantic_round_authorization_recovered_by_release_upgrade",
                    claim["ticket"], failed_run_id=failed_run,
                    transition_receipt_sha256=persisted["receipt_sha256"],
                )
            claim.update(receipt="", role="", status="claimed")
            claim.pop("blocked_reason", None)
            self.save_claim(claim)
            self.prior_transition_tickets.discard(claim["ticket"])
            if semantic_authorization:
                continue
            self.event(
                (
                    "push_failure_recovered"
                    if push_failure
                    else (
                        "contract_blocker_recovered"
                        if contract_blocked
                        else (
                            "submission_failure_recovered_by_release_upgrade"
                            if submission_unconfirmed
                            else (
                                "history_rewrite_recovered_by_release_upgrade"
                                if history_rewrite
                                else (
                                    (
                                        "semantic_round_authorization_recovered_by_release_upgrade"
                                        if semantic_authorization
                                        else "protected_ticket_mutation_recovered_by_release_upgrade"
                                    ) if protected_mutation
                                    else (
                                        "model_identity_success_recovered_by_release_upgrade"
                                        if model_identity_success
                                        else (
                                            "converged_success_recovered_by_release_upgrade"
                                            if converged_success
                                            else "interrupted_role_recovered"
                                        )
                                    )
                                )
                            )
                        )
                    )
                ),
                claim["ticket"],
                failed_run_id=failed_run,
            )

    def relocate_qualification_cell(self, claim: dict[str, Any]) -> None:
        if (
            not self.qualification
            or claim["ticket"] != self.qualification["tickets"][0]
            or self.marker("qualification-relocation")
        ):
            return
        source = Path(claim["worktree"])
        if subprocess.run(
            ["git", "-C", str(source), "status", "--porcelain=v1", "-z"],
            capture_output=True, check=True, timeout=120,
        ).stdout:
            raise ControllerError("qualification relocation requires a clean cell")
        destination = next(
            (
                source.parent / f"cell-{number}"
                for number in (5, 6)
                if not (source.parent / f"cell-{number}").exists()
            ),
            None,
        )
        if destination is None:
            raise ControllerError("qualification relocation has no clean destination")
        subprocess.run(
            ["git", "-C", str(self.product), "worktree", "move", str(source), str(destination)],
            check=True, timeout=120,
        )
        claim["worktree"] = str(destination)
        self.save_claim(claim)
        try:
            self.json_call(
                "passport", "validate", "--ticket", claim["ticket"],
                "--workdir", claim["worktree"], "--json",
            )
        except Exception:
            subprocess.run(
                [
                    "git", "-C", str(self.product), "worktree", "move",
                    str(destination), str(source),
                ],
                check=True, timeout=120,
            )
            claim["worktree"] = str(source)
            self.save_claim(claim)
            raise
        self.marker("qualification-relocation", {
            "from": str(source),
            "schema": EVENT_SCHEMA,
            "ticket": claim["ticket"],
            "to": str(destination),
        })
        self.event(
            "cell_relocated", claim["ticket"], from_worktree=str(source),
            to_worktree=str(destination),
        )

    def remove_cell(self, claim: dict[str, Any]) -> None:
        worktree = Path(claim["worktree"])
        if not worktree.exists():
            return
        subprocess.run(
            ["git", "-C", str(self.product), "worktree", "remove", str(worktree)],
            check=True, timeout=120,
        )
        self.event("cell_removed", claim["ticket"], worktree=str(worktree))

    def finish_pending_run(self, claim: dict[str, Any]) -> bool | None:
        """Return None for a live receipt, False for a terminal stop, else True."""
        if not claim.get("receipt"):
            return True
        if self.role_active(claim):
            self.observe_attempt_safely(claim)
            return None
        terminal = self.terminal_for_receipt(claim["ticket"], claim["receipt"])
        if terminal is None:
            claim.update(receipt="", role="", status="claimed")
            self.save_claim(claim)
            return True
        self.emit_attempt_terminal(claim, terminal)
        qualification_spend_limit = bool(
            self.qualification
            and terminal.get("accounting_state") in TERMINAL_ACCOUNTING
            and terminal.get("go_issued") == "1"
            and terminal.get("task_submitted") == "1"
            and terminal.get("exit_status") != "0"
            and terminal.get("role_exit") == "provider_failed"
            and terminal.get("terminal_reason_code") == "provider_spend_limit"
        )
        if qualification_spend_limit:
            self.latch_qualification_cohort_error()
        if self.repository_test:
            if (
                claim.get("role") == "planner"
                and terminal.get("phase") == "completed"
                and terminal.get("accounting_state") == "completed"
                and terminal.get("exit_status") == "0"
                and terminal.get("role_exit") == "ok"
            ):
                claim.update(receipt="", role="", status="claimed")
                self.save_claim(claim)
                return True
            claim["status"] = "blocked"
            claim["blocked_reason"] = "role-failure"
            self.save_claim(claim)
            self.release_ticket_lease(claim)
            return False
        if terminal.get("accounting_state") == "launch_void":
            if (
                self.typed_launch_void(terminal)
                and SHA.fullmatch(terminal.get("kit_sha", ""))
                and terminal["kit_sha"] != self.release_path.name
            ):
                prior = terminal["kit_sha"]
                run_id = terminal.get("run_id")
                claim.update(receipt="", role="", status="claimed")
                self.save_claim(claim)
                self.event(
                    "pre_go_failure_recovered_by_release_upgrade",
                    claim["ticket"], failed_run_id=run_id,
                    from_factory_sha=prior,
                )
                return True
            reason = terminal.get("terminal_reason_code", "")
            if not re.fullmatch(r"[a-z0-9_]{0,64}", reason):
                reason = "invalid_pre_go_reason"
            self.latch_qualification_cohort_error()
            claim["status"] = "blocked"
            claim["blocked_reason"] = "pre-go-failure"
            self.save_claim(claim)
            self.release_ticket_lease(claim)
            self.event(
                "pre_go_failure_blocked", claim["ticket"],
                failed_run_id=terminal.get("run_id"),
                reason=reason or "pre_go_failure",
            )
            return False
        publication = (
            "validating"
            if claim["role"] in {"reviewer", "narrator"}
            else "none"
        )
        qualification_fallback = (
            self.qualification
            and terminal.get("role_exit") == "provider_failed"
            and terminal.get("task_submitted") == "1"
            and terminal.get("route_id", "").startswith("cursor-")
        )
        if qualification_fallback and self.direct_model_identity_candidate(
            claim, terminal, claim["receipt"],
        ):
            try:
                self.recover_direct_model_identity_success(
                    claim, terminal, claim["receipt"],
                )
                return True
            except ModelIdentityEvidenceError as error:
                self.latch_qualification_cohort_error()
                self.block(
                    claim,
                    "model-identity-recovery-refused:" + self.release_path.name,
                )
                recovery_kind = "model_identity_success"
                recovery_reason = safe_error(str(error))
            except (ControllerError, OSError, subprocess.SubprocessError) as error:
                self.latch_qualification_cohort_error()
                self.block(
                    claim,
                    "model-identity-delivery-retry:" + self.release_path.name,
                )
                recovery_kind = "model_identity_delivery"
                recovery_reason = safe_error(str(error))
            self.release_ticket_lease(claim)
            self.event_once(
                "typed_recovery_refused", claim["ticket"],
                recovery_kind=recovery_kind,
                reason=recovery_reason,
            )
            return False
        terminal_failed = (
            terminal.get("exit_status") != "0"
            or terminal.get("role_exit") != "ok"
        )
        if (
            not qualification_fallback
            and terminal.get("role_exit") != "role_exit_invalid_output"
            and terminal_failed
        ):
            self.latch_qualification_cohort_error()
        if not qualification_fallback:
            dirty_spend_limit = False
            if qualification_spend_limit:
                status = self.cell_git(
                    claim, "status", "--porcelain=v1", "-z",
                )
                if status.returncode:
                    raise ControllerError(
                        "provider spend-limit cell status is unavailable"
                    )
                dirty_spend_limit = bool(status.stdout)
            if not dirty_spend_limit:
                if self.terminal_already_exported(claim, terminal):
                    self.migrate_passport(claim, publication)
                    self.event(
                        "terminal_export_recovered", claim["ticket"],
                        run_id=terminal.get("run_id"),
                    )
                else:
                    self.passport(claim, publication)
                self.archive_emergency_admission(claim, terminal)
        if (
            terminal.get("accounting_state") in {"cancelled", "cancelled_conservative"}
            or terminal.get("role_exit") == "cancelled"
        ):
            claim["status"] = "cancelled"
            self.release(claim)
            self.remove_cell(claim)
            self.event("attempt_cancelled", claim["ticket"])
            return False
        if terminal.get("role_exit") == "role_exit_invalid_output":
            self.migrate_passport(claim, publication)
            rejected_role = claim["role"]
            claim.update(receipt="", role="", status="claimed")
            self.save_claim(claim)
            self.event(
                "role_output_rejected", claim["ticket"], role=rejected_role,
                run_id=terminal.get("run_id"),
            )
            return True
        if terminal_failed:
            if qualification_fallback:
                try:
                    with self.fallback_lock:
                        result = self.json_call(
                            "models", "fallback-auto", "--ticket", claim["ticket"],
                            "--failed-run", terminal["run_id"],
                            "--workdir", claim["worktree"],
                            "--reason", "provider_unavailable", "--json",
                        )
                except ControllerError as error:
                    match = re.search(
                        r"automatic qualification fallback refused:"
                        r"(readiness|manifest|attempt_count|handoff|route_policy|provenance|unknown)",
                        str(error),
                    )
                    if match is None:
                        raise
                    reason_code = match.group(1)
                    self.latch_qualification_cohort_error()
                    self.block(
                        claim,
                        f"qualification-fallback-refused:{reason_code}:"
                        f"{self.release_path.name}",
                    )
                    self.release_ticket_lease(claim)
                    self.event_once(
                        "typed_recovery_refused", claim["ticket"],
                        failed_run_id=terminal["run_id"],
                        reason=reason_code,
                        recovery_kind="qualification_fallback",
                    )
                    return False
                if result.get("failed_run_id") != terminal["run_id"]:
                    raise ControllerError("provider fallback did not bind the failed run")
                self.migrate_passport(claim, publication)
                claim.update(receipt="", role="", status="claimed")
                self.save_claim(claim)
                self.event(
                    "provider_fallback", claim["ticket"],
                    failed_run_id=terminal["run_id"],
                    route_id=terminal["route_id"],
                )
                return True
            if terminal.get("role_exit") == "role_exit_contract_blocked":
                blocked = self.json_call(
                    "state-machine", "block", "--ticket", claim["ticket"],
                    "--lease", claim["lease"], "--receipt", claim["receipt"],
                    "--workdir", claim["worktree"], "--json",
                )
                if blocked.get("status") != "blocked":
                    raise ControllerError(
                        "state machine returned an invalid contract blocker"
                    )
            claim["status"] = "blocked"
            claim["blocked_reason"] = "role-failure"
            self.save_claim(claim)
            self.release_ticket_lease(claim)
            self.event(
                "role_blocked", claim["ticket"],
                passport_sha256=self.passport_sha256(claim["ticket"]),
                role=claim["role"], role_exit=terminal.get("role_exit"),
                run_id=terminal.get("run_id"),
                terminal_reason_code=terminal.get("terminal_reason_code", ""),
            )
            return False
        if claim["role"] == "reviewer":
            self.json_call(
                "ticket-state", "--ticket", claim["ticket"],
                "--workdir", claim["worktree"],
                "--action", "reviewer-reconcile", "--json",
            )
            self.migrate_passport(claim, "validating")
        self.relocate_qualification_cell(claim)
        claim.update(receipt="", role="", status="claimed")
        self.save_claim(claim)
        return True

    def ticket_pr(self, claim: dict[str, Any], receipt: str) -> dict[str, Any]:
        return self.json_call(
            "ticket-pr", "--ticket", claim["ticket"], "--lease", claim["lease"],
            "--receipt", receipt, "--workdir", claim["worktree"], "--json",
        )

    def wait_for_preview_identity(
        self, claim: dict[str, Any], pr: dict[str, Any]
    ) -> bool:
        identity = pr.get("preview_identity")
        preflight = pr.get("preview_preflight")
        head = pr.get("head", "")
        identity_wait = (
            isinstance(identity, dict)
            and identity.get("status") == "wait"
            and identity.get("expected") == head
        )
        preflight_wait = (
            isinstance(preflight, dict)
            and preflight.get("status") == "wait"
            and preflight.get("head") == head
        )
        if (
            not (identity_wait or preflight_wait)
            or not SHA.fullmatch(head)
        ):
            raise ControllerError("preview identity wait evidence is invalid")
        now = int(time.time())
        if claim.get("preview_wait_head") != head:
            claim["preview_wait_head"] = head
            claim["preview_wait_started_epoch"] = now
            self.event_once("preview_identity_wait", claim["ticket"], expected=head)
        started = claim.get("preview_wait_started_epoch")
        if isinstance(started, bool) or not isinstance(started, int) or started > now:
            raise ControllerError("preview identity wait boundary is invalid")
        if now - started >= PREVIEW_IDENTITY_WAIT_SECONDS:
            self.block(claim, "preview-identity-timeout")
            self.event_once(
                "preview_identity_timeout", claim["ticket"], expected=head,
                observed=(
                    preflight.get("evidence", {})
                    if preflight_wait else identity.get("observed", [])
                ),
            )
            return False
        self.save_claim(claim)
        return True

    def clear_preview_identity_wait(self, claim: dict[str, Any]) -> None:
        if "preview_wait_head" not in claim:
            return
        claim.pop("preview_wait_head", None)
        claim.pop("preview_wait_started_epoch", None)
        self.save_claim(claim)

    def refresh_dependency_tracking(self, claim: dict[str, Any]) -> bool:
        path = (
            Path(claim["worktree"])
            / "factory" / "tickets" / f"{claim['ticket']}.md"
        )
        try:
            ticket = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            # State-machine remains the authoritative fail-closed ticket
            # validator; focused controller callers may replace that boundary.
            return True
        values = re.findall(r"^Depends-On:\s*(.*?)\s*$", ticket, re.I | re.M)
        if len(values) > 1:
            raise ControllerError("ticket Depends-On is ambiguous")
        if not values or values[0].casefold() == "none":
            return True
        with self.git_lock:
            observation = run_external(
                [
                    "git", "-C", claim["worktree"], "ls-remote", "--heads",
                    "origin", "refs/heads/main",
                ],
                "protected main dependency observation failed",
            )
            observed = observation.stdout.split()
            if (
                len(observed) != 2
                or not SHA.fullmatch(observed[0])
                or observed[1] != "refs/heads/main"
            ):
                raise ControllerError(
                    "protected main dependency observation is ambiguous"
                )
            run_external(
                [
                    "git", "-C", claim["worktree"], "fetch", "--quiet",
                    "--no-tags", "origin",
                    "+refs/heads/main:refs/remotes/origin/main",
                ],
                "protected main dependency fetch failed",
            )
            local = subprocess.run(
                [
                    "git", "-C", claim["worktree"], "rev-parse",
                    "--verify", "origin/main^{commit}",
                ],
                text=True, capture_output=True, check=True, timeout=120,
            ).stdout.strip()
            if local != observed[0]:
                self.event(
                    "dependency_tracking_moved", claim["ticket"],
                    observed=observed[0], fetched=local,
                )
                return False
        return True

    def retry_ci(
        self, claim: dict[str, Any], receipt: str, pr: dict[str, Any]
    ) -> bool:
        value = self.json_call(
            "ci-rerun", "--ticket", claim["ticket"], "--lease", claim["lease"],
            "--receipt", receipt, "--pr", str(pr["pr_number"]),
            "--workdir", claim["worktree"], "--json",
        )
        return value.get("status") == "rerun"

    def publication_repair(
        self, claim: dict[str, Any], receipt: str, pr: dict[str, Any]
    ) -> None:
        value = self.json_call(
            "publication-repair", "--ticket", claim["ticket"],
            "--lease", claim["lease"], "--receipt", receipt,
            "--pr", str(pr["pr_number"]), "--workdir", claim["worktree"],
            "--json",
        )
        if value.get("status") != "repair":
            raise ControllerError("publication repair was not materialized")
        self.withdraw_publication(claim)
        self.migrate_passport(claim, "repair")
        self.event(
            "publication_repair", claim["ticket"], owner=value.get("owner"),
        )

    def protected_base_current(self, claim: dict[str, Any], head: str) -> bool:
        run_external(
            ["git", "-C", claim["worktree"], "fetch", "--quiet", "origin", "main"],
            "protected main refresh failed",
        )
        return subprocess.run(
            [
                "git", "-C", claim["worktree"], "merge-base", "--is-ancestor",
                "origin/main", head,
            ],
            check=False, timeout=120,
        ).returncode == 0

    def refresh_stale_protected_base(
        self, claim: dict[str, Any], receipt: str, head: str,
        event: str,
    ) -> bool:
        if not SHA.fullmatch(head):
            raise ControllerError("protected-base head is invalid")
        with self.git_lock:
            if self.protected_base_current(claim, head):
                return False
            self.withdraw_publication(claim)
            value = self.json_call(
                "ticket-attest", "--ticket", claim["ticket"],
                "--lease", claim["lease"], "--receipt", receipt,
                "--workdir", claim["worktree"], "--action", "refresh", "--json",
            )
            if value.get("action") not in {
                "refresh", "dependency-conflict-refresh",
            } or not SHA.fullmatch(
                value.get("head", "")
            ):
                raise ControllerError("protected-base refresh was not materialized")
            self.migrate_passport(claim, "validating")
            self.event(
                event, claim["ticket"], head_sha=value["head"],
                **(
                    {"repair_owner": "test-author"}
                    if value.get("action") == "dependency-conflict-refresh"
                    else {}
                ),
            )
            return True

    def publication_ready(
        self, claim: dict[str, Any], receipt: str, head: str
    ) -> bool:
        if self.refresh_stale_protected_base(
            claim, receipt, head, "protected_base_refreshed"
        ):
            return False
        with self.publication_lock:
            prior = claim.get("publication_lease", "")
            self.json_call(
                "publication", "ready", "--ticket", claim["ticket"],
                "--head", head, "--priority", claim["priority"], "--json",
            )
            lease = self.json_call(
                "publication", "acquire", "--ticket", claim["ticket"],
                "--head", head, "--priority", claim["priority"], "--json",
            )
            if lease.get("status") != "acquired":
                return False
            lease_sha256 = hashlib.sha256(lease["lease"].encode()).hexdigest()
            self.event_once(
                "publication_acquired", claim["ticket"], head_sha=head,
                publication_lease_sha256=lease_sha256,
            )
            if prior == lease["lease"]:
                self.event(
                    "publication_renewed", claim["ticket"], head_sha=head,
                    publication_lease_sha256=lease_sha256,
                )
            claim["publication_lease"] = lease["lease"]
            self.save_claim(claim)
            return True

    def request_protected_auto_merge(
        self, claim: dict[str, Any], receipt: str, pr: dict[str, Any]
    ) -> bool:
        if not self.publication_ready(claim, receipt, pr["head"]):
            current = self.cell_git(claim, "rev-parse", "HEAD")
            if current.returncode or not SHA.fullmatch(current.stdout.strip()):
                raise ControllerError("publication worktree head is unavailable")
            return current.stdout.strip() != pr["head"]
        approval = self.json_call(
            "ticket-attest", "--ticket", claim["ticket"],
            "--lease", claim["lease"], "--receipt", receipt,
            "--workdir", claim["worktree"], "--action", "approval",
            "--json",
        )
        if (
            approval.get("action") != "approval"
            or approval.get("auto_merge") is not True
            or approval.get("head") != pr["head"]
            or approval.get("pr_number") != pr.get("pr_number")
        ):
            raise ControllerError(
                "protected auto-merge did not bind exact H2"
            )
        self.migrate_passport(claim, "merge-pending")
        self.event(
            "protected_auto_merge_requested",
            claim["ticket"],
            head_sha=pr["head"],
        )
        return True

    def project_repository(self) -> str:
        values = re.findall(
            r"^(?:export\s+)?GH_REPO\s*=\s*['\"]?([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)['\"]?\s*$",
            (self.product / "factory/PROJECT.env").read_text(encoding="utf-8"),
            re.M,
        )
        if len(values) != 1:
            raise ControllerError("GH_REPO is missing or ambiguous")
        return values[0]

    def approval_pr_number(self, claim: dict[str, Any]) -> int:
        path = (
            Path(claim["worktree"]) / "factory" / "attestations"
            / claim["ticket"] / "approval.json"
        )
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ControllerError("approval PR identity is unavailable") from error
        if not isinstance(value, dict):
            raise ControllerError("approval PR identity is malformed")
        number = value.get("pr_number")
        if (
            value.get("schema") != "nysa.software-factory.ticket-approval/v1"
            or value.get("ticket") != claim["ticket"]
            or value.get("repository") != self.project_repository()
            or value.get("branch") != claim["branch"]
            or isinstance(number, bool)
            or not isinstance(number, int)
            or number <= 0
        ):
            raise ControllerError("approval PR identity is malformed")
        return number

    def merged_pr_identity(
        self, branch: str, number: int | None = None,
    ) -> dict[str, Any] | None:
        command = (
            ["gh", "pr", "view", str(number), "--repo", self.project_repository()]
            if number is not None else
            [
                "gh", "pr", "list", "--repo", self.project_repository(),
                "--state", "merged", "--head", branch,
            ]
        )
        result = run_external(
            [
                *command, "--json",
                "number,headRefName,baseRefName,headRefOid,mergeCommit,state,mergedAt",
            ],
            "GitHub merge query failed",
        )
        try:
            evidence = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise ControllerError("GitHub merge query returned malformed evidence") from error
        values = [evidence] if number is not None else evidence
        if not values:
            return None
        if not isinstance(values, list) or len(values) != 1:
            raise ControllerError("merged PR identity is ambiguous")
        value = values[0]
        if not isinstance(value, dict):
            raise ControllerError("merged PR identity is malformed")
        merge = value.get("mergeCommit")
        if (
            number is not None and value.get("number") != number
            or value.get("headRefName") != branch
            or value.get("baseRefName") != "main"
            or isinstance(value.get("number"), bool)
            or not isinstance(value.get("number"), int)
            or value["number"] <= 0
            or not SHA.fullmatch(value.get("headRefOid", ""))
        ):
            raise ControllerError("merged PR identity is malformed")
        if value.get("state") != "MERGED":
            if not value.get("mergedAt") and merge is None:
                return None
            raise ControllerError("merged PR identity is malformed")
        if (
            not value.get("mergedAt")
            or not isinstance(merge, dict)
            or not SHA.fullmatch(merge.get("oid", ""))
        ):
            raise ControllerError("merged PR identity is malformed")
        return {
            "head": value["headRefOid"],
            "merge_commit": merge["oid"],
            "number": value["number"],
        }

    def terminal_request_path(self, ticket: str) -> Path:
        return self.state / f"terminal-request-{ticket}.json"

    def terminal_request_main_is_safe(
        self, request: dict[str, Any], current: str,
    ) -> bool:
        expected = request.get("protected_main", "")
        if expected == current:
            return True
        if (
            request.get("schema")
            != "nysa.software-factory.terminal-request/v2"
            or not SHA.fullmatch(expected)
            or not SHA.fullmatch(request.get("protected_ticket_blob", ""))
        ):
            return False
        with self.git_lock:
            run_external(
                [
                    "git", "-C", str(self.product), "fetch", "--quiet",
                    "--no-tags", "origin", "+refs/heads/main:refs/remotes/origin/main",
                ],
                "terminal request refresh failed",
            )
            commits = [
                expected,
                request.get("implementation_pr", {}).get("merge_commit", ""),
                request.get("closeout_pr", {}).get("merge_commit", ""),
            ]
            if any(
                not SHA.fullmatch(commit)
                or subprocess.run(
                    [
                        "git", "-C", str(self.product), "merge-base",
                        "--is-ancestor", commit, current,
                    ],
                    check=False, timeout=120,
                ).returncode
                for commit in commits
            ):
                return False
            path = f"factory/tickets/{request['ticket']}.md"
            blobs = [
                subprocess.run(
                    ["git", "-C", str(self.product), "rev-parse", f"{commit}:{path}"],
                    text=True, capture_output=True, check=True, timeout=120,
                ).stdout.strip()
                for commit in (expected, current)
            ]
        return blobs == [request["protected_ticket_blob"]] * 2

    def terminal_request(
        self, claim: dict[str, Any], closeout_branch: str, create: bool,
    ) -> dict[str, Any] | None:
        branch = claim.get("branch", "")
        passport_path = self.state / "passports" / f"{claim['ticket']}.json"
        if branch != f"ticket/{claim['ticket']}" or not passport_path.exists():
            return None
        implementation = self.merged_pr_identity(
            branch, self.approval_pr_number(claim),
        )
        closeout = self.merged_pr_identity(closeout_branch)
        if implementation is None or closeout is None:
            return None
        passport = read(passport_path)
        if (
            passport.get("ticket") != claim["ticket"]
            or passport.get("branch") != branch
            or passport.get("factory_sha") != self.release_path.name
            or passport.get("publication_state") != "merged"
            or not DIGEST.fullmatch(passport.get("passport_sha256", ""))
        ):
            raise ControllerError("terminal request passport is invalid")
        remote = run_external(
            [
                "git", "-C", str(self.product), "ls-remote", "--heads", "origin",
                "refs/heads/main",
            ],
            "protected main terminal expectation is unavailable",
        )
        fields = remote.stdout.split()
        if len(fields) != 2 or not SHA.fullmatch(fields[0]):
            raise ControllerError("protected main terminal expectation is unavailable")
        with self.git_lock:
            run_external(
                [
                    "git", "-C", str(self.product), "fetch", "--quiet",
                    "--no-tags", "origin", "+refs/heads/main:refs/remotes/origin/main",
                ],
                "protected main terminal fetch failed",
            )
            ticket_blob = subprocess.run(
                [
                    "git", "-C", str(self.product), "rev-parse",
                    f"{fields[0]}:factory/tickets/{claim['ticket']}.md",
                ],
                text=True, capture_output=True, check=True, timeout=120,
            ).stdout.strip()
        if not SHA.fullmatch(ticket_blob):
            raise ControllerError("protected terminal ticket is unavailable")
        value = {
            "action": "done",
            "branch": branch,
            "closeout_pr": closeout,
            "factory_sha": self.release_path.name,
            "implementation_pr": implementation,
            "passport_sha256": passport.get("passport_sha256"),
            "protected_main": fields[0],
            "protected_ticket_blob": ticket_blob,
            "schema": "nysa.software-factory.terminal-request/v2",
            "ticket": claim["ticket"],
        }
        value["request_sha256"] = hashlib.sha256(
            canonical(value).encode()
        ).hexdigest()
        path = self.terminal_request_path(claim["ticket"])
        if path.exists():
            existing = read(path)
            existing_digest = existing.get("request_sha256", "")
            existing_payload = dict(existing)
            existing_payload.pop("request_sha256", None)
            stable = {"protected_main", "request_sha256"}
            if (
                existing_digest
                != hashlib.sha256(canonical(existing_payload).encode()).hexdigest()
                or {key: item for key, item in existing.items() if key not in stable}
                != {key: item for key, item in value.items() if key not in stable}
                or not self.terminal_request_main_is_safe(existing, fields[0])
            ):
                raise ControllerError("terminal request identity changed")
            return existing
        elif create:
            write(path, value)
        else:
            return None
        return value

    def ticket_merged(self, claim: dict[str, Any]) -> bool:
        return self.merged_pr_identity(
            claim["branch"], self.approval_pr_number(claim),
        ) is not None

    def closeout(self, claim: dict[str, Any]) -> bool:
        with self.closeout_lock:
            return self.closeout_serialized(claim)

    def closeout_serialized(self, claim: dict[str, Any]) -> bool:
        ticket = claim["ticket"]
        if self.qualification:
            for pending_ticket in self.qualification["tickets"]:
                if self.product_ticket_done(pending_ticket):
                    continue
                passport = self.authenticated_operator_passport(pending_ticket)
                if (
                    passport is None
                    or passport.get("factory_sha") != self.release_path.name
                    or passport.get("branch") != f"ticket/{pending_ticket}"
                    or passport.get("current_state") != "Approved"
                    or passport.get("publication_state") != "merged"
                ):
                    self.event_once(
                        "closeout_deferred_pending_implementation", ticket,
                        pending_ticket=pending_ticket,
                    )
                    return False
        root = Path(claim["worktree"]).parent
        for sibling in sorted(root.glob("closeout-T-*")):
            sibling_ticket = sibling.name.removeprefix("closeout-")
            if (
                sibling_ticket != ticket
                and re.fullmatch(r"T-\d+", sibling_ticket)
                and (
                    sibling / "factory" / "attestations" / sibling_ticket
                    / "done.json"
                ).is_file()
                and not self.product_ticket_done(sibling_ticket)
            ):
                self.event_once(
                    "closeout_deferred_pending_closeout", ticket,
                    pending_ticket=sibling_ticket,
                )
                return False
        active_claims = sorted(
            (self.product / "factory/.active-runs").glob("*")
        )
        if active_claims:
            self.event_once(
                "closeout_deferred_active_claim", ticket,
                active_claim=active_claims[0].name,
            )
            return False
        branch = f"chore/{ticket.lower().replace('-', '')}-closeout"
        worktree = root / f"closeout-{ticket}"
        with self.git_lock:
            run_external(
                ["git", "-C", str(self.product), "fetch", "--quiet", "origin", "main"],
                "protected main closeout fetch failed",
            )
            if not worktree.exists():
                exists = subprocess.run(
                    [
                        "git", "-C", str(self.product), "show-ref", "--verify",
                        "--quiet", f"refs/heads/{branch}",
                    ],
                    check=False, timeout=120,
                ).returncode == 0
                subprocess.run(
                    [
                        "git", "-C", str(self.product), "worktree", "add",
                        "--quiet",
                        *(["-b", branch, str(worktree), "origin/main"]
                          if not exists else [str(worktree), branch]),
                    ],
                    check=True, timeout=120,
                )
            elif not (
                worktree / "factory" / "attestations" / ticket / "done.json"
            ).exists():
                branch_result = subprocess.run(
                    [
                        "git", "-C", str(worktree), "symbolic-ref", "--quiet",
                        "--short", "HEAD",
                    ],
                    text=True, capture_output=True, check=False, timeout=120,
                )
                status = subprocess.run(
                    [
                        "git", "-C", str(worktree), "status",
                        "--porcelain=v1", "-z",
                    ],
                    text=True, capture_output=True, check=False, timeout=120,
                )
                if (
                    branch_result.returncode == 0
                    and branch_result.stdout.strip() == branch
                    and status.returncode == 0
                    and not status.stdout
                ):
                    subprocess.run(
                        [
                            "git", "-C", str(worktree), "merge", "--ff-only",
                            "origin/main",
                        ],
                        check=True, capture_output=True, timeout=120,
                    )
        self.terminal_request(claim, branch, create=True)
        try:
            value = self.json_call(
                "ticket-attest", "--ticket", ticket, "--lease", claim["lease"],
                "--workdir", str(worktree), "--action", "done", "--json",
            )
        except ControllerError as error:
            detail = str(error).removeprefix("ticket-attest: ")
            if detail.startswith((
                "required post-merge check is missing: ",
                "required post-merge check is pending: ",
            )):
                self.event("post_merge_check_wait", ticket, reason=detail)
                return False
            raise
        if value.get("closeout_pr_state") != "MERGED":
            return False
        terminal = value.get("terminal")
        if (
            not isinstance(terminal, dict)
            or terminal.get("basis") not in {
                "attested-done", "attested-emergency-closeout",
            }
            or not SHA.fullmatch(terminal.get("protected_main", ""))
        ):
            raise ControllerError("closeout lacks exact protected terminal evidence")
        self.event_once(
            "operator_terminal_recorded", ticket,
            protected_main=terminal["protected_main"],
            terminal_basis=terminal["basis"],
        )
        return True

    def run_role(
        self, claim: dict[str, Any], role: str, receipt: str,
        failed_checks: list[str], publication: dict[str, Any] | None = None,
        *, primed_planner: bool = False,
    ) -> bool:
        if self.qualification and self.qualification_cohort_error.is_set():
            return False
        self.ensure_execution_cell(claim)
        if self.qualification:
            try:
                ensure_qualification_artifacts(
                    self.product, self.state, claim["ticket"]
                )
            except QualificationArtifactError as error:
                raise ControllerError(str(error)) from error
        if role == "planner" and not primed_planner:
            preflight = self.json_call(
                "preflight", "--ticket", claim["ticket"], "--role", role,
                "--lease", claim["lease"], "--receipt", receipt,
                "--workdir", claim["worktree"], "--json",
                allow=(0, 1),
            )
            if preflight.get("status") != "ok" or preflight.get("exit_code") != 0:
                try:
                    evidence = self.preflight_refusal_evidence(preflight)
                except ControllerError as error:
                    self.event(
                        "preflight_refusal_invalid", claim["ticket"], error=str(error),
                        transition_receipt_sha256=receipt,
                    )
                    self.block(claim, "preflight-evidence")
                    return False
                self.event(
                    "preflight_refused", claim["ticket"], **evidence,
                    transition_receipt_sha256=receipt,
                )
                self.block(claim, "preflight")
                return False
        task = f"Execute {role} for {claim['ticket']} from its frozen contract and repository state."
        if failed_checks:
            task += " Required GitHub checks failed: " + ", ".join(failed_checks)
        if role == "reviewer":
            if publication is None:
                raise ControllerError("Reviewer CI evidence is missing")
            checks = publication.get("checks")
            head = publication.get("head")
            pr_number = publication.get("pr_number")
            pr_url = publication.get("url")
            status = publication.get("status")
            if (
                publication.get("schema") != "nysa.software-factory.ticket-pr/v1"
                or publication.get("boundary") != "reviewer"
                or publication.get("branch") != claim["branch"]
                or publication.get("ticket") != claim["ticket"]
                or status not in {"prepared", "failed"}
                or not isinstance(pr_number, int)
                or pr_number <= 0
                or not isinstance(pr_url, str)
                or not re.fullmatch(r"https://github[.]com/[^\s]+/pull/[1-9][0-9]*", pr_url)
                or not SHA.fullmatch(head or "")
                or not isinstance(checks, list)
                or checks != failed_checks
                or any(not isinstance(check, str) or not check for check in checks)
                or (status == "prepared" and checks)
                or (status == "failed" and not checks)
            ):
                raise ControllerError("Reviewer CI evidence is invalid")
            task += f" Trusted CI evidence: PR #{pr_number} is {pr_url} at exact head {head};"
            if status == "prepared":
                task += " every configured required GitHub check passed."
        if role == "narrator":
            if publication is None:
                raise ControllerError("Narrator publication evidence is missing")
            preview_urls = publication.get("preview_urls")
            publication_mode = publication.get("publication_mode")
            preview_identity = publication.get("preview_identity")
            pr_number = publication.get("pr_number")
            pr_url = publication.get("url")
            head = publication.get("head")
            if (
                publication.get("status") != "ready"
                or not isinstance(pr_number, int)
                or pr_number <= 0
                or not isinstance(pr_url, str)
                or not re.fullmatch(r"https://github[.]com/[^\s]+/pull/[1-9][0-9]*", pr_url)
                or not SHA.fullmatch(head or "")
                or publication.get("checks") != []
                or not isinstance(preview_urls, list)
                or publication_mode not in {"nonvisual", "railway"}
            ):
                raise ControllerError("Narrator publication evidence is invalid")
            if publication_mode == "nonvisual":
                observed = (
                    preview_identity.get("observed")
                    if isinstance(preview_identity, dict) else None
                )
                if (
                    preview_urls
                    or not isinstance(preview_identity, dict)
                    or preview_identity.get("expected") != head
                    or preview_identity.get("status") != "pass"
                    or preview_identity.get("reason") is not None
                    or not isinstance(observed, list)
                    or len(observed) != 1
                    or not isinstance(observed[0], dict)
                    or set(observed[0]) != {"paths_sha256", "policy"}
                    or observed[0].get("policy") != "nonvisual_paths"
                    or not isinstance(observed[0].get("paths_sha256"), str)
                    or not DIGEST.fullmatch(observed[0].get("paths_sha256", ""))
                ):
                    raise ControllerError("Narrator nonvisual evidence is invalid")
                task += (
                    f" Trusted publication evidence: PR #{pr_number} is {pr_url} at exact "
                    f"head {head}; every configured required GitHub check passed. "
                    "Trusted host marker: FACTORY_PR_NONVISUAL_EVIDENCE_V1. The complete "
                    "PR semantic diff is confined to the product's protected nonvisual "
                    "path policy. Mark Preview and Screenshots not applicable and explain "
                    "the offline behavior verified by the existing Reviewer and protected-CI "
                    "evidence. Do not run tests, builds, repo-check, secret-scan, or any "
                    "broad verification suite."
                )
            else:
                if not preview_urls:
                    raise ControllerError("Narrator preview evidence is invalid")
                for preview_url in preview_urls:
                    parsed = urlsplit(preview_url) if isinstance(preview_url, str) else None
                    if (
                        parsed is None
                        or parsed.scheme != "https"
                        or not parsed.hostname
                        or not parsed.hostname.endswith(".up.railway.app")
                        or parsed.username is not None
                        or parsed.password is not None
                        or parsed.port is not None
                        or parsed.query
                        or parsed.fragment
                        or parsed.path not in ("", "/")
                    ):
                        raise ControllerError("Narrator preview evidence is invalid")
                task += (
                    f" Trusted publication evidence: PR #{pr_number} is {pr_url} at exact "
                    f"head {head}; every configured required GitHub check passed. Trusted "
                    f"preview endpoints: {', '.join(preview_urls)}. Verify the deployed head "
                    "and exercise the frozen preview behavior, then capture the required "
                    "screenshots. Use the existing Reviewer and protected-CI evidence. Do not "
                    "run tests, builds, repo-check, secret-scan, or any broad verification suite."
                )
        command = [
            str(self.launcher), self.project, "run",
            "--role", role, "--ticket", claim["ticket"],
            "--lease", claim["lease"], "--receipt", receipt,
            "--prompt-file", str(self.release_path / f"roles/{role}.md"),
            "--workdir", claim["worktree"], "--", task,
        ]
        log_path = self.logs / f"{claim['ticket']}-{role}.log"
        with log_path.open("a", encoding="utf-8") as log:
            launch_gate = (
                self.qualification_launch_lock
                if self.qualification else nullcontext()
            )
            with launch_gate:
                if self.qualification and self.qualification_cohort_error.is_set():
                    return False
                claim.update(receipt=receipt, role=role, status="running")
                self.save_claim(claim)
                self.event(
                    "attempt_started", claim["ticket"], role=role,
                    transition_receipt_sha256=receipt,
                )
                process = subprocess.Popen(command, stdout=log, stderr=log)
            while True:
                if (
                    self.qualification
                    and not self.repository_test
                    and process.poll() is None
                    and self.role_active(claim)
                ):
                    self.observe_attempt_safely(claim)
                    return True
                try:
                    exit_status = process.wait(
                        timeout=RECONCILE_INTERVAL_SECONDS
                    )
                    break
                except subprocess.TimeoutExpired:
                    self.observe_attempt_safely(claim)
        if self.terminal_for_receipt(claim["ticket"], receipt) is None:
            self.latch_qualification_cohort_error()
            claim["status"] = "blocked"
            claim["blocked_reason"] = "missing-terminal"
            self.save_claim(claim)
            cleanup_deferred = []
            try:
                self.release_ticket_lease(claim)
            except (
                ControllerError, json.JSONDecodeError, OSError,
                subprocess.SubprocessError, UnicodeError,
            ):
                cleanup_deferred = ["lease"]
            self.event(
                "role_launch_missing_terminal", claim["ticket"],
                cleanup_deferred=cleanup_deferred,
                exit_status=exit_status, role=role,
                transition_receipt_sha256=receipt,
            )
            return True
        self.finish_pending_run(claim)
        return True

    def latch_qualification_cohort_error(self) -> None:
        if self.qualification:
            with self.qualification_launch_lock:
                self.qualification_cohort_error.set()

    def reconcile_ticket(self, claim: dict[str, Any]) -> dict[str, str]:
        try:
            if self.qualification and self.qualification_cohort_error.is_set():
                if claim.get("receipt"):
                    self.ensure_lease(claim, "terminal-accounting")
                    finished = self.finish_pending_run(claim)
                    if not finished:
                        if finished is None:
                            return self.live_role_wait(claim)
                        return {
                            "status": (
                                claim["status"]
                                if claim["status"] in {"blocked", "cancelled"}
                                else "active"
                            ),
                            "ticket": claim["ticket"],
                        }
                return {
                    "status": "waiting", "ticket": claim["ticket"],
                    "wait_reason": "qualification-cohort-error",
                }
            if (self.product / "factory/MAINTENANCE").exists():
                return {"status": "maintenance", "ticket": claim["ticket"]}
            self.ensure_lease(claim, "reconciliation")
            finished = self.finish_pending_run(claim)
            if not finished:
                if self.qualification and finished is None:
                    return self.live_role_wait(claim)
                return {
                    "status": (
                        claim["status"]
                        if claim["status"] in {"blocked", "cancelled"}
                        else "active"
                    ),
                    "ticket": claim["ticket"],
                }
            if self.qualification and self.qualification_cohort_error.is_set():
                return {
                    "status": "waiting", "ticket": claim["ticket"],
                    "wait_reason": "qualification-cohort-error",
                }
            repository_test_before_head = ""
            if self.repository_test:
                ticket_path = (
                    Path(claim["worktree"])
                    / "factory/tickets" / f"{claim['ticket']}.md"
                )
                states = re.findall(
                    r"^State:\s*(.*?)\s*$",
                    ticket_path.read_text(encoding="utf-8"),
                    re.I | re.M,
                )
                if states != ["Ready"]:
                    raise ControllerError(
                        "repository-test requires a fresh Ready ticket"
                    )
                repository_test_before_head = subprocess.run(
                    ["git", "-C", claim["worktree"], "rev-parse", "HEAD"],
                    text=True, capture_output=True, check=True, timeout=120,
                ).stdout.strip()
            passport_path = (
                self.state / "passports" / f"{claim['ticket']}.json"
            )
            if (
                (
                    claim.get("publication_lease")
                    or passport_path.exists()
                    and read(passport_path).get("publication_state") == "merged"
                )
                and self.ticket_merged(claim)
            ):
                if claim.get("publication_lease"):
                    self.release_publication(claim)
                self.migrate_passport(claim, "merged")
                if self.closeout(claim):
                    self.event_once("ticket_complete", claim["ticket"])
                    self.release(claim)
                    return {"status": "complete", "ticket": claim["ticket"]}
                return {
                    "status": "waiting", "ticket": claim["ticket"],
                    "wait_reason": "closeout",
                }
            if not self.repository_test and not self.route_path(claim).exists():
                raise ControllerError("ticket route was not batch pinned")
            if not self.refresh_dependency_tracking(claim):
                claim["status"] = "waiting"
                self.save_claim(claim)
                return {"status": "waiting", "ticket": claim["ticket"]}
            replay_transition = (
                self.dependency_publication_replay_transition(claim)
            )
            primed_planner_transition = None
            if replay_transition is None:
                primed_planner_transition = self.qualification_primed_planner_transition(
                    claim,
                )
                replay_transition = primed_planner_transition
            transition = (
                replay_transition
                if replay_transition is not None else self.json_call(
                    "state-machine", "--ticket", claim["ticket"],
                    "--lease", claim["lease"],
                    "--workdir", claim["worktree"], "--json",
                    timeout=None,
                )
            )
            maintenance = (self.product / "factory/MAINTENANCE").exists()
            if not valid_transition_evidence(transition, claim["ticket"]):
                raise ControllerError(
                    "maintenance boundary has invalid transition evidence"
                    if maintenance else
                    "state-machine returned invalid transition evidence"
                )
            stage = transition.get("stage", "")
            receipt = transition.get("receipt", "")
            role = transition.get("role")
            loop = transition.get("loop")
            if self.repository_test:
                states = re.findall(
                    r"^State:\s*(.*?)\s*$",
                    ticket_path.read_text(encoding="utf-8"),
                    re.I | re.M,
                )
                head = subprocess.run(
                    ["git", "-C", claim["worktree"], "rev-parse", "HEAD"],
                    text=True, capture_output=True, check=True, timeout=120,
                ).stdout.strip()
                persisted = self.transition_receipt(claim, record=False)
                if (
                    stage != "RUN planner"
                    or role != "planner"
                    or loop is not None
                    or states != ["Planning"]
                    or head == repository_test_before_head
                    or persisted is None
                    or persisted.get("receipt_sha256") != receipt
                    or persisted.get("stage") != stage
                    or persisted.get("role") != role
                    or persisted.get("head_sha") != head
                    or persisted.get("consumed") is not False
                    or persisted.get("lease_sha256")
                    != hashlib.sha256(claim["lease"].encode()).hexdigest()
                ):
                    raise ControllerError(
                        "repository-test did not reach authenticated Planning"
                    )
                self.event(
                    "repository_test_planning", claim["ticket"],
                    transition_receipt_sha256=receipt,
                )
                self.run_role(claim, "planner", receipt, [])
                terminal = self.terminal_for_receipt(claim["ticket"], receipt)
                head_after = self.cell_git(claim, "rev-parse", "HEAD")
                ancestry = self.cell_git(
                    claim, "merge-base", "--is-ancestor",
                    head, head_after.stdout.strip(),
                )
                consumed = self.transition_receipt(claim, record=False)
                output_sha256 = ""
                if terminal is not None:
                    try:
                        output_sha256 = role_output_sha256(
                            self.product / "factory/runs"
                            / f"{terminal.get('run_id', '')}.out"
                        )
                    except (OSError, RoleOutputError):
                        pass
                if (
                    terminal is None
                    or terminal.get("phase") != "completed"
                    or terminal.get("accounting_state") != "completed"
                    or terminal.get("go_issued") != "1"
                    or terminal.get("task_submitted") != "1"
                    or terminal.get("exit_status") != "0"
                    or terminal.get("role_exit") != "ok"
                    or terminal.get("role") != "planner"
                    or terminal.get("adapter") != "mock"
                    or terminal.get("selection_reason") != "test_override"
                    or terminal.get("kit_sha") != self.release_path.name
                    or terminal.get("kit_provenance_scope") != "repository-test"
                    or terminal.get("transition_receipt_sha256") != receipt
                    or terminal.get("role_head_before") != head
                    or output_sha256 != terminal.get("output_sha256")
                    or head_after.returncode
                    or not SHA.fullmatch(head_after.stdout.strip())
                    or head_after.stdout.strip() == head
                    or ancestry.returncode
                    or consumed is None
                    or consumed.get("receipt_sha256") != receipt
                    or consumed.get("consumed") is not True
                    or claim.get("receipt")
                    or claim.get("role")
                    or claim.get("status") != "claimed"
                ):
                    raise ControllerError(
                        "repository-test planner did not complete with authenticated evidence"
                    )
                self.event(
                    "repository_test_planner_completed", claim["ticket"],
                    head_sha=head_after.stdout.strip(),
                    output_sha256=output_sha256,
                    run_id=terminal["run_id"],
                    transition_receipt_sha256=receipt,
                )
                return {
                    "status": "planner-complete", "ticket": claim["ticket"]
                }
            if (
                not semantic_authorization_wait(stage)
                and str(claim.get("blocked_reason", "")).startswith(
                    "semantic-round-authorization:"
                )
            ):
                claim.pop("blocked_reason", None)
                self.save_claim(claim)
            if loop is not None:
                self.event_once(
                    "loop_attempt", claim["ticket"],
                    stage=stage,
                    transition_receipt_sha256=receipt,
                    **loop,
                )
            if maintenance:
                self.event(
                    "stage_resolution_paused",
                    claim["ticket"],
                    transition_receipt_sha256=receipt,
                )
                return {"status": "maintenance", "ticket": claim["ticket"]}
            if not (
                stage.startswith("AWAIT-OPERATOR operator approval observed")
                or stage.startswith("AWAIT-MERGE protected auto-merge requested")
            ):
                self.withdraw_publication(claim)
            if role:
                failed_checks: list[str] = []
                if role in {"reviewer", "narrator"}:
                    pr = self.ticket_pr(claim, receipt)
                    if (
                        pr.get("status") in {"failed", "prepared", "ready", "wait"}
                        and self.refresh_stale_protected_base(
                            claim, receipt, pr.get("head", ""),
                            "protected_base_refreshed_before_evidence",
                        )
                    ):
                        return {
                            "status": "progressed", "ticket": claim["ticket"]
                        }
                    if pr.get("status") == "wait":
                        if (
                            role == "narrator"
                            and isinstance(pr.get("preview_identity"), dict)
                            and not self.wait_for_preview_identity(claim, pr)
                        ):
                            return {"status": "blocked", "ticket": claim["ticket"]}
                        return {
                            "status": "waiting", "ticket": claim["ticket"],
                            "wait_reason": "pr-gate",
                        }
                    if (
                        role == "narrator"
                        and pr.get("status") == "failed"
                        and isinstance(pr.get("preview_preflight"), dict)
                        and pr["preview_preflight"].get("status") == "fail"
                    ):
                        self.block(claim, "preview-preflight")
                        self.event_once(
                            "preview_preflight_blocked", claim["ticket"],
                            expected=pr.get("head"),
                            reason=pr["preview_preflight"].get("reason"),
                        )
                        return {"status": "blocked", "ticket": claim["ticket"]}
                    if pr.get("status") == "failed" and self.retry_ci(
                        claim, receipt, pr
                    ):
                        return {
                            "status": "waiting", "ticket": claim["ticket"],
                            "wait_reason": "pr-gate",
                        }
                    if role == "narrator" and pr.get("status") != "ready":
                        self.block(claim, "narrator-pr-gate")
                        return {"status": "blocked", "ticket": claim["ticket"]}
                    if role == "reviewer" and pr.get("status") not in {
                        "prepared", "failed",
                    }:
                        raise ControllerError("Reviewer PR gate returned an invalid status")
                    if pr.get("status") == "failed":
                        failed_checks = list(pr.get("checks", []))
                if role in {"reviewer", "narrator"}:
                    self.clear_preview_identity_wait(claim)
                    launched = self.run_role(
                        claim, role, receipt, failed_checks, pr
                    )
                else:
                    launched = self.run_role(
                        claim, role, receipt, failed_checks,
                        primed_planner=primed_planner_transition is not None,
                    )
                if (
                    not launched
                    and self.qualification
                    and self.qualification_cohort_error.is_set()
                ):
                    return {
                        "status": "waiting", "ticket": claim["ticket"],
                        "wait_reason": "qualification-cohort-error",
                    }
                return {
                    "status": (
                        claim["status"]
                        if claim["status"] in {"blocked", "cancelled"}
                        else "progressed"
                    ),
                    "ticket": claim["ticket"],
                }
            if claim.get("release_refresh_required") is True:
                if stage == (
                    "REFUSE ticket Kit-SHA lease does not match the selected "
                    "kit SHA"
                ):
                    claim.pop("release_refresh_required", None)
                    self.block(claim, "route-migration-required")
                    return {"status": "blocked", "ticket": claim["ticket"]}
                with self.git_lock:
                    value = self.json_call(
                        "ticket-attest", "--ticket", claim["ticket"],
                        "--lease", claim["lease"], "--receipt", receipt,
                        "--workdir", claim["worktree"],
                        "--action", "refresh", "--json",
                    )
                    if value.get("action") != "refresh":
                        raise ControllerError(
                            "release refresh was not materialized"
                        )
                    self.migrate_passport(claim, "validating")
                claim.pop("release_refresh_required", None)
                self.block(claim, "route-migration-required")
                self.event(
                    "upgraded_bundle_refreshed", claim["ticket"],
                    head_sha=value.get("head"),
                )
                return {"status": "blocked", "ticket": claim["ticket"]}
            if stage.startswith("AWAIT-OPERATOR bundle posted"):
                pr = self.ticket_pr(claim, receipt)
                if (
                    pr.get("status") in {"failed", "prepared", "ready", "wait"}
                    and self.refresh_stale_protected_base(
                        claim, receipt, pr.get("head", ""),
                        "protected_base_refreshed_before_bundle",
                    )
                ):
                    return {"status": "progressed", "ticket": claim["ticket"]}
                if pr.get("status") == "failed" and self.retry_ci(
                    claim, receipt, pr
                ):
                    return {
                        "status": "waiting", "ticket": claim["ticket"],
                        "wait_reason": "pr-gate",
                    }
                if pr.get("status") == "wait":
                    return {
                        "status": "waiting", "ticket": claim["ticket"],
                        "wait_reason": "pr-gate",
                    }
                if pr.get("status") != "ready":
                    self.block(claim, "bundle-pr-gate")
                    return {"status": "blocked", "ticket": claim["ticket"]}
                attested = self.json_call(
                    "ticket-attest", "--ticket", claim["ticket"],
                    "--lease", claim["lease"], "--receipt", receipt,
                    "--workdir", claim["worktree"], "--action", "bundle", "--json",
                )
                self.migrate_passport(claim, "validating")
                self.event_once(
                    "awaiting_approval", claim["ticket"],
                    passport_sha256=self.passport_sha256(claim["ticket"]),
                    question="Approve this ticket to merge.",
                )
                return {"status": "progressed", "ticket": claim["ticket"]}
            if stage.startswith("AWAIT-OPERATOR operator approval observed"):
                if claim.get("publication_lease"):
                    self.release_publication(claim)
                pr = self.ticket_pr(claim, receipt)
                if pr.get("status") == "failed" and self.retry_ci(
                    claim, receipt, pr
                ):
                    return {
                        "status": "waiting", "ticket": claim["ticket"],
                        "wait_reason": "pr-gate",
                    }
                if pr.get("status") == "failed":
                    self.publication_repair(claim, receipt, pr)
                    return {"status": "progressed", "ticket": claim["ticket"]}
                if pr.get("status") != "ready":
                    return {
                        "status": "waiting", "ticket": claim["ticket"],
                        "wait_reason": "pr-gate",
                    }
                try:
                    attested = self.json_call(
                        "ticket-attest", "--ticket", claim["ticket"],
                        "--lease", claim["lease"], "--receipt", receipt,
                        "--workdir", claim["worktree"], "--action", "approval",
                        "--attest-only", "--json",
                    )
                except ControllerError as error:
                    if str(error).startswith(
                        "ticket-attest: stale_operator_approval:"
                    ):
                        self.mark_prepublication_retry(claim, pr)
                    raise
                if (
                    attested.get("action") != "approval-attested"
                    or attested.get("auto_merge") is not False
                    or not SHA.fullmatch(attested.get("head", ""))
                    or attested["head"] == pr["head"]
                    or attested.get("pr_number") != pr.get("pr_number")
                ):
                    raise ControllerError(
                        "approval attestation did not materialize exact H2"
                    )
                self.migrate_passport(claim, "merge-pending")
                self.event(
                    "approval_attested_before_publication",
                    claim["ticket"],
                    approved_head=attested["head"],
                    reviewed_head=pr["head"],
                )
                return {"status": "progressed", "ticket": claim["ticket"]}
            if stage.startswith(
                "AWAIT-MERGE approval attested; protected auto-merge request pending"
            ):
                pr = self.ticket_pr(claim, receipt)
                if pr.get("status") == "failed" and self.retry_ci(
                    claim, receipt, pr
                ):
                    return {
                        "status": "waiting", "ticket": claim["ticket"],
                        "wait_reason": "pr-gate",
                    }
                if pr.get("status") != "ready":
                    return {
                        "status": "waiting", "ticket": claim["ticket"],
                        "wait_reason": "pr-gate",
                    }
                if not self.request_protected_auto_merge(claim, receipt, pr):
                    return {
                        "status": "waiting", "ticket": claim["ticket"],
                        "wait_reason": "publication-lease",
                    }
                return {"status": "progressed", "ticket": claim["ticket"]}
            if stage.startswith("AWAIT_DEPENDENCY"):
                claim["status"] = "waiting"
                self.save_claim(claim)
                self.event(
                    "dependency_wait",
                    claim["ticket"],
                    dependencies=stage.partition(" ")[2],
                )
                return {"status": "waiting", "ticket": claim["ticket"]}
            if stage.startswith("AWAIT-OPERATOR"):
                claim["status"] = "waiting"
                semantic_event = semantic_authorization_event(
                    stage,
                    subprocess.run(
                        ["git", "-C", claim["worktree"], "rev-parse", "HEAD"],
                        text=True, capture_output=True, check=True, timeout=120,
                    ).stdout.strip(),
                    receipt,
                ) if semantic_authorization_wait(stage) is not None else None
                if semantic_event is not None:
                    wait = semantic_authorization_wait(stage)
                    assert wait is not None
                    claim["blocked_reason"] = semantic_block_reason(
                        wait[1], wait[2],
                    )
                    self.save_claim(claim)
                    event, dedupe, details = semantic_event
                    self.event_once(
                        event, claim["ticket"],
                        dedupe_fields=dedupe, **details,
                    )
                else:
                    claim.pop("blocked_reason", None)
                    self.save_claim(claim)
                return {"status": "waiting", "ticket": claim["ticket"]}
            if stage.startswith("AWAIT_BUDGET"):
                if claim.get("publication_lease"):
                    self.release_publication(claim)
                self.release_ticket_lease(claim)
                claim.update(
                    budget_sha256=self.envelope_digest(), receipt="",
                    role="", status="budget",
                )
                self.save_claim(claim)
                self.event(
                    "budget_wait", claim["ticket"],
                    passport_sha256=self.passport_sha256(claim["ticket"]),
                )
                return {"status": "budget", "ticket": claim["ticket"]}
            if stage.startswith("AWAIT-MERGE protected auto-merge requested"):
                if self.ticket_merged(claim):
                    if claim.get("publication_lease"):
                        self.release_publication(claim)
                    self.migrate_passport(claim, "merged")
                    self.closeout(claim)
                    return {"status": "progressed", "ticket": claim["ticket"]}
                try:
                    pr = self.ticket_pr(claim, receipt)
                except ControllerError:
                    if not self.ticket_merged(claim):
                        raise
                    if claim.get("publication_lease"):
                        self.release_publication(claim)
                    self.migrate_passport(claim, "merged")
                    self.closeout(claim)
                    return {"status": "progressed", "ticket": claim["ticket"]}
                if pr.get("status") == "failed" and self.retry_ci(
                    claim, receipt, pr
                ):
                    self.publication_ready(claim, receipt, pr["head"])
                    return {
                        "status": "waiting", "ticket": claim["ticket"],
                        "wait_reason": "pr-gate",
                    }
                if pr.get("status") == "failed":
                    self.publication_repair(claim, receipt, pr)
                    return {"status": "progressed", "ticket": claim["ticket"]}
                if pr.get("status") == "wait":
                    self.publication_ready(claim, receipt, pr["head"])
                    return {
                        "status": "waiting", "ticket": claim["ticket"],
                        "wait_reason": "pr-gate",
                    }
                if pr.get("status") == "ready":
                    requested = self.request_protected_auto_merge(
                        claim, receipt, pr,
                    )
                    current = self.cell_git(claim, "rev-parse", "HEAD")
                    if (
                        current.returncode == 0
                        and SHA.fullmatch(current.stdout.strip())
                        and current.stdout.strip() != pr["head"]
                    ):
                        return {
                            "status": "progressed", "ticket": claim["ticket"],
                        }
                    return {
                        "status": "waiting", "ticket": claim["ticket"],
                        "wait_reason": (
                            "protected-merge" if requested
                            else "publication-lease"
                        ),
                    }
                raise ControllerError("publication PR gate returned an invalid status")
            if stage.startswith("AWAIT-MERGE closeout auto-merge pending"):
                if self.closeout(claim):
                    return {
                        "status": "progressed", "ticket": claim["ticket"],
                    }
                return {
                    "status": "waiting", "ticket": claim["ticket"],
                    "wait_reason": "closeout",
                }
            if stage.startswith("COMPLETE"):
                self.event_once("ticket_complete", claim["ticket"])
                self.release(claim)
                return {"status": "complete", "ticket": claim["ticket"]}
            if stage.startswith("ESCALATE "):
                detail = stage.partition(" ")[2]
                self.block(claim, "state-machine-escalation")
                self.event(
                    "state_machine_escalated", claim["ticket"], detail=detail,
                    passport_sha256=self.passport_sha256(claim["ticket"]),
                )
                return {"status": "blocked", "ticket": claim["ticket"]}
            if stage in {
                "REFUSE refresh receipt was not committed directly after its merge",
                "REFUSE stale refresh receipt does not bind this branch history",
            }:
                with self.git_lock:
                    value = self.json_call(
                        "ticket-attest", "--ticket", claim["ticket"],
                        "--lease", claim["lease"], "--receipt", receipt,
                        "--workdir", claim["worktree"],
                        "--action", "refresh", "--json",
                    )
                    if value.get("action") != "refresh":
                        raise ControllerError(
                            "refresh topology repair was not materialized"
                        )
                    self.migrate_passport(claim, "validating")
                    self.event(
                        "refresh_topology_repaired", claim["ticket"],
                        head_sha=value.get("head"),
                    )
                return {"status": "progressed", "ticket": claim["ticket"]}
            dependency_refresh = re.fullmatch(
                r"REFUSE dependency refresh required; "
                r"dependencies=(T-[0-9]+(?:,T-[0-9]+)*); "
                r"protected-main=([0-9a-f]{40})",
                stage,
            )
            if dependency_refresh:
                receipt_record = read(self.state / f"{claim['ticket']}.json")
                current_head = subprocess.run(
                    ["git", "-C", claim["worktree"], "rev-parse", "HEAD"],
                    text=True, capture_output=True, check=True, timeout=120,
                ).stdout.strip()
                recorded_head = receipt_record.get("head_sha", "")
                if (
                    receipt_record.get("receipt_sha256") != receipt
                    or not SHA.fullmatch(recorded_head)
                    or not SHA.fullmatch(current_head)
                ):
                    raise ControllerError(
                        "dependency refresh receipt does not bind the old head"
                    )
                if current_head != recorded_head:
                    attest_arguments = [
                        "ticket-attest", "--ticket", claim["ticket"],
                        "--lease", claim["lease"],
                        "--workdir", claim["worktree"], "--action",
                        "dependency-refresh-replay", "--json",
                    ]
                else:
                    attest_arguments = [
                        "ticket-attest", "--ticket", claim["ticket"],
                        "--lease", claim["lease"], "--receipt", receipt,
                        "--workdir", claim["worktree"],
                        "--action", "dependency-refresh", "--json",
                    ]
                value = self.json_call(*attest_arguments)
                if value.get("action") == "dependency-wait":
                    claim["status"] = "waiting"
                    self.save_claim(claim)
                    self.event(
                        "dependency_base_moved", claim["ticket"],
                        expected=value.get("expected_protected_head"),
                        observed=value.get("observed_protected_head"),
                    )
                    return {"status": "waiting", "ticket": claim["ticket"]}
                attestation = value.get("attestation")
                refreshed = value.get("head", "")
                publication_refresh = (
                    value.get("action") == "dependency-publication-refresh"
                )
                replay = current_head != recorded_head
                dependency_tickets = dependency_refresh[1].split(",")
                terminal_receipts = value.get("dependency_terminals")
                if (
                    value.get("action") not in {
                        "dependency-refresh", "dependency-conflict-refresh",
                        "dependency-publication-refresh",
                    }
                    or not isinstance(attestation, dict)
                    or attestation.get("old_head") != receipt_record["head_sha"]
                    or (
                        attestation.get(
                            "base_head" if publication_refresh else "protected_head"
                        ) != dependency_refresh[2]
                    )
                    or (
                        publication_refresh
                        and value.get("dependencies")
                        != dependency_tickets
                    )
                    or (
                        publication_refresh
                        and (
                            not isinstance(terminal_receipts, list)
                            or len(terminal_receipts) != len(dependency_tickets)
                            or any(
                                not isinstance(item, dict)
                                or set(item) != {"ticket", "terminal_sha256"}
                                or item.get("ticket") != ticket
                                or not DIGEST.fullmatch(
                                    item.get("terminal_sha256", "")
                                )
                                for ticket, item in zip(
                                    dependency_tickets, terminal_receipts
                                )
                            )
                        )
                    )
                    or not SHA.fullmatch(refreshed)
                    or (
                        replay
                        and (not publication_refresh or refreshed != current_head)
                    )
                    or subprocess.run(
                        [
                            "git", "-C", claim["worktree"], "merge-base",
                            "--is-ancestor", dependency_refresh[2], refreshed,
                        ],
                        check=False, timeout=120,
                    ).returncode != 0
                ):
                    raise ControllerError(
                        "dependency refresh did not bind protected main"
                    )
                self.migrate_passport(claim, "validating")
                self.event(
                    (
                        "dependency_conflict_routed"
                        if value.get("action") == "dependency-conflict-refresh"
                        else "dependency_publication_evidence_retired"
                        if publication_refresh
                        else "dependency_base_refreshed"
                    ),
                    claim["ticket"],
                    dependencies=dependency_refresh[1],
                    old_head=receipt_record["head_sha"],
                    protected_main=dependency_refresh[2],
                    refreshed_head=refreshed,
                    **(
                        {
                            "repair_owner": attestation.get("repair_owner"),
                            "conflict_paths": [
                                item.get("path")
                                for item in attestation.get("conflicts", [])
                                if isinstance(item, dict)
                            ],
                        }
                        if value.get("action")
                        == "dependency-conflict-refresh"
                        else {}
                    ),
                )
                return {"status": "progressed", "ticket": claim["ticket"]}
            if stage.startswith("REFUSE"):
                self.block(claim, "state-machine-refusal")
                return {"status": "blocked", "ticket": claim["ticket"]}
            raise FactoryDefect(
                "unsupported_deterministic_stage",
                f"unsupported deterministic stage: {stage}",
            )
        except ExternalUnavailable:
            claim["status"] = "blocked"
            claim["blocked_reason"] = "external-unavailable"
            self.save_claim(claim)
            self.event_once(
                "external_service_wait", claim["ticket"],
                reason_code="external_unavailable",
            )
            return {
                "status": "waiting", "ticket": claim["ticket"],
                "wait_reason": "external-unavailable",
            }
        except (ControllerError, json.JSONDecodeError, OSError, subprocess.SubprocessError) as error:
            self.latch_qualification_cohort_error()
            claim["status"] = "blocked"
            claim["blocked_reason"] = "controller-error"
            self.save_claim(claim)
            cleanup_deferred = []
            for name, cleanup in (
                ("publication", lambda: self.withdraw_publication(claim)),
                ("lease", lambda: self.release_ticket_lease(claim)),
            ):
                if name == "lease" and (
                    self.role_active(claim)
                    or not DIGEST.fullmatch(claim.get("lease", ""))
                ):
                    continue
                try:
                    cleanup()
                except (
                    ControllerError, json.JSONDecodeError, OSError,
                    subprocess.SubprocessError, UnicodeError,
                ):
                    cleanup_deferred.append(name)
            self.event(
                "controller_error", claim["ticket"],
                cleanup_deferred=cleanup_deferred,
                error=safe_error(error),
                failure_class=(
                    "factory_defect" if isinstance(error, FactoryDefect)
                    else "unknown"
                ),
                reason_code=(
                    error.reason_code if isinstance(error, FactoryDefect)
                    else "controller_reconciliation_error"
                ),
            )
            return {"status": "error", "ticket": claim["ticket"], "error": str(error)}

    def reconcile_ticket_until_wait(self, claim: dict[str, Any]) -> dict[str, str]:
        while True:
            if (
                self.qualification
                and self.qualification_cohort_error.is_set()
                and not claim.get("receipt")
            ):
                if not self.role_active(claim):
                    self.park_claim(claim)
                self.settle_recovery_attempt(claim)
                return {
                    "status": "waiting", "ticket": claim["ticket"],
                    "wait_reason": "qualification-cohort-error",
                }
            try:
                result = self.reconcile_ticket(claim)
            except Exception:
                self.latch_qualification_cohort_error()
                raise
            if result.get("status") != "progressed":
                if (
                    result.get("status") in {
                        "blocked", "budget", "error", "maintenance", "waiting",
                    }
                    and result.get("wait_reason") != "live-role"
                    and not (
                        self.wait_seconds
                        and result.get("status") == "waiting"
                        and result.get("wait_reason") in RETRYABLE_RECONCILE_WAITS
                    )
                    and not self.role_active(claim)
                ):
                    self.park_claim(claim)
                self.settle_recovery_attempt(claim)
                return result
            self.mark_reconciling(claim, after_progress=True)

    def reconcile(self, *, prime: bool = False) -> dict[str, Any]:
        self.qualification_cohort_error.clear()
        self.admission_refusals = {}
        self.model_admission_outcome = None
        self.invalid_transition_tickets.clear()
        self.prior_transition_tickets.clear()
        existing = self.load_claims()
        if prime and not self.qualification:
            raise ControllerError("qualification prime requires qualification mode")
        if prime and self.qualification_marker("qualification-restart-boundary"):
            if self.qualification_marker("qualification-recovered"):
                raise ControllerError("qualification prime crossed the restart boundary")
            self.prime_qualification(existing)
            return {
                "active": len(existing),
                "results": [],
                "schema": SCHEMA,
                "status": "restart_required",
            }
        if self.repository_test and (
            existing
            or self.active_run_tickets()
            or self.dispatcher_lease_records()
        ):
            raise ControllerError(
                "repository-test Planning canary requires empty execution state"
            )
        qualification_preflight = self.qualification_admission_preflight(existing)
        if qualification_preflight is not None:
            return {
                "active": sum(self.consumes_capacity(item) for item in existing),
                "results": [qualification_preflight],
                "schema": SCHEMA,
                "status": "error",
            }
        protected_main = self.cancellation_authority(existing)
        marker_cleanup_pending = self.reconcile_refusal_readmission_markers(
            existing, protected_main,
        )
        if self.qualification:
            tickets = set(self.qualification["tickets"])
            for claim in existing:
                if claim["ticket"] not in tickets:
                    self.withdraw_publication(claim)
            existing = [
                claim for claim in existing
                if claim["ticket"] in tickets
                or claim["ticket"] in marker_cleanup_pending
            ]
        cancellable = [
            claim for claim in existing
            if claim["ticket"] not in marker_cleanup_pending
        ]
        retained = self.retire_canceled_claims(cancellable, protected_main)
        existing = [
            claim for claim in existing
            if claim["ticket"] in marker_cleanup_pending or claim in retained
        ]
        self.quarantine_invalid_transition_claims(existing)
        if self.qualification and any(
            claim.get("status") == "blocked"
            and claim.get("blocked_reason") in {
                "controller-error", "external-unavailable",
            }
            and not self.role_active(claim)
            for claim in existing
        ):
            self.protected_main_head()
        completed = [
            claim for claim in existing
            if claim["ticket"] not in marker_cleanup_pending
            if claim["ticket"] not in self.invalid_transition_tickets
            if self.product_ticket_done(claim["ticket"])
        ]
        for claim in completed:
            self.ensure_lease(claim, "terminal-cleanup")
            self.release(claim)
        existing = [claim for claim in existing if claim not in completed]
        self.reclaim_orphaned_execution_cells(existing)
        for claim in existing:
            if claim["ticket"] not in self.invalid_transition_tickets:
                self.operator_transition(claim)
        self.quarantine_invalid_transition_claims(existing)
        self.release_inactive_ticket_leases(existing)
        self.recover_changed_state_machine_refusals(existing, protected_main)
        self.recover_operator_action_events([
            claim for claim in existing
            if claim["ticket"] not in self.invalid_transition_tickets
        ])
        self.record_qualification_done_targets()
        self.recover_missing_passport_claims(existing)
        self.recover_terminal_requests([
            claim for claim in existing
            if claim["ticket"] not in self.invalid_transition_tickets
        ])
        self.readmit_prior_provider_failures(existing)
        self.recover_each(
            existing, self.recover_interrupted_claims, "interrupted-reconciliation",
        )
        self.recover_each(
            existing, self.recover_missing_terminals, "missing-terminal",
        )
        self.recover_each(
            [
                claim for claim in existing
                if claim["ticket"] not in self.prior_transition_tickets
            ], self.recover_passportless_route_migrations,
            "passportless-route-migration",
        )
        self.recover_each(
            [
                claim for claim in existing
                if claim["ticket"] not in self.prior_transition_tickets
            ], self.recover_passport_preflight_blocks,
            "preflight-retry", concurrent=True,
        )
        self.recover_each(
            [
                claim for claim in existing
                if claim["ticket"] not in self.prior_transition_tickets
            ], self.recover_preflight_blocks, "preflight-retry",
            concurrent=True,
        )
        self.recover_each(
            existing, self.recover_semantic_authorizations,
            "semantic-round-authorization",
        )
        self.recover_each(
            existing, self.recover_reviewer_voids, "reviewer-run-void",
        )
        self.recover_each(
            existing, self.recover_upgraded_claims, "release-upgrade",
            concurrent=True,
        )
        self.recover_prior_maintenance_receipts(existing)
        self.recover_each(
            existing, self.recover_terminal_exports, "terminal-export",
        )
        self.recover_each(
            existing, self.recover_repaired_failures, "targeted-repair",
        )
        self.event(
            "controller_started", recovered_tickets=sorted(
                item["ticket"] for item in existing
                if self.runnable(item)
                and item["ticket"] not in (
                    self.invalid_transition_tickets
                    | self.prior_transition_tickets
                )
            ),
        )
        claims = existing
        if self.qualification:
            try:
                claims = self.claim_new(existing)
                self.clear_admission_failure()
            except ControllerError as error:
                self.record_admission_failure(error, existing)
        if (
            self.qualification
            and not self.qualification_marker("qualification-restart-boundary")
        ):
            active = sorted(
                item["ticket"] for item in claims
                if self.runnable(item)
                and item["ticket"] not in (
                    self.invalid_transition_tickets
                    | self.prior_transition_tickets
                )
            )
            accounted = sorted({item["ticket"] for item in claims} | {
                ticket for ticket in self.qualification["tickets"]
                if self.product_ticket_done(ticket)
            })
            target = self.qualification["target_done"]
            if len(accounted) == target:
                if prime:
                    self.prime_qualification(claims)
                self.qualification_marker(
                    "qualification-restart-boundary", create=True,
                )
                self.event("restart_boundary", tickets=accounted)
                return {
                    "active": len(active),
                    "results": [],
                    "schema": SCHEMA,
                    "status": "restart_required",
                }
            return {
                "active": len(active),
                "results": [],
                "schema": SCHEMA,
                "status": "waiting_for_target",
            }
        if (
            self.qualification
            and not self.qualification_marker("qualification-recovered")
            and self.qualification_marker("qualification-restart-boundary")
        ):
            recovered = sorted(item["ticket"] for item in existing)
            accounted = sorted(set(recovered) | {
                ticket for ticket in self.qualification["tickets"]
                if self.product_ticket_done(ticket)
            })
            if accounted != sorted(self.qualification["tickets"]):
                raise ControllerError("qualification restart did not recover every target ticket")
            self.qualification_marker("qualification-recovered", create=True)
            self.event("controller_recovered", tickets=accounted)

        wait_seconds = min(self.wait_seconds, RECONCILE_LOCK_WAIT_SECONDS)
        wait_deadline = time.monotonic() + wait_seconds if wait_seconds else 0
        results: dict[str, dict[str, str]] = {}
        settled: set[str] = set()
        retry_after: dict[str, float] = {}
        futures: dict[Future, dict[str, Any]] = {}
        worker_limit = min(4, self.capacity)
        executor = ThreadPoolExecutor(max_workers=worker_limit)

        def reconcile_worker(claim: dict[str, Any]) -> dict[str, str]:
            try:
                return self.reconcile_ticket_until_wait(claim)
            except Exception:
                self.latch_qualification_cohort_error()
                raise

        def submit_ready(
            candidates: list[dict[str, Any]],
            all_claims: list[dict[str, Any]],
        ) -> None:
            if self.qualification and self.qualification_cohort_error.is_set():
                return
            available = worker_limit - len(futures)
            reserved_live = sum(
                not self.consumes_capacity(claim)
                for claim in futures.values()
            )
            capacity_slots = max(
                0,
                self.capacity
                - sum(
                    self.consumes_capacity(claim)
                    for claim in all_claims
                    if claim["ticket"] not in self.invalid_transition_tickets
                )
                - reserved_live,
            )
            for claim in sorted(
                candidates, key=lambda item: not self.consumes_capacity(item)
            ):
                if claim["ticket"] in (
                    self.invalid_transition_tickets
                    | self.prior_transition_tickets
                ):
                    continue
                if available <= 0:
                    break
                needs_capacity = not self.consumes_capacity(claim)
                if needs_capacity:
                    if capacity_slots <= 0:
                        continue
                try:
                    self.mark_reconciling(claim)
                except (
                    ControllerError,
                    json.JSONDecodeError,
                    OSError,
                    subprocess.SubprocessError,
                    UnicodeError,
                ) as error:
                    claim["status"] = "blocked"
                    claim["blocked_reason"] = "reconciliation-boundary"
                    self.save_claim(claim)
                    cleanup_errors = []
                    for name, cleanup in (
                        ("publication", lambda: self.withdraw_publication(claim)),
                        ("lease", lambda: self.release_ticket_lease(claim)),
                    ):
                        try:
                            cleanup()
                        except (
                            ControllerError,
                            json.JSONDecodeError,
                            OSError,
                            subprocess.SubprocessError,
                            UnicodeError,
                        ):
                            cleanup_errors.append(name)
                    try:
                        self.event_once(
                            "ticket_reconciliation_blocked", claim["ticket"],
                            cleanup_deferred=cleanup_errors,
                            error=safe_error(error),
                        )
                    except (
                        ControllerError,
                        json.JSONDecodeError,
                        OSError,
                        subprocess.SubprocessError,
                        UnicodeError,
                    ):
                        pass
                    results[claim["ticket"]] = {
                        "error": safe_error(error),
                        "status": "blocked",
                        "ticket": claim["ticket"],
                    }
                    settled.add(claim["ticket"])
                    continue
                if needs_capacity:
                    capacity_slots -= 1
                future = executor.submit(
                    reconcile_worker, claim
                )
                futures[future] = claim
                available -= 1

        try:
            while True:
                claims = self.load_claims()
                if self.qualification:
                    tickets = set(self.qualification["tickets"])
                    claims = [
                        claim for claim in claims if claim["ticket"] in tickets
                    ]
                busy = {claim["ticket"] for claim in futures.values()}
                idle = [
                    claim for claim in claims
                    if claim["ticket"] not in busy
                    and claim["ticket"] not in settled
                    and claim["ticket"] not in self.invalid_transition_tickets
                    and time.monotonic() >= retry_after.get(claim["ticket"], 0)
                    and not self.role_active(claim)
                ]
                inactive = [
                    claim for claim in idle
                    if claim["ticket"] not in retry_after
                ]
                self.release_inactive_ticket_leases(inactive)
                self.maintain_successor_leases(idle)
                if protected_main is None:
                    protected_main = self.cancellation_authority(idle)
                before_retirement = {claim["ticket"] for claim in idle}
                idle = self.retire_canceled_claims(idle, protected_main)
                retired = before_retirement - {
                    claim["ticket"] for claim in idle
                }
                if retired:
                    claims = [
                        claim for claim in claims
                        if claim["ticket"] not in retired
                    ]
                self.recover_missing_passport_claims(claims)
                self.recover_terminal_requests(idle)
                self.readmit_prior_provider_failures(idle)
                self.recover_each(
                    idle, self.recover_prepublication_attestations,
                    "prepublication-attestation",
                )
                self.recover_each(
                    idle, self.recover_interrupted_claims,
                    "interrupted-reconciliation",
                )
                self.recover_each(
                    idle, self.recover_missing_terminals, "missing-terminal",
                )
                self.recover_each(
                    [
                        claim for claim in idle
                        if claim["ticket"] not in self.prior_transition_tickets
                    ], self.recover_passportless_route_migrations,
                    "passportless-route-migration",
                )
                self.recover_each(
                    [
                        claim for claim in idle
                        if claim["ticket"] not in self.prior_transition_tickets
                    ], self.recover_passport_preflight_blocks,
                    "preflight-retry", concurrent=True,
                )
                self.recover_each(
                    [
                        claim for claim in idle
                        if claim["ticket"] not in self.prior_transition_tickets
                    ], self.recover_preflight_blocks, "preflight-retry",
                    concurrent=True,
                )
                self.recover_each(
                    idle, self.recover_semantic_authorizations,
                    "semantic-round-authorization",
                )
                self.recover_each(
                    idle, self.recover_upgraded_claims, "release-upgrade",
                    concurrent=True,
                )
                self.recover_prior_maintenance_receipts(idle)
                self.recover_each(
                    idle, self.recover_terminal_exports, "terminal-export",
                )
                self.recover_each(
                    idle, self.recover_repaired_failures, "targeted-repair",
                )
                # Existing pinned claims are scheduled before potentially slow
                # admission or batch route preparation.
                ready = [
                    claim for claim in idle
                    if self.runnable(claim)
                    and claim["ticket"] not in self.prior_transition_tickets
                    and self.route_path(claim).exists()
                ]
                submit_ready(ready, claims)
                busy.update(claim["ticket"] for claim in futures.values())
                reserved_live = sum(
                    not self.consumes_capacity(claim)
                    for claim in futures.values()
                )
                if self.model_admission_outcome is None:
                    try:
                        if self.repository_test:
                            reserved_live = self.capacity - 1
                        claims = (
                            self.claim_new(claims, reserved_live)
                            if reserved_live
                            else self.claim_new(claims)
                        )
                        self.clear_admission_failure()
                    except ControllerError as error:
                        self.record_admission_failure(error, claims)
                if self.model_admission_outcome is not None:
                    outcome = self.model_admission_outcome
                    results[outcome["ticket"]] = outcome
                new_idle = [
                    claim for claim in claims
                    if claim["ticket"] not in busy
                    and claim["ticket"] not in settled
                    and claim["ticket"] not in self.invalid_transition_tickets
                    and claim["ticket"] not in self.prior_transition_tickets
                    and time.monotonic() >= retry_after.get(claim["ticket"], 0)
                    and not self.role_active(claim)
                    and self.runnable(claim)
                ]
                pin_results = [] if self.repository_test else self.pin_routes(new_idle)
                pin_waiting = {item["ticket"] for item in pin_results}
                for item in pin_results:
                    results[item["ticket"]] = item
                    settled.add(item["ticket"])
                submit_ready(
                    [
                        claim for claim in new_idle
                        if claim["ticket"] not in pin_waiting
                        and claim["ticket"] not in busy
                    ],
                    claims,
                )
                if not futures:
                    pending = [
                        claim for claim in claims
                        if claim["ticket"] in retry_after
                        and claim["ticket"] not in settled
                    ]
                    now = time.monotonic()
                    if (
                        pending
                        and now < wait_deadline
                        and not self.qualification_cohort_error.is_set()
                    ):
                        time.sleep(max(0, min(
                            min(retry_after[claim["ticket"]] for claim in pending),
                            wait_deadline,
                        ) - now))
                        continue
                    for claim in pending:
                        if not self.role_active(claim):
                            self.park_claim(claim)
                        settled.add(claim["ticket"])
                    break
                done, _ = wait(
                    tuple(futures),
                    timeout=RECONCILE_INTERVAL_SECONDS,
                    return_when=FIRST_COMPLETED,
                )
                if not done:
                    continue
                for future in done:
                    claim = futures.pop(future)
                    worker_failed = False
                    try:
                        item = future.result()
                    except Exception as error:
                        worker_failed = True
                        self.latch_qualification_cohort_error()
                        claim["status"] = "blocked"
                        claim["blocked_reason"] = "worker-error"
                        self.save_claim(claim)
                        self.event(
                            "ticket_worker_failed",
                            claim["ticket"],
                            error=safe_error(error),
                            failure_class="factory_defect",
                            reason_code="controller_worker_exception",
                        )
                        item = {
                            "error": str(error),
                            "status": "error",
                            "ticket": claim["ticket"],
                        }
                    if (
                        self.qualification
                        and item.get("status") == "error"
                    ):
                        self.latch_qualification_cohort_error()
                    if (
                        not worker_failed
                        and not (
                            claim.get("status") == "blocked"
                            and claim.get("blocked_reason") in {
                                "controller-error", "external-unavailable",
                            }
                        )
                    ):
                        self.reconciliation_marker(claim["ticket"]).unlink(
                            missing_ok=True
                        )
                    results[claim["ticket"]] = item
                    retryable_wait = (
                        item.get("status") == "waiting"
                        and item.get("wait_reason") in RETRYABLE_RECONCILE_WAITS
                    )
                    if retryable_wait and (
                        wait_seconds and time.monotonic() < wait_deadline
                        or not wait_seconds and futures
                    ):
                        retry_after[claim["ticket"]] = (
                            time.monotonic() + RECONCILE_INTERVAL_SECONDS
                        )
                    elif retryable_wait and wait_seconds:
                        if not self.role_active(claim):
                            self.park_claim(claim)
                        settled.add(claim["ticket"])
                    elif item.get("status") in {
                        "active", "blocked", "budget", "error", "maintenance",
                        "planner-complete", "planning", "waiting",
                    }:
                        settled.add(claim["ticket"])
        finally:
            executor.shutdown(wait=True)
        claims = self.load_claims()
        for ticket, refusal in self.admission_refusals.items():
            results.setdefault(ticket, refusal)
        if self.qualification:
            selected = set(self.qualification["tickets"])
            for claim in claims:
                route_migration_wait = (
                    claim["status"] == "blocked"
                    and (
                        claim.get("blocked_reason") == "route-migration-required"
                        or self.stranded_route_upgrade_wait(claim)
                    )
                )
                if (
                    claim["ticket"] in selected
                    and claim["ticket"] not in (
                        self.invalid_transition_tickets
                        | self.prior_transition_tickets
                    )
                    and claim["status"] == "blocked"
                    and claim.get("blocked_reason") == "external-unavailable"
                ):
                    results.setdefault(claim["ticket"], {
                        "status": "waiting", "ticket": claim["ticket"],
                        "wait_reason": "external-unavailable",
                    })
                elif (
                    claim["ticket"] in selected
                    and claim["ticket"] not in (
                        self.invalid_transition_tickets
                        | self.prior_transition_tickets
                    )
                    and claim["status"] in {"blocked", "budget"}
                    and not route_migration_wait
                ):
                    results.setdefault(claim["ticket"], {
                        "status": claim["status"], "ticket": claim["ticket"],
                    })
        ordered = [results[ticket] for ticket in sorted(results)]
        active = len([
            item for item in claims if self.consumes_capacity(item)
        ])
        status = (
            "ok"
            if all(item["status"] != "error" for item in ordered)
            else "error"
        )
        if (
            self.qualification and active == 0
            and (
                not ordered
                or all(item["status"] == "complete" for item in ordered)
            )
        ):
            try:
                self.protected_main_head()
            except ExternalUnavailable:
                return {
                    "active": active,
                    "results": ordered,
                    "schema": SCHEMA,
                    "status": "waiting_for_target",
                }
            done = sorted(
                ticket for ticket in self.qualification["tickets"]
                if self.product_ticket_done(ticket)
            )
            if len(done) == self.qualification["target_done"]:
                if not ordered:
                    ordered = [
                        {"status": "complete", "ticket": ticket}
                        for ticket in done
                    ]
            elif ordered:
                status = "waiting_for_target"
        return {
            "active": active,
            "results": ordered,
            "schema": SCHEMA,
            "status": status,
        }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--launcher", required=True, type=Path)
    parser.add_argument("--project", required=True)
    parser.add_argument("--product-root", required=True, type=Path)
    parser.add_argument("--release-path", required=True, type=Path)
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--worktree-root", type=Path)
    parser.add_argument(
        "--action", choices=(
            "reconcile", "prime", "pause", "resume",
            "preview-timeout-retry",
            "authorize-round-plan", "authorize-round-apply",
            "contract-repair-plan", "contract-repair-apply",
            "qualification-history-repair",
            "reviewer-void-plan", "reviewer-void-apply",
        ),
        default="reconcile",
    )
    parser.add_argument("--ticket", default="")
    parser.add_argument("--issue", default="")
    parser.add_argument("--factory-sha", default="")
    parser.add_argument("--role", default="")
    parser.add_argument("--semantic-round", default=0, type=int)
    parser.add_argument("--run-ordinal", default=0, type=int)
    parser.add_argument("--operator-id", default="")
    parser.add_argument("--approve-hash", default="")
    parser.add_argument("--receipt", default="")
    parser.add_argument("--wait-seconds", default=0, type=int)
    args = parser.parse_args()
    lock_descriptor = -1
    try:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", args.project):
            raise ControllerError("invalid project")
        if (
            args.wait_seconds < 0
            or args.wait_seconds > 600
            or (args.wait_seconds and args.action != "reconcile")
            or (args.action in {"reconcile", "prime"} and any((
                args.ticket, args.issue, args.factory_sha, args.role,
                args.semantic_round, args.run_ordinal, args.operator_id,
                args.approve_hash, args.receipt,
            )))
            or (args.action == "pause" and any((
                args.factory_sha, args.role, args.semantic_round,
                args.run_ordinal, args.operator_id, args.approve_hash,
                args.receipt, not args.issue,
            )))
            or (args.action == "resume" and any((
                args.issue, args.role, args.semantic_round,
                args.run_ordinal, args.operator_id, args.approve_hash,
                args.receipt, not args.factory_sha,
            )))
            or (
                args.action == "preview-timeout-retry"
                and any((
                    args.issue, args.factory_sha, not args.ticket, args.role,
                    args.semantic_round, args.run_ordinal,
                    not args.operator_id, args.approve_hash, args.receipt,
                ))
            )
            or (
                args.action in {"authorize-round-plan", "authorize-round-apply"}
                and any((
                    args.issue, args.factory_sha, not args.ticket,
                    not args.role, not args.semantic_round, args.run_ordinal,
                    not args.operator_id,
                    args.action == "authorize-round-plan" and args.approve_hash,
                    args.action == "authorize-round-apply" and not args.approve_hash,
                    args.receipt,
                ))
            )
            or (
                args.action in {"contract-repair-plan", "contract-repair-apply"}
                and any((
                    args.issue, args.factory_sha, not args.ticket,
                    not args.role, args.semantic_round, args.run_ordinal,
                    not args.operator_id,
                    args.action == "contract-repair-plan" and args.approve_hash,
                    args.action == "contract-repair-apply" and not args.approve_hash,
                    args.receipt,
                ))
            )
            or (
                args.action in {"reviewer-void-plan", "reviewer-void-apply"}
                and any((
                    args.issue, args.factory_sha, args.role,
                    args.semantic_round, not args.ticket,
                    not args.run_ordinal, not args.operator_id,
                    args.action == "reviewer-void-plan" and args.approve_hash,
                    args.action == "reviewer-void-apply" and not args.approve_hash,
                    args.receipt,
                ))
            )
            or (
                args.action == "qualification-history-repair"
                and any((
                    args.issue, args.factory_sha, not args.ticket, args.role,
                    args.semantic_round, args.run_ordinal, args.operator_id,
                    args.approve_hash, not args.receipt,
                ))
            )
        ):
            raise ControllerError("controller action arguments are invalid")
        state = safe_directory(args.state_dir)
        lock_descriptor = os.open(
            state / "reconcile.lock",
            os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            fcntl.flock(lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(canonical({"schema": SCHEMA, "status": "busy"}))
            return
        controller = Controller(args)
        if args.wait_seconds and not controller.qualification:
            raise ControllerError(
                "reconcile wait requires sealed qualification mode"
            )
        if args.action == "pause":
            result = controller.pause_ticket(args.ticket, args.issue)
        elif args.action == "resume":
            result = controller.resume_ticket(args.ticket, args.factory_sha)
        elif args.action == "preview-timeout-retry":
            result = controller.retry_preview_timeout(
                args.ticket, args.operator_id,
            )
        elif args.action == "authorize-round-plan":
            result = controller.plan_semantic_authorization(
                args.ticket, args.role, args.semantic_round, args.operator_id,
            )
        elif args.action == "authorize-round-apply":
            result = controller.apply_semantic_authorization(
                args.ticket, args.role, args.semantic_round, args.operator_id,
                args.approve_hash,
            )
        elif args.action == "contract-repair-plan":
            result = controller.plan_contract_repair(
                args.ticket, args.role, args.operator_id,
            )
        elif args.action == "contract-repair-apply":
            result = controller.apply_contract_repair(
                args.ticket, args.role, args.operator_id, args.approve_hash,
            )
        elif args.action == "qualification-history-repair":
            result = controller.qualification_history_repair(
                args.ticket, args.receipt,
            )
        elif args.action == "reviewer-void-plan":
            result = controller.plan_reviewer_void(
                args.ticket, args.run_ordinal, args.operator_id,
            )
        elif args.action == "reviewer-void-apply":
            result = controller.apply_reviewer_void(
                args.ticket, args.run_ordinal, args.operator_id,
                args.approve_hash,
            )
        elif args.action == "prime":
            result = controller.reconcile(prime=True)
        else:
            if args.ticket:
                raise ControllerError("reconcile does not accept a ticket")
            result = controller.reconcile()
        print(canonical(result))
        if result["status"] == "error":
            raise SystemExit(1)
    except ExternalUnavailable:
        print('{"reason_code":"external_unavailable","status":"wait"}')
        raise SystemExit(75)
    except (
        FileNotFoundError, json.JSONDecodeError, OSError, ControllerError,
        subprocess.SubprocessError,
    ) as error:
        print(canonical({"error": str(error), "schema": SCHEMA, "status": "error"}))
        raise SystemExit(1)
    finally:
        if lock_descriptor >= 0:
            os.close(lock_descriptor)


if __name__ == "__main__":
    main()
