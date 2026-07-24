#!/usr/bin/env python3
"""Build a tests-first, two-commit mailbox from an approved product tree."""

import argparse
import os
import pathlib
import re
import shlex
import subprocess
import tempfile


SAFE_PATH = re.compile(r"[A-Za-z0-9._][A-Za-z0-9._/-]*")


def git(repo, *args, input=None, text=True):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        input=input,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
        text=text,
    ).stdout


def safe_path(path, *, directory=False):
    value = path[:-1] if directory and path.endswith("/") else path
    parts = value.split("/")
    return bool(
        value
        and SAFE_PATH.fullmatch(value)
        and all(part not in {"", ".", ".."} for part in parts)
    )


def test_paths(repo, base):
    text = git(repo, "show", f"{base}:factory/PROJECT.env")
    values = [
        line.split("=", 1)[1]
        for line in text.splitlines()
        if line.startswith("TEST_PATHS=")
    ]
    if len(values) != 1:
        raise ValueError("base PROJECT.env must define exactly one TEST_PATHS")
    words = shlex.split(values[0], comments=False, posix=True)
    paths = " ".join(words).split()
    if (
        not paths
        or len(paths) != len(set(paths))
        or any(not safe_path(path, directory=True) for path in paths)
        or any(path == "factory" or path.startswith("factory/") for path in paths)
    ):
        raise ValueError("TEST_PATHS is empty, duplicate, reserved, or unsafe")
    normalized = [path.rstrip("/") for path in paths]
    for i, left in enumerate(normalized):
        for right in normalized[i + 1 :]:
            if left == right or left.startswith(right + "/") or right.startswith(left + "/"):
                raise ValueError("TEST_PATHS entries overlap")
    return paths


def is_test(path, paths):
    return any(path.startswith(item) if item.endswith("/") else path == item for item in paths)


def changed_paths(repo, base, reviewed):
    raw = git(repo, "diff", "--name-status", "-z", "-M", "-C", base, reviewed, text=False)
    fields = raw.decode("utf-8").split("\0")
    fields.pop()
    changes = []
    index = 0
    while index < len(fields):
        status = fields[index]
        index += 1
        if status.startswith(("R", "C")):
            raise ValueError("renames and copies are unsafe for publication stratification")
        if status not in {"A", "D", "M", "T"} or index >= len(fields):
            raise ValueError("unsupported application change")
        path = fields[index]
        index += 1
        if not safe_path(path) or path == "factory" or path.startswith("factory/"):
            if path == "factory" or path.startswith("factory/"):
                continue
            raise ValueError("changed application path is unsafe")
        changes.append(path)
    if len(changes) != len(set(changes)):
        raise ValueError("application change set overlaps")
    return changes


def reject_unsafe_history(repo, base, reviewed):
    if git(repo, "rev-list", "--min-parents=2", f"{base}..{reviewed}").strip():
        raise ValueError("reviewed history contains a merge")
    for commit in git(repo, "rev-list", "--reverse", f"{base}..{reviewed}").splitlines():
        raw = git(
            repo,
            "diff-tree",
            "--root",
            "-r",
            "--no-commit-id",
            "--raw",
            "--no-abbrev",
            commit,
            "--",
            ".",
            ":(exclude)factory",
        )
        for line in raw.splitlines():
            modes = line.split("\t", 1)[0].split()
            if len(modes) < 2 or modes[0][1:] in {"120000", "160000"} or modes[1] in {
                "120000",
                "160000",
            }:
                raise ValueError("application history contains a symlink or submodule")


def update_index(repo, index, reviewed, paths):
    env = {**os.environ, "GIT_INDEX_FILE": str(index)}
    for path in paths:
        entry = git(repo, "ls-tree", reviewed, "--", path).strip().split()
        if not entry:
            subprocess.run(
                ["git", "-C", str(repo), "update-index", "--force-remove", "--", path],
                env=env,
                check=True,
            )
            continue
        if len(entry) < 3 or entry[1] != "blob" or entry[0] in {"120000", "160000"}:
            raise ValueError("publication path is not a regular blob")
        subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "update-index",
                "--add",
                "--cacheinfo",
                entry[0],
                entry[2],
                path,
            ],
            env=env,
            check=True,
        )


def commit_tree(repo, tree, parent, message, timestamp):
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Factory Trusted Host",
        "GIT_AUTHOR_EMAIL": "factory-trusted-host@local",
        "GIT_COMMITTER_NAME": "Factory Trusted Host",
        "GIT_COMMITTER_EMAIL": "factory-trusted-host@local",
        "GIT_AUTHOR_DATE": f"@{timestamp} +0000",
        "GIT_COMMITTER_DATE": f"@{timestamp} +0000",
    }
    return subprocess.run(
        ["git", "-C", str(repo), "commit-tree", tree, "-p", parent],
        input=message + "\n",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
        text=True,
        env=env,
    ).stdout.strip()


def build(repo, ticket, base, reviewed, temporary_parent):
    policy = test_paths(repo, base)
    reject_unsafe_history(repo, base, reviewed)
    changes = changed_paths(repo, base, reviewed)
    tests = [path for path in changes if is_test(path, policy)]
    implementation = [path for path in changes if path not in tests]
    if not tests or not implementation:
        raise ValueError("publication requires nonempty test and implementation strata")

    with tempfile.TemporaryDirectory(
        prefix="factory-export-mbox.", dir=temporary_parent
    ) as temporary:
        bare = pathlib.Path(temporary, "repo.git")
        subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
        git(bare, "fetch", "-q", str(repo), base, reviewed)
        index = pathlib.Path(temporary, "index")
        env = {**os.environ, "GIT_INDEX_FILE": str(index)}
        subprocess.run(["git", "-C", str(bare), "read-tree", base], env=env, check=True)
        update_index(bare, index, reviewed, tests)
        test_tree = subprocess.run(
            ["git", "-C", str(bare), "write-tree"],
            env=env,
            check=True,
            stdout=subprocess.PIPE,
            text=True,
        ).stdout.strip()
        test_commit = commit_tree(
            bare,
            test_tree,
            base,
            f"{ticket}: publish approved tests",
            git(bare, "show", "-s", "--format=%ct", base).strip(),
        )
        update_index(bare, index, reviewed, implementation)
        final_tree = subprocess.run(
            ["git", "-C", str(bare), "write-tree"],
            env=env,
            check=True,
            stdout=subprocess.PIPE,
            text=True,
        ).stdout.strip()
        implementation_commit = commit_tree(
            bare,
            final_tree,
            test_commit,
            f"{ticket}: publish approved implementation",
            git(bare, "show", "-s", "--format=%ct", reviewed).strip(),
        )
        if git(
            bare,
            "diff",
            "--name-only",
            implementation_commit,
            reviewed,
            "--",
            ".",
            ":(exclude)factory",
        ).strip():
            raise ValueError("publication tree differs from reviewed application tree")
        return git(
            bare,
            "format-patch",
            "--stdout",
            "--binary",
            f"{base}..{implementation_commit}",
            text=False,
        )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--ticket", required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--reviewed", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"T-[0-9]+", args.ticket):
        raise ValueError("invalid ticket")
    output = pathlib.Path(args.output)
    data = build(
        pathlib.Path(args.repo),
        args.ticket,
        args.base,
        args.reviewed,
        output.parent,
    )
    if not data:
        raise ValueError("publication mailbox is empty")
    output.write_bytes(data)


if __name__ == "__main__":
    try:
        main()
    except (OSError, subprocess.CalledProcessError, ValueError) as error:
        raise SystemExit(f"product-export-mbox: {error}")
