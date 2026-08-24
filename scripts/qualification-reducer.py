#!/usr/bin/env python3
"""Reduce Contract 1.8 qualification evidence against protected GitHub truth."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
from typing import Any

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
from release_lineage import (  # noqa: E402
    passport_head_lineage, successor_release_lineage,
)
from legacy_closeout import (  # noqa: E402
    ValidationError as ProtectedTerminalError,
    protected_terminal,
)
from external_transport import remote_command, temporarily_unavailable  # noqa: E402


SCHEMA = "nysa.software-factory.qualification-report/v1"
MANIFEST_SCHEMA = "nysa.software-factory.qualification/v2"
EVENT_SCHEMA = "nysa.software-factory.controller-event/v1"
TERMINAL_ADOPTION_SCHEMA = (
    "nysa.software-factory.qualification-terminal-adoption/v2"
)
PROTECTED_TERMINAL_RECONCILIATION_SCHEMA = (
    "nysa.software-factory.qualification-protected-terminal-reconciliation/v1"
)
EMERGENCY_TERMINAL_RECONCILIATION_SCHEMA = (
    "nysa.software-factory.qualification-emergency-terminal-reconciliation/v1"
)
PASSPORT_MIGRATION_SCHEMA = (
    "nysa.software-factory.ticket-passport-migration/v2"
)
SHA = re.compile(r"^[0-9a-f]{40}$")
DIGEST = re.compile(r"^[0-9a-f]{64}$")
TICKET = re.compile(r"^T-[0-9]+$")
ROLES = {"planner", "spec-linter", "test-author", "builder", "reviewer", "narrator"}
LATENCY_TARGETS_MS = {
    "cold_activation": 180_000,
    "prepared_to_all_planners": 90_000,
    "final_narrator_to_done": 300_000,
    "last_narrator_to_cohort_done": 600_000,
}


class QualificationError(ValueError):
    pass


class ExternalUnavailable(QualificationError):
    pass


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def regular(path: Path, mode: int | None = None, limit: int = 5_000_000) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
            or info.st_mode & 0o022
            or (mode is not None and stat.S_IMODE(info.st_mode) != mode)
            or info.st_size > limit
        ):
            raise QualificationError(f"unsafe qualification evidence: {path.name}")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            return stream.read()
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def repair_immutable_link(path: Path) -> None:
    try:
        target = path.lstat()
    except FileNotFoundError:
        return
    if target.st_nlink != 2:
        return
    prefix = f".{path.name}."
    candidates = []
    for entry in path.parent.iterdir():
        if not entry.name.startswith(prefix):
            continue
        try:
            info = entry.lstat()
        except FileNotFoundError:
            continue
        if (info.st_dev, info.st_ino) == (target.st_dev, target.st_ino):
            candidates.append((entry, info))
    if (
        not stat.S_ISREG(target.st_mode) or target.st_uid != os.geteuid()
        or stat.S_IMODE(target.st_mode) != 0o600 or target.st_mode & 0o022
        or len(candidates) != 1 or candidates[0][1].st_nlink != 2
        or candidates[0][1].st_uid != os.geteuid()
    ):
        raise QualificationError("immutable qualification report is unsafe")
    candidates[0][0].unlink()
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def write_immutable(path: Path, raw: bytes) -> None:
    if path.exists() or path.is_symlink():
        repair_immutable_link(path)
        if regular(path, 0o600) != raw:
            raise QualificationError("immutable qualification report changed")
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            repair_immutable_link(path)
            if regular(path, 0o600) != raw:
                raise QualificationError("immutable qualification report changed")
        else:
            Path(temporary).unlink(missing_ok=True)
            temporary = None
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


def retained_report(path: Path, manifest: dict[str, Any]) -> bytes | None:
    if not path.exists() and not path.is_symlink():
        return None
    repair_immutable_link(path)
    raw = regular(path, 0o600)
    if not raw.endswith(b"\n") or raw.count(b"\n") != 1:
        raise QualificationError("immutable qualification report is invalid")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise QualificationError("immutable qualification report is invalid") from error
    report_digest = value.pop("report_sha256", None) if isinstance(value, dict) else None
    tickets = value.get("tickets") if isinstance(value, dict) else None
    ticket_ids = [item.get("ticket") for item in tickets or [] if isinstance(item, dict)]
    if (
        not DIGEST.fullmatch(report_digest or "")
        or hashlib.sha256(canonical(value).encode()).hexdigest() != report_digest
        or value.get("schema") != SCHEMA or value.get("status") != "green"
        or value.get("factory_sha") != manifest.get("factory_sha")
        or value.get("qualification_manifest_sha256")
        != hashlib.sha256(canonical(manifest).encode()).hexdigest()
        or ticket_ids != manifest.get("tickets")
    ):
        raise QualificationError("immutable qualification report is invalid")
    return raw


def command(*arguments: str, cwd: Path | None = None) -> str:
    try:
        result = subprocess.run(
            arguments, cwd=cwd, text=True, capture_output=True, check=False,
            timeout=120,
        )
    except subprocess.TimeoutExpired as error:
        if remote_command(list(arguments)):
            raise ExternalUnavailable from error
        raise
    if result.returncode and remote_command(
        list(arguments)
    ) and temporarily_unavailable(result.stderr or result.stdout):
        raise ExternalUnavailable
    if result.returncode:
        raise QualificationError(
            result.stderr.strip() or result.stdout.strip() or "evidence query failed"
        )
    return result.stdout


def successful_checks(repo: str, commit: str) -> set[str]:
    checks = json.loads(command(
        "gh", "api", f"repos/{repo}/commits/{commit}/check-runs",
        "--method", "GET", "-f", "per_page=100",
    )).get("check_runs", [])
    return {
        item.get("name") for item in checks
        if item.get("status") == "completed"
        and item.get("conclusion") in {"success", "neutral", "skipped"}
    }


def revalidate_report_checks(raw: bytes, repo: str) -> None:
    report = json.loads(raw)
    for item in report.get("tickets", []):
        head = item.get("pr_head", "")
        required = item.get("required_checks")
        if (
            not SHA.fullmatch(head)
            or not isinstance(required, list) or not required
            or any(not isinstance(name, str) or not name for name in required)
            or not set(required).issubset(successful_checks(repo, head))
        ):
            raise QualificationError(
                f"{item.get('ticket', 'ticket')} protected checks are not green"
            )


def project_value(product: Path, name: str) -> str:
    values = re.findall(
        rf"^(?:export\s+)?{re.escape(name)}\s*=\s*['\"]?([^'\"\s]+)['\"]?\s*$",
        (product / "factory/PROJECT.env").read_text(encoding="utf-8"),
        re.M,
    )
    if len(values) != 1:
        raise QualificationError(f"{name} is missing or ambiguous")
    return values[0]


def event_records(path: Path) -> list[dict[str, Any]]:
    info = path.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or path.is_symlink()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise QualificationError("controller event directory is unsafe")
    records = []
    for item in sorted(path.glob("*.json")):
        value = json.loads(regular(item, 0o600))
        digest = value.pop("event_sha256", "")
        if (
            value.get("schema") != EVENT_SCHEMA
            or digest != hashlib.sha256(canonical(value).encode()).hexdigest()
        ):
            raise QualificationError("controller event evidence is invalid")
        value["event_sha256"] = digest
        records.append(value)
    return sorted(records, key=lambda value: value["observed_at_epoch_ns"])


def qualification_events(
    events: list[dict[str, Any]], manifest: dict[str, Any],
) -> list[dict[str, Any]]:
    factory_sha = manifest.get("factory_sha")
    generation = manifest.get("generation")
    manifest_sha256 = hashlib.sha256(canonical(manifest).encode()).hexdigest()
    selected = []
    other_scoped = []
    unscoped = []
    foreign = []
    for item in events:
        if item.get("factory_sha") != factory_sha:
            foreign.append(item)
            continue
        has_generation = "qualification_generation" in item
        has_manifest = "qualification_manifest_sha256" in item
        if has_generation != has_manifest:
            raise QualificationError("qualification event boundary is malformed")
        if not has_generation:
            unscoped.append(item)
            continue
        observed_generation = item.get("qualification_generation")
        observed_manifest = item.get("qualification_manifest_sha256")
        if (
            not isinstance(observed_generation, int)
            or isinstance(observed_generation, bool)
            or observed_generation < 1
            or not DIGEST.fullmatch(observed_manifest or "")
        ):
            raise QualificationError("qualification event boundary is malformed")
        if (
            observed_generation == generation
            and observed_manifest == manifest_sha256
        ):
            selected.append(item)
        else:
            other_scoped.append(item)
    if not selected:
        raise QualificationError("qualification event boundary is missing")
    boundary = min(item["observed_at_epoch_ns"] for item in selected)
    if any(item.get("observed_at_epoch_ns", 0) >= boundary for item in unscoped):
        raise QualificationError("qualification event boundary is incomplete")
    if any(
        item.get("observed_at_epoch_ns", 0) >= boundary
        for item in other_scoped
    ):
        raise QualificationError("qualification event boundary changed")
    return sorted(
        [*foreign, *selected], key=lambda item: item["observed_at_epoch_ns"],
    )


def iso(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as error:
        raise QualificationError("GitHub timestamp is invalid") from error
    if parsed.tzinfo is None:
        raise QualificationError("GitHub timestamp lacks a timezone")
    return parsed


def protected_reconciliations(
    events: list[dict[str, Any]], factory_sha: str,
) -> dict[str, dict[str, Any]]:
    records = [
        item for item in events
        if item.get("event") == "protected_terminal_reconciled"
        and item.get("factory_sha") == factory_sha
    ]
    result = {item.get("ticket"): item for item in records}
    if len(result) != len(records):
        raise QualificationError("protected terminal reconciliation is duplicated")
    return result


def emergency_terminal_reconciliations(
    events: list[dict[str, Any]], factory_sha: str,
) -> dict[str, dict[str, Any]]:
    records = [
        item for item in events
        if item.get("event") == "emergency_terminal_reconciled"
        and item.get("factory_sha") == factory_sha
    ]
    result = {item.get("ticket"): item for item in records}
    if len(result) != len(records):
        raise QualificationError("emergency terminal reconciliation is duplicated")
    return result


def qualification_latency(
    manifest: dict[str, Any], passports: dict[str, dict[str, Any]],
    events: list[dict[str, Any]], boundary: dict[str, Any],
    completions: list[dict[str, Any]], activation_receipt: dict[str, Any],
    narrator_run_ids: dict[str, str], provider_planner_starts: dict[str, int],
) -> dict[str, Any]:
    tickets = manifest["tickets"]
    activations = [item for item in events if item.get("event") == "activation_complete"]
    if len(activations) != 1:
        raise QualificationError("qualification activation timing proof is missing")
    activation = activations[0]
    activation_keys = {
        "activation_receipt_id", "activation_started_epoch_ns", "event",
        "event_sha256", "factory_sha", "factory_tree", "observed_at_epoch_ns",
        "product_sha", "product_tree", "qualification_generation",
        "qualification_manifest_sha256", "restart_boundary_event_sha256",
        "schema", "ticket",
    }
    started = activation.get("activation_started_epoch_ns")
    activated = activation.get("observed_at_epoch_ns")
    receipt = dict(activation_receipt)
    receipt_id = receipt.pop("receipt_id", "")
    if (
        set(activation) != activation_keys
        or activation.get("schema") != EVENT_SCHEMA
        or activation.get("ticket") is not None
        or activation.get("factory_sha") != manifest["factory_sha"]
        or activation.get("qualification_generation") != manifest["generation"]
        or activation.get("qualification_manifest_sha256")
        != hashlib.sha256(canonical(manifest).encode()).hexdigest()
        or activation.get("restart_boundary_event_sha256")
        != boundary.get("event_sha256")
        or not DIGEST.fullmatch(activation.get("activation_receipt_id", ""))
        or not SHA.fullmatch(activation.get("factory_tree", ""))
        or not SHA.fullmatch(activation.get("product_sha", ""))
        or not SHA.fullmatch(activation.get("product_tree", ""))
        or not isinstance(started, int) or isinstance(started, bool)
        or not isinstance(activated, int) or isinstance(activated, bool)
        or started < 1 or activated < started
        or activated < boundary["observed_at_epoch_ns"]
        or receipt_id != activation.get("activation_receipt_id")
        or receipt_id
        != hashlib.sha256((canonical(receipt) + "\n").encode()).hexdigest()
        or receipt.get("activation_started_epoch_ns") != started
        or receipt.get("status") != "pass"
        or receipt.get("kit_sha") != activation.get("factory_sha")
        or receipt.get("kit_tree") != activation.get("factory_tree")
        or receipt.get("product_sha") != activation.get("product_sha")
        or receipt.get("product_tree") != activation.get("product_tree")
        or set(narrator_run_ids) != set(tickets)
    ):
        raise QualificationError("qualification activation timing proof is invalid")

    def milliseconds(end: int, start: int) -> int:
        if end < start:
            raise QualificationError("qualification timing proof is out of order")
        return (end - start + 999_999) // 1_000_000

    if (
        set(provider_planner_starts) != set(tickets)
        or any(
            not isinstance(value, int) or isinstance(value, bool) or value < 1
            for value in provider_planner_starts.values()
        )
    ):
        raise QualificationError("provider Planner submission proof is missing")
    narrator_terminals: dict[str, int] = {}
    completion_times = {
        item["ticket"]: item["observed_at_epoch_ns"] for item in completions
    }
    for ticket in tickets:
        final_narrator = narrator_run_ids[ticket]
        completed_narrators = {
            (item.get("run_id"), item.get("transition_receipt_sha256"))
            for item in passports[ticket]["completed_role_evidence"]
            if item.get("role") == "narrator" and item.get("run_id") == final_narrator
        }
        narrator_events = [
            item for item in events
            if item.get("event") == "attempt_terminal"
            and item.get("ticket") == ticket and item.get("role") == "narrator"
            and item.get("accounting_state") in {
                "completed", "abandoned_conservative",
            }
            and item.get("exit_status") == "0" and item.get("role_exit") == "ok"
            and (item.get("run_id"), item.get("transition_receipt_sha256"))
            in completed_narrators
        ]
        if (
            not narrator_events or len(narrator_events) != len({
                (item.get("run_id"), item.get("transition_receipt_sha256"))
                for item in narrator_events
            })
        ):
            raise QualificationError("qualification role timing proof is missing")
        if len(narrator_events) != 1:
            raise QualificationError("qualification final Narrator proof is ambiguous")
        terminal_at = narrator_events[0].get("terminal_at_epoch_ns")
        if (
            not isinstance(terminal_at, int) or isinstance(terminal_at, bool)
            or terminal_at < 1
            or terminal_at > narrator_events[0].get("observed_at_epoch_ns", 0)
        ):
            raise QualificationError("qualification role timing proof is invalid")
        narrator_terminals[ticket] = terminal_at
    prepared_ms = milliseconds(
        max(provider_planner_starts.values()), activated,
    )
    activation_ms = milliseconds(activated, started)
    ticket_ms = {
        ticket: milliseconds(completion_times[ticket], narrator_terminals[ticket])
        for ticket in tickets
    }
    cohort_ms = milliseconds(
        max(completion_times.values()), max(narrator_terminals.values()),
    )
    observed = {
        "cold_activation_ms": activation_ms,
        "final_narrator_to_done_ms": ticket_ms,
        "last_narrator_to_cohort_done_ms": cohort_ms,
        "prepared_to_all_planners_ms": prepared_ms,
        "target_max_ms": LATENCY_TARGETS_MS,
    }
    if (
        activation_ms > LATENCY_TARGETS_MS["cold_activation"]
        or prepared_ms > LATENCY_TARGETS_MS["prepared_to_all_planners"]
        or cohort_ms > LATENCY_TARGETS_MS["last_narrator_to_cohort_done"]
        or any(
            value > LATENCY_TARGETS_MS["final_narrator_to_done"]
            for value in ticket_ms.values()
        )
    ):
        raise QualificationError("qualification latency target exceeded")
    return observed


def manifest_fields(raw: bytes) -> dict[str, str]:
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeDecodeError as error:
        raise QualificationError("qualification run manifest is invalid") from error
    result: dict[str, str] = {}
    for line in lines:
        name, separator, value = line.partition("=")
        if not separator or not name or name in result:
            raise QualificationError("qualification run manifest is invalid")
        result[name] = value
    return result


def manifest_micro_usd(value: str) -> int:
    try:
        decimal = Decimal(value)
        if not decimal.is_finite():
            raise InvalidOperation
        amount = (decimal * 1_000_000).to_integral_value(rounding=ROUND_CEILING)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise QualificationError("qualification run charge is invalid") from error
    if amount < 0 or amount > 10**15:
        raise QualificationError("qualification run charge is invalid")
    return int(amount)


def provider_accounting_evidence(
    product: Path, manifest: dict[str, Any], passports: dict[str, dict[str, Any]],
    events: list[dict[str, Any]], provider_status: dict[str, Any], project: str,
) -> tuple[dict[str, Any], dict[str, int]]:
    factory_sha = manifest["factory_sha"]
    charge_items = [
        (ticket, item)
        for ticket in manifest["tickets"]
        for item in passports[ticket]["charge_records"]
        if isinstance(item, dict) and item.get("factory_sha") == factory_sha
    ]
    charges = {
        item.get("manifest_sha256"): (ticket, item) for ticket, item in charge_items
    }
    if len(charges) != len(charge_items) or any(
        not DIGEST.fullmatch(digest or "") for digest in charges
    ):
        raise QualificationError("provider accounting evidence is ambiguous")
    records = []
    for path in sorted((product / "factory/runs").glob("*.meta")):
        raw = regular(path)
        digest = hashlib.sha256(raw).hexdigest()
        value = manifest_fields(raw)
        if (
            value.get("kit_sha") != factory_sha
            or value.get("ticket") not in manifest["tickets"]
            or not value.get("provider_attempt_id")
        ):
            continue
        ticket = value["ticket"]
        charge = charges.get(digest, (None, None))[1]
        attempt_id = value.get("provider_attempt_id", "")
        accounting_state = value.get("accounting_state")
        if (
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:@-]{0,199}", attempt_id)
            or accounting_state not in {
                "completed", "abandoned_conservative",
                "cancelled_conservative", "launch_void",
            }
            or (
                charge is None and accounting_state != "launch_void"
            )
            or (
                charge is not None and (
                    charge.get("accounting_state") != accounting_state
                    or accounting_state == "launch_void"
                    or value.get("run_id") != charge.get("run_id")
                    or value.get("role") != charge.get("role")
                    or value.get("transition_receipt_sha256")
                    != charge.get("transition_receipt_sha256")
                )
            )
        ):
            raise QualificationError("provider accounting manifest is invalid")
        records.append((attempt_id, ticket, charge, value, digest))
    if {item[4] for item in records if item[2] is not None} != set(charges):
        raise QualificationError("provider accounting manifest is missing")
    if len({item[0] for item in records}) != len(records):
        raise QualificationError("provider accounting attempt was duplicated")
    attempts = provider_status.get("attempts")
    if (
        provider_status.get("schema") != "factory-provider-coordinator/v1"
        or provider_status.get("active_reserve_micro_usd") != 0
        or provider_status.get("legacy_intervals") != []
        or not isinstance(attempts, list)
        or any(not isinstance(item, dict) for item in attempts)
    ):
        raise QualificationError("provider accounting state is not terminal")
    by_id = {item.get("attempt_id"): item for item in attempts}
    if len(by_id) != len(attempts) or set(by_id) != {item[0] for item in records}:
        raise QualificationError("provider accounting attempts do not reconcile")
    bound, planner_starts = [], {}
    for attempt_id, ticket, charge, value, digest in records:
        attempt = by_id[attempt_id]
        reserve = manifest_micro_usd(value.get("reserved_usd", ""))
        state = value.get("accounting_state")
        launch_void = state == "launch_void"
        if launch_void:
            if (
                value.get("go_issued") != "0"
                or value.get("task_submitted") != "0"
                or manifest_micro_usd(value.get("effective_cost", "")) != 0
                or value.get("cost_basis") != "launch_void"
            ):
                raise QualificationError("provider accounting manifest is invalid")
            expected_charge = 0
        elif state in {"abandoned_conservative", "cancelled_conservative"}:
            if (
                value.get("go_issued") != "1"
                or manifest_micro_usd(value.get("effective_cost", "")) != reserve
                or value.get("cost_basis") != "conservative_reservation"
            ):
                raise QualificationError("provider accounting manifest is invalid")
            expected_charge = reserve
        else:
            expected_charge = manifest_micro_usd(value.get("effective_cost", ""))
            if (
                value.get("go_issued") != "1"
                or expected_charge > reserve
                or not value.get("cost_basis")
            ):
                raise QualificationError("provider accounting manifest is invalid")
        if charge is not None and charge.get("charge_micro_usd") != expected_charge:
            raise QualificationError("provider accounting charge does not match")
        terminal_events = [
            item for item in events
            if item.get("event") == "attempt_terminal"
            and item.get("run_id") == value["run_id"]
            and item.get("ticket") == ticket
        ]
        submitted = attempt.get("submitted_at")
        go_at = attempt.get("go_at")
        terminal_at = attempt.get("terminal_at")
        submitted_ns = value.get("submitted_at_epoch_ns", "")
        terminal_ns = value.get("terminal_at_epoch_ns", "")
        submitted_value = (
            int(submitted_ns) if re.fullmatch(r"[1-9][0-9]{0,19}", submitted_ns)
            else None
        )
        terminal_value = (
            int(terminal_ns) if re.fullmatch(r"[1-9][0-9]{0,19}", terminal_ns)
            else None
        )
        expected_event = {
            "accounting_state": value.get("accounting_state"),
            "go_issued": value.get("go_issued"),
            "provider_attempt_id": attempt_id,
            "role": value.get("role"),
            "run_id": value.get("run_id"),
            "submitted_at_epoch_ns": submitted_value,
            "task_submitted": value.get("task_submitted"),
            "terminal_at_epoch_ns": terminal_value,
            "transition_receipt_sha256": value.get("transition_receipt_sha256"),
        }
        event = terminal_events[0] if len(terminal_events) == 1 else {}
        observed_at = event.get("observed_at_epoch_ns")
        if (
            attempt.get("state") != "terminal"
            or attempt.get("ticket_id") != ticket
            or attempt.get("product_id") != f"{project}:{factory_sha}"
            or attempt.get("provider_family") != value.get("provider_family")
            or attempt.get("account_route") != value.get("account_route_id")
            or (
                attempt.get("admitted_at") is not None
                and attempt.get("policy_sha256")
                != value.get("activation_policy_sha256")
            )
            or (
                attempt.get("admitted_at") is None
                and attempt.get("policy_sha256") is not None
            )
            or attempt.get("reserve_micro_usd") != reserve
            or attempt.get("charge_micro_usd") != expected_charge
            or not isinstance(terminal_at, int)
            or (value.get("go_issued") == "1") != isinstance(go_at, int)
            or (value.get("task_submitted") == "1") != isinstance(submitted, int)
            or (isinstance(submitted, int) and not isinstance(go_at, int))
            or (
                any(isinstance(item, bool) for item in (go_at, submitted, terminal_at))
            )
            or (
                isinstance(go_at, int) and isinstance(submitted, int)
                and not go_at <= submitted <= terminal_at
            )
            or (submitted_value is None) != (submitted is None)
            or (
                isinstance(submitted, int)
                and not submitted * 1_000_000_000
                <= submitted_value <= (submitted + 1) * 1_000_000_000 - 1
            )
            or terminal_value is None
            or not isinstance(observed_at, int) or isinstance(observed_at, bool)
            or terminal_value > observed_at
            or any(event.get(name) != selected for name, selected in expected_event.items())
            or (
                state == "launch_void"
                and attempt.get("terminal_result")
                not in {"capacity_denied", "failed_pre_go", "cancelled"}
            )
            or (
                state in {"completed", "abandoned_conservative"}
                and attempt.get("terminal_result") not in {"succeeded", "failed"}
            )
            or (
                state == "cancelled_conservative"
                and attempt.get("terminal_result") != "cancelled"
            )
        ):
            raise QualificationError("provider accounting attempt does not match")
        if value.get("role") == "planner" and isinstance(submitted, int):
            planner_starts[ticket] = min(
                planner_starts.get(ticket, sys.maxsize),
                (submitted + 1) * 1_000_000_000 - 1,
            )
        bound.append({
            "attempt_id": attempt_id,
            "charge_micro_usd": attempt["charge_micro_usd"],
            "manifest_sha256": digest,
            "reservation_micro_usd": reserve,
            "run_id": value["run_id"],
            "ticket": ticket,
        })
    bound.sort(key=lambda item: item["attempt_id"])
    if set(planner_starts) != set(manifest["tickets"]):
        raise QualificationError("provider Planner submission proof is missing")
    return {
        "attempt_count": len(bound),
        "evidence_sha256": hashlib.sha256(canonical(bound).encode()).hexdigest(),
        "launch_void_count": sum(
            value.get("accounting_state") == "launch_void"
            for _attempt, _ticket, _charge, value, _digest in records
        ),
        "reservation_micro_usd": sum(item["reservation_micro_usd"] for item in bound),
        "terminal_charge_micro_usd": sum(item["charge_micro_usd"] for item in bound),
    }, planner_starts


def verify(
    manifest: dict[str, Any],
    passports: dict[str, dict[str, Any]],
    events: list[dict[str, Any]],
    terminals: dict[str, dict[str, Any]],
    pull_requests: dict[str, dict[str, Any]],
    ticket_caps: dict[str, int],
    activation_receipt: dict[str, Any] | None = None,
    narrator_run_ids: dict[str, str] | None = None,
    provider_accounting: dict[str, Any] | None = None,
    provider_planner_starts: dict[str, int] | None = None,
) -> dict[str, Any]:
    tickets = manifest.get("tickets")
    factory_sha = manifest.get("factory_sha")
    target_done = manifest.get("target_done")
    successor = manifest.get("mode") == "successor"
    budget_profile = (
        manifest.get("budget_usd"),
        manifest.get("per_ticket_budget_usd"),
        manifest.get("per_run_budget_usd"),
    )
    extended = budget_profile == (
        "300.000000", "100.000000", "10.000000",
    )
    source_factory_sha = manifest.get("source_factory_sha")
    reconciliations = protected_reconciliations(events, factory_sha)
    reconciled = set(reconciliations)
    emergency_reconciliations = emergency_terminal_reconciliations(
        events, factory_sha,
    )
    emergency_reconciled = set(emergency_reconciliations)
    manifest_keys = {
        "budget_usd", "capacity", "contract_version", "factory_sha",
        "generation", "per_run_budget_usd", "per_ticket_budget_usd",
        "schema", "target_done", "tickets",
    } | ({"mode", "source_factory_sha"} if successor else set())
    if (
        set(manifest) != manifest_keys
        or manifest.get("schema") != MANIFEST_SCHEMA
        or manifest.get("contract_version") not in ("1.8.0", "2.0.0")
        or manifest.get("capacity") not in (3, 4)
        or target_done not in (3, 4)
        or target_done > manifest.get("capacity")
        or (
            successor
            and (
                target_done != 3
                or manifest.get("capacity") != 3
                or not SHA.fullmatch(source_factory_sha or "")
                or source_factory_sha == factory_sha
                or manifest.get("budget_usd") != "300.000000"
                or manifest.get("per_ticket_budget_usd") != "100.000000"
                or manifest.get("per_run_budget_usd") != "10.000000"
            )
        )
        or extended and (target_done != 3 or manifest.get("capacity") != 3)
        or (
            not successor
            and (
                "mode" in manifest
                or "source_factory_sha" in manifest
                or budget_profile not in {
                    ("100.000000", "25.000000", "2.000000"),
                    ("300.000000", "100.000000", "10.000000"),
                }
            )
        )
        or not SHA.fullmatch(factory_sha or "")
        or not isinstance(manifest.get("generation"), int)
        or isinstance(manifest.get("generation"), bool)
        or manifest["generation"] < 1
        or not isinstance(tickets, list)
        or len(tickets) != target_done
        or len(set(tickets)) != target_done
        or any(not TICKET.fullmatch(ticket) for ticket in tickets)
        or bool(set(passports) & reconciled)
        or bool(reconciled & emergency_reconciled)
        or set(passports) | reconciled != set(tickets)
        or not reconciled.issubset(set(tickets))
        or not emergency_reconciled.issubset(set(passports))
        or set(terminals) != set(tickets)
        or set(pull_requests) != set(tickets)
        or set(ticket_caps) != set(tickets)
        or any(
            not isinstance(cap, int) or isinstance(cap, bool)
            or cap < 25_000_000 or cap > 100_000_000
            for cap in ticket_caps.values()
        )
    ):
        raise QualificationError("qualification inputs are incomplete")

    run_ids: set[str] = set()
    manifest_digests: set[str] = set()
    ticket_reports = []
    total = 0
    qualification_total = 0
    per_run_cap = int(Decimal(manifest["per_run_budget_usd"]) * 1_000_000)
    for ticket in tickets:
        done = terminals[ticket]
        pr = pull_requests[ticket]
        merge = (pr.get("mergeCommit") or {}).get("oid")
        if ticket in reconciled:
            reconciliation = reconciliations[ticket]
            head = (
                done.get("approved_pr_head")
                if done.get("schema") == "nysa.software-factory.ticket-done/v1"
                else done.get("pr_head")
            )
            allowed = {
                "done_sha256", "event", "event_sha256", "factory_sha",
                "observed_at_epoch_ns", "protected_main_sha",
                "protected_main_tree", "protected_ticket_blob",
                "qualification_charge_micro_usd", "qualification_generation",
                "qualification_manifest_sha256", "reconciliation_schema",
                "schema", "terminal_basis", "ticket",
            }
            required = allowed - {"event_sha256", "schema"}
            if (
                not required.issubset(reconciliation)
                or not set(reconciliation).issubset(allowed)
                or reconciliation.get("reconciliation_schema")
                != PROTECTED_TERMINAL_RECONCILIATION_SCHEMA
                or reconciliation.get("terminal_basis") not in {
                    "attested-done", "attested-emergency-closeout",
                }
                or reconciliation.get("qualification_charge_micro_usd") != 0
                or not SHA.fullmatch(reconciliation.get("protected_main_sha", ""))
                or not SHA.fullmatch(reconciliation.get("protected_main_tree", ""))
                or not SHA.fullmatch(reconciliation.get("protected_ticket_blob", ""))
                or not DIGEST.fullmatch(reconciliation.get("done_sha256", ""))
                or done.get("schema") not in {
                    "nysa.software-factory.ticket-done/v1",
                    "nysa.software-factory.ticket-emergency-done/v1",
                    "nysa.software-factory.ticket-emergency-done/v2",
                }
                or done.get("ticket") != ticket
                or reconciliation.get("done_sha256")
                != hashlib.sha256(canonical(done).encode()).hexdigest()
                or done.get("required_checks") != done.get("successful_checks")
                or not done.get("required_checks")
                or pr.get("number") != done.get("pr_number")
                or pr.get("headRefOid") != head
                or pr.get("baseRefName") != "main"
                or pr.get("state") != "MERGED"
                or merge != done.get("merge_commit")
                or not SHA.fullmatch(head or "")
                or not SHA.fullmatch(merge or "")
            ):
                raise QualificationError(
                    f"{ticket} protected terminal reconciliation is invalid"
                )
            ticket_reports.append({
                "charge_micro_usd": 0,
                "evidence_mode": "protected-terminal-reconciliation",
                "merge_commit": merge,
                "pr_head": head,
                "pr_number": pr["number"],
                "qualification_charge_micro_usd": 0,
                "roles": 0,
                "ticket": ticket,
            })
            continue
        passport = passports[ticket]
        emergency = emergency_reconciliations.get(ticket)
        charges = passport.get("charge_records")
        completed = passport.get("completed_role_evidence")
        history = passport.get("factory_release_history")
        migrations = passport.get("migration_history")
        history_shas = {
            item.get("factory_sha") for item in history or []
            if isinstance(item, dict)
            and item.get("contract_version") in ("1.8.0", "2.0.0")
            and SHA.fullmatch(item.get("factory_sha", ""))
        }
        if (
            passport.get("ticket") != ticket
            or passport.get("factory_sha")
            != (source_factory_sha if emergency else factory_sha)
            or passport.get("contract_version") not in ("1.8.0", "2.0.0")
            or (not emergency and passport.get("publication_state") != "merged")
            or not isinstance(history, list)
            or len(history_shas) != len(history)
            or (not emergency and factory_sha not in history_shas)
            or (successor and source_factory_sha not in history_shas)
            or not isinstance(charges, list)
            or not isinstance(completed, list)
            or (successor and not isinstance(migrations, list))
        ):
            raise QualificationError(f"{ticket} passport is not terminal")
        if successor and not emergency and not successor_release_lineage(
            history, migrations, source_factory_sha, factory_sha
        ):
            raise QualificationError(f"{ticket} successor migration is missing")
        if (
            any(
                not isinstance(item.get("charge_micro_usd"), int)
                or item["charge_micro_usd"] < 0
                or item["charge_micro_usd"] > per_run_cap
                or item.get("factory_sha") not in history_shas
                or item.get("contract_version") not in ("1.8.0", "2.0.0")
                or not DIGEST.fullmatch(item.get("manifest_sha256", ""))
                for item in charges
            )
        ):
            raise QualificationError(f"{ticket} charges do not match the envelope")
        charge = sum(item["charge_micro_usd"] for item in charges)
        qualification_charge = sum(
            item["charge_micro_usd"]
            for item in charges if item.get("factory_sha") == factory_sha
        )
        if (
            charge != passport.get("cumulative_charges_micro_usd")
            or qualification_charge > ticket_caps[ticket]
        ):
            raise QualificationError(f"{ticket} charges do not match the envelope")
        for item in charges:
            if (
                not isinstance(item.get("run_id"), str)
                or not item["run_id"]
                or item["run_id"] in run_ids
                or item["manifest_sha256"] in manifest_digests
            ):
                raise QualificationError("run or charge evidence was duplicated")
            run_ids.add(item["run_id"])
            manifest_digests.add(item["manifest_sha256"])
        role_heads = [(item.get("role"), item.get("head_before")) for item in completed]
        if (
            not ROLES.issubset({role for role, _ in role_heads})
            or len(role_heads) != len(set(role_heads))
            or any(
                item.get("factory_sha") not in history_shas
                or item.get("contract_version") not in ("1.8.0", "2.0.0")
                or not SHA.fullmatch(item.get("head_before", ""))
                or not DIGEST.fullmatch(item.get("transition_receipt_sha256", ""))
                for item in completed
            )
        ):
            raise QualificationError(f"{ticket} role evidence was replayed or is incomplete")
        if emergency:
            plan = done.get("plan") if isinstance(done, dict) else None
            passport_basis = plan.get("passport") if isinstance(plan, dict) else None
            claim_basis = plan.get("claim") if isinstance(plan, dict) else None
            allowed = {
                "done_sha256", "event", "event_sha256", "factory_sha",
                "observed_at_epoch_ns", "pause_file_sha256",
                "pause_receipt_sha256", "protected_main_sha",
                "protected_main_tree", "protected_ticket_blob",
                "qualification_charge_micro_usd", "qualification_generation",
                "qualification_manifest_sha256", "reconciliation_schema",
                "schema", "source_current_state", "source_factory_sha",
                "source_head_sha", "source_passport_sha256",
                "source_publication_state", "terminal_basis",
                "terminal_factory_sha", "terminal_release_receipt_id", "ticket",
            }
            required = allowed - {
                "event_sha256", "qualification_generation",
                "qualification_manifest_sha256", "schema",
            }
            if (
                not successor
                or not required.issubset(emergency)
                or not set(emergency).issubset(allowed)
                or emergency.get("reconciliation_schema")
                != EMERGENCY_TERMINAL_RECONCILIATION_SCHEMA
                or emergency.get("terminal_basis")
                != "attested-emergency-closeout"
                or not SHA.fullmatch(emergency.get("terminal_factory_sha", ""))
                or not DIGEST.fullmatch(
                    emergency.get("terminal_release_receipt_id", "")
                )
                or emergency.get("qualification_charge_micro_usd") != 0
                or emergency.get("source_factory_sha") != source_factory_sha
                or emergency.get("source_factory_sha") != passport.get("factory_sha")
                or emergency.get("source_current_state")
                != passport.get("current_state")
                or emergency.get("source_publication_state")
                != passport.get("publication_state")
                or emergency.get("source_head_sha") != passport.get("head_sha")
                or emergency.get("source_passport_sha256")
                != passport.get("passport_sha256")
                or not SHA.fullmatch(emergency.get("protected_main_sha", ""))
                or not SHA.fullmatch(emergency.get("protected_main_tree", ""))
                or not SHA.fullmatch(emergency.get("protected_ticket_blob", ""))
                or not DIGEST.fullmatch(emergency.get("pause_file_sha256", ""))
                or not DIGEST.fullmatch(emergency.get("pause_receipt_sha256", ""))
                or done.get("schema") not in {
                    "nysa.software-factory.ticket-emergency-done/v1",
                    "nysa.software-factory.ticket-emergency-done/v2",
                }
                or done.get("ticket") != ticket
                or done.get("kit_sha") != emergency.get("terminal_factory_sha")
                or not isinstance(plan, dict)
                or plan.get("kit_sha") != emergency.get("terminal_factory_sha")
                or plan.get("execution_basis") != "authenticated-passport"
                or passport_basis != {
                    name: passport.get(name)
                    for name in (
                        "passport_sha256", "current_state", "publication_state",
                        "factory_sha", "head_sha",
                    )
                }
                or not isinstance(claim_basis, dict)
                or claim_basis.get("status") != "blocked"
                or claim_basis.get("role") != "factory-paused"
                or claim_basis.get("blocked_reason") != "factory-issue-pause"
                or claim_basis.get("parked") is not True
                or emergency.get("pause_file_sha256") != claim_basis.get("sha256")
                or emergency.get("pause_receipt_sha256") != claim_basis.get("receipt")
                or emergency.get("done_sha256")
                != hashlib.sha256(canonical(done).encode()).hexdigest()
                or done.get("required_checks") != done.get("successful_checks")
                or not done.get("required_checks")
                or pr.get("number") != done.get("pr_number")
                or pr.get("headRefName") != passport.get("branch")
                or pr.get("headRefOid") != done.get("pr_head")
                or pr.get("headRefOid") != passport.get("head_sha")
                or pr.get("baseRefName") != "main"
                or pr.get("state") != "MERGED"
                or merge != done.get("merge_commit")
                or not SHA.fullmatch(merge or "")
                or factory_sha in history_shas
                or any(
                    item.get("factory_sha") == factory_sha
                    for values in (charges, completed)
                    for item in values
                    if isinstance(item, dict)
                )
            ):
                raise QualificationError(
                    f"{ticket} emergency terminal reconciliation is invalid"
                )
        elif (
            done.get("schema") != "nysa.software-factory.ticket-done/v1"
            or done.get("ticket") != ticket
            or done.get("kit_sha") not in history_shas
            or done.get("required_checks") != done.get("successful_checks")
            or not done.get("required_checks")
            or pr.get("number") != done.get("pr_number")
            or pr.get("headRefName") != passport.get("branch")
            or pr.get("headRefOid") != done.get("approved_pr_head")
            or pr.get("baseRefName") != "main"
            or pr.get("state") != "MERGED"
            or merge != done.get("merge_commit")
            or not SHA.fullmatch(merge or "")
            or not passport_head_lineage(
                passport, done.get("approved_pr_head", "")
            )
        ):
            raise QualificationError(f"{ticket} protected merge truth does not match")
        total += charge
        qualification_total += qualification_charge
        ticket_reports.append({
            "charge_micro_usd": charge,
            "evidence_mode": (
                "passport-emergency-closeout" if emergency else "passport"
            ),
            "qualification_charge_micro_usd": qualification_charge,
            "merge_commit": merge,
            "pr_head": pr["headRefOid"],
            "pr_number": pr["number"],
            "required_checks": done["required_checks"],
            "roles": len(completed),
            "ticket": ticket,
        })
    qualification_budget = int(Decimal(manifest["budget_usd"]) * 1_000_000)
    if qualification_total > qualification_budget:
        raise QualificationError("qualification exceeded its total budget")

    passport_histories = [
        {
            item["factory_sha"]
            for item in passports[ticket]["factory_release_history"]
        }
        for ticket in tickets if ticket in passports
    ]
    common_history = (
        set.intersection(*passport_histories) if passport_histories else set()
    )
    relevant = [
        item for item in events
        if item.get("factory_sha") == factory_sha
        or (not successor and item.get("factory_sha") in common_history)
    ]
    current = [
        item["observed_at_epoch_ns"]
        for item in relevant if item.get("factory_sha") == factory_sha
    ]
    if not current or any(
        item["observed_at_epoch_ns"] > min(current)
        and item.get("factory_sha") != factory_sha
        for item in events
    ):
        raise QualificationError("Factory candidate changed after its final freeze")

    def matching(name: str) -> list[dict[str, Any]]:
        return [item for item in relevant if item.get("event") == name]

    boundaries = matching("restart_boundary")
    recoveries = matching("controller_recovered")
    relocations = matching("cell_relocated")
    completions = [
        item for item in matching("ticket_complete")
        if item.get("ticket") in tickets
    ]
    adoptions = matching("terminal_adopted")
    adopted: set[str] = set()
    for item in adoptions:
        ticket = item.get("ticket")
        passport = passports.get(ticket)
        done = terminals.get(ticket)
        pr = pull_requests.get(ticket)
        migrations = passport.get("migration_history") if passport else None
        history = passport.get("factory_release_history") if passport else None
        edge = migrations[-1] if isinstance(migrations, list) and migrations else {}
        pre_candidate_history = {
            release.get("factory_sha") for release in history or []
            if isinstance(release, dict)
            and release.get("factory_sha") != factory_sha
        }
        if (
            not successor
            or ticket not in tickets
            or ticket in adopted
            or not isinstance(passport, dict)
            or not isinstance(done, dict)
            or not isinstance(pr, dict)
            or item.get("adoption_schema") != TERMINAL_ADOPTION_SCHEMA
            or item.get("source_current_state") != "Approved"
            or item.get("source_factory_sha") != source_factory_sha
            or item.get("source_publication_state") != "merged"
            or item.get("candidate_passport_sha256")
            != passport.get("passport_sha256")
            or item.get("source_passport_sha256")
            != edge.get("from_passport_sha256")
            or item.get("source_passport_sha256")
            != passport.get("parent_digest")
            or edge.get("schema") != PASSPORT_MIGRATION_SCHEMA
            or item.get("passport_source_factory_sha")
            != edge.get("from_factory_sha")
            or item.get("passport_source_factory_sha")
            not in pre_candidate_history
            or edge.get("to_factory_sha") != factory_sha
            or passport.get("current_state") != "Approved"
            or passport.get("publication_state") != "merged"
            or any(
                evidence.get("factory_sha") == factory_sha
                for name in ("charge_records", "completed_role_evidence")
                for evidence in passport.get(name, [])
                if isinstance(evidence, dict)
            )
            or done.get("kit_sha") not in pre_candidate_history
            or item.get("done_sha256")
            != hashlib.sha256(canonical(done).encode()).hexdigest()
            or item.get("pr_number") != done.get("pr_number")
            or item.get("pr_number") != pr.get("number")
            or item.get("approved_pr_head") != done.get("approved_pr_head")
            or item.get("approved_pr_head") != pr.get("headRefOid")
            or item.get("merge_commit") != done.get("merge_commit")
            or item.get("merge_commit") != (pr.get("mergeCommit") or {}).get("oid")
        ):
            raise QualificationError("terminal adoption proof is invalid")
        adopted.add(ticket)
    boundary_tickets = (
        boundaries[0].get("tickets") if len(boundaries) == 1 else None
    )
    publication_targets = (
        set(tickets) - adopted - reconciled - emergency_reconciled
    )
    if (
        not isinstance(boundary_tickets, list)
        or len(boundary_tickets) not in (
            {target_done} if successor else {target_done, 4}
        )
        or len(set(boundary_tickets)) != len(boundary_tickets)
        or not set(tickets).issubset(boundary_tickets)
        or len(recoveries) != 1
        or recoveries[0].get("tickets") != boundary_tickets
        or len(relocations) > 1
        or (publication_targets and len(relocations) != 1)
        or (
            relocations
            and (
                relocations[0].get("ticket") not in boundary_tickets
                or (
                    len(boundary_tickets) == target_done
                    and relocations[0].get("ticket") not in tickets
                )
            )
        )
        or len(completions) != len(tickets)
        or {item.get("ticket") for item in completions} != set(tickets)
    ):
        raise QualificationError("restart, relocation, or completion proof is missing")
    latency = None
    if not successor and target_done == 3:
        if reconciled or emergency_reconciled or adopted:
            raise QualificationError("qualification latency requires a fresh cohort")
        latency = qualification_latency(
            manifest, passports, relevant, boundaries[0], completions,
            activation_receipt or {}, narrator_run_ids or {},
            provider_planner_starts or {},
        )
    holder = None
    acquired: set[str] = set()
    released: set[str] = set()
    acquisition_count = 0
    release_count = 0
    for item in relevant:
        if item.get("ticket") not in tickets:
            continue
        if item.get("event") == "publication_acquired":
            if holder is not None:
                raise QualificationError("publication leases overlapped")
            holder = item.get("ticket")
            acquired.add(holder)
            acquisition_count += 1
        elif item.get("event") == "publication_released":
            if item.get("ticket") != holder:
                raise QualificationError("publication lease release is out of order")
            released.add(holder)
            holder = None
            release_count += 1
    if (
        holder is not None
        or acquired != publication_targets
        or released != publication_targets
        or acquisition_count != len(publication_targets)
        or release_count != len(publication_targets)
    ):
        raise QualificationError("publication serialization proof is incomplete")
    created = [iso(pull_requests[ticket]["createdAt"]) for ticket in tickets]
    merged = [iso(pull_requests[ticket]["mergedAt"]) for ticket in tickets]
    if target_done == 4 and max(created) > min(merged):
        raise QualificationError("target PRs did not validate concurrently")
    report = {
        "factory_sha": factory_sha,
        "qualification_manifest_sha256": hashlib.sha256(
            canonical(manifest).encode()
        ).hexdigest(),
        "schema": SCHEMA,
        "status": "green",
        "tickets": ticket_reports,
        "total_charge_micro_usd": total,
        "qualification_charge_micro_usd": qualification_total,
    }
    if latency is not None:
        report["latency"] = latency
        if (
            not isinstance(provider_accounting, dict)
            or set(provider_accounting) != {
                "attempt_count", "evidence_sha256", "launch_void_count",
                "reservation_micro_usd", "terminal_charge_micro_usd",
            }
            or not DIGEST.fullmatch(provider_accounting.get("evidence_sha256", ""))
            or any(
                isinstance(provider_accounting.get(name), bool)
                or not isinstance(provider_accounting.get(name), int)
                or provider_accounting[name] < 0
                for name in (
                    "attempt_count", "launch_void_count",
                    "reservation_micro_usd", "terminal_charge_micro_usd",
                )
            )
            or provider_accounting["attempt_count"] < 1
            or provider_accounting["launch_void_count"]
            > provider_accounting["attempt_count"]
            or provider_accounting["terminal_charge_micro_usd"]
            > provider_accounting["reservation_micro_usd"]
        ):
            raise QualificationError("provider accounting proof is missing")
        report["provider_accounting"] = provider_accounting
    return report


def effective_ticket_caps(
    product: Path, kit_dir: Path, manifest: dict[str, Any]
) -> dict[str, int]:
    spec = importlib.util.spec_from_file_location(
        "qualification_envelope", kit_dir / "scripts/envelope-control.py"
    )
    if not spec or not spec.loader:
        raise QualificationError("envelope verifier is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        base = Decimal(manifest["per_ticket_budget_usd"])
        extended = (
            manifest.get("budget_usd"),
            manifest.get("per_ticket_budget_usd"),
            manifest.get("per_run_budget_usd"),
        ) == ("300.000000", "100.000000", "10.000000")
        if manifest.get("mode") == "successor" or extended:
            return {
                ticket: int(base * 1_000_000)
                for ticket in manifest["tickets"]
            }
        state = module.read_runtime_state(product)
        day = datetime.now(timezone.utc).date().isoformat()
        result = {}
        for ticket in manifest["tickets"]:
            _, changes = module.load_override_records(
                state[1], ticket, "reviewer", day, {"ticket"}
            )
            value = Decimal(changes.get("PER_TICKET_BUDGET_USD", str(base)))
            micro = value * 1_000_000
            if micro != micro.to_integral_value():
                raise QualificationError("ticket cap has excess precision")
            result[ticket] = int(micro)
        return result
    except (
        AttributeError, InvalidOperation, KeyError, TypeError, ValueError,
        module.ControlError,
    ) as error:
        raise QualificationError("authenticated ticket caps are invalid") from error


def validate_protected_reconciliation(
    product: Path, ticket: str, event: dict[str, Any],
    protected: str, done: dict[str, Any],
) -> None:
    observed = event.get("protected_main_sha", "")
    ticket_path = f"factory/tickets/{ticket}.md"
    done_path = f"factory/attestations/{ticket}/done.json"
    if not SHA.fullmatch(observed):
        raise QualificationError(
            f"{ticket} protected terminal reconciliation is invalid"
        )
    try:
        terminal = protected_terminal(product, ticket)
        command(
            "git", "-C", str(product), "merge-base", "--is-ancestor",
            observed, protected,
        )
        observed_tree = command(
            "git", "-C", str(product), "rev-parse", f"{observed}^{{tree}}",
        ).strip()
        observed_ticket = command(
            "git", "-C", str(product), "rev-parse", f"{observed}:{ticket_path}",
        ).strip()
        current_ticket = command(
            "git", "-C", str(product), "rev-parse", f"{protected}:{ticket_path}",
        ).strip()
        observed_done = json.loads(command(
            "git", "-C", str(product), "show", f"{observed}:{done_path}",
        ))
    except (json.JSONDecodeError, ProtectedTerminalError) as error:
        raise QualificationError(
            f"{ticket} protected terminal reconciliation is invalid"
        ) from error
    if (
        terminal.get("ticket") != ticket
        or terminal.get("basis") != event.get("terminal_basis")
        or observed_tree != event.get("protected_main_tree")
        or observed_ticket != event.get("protected_ticket_blob")
        or current_ticket != observed_ticket
        or observed_done != done
        or hashlib.sha256(canonical(observed_done).encode()).hexdigest()
        != event.get("done_sha256")
    ):
        raise QualificationError(
            f"{ticket} protected terminal reconciliation changed"
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--product-root", required=True, type=Path)
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--kit-dir", required=True, type=Path)
    parser.add_argument("--qualification-root", required=True, type=Path)
    parser.add_argument("--project", required=True)
    args = parser.parse_args()
    try:
        product = args.product_root.resolve(strict=True)
        state = args.state_dir.resolve(strict=True)
        qualification_root = args.qualification_root.resolve(strict=True)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", args.project):
            raise QualificationError("qualification project is invalid")
        manifest = json.loads(
            regular(product / "factory/QUALIFICATION.json").decode("utf-8")
        )
        if not SHA.fullmatch(manifest.get("factory_sha", "")):
            raise QualificationError("qualification manifest is invalid")
        destination = state / f"qualification-report-{manifest['factory_sha']}.json"
        retained = retained_report(destination, manifest)
        if retained is not None:
            revalidate_report_checks(retained, project_value(product, "GH_REPO"))
            sys.stdout.buffer.write(retained)
            return
        events = qualification_events(
            event_records(state / "events"), manifest,
        )
        reconciliations = protected_reconciliations(
            events, manifest.get("factory_sha", ""),
        )
        if not set(reconciliations).issubset(set(manifest.get("tickets", []))):
            raise QualificationError("qualification inputs are incomplete")
        spec = importlib.util.spec_from_file_location(
            "ticket_passport", args.kit_dir / "scripts/ticket-passport.py"
        )
        if not spec or not spec.loader:
            raise QualificationError("passport verifier is unavailable")
        passport_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(passport_module)
        secret = passport_module.key(state)
        passports = {
            ticket: passport_module.load_passport(
                state / "passports" / f"{ticket}.json", secret
            )[0]
            for ticket in manifest["tickets"] if ticket not in reconciliations
        }
        command(
            "git", "-C", str(product), "fetch", "--quiet", "origin",
            "+main:refs/remotes/origin/main",
        )
        protected = command(
            "git", "-C", str(product), "rev-parse", "origin/main"
        ).strip()
        repo = project_value(product, "GH_REPO")
        terminals, pull_requests, narrator_run_ids = {}, {}, {}
        activation_receipt = {}
        accounting_proof = None
        provider_planner_starts = None
        fresh_timing = (
            manifest.get("mode") != "successor"
            and manifest.get("target_done") == 3
        )
        if fresh_timing:
            active = json.loads(regular(
                qualification_root / "projects"
                / args.project / "active.json",
                mode=0o600,
            ).decode())
            receipt_id = active.get("receipt_id", "")
            if not DIGEST.fullmatch(receipt_id):
                raise QualificationError("qualification activation receipt is invalid")
            activation_receipt = json.loads(regular(
                qualification_root / "receipts" / f"{receipt_id}.json",
                mode=0o600,
            ).decode())
            if (
                activation_receipt.get("receipt_id") != receipt_id
                or activation_receipt.get("project") != args.project
                or activation_receipt.get("kit_sha") != manifest.get("factory_sha")
                or any(
                    activation_receipt.get(name) != active.get(name)
                    for name in (
                        "kit_sha", "kit_tree", "product_sha", "product_tree",
                        "project", "provider_state_path",
                    )
                )
            ):
                raise QualificationError("qualification activation receipt is invalid")
            provider_root = Path(active.get("provider_state_path", ""))
            provider_db = provider_root / "accounting/state-v2.sqlite3"
            if (
                not provider_root.is_absolute() or provider_root.is_symlink()
                or not provider_root.is_dir() or provider_db.is_symlink()
                or not provider_db.is_file()
            ):
                raise QualificationError("qualification provider authority is invalid")
            provider_status = json.loads(command(
                "python3", str(args.kit_dir / "scripts/provider-coordinator.py"),
                "--db", str(provider_db), "status",
            ))
            accounting_proof, provider_planner_starts = provider_accounting_evidence(
                product, manifest, passports, events, provider_status, args.project,
            )
        for ticket in manifest["tickets"]:
            terminals[ticket] = json.loads(command(
                "git", "-C", str(product), "show",
                f"origin/main:factory/attestations/{ticket}/done.json",
            ))
            if fresh_timing:
                try:
                    protected_terminal(product, ticket)
                    bundle = json.loads(command(
                        "git", "-C", str(product), "show",
                        f"origin/main:factory/attestations/{ticket}/bundle.json",
                    ))
                except (json.JSONDecodeError, ProtectedTerminalError) as error:
                    raise QualificationError(
                        f"{ticket} protected Narrator evidence is invalid"
                    ) from error
                narrator_run_ids[ticket] = bundle.get("narrator_run_id", "")
            if ticket in reconciliations:
                validate_protected_reconciliation(
                    product, ticket, reconciliations[ticket], protected,
                    terminals[ticket],
                )
            pr_number = terminals[ticket]["pr_number"]
            pull_requests[ticket] = json.loads(command(
                "gh", "pr", "view", str(pr_number), "--repo", repo, "--json",
                "number,headRefName,headRefOid,baseRefName,state,createdAt,mergedAt,mergeCommit",
            ))
            merge = (pull_requests[ticket].get("mergeCommit") or {}).get("oid", "")
            command(
                "git", "-C", str(product), "merge-base", "--is-ancestor",
                merge, "origin/main",
            )
            if not set(terminals[ticket]["required_checks"]).issubset(
                successful_checks(repo, pull_requests[ticket]["headRefOid"])
            ):
                raise QualificationError(f"{ticket} protected checks are not green")
        report = verify(
            manifest, passports,
            events,
            terminals, pull_requests,
            effective_ticket_caps(product, args.kit_dir, manifest),
            activation_receipt, narrator_run_ids, accounting_proof,
            provider_planner_starts,
        )
        report["protected_main_sha"] = protected
        report["report_sha256"] = hashlib.sha256(canonical(report).encode()).hexdigest()
        raw = (canonical(report) + "\n").encode()
        write_immutable(destination, raw)
        print(canonical(report))
    except ExternalUnavailable:
        print('{"reason_code":"external_unavailable","status":"wait"}')
        raise SystemExit(75)
    except (
        FileNotFoundError, json.JSONDecodeError, OSError, QualificationError,
        subprocess.SubprocessError,
    ) as error:
        print(canonical({"error": str(error), "schema": SCHEMA, "status": "error"}))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
