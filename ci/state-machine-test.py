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
import shutil
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

    def test_role_prompts_reject_identity_transformed_fixtures(self) -> None:
        prompts = {
            "planner": "An identity transformation is a contract contradiction",
            "spec-linter": "byte-identical to an accepted valid fixture",
            "test-author": "byte-identical to a valid fixture",
        }
        for role, rule in prompts.items():
            self.assertIn(rule, (ROOT / "roles" / f"{role}.md").read_text())

    def test_role_prompts_reject_unproducible_generated_values(self) -> None:
        prompts = {
            "planner": "evaluate its first generated value",
            "spec-linter": "a repair scope that excludes its required setup correction",
            "test-author": "the repair scope forbids the setup correction",
        }
        for role, rule in prompts.items():
            self.assertIn(rule, (ROOT / "roles" / f"{role}.md").read_text())

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
        self.assertIsNone(
            STATE.stage_role(
                "ESCALATE evidence bundle remained invalid after one Narrator retry"
            )
        )
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

    def test_resolver_receives_authenticated_passport_role_sequence(self) -> None:
        release = self.root / ("b" * 40)
        shutil.copytree(ROOT / "scripts", release / "scripts")
        (release / "integrations/hermes").mkdir(parents=True)
        shutil.copy2(
            ROOT / "integrations/hermes/contract.json",
            release / "integrations/hermes/contract.json",
        )
        release_tree = run(
            "/bin/bash", "-c",
            'source "$1"; factory_directory_tree "$2"',
            "_", str(release / "scripts/lib/kit-pin.sh"), str(release),
            cwd=self.root,
        )
        self.args.factory_sha = release.name
        self.args.kit_dir = release
        ticket = self.product / "factory/tickets/T-110.md"
        ticket.write_text(
            f"# T-110\n\nState: Building\nKit-SHA: {release.name}\n\n"
            "SPEC-LINT: PASS\n",
            encoding="utf-8",
        )
        (self.product / "factory/KIT_PIN").write_text(
            f"{release.name}\n", encoding="utf-8"
        )
        (self.product / "factory/ENVELOPE.env").write_text(
            "PER_RUN_BUDGET_USD=2.00\n"
            "PER_TICKET_BUDGET_USD=25.00\n"
            "PER_RUN_MAX_TURNS=5\n"
            "PER_RUN_TIMEOUT_MIN=1\n"
            "DAILY_CAP_USD=100.00\n",
            encoding="utf-8",
        )
        ledger = self.product / "factory/runtime-ledger.csv"
        ledger.write_text(
            "date,time,ticket,role,adapter,prompt_version,turns,cost_usd,"
            "exit_status,run_id,provider_family,model_id,selection_reason,"
            "cost_basis,adapter_version\n",
            encoding="utf-8",
        )
        durable_ledger = self.product / "factory/ledger.csv"
        shutil.copy2(ledger, durable_ledger)
        run("git", "add", ".", cwd=self.product)
        run("git", "commit", "-qm", "prepare building boundary", cwd=self.product)

        secret = b"k" * 32
        (self.state_dir / "passport.key").write_bytes(secret)
        os.chmod(self.state_dir / "passport.key", 0o600)
        passports = self.state_dir / "passports"
        passports.mkdir(mode=0o700)
        route = self.product / "factory/route-plans/T-110.json"
        records = []
        def add_role(role: str) -> None:
            index = len(records) + 1
            records.append({
                "contract_version": "1.8.0",
                "factory_sha": f"{index:040x}",
                "head_before": run("git", "rev-parse", "HEAD", cwd=self.product),
                "manifest_sha256": f"{index:064x}",
                "output_sha256": f"{index + 100:064x}",
                "role": role,
                "run_id": f"historical-{index}",
                "transition_receipt_sha256": f"{index + 200:064x}",
            })

        def write_passport() -> None:
            body = {
                "branch": "ticket/T-110",
                "completed_role_evidence": records,
                "contract_version": "1.8.0",
                "factory_sha": self.args.factory_sha,
                "head_sha": run("git", "rev-parse", "HEAD", cwd=self.product),
                "project": "relay",
                "route_plan_sha256": hashlib.sha256(route.read_bytes()).hexdigest(),
                "schema": STATE.PASSPORT_SCHEMA,
                "ticket": "T-110",
            }
            signed = dict(body)
            signed["authentication_sha256"] = hmac.new(
                secret, STATE.canonical(body), hashlib.sha256
            ).hexdigest()
            signed["passport_sha256"] = hashlib.sha256(
                STATE.canonical(signed)
            ).hexdigest()
            STATE.write_atomic(passports / "T-110.json", signed)

        for role in ("planner", "spec-linter", "test-author"):
            add_role(role)
        write_passport()

        with mock.patch.dict(os.environ, {
            "FACTORY_RELEASE_CONTRACT_VERSION": "1.8.0",
            "FACTORY_RELEASE_PATH": str(release),
            "FACTORY_RELEASE_TREE": release_tree,
            "FACTORY_LEDGER": str(ledger),
            "FACTORY_DURABLE_LEDGER": str(durable_ledger),
        }):
            self.assertEqual(STATE.resolve(self.args), "RUN builder")
            add_role("planner")
            write_passport()
            self.assertEqual(STATE.resolve(self.args), "RUN spec-linter")
            add_role("spec-linter")
            ticket.write_text(
                ticket.read_text(encoding="utf-8")
                + "SPEC-LINT: FAIL — repair is incomplete\n",
                encoding="utf-8",
            )
            run("git", "add", str(ticket), cwd=self.product)
            run("git", "commit", "-qm", "reject repaired contract", cwd=self.product)
            write_passport()
            self.assertEqual(STATE.resolve(self.args), "RUN planner")
            add_role("planner")
            write_passport()
            self.assertEqual(STATE.resolve(self.args), "RUN spec-linter")
            add_role("spec-linter")
            ticket.write_text(
                ticket.read_text(encoding="utf-8") + "SPEC-LINT: PASS\n",
                encoding="utf-8",
            )
            run("git", "add", str(ticket), cwd=self.product)
            run("git", "commit", "-qm", "record repaired spec lint", cwd=self.product)
            write_passport()
            self.assertEqual(STATE.resolve(self.args), "RUN test-author")
            add_role("test-author")
            write_passport()
            self.assertEqual(STATE.resolve(self.args), "RUN builder")
            add_role("builder")
            write_passport()
            self.assertEqual(STATE.resolve(self.args), "RUN reviewer")
        self.assertEqual(list(self.state_dir.glob(".role-evidence-*")), [])

    def test_narrator_bundle_decisions_are_scoped_to_latest_review_generation(
        self,
    ) -> None:
        release = self.root / ("c" * 40)
        shutil.copytree(ROOT / "scripts", release / "scripts")
        (release / "integrations/hermes").mkdir(parents=True)
        shutil.copy2(
            ROOT / "integrations/hermes/contract.json",
            release / "integrations/hermes/contract.json",
        )
        release_tree = run(
            "/bin/bash", "-c",
            'source "$1"; factory_directory_tree "$2"',
            "_", str(release / "scripts/lib/kit-pin.sh"), str(release),
            cwd=self.root,
        )
        self.args.factory_sha = release.name
        self.args.kit_dir = release
        (self.product / "factory/KIT_PIN").write_text(
            f"{release.name}\n", encoding="utf-8"
        )
        (self.product / "factory/ENVELOPE.env").write_text(
            "PER_RUN_BUDGET_USD=2.00\n"
            "PER_TICKET_BUDGET_USD=100.00\n"
            "PER_RUN_MAX_TURNS=5\n"
            "PER_RUN_TIMEOUT_MIN=1\n"
            "DAILY_CAP_USD=300.00\n",
            encoding="utf-8",
        )
        ledger = self.product / "factory/runtime-ledger.csv"
        ledger.write_text(
            "date,time,ticket,role,adapter,prompt_version,turns,cost_usd,"
            "exit_status,run_id,provider_family,model_id,selection_reason,"
            "cost_basis,adapter_version\n",
            encoding="utf-8",
        )
        durable_ledger = self.product / "factory/ledger.csv"
        shutil.copy2(ledger, durable_ledger)
        secret = b"n" * 32
        (self.state_dir / "passport.key").write_bytes(secret)
        os.chmod(self.state_dir / "passport.key", 0o600)
        passports = self.state_dir / "passports"
        passports.mkdir(mode=0o700)
        route = self.product / "factory/route-plans/T-110.json"
        ticket = self.product / "factory/tickets/T-110.md"
        bundle = self.product / "factory/tickets/T-110-bundle.md"
        attestation = self.product / "factory/attestations/T-110/bundle.json"
        prefix = ("planner", "spec-linter", "test-author", "builder")
        valid_bundle = (
            "# What this does\n# Preview\n# Screenshots\n"
            "# Acceptance criteria\n# Risk\n# Cost\n# Rollback\n"
            "Approve to merge?\n"
        )
        not_approvable = "NOT APPROVABLE: deployed preview is broken\n" + valid_bundle
        invalid_bundle = valid_bundle.replace("# Cost\n", "")
        cases = (
            (
                "unchanged explicit failure",
                ("reviewer", "narrator"),
                "reviewer round 1: APPROVE\n",
                not_approvable,
                False,
                "FIX builder",
            ),
            (
                "approved repair makes old failure stale",
                ("reviewer", "narrator", "builder", "reviewer"),
                "reviewer round 1: APPROVE\nreviewer round 2: APPROVE\n",
                not_approvable,
                False,
                "RUN narrator",
            ),
            (
                "fresh explicit failure returns to repair",
                ("reviewer", "narrator", "builder", "reviewer", "narrator"),
                "reviewer round 1: APPROVE\nreviewer round 2: APPROVE\n",
                not_approvable,
                False,
                "FIX builder",
            ),
            (
                "rejected repair review cannot authorize narrator",
                ("reviewer", "narrator", "builder", "reviewer"),
                "reviewer round 1: APPROVE\n"
                "reviewer round 2: REQUEST CHANGES\n"
                "reviewer round 2 FIX-OWNER: builder\n",
                not_approvable,
                False,
                "FIX builder",
            ),
            (
                "void duplicate reviewer preserves narrator",
                ("reviewer", "narrator", "reviewer"),
                "reviewer round 1: APPROVE\n"
                "OPERATOR NOTE: reviewer run 2 void — duplicate\n",
                not_approvable,
                False,
                "FIX builder",
            ),
            (
                "stale valid attestation cannot bypass narrator",
                ("reviewer", "narrator", "builder", "reviewer"),
                "reviewer round 1: APPROVE\nreviewer round 2: APPROVE\n",
                valid_bundle,
                True,
                "RUN narrator",
            ),
            (
                "fresh valid bundle awaits operator",
                ("reviewer", "narrator", "builder", "reviewer", "narrator"),
                "reviewer round 1: APPROVE\nreviewer round 2: APPROVE\n",
                valid_bundle,
                False,
                "AWAIT-OPERATOR bundle posted; operator approval + merge is the next step",
            ),
            (
                "one malformed bundle correction",
                ("reviewer", "narrator"),
                "reviewer round 1: APPROVE\n",
                invalid_bundle,
                False,
                "RUN narrator",
            ),
            (
                "malformed bundle correction exhausted",
                ("reviewer", "narrator", "narrator"),
                "reviewer round 1: APPROVE\n",
                invalid_bundle,
                False,
                "ESCALATE evidence bundle remained invalid after one Narrator retry",
            ),
        )

        for case_index, (
            name, suffix, verdicts, bundle_text, has_attestation, expected,
        ) in enumerate(cases, 1):
            with self.subTest(name=name):
                ticket.write_text(
                    f"# T-110\n\nState: Review\nKit-SHA: {release.name}\n"
                    f"SPEC-LINT: PASS\n{verdicts}",
                    encoding="utf-8",
                )
                bundle.write_text(bundle_text, encoding="utf-8")
                if has_attestation:
                    attestation.parent.mkdir(parents=True, exist_ok=True)
                    attestation.write_text("{}\n", encoding="utf-8")
                elif attestation.parent.exists():
                    shutil.rmtree(attestation.parent)
                run("git", "add", "-A", cwd=self.product)
                run(
                    "git", "commit", "--allow-empty", "-qm",
                    f"generation case {case_index}",
                    cwd=self.product,
                )
                head = run("git", "rev-parse", "HEAD", cwd=self.product)
                roles = prefix + suffix
                records = []
                for index, role in enumerate(roles, 1):
                    records.append({
                        "contract_version": "1.8.0",
                        "factory_sha": f"{index:040x}",
                        "head_before": head,
                        "manifest_sha256": f"{index:064x}",
                        "output_sha256": f"{index + 100:064x}",
                        "role": role,
                        "run_id": f"case-{case_index}-run-{index}",
                        "transition_receipt_sha256": f"{index + 200:064x}",
                    })
                body = {
                    "branch": "ticket/T-110",
                    "completed_role_evidence": records,
                    "contract_version": "1.8.0",
                    "factory_sha": self.args.factory_sha,
                    "head_sha": head,
                    "project": "relay",
                    "route_plan_sha256": hashlib.sha256(route.read_bytes()).hexdigest(),
                    "schema": STATE.PASSPORT_SCHEMA,
                    "ticket": "T-110",
                }
                signed = dict(body)
                signed["authentication_sha256"] = hmac.new(
                    secret, STATE.canonical(body), hashlib.sha256
                ).hexdigest()
                signed["passport_sha256"] = hashlib.sha256(
                    STATE.canonical(signed)
                ).hexdigest()
                STATE.write_atomic(passports / "T-110.json", signed)
                with mock.patch.dict(os.environ, {
                    "FACTORY_RELEASE_CONTRACT_VERSION": "1.8.0",
                    "FACTORY_RELEASE_PATH": str(release),
                    "FACTORY_RELEASE_TREE": release_tree,
                    "FACTORY_LEDGER": str(ledger),
                    "FACTORY_DURABLE_LEDGER": str(durable_ledger),
                }):
                    self.assertEqual(STATE.resolve(self.args), expected)
                self.assertEqual(list(self.state_dir.glob(".role-evidence-*")), [])

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

    def test_replay_after_committed_role_transition_preserves_narrator_evidence(
        self,
    ) -> None:
        receipt = "b" * 64
        evidence = self.product / "factory/tickets/T-110-evidence/narrator.txt"
        evidence.parent.mkdir(parents=True)
        evidence.write_text(
            "NOT APPROVABLE: deployed preview is broken\n", encoding="utf-8"
        )
        before = evidence.read_bytes()
        with (
            mock.patch.object(
                STATE,
                "current_state",
                side_effect=["Building", "Building"],
            ),
            mock.patch.object(
                STATE, "contract_repair_stage", return_value=(None, False)
            ),
            mock.patch.object(
                STATE, "resolve", return_value="FIX builder"
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
        transition.assert_not_called()
        migrate.assert_called_once_with(self.args)
        issue.assert_called_once_with(self.args, "FIX builder")
        self.assertEqual(evidence.read_bytes(), before)
        self.assertEqual(result["receipt"], receipt)
        self.assertEqual(result["role"], "builder")
        self.assertEqual(result["stage"], "FIX builder")

    def test_mock_role_transition_matrix_covers_every_lifecycle_state(self) -> None:
        targets = {
            "planner": "Planning",
            "spec-linter": "Planning",
            "test-author": "Building",
            "builder": "Building",
            "reviewer": "Review",
            "narrator": "Review",
        }
        paths = {
            ("Ready", "Planning"): ["Planning"],
            ("Ready", "Building"): ["Planning", "Building"],
            ("Ready", "Review"): ["Planning", "Building", "Review"],
            ("Planning", "Planning"): [],
            ("Planning", "Building"): ["Building"],
            ("Planning", "Review"): ["Building", "Review"],
            ("Building", "Building"): [],
            ("Building", "Review"): ["Review"],
            ("Review", "Building"): ["Building"],
            ("Review", "Review"): [],
        }
        receipt = "b" * 64

        for action in ("RUN", "FIX"):
            for role, target in targets.items():
                for current in ("Ready", "Planning", "Building", "Review"):
                    expected = paths.get((current, target))
                    with self.subTest(
                        action=action, role=role, current=current, target=target
                    ):
                        states = [current, current, *(expected or [])]
                        with (
                            mock.patch.object(
                                STATE, "current_state", side_effect=states
                            ),
                            mock.patch.object(
                                STATE,
                                "contract_repair_stage",
                                return_value=(None, False),
                            ),
                            mock.patch.object(
                                STATE,
                                "resolve",
                                return_value=f"{action} {role}",
                            ),
                            mock.patch.object(STATE, "transition") as transition,
                            mock.patch.object(STATE, "migrate_passport") as migrate,
                            mock.patch.object(
                                STATE,
                                "issue",
                                return_value={"receipt_sha256": receipt},
                            ) as issue,
                        ):
                            if expected is None:
                                with self.assertRaisesRegex(
                                    STATE.StateError,
                                    f"state machine cannot enter {target} from {current}",
                                ):
                                    STATE.next_transition(self.args)
                                migrate.assert_not_called()
                                issue.assert_not_called()
                            else:
                                result = STATE.next_transition(self.args)
                                self.assertEqual(
                                    [call.args[1] for call in transition.call_args_list],
                                    expected,
                                )
                                migrate.assert_called_once_with(self.args)
                                issue.assert_called_once_with(
                                    self.args, f"{action} {role}"
                                )
                                self.assertEqual(result["role"], role)
                                self.assertEqual(
                                    result["stage"], f"{action} {role}"
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
        self.args.receipt = "b" * 64
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
            + "\nOPERATOR RESUME: test-author\n"
            + f"OPERATOR RESUME RECEIPT: {self.args.receipt}\n",
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

    def test_operator_resume_replaces_one_prior_owner_exactly(self) -> None:
        prior_receipt = "a" * 64
        self.args.receipt = "b" * 64
        path = self.product / "factory/tickets/T-110.md"
        path.write_text(
            path.read_text(encoding="utf-8").rstrip("\n")
            + "\n\nOPERATOR RESUME: test-author\n"
            + f"OPERATOR RESUME RECEIPT: {prior_receipt}\n",
            encoding="utf-8",
        )
        run("git", "add", str(path), cwd=self.product)
        run("git", "commit", "-qm", "authorize prior test repair", cwd=self.product)
        blocked_head = run("git", "rev-parse", "HEAD", cwd=self.product)
        passport = {
            "branch": "ticket/T-110",
            "factory_sha": self.args.factory_sha,
            "head_sha": blocked_head,
            "ticket": "T-110",
        }
        path.write_text(
            path.read_text(encoding="utf-8")
            .replace("OPERATOR RESUME: test-author", "OPERATOR RESUME: planner")
            .replace(prior_receipt, self.args.receipt),
            encoding="utf-8",
        )
        run("git", "add", str(path), cwd=self.product)
        run("git", "commit", "-qm", "route contract repair to planner", cwd=self.product)

        self.assertEqual(
            STATE.operator_resume_role(self.args, passport, "test-author"),
            "planner",
        )

        (self.product / "unexpected").write_text("drift\n", encoding="utf-8")
        run("git", "add", "unexpected", cwd=self.product)
        run("git", "commit", "-qm", "add unrelated drift", cwd=self.product)
        with self.assertRaisesRegex(
            STATE.StateError, "operator directive is invalid"
        ):
            STATE.operator_resume_role(self.args, passport, "test-author")

    def test_operator_resume_upgrades_one_legacy_owner_exactly(self) -> None:
        self.args.receipt = "b" * 64
        path = self.product / "factory/tickets/T-110.md"
        path.write_text(
            path.read_text(encoding="utf-8").rstrip("\n")
            + "\n\nOPERATOR RESUME: test-author\n",
            encoding="utf-8",
        )
        run("git", "add", str(path), cwd=self.product)
        run("git", "commit", "-qm", "preserve legacy repair owner", cwd=self.product)
        blocked_head = run("git", "rev-parse", "HEAD", cwd=self.product)
        passport = {
            "branch": "ticket/T-110",
            "factory_sha": self.args.factory_sha,
            "head_sha": blocked_head,
            "ticket": "T-110",
        }
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                "OPERATOR RESUME: test-author",
                "OPERATOR RESUME: planner\n"
                f"OPERATOR RESUME RECEIPT: {self.args.receipt}",
            ),
            encoding="utf-8",
        )
        run("git", "add", str(path), cwd=self.product)
        run(
            "git", "commit", "-qm", "bind legacy repair to current receipt",
            cwd=self.product,
        )

        self.assertEqual(
            STATE.operator_resume_role(self.args, passport, "test-author"),
            "planner",
        )

        path.write_text(
            path.read_text(encoding="utf-8") + "\nUnrelated: drift\n",
            encoding="utf-8",
        )
        run("git", "add", str(path), cwd=self.product)
        run("git", "commit", "-qm", "add unrelated directive drift", cwd=self.product)
        passport["head_sha"] = run("git", "rev-parse", "HEAD", cwd=self.product)
        with self.assertRaisesRegex(
            STATE.StateError, "operator directive is invalid"
        ):
            STATE.operator_resume_role(self.args, passport, "test-author")

    def test_operator_resume_uses_current_passport_repair_window(self) -> None:
        historical_receipt = "a" * 64
        self.args.receipt = "b" * 64
        path = self.product / "factory/tickets/T-110.md"
        original = path.read_text(encoding="utf-8")
        directive = "OPERATOR RESUME: test-author"
        receipt_directive = (
            f"OPERATOR RESUME RECEIPT: {self.args.receipt}"
        )

        path.write_text(
            original.rstrip("\n")
            + f"\n\n{directive}\n"
            + f"OPERATOR RESUME RECEIPT: {historical_receipt}\n",
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
            + f"\n\n{directive}\n{receipt_directive}\n",
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
                f"\n\n{directive}\n{receipt_directive}\n", "\n", 1
            ),
            encoding="utf-8",
        )
        run("git", "add", str(path), cwd=self.product)
        run("git", "commit", "-qm", "withdraw current test repair", cwd=self.product)
        withdrawn_head = run("git", "rev-parse", "HEAD", cwd=self.product)
        path.write_text(
            path.read_text(encoding="utf-8").rstrip("\n")
            + f"\n\n{directive}\n{receipt_directive}\n",
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

    def test_operator_resume_ignores_authenticated_receipt_withdrawal(
        self,
    ) -> None:
        self.args.receipt = "b" * 64
        path = self.product / "factory/tickets/T-110.md"

        path.write_text(
            path.read_text(encoding="utf-8").rstrip("\n")
            + "\n\nOperator note: adjudication is pending.\n",
            encoding="utf-8",
        )
        run("git", "add", str(path), cwd=self.product)
        run("git", "commit", "-qm", "record operator context", cwd=self.product)
        note_head = run("git", "rev-parse", "HEAD", cwd=self.product)

        path.write_text(
            path.read_text(encoding="utf-8").rstrip("\n")
            + "\n\nOPERATOR RESUME: builder\n"
            + f"OPERATOR RESUME RECEIPT: {self.args.receipt}\n",
            encoding="utf-8",
        )
        run("git", "add", str(path), cwd=self.product)
        run("git", "commit", "-qm", "premature receipt binding", cwd=self.product)
        first_binding = run("git", "rev-parse", "HEAD", cwd=self.product)

        path.write_text(
            path.read_text(encoding="utf-8").replace(
                f"OPERATOR RESUME RECEIPT: {self.args.receipt}\n", ""
            ),
            encoding="utf-8",
        )
        run("git", "add", str(path), cwd=self.product)
        run("git", "commit", "-qm", "withdraw receipt binding", cwd=self.product)
        withdrawn = run("git", "rev-parse", "HEAD", cwd=self.product)

        path.write_text(
            path.read_text(encoding="utf-8").replace(
                "OPERATOR RESUME: builder\n",
                "OPERATOR RESUME: builder\n"
                f"OPERATOR RESUME RECEIPT: {self.args.receipt}\n",
            ),
            encoding="utf-8",
        )
        run("git", "add", str(path), cwd=self.product)
        run("git", "commit", "-qm", "bind authenticated receipt", cwd=self.product)
        final_binding = run("git", "rev-parse", "HEAD", cwd=self.product)

        passport = {
            "branch": "ticket/T-110",
            "factory_sha": self.args.factory_sha,
            "head_sha": withdrawn,
            "migration_history": [
                {
                    "from_head_sha": first_binding,
                    "to_head_sha": withdrawn,
                }
            ],
            "ticket": "T-110",
        }
        self.assertNotIn(note_head, {
            item["from_head_sha"]
            for item in passport["migration_history"]
        })
        self.assertEqual(
            run("git", "rev-parse", f"{final_binding}^", cwd=self.product),
            withdrawn,
        )
        self.assertEqual(
            STATE.operator_resume_role(self.args, passport, "builder"),
            "builder",
        )

    def test_backward_contract_repair_keeps_coarse_state_and_runs_owner(
        self,
    ) -> None:
        ticket = self.product / "factory/tickets/T-110.md"
        ticket.write_text(
            "# T-110\n\nState: Building\nResume-State: Building\n",
            encoding="utf-8",
        )
        head = run("git", "rev-parse", "HEAD", cwd=self.product)
        passport = {
            "branch": "ticket/T-110",
            "factory_sha": self.args.factory_sha,
            "head_sha": head,
            "passport_sha256": "e" * 64,
            "ticket": "T-110",
        }
        self.args.receipt = "b" * 64
        with (
            mock.patch.object(
                STATE, "contract_blocked_receipt", return_value="test-author"
            ),
            mock.patch.object(
                STATE,
                "authenticated_passport",
                return_value=(passport, b"k" * 32),
            ),
            mock.patch.object(
                STATE, "operator_resume_role", return_value="planner"
            ),
            mock.patch.object(STATE, "current_state", return_value="Building"),
            mock.patch.object(STATE, "transition") as transition,
            mock.patch.object(STATE, "migrate_passport") as migrate,
        ):
            result = STATE.resume_transition(self.args)

        transition.assert_not_called()
        migrate.assert_called_once_with(self.args)
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["repair_role"], "planner")
        self.assertEqual(
            STATE.load_repair(self.args, b"k" * 32)["repair_role"],
            "planner",
        )

    def test_backward_contract_repair_blocks_and_resumes_at_coarse_state(
        self,
    ) -> None:
        ticket = self.product / "factory/tickets/T-110.md"
        ticket.write_text(
            "# T-110\n\nState: Building\nResume-State: Building\n",
            encoding="utf-8",
        )
        self.args.receipt = "b" * 64
        passport = {
            "branch": "ticket/T-110",
            "factory_sha": self.args.factory_sha,
            "head_sha": run("git", "rev-parse", "HEAD", cwd=self.product),
            "passport_sha256": "e" * 64,
            "ticket": "T-110",
        }

        with (
            mock.patch.object(
                STATE, "contract_blocked_receipt", return_value="planner"
            ),
            mock.patch.object(
                STATE, "contract_repair_stage", return_value=(None, False)
            ),
            mock.patch.object(STATE, "transition") as transition,
        ):
            with self.assertRaisesRegex(
                STATE.StateError, "contract blocker role state drifted"
            ):
                STATE.block_transition(self.args)
        transition.assert_not_called()

        def block(_args, _state):
            ticket.write_text(
                "# T-110\n\nState: Blocked-Escalated\n"
                "Resume-State: Building\n",
                encoding="utf-8",
            )

        with (
            mock.patch.object(
                STATE, "contract_blocked_receipt", return_value="planner"
            ),
            mock.patch.object(
                STATE,
                "contract_repair_stage",
                return_value=("FIX planner", True),
            ),
            mock.patch.object(STATE, "run_helper") as materialize,
            mock.patch.object(STATE, "transition", side_effect=block),
            mock.patch.object(STATE, "migrate_passport") as migrate,
        ):
            result = STATE.block_transition(self.args)
        self.assertEqual(result["status"], "blocked")
        materialize.assert_not_called()
        migrate.assert_called_once_with(self.args)

        with (
            mock.patch.object(
                STATE, "contract_blocked_receipt", return_value="planner"
            ),
            mock.patch.object(
                STATE,
                "contract_repair_stage",
                return_value=("FIX planner", True),
            ),
            mock.patch.object(STATE, "transition") as transition,
            mock.patch.object(STATE, "migrate_passport") as migrate,
        ):
            result = STATE.block_transition(self.args)
        self.assertEqual(result["status"], "blocked")
        transition.assert_not_called()
        migrate.assert_called_once_with(self.args)

        def resume(*_args, **_kwargs):
            ticket.write_text(
                "# T-110\n\nState: Building\nResume-State: Building\n",
                encoding="utf-8",
            )
            return ""

        with (
            mock.patch.object(
                STATE, "contract_blocked_receipt", return_value="planner"
            ),
            mock.patch.object(
                STATE,
                "authenticated_passport",
                return_value=(passport, b"k" * 32),
            ),
            mock.patch.object(
                STATE, "operator_resume_role", return_value="planner"
            ),
            mock.patch.object(
                STATE,
                "contract_repair_stage",
                return_value=("FIX planner", True),
            ),
            mock.patch.object(STATE, "run_helper", side_effect=resume),
            mock.patch.object(STATE, "migrate_passport") as migrate,
        ):
            result = STATE.resume_transition(self.args)
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["repair_role"], "planner")
        migrate.assert_called_once_with(self.args)
        self.assertIn("State: Building", ticket.read_text(encoding="utf-8"))

    def test_completed_repair_authenticates_visible_historical_directive(
        self,
    ) -> None:
        self.args.receipt = "b" * 64
        secret = b"k" * 32
        (self.state_dir / "passport.key").write_bytes(secret)
        os.chmod(self.state_dir / "passport.key", 0o600)
        ticket = self.product / "factory/tickets/T-110.md"
        ticket.write_text(
            ticket.read_text(encoding="utf-8").rstrip("\n")
            + "\n\nOPERATOR RESUME: planner\n"
            + f"OPERATOR RESUME RECEIPT: {self.args.receipt}\n",
            encoding="utf-8",
        )
        run("git", "add", str(ticket), cwd=self.product)
        run("git", "commit", "-qm", "preserve consumed planner directive", cwd=self.product)
        head = run("git", "rev-parse", "HEAD", cwd=self.product)
        passports = self.state_dir / "passports"
        passports.mkdir(mode=0o700)
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
        completed = STATE.repair_path(self.args).parent / "completed"
        completed.mkdir(mode=0o700)
        record = STATE.signed_repair({
            "blocked_receipt": self.args.receipt,
            "blocked_role": "test-author",
            "branch": "ticket/T-110",
            "factory_sha": self.args.factory_sha,
            "head_sha": head,
            "head_tree": run(
                "git", "rev-parse", "HEAD^{tree}", cwd=self.product
            ),
            "passport_sha256": passport["passport_sha256"],
            "repair_role": "planner",
            "schema": STATE.REPAIR_SCHEMA,
            "ticket": "T-110",
        }, secret)
        STATE.write_atomic(
            completed / f"T-110-{record['repair_sha256']}.json",
            record,
        )

        self.assertEqual(STATE.contract_repair_stage(self.args), (None, False))
        with (
            mock.patch.object(STATE, "current_state", return_value="Building"),
            mock.patch.object(
                STATE, "resolve", return_value="RUN spec-linter"
            ) as resolve,
            mock.patch.object(STATE, "transition") as transition,
            mock.patch.object(STATE, "migrate_passport") as migrate,
        ):
            result = STATE.next_transition(self.args)

        resolve.assert_called_once_with(self.args)
        transition.assert_not_called()
        migrate.assert_called_once_with(self.args)
        self.assertEqual(result["stage"], "RUN spec-linter")

    def test_repeated_blocker_hands_back_to_earlier_owner_then_continues(
        self,
    ) -> None:
        prior_receipt = "a" * 64
        self.args.receipt = "b" * 64
        secret = b"k" * 32
        (self.state_dir / "passport.key").write_bytes(secret)
        os.chmod(self.state_dir / "passport.key", 0o600)
        ticket = self.product / "factory/tickets/T-110.md"
        ticket.write_text(
            "# T-110\n\nState: Building\nResume-State: Building\n\n"
            "OPERATOR RESUME: test-author\n"
            f"OPERATOR RESUME RECEIPT: {prior_receipt}\n",
            encoding="utf-8",
        )
        run("git", "add", str(ticket), cwd=self.product)
        run("git", "commit", "-qm", "materialize repeated test-author blocker", cwd=self.product)
        blocked_head = run("git", "rev-parse", "HEAD", cwd=self.product)
        passports = self.state_dir / "passports"
        passports.mkdir(mode=0o700)
        body = {
            "branch": "ticket/T-110",
            "factory_sha": self.args.factory_sha,
            "head_sha": blocked_head,
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

        with self.assertRaisesRegex(
            STATE.StateError, "receipt-bound operator directive"
        ):
            STATE.operator_resume_role(self.args, passport, "test-author")

        ticket.write_text(
            ticket.read_text(encoding="utf-8")
            .replace("OPERATOR RESUME: test-author", "OPERATOR RESUME: planner")
            .replace(prior_receipt, self.args.receipt),
            encoding="utf-8",
        )
        run("git", "add", str(ticket), cwd=self.product)
        run("git", "commit", "-qm", "route repeated blocker to planner", cwd=self.product)
        planner_head = run("git", "rev-parse", "HEAD", cwd=self.product)
        self.args.action = "resume"
        with (
            mock.patch.object(
                STATE, "contract_blocked_receipt", return_value="test-author"
            ),
            mock.patch.object(STATE, "transition") as transition,
            mock.patch.object(STATE, "migrate_passport") as migrate,
        ):
            result = STATE.resume_transition(self.args)
        transition.assert_not_called()
        migrate.assert_called_once_with(self.args)
        self.assertEqual(result["repair_role"], "planner")
        self.assertEqual(
            STATE.contract_repair_stage(self.args),
            ("FIX planner", True),
        )

        (self.product / "factory/runs/planner-repair.meta").write_text(
            "run_id=planner-repair\n"
            "phase=completed\n"
            "accounting_state=completed\n"
            "ticket=T-110\n"
            "role=planner\n"
            "exit_status=0\n"
            "role_exit=ok\n"
            f"role_head_before={planner_head}\n",
            encoding="utf-8",
        )
        with mock.patch.object(
            STATE, "resolve", return_value="RUN spec-linter"
        ):
            self.assertEqual(
                STATE.contract_repair_stage(self.args),
                ("RUN spec-linter", True),
            )
        with mock.patch.object(
            STATE, "resolve", return_value="RUN spec-linter"
        ):
            self.assertEqual(
                STATE.contract_repair_stage(self.args),
                ("RUN spec-linter", True),
            )

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
            "contract_version": self.args.contract_version,
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
            "product_origin_sha256": hashlib.sha256(
                b"test-origin"
            ).hexdigest(),
            "project": self.args.project,
            "route_plan_sha256": "4" * 64,
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

        body["migration_history"][0]["from_passport_sha256"] = old_passport
        receipt_body = {
            "branch": "ticket/T-110",
            "contract_version": self.args.contract_version,
            "factory_sha": self.args.factory_sha,
            "head_sha": current_head,
            "head_tree": run(
                "git", "rev-parse", f"{current_head}^{{tree}}", cwd=self.product
            ),
            "passport_sha256": "9" * 64,
            "product_origin_sha256": hashlib.sha256(
                b"test-origin"
            ).hexdigest(),
            "project": self.args.project,
            "role": "test-author",
            "schema": STATE.RECEIPT_SCHEMA,
            "stage": "FIX test-author",
            "ticket": "T-110",
        }
        receipt_digest = hashlib.sha256(
            STATE.canonical(receipt_body)
        ).hexdigest()
        STATE.write_atomic(
            self.state_dir / "T-110.json",
            {
                **receipt_body,
                "consumed": True,
                "consumed_at_epoch": 1,
                "receipt_sha256": receipt_digest,
            },
        )
        output_digest = "5" * 64
        manifest = (
            "run_id=migrated-repair\nphase=completed\n"
            "accounting_state=completed\n"
            f"contract_version={self.args.contract_version}\n"
            "ticket=T-110\nrole=test-author\nexit_status=0\nrole_exit=ok\n"
            f"kit_sha={self.args.factory_sha}\n"
            f"role_head_before={current_head}\n"
            "role_branch_before=ticket/T-110\n"
            f"transition_receipt_sha256={receipt_digest}\n"
            f"output_sha256={output_digest}\n"
            "go_issued=1\ntask_submitted=1\n"
        ).encode()
        (self.product / "factory/runs/migrated-repair.meta").write_bytes(
            manifest
        )
        manifest_digest = hashlib.sha256(manifest).hexdigest()
        ticket.write_text(
            ticket.read_text(encoding="utf-8")
            + "\nRepair result: contract clarified.\n",
            encoding="utf-8",
        )
        run("git", "add", str(ticket), cwd=self.product)
        run("git", "commit", "-qm", "complete migrated repair", cwd=self.product)
        output_head = run("git", "rev-parse", "HEAD", cwd=self.product)
        completed = {
            "contract_version": self.args.contract_version,
            "factory_sha": self.args.factory_sha,
            "head_before": current_head,
            "manifest_sha256": manifest_digest,
            "output_sha256": output_digest,
            "role": "test-author",
            "run_id": "migrated-repair",
            "transition_receipt_sha256": receipt_digest,
        }
        body.update({
            "charge_records": [
                {
                    "role": "builder",
                    "transition_receipt_sha256": blocked_receipt,
                },
                {
                    **completed,
                    "accounting_state": "completed",
                    "charge_micro_usd": 1,
                },
            ],
            "completed_role_evidence": [dict(completed)],
            "current_stage": "FIX test-author",
            "head_sha": output_head,
            "parent_file_sha256": receipt_body["passport_sha256"],
            "transition_receipt_sha256": receipt_digest,
        })
        wrong_parent_body = {
            **body,
            "parent_file_sha256": "8" * 64,
        }
        wrong_parent = dict(wrong_parent_body)
        wrong_parent["authentication_sha256"] = hmac.new(
            secret, STATE.canonical(wrong_parent_body), hashlib.sha256
        ).hexdigest()
        wrong_parent["passport_sha256"] = hashlib.sha256(
            STATE.canonical(wrong_parent)
        ).hexdigest()
        STATE.write_atomic(passports / "T-110.json", wrong_parent)
        with self.assertRaisesRegex(
            STATE.StateError, "contract repair record is invalid"
        ):
            STATE.contract_repair_stage(self.args)

        missing_charge_body = {
            **body,
            "charge_records": body["charge_records"][:1],
        }
        missing_charge = dict(missing_charge_body)
        missing_charge["authentication_sha256"] = hmac.new(
            secret, STATE.canonical(missing_charge_body), hashlib.sha256
        ).hexdigest()
        missing_charge["passport_sha256"] = hashlib.sha256(
            STATE.canonical(missing_charge)
        ).hexdigest()
        STATE.write_atomic(passports / "T-110.json", missing_charge)
        with self.assertRaisesRegex(
            STATE.StateError, "contract repair record is invalid"
        ):
            STATE.contract_repair_stage(self.args)

        invalid_charge_body = {
            **body,
            "charge_records": [
                body["charge_records"][0],
                {
                    **body["charge_records"][1],
                    "charge_micro_usd": True,
                },
            ],
        }
        invalid_charge = dict(invalid_charge_body)
        invalid_charge["authentication_sha256"] = hmac.new(
            secret, STATE.canonical(invalid_charge_body), hashlib.sha256
        ).hexdigest()
        invalid_charge["passport_sha256"] = hashlib.sha256(
            STATE.canonical(invalid_charge)
        ).hexdigest()
        STATE.write_atomic(passports / "T-110.json", invalid_charge)
        with self.assertRaisesRegex(
            STATE.StateError, "contract repair record is invalid"
        ):
            STATE.contract_repair_stage(self.args)

        passport = dict(body)
        passport["authentication_sha256"] = hmac.new(
            secret, STATE.canonical(body), hashlib.sha256
        ).hexdigest()
        passport["passport_sha256"] = hashlib.sha256(
            STATE.canonical(passport)
        ).hexdigest()
        STATE.write_atomic(passports / "T-110.json", passport)
        with mock.patch.object(STATE, "resolve", return_value="RUN builder"):
            self.assertEqual(
                STATE.contract_repair_stage(self.args),
                ("RUN builder", False),
            )
        self.assertFalse(STATE.repair_path(self.args).exists())

        # A Factory/route upgrade after the successful role must retain the
        # same terminal evidence without requiring the role to run again.
        STATE.write_atomic(STATE.repair_path(self.args), record)
        terminal_file_digest = hashlib.sha256(
            STATE.canonical(passport)
        ).hexdigest()
        ticket.write_text(
            ticket.read_text(encoding="utf-8")
            + "\nMigration marker: successor Factory.\n",
            encoding="utf-8",
        )
        run("git", "add", str(ticket), cwd=self.product)
        run("git", "commit", "-qm", "migrate completed repair", cwd=self.product)
        migrated_head = run("git", "rev-parse", "HEAD", cwd=self.product)
        successor_factory = "9" * 40
        migration = {
            "from_factory_sha": self.args.factory_sha,
            "from_head_sha": output_head,
            "from_passport_file_sha256": terminal_file_digest,
            "from_passport_sha256": passport["passport_sha256"],
            "from_protected_base_sha": body["protected_base_sha"],
            "from_route_plan_sha256": body["route_plan_sha256"],
            "schema": STATE.PASSPORT_MIGRATION_SCHEMA,
            "to_factory_sha": successor_factory,
            "to_head_sha": migrated_head,
            "to_protected_base_sha": "6" * 40,
            "to_route_plan_sha256": "7" * 64,
        }
        migrated_body = {
            **body,
            "factory_release_history": [
                *body["factory_release_history"],
                {
                    "contract_version": self.args.contract_version,
                    "factory_sha": successor_factory,
                },
            ],
            "factory_sha": successor_factory,
            "head_sha": migrated_head,
            "migration_history": [
                *body["migration_history"],
                migration,
            ],
            "parent_digest": passport["passport_sha256"],
            "parent_file_sha256": terminal_file_digest,
            "protected_base_sha": migration["to_protected_base_sha"],
            "route_plan_sha256": migration["to_route_plan_sha256"],
        }
        self.args.factory_sha = successor_factory

        wrong_bridge_body = {
            **migrated_body,
            "migration_history": [
                *body["migration_history"],
                {
                    **migration,
                    "from_head_sha": current_head,
                },
            ],
        }
        wrong_bridge = dict(wrong_bridge_body)
        wrong_bridge["authentication_sha256"] = hmac.new(
            secret, STATE.canonical(wrong_bridge_body), hashlib.sha256
        ).hexdigest()
        wrong_bridge["passport_sha256"] = hashlib.sha256(
            STATE.canonical(wrong_bridge)
        ).hexdigest()
        STATE.write_atomic(passports / "T-110.json", wrong_bridge)
        with self.assertRaisesRegex(
            STATE.StateError, "contract repair record is invalid"
        ):
            STATE.contract_repair_stage(self.args)

        wrong_route_body = {
            **migrated_body,
            "migration_history": [
                *body["migration_history"],
                {
                    **migration,
                    "from_route_plan_sha256": "8" * 64,
                },
            ],
        }
        wrong_route = dict(wrong_route_body)
        wrong_route["authentication_sha256"] = hmac.new(
            secret, STATE.canonical(wrong_route_body), hashlib.sha256
        ).hexdigest()
        wrong_route["passport_sha256"] = hashlib.sha256(
            STATE.canonical(wrong_route)
        ).hexdigest()
        STATE.write_atomic(passports / "T-110.json", wrong_route)
        with self.assertRaisesRegex(
            STATE.StateError, "contract repair record is invalid"
        ):
            STATE.contract_repair_stage(self.args)

        migrated_passport = dict(migrated_body)
        migrated_passport["authentication_sha256"] = hmac.new(
            secret, STATE.canonical(migrated_body), hashlib.sha256
        ).hexdigest()
        migrated_passport["passport_sha256"] = hashlib.sha256(
            STATE.canonical(migrated_passport)
        ).hexdigest()
        STATE.write_atomic(passports / "T-110.json", migrated_passport)
        successes = STATE.contract_repair_successes(
            self.args, "test-author", old_head,
        )
        self.assertEqual(len(successes), 1)
        migrated_loaded, _ = STATE.authenticated_passport(self.args)
        transition = STATE.safe_receipt(self.state_dir / "T-110.json")
        self.assertIsNotNone(STATE.completed_repair_migration_split(
            self.args, migrated_loaded, successes[0], transition,
        ))
        self.assertTrue(STATE.completed_migrated_contract_repair(
            self.args, migrated_loaded, record, successes[0],
        ))
        self.assertTrue(STATE.migrated_contract_repair(
            self.args, migrated_loaded, record, successes[0],
        ))
        with mock.patch.object(STATE, "resolve", return_value="RUN builder"):
            self.assertEqual(
                STATE.contract_repair_stage(self.args),
                ("RUN builder", False),
            )
        self.assertFalse(STATE.repair_path(self.args).exists())

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
