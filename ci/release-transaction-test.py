#!/usr/bin/env python3
"""Focused two-command release transaction regressions."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib.util
import json
import multiprocessing
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest import mock
import sys


ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location(
    "release_transaction", ROOT / "scripts/release-transaction.py"
)
assert SPEC and SPEC.loader
RELEASE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RELEASE)
sys.path.insert(0, str(ROOT / "scripts/lib"))
import activation_preflight as ACTIVATION  # noqa: E402
import historical_pr_objects as HISTORY  # noqa: E402


def cutover_lock_worker(
    kits: str, marker: str, plan: dict[str, object], mode: str,
) -> None:
    def hold(*_args: object) -> None:
        with Path(marker).open("a", encoding="utf-8") as stream:
            stream.write(f"start:{plan['request']['project']}\n")
        time.sleep(0.25)
        with Path(marker).open("a", encoding="utf-8") as stream:
            stream.write(f"end:{plan['request']['project']}\n")

    if mode == "setup":
        RELEASE._setup_locked = hold
        RELEASE.setup(argparse.Namespace(
            kits_root=Path(kits), request={"project": plan["request"]["project"]},
        ))
    elif mode == "resume":
        RELEASE._resume_locked = hold
        RELEASE.resume(argparse.Namespace(
            approved_by="tester", kits_root=Path(kits),
            project=plan["request"]["project"], sha="7" * 40,
        ))
    else:
        RELEASE._apply_host_cutover_locked = hold
        RELEASE.apply_host_cutover(plan, Path(kits), Path(kits))


class ReleaseTransactionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="release-transaction-test.")
        self.root = Path(self.temp.name).resolve()
        self.product = self.root / "product"
        (self.product / "factory").mkdir(parents=True)
        RELEASE.atomic_json(self.product / "factory/operator-map.json", {
            "_config": None, "_sync": {}, "initiatives": {}, "tickets": {},
        })
        self.kits = self.root / "kits"
        (self.kits / "projects/relay/release-plans/journals").mkdir(parents=True)
        self.sha = "a" * 40
        self.body = {
            "children": {
                "launcher": {
                    "action": "reuse", "path": str(self.root / "launcher"),
                    "sha256": "5" * 64,
                },
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
                "contract_version": "2.0.0",
                "controller": {"platform": "test", "status": "not-applicable"},
                "factory_origin": str(self.root / "factory-origin"),
                "factory_sha": self.sha,
                "factory_tree": "e" * 40,
                "maintenance_prior": None,
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
                "tickets": [],
            },
            "request": {
                "cli_paths": {}, "migrations": [], "operator_id": "tester",
                "product": str(self.product), "profile": "openai-priority-v1",
                "project": "relay", "repo": str(self.root / "factory"),
                "runtime_bin": None, "sha": self.sha,
                "skip_optional_tests": False,
            },
            "schema": RELEASE.PLAN_SCHEMA,
            "stage": "activation",
            "status": "authorized",
        }
        self.plan = RELEASE.seal_plan(self.body)

    def retired_runtime(self, home: Path, *, action: str = "reuse") -> dict[str, object]:
        removed = "her" + "mes"
        return {
            "action": action,
            "profile": {
                "path": str(home / f".{removed}/profiles/factory"),
                "tree_sha256": None,
            },
            "services": [
                {
                    "label": f"com.nysa.{removed}-dashboard", "loaded": False,
                    "path": str(home / "Library/LaunchAgents" / f"com.nysa.{removed}-dashboard.plist"),
                    "sha256": None,
                },
                {
                    "label": f"com.nysa.{removed}-factory-gateway", "loaded": False,
                    "path": str(home / "Library/LaunchAgents" / f"com.nysa.{removed}-factory-gateway.plist"),
                    "sha256": None,
                },
            ],
        }

    def test_retired_runtime_plan_streams_large_profile_files(self) -> None:
        home = self.root / "home"
        removed = "her" + "mes"
        profile = home / f".{removed}/profiles/factory"
        profile.mkdir(parents=True)
        database = profile / "state.db"
        database.write_bytes(b"factory\0")
        os.truncate(database, 10_000_001)
        with (
            mock.patch.object(RELEASE, "account_home", return_value=home),
            mock.patch.dict(os.environ, {"FACTORY_KIT_TEST_MODE": "1"}),
        ):
            plan = RELEASE.retired_runtime_plan()
        self.assertEqual(plan["action"], "apply")
        self.assertRegex(plan["profile"]["tree_sha256"], r"^[0-9a-f]{64}$")

    def test_retired_service_freeze_replans_after_mutable_profile_drift(self) -> None:
        home = self.root / "home"
        removed = "her" + "mes"
        profile = home / f".{removed}/profiles/factory"
        profile.mkdir(parents=True)
        database = profile / "state.db"
        database.write_bytes(b"before")
        with (
            mock.patch.object(RELEASE, "account_home", return_value=home),
            mock.patch.dict(os.environ, {"FACTORY_KIT_TEST_MODE": "1"}),
        ):
            retired = RELEASE.retired_runtime_plan()
            with mock.patch.object(RELEASE, "service_loaded", return_value=True):
                self.assertFalse(RELEASE.retired_runtime_matches(
                    retired, "1" * 64, check_profile=False,
                ))
            retired["services"][1]["loaded"] = True
            database.write_bytes(b"after")
            self.assertFalse(RELEASE.retired_runtime_matches(retired, "1" * 64))
            self.assertTrue(RELEASE.retired_runtime_matches(
                retired, "1" * 64, check_profile=False,
            ))
            plan = json.loads(json.dumps(self.plan))
            plan["stage"] = "prerequisites"
            plan["children"] = {
                "host_cutover": None,
                "launcher": plan["children"]["launcher"],
                "provider_cli": {"action": "reuse"},
                "provider_concurrency": {"action": "reuse"},
                "retired_runtime": retired,
            }
            order = []
            with (
                mock.patch.object(
                    RELEASE, "unload_service",
                    side_effect=lambda service: order.append(service["label"]),
                ),
                mock.patch.object(
                    RELEASE, "setup",
                    side_effect=lambda _args: order.append("setup") or self.plan,
                ),
            ):
                self.assertEqual(
                    RELEASE.apply_prerequisites(plan, self.kits, "tester"), self.plan,
                )
        self.assertEqual(order, [
            service["label"] for service in retired["services"]
        ] + ["setup"])

    def test_project_launcher_prerequisite_does_not_retire_legacy_runtime(self) -> None:
        home = self.root / "home"
        target = home / ".factory/kits/releases" / self.sha / "scripts/factory-launch"
        plan = json.loads(json.dumps(self.plan))
        plan["stage"] = "prerequisites"
        plan["children"] = {
            "host_cutover": None,
            "launcher": {
                "action": "apply", "active_projects": [],
                "approval_sha256": "4" * 64,
                "candidate": {"path": str(target), "sha256": "5" * 64},
                "human_cli": {
                    "candidate": {"path": str(self.root / "factory-cli.py"), "sha256": "6" * 64},
                    "previous_sha256": None, "target": str(home / ".factory/bin/factory"),
                },
                "previous_sha256": "5" * 64,
                "schema": "nysa.software-factory.owner-launcher-pin-plan/v3",
                "target": str(target),
            },
            "provider_cli": {"action": "reuse"},
            "provider_concurrency": {"action": "reuse"},
            "retired_runtime": self.retired_runtime(home, action="apply"),
        }
        plan["children"]["retired_runtime"]["services"][0]["loaded"] = True
        order = []
        with (
            mock.patch.object(RELEASE, "account_home", return_value=home),
            mock.patch.object(
                RELEASE, "apply_launcher_plan",
                side_effect=lambda *_args: order.append("launcher"),
            ),
            mock.patch.object(
                RELEASE, "unload_service",
                side_effect=lambda *_args: order.append("retired"),
            ),
            mock.patch.object(
                RELEASE, "setup", side_effect=lambda _args: order.append("setup") or self.plan,
            ),
        ):
            self.assertEqual(
                RELEASE.apply_prerequisites(plan, self.kits, "tester"), self.plan,
            )
        self.assertEqual(order, ["launcher", "setup"])

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

    def test_composite_plan_rejects_invalid_ticket_inventory(self) -> None:
        body = json.loads(json.dumps(self.body))
        body["identity"]["tickets"] = [{
            "blob": "3" * 40, "state": "Invented", "ticket": "T-1",
        }]
        with self.assertRaisesRegex(RELEASE.ReleaseError, "release plan is invalid"):
            RELEASE.validate_plan(RELEASE.seal_plan(body))
        body = json.loads(json.dumps(self.body))
        body["request"]["skip_optional_tests"] = "yes"
        with self.assertRaisesRegex(RELEASE.ReleaseError, "release plan is invalid"):
            RELEASE.validate_plan(RELEASE.seal_plan(body))

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

    def test_activation_restores_preexisting_maintenance_exactly(self) -> None:
        prior = b'{"incident":"operator"}\n'
        snapshot = self.root / "preexisting-maintenance"
        RELEASE.atomic_bytes(snapshot, prior)
        marker = self.product / "factory/MAINTENANCE"
        RELEASE.atomic_json(marker, {
            "product_path": str(self.product), "project": "relay",
        })
        body = {key: value for key, value in self.plan.items() if key != "approval_sha256"}
        body["identity"] = {
            **body["identity"],
            "maintenance_prior": {
                "path": str(snapshot),
                "sha256": RELEASE.hashlib.sha256(prior).hexdigest(),
            },
        }
        plan = RELEASE.seal_plan(body)
        with (
            mock.patch.object(RELEASE, "active_exact", return_value=True),
            mock.patch.object(RELEASE, "model_ready", return_value=True),
            mock.patch.object(RELEASE, "doctor", return_value={"status": "pass"}),
            mock.patch.object(RELEASE, "initialize_operator_map"),
            mock.patch.object(RELEASE, "ensure_controller"),
        ):
            result = RELEASE.apply_activation(
                plan, self.kits, "tester", self.root / "maintenance-restore-journal",
            )
        self.assertEqual(result["status"], "pass")
        self.assertEqual(marker.read_bytes(), prior)

    def test_prerequisite_replay_preserves_preexisting_maintenance(self) -> None:
        plan = json.loads(json.dumps(self.plan))
        prior = {"path": str(self.root / "snapshot"), "sha256": "9" * 64}
        plan["identity"]["maintenance_prior"] = prior
        plan["children"]["host_cutover"] = None
        request = RELEASE.plan_request(plan, self.kits)
        self.assertEqual(request.maintenance_prior, prior)

    def test_resume_uses_current_sealed_plan_and_rejects_approver_or_expiry(self) -> None:
        path, _ = RELEASE.plan_paths(self.kits, "relay", self.sha)
        RELEASE.write_plan(path, self.plan)
        base = argparse.Namespace(
            project="relay", sha=self.sha, approved_by="someone-else",
            kits_root=self.kits,
        )
        with self.assertRaisesRegex(RELEASE.ReleaseError, "approver"):
            RELEASE.resume(base)
        alternate_body = {
            key: value for key, value in self.plan.items() if key != "approval_sha256"
        }
        alternate_body["created_epoch"] = 2
        alternate = RELEASE.seal_plan(alternate_body)
        RELEASE.atomic_json(path, alternate)
        base.approved_by = "tester"
        with self.assertRaisesRegex(RELEASE.ReleaseError, "sealed copy"):
            RELEASE.resume(base)
        expired = json.loads(json.dumps(self.plan))
        body = {key: value for key, value in expired.items() if key != "approval_sha256"}
        body["expires_epoch"] = 2
        expired = RELEASE.seal_plan(body)
        RELEASE.write_plan(path, expired)
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
            project="relay", sha=self.sha, approved_by="tester",
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
            ticket_workdir=[], skip_optional_tests=True,
        )
        runtime = {
            "evidence": {"path": str(self.root / "runtime")},
            "plan_sha256": "2" * 64,
        }
        concurrency = {"action": "apply", "plan": {"approval_sha256": "3" * 64}}
        cli = {"action": "reuse", "evidence": {"status": "pass"}}
        reuse = {"action": "reuse", "evidence": {"status": "pass"}}
        unloaded = self.retired_runtime(self.root / "home")
        loaded = json.loads(json.dumps(unloaded))
        loaded["services"][0]["loaded"] = True
        loaded["action"] = "apply"
        order = []
        with (
            mock.patch.dict(os.environ, {"FACTORY_KIT_CERTIFICATION_NETWORK_REVIEWED": "1"}),
            mock.patch.object(RELEASE, "clean_identity", side_effect=[
                (self.sha, "e" * 40, str(repo)),
                ("f" * 40, "1" * 40, str(self.product)),
                ("f" * 40, "1" * 40, str(self.product)),
                (self.sha, "e" * 40, str(repo)),
                ("f" * 40, "1" * 40, str(self.product)),
                ("f" * 40, "1" * 40, str(self.product)),
            ]),
            mock.patch.object(RELEASE, "run") as run,
            mock.patch.object(
                RELEASE, "release_preflight", side_effect=lambda *_args: order.append("preflight"),
            ),
            mock.patch.object(RELEASE, "contract", return_value="2.0.0"),
            mock.patch.object(
                RELEASE, "prepare_runtime",
                side_effect=lambda *_args: order.append("runtime") or runtime,
            ),
            mock.patch.object(RELEASE, "validate_product_runtime_contract"),
            mock.patch.object(RELEASE, "validate_optional_test_request"),
            mock.patch.object(RELEASE, "prepare_product_runtime"),
            mock.patch.object(RELEASE, "prepare_controller", return_value=self.plan["identity"]["controller"]),
            mock.patch.object(RELEASE, "launcher_plan", return_value=self.plan["children"]["launcher"]),
            mock.patch.object(
                RELEASE, "retired_runtime_plan",
                side_effect=[unloaded, loaded],
            ),
            mock.patch.object(RELEASE, "capacity", return_value=2),
            mock.patch.object(
                RELEASE, "child_plan", side_effect=[(concurrency, cli), (reuse, cli)],
            ),
        ):
            plan = RELEASE.setup(args)
            retired_plan = RELEASE.setup(args)
        self.assertEqual(plan["stage"], "prerequisites")
        self.assertEqual(plan["children"]["provider_concurrency"], concurrency)
        self.assertEqual(retired_plan["stage"], "prerequisites")
        self.assertEqual(retired_plan["children"]["provider_concurrency"], reuse)
        self.assertTrue(retired_plan["children"]["retired_runtime"]["services"][0]["loaded"])
        self.assertIsNone(retired_plan["children"]["host_cutover"])
        self.assertFalse(RELEASE.reservation_path(self.kits).exists())
        self.assertTrue(plan["request"]["skip_optional_tests"])
        self.assertTrue(RELEASE.plan_request(plan, self.kits).skip_optional_tests)
        self.assertEqual(order, ["runtime", "preflight", "runtime", "preflight"])
        self.assertNotIn(
            "FACTORY_KIT_CERTIFICATION_NETWORK_REVIEWED",
            run.call_args_list[0].kwargs["environment"],
        )
        RELEASE.validate_plan(plan)
        RELEASE.validate_plan(retired_plan)

    def test_release_preflight_uses_the_prepared_runtime(self) -> None:
        runtime = self.root / "runtime/bin"
        completed = subprocess.CompletedProcess(
            [], 0, json.dumps({"status": "pass"}), "",
        )
        with mock.patch.object(
            RELEASE.subprocess, "run", return_value=completed,
        ) as invoked:
            report = RELEASE.release_preflight(
                self.root / "factory-kit.sh", self.kits, runtime,
                "relay", self.product, self.sha,
            )
        self.assertEqual(report["status"], "pass")
        environment = invoked.call_args.kwargs["env"]
        self.assertTrue(environment["PATH"].startswith(str(runtime) + ":"))

    def test_undeclared_optional_skip_refuses_before_install(self) -> None:
        repo = self.root / "factory"
        repo.mkdir()
        (self.product / "factory/KIT_PIN").write_text(self.sha + "\n")
        args = argparse.Namespace(
            project="relay", product=self.product, repo=repo, sha=self.sha,
            kits_root=self.kits, profile="openai-priority-v1", operator_id="tester",
            runtime_bin=None, claude_bin=None, codex_bin=None, cursor_bin=None,
            ticket_workdir=[], skip_optional_tests=True,
        )
        with (
            mock.patch.object(RELEASE, "clean_identity", side_effect=[
                (self.sha, "e" * 40, str(repo)),
                ("f" * 40, "1" * 40, str(self.product)),
            ]),
            mock.patch.object(RELEASE, "validate_product_runtime_contract"),
            mock.patch.object(RELEASE, "run") as install,
            mock.patch.object(RELEASE, "prepare_runtime") as runtime,
            self.assertRaisesRegex(RELEASE.ReleaseError, "optional-test"),
        ):
            RELEASE.setup(args)
        install.assert_not_called()
        runtime.assert_not_called()

    def test_activation_validation_binds_main_before_hydrating_ticket_evidence(self) -> None:
        order = []
        validator = ACTIVATION.Validator(
            self.product, self.sha, ROOT / "scripts", str(self.root / "origin"), "",
        )
        with (
            mock.patch.object(
                ACTIVATION, "run_git_remote",
                side_effect=lambda *_args, **_kwargs: order.append("remote") or subprocess.CompletedProcess(
                    [], 0, f"{self.sha}\trefs/heads/main\n", "",
                ),
            ),
            mock.patch.object(
                ACTIVATION, "hydrate",
                side_effect=lambda _product, _origin: order.append("hydrate") or 1,
            ),
            mock.patch.object(
                validator, "checked",
                side_effect=lambda *args: order.append("head") or self.sha,
            ),
            mock.patch.object(
                validator, "ticket_ids",
                side_effect=lambda: order.append("tickets") or ({"T-1"}, {}),
            ),
            mock.patch.object(
                validator, "validate_ticket",
                side_effect=lambda *_args: order.append("terminal"),
            ),
        ):
            blockers, _, hydrated = validator.run()
        self.assertEqual(blockers, [])
        self.assertEqual(hydrated, 1)
        self.assertEqual(order, ["head", "remote", "hydrate", "tickets", "terminal"])

    def test_activation_uses_the_exact_per_ticket_migration_source(self) -> None:
        ticket = "T-1"
        source = "c" * 40
        head = "d" * 40
        validator = ACTIVATION.Validator(
            self.product, self.sha, ROOT / "scripts", str(self.root / "origin"), "",
        )
        entry = {
            "branch": f"ticket/{ticket}", "head": head,
            "source_kit_sha": source, "state": "Ready", "ticket": ticket,
        }
        validator.authorization = {
            "repository": "example/product",
            "schema": "nysa.software-factory.inflight-release-authorization/v2",
            "source_kit_sha": "a" * 40,
            "target_kit_sha": self.sha,
            "tickets": [entry],
        }
        validator.authorized = {ticket: entry}
        validator.authorization_loaded = True
        plan = {
            "created_at": "1970-01-01T00:00:00Z", "kit_sha": source,
            "resolution": {}, "schema": "ticket-model-route-plan/v1",
            "ticket": ticket,
        }
        manager = mock.Mock()
        with (
            mock.patch.object(
                validator, "git",
                return_value=subprocess.CompletedProcess(
                    [], 0, json.dumps(plan), "",
                ),
            ),
            mock.patch.object(
                validator, "load_migration_policy",
                return_value=(manager, {}, {}, {}),
            ),
        ):
            validator.authorize_inflight(
                ticket, f"ticket/{ticket}", head,
                f"refs/remotes/origin/ticket/{ticket}", "Ready", source,
            )
        self.assertEqual(validator.used_authorizations, {ticket})
        manager._validate_pin.assert_called_once()

    def test_activation_main_check_ignores_repository_transport_rewrites(self) -> None:
        product = self.root / "trust-product"
        trusted = self.root / "trusted.git"
        redirected = self.root / "redirected.git"

        def git(root: Path, *arguments: str) -> str:
            return subprocess.run(
                ["git", "-C", str(root), *arguments], text=True,
                capture_output=True, check=True,
            ).stdout.strip()

        subprocess.run(
            ["git", "init", "--bare", "-q", str(trusted)], check=True,
        )
        subprocess.run(
            ["git", "init", "--bare", "-q", str(redirected)], check=True,
        )
        subprocess.run(
            ["git", "init", "-q", "-b", "main", str(product)], check=True,
        )
        git(product, "config", "user.name", "Test")
        git(product, "config", "user.email", "test@example.invalid")
        (product / "factory").mkdir()
        (product / "factory/PROJECT.env").write_text(
            "GH_REPO=example/product\n", encoding="utf-8",
        )
        git(product, "add", ".")
        git(product, "commit", "-qm", "protected main")
        head = git(product, "rev-parse", "HEAD")
        git(product, "push", "-q", str(trusted), "HEAD:main")
        git(product, "config", f"url.{redirected}.insteadOf", str(trusted))
        validator = ACTIVATION.Validator(
            product, self.sha, ROOT / "scripts", str(trusted), "",
        )
        with (
            mock.patch.object(ACTIVATION, "hydrate", return_value=0),
            mock.patch.object(validator, "ticket_ids", return_value=(set(), {})),
        ):
            blockers, _, _ = validator.run()
        self.assertEqual(head, git(product, "rev-parse", "HEAD"))
        self.assertEqual(blockers, [])

    def test_hardened_git_auth_is_github_host_scoped(self) -> None:
        home = self.root / "auth-home"
        helper_root = self.root / "Cellar/gh/1.0/bin"
        helper_root.mkdir(parents=True)
        helper = helper_root / "gh"
        helper.write_text("#!/bin/sh\n", encoding="utf-8")
        helper.chmod(0o700)
        link_root = self.root / "fixed-bin"
        link_root.mkdir()
        (link_root / "gh").symlink_to(helper)
        config = home / ".config/gh"
        config.mkdir(parents=True)
        (config / "hosts.yml").write_text("github.com: {}\n", encoding="utf-8")
        (config / "hosts.yml").chmod(0o600)
        with (
            mock.patch.object(
                HISTORY, "GITHUB_CLI_CANDIDATES",
                (link_root / "gh", self.root / "missing-gh"),
            ),
            mock.patch.dict(os.environ, {"HOME": str(home)}),
        ):
            auth = HISTORY.github_auth("https://github.com/example/private.git")
        self.assertEqual(auth, (str(helper), str(config)))
        with mock.patch.object(
            HISTORY, "GITHUB_CLI_CANDIDATES", (link_root / "gh",),
        ), mock.patch.dict(os.environ, {"HOME": str(home)}):
            self.assertIsNone(
                HISTORY.github_auth("https://github.example/private.git"),
            )
            (config / "hosts.yml").chmod(0o644)
            self.assertIsNone(
                HISTORY.github_auth("https://github.com/example/private.git"),
            )
        assert auth is not None
        command = HISTORY._git_command(
            None, "ls-remote", "https://github.com/example/private.git",
            auth=auth,
        )
        self.assertIn("credential.helper=", command)
        self.assertIn(
            "credential.https://github.com.helper="
            f"!{helper} auth git-credential",
            command,
        )
        self.assertFalse(any(
            value.startswith("credential.https://")
            and not value.startswith("credential.https://github.com")
            for value in command
        ))
        environment = HISTORY._git_environment(auth=auth)
        self.assertEqual(environment["GH_CONFIG_DIR"], auth[1])

    def test_historical_transport_environment_and_descriptor_are_strict(self) -> None:
        local = self.root / "local-origin.git"
        local.mkdir()
        for value in (
            str(local), f"file://{local}",
            "https://github.com/example/product.git",
            "ssh://git@github.com/example/product.git",
            "git@github.com:example/product.git",
        ):
            with self.subTest(accepted=value.split(":", 1)[0]):
                self.assertTrue(HISTORY._transport(value))
        for value in (
            "", "relative/origin", "http://example.invalid/product.git",
            "file://example.invalid/private/tmp/product.git", "ext::helper",
            "https://example.invalid/product.git\nextra",
        ):
            with self.subTest(rejected=value.split(":", 1)[0]), self.assertRaises(
                HISTORY.HistoricalObjectError,
            ):
                HISTORY._transport(value)

        with mock.patch.dict(os.environ, {
            "GIT_CONFIG_GLOBAL": str(self.root / "poisoned-config"),
            "GIT_OBJECT_DIRECTORY": str(self.root / "poisoned-objects"),
            "GIT_SSH_COMMAND": "false",
        }, clear=False):
            environment = HISTORY._git_environment()
        self.assertEqual(environment["GIT_CONFIG_GLOBAL"], os.devnull)
        self.assertEqual(
            environment["GIT_SSH_COMMAND"],
            "/usr/bin/ssh -F /dev/null -oBatchMode=yes",
        )
        self.assertNotIn("GIT_OBJECT_DIRECTORY", environment)
        with self.assertRaisesRegex(
            HISTORY.HistoricalObjectError,
            "historical Git environment override is unsafe",
        ):
            HISTORY._git_environment({"GIT_CONFIG_GLOBAL": os.devnull})

        descriptor = self.root / "descriptor-product/factory/PROJECT.env"
        descriptor.parent.mkdir(parents=True)
        descriptor.write_text(
            "GH_REPO=example/one\nGH_REPO=example/two\n", encoding="utf-8",
        )
        with self.assertRaisesRegex(
            HISTORY.HistoricalObjectError,
            "historical product repository is ambiguous",
        ):
            HISTORY._repository(descriptor.parents[1])
        descriptor.unlink()
        descriptor.symlink_to(self.root / "outside-descriptor")
        with self.assertRaisesRegex(
            HISTORY.HistoricalObjectError,
            "historical product descriptor is unsafe",
        ):
            HISTORY._repository(descriptor.parents[1])

    def test_historical_evidence_inventories_and_bytes_are_bounded(self) -> None:
        product = self.root / "history-limits"
        migrations = product / "factory/migrations"
        migrations.mkdir(parents=True)
        (migrations / "one.json").write_text("{}\n", encoding="utf-8")
        (migrations / "two.json").write_text("{}\n", encoding="utf-8")
        with mock.patch.object(HISTORY, "MAX_EVIDENCE_FILES", 1), self.assertRaisesRegex(
            HISTORY.HistoricalObjectError,
            "historical migration inventory is too large",
        ):
            HISTORY.hydrate(product, str(self.root))
        (migrations / "two.json").unlink()
        (migrations / "one.json").unlink()
        (migrations / "one.json").symlink_to(self.root / "outside-migration")
        with self.assertRaisesRegex(
            HISTORY.HistoricalObjectError,
            "historical object record is unsafe",
        ):
            HISTORY.hydrate(product, str(self.root))

        objects = {f"{value:040x}" for value in range(HISTORY.MAX_OBJECTS + 1)}
        with (
            mock.patch.object(HISTORY, "commit_present", return_value=False),
            mock.patch.object(HISTORY, "_blob_present", return_value=False),
            mock.patch.object(HISTORY, "run_git_remote") as remote,
            self.assertRaisesRegex(
                HISTORY.HistoricalObjectError,
                "historical evidence object inventory is too large",
            ),
        ):
            HISTORY.fetch_objects(product, str(self.root), objects, set())
        remote.assert_not_called()

        attested = self.root / "attested-product"
        subprocess.run(
            ["git", "init", "-q", "-b", "main", str(attested)], check=True,
        )
        subprocess.run(
            ["git", "-C", str(attested), "config", "user.name", "Test"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(attested), "config", "user.email", "test@example.invalid"],
            check=True,
        )
        ticket = attested / "factory/tickets/T-1.md"
        ticket.parent.mkdir(parents=True)
        ticket.write_text("State: Done\n", encoding="utf-8")
        (attested / "factory/PROJECT.env").write_text(
            "GH_REPO=example/product\n", encoding="utf-8",
        )
        migration = attested / "factory/migrations/unrelated.json"
        migration.parent.mkdir()
        migration.write_text(
            json.dumps({"padding": "m" * 180}) + "\n", encoding="utf-8",
        )
        subprocess.run(["git", "-C", str(attested), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(attested), "commit", "-qm", "base"], check=True,
        )
        base = subprocess.check_output(
            ["git", "-C", str(attested), "rev-parse", "HEAD"], text=True,
        ).strip()
        bundle = attested / "factory/attestations/T-1/bundle.json"
        bundle.parent.mkdir(parents=True)
        bundle.write_text(json.dumps({
            "branch_head": base, "padding": "x" * 512,
        }) + "\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(attested), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(attested), "commit", "-qm", "attestation"],
            check=True,
        )
        with mock.patch.object(
            HISTORY, "MAX_TOTAL_EVIDENCE_BYTES", 700,
        ), self.assertRaisesRegex(
            HISTORY.HistoricalObjectError,
            "historical attestation evidence is too large",
        ):
            HISTORY.hydrate(attested, str(attested))

        ticket_budget = self.root / "done-ticket-budget"
        subprocess.run(
            ["git", "init", "-q", "-b", "main", str(ticket_budget)], check=True,
        )
        subprocess.run(
            ["git", "-C", str(ticket_budget), "config", "user.name", "Test"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(ticket_budget), "config", "user.email", "test@example.invalid"],
            check=True,
        )
        (ticket_budget / "factory/PROJECT.env").parent.mkdir(parents=True)
        (ticket_budget / "factory/PROJECT.env").write_text(
            "GH_REPO=example/product\n", encoding="utf-8",
        )
        for number in (1, 2):
            path = ticket_budget / f"factory/tickets/T-{number}.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("State: Done\n\n" + "x" * 100 + "\n", encoding="utf-8")
            evidence = ticket_budget / f"factory/attestations/T-{number}/bundle.json"
            evidence.parent.mkdir(parents=True)
            evidence.write_text("{}\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(ticket_budget), "add", "."], check=True,
        )
        subprocess.run(
            ["git", "-C", str(ticket_budget), "commit", "-qm", "done tickets"],
            check=True,
        )
        with (
            mock.patch.object(HISTORY, "MAX_TOTAL_EVIDENCE_BYTES", 150),
            mock.patch.object(HISTORY, "_json_at", return_value={}),
            self.assertRaisesRegex(
                HISTORY.HistoricalObjectError,
                "historical attestation evidence is too large",
            ),
        ):
            HISTORY.hydrate(ticket_budget, str(ticket_budget))

    def test_historical_object_type_and_size_are_verified_before_import(self) -> None:
        remote = self.root / "objects.git"
        publisher = self.root / "object-publisher"
        consumer = self.root / "object-consumer"
        subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
        subprocess.run(
            ["git", "init", "-q", "-b", "main", str(publisher)], check=True,
        )
        subprocess.run(
            ["git", "-C", str(publisher), "config", "user.name", "Test"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(publisher), "config", "user.email", "test@example.invalid"],
            check=True,
        )
        (publisher / "object.txt").write_text("object\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(publisher), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(publisher), "commit", "-qm", "object"], check=True,
        )
        commit = subprocess.check_output(
            ["git", "-C", str(publisher), "rev-parse", "HEAD"], text=True,
        ).strip()
        blob = subprocess.check_output(
            ["git", "-C", str(publisher), "rev-parse", "HEAD:object.txt"],
            text=True,
        ).strip()
        subprocess.run(
            ["git", "-C", str(publisher), "push", "-q", str(remote), "main"],
            check=True,
        )
        subprocess.run(["git", "init", "-q", str(consumer)], check=True)
        with self.assertRaisesRegex(
            HISTORY.HistoricalObjectError,
            "historical evidence object type is invalid",
        ):
            HISTORY.fetch_objects(consumer, str(remote), set(), {commit})
        self.assertFalse(HISTORY.commit_present(consumer, commit))
        with mock.patch.object(
            HISTORY, "MAX_OBJECT_BYTES", 0,
        ), self.assertRaisesRegex(
            HISTORY.HistoricalObjectError,
            "historical evidence object is too large",
        ):
            HISTORY.fetch_objects(consumer, str(remote), set(), {blob})
        self.assertFalse(HISTORY._blob_present(consumer, blob))

    def test_reconciliation_attestations_share_the_migration_byte_budget(self) -> None:
        product = self.root / "reconciliation-budget"
        migration = product / "factory/migrations/reconciliation.json"
        migration.parent.mkdir(parents=True)
        (product / "factory/PROJECT.env").write_text(
            "GH_REPO=example/product\n", encoding="utf-8",
        )
        migration.write_text(json.dumps({
            "adoption_pr": {"head": "1" * 40, "number": 1},
            "evidence_head": "1" * 40,
            "original_pr": {"head": "1" * 40, "number": 1},
            "repository": "example/product",
            "schema": "nysa.software-factory.protected-merge-reconciliation/v1",
        }) + "\n", encoding="utf-8")
        limit = migration.stat().st_size + 50

        def charged_json(
            _product: Path, _sha: str, _path: str, budget: list[int] | None = None,
        ) -> dict[str, object]:
            self.assertIsNotNone(budget)
            assert budget is not None
            budget[0] += 100
            if budget[0] > limit:
                raise HISTORY.HistoricalObjectError(
                    "historical attestation evidence is too large",
                )
            return {}

        with (
            mock.patch.object(HISTORY, "MAX_TOTAL_EVIDENCE_BYTES", limit),
            mock.patch.object(HISTORY, "commit_present", return_value=True),
            mock.patch.object(HISTORY, "fetch_objects"),
            mock.patch.object(HISTORY, "_json_at", side_effect=charged_json),
            self.assertRaisesRegex(
                HISTORY.HistoricalObjectError,
                "historical attestation evidence is too large",
            ),
        ):
            HISTORY.hydrate(product, str(self.root))

    def test_activation_requires_an_exact_protected_main_record(self) -> None:
        product = self.root / "main-record-product"
        (product / "factory").mkdir(parents=True)
        (product / "factory/PROJECT.env").write_text(
            "GH_REPO=example/product\n", encoding="utf-8",
        )
        subprocess.run(
            ["git", "init", "-q", "-b", "main", str(product)], check=True,
        )
        subprocess.run(
            ["git", "-C", str(product), "config", "user.name", "Test"], check=True,
        )
        subprocess.run(
            ["git", "-C", str(product), "config", "user.email", "test@example.invalid"],
            check=True,
        )
        subprocess.run(["git", "-C", str(product), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(product), "commit", "-qm", "main"], check=True,
        )
        head = subprocess.check_output(
            ["git", "-C", str(product), "rev-parse", "HEAD"], text=True,
        ).strip()
        validator = ACTIVATION.Validator(
            product, self.sha, ROOT / "scripts", str(self.root), "",
        )
        valid = subprocess.CompletedProcess(
            [], 0, f"{head}\trefs/heads/main\n", "",
        )
        with mock.patch.object(ACTIVATION, "run_git_remote", return_value=valid):
            self.assertEqual(validator.remote_main(), head)
        invalid = (
            subprocess.CompletedProcess([], 0, f"{head}\trefs/heads/other\n", ""),
            subprocess.CompletedProcess(
                [], 0,
                f"{head}\trefs/heads/main\n{head}\trefs/heads/other\n", "",
            ),
            subprocess.CompletedProcess([], 0, f"{head}\n", ""),
            subprocess.CompletedProcess([], 1, "", "unavailable"),
        )
        for response in invalid:
            with self.subTest(output_fields=len(response.stdout.split())), mock.patch.object(
                ACTIVATION, "run_git_remote", return_value=response,
            ):
                blockers, _, _ = validator.run()
                self.assertEqual(blockers, [{
                    "reason_code": "activation_product_not_main",
                    "scope": "activation",
                }])

    def test_blocked_preflight_stops_before_certification_or_pause(self) -> None:
        repo = self.root / "factory"
        repo.mkdir()
        (self.product / "factory/KIT_PIN").write_text(self.sha + "\n")
        args = argparse.Namespace(
            project="relay", product=self.product, repo=repo, sha=self.sha,
            kits_root=self.kits, profile="openai-priority-v1", operator_id="tester",
            runtime_bin=None, claude_bin=None, codex_bin=None, cursor_bin=None,
            ticket_workdir=[], skip_optional_tests=False,
        )
        runtime = {
            "evidence": {"path": str(self.root / "runtime")},
            "plan_sha256": "2" * 64,
        }
        with (
            mock.patch.object(RELEASE, "clean_identity", side_effect=[
                (self.sha, "e" * 40, str(repo)),
                ("f" * 40, "1" * 40, str(self.product)),
            ]),
            mock.patch.object(RELEASE, "run") as run,
            mock.patch.object(RELEASE, "contract", return_value="2.0.0"),
            mock.patch.object(RELEASE, "prepare_runtime", return_value=runtime),
            mock.patch.object(RELEASE, "validate_product_runtime_contract"),
            mock.patch.object(
                RELEASE, "release_preflight",
                side_effect=RELEASE.ReleaseError("activation readiness blocked"),
            ),
            mock.patch.object(RELEASE, "prepare_product_runtime") as product_runtime,
            mock.patch.object(RELEASE, "prepare_controller") as controller,
            mock.patch.object(RELEASE, "child_plan") as child_plan,
            mock.patch.object(RELEASE, "find_receipt") as find_receipt,
        ):
            with self.assertRaisesRegex(RELEASE.ReleaseError, "readiness blocked"):
                RELEASE.setup(args)
        self.assertEqual(run.call_count, 1)
        product_runtime.assert_not_called()
        controller.assert_not_called()
        child_plan.assert_not_called()
        find_receipt.assert_not_called()
        self.assertFalse((self.product / "factory/MAINTENANCE").exists())
        self.assertFalse((self.kits / "receipts").exists())

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
            ticket_workdir=[], skip_optional_tests=True,
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
                ("f" * 40, "1" * 40, str(self.product)),
            ]),
            mock.patch.object(RELEASE, "run") as run,
            mock.patch.object(RELEASE, "release_preflight"),
            mock.patch.object(RELEASE, "contract", return_value="2.0.0"),
            mock.patch.object(RELEASE, "prepare_runtime", return_value=runtime),
            mock.patch.object(RELEASE, "validate_product_runtime_contract"),
            mock.patch.object(RELEASE, "validate_optional_test_request"),
            mock.patch.object(RELEASE, "prepare_product_runtime"),
            mock.patch.object(RELEASE, "prepare_controller", return_value=self.plan["identity"]["controller"]),
            mock.patch.object(RELEASE, "launcher_plan", return_value=self.plan["children"]["launcher"]),
            mock.patch.object(
                RELEASE, "retired_runtime_plan",
                return_value=self.retired_runtime(self.root / "home"),
            ),
            mock.patch.object(RELEASE, "capacity", return_value=1),
            mock.patch.object(RELEASE, "child_plan", return_value=(reuse, reuse)),
            mock.patch.object(RELEASE, "find_receipt", return_value=(
                receipt_path, {"receipt_id": "c" * 64},
            )),
            mock.patch.object(RELEASE, "profile_plan", return_value=model),
        ):
            plan = RELEASE.setup(args)
        self.assertEqual(plan["stage"], "activation")
        self.assertEqual(plan["children"]["receipt"]["sha256"], RELEASE.file_digest(receipt_path))
        certify = next(
            call.args[0] for call in run.call_args_list if "certify" in call.args[0]
        )
        self.assertEqual(certify[-1], "--skip-optional-tests")
        RELEASE.validate_plan(plan)

    def test_test_mode_release_requires_an_explicit_isolated_home(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"FACTORY_KIT_TEST_MODE": "1", "FACTORY_RELEASE_TEST_HOME": ""},
            clear=False,
        ):
            with self.assertRaisesRegex(RELEASE.ReleaseError, "isolated release test home"):
                RELEASE.account_home()
        real_home = Path(RELEASE.pwd.getpwuid(os.getuid()).pw_dir).resolve()
        with mock.patch.dict(
            os.environ,
            {
                "FACTORY_KIT_TEST_MODE": "1",
                "FACTORY_RELEASE_TEST_HOME": str(real_home),
            },
            clear=False,
        ):
            with self.assertRaisesRegex(RELEASE.ReleaseError, "real account home"):
                RELEASE.account_home()
        isolated = self.root / "isolated-home"
        isolated.mkdir(mode=0o700)
        with mock.patch.dict(
            os.environ,
            {
                "FACTORY_KIT_TEST_MODE": "1",
                "FACTORY_RELEASE_TEST_HOME": str(isolated),
            },
            clear=False,
        ):
            RELEASE.require_test_layout(isolated / ".factory/kits")
            with self.assertRaisesRegex(RELEASE.ReleaseError, "isolated test home"):
                RELEASE.require_test_layout(self.root / "production-kits")

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
            "--skip-optional-tests",
            "--ticket-workdir", "T-1", str(self.root / "worktree"),
        ]
        result = subprocess.run(
            command, text=True, capture_output=True, check=False,
            env={**os.environ, "FACTORY_KITS_ROOT": str(self.root / "state")},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        forwarded = json.loads(result.stdout)
        self.assertEqual(forwarded[2:4], ["setup", "--project"])
        self.assertIn("--skip-optional-tests", forwarded)
        self.assertEqual(forwarded[-3:], ["--ticket-workdir", "T-1", str(self.root / "worktree")])

    def test_public_setup_command_accepts_no_migration_tickets(self) -> None:
        copy = self.root / "wrapper-no-tickets"
        (copy / "scripts/lib").mkdir(parents=True)
        shutil.copy2(ROOT / "scripts/factory-kit.sh", copy / "scripts/factory-kit.sh")
        shutil.copy2(
            ROOT / "scripts/lib/dispatch-leases.sh",
            copy / "scripts/lib/dispatch-leases.sh",
        )
        (copy / "scripts/release-transaction.py").write_text(
            "import json,sys\nprint(json.dumps(sys.argv[1:]))\n"
        )
        result = subprocess.run(
            [
                "bash", str(copy / "scripts/factory-kit.sh"), "release", "setup",
                "--project", "relay", "--product", str(self.product),
                "--sha", self.sha, "--repo", str(self.root / "repo"),
                "--profile", "openai-priority-v1", "--operator-id", "tester",
            ],
            text=True, capture_output=True, check=False,
            env={**os.environ, "FACTORY_KITS_ROOT": str(self.root / "state")},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("--ticket-workdir", json.loads(result.stdout))

    def test_public_resume_and_abort_require_no_external_hash(self) -> None:
        state = self.root / "public-release-state"
        state.mkdir(mode=0o700)
        environment = {**os.environ, "FACTORY_KITS_ROOT": str(state)}
        for action in ("resume", "abort"):
            with self.subTest(action=action):
                command = [
                    "bash", str(ROOT / "scripts/factory-kit.sh"), "release", action,
                    "--project", "relay", "--sha", self.sha,
                    "--approved-by", "tester",
                ]
                accepted = subprocess.run(
                    command, text=True, capture_output=True, check=False,
                    env=environment,
                )
                self.assertEqual(accepted.returncode, 1)
                self.assertIn("trusted install manifest", accepted.stderr)
                legacy = subprocess.run(
                    [*command, "--approve-hash", "9" * 64],
                    text=True, capture_output=True, check=False,
                    env=environment,
                )
                self.assertEqual(legacy.returncode, 2)
                self.assertIn("Usage:", legacy.stderr)
                skipped = subprocess.run(
                    [*command, "--skip-optional-tests"],
                    text=True, capture_output=True, check=False,
                    env=environment,
                )
                self.assertEqual(skipped.returncode, 2)
                self.assertIn("Usage:", skipped.stderr)
                helper = subprocess.run(
                    [
                        sys.executable, str(ROOT / "scripts/release-transaction.py"),
                        "--kits-root", str(state), action, "--project", "relay",
                        "--sha", self.sha, "--approved-by", "tester",
                    ],
                    text=True, capture_output=True, check=False,
                )
                self.assertEqual(helper.returncode, 2)
                self.assertEqual(json.loads(helper.stdout)["status"], "error")

        qualification = [
            "bash", str(ROOT / "scripts/factory-kit.sh"), "qualification", "resume",
            "--project", "relay", "--sha", self.sha, "--approved-by", "tester",
        ]
        accepted = subprocess.run(
            qualification, text=True, capture_output=True, check=False,
            env=environment,
        )
        self.assertEqual(accepted.returncode, 1)
        self.assertIn("trusted install manifest", accepted.stderr)
        legacy = subprocess.run(
            [*qualification, "--approve-hash", "9" * 64],
            text=True, capture_output=True, check=False, env=environment,
        )
        self.assertEqual(legacy.returncode, 2)
        self.assertIn("Usage:", legacy.stderr)

    def test_qualification_resume_executes_sealed_helper_in_allowlisted_environment(self) -> None:
        home = self.root / "resume-home"
        kits = home / ".factory/kits"
        manifests = kits / "manifests"
        releases = kits / "releases"
        for directory in (home, home / ".factory", kits, manifests, releases):
            directory.mkdir(mode=0o700, exist_ok=True)
        origin = self.root / "resume-origin"
        origin.mkdir()
        source = self.root / "resume-source"
        (source / "scripts").mkdir(parents=True)
        (source / "factory-contract.json").write_text(
            '{"contract_version":"2.0.0"}\n', encoding="utf-8",
        )
        (source / "scripts/release-transaction.py").write_text(
            "import json,os,sys\n"
            "print(json.dumps({'arguments':sys.argv[1:],'environment':dict(os.environ)}))\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "init", "-q", "-b", "main", str(source)], check=True)
        subprocess.run(["git", "-C", str(source), "add", "."], check=True)
        subprocess.run([
            "git", "-C", str(source), "-c", "user.name=Factory Test", "-c",
            "user.email=factory@example.invalid", "commit", "-qm", "sealed",
        ], check=True)
        sha = subprocess.run(
            ["git", "-C", str(source), "rev-parse", "HEAD"], capture_output=True,
            text=True, check=True,
        ).stdout.strip()
        tree = subprocess.run(
            ["git", "-C", str(source), "rev-parse", "HEAD^{tree}"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        release = releases / sha
        shutil.copytree(source, release, ignore=shutil.ignore_patterns(".git"))
        for path in (release, *release.rglob("*")):
            path.chmod(path.stat().st_mode & ~0o222)
        manifest = manifests / f"{sha}.json"
        manifest.write_text(json.dumps({
            "canonical_origin": str(origin), "created_at": "2026-08-23T00:00:00Z",
            "git_tree": tree, "kit_sha": sha, "schema_version": 1,
            "sealed_release_path": str(release),
        }) + "\n", encoding="utf-8")
        manifest.chmod(0o600)
        environment = {
            **os.environ, "BASH_ENV": "/attacker/bash-env", "ENV": "/attacker/env",
            "FACTORY_KIT_CANONICAL_ORIGIN": str(origin),
            "FACTORY_KIT_TEST_MODE": "1", "FACTORY_RELEASE_TEST_HOME": str(home),
            "FACTORY_KITS_ROOT": str(kits), "GH_CONFIG_DIR": "/attacker/gh",
            "GH_HOST": "attacker.invalid", "GIT_DIR": "/attacker/git",
            "GIT_REPLACE_REF_BASE": "refs/attacker/", "HTTPS_PROXY": "attacker.invalid",
            "PS4": "attacker", "XDG_CONFIG_HOME": "/attacker/xdg",
        }
        resumed = subprocess.run(
            ["bash", str(ROOT / "scripts/factory-kit.sh"), "qualification", "resume",
             "--project", "relay", "--sha", sha, "--approved-by", "tester"],
            capture_output=True, text=True, env=environment, check=False,
        )
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        evidence = json.loads(resumed.stdout)
        self.assertIn("qualification-resume", evidence["arguments"])
        self.assertEqual(evidence["environment"]["PATH"], "/usr/bin:/bin")
        self.assertEqual(evidence["environment"]["GIT_NO_REPLACE_OBJECTS"], "1")
        self.assertEqual(evidence["environment"]["GIT_CONFIG_GLOBAL"], "/dev/null")
        for variable in (
            "BASH_ENV", "ENV", "GH_CONFIG_DIR", "GH_HOST", "GIT_DIR",
            "GIT_REPLACE_REF_BASE", "HTTPS_PROXY", "PS4", "XDG_CONFIG_HOME",
        ):
            self.assertNotIn(variable, evidence["environment"])
        target_sha = "a" * 40
        missing_target = subprocess.run(
            ["bash", str(ROOT / "scripts/factory-kit.sh"), "qualification", "resume",
             "--project", "relay", "--sha", target_sha, "--approved-by", "tester",
             "--repair-sha", sha],
            capture_output=True, text=True, env=environment, check=False,
        )
        self.assertEqual(missing_target.returncode, 1)
        self.assertIn("trusted install manifest", missing_target.stderr)
        target_release = releases / target_sha
        shutil.copytree(release, target_release)
        target_manifest = manifests / f"{target_sha}.json"
        target_manifest.write_text(json.dumps({
            "canonical_origin": str(origin), "created_at": "2026-08-23T00:00:00Z",
            "git_tree": tree, "kit_sha": target_sha, "schema_version": 1,
            "sealed_release_path": str(target_release),
        }) + "\n", encoding="utf-8")
        target_manifest.chmod(0o600)
        repaired = subprocess.run(
            ["bash", str(ROOT / "scripts/factory-kit.sh"), "qualification", "resume",
             "--project", "relay", "--sha", target_sha, "--approved-by", "tester",
             "--repair-sha", sha],
            capture_output=True, text=True, env=environment, check=False,
        )
        self.assertEqual(repaired.returncode, 0, repaired.stderr)
        arguments = json.loads(repaired.stdout)["arguments"]
        self.assertEqual(arguments[-2:], ["--repair-sha", sha])
        self.assertIn(target_sha, arguments)

    def test_protected_check_fixture_matches_slurped_check_run_shape(self) -> None:
        result = subprocess.run(
            [
                str(ROOT / "ci/fixtures/gh-protected-checks"), "api",
                "--paginate", "--slurp",
                f"repos/nysa-company/software-factory/commits/{self.sha}/check-runs?per_page=100",
            ],
            text=True, capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        pages = json.loads(result.stdout)
        self.assertEqual(pages[0]["check_runs"][0]["name"], "ci")
        self.assertEqual(pages[0]["check_runs"][0]["app"]["id"], 7)

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
            "host_cutover": None,
            "launcher": self.plan["children"]["launcher"],
            "provider_cli": {"action": "apply", "plan": {"approval_sha256": "6" * 64}},
            "provider_concurrency": {"action": "reuse"},
            "retired_runtime": self.retired_runtime(self.root / "home"),
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
            project="relay", sha=self.sha, approved_by="tester",
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
        runtime_journal = (
            self.kits.parent / "project-runtimes/relay/runtime-pin-journal.json"
        )
        RELEASE.atomic_json(runtime_journal, {
            "plan": {"approval_sha256": "2" * 64}, "status": "completed",
        })
        product_identity = ("f" * 40, "1" * 40, str(self.root / "product-origin"))
        home = self.root / "home"
        launcher = home / ".factory/bin/factory-launch"
        launcher.parent.mkdir(parents=True)
        launcher.write_text("launcher")
        launcher.chmod(0o700)
        plan = json.loads(json.dumps(self.plan))
        plan["children"]["launcher"] = {
            "action": "reuse", "path": str(launcher),
            "sha256": RELEASE.file_digest(launcher),
        }
        with (
            mock.patch.object(RELEASE.Path, "home", return_value=home),
            mock.patch.object(RELEASE, "clean_identity", return_value=product_identity),
            mock.patch.object(RELEASE, "contract", return_value="2.0.0"),
            mock.patch.object(RELEASE, "validate_product_runtime_contract"),
            mock.patch.object(RELEASE, "run_json", return_value={"status": "pass"}),
        ):
            RELEASE.validate_live_basis(self.kits, plan)
        drifted = ("f" * 40, "9" * 40, str(self.root / "product-origin"))
        with (
            mock.patch.object(RELEASE.Path, "home", return_value=home),
            mock.patch.object(RELEASE, "clean_identity", return_value=drifted),
            mock.patch.object(RELEASE, "contract", return_value="2.0.0"),
        ):
            with self.assertRaisesRegex(RELEASE.ReleaseError, "product changed"):
                RELEASE.validate_live_basis(self.kits, plan)
        value = json.loads(runtime_journal.read_text())
        value["plan"]["approval_sha256"] = "8" * 64
        runtime_journal.write_text(json.dumps(value))
        runtime_journal.chmod(0o600)
        with (
            mock.patch.object(RELEASE.Path, "home", return_value=home),
            mock.patch.object(RELEASE, "clean_identity", return_value=product_identity),
            mock.patch.object(RELEASE, "contract", return_value="2.0.0"),
            mock.patch.object(RELEASE, "validate_product_runtime_contract"),
        ):
            with self.assertRaisesRegex(RELEASE.ReleaseError, "runtime changed"):
                RELEASE.validate_live_basis(self.kits, plan)

    def test_project_runtime_is_outside_symlink_free_kit_state(self) -> None:
        runtime = RELEASE.project_runtime_root(self.kits, "relay")
        self.assertEqual(runtime, self.kits.parent / "project-runtimes/relay")
        self.assertNotEqual(runtime.parents[1], self.kits)

    def test_secure_create_keeps_every_new_state_parent_owner_only(self) -> None:
        nested = self.root / "fresh-home/.factory/kits"
        RELEASE.secure_directory(nested, create=True)
        for path in (nested, nested.parent, nested.parent.parent):
            self.assertEqual(path.stat().st_mode & 0o777, 0o700)

    def test_sealed_profile_preview_needs_no_qualification_manifest(self) -> None:
        model = RELEASE.profile_plan(
            ROOT, self.root / "profile-state", "relay", "openai-priority-v1",
        )
        self.assertEqual(model["profile_id"], "openai-priority-v1")
        self.assertRegex(model["profile_hash"], r"^[0-9a-f]{64}$")
        self.assertNotIn("QUALIFICATION", json.dumps(model))

    def test_ticket_inventory_binds_committed_blob_and_state(self) -> None:
        tickets = self.product / "factory/tickets"
        tickets.mkdir()
        (tickets / "T-126.md").write_text("# T-126\n\nState: Ready\n")
        (tickets / "T-126-bundle.md").write_text("# T-126 evidence\n")
        with mock.patch.object(RELEASE, "git", return_value="7" * 40):
            inventory = RELEASE.ticket_inventory(self.product)
        self.assertEqual(inventory, [{
            "blob": "7" * 40, "state": "Ready", "ticket": "T-126",
        }])
        (tickets / "T-bad.md").write_text("State: Ready\n")
        with (
            mock.patch.object(RELEASE, "git", return_value="7" * 40),
            self.assertRaisesRegex(RELEASE.ReleaseError, "filename"),
        ):
            RELEASE.ticket_inventory(self.product)

    def test_release_prepares_only_ignored_physical_runtime_directories(self) -> None:
        RELEASE.prepare_product_runtime(self.product)
        for relative in ("factory/runs", "factory/.active-runs"):
            path = self.product / relative
            self.assertTrue(path.is_dir())
            self.assertFalse(path.is_symlink())
            self.assertEqual(path.stat().st_mode & 0o777, 0o700)
        target = self.root / "foreign-runs"
        target.mkdir()
        (self.product / "factory/runs").rmdir()
        (self.product / "factory/runs").symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(RELEASE.ReleaseError, "unsafe"):
            RELEASE.prepare_product_runtime(self.product)

    def test_release_refuses_uncommitted_ignore_authority(self) -> None:
        (self.product / ".gitignore").write_text(
            "factory/runs/\nfactory/.active-runs/\nfactory/.dispatch-leases/\n"
            "factory/.dispatch-leases.lock/\nfactory/.operator-clears/\n"
        )
        subprocess.run(["git", "-C", str(self.product), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(self.product), "add", ".gitignore"], check=True)
        subprocess.run([
            "git", "-C", str(self.product), "-c", "user.name=test", "-c",
            "user.email=test@local", "commit", "-qm", "fixture",
        ], check=True)
        with (self.product / ".git/info/exclude").open("a") as stream:
            stream.write("factory/operator-map.json\nfactory/.operator-map.lock\n")
        with self.assertRaisesRegex(RELEASE.ReleaseError, "ignore authority"):
            RELEASE.validate_product_runtime_contract(self.product)

    def test_release_refuses_negated_operator_ignore(self) -> None:
        (self.product / ".gitignore").write_text(
            "factory/runs/\nfactory/.active-runs/\nfactory/.dispatch-leases/\n"
            "factory/.dispatch-leases.lock/\nfactory/operator-map.json\n"
            "!factory/operator-map.json\nfactory/.operator-map.lock\n"
            "factory/.operator-clears/\n"
        )
        subprocess.run(["git", "-C", str(self.product), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(self.product), "add", ".gitignore"], check=True)
        subprocess.run([
            "git", "-C", str(self.product), "-c", "user.name=test", "-c",
            "user.email=test@local", "commit", "-qm", "fixture",
        ], check=True)
        with self.assertRaisesRegex(RELEASE.ReleaseError, "gitignored"):
            RELEASE.validate_product_runtime_contract(self.product)

    def test_setup_refuses_tracked_operator_projection_before_install(self) -> None:
        (self.product / ".gitignore").write_text(
            "factory/runs/\nfactory/.active-runs/\nfactory/.dispatch-leases/\n"
            "factory/.dispatch-leases.lock/\n"
            "factory/operator-map.json\nfactory/.operator-map.lock\n"
            "factory/.operator-clears/\n"
        )
        (self.product / "factory/KIT_PIN").write_text(self.sha + "\n")
        subprocess.run(["git", "-C", str(self.product), "init", "-q"], check=True)
        subprocess.run([
            "git", "-C", str(self.product), "add", ".gitignore", "factory/KIT_PIN",
        ], check=True)
        subprocess.run([
            "git", "-C", str(self.product), "add", "-f", "factory/operator-map.json",
        ], check=True)
        subprocess.run([
            "git", "-C", str(self.product), "-c", "user.name=test", "-c",
            "user.email=test@local", "commit", "-qm", "fixture",
        ], check=True)
        repo = self.root / "factory"
        repo.mkdir()
        args = argparse.Namespace(
            project="relay", product=self.product, repo=repo, sha=self.sha,
            kits_root=self.kits, profile="openai-priority-v1", operator_id="tester",
            runtime_bin=None, claude_bin=None, codex_bin=None, cursor_bin=None,
            ticket_workdir=[], skip_optional_tests=False,
        )
        with (
            mock.patch.object(RELEASE, "clean_identity", side_effect=[
                (self.sha, "e" * 40, str(repo)),
                ("f" * 40, "1" * 40, str(self.product)),
            ]),
            mock.patch.object(RELEASE, "run") as install,
            mock.patch.object(RELEASE, "prepare_runtime") as runtime,
            self.assertRaisesRegex(RELEASE.ReleaseError, "operator-map.json"),
        ):
            RELEASE.setup(args)
        install.assert_not_called()
        runtime.assert_not_called()
        self.assertFalse((self.product / "factory/runs").exists())
        self.assertFalse((self.product / "factory/.active-runs").exists())

    def test_release_refuses_missing_or_negated_dispatch_ignores(self) -> None:
        base = (
            "factory/runs/\nfactory/.active-runs/\nfactory/operator-map.json\n"
            "factory/.operator-map.lock\nfactory/.operator-clears/\n"
        )
        for rule in ("", "factory/.dispatch-leases/\n!factory/.dispatch-leases.lock/\n"):
            with self.subTest(rule=rule):
                (self.product / ".gitignore").write_text(base + rule)
                subprocess.run(["git", "-C", str(self.product), "init", "-q"], check=True)
                subprocess.run(["git", "-C", str(self.product), "add", ".gitignore"], check=True)
                subprocess.run([
                    "git", "-C", str(self.product), "-c", "user.name=test", "-c",
                    "user.email=test@local", "commit", "-qm", "fixture",
                ], check=True)
                with self.assertRaisesRegex(RELEASE.ReleaseError, "dispatch-leases"):
                    RELEASE.validate_product_runtime_contract(self.product)
                subprocess.run(["git", "-C", str(self.product), "reset", "--hard", "-q"], check=True)

    def test_release_refuses_tracked_or_unsafe_dispatch_state(self) -> None:
        ignore = (
            "factory/runs/\nfactory/.active-runs/\nfactory/operator-map.json\n"
            "factory/.operator-map.lock\nfactory/.operator-clears/\n"
            "factory/.dispatch-leases/\n"
            "factory/.dispatch-leases.lock/\n"
        )
        for index, (relative, expected) in enumerate((
            ("factory/.dispatch-leases/T-1.json", "factory/.dispatch-leases"),
            ("factory/.dispatch-leases.lock/owner", "factory/.dispatch-leases.lock"),
        )):
            product = self.root / f"tracked-dispatch-{index}"
            (product / "factory").mkdir(parents=True)
            with self.subTest(relative=relative):
                (product / ".gitignore").write_text(ignore)
                path = product / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("tracked\n")
                subprocess.run(["git", "-C", str(product), "init", "-q"], check=True)
                subprocess.run([
                    "git", "-C", str(product), "add", ".gitignore",
                ], check=True)
                subprocess.run([
                    "git", "-C", str(product), "add", "-f", relative,
                ], check=True)
                subprocess.run([
                    "git", "-C", str(product), "-c", "user.name=test", "-c",
                    "user.email=test@local", "commit", "-qm", "fixture",
                ], check=True)
                with self.assertRaisesRegex(
                    RELEASE.ReleaseError, re.escape(expected) + ".*untracked",
                ):
                    RELEASE.validate_product_runtime_contract(product)

    def test_release_refuses_existing_empty_dispatch_lock(self) -> None:
        (self.product / ".gitignore").write_text(
            "factory/runs/\nfactory/.active-runs/\nfactory/operator-map.json\n"
            "factory/.operator-map.lock\nfactory/.operator-clears/\n"
            "factory/.dispatch-leases/\n"
            "factory/.dispatch-leases.lock/\n"
        )
        lock = self.product / "factory/.dispatch-leases.lock"
        lock.mkdir(mode=0o700)
        subprocess.run(["git", "-C", str(self.product), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(self.product), "add", ".gitignore"], check=True)
        subprocess.run([
            "git", "-C", str(self.product), "-c", "user.name=test", "-c",
            "user.email=test@local", "commit", "-qm", "fixture",
        ], check=True)
        with self.assertRaisesRegex(RELEASE.ReleaseError, "lock.*absent"):
            RELEASE.validate_product_runtime_contract(self.product)

    def test_release_replay_accepts_only_nonempty_lease_runtime_after_dispatch(self) -> None:
        (self.product / ".gitignore").write_text(
            "factory/runs/\nfactory/.active-runs/\nfactory/operator-map.json\n"
            "factory/.operator-map.lock\nfactory/.operator-clears/\n"
            "factory/.dispatch-leases/\n"
            "factory/.dispatch-leases.lock/\n"
        )
        subprocess.run(["git", "-C", str(self.product), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(self.product), "add", ".gitignore"], check=True)
        subprocess.run([
            "git", "-C", str(self.product), "-c", "user.name=test", "-c",
            "user.email=test@local", "commit", "-qm", "fixture",
        ], check=True)
        leases = self.product / "factory/.dispatch-leases"
        leases.mkdir(mode=0o700)
        (leases / "T-1.json").write_text("runtime\n")
        with self.assertRaisesRegex(RELEASE.ReleaseError, "empty or absent"):
            RELEASE.validate_product_runtime_contract(self.product)
        RELEASE.validate_product_runtime_contract(
            self.product, require_idle_dispatch=False,
        )

    def test_release_refuses_missing_operator_clear_ignore(self) -> None:
        (self.product / ".gitignore").write_text(
            "factory/runs/\nfactory/.active-runs/\nfactory/operator-map.json\n"
            "factory/.operator-map.lock\nfactory/.dispatch-leases/\n"
            "factory/.dispatch-leases.lock/\n"
        )
        subprocess.run(["git", "-C", str(self.product), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(self.product), "add", ".gitignore"], check=True)
        subprocess.run([
            "git", "-C", str(self.product), "-c", "user.name=test", "-c",
            "user.email=test@local", "commit", "-qm", "fixture",
        ], check=True)
        with self.assertRaisesRegex(RELEASE.ReleaseError, "operator-clears"):
            RELEASE.validate_product_runtime_contract(self.product)

    def test_release_refuses_tracked_or_unsafe_operator_clears(self) -> None:
        ignore = (
            "factory/runs/\nfactory/.active-runs/\nfactory/operator-map.json\n"
            "factory/.operator-map.lock\nfactory/.operator-clears/\n"
            "factory/.dispatch-leases/\nfactory/.dispatch-leases.lock/\n"
        )
        for index, unsafe in enumerate((False, True)):
            product = self.root / f"operator-clears-{index}"
            (product / "factory").mkdir(parents=True)
            (product / ".gitignore").write_text(ignore)
            subprocess.run(["git", "-C", str(product), "init", "-q"], check=True)
            subprocess.run(["git", "-C", str(product), "add", ".gitignore"], check=True)
            if unsafe:
                target = self.root / "foreign-operator-clears"
                target.mkdir()
                (product / "factory/.operator-clears").symlink_to(
                    target, target_is_directory=True,
                )
            else:
                intent = product / "factory/.operator-clears/T-218.json"
                intent.parent.mkdir()
                intent.write_text("tracked\n")
                subprocess.run([
                    "git", "-C", str(product), "add", "-f",
                    "factory/.operator-clears/T-218.json",
                ], check=True)
            subprocess.run([
                "git", "-C", str(product), "-c", "user.name=test", "-c",
                "user.email=test@local", "commit", "-qm", "fixture",
            ], check=True)
            expected = "state directory is unsafe" if unsafe else "operator-clears.*untracked"
            with self.assertRaisesRegex(RELEASE.ReleaseError, expected):
                RELEASE.validate_product_runtime_contract(product)

    def test_release_initializes_every_bound_ticket_without_erasing_overlays(self) -> None:
        tickets = self.product / "factory/tickets"
        tickets.mkdir()
        for ticket, state in (("T-1", "Ready"), ("T-2", "Done")):
            (tickets / f"{ticket}.md").write_text(
                f"# {ticket}\n\nState: {state}\nPriority: normal\n"
            )
        plan = json.loads(json.dumps(self.plan))
        plan["identity"]["tickets"] = [
            {"blob": "3" * 40, "state": "Ready", "ticket": "T-1"},
            {"blob": "4" * 40, "state": "Done", "ticket": "T-2"},
        ]
        mapping = json.loads((self.product / "factory/operator-map.json").read_text())
        mapping["tickets"]["T-1"] = {
            "operator": {"priority": "urgent"},
            "operator_fields_initialized": True,
        }
        RELEASE.atomic_json(self.product / "factory/operator-map.json", mapping)
        RELEASE.initialize_operator_map(ROOT, self.kits, plan, os.environ.copy())
        observed = RELEASE.safe_state(
            self.product / "factory/operator-map.json", "operator map",
        )
        self.assertEqual(
            observed["tickets"]["T-1"]["operator"], {"priority": "urgent"},
        )
        self.assertTrue(observed["tickets"]["T-2"]["operator_fields_initialized"])
        self.assertTrue(RELEASE.operator_map_ready(plan))

    def test_operator_initialization_precedes_controller_and_dispatch(self) -> None:
        source = (ROOT / "scripts/release-transaction.py").read_text()
        self.assertLess(
            source.index("initialize_operator_map(release, kits_root, plan, environment)"),
            source.index("ensure_controller(plan)"),
        )
        self.assertLess(
            source.index("ensure_controller(plan)"),
            source.index('marker.unlink()', source.index("ensure_controller(plan)")),
        )

    def test_launcher_test_mode_retains_its_explicit_isolated_kits_root(self) -> None:
        home = self.root / "test-home"
        home.mkdir(mode=0o700)
        kits = home / ".factory/kits"
        runtime = home / ".factory/project-runtimes/relay/bin"
        with mock.patch.dict(os.environ, {
            "FACTORY_KIT_TEST_MODE": "1",
            "FACTORY_RELEASE_TEST_HOME": str(home),
        }):
            environment = RELEASE.launcher_environment(kits, runtime)
        self.assertEqual(environment["FACTORY_KITS_ROOT"], str(kits))
        self.assertEqual(environment["FACTORY_LAUNCH_TEST_HOME"], str(home))
        self.assertNotIn("FACTORY_LAUNCH_TEST_ACCOUNT_HOME", environment)
        self.assertEqual(environment["FACTORY_LAUNCH_TEST_MODE"], "1")

    def test_runtime_reentry_reuses_the_completed_exact_approval(self) -> None:
        release = self.root / "release"
        runtime = self.root / "node/bin"
        runtime.mkdir(parents=True)
        root = RELEASE.project_runtime_root(self.kits, "relay")
        root.mkdir(parents=True)
        plan_hash = "7" * 64
        RELEASE.atomic_json(root / "runtime-pin-journal.json", {
            "plan": {
                "approval_sha256": plan_hash,
                "product_path": str(self.product),
                "runtime_bin": str(runtime),
                "target_bin": str(root / "bin"),
            },
            "status": "completed",
        })
        evidence = {
            "path": str(root / "bin"), "status": "ready",
        }
        with (
            mock.patch.object(RELEASE, "run_json", return_value=evidence) as checked,
            mock.patch.object(RELEASE.subprocess, "run") as replanned,
        ):
            observed = RELEASE.prepare_runtime(
                release, self.product, self.kits, "relay", runtime,
            )
        self.assertEqual(observed["plan_sha256"], plan_hash)
        checked.assert_called_once()
        replanned.assert_not_called()

    def test_host_cutover_refuses_sibling_runtime_drift_before_mutation(self) -> None:
        active = self.kits / "projects/nysa/active.json"
        active.parent.mkdir(parents=True)
        RELEASE.atomic_json(active, {"project": "nysa"})
        before = active.read_bytes()
        item = {
            "product": str(self.product), "project": "nysa", "tickets": [],
            "runtime": {
                "evidence": {
                    "path": str(RELEASE.project_runtime_root(self.kits, "nysa") / "bin"),
                },
                "plan_sha256": "7" * 64,
            },
        }
        with (
            mock.patch.object(RELEASE, "ticket_inventory", return_value=[]),
            self.assertRaisesRegex(RELEASE.ReleaseError, "runtime changed"),
        ):
            RELEASE.validate_host_item_basis(item, self.root / "release", self.kits)
        self.assertEqual(active.read_bytes(), before)
        self.assertFalse((self.kits / "contract-floor.json").exists())
        source = (ROOT / "scripts/release-transaction.py").read_text()
        apply = source.index("def _apply_host_cutover_locked")
        self.assertLess(
            source.index("validate_host_item_basis(item, release, kits_root)", apply),
            source.index('f"host cutover activation reconcile for {project}"', apply),
        )

    def test_host_cutover_initializes_bound_sibling_ticket_inventory(self) -> None:
        product = self.root / "sibling"
        (product / "factory/tickets").mkdir(parents=True)
        (product / "factory/tickets/T-1.md").write_text(
            "# T-1\n\nState: Ready\nPriority: normal\n"
        )
        RELEASE.atomic_json(product / "factory/operator-map.json", {
            "_config": None, "_sync": {}, "initiatives": {}, "tickets": {},
        })
        inventory = [{"blob": "3" * 40, "state": "Ready", "ticket": "T-1"}]
        item = {
            "product": str(product), "project": "nysa", "tickets": inventory,
            "runtime": {"evidence": {"path": str(self.root / "runtime")}},
        }
        self.assertFalse(RELEASE.operator_inventory_ready(product, inventory))
        RELEASE.initialize_host_operator_maps(ROOT, self.kits, [item])
        self.assertTrue(RELEASE.operator_inventory_ready(product, inventory))
        source = (ROOT / "scripts/release-transaction.py").read_text()
        apply = source.index("def _apply_host_cutover_locked")
        initialized = source.index(
            "initialize_host_operator_maps(release, kits_root, items)", apply,
        )
        self.assertLess(
            initialized,
            source.index("validate_host_runtime(plan, release, kits_root", initialized),
        )

    def test_isolated_launcher_forwards_only_explicit_local_origin_evidence(self) -> None:
        launcher = (ROOT / "scripts/factory-launch").read_text()
        controller = launcher[launcher.index("  reconcile)"):launcher.index("  incident-report)")]
        for binding in (
            '"FACTORY_LAUNCH_TEST_MODE=1"',
            '"FACTORY_LAUNCH_TEST_HOME=$HOME"',
            '"FACTORY_KITS_ROOT=$KITS_ROOT"',
        ):
            self.assertIn(binding, controller)
        self.assertNotIn("HER" + "MES_", controller)
        self.assertIn('exec "${CONTROLLER_ENV[@]}"', controller)
        self.assertIn('"${FACTORY_KIT_TEST_MODE:-0}" == "1"', launcher)
        self.assertIn('"${FACTORY_KIT_CANONICAL_ORIGIN:-}" == /*', launcher)
        self.assertIn(
            '"FACTORY_KIT_CANONICAL_ORIGIN=$FACTORY_KIT_CANONICAL_ORIGIN"',
            launcher,
        )

    def test_controller_job_is_exact_and_non_overwriting(self) -> None:
        home = self.root / "home"
        home.mkdir()
        with mock.patch.object(RELEASE.Path, "home", return_value=home):
            with mock.patch.object(RELEASE.sys, "platform", "darwin"):
                controller = RELEASE.prepare_controller("relay", self.product)
                self.assertEqual(RELEASE.prepare_controller("relay", self.product), controller)
                payload = plistlib.loads(Path(controller["path"]).read_bytes())
                self.assertEqual(payload["ProcessType"], "Interactive")
                Path(controller["path"]).write_text("foreign")
                with self.assertRaisesRegex(RELEASE.ReleaseError, "controller job conflicts"):
                    RELEASE.prepare_controller("relay", self.product)

    def test_controller_job_uses_the_project_bound_launcher(self) -> None:
        home = self.root / "home"
        launcher = home / ".factory/kits/releases" / self.sha / "scripts/factory-launch"
        with mock.patch.object(RELEASE, "account_home", return_value=home):
            payload = plistlib.loads(
                RELEASE.controller_payload("relay", self.product, launcher)
            )
        self.assertEqual(payload["ProgramArguments"][:2], [str(launcher), "relay"])

    def test_project_controller_replacement_waits_for_approved_activation(self) -> None:
        home = self.root / "home"
        jobs = home / "Library/LaunchAgents"
        jobs.mkdir(parents=True)
        path = jobs / "com.factory.controller.relay.plist"
        path.write_bytes(b"old controller\n")
        path.chmod(0o600)
        launcher = home / ".factory/kits/releases" / self.sha / "scripts/factory-launch"
        with (
            mock.patch.object(RELEASE, "account_home", return_value=home),
            mock.patch.object(RELEASE.sys, "platform", "darwin"),
        ):
            plan = RELEASE.prepare_controller("relay", self.product, launcher)
        self.assertEqual(plan["action"], "apply")
        self.assertEqual(path.read_bytes(), b"old controller\n")

    def test_controller_enable_loads_the_bound_job_before_dispatch(self) -> None:
        home = self.root / "home"
        (home / "Library/LaunchAgents").mkdir(parents=True)
        controller_path = home / "Library/LaunchAgents/com.factory.controller.relay.plist"
        with mock.patch.object(RELEASE.Path, "home", return_value=home):
            raw = RELEASE.controller_payload(
                "relay", self.product, RELEASE.launcher_path(self.plan["children"]["launcher"]),
            )
            RELEASE.atomic_bytes(controller_path, raw)
            plan = json.loads(json.dumps(self.plan))
            plan["identity"]["controller"] = {
                "label": "com.factory.controller.relay", "path": str(controller_path),
                "platform": "darwin", "sha256": RELEASE.hashlib.sha256(raw).hexdigest(),
            }
            statuses = [
                subprocess.CompletedProcess([], 1, "", ""),
                subprocess.CompletedProcess([], 0, "", ""),
                subprocess.CompletedProcess([], 0, "", ""),
            ]
            with (
                mock.patch.object(RELEASE, "run") as enable,
                mock.patch.object(RELEASE.Path, "is_file", return_value=True),
                mock.patch.object(RELEASE.os, "access", return_value=True),
                mock.patch.object(RELEASE.subprocess, "run", side_effect=statuses) as native,
            ):
                RELEASE.ensure_controller(plan)
        enable.assert_called_once()
        self.assertEqual(native.call_count, 3)
        self.assertIn("bootstrap", native.call_args_list[1].args[0])

    def test_launcher_pin_is_exact_recoverable_and_keeps_a_rollback(self) -> None:
        home = self.root / "home"
        target = home / ".factory/bin/factory-launch"
        target.parent.mkdir(parents=True)
        target.write_text("old launcher\n")
        target.chmod(0o700)
        release = self.root / "release"
        candidate = release / "scripts/factory-launch"
        candidate.parent.mkdir(parents=True)
        candidate.write_text("new launcher\n")
        candidate.chmod(0o555)
        with mock.patch.object(RELEASE.Path, "home", return_value=home):
            plan = RELEASE.launcher_plan(release, self.kits)
            self.assertEqual(plan["action"], "apply")
            result = RELEASE.apply_launcher_plan(plan, release, self.kits)
            self.assertEqual(result["status"], "applied")
            self.assertEqual(target.read_bytes(), candidate.read_bytes())
            rollback = home / ".factory/launcher-rollbacks" / (
                f"{plan['approval_sha256']}.factory-launch"
            )
            self.assertEqual(rollback.read_text(), "old launcher\n")
            self.assertEqual(
                RELEASE.apply_launcher_plan(plan, release, self.kits)["status"],
                "replayed",
            )
            journal = RELEASE.safe_state(
                home / ".factory/launcher-pin-journal.json", "launcher pin journal",
            )
            self.assertEqual(journal["status"], "completed")
            candidate.chmod(0o755)
            candidate.write_text("changed candidate\n")
            candidate.chmod(0o555)
            with self.assertRaisesRegex(RELEASE.ReleaseError, "launcher pin"):
                RELEASE.apply_launcher_plan(plan, release, self.kits)

    def test_project_launcher_plan_ignores_unrelated_legacy_projects(self) -> None:
        home = self.root / "home"
        (home / ".factory/bin").mkdir(parents=True)
        release = home / ".factory/kits/releases" / self.sha
        scripts = release / "scripts"
        scripts.mkdir(parents=True)
        for name in ("factory-launch", "factory-cli.py"):
            (scripts / name).write_text(f"{name}\n")
            (scripts / name).chmod(0o555)
        stable = home / ".factory/bin/factory-launch"
        stable.parent.mkdir(parents=True, exist_ok=True)
        stable.write_text("legacy launcher\n")
        stable.chmod(0o700)
        legacy = self.kits / "projects/legacy/active.json"
        legacy.parent.mkdir(parents=True)
        RELEASE.atomic_json(legacy, {
            "contract_version": "1.9.0", "kit_sha": "b" * 40,
            "product_path": str(self.product),
        })
        with mock.patch.object(RELEASE, "account_home", return_value=home):
            plan = RELEASE.launcher_plan(release, self.kits, "relay")
        self.assertEqual(plan["action"], "apply")
        self.assertEqual(plan["target"], str(scripts / "factory-launch"))
        self.assertEqual(plan["active_projects"], [])
        self.assertEqual(plan["previous_sha256"], plan["candidate"]["sha256"])
        with mock.patch.object(RELEASE, "account_home", return_value=home):
            RELEASE.apply_launcher_plan(plan, release, self.kits, self.sha)
            self.assertEqual(stable.read_text(), "legacy launcher\n")
            successor = home / ".factory/kits/releases" / ("c" * 40)
            (successor / "scripts").mkdir(parents=True)
            (successor / "scripts/factory-launch").write_text("successor launcher\n")
            (successor / "scripts/factory-cli.py").write_text("successor CLI\n")
            for path in (successor / "scripts").iterdir():
                path.chmod(0o555)
            successor_plan = RELEASE.launcher_plan(successor, self.kits, "other")
            self.assertEqual(successor_plan["action"], "reuse")
            self.assertEqual(
                successor_plan["human_cli"]["sha256"],
                plan["human_cli"]["candidate"]["sha256"],
            )
            tampered = json.loads(json.dumps(plan))
            tampered["candidate"]["path"] = str(scripts / "other-launcher")
            body = {key: value for key, value in tampered.items() if key != "approval_sha256"}
            tampered["approval_sha256"] = RELEASE.digest(body)
            with self.assertRaisesRegex(RELEASE.ReleaseError, "launcher pin plan"):
                RELEASE.validate_launcher_plan(tampered)

    def test_production_registration_executes_the_sealed_human_cli(self) -> None:
        release = self.root / "release"
        candidate = release / "scripts/factory-cli.py"
        candidate.parent.mkdir(parents=True)
        candidate.write_text("#!/usr/bin/python3\n")
        candidate.chmod(0o555)
        launcher = release / "scripts/factory-launch"
        with mock.patch.object(RELEASE, "run") as invoke:
            RELEASE.register_production_target(release, "relay", launcher)
        self.assertEqual(invoke.call_args.args[0][0], str(candidate))

    def test_launcher_pin_refuses_any_unpaused_active_factory(self) -> None:
        home = self.root / "home"
        home.mkdir()
        release = self.root / "release"
        candidate = release / "scripts/factory-launch"
        candidate.parent.mkdir(parents=True)
        candidate.write_text("candidate\n")
        candidate.chmod(0o555)
        active = self.kits / "projects/live/active.json"
        active.parent.mkdir()
        RELEASE.atomic_json(active, {
            "contract_version": "1.9.0", "kit_sha": "b" * 40,
            "product_path": str(self.product),
        })
        with mock.patch.object(RELEASE.Path, "home", return_value=home):
            plan = RELEASE.launcher_plan(release, self.kits)
            with self.assertRaisesRegex(RELEASE.ReleaseError, "live is not in maintenance"):
                RELEASE.apply_launcher_plan(plan, release, self.kits)
        self.assertFalse((home / ".factory/bin/factory-launch").exists())

    def test_host_cutover_reconciles_children_retires_old_runtime_and_sets_floor(self) -> None:
        release = self.kits / "releases" / self.sha
        (release / "scripts").mkdir(parents=True)
        (release / "scripts/factory-kit.sh").write_text("#!/bin/sh\n")
        home = self.root / "home"
        launcher_path = home / ".factory/bin/factory-launch"
        launcher_path.parent.mkdir(parents=True)
        launcher_path.write_text("Contract 2 launcher\n")
        launcher_path.chmod(0o700)
        removed = "her" + "mes"
        old_profile = home / f".{removed}/profiles/factory"
        old_profile.mkdir(parents=True)
        (old_profile / "SOUL.md").write_text("old Factory profile\n")
        old_jobs = home / "Library/LaunchAgents"
        old_jobs.mkdir(parents=True)
        for suffix in ("dashboard", "factory-gateway"):
            RELEASE.atomic_bytes(
                old_jobs / f"com.nysa.{removed}-{suffix}.plist",
                f"{suffix}\n".encode(),
            )
        reservation_id = "8" * 64
        items = []
        for index, project in enumerate(("relay", "nysa"), start=1):
            product = self.root / project
            (product / "factory").mkdir(parents=True)
            RELEASE.atomic_json(product / "factory/operator-map.json", {
                "_config": None, "_sync": {}, "initiatives": {}, "tickets": {},
            })
            RELEASE.atomic_json(product / "factory/MAINTENANCE", {
                "cutover_owner": reservation_id, "product_path": str(product),
                "project": project, "published_at": "test", "schema_version": 1,
            })
            maintenance_sha = RELEASE.file_digest(product / "factory/MAINTENANCE")
            active = self.kits / "projects" / project / "active.json"
            active.parent.mkdir(parents=True, exist_ok=True)
            RELEASE.atomic_json(active, {
                "contract_version": "1.9.0", "kit_sha": str(index) * 40,
                "product_path": str(product), "project": project,
            })
            receipt = self.root / f"{project}-receipt.json"
            receipt_id = str(index + 2) * 64
            RELEASE.atomic_json(receipt, {
                "contract_version": "2.0.0", "kit_sha": self.sha,
                "kit_tree": str(index + 3) * 40,
                "product_path": str(product), "product_sha": str(index + 5) * 40,
                "product_tree": str(index + 6) * 40, "project": project,
                "receipt_id": receipt_id,
            })
            items.append({
                "controller": {"platform": "test", "status": "not-applicable"},
                "incident": None,
                "maintenance": {
                    "cutover_sha256": maintenance_sha, "prior": None,
                    "reservation_id": reservation_id,
                },
                "product": str(product), "project": project,
                "receipt": {
                    "path": str(receipt), "receipt_id": receipt_id,
                    "sha256": RELEASE.file_digest(receipt),
                },
                "runtime": {
                    "evidence": {"path": str(self.root / f"{project}-runtime")},
                    "plan_sha256": str(index + 5) * 64,
                },
                "source_active_sha256": RELEASE.file_digest(active),
                "tickets": [],
            })
        with (
            mock.patch.object(RELEASE, "account_home", return_value=home),
            mock.patch.dict(os.environ, {"FACTORY_KIT_TEST_MODE": "1"}),
        ):
            retired = RELEASE.retired_runtime_plan()
        plan = json.loads(json.dumps(self.plan))
        plan["approval_sha256"] = "9" * 64
        plan["request"]["sha"] = self.sha
        plan["request"]["product"] = items[0]["product"]
        plan["children"] = {
            "host_cutover": items,
            "launcher": {
                "action": "apply", "target": str(launcher_path),
                "candidate": {"sha256": RELEASE.file_digest(launcher_path)},
            },
            "retired_runtime": retired,
        }
        basis = RELEASE.reservation_basis(
            self.kits, plan["children"]["launcher"], retired,
            Path(items[0]["product"]), "relay", self.sha,
        )
        reservation_id = basis["reservation_id"]
        for item in items:
            maintenance = Path(item["product"]) / "factory/MAINTENANCE"
            RELEASE.atomic_json(maintenance, {
                "cutover_owner": reservation_id, "product_path": item["product"],
                "project": item["project"], "published_at": "test", "schema_version": 1,
            })
            item["maintenance"].update(
                cutover_sha256=RELEASE.file_digest(maintenance),
                reservation_id=reservation_id,
            )
        reservation = {
            **basis, "approval_sha256": plan["approval_sha256"], "status": "prepared",
        }
        RELEASE.atomic_json(
            self.kits / "contract-cutover-reservation.json",
            {**reservation, "record_sha256": RELEASE.digest(reservation)},
        )
        activated = []
        doctor_status = {"value": "warning", "maintenance_only": False}

        def activate(arguments: list[str], _label: str, **_kwargs: object) -> str:
            if "activate" not in arguments:
                return ""
            project = arguments[arguments.index("--project") + 1]
            item = next(value for value in items if value["project"] == project)
            receipt_value = RELEASE.safe_state(
                Path(item["receipt"]["path"]), "certification receipt",
            )
            transaction = f"transaction-{project}"
            record = {
                "contract_version": "2.0.0", "generation": 2,
                "kit_sha": self.sha, "kit_tree": receipt_value["kit_tree"],
                "product_path": item["product"],
                "product_sha": receipt_value["product_sha"],
                "product_tree": receipt_value["product_tree"], "project": project,
                "receipt_id": item["receipt"]["receipt_id"],
                "release_path": str(release),
            }
            RELEASE.atomic_json(
                self.kits / "projects" / project / "active.json", record,
            )
            RELEASE.atomic_json(
                self.kits / "projects" / project / "activation-journal"
                / f"{2:020d}-{self.sha}.json",
                {
                    "candidate_record": record, "phase": "committed",
                    "receipt_hash": item["receipt"]["sha256"],
                    "receipt_snapshot": receipt_value, "transaction_id": transaction,
                },
            )
            RELEASE.atomic_json(
                self.kits / "receipts/consumed"
                / f"{item['receipt']['receipt_id']}.json",
                {
                    "receipt_id": item["receipt"]["receipt_id"],
                    "transaction_id": transaction,
                },
            )
            activated.append(project)
            return ""

        def doctor(arguments: list[str], *_args: object, **_kwargs: object) -> dict[str, object]:
            project = arguments[1]
            item = next(value for value in items if value["project"] == project)
            in_maintenance = (Path(item["product"]) / "factory/MAINTENANCE").exists()
            if doctor_status["maintenance_only"] and in_maintenance:
                checks = {
                    "active_binding": {"status": "ok"},
                    "runtime": {
                        "active_run_claims": 0, "active_run_tickets": [],
                        "active_runs": 0, "dispatch_lease_records": 0,
                        "locks": {
                            "global_ledger": False, "launch": False,
                            "ledger": False, "provider": False,
                        },
                        "maintenance": True, "malformed_dispatch_leases": 0,
                        "malformed_active_run_claims": 0, "malformed_runs": 0,
                        "provider_lock_state": "absent",
                        "run_records": 0, "stale_dispatch_leases": 0,
                        "stale_runs": 0, "status": "warning",
                    },
                }
            else:
                checks = {
                    "active_binding": {"status": "ok"},
                    "runtime": {"status": "ok"},
                }
            return {
                "checks": checks, "contract_version": "2.0.0",
                "overall_status": (
                    doctor_status["value"]
                    if not doctor_status["maintenance_only"] or in_maintenance else "ok"
                ),
                "project": project,
                "schema": "nysa.software-factory.doctor/v2", "schema_version": 2,
            }

        with (
            mock.patch.object(RELEASE, "run", side_effect=activate),
            mock.patch.object(RELEASE, "unload_service"),
            mock.patch.object(RELEASE, "ensure_service"),
            mock.patch.object(RELEASE, "validate_host_item_basis"),
            mock.patch.object(RELEASE, "initialize_host_operator_maps"),
            mock.patch.object(RELEASE, "apply_launcher_plan") as launcher,
            mock.patch.object(RELEASE, "run_json", side_effect=doctor),
            mock.patch.object(RELEASE, "account_home", return_value=home),
            mock.patch.object(
                RELEASE, "launcher_environment",
                side_effect=lambda kits, runtime: RELEASE.command_environment(kits, runtime),
            ),
            mock.patch.dict(
                os.environ,
                {
                    "FACTORY_KIT_TEST_MODE": "1",
                    "FACTORY_RELEASE_FAIL_AFTER_CUTOVER_PHASE": "project:relay",
                },
            ),
        ):
            with self.assertRaisesRegex(RELEASE.ReleaseError, "project:relay"):
                RELEASE.apply_host_cutover(plan, release, self.kits)
            for phase in (
                "active_records_switched", "contract_floor_committed", "launcher_installed",
                "operator_initialized",
            ):
                os.environ["FACTORY_RELEASE_FAIL_AFTER_CUTOVER_PHASE"] = phase
                with self.assertRaisesRegex(RELEASE.ReleaseError, phase):
                    RELEASE.apply_host_cutover(plan, release, self.kits)
            os.environ.pop("FACTORY_RELEASE_FAIL_AFTER_CUTOVER_PHASE", None)
            with self.assertRaisesRegex(RELEASE.ReleaseError, "Doctor did not pass"):
                RELEASE.apply_host_cutover(plan, release, self.kits)
            self.assertTrue((self.root / "relay/factory/MAINTENANCE").exists())
            self.assertTrue((self.root / "nysa/factory/MAINTENANCE").exists())
            self.assertTrue(old_profile.exists())
            doctor_status["maintenance_only"] = True
            for phase in ("retired_runtime_removed", "healthy"):
                os.environ["FACTORY_RELEASE_FAIL_AFTER_CUTOVER_PHASE"] = phase
                with self.assertRaisesRegex(RELEASE.ReleaseError, phase):
                    RELEASE.apply_host_cutover(plan, release, self.kits)
            os.environ.pop("FACTORY_RELEASE_FAIL_AFTER_CUTOVER_PHASE", None)
            RELEASE.apply_host_cutover(plan, release, self.kits)
            doctor_status.update(value="ok", maintenance_only=False)
            (self.kits / "contract-floor.json").unlink()
            RELEASE.apply_host_cutover(plan, release, self.kits)
        self.assertEqual(activated, ["relay", "nysa"])
        self.assertGreaterEqual(launcher.call_count, 2)
        self.assertTrue((self.root / "relay/factory/MAINTENANCE").exists())
        self.assertFalse((self.root / "nysa/factory/MAINTENANCE").exists())
        self.assertFalse(old_profile.exists())
        self.assertFalse(any(old_jobs.glob(f"com.nysa.{removed}-*.plist")))
        archive = home / ".factory/retired-runtime" / plan["approval_sha256"]
        self.assertTrue((archive / "profile/SOUL.md").is_file())
        self.assertEqual(len(list((archive / "services").glob("*.plist"))), 2)
        self.assertEqual(
            RELEASE.safe_state(self.kits / "contract-floor.json", "contract floor"),
            {
                "minimum_major": 2,
                "schema": "nysa.software-factory.contract-floor/v1",
            },
        )
        self.assertEqual(
            RELEASE.safe_state(
                self.kits / "contract-cutover-journal.json", "cutover journal",
            )["status"],
            "pass",
        )
        relay_journal = (
            self.kits / "projects/relay/activation-journal"
            / f"{2:020d}-{self.sha}.json"
        )
        terminal = RELEASE.safe_state(relay_journal, "activation journal")
        for phase in (
            "prepared", "receipt_claimed", "maintenance_published", "launch_drained",
            "services_stopped", "activation_record_switched",
            "integration_bundle_switched", "services_started", "healthy",
        ):
            with self.subTest(interrupted_child_phase=phase):
                RELEASE.atomic_json(relay_journal, {**terminal, "phase": phase})
                self.assertFalse(RELEASE.cutover_terminal_exact(
                    self.kits, items[0], self.sha, release,
                ))
        RELEASE.atomic_json(relay_journal, terminal)

    def test_cutover_restores_an_exact_preexisting_maintenance_marker(self) -> None:
        product = self.root / "paused-product"
        (product / "factory").mkdir(parents=True)
        prior = b'{"incident":"operator"}\n'
        snapshot = self.root / "prior-maintenance"
        RELEASE.atomic_bytes(snapshot, prior)
        marker = product / "factory/MAINTENANCE"
        reservation_id = "8" * 64
        RELEASE.atomic_json(marker, {
            "cutover_owner": reservation_id, "product_path": str(product),
            "project": "paused", "schema_version": 1,
        })
        item = {
            "maintenance": {
                "cutover_sha256": RELEASE.file_digest(marker),
                "prior": {
                    "path": str(snapshot),
                    "sha256": RELEASE.hashlib.sha256(prior).hexdigest(),
                },
                "reservation_id": reservation_id,
            },
            "product": str(product), "project": "paused",
        }
        RELEASE.clear_cutover_maintenance(item)
        self.assertEqual(marker.read_bytes(), prior)
        RELEASE.clear_cutover_maintenance(item)
        self.assertEqual(marker.read_bytes(), prior)

    def test_maintenance_capture_replays_for_absent_and_preexisting_markers(self) -> None:
        reservation_id = "8" * 64
        for prior in (None, b'{"incident":"operator"}\n'):
            with self.subTest(prior=prior is not None):
                product = self.root / ("prior" if prior else "absent")
                (product / "factory").mkdir(parents=True)
                marker = product / "factory/MAINTENANCE"
                if prior is not None:
                    RELEASE.atomic_bytes(marker, prior)
                first = RELEASE.capture_maintenance(
                    self.kits, product, product.name, reservation_id,
                )
                RELEASE.atomic_json(marker, {
                    "cutover_owner": reservation_id, "product_path": str(product),
                    "project": product.name, "schema_version": 1,
                })
                second = RELEASE.capture_maintenance(
                    self.kits, product, product.name, reservation_id,
                )
                self.assertEqual(second, first)

    def test_retired_service_unload_requires_the_label_to_disappear(self) -> None:
        value = {"label": "com.factory.retired", "path": "/tmp/unused"}
        statuses = [
            subprocess.CompletedProcess([], 0),
            subprocess.CompletedProcess([], 0),
            subprocess.CompletedProcess([], 0),
            subprocess.CompletedProcess([], 1),
        ]
        with (
            mock.patch.object(RELEASE.sys, "platform", "darwin"),
            mock.patch.object(RELEASE, "service_prefix", return_value=(["launchctl"], "gui/1")),
            mock.patch.object(RELEASE.subprocess, "run", side_effect=statuses),
            mock.patch.object(RELEASE.time, "sleep") as converging,
            mock.patch.object(RELEASE, "run") as bootout,
        ):
            RELEASE.unload_service(value)
        bootout.assert_called_once()
        self.assertEqual(bootout.call_args.kwargs["timeout"], 5)
        self.assertEqual(converging.call_count, 2)
        with (
            mock.patch.object(RELEASE.sys, "platform", "darwin"),
            mock.patch.object(RELEASE, "service_prefix", return_value=(["launchctl"], "gui/1")),
            mock.patch.object(
                RELEASE.subprocess, "run", return_value=subprocess.CompletedProcess([], 0),
            ) as status,
            mock.patch.object(RELEASE.time, "monotonic", side_effect=[0, 0, 5, 5]),
            mock.patch.object(RELEASE.time, "sleep") as pause,
            mock.patch.object(RELEASE, "run"),
            self.assertRaisesRegex(RELEASE.ReleaseError, "did not unload"),
        ):
            RELEASE.unload_service(value)
        self.assertEqual(status.call_count, 2)
        self.assertLessEqual(status.call_args.kwargs["timeout"], 5)
        pause.assert_not_called()

    def test_machine_cutover_lock_serializes_project_transactions(self) -> None:
        marker = self.root / "lock-order"
        retired = self.retired_runtime(self.root / "home")
        plans = []
        for project in ("relay", "nysa", "third"):
            plans.append({
                "children": {"host_cutover": [], "retired_runtime": retired},
                "request": {"project": project},
            })
        context = multiprocessing.get_context("spawn")
        processes = [
            context.Process(
                target=cutover_lock_worker,
                args=(str(self.kits), str(marker), plan, mode),
            )
            for plan, mode in zip(plans, ("setup", "resume", "apply"))
        ]
        for process in processes:
            process.start()
        for process in processes:
            process.join(10)
            self.assertEqual(process.exitcode, 0)
        lines = marker.read_text().splitlines()
        self.assertEqual(len(lines), 6)
        for index in range(0, len(lines), 2):
            self.assertEqual(lines[index].split(":", 1)[0], "start")
            self.assertEqual(
                lines[index + 1], "end:" + lines[index].split(":", 1)[1],
            )

    def test_machine_lock_capability_is_scoped_to_sealed_mutation_children(self) -> None:
        descriptor = RELEASE.acquire_cutover_lock(self.kits)
        admission = os.open(self.root / "admission.lock", os.O_CREAT | os.O_RDWR, 0o600)
        controller = os.open(self.root / "controller.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            ordinary = RELEASE.command_environment(self.kits)
            mutation = RELEASE.command_environment(self.kits, cutover_lock=True)
            self.assertNotIn("FACTORY_HOST_CUTOVER_LOCK_FD", ordinary)
            self.assertEqual(
                mutation["FACTORY_HOST_CUTOVER_LOCK_FD"], str(descriptor),
            )
            with mock.patch.object(
                RELEASE.subprocess, "run",
                return_value=subprocess.CompletedProcess([], 0, "", ""),
            ) as spawned:
                RELEASE.run(["candidate-install"], "candidate", environment=ordinary)
                self.assertEqual(spawned.call_args.kwargs["pass_fds"], ())
                RELEASE.run(["sealed-mutation"], "mutation", environment=mutation)
                self.assertEqual(
                    spawned.call_args.kwargs["pass_fds"], (descriptor,),
                )
                RELEASE.run(["sealed-recovery"], "recovery", environment={
                    "FACTORY_DISPATCH_ADMISSION_LOCK_FD": str(admission),
                    "FACTORY_QUALIFICATION_CONTROLLER_LOCK_FD": str(controller),
                })
                self.assertEqual(
                    spawned.call_args.kwargs["pass_fds"],
                    tuple(sorted((admission, controller))),
                )
        finally:
            os.close(admission)
            os.close(controller)
            RELEASE.release_cutover_lock(descriptor)

    def test_recovery_child_retains_admission_after_parent_kill(self) -> None:
        admission = self.root / "admission.lock"
        controller = self.root / "controller.lock"
        admission.touch(mode=0o600)
        controller.touch(mode=0o600)
        marker = self.root / "child.pid"
        child = self.root / "child.py"
        child.write_text(
            "import importlib.util,os,sys,time\n"
            "spec=importlib.util.spec_from_file_location('cancel_child',sys.argv[1])\n"
            "module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)\n"
            "assert module.sealed_recovery_locks_held({"
            "'contract_version':'2.0.0','kit_sha':os.environ["
            "'FACTORY_CROSS_RELEASE_SOURCE_SHA']})\n"
            "open(sys.argv[2],'w').write(str(os.getpid()))\n"
            "time.sleep(30)\n",
            encoding="utf-8",
        )
        wrapper = self.root / "wrapper.py"
        wrapper.write_text(
            "import fcntl,importlib.util,os,sys\n"
            "spec=importlib.util.spec_from_file_location('release_parent',sys.argv[1])\n"
            "module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)\n"
            "admission=os.open(sys.argv[2],os.O_RDWR);"
            "controller=os.open(sys.argv[3],os.O_RDWR)\n"
            "fcntl.flock(admission,fcntl.LOCK_EX);"
            "fcntl.flock(controller,fcntl.LOCK_EX)\n"
            "sha='d'*40\n"
            "env={'HOME':os.environ['HOME'],'PATH':'/usr/bin:/bin',"
            "'FACTORY_CROSS_RELEASE_SOURCE_SHA':sha,"
            "'FACTORY_CROSS_RELEASE_PRODUCT_ID':'relay:'+sha,"
            "'FACTORY_DISPATCH_ADMISSION_LOCK':sys.argv[2],"
            "'FACTORY_DISPATCH_ADMISSION_LOCK_FD':str(admission),"
            "'FACTORY_QUALIFICATION_CONTROLLER_LOCK':sys.argv[3],"
            "'FACTORY_QUALIFICATION_CONTROLLER_LOCK_FD':str(controller)}\n"
            "module.run([sys.executable,sys.argv[4],sys.argv[5],sys.argv[6]],"
            "'child',environment=env)\n",
            encoding="utf-8",
        )
        parent = subprocess.Popen([
            sys.executable, str(wrapper), str(ROOT / "scripts/release-transaction.py"),
            str(admission), str(controller), str(child),
            str(ROOT / "scripts/attempt-cancel.py"), str(marker),
        ])
        child_pid = None
        try:
            for _ in range(100):
                if marker.exists():
                    child_pid = int(marker.read_text())
                    break
                time.sleep(0.05)
            self.assertIsNotNone(child_pid)
            parent.kill()
            parent.wait(timeout=5)
            for path in (admission, controller):
                probe = os.open(path, os.O_RDWR)
                try:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                finally:
                    os.close(probe)
            os.kill(child_pid, 9)
            child_pid = None
            for path in (admission, controller):
                released = False
                for _ in range(100):
                    probe = os.open(path, os.O_RDWR)
                    try:
                        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        released = True
                        break
                    except BlockingIOError:
                        time.sleep(0.05)
                    finally:
                        os.close(probe)
                self.assertTrue(released)
        finally:
            if parent.poll() is None:
                parent.kill()
                parent.wait(timeout=5)
            if child_pid is not None:
                try:
                    os.kill(child_pid, 9)
                except ProcessLookupError:
                    pass

    def test_no_return_cutover_phases_repair_the_floor_before_replay(self) -> None:
        plan = {
            "approval_sha256": "9" * 64,
            "children": {"host_cutover": []},
        }
        journal = self.kits / "contract-cutover-journal.json"
        floor = self.kits / "contract-floor.json"

        def stop_after_floor(_kits: Path, _plan: dict[str, object]) -> None:
            self.assertTrue(floor.is_file())
            raise RELEASE.ReleaseError("stop after floor repair")

        for phase in (
            "active_records_switched", "contract_floor_committed",
            "launcher_installed", "retired_runtime_removed", "healthy",
        ):
            with self.subTest(phase=phase):
                floor.unlink(missing_ok=True)
                RELEASE.cutover_update(
                    journal, plan["approval_sha256"], phase, [], "in-progress",
                )
                with (
                    mock.patch.object(
                        RELEASE, "require_reservation", side_effect=stop_after_floor,
                    ),
                    self.assertRaisesRegex(RELEASE.ReleaseError, "stop after floor"),
                ):
                    RELEASE._apply_host_cutover_locked(plan, self.root, self.kits)

    def test_completed_cutover_keeps_the_floor_required_during_later_updates(self) -> None:
        journal = self.kits / "contract-cutover-journal.json"
        RELEASE.cutover_update(journal, "8" * 64, "healthy", ["relay"], "pass")
        RELEASE.cutover_update(journal, "9" * 64, "project:nysa", ["nysa"], "in-progress")
        value = RELEASE.safe_state(journal, "cutover journal")
        self.assertTrue(value["floor_required"])

    def test_abort_restores_maintenance_and_clears_only_an_unstarted_reservation(self) -> None:
        approval = "9" * 64
        reservation = self.kits / "contract-cutover-reservation.json"
        RELEASE.atomic_json(reservation, {"placeholder": True})
        product = self.root / "abort-product"
        (product / "factory").mkdir(parents=True)
        marker = product / "factory/MAINTENANCE"
        reservation_id = "8" * 64
        RELEASE.atomic_json(marker, {
            "cutover_owner": reservation_id, "product_path": str(product),
            "project": "relay", "schema_version": 1,
        })
        prior = b'{"incident":"operator"}\n'
        snapshot = self.root / "abort-prior"
        RELEASE.atomic_bytes(snapshot, prior)
        active = self.kits / "projects/relay/active.json"
        RELEASE.atomic_json(active, {"project": "relay"})
        receipt_id = "7" * 64
        item = {
            "maintenance": {
                "cutover_sha256": RELEASE.file_digest(marker),
                "prior": {
                    "path": str(snapshot),
                    "sha256": RELEASE.hashlib.sha256(prior).hexdigest(),
                },
                "reservation_id": reservation_id,
            },
            "product": str(product), "project": "relay",
            "receipt": {"receipt_id": receipt_id},
            "source_active_sha256": RELEASE.file_digest(active),
            "tickets": [],
        }
        plan = {
            "approval_sha256": approval,
            "children": {"host_cutover": [item]},
            "request": {"operator_id": "tester"}, "stage": "prerequisites",
        }
        stored = self.kits / "projects/relay/release-plans" / self.sha / f"{approval}.json"
        RELEASE.atomic_json(stored, plan)
        RELEASE.atomic_json(stored.parent.parent / f"{self.sha}.json", plan)
        args = argparse.Namespace(
            approved_by="tester", kits_root=self.kits, project="relay", sha=self.sha,
        )
        with (
            mock.patch.object(RELEASE, "validate_plan"),
            mock.patch.object(RELEASE, "validate_live_basis"),
            mock.patch.object(RELEASE, "require_reservation"),
            mock.patch.object(RELEASE, "read_cutover", return_value={
                "completed_projects": [], "phase": "approved", "status": "in-progress",
            }),
        ):
            before = marker.read_bytes()
            args.approved_by = "someone-else"
            with self.assertRaisesRegex(RELEASE.ReleaseError, "cannot be aborted"):
                RELEASE.abort(args)
            self.assertEqual(marker.read_bytes(), before)
            self.assertTrue(reservation.exists())
            args.approved_by = "tester"
            result = RELEASE.abort(args)
        self.assertEqual(result["status"], "aborted")
        self.assertEqual(marker.read_bytes(), prior)
        self.assertFalse(reservation.exists())

    def test_failed_preparation_restores_maintenance_and_releases_reservation(self) -> None:
        active = self.kits / "projects/relay/active.json"
        RELEASE.atomic_json(active, {
            "contract_version": "1.9.0", "kit_sha": "7" * 40,
            "product_path": str(self.product), "project": "relay",
        })
        source = RELEASE.active_inventory(self.kits)[0]
        retired = self.retired_runtime(self.root / "home")
        basis = RELEASE.reservation_basis(
            self.kits,
            {
                "action": "reuse", "active_projects": [source],
                "sha256": "5" * 64,
            },
            retired, self.product, "relay", self.sha,
        )
        reservation = {**basis, "approval_sha256": None, "status": "preparing"}
        reservation_path = self.kits / "contract-cutover-reservation.json"
        RELEASE.atomic_json(
            reservation_path,
            {**reservation, "record_sha256": RELEASE.digest(reservation)},
        )
        prior = b'{"incident":"operator"}\n'
        snapshot = (
            self.kits / "contract-cutover-reservations" / basis["reservation_id"]
            / "relay.maintenance"
        )
        RELEASE.atomic_bytes(snapshot, prior)
        marker = self.product / "factory/MAINTENANCE"
        RELEASE.atomic_json(marker, {
            "cutover_owner": basis["reservation_id"],
            "product_path": str(self.product), "project": "relay",
        })
        args = argparse.Namespace(
            kits_root=self.kits, product=self.product, project="relay", sha=self.sha,
        )
        with (
            mock.patch.object(
                RELEASE, "_setup_locked", side_effect=RELEASE.ReleaseError("prepare failed"),
            ),
            self.assertRaisesRegex(RELEASE.ReleaseError, "prepare failed"),
        ):
            RELEASE.setup(args)
        self.assertEqual(marker.read_bytes(), prior)
        self.assertFalse(reservation_path.exists())

    def qualification_plan(self, *, approval_required: bool = True) -> dict[str, object]:
        return RELEASE.seal_plan({
            "approval_required": approval_required,
            "children": {
                "provider_cli": {"action": "reuse", "evidence": {"status": "ready"}},
                "runtime": (
                    {"action": "apply", "plan": {"approval_sha256": "2" * 64}}
                    if approval_required else {
                        "action": "reuse", "evidence": {"status": "ready"},
                    }
                ),
            },
            "created_epoch": 1,
            "expires_epoch": 4_000_000_000,
            "fallback_readiness": {
                "evidence": {"readiness_sha256": "1" * 64},
                "sha256": "1" * 64,
            },
            "identity": {
                "active": {
                    "generation": 1, "kit_sha": "b" * 40,
                    "path": "/tmp/active", "sha256": "3" * 64,
                },
                "authority_sha256": "4" * 64,
                "environment": {"path": "/tmp/environment", "sha256": "5" * 64},
                "operator_identities": {
                    "map_sha256": "7" * 64,
                    "runtime_ledger_sha256": "8" * 64,
                },
                "previous_receipt": {"path": "/tmp/receipt", "sha256": "6" * 64},
                "provider_state": {},
                "selected_tickets": ["T-1"],
            },
            "preview_elapsed_ms": 1,
            "preview_timings": [{"duration_ms": 1, "phase": "validation"}],
            "request": {
                "operator_id": "tester", "product": str(self.product),
                "project": "relay", "repo": str(self.root / "factory"),
                "root": "/private/tmp/nysa-sf-qualification.fixture",
                "runtime_bin": str(self.root / "runtime"), "sha": self.sha,
            },
            "schema": RELEASE.QUALIFICATION_PLAN_SCHEMA,
            "status": "planned",
        })

    def test_qualification_plan_hash_binds_every_migration_input(self) -> None:
        plan = self.qualification_plan()
        RELEASE.validate_qualification_plan(plan)
        mutations = (
            ("identity", "active", {"generation": 2, "kit_sha": "b" * 40}),
            ("request", "runtime_bin", "/tmp/changed"),
            ("children", "runtime", {"action": "reuse", "evidence": {}}),
            ("fallback_readiness", "sha256", "2" * 64),
        )
        for parent, key, value in mutations:
            with self.subTest(field=f"{parent}.{key}"):
                changed = json.loads(json.dumps(plan))
                changed[parent][key] = value
                with self.assertRaisesRegex(
                    RELEASE.ReleaseError, "qualification migration plan is invalid",
                ):
                    RELEASE.validate_qualification_plan(changed)

    def recovery_plan(self) -> dict[str, object]:
        return RELEASE.seal_plan({
            "attempt": {
                "active_claim_sha256": None,
                "dispatch_lease_sha256": "1" * 64,
                "nested_plan": {"preview_hash": "2" * 64},
                "provider_attempt": {"attempt_id": "attempt-1", "version": 4},
                "provider_attempt_sha256": "3" * 64,
                "runtime_ledger_row": {"run_id": "run-1", "ticket": "T-1"},
            },
            "created_epoch": 1,
            "expires_epoch": 4_000_000_000,
            "identity": {
                "candidate_sha": self.sha, "product_sha": "4" * 40,
                "source_sha": "5" * 40,
            },
            "request": {
                "failed_run": "run-1", "operator_id": "tester",
                "product": str(self.product), "project": "relay",
                "repo": str(self.root / "factory"),
                "root": str(self.root / "qualification"), "sha": self.sha,
                "ticket": "T-1",
            },
            "schema": RELEASE.QUALIFICATION_RECOVERY_PLAN_SCHEMA,
            "status": "planned",
        })

    def test_qualification_recovery_plan_binds_exact_attempt(self) -> None:
        plan = self.recovery_plan()
        RELEASE.validate_qualification_recovery_plan(plan)
        for field in ("nested_plan", "provider_attempt", "runtime_ledger_row"):
            with self.subTest(field=field):
                changed = json.loads(json.dumps(plan))
                changed["attempt"][field] = {"changed": True}
                with self.assertRaisesRegex(
                    RELEASE.ReleaseError, "recovery plan is invalid",
                ):
                    RELEASE.validate_qualification_recovery_plan(changed)

    def test_qualification_recovery_uses_source_launcher_account_database(self) -> None:
        lane = {
            "active": {
                "kit_sha": "5" * 40, "project": "relay",
                "runtime_ledger_path": str(self.root / "runtime-ledger.csv"),
            },
            "product": self.product,
            "provider": self.root / "qualification/provider",
            "root": self.root / "qualification",
        }
        environment = RELEASE.qualification_recovery_environment(lane)
        expected = (
            Path(environment["HOME"]) /
            ".factory/accounting/cursor-account-admission-v1.sqlite3"
        )
        self.assertEqual(
            Path(environment["FACTORY_CURSOR_ACCOUNT_DB"]), expected,
        )
        self.assertNotEqual(
            expected, lane["provider"] / "accounting/cursor-account.sqlite3",
        )
        self.assertIn(
            'CURSOR_ACCOUNT_DB="$HOME/.factory/accounting/'
            'cursor-account-admission-v1.sqlite3"',
            (ROOT / "scripts/factory-launch").read_text(encoding="utf-8"),
        )

    def test_qualification_recovery_reuses_validated_existing_cancellation(self) -> None:
        runs = self.product / "factory/runs"
        runs.mkdir(parents=True)
        nested_plan = {"preview_hash": "2" * 64}
        RELEASE.atomic_json(runs / "run-1.cancel-request.json", {
            "plan": nested_plan, "requested_at": "2026-08-23T12:00:00Z",
            "schema": "nysa.software-factory.attempt-cancel-request/v1",
        })
        RELEASE.atomic_json(
            runs / "run-1.cancel.json", {"preview_hash": "2" * 64},
        )
        lane = {
            "active": {
                "kit_sha": "5" * 40, "project": "relay",
                "runtime_ledger_path": str(self.root / "runtime-ledger.csv"),
            },
            "product": self.product, "provider": self.root / "provider",
            "root": self.root / "qualification",
        }
        provider_attempt = {"attempt_id": "attempt-1", "version": 4}
        with (
            mock.patch.object(
                RELEASE, "qualification_attempt_cancel",
                return_value={"preview_hash": "2" * 64},
            ) as cancel,
            mock.patch.object(
                RELEASE, "qualification_recovery_manifest",
                return_value={"provider_attempt_id": "attempt-1", "role": "builder"},
            ),
            mock.patch.object(
                RELEASE, "run_json", return_value={"attempts": [provider_attempt]},
            ),
            mock.patch.object(
                RELEASE, "qualification_recovery_row", return_value={"run_id": "run-1"},
            ),
            mock.patch.object(
                RELEASE, "qualification_recovery_optional_digest", return_value=None,
            ),
        ):
            attempt = RELEASE.qualification_recovery_attempt(
                self.root / "factory", lane, "T-1", "run-1",
            )
        self.assertEqual(attempt["nested_plan"], nested_plan)
        self.assertEqual(attempt["provider_attempt"], provider_attempt)
        self.assertEqual(
            [call.args[2][0] for call in cancel.call_args_list],
            ["request", "receipt"],
        )

    def test_qualification_recovery_accepts_request_only_crash_prefix(self) -> None:
        runs = self.product / "factory/runs"
        runs.mkdir(parents=True)
        nested_plan = {"preview_hash": "2" * 64}
        RELEASE.atomic_json(runs / "run-1.cancel-request.json", {
            "plan": nested_plan, "requested_at": "2026-08-23T12:00:00Z",
            "schema": "nysa.software-factory.attempt-cancel-request/v1",
        })
        lane = {
            "active": {
                "kit_sha": "5" * 40, "project": "relay",
                "runtime_ledger_path": str(self.root / "runtime-ledger.csv"),
            },
            "product": self.product, "provider": self.root / "provider",
            "root": self.root / "qualification",
        }
        provider_attempt = {"attempt_id": "attempt-1", "version": 4}
        with (
            mock.patch.object(
                RELEASE, "qualification_attempt_cancel",
                return_value={"preview_hash": "2" * 64},
            ) as cancel,
            mock.patch.object(
                RELEASE, "qualification_recovery_manifest",
                return_value={"provider_attempt_id": "attempt-1", "role": "builder"},
            ),
            mock.patch.object(
                RELEASE, "run_json", return_value={"attempts": [provider_attempt]},
            ) as provider,
            mock.patch.object(
                RELEASE, "qualification_recovery_row", return_value={"run_id": "run-1"},
            ),
            mock.patch.object(
                RELEASE, "qualification_recovery_optional_digest", return_value=None,
            ),
        ):
            attempt = RELEASE.qualification_recovery_attempt(
                self.root / "factory", lane, "T-1", "run-1",
            )
        self.assertEqual(attempt["nested_plan"], nested_plan)
        self.assertEqual(attempt["provider_attempt"], provider_attempt)
        self.assertEqual(cancel.call_count, 1)
        self.assertEqual(cancel.call_args.args[2][0], "request")
        self.assertEqual(
            provider.call_args.kwargs["environment"]["FACTORY_CROSS_RELEASE_PRODUCT_ID"],
            f"relay:{'5' * 40}",
        )
        self.assertEqual(
            provider.call_args.kwargs["environment"]["FACTORY_DISPATCH_ADMISSION_LOCK"],
            str(lane["root"] / "worktrees/relay/.dispatch-admission.lock"),
        )

    def test_qualification_recovery_carries_provider_only_pre_go_attempt(self) -> None:
        runs = self.product / "factory/runs"
        runs.mkdir(parents=True)
        run_id = "1787640905-99999999-cli"
        provider_attempt = {
            "attempt_id": run_id, "state": "reserved", "version": 2,
        }
        nested_plan = {
            "preview_hash": "2" * 64,
            "provider_attempt": provider_attempt,
            "schema": "nysa.software-factory.provider-only-attempt-cancel-plan/v1",
        }
        lane = {
            "active": {
                "kit_sha": "5" * 40, "project": "relay",
                "runtime_ledger_path": str(self.root / "runtime-ledger.csv"),
            },
            "product": self.product, "provider": self.root / "provider",
            "root": self.root / "qualification",
        }
        with (
            mock.patch.object(
                RELEASE, "qualification_attempt_cancel", return_value=nested_plan,
            ),
            mock.patch.object(
                RELEASE, "run_json", return_value={"attempts": [provider_attempt]},
            ),
            mock.patch.object(
                RELEASE, "qualification_recovery_manifest",
            ) as recovery_manifest,
            mock.patch.object(
                RELEASE, "qualification_recovery_row",
            ) as recovery_row,
            mock.patch.object(
                RELEASE, "qualification_recovery_optional_digest", return_value=None,
            ),
        ):
            attempt = RELEASE.qualification_recovery_attempt(
                self.root / "factory", lane, "T-1", run_id,
            )
        self.assertEqual(attempt["nested_plan"], nested_plan)
        self.assertEqual(attempt["provider_attempt"], provider_attempt)
        self.assertIsNone(attempt["active_claim_sha256"])
        self.assertIsNone(attempt["runtime_ledger_row"])
        recovery_manifest.assert_not_called()
        recovery_row.assert_not_called()

    def test_qualification_recovery_refuses_attempt_drift_before_cancellation(self) -> None:
        plan = self.recovery_plan()
        state = RELEASE.qualification_recovery_state(
            Path(plan["request"]["root"]), "relay", self.sha, "T-1", "run-1",
        )
        RELEASE.atomic_json(state / "latest.json", plan)
        RELEASE.atomic_json(
            state / "plans" / f"{plan['approval_sha256']}.json", plan,
        )
        lane = {
            "controller": self.root / "controller", "product": self.product,
            "root": Path(plan["request"]["root"]),
        }
        module = mock.Mock()
        module.lock_controllers.side_effect = lambda *_: [
            os.open(os.devnull, os.O_RDONLY)
        ]
        module.lock_dispatch_admission.side_effect = lambda *_: [
            os.open(os.devnull, os.O_RDONLY)
        ]
        args = argparse.Namespace(
            approve_hash=plan["approval_sha256"], failed_run="run-1",
            operator_id="tester", product=self.product, project="relay",
            repo=self.root / "factory", root=lane["root"], sha=self.sha,
            ticket="T-1",
        )
        with (
            mock.patch.object(
                RELEASE, "qualification_recovery_identity",
                return_value=(plan["identity"], module, lane, args.repo),
            ),
            mock.patch.object(
                RELEASE, "qualification_recovery_attempt",
                return_value={"changed": True},
            ),
            mock.patch.object(RELEASE, "qualification_attempt_cancel") as cancel,
            self.assertRaisesRegex(RELEASE.ReleaseError, "attempt changed"),
        ):
            RELEASE.qualification_recovery_apply(args)
        cancel.assert_not_called()

    def test_qualification_recovery_refuses_receipt_without_request(self) -> None:
        plan = self.recovery_plan()
        state = RELEASE.qualification_recovery_state(
            Path(plan["request"]["root"]), "relay", self.sha, "T-1", "run-1",
        )
        RELEASE.atomic_json(
            state / "plans" / f"{plan['approval_sha256']}.json", plan,
        )
        runs = self.product / "factory/runs"
        runs.mkdir()
        RELEASE.atomic_json(runs / "run-1.cancel.json", {"preview_hash": "2" * 64})
        lane = {
            "controller": self.root / "controller", "product": self.product,
            "root": Path(plan["request"]["root"]),
        }
        module = mock.Mock()
        module.lock_controllers.side_effect = lambda *_: [
            os.open(os.devnull, os.O_RDONLY)
        ]
        module.lock_dispatch_admission.side_effect = lambda *_: [
            os.open(os.devnull, os.O_RDONLY)
        ]
        args = argparse.Namespace(
            approve_hash=plan["approval_sha256"], failed_run="run-1",
            operator_id="tester", product=self.product, project="relay",
            repo=self.root / "factory", root=lane["root"], sha=self.sha,
            ticket="T-1",
        )
        with (
            mock.patch.object(
                RELEASE, "qualification_recovery_identity",
                return_value=(plan["identity"], module, lane, args.repo),
            ),
            mock.patch.object(RELEASE, "qualification_attempt_cancel") as cancel,
            self.assertRaisesRegex(RELEASE.ReleaseError, "replay is incomplete"),
        ):
            RELEASE.qualification_recovery_apply(args)
        cancel.assert_not_called()

    def test_qualification_recovery_replays_nested_receipt_exactly(self) -> None:
        plan = self.recovery_plan()
        state = RELEASE.qualification_recovery_state(
            Path(plan["request"]["root"]), "relay", self.sha, "T-1", "run-1",
        )
        RELEASE.atomic_json(state / "latest.json", plan)
        RELEASE.atomic_json(
            state / "plans" / f"{plan['approval_sha256']}.json", plan,
        )
        runs = self.product / "factory/runs"
        runs.mkdir()
        RELEASE.atomic_json(runs / "run-1.cancel-request.json", {
            "plan": plan["attempt"]["nested_plan"],
            "requested_at": "2026-08-23T12:00:00Z",
            "schema": "nysa.software-factory.attempt-cancel-request/v1",
        })
        nested_path = runs / "run-1.cancel.json"
        RELEASE.atomic_json(nested_path, {"preview_hash": "2" * 64})
        lane = {
            "controller": self.root / "controller", "product": self.product,
            "root": Path(plan["request"]["root"]),
        }
        module = mock.Mock()
        module.lock_controllers.side_effect = lambda *_: [
            os.open(os.devnull, os.O_RDONLY)
        ]
        module.lock_dispatch_admission.side_effect = lambda *_: [
            os.open(os.devnull, os.O_RDONLY)
        ]
        args = argparse.Namespace(
            approve_hash=plan["approval_sha256"], failed_run="run-1",
            operator_id="tester", product=self.product, project="relay",
            repo=self.root / "factory", root=lane["root"], sha=self.sha,
            ticket="T-1",
        )
        nested = {"accounting_state": "cancelled_conservative",
                  "preview_hash": "2" * 64}
        with (
            mock.patch.object(
                RELEASE, "qualification_recovery_identity",
                return_value=(plan["identity"], module, lane, args.repo),
            ),
            mock.patch.object(
                RELEASE, "qualification_attempt_cancel", return_value=nested,
            ) as cancel,
        ):
            first = RELEASE.qualification_recovery_apply(args)
            second = RELEASE.qualification_recovery_apply(args)
        self.assertEqual(first, second)
        self.assertEqual(first["status"], "recovered")
        self.assertEqual(cancel.call_count, 2)
        self.assertTrue(all(
            call.args[4] >= 0 and call.args[5] >= 0
            for call in cancel.call_args_list
        ))
        self.assertFalse((state / "nested-plan.json").exists())
        module.lock_dispatch_boundaries.assert_not_called()

    def test_factory_kit_forwards_only_sealed_qualification_recovery_arguments(self) -> None:
        self.root.chmod(0o700)
        canonical = self.root / "origin.git"
        subprocess.run(["git", "init", "--bare", "-q", str(canonical)], check=True)
        repo = self.root / "recovery-candidate"
        subprocess.run(["git", "clone", "-q", str(canonical), str(repo)], check=True)
        (repo / "scripts").mkdir()
        (repo / "scripts/release-transaction.py").write_text(
            "import json,os,subprocess,sys\nfrom pathlib import Path\n"
            "if os.environ.get('FACTORY_TEST_HELPER_MARKER'):\n"
            " Path(os.environ['FACTORY_TEST_HELPER_MARKER']).write_text('executed')\n"
            "if any(value.startswith('qualification-recover-') for value in sys.argv):\n"
            " assert all(value not in os.environ for value in "
            "('GH_TOKEN','GH_CONFIG_DIR','GIT_ASKPASS',"
            "'FACTORY_QUALIFICATION_INSTALL_AUTH_DESCRIPTOR'))\n"
            " assert os.environ['PATH'] == '/usr/bin:/bin'\n"
            "if os.environ.get('FACTORY_TRUSTED_TEST_HARNESS') == '1' and "
            "'qualification-upgrade' in sys.argv:\n"
            " assert all(value not in os.environ for value in "
            "('GH_TOKEN','GH_CONFIG_DIR','GIT_ASKPASS'))\n"
            " assert os.environ['PATH'] == '/usr/bin:/bin'\n"
            " assert Path(os.environ['FACTORY_QUALIFICATION_INSTALL_AUTH_DESCRIPTOR']).is_file()\n"
            "print(json.dumps(sys.argv[1:]))\n", encoding="utf-8",
        )
        (repo / "factory-contract.json").write_text(
            '{"contract_version":"2.0.0"}\n', encoding="utf-8",
        )
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
        subprocess.run([
            "git", "-C", str(repo), "-c", "user.name=Factory Test", "-c",
            "user.email=factory@example.invalid", "commit", "-qm", "candidate",
        ], check=True)
        candidate_sha = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        subprocess.run([
            "git", "-C", str(repo), "push", "-q", "-u", "origin", "HEAD:main",
        ], check=True)
        product = self.root / "recovery-product"
        product.mkdir()
        qualification = self.root / "nysa-sf-qualification.fixture"
        qualification.mkdir()
        environment = {
            **os.environ,
            "FACTORY_KIT_CANONICAL_ORIGIN": str(canonical),
            "FACTORY_KIT_TEST_MODE": "1",
            "FACTORY_KITS_ROOT": str(self.root / ".factory/kits"),
            "FACTORY_RELEASE_TEST_HOME": str(self.root),
        }
        common = [
            "--project", "relay", "--root", str(qualification),
            "--product", str(product), "--repo", str(repo), "--sha", candidate_sha,
            "--operator-id", "tester", "--ticket", "T-1",
            "--failed-run", "run-1",
        ]
        runtime = self.root / "runtime"
        runtime.mkdir()
        upgrade_common = [
            "--project", "relay", "--root", str(qualification),
            "--product", str(product), "--repo", str(repo), "--sha", candidate_sha,
            "--operator-id", "tester", "--runtime-bin", str(runtime),
        ]
        config_marker = self.root / "candidate-config-executed"
        config_helper = self.root / "candidate-config-helper"
        config_helper.write_text(
            f"#!/bin/sh\ntouch {str(config_marker)!r}\nexit 1\n", encoding="utf-8",
        )
        config_helper.chmod(0o700)
        attributes = self.root / "candidate-global-attributes"
        attributes.write_text("* filter=attacker\n", encoding="utf-8")
        for key, value in (
            ("core.fsmonitor", str(config_helper)),
            ("core.attributesFile", str(attributes)),
            ("credential.helper", f"!{config_helper}"),
            ("core.sshCommand", str(config_helper)),
            ("filter.attacker.clean", str(config_helper)),
            ("url.file:///tmp/attacker/.insteadOf", str(canonical)),
        ):
            subprocess.run(["git", "-C", str(repo), "config", key, value], check=True)
        planned = subprocess.run(
            ["bash", str(ROOT / "scripts/factory-kit.sh"), "qualification",
             "recover-plan", *common],
            capture_output=True, text=True, env=environment, check=False,
        )
        self.assertEqual(planned.returncode, 0, planned.stderr)
        plan_arguments = json.loads(planned.stdout)
        self.assertIn("qualification-recover-plan", plan_arguments)
        self.assertNotIn("--approve-hash", plan_arguments)

        approval = "b" * 64
        applied = subprocess.run(
            ["bash", str(ROOT / "scripts/factory-kit.sh"), "qualification",
             "recover-apply", *common, "--approve-hash", approval],
            capture_output=True, text=True, env=environment, check=False,
        )
        self.assertEqual(applied.returncode, 0, applied.stderr)
        apply_arguments = json.loads(applied.stdout)
        self.assertIn("qualification-recover-apply", apply_arguments)
        self.assertEqual(apply_arguments[-2:], ["--approve-hash", approval])
        configured_upgrade = subprocess.run(
            ["bash", str(ROOT / "scripts/factory-kit.sh"), "qualification",
             "upgrade", *upgrade_common], capture_output=True, text=True,
            env=environment, check=False,
        )
        self.assertEqual(configured_upgrade.returncode, 0, configured_upgrade.stderr)
        self.assertFalse(config_marker.exists())
        for key in (
            "core.fsmonitor", "credential.helper", "core.sshCommand",
            "core.attributesFile", "filter.attacker.clean",
            "url.file:///tmp/attacker/.insteadOf",
        ):
            subprocess.run([
                "git", "-C", str(repo), "config", "--unset-all", key,
            ], check=True)
        subprocess.run([
            "git", "-C", str(repo), "config", "core.repositoryFormatVersion", "1",
        ], check=True)
        subprocess.run([
            "git", "-C", str(repo), "config", "Extensions.PartialClone", "origin",
        ], check=True)
        partial = subprocess.run(
            ["bash", str(ROOT / "scripts/factory-kit.sh"), "qualification",
             "recover-plan", *common], capture_output=True, text=True,
            env=environment, check=False,
        )
        self.assertNotEqual(partial.returncode, 0)
        self.assertIn("partial or promisor", partial.stderr)
        subprocess.run([
            "git", "-C", str(repo), "config", "--unset-all", "extensions.partialClone",
        ], check=True)
        subprocess.run([
            "git", "-C", str(repo), "config", "core.repositoryFormatVersion", "0",
        ], check=True)
        subprocess.run([
            "git", "-C", str(repo), "config", "extensions.worktreeConfig", "true",
        ], check=True)
        subprocess.run([
            "git", "-C", str(repo), "config", "--worktree",
            "ReMoTe.origin.ProMiSoR", "true",
        ], check=True)
        worktree_partial = subprocess.run(
            ["bash", str(ROOT / "scripts/factory-kit.sh"), "qualification",
             "recover-plan", *common], capture_output=True, text=True,
            env=environment, check=False,
        )
        self.assertNotEqual(worktree_partial.returncode, 0)
        self.assertIn("partial or promisor", worktree_partial.stderr)
        subprocess.run([
            "git", "-C", str(repo), "config", "--worktree", "--unset-all",
            "remote.origin.promisor",
        ], check=True)
        subprocess.run([
            "git", "-C", str(repo), "config", "--unset-all",
            "extensions.worktreeConfig",
        ], check=True)

        malformed = subprocess.run(
            ["bash", str(ROOT / "scripts/factory-kit.sh"), "qualification",
             "recover-apply", *common, "--approve-hash", "short"],
            capture_output=True, text=True, env=environment, check=False,
        )
        self.assertEqual(malformed.returncode, 2)
        self.assertEqual(malformed.stdout, "")

        for option, value in (("--stage", "planning"), ("--priority", "high")):
            with self.subTest(option=option):
                smuggled = subprocess.run(
                    ["bash", str(ROOT / "scripts/factory-kit.sh"), "qualification",
                     "recover-plan", *common, option, value],
                    capture_output=True, text=True, env=environment, check=False,
                )
                self.assertEqual(smuggled.returncode, 2)
                self.assertEqual(smuggled.stdout, "")

        dirty_marker = self.root / "dirty-helper-executed"
        (repo / "scripts/release-transaction.py").write_text(
            f"from pathlib import Path\nPath({str(dirty_marker)!r}).write_text('executed')\n",
            encoding="utf-8",
        )
        dirty = subprocess.run(
            ["bash", str(ROOT / "scripts/factory-kit.sh"), "qualification",
             "recover-plan", *common],
            capture_output=True, text=True, env=environment, check=False,
        )
        self.assertNotEqual(dirty.returncode, 0)
        self.assertIn("candidate must be clean", dirty.stderr)
        self.assertFalse(dirty_marker.exists())
        subprocess.run([
            "git", "-C", str(repo), "restore", "scripts/release-transaction.py",
        ], check=True)
        for flag, clear_flag in (
            ("--assume-unchanged", "--no-assume-unchanged"),
            ("--skip-worktree", "--no-skip-worktree"),
        ):
            hidden_marker = self.root / f"hidden-{flag[2:]}-helper-executed"
            (repo / "scripts/release-transaction.py").write_text(
                f"from pathlib import Path\nPath({str(hidden_marker)!r}).write_text('executed')\n",
                encoding="utf-8",
            )
            subprocess.run([
                "git", "-C", str(repo), "update-index", flag,
                "scripts/release-transaction.py",
            ], check=True)
            hidden = subprocess.run(
                ["bash", str(ROOT / "scripts/factory-kit.sh"), "qualification",
                 "recover-plan", *common],
                capture_output=True, text=True, env=environment, check=False,
            )
            self.assertNotEqual(hidden.returncode, 0)
            self.assertIn("candidate must be clean", hidden.stderr)
            hidden_upgrade = subprocess.run(
                ["bash", str(ROOT / "scripts/factory-kit.sh"), "qualification",
                 "upgrade", *upgrade_common],
                capture_output=True, text=True, env=environment, check=False,
            )
            self.assertNotEqual(hidden_upgrade.returncode, 0)
            self.assertIn("candidate must be clean", hidden_upgrade.stderr)
            self.assertFalse(hidden_marker.exists())
            subprocess.run([
                "git", "-C", str(repo), "update-index", clear_flag,
                "scripts/release-transaction.py",
            ], check=True)
            subprocess.run([
                "git", "-C", str(repo), "restore", "scripts/release-transaction.py",
            ], check=True)

        replacement_marker = self.root / "replacement-helper-executed"
        (repo / "scripts/release-transaction.py").write_text(
            f"from pathlib import Path\nPath({str(replacement_marker)!r}).write_text('executed')\n",
            encoding="utf-8",
        )
        subprocess.run([
            "git", "-C", str(repo), "add", "scripts/release-transaction.py",
        ], check=True)
        subprocess.run([
            "git", "-C", str(repo), "-c", "user.name=Factory Test", "-c",
            "user.email=factory@example.invalid", "commit", "-qm", "replacement",
        ], check=True)
        replacement_sha = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True,
            text=True, check=True,
        ).stdout.strip()
        subprocess.run([
            "git", "-C", str(repo), "reset", "--hard", "-q", candidate_sha,
        ], check=True)
        subprocess.run([
            "git", "-C", str(repo), "replace", candidate_sha, replacement_sha,
        ], check=True)
        for action, arguments in (
            ("recover-plan", common), ("upgrade", upgrade_common),
        ):
            replaced = subprocess.run(
                ["bash", str(ROOT / "scripts/factory-kit.sh"), "qualification",
                 action, *arguments], capture_output=True, text=True,
                env=environment, check=False,
            )
            self.assertEqual(replaced.returncode, 0, replaced.stderr)
            self.assertIn(
                "qualification-recover-plan" if action == "recover-plan"
                else "qualification-upgrade", json.loads(replaced.stdout),
            )
        self.assertFalse(replacement_marker.exists())
        subprocess.run([
            "git", "-C", str(repo), "replace", "-d", candidate_sha,
        ], check=True)

        foreign_origin = self.root / "foreign.git"
        foreign = self.root / "foreign-candidate"
        subprocess.run(["git", "init", "--bare", "-q", str(foreign_origin)], check=True)
        subprocess.run(["git", "clone", "-q", str(foreign_origin), str(foreign)], check=True)
        (foreign / "scripts").mkdir()
        marker = self.root / "foreign-helper-executed"
        (foreign / "scripts/release-transaction.py").write_text(
            f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\n",
            encoding="utf-8",
        )
        (foreign / "factory-contract.json").write_text(
            '{"contract_version":"2.0.0"}\n', encoding="utf-8",
        )
        subprocess.run(["git", "-C", str(foreign), "add", "."], check=True)
        subprocess.run([
            "git", "-C", str(foreign), "-c", "user.name=Factory Test", "-c",
            "user.email=factory@example.invalid", "commit", "-qm", "foreign",
        ], check=True)
        foreign_sha = subprocess.run(
            ["git", "-C", str(foreign), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        foreign_common = [
            "--project", "relay", "--root", str(qualification),
            "--product", str(product), "--repo", str(foreign),
            "--sha", foreign_sha, "--operator-id", "tester",
        ]
        recovery = subprocess.run(
            ["bash", str(ROOT / "scripts/factory-kit.sh"), "qualification",
             "recover-plan", *foreign_common, "--ticket", "T-1",
             "--failed-run", "run-1"],
            capture_output=True, text=True, env=environment, check=False,
        )
        upgrade = subprocess.run(
            ["bash", str(ROOT / "scripts/factory-kit.sh"), "qualification",
             "upgrade", *foreign_common, "--runtime-bin", str(runtime)],
            capture_output=True, text=True, env=environment, check=False,
        )
        self.assertNotEqual(recovery.returncode, 0)
        self.assertNotEqual(upgrade.returncode, 0)
        self.assertIn("wrong kit origin", recovery.stderr)
        self.assertIn("wrong kit origin", upgrade.stderr)
        self.assertFalse(marker.exists())

        subprocess.run([
            "/usr/bin/git", "-C", str(repo), "update-ref",
            "refs/remotes/origin/main", candidate_sha,
        ], check=True)
        subprocess.run([
            "/usr/bin/git", "-C", str(repo), "config",
            "url.file:///tmp/malicious/.insteadOf", str(canonical),
        ], check=True)
        stub_bin = self.root / "hostile-bin"
        stub_bin.mkdir()
        path_marker = self.root / "hostile-path-used"
        for name in ("git", "gh"):
            path_stub = stub_bin / name
            path_stub.write_text(
                f"#!/bin/sh\ntouch {str(path_marker)!r}\nexit 91\n",
                encoding="utf-8",
            )
            path_stub.chmod(0o700)
        auth_config = self.root / "trusted-gh-config"
        auth_config.mkdir(mode=0o700)
        (auth_config / "hosts.yml").write_text("github.com: {}\n", encoding="utf-8")
        (auth_config / "hosts.yml").chmod(0o600)
        token_file = auth_config / "test-token"
        token_file.write_text("config-only-token\n", encoding="utf-8")
        live_file = self.root / "live-main"
        live_file.write_text(candidate_sha + "\n", encoding="utf-8")
        gh_stub = self.root / "trusted-gh"
        gh_stub.write_text(
            "#!/bin/sh\n"
            "[ \"${GH_PROMPT_DISABLED:-}\" = 1 ] || exit 9\n"
            "[ -z \"${XDG_CONFIG_HOME+x}${GH_HTTP_UNIX_SOCKET+x}"
            "${HTTP_PROXY+x}${HTTPS_PROXY+x}${ALL_PROXY+x}${NO_PROXY+x}"
            "${http_proxy+x}${https_proxy+x}${all_proxy+x}${no_proxy+x}"
            "${SSL_CERT_FILE+x}${SSL_CERT_DIR+x}${CURL_CA_BUNDLE+x}"
            "${REQUESTS_CA_BUNDLE+x}\" ] || exit 9\n"
            f"if [ \"$1|$2|$3\" = 'auth|token|--hostname' ] && [ \"$4\" = github.com ]; then\n"
            f" [ \"$GH_CONFIG_DIR\" = {str(auth_config)!r} ] || exit 9\n"
            f" cat {str(token_file)!r}; exit 0\n"
            "fi\n"
            "[ \"${GH_TOKEN:-}\" = config-only-token ] || exit 9\n"
            f"[ \"${{GH_CONFIG_DIR:-}}\" != {str(auth_config)!r} ] || exit 9\n"
            "[ -d \"${GH_CONFIG_DIR:-missing}\" ] || exit 9\n"
            "[ \"$1|$2|$3|$4|$5|$6\" = "
            "'api|--hostname|github.com|repos/nysa-company/software-factory/git/ref/heads/main|--jq|.object.sha' ] || exit 9\n"
            f"cat {str(live_file)!r}\n",
            encoding="utf-8",
        )
        gh_stub.chmod(0o700)
        hostile_environment = {
            **os.environ,
            "FACTORY_KIT_CANONICAL_ORIGIN": str(canonical),
            "FACTORY_KIT_TEST_MODE": "1",
            "FACTORY_TRUSTED_TEST_HARNESS": "1",
            "FACTORY_KIT_TEST_QUALIFICATION_LIVE_MAIN": "1",
            "FACTORY_KIT_TEST_QUALIFICATION_GH": str(gh_stub),
            "FACTORY_KIT_TEST_QUALIFICATION_GH_CONFIG": str(auth_config),
            "FACTORY_KITS_ROOT": environment["FACTORY_KITS_ROOT"],
            "FACTORY_RELEASE_TEST_HOME": str(self.root),
            "GH_HOST": "attacker.invalid",
            "GH_REPO": "attacker/repository",
            "GITHUB_REPOSITORY": "attacker/repository",
            "GH_CONFIG_DIR": "/attacker/gh",
            "XDG_CONFIG_HOME": "/attacker/xdg",
            "GH_HTTP_UNIX_SOCKET": "/attacker/socket",
            "HTTP_PROXY": "http://attacker.invalid",
            "HTTPS_PROXY": "http://attacker.invalid",
            "ALL_PROXY": "http://attacker.invalid",
            "NO_PROXY": "github.com",
            "http_proxy": "http://attacker.invalid",
            "https_proxy": "http://attacker.invalid",
            "all_proxy": "http://attacker.invalid",
            "no_proxy": "github.com",
            "SSL_CERT_FILE": "/attacker/ca.pem",
            "SSL_CERT_DIR": "/attacker/certs",
            "CURL_CA_BUNDLE": "/attacker/curl-ca.pem",
            "REQUESTS_CA_BUNDLE": "/attacker/requests-ca.pem",
            "PATH": f"{stub_bin}:/usr/bin:/bin",
        }
        hostile_environment.pop("GH_TOKEN", None)
        hostile_environment.pop("GITHUB_TOKEN", None)
        authenticated = subprocess.run(
            ["bash", str(ROOT / "scripts/factory-kit.sh"), "qualification",
             "recover-plan", *common],
            capture_output=True, text=True, env=hostile_environment, check=False,
        )
        self.assertEqual(authenticated.returncode, 0, authenticated.stderr)
        authenticated_apply = subprocess.run(
            ["bash", str(ROOT / "scripts/factory-kit.sh"), "qualification",
             "recover-apply", *common, "--approve-hash", approval],
            capture_output=True, text=True, env=hostile_environment, check=False,
        )
        self.assertEqual(authenticated_apply.returncode, 0, authenticated_apply.stderr)
        authenticated_upgrade = subprocess.run(
            ["bash", str(ROOT / "scripts/factory-kit.sh"), "qualification",
             "upgrade", *upgrade_common], capture_output=True, text=True,
            env=hostile_environment, check=False,
        )
        self.assertEqual(authenticated_upgrade.returncode, 0, authenticated_upgrade.stderr)
        self.assertIn("qualification-upgrade", json.loads(authenticated_upgrade.stdout))
        self.assertFalse(path_marker.exists())

        for token in ("", "bad token"):
            with self.subTest(token=token):
                token_file.write_text(token, encoding="utf-8")
                rejected = subprocess.run(
                    ["bash", str(ROOT / "scripts/factory-kit.sh"), "qualification",
                     "recover-plan", *common], capture_output=True, text=True,
                    env=hostile_environment, check=False,
                )
                self.assertNotEqual(rejected.returncode, 0)
                self.assertIn("authentication for qualification is unavailable", rejected.stderr)
        token_file.write_text("config-only-token\n", encoding="utf-8")
        live_file.write_text("d" * 40 + "\n", encoding="utf-8")
        helper_marker = self.root / "live-mismatch-helper-executed"
        mismatch_environment = {
            **hostile_environment, "FACTORY_TEST_HELPER_MARKER": str(helper_marker),
        }
        mismatched = subprocess.run(
            ["bash", str(ROOT / "scripts/factory-kit.sh"), "qualification",
             "recover-plan", *common], capture_output=True, text=True,
            env=mismatch_environment, check=False,
        )
        self.assertNotEqual(mismatched.returncode, 0)
        self.assertIn("does not match live protected main", mismatched.stderr)
        self.assertFalse(helper_marker.exists())
        self.assertFalse(path_marker.exists())

        injected_root = subprocess.run(
            [sys.executable, "-I", str(ROOT / "scripts/qualification-environment.py"),
             "--factory-root", str(repo), "--product-root", str(product),
             "--project", "relay", "--root", str(qualification), "--upgrade",
             "--transaction-root", str(repo)],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(injected_root.returncode, 2)
        self.assertIn("unrecognized arguments: --transaction-root", injected_root.stderr)

    def test_qualification_runtime_change_plans_once_then_reuses_receipt(self) -> None:
        plan = {
            "action": "install", "approval_sha256": "3" * 64,
            "product_path": str(self.product),
            "runtime_bin": str(self.root / "runtime"),
            "target_bin": str(self.root / "project-runtimes/relay/bin"),
        }
        ready = {"path": str(self.root / "project-runtimes/relay/bin"), "status": "ready"}
        (self.root / "factory").mkdir()
        (self.root / "runtime").mkdir()
        (self.root / "project-runtimes/relay").mkdir(parents=True)
        with mock.patch.object(subprocess, "run", return_value=mock.Mock(
            returncode=0, stderr="", stdout=json.dumps(plan),
        )):
            child = RELEASE.qualification_runtime_child(
                self.root / "factory", self.product, self.root, self.kits, "relay",
                self.root / "runtime",
            )
        self.assertEqual(child, {"action": "apply", "plan": plan})

        journal = self.root / "project-runtimes/relay/runtime-pin-journal.json"
        RELEASE.atomic_json(journal, {"plan": plan, "status": "completed"})
        with mock.patch.object(RELEASE, "run_json", return_value=ready):
            child = RELEASE.qualification_runtime_child(
                self.root / "factory", self.product, self.root, self.kits, "relay",
                self.root / "runtime",
            )
        self.assertEqual(
            child, {"action": "reuse", "evidence": ready, "plan": plan},
        )

    def test_qualification_runtime_mismatch_preserves_both_tuples(self) -> None:
        (self.root / "factory").mkdir()
        (self.root / "runtime").mkdir()
        (self.root / "project-runtimes/relay").mkdir(parents=True)
        diagnostic = (
            'ERROR: runtime mismatch for node: expected tuple {"node":"v22"}; '
            'actual tuple {"node":"v25"}\n'
        )
        with (
            mock.patch.object(subprocess, "run", return_value=mock.Mock(
                returncode=1, stderr=diagnostic, stdout="",
            )),
            self.assertRaisesRegex(
                RELEASE.ReleaseError,
                "runtime_tuple_mismatch.*expected tuple.*actual tuple",
            ) as caught,
        ):
            RELEASE.qualification_runtime_child(
                self.root / "factory", self.product, self.root, self.kits,
                "relay", self.root / "runtime",
            )
        self.assertEqual(caught.exception.reason_code, "runtime_tuple_mismatch")

    def test_qualification_changed_host_input_stops_before_lane_mutation(self) -> None:
        repo = self.root / "factory"
        runtime = self.root / "runtime"
        repo.mkdir()
        runtime.mkdir()
        lane = Path(
            "/private/tmp/nysa-sf-qualification."
            + hashlib.sha256(str(self.root).encode()).hexdigest()[:12]
        )
        args = argparse.Namespace(
            kits_root=self.kits, operator_id="tester", product=self.product,
            project="relay", repo=repo, root=lane, runtime_bin=runtime,
            sha=self.sha,
        )
        identity = {
            "active": {"generation": 1, "kit_sha": "b" * 40},
            "factory_origin": "https://github.com/nysa-company/software-factory.git",
            "factory_tree": "c" * 40, "selected_tickets": ["T-1"],
        }
        module = mock.Mock()
        module.qualification_fallback_readiness.return_value = (
            {"readiness_sha256": "1" * 64}, "1" * 64,
        )
        runtime_child = {
            "action": "apply", "plan": {"approval_sha256": "2" * 64},
        }
        with (
            mock.patch.object(
                RELEASE, "qualification_basis", return_value=(identity, module),
            ),
            mock.patch.object(
                RELEASE, "qualification_runtime_child", return_value=runtime_child,
            ),
            mock.patch.object(
                RELEASE, "qualification_install_descriptor", return_value=({
                    "install_repo": str(repo), "descriptor_path": str(repo / "descriptor"),
                }, b"descriptor"),
            ),
            mock.patch.object(
                RELEASE, "clean_identity", return_value=(
                    self.sha, "c" * 40,
                    "https://github.com/nysa-company/software-factory.git",
                ),
            ),
            mock.patch.object(
                RELEASE, "consume_qualification_install_token", return_value={},
            ),
            mock.patch.object(RELEASE, "qualification_provider_child", return_value={
                "action": "reuse", "evidence": {"status": "ready"},
            }),
            mock.patch.object(RELEASE, "run"),
            mock.patch.object(
                RELEASE, "apply_qualification_plan",
                side_effect=AssertionError("approval path mutated the lane"),
            ),
        ):
            result = RELEASE._qualification_upgrade_locked(args)
        self.assertEqual(result["status"], "approval_required")
        self.assertEqual(result["changes"], ["runtime"])
        self.assertFalse(lane.exists())

    def test_qualification_apply_refuses_changed_bound_identity(self) -> None:
        plan = self.qualification_plan()
        state = RELEASE.qualification_state(self.kits, "relay", self.sha)
        RELEASE.secure_directory(state, create=True)
        changed = json.loads(json.dumps(plan["identity"]))
        changed["active"]["generation"] = 2
        with (
            mock.patch.object(
                RELEASE, "qualification_basis", return_value=(changed, mock.Mock()),
            ),
            self.assertRaisesRegex(RELEASE.ReleaseError, "inputs changed"),
        ):
            RELEASE.apply_qualification_plan(plan, self.kits, "tester")

    def test_qualification_published_restart_allows_only_the_exact_transition(self) -> None:
        expected = {
            "active": {
                "generation": 1, "kit_sha": "b" * 40,
                "path": "/tmp/active", "sha256": "1" * 64,
            },
            "authority_sha256": "2" * 64,
            "environment": {"path": "/tmp/environment", "sha256": "3" * 64},
            "operator_identities": {
                "map_sha256": "9" * 64,
                "runtime_ledger_sha256": "a" * 64,
            },
            "previous_receipt": {"path": "/tmp/receipt-1", "sha256": "7" * 64},
            "product_sha": "c" * 40,
        }
        current = json.loads(json.dumps(expected))
        current["active"].update(
            generation=2, kit_sha=self.sha, sha256="4" * 64,
        )
        current["authority_sha256"] = "5" * 64
        current["environment"]["sha256"] = "6" * 64
        current["previous_receipt"] = {
            "path": "/tmp/receipt-2", "sha256": "8" * 64,
        }
        current["operator_identities"]["runtime_ledger_sha256"] = "b" * 64
        self.assertTrue(RELEASE.qualification_basis_matches(
            current, expected, self.sha,
        ))
        current["operator_identities"]["map_sha256"] = "c" * 64
        self.assertFalse(RELEASE.qualification_basis_matches(
            current, expected, self.sha,
        ))
        current["operator_identities"]["map_sha256"] = "9" * 64
        for key, value in (("generation", 3), ("kit_sha", "d" * 40)):
            changed = json.loads(json.dumps(current))
            changed["active"][key] = value
            self.assertFalse(RELEASE.qualification_basis_matches(
                changed, expected, self.sha,
            ))
        current["product_sha"] = "d" * 40
        self.assertFalse(RELEASE.qualification_basis_matches(
            current, expected, self.sha,
        ))

    def test_qualification_runtime_ledger_requires_the_sealed_projection(self) -> None:
        ledger = self.root / "runtime-ledger.csv"
        RELEASE.atomic_bytes(ledger, b"canonical\n")
        completed = mock.Mock(returncode=0, stdout=b"canonical\n")
        with mock.patch.object(RELEASE.subprocess, "run", return_value=completed) as run:
            self.assertEqual(
                RELEASE.qualification_runtime_ledger_digest(
                    self.root, self.product, ledger,
                ),
                hashlib.sha256(b"canonical\n").hexdigest(),
            )
        self.assertIn(str(self.root / "scripts/ledger-view.py"), run.call_args.args[0])
        completed.stdout = b"changed\n"
        with (
            mock.patch.object(RELEASE.subprocess, "run", return_value=completed),
            self.assertRaisesRegex(RELEASE.ReleaseError, "not canonical"),
        ):
            RELEASE.qualification_runtime_ledger_digest(
                self.root, self.product, ledger,
            )
        completed.returncode = 1
        with (
            mock.patch.object(RELEASE.subprocess, "run", return_value=completed),
            self.assertRaisesRegex(RELEASE.ReleaseError, "projection failed"),
        ):
            RELEASE.qualification_runtime_ledger_digest(
                self.root, self.product, ledger,
            )
        with (
            mock.patch.object(
                RELEASE.subprocess, "run",
                side_effect=subprocess.TimeoutExpired("ledger-view", 60),
            ),
            self.assertRaisesRegex(RELEASE.ReleaseError, "projection failed"),
        ):
            RELEASE.qualification_runtime_ledger_digest(
                self.root, self.product, ledger,
            )

    def test_qualification_basis_hashes_the_selected_target_transaction(self) -> None:
        repo = self.root / "factory"
        repo.mkdir()
        repair = self.root / "sealed-repair"
        repair.mkdir()
        target = self.root / "sealed-target"
        target.mkdir()
        with mock.patch.object(RELEASE, "TRANSACTION_ROOT", repair):
            for selected, expected in ((None, repair), (target, target)):
                with (
                    self.subTest(selected=selected),
                    mock.patch.object(
                        RELEASE, "factory_ref_identity",
                        return_value=(
                            self.sha, "b" * 40, "https://example.invalid/factory",
                        ),
                    ) as identity,
                    mock.patch.object(
                        RELEASE, "clean_identity",
                        side_effect=RELEASE.ReleaseError("stop"),
                    ),
                    self.assertRaisesRegex(RELEASE.ReleaseError, "stop"),
                ):
                    RELEASE.qualification_basis(
                        "relay", Path("/private/tmp/nysa-sf-qualification.fixture"),
                        self.product, repo, self.sha, selected,
                    )
                identity.assert_called_once_with(repo, "Factory candidate", expected)

    def test_factory_identity_hashes_the_selected_transaction_directory(self) -> None:
        repo = self.root / "factory"
        repo.mkdir()
        target = self.root / "sealed-target"
        target.mkdir()
        tree = "b" * 40
        with (
            mock.patch.object(RELEASE, "git", side_effect=[
                str(repo), self.sha, "https://example.invalid/factory",
            ]),
            mock.patch.object(
                RELEASE, "factory_object_git", return_value=mock.Mock(stdout=tree),
            ),
            mock.patch.object(
                RELEASE, "directory_git_tree", return_value=tree,
            ) as directory_tree,
            mock.patch.object(RELEASE, "factory_worktree_tree", return_value=tree),
        ):
            self.assertEqual(
                RELEASE.factory_ref_identity(repo, "Factory candidate", target),
                (self.sha, tree, "https://example.invalid/factory"),
            )
        directory_tree.assert_called_once_with(target)

    def test_qualification_repair_accepts_only_a_protected_main_ancestor(self) -> None:
        repo = self.root / "factory"
        repo.mkdir()
        target = self.root / "sealed-target"
        target.mkdir()
        product_sha = "c" * 40

        def git(root: Path, *arguments: str) -> str:
            if arguments == ("rev-parse", "refs/remotes/origin/main"):
                return protected if root == repo else protected_product
            if arguments == ("merge-base", self.sha, protected):
                return merge_base
            raise AssertionError((root, arguments))

        for transaction, protected, protected_product, merge_base, error in (
            (RELEASE.TRANSACTION_ROOT, self.sha, product_sha, self.sha, "stop"),
            (RELEASE.TRANSACTION_ROOT, "d" * 40, product_sha, self.sha,
             "exact protected main"),
            (target, "d" * 40, product_sha, self.sha, "stop"),
            (target, "d" * 40, product_sha, "e" * 40, "exact protected main"),
            (target, "d" * 40, "e" * 40, self.sha, "exact protected main"),
        ):
            with (
                self.subTest(
                    transaction=transaction, protected=protected,
                    protected_product=protected_product, merge_base=merge_base,
                ),
                mock.patch.dict(os.environ, {"FACTORY_KIT_TEST_MODE": "0"}),
                mock.patch.object(
                    RELEASE, "factory_ref_identity",
                    return_value=(self.sha, "b" * 40, "https://example.invalid/factory"),
                ),
                mock.patch.object(
                    RELEASE, "clean_identity",
                    return_value=(product_sha, "f" * 40, "https://example.invalid/product"),
                ),
                mock.patch.object(RELEASE, "git", side_effect=git),
                mock.patch.object(
                    RELEASE, "secure_regular_bytes",
                    side_effect=RELEASE.ReleaseError("stop"),
                ),
                self.assertRaisesRegex(RELEASE.ReleaseError, error),
            ):
                RELEASE.qualification_basis(
                    "relay", Path("/private/tmp/nysa-sf-qualification.fixture"),
                    self.product, repo, self.sha, transaction,
                )

    def test_qualification_restart_resumes_the_signed_in_progress_plan(self) -> None:
        plan = self.qualification_plan(approval_required=False)
        (self.root / "factory").mkdir()
        (self.root / "runtime").mkdir()
        state = RELEASE.qualification_state(self.kits, "relay", self.sha)
        RELEASE.secure_directory(state, create=True)
        RELEASE.atomic_json(state / "latest.json", plan)
        RELEASE.qualification_journal_update(
            state / "journal.json", plan, "environment_upgraded",
            plan["preview_timings"],
        )
        args = argparse.Namespace(
            kits_root=self.kits, operator_id="tester", product=self.product,
            project="relay", repo=self.root / "factory",
            root=Path(plan["request"]["root"]), runtime_bin=self.root / "runtime",
            sha=self.sha,
        )
        with (
            mock.patch.object(
                RELEASE, "qualification_basis",
                side_effect=AssertionError("restart must not preview a new identity"),
            ),
            mock.patch.object(
                RELEASE, "apply_qualification_plan",
                return_value={"status": "doctor_ready"},
            ) as applied,
        ):
            result = RELEASE._qualification_upgrade_locked(args)
        self.assertEqual(result["status"], "doctor_ready")
        applied.assert_called_once_with(
            plan, self.kits, None, started=None,
        )

    def test_qualification_reuse_refuses_provider_evidence_drift(self) -> None:
        plan = self.qualification_plan(approval_required=False)
        state = RELEASE.qualification_state(self.kits, "relay", self.sha)
        RELEASE.secure_directory(state, create=True)
        with (
            mock.patch.object(
                RELEASE, "qualification_basis",
                return_value=(plan["identity"], mock.Mock()),
            ),
            mock.patch.object(RELEASE, "run_json", side_effect=[
                {"path": "runtime", "status": "ready"},
                {"receipt_sha256": "3" * 64, "status": "ready"},
            ]),
            self.assertRaisesRegex(
                RELEASE.ReleaseError, "provider CLI evidence changed",
            ),
        ):
            RELEASE.apply_qualification_plan(plan, self.kits, None)

    def test_qualification_restart_uses_furthest_signed_phase_once(self) -> None:
        plan = self.qualification_plan(approval_required=False)
        state = RELEASE.qualification_state(self.kits, "relay", self.sha)
        RELEASE.secure_directory(state, create=True)
        historical = [
            *plan["preview_timings"],
            {"duration_ms": 59_000, "phase": "provider_cli"},
        ]
        for phase in (
            "validated", "runtime_ready", "provider_cli_ready",
            "validated", "runtime_ready", "provider_cli_ready",
        ):
            RELEASE.qualification_journal_update(
                state / "journal.json", plan, phase, historical,
            )
        signed = RELEASE.safe_state(
            state / "journal.json", "qualification migration journal",
        )
        unsigned = dict(signed)
        unsigned.pop("record_sha256")
        RELEASE.atomic_json(state / "journal.json", unsigned)
        with self.assertRaisesRegex(
            RELEASE.ReleaseError, "journal is invalid",
        ):
            RELEASE.apply_qualification_plan(plan, self.kits, None)
        RELEASE.atomic_json(state / "journal.json", signed)
        target = json.loads(json.dumps(plan["identity"]))
        target["active"].update(
            generation=2, kit_sha=self.sha, sha256="7" * 64,
        )
        target["authority_sha256"] = "8" * 64
        target["environment"]["sha256"] = "9" * 64
        target["previous_receipt"]["sha256"] = "a" * 64
        module = mock.Mock()
        module.qualification_lane.return_value = self.root / "lane"
        doctor = {
            "checks": {}, "overall_status": "ok",
            "schema": "nysa.software-factory.doctor/v2",
        }
        upgraded = mock.Mock(
            returncode=0,
            stdout=json.dumps({"launcher": "/tmp/factory-launch", "status": "upgraded"}),
        )
        journals = []
        journal_update = RELEASE.qualification_journal_update

        def capture_journal(*args: object) -> dict[str, object]:
            value = journal_update(*args)
            journals.append(value)
            return value

        with (
            mock.patch.object(
                RELEASE, "qualification_basis",
                side_effect=[(target, module), (target, module), (target, module)],
            ),
            mock.patch.object(
                RELEASE, "qualification_fallback",
                return_value=(plan["fallback_readiness"]["evidence"],
                              plan["fallback_readiness"]["sha256"]),
            ),
            mock.patch.object(RELEASE.subprocess, "run", return_value=upgraded) as process,
            mock.patch.object(RELEASE, "run_json", return_value=doctor) as run_json,
            mock.patch.object(
                RELEASE, "qualification_journal_update", side_effect=capture_journal,
            ),
        ):
            receipt = RELEASE.apply_qualification_plan(
                plan, self.kits, None, repair_sha="d" * 40,
            )
        process.assert_called_once()
        run_json.assert_called_once()
        self.assertEqual(receipt["repair_sha"], "d" * 40)
        self.assertLess(receipt["total_duration_ms"], RELEASE.QUALIFICATION_BUDGET_MS)
        self.assertEqual(receipt["timings"][:len(historical)], historical)
        self.assertEqual(receipt["journal_sha256"], journals[-1]["record_sha256"])
        self.assertEqual(journals[-1]["phase"], "doctor_ready")
        self.assertEqual(journals[-1]["repair_sha"], "d" * 40)
        self.assertFalse((state / "journal.json").exists())
        self.assertFalse((state / ".migration.lock").exists())
        self.assertEqual(
            RELEASE.apply_qualification_plan(
                plan, self.kits, None, repair_sha="d" * 40,
            ),
            receipt,
        )
        with self.assertRaisesRegex(
            RELEASE.ReleaseError, "completion is invalid",
        ):
            RELEASE.apply_qualification_plan(
                plan, self.kits, None, repair_sha="e" * 40,
            )
        self.assertFalse((state / ".migration.lock").exists())

    def test_qualification_repair_supersedes_only_at_authenticated_boundaries(self) -> None:
        plan = self.qualification_plan(approval_required=False)
        state = RELEASE.qualification_state(self.kits, "relay", self.sha)
        RELEASE.secure_directory(state, create=True)
        journal_path = state / "journal.json"
        for phase in ("validated", "runtime_ready", "provider_cli_ready"):
            RELEASE.qualification_journal_update(
                journal_path, plan, phase, plan["preview_timings"],
            )
        journal = RELEASE.safe_state(journal_path, "qualification migration journal")
        journal["repair_sha"] = "d" * 40
        RELEASE.atomic_json(journal_path, RELEASE.signed_journal(journal))
        module = mock.Mock()
        with (
            mock.patch.object(
                RELEASE, "qualification_basis",
                return_value=(plan["identity"], module),
            ),
            mock.patch.object(
                RELEASE, "qualification_fallback",
                side_effect=RELEASE.ReleaseError("stop"),
            ),
            self.assertRaisesRegex(RELEASE.ReleaseError, "stop"),
        ):
            RELEASE.apply_qualification_plan(
                plan, self.kits, None, repair_sha="e" * 40,
            )
        self.assertEqual(
            RELEASE.read_qualification_journal(journal_path, plan)["repair_sha"],
            "e" * 40,
        )

        transitioned = json.loads(json.dumps(plan["identity"]))
        transitioned["active"].update(
            generation=2, kit_sha=self.sha, sha256="7" * 64,
        )
        transitioned["authority_sha256"] = "8" * 64
        transitioned["environment"]["sha256"] = "9" * 64
        transitioned["previous_receipt"]["sha256"] = "a" * 64
        transitioned["operator_identities"]["runtime_ledger_sha256"] = "b" * 64
        with (
            mock.patch.object(
                RELEASE, "qualification_basis", return_value=(transitioned, module),
            ),
            self.assertRaisesRegex(RELEASE.ReleaseError, "repair helper changed"),
        ):
            RELEASE.apply_qualification_plan(
                plan, self.kits, None, repair_sha="f" * 40,
            )
        self.assertEqual(
            RELEASE.read_qualification_journal(journal_path, plan)["repair_sha"],
            "e" * 40,
        )
        RELEASE.qualification_journal_update(
            journal_path, plan, "environment_upgraded", plan["preview_timings"],
        )
        with (
            mock.patch.object(
                RELEASE, "qualification_basis",
                return_value=(plan["identity"], module),
            ),
            self.assertRaisesRegex(RELEASE.ReleaseError, "repair helper changed"),
        ):
            RELEASE.apply_qualification_plan(
                plan, self.kits, None, repair_sha="f" * 40,
            )
        self.assertEqual(
            RELEASE.read_qualification_journal(journal_path, plan)["repair_sha"],
            "e" * 40,
        )
        with (
            mock.patch.object(
                RELEASE, "qualification_basis", return_value=(transitioned, module),
            ),
            mock.patch.object(
                RELEASE, "run_json", side_effect=RELEASE.ReleaseError("stop"),
            ),
            mock.patch.object(RELEASE, "secure_regular_bytes", return_value=b""),
            self.assertRaisesRegex(RELEASE.ReleaseError, "stop"),
        ):
            RELEASE.apply_qualification_plan(
                plan, self.kits, None, repair_sha="f" * 40,
            )
        self.assertEqual(
            RELEASE.read_qualification_journal(journal_path, plan)["repair_sha"],
            "f" * 40,
        )

        RELEASE.qualification_journal_update(
            journal_path, plan, "doctor_ready", plan["preview_timings"],
        )
        journal = RELEASE.safe_state(journal_path, "qualification migration journal")
        journal["repair_sha"] = "d" * 40
        RELEASE.atomic_json(journal_path, RELEASE.signed_journal(journal))
        with (
            mock.patch.object(
                RELEASE, "qualification_basis", return_value=(transitioned, module),
            ),
            mock.patch.object(
                RELEASE, "run_json", side_effect=RELEASE.ReleaseError("stop"),
            ),
            mock.patch.object(RELEASE, "secure_regular_bytes", return_value=b""),
            self.assertRaisesRegex(RELEASE.ReleaseError, "stop"),
        ):
            RELEASE.apply_qualification_plan(
                plan, self.kits, None, repair_sha="e" * 40,
            )
        self.assertEqual(
            RELEASE.read_qualification_journal(journal_path, plan)["repair_sha"],
            "e" * 40,
        )

    def test_qualification_restart_refuses_journal_ahead_of_live_activation(self) -> None:
        plan = self.qualification_plan(approval_required=False)
        state = RELEASE.qualification_state(self.kits, "relay", self.sha)
        RELEASE.secure_directory(state, create=True)
        for phase in (
            "validated", "runtime_ready", "provider_cli_ready", "environment_upgraded",
        ):
            RELEASE.qualification_journal_update(
                state / "journal.json", plan, phase, plan["preview_timings"],
            )
        module = mock.Mock()
        with (
            mock.patch.object(
                RELEASE, "qualification_basis",
                return_value=(plan["identity"], module),
            ),
            mock.patch.object(
                RELEASE, "qualification_fallback",
                return_value=(plan["fallback_readiness"]["evidence"],
                              plan["fallback_readiness"]["sha256"]),
            ),
            self.assertRaisesRegex(
                RELEASE.ReleaseError, "journal exceeds live activation",
            ),
        ):
            RELEASE.apply_qualification_plan(plan, self.kits, None)

    def test_qualification_timer_restart_ignores_historical_budget(self) -> None:
        timer = RELEASE.QualificationTimer(
            [{"duration_ms": RELEASE.QUALIFICATION_BUDGET_MS + 1,
              "phase": "runtime"}],
            0,
        )
        timer.check()
        self.assertEqual(timer.current_timings, [])
        timer.started -= 61
        with self.assertRaisesRegex(
            RELEASE.ReleaseError, "exceeded 60 seconds during current",
        ):
            timer.phase("current", lambda: None)

    def test_qualification_fallback_preserves_the_typed_reason(self) -> None:
        module = mock.Mock()
        refusal = ValueError("qualification fallback refused: runtime_tuple_mismatch")
        refusal.reason_code = "runtime_tuple_mismatch"
        module.qualification_fallback_readiness.side_effect = refusal
        with self.assertRaisesRegex(
            RELEASE.ReleaseError, "runtime_tuple_mismatch",
        ) as caught:
            RELEASE.qualification_fallback(
                module, self.root, self.root, "relay", self.product,
                self.root, 1,
            )
        self.assertEqual(caught.exception.reason_code, "runtime_tuple_mismatch")

    def test_qualification_fault_injection_names_each_material_phase(self) -> None:
        for phase in ("runtime", "provider_cli", "environment_upgrade", "doctor"):
            with (
                self.subTest(phase=phase),
                mock.patch.dict(os.environ, {
                    "FACTORY_KIT_TEST_MODE": "1",
                    "FACTORY_TRUSTED_TEST_HARNESS": "1",
                    "FACTORY_QUALIFICATION_MIGRATION_FAIL_AFTER": phase,
                }),
                self.assertRaisesRegex(
                    RELEASE.ReleaseError,
                    f"injected qualification migration failure after {phase}",
                ),
            ):
                RELEASE.qualification_fail_after(phase)

    def test_qualification_resume_uses_the_exact_internal_plan(self) -> None:
        plan = self.qualification_plan()
        state = RELEASE.qualification_state(self.kits, "relay", self.sha)
        RELEASE.secure_directory(state, create=True)
        RELEASE.atomic_json(state / "latest.json", plan)
        args = argparse.Namespace(
            approved_by="other",
            kits_root=self.kits, project="relay", sha=self.sha,
        )
        with self.assertRaisesRegex(RELEASE.ReleaseError, "does not match"):
            RELEASE._qualification_resume_locked(args)
        args.approved_by = "tester"
        stale = self.qualification_plan()
        stale["expires_epoch"] = 2
        stale = RELEASE.seal_plan({
            key: value for key, value in stale.items() if key != "approval_sha256"
        })
        RELEASE.atomic_json(state / "latest.json", stale)
        with self.assertRaisesRegex(RELEASE.ReleaseError, "stale"):
            RELEASE._qualification_resume_locked(args)
        RELEASE.atomic_json(state / "latest.json", plan)
        with mock.patch.object(
            RELEASE, "apply_qualification_plan", return_value={"status": "doctor_ready"},
        ) as applied:
            self.assertEqual(
                RELEASE._qualification_resume_locked(args)["status"], "doctor_ready",
            )
        applied.assert_called_once_with(
            plan, self.kits, "tester", repair_sha=None,
        )

        repair_plan = self.qualification_plan(approval_required=False)
        RELEASE.atomic_json(state / "latest.json", repair_plan)
        RELEASE.qualification_journal_update(
            state / "journal.json", repair_plan, "provider_cli_ready",
            repair_plan["preview_timings"],
        )
        repair_sha = "d" * 40
        repair_release = self.kits / "releases" / repair_sha
        repair_release.mkdir(parents=True)
        args.repair_sha = repair_sha
        with (
            mock.patch.object(RELEASE, "TRANSACTION_ROOT", repair_release),
            mock.patch.object(
                RELEASE, "apply_qualification_plan",
                return_value={"status": "doctor_ready"},
            ) as repaired,
        ):
            self.assertEqual(
                RELEASE._qualification_resume_locked(args)["status"], "doctor_ready",
            )
        repaired.assert_called_once_with(
            repair_plan, self.kits, "tester", repair_sha=repair_sha,
        )
        unsigned_receipt = {
            "active_sha256": "1" * 64,
            "approval_sha256": repair_plan["approval_sha256"],
            "doctor_sha256": "2" * 64,
            "environment_sha256": "3" * 64,
            "factory_sha": self.sha,
            "generation": 2,
            "journal_sha256": "4" * 64,
            "project": "relay",
            "repair_sha": repair_sha,
            "schema": RELEASE.QUALIFICATION_RECEIPT_SCHEMA,
            "slowest_phase": {"duration_ms": 1, "phase": "doctor"},
            "status": "doctor_ready",
            "timings": [],
            "total_duration_ms": 1,
        }
        receipt = {
            **unsigned_receipt,
            "completion_sha256": RELEASE.digest(unsigned_receipt),
        }
        RELEASE.atomic_json(state / "completion.json", receipt)
        (state / "journal.json").unlink()
        with (
            mock.patch.object(RELEASE, "TRANSACTION_ROOT", repair_release),
            mock.patch.object(
                RELEASE, "apply_qualification_plan", return_value=receipt,
            ) as replayed,
        ):
            self.assertEqual(RELEASE._qualification_resume_locked(args), receipt)
        replayed.assert_called_once_with(
            repair_plan, self.kits, "tester", repair_sha=repair_sha,
        )
        foreign = json.loads(json.dumps(repair_plan))
        foreign.pop("approval_sha256")
        foreign["request"]["project"] = "other"
        RELEASE.atomic_json(state / "latest.json", RELEASE.seal_plan(foreign))
        args.repair_sha = None
        with self.assertRaisesRegex(
            RELEASE.ReleaseError, "resume target changed",
        ):
            RELEASE._qualification_resume_locked(args)

    def test_qualification_completion_replay_returns_identical_receipt(self) -> None:
        plan = self.qualification_plan(approval_required=False)
        state = RELEASE.qualification_state(self.kits, "relay", self.sha)
        RELEASE.secure_directory(state, create=True)
        unsigned = {
            "active_sha256": "2" * 64, "approval_sha256": plan["approval_sha256"],
            "doctor_sha256": "3" * 64, "environment_sha256": "4" * 64,
            "factory_sha": self.sha, "generation": 2, "project": "relay",
            "schema": RELEASE.QUALIFICATION_RECEIPT_SCHEMA,
            "slowest_phase": {"duration_ms": 1, "phase": "doctor"},
            "status": "doctor_ready", "timings": [], "total_duration_ms": 1,
        }
        receipt = {**unsigned, "completion_sha256": RELEASE.digest(unsigned)}
        RELEASE.atomic_json(state / "completion.json", receipt)
        before = (state / "completion.json").read_bytes()
        self.assertEqual(RELEASE.apply_qualification_plan(plan, self.kits, None), receipt)
        self.assertEqual((state / "completion.json").read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
