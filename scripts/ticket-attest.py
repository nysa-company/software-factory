#!/usr/bin/env python3
"""Evidence-bound ticket approval, protected auto-merge, and closeout."""

import argparse
import base64
import csv
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
from urllib.parse import quote


class Refusal(ValueError):
    pass


ROLES = ("planner", "spec-linter", "test-author", "builder", "reviewer", "narrator")


def run(argv, *, cwd=None, input_text=None, check=True):
    result = subprocess.run(
        argv, cwd=cwd, input=input_text, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if check and result.returncode:
        raise Refusal(result.stderr.strip() or result.stdout.strip() or f"{argv[0]} failed")
    return result


def git(root, *args, check=True):
    result = run(["git", "-C", str(root), *args], check=False)
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


def successful_runs(product, ticket):
    manifests = []
    runs = product / "factory" / "runs"
    if not runs.is_dir() or runs.is_symlink():
        raise Refusal("authoritative run manifest directory is missing")
    for path in sorted(runs.glob("*.meta")):
        if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
            raise Refusal("run manifests must be regular single-link files")
        value = meta(path)
        if (
            value.get("ticket") == ticket
            and value.get("accounting_state") == "completed"
            and value.get("exit_status") == "0"
        ):
            value["_manifest_name"] = path.name
            value["_manifest_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
            manifests.append(value)
    ledger = product / "factory" / "runtime-ledger.csv"
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
    return manifests


def route_revision_hash(index, parent, body):
    return hashlib.sha256(json.dumps(
        {"body": body, "parent_hash": parent, "revision": index},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


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
        for index, revision_value in enumerate(plan.get("revisions", [])):
            body = revision_value.get("body") if isinstance(revision_value, dict) else None
            expected = route_revision_hash(index, parent, body)
            if (
                set(revision_value) != {"revision", "parent_hash", "body", "revision_hash"}
                or revision_value.get("revision") != index
                or revision_value.get("parent_hash") != parent
                or revision_value.get("revision_hash") != expected
            ):
                raise Refusal("ticket route journal hash chain is invalid")
            if index == 0 and body.get("kind") == "migration":
                try:
                    legacy_raw = base64.b64decode(body["legacy_plan_b64"], validate=True)
                    legacy = json.loads(legacy_raw)
                except (KeyError, ValueError, UnicodeError, json.JSONDecodeError):
                    raise Refusal("ticket route migration provenance is malformed")
                if (
                    hashlib.sha256(legacy_raw).hexdigest() != body.get("legacy_plan_sha256")
                    or legacy.get("ticket") != ticket
                    or legacy.get("resolution", {}).get("policy_hash") != body.get("policy_hash")
                ):
                    raise Refusal("ticket route migration provenance does not match")
                resolution = legacy["resolution"]
            elif index > 0 and body.get("kind") == "fallback":
                if body.get("prior_resolution") != resolution:
                    raise Refusal("fallback revision does not extend the prior resolution")
                resolution = body.get("new_resolution")
                failed_digests.add(body.get("failed_manifest_digest"))
            else:
                raise Refusal("ticket route journal revision kind is invalid")
            prefix = dict(plan)
            prefix["revisions"] = plan["revisions"][:index + 1]
            prefix_raw = (
                json.dumps(prefix, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                + "\n"
            ).encode("utf-8")
            revisions[index] = (revision_value["revision_hash"], resolution, prefix_raw)
            parent = revision_value["revision_hash"]
        if resolution is None:
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
                revision_hash, selected_resolution, revision_raw = revisions[number]
            except (ValueError, KeyError):
                raise Refusal("successful run references an unknown route revision")
            if manifest.get("route_revision_hash") != revision_hash:
                raise Refusal("successful run route revision hash does not match")
            expected_digest = hashlib.sha256(revision_raw).hexdigest()
            expected_kit = kit_sha
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


def validate_refresh_review_evidence(workdir, ticket, text, manifests, reviewer, narrator):
    relative = f"factory/attestations/{ticket}/refresh.json"
    path = workdir / relative
    if not os.path.lexists(path):
        historical = git(
            workdir, "log", "-1", "--format=%H", "HEAD", "--", relative,
        ).stdout.strip()
        if historical:
            raise Refusal("committed refresh receipt is missing from the ticket head")
        return
    if not safe_optional_attestation(path):
        raise Refusal("refresh receipt is unsafe")

    def unique_object(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate key")
            value[key] = item
        return value

    try:
        receipt = json.loads(path.read_text(), object_pairs_hook=unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise Refusal("refresh receipt is malformed")
    expected = {
        "schema", "ticket", "generation", "old_head", "base_head", "merge_head",
        "prior_reviewer_runs", "prior_approve_verdicts",
        "prior_request_changes_verdicts", "prior_narrator_runs",
        "prior_bundle_blob", "prior_approval_blob", "refreshed_at",
    }
    counts = [receipt.get(name) for name in (
        "prior_reviewer_runs", "prior_approve_verdicts",
        "prior_request_changes_verdicts", "prior_narrator_runs",
    )]
    if (
        set(receipt) != expected
        or receipt.get("schema") != "nysa.software-factory.ticket-refresh/v1"
        or receipt.get("ticket") != ticket
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
    old_raw_reviewers = len(old_verdicts) + len(old_voids)
    if (
        len(current_verdicts) <= len(old_verdicts)
        or any(value <= old_raw_reviewers for value in current_voids[len(old_voids):])
    ):
        raise Refusal("a new post-refresh Reviewer verdict is required")
    reviewers = sorted(
        (item for item in manifests if item.get("role") == "reviewer"),
        key=lambda item: item["_ledger_index"],
    )
    try:
        reviewer_ordinal = reviewers.index(reviewer) + 1
    except ValueError:
        raise Refusal("post-refresh Reviewer evidence is missing")
    if reviewer_ordinal <= old_raw_reviewers:
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
    base_keys = {
        "schema", "ticket", "repository", "branch", "branch_head",
        "reviewed_sha", "bundle_path", "bundle_blob", "pr_number", "pr_url",
        "reviewer_run_id", "narrator_run_id", "kit_sha", "policy_hash",
        "route_plan_path", "route_plan_blob", "route_plan_sha256", "attested_at",
    }
    schema = value.get("schema")
    if schema == "nysa.software-factory.ticket-bundle/v1":
        expected_keys = base_keys
        legacy_digest_valid = "legacy_planner_manifest_sha256" not in value
    elif schema == "nysa.software-factory.ticket-bundle/v2":
        expected_keys = base_keys | {"legacy_planner_manifest_sha256"}
        legacy_digest_valid = bool(re.fullmatch(
            r"[0-9a-f]{64}", value.get("legacy_planner_manifest_sha256", ""),
        ))
    else:
        expected_keys = set()
        legacy_digest_valid = False
    if (
        set(value) != expected_keys
        or not legacy_digest_valid
        or value.get("ticket") != ticket
        or value.get("repository") != repo
        or value.get("branch") != branch
        or value.get("kit_sha") != kit_sha
        or value.get("route_plan_path") != f"factory/route-plans/{ticket}.json"
        or not valid_oid(value.get("route_plan_blob"))
        or not re.fullmatch(r"[0-9a-f]{64}", value.get("route_plan_sha256", ""))
        or not re.fullmatch(r"[0-9a-f]{64}", value.get("policy_hash", ""))
        or not isinstance(value.get("pr_number"), int)
        or value["pr_number"] <= 0
        or not isinstance(value.get("pr_url"), str)
        or not value["pr_url"]
        or not all(valid_oid(value.get(key)) for key in (
            "branch_head", "reviewed_sha", "bundle_blob",
        ))
        or value.get("bundle_path") != f"factory/tickets/{ticket}-bundle.md"
        or not isinstance(value.get("reviewer_run_id"), str)
        or not value["reviewer_run_id"]
        or not isinstance(value.get("narrator_run_id"), str)
        or not value["narrator_run_id"]
        or blob_at(
            workdir, value.get("branch_head", ""), value.get("route_plan_path", ""),
        ) != value.get("route_plan_blob")
        or hashlib.sha256(git(
            workdir, "show",
            f"{value.get('branch_head', '')}:{value.get('route_plan_path', '')}",
        ).stdout.encode()).hexdigest() != value.get("route_plan_sha256")
    ):
        raise Refusal("bundle attestation identity or evidence is invalid")
    timestamp(value.get("attested_at"), "bundle attestation")
    return value


def validate_bundle_commit(workdir, ticket, value, bundle_commit):
    receipt_path = f"factory/attestations/{ticket}/bundle.json"
    bundle_path = f"factory/tickets/{ticket}-bundle.md"
    expected_paths = {
        f"factory/tickets/{ticket}.md",
        receipt_path,
    }
    parent = git(workdir, "rev-parse", f"{bundle_commit}^").stdout.strip()
    actual_paths = set(git(
        workdir, "diff-tree", "--no-commit-id", "--name-only", "-r",
        bundle_commit,
    ).stdout.splitlines())
    if (
        parent != value["branch_head"]
        or actual_paths != expected_paths
        or blob_at(workdir, bundle_commit, bundle_path) != value["bundle_blob"]
    ):
        raise Refusal("bundle attestation commit or reviewed branch evidence is invalid")


def validate_approval_attestation(
    value, bundle_att, ticket, repo, branch, kit_sha, method, workdir, head,
):
    bundle_path = f"factory/attestations/{ticket}/bundle.json"
    expected_paths = {
        f"factory/tickets/{ticket}.md",
        f"factory/attestations/{ticket}/approval.json",
    }
    expected_keys = {
        "schema", "ticket", "repository", "branch", "parent_head",
        "reviewed_sha", "bundle_blob", "bundle_attestation_blob", "pr_number",
        "operator_version", "linear_updated_at", "observed_at", "kit_sha",
        "auto_merge_method", "attested_at",
    }
    parent = git(workdir, "rev-parse", f"{head}^").stdout.strip()
    validate_bundle_commit(workdir, ticket, bundle_att, parent)
    actual_paths = set(git(
        workdir, "diff-tree", "--no-commit-id", "--name-only", "-r", head,
    ).stdout.splitlines())
    if (
        set(value) != expected_keys
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
        or not re.fullmatch(r"[0-9a-f]{64}", value.get("operator_version", ""))
        or timestamp(value.get("observed_at"), "approval observation")
        <= timestamp(bundle_att.get("attested_at"), "bundle attestation")
        or timestamp(value.get("linear_updated_at"), "Linear approval update")
        <= timestamp(bundle_att.get("attested_at"), "bundle attestation")
        or value.get("parent_head") != parent
        or parent != git(workdir, "rev-parse", "HEAD^").stdout.strip()
        or value.get("bundle_attestation_blob") != blob_at(
            workdir, parent, bundle_path,
        )
        or value["bundle_attestation_blob"] != blob_id(
            workdir, workdir / bundle_path,
        )
        or actual_paths != expected_paths
    ):
        raise Refusal("existing approval attestation or approval commit is invalid")
    return value


def protected_approval_evidence(
    workdir, ticket, repo, branch, kit_sha, method, pr,
):
    root = workdir / "factory" / "attestations" / ticket
    bundle_path = root / "bundle.json"
    approval_path = root / "approval.json"
    if not bundle_path.is_file() or not approval_path.is_file():
        raise Refusal("protected main lacks bundle or approval attestation")
    bundle_att = validate_bundle_attestation(
        json.loads(bundle_path.read_text()), ticket, repo, branch, kit_sha, workdir,
    )
    approval_att = json.loads(approval_path.read_text())
    approval_keys = {
        "schema", "ticket", "repository", "branch", "parent_head",
        "reviewed_sha", "bundle_blob", "bundle_attestation_blob", "pr_number",
        "operator_version", "linear_updated_at", "observed_at", "kit_sha",
        "auto_merge_method", "attested_at",
    }
    approval_head = pr.get("headRefOid", "")
    if not valid_oid(approval_head):
        raise Refusal("merged PR lacks the approved head commit")
    parent = git(workdir, "rev-parse", f"{approval_head}^").stdout.strip()
    validate_bundle_commit(workdir, ticket, bundle_att, parent)
    expected_paths = {
        f"factory/tickets/{ticket}.md",
        f"factory/attestations/{ticket}/approval.json",
    }
    actual_paths = set(git(
        workdir, "diff-tree", "--no-commit-id", "--name-only", "-r",
        approval_head,
    ).stdout.splitlines())
    relative_bundle = f"factory/attestations/{ticket}/bundle.json"
    relative_approval = f"factory/attestations/{ticket}/approval.json"
    bundle_attestation_blob = blob_id(workdir, bundle_path)
    approval_attestation_blob = blob_id(workdir, approval_path)
    observed = timestamp(approval_att.get("observed_at"), "approval observation")
    updated = timestamp(
        approval_att.get("linear_updated_at"), "Linear approval update",
    )
    bundle_time = timestamp(bundle_att.get("attested_at"), "bundle attestation")
    if (
        set(approval_att) != approval_keys
        or approval_att.get("schema") != "nysa.software-factory.ticket-approval/v1"
        or approval_att.get("ticket") != ticket
        or approval_att.get("repository") != repo
        or approval_att.get("branch") != branch
        or approval_att.get("pr_number") != pr.get("number")
        or approval_att.get("reviewed_sha") != bundle_att["reviewed_sha"]
        or approval_att.get("bundle_blob") != bundle_att["bundle_blob"]
        or approval_att.get("bundle_attestation_blob") != bundle_attestation_blob
        or approval_att.get("kit_sha") != kit_sha
        or approval_att.get("auto_merge_method") != method
        or approval_att.get("attested_at") != approval_att.get("observed_at")
        or observed <= bundle_time
        or updated <= bundle_time
        or not re.fullmatch(
            r"[0-9a-f]{64}", approval_att.get("operator_version", ""),
        )
        or approval_att.get("parent_head") != parent
        or actual_paths != expected_paths
        or blob_at(workdir, approval_head, relative_bundle)
        != bundle_attestation_blob
        or blob_at(workdir, approval_head, relative_approval)
        != approval_attestation_blob
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
            statuses[name] = status.get("state") == "success"
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
            passed = statuses[name]
        elif len(check_runs) == 1:
            item = check_runs[0]
            passed = (
                item.get("status") == "completed"
                and item.get("conclusion") == "success"
            )
        else:
            passed = False
        if not passed:
            raise Refusal(f"required post-merge check is missing or unsuccessful: {name}")
        successful.append(name)
    return successful


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


def consume_overlay(product, ticket, expected_version):
    path = product / "factory" / "linear-map.json"
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
    path = product / "factory" / "linear-map.json"
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
    path = product / "factory" / "linear-map.json"
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
    if field(text, "State").lower() not in {"review", "awaiting approval", "approved"}:
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
        or view.get("mergeStateStatus") not in {"BEHIND", "BLOCKED", "DIRTY"}
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

    manifests = successful_runs(product, args.ticket)
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
        previous = json.loads(previous_path.read_text())
        expected_refresh_keys = {
            "schema", "ticket", "generation", "old_head", "base_head", "merge_head",
            "prior_reviewer_runs", "prior_approve_verdicts",
            "prior_request_changes_verdicts", "prior_narrator_runs",
            "prior_bundle_blob", "prior_approval_blob", "refreshed_at",
        }
        previous_generation = previous.get("generation")
        if (
            set(previous) != expected_refresh_keys
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


def bundle(args, product, workdir, repo, prefix, remote, kit_sha):
    branch = f"{prefix}{args.ticket}"
    head = ensure_clean_branch(product, workdir, branch)
    ticket_path = workdir / "factory" / "tickets" / f"{args.ticket}.md"
    bundle_path = workdir / "factory" / "tickets" / f"{args.ticket}-bundle.md"
    text = ticket_path.read_text()
    if field(text, "State").lower() != "review":
        raise Refusal("bundle requires ticket State Review")
    bundle_text = bundle_path.read_text()
    required = (
        "What this does", "Preview", "Screenshots", "Acceptance criteria",
        "Risk", "Cost", "Rollback",
    )
    if any(not re.search(rf"^#+\s+.*{re.escape(section)}", bundle_text, re.I | re.M) for section in required):
        raise Refusal("evidence bundle is missing a required section")
    if not re.search(r"approve to merge", bundle_text, re.I):
        raise Refusal("evidence bundle lacks the operator approval question")
    manifests = successful_runs(product, args.ticket)
    route_plan = route_plan_evidence(workdir, product, args.ticket, kit_sha, manifests)
    reviewer, narrator, reviewed = review_evidence(text, manifests, workdir)
    validate_refresh_review_evidence(
        workdir, args.ticket, text, manifests, reviewer, narrator,
    )
    allowed = {
        f"factory/tickets/{args.ticket}.md",
        f"factory/tickets/{args.ticket}-bundle.md",
    }
    changed = set(git(workdir, "diff", "--name-only", f"{reviewed}..{head}").stdout.splitlines())
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
    branch = f"{prefix}{args.ticket}"
    head = ensure_clean_branch(product, workdir, branch)
    ticket_path = workdir / "factory" / "tickets" / f"{args.ticket}.md"
    bundle_path = workdir / "factory" / "tickets" / f"{args.ticket}-bundle.md"
    attestation_path = workdir / "factory" / "attestations" / args.ticket / "bundle.json"
    approval_path = attestation_path.with_name("approval.json")
    bundle_att = validate_bundle_attestation(
        json.loads(attestation_path.read_text()), args.ticket, repo, branch, kit_sha,
        workdir,
    )
    if git(workdir, "hash-object", str(bundle_path)).stdout.strip() != bundle_att.get("bundle_blob"):
        raise Refusal("evidence bundle changed after attestation")
    mapping = json.loads((product / "factory" / "linear-map.json").read_text())
    operator = mapping.get("tickets", {}).get(args.ticket, {}).get("operator") or {}
    existing_approval = json.loads(approval_path.read_text()) if approval_path.exists() else None
    exact_overlay = (
        operator.get("state") == "Approved"
        and operator.get("approval") == "Linear"
        and operator.get("state_base") == "awaiting approval"
    )
    if not exact_overlay:
        raise Refusal("exact Linear Awaiting Approval -> Approved overlay is required")
    observed = timestamp(operator.get("observed_at"), "approval observation")
    updated = timestamp(operator.get("linear_updated_at"), "Linear approval update")
    attested = timestamp(bundle_att.get("attested_at"), "bundle attestation")
    if observed <= attested or updated <= attested:
        raise Refusal("Linear approval is not newer than the bundle attestation")
    version = hashlib.sha256(json.dumps(
        {key: operator[key] for key in ("priority", "initiative", "state", "approval") if key in operator},
        sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    pr = exact_pr(repo, branch, "open")
    if pr.get("number") != bundle_att.get("pr_number") or pr.get("headRefOid") != head:
        raise Refusal("PR identity or head changed before approval")
    if existing_approval:
        approval_att = validate_approval_attestation(
            existing_approval, bundle_att, args.ticket, repo, branch, kit_sha,
            method, workdir, head,
        )
        if approval_att.get("operator_version") != version:
            raise Refusal("existing approval attestation does not match the overlay")
    else:
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
    consume_overlay(product, args.ticket, version)
    return {"action": "approval", "head": head, "pr_number": current["number"], "auto_merge": True}


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
    pr = exact_pr(repo, ticket_branch, "all")
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
    return {
        "action": "done",
        "head": head,
        "attestation": done_att,
        "closeout_pr_number": closeout_pr["number"],
        "closeout_pr_state": closeout_pr["state"],
        "auto_merge": closeout_pr["state"] != "MERGED",
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ticket", required=True)
    parser.add_argument("--workdir", required=True)
    parser.add_argument(
        "--action", choices=("bundle", "approval", "refresh", "done"), required=True,
    )
    args = parser.parse_args()
    if not re.fullmatch(r"T-\d+", args.ticket):
        parser.error("invalid ticket identifier")
    product = Path(os.environ["FACTORY_ROOT"]).resolve()
    workdir = Path(args.workdir).resolve()
    remote = os.environ.get("FACTORY_CERTIFIED_PRODUCT_ORIGIN", "")
    kit_sha = os.environ.get("FACTORY_RELEASE_SHA", "")
    if not remote or not re.fullmatch(r"[0-9a-f]{40}", kit_sha):
        raise Refusal("trusted launcher evidence is unavailable")
    repo, prefix, checks, method = parse_project(product / "factory" / "PROJECT.env")
    if args.action == "bundle":
        result = bundle(args, product, workdir, repo, prefix, remote, kit_sha)
    elif args.action == "approval":
        result = approval(
            args, product, workdir, repo, prefix, remote, kit_sha, method,
        )
    elif args.action == "refresh":
        result = refresh(args, product, workdir, repo, prefix, remote)
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
