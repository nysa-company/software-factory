#!/usr/bin/env python3
"""Plan and resume bounded, independently-authorized route migrations."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any

sys.dont_write_bytecode = True

PLAN_SCHEMA = "nysa.software-factory.model-migration-batch-preview/v1"
JOURNAL_SCHEMA = "nysa.software-factory.model-migration-batch-journal/v1"
MIGRATION_SCHEMA = "ticket-model-route-migration-preview/v1"
SHA = re.compile(r"^[0-9a-f]{40}$")
DIGEST = re.compile(r"^[0-9a-f]{64}$")
TICKET = re.compile(r"^T-[0-9]+$")
APPROVER = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
PREVIEW_KEYS = {
    "journal_kit_sha", "journal_revision_count", "journal_tail_sha256",
    "preview_hash", "readiness_sha256", "schema",
    "source_document_sha256", "ticket",
}


class Refusal(Exception):
    pass


def canonical(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode()


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def output(value: dict[str, Any]) -> None:
    print(canonical(value).decode(), end="")


def safe_directory(path: Path, *, create: bool = False) -> None:
    if create and not path.exists() and not path.is_symlink():
        path.mkdir(mode=0o700, parents=True)
    info = path.lstat()
    if (
        path.is_symlink()
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) & 0o077
    ):
        raise Refusal("migration batch state directory is unsafe")


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(canonical(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        Path(temporary).unlink(missing_ok=True)


def load_json(path: Path, label: str, maximum: int = 1_000_000) -> dict[str, Any]:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_size > maximum
        ):
            raise Refusal(f"{label} is unsafe")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            value = json.load(stream)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise Refusal(f"{label} is malformed") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not isinstance(value, dict):
        raise Refusal(f"{label} is malformed")
    return value


def git(workdir: Path, *arguments: str) -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", str(workdir), *arguments],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except subprocess.CalledProcessError as error:
        raise Refusal("migration batch worktree identity is unavailable") from error


def validate_items(raw: list[list[str]]) -> list[tuple[str, Path]]:
    if not 1 <= len(raw) <= 4:
        raise Refusal("migration batch requires one to four tickets")
    items: list[tuple[str, Path]] = []
    seen: set[str] = set()
    for ticket, workdir_raw in raw:
        if not TICKET.fullmatch(ticket) or ticket in seen:
            raise Refusal("migration batch tickets must be unique T-NNN identifiers")
        workdir = Path(workdir_raw)
        try:
            physical = workdir.resolve(strict=True)
        except OSError as error:
            raise Refusal("migration batch workdir is unavailable") from error
        if not workdir.is_absolute() or physical != workdir or not workdir.is_dir():
            raise Refusal("migration batch workdir must be an exact physical directory")
        seen.add(ticket)
        items.append((ticket, workdir))
    return items


def child(
    control: Path,
    action: str,
    ticket: str,
    workdir: Path,
    *,
    preview: dict[str, Any] | None = None,
    approved_by: str = "",
    token: str = "",
) -> tuple[int, dict[str, Any]]:
    command = [str(control), action, "--ticket", ticket, "--workdir", str(workdir)]
    environment = os.environ.copy()
    environment.pop("FACTORY_GITHUB_TOKEN_FD", None)
    input_text = None
    if action == "migrate":
        assert preview is not None
        command.extend([
            "--approve-hash", preview["preview_hash"],
            "--readiness-hash", preview["readiness_sha256"],
            "--approved-by", approved_by,
        ])
        if token:
            command = ["/bin/bash", "-c", 'exec 9<&0; exec "$@"', "_", *command]
            environment["FACTORY_GITHUB_TOKEN_FD"] = "9"
            input_text = token + "\n"
    result = subprocess.run(
        command,
        env=environment,
        input=input_text,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    try:
        value = json.loads(result.stdout)
    except (UnicodeError, json.JSONDecodeError):
        value = {"error": "migration child returned malformed output", "status": "error"}
    if not isinstance(value, dict):
        value = {"error": "migration child returned malformed output", "status": "error"}
    return result.returncode, value


def validate_preview(value: dict[str, Any], ticket: str, factory_sha: str) -> None:
    if (
        set(value) != PREVIEW_KEYS
        or value.get("schema") != MIGRATION_SCHEMA
        or value.get("ticket") != ticket
        or value.get("journal_kit_sha") != factory_sha
        or not isinstance(value.get("journal_revision_count"), int)
        or isinstance(value.get("journal_revision_count"), bool)
        or value["journal_revision_count"] < 1
        or any(
            not DIGEST.fullmatch(value.get(key, ""))
            for key in (
                "journal_tail_sha256", "preview_hash", "readiness_sha256",
                "source_document_sha256",
            )
        )
    ):
        raise Refusal(f"migration preview is invalid for {ticket}")


def build_plan(
    control: Path,
    factory_sha: str,
    capacity: int,
    items: list[tuple[str, Path]],
) -> dict[str, Any]:
    identities = []
    protected_main = ""
    for ticket, workdir in items:
        branch = git(workdir, "symbolic-ref", "--quiet", "--short", "HEAD")
        head = git(workdir, "rev-parse", "HEAD")
        tracked_main = git(workdir, "rev-parse", "refs/remotes/origin/main")
        if (
            not BRANCH.fullmatch(branch)
            or ".." in branch
            or branch.endswith("/")
            or not SHA.fullmatch(head)
            or not SHA.fullmatch(tracked_main)
        ):
            raise Refusal(f"migration batch worktree identity is invalid for {ticket}")
        if protected_main and tracked_main != protected_main:
            raise Refusal("migration batch worktrees disagree on protected main")
        protected_main = tracked_main
        identities.append((ticket, workdir, branch, head))
    with ThreadPoolExecutor(max_workers=capacity) as executor:
        futures = [
            executor.submit(child, control, "migrate-plan", ticket, workdir)
            for ticket, workdir, _, _ in identities
        ]
        results = [future.result() for future in futures]
    planned = []
    for (ticket, workdir, branch, head), (status, preview) in zip(identities, results):
        if status:
            reason = preview.get("error", "migration preview failed")
            if not isinstance(reason, str) or len(reason) > 500:
                reason = "migration preview failed"
            raise Refusal(f"{ticket}: {reason}")
        validate_preview(preview, ticket, factory_sha)
        if (
            git(workdir, "symbolic-ref", "--quiet", "--short", "HEAD") != branch
            or git(workdir, "rev-parse", "HEAD") != head
        ):
            raise Refusal(f"migration batch worktree changed during preview for {ticket}")
        planned.append({
            "branch": branch,
            "head": head,
            "migration": preview,
            "ticket": ticket,
            "workdir": str(workdir),
        })
    body = {
        "factory_sha": factory_sha,
        "items": planned,
        "max_workers": capacity,
        "protected_main": protected_main,
        "schema": PLAN_SCHEMA,
    }
    return {**body, "approval_sha256": hashlib.sha256(canonical(body)).hexdigest()}


def validate_plan(
    value: dict[str, Any],
    factory_sha: str,
    capacity: int,
    items: list[tuple[str, Path]],
) -> None:
    if set(value) != {
        "approval_sha256", "factory_sha", "items", "max_workers",
        "protected_main", "schema",
    }:
        raise Refusal("migration batch preview is malformed")
    body = {key: value[key] for key in value if key != "approval_sha256"}
    expected_items = [(ticket, str(workdir)) for ticket, workdir in items]
    actual_items = value.get("items")
    if (
        value.get("schema") != PLAN_SCHEMA
        or value.get("factory_sha") != factory_sha
        or value.get("max_workers") != capacity
        or not SHA.fullmatch(value.get("protected_main", ""))
        or not isinstance(actual_items, list)
        or len(actual_items) != len(items)
        or value.get("approval_sha256")
        != hashlib.sha256(canonical(body)).hexdigest()
    ):
        raise Refusal("migration batch preview is invalid")
    for item, (ticket, workdir) in zip(actual_items, expected_items):
        if (
            not isinstance(item, dict)
            or set(item) != {"branch", "head", "migration", "ticket", "workdir"}
            or (item.get("ticket"), item.get("workdir")) != (ticket, workdir)
            or not BRANCH.fullmatch(item.get("branch", ""))
            or ".." in item["branch"]
            or item["branch"].endswith("/")
            or not SHA.fullmatch(item.get("head", ""))
            or not isinstance(item.get("migration"), dict)
        ):
            raise Refusal("migration batch preview item is invalid")
        validate_preview(item["migration"], ticket, factory_sha)


def validate_apply_basis(plan: dict[str, Any]) -> None:
    for item in plan["items"]:
        workdir = Path(item["workdir"])
        if git(workdir, "rev-parse", "refs/remotes/origin/main") != plan["protected_main"]:
            raise Refusal("protected main changed after migration batch approval")


def signed_journal(value: dict[str, Any]) -> dict[str, Any]:
    body = {key: item for key, item in value.items() if key != "record_sha256"}
    return {**body, "record_sha256": hashlib.sha256(canonical(body)).hexdigest()}


def validate_result(value: dict[str, Any], item: dict[str, Any], approved_by: str) -> None:
    migration = item["migration"]
    if (
        value.get("ticket") != item["ticket"]
        or value.get("approved_by") != approved_by
        or value.get("preview_hash") != migration["preview_hash"]
        or value.get("readiness_sha256") != migration["readiness_sha256"]
        or not SHA.fullmatch(value.get("commit_sha", ""))
        or value.get("schema") != MIGRATION_SCHEMA
    ):
        raise Refusal(f"migration result is invalid for {item['ticket']}")


def load_journal(path: Path, plan: dict[str, Any] | None = None) -> dict[str, Any]:
    value = load_json(path, "migration batch journal")
    if set(value) != {
        "approved_by", "created_at", "plan", "record_sha256", "results",
        "schema", "status", "updated_at",
    }:
        raise Refusal("migration batch journal is malformed")
    body = {key: item for key, item in value.items() if key != "record_sha256"}
    if (
        value.get("schema") != JOURNAL_SCHEMA
        or value.get("status") not in {"in_progress", "pass"}
        or value.get("record_sha256") != hashlib.sha256(canonical(body)).hexdigest()
        or not isinstance(value.get("plan"), dict)
        or (plan is not None and value["plan"] != plan)
        or not isinstance(value.get("results"), dict)
        or set(value["results"]) - {item["ticket"] for item in value["plan"].get("items", [])}
    ):
        raise Refusal("migration batch journal is invalid")
    for item in value["plan"]["items"]:
        result = value["results"].get(item["ticket"])
        if result is not None:
            if not isinstance(result, dict):
                raise Refusal("migration batch journal result is invalid")
            validate_result(result, item, value["approved_by"])
    if value["status"] == "pass" and len(value["results"]) != len(value["plan"]["items"]):
        raise Refusal("completed migration batch journal is incomplete")
    return value


def record(path: Path, journal: dict[str, Any], ticket: str, result: dict[str, Any]) -> None:
    item = next(item for item in journal["plan"]["items"] if item["ticket"] == ticket)
    validate_result(result, item, journal["approved_by"])
    existing = journal["results"].get(ticket)
    if existing is not None:
        for key in ("approved_by", "commit_sha", "preview_hash", "readiness_sha256", "ticket"):
            if existing.get(key) != result.get(key):
                raise Refusal(f"migration replay changed durable result for {ticket}")
        return
    journal["results"][ticket] = result
    journal["updated_at"] = now()
    atomic_json(path, signed_journal(journal))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("plan", "apply"))
    parser.add_argument("--control", required=True, type=Path)
    parser.add_argument("--factory-sha", required=True)
    parser.add_argument("--capacity", required=True, type=int)
    parser.add_argument("--ticket-workdir", action="append", nargs=2, default=[])
    parser.add_argument("--approve-hash", default="")
    parser.add_argument("--approved-by", default="")
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--github-token-stdin", action="store_true")
    args = parser.parse_args()
    try:
        if (
            not args.control.is_absolute()
            or args.control.resolve(strict=True) != args.control
            or not args.control.is_file()
            or not SHA.fullmatch(args.factory_sha)
            or not 1 <= args.capacity <= 4
        ):
            raise Refusal("migration batch boundary is invalid")
        items = validate_items(args.ticket_workdir)
        capacity = min(args.capacity, len(items))
        if args.action == "plan":
            if args.approve_hash or args.approved_by or args.state_dir or args.github_token_stdin:
                raise Refusal("migration batch plan received apply-only arguments")
            output(build_plan(args.control, args.factory_sha, capacity, items))
            return 0
        if (
            not DIGEST.fullmatch(args.approve_hash)
            or not APPROVER.fullmatch(args.approved_by)
            or args.approved_by == "auto"
            or args.state_dir is None
            or not args.state_dir.is_absolute()
        ):
            raise Refusal("migration batch approval boundary is invalid")
        safe_directory(args.state_dir)
        journal_dir = args.state_dir / "migration-batches"
        safe_directory(journal_dir, create=True)
        journal_path = journal_dir / f"{args.approve_hash}.json"
        token = sys.stdin.readline().rstrip("\n") if args.github_token_stdin else ""
        if args.github_token_stdin and not token:
            raise Refusal("GitHub credential descriptor is unreadable")
        if journal_path.exists() or journal_path.is_symlink():
            journal = load_journal(journal_path)
            validate_plan(journal["plan"], args.factory_sha, capacity, items)
            if (
                journal["plan"]["approval_sha256"] != args.approve_hash
                or journal["approved_by"] != args.approved_by
            ):
                raise Refusal("migration batch journal does not match approval")
            if journal["status"] == "pass":
                output(journal)
                return 0
            plan = journal["plan"]
        else:
            plan = build_plan(args.control, args.factory_sha, capacity, items)
            if plan["approval_sha256"] != args.approve_hash:
                raise Refusal("migration batch approval hash does not match preview")
            observed = now()
            journal = signed_journal({
                "approved_by": args.approved_by,
                "created_at": observed,
                "plan": plan,
                "results": {},
                "schema": JOURNAL_SCHEMA,
                "status": "in_progress",
                "updated_at": observed,
            })
            atomic_json(journal_path, journal)
        with ThreadPoolExecutor(max_workers=capacity) as executor:
            validate_apply_basis(plan)
            futures = [
                executor.submit(
                    child,
                    args.control,
                    "migrate",
                    item["ticket"],
                    Path(item["workdir"]),
                    preview=item["migration"],
                    approved_by=args.approved_by,
                    token=token,
                )
                for item in plan["items"]
            ]
            results = [future.result() for future in futures]
        failures = []
        recorded = 0
        injected_after = os.environ.get("FACTORY_TEST_BATCH_FAIL_AFTER_SUCCESS", "")
        for item, (status_code, result) in zip(plan["items"], results):
            ticket = item["ticket"]
            if status_code:
                reason = result.get("error", "migration failed")
                if not isinstance(reason, str) or len(reason) > 500:
                    reason = "migration failed"
                failures.append({"error": reason, "ticket": ticket})
                continue
            record(journal_path, journal, ticket, result)
            recorded += 1
            if (
                os.environ.get("FACTORY_TEST_MODE") == "1"
                and injected_after.isdigit()
                and recorded == int(injected_after)
            ):
                raise Refusal("injected migration batch interruption")
        if failures:
            output({
                "approval_sha256": args.approve_hash,
                "failed": failures,
                "journal": str(journal_path),
                "schema": JOURNAL_SCHEMA,
                "status": "error",
            })
            return 2
        journal["status"] = "pass"
        journal["updated_at"] = now()
        journal = signed_journal(journal)
        atomic_json(journal_path, journal)
        output(journal)
        return 0
    except (OSError, Refusal, subprocess.SubprocessError) as error:
        output({"error": str(error), "status": "error"})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
