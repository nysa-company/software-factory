#!/usr/bin/env python3
"""Focused Contract 1.8 transition receipt tests."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
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
        self.assertIsNone(STATE.stage_role("AWAIT_DEPENDENCY T-094"))
        with self.assertRaisesRegex(STATE.StateError, "unsupported transition"):
            STATE.stage_role("FIX builder-or-test-author")

    def test_unmerged_dependency_waits_without_consuming_a_role_receipt(self) -> None:
        ticket = self.product / "factory/tickets/T-110.md"
        ticket.write_text(
            "# T-110\n\nState: Building\nDepends-On: T-094\n",
            encoding="utf-8",
        )
        with (
            mock.patch.object(
                STATE,
                "protected_dependency",
                side_effect=STATE.ValidationError("not merged"),
            ),
            mock.patch.object(STATE, "contract_repair_stage", return_value=(None, False)),
            mock.patch.object(STATE, "resolve") as resolve,
            mock.patch.object(STATE, "migrate_passport") as migrate,
        ):
            result = STATE.next_transition(self.args)
        resolve.assert_not_called()
        migrate.assert_called_once_with(self.args)
        self.assertEqual(result["stage"], "AWAIT_DEPENDENCY T-094")
        self.assertIsNone(result["role"])
        receipt = STATE.safe_receipt(self.state_dir / "T-110.json")
        self.assertFalse(receipt["consumed"])

    def test_resolved_dependency_requires_current_protected_base_before_role(self) -> None:
        original = run("git", "rev-parse", "HEAD", cwd=self.product)
        run("git", "checkout", "-qb", "main", cwd=self.product)
        (self.product / "dependency.txt").write_text("merged dependency\n")
        run("git", "add", "dependency.txt", cwd=self.product)
        run("git", "commit", "-qm", "merge dependency", cwd=self.product)
        protected = run("git", "rev-parse", "HEAD", cwd=self.product)
        run("git", "update-ref", "refs/remotes/origin/main", protected, cwd=self.product)
        run("git", "checkout", "-q", "ticket/T-110", cwd=self.product)
        self.assertEqual(run("git", "rev-parse", "HEAD", cwd=self.product), original)
        ticket = self.product / "factory/tickets/T-110.md"
        ticket.write_text(
            "# T-110\n\nState: Building\nDepends-On: T-094\n",
            encoding="utf-8",
        )
        run("git", "add", str(ticket), cwd=self.product)
        run("git", "commit", "-qm", "wait for dependency", cwd=self.product)
        with (
            mock.patch.object(STATE, "protected_dependency", return_value={}),
            mock.patch.object(
                STATE, "contract_repair_stage", return_value=(None, False)
            ),
            mock.patch.object(STATE, "resolve") as resolve,
            mock.patch.object(STATE, "migrate_passport") as migrate,
        ):
            result = STATE.next_transition(self.args)
        resolve.assert_not_called()
        migrate.assert_not_called()
        self.assertEqual(
            result["stage"],
            "REFUSE dependency refresh required; "
            f"dependencies=T-094; protected-main={protected}",
        )
        self.assertIsNone(result["role"])
        receipt = STATE.safe_receipt(self.state_dir / "T-110.json")
        self.assertEqual(receipt["head_sha"], run(
            "git", "rev-parse", "HEAD", cwd=self.product
        ))
        self.assertIn(protected, receipt["stage"])

    def test_exact_refusal_is_bound_to_a_transition_receipt(self) -> None:
        kit = self.root / "kit"
        (kit / "scripts").mkdir(parents=True)
        (kit / "scripts/next-stage.sh").write_text(
            "#!/bin/bash\n"
            "echo 'REFUSE refresh receipt was not committed directly after its merge'\n"
            "exit 1\n",
            encoding="utf-8",
        )
        self.args.kit_dir = kit
        with mock.patch.object(STATE, "migrate_passport") as migrate:
            result = STATE.next_transition(self.args)
        migrate.assert_not_called()
        self.assertEqual(
            result["stage"],
            "REFUSE refresh receipt was not committed directly after its merge",
        )
        self.assertEqual(
            STATE.safe_receipt(self.state_dir / "T-110.json")["receipt_sha256"],
            result["receipt"],
        )

    def test_role_stage_is_resolved_once_before_transition_receipt(self) -> None:
        receipt = "b" * 64
        with (
            mock.patch.object(
                STATE,
                "current_state",
                side_effect=["Planning", "Planning", "Building"],
            ),
            mock.patch.object(
                STATE, "contract_repair_stage", return_value=(None, False)
            ),
            mock.patch.object(
                STATE, "resolve", return_value="RUN builder"
            ) as resolve,
            mock.patch.object(STATE, "transition") as transition,
            mock.patch.object(STATE, "migrate_passport") as migrate,
            mock.patch.object(
                STATE,
                "issue",
                return_value={"receipt_sha256": receipt},
            ) as issue,
        ):
            result = STATE.next_transition(self.args)

        resolve.assert_called_once_with(self.args)
        transition.assert_called_once_with(self.args, "Building")
        migrate.assert_called_once_with(self.args)
        issue.assert_called_once_with(self.args, "RUN builder")
        self.assertEqual(result["receipt"], receipt)
        self.assertEqual(result["role"], "builder")
        self.assertEqual(result["stage"], "RUN builder")

    def test_completed_repair_stage_is_not_resolved_again(self) -> None:
        receipt = "b" * 64
        with (
            mock.patch.object(
                STATE,
                "current_state",
                side_effect=["Building", "Building"],
            ),
            mock.patch.object(
                STATE,
                "contract_repair_stage",
                return_value=("RUN builder", False),
            ),
            mock.patch.object(STATE, "resolve") as resolve,
            mock.patch.object(STATE, "transition") as transition,
            mock.patch.object(STATE, "migrate_passport") as migrate,
            mock.patch.object(
                STATE,
                "issue",
                return_value={"receipt_sha256": receipt},
            ),
        ):
            result = STATE.next_transition(self.args)

        resolve.assert_not_called()
        transition.assert_not_called()
        migrate.assert_called_once_with(self.args)
        self.assertEqual(result["receipt"], receipt)
        self.assertEqual(result["role"], "builder")
        self.assertEqual(result["stage"], "RUN builder")

    def test_contract_block_and_resume_require_exact_terminal_receipt(self) -> None:
        self.args.lease = "d" * 64
        issued = STATE.issue(self.args, "RUN planner")
        self.args.receipt = issued["receipt_sha256"]
        STATE.verify(self.args, consume=True)
        manifest = self.product / "factory/runs/blocked.meta"
        manifest.write_text(
            "run_id=blocked\n"
            "phase=completed\n"
            "accounting_state=completed\n"
            "go_issued=1\n"
            "task_submitted=1\n"
            "ticket=T-110\n"
            "role=planner\n"
            f"contract_version={self.args.contract_version}\n"
            f"kit_sha={self.args.factory_sha}\n"
            "exit_status=12\n"
            "role_exit=role_exit_contract_blocked\n"
            "role_branch_before=ticket/T-110\n"
            f"role_head_before={issued['head_sha']}\n"
            f"transition_receipt_sha256={self.args.receipt}\n",
            encoding="utf-8",
        )
        self.args.action = "block"
        manifest.write_text(
            manifest.read_text(encoding="utf-8").replace(
                "task_submitted=1", "task_submitted=0"
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(STATE.StateError, "terminal evidence is invalid"):
            STATE.block_transition(self.args)
        manifest.write_text(
            manifest.read_text(encoding="utf-8").replace(
                "task_submitted=0", "task_submitted=1"
            ),
            encoding="utf-8",
        )

        def block(_args, _state):
            path = self.product / "factory/tickets/T-110.md"
            path.write_text(
                "# T-110\n\nState: Blocked-Escalated\n"
                "Resume-State: Planning\n",
                encoding="utf-8",
            )

        with (
            mock.patch.object(STATE, "run_helper", return_value=""),
            mock.patch.object(STATE, "transition", side_effect=block),
        ):
            result = STATE.block_transition(self.args)
        self.assertEqual(result["status"], "blocked")

        self.args.action = "resume"

        def resume(*_args, **_kwargs):
            path = self.product / "factory/tickets/T-110.md"
            path.write_text(
                path.read_text(encoding="utf-8").replace(
                    "State: Blocked-Escalated", "State: Planning"
                ),
                encoding="utf-8",
            )
            return ""

        with (
            mock.patch.object(STATE, "run_helper", side_effect=resume),
            mock.patch.object(STATE, "migrate_passport") as migrate,
            mock.patch.object(
                STATE,
                "authenticated_passport",
                return_value=({
                    "branch": "ticket/T-110",
                    "factory_sha": self.args.factory_sha,
                    "head_sha": issued["head_sha"],
                    "passport_sha256": "e" * 64,
                    "ticket": "T-110",
                }, b"x" * 32),
            ),
            mock.patch.object(
                STATE, "operator_resume_role", return_value="planner"
            ),
        ):
            result = STATE.resume_transition(self.args)
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["repair_role"], "planner")
        migrate.assert_called_once_with(self.args)

    def test_materialized_contract_block_survives_lease_rotation(self) -> None:
        original_lease = "d" * 64
        self.args.lease = original_lease
        issued = STATE.issue(self.args, "RUN planner")
        self.args.receipt = issued["receipt_sha256"]
        STATE.verify(self.args, consume=True)
        manifest = self.product / "factory/runs/blocked-after-restart.meta"
        manifest.write_text(
            "run_id=blocked-after-restart\n"
            "phase=completed\n"
            "accounting_state=completed\n"
            "go_issued=1\n"
            "task_submitted=1\n"
            "ticket=T-110\n"
            "role=planner\n"
            f"contract_version={self.args.contract_version}\n"
            f"kit_sha={self.args.factory_sha}\n"
            "exit_status=12\n"
            "role_exit=role_exit_contract_blocked\n"
            "role_branch_before=ticket/T-110\n"
            f"role_head_before={issued['head_sha']}\n"
            f"transition_receipt_sha256={self.args.receipt}\n",
            encoding="utf-8",
        )
        ticket = self.product / "factory/tickets/T-110.md"
        ticket.write_text(
            "# T-110\n\nState: Blocked-Escalated\n"
            "Resume-State: Planning\n",
            encoding="utf-8",
        )
        run("git", "add", str(ticket), cwd=self.product)
        run("git", "commit", "-qm", "materialize contract blocker", cwd=self.product)
        blocked_head = run("git", "rev-parse", "HEAD", cwd=self.product)
        secret = b"k" * 32
        (self.state_dir / "passport.key").write_bytes(secret)
        os.chmod(self.state_dir / "passport.key", 0o600)
        passports = self.state_dir / "passports"
        passports.mkdir(mode=0o700)
        body = {
            "branch": "ticket/T-110",
            "charge_records": [{
                "contract_version": self.args.contract_version,
                "factory_sha": self.args.factory_sha,
                "head_before": issued["head_sha"],
                "role": "planner",
                "transition_receipt_sha256": self.args.receipt,
            }],
            "completed_role_evidence": [],
            "contract_version": self.args.contract_version,
            "current_stage": "RUN planner",
            "current_state": "Blocked-Escalated",
            "factory_sha": self.args.factory_sha,
            "head_sha": blocked_head,
            "project": self.args.project,
            "schema": STATE.PASSPORT_SCHEMA,
            "ticket": "T-110",
            "transition_receipt_sha256": self.args.receipt,
        }
        passport = dict(body)
        passport["authentication_sha256"] = hmac.new(
            secret, STATE.canonical(body), hashlib.sha256
        ).hexdigest()
        passport["passport_sha256"] = hashlib.sha256(
            STATE.canonical(passport)
        ).hexdigest()
        STATE.write_atomic(passports / "T-110.json", passport)

        self.args.action = "block"
        self.args.lease = "e" * 64
        leases = self.product / "factory/.dispatch-leases"
        leases.mkdir()
        lease_path = leases / "T-110.json"
        lease_path.write_text(
            json.dumps({
                "claimed_epoch": int(time.time()),
                "expires_epoch": int(time.time()) + 900,
                "lease_id": self.args.lease,
                "schema_version": 1,
                "ticket": "T-110",
            }) + "\n",
            encoding="utf-8",
        )
        os.chmod(lease_path, 0o600)
        self.assertEqual(STATE.contract_blocked_receipt(self.args), "planner")

        passport["current_state"] = "Planning"
        unsigned = {
            key: value for key, value in passport.items()
            if key not in {"authentication_sha256", "passport_sha256"}
        }
        passport["authentication_sha256"] = hmac.new(
            secret, STATE.canonical(unsigned), hashlib.sha256
        ).hexdigest()
        passport.pop("passport_sha256")
        passport["passport_sha256"] = hashlib.sha256(
            STATE.canonical(passport)
        ).hexdigest()
        STATE.write_atomic(passports / "T-110.json", passport)
        with self.assertRaisesRegex(
            STATE.StateError, "contract blocker receipt is invalid"
        ):
            STATE.contract_blocked_receipt(self.args)

    def test_migrated_contract_block_uses_historical_charge_and_current_lease(
        self,
    ) -> None:
        old_factory = "b" * 40
        current_factory = self.args.factory_sha
        old_lease = "c" * 64
        self.args.factory_sha = old_factory
        self.args.lease = old_lease
        issued = STATE.issue(self.args, "RUN planner")
        self.args.receipt = issued["receipt_sha256"]
        STATE.verify(self.args, consume=True)
        manifest = self.product / "factory/runs/migrated-block.meta"
        manifest.write_text(
            "run_id=migrated-block\n"
            "phase=completed\n"
            "accounting_state=completed\n"
            "go_issued=1\n"
            "task_submitted=1\n"
            "ticket=T-110\n"
            "role=planner\n"
            f"contract_version={self.args.contract_version}\n"
            f"kit_sha={old_factory}\n"
            "exit_status=12\n"
            "role_exit=role_exit_contract_blocked\n"
            "role_branch_before=ticket/T-110\n"
            f"role_head_before={issued['head_sha']}\n"
            f"transition_receipt_sha256={self.args.receipt}\n",
            encoding="utf-8",
        )
        secret = b"k" * 32
        (self.state_dir / "passport.key").write_bytes(secret)
        os.chmod(self.state_dir / "passport.key", 0o600)
        passports = self.state_dir / "passports"
        passports.mkdir(mode=0o700)
        body = {
            "branch": "ticket/T-110",
            "charge_records": [{
                "accounting_state": "completed",
                "contract_version": self.args.contract_version,
                "factory_sha": old_factory,
                "head_before": issued["head_sha"],
                "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
                "role": "planner",
                "run_id": "migrated-block",
                "transition_receipt_sha256": self.args.receipt,
            }],
            "completed_role_evidence": [],
            "factory_release_history": [
                {
                    "contract_version": self.args.contract_version,
                    "factory_sha": old_factory,
                },
                {
                    "contract_version": self.args.contract_version,
                    "factory_sha": current_factory,
                },
            ],
            "factory_sha": current_factory,
            "head_sha": issued["head_sha"],
            "schema": STATE.PASSPORT_SCHEMA,
            "ticket": "T-110",
        }
        passport = dict(body)
        passport["authentication_sha256"] = hmac.new(
            secret, STATE.canonical(body), hashlib.sha256
        ).hexdigest()
        passport["passport_sha256"] = hashlib.sha256(
            STATE.canonical(passport)
        ).hexdigest()
        STATE.write_atomic(passports / "T-110.json", passport)
        self.args.action = "block"
        self.args.factory_sha = current_factory
        self.args.lease = "d" * 64
        with self.assertRaisesRegex(
            STATE.StateError, "current dispatcher lease is invalid"
        ):
            STATE.contract_blocked_receipt(self.args)
        leases = self.product / "factory/.dispatch-leases"
        leases.mkdir()
        lease_path = leases / "T-110.json"
        lease_path.write_text(
            json.dumps({
                "claimed_epoch": int(time.time()),
                "expires_epoch": int(time.time()) + 900,
                "lease_id": "e" * 64,
                "schema_version": 1,
                "ticket": "T-110",
            }) + "\n",
            encoding="utf-8",
        )
        os.chmod(lease_path, 0o600)
        with self.assertRaisesRegex(
            STATE.StateError, "current dispatcher lease is invalid"
        ):
            STATE.contract_blocked_receipt(self.args)
        lease = json.loads(lease_path.read_text(encoding="utf-8"))
        lease["lease_id"] = self.args.lease
        lease_path.write_text(json.dumps(lease) + "\n", encoding="utf-8")
        self.assertEqual(STATE.contract_blocked_receipt(self.args), "planner")

    def test_operator_resume_names_exact_repair_owner_only(self) -> None:
        head = run("git", "rev-parse", "HEAD", cwd=self.product)
        passport = {
            "branch": "ticket/T-110",
            "factory_sha": self.args.factory_sha,
            "head_sha": head,
            "ticket": "T-110",
        }
        path = self.product / "factory/tickets/T-110.md"
        path.write_text(
            path.read_text(encoding="utf-8")
            + "\nOPERATOR RESUME: test-author\n",
            encoding="utf-8",
        )
        run("git", "add", str(path), cwd=self.product)
        run("git", "commit", "-qm", "authorize exact test repair", cwd=self.product)
        self.assertEqual(
            STATE.operator_resume_role(self.args, passport, "builder"),
            "test-author",
        )

        (self.product / "unexpected").write_text("drift\n", encoding="utf-8")
        run("git", "add", "unexpected", cwd=self.product)
        run("git", "commit", "-qm", "add unrelated drift", cwd=self.product)
        with self.assertRaisesRegex(
            STATE.StateError, "operator directive is invalid"
        ):
            STATE.operator_resume_role(self.args, passport, "builder")

    def test_operator_resume_uses_current_passport_repair_window(self) -> None:
        path = self.product / "factory/tickets/T-110.md"
        original = path.read_text(encoding="utf-8")
        directive = "OPERATOR RESUME: test-author"

        path.write_text(
            original.rstrip("\n") + f"\n\n{directive}\n",
            encoding="utf-8",
        )
        run("git", "add", str(path), cwd=self.product)
        run("git", "commit", "-qm", "historical test repair", cwd=self.product)
        path.write_text(original, encoding="utf-8")
        run("git", "add", str(path), cwd=self.product)
        run("git", "commit", "-qm", "finish historical test repair", cwd=self.product)

        path.write_text(
            original.rstrip("\n") + "\n\nBlocked-Receipt: current\n",
            encoding="utf-8",
        )
        run("git", "add", str(path), cwd=self.product)
        run("git", "commit", "-qm", "materialize current blocker", cwd=self.product)
        blocked_head = run("git", "rev-parse", "HEAD", cwd=self.product)

        path.write_text(
            path.read_text(encoding="utf-8").rstrip("\n")
            + f"\n\n{directive}\n",
            encoding="utf-8",
        )
        run("git", "add", str(path), cwd=self.product)
        run("git", "commit", "-qm", "authorize current test repair", cwd=self.product)

        route = self.product / "factory/route-plans/T-110.json"
        route.write_text(
            '{"factory_sha":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
            '"ticket":"T-110"}\n',
            encoding="utf-8",
        )
        run("git", "add", str(route), cwd=self.product)
        run("git", "commit", "-qm", "migrate current repair route", cwd=self.product)
        migrated_head = run("git", "rev-parse", "HEAD", cwd=self.product)
        passport = {
            "branch": "ticket/T-110",
            "factory_sha": self.args.factory_sha,
            "head_sha": migrated_head,
            "migration_history": [{
                "from_head_sha": blocked_head,
                "to_head_sha": migrated_head,
            }],
            "ticket": "T-110",
        }
        self.assertEqual(
            STATE.operator_resume_role(self.args, passport, "builder"),
            "test-author",
        )

        path.write_text(
            path.read_text(encoding="utf-8").replace(
                f"\n\n{directive}\n", "\n", 1
            ),
            encoding="utf-8",
        )
        run("git", "add", str(path), cwd=self.product)
        run("git", "commit", "-qm", "withdraw current test repair", cwd=self.product)
        withdrawn_head = run("git", "rev-parse", "HEAD", cwd=self.product)
        path.write_text(
            path.read_text(encoding="utf-8").rstrip("\n")
            + f"\n\n{directive}\n",
            encoding="utf-8",
        )
        run("git", "add", str(path), cwd=self.product)
        run("git", "commit", "-qm", "duplicate current test repair", cwd=self.product)
        duplicate_head = run("git", "rev-parse", "HEAD", cwd=self.product)
        passport["head_sha"] = duplicate_head
        passport["migration_history"].append({
            "from_head_sha": withdrawn_head,
            "to_head_sha": duplicate_head,
        })
        with self.assertRaisesRegex(
            STATE.StateError, "operator directive is invalid"
        ):
            STATE.operator_resume_role(self.args, passport, "builder")

    def test_authenticated_contract_repair_is_one_success_boundary(self) -> None:
        secret = b"k" * 32
        (self.state_dir / "passport.key").write_bytes(secret)
        os.chmod(self.state_dir / "passport.key", 0o600)
        passports = self.state_dir / "passports"
        passports.mkdir(mode=0o700)
        head = run("git", "rev-parse", "HEAD", cwd=self.product)
        body = {
            "branch": "ticket/T-110",
            "factory_sha": self.args.factory_sha,
            "head_sha": head,
            "schema": STATE.PASSPORT_SCHEMA,
            "ticket": "T-110",
        }
        passport = dict(body)
        passport["authentication_sha256"] = hmac.new(
            secret, STATE.canonical(body), hashlib.sha256
        ).hexdigest()
        passport["passport_sha256"] = hashlib.sha256(
            STATE.canonical(passport)
        ).hexdigest()
        STATE.write_atomic(passports / "T-110.json", passport)
        record = STATE.signed_repair({
            "blocked_receipt": "b" * 64,
            "blocked_role": "builder",
            "branch": "ticket/T-110",
            "factory_sha": self.args.factory_sha,
            "head_sha": head,
            "head_tree": run("git", "rev-parse", "HEAD^{tree}", cwd=self.product),
            "passport_sha256": passport["passport_sha256"],
            "repair_role": "test-author",
            "schema": STATE.REPAIR_SCHEMA,
            "ticket": "T-110",
        }, secret)
        STATE.write_atomic(STATE.repair_path(self.args), record)
        self.assertEqual(
            STATE.contract_repair_stage(self.args), ("FIX test-author", True)
        )

        (self.product / "factory/runs/repair.meta").write_text(
            "run_id=repair\nphase=completed\naccounting_state=completed\n"
            "ticket=T-110\nrole=test-author\nexit_status=0\nrole_exit=ok\n"
            f"role_head_before={head}\n",
            encoding="utf-8",
        )
        ticket = self.product / "factory/tickets/T-110.md"
        ticket.write_text(
            ticket.read_text(encoding="utf-8").replace(
                "State: Planning", "State: Building"
            ),
            encoding="utf-8",
        )
        with mock.patch.object(STATE, "resolve", return_value="RUN planner"):
            self.assertEqual(
                STATE.contract_repair_stage(self.args), ("RUN planner", True)
            )
        STATE.write_atomic(STATE.repair_path(self.args), record)
        with mock.patch.object(
            STATE, "resolve", return_value="RUN builder"
        ) as resolve:
            self.assertEqual(
                STATE.contract_repair_stage(self.args),
                ("RUN builder", False),
            )
        resolve.assert_called_once_with(self.args)

    def test_dependency_conflict_routes_exactly_one_new_test_author(self) -> None:
        secret = b"k" * 32
        (self.state_dir / "passport.key").write_bytes(secret)
        os.chmod(self.state_dir / "passport.key", 0o600)
        passports = self.state_dir / "passports"
        passports.mkdir(mode=0o700)
        prior_head = run("git", "rev-parse", "HEAD", cwd=self.product)
        conflict_path = self.product / "tests/dependency-conflict.test.ts"
        conflict_path.parent.mkdir()
        conflict_path.write_text("protected baseline\n", encoding="utf-8")
        ticket = self.product / "factory/tickets/T-110.md"
        ticket.write_text(
            "# T-110\n\nState: Building\nDepends-On: T-094\n",
            encoding="utf-8",
        )
        run("git", "add", str(ticket), str(conflict_path), cwd=self.product)
        run("git", "commit", "-qm", "bind dependency conflict receipt", cwd=self.product)
        receipt_head = run("git", "rev-parse", "HEAD", cwd=self.product)
        passport_body = {
            "branch": "ticket/T-110",
            "factory_sha": self.args.factory_sha,
            "head_sha": receipt_head,
            "protected_base_sha": prior_head,
            "schema": STATE.PASSPORT_SCHEMA,
            "ticket": "T-110",
        }
        passport = dict(passport_body)
        passport["authentication_sha256"] = hmac.new(
            secret, STATE.canonical(passport_body), hashlib.sha256
        ).hexdigest()
        passport["passport_sha256"] = hashlib.sha256(
            STATE.canonical(passport)
        ).hexdigest()
        STATE.write_atomic(passports / "T-110.json", passport)
        conflict = {
            "conflicts": [{"path": "tests/dependency-conflict.test.ts"}],
            "contract_version": self.args.contract_version,
            "factory_sha": self.args.factory_sha,
            "protected_head": prior_head,
            "transition_receipt_sha256": "c" * 64,
        }
        conflict_digest = "d" * 64
        found = (conflict, conflict_digest, receipt_head)
        receipt_path = (
            self.product
            / "factory/attestations/T-110/dependency-refresh.json"
        )
        receipt_path.parent.mkdir(parents=True)
        receipt_path.write_text("{}\n", encoding="utf-8")
        # A still-valid earlier Test-author success must not satisfy the new
        # protected-base repair boundary.
        (self.product / "factory/runs/prior-test-author.meta").write_text(
            "run_id=prior-test-author\nphase=completed\n"
            "accounting_state=completed\nticket=T-110\nrole=test-author\n"
            "exit_status=0\nrole_exit=ok\n"
            f"role_head_before={prior_head}\n",
            encoding="utf-8",
        )
        with (
            mock.patch.object(STATE, "migrate_passport"),
            mock.patch.object(
                STATE, "authenticated_passport",
                return_value=(passport, secret),
            ),
            mock.patch.object(
                STATE, "dependency_conflict_receipt", return_value=found,
            ),
            mock.patch.object(
                STATE, "protected_base_sha", return_value=prior_head,
            ),
            mock.patch.object(
                STATE, "validate_dependency_conflict_transition",
            ) as validate,
        ):
            STATE.ensure_dependency_conflict_repair(self.args)
        validate.assert_called_once_with(self.args, conflict)
        record = STATE.load_repair(self.args, secret)
        self.assertEqual(record["repair_source"], STATE.DEPENDENCY_CONFLICT_SOURCE)
        self.assertEqual(record["repair_role"], "test-author")
        self.assertEqual(record["head_sha"], receipt_head)
        run(
            "git", "switch", "-q", "-c", "protected-advanced", prior_head,
            cwd=self.product,
        )
        (self.product / "sibling.txt").write_text(
            "sibling merge\n", encoding="utf-8",
        )
        run("git", "add", "sibling.txt", cwd=self.product)
        run("git", "commit", "-qm", "advance protected sibling", cwd=self.product)
        advanced_base = run("git", "rev-parse", "HEAD", cwd=self.product)
        run("git", "switch", "-q", "ticket/T-110", cwd=self.product)
        with (
            mock.patch.object(
                STATE, "authenticated_passport",
                return_value=(passport, secret),
            ),
            mock.patch.object(
                STATE, "dependency_conflict_receipt", return_value=found,
            ),
            mock.patch.object(
                STATE, "protected_base_sha", return_value=advanced_base,
            ),
        ):
            STATE.ensure_dependency_conflict_repair(self.args)
        mismatched_body = {
            key: value for key, value in record.items()
            if key not in {"authentication_sha256", "repair_sha256"}
        }
        mismatched_body["dependency_refresh_sha256"] = "9" * 64
        STATE.write_atomic(
            STATE.repair_path(self.args),
            STATE.signed_repair(mismatched_body, secret),
        )
        with (
            mock.patch.object(
                STATE, "authenticated_passport",
                return_value=(passport, secret),
            ),
            mock.patch.object(
                STATE, "dependency_conflict_receipt", return_value=found,
            ),
            mock.patch.object(
                STATE, "protected_base_sha", return_value=prior_head,
            ),
            self.assertRaisesRegex(
                STATE.StateError, "conflicts with active repair",
            ),
        ):
            STATE.ensure_dependency_conflict_repair(self.args)
        STATE.write_atomic(STATE.repair_path(self.args), record)
        with (
            mock.patch.object(
                STATE, "authenticated_passport",
                return_value=(passport, secret),
            ),
            mock.patch.object(
                STATE, "dependency_conflict_receipt", return_value=found,
            ),
            mock.patch.object(
                STATE, "protected_base_sha", return_value=prior_head,
            ),
        ):
            self.assertEqual(
                STATE.contract_repair_stage(self.args),
                ("FIX test-author", True),
            )

        issued = STATE.issue(self.args, "FIX test-author")
        self.args.receipt = issued["receipt_sha256"]
        self.args.role = "test-author"
        STATE.verify(self.args, consume=True)
        record = STATE.load_repair(self.args, secret)
        self.assertEqual(
            STATE.dependency_conflict_successes(
                self.args, record, conflict, [{
                    "transition_receipt_sha256": "0" * 64,
                }], passport, False,
            ),
            [],
        )

        ticket.write_text(
            ticket.read_text(encoding="utf-8") + "\nintervening log\n",
            encoding="utf-8",
        )
        run("git", "add", str(ticket), cwd=self.product)
        run("git", "commit", "-qm", "intervening descendant", cwd=self.product)
        descendant = run("git", "rev-parse", "HEAD", cwd=self.product)
        with self.assertRaisesRegex(
            STATE.StateError, "repair success is invalid",
        ):
            STATE.dependency_conflict_successes(
                self.args, record, conflict, [{
                    "accounting_state": "completed",
                    "contract_version": self.args.contract_version,
                    "go_issued": "1",
                    "kit_sha": self.args.factory_sha,
                    "manifest_sha256": "1" * 64,
                    "role_branch_before": "ticket/T-110",
                    "role_head_before": descendant,
                    "run_id": "descendant-before",
                    "task_submitted": "1",
                    "transition_receipt_sha256": self.args.receipt,
                }], passport, False,
            )
        run("git", "reset", "--hard", issued["head_sha"], cwd=self.product)

        unrelated = self.product / "src/unrelated.ts"
        unrelated.parent.mkdir()
        unrelated.write_text("unrelated\n", encoding="utf-8")
        run("git", "add", str(unrelated), cwd=self.product)
        run("git", "commit", "-qm", "unrelated repair output", cwd=self.product)
        unrelated_head = run("git", "rev-parse", "HEAD", cwd=self.product)
        unrelated_success = {
            "accounting_state": "completed",
            "contract_version": self.args.contract_version,
            "go_issued": "1",
            "kit_sha": self.args.factory_sha,
            "manifest_sha256": "2" * 64,
            "role_branch_before": "ticket/T-110",
            "role_head_before": issued["head_sha"],
            "run_id": "unrelated-output",
            "task_submitted": "1",
            "transition_receipt_sha256": self.args.receipt,
        }
        unrelated_evidence = {
            "contract_version": self.args.contract_version,
            "factory_sha": self.args.factory_sha,
            "head_before": issued["head_sha"],
            "manifest_sha256": "2" * 64,
            "role": "test-author",
            "run_id": "unrelated-output",
            "transition_receipt_sha256": self.args.receipt,
        }
        unrelated_passport = {
            **passport,
            "charge_records": [{
                **unrelated_evidence,
                "accounting_state": "completed",
                "charge_micro_usd": 1,
            }],
            "completed_role_evidence": [{
                **unrelated_evidence,
                "output_sha256": "3" * 64,
            }],
            "current_stage": "FIX test-author",
            "head_sha": unrelated_head,
            "transition_receipt_sha256": self.args.receipt,
        }
        with self.assertRaisesRegex(
            STATE.StateError, "unauthorized path",
        ):
            STATE.dependency_conflict_successes(
                self.args, record, conflict,
                [unrelated_success], unrelated_passport, False,
            )
        run("git", "reset", "--hard", issued["head_sha"], cwd=self.product)

        run("git", "rm", "-q", str(conflict_path), cwd=self.product)
        run("git", "commit", "-qm", "delete allowed conflict", cwd=self.product)
        deleted_head = run("git", "rev-parse", "HEAD", cwd=self.product)
        deleted_success = {
            **unrelated_success,
            "manifest_sha256": "4" * 64,
            "run_id": "deleted-output",
        }
        deleted_evidence = {
            **unrelated_evidence,
            "manifest_sha256": "4" * 64,
            "run_id": "deleted-output",
        }
        deleted_passport = {
            **passport,
            "charge_records": [{
                **deleted_evidence,
                "accounting_state": "completed",
                "charge_micro_usd": 1,
            }],
            "completed_role_evidence": [{
                **deleted_evidence,
                "output_sha256": "5" * 64,
            }],
            "current_stage": "FIX test-author",
            "head_sha": deleted_head,
            "transition_receipt_sha256": self.args.receipt,
        }
        with self.assertRaisesRegex(
            STATE.StateError, "unauthorized path",
        ):
            STATE.dependency_conflict_successes(
                self.args, record, conflict,
                [deleted_success], deleted_passport, False,
            )
        run("git", "reset", "--hard", issued["head_sha"], cwd=self.product)

        conflict_path.write_text("reconciled contract\n", encoding="utf-8")
        run("git", "add", str(conflict_path), cwd=self.product)
        run("git", "commit", "-qm", "reconcile protected test", cwd=self.product)
        manifest = self.product / "factory/runs/conflict-test-author.meta"
        manifest.write_text(
            "run_id=conflict-test-author\nphase=completed\n"
            "accounting_state=abandoned_conservative\n"
            "cost_basis=conservative_reservation\n"
            "effective_cost=10.00\nreserved_usd=10.00\n"
            "ticket=T-110\nrole=test-author\n"
            "go_issued=1\ntask_submitted=1\n"
            f"contract_version={self.args.contract_version}\n"
            f"kit_sha={self.args.factory_sha}\n"
            "exit_status=0\nrole_exit=ok\n"
            "role_branch_before=ticket/T-110\n"
            f"role_head_before={issued['head_sha']}\n"
            f"transition_receipt_sha256={self.args.receipt}\n",
            encoding="utf-8",
        )
        repaired_head = run("git", "rev-parse", "HEAD", cwd=self.product)
        manifest_digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
        evidence = {
            "contract_version": self.args.contract_version,
            "factory_sha": self.args.factory_sha,
            "head_before": issued["head_sha"],
            "manifest_sha256": manifest_digest,
            "role": "test-author",
            "run_id": "conflict-test-author",
            "transition_receipt_sha256": self.args.receipt,
        }
        terminal_passport = {
            **passport,
            "charge_records": [{
                **evidence,
                "accounting_state": "abandoned_conservative",
                "charge_micro_usd": 1,
            }],
            "completed_role_evidence": [{
                **evidence,
                "output_sha256": "e" * 64,
            }],
            "current_stage": "FIX test-author",
            "head_sha": repaired_head,
            "transition_receipt_sha256": self.args.receipt,
        }
        success = {
            **dict(
                line.split("=", 1)
                for line in manifest.read_text(
                    encoding="utf-8",
                ).splitlines()
            ),
            "manifest_sha256": manifest_digest,
        }
        with self.assertRaisesRegex(
            STATE.StateError, "repair success is invalid",
        ):
            STATE.dependency_conflict_successes(
                self.args, record, conflict,
                [{**success, "cost_basis": "actual"}],
                terminal_passport, False,
            )
        with self.assertRaisesRegex(
            STATE.StateError, "passport evidence is invalid",
        ):
            STATE.dependency_conflict_successes(
                self.args, record, conflict, [success],
                passport, False,
            )
        old_factory = self.args.factory_sha
        route = self.product / "factory/route-plans/T-110.json"
        route.write_text(
            '{"kit_sha":"migrated","ticket":"T-110"}\n',
            encoding="utf-8",
        )
        run("git", "add", str(route), cwd=self.product)
        run("git", "commit", "-qm", "migrate repaired ticket route", cwd=self.product)
        migrated_head = run("git", "rev-parse", "HEAD", cwd=self.product)
        new_factory = "f" * 40
        migrated_passport = {
            **terminal_passport,
            "factory_release_history": [
                {
                    "contract_version": self.args.contract_version,
                    "factory_sha": old_factory,
                },
                {
                    "contract_version": self.args.contract_version,
                    "factory_sha": new_factory,
                },
            ],
            "factory_sha": new_factory,
            "head_sha": migrated_head,
            "migration_history": [{
                "from_factory_sha": old_factory,
                "from_head_sha": repaired_head,
                "from_passport_file_sha256": "1" * 64,
                "from_passport_sha256": "2" * 64,
                "from_protected_base_sha": prior_head,
                "from_route_plan_sha256": "3" * 64,
                "schema": STATE.PASSPORT_MIGRATION_SCHEMA,
                "to_factory_sha": new_factory,
                "to_head_sha": migrated_head,
                "to_protected_base_sha": advanced_base,
                "to_route_plan_sha256": "4" * 64,
            }],
            "parent_digest": "2" * 64,
            "parent_file_sha256": "1" * 64,
            "protected_base_sha": advanced_base,
            "route_plan_sha256": "4" * 64,
        }
        self.args.factory_sha = new_factory
        self.assertEqual(
            STATE.dependency_conflict_successes(
                self.args, record, conflict, [success],
                migrated_passport, True,
            ),
            [success],
        )
        with self.assertRaisesRegex(
            STATE.StateError, "repair success is invalid",
        ):
            STATE.dependency_conflict_successes(
                self.args, record, conflict, [success],
                {**migrated_passport, "parent_digest": "9" * 64},
                True,
            )
        with (
            mock.patch.object(
                STATE, "authenticated_passport",
                return_value=(migrated_passport, secret),
            ),
            mock.patch.object(
                STATE, "dependency_conflict_receipt", return_value=found,
            ),
            mock.patch.object(
                STATE, "protected_base_sha", return_value=advanced_base,
            ),
            mock.patch.object(STATE, "resolve", return_value="RUN builder"),
        ):
            STATE.ensure_dependency_conflict_repair(self.args)
            self.assertEqual(
                STATE.contract_repair_stage(self.args),
                ("RUN builder", False),
            )
        self.assertFalse(STATE.repair_path(self.args).exists())
        receipt_path.unlink()
        with (
            mock.patch.object(
                STATE, "authenticated_passport",
                return_value=(terminal_passport, secret),
            ),
            self.assertRaisesRegex(
                STATE.StateError, "receipt was deleted",
            ),
        ):
            STATE.ensure_dependency_conflict_repair(self.args)
        receipt_path.write_text("{}\n", encoding="utf-8")
        with (
            mock.patch.object(STATE, "migrate_passport") as migrate,
            mock.patch.object(
                STATE, "authenticated_passport",
                return_value=(terminal_passport, secret),
            ),
            mock.patch.object(
                STATE, "dependency_conflict_receipt", return_value=found,
            ),
            mock.patch.object(
                STATE, "validate_dependency_conflict_transition",
            ) as validate_again,
            mock.patch.object(
                STATE, "protected_base_sha",
                side_effect=AssertionError(
                    "completed repair revalidated current protected main"
                ),
            ),
        ):
            STATE.ensure_dependency_conflict_repair(self.args)
        validate_again.assert_not_called()
        migrate.assert_not_called()
        self.assertFalse(STATE.repair_path(self.args).exists())

    def test_contract_repair_survives_dependency_wait_and_release_migration(
        self,
    ) -> None:
        secret = b"k" * 32
        (self.state_dir / "passport.key").write_bytes(secret)
        os.chmod(self.state_dir / "passport.key", 0o600)
        passports = self.state_dir / "passports"
        passports.mkdir(mode=0o700)
        old_factory = "b" * 40
        old_passport = "c" * 64
        blocked_receipt = "d" * 64
        old_head = run("git", "rev-parse", "HEAD", cwd=self.product)
        ticket = self.product / "factory/tickets/T-110.md"
        ticket.write_text(
            ticket.read_text(encoding="utf-8")
            + "\nDepends-On: T-092\n",
            encoding="utf-8",
        )
        run("git", "add", str(ticket), cwd=self.product)
        run("git", "commit", "-qm", "wait for dependency", cwd=self.product)
        current_head = run("git", "rev-parse", "HEAD", cwd=self.product)
        body = {
            "branch": "ticket/T-110",
            "charge_records": [{
                "role": "builder",
                "transition_receipt_sha256": blocked_receipt,
            }],
            "completed_role_evidence": [],
            "current_stage": "AWAIT_DEPENDENCY T-092",
            "factory_release_history": [
                {
                    "contract_version": self.args.contract_version,
                    "factory_sha": old_factory,
                },
                {
                    "contract_version": self.args.contract_version,
                    "factory_sha": self.args.factory_sha,
                },
            ],
            "factory_sha": self.args.factory_sha,
            "head_sha": current_head,
            "migration_history": [{
                "from_factory_sha": old_factory,
                "from_head_sha": old_head,
                "from_passport_file_sha256": "f" * 64,
                "from_passport_sha256": old_passport,
                "from_protected_base_sha": "1" * 40,
                "from_route_plan_sha256": "2" * 64,
                "schema": STATE.PASSPORT_MIGRATION_SCHEMA,
                "to_factory_sha": self.args.factory_sha,
                "to_head_sha": current_head,
                "to_protected_base_sha": "3" * 40,
                "to_route_plan_sha256": "4" * 64,
            }],
            "protected_base_sha": "3" * 40,
            "schema": STATE.PASSPORT_SCHEMA,
            "ticket": "T-110",
        }
        passport = dict(body)
        passport["authentication_sha256"] = hmac.new(
            secret, STATE.canonical(body), hashlib.sha256
        ).hexdigest()
        passport["passport_sha256"] = hashlib.sha256(
            STATE.canonical(passport)
        ).hexdigest()
        STATE.write_atomic(passports / "T-110.json", passport)
        record = STATE.signed_repair({
            "blocked_receipt": blocked_receipt,
            "blocked_role": "builder",
            "branch": "ticket/T-110",
            "factory_sha": old_factory,
            "head_sha": old_head,
            "head_tree": run(
                "git", "rev-parse", f"{old_head}^{{tree}}", cwd=self.product
            ),
            "passport_sha256": old_passport,
            "repair_role": "test-author",
            "schema": STATE.REPAIR_SCHEMA,
            "ticket": "T-110",
        }, secret)
        STATE.write_atomic(STATE.repair_path(self.args), record)
        self.assertEqual(
            STATE.contract_repair_stage(self.args), ("FIX test-author", True)
        )

        body["migration_history"][0]["from_passport_sha256"] = "e" * 64
        passport = dict(body)
        passport["authentication_sha256"] = hmac.new(
            secret, STATE.canonical(body), hashlib.sha256
        ).hexdigest()
        passport["passport_sha256"] = hashlib.sha256(
            STATE.canonical(passport)
        ).hexdigest()
        STATE.write_atomic(passports / "T-110.json", passport)
        with self.assertRaisesRegex(
            STATE.StateError, "contract repair record is invalid"
        ):
            STATE.contract_repair_stage(self.args)

    def test_completed_repair_retires_after_terminal_export_lost_history(
        self,
    ) -> None:
        secret = b"k" * 32
        (self.state_dir / "passport.key").write_bytes(secret)
        os.chmod(self.state_dir / "passport.key", 0o600)
        passports = self.state_dir / "passports"
        passports.mkdir(mode=0o700)
        head = run("git", "rev-parse", "HEAD", cwd=self.product)
        old_factory = "b" * 40
        repair_factory = "c" * 40
        blocked_receipt = "d" * 64
        parent_file = "f" * 64
        receipt_body = {
            "branch": "ticket/T-110",
            "contract_version": self.args.contract_version,
            "factory_sha": repair_factory,
            "head_sha": head,
            "passport_sha256": "e" * 64,
            "project": self.args.project,
            "role": "test-author",
            "schema": STATE.RECEIPT_SCHEMA,
            "stage": "FIX test-author",
            "ticket": "T-110",
        }
        receipt_digest = hashlib.sha256(
            STATE.canonical(receipt_body)
        ).hexdigest()
        receipt = {
            **receipt_body,
            "consumed": True,
            "consumed_at_epoch": 1,
            "receipt_sha256": receipt_digest,
        }
        STATE.write_atomic(self.state_dir / "T-110.json", receipt)
        manifest = (
            "run_id=repair\nphase=completed\naccounting_state=completed\n"
            "ticket=T-110\nrole=test-author\nexit_status=0\nrole_exit=ok\n"
            f"kit_sha={repair_factory}\n"
            f"role_head_before={head}\n"
            f"transition_receipt_sha256={receipt_digest}\n"
        ).encode()
        (self.product / "factory/runs/repair.meta").write_bytes(manifest)
        manifest_digest = hashlib.sha256(manifest).hexdigest()
        completed = {
            "factory_sha": repair_factory,
            "head_before": head,
            "manifest_sha256": manifest_digest,
            "role": "test-author",
            "run_id": "repair",
            "transition_receipt_sha256": receipt_digest,
        }
        body = {
            "branch": "ticket/T-110",
            "charge_records": [
                {
                    "role": "builder",
                    "transition_receipt_sha256": blocked_receipt,
                },
                dict(completed),
            ],
            "completed_role_evidence": [dict(completed)],
            "current_stage": "FIX test-author",
            "factory_release_history": [
                {
                    "contract_version": self.args.contract_version,
                    "factory_sha": old_factory,
                },
                {
                    "contract_version": self.args.contract_version,
                    "factory_sha": repair_factory,
                },
                {
                    "contract_version": self.args.contract_version,
                    "factory_sha": self.args.factory_sha,
                },
            ],
            "factory_sha": self.args.factory_sha,
            "head_sha": head,
            "migration_history": [{
                "from_factory_sha": repair_factory,
                "from_head_sha": head,
                "from_passport_file_sha256": parent_file,
                "from_passport_sha256": "9" * 64,
                "from_protected_base_sha": "1" * 40,
                "from_route_plan_sha256": "2" * 64,
                "schema": STATE.PASSPORT_MIGRATION_SCHEMA,
                "to_factory_sha": self.args.factory_sha,
                "to_head_sha": head,
                "to_protected_base_sha": "3" * 40,
                "to_route_plan_sha256": "4" * 64,
            }],
            "parent_digest": "9" * 64,
            "parent_file_sha256": parent_file,
            "protected_base_sha": "3" * 40,
            "route_plan_sha256": "4" * 64,
            "schema": STATE.PASSPORT_SCHEMA,
            "ticket": "T-110",
            "transition_receipt_sha256": receipt_digest,
        }
        passport = dict(body)
        passport["authentication_sha256"] = hmac.new(
            secret, STATE.canonical(body), hashlib.sha256
        ).hexdigest()
        passport["passport_sha256"] = hashlib.sha256(
            STATE.canonical(passport)
        ).hexdigest()
        STATE.write_atomic(passports / "T-110.json", passport)
        record = STATE.signed_repair({
            "blocked_receipt": blocked_receipt,
            "blocked_role": "builder",
            "branch": "ticket/T-110",
            "factory_sha": old_factory,
            "head_sha": head,
            "head_tree": run(
                "git", "rev-parse", "HEAD^{tree}", cwd=self.product
            ),
            "passport_sha256": "c" * 64,
            "repair_role": "test-author",
            "schema": STATE.REPAIR_SCHEMA,
            "ticket": "T-110",
        }, secret)
        active = STATE.repair_path(self.args)
        STATE.write_atomic(active, record)

        tampered_body = {**body, "parent_digest": "8" * 64}
        tampered = dict(tampered_body)
        tampered["authentication_sha256"] = hmac.new(
            secret, STATE.canonical(tampered_body), hashlib.sha256
        ).hexdigest()
        tampered["passport_sha256"] = hashlib.sha256(
            STATE.canonical(tampered)
        ).hexdigest()
        STATE.write_atomic(passports / "T-110.json", tampered)
        with self.assertRaisesRegex(
            STATE.StateError, "contract repair record is invalid"
        ):
            STATE.contract_repair_stage(self.args)
        STATE.write_atomic(passports / "T-110.json", passport)

        with mock.patch.object(STATE, "resolve", return_value="RUN builder"):
            self.assertEqual(
                STATE.contract_repair_stage(self.args),
                ("RUN builder", False),
            )
        self.assertFalse(active.exists())
        archived = list((active.parent / "completed").glob("T-110-*.json"))
        self.assertEqual(len(archived), 1)
        self.assertEqual(json.loads(archived[0].read_text()), record)
        self.assertEqual(STATE.contract_repair_stage(self.args), (None, False))

    def test_runner_keeps_host_project_for_pre_go_receipt_check(self) -> None:
        source = (ROOT / "scripts/run-agent.sh").read_text(encoding="utf-8")
        start = source.index("sequencer_allows_role() {")
        function = source[start : source.index("\n}\n", start) + 3]
        capture = next(
            line for line in source.splitlines()
            if line.startswith('readonly TRANSITION_PROJECT=')
        )
        kit = self.root / "kit"
        (kit / "scripts").mkdir(parents=True)
        trace = self.root / "trace.json"
        (kit / "scripts/state-machine.py").write_text(
            "import json, os, sys\n"
            "json.dump({'argv': sys.argv[1:], "
            "'factory_project': os.environ.get('FACTORY_PROJECT')}, "
            "open(os.environ['TRACE'], 'w'))\n",
            encoding="utf-8",
        )
        script = f"""
set -euo pipefail
{function}
FACTORY_PROJECT=relay
{capture}
unset FACTORY_PROJECT
PROVIDER_CONTRACT_VERSION=1.8.0
FACTORY_TRANSITION_RECEIPT_SHA256={'a' * 64}
FACTORY_TRANSITION_STATE_DIR=/state
REPO_ROOT=/product
WORKDIR=/cell
KIT_DIR={kit}
TICKET=T-110
FACTORY_KIT_SHA={'b' * 40}
ROLE=planner
DISPATCH_LEASE_ID=
FACTORY_TRUSTED_PRODUCT_ORIGIN=test-origin
SEQUENCER_ERROR=
sequencer_allows_role
"""
        environment = os.environ.copy()
        environment.pop("FACTORY_PROJECT", None)
        environment.pop("TRANSITION_PROJECT", None)
        environment["TRACE"] = str(trace)
        subprocess.run(["bash", "-c", script], check=True, env=environment)
        result = json.loads(trace.read_text(encoding="utf-8"))
        project = result["argv"].index("--project")
        self.assertEqual(result["argv"][project + 1], "relay")
        self.assertIsNone(result["factory_project"])


if __name__ == "__main__":
    unittest.main()
