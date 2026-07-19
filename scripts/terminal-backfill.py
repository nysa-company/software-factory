#!/usr/bin/env python3
"""Generate the exact T-001..T-012 pre-contract terminal backfill."""

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

from legacy_closeout import ValidationError, exact, hash_text, oid, timestamp  # noqa: E402
from terminal_backfill import (  # noqa: E402
    AUTHORIZED_TICKETS,
    AUTH_SCHEMA,
    CHECK_NAMES,
    CLASSIFICATION,
    MIGRATION_DIR,
    RECEIPT_SCHEMA,
    validate_generated_terminal_backfill,
)


REQUEST_SCHEMA = "nysa.software-factory.terminal-backfill-request/v1"
REQUEST_KEYS = {
    "schema", "repository", "target_kit_sha", "candidate_contract", "cutoff",
    "protected_main_basis", "authorization", "tickets",
}
REQUEST_TICKET_KEYS = {
    "ticket", "implementation_pr_number", "closeout_pr_number",
    "required_checks",
}


def command(argv, *, cwd=None, input_text=None, env=None, check=True):
    result = subprocess.run(
        argv, cwd=cwd, input=input_text, capture_output=True, text=True, env=env,
    )
    if check and result.returncode:
        raise ValidationError(result.stderr.strip() or "command failed")
    return result


def git(repo, *args, **kwargs):
    return command(["git", "-C", str(repo), *args], **kwargs)


def gh_json(*args):
    result = command([
        "gh", "api", "-H", "Accept: application/vnd.github+json", *args,
    ])
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise ValidationError("GitHub returned invalid JSON") from error


def canonical_json(value):
    return json.dumps(value, indent=2, sort_keys=True) + "\n"


def read_json(path, label):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ValidationError(f"{label} is unavailable or invalid") from error


def source_text(repo, ref, path, *, nullable=False):
    result = git(repo, "show", f"{ref}:{path}", check=False)
    if result.returncode:
        if nullable:
            return None
        raise ValidationError(f"source evidence is missing: {path}")
    return result.stdout


def project_repository(repo, ref):
    text = source_text(repo, ref, "factory/PROJECT.env")
    values = []
    for raw in text.splitlines():
        match = re.fullmatch(r"\s*(?:export\s+)?GH_REPO\s*=\s*(.*?)\s*", raw)
        if match:
            value = match.group(1)
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            values.append(value)
    if len(values) != 1:
        raise ValidationError("product must define one exact GH_REPO")
    return values[0]


def one_optional_field(text, name):
    values = re.findall(rf"(?mi)^{re.escape(name)}:\s*(.*?)\s*$", text)
    if len(values) > 1:
        raise ValidationError(f"source ticket has duplicate {name} fields")
    return values[0].strip() if values else None


def ledger_evidence(repo, basis, ticket):
    text = source_text(repo, basis, "factory/ledger.csv")
    rows = list(csv.DictReader(io.StringIO(text)))
    ticket_rows = [row for row in rows if row.get("ticket") == ticket]
    run_ids = [row.get("run_id") for row in ticket_rows]
    successful = {
        row.get("role") for row in ticket_rows if row.get("exit_status") == "0"
    }
    if (
        not ticket_rows
        or any(not value for value in run_ids)
        or len(run_ids) != len(set(run_ids))
        or not {"reviewer", "narrator"} <= successful
    ):
        raise ValidationError(f"{ticket} ledger evidence is absent or ambiguous")
    return {
        "sha256": hashlib.sha256(text.encode()).hexdigest(),
        "run_ids": run_ids,
    }


def pr_evidence(repo, repository, number, basis, cutoff, label):
    pr = gh_json(f"repos/{repository}/pulls/{number}")
    if (
        pr.get("number") != number
        or pr.get("state") != "closed"
        or not pr.get("merged")
        or pr.get("base", {}).get("ref") != "main"
    ):
        raise ValidationError(f"{label} is not an exact merged main PR")
    head_ref = pr.get("head", {}).get("ref")
    head = pr.get("head", {}).get("sha")
    merge = pr.get("merge_commit_sha")
    oid(head, f"{label} head")
    oid(merge, f"{label} merge commit")
    if not isinstance(head_ref, str) or not head_ref:
        raise ValidationError(f"{label} head ref is unavailable")
    if git(repo, "merge-base", "--is-ancestor", merge, basis, check=False).returncode:
        raise ValidationError(f"{label} merge is outside the protected basis")
    merged_at = pr.get("merged_at")
    if timestamp(merged_at, f"{label} merged_at") > cutoff:
        raise ValidationError(f"{label} merged after the cutoff")
    actor = (pr.get("merged_by") or {}).get("login")
    if not isinstance(actor, str) or not actor:
        raise ValidationError(f"{label} merge actor is unavailable")
    return {
        "number": number,
        "head_ref": head_ref,
        "base_ref": "main",
        "head": head,
        "merge_commit": merge,
        "merged_at": merged_at,
        "merged_by": actor,
    }


def check_evidence(repository, commit, identities):
    response = gh_json(f"repos/{repository}/commits/{commit}/check-runs?per_page=100")
    runs = response.get("check_runs")
    if (
        not isinstance(runs, list)
        or response.get("total_count") != len(runs)
        or len(runs) >= 100
    ):
        raise ValidationError("GitHub check evidence is incomplete")
    by_name = {}
    for item in runs:
        if item.get("name") in identities:
            by_name.setdefault(item["name"], []).append(item)
    if set(by_name) != set(identities) or any(
        len(items) != 1 for items in by_name.values()
    ):
        raise ValidationError("historical checks are missing or ambiguous")
    statuses = gh_json(
        f"repos/{repository}/commits/{commit}/status?per_page=100"
    ).get("statuses")
    if not isinstance(statuses, list) or len(statuses) >= 100:
        raise ValidationError("historical status evidence is incomplete")
    if any(item.get("context") in identities for item in statuses):
        raise ValidationError("historical check name collides with a status")
    result = []
    for name in sorted(identities):
        item = by_name[name][0]
        app = item.get("app") or {}
        identity = identities[name]
        if (
            app.get("id") != identity["app_id"]
            or app.get("slug") != identity["app_slug"]
            or item.get("status") != "completed"
            or item.get("conclusion") != "success"
        ):
            raise ValidationError(
                f"historical check is failed, skipped, or wrong-app: {name}"
            )
        result.append({
            "name": name,
            "app_id": app["id"],
            "app_slug": app["slug"],
            "status": "completed",
            "conclusion": "success",
            "skipped": False,
        })
    return result


def temporary_candidate(repo, basis, files, cutoff):
    with tempfile.TemporaryDirectory(prefix="terminal-backfill-index.") as temp:
        env = dict(os.environ, GIT_INDEX_FILE=str(Path(temp) / "index"))
        git(repo, "read-tree", basis, env=env)
        for relative, content in files.items():
            blob = git(
                repo, "hash-object", "-w", "--stdin", input_text=content
            ).stdout.strip()
            command([
                "git", "-C", str(repo), "update-index", "--add", "--cacheinfo",
                "100644", blob, relative,
            ], env=env)
        tree = git(repo, "write-tree", env=env).stdout.strip()
        commit_env = dict(
            os.environ,
            GIT_AUTHOR_NAME="Terminal Backfill Validator",
            GIT_AUTHOR_EMAIL="factory@invalid",
            GIT_COMMITTER_NAME="Terminal Backfill Validator",
            GIT_COMMITTER_EMAIL="factory@invalid",
            GIT_AUTHOR_DATE=cutoff,
            GIT_COMMITTER_DATE=cutoff,
        )
        return git(
            repo, "commit-tree", tree, "-p", basis,
            input_text="validate deterministic terminal backfill\n",
            env=commit_env,
        ).stdout.strip()


def write_if_changed(path, content, *, immutable=False):
    if path.exists() and path.is_symlink():
        raise ValidationError(f"refusing symlink output: {path}")
    if path.exists() and path.read_text() == content:
        return False
    if path.exists() and immutable:
        raise ValidationError(f"existing terminal-backfill evidence conflicts: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return True


def generate(product, request_path):
    repo = Path(git(product, "rev-parse", "--show-toplevel").stdout.strip()).resolve()
    if product.resolve() != repo:
        raise ValidationError("--product must be the repository root")
    request = exact(
        read_json(request_path, "terminal-backfill request"),
        REQUEST_KEYS,
        "terminal-backfill request",
    )
    if request["schema"] != REQUEST_SCHEMA:
        raise ValidationError("terminal-backfill request schema is invalid")
    basis = exact(
        request["protected_main_basis"], {"commit", "tree"},
        "terminal-backfill request basis",
    )
    basis_commit = oid(basis["commit"], "terminal-backfill basis commit")
    oid(basis["tree"], "terminal-backfill basis tree")
    head = git(repo, "rev-parse", "HEAD").stdout.strip()
    protected = git(repo, "rev-parse", "refs/remotes/origin/main").stdout.strip()
    if head != basis_commit or protected != basis_commit:
        raise ValidationError("HEAD and origin/main must equal the protected basis")
    if git(repo, "rev-parse", f"{basis_commit}^{{tree}}").stdout.strip() != basis["tree"]:
        raise ValidationError("terminal-backfill request basis tree is invalid")
    if project_repository(repo, basis_commit) != request["repository"]:
        raise ValidationError("terminal-backfill repository does not match basis")
    basis_pin = source_text(repo, basis_commit, "factory/KIT_PIN").strip()
    oid(basis_pin, "terminal-backfill basis kit")
    oid(request["target_kit_sha"], "terminal-backfill target kit")
    if basis_pin == request["target_kit_sha"]:
        raise ValidationError("terminal-backfill source and target kits must differ")
    if request["candidate_contract"] != "1.3.0":
        raise ValidationError("terminal backfill targets only Contract 1.3.0")
    cutoff = timestamp(request["cutoff"], "terminal-backfill cutoff")
    if cutoff.microsecond or cutoff > datetime.now(timezone.utc):
        raise ValidationError("terminal-backfill cutoff is invalid")
    approval = exact(
        request["authorization"],
        {"method", "statement", "auto_merge", "bypass"},
        "terminal-backfill authorization",
    )
    if (
        approval["method"] != "manual-protected-main-merge"
        or approval["auto_merge"] is not False
        or approval["bypass"] is not False
    ):
        raise ValidationError("terminal backfill requires manual merge without bypass")

    ticket_requests = request["tickets"]
    if not isinstance(ticket_requests, list):
        raise ValidationError("terminal-backfill ticket batch must be a list")
    by_ticket = {}
    for item in ticket_requests:
        exact(item, REQUEST_TICKET_KEYS, "terminal-backfill request ticket")
        ticket = item["ticket"]
        if ticket in by_ticket:
            raise ValidationError("terminal-backfill request ticket is duplicated")
        identities = {}
        checks = item["required_checks"]
        if not isinstance(checks, list) or len(checks) != len(CHECK_NAMES):
            raise ValidationError(f"{ticket} required checks are incomplete")
        for identity in checks:
            exact(identity, {"name", "app_id", "app_slug"}, f"{ticket} check identity")
            if identity["name"] in identities:
                raise ValidationError(f"{ticket} required check is duplicated")
            identities[identity["name"]] = identity
        if tuple(sorted(identities)) != CHECK_NAMES:
            raise ValidationError(f"{ticket} check names are not exact")
        by_ticket[ticket] = (item, identities)
    if tuple(sorted(by_ticket)) != AUTHORIZED_TICKETS:
        raise ValidationError("terminal backfill requires exact T-001 through T-012")

    receipts = {}
    auth_entries = []
    for ticket in AUTHORIZED_TICKETS:
        item, identities = by_ticket[ticket]
        ticket_path = f"factory/tickets/{ticket}.md"
        bundle_path = f"factory/tickets/{ticket}-bundle.md"
        ticket_text = source_text(repo, basis_commit, ticket_path)
        if one_optional_field(ticket_text, "State") != "Done":
            raise ValidationError(f"{ticket} source state must be exact Done")
        source_kit = one_optional_field(ticket_text, "Kit-SHA")
        if source_kit is not None:
            oid(source_kit, f"{ticket} source Kit-SHA")
        if git(
            repo, "cat-file", "-e",
            f"{basis_commit}:factory/route-plans/{ticket}.json", check=False,
        ).returncode == 0:
            raise ValidationError(f"{ticket} unexpectedly has a route plan")
        implementation = pr_evidence(
            repo, request["repository"], item["implementation_pr_number"],
            basis_commit, cutoff, f"{ticket} implementation PR",
        )
        if not re.match(rf"^ticket/{re.escape(ticket)}(?:-|$)", implementation["head_ref"]):
            raise ValidationError(f"{ticket} implementation PR branch is invalid")
        closeout = pr_evidence(
            repo, request["repository"], item["closeout_pr_number"],
            basis_commit, cutoff, f"{ticket} closeout PR",
        )
        if git(
            repo, "merge-base", "--is-ancestor", implementation["merge_commit"],
            closeout["merge_commit"], check=False,
        ).returncode:
            raise ValidationError(f"{ticket} closeout does not descend from implementation")
        ticket_blob = hash_text(repo, ticket_text)
        closeout_ticket_text = source_text(repo, closeout["head"], ticket_path)
        if one_optional_field(closeout_ticket_text, "State") != "Done":
            raise ValidationError(f"{ticket} closeout head is not terminal Done")
        closeout_ticket_blob = hash_text(repo, closeout_ticket_text)
        bundle_text = source_text(
            repo, basis_commit, bundle_path, nullable=True
        )
        bundle_blob = hash_text(repo, bundle_text) if bundle_text is not None else None
        closeout_bundle_text = source_text(
            repo, closeout["head"], bundle_path, nullable=True
        )
        closeout_bundle_blob = (
            hash_text(repo, closeout_bundle_text)
            if closeout_bundle_text is not None else None
        )
        receipt = {
            "schema": RECEIPT_SCHEMA,
            "ticket": ticket,
            "repository": request["repository"],
            "classification": CLASSIFICATION,
            "source_state": "Done",
            "basis_kit_sha": basis_pin,
            "target_kit_sha": request["target_kit_sha"],
            "candidate_contract": request["candidate_contract"],
            "source_ticket_blob": ticket_blob,
            "source_bundle_blob": bundle_blob,
            "source_kit_sha": source_kit,
            "closeout_ticket_blob": closeout_ticket_blob,
            "closeout_bundle_blob": closeout_bundle_blob,
            "implementation_pr": implementation,
            "closeout_pr": closeout,
            "checks": check_evidence(
                request["repository"], implementation["head"], identities
            ),
            "ledger": ledger_evidence(repo, basis_commit, ticket),
            "authorization_blob": "",
            "cutoff": request["cutoff"],
            "protected_main_basis": basis,
            "route_plan": {"present": False, "sha256": None},
        }
        receipts[ticket] = receipt
        auth_entries.append({
            "ticket": ticket,
            "classification": CLASSIFICATION,
            "source_state": "Done",
            "receipt": f"{MIGRATION_DIR}/{ticket}.json",
            "implementation_pr_number": item["implementation_pr_number"],
            "closeout_pr_number": item["closeout_pr_number"],
            "required_checks": sorted(
                item["required_checks"], key=lambda value: value["name"]
            ),
        })

    authorization = {
        "schema": AUTH_SCHEMA,
        "repository": request["repository"],
        "basis_kit_sha": basis_pin,
        "target_kit_sha": request["target_kit_sha"],
        "candidate_contract": request["candidate_contract"],
        "tickets": auth_entries,
        "authorization": approval,
        "cutoff": request["cutoff"],
        "protected_main_basis": basis,
    }
    auth_text = canonical_json(authorization)
    auth_blob = git(repo, "hash-object", "--stdin", input_text=auth_text).stdout.strip()
    files = {f"{MIGRATION_DIR}/authorization.json": auth_text}
    if basis_pin != request["target_kit_sha"]:
        files["factory/KIT_PIN"] = request["target_kit_sha"] + "\n"
    for ticket, receipt in receipts.items():
        receipt["authorization_blob"] = auth_blob
        files[f"{MIGRATION_DIR}/{ticket}.json"] = canonical_json(receipt)
    candidate = temporary_candidate(repo, basis_commit, files, request["cutoff"])
    validate_generated_terminal_backfill(repo, authorization, receipts, candidate)
    changed = [
        relative for relative, content in sorted(files.items())
        if write_if_changed(
            repo / relative,
            content,
            immutable=relative.startswith(MIGRATION_DIR + "/"),
        )
    ]
    return {
        "status": "ok",
        "repository": request["repository"],
        "tickets": list(AUTHORIZED_TICKETS),
        "changed": changed,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--product", required=True)
    parser.add_argument("--request", required=True)
    args = parser.parse_args()
    print(json.dumps(
        generate(Path(args.product), Path(args.request)), indent=2, sort_keys=True
    ))


if __name__ == "__main__":
    try:
        main()
    except (OSError, KeyError, ValidationError) as error:
        print(f"terminal-backfill: {error}", file=sys.stderr)
        raise SystemExit(1)
