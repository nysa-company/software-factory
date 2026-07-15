#!/usr/bin/env python3
"""Reconcile factory Markdown with the operator-facing Linear workflow.

Markdown remains execution truth. Linear owns only priority, Project
membership, Backlog -> Ready, Awaiting Approval -> Approved, and an operator
resume from Blocked-Escalated. Pulls happen before pushes and the sequencer
never calls Linear directly.
"""

import argparse
import csv
import datetime as dt
import fcntl
import json
import os
import re
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
)

API_URL = "https://api.linear.app/graphql"
KEY_FILE = Path.home() / ".hermes" / "secrets" / "linear-api-key"
TEAM_NAME = "Software Factory"
TEAM_KEY = "SF"
MAX_COMMENT_CHARS = 60000

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
}
AUXILIARY_STATE_COLORS = {
    "canceled": "#6B7280",
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
    ("awaiting approval", "approved"),
}

BANNER = (
    "**Factory-managed issue.** Linear owns priority, Project membership, "
    "Backlog → Ready, Awaiting Approval → Approved, and approved unblock "
    "decisions. Contract, execution stage, logs, evidence, and cost are "
    "projected from the product repo."
)


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
                raise RuntimeError(f"GraphQL errors: {data['errors']}")
            return data["data"]
        except urllib.error.HTTPError as error:
            if error.code == 429 and attempt < 2:
                wait = int(error.headers.get("Retry-After", "10"))
                log(f"rate limited, backing off {wait}s")
                time.sleep(wait)
                continue
            detail = error.read().decode(errors="replace")
            raise RuntimeError(f"Linear HTTP {error.code}: {detail}") from error
    raise RuntimeError("Linear request failed after retries")


def normalize_state(value):
    value = value.strip().lower()
    return LEGACY_STATES.get(value, value)


def field(text, name, default=""):
    match = re.search(rf"^{re.escape(name)}:\s*(.+)$", text, re.MULTILINE | re.IGNORECASE)
    return match.group(1).strip() if match else default


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
        "branch": field(text, "Branch"),
        "risk": field(text, "Risk class", "low").lower().split()[0],
        "external": field(text, "External", "no").lower() in ("yes", "true", "1"),
        "description": section(text, "Description"),
        "criteria": section(text, r"Acceptance criteria"),
        "log_lines": [
            line for line in section(text, "Log").splitlines() if line.strip().startswith("- ")
        ],
    }


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
    lock_path = factory_dir / ".linear-sync.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("another reconciliation cycle is active") from error
        yield


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


def save_map(path, mapping):
    atomic_write(path, json.dumps(mapping, indent=2, sort_keys=True) + "\n")


def record_failure(map_path, mapping, error):
    mapping["_sync"] = {
        "last_success_at": mapping.get("_sync", {}).get("last_success_at"),
        "last_error": str(error),
        "failed_at": utc_now(),
    }
    save_map(map_path, mapping)


def ledger_stats(path):
    stats = {}
    if not path.is_file():
        return stats
    with path.open() as handle:
        for row in csv.DictReader(handle):
            ticket_id = row.get("ticket", "").strip()
            if not ticket_id:
                continue
            item = stats.setdefault(ticket_id, {"cost": 0.0, "runs": 0, "last_role": None})
            try:
                item["cost"] += float(row.get("cost_usd") or 0)
            except ValueError:
                pass
            if row.get("exit_status", "").strip() == "0":
                item["runs"] += 1
                item["last_role"] = row.get("role", "").strip() or item["last_role"]
    return stats


def effective_ledger(factory_dir):
    helper = Path(__file__).resolve().parent / "ledger-view.py"
    subprocess.run(
        [sys.executable, str(helper), "refresh", "--factory-root", str(factory_dir.parent)],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    return factory_dir / "runtime-ledger.csv"


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
    for line in (text or "").splitlines():
        line = re.sub(r"^(\s*)\*(\s)", r"\1-\2", line)
        lines.append(line.rstrip())
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

    for name, color in AUXILIARY_STATE_COLORS.items():
        state = by_state.get(name)
        if state and state.get("color", "").lower() != color.lower():
            if dry:
                log(f"DRY would recolor workflow state {state['name']}")
            else:
                gql(
                    key,
                    """mutation($id: String!, $input: WorkflowStateUpdateInput!) {
                         workflowStateUpdate(id: $id, input: $input) { success }
                       }""",
                    {"id": state["id"], "input": {"color": color}},
                )

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
    for initiative_id, initiative in initiatives.items():
        entry = mapping["initiatives"].get(initiative_id)
        if entry is None:
            entry = {"project_id": None}
            if not dry:
                mapping["initiatives"][initiative_id] = entry
        if entry.get("project_id"):
            project = gql(
                key,
                "query($id: String!) { project(id: $id) { id name url targetDate status { name } } }",
                {"id": entry["project_id"]},
            )["project"]
            if project:
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
        if dry:
            log(f"{initiative_id}: DRY would create Linear Project")
            continue
        project = gql(
            key,
            "mutation($input: ProjectCreateInput!) { projectCreate(input: $input) { project { id name url } } }",
            {"input": {
                "name": initiative["name"],
                "description": initiative["summary"][:255],
                "content": initiative["summary"],
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
    return gql(
        key,
        """query($id: String!) { issue(id: $id) {
             id identifier title description priority updatedAt
             state { id name } project { id } labels { nodes { id name } }
           } }""",
        {"id": issue_id},
    )["issue"]


def desired_labels(ticket, config):
    names = [f"risk:{ticket['risk']}"]
    if ticket["external"]:
        names.append("external")
    return [config["labels"][name] for name in names if name in config.get("labels", {})]


def ingest_operator_fields(ticket, actual, mapping, entry, dry):
    operator = dict(entry.get("operator", {}))
    if operator.get("state") and normalize_state(operator.get("state_base", "")) != ticket["state"]:
        operator.pop("state", None)
        operator.pop("state_base", None)
        operator.pop("approval", None)
    remote_priority = PRIORITY_NAMES.get(actual.get("priority", 0), "none")
    operator["priority"] = remote_priority

    project_id = (actual.get("project") or {}).get("id")
    reverse_projects = {
        entry.get("project_id"): initiative_id
        for initiative_id, entry in mapping["initiatives"].items()
    }
    remote_initiative = reverse_projects.get(project_id)
    if remote_initiative:
        operator["initiative"] = remote_initiative

    remote_state = normalize_state(actual["state"]["name"])
    effective = parse_ticket_text(
        ticket["id"], ticket["path"], apply_operator_fields(ticket["text"], operator)
    )
    local_state = effective["state"]
    allowed = (local_state, remote_state) in OPERATOR_TRANSITIONS
    if local_state == "blocked-escalated" and remote_state == effective["resume_state"] and remote_state in STATES:
        allowed = True
    if allowed:
        operator["state"] = STATES[remote_state][0]
        operator["state_base"] = ticket["state"]
        if remote_state == "approved":
            operator["approval"] = "Linear"
    elif remote_state != local_state:
        log(f"{ticket['id']}: ignoring non-operator transition {local_state} -> {remote_state}")

    operator["linear_updated_at"] = actual.get("updatedAt")
    operator["observed_at"] = utc_now()
    if dry:
        log(f"{ticket['id']}: DRY would update operator overlay")
    else:
        entry["operator"] = operator
    return parse_ticket_text(
        ticket["id"], ticket["path"], apply_operator_fields(ticket["text"], operator)
    )


def post_comment(key, issue_id, body, dry):
    if dry:
        log(f"DRY would post comment ({len(body)} chars)")
        return
    if len(body) > MAX_COMMENT_CHARS:
        body = body[:MAX_COMMENT_CHARS] + "\n\n*[truncated by linear-sync]*"
    gql(
        key,
        "mutation($input: CommentCreateInput!) { commentCreate(input: $input) { success } }",
        {"input": {"issueId": issue_id, "body": body}},
    )


def sync_tickets(key, factory_dir, mapping, map_path, dry):
    config = mapping["_config"]
    stats = ledger_stats(effective_ledger(factory_dir))
    project_ids = {
        initiative_id: entry.get("project_id")
        for initiative_id, entry in mapping["initiatives"].items()
    }
    ticket_paths = sorted(
        path for path in (factory_dir / "tickets").glob("T-*.md")
        if re.fullmatch(r"T-\d+", path.stem)
    )
    for path in ticket_paths:
        text, source_ref = committed_ticket(factory_dir, path.stem)
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
                "bundle_posted": False,
                "operator_fields_initialized": False,
            }
            if not dry:
                mapping["tickets"][ticket["id"]] = entry
        project_id = project_ids.get(ticket["initiative"])
        desired_state_id = config["states"].get(ticket["state"])

        if entry.get("issue_id"):
            actual = fetch_issue(key, entry["issue_id"])
            if not entry.get("identifier") and actual.get("identifier") and not dry:
                entry["identifier"] = actual["identifier"]
                save_map(map_path, mapping)
            if entry.get("operator_fields_initialized"):
                ticket = ingest_operator_fields(ticket, actual, mapping, entry, dry)
            else:
                log(f"{ticket['id']}: bootstrapping operator fields from Markdown")
            if not dry:
                entry["source_ref"] = source_ref
            project_id = project_ids.get(ticket["initiative"])
            desired_state_id = config["states"].get(ticket["state"])
        else:
            actual = None

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
            if desired_state_id and actual["state"]["id"] != desired_state_id:
                patch["stateId"] = desired_state_id
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
                    gql(
                        key,
                        "mutation($id: String!, $input: IssueUpdateInput!) { issueUpdate(id: $id, input: $input) { success } }",
                        {"id": entry["issue_id"], "input": patch},
                    )
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
        if (
            not entry.get("bundle_posted")
            and entry.get("issue_id")
            and ticket["state"] in ("awaiting approval", "approved", "done")
            and bundle_text is not None
        ):
            post_comment(key, entry["issue_id"], "**Evidence bundle**\n\n" + bundle_text, dry)
            if not dry:
                entry["bundle_posted"] = True
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
        mapping["_sync"] = {"last_success_at": utc_now(), "last_error": None}
        save_map(map_path, mapping)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--factory-root", default=os.environ.get("FACTORY_ROOT", "."))
    parser.add_argument("--setup", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    factory_dir = Path(args.factory_root).expanduser().resolve() / "factory"
    if not factory_dir.is_dir():
        log(f"no factory/ under {args.factory_root} — nothing to do")
        return 0
    map_path = factory_dir / "linear-map.json"
    mapping = load_map(map_path)
    key = api_key()
    if not key:
        log(f"no API key (set LINEAR_API_KEY or create {KEY_FILE}) — skipping cycle")
        return 0

    try:
        with sync_lock(factory_dir, args.dry_run):
            reconcile(key, factory_dir, mapping, map_path, args.setup, args.dry_run)
    except Exception as error:
        log(f"sync error (will retry next cycle): {error}")
        if not args.dry_run:
            try:
                record_failure(map_path, mapping, error)
            except OSError:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
