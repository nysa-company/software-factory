#!/usr/bin/env python3
"""Reduce Contract 1.8 qualification evidence against protected GitHub truth."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
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


class QualificationError(ValueError):
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


def command(*arguments: str, cwd: Path | None = None) -> str:
    result = subprocess.run(
        arguments, cwd=cwd, text=True, capture_output=True, check=False, timeout=120,
    )
    if result.returncode:
        raise QualificationError(
            result.stderr.strip() or result.stdout.strip() or "evidence query failed"
        )
    return result.stdout


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


def verify(
    manifest: dict[str, Any],
    passports: dict[str, dict[str, Any]],
    events: list[dict[str, Any]],
    terminals: dict[str, dict[str, Any]],
    pull_requests: dict[str, dict[str, Any]],
    ticket_caps: dict[str, int],
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
    return {
        "factory_sha": factory_sha,
        "schema": SCHEMA,
        "status": "green",
        "tickets": ticket_reports,
        "total_charge_micro_usd": total,
        "qualification_charge_micro_usd": qualification_total,
    }


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
    args = parser.parse_args()
    try:
        product = args.product_root.resolve(strict=True)
        state = args.state_dir.resolve(strict=True)
        manifest = json.loads(
            regular(product / "factory/QUALIFICATION.json").decode("utf-8")
        )
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
        terminals, pull_requests = {}, {}
        for ticket in manifest["tickets"]:
            terminals[ticket] = json.loads(command(
                "git", "-C", str(product), "show",
                f"origin/main:factory/attestations/{ticket}/done.json",
            ))
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
            checks = json.loads(command(
                "gh", "api", f"repos/{repo}/commits/{merge}/check-runs",
                "--method", "GET", "-f", "per_page=100",
            )).get("check_runs", [])
            successes = {
                item.get("name") for item in checks
                if item.get("status") == "completed"
                and item.get("conclusion") in {"success", "neutral", "skipped"}
            }
            if not set(terminals[ticket]["required_checks"]).issubset(successes):
                raise QualificationError(f"{ticket} protected checks are not green")
        report = verify(
            manifest, passports,
            events,
            terminals, pull_requests,
            effective_ticket_caps(product, args.kit_dir, manifest),
        )
        report["protected_main_sha"] = protected
        report["report_sha256"] = hashlib.sha256(canonical(report).encode()).hexdigest()
        destination = state / f"qualification-report-{manifest['factory_sha']}.json"
        raw = (canonical(report) + "\n").encode()
        try:
            descriptor = os.open(
                destination,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
        except FileExistsError:
            if regular(destination, 0o600) != raw:
                raise QualificationError("immutable qualification report changed")
        else:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
        print(canonical(report))
    except (
        FileNotFoundError, json.JSONDecodeError, OSError, QualificationError,
        subprocess.SubprocessError,
    ) as error:
        print(canonical({"error": str(error), "schema": SCHEMA, "status": "error"}))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
