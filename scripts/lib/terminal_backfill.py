#!/usr/bin/env python3
"""Strict validation for the bounded pre-contract terminal-Done backfill."""

import csv
import hashlib
import io
import re
from pathlib import Path

from legacy_closeout import (
    MIGRATION_DIR as LEGACY_MIGRATION_DIR,
    ValidationError,
    blob_at,
    digest,
    exact,
    hash_text,
    json_at,
    oid,
    one_field,
    repository_from_project,
    run,
    text_at,
    timestamp,
)


MIGRATION_DIR = "factory/migrations/contract-1.3-terminal-backfill"
AUTH_SCHEMA = "nysa.software-factory.terminal-backfill-authorization/v1"
RECEIPT_SCHEMA = "nysa.software-factory.terminal-backfill/v1"
CLASSIFICATION = "pre-contract-terminal-done"
AUTHORIZED_TICKETS = tuple(f"T-{number:03d}" for number in range(1, 13))
CHECK_NAMES = ("ci", "test-immutability")
ROLES = frozenset(
    ("planner", "spec-linter", "test-author", "builder", "reviewer", "narrator")
)

AUTH_KEYS = {
    "schema", "repository", "basis_kit_sha", "target_kit_sha",
    "candidate_contract", "tickets", "authorization", "cutoff",
    "protected_main_basis",
}
AUTH_TICKET_KEYS = {
    "ticket", "classification", "source_state", "receipt",
    "implementation_pr_number", "closeout_pr_number", "required_checks",
}
AUTHORIZATION_KEYS = {"method", "statement", "auto_merge", "bypass"}
BASIS_KEYS = {"commit", "tree"}
CHECK_IDENTITY_KEYS = {"name", "app_id", "app_slug"}
RECEIPT_KEYS = {
    "schema", "ticket", "repository", "classification", "source_state",
    "basis_kit_sha", "target_kit_sha", "candidate_contract",
    "source_ticket_blob", "source_bundle_blob", "source_kit_sha",
    "closeout_ticket_blob", "closeout_bundle_blob",
    "implementation_pr", "closeout_pr", "checks", "ledger",
    "authorization_blob", "cutoff", "protected_main_basis", "route_plan",
}
PR_KEYS = {
    "number", "head_ref", "base_ref", "head", "merge_commit", "merged_at",
    "merged_by",
}
CHECK_KEYS = {
    "name", "app_id", "app_slug", "status", "conclusion", "skipped",
}
LEDGER_KEYS = {"sha256", "run_ids"}
ROUTE_PLAN_KEYS = {"present", "sha256"}


def _nullable_oid(value, label):
    if value is not None:
        oid(value, label)


def _validate_check_identities(required, label):
    if not isinstance(required, list) or len(required) != len(CHECK_NAMES):
        raise ValidationError(f"{label} check identities are incomplete")
    identities = {}
    for item in required:
        exact(item, CHECK_IDENTITY_KEYS, f"{label} check identity")
        if (
            item["name"] in identities
            or item["name"] not in CHECK_NAMES
            or not isinstance(item["app_id"], int)
            or item["app_id"] <= 0
            or not isinstance(item["app_slug"], str)
            or not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9-]{0,99}", item["app_slug"]
            )
        ):
            raise ValidationError(f"{label} check identity is invalid")
        identities[item["name"]] = item
    if tuple(sorted(identities)) != CHECK_NAMES:
        raise ValidationError(f"{label} check names are not exact")
    return identities


def _validate_pr(repo, basis, cutoff, ticket, value, expected_number, label):
    pr = exact(value, PR_KEYS, f"{ticket} {label} PR")
    if (
        pr["number"] != expected_number
        or not isinstance(expected_number, int)
        or expected_number <= 0
        or pr["base_ref"] != "main"
        or not isinstance(pr["head_ref"], str)
        or not pr["head_ref"]
        or not isinstance(pr["merged_by"], str)
        or not pr["merged_by"]
    ):
        raise ValidationError(f"{ticket} {label} PR identity is invalid")
    oid(pr["head"], f"{ticket} {label} PR head")
    oid(pr["merge_commit"], f"{ticket} {label} PR merge commit")
    if run(
        repo, "merge-base", "--is-ancestor", pr["merge_commit"], basis,
        check=False,
    ).returncode:
        raise ValidationError(f"{ticket} {label} PR merge is outside the basis")
    if timestamp(pr["merged_at"], f"{ticket} {label} PR merged_at") > cutoff:
        raise ValidationError(f"{ticket} {label} PR merged after the cutoff")
    return pr


def _validate_ledger(repo, basis, ticket, ledger):
    exact(ledger, LEDGER_KEYS, f"{ticket} ledger evidence")
    digest(ledger["sha256"], f"{ticket} ledger sha256")
    if (
        not isinstance(ledger["run_ids"], list)
        or not ledger["run_ids"]
        or len(ledger["run_ids"]) != len(set(ledger["run_ids"]))
        or any(not isinstance(item, str) or not item for item in ledger["run_ids"])
    ):
        raise ValidationError(f"{ticket} ledger run IDs are absent or ambiguous")
    text = text_at(repo, basis, "factory/ledger.csv")
    if text is None or hashlib.sha256(text.encode()).hexdigest() != ledger["sha256"]:
        raise ValidationError(f"{ticket} ledger digest does not match the basis")
    rows = list(csv.DictReader(io.StringIO(text)))
    ticket_rows = [row for row in rows if row.get("ticket") == ticket]
    actual_ids = [row.get("run_id") for row in ticket_rows]
    if (
        not ticket_rows
        or any(not value for value in actual_ids)
        or len(actual_ids) != len(set(actual_ids))
        or actual_ids != ledger["run_ids"]
    ):
        raise ValidationError(f"{ticket} ledger does not bind every ticket row")
    successful = {
        row.get("role") for row in ticket_rows if row.get("exit_status") == "0"
    }
    if not {"reviewer", "narrator"} <= successful or not successful <= ROLES:
        raise ValidationError(
            f"{ticket} ledger lacks successful Reviewer/Narrator evidence"
        )


def _migration_commit(repo, ref, basis, expected_paths):
    additions = run(
        repo, "log", "--format=%H", "--diff-filter=A", ref, "--",
        f"{MIGRATION_DIR}/authorization.json",
    ).stdout.splitlines()
    matches = []
    for commit in additions:
        parents = run(repo, "show", "-s", "--format=%P", commit).stdout.split()
        paths = set(run(
            repo, "diff-tree", "--no-commit-id", "--name-only", "-r", commit,
        ).stdout.splitlines())
        if parents == [basis] and paths == expected_paths:
            matches.append(commit)
    if len(matches) != 1:
        raise ValidationError(
            "terminal backfill must have one atomic protected introduction"
        )
    return matches[0]


def _legacy_companion_paths(repo, ref, authorization):
    companion = json_at(
        repo, ref, f"{LEGACY_MIGRATION_DIR}/authorization.json",
        "legacy companion authorization",
    )
    if companion is None:
        return set()
    if (
        not isinstance(companion, dict)
        or companion.get("repository") != authorization["repository"]
        or companion.get("source_kit_sha") != authorization["basis_kit_sha"]
        or companion.get("target_kit_sha") != authorization["target_kit_sha"]
        or companion.get("candidate_contract") != authorization["candidate_contract"]
        or companion.get("cutoff") != authorization["cutoff"]
        or companion.get("protected_main_basis")
        != authorization["protected_main_basis"]
        or not isinstance(companion.get("tickets"), list)
    ):
        raise ValidationError("legacy companion does not match terminal backfill")
    paths = {f"{LEGACY_MIGRATION_DIR}/authorization.json"}
    for entry in companion["tickets"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("ticket"), str):
            raise ValidationError("legacy companion ticket is invalid")
        ticket = entry["ticket"]
        if entry.get("receipt") != f"{LEGACY_MIGRATION_DIR}/{ticket}.json":
            raise ValidationError("legacy companion receipt path is invalid")
        paths.add(entry["receipt"])
        paths.add(f"factory/tickets/{ticket}.md")
    return paths


def _validate_documents(repo, ref, authorization, receipts):
    exact(authorization, AUTH_KEYS, "terminal-backfill authorization")
    if (
        authorization["schema"] != AUTH_SCHEMA
        or authorization["candidate_contract"] != "1.3.0"
        or not re.fullmatch(
            r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+",
            authorization.get("repository", ""),
        )
    ):
        raise ValidationError("terminal-backfill authorization identity is invalid")
    oid(authorization["basis_kit_sha"], "terminal-backfill basis kit SHA")
    oid(authorization["target_kit_sha"], "terminal-backfill target kit SHA")
    if authorization["basis_kit_sha"] == authorization["target_kit_sha"]:
        raise ValidationError("terminal-backfill source and target kits must differ")
    if repository_from_project(repo, ref) != authorization["repository"]:
        raise ValidationError("terminal-backfill repository does not match protected main")
    cutoff = timestamp(authorization["cutoff"], "terminal-backfill cutoff")
    if cutoff.microsecond:
        raise ValidationError("terminal-backfill cutoff must use whole-second precision")
    basis = exact(
        authorization["protected_main_basis"], BASIS_KEYS,
        "terminal-backfill protected-main basis",
    )
    oid(basis["commit"], "terminal-backfill basis commit")
    oid(basis["tree"], "terminal-backfill basis tree")
    if run(repo, "rev-parse", f"{basis['commit']}^{{tree}}").stdout.strip() != basis["tree"]:
        raise ValidationError("terminal-backfill basis tree is inconsistent")
    if run(
        repo, "merge-base", "--is-ancestor", basis["commit"], ref, check=False,
    ).returncode:
        raise ValidationError("terminal-backfill basis is outside protected main")
    if timestamp(
        run(repo, "show", "-s", "--format=%cI", basis["commit"]).stdout.strip(),
        "terminal-backfill basis time",
    ) > cutoff:
        raise ValidationError("terminal-backfill basis is newer than its cutoff")
    basis_pin = text_at(repo, basis["commit"], "factory/KIT_PIN")
    if basis_pin != authorization["basis_kit_sha"] + "\n":
        raise ValidationError("terminal-backfill basis kit does not match")
    approval = exact(
        authorization["authorization"], AUTHORIZATION_KEYS,
        "terminal-backfill authorization payload",
    )
    if (
        approval["method"] != "manual-protected-main-merge"
        or approval["auto_merge"] is not False
        or approval["bypass"] is not False
        or not isinstance(approval["statement"], str)
        or approval["statement"].strip() != approval["statement"]
        or len(approval["statement"]) < 20
        or any(ord(character) < 32 for character in approval["statement"])
    ):
        raise ValidationError("terminal backfill requires manual protected merge")

    entries = authorization["tickets"]
    if not isinstance(entries, list):
        raise ValidationError("terminal-backfill tickets must be a list")
    expected = {}
    for entry in entries:
        exact(entry, AUTH_TICKET_KEYS, "terminal-backfill ticket authorization")
        ticket = entry["ticket"]
        if ticket in expected:
            raise ValidationError("terminal-backfill ticket is duplicated")
        if (
            entry["classification"] != CLASSIFICATION
            or entry["source_state"] != "Done"
            or entry["receipt"] != f"{MIGRATION_DIR}/{ticket}.json"
            or not isinstance(entry["implementation_pr_number"], int)
            or not isinstance(entry["closeout_pr_number"], int)
        ):
            raise ValidationError("terminal-backfill ticket authorization is invalid")
        _validate_check_identities(entry["required_checks"], ticket)
        expected[ticket] = entry
    if tuple(sorted(expected)) != AUTHORIZED_TICKETS:
        raise ValidationError("terminal backfill is limited to exact T-001 through T-012")
    if set(receipts) != set(expected):
        raise ValidationError("terminal-backfill receipt batch is partial or extra")

    expected_files = {
        f"{MIGRATION_DIR}/authorization.json",
        *(entry["receipt"] for entry in expected.values()),
    }
    actual_files = set(run(
        repo, "ls-tree", "-r", "--name-only", ref, "--", MIGRATION_DIR,
    ).stdout.splitlines())
    if actual_files != expected_files:
        raise ValidationError("terminal-backfill directory has missing or extra files")
    pin_changes = authorization["basis_kit_sha"] != authorization["target_kit_sha"]
    expected_paths = set(expected_files)
    if pin_changes:
        expected_paths.add("factory/KIT_PIN")
    expected_paths.update(_legacy_companion_paths(repo, ref, authorization))
    migration_commit = _migration_commit(
        repo, ref, basis["commit"], expected_paths
    )
    if text_at(repo, migration_commit, "factory/KIT_PIN") != (
        authorization["target_kit_sha"] + "\n"
    ):
        raise ValidationError("terminal-backfill migration does not target its kit")
    if timestamp(
        run(repo, "show", "-s", "--format=%cI", migration_commit).stdout.strip(),
        "terminal-backfill migration time",
    ) < cutoff:
        raise ValidationError("terminal-backfill migration predates its cutoff")
    for path in expected_files:
        if blob_at(repo, migration_commit, path) != blob_at(repo, ref, path):
            raise ValidationError("terminal-backfill evidence changed after merge")

    auth_blob = blob_at(repo, ref, f"{MIGRATION_DIR}/authorization.json")
    for ticket, entry in expected.items():
        receipt = exact(receipts[ticket], RECEIPT_KEYS, f"{ticket} terminal receipt")
        if (
            receipt["schema"] != RECEIPT_SCHEMA
            or receipt["ticket"] != ticket
            or receipt["repository"] != authorization["repository"]
            or receipt["classification"] != CLASSIFICATION
            or receipt["source_state"] != "Done"
            or receipt["basis_kit_sha"] != authorization["basis_kit_sha"]
            or receipt["target_kit_sha"] != authorization["target_kit_sha"]
            or receipt["candidate_contract"] != authorization["candidate_contract"]
            or receipt["authorization_blob"] != auth_blob
            or receipt["cutoff"] != authorization["cutoff"]
            or receipt["protected_main_basis"] != basis
        ):
            raise ValidationError(f"{ticket} terminal receipt does not match authorization")
        ticket_text = text_at(
            repo, basis["commit"], f"factory/tickets/{ticket}.md"
        )
        if (
            ticket_text is None
            or one_field(ticket_text, "State") != "Done"
            or hash_text(repo, ticket_text) != receipt["source_ticket_blob"]
        ):
            raise ValidationError(f"{ticket} source Done ticket changed")
        oid(receipt["source_ticket_blob"], f"{ticket} source ticket blob")
        bundle_path = f"factory/tickets/{ticket}-bundle.md"
        bundle_text = text_at(repo, basis["commit"], bundle_path)
        expected_bundle_blob = (
            hash_text(repo, bundle_text) if bundle_text is not None else None
        )
        if receipt["source_bundle_blob"] != expected_bundle_blob:
            raise ValidationError(f"{ticket} source bundle evidence is dishonest")
        _nullable_oid(receipt["source_bundle_blob"], f"{ticket} source bundle blob")
        kit_values = re.findall(r"(?mi)^Kit-SHA:\s*(.*?)\s*$", ticket_text)
        expected_kit = kit_values[0].strip() if len(kit_values) == 1 else None
        if len(kit_values) > 1 or receipt["source_kit_sha"] != expected_kit:
            raise ValidationError(f"{ticket} source Kit-SHA evidence is dishonest")
        _nullable_oid(receipt["source_kit_sha"], f"{ticket} source Kit-SHA")
        if receipt["route_plan"] != {"present": False, "sha256": None} or text_at(
            repo, basis["commit"], f"factory/route-plans/{ticket}.json"
        ) is not None:
            raise ValidationError("terminal backfill may not invent route-plan evidence")

        implementation = _validate_pr(
            repo, basis["commit"], cutoff, ticket, receipt["implementation_pr"],
            entry["implementation_pr_number"], "implementation",
        )
        closeout = _validate_pr(
            repo, basis["commit"], cutoff, ticket, receipt["closeout_pr"],
            entry["closeout_pr_number"], "closeout",
        )
        closeout_ticket = text_at(
            repo, closeout["head"], f"factory/tickets/{ticket}.md"
        )
        closeout_bundle = text_at(repo, closeout["head"], bundle_path)
        if (
            not re.match(rf"^ticket/{re.escape(ticket)}(?:-|$)", implementation["head_ref"])
            or run(
                repo, "merge-base", "--is-ancestor",
                implementation["merge_commit"], closeout["merge_commit"],
                check=False,
            ).returncode
            or closeout_ticket is None
            or one_field(closeout_ticket, "State") != "Done"
            or hash_text(repo, closeout_ticket) != receipt["closeout_ticket_blob"]
            or (
                hash_text(repo, closeout_bundle)
                if closeout_bundle is not None else None
            ) != receipt["closeout_bundle_blob"]
        ):
            raise ValidationError(f"{ticket} PR ancestry or source binding is inconsistent")
        oid(receipt["closeout_ticket_blob"], f"{ticket} closeout ticket blob")
        _nullable_oid(
            receipt["closeout_bundle_blob"], f"{ticket} closeout bundle blob"
        )

        identities = _validate_check_identities(entry["required_checks"], ticket)
        checks = receipt["checks"]
        if not isinstance(checks, list) or len(checks) != len(identities):
            raise ValidationError(f"{ticket} check evidence is incomplete")
        seen = set()
        for check in checks:
            exact(check, CHECK_KEYS, f"{ticket} historical check")
            name = check["name"]
            if (
                name in seen
                or name not in identities
                or check["app_id"] != identities[name]["app_id"]
                or check["app_slug"] != identities[name]["app_slug"]
                or check["status"] != "completed"
                or check["conclusion"] != "success"
                or check["skipped"] is not False
            ):
                raise ValidationError(
                    f"{ticket} historical check is failed, ambiguous, or wrong-app"
                )
            seen.add(name)
        if set(seen) != set(identities):
            raise ValidationError(f"{ticket} check evidence is incomplete")
        _validate_ledger(repo, basis["commit"], ticket, receipt["ledger"])
        if blob_at(repo, ref, f"factory/tickets/{ticket}.md") != receipt["source_ticket_blob"]:
            raise ValidationError(f"{ticket} protected Done ticket changed after backfill")

    return {
        ticket: {
            "basis": "validated-terminal-backfill",
            "ticket": ticket,
            "text": text_at(repo, ref, f"factory/tickets/{ticket}.md"),
            "target_kit_sha": authorization["target_kit_sha"],
        }
        for ticket in expected
    }


def terminal_backfill_batch(repo, ref="refs/remotes/origin/main"):
    repo = Path(repo)
    authorization = json_at(
        repo, ref, f"{MIGRATION_DIR}/authorization.json",
        "terminal-backfill authorization",
    )
    if authorization is None:
        return {}
    if not isinstance(authorization, dict):
        raise ValidationError("terminal-backfill authorization must be an object")
    entries = authorization.get("tickets")
    if not isinstance(entries, list):
        raise ValidationError("terminal-backfill authorization tickets must be a list")
    receipts = {}
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("ticket"), str):
            raise ValidationError("terminal-backfill authorization ticket is invalid")
        ticket = entry["ticket"]
        value = json_at(
            repo, ref, f"{MIGRATION_DIR}/{ticket}.json",
            f"{ticket} terminal-backfill receipt",
        )
        if value is not None:
            receipts[ticket] = value
    return _validate_documents(repo, ref, authorization, receipts)


def validate_generated_terminal_backfill(repo, authorization, receipts, ref):
    return _validate_documents(Path(repo), ref, authorization, receipts)
