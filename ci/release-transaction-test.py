#!/usr/bin/env python3
"""Focused two-command release transaction regressions."""

from __future__ import annotations

import argparse
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
        order = []
        with (
            mock.patch.dict(os.environ, {"FACTORY_KIT_CERTIFICATION_NETWORK_REVIEWED": "1"}),
            mock.patch.object(RELEASE, "clean_identity", side_effect=[
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
                return_value=self.retired_runtime(self.root / "home"),
            ),
            mock.patch.object(RELEASE, "capacity", return_value=2),
            mock.patch.object(RELEASE, "child_plan", return_value=(concurrency, cli)),
        ):
            plan = RELEASE.setup(args)
        self.assertEqual(plan["stage"], "prerequisites")
        self.assertEqual(plan["children"]["provider_concurrency"], concurrency)
        self.assertTrue(plan["request"]["skip_optional_tests"])
        self.assertTrue(RELEASE.plan_request(plan, self.kits).skip_optional_tests)
        self.assertEqual(order, ["runtime", "preflight"])
        self.assertNotIn(
            "FACTORY_KIT_CERTIFICATION_NETWORK_REVIEWED",
            run.call_args_list[0].kwargs["environment"],
        )
        RELEASE.validate_plan(plan)

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

    def test_controller_enable_loads_the_bound_job_before_dispatch(self) -> None:
        home = self.root / "home"
        (home / "Library/LaunchAgents").mkdir(parents=True)
        controller_path = home / "Library/LaunchAgents/com.factory.controller.relay.plist"
        with mock.patch.object(RELEASE.Path, "home", return_value=home):
            raw = RELEASE.controller_payload("relay", self.product)
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
            subprocess.CompletedProcess([], 1),
        ]
        with (
            mock.patch.object(RELEASE.sys, "platform", "darwin"),
            mock.patch.object(RELEASE, "service_prefix", return_value=(["launchctl"], "gui/1")),
            mock.patch.object(RELEASE.subprocess, "run", side_effect=statuses),
            mock.patch.object(RELEASE, "run") as bootout,
        ):
            RELEASE.unload_service(value)
        bootout.assert_called_once()
        with (
            mock.patch.object(RELEASE.sys, "platform", "darwin"),
            mock.patch.object(RELEASE, "service_prefix", return_value=(["launchctl"], "gui/1")),
            mock.patch.object(RELEASE.subprocess, "run", side_effect=[
                subprocess.CompletedProcess([], 0), subprocess.CompletedProcess([], 0),
            ]),
            mock.patch.object(RELEASE, "run"),
            self.assertRaisesRegex(RELEASE.ReleaseError, "did not unload"),
        ):
            RELEASE.unload_service(value)

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
        finally:
            RELEASE.release_cutover_lock(descriptor)

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


if __name__ == "__main__":
    unittest.main()
