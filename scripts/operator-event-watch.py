#!/usr/bin/env python3
"""Stream bounded operator-action projections of authenticated controller events."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import time
from typing import Any, Iterator


EVENT_SCHEMA = "nysa.software-factory.controller-event/v1"
WATCH_SCHEMA = "nysa.software-factory.operator-watch-event/v1"
CURSOR_SCHEMA = "nysa.software-factory.operator-watch-cursor/v1"
DIGEST = re.compile(r"^[0-9a-f]{64}$")
EVENT_FILE = re.compile(r"^([1-9][0-9]{0,20})-([0-9a-f]{16})[.]json$")
PROJECT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
TICKET = re.compile(r"^T-[0-9]+$")
ROLE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
RUN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
MAX_EVENT_BYTES = 1_000_000
MAX_CURSOR_BYTES = 1024
TIMEOUT_REASONS = frozenset({"hard_timeout", "invalid_progress", "soft_timeout"})


class WatchError(ValueError):
    pass


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def safe_text(value: Any, limit: int = 240) -> str:
    if not isinstance(value, str):
        return ""
    detail = value.replace("\x00", "")
    detail = re.sub(
        r"(?im)(authorization\s*:\s*)(?:bearer|basic|token)?\s*[^\r\n]*",
        lambda match: match.group(1) + "[redacted]",
        detail,
    )
    detail = re.sub(
        r"(?i)\b[A-Za-z][A-Za-z0-9+.-]*://\S+", "[redacted-url]", detail,
    )
    sensitive = (
        r"[A-Za-z0-9_.-]*(?:key|token|secret|password|url|dsn|conn|auth)"
        r"[A-Za-z0-9_.-]*"
    )
    quoted = re.compile(
        rf"(?is)(?P<prefix>['\"]?{sensitive}['\"]?\s*[:=]\s*)"
        rf"(?P<quote>['\"])(?:\\.|(?!(?P=quote)).)*(?P=quote)"
    )
    detail = quoted.sub(
        lambda match: match.group("prefix") + "[redacted]", detail,
    )
    key_line = re.compile(
        rf"(?i)^(?P<prefix>.*?['\"]?{sensitive}['\"]?\s*[:=]\s*)"
        r"(?P<value>.*)$"
    )
    redacted: list[str] = []
    continuation_indent: int | None = None
    for line in detail.splitlines(keepends=True):
        content = line.rstrip("\r\n")
        ending = line[len(content):]
        indent = len(content) - len(content.lstrip(" \t"))
        if continuation_indent is not None:
            if not content.strip() or indent > continuation_indent:
                redacted.append(content[:indent] + "[redacted]" + ending)
                continue
            continuation_indent = None
        match = key_line.match(content)
        if match:
            item = match.group("value").strip()
            redacted.append(match.group("prefix") + "[redacted]" + ending)
            if item in {"", "|", ">", "|-", ">-"}:
                continuation_indent = indent
            continue
        redacted.append(line)
    return " ".join("".join(redacted).split())[:limit]


def safe_directory(path: Path, label: str) -> Path:
    if not path.is_absolute():
        raise WatchError(f"{label} is unsafe")
    try:
        info = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise WatchError(f"{label} is unavailable") from error
    if (
        resolved != path
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise WatchError(f"{label} is unsafe")
    return path


def stream_id(state: Path, project: str) -> str:
    return hashlib.sha256(f"{state}\0{project}".encode()).hexdigest()


def event_key(name: str) -> tuple[int, str]:
    match = EVENT_FILE.fullmatch(name)
    if not match:
        raise WatchError("controller event filename is invalid")
    return int(match[1]), match[2]


def encode_cursor(state: Path, project: str, name: str, digest: str) -> str:
    raw = canonical({
        "event": name,
        "event_sha256": digest,
        "project": project,
        "schema": CURSOR_SCHEMA,
        "stream_sha256": stream_id(state, project),
    }).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(state: Path, project: str, token: str) -> tuple[str, str]:
    if not token or len(token) > MAX_CURSOR_BYTES or not re.fullmatch(
        r"[A-Za-z0-9_-]+", token
    ):
        raise WatchError("operator watch cursor is malformed")
    try:
        padding = "=" * (-len(token) % 4)
        raw = base64.b64decode(
            token + padding, altchars=b"-_", validate=True
        )
        value = json.loads(raw)
    except (ValueError, json.JSONDecodeError) as error:
        raise WatchError("operator watch cursor is malformed") from error
    if (
        len(raw) > MAX_CURSOR_BYTES
        or not isinstance(value, dict)
        or set(value) != {
            "event", "event_sha256", "project", "schema", "stream_sha256"
        }
        or value.get("schema") != CURSOR_SCHEMA
        or value.get("project") != project
        or value.get("stream_sha256") != stream_id(state, project)
        or not EVENT_FILE.fullmatch(value.get("event", ""))
        or not DIGEST.fullmatch(value.get("event_sha256", ""))
    ):
        raise WatchError("operator watch cursor is invalid for this stream")
    return value["event"], value["event_sha256"]


def file_identity(path: Path) -> tuple[int, int, int, int]:
    try:
        info = path.lstat()
    except OSError as error:
        raise WatchError("controller event stream was lost") from error
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_nlink != 1
        or stat.S_IMODE(info.st_mode) != 0o600
        or not 0 < info.st_size <= MAX_EVENT_BYTES
    ):
        raise WatchError("controller event stream is unsafe")
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def scan(events: Path) -> tuple[tuple[int, int], int, dict[str, tuple[int, int, int, int]]]:
    safe_directory(events, "controller event directory")
    directory = events.stat()
    values: dict[str, tuple[int, int, int, int]] = {}
    try:
        entries = list(os.scandir(events))
    except OSError as error:
        raise WatchError("controller event stream was lost") from error
    for entry in entries:
        if not EVENT_FILE.fullmatch(entry.name) or entry.name in values:
            raise WatchError("controller event stream contains an invalid entry")
        values[entry.name] = file_identity(events / entry.name)
    return (directory.st_dev, directory.st_ino), directory.st_mtime_ns, values


def read_event(path: Path, expected: tuple[int, int, int, int]) -> dict[str, Any]:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        before = os.fstat(descriptor)
        identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        if identity != expected:
            raise WatchError("controller event stream changed during observation")
        raw = os.read(descriptor, MAX_EVENT_BYTES + 1)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (
        len(raw) > MAX_EVENT_BYTES
        or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != expected
        or not raw.endswith(b"\n")
    ):
        raise WatchError("controller event stream changed during observation")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise WatchError("controller event evidence is malformed") from error
    if not isinstance(value, dict):
        raise WatchError("controller event evidence is malformed")
    digest = value.get("event_sha256", "")
    unsigned = dict(value)
    unsigned.pop("event_sha256", None)
    if (
        value.get("schema") != EVENT_SCHEMA
        or value.get("observed_at_epoch_ns") != event_key(path.name)[0]
        or not DIGEST.fullmatch(digest)
        or digest != hashlib.sha256(canonical(unsigned).encode()).hexdigest()
        or raw != (canonical(value) + "\n").encode()
    ):
        raise WatchError("controller event evidence is unauthenticated")
    return value


def action_event(
    source: dict[str, Any], state: Path, project: str, name: str
) -> dict[str, Any] | None:
    event = source.get("event")
    reason = ""
    question = ""
    action = ""
    if event == "awaiting_approval":
        action = "awaiting_approval"
        reason = "linear_approval_required"
        question = source.get("question", "Approve this ticket to merge in Linear.")
    elif event == "state_machine_escalated":
        action = "blocked_escalated"
        reason = source.get("detail", "state_machine_escalation")
        question = "Resolve the escalation before authorizing a resume in Linear."
    elif event == "ticket_blocked":
        if source.get("reason") == "state-machine-escalation":
            return None
        action = "blocked_escalated"
        reason = source.get("reason", "ticket_blocked")
        question = "Inspect the blocked claim and choose a supported recovery."
    elif event == "budget_wait":
        action = "budget_halt"
        reason = "budget_envelope_exhausted"
        question = "Update the approved budget envelope before resuming."
    elif event == "pre_go_failure_blocked":
        action = "terminal_role_failure"
        reason = source.get("reason", "pre_go_failure")
        question = "Inspect terminal role evidence and choose a supported recovery."
        source = dict(source)
        source["run_id"] = source.get("failed_run_id")
    elif event == "role_blocked":
        reason = source.get("terminal_reason_code") or source.get("role_exit")
        if reason in TIMEOUT_REASONS:
            action = "progress_timeout"
            question = "Inspect progress evidence and choose a supported retry or repair."
        elif source.get("role_exit") == "role_exit_contract_blocked":
            action = "contract_blocker"
            question = "Resolve the role contract blocker before authorizing a resume."
        else:
            action = "terminal_role_failure"
            question = "Inspect terminal role evidence and choose a supported recovery."
    else:
        return None
    ticket = source.get("ticket")
    role = source.get("role")
    run_id = source.get("run_id")
    passport = source.get("passport_sha256")
    if (
        not TICKET.fullmatch(ticket or "")
        or role is not None and not ROLE.fullmatch(role)
        or run_id is not None and not RUN.fullmatch(run_id)
        or passport is not None and not DIGEST.fullmatch(passport)
        or not isinstance(source.get("observed_at_epoch_ns"), int)
        or isinstance(source.get("observed_at_epoch_ns"), bool)
        or source["observed_at_epoch_ns"] <= 0
        or not re.fullmatch(r"[0-9a-f]{40}", source.get("factory_sha", ""))
    ):
        raise WatchError("operator action context is invalid")
    generation = source.get("qualification_generation")
    manifest = source.get("qualification_manifest_sha256")
    if (
        (generation is None) != (manifest is None)
        or generation is not None and (
            isinstance(generation, bool) or not isinstance(generation, int)
            or generation < 1 or not DIGEST.fullmatch(manifest or "")
        )
    ):
        raise WatchError("operator action qualification context is invalid")
    digest = source["event_sha256"]
    return {
        "action": action,
        "cursor": encode_cursor(state, project, name, digest),
        "factory_sha": source["factory_sha"],
        "observed_at_epoch_ns": source["observed_at_epoch_ns"],
        "passport_sha256": passport,
        "project": project,
        "qualification_generation": generation,
        "qualification_manifest_sha256": manifest,
        "question": safe_text(question),
        "reason": safe_text(reason),
        "role": role,
        "run_id": run_id,
        "schema": WATCH_SCHEMA,
        "source_event_sha256": digest,
        "ticket": ticket,
    }


def watch(
    state: Path, project: str, cursor: str = "", limit: int = 0,
    idle_timeout_seconds: float = 0,
) -> Iterator[dict[str, Any]]:
    safe_directory(state, "controller state directory")
    if not PROJECT.fullmatch(project):
        raise WatchError("project identifier is invalid")
    events = state / "events"
    anchor_name = ""
    anchor_digest = ""
    if cursor:
        anchor_name, anchor_digest = decode_cursor(state, project, cursor)
    deadline = (
        time.monotonic() + idle_timeout_seconds if idle_timeout_seconds else 0
    )
    known: dict[str, tuple[int, int, int, int]] = {}
    directory_identity: tuple[int, int] | None = None
    directory_mtime = -1
    pending: list[str] = []
    high_watermark: tuple[int, str] | None = None
    checkpoint_name = ""
    emitted = 0
    while True:
        if not events.exists() and not events.is_symlink():
            if anchor_name or directory_identity is not None:
                raise WatchError("controller event stream was lost")
        else:
            safe_directory(events, "controller event directory")
            observed = events.stat()
            observed_identity = (observed.st_dev, observed.st_ino)
            if (
                directory_identity is not None
                and observed_identity == directory_identity
                and observed.st_mtime_ns == directory_mtime
            ):
                current = None
                current_identity = observed_identity
                current_mtime = directory_mtime
            else:
                current_identity, current_mtime, current = scan(events)
            if directory_identity is None:
                assert current is not None
                directory_identity = current_identity
                known = current
                names = sorted(current, key=event_key)
                high_watermark = event_key(names[-1]) if names else None
                if anchor_name:
                    if anchor_name not in current:
                        raise WatchError("operator watch cursor event was lost")
                    anchor = read_event(events / anchor_name, current[anchor_name])
                    if anchor.get("event_sha256") != anchor_digest:
                        raise WatchError("operator watch cursor event was tampered")
                    checkpoint_name = anchor_name
                    anchor_key = event_key(anchor_name)
                    pending = [
                        name for name in names if event_key(name) > anchor_key
                    ]
                else:
                    pending = names
            elif current_identity != directory_identity:
                raise WatchError("controller event stream was replaced")
            elif current is not None:
                for name, identity in known.items():
                    if current.get(name) != identity:
                        raise WatchError("controller event stream lost immutable evidence")
                added = sorted(set(current) - set(known), key=event_key)
                if (
                    added and high_watermark is not None
                    and event_key(added[0]) <= high_watermark
                ):
                    raise WatchError("controller event stream order regressed")
                pending.extend(added)
                known = current
                if added:
                    high_watermark = event_key(added[-1])
            directory_mtime = current_mtime
        while pending:
            name = pending.pop(0)
            source = read_event(events / name, known[name])
            checkpoint_name = name
            projected = action_event(source, state, project, name)
            if projected is None:
                continue
            yield projected
            emitted += 1
            if limit and emitted >= limit:
                return
        if checkpoint_name and file_identity(events / checkpoint_name) != known.get(
            checkpoint_name
        ):
            raise WatchError("controller event stream lost its observation checkpoint")
        if deadline and time.monotonic() >= deadline:
            return
        time.sleep(0.1)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--project", required=True)
    parser.add_argument("--cursor", default="")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--idle-timeout-seconds", type=float, default=0)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if (
        not args.json
        or not 0 <= args.limit <= 1000
        or not 0 <= args.idle_timeout_seconds <= 3600
    ):
        parser.error("invalid operator watch arguments")
    try:
        for event in watch(
            args.state_dir, args.project, args.cursor, args.limit,
            args.idle_timeout_seconds,
        ):
            print(canonical(event), flush=True)
    except (OSError, WatchError) as error:
        print(f"operator-event-watch: {safe_text(str(error), 300)}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
