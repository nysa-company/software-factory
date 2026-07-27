#!/usr/bin/env python3
"""Focused non-agent controller persistence tests."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "factory_controller", ROOT / "scripts/factory-controller.py"
)
assert SPEC and SPEC.loader
CONTROL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CONTROL)


class FactoryControllerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        os.chmod(self.root, 0o700)
        self.product = self.root / "product"
        (self.product / "factory/runs").mkdir(parents=True)
        (self.product / "factory/PROJECT.env").write_text(
            "MAX_CONCURRENT_TICKETS=4\n", encoding="utf-8"
        )
        (self.product / "factory/ENVELOPE.env").write_text(
            "PER_TICKET_BUDGET_USD=25.000000\n", encoding="utf-8"
        )
        self.state = self.root / "controller"
        self.state.mkdir(mode=0o700)
        self.launcher = self.root / "factory-launch"
        self.launcher.write_text("#!/bin/sh\n", encoding="utf-8")
        self.launcher.chmod(0o700)
        self.args = argparse.Namespace(
            launcher=self.launcher,
            product_root=self.product,
            project="relay",
            release_path=ROOT,
            state_dir=self.state,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_claims_four_cells_and_recovers_terminal_receipt(self) -> None:
        controller = CONTROL.Controller(self.args)
        values = [
            {
                "action": "START",
                "branch": f"ticket/T-{110 + number}",
                "lease_id": f"{number + 1:064x}",
                "priority": "normal",
                "ticket": f"T-{110 + number}",
                "worktree": str(self.root / f"cell-{number + 1}"),
            }
            for number in range(4)
        ]
        for value in values:
            Path(value["worktree"]).mkdir()
        values.append({"action": "WAIT"})
        controller.json_call = lambda *_args, **_kwargs: values.pop(0)
        claims = controller.claim_new([])
        self.assertEqual(len(claims), 4)
        self.assertEqual(
            {item["ticket"] for item in controller.load_claims()},
            {"T-110", "T-111", "T-112", "T-113"},
        )

        claim = claims[0]
        claim.update(
            receipt="a" * 64,
            role="planner",
            status="running",
        )
        controller.save_claim(claim)
        (self.product / "factory/runs/run-1.meta").write_text(
            "run_id=run-1\n"
            "ticket=T-110\n"
            "role=planner\n"
            "accounting_state=completed\n"
            "exit_status=0\n"
            "role_exit=ok\n"
            f"transition_receipt_sha256={'a' * 64}\n",
            encoding="utf-8",
        )
        exports = []
        controller.passport = lambda item, state: exports.append((item["ticket"], state))
        self.assertTrue(controller.finish_pending_run(claim))
        self.assertEqual(exports, [("T-110", "none")])
        self.assertEqual(claim["receipt"], "")
        self.assertEqual(claim["status"], "claimed")

    def test_qualification_forces_real_restart_before_four_ticket_run(self) -> None:
        tickets = [f"T-{number}" for number in range(110, 114)]
        (self.product / "factory/QUALIFICATION.json").write_text(
            json.dumps({
                "capacity": 4,
                "contract_version": "1.8.0",
                "factory_sha": "a" * 40,
                "schema": CONTROL.QUALIFICATION_SCHEMA,
                "target_done": 4,
                "tickets": tickets,
            }),
            encoding="utf-8",
        )
        first = CONTROL.Controller(self.args)
        values = []
        for number, ticket in enumerate(tickets, 1):
            cell = self.root / f"cell-{number}"
            cell.mkdir()
            values.append({
                "action": "START",
                "branch": f"ticket/{ticket}",
                "lease_id": f"{number:064x}",
                "priority": "normal",
                "ticket": ticket,
                "worktree": str(cell),
            })
        first.json_call = lambda *_args, **_kwargs: values.pop(0)
        self.assertEqual(first.reconcile()["status"], "restart_required")

        second = CONTROL.Controller(self.args)
        second.reconcile_ticket = lambda claim: {
            "status": "active", "ticket": claim["ticket"],
        }
        result = second.reconcile()
        self.assertEqual(result["active"], 4)
        events = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in (self.state / "events").glob("*.json")
        ]
        self.assertIn("restart_boundary", {item["event"] for item in events})
        self.assertIn("controller_recovered", {item["event"] for item in events})

    def test_blocked_ticket_is_excluded_without_holding_capacity(self) -> None:
        controller = CONTROL.Controller(self.args)
        blocked = {
            "branch": "ticket/T-110",
            "lease": "a" * 64,
            "priority": "normal",
            "publication_lease": "",
            "receipt": "",
            "role": "",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "blocked",
            "ticket": "T-110",
            "worktree": str(self.root / "cell-1"),
        }
        calls = []

        def dispatch(*arguments, **_kwargs):
            calls.append(arguments)
            return {"action": "WAIT"}

        controller.json_call = dispatch
        controller.claim_new([blocked])
        self.assertEqual(
            calls,
            [(
                "dispatch-plan", "--claim", "--exclude-ticket", "T-110", "--json"
            )],
        )

    def test_cancelled_run_releases_every_controller_resource(self) -> None:
        controller = CONTROL.Controller(self.args)
        claim = {
            "branch": "ticket/T-110",
            "lease": "a" * 64,
            "priority": "normal",
            "publication_lease": "b" * 64,
            "receipt": "c" * 64,
            "role": "builder",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "running",
            "ticket": "T-110",
            "worktree": str(self.root / "cell-1"),
        }
        (self.product / "factory/runs/cancelled.meta").write_text(
            "run_id=cancelled\n"
            "ticket=T-110\n"
            "role=builder\n"
            "accounting_state=cancelled_conservative\n"
            "exit_status=130\n"
            "role_exit=cancelled\n"
            f"transition_receipt_sha256={'c' * 64}\n",
            encoding="utf-8",
        )
        calls = []
        controller.passport = lambda *_args: calls.append("passport")
        controller.release = lambda *_args: calls.append("leases-and-claim")
        controller.remove_cell = lambda *_args: calls.append("cell")
        controller.event = lambda name, *_args, **_kwargs: calls.append(name)
        self.assertFalse(controller.finish_pending_run(claim))
        self.assertEqual(claim["status"], "cancelled")
        self.assertEqual(
            calls,
            ["passport", "leases-and-claim", "cell", "attempt_cancelled"],
        )

    def test_budget_wait_reopens_only_after_envelope_change(self) -> None:
        controller = CONTROL.Controller(self.args)
        cell = self.root / "cell-1"
        cell.mkdir()
        claim = {
            "branch": "ticket/T-110",
            "budget_sha256": controller.envelope_digest(),
            "lease": "a" * 64,
            "priority": "normal",
            "publication_lease": "",
            "receipt": "",
            "role": "",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "budget",
            "ticket": "T-110",
            "worktree": str(cell),
        }
        controller.save_claim(claim)
        self.assertEqual(len(controller.load_claims()), 1)
        (self.product / "factory/ENVELOPE.env").write_text(
            "PER_TICKET_BUDGET_USD=30.000000\n", encoding="utf-8"
        )
        self.assertEqual(controller.load_claims(), [])
        self.assertFalse(controller.claim_path("T-110").exists())

    def test_publication_repair_releases_merge_lease_and_preserves_checkpoint(self) -> None:
        controller = CONTROL.Controller(self.args)
        claim = {
            "lease": "a" * 64,
            "priority": "normal",
            "publication_lease": "b" * 64,
            "receipt": "",
            "role": "",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "claimed",
            "ticket": "T-110",
            "worktree": str(self.root / "cell-1"),
        }
        calls = []
        controller.json_call = lambda *_args, **_kwargs: {
            "owner": "builder", "status": "repair",
        }
        controller.release_publication = lambda item: (
            calls.append("release"), item.update(publication_lease="")
        )
        controller.migrate_passport = lambda *_args: calls.append("passport")
        controller.event = lambda *_args, **_kwargs: calls.append("event")
        controller.publication_repair(
            claim, "c" * 64, {"pr_number": 24},
        )
        self.assertEqual(calls, ["release", "passport", "event"])


if __name__ == "__main__":
    unittest.main()
