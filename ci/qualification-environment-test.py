#!/usr/bin/env python3
"""Focused sealed qualification-environment test."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock
import json


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "qualification_environment", ROOT / "scripts/qualification-environment.py"
)
assert SPEC and SPEC.loader
ENVIRONMENT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ENVIRONMENT)


def run(root: Path, *arguments: str) -> str:
    return subprocess.run(
        arguments, cwd=root, text=True, capture_output=True, check=True,
    ).stdout.strip()


class QualificationEnvironmentTest(unittest.TestCase):
    def setUp(self) -> None:
        if not Path("/private/tmp").is_dir():
            self.skipTest("qualification trust root is macOS-only")
        self.workspace = Path(tempfile.mkdtemp(prefix="qualification-test."))
        self.root = Path(tempfile.mkdtemp(
            prefix="nysa-sf-qualification.q-", dir="/private/tmp",
        )).resolve()
        os.chmod(self.root, 0o700)
        self.factory = self.workspace / "factory"
        (self.factory / "integrations/hermes/bin").mkdir(parents=True)
        (self.factory / "integrations/hermes/contract.json").write_text(
            '{"contract_version":"1.8.0"}\n', encoding="utf-8",
        )
        launcher = self.factory / "integrations/hermes/bin/factory-launch"
        launcher.write_text("#!/bin/sh\n", encoding="utf-8")
        launcher.chmod(0o755)
        (self.factory / "scripts/model-routing").mkdir(parents=True)
        shutil.copy2(
            ROOT / "scripts/provider-activation.py",
            self.factory / "scripts/provider-activation.py",
        )
        shutil.copy2(
            ROOT / "scripts/provider-coordinator.py",
            self.factory / "scripts/provider-coordinator.py",
        )
        shutil.copy2(
            ROOT / "scripts/ticket-passport.py",
            self.factory / "scripts/ticket-passport.py",
        )
        (self.factory / "scripts/lib").mkdir()
        shutil.copy2(
            ROOT / "scripts/certification-preflight.py",
            self.factory / "scripts/certification-preflight.py",
        )
        shutil.copy2(
            ROOT / "scripts/lib/certification_plan.py",
            self.factory / "scripts/lib/certification_plan.py",
        )
        shutil.copy2(
            ROOT / "scripts/lib/role_output.py",
            self.factory / "scripts/lib/role_output.py",
        )
        (self.factory / "scripts/model-routing/catalog-v1.json").write_text(
            json.dumps({
                "routes": [{
                    "account_route_id": "cursor",
                    "adapter": "cursor-openai",
                    "enabled": True,
                    "provider_family": "openai",
                    "route_id": "cursor-test",
                    "selection_id": "test-model",
                }],
            }) + "\n",
            encoding="utf-8",
        )
        run(self.factory, "git", "init", "-q", "-b", "main")
        run(self.factory, "git", "config", "user.name", "Test")
        run(self.factory, "git", "config", "user.email", "test@example.invalid")
        run(self.factory, "git", "add", ".")
        run(self.factory, "git", "commit", "-qm", "candidate")
        self.sha = run(self.factory, "git", "rev-parse", "HEAD")

        self.product = self.workspace / "product"
        (self.product / "factory").mkdir(parents=True)
        (self.product / "factory/KIT_PIN").write_text(
            self.sha + "\n", encoding="utf-8",
        )
        (self.product / "factory/certification-plan.json").write_text(
            json.dumps({
                "phases": [{
                    "artifacts": [],
                    "command": ["true"],
                    "depends_on": [],
                    "name": "control",
                    "network": "denied",
                }],
                "runtime": {
                    "node": run(self.workspace, "node", "--version"),
                    "npm": run(self.workspace, "npm", "--version"),
                },
                "schema": "nysa.software-factory.certification-plan/v2",
            }) + "\n",
            encoding="utf-8",
        )
        run(self.product, "git", "init", "-q", "-b", "main")
        run(self.product, "git", "config", "user.name", "Test")
        run(self.product, "git", "config", "user.email", "test@example.invalid")
        run(self.product, "git", "remote", "add", "origin", "git@example.invalid")
        run(self.product, "git", "add", ".")
        run(self.product, "git", "commit", "-qm", "product")

    def tearDown(self) -> None:
        for base, directories, files in os.walk(self.root, topdown=False):
            for name in files:
                (Path(base) / name).chmod(0o600)
            for name in directories:
                (Path(base) / name).chmod(0o700)
        shutil.rmtree(self.root)
        shutil.rmtree(self.workspace)

    def test_prepares_exact_read_only_candidate_once(self) -> None:
        args = argparse.Namespace(
            factory_root=self.factory,
            product_root=self.product,
            project="relay",
            root=self.root,
        )
        value = ENVIRONMENT.prepare(args)
        release = Path(value["launcher"]).parents[3]
        self.assertEqual(value["factory_sha"], self.sha)
        self.assertEqual(
            value["product_sha"], run(self.product, "git", "rev-parse", "HEAD")
        )
        self.assertEqual(value["runtime_tuple"]["factory_sha"], self.sha)
        self.assertEqual(ENVIRONMENT.git_tree(release), value["factory_tree"])
        self.assertFalse(release.stat().st_mode & 0o222)
        self.assertEqual(
            (self.root / "profile/projects/relay.env").read_text(),
            f"PRODUCT_ROOT={self.product.resolve()}\n",
        )
        self.assertEqual(
            json.loads((self.root / "marker.json").read_text()),
            {
                "mode": "qualification",
                "schema": "nysa.software-factory.qualification-environment/v1",
            },
        )
        status = json.loads(run(
            self.root,
            "/usr/bin/python3",
            str(release / "scripts/provider-activation.py"),
            "--config", str(self.root / "provider/provider-activation.json"),
            "--policy", str(self.root / "provider/provider-policy.json"),
            "--contract-version", "1.8.0",
            "--status",
        ))
        self.assertEqual(status["execution_mode"], "cli-concurrent-v1")
        launcher_text = (
            ROOT / "integrations/hermes/bin/factory-launch"
        ).read_text(encoding="utf-8")
        self.assertIn(
            'PROVIDER_STATE_ROOT="$QUALIFICATION_ROOT/provider"', launcher_text
        )
        self.assertIn(
            '"FACTORY_CLI_LANE_ROOT=$QUALIFICATION_ROOT"',
            launcher_text,
        )
        self.assertIn(
            '"FACTORY_PROVIDER_PRODUCT_ID=$PROJECT:$KIT_SHA"',
            launcher_text,
        )
        runner_text = (ROOT / "scripts/run-agent.sh").read_text(encoding="utf-8")
        self.assertIn(
            '"$FACTORY_PROVIDER_PRODUCT_ID" != "$TRANSITION_PROJECT:$FACTORY_KIT_SHA"',
            runner_text,
        )
        self.assertIn(
            '"${FACTORY_CLI_LANE_ROOT:-}" != /*', runner_text,
        )
        self.assertNotIn(
            '[[ -z "$DEVELOPMENT_LANE_ROOT" ||', runner_text,
        )
        self.assertIn('CLI_PRODUCT_ID="$PROVIDER_PRODUCT_ID"', runner_text)
        self.assertIn('ISOLATED_PRODUCT_ID="$PROVIDER_PRODUCT_ID"', runner_text)
        self.assertIn(
            '"FACTORY_QUALIFICATION_PRODUCT_TREE=$ACTIVE_PRODUCT_TREE"',
            launcher_text,
        )
        self.assertIn(
            '"FACTORY_QUALIFICATION_MANIFEST=$PRODUCT_ROOT/factory/QUALIFICATION.json"',
            launcher_text,
        )
        self.assertIn(
            '"FACTORY_CLI_RUNTIME_ROOT=$PROVIDER_STATE_ROOT/cli-runtimes"',
            launcher_text,
        )
        for relative in (
            "provider/accounting",
            "provider/cli-runtimes",
            "provider/provider-apply-locks",
            "provider/provider-attempts",
        ):
            path = self.root / relative
            self.assertTrue(path.is_dir())
            self.assertEqual(path.stat().st_mode & 0o777, 0o700)
        configuration_lock = self.root / "provider/provider-configuration.lock"
        self.assertTrue(configuration_lock.is_file())
        self.assertEqual(configuration_lock.stat().st_mode & 0o777, 0o600)
        with self.assertRaisesRegex(
            ENVIRONMENT.EnvironmentError, "already exists",
        ):
            ENVIRONMENT.prepare(args)

    def test_rejects_root_too_long_for_cursor_scratch(self) -> None:
        root = Path(tempfile.mkdtemp(
            prefix="nysa-sf-qualification.too-long-", dir="/private/tmp",
        )).resolve()
        os.chmod(root, 0o700)
        try:
            with self.assertRaisesRegex(
                ENVIRONMENT.EnvironmentError, "too long for isolated Cursor",
            ):
                ENVIRONMENT.prepare(argparse.Namespace(
                    factory_root=self.factory,
                    product_root=self.product,
                    project="relay",
                    root=root,
                ))
        finally:
            shutil.rmtree(root)

    def test_runtime_mismatch_fails_before_qualification_materialization(self) -> None:
        plan = self.product / "factory/certification-plan.json"
        value = json.loads(plan.read_text(encoding="utf-8"))
        value["runtime"]["node"] = "v99.0.0"
        plan.write_text(json.dumps(value) + "\n", encoding="utf-8")
        run(self.product, "git", "add", "factory/certification-plan.json")
        run(self.product, "git", "commit", "-qm", "mismatched runtime")
        with self.assertRaisesRegex(
            ENVIRONMENT.EnvironmentError, "runtime_tuple_mismatch",
        ):
            ENVIRONMENT.prepare(argparse.Namespace(
                factory_root=self.factory,
                product_root=self.product,
                project="relay",
                root=self.root,
            ))
        self.assertFalse((self.root / "marker.json").exists())

    def test_takeover_reuses_authenticated_live_state_without_copying_it(self) -> None:
        source_sha = "b" * 40
        intermediate_sha = "d" * 40
        tickets = ["T-094", "T-100", "T-093"]
        (self.product / "factory/KIT_PIN").write_text(
            source_sha + "\n", encoding="utf-8",
        )
        (self.product / ".gitignore").write_text(
            "factory/linear-map.json\n", encoding="utf-8",
        )
        run(self.product, "git", "add", "factory/KIT_PIN", ".gitignore")
        run(self.product, "git", "commit", "-qm", "protected source")
        protected_sha = run(self.product, "git", "rev-parse", "HEAD")
        protected_tree = run(self.product, "git", "rev-parse", "HEAD^{tree}")
        run(
            self.product, "git", "update-ref", "refs/remotes/origin/main",
            protected_sha,
        )
        source_product = self.workspace / "source-product"
        run(
            self.product, "git", "worktree", "add", "-q", "--detach",
            str(source_product), protected_sha,
        )
        operator_map = source_product / "factory/linear-map.json"
        ENVIRONMENT.write(operator_map, {"last_success_at": "2026-07-31T12:00:00Z"})
        (self.product / "shared-policy.txt").write_text(
            "protected control change\n", encoding="utf-8",
        )
        run(self.product, "git", "add", "shared-policy.txt")
        run(self.product, "git", "commit", "-qm", "advance protected policy")
        current_protected_sha = run(self.product, "git", "rev-parse", "HEAD")
        run(
            self.product, "git", "update-ref", "refs/remotes/origin/main",
            current_protected_sha,
        )
        (self.product / "factory/KIT_PIN").write_text(
            self.sha + "\n", encoding="utf-8",
        )
        (self.product / "factory/QUALIFICATION.json").write_text(json.dumps({
            "budget_usd": "300.000000",
            "capacity": 3,
            "contract_version": "1.8.0",
            "factory_sha": self.sha,
            "generation": 1,
            "mode": "successor",
            "per_run_budget_usd": "10.000000",
            "per_ticket_budget_usd": "100.000000",
            "schema": "nysa.software-factory.qualification/v2",
            "source_factory_sha": source_sha,
            "target_done": 3,
            "tickets": tickets,
        }, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
        run(
            self.product, "git", "add", "factory/KIT_PIN",
            "factory/QUALIFICATION.json",
        )
        run(self.product, "git", "commit", "-qm", "authorize qualification")

        account = (self.workspace / "account").resolve()
        provider = account / ".factory"
        kits = provider / "kits"
        source = kits / "projects/relay"
        state = source / "controller"
        passports = state / "passports"
        for path in (
            provider, kits, kits / "projects", source, state, passports,
            provider / "accounting", provider / "cli-runtimes",
            provider / "provider-attempts", provider / "provider-apply-locks",
        ):
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
            path.chmod(0o700)
        ENVIRONMENT.write(source / "active.json", {
            "contract_version": "1.8.0",
            "kit_sha": source_sha,
            "kit_tree": "c" * 40,
            "product_path": str(source_product.resolve()),
            "product_tree": protected_tree,
            "project": "relay",
        })
        secret = b"p" * 32
        key = state / "passport.key"
        key.write_bytes(secret)
        key.chmod(0o600)
        for ticket in tickets:
            body = {
                "factory_release_history": [{
                    "contract_version": "1.8.0",
                    "factory_sha": source_sha,
                }, {
                    "contract_version": "1.8.0",
                    "factory_sha": intermediate_sha,
                }],
                "factory_sha": intermediate_sha,
                "migration_history": [{
                    "from_factory_sha": source_sha,
                    "from_head_sha": "1" * 40,
                    "from_passport_file_sha256": "2" * 64,
                    "from_passport_sha256": "3" * 64,
                    "from_protected_base_sha": "4" * 40,
                    "from_route_plan_sha256": "5" * 64,
                    "schema": "nysa.software-factory.ticket-passport-migration/v2",
                    "to_factory_sha": intermediate_sha,
                    "to_head_sha": "6" * 40,
                    "to_protected_base_sha": "7" * 40,
                    "to_route_plan_sha256": "8" * 64,
                }],
                "project": "relay",
                "schema": "nysa.software-factory.ticket-passport/v1",
                "ticket": ticket,
            }
            authenticated = dict(body)
            authenticated["authentication_sha256"] = hmac.new(
                secret, ENVIRONMENT.canonical(body), hashlib.sha256
            ).hexdigest()
            authenticated["passport_sha256"] = hashlib.sha256(
                ENVIRONMENT.canonical(authenticated)
            ).hexdigest()
            path = passports / f"{ticket}.json"
            path.write_bytes(ENVIRONMENT.canonical(authenticated))
            path.chmod(0o600)
        policy, activation, _ = ENVIRONMENT.provider_configuration(self.factory)
        ENVIRONMENT.write(provider / "provider-policy.json", policy)
        ENVIRONMENT.write(provider / "isolated-v1.enabled", activation)
        configuration_lock = provider / "provider-configuration.lock"
        configuration_lock.touch(mode=0o600)
        configuration_lock.chmod(0o600)
        run(
            provider,
            "/usr/bin/python3", str(self.factory / "scripts/provider-coordinator.py"),
            "--db", str(provider / "accounting/state-v2.sqlite3"), "status",
        )

        args = argparse.Namespace(
            factory_root=self.factory,
            product_root=self.product,
            project="relay",
            root=self.root,
            takeover_project="relay",
        )
        with mock.patch.object(Path, "home", return_value=account):
            value = ENVIRONMENT.prepare(args)

        active = json.loads((self.root / "projects/relay/active.json").read_text())
        self.assertEqual(value["qualification_mode"], "takeover")
        self.assertEqual(active["qualification_mode"], "takeover")
        self.assertEqual(active["takeover_kits_root"], str(kits))
        self.assertEqual(active["operator_map_path"], str(operator_map.resolve()))
        self.assertFalse((self.product / "factory/linear-map.json").exists())
        self.assertFalse((self.root / "provider").exists())
        self.assertFalse((self.root / "projects/relay/controller").exists())

        with self.assertRaisesRegex(
            ENVIRONMENT.EnvironmentError, "does not match active product",
        ):
            ENVIRONMENT.validate_takeover_product(
                source_product,
                self.product,
                {"product_tree": "0" * 40},
                {"tickets": tickets},
            )
        run(
            self.product, "git", "update-ref", "refs/remotes/origin/main",
            f"{protected_sha}^",
        )
        with self.assertRaisesRegex(
            ENVIRONMENT.EnvironmentError, "does not contain the active product",
        ):
            ENVIRONMENT.validate_takeover_product(
                source_product,
                self.product,
                {"product_tree": protected_tree},
                {"tickets": tickets},
            )
        run(
            self.product, "git", "update-ref", "refs/remotes/origin/main",
            current_protected_sha,
        )

        unrelated = self.workspace / "unrelated-product"
        shutil.copytree(self.product, unrelated, ignore=shutil.ignore_patterns(".git"))
        run(unrelated, "git", "init", "-q", "-b", "main")
        run(unrelated, "git", "config", "user.name", "Test")
        run(unrelated, "git", "config", "user.email", "test@example.invalid")
        run(unrelated, "git", "remote", "add", "origin", "git@example.invalid")
        run(unrelated, "git", "add", ".")
        run(unrelated, "git", "commit", "-qm", "unrelated")
        with self.assertRaisesRegex(
            ENVIRONMENT.EnvironmentError, "not a linked canonical worktree",
        ):
            ENVIRONMENT.validate_takeover_product(
                source_product,
                unrelated,
                {"product_tree": protected_tree},
                {"tickets": tickets},
            )
        (self.product / "application.txt").write_text("not control data\n")
        run(self.product, "git", "add", "application.txt")
        run(self.product, "git", "commit", "-qm", "change product code")
        with self.assertRaisesRegex(
            ENVIRONMENT.EnvironmentError, "changes non-control product files",
        ):
            ENVIRONMENT.validate_takeover_product(
                source_product,
                self.product,
                {"product_tree": protected_tree},
                {"tickets": tickets},
            )

    def test_upgrades_release_without_replacing_controller_state(self) -> None:
        args = argparse.Namespace(
            factory_root=self.factory,
            product_root=self.product,
            project="relay",
            root=self.root,
        )
        first = ENVIRONMENT.prepare(args)
        active_path = self.root / "projects/relay/active.json"
        legacy_active = ENVIRONMENT.read(active_path)
        legacy_active.pop("product_sha")
        legacy_active.pop("runtime_tuple")
        ENVIRONMENT.replace(active_path, legacy_active)
        controller = self.root / "projects/relay/controller"
        claims = controller / "claims"
        controller.mkdir(mode=0o700)
        claims.mkdir(mode=0o700)
        key = controller / "passport.key"
        key.write_bytes(b"p" * 32)
        key.chmod(0o600)
        ENVIRONMENT.write(claims / "T-110.json", {"status": "running"})

        (self.factory / "successor.txt").write_text("successor\n", encoding="utf-8")
        run(self.factory, "git", "add", "successor.txt")
        run(self.factory, "git", "commit", "-qm", "successor")
        successor = run(self.factory, "git", "rev-parse", "HEAD")
        (self.product / "factory/KIT_PIN").write_text(
            successor + "\n", encoding="utf-8",
        )
        run(self.product, "git", "add", "factory/KIT_PIN")
        run(self.product, "git", "commit", "-qm", "pin successor")

        second = ENVIRONMENT.upgrade(args)
        active = json.loads(active_path.read_text())
        self.assertEqual(first["status"], "prepared")
        self.assertEqual(second["status"], "upgraded")
        self.assertEqual(active["kit_sha"], successor)
        self.assertEqual(active["generation"], 2)
        self.assertEqual(active["product_sha"], second["product_sha"])
        self.assertEqual(active["runtime_tuple"], second["runtime_tuple"])
        self.assertEqual(key.read_bytes(), b"p" * 32)
        self.assertTrue((self.root / f"releases/{self.sha}").is_dir())
        self.assertTrue((self.root / f"releases/{successor}").is_dir())


if __name__ == "__main__":
    unittest.main()
