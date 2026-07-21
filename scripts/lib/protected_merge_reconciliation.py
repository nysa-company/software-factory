#!/usr/bin/env python3
"""Validate a one-time protected-merge reconciliation batch."""

import csv
import hashlib
import io
import re
from pathlib import Path, PurePosixPath

from legacy_closeout import (
    NORMAL_APPROVAL_KEYS,
    NORMAL_BUNDLE_KEYS,
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


MIGRATION_DIR = "factory/migrations/protected-merge-reconciliation"
AUTH_SCHEMA = "nysa.software-factory.protected-merge-reconciliation-authorization/v1"
RECEIPT_SCHEMA = "nysa.software-factory.protected-merge-reconciliation/v1"
CLASSIFICATIONS = {
    "reviewed-clean-history-adoption",
    "merged-adoption",
}
AUTH_KEYS = {
    "schema", "repository", "basis_kit_sha", "target_kit_sha",
    "candidate_contract", "cutoff", "protected_main_basis",
    "required_checks", "authorization", "companions", "tickets",
}
AUTH_TICKET_KEYS = {
    "ticket", "source_state", "source_kit_sha", "classification",
    "evidence_head", "original_pr_number", "adoption_pr_number", "paths",
    "receipt",
}
CHECK_IDENTITY_KEYS = {"name", "app_id", "app_slug"}
AUTHORIZATION_KEYS = {
    "method", "operator", "authorized_at", "statement", "auto_merge", "bypass",
}
BASIS_KEYS = {"commit", "tree"}
RECEIPT_KEYS = {
    "schema", "ticket", "repository", "classification", "source_state",
    "source_kit_sha", "basis_kit_sha", "target_kit_sha", "candidate_contract",
    "evidence_head", "source_ticket_blob", "source_bundle_blob",
    "route_plan_blob", "route_plan_sha256", "bundle_attestation_blob",
    "approval_attestation_blob", "legacy_review", "original_pr", "adoption_pr", "paths",
    "checks", "ledger", "authorization_blob", "cutoff",
    "protected_main_basis",
}
PR_KEYS = {
    "number", "head_ref", "base_ref", "head", "merged", "merge_commit",
    "merged_at", "merged_by",
}
PATH_KEYS = {"path", "blob"}
CHECK_KEYS = {"name", "app_id", "app_slug", "status", "conclusion", "skipped"}
LEDGER_KEYS = {"sha256", "run_ids", "reviewer_run_id", "narrator_run_id"}
LEGACY_REVIEW_KEYS = {"reviewed_sha", "verdict_commit"}
TICKET_ID = re.compile(r"T-[0-9]+")


def _project_required_checks(repo, ref):
    text = text_at(repo, ref, "factory/PROJECT.env")
    if text is None:
        raise ValidationError("protected main lacks factory/PROJECT.env")
    values = []
    for raw in text.splitlines():
        match = re.fullmatch(r"\s*(?:export\s+)?DONE_REQUIRED_CHECKS\s*=\s*(.*?)\s*", raw)
        if match:
            value = match.group(1)
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            values.append(value)
    if len(values) != 1:
        raise ValidationError("factory/PROJECT.env must define one DONE_REQUIRED_CHECKS")
    names = values[0].split(",")
    if not names or any(not name or name != name.strip() for name in names):
        raise ValidationError("DONE_REQUIRED_CHECKS is invalid")
    if len(names) != len(set(names)):
        raise ValidationError("DONE_REQUIRED_CHECKS contains duplicates")
    return tuple(names)


def _check_identities(required, expected_names):
    if not isinstance(required, list) or len(required) != len(expected_names):
        raise ValidationError("reconciliation required-check identities are incomplete")
    identities = {}
    for item in required:
        exact(item, CHECK_IDENTITY_KEYS, "reconciliation check identity")
        if (
            item["name"] in identities
            or item["name"] not in expected_names
            or not isinstance(item["app_id"], int)
            or item["app_id"] <= 0
            or not isinstance(item["app_slug"], str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,99}", item["app_slug"])
        ):
            raise ValidationError("reconciliation check identity is invalid")
        identities[item["name"]] = item
    if set(identities) != set(expected_names):
        raise ValidationError("reconciliation check names do not match product policy")
    return identities


def _path(value, label):
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValidationError(f"{label} is invalid")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or path.parts[0] == "factory":
        raise ValidationError(f"{label} must be a non-factory repository path")
    return value


def _companion_path(value):
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValidationError("reconciliation companion path is invalid")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or value == "factory/KIT_PIN" or value.startswith(MIGRATION_DIR + "/"):
        raise ValidationError("reconciliation companion path is reserved or invalid")
    return value


def terminal_projection(text, receipt_path):
    """Return the only accepted migration terminal projection."""
    state = re.compile(r"(?mi)^State:\s*.*$")
    approval = re.compile(r"(?mi)^Operator-Approval:\s*.*$")
    receipt = re.compile(r"(?mi)^Migration-Receipt:\s*.*$")
    if len(state.findall(text)) != 1 or len(receipt.findall(text)):
        raise ValidationError("source ticket cannot be projected deterministically")
    projected = state.sub("State: Done", text, count=1)
    if len(approval.findall(projected)) > 1:
        raise ValidationError("source ticket has ambiguous operator approval")
    if approval.search(projected):
        projected = approval.sub("Operator-Approval: Migration", projected, count=1)
        match = approval.search(projected)
        return projected[:match.end()] + f"\nMigration-Receipt: {receipt_path}" + projected[match.end():]
    match = state.search(projected)
    return projected[:match.end()] + (
        f"\nOperator-Approval: Migration\nMigration-Receipt: {receipt_path}"
    ) + projected[match.end():]


def _validate_pr(repo, basis, cutoff, ticket, value, number, label, *, must_merge):
    pr = exact(value, PR_KEYS, f"{ticket} {label} PR")
    if (
        pr["number"] != number
        or not isinstance(number, int)
        or number <= 0
        or pr["base_ref"] != "main"
        or not isinstance(pr["head_ref"], str)
        or not pr["head_ref"]
    ):
        raise ValidationError(f"{ticket} {label} PR identity is invalid")
    oid(pr["head"], f"{ticket} {label} PR head")
    if pr["merged"] is not must_merge:
        raise ValidationError(f"{ticket} {label} PR merge state is invalid")
    if must_merge:
        oid(pr["merge_commit"], f"{ticket} {label} merge commit")
        if (
            not isinstance(pr["merged_by"], str)
            or not pr["merged_by"]
            or run(repo, "merge-base", "--is-ancestor", pr["merge_commit"], basis,
                   check=False).returncode
            or timestamp(pr["merged_at"], f"{ticket} {label} merged_at") > cutoff
        ):
            raise ValidationError(f"{ticket} {label} merged PR is outside the basis")
    elif any(pr[name] is not None for name in ("merge_commit", "merged_at", "merged_by")):
        raise ValidationError(f"{ticket} unmerged {label} PR invents merge evidence")
    return pr


def _validate_ledger(repo, basis, ticket, value, bundle):
    ledger = exact(value, LEDGER_KEYS, f"{ticket} ledger evidence")
    digest(ledger["sha256"], f"{ticket} ledger sha256")
    if (
        ledger["reviewer_run_id"] != bundle["reviewer_run_id"]
        or ledger["narrator_run_id"] != bundle["narrator_run_id"]
        or not isinstance(ledger["run_ids"], list)
        or not ledger["run_ids"]
        or len(ledger["run_ids"]) != len(set(ledger["run_ids"]))
        or any(not isinstance(item, str) or not item for item in ledger["run_ids"])
    ):
        raise ValidationError(f"{ticket} ledger run identity is incomplete")
    text = text_at(repo, basis, "factory/ledger.csv")
    if text is None or hashlib.sha256(text.encode()).hexdigest() != ledger["sha256"]:
        raise ValidationError(f"{ticket} ledger digest does not match the protected basis")
    rows = [row for row in csv.DictReader(io.StringIO(text)) if row.get("ticket") == ticket]
    actual_ids = [row.get("run_id") for row in rows]
    successful = {(row.get("run_id"), row.get("role")) for row in rows if row.get("exit_status") == "0"}
    if (
        not rows
        or actual_ids != ledger["run_ids"]
        or any(not value for value in actual_ids)
        or len(actual_ids) != len(set(actual_ids))
        or (ledger["reviewer_run_id"], "reviewer") not in successful
        or (ledger["narrator_run_id"], "narrator") not in successful
    ):
        raise ValidationError(f"{ticket} ledger does not bind successful Reviewer/Narrator runs")


def _validate_source_evidence(repo, ticket, receipt, original):
    evidence = receipt["evidence_head"]
    oid(evidence, f"{ticket} evidence head")
    if run(repo, "merge-base", "--is-ancestor", evidence, original["head"], check=False).returncode:
        raise ValidationError(f"{ticket} evidence head is not in the original PR")
    ticket_path = f"factory/tickets/{ticket}.md"
    bundle_path = f"factory/tickets/{ticket}-bundle.md"
    route_path = f"factory/route-plans/{ticket}.json"
    bundle_attestation_path = f"factory/attestations/{ticket}/bundle.json"
    approval_path = f"factory/attestations/{ticket}/approval.json"
    source_ticket = text_at(repo, evidence, ticket_path)
    source_bundle = text_at(repo, evidence, bundle_path)
    route_text = text_at(repo, evidence, route_path)
    bundle = json_at(repo, evidence, bundle_attestation_path, f"{ticket} bundle attestation")
    approval = json_at(repo, evidence, approval_path, f"{ticket} approval attestation")
    if source_ticket is None or source_bundle is None or route_text is None:
        raise ValidationError(f"{ticket} original ticket/route/review/bundle evidence is incomplete")
    legacy = receipt["classification"] == "reviewed-clean-history-adoption"
    if legacy:
        if bundle is not None or receipt["bundle_attestation_blob"] is not None:
            raise ValidationError(f"{ticket} legacy reviewed evidence may not invent a bundle attestation")
        review = exact(receipt["legacy_review"], LEGACY_REVIEW_KEYS, f"{ticket} legacy review evidence")
        oid(review["reviewed_sha"], f"{ticket} reviewed SHA")
        oid(review["verdict_commit"], f"{ticket} verdict commit")
        evidence_parents = run(repo, "show", "-s", "--format=%P", evidence).stdout.split()
        verdict_parents = run(repo, "show", "-s", "--format=%P", review["verdict_commit"]).stdout.split()
        evidence_paths = run(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", evidence).stdout.splitlines()
        verdict_paths = run(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", review["verdict_commit"]).stdout.splitlines()
        reviewed_ticket = text_at(repo, review["reviewed_sha"], ticket_path)
        verdict_ticket = text_at(repo, review["verdict_commit"], ticket_path)
        if (
            evidence_parents != [review["verdict_commit"]]
            or verdict_parents != [review["reviewed_sha"]]
            or evidence_paths != [bundle_path]
            or verdict_paths != [ticket_path]
            or reviewed_ticket is None
            or verdict_ticket is None
            or not verdict_ticket.startswith(reviewed_ticket)
            or not re.fullmatch(r"\nReviewer round [1-9][0-9]*: APPROVE\n", verdict_ticket[len(reviewed_ticket):])
        ):
            raise ValidationError(f"{ticket} legacy reviewer/bundle topology is invalid")
    else:
        if bundle is None or receipt["legacy_review"] is not None:
            raise ValidationError(f"{ticket} normal bundle attestation is missing")
        exact(bundle, NORMAL_BUNDLE_KEYS, f"{ticket} bundle attestation")
    expected_approval = receipt["source_state"] == "Approved"
    if (approval is not None) is not expected_approval:
        raise ValidationError(f"{ticket} original approval presence does not match source state")
    if approval is not None:
        exact(approval, NORMAL_APPROVAL_KEYS, f"{ticket} approval attestation")
    if (
        one_field(source_ticket, "State") != receipt["source_state"]
        or one_field(source_ticket, "Kit-SHA") != receipt["source_kit_sha"]
        or hash_text(repo, source_ticket) != receipt["source_ticket_blob"]
        or hash_text(repo, source_bundle) != receipt["source_bundle_blob"]
        or blob_at(repo, evidence, route_path) != receipt["route_plan_blob"]
        or hashlib.sha256(route_text.encode()).hexdigest() != receipt["route_plan_sha256"]
        or blob_at(repo, evidence, bundle_attestation_path) != receipt["bundle_attestation_blob"]
        or receipt["approval_attestation_blob"] != (
            blob_at(repo, evidence, approval_path) if approval is not None else None
        )
    ):
        raise ValidationError(f"{ticket} original evidence identities do not match")
    if legacy:
        return receipt["legacy_review"]
    if (
        bundle["schema"] != "nysa.software-factory.ticket-bundle/v1"
        or bundle["ticket"] != ticket
        or bundle["repository"] != receipt["repository"]
        or bundle["branch"] != f"ticket/{ticket}"
        or bundle["pr_number"] != original["number"]
        or bundle["bundle_path"] != bundle_path
        or bundle["route_plan_path"] != route_path
        or bundle["bundle_blob"] != receipt["source_bundle_blob"]
        or bundle["route_plan_blob"] != receipt["route_plan_blob"]
        or bundle["route_plan_sha256"] != receipt["route_plan_sha256"]
        or bundle["kit_sha"] != receipt["source_kit_sha"]
    ):
        raise ValidationError(f"{ticket} normal bundle identities do not match")
    for name in ("branch_head", "reviewed_sha"):
        oid(bundle[name], f"{ticket} bundle {name}")
    if run(repo, "merge-base", "--is-ancestor", bundle["branch_head"], evidence, check=False).returncode:
        raise ValidationError(f"{ticket} reviewer/bundle ancestry is invalid")
    if approval is not None and (
        approval["schema"] != "nysa.software-factory.ticket-approval/v1"
        or approval["ticket"] != ticket
        or approval["repository"] != receipt["repository"]
        or approval["branch"] != f"ticket/{ticket}"
        or approval["pr_number"] != original["number"]
        or approval["reviewed_sha"] != bundle["reviewed_sha"]
        or approval["bundle_blob"] != receipt["source_bundle_blob"]
        or approval["bundle_attestation_blob"] != receipt["bundle_attestation_blob"]
        or approval["kit_sha"] != receipt["source_kit_sha"]
    ):
        raise ValidationError(f"{ticket} original approval identity does not match")
    if approval is not None:
        oid(approval["parent_head"], f"{ticket} approval parent head")
        if run(repo, "merge-base", "--is-ancestor", approval["parent_head"], evidence, check=False).returncode:
            raise ValidationError(f"{ticket} approval ancestry is invalid")
    return bundle


def _migration_commit(repo, ref, basis, expected_paths):
    additions = run(
        repo, "log", "--format=%H", "--diff-filter=A", ref, "--",
        f"{MIGRATION_DIR}/authorization.json",
    ).stdout.splitlines()
    if len(additions) != 1:
        raise ValidationError("reconciliation authorization was introduced more than once")
    matches = []
    for commit in additions:
        parents = run(repo, "show", "-s", "--format=%P", commit).stdout.split()
        paths = set(run(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", commit).stdout.splitlines())
        if parents == [basis] and paths == expected_paths:
            matches.append(commit)
    if len(matches) != 1:
        raise ValidationError("reconciliation must have one atomic protected introduction")
    return matches[0]


def _validate_documents(repo, ref, authorization, receipts):
    exact(authorization, AUTH_KEYS, "reconciliation authorization")
    if (
        authorization["schema"] != AUTH_SCHEMA
        or authorization["candidate_contract"] != "1.6.0"
        or repository_from_project(repo, ref) != authorization["repository"]
    ):
        raise ValidationError("reconciliation authorization identity is invalid")
    oid(authorization["basis_kit_sha"], "reconciliation basis kit")
    oid(authorization["target_kit_sha"], "reconciliation target kit")
    if authorization["basis_kit_sha"] == authorization["target_kit_sha"]:
        raise ValidationError("reconciliation source and target kits must differ")
    cutoff = timestamp(authorization["cutoff"], "reconciliation cutoff")
    if cutoff.microsecond:
        raise ValidationError("reconciliation cutoff must use whole-second precision")
    basis = exact(authorization["protected_main_basis"], BASIS_KEYS, "reconciliation basis")
    oid(basis["commit"], "reconciliation basis commit")
    oid(basis["tree"], "reconciliation basis tree")
    if (
        run(repo, "rev-parse", f"{basis['commit']}^{{tree}}").stdout.strip() != basis["tree"]
        or run(repo, "merge-base", "--is-ancestor", basis["commit"], ref, check=False).returncode
        or text_at(repo, basis["commit"], "factory/KIT_PIN") != authorization["basis_kit_sha"] + "\n"
    ):
        raise ValidationError("reconciliation protected basis is invalid")
    identities = _check_identities(
        authorization["required_checks"], _project_required_checks(repo, basis["commit"])
    )
    approval = exact(authorization["authorization"], AUTHORIZATION_KEYS, "reconciliation authorization payload")
    authorized_at = timestamp(approval["authorized_at"], "reconciliation authorized_at")
    basis_time = timestamp(run(repo, "show", "-s", "--format=%cI", basis["commit"]).stdout.strip(), "basis time")
    if (
        approval["method"] != "manual-protected-main-merge"
        or approval["auto_merge"] is not False
        or approval["bypass"] is not False
        or not isinstance(approval["operator"], str)
        or not approval["operator"].strip()
        or not isinstance(approval["statement"], str)
        or approval["statement"].strip() != approval["statement"]
        or len(approval["statement"]) < 20
        or authorized_at < basis_time
        or authorized_at != cutoff
    ):
        raise ValidationError("reconciliation requires fresh manual operator authorization")
    entries = authorization["tickets"]
    if not isinstance(entries, list) or not entries:
        raise ValidationError("reconciliation ticket batch is empty")
    expected = {}
    for entry in entries:
        exact(entry, AUTH_TICKET_KEYS, "reconciliation ticket authorization")
        ticket = entry["ticket"]
        if not isinstance(ticket, str) or not TICKET_ID.fullmatch(ticket) or ticket in expected:
            raise ValidationError("reconciliation ticket IDs are invalid or duplicated")
        if (
            entry["classification"] not in CLASSIFICATIONS
            or entry["source_state"] not in {"Ready", "Review", "Awaiting Approval", "Approved"}
            or entry["receipt"] != f"{MIGRATION_DIR}/{ticket}.json"
            or not isinstance(entry["original_pr_number"], int)
            or not isinstance(entry["adoption_pr_number"], int)
            or not isinstance(entry["paths"], list)
            or not entry["paths"]
            or entry["paths"] != sorted(entry["paths"])
            or len(entry["paths"]) != len(set(entry["paths"]))
        ):
            raise ValidationError("reconciliation ticket authorization is invalid")
        oid(entry["source_kit_sha"], f"{ticket} source kit")
        oid(entry["evidence_head"], f"{ticket} evidence head")
        if timestamp(
            run(repo, "show", "-s", "--format=%cI", entry["evidence_head"]).stdout.strip(),
            f"{ticket} evidence time",
        ) > cutoff:
            raise ValidationError(f"{ticket} evidence head is newer than authorization")
        for path in entry["paths"]:
            _path(path, f"{ticket} adopted path")
        if (
            entry["classification"] == "reviewed-clean-history-adoption"
            and entry["source_state"] != "Ready"
        ):
            raise ValidationError("clean-history adoption requires the bounded legacy Ready shape")
        expected[ticket] = entry
    if list(expected) != sorted(expected):
        raise ValidationError("reconciliation tickets must be sorted")
    companions = authorization["companions"]
    if not isinstance(companions, list):
        raise ValidationError("reconciliation companions must be a list")
    companion_paths = []
    for companion in companions:
        exact(companion, PATH_KEYS, "reconciliation companion")
        companion_paths.append(_companion_path(companion["path"]))
        oid(companion["blob"], "reconciliation companion blob")
    if companion_paths != sorted(companion_paths) or len(companion_paths) != len(set(companion_paths)):
        raise ValidationError("reconciliation companions must be sorted and unique")
    reserved_tickets = {f"factory/tickets/{ticket}.md" for ticket in expected}
    if set(companion_paths) & reserved_tickets:
        raise ValidationError("reconciliation companion collides with a terminal ticket")
    if set(receipts) != set(expected):
        raise ValidationError("reconciliation receipt batch is partial or extra")
    expected_files = {
        f"{MIGRATION_DIR}/authorization.json",
        *(entry["receipt"] for entry in expected.values()),
    }
    actual_files = set(run(repo, "ls-tree", "-r", "--name-only", ref, "--", MIGRATION_DIR).stdout.splitlines())
    if actual_files != expected_files:
        raise ValidationError("reconciliation directory has missing or extra files")
    expected_paths = {
        "factory/KIT_PIN", *expected_files, *reserved_tickets, *companion_paths,
    }
    migration_commit = _migration_commit(repo, ref, basis["commit"], expected_paths)
    later_touches = run(
        repo, "log", "--format=%H", f"{migration_commit}..{ref}", "--",
        *sorted(expected_paths),
    ).stdout.splitlines()
    if later_touches:
        raise ValidationError("reconciliation evidence or companions changed after introduction")
    if (
        text_at(repo, migration_commit, "factory/KIT_PIN") != authorization["target_kit_sha"] + "\n"
        or timestamp(run(repo, "show", "-s", "--format=%cI", migration_commit).stdout.strip(), "migration time") < cutoff
    ):
        raise ValidationError("reconciliation migration target or time is invalid")
    for path in expected_paths - {"factory/KIT_PIN"}:
        if blob_at(repo, migration_commit, path) != blob_at(repo, ref, path):
            raise ValidationError("reconciliation evidence changed after protected merge")
    for companion in companions:
        if blob_at(repo, migration_commit, companion["path"]) != companion["blob"]:
            raise ValidationError("reconciliation companion blob does not match authorization")
    auth_blob = blob_at(repo, ref, f"{MIGRATION_DIR}/authorization.json")
    for ticket, entry in expected.items():
        immutable_source_paths = (
            f"factory/tickets/{ticket}-bundle.md",
            f"factory/route-plans/{ticket}.json",
            f"factory/attestations/{ticket}/bundle.json",
            f"factory/attestations/{ticket}/approval.json",
            f"factory/attestations/{ticket}/done.json",
            f"factory/attestations/{ticket}/refresh.json",
        )
        if any(
            blob_at(repo, ref, path) != blob_at(repo, basis["commit"], path)
            for path in immutable_source_paths
        ):
            raise ValidationError(f"{ticket} superseded factory evidence changed after the basis")
        receipt = exact(receipts[ticket], RECEIPT_KEYS, f"{ticket} reconciliation receipt")
        if (
            receipt["schema"] != RECEIPT_SCHEMA
            or receipt["ticket"] != ticket
            or receipt["repository"] != authorization["repository"]
            or receipt["classification"] != entry["classification"]
            or receipt["source_state"] != entry["source_state"]
            or receipt["source_kit_sha"] != entry["source_kit_sha"]
            or receipt["basis_kit_sha"] != authorization["basis_kit_sha"]
            or receipt["target_kit_sha"] != authorization["target_kit_sha"]
            or receipt["candidate_contract"] != authorization["candidate_contract"]
            or receipt["evidence_head"] != entry["evidence_head"]
            or receipt["authorization_blob"] != auth_blob
            or receipt["cutoff"] != authorization["cutoff"]
            or receipt["protected_main_basis"] != basis
        ):
            raise ValidationError(f"{ticket} receipt does not match authorization")
        for name in ("source_ticket_blob", "source_bundle_blob", "route_plan_blob"):
            oid(receipt[name], f"{ticket} {name}")
        if receipt["bundle_attestation_blob"] is not None:
            oid(receipt["bundle_attestation_blob"], f"{ticket} bundle attestation")
        digest(receipt["route_plan_sha256"], f"{ticket} route plan sha256")
        if receipt["approval_attestation_blob"] is not None:
            oid(receipt["approval_attestation_blob"], f"{ticket} approval attestation")
        original = _validate_pr(
            repo, basis["commit"], cutoff, ticket, receipt["original_pr"],
            entry["original_pr_number"], "original", must_merge=entry["classification"] == "merged-adoption",
        )
        adoption = _validate_pr(
            repo, basis["commit"], cutoff, ticket, receipt["adoption_pr"],
            entry["adoption_pr_number"], "adoption", must_merge=True,
        )
        if (
            entry["classification"] == "reviewed-clean-history-adoption"
            and original["number"] == adoption["number"]
        ) or (
            entry["classification"] == "merged-adoption"
            and original != adoption
        ):
            raise ValidationError(f"{ticket} PR topology does not match classification")
        bundle = _validate_source_evidence(repo, ticket, receipt, original)
        paths = receipt["paths"]
        if not isinstance(paths, list) or len(paths) != len(entry["paths"]):
            raise ValidationError(f"{ticket} adopted path evidence is incomplete")
        observed_paths = []
        for value in paths:
            exact(value, PATH_KEYS, f"{ticket} adopted path evidence")
            path = _path(value["path"], f"{ticket} adopted path")
            oid(value["blob"], f"{ticket} adopted blob")
            observed_paths.append(path)
            reviewed_source = (
                bundle["reviewed_sha"]
                if receipt["classification"] != "reviewed-clean-history-adoption"
                else receipt["legacy_review"]["reviewed_sha"]
            )
            if any(
                blob_at(repo, commit, path) != value["blob"]
                for commit in (
                    reviewed_source, receipt["evidence_head"],
                    adoption["head"], basis["commit"],
                )
            ):
                raise ValidationError(f"{ticket} adopted product/test blob changed")
        if observed_paths != entry["paths"]:
            raise ValidationError(f"{ticket} adopted path set does not match authorization")
        observed_checks = receipt["checks"]
        if not isinstance(observed_checks, list) or len(observed_checks) != len(identities):
            raise ValidationError(f"{ticket} required checks are incomplete")
        seen = set()
        for check in observed_checks:
            exact(check, CHECK_KEYS, f"{ticket} required check")
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
                raise ValidationError(f"{ticket} required check is invalid")
            seen.add(name)
        if seen != set(identities):
            raise ValidationError(f"{ticket} required checks are incomplete")
        if receipt["classification"] == "reviewed-clean-history-adoption":
            ledger_identity = {
                "reviewer_run_id": receipt["ledger"]["reviewer_run_id"],
                "narrator_run_id": receipt["ledger"]["narrator_run_id"],
            }
        else:
            ledger_identity = bundle
        _validate_ledger(repo, basis["commit"], ticket, receipt["ledger"], ledger_identity)
        terminal = text_at(repo, ref, f"factory/tickets/{ticket}.md")
        source_current = text_at(repo, basis["commit"], f"factory/tickets/{ticket}.md")
        if terminal is None or source_current is None:
            raise ValidationError(f"{ticket} terminal projection is absent")
        if terminal != terminal_projection(source_current, entry["receipt"]):
            raise ValidationError(f"{ticket} terminal ticket does not bind reconciliation")
    return {
        ticket: {
            "basis": "validated-protected-merge-reconciliation",
            "ticket": ticket,
            "text": text_at(repo, ref, f"factory/tickets/{ticket}.md"),
            "target_kit_sha": authorization["target_kit_sha"],
        }
        for ticket in expected
    }


def reconciliation_batch(repo, ref="refs/remotes/origin/main"):
    repo = Path(repo)
    authorization = json_at(repo, ref, f"{MIGRATION_DIR}/authorization.json", "reconciliation authorization")
    if authorization is None:
        return {}
    if not isinstance(authorization, dict) or not isinstance(authorization.get("tickets"), list):
        raise ValidationError("reconciliation authorization is invalid")
    receipts = {}
    for entry in authorization["tickets"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("ticket"), str):
            raise ValidationError("reconciliation ticket authorization is invalid")
        ticket = entry["ticket"]
        value = json_at(repo, ref, f"{MIGRATION_DIR}/{ticket}.json", f"{ticket} reconciliation receipt")
        if value is not None:
            receipts[ticket] = value
    return _validate_documents(repo, ref, authorization, receipts)


def validate_generated_reconciliation_batch(repo, authorization, receipts, ref):
    return _validate_documents(Path(repo), ref, authorization, receipts)
