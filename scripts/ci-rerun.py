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
import tempfile
from typing import Any


SCHEMA = "nysa.software-factory.ci-rerun/v1"
TICKET = re.compile(r"^T-[0-9]+$")
SHA = re.compile(r"^[0-9a-f]{40}$")
PROTECTED = re.compile(
    r"policy|secur|secret|config|control|immutable|hermes|factory|contract|license",
    re.I,
)
APPLICATION = re.compile(r"test|unit|integration|e2e|application", re.I)


class RerunError(ValueError):
    pass


class NotTransient(RerunError):
    pass


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def classify(checks: list[dict[str, Any]], repo: str) -> tuple[int, int, str]:
    if not isinstance(checks, list) or not checks:
        raise NotTransient("required checks are unavailable")
    failed = [
        item for item in checks
        if isinstance(item, dict) and item.get("bucket") in {"fail", "cancel"}
    ]
    if len(failed) != 1:
        raise NotTransient("exactly one failed application-test job is required")
    if any(
        not isinstance(item, dict)
        or item.get("bucket") not in {"pass", "fail", "cancel"}
        for item in checks
    ):
        raise NotTransient("required checks are not terminal")
    protected = [
        item for item in checks
        if isinstance(item.get("name"), str) and PROTECTED.search(item["name"])
    ]
    if not protected or any(item.get("bucket") != "pass" for item in protected):
        raise NotTransient("protected CI classes are not proven green")
    selected = failed[0]
    name = selected.get("name", "")
    if (
        not isinstance(name, str)
        or PROTECTED.search(name)
        or not APPLICATION.search(name)
    ):
        raise NotTransient("failed check is not an application-test transient")
    link = selected.get("link", "")
    match = re.fullmatch(
        rf"https://github[.]com/{re.escape(repo)}/actions/runs/([0-9]+)/job/([0-9]+)",
        link if isinstance(link, str) else "",
    )
    if not match:
        raise NotTransient("failed check lacks exact GitHub job identity")
    return int(match.group(1)), int(match.group(2)), name


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
    result = subprocess.run(
        ["gh", *arguments], text=True, capture_output=True, check=False, timeout=120
    )
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
        checks = json.loads(gh(
            "pr", "checks", str(args.pr), "--repo", repo, "--required",
            "--json", "name,bucket,link",
        ))
        run_id, job_id, name = classify(checks, repo)
        record = reruns / f"{args.ticket}-{head}.json"
        if record.exists() or record.is_symlink():
            print(canonical({
                "reason": "same_head_rerun_already_used", "schema": SCHEMA,
                "status": "refused", "ticket": args.ticket,
            }))
            return
        result = subprocess.run(
            ["gh", "run", "rerun", str(run_id), "--repo", repo, "--failed"],
            text=True, capture_output=True, check=False, timeout=120,
        )
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
        })
        print(canonical({
            "head": head, "job_id": job_id, "run_id": run_id,
            "schema": SCHEMA, "status": "rerun", "ticket": args.ticket,
        }))
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
