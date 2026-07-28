#!/usr/bin/env python3
"""Focused non-agent controller persistence tests."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import threading
import time
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
        self.release = self.root / ("a" * 40)
        self.release.mkdir()
        self.args = argparse.Namespace(
            launcher=self.launcher,
            product_root=self.product,
            project="relay",
            release_path=self.release,
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
                "budget_usd": "100.000000",
                "capacity": 4,
                "contract_version": "1.8.0",
                "factory_sha": "a" * 40,
                "generation": 1,
                "per_run_budget_usd": "2.000000",
                "per_ticket_budget_usd": "25.000000",
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

    def test_preflight_runs_once_before_planner_only(self) -> None:
        controller = CONTROL.Controller(self.args)
        cell = self.root / "cell-1"
        cell.mkdir()
        claim = {
            "lease": "a" * 64,
            "publication_lease": "",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "claimed",
            "ticket": "T-110",
            "worktree": str(cell),
        }
        calls = []
        controller.json_call = lambda *args, **kwargs: (
            calls.append((args, kwargs)) or {"exit_code": 0, "status": "ok"}
        )
        controller.finish_pending_run = lambda _claim: True

        controller.run_role(claim, "planner", "b" * 64, [])
        controller.run_role(claim, "builder", "c" * 64, [])

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0][0], "preflight")
        self.assertIn("planner", calls[0][0])

    def test_model_pin_relies_on_its_bounded_probes_not_an_outer_timeout(self) -> None:
        controller = CONTROL.Controller(self.args)
        cell = self.root / "cell-1"
        cell.mkdir()
        claim = {
            "branch": "ticket/T-110",
            "lease": "a" * 64,
            "priority": "normal",
            "publication_lease": "",
            "receipt": "",
            "role": "",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "claimed",
            "ticket": "T-110",
            "worktree": str(cell),
        }
        calls = []

        def json_call(*args, **kwargs):
            calls.append((args, kwargs))
            if args[0] == "state-machine":
                return {"stage": "AWAIT-OPERATOR test", "receipt": "b" * 64}
            return {}

        controller.json_call = json_call
        controller.renew = lambda _claim: None
        controller.finish_pending_run = lambda _claim: True
        self.assertEqual(
            controller.reconcile_ticket(claim)["status"], "waiting"
        )
        model_calls = [
            (args, kwargs) for args, kwargs in calls if args[:2] == ("models", "pin")
        ]
        self.assertEqual(len(model_calls), 1)
        self.assertIsNone(model_calls[0][1]["timeout"])

    def test_concurrent_tickets_serialize_model_readiness_probes(self) -> None:
        controller = CONTROL.Controller(self.args)
        claims = []
        for number in range(4):
            cell = self.root / f"cell-{number + 1}"
            cell.mkdir()
            claims.append({
                "branch": f"ticket/T-{110 + number}",
                "lease": f"{number + 1:064x}",
                "priority": "normal",
                "publication_lease": "",
                "receipt": "",
                "role": "",
                "schema": CONTROL.CLAIM_SCHEMA,
                "status": "claimed",
                "ticket": f"T-{110 + number}",
                "worktree": str(cell),
            })
        guard = threading.Lock()
        active = 0
        maximum = 0

        def json_call(*args, **_kwargs):
            nonlocal active, maximum
            if args[:2] == ("models", "pin"):
                with guard:
                    active += 1
                    maximum = max(maximum, active)
                time.sleep(0.02)
                with guard:
                    active -= 1
                return {}
            return {"stage": "AWAIT-OPERATOR test", "receipt": "b" * 64}

        controller.json_call = json_call
        controller.renew = lambda _claim: None
        controller.finish_pending_run = lambda _claim: True
        with CONTROL.ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(controller.reconcile_ticket, claims))

        self.assertEqual(maximum, 1)
        self.assertEqual({item["status"] for item in results}, {"waiting"})

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

    def test_qualification_cursor_failure_routes_to_direct_cli(self) -> None:
        (self.product / "factory/QUALIFICATION.json").write_text(
            json.dumps({
                "budget_usd": "100.000000",
                "capacity": 4,
                "contract_version": "1.8.0",
                "factory_sha": "a" * 40,
                "generation": 1,
                "per_run_budget_usd": "2.000000",
                "per_ticket_budget_usd": "25.000000",
                "schema": CONTROL.QUALIFICATION_SCHEMA,
                "target_done": 4,
                "tickets": ["T-110", "T-111", "T-112", "T-113"],
            }),
            encoding="utf-8",
        )
        controller = CONTROL.Controller(self.args)
        claim = {
            "lease": "a" * 64,
            "publication_lease": "",
            "receipt": "b" * 64,
            "role": "planner",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "running",
            "ticket": "T-112",
            "worktree": str(self.root / "cell-3"),
        }
        (self.product / "factory/runs/failed.meta").write_text(
            "run_id=failed\n"
            "ticket=T-112\n"
            "role=planner\n"
            "route_id=cursor-gpt\n"
            "accounting_state=abandoned_conservative\n"
            "exit_status=9\n"
            "role_exit=provider_failed\n"
            f"transition_receipt_sha256={'b' * 64}\n",
            encoding="utf-8",
        )
        calls = []
        controller.passport = lambda *_args: calls.append("passport")
        controller.json_call = lambda *args, **_kwargs: (
            calls.append(args) or {"failed_run_id": "failed"}
        )
        controller.migrate_passport = lambda *_args: calls.append("migrate")
        controller.event = lambda *_args, **_kwargs: calls.append("event")
        self.assertTrue(controller.finish_pending_run(claim))
        self.assertEqual(claim["status"], "claimed")
        self.assertEqual(claim["receipt"], "")
        self.assertIn(
            (
                "models", "fallback-auto", "--ticket", "T-112",
                "--failed-run", "failed", "--workdir", claim["worktree"],
                "--reason", "provider_unavailable", "--json",
            ),
            calls,
        )
        self.assertFalse(
            any(
                isinstance(call, tuple) and call and call[0] == "release"
                for call in calls
            )
        )

    def test_invalid_reviewer_output_retries_only_reviewer(self) -> None:
        controller = CONTROL.Controller(self.args)
        claim = {
            "lease": "a" * 64,
            "publication_lease": "",
            "receipt": "b" * 64,
            "role": "reviewer",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "running",
            "ticket": "T-110",
            "worktree": str(self.root / "cell-1"),
        }
        (self.product / "factory/runs/invalid.meta").write_text(
            "run_id=invalid\n"
            "ticket=T-110\n"
            "role=reviewer\n"
            "accounting_state=abandoned_conservative\n"
            "exit_status=11\n"
            "role_exit=role_exit_invalid_output\n"
            f"transition_receipt_sha256={'b' * 64}\n",
            encoding="utf-8",
        )
        calls = []
        controller.passport = lambda *_args: calls.append("passport")
        controller.migrate_passport = lambda *_args: calls.append("migrate")
        controller.event = lambda name, *_args, **_kwargs: calls.append(name)
        self.assertTrue(controller.finish_pending_run(claim))
        self.assertEqual(claim["status"], "claimed")
        self.assertEqual(claim["receipt"], "")
        self.assertEqual(claim["role"], "")
        self.assertEqual(
            calls, ["passport", "migrate", "role_output_rejected"]
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

    def test_stale_publication_refreshes_before_acquiring_merge_lease(self) -> None:
        controller = CONTROL.Controller(self.args)
        claim = {
            "lease": "a" * 64,
            "priority": "normal",
            "publication_lease": "b" * 64,
            "ticket": "T-110",
            "worktree": str(self.root / "cell-1"),
        }
        calls = []
        controller.protected_base_current = lambda *_args: False
        controller.release_publication = lambda item: (
            calls.append("release"), item.update(publication_lease="")
        )
        controller.json_call = lambda *_args, **_kwargs: {
            "action": "refresh", "head": "d" * 40,
        }
        controller.migrate_passport = lambda *_args: calls.append("passport")
        controller.event = lambda *_args, **_kwargs: calls.append("event")
        self.assertFalse(
            controller.publication_ready(claim, "c" * 64, "d" * 40)
        )
        self.assertEqual(calls, ["release", "passport", "event"])


if __name__ == "__main__":
    unittest.main()
