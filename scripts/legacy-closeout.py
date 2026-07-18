#!/usr/bin/env python3
"""Generate the bounded Contract 1.2 legacy-closeout product change."""

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

from legacy_closeout import (  # noqa: E402
    AGGREGATE_CHECK_TICKETS,
    AUTH_SCHEMA,
    MIGRATION_DIR,
    OUT_OF_BAND_TICKETS,
    RECEIPT_SCHEMA,
    REQUIRED_CHECK_NAMES,
    ValidationError,
    exact,
    hash_text,
    oid,
    required_check_names,
    timestamp,
    validate_generated_legacy_batch,
)


REQUEST_SCHEMA = "nysa.software-factory.legacy-closeout-request/v1"
REQUEST_KEYS = {
    "schema", "repository", "source_kit_sha", "target_kit_sha",
    "candidate_contract", "cutoff", "protected_main_basis",
    "required_checks", "authorization", "tickets",
}
REQUEST_TICKET_KEYS = {
    "ticket", "classification", "pr_number", "independent_audit",
}


def command(argv, *, cwd=None, input_text=None, env=None, check=True):
    result = subprocess.run(
        argv,
        cwd=cwd,
        input=input_text,
        capture_output=True,
        text=True,
        env=env,
    )
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


def canonical_json(value):
    return json.dumps(value, indent=2, sort_keys=True) + "\n"


def read_json(path, label):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ValidationError(f"{label} is unavailable or invalid") from error


def project_repository(repo, ref):
    text = git(repo, "show", f"{ref}:factory/PROJECT.env").stdout
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


def source_text(repo, ref, path):
    result = git(repo, "show", f"{ref}:{path}", check=False)
    if result.returncode:
        raise ValidationError(f"source evidence is missing: {path}")
    return result.stdout


def source_blob(repo, ref, path):
    return git(repo, "rev-parse", f"{ref}:{path}").stdout.strip()


def one_field(text, name):
    values = re.findall(rf"(?mi)^{re.escape(name)}:\s*(.*?)\s*$", text)
    if len(values) != 1:
        raise ValidationError(f"source ticket must contain exactly one {name}")
    return values[0].strip()


def replace_field(text, name, value):
    pattern = re.compile(rf"(?mi)^{re.escape(name)}:\s*.*$")
    if len(pattern.findall(text)) != 1:
        raise ValidationError(f"source ticket must contain exactly one {name}")
    return pattern.sub(f"{name}: {value}", text, count=1)


def add_field(text, after, name, value):
    if re.search(rf"(?mi)^{re.escape(name)}:", text):
        raise ValidationError(f"source ticket already contains {name}")
    pattern = re.compile(rf"(?mi)^({re.escape(after)}:\s*.*)$")
    if len(pattern.findall(text)) != 1:
        raise ValidationError(f"source ticket must contain exactly one {after}")
    return pattern.sub(rf"\1\n{name}: {value}", text, count=1)


def ledger_evidence(repo, basis, ticket):
    text = source_text(repo, basis, "factory/ledger.csv")
    rows = list(csv.DictReader(io.StringIO(text)))
    ticket_rows = [row for row in rows if row.get("ticket") == ticket]
    run_ids = [row.get("run_id") for row in ticket_rows]
    if (
        not ticket_rows
        or any(not value for value in run_ids)
        or len(run_ids) != len(set(run_ids))
    ):
        raise ValidationError(
            f"{ticket} accounting is absent or ambiguous; project authoritative "
            "runtime manifests with the existing ledger helper first"
        )
    successful = {
        row.get("role") for row in ticket_rows if row.get("exit_status") == "0"
    }
    if not {"reviewer", "narrator"} <= successful:
        raise ValidationError(f"{ticket} lacks settled Reviewer/Narrator accounting")
    return {
        "sha256": hashlib.sha256(text.encode()).hexdigest(),
        "run_ids": run_ids,
    }


def pr_evidence(repo, repository, number, ticket, basis, cutoff):
    pr = gh_json(f"repos/{repository}/pulls/{number}")
    if (
        pr.get("number") != number
        or pr.get("state") != "closed"
        or not pr.get("merged")
        or pr.get("base", {}).get("ref") != "main"
        or pr.get("head", {}).get("ref") != f"ticket/{ticket}"
    ):
        raise ValidationError(f"{ticket} does not have the exact merged ticket PR")
    head = pr.get("head", {}).get("sha")
    merge = pr.get("merge_commit_sha")
    oid(head, f"{ticket} PR head")
    oid(merge, f"{ticket} PR merge commit")
    if git(repo, "merge-base", "--is-ancestor", merge, basis, check=False).returncode:
        raise ValidationError(f"{ticket} merge commit is not in the protected basis")
    merged_at = pr.get("merged_at")
    if timestamp(merged_at, f"{ticket} merged_at") > cutoff:
        raise ValidationError(f"{ticket} merged after the cutoff")
    actor = (pr.get("merged_by") or {}).get("login")
    if not isinstance(actor, str) or not actor:
        raise ValidationError(f"{ticket} merge actor is unavailable")
    return {
        "number": number,
        "head": head,
        "merge_commit": merge,
        "merged_at": merged_at,
        "merged_by": actor,
    }


def check_evidence(repository, commit, expected):
    response = gh_json(f"repos/{repository}/commits/{commit}/check-runs?per_page=100")
    runs = response.get("check_runs")
    if (
        not isinstance(runs, list)
        or response.get("total_count") != len(runs)
        or len(runs) >= 100
    ):
        raise ValidationError("GitHub check evidence is incomplete or requires pagination")
    by_name = {}
    for item in runs:
        name = item.get("name")
        if name in expected:
            by_name.setdefault(name, []).append(item)
    if set(by_name) != set(expected) or any(len(items) != 1 for items in by_name.values()):
        raise ValidationError("required checks are missing or ambiguous")
    statuses = gh_json(
        f"repos/{repository}/commits/{commit}/status?per_page=100"
    ).get("statuses")
    if not isinstance(statuses, list) or len(statuses) >= 100:
        raise ValidationError("GitHub status evidence is invalid or incomplete")
    if any(item.get("context") in expected for item in statuses):
        raise ValidationError("required check name is ambiguous across checks and statuses")
    result = []
    for name in sorted(expected):
        item = by_name[name][0]
        app = item.get("app") or {}
        identity = expected[name]
        skipped = item.get("conclusion") in {"skipped", "neutral"}
        if (
            app.get("id") != identity["app_id"]
            or app.get("slug") != identity["app_slug"]
            or item.get("status") != "completed"
            or item.get("conclusion") != "success"
            or skipped
        ):
            raise ValidationError(f"required check is skipped, unsuccessful, or from the wrong app: {name}")
        result.append({
            "name": name,
            "app_id": app["id"],
            "app_slug": app["slug"],
            "status": item["status"],
            "conclusion": item["conclusion"],
            "skipped": False,
        })
    return result


def branch_evidence(repo, ticket, head, cutoff_text):
    remote = git(repo, "remote", "get-url", "--push", "origin").stdout.strip()
    observed = git(
        repo, "ls-remote", "--heads", "--", remote, f"refs/heads/ticket/{ticket}",
    ).stdout.splitlines()
    if len(observed) > 1:
        raise ValidationError(f"{ticket} remote branch is ambiguous")
    if not observed:
        return {
            "name": f"ticket/{ticket}",
            "state": "deleted",
            "tip": None,
            "observed_at": cutoff_text,
        }
    values = observed[0].split()
    if len(values) != 2 or values[0] != head:
        raise ValidationError(f"{ticket} remote branch advanced after the merged PR")
    return {
        "name": f"ticket/{ticket}",
        "state": "exact",
        "tip": head,
        "observed_at": cutoff_text,
    }


def temporary_candidate(repo, basis, files, cutoff):
    with tempfile.TemporaryDirectory(prefix="legacy-closeout-index.") as temp:
        index = Path(temp) / "index"
        env = dict(os.environ, GIT_INDEX_FILE=str(index))
        git(repo, "read-tree", basis, env=env)
        for relative, content in files.items():
            blob = git(repo, "hash-object", "-w", "--stdin", input_text=content).stdout.strip()
            command(
                ["git", "-C", str(repo), "update-index", "--add", "--cacheinfo",
                 "100644", blob, relative],
                env=env,
            )
        tree = git(repo, "write-tree", env=env).stdout.strip()
        author = "Legacy Closeout Validator <factory@invalid>"
        commit_env = dict(
            os.environ,
            GIT_AUTHOR_NAME="Legacy Closeout Validator",
            GIT_AUTHOR_EMAIL="factory@invalid",
            GIT_COMMITTER_NAME="Legacy Closeout Validator",
            GIT_COMMITTER_EMAIL="factory@invalid",
            GIT_AUTHOR_DATE=cutoff,
            GIT_COMMITTER_DATE=cutoff,
        )
        commit = git(
            repo, "commit-tree", tree, "-p", basis,
            input_text="validate deterministic legacy closeout\n", env=commit_env,
        ).stdout.strip()
        return commit


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
    repo = Path(
        git(product, "rev-parse", "--show-toplevel").stdout.strip()
    ).resolve()
    if product.resolve() != repo:
        raise ValidationError("--product must be the repository root")
    request = exact(read_json(request_path, "migration request"), REQUEST_KEYS, "migration request")
    if request["schema"] != REQUEST_SCHEMA:
        raise ValidationError("migration request schema is invalid")
    basis = exact(
        request["protected_main_basis"], {"commit", "tree"},
        "request protected-main basis",
    )
    basis_commit = oid(basis["commit"], "request basis commit")
    oid(basis["tree"], "request basis tree")
    head = git(repo, "rev-parse", "HEAD").stdout.strip()
    protected = git(repo, "rev-parse", "refs/remotes/origin/main").stdout.strip()
    if head != basis_commit or protected != basis_commit:
        raise ValidationError("HEAD and origin/main must exactly match the protected-main basis")
    if git(repo, "rev-parse", f"{basis_commit}^{{tree}}").stdout.strip() != basis["tree"]:
        raise ValidationError("request basis tree does not match its commit")
    if project_repository(repo, basis_commit) != request["repository"]:
        raise ValidationError("request repository does not match factory/PROJECT.env")
    source_pin = source_text(repo, basis_commit, "factory/KIT_PIN")
    if source_pin != request["source_kit_sha"] + "\n":
        raise ValidationError("request source kit does not match protected basis")
    oid(request["source_kit_sha"], "request source kit")
    oid(request["target_kit_sha"], "request target kit")
    if request["source_kit_sha"] == request["target_kit_sha"]:
        raise ValidationError("source and target kits must differ")
    if request["candidate_contract"] != "1.3.0":
        raise ValidationError("legacy closeout targets only Contract 1.3.0")
    cutoff = timestamp(request["cutoff"], "request cutoff")
    if cutoff.microsecond:
        raise ValidationError("migration cutoff must use whole-second precision")
    if cutoff > datetime.now(timezone.utc):
        raise ValidationError("migration cutoff may not be in the future")
    required = request["required_checks"]
    if not isinstance(required, list):
        raise ValidationError("request required_checks must be a list")
    expected_checks = {}
    for item in required:
        exact(item, {"name", "app_id", "app_slug"}, "request check identity")
        if item["name"] in expected_checks:
            raise ValidationError("request required check is duplicated")
        expected_checks[item["name"]] = item
    if tuple(sorted(expected_checks)) != REQUIRED_CHECK_NAMES:
        raise ValidationError("request must name the four exact required checks")
    authorization_payload = exact(
        request["authorization"],
        {"method", "statement", "auto_merge", "bypass"},
        "request authorization",
    )
    if (
        authorization_payload["method"] != "manual-protected-main-merge"
        or authorization_payload["auto_merge"] is not False
        or authorization_payload["bypass"] is not False
    ):
        raise ValidationError("request must require manual protected merge without bypass")
    ticket_requests = request["tickets"]
    if not isinstance(ticket_requests, list) or not ticket_requests:
        raise ValidationError("request ticket batch is empty")
    receipts = {}
    terminal_tickets = {}
    auth_entries = []
    seen = set()
    for item in sorted(ticket_requests, key=lambda value: value.get("ticket", "")):
        exact(item, REQUEST_TICKET_KEYS, "request ticket")
        ticket = item["ticket"]
        if not re.fullmatch(r"T-[0-9]+", ticket) or ticket in seen:
            raise ValidationError("request ticket IDs are invalid or duplicated")
        seen.add(ticket)
        classification = item["classification"]
        source_ticket = source_text(repo, basis_commit, f"factory/tickets/{ticket}.md")
        source_bundle = source_text(repo, basis_commit, f"factory/tickets/{ticket}-bundle.md")
        source_state = one_field(source_ticket, "State")
        if one_field(source_ticket, "Kit-SHA") != request["source_kit_sha"]:
            raise ValidationError(f"{ticket} is not leased to the exact source kit")
        audit = exact(
            item["independent_audit"],
            {"required", "report_sha256", "combined_test_sha256"},
            f"{ticket} independent audit",
        )
        if classification == "legacy-reviewed":
            if source_state != "Review" or audit != {
                "required": False,
                "report_sha256": None,
                "combined_test_sha256": None,
            }:
                raise ValidationError("legacy-reviewed requires exact Review and no invented audit")
        elif classification == "legacy-reviewed-aggregate":
            if (
                ticket not in AGGREGATE_CHECK_TICKETS
                or source_state != "Review"
                or audit.get("required") is not True
                or not re.fullmatch(r"[0-9a-f]{64}", audit.get("report_sha256", ""))
                or not re.fullmatch(
                    r"[0-9a-f]{64}", audit.get("combined_test_sha256", "")
                )
            ):
                raise ValidationError(
                    "aggregate-check migration requires exact T-013 through T-016 "
                    "Review and audit evidence"
                )
        elif classification == "out-of-band-merged":
            if (
                ticket not in OUT_OF_BAND_TICKETS
                or source_state != "Planning"
                or audit.get("required") is not True
                or not re.fullmatch(r"[0-9a-f]{64}", audit.get("report_sha256", ""))
                or not re.fullmatch(r"[0-9a-f]{64}", audit.get("combined_test_sha256", ""))
            ):
                raise ValidationError("out-of-band migration requires exact T-019/T-020 audit evidence")
        else:
            raise ValidationError("request ticket classification is invalid")
        if git(
            repo, "cat-file", "-e", f"{basis_commit}:factory/route-plans/{ticket}.json",
            check=False,
        ).returncode == 0:
            raise ValidationError(f"{ticket} unexpectedly has a route plan")
        pr = pr_evidence(
            repo, request["repository"], item["pr_number"], ticket,
            basis_commit, cutoff,
        )
        ticket_blob = source_blob(repo, pr["head"], f"factory/tickets/{ticket}.md")
        bundle_blob = source_blob(repo, pr["head"], f"factory/tickets/{ticket}-bundle.md")
        if (
            ticket_blob != hash_text(repo, source_ticket)
            or bundle_blob != hash_text(repo, source_bundle)
        ):
            raise ValidationError(f"{ticket} PR head does not match protected source evidence")
        receipt = {
            "schema": RECEIPT_SCHEMA,
            "ticket": ticket,
            "repository": request["repository"],
            "classification": classification,
            "source_state": source_state,
            "source_kit_sha": request["source_kit_sha"],
            "target_kit_sha": request["target_kit_sha"],
            "candidate_contract": request["candidate_contract"],
            "source_ticket_blob": ticket_blob,
            "source_bundle_blob": bundle_blob,
            "pr": pr,
            "branch": branch_evidence(repo, ticket, pr["head"], request["cutoff"]),
            "checks": check_evidence(
                request["repository"],
                pr["head"],
                {
                    name: expected_checks[name]
                    for name in required_check_names(classification, ticket)
                },
            ),
            "ledger": ledger_evidence(repo, basis_commit, ticket),
            "independent_audit": audit,
            "authorization_blob": "",
            "cutoff": request["cutoff"],
            "protected_main_basis": basis,
            "route_plan": {"present": False, "sha256": None},
        }
        receipts[ticket] = receipt
        auth_entries.append({
            "ticket": ticket,
            "classification": classification,
            "source_state": source_state,
            "receipt": f"{MIGRATION_DIR}/{ticket}.json",
        })
        terminal = replace_field(source_ticket, "State", "Done")
        terminal = add_field(terminal, "State", "Operator-Approval", "Migration")
        terminal = add_field(
            terminal, "Operator-Approval", "Migration-Receipt",
            f"{MIGRATION_DIR}/{ticket}.json",
        )
        terminal_tickets[ticket] = terminal
    authorization = {
        "schema": AUTH_SCHEMA,
        "repository": request["repository"],
        "source_kit_sha": request["source_kit_sha"],
        "target_kit_sha": request["target_kit_sha"],
        "candidate_contract": request["candidate_contract"],
        "tickets": auth_entries,
        "required_checks": sorted(required, key=lambda item: item["name"]),
        "authorization": authorization_payload,
        "cutoff": request["cutoff"],
        "protected_main_basis": basis,
    }
    auth_text = canonical_json(authorization)
    auth_blob = git(repo, "hash-object", "--stdin", input_text=auth_text).stdout.strip()
    for receipt in receipts.values():
        receipt["authorization_blob"] = auth_blob
    files = {
        f"{MIGRATION_DIR}/authorization.json": auth_text,
        "factory/KIT_PIN": request["target_kit_sha"] + "\n",
    }
    for ticket, receipt in receipts.items():
        files[f"{MIGRATION_DIR}/{ticket}.json"] = canonical_json(receipt)
        files[f"factory/tickets/{ticket}.md"] = terminal_tickets[ticket]
    candidate = temporary_candidate(repo, basis_commit, files, request["cutoff"])
    validate_generated_legacy_batch(repo, authorization, receipts, candidate)
    changed = []
    for relative, content in sorted(files.items()):
        if write_if_changed(
            repo / relative, content,
            immutable=relative.startswith(MIGRATION_DIR + "/"),
        ):
            changed.append(relative)
    return {
        "status": "ok",
        "repository": request["repository"],
        "tickets": sorted(receipts),
        "changed": changed,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--product", required=True)
    parser.add_argument("--request", required=True)
    args = parser.parse_args()
    result = generate(Path(args.product), Path(args.request))
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except (OSError, KeyError, ValidationError) as error:
        print(f"legacy-closeout: {error}", file=sys.stderr)
        raise SystemExit(1)
