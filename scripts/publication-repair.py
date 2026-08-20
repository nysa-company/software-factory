#!/usr/bin/env python3
"""Create and resolve one authenticated Contract 1.8 publication repair."""

from __future__ import annotations

import argparse
import csv
import hashlib
import hmac
import importlib.util
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
from external_transport import remote_command, temporarily_unavailable  # noqa: E402


SCHEMA = "nysa.software-factory.publication-repair/v1"
TICKET = re.compile(r"^T-[0-9]+$")
SHA = re.compile(r"^[0-9a-f]{40}$")
DIGEST = re.compile(r"^[0-9a-f]{64}$")


class RepairError(ValueError):
    pass


class ExternalUnavailable(RepairError):
    pass


def canonical(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode()


def command(*arguments: str, cwd: Path | None = None) -> str:
    try:
        result = subprocess.run(
            arguments, cwd=cwd, text=True, capture_output=True, check=False,
            timeout=120,
        )
    except subprocess.TimeoutExpired as error:
        if remote_command(list(arguments)):
            raise ExternalUnavailable from error
        raise
    if result.returncode and remote_command(
        list(arguments)
    ) and temporarily_unavailable(result.stderr or result.stdout):
        raise ExternalUnavailable
    if result.returncode:
        raise RepairError(
            result.stderr.strip() or result.stdout.strip() or "publication repair failed"
        )
    return result.stdout.strip()


def module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if not spec or not spec.loader:
        raise RepairError(f"{name} is unavailable")
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def safe_directory(path: Path, create: bool = False) -> Path:
    if create:
        path.mkdir(mode=0o700, parents=False, exist_ok=True)
    info = path.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or path.is_symlink()
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise RepairError("publication repair directory is unsafe")
    return path


def write(path: Path, value: dict[str, Any]) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(canonical(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        Path(temporary).unlink(missing_ok=True)


def signed(value: dict[str, Any], secret: bytes) -> dict[str, Any]:
    result = dict(value)
    result["authentication_sha256"] = hmac.new(
        secret, canonical(value), hashlib.sha256
    ).hexdigest()
    result["repair_sha256"] = hashlib.sha256(canonical(result)).hexdigest()
    return result


def load(path: Path, secret: bytes) -> dict[str, Any]:
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or path.is_symlink()
        or info.st_uid != os.geteuid()
        or info.st_nlink != 1
        or stat.S_IMODE(info.st_mode) != 0o600
    ):
        raise RepairError("publication repair record is unsafe")
    value = json.loads(path.read_text(encoding="utf-8"))
    repair_digest = value.pop("repair_sha256", "")
    if repair_digest != hashlib.sha256(canonical(value)).hexdigest():
        raise RepairError("publication repair digest is invalid")
    authentication = value.pop("authentication_sha256", "")
    if not hmac.compare_digest(
        authentication, hmac.new(secret, canonical(value), hashlib.sha256).hexdigest()
    ):
        raise RepairError("publication repair authentication is invalid")
    value["authentication_sha256"] = authentication
    value["repair_sha256"] = repair_digest
    return value


def field(text: str, name: str) -> str:
    values = re.findall(
        rf"^{re.escape(name)}:\s*(.*?)\s*$", text, re.I | re.M
    )
    if len(values) != 1:
        raise RepairError(f"ticket {name} is ambiguous")
    return values[0]


def replace_field(text: str, name: str, value: str) -> str:
    return re.sub(
        rf"^({re.escape(name)}:)\s*.*$", rf"\1 {value}", text,
        count=1, flags=re.I | re.M,
    )


def push_head(
    workdir: Path, origin: str, branch: str, head: str, expected: str,
) -> None:
    failure: RepairError | None = None
    try:
        command(
            "git", "-C", str(workdir), "push",
            f"--force-with-lease=refs/heads/{branch}:{expected}", "--", origin,
            f"{head}:refs/heads/{branch}",
        )
    except RepairError as error:
        failure = error
    remote = command(
        "git", "ls-remote", "--heads", origin, f"refs/heads/{branch}",
    ).split()
    if len(remote) != 2 or remote[0] != head:
        if failure is not None:
            raise failure
        raise RepairError("publication repair push was not confirmed")
    tracking = command(
        "git", "-C", str(workdir), "rev-parse", "--verify",
        f"refs/remotes/origin/{branch}",
    )
    if tracking == expected:
        command(
            "git", "-C", str(workdir), "update-ref",
            f"refs/remotes/origin/{branch}", head, expected,
        )
    elif tracking != head:
        raise RepairError("publication repair tracking state changed")


def project_value(product: Path, name: str, pattern: str) -> str:
    values = re.findall(
        rf"^(?:export\s+)?{re.escape(name)}\s*=\s*['\"]?({pattern})['\"]?\s*$",
        (product / "factory/PROJECT.env").read_text(encoding="utf-8"),
        re.M,
    )
    if len(values) != 1:
        raise RepairError(f"{name} is missing or ambiguous")
    return values[0]


def ticket_branch(product: Path, ticket: str) -> str:
    values = re.findall(
        r"^(?:export\s+)?TICKET_BRANCH_PREFIX\s*=\s*['\"]?([^'\"\s]+)['\"]?\s*$",
        (product / "factory/PROJECT.env").read_text(encoding="utf-8"),
        re.M,
    )
    if len(values) > 1 or (
        values and not re.fullmatch(r"[A-Za-z0-9._/-]+", values[0])
    ):
        raise RepairError("ticket branch prefix is invalid")
    return (values[0] if values else "ticket/") + ticket


def verdicts(text: str) -> list[str]:
    return [
        "REQUEST CHANGES" if value.upper().startswith("REQUEST CHANGES") else "APPROVE"
        for value in re.findall(
            r"^\s*Reviewer round \d+:\s*(APPROVE|REQUEST CHANGES(?:\s+—\s+.*)?)\s*$",
            text, re.I | re.M,
        )
    ]


def decide(record: dict[str, Any], ticket_text: str, roles: list[str]) -> str:
    fixes = [index for index, role in enumerate(roles) if role in {"builder", "test-author"}]
    if not fixes:
        return f"FIX {record['repair_owner']}"
    last_fix = fixes[-1]
    reviewers = [
        index for index, role in enumerate(roles)
        if role == "reviewer" and index > last_fix
    ]
    if not reviewers:
        return "RUN reviewer"
    fresh = verdicts(ticket_text)[record["verdict_baseline"]:]
    if len(fresh) < len(reviewers):
        return "REFUSE publication repair reviewer verdict is not reconciled"
    if fresh[-1] == "REQUEST CHANGES":
        return "INACTIVE"
    last_reviewer = reviewers[-1]
    if not any(
        role == "narrator" and index > last_reviewer
        for index, role in enumerate(roles)
    ):
        return "RUN narrator"
    return "AWAIT-OPERATOR bundle posted; operator approval + merge is the next step"


def create(args: argparse.Namespace, secret: bytes) -> dict[str, Any]:
    product = args.factory_root.resolve(strict=True)
    workdir = args.workdir.resolve(strict=True)
    if command("git", "-C", str(workdir), "status", "--porcelain", "--untracked-files=all"):
        raise RepairError("publication repair requires a clean ticket cell")
    branch = command("git", "-C", str(workdir), "symbolic-ref", "--quiet", "--short", "HEAD")
    head = command("git", "-C", str(workdir), "rev-parse", "HEAD")
    if branch != ticket_branch(product, args.ticket) or not SHA.fullmatch(head):
        raise RepairError("publication repair ticket identity is invalid")
    origin = os.environ.get("FACTORY_CERTIFIED_PRODUCT_ORIGIN", "")
    if command("git", "-C", str(product), "remote", "get-url", "--push", "--all", "origin").splitlines() != [origin]:
        raise RepairError("certified product origin changed")
    repairs = safe_directory(args.state_dir / "publication-repairs", create=True)
    repair_path = repairs / f"{args.ticket}.json"
    if repair_path.exists() or repair_path.is_symlink():
        record = load(repair_path, secret)
        if (
            record.get("schema") != SCHEMA
            or record.get("ticket") != args.ticket
            or record.get("branch") != branch
            or record.get("factory_sha") != args.factory_sha
            or record.get("pr_number") != args.pr
            or record.get("reset_head") != head
            or command("git", "-C", str(workdir), "rev-parse", "HEAD^")
            != record.get("original_head")
        ):
            raise RepairError("publication repair replay changed")
        push_head(
            workdir, origin, branch, head, record["original_head"],
        )
        return record
    remote = command(
        "git", "ls-remote", "--heads", origin, f"refs/heads/{branch}"
    ).split()
    if len(remote) != 2 or remote[0] != head:
        raise RepairError("publication repair branch is not at its exact remote head")
    repo = project_value(product, "GH_REPO", r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
    pr = json.loads(command(
        "gh", "pr", "view", str(args.pr), "--repo", repo, "--json",
        "number,headRefName,headRefOid,baseRefName,state,isDraft,autoMergeRequest",
    ))
    if (
        pr.get("number") != args.pr
        or pr.get("headRefName") != branch
        or pr.get("headRefOid") != head
        or pr.get("baseRefName") != "main"
        or pr.get("state") != "OPEN"
    ):
        raise RepairError("publication repair PR identity changed")
    rerun_path = args.state_dir / "ci-reruns" / f"{args.ticket}-{head}.json"
    rerun_info = rerun_path.lstat()
    if (
        not stat.S_ISREG(rerun_info.st_mode)
        or rerun_path.is_symlink()
        or rerun_info.st_uid != os.geteuid()
        or rerun_info.st_nlink != 1
        or stat.S_IMODE(rerun_info.st_mode) != 0o600
    ):
        raise RepairError("same-head rerun evidence is unsafe")
    rerun = json.loads(rerun_path.read_text(encoding="utf-8"))
    checks = json.loads(command(
        "gh", "pr", "checks", str(args.pr), "--repo", repo, "--required",
        "--json", "name,bucket,link",
    ))
    rerun_module = module("ci_rerun", args.kit_dir / "scripts/ci-rerun.py")
    run_id, job_id, check_name = rerun_module.classify(checks, repo)
    if (
        rerun.get("schema") != rerun_module.SCHEMA
        or rerun.get("ticket") != args.ticket
        or rerun.get("head_sha") != head
        or rerun.get("pr_number") != args.pr
        or rerun.get("run_id") != run_id
        or rerun.get("job_id") != job_id
        or rerun.get("check_name") != check_name
    ):
        raise RepairError("same-head rerun evidence does not match")
    failures = [
        item for item in checks if item.get("bucket") in {"fail", "cancel"}
    ]
    if len(failures) != 1:
        raise RepairError("publication repair failure is ambiguous")
    failure = failures[0]
    owner = (
        "test-author"
        if re.search(r"fixture|test-author|contract test", check_name, re.I)
        else "builder"
    )
    ticket_path = workdir / f"factory/tickets/{args.ticket}.md"
    text = ticket_path.read_text(encoding="utf-8")
    if field(text, "State").casefold() not in {"awaiting approval", "approved"}:
        raise RepairError(
            "publication repair requires committed Awaiting Approval or Approved state"
        )
    if pr.get("autoMergeRequest"):
        command("gh", "pr", "merge", str(args.pr), "--repo", repo, "--disable-auto")
    if not pr.get("isDraft"):
        command("gh", "pr", "ready", str(args.pr), "--repo", repo, "--undo")
    baseline = len(verdicts(text))
    text = replace_field(text, "State", "Building")
    text = re.sub(r"^Operator-Approval:.*\n?", "", text, flags=re.I | re.M)
    for label in ("Evidence bundle posted", "Operator approved"):
        text = re.sub(
            rf"^- \[[xX]\] {re.escape(label)}\s*$",
            f"- [ ] {label}", text, flags=re.M,
        )
    text += (
        f"\nFACTORY PUBLICATION REPAIR: {owner}\n"
        f"PUBLICATION FAILURE: {failure['link']}\n"
    )
    ticket_path.write_text(text, encoding="utf-8")
    attestation = workdir / f"factory/attestations/{args.ticket}"
    changed = [ticket_path]
    for name in ("bundle.json", "approval.json"):
        path = attestation / name
        if path.exists():
            if path.is_symlink() or not path.is_file():
                raise RepairError("publication repair attestation is unsafe")
            path.unlink()
            changed.append(path)
    command("git", "-C", str(workdir), "add", "--", *(
        str(path.relative_to(workdir)) for path in changed
    ))
    command(
        "git", "-C", str(workdir), "-c", "user.name=Software Factory",
        "-c", "user.email=factory@local", "commit", "-m",
        f"{args.ticket}: reopen protected publication repair",
    )
    reset_head = command("git", "-C", str(workdir), "rev-parse", "HEAD")
    value = signed({
        "branch": branch,
        "check_name": check_name,
        "factory_sha": args.factory_sha,
        "failure_link": failure["link"],
        "original_head": head,
        "pr_number": args.pr,
        "repair_owner": owner,
        "reset_head": reset_head,
        "schema": SCHEMA,
        "ticket": args.ticket,
        "verdict_baseline": baseline,
    }, secret)
    write(repair_path, value)
    push_head(workdir, origin, branch, reset_head, head)
    return value


def stage(args: argparse.Namespace, secret: bytes) -> str:
    path = args.state_dir / "publication-repairs" / f"{args.ticket}.json"
    if not path.exists():
        return "INACTIVE"
    record = load(path, secret)
    if (
        record.get("schema") != SCHEMA
        or record.get("ticket") != args.ticket
        or record.get("repair_owner") not in {"builder", "test-author"}
        or not SHA.fullmatch(record.get("reset_head", ""))
        or not isinstance(record.get("verdict_baseline"), int)
        or record["verdict_baseline"] < 0
    ):
        raise RepairError("publication repair record is malformed")
    workdir = args.workdir.resolve(strict=True)
    head = command("git", "-C", str(workdir), "rev-parse", "HEAD")
    command(
        "git", "-C", str(workdir), "merge-base", "--is-ancestor",
        record["reset_head"], head,
    )
    ticket_text = (workdir / f"factory/tickets/{args.ticket}.md").read_text(
        encoding="utf-8"
    )
    if (
        field(ticket_text, "State").casefold() not in {"building", "review"}
        or f"FACTORY PUBLICATION REPAIR: {record['repair_owner']}" not in ticket_text
        or f"PUBLICATION FAILURE: {record['failure_link']}" not in ticket_text
    ):
        raise RepairError("publication repair ticket lineage changed")
    manifests = {}
    for path in (args.factory_root / "factory/runs").glob("*.meta"):
        values = dict(
            line.split("=", 1)
            for line in path.read_text(encoding="utf-8").splitlines()
            if "=" in line
        )
        if values.get("run_id"):
            if values["run_id"] in manifests:
                raise RepairError("publication repair run evidence is ambiguous")
            manifests[values["run_id"]] = values
    roles = []
    with args.ledger.open(newline="", encoding="utf-8") as stream:
        for row in csv.DictReader(stream):
            if row.get("ticket") != args.ticket or row.get("exit_status") != "0":
                continue
            value = manifests.get(row.get("run_id", ""), {})
            before = value.get("role_head_before", "")
            if (
                value.get("role") != row.get("role")
                or not SHA.fullmatch(before)
                or subprocess.run(
                    [
                        "git", "-C", str(workdir), "merge-base", "--is-ancestor",
                        record["reset_head"], before,
                    ],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    check=False,
                ).returncode
            ):
                continue
            roles.append(row["role"])
    return decide(record, ticket_text, roles)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("create", "stage"))
    parser.add_argument("--factory-root", required=True, type=Path)
    parser.add_argument("--workdir", required=True, type=Path)
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--kit-dir", required=True, type=Path)
    parser.add_argument("--ticket", required=True)
    parser.add_argument("--factory-sha", required=True)
    parser.add_argument("--pr", type=int, default=0)
    parser.add_argument("--ledger", type=Path)
    args = parser.parse_args()
    try:
        if (
            not TICKET.fullmatch(args.ticket)
            or not SHA.fullmatch(args.factory_sha)
            or (args.action == "create" and args.pr <= 0)
            or (args.action == "stage" and args.ledger is None)
        ):
            raise RepairError("invalid publication repair arguments")
        args.factory_root = args.factory_root.resolve(strict=True)
        args.workdir = args.workdir.resolve(strict=True)
        args.state_dir = safe_directory(args.state_dir.resolve(strict=True))
        args.kit_dir = args.kit_dir.resolve(strict=True)
        passport = module("ticket_passport", args.kit_dir / "scripts/ticket-passport.py")
        secret = passport.key(args.state_dir)
        if args.action == "create":
            value = create(args, secret)
            print(json.dumps({
                "head": value["reset_head"],
                "owner": value["repair_owner"],
                "schema": SCHEMA,
                "status": "repair",
                "ticket": args.ticket,
            }, sort_keys=True))
        else:
            print(stage(args, secret))
    except ExternalUnavailable:
        print('{"reason_code":"external_unavailable","status":"wait"}')
        raise SystemExit(75)
    except (
        FileNotFoundError, json.JSONDecodeError, OSError, RepairError,
        subprocess.SubprocessError, ValueError,
    ) as error:
        if args.action == "stage":
            print(f"REFUSE {error}")
        else:
            print(json.dumps({
                "error": str(error), "schema": SCHEMA, "status": "error",
                "ticket": args.ticket,
            }, sort_keys=True))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
