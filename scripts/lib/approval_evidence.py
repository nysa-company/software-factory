#!/usr/bin/env python3
"""Fail-closed validation for Factory-generated approval continuations."""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import subprocess


class ApprovalEvidenceError(ValueError):
    pass


BUNDLE_BASE_KEYS = {
    "schema", "ticket", "repository", "branch", "branch_head",
    "reviewed_sha", "bundle_path", "bundle_blob", "pr_number", "pr_url",
    "reviewer_run_id", "narrator_run_id", "kit_sha", "policy_hash",
    "route_plan_path", "route_plan_blob", "route_plan_sha256", "attested_at",
}
APPROVAL_KEYS = {
    "schema", "ticket", "repository", "branch", "parent_head",
    "reviewed_sha", "bundle_blob", "bundle_attestation_blob", "pr_number",
    "operator_version", "linear_updated_at", "observed_at", "kit_sha",
    "auto_merge_method", "attested_at",
}
OID = re.compile(r"[0-9a-f]{40}")
DIGEST = re.compile(r"[0-9a-f]{64}")


def _git(root: Path, *arguments: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *arguments],
        text=True, capture_output=True, check=False,
    )
    if check and result.returncode:
        raise ApprovalEvidenceError(
            result.stderr.strip() or result.stdout.strip() or "Git evidence is unavailable"
        )
    return result.stdout.strip()


def _git_raw(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *arguments],
        text=True, capture_output=True, check=False,
    )
    if result.returncode:
        raise ApprovalEvidenceError(
            result.stderr.strip() or result.stdout.strip() or "Git evidence is unavailable"
        )
    return result.stdout


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ApprovalEvidenceError("approval evidence contains a duplicate key")
        value[key] = item
    return value


def _decode_json(text: str, label: str):
    try:
        value = json.loads(
            text, object_pairs_hook=_unique_object,
        )
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ApprovalEvidenceError(f"{label} is not valid JSON") from error
    if not isinstance(value, dict):
        raise ApprovalEvidenceError(f"{label} must be a JSON object")
    return value


def read_json(path: Path, label: str):
    if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
        raise ApprovalEvidenceError(f"{label} is missing or unsafe")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise ApprovalEvidenceError(f"{label} is not valid JSON") from error
    return _decode_json(text, label)


def _read_json_at(workdir: Path, commit: str, relative: str, label: str):
    return _decode_json(_git_raw(workdir, "show", f"{commit}:{relative}"), label)


def _timestamp(value, label):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as error:
        raise ApprovalEvidenceError(f"invalid {label} timestamp") from error
    if parsed.tzinfo is None:
        raise ApprovalEvidenceError(f"{label} timestamp lacks a timezone")
    return parsed


def _blob_at(workdir: Path, commit: str, relative: str) -> str:
    value = _git(workdir, "rev-parse", f"{commit}:{relative}")
    if not OID.fullmatch(value):
        raise ApprovalEvidenceError("approval evidence blob identity is invalid")
    return value


def _mode_at(workdir: Path, commit: str, relative: str) -> str:
    line = _git(workdir, "ls-tree", commit, "--", relative)
    parts = line.split()
    if len(parts) != 4 or parts[1] != "blob" or parts[3] != relative:
        raise ApprovalEvidenceError("approval evidence Git object is invalid")
    return parts[0]


def validate_bundle_attestation(value, ticket, repo, branch, kit_sha, workdir):
    schema = value.get("schema")
    if schema == "nysa.software-factory.ticket-bundle/v1":
        expected_keys = BUNDLE_BASE_KEYS
        legacy_digest_valid = "legacy_planner_manifest_sha256" not in value
    elif schema == "nysa.software-factory.ticket-bundle/v2":
        expected_keys = BUNDLE_BASE_KEYS | {"legacy_planner_manifest_sha256"}
        legacy_digest_valid = bool(DIGEST.fullmatch(
            value.get("legacy_planner_manifest_sha256", ""),
        ))
    else:
        expected_keys = set()
        legacy_digest_valid = False
    route_path = f"factory/route-plans/{ticket}.json"
    bundle_path = f"factory/tickets/{ticket}-bundle.md"
    if (
        set(value) != expected_keys
        or not legacy_digest_valid
        or value.get("ticket") != ticket
        or value.get("repository") != repo
        or value.get("branch") != branch
        or value.get("kit_sha") != kit_sha
        or value.get("route_plan_path") != route_path
        or value.get("bundle_path") != bundle_path
        or not OID.fullmatch(value.get("route_plan_blob", ""))
        or not OID.fullmatch(value.get("branch_head", ""))
        or not OID.fullmatch(value.get("reviewed_sha", ""))
        or not OID.fullmatch(value.get("bundle_blob", ""))
        or not DIGEST.fullmatch(value.get("route_plan_sha256", ""))
        or not DIGEST.fullmatch(value.get("policy_hash", ""))
        or isinstance(value.get("pr_number"), bool)
        or not isinstance(value.get("pr_number"), int)
        or value["pr_number"] <= 0
        or not isinstance(value.get("pr_url"), str)
        or not value["pr_url"]
        or not isinstance(value.get("reviewer_run_id"), str)
        or not value["reviewer_run_id"]
        or not isinstance(value.get("narrator_run_id"), str)
        or not value["narrator_run_id"]
        or _blob_at(workdir, value["branch_head"], route_path)
        != value["route_plan_blob"]
        or _blob_at(workdir, value["branch_head"], bundle_path)
        != value["bundle_blob"]
        or hashlib.sha256(
            _git_raw(workdir, "show", f"{value['branch_head']}:{route_path}").encode()
        ).hexdigest() != value["route_plan_sha256"]
    ):
        raise ApprovalEvidenceError("bundle attestation identity or evidence is invalid")
    _timestamp(value.get("attested_at"), "bundle attestation")
    return value


def validate_bundle_commit(workdir, ticket, value, bundle_commit):
    receipt_path = f"factory/attestations/{ticket}/bundle.json"
    bundle_path = f"factory/tickets/{ticket}-bundle.md"
    ticket_path = f"factory/tickets/{ticket}.md"
    parent = _git(workdir, "rev-parse", f"{bundle_commit}^")
    statuses = _git(
        workdir, "diff-tree", "--no-commit-id", "--name-status", "-r",
        bundle_commit,
    ).splitlines()
    if (
        parent != value["branch_head"]
        or set(statuses) != {f"M\t{ticket_path}", f"A\t{receipt_path}"}
        or _mode_at(workdir, bundle_commit, receipt_path) != "100644"
        or _blob_at(workdir, bundle_commit, bundle_path) != value["bundle_blob"]
    ):
        raise ApprovalEvidenceError(
            "bundle attestation commit or reviewed branch evidence is invalid"
        )


def _replace_field(text: str, name: str, value: str) -> str:
    pattern = re.compile(rf"^{re.escape(name)}:\s*.*$", re.I | re.M)
    matches = pattern.findall(text)
    if len(matches) != 1:
        raise ApprovalEvidenceError(f"approval parent must contain exactly one {name}")
    return pattern.sub(f"{name}: {value}", text, count=1)


def _expected_approved_ticket(parent_text: str) -> str:
    states = re.findall(r"^State:\s*(.*?)\s*$", parent_text, re.I | re.M)
    if len(states) != 1 or states[0].casefold() != "awaiting approval":
        raise ApprovalEvidenceError("approval parent is not Awaiting Approval")
    text = _replace_field(parent_text, "State", "Approved")
    approvals = re.findall(r"^Operator-Approval:\s*(.*?)\s*$", text, re.I | re.M)
    if len(approvals) > 1:
        raise ApprovalEvidenceError("approval parent has ambiguous operator approval")
    if approvals:
        text = _replace_field(text, "Operator-Approval", "Linear")
    else:
        text = re.sub(
            r"^(State:.*)$", r"\1\nOperator-Approval: Linear", text,
            count=1, flags=re.M,
        )
    return re.sub(
        r"^- \[ \] Operator approved\s*$", "- [x] Operator approved",
        text, count=1, flags=re.I | re.M,
    )


def validate_approval_attestation(
    value, bundle_att, ticket, repo, branch, kit_sha, method, workdir, head,
):
    workdir = Path(workdir)
    bundle_path = f"factory/attestations/{ticket}/bundle.json"
    approval_path = f"factory/attestations/{ticket}/approval.json"
    ticket_path = f"factory/tickets/{ticket}.md"
    parent = _git(workdir, "rev-parse", f"{head}^")
    validate_bundle_commit(workdir, ticket, bundle_att, parent)
    statuses = _git(
        workdir, "diff-tree", "--no-commit-id", "--name-status", "-r", head,
    ).splitlines()
    parent_ticket = _git_raw(workdir, "show", f"{parent}:{ticket_path}")
    current_ticket = _git_raw(workdir, "show", f"{head}:{ticket_path}")
    if (
        set(value) != APPROVAL_KEYS
        or value.get("schema") != "nysa.software-factory.ticket-approval/v1"
        or value.get("ticket") != ticket
        or value.get("repository") != repo
        or value.get("branch") != branch
        or value.get("pr_number") != bundle_att["pr_number"]
        or value.get("reviewed_sha") != bundle_att["reviewed_sha"]
        or value.get("bundle_blob") != bundle_att["bundle_blob"]
        or value.get("kit_sha") != kit_sha
        or value.get("auto_merge_method") != method
        or value.get("attested_at") != value.get("observed_at")
        or not DIGEST.fullmatch(value.get("operator_version", ""))
        or _timestamp(value.get("observed_at"), "approval observation")
        <= _timestamp(bundle_att.get("attested_at"), "bundle attestation")
        or _timestamp(value.get("linear_updated_at"), "Linear approval update")
        <= _timestamp(bundle_att.get("attested_at"), "bundle attestation")
        or value.get("parent_head") != parent
        or value.get("bundle_attestation_blob")
        != _blob_at(workdir, parent, bundle_path)
        or _blob_at(workdir, head, bundle_path)
        != value.get("bundle_attestation_blob")
        or set(statuses) != {f"M\t{ticket_path}", f"A\t{approval_path}"}
        or _mode_at(workdir, head, approval_path) != "100644"
        or current_ticket != _expected_approved_ticket(parent_ticket)
    ):
        raise ApprovalEvidenceError(
            "existing approval attestation or approval commit is invalid"
        )
    return value


def trusted_approval_continuation_paths(
    workdir: Path,
    ticket: str,
    repo: str,
    branch: str,
    kit_sha: str,
    method: str,
    reviewed: str,
    head: str,
    changed: set[str],
) -> set[str]:
    relative = f"factory/attestations/{ticket}/approval.json"
    if relative not in changed:
        return set()
    bundle_relative = f"factory/attestations/{ticket}/bundle.json"
    bundle_document = f"factory/tickets/{ticket}-bundle.md"
    ticket_relative = f"factory/tickets/{ticket}.md"
    additions = _git(
        workdir, "log", "--format=%H", "--diff-filter=A",
        f"{reviewed}..{head}", "--", relative,
    ).splitlines()
    if len(additions) != 1 or not OID.fullmatch(additions[0]):
        raise ApprovalEvidenceError(
            "approval continuation addition lineage is invalid"
        )
    approval_commit = additions[0]
    bundle = _read_json_at(
        workdir, approval_commit, bundle_relative, "bundle attestation",
    )
    approval = _read_json_at(
        workdir, approval_commit, relative, "approval attestation",
    )
    approval_kit_sha = approval.get("kit_sha", "")
    if not OID.fullmatch(approval_kit_sha):
        raise ApprovalEvidenceError("approval continuation Kit-SHA is invalid")
    validate_bundle_attestation(
        bundle, ticket, repo, branch, approval_kit_sha, workdir,
    )
    if bundle.get("reviewed_sha") != reviewed:
        raise ApprovalEvidenceError("approval continuation reviewed SHA is invalid")
    validate_approval_attestation(
        approval, bundle, ticket, repo, branch, approval_kit_sha, method,
        workdir, approval_commit,
    )
    approved_ticket = _git_raw(
        workdir, "show", f"{approval_commit}:{ticket_relative}",
    )
    current_ticket = _git_raw(workdir, "show", f"{head}:{ticket_relative}")
    expected_ticket = _replace_field(approved_ticket, "Kit-SHA", kit_sha)
    route_relative = f"factory/route-plans/{ticket}.json"
    if (
        _mode_at(workdir, head, relative) != "100644"
        or _mode_at(workdir, head, bundle_relative) != "100644"
        or _blob_at(workdir, head, relative)
        != _blob_at(workdir, approval_commit, relative)
        or _blob_at(workdir, head, bundle_relative)
        != _blob_at(workdir, approval_commit, bundle_relative)
        or _blob_at(workdir, head, bundle_document) != bundle.get("bundle_blob")
        or current_ticket != expected_ticket
        or (kit_sha != approval_kit_sha and route_relative not in changed)
    ):
        raise ApprovalEvidenceError(
            "approval continuation changed after its attested commit"
        )
    return {relative}
