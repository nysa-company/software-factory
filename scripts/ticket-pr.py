#!/usr/bin/env python3
"""Create or reuse the exact ticket PR and gate review evidence on its checks."""

from __future__ import annotations

import argparse
import base64
import csv
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
from effective_ticket import ticket_branch_prefix  # noqa: E402
from approval_evidence import (  # noqa: E402
    ApprovalEvidenceError,
    trusted_approval_continuation_paths,
)
from narrator_evidence import (  # noqa: E402
    MAX_NARRATOR_EVIDENCE_BYTES,
    MAX_NARRATOR_EVIDENCE_FILES,
    PNG_END,
    PNG_SIGNATURE,
    trusted_narrator_evidence_paths,
)
from runtime_paths import canonical_factory_file  # noqa: E402
from refresh_semantics import (  # noqa: E402
    ClassificationError,
    preserved_control_paths,
    retained_control_paths,
)

SCHEMA = "nysa.software-factory.ticket-pr/v1"
REFRESH_RECEIPT_KEYS = {
    "schema", "ticket", "generation", "old_head", "base_head", "merge_head",
    "prior_reviewer_runs", "prior_approve_verdicts",
    "prior_request_changes_verdicts", "prior_narrator_runs",
    "prior_bundle_blob", "prior_approval_blob", "refreshed_at",
}


class Refusal(ValueError):
    pass


def run(command: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess:
    result = subprocess.run(command, cwd=cwd, text=True, capture_output=True, check=False)
    if result.returncode:
        raise Refusal(result.stderr.strip() or result.stdout.strip() or "command failed")
    return result


def git(root: Path, *arguments: str) -> str:
    command = ["git", "-C", str(root), *arguments]
    try:
        return run(command).stdout.strip()
    except Refusal:
        if arguments[0] != "ls-remote":
            raise
        return run(command).stdout.strip()


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


def project_auto_merge_method(factory: Path) -> str:
    values = []
    for raw in (factory / "PROJECT.env").read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(
            r"(?:export\s+)?AUTO_MERGE_METHOD\s*=\s*['\"]?(squash|merge|rebase)['\"]?",
            raw.strip(),
        )
        if match:
            values.append(match.group(1))
    if len(values) != 1:
        raise Refusal("AUTO_MERGE_METHOD is missing or ambiguous")
    return values[0]


def latest_reviewer_head(product: Path, workdir: Path, ticket: str) -> str:
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
    configured_ledger = os.environ.get("FACTORY_LEDGER", "")
    ledger = (
        Path(configured_ledger)
        if configured_ledger
        else canonical_factory_file(workdir, "runtime-ledger.csv")
    )
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


def unique_json_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate key")
        value[key] = item
    return value


def preserved_refresh_metadata(
    workdir: Path, ticket: str, reviewed: str, head: str, changed: set[str],
) -> set[str]:
    if (
        os.environ.get("FACTORY_RELEASE_CONTRACT_VERSION") != "1.8.0"
        or not re.fullmatch(
            r"[0-9a-f]{64}",
            os.environ.get("FACTORY_TRANSITION_RECEIPT_SHA256", ""),
        )
    ):
        return set()
    relative = f"factory/attestations/{ticket}/refresh.json"
    path = workdir / relative
    if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
        return set()
    try:
        receipt = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=unique_json_object,
        )
        if not isinstance(receipt, dict):
            return set()
        counts = [
            receipt.get(name) for name in (
                "prior_reviewer_runs", "prior_approve_verdicts",
                "prior_request_changes_verdicts", "prior_narrator_runs",
            )
        ]
        if (
            set(receipt) != REFRESH_RECEIPT_KEYS
            or receipt.get("schema") != "nysa.software-factory.ticket-refresh/v1"
            or receipt.get("ticket") != ticket
            or isinstance(receipt.get("generation"), bool)
            or not isinstance(receipt.get("generation"), int)
            or receipt["generation"] < 1
            or any(isinstance(value, bool) or not isinstance(value, int) or value < 0
                   for value in counts)
            or counts[0] != counts[1] + counts[2]
            or any(
                not re.fullmatch(r"[0-9a-f]{40}", receipt.get(name, ""))
                for name in ("old_head", "base_head", "merge_head")
            )
            or any(
                value is not None
                and not re.fullmatch(r"[0-9a-f]{40}", value)
                for value in (
                    receipt.get("prior_bundle_blob"),
                    receipt.get("prior_approval_blob"),
                )
            )
            or not isinstance(receipt.get("refreshed_at"), str)
            or not receipt["refreshed_at"].endswith("Z")
        ):
            return set()
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return set()
    receipt_commit = git(workdir, "log", "-1", "--format=%H", head, "--", relative)
    if (
        git(workdir, "rev-list", "--parents", "-n", "1", receipt_commit).split()
        != [receipt_commit, receipt["merge_head"]]
        or git(
            workdir, "rev-list", "--parents", "-n", "1", receipt["merge_head"],
        ).split()
        != [
            receipt["merge_head"], receipt["old_head"], receipt["base_head"],
        ]
    ):
        return set()
    for ancestor, descendant in (
        (reviewed, receipt["old_head"]),
        (receipt_commit, head),
    ):
        if subprocess.run(
            ["git", "-C", str(workdir), "merge-base", "--is-ancestor",
             ancestor, descendant],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        ).returncode:
            return set()
    try:
        preserved = preserved_control_paths(
            workdir, receipt["old_head"], receipt["base_head"],
        )
    except ClassificationError:
        return set()
    if preserved is None:
        return set()
    return {
        relative,
        *retained_control_paths(workdir, head, receipt["base_head"], changed),
    }


def validate_review_lineage(product: Path, workdir: Path, ticket: str, head: str) -> None:
    reviewed = latest_reviewer_head(product, workdir, ticket)
    run(["git", "-C", str(workdir), "merge-base", "--is-ancestor", reviewed, head])
    changed = set(git(workdir, "diff", "--name-only", f"{reviewed}..{head}").splitlines())
    route_path = f"factory/route-plans/{ticket}.json"
    trusted_metadata = {
        route_path,
        f"factory/attestations/{ticket}/bundle.json",
        f"factory/tickets/{ticket}-bundle.md",
        f"factory/tickets/{ticket}.md",
    }
    trusted_metadata.update(
        preserved_refresh_metadata(workdir, ticket, reviewed, head, changed)
    )
    trusted_metadata.update(
        trusted_narrator_evidence_paths(workdir, ticket, reviewed, head, changed)
    )
    approval_path = f"factory/attestations/{ticket}/approval.json"
    if approval_path in changed:
        ticket_text = git(workdir, "show", f"{head}:factory/tickets/{ticket}.md")
        kit_shas = re.findall(r"^Kit-SHA:\s*([0-9a-f]{40})\s*$", ticket_text, re.MULTILINE)
        if len(kit_shas) != 1:
            raise Refusal("approval continuation Kit-SHA is missing or ambiguous")
        try:
            trusted_metadata.update(trusted_approval_continuation_paths(
                workdir,
                ticket,
                project_repo(product / "factory"),
                ticket_branch_prefix(product / "factory") + ticket,
                kit_shas[0],
                project_auto_merge_method(product / "factory"),
                reviewed,
                head,
                changed,
            ))
        except ApprovalEvidenceError as error:
            raise Refusal(str(error)) from error
    if changed - trusted_metadata:
        raise Refusal("ticket implementation changed after the latest successful review")
    if route_path not in changed:
        return

    state_root = os.environ.get("FACTORY_MODEL_STATE_ROOT", "")
    project = os.environ.get("FACTORY_PROJECT", "")
    ticket_text = (workdir / f"factory/tickets/{ticket}.md").read_text(encoding="utf-8")
    kit_shas = re.findall(r"^Kit-SHA:\s*([0-9a-f]{40})\s*$", ticket_text, re.MULTILINE)
    if not Path(state_root).is_absolute() or not project or len(kit_shas) != 1:
        raise Refusal("route migration validation environment is missing")
    release_sha = kit_shas[0]
    manager = Path(__file__).resolve().parent / "model-manager.py"
    route_file = workdir / route_path
    command = [
        sys.executable, "-B", str(manager), "select",
        "--state-root", state_root,
        "--project", project,
        "--ticket-plan", str(route_file),
        "--ticket", ticket,
        "--kit-sha", release_sha,
        "--role", "narrator",
    ]
    policy = os.environ.get("FACTORY_MODEL_POLICY_FILE", "")
    if policy:
        command.extend(["--policy-file", policy])
    run(command)

    prior_blob = run([
        "git", "-C", str(workdir), "show", f"{reviewed}:{route_path}",
    ]).stdout.encode("utf-8")
    try:
        prior = json.loads(prior_blob)
        current = json.loads(route_file.read_text(encoding="utf-8"))
        revisions = current["revisions"]
        if prior.get("schema") == "ticket-model-route-journal/v2":
            prefix = prior["revisions"]
            suffix = revisions[len(prefix):]
            valid_lineage = revisions[:len(prefix)] == prefix
        elif prior.get("schema") == "ticket-model-route-plan/v1":
            prefix = []
            suffix = revisions[1:]
            valid_lineage = base64.b64decode(
                revisions[0]["body"]["legacy_plan_b64"], validate=True
            ) == prior_blob
        else:
            valid_lineage = False
            suffix = []
    except (KeyError, TypeError, ValueError, json.JSONDecodeError, OSError):
        valid_lineage = False
        suffix = []
    if (
        not valid_lineage
        or not suffix
        or any(item.get("body", {}).get("kind") != "release-migration" for item in suffix)
    ):
        raise Refusal("post-review route migration lineage is invalid")


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
    if (
        result.returncode == 1
        and not result.stdout.strip()
        and re.fullmatch(
            r"no (?:required )?checks reported on the '[^'\r\n]+' branch",
            result.stderr.strip(),
        )
    ):
        return "wait", ["required checks not reported"]
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


def railway_preview_evidence(
    repo: str, number: int, branch: str, head: str,
) -> tuple[list[str], dict[str, object]]:
    result = run([
        "gh", "pr", "view", str(number), "--repo", repo,
        "--json", "comments",
    ])
    try:
        value = json.loads(result.stdout)
        comments = value["comments"]
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise Refusal("GitHub returned invalid preview evidence") from error
    if not isinstance(comments, list):
        raise Refusal("GitHub returned invalid preview evidence")
    bodies = []
    for comment in comments:
        if not isinstance(comment, dict):
            raise Refusal("GitHub returned malformed preview evidence")
        author = comment.get("author")
        body = comment.get("body")
        if not (
            isinstance(author, dict)
            and author.get("login") == "railway-app"
            and isinstance(body, str)
        ):
            continue
        if "railway-bot-comment-version=2" in body:
            bodies.append(body)
    if not bodies:
        return [], {
            "expected": head, "observed": [], "reason": "not_reported",
            "status": "wait",
        }
    body = bodies[-1]
    rows = re.findall(
        r"^\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*"
        r"\[Web\]\((https://[^\s()]+)\)\s*\|[^\n]*$",
        body,
        re.M,
    )
    if not rows:
        raise Refusal("Railway preview comment has no service evidence")
    urls = []
    deployments = []
    railway = Path.home() / ".railway/bin/railway"
    if not railway.is_file() or not os.access(railway, os.X_OK):
        return [], {
            "expected": head, "observed": [], "reason": "cli_unavailable",
            "status": "wait",
        }
    for service, status_cell, candidate in rows:
        parsed = urlsplit(candidate)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or not parsed.hostname.endswith(".up.railway.app")
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in ("", "/")
        ):
            raise Refusal("Railway preview URL is malformed")
        urls.append(candidate.rstrip("/"))
        links = re.findall(r"\[View Logs\]\((https://[^\s()]+)\)", status_cell)
        if len(links) != 1:
            raise Refusal("Railway preview deployment link is malformed")
        detail = urlsplit(links[0])
        match = re.fullmatch(
            r"/project/([0-9a-f-]{36})/service/([0-9a-f-]{36})", detail.path
        )
        query = parse_qs(detail.query, strict_parsing=True)
        if (
            detail.scheme != "https"
            or detail.hostname != "railway.com"
            or not match
            or set(query) != {"id", "environmentId"}
            or any(len(values) != 1 for values in query.values())
            or not all(
                re.fullmatch(r"[0-9a-f-]{36}", query[name][0])
                for name in ("id", "environmentId")
            )
        ):
            raise Refusal("Railway preview deployment link is malformed")
        project, service_id = match.groups()
        deployment_id = query["id"][0]
        environment = query["environmentId"][0]
        result = subprocess.run(
            [
                str(railway), "deployment", "list", "-p", project,
                "-e", environment, "-s", service_id, "--limit", "100", "--json",
            ],
            text=True, capture_output=True, check=False,
        )
        if result.returncode:
            return urls, {
                "expected": head, "observed": deployments,
                "reason": "cli_unavailable", "status": "wait",
            }
        try:
            values = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise Refusal("Railway returned invalid deployment evidence") from error
        if not isinstance(values, list):
            raise Refusal("Railway returned invalid deployment evidence")
        matching = [
            item for item in values
            if isinstance(item, dict) and item.get("id") == deployment_id
        ]
        if len(matching) != 1:
            deployments.append({"service": service.strip(), "sha": None})
            continue
        deployment = matching[0]
        metadata = deployment.get("meta")
        if not isinstance(metadata, dict):
            raise Refusal("Railway returned malformed deployment evidence")
        observed = metadata.get("commitHash")
        if observed is not None and not re.fullmatch(r"[0-9a-f]{40}", observed):
            raise Refusal("Railway returned malformed deployment commit")
        deployments.append({
            "deployment_id": deployment_id,
            "service": service.strip(),
            "sha": observed,
            "status": deployment.get("status"),
        })
        if metadata.get("repo") != repo or metadata.get("branch") != branch:
            raise Refusal("Railway deployment repository or branch is invalid")
    exact = bool(deployments) and len(deployments) == len(rows) and all(
        item.get("sha") == head and item.get("status") == "SUCCESS"
        for item in deployments
    )
    return list(dict.fromkeys(urls)), {
        "expected": head,
        "observed": deployments,
        "reason": None if exact else "stale_or_pending",
        "status": "pass" if exact else "wait",
    }


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
        contract = os.environ.get("FACTORY_RELEASE_CONTRACT_VERSION", "")
        if lease_id and not re.fullmatch(r"[0-9a-f]{64}", lease_id):
            raise Refusal("dispatcher lease is invalid")
        if contract == "1.8.0":
            stage = os.environ.get("FACTORY_TRANSITION_STAGE", "")
            if not re.fullmatch(
                r"(?:RUN (?:reviewer|narrator)|AWAIT-(?:OPERATOR|MERGE) .+)",
                stage,
            ):
                raise Refusal("transition receipt does not authorize ticket PR verification")
        else:
            stage_command = [
                "bash", str(Path(__file__).resolve().parent / "next-stage.sh"),
                "--ticket", args.ticket,
            ]
            if lease_id:
                stage_command.extend(["--lease", lease_id])
            stage_command.extend(["--workdir", str(workdir)])
            stage = run(stage_command).stdout.strip()
        if stage.startswith("RUN reviewer"):
            boundary = "reviewer"
        elif stage.startswith("RUN narrator"):
            boundary = "narrator"
        elif stage.startswith(("AWAIT-OPERATOR", "AWAIT-MERGE")):
            boundary = "publication"
        else:
            raise Refusal("ticket PR verification requires the reviewer or narrator stage")
        if boundary in {"narrator", "publication"}:
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
        preview_urls = []
        preview_identity: dict[str, object] | None = None
        if (
            boundary in {"narrator", "publication"}
            and check_status == "pass"
        ):
            preview_urls, preview_identity = railway_preview_evidence(
                repo, pr["number"], branch, head,
            )
            if preview_identity["status"] != "pass":
                check_status = "wait"
                checks = [f"preview identity {preview_identity['reason']}"]
        if (
            boundary in {"narrator", "publication"}
            and check_status == "pass"
            and not preview_urls
        ):
            check_status = "wait"
            checks = ["preview deployment not reported"]
        status = (
            "ready" if boundary in {"narrator", "publication"} and check_status == "pass"
            else "prepared" if check_status == "pass"
            else check_status
        )
        print(json.dumps({
            "boundary": boundary,
            "branch": branch,
            "checks": checks,
            "head": head,
            "pr_number": pr["number"],
            "preview_urls": preview_urls,
            "preview_identity": preview_identity,
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
