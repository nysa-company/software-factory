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
                "protected_terminal",
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
            mock.patch.object(STATE, "protected_terminal", return_value={}),
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
        with mock.patch.object(STATE, "resolve", return_value="RUN builder"):
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
