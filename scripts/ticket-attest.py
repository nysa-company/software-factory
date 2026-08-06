#!/usr/bin/env python3
"""Evidence-bound ticket approval, protected auto-merge, and closeout."""

import argparse
import base64
import csv
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import time
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
from refresh_semantics import (  # noqa: E402
    ClassificationError,
    preserved_control_paths,
    retained_control_paths,
)
from narrator_evidence import trusted_narrator_evidence_paths  # noqa: E402
from approval_evidence import (  # noqa: E402
    ApprovalEvidenceError,
    validate_approval_continuation as validate_shared_approval_continuation,
    validate_approval_attestation as validate_shared_approval_attestation,
    validate_bundle_attestation as validate_shared_bundle_attestation,
    validate_bundle_commit as validate_shared_bundle_commit,
)
from legacy_closeout import (  # noqa: E402
    EMERGENCY_DONE_SCHEMA,
    EMERGENCY_PLAN_SCHEMA,
    ValidationError,
    protected_dependency,
    protected_terminal,
)
from runtime_paths import canonical_factory_file  # noqa: E402


class Refusal(ValueError):
    pass


ROLES = ("planner", "spec-linter", "test-author", "builder", "reviewer", "narrator")
EMERGENCY_REQUEST_SCHEMA = "nysa.software-factory.emergency-closeout-request/v1"
EMERGENCY_REQUEST_KEYS = {
    "schema", "issue", "operator_id", "reason", "issued_at", "expires_at",
}
EMERGENCY_PAUSE_KEYS = {
    "blocking_issue", "branch", "budget_sha256", "created_at_epoch",
    "current_stage", "current_state", "factory_sha", "head_sha",
    "passport_factory_sha", "passport_sha256", "pause_sha256",
    "resume_state", "run_snapshot_sha256", "schema", "status", "ticket",
    "worktree",
}


def run(argv, *, cwd=None, input_text=None, check=True):
    result = subprocess.run(
        argv, cwd=cwd, input=input_text, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if check and result.returncode:
        raise Refusal(result.stderr.strip() or result.stdout.strip() or f"{argv[0]} failed")
    return result


def git(root, *args, check=True):
    command = ["git", "-C", str(root), *args]
    result = run(command, check=False)
    if result.returncode and args[0] == "ls-remote":
        result = run(command, check=False)
    if check and result.returncode:
        raise Refusal(f"Git operation failed: {args[0]}")
    return result


def gh(*args):
    result = run(["gh", *args], check=False)
    if result.returncode:
        if args[:2] == ("pr", "merge"):
            raise Refusal("GitHub did not accept protected auto-merge")
        if args[:2] == ("pr", "create"):
            raise Refusal("GitHub did not create the protected closeout PR")
        raise Refusal("GitHub query failed")
    return result


def now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def timestamp(value, label):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        raise Refusal(f"invalid {label} timestamp")


def parse_project(path):
    allowed = {
        "GH_REPO", "DONE_REQUIRED_CHECKS", "TICKET_BRANCH_PREFIX",
        "AUTO_MERGE_METHOD",
    }
    values = {}
    if not path.is_file() or path.is_symlink():
        raise Refusal("factory/PROJECT.env is missing or unsafe")
    assignment = re.compile(r"(?:export[ \t]+)?([A-Z][A-Z0-9_]*)[ \t]*=[ \t]*(.*)")
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = assignment.fullmatch(line)
        if not match or match.group(1) not in allowed:
            continue
        key, encoded = match.groups()
        if key in values:
            raise Refusal(f"duplicate product field {key}")
        if encoded[:1] in ("'", '"'):
            if len(encoded) < 2 or encoded[-1] != encoded[0] or encoded[0] in encoded[1:-1]:
                raise Refusal(f"unsafe product field {key}")
            encoded = encoded[1:-1]
        if any(fragment in encoded for fragment in ("`", "$(", "${", "\\", "\n", "\r")):
            raise Refusal(f"unsafe product field {key}")
        values[key] = encoded
    repo = values.get("GH_REPO", "")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise Refusal("GH_REPO must be an exact owner/repository slug")
    prefix = values.get("TICKET_BRANCH_PREFIX", "ticket/")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*/", prefix) or any(
        part in prefix for part in ("..", "//", "@{", "\\", "~", "^", ":")
    ):
        raise Refusal("TICKET_BRANCH_PREFIX is invalid")
    checks = values.get("DONE_REQUIRED_CHECKS", "")
    names = checks.split(",") if checks else []
    if not names or any(
        name != name.strip()
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._:/()=-]{0,199}", name)
        for name in names
    ) or len(names) != len(set(names)):
        raise Refusal("DONE_REQUIRED_CHECKS must be a nonempty unique comma-separated exact-name list")
    method = values.get("AUTO_MERGE_METHOD", "")
    if method not in {"squash", "merge", "rebase"}:
        raise Refusal("AUTO_MERGE_METHOD must be exactly squash, merge, or rebase")
    return repo, prefix, names, method


def meta(path):
    values = {}
    for line in path.read_text().splitlines():
        if "=" not in line:
            raise Refusal(f"malformed run manifest {path.name}")
        key, value = line.split("=", 1)
        if key in values:
            raise Refusal(f"duplicate manifest field {key}")
        values[key] = value
    return values


def successful_runs(product, workdir, ticket):
    manifests = []
    runs = product / "factory" / "runs"
    if not runs.is_dir() or runs.is_symlink():
        raise Refusal("authoritative run manifest directory is missing")
    for path in sorted(runs.glob("*.meta")):
        if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
            raise Refusal("run manifests must be regular single-link files")
        value = meta(path)
        legacy_success = (
            value.get("contract_version") == "1.2.0"
            and value.get("selection_reason") == "primary_ready"
            and value.get("accounting_state") == "completed"
            and value.get("exit_status") == "0"
        )
        if (
            value.get("ticket") == ticket
            and (
                legacy_success
                or (
                    value.get("phase") == "completed"
                    and value.get("accounting_schema") == "1"
                    and value.get("accounting_state") in {
                        "completed", "abandoned_conservative",
                    }
                    and value.get("go_issued") == "1"
                    and value.get("task_submitted") == "1"
                    and value.get("exit_status") == "0"
                    and value.get("role_exit") == "ok"
                    and (
                        value.get("accounting_state") != "abandoned_conservative"
                        or value.get("cost_basis") == "conservative_reservation"
                    )
                )
            )
        ):
            value["_manifest_name"] = path.name
            value["_manifest_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
            manifests.append(value)
    configured_ledger = os.environ.get("FACTORY_LEDGER", "")
    ledger = (
        Path(configured_ledger)
        if configured_ledger
        else canonical_factory_file(workdir, "runtime-ledger.csv")
    )
    if not ledger.is_file() or ledger.is_symlink():
        raise Refusal("effective runtime ledger is missing")
    with ledger.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    successful_ids = {}
    for index, row in enumerate(rows):
        if row.get("ticket") != ticket or row.get("exit_status") != "0":
            continue
        run_id = row.get("run_id")
        if not run_id or run_id in successful_ids:
            raise Refusal("successful ledger run IDs are missing or ambiguous")
        successful_ids[run_id] = (index, row)
    for value in manifests:
        if value.get("run_id") not in successful_ids:
            raise Refusal(f"successful manifest {value.get('run_id')} is absent from ledger")
        value["_ledger_index"], value["_ledger_row"] = successful_ids[value["run_id"]]
        ledger_row = value["_ledger_row"]
        ledger_fields = (
            "ticket", "role", "adapter", "exit_status", "run_id", "provider_family",
            "model_id", "selection_reason", "cost_basis", "adapter_version",
        )
        if any(value.get(field) != ledger_row.get(field) for field in ledger_fields):
            raise Refusal(f"successful manifest {value.get('run_id')} does not match ledger")
    return manifests


def route_revision_hash(index, parent, body):
    return hashlib.sha256(json.dumps(
        {"body": body, "parent_hash": parent, "revision": index},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def content_hash(value):
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def logical_resolution(value):
    result = {
        key: item for key, item in value.items()
        if key not in ("catalog_hash", "profile_hash", "policy_hash")
    }
    result["selections"] = {
        role: {
            key: item for key, item in selection.items()
            if key not in ("adapter_version", "reported_identity")
        }
        for role, selection in value["selections"].items()
    }
    return result


def route_plan_evidence(workdir, product, ticket, kit_sha, manifests):
    path = workdir / "factory" / "route-plans" / f"{ticket}.json"
    if not path.is_file() or path.is_symlink() or path.stat().st_nlink != 1:
        raise Refusal("committed ticket route plan is missing or unsafe")
    raw = path.read_bytes()
    try:
        plan = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise Refusal("ticket route plan is malformed")
    if plan.get("ticket") != ticket or plan.get("kit_sha") != kit_sha:
        raise Refusal("ticket route plan does not match the attested ticket and kit")
    revisions = {}
    failed_digests = set()
    if plan.get("schema") == "ticket-model-route-plan/v1":
        if set(plan) != {"schema", "ticket", "kit_sha", "created_at", "resolution"}:
            raise Refusal("legacy ticket route plan is malformed")
        legacy = plan
        resolution = plan.get("resolution")
        legacy_raw = raw
    elif plan.get("schema") == "ticket-model-route-journal/v2":
        if set(plan) != {"schema", "ticket", "kit_sha", "revisions"}:
            raise Refusal("ticket route journal is malformed")
        parent = None
        resolution = None
        current_kit = None
        for index, revision_value in enumerate(plan.get("revisions", [])):
            body = revision_value.get("body") if isinstance(revision_value, dict) else None
            expected = route_revision_hash(index, parent, body)
            if (
                not isinstance(revision_value, dict)
                or not isinstance(body, dict)
                or set(revision_value) != {"revision", "parent_hash", "body", "revision_hash"}
                or revision_value.get("revision") != index
                or revision_value.get("parent_hash") != parent
                or revision_value.get("revision_hash") != expected
            ):
                raise Refusal("ticket route journal hash chain is invalid")
            if index == 0 and body.get("kind") == "migration":
                migration_keys = {
                    "kind", "migrated_at", "legacy_plan_b64", "legacy_plan_sha256",
                    "pin_commit", "old_kit_sha", "new_kit_sha", "policy_hash",
                    "historical_selections",
                }
                try:
                    legacy_raw = base64.b64decode(body["legacy_plan_b64"], validate=True)
                    legacy = json.loads(legacy_raw)
                except (KeyError, ValueError, UnicodeError, json.JSONDecodeError):
                    raise Refusal("ticket route migration provenance is malformed")
                if (
                    set(body) != migration_keys
                    or not isinstance(legacy, dict)
                    or hashlib.sha256(legacy_raw).hexdigest() != body.get("legacy_plan_sha256")
                    or legacy.get("ticket") != ticket
                    or legacy.get("kit_sha") != body.get("old_kit_sha")
                    or legacy.get("resolution", {}).get("policy_hash") != body.get("policy_hash")
                    or legacy.get("resolution", {}).get("selections")
                    != body.get("historical_selections")
                    or not valid_oid(body.get("pin_commit", ""))
                    or not valid_oid(body.get("old_kit_sha", ""))
                    or not valid_oid(body.get("new_kit_sha", ""))
                ):
                    raise Refusal("ticket route migration provenance does not match")
                timestamp(body.get("migrated_at"), "route migration")
                resolution = legacy["resolution"]
                current_kit = body["new_kit_sha"]
            elif index > 0 and body.get("kind") == "fallback":
                if body.get("prior_resolution") != resolution:
                    raise Refusal("fallback revision does not extend the prior resolution")
                resolution = body.get("new_resolution")
                failed_digests.add(body.get("failed_manifest_digest"))
            elif index > 0 and body.get("kind") == "release-migration":
                inline_keys = {
                    "kind", "migrated_at", "pin_commit", "old_kit_sha",
                    "new_kit_sha", "prior_resolution",
                }
                compact_keys = inline_keys - {"prior_resolution"} | {
                    "prior_resolution_sha256"
                }
                if "new_resolution" in body:
                    inline_keys.add("new_resolution")
                    compact_keys.add("new_resolution")
                prior_matches = (
                    body.get("prior_resolution") == resolution
                    if "prior_resolution" in body
                    else body.get("prior_resolution_sha256") == content_hash(resolution)
                )
                if (
                    set(body) not in (inline_keys, compact_keys)
                    or not prior_matches
                    or body.get("old_kit_sha") != current_kit
                    or not valid_oid(body.get("pin_commit", ""))
                    or not valid_oid(body.get("new_kit_sha", ""))
                    or body.get("new_kit_sha") == current_kit
                ):
                    raise Refusal("release migration does not extend the prior route evidence")
                timestamp(body.get("migrated_at"), "release migration")
                if "new_resolution" in body:
                    try:
                        unchanged = logical_resolution(body["new_resolution"]) == logical_resolution(
                            resolution
                        )
                    except (KeyError, TypeError):
                        unchanged = False
                    if not unchanged:
                        raise Refusal("release migration changed logical routing")
                    resolution = body["new_resolution"]
                current_kit = body["new_kit_sha"]
            else:
                raise Refusal("ticket route journal revision kind is invalid")
            prefix = dict(plan)
            prefix["kit_sha"] = current_kit
            prefix["revisions"] = plan["revisions"][:index + 1]
            prefix_raw = (
                json.dumps(prefix, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                + "\n"
            ).encode("utf-8")
            revisions[index] = (
                revision_value["revision_hash"], resolution, prefix_raw, current_kit,
            )
            parent = revision_value["revision_hash"]
        if resolution is None or current_kit != plan.get("kit_sha"):
            raise Refusal("ticket route journal has no active resolution")
    else:
        raise Refusal("unsupported ticket route evidence schema")
    selections = resolution.get("selections") if isinstance(resolution, dict) else None
    if not isinstance(selections, dict) or set(selections) != set(ROLES):
        raise Refusal("ticket route evidence lacks a complete six-role policy")
    digest = hashlib.sha256(raw).hexdigest()
    manifest_fields = {
        "adapter": "adapter",
        "provider_family": "provider_family",
        "model_id": "selection_id",
        "effort": "effort",
        "adapter_version": "adapter_version",
        "route_id": "route_id",
        "gateway_id": "gateway_id",
        "inference_provider_id": "inference_provider_id",
        "account_route_id": "account_route_id",
        "transport": "transport",
    }
    legacy_planners = []
    pinned_planners = []
    for manifest in manifests:
        role = manifest.get("role")
        reason = manifest.get("selection_reason")
        if reason == "primary_ready":
            legacy_planners.append(manifest)
            continue
        if reason == "pinned_route_plan":
            selected_resolution = legacy["resolution"]
            expected_digest = hashlib.sha256(legacy_raw).hexdigest()
            expected_kit = legacy["kit_sha"]
        elif reason == "route_journal":
            try:
                number = int(manifest.get("route_revision", ""))
                revision_hash, selected_resolution, revision_raw, expected_kit = revisions[number]
            except (ValueError, KeyError):
                raise Refusal("successful run references an unknown route revision")
            if manifest.get("route_revision_hash") != revision_hash:
                raise Refusal("successful run route revision hash does not match")
            expected_digest = hashlib.sha256(revision_raw).hexdigest()
        else:
            raise Refusal("successful run has an unsupported route selection reason")
        selection = selected_resolution["selections"].get(role)
        if not isinstance(selection, dict):
            raise Refusal("successful run references a role absent from the route plan")
        if any(
            manifest.get(field) != selection.get(selected)
            for field, selected in manifest_fields.items()
        ):
            raise Refusal(f"successful {role} run does not match its pinned route")
        if (
            manifest.get("policy_hash") != selected_resolution["policy_hash"]
            or manifest.get("route_plan_sha256") != expected_digest
            or manifest.get("kit_sha") != expected_kit
        ):
            raise Refusal(f"successful {role} run lacks pinned route provenance")
        if role == "planner":
            pinned_planners.append(manifest)
    legacy_digest = None
    if legacy_planners:
        if len(legacy_planners) != 1:
            raise Refusal("exactly one legacy pre-pin Planner manifest is supported")
        manifest = legacy_planners[0]
        legacy_fields = {
            "run_id", "phase", "accounting_schema", "accounting_state",
            "reserved_usd", "go_issued", "started_at", "terminal_at",
            "prompt_version", "turns", "effective_cost", "exit_status",
            "cost_basis", "ticket", "role", "adapter", "provider_family",
            "model_id", "effort", "selection_reason", "adapter_version",
            "primary_probe", "kit_sha", "kit_tree", "product_tree",
            "ticket_kit_sha", "contract_version", "physical_kit_path",
            "kit_provenance_mode", "pid", "pgid", "process_start",
            "role_exit", "role_branch_before", "role_head_before",
            "role_remote_before", "updated_at", "_manifest_name",
            "_manifest_sha256", "_ledger_index", "_ledger_row",
        }
        current_branch = git(
            workdir, "symbolic-ref", "--quiet", "--short", "HEAD",
        ).stdout.strip()
        old_kit = manifest.get("kit_sha", "")
        head = manifest.get("role_head_before", "")
        ledger_row = manifest.get("_ledger_row", {})
        ledger_fields = (
            "ticket", "role", "adapter", "exit_status", "run_id",
            "provider_family", "model_id", "selection_reason", "cost_basis",
            "adapter_version",
        )
        if manifest.get("role") != "planner":
            raise Refusal("legacy pre-pin run must be a Planner")
        if (
            set(manifest) != legacy_fields
            or manifest.get("phase") != "completed"
            or manifest.get("accounting_schema") != "1"
            or manifest.get("accounting_state") != "completed"
            or manifest.get("go_issued") != "1"
            or manifest.get("exit_status") != "0"
            or manifest.get("selection_reason") != "primary_ready"
            or manifest.get("primary_probe") != "READY:local_contract_ready"
            or manifest.get("contract_version") != "1.2.0"
            or manifest.get("kit_provenance_mode") != "sealed"
            or manifest.get("role_exit") != "ok"
            or old_kit == kit_sha
            or not valid_oid(old_kit)
            or manifest.get("ticket_kit_sha") != old_kit
            or not valid_oid(manifest.get("kit_tree"))
            or not valid_oid(manifest.get("product_tree"))
            or not Path(manifest.get("physical_kit_path", "")).is_absolute()
            or Path(manifest.get("physical_kit_path", "")).name != old_kit
            or not re.fullmatch(r"\d+-\d+", manifest.get("run_id", ""))
            or manifest.get("_manifest_name") != f"{manifest.get('run_id')}.meta"
            or not re.fullmatch(r"[1-9]\d*", manifest.get("pid", ""))
            or manifest.get("pgid") != manifest.get("pid")
            or manifest.get("role_branch_before") != current_branch
            or not valid_oid(head)
            or manifest.get("role_remote_before") != head
            or any(manifest.get(field) != ledger_row.get(field) for field in ledger_fields)
        ):
            raise Refusal("legacy pre-pin Planner manifest provenance is invalid")
        started = timestamp(manifest.get("started_at"), "legacy Planner start")
        terminal = timestamp(manifest.get("terminal_at"), "legacy Planner completion")
        plan_created = timestamp(legacy.get("created_at"), "legacy route plan creation")
        if (
            terminal <= started
            or terminal >= plan_created
            or manifest.get("updated_at") != manifest.get("terminal_at")
        ):
            raise Refusal("legacy pre-pin Planner timestamps are invalid")
        selection = resolution["selections"]["planner"]
        legacy_route_fields = {
            "adapter": "adapter",
            "provider_family": "provider_family",
            "model_id": "selection_id",
            "effort": "effort",
            "adapter_version": "adapter_version",
        }
        if any(
            manifest.get(field) != selection.get(selected)
            for field, selected in legacy_route_fields.items()
        ):
            raise Refusal("legacy pre-pin Planner does not match the pinned Planner route")
        later = [
            item for item in pinned_planners
            if item["_ledger_index"] > manifest["_ledger_index"]
            and timestamp(item.get("terminal_at"), "pinned Planner completion") > terminal
        ]
        if not later:
            raise Refusal("legacy pre-pin Planner lacks a later pinned Planner supersession")
        legacy_digest = manifest["_manifest_sha256"]
    if failed_digests:
        actual_failed = set()
        runs = product / "factory" / "runs"
        for manifest_path in runs.glob("*.meta"):
            value = meta(manifest_path)
            if (
                value.get("ticket") == ticket
                and value.get("go_issued") == "1"
                and value.get("exit_status") not in ("", "0")
            ):
                actual_failed.add(hashlib.sha256(manifest_path.read_bytes()).hexdigest())
        if not failed_digests.issubset(actual_failed):
            raise Refusal("route journal references an unattested failed attempt")
    evidence = {
        "policy_hash": resolution["policy_hash"],
        "route_plan_blob": git(workdir, "hash-object", str(path)).stdout.strip(),
        "route_plan_path": str(path.relative_to(workdir)),
        "route_plan_sha256": digest,
    }
    if legacy_digest:
        evidence["legacy_planner_manifest_sha256"] = legacy_digest
    return evidence


def review_evidence(text, manifests, workdir):
    reviewers = sorted(
        (item for item in manifests if item.get("role") == "reviewer"),
        key=lambda item: item["_ledger_index"],
    )
    narrators = sorted(
        (item for item in manifests if item.get("role") == "narrator"),
        key=lambda item: item["_ledger_index"],
    )
    if not reviewers or not narrators:
        raise Refusal("successful reviewer and narrator evidence is required")
    voids = set()
    for match in re.finditer(
        r"^\s*OPERATOR NOTE:\s*reviewer run\s+(\d+)\s+void[^A-Za-z0-9]*duplicate\s*$",
        text, re.I | re.M,
    ):
        ordinal = int(match.group(1))
        if 1 <= ordinal <= len(reviewers):
            voids.add(ordinal)
    nonvoid = [item for ordinal, item in enumerate(reviewers, 1) if ordinal not in voids]
    verdicts = re.findall(
        r"^\s*reviewer round\s+(\d+):\s*(APPROVE|REQUEST CHANGES(?:\s+—\s+.*)?)\s*$",
        text, re.I | re.M,
    )
    if not nonvoid or len(verdicts) != len(nonvoid):
        raise Refusal("reviewer run and semantic verdict evidence is incomplete")
    if not verdicts[-1][1].upper().startswith("APPROVE"):
        raise Refusal("latest non-void Reviewer verdict is not APPROVE")
    reviewer = nonvoid[-1]
    narrator = narrators[-1]
    reviewed = reviewer.get("role_head_before", "")
    narrator_head = narrator.get("role_head_before", "")
    if not re.fullmatch(r"[0-9a-f]{40}", reviewed):
        raise Refusal("latest reviewer manifest lacks a reviewed SHA")
    if not re.fullmatch(r"[0-9a-f]{40}", narrator_head):
        raise Refusal("latest narrator manifest lacks branch lineage")
    if timestamp(narrator.get("terminal_at"), "Narrator completion") <= timestamp(
        reviewer.get("terminal_at"), "Reviewer completion"
    ) or narrator["_ledger_index"] <= reviewer["_ledger_index"]:
        raise Refusal("Narrator evidence is not after the latest Reviewer")
    if git(
        workdir, "merge-base", "--is-ancestor", reviewed, narrator_head, check=False,
    ).returncode:
        raise Refusal("Narrator did not run on the reviewed branch lineage")
    return reviewer, narrator, reviewed


def reviewer_sequences(text):
    verdicts = [
        f"{int(round_number)}:{' '.join(verdict.split()).upper()}"
        for round_number, verdict in re.findall(
            r"^\s*reviewer round\s+(\d+):\s*(APPROVE|REQUEST CHANGES(?:\s+—\s+.*)?)\s*$",
            text, re.I | re.M,
        )
    ]
    voids = [
        int(value) for value in re.findall(
            r"^\s*OPERATOR NOTE:\s*reviewer run\s+(\d+)\s+void[^A-Za-z0-9]*duplicate\s*$",
            text, re.I | re.M,
        )
    ]
    return verdicts, voids


REFRESH_RECEIPT_KEYS = {
    "schema", "ticket", "generation", "old_head", "base_head", "merge_head",
    "prior_reviewer_runs", "prior_approve_verdicts",
    "prior_request_changes_verdicts", "prior_narrator_runs",
    "prior_bundle_blob", "prior_approval_blob", "refreshed_at",
}


def unique_json_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate key")
        value[key] = item
    return value


def validate_refresh_review_evidence(workdir, ticket, text, manifests, reviewer, narrator):
    relative = f"factory/attestations/{ticket}/refresh.json"
    path = workdir / relative
    if not os.path.lexists(path):
        historical = git(
            workdir, "log", "-1", "--format=%H", "HEAD", "--", relative,
        ).stdout.strip()
        if historical:
            raise Refusal("committed refresh receipt is missing from the ticket head")
        return None
    if not safe_optional_attestation(path):
        raise Refusal("refresh receipt is unsafe")

    try:
        receipt = json.loads(path.read_text(), object_pairs_hook=unique_json_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise Refusal("refresh receipt is malformed")
    if not isinstance(receipt, dict):
        raise Refusal("refresh receipt is malformed")
    counts = [receipt.get(name) for name in (
        "prior_reviewer_runs", "prior_approve_verdicts",
        "prior_request_changes_verdicts", "prior_narrator_runs",
    )]
    generation = receipt.get("generation")
    if (
        set(receipt) != REFRESH_RECEIPT_KEYS
        or receipt.get("schema") != "nysa.software-factory.ticket-refresh/v1"
        or receipt.get("ticket") != ticket
        or isinstance(generation, bool)
        or not isinstance(generation, int)
        or generation < 1
        or any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in counts)
        or counts[0] != counts[1] + counts[2]
        or not all(valid_oid(receipt.get(name)) for name in ("old_head", "base_head", "merge_head"))
    ):
        raise Refusal("refresh receipt identity or baselines are invalid")
    receipt_commit = git(
        workdir, "log", "-1", "--format=%H", "HEAD", "--", relative,
    ).stdout.strip()
    parents = git(workdir, "rev-list", "--parents", "-n", "1", receipt_commit).stdout.split()
    if parents != [receipt_commit, receipt["merge_head"]]:
        raise Refusal("refresh receipt commit topology is invalid")
    merge_parents = git(
        workdir, "rev-list", "--parents", "-n", "1", receipt["merge_head"],
    ).stdout.split()
    if merge_parents != [
        receipt["merge_head"], receipt["old_head"], receipt["base_head"],
    ]:
        raise Refusal("refresh merge topology is invalid")
    previous_result = git(
        workdir, "show", f"{receipt['old_head']}:{relative}", check=False,
    )
    expected_generation = 1
    if previous_result.returncode == 0:
        try:
            previous = json.loads(previous_result.stdout, object_pairs_hook=unique_json_object)
            previous_generation = previous.get("generation")
        except (json.JSONDecodeError, ValueError, AttributeError):
            raise Refusal("prior refresh receipt is malformed")
        if (
            set(previous) != REFRESH_RECEIPT_KEYS
            or previous.get("schema") != "nysa.software-factory.ticket-refresh/v1"
            or previous.get("ticket") != ticket
            or isinstance(previous_generation, bool)
            or not isinstance(previous_generation, int)
            or previous_generation < 1
        ):
            raise Refusal("prior refresh generation is invalid")
        expected_generation = previous_generation + 1
    elif git(
        workdir, "log", "-1", "--format=%H", receipt["old_head"], "--", relative,
    ).stdout.strip():
        raise Refusal("prior refresh receipt is missing from the recorded old head")
    if generation != expected_generation:
        raise Refusal("refresh generation is not continuous")
    old_ticket = git(
        workdir, "show", f"{receipt['old_head']}:factory/tickets/{ticket}.md",
    ).stdout
    old_verdicts, old_voids = reviewer_sequences(old_ticket)
    current_verdicts, current_voids = reviewer_sequences(text)
    old_approvals = sum(value.endswith(":APPROVE") for value in old_verdicts)
    if (
        receipt["prior_reviewer_runs"] != len(old_verdicts)
        or receipt["prior_approve_verdicts"] != old_approvals
        or receipt["prior_request_changes_verdicts"] != len(old_verdicts) - old_approvals
        or current_verdicts[:len(old_verdicts)] != old_verdicts
        or current_voids[:len(old_voids)] != old_voids
    ):
        raise Refusal("refresh historical review evidence changed")
    try:
        preserved = preserved_control_paths(
            workdir, receipt["old_head"], receipt["base_head"],
        )
    except ClassificationError as error:
        raise Refusal(f"refresh semantic classification failed: {error}")
    reviewers = sorted(
        (item for item in manifests if item.get("role") == "reviewer"),
        key=lambda item: item["_ledger_index"],
    )
    raw_reviewers = len(old_verdicts) + len(old_voids)

    def belongs_to_old_head(item):
        head = item.get("role_head_before", "")
        return valid_oid(head) and not git(
            workdir, "merge-base", "--is-ancestor",
            head, receipt["old_head"], check=False,
        ).returncode

    if (
        preserved is not None
        and belongs_to_old_head(reviewer)
        and belongs_to_old_head(narrator)
    ):
        return receipt["base_head"]
    if (
        len(current_verdicts) <= len(old_verdicts)
        or any(value <= raw_reviewers for value in current_voids[len(old_voids):])
    ):
        raise Refusal("a new post-refresh Reviewer verdict is required")
    try:
        reviewer_ordinal = reviewers.index(reviewer) + 1
    except ValueError:
        raise Refusal("post-refresh Reviewer evidence is missing")
    if reviewer_ordinal <= raw_reviewers:
        raise Refusal("post-refresh Reviewer evidence is required")
    for role, manifest in (("Reviewer", reviewer), ("Narrator", narrator)):
        head = manifest.get("role_head_before", "")
        if git(
            workdir, "merge-base", "--is-ancestor", receipt_commit, head,
            check=False,
        ).returncode or git(
            workdir, "merge-base", "--is-ancestor", head, "HEAD",
            check=False,
        ).returncode:
            raise Refusal(f"post-refresh {role} evidence is required")
    return None


def exact_pr(repo, branch, state):
    fields = "number,headRefName,baseRefName,headRefOid,url,state,isDraft,mergedAt,mergeCommit"
    result = json.loads(gh(
        "pr", "list", "--repo", repo, "--state", state, "--head", branch,
        "--base", "main", "--json", fields,
    ).stdout)
    if not isinstance(result, list) or len(result) != 1:
        raise Refusal(f"expected exactly one {state} PR for {repo}:{branch} -> main")
    pr = result[0]
    if pr.get("headRefName") != branch or pr.get("baseRefName") != "main":
        raise Refusal("GitHub returned a PR with the wrong head or base")
    return pr


def exact_pr_number(repo, branch, number):
    if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
        raise Refusal("approval attestation lacks an exact PR number")
    fields = "number,headRefName,baseRefName,headRefOid,url,state,isDraft,mergedAt,mergeCommit"
    pr = json.loads(gh(
        "pr", "view", str(number), "--repo", repo, "--json", fields,
    ).stdout)
    if (
        not isinstance(pr, dict)
        or pr.get("number") != number
        or pr.get("headRefName") != branch
        or pr.get("baseRefName") != "main"
    ):
        raise Refusal("approval-bound PR identity is invalid")
    return pr


def emergency_pr(repo, branch, ticket, number, workdir, protected):
    if number is None:
        return exact_pr(repo, branch, "all")
    fields = (
        "number,headRefName,baseRefName,headRefOid,url,state,isDraft,"
        "mergedAt,mergeCommit"
    )
    pr = json.loads(gh(
        "pr", "view", str(number), "--repo", repo, "--json", fields,
    ).stdout)
    head = pr.get("headRefOid", "")
    head_ref = pr.get("headRefName", "")
    ticket_path = f"factory/tickets/{ticket}.md"
    if (
        pr.get("number") != number
        or pr.get("baseRefName") != "main"
        or pr.get("state") != "MERGED"
        or not pr.get("mergedAt")
        or not valid_oid(head)
        or head_ref != branch and not head_ref.startswith(branch + "-")
        or git(workdir, "rev-parse", f"{head}:{ticket_path}").stdout.strip()
        != git(workdir, "rev-parse", f"{protected}:{ticket_path}").stdout.strip()
    ):
        raise Refusal("explicit emergency PR does not bind the protected ticket")
    return pr


def ensure_closeout_pr(repo, ticket, branch, head, method):
    fields = "number,headRefName,baseRefName,headRefOid,url,state,mergedAt,mergeCommit"

    def candidates():
        value = json.loads(gh(
            "pr", "list", "--repo", repo, "--state", "all", "--head", branch,
            "--base", "main", "--json", fields,
        ).stdout)
        if not isinstance(value, list):
            raise Refusal("GitHub returned invalid closeout PR evidence")
        return value

    prs = candidates()
    if not prs:
        gh(
            "pr", "create", "--repo", repo, "--head", branch, "--base", "main",
            "--title", f"{ticket}: record protected merge closeout",
            "--body", (
                f"Factory-owned metadata and accounting closeout for {ticket}.\n\n"
                "No additional business approval is required. Protected checks, "
                "reviews, and merge policy remain authoritative."
            ),
        )
        prs = candidates()
    if len(prs) != 1:
        raise Refusal("expected exactly one closeout PR for the exact branch")
    pr = prs[0]
    if (
        pr.get("headRefName") != branch
        or pr.get("baseRefName") != "main"
        or pr.get("headRefOid") != head
        or not isinstance(pr.get("number"), int)
        or pr["number"] <= 0
    ):
        raise Refusal("closeout PR repository, branch, base, or head is invalid")
    if pr.get("state") == "MERGED":
        if not pr.get("mergedAt"):
            raise Refusal("merged closeout PR lacks merge evidence")
        return pr
    if pr.get("state") != "OPEN":
        raise Refusal("closeout PR is neither open nor merged")
    gh(
        "pr", "merge", str(pr["number"]), "--repo", repo, "--auto",
        f"--{method}",
    )
    view = json.loads(gh(
        "pr", "view", str(pr["number"]), "--repo", repo,
        "--json", "number,headRefName,baseRefName,headRefOid,autoMergeRequest,state,mergedAt",
    ).stdout)
    request = view.get("autoMergeRequest") or {}
    if (
        view.get("number") != pr["number"]
        or view.get("headRefName") != branch
        or view.get("baseRefName") != "main"
        or view.get("headRefOid") != head
        or (
            view.get("state") != "MERGED"
            and request.get("mergeMethod") != method.upper()
        )
    ):
        raise Refusal("GitHub did not confirm auto-merge for the exact closeout head")
    return view


def ensure_clean_branch(product, workdir, expected, *, based_on_main=False, require_remote=True):
    if git(workdir, "status", "--porcelain", "--untracked-files=all").stdout:
        raise Refusal("attestation worktree must be clean")
    branch = git(workdir, "symbolic-ref", "--quiet", "--short", "HEAD").stdout.strip()
    if branch != expected:
        raise Refusal(f"attestation worktree must be on {expected}")
    local = git(workdir, "rev-parse", "HEAD").stdout.strip()
    if require_remote:
        remote = git(workdir, "rev-parse", f"refs/remotes/origin/{branch}", check=False)
        if remote.returncode or remote.stdout.strip() != local:
            raise Refusal("local branch must exactly match its origin tracking tip")
    if based_on_main and git(
        workdir, "merge-base", "--is-ancestor", "origin/main", "HEAD", check=False
    ).returncode:
        raise Refusal("closeout branch must be based on origin/main")
    return local


def field(text, name):
    matches = re.findall(rf"^{re.escape(name)}:\s*(.*?)\s*$", text, re.I | re.M)
    if len(matches) != 1:
        raise Refusal(f"ticket must contain exactly one {name} field")
    return matches[0]


def merge_policy(text):
    matches = re.findall(r"^Merge-Policy:\s*(.*?)\s*$", text, re.I | re.M)
    if len(matches) > 1:
        raise Refusal("ticket must contain at most one Merge-Policy field")
    policy = matches[0].lower() if matches else "manual"
    if policy not in {"manual", "auto"}:
        raise Refusal("Merge-Policy must be manual or auto")
    return policy


def protected_merge_policy(workdir, ticket):
    result = git(
        workdir, "show", f"refs/remotes/origin/main:factory/tickets/{ticket}.md",
        check=False,
    )
    if result.returncode:
        raise Refusal("protected origin/main ticket is unavailable")
    return merge_policy(result.stdout)


def replace_field(text, name, value):
    return re.sub(
        rf"^{re.escape(name)}:\s*.*$", f"{name}: {value}", text,
        count=1, flags=re.I | re.M,
    )


def check_item(text, label):
    pattern = rf"^- \[[ xX]\] {re.escape(label)}\s*$"
    if re.search(pattern, text, re.M):
        return re.sub(pattern, f"- [x] {label}", text, count=1, flags=re.M)
    return text


def uncheck_item(text, label):
    pattern = rf"^- \[[ xX]\] {re.escape(label)}\s*$"
    if re.search(pattern, text, re.M):
        return re.sub(pattern, f"- [ ] {label}", text, count=1, flags=re.M)
    return text


def remove_field(text, name):
    return re.sub(
        rf"^{re.escape(name)}:\s*.*\n?", "", text,
        count=1, flags=re.I | re.M,
    )


def set_link(text, label, value):
    pattern = rf"^- {re.escape(label)}:\s*.*$"
    if re.search(pattern, text, re.M):
        return re.sub(pattern, f"- {label}: {value}", text, count=1, flags=re.M)
    return text


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def blob_id(workdir, path):
    return git(workdir, "hash-object", str(path)).stdout.strip()


def blob_at(workdir, commit, relative):
    content = git(workdir, "show", f"{commit}:{relative}").stdout
    result = run(
        ["git", "-C", str(workdir), "hash-object", "--stdin"],
        input_text=content,
    )
    return result.stdout.strip()


def valid_oid(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value)


def validate_bundle_attestation(value, ticket, repo, branch, kit_sha, workdir):
    try:
        return validate_shared_bundle_attestation(
            value, ticket, repo, branch, kit_sha, workdir,
        )
    except ApprovalEvidenceError as error:
        raise Refusal(str(error)) from error


def validate_bundle_commit(workdir, ticket, value, bundle_commit):
    try:
        return validate_shared_bundle_commit(workdir, ticket, value, bundle_commit)
    except ApprovalEvidenceError as error:
        raise Refusal(str(error)) from error


def validate_approval_attestation(
    value, bundle_att, ticket, repo, branch, kit_sha, method, workdir, head,
):
    try:
        return validate_shared_approval_attestation(
            value, bundle_att, ticket, repo, branch, kit_sha, method, workdir, head,
        )
    except ApprovalEvidenceError as error:
        raise Refusal(str(error)) from error


def protected_approval_evidence(
    workdir, ticket, repo, branch, kit_sha, method, pr,
):
    root = workdir / "factory" / "attestations" / ticket
    bundle_path = root / "bundle.json"
    approval_path = root / "approval.json"
    if not bundle_path.is_file() or not approval_path.is_file():
        raise Refusal("protected main lacks bundle or approval attestation")
    approval_att = json.loads(approval_path.read_text())
    approval_head = pr.get("headRefOid", "")
    if not valid_oid(approval_head):
        raise Refusal("merged PR lacks the approved head commit")
    try:
        bundle_att, approval_att, _ = validate_shared_approval_continuation(
            workdir, ticket, repo, branch, kit_sha, method,
            approval_att.get("reviewed_sha", ""), approval_head,
        )
    except ApprovalEvidenceError as error:
        raise Refusal(
            f"protected approval evidence is invalid: {error}"
        ) from error
    bundle_attestation_blob = blob_id(workdir, bundle_path)
    approval_attestation_blob = blob_id(workdir, approval_path)
    if (
        approval_att.get("pr_number") != pr.get("number")
        or approval_att.get("bundle_attestation_blob")
        != bundle_attestation_blob
    ):
        raise Refusal("protected approval evidence does not match the merged PR head")
    approved_ticket = git(
        workdir, "show", f"{approval_head}:factory/tickets/{ticket}.md",
    ).stdout
    if (
        field(approved_ticket, "State").lower() != "approved"
        or field(approved_ticket, "Operator-Approval").lower() != "linear"
    ):
        raise Refusal("merged PR head does not contain the attested Approved ticket")
    return bundle_att, approval_att, approval_head, (
        bundle_attestation_blob, approval_attestation_blob,
    )


def successful_post_merge_checks(repo, merge, required):
    combined = json.loads(gh("api", f"repos/{repo}/commits/{merge}/status").stdout)
    statuses = {}
    for status in combined.get("statuses", []):
        name = status.get("context")
        if name in required and name not in statuses:
            statuses[name] = status.get("state")
    successful = []
    for name in required:
        encoded = quote(name, safe="")
        response = json.loads(gh(
            "api",
            f"repos/{repo}/commits/{merge}/check-runs"
            f"?check_name={encoded}&filter=latest",
        ).stdout)
        check_runs = [
            item for item in response.get("check_runs", [])
            if item.get("name") == name
        ]
        if name in statuses and check_runs:
            raise Refusal(f"required context name is ambiguous across status and check APIs: {name}")
        if len(check_runs) > 1:
            raise Refusal(f"multiple latest check runs share required name: {name}")
        if name in statuses:
            if statuses[name] == "pending":
                raise Refusal(f"required post-merge check is pending: {name}")
            passed = statuses[name] == "success"
        elif len(check_runs) == 1:
            item = check_runs[0]
            if item.get("status") != "completed":
                raise Refusal(f"required post-merge check is pending: {name}")
            passed = (
                item.get("conclusion") == "success"
            )
        else:
            raise Refusal(f"required post-merge check is missing: {name}")
        if not passed:
            raise Refusal(f"required post-merge check is unsuccessful: {name}")
        successful.append(name)
    return successful


def emergency_request(path, *, require_current=True):
    if not path.is_absolute() or path.is_symlink():
        raise Refusal("emergency closeout request is unsafe")
    original = path
    path = path.resolve(strict=True)
    info = path.lstat()
    if (
        path != original
        or not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_nlink != 1
        or info.st_size > 64_000
    ):
        raise Refusal("emergency closeout request is unsafe")
    try:
        def unique_object(pairs):
            value = {}
            for name, item in pairs:
                if name in value:
                    raise ValueError("duplicate request field")
                value[name] = item
            return value

        value = json.loads(path.read_text(), object_pairs_hook=unique_object)
    except (UnicodeError, ValueError) as error:
        raise Refusal("emergency closeout request is invalid") from error
    if not isinstance(value, dict) or set(value) != EMERGENCY_REQUEST_KEYS:
        raise Refusal("emergency closeout request has unknown or missing fields")
    issued = timestamp(value.get("issued_at"), "emergency issued_at")
    expires = timestamp(value.get("expires_at"), "emergency expires_at")
    current = datetime.now(timezone.utc)
    if (
        value.get("schema") != EMERGENCY_REQUEST_SCHEMA
        or issued.tzinfo is None
        or expires.tzinfo is None
        or issued.microsecond
        or expires.microsecond
        or expires <= issued
        or (expires - issued).total_seconds() > 24 * 60 * 60
        or (
            require_current
            and (issued > current or (current - issued).total_seconds() > 15 * 60 or current >= expires)
        )
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", value.get("operator_id", ""))
        or value.get("operator_id") == "auto"
        or not isinstance(value.get("reason"), str)
        or not 20 <= len(value["reason"]) <= 500
        or not re.fullmatch(
            r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/issues/[1-9][0-9]*",
            value.get("issue", ""),
        )
    ):
        raise Refusal("emergency closeout request authority is invalid or expired")
    return value


def authenticated_passport(ticket, state_dir):
    module_path = Path(__file__).with_name("ticket-passport.py")
    spec = importlib.util.spec_from_file_location("emergency_ticket_passport", module_path)
    if spec is None or spec.loader is None:
        raise Refusal("passport validator is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        state_dir = module.safe_directory(state_dir)
        value, _ = module.load_passport(
            state_dir / "passports" / f"{ticket}.json", module.key(state_dir),
        )
    except (FileNotFoundError, OSError, ValueError) as error:
        raise Refusal(f"authenticated emergency passport is unavailable: {error}") from error
    return value


def controller_record(path, label):
    descriptor = -1
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
            or info.st_size > 64_000
        ):
            raise Refusal(f"{label} is unsafe")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            raw = handle.read()
        value = json.loads(raw)
    except (OSError, json.JSONDecodeError) as error:
        raise Refusal(f"{label} is unavailable") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not isinstance(value, dict):
        raise Refusal(f"{label} is invalid")
    return raw, value


def paused_claim_basis(path, ticket, branch, state, passport, issue):
    raw, value = controller_record(path, "paused emergency checkpoint")
    signed = dict(value)
    pause_digest = signed.pop("pause_sha256", "")
    budget = value.get("budget_sha256")
    resume_state = value.get("resume_state")
    paused_status = value.get("status")
    resume_states = {"Backlog", "Ready", "Planning", "Building", "Review"}
    if (
        set(value) != EMERGENCY_PAUSE_KEYS
        or value.get("schema") != "nysa.software-factory.ticket-pause/v2"
        or value.get("blocking_issue") != issue
        or value.get("ticket") != ticket
        or value.get("branch") != branch
        or paused_status not in {"blocked", "budget", "claimed", "waiting"}
        or (paused_status == "budget" and not re.fullmatch(r"[0-9a-f]{64}", budget or ""))
        or value.get("current_state") != state
        or value.get("current_state") != passport.get("current_state")
        or value.get("head_sha") != passport.get("head_sha")
        or value.get("passport_sha256") != passport.get("passport_sha256")
        or not valid_oid(value.get("factory_sha", ""))
        or not valid_oid(value.get("passport_factory_sha", ""))
        or not isinstance(value.get("created_at_epoch"), int)
        or isinstance(value.get("created_at_epoch"), bool)
        or value["created_at_epoch"] <= 0
        or not isinstance(value.get("current_stage"), str)
        or not value["current_stage"]
        or (
            state == "Blocked-Escalated"
            and resume_state not in resume_states
        )
        or (
            state != "Blocked-Escalated"
            and resume_state is not None
            and resume_state not in resume_states
        )
        or not isinstance(value.get("worktree"), str)
        or not Path(value["worktree"]).is_absolute()
        or (budget is not None and not re.fullmatch(r"[0-9a-f]{64}", budget))
        or not re.fullmatch(r"[0-9a-f]{64}", value.get("run_snapshot_sha256", ""))
        or not re.fullmatch(r"[0-9a-f]{64}", pause_digest)
        or pause_digest != hashlib.sha256(json.dumps(
            signed, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
        ).encode()).hexdigest()
    ):
        raise Refusal("paused emergency checkpoint is invalid")
    return {
        "sha256": hashlib.sha256(raw).hexdigest(),
        "status": "blocked",
        "role": "factory-paused",
        "blocked_reason": "factory-issue-pause",
        "receipt": pause_digest,
        "parked": True,
    }


def validate_linked_issue(url):
    match = re.fullmatch(
        r"https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/issues/([1-9][0-9]*)",
        url,
    )
    issue = json.loads(gh("api", f"repos/{match.group(1)}/issues/{match.group(2)}").stdout)
    if (
        issue.get("number") != int(match.group(2))
        or issue.get("html_url") != url
        or issue.get("state") != "open"
        or "pull_request" in issue
    ):
        raise Refusal("linked emergency closeout issue is not an exact open issue")


def emergency_preview(args, product, workdir, repo, prefix, checks, kit_sha, method):
    request = emergency_request(args.request)
    validate_linked_issue(request["issue"])
    try:
        protected_terminal(workdir, args.ticket)
    except ValidationError as error:
        if str(error) != "protected main lacks valid terminal evidence":
            raise Refusal(f"protected terminal evidence is invalid: {error}") from error
    else:
        raise Refusal("ticket is already terminal on protected main")
    main_head = git(workdir, "rev-parse", "origin/main").stdout.strip()
    remote_main = git(
        workdir, "ls-remote", "--heads", "--", os.environ["FACTORY_CERTIFIED_PRODUCT_ORIGIN"],
        "refs/heads/main",
    ).stdout.split()
    if remote_main[:1] != [main_head]:
        raise Refusal("origin/main is not the authoritative protected tip")
    ticket_path = f"factory/tickets/{args.ticket}.md"
    text = git(workdir, "show", f"{main_head}:{ticket_path}").stdout
    state = field(text, "State")
    if state not in ({
        "Ready", "Planning", "Building", "Review", "Awaiting Approval",
        "Approved", "Blocked-Escalated",
    } | ({"Backlog"} if args.pr is not None else set())):
        raise Refusal("emergency closeout requires one exact nonterminal protected state")
    branch = f"{prefix}{args.ticket}"
    pr = emergency_pr(
        repo, branch, args.ticket, args.pr, workdir, main_head,
    )
    if pr.get("state") != "MERGED" or not pr.get("mergedAt"):
        raise Refusal("ticket PR is not merged")
    pr_head = pr.get("headRefOid", "")
    merge = (pr.get("mergeCommit") or {}).get("oid", "")
    if not valid_oid(pr_head) or not valid_oid(merge):
        raise Refusal("merged PR lacks exact head and merge commits")
    if git(workdir, "merge-base", "--is-ancestor", merge, main_head, check=False).returncode:
        raise Refusal("PR merge commit is not reachable from authoritative origin/main")
    successful = successful_post_merge_checks(repo, merge, checks)
    state_dir = Path(os.environ.get("FACTORY_CONTROLLER_STATE_DIR", ""))
    if not state_dir.is_absolute():
        raise Refusal("trusted controller state is unavailable")
    state_info = state_dir.lstat()
    if (
        state_dir.resolve(strict=True) != state_dir
        or not stat.S_ISDIR(state_info.st_mode)
        or state_info.st_uid != os.geteuid()
        or stat.S_IMODE(state_info.st_mode) != 0o700
    ):
        raise Refusal("trusted controller state is unsafe")
    passport_path = state_dir / "passports" / f"{args.ticket}.json"
    claim_path = state_dir / "claims" / f"{args.ticket}.json"
    pause_path = state_dir / f"pause-{args.ticket}.json"
    if passport_path.exists() and not passport_path.is_symlink():
        passport = authenticated_passport(args.ticket, state_dir)
        if (
            passport.get("ticket") != args.ticket
            or passport.get("project") != os.environ.get("FACTORY_PROJECT")
            or passport.get("branch") != branch
            or passport.get("current_state") != state
            or passport.get("publication_state") not in {
                "none", "validating", "ready", "merge-pending", "merged", "repair",
            }
            or not valid_oid(passport.get("factory_sha", ""))
            or not valid_oid(passport.get("head_sha", ""))
            or not re.fullmatch(r"[0-9a-f]{64}", passport.get("passport_sha256", ""))
        ):
            raise Refusal("authenticated passport does not match the emergency target")
        passport_plan = {
            name: passport[name]
            for name in (
                "passport_sha256", "current_state", "publication_state",
                "factory_sha", "head_sha",
            )
        }
        if os.path.lexists(claim_path):
            claim_raw, claim = controller_record(
                claim_path, "blocked emergency claim",
            )
            if (
                claim.get("schema") != "nysa.software-factory.controller-claim/v1"
                or claim.get("ticket") != args.ticket
                or claim.get("branch") != branch
                or claim.get("status") != "blocked"
                or claim.get("parked") is not True
                or claim.get("lease") != ""
                or claim.get("publication_lease") != ""
                or not re.fullmatch(r"[a-z][a-z-]*", claim.get("role", ""))
                or not isinstance(claim.get("blocked_reason"), str)
                or not claim["blocked_reason"]
                or not re.fullmatch(r"[0-9a-f]{64}", claim.get("receipt", ""))
            ):
                raise Refusal("emergency claim is not an exact idle blocked claim")
            claim_plan = {
                "sha256": hashlib.sha256(claim_raw).hexdigest(),
                "status": claim["status"],
                "role": claim["role"],
                "blocked_reason": claim["blocked_reason"],
                "receipt": claim["receipt"],
                "parked": claim["parked"],
            }
        elif os.path.lexists(pause_path):
            claim_plan = paused_claim_basis(
                pause_path, args.ticket, branch, state, passport,
                request["issue"],
            )
        else:
            raise Refusal("blocked emergency claim is unavailable")
        execution_basis = "authenticated-passport"
    elif (
        os.path.lexists(passport_path)
        or os.path.lexists(claim_path)
        or os.path.lexists(pause_path)
    ):
        raise Refusal("passportless emergency target is not exact operator-built work")
    else:
        passport_plan = None
        claim_plan = None
        if state == "Backlog" and args.pr is not None:
            execution_basis = "protected-merge-no-runtime"
        elif field(text, "Assignee") == "operator (built outside the software factory)":
            execution_basis = "operator-built-no-runtime"
        else:
            raise Refusal("passportless emergency target is not exact operator-built work")
    plan = {
        "schema": EMERGENCY_PLAN_SCHEMA,
        "ticket": args.ticket,
        "repository": repo,
        "branch": branch,
        "pr_number": pr["number"],
        "pr_head": pr_head,
        "merge_commit": merge,
        "merged_at": pr["mergedAt"],
        "protected_main": {
            "commit": main_head,
            "tree": git(workdir, "rev-parse", f"{main_head}^{{tree}}").stdout.strip(),
            "ticket_blob": git(workdir, "rev-parse", f"{main_head}:{ticket_path}").stdout.strip(),
            "state": state,
        },
        "required_checks": checks,
        "successful_checks": successful,
        "passport": passport_plan,
        "claim": claim_plan,
        "execution_basis": execution_basis,
        "kit_sha": kit_sha,
        "auto_merge_method": method,
        **{name: value for name, value in request.items() if name != "schema"},
    }
    approval = hashlib.sha256(json.dumps(
        plan, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    return {"action": "emergency-plan", "plan": plan, "approval_sha256": approval}


def validate_closeout_commit(
    workdir, ticket, head, done_att, repo, original_pr, merge, checks,
    successful, kit_sha, method, bundle_att, approval_att, approval_head,
    evidence_blobs,
):
    expected_keys = {
        "schema", "ticket", "repository", "pr_number", "approved_pr_head",
        "reviewed_sha", "bundle_blob", "bundle_attestation_blob",
        "approval_attestation_blob", "approval_parent_head",
        "auto_merge_method", "merge_commit", "merged_at", "required_checks",
        "successful_checks", "ledger", "kit_sha", "closeout_parent",
        "attested_at",
    }
    required_paths = {
        f"factory/tickets/{ticket}.md",
        f"factory/attestations/{ticket}/done.json",
    }
    allowed_paths = required_paths | {"factory/ledger.csv"}
    parent = git(workdir, "rev-parse", f"{head}^").stdout.strip()
    paths = set(git(
        workdir, "diff-tree", "--no-commit-id", "--name-only", "-r", head,
    ).stdout.splitlines())
    ledger = done_att.get("ledger") or {}
    ledger_path = workdir / "factory" / "ledger.csv"
    if (
        set(done_att) != expected_keys
        or done_att.get("schema") != "nysa.software-factory.ticket-done/v1"
        or done_att.get("ticket") != ticket
        or done_att.get("repository") != repo
        or done_att.get("pr_number") != original_pr.get("number")
        or done_att.get("approved_pr_head") != approval_head
        or done_att.get("reviewed_sha") != bundle_att["reviewed_sha"]
        or done_att.get("bundle_blob") != bundle_att["bundle_blob"]
        or done_att.get("bundle_attestation_blob") != evidence_blobs[0]
        or done_att.get("approval_attestation_blob") != evidence_blobs[1]
        or done_att.get("approval_parent_head") != approval_att["parent_head"]
        or done_att.get("auto_merge_method") != method
        or done_att.get("merge_commit") != merge
        or done_att.get("merged_at") != original_pr.get("mergedAt")
        or done_att.get("required_checks") != checks
        or done_att.get("successful_checks") != successful
        or done_att.get("kit_sha") != kit_sha
        or done_att.get("closeout_parent") != parent
        or not required_paths.issubset(paths)
        or not paths.issubset(allowed_paths)
        or ledger.get("schema") != "nysa.software-factory.ledger-projection/v1"
        or ledger.get("status") != "ok"
        or ledger.get("ticket") != ticket
        or ledger.get("sha256") != hashlib.sha256(ledger_path.read_bytes()).hexdigest()
    ):
        raise Refusal("existing closeout commit or Done receipt is invalid")
    timestamp(done_att.get("attested_at"), "Done attestation")
    text = (workdir / "factory" / "tickets" / f"{ticket}.md").read_text()
    if (
        field(text, "State").lower() != "done"
        or field(text, "Operator-Approval").lower() != "linear"
    ):
        raise Refusal("existing closeout commit lacks the attested Done ticket")
    return parent


def push_head(product, workdir, remote, branch, head):
    configured = git(product, "remote", "get-url", "--push", "--all", "origin").stdout.splitlines()
    if configured != [remote]:
        raise Refusal("configured origin no longer matches the certified product origin")
    git(workdir, "push", "--no-force", "--", remote, f"{head}:refs/heads/{branch}")
    observed = git(workdir, "ls-remote", "--heads", "--", remote, f"refs/heads/{branch}").stdout
    if observed.split()[:1] != [head]:
        raise Refusal("remote did not confirm the attestation commit")
    git(workdir, "update-ref", f"refs/remotes/origin/{branch}", head)
    return head


def commit_push(product, workdir, remote, branch, message, paths):
    for path in paths:
        git(workdir, "add", "--", str(path.relative_to(workdir)))
    git(
        workdir, "-c", "user.name=Software Factory", "-c",
        "user.email=factory@local", "commit", "-m", message,
    )
    head = git(workdir, "rev-parse", "HEAD").stdout.strip()
    return push_head(product, workdir, remote, branch, head)


def operator_map_path(product):
    path = Path(os.environ.get(
        "FACTORY_OPERATOR_MAP", product / "factory/linear-map.json"
    ))
    if not path.is_absolute():
        raise Refusal("operator map path is invalid")
    return path


def consume_overlay(product, ticket, expected_version):
    path = operator_map_path(product)
    if not path.is_file():
        return
    data = json.loads(path.read_text())
    entry = data.get("tickets", {}).get(ticket, {})
    operator = entry.get("operator") or {}
    actual = hashlib.sha256(json.dumps(
        {key: operator[key] for key in ("priority", "initiative", "state", "approval") if key in operator},
        sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    if actual != expected_version:
        raise Refusal("operator overlay changed before consumption")
    entry.pop("operator", None)
    fd, temporary = tempfile.mkstemp(prefix=".linear-map.", dir=path.parent)
    with os.fdopen(fd, "w") as output:
        json.dump(data, output, indent=2, sort_keys=True)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)


def stale_approval_overlay_version(product, ticket):
    path = operator_map_path(product)
    if not path.is_file():
        return None
    operator = json.loads(path.read_text()).get("tickets", {}).get(ticket, {}).get("operator")
    if not (
        operator
        and operator.get("state") == "Approved"
        and operator.get("approval") == "Linear"
        and operator.get("state_base") == "awaiting approval"
    ):
        return None
    return hashlib.sha256(json.dumps(
        {key: operator[key] for key in (
            "state", "approval", "state_base", "observed_at", "linear_updated_at",
        ) if key in operator},
        sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()


def consume_stale_approval_overlay(product, ticket, expected_version):
    path = operator_map_path(product)
    data = json.loads(path.read_text())
    operator = data.get("tickets", {}).get(ticket, {}).get("operator") or {}
    actual = stale_approval_overlay_version(product, ticket)
    if actual != expected_version:
        raise Refusal("stale approval overlay changed before consumption")
    for key in ("state", "approval", "state_base", "observed_at", "linear_updated_at"):
        operator.pop(key, None)
    if not operator:
        data.get("tickets", {}).get(ticket, {}).pop("operator", None)
    fd, temporary = tempfile.mkstemp(prefix=".linear-map.", dir=path.parent)
    with os.fdopen(fd, "w") as output:
        json.dump(data, output, indent=2, sort_keys=True)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)


def safe_optional_attestation(path):
    if not os.path.lexists(path):
        return False
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise Refusal(f"attestation path is unsafe: {path.name}")
    return True


def refresh_baselines(text, manifests):
    reviewers = sorted(
        (item for item in manifests if item.get("role") == "reviewer"),
        key=lambda item: item["_ledger_index"],
    )
    narrators = [item for item in manifests if item.get("role") == "narrator"]
    voids = set()
    for match in re.finditer(
        r"^\s*OPERATOR NOTE:\s*reviewer run\s+(\d+)\s+void[^A-Za-z0-9]*duplicate\s*$",
        text, re.I | re.M,
    ):
        ordinal = int(match.group(1))
        if 1 <= ordinal <= len(reviewers):
            voids.add(ordinal)
    verdicts = re.findall(
        r"^\s*reviewer round\s+\d+:\s*(APPROVE|REQUEST CHANGES(?:\s+—\s+.*)?)\s*$",
        text, re.I | re.M,
    )
    approvals = sum(value.upper() == "APPROVE" for value in verdicts)
    requests = len(verdicts) - approvals
    reviewer_count = len(reviewers) - len(voids)
    if reviewer_count != len(verdicts):
        raise Refusal("reviewer runs and verdicts must be complete before base refresh")
    return reviewer_count, approvals, requests, len(narrators)


def refresh(args, product, workdir, repo, prefix, remote):
    branch = f"{prefix}{args.ticket}"
    old_head = ensure_clean_branch(product, workdir, branch)
    ticket_path = workdir / "factory" / "tickets" / f"{args.ticket}.md"
    text = ticket_path.read_text()
    state = field(text, "State").lower()
    repaired_building = (
        state == "building"
        and os.environ.get("FACTORY_TRANSITION_STAGE") in {
            "REFUSE refresh receipt was not committed directly after its merge",
            "REFUSE stale refresh receipt does not bind this branch history",
        }
    )
    if (
        state not in {"review", "awaiting approval", "approved"}
        and not repaired_building
    ):
        raise Refusal("refresh requires ticket State Review, Awaiting Approval, or Approved")
    pr = exact_pr(repo, branch, "open")
    if pr.get("headRefOid") != old_head:
        raise Refusal("PR head does not match the exact ticket branch")
    configured = git(product, "remote", "get-url", "--push", "--all", "origin").stdout.splitlines()
    if configured != [remote]:
        raise Refusal("configured origin no longer matches the certified product origin")
    observed = git(workdir, "ls-remote", "--heads", "--", remote, "refs/heads/main").stdout.split()
    if len(observed) != 2 or not valid_oid(observed[0]) or observed[1] != "refs/heads/main":
        raise Refusal("certified protected main tip is missing or ambiguous")
    base_head = observed[0]
    git(workdir, "fetch", "--no-tags", "--", remote, "refs/heads/main")
    if git(workdir, "rev-parse", "FETCH_HEAD").stdout.strip() != base_head:
        raise Refusal("fetched protected main does not match its certified remote tip")
    if not git(workdir, "merge-base", "--is-ancestor", base_head, old_head, check=False).returncode:
        raise Refusal("ticket branch is already based on protected main")

    view = json.loads(gh(
        "pr", "view", str(pr["number"]), "--repo", repo,
        "--json", "number,headRefName,baseRefName,headRefOid,autoMergeRequest,state,isDraft,mergeStateStatus",
    ).stdout)
    if (
        view.get("number") != pr["number"]
        or view.get("headRefName") != branch
        or view.get("baseRefName") != "main"
        or view.get("headRefOid") != old_head
        or view.get("state") != "OPEN"
    ):
        raise Refusal("GitHub did not confirm the exact open PR before refresh")
    if view.get("autoMergeRequest"):
        gh("pr", "merge", str(pr["number"]), "--repo", repo, "--disable-auto")
        confirmed = json.loads(gh(
            "pr", "view", str(pr["number"]), "--repo", repo,
            "--json", "number,headRefName,baseRefName,headRefOid,autoMergeRequest,state,isDraft,mergeStateStatus",
        ).stdout)
        if (
            confirmed.get("number") != pr["number"]
            or confirmed.get("headRefName") != branch
            or confirmed.get("baseRefName") != "main"
            or confirmed.get("headRefOid") != old_head
            or confirmed.get("state") != "OPEN"
            or confirmed.get("autoMergeRequest") is not None
        ):
            raise Refusal("GitHub did not disable auto-merge for the exact stale PR head")
        view = confirmed
    if not view.get("isDraft"):
        gh("pr", "ready", str(pr["number"]), "--repo", repo, "--undo")
        draft = json.loads(gh(
            "pr", "view", str(pr["number"]), "--repo", repo,
            "--json", "number,headRefName,baseRefName,headRefOid,autoMergeRequest,state,isDraft,mergeStateStatus",
        ).stdout)
        if (
            draft.get("number") != pr["number"]
            or draft.get("headRefName") != branch
            or draft.get("baseRefName") != "main"
            or draft.get("headRefOid") != old_head
            or draft.get("state") != "OPEN"
            or not draft.get("isDraft")
            or draft.get("autoMergeRequest") is not None
        ):
            raise Refusal("GitHub did not make the exact stale PR head a draft")

    manifests = successful_runs(product, workdir, args.ticket)
    reviewers, approvals, requests, narrators = refresh_baselines(text, manifests)
    bundle_path = workdir / "factory" / "attestations" / args.ticket / "bundle.json"
    approval_path = bundle_path.with_name("approval.json")
    attestation_dir = bundle_path.parent
    for directory in (attestation_dir.parent, attestation_dir):
        if os.path.lexists(directory) and (
            directory.is_symlink() or not directory.is_dir()
        ):
            raise Refusal("ticket attestation directory is unsafe")
    had_bundle = safe_optional_attestation(bundle_path)
    had_approval = safe_optional_attestation(approval_path)
    prior_bundle = blob_id(workdir, bundle_path) if had_bundle else None
    prior_approval = blob_id(workdir, approval_path) if had_approval else None
    previous_path = bundle_path.with_name("refresh.json")
    generation = 1
    had_previous = safe_optional_attestation(previous_path)
    prior_refresh = blob_id(workdir, previous_path) if had_previous else None
    if not had_previous and git(
        workdir, "log", "-1", "--format=%H", "HEAD", "--",
        str(previous_path.relative_to(workdir)),
    ).stdout.strip():
        raise Refusal("historical refresh receipt is missing from the ticket head")
    if had_previous:
        try:
            previous = json.loads(
                previous_path.read_text(), object_pairs_hook=unique_json_object,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            raise Refusal("existing refresh receipt is malformed")
        if not isinstance(previous, dict):
            raise Refusal("existing refresh receipt is malformed")
        previous_generation = previous.get("generation")
        if (
            set(previous) != REFRESH_RECEIPT_KEYS
            or previous.get("schema") != "nysa.software-factory.ticket-refresh/v1"
            or previous.get("ticket") != args.ticket
            or isinstance(previous_generation, bool)
            or not isinstance(previous_generation, int)
            or previous_generation < 1
        ):
            raise Refusal("existing refresh receipt is malformed")
        generation = previous_generation + 1

    merged = git(
        workdir, "-c", "user.name=Software Factory", "-c",
        "user.email=factory@local", "merge", "--no-ff", "--no-edit", base_head,
        check=False,
    )
    if merged.returncode:
        git(workdir, "merge", "--abort", check=False)
        if git(workdir, "rev-parse", "HEAD").stdout.strip() != old_head:
            raise Refusal("base refresh conflict could not restore the ticket head")
        raise Refusal("protected main conflicts with the ticket branch; refresh aborted")
    merge_head = git(workdir, "rev-parse", "HEAD").stdout.strip()
    parents = git(workdir, "rev-list", "--parents", "-n", "1", merge_head).stdout.split()
    if parents != [merge_head, old_head, base_head]:
        raise Refusal("base refresh did not create the required two-parent merge")

    for directory in (attestation_dir.parent, attestation_dir):
        if os.path.lexists(directory) and (
            directory.is_symlink() or not directory.is_dir()
        ):
            raise Refusal("protected main introduced an unsafe attestation directory")
    if safe_optional_attestation(bundle_path) != had_bundle or \
       safe_optional_attestation(approval_path) != had_approval:
        raise Refusal("protected main changed stale attestation evidence during refresh")
    if (
        (had_bundle and blob_id(workdir, bundle_path) != prior_bundle)
        or (had_approval and blob_id(workdir, approval_path) != prior_approval)
    ):
        raise Refusal("protected main changed stale attestation evidence during refresh")
    if safe_optional_attestation(previous_path) != had_previous:
        raise Refusal("protected main changed refresh evidence during refresh")
    if had_previous and blob_id(workdir, previous_path) != prior_refresh:
        raise Refusal("protected main changed refresh evidence during refresh")

    merged_text = ticket_path.read_text()
    merged_text = replace_field(merged_text, "State", "Review")
    merged_text = remove_field(merged_text, "Operator-Approval")
    merged_text = uncheck_item(merged_text, "Evidence bundle posted")
    merged_text = uncheck_item(merged_text, "Operator approved")
    ticket_path.write_text(merged_text)
    for stale in (bundle_path, approval_path):
        if stale.exists():
            stale.unlink()
    receipt = {
        "schema": "nysa.software-factory.ticket-refresh/v1",
        "ticket": args.ticket,
        "generation": generation,
        "old_head": old_head,
        "base_head": base_head,
        "merge_head": merge_head,
        "prior_reviewer_runs": reviewers,
        "prior_approve_verdicts": approvals,
        "prior_request_changes_verdicts": requests,
        "prior_narrator_runs": narrators,
        "prior_bundle_blob": prior_bundle,
        "prior_approval_blob": prior_approval,
        "refreshed_at": now(),
    }
    write_json(previous_path, receipt)
    changed_paths = [ticket_path, previous_path]
    if had_bundle:
        changed_paths.append(bundle_path)
    if had_approval:
        changed_paths.append(approval_path)
    result_head = commit_push(
        product, workdir, remote, branch, f"{args.ticket}: refresh protected base evidence",
        changed_paths,
    )
    refreshed_pr = exact_pr(repo, branch, "open")
    if (
        refreshed_pr.get("number") != pr["number"]
        or refreshed_pr.get("headRefOid") != result_head
        or not refreshed_pr.get("isDraft")
    ):
        raise Refusal("GitHub did not confirm the exact refreshed draft PR head")
    overlay = stale_approval_overlay_version(product, args.ticket)
    if overlay:
        consume_stale_approval_overlay(product, args.ticket, overlay)
    return {"action": "refresh", "head": result_head, "attestation": receipt}


DEPENDENCY_REFRESH_KEYS = {
    "schema", "ticket", "generation", "dependencies", "dependency_terminals",
    "old_head", "prior_base_head", "protected_head", "merge_head",
    "preserved_state", "refreshed_at",
}

DEPENDENCY_CONFLICT_REFRESH_KEYS = {
    "schema", "ticket", "generation", "dependencies", "dependency_terminals",
    "old_head", "old_head_tree", "prior_base_head", "protected_head",
    "protected_head_tree", "protected_project_blob", "protected_delta_sha256",
    "test_paths", "test_paths_sha256", "conflicts", "repair_owner",
    "resolution", "merge_head", "merge_head_tree", "preserved_state",
    "transition_receipt_sha256", "factory_sha", "contract_version",
    "refreshed_at",
}


def safe_project_test_paths(workdir, protected_head):
    raw = git(
        workdir, "show", f"{protected_head}:factory/PROJECT.env",
    ).stdout
    values = re.findall(r"(?m)^TEST_PATHS=(.*)$", raw)
    if len(values) != 1:
        raise Refusal("protected PROJECT.env TEST_PATHS is ambiguous")
    try:
        paths = " ".join(
            shlex.split(values[0], comments=False, posix=True)
        ).split()
    except ValueError as error:
        raise Refusal("protected PROJECT.env TEST_PATHS is invalid") from error
    safe = re.compile(r"[A-Za-z0-9._][A-Za-z0-9._/-]*")
    if (
        not paths
        or len(paths) != len(set(paths))
        or any(
            not safe.fullmatch(path.rstrip("/"))
            or any(part in {"", ".", ".."} for part in path.rstrip("/").split("/"))
            or path.rstrip("/") == "factory"
            or path.rstrip("/").startswith("factory/")
            for path in paths
        )
    ):
        raise Refusal("protected PROJECT.env TEST_PATHS is invalid")
    normalized = [path.rstrip("/") for path in paths]
    if any(
        left == right
        or left.startswith(right + "/")
        or right.startswith(left + "/")
        for index, left in enumerate(normalized)
        for right in normalized[index + 1:]
    ):
        raise Refusal("protected PROJECT.env TEST_PATHS overlaps")
    return paths


def path_is_test(path, test_paths):
    return any(
        path.startswith(prefix) if prefix.endswith("/") else path == prefix
        for prefix in test_paths
    )


def conflict_index(workdir):
    raw = git(workdir, "ls-files", "-u", "-z").stdout
    entries = {}
    for item in raw.split("\0"):
        if not item:
            continue
        metadata, separator, path = item.partition("\t")
        fields = metadata.split()
        if (
            not separator
            or len(fields) != 3
            or fields[2] not in {"1", "2", "3"}
            or not re.fullmatch(r"[0-7]{6}", fields[0])
            or not valid_oid(fields[1])
            or not re.fullmatch(r"[A-Za-z0-9._][A-Za-z0-9._/@+-]*", path)
            or any(part in {"", ".", ".."} for part in path.split("/"))
        ):
            raise Refusal("dependency conflict index is unsafe")
        stage = int(fields[2])
        if stage in entries.setdefault(path, {}):
            raise Refusal("dependency conflict index is ambiguous")
        entries[path][stage] = {
            "blob": fields[1],
            "mode": fields[0],
        }
    unresolved = [
        path for path in git(
            workdir, "diff", "--name-only", "--diff-filter=U", "-z",
        ).stdout.split("\0") if path
    ]
    if (
        not unresolved
        or unresolved != sorted(unresolved)
        or len(unresolved) != len(set(unresolved))
        or set(unresolved) != set(entries)
    ):
        raise Refusal("dependency conflict index is ambiguous")
    conflicts = []
    for path in unresolved:
        stages = entries[path]
        if set(stages) != {1, 2, 3} or any(
            stages[stage]["mode"] != "100644" for stage in (1, 2, 3)
        ):
            raise Refusal("dependency conflict is not a regular both-modified file")
        conflicts.append({
            "path": path,
            "base_blob": stages[1]["blob"],
            "base_mode": stages[1]["mode"],
            "ticket_blob": stages[2]["blob"],
            "ticket_mode": stages[2]["mode"],
            "protected_blob": stages[3]["blob"],
            "protected_mode": stages[3]["mode"],
        })
    return conflicts


def dependency_refresh_generation(receipt_path, ticket):
    if not os.path.lexists(receipt_path):
        return 1
    if not safe_optional_attestation(receipt_path):
        raise Refusal("dependency refresh receipt is unsafe")
    try:
        previous = json.loads(
            receipt_path.read_text(encoding="utf-8"),
            object_pairs_hook=unique_json_object,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise Refusal("dependency refresh receipt is malformed")
    previous_generation = previous.get("generation")
    keys = (
        DEPENDENCY_REFRESH_KEYS
        if previous.get("schema")
        == "nysa.software-factory.dependency-refresh/v1"
        else DEPENDENCY_CONFLICT_REFRESH_KEYS
        if previous.get("schema")
        == "nysa.software-factory.dependency-refresh/v2"
        else set()
    )
    if (
        set(previous) != keys
        or previous.get("ticket") != ticket
        or isinstance(previous_generation, bool)
        or not isinstance(previous_generation, int)
        or previous_generation < 1
    ):
        raise Refusal("dependency refresh receipt is malformed")
    return previous_generation + 1


def dependency_refresh(args, product, workdir, prefix, remote):
    stage = os.environ.get("FACTORY_TRANSITION_STAGE", "")
    match = re.fullmatch(
        r"REFUSE dependency refresh required; "
        r"dependencies=(T-[0-9]+(?:,T-[0-9]+)*); "
        r"protected-main=([0-9a-f]{40})",
        stage,
    )
    if not match:
        raise Refusal("dependency refresh requires an exact transition receipt")
    dependencies = match[1].split(",")
    expected_base = match[2]
    branch = f"{prefix}{args.ticket}"
    old_head = ensure_clean_branch(product, workdir, branch)
    ticket_path = workdir / "factory" / "tickets" / f"{args.ticket}.md"
    text = ticket_path.read_text(encoding="utf-8")
    state = field(text, "State")
    if state.lower() not in {"planning", "building"}:
        raise Refusal("dependency refresh is limited to prepublication ticket states")
    declared = [item.strip() for item in field(text, "Depends-On").split(",")]
    if declared != dependencies:
        raise Refusal("dependency refresh does not match the ticket dependencies")
    attestation_dir = workdir / "factory" / "attestations" / args.ticket
    bundle = attestation_dir / "bundle.json"
    approval = attestation_dir / "approval.json"
    if any(os.path.lexists(path) for path in (bundle, approval)):
        raise Refusal("prepublication dependency refresh found publication evidence")
    configured = git(
        product, "remote", "get-url", "--push", "--all", "origin"
    ).stdout.splitlines()
    if configured != [remote]:
        raise Refusal("configured origin no longer matches the certified product origin")
    observed = git(
        workdir, "ls-remote", "--heads", "--", remote, "refs/heads/main"
    ).stdout.split()
    if (
        len(observed) != 2
        or not valid_oid(observed[0])
        or observed[1] != "refs/heads/main"
    ):
        raise Refusal("certified protected main tip is missing or ambiguous")
    if observed[0] != expected_base:
        return {
            "action": "dependency-wait",
            "expected_protected_head": expected_base,
            "observed_protected_head": observed[0],
        }
    git(workdir, "fetch", "--no-tags", "--", remote, "refs/heads/main")
    fetched = git(workdir, "rev-parse", "FETCH_HEAD").stdout.strip()
    if fetched != expected_base:
        return {
            "action": "dependency-wait",
            "expected_protected_head": expected_base,
            "observed_protected_head": fetched,
        }
    if not git(
        workdir, "merge-base", "--is-ancestor", expected_base, old_head,
        check=False,
    ).returncode:
        raise Refusal("ticket branch already contains the dependency base")
    terminals = []
    for dependency in dependencies:
        try:
            terminal = protected_dependency(product, dependency, expected_base)
        except ValidationError as error:
            raise Refusal(
                f"dependency terminal truth changed for {dependency}: {error}"
            )
        terminals.append({
            "ticket": dependency,
            "terminal_sha256": hashlib.sha256(
                json.dumps(
                    terminal, ensure_ascii=True, sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest(),
        })
    prior_base = git(workdir, "merge-base", old_head, expected_base).stdout.strip()
    if not valid_oid(prior_base):
        raise Refusal("dependency refresh prior base is invalid")
    ticket_blob = git(
        workdir, "rev-parse", f"{old_head}:factory/tickets/{args.ticket}.md"
    ).stdout.strip()
    route_relative = f"factory/route-plans/{args.ticket}.json"
    route_blob = git(
        workdir, "rev-parse", f"{old_head}:{route_relative}"
    ).stdout.strip()
    receipt_path = attestation_dir / "dependency-refresh.json"
    generation = dependency_refresh_generation(receipt_path, args.ticket)
    merged = git(
        workdir, "-c", "user.name=Software Factory", "-c",
        "user.email=factory@local", "merge", "--no-ff", "--no-edit",
        expected_base, check=False,
    )
    if merged.returncode:
        try:
            conflicts = conflict_index(workdir)
            test_paths = safe_project_test_paths(workdir, expected_base)
            if (
                state.lower() != "building"
                or not all(
                    path_is_test(item["path"], test_paths)
                    for item in conflicts
                )
            ):
                raise Refusal(
                    "protected dependency conflict is not test-author-owned"
                )
            for item in conflicts:
                git(workdir, "checkout", "--theirs", "--", item["path"])
                git(workdir, "add", "--", item["path"])
            if git(workdir, "ls-files", "-u", "-z").stdout:
                raise Refusal("dependency conflict resolution left an unmerged path")
            observed = git(
                workdir, "ls-remote", "--heads", "--", remote,
                "refs/heads/main",
            ).stdout.split()
            if observed != [expected_base, "refs/heads/main"]:
                git(workdir, "merge", "--abort", check=False)
                if git(workdir, "rev-parse", "HEAD").stdout.strip() != old_head:
                    raise Refusal(
                        "dependency conflict could not restore the ticket head"
                    )
                return {
                    "action": "dependency-wait",
                    "expected_protected_head": expected_base,
                    "observed_protected_head": observed[0] if observed else None,
                }
            git(
                workdir, "-c", "user.name=Software Factory", "-c",
                "user.email=factory@local", "commit", "--no-edit",
            )
            merge_head = git(workdir, "rev-parse", "HEAD").stdout.strip()
            parents = git(
                workdir, "rev-list", "--parents", "-n", "1", merge_head
            ).stdout.split()
            if parents != [merge_head, old_head, expected_base]:
                raise Refusal("dependency conflict resolution created an invalid merge")
            for item in conflicts:
                resolved_blob = git(
                    workdir, "rev-parse", f"{merge_head}:{item['path']}"
                ).stdout.strip()
                if resolved_blob != item["protected_blob"]:
                    raise Refusal(
                        "dependency conflict did not retain the protected baseline"
                    )
            if (
                git(
                    workdir, "rev-parse",
                    f"{merge_head}:factory/tickets/{args.ticket}.md",
                ).stdout.strip() != ticket_blob
                or git(
                    workdir, "rev-parse", f"{merge_head}:{route_relative}"
                ).stdout.strip() != route_blob
                or any(os.path.lexists(path) for path in (bundle, approval))
            ):
                raise Refusal(
                    "dependency conflict resolution changed ticket control evidence"
                )
            transition = os.environ.get(
                "FACTORY_TRANSITION_RECEIPT_SHA256", ""
            )
            factory_sha = os.environ.get("FACTORY_RELEASE_SHA", "")
            contract_version = os.environ.get(
                "FACTORY_CONTRACT_VERSION", ""
            )
            if (
                not re.fullmatch(r"[0-9a-f]{64}", transition)
                or not valid_oid(factory_sha)
                or contract_version != "1.8.0"
            ):
                raise Refusal(
                    "trusted dependency conflict evidence is unavailable"
                )
            project_blob = git(
                workdir, "rev-parse",
                f"{expected_base}:factory/PROJECT.env",
            ).stdout.strip()
            protected_delta = git(
                workdir, "diff", "--name-status", "-z",
                prior_base, expected_base,
            ).stdout.encode()
            receipt = {
                "schema": "nysa.software-factory.dependency-refresh/v2",
                "ticket": args.ticket,
                "generation": generation,
                "dependencies": dependencies,
                "dependency_terminals": terminals,
                "old_head": old_head,
                "old_head_tree": git(
                    workdir, "rev-parse", f"{old_head}^{{tree}}",
                ).stdout.strip(),
                "prior_base_head": prior_base,
                "protected_head": expected_base,
                "protected_head_tree": git(
                    workdir, "rev-parse", f"{expected_base}^{{tree}}",
                ).stdout.strip(),
                "protected_project_blob": project_blob,
                "protected_delta_sha256": hashlib.sha256(
                    protected_delta
                ).hexdigest(),
                "test_paths": test_paths,
                "test_paths_sha256": hashlib.sha256(json.dumps(
                    test_paths, ensure_ascii=True, separators=(",", ":"),
                ).encode()).hexdigest(),
                "conflicts": conflicts,
                "repair_owner": "test-author",
                "resolution": "protected-baseline-before-test-author",
                "merge_head": merge_head,
                "merge_head_tree": git(
                    workdir, "rev-parse", f"{merge_head}^{{tree}}",
                ).stdout.strip(),
                "preserved_state": state,
                "transition_receipt_sha256": transition,
                "factory_sha": factory_sha,
                "contract_version": contract_version,
                "refreshed_at": now(),
            }
            write_json(receipt_path, receipt)
            result_head = commit_push(
                product, workdir, remote, branch,
                f"{args.ticket}: bind protected test conflict", [receipt_path],
            )
            return {
                "action": "dependency-conflict-refresh",
                "head": result_head,
                "attestation": receipt,
            }
        except Refusal:
            git(workdir, "merge", "--abort", check=False)
            if git(workdir, "rev-parse", "HEAD").stdout.strip() != old_head:
                git(workdir, "reset", "--hard", old_head, check=False)
            if (
                git(workdir, "rev-parse", "HEAD").stdout.strip() != old_head
                or git(
                    workdir, "status", "--porcelain=v1", "-z",
                ).stdout
            ):
                raise Refusal(
                    "dependency conflict could not restore the ticket head"
                ) from None
            raise
    merge_head = git(workdir, "rev-parse", "HEAD").stdout.strip()
    parents = git(
        workdir, "rev-list", "--parents", "-n", "1", merge_head
    ).stdout.split()
    if parents != [merge_head, old_head, expected_base]:
        raise Refusal("dependency refresh did not create the required merge")
    if (
        git(
            workdir, "rev-parse",
            f"{merge_head}:factory/tickets/{args.ticket}.md",
        ).stdout.strip() != ticket_blob
        or git(
            workdir, "rev-parse", f"{merge_head}:{route_relative}"
        ).stdout.strip() != route_blob
        or any(os.path.lexists(path) for path in (bundle, approval))
    ):
        git(workdir, "reset", "--hard", old_head)
        raise Refusal("dependency refresh changed ticket control evidence")
    receipt = {
        "schema": "nysa.software-factory.dependency-refresh/v1",
        "ticket": args.ticket,
        "generation": generation,
        "dependencies": dependencies,
        "dependency_terminals": terminals,
        "old_head": old_head,
        "prior_base_head": prior_base,
        "protected_head": expected_base,
        "merge_head": merge_head,
        "preserved_state": state,
        "refreshed_at": now(),
    }
    write_json(receipt_path, receipt)
    result_head = commit_push(
        product, workdir, remote, branch,
        f"{args.ticket}: bind protected dependency base", [receipt_path],
    )
    if git(
        workdir, "merge-base", "--is-ancestor", expected_base, result_head,
        check=False,
    ).returncode:
        raise Refusal("dependency refresh result omitted protected main")
    return {
        "action": "dependency-refresh",
        "head": result_head,
        "attestation": receipt,
    }


def bundle(args, product, workdir, repo, prefix, remote, kit_sha):
    branch = f"{prefix}{args.ticket}"
    head = ensure_clean_branch(product, workdir, branch)
    ticket_path = workdir / "factory" / "tickets" / f"{args.ticket}.md"
    bundle_path = workdir / "factory" / "tickets" / f"{args.ticket}-bundle.md"
    text = ticket_path.read_text()
    if merge_policy(text) != protected_merge_policy(workdir, args.ticket):
        raise Refusal("Merge-Policy differs from protected origin/main")
    if field(text, "State").lower() != "review":
        raise Refusal("bundle requires ticket State Review")
    bundle_text = bundle_path.read_text()
    if re.search(r"\bNOT\s+APPROVABLE\s*:", bundle_text, re.I):
        raise Refusal("evidence bundle is explicitly not approvable")
    required = (
        "What this does", "Preview", "Screenshots", "Acceptance criteria",
        "Risk", "Cost", "Rollback",
    )
    if any(not re.search(rf"^#+\s+.*{re.escape(section)}", bundle_text, re.I | re.M) for section in required):
        raise Refusal("evidence bundle is missing a required section")
    if not re.search(r"approve to merge", bundle_text, re.I):
        raise Refusal("evidence bundle lacks the operator approval question")
    manifests = successful_runs(product, workdir, args.ticket)
    route_plan = route_plan_evidence(workdir, product, args.ticket, kit_sha, manifests)
    reviewer, narrator, reviewed = review_evidence(text, manifests, workdir)
    preserved_base = validate_refresh_review_evidence(
        workdir, args.ticket, text, manifests, reviewer, narrator,
    )
    allowed = {
        f"factory/route-plans/{args.ticket}.json",
        f"factory/tickets/{args.ticket}.md",
        f"factory/tickets/{args.ticket}-bundle.md",
    }
    changed = set(git(workdir, "diff", "--name-only", f"{reviewed}..{head}").stdout.splitlines())
    allowed.update(
        trusted_narrator_evidence_paths(
            workdir, args.ticket, reviewed, head, changed,
        )
    )
    if preserved_base:
        allowed.add(f"factory/attestations/{args.ticket}/refresh.json")
        allowed.update(retained_control_paths(workdir, head, preserved_base, changed))
    if not changed or changed - allowed:
        raise Refusal("product or code changed after the reviewed SHA")
    pr = exact_pr(repo, branch, "open")
    if pr.get("headRefOid") != head:
        raise Refusal("PR head does not match the exact ticket branch")
    blob = git(workdir, "hash-object", str(bundle_path)).stdout.strip()
    attestation_path = workdir / "factory" / "attestations" / args.ticket / "bundle.json"
    attestation = {
        "schema": (
            "nysa.software-factory.ticket-bundle/v2"
            if "legacy_planner_manifest_sha256" in route_plan
            else "nysa.software-factory.ticket-bundle/v1"
        ),
        "ticket": args.ticket,
        "repository": repo,
        "branch": branch,
        "branch_head": head,
        "reviewed_sha": reviewed,
        "bundle_path": str(bundle_path.relative_to(workdir)),
        "bundle_blob": blob,
        "pr_number": pr["number"],
        "pr_url": pr.get("url"),
        "reviewer_run_id": reviewer["run_id"],
        "narrator_run_id": narrator["run_id"],
        "kit_sha": kit_sha,
        **route_plan,
        "attested_at": now(),
    }
    write_json(attestation_path, attestation)
    text = replace_field(text, "State", "Awaiting Approval")
    text = check_item(text, "Evidence bundle posted")
    text = set_link(text, "PR", pr.get("url") or f"#{pr['number']}")
    text = set_link(text, "Evidence", str(bundle_path.relative_to(workdir)))
    ticket_path.write_text(text)
    result_head = commit_push(
        product, workdir, remote, branch, f"{args.ticket}: attest operator bundle",
        (ticket_path, attestation_path),
    )
    return {"action": "bundle", "head": result_head, "attestation": attestation}


def approval(args, product, workdir, repo, prefix, remote, kit_sha, method):
    attest_only = getattr(args, "attest_only", False)
    branch = f"{prefix}{args.ticket}"
    head = ensure_clean_branch(product, workdir, branch)
    ticket_path = workdir / "factory" / "tickets" / f"{args.ticket}.md"
    bundle_path = workdir / "factory" / "tickets" / f"{args.ticket}-bundle.md"
    attestation_path = workdir / "factory" / "attestations" / args.ticket / "bundle.json"
    approval_path = attestation_path.with_name("approval.json")
    bundle_value = json.loads(attestation_path.read_text())
    map_path = operator_map_path(product)
    mapping = json.loads(map_path.read_text()) if map_path.is_file() else {}
    operator = mapping.get("tickets", {}).get(args.ticket, {}).get("operator") or {}
    existing_approval = json.loads(approval_path.read_text()) if approval_path.exists() else None
    exact_overlay = (
        operator.get("state") == "Approved"
        and operator.get("approval") == "Linear"
        and operator.get("state_base") == "awaiting approval"
    )
    projected_overlay = not any(
        key in operator for key in ("state", "approval", "state_base")
    )
    if not exact_overlay and not (existing_approval and projected_overlay):
        raise Refusal("exact Linear Awaiting Approval -> Approved overlay is required")
    if attest_only and not exact_overlay:
        raise Refusal("exact Linear approval overlay is required for phase-one attestation")
    if existing_approval:
        reviewed = existing_approval.get("reviewed_sha", "")
        try:
            bundle_att, approval_att, _ = validate_shared_approval_continuation(
                workdir, args.ticket, repo, branch, kit_sha, method,
                reviewed, head,
            )
        except ApprovalEvidenceError as error:
            raise Refusal(str(error)) from error
        version = approval_att["operator_version"]
        if exact_overlay:
            current_version = hashlib.sha256(json.dumps(
                {
                    key: operator[key]
                    for key in ("priority", "initiative", "state", "approval")
                    if key in operator
                },
                sort_keys=True, separators=(",", ":"),
            ).encode()).hexdigest()
            if current_version != version:
                raise Refusal(
                    "existing approval attestation does not match the overlay"
                )
    else:
        bundle_att = validate_bundle_attestation(
            bundle_value, args.ticket, repo, branch, kit_sha, workdir,
        )
        observed = timestamp(operator.get("observed_at"), "approval observation")
        updated = timestamp(operator.get("linear_updated_at"), "Linear approval update")
        attested = timestamp(bundle_att.get("attested_at"), "bundle attestation")
        if observed <= attested or updated <= attested:
            raise Refusal("Linear approval is not newer than the bundle attestation")
        version = hashlib.sha256(json.dumps(
            {
                key: operator[key]
                for key in ("priority", "initiative", "state", "approval")
                if key in operator
            },
            sort_keys=True, separators=(",", ":"),
        ).encode()).hexdigest()
    if git(workdir, "hash-object", str(bundle_path)).stdout.strip() != bundle_att.get("bundle_blob"):
        raise Refusal("evidence bundle changed after attestation")
    pr = exact_pr(repo, branch, "open")
    if pr.get("number") != bundle_att.get("pr_number") or pr.get("headRefOid") != head:
        raise Refusal("PR identity or head changed before approval")
    if attest_only:
        before = json.loads(gh(
            "pr", "view", str(pr["number"]), "--repo", repo,
            "--json", "number,headRefOid,autoMergeRequest,state",
        ).stdout)
        if (
            before.get("number") != pr["number"]
            or before.get("headRefOid") != head
        ):
            raise Refusal("PR head changed before approval attestation")
        if before.get("autoMergeRequest"):
            gh(
                "pr", "merge", str(pr["number"]), "--repo", repo,
                "--disable-auto",
            )
            disabled = json.loads(gh(
                "pr", "view", str(pr["number"]), "--repo", repo,
                "--json", "number,headRefOid,autoMergeRequest,state",
            ).stdout)
            if (
                disabled.get("number") != pr["number"]
                or disabled.get("headRefOid") != head
                or disabled.get("autoMergeRequest")
            ):
                raise Refusal(
                    "GitHub did not disable auto-merge before approval attestation"
                )
    if not existing_approval:
        text = ticket_path.read_text()
        if field(text, "State").lower() != "awaiting approval":
            raise Refusal("approval requires committed Awaiting Approval state")
        validate_bundle_commit(workdir, args.ticket, bundle_att, head)
        text = replace_field(text, "State", "Approved")
        if re.search(r"^Operator-Approval:", text, re.I | re.M):
            text = replace_field(text, "Operator-Approval", "Linear")
        else:
            text = re.sub(r"^(State:.*)$", r"\1\nOperator-Approval: Linear", text, count=1, flags=re.M)
        text = check_item(text, "Operator approved")
        ticket_path.write_text(text)
        approval_att = {
            "schema": "nysa.software-factory.ticket-approval/v1",
            "ticket": args.ticket,
            "repository": repo,
            "branch": branch,
            "parent_head": head,
            "reviewed_sha": bundle_att["reviewed_sha"],
            "bundle_blob": bundle_att["bundle_blob"],
            "bundle_attestation_blob": git(workdir, "hash-object", str(attestation_path)).stdout.strip(),
            "pr_number": pr["number"],
            "operator_version": version,
            "linear_updated_at": operator["linear_updated_at"],
            "observed_at": operator["observed_at"],
            "kit_sha": kit_sha,
            "auto_merge_method": method,
            "attested_at": operator["observed_at"],
        }
        write_json(approval_path, approval_att)
        head = commit_push(
            product, workdir, remote, branch, f"{args.ticket}: attest Linear approval",
            (ticket_path, approval_path),
        )
        validate_approval_attestation(
            approval_att, bundle_att, args.ticket, repo, branch, kit_sha,
            method, workdir, head,
        )
    if attest_only:
        current = exact_pr(repo, branch, "open")
        view = json.loads(gh(
            "pr", "view", str(current["number"]), "--repo", repo,
            "--json", "number,headRefOid,autoMergeRequest,state",
        ).stdout)
        if (
            current.get("number") != approval_att["pr_number"]
            or current.get("headRefOid") != head
            or view.get("number") != current["number"]
            or view.get("headRefOid") != head
            or view.get("autoMergeRequest")
        ):
            raise Refusal(
                "approval attestation head is not protected from auto-merge"
            )
        return {
            "action": "approval-attested",
            "auto_merge": False,
            "head": head,
            "pr_number": current["number"],
        }
    current = exact_pr(repo, branch, "open")
    if current.get("number") != approval_att["pr_number"] or current.get("headRefOid") != head:
        raise Refusal("PR head changed before auto-merge request")
    if current.get("isDraft"):
        gh("pr", "ready", str(current["number"]), "--repo", repo)
        current = exact_pr(repo, branch, "open")
        if (
            current.get("number") != approval_att["pr_number"]
            or current.get("headRefOid") != head
            or current.get("isDraft")
        ):
            raise Refusal("GitHub did not mark the exact approved PR head ready")
    gh(
        "pr", "merge", str(current["number"]), "--repo", repo, "--auto",
        f"--{method}",
    )
    view = json.loads(gh(
        "pr", "view", str(current["number"]), "--repo", repo,
        "--json", "number,headRefOid,autoMergeRequest,state,mergeStateStatus",
    ).stdout)
    request = view.get("autoMergeRequest") or {}
    if view.get("headRefOid") != head or (
        view.get("state") != "MERGED"
        and request.get("mergeMethod") != method.upper()
    ):
        raise Refusal("GitHub did not confirm auto-merge for the exact approved head")
    if exact_overlay:
        consume_overlay(product, args.ticket, version)
    return {"action": "approval", "head": head, "pr_number": current["number"], "auto_merge": True}


def finalize_terminal(product, workdir, remote, ticket, expected_basis):
    git(
        workdir, "fetch", "--quiet", "--", remote,
        "refs/heads/main:refs/remotes/origin/main",
    )
    try:
        terminal = protected_terminal(workdir, ticket)
    except ValidationError as error:
        raise Refusal(f"protected terminal validation failed: {error}") from error
    if terminal.get("basis") != expected_basis:
        raise Refusal("protected terminal evidence has the wrong basis")
    sync = run([
        sys.executable, "-I", "-S", str(Path(__file__).with_name("linear-sync.py")),
        "--factory-root", str(product), "--ticket", ticket, "--terminal",
    ])
    try:
        linear = json.loads(sync.stdout)
    except json.JSONDecodeError as error:
        raise Refusal("Linear terminal sync returned invalid evidence") from error
    if (
        not isinstance(linear, dict)
        or set(linear) != {
            "identifier", "issue_id", "source_ref", "state", "state_id", "updated",
        }
        or not isinstance(linear["identifier"], str)
        or not linear["identifier"]
        or not isinstance(linear["issue_id"], str)
        or not linear["issue_id"]
        or linear["source_ref"] != "refs/remotes/origin/main"
        or linear["state"] != "Done"
        or not isinstance(linear["state_id"], str)
        or not linear["state_id"]
        or not isinstance(linear["updated"], bool)
    ):
        raise Refusal("Linear terminal sync did not confirm exact Done")
    return {
        "basis": terminal["basis"],
        "protected_main": git(workdir, "rev-parse", "origin/main").stdout.strip(),
        "linear": linear,
    }


def record_terminal_controller_event(ticket, kit_sha, terminal):
    state = Path(os.environ.get("FACTORY_CONTROLLER_STATE_DIR", ""))
    if not state.is_absolute():
        raise Refusal("trusted controller state is unavailable")
    state_info = state.lstat()
    if (
        state.resolve(strict=True) != state
        or not stat.S_ISDIR(state_info.st_mode)
        or state_info.st_uid != os.geteuid()
        or stat.S_IMODE(state_info.st_mode) != 0o700
    ):
        raise Refusal("trusted controller state is unsafe")
    events = state / "events"
    events.mkdir(mode=0o700, exist_ok=True)
    info = events.lstat()
    if (
        events.resolve(strict=True) != events
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise Refusal("trusted controller event directory is unsafe")
    linear = terminal["linear"]
    details = {
        "linear_identifier": linear["identifier"],
        "linear_issue_id": linear["issue_id"],
        "linear_state_id": linear["state_id"],
        "protected_main": terminal["protected_main"],
        "terminal_basis": terminal["basis"],
    }
    for path in sorted(events.glob("*.json")):
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "r") as stream:
            event = json.load(stream)
            info = os.fstat(stream.fileno())
        digest = event.pop("event_sha256", "") if isinstance(event, dict) else ""
        encoded = json.dumps(
            event, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
        ).encode()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_size > 1_000_000
            or digest != hashlib.sha256(encoded).hexdigest()
        ):
            raise Refusal("controller event evidence is invalid")
        if (
            event.get("schema") == "nysa.software-factory.controller-event/v1"
            and event.get("event") == "linear_terminal_synced"
            and event.get("factory_sha") == kit_sha
            and event.get("ticket") == ticket
            and all(event.get(name) == value for name, value in details.items())
        ):
            return
    value = {
        "event": "linear_terminal_synced",
        "factory_sha": kit_sha,
        "observed_at_epoch_ns": time.time_ns(),
        "schema": "nysa.software-factory.controller-event/v1",
        "ticket": ticket,
        **details,
    }
    encoded = json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
    ).encode()
    value["event_sha256"] = hashlib.sha256(encoded).hexdigest()
    identity = hashlib.sha256(json.dumps(
        {"factory_sha": kit_sha, "ticket": ticket, **details},
        ensure_ascii=True, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    path = events / f"terminal-{ticket}-{identity}.json"
    try:
        descriptor = os.open(
            path, os.O_WRONLY | os.O_CREAT | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0), 0o600,
        )
    except FileExistsError:
        return record_terminal_controller_event(ticket, kit_sha, terminal)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(value, stream, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def emergency_done(
    args, product, workdir, repo, prefix, remote, checks, kit_sha, method,
    preview, request,
):
    branch = f"chore/{args.ticket.lower().replace('-', '')}-closeout"
    head = ensure_clean_branch(product, workdir, branch, require_remote=False)
    main_head = git(workdir, "rev-parse", "origin/main").stdout.strip()
    remote_line = git(
        workdir, "ls-remote", "--heads", "--", remote, f"refs/heads/{branch}",
    ).stdout.split()
    remote_head = remote_line[0] if remote_line else ""
    done_path = workdir / "factory" / "attestations" / args.ticket / "done.json"
    retry = head != main_head
    done_att = json.loads(done_path.read_text()) if done_path.is_file() else None
    pending_push = remote_head != head
    if not retry and remote_head not in ("", head):
        raise Refusal("closeout branch must exactly match its certified remote tip")
    if retry and pending_push and (
        not done_att or remote_head not in ("", done_att.get("closeout_parent"))
    ):
        raise Refusal("closeout branch must exactly match its certified remote tip")
    if retry and not done_att:
        raise Refusal("modified closeout head lacks an exact emergency Done receipt")
    if not retry and done_path.is_file():
        raise Refusal("Done is already present on protected main; use terminal sequencing")
    ticket_branch = f"{prefix}{args.ticket}"
    pr = emergency_pr(
        repo, ticket_branch, args.ticket, args.pr, workdir, main_head,
    )
    merge = (pr.get("mergeCommit") or {}).get("oid", "")
    if (
        pr.get("state") != "MERGED"
        or not pr.get("mergedAt")
        or not valid_oid(pr.get("headRefOid", ""))
        or not valid_oid(merge)
        or git(workdir, "merge-base", "--is-ancestor", merge, "origin/main", check=False).returncode
    ):
        raise Refusal("emergency closeout requires one exact merged ticket PR on protected main")
    successful = successful_post_merge_checks(repo, merge, checks)
    if retry:
        plan = done_att.get("plan") if isinstance(done_att, dict) else None
        expected_request = {
            name: request[name] for name in EMERGENCY_REQUEST_KEYS if name != "schema"
        }
        if (
            done_att.get("schema") != EMERGENCY_DONE_SCHEMA
            or done_att.get("approval_sha256") != args.approve_hash
            or not isinstance(plan, dict)
            or any(plan.get(name) != value for name, value in expected_request.items())
            or done_att.get("pr_number") != pr.get("number")
            or done_att.get("pr_head") != pr.get("headRefOid")
            or done_att.get("merge_commit") != merge
            or done_att.get("merged_at") != pr.get("mergedAt")
            or done_att.get("required_checks") != checks
            or done_att.get("successful_checks") != successful
        ):
            raise Refusal("emergency closeout retry does not match its exact authorization")
    else:
        if preview["approval_sha256"] != args.approve_hash:
            raise Refusal("approval hash does not match the exact emergency closeout plan")
        plan = preview["plan"]
        if (
            plan["protected_main"]["commit"] != head
            or plan["pr_number"] != pr.get("number")
            or plan["pr_head"] != pr.get("headRefOid")
            or plan["merge_commit"] != merge
            or plan["merged_at"] != pr.get("mergedAt")
            or plan["required_checks"] != checks
            or plan["successful_checks"] != successful
        ):
            raise Refusal("emergency closeout target changed after planning")
        ticket_path = workdir / "factory" / "tickets" / f"{args.ticket}.md"
        text = ticket_path.read_text()
        if field(text, "State") != plan["protected_main"]["state"]:
            raise Refusal("emergency closeout ticket changed after planning")
        ledger = Path(__file__).with_name("ledger-view.py")
        projection = run([
            sys.executable, "-I", "-S", str(ledger), "project",
            "--factory-root", str(product), "--workdir", str(workdir),
            "--ticket", args.ticket,
        ])
        ledger_result = json.loads(projection.stdout)
        text = replace_field(text, "State", "Done")
        text = check_item(text, "PR merged and staging confirmed")
        ticket_path.write_text(text)
        done_att = {
            "schema": EMERGENCY_DONE_SCHEMA,
            "ticket": args.ticket,
            "repository": repo,
            "pr_number": pr["number"],
            "pr_head": pr["headRefOid"],
            "merge_commit": merge,
            "merged_at": pr["mergedAt"],
            "required_checks": checks,
            "successful_checks": successful,
            "ledger": ledger_result,
            "kit_sha": kit_sha,
            "closeout_parent": head,
            "auto_merge_method": method,
            "plan": plan,
            "approval_sha256": args.approve_hash,
            "attested_at": now(),
        }
        write_json(done_path, done_att)
        head = commit_push(
            product, workdir, remote, branch,
            f"{args.ticket}: record authorized emergency closeout",
            (workdir / "factory" / "ledger.csv", ticket_path, done_path),
        )
    try:
        terminal = protected_terminal(workdir, args.ticket, head)
    except ValidationError as error:
        raise Refusal(f"emergency closeout receipt is invalid: {error}") from error
    if terminal.get("basis") != "attested-emergency-closeout":
        raise Refusal("emergency closeout did not produce exact terminal evidence")
    if retry and pending_push:
        push_head(product, workdir, remote, branch, head)
    closeout_pr = ensure_closeout_pr(repo, args.ticket, branch, head, method)
    finalized = (
        finalize_terminal(
            product, workdir, remote, args.ticket, "attested-emergency-closeout",
        )
        if closeout_pr["state"] == "MERGED" else None
    )
    if finalized:
        record_terminal_controller_event(args.ticket, kit_sha, finalized)
    return {
        "action": "emergency-done",
        "head": head,
        "attestation": done_att,
        "closeout_pr_number": closeout_pr["number"],
        "closeout_pr_state": closeout_pr["state"],
        "auto_merge": closeout_pr["state"] != "MERGED",
        **({"terminal": finalized} if finalized else {}),
    }


def done(args, product, workdir, repo, prefix, remote, checks, kit_sha, method):
    branch = f"chore/{args.ticket.lower().replace('-', '')}-closeout"
    head = ensure_clean_branch(product, workdir, branch, require_remote=False)
    main_head = git(workdir, "rev-parse", "origin/main").stdout.strip()
    remote_line = git(
        workdir, "ls-remote", "--heads", "--", remote, f"refs/heads/{branch}",
    ).stdout.split()
    remote_head = remote_line[0] if remote_line else ""
    done_path = workdir / "factory" / "attestations" / args.ticket / "done.json"
    retry = head != main_head
    done_att = json.loads(done_path.read_text()) if done_path.is_file() else None
    pending_push = remote_head != head
    if not retry and remote_head not in ("", head):
        raise Refusal("closeout branch must exactly match its certified remote tip")
    if retry and pending_push and (
        not done_att or remote_head not in ("", done_att.get("closeout_parent"))
    ):
        raise Refusal("closeout branch must exactly match its certified remote tip")
    if retry and not done_att:
        raise Refusal("modified closeout head lacks an exact Done receipt")
    if not retry and done_path.is_file():
        raise Refusal("Done is already present on protected main; use terminal sequencing")
    ticket_branch = f"{prefix}{args.ticket}"
    approval_path = (
        workdir / "factory" / "attestations" / args.ticket / "approval.json"
    )
    if not approval_path.is_file():
        raise Refusal("protected main lacks bundle or approval attestation")
    try:
        approval_value = json.loads(approval_path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise Refusal("protected approval evidence is malformed") from error
    pr = exact_pr_number(
        repo, ticket_branch,
        approval_value.get("pr_number") if isinstance(approval_value, dict) else None,
    )
    if pr.get("state") != "MERGED" or not pr.get("mergedAt"):
        raise Refusal("ticket PR is not merged")
    merge = (pr.get("mergeCommit") or {}).get("oid", "")
    if not re.fullmatch(r"[0-9a-f]{40}", merge):
        raise Refusal("merged PR lacks an exact merge commit")
    if git(workdir, "merge-base", "--is-ancestor", merge, "origin/main", check=False).returncode:
        raise Refusal("PR merge commit is not reachable from authoritative origin/main")
    ticket_path = workdir / "factory" / "tickets" / f"{args.ticket}.md"
    text = ticket_path.read_text()
    pins = re.findall(r"^Kit-SHA:\s*(.*?)\s*$", text, re.I | re.M)
    if len(pins) > 1:
        raise Refusal("closeout ticket has ambiguous Kit-SHA fields")
    bundle_path = workdir / "factory" / "attestations" / args.ticket / "bundle.json"
    bundle_value = json.loads(bundle_path.read_text())
    evidence_kit_sha = pins[0] if pins else bundle_value.get("kit_sha", "")
    if not valid_oid(evidence_kit_sha):
        raise Refusal("closeout ticket lacks a canonical Kit-SHA")
    bundle_att, approval_att, approval_head, evidence_blobs = (
        protected_approval_evidence(
            workdir, args.ticket, repo, ticket_branch, evidence_kit_sha, method, pr,
        )
    )
    successful = successful_post_merge_checks(repo, merge, checks)
    if (
        field(text, "State").lower() != "approved"
        or field(text, "Operator-Approval").lower() != "linear"
    ) and not retry:
        raise Refusal("closeout requires an Approved ticket on protected main")
    if retry:
        validate_closeout_commit(
            workdir, args.ticket, head, done_att, repo, pr, merge, checks,
            successful, evidence_kit_sha, method, bundle_att, approval_att,
            approval_head, evidence_blobs,
        )
        if pending_push:
            push_head(product, workdir, remote, branch, head)
    else:
        ledger = Path(__file__).with_name("ledger-view.py")
        projection = run([
            sys.executable, "-I", "-S", str(ledger), "project",
            "--factory-root", str(product), "--workdir", str(workdir),
            "--ticket", args.ticket,
        ])
        ledger_result = json.loads(projection.stdout)
        text = replace_field(text, "State", "Done")
        text = check_item(text, "PR merged and staging confirmed")
        ticket_path.write_text(text)
        done_att = {
            "schema": "nysa.software-factory.ticket-done/v1",
            "ticket": args.ticket,
            "repository": repo,
            "pr_number": pr["number"],
            "approved_pr_head": approval_head,
            "reviewed_sha": bundle_att["reviewed_sha"],
            "bundle_blob": bundle_att["bundle_blob"],
            "bundle_attestation_blob": evidence_blobs[0],
            "approval_attestation_blob": evidence_blobs[1],
            "approval_parent_head": approval_att["parent_head"],
            "auto_merge_method": method,
            "merge_commit": merge,
            "merged_at": pr["mergedAt"],
            "required_checks": checks,
            "successful_checks": successful,
            "ledger": ledger_result,
            "kit_sha": evidence_kit_sha,
            "closeout_parent": head,
            "attested_at": now(),
        }
        write_json(done_path, done_att)
        head = commit_push(
            product, workdir, remote, branch,
            f"{args.ticket}: record protected merge closeout",
            (workdir / "factory" / "ledger.csv", ticket_path, done_path),
        )
        validate_closeout_commit(
            workdir, args.ticket, head, done_att, repo, pr, merge, checks,
            successful, evidence_kit_sha, method, bundle_att, approval_att,
            approval_head, evidence_blobs,
        )
    closeout_pr = ensure_closeout_pr(
        repo, args.ticket, branch, head, method,
    )
    finalized = (
        finalize_terminal(product, workdir, remote, args.ticket, "attested-done")
        if closeout_pr["state"] == "MERGED" else None
    )
    return {
        "action": "done",
        "head": head,
        "attestation": done_att,
        "closeout_pr_number": closeout_pr["number"],
        "closeout_pr_state": closeout_pr["state"],
        "auto_merge": closeout_pr["state"] != "MERGED",
        **({"terminal": finalized} if finalized else {}),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ticket", required=True)
    parser.add_argument("--workdir", required=True)
    parser.add_argument(
        "--action",
        choices=(
            "bundle", "approval", "dependency-refresh", "refresh", "done",
            "emergency-plan", "emergency-apply",
        ),
        required=True,
    )
    parser.add_argument("--attest-only", action="store_true")
    parser.add_argument("--request", type=Path)
    parser.add_argument("--approve-hash", default="")
    parser.add_argument("--pr", type=int)
    args = parser.parse_args()
    if not re.fullmatch(r"T-\d+", args.ticket):
        parser.error("invalid ticket identifier")
    if args.attest_only and args.action != "approval":
        parser.error("--attest-only requires --action approval")
    emergency = args.action in {"emergency-plan", "emergency-apply"}
    if emergency != (args.request is not None) or (
        args.action == "emergency-apply"
        and not re.fullmatch(r"[0-9a-f]{64}", args.approve_hash)
    ) or (args.action != "emergency-apply" and args.approve_hash) or (
        args.pr is not None and (not emergency or args.pr <= 0)
    ):
        parser.error("emergency closeout requires an exact request and apply approval hash")
    product = Path(os.environ["FACTORY_ROOT"]).resolve()
    workdir = Path(args.workdir).resolve()
    remote = os.environ.get("FACTORY_CERTIFIED_PRODUCT_ORIGIN", "")
    kit_sha = os.environ.get("FACTORY_RELEASE_SHA", "")
    if not remote or not re.fullmatch(r"[0-9a-f]{40}", kit_sha):
        raise Refusal("trusted launcher evidence is unavailable")
    repo, prefix, checks, method = parse_project(product / "factory" / "PROJECT.env")
    if args.action == "emergency-plan":
        result = emergency_preview(
            args, product, workdir, repo, prefix, checks, kit_sha, method,
        )
    elif args.action == "emergency-apply":
        retry = (
            git(workdir, "rev-parse", "HEAD").stdout.strip()
            != git(workdir, "rev-parse", "origin/main").stdout.strip()
            and (workdir / "factory" / "attestations" / args.ticket / "done.json").is_file()
        )
        request = emergency_request(args.request, require_current=not retry)
        preview = None if retry else emergency_preview(
            args, product, workdir, repo, prefix, checks, kit_sha, method,
        )
        result = emergency_done(
            args, product, workdir, repo, prefix, remote, checks, kit_sha,
            method, preview, request,
        )
    elif args.action == "bundle":
        result = bundle(args, product, workdir, repo, prefix, remote, kit_sha)
    elif args.action == "approval":
        result = approval(
            args, product, workdir, repo, prefix, remote, kit_sha, method,
        )
    elif args.action == "refresh":
        result = refresh(args, product, workdir, repo, prefix, remote)
    elif args.action == "dependency-refresh":
        result = dependency_refresh(
            args, product, workdir, prefix, remote,
        )
    else:
        result = done(
            args, product, workdir, repo, prefix, remote, checks, kit_sha, method,
        )
    print(json.dumps({"status": "ok", "ticket": args.ticket, **result}, indent=2, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except (KeyError, OSError, json.JSONDecodeError, Refusal) as error:
        print(f"ticket-attest: {error}", file=sys.stderr)
        raise SystemExit(1)
