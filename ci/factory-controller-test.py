#!/usr/bin/env python3
"""Focused non-agent controller persistence tests."""

from __future__ import annotations

import argparse
import copy
import hashlib
import hmac
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "factory_controller", ROOT / "scripts/factory-controller.py"
)
assert SPEC and SPEC.loader
CONTROL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CONTROL)

STATE_SPEC = importlib.util.spec_from_file_location(
    "state_machine_for_controller_test", ROOT / "scripts/state-machine.py"
)
assert STATE_SPEC and STATE_SPEC.loader
STATE = importlib.util.module_from_spec(STATE_SPEC)
STATE_SPEC.loader.exec_module(STATE)

ATTEST_SPEC = importlib.util.spec_from_file_location(
    "ticket_attest_for_controller_test", ROOT / "scripts/ticket-attest.py"
)
assert ATTEST_SPEC and ATTEST_SPEC.loader
ATTEST = importlib.util.module_from_spec(ATTEST_SPEC)
ATTEST_SPEC.loader.exec_module(ATTEST)


def state_transition(
    stage: str, receipt: str = "b" * 64, ticket: str = "T-110"
) -> dict:
    return {
        "action": stage.partition(" ")[0],
        "detail": stage.partition(" ")[2] or None,
        "receipt": receipt,
        "role": STATE.stage_role(stage),
        "schema": "nysa.software-factory.state-machine/v1",
        "stage": stage,
        "status": "ok",
        "ticket": ticket,
    }


class FactoryControllerTest(unittest.TestCase):
    def test_launch_agent_does_not_throttle_bounded_provider_probes(self) -> None:
        template = ROOT / "scripts/launchd/com.factory.controller.plist.template"
        with template.open("rb") as handle:
            job = plistlib.load(handle)
        self.assertEqual(job["ProcessType"], "Interactive")

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

    def test_launch_void_blocks_once_and_preserves_role_receipt(self) -> None:
        controller = CONTROL.Controller(self.args)
        receipt = "b" * 64
        claim = {
            "branch": "ticket/T-110",
            "lease": "c" * 64,
            "publication_lease": "",
            "receipt": receipt,
            "role": "test-author",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "running",
            "ticket": "T-110",
            "worktree": str(self.root / "cell-1"),
        }
        Path(claim["worktree"]).mkdir()
        controller.save_claim(claim)
        for number in (1, 2):
            (self.product / f"factory/runs/void-{number}.meta").write_text(
                f"run_id=void-{number}\n"
                "phase=abandoned\n"
                "ticket=T-110\n"
                "role=test-author\n"
                "accounting_state=launch_void\n"
                "go_issued=0\n"
                "task_submitted=0\n"
                "effective_cost=0\n"
                "cost_basis=launch_void\n"
                "exit_status=6\n"
                "role_exit=\n"
                f"kit_sha={'a' * 40}\n"
                f"role_head_before={'d' * 40}\n"
                "terminal_reason_code=cursor_credential_unsafe\n"
                f"terminal_at=2026-07-30T15:0{number}:00Z\n"
                f"transition_receipt_sha256={receipt}\n",
                encoding="utf-8",
            )
        calls = []
        controller.json_call = lambda *args, **_kwargs: calls.append(args) or {}
        controller.passport = lambda *_args: self.fail(
            "pre-GO attempts must not replace successful-role passport evidence"
        )

        self.assertFalse(controller.finish_pending_run(claim))
        self.assertEqual(claim["status"], "blocked")
        self.assertEqual(claim["receipt"], receipt)
        self.assertEqual(claim["role"], "test-author")
        self.assertTrue(claim["lease_released"])
        self.assertEqual(calls, [
            ("release", "--ticket", "T-110", "--lease", "c" * 64),
        ])
        events = [
            CONTROL.read(path) for path in sorted(self.state.glob("events/*.json"))
        ]
        terminal = next(
            item for item in events if item["event"] == "attempt_terminal"
        )
        self.assertEqual(terminal["run_id"], "void-2")
        self.assertEqual(terminal["duplicate_launch_void_count"], 2)
        blocked = next(
            item for item in events if item["event"] == "pre_go_failure_blocked"
        )
        self.assertEqual(blocked["reason"], "cursor_credential_unsafe")

    def test_prior_release_launch_void_retries_stage_once(self) -> None:
        controller = CONTROL.Controller(self.args)
        receipt = "b" * 64
        claim = {
            "branch": "ticket/T-110",
            "lease": "c" * 64,
            "publication_lease": "",
            "receipt": receipt,
            "role": "test-author",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "running",
            "ticket": "T-110",
            "worktree": str(self.root / "cell-1"),
        }
        Path(claim["worktree"]).mkdir()
        controller.save_claim(claim)
        (self.product / "factory/runs/prior-void.meta").write_text(
            "run_id=prior-void\n"
            "phase=abandoned\n"
            "ticket=T-110\n"
            "role=test-author\n"
            "accounting_state=launch_void\n"
            "go_issued=0\n"
            "task_submitted=0\n"
            "effective_cost=0\n"
            "cost_basis=launch_void\n"
            "exit_status=6\n"
            "role_exit=\n"
            f"kit_sha={'e' * 40}\n"
            f"role_head_before={'d' * 40}\n"
            "terminal_reason_code=cursor_credential_unsafe\n"
            f"transition_receipt_sha256={receipt}\n",
            encoding="utf-8",
        )

        self.assertTrue(controller.finish_pending_run(claim))
        self.assertEqual(claim["status"], "claimed")
        self.assertEqual(claim["receipt"], "")
        self.assertEqual(claim["role"], "")
        self.assertTrue(controller.finish_pending_run(claim))
        events = [
            CONTROL.read(path) for path in sorted(self.state.glob("events/*.json"))
        ]
        self.assertEqual(
            sum(
                item["event"] == "pre_go_failure_recovered_by_release_upgrade"
                for item in events
            ),
            1,
        )

    def test_attempt_progress_is_content_free_and_monotonic(self) -> None:
        controller = CONTROL.Controller(self.args)
        receipt = "b" * 64
        claim = {
            "receipt": receipt,
            "role": "builder",
            "ticket": "T-110",
        }
        (self.product / "factory/runs/live.meta").write_text(
            "run_id=live\n"
            "ticket=T-110\n"
            "role=builder\n"
            "adapter=cursor-anthropic\n"
            "route_id=cursor-claude\n"
            "provider_attempt_id=live-cli\n"
            "accounting_state=reserved\n"
            "go_issued=1\n"
            "task_submitted=1\n"
            f"transition_receipt_sha256={receipt}\n",
            encoding="utf-8",
        )
        progress = self.product / "factory/runs/live.progress.jsonl"
        records = [
            {
                "event_sha256": "c" * 64,
                "observed_monotonic_ns": 100,
                "sequence": 1,
                "subtype": "",
                "type": "assistant",
            },
            {
                "event_sha256": "d" * 64,
                "observed_monotonic_ns": 200,
                "sequence": 2,
                "subtype": "completed",
                "type": "tool_call",
            },
        ]
        progress.write_text(json.dumps(records[0]) + "\n", encoding="utf-8")
        progress.chmod(0o600)

        controller.observe_attempt(claim)
        controller.observe_attempt(claim)
        with progress.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(records[1]) + "\n")
        controller.observe_attempt(claim)
        (self.product / "factory/runs/duplicate-live.meta").write_text(
            (self.product / "factory/runs/live.meta").read_text(
                encoding="utf-8"
            ).replace("run_id=live\n", "run_id=duplicate-live\n"),
            encoding="utf-8",
        )
        controller.observe_attempt_safely(claim)

        events = [
            CONTROL.read(path) for path in sorted(self.state.glob("events/*.json"))
        ]
        self.assertEqual(
            [item["event"] for item in events].count("attempt_bound"), 1
        )
        progress_events = [
            item for item in events if item["event"] == "attempt_progress"
        ]
        self.assertEqual(
            [item["progress_events"] for item in progress_events], [1, 2]
        )
        self.assertEqual(progress_events[-1]["latest_type"], "tool_call")
        self.assertNotIn("output", progress_events[-1])
        self.assertEqual(
            [item["event"] for item in events].count(
                "attempt_observation_invalid"
            ),
            1,
        )

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
        stale = {
            "factory_sha": "b" * 40,
            "schema": CONTROL.EVENT_SCHEMA,
            "tickets": tickets,
        }
        for name in (
            "qualification-restart-boundary", "qualification-recovered",
        ):
            (self.state / f"{name}.json").write_text(
                json.dumps(stale), encoding="utf-8",
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
        current_boundary = self.state / (
            f"qualification-restart-boundary-{'a' * 40}.json"
        )
        self.assertEqual(
            json.loads(current_boundary.read_text(encoding="utf-8")),
            {
                "factory_sha": "a" * 40,
                "schema": CONTROL.EVENT_SCHEMA,
                "tickets": tickets,
            },
        )

        second = CONTROL.Controller(self.args)
        second.pin_routes = lambda _claims: []
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
        self.assertTrue(
            (self.state / f"qualification-recovered-{'a' * 40}.json").is_file()
        )

    def test_three_ticket_qualification_parks_an_excluded_claim(self) -> None:
        tickets = [f"T-{number}" for number in range(110, 113)]
        (self.product / "factory/PROJECT.env").write_text(
            "MAX_CONCURRENT_TICKETS=3\n", encoding="utf-8"
        )
        (self.product / "factory/QUALIFICATION.json").write_text(
            json.dumps({
                "budget_usd": "100.000000",
                "capacity": 3,
                "contract_version": "1.8.0",
                "factory_sha": "a" * 40,
                "generation": 2,
                "per_run_budget_usd": "2.000000",
                "per_ticket_budget_usd": "25.000000",
                "schema": CONTROL.QUALIFICATION_SCHEMA,
                "target_done": 3,
                "tickets": tickets,
            }),
            encoding="utf-8",
        )
        controller = CONTROL.Controller(self.args)
        for number, ticket in enumerate([*tickets, "T-113"], 1):
            cell = self.root / f"cell-{number}"
            cell.mkdir()
            controller.save_claim({
                "branch": f"ticket/{ticket}",
                "lease": f"{number:064x}",
                "priority": "normal",
                "publication_lease": "",
                "receipt": "",
                "role": "",
                "schema": CONTROL.CLAIM_SCHEMA,
                "status": "claimed",
                "ticket": ticket,
                "worktree": str(cell),
            })
        seen = []
        controller.recover_missing_passport_claims = lambda claims: seen.extend(
            claim["ticket"] for claim in claims
        )
        controller.recover_upgraded_claims = lambda _claims: None
        controller.recover_repaired_failures = lambda _claims: None
        controller.claim_new = lambda claims: claims
        withdrawn = []
        controller.withdraw_publication = lambda claim: withdrawn.append(
            claim["ticket"]
        )
        result = controller.reconcile()
        self.assertEqual(result["status"], "restart_required")
        self.assertEqual(result["active"], 3)
        self.assertEqual(seen, tickets)
        self.assertEqual(withdrawn, ["T-113"])
        self.assertTrue(controller.claim_path("T-113").exists())

    def test_three_ticket_successor_qualification_accepts_live_envelope(self) -> None:
        tickets = [f"T-{number}" for number in range(110, 113)]
        (self.product / "factory/PROJECT.env").write_text(
            "MAX_CONCURRENT_TICKETS=3\n", encoding="utf-8"
        )
        (self.product / "factory/QUALIFICATION.json").write_text(
            json.dumps({
                "budget_usd": "300.000000",
                "capacity": 3,
                "contract_version": "1.8.0",
                "factory_sha": "a" * 40,
                "generation": 1,
                "mode": "successor",
                "per_run_budget_usd": "10.000000",
                "per_ticket_budget_usd": "100.000000",
                "schema": CONTROL.QUALIFICATION_SCHEMA,
                "source_factory_sha": "b" * 40,
                "target_done": 3,
                "tickets": tickets,
            }),
            encoding="utf-8",
        )
        controller = CONTROL.Controller(self.args)
        self.assertEqual(controller.qualification["mode"], "successor")
        self.assertEqual(controller.qualification["tickets"], tickets)

    def test_three_ticket_restart_includes_prior_release_launch_void(self) -> None:
        tickets = [f"T-{number}" for number in range(110, 113)]
        old_factory = "b" * 40
        receipt = "c" * 64
        (self.product / "factory/PROJECT.env").write_text(
            "MAX_CONCURRENT_TICKETS=3\n", encoding="utf-8"
        )
        (self.product / "factory/QUALIFICATION.json").write_text(
            json.dumps({
                "budget_usd": "300.000000",
                "capacity": 3,
                "contract_version": "1.8.0",
                "factory_sha": self.release.name,
                "generation": 1,
                "mode": "successor",
                "per_run_budget_usd": "10.000000",
                "per_ticket_budget_usd": "100.000000",
                "schema": CONTROL.QUALIFICATION_SCHEMA,
                "source_factory_sha": old_factory,
                "target_done": 3,
                "tickets": tickets,
            }),
            encoding="utf-8",
        )
        controller = CONTROL.Controller(self.args)
        passports = self.state / "passports"
        passports.mkdir(mode=0o700)
        for number, ticket in enumerate(tickets, 1):
            cell = self.root / "parked" / ticket
            cell.mkdir(parents=True)
            controller.save_claim({
                "branch": f"ticket/{ticket}",
                "lease": f"{number:064x}",
                "parked": True,
                "priority": "normal",
                "publication_lease": "",
                "receipt": receipt if number == 1 else "",
                "role": "narrator" if number == 1 else "",
                "schema": CONTROL.CLAIM_SCHEMA,
                "status": "blocked" if number == 1 else "claimed",
                "ticket": ticket,
                "worktree": str(cell),
            })
            CONTROL.write(
                passports / f"{ticket}.json", {"factory_sha": old_factory},
            )
        (self.product / "factory/runs/prior-void.meta").write_text(
            "run_id=prior-void\n"
            "phase=abandoned\n"
            "ticket=T-110\n"
            "role=narrator\n"
            "accounting_state=launch_void\n"
            "go_issued=0\n"
            "task_submitted=0\n"
            "effective_cost=0\n"
            "cost_basis=launch_void\n"
            "exit_status=6\n"
            "role_exit=\n"
            f"kit_sha={old_factory}\n"
            f"transition_receipt_sha256={receipt}\n",
            encoding="utf-8",
        )

        def migrate(claim, _publication):
            CONTROL.write(
                passports / f"{claim['ticket']}.json",
                {"factory_sha": self.release.name},
            )

        controller.recover_missing_passport_claims = lambda _claims: None
        controller.ticket_release_current = lambda _claim: True
        controller.renew = lambda _claim: None
        controller.migrate_passport = migrate
        controller.restore_contract_blocker = lambda _claim: False
        controller.claim_new = lambda claims: claims

        result = controller.reconcile()

        self.assertEqual(result["status"], "restart_required")
        self.assertEqual(result["active"], 3)
        recovered = CONTROL.read(controller.claim_path("T-110"))
        self.assertEqual(recovered["status"], "running")
        self.assertEqual(recovered["receipt"], receipt)

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
        controller.terminal_for_receipt = lambda *_args: {}
        controller.finish_pending_run = lambda _claim: True

        controller.run_role(claim, "planner", "b" * 64, [])
        controller.run_role(claim, "builder", "c" * 64, [])

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0][0], "preflight")
        self.assertIn("planner", calls[0][0])

    def test_narrator_receives_trusted_publication_context(self) -> None:
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
        captured = []

        class CompletedProcess:
            def __init__(self, command, **_kwargs):
                captured.append(command)

            @staticmethod
            def wait(timeout=None):
                return 0

        controller.ensure_execution_cell = lambda _claim: None
        controller.terminal_for_receipt = lambda *_args: {}
        controller.finish_pending_run = lambda _claim: True
        publication = {
            "checks": [],
            "head": "b" * 40,
            "pr_number": 7,
            "preview_urls": [
                "https://api-example-pr-7.up.railway.app",
                "https://web-example-pr-7.up.railway.app",
            ],
            "status": "ready",
            "url": "https://github.com/example/product/pull/7",
        }
        with patch.object(CONTROL.subprocess, "Popen", CompletedProcess):
            controller.run_role(
                claim, "narrator", "c" * 64, [], publication,
            )
        task = captured[0][-1]
        self.assertIn("PR #7", task)
        self.assertIn("web-example-pr-7.up.railway.app", task)
        self.assertIn("Do not run tests", task)

    def test_role_launch_without_terminal_blocks_once(self) -> None:
        controller = CONTROL.Controller(self.args)
        exit_statuses = [0, 3]
        releases = []

        class MissingTerminalProcess:
            def __init__(self, *_args, **_kwargs):
                pass

            @staticmethod
            def wait(timeout=None):
                return exit_statuses.pop(0)

        controller.ensure_execution_cell = lambda _claim: None
        controller.release_ticket_lease = lambda claim: releases.append(
            claim["ticket"]
        )
        with patch.object(CONTROL.subprocess, "Popen", MissingTerminalProcess):
            for number in (110, 111):
                ticket = f"T-{number}"
                cell = self.root / f"cell-{number}"
                cell.mkdir()
                receipt = f"{number:064x}"
                claim = {
                    "branch": f"ticket/{ticket}",
                    "lease": "a" * 64,
                    "priority": "normal",
                    "publication_lease": "",
                    "receipt": "",
                    "role": "",
                    "schema": CONTROL.CLAIM_SCHEMA,
                    "status": "claimed",
                    "ticket": ticket,
                    "worktree": str(cell),
                }
                controller.run_role(claim, "builder", receipt, [])
                self.assertEqual(claim["status"], "blocked")
                self.assertEqual(claim["receipt"], receipt)
                self.assertEqual(claim["role"], "builder")

        self.assertEqual(releases, ["T-110", "T-111"])
        events = [
            CONTROL.read(path) for path in sorted(self.state.glob("events/*.json"))
        ]
        missing = [
            item for item in events
            if item["event"] == "role_launch_missing_terminal"
        ]
        self.assertEqual(
            [(item["ticket"], item["exit_status"]) for item in missing],
            [("T-110", 0), ("T-111", 3)],
        )

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
            route = cell / "factory/route-plans/T-110.json"
            route.parent.mkdir(parents=True)
            route.write_text("{}\n", encoding="utf-8")
            return {
                "pins": [{}], "schema": "model-pin-batch/v1", "status": "ok",
            }

        controller.json_call = json_call
        self.assertEqual(controller.pin_routes([claim]), [])
        model_calls = [
            (args, kwargs)
            for args, kwargs in calls
            if args[:2] == ("models", "pin-batch")
        ]
        self.assertEqual(len(model_calls), 1)
        self.assertIsNone(model_calls[0][1]["timeout"])

    def test_state_machine_relies_on_its_bounded_helpers_not_an_outer_timeout(
        self,
    ) -> None:
        controller = CONTROL.Controller(self.args)
        cell = self.root / "cell-1"
        route = cell / "factory/route-plans/T-110.json"
        route.parent.mkdir(parents=True)
        route.write_text("{}\n", encoding="utf-8")
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
                return state_transition("FIX builder")
            if args[:2] == ("publication", "withdraw"):
                return {"status": "absent"}
            raise AssertionError(args)

        controller.json_call = json_call
        controller.ensure_lease = lambda *_args: None
        controller.finish_pending_run = lambda _claim: True
        controller.refresh_dependency_tracking = lambda _claim: True
        controller.run_role = lambda *_args: None

        self.assertEqual(
            controller.reconcile_ticket(claim),
            {"status": "progressed", "ticket": "T-110"},
        )
        state_machine_calls = [
            (args, kwargs)
            for args, kwargs in calls
            if args[0] == "state-machine"
        ]
        self.assertEqual(len(state_machine_calls), 1)
        self.assertIn("timeout", state_machine_calls[0][1])
        self.assertIsNone(state_machine_calls[0][1]["timeout"])

    def test_state_machine_transition_envelope_mutations_fail_before_provider(
        self,
    ) -> None:
        cell = self.root / "cell-1"
        route = cell / "factory/route-plans/T-110.json"
        route.parent.mkdir(parents=True)
        route.write_text("{}\n", encoding="utf-8")
        valid = {
            "action": "FIX",
            "detail": "builder",
            "receipt": "b" * 64,
            "role": "builder",
            "schema": "nysa.software-factory.state-machine/v1",
            "stage": "FIX builder",
            "status": "ok",
            "ticket": "T-110",
        }
        cases = {
            "schema": {**valid, "schema": "wrong"},
            "status": {**valid, "status": "error"},
            "ticket": {**valid, "ticket": "T-111"},
            "action": {**valid, "action": "RUN"},
            "detail": {**valid, "detail": "narrator"},
            "receipt": {**valid, "receipt": "not-a-digest"},
            "empty-stage": {**valid, "stage": "", "action": "", "detail": None},
            "unknown-stage": {
                **valid, "stage": "GARBAGE", "action": "GARBAGE",
                "detail": None, "role": None,
            },
            "role-stage-mismatch": {**valid, "role": "narrator"},
            "non-role-has-role": {
                **valid,
                "action": "AWAIT-OPERATOR",
                "detail": "product decision required",
                "role": "builder",
                "stage": "AWAIT-OPERATOR product decision required",
            },
        }

        for name, transition in cases.items():
            with self.subTest(mutation=name):
                controller = CONTROL.Controller(self.args)
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
                controller.ensure_lease = lambda *_args: None
                controller.finish_pending_run = lambda _claim: True
                controller.refresh_dependency_tracking = lambda _claim: True
                controller.json_call = lambda *_args, **_kwargs: transition
                controller.withdraw_publication = lambda *_args: None
                controller.release_ticket_lease = lambda item: item.update(
                    lease="", lease_released=True
                )
                controller.run_role = lambda *_args: self.fail(
                    f"{name} reached provider execution"
                )
                controller.ticket_pr = lambda *_args: self.fail(
                    f"{name} reached publication execution"
                )

                result = controller.reconcile_ticket(claim)

                self.assertEqual(result["status"], "error")
                self.assertEqual(
                    result["error"],
                    "state-machine returned invalid transition evidence",
                )
                self.assertEqual(claim["status"], "blocked")
                self.assertTrue(claim["lease_released"])

    def test_concurrent_tickets_share_one_batch_readiness_probe(self) -> None:
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
        calls = []

        def json_call(*args, **_kwargs):
            calls.append(args)
            for claim in claims:
                route = controller.route_path(claim)
                route.parent.mkdir(parents=True, exist_ok=True)
                route.write_text("{}\n", encoding="utf-8")
            return {
                "pins": [{} for _ in claims],
                "schema": "model-pin-batch/v1",
                "status": "ok",
            }

        controller.json_call = json_call
        self.assertEqual(controller.pin_routes(claims), [])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][:2], ("models", "pin-batch"))
        self.assertEqual(
            [calls[0][index] for index in range(3, len(calls[0]), 4)],
            [claim["ticket"] for claim in claims],
        )

    def test_temporary_model_outage_waits_four_tickets_after_one_probe(self) -> None:
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
        model_calls = 0

        def json_call(*args, **_kwargs):
            nonlocal model_calls
            if args[:2] == ("models", "pin-batch"):
                model_calls += 1
                return {
                    "error": (
                        "model pin resolution failed: "
                        "profile_temporarily_unavailable"
                    ),
                    "status": "error",
                }
            raise AssertionError("state machine must wait for model readiness")

        controller.json_call = json_call
        results = controller.pin_routes(claims)

        self.assertEqual(model_calls, 1)
        self.assertEqual({item["status"] for item in results}, {"waiting"})
        self.assertEqual({item["status"] for item in claims}, {"waiting"})

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

        class FallbackGuard:
            def __enter__(self):
                calls.append("fallback-lock")

            def __exit__(self, *_args):
                return False

        controller.fallback_lock = FallbackGuard()
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
        self.assertIn("fallback-lock", calls)
        self.assertNotIn("passport", calls)
        fallback = next(
            index
            for index, call in enumerate(calls)
            if isinstance(call, tuple) and call[:2] == ("models", "fallback-auto")
        )
        self.assertLess(fallback, calls.index("migrate"))
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
            calls, [
                "attempt_terminal", "passport", "migrate",
                "role_output_rejected",
            ]
        )

    def test_contract_block_waits_for_exact_resume_then_reclaims(self) -> None:
        controller = CONTROL.Controller(self.args)
        cell = self.root / "cell-1"
        cell.mkdir()
        receipt = "b" * 64
        head = "c" * 40
        passport_digest = "d" * 64
        claim = {
            "branch": "ticket/T-110",
            "lease": "a" * 64,
            "priority": "normal",
            "publication_lease": "",
            "receipt": receipt,
            "role": "planner",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "running",
            "ticket": "T-110",
            "worktree": str(cell),
        }
        controller.save_claim(claim)
        (self.product / "factory/runs/blocked.meta").write_text(
            "run_id=blocked\n"
            "ticket=T-110\n"
            "role=planner\n"
            "accounting_state=abandoned_conservative\n"
            "exit_status=12\n"
            "role_exit=role_exit_contract_blocked\n"
            f"transition_receipt_sha256={receipt}\n",
            encoding="utf-8",
        )
        calls = []
        controller.passport = lambda *_args: calls.append("passport")
        controller.migrate_passport = lambda *_args: calls.append("migrate")

        def json_call(*args, **_kwargs):
            calls.append(args)
            if args[:2] == ("state-machine", "block"):
                return {"status": "blocked"}
            return {}

        controller.json_call = json_call
        self.assertFalse(controller.finish_pending_run(claim))
        self.assertEqual(claim["status"], "blocked")
        self.assertEqual(
            [call if isinstance(call, str) else call[:2] for call in calls],
            [
                "passport", ("state-machine", "block"),
                ("release", "--ticket"),
            ],
        )

        (self.state / "passports").mkdir(mode=0o700)
        CONTROL.write(
            self.state / "passports/T-110.json",
            {
                "branch": claim["branch"],
                "head_sha": head,
                "passport_sha256": passport_digest,
            },
        )
        calls.clear()
        resume_status = "waiting"

        def recover_call(*args, **_kwargs):
            calls.append(args)
            if args[:2] == ("state-machine", "block"):
                return {"status": "blocked"}
            if args[:2] == ("state-machine", "resume"):
                return {"status": resume_status}
            if args[:2] == ("passport", "validate"):
                return {"passport": passport_digest, "status": "ok"}
            if args[0] == "renew":
                raise CONTROL.ControllerError("released")
            if args[0] == "claim":
                return {
                    "lease_id": "e" * 64,
                    "schema_version": 1,
                    "ticket": "T-110",
                }
            return {}

        controller.json_call = recover_call
        controller.event = lambda name, *_args, **_kwargs: calls.append((name,))
        remote = CONTROL.subprocess.CompletedProcess(
            [], 0, f"{head}\trefs/heads/{claim['branch']}\n", ""
        )
        with patch.object(CONTROL.subprocess, "run", return_value=remote):
            controller.recover_repaired_failures([claim])
        self.assertEqual(claim["status"], "blocked")
        self.assertIn(("claim", "--ticket", "T-110"), calls)
        self.assertIn(("ticket_lease_recovered",), calls)
        self.assertIn(("state-machine", "resume"), [call[:2] for call in calls])

        calls.clear()
        resume_status = "ready"
        with patch.object(CONTROL.subprocess, "run", return_value=remote):
            controller.recover_repaired_failures([claim])
        self.assertEqual(claim["status"], "claimed")
        self.assertEqual(claim["lease"], "e" * 64)
        self.assertEqual(claim["receipt"], "")
        self.assertEqual(claim["role"], "")
        self.assertIn(("contract_blocker_recovered",), calls)

    def test_recorded_repair_recovers_after_transition_receipt_was_replaced(
        self,
    ) -> None:
        controller = CONTROL.Controller(self.args)
        cell = self.root / "cell-1"
        cell.mkdir()
        claim = {
            "branch": "ticket/T-110",
            "lease": "",
            "parked": True,
            "priority": "normal",
            "publication_lease": "",
            "receipt": "",
            "role": "",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "blocked",
            "ticket": "T-110",
            "worktree": str(cell),
        }
        repairs = self.state / "contract-repairs"
        repairs.mkdir(mode=0o700)
        CONTROL.write(repairs / "T-110.json", {"authenticated": "by-state-machine"})
        calls = []

        def json_call(*args, **_kwargs):
            calls.append(args)
            if args[0] == "claim":
                return {
                    "lease_id": "e" * 64,
                    "schema_version": 1,
                    "ticket": "T-110",
                }
            return {}

        controller.json_call = json_call
        controller.remote_passport_valid = lambda _claim: True
        controller.event = lambda name, *_args, **kwargs: calls.append(
            (name, kwargs)
        )

        controller.recover_repaired_failures([claim])

        self.assertEqual(claim["status"], "claimed")
        self.assertEqual(claim["lease"], "e" * 64)
        self.assertEqual(claim["receipt"], "")
        self.assertEqual(claim["role"], "")
        self.assertIn(
            (
                "recorded_contract_repair_prepared",
                {},
            ),
            calls,
        )
        self.assertFalse(any(call[0] == "state-machine" for call in calls))

    def test_recorded_repair_recovers_claim_left_at_blocked_terminal(
        self,
    ) -> None:
        controller = CONTROL.Controller(self.args)
        cell = self.root / "cell-1"
        cell.mkdir()
        blocked_receipt = "d" * 64
        claim = {
            "branch": "ticket/T-110",
            "lease": "a" * 64,
            "parked": True,
            "priority": "normal",
            "publication_lease": "",
            "receipt": blocked_receipt,
            "role": "builder",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "blocked",
            "ticket": "T-110",
            "worktree": str(cell),
        }
        repairs = self.state / "contract-repairs"
        repairs.mkdir(mode=0o700)
        CONTROL.write(repairs / "T-110.json", {
            "blocked_receipt": blocked_receipt,
            "blocked_role": "builder",
            "repair_role": "test-author",
        })
        calls = []

        def json_call(*args, **_kwargs):
            calls.append(args)
            return {}

        controller.json_call = json_call
        controller.remote_passport_valid = lambda _claim: True
        controller.event = lambda name, *_args, **kwargs: calls.append(
            (name, kwargs)
        )

        controller.recover_repaired_failures([claim])

        self.assertEqual(claim["status"], "claimed")
        self.assertEqual(claim["lease"], "a" * 64)
        self.assertEqual(claim["receipt"], "")
        self.assertEqual(claim["role"], "")
        self.assertIn(
            (
                "recorded_contract_repair_prepared",
                {},
            ),
            calls,
        )
        self.assertFalse(any(call[0] == "state-machine" for call in calls))

    def test_recorded_repair_refuses_mismatched_blocked_claim(self) -> None:
        controller = CONTROL.Controller(self.args)
        cell = self.root / "cell-1"
        cell.mkdir()
        claim = {
            "branch": "ticket/T-110",
            "lease": "a" * 64,
            "parked": True,
            "priority": "normal",
            "publication_lease": "",
            "receipt": "d" * 64,
            "role": "builder",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "blocked",
            "ticket": "T-110",
            "worktree": str(cell),
        }
        repairs = self.state / "contract-repairs"
        repairs.mkdir(mode=0o700)
        CONTROL.write(repairs / "T-110.json", {
            "blocked_receipt": "e" * 64,
            "blocked_role": "builder",
            "repair_role": "test-author",
        })
        calls = []
        controller.json_call = lambda *args, **_kwargs: calls.append(args)
        controller.remote_passport_valid = lambda _claim: True

        self.assertFalse(controller.restore_recorded_contract_repair(claim))

        self.assertEqual(claim["status"], "blocked")
        self.assertEqual(claim["receipt"], "d" * 64)
        self.assertEqual(claim["role"], "builder")
        self.assertEqual(calls, [])

    def test_invalid_recorded_repair_fails_in_the_single_ordinary_resolution(
        self,
    ) -> None:
        controller = CONTROL.Controller(self.args)
        cell = self.root / "cell-1"
        cell.mkdir()
        claim = {
            "branch": "ticket/T-110",
            "lease": "",
            "parked": True,
            "priority": "normal",
            "publication_lease": "",
            "receipt": "",
            "role": "",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "blocked",
            "ticket": "T-110",
            "worktree": str(cell),
        }
        repairs = self.state / "contract-repairs"
        repairs.mkdir(mode=0o700)
        CONTROL.write(repairs / "T-110.json", {"invalid": True})
        calls = []

        def json_call(*args, **_kwargs):
            calls.append(args)
            if args[0] == "claim":
                return {
                    "lease_id": "e" * 64,
                    "schema_version": 1,
                    "ticket": "T-110",
                }
            if args[0] == "state-machine":
                raise CONTROL.ControllerError("repair record is invalid")
            if args[:2] == ("publication", "withdraw"):
                return {"status": "absent"}
            return {}

        controller.json_call = json_call
        controller.remote_passport_valid = lambda _claim: True
        controller.event = lambda *_args, **_kwargs: None

        self.assertTrue(controller.restore_recorded_contract_repair(claim))

        self.assertEqual(claim["status"], "claimed")
        self.assertEqual(claim["lease"], "e" * 64)
        self.assertFalse(any(call[0] == "state-machine" for call in calls))

        (cell / "factory/route-plans").mkdir(parents=True)
        (cell / "factory/route-plans/T-110.json").write_text(
            '{"ticket":"T-110"}\n', encoding="utf-8"
        )
        controller.finish_pending_run = lambda _claim: True
        controller.refresh_dependency_tracking = lambda _claim: True
        controller.ticket_merged = lambda _claim: False
        controller.run_role = lambda *_args: self.fail(
            "invalid repair evidence reached a provider role"
        )

        result = controller.reconcile_ticket(claim)

        self.assertEqual(result["status"], "error")
        self.assertEqual(claim["status"], "blocked")
        self.assertTrue(claim["lease_released"])
        self.assertEqual(
            len([call for call in calls if call[0] == "state-machine"]), 1
        )
        self.assertIn(
            ("release", "--ticket", "T-110", "--lease", "e" * 64),
            calls,
        )

    def test_composite_historical_state_replay(self) -> None:
        fixture_path = ROOT / "ci/fixtures/composite-historical-state.json"
        fixture_raw = fixture_path.read_bytes()
        fixture = json.loads(fixture_raw)
        key = hashlib.sha256(b"sanitized-composite-replay-key").digest()
        primary = fixture["primary"]
        source = fixture["release"]["source"]
        successor_a = fixture["release"]["successor_a"]
        successor_b = fixture["release"]["successor_b"]

        def digest(value) -> str:
            return hashlib.sha256(STATE.canonical(value)).hexdigest()

        def oid(label: str) -> str:
            return hashlib.sha256(label.encode()).hexdigest()[:40]

        def require(condition: bool, message: str) -> None:
            if not condition:
                raise ValueError(message)

        charges = []
        completed = []
        for number, (role, result, factory_sha) in enumerate(
            primary["charged_attempts"], 1
        ):
            run_id = f"historical-{number:02d}"
            receipt = digest(["receipt", run_id])
            charge = {
                "amount": 10_000_000,
                "factory_sha": factory_sha,
                "receipt": receipt,
                "role": role,
                "run_id": run_id,
            }
            charges.append(charge)
            if result == "ok":
                completed.append({
                    **charge,
                    "output": digest(["output", run_id]),
                })

        initial_head = oid("historical-ticket-head")
        initial_tree = oid("historical-ticket-tree")
        route_revisions = []
        route_parent = None
        for number in range(primary["initial_route_revision_count"]):
            body = {"factory_sha": source, "kind": "historical", "number": number}
            revision_hash = ATTEST.route_revision_hash(number, route_parent, body)
            route_revisions.append({
                "body": body,
                "parent_hash": route_parent,
                "revision": number,
                "revision_hash": revision_hash,
            })
            route_parent = revision_hash

        migrations = []
        migration_parent = None
        for number in range(primary["initial_migration_count"]):
            body = {
                "from_factory_sha": source,
                "from_head_sha": initial_head,
                "to_factory_sha": source,
                "to_head_sha": initial_head,
            }
            edge_hash = digest({
                "body": body,
                "parent_hash": migration_parent,
                "revision": number,
            })
            migrations.append({
                **body,
                "edge_hash": edge_hash,
                "parent_hash": migration_parent,
                "revision": number,
            })
            migration_parent = edge_hash

        charged_factories = list(dict.fromkeys(
            item["factory_sha"] for item in charges
        ))
        release_history = [
            *charged_factories,
            *[
                oid(f"historical-release-{number}")
                for number in range(
                    primary["initial_release_count"] - len(charged_factories) - 1
                )
            ],
            source,
        ]
        blocker_receipt = charges[-1]["receipt"]
        lease = digest("initial-lease")
        state = {
            "activation": {"active_factory_sha": source, "history": [source]},
            "claim": {
                "branch": primary["branch"],
                "cell": "/sealed/cells/primary",
                "head_sha": initial_head,
                "lease": lease,
                "passport_sha256": "",
                "parked": True,
                "status": "blocked",
                "ticket": primary["ticket"],
                "transition_receipt": blocker_receipt,
            },
            "conflict": {
                "blob": digest("protected-test-blob"),
                "mode": "100644",
                "path": "tests/dependency.test.ts",
                "repair_owner": "test-author",
            },
            "passport": {
                "base_sha": oid("protected-main-before-dependency"),
                "branch": primary["branch"],
                "charges": charges,
                "completed": completed,
                "cumulative_charges_micro_usd": 130_000_000,
                "factory_sha": source,
                "factory_release_history": release_history,
                "head_sha": initial_head,
                "migration_history": migrations,
                "parent_digest": migration_parent,
                "publication_state": "none",
                "route_revision_hash": route_parent,
                "ticket": primary["ticket"],
                "tree_sha": initial_tree,
            },
            "publication": {
                "approval_head": None,
                "merge_lease": None,
                "queue": [],
                "reviewed_head": None,
                "state": "none",
                "tested_head": None,
            },
            "receipt": {
                "base_sha": oid("protected-main-before-dependency"),
                "digest": blocker_receipt,
                "factory_sha": source,
                "head_sha": initial_head,
                "lease_sha256": hashlib.sha256(lease.encode()).hexdigest(),
                "route_revision_hash": route_parent,
                "tree_sha": initial_tree,
            },
            "receipt_history": [],
            "repair": {
                "blocked_receipt": blocker_receipt,
                "directive_receipt": blocker_receipt,
                "owner": primary["repair_owner"],
                "status": "pending",
            },
            "route": {"revisions": route_revisions},
            "tickets": {
                primary["ticket"]: {"provider": False, "publication": False},
                fixture["tickets"]["dependents"][0]: {
                    "dependency": primary["ticket"], "parked": True,
                    "provider": False, "publication": False, "status": "waiting",
                },
                fixture["tickets"]["dependents"][1]: {
                    "dependency": primary["ticket"], "parked": True,
                    "provider": False, "publication": False, "status": "waiting",
                },
                fixture["tickets"]["dormant"]: {
                    "provider": False, "publication": False, "status": "backlog",
                },
                fixture["tickets"]["independent_sibling"]: {
                    "charges": [], "completed": [], "provider": True,
                    "publication": False, "status": "running",
                },
            },
        }
        expected_conflict = copy.deepcopy(state["conflict"])

        def seal(value: dict) -> None:
            passport = value["passport"]
            value["claim"]["passport_sha256"] = digest(passport)
            unsigned = {
                name: item for name, item in value.items()
                if name != "authentication_sha256"
            }
            value["authentication_sha256"] = hmac.new(
                key, STATE.canonical(unsigned), hashlib.sha256
            ).hexdigest()

        def validate(value: dict) -> None:
            unsigned = {
                name: item for name, item in value.items()
                if name != "authentication_sha256"
            }
            require(
                hmac.compare_digest(
                    value.get("authentication_sha256", ""),
                    hmac.new(key, STATE.canonical(unsigned), hashlib.sha256).hexdigest(),
                ),
                "aggregate HMAC mismatch",
            )
            passport = value["passport"]
            claim = value["claim"]
            receipt = value["receipt"]
            route = value["route"]["revisions"]
            parent = None
            for number, revision in enumerate(route):
                require(revision["revision"] == number, "route revision gap")
                require(revision["parent_hash"] == parent, "route parent mismatch")
                require(
                    revision["revision_hash"] == ATTEST.route_revision_hash(
                        number, parent, revision["body"]
                    ),
                    "route revision hash mismatch",
                )
                parent = revision["revision_hash"]
            require(parent == passport["route_revision_hash"], "route tip mismatch")
            migration_parent = None
            for number, edge in enumerate(passport["migration_history"]):
                body = {
                    name: edge[name] for name in (
                        "from_factory_sha", "from_head_sha",
                        "to_factory_sha", "to_head_sha",
                    )
                }
                require(edge["revision"] == number, "passport migration gap")
                require(edge["parent_hash"] == migration_parent, "migration parent mismatch")
                require(
                    edge["edge_hash"] == digest({
                        "body": body,
                        "parent_hash": migration_parent,
                        "revision": number,
                    }),
                    "passport migration edge mismatch",
                )
                migration_parent = edge["edge_hash"]
            require(passport["parent_digest"] == migration_parent, "passport parent mismatch")
            require(claim["passport_sha256"] == digest(passport), "claim passport mismatch")
            require(claim["ticket"] == passport["ticket"], "ticket identity mismatch")
            require(claim["branch"] == passport["branch"], "branch identity mismatch")
            require(claim["head_sha"] == passport["head_sha"] == receipt["head_sha"], "head mismatch")
            require(passport["tree_sha"] == receipt["tree_sha"], "tree mismatch")
            require(passport["base_sha"] == receipt["base_sha"], "base mismatch")
            require(
                passport["route_revision_hash"] == receipt["route_revision_hash"],
                "receipt route mismatch",
            )
            require(claim["transition_receipt"] == receipt["digest"], "receipt mismatch")
            require(
                receipt["lease_sha256"] == hashlib.sha256(
                    claim["lease"].encode()
                ).hexdigest(),
                "lease mismatch",
            )
            require(
                passport["factory_sha"] == receipt["factory_sha"]
                == value["activation"]["active_factory_sha"],
                "active Factory mismatch",
            )
            require(
                passport["factory_release_history"][-1] == passport["factory_sha"],
                "release history tip mismatch",
            )
            require(
                all(
                    charge["factory_sha"] in passport["factory_release_history"]
                    for charge in passport["charges"]
                ),
                "historical charge Factory changed",
            )
            run_ids = [item["run_id"] for item in passport["charges"]]
            receipts = [item["receipt"] for item in passport["charges"]]
            outputs = [item["output"] for item in passport["completed"]]
            require(len(run_ids) == len(set(run_ids)), "duplicate charge run")
            require(len(receipts) == len(set(receipts)), "duplicate charged receipt")
            require(len(outputs) == len(set(outputs)), "duplicate role output")
            require(
                passport["cumulative_charges_micro_usd"]
                == sum(item["amount"] for item in passport["charges"]),
                "charge accounting mismatch",
            )
            require(
                {item["run_id"] for item in passport["completed"]}
                <= set(run_ids),
                "successful role lacks a charge",
            )
            require(
                value["repair"]["directive_receipt"]
                == value["repair"]["blocked_receipt"],
                "repair directive receipt mismatch",
            )
            require(value["conflict"] == expected_conflict, "unsafe conflict evidence")
            publication = value["publication"]
            if publication["state"] == "merged":
                require(publication["queue"] == [], "publication queue not drained")
                require(
                    publication["reviewed_head"] == publication["tested_head"]
                    == publication["approval_head"] == passport["head_sha"],
                    "publication head mismatch",
                )

        def new_receipt(label: str) -> None:
            state["receipt_history"].append(copy.deepcopy(state["receipt"]))
            state["receipt"] = {
                "base_sha": state["passport"]["base_sha"],
                "digest": digest(["transition", label, len(state["receipt_history"])]),
                "factory_sha": state["passport"]["factory_sha"],
                "head_sha": state["passport"]["head_sha"],
                "lease_sha256": hashlib.sha256(
                    state["claim"]["lease"].encode()
                ).hexdigest(),
                "route_revision_hash": state["passport"]["route_revision_hash"],
                "tree_sha": state["passport"]["tree_sha"],
            }
            state["claim"]["transition_receipt"] = state["receipt"]["digest"]

        def passport_migration(
            label: str, *, factory_sha: str | None = None,
            head_sha: str | None = None, tree_sha: str | None = None,
            route_change: bool = False,
        ) -> None:
            passport = state["passport"]
            old_factory = passport["factory_sha"]
            old_head = passport["head_sha"]
            target_factory = factory_sha or old_factory
            target_head = head_sha or old_head
            if route_change:
                number = len(state["route"]["revisions"])
                parent = state["route"]["revisions"][-1]["revision_hash"]
                body = {
                    "kind": "release-migration",
                    "old_factory_sha": old_factory,
                    "new_factory_sha": target_factory,
                }
                revision_hash = ATTEST.route_revision_hash(number, parent, body)
                state["route"]["revisions"].append({
                    "body": body, "parent_hash": parent,
                    "revision": number, "revision_hash": revision_hash,
                })
                passport["route_revision_hash"] = revision_hash
            number = len(passport["migration_history"])
            parent = passport["migration_history"][-1]["edge_hash"]
            body = {
                "from_factory_sha": old_factory,
                "from_head_sha": old_head,
                "to_factory_sha": target_factory,
                "to_head_sha": target_head,
            }
            edge_hash = digest({
                "body": body, "parent_hash": parent, "revision": number,
            })
            passport["migration_history"].append({
                **body, "edge_hash": edge_hash, "parent_hash": parent,
                "revision": number,
            })
            passport.update(
                factory_sha=target_factory,
                head_sha=target_head,
                parent_digest=edge_hash,
                tree_sha=tree_sha or passport["tree_sha"],
            )
            state["claim"]["head_sha"] = target_head
            if target_factory != old_factory:
                passport["factory_release_history"].append(target_factory)
                state["activation"]["active_factory_sha"] = target_factory
                state["activation"]["history"].append(target_factory)
            new_receipt(label)
            seal(state)
            validate(state)

        def append_success(role: str, label: str) -> None:
            passport = state["passport"]
            run_id = f"replay-{label}"
            charge = {
                "amount": 10_000_000,
                "factory_sha": passport["factory_sha"],
                "receipt": state["receipt"]["digest"],
                "role": role,
                "run_id": run_id,
            }
            passport["charges"].append(charge)
            passport["completed"].append({
                **charge, "output": digest(["output", run_id]),
            })
            passport["cumulative_charges_micro_usd"] += charge["amount"]

        def append_failure(role: str, label: str) -> None:
            passport = state["passport"]
            passport["charges"].append({
                "amount": 10_000_000,
                "factory_sha": passport["factory_sha"],
                "receipt": state["receipt"]["digest"],
                "role": role,
                "run_id": f"replay-{label}",
            })
            passport["cumulative_charges_micro_usd"] += 10_000_000

        seal(state)
        validate(state)
        self.assertEqual(len(state["passport"]["charges"]), 13)
        self.assertEqual(len(state["passport"]["completed"]), 6)
        self.assertEqual(
            state["passport"]["cumulative_charges_micro_usd"], 130_000_000
        )
        self.assertEqual(len(state["route"]["revisions"]), 27)
        self.assertEqual(len(state["passport"]["migration_history"]), 31)
        self.assertEqual(len(state["passport"]["factory_release_history"]), 24)

        trace = ["historical-passport-validated", "builder-blocked-and-parked"]
        provider_calls = []
        settled = {primary["ticket"]}
        launch_voids = {
            f"launch-void-{number:02d}"
            for number in range(primary["launch_void_count"])
        }
        terminalized = set()
        terminalized.update(launch_voids)
        terminalized.update(launch_voids)
        self.assertEqual(len(terminalized), 41)
        self.assertFalse(CONTROL.Controller.consumes_capacity({
            "lease": lease, "parked": True, "status": "blocked",
        }))
        self.assertIn(primary["ticket"], settled)
        trace.extend(["launch-void-terminalized-once", "invocation-settled"])

        sibling = fixture["tickets"]["independent_sibling"]
        provider_calls.append((sibling, "builder"))
        state["tickets"][sibling]["provider"] = False
        state["tickets"][sibling]["status"] = "validating"
        state["tickets"][sibling]["charges"].append("sibling-builder-run")
        state["tickets"][sibling]["completed"].append("sibling-builder-run")
        sibling_snapshot = copy.deepcopy(state["tickets"][sibling])
        trace.append("independent-sibling-progressed")

        accepted_resumes = set()
        resume_key = (state["repair"]["owner"], state["repair"]["blocked_receipt"])
        accepted_resumes.add(resume_key)
        self.assertIn(resume_key, accepted_resumes)
        self.assertFalse(resume_key not in accepted_resumes)
        trace.append("exact-resume-one-use")
        new_receipt("repair-a")
        provider_calls.append((primary["ticket"], "test-author"))
        append_success("test-author", "repair-a")
        state["repair"]["status"] = "completed"
        seal(state)
        validate(state)
        trace.append("repair-a-completed-and-archived")

        blocker_a = copy.deepcopy(state["repair"])
        planner_blocker = digest("planner-blocker")
        state["repair"] = {
            "blocked_receipt": planner_blocker,
            "directive_receipt": planner_blocker,
            "owner": "planner",
            "status": "pending",
        }
        self.assertNotEqual(blocker_a["blocked_receipt"], planner_blocker)
        self.assertTrue("FIX planner".startswith("FIX "))
        self.assertFalse("RUN planner".startswith("FIX "))
        new_receipt("planner-repair")
        provider_calls.append((primary["ticket"], "planner"))
        append_success("planner", "repair-planner")
        state["repair"]["status"] = "completed"
        seal(state)
        validate(state)
        trace.append("planner-repair-preflight-and-strict-output")

        current_blocker = digest("current-builder-blocker")
        state["repair"] = {
            "blocked_receipt": current_blocker,
            "directive_receipt": current_blocker,
            "owner": "test-author",
            "status": "pending",
        }
        passport_migration(
            "successor-a", factory_sha=successor_a, route_change=True
        )
        self.assertEqual(state["repair"]["owner"], "test-author")
        self.assertEqual(state["tickets"][sibling], sibling_snapshot)
        trace.extend(["successor-a-route-passport-migration", "controller-restart-no-replay"])

        old_identity = (state["claim"]["ticket"], state["claim"]["branch"])
        state["claim"]["cell"] = "/sealed/cells/rotated-primary"
        state["claim"]["lease"] = digest("rotated-lease")
        state["receipt"]["lease_sha256"] = hashlib.sha256(
            state["claim"]["lease"].encode()
        ).hexdigest()
        seal(state)
        validate(state)
        self.assertEqual(
            old_identity, (state["claim"]["ticket"], state["claim"]["branch"])
        )
        trace.append("cell-and-lease-rotated")

        state_machine_calls = 0
        paused_receipt = state["receipt"]["digest"]
        state_machine_calls += 1
        maintenance = True
        if maintenance:
            trace.append("stage-resolution-paused")
        self.assertEqual(state_machine_calls, 1)
        self.assertEqual(state["receipt"]["digest"], paused_receipt)
        trace.extend(["drain-refused-active-boundary", "stale-claim-refused"])

        cell = self.root / "composite-cell"
        (cell / "factory/tickets").mkdir(parents=True)
        (cell / "factory/route-plans").mkdir()
        (cell / "factory/runs").mkdir()
        (cell / "factory/tickets/T-710.md").write_text(
            "# T-710\n\nState: Building\nDepends-On: T-092\n",
            encoding="utf-8",
        )
        (cell / "factory/route-plans/T-710.json").write_text(
            '{"ticket":"T-710"}\n', encoding="utf-8"
        )

        def git(*arguments: str) -> str:
            return subprocess.run(
                ["git", "-C", str(cell), *arguments], text=True,
                capture_output=True, check=True,
            ).stdout.strip()

        git("init", "-q", "-b", "ticket/T-710")
        git("config", "user.name", "Composite Replay")
        git("config", "user.email", "replay@example.invalid")
        git("add", ".")
        git("commit", "-qm", "historical ticket checkpoint")
        git("checkout", "-qb", "main")
        (cell / "dependency.txt").write_text("T-092 merged\n", encoding="utf-8")
        git("add", "dependency.txt")
        git("commit", "-qm", "merge T-092")
        protected_head = git("rev-parse", "HEAD")
        git("update-ref", "refs/remotes/origin/main", protected_head)
        git("checkout", "-q", "ticket/T-710")

        state_args = argparse.Namespace(
            contract_version="1.8.0",
            factory_root=cell,
            factory_sha=successor_a,
            kit_dir=ROOT,
            lease=state["claim"]["lease"],
            project="replay",
            receipt="",
            require_used=False,
            role="",
            state_dir=self.state,
            ticket="T-710",
            workdir=cell,
        )
        controller = CONTROL.Controller(self.args)
        claim = {
            "branch": "ticket/T-710",
            "lease": state["claim"]["lease"],
            "parked": True,
            "priority": "normal",
            "publication_lease": "",
            "receipt": current_blocker,
            "role": "builder",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "blocked",
            "ticket": "T-710",
            "worktree": str(cell),
        }
        repairs = self.state / "contract-repairs"
        repairs.mkdir(mode=0o700, exist_ok=True)
        CONTROL.write(repairs / "T-710.json", {
            "blocked_receipt": current_blocker,
            "blocked_role": "builder",
            "repair_role": "test-author",
        })
        events = []
        transition_attempt_calls = []
        controller.remote_passport_valid = lambda _claim: True
        controller.ensure_lease = lambda _claim, _label: None
        controller.finish_pending_run = lambda _claim: True
        controller.refresh_dependency_tracking = lambda _claim: True
        controller.ticket_merged = lambda _claim: False
        controller.migrate_passport = lambda _claim, _state: None
        controller.event = lambda name, *_args, **_kwargs: events.append(name)

        def controller_json_call(*arguments, **_kwargs):
            if arguments[0] == "state-machine":
                transition_attempt_calls.append(arguments)
                return STATE.next_transition(state_args)
            if arguments[:2] == ("ticket-attest", "--ticket"):
                old_head = git("rev-parse", "HEAD")
                git("merge", "-q", "--no-edit", "origin/main")
                return {
                    "action": "dependency-refresh",
                    "attestation": {
                        "old_head": old_head,
                        "protected_head": protected_head,
                    },
                    "head": git("rev-parse", "HEAD"),
                }
            if arguments[:2] == ("publication", "withdraw"):
                return {"status": "absent"}
            raise AssertionError(f"unexpected helper call: {arguments}")

        controller.json_call = controller_json_call
        role_receipts = []

        def run_role(_claim, role, receipt, _failed_checks):
            state_args.receipt = receipt
            state_args.role = role
            STATE.verify(state_args, consume=True)
            role_receipts.append((role, receipt))

        controller.run_role = run_role
        with (
            patch.dict(
                os.environ,
                {"FACTORY_CERTIFIED_PRODUCT_ORIGIN": "sanitized-replay-origin"},
            ),
            patch.object(STATE, "protected_dependency", return_value={}),
            patch.object(STATE, "ensure_dependency_conflict_repair"),
            patch.object(
                STATE, "contract_repair_stage",
                return_value=("FIX test-author", True),
            ),
            patch.object(STATE, "migrate_passport"),
        ):
            calls_before_restore = len(transition_attempt_calls)
            self.assertTrue(controller.restore_recorded_contract_repair(claim))
            self.assertEqual(len(transition_attempt_calls), calls_before_restore)
            first = controller.reconcile_ticket(claim)
            self.assertEqual(first["status"], "progressed")
            self.assertEqual(len(transition_attempt_calls), 1)
            self.assertIn("dependency_base_refreshed", events)
            self.assertEqual(role_receipts, [])
            second = controller.reconcile_ticket(claim)
            self.assertEqual(second["status"], "progressed")
            self.assertEqual(len(transition_attempt_calls), 2)
            self.assertEqual(role_receipts[0][0], "test-author")
        trace.extend([
            "recorded-repair-prepared-without-resolution",
            "protected-dependency-refresh-provider-free",
            "repair-owner-resolved-next-attempt",
        ])

        refreshed_head = git("rev-parse", "HEAD")
        refreshed_tree = git("rev-parse", "HEAD^{tree}")
        state["passport"]["base_sha"] = protected_head
        state["receipt"]["base_sha"] = protected_head
        passport_migration(
            "dependency-refresh", head_sha=refreshed_head,
            tree_sha=refreshed_tree,
        )
        provider_calls.append((primary["ticket"], "test-author"))
        append_success("test-author", "repair-b")
        state["repair"]["status"] = "completed"
        seal(state)
        validate(state)
        trace.append("repair-b-completed-on-successor-a")

        new_receipt("reviewer-submission-unconfirmed")
        append_failure("reviewer", "reviewer-submission-unconfirmed")
        failed_submission_factory = state["passport"]["factory_sha"]
        failed_submission_receipt = state["receipt"]["digest"]
        seal(state)
        validate(state)
        self.assertEqual(failed_submission_factory, successor_a)
        self.assertEqual(
            sum(
                item["receipt"] == failed_submission_receipt
                for item in state["passport"]["charges"]
            ),
            1,
        )
        self.assertFalse(any(
            item["receipt"] == failed_submission_receipt
            for item in state["passport"]["completed"]
        ))
        trace.extend([
            "post-go-submission-unconfirmed-charged",
            "same-release-submission-replay-refused",
        ])

        passport_migration(
            "successor-b", factory_sha=successor_b, route_change=True
        )
        self.assertNotEqual(
            state["passport"]["factory_sha"], failed_submission_factory
        )
        self.assertEqual(state["tickets"][sibling], sibling_snapshot)
        trace.extend([
            "successor-b-route-passport-migration",
            "successor-release-submission-recovered",
            "second-restart-no-replay",
        ])

        state["publication"].update(queue=[primary["ticket"]], state="validating")
        trace.append("publication-returned-to-queue-tail")
        for role in ("reviewer", "narrator"):
            new_receipt(f"fresh-{role}")
            provider_calls.append((primary["ticket"], role))
            append_success(role, f"fresh-{role}")
        publication_head = state["passport"]["head_sha"]
        state["publication"].update(
            approval_head=publication_head,
            merge_lease=digest("short-product-merge-lease"),
            queue=[],
            reviewed_head=publication_head,
            state="merged",
            tested_head=publication_head,
        )
        state["passport"]["publication_state"] = "merged"
        state["tickets"][primary["ticket"]].update(
            provider=False, publication=False, status="done"
        )
        for ticket in fixture["tickets"]["dependents"]:
            state["tickets"][ticket]["status"] = "ready"
        seal(state)
        validate(state)
        trace.append("reviewer-narrator-publication-done")

        final_successes = len(state["passport"]["completed"])
        final_charges = len(state["passport"]["charges"])
        self.assertEqual(final_successes, 11)
        self.assertEqual(final_charges, 19)
        self.assertEqual(
            len([call for call in provider_calls if call[0] == primary["ticket"]]),
            5,
        )
        self.assertEqual(
            len([call for call in provider_calls if call[0] == sibling]), 1
        )
        self.assertEqual(state["tickets"][fixture["tickets"]["dormant"]]["status"], "backlog")
        self.assertEqual(len(state["route"]["revisions"]), 29)
        self.assertEqual(len(state["passport"]["migration_history"]), 34)
        self.assertEqual(len(state["passport"]["factory_release_history"]), 26)
        self.assertEqual(fixture_path.read_bytes(), fixture_raw)

        final = copy.deepcopy(state)
        sibling_final = copy.deepcopy(final["tickets"][sibling])
        providers_before_matrix = list(provider_calls)

        def resign(value: dict) -> None:
            value["claim"]["passport_sha256"] = digest(value["passport"])
            unsigned = {
                name: item for name, item in value.items()
                if name != "authentication_sha256"
            }
            value["authentication_sha256"] = hmac.new(
                key, STATE.canonical(unsigned), hashlib.sha256
            ).hexdigest()

        def tamper(value: dict, field: str) -> None:
            if field == "hmac":
                value["authentication_sha256"] = "0" * 64
                return
            if field == "passport_parent":
                value["passport"]["parent_digest"] = "0" * 64
            elif field == "migration_edge":
                value["passport"]["migration_history"][-1]["to_factory_sha"] = source
            elif field == "migration_gap":
                value["passport"]["migration_history"][-1]["revision"] += 1
            elif field == "head":
                value["claim"]["head_sha"] = oid("tampered-head")
            elif field == "tree":
                value["receipt"]["tree_sha"] = oid("tampered-tree")
            elif field == "route":
                value["passport"]["route_revision_hash"] = "0" * 64
            elif field == "base":
                value["receipt"]["base_sha"] = oid("tampered-base")
            elif field == "receipt":
                value["claim"]["transition_receipt"] = "0" * 64
            elif field == "charge_accounting":
                value["passport"]["cumulative_charges_micro_usd"] += 1
            elif field == "repair_directive_receipt":
                value["repair"]["directive_receipt"] = "0" * 64
            elif field == "conflict_blob":
                value["conflict"]["blob"] = "0" * 64
            elif field == "conflict_path":
                value["conflict"]["path"] = "apps/api/src/app.ts"
            elif field == "conflict_mode":
                value["conflict"]["mode"] = "120000"
            elif field == "lease":
                value["receipt"]["lease_sha256"] = "0" * 64
            elif field == "approval_head":
                value["publication"]["approval_head"] = oid("unreviewed-head")
            elif field == "publication_queue":
                value["publication"]["queue"] = [sibling]
            else:
                raise AssertionError(field)
            resign(value)

        self.assertEqual(set(fixture["tamper_fields"]), {
            "hmac", "passport_parent", "migration_edge", "migration_gap",
            "head", "tree", "route", "base", "receipt",
            "charge_accounting", "repair_directive_receipt", "conflict_blob",
            "conflict_path", "conflict_mode", "lease", "approval_head",
            "publication_queue",
        })
        for field in fixture["tamper_fields"]:
            altered = copy.deepcopy(final)
            tamper(altered, field)
            with self.subTest(tamper=field):
                with self.assertRaises(ValueError):
                    validate(altered)
                self.assertEqual(altered["tickets"][sibling], sibling_final)
                self.assertEqual(provider_calls, providers_before_matrix)

        self.assertEqual(len(trace), len(set(trace)))
        self.assertIn("protected-dependency-refresh-provider-free", trace)
        self.assertIn("reviewer-narrator-publication-done", trace)

    def test_upgrade_reconstructs_cleared_contract_blocker_fields(
        self,
    ) -> None:
        controller = CONTROL.Controller(self.args)
        receipt = "b" * 64
        old_factory = "c" * 40
        head = "d" * 40
        cell = self.root / "cell-1"
        (cell / "factory/route-plans").mkdir(parents=True)
        (cell / "factory/tickets").mkdir()
        (cell / "factory/route-plans/T-110.json").write_text(
            json.dumps({
                "kit_sha": self.release.name,
                "ticket": "T-110",
            }) + "\n",
            encoding="utf-8",
        )
        (cell / "factory/tickets/T-110.md").write_text(
            f"# T-110\n\nState: Review\nKit-SHA: {self.release.name}\n",
            encoding="utf-8",
        )
        claim = {
            "branch": "ticket/T-110",
            "lease": "e" * 64,
            "priority": "normal",
            "publication_lease": "",
            "receipt": "",
            "role": "",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "blocked",
            "ticket": "T-110",
            "worktree": str(cell),
        }
        controller.save_claim(claim)
        manifest = self.product / "factory/runs/migrated-block.meta"
        manifest.write_text(
            "run_id=migrated-block\n"
            "ticket=T-110\n"
            "role=builder\n"
            "phase=completed\n"
            "accounting_state=completed\n"
            "exit_status=12\n"
            "role_exit=role_exit_contract_blocked\n"
            f"kit_sha={old_factory}\n"
            f"transition_receipt_sha256={receipt}\n",
            encoding="utf-8",
        )
        CONTROL.write(
            self.state / "T-110.json",
            {
                "branch": claim["branch"],
                "consumed": True,
                "contract_version": "1.8.0",
                "factory_sha": old_factory,
                "head_sha": head,
                "receipt_sha256": receipt,
                "role": "builder",
                "schema": "nysa.software-factory.transition-receipt/v1",
                "ticket": "T-110",
            },
        )
        (self.state / "passports").mkdir(mode=0o700)
        CONTROL.write(
            self.state / "passports/T-110.json",
            {
                "branch": claim["branch"],
                "charge_records": [{
                    "contract_version": "1.8.0",
                    "factory_sha": old_factory,
                    "head_before": head,
                    "manifest_sha256": CONTROL.hashlib.sha256(
                        manifest.read_bytes()
                    ).hexdigest(),
                    "role": "builder",
                    "run_id": "migrated-block",
                    "transition_receipt_sha256": receipt,
                }],
                "completed_role_evidence": [],
                "factory_sha": old_factory,
                "ticket": "T-110",
            },
        )
        calls = []
        renew_failures = 1

        def json_call(*args, **_kwargs):
            nonlocal renew_failures
            calls.append(args)
            if args[0] == "renew" and renew_failures:
                renew_failures -= 1
                raise CONTROL.ControllerError("old lease was withdrawn")
            if args[0] == "claim":
                return {
                    "lease_id": "f" * 64,
                    "schema_version": 1,
                    "ticket": "T-110",
                }
            if args[:2] == ("passport", "migrate"):
                passport = CONTROL.read(
                    self.state / "passports/T-110.json"
                )
                passport["factory_sha"] = self.release.name
                CONTROL.write(
                    self.state / "passports/T-110.json", passport
                )
            return {}

        controller.json_call = json_call
        controller.remote_passport_valid = lambda _claim: True
        controller.event = lambda name, *_args, **_kwargs: calls.append((name,))
        controller.recover_upgraded_claims([claim])
        self.assertEqual(claim["status"], "blocked")
        self.assertEqual(claim["receipt"], receipt)
        self.assertEqual(claim["role"], "builder")
        self.assertEqual(claim["lease"], "f" * 64)
        self.assertIn(("contract_blocker_claim_restored",), calls)
        self.assertIn(("upgraded_claim_recovered",), calls)

    def test_exported_terminal_migrates_without_reexport(self) -> None:
        controller = CONTROL.Controller(self.args)
        receipt = "b" * 64
        claim = {
            "branch": "ticket/T-110",
            "lease": "a" * 64,
            "priority": "normal",
            "publication_lease": "",
            "receipt": receipt,
            "role": "reviewer",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "running",
            "ticket": "T-110",
            "worktree": str(self.root / "cell-1"),
        }
        (self.product / "factory/runs/exported.meta").write_text(
            "run_id=exported\n"
            "ticket=T-110\n"
            "role=reviewer\n"
            "accounting_state=completed\n"
            "exit_status=0\n"
            "role_exit=ok\n"
            f"transition_receipt_sha256={receipt}\n",
            encoding="utf-8",
        )
        (self.state / "passports").mkdir(mode=0o700)
        record = {
            "role": "reviewer",
            "run_id": "exported",
            "transition_receipt_sha256": receipt,
        }
        CONTROL.write(
            self.state / "passports/T-110.json",
            {
                "charge_records": [record],
                "completed_role_evidence": [record],
                "transition_receipt_sha256": receipt,
            },
        )
        calls = []
        controller.passport = lambda *_args: calls.append("export")
        controller.migrate_passport = lambda *_args: calls.append("migrate")
        controller.event = lambda name, *_args, **_kwargs: calls.append(name)
        controller.relocate_qualification_cell = lambda *_args: None
        controller.json_call = lambda *args, **_kwargs: (
            calls.append(args[0]) or {}
        )

        self.assertTrue(controller.finish_pending_run(claim))
        self.assertEqual(
            calls,
            [
                "attempt_terminal", "migrate", "terminal_export_recovered",
                "ticket-state", "migrate",
            ],
        )
        self.assertEqual(claim["status"], "claimed")
        self.assertNotIn("export", calls)

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
            [
                "attempt_terminal", "passport", "leases-and-claim", "cell",
                "attempt_cancelled",
            ],
        )

    def test_complete_releases_claim(self) -> None:
        controller = CONTROL.Controller(self.args)
        cell = self.root / "cell-1"
        route = cell / "factory/route-plans/T-110.json"
        route.parent.mkdir(parents=True)
        route.write_text("{}\n", encoding="utf-8")
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
        controller.save_claim(claim)
        controller.finish_pending_run = lambda _claim: True

        def json_call(*args, **_kwargs):
            if args[0] == "state-machine":
                return state_transition(
                    "COMPLETE attested Done is on protected main"
                )
            if args[:2] == ("publication", "withdraw"):
                return {"status": "absent"}
            return {}

        controller.json_call = json_call
        self.assertEqual(
            controller.reconcile_ticket(claim),
            {"status": "complete", "ticket": "T-110"},
        )
        self.assertFalse(controller.claim_path("T-110").exists())

    def test_factory_upgrade_authenticates_passport_before_route_migration(self) -> None:
        controller = CONTROL.Controller(self.args)
        cell = self.root / "cell-1"
        (cell / "factory/route-plans").mkdir(parents=True)
        (cell / "factory/tickets").mkdir()
        route = cell / "factory/route-plans/T-110.json"
        ticket = cell / "factory/tickets/T-110.md"
        route.write_text(
            json.dumps({"kit_sha": "b" * 40, "ticket": "T-110"}) + "\n",
            encoding="utf-8",
        )
        ticket.write_text(
            f"# T-110\n\nState: Review\nKit-SHA: {'b' * 40}\n",
            encoding="utf-8",
        )
        claim = {
            "branch": "ticket/T-110",
            "lease": "a" * 64,
            "lease_released": True,
            "priority": "normal",
            "publication_lease": "",
            "receipt": "",
            "role": "",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "blocked",
            "ticket": "T-110",
            "worktree": str(cell),
        }
        controller.save_claim(claim)
        (self.state / "passports").mkdir(mode=0o700)
        CONTROL.write(
            self.state / "passports/T-110.json",
            {"factory_sha": "b" * 40},
        )
        calls = []
        failures = 1

        def json_call(*args, **_kwargs):
            nonlocal failures
            calls.append(args)
            if args[0] == "passport":
                if failures:
                    failures -= 1
                    raise CONTROL.ControllerError("interrupted passport migration")
                CONTROL.write(
                    self.state / "passports/T-110.json",
                    {"factory_sha": "a" * 40},
                )
            if args[0] == "renew":
                raise CONTROL.ControllerError("old lease was released")
            if args[0] == "claim":
                return {
                    "lease_id": "c" * 64,
                    "schema_version": 1,
                    "ticket": "T-110",
                }
            return {}

        controller.json_call = json_call
        with self.assertRaisesRegex(
            CONTROL.ControllerError, "interrupted passport migration"
        ):
            controller.recover_upgraded_claims([claim])
        self.assertTrue(controller.marker(
            "passport-route-migration-pending-T-110-" + "a" * 40
        ))
        controller.recover_upgraded_claims([claim])
        self.assertEqual(claim["status"], "blocked")
        self.assertEqual([call[0] for call in calls], ["passport", "passport"])
        route.write_text(
            json.dumps({"kit_sha": "a" * 40, "ticket": "T-110"}) + "\n",
            encoding="utf-8",
        )
        ticket.write_text(
            f"# T-110\n\nState: Review\nKit-SHA: {'a' * 40}\n",
            encoding="utf-8",
        )
        controller.recover_upgraded_claims([claim])
        self.assertEqual(claim["status"], "claimed")
        self.assertEqual(claim["lease"], "c" * 64)
        self.assertNotIn("lease_released", claim)
        self.assertEqual(
            [call[0] for call in calls],
            ["passport", "passport", "renew", "claim", "passport"],
        )
        calls.clear()
        controller.renew = lambda _claim: calls.append(("renew-existing",))
        controller.ensure_lease(claim, "reconciliation")
        self.assertEqual(calls, [("renew-existing",)])

    def test_factory_upgrade_preserves_failed_terminal_for_recovery(self) -> None:
        controller = CONTROL.Controller(self.args)
        cell = self.root / "cell-failed-upgrade"
        cell.mkdir()
        old_factory = "b" * 40
        receipt = "c" * 64
        claim = {
            "branch": "ticket/T-110",
            "lease": "d" * 64,
            "priority": "normal",
            "publication_lease": "",
            "receipt": receipt,
            "role": "builder",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "blocked",
            "ticket": "T-110",
            "worktree": str(cell),
        }
        passports = self.state / "passports"
        passports.mkdir(mode=0o700)
        passport_path = passports / "T-110.json"
        CONTROL.write(passport_path, {"factory_sha": old_factory})
        (self.product / "factory/runs/history-failure.meta").write_text(
            "run_id=history-failure\n"
            "phase=completed\n"
            "ticket=T-110\n"
            "role=builder\n"
            "accounting_state=abandoned_conservative\n"
            "exit_status=11\n"
            "role_exit=role_exit_history_rewritten\n"
            f"kit_sha={old_factory}\n"
            f"transition_receipt_sha256={receipt}\n",
            encoding="utf-8",
        )

        def migrate(_claim, _publication):
            passport = CONTROL.read(passport_path)
            passport["factory_sha"] = self.release.name
            CONTROL.write(passport_path, passport)

        controller.ticket_release_current = lambda _claim: True
        controller.renew = lambda _claim: None
        controller.migrate_passport = migrate
        controller.restore_contract_blocker = lambda _claim: False
        controller.event = lambda *_args, **_kwargs: None

        controller.recover_upgraded_claims([claim])

        self.assertEqual(claim["status"], "blocked")
        self.assertEqual(claim["receipt"], receipt)
        self.assertEqual(claim["role"], "builder")
        self.assertEqual(
            CONTROL.read(passport_path)["factory_sha"], self.release.name
        )

    def test_successor_upgrade_reopens_qualification_cursor_failure(self) -> None:
        controller = CONTROL.Controller(self.args)
        controller.qualification = {
            "mode": "successor", "tickets": ["T-110"],
        }
        old_factory = "b" * 40
        receipt = "c" * 64
        claim = {
            "branch": "ticket/T-110",
            "lease": "d" * 64,
            "priority": "normal",
            "publication_lease": "",
            "receipt": receipt,
            "role": "builder",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "blocked",
            "ticket": "T-110",
            "worktree": str(self.root / "cell-provider-upgrade"),
        }
        passports = self.state / "passports"
        passports.mkdir(mode=0o700)
        passport_path = passports / "T-110.json"
        CONTROL.write(passport_path, {"factory_sha": old_factory})
        (self.product / "factory/runs/provider-failure.meta").write_text(
            "run_id=provider-failure\n"
            "phase=completed\n"
            "ticket=T-110\n"
            "role=builder\n"
            "route_id=cursor-gpt\n"
            "accounting_state=abandoned_conservative\n"
            "exit_status=124\n"
            "role_exit=provider_failed\n"
            f"kit_sha={old_factory}\n"
            f"transition_receipt_sha256={receipt}\n",
            encoding="utf-8",
        )

        def migrate(_claim, _publication):
            passport = CONTROL.read(passport_path)
            passport["factory_sha"] = self.release.name
            CONTROL.write(passport_path, passport)

        controller.ticket_release_current = lambda _claim: True
        controller.renew = lambda _claim: None
        controller.migrate_passport = migrate
        controller.restore_contract_blocker = lambda _claim: False
        controller.event = lambda *_args, **_kwargs: None

        controller.recover_upgraded_claims([claim])

        self.assertEqual(claim["status"], "running")
        self.assertEqual(claim["receipt"], receipt)
        self.assertEqual(claim["role"], "builder")

    def test_successor_upgrade_reopens_only_prior_release_launch_void(self) -> None:
        controller = CONTROL.Controller(self.args)
        old_factory = "b" * 40
        claims = []
        passports = self.state / "passports"
        passports.mkdir(mode=0o700)
        cases = (
            ("T-110", old_factory, {}),
            ("T-111", self.release.name, {}),
            ("T-112", old_factory, {"phase": "completed"}),
            ("T-113", old_factory, {"go_issued": "1"}),
            ("T-114", old_factory, {"task_submitted": "1"}),
            ("T-115", old_factory, {"effective_cost": "10.000000"}),
            ("T-116", old_factory, {"cost_basis": "provider_reported"}),
            ("T-117", "not-a-release", {}),
        )
        for number, (ticket, kit_sha, changes) in enumerate(cases, 1):
            receipt = f"{number:064x}"
            claim = {
                "branch": f"ticket/{ticket}",
                "lease": "d" * 64,
                "priority": "normal",
                "publication_lease": "",
                "receipt": receipt,
                "role": "narrator",
                "schema": CONTROL.CLAIM_SCHEMA,
                "status": "blocked",
                "ticket": ticket,
                "worktree": str(self.root / f"cell-{ticket}"),
            }
            claims.append(claim)
            CONTROL.write(
                passports / f"{ticket}.json", {"factory_sha": old_factory},
            )
            terminal = {
                "phase": "abandoned",
                "go_issued": "0",
                "task_submitted": "0",
                "effective_cost": "0",
                "cost_basis": "launch_void",
                **changes,
            }
            (self.product / f"factory/runs/{ticket}-void.meta").write_text(
                f"run_id={ticket}-void\n"
                f"phase={terminal['phase']}\n"
                f"ticket={ticket}\n"
                "role=narrator\n"
                "accounting_state=launch_void\n"
                f"go_issued={terminal['go_issued']}\n"
                f"task_submitted={terminal['task_submitted']}\n"
                f"effective_cost={terminal['effective_cost']}\n"
                f"cost_basis={terminal['cost_basis']}\n"
                "exit_status=6\n"
                "role_exit=\n"
                f"kit_sha={kit_sha}\n"
                f"transition_receipt_sha256={receipt}\n",
                encoding="utf-8",
            )

        def migrate(claim, _publication):
            CONTROL.write(
                passports / f"{claim['ticket']}.json",
                {"factory_sha": self.release.name},
            )

        controller.ticket_release_current = lambda _claim: True
        controller.renew = lambda _claim: None
        controller.migrate_passport = migrate
        controller.restore_contract_blocker = lambda _claim: False

        controller.recover_upgraded_claims(claims)

        self.assertEqual(claims[0]["status"], "running")
        self.assertEqual(claims[0]["receipt"], f"{1:064x}")
        for claim in claims[1:]:
            with self.subTest(ticket=claim["ticket"]):
                self.assertEqual(claim["status"], "blocked")
                self.assertTrue(claim["receipt"])
        self.assertTrue(controller.finish_pending_run(claims[0]))
        self.assertTrue(controller.finish_pending_run(claims[0]))
        self.assertEqual(claims[0]["status"], "claimed")
        self.assertEqual(claims[0]["receipt"], "")
        events = [
            CONTROL.read(path) for path in sorted(self.state.glob("events/*.json"))
        ]
        self.assertEqual(
            sum(
                item["event"] == "pre_go_failure_recovered_by_release_upgrade"
                for item in events
            ),
            1,
        )

    def test_successor_upgrade_reopens_candidate_scoped_budget(self) -> None:
        controller = CONTROL.Controller(self.args)
        controller.qualification = {
            "mode": "successor", "tickets": ["T-110"],
        }
        cell = self.root / "parked/T-110"
        cell.mkdir(parents=True)
        claim = {
            "branch": "ticket/T-110",
            "budget_sha256": "b" * 64,
            "lease": "",
            "parked": True,
            "priority": "normal",
            "publication_lease": "",
            "receipt": "",
            "role": "",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "budget",
            "ticket": "T-110",
            "worktree": str(cell),
        }
        passports = self.state / "passports"
        passports.mkdir(mode=0o700)
        passport_path = passports / "T-110.json"
        CONTROL.write(passport_path, {"factory_sha": "b" * 40})

        def json_call(*args, **_kwargs):
            if args[0] == "claim":
                return {
                    "lease_id": "c" * 64,
                    "schema_version": 1,
                    "ticket": "T-110",
                }
            raise CONTROL.ControllerError("prior lease is unavailable")

        def migrate(_claim, _publication):
            CONTROL.write(
                passport_path, {"factory_sha": self.release.name},
            )

        controller.json_call = json_call
        controller.ticket_release_current = lambda _claim: True
        controller.migrate_passport = migrate
        controller.event = lambda *_args, **_kwargs: None

        controller.recover_upgraded_claims([claim])

        self.assertEqual(claim["status"], "claimed")
        self.assertEqual(claim["lease"], "c" * 64)
        self.assertNotIn("budget_sha256", claim)
        claim.update(status="budget", budget_sha256="d" * 64)
        controller.recover_upgraded_claims([claim])
        self.assertEqual(claim["status"], "budget")
        self.assertEqual(claim["budget_sha256"], "d" * 64)
        controller.qualification = None
        CONTROL.write(passport_path, {"factory_sha": "b" * 40})
        controller.recover_upgraded_claims([claim])
        self.assertEqual(claim["status"], "budget")
        self.assertEqual(
            CONTROL.read(passport_path)["factory_sha"], "b" * 40,
        )

    def test_factory_upgrade_recovers_waiting_claim_before_reconciliation(
        self,
    ) -> None:
        controller = CONTROL.Controller(self.args)
        cell = self.root / "parked/T-110"
        (cell / "factory/route-plans").mkdir(parents=True)
        (cell / "factory/tickets").mkdir()
        (cell / "factory/route-plans/T-110.json").write_text(
            json.dumps({
                "kit_sha": "b" * 40,
                "ticket": "T-110",
            }) + "\n",
            encoding="utf-8",
        )
        (cell / "factory/tickets/T-110.md").write_text(
            f"# T-110\n\nState: Building\nKit-SHA: {'b' * 40}\n",
            encoding="utf-8",
        )
        claim = {
            "branch": "ticket/T-110",
            "lease": "",
            "parked": True,
            "priority": "normal",
            "publication_lease": "",
            "receipt": "",
            "role": "",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "waiting",
            "ticket": "T-110",
            "worktree": str(cell),
        }
        (self.state / "passports").mkdir(mode=0o700)
        passport_path = self.state / "passports/T-110.json"
        CONTROL.write(passport_path, {"factory_sha": "b" * 40})
        calls = []

        def json_call(*args, **_kwargs):
            calls.append(args)
            if args[0] == "renew":
                raise CONTROL.ControllerError("waiting lease was released")
            if args[0] == "claim":
                return {
                    "lease_id": "c" * 64,
                    "schema_version": 1,
                    "ticket": "T-110",
                }
            return {}

        def migrate(_claim, _publication):
            passport = CONTROL.read(passport_path)
            passport["factory_sha"] = self.release.name
            CONTROL.write(passport_path, passport)

        controller.json_call = json_call
        controller.migrate_passport = migrate
        controller.restore_contract_blocker = lambda _claim: False
        controller.event = lambda name, *_args, **_kwargs: calls.append((name,))

        controller.recover_upgraded_claims([claim])

        self.assertEqual(claim["status"], "blocked")
        self.assertEqual(claim["lease"], "")
        self.assertEqual(
            CONTROL.read(passport_path)["factory_sha"], self.release.name
        )
        self.assertNotIn(("claim", "--ticket", "T-110"), calls)

        (cell / "factory/route-plans/T-110.json").write_text(
            json.dumps({
                "kit_sha": self.release.name,
                "ticket": "T-110",
            }) + "\n",
            encoding="utf-8",
        )
        (cell / "factory/tickets/T-110.md").write_text(
            f"# T-110\n\nState: Building\nKit-SHA: {self.release.name}\n",
            encoding="utf-8",
        )
        controller.recover_upgraded_claims([claim])

        self.assertEqual(claim["status"], "claimed")
        self.assertEqual(claim["lease"], "c" * 64)
        self.assertEqual(
            CONTROL.read(passport_path)["factory_sha"], self.release.name
        )
        self.assertIn(("upgraded_claim_recovered",), calls)
        self.assertTrue(controller.marker(
            "passport-route-migration-complete-T-110-" + self.release.name
        ))

        first_calls = list(calls)
        claim.update(lease="", status="waiting")
        controller.recover_upgraded_claims([claim])
        self.assertEqual(calls, first_calls)
        self.assertEqual(claim["status"], "waiting")

    def test_maintenance_after_stage_resolution_parks_without_provider(
        self,
    ) -> None:
        controller = CONTROL.Controller(self.args)
        parked = self.root / "parked/T-110"
        route = parked / "factory/route-plans/T-110.json"
        route.parent.mkdir(parents=True)
        route.write_text("{}\n", encoding="utf-8")
        claim = {
            "branch": "ticket/T-110",
            "lease": "a" * 64,
            "parked": True,
            "priority": "normal",
            "publication_lease": "",
            "receipt": "",
            "role": "",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "claimed",
            "ticket": "T-110",
            "worktree": str(parked),
        }
        controller.save_claim(claim)
        calls = []

        def json_call(*args, **_kwargs):
            calls.append(args)
            if args[0] == "state-machine":
                (self.product / "factory/MAINTENANCE").write_text(
                    "maintenance\n", encoding="utf-8"
                )
                return {
                    "action": "RUN",
                    "detail": "builder",
                    "receipt": "b" * 64,
                    "role": "builder",
                    "schema": "nysa.software-factory.state-machine/v1",
                    "stage": "RUN builder",
                    "status": "ok",
                    "ticket": "T-110",
                }
            return {}

        controller.json_call = json_call
        controller.finish_pending_run = lambda _claim: True
        controller.refresh_dependency_tracking = lambda _claim: True
        controller.run_role = lambda *_args: self.fail(
            "maintenance must stop before provider submission"
        )

        result = controller.reconcile_ticket_until_wait(claim)

        self.assertEqual(
            result, {"status": "maintenance", "ticket": "T-110"}
        )
        self.assertEqual(claim["status"], "waiting")
        self.assertEqual(claim["lease"], "")
        self.assertEqual(
            sum(call[0] == "state-machine" for call in calls), 1
        )
        self.assertIn(
            ("release", "--ticket", "T-110", "--lease", "a" * 64),
            calls,
        )
        events = [
            CONTROL.read(path)
            for path in sorted((self.state / "events").glob("*.json"))
        ]
        paused = next(
            item for item in events
            if item["event"] == "stage_resolution_paused"
        )
        self.assertEqual(
            paused["transition_receipt_sha256"], "b" * 64
        )

    def test_maintenance_does_not_hide_malformed_transition(self) -> None:
        controller = CONTROL.Controller(self.args)
        parked = self.root / "parked/T-110"
        route = parked / "factory/route-plans/T-110.json"
        route.parent.mkdir(parents=True)
        route.write_text("{}\n", encoding="utf-8")
        claim = {
            "branch": "ticket/T-110",
            "lease": "a" * 64,
            "parked": True,
            "priority": "normal",
            "publication_lease": "",
            "receipt": "",
            "role": "",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "claimed",
            "ticket": "T-110",
            "worktree": str(parked),
        }
        controller.save_claim(claim)

        def json_call(*args, **_kwargs):
            if args[0] == "state-machine":
                (self.product / "factory/MAINTENANCE").write_text(
                    "maintenance\n", encoding="utf-8"
                )
                return {
                    "action": "GARBAGE",
                    "detail": None,
                    "receipt": "b" * 64,
                    "role": None,
                    "schema": "nysa.software-factory.state-machine/v1",
                    "stage": "GARBAGE",
                    "status": "ok",
                    "ticket": "T-110",
                }
            if args[:2] == ("publication", "withdraw"):
                return {"status": "absent"}
            return {}

        controller.json_call = json_call
        controller.finish_pending_run = lambda _claim: True
        controller.refresh_dependency_tracking = lambda _claim: True
        result = controller.reconcile_ticket_until_wait(claim)

        self.assertEqual(result["status"], "error")
        self.assertEqual(
            result["error"],
            "maintenance boundary has invalid transition evidence",
        )
        self.assertEqual(claim["status"], "blocked")
        events = [
            CONTROL.read(path)
            for path in sorted((self.state / "events").glob("*.json"))
        ]
        self.assertNotIn(
            "stage_resolution_paused", {item["event"] for item in events}
        )

    def test_repaired_failure_reclaims_only_exact_remote_passport(self) -> None:
        controller = CONTROL.Controller(self.args)
        cell = self.root / "cell-1"
        cell.mkdir()
        receipt = "b" * 64
        head = "c" * 40
        passport_digest = "d" * 64
        claim = {
            "branch": "ticket/T-110",
            "lease": "a" * 64,
            "priority": "normal",
            "publication_lease": "",
            "receipt": receipt,
            "role": "test-author",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "blocked",
            "ticket": "T-110",
            "worktree": str(cell),
        }
        controller.save_claim(claim)
        (self.state / "passports").mkdir(mode=0o700)
        CONTROL.write(
            self.state / "passports/T-110.json",
            {
                "branch": claim["branch"],
                "head_sha": head,
                "passport_sha256": passport_digest,
                "publication_state": "none",
            },
        )
        (self.product / "factory/runs/failed.meta").write_text(
            "run_id=failed\n"
            "ticket=T-110\n"
            "role=test-author\n"
            "accounting_state=abandoned_conservative\n"
            "exit_status=11\n"
            "role_exit=role_exit_push_failed\n"
            f"transition_receipt_sha256={receipt}\n",
            encoding="utf-8",
        )
        calls = []

        def json_call(*args, **_kwargs):
            calls.append(args)
            if args[0] == "passport":
                return {"passport": passport_digest, "status": "ok"}
            if args[0] == "renew":
                raise CONTROL.ControllerError("failed run released its lease")
            if args[0] == "claim":
                return {
                    "lease_id": "e" * 64,
                    "schema_version": 1,
                    "ticket": "T-110",
                }
            return {}

        controller.json_call = json_call
        controller.event = lambda name, *_args, **_kwargs: calls.append((name,))
        exported = set()
        controller.terminal_already_exported = lambda _claim, terminal: (
            terminal["run_id"] in exported
        )

        def export(_claim, _state):
            terminal = controller.terminal_for_receipt(
                claim["ticket"], claim["receipt"],
            )
            exported.add(terminal["run_id"])
            calls.append(("passport", "export"))

        controller.passport = export
        remote = CONTROL.subprocess.CompletedProcess(
            [], 0, f"{head}\trefs/heads/{claim['branch']}\n", ""
        )
        with patch.object(CONTROL.subprocess, "run", return_value=remote):
            controller.recover_repaired_failures([claim])
        self.assertEqual(claim["status"], "claimed")
        self.assertEqual(claim["receipt"], "")
        self.assertEqual(claim["role"], "")
        self.assertEqual(claim["lease"], "e" * 64)
        self.assertEqual(
            [call[0] for call in calls],
            [
                "passport", "passport", "renew", "claim",
                "ticket_lease_recovered",
                "push_failure_recovered",
            ],
        )

        receipt = "f" * 64
        claim.update(receipt=receipt, role="reviewer", status="blocked")
        (self.product / "factory/runs/interrupted.meta").write_text(
            "run_id=interrupted\n"
            "phase=abandoned\n"
            "ticket=T-110\n"
            "role=reviewer\n"
            "accounting_state=abandoned_conservative\n"
            "task_submitted=0\n"
            "exit_status=143\n"
            "role_exit=\n"
            f"transition_receipt_sha256={receipt}\n",
            encoding="utf-8",
        )
        calls.clear()
        with patch.object(CONTROL.subprocess, "run", return_value=remote):
            controller.recover_repaired_failures([claim])
        self.assertEqual(claim["status"], "claimed")
        self.assertEqual(claim["receipt"], "")
        self.assertEqual(claim["role"], "")
        self.assertEqual(
            [call[0] for call in calls],
            [
                "passport", "passport", "renew", "claim",
                "ticket_lease_recovered",
                "interrupted_role_recovered",
            ],
        )
        claim.update(receipt=receipt, role="reviewer", status="blocked")
        calls.clear()
        (self.product / "factory/runs/interrupted.meta").write_text(
            (self.product / "factory/runs/interrupted.meta")
            .read_text(encoding="utf-8")
            .replace("task_submitted=0", "task_submitted=1"),
            encoding="utf-8",
        )
        controller.recover_repaired_failures([claim])
        self.assertEqual(claim["status"], "blocked")
        self.assertEqual(calls, [])

    def test_push_failure_migrates_authorized_rewrite_before_reclaim(self) -> None:
        controller = CONTROL.Controller(self.args)
        cell = self.root / "cell-rewrite"
        cell.mkdir()
        receipt = "b" * 64
        old_digest = "c" * 64
        new_digest = "d" * 64
        head = "e" * 40
        claim = {
            "branch": "ticket/T-110",
            "lease": "a" * 64,
            "priority": "normal",
            "publication_lease": "",
            "receipt": receipt,
            "role": "test-author",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "blocked",
            "ticket": "T-110",
            "worktree": str(cell),
        }
        controller.save_claim(claim)
        (self.state / "passports").mkdir(mode=0o700)
        passport_path = self.state / "passports/T-110.json"
        CONTROL.write(
            passport_path,
            {
                "branch": claim["branch"],
                "head_sha": "f" * 40,
                "passport_sha256": old_digest,
                "publication_state": "none",
            },
        )
        (self.product / "factory/runs/failed-rewrite.meta").write_text(
            "run_id=failed-rewrite\n"
            "ticket=T-110\n"
            "role=test-author\n"
            "accounting_state=abandoned_conservative\n"
            "exit_status=11\n"
            "role_exit=role_exit_push_failed\n"
            f"transition_receipt_sha256={receipt}\n",
            encoding="utf-8",
        )
        calls = []
        migrated = False
        exported = False

        def json_call(*args, **_kwargs):
            nonlocal migrated
            calls.append(args)
            if args[:2] == ("passport", "validate"):
                return {"passport": new_digest, "status": "ok"}
            if args[:2] == ("passport", "migrate"):
                migrated = True
                CONTROL.write(
                    passport_path,
                    {
                        "branch": claim["branch"],
                        "head_sha": head,
                        "passport_sha256": new_digest,
                        "publication_state": "none",
                    },
                )
            if args[0] == "renew":
                raise CONTROL.ControllerError("failed run released its lease")
            if args[0] == "claim":
                return {
                    "lease_id": "f" * 64,
                    "schema_version": 1,
                    "ticket": "T-110",
                }
            return {}

        controller.json_call = json_call
        controller.event = lambda name, *_args, **_kwargs: calls.append((name,))
        controller.terminal_already_exported = lambda *_args: exported

        def export(_claim, _state):
            nonlocal exported
            calls.append(("passport", "export"))
            if not migrated:
                raise CONTROL.ControllerError("passport head is stale")
            exported = True

        controller.passport = export
        remote = CONTROL.subprocess.CompletedProcess(
            [], 0, f"{head}\trefs/heads/{claim['branch']}\n", ""
        )
        with patch.object(CONTROL.subprocess, "run", return_value=remote):
            controller.recover_repaired_failures([claim])
        self.assertEqual(claim["status"], "claimed")
        self.assertEqual(claim["lease"], "f" * 64)
        self.assertEqual(
            [call[:2] for call in calls],
            [
                ("passport", "export"),
                ("passport", "migrate"),
                ("passport", "export"),
                ("passport", "validate"),
                ("renew", "--ticket"),
                ("claim", "--ticket"),
                ("ticket_lease_recovered",),
                ("push_failure_recovered",),
            ],
        )

    def test_submission_failure_retries_only_after_release_upgrade(self) -> None:
        controller = CONTROL.Controller(self.args)
        cell = self.root / "cell-submission"
        cell.mkdir()
        receipt = "b" * 64
        run_id = "submission-unconfirmed"
        claim = {
            "branch": "ticket/T-110",
            "lease": "c" * 64,
            "priority": "normal",
            "publication_lease": "",
            "receipt": receipt,
            "role": "test-author",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "blocked",
            "ticket": "T-110",
            "worktree": str(cell),
        }
        controller.save_claim(claim)
        passports = self.state / "passports"
        passports.mkdir(mode=0o700)
        passport = {
            "charge_records": [{
                "role": "test-author",
                "run_id": run_id,
                "transition_receipt_sha256": receipt,
            }],
            "completed_role_evidence": [],
            "transition_receipt_sha256": receipt,
        }
        CONTROL.write(passports / "T-110.json", passport)
        manifest = self.product / f"factory/runs/{run_id}.meta"

        def write_manifest(
            kit_sha: str, *, reason: str = "", output: str | None = None
        ) -> None:
            output = output or hashlib.sha256(b"").hexdigest()
            manifest.write_text(
                f"run_id={run_id}\n"
                "phase=completed\n"
                "ticket=T-110\n"
                "role=test-author\n"
                "accounting_state=abandoned_conservative\n"
                "reserved_usd=10.00\n"
                "go_issued=1\n"
                "task_submitted=0\n"
                "turns=0\n"
                "effective_cost=10.00\n"
                "exit_status=125\n"
                "cost_basis=conservative_reservation\n"
                f"kit_sha={kit_sha}\n"
                "role_exit=provider_failed\n"
                f"terminal_reason_code={reason}\n"
                f"output_sha256={output}\n"
                "progress_events=\n"
                f"transition_receipt_sha256={receipt}\n",
                encoding="utf-8",
            )

        events = []
        controller.restore_recorded_contract_repair = lambda _claim: False
        controller.restore_contract_blocker = lambda _claim: False
        controller.remote_passport_valid = lambda _claim: True
        controller.ensure_lease = lambda _claim, _label: None
        controller.event = (
            lambda name, *_args, **details: events.append((name, details))
        )

        write_manifest(self.release.name)
        controller.recover_repaired_failures([claim])
        self.assertEqual(claim["status"], "blocked")
        self.assertEqual(events, [])

        write_manifest(
            "e" * 40,
            reason="adapter_submission_unconfirmed",
            output=hashlib.sha256(b"wrapper diagnostic\n").hexdigest(),
        )
        controller.recover_repaired_failures([claim])
        self.assertEqual(claim["status"], "claimed")
        self.assertEqual(
            events[0][0],
            "submission_failure_recovered_by_release_upgrade",
        )

        write_manifest("e" * 40)
        controller.recover_repaired_failures([claim])
        self.assertEqual(claim["status"], "claimed")
        self.assertEqual(claim["receipt"], "")
        self.assertEqual(claim["role"], "")
        self.assertEqual(
            events,
            [(
                "submission_failure_recovered_by_release_upgrade",
                {"failed_run_id": run_id},
            )],
        )
        self.assertEqual(
            CONTROL.read(passports / "T-110.json")["charge_records"],
            passport["charge_records"],
        )

        claim.update(receipt=receipt, role="test-author", status="blocked")
        events.clear()
        write_manifest("e" * 40, reason="unrelated_failure")
        controller.recover_repaired_failures([claim])
        self.assertEqual(claim["status"], "blocked")
        self.assertEqual(events, [])

    def test_history_rewrite_retries_only_after_release_upgrade(self) -> None:
        controller = CONTROL.Controller(self.args)
        cell = self.root / "cell-history-rewrite"
        cell.mkdir()
        receipt = "b" * 64
        input_head = "c" * 40
        run_id = "history-rewritten"
        claim = {
            "branch": "ticket/T-110",
            "lease": "d" * 64,
            "priority": "normal",
            "publication_lease": "",
            "receipt": receipt,
            "role": "builder",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "blocked",
            "ticket": "T-110",
            "worktree": str(cell),
        }
        controller.save_claim(claim)
        passports = self.state / "passports"
        passports.mkdir(mode=0o700)
        CONTROL.write(
            passports / "T-110.json",
            {
                "branch": claim["branch"],
                "charge_records": [{
                    "role": "builder",
                    "run_id": run_id,
                    "transition_receipt_sha256": receipt,
                }],
                "completed_role_evidence": [],
                "head_sha": input_head,
                "transition_receipt_sha256": receipt,
            },
        )
        manifest = self.product / f"factory/runs/{run_id}.meta"

        def write_manifest(kit_sha: str) -> None:
            manifest.write_text(
                f"run_id={run_id}\n"
                "phase=completed\n"
                "ticket=T-110\n"
                "role=builder\n"
                "accounting_state=abandoned_conservative\n"
                "reserved_usd=10.00\n"
                "go_issued=1\n"
                "task_submitted=1\n"
                "effective_cost=10.00\n"
                "exit_status=11\n"
                "cost_basis=conservative_reservation\n"
                f"kit_sha={kit_sha}\n"
                "role_exit=role_exit_history_rewritten\n"
                f"role_head_before={input_head}\n"
                f"transition_receipt_sha256={receipt}\n",
                encoding="utf-8",
            )

        events = []
        leases = []
        controller.restore_recorded_contract_repair = lambda _claim: False
        controller.restore_contract_blocker = lambda _claim: False
        controller.remote_passport_valid = lambda _claim: True
        controller.ensure_lease = lambda _claim, label: leases.append(label)
        controller.event = (
            lambda name, *_args, **details: events.append((name, details))
        )

        write_manifest(self.release.name)
        controller.recover_repaired_failures([claim])
        self.assertEqual(claim["status"], "blocked")
        self.assertEqual(events, [])
        self.assertEqual(leases, [])

        predecessor = "e" * 40
        if predecessor == self.release.name:
            predecessor = "f" * 40
        write_manifest(predecessor)
        controller.recover_repaired_failures([claim])
        self.assertEqual(claim["status"], "claimed")
        self.assertEqual(claim["receipt"], "")
        self.assertEqual(claim["role"], "")
        self.assertEqual(leases, ["repaired-role"])
        self.assertEqual(
            events,
            [(
                "history_rewrite_recovered_by_release_upgrade",
                {"failed_run_id": run_id},
            )],
        )

    def test_exact_refresh_topology_refusal_runs_attested_refresh(self) -> None:
        controller = CONTROL.Controller(self.args)
        cell = self.root / "cell-1"
        route = cell / "factory/route-plans/T-110.json"
        route.parent.mkdir(parents=True)
        route.write_text("{}\n", encoding="utf-8")
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
        receipt = "b" * 64
        calls = []
        controller.renew = lambda *_args: calls.append("renew")
        controller.finish_pending_run = lambda *_args: True
        stage = ""

        def json_call(*args, **_kwargs):
            calls.append(args)
            if args[0] == "state-machine":
                return state_transition(stage, receipt)
            if args[:2] == ("publication", "withdraw"):
                return {"status": "absent"}
            if args[0] == "ticket-attest":
                return {"action": "refresh", "head": "c" * 40}
            raise AssertionError(args)

        controller.json_call = json_call
        controller.migrate_passport = lambda *_args: calls.append("passport")
        controller.event = lambda name, *_args, **_kwargs: calls.append(name)
        for stage in (
            "REFUSE refresh receipt was not committed directly after its merge",
            "REFUSE stale refresh receipt does not bind this branch history",
        ):
            with self.subTest(stage=stage):
                calls.clear()
                self.assertEqual(
                    controller.reconcile_ticket(claim),
                    {"status": "progressed", "ticket": "T-110"},
                )
                self.assertEqual(
                    calls,
                    [
                        "renew",
                        (
                            "state-machine", "--ticket", "T-110", "--lease",
                            "a" * 64, "--workdir", str(cell), "--json",
                        ),
                        (
                            "publication", "withdraw", "--ticket", "T-110",
                            "--json",
                        ),
                        (
                            "ticket-attest", "--ticket", "T-110", "--lease",
                            "a" * 64, "--receipt", receipt, "--workdir", str(cell),
                            "--action", "refresh", "--json",
                        ),
                        "passport",
                        "refresh_topology_repaired",
                    ],
                )

    def test_narrator_retry_exhaustion_blocks_as_typed_escalation(self) -> None:
        controller = CONTROL.Controller(self.args)
        cell = self.root / "cell-1"
        route = cell / "factory/route-plans/T-110.json"
        route.parent.mkdir(parents=True)
        route.write_text("{}\n", encoding="utf-8")
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
        stage = (
            "ESCALATE evidence bundle remained invalid after one Narrator retry"
        )
        calls = []
        controller.ensure_lease = lambda *_args: calls.append("renew")
        controller.finish_pending_run = lambda *_args: True
        controller.refresh_dependency_tracking = lambda *_args: True

        def json_call(*args, **_kwargs):
            calls.append(args)
            if args[0] == "state-machine":
                return state_transition(stage)
            if args[:2] == ("publication", "withdraw"):
                return {"status": "absent"}
            if args[0] == "release":
                return {"status": "released"}
            raise AssertionError(args)

        events = []
        controller.json_call = json_call
        controller.event = (
            lambda name, *_args, **details: events.append((name, details))
        )
        self.assertEqual(
            controller.reconcile_ticket(claim),
            {"status": "blocked", "ticket": "T-110"},
        )
        self.assertEqual(claim["status"], "blocked")
        self.assertTrue(claim["lease_released"])
        self.assertIn(
            ("ticket_blocked", {"reason": "state-machine-escalation"}), events,
        )
        self.assertIn(
            (
                "state_machine_escalated",
                {
                    "detail": (
                        "evidence bundle remained invalid after one Narrator retry"
                    )
                },
            ),
            events,
        )

    def test_budget_wait_reopens_after_envelope_or_override_change(self) -> None:
        controller = CONTROL.Controller(self.args)
        leases = iter(("b" * 64, "c" * 64))
        controller.json_call = lambda *_args, **_kwargs: {
            "lease_id": next(leases),
            "schema_version": 1,
            "ticket": "T-110",
        }
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
        overrides = self.product / "factory/envelope-overrides"
        overrides.mkdir()
        (overrides / "a.json").write_text("{}\n", encoding="utf-8")
        reopened = controller.load_claims()
        self.assertEqual(reopened[0]["status"], "claimed")
        self.assertEqual(reopened[0]["lease"], "b" * 64)

        claim.update(
            budget_sha256=controller.envelope_digest(),
            status="budget",
        )
        controller.save_claim(claim)
        (self.product / "factory/ENVELOPE.env").write_text(
            "PER_TICKET_BUDGET_USD=30.000000\n", encoding="utf-8"
        )
        reopened = controller.load_claims()
        self.assertEqual(reopened[0]["status"], "claimed")
        self.assertEqual(reopened[0]["lease"], "c" * 64)

    def test_failed_admission_does_not_block_existing_claims(self) -> None:
        controller = CONTROL.Controller(self.args)
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
            "worktree": str(self.root / "cell-1"),
        }
        Path(claim["worktree"]).mkdir()
        controller.save_claim(claim)
        events = []
        controller.claim_new = lambda _claims: (
            (_ for _ in ()).throw(CONTROL.ControllerError("unsafe admission"))
        )
        controller.pin_routes = lambda _claims: []
        controller.reconcile_ticket = lambda item: {
            "status": "active", "ticket": item["ticket"],
        }
        controller.event = lambda name, **fields: events.append((name, fields))

        result = controller.reconcile()

        self.assertEqual(result["active"], 1)
        self.assertEqual(result["results"], [
            {"status": "active", "ticket": "T-110"},
        ])
        self.assertIn(("admission_blocked", {"error": "unsafe admission"}), events)

    def test_progressed_ticket_advances_while_sibling_is_still_active(self) -> None:
        controller = CONTROL.Controller(self.args)
        claims = []
        for number, ticket in enumerate(("T-110", "T-111"), 1):
            cell = self.root / f"cell-{number}"
            cell.mkdir()
            claims.append({
                "branch": f"ticket/{ticket}",
                "lease": f"{number:064x}",
                "priority": "normal",
                "publication_lease": "",
                "receipt": "",
                "role": "",
                "schema": CONTROL.CLAIM_SCHEMA,
                "status": "claimed",
                "ticket": ticket,
                "worktree": str(cell),
            })
        controller.load_claims = lambda: claims
        controller.recover_missing_passport_claims = lambda _claims: None
        controller.recover_upgraded_claims = lambda _claims: None
        controller.recover_repaired_failures = lambda _claims: None
        controller.claim_new = lambda current: current
        controller.pin_routes = lambda _claims: []
        controller.event = lambda *_args, **_kwargs: None
        advanced = __import__("threading").Event()
        calls = {"T-110": 0, "T-111": 0}

        def reconcile(claim):
            ticket = claim["ticket"]
            calls[ticket] += 1
            if ticket == "T-110" and calls[ticket] == 1:
                return {"status": "progressed", "ticket": ticket}
            if ticket == "T-110":
                advanced.set()
                return {"status": "waiting", "ticket": ticket}
            self.assertTrue(advanced.wait(1), "sibling checkpoint did not advance")
            return {"status": "waiting", "ticket": ticket}

        controller.reconcile_ticket = reconcile
        result = controller.reconcile()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(calls, {"T-110": 2, "T-111": 1})

    def test_scheduler_tracks_each_concurrent_ticket_once(self) -> None:
        import threading

        controller = CONTROL.Controller(self.args)
        claims = []
        for number, ticket in enumerate(("T-110", "T-111", "T-112"), 1):
            cell = self.root / f"cell-{number}"
            route = cell / f"factory/route-plans/{ticket}.json"
            route.parent.mkdir(parents=True)
            route.write_text("{}\n", encoding="utf-8")
            claims.append({
                "branch": f"ticket/{ticket}",
                "lease": f"{number:064x}",
                "priority": "normal",
                "publication_lease": "",
                "receipt": "",
                "role": "",
                "schema": CONTROL.CLAIM_SCHEMA,
                "status": "claimed",
                "ticket": ticket,
                "worktree": str(cell),
            })
        controller.load_claims = lambda: claims
        controller.recover_missing_passport_claims = lambda _claims: None
        controller.recover_upgraded_claims = lambda _claims: None
        controller.recover_terminal_exports = lambda _claims: None
        controller.recover_repaired_failures = lambda _claims: None
        controller.claim_new = lambda current: current
        controller.pin_routes = lambda _claims: []
        controller.event = lambda *_args, **_kwargs: None
        barrier = threading.Barrier(3)
        lock = threading.Lock()
        live = 0
        peak = 0
        calls = {claim["ticket"]: 0 for claim in claims}

        def reconcile(claim):
            nonlocal live, peak
            with lock:
                calls[claim["ticket"]] += 1
                live += 1
                peak = max(peak, live)
            barrier.wait(timeout=2)
            with lock:
                live -= 1
            return {"status": "waiting", "ticket": claim["ticket"]}

        controller.reconcile_ticket = reconcile
        result = controller.reconcile()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(calls, {"T-110": 1, "T-111": 1, "T-112": 1})
        self.assertEqual(peak, 3)

    def test_waiting_ticket_stays_settled_while_sibling_worker_is_live(
        self,
    ) -> None:
        import threading
        import time

        controller = CONTROL.Controller(self.args)
        claims = []
        for number, ticket in enumerate(("T-110", "T-111"), 1):
            cell = self.root / f"cell-{number}"
            route = cell / f"factory/route-plans/{ticket}.json"
            route.parent.mkdir(parents=True)
            route.write_text("{}\n", encoding="utf-8")
            claims.append({
                "branch": f"ticket/{ticket}",
                "lease": f"{number:064x}",
                "priority": "normal",
                "publication_lease": "",
                "receipt": "",
                "role": "",
                "schema": CONTROL.CLAIM_SCHEMA,
                "status": "claimed",
                "ticket": ticket,
                "worktree": str(cell),
            })
        controller.load_claims = lambda: claims
        controller.recover_missing_passport_claims = lambda _claims: None
        controller.recover_upgraded_claims = lambda _claims: None
        controller.recover_terminal_exports = lambda _claims: None
        controller.recover_repaired_failures = lambda _claims: None
        controller.claim_new = lambda current: current
        controller.pin_routes = lambda _claims: []
        controller.event = lambda *_args, **_kwargs: None
        first_waited = threading.Event()
        calls = {"T-110": 0, "T-111": 0}

        def reconcile(claim):
            ticket = claim["ticket"]
            calls[ticket] += 1
            if ticket == "T-110":
                first_waited.set()
            else:
                self.assertTrue(first_waited.wait(1))
                time.sleep(0.1)
            return {"status": "waiting", "ticket": ticket}

        controller.reconcile_ticket = reconcile
        with patch.object(CONTROL, "RECONCILE_INTERVAL_SECONDS", 0.02):
            result = controller.reconcile()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(calls, {"T-110": 1, "T-111": 1})

    def test_scheduler_wakes_new_ticket_while_provider_future_is_live(self) -> None:
        import threading

        controller = CONTROL.Controller(self.args)
        claims = []
        for number, ticket in enumerate(("T-110", "T-111"), 1):
            cell = self.root / f"cell-{number}"
            route = cell / f"factory/route-plans/{ticket}.json"
            route.parent.mkdir(parents=True)
            route.write_text("{}\n", encoding="utf-8")
            claims.append({
                "branch": f"ticket/{ticket}",
                "lease": f"{number:064x}",
                "priority": "normal",
                "publication_lease": "",
                "receipt": "",
                "role": "",
                "schema": CONTROL.CLAIM_SCHEMA,
                "status": "claimed",
                "ticket": ticket,
                "worktree": str(cell),
            })
        first_started = threading.Event()
        expose_second = threading.Event()
        second_started = threading.Event()
        release_first = threading.Event()
        calls = {"T-110": 0, "T-111": 0}

        def load_claims():
            return claims if expose_second.is_set() else claims[:1]

        controller.load_claims = load_claims
        controller.recover_missing_passport_claims = lambda _claims: None
        controller.recover_upgraded_claims = lambda _claims: None
        controller.recover_terminal_exports = lambda _claims: None
        controller.recover_repaired_failures = lambda _claims: None
        controller.claim_new = lambda current: current
        controller.pin_routes = lambda _claims: []
        controller.event = lambda *_args, **_kwargs: None

        def reconcile(claim):
            ticket = claim["ticket"]
            calls[ticket] += 1
            if ticket == "T-110":
                first_started.set()
                self.assertTrue(release_first.wait(2))
            else:
                self.assertTrue(first_started.is_set())
                second_started.set()
                release_first.set()
            return {"status": "waiting", "ticket": ticket}

        controller.reconcile_ticket = reconcile

        def expose() -> None:
            self.assertTrue(first_started.wait(1))
            expose_second.set()

        wake = threading.Thread(target=expose)
        wake.start()
        with patch.object(CONTROL, "RECONCILE_INTERVAL_SECONDS", 0.02):
            result = controller.reconcile()
        wake.join(timeout=1)
        self.assertTrue(second_started.is_set())
        self.assertEqual(calls, {"T-110": 1, "T-111": 1})
        self.assertEqual(result["status"], "ok")

    def test_restart_does_not_resubmit_externally_active_role(self) -> None:
        controller = CONTROL.Controller(self.args)
        claims = []
        for number, ticket in enumerate(("T-110", "T-111"), 1):
            cell = self.root / f"cell-{number}"
            route = cell / f"factory/route-plans/{ticket}.json"
            route.parent.mkdir(parents=True)
            route.write_text("{}\n", encoding="utf-8")
            claims.append({
                "branch": f"ticket/{ticket}",
                "lease": f"{number:064x}",
                "priority": "normal",
                "publication_lease": "",
                "receipt": "a" * 64 if ticket == "T-110" else "",
                "role": "builder" if ticket == "T-110" else "",
                "schema": CONTROL.CLAIM_SCHEMA,
                "status": "running" if ticket == "T-110" else "claimed",
                "ticket": ticket,
                "worktree": str(cell),
            })
        controller.load_claims = lambda: claims
        controller.recover_missing_passport_claims = lambda _claims: None
        controller.recover_upgraded_claims = lambda _claims: None
        controller.recover_terminal_exports = lambda _claims: None
        controller.recover_repaired_failures = lambda _claims: None
        controller.claim_new = lambda current: current
        controller.pin_routes = lambda _claims: []
        controller.event = lambda *_args, **_kwargs: None
        controller.role_active = lambda claim: claim["ticket"] == "T-110"
        called = []
        controller.reconcile_ticket = lambda claim: (
            called.append(claim["ticket"])
            or {"status": "waiting", "ticket": claim["ticket"]}
        )
        controller.reconcile()
        self.assertEqual(called, ["T-111"])

    def test_restart_does_not_reattach_parked_ticket_when_cells_are_full(self) -> None:
        controller = CONTROL.Controller(self.args)
        controller.capacity = 3
        claims = []
        for number, ticket in enumerate(("T-110", "T-111", "T-112"), 1):
            claims.append({
                "branch": f"ticket/{ticket}",
                "lease": f"{number:064x}",
                "priority": "normal",
                "publication_lease": "",
                "receipt": "a" * 64,
                "role": "builder",
                "schema": CONTROL.CLAIM_SCHEMA,
                "status": "running",
                "ticket": ticket,
                "worktree": str(self.root / f"cell-{number}"),
            })
        parked = {
            "branch": "ticket/T-113",
            "lease": "",
            "parked": True,
            "priority": "normal",
            "publication_lease": "",
            "receipt": "",
            "role": "",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "waiting",
            "ticket": "T-113",
            "worktree": str(self.root / "parked/T-113"),
        }
        claims.append(parked)
        route = Path(parked["worktree"]) / "factory/route-plans/T-113.json"
        route.parent.mkdir(parents=True)
        route.write_text("{}\n", encoding="utf-8")
        controller.load_claims = lambda: claims
        controller.recover_missing_passport_claims = lambda _claims: None
        controller.recover_upgraded_claims = lambda _claims: None
        controller.recover_terminal_exports = lambda _claims: None
        controller.recover_repaired_failures = lambda _claims: None
        controller.claim_new = lambda current: current
        controller.pin_routes = lambda _claims: []
        controller.event = lambda *_args, **_kwargs: None
        controller.role_active = lambda claim: claim["ticket"] != "T-113"
        called = []
        controller.reconcile_ticket = lambda claim: called.append(claim["ticket"])

        result = controller.reconcile()

        self.assertEqual(result["status"], "ok")
        self.assertEqual(called, [])
        self.assertEqual(parked["status"], "waiting")
        self.assertTrue(parked["parked"])

    def test_clean_checkpoint_parks_frees_capacity_and_reattaches(self) -> None:
        import subprocess

        run = lambda *command, cwd=None: subprocess.run(
            command, cwd=cwd, text=True, capture_output=True, check=True
        )
        run("git", "init", "-q", "-b", "main", cwd=self.product)
        run("git", "config", "user.name", "Test", cwd=self.product)
        run("git", "config", "user.email", "test@example.invalid", cwd=self.product)
        run("git", "add", ".", cwd=self.product)
        run("git", "commit", "-qm", "seed", cwd=self.product)
        run("git", "branch", "ticket/T-110", cwd=self.product)
        cells = self.root / "cells"
        cells.mkdir(mode=0o700)
        cell = cells / "cell-1"
        run(
            "git", "worktree", "add", "-q", str(cell), "ticket/T-110",
            cwd=self.product,
        )
        controller = CONTROL.Controller(self.args)
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
        controller.save_claim(claim)
        controller.remote_passport_valid = lambda _claim: True
        calls = []

        def json_call(*arguments, **_kwargs):
            calls.append(arguments)
            if arguments[0] == "release":
                return {}
            if arguments[0] == "claim":
                return {
                    "lease_id": "b" * 64,
                    "schema_version": 1,
                    "ticket": "T-110",
                }
            raise AssertionError(arguments)

        controller.json_call = json_call
        controller.event = lambda *_args, **_kwargs: None
        self.assertTrue(controller.park_claim(claim))
        self.assertEqual(claim["status"], "waiting")
        self.assertEqual(claim["lease"], "")
        self.assertTrue(claim["parked"])
        self.assertFalse(CONTROL.Controller.consumes_capacity(claim))
        self.assertFalse(cell.exists())
        self.assertTrue((cells / "parked/T-110").is_dir())

        admissions = iter(({
            "action": "START",
            "branch": "ticket/T-111",
            "lease_id": "c" * 64,
            "priority": "normal",
            "ticket": "T-111",
            "worktree": str(cell),
        }, {"action": "WAIT"}))
        controller.json_call = lambda *_args, **_kwargs: next(admissions)
        admitted = controller.claim_new([claim])
        self.assertIn("T-111", {item["ticket"] for item in admitted})

        controller.json_call = json_call
        controller.ensure_lease(claim, "paid-role")
        controller.ensure_execution_cell(claim)
        self.assertNotIn("parked", claim)
        self.assertEqual(claim["lease"], "b" * 64)
        self.assertEqual(claim["worktree"], str(cell))
        self.assertTrue(cell.is_dir())

    def test_approval_attestation_precedes_h2_merge_lease_and_keeps_it(self) -> None:
        controller = CONTROL.Controller(self.args)
        cell = self.root / "cell-1"
        route = cell / "factory/route-plans/T-110.json"
        route.parent.mkdir(parents=True)
        route.write_text("{}\n", encoding="utf-8")
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
        stages = iter((
            state_transition(
                "AWAIT-OPERATOR Linear approval observed", "b" * 64
            ),
            state_transition(
                "AWAIT-MERGE approval attested; "
                "protected auto-merge request pending",
                "c" * 64,
            ),
        ))
        heads = iter(("d" * 40, "e" * 40))
        calls = []
        controller.renew = lambda _claim: None
        controller.finish_pending_run = lambda _claim: True
        controller.refresh_dependency_tracking = lambda _claim: True
        controller.migrate_passport = lambda *_args: calls.append("passport")
        controller.event = lambda *_args, **_kwargs: None
        controller.withdraw_publication = lambda *_args: None

        def json_call(*arguments, **_kwargs):
            if arguments[0] == "state-machine":
                return next(stages)
            if arguments[0] == "ticket-pr":
                return {
                    "head": next(heads), "pr_number": 24, "status": "ready",
                }
            if arguments[0] == "ticket-attest":
                calls.append(arguments)
                return (
                    {
                        "action": "approval-attested",
                        "auto_merge": False,
                        "head": "e" * 40,
                        "pr_number": 24,
                    }
                    if "--attest-only" in arguments
                    else {
                        "action": "approval",
                        "auto_merge": True,
                        "head": "e" * 40,
                        "pr_number": 24,
                    }
                )
            raise AssertionError(arguments)

        controller.json_call = json_call
        controller.publication_ready = lambda item, _receipt, head: (
            calls.append(("publication", head)),
            item.update(publication_lease="f" * 64),
            True,
        )[-1]
        self.assertEqual(
            controller.reconcile_ticket(claim)["status"], "progressed"
        )
        self.assertIn("--attest-only", calls[0])
        self.assertEqual(
            controller.reconcile_ticket(claim)["status"], "progressed"
        )
        self.assertEqual(calls[2], ("publication", "e" * 40))
        self.assertNotIn("--attest-only", calls[3])
        self.assertEqual(claim["publication_lease"], "f" * 64)

    def test_projected_approval_stage_still_requests_exact_auto_merge(self) -> None:
        controller = CONTROL.Controller(self.args)
        cell = self.root / "cell-1"
        route = cell / "factory/route-plans/T-110.json"
        route.parent.mkdir(parents=True)
        route.write_text("{}\n", encoding="utf-8")
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
        receipt = "b" * 64
        head = "d" * 40
        calls = []
        controller.renew = lambda _claim: None
        controller.finish_pending_run = lambda _claim: True
        controller.refresh_dependency_tracking = lambda _claim: True
        controller.ticket_merged = lambda _claim: False
        controller.withdraw_publication = lambda *_args: None
        controller.publication_ready = lambda item, _receipt, exact_head: (
            calls.append(("publication", exact_head)),
            item.update(publication_lease="f" * 64),
            True,
        )[-1]
        controller.migrate_passport = lambda *_args: calls.append("passport")
        controller.event = lambda name, *_args, **_kwargs: calls.append(name)

        def json_call(*arguments, **_kwargs):
            if arguments[0] == "state-machine":
                return state_transition(
                    "AWAIT-MERGE protected auto-merge requested; "
                    "await merge and closeout",
                    receipt,
                )
            if arguments[0] == "ticket-pr":
                return {
                    "head": head, "pr_number": 24, "status": "ready",
                }
            if arguments[0] == "ticket-attest":
                calls.append(arguments)
                return {
                    "action": "approval", "auto_merge": True,
                    "head": head, "pr_number": 24,
                }
            raise AssertionError(arguments)

        controller.json_call = json_call
        self.assertEqual(
            controller.reconcile_ticket(claim),
            {"status": "waiting", "ticket": "T-110"},
        )
        self.assertEqual(calls[0], ("publication", head))
        self.assertEqual(calls[1][0], "ticket-attest")
        self.assertIn("passport", calls)
        self.assertIn("protected_auto_merge_requested", calls)
        self.assertEqual(claim["publication_lease"], "f" * 64)

    def test_launcher_authorizes_requested_stage_publication_recovery(self) -> None:
        launcher = (
            ROOT / "integrations/hermes/bin/factory-launch"
        ).read_text(encoding="utf-8")
        phase_two = launcher.index(
            'die "transition receipt does not authorize protected auto-merge"'
        )
        guard = launcher[phase_two - 500:phase_two]
        self.assertIn(
            '"$TRANSITION_STAGE" == AWAIT-MERGE\\ approval\\ attested*',
            guard,
        )
        self.assertIn(
            '"AWAIT-MERGE protected auto-merge requested; '
            'await merge and closeout"',
            guard,
        )

    def test_dependency_refresh_race_waits_then_migrates_exact_base(self) -> None:
        controller = CONTROL.Controller(self.args)
        cell = self.root / "cell-1"
        route = cell / "factory/route-plans/T-110.json"
        route.parent.mkdir(parents=True)
        route.write_text("{}\n", encoding="utf-8")
        (cell / "factory/tickets").mkdir()
        (cell / "factory/tickets/T-110.md").write_text(
            "# T-110\n\nState: Building\nDepends-On: T-094\n",
            encoding="utf-8",
        )
        old = "d" * 40
        protected = "e" * 40
        refreshed = "f" * 40
        receipt = "b" * 64
        stage = (
            "REFUSE dependency refresh required; "
            f"dependencies=T-094; protected-main={protected}"
        )
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
        CONTROL.write(self.state / "T-110.json", {
            "head_sha": old,
            "receipt_sha256": receipt,
        })
        results = iter((
            {
                "action": "dependency-wait",
                "expected_protected_head": protected,
                "observed_protected_head": "1" * 40,
            },
            {
                "action": "dependency-refresh",
                "attestation": {
                    "old_head": old,
                    "protected_head": protected,
                },
                "head": refreshed,
            },
        ))
        migrations = []
        controller.renew = lambda _claim: None
        controller.finish_pending_run = lambda _claim: True
        controller.refresh_dependency_tracking = lambda _claim: True
        controller.withdraw_publication = lambda _claim: None
        controller.migrate_passport = lambda *_args: migrations.append("passport")
        controller.event = lambda *_args, **_kwargs: None

        def json_call(*arguments, **_kwargs):
            if arguments[0] == "state-machine":
                return state_transition(stage, receipt)
            if arguments[0] == "ticket-attest":
                self.assertIn("dependency-refresh", arguments)
                return next(results)
            raise AssertionError(arguments)

        controller.json_call = json_call

        def run(command, **_kwargs):
            if "rev-parse" in command:
                return CONTROL.subprocess.CompletedProcess(command, 0, old + "\n", "")
            if "merge-base" in command:
                return CONTROL.subprocess.CompletedProcess(command, 0, "", "")
            raise AssertionError(command)

        with patch.object(CONTROL.subprocess, "run", side_effect=run):
            self.assertEqual(
                controller.reconcile_ticket(claim)["status"], "waiting"
            )
            self.assertEqual(migrations, [])
            claim["status"] = "claimed"
            self.assertEqual(
                controller.reconcile_ticket(claim)["status"], "progressed"
            )
        self.assertEqual(migrations, ["passport"])

    def test_dependency_test_conflict_routes_without_generic_block(self) -> None:
        controller = CONTROL.Controller(self.args)
        cell = self.root / "cell-1"
        route = cell / "factory/route-plans/T-110.json"
        route.parent.mkdir(parents=True)
        route.write_text("{}\n", encoding="utf-8")
        (cell / "factory/tickets").mkdir()
        (cell / "factory/tickets/T-110.md").write_text(
            "# T-110\n\nState: Building\nDepends-On: T-094\n",
            encoding="utf-8",
        )
        old = "d" * 40
        protected = "e" * 40
        refreshed = "f" * 40
        receipt = "b" * 64
        stage = (
            "REFUSE dependency refresh required; "
            f"dependencies=T-094; protected-main={protected}"
        )
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
        CONTROL.write(self.state / "T-110.json", {
            "head_sha": old,
            "receipt_sha256": receipt,
        })
        migrations = []
        events = []
        controller.renew = lambda _claim: None
        controller.finish_pending_run = lambda _claim: True
        controller.refresh_dependency_tracking = lambda _claim: True
        controller.withdraw_publication = lambda _claim: None
        controller.migrate_passport = lambda *_args: migrations.append("passport")
        controller.event = lambda name, *_args, **kwargs: events.append(
            (name, kwargs)
        )

        def json_call(*arguments, **_kwargs):
            if arguments[0] == "state-machine":
                return state_transition(stage, receipt)
            if arguments[0] == "ticket-attest":
                return {
                    "action": "dependency-conflict-refresh",
                    "attestation": {
                        "conflicts": [{"path": "tests/conflict.test.ts"}],
                        "old_head": old,
                        "protected_head": protected,
                        "repair_owner": "test-author",
                    },
                    "head": refreshed,
                }
            raise AssertionError(arguments)

        controller.json_call = json_call

        def run(command, **_kwargs):
            if "rev-parse" in command:
                return CONTROL.subprocess.CompletedProcess(
                    command, 0, old + "\n", "",
                )
            if "merge-base" in command:
                return CONTROL.subprocess.CompletedProcess(command, 0, "", "")
            raise AssertionError(command)

        with patch.object(CONTROL.subprocess, "run", side_effect=run):
            self.assertEqual(
                controller.reconcile_ticket(claim)["status"], "progressed"
            )
        self.assertEqual(migrations, ["passport"])
        self.assertEqual(events[-1][0], "dependency_conflict_routed")
        self.assertEqual(events[-1][1]["repair_owner"], "test-author")
        self.assertEqual(
            events[-1][1]["conflict_paths"], ["tests/conflict.test.ts"]
        )
        self.assertEqual(claim["status"], "claimed")

    def test_merged_publication_closes_before_dependency_refresh(self) -> None:
        controller = CONTROL.Controller(self.args)
        claim = {
            "branch": "ticket/T-110",
            "lease": "a" * 64,
            "priority": "normal",
            "publication_lease": "b" * 64,
            "receipt": "c" * 64,
            "role": "",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "claimed",
            "ticket": "T-110",
            "worktree": str(self.root / "cell-1"),
        }
        calls = []
        controller.ensure_lease = lambda *_args: None
        controller.finish_pending_run = lambda _claim: True
        controller.ticket_merged = lambda _claim: True
        controller.release_publication = lambda _claim: calls.append(
            "release"
        )
        controller.migrate_passport = lambda *_args: calls.append("passport")
        controller.closeout = lambda _claim: calls.append("closeout") or True
        controller.refresh_dependency_tracking = lambda _claim: (
            (_ for _ in ()).throw(
                AssertionError("merged ticket refreshed dependencies")
            )
        )
        controller.json_call = lambda *_args, **_kwargs: (
            (_ for _ in ()).throw(
                AssertionError("merged ticket entered state machine")
            )
        )
        self.assertEqual(
            controller.reconcile_ticket(claim),
            {"status": "progressed", "ticket": "T-110"},
        )
        self.assertEqual(calls, ["release", "passport", "closeout"])

    def test_run_wrapper_renews_lease_before_provider_queue(self) -> None:
        source = (ROOT / "scripts/run-agent.sh").read_text(encoding="utf-8")
        required = source.index(
            'if ! factory_dispatch_require_lease "$REPO_ROOT" "$TICKET"'
        )
        heartbeat = source.index("start_lease_heartbeat", required)
        provider = source.index(
            "# --- resolve one backend before reservation", required
        )
        self.assertLess(required, heartbeat)
        self.assertLess(heartbeat, provider)

    def test_missing_claim_recovers_only_from_current_passport_cell(self) -> None:
        controller = CONTROL.Controller(self.args)
        controller.qualification = {"tickets": ["T-110"]}
        cell = self.root / "cell-1"
        cell.mkdir()
        (self.state / "passports").mkdir(mode=0o700)
        CONTROL.write(
            self.state / "passports/T-110.json",
            {
                "branch": "ticket/T-110",
                "current_state": "Review",
                "ticket": "T-110",
            },
        )
        calls = []
        controller.ticket_release_current = lambda _claim: True
        controller.migrate_passport = lambda *_args: calls.append("passport")
        controller.event = lambda name, *_args, **_kwargs: calls.append(name)
        controller.json_call = lambda *args, **_kwargs: (
            {
                "lease_id": "a" * 64,
                "schema_version": 1,
                "ticket": "T-110",
            }
            if args[0] == "claim"
            else {}
        )
        with patch("subprocess.run") as run:
            run.return_value.stdout = (
                f"worktree {cell}\n"
                "HEAD deadbeef\n"
                "branch refs/heads/ticket/T-110\n\n"
            )
            claims = []
            controller.recover_missing_passport_claims(claims)

        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0]["lease"], "a" * 64)
        self.assertEqual(claims[0]["worktree"], str(cell))
        self.assertEqual(calls, ["passport", "missing_claim_recovered"])
        self.assertEqual(
            CONTROL.read(controller.claim_path("T-110"))["status"],
            "claimed",
        )

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

    def test_nonpublication_stage_withdraws_stale_queue_entry(self) -> None:
        controller = CONTROL.Controller(self.args)
        cell = self.root / "cell-1"
        (cell / "factory/route-plans").mkdir(parents=True)
        (cell / "factory/route-plans/T-110.json").write_text("{}\n")
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
        controller.renew = lambda _claim: None
        controller.finish_pending_run = lambda _claim: True

        def json_call(*arguments, **_kwargs):
            calls.append(arguments[:2])
            if arguments[0] == "state-machine":
                return state_transition(
                    "AWAIT-OPERATOR product decision required"
                )
            if arguments[:2] == ("publication", "withdraw"):
                return {"status": "withdrawn"}
            raise AssertionError(arguments)

        controller.json_call = json_call
        controller.event = lambda name, *_args, **_kwargs: calls.append((name,))
        result = controller.reconcile_ticket(claim)

        self.assertEqual(result, {"status": "waiting", "ticket": "T-110"})
        self.assertIn(("publication", "withdraw"), calls)
        self.assertIn(("publication_withdrawn",), calls)

    def test_reconcile_recovers_missing_idle_ticket_lease(self) -> None:
        controller = CONTROL.Controller(self.args)
        cell = self.root / "cell-1"
        route = cell / "factory/route-plans/T-110.json"
        route.parent.mkdir(parents=True)
        route.write_text("{}\n", encoding="utf-8")
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
        controller.save_claim(claim)
        controller.finish_pending_run = lambda _claim: True
        calls = []

        def json_call(*arguments, **_kwargs):
            calls.append(arguments)
            if arguments[0] == "renew":
                raise CONTROL.ControllerError("lease is absent")
            if arguments[0] == "claim":
                return {
                    "lease_id": "b" * 64,
                    "schema_version": 1,
                    "ticket": "T-110",
                }
            if arguments[0] == "state-machine":
                self.assertEqual(arguments[4], "b" * 64)
                return state_transition(
                    "AWAIT-OPERATOR product decision required", "c" * 64
                )
            if arguments[:2] == ("publication", "withdraw"):
                return {"status": "absent"}
            raise AssertionError(arguments)

        controller.json_call = json_call
        controller.event = lambda name, *_args, **_kwargs: calls.append((name,))
        self.assertEqual(
            controller.reconcile_ticket(claim),
            {"status": "waiting", "ticket": "T-110"},
        )
        self.assertEqual(claim["lease"], "b" * 64)
        self.assertEqual(
            CONTROL.read(controller.claim_path("T-110"))["lease"],
            "b" * 64,
        )
        self.assertIn(("ticket_lease_recovered",), calls)

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
        class RecordingLock:
            def __enter__(self):
                calls.append("git-lock")

            def __exit__(self, *_args):
                calls.append("git-unlock")

        controller.git_lock = RecordingLock()
        controller.protected_base_current = lambda *_args: (
            calls.append("base") or False
        )
        controller.release_publication = lambda item: (
            calls.append("release"), item.update(publication_lease="")
        )
        controller.json_call = lambda *_args, **_kwargs: (
            calls.append("refresh") or {
                "action": "refresh", "head": "d" * 40,
            }
        )
        controller.migrate_passport = lambda *_args: calls.append("passport")
        controller.event = lambda *_args, **_kwargs: calls.append("event")
        self.assertFalse(
            controller.publication_ready(claim, "c" * 64, "d" * 40)
        )
        self.assertEqual(
            calls,
            [
                "git-lock", "base", "release", "refresh", "passport", "event",
                "git-unlock",
            ],
        )

    def test_closeout_fetch_uses_shared_git_lock(self) -> None:
        controller = CONTROL.Controller(self.args)
        cell = self.root / "cells/cell-1"
        cell.mkdir(parents=True)
        (cell.parent / "closeout-T-110").mkdir()
        calls = []

        class RecordingLock:
            def __enter__(self):
                calls.append("git-lock")

            def __exit__(self, *_args):
                calls.append("git-unlock")

        controller.git_lock = RecordingLock()
        controller.json_call = lambda *_args, **_kwargs: (
            calls.append("done") or {"closeout_pr_state": "OPEN"}
        )

        def run(command, **_kwargs):
            calls.append("fetch")
            return CONTROL.subprocess.CompletedProcess(command, 0, "", "")

        with patch.object(CONTROL.subprocess, "run", side_effect=run):
            self.assertFalse(controller.closeout({
                "lease": "a" * 64,
                "ticket": "T-110",
                "worktree": str(cell),
            }))
        self.assertEqual(calls, ["git-lock", "fetch", "git-unlock", "done"])

    def test_closeout_waits_for_post_merge_check_propagation(self) -> None:
        controller = CONTROL.Controller(self.args)
        cell = self.root / "cells/cell-1"
        cell.mkdir(parents=True)
        (cell.parent / "closeout-T-110").mkdir()
        events = []
        controller.event = lambda *args, **kwargs: events.append((args, kwargs))
        controller.json_call = lambda *_args, **_kwargs: (
            (_ for _ in ()).throw(CONTROL.ControllerError(
                "ticket-attest: required post-merge check is pending: ci"
            ))
        )
        with patch.object(CONTROL.subprocess, "run"):
            self.assertFalse(controller.closeout({
                "lease": "a" * 64,
                "ticket": "T-110",
                "worktree": str(cell),
            }))
        self.assertEqual(events, [(("post_merge_check_wait", "T-110"), {
            "reason": "required post-merge check is pending: ci",
        })])


if __name__ == "__main__":
    unittest.main()
