#!/usr/bin/env python3
"""Report explicitly classified Software Factory defects to GitHub."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
from typing import Any


EVENT_SCHEMA = "nysa.software-factory.controller-event/v1"
STATE_SCHEMA = "nysa.software-factory.incident-reporter-state/v1"
REPORTABLE = {
    ("controller_error", "unsupported_deterministic_stage"):
        "unsupported deterministic stage",
    ("ticket_worker_failed", "controller_worker_exception"):
        "controller worker exception",
}
REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
SHA = re.compile(r"^[0-9a-f]{40}$")
DIGEST = re.compile(r"^[0-9a-f]{64}$")
TICKET = re.compile(r"^T-[0-9]+$")


class ReportError(ValueError):
    pass


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def read_regular(path: Path, maximum: int = 131_072) -> bytes:
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
            raise ReportError("incident reporter input is unsafe")
        raw = os.read(descriptor, maximum + 1)
    finally:
        os.close(descriptor)
    if len(raw) > maximum:
        raise ReportError("incident reporter input is oversized")
    return raw


def load_event(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(read_regular(path))
    except (json.JSONDecodeError, OSError) as error:
        raise ReportError("controller event is unreadable") from error
    if not isinstance(value, dict):
        raise ReportError("controller event is malformed")
    digest = value.pop("event_sha256", "")
    if (
        value.get("schema") != EVENT_SCHEMA
        or not DIGEST.fullmatch(digest)
        or digest != hashlib.sha256(canonical(value).encode()).hexdigest()
    ):
        raise ReportError("controller event is invalid")
    value["event_sha256"] = digest
    return value


def reportable(event: dict[str, Any]) -> bool:
    return (
        event.get("failure_class") == "factory_defect"
        and (event.get("event"), event.get("reason_code")) in REPORTABLE
        and SHA.fullmatch(str(event.get("factory_sha", ""))) is not None
        and (
            event.get("ticket") is None
            or TICKET.fullmatch(str(event.get("ticket", ""))) is not None
        )
    )


def fingerprint(event: dict[str, Any]) -> str:
    identity = {
        "event": event["event"],
        "reason_code": event["reason_code"],
        "schema": "nysa.software-factory.incident-fingerprint/v1",
    }
    return hashlib.sha256(canonical(identity).encode()).hexdigest()


def issue_body(event: dict[str, Any], incident: str, project: str) -> str:
    ticket = event.get("ticket") or "none"
    return f"""## Summary

The Software Factory recorded a high-confidence internal controller defect.

## Evidence

- Project: `{project}`
- Ticket: `{ticket}`
- Factory SHA: `{event['factory_sha']}`
- Controller event: `{event['event_sha256']}`
- Reason code: `{event['reason_code']}`
- Incident fingerprint: `{incident}`

## Expected behavior

The deterministic controller handles the stage through a typed supported path.

## Actual behavior

The controller reached an explicitly reportable internal failure boundary.

## Acceptance criteria

- Fix the shared controller path that produced this reason code.
- Add one regression case that fails before the repair and passes afterward.
- Prove the repaired path in qualification or a bounded low-risk production run.

<!-- sf-incident-fingerprint: {incident} -->
"""


def occurrence_body(event: dict[str, Any], project: str) -> str:
    return (
        "Additional occurrence: "
        f"project `{project}`, ticket `{event.get('ticket') or 'none'}`, "
        f"Factory `{event['factory_sha']}`, event `{event['event_sha256']}`."
    )


def gh(*arguments: str) -> str:
    try:
        result = subprocess.run(
            ("gh", *arguments), capture_output=True, check=False, text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ReportError("GitHub command is unavailable") from error
    if result.returncode != 0:
        raise ReportError("GitHub command failed")
    return result.stdout.strip()


def find_issue(repo: str, incident: str) -> int | None:
    query = f'repo:{repo} is:issue "{incident}" in:body'
    try:
        result = json.loads(
            gh("api", "--method", "GET", "search/issues", "-f", f"q={query}",
               "-f", "per_page=10")
        )
    except json.JSONDecodeError as error:
        raise ReportError("GitHub search returned malformed output") from error
    marker = f"<!-- sf-incident-fingerprint: {incident} -->"
    matches = [
        item for item in result.get("items", [])
        if isinstance(item, dict)
        and isinstance(item.get("number"), int)
        and marker in str(item.get("body", ""))
    ] if isinstance(result, dict) else []
    if len(matches) > 1:
        raise ReportError("GitHub incident fingerprint is ambiguous")
    return matches[0]["number"] if matches else None


def body_file(state_dir: Path, body: str) -> Path:
    descriptor, raw_path = tempfile.mkstemp(prefix=".incident-body-", dir=state_dir)
    path = Path(raw_path)
    os.chmod(path, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(body)
        stream.flush()
        os.fsync(stream.fileno())
    return path


def publish(repo: str, project: str, event: dict[str, Any]) -> None:
    incident = fingerprint(event)
    issue = find_issue(repo, incident)
    body = occurrence_body(event, project) if issue else issue_body(event, incident, project)
    path = body_file(Path(event["state_dir"]), body)
    try:
        if issue:
            gh("issue", "comment", str(issue), "--repo", repo, "--body-file", str(path))
        else:
            title = f"Software Factory incident: {REPORTABLE[(event['event'], event['reason_code'])]}"
            gh("issue", "create", "--repo", repo, "--title", title,
               "--body-file", str(path))
    finally:
        path.unlink(missing_ok=True)


def load_state(path: Path) -> set[str]:
    if not path.exists() and not path.is_symlink():
        return set()
    try:
        value = json.loads(read_regular(path, 2_000_000))
    except (json.JSONDecodeError, OSError) as error:
        raise ReportError("incident reporter state is unreadable") from error
    if (
        not isinstance(value, dict)
        or value.get("schema") != STATE_SCHEMA
        or not isinstance(value.get("processed"), list)
        or any(not DIGEST.fullmatch(str(item)) for item in value["processed"])
        or len(set(value["processed"])) != len(value["processed"])
    ):
        raise ReportError("incident reporter state is invalid")
    return set(value["processed"])


def save_state(path: Path, processed: set[str]) -> None:
    value = {"processed": sorted(processed), "schema": STATE_SCHEMA}
    descriptor, raw_path = tempfile.mkstemp(prefix=".incident-state-", dir=path.parent)
    temporary = Path(raw_path)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(canonical(value) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def report(state_dir: Path, repo: str, project: str) -> dict[str, Any]:
    events = state_dir / "events"
    if not events.is_dir() or events.is_symlink():
        raise ReportError("controller event directory is unavailable")
    state_path = state_dir / "incident-reporter.json"
    processed = load_state(state_path)
    published = 0
    invalid = 0
    for path in sorted(events.glob("*.json")):
        try:
            event = load_event(path)
        except ReportError:
            invalid += 1
            continue
        digest = event["event_sha256"]
        if digest in processed or not reportable(event):
            continue
        event["state_dir"] = str(state_dir)
        publish(repo, project, event)
        processed.add(digest)
        save_state(state_path, processed)
        published += 1
    return {
        "invalid_events": invalid,
        "published": published,
        "schema": "nysa.software-factory.incident-reporter/v1",
        "status": "ok" if not invalid else "warning",
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--json", action="store_true", required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", args.project):
        parser.error("invalid project")
    if not REPO.fullmatch(args.repo):
        parser.error("invalid repository")
    return args


def main() -> None:
    args = parse_args()
    lock_path = args.state_dir / "incident-reporter.lock"
    lock = os.open(
        lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600
    )
    try:
        info = os.fstat(lock)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise ReportError("incident reporter lock is unsafe")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            result = {
                "schema": "nysa.software-factory.incident-reporter/v1",
                "status": "busy",
            }
        else:
            result = report(args.state_dir, args.repo, args.project)
    except ReportError as error:
        result = {
            "error": str(error),
            "schema": "nysa.software-factory.incident-reporter/v1",
            "status": "error",
        }
    finally:
        os.close(lock)
    print(json.dumps(result, sort_keys=True))
    raise SystemExit(1 if result["status"] == "error" else 0)


if __name__ == "__main__":
    main()
