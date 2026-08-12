#!/usr/bin/env python3
"""Focused two-command release transaction regressions."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location(
    "release_transaction", ROOT / "scripts/release-transaction.py"
)
assert SPEC and SPEC.loader
RELEASE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RELEASE)


class ReleaseTransactionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="release-transaction-test.")
        self.root = Path(self.temp.name).resolve()
        self.product = self.root / "product"
        (self.product / "factory").mkdir(parents=True)
        self.kits = self.root / "kits"
        (self.kits / "projects/relay/release-plans/journals").mkdir(parents=True)
        self.sha = "a" * 40
        self.body = {
            "children": {
                "migration": None,
                "model": {
                    "profile_hash": "b" * 64,
                    "profile_id": "openai-priority-v1",
                    "profile_version": 1,
                },
                "provider_cli": {"action": "reuse"},
                "provider_concurrency": {"action": "reuse"},
                "qualification": {"status": "prepared"},
                "receipt": {
                    "path": str(self.root / "receipt.json"),
                    "receipt_id": "c" * 64,
                    "sha256": "d" * 64,
                },
            },
            "created_epoch": 1,
            "expires_epoch": 4_000_000_000,
            "identity": {
                "capacity": 1,
                "contract_version": "1.9.0",
                "factory_origin": str(self.root / "factory-origin"),
                "factory_sha": self.sha,
                "factory_tree": "e" * 40,
                "mode": "new",
                "previous": None,
                "product_origin": str(self.root / "product-origin"),
                "product_path": str(self.product),
                "product_sha": "f" * 40,
                "product_tree": "1" * 40,
                "runtime": {
                    "evidence": {"path": str(self.root / "runtime")},
                    "plan_sha256": "2" * 64,
                },
            },
            "request": {
                "cli_paths": {}, "migrations": [], "operator_id": "tester",
                "product": str(self.product), "profile": "openai-priority-v1",
                "project": "relay", "repo": str(self.root / "factory"),
                "runtime_bin": None, "sha": self.sha,
            },
            "schema": RELEASE.PLAN_SCHEMA,
            "stage": "activation",
            "status": "approval-required",
        }
        self.plan = RELEASE.seal_plan(self.body)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_composite_approval_rejects_every_bound_tamper(self) -> None:
        RELEASE.validate_plan(self.plan)
        mutations = (
            ("identity", "factory_tree", "2" * 40),
            ("identity", "product_tree", "3" * 40),
            ("request", "profile", "balanced-v2"),
            ("request", "migrations", [{"ticket": "T-1", "workdir": "/tmp/x"}]),
            ("children", "receipt", {"receipt_id": "4" * 64}),
        )
        for first, second, changed in mutations:
            with self.subTest(field=f"{first}.{second}"):
                candidate = json.loads(json.dumps(self.plan))
                candidate[first][second] = changed
                with self.assertRaisesRegex(RELEASE.ReleaseError, "release plan is invalid"):
                    RELEASE.validate_plan(candidate)

    def test_signed_phase_journal_rejects_tamper_and_orders_recovery(self) -> None:
        path = self.root / "journal.json"
        first = RELEASE.journal_update(path, self.plan, "approved", "pass")
        second = RELEASE.journal_update(path, self.plan, "activated", "pass")
        self.assertEqual(
            [event["phase"] for event in second["events"]],
            ["approved", "activated"],
        )
        self.assertLessEqual(
            second["events"][0]["observed_epoch_ms"],
            second["events"][1]["observed_epoch_ms"],
        )
        tampered = json.loads(path.read_text())
        tampered["phase"] = "dispatch_started"
        path.write_text(json.dumps(tampered))
        path.chmod(0o600)
        with self.assertRaisesRegex(RELEASE.ReleaseError, "release journal is invalid"):
            RELEASE.journal_update(path, self.plan, "doctor_pass", "pass")

    def test_cutover_barrier_never_overwrites_operator_kill(self) -> None:
        maintenance = self.product / "factory/MAINTENANCE"
        maintenance.write_text("{}")
        RELEASE.ensure_barrier(self.product, self.plan)
        marker = self.product / "factory/KILL"
        self.assertEqual(json.loads(marker.read_text()), RELEASE.barrier_value(self.plan))
        marker.write_text("operator incident\n")
        with self.assertRaisesRegex(RELEASE.ReleaseError, "existing KILL"):
            RELEASE.ensure_barrier(self.product, self.plan)

    def test_lost_response_after_dispatch_start_replays_without_mutation(self) -> None:
        journal = self.root / "dispatch-lost.json"
        RELEASE.journal_update(journal, self.plan, "doctor_pass", "pass")
        with (
            mock.patch.object(RELEASE, "active_exact", return_value=True),
            mock.patch.object(RELEASE, "model_ready", return_value=True),
            mock.patch.object(RELEASE, "doctor", return_value={"status": "pass"}),
        ):
            result = RELEASE.apply_activation(self.plan, self.kits, "tester", journal)
        self.assertEqual(result["status"], "replayed")
        value = RELEASE.safe_state(journal, "release journal")
        self.assertEqual(value["phase"], "dispatch_started")
        self.assertEqual(value["status"], "pass")
        self.assertFalse((self.product / "factory/KILL").exists())
        self.assertFalse((self.product / "factory/MAINTENANCE").exists())

    def test_resume_rejects_wrong_hash_approver_and_expiry_before_apply(self) -> None:
        path, _ = RELEASE.plan_paths(self.kits, "relay", self.sha)
        RELEASE.write_plan(path, self.plan)
        base = argparse.Namespace(
            project="relay", sha=self.sha, approve_hash="9" * 64,
            approved_by="tester", kits_root=self.kits,
        )
        with self.assertRaisesRegex(RELEASE.ReleaseError, "approved hash"):
            RELEASE.resume(base)
        base.approve_hash = self.plan["approval_sha256"]
        base.approved_by = "someone-else"
        with self.assertRaisesRegex(RELEASE.ReleaseError, "approver"):
            RELEASE.resume(base)
        expired = json.loads(json.dumps(self.plan))
        body = {key: value for key, value in expired.items() if key != "approval_sha256"}
        body["expires_epoch"] = 2
        expired = RELEASE.seal_plan(body)
        RELEASE.write_plan(path, expired)
        base.approve_hash = expired["approval_sha256"]
        base.approved_by = "tester"
        with self.assertRaisesRegex(RELEASE.ReleaseError, "stale"):
            RELEASE.resume(base)

    def test_expired_plan_resumes_only_with_a_pre_expiry_signed_approval(self) -> None:
        path, journals = RELEASE.plan_paths(self.kits, "relay", self.sha)
        body = {key: value for key, value in self.plan.items() if key != "approval_sha256"}
        body["created_epoch"] = 1
        body["expires_epoch"] = 2
        expired = RELEASE.seal_plan(body)
        RELEASE.write_plan(path, expired)
        journal = journals / f"{expired['approval_sha256']}.json"
        with mock.patch.object(RELEASE.time, "time", return_value=1.5):
            RELEASE.journal_update(journal, expired, "approved", "pass")
        args = argparse.Namespace(
            project="relay", sha=self.sha,
            approve_hash=expired["approval_sha256"], approved_by="tester",
            kits_root=self.kits,
        )
        with (
            mock.patch.object(RELEASE, "apply_activation", return_value={"status": "replayed"}),
            mock.patch.object(RELEASE, "validate_live_basis"),
            mock.patch.object(RELEASE.time, "time", return_value=3),
        ):
            self.assertEqual(RELEASE.resume(args)["status"], "replayed")

    def test_setup_emits_prerequisite_plan_before_certification(self) -> None:
        repo = self.root / "factory"
        repo.mkdir()
        (self.product / "factory/KIT_PIN").write_text(self.sha + "\n")
        args = argparse.Namespace(
            project="relay", product=self.product, repo=repo, sha=self.sha,
            kits_root=self.kits, profile="openai-priority-v1", operator_id="tester",
            runtime_bin=None, claude_bin=None, codex_bin=None, cursor_bin=None,
            ticket_workdir=[],
        )
        runtime = {
            "evidence": {"path": str(self.root / "runtime")},
            "plan_sha256": "2" * 64,
        }
        concurrency = {"action": "apply", "plan": {"approval_sha256": "3" * 64}}
        cli = {"action": "reuse", "evidence": {"status": "pass"}}
        with (
            mock.patch.object(RELEASE, "clean_identity", side_effect=[
                (self.sha, "e" * 40, str(repo)),
                ("f" * 40, "1" * 40, str(self.product)),
            ]),
            mock.patch.object(RELEASE, "run"),
            mock.patch.object(RELEASE, "contract", return_value="1.9.0"),
            mock.patch.object(RELEASE, "prepare_runtime", return_value=runtime),
            mock.patch.object(RELEASE, "capacity", return_value=2),
            mock.patch.object(RELEASE, "child_plan", return_value=(concurrency, cli)),
        ):
            plan = RELEASE.setup(args)
        self.assertEqual(plan["stage"], "prerequisites")
        self.assertEqual(plan["children"]["provider_concurrency"], concurrency)
        RELEASE.validate_plan(plan)

    def test_setup_emits_receipt_bound_activation_plan(self) -> None:
        repo = self.root / "factory"
        repo.mkdir()
        (self.product / "factory/KIT_PIN").write_text(self.sha + "\n")
        receipt_path = self.root / "receipt.json"
        receipt_path.write_text(json.dumps({"receipt_id": "c" * 64}))
        receipt_path.chmod(0o600)
        args = argparse.Namespace(
            project="relay", product=self.product, repo=repo, sha=self.sha,
            kits_root=self.kits, profile="openai-priority-v1", operator_id="tester",
            runtime_bin=None, claude_bin=None, codex_bin=None, cursor_bin=None,
            ticket_workdir=[],
        )
        runtime = {
            "evidence": {"path": str(self.root / "runtime")},
            "plan_sha256": "2" * 64,
        }
        reuse = {"action": "reuse", "evidence": {"status": "pass"}}
        model = {
            "profile_hash": "b" * 64, "profile_id": "openai-priority-v1",
            "profile_version": 1,
        }
        with (
            mock.patch.object(RELEASE, "clean_identity", side_effect=[
                (self.sha, "e" * 40, str(repo)),
                ("f" * 40, "1" * 40, str(self.product)),
            ]),
            mock.patch.object(RELEASE, "run"),
            mock.patch.object(RELEASE, "contract", return_value="1.9.0"),
            mock.patch.object(RELEASE, "prepare_runtime", return_value=runtime),
            mock.patch.object(RELEASE, "capacity", return_value=1),
            mock.patch.object(RELEASE, "child_plan", return_value=(reuse, reuse)),
            mock.patch.object(RELEASE, "find_receipt", return_value=(
                receipt_path, {"receipt_id": "c" * 64},
            )),
            mock.patch.object(RELEASE, "qualification_plans", return_value=(
                {"status": "prepared"}, model, None,
            )),
        ):
            plan = RELEASE.setup(args)
        self.assertEqual(plan["stage"], "activation")
        self.assertEqual(plan["children"]["receipt"]["sha256"], RELEASE.file_digest(receipt_path))
        RELEASE.validate_plan(plan)

    def test_public_setup_command_forwards_every_exact_argument(self) -> None:
        copy = self.root / "wrapper"
        (copy / "scripts/lib").mkdir(parents=True)
        shutil.copy2(ROOT / "scripts/factory-kit.sh", copy / "scripts/factory-kit.sh")
        shutil.copy2(
            ROOT / "scripts/lib/dispatch-leases.sh",
            copy / "scripts/lib/dispatch-leases.sh",
        )
        (copy / "scripts/release-transaction.py").write_text(
            "import json,sys\nprint(json.dumps(sys.argv[1:]))\n"
        )
        command = [
            "bash", str(copy / "scripts/factory-kit.sh"), "release", "setup",
            "--project", "relay", "--product", str(self.product), "--sha", self.sha,
            "--repo", str(self.root / "repo"), "--profile", "openai-priority-v1",
            "--operator-id", "tester", "--runtime-bin", str(self.root / "runtime"),
            "--claude-bin", "/a", "--codex-bin", "/b", "--cursor-bin", "/c",
            "--ticket-workdir", "T-1", str(self.root / "worktree"),
        ]
        result = subprocess.run(
            command, text=True, capture_output=True, check=False,
            env={**os.environ, "FACTORY_KITS_ROOT": str(self.root / "state")},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        forwarded = json.loads(result.stdout)
        self.assertEqual(forwarded[2:4], ["setup", "--project"])
        self.assertEqual(forwarded[-3:], ["--ticket-workdir", "T-1", str(self.root / "worktree")])

    def test_release_only_options_are_rejected_elsewhere(self) -> None:
        result = subprocess.run(
            ["bash", str(ROOT / "scripts/factory-kit.sh"), "status", "--project",
             "relay", "--profile", "x"], text=True, capture_output=True, check=False,
            env={**os.environ, "FACTORY_KITS_ROOT": str(self.root / "state")},
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("release-only option", result.stderr)

    def test_prerequisite_lost_response_returns_the_same_activation_plan(self) -> None:
        activation_path, journals = RELEASE.plan_paths(self.kits, "relay", self.sha)
        RELEASE.write_plan(activation_path, self.plan)
        body = {key: value for key, value in self.plan.items() if key != "approval_sha256"}
        body["stage"] = "prerequisites"
        body["children"] = {
            "provider_cli": {"action": "apply", "plan": {"approval_sha256": "6" * 64}},
            "provider_concurrency": {"action": "reuse"},
        }
        prerequisite = RELEASE.seal_plan(body)
        RELEASE.write_plan(activation_path, prerequisite)
        journal = journals / f"{prerequisite['approval_sha256']}.json"
        RELEASE.journal_update(journal, prerequisite, "approved", "pass")
        RELEASE.journal_update(
            journal, prerequisite, "prerequisites_applied", "pass",
            self.plan["approval_sha256"],
        )
        args = argparse.Namespace(
            project="relay", sha=self.sha,
            approve_hash=prerequisite["approval_sha256"], approved_by="tester",
            kits_root=self.kits,
        )
        with (
            mock.patch.object(RELEASE, "apply_prerequisites") as apply,
            mock.patch.object(RELEASE, "validate_live_basis"),
        ):
            self.assertEqual(RELEASE.resume(args), self.plan)
        apply.assert_not_called()

    def test_cutover_failure_restores_maintenance_and_keeps_dispatch_stopped(self) -> None:
        maintenance = self.product / "factory/MAINTENANCE"
        maintenance.write_text(json.dumps({
            "product_path": str(self.product), "project": "relay",
        }))
        journal = self.root / "cutover-failure.json"

        def restore(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
            maintenance.write_text(json.dumps({
                "product_path": str(self.product), "project": "relay",
            }))
            return subprocess.CompletedProcess([], 0, "", "")

        with (
            mock.patch.object(RELEASE, "active_exact", return_value=True),
            mock.patch.object(RELEASE, "model_ready", return_value=False),
            mock.patch.object(
                RELEASE, "run_json", side_effect=RELEASE.ReleaseError("injected model failure"),
            ),
            mock.patch.object(RELEASE.subprocess, "run", side_effect=restore),
        ):
            with self.assertRaisesRegex(RELEASE.ReleaseError, "injected model failure"):
                RELEASE.apply_activation(self.plan, self.kits, "tester", journal)
        self.assertTrue(maintenance.exists())
        self.assertEqual(
            json.loads((self.product / "factory/KILL").read_text()),
            RELEASE.barrier_value(self.plan),
        )

    def test_completed_migration_requires_an_exact_signed_batch_journal(self) -> None:
        migration = {
            "approval_sha256": "4" * 64,
            "items": [{"ticket": "T-1"}],
        }
        body = {key: value for key, value in self.plan.items() if key != "approval_sha256"}
        body["children"] = {**body["children"], "migration": migration}
        plan = RELEASE.seal_plan(body)
        path = self.kits / "projects/relay/controller/migration-batches" / f"{'4' * 64}.json"
        journal_body = {
            "approved_by": "tester", "created_at": "x", "plan": migration,
            "results": {"T-1": {"ticket": "T-1"}},
            "schema": "nysa.software-factory.model-migration-batch-journal/v1",
            "status": "pass", "updated_at": "x",
        }
        RELEASE.atomic_json(path, {**journal_body, "record_sha256": RELEASE.digest(journal_body)})
        self.assertTrue(RELEASE.migration_complete(self.kits, plan, "tester"))
        value = json.loads(path.read_text())
        value["approved_by"] = "intruder"
        path.write_text(json.dumps(value))
        path.chmod(0o600)
        self.assertFalse(RELEASE.migration_complete(self.kits, plan, "tester"))

    def test_existing_qualification_seed_is_reused_without_mutation(self) -> None:
        (self.product / "factory/QUALIFICATION.json").write_text(
            '{"tickets":["T-1"]}\n'
        )
        seed_root = self.kits / "seed"
        seed_root.mkdir()
        (seed_root / "controller").mkdir()
        seed = seed_root / "operator-map.json"
        seed.write_text('{"tickets":{}}\n')
        seed.chmod(0o600)
        before = seed.read_bytes()
        self.assertEqual(RELEASE.create_seed(self.root, self.product, self.kits), seed)
        self.assertEqual(seed.read_bytes(), before)

    def test_resume_live_basis_refuses_product_and_runtime_drift(self) -> None:
        (self.product / "factory/KIT_PIN").write_text(self.sha + "\n")
        runtime_journal = self.kits / "projects/relay/runtime/runtime-pin-journal.json"
        RELEASE.atomic_json(runtime_journal, {
            "plan": {"approval_sha256": "2" * 64}, "status": "completed",
        })
        product_identity = ("f" * 40, "1" * 40, str(self.root / "product-origin"))
        with (
            mock.patch.object(RELEASE, "clean_identity", return_value=product_identity),
            mock.patch.object(RELEASE, "contract", return_value="1.9.0"),
            mock.patch.object(RELEASE, "run_json", return_value={"status": "pass"}),
        ):
            RELEASE.validate_live_basis(self.kits, self.plan)
        drifted = ("f" * 40, "9" * 40, str(self.root / "product-origin"))
        with (
            mock.patch.object(RELEASE, "clean_identity", return_value=drifted),
            mock.patch.object(RELEASE, "contract", return_value="1.9.0"),
        ):
            with self.assertRaisesRegex(RELEASE.ReleaseError, "product changed"):
                RELEASE.validate_live_basis(self.kits, self.plan)
        value = json.loads(runtime_journal.read_text())
        value["plan"]["approval_sha256"] = "8" * 64
        runtime_journal.write_text(json.dumps(value))
        runtime_journal.chmod(0o600)
        with (
            mock.patch.object(RELEASE, "clean_identity", return_value=product_identity),
            mock.patch.object(RELEASE, "contract", return_value="1.9.0"),
        ):
            with self.assertRaisesRegex(RELEASE.ReleaseError, "runtime changed"):
                RELEASE.validate_live_basis(self.kits, self.plan)


if __name__ == "__main__":
    unittest.main()
