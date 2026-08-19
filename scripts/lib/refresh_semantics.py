#!/usr/bin/env python3
"""Classify whether a protected-base advance changes review semantics."""

import argparse
from pathlib import Path
import re
import subprocess
import sys


SHA = re.compile(r"[0-9a-f]{40}")
MIGRATION = re.compile(r"factory/migrations/inflight-release/[0-9a-f]{40}\.json")
TICKET_FILE = re.compile(r"factory/tickets/(T-[0-9]+)\.md")
TICKET_ARTIFACT = re.compile(
    r"factory/(?:"
    r"tickets/(T-[0-9]+)(?:\.md|-bundle\.md|-evidence/[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.png)|"
    r"route-plans/(T-[0-9]+)\.json|"
    r"attestations/(T-[0-9]+)/(?:approval|bundle|dependency-refresh|done|refresh)\.json"
    r")"
)
MODIFIED_CONTROL = {
    "factory/KIT_PIN", "factory/QUALIFICATION.json", "factory/ledger.csv",
}


class ClassificationError(ValueError):
    pass


def git(repo, *args, check=True):
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if check and result.returncode:
        raise ClassificationError(f"git {args[0]} failed")
    return result


def regular_blob(repo, commit, path):
    rows = git(repo, "ls-tree", "-z", commit, "--", path).stdout.rstrip("\0").split("\0")
    return len(rows) == 1 and rows[0].startswith(f"100644 blob ") and rows[0].endswith(f"\t{path}")


def ticket_dependencies(repo, commit, ticket):
    value = git(repo, "show", f"{commit}:factory/tickets/{ticket}.md")
    fields = re.findall(r"(?m)^Depends-On:\s*(\S(?:.*\S)?)\s*$", value.stdout)
    if len(fields) > 1:
        return None
    if not fields or fields[0].casefold() == "none":
        return set()
    dependencies = [item.strip() for item in fields[0].split(",")]
    if len(dependencies) != len(set(dependencies)) or any(
        not re.fullmatch(r"T-[0-9]+", item) for item in dependencies
    ):
        return None
    return set(dependencies)


def ticket_artifact(path):
    match = TICKET_ARTIFACT.fullmatch(path)
    return next((ticket for ticket in match.groups() if ticket), None) if match else None


def preserved_control_paths(repo, old_head, base_head):
    """Return exact review-preserving base paths, or None when review changed."""
    if not SHA.fullmatch(old_head) or not SHA.fullmatch(base_head):
        raise ClassificationError("invalid protected-base identity")
    for commit in (old_head, base_head):
        if git(repo, "cat-file", "-e", f"{commit}^{{commit}}", check=False).returncode:
            raise ClassificationError("protected-base commit is unavailable")
    bases = git(repo, "merge-base", "--all", old_head, base_head).stdout.splitlines()
    if len(bases) != 1 or not SHA.fullmatch(bases[0]):
        raise ClassificationError("protected-base lineage is ambiguous")
    previous_base = bases[0]
    ticket_paths = git(
        repo, "diff", "--name-only", "-z", "--no-renames", previous_base, old_head,
    ).stdout.split("\0")
    if ticket_paths[-1:] == [""]:
        ticket_paths.pop()
    if len(ticket_paths) != len(set(ticket_paths)):
        raise ClassificationError("ticket diff contains a duplicate path")
    ticket_paths = set(ticket_paths)
    ticket_ids = {
        match.group(1) for path in ticket_paths
        if (match := TICKET_FILE.fullmatch(path))
    }
    if len(ticket_ids) != 1:
        return None
    ticket_id = next(iter(ticket_ids))
    dependencies = ticket_dependencies(repo, old_head, ticket_id)
    if dependencies is None:
        return None
    fields = git(
        repo, "diff", "--name-status", "-z", "--no-renames", previous_base, base_head,
    ).stdout.split("\0")
    if fields[-1:] == [""]:
        fields.pop()
    if len(fields) % 2:
        raise ClassificationError("protected-base diff is malformed")
    paths = set()
    for status, path in zip(fields[::2], fields[1::2]):
        if path in paths:
            raise ClassificationError("protected-base diff contains a duplicate path")
        paths.add(path)
        if path in MODIFIED_CONTROL:
            if status != "M" or not regular_blob(repo, previous_base, path):
                return None
        elif MIGRATION.fullmatch(path):
            if status != "A":
                return None
        else:
            artifact = ticket_artifact(path)
            if path.startswith("factory/") and (
                artifact is None or artifact == ticket_id or artifact in dependencies
            ):
                return None
            if (
                path in ticket_paths
                or status not in {"A", "M"}
                or status == "M" and not regular_blob(repo, previous_base, path)
            ):
                return None
        if not regular_blob(repo, base_head, path):
            return None
    return paths


def retained_control_paths(repo, head, base_head, paths):
    """Return reviewed-range base paths that still match protected main exactly."""
    retained = set()
    for path in paths:
        if not regular_blob(repo, head, path) or not regular_blob(repo, base_head, path):
            continue
        head_blob = git(repo, "rev-parse", f"{head}:{path}").stdout.strip()
        base_blob = git(repo, "rev-parse", f"{base_head}:{path}").stdout.strip()
        if SHA.fullmatch(head_blob) and head_blob == base_blob:
            retained.add(path)
    return retained


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--old-head", required=True)
    parser.add_argument("--base-head", required=True)
    args = parser.parse_args()
    paths = preserved_control_paths(Path(args.repo), args.old_head, args.base_head)
    print("PRESERVE" if paths is not None else "INVALIDATE")


if __name__ == "__main__":
    try:
        main()
    except (ClassificationError, OSError) as error:
        print(f"refresh-semantics: {error}", file=sys.stderr)
        raise SystemExit(2)
