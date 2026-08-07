#!/usr/bin/env python3
"""Reconcile factory Markdown with the operator-facing Linear workflow.

Markdown remains execution truth. Linear owns only priority, Project
membership, Backlog -> Ready, Awaiting Approval -> Approved, and an operator
resume from Blocked-Escalated. Pulls happen before pushes. The only
controller-bound push is an exact Done projection after protected terminal
evidence validates.
"""

import argparse
import copy
import csv
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
from effective_ticket import (  # noqa: E402
    apply_operator_fields,
    committed_factory_file,
    committed_ticket,
    operator_version,
)

API_URL = "https://api.linear.app/graphql"
KEY_FILE = Path.home() / ".hermes" / "secrets" / "linear-api-key"
TEAM_NAME = "Software Factory"
TEAM_KEY = "SF"
MAX_COMMENT_CHARS = 60000
FALLBACK_APPROVAL_TTL_SECONDS = 900
FALLBACK_APPROVAL = re.compile(
    r"FACTORY MODEL FALLBACK APPROVAL:\s*([0-9a-f]{64})\s+"
    r"RUN:\s*([A-Za-z0-9._-]{1,200})\s+"
    r"REASON:\s*(credits_exhausted|provider_unavailable)\s+"
    r"NONCE:\s*([0-9a-f]{32})\s*\Z"
)
TARGETED_OPERATOR_FIELDS = (
    "operator",
    "model_fallback_approval",
    "linear_comment_head_sha256",
    "blocked_source_sha256",
    "blocked_remote_updated_at",
    "operator_state_source_sha256",
)
LINEAR_COOLDOWN_SCHEMA = "nysa.software-factory.linear-account-cooldown/v1"

# Ticket State: values map 1:1 onto board columns (docs/workflows/linear.md).
# The second element is the Linear workflow-state *type* used when the
# column has to be created during --setup.
STATES = {
    "backlog": ("Backlog", "backlog"),
    "ready": ("Ready", "unstarted"),
    "planning": ("Planning", "started"),
    "building": ("Building", "started"),
    "review": ("Review", "started"),
    "awaiting approval": ("Awaiting Approval", "started"),
    "approved": ("Approved", "started"),
    "blocked-escalated": ("Blocked-Escalated", "started"),
    "done": ("Done", "completed"),
    "canceled": ("Canceled", "canceled"),
}
STATE_COLORS = {
    "backlog": "#BEC2C8",
    "ready": "#5E6AD2",
    "planning": "#8B5CF6",
    "building": "#F2994A",
    "review": "#4EA7FC",
    "awaiting approval": "#F2C94C",
    "approved": "#4CB782",
    "blocked-escalated": "#EB5757",
    "done": "#27AE60",
    "canceled": "#6B7280",
}
STATE_POSITIONS = {
    "backlog": 0.0,
    "ready": 1000.0,
    "planning": 1000.0,
    "building": 1500.0,
    "review": 2000.0,
    "awaiting approval": 4000.0,
    "approved": 5000.0,
    "blocked-escalated": 6000.0,
    "done": 0.0,
    "canceled": 0.0,
}
LEGACY_STATES = {"in progress": "planning"}
PRIORITIES = {"none": 0, "urgent": 1, "high": 2, "normal": 3, "low": 4}
PRIORITY_NAMES = {value: name for name, value in PRIORITIES.items()}
LABELS = {
    "external": "#eb5757",
    "risk:low": "#4cb782",
    "risk:medium": "#f2c94c",
    "risk:high": "#eb5757",
}
TEMPLATE_NAME = "Factory ticket"
TEMPLATE_DESCRIPTION = """State: Backlog
Initiative: I-NNN
Priority: none
Risk class: low
External: no
Merge-Policy: manual

## Description

What changes, why, and source links.

## Acceptance criteria

1. A mechanically or visually checkable outcome.

## Factory checklist

- [ ] Contract frozen
- [ ] Spec lint passed
- [ ] Tests authored
- [ ] Implementation green
- [ ] Reviewer approved
- [ ] Evidence bundle posted
- [ ] Operator approved
- [ ] PR merged and staging confirmed

## Links

- Branch:
- PR:
- Evidence:
"""
OPERATOR_TRANSITIONS = {
    ("backlog", "ready"),
    ("backlog", "canceled"),
    ("awaiting approval", "approved"),
}

BANNER = (
    "**Factory-managed issue.** Linear owns priority, Project membership, "
    "Backlog → Ready/Canceled, Awaiting Approval → Approved, and approved unblock "
    "decisions. Contract, execution stage, logs, evidence, and cost are "
    "projected from the product repo."
)
PROJECT_MARKER = "Software-Factory-Initiative:"
PROJECT_IDENTITY_SCHEMA = "nysa.software-factory.linear-project-identity-conflict/v1"


class ProjectIdentityError(RuntimeError):
    def __init__(self, message, initiative, reason, projects=(), project_ids=()):
        candidates = {}
        for project in projects:
            project_id = project.get("id") if isinstance(project, dict) else None
            if not isinstance(project_id, str) or not re.fullmatch(
                r"[A-Za-z0-9-]{1,200}", project_id
            ):
                continue
            project_url = project.get("url")
            if not isinstance(project_url, str) or not re.fullmatch(
                r"https://linear\.app/[^\s\x00-\x1f\x7f]+", project_url
            ):
                project_url = None
            candidates[project_id] = {
                "project_id": project_id,
                "project_url": project_url,
            }
        for project_id in project_ids:
            if isinstance(project_id, str) and re.fullmatch(
                r"[A-Za-z0-9-]{1,200}", project_id
            ):
                candidates.setdefault(project_id, {
                    "project_id": project_id,
                    "project_url": None,
                })
        self.conflict = {
            "schema": PROJECT_IDENTITY_SCHEMA,
            "initiative": initiative,
            "reason": reason,
            "candidates": [candidates[key] for key in sorted(candidates)],
            "observed_at": utc_now(),
        }
        super().__init__(message)


def log(message):
    print(f"[linear-sync] {time.strftime('%Y-%m-%d %H:%M:%S')} {message}", flush=True)


def utc_now():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def api_key():
    key = os.environ.get("LINEAR_API_KEY", "").strip()
    if not key:
        try:
            result = subprocess.run(
                ["security", "find-generic-password", "-s", "linear-api-key", "-w"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                key = result.stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            pass
    if not key and KEY_FILE.is_file():
        key = KEY_FILE.read_text().strip()
    return key


def operator_map_path(factory_dir):
    path = Path(os.environ.get(
        "FACTORY_OPERATOR_MAP", factory_dir / "linear-map.json"
    ))
    if not path.is_absolute():
        raise RuntimeError("operator map path must be absolute")
    return path


def gql(key, query, variables=None):
    body = json.dumps({"query": query, "variables": variables or {}}).encode()
    request = urllib.request.Request(
        API_URL,
        data=body,
        headers={"Content-Type": "application/json", "Authorization": key},
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                data = json.loads(response.read().decode())
            if data.get("errors"):
                wait = rate_limit_seconds(detail=data["errors"])
                if wait is not None:
                    if attempt < 2:
                        delay = min(2 ** attempt, 30)
                        log(f"Linear quota exhausted, backing off {delay}s")
                        time.sleep(delay)
                        continue
                    raise RuntimeError(
                        f"linear_rate_limited retry_after_seconds={wait}"
                    )
                raise RuntimeError(f"GraphQL errors: {data['errors']}")
            return data["data"]
        except urllib.error.HTTPError as error:
            detail = (
                error.read().decode(errors="replace")
                if error.code == 400 else "rate limit" if error.code == 429 else ""
            )
            quota_wait = rate_limit_seconds(error.headers, detail)
            if quota_wait is not None:
                if attempt < 2:
                    raw_wait = error.headers.get("Retry-After")
                    try:
                        wait = int(raw_wait) if raw_wait is not None else 2 ** attempt
                    except (TypeError, ValueError):
                        wait = 2 ** attempt
                    wait = min(max(wait, 0), 30)
                    log(f"Linear quota exhausted, backing off {wait}s")
                    time.sleep(wait)
                    continue
                raise RuntimeError(
                    f"linear_rate_limited retry_after_seconds={quota_wait}"
                ) from error
            if error.code in {500, 502, 503, 504} and attempt < 2:
                raw_wait = error.headers.get("Retry-After")
                try:
                    wait = int(raw_wait) if raw_wait is not None else 2 ** attempt
                except (TypeError, ValueError):
                    wait = 2 ** attempt
                wait = min(max(wait, 0), 30)
                log(f"Linear HTTP {error.code}, backing off {wait}s")
                time.sleep(wait)
                continue
            raise RuntimeError(f"Linear HTTP {error.code}: {detail}") from error
    raise RuntimeError("Linear request failed after retries")


def rate_limit_seconds(headers=None, detail=None):
    try:
        encoded = (
            json.dumps(detail, sort_keys=True)
            if not isinstance(detail, str) else detail
        )
    except (TypeError, ValueError):
        encoded = str(detail)
    if "rate limit" not in encoded.lower() and "ratelimited" not in encoded.lower():
        return None
    raw = headers.get("Retry-After") if headers is not None else None
    try:
        wait = int(raw) if raw is not None else 3600
    except (TypeError, ValueError):
        wait = 3600
    return min(max(wait, 0), 3600)


def normalize_state(value):
    value = value.strip().lower()
    return LEGACY_STATES.get(value, value)


def field(text, name, default=""):
    match = re.search(rf"^{re.escape(name)}:\s*(.+)$", text, re.MULTILINE | re.IGNORECASE)
    return match.group(1).strip() if match else default


def merge_policy(text):
    matches = re.findall(r"^Merge-Policy:\s*(.*?)\s*$", text, re.MULTILINE | re.IGNORECASE)
    if len(matches) > 1:
        raise ValueError("ticket contains duplicate Merge-Policy fields")
    policy = matches[0].lower() if matches else "manual"
    if policy not in {"manual", "auto"}:
        raise ValueError("Merge-Policy must be manual or auto")
    return policy


def section(text, name):
    match = re.search(
        rf"^##\s+{name}\b[^\n]*\n(.*?)(?=^##\s|\Z)",
        text,
        re.MULTILINE | re.DOTALL | re.IGNORECASE,
    )
    return match.group(1).strip() if match else ""


def parse_ticket(path):
    return parse_ticket_text(path.stem, path, path.read_text())


def parse_ticket_text(ticket_id, path, text):
    first = re.match(r"^#\s+(.+)$", text.split("\n", 1)[0])
    state = normalize_state(field(text, "State", "backlog"))
    return {
        "id": ticket_id,
        "path": path,
        "text": text,
        "title": first.group(1).strip() if first else ticket_id,
        "state": state,
        "resume_state": normalize_state(field(text, "Resume-State", "")),
        "initiative": field(text, "Initiative"),
        "priority": field(text, "Priority", "none").lower(),
        "branch": re.sub(r"^`([^`\n]+)`$", r"\1", field(text, "Branch")),
        "risk": field(text, "Risk class", "low").lower().split()[0],
        "external": field(text, "External", "no").lower() in ("yes", "true", "1"),
        "merge_policy": merge_policy(text),
        "description": section(text, "Description"),
        "criteria": section(text, r"Acceptance criteria"),
        "log_lines": [
            line for line in section(text, "Log").splitlines() if line.strip().startswith("- ")
        ],
    }


def protected_merge_policy(factory_dir, ticket_id):
    repo = subprocess.run(
        ["git", "-C", str(factory_dir), "rev-parse", "--show-toplevel"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    result = subprocess.run(
        ["git", "-C", repo, "show", f"refs/remotes/origin/main:factory/tickets/{ticket_id}.md"],
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise RuntimeError(f"{ticket_id}: protected origin/main ticket is unavailable")
    return merge_policy(result.stdout)


def parse_initiative(path):
    text = path.read_text()
    first = re.match(r"^#\s+(.+)$", text.split("\n", 1)[0])
    return {
        "id": path.stem,
        "path": path,
        "text": text,
        "name": first.group(1).strip() if first else path.stem,
        "status": field(text, "Status", "planned").lower(),
        "target_date": field(text, "Target-Date"),
        "view": field(text, "View").lower() == "factory",
        "summary": section(text, "Summary"),
    }


def replace_field(text, name, value):
    pattern = re.compile(rf"^{re.escape(name)}:\s*.*$", re.MULTILINE | re.IGNORECASE)
    line = f"{name}: {value}"
    if pattern.search(text):
        return pattern.sub(line, text, count=1)
    lines = text.splitlines()
    insert_at = 1
    while insert_at < len(lines) and not lines[insert_at].strip():
        insert_at += 1
    lines.insert(insert_at, line)
    return "\n".join(lines) + ("\n" if text.endswith("\n") else "")


def atomic_write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def sync_lock(factory_dir, dry_run=False):
    if dry_run:
        yield
        return
    lock_path = factory_dir / ".linear-sync-cycle.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("another reconciliation cycle is active") from error
        yield


@contextmanager
def map_lock(map_path):
    with (map_path.parent / ".linear-sync.lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def operator_clear_intents(map_path):
    directory = map_path.parent / ".linear-operator-clears"
    if not directory.is_dir() or directory.is_symlink():
        return []
    result = []
    for path in sorted(directory.glob("T-*.json")):
        value = json.loads(path.read_text())
        if (
            set(value) != {"operator_version", "schema", "ticket"}
            or value.get("schema") != "linear-operator-clear/v1"
            or path.name != f"{value.get('ticket')}-{value.get('operator_version')}.json"
            or not re.fullmatch(r"T-[0-9]+", value.get("ticket", ""))
            or not re.fullmatch(r"[0-9a-f]{64}", value.get("operator_version", ""))
        ):
            raise ValueError("Linear operator clear intent is invalid")
        result.append((path, value))
    return result


def apply_operator_clears(map_path, mapping):
    for _path, intent in operator_clear_intents(map_path):
        entry = mapping.get("tickets", {}).get(intent["ticket"], {})
        if operator_version(entry.get("operator") or {}) == intent["operator_version"]:
            entry.pop("operator", None)


def retire_operator_clears(map_path):
    with map_lock(map_path):
        mapping = load_map(map_path)
        for path, intent in operator_clear_intents(map_path):
            entry = mapping.get("tickets", {}).get(intent["ticket"], {})
            if operator_version(entry.get("operator") or {}) != intent["operator_version"]:
                path.unlink()


def load_map(path):
    if path.is_file():
        mapping = json.loads(path.read_text())
    else:
        mapping = {}
    mapping.setdefault("_config", None)
    mapping.setdefault("_sync", {})
    mapping.setdefault("initiatives", {})
    mapping.setdefault("tickets", {})
    return mapping


def parsed_timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        value = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value.astimezone(dt.timezone.utc) if value.tzinfo is not None else None


def rate_limit_cooldown(mapping, now=None):
    health = mapping.get("_sync", mapping) if isinstance(mapping, dict) else None
    health = health if isinstance(health, dict) else {}
    match = re.fullmatch(
        r"linear_rate_limited retry_after_seconds=([0-9]+)",
        str(health.get("last_error") or ""),
    )
    failed_at = parsed_timestamp(health.get("failed_at"))
    if match is None or failed_at is None:
        return 0
    wait = min(int(match.group(1)), 3600)
    now = now or dt.datetime.now(dt.timezone.utc)
    remaining = (failed_at + dt.timedelta(seconds=wait) - now).total_seconds()
    return max(0, int(remaining + 0.999))


def account_cooldown_path(key):
    root = Path(os.environ.get(
        "FACTORY_LINEAR_COOLDOWN_DIR",
        Path.home() / ".factory" / "linear-cooldowns",
    )).expanduser()
    if not root.is_absolute():
        raise RuntimeError("Linear cooldown directory must be absolute")
    return root / f"{hashlib.sha256(key.encode()).hexdigest()}.json"


def load_account_cooldown(path):
    if not path.exists() and not path.is_symlink():
        return {}
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or path.is_symlink()
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_size > 4096
    ):
        raise RuntimeError("Linear account cooldown is unsafe")
    value = json.loads(path.read_text())
    if not isinstance(value, dict) or value.get("schema") != LINEAR_COOLDOWN_SCHEMA:
        raise RuntimeError("Linear account cooldown is invalid")
    return value


def record_account_cooldown(path, error):
    if re.fullmatch(
        r"linear_rate_limited retry_after_seconds=([0-9]+)", str(error)
    ) is None:
        return False
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    parent = path.parent.lstat()
    if (
        not stat.S_ISDIR(parent.st_mode)
        or path.parent.is_symlink()
        or parent.st_uid != os.geteuid()
        or stat.S_IMODE(parent.st_mode) != 0o700
    ):
        raise RuntimeError("Linear cooldown directory is unsafe")
    value = {
        "failed_at": utc_now(),
        "last_error": str(error),
        "schema": LINEAR_COOLDOWN_SCHEMA,
    }
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            descriptor = -1
            handle.write(json.dumps(value, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        Path(temporary).unlink(missing_ok=True)
    return True


def operator_freshness(entry):
    operator = entry.get("operator") if isinstance(entry, dict) else None
    operator = operator if isinstance(operator, dict) else {}
    earliest = dt.datetime.min.replace(tzinfo=dt.timezone.utc)
    return (
        parsed_timestamp(operator.get("linear_updated_at")) or earliest,
        parsed_timestamp(operator.get("observed_at")) or earliest,
    )


def preserve_newer_operator_pull(path, mapping, preserve_project_conflict=True):
    if not path.is_file():
        return
    current = load_map(path)
    for ticket_id, current_entry in current["tickets"].items():
        incoming_entry = mapping["tickets"].get(ticket_id)
        if not isinstance(incoming_entry, dict) or not isinstance(current_entry, dict):
            continue
        if (
            current_entry.get("issue_id") != incoming_entry.get("issue_id")
            or current_entry.get("operator_fields_initialized") is not True
            or incoming_entry.get("operator_fields_initialized") is not True
        ):
            continue
        current_operator = current_entry.get("operator") or {}
        incoming_operator = incoming_entry.get("operator") or {}
        same_blocker = (
            re.fullmatch(
                r"[0-9a-f]{64}", incoming_entry.get("blocked_source_sha256", "")
            )
            and current_entry.get("blocked_source_sha256")
            == incoming_entry.get("blocked_source_sha256")
        )
        accepted_same_blocker_decision = (
            same_blocker
            and incoming_operator.get("state")
            and incoming_entry.get("operator_state_source_sha256")
            and not current_operator.get("state")
        )
        if (
            not accepted_same_blocker_decision
            and operator_freshness(current_entry) > operator_freshness(incoming_entry)
        ):
            for name in TARGETED_OPERATOR_FIELDS:
                if name in current_entry:
                    incoming_entry[name] = copy.deepcopy(current_entry[name])
                else:
                    incoming_entry.pop(name, None)
        current_ticket_rejection = current_entry.get("operator_rejection")
        incoming_ticket_rejection = incoming_entry.get("operator_rejection")
        if (
            isinstance(current_ticket_rejection, dict)
            and (
                current_ticket_rejection.get("observed_at", ""),
                current_ticket_rejection.get("rejection_sha256", ""),
            ) > (
                (
                    incoming_ticket_rejection.get("observed_at", ""),
                    incoming_ticket_rejection.get("rejection_sha256", ""),
                )
                if isinstance(incoming_ticket_rejection, dict) else ("", "")
            )
        ):
            incoming_entry["operator_rejection"] = copy.deepcopy(
                current_ticket_rejection
            )
    current_rejection = current.get("_sync", {}).get("last_rejected")
    incoming_rejection = mapping.get("_sync", {}).get("last_rejected")
    if (
        isinstance(current_rejection, dict)
        and (
            current_rejection.get("observed_at", ""),
            current_rejection.get("rejection_sha256", ""),
        ) > (
            (
                incoming_rejection.get("observed_at", ""),
                incoming_rejection.get("rejection_sha256", ""),
            ) if isinstance(incoming_rejection, dict) else ("", "")
        )
    ):
        mapping["_sync"]["last_rejected"] = copy.deepcopy(current_rejection)
    current_selected = current.get("_sync", {}).get(
        "selected_ticket_success_at", {}
    )
    incoming_selected = mapping.get("_sync", {}).get(
        "selected_ticket_success_at", {}
    )
    selected = dict(incoming_selected) if isinstance(incoming_selected, dict) else {}
    if isinstance(current_selected, dict):
        for ticket_id, observed_at in current_selected.items():
            if (
                re.fullmatch(r"T-[0-9]+", ticket_id)
                and parsed_timestamp(observed_at)
                and (
                    not parsed_timestamp(selected.get(ticket_id))
                    or parsed_timestamp(observed_at)
                    > parsed_timestamp(selected[ticket_id])
                )
            ):
                selected[ticket_id] = observed_at
    if selected:
        mapping["_sync"]["selected_ticket_success_at"] = selected
    current_conflict = current.get("_sync", {}).get("project_identity_conflict")
    incoming_conflict = mapping.get("_sync", {}).get("project_identity_conflict")
    if (
        preserve_project_conflict
        and isinstance(current_conflict, dict)
        and current_conflict.get("observed_at", "")
        > (
            incoming_conflict.get("observed_at", "")
            if isinstance(incoming_conflict, dict) else ""
        )
    ):
        mapping["_sync"]["project_identity_conflict"] = copy.deepcopy(
            current_conflict
        )


def save_map(path, mapping, preserve_project_conflict=True):
    with map_lock(path):
        preserve_newer_operator_pull(path, mapping, preserve_project_conflict)
        apply_operator_clears(path, mapping)
        atomic_write(path, json.dumps(mapping, indent=2, sort_keys=True) + "\n")


def record_failure(map_path, mapping, error):
    health = mapping.get("_sync", {})
    conflict = (
        error.conflict
        if isinstance(error, ProjectIdentityError)
        else health.get("project_identity_conflict")
    )
    mapping["_sync"] = {
        "last_success_at": health.get("last_success_at"),
        "last_error": str(error),
        "failed_at": utc_now(),
        **(
            {"last_rejected": health["last_rejected"]}
            if isinstance(health.get("last_rejected"), dict) else {}
        ),
        **(
            {"selected_ticket_success_at": health["selected_ticket_success_at"]}
            if isinstance(health.get("selected_ticket_success_at"), dict) else {}
        ),
        **(
            {"project_identity_conflict": conflict}
            if isinstance(conflict, dict) else {}
        ),
    }
    save_map(map_path, mapping)


def ledger_stats(path):
    if not isinstance(path, Path):
        reader = csv.DictReader(path)
    elif not path.is_file():
        return {}
    else:
        with path.open() as handle:
            return ledger_stats(handle)
    stats = {}
    for row in reader:
        ticket_id = row.get("ticket", "").strip()
        if not ticket_id:
            continue
        item = stats.setdefault(
            ticket_id,
            {"cost": 0.0, "runs": 0, "last_role": None, "narrator_runs": 0},
        )
        try:
            item["cost"] += float(row.get("cost_usd") or 0)
        except ValueError:
            pass
        if row.get("exit_status", "").strip() == "0":
            role = row.get("role", "").strip()
            item["runs"] += 1
            item["last_role"] = role or item["last_role"]
            if role == "narrator":
                item["narrator_runs"] += 1
    return stats


def effective_ledger(factory_dir, dry=False):
    if dry and not os.path.lexists(factory_dir / "runs"):
        return []
    if not dry:
        ensure_runs_root(factory_dir)
    helper = Path(__file__).resolve().parent / "ledger-view.py"
    command = "print" if dry else "refresh"
    runtime = os.environ.get("FACTORY_LEDGER", "").strip()
    durable = os.environ.get("FACTORY_DURABLE_LEDGER", "").strip()
    arguments = [
        sys.executable, str(helper), command, "--factory-root", str(factory_dir.parent),
    ]
    if runtime:
        arguments.extend(("--runtime-ledger", runtime))
    if durable:
        arguments.extend(("--durable-ledger", durable))
    result = subprocess.run(
        arguments,
        check=True,
        stdout=subprocess.PIPE if dry else subprocess.DEVNULL,
        text=True,
    )
    if dry:
        return result.stdout.splitlines()
    return Path(runtime) if runtime else factory_dir / "runtime-ledger.csv"


def ensure_runs_root(factory_dir):
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    factory = os.open(factory_dir, flags)
    try:
        try:
            value = os.stat("runs", dir_fd=factory, follow_symlinks=False)
        except FileNotFoundError:
            os.mkdir("runs", 0o700, dir_fd=factory)
            os.fsync(factory)
            value = os.stat("runs", dir_fd=factory, follow_symlinks=False)
        if not stat.S_ISDIR(value.st_mode):
            raise RuntimeError("factory/runs must be a real directory")
        runs = os.open("runs", flags, dir_fd=factory)
        try:
            os.fsync(runs)
        finally:
            os.close(runs)
    finally:
        os.close(factory)


def build_description(ticket, stats):
    item = stats.get(ticket["id"], {})
    facts = [f"**State:** {STATES[ticket['state']][0]}"]
    if ticket["initiative"]:
        facts.append(f"**Initiative:** `{ticket['initiative']}`")
    if ticket["branch"]:
        facts.append(f"**Branch:** `{ticket['branch']}`")
    if item:
        facts.append(f"**Cost:** ${item['cost']:.2f} ({item['runs']} successful runs)")
        if item.get("last_role"):
            facts.append(f"**Last role:** {item['last_role']}")
    parts = [BANNER, "", " · ".join(facts)]
    if ticket["description"]:
        parts += ["", "## Description", "", ticket["description"]]
    if ticket["criteria"]:
        parts += ["", "## Acceptance criteria", "", ticket["criteria"]]
    if ticket["state"] == "blocked-escalated" and ticket["log_lines"]:
        parts += ["", "## Escalation", "", ticket["log_lines"][-1]]
    return "\n".join(parts)


def normalize_md(text):
    lines = []
    fence = None
    ordered = {}
    continuation = None
    for raw_line in (text or "").splitlines():
        if fence:
            lines.append(raw_line)
            if re.fullmatch(rf"\s*{re.escape(fence[0])}{{{fence[1]},}}\s*", raw_line):
                fence = None
            continue
        line = raw_line.rstrip()
        opening = re.match(r"^\s*(`{3,}|~{3,})(.*)$", line)
        if opening:
            while lines and not lines[-1]:
                lines.pop()
            fence = (opening.group(1)[0], len(opening.group(1)))
            lines.append(line)
            ordered.clear()
            continuation = None
            continue
        line = re.sub(r"^(\s*)\*(\s)", r"\1-\2", line)
        line = re.sub(r"(\[[^\]\n]*\]\()<([^<>\n]+)>(\))", r"\1\2\3", line)
        line = re.sub(r"^ (?=\d+[.)] )", "", line)
        marker = re.match(r"^(\s*)(\d+)([.)])\s+(.*)$", line)
        if marker:
            indent, number, delimiter, body = marker.groups()
            depth = len(indent)
            for item in tuple(ordered):
                if item > depth:
                    ordered.pop(item)
            if depth in ordered:
                number = str(ordered[depth])
            ordered[depth] = int(number) + 1
            line = f"{indent}{number}{delimiter} {body}"
            continuation = (depth, depth + 3)
        else:
            nested = re.match(r"^(\s*)[-+*] ", line)
            if nested:
                depth = len(nested.group(1))
                for item in tuple(ordered):
                    if item >= depth:
                        ordered.pop(item)
                continuation = (depth, depth + 2)
            elif line and continuation:
                depth = len(line) - len(line.lstrip())
                if (
                    depth in {continuation[1], continuation[1] + 1}
                    and not re.match(r"\s*(?:[-+*] |\d+[.)] |[>#|])", line)
                ):
                    lines[-1] += " " + line.lstrip()
                    continue
                if depth <= continuation[0]:
                    ordered.clear()
                    continuation = None
        lines.append(line)
    if fence:
        lines.append(fence[0] * fence[1])
    return "\n".join(lines).strip()


def setup(key, mapping, map_path, dry=False):
    teams = gql(key, "{ teams { nodes { id name key } } }")["teams"]["nodes"]
    team = next((item for item in teams if item["name"] == TEAM_NAME or item["key"] == TEAM_KEY), None)
    if team is None:
        if dry:
            log(f"DRY would create team {TEAM_NAME}")
            return
        team = gql(
            key,
            "mutation($input: TeamCreateInput!) { teamCreate(input: $input) { team { id name key } } }",
            {"input": {"name": TEAM_NAME, "key": TEAM_KEY}},
        )["teamCreate"]["team"]

    existing = gql(
        key,
        """query($id: String!) { team(id: $id) {
             states { nodes { id name type color position } }
             labels { nodes { id name } }
             templates { nodes { id name type } }
           } }""",
        {"id": team["id"]},
    )["team"]
    by_state = {item["name"].lower(): item for item in existing["states"]["nodes"]}
    state_ids = {}
    for lower, (name, state_type) in STATES.items():
        position = STATE_POSITIONS[lower]
        color = STATE_COLORS[lower]
        state = by_state.get(name.lower())
        if state is None:
            if dry:
                log(f"DRY would create workflow state {name}")
                continue
            state = gql(
                key,
                "mutation($input: WorkflowStateCreateInput!) { workflowStateCreate(input: $input) { workflowState { id name type } } }",
                {"input": {
                    "teamId": team["id"],
                    "name": name,
                    "type": state_type,
                    "color": color,
                    "position": position,
                    "description": f"Software Factory stage: {name}",
                }},
            )["workflowStateCreate"]["workflowState"]
        else:
            update = {}
            if state.get("position") != position:
                update["position"] = position
            if state.get("color", "").lower() != color.lower():
                update["color"] = color
            if not update:
                state_ids[lower] = state["id"]
                continue
            if dry:
                log(f"DRY would update workflow state {name}: {sorted(update)}")
            else:
                gql(
                    key,
                    """mutation($id: String!, $input: WorkflowStateUpdateInput!) {
                         workflowStateUpdate(id: $id, input: $input) { success }
                       }""",
                    {"id": state["id"], "input": update},
                )
        state_ids[lower] = state["id"]

    by_label = {item["name"].lower(): item for item in existing["labels"]["nodes"]}
    label_ids = {}
    for name, color in LABELS.items():
        label = by_label.get(name)
        if label is None:
            if dry:
                log(f"DRY would create label {name}")
                continue
            label = gql(
                key,
                "mutation($input: IssueLabelCreateInput!) { issueLabelCreate(input: $input) { issueLabel { id name } } }",
                {"input": {"teamId": team["id"], "name": name, "color": color}},
            )["issueLabelCreate"]["issueLabel"]
        label_ids[name] = label["id"]

    template = next(
        (
            item for item in existing["templates"]["nodes"]
            if item["name"] == TEMPLATE_NAME and item["type"] == "issue"
        ),
        None,
    )
    if template is None:
        if dry:
            log(f"DRY would create issue template {TEMPLATE_NAME}")
        else:
            template = gql(
                key,
                """mutation($input: TemplateCreateInput!) {
                     templateCreate(input: $input) { template { id name type } }
                   }""",
                {"input": {
                    "teamId": team["id"],
                    "name": TEMPLATE_NAME,
                    "type": "issue",
                    "templateData": {"description": TEMPLATE_DESCRIPTION},
                }},
            )["templateCreate"]["template"]

    if not dry:
        mapping["_config"] = {
            "team_id": team["id"],
            "team_key": team["key"],
            "states": state_ids,
            "labels": label_ids,
            "template_id": template["id"],
        }
        save_map(map_path, mapping)
    log("setup complete" if not dry else "DRY setup inspection complete")


def ensure_projects(key, factory_dir, mapping, map_path, dry):
    initiatives_dir = factory_dir / "initiatives"
    initiatives = {
        item["id"]: item
        for item in (parse_initiative(path) for path in sorted(initiatives_dir.glob("I-*.md")))
    }
    config = mapping["_config"]
    missing = [
        initiative_id for initiative_id in initiatives
        if not mapping["initiatives"].get(initiative_id, {}).get("project_id")
    ]
    candidates = {}
    page = gql(
        key,
        """query { projects(first: 250) {
             nodes { id name url content targetDate status { name }
                     teams { nodes { id } } }
             pageInfo { hasNextPage }
           } }""",
    )["projects"]
    if page.get("pageInfo", {}).get("hasNextPage"):
        raise RuntimeError("Linear Project inventory is incomplete")
    projects = {item["id"]: item for item in page.get("nodes", [])}
    identities = {}
    for project_id, project in projects.items():
        marker = re.findall(
            rf"^{re.escape(PROJECT_MARKER)}\s*(I-[0-9]+)\s*$",
            project.get("content") or "", re.MULTILINE,
        )
        if len(marker) == 1:
            identities.setdefault(project_id, set()).add(marker[0])
    if missing:
        title_initiatives = {}
        for path in sorted((factory_dir / "tickets").glob("T-*.md")):
            text, _source = committed_ticket(factory_dir, path.stem)
            if text is None:
                continue
            ticket = parse_ticket_text(path.stem, path, text)
            title_initiatives.setdefault(ticket["title"], set()).add(ticket["initiative"])
        for title, issues in factory_issue_index(key, config["team_id"])[0].items():
            initiative_ids = title_initiatives.get(title, set())
            if len(initiative_ids) != 1:
                continue
            for issue in issues:
                project_id = (issue.get("project") or {}).get("id")
                if project_id:
                    identities.setdefault(project_id, set()).update(initiative_ids)
        for initiative_id in missing:
            matches = [
                project for project_id, project in projects.items()
                if identities.get(project_id) == {initiative_id}
            ]
            if len(matches) > 1:
                raise ProjectIdentityError(
                    f"{initiative_id}: multiple durable Linear Project identities",
                    initiative_id,
                    "multiple_durable_identities",
                    matches,
                )
            if matches:
                if config["team_id"] not in {
                    team.get("id")
                    for team in matches[0].get("teams", {}).get("nodes", [])
                }:
                    raise ProjectIdentityError(
                        f"{initiative_id}: durable Linear Project belongs to another team",
                        initiative_id,
                        "durable_project_foreign_team",
                        matches,
                    )
                candidates[initiative_id] = matches[0]
    for initiative_id, initiative in initiatives.items():
        entry = mapping["initiatives"].get(initiative_id)
        if entry is None:
            entry = {"project_id": None}
            if not dry:
                mapping["initiatives"][initiative_id] = entry
        durable_matches = [
            project for project_id, project in projects.items()
            if identities.get(project_id) == {initiative_id}
        ]
        if len(durable_matches) > 1:
            raise ProjectIdentityError(
                f"{initiative_id}: multiple durable Linear Project identities",
                initiative_id,
                "multiple_durable_identities",
                durable_matches,
            )
        same_name = [
            item for item in projects.values()
            if item.get("name") == initiative["name"]
            and config["team_id"] in {
                team.get("id")
                for team in item.get("teams", {}).get("nodes", [])
            }
        ]
        if entry.get("project_id"):
            project = projects.get(entry["project_id"])
            if project is None:
                raise ProjectIdentityError(
                    f"{initiative_id}: mapped Linear Project is unavailable",
                    initiative_id,
                    "mapped_project_unavailable",
                    project_ids=[entry["project_id"]],
                )
            if config["team_id"] not in {
                team.get("id")
                for team in project.get("teams", {}).get("nodes", [])
            }:
                raise ProjectIdentityError(
                    f"{initiative_id}: mapped Linear Project belongs to another team",
                    initiative_id,
                    "mapped_project_foreign_team",
                    [project],
                )
            if (
                any(item.get("id") != project.get("id") for item in same_name)
                or durable_matches
                and durable_matches[0].get("id") != project.get("id")
            ):
                raise ProjectIdentityError(
                    f"{initiative_id}: conflicting Linear Project identity",
                    initiative_id,
                    "conflicting_project_identity",
                    [project, *same_name, *durable_matches],
                )
            if project.get("url") and entry.get("project_url") != project["url"] and not dry:
                entry["project_url"] = project["url"]
                save_map(map_path, mapping)
            remote_target = project.get("targetDate") or ""
            operator = {
                "status": (project.get("status") or {}).get("name", "").lower(),
                "target_date": remote_target,
                "observed_at": utc_now(),
            }
            if dry:
                log(f"{initiative_id}: DRY would update Project operator overlay")
            else:
                entry["operator"] = operator
            continue
        if initiative_id in candidates:
            project = candidates[initiative_id]
            if any(item.get("id") != project.get("id") for item in same_name):
                raise ProjectIdentityError(
                    f"{initiative_id}: conflicting Linear Project identity",
                    initiative_id,
                    "conflicting_project_identity",
                    [project, *same_name],
                )
            if dry:
                log(f"{initiative_id}: DRY would adopt Linear Project {project['name']}")
                continue
            entry["project_id"] = project["id"]
            if project.get("url"):
                entry["project_url"] = project["url"]
            save_map(map_path, mapping)
            log(f"{initiative_id}: adopted Project {project['name']}")
            continue
        if same_name:
            raise ProjectIdentityError(
                f"{initiative_id}: existing same-name Project lacks durable identity",
                initiative_id,
                "unmarked_same_name_project",
                same_name,
            )
        if dry:
            log(f"{initiative_id}: DRY would create Linear Project")
            continue
        project = gql(
            key,
            "mutation($input: ProjectCreateInput!) { projectCreate(input: $input) { project { id name url } } }",
            {"input": {
                "name": initiative["name"],
                "description": initiative["summary"][:255],
                "content": (
                    f"{PROJECT_MARKER} {initiative_id}\n\n{initiative['summary']}"
                ),
                "teamIds": [config["team_id"]],
                **({"targetDate": initiative["target_date"]} if initiative["target_date"] else {}),
            }},
        )["projectCreate"]["project"]
        entry["project_id"] = project["id"]
        if project.get("url"):
            entry["project_url"] = project["url"]
        save_map(map_path, mapping)
        log(f"{initiative_id}: created Project {project['name']}")

    existing_views = None
    for initiative_id, initiative in initiatives.items():
        if not initiative["view"]:
            continue
        entry = mapping["initiatives"].get(initiative_id, {})
        if not entry.get("project_id"):
            continue
        if entry.get("view_id") and not entry.get("view_slug"):
            view = gql(
                key,
                "query($id: String!) { customView(id: $id) { id slugId } }",
                {"id": entry["view_id"]},
            )["customView"]
            if view and view.get("slugId") and not dry:
                entry["view_slug"] = view["slugId"]
                save_map(map_path, mapping)
        if entry.get("view_id"):
            continue
        view_name = f"{initiative['name']} — Factory Pipeline"
        if existing_views is None:
            existing_views = gql(
                key,
                "{ customViews(first: 100) { nodes { id name slugId } } }",
            )["customViews"]["nodes"]
        view = next((item for item in existing_views if item["name"] == view_name), None)
        if view is None:
            if dry:
                log(f"{initiative_id}: DRY would create view {view_name}")
                continue
            view = gql(
                key,
                """mutation($input: CustomViewCreateInput!) {
                     customViewCreate(input: $input) {
                       customView { id name slugId }
                     }
                   }""",
                {"input": {
                    "name": view_name,
                    "description": (
                        "Factory pipeline for this initiative. Workflow states "
                        "match planner through approval and close-out."
                    ),
                    "shared": True,
                    "teamId": config["team_id"],
                    "filterData": {
                        "project": {"id": {"eq": entry["project_id"]}}
                    },
                    "color": "#5E6AD2",
                }},
            )["customViewCreate"]["customView"]
            log(f"{initiative_id}: created view {view['name']}")
        if not dry:
            entry["view_id"] = view["id"]
            if view.get("slugId"):
                entry["view_slug"] = view["slugId"]
            save_map(map_path, mapping)
    return initiatives


def fetch_issue(key, issue_id):
    issue = gql(
        key,
        """query($id: String!) { issue(id: $id) {
             id identifier title description priority updatedAt
             state { id name } project { id } labels { nodes { id name } }
             assignee { id }
             comments(last: 100) {
               nodes { id body createdAt updatedAt user { id name } }
               pageInfo { hasPreviousPage startCursor }
             }
           } }""",
        {"id": issue_id},
    )["issue"]
    comments = issue.get("comments")
    if (
        not isinstance(comments, dict)
        or not isinstance(comments.get("nodes"), list)
        or not isinstance(comments.get("pageInfo"), dict)
        or "hasPreviousPage" not in comments["pageInfo"]
    ):
        raise RuntimeError("Linear comment history is incomplete")
    nodes = list(comments["nodes"])
    page_info = comments["pageInfo"]
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(
        seconds=FALLBACK_APPROVAL_TTL_SECONDS
    )

    def covers_approval_window():
        for comment in nodes:
            try:
                created = dt.datetime.fromisoformat(
                    (comment.get("createdAt") or "").replace("Z", "+00:00")
                )
            except (AttributeError, ValueError):
                continue
            if created.tzinfo is not None and created <= cutoff:
                return True
        return page_info.get("hasPreviousPage") is False

    while not covers_approval_window():
        cursor = page_info.get("startCursor")
        if not isinstance(cursor, str) or not cursor:
            raise RuntimeError("Linear comment history is incomplete: missing cursor")
        page = gql(
            key,
            """query($id: String!, $before: String!) { issue(id: $id) {
                 comments(last: 100, before: $before) {
                   nodes { id body createdAt updatedAt user { id name } }
                   pageInfo { hasPreviousPage startCursor }
                 }
               } }""",
            {"id": issue_id, "before": cursor},
        )["issue"]["comments"]
        if not isinstance(page, dict) or not isinstance(page.get("nodes"), list):
            raise RuntimeError("Linear comment history is incomplete")
        nodes[:0] = page["nodes"]
        page_info = page.get("pageInfo") or {}
        if "hasPreviousPage" not in page_info:
            raise RuntimeError("Linear comment history is incomplete")
    issue["comments"] = {"nodes": nodes, "pageInfo": page_info}
    return issue


def fetch_issue_state(key, issue_id):
    return gql(
        key,
        "query($id: String!) { issue(id: $id) { "
        "id identifier updatedAt state { id name } } }",
        {"id": issue_id},
    )["issue"]


def factory_issue_index(key, team_id):
    by_title = {}
    by_id = {}
    after = None
    while True:
        page = gql(
            key,
            """query($id: String!, $after: String) { team(id: $id) {
                 issues(first: 100, after: $after) {
                   nodes { id identifier title description priority updatedAt
                           state { id name type } project { id }
                           labels { nodes { id name } } assignee { id }
                           comments(last: 1) {
                             nodes { id createdAt updatedAt }
                           } }
                   pageInfo { hasNextPage endCursor }
                 }
               } }""",
            {"id": team_id, "after": after},
        )["team"]["issues"]
        for issue in page["nodes"]:
            if not (issue.get("description") or "").startswith(BANNER):
                continue
            by_id[issue["id"]] = issue
            if issue.get("state", {}).get("type") != "canceled":
                by_title.setdefault(issue["title"], []).append(issue)
        if not page["pageInfo"]["hasNextPage"]:
            return by_title, by_id
        after = page["pageInfo"].get("endCursor")
        if not after:
            raise RuntimeError("Linear issue history is incomplete")


def comment_head(issue):
    nodes = (issue.get("comments") or {}).get("nodes")
    if not isinstance(nodes, list) or len(nodes) > 1:
        raise RuntimeError("Linear comment head is invalid")
    if not nodes:
        return "", None
    comment = nodes[-1]
    identity = {
        key: comment.get(key) for key in ("id", "createdAt", "updatedAt")
    }
    if not all(isinstance(value, str) and value for value in identity.values()):
        raise RuntimeError("Linear comment head is invalid")
    digest = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return digest, parsed_timestamp(identity["createdAt"])


def fetch_recent_comments(key, issue, entry, dry):
    digest, created_at = comment_head(issue)
    recent = (
        created_at is not None
        and created_at >= dt.datetime.now(dt.timezone.utc) - dt.timedelta(
            seconds=FALLBACK_APPROVAL_TTL_SECONDS
        )
    )
    if digest != entry.get("linear_comment_head_sha256") and recent:
        issue = fetch_issue(key, issue["id"])
        ingest_fallback_approval(issue, entry, dry)
        nodes = (issue.get("comments") or {}).get("nodes", [])
        head = {"comments": {"nodes": nodes[-1:]}}
        digest, _created_at = comment_head(head)
    if not dry:
        entry["linear_comment_head_sha256"] = digest
    return issue


def ingest_fallback_approval(actual, entry, dry):
    consumed = set(entry.get("consumed_model_fallback_comment_ids", []))
    observed_at = utc_now()
    try:
        observed = dt.datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    except ValueError:
        return
    candidates = []
    for comment in (actual.get("comments") or {}).get("nodes", []):
        comment_id = comment.get("id")
        if not isinstance(comment_id, str) or comment_id in consumed:
            continue
        match = FALLBACK_APPROVAL.fullmatch((comment.get("body") or "").strip())
        user = comment.get("user") or {}
        if not match or not isinstance(user.get("id"), str) or not user["id"]:
            continue
        approval_hash, failed_run_id, reason, nonce = match.groups()
        try:
            created = dt.datetime.fromisoformat(
                (comment.get("createdAt") or "").replace("Z", "+00:00")
            )
        except ValueError:
            continue
        if created.tzinfo is None:
            continue
        expires = created + dt.timedelta(seconds=FALLBACK_APPROVAL_TTL_SECONDS)
        if expires < observed:
            continue
        candidates.append((
            comment.get("updatedAt") or comment.get("createdAt") or "",
            comment_id,
            {
                "approval_hash": approval_hash,
                "comment_id": comment_id,
                "expires_at": expires.replace(microsecond=0).isoformat(),
                "failed_run_id": failed_run_id,
                "linear_created_at": comment.get("createdAt"),
                "linear_updated_at": comment.get("updatedAt"),
                "nonce": nonce,
                "observed_at": observed_at,
                "operator_id": user["id"],
                "operator_name": user.get("name") or "",
                "reason": reason,
                "schema": "model-fallback-linear-approval/v1",
            },
        ))
    if not candidates:
        return
    approval = sorted(candidates, key=lambda item: (item[0], item[1]))[-1][2]
    if dry:
        log("DRY would update model fallback approval overlay")
    else:
        entry["model_fallback_approval"] = approval


_VIEWER_ID_CACHE = {}


def fetch_viewer_id(key):
    if key in _VIEWER_ID_CACHE:
        return _VIEWER_ID_CACHE[key]
    try:
        viewer_id = gql(key, "{ viewer { id } }")["viewer"]["id"]
    except Exception as error:  # noqa: BLE001 - must never fail the sync cycle
        log(f"could not fetch viewer id, skipping escalation assignment: {error}")
        viewer_id = None
    _VIEWER_ID_CACHE[key] = viewer_id
    return viewer_id


def desired_labels(ticket, config):
    names = [f"risk:{ticket['risk']}"]
    if ticket["external"]:
        names.append("external")
    return [config["labels"][name] for name in names if name in config.get("labels", {})]


def linear_updated_after(candidate, baseline):
    candidate = parsed_timestamp(candidate)
    baseline = parsed_timestamp(baseline)
    return candidate is not None and baseline is not None and candidate > baseline


def blocked_source_digest(text):
    without_resume = re.sub(
        r"^OPERATOR RESUME: (?:planner|spec-linter|test-author|builder)\n?"
        r"|^OPERATOR RESUME RECEIPT: [0-9a-f]{64}\n?",
        "",
        text,
        flags=re.MULTILINE,
    )
    return hashlib.sha256(without_resume.rstrip().encode()).hexdigest()


def ingest_operator_fields(key, ticket, actual, mapping, entry, dry):
    operator = dict(entry.get("operator", {}))
    blocked_remote_updated_at = entry.get("blocked_remote_updated_at")
    source_digest = hashlib.sha256(ticket["text"].encode()).hexdigest()
    new_blocked_source = (
        ticket["state"] == "blocked-escalated"
        and entry.get("blocked_source_sha256") != blocked_source_digest(ticket["text"])
    )
    source_changed = (
        operator.get("state")
        and entry.get("operator_state_source_sha256") != source_digest
    )
    if operator.get("state") and (
        source_changed
        or normalize_state(operator.get("state_base", "")) != ticket["state"]
    ):
        operator.pop("state", None)
        operator.pop("state_base", None)
        operator.pop("approval", None)
        blocked_remote_updated_at = None
        if not dry:
            entry.pop("blocked_remote_updated_at", None)
            entry.pop("operator_state_source_sha256", None)
    if new_blocked_source:
        operator.pop("state", None)
        operator.pop("state_base", None)
        operator.pop("approval", None)
        blocked_remote_updated_at = None
        if not dry:
            entry["blocked_source_sha256"] = blocked_source_digest(ticket["text"])
            entry.pop("blocked_remote_updated_at", None)
            entry.pop("operator_state_source_sha256", None)
    elif ticket["state"] != "blocked-escalated" and not dry:
        entry.pop("blocked_source_sha256", None)
        entry.pop("blocked_remote_updated_at", None)
    remote_priority = PRIORITY_NAMES.get(actual.get("priority", 0), "none")
    operator["priority"] = remote_priority

    project_id = (actual.get("project") or {}).get("id")
    reverse_projects = {
        entry.get("project_id"): initiative_id
        for initiative_id, entry in mapping["initiatives"].items()
        if entry.get("project_id")
    }
    remote_initiative = reverse_projects.get(project_id)
    operator["initiative"] = remote_initiative

    remote_state = normalize_state(actual["state"]["name"])
    if (
        ticket["state"] == "blocked-escalated"
        and remote_state == "blocked-escalated"
        and operator.get("state")
    ):
        operator.pop("state", None)
        operator.pop("state_base", None)
        operator.pop("approval", None)
        if not dry:
            entry.pop("operator_state_source_sha256", None)
    effective = parse_ticket_text(
        ticket["id"], ticket["path"], apply_operator_fields(ticket["text"], operator)
    )
    local_state = effective["state"]
    remote_updated_at = actual.get("updatedAt")
    if local_state == "blocked-escalated" and remote_state == local_state:
        if (
            isinstance(remote_updated_at, str)
            and parsed_timestamp(blocked_remote_updated_at) is None
        ):
            blocked_remote_updated_at = remote_updated_at
            if not dry:
                entry["blocked_remote_updated_at"] = remote_updated_at
    allowed = (local_state, remote_state) in OPERATOR_TRANSITIONS
    if (
        local_state == "blocked-escalated"
        and remote_state == effective["resume_state"]
        and remote_state in STATES
        and remote_state not in {"awaiting approval", "approved", "done"}
        and linear_updated_after(
            remote_updated_at, blocked_remote_updated_at
        )
    ):
        allowed = True
    if allowed:
        operator["state"] = STATES[remote_state][0]
        operator["state_base"] = ticket["state"]
        if not dry:
            entry["operator_state_source_sha256"] = source_digest
        if remote_state == "approved":
            operator["approval"] = "Linear"
    preserve_remote_state = False
    if not allowed and remote_state != local_state:
        log(f"{ticket['id']}: ignoring non-operator transition {local_state} -> {remote_state}")
        if (
            local_state == "blocked-escalated"
            and parsed_timestamp(blocked_remote_updated_at) is not None
        ):
            preserve_remote_state = True
            required = effective.get("resume_state")
            reason_code = (
                "resume_state_not_fresh"
                if remote_state == required
                else "resume_state_mismatch"
            )
            identity = {
                "blocked_remote_updated_at": blocked_remote_updated_at,
                "local_state": local_state,
                "reason_code": reason_code,
                "remote_state": remote_state,
                "remote_updated_at": remote_updated_at,
                "required_state": required,
                "source_sha256": source_digest,
                "ticket": ticket["id"],
            }
            rejection_digest = hashlib.sha256(
                json.dumps(
                    identity, sort_keys=True, separators=(",", ":")
                ).encode()
            ).hexdigest()
            previous_rejection = entry.get("operator_rejection")
            rejection = (
                copy.deepcopy(previous_rejection)
                if isinstance(previous_rejection, dict)
                and previous_rejection.get("rejection_sha256") == rejection_digest
                else {
                    **identity,
                    "observed_at": utc_now(),
                    "rejection_sha256": rejection_digest,
                }
            )
            marker = f"<!-- nysa-operator-rejection:{rejection_digest} -->"
            known_comment = any(
                marker in (comment.get("body") or "")
                for comment in (actual.get("comments") or {}).get("nodes", [])
                if isinstance(comment, dict)
            )
            if (
                (
                    not isinstance(entry.get("operator_rejection"), dict)
                    or entry["operator_rejection"].get("rejection_sha256")
                    != rejection_digest
                )
                and not known_comment
            ):
                required_name = (
                    STATES[required][0] if required in STATES else "a valid Resume-State"
                )
                detail = (
                    "The Linear move predates this blocker. Move the issue away "
                    f"and back to `Resume-State: {required_name}` after committing "
                    "the exact receipt-bound operator directive."
                    if reason_code == "resume_state_not_fresh"
                    else f"Move it to its exact `Resume-State: {required_name}` column."
                )
                post_comment(
                    key, actual["id"],
                    f"{marker}\n**Factory unblock rejected.** The ticket is "
                    f"Blocked-Escalated, but Linear requested "
                    f"{STATES.get(remote_state, (remote_state,))[0]}. {detail} A Linear "
                    "move alone is insufficient: commit the exact receipt-bound "
                    "`OPERATOR RESUME: <role>` and `OPERATOR RESUME RECEIPT: "
                    "<sha256>` lines described in the Factory operator runbook.",
                    dry,
                )
            if not dry:
                entry["operator_rejection"] = rejection
                mapping["_sync"]["last_rejected"] = copy.deepcopy(rejection)

    operator["linear_updated_at"] = remote_updated_at
    operator["observed_at"] = utc_now()
    if dry:
        log(f"{ticket['id']}: DRY would update operator overlay")
    else:
        if not operator.get("state"):
            entry.pop("operator_state_source_sha256", None)
        entry["operator"] = operator
    effective = parse_ticket_text(
        ticket["id"], ticket["path"], apply_operator_fields(ticket["text"], operator)
    )
    effective["preserve_remote_state"] = preserve_remote_state
    return effective


def post_comment(key, issue_id, body, dry):
    if dry:
        log(f"DRY would post comment ({len(body)} chars)")
        return
    if len(body) > MAX_COMMENT_CHARS:
        body = body[:MAX_COMMENT_CHARS] + "\n\n*[truncated by linear-sync]*"
    result = gql(
        key,
        "mutation($input: CommentCreateInput!) { commentCreate(input: $input) { success } }",
        {"input": {"issueId": issue_id, "body": body}},
    )
    if result.get("commentCreate", {}).get("success") is not True:
        raise RuntimeError("Linear commentCreate did not succeed")


def project_bindings(mapping):
    initiatives = mapping.get("initiatives")
    if not isinstance(initiatives, dict):
        raise RuntimeError("Linear initiative mapping is invalid")
    result = {}
    for initiative_id, entry in initiatives.items():
        if not isinstance(entry, dict):
            raise RuntimeError("Linear initiative mapping is invalid")
        result[initiative_id] = entry.get("project_id")
    return result


def sync_ticket_operator(key, factory_dir, map_path, ticket_id, dry=False):
    if not re.fullmatch(r"T-[0-9]+", ticket_id):
        raise RuntimeError("exact-ticket sync requires T-NNN")
    with map_lock(map_path):
        mapping = load_map(map_path)
    entry = mapping["tickets"].get(ticket_id)
    if (
        not isinstance(entry, dict)
        or not isinstance(entry.get("issue_id"), str)
        or not entry["issue_id"]
        or entry.get("operator_fields_initialized") is not True
    ):
        raise RuntimeError(f"{ticket_id}: mapped initialized Linear issue is required")
    text, source_ref = committed_ticket(factory_dir, ticket_id)
    if text is None:
        raise RuntimeError(f"{ticket_id}: committed ticket source is unavailable")
    ticket = parse_ticket_text(
        ticket_id, factory_dir / "tickets" / f"{ticket_id}.md", text
    )
    if ticket["state"] not in STATES:
        raise RuntimeError(f"{ticket_id}: unknown state '{ticket['state']}'")

    issue_id = entry["issue_id"]
    bindings = project_bindings(mapping)
    working = copy.deepcopy(mapping)
    working_entry = working["tickets"][ticket_id]
    actual = fetch_issue(key, issue_id)
    ingest_fallback_approval(actual, working_entry, dry)
    digest, _created_at = comment_head({
        "comments": {
            "nodes": (actual.get("comments") or {}).get("nodes", [])[-1:]
        }
    })
    if not dry:
        working_entry["linear_comment_head_sha256"] = digest
    ingest_operator_fields(key, ticket, actual, working, working_entry, dry)
    if dry:
        return

    with map_lock(map_path):
        current = load_map(map_path)
        current_entry = current["tickets"].get(ticket_id)
        if (
            not isinstance(current_entry, dict)
            or current_entry.get("issue_id") != issue_id
            or current_entry.get("operator_fields_initialized") is not True
            or project_bindings(current) != bindings
        ):
            raise RuntimeError(f"{ticket_id}: Linear mapping changed during exact-ticket sync")
        current_text, current_source_ref = committed_ticket(factory_dir, ticket_id)
        if current_text != text or current_source_ref != source_ref:
            raise RuntimeError(f"{ticket_id}: committed ticket changed during exact-ticket sync")
        for name in TARGETED_OPERATOR_FIELDS:
            if name in working_entry:
                current_entry[name] = copy.deepcopy(working_entry[name])
            else:
                current_entry.pop(name, None)
        working_ticket_rejection = working_entry.get("operator_rejection")
        current_ticket_rejection = current_entry.get("operator_rejection")
        if isinstance(working_ticket_rejection, dict) and (
            working_ticket_rejection.get("observed_at", ""),
            working_ticket_rejection.get("rejection_sha256", ""),
        ) >= (
            (
                current_ticket_rejection.get("observed_at", ""),
                current_ticket_rejection.get("rejection_sha256", ""),
            ) if isinstance(current_ticket_rejection, dict) else ("", "")
        ):
            current_entry["operator_rejection"] = copy.deepcopy(
                working_ticket_rejection
            )
        rejection = working.get("_sync", {}).get("last_rejected")
        current_rejection = current.get("_sync", {}).get("last_rejected")
        if isinstance(rejection, dict) and (
            rejection.get("observed_at", ""),
            rejection.get("rejection_sha256", ""),
        ) >= (
            (
                current_rejection.get("observed_at", ""),
                current_rejection.get("rejection_sha256", ""),
            ) if isinstance(current_rejection, dict) else ("", "")
        ):
            current["_sync"]["last_rejected"] = copy.deepcopy(rejection)
        apply_operator_clears(map_path, current)
        atomic_write(map_path, json.dumps(current, indent=2, sort_keys=True) + "\n")


def sync_ticket_terminal(key, factory_dir, map_path, ticket_id):
    """Project one protected terminal ticket to its exact mapped Linear issue."""
    if not re.fullmatch(r"T-[0-9]+", ticket_id):
        raise RuntimeError("terminal sync requires T-NNN")
    with map_lock(map_path):
        mapping = load_map(map_path)
        tickets = mapping.get("tickets")
        config = mapping.get("_config")
        entry = tickets.get(ticket_id) if isinstance(tickets, dict) else None
        done_state = (
            config.get("states", {}).get("done") if isinstance(config, dict) else None
        )
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("issue_id"), str)
            or not entry["issue_id"]
            or entry.get("operator_fields_initialized") is not True
        ):
            raise RuntimeError(f"{ticket_id}: mapped initialized Linear issue is required")
        if not isinstance(done_state, str) or not done_state:
            raise RuntimeError("Linear Done state mapping is required")
        text, source_ref = committed_ticket(factory_dir, ticket_id)
        if source_ref != "refs/remotes/origin/main":
            raise RuntimeError(f"{ticket_id}: protected terminal ticket is unavailable")
        ticket = parse_ticket_text(
            ticket_id, factory_dir / "tickets" / f"{ticket_id}.md", text
        )
        if ticket["state"] != "done":
            raise RuntimeError(f"{ticket_id}: protected ticket is not Done")

        issue_id = entry["issue_id"]
        actual = fetch_issue_state(key, issue_id)
        if not isinstance(actual, dict) or actual.get("id") != issue_id:
            raise RuntimeError(f"{ticket_id}: Linear returned the wrong issue")
        updated = actual.get("state", {}).get("id") != done_state
        if updated:
            if committed_ticket(factory_dir, ticket_id) != (text, source_ref):
                raise RuntimeError(f"{ticket_id}: protected terminal ticket changed")
            result = gql(
                key,
                "mutation($id: String!, $input: IssueUpdateInput!) { "
                "issueUpdate(id: $id, input: $input) { success } }",
                {"id": issue_id, "input": {"stateId": done_state}},
            )
            if result.get("issueUpdate", {}).get("success") is not True:
                raise RuntimeError("Linear terminal issueUpdate did not succeed")
            actual = fetch_issue_state(key, issue_id)
        state = actual.get("state") if isinstance(actual, dict) else None
        if (
            not isinstance(actual, dict)
            or actual.get("id") != issue_id
            or not isinstance(actual.get("identifier"), str)
            or not actual["identifier"]
            or not isinstance(state, dict)
            or state.get("id") != done_state
            or normalize_state(state.get("name", "")) != "done"
        ):
            raise RuntimeError(f"{ticket_id}: Linear did not confirm exact Done state")
        if committed_ticket(factory_dir, ticket_id) != (text, source_ref):
            raise RuntimeError(f"{ticket_id}: protected terminal ticket changed")

        operator = entry.get("operator")
        if isinstance(operator, dict):
            for name in ("state", "state_base", "approval"):
                operator.pop(name, None)
            if not operator:
                entry.pop("operator", None)
        entry["identifier"] = actual["identifier"]
        entry["source_ref"] = source_ref
        apply_operator_clears(map_path, mapping)
        atomic_write(map_path, json.dumps(mapping, indent=2, sort_keys=True) + "\n")
    return {
        "identifier": actual["identifier"],
        "issue_id": issue_id,
        "source_ref": source_ref,
        "state": "Done",
        "state_id": done_state,
        "updated": updated,
    }


def sync_tickets(key, factory_dir, mapping, map_path, dry, only=None):
    config = mapping["_config"]
    viewer_id = fetch_viewer_id(key)
    existing_issues, issues_by_id = factory_issue_index(key, config["team_id"])
    stats = ledger_stats(effective_ledger(factory_dir, dry))
    project_ids = {
        initiative_id: entry.get("project_id")
        for initiative_id, entry in mapping["initiatives"].items()
    }
    ticket_paths = sorted(
        path for path in (factory_dir / "tickets").glob("T-*.md")
        if re.fullmatch(r"T-\d+", path.stem)
    )
    for path in ticket_paths:
        if only is not None and path.stem not in only:
            continue
        text, source_ref = committed_ticket(factory_dir, path.stem)
        if text is None:
            log(f"{path.stem}: no committed ticket source, skipping")
            continue
        ticket = parse_ticket_text(path.stem, path, text)
        if ticket["state"] not in STATES:
            log(f"{ticket['id']}: unknown state '{ticket['state']}', skipping")
            continue
        entry = mapping["tickets"].get(ticket["id"])
        if entry is None:
            entry = {
                "issue_id": None,
                "identifier": None,
                "log_cursor": 0,
                "bundle_digest": None,
                "operator_fields_initialized": False,
            }
            if not dry:
                mapping["tickets"][ticket["id"]] = entry
        project_id = project_ids.get(ticket["initiative"])
        desired_state_id = config["states"].get(ticket["state"])

        if entry.get("issue_id"):
            actual = issues_by_id.get(entry["issue_id"])
            if actual is None:
                actual = fetch_issue(key, entry["issue_id"])
                ingest_fallback_approval(actual, entry, dry)
                digest, _created_at = comment_head({
                    "comments": {
                        "nodes": (actual.get("comments") or {}).get("nodes", [])[-1:]
                    }
                })
                if not dry:
                    entry["linear_comment_head_sha256"] = digest
            else:
                actual = fetch_recent_comments(key, actual, entry, dry)
            if not entry.get("identifier") and actual.get("identifier") and not dry:
                entry["identifier"] = actual["identifier"]
                save_map(map_path, mapping)
            if entry.get("operator_fields_initialized"):
                ticket = ingest_operator_fields(
                    key, ticket, actual, mapping, entry, dry
                )
            else:
                log(f"{ticket['id']}: bootstrapping operator fields from Markdown")
            if not dry:
                entry["source_ref"] = source_ref
            project_id = project_ids.get(ticket["initiative"])
            desired_state_id = config["states"].get(ticket["state"])
        else:
            candidates = existing_issues.get(ticket["title"], [])
            if len(candidates) > 1:
                raise RuntimeError(
                    f"{ticket['id']}: multiple active Factory issues require reconciliation"
                )
            actual = (
                fetch_recent_comments(key, candidates[0], entry, dry)
                if candidates else None
            )
            if actual is not None:
                entry.update({
                    "issue_id": actual["id"],
                    "identifier": actual["identifier"],
                    "operator_fields_initialized": True,
                    "source_ref": source_ref,
                })
                ticket = ingest_operator_fields(
                    key, ticket, actual, mapping, entry, dry
                )
                project_id = project_ids.get(ticket["initiative"])
                desired_state_id = config["states"].get(ticket["state"])
                if not dry:
                    save_map(map_path, mapping)
                    log(f"{ticket['id']}: adopted existing issue {actual['identifier']}")
        if ticket["state"] == "awaiting approval" and ticket["merge_policy"] == "auto":
            if protected_merge_policy(factory_dir, ticket["id"]) == "auto":
                desired_state_id = config["states"].get("approved")
            else:
                log(f"{ticket['id']}: protected Merge-Policy is not auto; awaiting operator")

        description = build_description(ticket, stats)
        label_ids = desired_labels(ticket, config)
        if actual is None:
            if dry:
                log(f"{ticket['id']}: DRY would create issue in Project {ticket['initiative'] or 'none'}")
                continue
            issue = gql(
                key,
                "mutation($input: IssueCreateInput!) { issueCreate(input: $input) { issue { id identifier } } }",
                {"input": {
                    "teamId": config["team_id"],
                    "title": ticket["title"],
                    "description": description,
                    "priority": PRIORITIES.get(ticket["priority"], 0),
                    **({"stateId": desired_state_id} if desired_state_id else {}),
                    **({"projectId": project_id} if project_id else {}),
                    **({"labelIds": label_ids} if label_ids else {}),
                }},
            )["issueCreate"]["issue"]
            entry.update({
                "issue_id": issue["id"],
                "identifier": issue["identifier"],
                "operator_fields_initialized": True,
            })
            save_map(map_path, mapping)
            log(f"{ticket['id']}: created issue {issue['identifier']}")
        else:
            patch = {}
            if actual["title"] != ticket["title"]:
                patch["title"] = ticket["title"]
            if normalize_md(actual.get("description")) != normalize_md(description):
                patch["description"] = description
            if (
                desired_state_id
                and actual["state"]["id"] != desired_state_id
                and not ticket.get("preserve_remote_state")
            ):
                patch["stateId"] = desired_state_id
            if (
                ticket["state"] in {"blocked-escalated", "awaiting approval"}
                and viewer_id
                and (actual.get("assignee") or {}).get("id") != viewer_id
            ):
                patch["assigneeId"] = viewer_id
            if (actual.get("project") or {}).get("id") != project_id and project_id:
                patch["projectId"] = project_id
            if actual.get("priority", 0) != PRIORITIES.get(ticket["priority"], 0):
                patch["priority"] = PRIORITIES.get(ticket["priority"], 0)
            actual_labels = sorted(item["id"] for item in actual["labels"]["nodes"])
            managed_label_ids = set(config.get("labels", {}).values())
            preserved_labels = [
                item for item in actual_labels if item not in managed_label_ids
            ]
            reconciled_labels = sorted(set(preserved_labels + label_ids))
            if reconciled_labels != actual_labels:
                patch["labelIds"] = reconciled_labels
            if patch:
                if dry:
                    log(f"{ticket['id']}: DRY would patch {sorted(patch)}")
                else:
                    result = gql(
                        key,
                        "mutation($id: String!, $input: IssueUpdateInput!) { issueUpdate(id: $id, input: $input) { success } }",
                        {"id": entry["issue_id"], "input": patch},
                    )
                    if result.get("issueUpdate", {}).get("success") is not True:
                        raise RuntimeError("Linear issueUpdate did not succeed")
                    log(f"{ticket['id']}: patched {sorted(patch)}")
            if not dry and not entry.get("operator_fields_initialized"):
                entry["operator_fields_initialized"] = True
                save_map(map_path, mapping)

        new_lines = ticket["log_lines"][entry.get("log_cursor", 0):]
        if new_lines and entry.get("issue_id"):
            post_comment(key, entry["issue_id"], "**Ticket log**\n\n" + "\n".join(new_lines), dry)
            if not dry:
                entry["log_cursor"] = len(ticket["log_lines"])
                save_map(map_path, mapping)

        bundle = factory_dir / "tickets" / f"{ticket['id']}-bundle.md"
        bundle_text, _bundle_ref = committed_factory_file(
            factory_dir, ticket["id"], bundle.name
        )
        bundle_digest = (
            hashlib.sha256(bundle_text.encode()).hexdigest()
            if bundle_text is not None else None
        )
        if (
            entry.get("bundle_digest") != bundle_digest
            and entry.get("issue_id")
            and (
                ticket["state"] in ("awaiting approval", "approved", "done")
                or (
                    ticket["state"] == "review"
                    and stats.get(ticket["id"], {}).get("narrator_runs", 0) > 0
                )
            )
            and bundle_text is not None
        ):
            post_comment(key, entry["issue_id"], "**Evidence bundle**\n\n" + bundle_text, dry)
            if not dry:
                entry["bundle_digest"] = bundle_digest
                entry.pop("bundle_posted", None)
                save_map(map_path, mapping)


def initialize_ticket(key, factory_dir, map_path, ticket_id, dry=False):
    if not re.fullmatch(r"T-[0-9]+", ticket_id):
        raise RuntimeError("exact-ticket initialization requires T-NNN")
    mapping = load_map(map_path)
    if not isinstance(mapping.get("_config"), dict):
        raise RuntimeError("exact-ticket initialization requires canonical Linear setup")
    text, _source = committed_ticket(factory_dir, ticket_id)
    if text is None:
        raise RuntimeError(f"{ticket_id}: committed ticket source is unavailable")
    ticket = parse_ticket_text(
        ticket_id, factory_dir / "tickets" / f"{ticket_id}.md", text,
    )
    project = mapping["initiatives"].get(ticket["initiative"], {})
    if not isinstance(project, dict) or not project.get("project_id"):
        raise RuntimeError(
            f"{ticket_id}: mapped initiative Project is required before initialization"
        )
    sync_tickets(key, factory_dir, mapping, map_path, dry, {ticket_id})
    if not dry:
        previous = mapping.get("_sync", {})
        selected = dict(previous.get("selected_ticket_success_at", {}))
        selected[ticket_id] = utc_now()
        mapping["_sync"] = {
            **previous,
            "selected_ticket_success_at": selected,
        }
        save_map(map_path, mapping)


def reconcile(key, factory_dir, mapping, map_path, setup_only=False, dry=False):
    if not mapping.get("_config") or setup_only:
        setup(key, mapping, map_path, dry)
        if dry and not mapping.get("_config"):
            return
    ensure_projects(key, factory_dir, mapping, map_path, dry)
    if not setup_only:
        sync_tickets(key, factory_dir, mapping, map_path, dry)
    if not dry:
        previous_health = mapping.get("_sync", {})
        mapping["_sync"] = {
            "last_success_at": utc_now(), "last_error": None,
            **(
                {"last_rejected": previous_health["last_rejected"]}
                if isinstance(previous_health.get("last_rejected"), dict) else {}
            ),
        }
        save_map(map_path, mapping, preserve_project_conflict=False)
        retire_operator_clears(map_path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--factory-root", default=os.environ.get("FACTORY_ROOT", "."))
    parser.add_argument("--setup", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--ticket")
    parser.add_argument("--initialize", action="store_true")
    parser.add_argument("--terminal", action="store_true")
    args = parser.parse_args()
    if args.ticket and args.setup:
        parser.error("--ticket cannot be combined with --setup")
    if args.initialize and (not args.ticket or args.terminal):
        parser.error("--initialize requires --ticket and cannot be terminal")
    if args.terminal and (not args.ticket or args.dry_run):
        parser.error("--terminal requires --ticket and cannot be a dry run")

    factory_dir = Path(args.factory_root).expanduser().resolve() / "factory"
    if not factory_dir.is_dir():
        log(f"no factory/ under {args.factory_root} — nothing to do")
        return 0
    try:
        map_path = operator_map_path(factory_dir)
    except RuntimeError as error:
        log(str(error))
        return 1
    key = api_key()
    if not key:
        log(f"no API key (set LINEAR_API_KEY or create {KEY_FILE}) — skipping cycle")
        return 1 if args.ticket else 0
    cooldown_path = account_cooldown_path(key)
    try:
        cooldown = max(
            rate_limit_cooldown(load_map(map_path)),
            rate_limit_cooldown(load_account_cooldown(cooldown_path)),
        )
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as error:
        log(f"Linear quota cooldown unavailable: {error}")
        return 1 if args.ticket else 0
    if cooldown:
        log(f"Linear quota cooldown active; retry_after_seconds={cooldown}")
        return 1 if args.ticket else 0

    if args.ticket:
        try:
            if args.initialize:
                with sync_lock(map_path.parent, args.dry_run):
                    initialize_ticket(
                        key, factory_dir, map_path, args.ticket, args.dry_run,
                    )
            elif args.terminal:
                result = sync_ticket_terminal(key, factory_dir, map_path, args.ticket)
                print(json.dumps(result, sort_keys=True))
            else:
                sync_ticket_operator(key, factory_dir, map_path, args.ticket, args.dry_run)
        except Exception as error:
            try:
                record_account_cooldown(cooldown_path, error)
            except (OSError, RuntimeError):
                pass
            if args.initialize and not args.dry_run:
                try:
                    record_failure(map_path, load_map(map_path), error)
                except OSError:
                    pass
            log(f"exact-ticket sync error: {error}")
            return 1
        return 0

    try:
        with sync_lock(map_path.parent, args.dry_run):
            mapping = load_map(map_path)
            try:
                reconcile(key, factory_dir, mapping, map_path, args.setup, args.dry_run)
            except Exception as error:
                log(f"sync error (will retry next cycle): {error}")
                if not args.dry_run:
                    try:
                        record_account_cooldown(cooldown_path, error)
                        record_failure(map_path, mapping, error)
                    except (OSError, RuntimeError):
                        pass
    except Exception as error:
        log(f"sync error (will retry next cycle): {error}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
