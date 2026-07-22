#!/usr/bin/env python3
"""Create or reuse the exact ticket PR and gate review evidence on its checks."""

from __future__ import annotations

import argparse
import csv
import json
import os
from pathlib import Path
import re
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
from effective_ticket import ticket_branch_prefix  # noqa: E402

SCHEMA = "nysa.software-factory.ticket-pr/v1"


class Refusal(ValueError):
    pass


def run(command: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess:
    result = subprocess.run(command, cwd=cwd, text=True, capture_output=True, check=False)
    if result.returncode:
        raise Refusal(result.stderr.strip() or result.stdout.strip() or "command failed")
    return result


def git(root: Path, *arguments: str) -> str:
    return run(["git", "-C", str(root), *arguments]).stdout.strip()


def project_repo(factory: Path) -> str:
    values = []
    for raw in (factory / "PROJECT.env").read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(
            r"(?:export\s+)?GH_REPO\s*=\s*['\"]?([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)['\"]?",
            raw.strip(),
        )
        if match:
            values.append(match.group(1))
    if len(values) != 1:
        raise Refusal("GH_REPO is missing or ambiguous")
    return values[0]


def latest_reviewer_head(product: Path, ticket: str) -> str:
    runs = product / "factory" / "runs"
    if not runs.is_dir() or runs.is_symlink():
        raise Refusal("reviewer run evidence is missing")
    reviewers = {}
    for path in sorted(runs.glob("*.meta")):
        if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
            raise Refusal("reviewer run evidence is unsafe")
        values = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition("=")
            if not separator or key in values:
                raise Refusal("reviewer run evidence is malformed")
            values[key] = value
        if (
            values.get("ticket") == ticket
            and values.get("role") == "reviewer"
            and values.get("phase") == "completed"
            and values.get("accounting_schema") == "1"
            and values.get("accounting_state") in {"completed", "abandoned_conservative"}
            and values.get("go_issued") == "1"
            and values.get("task_submitted") == "1"
            and values.get("exit_status") == "0"
            and values.get("role_exit") == "ok"
            and (
                values.get("accounting_state") != "abandoned_conservative"
                or values.get("cost_basis") == "conservative_reservation"
            )
        ):
            run_id = values.get("run_id", "")
            if not run_id or run_id in reviewers:
                raise Refusal("reviewer run evidence is ambiguous")
            reviewers[run_id] = values
    ledger = Path(os.environ.get(
        "FACTORY_LEDGER", product / "factory" / "runtime-ledger.csv"
    ))
    if not ledger.is_file() or ledger.is_symlink():
        raise Refusal("reviewer ledger evidence is missing")
    with ledger.open(newline="", encoding="utf-8") as handle:
        rows = [
            row for row in csv.DictReader(handle)
            if row.get("ticket") == ticket
            and row.get("role") == "reviewer"
            and row.get("exit_status") == "0"
        ]
    if not rows:
        raise Refusal("successful reviewer run evidence is missing")
    run_id = rows[-1].get("run_id", "")
    if run_id not in reviewers:
        raise Refusal("latest successful reviewer manifest is missing")
    reviewer = reviewers[run_id]
    if reviewer.get("cost_basis") != rows[-1].get("cost_basis"):
        raise Refusal("latest successful reviewer ledger evidence does not match")
    head = reviewer.get("role_head_before", "")
    if not re.fullmatch(r"[0-9a-f]{40}", head):
        raise Refusal("reviewer head evidence is invalid")
    return head


def validate_review_lineage(product: Path, workdir: Path, ticket: str, head: str) -> None:
    reviewed = latest_reviewer_head(product, ticket)
    run(["git", "-C", str(workdir), "merge-base", "--is-ancestor", reviewed, head])
    changed = set(git(workdir, "diff", "--name-only", f"{reviewed}..{head}").splitlines())
    if changed - {f"factory/tickets/{ticket}.md"}:
        raise Refusal("ticket implementation changed after the latest successful review")


def required_check_status(repo: str, number: int) -> tuple[str, list[str]]:
    result = subprocess.run(
        [
            "gh", "pr", "checks", str(number), "--repo", repo, "--required",
            "--json", "name,state,bucket",
        ],
        text=True, capture_output=True, check=False,
    )
    if result.returncode not in (0, 1, 8):
        raise Refusal(result.stderr.strip() or "GitHub required-check query failed")
    try:
        checks = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise Refusal("GitHub returned invalid required-check evidence") from error
    if not isinstance(checks, list):
        raise Refusal("GitHub returned invalid required-check evidence")
    if not checks:
        return "wait", ["required checks not reported"]
    if any(
        not isinstance(item, dict)
        or not isinstance(item.get("name"), str)
        or not item["name"]
        for item in checks
    ):
        raise Refusal("GitHub returned malformed required-check evidence")
    buckets = {item.get("bucket") for item in checks if isinstance(item, dict)}
    if len(buckets) == 0 or not buckets <= {"pass", "fail", "pending", "skipping", "cancel"}:
        raise Refusal("GitHub returned unknown required-check state")
    if buckets & {"pending"}:
        return "wait", sorted(str(item.get("name")) for item in checks if item.get("bucket") == "pending")
    if buckets & {"fail", "skipping", "cancel"}:
        return "failed", sorted(
            str(item.get("name")) for item in checks
            if item.get("bucket") in {"fail", "skipping", "cancel"}
        )
    return "pass", []


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ticket", required=True)
    parser.add_argument("--workdir", required=True, type=Path)
    args = parser.parse_args()
    try:
        if not re.fullmatch(r"T-[0-9]+", args.ticket):
            raise Refusal("invalid ticket identifier")
        product = Path(os.environ["FACTORY_ROOT"]).resolve(strict=True)
        workdir = args.workdir.resolve(strict=True)
        factory = product / "factory"
        branch = ticket_branch_prefix(factory) + args.ticket
        expected_origin = os.environ.get("FACTORY_CERTIFIED_PRODUCT_ORIGIN", "")
        origins = git(product, "remote", "get-url", "--push", "--all", "origin").splitlines()
        if not expected_origin or origins != [expected_origin]:
            raise Refusal("product origin does not match certification")
        if git(workdir, "symbolic-ref", "--quiet", "--short", "HEAD") != branch:
            raise Refusal("ticket worktree is on the wrong branch")
        if git(workdir, "status", "--porcelain", "--untracked-files=all"):
            raise Refusal("ticket worktree is dirty")
        head = git(workdir, "rev-parse", "HEAD")
        remote = run(
            ["git", "ls-remote", "--heads", expected_origin, f"refs/heads/{branch}"]
        ).stdout.split()
        if not remote or remote[0] != head:
            raise Refusal("ticket branch is not pushed at the exact local head")
        lease_id = os.environ.get("FACTORY_DISPATCH_LEASE_ID", "")
        stage_command = [
            "bash", str(Path(__file__).resolve().parent / "next-stage.sh"),
            "--ticket", args.ticket,
        ]
        if lease_id:
            if not re.fullmatch(r"[0-9a-f]{64}", lease_id):
                raise Refusal("dispatcher lease is invalid")
            stage_command.extend(["--lease", lease_id])
        stage_command.extend(["--workdir", str(workdir)])
        stage = run(stage_command).stdout.strip()
        if stage.startswith("RUN reviewer"):
            boundary = "reviewer"
        elif stage.startswith("RUN narrator"):
            boundary = "narrator"
        else:
            raise Refusal("ticket PR verification requires the reviewer or narrator stage")
        if boundary == "narrator":
            validate_review_lineage(product, workdir, args.ticket, head)
        repo = project_repo(factory)
        fields = "number,headRefName,baseRefName,headRefOid,url,state"

        def candidates() -> list[dict]:
            value = json.loads(run([
                "gh", "pr", "list", "--repo", repo, "--state", "all",
                "--head", branch, "--base", "main", "--json", fields,
            ]).stdout)
            if not isinstance(value, list):
                raise Refusal("GitHub returned invalid ticket PR evidence")
            return value

        prs = candidates()
        if not prs:
            run([
                "gh", "pr", "create", "--repo", repo, "--head", branch,
                "--base", "main", "--title", f"{args.ticket}: implementation",
                "--body", "Factory ticket implementation. Approval and merge remain protected.",
            ])
            prs = candidates()
        if len(prs) != 1:
            raise Refusal("expected exactly one PR for the exact ticket branch")
        pr = prs[0]
        if (
            pr.get("headRefName") != branch
            or pr.get("baseRefName") != "main"
            or pr.get("headRefOid") != head
            or pr.get("state") != "OPEN"
            or not isinstance(pr.get("number"), int)
            or pr["number"] <= 0
        ):
            raise Refusal("ticket PR branch, base, head, or state is invalid")
        check_status, checks = required_check_status(repo, pr["number"])
        status = (
            "ready" if boundary == "narrator" and check_status == "pass"
            else "prepared" if check_status == "pass"
            else check_status
        )
        print(json.dumps({
            "boundary": boundary,
            "branch": branch,
            "checks": checks,
            "head": head,
            "pr_number": pr["number"],
            "schema": SCHEMA,
            "status": status,
            "ticket": args.ticket,
            "url": pr.get("url"),
        }, sort_keys=True, separators=(",", ":")))
    except (
        FileNotFoundError, json.JSONDecodeError, KeyError, OSError,
        Refusal, subprocess.SubprocessError, UnicodeError, ValueError,
    ) as error:
        print(json.dumps({
            "error": str(error), "schema": SCHEMA, "status": "error",
        }, sort_keys=True, separators=(",", ":")))
        raise SystemExit(2)


if __name__ == "__main__":
    main()
