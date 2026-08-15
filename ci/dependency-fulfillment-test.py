#!/usr/bin/env python3
"""Focused dependency-only fulfillment regressions."""

from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/dependency-fulfillment.py"
SPEC = importlib.util.spec_from_file_location("dependency_fulfillment_cli", SCRIPT)
assert SPEC and SPEC.loader
CLI = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CLI)

from legacy_closeout import ValidationError, protected_dependency  # noqa: E402


OLD_KIT = "a" * 40
NEW_KIT = "b" * 40
CHECKS = (
    {"name": "ci", "app_id": 7, "app_slug": "github-actions"},
    {"name": "policy", "app_id": 7, "app_slug": "github-actions"},
)


def run(*args, cwd):
    result = subprocess.run(
        args, cwd=cwd, text=True, capture_output=True, check=False
    )
    if result.returncode:
        raise AssertionError(result.stderr)
    return result.stdout.strip()


class DependencyFulfillmentTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.repo = Path(self.temporary.name) / "product"
        self.repo.mkdir()
        run("git", "init", "-q", "-b", "main", cwd=self.repo)
        run("git", "config", "user.name", "Test", cwd=self.repo)
        run("git", "config", "user.email", "test@example.invalid", cwd=self.repo)
        (self.repo / "factory/tickets").mkdir(parents=True)
        (self.repo / "factory/PROJECT.env").write_text(
            "GH_REPO=acme/widget\nDONE_REQUIRED_CHECKS=ci,policy\n",
            encoding="utf-8",
        )
        (self.repo / "factory/KIT_PIN").write_text(OLD_KIT + "\n")
        (self.repo / "factory/tickets/T-300.md").write_text(
            "# T-300\n\nState: Backlog\n", encoding="utf-8"
        )
        (self.repo / "app.txt").write_text("before\n", encoding="utf-8")
        run("git", "add", ".", cwd=self.repo)
        run("git", "commit", "-qm", "seed", cwd=self.repo)
        run("git", "checkout", "-qb", "feature/t300", cwd=self.repo)
        (self.repo / "app.txt").write_text("after\n", encoding="utf-8")
        run("git", "add", "app.txt", cwd=self.repo)
        run("git", "commit", "-qm", "implement T-300", cwd=self.repo)
        self.pr_head = run("git", "rev-parse", "HEAD", cwd=self.repo)
        run("git", "checkout", "-q", "main", cwd=self.repo)
        run("git", "merge", "-q", "--no-ff", "feature/t300", "-m", "merge T-300", cwd=self.repo)
        self.merge_commit = run("git", "rev-parse", "HEAD", cwd=self.repo)
        self.basis = self.merge_commit
        self.tree = run("git", "rev-parse", "HEAD^{tree}", cwd=self.repo)
        run("git", "update-ref", "refs/remotes/origin/main", self.basis, cwd=self.repo)
        self.cutoff = (
            datetime.now(timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )
        self.request = Path(self.temporary.name) / "request.json"
        self.request.write_text(
            json.dumps(
                {
                    "schema": (
                        "nysa.software-factory."
                        "dependency-fulfillment-request/v1"
                    ),
                    "repository": "acme/widget",
                    "target_kit_sha": NEW_KIT,
                    "candidate_contract": "1.8.0",
                    "cutoff": self.cutoff,
                    "protected_main_basis": {
                        "commit": self.basis,
                        "tree": self.tree,
                    },
                    "required_checks": list(CHECKS),
                    "authorization": {
                        "method": "manual-protected-main-merge",
                        "operator": "operator@example.invalid",
                        "authorized_at": self.cutoff,
                        "statement": (
                            "Operator authorizes dependency-only adoption "
                            "without marking T-300 Done."
                        ),
                        "auto_merge": False,
                        "bypass": False,
                    },
                    "tickets": [{"ticket": "T-300", "pr_number": 7}],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

    def tearDown(self):
        self.temporary.cleanup()

    def refresh_basis(self):
        self.basis = run("git", "rev-parse", "HEAD", cwd=self.repo)
        self.tree = run("git", "rev-parse", "HEAD^{tree}", cwd=self.repo)
        run("git", "update-ref", "refs/remotes/origin/main", self.basis, cwd=self.repo)
        self.cutoff = (
            datetime.now(timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )
        request = json.loads(self.request.read_text(encoding="utf-8"))
        request["cutoff"] = self.cutoff
        request["protected_main_basis"] = {
            "commit": self.basis,
            "tree": self.tree,
        }
        request["authorization"]["authorized_at"] = self.cutoff
        self.request.write_text(
            json.dumps(request, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def gh(self, endpoint):
        if endpoint == "repos/acme/widget/pulls/7":
            return {
                "number": 7,
                "state": "closed",
                "merged": True,
                "base": {"ref": "main"},
                "head": {"ref": "feature/t300", "sha": self.pr_head},
                "merge_commit_sha": self.merge_commit,
                "merged_at": self.cutoff,
                "merged_by": {"login": "operator"},
            }
        if endpoint.endswith("/check-runs?per_page=100"):
            runs = [
                {
                    "name": item["name"],
                    "app": {
                        "id": item["app_id"],
                        "slug": item["app_slug"],
                    },
                    "status": "completed",
                    "conclusion": "success",
                }
                for item in CHECKS
            ]
            return {"total_count": len(runs), "check_runs": runs}
        if endpoint.endswith("/status?per_page=100"):
            return {"statuses": []}
        raise AssertionError(endpoint)

    def apply_and_commit(self, inflight_target=None):
        with mock.patch.object(CLI, "gh_json", side_effect=self.gh):
            plan = CLI.prepare(self.repo, self.request)
            previous = Path.cwd()
            try:
                import os

                os.chdir(self.repo)
                CLI.apply_plan(plan, plan["approval_sha256"])
            finally:
                os.chdir(previous)
        if inflight_target is not None:
            path = (
                self.repo
                / f"factory/migrations/inflight-release/{inflight_target}.json"
            )
            path.parent.mkdir(parents=True)
            path.write_text("{}\n", encoding="utf-8")
        run("git", "add", ".", cwd=self.repo)
        run("git", "commit", "-qm", "activate dependency fulfillment", cwd=self.repo)
        run(
            "git", "update-ref", "refs/remotes/origin/main",
            run("git", "rev-parse", "HEAD", cwd=self.repo),
            cwd=self.repo,
        )
        return plan

    def test_explicit_receipt_satisfies_dependency_without_marking_ticket_done(self):
        plan = self.apply_and_commit()
        result = protected_dependency(self.repo, "T-300")
        self.assertEqual(
            result["basis"], "validated-protected-dependency-fulfillment"
        )
        self.assertEqual(result["merge_commit"], self.basis)
        self.assertEqual(result["target_kit_sha"], NEW_KIT)
        self.assertRegex(plan["approval_sha256"], r"^[0-9a-f]{64}$")
        self.assertIn(
            "State: Backlog",
            (self.repo / "factory/tickets/T-300.md").read_text(encoding="utf-8"),
        )
        with self.assertRaisesRegex(
            ValidationError, "lacks dependency fulfillment evidence"
        ):
            protected_dependency(self.repo, "T-301")

    def test_already_done_ticket_may_supply_dependency_only_evidence(self):
        path = self.repo / "factory/tickets/T-300.md"
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                "State: Backlog", "State: Done",
            ),
            encoding="utf-8",
        )
        run("git", "add", str(path), cwd=self.repo)
        run("git", "commit", "-qm", "record external closeout", cwd=self.repo)
        self.refresh_basis()

        self.apply_and_commit()

        result = protected_dependency(self.repo, "T-300")
        self.assertEqual(
            result["basis"], "validated-protected-dependency-fulfillment"
        )
        receipt = json.loads((
            self.repo
            / "factory/migrations/dependency-fulfillment/T-300.json"
        ).read_text(encoding="utf-8"))
        self.assertEqual(receipt["source_state"], "Done")
        self.assertIn("State: Done", path.read_text(encoding="utf-8"))

    def test_partial_terminal_evidence_cannot_bypass_through_fulfillment(self):
        self.apply_and_commit()
        path = self.repo / "factory/attestations/T-300/bundle.json"
        path.parent.mkdir(parents=True)
        path.write_text("{}\n", encoding="utf-8")
        run("git", "add", ".", cwd=self.repo)
        run("git", "commit", "-qm", "introduce partial terminal evidence", cwd=self.repo)
        run(
            "git", "update-ref", "refs/remotes/origin/main",
            run("git", "rev-parse", "HEAD", cwd=self.repo),
            cwd=self.repo,
        )
        with self.assertRaisesRegex(ValidationError, "partial normal attestation"):
            protected_dependency(self.repo, "T-300")

    def test_reverted_receipt_mutation_still_fails_closed(self):
        self.apply_and_commit()
        path = (
            self.repo
            / "factory/migrations/dependency-fulfillment/T-300.json"
        )
        original = path.read_text(encoding="utf-8")
        path.write_text(original + "\n", encoding="utf-8")
        run("git", "add", str(path), cwd=self.repo)
        run("git", "commit", "-qm", "tamper", cwd=self.repo)
        path.write_text(original, encoding="utf-8")
        run("git", "add", str(path), cwd=self.repo)
        run("git", "commit", "-qm", "revert tamper", cwd=self.repo)
        run(
            "git", "update-ref", "refs/remotes/origin/main",
            run("git", "rev-parse", "HEAD", cwd=self.repo),
            cwd=self.repo,
        )
        with self.assertRaisesRegex(ValidationError, "changed after introduction"):
            protected_dependency(self.repo, "T-300")

    def test_apply_requires_the_exact_plan_hash(self):
        with mock.patch.object(CLI, "gh_json", side_effect=self.gh):
            plan = CLI.prepare(self.repo, self.request)
        previous = Path.cwd()
        try:
            import os

            os.chdir(self.repo)
            with self.assertRaisesRegex(ValidationError, "does not match"):
                CLI.apply_plan(plan, "0" * 64)
        finally:
            os.chdir(previous)
        self.assertEqual(
            (self.repo / "factory/KIT_PIN").read_text(encoding="utf-8"),
            OLD_KIT + "\n",
        )

    def test_same_target_inflight_authorization_may_share_atomic_commit(self):
        self.apply_and_commit(NEW_KIT)
        self.assertEqual(
            protected_dependency(self.repo, "T-300")["target_kit_sha"],
            NEW_KIT,
        )

    def test_other_inflight_authorization_cannot_share_atomic_commit(self):
        self.apply_and_commit("c" * 40)
        with self.assertRaisesRegex(
            ValidationError, "one atomic protected introduction"
        ):
            protected_dependency(self.repo, "T-300")


if __name__ == "__main__":
    unittest.main()
