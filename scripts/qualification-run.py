#!/usr/bin/env python3
"""Drive one sealed qualification to its next deterministic boundary."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
from qualification_manifest import ManifestError, validate as validate_manifest  # noqa: E402
from inflight_release import (  # noqa: E402
    AuthorizationError, parse_authorization, ticket_source_kit,
)
from legacy_closeout import ValidationError, run as run_git  # noqa: E402
import operator_receipt  # noqa: E402


SCHEMA = "nysa.software-factory.qualification-run/v1"
CONTROLLER_SCHEMA = "nysa.software-factory.controller/v1"
DOCTOR_SCHEMA = "nysa.software-factory.doctor/v2"
REPORT_SCHEMA = "nysa.software-factory.qualification-report/v1"
MIGRATION_PLAN_SCHEMA = "nysa.software-factory.model-migration-batch-preview/v1"
MIGRATION_JOURNAL_SCHEMA = "nysa.software-factory.model-migration-batch-journal/v1"
PROJECT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
TICKET = re.compile(r"T-[0-9]+")
SHA = re.compile(r"[0-9a-f]{40}")
DIGEST = re.compile(r"[0-9a-f]{64}")
REQUIRED_CHECKS = {
    "active_binding", "clis", "contract_resume", "credentials",
    "fallback_readiness", "isolated_provider", "kit", "kit_pin",
    "transition_receipts",
}
NEUTRAL_CHECKS = {"controller", "model_readiness", "provider_cli_pins"}


class QualificationRunError(RuntimeError):
    pass


def canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
    ).encode()


def launcher_path(path: Path) -> Path:
    if not path.is_absolute() or path.is_symlink():
        raise QualificationRunError("qualification launcher is unsafe")
    try:
        resolved = path.resolve(strict=True)
        info = resolved.stat()
    except OSError as error:
        raise QualificationRunError("qualification launcher is unavailable") from error
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_nlink != 1
        or info.st_mode & 0o022
        or not os.access(resolved, os.X_OK)
    ):
        raise QualificationRunError("qualification launcher is unsafe")
    return resolved


def invoke(
    launcher: Path, project: str, action: str, phases: list[dict[str, Any]],
    *arguments: str,
) -> tuple[int, dict[str, Any]]:
    started_epoch_ms = time.time_ns() // 1_000_000
    started = time.monotonic()
    result = subprocess.run(
        [str(launcher), project, action, *arguments, "--json"],
        capture_output=True, check=False, text=True,
    )
    phases.append({
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "name": " ".join((action, *arguments[:1])),
        "started_epoch_ms": started_epoch_ms,
    })
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise QualificationRunError(f"{action} returned invalid JSON") from error
    if not isinstance(value, dict):
        raise QualificationRunError(f"{action} returned invalid JSON")
    return result.returncode, value


def controller_result(value: dict[str, Any]) -> None:
    status = value.get("status")
    if value.get("schema") != CONTROLLER_SCHEMA or status not in {
        "busy", "error", "ok", "restart_required", "waiting_for_target",
    }:
        raise QualificationRunError("controller returned an invalid result")
    if status != "busy" and (
        not isinstance(value.get("active"), int)
        or isinstance(value.get("active"), bool)
        or value["active"] < 0
        or not isinstance(value.get("results"), list)
    ):
        raise QualificationRunError("controller returned an invalid result")


def report_result(value: dict[str, Any]) -> None:
    unsigned = dict(value)
    digest = unsigned.pop("report_sha256", "")
    if (
        value.get("schema") != REPORT_SCHEMA
        or value.get("status") != "green"
        or digest != hashlib.sha256(canonical(unsigned)).hexdigest()
    ):
        raise QualificationRunError("qualification reducer returned invalid evidence")


def qualification_basis() -> tuple[
    set[str], int, bool, str, str, dict[str, str],
]:
    raw_path = os.environ.get("FACTORY_QUALIFICATION_MANIFEST", "")
    factory_sha = os.environ.get("FACTORY_RELEASE_SHA", "")
    path = Path(raw_path)
    descriptor = -1
    try:
        if not path.is_absolute():
            raise QualificationRunError("qualification manifest is unavailable")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
            or info.st_mode & 0o022
            or info.st_size > 131_072
        ):
            raise QualificationRunError("qualification manifest is unsafe")
        raw = os.read(descriptor, 131_073)
        if len(raw) != info.st_size:
            raise QualificationRunError("qualification manifest changed while reading")
        value = json.loads(raw.decode("utf-8", "strict"))
        manifest = validate_manifest(value, factory_sha)
        ticket_sources: dict[str, str] = {}
        product_sha = os.environ.get("FACTORY_QUALIFICATION_PRODUCT_SHA", "")
        if manifest.get("mode") == "successor" and product_sha:
            if not SHA.fullmatch(product_sha) or path.parent.name != "factory":
                raise QualificationRunError("qualification product identity is invalid")
            product = path.parent.parent.resolve(strict=True)
            if path.resolve(strict=True) != product / "factory/QUALIFICATION.json":
                raise QualificationRunError("qualification manifest is unavailable")
            committed = run_git(
                product, "show",
                f"{product_sha}:factory/QUALIFICATION.json",
            ).stdout.encode()
            if committed != raw:
                raise QualificationRunError("qualification manifest is not committed")
            authorization, entries = parse_authorization(
                run_git(
                    product, "show", f"{product_sha}:factory/migrations/"
                    f"inflight-release/{factory_sha}.json",
                ).stdout,
                run_git(
                    product, "show", f"{product_sha}:factory/PROJECT.env",
                ).stdout,
                factory_sha,
            )
            if (
                authorization["source_kit_sha"]
                != manifest["source_factory_sha"]
                or set(entries) != set(manifest["tickets"])
            ):
                raise QualificationRunError(
                    "qualification source authorization is not exact"
                )
            ticket_sources = {
                ticket: ticket_source_kit(authorization, entries[ticket])
                for ticket in manifest["tickets"]
            }
        return (
            set(manifest["tickets"]), manifest["capacity"],
            manifest.get("mode") == "successor", factory_sha,
            manifest.get("source_factory_sha", ""), ticket_sources,
        )
    except (
        AuthorizationError, OSError, UnicodeDecodeError, json.JSONDecodeError,
        ManifestError, ValidationError,
    ) as error:
        raise QualificationRunError("qualification manifest is invalid") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def route_migration_arguments(
    selected: set[str], factory_sha: str,
) -> tuple[str, ...]:
    state = Path(os.environ.get("FACTORY_CONTROLLER_STATE_DIR", ""))
    try:
        if not state.is_absolute() or state.resolve(strict=True) != state:
            return ()
        for directory in (state, state / "claims"):
            info = directory.lstat()
            if (
                directory.is_symlink()
                or not stat.S_ISDIR(info.st_mode)
                or info.st_uid != os.geteuid()
                or info.st_mode & 0o022
            ):
                raise QualificationRunError("qualification claim state is unsafe")
        pairs: list[str] = []
        migration_tickets: set[str] = set()
        for ticket in sorted(selected):
            path = state / f"claims/{ticket}.json"
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                info = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.geteuid()
                    or info.st_nlink != 1
                    or stat.S_IMODE(info.st_mode) != 0o600
                    or info.st_size > 1_048_576
                ):
                    raise QualificationRunError("qualification claim is unsafe")
                raw = os.read(descriptor, 1_048_577)
            finally:
                os.close(descriptor)
            if len(raw) != info.st_size:
                raise QualificationRunError("qualification claim changed while reading")
            claim = json.loads(raw.decode("utf-8", "strict"))
            if not isinstance(claim, dict) or claim.get("ticket") != ticket:
                raise QualificationRunError("qualification claim is invalid")
            route_wait = (
                claim.get("status") == "blocked"
                and claim.get("blocked_reason") == "route-migration-required"
            )
            attempt = claim.get("recovery_attempt")
            abandoned_route_wait = (
                claim.get("status") == "blocked"
                and claim.get("blocked_reason")
                == "recovery-abandoned:release-upgrade"
                and isinstance(attempt, dict)
                and set(attempt) == {
                    "count", "factory_sha", "input_sha256", "outcome_sha256",
                    "phase", "recovery", "retry_reason", "retry_status",
                }
                and isinstance(attempt.get("count"), int)
                and not isinstance(attempt["count"], bool)
                and attempt["count"] > 0
                and attempt.get("factory_sha") == factory_sha
                and isinstance(attempt.get("input_sha256"), str)
                and DIGEST.fullmatch(attempt["input_sha256"])
                and isinstance(attempt.get("outcome_sha256"), str)
                and DIGEST.fullmatch(attempt["outcome_sha256"])
                and attempt.get("phase") == "abandoned"
                and attempt.get("recovery") == "release-upgrade"
                and attempt.get("retry_reason") == "route-migration-required"
                and attempt.get("retry_status") == "blocked"
                and (
                    claim.get("lease_released") is True
                    or claim.get("parked") is True
                    and claim.get("lease", "") == ""
                )
            )
            if not route_wait and not abandoned_route_wait:
                continue
            worktree = Path(claim.get("worktree", ""))
            worktree_info = worktree.lstat()
            if (
                not worktree.is_absolute()
                or worktree.is_symlink()
                or worktree.resolve(strict=True) != worktree
                or not stat.S_ISDIR(worktree_info.st_mode)
                or worktree_info.st_uid != os.geteuid()
                or worktree_info.st_mode & 0o022
            ):
                raise QualificationRunError("qualification worktree is unsafe")
            migration_tickets.add(ticket)
            pairs.extend(("--ticket", ticket, "--workdir", str(worktree)))
        return tuple(pairs) if migration_tickets == selected else ()
    except (
        FileNotFoundError, json.JSONDecodeError, OSError, TypeError, UnicodeError,
    ) as error:
        raise QualificationRunError("qualification claim state is invalid") from error


def contract_recovery_claim(ticket: str) -> dict[str, Any]:
    state = Path(os.environ.get("FACTORY_CONTROLLER_STATE_DIR", ""))
    path = state / "claims" / f"{ticket}.json"
    descriptor = -1
    try:
        if (
            not state.is_absolute()
            or state.resolve(strict=True) != state
            or state.is_symlink()
        ):
            raise QualificationRunError("qualification claim state is unsafe")
        state_info = state.lstat()
        if (
            not stat.S_ISDIR(state_info.st_mode)
            or state_info.st_uid != os.geteuid()
            or state_info.st_mode & 0o022
        ):
            raise QualificationRunError("qualification claim state is unsafe")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_size > 1_048_576
        ):
            raise QualificationRunError("qualification claim is unsafe")
        raw = os.read(descriptor, 1_048_577)
        if len(raw) != info.st_size:
            raise QualificationRunError("qualification claim changed while reading")
        claim = json.loads(raw.decode("utf-8", "strict"))
        if not isinstance(claim, dict):
            raise QualificationRunError("qualification recovery claim is invalid")
        worktree = Path(claim.get("worktree", ""))
        worktree_info = worktree.lstat()
        if (
            claim.get("ticket") != ticket
            or claim.get("status") != "blocked"
            or claim.get("parked") is not True
            or claim.get("lease_released") is not True
            or claim.get("role") not in {
                "planner", "spec-linter", "test-author", "builder",
            }
            or not DIGEST.fullmatch(claim.get("receipt", ""))
            or not worktree.is_absolute()
            or worktree.is_symlink()
            or worktree.resolve(strict=True) != worktree
            or not stat.S_ISDIR(worktree_info.st_mode)
            or worktree_info.st_uid != os.geteuid()
            or worktree_info.st_mode & 0o022
        ):
            raise QualificationRunError("qualification recovery claim is invalid")
        return claim
    except (
        FileNotFoundError, json.JSONDecodeError, OSError, TypeError, UnicodeError,
    ) as error:
        raise QualificationRunError("qualification claim state is invalid") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def worktree_head(worktree: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(worktree), "rev-parse", "HEAD"],
        capture_output=True, check=False, text=True, timeout=120,
    )
    head = result.stdout.strip()
    if result.returncode or not SHA.fullmatch(head):
        raise QualificationRunError("qualification recovery worktree is invalid")
    return head


def worktree_clean(worktree: Path) -> bool:
    result = subprocess.run(
        ["git", "-C", str(worktree), "status", "--porcelain=v1", "-z",
         "--untracked-files=all", "--ignore-submodules=none"],
        capture_output=True, check=False, timeout=120,
    )
    return result.returncode == 0 and not result.stdout


def project_contract_recovery(
    launcher: Path, project: str, doctor: dict[str, Any],
    selected: set[str], phases: list[dict[str, Any]],
    authority: tuple[str, str] | None = None,
) -> None:
    contract = doctor["checks"]["contract_resume"]
    incidents = contract.get("incidents")
    if authority is None:
        if contract.get("status") != "warning" or not isinstance(incidents, list):
            return
        if len(incidents) != 1:
            raise QualificationRunError("qualification contract recovery is ambiguous")
        incident = incidents[0]
        ticket, expected_receipt = (
            incident["ticket"], incident["blocked_receipt_sha256"],
        )
    else:
        if contract.get("status") != "ok" or incidents not in (None, []):
            raise QualificationRunError("qualification contract recovery is ambiguous")
        ticket, expected_receipt = authority
    if ticket not in selected:
        raise QualificationRunError("qualification contract recovery is foreign")
    claim = contract_recovery_claim(ticket)
    if claim["receipt"] != expected_receipt:
        raise QualificationRunError("qualification contract recovery receipt changed")
    worktree = Path(claim["worktree"])
    code, checked = invoke(
        launcher, project, "state-machine", phases,
        "repair-check", "--ticket", ticket, "--receipt", claim["receipt"],
        "--workdir", str(worktree), "--qualification-recovery",
    )
    current = checked.get("current_state")
    resume_state = checked.get("resume_state")
    if (
        code != 0
        or checked.get("action") != "repair-check"
        or checked.get("schema") != "nysa.software-factory.state-machine/v1"
        or checked.get("status") != "ready"
        or checked.get("ticket") != ticket
        or checked.get("role") != claim["role"]
        or checked.get("repair_role") not in {
            "planner", "spec-linter", "test-author", "builder",
        }
        or resume_state not in {"Planning", "Building", "Review"}
        or current not in {"Blocked-Escalated", resume_state}
        or checked.get("head") != worktree_head(worktree)
    ):
        raise QualificationRunError("qualification contract recovery is invalid")
    if current == resume_state:
        if (
            not DIGEST.fullmatch(
                checked.get("operator_resume_receipt_sha256", "")
            )
            or not isinstance(
                checked.get("operator_resume_receipt_consumed"), bool
            )
            or not isinstance(
                checked.get("operator_resume_projection_pending"), bool
            )
        ):
            raise QualificationRunError(
                "qualification resume receipt is unavailable"
            )
        code, resumed = invoke(
            launcher, project, "state-machine", phases,
            "resume", "--ticket", ticket, "--receipt", claim["receipt"],
            "--workdir", str(worktree), "--qualification-recovery",
        )
        if (
            code != 0
            or resumed.get("action") != "resume"
            or resumed.get("schema")
            != "nysa.software-factory.state-machine/v1"
            or resumed.get("status") != "ready"
            or resumed.get("ticket") != ticket
            or resumed.get("role") != claim["role"]
            or resumed.get("repair_role") != checked["repair_role"]
            or not SHA.fullmatch(resumed.get("head", ""))
        ):
            raise QualificationRunError("qualification contract resume replay failed")
        return
    before = checked["head"]
    if not worktree_clean(worktree):
        raise QualificationRunError("qualification recovery worktree is dirty")
    started_epoch_ms = time.time_ns() // 1_000_000
    started = time.monotonic()
    environment = dict(os.environ)
    result = subprocess.run(
        [
            sys.executable, "-I", "-S", str(Path(__file__).with_name("operator-cli.py")),
            "--product", str(worktree), "--state-dir",
            os.environ["FACTORY_CONTROLLER_STATE_DIR"],
            "--qualification-runtime", "--qualification-receipt",
            claim["receipt"], "resume",
            "--ticket", ticket, "--stage", resume_state,
        ],
        capture_output=True, check=False, text=True, timeout=120,
        env=environment,
    )
    phases.append({
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "name": "operator resume",
        "started_epoch_ms": started_epoch_ms,
    })
    try:
        receipt = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise QualificationRunError("qualification operator resume returned invalid JSON") from error
    state = Path(os.environ["FACTORY_CONTROLLER_STATE_DIR"])
    persisted = operator_receipt.read_exact(
        state, ticket, "resume", receipt.get("receipt_sha256", ""),
        {
            "blocked_receipt_sha256": claim["receipt"],
            "resume_stage": resume_state,
        },
    ) if isinstance(receipt, dict) else None
    if (
        result.returncode != 0
        or persisted != receipt
        or receipt.get("consumed") is not False
        or receipt.get("ticket") != ticket
        or receipt.get("action") != "resume"
        or worktree_head(worktree) != before
        or not worktree_clean(worktree)
    ):
        raise QualificationRunError("qualification operator resume projection failed")


def migration_plan_result(
    value: dict[str, Any], selected: set[str], capacity: int, factory_sha: str,
) -> str:
    items = value.get("items")
    unsigned = {key: item for key, item in value.items() if key != "approval_sha256"}
    digest = value.get("approval_sha256", "")
    if (
        set(value) != {
            "approval_sha256", "factory_sha", "items", "max_workers",
            "protected_main", "schema",
        }
        or value.get("schema") != MIGRATION_PLAN_SCHEMA
        or value.get("factory_sha") != factory_sha
        or value.get("max_workers") != min(capacity, len(selected))
        or not isinstance(value.get("protected_main"), str)
        or not SHA.fullmatch(value["protected_main"])
        or not isinstance(items, list)
        or {item.get("ticket") for item in items if isinstance(item, dict)} != selected
        or len(items) != len(selected)
        or not isinstance(digest, str)
        or not DIGEST.fullmatch(digest)
        or digest != hashlib.sha256(canonical(unsigned) + b"\n").hexdigest()
    ):
        raise QualificationRunError("route migration preview is invalid")
    return digest


def migration_apply_result(
    value: dict[str, Any], plan: dict[str, Any], selected: set[str],
) -> None:
    unsigned = {key: item for key, item in value.items() if key != "record_sha256"}
    results = value.get("results")
    if (
        set(value) != {
            "approved_by", "created_at", "plan", "record_sha256", "results",
            "schema", "status", "updated_at",
        }
        or value.get("schema") != MIGRATION_JOURNAL_SCHEMA
        or value.get("status") != "pass"
        or value.get("approved_by") != "qualification-run"
        or value.get("plan") != plan
        or not isinstance(results, dict)
        or set(results) != selected
        or not isinstance(value.get("record_sha256"), str)
        or value["record_sha256"]
        != hashlib.sha256(canonical(unsigned) + b"\n").hexdigest()
    ):
        raise QualificationRunError("route migration result is invalid")


def doctor_allows_reconcile(
    value: dict[str, Any], project: str, selected: set[str], capacity: int,
    successor: bool, factory_sha: str, source_factory_sha: str,
    ticket_sources: dict[str, str],
) -> bool:
    checks = value.get("checks")
    if (
        value.get("schema") != DOCTOR_SCHEMA
        or value.get("schema_version") != 2
        or value.get("contract_version") != "2.0.0"
        or value.get("project") != project
        or not isinstance(checks, dict)
        or set(checks) != REQUIRED_CHECKS | NEUTRAL_CHECKS | {"runtime"}
        or any(
            not isinstance(checks[name], dict)
            or checks[name].get("status") != "ok"
            for name in REQUIRED_CHECKS - {
                "contract_resume", "transition_receipts",
            }
        )
        or any(
            not isinstance(checks[name], dict)
            or checks[name].get("status") != "not_applicable"
            for name in NEUTRAL_CHECKS
        )
        or not isinstance(checks["runtime"], dict)
    ):
        return False
    transition = checks["transition_receipts"]
    incidents = transition.get("incidents")
    incident_tickets = {
        item.get("ticket") for item in incidents if isinstance(item, dict)
    } if isinstance(incidents, list) else set()
    incident_factories = {
        item.get("active_factory_sha")
        for item in incidents if isinstance(item, dict)
    } if isinstance(incidents, list) else set()
    transition_ok = (
        transition.get("status") == "ok"
        and (incidents is None or incidents == [])
    )
    transition_recovery = (
        successor
        and transition.get("status") == "warning"
        and isinstance(incidents, list)
        and bool(incidents)
        and all(isinstance(item, dict) for item in incidents)
        and all(
            set(item) == {
                "active_factory_sha", "observed_at_epoch_ns", "reason_code",
                "receipt_factory_sha", "ticket", "transition_receipt_sha256",
            }
            for item in incidents
        )
        and len({item["ticket"] for item in incidents}) == len(incidents)
        and all(
            isinstance(item.get("active_factory_sha"), str)
            and SHA.fullmatch(item["active_factory_sha"])
            and isinstance(item.get("receipt_factory_sha"), str)
            and SHA.fullmatch(item["receipt_factory_sha"])
            and item["receipt_factory_sha"] != item["active_factory_sha"]
            and item.get("reason_code") == "prior_kit_receipt"
            and item.get("ticket") in selected
            and isinstance(item.get("observed_at_epoch_ns"), int)
            and not isinstance(item["observed_at_epoch_ns"], bool)
            and item["observed_at_epoch_ns"] >= 0
            and isinstance(item.get("transition_receipt_sha256"), str)
            and re.fullmatch(
                r"[0-9a-f]{64}", item["transition_receipt_sha256"],
            )
            for item in incidents
        )
        and (
            incident_factories == {factory_sha}
            or incident_factories == {source_factory_sha}
            or all(
                item["active_factory_sha"]
                == ticket_sources.get(item["ticket"])
                for item in incidents
            )
            or (
                incident_tickets == selected
                and len(incident_factories) == 1
                and factory_sha not in incident_factories
            )
        )
    )
    if not transition_ok and not transition_recovery:
        return False
    contract = checks["contract_resume"]
    contract_incidents = contract.get("incidents")
    transition_by_ticket = {
        item["ticket"]: item for item in incidents
    } if transition_recovery else {}
    contract_ok = (
        contract.get("status") == "ok"
        and (contract_incidents is None or contract_incidents == [])
    )
    contract_recovery = (
        successor
        and transition_recovery
        and contract.get("status") == "warning"
        and isinstance(contract_incidents, list)
        and len(contract_incidents) == 1
        and all(
            isinstance(item, dict)
            and set(item) == {
                "actual_bytes", "blocked_receipt_sha256",
                "changed_path_count", "expected_bytes",
                "first_differing_line", "observed_at_epoch_ns",
                "reason_code", "ticket",
            }
            and item.get("ticket") in selected
            and item.get("reason_code") == "resume_commit_content_mismatch"
            and isinstance(item.get("blocked_receipt_sha256"), str)
            and DIGEST.fullmatch(item["blocked_receipt_sha256"])
            and transition_by_ticket.get(item["ticket"], {}).get(
                "transition_receipt_sha256"
            ) == item["blocked_receipt_sha256"]
            and isinstance(item.get("observed_at_epoch_ns"), int)
            and not isinstance(item["observed_at_epoch_ns"], bool)
            and item["observed_at_epoch_ns"] >= 0
            and isinstance(item.get("actual_bytes"), int)
            and not isinstance(item["actual_bytes"], bool)
            and item["actual_bytes"] >= 0
            and item.get("expected_bytes") == item["actual_bytes"] + 1
            and item.get("changed_path_count") == 1
            and isinstance(item.get("first_differing_line"), int)
            and not isinstance(item["first_differing_line"], bool)
            and item["first_differing_line"] > 0
            for item in contract_incidents
        )
    )
    if not contract_ok and not contract_recovery:
        return False
    runtime = checks["runtime"]
    if value.get("overall_status") == "ok":
        return runtime.get("status") == "ok" and transition_ok and contract_ok
    if value.get("overall_status") != "warning":
        return False
    if runtime.get("status") == "ok":
        return transition_recovery and (contract_ok or contract_recovery)
    if runtime.get("status") != "warning":
        return False

    counters = (
        "run_records", "active_runs", "stale_runs", "malformed_runs",
        "active_run_claims", "malformed_active_run_claims",
        "max_concurrent_tickets", "dispatch_lease_records",
        "stale_dispatch_leases", "malformed_dispatch_leases",
    )
    if any(
        not isinstance(runtime.get(name), int)
        or isinstance(runtime[name], bool)
        or runtime[name] < 0
        for name in counters
    ):
        return False
    runs = runtime.get("runs")
    active_run_tickets = runtime.get("active_run_tickets")
    leases = runtime.get("dispatch_leases")
    locks = runtime.get("locks")
    provider = checks["isolated_provider"]
    if (
        runtime.get("maintenance") is not False
        or locks != {
            "global_ledger": False, "launch": False,
            "ledger": False, "provider": False,
        }
        or runtime.get("provider_lock_state") != "absent"
        or any(runtime[name] for name in (
            "stale_runs", "malformed_runs", "malformed_dispatch_leases",
            "malformed_active_run_claims",
        ))
        or not isinstance(runs, list)
        or not runtime["run_records"] == runtime["active_runs"] == len(runs)
        or not isinstance(leases, list)
        or runtime["dispatch_lease_records"] != len(leases)
        or runtime["max_concurrent_tickets"] != capacity
        or not 1 <= runtime["dispatch_lease_records"] <= runtime["max_concurrent_tickets"]
        or runtime["run_records"] != 0
        or runtime["active_runs"] != 0
        or runs != []
        or runtime["active_run_claims"] != 0
        or active_run_tickets != []
        or any(
            not isinstance(provider.get(name), int)
            or isinstance(provider[name], bool)
            or provider[name] != 0
            for name in (
                "active_attempts", "active_tokens", "unknown_workers",
                "legacy_intervals",
            )
        )
    ):
        return False
    tickets = [item.get("ticket") for item in leases if isinstance(item, dict)]
    states = [item.get("state") for item in leases if isinstance(item, dict)]
    stale_recovery = (
        successor
        and runtime["stale_dispatch_leases"] > 0
        and runtime["stale_dispatch_leases"] == states.count("stale")
        and all(state in {"active", "stale"} for state in states)
    )
    return (
        len(tickets) == len(leases)
        and all(
            isinstance(ticket, str) and TICKET.fullmatch(ticket) and ticket in selected
            for ticket, item in zip(tickets, leases)
        )
        and len(set(tickets)) == len(tickets)
        and (
            runtime["stale_dispatch_leases"] == 0
            and states == ["active"] * len(states)
            or stale_recovery
        )
    )


def execute(args: argparse.Namespace) -> dict[str, Any]:
    if not PROJECT.fullmatch(args.project):
        raise QualificationRunError("invalid qualification project")
    launcher = launcher_path(args.launcher)
    (
        selected, capacity, successor, factory_sha, source_factory_sha,
        ticket_sources,
    ) = qualification_basis()
    phases: list[dict[str, Any]] = []
    started = time.monotonic()
    code, doctor = invoke(launcher, args.project, "doctor", phases)
    if code != 0 or not doctor_allows_reconcile(
        doctor, args.project, selected, capacity, successor, factory_sha,
        source_factory_sha, ticket_sources,
    ):
        return {
            "doctor_status": doctor.get("overall_status"),
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "phases": phases,
            "project": args.project,
            "reason": "doctor_not_ready",
            "restarts": 0,
            "schema": SCHEMA,
            "status": "blocked",
        }
    if args.resume_ticket:
        if not TICKET.fullmatch(args.resume_ticket) or not DIGEST.fullmatch(
            args.resume_receipt
        ):
            raise QualificationRunError("qualification resume authority is invalid")
        project_contract_recovery(
            launcher, args.project, doctor, selected, phases,
            (args.resume_ticket, args.resume_receipt),
        )
        return {
            "doctor_status": doctor["overall_status"],
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "phases": phases,
            "project": args.project,
            "reason": "operator_resume_projected",
            "schema": SCHEMA,
            "status": "projected",
            "ticket": args.resume_ticket,
        }

    restarts = 0
    migration_applied = False
    contract_recovery_pending = (
        doctor["checks"]["contract_resume"]["status"] == "warning"
    )
    while True:
        code, controller = invoke(launcher, args.project, "reconcile", phases)
        controller_result(controller)
        if code != 0 or controller["status"] == "error":
            return {
                "controller": controller,
                "doctor_status": doctor["overall_status"],
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "phases": phases,
                "project": args.project,
                "reason": "controller_error",
                "restarts": restarts,
                "schema": SCHEMA,
                "status": "error",
            }
        if controller["status"] != "restart_required":
            if contract_recovery_pending:
                code, refreshed = invoke(
                    launcher, args.project, "doctor", phases,
                )
                if code != 0 or not doctor_allows_reconcile(
                    refreshed, args.project, selected, capacity, successor,
                    factory_sha, source_factory_sha, ticket_sources,
                ):
                    raise QualificationRunError(
                        "qualification Doctor changed during contract recovery"
                    )
                doctor = refreshed
                contract_recovery_pending = False
                if doctor["checks"]["contract_resume"]["status"] == "warning":
                    project_contract_recovery(
                        launcher, args.project, doctor, selected, phases,
                    )
                    continue
            if (
                not migration_applied
                and successor
                and controller["status"] == "ok"
                and controller["active"] == 0
                and controller["results"] == []
            ):
                migration_arguments = route_migration_arguments(
                    selected, factory_sha,
                )
                if migration_arguments:
                    code, plan = invoke(
                        launcher, args.project, "models", phases,
                        "migrate-batch-plan", *migration_arguments,
                    )
                    if code != 0:
                        raise QualificationRunError("route migration preview failed")
                    approval = migration_plan_result(
                        plan, selected, capacity, factory_sha,
                    )
                    code, migration = invoke(
                        launcher, args.project, "models", phases,
                        "migrate-batch", "--approve-hash", approval,
                        "--approved-by", "qualification-run", *migration_arguments,
                    )
                    if code != 0:
                        raise QualificationRunError("route migration failed")
                    migration_apply_result(migration, plan, selected)
                    migration_applied = True
                    continue
            break
        restarts += 1
        if restarts > 1:
            raise QualificationRunError("qualification restart did not converge")

    base = {
        "controller": controller,
        "doctor_status": doctor["overall_status"],
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "phases": phases,
        "project": args.project,
        "restarts": restarts,
        "schema": SCHEMA,
    }
    if controller["status"] == "busy":
        return {**base, "reason": "controller_busy", "status": "waiting"}
    if controller["status"] == "waiting_for_target":
        return {**base, "reason": "cohort_not_accounted", "status": "waiting"}

    if any(
        not isinstance(item, dict) or not isinstance(item.get("status"), str)
        for item in controller["results"]
    ):
        raise QualificationRunError("controller returned invalid ticket results")
    result_statuses = {item["status"] for item in controller["results"]}
    if result_statuses - {
        "active", "blocked", "budget", "cancelled", "complete",
        "maintenance", "waiting",
    }:
        raise QualificationRunError("controller returned an unknown ticket status")
    if "error" in result_statuses:
        raise QualificationRunError("controller reported a ticket error")
    if result_statuses & {"blocked", "budget", "cancelled", "maintenance"}:
        return {**base, "reason": "ticket_blocked", "status": "blocked"}
    if (
        not result_statuses
        or result_statuses - {"complete"}
        or controller["active"]
    ):
        return {**base, "reason": "authenticated_wait", "status": "waiting"}

    code, report = invoke(launcher, args.project, "qualification", phases)
    if code != 0:
        raise QualificationRunError("qualification reduction failed")
    report_result(report)
    return {
        **base,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "report": report,
        "status": "green",
    }


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--launcher", required=True, type=Path)
    parser.add_argument("--project", required=True)
    parser.add_argument("--resume-ticket", default="")
    parser.add_argument("--resume-receipt", default="")
    parser.add_argument("--json", action="store_true", required=True)
    args = parser.parse_args()
    if bool(args.resume_ticket) != bool(args.resume_receipt):
        parser.error("qualification resume requires ticket and receipt")
    return args


def main() -> int:
    try:
        result = execute(arguments())
        code = 0 if result["status"] in {"green", "projected"} else (
            2 if result["status"] == "error" else 3
        )
    except (OSError, QualificationRunError, subprocess.SubprocessError) as error:
        result = {"error": str(error), "schema": SCHEMA, "status": "error"}
        code = 2
    print(canonical(result).decode())
    return code


if __name__ == "__main__":
    raise SystemExit(main())
