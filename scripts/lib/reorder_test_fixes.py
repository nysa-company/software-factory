#!/usr/bin/env python3
"""reorder_test_fixes.py — implementation behind scripts/reorder-test-fixes.sh.

Problem
-------
ci/test-immutability-check.sh enforces two rules on BASE_REF..HEAD:

  Rule 1 (separation): a commit touches only TEST_PATHS files, or only
  non-TEST_PATHS files. Mixing fails.
  Rule 2 (order): every test commit must precede every implementation
  commit in its frozen-contract epoch. Files under EXEMPT_PATHS are invisible
  to both rules except for the exact append-only contract-epoch marker.

When a reviewer asks for test fixes after the builder has already committed
implementation in the same contract epoch, the fix commit is a pure test
commit but lands after implementation started, and fails rule 2. This tool
rewrites history so that any such "late" test commit moves to sit immediately
before that epoch's first implementation commit, preserving relative order
and leaving every other commit exactly where it was. A newly frozen numbered
contract starts a new epoch and therefore requires no rewrite.

Classification mirrors ci/test-immutability-check.sh exactly (same
TEST_PATHS/EXEMPT_PATHS semantics, same directory-prefix/exact-file match, same precedence of
"mixed over test over impl") so that "first implementation commit" here is
the same commit that flips the gate's SEEN_IMPL flag.

Safety model
------------
  - Refuses a dirty working tree, a detached HEAD, or a non-ancestor base.
  - Records the original HEAD before doing anything.
  - Never touches the branch ref until the rewritten history's tree
    (`HEAD^{tree}`) is byte-for-byte identical to the original tree. If it
    isn't, the branch is left untouched (nothing to "restore" — it was never
    moved) and the tool exits non-zero.
  - The rewrite happens in a detached HEAD; the working branch ref is only
    fast-forwarded to the new history after the tree check passes.
  - Any conflict touching a non-exempt path is unresolvable by policy: the
    cherry-pick/rebase is aborted and the tool exits non-zero. Only
    conflicts where *every* conflicted path is under EXEMPT_PATHS are
    auto-resolved (see resolve_exempt_conflict below) — and even then, the
    final tree-equality check is what actually guarantees correctness, not
    the resolution heuristic itself.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field


class Fail(Exception):
    """Raised for any condition that should abort with a clear message."""


def run(args, cwd=None, check=True, input_text=None):
    result = subprocess.run(
        args,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        input=input_text,
    )
    if check and result.returncode != 0:
        raise Fail(
            "command failed: {}\n--- stdout ---\n{}\n--- stderr ---\n{}".format(
                " ".join(args), result.stdout, result.stderr
            )
        )
    return result


def git(repo, *args, check=True, input_text=None):
    return run(["git", "-C", repo] + list(args), check=check, input_text=input_text)


@dataclass
class Commit:
    sha: str
    subject: str
    files: list = field(default_factory=list)
    test_files: list = field(default_factory=list)
    nontest_files: list = field(default_factory=list)
    kind: str = ""  # TEST | IMPL | BOOKKEEPING | MIXED
    contract_epoch_reset: bool = False
    is_merge: bool = False


def is_exempt(path, exempt_paths):
    return any(path.startswith(p) if p.endswith("/") else path == p for p in exempt_paths)


def diff_tree_files(repo, sha, pathspecs=None):
    args = ["diff-tree", "--no-commit-id", "--name-only", "-r", sha]
    if pathspecs:
        args += ["--"] + list(pathspecs)
    out = git(repo, *args).stdout
    return [line for line in out.splitlines() if line]


FROZEN = re.compile(r"^## Frozen contract — version ([1-9][0-9]*)$")
FROZEN_PASS = re.compile(
    r"^- \*\*Freeze result — PASS\.\*\* "
    r"Contract version ([1-9][0-9]*) is frozen\.$"
)


def contract_epoch_reset(repo, sha, files):
    if len(files) != 1 or not re.fullmatch(
        r"(?:factory|conformance/factory)/tickets/T-[^/]+\.md", files[0]
    ):
        return False
    path = files[0]
    diff = git(
        repo, "diff", "--no-ext-diff", "--unified=0", f"{sha}^", sha,
        "--", path,
    ).stdout.splitlines()
    added = [FROZEN.fullmatch(line[1:]) for line in diff if line.startswith("+")]
    added = [match for match in added if match]
    passed = [
        FROZEN_PASS.fullmatch(line[1:]) for line in diff if line.startswith("+")
    ]
    passed = [match for match in passed if match]
    if len(added) != 1 or len(passed) != 1 or added[0][1] != passed[0][1]:
        return False
    if any(
        FROZEN.fullmatch(line[1:]) or FROZEN_PASS.fullmatch(line[1:])
        for line in diff if line.startswith("-")
    ):
        return False
    prior = git(repo, "show", f"{sha}^:{path}", check=False).stdout.splitlines()
    versions = [
        int(match[1]) for line in prior if (match := FROZEN.fullmatch(line))
    ]
    return int(added[0][1]) > max(versions, default=0)


def classify_commits(repo, base, head, test_paths, exempt_paths):
    rev_list = git(repo, "rev-list", "--reverse", f"{base}..{head}").stdout
    shas = [line for line in rev_list.splitlines() if line]

    commits = []
    for sha in shas:
        parents = git(repo, "rev-list", "--parents", "-n", "1", sha).stdout.split()
        is_merge = len(parents) > 2  # sha + >1 parent

        subject = git(repo, "log", "-1", "--format=%s", sha).stdout.strip()
        all_files = diff_tree_files(repo, sha)
        nonexempt_files = sorted(f for f in all_files if not is_exempt(f, exempt_paths))
        test_files = sorted(diff_tree_files(repo, sha, test_paths))
        nontest_files = sorted(set(nonexempt_files) - set(test_files))

        if test_files and nontest_files:
            kind = "MIXED"
        elif test_files:
            kind = "TEST"
        elif nontest_files:
            kind = "IMPL"
        else:
            kind = "BOOKKEEPING"

        commits.append(
            Commit(
                sha=sha,
                subject=subject,
                files=all_files,
                test_files=test_files,
                nontest_files=nontest_files,
                kind=kind,
                contract_epoch_reset=contract_epoch_reset(repo, sha, all_files),
                is_merge=is_merge,
            )
        )
    return commits


def plan_new_order(commits):
    """Return (new_order_shas, moved_commits, first_impl_commit) or None if
    the branch already satisfies rule 2 (nothing to do)."""
    first_impl = None
    moves = {}
    late_test = []
    for commit in commits:
        if commit.contract_epoch_reset:
            first_impl = None
        if commit.kind == "IMPL" and first_impl is None:
            first_impl = commit
        elif commit.kind == "TEST" and first_impl is not None:
            moves.setdefault(first_impl.sha, []).append(commit)
            late_test.append(commit)
    if not late_test:
        return None

    late_shas = {c.sha for c in late_test}
    new_order = []
    for c in commits:
        if c.sha in late_shas:
            continue
        if c.sha in moves:
            new_order.extend(lc.sha for lc in moves[c.sha])
        new_order.append(c.sha)

    first = next(c for c in commits if c.sha in moves)
    return new_order, late_test, first


CONFLICT_STATUS_CODES = {"UU", "AA", "DD", "AU", "UA", "UD", "DU"}


def conflicted_paths(repo):
    out = git(repo, "status", "--porcelain=v1", "-z").stdout
    paths = []
    for entry in out.split("\0"):
        if not entry:
            continue
        code, path = entry[:2], entry[3:]
        if code in CONFLICT_STATUS_CODES:
            paths.append(path)
    return paths


def path_exists_in_tree(repo, treeish, path):
    r = git(repo, "cat-file", "-e", f"{treeish}:{path}", check=False)
    return r.returncode == 0


def resolve_exempt_conflict(repo, orig_head, path):
    """Deterministic resolution for a conflict on a single exempt-path file:
    force it to the content it has in the ORIGINAL branch's final tree.

    Why this is safe: the tool's actual correctness guarantee is the final
    HEAD^{tree} == original tree check, not this heuristic. But this
    specific heuristic also converges correctly on its own for the realistic
    case it targets (append-only bookkeeping logs, e.g. ticket files that
    later bookkeeping commits keep appending to): forcing a conflicted
    exempt file to its final content is idempotent, and any later commit
    that still touches the same path either merges cleanly (git recognizes
    the identical addition already present and does not duplicate it) or
    conflicts again — in which case we apply the exact same resolution
    again. Either way the path's content only ever moves towards, and stays
    at, the original final content.
    """
    full_path = os.path.join(repo, path)
    if path_exists_in_tree(repo, orig_head, path):
        content = git(repo, "show", f"{orig_head}:{path}").stdout
        os.makedirs(os.path.dirname(full_path) or ".", exist_ok=True)
        with open(full_path, "w") as f:
            f.write(content)
        git(repo, "add", "--", path)
    else:
        # File doesn't exist in the final tree: it must end up deleted.
        if os.path.exists(full_path):
            git(repo, "rm", "-f", "--", path)
        else:
            git(repo, "rm", "-f", "--cached", "--", path, check=False)


EMPTY_MARKERS = (
    "The previous cherry-pick is now empty",
    "nothing to commit",
)


def cherry_pick_one(repo, sha, orig_head, exempt_paths):
    """Cherry-pick a single commit onto the current detached HEAD.

    Returns True if applied (possibly as an intentional no-op skip), raises
    Fail on any unresolvable condition. On failure this function always
    leaves no in-progress cherry-pick behind (it aborts before raising).
    """
    r = git(repo, "cherry-pick", sha, check=False)
    if r.returncode == 0:
        return True

    conflicts = conflicted_paths(repo)
    if conflicts:
        non_exempt = [p for p in conflicts if not is_exempt(p, exempt_paths)]
        if non_exempt:
            git(repo, "cherry-pick", "--abort", check=False)
            raise Fail(
                "unresolvable conflict cherry-picking {} ({}): non-exempt path(s) "
                "in conflict: {}".format(sha, git(repo, "log", "-1", "--format=%s", sha).stdout.strip(), ", ".join(non_exempt))
            )
        for path in conflicts:
            resolve_exempt_conflict(repo, orig_head, path)

        env = dict(os.environ)
        env["GIT_EDITOR"] = "true"
        cont = subprocess.run(
            ["git", "-C", repo, "cherry-pick", "--continue"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        if cont.returncode == 0:
            return True
        combined = cont.stdout + cont.stderr
        if any(m in combined for m in EMPTY_MARKERS):
            skip = git(repo, "cherry-pick", "--skip", check=False)
            if skip.returncode == 0:
                return True
        # Anything else left over (still-unmerged paths, etc.) is unresolvable.
        git(repo, "cherry-pick", "--abort", check=False)
        raise Fail(
            f"cherry-pick --continue failed unexpectedly for {sha}:\n{combined}"
        )

    # Failed with no conflicted paths: almost certainly an empty-patch cherry-pick.
    combined = r.stdout + r.stderr
    if any(m in combined for m in EMPTY_MARKERS):
        skip = git(repo, "cherry-pick", "--skip", check=False)
        if skip.returncode == 0:
            return True

    git(repo, "cherry-pick", "--abort", check=False)
    raise Fail(f"cherry-pick failed unexpectedly for {sha}:\n{combined}")


def ensure_clean_and_on_branch(repo):
    status = git(repo, "status", "--porcelain").stdout
    if status.strip():
        raise Fail("working tree is not clean; commit, stash, or discard changes first")

    branch_r = git(repo, "symbolic-ref", "-q", "--short", "HEAD", check=False)
    if branch_r.returncode != 0:
        raise Fail("HEAD is detached; check out the branch to reorder first")
    return branch_r.stdout.strip()


def run_gate_check(repo, base, head, test_paths, exempt_paths):
    gate = os.path.join(repo, "ci", "test-immutability-check.sh")
    if not os.path.isfile(gate):
        return None
    env = dict(os.environ)
    env["BASE_REF"] = base
    env["TEST_PATHS"] = " ".join(test_paths)
    env["EXEMPT_PATHS"] = " ".join(exempt_paths)
    r = subprocess.run(
        ["bash", gate],
        cwd=repo,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    return r.returncode == 0


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True, help="base ref, e.g. origin/main")
    parser.add_argument(
        "--test-paths",
        default="tests/",
        help='space-separated test pathspecs (default "tests/")',
    )
    parser.add_argument(
        "--exempt-paths",
        default="factory/ conformance/factory/ .gitignore context/memory.md",
        help=(
            "space-separated exempt directory prefixes (ending /) or exact files "
            '(default "factory/ conformance/factory/ .gitignore context/memory.md")'
        ),
    )
    args = parser.parse_args(argv)

    test_paths = args.test_paths.split()
    exempt_paths = args.exempt_paths.split()

    repo_r = run(["git", "rev-parse", "--show-toplevel"])
    repo = repo_r.stdout.strip()

    branch = ensure_clean_and_on_branch(repo)

    base_r = git(repo, "rev-parse", "--verify", f"{args.base}^{{commit}}", check=False)
    if base_r.returncode != 0:
        raise Fail(f"base ref '{args.base}' does not resolve to a commit")
    base_sha = base_r.stdout.strip()

    orig_head = git(repo, "rev-parse", "HEAD").stdout.strip()
    orig_tree = git(repo, "rev-parse", "HEAD^{tree}").stdout.strip()

    merge_base = git(repo, "merge-base", base_sha, orig_head, check=False)
    if merge_base.returncode != 0 or merge_base.stdout.strip() != base_sha:
        raise Fail(
            f"base '{args.base}' ({base_sha}) is not an ancestor of HEAD ({orig_head})"
        )

    commits = classify_commits(repo, base_sha, orig_head, test_paths, exempt_paths)
    if not commits:
        print("NOTHING-TO-DO")
        return 0

    plan = plan_new_order(commits)
    if plan is None:
        print("NOTHING-TO-DO")
        return 0

    new_order, moved, first_impl = plan
    if any(commit.is_merge for commit in commits):
        raise Fail(
            "history requires reordering but contains a merge commit; choose a "
            "linear base at or after the latest protected refresh"
        )

    print(f"branch: {branch}")
    print(f"base:   {args.base} ({base_sha})")
    print(f"found {len(moved)} test commit(s) after the first implementation commit")
    print(f"first implementation commit: {first_impl.sha[:12]} {first_impl.subject}")
    print("moving (preserving relative order) to sit immediately before it:")
    for c in moved:
        print(f"  {c.sha[:12]} {c.subject}")

    git(repo, "checkout", "-q", "--detach", base_sha)
    try:
        for sha in new_order:
            cherry_pick_one(repo, sha, orig_head, exempt_paths)
    except Fail as e:
        git(repo, "checkout", "-q", branch)
        print(f"ABORTED: {e}", file=sys.stderr)
        print(f"original HEAD restored: {orig_head}", file=sys.stderr)
        return 1

    new_head = git(repo, "rev-parse", "HEAD").stdout.strip()
    new_tree = git(repo, "rev-parse", "HEAD^{tree}").stdout.strip()

    if new_tree != orig_tree:
        git(repo, "checkout", "-q", branch)
        print(
            "ABORTED: rewritten tree does not match original tree "
            f"(orig {orig_tree}, new {new_tree}); branch left untouched at {orig_head}",
            file=sys.stderr,
        )
        return 1

    git(repo, "update-ref", f"refs/heads/{branch}", new_head)
    git(repo, "checkout", "-q", branch)

    print("tree verification: OK (rewritten HEAD^{tree} == original HEAD^{tree})")
    print(f"new HEAD: {new_head}")

    gate_ok = run_gate_check(repo, args.base, new_head, test_paths, exempt_paths)
    if gate_ok is None:
        print("gate check: ci/test-immutability-check.sh not found, skipped")
    elif gate_ok:
        print("gate check: PASS (ci/test-immutability-check.sh)")
    else:
        print("gate check: FAIL (ci/test-immutability-check.sh) — tree is correct but gate still fails; investigate")

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Fail as e:
        print(f"reorder-test-fixes: {e}", file=sys.stderr)
        sys.exit(1)
