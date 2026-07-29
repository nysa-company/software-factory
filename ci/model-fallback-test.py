#!/usr/bin/env python3
"""End-to-end regression for fallback preview and trusted handoff commit."""

import base64
import datetime as dt
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/model-fallback.py"


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


ROUTER = module("fallback_test_router", ROOT / "scripts/model-router.py")
MANAGER = module("fallback_test_manager", ROOT / "scripts/model-manager.py")


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()


class FallbackTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.repo = base / "ticket"
        self.remote = base / "remote.git"
        self.fetch_remote = base / "fetch.git"
        self.product = base / "product"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", "--bare", self.remote], check=True)
        subprocess.run(["git", "init", "-q", "--bare", self.fetch_remote], check=True)
        subprocess.run(["git", "init", "-q", "-b", "ticket/T-1", self.repo], check=True)
        git(self.repo, "config", "user.name", "Test")
        git(self.repo, "config", "user.email", "test@example.test")
        git(self.repo, "remote", "add", "origin", str(self.remote))

        catalog, routes, profiles, profile_map = ROUTER.load_policy()
        profile = profile_map["cursor-balanced-v2"]
        readiness = {
            route_id: {
                "adapter_version": "test-v1",
                "reason": "ok",
                "reported_identity": route["expected_reported_identity"],
                "state": "READY",
            }
            for route_id, route in routes.items()
            if route["enabled"]
        }
        resolution = ROUTER.resolve_policy(catalog, routes, profile, readiness)
        legacy = {
            "created_at": "2026-07-18T12:00:00Z",
            "kit_sha": "a" * 40,
            "resolution": resolution,
            "schema": "ticket-model-route-plan/v1",
            "ticket": "T-1",
        }
        legacy_raw = (ROUTER.canonical_json(legacy) + "\n").encode()
        journal = MANAGER.migrate_v1_plan(
            legacy_raw,
            "b" * 40,
            "a" * 40,
            "2026-07-18T12:01:00Z",
            catalog,
            routes,
            profile_map,
        )
        (self.repo / "factory/route-plans").mkdir(parents=True)
        (self.repo / "factory/tickets").mkdir()
        (self.repo / "src").mkdir()
        (self.repo / "factory/route-plans/T-1.json").write_text(
            ROUTER.canonical_json(journal) + "\n"
        )
        (self.repo / "factory/QUALIFICATION.json").write_text(json.dumps({
            "factory_sha": "a" * 40,
            "generation": 1,
            "schema": "nysa.software-factory.qualification/v2",
            "tickets": ["T-1"],
        }))
        (self.repo / "factory/tickets/T-1.md").write_text(
            "State: in-progress\nKit-SHA: " + "a" * 40 + "\n"
        )
        (self.repo / "src/app.txt").write_text("before\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "ticket start")
        git(self.repo, "push", "-u", "origin", "ticket/T-1")
        git(self.repo, "config", "remote.origin.pushurl", str(self.remote))
        git(self.repo, "config", "remote.origin.url", str(self.fetch_remote))
        self.head = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "update-ref", "refs/remotes/origin/main", self.head)

        failed = resolution["selections"]["builder"]
        readiness[failed["route_id"]]["state"] = "UNAVAILABLE"
        readiness[failed["route_id"]]["reason"] = "credits_exhausted"
        for value in readiness.values():
            value["adapter_version"] = "tést-v1"
        self.readiness = base / "readiness.json"
        self.readiness.write_text(ROUTER.canonical_json(readiness) + "\n")
        runs = self.product / "factory/runs"
        runs.mkdir(parents=True)
        now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
        manifest = {
            "accounting_state": "completed",
            "exit_status": "75",
            "go_issued": "1",
            "kit_sha": "a" * 40,
            "policy_hash": resolution["policy_hash"],
            "phase": "completed",
            "provider_family": failed["provider_family"],
            "role": "builder",
            "role_exit": "provider_failed",
            "role_branch_before": "ticket/T-1",
            "role_head_before": self.head,
            "role_remote_before": self.head,
            "route_id": failed["route_id"],
            "run_id": "run-failed-1",
            "task_submitted": "1",
            "terminal_at": now,
            "ticket": "T-1",
        }
        (runs / "run-failed-1.meta").write_text(
            "".join(f"{key}={value}\n" for key, value in sorted(manifest.items()))
        )
        (self.product / "factory/runtime-ledger.csv").write_text(
            "ticket,run_id,exit_status\nT-1,run-failed-1,75\n"
        )
        (self.repo / "src/app.txt").write_text("partial handoff\n")

    def tearDown(self):
        self.temp.cleanup()

    def command(
        self, action, *extra, check=True, reason="credits_exhausted"
    ):
        result = subprocess.run(
            [
                sys.executable, str(SCRIPT), action,
                "--workdir", str(self.repo),
                "--factory-root", str(self.product),
                "--project", "test",
                "--ticket", "T-1",
                "--failed-run", "run-failed-1",
                "--reason", reason,
                "--readiness", str(self.readiness),
                "--remote", str(self.remote),
                *extra,
            ],
            text=True,
            capture_output=True,
        )
        if result.returncode and check:
            self.fail(result.stderr)
        if result.returncode:
            return result
        return json.loads(result.stdout)

    def test_cancelled_attempt_is_eligible_for_same_stage_fallback(self):
        path = self.product / "factory/runs/run-failed-1.meta"
        values = {}
        for line in path.read_text().splitlines():
            key, value = line.split("=", 1)
            values[key] = value
        values.update({
            "accounting_state": "cancelled_conservative",
            "cancellation_reason": "budget_exhausted",
            "phase": "cancelled_conservative",
            "role_exit": "cancelled",
        })
        path.write_text("".join(
            f"{key}={value}\n" for key, value in sorted(values.items())
        ))
        preview = self.command("preview", reason="budget_exhausted")
        self.assertEqual(preview["reason"], "budget_exhausted")
        self.assertEqual(preview["failed_run_id"], "run-failed-1")

    def test_preview_then_apply_commits_partial_work_and_journal_once(self):
        preview = self.command("preview")
        journal_path = self.repo / "factory/route-plans/T-1.json"
        initial_journal = journal_path.read_bytes()
        approval = Path(self.temp.name) / "approval.json"
        approval.write_text(json.dumps({
            "approval_hash": preview["approval_hash"],
            "comment_id": "linear-comment-1",
            "failed_run_id": "run-failed-1",
            "nonce": preview["nonce"],
            "operator_id": "linear-user-1",
            "reason": "credits_exhausted",
            "schema": "model-fallback-linear-approval/v1",
        }))
        applied = self.command("apply", "--approval", str(approval))
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), applied["commit_sha"])
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")
        self.assertEqual(
            git(self.repo, "show", "HEAD:src/app.txt"), "partial handoff"
        )
        journal = json.loads(
            git(self.repo, "show", "HEAD:factory/route-plans/T-1.json")
        )
        journal_raw = git(
            self.repo, "show", "HEAD:factory/route-plans/T-1.json"
        )
        self.assertIn("tést-v1", journal_raw)
        self.assertEqual(journal_raw, ROUTER.canonical_json(journal))
        self.assertEqual(len(journal["revisions"]), 2)
        self.assertEqual(
            journal["revisions"][-1]["revision_hash"], applied["revision_hash"]
        )
        # Simulate a crash after update-ref but before index/worktree journal
        # publication. Retry must recognize and complete the committed handoff.
        git(self.repo, "read-tree", "HEAD^")
        journal_path.write_bytes(initial_journal)
        recovered = self.command("apply", "--approval", str(approval))
        self.assertTrue(recovered["recovered"])
        self.assertEqual(recovered["commit_sha"], applied["commit_sha"])
        consumed_recovery = self.command("recover")
        self.assertTrue(consumed_recovery["recovered"])
        self.assertEqual(
            consumed_recovery["approval_receipt"]["comment_id"],
            "linear-comment-1",
        )
        self.assertEqual(
            len(json.loads(
                git(self.repo, "show", "HEAD:factory/route-plans/T-1.json")
            )["revisions"]),
            2,
        )

    def test_qualification_apply_uses_direct_cli_once(self):
        initial = json.loads(
            (self.repo / "factory/route-plans/T-1.json").read_text()
        )
        reviewer_route = MANAGER.active_resolution(initial)["selections"]["reviewer"][
            "route_id"
        ]
        readiness = json.loads(self.readiness.read_text())
        readiness[reviewer_route].update({
            "reason": "model_unavailable",
            "state": "INVALID",
        })
        self.readiness.write_text(ROUTER.canonical_json(readiness) + "\n")

        applied = self.command("qualification-apply")
        journal = json.loads(
            git(self.repo, "show", "HEAD:factory/route-plans/T-1.json")
        )
        resolution = journal["revisions"][-1]["body"]["new_resolution"]
        self.assertEqual(resolution["future_roles"], ["builder"])
        self.assertEqual(resolution["selections"]["builder"]["adapter"], "codex")
        self.assertEqual(
            resolution["selections"]["reviewer"]["route_id"], reviewer_route
        )
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), applied["commit_sha"])
        recovered = self.command("qualification-apply")
        self.assertTrue(recovered["recovered"])
        self.assertEqual(recovered["commit_sha"], applied["commit_sha"])

    def test_qualification_apply_migrates_initial_v1_plan(self):
        path = self.repo / "factory/route-plans/T-1.json"
        journal = json.loads(path.read_text())
        legacy = base64.b64decode(
            journal["revisions"][0]["body"]["legacy_plan_b64"]
        )
        path.write_bytes(legacy)
        git(self.repo, "add", str(path.relative_to(self.repo)))
        git(self.repo, "commit", "-m", "restore initial route plan")
        git(self.repo, "push", "origin", "ticket/T-1")
        head = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "update-ref", "refs/remotes/origin/main", head)
        manifest = self.product / "factory/runs/run-failed-1.meta"
        manifest.write_text(
            manifest.read_text()
            .replace(f"role_head_before={self.head}", f"role_head_before={head}")
            .replace(f"role_remote_before={self.head}", f"role_remote_before={head}")
        )

        applied = self.command("qualification-apply")
        migrated = json.loads(
            git(self.repo, "show", "HEAD:factory/route-plans/T-1.json")
        )
        self.assertEqual(
            [item["body"]["kind"] for item in migrated["revisions"]],
            ["migration", "fallback"],
        )
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), applied["commit_sha"])

    def test_qualification_apply_refuses_a_second_role_attempt(self):
        second = self.product / "factory/runs/run-failed-2.meta"
        second.write_text(
            (self.product / "factory/runs/run-failed-1.meta").read_text()
            .replace("run_id=run-failed-1", "run_id=run-failed-2")
        )
        result = self.command("qualification-apply", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("only after the first role attempt", result.stderr)

    def test_handoff_preserves_role_commits_and_remaining_dirty_work(self):
        git(self.repo, "add", "src/app.txt")
        git(self.repo, "commit", "-m", "partial builder progress")
        role_commit = git(self.repo, "rev-parse", "HEAD")
        (self.repo / "src/app.txt").write_text("remaining handoff\n")

        preview = self.command("preview")
        approval = Path(self.temp.name) / "approval-with-commit.json"
        approval.write_text(json.dumps({
            "approval_hash": preview["approval_hash"],
            "comment_id": "linear-comment-2",
            "failed_run_id": "run-failed-1",
            "nonce": preview["nonce"],
            "operator_id": "linear-user-1",
            "reason": "credits_exhausted",
            "schema": "model-fallback-linear-approval/v1",
        }))
        applied = self.command("apply", "--approval", str(approval))

        self.assertEqual(git(self.repo, "rev-parse", "HEAD^"), role_commit)
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), applied["commit_sha"])
        self.assertEqual(
            git(self.repo, "show", "HEAD:src/app.txt"), "remaining handoff"
        )
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")

    def test_conservatively_accounted_completed_failure_is_eligible(self):
        manifest = self.product / "factory/runs/run-failed-1.meta"
        values = {}
        for line in manifest.read_text().splitlines():
            key, value = line.split("=", 1)
            values[key] = value
        values["accounting_state"] = "abandoned_conservative"
        values["phase"] = "completed"
        manifest.write_text(
            "".join(f"{key}={value}\n" for key, value in sorted(values.items()))
        )
        preview = self.command("preview")
        self.assertEqual(preview["failed_run_id"], "run-failed-1")

    def test_pre_submission_failure_is_ineligible(self):
        manifest = self.product / "factory/runs/run-failed-1.meta"
        manifest.write_text(
            manifest.read_text().replace("task_submitted=1", "task_submitted=0")
        )
        result = self.command("preview", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "not an eligible terminal provider failure",
            result.stderr,
        )


if __name__ == "__main__":
    unittest.main()
