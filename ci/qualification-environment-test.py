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
        self.original_home = os.environ.get("HOME")
        self.home = self.workspace / "home"
        self.home.mkdir(mode=0o700)
        (self.home / ".factory").mkdir(mode=0o700)
        self.global_env = self.home / ".factory/global.env"
        self.global_env.write_text(
            "CLAUDE_CODE_PINNED=2.1.223\n", encoding="utf-8",
        )
        self.global_env.chmod(0o600)
        os.environ["HOME"] = str(self.home)
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
        (self.product / "factory/QUALIFICATION.json").write_text(
            json.dumps({
                "capacity": 3,
                "schema": "nysa.software-factory.qualification/v2",
                "target_done": 3,
                "tickets": ["T-101", "T-102", "T-103"],
            }) + "\n",
            encoding="utf-8",
        )
        (self.product / ".gitignore").write_text(
            "factory/runs/\n", encoding="utf-8",
        )
        (self.product / "factory/tickets").mkdir()
        for ticket in ("T-101", "T-102", "T-103"):
            (self.product / f"factory/tickets/{ticket}.md").write_text(
                f"# {ticket}\n\nState: Ready\nProduct-Decisions: frozen\n"
                "Depends-On: none\nFixture-Seams: none\n"
                "Authentication-Seams: none\nProtected-Test-Conflicts: none\n",
                encoding="utf-8",
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
        run(
            self.product, "git", "update-ref", "refs/remotes/origin/main",
            run(self.product, "git", "rev-parse", "HEAD"),
        )

    def tearDown(self) -> None:
        for base, directories, files in os.walk(self.root, topdown=False):
            for name in files:
                (Path(base) / name).chmod(0o600)
            for name in directories:
                (Path(base) / name).chmod(0o700)
        shutil.rmtree(self.root)
        if self.original_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self.original_home
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
        authority = Path(value["authority_root"])
        self.assertEqual(value["factory_sha"], self.sha)
        runs = self.product / "factory/runs"
        self.assertTrue(runs.is_dir())
        self.assertEqual(runs.stat().st_mode & 0o777, 0o700)
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
            (self.root / "global.env").read_bytes(),
            self.global_env.read_bytes(),
        )
        self.assertEqual((self.root / "global.env").stat().st_mode & 0o777, 0o600)
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
            "--config", str(authority / "provider/provider-activation.json"),
            "--policy", str(authority / "provider/provider-policy.json"),
            "--contract-version", "1.8.0",
            "--status",
        ))
        self.assertEqual(status["execution_mode"], "cli-concurrent-v1")
        policy = json.loads(
            (authority / "provider/provider-policy.json").read_text()
        )
        self.assertEqual(policy["coupled_max_concurrent"], 3)
        self.assertEqual(policy["global"]["max_concurrent"], 3)
        launcher_text = (
            ROOT / "integrations/hermes/bin/factory-launch"
        ).read_text(encoding="utf-8")
        self.assertIn(
            'PROVIDER_STATE_ROOT="$ACTIVE_PROVIDER_STATE"', launcher_text
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
            '"FACTORY_CLI_RUNTIME_ROOT=$CLI_RUNTIME_ROOT"',
            launcher_text,
        )
        self.assertIn('CLI_RUNTIME_ROOT="$QUALIFICATION_ROOT"', launcher_text)
        self.assertIn(
            'GLOBAL_ENV_PATH="$QUALIFICATION_ROOT/global.env"', launcher_text,
        )
        self.assertIn('"FACTORY_GLOBAL_ENV=$GLOBAL_ENV_PATH"', launcher_text)
        self.assertIn(
            '--cli-root "${FACTORY_CLI_RUNTIME_ROOT:-}"', runner_text,
        )
        for relative in (
            "provider/accounting",
            "provider/cli-runtimes",
            "provider/provider-apply-locks",
            "provider/provider-attempts",
        ):
            path = authority / relative
            self.assertTrue(path.is_dir())
            self.assertEqual(path.stat().st_mode & 0o777, 0o700)
        configuration_lock = authority / "provider/provider-configuration.lock"
        self.assertTrue(configuration_lock.is_file())
        self.assertEqual(configuration_lock.stat().st_mode & 0o777, 0o600)
        with self.assertRaisesRegex(
            ENVIRONMENT.EnvironmentError, "already exists",
        ):
            ENVIRONMENT.prepare(args)

    def test_rejects_unsafe_runtime_root_and_noncanonical_contracts(self) -> None:
        runs = self.product / "factory/runs"
        runs.symlink_to(self.workspace)
        with self.assertRaisesRegex(ENVIRONMENT.EnvironmentError, "factory/runs is unsafe"):
            ENVIRONMENT.prepare(argparse.Namespace(
                factory_root=self.factory, product_root=self.product,
                project="relay", root=self.root,
            ))
        runs.unlink()
        ticket = self.product / "factory/tickets/T-101.md"
        ticket.write_text(ticket.read_text().replace(
            "Product-Decisions: frozen",
            "Product-Decisions: frozen - inherited",
        ))
        run(self.product, "git", "add", "factory/tickets/T-101.md")
        run(self.product, "git", "commit", "-qm", "decorate control field")
        with self.assertRaisesRegex(
            ENVIRONMENT.EnvironmentError,
            "Product-Decisions must be exactly frozen",
        ):
            ENVIRONMENT.prepare(argparse.Namespace(
                factory_root=self.factory, product_root=self.product,
                project="relay", root=self.root,
            ))

    def test_rejects_unsafe_global_config_sources(self) -> None:
        unsafe = self.workspace / "unsafe-global.env"
        unsafe.write_text("CLAUDE_CODE_PINNED=2.1.223\n", encoding="utf-8")
        unsafe.chmod(0o644)
        with self.assertRaisesRegex(
            ENVIRONMENT.EnvironmentError, "global config is unsafe",
        ):
            ENVIRONMENT.snapshot_global_config(
                argparse.Namespace(global_env=unsafe), self.root,
            )
        unsafe.chmod(0o600)
        link = self.workspace / "linked-global.env"
        link.symlink_to(unsafe)
        with self.assertRaisesRegex(
            ENVIRONMENT.EnvironmentError, "global config is unsafe",
        ):
            ENVIRONMENT.snapshot_global_config(
                argparse.Namespace(global_env=link), self.root,
            )

    def test_rejects_internal_qualification_dependency(self) -> None:
        ticket = self.product / "factory/tickets/T-103.md"
        ticket.write_text(ticket.read_text().replace("Depends-On: none", "Depends-On: T-101"))
        run(self.product, "git", "add", "factory/tickets/T-103.md")
        run(self.product, "git", "commit", "-qm", "dependent cohort")
        with self.assertRaisesRegex(
            ENVIRONMENT.EnvironmentError, "qualification cohort dependency T-103 -> T-101",
        ):
            ENVIRONMENT.prepare(argparse.Namespace(
                factory_root=self.factory, product_root=self.product,
                project="relay", root=self.root,
            ))

    def test_selected_ticket_authoring_fields_fail_before_lane_creation(self) -> None:
        ticket = self.product / "factory/tickets/T-101.md"
        original = ticket.read_text()
        ticket.write_text(original.replace(
            "Depends-On: none", "Depends-On: none — rationale",
        ))
        with self.assertRaisesRegex(
            ENVIRONMENT.EnvironmentError, "Depends-On is invalid",
        ):
            ENVIRONMENT.validate_selected_contracts(self.product)

        ticket.write_text(original.replace(
            "Fixture-Seams: none", "Fixture-Seams: missing.test.ts",
        ))
        with self.assertRaisesRegex(
            ENVIRONMENT.EnvironmentError, "READINESS BLOCKED.*missing.test.ts",
        ):
            ENVIRONMENT.validate_selected_contracts(self.product)

    def test_rejects_ticket_blob_that_dispatch_would_not_use(self) -> None:
        ticket = self.product / "factory/tickets/T-101.md"
        ticket.write_text(ticket.read_text() + "\n## Log\n\nControl-only edit.\n")
        run(self.product, "git", "add", str(ticket))
        run(self.product, "git", "commit", "-qm", "diverge qualification ticket")

        with self.assertRaisesRegex(
            ENVIRONMENT.EnvironmentError,
            "T-101: qualification ticket source differs from protected dispatch",
        ):
            ENVIRONMENT.prepare(argparse.Namespace(
                factory_root=self.factory, product_root=self.product,
                project="relay", root=self.root,
            ))
        self.assertFalse((self.root / "marker.json").exists())

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

    def test_hydrates_historical_pr_objects_once_without_moving_refs(self) -> None:
        publisher = self.workspace / "publisher"
        remote = self.workspace / "history.git"
        run(self.workspace, "git", "init", "--bare", "-q", str(remote))
        run(self.workspace, "git", "init", "-q", "-b", "main", str(publisher))
        run(publisher, "git", "config", "user.name", "Test")
        run(publisher, "git", "config", "user.email", "test@example.invalid")
        (publisher / "factory").mkdir()
        (publisher / "factory/PROJECT.env").write_text(
            "GH_REPO=example/product\n", encoding="utf-8",
        )
        run(publisher, "git", "add", ".")
        run(publisher, "git", "commit", "-qm", "base")
        run(publisher, "git", "remote", "add", "origin", str(remote))
        run(publisher, "git", "push", "-q", "origin", "main")
        run(publisher, "git", "switch", "-qc", "ticket/T-030")
        (publisher / "evidence.txt").write_text("evidence\n", encoding="utf-8")
        run(publisher, "git", "add", ".")
        run(publisher, "git", "commit", "-qm", "evidence")
        evidence = run(publisher, "git", "rev-parse", "HEAD")
        (publisher / "tip.txt").write_text("tip\n", encoding="utf-8")
        run(publisher, "git", "add", ".")
        run(publisher, "git", "commit", "-qm", "tip")
        head = run(publisher, "git", "rev-parse", "HEAD")
        run(
            publisher, "git", "push", "-q", "origin",
            f"HEAD:refs/pull/30/head",
        )
        run(publisher, "git", "switch", "-q", "main")
        migration = publisher / "factory/migrations/protected-merge-reconciliation/T-030.json"
        migration.parent.mkdir(parents=True)
        migration.write_text(json.dumps({
            "adoption_pr": {"head": head, "number": 30},
            "evidence_head": evidence,
            "original_pr": {"head": head, "number": 30},
            "repository": "example/product",
            "schema": "nysa.software-factory.protected-merge-reconciliation/v1",
        }) + "\n", encoding="utf-8")
        run(publisher, "git", "add", ".")
        run(publisher, "git", "commit", "-qm", "record migration")
        run(publisher, "git", "push", "-q", "origin", "main")

        consumer = self.workspace / "consumer"
        run(
            self.workspace, "git", "clone", "-q", "--no-local",
            "--single-branch", "--branch", "main", str(remote), str(consumer),
        )
        self.assertFalse(ENVIRONMENT.commit_present(consumer, head))
        refs = run(consumer, "git", "show-ref")
        self.assertEqual(ENVIRONMENT.historical_pr_objects(consumer), 1)
        self.assertTrue(ENVIRONMENT.commit_present(consumer, head))
        self.assertTrue(ENVIRONMENT.commit_present(consumer, evidence))
        self.assertEqual(run(consumer, "git", "show-ref"), refs)
        run(consumer, "git", "remote", "set-url", "origin", "invalid://offline")
        self.assertEqual(ENVIRONMENT.historical_pr_objects(consumer), 1)

    def test_historical_pr_ref_mismatch_fails_closed(self) -> None:
        migrations = self.product / "factory/migrations/contract-1.3"
        migrations.mkdir(parents=True)
        (self.product / "factory/PROJECT.env").write_text(
            "GH_REPO=example/product\n", encoding="utf-8",
        )
        (migrations / "T-013.json").write_text(json.dumps({
            "pr": {"head": "f" * 40, "number": 13},
            "repository": "example/product",
            "schema": "nysa.software-factory.legacy-closeout/v1",
        }) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(
            ENVIRONMENT.EnvironmentError,
            r"historical PR head unavailable: .*T-013.json PR #13",
        ):
            ENVIRONMENT.historical_pr_objects(self.product)
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
        for ticket in tickets:
            (self.product / f"factory/tickets/{ticket}.md").write_text(
                f"# {ticket}\n\nState: Ready\nProduct-Decisions: frozen\n"
                "Depends-On: none\nFixture-Seams: none\n"
                "Authentication-Seams: none\nProtected-Test-Conflicts: none\n",
                encoding="utf-8",
            )
        run(
            self.product, "git", "add", "factory/KIT_PIN", ".gitignore",
            "factory/tickets",
        )
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
            "factory/QUALIFICATION.json", "factory/tickets",
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
                "completed_role_evidence": ([{
                    "contract_version": "1.8.0",
                    "factory_sha": source_sha,
                    "head_before": "1" * 40,
                    "manifest_sha256": "2" * 64,
                    "output_sha256": "3" * 64,
                    "role": "test-author",
                    "run_id": "missing-terminal-run",
                    "transition_receipt_sha256": "4" * 64,
                }] if ticket == "T-094" else []),
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
        (self.product / "factory/runs").mkdir(mode=0o700)
        with (
            mock.patch.object(Path, "home", return_value=account),
            mock.patch.object(
                ENVIRONMENT, "protected_terminal",
                side_effect=ENVIRONMENT.TerminalError("not terminal"),
            ),
            self.assertRaisesRegex(
                ENVIRONMENT.EnvironmentError, "missing-terminal-run test-author missing meta",
            ),
        ):
            ENVIRONMENT.takeover_source(
                self.factory, self.product.resolve(), "relay", "relay",
            )
        with (
            mock.patch.object(Path, "home", return_value=account),
            mock.patch.object(
                ENVIRONMENT, "protected_terminal",
                side_effect=(
                    {},
                    ENVIRONMENT.TerminalError("not terminal"),
                    ENVIRONMENT.TerminalError("not terminal"),
                ),
            ),
        ):
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
        controller = Path(first["authority_root"]) / "controller"
        claims = controller / "claims"
        self.assertTrue(controller.is_dir())
        self.assertEqual(controller.stat().st_mode & 0o777, 0o700)
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

        original_global = (self.root / "global.env").read_bytes()
        self.global_env.write_text(
            "CLAUDE_CODE_PINNED=9.9.9\n", encoding="utf-8",
        )
        ENVIRONMENT.snapshot_global_config(args, self.root)
        self.assertEqual((self.root / "global.env").read_bytes(), original_global)
        replacement = self.workspace / "qualification-global.env"
        replacement.write_text(
            "CLAUDE_CODE_PINNED=2.1.224\n", encoding="utf-8",
        )
        replacement = replacement.resolve(strict=True)
        replacement.chmod(0o600)

        second = ENVIRONMENT.upgrade(argparse.Namespace(
            **vars(args), global_env=replacement,
        ))
        active = json.loads(active_path.read_text())
        self.assertEqual(first["status"], "prepared")
        self.assertEqual(second["status"], "upgraded")
        self.assertEqual(active["kit_sha"], successor)
        self.assertEqual(active["generation"], 2)
        self.assertEqual(active["product_sha"], second["product_sha"])
        self.assertEqual(active["runtime_tuple"], second["runtime_tuple"])
        self.assertEqual(
            (self.root / "global.env").read_bytes(), replacement.read_bytes(),
        )
        self.assertEqual(key.read_bytes(), b"p" * 32)
        self.assertTrue((self.root / f"releases/{self.sha}").is_dir())
        self.assertTrue((self.root / f"releases/{successor}").is_dir())

    def test_restores_signed_safe_pause_after_disposable_root_is_removed(self) -> None:
        args = argparse.Namespace(
            factory_root=self.factory,
            product_root=self.product,
            project="relay",
            root=self.root,
        )
        first = ENVIRONMENT.prepare(args)
        authority = Path(first["authority_root"])
        controller = authority / "controller"
        parked = controller / "parked/T-101"
        parked.parent.mkdir(mode=0o700)
        run(self.product, "git", "branch", "ticket/T-101")
        run(
            self.product, "git", "worktree", "add", "-q", str(parked),
            "ticket/T-101",
        )
        head = run(parked, "git", "rev-parse", "HEAD")
        secret = b"p" * 32
        key = controller / "passport.key"
        key.write_bytes(secret)
        key.chmod(0o600)
        body = {
            "branch": "ticket/T-101",
            "current_stage": "RUN builder",
            "current_state": "Building",
            "factory_sha": self.sha,
            "head_sha": head,
            "project": "relay",
            "publication_state": "none",
            "schema": "nysa.software-factory.ticket-passport/v1",
            "ticket": "T-101",
        }
        passport = dict(body)
        passport["authentication_sha256"] = hmac.new(
            secret, ENVIRONMENT.canonical(body), hashlib.sha256,
        ).hexdigest()
        passport["passport_sha256"] = hashlib.sha256(
            ENVIRONMENT.canonical(passport)
        ).hexdigest()
        passports = controller / "passports"
        passports.mkdir(mode=0o700)
        ENVIRONMENT.write(passports / "T-101.json", passport)
        run_snapshot = hashlib.sha256(b"[]").hexdigest()
        pause = {
            "blocking_issue": "https://github.com/example/software-factory/issues/1",
            "branch": "ticket/T-101",
            "budget_sha256": None,
            "created_at_epoch": 1,
            "current_stage": "RUN builder",
            "current_state": "Building",
            "factory_sha": self.sha,
            "head_sha": head,
            "passport_sha256": passport["passport_sha256"],
            "passport_factory_sha": self.sha,
            "resume_state": None,
            "run_snapshot_sha256": run_snapshot,
            "schema": "nysa.software-factory.ticket-pause/v2",
            "status": "claimed",
            "ticket": "T-101",
            "worktree": str(parked),
        }
        pause["pause_sha256"] = hashlib.sha256(json.dumps(
            pause, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
        ).encode()).hexdigest()
        ENVIRONMENT.write(controller / "pause-T-101.json", pause)

        for base, directories, files in os.walk(self.root, topdown=False):
            for name in files:
                (Path(base) / name).chmod(0o600)
            for name in directories:
                (Path(base) / name).chmod(0o700)
        shutil.rmtree(self.root)
        restored = ENVIRONMENT.prepare(argparse.Namespace(
            **vars(args), restore=True,
        ))
        active = ENVIRONMENT.read(self.root / "projects/relay/active.json")
        self.assertEqual(restored["status"], "restored")
        self.assertEqual(active["controller_state_path"], str(controller))
        self.assertEqual(active["provider_state_path"], str(authority / "provider"))
        self.assertEqual(key.read_bytes(), secret)
        self.assertEqual(run(parked, "git", "rev-parse", "HEAD"), head)


if __name__ == "__main__":
    unittest.main()
