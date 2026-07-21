#!/usr/bin/env python3
"""Create or reuse the exact ticket PR before independent review."""

from __future__ import annotations

import argparse
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
        stage = run(
            [
                "bash", str(Path(__file__).resolve().parent / "next-stage.sh"),
                "--ticket", args.ticket, "--workdir", str(workdir),
            ]
        ).stdout.strip()
        if not stage.startswith("RUN reviewer"):
            raise Refusal("ticket PR preparation requires the reviewer stage")
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
        print(json.dumps({
            "branch": branch,
            "head": head,
            "pr_number": pr["number"],
            "schema": SCHEMA,
            "status": "prepared",
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
