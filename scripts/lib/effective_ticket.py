#!/usr/bin/env python3
"""Read committed ticket content with Linear-owned operator fields overlaid."""

import argparse
import hashlib
import json
import re
import subprocess
import unicodedata
from pathlib import Path

MATERIALIZED_OPERATOR_FIELDS = ("priority", "initiative", "state", "approval")
OPERATOR_METADATA_FIELDS = ("state_base", "observed_at", "linear_updated_at")
OPERATOR_FIELDS = frozenset(MATERIALIZED_OPERATOR_FIELDS + OPERATOR_METADATA_FIELDS)
PRIORITIES = frozenset(("none", "urgent", "high", "normal", "low"))
STATES = {
    "Backlog": "backlog",
    "Ready": "ready",
    "Planning": "planning",
    "Building": "building",
    "Review": "review",
    "Awaiting Approval": "awaiting approval",
    "Approved": "approved",
    "Blocked-Escalated": "blocked-escalated",
    "Done": "done",
}
PROTECTED_TICKET_FIELDS = (
    "Priority", "Initiative", "State", "Operator-Approval",
    "Operator-Approval-Attestation",
)


def safe_operator_string(name, value):
    if not isinstance(value, str):
        raise ValueError(f"operator {name} must be a string")
    if not value or value != value.strip():
        raise ValueError(f"operator {name} has an invalid value")
    if any(unicodedata.category(character) == "Cc" for character in value):
        raise ValueError(f"operator {name} contains a control character")
    return value


def validate_operator(operator):
    if not isinstance(operator, dict):
        raise ValueError("operator overlay must be an object")
    if any(not isinstance(name, str) for name in operator):
        raise ValueError("operator overlay field names must be strings")
    unknown = sorted(set(operator) - OPERATOR_FIELDS)
    if unknown:
        raise ValueError(f"operator overlay contains unknown fields: {', '.join(unknown)}")
    for name, value in operator.items():
        if name == "linear_updated_at" and value is None:
            continue
        safe_operator_string(name, value)
    if "priority" in operator and operator["priority"] not in PRIORITIES:
        raise ValueError("operator priority is invalid")
    if "initiative" in operator and not re.fullmatch(r"I-[0-9]+", operator["initiative"]):
        raise ValueError("operator initiative is invalid")
    if "state" in operator and operator["state"] not in STATES:
        raise ValueError("operator state is invalid")
    if "state_base" in operator and operator["state_base"] not in STATES.values():
        raise ValueError("operator state_base is invalid")
    if "approval" in operator and operator["approval"] != "Linear":
        raise ValueError("operator approval is invalid")
    if (operator.get("state") == "Approved") != (operator.get("approval") == "Linear"):
        raise ValueError("operator Approved state and Linear approval must appear together")
    return operator


def replace_field(text, name, value):
    pattern = re.compile(rf"^{re.escape(name)}:\s*.*$", re.MULTILINE | re.IGNORECASE)
    if len(pattern.findall(text)) > 1:
        raise ValueError(f"ticket contains duplicate {name} fields")
    line = f"{name}: {value}"
    if pattern.search(text):
        return pattern.sub(line, text, count=1)
    lines = text.splitlines()
    insert_at = 1
    while insert_at < len(lines) and not lines[insert_at].strip():
        insert_at += 1
    lines.insert(insert_at, line)
    return "\n".join(lines) + ("\n" if text.endswith("\n") else "")


def validate_protected_fields(text):
    for name in PROTECTED_TICKET_FIELDS:
        pattern = re.compile(rf"^{re.escape(name)}:\s*.*$", re.MULTILINE | re.IGNORECASE)
        if len(pattern.findall(text)) > 1:
            raise ValueError(f"ticket contains duplicate {name} fields")


def operator_fields(mapping, ticket_id):
    if not isinstance(mapping, dict):
        raise ValueError("operator map must be an object")
    tickets = mapping.get("tickets", {})
    if not isinstance(tickets, dict):
        raise ValueError("operator map tickets must be an object")
    entry = tickets.get(ticket_id, {})
    if not isinstance(entry, dict):
        raise ValueError("operator map ticket entry must be an object")
    operator = entry.get("operator")
    if operator is None:
        return {}
    return validate_operator(operator)


def operator_version(operator):
    validate_operator(operator)
    values = {
        key: operator[key]
        for key in MATERIALIZED_OPERATOR_FIELDS
        if operator.get(key)
    }
    return hashlib.sha256(
        json.dumps(values, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def materialized_operator_version(text):
    """Hash the protected operator fields as they exist in ticket content."""
    validate_protected_fields(text)
    operator = {}
    for key, name in (
        ("priority", "Priority"),
        ("initiative", "Initiative"),
        ("state", "State"),
        ("approval", "Operator-Approval"),
    ):
        match = re.search(
            rf"^{re.escape(name)}:\s*(.+)$", text, re.MULTILINE | re.IGNORECASE
        )
        if match:
            operator[key] = match.group(1).strip()
    return operator_version(operator)


def apply_operator_fields(text, operator):
    validate_operator(operator)
    validate_protected_fields(text)
    for key, name in (("priority", "Priority"), ("initiative", "Initiative"), ("state", "State")):
        if operator.get(key):
            text = replace_field(text, name, operator[key])
    if operator.get("approval"):
        text = replace_field(text, "Operator-Approval", operator["approval"])
    return text


def load_mapping(path):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return {}


def ticket_branch_prefix(factory_dir):
    descriptor = factory_dir / "PROJECT.env"
    values = []
    if descriptor.is_file() and not descriptor.is_symlink():
        for raw in descriptor.read_text().splitlines():
            line = re.sub(r"^\s*export\s+", "", raw.strip())
            if not line or line.startswith("#") or not line.startswith("TICKET_BRANCH_PREFIX"):
                continue
            match = re.fullmatch(r"TICKET_BRANCH_PREFIX\s*=\s*(.*)", line)
            if match:
                value = match.group(1).strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                values.append(value)
    if len(values) > 1:
        raise ValueError("product ticket branch prefix must be defined at most once")
    prefix = values[0] if values else "ticket/"
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*/", prefix) or any(
        item in prefix for item in ("..", "//", "@{", "\\", "~", "^", ":")
    ):
        raise ValueError("product ticket branch prefix is invalid")
    return prefix


def committed_factory_file(factory_dir, ticket_id, filename):
    """Return (text, ref); prefer the durable remote ticket branch."""
    fallback = factory_dir / "tickets" / filename
    try:
        repo = Path(
            subprocess.run(
                ["git", "-C", str(factory_dir), "rev-parse", "--show-toplevel"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
        ).resolve()
    except subprocess.CalledProcessError:
        if fallback.is_file():
            return fallback.read_text(), "main-worktree"
        return None, None
    relative = fallback.resolve().relative_to(repo).as_posix()
    branch = f"{ticket_branch_prefix(factory_dir)}{ticket_id}"
    for ref in (f"refs/remotes/origin/{branch}", f"refs/heads/{branch}"):
        result = subprocess.run(
            ["git", "-C", str(repo), "show", f"{ref}:{relative}"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return result.stdout, ref
    if fallback.is_file():
        return fallback.read_text(), "main-worktree"
    return None, None


def committed_ticket(factory_dir, ticket_id):
    return committed_factory_file(factory_dir, ticket_id, f"{ticket_id}.md")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ticket-file", required=True)
    parser.add_argument("--operator-map", required=True)
    parser.add_argument("--ticket", required=True)
    parser.add_argument("--operator-version-file")
    args = parser.parse_args()
    if not re.fullmatch(r"T-\d+", args.ticket):
        parser.error("invalid ticket identifier")
    text = Path(args.ticket_file).read_text()
    mapping = load_mapping(Path(args.operator_map))
    operator = operator_fields(mapping, args.ticket)
    if args.operator_version_file:
        Path(args.operator_version_file).write_text(operator_version(operator) + "\n")
    print(apply_operator_fields(text, operator), end="")


if __name__ == "__main__":
    main()
