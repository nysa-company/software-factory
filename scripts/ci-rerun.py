#!/usr/bin/env python3
"""Authorize one exact-head rerun for an application-test transient only."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
from external_transport import temporarily_unavailable  # noqa: E402


SCHEMA = "nysa.software-factory.ci-rerun/v1"
TICKET = re.compile(r"^T-[0-9]+$")
SHA = re.compile(r"^[0-9a-f]{40}$")
PROTECTED = re.compile(
    r"policy|secur|secret|config|control|immutable|runtime|factory|contract|license",
    re.I,
)
APPLICATION = re.compile(r"test|unit|integration|e2e|application", re.I)
CHECK_FAILURE = {"fail", "cancel"}
RUN_FAILURE = {
    "action_required", "cancelled", "failure", "stale", "startup_failure",
    "timed_out",
}
RUN_TERMINAL = RUN_FAILURE | {"neutral", "skipped", "success"}
AGGREGATE_CONTROL = re.compile(
    r"^(?:Set up job|Run actions/(?:checkout|setup-node)@v[45]|"
    r"inspect qualification control|classify change|pin and product contract|"
    r"Post Run actions/(?:checkout|setup-node)@v[45]|Complete job)$",
    re.I,
)
AGGREGATE_APPLICATION = re.compile(
    r"^(?:npm ci|install(?: dependencies| packages)?|"
    r"(?:(?:run|targeted|product|web|api|app|application|unit|integration|"
    r"e2e|browser|server|client|frontend|backend)[ -])?"
    r"(?:tests?|lint|type[ -]?check|build|compile|playwright(?: snapshots?)?|"
    r"snapshots?))$",
    re.I,
)


class RerunError(ValueError):
    pass


class NotTransient(RerunError):
    pass


class ExternalUnavailable(RerunError):
    pass


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def job_identity(item: dict[str, Any], repo: str) -> tuple[int, int, str, str]:
    name = item.get("name", "")
    workflow = item.get("workflow", "")
    link = item.get("link", "")
    if not isinstance(name, str) or not isinstance(workflow, str) or not workflow:
        raise NotTransient("failed check identity is incomplete")
    match = re.fullmatch(
        rf"https://github[.]com/{re.escape(repo)}/actions/runs/([0-9]+)/job/([0-9]+)",
        link if isinstance(link, str) else "",
    )
    if not match:
        raise NotTransient("failed check lacks exact GitHub job identity")
    return int(match.group(1)), int(match.group(2)), name, workflow


def classify(
    required: list[dict[str, Any]], checks: list[dict[str, Any]], repo: str,
) -> tuple[int, int, str, str, tuple[int, ...]]:
    if not isinstance(required, list) or not required:
        raise NotTransient("required checks are unavailable")
    if any(
        not isinstance(item, dict)
        or item.get("bucket") not in {"pass", *CHECK_FAILURE}
        for item in required
    ):
        raise NotTransient("required checks are not terminal")
    if not isinstance(checks, list) or not checks:
        raise NotTransient("complete checks are unavailable")
    for item in required:
        if sum(item == observed for observed in checks) != 1:
            raise NotTransient("required check identity changed")
    protected = [
        item for item in checks
        if isinstance(item, dict) and isinstance(item.get("name"), str)
        and PROTECTED.search(item["name"])
    ]
    if not protected or any(item.get("bucket") != "pass" for item in protected):
        raise NotTransient("protected CI classes are not proven green")
    failed = [
        item for item in checks
        if isinstance(item, dict) and item.get("bucket") in CHECK_FAILURE
    ]
    if len(failed) not in {1, 2}:
        raise NotTransient("exactly one failed application leaf is required")
    identities = [job_identity(item, repo) for item in failed]
    if len({(run, job) for run, job, _name, _workflow in identities}) != len(failed):
        raise NotTransient("failed check identity is ambiguous")
    runs = {run for run, _job, _name, _workflow in identities}
    workflows = {workflow for _run, _job, _name, workflow in identities}
    if len(runs) != 1 or len(workflows) != 1:
        raise NotTransient("failed checks do not share one workflow run")
    leaves = [
        identity for identity in identities
        if APPLICATION.search(identity[2]) and not PROTECTED.search(identity[2])
    ]
    required_failures = [
        job_identity(item, repo) for item in required
        if item.get("bucket") in CHECK_FAILURE
    ]
    if len(leaves) == 1:
        selected = leaves[0]
    elif (
        not leaves
        and len(identities) == 1
        and identities[0] in required_failures
        and identities[0][2:] == ("ci", "ci")
    ):
        selected = identities[0]
    else:
        raise NotTransient("failed check is not one application-test leaf")
    aggregate = [identity for identity in identities if identity != selected]
    if (
        not required_failures
        or any(identity not in identities for identity in required_failures)
        or aggregate and aggregate[0] not in required_failures
        or not aggregate and selected not in required_failures
    ):
        raise NotTransient("failed required check is not the bound aggregate")
    run, job, name, workflow = selected
    return run, job, name, workflow, tuple(sorted(item[1] for item in identities))


def validate_run(
    run: dict[str, Any], head: str,
    selected: tuple[int, int, str, str, tuple[int, ...]],
) -> None:
    run_id, job_id, name, workflow, failed_job_ids = selected
    if (
        not isinstance(run, dict)
        or run.get("databaseId") != run_id
        or run.get("event") != "pull_request"
        or run.get("headSha") != head
        or run.get("status") != "completed"
        or run.get("conclusion") not in RUN_FAILURE
        or run.get("workflowName") != workflow
    ):
        raise NotTransient("failed workflow run identity changed")
    jobs = run.get("jobs")
    if not isinstance(jobs, list) or not jobs or any(
        not isinstance(item, dict)
        or not isinstance(item.get("databaseId"), int)
        or item.get("status") != "completed"
        or item.get("conclusion") not in RUN_TERMINAL
        for item in jobs
    ):
        raise NotTransient("failed workflow jobs are not terminal")
    if len({item["databaseId"] for item in jobs}) != len(jobs):
        raise NotTransient("failed workflow job identity is ambiguous")
    failed = [item for item in jobs if item.get("conclusion") in RUN_FAILURE]
    if tuple(sorted(item["databaseId"] for item in failed)) != failed_job_ids:
        raise NotTransient("failed workflow jobs changed")
    leaf = [item for item in failed if item.get("databaseId") == job_id]
    if len(leaf) != 1 or leaf[0].get("name") != name:
        raise NotTransient("failed application leaf identity changed")
    if not APPLICATION.search(name):
        steps = leaf[0].get("steps")
        if not isinstance(steps, list) or not steps or any(
            not isinstance(item, dict)
            or not isinstance(item.get("number"), int)
            or not isinstance(item.get("name"), str)
            or item.get("status") != "completed"
            or item.get("conclusion") not in RUN_TERMINAL
            for item in steps
        ):
            raise NotTransient("aggregate workflow steps are not terminal")
        numbers = [item["number"] for item in steps]
        if len(set(numbers)) != len(steps) or numbers != sorted(numbers):
            raise NotTransient("aggregate workflow step identity is ambiguous")
        failures = [
            index for index, item in enumerate(steps)
            if item["conclusion"] in RUN_FAILURE
        ]
        names = [item["name"] for item in steps]
        safe_application = [
            bool(AGGREGATE_APPLICATION.fullmatch(item)) for item in names
        ]
        if len(failures) != 1 or not safe_application[failures[0]] or any(
            not AGGREGATE_CONTROL.fullmatch(item) and not safe
            for item, safe in zip(names, safe_application)
        ) or names[0] != "Set up job" or names[-1] != "Complete job" or (
            steps[0]["conclusion"] != "success"
            or steps[-1]["conclusion"] != "success"
        ):
            raise NotTransient("aggregate workflow is not the safe Factory CI template")
        failed_index = failures[0]
        v5 = [
            index for index, item in enumerate(names)
            if item == "Run actions/checkout@v5"
        ]
        if v5:
            if (
                len(v5) != 2
                or names.count("Set up job") != 1
                or names.count("inspect qualification control") != 1
                or names.count("classify change") != 1
                or names.count("Run actions/setup-node@v5") != 1
                or any("@v4" in item for item in names)
            ):
                raise NotTransient("aggregate workflow is not the safe Factory CI template")
            required_order = [
                names.index("Set up job"), v5[0],
                names.index("inspect qualification control"), v5[-1],
                names.index("classify change"),
                names.index("Run actions/setup-node@v5"), failed_index,
            ]
            if required_order != sorted(required_order) or any(
                steps[index]["conclusion"] != "success"
                for index in (
                    v5[0], names.index("inspect qualification control"),
                    names.index("classify change"),
                    names.index("Run actions/setup-node@v5"),
                )
            ) or steps[v5[-1]]["conclusion"] not in {"success", "skipped"}:
                raise NotTransient("aggregate workflow is not the safe Factory CI template")
        else:
            required = (
                "Set up job", "Run actions/checkout@v4",
                "Run actions/setup-node@v4", "pin and product contract",
                "Complete job",
            )
            if any(names.count(item) != 1 for item in required) or any(
                "@v5" in item for item in names
            ):
                raise NotTransient("aggregate workflow is not the safe Factory CI template")
            required_order = [
                names.index("Set up job"), names.index("Run actions/checkout@v4"),
                names.index("Run actions/setup-node@v4"), failed_index,
                names.index("pin and product contract"),
                names.index("Complete job"),
            ]
            if required_order != sorted(required_order) or any(
                steps[index]["conclusion"] != "success"
                for index in required_order[1:3]
            ) or steps[required_order[4]]["conclusion"] not in {
                "success", "skipped",
            }:
                raise NotTransient("aggregate workflow is not the safe Factory CI template")
    protected = [
        item for item in jobs
        if isinstance(item.get("name"), str) and PROTECTED.search(item["name"])
    ]
    if protected and any(item.get("conclusion") != "success" for item in protected):
        raise NotTransient("protected workflow jobs are not proven green")


def safe_directory(path: Path, create: bool = False) -> Path:
    if create:
        path.mkdir(mode=0o700, parents=False, exist_ok=True)
    info = path.lstat()
    if (
        path.resolve(strict=True) != path
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise RerunError("CI rerun directory is unsafe")
    return path


def write_once(path: Path, value: dict[str, Any]) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = -1
            stream.write(canonical(value) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        Path(temporary).unlink(missing_ok=True)


def project_repo(factory: Path) -> str:
    values = re.findall(
        r"^(?:export\s+)?GH_REPO\s*=\s*['\"]?([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)['\"]?\s*$",
        (factory / "PROJECT.env").read_text(encoding="utf-8"),
        re.M,
    )
    if len(values) != 1:
        raise RerunError("GH_REPO is missing or ambiguous")
    return values[0]


def gh(*arguments: str) -> str:
    try:
        result = subprocess.run(
            ["gh", *arguments], text=True, capture_output=True, check=False,
            timeout=120,
        )
    except subprocess.TimeoutExpired as error:
        raise ExternalUnavailable from error
    if result.returncode and temporarily_unavailable(
        result.stderr or result.stdout
    ):
        raise ExternalUnavailable
    if result.returncode not in (0, 1, 8):
        raise RerunError(result.stderr.strip() or "GitHub query failed")
    return result.stdout


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--factory-root", required=True, type=Path)
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--workdir", required=True, type=Path)
    parser.add_argument("--ticket", required=True)
    parser.add_argument("--pr", required=True, type=int)
    args = parser.parse_args()
    try:
        if not TICKET.fullmatch(args.ticket) or args.pr <= 0:
            raise RerunError("invalid CI rerun arguments")
        product = args.factory_root.resolve(strict=True)
        workdir = args.workdir.resolve(strict=True)
        state = safe_directory(args.state_dir)
        reruns = state / "ci-reruns"
        safe_directory(reruns, create=True)
        if subprocess.run(
            ["git", "-C", str(workdir), "status", "--porcelain", "--untracked-files=all"],
            text=True, capture_output=True, check=True,
        ).stdout:
            raise RerunError("CI rerun requires a clean ticket cell")
        head = subprocess.check_output(
            ["git", "-C", str(workdir), "rev-parse", "HEAD"], text=True
        ).strip()
        branch = subprocess.check_output(
            ["git", "-C", str(workdir), "symbolic-ref", "--quiet", "--short", "HEAD"],
            text=True,
        ).strip()
        if not SHA.fullmatch(head):
            raise RerunError("ticket head is invalid")
        repo = project_repo(product / "factory")
        pr = json.loads(gh(
            "pr", "view", str(args.pr), "--repo", repo, "--json",
            "number,headRefName,headRefOid,baseRefName,state",
        ))
        if (
            pr.get("number") != args.pr
            or pr.get("headRefName") != branch
            or pr.get("headRefOid") != head
            or pr.get("baseRefName") != "main"
            or pr.get("state") != "OPEN"
        ):
            raise RerunError("PR identity changed before CI rerun")
        required = json.loads(gh(
            "pr", "checks", str(args.pr), "--repo", repo, "--required",
            "--json", "name,bucket,link,workflow",
        ))
        checks = json.loads(gh(
            "pr", "checks", str(args.pr), "--repo", repo,
            "--json", "name,bucket,link,workflow",
        ))
        selected = classify(required, checks, repo)
        run_id, job_id, name, workflow, _failed_job_ids = selected
        run = json.loads(gh(
            "run", "view", str(run_id), "--repo", repo, "--json",
            "databaseId,event,headSha,jobs,status,conclusion,workflowName",
        ))
        validate_run(run, head, selected)
        record = reruns / f"{args.ticket}-{head}.json"
        if record.exists() or record.is_symlink():
            print(canonical({
                "reason": "same_head_rerun_already_used", "schema": SCHEMA,
                "status": "refused", "ticket": args.ticket,
            }))
            return
        try:
            result = subprocess.run(
                ["gh", "run", "rerun", str(run_id), "--repo", repo, "--failed"],
                text=True, capture_output=True, check=False, timeout=120,
            )
        except subprocess.TimeoutExpired as error:
            raise ExternalUnavailable from error
        if result.returncode and temporarily_unavailable(
            result.stderr or result.stdout
        ):
            raise ExternalUnavailable
        if result.returncode:
            raise RerunError(result.stderr.strip() or "GitHub rerun request failed")
        write_once(record, {
            "check_name": name,
            "head_sha": head,
            "job_id": job_id,
            "pr_number": args.pr,
            "run_id": run_id,
            "schema": SCHEMA,
            "ticket": args.ticket,
            "workflow": workflow,
        })
        print(canonical({
            "head": head, "job_id": job_id, "run_id": run_id,
            "schema": SCHEMA, "status": "rerun", "ticket": args.ticket,
        }))
    except ExternalUnavailable:
        print('{"reason_code":"external_unavailable","status":"wait"}')
        raise SystemExit(75)
    except NotTransient as error:
        print(canonical({
            "reason": str(error), "schema": SCHEMA, "status": "refused",
            "ticket": args.ticket,
        }))
    except (
        FileExistsError, FileNotFoundError, json.JSONDecodeError, OSError,
        RerunError, subprocess.SubprocessError,
    ) as error:
        print(canonical({
            "error": str(error), "schema": SCHEMA, "status": "error",
            "ticket": args.ticket,
        }))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
