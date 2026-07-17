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
        if name == "initiative" and value is None:
            continue
        safe_operator_string(name, value)
    if "priority" in operator and operator["priority"] not in PRIORITIES:
        raise ValueError("operator priority is invalid")
    if (
        "initiative" in operator
        and operator["initiative"] is not None
        and not re.fullmatch(r"I-[0-9]+", operator["initiative"])
    ):
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
        if key in operator
    }
    return hashlib.sha256(
        json.dumps(values, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def apply_operator_fields(text, operator):
    validate_operator(operator)
    validate_protected_fields(text)
    if "initiative" in operator and operator["initiative"] is None:
        text = re.sub(r"^Initiative:\s*.*\n?", "", text, count=1,
                      flags=re.MULTILINE | re.IGNORECASE)
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
    ticket_relative = (factory_dir / "tickets" / f"{ticket_id}.md").resolve().relative_to(repo).as_posix()
    done_relative = (
        factory_dir / "attestations" / ticket_id / "done.json"
    ).resolve().relative_to(repo).as_posix()
    done = subprocess.run(
        ["git", "-C", str(repo), "show", f"refs/remotes/origin/main:{done_relative}"],
        capture_output=True, text=True,
    )
    terminal_ticket = subprocess.run(
        ["git", "-C", str(repo), "show", f"refs/remotes/origin/main:{ticket_relative}"],
        capture_output=True, text=True,
    )
    bundle_relative = (
        factory_dir / "attestations" / ticket_id / "bundle.json"
    ).resolve().relative_to(repo).as_posix()
    approval_relative = (
        factory_dir / "attestations" / ticket_id / "approval.json"
    ).resolve().relative_to(repo).as_posix()
    bundle_receipt = subprocess.run(
        ["git", "-C", str(repo), "show", f"refs/remotes/origin/main:{bundle_relative}"],
        capture_output=True, text=True,
    )
    approval_receipt = subprocess.run(
        ["git", "-C", str(repo), "show", f"refs/remotes/origin/main:{approval_relative}"],
        capture_output=True, text=True,
    )
    if (
        done.returncode == 0
        and terminal_ticket.returncode == 0
        and bundle_receipt.returncode == 0
        and approval_receipt.returncode == 0
    ):
        try:
            attestation = json.loads(done.stdout)
            bundle_value = json.loads(bundle_receipt.stdout)
            approval_value = json.loads(approval_receipt.stdout)
        except json.JSONDecodeError:
            attestation = bundle_value = approval_value = {}
        def blob(content):
            return subprocess.run(
                ["git", "-C", str(repo), "hash-object", "--stdin"],
                input=content, capture_output=True, text=True, check=True,
            ).stdout.strip()
        terminal_state = re.search(
            r"^State:\s*Done\s*$", terminal_ticket.stdout, re.MULTILINE | re.IGNORECASE,
        )
        terminal_approval = re.search(
            r"^Operator-Approval:\s*Linear\s*$",
            terminal_ticket.stdout, re.MULTILINE | re.IGNORECASE,
        )
        required = attestation.get("required_checks")
        successful = attestation.get("successful_checks")
        ledger = attestation.get("ledger")
        oid_fields = (
            "merge_commit", "approved_pr_head", "reviewed_sha", "bundle_blob",
            "bundle_attestation_blob", "approval_attestation_blob",
            "approval_parent_head", "closeout_parent", "kit_sha",
        )
        checks_valid = (
            isinstance(required, list)
            and bool(required)
            and required == successful
            and len(required) == len(set(required))
            and all(
                isinstance(name, str)
                and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._:/()=-]{0,199}", name)
                for name in required
            )
        )
        if (
            attestation.get("schema") == "nysa.software-factory.ticket-done/v1"
            and attestation.get("ticket") == ticket_id
            and re.fullmatch(
                r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+",
                attestation.get("repository", ""),
            )
            and isinstance(attestation.get("pr_number"), int)
            and attestation["pr_number"] > 0
            and attestation.get("auto_merge_method") in {"squash", "merge", "rebase"}
            and all(
                re.fullmatch(r"[0-9a-f]{40}", attestation.get(name, ""))
                for name in oid_fields
            )
            and checks_valid
            and isinstance(attestation.get("merged_at"), str)
            and isinstance(attestation.get("attested_at"), str)
            and isinstance(ledger, dict)
            and ledger.get("schema") == "nysa.software-factory.ledger-projection/v1"
            and ledger.get("status") == "ok"
            and ledger.get("ticket") == ticket_id
            and isinstance(ledger.get("row_count"), int)
            and ledger["row_count"] >= 0
            and re.fullmatch(r"[0-9a-f]{64}", ledger.get("sha256", ""))
            and blob(bundle_receipt.stdout) == attestation.get("bundle_attestation_blob")
            and blob(approval_receipt.stdout) == attestation.get("approval_attestation_blob")
            and bundle_value.get("schema") == "nysa.software-factory.ticket-bundle/v1"
            and bundle_value.get("ticket") == ticket_id
            and bundle_value.get("repository") == attestation.get("repository")
            and bundle_value.get("pr_number") == attestation.get("pr_number")
            and bundle_value.get("reviewed_sha") == attestation.get("reviewed_sha")
            and bundle_value.get("bundle_blob") == attestation.get("bundle_blob")
            and bundle_value.get("kit_sha") == attestation.get("kit_sha")
            and approval_value.get("schema") == "nysa.software-factory.ticket-approval/v1"
            and approval_value.get("ticket") == ticket_id
            and approval_value.get("repository") == attestation.get("repository")
            and approval_value.get("pr_number") == attestation.get("pr_number")
            and approval_value.get("reviewed_sha") == attestation.get("reviewed_sha")
            and approval_value.get("bundle_blob") == attestation.get("bundle_blob")
            and approval_value.get("kit_sha") == attestation.get("kit_sha")
            and approval_value.get("auto_merge_method")
            == attestation.get("auto_merge_method")
            and approval_value.get("parent_head")
            == attestation.get("approval_parent_head")
            and terminal_state
            and terminal_approval
        ):
            terminal = subprocess.run(
                ["git", "-C", str(repo), "show", f"refs/remotes/origin/main:{relative}"],
                capture_output=True, text=True,
            )
            if terminal.returncode == 0:
                return terminal.stdout, "refs/remotes/origin/main"
    branch = f"{ticket_branch_prefix(factory_dir)}{ticket_id}"
    for ref in (f"refs/remotes/origin/{branch}", f"refs/heads/{branch}"):
        result = subprocess.run(
            ["git", "-C", str(repo), "show", f"{ref}:{relative}"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return result.stdout, ref
    result = subprocess.run(
        ["git", "-C", str(repo), "show", f"HEAD:{relative}"],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return result.stdout, "HEAD"
    return None, None


def committed_ticket(factory_dir, ticket_id):
    return committed_factory_file(factory_dir, ticket_id, f"{ticket_id}.md")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ticket-file")
    parser.add_argument("--operator-map")
    parser.add_argument("--factory-dir")
    parser.add_argument("--terminal-main", action="store_true")
    parser.add_argument("--ticket", required=True)
    parser.add_argument("--operator-version-file")
    args = parser.parse_args()
    if not re.fullmatch(r"T-\d+", args.ticket):
        parser.error("invalid ticket identifier")
    if args.terminal_main:
        if (
            not args.factory_dir
            or args.ticket_file
            or args.operator_map
            or args.operator_version_file
        ):
            parser.error("--terminal-main requires only --factory-dir and --ticket")
        text, source = committed_ticket(Path(args.factory_dir), args.ticket)
        if source != "refs/remotes/origin/main":
            raise SystemExit(1)
        print(text, end="")
        return
    if not args.ticket_file or not args.operator_map or args.factory_dir:
        parser.error("--ticket-file and --operator-map are required")
    text = Path(args.ticket_file).read_text()
    mapping = load_mapping(Path(args.operator_map))
    operator = operator_fields(mapping, args.ticket)
    if args.operator_version_file:
        Path(args.operator_version_file).write_text(operator_version(operator) + "\n")
    print(apply_operator_fields(text, operator), end="")


if __name__ == "__main__":
    main()
