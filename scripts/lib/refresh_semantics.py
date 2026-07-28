#!/usr/bin/env python3
"""Classify whether a protected-base advance changes review semantics."""

import argparse
from pathlib import Path
import re
import subprocess
import sys


SHA = re.compile(r"[0-9a-f]{40}")
MIGRATION = re.compile(r"factory/migrations/inflight-release/[0-9a-f]{40}\.json")
MODIFIED_CONTROL = {"factory/KIT_PIN", "factory/QUALIFICATION.json"}


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


def preserved_control_paths(repo, old_head, base_head):
    """Return exact non-semantic paths, or None when review must be invalidated."""
    if not SHA.fullmatch(old_head) or not SHA.fullmatch(base_head):
        raise ClassificationError("invalid protected-base identity")
    for commit in (old_head, base_head):
        if git(repo, "cat-file", "-e", f"{commit}^{{commit}}", check=False).returncode:
            raise ClassificationError("protected-base commit is unavailable")
    bases = git(repo, "merge-base", "--all", old_head, base_head).stdout.splitlines()
    if len(bases) != 1 or not SHA.fullmatch(bases[0]):
        raise ClassificationError("protected-base lineage is ambiguous")
    previous_base = bases[0]
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
            return None
        if not regular_blob(repo, base_head, path):
            return None
    return paths


def retained_control_paths(repo, head, base_head, paths):
    """Return reviewed-range control paths that exactly match protected base."""
    retained = set()
    for path in paths:
        if path not in MODIFIED_CONTROL and not MIGRATION.fullmatch(path):
            continue
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
