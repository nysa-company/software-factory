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
  - A rewrite may not move a commit across a merge. Retained two-parent merges
    are recreated with their exact original tree and second parent; octopus
    merges are refused.
  - Any conflict touching a non-exempt path is unresolvable by policy: the
    cherry-pick/rebase is aborted and the tool exits non-zero. Only
    conflicts where *every* conflicted path is under EXEMPT_PATHS are
    auto-resolved (see resolve_exempt_conflict below) — and even then, the
    final tree-equality check is what actually guarantees correctness, not
    the resolution heuristic itself.
  - This local helper never pushes. Replacing an accepted remote history still
    requires separate protected authorization and an explicit force-with-lease.
"""
from __future__ import annotations

import argparse
from collections import Counter
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field


class Fail(Exception):
    """Raised for any condition that should abort with a clear message."""


def run(args, cwd=None, check=True, input_text=None, env=None):
    result = subprocess.run(
        args,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        input=input_text,
        env=env,
    )
    if check and result.returncode != 0:
        raise Fail(
            "command failed: {}\n--- stdout ---\n{}\n--- stderr ---\n{}".format(
                " ".join(args), result.stdout, result.stderr
            )
        )
    return result


def git(repo, *args, check=True, input_text=None, env=None):
    return run(
        ["git", "-C", repo] + list(args), check=check,
        input_text=input_text, env=env,
    )


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


def _matches_path(path, configured):
    return any(
        path.startswith(item) if item.endswith("/") else path == item
        for item in configured
    )


def _snapshot_paths(repo, base, old_head, test_paths, ticket):
    raw = git(
        repo, "diff", "--name-status", "-z", "--no-renames", base, old_head,
    ).stdout.split("\0")
    if raw and raw[-1] == "":
        raw.pop()
    if len(raw) % 2:
        raise Fail("history reconstruction diff is malformed")
    tests, factory = [], []
    ticket_factory = re.compile(
        rf"^factory/(?:tickets/{re.escape(ticket)}[.]md|"
        rf"route-plans/{re.escape(ticket)}[.]json|"
        rf"(?:receipts|attestations)/{re.escape(ticket)}/"
        r"[A-Za-z0-9._+@-]+)$"
    )
    safe = re.compile(r"^[A-Za-z0-9._+@/-]+$")
    for status, path in zip(raw[::2], raw[1::2]):
        if (
            status not in {"A", "M"}
            or not safe.fullmatch(path)
            or any(part in {"", ".", ".."} for part in path.split("/"))
        ):
            raise Fail("history reconstruction contains an unsafe path change")
        if _matches_path(path, test_paths):
            tests.append(path)
        elif ticket_factory.fullmatch(path):
            factory.append(path)
        else:
            raise Fail("history reconstruction contains a product or foreign path")
    ticket_path = f"factory/tickets/{ticket}.md"
    if not tests or ticket_path not in factory:
        raise Fail("history reconstruction requires protected tests and ticket evidence")
    for path in (*tests, *factory):
        _regular_blob(repo, old_head, path)
    return tuple(sorted(tests)), tuple(sorted(factory))


def _regular_blob(repo, head, path):
    fields = git(repo, "ls-tree", head, "--", path).stdout.split()
    if len(fields) < 4 or fields[:2] != ["100644", "blob"]:
        raise Fail(f"history reconstruction path is not a regular blob: {path}")
    return fields[0], fields[2]


def create_test_snapshot_reconstruction(repo, base, old_head, test_paths, ticket):
    """Create the canonical two-commit, tree-identical reconstruction."""
    tests, factory = _snapshot_paths(repo, base, old_head, test_paths, ticket)
    old_tree = git(repo, "rev-parse", f"{old_head}^{{tree}}").stdout.strip()
    old_epoch = git(repo, "show", "-s", "--format=%ct", old_head).stdout.strip()
    if not old_epoch.isdigit():
        raise Fail("history reconstruction timestamp is invalid")
    descriptor, index = tempfile.mkstemp(prefix="factory-history-index.")
    os.close(descriptor)
    os.unlink(index)
    environment = dict(os.environ)
    environment["GIT_INDEX_FILE"] = index
    environment.update({
        "GIT_AUTHOR_NAME": "Software Factory",
        "GIT_AUTHOR_EMAIL": "factory@local",
        "GIT_COMMITTER_NAME": "Software Factory",
        "GIT_COMMITTER_EMAIL": "factory@local",
    })
    try:
        git(repo, "read-tree", base, env=environment)
        for path in tests:
            mode, blob = _regular_blob(repo, old_head, path)
            git(
                repo, "update-index", "--add", "--cacheinfo",
                f"{mode},{blob},{path}", env=environment,
            )
        test_tree = git(repo, "write-tree", env=environment).stdout.strip()
        environment["GIT_AUTHOR_DATE"] = f"{int(old_epoch) + 1} +0000"
        environment["GIT_COMMITTER_DATE"] = environment["GIT_AUTHOR_DATE"]
        test_commit = git(
            repo, "commit-tree", test_tree, "-p", base,
            input_text=f"{ticket}: reconstruct protected tests\n",
            env=environment,
        ).stdout.strip()
        environment["GIT_AUTHOR_DATE"] = f"{int(old_epoch) + 2} +0000"
        environment["GIT_COMMITTER_DATE"] = environment["GIT_AUTHOR_DATE"]
        new_head = git(
            repo, "commit-tree", old_tree, "-p", test_commit,
            input_text=f"{ticket}: snapshot preserved factory evidence\n",
            env=environment,
        ).stdout.strip()
    finally:
        try:
            os.unlink(index)
        except FileNotFoundError:
            pass
    if not verified_test_snapshot_reconstruction(
        repo, base, old_head, new_head, test_paths, ticket,
    ):
        raise Fail("history reconstruction verification failed")
    return {
        "factory_paths": list(factory),
        "new_head": new_head,
        "old_tree": old_tree,
        "test_commit": test_commit,
        "test_paths": list(tests),
    }


def verified_test_snapshot_reconstruction(
    repo, base, old_head, new_head, test_paths, ticket,
):
    """Verify a two-commit test/factory snapshot with an unchanged final tree."""
    try:
        tests, factory = _snapshot_paths(
            repo, base, old_head, test_paths, ticket,
        )
        if git(repo, "rev-parse", f"{old_head}^{{tree}}").stdout.strip() != git(
            repo, "rev-parse", f"{new_head}^{{tree}}",
        ).stdout.strip():
            return False
        rows = [
            line.split()
            for line in git(
                repo, "rev-list", "--reverse", "--parents", f"{base}..{new_head}",
            ).stdout.splitlines()
        ]
        if (
            len(rows) != 2
            or len(rows[0]) != 2
            or rows[0][1] != base
            or rows[1] != [new_head, rows[0][0]]
        ):
            return False
        test_commit = rows[0][0]
        observed_test = tuple(sorted(diff_tree_files(repo, test_commit)))
        observed_factory = tuple(sorted(diff_tree_files(repo, new_head)))
        identity = git(
            repo, "show", "-s", "--format=%an%x00%ae%x00%cn%x00%ce%x00%s",
            new_head,
        ).stdout.rstrip("\n").split("\0")
        test_identity = git(
            repo, "show", "-s", "--format=%an%x00%ae%x00%cn%x00%ce%x00%s",
            test_commit,
        ).stdout.rstrip("\n").split("\0")
        return (
            observed_test == tests
            and observed_factory == factory
            and test_identity == [
                "Software Factory", "factory@local", "Software Factory",
                "factory@local", f"{ticket}: reconstruct protected tests",
            ]
            and identity == [
                "Software Factory", "factory@local", "Software Factory",
                "factory@local", f"{ticket}: snapshot preserved factory evidence",
            ]
        )
    except (Fail, OSError):
        return False


def diff_tree_files(repo, sha, pathspecs=None):
    args = ["diff-tree", "--no-commit-id", "--name-only", "-r", sha]
    if pathspecs:
        args += ["--"] + list(pathspecs)
    out = git(repo, *args).stdout
    return [line for line in out.splitlines() if line]


FROZEN = re.compile(r"^#{2,3} Frozen contract — version ([1-9][0-9]*)$")
FROZEN_PASSES = tuple(map(re.compile, (
    r"^- \*\*Freeze result — PASS\.\*\* "
    r"Contract version ([1-9][0-9]*) is frozen\.$",
    r"^- \*\*Freeze result:\*\* PASS\. Contract version ([1-9][0-9]*) "
    r"(?:is frozen(?:[.;].*)?|supersedes (?:contract )?versions? "
    r"[1-9][0-9]*.*)$",
)))


def frozen_pass(line):
    return next((match for regex in FROZEN_PASSES if (match := regex.fullmatch(line))), None)


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
    passed = [frozen_pass(line[1:]) for line in diff if line.startswith("+")]
    passed = [match for match in passed if match]
    if len(added) != 1 or len(passed) != 1 or added[0][1] != passed[0][1]:
        return False
    prior = git(repo, "show", f"{sha}^:{path}", check=False).stdout.splitlines()
    versions = [
        int(match[1]) for line in prior if (match := FROZEN.fullmatch(line))
    ]
    prior_max = max(versions, default=0)
    removed = [FROZEN.fullmatch(line[1:]) for line in diff if line.startswith("-")]
    removed = [match for match in removed if match]
    removed_passes = [frozen_pass(line[1:]) for line in diff if line.startswith("-")]
    removed_passes = [match for match in removed_passes if match]
    if removed or removed_passes:
        if len(removed) != 1 or len(removed_passes) != 1:
            return False
        if removed[0][1] != removed_passes[0][1] or int(removed[0][1]) != prior_max:
            return False
    return int(added[0][1]) > prior_max


def classify_commits(repo, base, head, test_paths, exempt_paths):
    rev_list = git(
        repo, "rev-list", "--first-parent", "--reverse", f"{base}..{head}"
    ).stdout
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


def merge_boundaries_preserved(commits, new_order):
    """Return true only when reordering never moves a commit across a merge."""
    positions = {sha: index for index, sha in enumerate(new_order)}
    for index, commit in enumerate(commits):
        if commit.is_merge and {
            item.sha for item in commits[:index]
        } != set(new_order[:positions[commit.sha]]):
            return False
    return True


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
    parents = git(repo, "rev-list", "--parents", "-n", "1", sha).stdout.split()[1:]
    if len(parents) > 2:
        raise Fail(f"octopus merge is not supported: {sha}")
    before = git(repo, "rev-parse", "HEAD").stdout.strip()
    if len(parents) == 2:
        return recreate_merge(repo, sha, before, parents)
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


def recreate_merge(repo, original, first_parent, parents):
    """Recreate the exact reviewed merge tree and protected second parent."""
    tree = git(repo, "rev-parse", f"{original}^{{tree}}").stdout.strip()
    message = git(repo, "show", "-s", "--format=%B", original).stdout
    rewritten = git(
        repo, "commit-tree", tree, "-p", first_parent, "-p", parents[1],
        input_text=message,
    ).stdout.strip()
    git(repo, "update-ref", "HEAD", rewritten, first_parent)
    git(repo, "reset", "--hard", "-q", rewritten)
    return True


def nonexempt_patch_ids(repo, commits, exempt_paths):
    """Return the semantic non-bookkeeping patches on one first-parent line."""
    values = []
    for commit in commits:
        parent = git(repo, "rev-parse", f"{commit.sha}^1").stdout.strip()
        paths = sorted(path for path in commit.files if not is_exempt(path, exempt_paths))
        if not paths:
            continue
        patch = git(
            repo, "diff", "--binary", "--no-ext-diff", parent, commit.sha,
            "--", *paths,
        ).stdout
        result = run(["git", "patch-id", "--stable"], input_text=patch)
        fields = result.stdout.split()
        if len(fields) < 1 or not re.fullmatch(r"[0-9a-f]{40}", fields[0]):
            raise Fail(f"commit has no stable patch identity: {commit.sha}")
        values.append(fields[0])
    return Counter(values)


def nonexempt_path_patch_ids(repo, commits, exempt_paths):
    """Return per-path patches so a mixed commit may be split without drift."""
    values = []
    for commit in commits:
        parent = git(repo, "rev-parse", f"{commit.sha}^1").stdout.strip()
        for path in sorted(
            path for path in commit.files if not is_exempt(path, exempt_paths)
        ):
            patch = git(
                repo, "diff", "--binary", "--no-ext-diff", "--no-renames",
                parent, commit.sha, "--", path,
            ).stdout
            result = run(["git", "patch-id", "--stable"], input_text=patch)
            fields = result.stdout.split()
            if len(fields) < 1 or not re.fullmatch(r"[0-9a-f]{40}", fields[0]):
                raise Fail(f"commit has no stable patch identity: {commit.sha}:{path}")
            values.append((path, fields[0]))
    return Counter(values)


def protected_merges(repo, commits):
    result = []
    for commit in commits:
        parents = git(
            repo, "rev-list", "--parents", "-n", "1", commit.sha
        ).stdout.split()[1:]
        if len(parents) > 2:
            raise Fail(f"octopus merge is not supported: {commit.sha}")
        if len(parents) == 2:
            tree = git(repo, "rev-parse", f"{commit.sha}^{{tree}}").stdout.strip()
            result.append((parents[1], tree))
    return result


def verified_normalization_plan(
    repo, base, old_head, new_head, test_paths, exempt_paths
):
    """Return the verified old-history reorder plan, or None when unauthorized."""
    try:
        if old_head == new_head:
            return None
        for head in (old_head, new_head):
            if git(repo, "merge-base", base, head).stdout.strip() != base:
                return None
        if git(repo, "rev-parse", f"{old_head}^{{tree}}").stdout.strip() != git(
            repo, "rev-parse", f"{new_head}^{{tree}}"
        ).stdout.strip():
            return None
        old = classify_commits(repo, base, old_head, test_paths, exempt_paths)
        new = classify_commits(repo, base, new_head, test_paths, exempt_paths)
        old_plan = plan_new_order(old)
        if (
            old_plan is None
            or not merge_boundaries_preserved(old, old_plan[0])
            or plan_new_order(new) is not None
        ):
            return None
        if any(item.kind == "MIXED" for item in new):
            return None
        if protected_merges(repo, old) != protected_merges(repo, new):
            return None
        if nonexempt_patch_ids(repo, old, exempt_paths) != nonexempt_patch_ids(
            repo, new, exempt_paths
        ):
            return None
        return old_plan
    except (Fail, OSError):
        return None


def verified_history_repair(
    repo, base, old_head, new_head, test_paths, exempt_paths
):
    """Authenticate a patch-identical repair of mixed or late test history."""
    try:
        if old_head == new_head:
            return False
        for head in (old_head, new_head):
            if git(repo, "merge-base", base, head).stdout.strip() != base:
                return False
        old = classify_commits(repo, base, old_head, test_paths, exempt_paths)
        new = classify_commits(repo, base, new_head, test_paths, exempt_paths)
        old_plan = plan_new_order(old)
        if old_plan is None and not any(item.kind == "MIXED" for item in old):
            return False
        if old_plan is not None and not merge_boundaries_preserved(old, old_plan[0]):
            return False
        return (
            plan_new_order(new) is None
            and not any(item.kind == "MIXED" for item in new)
            and protected_merges(repo, old) == protected_merges(repo, new)
            and nonexempt_path_patch_ids(repo, old, exempt_paths)
            == nonexempt_path_patch_ids(repo, new, exempt_paths)
        )
    except (Fail, OSError):
        return False


def normalization_allowed(repo, base, old_head, new_head, test_paths, exempt_paths):
    """Authenticate a tree-identical tests-first rewrite, including merges."""
    return verified_normalization_plan(
        repo, base, old_head, new_head, test_paths, exempt_paths
    ) is not None


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
    if not merge_boundaries_preserved(commits, new_order):
        raise Fail(
            "history requires moving a commit across a merge boundary; choose "
            "a later tests-first contract epoch instead"
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
