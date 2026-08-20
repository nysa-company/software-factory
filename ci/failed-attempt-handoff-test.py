#!/usr/bin/env python3
"""Focused security regressions for failed-attempt snapshot handoffs."""

import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

from failed_attempt_handoff import (  # noqa: E402
    GitHubHTTPSCredential,
    HandoffError,
    RoleBoundaryPolicy,
    build_handoff_commit,
    preview_handoff,
    revalidate_handoff,
    validate_handoff_commit,
)
import failed_attempt_handoff as HANDOFF  # noqa: E402


def git(repo, *args, input_text=None):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        input=input_text,
        text=True,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ).stdout.strip()


class HandoffTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.repo = self.base / "product"
        self.remote = self.base / "product.git"
        subprocess.run(["git", "init", "-q", "-b", "main", self.repo], check=True)
        subprocess.run(["git", "init", "--bare", "-q", self.remote], check=True)
        git(self.repo, "config", "user.name", "Human Builder")
        git(self.repo, "config", "user.email", "human@example.test")
        git(self.repo, "remote", "add", "origin", str(self.remote))
        (self.repo / "src").mkdir()
        (self.repo / "factory").mkdir()
        (self.repo / "src/kept.txt").write_text("original\n")
        (self.repo / "src/deleted.txt").write_text("delete me\n")
        (self.repo / "factory/model-route-journal.json").write_text("{}\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "baseline")
        git(self.repo, "push", "-q", "-u", "origin", "main")
        git(self.remote, "symbolic-ref", "HEAD", "refs/heads/main")
        self.head = git(self.repo, "rev-parse", "HEAD")
        self.policy = self.make_policy()

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def make_policy(**overrides):
        value = {
            "schema": "nysa.software-factory.handoff-boundary/v1",
            "roles": {"builder": ["src/**", ".gitattributes"]},
            "protected_paths": [
                ".git",
                ".git/**",
                "factory/tickets/**",
                "factory/attestations/**",
            ],
            "journal_path": "factory/model-route-journal.json",
            "max_file_bytes": 128,
            "provider_identities": ["provider@example.test"],
        }
        value.update(overrides)
        return RoleBoundaryPolicy.from_dict(value)

    def preview(self, **overrides):
        values = {
            "role": "builder",
            "policy": self.policy,
            "expected_head": self.head,
            "expected_branch": "main",
            "remote": "origin",
            "remote_branch": "main",
            "expected_remote_head": self.head,
            "provider_scan_base": self.head,
        }
        values.update(overrides)
        return preview_handoff(self.repo, **values)

    def test_snapshot_is_canonical_and_covers_add_delete_content_and_mode(self):
        (self.repo / "src/deleted.txt").unlink()
        (self.repo / "src/kept.txt").write_text("changed\n")
        os.chmod(self.repo / "src/kept.txt", 0o755)
        (self.repo / "src/new file.txt").write_text("new\n")
        first = self.preview()
        self.assertEqual(
            [(item.path, item.state, item.mode) for item in first.entries],
            [
                ("src/deleted.txt", "deleted", None),
                ("src/kept.txt", "file", "100755"),
                ("src/new file.txt", "file", "100644"),
            ],
        )
        self.assertEqual(len(first.snapshot_digest), 64)

        # Index state is separately bound; staging cannot alter the worktree snapshot.
        git(self.repo, "add", "-A")
        second = self.preview()
        self.assertEqual(second.snapshot_digest, first.snapshot_digest)
        self.assertNotEqual(second.index_digest, first.index_digest)
        self.assertNotEqual(second.preview_digest, first.preview_digest)

        # The journal is excluded completely, so its content cannot self-reference.
        (self.repo / "factory/model-route-journal.json").write_text(
            '{"snapshot":"' + first.snapshot_digest + '"}\n'
        )
        third = self.preview()
        self.assertEqual(third.snapshot_digest, first.snapshot_digest)
        self.assertFalse(
            any(item.path == "factory/model-route-journal.json" for item in third.entries)
        )

    def test_github_credential_is_scoped_to_the_exact_network_git_call(self):
        helper = self.base / "gh"
        helper.write_text(
            "#!/bin/sh\n"
            "test -z \"${GH_TOKEN:-}${GITHUB_TOKEN:-}${GH_ENTERPRISE_TOKEN:-}\" || exit 3\n"
            "test -z \"${GITHUB_ENTERPRISE_TOKEN:-}${GH_HOST:-}\" || exit 4\n"
            "test \"$GH_CONFIG_DIR\" = \"$HOME/.config/gh\" || exit 5\n"
            "test \"$1 $2 $3\" = 'auth git-credential get' || exit 0\n"
            "printf 'username=test\\npassword=fixture\\n'\n"
        )
        helper.chmod(0o700)
        helper = helper.resolve()
        credential = GitHubHTTPSCredential(str(helper))
        completed = subprocess.CompletedProcess([], 0, b"ok", b"")
        ambient = {
            "GH_CONFIG_DIR": "/tmp/untrusted-config",
            "GH_ENTERPRISE_TOKEN": "ambient-enterprise",
            "GH_HOST": "untrusted.example",
            "GH_TOKEN": "ambient-token",
            "GITHUB_ENTERPRISE_TOKEN": "ambient-github-enterprise",
            "GITHUB_TOKEN": "ambient-github-token",
        }
        with mock.patch.dict(os.environ, ambient):
            with mock.patch.object(
                HANDOFF.subprocess, "run", return_value=completed
            ) as run:
                HANDOFF._git(self.repo, ["ls-remote"], git_auth=credential)
        command = run.call_args.args[0]
        environment = run.call_args.kwargs["env"]
        for name in ambient:
            if name != "GH_CONFIG_DIR":
                self.assertNotIn(name, environment)
        self.assertEqual(
            environment["GH_CONFIG_DIR"], str(Path(environment["HOME"]) / ".config/gh")
        )
        self.assertEqual(environment["GH_PROMPT_DISABLED"], "1")
        self.assertIn("credential.helper=", command)
        self.assertIn(
            f"credential.https://github.com.helper=!{helper} auth git-credential",
            command,
        )
        output = HANDOFF._git(
            self.repo,
            ["credential", "fill"],
            input_bytes=b"protocol=https\nhost=github.com\n\n",
            git_auth=credential,
        )
        self.assertIn(b"password=fixture", output)

        with mock.patch.dict(os.environ, ambient):
            with mock.patch.object(
                HANDOFF.subprocess, "run", return_value=completed
            ) as run:
                HANDOFF._git(self.repo, ["status"])
        for name in ambient:
            self.assertNotIn(name, run.call_args.kwargs["env"])

    def test_revalidate_detects_worktree_index_head_branch_and_remote_drift(self):
        (self.repo / "src/kept.txt").write_text("changed\n")
        preview = self.preview()
        (self.repo / "src/kept.txt").write_text("changed again\n")
        with self.assertRaisesRegex(HandoffError, "snapshot drifted"):
            revalidate_handoff(preview, self.policy)

        (self.repo / "src/kept.txt").write_text("changed\n")
        preview = self.preview()
        git(self.repo, "add", "src/kept.txt")
        with self.assertRaisesRegex(HandoffError, "snapshot drifted"):
            revalidate_handoff(preview, self.policy)

        git(self.repo, "reset", "-q", "HEAD", "--", "src/kept.txt")
        git(self.repo, "switch", "-q", "-c", "other")
        with self.assertRaisesRegex(HandoffError, "branch drifted"):
            revalidate_handoff(preview, self.policy)
        git(self.repo, "switch", "-q", "main")

        git(self.repo, "add", "src/kept.txt")
        git(self.repo, "commit", "-qm", "local drift")
        with self.assertRaisesRegex(HandoffError, "HEAD drifted"):
            revalidate_handoff(preview, self.policy)

        # A distinct clone advances the certified remote while local HEAD is restored.
        git(self.repo, "reset", "--hard", "-q", self.head)
        clone = self.base / "other"
        subprocess.run(["git", "clone", "-q", str(self.remote), clone], check=True)
        git(clone, "config", "user.name", "Remote Human")
        git(clone, "config", "user.email", "remote@example.test")
        (clone / "remote.txt").write_text("advance\n")
        git(clone, "add", ".")
        git(clone, "commit", "-qm", "advance")
        git(clone, "push", "-q", "origin", "main")
        with self.assertRaisesRegex(HandoffError, "remote branch drifted"):
            revalidate_handoff(preview, self.policy)

    def test_rejects_provider_authored_commits_since_scan_base(self):
        git(self.repo, "config", "user.name", "Provider Bot")
        git(self.repo, "config", "user.email", "provider@example.test")
        (self.repo / "src/provider.txt").write_text("provider\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "provider mutation")
        current = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "push", "-q", "origin", "main")
        with self.assertRaisesRegex(HandoffError, "provider-authored"):
            self.preview(
                expected_head=current,
                expected_remote_head=current,
                provider_scan_base=self.head,
            )

    def test_committed_role_work_obeys_the_same_boundary_policy(self):
        (self.repo / "src/kept.txt").write_text("allowed commit\n")
        git(self.repo, "add", "src/kept.txt")
        git(self.repo, "commit", "-qm", "allowed role progress")
        allowed_head = git(self.repo, "rev-parse", "HEAD")
        self.preview(expected_head=allowed_head)

        (self.repo / "outside.txt").write_text("forbidden commit\n")
        git(self.repo, "add", "outside.txt")
        git(self.repo, "commit", "-qm", "out-of-bound role progress")
        forbidden_head = git(self.repo, "rev-parse", "HEAD")
        with self.assertRaisesRegex(HandoffError, "outside"):
            self.preview(expected_head=forbidden_head)

    def test_committed_narrator_handoff_accepts_only_current_ticket_png_evidence(self):
        value = json.loads(
            (ROOT / "scripts/model-routing/handoff-boundaries-v1.json").read_text()
            .replace("TICKET", "T-1")
        )
        policy = RoleBoundaryPolicy.from_dict(value)
        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
            "+A8AAQUBAScY42YAAAAASUVORK5CYII="
        )

        def committed(relative, content, mode=0o644):
            path = self.repo / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            path.chmod(mode)
            git(self.repo, "add", relative)
            git(self.repo, "commit", "-qm", "record narrator evidence")
            return git(self.repo, "rev-parse", "HEAD")

        bundle = "factory/tickets/T-1-bundle.md"
        screenshot = "factory/tickets/T-1-evidence/preview.png"
        (self.repo / bundle).parent.mkdir(parents=True, exist_ok=True)
        (self.repo / bundle).write_text("![Preview](T-1-evidence/preview.png)\n")
        head = committed(screenshot, png)
        self.preview(
            role="narrator", policy=policy, expected_head=head,
            provider_scan_base=self.head,
        )

        cases = (
            ("factory/tickets/T-2-evidence/preview.png", png, 0o644, "outside"),
            ("factory/tickets/T-1-evidence/nested/preview.png", png, 0o644, "outside"),
            ("factory/tickets/T-1-evidence/preview.png", b"not a png\n", 0o644, "PNG"),
            ("factory/tickets/T-1-evidence/preview.png", png, 0o755, "PNG"),
        )
        for relative, content, mode, message in cases:
            with self.subTest(relative=relative):
                git(self.repo, "reset", "--hard", "-q", self.head)
                head = committed(relative, content, mode)
                with self.assertRaisesRegex(HandoffError, message):
                    self.preview(
                        role="narrator", policy=policy, expected_head=head,
                        provider_scan_base=self.head,
                    )

    def test_committed_role_validation_allows_only_unchanged_symlinks(self):
        link = self.repo / "src/baseline-link"
        link.symlink_to("kept.txt")
        git(self.repo, "add", "src/baseline-link")
        git(self.repo, "commit", "-qm", "add protected baseline symlink")
        baseline = git(self.repo, "rev-parse", "HEAD")

        (self.repo / "src/kept.txt").write_text("allowed commit\n")
        git(self.repo, "add", "src/kept.txt")
        git(self.repo, "commit", "-qm", "allowed role progress")
        head = git(self.repo, "rev-parse", "HEAD")
        HANDOFF._validate_committed_changes(
            self.repo, baseline, head, "builder", self.policy,
        )

        link.unlink()
        link.symlink_to("deleted.txt")
        git(self.repo, "add", "src/baseline-link")
        git(self.repo, "commit", "-qm", "change protected baseline symlink")
        with self.assertRaisesRegex(HandoffError, "unsafe mode"):
            HANDOFF._validate_committed_changes(
                self.repo, baseline, git(self.repo, "rev-parse", "HEAD"),
                "builder", self.policy,
            )

    def test_spec_lint_fallback_preserves_one_legal_verdict_append(self):
        ticket = self.repo / "factory/tickets/T-366.md"
        ticket.parent.mkdir()
        ticket.write_text("State: In Progress\n")
        git(self.repo, "add", str(ticket.relative_to(self.repo)))
        git(self.repo, "commit", "-qm", "seed qualification ticket")
        git(self.repo, "push", "-q", "origin", "main")
        baseline = git(self.repo, "rev-parse", "HEAD")
        policy = self.make_policy(
            roles={"spec-linter": ["factory/tickets/T-366.md"]},
            protected_paths=[".git", ".git/**"],
        )

        ticket.write_text("State: In Progress\nSPEC-LINT: FAIL — missing proof\n")
        git(self.repo, "add", str(ticket.relative_to(self.repo)))
        git(self.repo, "commit", "-qm", "T-366: spec-lint round 1 verdict FAIL")
        head = git(self.repo, "rev-parse", "HEAD")
        preview = preview_handoff(
            self.repo,
            role="spec-linter",
            policy=policy,
            expected_head=head,
            expected_branch="main",
            remote="origin",
            remote_branch="main",
            expected_remote_head=baseline,
            provider_scan_base=baseline,
        )
        result = build_handoff_commit(
            preview,
            policy,
            revision_hash="a" * 64,
            commit_timestamp="1784390400 +0000",
            journal_content=b'{"schema":"ticket-model-route-journal/v2"}\n',
        )
        validate_handoff_commit(
            self.repo,
            commit=result.commit,
            role="spec-linter",
            provider_scan_base=baseline,
            policy=policy,
            expected_snapshot_digest=preview.snapshot_digest,
            expected_revision_hash="a" * 64,
            expected_subject="Preserve failed attempt for trusted handoff",
        )

        ticket.write_text("State: Done\nSPEC-LINT: FAIL — missing proof\n")
        git(self.repo, "add", str(ticket.relative_to(self.repo)))
        git(self.repo, "commit", "-qm", "mutate protected ticket state")
        with self.assertRaisesRegex(HandoffError, "protected ticket evidence changed"):
            preview_handoff(
                self.repo,
                role="spec-linter",
                policy=policy,
                expected_head=git(self.repo, "rev-parse", "HEAD"),
                expected_branch="main",
                remote="origin",
                remote_branch="main",
                expected_remote_head=baseline,
                provider_scan_base=baseline,
            )

    def test_preview_and_replay_preserve_only_exact_tracked_symlinks(self):
        (self.repo / "skills").mkdir()
        (self.repo / "skills/README.md").write_text("# Skills\n")
        (self.repo / ".claude").mkdir()
        link = self.repo / ".claude/skills"
        link.symlink_to("../skills")
        git(self.repo, "add", "skills", ".claude/skills")
        git(self.repo, "commit", "-qm", "add repository skill bridge")
        git(self.repo, "push", "-q", "origin", "main")
        self.head = git(self.repo, "rev-parse", "HEAD")

        (self.repo / "src/kept.txt").write_text("handoff\n")
        preview = self.preview()
        self.assertEqual([entry.path for entry in preview.entries], ["src/kept.txt"])
        result = build_handoff_commit(
            preview,
            self.policy,
            revision_hash="a" * 64,
            commit_timestamp="1784390400 +0000",
            journal_content=b'{"schema":"ticket-model-route-journal/v2"}\n',
        )
        validate_handoff_commit(
            self.repo,
            commit=result.commit,
            role="builder",
            provider_scan_base=self.head,
            policy=self.policy,
            expected_snapshot_digest=preview.snapshot_digest,
            expected_revision_hash="a" * 64,
            expected_subject="Preserve failed attempt for trusted handoff",
        )
        self.assertEqual(
            git(self.repo, "show", f"{result.commit}:.claude/skills"), "../skills"
        )

        for target, staged in (("../other", False), ("../other", True)):
            with self.subTest(target=target, staged=staged):
                link.unlink()
                link.symlink_to(target)
                if staged:
                    git(self.repo, "add", ".claude/skills")
                with self.assertRaisesRegex(HandoffError, "symlink"):
                    self.preview()
                link.unlink()
                link.symlink_to("../skills")
                git(self.repo, "add", ".claude/skills")

        link.unlink()
        with self.assertRaisesRegex(HandoffError, "symlink"):
            self.preview()

        link.write_text("not a symlink\n")
        with self.assertRaisesRegex(HandoffError, "symlink"):
            self.preview()
        link.unlink()

        link.symlink_to("../skills")
        git(self.repo, "add", ".claude/skills")
        journal = self.repo / "factory/model-route-journal.json"
        journal.unlink()
        journal.symlink_to("../src/kept.txt")
        git(self.repo, "add", "factory/model-route-journal.json")
        git(self.repo, "commit", "-qm", "replace route journal with symlink")
        git(self.repo, "push", "-q", "origin", "main")
        self.head = git(self.repo, "rev-parse", "HEAD")
        with self.assertRaisesRegex(HandoffError, "symlink"):
            self.preview()

    def test_replay_refuses_deleting_a_tracked_symlink(self):
        (self.repo / "skills").mkdir()
        (self.repo / "skills/README.md").write_text("# Skills\n")
        (self.repo / ".claude").mkdir()
        link = self.repo / ".claude/skills"
        link.symlink_to("../skills")
        git(self.repo, "add", "skills", ".claude/skills")
        git(self.repo, "commit", "-qm", "add repository skill bridge")
        parent = git(self.repo, "rev-parse", "HEAD")

        digest = HANDOFF._snapshot_digest(
            (HANDOFF.SnapshotEntry(path=".claude/skills", state="deleted"),)
        )
        link.unlink()
        (self.repo / "factory/model-route-journal.json").write_text(
            '{"schema":"ticket-model-route-journal/v2"}\n'
        )
        git(self.repo, "add", "-A")
        git(
            self.repo,
            "commit",
            "-qm",
            "Preserve failed attempt for trusted handoff\n\n"
            f"Failed-Attempt-Snapshot: {digest}\n"
            f"Model-Route-Revision: {'a' * 64}",
        )
        commit = git(self.repo, "rev-parse", "HEAD")
        with self.assertRaisesRegex(HandoffError, "unsafe mode"):
            validate_handoff_commit(
                self.repo,
                commit=commit,
                role="builder",
                provider_scan_base=parent,
                policy=self.policy,
                expected_snapshot_digest=digest,
                expected_revision_hash="a" * 64,
                expected_subject="Preserve failed attempt for trusted handoff",
            )

    def test_rejects_path_boundary_protected_binary_large_and_secret_content(self):
        cases = (
            ("outside.txt", b"text\n", self.policy, "outside"),
            (
                "factory/tickets/T-1.md",
                b"State: Ready\n",
                self.make_policy(roles={"builder": ["**"]}),
                "protected",
            ),
            ("src/binary.dat", b"one\0two", self.policy, "binary"),
            ("src/large.txt", b"x" * 129, self.policy, "oversized"),
            (
                "src/secret.txt",
                b"-----BEGIN PRIVATE KEY-----\n",
                self.policy,
                "secret-like",
            ),
        )
        for relative, content, policy, message in cases:
            with self.subTest(relative=relative):
                path = self.repo / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
                with self.assertRaisesRegex(HandoffError, message):
                    self.preview(policy=policy)
                path.unlink()
        unsafe_name = self.repo / "src/line\nbreak.txt"
        unsafe_name.write_text("content\n")
        with self.assertRaisesRegex(HandoffError, "unsafe repository path"):
            self.preview()

    def test_rejects_symlink_hardlink_fifo_nested_repo_and_submodule(self):
        target = self.repo / "src/unsafe"
        target.symlink_to("kept.txt")
        with self.assertRaisesRegex(HandoffError, "unsafe|non-regular"):
            self.preview()
        target.unlink()

        os.link(self.repo / "src/kept.txt", target)
        with self.assertRaisesRegex(HandoffError, "hardlinked"):
            self.preview()
        target.unlink()

        os.mkfifo(target)
        with self.assertRaisesRegex(HandoffError, "non-regular"):
            self.preview()
        target.unlink()

        nested = self.repo / "vendor"
        subprocess.run(["git", "init", "-q", nested], check=True)
        with self.assertRaisesRegex(HandoffError, "nested repository"):
            self.preview()
        shutil.rmtree(nested)

        # A committed gitlink is rejected even if the worktree itself is absent.
        git(self.repo, "update-index", "--add", "--cacheinfo", "160000", self.head, "vendor")
        with self.assertRaisesRegex(HandoffError, "submodules"):
            self.preview()

    def test_ignored_dependency_symlink_is_outside_the_handoff_snapshot(self):
        (self.repo / ".gitignore").write_text("node_modules/\n")
        git(self.repo, "add", ".gitignore")
        git(self.repo, "commit", "-qm", "ignore dependencies")
        git(self.repo, "push", "-q", "origin", "main")
        self.head = git(self.repo, "rev-parse", "HEAD")
        dependencies = self.repo / "node_modules"
        dependencies.mkdir()
        (dependencies / "workspace-package").symlink_to("../src")
        (self.repo / "src/kept.txt").write_text("handoff\n")

        preview = self.preview()
        self.assertEqual([item.path for item in preview.entries], ["src/kept.txt"])

    def test_build_uses_raw_blobs_temporary_index_and_no_hooks_or_filters(self):
        sentinel = self.base / "unsafe-ran"
        hooks = self.base / "hooks"
        hooks.mkdir()
        hook = hooks / "pre-commit"
        hook.write_text(f"#!/bin/sh\ntouch '{sentinel}'\nexit 1\n")
        hook.chmod(0o755)
        git(self.repo, "config", "core.hooksPath", str(hooks))
        git(self.repo, "config", "core.fsmonitor", str(hook))
        git(self.repo, "config", "filter.evil.clean", f"touch '{sentinel}'; cat")
        git(self.repo, "config", "credential.helper", f"!touch '{sentinel}'")
        (self.repo / ".gitattributes").write_text("src/*.txt filter=evil\n")
        (self.repo / "src/kept.txt").write_text("raw handoff\n")
        (self.repo / "src/new.txt").write_text("new\n")
        (self.repo / "src/deleted.txt").unlink()
        index_before = (self.repo / ".git/index").read_bytes()
        preview = self.preview()
        with self.assertRaisesRegex(HandoffError, "canonical JSON"):
            build_handoff_commit(
                preview,
                self.policy,
                revision_hash="a" * 64,
                commit_timestamp="1784390400 +0000",
                journal_content=b'{"schema": "not-canonical"}\n',
            )
        result = build_handoff_commit(
            preview,
            self.policy,
            revision_hash="a" * 64,
            commit_timestamp="1784390400 +0000",
            journal_content=b'{"schema":"ticket-model-route-journal/v2"}\n',
        )
        self.assertEqual(result.parent, self.head)
        self.assertEqual(
            git(self.repo, "show", f"{result.commit}:src/kept.txt"), "raw handoff"
        )
        self.assertEqual(
            git(self.repo, "show", "-s", "--format=%P", result.commit), self.head
        )
        message = git(self.repo, "show", "-s", "--format=%B", result.commit)
        self.assertIn(f"Failed-Attempt-Snapshot: {preview.snapshot_digest}", message)
        self.assertIn("Model-Route-Revision: " + "a" * 64, message)
        self.assertIn(
            "ticket-model-route-journal/v2",
            git(self.repo, "show", f"{result.commit}:factory/model-route-journal.json"),
        )
        self.assertNotEqual(
            subprocess.run(
                ["git", "-C", str(self.repo), "cat-file", "-e", f"{result.commit}:src/deleted.txt"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            ).returncode,
            0,
        )
        self.assertEqual((self.repo / ".git/index").read_bytes(), index_before)
        self.assertFalse(sentinel.exists())
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), self.head)


if __name__ == "__main__":
    unittest.main()
