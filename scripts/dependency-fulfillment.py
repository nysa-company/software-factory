#!/usr/bin/env python3
"""Plan or apply an operator-approved dependency-only adoption batch."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))

from dependency_fulfillment import (  # noqa: E402
    AUTH_SCHEMA,
    MIGRATION_DIR,
    RECEIPT_SCHEMA,
    validate_generated_dependency_batch,
)
from legacy_closeout import (  # noqa: E402
    ValidationError,
    blob_at,
    exact,
    oid,
    one_field,
    repository_from_project,
    run,
    text_at,
    timestamp,
)


REQUEST_SCHEMA = "nysa.software-factory.dependency-fulfillment-request/v1"
REQUEST_KEYS = {
    "schema", "repository", "target_kit_sha", "candidate_contract", "cutoff",
    "protected_main_basis", "required_checks", "authorization", "tickets",
}
REQUEST_TICKET_KEYS = {"ticket", "pr_number"}
CHECK_IDENTITY_KEYS = {"name", "app_id", "app_slug"}
AUTHORIZATION_KEYS = {
    "method", "operator", "authorized_at", "statement", "auto_merge", "bypass",
}
SHA256 = re.compile(r"[0-9a-f]{64}")


def command(argv, *, cwd=None, input_text=None, env=None, check=True):
    result = subprocess.run(
        argv,
        cwd=cwd,
        input=input_text,
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=120,
    )
    if check and result.returncode:
        raise ValidationError(result.stderr.strip() or "command failed")
    return result


def gh_json(*args):
    result = command(
        ["gh", "api", "-H", "Accept: application/vnd.github+json", *args]
    )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise ValidationError("GitHub returned invalid JSON") from error


def canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def pretty(value):
    return json.dumps(value, indent=2, sort_keys=True) + "\n"


def read_json(path, label):
    def unique_object(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate JSON key")
            value[key] = item
        return value

    try:
        return json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=unique_object,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise ValidationError(f"{label} is unavailable or invalid") from error


def check_evidence(repository, commit, expected):
    response = gh_json(f"repos/{repository}/commits/{commit}/check-runs?per_page=100")
    runs = response.get("check_runs")
    statuses = gh_json(
        f"repos/{repository}/commits/{commit}/status?per_page=100"
    ).get("statuses")
    if (
        not isinstance(runs, list)
        or response.get("total_count") != len(runs)
        or len(runs) >= 100
        or not isinstance(statuses, list)
        or len(statuses) >= 100
        or any(item.get("context") in expected for item in statuses)
    ):
        raise ValidationError("GitHub check evidence is incomplete or ambiguous")
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
            raise ValidationError(
                f"required check is unsuccessful or from the wrong app: {name}"
            )
        result.append(
            {
                **identity,
                "status": "completed",
                "conclusion": "success",
                "skipped": False,
            }
        )
    return result


def pr_evidence(repo, repository, number, basis, cutoff, ticket):
    pr = gh_json(f"repos/{repository}/pulls/{number}")
    if (
        pr.get("number") != number
        or pr.get("state") != "closed"
        or pr.get("merged") is not True
        or pr.get("base", {}).get("ref") != "main"
    ):
        raise ValidationError(f"{ticket} dependency PR is not merged to main")
    head = pr.get("head", {}).get("sha")
    merge_commit = pr.get("merge_commit_sha")
    oid(head, f"{ticket} dependency PR head")
    oid(merge_commit, f"{ticket} dependency merge commit")
    merged_at = pr.get("merged_at")
    merged_by = (pr.get("merged_by") or {}).get("login")
    if (
        not isinstance(merged_by, str)
        or not merged_by
        or timestamp(merged_at, f"{ticket} dependency merged_at") > cutoff
        or run(
            repo,
            "merge-base",
            "--is-ancestor",
            merge_commit,
            basis,
            check=False,
        ).returncode
    ):
        raise ValidationError(f"{ticket} dependency merge is outside the basis")
    return {
        "number": number,
        "head_ref": pr.get("head", {}).get("ref"),
        "base_ref": "main",
        "head": head,
        "merge_commit": merge_commit,
        "merged_at": merged_at,
        "merged_by": merged_by,
    }


def temporary_candidate(repo, basis, files, cutoff):
    with tempfile.TemporaryDirectory(prefix="dependency-fulfillment.") as temp:
        env = dict(os.environ, GIT_INDEX_FILE=str(Path(temp) / "index"))
        command(["git", "-C", str(repo), "read-tree", basis], env=env)
        for relative, content in files.items():
            blob = command(
                ["git", "-C", str(repo), "hash-object", "-w", "--stdin"],
                input_text=content,
            ).stdout.strip()
            command(
                [
                    "git", "-C", str(repo), "update-index", "--add",
                    "--cacheinfo", "100644", blob, relative,
                ],
                env=env,
            )
        tree = command(["git", "-C", str(repo), "write-tree"], env=env).stdout.strip()
        commit_env = dict(
            os.environ,
            GIT_AUTHOR_NAME="Dependency Fulfillment Validator",
            GIT_AUTHOR_EMAIL="factory@invalid",
            GIT_COMMITTER_NAME="Dependency Fulfillment Validator",
            GIT_COMMITTER_EMAIL="factory@invalid",
            GIT_AUTHOR_DATE=cutoff,
            GIT_COMMITTER_DATE=cutoff,
        )
        return command(
            ["git", "-C", str(repo), "commit-tree", tree, "-p", basis],
            input_text="validate dependency fulfillment\n",
            env=commit_env,
        ).stdout.strip()


def prepare(product, request_path):
    repo = Path(
        run(product, "rev-parse", "--show-toplevel").stdout.strip()
    ).resolve()
    if product.resolve() != repo:
        raise ValidationError("--product must be the repository root")
    if run(repo, "status", "--porcelain").stdout:
        raise ValidationError("dependency fulfillment requires a clean worktree")
    request = exact(
        read_json(request_path, "dependency fulfillment request"),
        REQUEST_KEYS,
        "dependency fulfillment request",
    )
    if request["schema"] != REQUEST_SCHEMA:
        raise ValidationError("dependency fulfillment request schema is invalid")
    basis = exact(
        request["protected_main_basis"],
        {"commit", "tree"},
        "dependency fulfillment request basis",
    )
    basis_commit = oid(basis["commit"], "dependency fulfillment request basis commit")
    oid(basis["tree"], "dependency fulfillment request basis tree")
    head = run(repo, "rev-parse", "HEAD").stdout.strip()
    if (
        head != basis_commit
        or run(repo, "rev-parse", "refs/remotes/origin/main").stdout.strip()
        != basis_commit
        or run(repo, "rev-parse", f"{basis_commit}^{{tree}}").stdout.strip()
        != basis["tree"]
    ):
        raise ValidationError(
            "HEAD and origin/main must match the exact protected basis"
        )
    if repository_from_project(repo, basis_commit) != request["repository"]:
        raise ValidationError("request repository does not match product configuration")
    oid(request["target_kit_sha"], "dependency fulfillment target kit")
    if (
        request["candidate_contract"] not in ("1.8.0", "1.9.0")
        or text_at(repo, basis_commit, "factory/KIT_PIN")
        == request["target_kit_sha"] + "\n"
    ):
        raise ValidationError(
            "dependency fulfillment must install a different Contract 1.8 kit"
        )
    cutoff = timestamp(request["cutoff"], "dependency fulfillment request cutoff")
    if cutoff.microsecond or cutoff > datetime.now(timezone.utc):
        raise ValidationError(
            "request cutoff must be a whole-second time that is not in the future"
        )
    approval = exact(
        request["authorization"],
        AUTHORIZATION_KEYS,
        "dependency fulfillment request authorization",
    )
    if approval["authorized_at"] != request["cutoff"]:
        raise ValidationError("operator authorization must define the exact cutoff")
    required = request["required_checks"]
    if not isinstance(required, list):
        raise ValidationError("request required_checks must be a list")
    expected_checks = {}
    for item in required:
        exact(item, CHECK_IDENTITY_KEYS, "request check identity")
        if (
            item["name"] in expected_checks
            or not isinstance(item["app_id"], int)
            or item["app_id"] <= 0
            or not isinstance(item["app_slug"], str)
            or not item["app_slug"]
        ):
            raise ValidationError("request check identity is invalid or duplicated")
        expected_checks[item["name"]] = item
    tickets = request["tickets"]
    if (
        not isinstance(tickets, list)
        or not tickets
        or tickets
        != sorted(tickets, key=lambda item: item.get("ticket", ""))
    ):
        raise ValidationError("request tickets must be a nonempty sorted list")
    authorization = {
        "schema": AUTH_SCHEMA,
        "repository": request["repository"],
        "target_kit_sha": request["target_kit_sha"],
        "candidate_contract": request["candidate_contract"],
        "cutoff": request["cutoff"],
        "protected_main_basis": basis,
        "required_checks": required,
        "authorization": approval,
        "tickets": [],
    }
    seen = set()
    evidence = {}
    for item in tickets:
        exact(item, REQUEST_TICKET_KEYS, "dependency fulfillment request ticket")
        ticket = item["ticket"]
        if (
            not isinstance(ticket, str)
            or not re.fullmatch(r"T-[0-9]+", ticket)
            or ticket in seen
            or not isinstance(item["pr_number"], int)
            or item["pr_number"] <= 0
        ):
            raise ValidationError(
                "dependency fulfillment request ticket is invalid or duplicated"
            )
        seen.add(ticket)
        ticket_text = text_at(
            repo, basis_commit, f"factory/tickets/{ticket}.md"
        )
        if ticket_text is None or one_field(ticket_text, "State") != "Backlog":
            raise ValidationError(
                f"{ticket} dependency-only fulfillment requires Backlog state"
            )
        pr = pr_evidence(
            repo,
            request["repository"],
            item["pr_number"],
            basis_commit,
            cutoff,
            ticket,
        )
        evidence[ticket] = {
            "pr": pr,
            "checks": check_evidence(
                request["repository"], pr["merge_commit"], expected_checks
            ),
            "source_ticket_blob": blob_at(
                repo, basis_commit, f"factory/tickets/{ticket}.md"
            ),
        }
        authorization["tickets"].append(
            {
                "ticket": ticket,
                "pr_number": item["pr_number"],
                "receipt": f"{MIGRATION_DIR}/{ticket}.json",
            }
        )
    authorization_text = pretty(authorization)
    authorization_blob = run(
        repo, "hash-object", "--stdin", input_text=authorization_text
    ).stdout.strip()
    receipts = {}
    files = {
        "factory/KIT_PIN": request["target_kit_sha"] + "\n",
        f"{MIGRATION_DIR}/authorization.json": authorization_text,
    }
    for ticket in sorted(evidence):
        receipts[ticket] = {
            "schema": RECEIPT_SCHEMA,
            "ticket": ticket,
            "repository": request["repository"],
            "target_kit_sha": request["target_kit_sha"],
            "candidate_contract": request["candidate_contract"],
            "source_state": "Backlog",
            "source_ticket_blob": evidence[ticket]["source_ticket_blob"],
            "pr": evidence[ticket]["pr"],
            "checks": evidence[ticket]["checks"],
            "authorization_blob": authorization_blob,
            "cutoff": request["cutoff"],
            "protected_main_basis": basis,
        }
        files[f"{MIGRATION_DIR}/{ticket}.json"] = pretty(receipts[ticket])
    candidate = temporary_candidate(repo, basis_commit, files, request["cutoff"])
    validate_generated_dependency_batch(repo, authorization, receipts, candidate)
    payload = {
        "authorization": authorization,
        "files": files,
        "receipts": receipts,
    }
    return {
        "approval_sha256": hashlib.sha256(canonical(payload).encode()).hexdigest(),
        "candidate_commit": candidate,
        "payload": payload,
    }


def apply_plan(plan, approval):
    if not SHA256.fullmatch(approval) or approval != plan["approval_sha256"]:
        raise ValidationError("dependency fulfillment approval hash does not match")
    for relative, content in plan["payload"]["files"].items():
        path = Path(relative)
        if path.is_symlink() or (
            relative != "factory/KIT_PIN" and path.exists()
        ):
            raise ValidationError(f"dependency fulfillment output already exists: {relative}")
    ordered = sorted(
        plan["payload"]["files"],
        key=lambda relative: relative == "factory/KIT_PIN",
    )
    for relative in ordered:
        content = plan["payload"]["files"][relative]
        path = Path(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("plan", "apply"))
    parser.add_argument("--product", required=True)
    parser.add_argument("--request", required=True)
    parser.add_argument("--approve-hash")
    args = parser.parse_args()
    if (args.action == "apply") != (args.approve_hash is not None):
        parser.error("apply requires --approve-hash and plan forbids it")
    product = Path(args.product).resolve()
    plan = prepare(product, Path(args.request).resolve())
    if args.action == "apply":
        previous = Path.cwd()
        try:
            os.chdir(product)
            apply_plan(plan, args.approve_hash)
        finally:
            os.chdir(previous)
    print(
        json.dumps(
            {
                "action": args.action,
                "approval_sha256": plan["approval_sha256"],
                "candidate_commit": plan["candidate_commit"],
                "files": sorted(plan["payload"]["files"]),
                "status": "ok",
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValidationError) as error:
        print(f"dependency-fulfillment: {error}", file=sys.stderr)
        raise SystemExit(1)
