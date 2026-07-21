#!/usr/bin/env python3
"""Generate a one-time protected-merge reconciliation product change."""

import argparse
import csv
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))

from legacy_closeout import ValidationError, exact, oid, timestamp  # noqa: E402
from protected_merge_reconciliation import (  # noqa: E402
    AUTH_SCHEMA,
    CLASSIFICATIONS,
    MIGRATION_DIR,
    RECEIPT_SCHEMA,
    terminal_projection,
    validate_generated_reconciliation_batch,
)


REQUEST_SCHEMA = "nysa.software-factory.protected-merge-reconciliation-request/v1"
REQUEST_KEYS = {
    "schema", "repository", "basis_kit_sha", "target_kit_sha",
    "candidate_contract", "cutoff", "protected_main_basis",
    "required_checks", "authorization", "companions", "tickets",
}
REQUEST_TICKET_KEYS = {
    "ticket", "classification", "original_pr_number", "adoption_pr_number",
    "evidence_head", "paths",
}


def command(argv, *, cwd=None, input_text=None, env=None, check=True):
    result = subprocess.run(argv, cwd=cwd, input=input_text, capture_output=True, text=True, env=env)
    if check and result.returncode:
        raise ValidationError(result.stderr.strip() or "command failed")
    return result


def git(repo, *args, **kwargs):
    return command(["git", "-C", str(repo), *args], **kwargs)


def gh_json(*args):
    result = command(["gh", "api", "-H", "Accept: application/vnd.github+json", *args])
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise ValidationError("GitHub returned invalid JSON") from error


def canonical(value):
    return json.dumps(value, indent=2, sort_keys=True) + "\n"


def read_json(path, label):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ValidationError(f"{label} is unavailable or invalid") from error


def source_text(repo, ref, path):
    result = git(repo, "show", f"{ref}:{path}", check=False)
    if result.returncode:
        raise ValidationError(f"source evidence is missing: {path}")
    return result.stdout


def source_blob(repo, ref, path):
    return git(repo, "rev-parse", f"{ref}:{path}").stdout.strip()


def field(text, name):
    values = re.findall(rf"(?mi)^{re.escape(name)}:\s*(.*?)\s*$", text)
    if len(values) != 1:
        raise ValidationError(f"ticket must contain exactly one {name}")
    return values[0].strip()


def project_value(repo, ref, name):
    text = source_text(repo, ref, "factory/PROJECT.env")
    values = []
    for raw in text.splitlines():
        match = re.fullmatch(rf"\s*(?:export\s+)?{name}\s*=\s*(.*?)\s*", raw)
        if match:
            value = match.group(1)
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            values.append(value)
    if len(values) != 1:
        raise ValidationError(f"factory/PROJECT.env must define one {name}")
    return values[0]


def pr_evidence(repo, repository, number, basis, cutoff, label):
    pr = gh_json(f"repos/{repository}/pulls/{number}")
    if pr.get("number") != number or pr.get("state") != "closed" or pr.get("base", {}).get("ref") != "main":
        raise ValidationError(f"{label} is not an exact closed PR to main")
    head = pr.get("head", {}).get("sha")
    oid(head, f"{label} head")
    merged = pr.get("merged") is True
    merge_commit = pr.get("merge_commit_sha") if merged else None
    merged_at = pr.get("merged_at") if merged else None
    merged_by = (pr.get("merged_by") or {}).get("login") if merged else None
    if merged:
        oid(merge_commit, f"{label} merge commit")
        if (
            timestamp(merged_at, f"{label} merged_at") > cutoff
            or git(repo, "merge-base", "--is-ancestor", merge_commit, basis, check=False).returncode
        ):
            raise ValidationError(f"{label} merge is outside the protected basis")
    return {
        "number": number,
        "head_ref": pr.get("head", {}).get("ref"),
        "base_ref": "main",
        "head": head,
        "merged": merged,
        "merge_commit": merge_commit,
        "merged_at": merged_at,
        "merged_by": merged_by,
    }


def check_evidence(repository, commit, expected):
    response = gh_json(f"repos/{repository}/commits/{commit}/check-runs?per_page=100")
    runs = response.get("check_runs")
    if not isinstance(runs, list) or response.get("total_count") != len(runs) or len(runs) >= 100:
        raise ValidationError("GitHub check evidence is incomplete or requires pagination")
    statuses = gh_json(f"repos/{repository}/commits/{commit}/status?per_page=100").get("statuses")
    if not isinstance(statuses, list) or len(statuses) >= 100:
        raise ValidationError("GitHub status evidence is incomplete")
    if any(item.get("context") in expected for item in statuses):
        raise ValidationError("required check name is ambiguous across checks and statuses")
    result = []
    for name in sorted(expected):
        matches = [item for item in runs if item.get("name") == name]
        if len(matches) != 1:
            raise ValidationError(f"required check is missing or ambiguous: {name}")
        item = matches[0]
        app = item.get("app") or {}
        identity = expected[name]
        if (
            app.get("id") != identity["app_id"]
            or app.get("slug") != identity["app_slug"]
            or item.get("status") != "completed"
            or item.get("conclusion") != "success"
        ):
            raise ValidationError(f"required check is skipped, unsuccessful, or from the wrong app: {name}")
        result.append({
            "name": name,
            "app_id": app["id"],
            "app_slug": app["slug"],
            "status": "completed",
            "conclusion": "success",
            "skipped": False,
        })
    return result


def ledger_evidence(repo, basis, ticket, reviewer_run_id=None, narrator_run_id=None):
    text = source_text(repo, basis, "factory/ledger.csv")
    rows = [row for row in csv.DictReader(io.StringIO(text)) if row.get("ticket") == ticket]
    run_ids = [row.get("run_id") for row in rows]
    successful = {(row.get("run_id"), row.get("role")) for row in rows if row.get("exit_status") == "0"}
    if reviewer_run_id is None:
        reviewer_run_id = next((row.get("run_id") for row in reversed(rows) if row.get("role") == "reviewer" and row.get("exit_status") == "0"), None)
    if narrator_run_id is None:
        narrator_run_id = next((row.get("run_id") for row in reversed(rows) if row.get("role") == "narrator" and row.get("exit_status") == "0"), None)
    if (
        not rows
        or any(not item for item in run_ids)
        or len(run_ids) != len(set(run_ids))
        or (reviewer_run_id, "reviewer") not in successful
        or (narrator_run_id, "narrator") not in successful
    ):
        raise ValidationError(f"{ticket} lacks complete settled Reviewer/Narrator accounting")
    return {
        "sha256": hashlib.sha256(text.encode()).hexdigest(),
        "run_ids": run_ids,
        "reviewer_run_id": reviewer_run_id,
        "narrator_run_id": narrator_run_id,
    }


def temporary_candidate(repo, basis, files, cutoff):
    with tempfile.TemporaryDirectory(prefix="protected-merge-reconciliation.") as temp:
        env = dict(os.environ, GIT_INDEX_FILE=str(Path(temp) / "index"))
        git(repo, "read-tree", basis, env=env)
        for relative, content in files.items():
            blob = git(repo, "hash-object", "-w", "--stdin", input_text=content).stdout.strip()
            git(repo, "update-index", "--add", "--cacheinfo", "100644", blob, relative, env=env)
        tree = git(repo, "write-tree", env=env).stdout.strip()
        commit_env = dict(
            os.environ,
            GIT_AUTHOR_NAME="Protected Merge Reconciliation Validator",
            GIT_AUTHOR_EMAIL="factory@invalid",
            GIT_COMMITTER_NAME="Protected Merge Reconciliation Validator",
            GIT_COMMITTER_EMAIL="factory@invalid",
            GIT_AUTHOR_DATE=cutoff,
            GIT_COMMITTER_DATE=cutoff,
        )
        return git(repo, "commit-tree", tree, "-p", basis, input_text="validate reconciliation\n", env=commit_env).stdout.strip()


def write_if_changed(path, content, *, immutable=False):
    if path.exists() and path.is_symlink():
        raise ValidationError(f"refusing symlink output: {path}")
    if path.exists() and path.read_text() == content:
        return False
    if path.exists() and immutable:
        raise ValidationError(f"existing migration evidence conflicts: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return True


def generate(product, request_path):
    repo = Path(git(product, "rev-parse", "--show-toplevel").stdout.strip()).resolve()
    if product.resolve() != repo:
        raise ValidationError("--product must be the repository root")
    request = exact(read_json(request_path, "reconciliation request"), REQUEST_KEYS, "reconciliation request")
    if request["schema"] != REQUEST_SCHEMA:
        raise ValidationError("reconciliation request schema is invalid")
    basis = exact(request["protected_main_basis"], {"commit", "tree"}, "request basis")
    basis_commit = oid(basis["commit"], "request basis commit")
    oid(basis["tree"], "request basis tree")
    head = git(repo, "rev-parse", "HEAD").stdout.strip()
    if (
        git(repo, "rev-parse", "refs/remotes/origin/main").stdout.strip() != basis_commit
        or git(repo, "rev-parse", f"{basis_commit}^{{tree}}").stdout.strip() != basis["tree"]
        or git(repo, "merge-base", "--is-ancestor", basis_commit, head, check=False).returncode
    ):
        raise ValidationError("origin/main must match the basis and prep HEAD must descend from it")
    if project_value(repo, basis_commit, "GH_REPO") != request["repository"]:
        raise ValidationError("request repository does not match product configuration")
    if source_text(repo, basis_commit, "factory/KIT_PIN") != request["basis_kit_sha"] + "\n":
        raise ValidationError("request basis kit does not match protected main")
    oid(request["basis_kit_sha"], "request basis kit")
    oid(request["target_kit_sha"], "request target kit")
    if request["basis_kit_sha"] == request["target_kit_sha"] or request["candidate_contract"] != "1.6.0":
        raise ValidationError("reconciliation must target a different Contract 1.6 kit")
    cutoff = timestamp(request["cutoff"], "request cutoff")
    if cutoff.microsecond or cutoff > datetime.now(timezone.utc):
        raise ValidationError("request cutoff must be a whole-second time that is not in the future")
    approval = exact(request["authorization"], {
        "method", "operator", "authorized_at", "statement", "auto_merge", "bypass",
    }, "request authorization")
    if approval["authorized_at"] != request["cutoff"]:
        raise ValidationError("operator authorization must define the exact cutoff")
    configured_names = project_value(repo, basis_commit, "DONE_REQUIRED_CHECKS").split(",")
    required = request["required_checks"]
    if not isinstance(required, list):
        raise ValidationError("request required_checks must be a list")
    expected_checks = {}
    for item in required:
        exact(item, {"name", "app_id", "app_slug"}, "request check identity")
        if item["name"] in expected_checks:
            raise ValidationError("request check identity is duplicated")
        expected_checks[item["name"]] = item
    if set(expected_checks) != set(configured_names):
        raise ValidationError("request checks do not match DONE_REQUIRED_CHECKS")
    ticket_requests = request["tickets"]
    if not isinstance(ticket_requests, list) or not ticket_requests:
        raise ValidationError("request ticket batch is empty")
    companions = request["companions"]
    if not isinstance(companions, list):
        raise ValidationError("request companions must be a list")
    companion_paths = []
    companion_files = {}
    for companion in companions:
        exact(companion, {"path", "blob"}, "request companion")
        path = companion["path"]
        if (
            not isinstance(path, str)
            or not path
            or path.startswith("/")
            or ".." in Path(path).parts
            or path == "factory/KIT_PIN"
            or path.startswith(MIGRATION_DIR + "/")
        ):
            raise ValidationError("request companion path is reserved or invalid")
        oid(companion["blob"], "request companion blob")
        companion_paths.append(path)
        content = source_text(repo, head, path)
        if source_blob(repo, head, path) != companion["blob"]:
            raise ValidationError("request companion blob does not match prep HEAD")
        companion_files[path] = content
    if companion_paths != sorted(companion_paths) or len(companion_paths) != len(set(companion_paths)):
        raise ValidationError("request companions must be sorted and unique")
    reserved_ticket_paths = {
        f"factory/tickets/{item.get('ticket')}.md"
        for item in ticket_requests if isinstance(item, dict)
    }
    if set(companion_paths) & reserved_ticket_paths:
        raise ValidationError("request companion collides with a terminal ticket")
    changed = git(repo, "diff", "--name-only", basis_commit, head).stdout.splitlines()
    if changed != companion_paths:
        raise ValidationError("prep HEAD differs from the basis outside exact companions")
    receipts = {}
    terminals = {}
    auth_entries = []
    seen = set()
    for item in sorted(ticket_requests, key=lambda value: value.get("ticket", "")):
        exact(item, REQUEST_TICKET_KEYS, "request ticket")
        ticket = item["ticket"]
        if not re.fullmatch(r"T-[0-9]+", ticket) or ticket in seen:
            raise ValidationError("request ticket IDs are invalid or duplicated")
        seen.add(ticket)
        classification = item["classification"]
        if classification not in CLASSIFICATIONS:
            raise ValidationError(f"{ticket} classification is invalid")
        evidence = oid(item["evidence_head"], f"{ticket} evidence head")
        if timestamp(git(repo, "show", "-s", "--format=%cI", evidence).stdout.strip(), f"{ticket} evidence time") > cutoff:
            raise ValidationError(f"{ticket} evidence is newer than authorization")
        original = pr_evidence(repo, request["repository"], item["original_pr_number"], basis_commit, cutoff, f"{ticket} original PR")
        adoption = pr_evidence(repo, request["repository"], item["adoption_pr_number"], basis_commit, cutoff, f"{ticket} adoption PR")
        if (
            classification == "reviewed-clean-history-adoption"
            and (original["merged"] or not adoption["merged"] or original["number"] == adoption["number"])
        ) or (
            classification == "merged-adoption"
            and (not original["merged"] or original != adoption)
        ):
            raise ValidationError(f"{ticket} PR topology does not match classification")
        if git(repo, "merge-base", "--is-ancestor", evidence, original["head"], check=False).returncode:
            raise ValidationError(f"{ticket} evidence head is outside the original PR")
        ticket_path = f"factory/tickets/{ticket}.md"
        bundle_path = f"factory/tickets/{ticket}-bundle.md"
        route_path = f"factory/route-plans/{ticket}.json"
        bundle_attestation_path = f"factory/attestations/{ticket}/bundle.json"
        approval_path = f"factory/attestations/{ticket}/approval.json"
        ticket_text = source_text(repo, evidence, ticket_path)
        source_state = field(ticket_text, "State")
        if source_state not in {"Ready", "Review", "Awaiting Approval", "Approved"}:
            raise ValidationError(f"{ticket} evidence state is not reconcilable")
        source_kit = field(ticket_text, "Kit-SHA")
        oid(source_kit, f"{ticket} source kit")
        legacy = classification == "reviewed-clean-history-adoption"
        if legacy:
            if source_state != "Ready":
                raise ValidationError(f"{ticket} clean-history adoption requires the bounded legacy Ready shape")
            parents = git(repo, "show", "-s", "--format=%P", evidence).stdout.split()
            if len(parents) != 1:
                raise ValidationError(f"{ticket} legacy bundle commit is not single-parent")
            verdict_commit = parents[0]
            reviewed = git(repo, "show", "-s", "--format=%P", verdict_commit).stdout.split()
            if len(reviewed) != 1:
                raise ValidationError(f"{ticket} legacy verdict commit is not single-parent")
            legacy_review = {"reviewed_sha": reviewed[0], "verdict_commit": verdict_commit}
            bundle = None
        else:
            bundle = read_json_file_at(repo, evidence, bundle_attestation_path, f"{ticket} bundle attestation")
            legacy_review = None
        approval_blob = git(repo, "rev-parse", f"{evidence}:{approval_path}", check=False)
        approval_oid = approval_blob.stdout.strip() if approval_blob.returncode == 0 else None
        if (approval_oid is not None) is not (source_state == "Approved"):
            raise ValidationError(f"{ticket} approval evidence does not match source state")
        paths = item["paths"]
        if not isinstance(paths, list) or not paths or paths != sorted(paths) or len(paths) != len(set(paths)):
            raise ValidationError(f"{ticket} adopted paths must be sorted and unique")
        path_evidence = []
        for path in paths:
            if not isinstance(path, str) or path.startswith("/") or path.startswith("factory/") or ".." in Path(path).parts:
                raise ValidationError(f"{ticket} adopted path is invalid")
            reviewed_source = legacy_review["reviewed_sha"] if legacy else bundle["reviewed_sha"]
            blobs = [
                source_blob(repo, ref, path)
                for ref in (reviewed_source, evidence, adoption["head"], basis_commit)
            ]
            if len(set(blobs)) != 1:
                raise ValidationError(f"{ticket} adopted product/test path changed")
            path_evidence.append({"path": path, "blob": blobs[0]})
        route_text = source_text(repo, evidence, route_path)
        receipt = {
            "schema": RECEIPT_SCHEMA,
            "ticket": ticket,
            "repository": request["repository"],
            "classification": classification,
            "source_state": source_state,
            "source_kit_sha": source_kit,
            "basis_kit_sha": request["basis_kit_sha"],
            "target_kit_sha": request["target_kit_sha"],
            "candidate_contract": request["candidate_contract"],
            "evidence_head": evidence,
            "source_ticket_blob": source_blob(repo, evidence, ticket_path),
            "source_bundle_blob": source_blob(repo, evidence, bundle_path),
            "route_plan_blob": source_blob(repo, evidence, route_path),
            "route_plan_sha256": hashlib.sha256(route_text.encode()).hexdigest(),
            "bundle_attestation_blob": (
                None if legacy else source_blob(repo, evidence, bundle_attestation_path)
            ),
            "approval_attestation_blob": approval_oid,
            "legacy_review": legacy_review,
            "original_pr": original,
            "adoption_pr": adoption,
            "paths": path_evidence,
            "checks": check_evidence(request["repository"], adoption["merge_commit"], expected_checks),
            "ledger": ledger_evidence(
                repo, basis_commit, ticket,
                None if legacy else bundle["reviewer_run_id"],
                None if legacy else bundle["narrator_run_id"],
            ),
            "authorization_blob": "",
            "cutoff": request["cutoff"],
            "protected_main_basis": basis,
        }
        receipts[ticket] = receipt
        receipt_path = f"{MIGRATION_DIR}/{ticket}.json"
        auth_entries.append({
            "ticket": ticket,
            "source_state": source_state,
            "source_kit_sha": source_kit,
            "classification": classification,
            "evidence_head": evidence,
            "original_pr_number": original["number"],
            "adoption_pr_number": adoption["number"],
            "paths": paths,
            "receipt": receipt_path,
        })
        terminals[ticket] = terminal_projection(source_text(repo, basis_commit, ticket_path), receipt_path)
    authorization = {
        "schema": AUTH_SCHEMA,
        "repository": request["repository"],
        "basis_kit_sha": request["basis_kit_sha"],
        "target_kit_sha": request["target_kit_sha"],
        "candidate_contract": request["candidate_contract"],
        "cutoff": request["cutoff"],
        "protected_main_basis": basis,
        "required_checks": sorted(required, key=lambda value: value["name"]),
        "authorization": approval,
        "companions": companions,
        "tickets": auth_entries,
    }
    auth_text = canonical(authorization)
    auth_blob = git(repo, "hash-object", "--stdin", input_text=auth_text).stdout.strip()
    for receipt in receipts.values():
        receipt["authorization_blob"] = auth_blob
    files = {
        **companion_files,
        "factory/KIT_PIN": request["target_kit_sha"] + "\n",
        f"{MIGRATION_DIR}/authorization.json": auth_text,
        **{f"{MIGRATION_DIR}/{ticket}.json": canonical(receipt) for ticket, receipt in receipts.items()},
        **{f"factory/tickets/{ticket}.md": text for ticket, text in terminals.items()},
    }
    candidate = temporary_candidate(repo, basis_commit, files, request["cutoff"])
    validate_generated_reconciliation_batch(repo, authorization, receipts, candidate)
    for relative, content in files.items():
        write_if_changed(repo / relative, content, immutable=relative.startswith(MIGRATION_DIR + "/"))
    return {"tickets": sorted(receipts), "files": sorted(files), "authorization_blob": auth_blob}


def read_json_file_at(repo, ref, path, label):
    try:
        return json.loads(source_text(repo, ref, path))
    except json.JSONDecodeError as error:
        raise ValidationError(f"{label} is invalid JSON") from error


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--product", required=True, type=Path)
    parser.add_argument("--request", required=True, type=Path)
    args = parser.parse_args()
    try:
        print(canonical(generate(args.product, args.request)), end="")
    except ValidationError as error:
        print(f"protected-merge-reconciliation: {error}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
