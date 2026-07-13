#!/usr/bin/env python3
"""linear-sync.py — one-way projection of factory tickets into Linear.

The ticket files (factory/tickets/T-NNN.md) and the cost ledger
(factory/ledger.csv) are the only source of truth. This script is a pure
reconciler: it computes the desired Linear state from local files, diffs it
against the actual Linear state, and patches the difference. It is safe to
run repeatedly (idempotent) and Linear is strictly downstream — any API
failure logs and exits 0 so the pipeline is never blocked or signalled.

Manual edits made in Linear are reconciled away by design; every issue
carries a banner comment saying so.

Usage:
  linear-sync.py --factory-root <dir-containing-factory/> [--dry-run]
  linear-sync.py --factory-root <dir> --setup   # create/verify team + states

Auth (in order): LINEAR_API_KEY env var, macOS keychain item 'linear-api-key',
or ~/.hermes/secrets/linear-api-key.
State: <factory>/linear-map.json maps T-NNN -> Linear issue id + log cursor.
"""

import argparse
import csv
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

API_URL = "https://api.linear.app/graphql"
KEY_FILE = Path.home() / ".hermes" / "secrets" / "linear-api-key"
TEAM_NAME = "Software Factory"
TEAM_KEY = "SF"

# Ticket State: values map 1:1 onto board columns (workflows/linear.md).
# The second element is the Linear workflow-state *type* used when the
# column has to be created during --setup.
STATES = {
    "backlog": ("Backlog", "backlog"),
    "ready": ("Ready", "unstarted"),
    "in progress": ("In progress", "started"),
    "review": ("Review", "started"),
    "blocked-escalated": ("Blocked-Escalated", "started"),
    "done": ("Done", "completed"),
}

BANNER = (
    "**Read-only mirror** of `factory/tickets/` in the software-factory "
    "repo. State, description, and comments are projected from the ticket "
    "file and the cost ledger; manual edits here are overwritten on the "
    "next sync cycle."
)

MAX_COMMENT_CHARS = 60000


def log(msg):
    print(f"[linear-sync] {time.strftime('%Y-%m-%d %H:%M:%S')} {msg}", flush=True)


def api_key():
    key = os.environ.get("LINEAR_API_KEY", "").strip()
    if not key:
        # macOS keychain: security add-generic-password -a "$USER" -s linear-api-key -w
        try:
            out = subprocess.run(
                ["security", "find-generic-password", "-s", "linear-api-key", "-w"],
                capture_output=True, text=True, timeout=10,
            )
            if out.returncode == 0:
                key = out.stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            pass
    if not key and KEY_FILE.is_file():
        key = KEY_FILE.read_text().strip()
    return key


def gql(key, query, variables=None):
    body = json.dumps({"query": query, "variables": variables or {}}).encode()
    req = urllib.request.Request(
        API_URL,
        data=body,
        headers={"Content-Type": "application/json", "Authorization": key},
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode())
            if data.get("errors"):
                raise RuntimeError(f"GraphQL errors: {data['errors']}")
            return data["data"]
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 2:
                wait = int(e.headers.get("Retry-After", "10"))
                log(f"rate limited, backing off {wait}s")
                time.sleep(wait)
                continue
            raise
    raise RuntimeError("unreachable")


# --- local state ------------------------------------------------------------

def parse_ticket(path):
    text = path.read_text()
    m = re.match(r"^#\s+(.+)$", text.split("\n", 1)[0])
    title = m.group(1).strip() if m else path.stem
    sm = re.search(r"^State:\s*(.+)$", text, re.MULTILINE)
    state = sm.group(1).strip().lower() if sm else "backlog"
    bm = re.search(r"^Branch:\s*(.+)$", text, re.MULTILINE)
    branch = bm.group(1).strip() if bm else None

    def section(name):
        m = re.search(
            rf"^##\s+{name}\b[^\n]*\n(.*?)(?=^##\s|\Z)", text, re.MULTILINE | re.DOTALL
        )
        return m.group(1).strip() if m else ""

    log_lines = [
        ln for ln in section("Log").splitlines() if ln.strip().startswith("- ")
    ]
    return {
        "id": path.stem,
        "title": title,
        "state": state,
        "branch": branch,
        "description": section("Description"),
        "criteria": section(r"Acceptance criteria"),
        "log_lines": log_lines,
    }


def ledger_stats(ledger_path):
    stats = {}
    if not ledger_path.is_file():
        return stats
    with ledger_path.open() as f:
        for row in csv.DictReader(f):
            t = row.get("ticket", "").strip()
            if not t:
                continue
            s = stats.setdefault(t, {"cost": 0.0, "runs": 0, "last_role": None})
            try:
                s["cost"] += float(row.get("cost_usd") or 0)
            except ValueError:
                pass
            if row.get("exit_status", "").strip() == "0":
                s["runs"] += 1
                s["last_role"] = row.get("role", "").strip() or s["last_role"]
    return stats


def build_description(ticket, stats):
    s = stats.get(ticket["id"], {})
    facts = [f"**State:** {ticket['state'].title()}"]
    if ticket["branch"]:
        facts.append(f"**Branch:** `{ticket['branch']}`")
    if s:
        facts.append(f"**Cost to date:** ${s['cost']:.2f} ({s['runs']} successful runs)")
        if s.get("last_role"):
            facts.append(f"**Last completed role:** {s['last_role']}")
    parts = [BANNER, "", " · ".join(facts)]
    if ticket["description"]:
        parts += ["", "## Description", "", ticket["description"]]
    if ticket["criteria"]:
        parts += ["", "## Acceptance criteria", "", ticket["criteria"]]
    if ticket["state"] == "blocked-escalated" and ticket["log_lines"]:
        parts += ["", "## Escalation", "", ticket["log_lines"][-1]]
    return "\n".join(parts)


# --- setup ------------------------------------------------------------------

def setup(key, mapping, map_path):
    teams = gql(key, "{ teams { nodes { id name key } } }")["teams"]["nodes"]
    team = next(
        (t for t in teams if t["name"] == TEAM_NAME or t["key"] == TEAM_KEY), None
    )
    if team is None:
        team = gql(
            key,
            """mutation($input: TeamCreateInput!) {
                 teamCreate(input: $input) { team { id name key } } }""",
            {"input": {"name": TEAM_NAME, "key": TEAM_KEY}},
        )["teamCreate"]["team"]
        log(f"created team {team['name']} ({team['key']})")
    else:
        log(f"found team {team['name']} ({team['key']})")

    existing = gql(
        key,
        "query($id: String!) { team(id: $id) { states { nodes { id name type } } } }",
        {"id": team["id"]},
    )["team"]["states"]["nodes"]
    by_name = {s["name"].lower(): s for s in existing}
    state_ids = {}
    for lower, (name, stype) in STATES.items():
        st = by_name.get(name.lower())
        if st is None:
            st = gql(
                key,
                """mutation($input: WorkflowStateCreateInput!) {
                     workflowStateCreate(input: $input) {
                       workflowState { id name type } } }""",
                {"input": {"teamId": team["id"], "name": name, "type": stype,
                           "color": "#95a2b3"}},
            )["workflowStateCreate"]["workflowState"]
            log(f"created workflow state {name}")
        state_ids[lower] = st["id"]

    mapping["_config"] = {"team_id": team["id"], "team_key": team["key"],
                          "states": state_ids}
    save_map(map_path, mapping)
    log("setup complete; config written to linear-map.json")


# --- sync -------------------------------------------------------------------

def load_map(path):
    if path.is_file():
        return json.loads(path.read_text())
    return {"_config": None, "tickets": {}}


def save_map(path, mapping):
    path.write_text(json.dumps(mapping, indent=2, sort_keys=True) + "\n")


def normalize_md(text):
    """Linear re-serializes markdown on save (e.g. '- ' bullets become '* ').

    Normalize both sides before diffing so cosmetic rewrites don't cause an
    endless patch loop. Only differences that survive normalization are
    treated as real drift.
    """
    lines = []
    for ln in (text or "").splitlines():
        ln = re.sub(r"^(\s*)\*(\s)", r"\1-\2", ln)
        lines.append(ln.rstrip())
    return "\n".join(lines).strip()


def fetch_issue(key, issue_id):
    return gql(
        key,
        """query($id: String!) { issue(id: $id) {
             id title description state { id } } }""",
        {"id": issue_id},
    )["issue"]


def comment(key, issue_id, body, dry):
    if dry:
        log(f"  DRY would post comment ({len(body)} chars)")
        return
    if len(body) > MAX_COMMENT_CHARS:
        body = body[:MAX_COMMENT_CHARS] + "\n\n*[truncated by linear-sync]*"
    gql(
        key,
        """mutation($input: CommentCreateInput!) {
             commentCreate(input: $input) { success } }""",
        {"input": {"issueId": issue_id, "body": body}},
    )


def sync(key, factory_dir, mapping, map_path, dry):
    cfg = mapping.get("_config")
    if not cfg:
        log("no _config in linear-map.json — running first-time setup")
        setup(key, mapping, map_path)
        cfg = mapping["_config"]
    tickets_dir = factory_dir / "tickets"
    stats = ledger_stats(factory_dir / "ledger.csv")
    ticket_files = sorted(
        p for p in tickets_dir.glob("T-*.md") if re.fullmatch(r"T-\d+", p.stem)
    )

    for path in ticket_files:
        t = parse_ticket(path)
        entry = mapping["tickets"].setdefault(
            t["id"], {"issue_id": None, "log_cursor": 0, "bundle_posted": False}
        )
        desired_state_id = cfg["states"].get(t["state"])
        if desired_state_id is None:
            log(f"{t['id']}: unknown state '{t['state']}', skipping state move")
        desired_desc = build_description(t, stats)

        if entry["issue_id"] is None:
            if dry:
                log(f"{t['id']}: DRY would create issue")
                continue
            issue = gql(
                key,
                """mutation($input: IssueCreateInput!) {
                     issueCreate(input: $input) { issue { id identifier } } }""",
                {"input": {
                    "teamId": cfg["team_id"],
                    "title": t["title"],
                    "description": desired_desc,
                    **({"stateId": desired_state_id} if desired_state_id else {}),
                }},
            )["issueCreate"]["issue"]
            entry["issue_id"] = issue["id"]
            save_map(map_path, mapping)
            log(f"{t['id']}: created issue {issue['identifier']}")
        else:
            actual = fetch_issue(key, entry["issue_id"])
            patch = {}
            if actual["title"] != t["title"]:
                patch["title"] = t["title"]
            if normalize_md(actual["description"]) != normalize_md(desired_desc):
                patch["description"] = desired_desc
                if os.environ.get("LINEAR_SYNC_DEBUG") or (factory_dir / ".linear-sync-debug").exists():
                    import difflib
                    diff = "\n".join(difflib.unified_diff(
                        (actual["description"] or "").splitlines(),
                        desired_desc.splitlines(), "actual", "desired", lineterm=""))
                    log(f"{t['id']}: description diff:\n{diff}")
            if desired_state_id and actual["state"]["id"] != desired_state_id:
                patch["stateId"] = desired_state_id
            if patch:
                if dry:
                    log(f"{t['id']}: DRY would patch {sorted(patch)}")
                else:
                    gql(
                        key,
                        """mutation($id: String!, $input: IssueUpdateInput!) {
                             issueUpdate(id: $id, input: $input) { success } }""",
                        {"id": entry["issue_id"], "input": patch},
                    )
                    log(f"{t['id']}: patched {sorted(patch)}")

        # Append-only log lines become comments exactly once.
        new_lines = t["log_lines"][entry["log_cursor"]:]
        if new_lines and entry["issue_id"]:
            comment(key, entry["issue_id"], "**Ticket log**\n\n" + "\n".join(new_lines), dry)
            if not dry:
                entry["log_cursor"] = len(t["log_lines"])
                save_map(map_path, mapping)
                log(f"{t['id']}: posted {len(new_lines)} log line(s)")

        # Evidence bundle surfaces once the ticket reaches Review (or Done).
        bundle = tickets_dir / f"{t['id']}-bundle.md"
        if (
            not entry["bundle_posted"]
            and entry["issue_id"]
            and t["state"] in ("review", "done")
            and bundle.is_file()
        ):
            comment(key, entry["issue_id"],
                    "**Evidence bundle**\n\n" + bundle.read_text(), dry)
            if not dry:
                entry["bundle_posted"] = True
                save_map(map_path, mapping)
                log(f"{t['id']}: posted evidence bundle")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--factory-root", default=os.environ.get("FACTORY_ROOT", "."),
                    help="directory containing factory/ (default: FACTORY_ROOT or cwd)")
    ap.add_argument("--setup", action="store_true",
                    help="create/verify the Linear team and workflow states")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

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
        if args.setup:
            setup(key, mapping, map_path)
        sync(key, factory_dir, mapping, map_path, args.dry_run)
    except Exception as e:  # Linear is downstream: never fail the caller.
        log(f"sync error (will retry next cycle): {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
