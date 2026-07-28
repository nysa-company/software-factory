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
            return {}

        controller.json_call = json_call
        self.assertEqual(
            controller.reconcile_ticket(claim),
            {"status": "complete", "ticket": "T-110"},
        )
        self.assertFalse(controller.claim_path("T-110").exists())

    def test_factory_upgrade_reclaims_only_its_blocked_claim(self) -> None:
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

        def json_call(*args, **_kwargs):
            calls.append(args)
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
        controller.recover_upgraded_claims([claim])
        self.assertEqual(claim["status"], "claimed")
        self.assertEqual(claim["lease"], "c" * 64)
        self.assertEqual(
            [call[0] for call in calls],
            ["renew", "claim", "passport"],
        )

    def test_repaired_push_failure_reclaims_only_exact_remote_passport(self) -> None:
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
            controller.recover_repaired_push_failures([claim])
        self.assertEqual(claim["status"], "claimed")
        self.assertEqual(claim["receipt"], "")
        self.assertEqual(claim["role"], "")
        self.assertEqual(claim["lease"], "e" * 64)
        self.assertEqual(
            [call[0] for call in calls],
            ["passport", "renew", "claim", "push_failure_recovered"],
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
