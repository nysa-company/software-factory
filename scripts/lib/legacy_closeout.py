#!/usr/bin/env python3
"""Strict protected-main validation for normal and one-time legacy closeout."""

import csv
import hashlib
import io
import json
import re
import subprocess
from datetime import datetime
from functools import lru_cache
from pathlib import Path


class ValidationError(ValueError):
    pass


MIGRATION_DIR = "factory/migrations/contract-1.3"
AUTH_SCHEMA = "nysa.software-factory.legacy-closeout-authorization/v1"
RECEIPT_SCHEMA = "nysa.software-factory.legacy-closeout/v1"
REQUIRED_CHECK_NAMES = ("app-tests", "ci", "policy", "test-immutability")
AGGREGATE_CHECK_NAMES = ("ci", "test-immutability")
AGGREGATE_CHECK_TICKETS = frozenset(("T-013", "T-014", "T-015", "T-016"))
OUT_OF_BAND_TICKETS = frozenset(("T-019", "T-020"))
ROLES = frozenset(("planner", "spec-linter", "test-author", "builder", "reviewer", "narrator"))
OID = re.compile(r"[0-9a-f]{40}")
DIGEST = re.compile(r"[0-9a-f]{64}")
TICKET_ID = re.compile(r"T-[0-9]+")
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")

AUTH_KEYS = {
    "schema", "repository", "source_kit_sha", "target_kit_sha",
    "candidate_contract", "tickets", "required_checks", "authorization",
    "cutoff", "protected_main_basis",
}
AUTH_TICKET_KEYS = {"ticket", "classification", "source_state", "receipt"}
CHECK_IDENTITY_KEYS = {"name", "app_id", "app_slug"}
AUTHORIZATION_KEYS = {"method", "statement", "auto_merge", "bypass"}
BASIS_KEYS = {"commit", "tree"}
RECEIPT_KEYS = {
    "schema", "ticket", "repository", "classification", "source_state",
    "source_kit_sha", "target_kit_sha", "candidate_contract",
    "source_ticket_blob", "source_bundle_blob", "pr", "branch", "checks",
    "ledger", "independent_audit", "authorization_blob", "cutoff",
    "protected_main_basis", "route_plan",
}
PR_KEYS = {"number", "head", "merge_commit", "merged_at", "merged_by"}
BRANCH_KEYS = {"name", "state", "tip", "observed_at"}
CHECK_KEYS = {
    "name", "app_id", "app_slug", "status", "conclusion", "skipped",
}
LEDGER_KEYS = {"sha256", "run_ids"}
AUDIT_KEYS = {"required", "report_sha256", "combined_test_sha256"}
ROUTE_PLAN_KEYS = {"present", "sha256"}

NORMAL_BUNDLE_KEYS = {
    "schema", "ticket", "repository", "branch", "branch_head",
    "reviewed_sha", "bundle_path", "bundle_blob", "pr_number", "pr_url",
    "reviewer_run_id", "narrator_run_id", "kit_sha", "policy_hash",
    "route_plan_path", "route_plan_blob", "route_plan_sha256", "attested_at",
}
NORMAL_APPROVAL_KEYS = {
    "schema", "ticket", "repository", "branch", "parent_head",
    "reviewed_sha", "bundle_blob", "bundle_attestation_blob", "pr_number",
    "operator_version", "linear_updated_at", "observed_at", "kit_sha",
    "auto_merge_method", "attested_at",
}
NORMAL_DONE_KEYS = {
    "schema", "ticket", "repository", "pr_number", "approved_pr_head",
    "reviewed_sha", "bundle_blob", "bundle_attestation_blob",
    "approval_attestation_blob", "approval_parent_head",
    "auto_merge_method", "merge_commit", "merged_at", "required_checks",
    "successful_checks", "ledger", "kit_sha", "closeout_parent",
    "attested_at",
}
NORMAL_LEDGER_KEYS = {
    "schema", "schema_version", "status", "ticket", "row_count",
    "ticket_cost_usd", "sha256",
}
EMERGENCY_DONE_SCHEMA = "nysa.software-factory.ticket-emergency-done/v1"
EMERGENCY_PLAN_SCHEMA = "nysa.software-factory.emergency-closeout-plan/v1"
EMERGENCY_DONE_KEYS = {
    "schema", "ticket", "repository", "pr_number", "pr_head",
    "merge_commit", "merged_at", "required_checks", "successful_checks",
    "ledger", "kit_sha", "closeout_parent", "auto_merge_method", "plan",
    "approval_sha256", "attested_at",
}
EMERGENCY_PLAN_KEYS = {
    "schema", "ticket", "repository", "branch", "pr_number", "pr_head",
    "merge_commit", "merged_at", "protected_main", "required_checks",
    "successful_checks", "passport", "claim", "kit_sha", "auto_merge_method",
    "execution_basis", "issue", "operator_id", "reason", "issued_at",
    "expires_at",
}
EMERGENCY_MAIN_KEYS = {"commit", "tree", "ticket_blob", "state"}
EMERGENCY_PASSPORT_KEYS = {
    "passport_sha256", "current_state", "publication_state", "factory_sha",
    "head_sha",
}
EMERGENCY_CLAIM_KEYS = {
    "sha256", "status", "role", "blocked_reason", "receipt", "parked",
}


def run(repo, *args, input_text=None, check=True):
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        input=input_text,
        capture_output=True,
        text=True,
    )
    if check and result.returncode:
        raise ValidationError(result.stderr.strip() or "Git evidence is unavailable")
    return result


def exact(value, keys, label):
    if not isinstance(value, dict) or set(value) != keys:
        raise ValidationError(f"{label} has unknown or missing fields")
    return value


def oid(value, label):
    if not isinstance(value, str) or not OID.fullmatch(value):
        raise ValidationError(f"{label} is not a full lowercase Git object ID")
    return value


def digest(value, label, *, nullable=False):
    if nullable and value is None:
        return value
    if not isinstance(value, str) or not DIGEST.fullmatch(value):
        raise ValidationError(f"{label} is not a lowercase SHA-256 digest")
    return value


def timestamp(value, label):
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValidationError(f"{label} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValidationError(f"{label} is invalid") from error
    if parsed.tzinfo is None:
        raise ValidationError(f"{label} must include a timezone")
    return parsed


def json_at(repo, ref, path, label):
    result = run(repo, "show", f"{ref}:{path}", check=False)
    if result.returncode:
        return None
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise ValidationError(f"{label} is not valid JSON") from error
    return value


def text_at(repo, ref, path):
    result = run(repo, "show", f"{ref}:{path}", check=False)
    return None if result.returncode else result.stdout


def blob_at(repo, ref, path):
    result = run(repo, "rev-parse", f"{ref}:{path}", check=False)
    return result.stdout.strip() if result.returncode == 0 else None


def hash_text(repo, text):
    return run(repo, "hash-object", "--stdin", input_text=text).stdout.strip()


def one_field(text, name):
    values = re.findall(
        rf"(?mi)^{re.escape(name)}:\s*(.*?)\s*$", text,
    )
    if len(values) != 1:
        raise ValidationError(f"ticket must contain exactly one {name} field")
    return values[0].strip()


def normal_route_plan(repo, ref, ticket, bundle, done, ticket_text):
    historical = run(
        repo, "cat-file", "blob", bundle["route_plan_blob"], check=False,
    )
    current_text = text_at(repo, ref, bundle["route_plan_path"])
    if historical.returncode or current_text is None:
        raise ValidationError("normal route plan evidence is unavailable")
    if done["kit_sha"] == bundle["kit_sha"]:
        if current_text != historical.stdout:
            raise ValidationError("normal route plan changed after attestation")
        return historical.stdout
    try:
        before = json.loads(historical.stdout)
        current = json.loads(current_text)
    except json.JSONDecodeError as error:
        raise ValidationError("normal route migration is not valid JSON") from error
    ticket_kits = re.findall(r"(?mi)^Kit-SHA:\s*(.*?)\s*$", ticket_text)
    revisions = current.get("revisions") if isinstance(current, dict) else None
    prior = before.get("revisions") if isinstance(before, dict) else None
    if (
        not isinstance(before, dict)
        or not isinstance(current, dict)
        or set(before) != {"kit_sha", "revisions", "schema", "ticket"}
        or set(current) != set(before)
        or before.get("schema") != "ticket-model-route-journal/v2"
        or current.get("schema") != before["schema"]
        or before.get("ticket") != ticket
        or current.get("ticket") != ticket
        or before.get("kit_sha") != bundle["kit_sha"]
        or current.get("kit_sha") != done["kit_sha"]
        or ticket_kits != [done["kit_sha"]]
        or not isinstance(prior, list)
        or not isinstance(revisions, list)
        or len(revisions) <= len(prior)
        or revisions[:len(prior)] != prior
    ):
        raise ValidationError("normal route migration does not bind terminal kit")
    parent = prior[-1].get("revision_hash") if prior else None
    kit = bundle["kit_sha"]
    for index, revision in enumerate(revisions[len(prior):], len(prior)):
        body = revision.get("body") if isinstance(revision, dict) else None
        expected = hashlib.sha256(json.dumps(
            {"body": body, "parent_hash": parent, "revision": index},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode()).hexdigest()
        if (
            not isinstance(revision, dict)
            or set(revision) != {"body", "parent_hash", "revision", "revision_hash"}
            or not isinstance(body, dict)
            or body.get("kind") != "release-migration"
            or revision.get("revision") != index
            or revision.get("parent_hash") != parent
            or revision.get("revision_hash") != expected
            or body.get("old_kit_sha") != kit
            or not OID.fullmatch(body.get("new_kit_sha", ""))
        ):
            raise ValidationError("normal route migration chain is invalid")
        kit = body["new_kit_sha"]
        parent = revision["revision_hash"]
    if kit != done["kit_sha"]:
        raise ValidationError("normal route migration does not reach terminal kit")
    return historical.stdout


def repository_from_project(repo, ref):
    text = text_at(repo, ref, "factory/PROJECT.env")
    if text is None:
        raise ValidationError("protected main lacks factory/PROJECT.env")
    values = []
    for raw in text.splitlines():
        match = re.fullmatch(r"\s*(?:export\s+)?GH_REPO\s*=\s*(.*?)\s*", raw)
        if match:
            value = match.group(1)
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            values.append(value)
    if len(values) != 1 or not REPOSITORY.fullmatch(values[0]):
        raise ValidationError("factory/PROJECT.env must define one exact GH_REPO")
    return values[0]


def _emergency_terminal(repo, ticket, ref, done):
    exact(done, EMERGENCY_DONE_KEYS, "emergency Done attestation")
    plan = exact(done.get("plan"), EMERGENCY_PLAN_KEYS, "emergency closeout plan")
    protected = exact(
        plan.get("protected_main"), EMERGENCY_MAIN_KEYS,
        "emergency protected-main basis",
    )
    passport = plan.get("passport")
    if passport is not None:
        exact(passport, EMERGENCY_PASSPORT_KEYS, "emergency passport basis")
    claim = plan.get("claim")
    if claim is not None:
        exact(claim, EMERGENCY_CLAIM_KEYS, "emergency claim basis")
    ledger = exact(done.get("ledger"), NORMAL_LEDGER_KEYS, "emergency Done ledger")
    repository = repository_from_project(repo, ref)
    parent = oid(done.get("closeout_parent"), "emergency closeout parent")
    pr_head = oid(done.get("pr_head"), "emergency PR head")
    merge = oid(done.get("merge_commit"), "emergency merge commit")
    kit_sha = oid(done.get("kit_sha"), "emergency kit SHA")
    for name in ("commit", "tree", "ticket_blob"):
        oid(protected.get(name), f"emergency protected-main {name}")
    if passport is not None:
        digest(passport.get("passport_sha256"), "emergency passport passport_sha256")
        for name in ("factory_sha", "head_sha"):
            oid(passport.get(name), f"emergency passport {name}")
    if claim is not None:
        digest(claim.get("sha256"), "emergency claim sha256")
        digest(claim.get("receipt"), "emergency claim receipt")
    if (
        done["schema"] != EMERGENCY_DONE_SCHEMA
        or plan["schema"] != EMERGENCY_PLAN_SCHEMA
        or done["ticket"] != ticket
        or plan["ticket"] != ticket
        or done["repository"] != repository
        or plan["repository"] != repository
        or plan["branch"] != f"ticket/{ticket}"
        or done["pr_number"] != plan["pr_number"]
        or not isinstance(done["pr_number"], int)
        or done["pr_number"] <= 0
        or pr_head != plan["pr_head"]
        or merge != plan["merge_commit"]
        or done["merged_at"] != plan["merged_at"]
        or done["required_checks"] != plan["required_checks"]
        or done["successful_checks"] != plan["successful_checks"]
        or done["required_checks"] != done["successful_checks"]
        or not done["required_checks"]
        or len(done["required_checks"]) != len(set(done["required_checks"]))
        or kit_sha != plan["kit_sha"]
        or done["auto_merge_method"] != plan["auto_merge_method"]
        or done["auto_merge_method"] not in {"squash", "merge", "rebase"}
        or parent != protected["commit"]
        or protected["state"] not in {
            "Backlog", "Ready", "Planning", "Building", "Review", "Awaiting Approval",
            "Approved", "Blocked-Escalated",
        }
        or plan["execution_basis"] not in {
            "authenticated-passport", "operator-built-no-runtime",
            "protected-merge-no-runtime",
        }
        or (plan["execution_basis"] == "authenticated-passport") != (passport is not None)
        or (plan["execution_basis"] == "authenticated-passport") != (claim is not None)
        or (plan["execution_basis"] == "protected-merge-no-runtime")
        != (protected["state"] == "Backlog")
        or (
            passport is not None
            and (
                passport["current_state"] != protected["state"]
                or passport["publication_state"] not in {
                    "none", "validating", "ready", "merge-pending", "merged", "repair",
                }
            )
        )
        or (
            claim is not None
            and (
                claim["status"] != "blocked"
                or claim["parked"] is not True
                or not re.fullmatch(r"[a-z][a-z-]*", claim["role"])
                or not isinstance(claim["blocked_reason"], str)
                or not claim["blocked_reason"]
            )
        )
        or not re.fullmatch(
            r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/issues/[1-9][0-9]*",
            plan["issue"],
        )
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", plan["operator_id"])
        or plan["operator_id"] == "auto"
        or not isinstance(plan["reason"], str)
        or not 20 <= len(plan["reason"]) <= 500
        or ledger["schema"] != "nysa.software-factory.ledger-projection/v1"
        or ledger["schema_version"] != 1
        or ledger["status"] != "ok"
        or ledger["ticket"] != ticket
        or not isinstance(ledger["row_count"], int)
        or ledger["row_count"] < 0
        or not isinstance(ledger["ticket_cost_usd"], (int, float))
    ):
        raise ValidationError("emergency closeout identities do not match")
    expected_approval = hashlib.sha256(json.dumps(
        plan, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    digest(done["approval_sha256"], "emergency approval sha256")
    digest(ledger["sha256"], "emergency ledger sha256")
    if done["approval_sha256"] != expected_approval:
        raise ValidationError("emergency closeout approval does not bind its exact plan")
    merged_at = timestamp(done["merged_at"], "emergency merged_at")
    issued_at = timestamp(plan["issued_at"], "emergency issued_at")
    expires_at = timestamp(plan["expires_at"], "emergency expires_at")
    attested_at = timestamp(done["attested_at"], "emergency attested_at")
    if not merged_at <= issued_at <= attested_at <= expires_at:
        raise ValidationError("emergency closeout timestamps are not ordered")
    if run(repo, "rev-parse", f"{parent}^{{tree}}").stdout.strip() != protected["tree"]:
        raise ValidationError("emergency protected-main tree does not match")
    ticket_path = f"factory/tickets/{ticket}.md"
    source_text = text_at(repo, parent, ticket_path)
    if (
        source_text is None
        or blob_at(repo, parent, ticket_path) != protected["ticket_blob"]
        or one_field(source_text, "State") != protected["state"]
    ):
        raise ValidationError("emergency source ticket does not match protected main")
    if plan["execution_basis"] == "operator-built-no-runtime" and one_field(
        source_text, "Assignee",
    ) != "operator (built outside the software factory)":
        raise ValidationError("passportless emergency source is not operator-built")
    ticket_text = text_at(repo, ref, ticket_path)
    if ticket_text is None or one_field(ticket_text, "State").lower() != "done":
        raise ValidationError("emergency terminal ticket is not Done")
    done_path = f"factory/attestations/{ticket}/done.json"
    additions = run(
        repo, "log", "--format=%H", "--diff-filter=A", f"{parent}..{ref}", "--",
        done_path,
    ).stdout.splitlines()
    current_done_blob = blob_at(repo, ref, done_path)
    closeouts = []
    for addition in additions:
        addition = oid(addition, "emergency closeout candidate")
        topology = run(repo, "rev-list", "--parents", "-n", "1", addition).stdout.split()
        if topology == [addition, parent] and blob_at(repo, addition, done_path) == current_done_blob:
            closeouts.append(addition)
    if len(closeouts) != 1:
        raise ValidationError("emergency Done attestation addition is ambiguous")
    closeout = closeouts[0]
    paths = set(run(
        repo, "diff-tree", "--no-commit-id", "--name-only", "-r", closeout,
    ).stdout.splitlines())
    required_paths = {ticket_path, done_path}
    if not required_paths.issubset(paths) or not paths.issubset(
        required_paths | {"factory/ledger.csv"}
    ):
        raise ValidationError("emergency closeout commit changes unauthorized paths")
    ledger_text = text_at(repo, closeout, "factory/ledger.csv")
    current_ledger = text_at(repo, ref, "factory/ledger.csv")
    if (
        ledger_text is None
        or not ledger_text.endswith("\n")
        or hashlib.sha256(ledger_text.encode()).hexdigest() != ledger["sha256"]
        or current_ledger is None
        or not current_ledger.startswith(ledger_text)
    ):
        raise ValidationError("emergency closeout ledger does not match")
    return {
        "basis": "attested-emergency-closeout", "ticket": ticket,
        "text": ticket_text,
    }


def _normal_terminal(repo, ticket, ref):
    root = f"factory/attestations/{ticket}"
    done_path = f"{root}/done.json"
    bundle = json_at(repo, ref, f"{root}/bundle.json", "bundle attestation")
    approval = json_at(repo, ref, f"{root}/approval.json", "approval attestation")
    done = json_at(repo, ref, done_path, "Done attestation")
    if isinstance(done, dict) and done.get("schema") == EMERGENCY_DONE_SCHEMA:
        return _emergency_terminal(repo, ticket, ref, done)
    present = tuple(value is not None for value in (bundle, approval, done))
    if not any(present):
        return None
    if not all(present):
        raise ValidationError("protected main has a partial normal attestation chain")
    exact(bundle, NORMAL_BUNDLE_KEYS, "bundle attestation")
    exact(approval, NORMAL_APPROVAL_KEYS, "approval attestation")
    exact(done, NORMAL_DONE_KEYS, "Done attestation")
    exact(done.get("ledger"), NORMAL_LEDGER_KEYS, "Done ledger projection")
    repository = repository_from_project(repo, ref)
    branch = f"ticket/{ticket}"
    if (
        bundle["schema"] != "nysa.software-factory.ticket-bundle/v1"
        or approval["schema"] != "nysa.software-factory.ticket-approval/v1"
        or done["schema"] != "nysa.software-factory.ticket-done/v1"
        or any(value["ticket"] != ticket for value in (bundle, approval, done))
        or any(value["repository"] != repository for value in (bundle, approval, done))
        or bundle["branch"] != branch
        or approval["branch"] != branch
        or bundle["bundle_path"] != f"factory/tickets/{ticket}-bundle.md"
        or bundle["route_plan_path"] != f"factory/route-plans/{ticket}.json"
        or approval["pr_number"] != bundle["pr_number"]
        or done["pr_number"] != bundle["pr_number"]
        or approval["reviewed_sha"] != bundle["reviewed_sha"]
        or done["reviewed_sha"] != bundle["reviewed_sha"]
        or approval["bundle_blob"] != bundle["bundle_blob"]
        or done["bundle_blob"] != bundle["bundle_blob"]
        or approval["bundle_attestation_blob"] != done["bundle_attestation_blob"]
        or approval["parent_head"] != done["approval_parent_head"]
        or approval["kit_sha"] != bundle["kit_sha"]
        or approval["auto_merge_method"] != done["auto_merge_method"]
        or done["auto_merge_method"] not in {"squash", "merge", "rebase"}
        or done["required_checks"] != done["successful_checks"]
        or not done["required_checks"]
        or len(done["required_checks"]) != len(set(done["required_checks"]))
        or done["ledger"]["schema"] != "nysa.software-factory.ledger-projection/v1"
        or done["ledger"]["schema_version"] != 1
        or done["ledger"]["status"] != "ok"
        or done["ledger"]["ticket"] != ticket
        or not isinstance(done["ledger"]["row_count"], int)
        or done["ledger"]["row_count"] < 0
        or not isinstance(done["ledger"]["ticket_cost_usd"], (int, float))
    ):
        raise ValidationError("normal attestation chain identities do not match")
    for name in (
        "branch_head", "reviewed_sha", "bundle_blob", "route_plan_blob",
    ):
        oid(bundle[name], f"bundle {name}")
    for name in ("parent_head", "bundle_blob", "bundle_attestation_blob", "reviewed_sha"):
        oid(approval[name], f"approval {name}")
    for name in (
        "approved_pr_head", "reviewed_sha", "bundle_blob",
        "bundle_attestation_blob", "approval_attestation_blob",
        "approval_parent_head", "merge_commit", "kit_sha", "closeout_parent",
    ):
        oid(done[name], f"Done {name}")
    for name in ("policy_hash", "route_plan_sha256"):
        digest(bundle[name], f"bundle {name}")
    digest(approval["operator_version"], "approval operator_version")
    digest(done["ledger"]["sha256"], "Done ledger sha256")
    timestamp(bundle["attested_at"], "bundle attested_at")
    bundle_time = timestamp(bundle["attested_at"], "bundle attested_at")
    observed = timestamp(approval["observed_at"], "approval observed_at")
    updated = timestamp(approval["linear_updated_at"], "approval linear_updated_at")
    merged = timestamp(done["merged_at"], "Done merged_at")
    attested = timestamp(done["attested_at"], "Done attested_at")
    if (
        approval["attested_at"] != approval["observed_at"]
        or observed <= bundle_time
        or updated <= bundle_time
        or attested < merged
    ):
        raise ValidationError("normal attestation timestamps are not ordered")
    if (
        blob_at(repo, ref, f"{root}/bundle.json") != done["bundle_attestation_blob"]
        or blob_at(repo, ref, f"{root}/approval.json") != done["approval_attestation_blob"]
    ):
        raise ValidationError("normal attestation blobs do not match protected main")
    ticket_text = text_at(repo, ref, f"factory/tickets/{ticket}.md")
    if ticket_text is None or one_field(ticket_text, "State").lower() != "done":
        raise ValidationError("normal terminal ticket is not Done")
    if one_field(ticket_text, "Operator-Approval").lower() != "linear":
        raise ValidationError("normal terminal ticket lacks Linear approval")
    route_plan_text = normal_route_plan(
        repo, ref, ticket, bundle, done, ticket_text,
    )
    bundle_text = text_at(repo, ref, bundle["bundle_path"])
    additions = run(
        repo, "log", "--format=%H", "--diff-filter=A",
        f"{done['closeout_parent']}..{ref}", "--", done_path,
    ).stdout.splitlines()
    closeouts = []
    current_done_blob = blob_at(repo, ref, done_path)
    for addition in additions:
        addition = oid(addition, "normal closeout candidate")
        topology = run(repo, "rev-list", "--parents", "-n", "1", addition).stdout.split()
        if (
            topology == [addition, done["closeout_parent"]]
            and blob_at(repo, addition, done_path) == current_done_blob
        ):
            closeouts.append(addition)
    if len(closeouts) != 1:
        raise ValidationError("normal Done attestation addition is ambiguous")
    closeout = closeouts[0]
    ledger_text = text_at(repo, closeout, "factory/ledger.csv")
    current_ledger_text = text_at(repo, ref, "factory/ledger.csv")
    if (
        route_plan_text is None
        or hashlib.sha256(route_plan_text.encode()).hexdigest()
        != bundle["route_plan_sha256"]
        or bundle_text is None
        or hash_text(repo, bundle_text) != bundle["bundle_blob"]
        or ledger_text is None
        or not ledger_text.endswith("\n")
        or hashlib.sha256(ledger_text.encode()).hexdigest()
        != done["ledger"]["sha256"]
        or current_ledger_text is None
        or not current_ledger_text.startswith(ledger_text)
    ):
        raise ValidationError("normal protected-main blobs or digests do not match")
    return {"basis": "attested-done", "ticket": ticket, "text": ticket_text}


def _validate_check_identities(required):
    if not isinstance(required, list) or len(required) != len(REQUIRED_CHECK_NAMES):
        raise ValidationError("legacy authorization required_checks is incomplete")
    names = []
    for item in required:
        exact(item, CHECK_IDENTITY_KEYS, "required check identity")
        if (
            not isinstance(item["name"], str)
            or not isinstance(item["app_id"], int)
            or item["app_id"] <= 0
            or not isinstance(item["app_slug"], str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,99}", item["app_slug"])
        ):
            raise ValidationError("required check identity is invalid")
        names.append(item["name"])
    if tuple(sorted(names)) != REQUIRED_CHECK_NAMES or len(names) != len(set(names)):
        raise ValidationError("legacy authorization check names are not exact")
    return {item["name"]: item for item in required}


def required_check_names(classification, ticket):
    if classification == "legacy-reviewed-aggregate":
        if ticket not in AGGREGATE_CHECK_TICKETS:
            raise ValidationError(
                "aggregate-check migration is limited to T-013 through T-016"
            )
        return AGGREGATE_CHECK_NAMES
    return REQUIRED_CHECK_NAMES


def _terminal_companion_paths(repo, ref, authorization):
    from terminal_backfill import (
        AUTHORIZED_TICKETS as TERMINAL_TICKETS,
        MIGRATION_DIR as TERMINAL_MIGRATION_DIR,
    )

    companion = json_at(
        repo, ref, f"{TERMINAL_MIGRATION_DIR}/authorization.json",
        "terminal companion authorization",
    )
    if companion is None:
        return set()
    if (
        not isinstance(companion, dict)
        or companion.get("repository") != authorization["repository"]
        or companion.get("basis_kit_sha") != authorization["source_kit_sha"]
        or companion.get("target_kit_sha") != authorization["target_kit_sha"]
        or companion.get("candidate_contract") != authorization["candidate_contract"]
        or companion.get("cutoff") != authorization["cutoff"]
        or companion.get("protected_main_basis")
        != authorization["protected_main_basis"]
        or not isinstance(companion.get("tickets"), list)
    ):
        raise ValidationError("terminal companion does not match legacy closeout")
    paths = {f"{TERMINAL_MIGRATION_DIR}/authorization.json"}
    tickets = []
    for entry in companion["tickets"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("ticket"), str):
            raise ValidationError("terminal companion ticket is invalid")
        ticket = entry["ticket"]
        if entry.get("receipt") != f"{TERMINAL_MIGRATION_DIR}/{ticket}.json":
            raise ValidationError("terminal companion receipt path is invalid")
        tickets.append(ticket)
        paths.add(entry["receipt"])
    if tuple(sorted(tickets)) != TERMINAL_TICKETS:
        raise ValidationError("terminal companion ticket batch is not exact")
    return paths


def _validate_ledger(repo, basis, ticket, ledger):
    exact(ledger, LEDGER_KEYS, "legacy ledger evidence")
    digest(ledger["sha256"], "legacy ledger sha256")
    if (
        not isinstance(ledger["run_ids"], list)
        or not ledger["run_ids"]
        or len(ledger["run_ids"]) != len(set(ledger["run_ids"]))
        or any(not isinstance(item, str) or not item for item in ledger["run_ids"])
    ):
        raise ValidationError("legacy ledger run IDs are missing or ambiguous")
    text = text_at(repo, basis, "factory/ledger.csv")
    if text is None or hashlib.sha256(text.encode()).hexdigest() != ledger["sha256"]:
        raise ValidationError("legacy ledger digest does not match protected basis")
    rows = list(csv.DictReader(io.StringIO(text)))
    ticket_rows = [row for row in rows if row.get("ticket") == ticket]
    actual_ids = [row.get("run_id") for row in ticket_rows]
    if (
        any(not value for value in actual_ids)
        or len(actual_ids) != len(set(actual_ids))
        or ledger["run_ids"] != actual_ids
    ):
        raise ValidationError("legacy ledger run IDs do not bind all ticket rows")
    successful_roles = {
        row.get("role") for row in ticket_rows if row.get("exit_status") == "0"
    }
    if not {"reviewer", "narrator"} <= successful_roles or not successful_roles <= ROLES:
        raise ValidationError("legacy ledger lacks successful Reviewer/Narrator evidence")


def _validate_legacy_documents(repo, ref, authorization, receipts):
    exact(authorization, AUTH_KEYS, "legacy authorization")
    if (
        authorization["schema"] != AUTH_SCHEMA
        or not REPOSITORY.fullmatch(authorization.get("repository", ""))
        or authorization["candidate_contract"] != "1.3.0"
        or authorization["source_kit_sha"] == authorization["target_kit_sha"]
    ):
        raise ValidationError("legacy authorization identity is invalid")
    oid(authorization["source_kit_sha"], "source kit SHA")
    oid(authorization["target_kit_sha"], "target kit SHA")
    if repository_from_project(repo, ref) != authorization["repository"]:
        raise ValidationError("legacy authorization repository does not match protected main")
    cutoff = timestamp(authorization["cutoff"], "legacy cutoff")
    if cutoff.microsecond:
        raise ValidationError("legacy cutoff must use whole-second precision")
    basis = exact(
        authorization["protected_main_basis"], BASIS_KEYS,
        "protected-main basis",
    )
    oid(basis["commit"], "protected-main basis commit")
    oid(basis["tree"], "protected-main basis tree")
    if run(repo, "rev-parse", f"{basis['commit']}^{{tree}}").stdout.strip() != basis["tree"]:
        raise ValidationError("protected-main basis tree does not match its commit")
    if run(repo, "merge-base", "--is-ancestor", basis["commit"], ref, check=False).returncode:
        raise ValidationError("protected-main basis is not an ancestor of protected main")
    basis_time = timestamp(
        run(repo, "show", "-s", "--format=%cI", basis["commit"]).stdout.strip(),
        "protected-main basis commit time",
    )
    if basis_time > cutoff:
        raise ValidationError("protected-main basis is newer than the migration cutoff")
    checks_by_name = _validate_check_identities(authorization["required_checks"])
    approval = exact(
        authorization["authorization"], AUTHORIZATION_KEYS,
        "legacy authorization payload",
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
        raise ValidationError("legacy authorization must require a manual protected merge")
    entries = authorization["tickets"]
    if not isinstance(entries, list) or not entries:
        raise ValidationError("legacy authorization ticket batch is empty")
    auth_blob = blob_at(repo, ref, f"{MIGRATION_DIR}/authorization.json")
    if not auth_blob:
        raise ValidationError("legacy authorization is not on protected main")
    expected_receipts = {}
    for entry in entries:
        exact(entry, AUTH_TICKET_KEYS, "legacy authorization ticket")
        ticket = entry["ticket"]
        if not isinstance(ticket, str) or not TICKET_ID.fullmatch(ticket):
            raise ValidationError("legacy authorization ticket ID is invalid")
        if ticket in expected_receipts:
            raise ValidationError("legacy authorization contains a duplicate ticket")
        expected_path = f"{MIGRATION_DIR}/{ticket}.json"
        if entry["receipt"] != expected_path:
            raise ValidationError("legacy authorization receipt path is invalid")
        expected_receipts[ticket] = entry
    actual_files = run(
        repo, "ls-tree", "-r", "--name-only", ref, "--", MIGRATION_DIR,
    ).stdout.splitlines()
    expected_files = [
        f"{MIGRATION_DIR}/authorization.json",
        *(expected_receipts[ticket]["receipt"] for ticket in sorted(expected_receipts)),
    ]
    if sorted(actual_files) != sorted(expected_files):
        raise ValidationError("legacy migration directory has missing or extra files")
    if set(receipts) != set(expected_receipts):
        raise ValidationError("legacy receipt batch is partial or contains extra tickets")
    expected_paths = {
        "factory/KIT_PIN",
        *expected_files,
        *(f"factory/tickets/{ticket}.md" for ticket in expected_receipts),
    }
    expected_paths.update(_terminal_companion_paths(repo, ref, authorization))
    matches = []
    for commit in run(
        repo, "log", "--format=%H", "--diff-filter=A", ref, "--",
        f"{MIGRATION_DIR}/authorization.json",
    ).stdout.splitlines():
        parents = run(repo, "show", "-s", "--format=%P", commit).stdout.split()
        paths = set(run(
            repo, "diff-tree", "--no-commit-id", "--name-only", "-r", commit,
        ).stdout.splitlines())
        if parents == [basis["commit"]] and paths == expected_paths:
            matches.append(commit)
    if len(matches) != 1:
        raise ValidationError(
            "legacy migration must have one atomic protected introduction from its basis"
        )
    migration_commit = matches[0]
    if text_at(repo, migration_commit, "factory/KIT_PIN") != authorization["target_kit_sha"] + "\n":
        raise ValidationError("legacy migration commit is not pinned to the target kit")
    if timestamp(
        run(repo, "show", "-s", "--format=%cI", migration_commit).stdout.strip(),
        "legacy migration commit time",
    ) < cutoff:
        raise ValidationError("legacy migration commit predates its evidence cutoff")
    for path in expected_paths - {"factory/KIT_PIN"}:
        if blob_at(repo, migration_commit, path) != blob_at(repo, ref, path):
            raise ValidationError("legacy migration evidence changed after protected merge")
    for ticket, entry in expected_receipts.items():
        receipt = exact(receipts[ticket], RECEIPT_KEYS, f"{ticket} legacy receipt")
        classification = entry["classification"]
        source_state = entry["source_state"]
        if (
            receipt["schema"] != RECEIPT_SCHEMA
            or receipt["ticket"] != ticket
            or receipt["repository"] != authorization["repository"]
            or receipt["classification"] != classification
            or receipt["source_state"] != source_state
            or receipt["source_kit_sha"] != authorization["source_kit_sha"]
            or receipt["target_kit_sha"] != authorization["target_kit_sha"]
            or receipt["candidate_contract"] != authorization["candidate_contract"]
            or receipt["authorization_blob"] != auth_blob
            or receipt["cutoff"] != authorization["cutoff"]
            or receipt["protected_main_basis"] != basis
        ):
            raise ValidationError(f"{ticket} legacy receipt does not match authorization")
        if classification == "legacy-reviewed":
            if source_state != "Review":
                raise ValidationError("legacy-reviewed requires exact source State Review")
        elif classification == "legacy-reviewed-aggregate":
            if source_state != "Review":
                raise ValidationError(
                    "legacy-reviewed-aggregate requires exact source State Review"
                )
            required_check_names(classification, ticket)
        elif classification == "out-of-band-merged":
            if source_state != "Planning" or ticket not in OUT_OF_BAND_TICKETS:
                raise ValidationError("out-of-band migration is limited to T-019/T-020 Planning")
        else:
            raise ValidationError("legacy classification is invalid")
        source_ticket = text_at(repo, basis["commit"], f"factory/tickets/{ticket}.md")
        source_bundle = text_at(
            repo, basis["commit"], f"factory/tickets/{ticket}-bundle.md",
        )
        if source_ticket is None or source_bundle is None:
            raise ValidationError(f"{ticket} source evidence is absent from the basis")
        if (
            hash_text(repo, source_ticket) != receipt["source_ticket_blob"]
            or hash_text(repo, source_bundle) != receipt["source_bundle_blob"]
            or one_field(source_ticket, "State") != source_state
            or one_field(source_ticket, "Kit-SHA") != authorization["source_kit_sha"]
        ):
            raise ValidationError(f"{ticket} source ticket or bundle evidence changed")
        oid(receipt["source_ticket_blob"], "source ticket blob")
        oid(receipt["source_bundle_blob"], "source bundle blob")
        route_plan = exact(receipt["route_plan"], ROUTE_PLAN_KEYS, "legacy route plan")
        if route_plan != {"present": False, "sha256": None} or text_at(
            repo, basis["commit"], f"factory/route-plans/{ticket}.json",
        ) is not None:
            raise ValidationError("legacy ticket may not satisfy ordinary route-plan evidence")
        pr = exact(receipt["pr"], PR_KEYS, "legacy PR evidence")
        if (
            not isinstance(pr["number"], int)
            or pr["number"] <= 0
            or not isinstance(pr["merged_by"], str)
            or not pr["merged_by"]
        ):
            raise ValidationError("legacy PR identity is invalid")
        oid(pr["head"], "legacy PR head")
        oid(pr["merge_commit"], "legacy PR merge commit")
        if run(
            repo, "merge-base", "--is-ancestor", pr["merge_commit"], basis["commit"],
            check=False,
        ).returncode:
            raise ValidationError("legacy PR merge is not in the protected basis")
        if timestamp(pr["merged_at"], "legacy PR merged_at") > cutoff:
            raise ValidationError("legacy PR merged after the cutoff")
        if (
            blob_at(repo, pr["head"], f"factory/tickets/{ticket}.md")
            != receipt["source_ticket_blob"]
            or blob_at(repo, pr["head"], f"factory/tickets/{ticket}-bundle.md")
            != receipt["source_bundle_blob"]
        ):
            raise ValidationError("legacy PR head does not bind source ticket and bundle")
        branch = exact(receipt["branch"], BRANCH_KEYS, "legacy branch observation")
        if (
            branch["name"] != f"ticket/{ticket}"
            or branch["state"] not in {"deleted", "exact"}
            or branch["observed_at"] != authorization["cutoff"]
            or (
                branch["state"] == "deleted" and branch["tip"] is not None
            )
            or (
                branch["state"] == "exact" and branch["tip"] != pr["head"]
            )
        ):
            raise ValidationError("legacy branch observation is invalid")
        if branch["tip"] is not None:
            oid(branch["tip"], "legacy branch tip")
        ticket_checks = {
            name: checks_by_name[name]
            for name in required_check_names(classification, ticket)
        }
        observed_checks = receipt["checks"]
        if not isinstance(observed_checks, list) or len(observed_checks) != len(ticket_checks):
            raise ValidationError("legacy check evidence is incomplete")
        seen = set()
        for check in observed_checks:
            exact(check, CHECK_KEYS, "legacy check")
            name = check["name"]
            if (
                name in seen
                or name not in ticket_checks
                or check["app_id"] != ticket_checks[name]["app_id"]
                or check["app_slug"] != ticket_checks[name]["app_slug"]
                or check["status"] != "completed"
                or check["conclusion"] != "success"
                or check["skipped"] is not False
            ):
                raise ValidationError("legacy check is duplicate, skipped, unsuccessful, or from the wrong app")
            seen.add(name)
        if set(seen) != set(ticket_checks):
            raise ValidationError("legacy check evidence is incomplete")
        _validate_ledger(repo, basis["commit"], ticket, receipt["ledger"])
        audit = exact(receipt["independent_audit"], AUDIT_KEYS, "independent audit")
        required_audit = classification in {
            "legacy-reviewed-aggregate", "out-of-band-merged",
        }
        if audit["required"] is not required_audit:
            raise ValidationError("independent audit requirement does not match classification")
        digest(audit["report_sha256"], "independent audit report", nullable=not required_audit)
        digest(
            audit["combined_test_sha256"], "combined test evidence",
            nullable=not required_audit,
        )
        if not required_audit and (
            audit["report_sha256"] is not None
            or audit["combined_test_sha256"] is not None
        ):
            raise ValidationError("legacy-reviewed receipt may not invent independent audit evidence")
        terminal = text_at(repo, ref, f"factory/tickets/{ticket}.md")
        if terminal is None:
            raise ValidationError(f"{ticket} terminal ticket is absent")
        if (
            one_field(terminal, "State") != "Done"
            or one_field(terminal, "Operator-Approval") != "Migration"
            or one_field(terminal, "Migration-Receipt") != entry["receipt"]
            or one_field(terminal, "Kit-SHA") != authorization["source_kit_sha"]
        ):
            raise ValidationError(f"{ticket} terminal ticket does not bind the legacy receipt")
    return {
        ticket: {
            "basis": "validated-legacy-closeout",
            "ticket": ticket,
            "text": text_at(repo, ref, f"factory/tickets/{ticket}.md"),
            "target_kit_sha": authorization["target_kit_sha"],
        }
        for ticket in expected_receipts
    }


def legacy_batch(repo, ref="refs/remotes/origin/main"):
    repo = Path(repo)
    authorization = json_at(
        repo, ref, f"{MIGRATION_DIR}/authorization.json", "legacy authorization",
    )
    if authorization is None:
        return {}
    if not isinstance(authorization, dict):
        raise ValidationError("legacy authorization must be an object")
    entries = authorization.get("tickets")
    if not isinstance(entries, list):
        raise ValidationError("legacy authorization tickets must be a list")
    receipts = {}
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("ticket"), str):
            raise ValidationError("legacy authorization ticket is invalid")
        ticket = entry["ticket"]
        value = json_at(
            repo, ref, f"{MIGRATION_DIR}/{ticket}.json", f"{ticket} legacy receipt",
        )
        if value is not None:
            receipts[ticket] = value
    return _validate_legacy_documents(repo, ref, authorization, receipts)


def validate_generated_legacy_batch(repo, authorization, receipts, ref):
    """Validate generated documents against an index/tree containing them."""
    return _validate_legacy_documents(Path(repo), ref, authorization, receipts)


@lru_cache(maxsize=64)
def _legacy_batch_at(repo, commit):
    return legacy_batch(Path(repo), commit)


@lru_cache(maxsize=64)
def _terminal_backfill_batch_at(repo, commit):
    from terminal_backfill import terminal_backfill_batch

    return terminal_backfill_batch(Path(repo), commit)


@lru_cache(maxsize=64)
def _protected_merge_reconciliation_batch_at(repo, commit):
    from protected_merge_reconciliation import reconciliation_batch

    return reconciliation_batch(Path(repo), commit)


def protected_terminal(repo, ticket, ref="refs/remotes/origin/main"):
    if not isinstance(ticket, str) or not TICKET_ID.fullmatch(ticket):
        raise ValidationError("invalid ticket identifier")
    repo = Path(repo).resolve(strict=True)
    commit = run(repo, "rev-parse", "--verify", f"{ref}^{{commit}}").stdout.strip()
    oid(commit, "protected-main commit")
    reconciliation = _protected_merge_reconciliation_batch_at(str(repo), commit)
    if ticket in reconciliation:
        normal_root = f"factory/attestations/{ticket}"
        normal_done = json_at(repo, commit, f"{normal_root}/done.json", "Done attestation")
        normal = _normal_terminal(repo, ticket, commit) if normal_done is not None else None
    else:
        normal = _normal_terminal(repo, ticket, commit)
    legacy = _legacy_batch_at(str(repo), commit)
    backfill = _terminal_backfill_batch_at(str(repo), commit)
    evidence_count = sum((
        normal is not None,
        ticket in legacy,
        ticket in backfill,
        ticket in reconciliation,
    ))
    if evidence_count > 1:
        raise ValidationError("ticket has conflicting protected-main terminal evidence")
    if normal:
        return normal
    if ticket in legacy:
        return dict(legacy[ticket])
    if ticket in backfill:
        return dict(backfill[ticket])
    if ticket in reconciliation:
        return dict(reconciliation[ticket])
    raise ValidationError("protected main lacks valid terminal evidence")


def protected_dependency(repo, ticket, ref="refs/remotes/origin/main"):
    """Accept terminal truth or one explicit dependency-only fulfillment."""
    try:
        return protected_terminal(repo, ticket, ref)
    except ValidationError as error:
        if str(error) != "protected main lacks valid terminal evidence":
            raise
    from dependency_fulfillment import dependency_fulfillment

    return dependency_fulfillment(repo, ticket, ref)


@lru_cache(maxsize=64)
def _ancestor_commit_for_tree(repo, commit, tree):
    repo = Path(repo)
    if run(repo, "cat-file", "-t", tree, check=False).stdout.strip() != "tree":
        return None
    for line in run(repo, "log", "--format=%H%x09%T", commit).stdout.splitlines():
        candidate, candidate_tree = line.split("\t", 1)
        if candidate_tree == tree:
            return candidate
    return None


def certified_legacy_terminal(repo, ticket, ref, certified_tree):
    """Preserve an unchanged legacy Done blob from the prior certified tree."""
    if not isinstance(ticket, str) or not TICKET_ID.fullmatch(ticket):
        raise ValidationError("invalid ticket identifier")
    if not isinstance(certified_tree, str) or not OID.fullmatch(certified_tree):
        return None
    repo = Path(repo).resolve(strict=True)
    commit = run(repo, "rev-parse", "--verify", f"{ref}^{{commit}}").stdout.strip()
    baseline = _ancestor_commit_for_tree(str(repo), commit, certified_tree)
    if baseline is None:
        return None
    path = f"factory/tickets/{ticket}.md"
    current_text = text_at(repo, commit, path)
    if (
        current_text is None
        or one_field(current_text, "State") != "Done"
        or blob_at(repo, commit, path) != blob_at(repo, baseline, path)
    ):
        return None
    evidence_paths = (
        f"factory/attestations/{ticket}/done.json",
        f"factory/migrations/contract-1.3/{ticket}.json",
        f"factory/migrations/contract-1.3-terminal-backfill/{ticket}.json",
        f"factory/migrations/protected-merge-reconciliation/{ticket}.json",
    )
    if any(blob_at(repo, baseline, item) is not None for item in evidence_paths):
        return None
    return {
        "basis": "certified-legacy-done",
        "ticket": ticket,
        "baseline_commit": baseline,
        "ticket_blob": blob_at(repo, baseline, path),
    }
