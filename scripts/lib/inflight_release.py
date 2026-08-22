#!/usr/bin/env python3
"""Strict protected in-flight release authorization validation."""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import pathlib
import re
import subprocess


SCHEMA = "nysa.software-factory.inflight-release-authorization/v1"
SCHEMA_V2 = "nysa.software-factory.inflight-release-authorization/v2"
SHA = re.compile(r"^[0-9a-f]{40}$")
TICKET = re.compile(r"^T-[0-9]+$")
REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
BRANCH_PREFIX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*/$")
STATES = {
    "Ready", "Planning", "Building", "Review", "Awaiting Approval",
    "Approved", "Blocked-Escalated",
}


class AuthorizationError(ValueError):
    pass


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate key")
        value[key] = item
    return value


def _project_values(text: str, name: str) -> list[str]:
    values = []
    pattern = re.compile(rf"\s*(?:export\s+)?{re.escape(name)}\s*=\s*(.*?)\s*")
    for raw in text.splitlines():
        match = pattern.fullmatch(raw)
        if not match:
            continue
        value = match.group(1)
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values.append(value)
    return values


def project_identity(project: str) -> tuple[str, str]:
    repositories = _project_values(project, "GH_REPO")
    prefixes = _project_values(project, "TICKET_BRANCH_PREFIX")
    prefix = prefixes[0] if prefixes else "ticket/"
    if len(repositories) != 1 or not REPOSITORY.fullmatch(repositories[0]):
        raise AuthorizationError("product must define one exact GH_REPO")
    if (
        len(prefixes) > 1
        or not BRANCH_PREFIX.fullmatch(prefix)
        or any(item in prefix for item in ("..", "//", "@{", "\\", "~", "^", ":"))
    ):
        raise AuthorizationError("product ticket branch prefix is invalid")
    return repositories[0], prefix


def parse_authorization(
    raw: str, project: str, target_kit_sha: str,
) -> tuple[dict, dict[str, dict]]:
    if not all(isinstance(item, str) for item in (raw, project, target_kit_sha)):
        raise AuthorizationError("in-flight release authorization is malformed")
    if len(raw.encode("utf-8")) > 1024 * 1024:
        raise AuthorizationError("in-flight release authorization is malformed")
    try:
        value = json.loads(raw, object_pairs_hook=unique_object)
    except (json.JSONDecodeError, UnicodeError, ValueError):
        raise AuthorizationError("in-flight release authorization is malformed") from None
    repository, prefix = project_identity(project)
    if (
        not isinstance(value, dict)
        or set(value) != {
            "schema", "repository", "source_kit_sha", "target_kit_sha", "tickets",
        }
        or value.get("schema") not in {SCHEMA, SCHEMA_V2}
    ):
        raise AuthorizationError("in-flight release authorization is malformed")
    if value.get("repository") != repository:
        raise AuthorizationError(
            "in-flight release authorization repository does not match the product"
        )
    source = value.get("source_kit_sha", "")
    if (
        not isinstance(source, str)
        or not SHA.fullmatch(source)
        or not SHA.fullmatch(target_kit_sha)
        or source == target_kit_sha
        or value.get("target_kit_sha") != target_kit_sha
        or not isinstance(value.get("tickets"), list)
        or not value["tickets"]
    ):
        raise AuthorizationError("in-flight release authorization kit binding is invalid")
    entries = {}
    ordered = []
    entry_keys = {"ticket", "branch", "head", "state"}
    if value["schema"] == SCHEMA_V2:
        entry_keys.add("source_kit_sha")
    for item in value["tickets"]:
        if not isinstance(item, dict) or set(item) != entry_keys:
            raise AuthorizationError(
                "in-flight release authorization ticket entry is malformed"
            )
        ticket = item.get("ticket", "")
        if (
            not all(
                isinstance(item.get(name), str)
                for name in ("ticket", "branch", "head", "state")
            )
            or not TICKET.fullmatch(ticket)
            or item.get("branch") != prefix + ticket
            or not SHA.fullmatch(item.get("head", ""))
            or item.get("state") not in STATES
            or value["schema"] == SCHEMA_V2
            and (
                not isinstance(item.get("source_kit_sha"), str)
                or not SHA.fullmatch(item["source_kit_sha"])
                or item["source_kit_sha"] == target_kit_sha
            )
            or ticket in entries
        ):
            raise AuthorizationError(
                "in-flight release authorization ticket entry is invalid"
            )
        entries[ticket] = item
        ordered.append(ticket)
    if ordered != sorted(ordered):
        raise AuthorizationError("in-flight release authorization tickets are not canonical")
    return value, entries


def ticket_source_kit(authorization: dict, item: dict) -> str:
    return item.get("source_kit_sha", authorization["source_kit_sha"])


def authorize_ticket(
    authorization: dict,
    entries: dict[str, dict],
    *,
    ticket: str,
    branch: str,
    head: str,
    state: str,
    source_kit_sha: str,
) -> dict:
    item = entries.get(ticket)
    expected = {
        "ticket": ticket, "branch": branch, "head": head, "state": state,
    }
    if authorization.get("schema") == SCHEMA_V2:
        expected["source_kit_sha"] = source_kit_sha
    if item != expected or ticket_source_kit(authorization, item) != source_kit_sha:
        raise AuthorizationError(
            "%s does not match its exact in-flight release authorization" % ticket
        )
    return item


def _git(repo: pathlib.Path, *args: str, text: bool = True):
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=text,
    )
    if result.returncode:
        raise AuthorizationError("in-flight release authorization Git evidence is missing")
    return result.stdout


def _ticket_fields(text: str) -> tuple[str, str]:
    states = re.findall(r"(?mi)^State:\s*(.*?)\s*$", text)
    kits = re.findall(r"(?mi)^Kit-SHA:\s*(.*?)\s*$", text)
    if len(states) != 1 or len(kits) != 1 or not SHA.fullmatch(kits[0].strip()):
        raise AuthorizationError("authorized ticket fields are ambiguous")
    return states[0].strip(), kits[0].strip()


def _canonical(value) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode()


def _regular_blob(repo: pathlib.Path, commit: str, relative: str) -> str:
    line = _git(repo, "ls-tree", commit, "--", relative).rstrip("\n")
    match = re.fullmatch(
        r"100644 blob ([0-9a-f]{40})\t" + re.escape(relative), line,
    )
    if not match:
        raise AuthorizationError("authorized migration path mode is invalid")
    return match.group(1)


def _revision_hash(revision: int, parent_hash: str | None, body: dict) -> str:
    return hashlib.sha256(_canonical({
        "body": body, "parent_hash": parent_hash, "revision": revision,
    })).hexdigest()


def _verify_replay_route(
    repo: pathlib.Path,
    authorized_head: str,
    current_head: str,
    ticket: str,
    source: str,
    target: str,
) -> None:
    relative = f"factory/route-plans/{ticket}.json"
    source_raw = _git(repo, "show", f"{authorized_head}:{relative}", text=False)
    current_raw = _git(repo, "show", f"{current_head}:{relative}", text=False)
    try:
        old = json.loads(source_raw, object_pairs_hook=unique_object)
        new = json.loads(current_raw, object_pairs_hook=unique_object)
    except (json.JSONDecodeError, UnicodeError, ValueError):
        raise AuthorizationError("replayed route migration is malformed") from None
    if not isinstance(old, dict):
        raise AuthorizationError("authorized route document is malformed")
    pin_commit = _git(repo, "rev-list", "-1", authorized_head, "--", relative).strip()
    epoch = _git(repo, "show", "-s", "--format=%ct", pin_commit).strip()
    migrated_at = dt.datetime.fromtimestamp(int(epoch), dt.timezone.utc).replace(
        microsecond=0
    ).isoformat().replace("+00:00", "Z")
    if (
        not isinstance(new, dict)
        or set(new) != {"kit_sha", "revisions", "schema", "ticket"}
        or new.get("schema") != "ticket-model-route-journal/v2"
        or new.get("ticket") != ticket
        or new.get("kit_sha") != target
        or not isinstance(new.get("revisions"), list)
    ):
        raise AuthorizationError("replayed route migration is invalid")
    if old.get("schema") == "ticket-model-route-plan/v1":
        resolution = old.get("resolution") if isinstance(old, dict) else None
        if (
            set(old) != {"schema", "ticket", "kit_sha", "created_at", "resolution"}
            or old.get("ticket") != ticket
            or old.get("kit_sha") != source
            or not isinstance(resolution, dict)
            or len(new["revisions"]) != 2
        ):
            raise AuthorizationError("replayed legacy route migration is invalid")
        body = {
            "historical_selections": resolution.get("selections"),
            "kind": "migration",
            "legacy_plan_b64": base64.b64encode(source_raw).decode("ascii"),
            "legacy_plan_sha256": hashlib.sha256(source_raw).hexdigest(),
            "migrated_at": migrated_at,
            "new_kit_sha": source,
            "old_kit_sha": source,
            "pin_commit": pin_commit,
            "policy_hash": resolution.get("policy_hash"),
        }
        first = {"body": body, "parent_hash": None, "revision": 0}
        first["revision_hash"] = _revision_hash(0, None, body)
        if new["revisions"][0] != first:
            raise AuthorizationError("replayed legacy route provenance changed")
        prefix = [first]
    elif old.get("schema") == "ticket-model-route-journal/v2":
        if (
            set(old) != {"kit_sha", "revisions", "schema", "ticket"}
            or old.get("ticket") != ticket
            or old.get("kit_sha") != source
            or not isinstance(old.get("revisions"), list)
            or not old["revisions"]
            or not isinstance(old["revisions"][-1], dict)
            or new["revisions"][:-1] != old["revisions"]
            or len(new["revisions"]) != len(old["revisions"]) + 1
        ):
            raise AuthorizationError("replayed route history changed")
        prefix = old["revisions"]
    else:
        raise AuthorizationError("authorized route document schema is unsupported")
    tail = new["revisions"][-1]
    body = tail.get("body") if isinstance(tail, dict) else None
    revision = len(prefix)
    parent_hash = prefix[-1].get("revision_hash")
    if (
        not isinstance(body, dict)
        or body.get("kind") != "release-migration"
        or body.get("old_kit_sha") != source
        or body.get("new_kit_sha") != target
        or body.get("pin_commit") != pin_commit
        or body.get("migrated_at") != migrated_at
        or tail.get("revision") != revision
        or tail.get("parent_hash") != parent_hash
        or tail.get("revision_hash") != _revision_hash(revision, parent_hash, body)
    ):
        raise AuthorizationError("replayed route migration tail is invalid")


def verify_protected_ticket_pin(
    repo: pathlib.Path,
    protected: str,
    target: str,
    ticket: str,
    branch: str,
    authorized_head: str,
    state: str,
    source: str,
) -> None:
    """Validate the exact protected pin-only continuation of an authorized ticket."""
    if not all(SHA.fullmatch(value) for value in (protected, target, authorized_head, source)):
        raise AuthorizationError("protected ticket continuation ref is invalid")
    relative = f"factory/migrations/inflight-release/{target}.json"
    _regular_blob(repo, protected, relative)
    _regular_blob(repo, protected, "factory/KIT_PIN")
    _regular_blob(repo, protected, f"factory/tickets/{ticket}.md")
    raw = _git(repo, "show", f"{protected}:{relative}")
    project = _git(repo, "show", f"{protected}:factory/PROJECT.env")
    authorization, entries = parse_authorization(raw, project, target)
    authorize_ticket(
        authorization, entries, ticket=ticket, branch=branch,
        head=authorized_head, state=state, source_kit_sha=source,
    )
    ticket_path = f"factory/tickets/{ticket}.md"
    _regular_blob(repo, authorized_head, ticket_path)
    authorized = _git(repo, "show", f"{authorized_head}:{ticket_path}")
    authorized_state, authorized_kit = _ticket_fields(authorized)
    current = _git(repo, "show", f"{protected}:{ticket_path}")
    current_state, current_kit = _ticket_fields(current)
    pattern = re.compile(rf"(?mi)^Kit-SHA:[ \t]*{re.escape(source)}[ \t]*$")
    if (
        authorized_state != state
        or authorized_kit != source
        or current_state != state
        or current_kit != target
        or len(pattern.findall(authorized)) != 1
        or pattern.sub("Kit-SHA: " + target, authorized) != current
        or _git(repo, "show", f"{protected}:factory/KIT_PIN") != target + "\n"
    ):
        raise AuthorizationError("protected ticket continuation changed unauthorized fields")


def verify_migration(
    repo: pathlib.Path,
    protected: str,
    target: str,
    ticket: str,
    branch: str,
    current_head: str,
    *,
    allow_legacy_pinless: bool = False,
) -> str:
    if not all(SHA.fullmatch(value) for value in (protected, target, current_head)):
        raise AuthorizationError("in-flight release authorization ref is invalid")
    relative = f"factory/migrations/inflight-release/{target}.json"
    raw = _git(repo, "show", f"{protected}:{relative}")
    project = _git(repo, "show", f"{protected}:factory/PROJECT.env")
    authorization, entries = parse_authorization(raw, project, target)
    item = entries.get(ticket)
    if item is None or item["branch"] != branch:
        raise AuthorizationError("ticket is absent from in-flight release authorization")
    source = ticket_source_kit(authorization, item)
    authorized_head = item["head"]
    source_ticket = _git(
        repo, "show", f"{authorized_head}:factory/tickets/{ticket}.md"
    )
    source_state, source_kit = _ticket_fields(source_ticket)
    authorize_ticket(
        authorization, entries, ticket=ticket, branch=branch,
        head=authorized_head, state=source_state, source_kit_sha=source_kit,
    )
    migration_paths = (
        "factory/KIT_PIN", f"factory/tickets/{ticket}.md",
        f"factory/route-plans/{ticket}.json",
    )
    checked_paths = migration_paths[1:]
    for relative in checked_paths:
        _regular_blob(repo, authorized_head, relative)
    source_pin_line = _git(
        repo, "ls-tree", authorized_head, "--", "factory/KIT_PIN",
    ).rstrip("\n")
    source_pin = ""
    if source_pin_line:
        _regular_blob(repo, authorized_head, "factory/KIT_PIN")
        source_pin = _git(repo, "show", f"{authorized_head}:factory/KIT_PIN")
        if not SHA.fullmatch(source_pin.rstrip("\n")) or source_pin.count("\n") != 1:
            raise AuthorizationError("authorized factory KIT_PIN is invalid")
    elif not allow_legacy_pinless:
        raise AuthorizationError("authorized factory KIT_PIN is invalid")
    if current_head == authorized_head:
        return "exact"
    parents = _git(repo, "show", "-s", "--format=%P", current_head).split()
    changed = _git(
        repo, "diff-tree", "--no-commit-id", "--name-status", "--no-renames",
        "-r", current_head,
    ).splitlines()
    expected_paths = [
        f"M\tfactory/route-plans/{ticket}.json",
        f"M\tfactory/tickets/{ticket}.md",
    ]
    changed_paths = set(changed)
    if (
        (not allow_legacy_pinless and source_pin != target + "\n")
        or "M\tfactory/KIT_PIN" in changed_paths
    ):
        expected_paths.append("M\tfactory/KIT_PIN")
    if parents != [authorized_head] or sorted(changed) != sorted(expected_paths):
        raise AuthorizationError("current head is not the exact authorized migration child")
    current_ticket = _git(repo, "show", f"{current_head}:factory/tickets/{ticket}.md")
    current_state, current_kit = _ticket_fields(current_ticket)
    validate_target_pin = (
        not allow_legacy_pinless or "M\tfactory/KIT_PIN" in changed_paths
    )
    checked_current_paths = (
        migration_paths if validate_target_pin else checked_paths
    )
    for relative in checked_current_paths:
        _regular_blob(repo, current_head, relative)
    pattern = re.compile(
        rf"(?mi)^Kit-SHA:[ \t]*{re.escape(source)}[ \t]*$"
    )
    if (
        current_state != source_state
        or current_kit != target
        or len(pattern.findall(source_ticket)) != 1
        or pattern.sub("Kit-SHA: " + target, source_ticket) != current_ticket
    ):
        raise AuthorizationError("replayed ticket migration changed unauthorized fields")
    if validate_target_pin and _git(
        repo, "show", f"{current_head}:factory/KIT_PIN",
    ) != target + "\n":
        raise AuthorizationError("replayed factory KIT_PIN migration is invalid")
    _verify_replay_route(repo, authorized_head, current_head, ticket, source, target)
    return "replay"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=pathlib.Path, required=True)
    parser.add_argument("--protected", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--ticket", required=True)
    parser.add_argument("--branch", required=True)
    parser.add_argument("--head", required=True)
    args = parser.parse_args()
    try:
        print(verify_migration(
            args.repo, args.protected, args.target, args.ticket, args.branch, args.head,
        ))
    except (AuthorizationError, OSError, OverflowError, UnicodeError, ValueError):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
