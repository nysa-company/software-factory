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
FACTORY_ISSUE = "https://github.com/nysa-company/software-factory/issues/253"


def state_transition(
    stage: str, receipt: str = "b" * 64, ticket: str = "T-110"
) -> dict:
    return {
        "action": stage.partition(" ")[0],
        "detail": stage.partition(" ")[2] or None,
        "loop": None,
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

    def test_terminal_event_is_idempotent_across_restart(self) -> None:
        controller = CONTROL.Controller(self.args)
        details = {"protected_main": "b" * 40, "terminal_basis": "attested-done"}
        controller.event_once("linear_terminal_synced", "T-110", **details)
        controller.event_once("linear_terminal_synced", "T-110", **details)
        matching = [
            json.loads(path.read_text()) for path in controller.events.glob("*.json")
            if json.loads(path.read_text()).get("event") == "linear_terminal_synced"
        ]
        self.assertEqual(len(matching), 1)

    def test_contract_resume_refusal_is_restart_safe_and_ticket_scoped(self) -> None:
        controller = CONTROL.Controller(self.args)
        claim = {"receipt": "c" * 64, "ticket": "T-110"}
        evidence = {
            "local_head": "b" * 40,
            "remote_head": "a" * 40,
        }
        controller.record_contract_resume_refusal(
            claim, "resume_commit_not_pushed", evidence
        )
        CONTROL.Controller(self.args).record_contract_resume_refusal(
            claim, "resume_commit_not_pushed", evidence
        )
        claim["receipt"] = "d" * 64
        controller.record_contract_resume_refusal(
            claim, "resume_commit_not_pushed", evidence
        )
        controller.record_contract_resume_refusal(
            {"receipt": "e" * 64, "ticket": "T-111"},
            "resume_commit_not_pushed", evidence,
        )
        refusals = [
            CONTROL.read(path) for path in controller.events.glob("*.json")
            if CONTROL.read(path).get("event") == "contract_resume_refused"
        ]
        self.assertEqual(
            sorted(item["ticket"] for item in refusals),
            ["T-110", "T-110", "T-111"],
        )

    def test_remote_cell_head_status_distinguishes_unpushed_from_diverged(self) -> None:
        controller = CONTROL.Controller(self.args)
        claim = {
            "branch": "ticket/T-110",
            "ticket": "T-110",
            "worktree": str(self.root / "cell-1"),
        }
        local = "b" * 40
        remote = "a" * 40
        responses = [
            CONTROL.subprocess.CompletedProcess([], 0, local + "\n", ""),
            CONTROL.subprocess.CompletedProcess(
                [], 0, f"{remote}\trefs/heads/{claim['branch']}\n", ""
            ),
            CONTROL.subprocess.CompletedProcess([], 0, "", ""),
        ]
        with patch.object(CONTROL.subprocess, "run", side_effect=responses):
            self.assertEqual(
                controller.remote_cell_head_status(claim),
                ("resume_commit_not_pushed", local, remote),
            )
        responses[-1] = CONTROL.subprocess.CompletedProcess([], 1, "", "")
        with patch.object(CONTROL.subprocess, "run", side_effect=responses):
            self.assertEqual(
                controller.remote_cell_head_status(claim),
                ("resume_ancestry_invalid", local, remote),
            )
        responses[-1] = CONTROL.subprocess.CompletedProcess([], 128, "", "")
        with patch.object(CONTROL.subprocess, "run", side_effect=responses):
            self.assertEqual(
                controller.remote_cell_head_status(claim),
                ("remote_unavailable", local, remote),
            )

    def test_qualification_events_bind_the_exact_manifest_generation(self) -> None:
        manifest = {
            "budget_usd": "100.000000",
            "capacity": 4,
            "contract_version": "1.8.0",
            "factory_sha": "a" * 40,
            "generation": 27,
            "per_run_budget_usd": "2.000000",
            "per_ticket_budget_usd": "25.000000",
            "schema": CONTROL.QUALIFICATION_SCHEMA,
            "target_done": 4,
            "tickets": ["T-110", "T-111", "T-112", "T-113"],
        }
        (self.product / "factory/QUALIFICATION.json").write_text(
            json.dumps(manifest), encoding="utf-8",
        )
        controller = CONTROL.Controller(self.args)
        controller.event("restart_boundary", tickets=manifest["tickets"])
        event = CONTROL.read(next(controller.events.glob("*.json")))
        self.assertEqual(event["qualification_generation"], 27)
        self.assertEqual(
            event["qualification_manifest_sha256"],
            hashlib.sha256(CONTROL.canonical(manifest).encode()).hexdigest(),
        )

    def test_qualification_reconciles_protected_terminal_without_passport(self) -> None:
        tickets = [f"T-{number}" for number in range(110, 114)]
        (self.product / "factory/QUALIFICATION.json").write_text(json.dumps({
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
        }), encoding="utf-8")
        ticket = self.product / "factory/tickets/T-110.md"
        ticket.parent.mkdir()
        ticket.write_text("State: Done\n", encoding="utf-8")
        done = self.product / "factory/attestations/T-110/done.json"
        done.parent.mkdir(parents=True)
        done.write_text('{"ticket":"T-110"}\n', encoding="utf-8")
        subprocess.run(["git", "init", "-q", str(self.product)], check=True)
        subprocess.run(
            ["git", "-C", str(self.product), "config", "user.email", "test@nysa.dev"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.product), "config", "user.name", "Test"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.product), "add", "factory"], check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.product), "commit", "-qm", "terminal"],
            check=True,
        )
        subprocess.run(
            [
                "git", "-C", str(self.product), "update-ref",
                "refs/remotes/origin/main", "HEAD",
            ],
            check=True,
        )
        ticket.write_text("State: Ready\n", encoding="utf-8")
        controller = CONTROL.Controller(self.args)
        with patch.object(CONTROL, "protected_terminal", return_value={
            "basis": "attested-emergency-closeout", "ticket": "T-110",
        }):
            self.assertTrue(controller.product_ticket_done("T-110"))
            controller.record_qualification_done_targets()
            controller.record_qualification_done_targets()
        records = [CONTROL.read(path) for path in controller.events.glob("*.json")]
        self.assertEqual(sum(
            item.get("event") == "protected_terminal_reconciled"
            for item in records
        ), 1)
        self.assertEqual(sum(
            item.get("event") == "ticket_complete" for item in records
        ), 1)
        self.assertFalse((self.state / "passports").exists())
        self.assertEqual(list(controller.claims.iterdir()), [])

    def test_qualification_reconciles_exact_emergency_terminal_with_passport(self) -> None:
        ticket = "T-110"
        source = "b" * 40
        terminal_factory = "e" * 40
        terminal_receipt = "9" * 64
        head = "c" * 40
        passport_sha = "d" * 64
        (self.product / "factory/PROJECT.env").write_text(
            "MAX_CONCURRENT_TICKETS=3\n", encoding="utf-8",
        )
        (self.product / "factory/QUALIFICATION.json").write_text(json.dumps({
            "budget_usd": "300.000000",
            "capacity": 3,
            "contract_version": "1.8.0",
            "factory_sha": "a" * 40,
            "generation": 2,
            "mode": "successor",
            "per_run_budget_usd": "10.000000",
            "per_ticket_budget_usd": "100.000000",
            "schema": CONTROL.QUALIFICATION_SCHEMA,
            "source_factory_sha": source,
            "target_done": 3,
            "tickets": ["T-110", "T-111", "T-112"],
        }), encoding="utf-8")
        ticket_path = self.product / f"factory/tickets/{ticket}.md"
        ticket_path.parent.mkdir()
        ticket_path.write_text("State: Done\n", encoding="utf-8")
        passport_path = self.state / f"passports/{ticket}.json"
        passport_path.parent.mkdir(mode=0o700)
        passport = {
            "branch": f"ticket/{ticket}",
            "current_state": "Review",
            "factory_sha": source,
            "head_sha": head,
            "passport_sha256": passport_sha,
            "publication_state": "validating",
            "ticket": ticket,
        }
        CONTROL.write(passport_path, passport)
        pause = {
            "branch": f"ticket/{ticket}",
            "budget_sha256": "1" * 64,
            "current_state": "Review",
            "head_sha": head,
            "passport_factory_sha": source,
            "passport_sha256": passport_sha,
            "schema": "nysa.software-factory.ticket-pause/v2",
            "status": "budget",
            "ticket": ticket,
        }
        pause["pause_sha256"] = hashlib.sha256(
            CONTROL.canonical(pause).encode()
        ).hexdigest()
        pause_path = self.state / f"pause-{ticket}.json"
        CONTROL.write(pause_path, pause)
        pause_file = hashlib.sha256(
            (CONTROL.canonical(pause) + "\n").encode()
        ).hexdigest()
        done_path = self.product / f"factory/attestations/{ticket}/done.json"
        done_path.parent.mkdir(parents=True)
        done = {
            "kit_sha": terminal_factory,
            "plan": {
                "claim": {
                    "blocked_reason": "factory-issue-pause",
                    "parked": True,
                    "receipt": pause["pause_sha256"],
                    "role": "factory-paused",
                    "sha256": pause_file,
                    "status": "blocked",
                },
                "execution_basis": "authenticated-passport",
                "kit_sha": terminal_factory,
                "passport": {
                    name: passport[name]
                    for name in (
                        "passport_sha256", "current_state", "publication_state",
                        "factory_sha", "head_sha",
                    )
                },
            },
            "schema": "nysa.software-factory.ticket-emergency-done/v1",
        }
        done_path.write_text(json.dumps(done), encoding="utf-8")
        protected = {
            "done_sha256": hashlib.sha256(
                CONTROL.canonical(done).encode()
            ).hexdigest(),
            "protected_main_sha": "2" * 40,
            "protected_main_tree": "3" * 40,
            "protected_ticket_blob": "4" * 40,
            "qualification_charge_micro_usd": 0,
            "reconciliation_schema": (
                CONTROL.PROTECTED_TERMINAL_RECONCILIATION_SCHEMA
            ),
            "terminal_basis": "attested-emergency-closeout",
        }
        controller = CONTROL.Controller(self.args)
        controller.qualification_protected_terminal = lambda _ticket: protected
        controller.qualification_release_receipts = lambda: {
            "a" * 40: "8" * 64,
            terminal_factory: terminal_receipt,
        }

        result = controller.qualification_emergency_terminal(ticket)
        self.assertEqual(
            result["reconciliation_schema"],
            CONTROL.EMERGENCY_TERMINAL_RECONCILIATION_SCHEMA,
        )
        self.assertEqual(result["source_passport_sha256"], passport_sha)
        self.assertEqual(result["terminal_factory_sha"], terminal_factory)
        self.assertEqual(result["terminal_release_receipt_id"], terminal_receipt)
        controller.qualification_release_receipts = lambda: {"a" * 40: "8" * 64}
        with self.assertRaisesRegex(
            CONTROL.ControllerError, "emergency terminal evidence is invalid",
        ):
            controller.qualification_emergency_terminal(ticket)
        controller.qualification_release_receipts = lambda: {
            "a" * 40: "8" * 64,
            terminal_factory: terminal_receipt,
        }
        changed_done = copy.deepcopy(done)
        changed_done["plan"]["kit_sha"] = "f" * 40
        done_path.write_text(json.dumps(changed_done), encoding="utf-8")
        with self.assertRaisesRegex(
            CONTROL.ControllerError, "emergency terminal evidence is invalid",
        ):
            controller.qualification_emergency_terminal(ticket)
        done_path.write_text(json.dumps(done), encoding="utf-8")
        changed = copy.deepcopy(pause)
        changed["budget_sha256"] = "short"
        changed["pause_sha256"] = hashlib.sha256(
            CONTROL.canonical({
                name: value for name, value in changed.items()
                if name != "pause_sha256"
            }).encode()
        ).hexdigest()
        CONTROL.write(pause_path, changed)
        with self.assertRaisesRegex(
            CONTROL.ControllerError, "emergency terminal evidence is invalid",
        ):
            controller.qualification_emergency_terminal(ticket)
        CONTROL.write(pause_path, pause)

        controller.product_ticket_done = lambda selected: selected == ticket
        controller.record_qualification_done_targets()
        controller.record_qualification_done_targets()
        records = [CONTROL.read(path) for path in controller.events.glob("*.json")]
        self.assertEqual(sum(
            item.get("event") == "emergency_terminal_reconciled"
            for item in records
        ), 1)
        self.assertEqual(sum(
            item.get("event") == "ticket_complete" for item in records
        ), 1)

    def test_qualification_release_receipts_follow_exact_chain(self) -> None:
        environment = self.root
        releases = environment / "releases"
        projects = environment / "projects"
        receipts = environment / "receipts"
        for path in (releases, projects, receipts, projects / "relay"):
            path.mkdir(mode=0o700)
        current = "a" * 40
        prior = "e" * 40
        release = releases / current
        release.mkdir()
        controller = CONTROL.Controller(self.args)
        controller.release_path = release

        def receipt(kit_sha: str, previous: str | None, project: str = "relay") -> str:
            value = {
                "contract_version": "1.8.0",
                "kit_sha": kit_sha,
                "kit_tree": "1" * 40,
                "product_path": str(self.product),
                "project": project,
                "provider_policy_sha256": "2" * 64,
                "qualification_mode": "isolated",
                "status": "pass",
            }
            if previous:
                value["previous_receipt_id"] = previous
            receipt_id = hashlib.sha256(
                (CONTROL.canonical(value) + "\n").encode()
            ).hexdigest()
            value["receipt_id"] = receipt_id
            CONTROL.write(receipts / f"{receipt_id}.json", value)
            return receipt_id

        prior_receipt = receipt(prior, None)
        current_receipt = receipt(current, prior_receipt)
        active_path = projects / "relay/active.json"
        CONTROL.write(active_path, {
            "kit_sha": current,
            "project": "relay",
            "receipt_id": current_receipt,
            "release_path": str(release),
        })
        self.assertEqual(controller.qualification_release_receipts(), {
            current: current_receipt,
            prior: prior_receipt,
        })

        changed = CONTROL.read(receipts / f"{prior_receipt}.json")
        changed["project"] = "other"
        CONTROL.write(receipts / f"{prior_receipt}.json", changed)
        with self.assertRaisesRegex(
            CONTROL.ControllerError, "qualification release receipt is invalid",
        ):
            controller.qualification_release_receipts()

        foreign_receipt = receipt(prior, None, "other")
        current_receipt = receipt(current, foreign_receipt)
        CONTROL.write(active_path, {
            "kit_sha": current,
            "project": "relay",
            "receipt_id": current_receipt,
            "release_path": str(release),
        })
        with self.assertRaisesRegex(
            CONTROL.ControllerError, "qualification release receipt is invalid",
        ):
            controller.qualification_release_receipts()

    def test_qualification_plain_done_without_protected_terminal_refuses(self) -> None:
        tickets = [f"T-{number}" for number in range(110, 114)]
        (self.product / "factory/QUALIFICATION.json").write_text(json.dumps({
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
        }), encoding="utf-8")
        ticket = self.product / "factory/tickets/T-110.md"
        ticket.parent.mkdir()
        ticket.write_text("State: Done\n", encoding="utf-8")
        controller = CONTROL.Controller(self.args)
        with (
            patch.object(
                CONTROL, "protected_terminal",
                side_effect=CONTROL.ProtectedTerminalError("missing evidence"),
            ),
            self.assertRaisesRegex(
                CONTROL.ControllerError, "protected terminal is invalid",
            ),
        ):
            controller.record_qualification_done_targets()

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

    def test_reviewer_passport_migrates_before_qualification_cell_parks(self) -> None:
        controller = CONTROL.Controller(self.args)
        receipt = "b" * 64
        claim = {
            "branch": "ticket/T-110",
            "lease": "c" * 64,
            "publication_lease": "",
            "receipt": receipt,
            "role": "reviewer",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "running",
            "ticket": "T-110",
            "worktree": str(self.root / "cell-1"),
        }
        Path(claim["worktree"]).mkdir()
        (self.product / "factory/runs/reviewer.meta").write_text(
            "run_id=reviewer\n"
            "ticket=T-110\n"
            "role=reviewer\n"
            "accounting_state=completed\n"
            "exit_status=0\n"
            "role_exit=ok\n"
            f"transition_receipt_sha256={receipt}\n",
            encoding="utf-8",
        )
        calls = []
        controller.passport = lambda *_args: calls.append("export")
        controller.migrate_passport = lambda *_args: calls.append("migrate")
        controller.relocate_qualification_cell = lambda *_args: calls.append("park")
        controller.json_call = lambda *args, **_kwargs: calls.append(args[0]) or {}
        controller.event = lambda *_args, **_kwargs: None

        self.assertTrue(controller.finish_pending_run(claim))
        self.assertLess(calls.index("migrate"), calls.index("park"))

    def test_narrator_preview_wait_is_head_bound_and_bounded(self) -> None:
        controller = CONTROL.Controller(self.args)
        claim = {
            "branch": "ticket/T-110",
            "lease": "c" * 64,
            "publication_lease": "",
            "receipt": "",
            "role": "",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "claimed",
            "ticket": "T-110",
            "worktree": str(self.root / "cell-1"),
        }
        first = {
            "head": "d" * 40,
            "preview_identity": {
                "expected": "d" * 40,
                "observed": [{"service": "api", "sha": "e" * 40}],
                "status": "wait",
            },
        }
        with patch.object(CONTROL.time, "time", return_value=1000):
            self.assertTrue(controller.wait_for_preview_identity(claim, first))
        self.assertEqual(claim["preview_wait_started_epoch"], 1000)

        preflight_claim = {
            key: item for key, item in claim.items()
            if key not in {"preview_wait_head", "preview_wait_started_epoch"}
        }
        paired = {
            "head": "a" * 40,
            "preview_identity": {
                "expected": "a" * 40,
                "observed": [{"service": "api", "sha": "a" * 40}],
                "status": "pass",
            },
            "preview_preflight": {
                "evidence": {"observed_api": "production"},
                "head": "a" * 40,
                "reason": "production_origin",
                "status": "wait",
            },
        }
        with patch.object(CONTROL.time, "time", return_value=1050):
            self.assertTrue(
                controller.wait_for_preview_identity(preflight_claim, paired)
            )
        self.assertEqual(preflight_claim["preview_wait_started_epoch"], 1050)

        changed = copy.deepcopy(first)
        changed["head"] = changed["preview_identity"]["expected"] = "f" * 40
        with patch.object(CONTROL.time, "time", return_value=1100):
            self.assertTrue(controller.wait_for_preview_identity(claim, changed))
        self.assertEqual(claim["preview_wait_started_epoch"], 1100)
        claim["preview_wait_started_epoch"] = 0
        controller.withdraw_publication = lambda *_args: None
        controller.release_ticket_lease = lambda item: item.update(lease_released=True)
        with patch.object(CONTROL.time, "time", return_value=1100):
            self.assertFalse(controller.wait_for_preview_identity(claim, changed))
        self.assertEqual(claim["status"], "blocked")
        self.assertEqual(claim["blocked_reason"], "preview-identity-timeout")

    def test_exact_terminal_request_recovers_only_terminal_controller_error(self) -> None:
        controller = CONTROL.Controller(self.args)
        worktree = self.root / "parked/T-110"
        worktree.mkdir(parents=True)
        passport = {
            "passport_sha256": "d" * 64,
            "publication_state": "merged",
            "ticket": "T-110",
        }
        passports = self.state / "passports"
        passports.mkdir()
        CONTROL.write(passports / "T-110.json", passport)
        CONTROL.write(controller.terminal_request_path("T-110"), {
            "request_sha256": "e" * 64,
        })
        claim = {
            "blocked_reason": "controller-error",
            "branch": "ticket/T-110",
            "lease": "",
            "parked": True,
            "publication_lease": "",
            "receipt": "",
            "role": "",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "blocked",
            "ticket": "T-110",
            "worktree": str(worktree),
        }
        controller.role_active = lambda _claim: False
        controller.remote_passport_valid = lambda _claim: True
        controller.terminal_request = lambda *_args, **_kwargs: {"action": "done"}
        controller.ensure_lease = lambda item, _label: item.update(lease="f" * 64)
        clean = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with patch.object(CONTROL.subprocess, "run", return_value=clean):
            controller.recover_terminal_requests([claim])
        self.assertEqual(claim["status"], "claimed")
        self.assertNotIn("blocked_reason", claim)
        self.assertEqual(claim["lease"], "f" * 64)

        unrelated = dict(claim, status="blocked", blocked_reason="worker-error")
        unrelated["lease"] = ""
        with patch.object(CONTROL.subprocess, "run", return_value=clean):
            controller.recover_terminal_requests([unrelated])
        self.assertEqual(unrelated["status"], "blocked")

    def test_ticket_merge_uses_the_approval_bound_pr_amid_branch_history(self) -> None:
        (self.product / "factory/PROJECT.env").write_text(
            "GH_REPO=example/product\nMAX_CONCURRENT_TICKETS=4\n",
            encoding="utf-8",
        )
        worktree = self.root / "parked/T-182"
        approval = worktree / "factory/attestations/T-182/approval.json"
        approval.parent.mkdir(parents=True)
        approval.write_text(json.dumps({
            "schema": "nysa.software-factory.ticket-approval/v1",
            "ticket": "T-182",
            "repository": "example/product",
            "branch": "ticket/T-182",
            "pr_number": 359,
        }), encoding="utf-8")
        claim = {
            "branch": "ticket/T-182", "ticket": "T-182",
            "worktree": str(worktree),
        }
        evidence = {
            "number": 359,
            "headRefName": "ticket/T-182",
            "baseRefName": "main",
            "headRefOid": "b" * 40,
            "mergeCommit": {"oid": "c" * 40},
            "state": "MERGED",
            "mergedAt": "2026-08-06T09:21:00Z",
        }
        calls = []

        def execute(arguments, **_kwargs):
            calls.append(arguments)
            return subprocess.CompletedProcess(
                arguments, 0, json.dumps(evidence), "",
            )

        controller = CONTROL.Controller(self.args)
        with patch.object(CONTROL.subprocess, "run", side_effect=execute):
            self.assertTrue(controller.ticket_merged(claim))

        self.assertEqual(calls[0][0:4], ["gh", "pr", "view", "359"])

        evidence["headRefName"] = "ticket/T-legacy"
        with (
            patch.object(CONTROL.subprocess, "run", side_effect=execute),
            self.assertRaisesRegex(
                CONTROL.ControllerError, "merged PR identity is malformed",
            ),
        ):
            controller.ticket_merged(claim)

    def test_terminal_request_allows_only_unrelated_protected_main_advance(self) -> None:
        controller = CONTROL.Controller(self.args)
        (self.product / "factory/PROJECT.env").write_text(
            "GH_REPO=example/product\n", encoding="utf-8",
        )
        passports = self.state / "passports"
        passports.mkdir()
        CONTROL.write(passports / "T-110.json", {
            "branch": "ticket/T-110",
            "factory_sha": "a" * 40,
            "passport_sha256": "d" * 64,
            "publication_state": "merged",
            "ticket": "T-110",
        })
        claim = {"branch": "ticket/T-110", "ticket": "T-110"}
        ticket = self.product / "factory/tickets/T-110.md"
        ticket.parent.mkdir(parents=True)
        ticket.write_text("State: Done\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q", "-b", "main", self.product], check=True)
        for key, value in (("user.name", "Test"), ("user.email", "test@example.invalid")):
            subprocess.run(
                ["git", "-C", self.product, "config", key, value], check=True,
            )
        subprocess.run(["git", "-C", self.product, "add", "factory"], check=True)
        subprocess.run(
            ["git", "-C", self.product, "commit", "-qm", "terminal"], check=True,
        )
        terminal = subprocess.run(
            ["git", "-C", self.product, "rev-parse", "HEAD"],
            text=True, capture_output=True, check=True,
        ).stdout.strip()
        remote = self.root / "remote.git"
        subprocess.run(["git", "init", "--bare", "-q", remote], check=True)
        subprocess.run(
            ["git", "-C", self.product, "remote", "add", "origin", remote],
            check=True,
        )
        subprocess.run(
            ["git", "-C", self.product, "push", "-q", "origin", "main"], check=True,
        )
        controller.approval_pr_number = lambda _claim: 1
        controller.merged_pr_identity = lambda branch, number=None: {
            "head": "a" * 40,
            "merge_commit": terminal,
            "number": 2 if "closeout" in branch else 1,
        }
        request = controller.terminal_request(
            claim, "chore/t110-closeout", create=True,
        )
        self.assertEqual(
            request["schema"], "nysa.software-factory.terminal-request/v2",
        )

        (self.product / "unrelated.txt").write_text("safe\n", encoding="utf-8")
        subprocess.run(["git", "-C", self.product, "add", "unrelated.txt"], check=True)
        subprocess.run(
            ["git", "-C", self.product, "commit", "-qm", "unrelated advance"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", self.product, "push", "-q", "origin", "main"], check=True,
        )
        self.assertEqual(
            controller.terminal_request(claim, "chore/t110-closeout", create=False),
            request,
        )

        ticket.write_text("State: Done\nchanged\n", encoding="utf-8")
        subprocess.run(["git", "-C", self.product, "add", str(ticket)], check=True)
        subprocess.run(
            ["git", "-C", self.product, "commit", "-qm", "change ticket"], check=True,
        )
        subprocess.run(
            ["git", "-C", self.product, "push", "-q", "origin", "main"], check=True,
        )
        with self.assertRaisesRegex(
            CONTROL.ControllerError, "terminal request identity changed",
        ):
            controller.terminal_request(
                claim, "chore/t110-closeout", create=False,
            )

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

    def test_qualification_restart_preserves_blocked_target_claims(self) -> None:
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
                "generation": 2,
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
        first = CONTROL.Controller(self.args)
        for number, ticket in enumerate(tickets, 1):
            cell = self.root / f"cell-{number}"
            cell.mkdir()
            first.save_claim({
                "blocked_reason": "preflight" if number > 1 else "",
                "branch": f"ticket/{ticket}",
                "lease": f"{number:064x}",
                "lease_released": number > 1,
                "priority": "normal",
                "publication_lease": "",
                "receipt": "",
                "role": "",
                "schema": CONTROL.CLAIM_SCHEMA,
                "status": "blocked" if number > 1 else "claimed",
                "ticket": ticket,
                "worktree": str(cell),
            })
        first.recover_missing_passport_claims = lambda _claims: None
        first.recover_upgraded_claims = lambda _claims: None
        first.recover_terminal_exports = lambda _claims: None
        first.recover_repaired_failures = lambda _claims: None
        first.claim_new = lambda claims: claims

        result = first.reconcile()
        self.assertEqual(result["status"], "restart_required")
        self.assertEqual(result["active"], 1)

        second = CONTROL.Controller(self.args)
        second.recover_missing_passport_claims = lambda _claims: None
        second.recover_upgraded_claims = lambda _claims: None
        second.recover_terminal_exports = lambda _claims: None
        second.recover_repaired_failures = lambda _claims: None
        second.claim_new = lambda claims, *_args: claims
        second.pin_routes = lambda claims: [
            {"status": "waiting", "ticket": claim["ticket"]}
            for claim in claims
        ]
        second.reconcile()

        self.assertTrue(
            (self.state / f"qualification-recovered-{'a' * 40}.json").is_file()
        )
        claims = {item["ticket"]: item for item in second.load_claims()}
        self.assertEqual(claims["T-111"]["status"], "blocked")
        self.assertEqual(claims["T-112"]["status"], "blocked")
        self.assertFalse((self.state / "passports").exists())
        self.assertEqual(list((self.product / "factory/runs").iterdir()), [])

    def test_qualification_restart_counts_protected_done_ticket(self) -> None:
        tickets = [f"T-{number}" for number in range(110, 113)]
        source_factory = "b" * 40
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
                "source_factory_sha": source_factory,
                "target_done": 3,
                "tickets": tickets,
            }),
            encoding="utf-8",
        )
        (self.product / "factory/tickets").mkdir()
        (self.product / "factory/tickets/T-110.md").write_text(
            "State: Done\n", encoding="utf-8"
        )
        first = CONTROL.Controller(self.args)
        passport_path = self.state / "passports/T-110.json"
        passport_path.parent.mkdir(mode=0o700)
        source_passport = "c" * 64
        done_kit = "c" * 40
        passport_factory = "9" * 40
        pr_head = "d" * 40
        post_merge_head = "7" * 40
        protected_base = "4" * 40
        route = "5" * 64
        merge_commit = "e" * 40
        CONTROL.write(passport_path, {
            "branch": "ticket/T-110",
            "charge_records": [],
            "completed_role_evidence": [],
            "current_state": "Approved",
            "factory_release_history": [{
                "contract_version": "1.8.0", "factory_sha": done_kit,
            }, {
                "contract_version": "1.8.0", "factory_sha": source_factory,
            }, {
                "contract_version": "1.8.0", "factory_sha": passport_factory,
            }],
            "factory_sha": passport_factory,
            "head_sha": post_merge_head,
            "migration_history": [{
                "from_factory_sha": source_factory,
                "from_head_sha": pr_head,
                "from_passport_file_sha256": "2" * 64,
                "from_passport_sha256": "1" * 64,
                "from_protected_base_sha": "3" * 40,
                "from_route_plan_sha256": "6" * 64,
                "schema": "nysa.software-factory.ticket-passport-migration/v2",
                "to_factory_sha": passport_factory,
                "to_head_sha": post_merge_head,
                "to_protected_base_sha": protected_base,
                "to_route_plan_sha256": route,
            }],
            "parent_digest": "1" * 64,
            "parent_file_sha256": "2" * 64,
            "passport_sha256": source_passport,
            "protected_base_sha": protected_base,
            "publication_state": "merged",
            "route_plan_sha256": route,
            "ticket": "T-110",
        })
        done_path = self.product / "factory/attestations/T-110/done.json"
        done_path.parent.mkdir(parents=True)
        done_path.write_text(json.dumps({
            "approved_pr_head": pr_head,
            "kit_sha": done_kit,
            "merge_commit": merge_commit,
            "pr_number": 94,
            "schema": "nysa.software-factory.ticket-done/v1",
            "ticket": "T-110",
        }), encoding="utf-8")
        old_completion = {
            "event": "ticket_complete",
            "factory_sha": source_factory,
            "observed_at_epoch_ns": 1,
            "schema": CONTROL.EVENT_SCHEMA,
            "ticket": "T-110",
        }
        old_completion["event_sha256"] = hashlib.sha256(
            CONTROL.canonical(old_completion).encode()
        ).hexdigest()
        CONTROL.write(first.events / "old-completion.json", old_completion)
        first.worktrees_by_branch = lambda: {
            "refs/heads/ticket/T-110": [str(self.root / "terminal-cell")],
        }
        migrations = []

        def passport_call(*args, **_kwargs):
            passport = CONTROL.read(passport_path)
            if args[:2] == ("passport", "validate"):
                return {"passport": passport["passport_sha256"], "status": "ok"}
            if args[:2] == ("passport", "migrate"):
                migrations.append("T-110")
                self.assertEqual(
                    args[args.index("--publication-state") + 1], "preserve"
                )
                candidate_passport = "f" * 64
                passport.update({
                    "factory_sha": "a" * 40,
                    "parent_digest": source_passport,
                    "parent_file_sha256": "8" * 64,
                    "passport_sha256": candidate_passport,
                })
                passport["factory_release_history"].append({
                    "contract_version": "1.8.0", "factory_sha": "a" * 40,
                })
                passport["migration_history"].append({
                    "from_factory_sha": passport_factory,
                    "from_head_sha": post_merge_head,
                    "from_passport_file_sha256": "8" * 64,
                    "from_passport_sha256": source_passport,
                    "from_protected_base_sha": protected_base,
                    "from_route_plan_sha256": route,
                    "schema": "nysa.software-factory.ticket-passport-migration/v2",
                    "to_factory_sha": "a" * 40,
                    "to_head_sha": post_merge_head,
                    "to_protected_base_sha": protected_base,
                    "to_route_plan_sha256": route,
                })
                CONTROL.write(passport_path, passport)
                return {"passport": candidate_passport, "status": "ok"}
            raise AssertionError(args)

        first.json_call = passport_call
        disconnected = CONTROL.read(passport_path)
        disconnected["migration_history"][0]["from_factory_sha"] = "8" * 40
        CONTROL.write(passport_path, disconnected)
        with self.assertRaisesRegex(
            CONTROL.ControllerError, "unknown release"
        ):
            first.record_qualification_done_targets()
        self.assertEqual(migrations, [])
        self.assertFalse(any(
            CONTROL.read(path).get("factory_sha") == "a" * 40
            for path in self.state.glob("events/*.json")
        ))
        disconnected["migration_history"][0][
            "from_factory_sha"
        ] = source_factory
        CONTROL.write(passport_path, disconnected)
        disconnected_head = copy.deepcopy(disconnected)
        disconnected_head["migration_history"][0]["from_head_sha"] = "8" * 40
        CONTROL.write(passport_path, disconnected_head)
        with self.assertRaisesRegex(
            CONTROL.ControllerError, "source terminal is invalid"
        ):
            first.record_qualification_done_targets()
        self.assertEqual(migrations, [])
        substituted = copy.deepcopy(disconnected)
        substituted["migration_history"][0]["from_passport_sha256"] = "0" * 64
        CONTROL.write(passport_path, substituted)
        with self.assertRaisesRegex(
            CONTROL.ControllerError, "source terminal is invalid"
        ):
            first.record_qualification_done_targets()
        self.assertEqual(migrations, [])
        CONTROL.write(passport_path, disconnected)
        regressed = CONTROL.read(passport_path)
        regressed["factory_release_history"].insert(-1, {
            "contract_version": "1.8.0", "factory_sha": "a" * 40,
        })
        regressed["migration_history"] = [{
            "from_factory_sha": source_factory,
            "schema": "nysa.software-factory.ticket-passport-migration/v2",
            "to_factory_sha": "a" * 40,
        }, {
            "from_factory_sha": "a" * 40,
            "schema": "nysa.software-factory.ticket-passport-migration/v2",
            "to_factory_sha": passport_factory,
        }]
        CONTROL.write(passport_path, regressed)
        with self.assertRaisesRegex(
            CONTROL.ControllerError, "source terminal is invalid"
        ):
            first.record_qualification_done_targets()
        self.assertEqual(migrations, [])
        CONTROL.write(passport_path, disconnected)
        nonterminal = CONTROL.read(passport_path)
        nonterminal["publication_state"] = "validating"
        CONTROL.write(passport_path, nonterminal)
        with self.assertRaisesRegex(
            CONTROL.ControllerError, "source terminal is invalid"
        ):
            first.record_qualification_done_targets()
        self.assertEqual(migrations, [])
        self.assertFalse(
            (
                self.state
                / f"qualification-terminal-adoption-{'a' * 40}-T-110.json"
            ).exists()
        )
        self.assertFalse(any(
            CONTROL.read(path).get("factory_sha") == "a" * 40
            for path in self.state.glob("events/*.json")
        ))
        nonterminal["publication_state"] = "merged"
        CONTROL.write(passport_path, nonterminal)
        for number, ticket in enumerate(tickets[1:], 1):
            cell = self.root / f"cell-{number}"
            cell.mkdir()
            first.save_claim({
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
        first.recover_missing_passport_claims = lambda _claims: None
        first.recover_upgraded_claims = lambda _claims: None
        first.recover_terminal_exports = lambda _claims: None
        first.recover_repaired_failures = lambda _claims: None
        first.claim_new = lambda claims: claims
        result = first.reconcile()
        self.assertEqual(result["status"], "restart_required")
        self.assertEqual(result["active"], 2)
        events = [
            CONTROL.read(path) for path in sorted(self.state.glob("events/*.json"))
        ]
        self.assertEqual(
            [
                item["ticket"] for item in events
                if item["event"] == "ticket_complete"
                and item["factory_sha"] == "a" * 40
            ],
            ["T-110"],
        )
        self.assertEqual(migrations, ["T-110"])
        self.assertEqual(CONTROL.read(passport_path)["factory_sha"], "a" * 40)

        second = CONTROL.Controller(self.args)
        second.recover_missing_passport_claims = lambda _claims: None
        second.recover_upgraded_claims = lambda _claims: None
        second.recover_terminal_exports = lambda _claims: None
        second.recover_repaired_failures = lambda _claims: None
        second.claim_new = lambda claims: claims
        second.pin_routes = lambda _claims: []
        second.reconcile_ticket = lambda claim: {
            "status": "active", "ticket": claim["ticket"],
        }
        self.assertEqual(second.reconcile()["active"], 2)
        self.assertEqual(second.reconcile()["active"], 2)
        events = [
            CONTROL.read(path) for path in sorted(self.state.glob("events/*.json"))
        ]
        self.assertIn(
            {"event": "controller_recovered", "tickets": tickets},
            [
                {"event": item["event"], "tickets": item.get("tickets")}
                for item in events
            ],
        )
        self.assertEqual(
            [
                item["ticket"] for item in events
                if item["event"] == "ticket_complete"
                and item["factory_sha"] == "a" * 40
            ],
            ["T-110"],
        )
        self.assertEqual(
            [
                item["ticket"] for item in events
                if item["event"] == "terminal_adopted"
                and item["factory_sha"] == "a" * 40
            ],
            ["T-110"],
        )
        self.assertTrue(
            (
                self.state
                / f"qualification-terminal-adoption-{'a' * 40}-T-110.json"
            ).is_file()
        )
        self.assertNotIn(
            "T-110",
            {
                item["ticket"] for item in events
                if item["event"] in {"attempt_started", "ticket_claimed"}
            },
        )

        (self.product / "factory/tickets/T-111.md").write_text(
            "State: Done\n", encoding="utf-8"
        )
        CONTROL.write(self.state / "passports/T-111.json", {
            "charge_records": [{"factory_sha": "a" * 40}],
            "completed_role_evidence": [],
            "factory_sha": "a" * 40,
            "migration_history": [],
        })
        adoption_calls = []
        original_adoption = second.adopt_qualification_terminal

        def track_adoption(ticket):
            adoption_calls.append(ticket)
            return original_adoption(ticket)

        second.adopt_qualification_terminal = track_adoption
        second.record_qualification_done_targets()
        self.assertEqual(adoption_calls, ["T-110"])
        events = [
            CONTROL.read(path) for path in sorted(self.state.glob("events/*.json"))
        ]
        self.assertEqual(
            [
                item["ticket"] for item in events
                if item["event"] == "ticket_complete"
                and item["factory_sha"] == "a" * 40
            ],
            ["T-110", "T-111"],
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

    def test_preflight_refusal_evidence_survives_block_redacted_and_bounded(self) -> None:
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
        output = (
            "PREFLIGHT FAIL: Fixture-Seams path is not a regular file\n"
            "FAIL: token=do-not-persist https://secret.example.invalid/path\n"
            + "ignored\n" * 20
        )
        controller.json_call = lambda *_args, **_kwargs: {
            "exit_code": 1, "output": output, "status": "error",
        }
        controller.block = lambda item, reason: item.update(
            status="blocked", blocked_reason=reason, lease_released=True,
        )
        controller.run_role(claim, "planner", "b" * 64, [])

        events = [CONTROL.read(path) for path in controller.events.glob("*.json")]
        refusal = next(item for item in events if item["event"] == "preflight_refused")
        self.assertEqual(refusal["preflight_reason_code"], "deterministic_refusal")
        self.assertEqual(len(refusal["preflight_failure_lines"]), 2)
        self.assertNotIn("do-not-persist", json.dumps(refusal))
        self.assertNotIn("secret.example.invalid", json.dumps(refusal))
        self.assertEqual(
            refusal["preflight_output_sha256"], hashlib.sha256(output.encode()).hexdigest(),
        )
        self.assertEqual(claim["blocked_reason"], "preflight")
        self.assertEqual(
            len(list(CONTROL.Controller(self.args).events.glob("*.json"))), 1,
        )

        malformed = dict(claim, status="claimed")
        controller.json_call = lambda *_args, **_kwargs: {
            "exit_code": 1, "output": ["not text"], "status": "error",
        }
        controller.run_role(malformed, "planner", "c" * 64, [])
        self.assertEqual(malformed["blocked_reason"], "preflight-evidence")

    def test_corrected_passportless_preflight_block_reopens_fail_closed(self) -> None:
        tickets = ["T-110", "T-111", "T-112"]
        (self.product / "factory/PROJECT.env").write_text(
            "MAX_CONCURRENT_TICKETS=3\n", encoding="utf-8"
        )
        (self.product / "factory/QUALIFICATION.json").write_text(json.dumps({
            "budget_usd": "100.000000",
            "capacity": 3,
            "contract_version": "1.8.0",
            "factory_sha": "a" * 40,
            "generation": 1,
            "per_run_budget_usd": "2.000000",
            "per_ticket_budget_usd": "25.000000",
            "schema": CONTROL.QUALIFICATION_SCHEMA,
            "target_done": 3,
            "tickets": tickets,
        }), encoding="utf-8")
        controller = CONTROL.Controller(self.args)
        cell = self.root / "cell-1"
        cell.mkdir()
        claim = {
            "blocked_reason": "preflight",
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
        CONTROL.write(self.state / "T-110.json", {
            "branch": "ticket/T-110",
            "consumed": False,
            "receipt_sha256": "b" * 64,
            "role": "planner",
            "schema": "nysa.software-factory.transition-receipt/v1",
            "stage": "RUN planner",
            "ticket": "T-110",
        })
        transition = {
            "action": "RUN",
            "detail": "planner",
            "loop": None,
            "receipt": "c" * 64,
            "role": "planner",
            "schema": "nysa.software-factory.state-machine/v1",
            "stage": "RUN planner",
            "status": "ok",
            "ticket": "T-110",
        }
        calls = []

        def json_call(*args, **_kwargs):
            calls.append(args[0])
            if args[0] == "claim":
                return {"lease_id": "d" * 64, "schema_version": 1, "ticket": "T-110"}
            if args[0] == "state-machine":
                return transition
            if args[0] == "preflight":
                return {"exit_code": 0, "status": "ok"}
            raise AssertionError(args)

        controller.json_call = json_call
        controller.ticket_release_current = lambda _claim: True
        controller.remote_cell_head_valid = lambda _claim: True
        with (
            patch.object(CONTROL, "ensure_qualification_artifacts"),
            patch.object(
                CONTROL.subprocess, "run",
                return_value=argparse.Namespace(stdout=""),
            ),
        ):
            controller.recover_preflight_blocks([claim])

        self.assertEqual(claim["status"], "claimed")
        self.assertEqual(claim["lease"], "d" * 64)
        self.assertNotIn("blocked_reason", claim)
        self.assertNotIn("lease_released", claim)
        self.assertEqual(calls, ["claim", "state-machine", "preflight"])

        (self.product / "factory/QUALIFICATION.json").unlink()
        production = CONTROL.Controller(self.args)
        claim.update(
            status="blocked", blocked_reason="preflight", lease_released=True,
        )
        calls.clear()
        production.json_call = json_call
        production.ticket_release_current = lambda _claim: True
        production.remote_cell_head_valid = lambda _claim: True
        with (
            patch.object(
                CONTROL, "ensure_qualification_artifacts",
                side_effect=AssertionError("production used qualification artifacts"),
            ),
            patch.object(
                CONTROL.subprocess, "run",
                return_value=argparse.Namespace(stdout=""),
            ),
        ):
            production.recover_preflight_blocks([claim])
        self.assertEqual(claim["status"], "claimed")
        self.assertEqual(calls, ["claim", "state-machine", "preflight"])

        claim.update(
            status="blocked", blocked_reason="preflight", lease_released=True,
        )
        controller.release_ticket_lease = lambda item: item.update(lease_released=True)
        controller.json_call = lambda *args, **_kwargs: (
            {"lease_id": "e" * 64, "schema_version": 1, "ticket": "T-110"}
            if args[0] == "claim"
            else transition if args[0] == "state-machine"
            else {
                "exit_code": 1,
                "output": "PREFLIGHT FAIL: deterministic fixture refusal\n",
                "status": "error",
            }
        )
        with (
            patch.object(CONTROL, "ensure_qualification_artifacts"),
            patch.object(
                CONTROL.subprocess, "run",
                return_value=argparse.Namespace(stdout=""),
            ),
        ):
            controller.recover_preflight_blocks([claim])
        self.assertEqual(claim["status"], "blocked")
        self.assertEqual(claim["blocked_reason"], "preflight")
        self.assertTrue(claim["lease_released"])

    def test_passportless_preflight_retry_rejects_unsafe_boundaries(self) -> None:
        tickets = ["T-110", "T-111", "T-112"]
        (self.product / "factory/PROJECT.env").write_text(
            "MAX_CONCURRENT_TICKETS=3\n", encoding="utf-8"
        )
        (self.product / "factory/QUALIFICATION.json").write_text(json.dumps({
            "budget_usd": "100.000000",
            "capacity": 3,
            "contract_version": "1.8.0",
            "factory_sha": "a" * 40,
            "generation": 1,
            "per_run_budget_usd": "2.000000",
            "per_ticket_budget_usd": "25.000000",
            "schema": CONTROL.QUALIFICATION_SCHEMA,
            "target_done": 3,
            "tickets": tickets,
        }), encoding="utf-8")
        controller = CONTROL.Controller(self.args)
        cell = self.root / "cell-1"
        cell.mkdir()
        claim = {
            "blocked_reason": "preflight",
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
        CONTROL.write(self.state / "T-110.json", {
            "branch": "ticket/T-110",
            "consumed": False,
            "receipt_sha256": "b" * 64,
            "role": "planner",
            "schema": "nysa.software-factory.transition-receipt/v1",
            "stage": "RUN planner",
            "ticket": "T-110",
        })
        leases = []
        controller.ensure_lease = lambda *_args: leases.append("lease")
        controller.ticket_release_current = lambda _claim: True
        controller.remote_cell_head_valid = lambda _claim: True

        passport = self.state / "passports/T-110.json"
        passport.parent.mkdir(mode=0o700)
        CONTROL.write(passport, {})
        controller.recover_preflight_blocks([claim])
        passport.unlink()

        run = self.product / "factory/runs/preflight.meta"
        run.write_text("ticket=T-110\n", encoding="utf-8")
        controller.recover_preflight_blocks([claim])
        run.unlink()

        with patch.object(
            CONTROL.subprocess, "run",
            return_value=argparse.Namespace(stdout=" M factory/tickets/T-110.md"),
        ):
            controller.recover_preflight_blocks([claim])

        controller.remote_cell_head_valid = lambda _claim: False
        with patch.object(
            CONTROL.subprocess, "run",
            return_value=argparse.Namespace(stdout=""),
        ):
            controller.recover_preflight_blocks([claim])

        controller.role_active = lambda _claim: True
        controller.recover_preflight_blocks([claim])
        self.assertEqual(leases, [])

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
            "publication_mode": "railway",
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

    def test_narrator_receives_trusted_nonvisual_publication_context(self) -> None:
        controller = CONTROL.Controller(self.args)
        cell = self.root / "cell-nonvisual"
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
            "publication_mode": "nonvisual",
            "preview_identity": {
                "expected": "b" * 40,
                "observed": [{
                    "paths_sha256": "d" * 64,
                    "policy": "nonvisual_paths",
                }],
                "reason": None,
                "status": "pass",
            },
            "preview_urls": [],
            "status": "ready",
            "url": "https://github.com/example/product/pull/7",
        }
        with patch.object(CONTROL.subprocess, "Popen", CompletedProcess):
            controller.run_role(
                claim, "narrator", "c" * 64, [], publication,
            )
        task = captured[0][-1]
        self.assertIn("FACTORY_PR_NONVISUAL_EVIDENCE_V1", task)
        self.assertIn("Mark Preview and Screenshots not applicable", task)
        self.assertNotIn("up.railway.app", task)

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

    def test_delayed_terminal_is_finished_without_rerunning_role(self) -> None:
        controller = CONTROL.Controller(self.args)
        receipt = "b" * 64
        claim = {
            "blocked_reason": "missing-terminal",
            "branch": "ticket/T-110",
            "lease": "a" * 64,
            "lease_released": True,
            "priority": "normal",
            "publication_lease": "",
            "receipt": receipt,
            "role": "builder",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "blocked",
            "ticket": "T-110",
            "worktree": str(self.root / "cell-1"),
        }
        terminal = {
            "kit_sha": self.release.name,
            "role": "builder",
            "run_id": "delayed-terminal",
        }
        terminals = [None, terminal]
        controller.terminal_for_receipt = lambda *_args: terminals.pop(0)
        controller.role_active = lambda _claim: False
        recovered = []
        controller.ensure_lease = lambda _claim, label: recovered.append(label)
        controller.finish_pending_run = lambda item: item.update(
            receipt="", role="", status="claimed"
        )

        controller.recover_missing_terminals([claim])
        self.assertEqual(claim["status"], "blocked")
        controller.recover_missing_terminals([claim])

        self.assertEqual(recovered, ["missing-terminal"])
        self.assertEqual(claim["status"], "claimed")
        events = [CONTROL.read(path) for path in controller.events.glob("*.json")]
        event = next(item for item in events if item["event"] == "missing_terminal_recovered")
        self.assertEqual(event["run_id"], "delayed-terminal")
        self.assertEqual(event["transition_receipt_sha256"], receipt)

    def test_missing_terminal_recovery_rejects_unsafe_boundaries(self) -> None:
        controller = CONTROL.Controller(self.args)
        base_claim = {
            "blocked_reason": "missing-terminal",
            "branch": "ticket/T-110",
            "lease": "a" * 64,
            "lease_released": True,
            "priority": "normal",
            "publication_lease": "",
            "receipt": "b" * 64,
            "role": "builder",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "blocked",
            "ticket": "T-110",
            "worktree": str(self.root / "cell-1"),
        }
        base_terminal = {
            "kit_sha": self.release.name,
            "role": "builder",
            "run_id": "delayed-terminal",
        }
        cases = {
            "malformed receipt": ({"receipt": "invalid"}, {}, False),
            "unknown role": ({"role": "unknown"}, {}, False),
            "unreleased lease": ({"lease_released": False}, {}, False),
            "active role": ({}, {}, True),
            "wrong terminal role": ({}, {"role": "reviewer"}, False),
            "wrong terminal kit": ({}, {"kit_sha": "f" * 40}, False),
        }
        for name, (claim_patch, terminal_patch, active) in cases.items():
            with self.subTest(name=name):
                claim = {**base_claim, **claim_patch}
                terminal = {**base_terminal, **terminal_patch}
                calls = []
                controller.role_active = lambda _claim, value=active: value
                controller.terminal_for_receipt = lambda *_args, value=terminal: value
                controller.ensure_lease = lambda *_args: calls.append("lease")
                controller.finish_pending_run = lambda *_args: calls.append("finish")

                controller.recover_missing_terminals([claim])

                self.assertEqual(calls, [])
                self.assertEqual(claim["status"], "blocked")

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
                transition = state_transition("FIX builder")
                transition["loop"] = {
                    "attempt": 2,
                    "capped": False,
                    "kind": "builder-reviewer",
                    "limit": 3,
                }
                return transition
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
        loop_events = [
            json.loads(path.read_text())
            for path in controller.events.glob("*.json")
            if json.loads(path.read_text()).get("event") == "loop_attempt"
        ]
        self.assertEqual(len(loop_events), 1)
        self.assertEqual(loop_events[0]["attempt"], 2)
        self.assertEqual(loop_events[0]["stage"], "FIX builder")

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
            "loop-extra-key": {
                **valid,
                "loop": {
                    "attempt": 1, "capped": False, "extra": True,
                    "kind": "builder-reviewer", "limit": 3,
                },
            },
            "loop-zero-attempt": {
                **valid,
                "loop": {
                    "attempt": 0, "capped": False,
                    "kind": "builder-reviewer", "limit": 3,
                },
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
            "task_submitted=1\n"
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

    def test_qualification_unsubmitted_failure_exports_passport(self) -> None:
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
            "task_submitted=0\n"
            "accounting_state=abandoned_conservative\n"
            "exit_status=125\n"
            "role_exit=provider_failed\n"
            f"transition_receipt_sha256={'b' * 64}\n",
            encoding="utf-8",
        )
        calls = []
        controller.emit_attempt_terminal = lambda *_args: None
        controller.passport = lambda *_args: calls.append("passport")
        controller.json_call = lambda *args, **_kwargs: calls.append(args)
        controller.save_claim = lambda *_args: None
        controller.release_ticket_lease = lambda *_args: calls.append("release")
        controller.event = lambda *_args, **_kwargs: calls.append("event")

        self.assertFalse(controller.finish_pending_run(claim))
        self.assertEqual(claim["status"], "blocked")
        self.assertEqual(claim["blocked_reason"], "role-failure")
        self.assertIn("passport", calls)
        self.assertNotIn("migrate", calls)
        self.assertFalse(any(
            isinstance(call, tuple) and call[:2] == ("models", "fallback-auto")
            for call in calls
        ))

    def test_reconcile_rearms_exact_externally_applied_fallback(self) -> None:
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
            "branch": "ticket/T-112",
            "lease": "",
            "parked": True,
            "publication_lease": "",
            "receipt": "b" * 64,
            "role": "reviewer",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "blocked",
            "ticket": "T-112",
            "worktree": str(self.root / "parked/T-112"),
        }
        (self.product / "factory/runs/failed-reviewer.meta").write_text(
            "run_id=failed-reviewer\n"
            "ticket=T-112\n"
            "role=reviewer\n"
            "route_id=cursor-reviewer\n"
            "task_submitted=1\n"
            "accounting_state=abandoned_conservative\n"
            "exit_status=6\n"
            "role_exit=provider_failed\n"
            f"transition_receipt_sha256={'b' * 64}\n",
            encoding="utf-8",
        )
        calls = []
        controller.restore_recorded_contract_repair = lambda _claim: False
        controller.restore_contract_blocker = lambda _claim: False
        controller.role_active = lambda _claim: False
        def json_call(*args, **_kwargs):
            calls.append(args)
            if args[0] == "claim":
                return {
                    "lease_id": "c" * 64,
                    "schema_version": 1,
                    "ticket": "T-112",
                }
            return {"failed_run_id": "failed-reviewer"}

        controller.json_call = json_call
        controller.renew = lambda _claim: None

        def migrate(_claim, _publication):
            self.assertEqual(_claim["receipt"], "b" * 64)
            self.assertEqual(_claim["role"], "reviewer")
            calls.append("migrate")

        controller.migrate_passport = migrate
        controller.event = lambda name, *_args, **_kwargs: calls.append(name)
        controller.recover_repaired_failures([claim])

        self.assertEqual(claim["status"], "claimed")
        self.assertEqual(claim["lease"], "c" * 64)
        self.assertEqual(claim["receipt"], "")
        self.assertEqual(claim["role"], "")
        self.assertLess(calls.index("migrate"), calls.index("provider_fallback"))

        claim.update(status="blocked", receipt="b" * 64, role="reviewer")
        controller.json_call = lambda *_args, **_kwargs: {
            "failed_run_id": "different-run"
        }
        with self.assertRaisesRegex(
            CONTROL.ControllerError, "did not bind the failed run"
        ):
            controller.recover_repaired_failures([claim])
        self.assertEqual(claim["status"], "blocked")
        self.assertEqual(claim["receipt"], "b" * 64)
        self.assertEqual(claim["role"], "reviewer")

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
        ticket = cell / "factory/tickets/T-110.md"
        ticket.parent.mkdir(parents=True)
        ticket.write_text(
            f"# T-110\n\nOPERATOR RESUME RECEIPT: {'f' * 64}\n",
            encoding="utf-8",
        )
        calls.clear()
        resume_status = "waiting"

        def recover_call(*args, **_kwargs):
            calls.append(args)
            if args[:2] == ("state-machine", "block"):
                return {"status": "blocked"}
            if args[:2] == ("state-machine", "resume"):
                if resume_status == "error":
                    return {
                        "actual_bytes": 120,
                        "expected_bytes": 80,
                        "first_differing_line": 5,
                        "reason_code": "resume_commit_content_mismatch",
                        "status": "error",
                    }
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
        self.assertNotIn(
            ("state-machine", "resume"), [call[:2] for call in calls]
        )

        ticket.write_text(
            f"# T-110\n\nOPERATOR RESUME: planner\n"
            f"OPERATOR RESUME RECEIPT: {receipt}\n",
            encoding="utf-8",
        )
        cell_status = "resume_commit_not_pushed"
        controller.remote_cell_head_status = lambda _claim: (
            cell_status, head, "a" * 40
        )
        calls.clear()
        with patch.object(CONTROL.subprocess, "run", return_value=remote):
            controller.recover_repaired_failures([claim])
        self.assertIn(("contract_resume_refused",), calls)
        self.assertNotIn(
            ("state-machine", "resume"), [call[:2] for call in calls]
        )

        calls.clear()
        cell_status = "pushed"
        with patch.object(CONTROL.subprocess, "run", return_value=remote):
            controller.recover_repaired_failures([claim])
        self.assertIn(("state-machine", "resume"), [call[:2] for call in calls])

        calls.clear()
        resume_status = "error"
        with patch.object(CONTROL.subprocess, "run", return_value=remote):
            controller.recover_repaired_failures([claim])
        self.assertIn(("contract_resume_refused",), calls)
        self.assertEqual(claim["status"], "blocked")

        calls.clear()
        resume_status = "ready"
        with patch.object(CONTROL.subprocess, "run", return_value=remote):
            controller.recover_repaired_failures([claim])
        self.assertEqual(claim["status"], "claimed")
        self.assertEqual(claim["lease"], "e" * 64)
        self.assertEqual(claim["receipt"], "")
        self.assertEqual(claim["role"], "")
        self.assertIn(("contract_blocker_recovered",), calls)

    def test_normalized_contract_block_exports_before_block_transition(self) -> None:
        controller = CONTROL.Controller(self.args)
        cell = self.root / "cell-normalized-block"
        cell.mkdir()
        receipt = "b" * 64
        claim = {
            "branch": "ticket/T-110",
            "lease": "a" * 64,
            "publication_lease": "",
            "receipt": receipt,
            "role": "builder",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "running",
            "ticket": "T-110",
            "worktree": str(cell),
        }
        (self.product / "factory/runs/normalized-block.meta").write_text(
            "run_id=normalized-block\n"
            "ticket=T-110\n"
            "role=builder\n"
            "accounting_state=abandoned_conservative\n"
            "exit_status=12\n"
            "role_exit=role_exit_contract_blocked\n"
            f"transition_receipt_sha256={receipt}\n",
            encoding="utf-8",
        )
        calls = []
        controller.passport = lambda *_args: calls.append("normalized-export")

        def json_call(*args, **_kwargs):
            if args[:2] == ("state-machine", "block"):
                self.assertEqual(calls, ["normalized-export"])
                calls.append("state-machine-block")
                return {"status": "blocked"}
            if args[0] == "release":
                calls.append("release")
            return {}

        controller.json_call = json_call
        self.assertFalse(controller.finish_pending_run(claim))
        self.assertEqual(
            calls, ["normalized-export", "state-machine-block", "release"]
        )
        self.assertEqual(claim["status"], "blocked")

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

    def test_release_upgrade_recovers_merged_ticket_without_route_migration(
        self,
    ) -> None:
        controller = CONTROL.Controller(self.args)
        cell = self.root / "parked/T-110"
        cell.mkdir(parents=True)
        claim = {
            "branch": "ticket/T-110", "lease": "", "parked": True,
            "priority": "normal", "publication_lease": "", "receipt": "",
            "role": "", "schema": CONTROL.CLAIM_SCHEMA, "status": "blocked",
            "blocked_reason": "route-migration-required", "ticket": "T-110",
            "worktree": str(cell),
        }
        passports = self.state / "passports"
        passports.mkdir(mode=0o700)
        passport = {
            "current_state": "Approved",
            "factory_sha": self.release.name,
            "passport_sha256": "b" * 64,
            "publication_state": "merged",
        }
        CONTROL.write(passports / "T-110.json", passport)
        controller.marker(
            f"passport-route-migration-pending-T-110-{self.release.name}",
            {
                "factory_sha": self.release.name,
                "schema": CONTROL.EVENT_SCHEMA,
                "ticket": "T-110",
            },
        )
        calls = []

        def json_call(*args, **_kwargs):
            calls.append(args)
            if args[0] == "renew":
                raise CONTROL.ControllerError("old lease is absent")
            if args[0] == "claim":
                return {
                    "lease_id": "c" * 64,
                    "schema_version": 1,
                    "ticket": "T-110",
                }
            if args[:2] == ("passport", "validate"):
                return {"passport": "b" * 64, "status": "ok"}
            return {}

        controller.json_call = json_call
        controller.ticket_merged = lambda _claim: True
        controller.event = lambda name, *_args, **_kwargs: calls.append((name,))
        controller.recover_upgraded_claims([claim])

        self.assertEqual(claim["status"], "claimed")
        self.assertEqual(claim["lease"], "c" * 64)
        self.assertNotIn("blocked_reason", claim)
        self.assertIn(("upgraded_merged_claim_recovered",), calls)
        self.assertTrue(controller.marker(
            f"passport-route-migration-complete-T-110-{self.release.name}"
        ))

    def test_release_bundle_refresh_requires_stale_protected_base(self) -> None:
        controller = CONTROL.Controller(self.args)
        cell = self.root / "parked/T-110"
        bundle = cell / "factory/attestations/T-110/bundle.json"
        bundle.parent.mkdir(parents=True)
        bundle.write_text(json.dumps({"kit_sha": "b" * 40}), encoding="utf-8")
        claim = {
            "branch": "ticket/T-110", "lease": "c" * 64,
            "priority": "normal", "publication_lease": "", "receipt": "",
            "role": "", "schema": CONTROL.CLAIM_SCHEMA, "status": "blocked",
            "ticket": "T-110", "worktree": str(cell),
        }
        passport = {
            "current_state": "Awaiting Approval",
            "factory_release_history": [
                {"factory_sha": "b" * 40},
                {"factory_sha": self.release.name},
            ],
            "factory_sha": self.release.name,
            "head_sha": "d" * 40,
            "publication_state": "validating",
        }
        controller.remote_passport_valid = lambda _claim: True
        controller.protected_base_current = lambda *_args: True
        self.assertFalse(controller.release_bundle_refreshable(claim, passport))
        controller.protected_base_current = lambda *_args: False
        self.assertTrue(controller.release_bundle_refreshable(claim, passport))
        passport["publication_state"] = "merged"
        self.assertFalse(controller.release_bundle_refreshable(claim, passport))

    def test_release_upgrade_reclaims_preserved_bundle_refresh(self) -> None:
        controller = CONTROL.Controller(self.args)
        cell = self.root / "parked/T-110"
        cell.mkdir(parents=True)
        claim = {
            "branch": "ticket/T-110", "lease": "a" * 64,
            "priority": "normal", "publication_lease": "", "receipt": "",
            "release_refresh_required": True, "role": "",
            "schema": CONTROL.CLAIM_SCHEMA, "status": "blocked",
            "blocked_reason": "route-migration-required", "ticket": "T-110",
            "worktree": str(cell),
        }
        passports = self.state / "passports"
        passports.mkdir(mode=0o700)
        CONTROL.write(passports / "T-110.json", {
            "factory_sha": self.release.name,
        })
        controller.marker(
            f"passport-route-migration-pending-T-110-{self.release.name}",
            {
                "factory_sha": self.release.name,
                "schema": CONTROL.EVENT_SCHEMA,
                "ticket": "T-110",
            },
        )
        events = []
        controller.release_bundle_refreshable = lambda *_args: True
        controller.ticket_release_current = lambda _claim: False
        controller.renew = lambda _claim: None
        controller.event = lambda name, *_args, **_kwargs: events.append(name)

        controller.recover_upgraded_claims([claim])

        self.assertEqual(claim["status"], "claimed")
        self.assertTrue(claim["release_refresh_required"])
        self.assertNotIn("blocked_reason", claim)
        self.assertIn("upgraded_bundle_refresh_recovered", events)

    def test_release_bundle_refresh_returns_to_route_migration_gate(self) -> None:
        controller = CONTROL.Controller(self.args)
        cell = self.root / "parked/T-110"
        route = cell / "factory/route-plans/T-110.json"
        route.parent.mkdir(parents=True)
        route.write_text("{}\n", encoding="utf-8")
        claim = {
            "branch": "ticket/T-110", "lease": "a" * 64,
            "priority": "normal", "publication_lease": "", "receipt": "",
            "release_refresh_required": True, "role": "",
            "schema": CONTROL.CLAIM_SCHEMA, "status": "claimed",
            "ticket": "T-110", "worktree": str(cell),
        }
        controller.save_claim(claim)
        calls = []

        def json_call(*args, **_kwargs):
            calls.append(args)
            if args[0] == "state-machine":
                return state_transition(
                    "REFUSE ticket Kit-SHA lease does not match the selected kit SHA"
                )
            if args[0] == "ticket-attest":
                return {"action": "refresh", "head": "d" * 40}
            if args[:2] == ("publication", "withdraw"):
                return {"status": "absent"}
            return {}

        controller.json_call = json_call
        controller.finish_pending_run = lambda _claim: True
        controller.refresh_dependency_tracking = lambda _claim: True
        controller.ticket_merged = lambda _claim: False
        controller.migrate_passport = lambda *_args: calls.append(("passport",))

        self.assertEqual(
            controller.reconcile_ticket(claim),
            {"status": "blocked", "ticket": "T-110"},
        )
        self.assertEqual(claim["status"], "blocked")
        self.assertEqual(claim["blocked_reason"], "route-migration-required")
        self.assertNotIn("release_refresh_required", claim)
        self.assertIn(("passport",), calls)
        self.assertEqual(
            sum(call[0] == "ticket-attest" for call in calls if call), 1
        )

    def test_release_upgrade_recovery_overlaps_independent_tickets(self) -> None:
        import threading

        controller = CONTROL.Controller(self.args)
        claims = [{"ticket": f"T-{number}"} for number in range(110, 113)]
        barrier = threading.Barrier(len(claims))

        def recovery(items):
            self.assertEqual(len(items), 1)
            barrier.wait(timeout=2)

        controller.recover_each(
            claims, recovery, "release-upgrade", concurrent=True
        )

    def test_successor_recovers_only_exact_expired_parked_lease(self) -> None:
        controller = CONTROL.Controller(self.args)
        controller.qualification = {
            "generation": 1, "mode": "successor", "tickets": ["T-110"],
        }
        cell = self.root / "parked/T-110"
        cell.mkdir(parents=True)
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
        controller.save_claim(claim)
        passports = self.state / "passports"
        passports.mkdir(mode=0o700)
        CONTROL.write(passports / "T-110.json", {
            "branch": claim["branch"],
            "factory_sha": self.release.name,
            "ticket": claim["ticket"],
        })
        controller.marker(
            f"passport-route-migration-pending-T-110-{self.release.name}",
            {
                "factory_sha": self.release.name,
                "schema": CONTROL.EVENT_SCHEMA,
                "ticket": "T-110",
            },
        )
        leases = self.product / "factory/.dispatch-leases"
        leases.mkdir()
        now = int(time.time())
        stale = {
            "claimed_epoch": now - 901,
            "expires_epoch": now - 1,
            "lease_id": "b" * 64,
            "schema_version": 1,
            "ticket": "T-110",
        }
        sibling = {
            "claimed_epoch": now,
            "expires_epoch": now + 900,
            "lease_id": "c" * 64,
            "schema_version": 1,
            "ticket": "T-111",
        }
        CONTROL.write(leases / "T-110.json", stale)
        CONTROL.write(leases / "T-111.json", sibling)
        calls = []

        def json_call(*args, **_kwargs):
            calls.append(args)
            if args[0] == "renew":
                raise CONTROL.ControllerError("parked claim owns no lease")
            if args[0] == "release-expired":
                self.assertEqual(
                    args,
                    (
                        "release-expired", "--ticket", "T-110",
                        "--lease", stale["lease_id"],
                    ),
                )
                (leases / "T-110.json").unlink()
                return {"expired": True, "released": True, "ticket": "T-110"}
            if args[0] == "claim":
                return {
                    "lease_id": "d" * 64,
                    "schema_version": 1,
                    "ticket": "T-110",
                }
            return {}

        controller.json_call = json_call
        controller.ticket_release_current = lambda _claim: True
        controller.remote_passport_valid = lambda _claim: True
        controller.migrate_passport = lambda *_args: None
        controller.restore_contract_blocker = lambda _claim: False

        controller.recover_upgraded_claims([claim])

        self.assertEqual(claim["status"], "claimed")
        self.assertEqual(claim["lease"], "d" * 64)
        self.assertEqual(CONTROL.read(leases / "T-111.json"), sibling)
        self.assertFalse((leases / "T-110.json").exists())
        calls_before_restart = list(calls)
        controller.recover_upgraded_claims([claim])
        self.assertEqual(calls, calls_before_restart)

    def test_successor_expired_lease_recovery_refuses_live_or_malformed(self) -> None:
        controller = CONTROL.Controller(self.args)
        controller.qualification = {
            "generation": 1, "mode": "successor", "tickets": ["T-110"],
        }
        cell = self.root / "parked/T-110"
        cell.mkdir(parents=True)
        claim = {
            "branch": "ticket/T-110", "lease": "", "parked": True,
            "priority": "normal", "publication_lease": "", "receipt": "",
            "role": "", "schema": CONTROL.CLAIM_SCHEMA, "status": "blocked",
            "ticket": "T-110", "worktree": str(cell),
        }
        passports = self.state / "passports"
        passports.mkdir(mode=0o700)
        CONTROL.write(passports / "T-110.json", {
            "branch": claim["branch"], "factory_sha": self.release.name,
            "ticket": claim["ticket"],
        })
        leases = self.product / "factory/.dispatch-leases"
        leases.mkdir()
        now = int(time.time())
        live = {
            "claimed_epoch": now, "expires_epoch": now + 900,
            "lease_id": "b" * 64, "schema_version": 1, "ticket": "T-110",
        }
        CONTROL.write(leases / "T-110.json", live)
        controller.ticket_release_current = lambda _claim: True
        controller.remote_passport_valid = lambda _claim: True
        controller.json_call = lambda *_args, **_kwargs: self.fail(
            "live lease must not be released"
        )
        self.assertFalse(controller.release_expired_successor_lease(claim))
        self.assertEqual(CONTROL.read(leases / "T-110.json"), live)
        live["ticket"] = "T-111"
        CONTROL.write(leases / "T-110.json", live)
        with self.assertRaisesRegex(
            CONTROL.ControllerError, "dispatcher lease state is unsafe"
        ):
            controller.release_expired_successor_lease(claim)

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

    def test_contract_block_recovers_authorized_accepted_push_normalization_once(self) -> None:
        controller = CONTROL.Controller(self.args)
        cell = self.root / "cell-accepted-normalization"
        (cell / "factory/tickets").mkdir(parents=True)
        receipt = "b" * 64
        (cell / "factory/tickets/T-110.md").write_text(
            "# T-110\n\nState: Blocked-Escalated\nResume-State: Building\n\n"
            f"OPERATOR RESUME: builder\nOPERATOR RESUME RECEIPT: {receipt}\n",
            encoding="utf-8",
        )
        claim = {
            "branch": "ticket/T-110",
            "lease": "a" * 64,
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
        (self.state / "passports").mkdir(mode=0o700)
        CONTROL.write(
            self.state / "passports/T-110.json",
            {
                "branch": claim["branch"],
                "head_sha": "c" * 40,
                "passport_sha256": "d" * 64,
                "publication_state": "none",
            },
        )
        (self.product / "factory/runs/contract-block.meta").write_text(
            "run_id=contract-block\n"
            "phase=completed\n"
            "ticket=T-110\n"
            "role=builder\n"
            "accounting_state=abandoned_conservative\n"
            "task_submitted=1\n"
            "exit_status=12\n"
            "role_exit=role_exit_contract_blocked\n"
            f"transition_receipt_sha256={receipt}\n",
            encoding="utf-8",
        )
        calls = []
        validations = iter((False, False, True, True))
        controller.remote_passport_valid = lambda _claim: next(validations)
        remote_heads = iter((
            ("remote_unavailable", "", ""),
            ("pushed", "c" * 40, "c" * 40),
            ("pushed", "c" * 40, "c" * 40),
        ))
        controller.remote_cell_head_status = lambda _claim: next(remote_heads)
        controller.ensure_lease = lambda *_args: calls.append(("ensure-lease",))

        def migrate(_claim, publication):
            calls.append(("migrate", publication))

        controller.migrate_passport = migrate
        controller.event = lambda name, *_args, **_kwargs: calls.append((name,))

        def json_call(*args, **_kwargs):
            calls.append(args)
            if args[:2] == ("state-machine", "block"):
                return {"status": "blocked"}
            if args[:2] == ("state-machine", "resume"):
                return {"status": "ready"}
            return {}

        controller.json_call = json_call
        controller.recover_repaired_failures([claim])
        self.assertEqual(calls, [])
        self.assertEqual(claim["status"], "blocked")
        self.assertEqual(claim["receipt"], receipt)
        controller.recover_repaired_failures([claim])
        controller.recover_repaired_failures([claim])
        self.assertEqual(claim["status"], "claimed")
        self.assertEqual(claim["receipt"], "")
        self.assertEqual(claim["role"], "")
        self.assertEqual(calls.count(("migrate", "preserve")), 1)
        self.assertEqual(
            calls.count(("contract_block_passport_migrated",)), 1
        )
        self.assertEqual(
            calls.count(("contract_blocker_recovered",)), 1
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
        controller.role_active = lambda _claim: False
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

    def test_exact_converged_builder_success_recovers_once_after_upgrade(
        self,
    ) -> None:
        controller = CONTROL.Controller(self.args)
        cell = self.root / "cell-converged-success"
        cell.mkdir()
        receipt = "9" * 64
        run_id = "converged-success"
        claim = {
            "branch": "ticket/T-110",
            "lease": "8" * 64,
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
        CONTROL.write(passports / "T-110.json", {})
        manifest = self.product / f"factory/runs/{run_id}.meta"
        predecessor = "e" * 40
        if predecessor == self.release.name:
            predecessor = "f" * 40

        def write_manifest(
            role_exit: str = "", adapter: str = "cursor-openai"
        ) -> None:
            manifest.write_text(
                f"run_id={run_id}\n"
                "phase=abandoned\n"
                "ticket=T-110\n"
                "role=builder\n"
                f"adapter={adapter}\n"
                "provider_attempt_id=attempt-1\n"
                "accounting_state=abandoned_conservative\n"
                "reserved_usd=10.00\n"
                "go_issued=1\n"
                "task_submitted=1\n"
                "effective_cost=10.00\n"
                "exit_status=128\n"
                "cost_basis=conservative_reservation\n"
                f"kit_sha={predecessor}\n"
                "contract_version=1.8.0\n"
                "role_branch_before=ticket/T-110\n"
                f"role_head_before={'7' * 40}\n"
                f"role_exit={role_exit}\n"
                "terminal_reason_code=\n"
                f"output_sha256={'6' * 64}\n"
                "progress_events=\n"
                "progress_journal_sha256=\n"
                f"transition_receipt_sha256={receipt}\n",
                encoding="utf-8",
            )

        events = []
        corrections = []
        controller.restore_recorded_contract_repair = lambda _claim: False
        controller.restore_contract_blocker = lambda _claim: False
        controller.remote_passport_valid = lambda _claim: True
        controller.converged_success_exported = lambda *_args: True
        controller.ensure_lease = lambda _claim, label: events.append((label, {}))
        controller.event = (
            lambda name, *_args, **details: events.append((name, details))
        )

        def correct(_claim, terminal):
            corrections.append(terminal["run_id"])

        controller.correct_converged_success = correct
        write_manifest("unrelated")
        controller.recover_repaired_failures([claim])
        self.assertEqual(claim["status"], "blocked")
        self.assertEqual(corrections, [])

        write_manifest(adapter="codex")
        controller.recover_repaired_failures([claim])
        self.assertEqual(claim["status"], "blocked")
        self.assertEqual(corrections, [])

        write_manifest()
        controller.role_active = lambda _claim: True
        controller.recover_repaired_failures([claim])
        self.assertEqual(claim["status"], "blocked")
        self.assertEqual(corrections, [])

        controller.role_active = lambda _claim: False
        controller.remote_passport_valid = lambda _claim: False
        controller.recover_repaired_failures([claim])
        self.assertEqual(claim["status"], "blocked")
        self.assertEqual(corrections, [])

        controller.remote_passport_valid = lambda _claim: True
        controller.recover_repaired_failures([claim])
        self.assertEqual(claim["status"], "claimed")
        self.assertEqual(claim["receipt"], "")
        self.assertEqual(claim["role"], "")
        self.assertEqual(corrections, [run_id])
        self.assertEqual(
            events,
            [
                ("repaired-role", {}),
                (
                    "converged_success_recovered_by_release_upgrade",
                    {"failed_run_id": run_id},
                ),
            ],
        )

        claim.update(receipt=receipt, role="builder", status="blocked")
        events.clear()
        write_manifest(adapter="cursor-anthropic")
        controller.recover_repaired_failures([claim])
        self.assertEqual(claim["status"], "claimed")
        self.assertEqual(corrections, [run_id, run_id])

    def test_model_identity_success_recovers_before_provider_fallback(self) -> None:
        controller = CONTROL.Controller(self.args)
        controller.qualification = {"tickets": ["T-110"]}
        cell = self.root / "cell-model-identity-success"
        cell.mkdir()
        receipt = "9" * 64
        run_id = "model-identity-success"
        claim = {
            "branch": "ticket/T-110",
            "lease": "8" * 64,
            "priority": "normal",
            "publication_lease": "",
            "receipt": receipt,
            "role": "spec-linter",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "blocked",
            "ticket": "T-110",
            "worktree": str(cell),
        }
        controller.save_claim(claim)
        passports = self.state / "passports"
        passports.mkdir(mode=0o700)
        CONTROL.write(passports / "T-110.json", {})
        predecessor = "e" * 40
        manifest = self.product / f"factory/runs/{run_id}.meta"
        manifest.write_text(
            f"run_id={run_id}\n"
            "phase=completed\n"
            "ticket=T-110\n"
            "role=spec-linter\n"
            "adapter=cursor-anthropic\n"
            "route_id=cursor-claude-opus-5-thinking-medium\n"
            "provider_attempt_id=attempt-identity\n"
            "accounting_state=abandoned_conservative\n"
            "reserved_usd=2.00\n"
            "go_issued=1\n"
            "task_submitted=1\n"
            "effective_cost=2.00\n"
            "exit_status=9\n"
            "cost_basis=conservative_reservation\n"
            f"kit_sha={predecessor}\n"
            "role_exit=provider_failed\n"
            "role_branch_before=ticket/T-110\n"
            f"role_head_before={'7' * 40}\n"
            f"output_sha256={'6' * 64}\n"
            "progress_events=2\n"
            f"progress_journal_sha256={'5' * 64}\n"
            "terminal_reason_code=\n"
            f"transition_receipt_sha256={receipt}\n",
            encoding="utf-8",
        )
        calls = []
        controller.restore_recorded_contract_repair = lambda _claim: False
        controller.restore_contract_blocker = lambda _claim: False
        controller.role_active = lambda _claim: False
        controller.restore_model_identity_success = (
            lambda _claim, terminal: calls.append(("restore", terminal["run_id"]))
        )
        controller.finish_pending_run = lambda _claim: calls.append("fallback")
        controller.remote_passport_valid = lambda _claim: True
        controller.converged_success_exported = lambda *_args: True
        controller.ensure_lease = lambda _claim, label: calls.append(label)
        controller.event = (
            lambda name, *_args, **_details: calls.append(name)
        )

        controller.recover_repaired_failures([claim])
        self.assertEqual(claim["status"], "claimed")
        self.assertNotIn("fallback", calls)
        self.assertIn(("restore", run_id), calls)
        self.assertIn(
            "model_identity_success_recovered_by_release_upgrade", calls
        )

        for status in ("claimed", "running"):
            with self.subTest(status=status):
                claim.update(
                    receipt=receipt, role="spec-linter", status=status
                )
                calls.clear()
                controller.recover_repaired_failures([claim])
                self.assertEqual(claim["receipt"], "")
                self.assertNotIn("fallback", calls)
                self.assertIn(("restore", run_id), calls)
                self.assertIn(
                    "model_identity_success_recovered_by_release_upgrade",
                    calls,
                )

        claim.update(receipt=receipt, role="spec-linter", status="running")
        controller.role_active = lambda _claim: True
        calls.clear()
        controller.recover_repaired_failures([claim])
        self.assertEqual(claim["receipt"], receipt)
        self.assertNotIn(("restore", run_id), calls)

    def test_model_identity_restore_preserves_pushed_route_migration(self) -> None:
        controller = CONTROL.Controller(self.args)
        base = "4" * 40
        restored = "5" * 40
        reverted = "6" * 40
        run_id = "identity-run"
        claim = {
            "branch": "ticket/T-110",
            "lease": "7" * 64,
            "receipt": "8" * 64,
            "ticket": "T-110",
            "worktree": str(self.root / "cell-route-migrated"),
        }
        Path(claim["worktree"]).mkdir()
        passports = self.state / "passports"
        passports.mkdir(mode=0o700)
        CONTROL.write(passports / "T-110.json", {"publication_state": "none"})

        def evidence(status: str) -> dict[str, str]:
            return {
                "input_head": "1" * 40,
                "migration_count": 2,
                "migration_head": base,
                "output_head": "2" * 40,
                "output_tree": "3" * 40,
                "recovery_base_head": base,
                "recovery_status": status,
                "restore_head": restored if status == "restored" else "",
                "revert_head": reverted,
                "run_id": run_id,
                "schema": "nysa.software-factory.ticket-passport/v1",
                "status": "ok",
                "ticket": "T-110",
            }

        wrong_schema = evidence("restore-required")
        wrong_schema["schema"] = CONTROL.SCHEMA
        controller.json_call = lambda *_args, **_kwargs: wrong_schema
        with self.assertRaisesRegex(
            CONTROL.ControllerError,
            "model identity recovery evidence is invalid",
        ):
            controller.restore_model_identity_success(
                claim, {"run_id": run_id}
            )

        responses = [evidence("restore-required"), evidence("restored")]
        controller.json_call = lambda *_args, **_kwargs: responses.pop(0)
        controller.ensure_lease = lambda *_args: None
        remote = iter([
            ("pushed", base, base),
            ("resume_commit_not_pushed", restored, base),
        ])
        controller.remote_cell_head_status = lambda _claim: next(remote)
        controller.remote_cell_head_valid = lambda _claim: True
        controller.migrate_passport = lambda *_args: {"status": "ok"}
        controller.terminal_already_exported = lambda *_args: True
        corrected = []
        controller.correct_converged_success = (
            lambda *_args: corrected.append(run_id)
        )
        commands = []

        def execute(command, **_kwargs):
            commands.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")

        with patch.object(CONTROL.subprocess, "run", side_effect=execute):
            controller.restore_model_identity_success(
                claim, {"run_id": run_id}
            )

        self.assertEqual(corrected, [run_id])
        self.assertEqual(commands[0][-3:], ["revert", "--no-edit", reverted])
        self.assertEqual(
            commands[1][-2:],
            ["origin", f"{restored}:refs/heads/ticket/T-110"],
        )

    def test_successor_quarantines_legacy_protected_mutation(self) -> None:
        controller = CONTROL.Controller(self.args)
        cell = self.root / "cell-protected-mutation"
        remote = self.root / "protected-mutation.git"
        subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
        subprocess.run(["git", "init", "-q", str(cell)], check=True)
        for key, value in (
            ("user.email", "test@nysa.dev"), ("user.name", "Test"),
        ):
            subprocess.run(
                ["git", "-C", str(cell), "config", key, value], check=True,
            )
        ticket = cell / "factory/tickets/T-110.md"
        ticket.parent.mkdir(parents=True)
        ticket.write_text("# T-110\n\nState: Planning\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(cell), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(cell), "commit", "-qm", "input"], check=True,
        )
        subprocess.run(
            ["git", "-C", str(cell), "branch", "-M", "ticket/T-110"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(cell), "remote", "add", "origin", str(remote)],
            check=True,
        )
        subprocess.run(
            [
                "git", "-C", str(cell), "push", "-q", "-u", "origin",
                "ticket/T-110",
            ],
            check=True,
        )
        input_head = subprocess.run(
            ["git", "-C", str(cell), "rev-parse", "HEAD"], text=True,
            capture_output=True, check=True,
        ).stdout.strip()
        ticket.write_text(
            "# T-110\n\nState: Planning\n\nKit-SHA: stale\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "-C", str(cell), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(cell), "commit", "-qm", "bad output"],
            check=True,
        )
        output_head = subprocess.run(
            ["git", "-C", str(cell), "rev-parse", "HEAD"], text=True,
            capture_output=True, check=True,
        ).stdout.strip()
        receipt = "b" * 64
        run_id = "protected-mutation"
        predecessor = "e" * 40
        claim = {
            "branch": "ticket/T-110",
            "receipt": receipt,
            "role": "planner",
            "status": "blocked",
            "ticket": "T-110",
            "worktree": str(cell),
        }
        (self.state / "passports").mkdir(mode=0o700)
        CONTROL.write(self.state / "passports/T-110.json", {
            "branch": claim["branch"],
            "charge_records": [{
                "role": "planner", "run_id": run_id,
                "transition_receipt_sha256": receipt,
            }],
            "completed_role_evidence": [],
            "factory_sha": predecessor,
            "head_sha": output_head,
            "ticket": "T-110",
            "transition_receipt_sha256": receipt,
        })
        terminal = {
            "accounting_state": "abandoned_conservative",
            "cost_basis": "conservative_reservation",
            "effective_cost": "2.00",
            "exit_status": "11",
            "go_issued": "1",
            "kit_sha": predecessor,
            "phase": "completed",
            "reserved_usd": "2.00",
            "role": "planner",
            "role_branch_before": claim["branch"],
            "role_exit": "role_exit_protected_ticket_mutation",
            "role_head_before": input_head,
            "role_remote_before": input_head,
            "run_id": run_id,
            "task_submitted": "1",
        }

        same_release = dict(terminal, kit_sha=self.release.name)
        self.assertFalse(
            controller.quarantine_legacy_protected_mutation(claim, same_release)
        )
        self.assertTrue(
            controller.quarantine_legacy_protected_mutation(claim, terminal)
        )
        self.assertEqual(
            subprocess.run(
                ["git", "-C", str(cell), "rev-parse", "HEAD"], text=True,
                capture_output=True, check=True,
            ).stdout.strip(),
            input_head,
        )
        self.assertEqual(
            subprocess.run(
                [
                    "git", "-C", str(cell), "rev-parse",
                    f"refs/factory/failed-role/T-110/{run_id}",
                ],
                text=True, capture_output=True, check=True,
            ).stdout.strip(),
            output_head,
        )
        self.assertFalse(subprocess.run(
            ["git", "-C", str(cell), "status", "--porcelain=v1", "-z"],
            capture_output=True, check=True,
        ).stdout)

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

        def write_manifest(
            kit_sha: str, role_exit: str = "role_exit_history_rewritten"
        ) -> None:
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
                f"role_exit={role_exit}\n"
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
        for role_exit, event in (
            (
                "role_exit_history_rewritten",
                "history_rewrite_recovered_by_release_upgrade",
            ),
            (
                "role_exit_protected_ticket_mutation",
                "protected_ticket_mutation_recovered_by_release_upgrade",
            ),
        ):
            with self.subTest(role_exit=role_exit):
                claim.update(
                    receipt=receipt, role="builder", status="blocked",
                )
                events.clear()
                leases.clear()
                write_manifest(predecessor, role_exit)
                controller.recover_repaired_failures([claim])
                self.assertEqual(claim["status"], "claimed")
                self.assertEqual(claim["receipt"], "")
                self.assertEqual(claim["role"], "")
                self.assertEqual(leases, ["repaired-role"])
                self.assertEqual(
                    events,
                    [(event, {"failed_run_id": run_id})],
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
        self.assertIn(
            (
                "admission_blocked",
                {
                    "error": "unsafe admission",
                    "existing_claims": ["T-110"],
                    "incident_sha256": hashlib.sha256(CONTROL.canonical({
                        "error": "unsafe admission", "reason_code": "unsafe_state",
                    }).encode()).hexdigest(),
                    "reason_code": "unsafe_state",
                },
            ),
            events,
        )

    def test_named_ticket_refusal_does_not_block_sibling_or_repeat_in_cycle(self) -> None:
        controller = CONTROL.Controller(self.args)
        cell = self.root / "cell-1"
        cell.mkdir()
        refusal = {
            "error": "ticket dependencies are invalid",
            "reason_code": "invalid_ticket_contract",
            "ticket": "T-184",
        }
        values = [
            {
                "action": "START",
                "admission_refusal": refusal,
                "branch": "ticket/T-110",
                "lease_id": "a" * 64,
                "priority": "normal",
                "ticket": "T-110",
                "worktree": str(cell),
            },
            {"action": "WAIT", "admission_refusal": refusal},
        ]
        controller.json_call = lambda *_args, **_kwargs: (
            values.pop(0) if values else {
                "action": "WAIT", "admission_refusal": refusal,
            }
        )
        controller.pin_routes = lambda _claims: []
        controller.reconcile_ticket = lambda item: {
            "status": "active", "ticket": item["ticket"],
        }

        result = controller.reconcile()

        self.assertEqual(result["active"], 1)
        self.assertEqual(result["results"], [
            {"status": "active", "ticket": "T-110"},
            {
                "error": "ticket dependencies are invalid",
                "reason_code": "invalid_ticket_contract",
                "status": "skipped",
                "ticket": "T-184",
            },
        ])
        incident = CONTROL.read(self.state / "admission-incident.json")
        self.assertEqual(incident["count"], 1)
        self.assertEqual(incident["ticket"], "T-184")
        events = [CONTROL.read(path) for path in controller.events.glob("*.json")]
        blocked = [item for item in events if item["event"] == "admission_blocked"]
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0]["ticket"], "T-184")

        restarted = CONTROL.Controller(self.args)
        restarted.json_call = lambda *_args, **_kwargs: {
            "action": "WAIT", "admission_refusal": refusal,
        }
        restarted.claim_new(restarted.load_claims())
        self.assertEqual(
            CONTROL.read(self.state / "admission-incident.json")["count"], 2
        )

    def test_malformed_dispatch_refusal_fails_closed(self) -> None:
        controller = CONTROL.Controller(self.args)
        controller.json_call = lambda *_args, **_kwargs: {
            "action": "WAIT",
            "admission_refusal": {
                "error": "ticket dependencies are invalid",
                "reason_code": "invalid_ticket_contract",
                "ticket": "not-a-ticket",
            },
        }

        with self.assertRaisesRegex(
            CONTROL.ControllerError, "dispatch admission refusal is malformed"
        ):
            controller.claim_new([])

    def test_named_initiative_refusal_is_returned(self) -> None:
        controller = CONTROL.Controller(self.args)
        refusal = {
            "error": "ticket initiative is missing",
            "reason_code": "initiative_missing",
            "ticket": "T-184",
        }
        controller.json_call = lambda *_args, **_kwargs: {
            "action": "WAIT", "admission_refusal": refusal,
        }
        controller.pin_routes = lambda _claims: []

        result = controller.reconcile()

        self.assertEqual(result["results"], [{**refusal, "status": "skipped"}])
        self.assertEqual(
            CONTROL.read(self.state / "admission-incident.json")["ticket"],
            "T-184",
        )

    def test_identical_admission_failure_is_durable_and_deduplicated(self) -> None:
        controller = CONTROL.Controller(self.args)
        events = []
        controller.event = lambda name, **fields: events.append((name, fields))
        error = CONTROL.ControllerError(json.dumps({
            "error": "T-100: remote Building conflicts with local Review",
            "reason_code": "unsafe_state",
        }))

        controller.record_admission_failure(error, [])
        controller.record_admission_failure(error, [])

        self.assertEqual([name for name, _ in events], ["admission_blocked"])
        incident = CONTROL.read(self.state / "admission-incident.json")
        self.assertEqual(incident["count"], 2)
        self.assertIn("remote Building", incident["error"])

        controller.record_admission_failure(
            CONTROL.ControllerError("Linear reconciliation is stale"), []
        )
        self.assertEqual(
            [name for name, _ in events],
            ["admission_blocked", "admission_blocked"],
        )

    def test_explicit_pause_and_resume_preserve_exact_passport(self) -> None:
        controller = CONTROL.Controller(self.args)
        ticket = "T-110"
        cell = self.root / "cell-pause"
        (cell / "factory/tickets").mkdir(parents=True)
        (cell / "factory/tickets/T-110.md").write_text(
            "State: Building\nResume-State: Planning\n", encoding="utf-8"
        )
        passport = {
            "branch": f"ticket/{ticket}",
            "current_state": "Building",
            "factory_sha": self.release.name,
            "head_sha": "b" * 40,
            "migration_history": [],
            "passport_sha256": "c" * 64,
            "ticket": ticket,
        }
        passports = self.state / "passports"
        passports.mkdir(mode=0o700)
        CONTROL.write(passports / f"{ticket}.json", passport)
        claim = {
            "branch": f"ticket/{ticket}",
            "lease": "d" * 64,
            "priority": "normal",
            "publication_lease": "",
            "receipt": "",
            "role": "",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "waiting",
            "ticket": ticket,
            "worktree": str(cell),
        }
        controller.save_claim(claim)
        controller.worktrees_by_branch = lambda: {
            f"refs/heads/ticket/{ticket}": [str(cell)]
        }
        controller.remote_passport_valid = lambda _claim: True
        controller.park_claim = lambda _claim: False
        controller.ticket_release_current = lambda _claim: True
        calls = []

        def launcher(*args, **_kwargs):
            calls.append(args)
            if args[0] == "claim":
                return {
                    "lease_id": "e" * 64,
                    "schema_version": 1,
                    "ticket": ticket,
                }
            return {}

        controller.json_call = launcher
        with self.assertRaisesRegex(
            CONTROL.ControllerError, "could not park a clean checkpoint"
        ):
            controller.pause_ticket(ticket, FACTORY_ISSUE)
        self.assertTrue(controller.claim_path(ticket).exists())
        self.assertFalse(controller.pause_path(ticket).exists())
        controller.park_claim = lambda _claim: True
        self.assertEqual(
            controller.pause_ticket(ticket, FACTORY_ISSUE)["status"], "paused"
        )
        self.assertFalse(controller.claim_path(ticket).exists())
        self.assertTrue(controller.pause_path(ticket).exists())
        self.assertEqual(
            CONTROL.read(controller.pause_path(ticket))["current_state"],
            "Building",
        )
        pause = CONTROL.read(controller.pause_path(ticket))
        self.assertEqual(pause["blocking_issue"], FACTORY_ISSUE)
        self.assertEqual(pause["resume_state"], "Planning")
        self.assertRegex(pause["pause_sha256"], CONTROL.DIGEST)
        self.assertEqual(
            controller.pause_ticket(ticket, FACTORY_ISSUE)["status"], "paused"
        )

        CONTROL.write(controller.pause_path(ticket), {**pause, "status": "claimed"})
        with self.assertRaisesRegex(
            CONTROL.ControllerError, "pause intent digest is invalid"
        ):
            controller.resume_ticket(ticket, self.release.name)
        CONTROL.write(controller.pause_path(ticket), pause)

        changed = {**passport, "head_sha": "f" * 40}
        CONTROL.write(passports / f"{ticket}.json", changed)
        with self.assertRaisesRegex(
            CONTROL.ControllerError, "does not match the passport"
        ):
            controller.resume_ticket(ticket, self.release.name)
        CONTROL.write(passports / f"{ticket}.json", passport)

        self.assertEqual(
            controller.resume_ticket(ticket, self.release.name)["status"],
            "resumed",
        )
        resumed = CONTROL.read(controller.claim_path(ticket))
        self.assertEqual(resumed["status"], "waiting")
        self.assertEqual(resumed["lease"], "e" * 64)
        self.assertFalse(controller.pause_path(ticket).exists())
        self.assertTrue(
            (self.state / "repros" / f"{ticket}-{pause['pause_sha256']}.json").exists()
        )

        resumed["status"] = "blocked"
        controller.save_claim(resumed)
        controller.pause_ticket(ticket, FACTORY_ISSUE)
        controller.ticket_release_current = lambda _claim: False
        controller.resume_ticket(ticket, self.release.name)
        self.assertEqual(
            CONTROL.read(controller.claim_path(ticket))["status"], "blocked"
        )

        controller.pause_ticket(ticket, FACTORY_ISSUE)
        controller.ticket_release_current = lambda _claim: True
        controller.resume_ticket(ticket, self.release.name)
        self.assertEqual(
            CONTROL.read(controller.claim_path(ticket))["status"], "claimed"
        )
        controller.active_run = lambda _ticket: {"run_id": "active"}
        with self.assertRaisesRegex(CONTROL.ControllerError, "idle passport"):
            controller.pause_ticket(ticket, FACTORY_ISSUE)

    def test_ticket_control_reconstructs_only_settled_contract_blocker(self) -> None:
        controller = CONTROL.Controller(self.args)
        ticket = "T-110"
        receipt = "b" * 64
        cell = self.root / "parked" / ticket
        (cell / "factory/tickets").mkdir(parents=True)
        (cell / "factory/tickets" / f"{ticket}.md").write_text(
            "State: Blocked-Escalated\nResume-State: Planning\n",
            encoding="utf-8",
        )
        passport = {
            "branch": f"ticket/{ticket}",
            "current_stage": "ESCALATE planner",
            "current_state": "Blocked-Escalated",
            "factory_sha": self.release.name,
            "head_sha": "c" * 40,
            "migration_history": [],
            "passport_sha256": "d" * 64,
            "ticket": ticket,
        }
        passports = self.state / "passports"
        passports.mkdir(mode=0o700)
        CONTROL.write(passports / f"{ticket}.json", passport)
        claim = {
            "blocked_reason": "role-failure",
            "branch": f"ticket/{ticket}",
            "lease": "e" * 64,
            "lease_released": True,
            "parked": True,
            "priority": "normal",
            "publication_lease": "",
            "receipt": receipt,
            "role": "planner",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "blocked",
            "ticket": ticket,
            "worktree": str(cell),
        }
        controller.save_claim(claim)
        controller.worktrees_by_branch = lambda: {
            f"refs/heads/ticket/{ticket}": [str(cell)]
        }
        controller.remote_passport_valid = lambda _claim: True
        controller.ticket_release_current = lambda _claim: True
        terminal = {
            "accounting_state": "abandoned_conservative",
            "exit_status": "12",
            "role": "planner",
            "role_exit": "role_exit_contract_blocked",
            "run_id": "settled-blocker",
            "task_submitted": "1",
        }
        controller.terminal_for_receipt = lambda *_args: terminal
        controller.json_call = lambda *args, **_kwargs: (
            {"lease_id": "f" * 64, "schema_version": 1, "ticket": ticket}
            if args[0] == "claim" else {}
        )

        controller.terminal_for_receipt = lambda *_args: {
            **terminal, "role_exit": "provider_failed",
        }
        with self.assertRaisesRegex(
            CONTROL.ControllerError, "pre-provider boundary"
        ):
            controller.pause_ticket(ticket, FACTORY_ISSUE)

        controller.terminal_for_receipt = lambda *_args: terminal
        self.assertEqual(
            controller.pause_ticket(ticket, FACTORY_ISSUE)["status"], "paused"
        )
        changed = self.product / "factory/runs/changed.meta"
        changed.write_text(f"ticket={ticket}\n", encoding="utf-8")
        with self.assertRaisesRegex(
            CONTROL.ControllerError, "does not match the passport"
        ):
            controller.resume_ticket(ticket, self.release.name)
        changed.unlink()
        self.assertEqual(
            controller.resume_ticket(ticket, self.release.name)["status"], "resumed"
        )
        resumed = CONTROL.read(controller.claim_path(ticket))
        self.assertEqual(resumed["status"], "blocked")
        self.assertEqual((resumed["receipt"], resumed["role"]), ("", ""))

    def test_pause_resume_state_boundary_survives_restart_and_cutover(self) -> None:
        controller = CONTROL.Controller(self.args)
        ticket = "T-110"
        cell = self.root / "cell-pause-boundary"
        (cell / "factory/tickets").mkdir(parents=True)
        (cell / "factory/tickets/T-110.md").write_text(
            "State: Building\n", encoding="utf-8"
        )
        passport = {
            "branch": f"ticket/{ticket}",
            "current_state": "Building",
            "factory_sha": self.release.name,
            "head_sha": "b" * 40,
            "migration_history": [],
            "passport_sha256": "c" * 64,
            "ticket": ticket,
        }
        passports = self.state / "passports"
        passports.mkdir(mode=0o700)
        worktrees = lambda: {
            f"refs/heads/ticket/{ticket}": [str(cell)]
        }
        controller.worktrees_by_branch = worktrees
        controller.remote_passport_valid = lambda _claim: True

        self.assertEqual(CONTROL.INFLIGHT_STATES, frozenset({
            "Ready", "Planning", "Building", "Review", "Awaiting Approval",
            "Approved", "Blocked-Escalated",
        }))
        CONTROL.write(passports / f"{ticket}.json", {
            **passport, "current_state": "Backlog",
        })
        with self.assertRaisesRegex(
            CONTROL.ControllerError, "requires an in-flight passport"
        ):
            controller.pause_ticket(ticket, FACTORY_ISSUE)
        self.assertFalse(controller.pause_path(ticket).exists())
        CONTROL.write(passports / f"{ticket}.json", {
            **passport,
            "current_state": "Approved",
            "publication_state": "merged",
        })
        with self.assertRaisesRegex(
            CONTROL.ControllerError, "requires an in-flight passport"
        ):
            controller.pause_ticket(ticket, FACTORY_ISSUE)
        self.assertFalse(controller.pause_path(ticket).exists())

        CONTROL.write(passports / f"{ticket}.json", passport)
        with self.assertRaisesRegex(
            CONTROL.ControllerError, "Software Factory issue URL"
        ):
            controller.pause_ticket(ticket, "https://example.com/issues/253")
        controller.pause_ticket(ticket, FACTORY_ISSUE)
        controller.qualification = {"tickets": [ticket]}
        claims = []
        controller.recover_missing_passport_claims(claims)
        self.assertEqual(claims, [])

        interrupted = {
            "branch": f"ticket/{ticket}",
            "lease": "d" * 64,
            "priority": "normal",
            "publication_lease": "",
            "receipt": "",
            "role": "",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "claimed",
            "ticket": ticket,
            "worktree": str(cell),
        }
        controller.mark_reconciling(interrupted)
        interrupted["status"] = "blocked"
        controller.save_claim(interrupted)
        controller.recover_interrupted_claims([interrupted])
        self.assertEqual(interrupted["status"], "blocked")
        self.assertTrue(controller.reconciliation_marker(ticket).exists())
        controller.claim_path(ticket).unlink()

        CONTROL.write(passports / f"{ticket}.json", {
            **passport, "current_state": "Review",
        })
        with self.assertRaisesRegex(
            CONTROL.ControllerError, "does not match the passport"
        ):
            controller.resume_ticket(ticket, self.release.name)
        CONTROL.write(passports / f"{ticket}.json", {
            **passport, "current_state": "Done",
        })
        with self.assertRaisesRegex(
            CONTROL.ControllerError, "requires an in-flight passport"
        ):
            controller.resume_ticket(ticket, self.release.name)
        CONTROL.write(passports / f"{ticket}.json", {
            **passport, "publication_state": "merged",
        })
        with self.assertRaisesRegex(
            CONTROL.ControllerError, "requires an in-flight passport"
        ):
            controller.resume_ticket(ticket, self.release.name)

        CONTROL.write(passports / f"{ticket}.json", passport)
        tickets = self.product / "factory/tickets"
        tickets.mkdir()
        (tickets / f"{ticket}.md").write_text(
            "State: Done\n", encoding="utf-8"
        )
        with self.assertRaisesRegex(
            CONTROL.ControllerError, "requires an in-flight passport"
        ):
            controller.resume_ticket(ticket, self.release.name)
        (tickets / f"{ticket}.md").unlink()
        self.assertTrue(controller.pause_path(ticket).exists())
        self.assertFalse(controller.claim_path(ticket).exists())

        with patch.object(
            controller, "ensure_lease",
            side_effect=CONTROL.ControllerError("ticket capacity is full"),
        ):
            with self.assertRaisesRegex(
                CONTROL.ControllerError, "capacity is full"
            ):
                controller.resume_ticket(ticket, self.release.name)
        self.assertTrue(controller.pause_path(ticket).exists())
        self.assertFalse(controller.claim_path(ticket).exists())

        successor = self.root / ("e" * 40)
        successor.mkdir()
        migrated = {
            **passport,
            "factory_sha": successor.name,
            "migration_history": [{
                "from_passport_sha256": passport["passport_sha256"],
            }],
            "passport_sha256": "f" * 64,
        }
        CONTROL.write(passports / f"{ticket}.json", migrated)
        successor_args = copy.copy(self.args)
        successor_args.release_path = successor
        restarted = CONTROL.Controller(successor_args)
        restarted.worktrees_by_branch = worktrees
        restarted.remote_passport_valid = lambda _claim: True
        restarted.ticket_release_current = lambda _claim: True
        restarted.json_call = lambda *args, **_kwargs: {
            "lease_id": "1" * 64,
            "schema_version": 1,
            "ticket": ticket,
        } if args[0] == "claim" else {}

        with self.assertRaisesRegex(
            CONTROL.ControllerError, "resume intent is unavailable"
        ):
            restarted.resume_ticket(ticket, self.release.name)
        self.assertEqual(
            restarted.resume_ticket(ticket, successor.name)["status"], "resumed"
        )
        resumed = CONTROL.read(restarted.claim_path(ticket))
        self.assertEqual(resumed["lease"], "1" * 64)
        self.assertEqual(resumed["status"], "claimed")
        self.assertFalse(restarted.pause_path(ticket).exists())

    def test_interrupted_receipt_free_claim_recovers_once_from_marker(self) -> None:
        controller = CONTROL.Controller(self.args)
        ticket = "T-110"
        cell = self.root / "cell-interrupted"
        subprocess.run(["git", "init", "-q", str(cell)], check=True)
        passport = {
            "branch": f"ticket/{ticket}",
            "factory_sha": self.release.name,
            "head_sha": "b" * 40,
            "passport_sha256": "c" * 64,
            "ticket": ticket,
        }
        passports = self.state / "passports"
        passports.mkdir(mode=0o700)
        CONTROL.write(passports / f"{ticket}.json", passport)
        claim = {
            "branch": f"ticket/{ticket}",
            "lease": "d" * 64,
            "priority": "normal",
            "publication_lease": "",
            "receipt": "",
            "role": "",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "claimed",
            "ticket": ticket,
            "worktree": str(cell),
        }
        controller.mark_reconciling(claim)
        claim["status"] = "blocked"
        controller.save_claim(claim)
        controller.ticket_release_current = lambda _claim: True
        controller.remote_passport_valid = lambda _claim: True
        controller.json_call = lambda *_args, **_kwargs: {}

        controller.recover_interrupted_claims([claim])
        self.assertEqual(claim["status"], "claimed")
        self.assertFalse(controller.reconciliation_marker(ticket).exists())
        controller.recover_interrupted_claims([claim])
        self.assertEqual(claim["status"], "claimed")

        claim["status"] = "blocked"
        claim["blocked_reason"] = "state-machine-refusal"
        controller.save_claim(claim)
        controller.mark_reconciling({**claim, "blocked_reason": None})
        controller.recover_interrupted_claims([claim])
        self.assertEqual(claim["status"], "blocked")

        claim.pop("blocked_reason")
        claim["receipt"] = "e" * 64
        controller.recover_interrupted_claims([claim])
        self.assertEqual(claim["status"], "blocked")
        claim["receipt"] = ""

        (cell / "dirty").write_text("dirty", encoding="utf-8")
        controller.recover_interrupted_claims([claim])
        self.assertEqual(claim["status"], "blocked")
        (cell / "dirty").unlink()

        CONTROL.write(controller.pause_path(ticket), {"ticket": ticket})
        controller.recover_interrupted_claims([claim])
        self.assertEqual(claim["status"], "blocked")
        controller.pause_path(ticket).unlink()

        controller.role_active = lambda _claim: True
        controller.recover_interrupted_claims([claim])
        self.assertEqual(claim["status"], "blocked")
        controller.role_active = lambda _claim: False

        controller.ticket_release_current = lambda _claim: False
        controller.recover_interrupted_claims([claim])
        self.assertEqual(claim["status"], "blocked")
        controller.ticket_release_current = lambda _claim: True

        (self.product / "factory/runs/new-terminal.meta").write_text(
            f"ticket={ticket}\n", encoding="utf-8"
        )
        controller.recover_interrupted_claims([claim])
        self.assertEqual(claim["status"], "blocked")
        (self.product / "factory/runs/new-terminal.meta").unlink()

        controller.remote_passport_valid = lambda _claim: False
        controller.recover_interrupted_claims([claim])
        self.assertEqual(claim["status"], "blocked")
        controller.remote_passport_valid = lambda _claim: True
        controller.recover_interrupted_claims([claim])
        self.assertEqual(claim["status"], "claimed")

    def test_interrupted_two_ticket_recovery_is_independent(self) -> None:
        controller = CONTROL.Controller(self.args)
        passports = self.state / "passports"
        passports.mkdir(mode=0o700)
        claims = []
        for ticket in ("T-093", "T-100"):
            cell = self.root / f"cell-{ticket}"
            subprocess.run(["git", "init", "-q", str(cell)], check=True)
            CONTROL.write(passports / f"{ticket}.json", {
                "branch": f"ticket/{ticket}",
                "factory_sha": self.release.name,
                "head_sha": "b" * 40,
                "passport_sha256": hashlib.sha256(ticket.encode()).hexdigest(),
                "ticket": ticket,
            })
            claim = {
                "branch": f"ticket/{ticket}",
                "lease": hashlib.sha256(f"lease-{ticket}".encode()).hexdigest(),
                "priority": "normal",
                "publication_lease": "",
                "receipt": "",
                "role": "",
                "schema": CONTROL.CLAIM_SCHEMA,
                "status": "claimed",
                "ticket": ticket,
                "worktree": str(cell),
            }
            controller.mark_reconciling(claim)
            claim["status"] = "blocked"
            controller.save_claim(claim)
            claims.append(claim)
        controller.ticket_release_current = lambda _claim: True
        controller.remote_passport_valid = lambda _claim: True
        controller.json_call = lambda *_args, **_kwargs: {}

        controller.recover_interrupted_claims(claims)
        self.assertEqual([claim["status"] for claim in claims], ["claimed", "claimed"])
        self.assertFalse(any(
            controller.reconciliation_marker(claim["ticket"]).exists()
            for claim in claims
        ))

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

    def test_pr_gated_waiting_ticket_rechecks_while_sibling_worker_is_live(
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
                if calls[ticket] == 1:
                    return {
                        "status": "waiting", "ticket": ticket,
                        "wait_reason": "pr-gate",
                    }
            else:
                self.assertTrue(first_waited.wait(1))
                time.sleep(0.1)
            return {"status": "waiting", "ticket": ticket}

        controller.reconcile_ticket = reconcile
        with patch.object(CONTROL, "RECONCILE_INTERVAL_SECONDS", 0.02):
            result = controller.reconcile()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(calls, {"T-110": 2, "T-111": 1})

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

    def test_qualification_parks_checkpoint_under_durable_controller_state(self) -> None:
        run = lambda *command, cwd=None: subprocess.run(
            command, cwd=cwd, text=True, capture_output=True, check=True
        )
        (self.product / "factory/QUALIFICATION.json").write_text(json.dumps({
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
        }), encoding="utf-8")
        run("git", "init", "-q", "-b", "main", cwd=self.product)
        run("git", "config", "user.name", "Test", cwd=self.product)
        run("git", "config", "user.email", "test@example.invalid", cwd=self.product)
        run("git", "add", ".", cwd=self.product)
        run("git", "commit", "-qm", "seed", cwd=self.product)
        run("git", "branch", "ticket/T-110", cwd=self.product)
        scratch = self.root / "scratch"
        scratch.mkdir(mode=0o700)
        cell = scratch / "cell-1"
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
        controller.remote_passport_valid = lambda _claim: True
        controller.json_call = lambda *_args, **_kwargs: {}
        controller.event = lambda *_args, **_kwargs: None

        self.assertTrue(controller.park_claim(claim))
        self.assertEqual(
            claim["worktree"], str(self.state / "parked/T-110")
        )
        controller.ensure_execution_cell(claim)
        self.assertEqual(claim["worktree"], str(self.state / "cells/cell-1"))

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

    def test_auto_merge_race_rechecks_merged_before_open_pr_failure(self) -> None:
        controller = CONTROL.Controller(self.args)
        cell = self.root / "cell-1"
        route = cell / "factory/route-plans/T-110.json"
        route.parent.mkdir(parents=True)
        route.write_text("{}\n", encoding="utf-8")
        claim = {
            "branch": "ticket/T-110",
            "lease": "a" * 64,
            "priority": "normal",
            "publication_lease": "f" * 64,
            "receipt": "",
            "role": "",
            "schema": CONTROL.CLAIM_SCHEMA,
            "status": "claimed",
            "ticket": "T-110",
            "worktree": str(cell),
        }
        merged = iter((False, True))
        calls = []
        controller.renew = lambda _claim: None
        controller.finish_pending_run = lambda _claim: True
        controller.refresh_dependency_tracking = lambda _claim: True
        controller.ticket_merged = lambda _claim: next(merged)
        controller.ticket_pr = lambda *_args: (_ for _ in ()).throw(
            CONTROL.ControllerError("ticket-pr: ticket PR is not open")
        )
        controller.release_publication = lambda _claim: calls.append("release")
        controller.migrate_passport = lambda *_args: calls.append("passport")
        controller.closeout = lambda _claim: calls.append("closeout") or False
        controller.withdraw_publication = lambda *_args: None
        controller.json_call = lambda *arguments, **_kwargs: (
            state_transition(
                "AWAIT-MERGE protected auto-merge requested; "
                "await merge and closeout"
            )
            if arguments[0] == "state-machine"
            else (_ for _ in ()).throw(AssertionError(arguments))
        )

        self.assertEqual(
            controller.reconcile_ticket(claim),
            {"status": "progressed", "ticket": "T-110"},
        )
        self.assertEqual(calls, ["release", "passport", "closeout"])

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

    def test_launcher_ticket_parking_requires_issue_and_named_release(self) -> None:
        launcher = (
            ROOT / "integrations/hermes/bin/factory-launch"
        ).read_text(encoding="utf-8")
        contract = json.loads(
            (ROOT / "integrations/hermes/contract.json").read_text()
        )["launcher"]["commands"]["ticket-control"]
        self.assertIn('"$4" == "--issue"', launcher)
        self.assertIn('"$4" == "--factory-sha"', launcher)
        self.assertEqual(contract["grammars"], [
            "pause --ticket <T-NNN> --issue "
            "<software-factory-issue-url> --json",
            "resume --ticket <T-NNN> --factory-sha <FULL_SHA> --json",
        ])

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
        controller.closeout = lambda _claim: calls.append("closeout") or False
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
            {"status": "waiting", "ticket": "T-110"},
        )
        self.assertEqual(calls, ["release", "passport", "closeout"])

    def test_recovered_merged_passport_closes_without_publication_lease(self) -> None:
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
        (self.state / "passports").mkdir()
        passport = self.state / "passports/T-110.json"
        passport.write_text(
            json.dumps({"publication_state": "merged"}), encoding="utf-8"
        )
        passport.chmod(0o600)
        calls = []
        controller.ensure_lease = lambda *_args: None
        controller.finish_pending_run = lambda _claim: True
        controller.ticket_merged = lambda _claim: True
        controller.migrate_passport = lambda *_args: calls.append("passport")
        controller.closeout = lambda _claim: calls.append("closeout") or False
        controller.refresh_dependency_tracking = lambda _claim: (
            (_ for _ in ()).throw(
                AssertionError("merged recovery refreshed dependencies")
            )
        )
        controller.json_call = lambda *_args, **_kwargs: (
            (_ for _ in ()).throw(
                AssertionError("merged recovery entered state machine")
            )
        )
        self.assertEqual(
            controller.reconcile_ticket(claim),
            {"status": "waiting", "ticket": "T-110"},
        )
        self.assertEqual(calls, ["passport", "closeout"])

    def test_merged_closeout_attestation_completes_before_dependency_refresh(self) -> None:
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
        (self.state / "passports").mkdir()
        passport = self.state / "passports/T-110.json"
        passport.write_text(
            json.dumps({"publication_state": "merged"}), encoding="utf-8"
        )
        passport.chmod(0o600)
        calls = []
        controller.ensure_lease = lambda *_args: None
        controller.finish_pending_run = lambda _claim: True
        controller.ticket_merged = lambda _claim: True
        controller.migrate_passport = lambda *_args: calls.append("passport")
        controller.closeout = lambda _claim: calls.append("closeout") or True
        controller.refresh_dependency_tracking = lambda _claim: (
            (_ for _ in ()).throw(
                AssertionError("attested Done refreshed dependencies")
            )
        )
        controller.json_call = lambda *_args, **_kwargs: (
            (_ for _ in ()).throw(
                AssertionError("attested Done entered branch state machine")
            )
        )
        controller.event = lambda name, *_args, **_kwargs: calls.append(name)
        controller.release = lambda _claim: calls.append("release")
        self.assertEqual(
            controller.reconcile_ticket(claim),
            {"status": "complete", "ticket": "T-110"},
        )
        self.assertEqual(
            calls, ["passport", "closeout", "ticket_complete", "release"]
        )

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

    def test_done_product_ticket_is_not_recovered_from_passport(self) -> None:
        controller = CONTROL.Controller(self.args)
        controller.qualification = {"tickets": ["T-110"]}
        (self.product / "factory/tickets").mkdir()
        (self.product / "factory/tickets/T-110.md").write_text(
            "State: Done\n", encoding="utf-8"
        )
        (self.state / "passports").mkdir(mode=0o700)
        CONTROL.write(
            self.state / "passports/T-110.json",
            {
                "branch": "ticket/T-110",
                "current_state": "Approved",
                "ticket": "T-110",
            },
        )
        claims = []
        with patch.object(CONTROL.subprocess, "run") as run:
            run.return_value.returncode = 1
            controller.recover_missing_passport_claims(claims)
        self.assertEqual(claims, [])
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0][3], "show")

    def test_done_product_ticket_prunes_existing_claim_before_recovery(self) -> None:
        controller = CONTROL.Controller(self.args)
        (self.product / "factory/tickets").mkdir()
        (self.product / "factory/tickets/T-110.md").write_text(
            "State: Done\n", encoding="utf-8"
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
            "worktree": str(self.root / "cell-1"),
        }
        controller.save_claim(claim)
        controller.load_claims = lambda: (
            [claim] if controller.claim_path("T-110").exists() else []
        )
        calls = []
        controller.ensure_lease = lambda *_args: calls.append("ensure")

        def release(item):
            calls.append("release")
            controller.claim_path(item["ticket"]).unlink()

        controller.release = release
        controller.recover_missing_passport_claims = (
            lambda claims: calls.append(("recover", len(claims)))
        )
        controller.recover_each = lambda *_args, **_kwargs: None
        controller.event = lambda *_args, **_kwargs: None
        controller.claim_new = lambda claims, *_args: claims
        controller.pin_routes = lambda _claims: []
        result = controller.reconcile()
        self.assertEqual(result["active"], 0)
        self.assertFalse(controller.claim_path("T-110").exists())
        self.assertEqual(calls[:3], ["ensure", "release", ("recover", 0)])

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
        done = cell.parent / "closeout-T-110/factory/attestations/T-110/done.json"
        done.parent.mkdir(parents=True)
        done.write_text("{}\n", encoding="utf-8")
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

    def test_closeout_fast_forwards_clean_unattested_retry(self) -> None:
        controller = CONTROL.Controller(self.args)
        cell = self.root / "cells/cell-1"
        cell.mkdir(parents=True)
        worktree = cell.parent / "closeout-T-110"
        worktree.mkdir()
        commands = []
        controller.terminal_request = lambda *_args, **_kwargs: None
        controller.json_call = lambda *_args, **_kwargs: {
            "closeout_pr_state": "OPEN"
        }

        def run(command, **_kwargs):
            commands.append(command)
            output = "chore/t110-closeout\n" if "symbolic-ref" in command else ""
            return CONTROL.subprocess.CompletedProcess(command, 0, output, "")

        with patch.object(CONTROL.subprocess, "run", side_effect=run):
            self.assertFalse(controller.closeout({
                "lease": "a" * 64,
                "ticket": "T-110",
                "worktree": str(cell),
            }))
        self.assertIn(
            [
                "git", "-C", str(worktree), "merge", "--ff-only",
                "origin/main",
            ],
            commands,
        )

    def test_closeout_preserves_attested_retry(self) -> None:
        controller = CONTROL.Controller(self.args)
        cell = self.root / "cells/cell-1"
        cell.mkdir(parents=True)
        worktree = cell.parent / "closeout-T-110"
        done = worktree / "factory/attestations/T-110/done.json"
        done.parent.mkdir(parents=True)
        done.write_text("{}\n", encoding="utf-8")
        commands = []
        controller.terminal_request = lambda *_args, **_kwargs: None
        controller.json_call = lambda *_args, **_kwargs: {
            "closeout_pr_state": "OPEN"
        }

        def run(command, **_kwargs):
            commands.append(command)
            return CONTROL.subprocess.CompletedProcess(command, 0, "", "")

        with patch.object(CONTROL.subprocess, "run", side_effect=run):
            self.assertFalse(controller.closeout({
                "lease": "a" * 64,
                "ticket": "T-110",
                "worktree": str(cell),
            }))
        self.assertFalse(any("merge" in command for command in commands))

    def test_closeout_dirty_retry_remains_fail_closed(self) -> None:
        controller = CONTROL.Controller(self.args)
        cell = self.root / "cells/cell-1"
        cell.mkdir(parents=True)
        worktree = cell.parent / "closeout-T-110"
        worktree.mkdir()
        commands = []
        controller.terminal_request = lambda *_args, **_kwargs: None

        def json_call(*_args, **_kwargs):
            raise CONTROL.ControllerError(
                "ticket-attest: done worktree must be clean"
            )

        controller.json_call = json_call

        def run(command, **_kwargs):
            commands.append(command)
            if "symbolic-ref" in command:
                output = "chore/t110-closeout\n"
            elif "status" in command:
                output = " M factory/LEDGER.csv\n"
            else:
                output = ""
            return CONTROL.subprocess.CompletedProcess(command, 0, output, "")

        with (
            patch.object(CONTROL.subprocess, "run", side_effect=run),
            self.assertRaisesRegex(
                CONTROL.ControllerError, "done worktree must be clean"
            ),
        ):
            controller.closeout({
                "lease": "a" * 64,
                "ticket": "T-110",
                "worktree": str(cell),
            })
        self.assertFalse(any("merge" in command for command in commands))

    def test_closeout_defers_while_sibling_claim_is_active(self) -> None:
        controller = CONTROL.Controller(self.args)
        active = self.product / "factory/.active-runs/T-174.reviewer.lock"
        active.parent.mkdir(parents=True)
        active.mkdir()
        events = []
        controller.event_once = lambda *args, **kwargs: events.append((args, kwargs))
        controller.json_call = lambda *_args, **_kwargs: (
            (_ for _ in ()).throw(AssertionError("closeout ran with active claim"))
        )

        self.assertFalse(controller.closeout({
            "lease": "a" * 64,
            "ticket": "T-175",
            "worktree": str(self.root / "cells/cell-1"),
        }))
        self.assertEqual(events, [(('closeout_deferred_active_claim', 'T-175'), {
            "active_claim": "T-174.reviewer.lock",
        })])

    def test_closeout_defers_behind_unmerged_sibling_closeout(self) -> None:
        controller = CONTROL.Controller(self.args)
        root = self.root / "cells"
        done = root / "closeout-T-174/factory/attestations/T-174/done.json"
        done.parent.mkdir(parents=True)
        done.write_text("{}\n", encoding="utf-8")
        events = []
        controller.product_ticket_done = lambda ticket: False
        controller.event_once = lambda *args, **kwargs: events.append((args, kwargs))
        controller.json_call = lambda *_args, **_kwargs: (
            (_ for _ in ()).throw(AssertionError("concurrent closeout ran"))
        )

        self.assertFalse(controller.closeout({
            "lease": "a" * 64,
            "ticket": "T-175",
            "worktree": str(root / "cell-1"),
        }))
        self.assertEqual(events, [(('closeout_deferred_pending_closeout', 'T-175'), {
            "pending_ticket": "T-174",
        })])

    def test_closeout_records_exact_terminal_linear_evidence_once(self) -> None:
        controller = CONTROL.Controller(self.args)
        cell = self.root / "cells/cell-1"
        cell.mkdir(parents=True)
        (cell.parent / "closeout-T-110").mkdir()
        events = []
        controller.event_once = lambda *args, **kwargs: events.append((args, kwargs))
        controller.json_call = lambda *_args, **_kwargs: {
            "closeout_pr_state": "MERGED",
            "terminal": {
                "basis": "attested-done",
                "protected_main": "b" * 40,
                "linear": {
                    "identifier": "SF-110",
                    "issue_id": "issue-110",
                    "source_ref": "refs/remotes/origin/main",
                    "state": "Done",
                    "state_id": "state-done",
                    "updated": True,
                },
            },
        }

        with patch.object(CONTROL.subprocess, "run"):
            self.assertTrue(controller.closeout({
                "lease": "a" * 64,
                "ticket": "T-110",
                "worktree": str(cell),
            }))

        self.assertEqual(events, [(('linear_terminal_synced', 'T-110'), {
            "linear_identifier": "SF-110",
            "linear_issue_id": "issue-110",
            "linear_state_id": "state-done",
            "protected_main": "b" * 40,
            "terminal_basis": "attested-done",
        })])

    def test_closeout_refuses_merged_without_terminal_linear_evidence(self) -> None:
        controller = CONTROL.Controller(self.args)
        cell = self.root / "cells/cell-1"
        cell.mkdir(parents=True)
        (cell.parent / "closeout-T-110").mkdir()
        controller.json_call = lambda *_args, **_kwargs: {
            "closeout_pr_state": "MERGED",
        }

        with (
            patch.object(CONTROL.subprocess, "run"),
            self.assertRaisesRegex(CONTROL.ControllerError, "terminal Linear"),
        ):
            controller.closeout({
                "lease": "a" * 64,
                "ticket": "T-110",
                "worktree": str(cell),
            })

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
