#!/usr/bin/env python3
"""Focused Contract 1.8 transition receipt tests."""

from __future__ import annotations

import argparse
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts" / "state-machine.py"
SPEC = importlib.util.spec_from_file_location("state_machine", HELPER)
assert SPEC and SPEC.loader
STATE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(STATE)


def run(*command: str, cwd: Path) -> str:
    return subprocess.run(
        command, cwd=cwd, text=True, capture_output=True, check=True
    ).stdout.strip()


class StateMachineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        os.chmod(self.root, 0o700)
        self.product = self.root / "product"
        (self.product / "factory/tickets").mkdir(parents=True)
        (self.product / "factory/route-plans").mkdir()
        (self.product / "factory/runs").mkdir()
        (self.product / "factory/tickets/T-110.md").write_text(
            "# T-110\n\nState: Planning\n", encoding="utf-8"
        )
        (self.product / "factory/route-plans/T-110.json").write_text(
            '{"ticket":"T-110"}\n', encoding="utf-8"
        )
        run("git", "init", "-q", "-b", "ticket/T-110", cwd=self.product)
        run("git", "config", "user.name", "Test", cwd=self.product)
        run("git", "config", "user.email", "test@example.invalid", cwd=self.product)
        run("git", "add", ".", cwd=self.product)
        run("git", "commit", "-qm", "seed", cwd=self.product)
        self.state_dir = STATE.safe_state_dir(self.root / "controller")
        self.args = argparse.Namespace(
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
        self.origin = mock.patch.dict(
            os.environ, {"FACTORY_CERTIFIED_PRODUCT_ORIGIN": "test-origin"}
        )
        self.origin.start()

    def tearDown(self) -> None:
        self.origin.stop()
        self.temporary.cleanup()

    def test_receipt_is_one_use_and_chains_after_terminal_evidence(self) -> None:
        first = STATE.issue(self.args, "RUN planner")
        self.args.receipt = first["receipt_sha256"]
        self.assertFalse(STATE.verify(self.args, consume=False)["consumed"])
        self.assertTrue(STATE.verify(self.args, consume=True)["consumed"])
        self.args.require_used = True
        self.assertTrue(STATE.verify(self.args, consume=False)["consumed"])
        with self.assertRaisesRegex(STATE.StateError, "already consumed"):
            STATE.verify(self.args, consume=True)

        self.args.require_used = False
        second = STATE.issue(self.args, "RUN planner")
        self.assertEqual(second["parent_digest"], first["receipt_sha256"])
        self.assertNotEqual(second["receipt_sha256"], first["receipt_sha256"])
        self.args.receipt = second["receipt_sha256"]

        (self.product / "factory/runs/run-1.meta").write_text(
            "run_id=run-1\n"
            "ticket=T-110\n"
            "role=planner\n"
            "accounting_state=completed\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(STATE.StateError, "inputs drifted"):
            STATE.verify(self.args, consume=False)
        third = STATE.issue(self.args, "RUN spec-linter")
        self.assertEqual(third["parent_digest"], second["receipt_sha256"])
        self.assertEqual(third["role"], "spec-linter")

    def test_ambiguous_repair_has_no_runnable_transition(self) -> None:
        self.assertIsNone(
            STATE.stage_role("AWAIT_BUDGET ticket budget exhausted")
        )
        with self.assertRaisesRegex(STATE.StateError, "unsupported transition"):
            STATE.stage_role("FIX builder-or-test-author")


if __name__ == "__main__":
    unittest.main()
