#!/usr/bin/env python3
"""Focused non-agent controller persistence tests."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "factory_controller", ROOT / "scripts/factory-controller.py"
)
assert SPEC and SPEC.loader
CONTROL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CONTROL)


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

    def test_three_ticket_qualification_parks_an_excluded_claim(self) -> None:
        tickets = [f"T-{number}" for number in range(110, 113)]
        (self.product / "factory/QUALIFICATION.json").write_text(
            json.dumps({
                "budget_usd": "100.000000",
                "capacity": 4,
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
            if args[0] == "state-machine":
                return {
                    "receipt": "f" * 64,
                    "role": "test-author",
                    "stage": "FIX test-author",
                    "status": "ok",
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
                "recorded_contract_repair_recovered",
                {"stage": "FIX test-author"},
            ),
            calls,
        )
        self.assertIn(
            (
                "state-machine", "--ticket", "T-110", "--lease", "e" * 64,
                "--workdir", str(cell), "--json",
            ),
            calls,
        )

    def test_invalid_recorded_repair_releases_new_lease_and_stays_blocked(
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
            return {}

        controller.json_call = json_call
        controller.remote_passport_valid = lambda _claim: True
        controller.event = lambda *_args, **_kwargs: None

        with self.assertRaisesRegex(
            CONTROL.ControllerError, "repair record is invalid"
        ):
            controller.restore_recorded_contract_repair(claim)

        self.assertEqual(claim["status"], "blocked")
        self.assertEqual(claim["lease"], "")
        self.assertNotIn("lease_released", claim)
        self.assertIn(
            ("release", "--ticket", "T-110", "--lease", "e" * 64),
            calls,
        )

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
                return {
                    "receipt": "b" * 64,
                    "role": None,
                    "stage": "COMPLETE attested Done is on protected main",
                }
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
        self.assertEqual(
            [call[0] for call in calls],
            ["passport", "passport", "renew", "claim", "passport"],
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
                "passport", "renew", "claim", "ticket_lease_recovered",
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
                "passport", "renew", "claim", "ticket_lease_recovered",
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
        validations = 0

        def json_call(*args, **_kwargs):
            nonlocal validations
            calls.append(args)
            if args[:2] == ("passport", "validate"):
                validations += 1
                if validations == 1:
                    raise CONTROL.ControllerError("passport head is stale")
                return {"passport": new_digest, "status": "ok"}
            if args[:2] == ("passport", "migrate"):
                CONTROL.write(
                    passport_path,
                    {
                        "branch": claim["branch"],
                        "head_sha": head,
                        "passport_sha256": new_digest,
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
                ("passport", "validate"),
                ("passport", "migrate"),
                ("passport", "validate"),
                ("renew", "--ticket"),
                ("claim", "--ticket"),
                ("ticket_lease_recovered",),
                ("push_failure_recovered",),
            ],
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
                return {
                    "receipt": receipt,
                    "role": None,
                    "stage": stage,
                }
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
            {
                "receipt": "b" * 64,
                "role": None,
                "stage": "AWAIT-OPERATOR Linear approval observed",
            },
            {
                "receipt": "c" * 64,
                "role": None,
                "stage": (
                    "AWAIT-MERGE approval attested; "
                    "protected auto-merge request pending"
                ),
            },
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
                return {"receipt": receipt, "role": None, "stage": stage}
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
                return {
                    "receipt": "b" * 64,
                    "role": None,
                    "stage": "AWAIT-OPERATOR product decision required",
                }
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
                return {
                    "receipt": "c" * 64,
                    "role": None,
                    "stage": "AWAIT-OPERATOR product decision required",
                }
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


if __name__ == "__main__":
    unittest.main()
