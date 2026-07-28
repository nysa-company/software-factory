#!/usr/bin/env python3
"""Focused authenticated passport continuity tests."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


STATE = module("state_machine", ROOT / "scripts/state-machine.py")
PASSPORT = module("ticket_passport", ROOT / "scripts/ticket-passport.py")


def run(*command: str, cwd: Path) -> str:
    return subprocess.run(
        command, cwd=cwd, text=True, capture_output=True, check=True
    ).stdout.strip()


class TicketPassportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        os.chmod(self.root, 0o700)
        self.remote = self.root / "remote.git"
        run("git", "init", "--bare", "-q", str(self.remote), cwd=self.root)
        self.product = self.root / "product"
        (self.product / "factory/tickets").mkdir(parents=True)
        (self.product / "factory/route-plans").mkdir()
        (self.product / "factory/runs").mkdir()
        (self.product / "factory/PROJECT.env").write_text(
            "GH_REPO=nysa-company/relay-factory\n", encoding="utf-8"
        )
        (self.product / "factory/tickets/T-110.md").write_text(
            "# T-110\n\nState: Planning\n", encoding="utf-8"
        )
        (self.product / "factory/route-plans/T-110.json").write_text(
            f'{{"kit_sha":"{"a" * 40}","ticket":"T-110"}}\n',
            encoding="utf-8",
        )
        (self.product / ".gitignore").write_text(
            "factory/runs/\n", encoding="utf-8"
        )
        run("git", "init", "-q", "-b", "ticket/T-110", cwd=self.product)
        run("git", "config", "user.name", "Test", cwd=self.product)
        run("git", "config", "user.email", "test@example.invalid", cwd=self.product)
        run("git", "add", ".", cwd=self.product)
        run("git", "commit", "-qm", "seed", cwd=self.product)
        run("git", "remote", "add", "origin", str(self.remote), cwd=self.product)
        run("git", "push", "-qu", "origin", "HEAD:main", cwd=self.product)
        self.state_dir = STATE.safe_state_dir(self.root / "controller")
        self.state_args = argparse.Namespace(
            contract_version="1.8.0",
            factory_root=self.product,
            factory_sha="a" * 40,
            kit_dir=ROOT,
            lease="",
            project="relay",
            receipt="",
            require_used=False,
            role="planner",
            state_dir=self.state_dir,
            ticket="T-110",
            workdir=self.product,
        )
        self.passport_args = argparse.Namespace(
            action="export",
            contract_version="1.8.0",
            factory_root=self.product,
            factory_sha="a" * 40,
            project="relay",
            publication_state="none",
            receipt="",
            state_dir=self.state_dir,
            ticket="T-110",
            workdir=self.product,
        )
        self.origin = mock.patch.dict(
            os.environ, {"FACTORY_CERTIFIED_PRODUCT_ORIGIN": str(self.remote)}
        )
        self.origin.start()

    def tearDown(self) -> None:
        self.origin.stop()
        self.temporary.cleanup()

    def terminal(self, run_id: str, role: str, receipt: str, factory_sha: str) -> None:
        output_path = self.product / f"factory/runs/{run_id}.out"
        output_path.write_text(f"{role} output\n", encoding="utf-8")
        output = hashlib.sha256(output_path.read_bytes()).hexdigest()
        (self.product / f"factory/runs/{run_id}.meta").write_text(
            f"run_id={run_id}\n"
            "phase=completed\n"
            "accounting_state=completed\n"
            "effective_cost=1.500000\n"
            "exit_status=0\n"
            "ticket=T-110\n"
            f"role={role}\n"
            "role_exit=ok\n"
            f"role_head_before={run('git', 'rev-parse', 'HEAD', cwd=self.product)}\n"
            f"kit_sha={factory_sha}\n"
            "contract_version=1.8.0\n"
            f"transition_receipt_sha256={receipt}\n"
            f"output_sha256={output}\n",
            encoding="utf-8",
        )

    def test_passport_chains_receipts_without_replay_or_double_charge(self) -> None:
        secret = PASSPORT.key(self.state_dir)
        first = STATE.issue(self.state_args, "RUN planner")
        self.state_args.receipt = first["receipt_sha256"]
        STATE.verify(self.state_args, consume=True)
        self.terminal("run-1", "planner", first["receipt_sha256"], "a" * 40)
        self.passport_args.receipt = first["receipt_sha256"]
        exported = PASSPORT.export(self.passport_args, secret)
        self.assertEqual(exported["cumulative_charges_micro_usd"], 1_500_000)
        self.assertEqual(len(exported["completed_role_evidence"]), 1)

        validated = PASSPORT.validate(self.passport_args, secret)
        self.assertEqual(validated["passport_sha256"], exported["passport_sha256"])
        with self.assertRaisesRegex(PASSPORT.PassportError, "lineage"):
            PASSPORT.export(self.passport_args, secret)

        self.passport_args.factory_sha = "b" * 40
        migrated = PASSPORT.migrate(self.passport_args, secret)
        self.assertEqual(migrated["factory_sha"], "b" * 40)
        self.assertEqual(len(migrated["migration_history"]), 1)

        self.state_args.factory_sha = "b" * 40
        self.state_args.role = "spec-linter"
        second = STATE.issue(self.state_args, "RUN spec-linter")
        self.assertRegex(second["passport_sha256"], r"^[0-9a-f]{64}$")
        self.state_args.receipt = second["receipt_sha256"]
        STATE.verify(self.state_args, consume=True)
        self.terminal(
            "run-2", "spec-linter", second["receipt_sha256"], "b" * 40
        )
        self.passport_args.receipt = second["receipt_sha256"]
        upgraded = PASSPORT.export(self.passport_args, secret)
        self.assertEqual(upgraded["cumulative_charges_micro_usd"], 3_000_000)
        self.assertEqual(len(upgraded["completed_role_evidence"]), 2)
        self.assertEqual(
            [item["factory_sha"] for item in upgraded["factory_release_history"]],
            ["a" * 40, "b" * 40],
        )

    def test_protected_inflight_authorization_allows_exact_rewrite(self) -> None:
        secret = PASSPORT.key(self.state_dir)
        receipt = STATE.issue(self.state_args, "RUN planner")
        self.state_args.receipt = receipt["receipt_sha256"]
        STATE.verify(self.state_args, consume=True)
        self.terminal("run-1", "planner", receipt["receipt_sha256"], "a" * 40)
        self.passport_args.receipt = receipt["receipt_sha256"]
        previous = PASSPORT.export(self.passport_args, secret)

        rewritten = run(
            "git", "commit-tree", "HEAD^{tree}", "-m", "authorized rewrite",
            cwd=self.product,
        )
        run("git", "reset", "--hard", rewritten, cwd=self.product)
        with self.assertRaisesRegex(PASSPORT.PassportError, "lineage"):
            PASSPORT.migrate(self.passport_args, secret)
        self.passport_args.factory_sha = "b" * 40
        with self.assertRaisesRegex(PASSPORT.PassportError, "lineage"):
            PASSPORT.migrate(self.passport_args, secret)

        protected = self.root / "protected"
        run("git", "clone", "-q", str(self.remote), str(protected), cwd=self.root)
        run("git", "config", "user.name", "Test", cwd=protected)
        run("git", "config", "user.email", "test@example.invalid", cwd=protected)
        authorization = protected / (
            "factory/migrations/inflight-release/" + "b" * 40 + ".json"
        )
        authorization.parent.mkdir(parents=True)
        authorization.write_text(
            json.dumps({
                "repository": "nysa-company/relay-factory",
                "schema": PASSPORT.INFLIGHT_SCHEMA,
                "source_kit_sha": "a" * 40,
                "target_kit_sha": "b" * 40,
                "tickets": [{
                    "branch": "ticket/T-110",
                    "head": rewritten,
                    "state": "Planning",
                    "ticket": "T-110",
                }],
            }, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        run("git", "add", ".", cwd=protected)
        run("git", "commit", "-qm", "authorize rewrite", cwd=protected)
        run("git", "push", "-q", "origin", "HEAD:main", cwd=protected)
        run("git", "fetch", "-q", "origin", "main", cwd=self.product)

        migrated = PASSPORT.migrate(self.passport_args, secret)
        self.assertEqual(migrated["head_sha"], rewritten)
        self.assertEqual(migrated["parent_digest"], previous["passport_sha256"])
        self.assertEqual(migrated["factory_sha"], "b" * 40)


if __name__ == "__main__":
    unittest.main()
