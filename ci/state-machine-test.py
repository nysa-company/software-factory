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
            contract_version="2.0.0",
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

    def test_expected_head_refuses_state_machine_snapshot_drift(self) -> None:
        self.args.expected_head = "a" * 40
        with (
            mock.patch.object(STATE, "git", return_value="b" * 40),
            self.assertRaisesRegex(STATE.StateError, "expected head"),
        ):
            STATE.next_transition(self.args)

    def test_trusted_state_commit_advances_expected_head_cas(self) -> None:
        self.args.expected_head = "a" * 40
        with (
            mock.patch.object(STATE, "run_helper") as helper,
            mock.patch.object(STATE, "git", return_value="b" * 40),
        ):
            STATE.transition(self.args, "Building")
        helper.assert_called_once()
        self.assertEqual(self.args.expected_head, "b" * 40)

    def test_materialization_advances_expected_head_before_receipt(self) -> None:
        self.args.expected_head = "a" * 40
        with (
            mock.patch.object(
                STATE, "git", side_effect=["a" * 40, "b" * 40],
            ),
            mock.patch.object(STATE, "current_state", return_value="Backlog"),
            mock.patch.object(STATE, "run_helper") as helper,
            mock.patch.object(STATE, "declared_dependencies", return_value=[]),
            mock.patch.object(
                STATE, "contract_repair_stage", return_value=(None, False),
            ),
            mock.patch.object(
                STATE, "resolve", return_value="AWAIT_BUDGET daily cap",
            ),
            mock.patch.object(STATE, "migrate_passport"),
            mock.patch.object(
                STATE, "issue",
                side_effect=lambda args, _stage, _loop=None: {
                    "receipt_sha256": args.expected_head,
                },
            ),
        ):
            result = STATE.next_transition(self.args)
        helper.assert_called_once()
        self.assertEqual(self.args.expected_head, "b" * 40)
        self.assertEqual(result["receipt"], "b" * 40)

    def test_role_prompts_reject_unproducible_generated_values(self) -> None:
        prompts = {
            "planner": "evaluate its first generated value",
            "spec-linter": "a repair scope that excludes its required setup correction",
            "test-author": "the repair scope forbids the setup correction",
        }
        for role, rule in prompts.items():
            self.assertIn(rule, (ROOT / "roles" / f"{role}.md").read_text())

    def test_role_prompts_close_serialized_fixture_dependencies(self) -> None:
        prompts = {
            "planner": (
                "enumerate every sibling dependent table",
                "authorize child-first cleanup",
                "an exact `ON DELETE CASCADE` is sufficient",
                "only the minimal protected-test setup edits",
            ),
            "spec-linter": (
                "enumerate every sibling dependent table",
                "each non-cascading foreign key has child-first cleanup",
                "an exact `ON DELETE CASCADE` needs no redundant cleanup edit",
                "unrelated helpers or tests remain outside Test-author ownership",
            ),
            "test-author": (
                "delete non-cascading dependent rows before their parent",
                "including every sibling dependent table named by the contract",
                "Do not add redundant cleanup for an exact `ON DELETE CASCADE`",
                "preserve every already committed valid test",
                "never broaden ownership yourself",
                "ROLE-ESCALATE: CONTRACT-BLOCKED",
            ),
        }
        for role, rules in prompts.items():
            prompt = (ROOT / "roles" / f"{role}.md").read_text()
            for rule in rules:
                self.assertIn(rule, prompt)

    def test_role_prompts_fail_closed_on_protected_source_boundaries(self) -> None:
        prompts = {
            "planner": (
                "import/export allowlists",
                "exact protected test plus a concise identifier",
                "An unknown or unparsable static source-boundary check is a block",
                "--conflict-entry",
                "CONFLICT DECLARATION PASS",
                "--global-literal",
                "GLOBAL TEXT PASS",
            ),
            "spec-linter": (
                "FAIL before Builder when a planned module literal is rejected",
                "naming the exact test and assertion",
                "unknown or unparsable static checks also FAIL closed",
                "page-wide text assertions",
                "GLOBAL TEXT PASS",
            ),
            "test-author": (
                "Protected-Test-Conflicts: <test path> => <literal>",
                "change only that exact protected test inside `Fixture-Seams`",
                "never broaden the allowlist or ownership",
            ),
        }
        for role, rules in prompts.items():
            prompt = (ROOT / "roles" / f"{role}.md").read_text()
            for rule in rules:
                self.assertIn(rule, prompt)

    def test_planner_emits_the_epoch_gate_marker_append_only(self) -> None:
        prompt = (ROOT / "roles/planner.md").read_text()
        self.assertIn("- **Freeze result:** PASS. Contract version N is frozen.", prompt)
        self.assertIn("without editing or removing prior frozen versions", prompt)
        self.assertIn("append one higher numbered frozen-contract epoch", prompt)
        self.assertIn("do not invoke npm, pnpm, yarn, npx, corepack", prompt)

    def test_planner_package_manager_guard_refuses_product_suites(self) -> None:
        guard = ROOT / "scripts/lib/role-command-guard.sh"
        with tempfile.TemporaryDirectory() as raw:
            npm = Path(raw) / "npm"
            npm.symlink_to(guard)
            result = subprocess.run(
                [str(npm), "test"], text=True, capture_output=True
            )
        self.assertEqual(result.returncode, 126)
        self.assertIn("role_policy_violation", result.stderr)

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

    def test_spec_lint_waits_for_each_round_after_three(self) -> None:
        ticket = self.product / "factory/tickets/T-110.md"
        cap = "ESCALATE planner-spec-linter loop cap reached; attempts=3; limit=3"
        ticket.write_text(
            "# T-110\n\nState: Planning\n"
            "SPEC-LINT: FAIL — one\n"
            "SPEC-LINT: FAIL — two\n"
            "OPERATOR AUTHORIZATION: spec-linter round 3\n"
            "SPEC-LINT: FAIL — three\n",
            encoding="utf-8",
        )
        stage, loop = STATE.govern_loop(self.args, "RUN planner", False)
        self.assertEqual(
            stage,
            "AWAIT-OPERATOR semantic-round authorization required; add exact "
            "line: OPERATOR AUTHORIZATION: spec-linter round 4",
        )
        self.assertEqual(loop, {
            "attempt": 3, "capped": True,
            "kind": "planner-spec-linter", "limit": 3,
        })
        stage, loop = STATE.govern_loop(self.args, cap, False)
        self.assertEqual(
            stage,
            "AWAIT-OPERATOR semantic-round authorization required; add exact "
            "line: OPERATOR AUTHORIZATION: spec-linter round 4",
        )
        self.assertTrue(loop["capped"])
        ticket.write_text(
            ticket.read_text(encoding="utf-8")
            + "OPERATOR AUTHORIZATION: spec-linter round 4\n",
            encoding="utf-8",
        )
        stage, loop = STATE.govern_loop(self.args, cap, False)
        self.assertEqual(stage, "RUN planner")
        self.assertFalse(loop["capped"])
        for malformed in (
            "ESCALATE planner-spec-linter loop cap reached; attempts=4; limit=3",
            "ESCALATE planner-spec-linter loop cap reached; attempts=3; limit=4",
            cap + "; unexpected=true",
        ):
            with self.subTest(malformed=malformed):
                stage, _loop = STATE.govern_loop(self.args, malformed, False)
                self.assertEqual(stage, malformed)

        for authorization, expected_stage in (
            ("", "AWAIT-OPERATOR semantic-round authorization required; add "
                 "exact line: OPERATOR AUTHORIZATION: spec-linter round 4"),
            ("OPERATOR AUTHORIZATION: spec-linter round 4\n", "RUN planner"),
        ):
            with self.subTest(next_transition=expected_stage):
                ticket.write_text(
                    "# T-110\n\nState: Planning\n"
                    "SPEC-LINT: FAIL — one\n"
                    "SPEC-LINT: FAIL — two\n"
                    "OPERATOR AUTHORIZATION: spec-linter round 3\n"
                    "SPEC-LINT: FAIL — three\n"
                    + authorization,
                    encoding="utf-8",
                )
                with (
                    mock.patch.object(
                        STATE, "current_state", return_value="Planning",
                    ),
                    mock.patch.object(
                        STATE, "declared_dependencies", return_value=[],
                    ),
                    mock.patch.object(
                        STATE, "contract_repair_stage",
                        return_value=(None, False),
                    ),
                    mock.patch.object(STATE, "resolve", return_value=cap),
                    mock.patch.object(STATE, "migrate_passport"),
                    mock.patch.object(
                        STATE, "issue",
                        side_effect=lambda _args, _stage, loop=None: {
                            "loop": loop, "receipt_sha256": "d" * 64,
                        },
                    ),
                ):
                    transition = STATE.next_transition(self.args)
                self.assertEqual(transition["stage"], expected_stage)

        ticket.write_text(
            "# T-110\n\nState: Building\n"
            "reviewer round 1: REQUEST CHANGES\n"
            "reviewer round 2: REQUEST CHANGES\n"
            "reviewer round 3: REQUEST CHANGES\n",
            encoding="utf-8",
        )
        stage, loop = STATE.govern_loop(self.args, "FIX builder", True)
        self.assertEqual(stage, "FIX builder")
        self.assertIsNone(loop)
        stage, loop = STATE.govern_loop(self.args, "RUN reviewer", False)
        self.assertEqual(stage, "RUN reviewer")
        self.assertIsNone(loop)
        self.assertEqual(
            STATE.verified_preflight_stage(self.args, {
                "stage": "RUN reviewer",
                "loop": {
                    "attempt": 3, "capped": True,
                    "kind": "builder-reviewer", "limit": 3,
                },
            }),
            "RUN reviewer",
        )

        ticket.write_text("# T-110\n\nState: Building\n", encoding="utf-8")
        with mock.patch.object(STATE, "contract_repair_attempt", return_value=3):
            stage, loop = STATE.govern_loop(
                self.args, "RUN spec-linter", True
            )
        self.assertEqual(stage, "RUN spec-linter")
        self.assertEqual(loop, {
            "attempt": 3, "capped": False,
            "kind": "contract-repair", "limit": 3,
        })

        ticket.write_text("# T-110\n\nState: Building\n", encoding="utf-8")
        with mock.patch.object(STATE, "contract_repair_attempt", return_value=3):
            stage, loop = STATE.govern_loop(self.args, "FIX builder", True)
        self.assertEqual(
            stage,
            "AWAIT-OPERATOR semantic-round authorization required; add exact "
            "line: OPERATOR AUTHORIZATION: builder round 4",
        )
        self.assertTrue(loop["capped"])
        ticket.write_text(
            "# T-110\n\nState: Building\n"
            "OPERATOR AUTHORIZATION: builder round 4\n",
            encoding="utf-8",
        )
        with mock.patch.object(STATE, "contract_repair_attempt", return_value=3):
            stage, loop = STATE.govern_loop(self.args, "FIX builder", True)
        self.assertEqual(stage, "FIX builder")
        self.assertFalse(loop["capped"])

    def test_third_spec_lint_round_waits_for_one_exact_authorization(self) -> None:
        ticket = self.product / "factory/tickets/T-110.md"
        prefix = (
            "# T-110\n\nState: Planning\n"
            "SPEC-LINT: FAIL — one\n"
            "SPEC-LINT: FAIL — two\n"
        )
        expected = (
            "AWAIT-OPERATOR semantic-round authorization required; "
            "add exact line: OPERATOR AUTHORIZATION: spec-linter round 3"
        )
        for addition in (
            "",
            "OPERATOR AUTHORIZATION: spec-linter round 2\n",
            "OPERATOR AUTHORIZATION: spec-linter round 4\n",
            "OPERATOR AUTHORIZATION: reviewer round 3\n",
            "operator authorization: spec-linter round 3\n",
        ):
            with self.subTest(addition=addition):
                ticket.write_text(prefix + addition, encoding="utf-8")
                for stage in ("RUN planner", "RUN spec-linter"):
                    governed, loop = STATE.govern_loop(self.args, stage, False)
                    self.assertEqual(governed, expected)
                    self.assertEqual(loop, {
                        "attempt": 2, "capped": False,
                        "kind": "planner-spec-linter", "limit": 3,
                    })

        ticket.write_text(prefix, encoding="utf-8")
        with (
            mock.patch.object(STATE, "current_state", return_value="Planning"),
            mock.patch.object(STATE, "declared_dependencies", return_value=[]),
            mock.patch.object(
                STATE, "contract_repair_stage", return_value=(None, False),
            ),
            mock.patch.object(STATE, "resolve", return_value=expected),
            mock.patch.object(STATE, "migrate_passport"),
            mock.patch.object(
                STATE, "issue",
                side_effect=lambda _args, _stage, loop=None: {
                    "loop": loop, "receipt_sha256": "d" * 64,
                },
            ),
        ):
            transition = STATE.next_transition(self.args)
        self.assertEqual(transition["loop"], {
            "attempt": 2, "capped": False,
            "kind": "planner-spec-linter", "limit": 3,
        })

        authorization = (
            "OPERATOR AUTHORIZATION: spec-linter round 3\n"
        )
        ticket.write_text(prefix + authorization, encoding="utf-8")
        for stage in ("RUN planner", "RUN spec-linter"):
            governed, loop = STATE.govern_loop(self.args, stage, False)
            self.assertEqual(governed, stage)
            self.assertEqual(loop["attempt"], 2)

        ticket.write_text(prefix + authorization * 2, encoding="utf-8")
        governed, _loop = STATE.govern_loop(self.args, "RUN planner", False)
        self.assertEqual(
            governed,
            "REFUSE semantic-round authorization is ambiguous; "
            "spec-linter grants are not ordered one-use controls",
        )

        ticket.write_text(
            prefix
            + authorization
            + "SPEC-LINT: FAIL — three\n"
            + "OPERATOR AUTHORIZATION: spec-linter round 4\n",
            encoding="utf-8",
        )
        governed, loop = STATE.govern_loop(self.args, "RUN planner", False)
        self.assertEqual(governed, "RUN planner")
        self.assertFalse(loop["capped"])

        ticket.write_text(
            prefix
            + authorization
            + "SPEC-LINT: FAIL — three\n"
            + authorization,
            encoding="utf-8",
        )
        governed, loop = STATE.govern_loop(self.args, "RUN planner", False)
        self.assertIn("OPERATOR AUTHORIZATION: spec-linter round 4", governed)
        self.assertTrue(loop["capped"])

    def test_consumed_spec_lint_grant_requires_the_next_round(self) -> None:
        ticket = self.product / "factory/tickets/T-110.md"
        history = (
            "# T-110\n\nState: Building\n"
            "SPEC-LINT: FAIL — one\n"
            "SPEC-LINT: PASS\n"
            "SPEC-LINT: FAIL — two\n"
            "OPERATOR AUTHORIZATION: spec-linter round 3\n"
            "SPEC-LINT: FAIL — three\n"
            "OPERATOR AUTHORIZATION: spec-linter round 4\n"
            "SPEC-LINT: PASS\n"
        )
        expected = (
            "AWAIT-OPERATOR semantic-round authorization required; add exact "
            "line: OPERATOR AUTHORIZATION: spec-linter round 5"
        )
        ticket.write_text(history, encoding="utf-8")
        for raw in ("RUN planner", "RUN spec-linter"):
            with self.subTest(raw=raw):
                stage, loop = STATE.govern_loop(self.args, raw, False)
                self.assertEqual(stage, expected)
                self.assertEqual(loop, {
                    "attempt": 4, "capped": True,
                    "kind": "planner-spec-linter", "limit": 3,
                })
        stage, loop = STATE.govern_loop(self.args, expected, False)
        self.assertEqual(stage, expected)
        self.assertEqual(loop, {
            "attempt": 4, "capped": True,
            "kind": "planner-spec-linter", "limit": 3,
        })
        narrator_wait = (
            "AWAIT-OPERATOR semantic-round authorization required; add exact "
            "line: OPERATOR AUTHORIZATION: narrator round 3"
        )
        stage, loop = STATE.govern_loop(self.args, narrator_wait, False)
        self.assertEqual(stage, narrator_wait)
        self.assertEqual(loop, {
            "attempt": 2, "capped": True,
            "kind": "narrator-bundle", "limit": 2,
        })
        grant = "OPERATOR AUTHORIZATION: spec-linter round 5\n"
        ticket.write_text(history + grant, encoding="utf-8")
        stage, loop = STATE.govern_loop(self.args, "RUN spec-linter", False)
        self.assertEqual(stage, "RUN spec-linter")
        self.assertFalse(loop["capped"])
        ticket.write_text(history + grant * 2, encoding="utf-8")
        stage, _loop = STATE.govern_loop(self.args, "RUN spec-linter", False)
        self.assertIn("authorization is ambiguous", stage)
        ticket.write_text(
            history.replace(
                "OPERATOR AUTHORIZATION: spec-linter round 4\n", "",
            ),
            encoding="utf-8",
        )
        stage, loop = STATE.govern_loop(self.args, "RUN test-author", False)
        self.assertEqual(
            stage,
            "REFUSE semantic-round authorization is ambiguous; "
            "spec-linter grants are not ordered one-use controls",
        )
        self.assertEqual(loop["attempt"], 3)

    def test_qualification_semantic_round_uses_only_the_sealed_epoch(self) -> None:
        ticket = self.product / "factory/tickets/T-110.md"
        historical = (
            "# T-110\n\nState: Planning\n"
            "SPEC-LINT: FAIL — historical one\n"
            "SPEC-LINT: FAIL — historical two\n"
            "SPEC-LINT: FAIL — historical three\n"
            "OPERATOR AUTHORIZATION: spec-linter round 3\n"
        )
        ticket.write_text(historical, encoding="utf-8")
        run("git", "add", str(ticket), cwd=self.product)
        run("git", "commit", "-qm", "protected baseline", cwd=self.product)
        baseline = run("git", "rev-parse", "HEAD", cwd=self.product)
        fresh = (
            "SPEC-LINT: FAIL — current one\n"
            "SPEC-LINT: FAIL — current two\n"
        )
        expected = (
            "AWAIT-OPERATOR semantic-round authorization required; add exact "
            "line: OPERATOR AUTHORIZATION: spec-linter round 3"
        )
        with mock.patch.dict(os.environ, {
            "FACTORY_KIT_TRUST_SCOPE": "qualification-candidate",
            "FACTORY_QUALIFICATION_PRODUCT_SHA": baseline,
        }):
            ticket.write_text(historical + fresh, encoding="utf-8")
            stage, loop = STATE.govern_loop(self.args, "RUN planner", False)
            self.assertEqual(stage, expected)
            self.assertEqual(loop["attempt"], 2)

            grant = "OPERATOR AUTHORIZATION: spec-linter round 3\n"
            ticket.write_text(historical + fresh + grant, encoding="utf-8")
            stage, loop = STATE.govern_loop(self.args, "RUN planner", False)
            self.assertEqual(stage, "RUN planner")
            self.assertFalse(loop["capped"])

            ticket.write_text(historical + fresh + grant * 2, encoding="utf-8")
            stage, _loop = STATE.govern_loop(self.args, "RUN planner", False)
            self.assertIn("authorization is ambiguous", stage)

    def test_later_narrator_correction_wait_binds_exact_round(self) -> None:
        ticket = self.product / "factory/tickets/T-110.md"
        required = (
            "AWAIT-OPERATOR semantic-round authorization required; add exact "
            "line: OPERATOR AUTHORIZATION: narrator round 3"
        )
        ticket.write_text("# T-110\n\nState: Review\n", encoding="utf-8")
        stage, loop = STATE.govern_loop(self.args, required, False)
        self.assertEqual(stage, required)
        self.assertEqual(loop, {
            "attempt": 2, "capped": True,
            "kind": "narrator-bundle", "limit": 2,
        })

        invalid = required.replace(
            "required; add exact line", "invalid; keep exactly one line",
        )
        ticket.write_text(
            "# T-110\n\nState: Review\n"
            "OPERATOR AUTHORIZATION: narrator round 3\n"
            "OPERATOR AUTHORIZATION: narrator round 3\n",
            encoding="utf-8",
        )
        stage, loop = STATE.govern_loop(self.args, invalid, False)
        self.assertEqual(stage, invalid)
        self.assertTrue(loop["capped"])

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
        protected = run("git", "rev-parse", "HEAD", cwd=self.product)
        run(
            "git", "update-ref", "refs/remotes/origin/main", protected,
            cwd=self.product,
        )
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
        receipt = STATE.safe_receipt(self.state_dir / "T-110.json")
        self.assertEqual(receipt["protected_base_sha"], protected)
        self.assertEqual(receipt["receipt_sha256"], result["receipt"])

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
        shutil.copy2(
            ROOT / "factory-contract.json",
            release / "factory-contract.json",
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
                "contract_version": "2.0.0",
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
                "contract_version": "2.0.0",
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
            "FACTORY_RELEASE_CONTRACT_VERSION": "2.0.0",
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
        shutil.copy2(
            ROOT / "factory-contract.json",
            release / "factory-contract.json",
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
                "AWAIT-OPERATOR semantic-round authorization required; add exact "
                "line: OPERATOR AUTHORIZATION: narrator round 3",
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
                        "contract_version": "2.0.0",
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
                    "contract_version": "2.0.0",
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
                    "FACTORY_RELEASE_CONTRACT_VERSION": "2.0.0",
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

    def test_reviewer_planner_repair_catches_up_without_rewinding_state(
        self,
    ) -> None:
        ticket = self.product / "factory/tickets/T-110.md"
        ticket.write_text(
            "# T-110\n\nState: Building\n"
            "reviewer round 1: REQUEST CHANGES\n"
            "reviewer round 1 FIX-OWNER: test-author\n"
            "SPEC-LINT: FAIL — frozen path mismatch\n",
            encoding="utf-8",
        )
        completed = [
            {"role": role} for role in (
                "planner", "spec-linter", "test-author", "builder",
                "reviewer", "planner",
            )
        ]
        receipt = "b" * 64
        with (
            mock.patch.object(STATE, "current_state", return_value="Building"),
            mock.patch.object(
                STATE, "contract_repair_stage", return_value=(None, False)
            ),
            mock.patch.object(
                STATE, "authenticated_role_evidence",
                return_value=({}, completed),
            ),
            mock.patch.object(
                STATE, "resolve", return_value="RUN spec-linter"
            ),
            mock.patch.object(STATE, "transition") as transition,
            mock.patch.object(STATE, "migrate_passport"),
            mock.patch.object(
                STATE, "issue", return_value={"receipt_sha256": receipt}
            ),
        ):
            result = STATE.next_transition(self.args)

        transition.assert_not_called()
        self.assertEqual(result["stage"], "RUN spec-linter")

        with mock.patch.object(
            STATE, "authenticated_role_evidence",
            return_value=({}, completed + [{"role": "builder"}]),
        ):
            self.assertFalse(
                STATE.reviewer_repair_catchup(self.args, "RUN spec-linter")
            )

    def test_reviewer_repair_catchup_sequence_and_receipt_loop_fail_closed(
        self,
    ) -> None:
        ticket = self.product / "factory/tickets/T-110.md"
        prefix = [
            {"role": role} for role in (
                "planner", "spec-linter", "test-author", "builder", "reviewer",
            )
        ]

        def write(verdict="REQUEST CHANGES", spec="FAIL") -> None:
            ticket.write_text(
                "# T-110\n\nState: Building\n"
                f"reviewer round 1: {verdict}\n"
                f"SPEC-LINT: {spec}\n",
                encoding="utf-8",
            )

        valid = (
            (("planner",), "RUN spec-linter", "FAIL"),
            (("planner", "spec-linter"), "RUN planner", "FAIL"),
            (("planner", "spec-linter"), "RUN test-author", "PASS"),
            (("planner", "spec-linter", "planner"), "RUN spec-linter", "FAIL"),
            (("planner", "spec-linter") * 3, "RUN planner", "FAIL"),
            (("planner", "spec-linter") * 3 + ("planner",),
             "RUN spec-linter", "FAIL"),
            (("planner", "spec-linter") * 4, "RUN planner", "FAIL"),
        )
        for after, stage, spec in valid:
            with self.subTest(after=after, stage=stage, spec=spec):
                write(spec=spec)
                completed = prefix + [{"role": role} for role in after]
                with mock.patch.object(
                    STATE, "authenticated_role_evidence",
                    return_value=({}, completed),
                ):
                    self.assertTrue(
                        STATE.reviewer_repair_catchup(self.args, stage)
                    )

        invalid = (
            ((), "RUN planner"),
            (("spec-linter",), "RUN planner"),
            (("planner", "planner"), "RUN planner"),
            (("planner", "spec-linter", "narrator"), "RUN spec-linter"),
            (("planner", "spec-linter", "builder"), "RUN planner"),
            (("planner",), "RUN planner"),
            (("planner", "spec-linter"), "RUN spec-linter"),
        )
        write()
        for after, stage in invalid:
            with self.subTest(after=after, stage=stage):
                completed = prefix + [{"role": role} for role in after]
                with mock.patch.object(
                    STATE, "authenticated_role_evidence",
                    return_value=({}, completed),
                ):
                    self.assertFalse(
                        STATE.reviewer_repair_catchup(self.args, stage)
                    )

        write(verdict="APPROVE")
        completed = prefix + [{"role": role} for role in ("planner", "spec-linter")]
        with mock.patch.object(
            STATE, "authenticated_role_evidence", return_value=({}, completed)
        ):
            self.assertFalse(
                STATE.reviewer_repair_catchup(self.args, "RUN planner")
            )

        write()
        for prior in (
            [{"role": "planner"}, {"role": "reviewer"}],
            [{"role": "planner"}, {"role": "spec-linter"}, {"role": "test-author"}],
        ):
            with self.subTest(prior=prior):
                with mock.patch.object(
                    STATE,
                    "authenticated_role_evidence",
                    return_value=(
                        {}, prior + [{"role": "planner"}, {"role": "spec-linter"}]
                    ),
                ):
                    self.assertFalse(
                        STATE.reviewer_repair_catchup(self.args, "RUN planner")
                    )

        valid_receipt = {
            "stage": "RUN planner",
            "loop": {
                "attempt": 2,
                "capped": False,
                "kind": "planner-spec-linter",
                "limit": 3,
            },
        }
        with mock.patch.object(
            STATE, "authenticated_role_evidence", return_value=({}, completed)
        ):
            for attempt in (1, 2):
                with self.subTest(attempt=attempt):
                    self.assertEqual(
                        STATE.verified_preflight_stage(
                            self.args,
                            {
                                **valid_receipt,
                                "loop": {
                                    **valid_receipt["loop"],
                                    "attempt": attempt,
                                },
                            },
                        ),
                        "CATCHUP planner",
                    )
            for change in (
                {"attempt": 0},
                {"attempt": True},
                {"capped": True},
                {"kind": "builder-reviewer"},
                {"limit": 4},
            ):
                loop = {**valid_receipt["loop"], **change}
                with self.subTest(loop=loop):
                    self.assertEqual(
                        STATE.verified_preflight_stage(
                            self.args, {**valid_receipt, "loop": loop}
                        ),
                        "RUN planner",
                    )
            self.assertEqual(
                STATE.verified_preflight_stage(
                    self.args, {"stage": "RUN planner", "loop": None}
                ),
                "RUN planner",
            )
            for malformed in (
                {key: value for key, value in valid_receipt["loop"].items()
                 if key != "attempt"},
                {**valid_receipt["loop"], "extra": 1},
            ):
                with self.subTest(malformed=malformed):
                    self.assertEqual(
                        STATE.verified_preflight_stage(
                            self.args, {**valid_receipt, "loop": malformed}
                        ),
                        "RUN planner",
                    )
        with mock.patch.object(
            STATE, "reviewer_repair_catchup", return_value=True,
        ):
            for attempt in (3, 4):
                controls = (
                    "SPEC-LINT: FAIL — failure 1\n"
                    "SPEC-LINT: FAIL — failure 2\n"
                    + "".join(
                        "OPERATOR AUTHORIZATION: spec-linter "
                        f"round {semantic_round}\n"
                        f"SPEC-LINT: FAIL — failure {semantic_round}\n"
                        for semantic_round in range(3, attempt + 1)
                    )
                    + "OPERATOR AUTHORIZATION: spec-linter "
                    f"round {attempt + 1}\n"
                )
                ticket.write_text(
                    "# T-110\n\nState: Building\n" + controls,
                    encoding="utf-8",
                )
                receipt = {
                    **valid_receipt,
                    "loop": {**valid_receipt["loop"], "attempt": attempt},
                }
                with self.subTest(authorized_cap_attempt=attempt):
                    self.assertEqual(
                        STATE.verified_preflight_stage(self.args, receipt),
                        "CATCHUP planner",
                    )
                    ticket.write_text(
                        ticket.read_text(encoding="utf-8").replace(
                            "OPERATOR AUTHORIZATION:", "OPERATOR AUTHORITY:"
                        ),
                        encoding="utf-8",
                    )
                    self.assertEqual(
                        STATE.verified_preflight_stage(self.args, receipt),
                        "RUN planner",
                    )
                    ticket.write_text(
                        ticket.read_text(encoding="utf-8").replace(
                            "OPERATOR AUTHORITY:", "OPERATOR AUTHORIZATION:"
                        ),
                        encoding="utf-8",
                    )
        with mock.patch.object(
            STATE, "reviewer_repair_catchup", return_value=False,
        ):
            self.assertEqual(
                STATE.verified_preflight_stage(self.args, receipt),
                "RUN planner",
            )

    def test_qualification_reviewer_catchup_requires_a_current_verdict(self) -> None:
        ticket = self.product / "factory/tickets/T-110.md"
        historical = (
            "# T-110\n\nState: Building\n"
            "reviewer round 1: REQUEST CHANGES\n"
            "SPEC-LINT: FAIL — historical\n"
        )
        ticket.write_text(historical, encoding="utf-8")
        run("git", "add", str(ticket), cwd=self.product)
        run("git", "commit", "-qm", "protected review baseline", cwd=self.product)
        baseline = run("git", "rev-parse", "HEAD", cwd=self.product)
        completed = [
            {"role": role} for role in (
                "planner", "spec-linter", "test-author", "builder", "reviewer",
                "planner", "spec-linter",
            )
        ]
        with mock.patch.dict(os.environ, {
            "FACTORY_KIT_TRUST_SCOPE": "qualification-candidate",
            "FACTORY_QUALIFICATION_PRODUCT_SHA": baseline,
        }), mock.patch.object(
            STATE, "authenticated_role_evidence", return_value=({}, completed),
        ):
            self.assertFalse(
                STATE.reviewer_repair_catchup(self.args, "RUN planner")
            )
            ticket.write_text(
                historical
                + "reviewer round 2: REQUEST CHANGES\n"
                + "SPEC-LINT: FAIL — current\n",
                encoding="utf-8",
            )
            self.assertTrue(
                STATE.reviewer_repair_catchup(self.args, "RUN planner")
            )

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
                    if (
                        action == "FIX"
                        and role == "planner"
                        and current in {"Building", "Review"}
                    ):
                        expected = []
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

    def test_fix_contract_block_survives_lease_rotation_and_resume(self) -> None:
        original_lease = "d" * 64
        self.args.lease = original_lease
        self.args.role = "builder"
        issued = STATE.issue(self.args, "FIX builder")
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
            "role=builder\n"
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
            "Resume-State: Review\n",
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
        def write_passport(value: dict) -> dict:
            unsigned = {
                key: item for key, item in value.items()
                if key not in {"authentication_sha256", "passport_sha256"}
            }
            signed = dict(unsigned)
            signed["authentication_sha256"] = hmac.new(
                secret, STATE.canonical(unsigned), hashlib.sha256
            ).hexdigest()
            signed["passport_sha256"] = hashlib.sha256(
                STATE.canonical(signed)
            ).hexdigest()
            STATE.write_atomic(passports / "T-110.json", signed)
            return signed

        body = {
            "branch": "ticket/T-110",
            "charge_records": [{
                "contract_version": self.args.contract_version,
                "factory_sha": self.args.factory_sha,
                "head_before": issued["head_sha"],
                "role": "builder",
                "transition_receipt_sha256": self.args.receipt,
            }],
            "completed_role_evidence": [],
            "contract_version": self.args.contract_version,
            "current_stage": "FIX builder",
            "current_state": "Blocked-Escalated",
            "factory_sha": self.args.factory_sha,
            "head_sha": blocked_head,
            "project": self.args.project,
            "schema": STATE.PASSPORT_SCHEMA,
            "ticket": "T-110",
            "transition_receipt_sha256": self.args.receipt,
        }
        passport = write_passport(body)

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
        self.assertEqual(STATE.contract_blocked_receipt(self.args), "builder")

        ticket.write_text(
            ticket.read_text(encoding="utf-8").rstrip()
            + "\n\nOPERATOR RESUME: builder\n"
            + f"OPERATOR RESUME RECEIPT: {self.args.receipt}\n",
            encoding="utf-8",
        )
        run("git", "add", str(ticket), cwd=self.product)
        run("git", "commit", "-qm", "authorize exact builder resume", cwd=self.product)
        ticket.write_text(
            ticket.read_text(encoding="utf-8").replace(
                "State: Blocked-Escalated", "State: Review"
            ),
            encoding="utf-8",
        )
        run("git", "add", str(ticket), cwd=self.product)
        run("git", "commit", "-qm", "materialize operator resume", cwd=self.product)
        self.assertEqual(STATE.contract_blocked_receipt(self.args), "builder")

        passport["current_stage"] = "RUN builder"
        passport = write_passport(passport)
        with self.assertRaisesRegex(
            STATE.StateError, "contract blocker receipt is invalid"
        ):
            STATE.contract_blocked_receipt(self.args)

        passport["current_stage"] = "FIX builder"
        passport["current_state"] = "Review"
        write_passport(passport)
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
        def write_passport(value: dict) -> None:
            passport = dict(value)
            passport["authentication_sha256"] = hmac.new(
                secret, STATE.canonical(value), hashlib.sha256
            ).hexdigest()
            passport["passport_sha256"] = hashlib.sha256(
                STATE.canonical(passport)
            ).hexdigest()
            STATE.write_atomic(passports / "T-110.json", passport)

        write_passport(body)
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

        ticket = self.product / "factory/tickets/T-110.md"
        ticket.write_text(
            ticket.read_text(encoding="utf-8") + "normalized history\n",
            encoding="utf-8",
        )
        run("git", "add", str(ticket), cwd=self.product)
        run("git", "commit", "-qm", "old history tip", cwd=self.product)
        old_tip = run("git", "rev-parse", "HEAD", cwd=self.product)
        old_tree = run("git", "rev-parse", "HEAD^{tree}", cwd=self.product)
        run("git", "commit", "--allow-empty", "-qm", "ordinary role commit",
            cwd=self.product)
        gap = run("git", "rev-parse", "HEAD", cwd=self.product)
        normalized = subprocess.run(
            ["git", "commit-tree", old_tree],
            cwd=self.product,
            input="protected normalization\n",
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        run("git", "reset", "--hard", normalized, cwd=self.product)
        route = hashlib.sha256(
            (self.product / "factory/route-plans/T-110.json").read_bytes()
        ).hexdigest()
        protected = issued["head_sha"]
        migration = {
            "from_factory_sha": old_factory,
            "from_head_sha": old_tip,
            "from_passport_file_sha256": "1" * 64,
            "from_passport_sha256": "2" * 64,
            "from_protected_base_sha": protected,
            "from_route_plan_sha256": route,
            "schema": STATE.PASSPORT_MIGRATION_SCHEMA,
            "to_factory_sha": current_factory,
            "to_head_sha": old_tip,
            "to_protected_base_sha": protected,
            "to_route_plan_sha256": route,
        }
        rewrite = {
            "from_factory_sha": current_factory,
            "from_head_sha": gap,
            "from_passport_file_sha256": "3" * 64,
            "from_passport_sha256": "4" * 64,
            "from_protected_base_sha": protected,
            "from_route_plan_sha256": route,
            "rewrite_authorization_sha256": "5" * 64,
            "schema": STATE.PASSPORT_MIGRATION_SCHEMA,
            "to_factory_sha": current_factory,
            "to_head_sha": normalized,
            "to_protected_base_sha": protected,
            "to_route_plan_sha256": route,
        }
        ticket.write_text(
            ticket.read_text(encoding="utf-8")
            + "\nOPERATOR RESUME: planner\n"
            + f"OPERATOR RESUME RECEIPT: {self.args.receipt}\n",
            encoding="utf-8",
        )
        run("git", "add", str(ticket), cwd=self.product)
        run("git", "commit", "-qm", "authorize exact planner resume", cwd=self.product)
        resumed = run("git", "rev-parse", "HEAD", cwd=self.product)
        resume_migration = {
            "from_factory_sha": current_factory,
            "from_head_sha": normalized,
            "from_passport_file_sha256": "6" * 64,
            "from_passport_sha256": "7" * 64,
            "from_protected_base_sha": protected,
            "from_route_plan_sha256": route,
            "schema": STATE.PASSPORT_MIGRATION_SCHEMA,
            "to_factory_sha": current_factory,
            "to_head_sha": resumed,
            "to_protected_base_sha": protected,
            "to_route_plan_sha256": route,
        }
        body.update({
            "head_sha": resumed,
            "migration_history": [migration, rewrite, resume_migration],
            "protected_base_sha": protected,
            "route_plan_sha256": route,
        })
        write_passport(body)
        self.assertEqual(STATE.contract_blocked_receipt(self.args), "planner")
        passport = json.loads((passports / "T-110.json").read_text())
        self.assertEqual(
            STATE.operator_resume_role(self.args, passport, "planner"), "planner"
        )

        rewrite.pop("rewrite_authorization_sha256")
        write_passport(body)
        with self.assertRaisesRegex(
            STATE.StateError, "contract blocker is outside receipt lineage"
        ):
            STATE.contract_blocked_receipt(self.args)

        rewrite["rewrite_authorization_sha256"] = "5" * 64
        ticket.write_text(
            ticket.read_text(encoding="utf-8") + "tree drift\n",
            encoding="utf-8",
        )
        run("git", "add", str(ticket), cwd=self.product)
        run("git", "commit", "-qm", "change normalized tree", cwd=self.product)
        changed = run("git", "rev-parse", "HEAD", cwd=self.product)
        rewrite["to_head_sha"] = changed
        body["head_sha"] = changed
        write_passport(body)
        with self.assertRaisesRegex(
            STATE.StateError, "contract blocker is outside receipt lineage"
        ):
            STATE.contract_blocked_receipt(self.args)

        body["migration_history"] = [migration, rewrite]
        write_passport(body)
        self.assertEqual(STATE.contract_blocked_receipt(self.args), "planner")
        (self.product / "app.js").write_text("semantic drift\n", encoding="utf-8")
        run("git", "add", "app.js", cwd=self.product)
        run("git", "commit", "-qm", "change normalized source", cwd=self.product)
        unsafe = run("git", "rev-parse", "HEAD", cwd=self.product)
        rewrite["to_head_sha"] = unsafe
        body["head_sha"] = unsafe
        write_passport(body)
        with self.assertRaisesRegex(
            STATE.StateError, "contract blocker is outside receipt lineage"
        ):
            STATE.contract_blocked_receipt(self.args)

        run("git", "reset", "--hard", resumed, cwd=self.product)
        rewrite["to_head_sha"] = normalized
        resume_migration["from_head_sha"] = old_tip
        body["head_sha"] = resumed
        body["migration_history"] = [migration, rewrite, resume_migration]
        write_passport(body)
        with self.assertRaisesRegex(
            STATE.StateError, "contract blocker is outside receipt lineage"
        ):
            STATE.contract_blocked_receipt(self.args)

        resume_migration["from_head_sha"] = normalized
        run("git", "reset", "--hard", normalized, cwd=self.product)
        rewrite["to_head_sha"] = normalized
        body["head_sha"] = normalized
        body["migration_history"] = [migration, rewrite, dict(rewrite)]
        write_passport(body)
        with self.assertRaisesRegex(
            STATE.StateError, "contract blocker is outside receipt lineage"
        ):
            STATE.contract_blocked_receipt(self.args)

    def test_migrated_fix_builder_resumes_to_receipt_bound_planner(self) -> None:
        old_factory = "b" * 40
        current_factory = self.args.factory_sha
        self.args.factory_sha = old_factory
        self.args.role = "builder"
        issued = STATE.issue(self.args, "FIX builder")
        self.args.receipt = issued["receipt_sha256"]
        STATE.verify(self.args, consume=True)
        manifest = self.product / "factory/runs/migrated-fix-builder.meta"
        manifest.write_text(
            "run_id=migrated-fix-builder\n"
            "phase=completed\n"
            "accounting_state=completed\n"
            "go_issued=1\n"
            "task_submitted=1\n"
            "ticket=T-110\n"
            "role=builder\n"
            f"contract_version={self.args.contract_version}\n"
            f"kit_sha={old_factory}\n"
            "exit_status=12\n"
            "role_exit=role_exit_contract_blocked\n"
            "role_branch_before=ticket/T-110\n"
            f"role_head_before={issued['head_sha']}\n"
            f"transition_receipt_sha256={self.args.receipt}\n",
            encoding="utf-8",
        )
        ticket = self.product / "factory/tickets/T-110.md"
        ticket.write_text(
            "# T-110\n\nState: Blocked-Escalated\nResume-State: Review\n",
            encoding="utf-8",
        )
        run("git", "add", str(ticket), cwd=self.product)
        run("git", "commit", "-qm", "materialize migrated blocker", cwd=self.product)
        blocked_head = run("git", "rev-parse", "HEAD", cwd=self.product)
        ticket.write_text(
            ticket.read_text(encoding="utf-8").rstrip()
            + "\n\nOPERATOR ANSWER: Preserve the authenticated planner decision.\n"
            + f"OPERATOR ANSWER RECEIPT: {self.args.receipt}\n",
            encoding="utf-8",
        )
        run("git", "add", str(ticket), cwd=self.product)
        run(
            "git", "commit", "-qm", "record receipt-bound planner answer",
            cwd=self.product,
        )
        answer_head = run("git", "rev-parse", "HEAD", cwd=self.product)
        ticket.write_text(
            ticket.read_text(encoding="utf-8").rstrip()
            + "\n\nOPERATOR RESUME: planner\n"
            + f"OPERATOR RESUME RECEIPT: {self.args.receipt}\n",
            encoding="utf-8",
        )
        run("git", "add", str(ticket), cwd=self.product)
        run("git", "commit", "-qm", "authorize exact planner resume", cwd=self.product)
        resume_head = run("git", "rev-parse", "HEAD", cwd=self.product)

        secret = b"k" * 32
        (self.state_dir / "passport.key").write_bytes(secret)
        os.chmod(self.state_dir / "passport.key", 0o600)
        passports = self.state_dir / "passports"
        passports.mkdir(mode=0o700)
        prior_completed = {
            "contract_version": self.args.contract_version,
            "factory_sha": old_factory,
            "head_before": issued["head_sha"],
            "manifest_sha256": "1" * 64,
            "output_sha256": "2" * 64,
            "role": "planner",
            "run_id": "completed-planner",
            "transition_receipt_sha256": "3" * 64,
        }
        body = {
            "branch": "ticket/T-110",
            "charge_records": [
                {
                    **prior_completed,
                    "accounting_state": "completed",
                    "charge_micro_usd": 1_000_000,
                },
                {
                    "accounting_state": "completed",
                    "contract_version": self.args.contract_version,
                    "factory_sha": old_factory,
                    "head_before": issued["head_sha"],
                    "manifest_sha256": hashlib.sha256(
                        manifest.read_bytes()
                    ).hexdigest(),
                    "role": "builder",
                    "run_id": "migrated-fix-builder",
                    "transition_receipt_sha256": self.args.receipt,
                },
            ],
            "completed_role_evidence": [prior_completed],
            "contract_version": self.args.contract_version,
            "current_stage": "FIX builder",
            "current_state": "Blocked-Escalated",
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
            "head_sha": answer_head,
            "migration_history": [{
                "from_head_sha": blocked_head,
                "to_head_sha": answer_head,
            }],
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
        preserved_charges = json.loads(json.dumps(body["charge_records"]))
        preserved_roles = json.loads(json.dumps(body["completed_role_evidence"]))

        self.args.factory_sha = current_factory
        self.args.action = "resume"
        receipt = STATE.safe_receipt(self.state_dir / "T-110.json")
        with mock.patch.object(
            STATE, "contract_repair_stage", return_value=(None, False)
        ):
            with self.assertRaisesRegex(
                STATE.StateError, "contract blocker role state drifted"
            ):
                STATE.contract_block_resume_state(
                    self.args,
                    "builder",
                    "Review",
                    receipt,
                    {**passport, "passport_sha256": "0" * 64},
                )

        def materialize(*_args, **_kwargs):
            ticket.write_text(
                ticket.read_text(encoding="utf-8").replace(
                    "State: Blocked-Escalated", "State: Review"
                ),
                encoding="utf-8",
            )
            return ""

        with (
            mock.patch.object(STATE, "run_helper", side_effect=materialize),
            mock.patch.object(STATE, "migrate_passport") as migrate,
        ):
            result = STATE.resume_transition(self.args)

        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["ticket"], "T-110")
        self.assertEqual(result["head"], resume_head)
        self.assertEqual(result["role"], "builder")
        self.assertEqual(result["repair_role"], "planner")
        resumed = ticket.read_text(encoding="utf-8")
        self.assertIn(
            "OPERATOR ANSWER: Preserve the authenticated planner decision.",
            resumed,
        )
        self.assertIn("OPERATOR RESUME: planner", resumed)
        retained, _ = STATE.authenticated_passport(self.args)
        self.assertEqual(retained["charge_records"], preserved_charges)
        self.assertEqual(retained["completed_role_evidence"], preserved_roles)
        self.assertEqual(
            STATE.load_repair(self.args, secret)["blocked_receipt"],
            self.args.receipt,
        )
        migrate.assert_called_once_with(self.args)

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
        with self.assertRaises(STATE.ContractResumeError) as raised:
            STATE.operator_resume_role(self.args, passport, "builder")
        self.assertEqual(raised.exception.reason_code, "resume_ancestry_invalid")

    def test_operator_resume_accepts_only_one_safe_unmigrated_context_commit(self) -> None:
        self.args.receipt = "b" * 64
        ticket = self.product / "factory/tickets/T-110.md"
        initiative = self.product / "factory/initiatives/I-100.md"
        initiative.parent.mkdir()
        initiative.write_text("# I-100\n", encoding="utf-8")
        fixture = self.product / "apps/api/tests/example.test.ts"
        fixture.parent.mkdir(parents=True)
        fixture.write_text("export const expected = 'expected-literal';\n")
        sibling_fixture = self.product / "apps/api/tests/sibling.test.ts"
        sibling_fixture.write_text("export const expected = 'sibling-literal';\n")
        ticket.write_text(
            "# T-110\n\nState: Planning\n"
            "Priority: normal\n"
            "Initiative: I-100\n"
            "Depends-On: none\n"
            "Product-Decisions: frozen\n"
            "Builder ownership: README.md only\n"
            "Fixture-Seams: apps/api/tests/example.test.ts, "
            "apps/api/tests/sibling.test.ts\n"
            "Authentication-Seams: none\n"
            "Protected-Test-Conflicts: none\n",
            encoding="utf-8",
        )
        project = self.product / "factory/PROJECT.env"
        project.write_text('TEST_PATHS="apps/api/tests/"\n')
        rulings = self.product / "factory/rulings.md"
        rulings.write_text("# Rulings\n\nT-100: preserve prior ruling.\n")
        run(
            "git", "add", str(ticket), str(initiative), str(rulings), str(fixture),
            str(sibling_fixture), str(project), cwd=self.product,
        )
        run("git", "commit", "-qm", "add operator fields", cwd=self.product)
        base = run("git", "rev-parse", "HEAD", cwd=self.product)
        passport = {
            "branch": "ticket/T-110",
            "factory_sha": self.args.factory_sha,
            "head_sha": base,
            "protected_base_sha": base,
            "ticket": "T-110",
        }
        entry = (
            "apps/api/tests/example.test.ts => expected-literal, "
            "apps/api/tests/sibling.test.ts => sibling-literal"
        )

        def commit_resume() -> str:
            ticket.write_text(
                ticket.read_text(encoding="utf-8").rstrip()
                + "\n\nOPERATOR RESUME: test-author\n"
                + f"OPERATOR RESUME RECEIPT: {self.args.receipt}\n",
                encoding="utf-8",
            )
            run("git", "add", str(ticket), cwd=self.product)
            run("git", "commit", "-qm", "resume after operator context", cwd=self.product)
            return run("git", "rev-parse", "HEAD", cwd=self.product)

        ticket.write_text(
            ticket.read_text().replace(
                "Protected-Test-Conflicts: none",
                f"Protected-Test-Conflicts: {entry}",
            )
        )
        run("git", "add", str(ticket), cwd=self.product)
        run("git", "commit", "-qm", "record operator context", cwd=self.product)
        commit_resume()
        self.assertEqual(
            STATE.operator_resume_role(self.args, passport, "builder"),
            "test-author",
        )
        self.assertEqual(
            STATE.operator_resume_role(self.args, passport, "builder"),
            "test-author",
        )

        def assert_refused(label: str, extra_path: str | None = None,
                           change_state: bool = False, two_commits: bool = False,
                           ruling_change: str | None = None) -> None:
            run("git", "reset", "--hard", base, cwd=self.product)
            ticket.write_text(
                ticket.read_text().replace(
                    "Protected-Test-Conflicts: none",
                    f"Protected-Test-Conflicts: {entry}",
                ).replace(
                    "State: Planning",
                    "State: Building" if change_state else "State: Planning",
                )
            )
            run("git", "add", str(ticket), cwd=self.product)
            if extra_path:
                path = self.product / extra_path
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("unapproved context\n")
                run("git", "add", extra_path, cwd=self.product)
            if ruling_change == "rewrite":
                rulings.write_text("# Rulings\n\nT-110: replace every ruling.\n")
                run("git", "add", str(rulings), cwd=self.product)
            elif ruling_change == "append":
                rulings.write_text(
                    rulings.read_text(encoding="utf-8")
                    + "T-110: append an unauthorized ruling.\n",
                    encoding="utf-8",
                )
                run("git", "add", str(rulings), cwd=self.product)
            elif ruling_change == "delete":
                rulings.unlink()
                run("git", "add", "-u", str(rulings), cwd=self.product)
            run("git", "commit", "-qm", f"{label} context", cwd=self.product)
            if two_commits:
                (self.product / "factory/rulings.md").write_text("late ruling\n")
                run("git", "add", "factory/rulings.md", cwd=self.product)
                run("git", "commit", "-qm", "second context", cwd=self.product)
            parent = run("git", "rev-parse", "HEAD", cwd=self.product)
            commit_resume()
            with self.assertRaises(STATE.ContractResumeError) as raised:
                STATE.operator_resume_role(self.args, passport, "builder")
            self.assertEqual(
                raised.exception.reason_code, "resume_parent_not_migrated"
            )
            self.assertEqual(raised.exception.evidence["offending_parent"], parent)

        for label, extra_path, change_state, two_commits, ruling_change in (
            ("application", "app.js", False, False, None),
            ("other-control", "factory/other.md", False, False, None),
            ("sibling", "factory/tickets/T-111.md", False, False, None),
            ("protected-field", None, True, False, None),
            ("rulings-append", None, False, False, "append"),
            ("rulings-rewrite", None, False, False, "rewrite"),
            ("rulings-delete", None, False, False, "delete"),
            ("two-intermediates", None, False, True, None),
        ):
            with self.subTest(label=label):
                assert_refused(
                    label, extra_path, change_state, two_commits, ruling_change,
                )

        run("git", "reset", "--hard", base, cwd=self.product)
        run("git", "checkout", "-qb", "operator-context-side", cwd=self.product)
        ticket.write_text(ticket.read_text().replace(
            "Protected-Test-Conflicts: none",
            f"Protected-Test-Conflicts: {entry}",
        ))
        run("git", "add", str(ticket), cwd=self.product)
        run("git", "commit", "-qm", "side context", cwd=self.product)
        run("git", "checkout", "-q", "ticket/T-110", cwd=self.product)
        (self.product / "factory/rulings.md").write_text("parallel ruling\n")
        run("git", "add", "factory/rulings.md", cwd=self.product)
        run("git", "commit", "-qm", "parallel ruling", cwd=self.product)
        run(
            "git", "merge", "-q", "--no-ff", "operator-context-side",
            "-m", "merge operator context", cwd=self.product,
        )
        merge_parent = run("git", "rev-parse", "HEAD", cwd=self.product)
        commit_resume()
        with self.assertRaises(STATE.ContractResumeError) as raised:
            STATE.operator_resume_role(self.args, passport, "builder")
        self.assertEqual(raised.exception.reason_code, "resume_parent_not_migrated")
        self.assertEqual(raised.exception.evidence["offending_parent"], merge_parent)

    def test_operator_resume_accepts_receipt_bound_answer_context(self) -> None:
        prior_receipt = "a" * 64
        self.args.receipt = "b" * 64
        ticket = self.product / "factory/tickets/T-110.md"

        def commit_resume() -> None:
            ticket.write_text(
                ticket.read_text(encoding="utf-8").rstrip()
                + "\n\nOPERATOR RESUME: builder\n"
                + f"OPERATOR RESUME RECEIPT: {self.args.receipt}\n",
                encoding="utf-8",
            )
            run("git", "add", str(ticket), cwd=self.product)
            run("git", "commit", "-qm", "resume after operator answer", cwd=self.product)

        base = run("git", "rev-parse", "HEAD", cwd=self.product)
        passport = {
            "branch": "ticket/T-110",
            "factory_sha": self.args.factory_sha,
            "head_sha": base,
            "ticket": "T-110",
        }
        ticket.write_text(
            ticket.read_text(encoding="utf-8").rstrip()
            + "\n\nOPERATOR ANSWER: Preserve the exact authenticated seam.\n"
            + f"OPERATOR ANSWER RECEIPT: {self.args.receipt}\n",
            encoding="utf-8",
        )
        run("git", "add", str(ticket), cwd=self.product)
        run("git", "commit", "-qm", "record receipt-bound answer", cwd=self.product)
        with self.assertRaisesRegex(STATE.StateError, "requires exactly one"):
            STATE.operator_resume_role(self.args, passport, "builder")
        commit_resume()
        self.assertEqual(
            STATE.operator_resume_role(self.args, passport, "builder"), "builder"
        )
        self.assertEqual(
            STATE.operator_resume_role(self.args, passport, "builder"), "builder"
        )

        run("git", "reset", "--hard", base, cwd=self.product)
        ticket.write_text(
            ticket.read_text(encoding="utf-8").rstrip()
            + "\n\nOPERATOR ANSWER: Preserve the prior fixture.\n"
            + f"OPERATOR ANSWER RECEIPT: {prior_receipt}\n",
            encoding="utf-8",
        )
        run("git", "add", str(ticket), cwd=self.product)
        run("git", "commit", "-qm", "record prior operator answer", cwd=self.product)
        prior_answer_head = run("git", "rev-parse", "HEAD", cwd=self.product)
        passport["head_sha"] = prior_answer_head
        commit_resume()
        with self.assertRaises(STATE.ContractResumeError) as raised:
            STATE.operator_resume_role(self.args, passport, "builder")
        self.assertEqual(raised.exception.reason_code, "resume_parent_not_migrated")
        run("git", "reset", "--hard", prior_answer_head, cwd=self.product)
        ticket.write_text(
            ticket.read_text(encoding="utf-8")
            .replace("Preserve the prior fixture.", "Use the current isolated seam.")
            .replace(prior_receipt, self.args.receipt),
            encoding="utf-8",
        )
        run("git", "add", str(ticket), cwd=self.product)
        run("git", "commit", "-qm", "replace answer for later blocker", cwd=self.product)
        context_head = run("git", "rev-parse", "HEAD", cwd=self.product)
        passport["head_sha"] = context_head
        passport["migration_history"] = [{
            "from_head_sha": prior_answer_head,
            "to_head_sha": context_head,
        }]
        commit_resume()
        self.assertEqual(
            STATE.operator_resume_role(self.args, passport, "builder"), "builder"
        )

        run("git", "reset", "--hard", base, cwd=self.product)
        ticket.write_text(
            ticket.read_text(encoding="utf-8").rstrip()
            + "\n\nOPERATOR ANSWER: This must not authorize application drift.\n"
            + f"OPERATOR ANSWER RECEIPT: {self.args.receipt}\n",
            encoding="utf-8",
        )
        application = self.product / "app.js"
        application.write_text("unsafe context\n")
        run("git", "add", str(ticket), str(application), cwd=self.product)
        run("git", "commit", "-qm", "record unsafe migrated answer", cwd=self.product)
        unsafe_head = run("git", "rev-parse", "HEAD", cwd=self.product)
        passport["head_sha"] = unsafe_head
        passport["migration_history"] = [{
            "from_head_sha": base,
            "to_head_sha": unsafe_head,
        }]
        commit_resume()
        with self.assertRaises(STATE.ContractResumeError) as raised:
            STATE.operator_resume_role(self.args, passport, "builder")
        self.assertEqual(raised.exception.reason_code, "resume_parent_not_migrated")
        self.assertEqual(raised.exception.evidence["offending_parent"], unsafe_head)

    def test_repair_check_validates_context_before_passport_migration(self) -> None:
        self.args.receipt = "b" * 64
        ticket = self.product / "factory/tickets/T-110.md"
        ticket.write_text(
            "# T-110\n\nState: Blocked-Escalated\n"
            "Resume-State: Planning\n\n"
            "ROLE-ESCALATE: CONTRACT-BLOCKED\n"
            f"Kit-SHA: {self.args.factory_sha}\n",
            encoding="utf-8",
        )
        run("git", "add", str(ticket), cwd=self.product)
        run("git", "commit", "-qm", "record contract blocker", cwd=self.product)
        base = run("git", "rev-parse", "HEAD", cwd=self.product)
        passport = {
            "branch": "ticket/T-110",
            "factory_sha": self.args.factory_sha,
            "head_sha": base,
            "ticket": "T-110",
        }

        def check() -> dict:
            with (
                mock.patch.object(
                    STATE, "contract_blocked_receipt", return_value="planner"
                ),
                mock.patch.object(
                    STATE, "authenticated_passport",
                    return_value=(passport, b"k" * 32),
                ),
            ):
                return STATE.repair_check_transition(self.args)

        answer = (
            "OPERATOR ANSWER: Preserve the authenticated decision.\n"
            f"OPERATOR ANSWER RECEIPT: {self.args.receipt}\n"
        )
        ticket.write_text(
            ticket.read_text(encoding="utf-8").replace(
                "ROLE-ESCALATE: CONTRACT-BLOCKED\n", answer
                + "ROLE-ESCALATE: CONTRACT-BLOCKED\n",
            ),
            encoding="utf-8",
        )
        run("git", "add", str(ticket), cwd=self.product)
        run("git", "commit", "-qm", "misplace operator answer", cwd=self.product)
        offending = run("git", "rev-parse", "HEAD", cwd=self.product)
        ticket.write_text(
            ticket.read_text(encoding="utf-8").rstrip()
            + "\n\nOPERATOR RESUME: planner\n"
            + f"OPERATOR RESUME RECEIPT: {self.args.receipt}\n",
            encoding="utf-8",
        )
        run("git", "add", str(ticket), cwd=self.product)
        run("git", "commit", "-qm", "resume malformed operator answer", cwd=self.product)
        with self.assertRaises(STATE.ContractResumeError) as raised:
            check()
        self.assertEqual(raised.exception.reason_code, "resume_parent_not_migrated")
        self.assertEqual(raised.exception.evidence["offending_parent"], offending)
        self.assertEqual(passport["head_sha"], base)

        run("git", "reset", "--hard", base, cwd=self.product)
        ticket.write_text(
            ticket.read_text(encoding="utf-8").rstrip()
            + "\n\n" + answer,
            encoding="utf-8",
        )
        run("git", "add", str(ticket), cwd=self.product)
        run("git", "commit", "-qm", "record canonical operator answer", cwd=self.product)
        answer_head = run("git", "rev-parse", "HEAD", cwd=self.product)
        waiting = check()
        self.assertEqual(waiting["status"], "waiting")
        self.assertEqual(waiting["head"], answer_head)

        passport.update(
            head_sha=answer_head,
            migration_history=[{
                "from_head_sha": base,
                "schema": STATE.PASSPORT_MIGRATION_SCHEMA,
                "to_factory_sha": self.args.factory_sha,
                "to_head_sha": answer_head,
            }],
        )
        recovered_wait = check()
        self.assertEqual(recovered_wait["status"], "waiting")
        self.assertEqual(recovered_wait["head"], answer_head)

        ticket.write_text(
            ticket.read_text(encoding="utf-8").rstrip()
            + "\n\nOPERATOR RESUME: planner\n"
            + f"OPERATOR RESUME RECEIPT: {self.args.receipt}\n",
            encoding="utf-8",
        )
        run("git", "add", str(ticket), cwd=self.product)
        run("git", "commit", "-qm", "resume canonical operator answer", cwd=self.product)
        fast = check()
        self.assertEqual(fast["status"], "ready")
        self.assertEqual(fast["current_state"], "Blocked-Escalated")
        self.assertEqual(fast["resume_state"], "Planning")
        canonical = check()
        self.assertEqual(canonical["status"], "ready")

        ticket.write_text(
            ticket.read_text(encoding="utf-8").replace(
                "State: Blocked-Escalated", "State: Planning",
            ),
            encoding="utf-8",
        )
        run("git", "add", str(ticket), cwd=self.product)
        run(
            "git", "-c", "user.name=Software Factory",
            "-c", "user.email=factory@local", "commit", "-qm",
            "T-110: materialize ticket state", cwd=self.product,
        )
        materialized_head = run("git", "rev-parse", "HEAD", cwd=self.product)
        self.args.qualification_recovery = True
        operator_map = self.root / "operator/operator-map.json"
        operator_map.parent.mkdir(mode=0o700)
        operator_map.write_text(
            json.dumps({
                "_config": None, "_sync": {}, "initiatives": {},
                "tickets": {},
            }),
            encoding="utf-8",
        )
        environment = {
            "FACTORY_KIT_TRUST_SCOPE": "qualification-candidate",
            "FACTORY_OPERATOR_MAP": str(operator_map),
            "FACTORY_QUALIFICATION_MODE": "isolated",
        }
        secret = b"k" * 32
        passport["passport_sha256"] = "e" * 64
        for consumed in (False, True):
            with self.subTest(consumed=consumed):
                shutil.rmtree(
                    self.state_dir / "operator-receipts", ignore_errors=True,
                )
                authority = STATE.operator_receipt.issue(
                    self.state_dir, "T-110", "resume", {
                        "blocked_receipt_sha256": self.args.receipt,
                        "resume_stage": "Planning",
                    },
                )
                if consumed:
                    STATE.operator_receipt.verify_consume_exact(
                        self.state_dir, "T-110", "resume",
                        authority["receipt_sha256"], authority["payload"],
                    )
                operator_map.write_text(json.dumps({
                    "_config": None, "_sync": {}, "initiatives": {},
                    "tickets": {"T-110": {"operator": {
                        "observed_at": authority["issued_at"],
                        "receipt_sha256": authority["receipt_sha256"],
                        "state": "Planning",
                        "state_base": "blocked-escalated",
                    }}},
                }), encoding="utf-8")

                def finish_materialization(*_args, **_kwargs) -> str:
                    STATE.operator_receipt.verify_consume_replay_exact(
                        self.state_dir, "T-110", "resume",
                        authority["receipt_sha256"], authority["payload"],
                    )
                    operator_map.write_text(json.dumps({
                        "_config": None, "_sync": {}, "initiatives": {},
                        "tickets": {},
                    }), encoding="utf-8")
                    return ""

                with (
                    mock.patch.dict(os.environ, environment),
                    mock.patch.object(
                        STATE, "contract_blocked_receipt", return_value="planner",
                    ),
                    mock.patch.object(
                        STATE, "authenticated_passport",
                        return_value=(passport, secret),
                    ),
                    mock.patch.object(STATE, "migrate_passport"),
                    mock.patch.object(
                        STATE, "run_helper", side_effect=finish_materialization,
                    ) as helper,
                ):
                    resumed = STATE.resume_transition(self.args)
                self.assertEqual(resumed["status"], "ready")
                helper.assert_called_once()
                self.assertEqual(helper.call_args.kwargs["extra_environment"], {
                    "FACTORY_BLOCKED_RECEIPT": self.args.receipt,
                    "FACTORY_QUALIFICATION_REPLAY": "1",
                })
                self.assertTrue(STATE.operator_receipt.read_exact(
                    self.state_dir, "T-110", "resume",
                    authority["receipt_sha256"], authority["payload"],
                )["consumed"])

        shutil.rmtree(self.state_dir / "operator-receipts", ignore_errors=True)
        authority = STATE.operator_receipt.issue(
            self.state_dir, "T-110", "resume", {
                "blocked_receipt_sha256": self.args.receipt,
                "resume_stage": "Planning",
            },
        )
        STATE.operator_receipt.verify_consume_exact(
            self.state_dir, "T-110", "resume", authority["receipt_sha256"],
            authority["payload"],
        )
        with mock.patch.dict(os.environ, environment):
            materialized = check()
        self.assertEqual(materialized["status"], "ready")
        self.assertEqual(materialized["current_state"], "Planning")
        self.assertEqual(materialized["resume_state"], "Planning")
        with (
            mock.patch.dict(os.environ, environment),
            mock.patch.object(
                STATE, "contract_blocked_receipt", return_value="planner",
            ),
            mock.patch.object(
                STATE, "authenticated_passport",
                return_value=(passport, secret),
            ),
            mock.patch.object(STATE, "migrate_passport"),
        ):
            resumed = STATE.resume_transition(self.args)
        self.assertEqual(resumed["status"], "ready")
        self.assertEqual(resumed["head"], materialized_head)
        repair = STATE.load_signed_repair(STATE.repair_path(self.args), secret)
        self.assertEqual(repair["blocked_receipt"], self.args.receipt)
        self.assertEqual(repair["head_sha"], materialized_head)

    def test_operator_resume_accepts_coupled_conflict_fixture_and_answer(self) -> None:
        self.args.receipt = "b" * 64
        ticket = self.product / "factory/tickets/T-110.md"
        initiative = self.product / "factory/initiatives/I-100.md"
        initiative.parent.mkdir()
        initiative.write_text("# I-100\n", encoding="utf-8")
        fixture = self.product / "apps/api/tests/example.test.ts"
        fixture.parent.mkdir(parents=True)
        fixture.write_text("export const expected = 'expected-literal';\n")
        application = self.product / "apps/api/src/server.ts"
        application.parent.mkdir(parents=True)
        application.write_text("export const server = true;\n")
        workflow = self.product / ".github/workflows/ci.yml"
        workflow.parent.mkdir(parents=True)
        workflow.write_text("name: CI\n")
        factory_control = self.product / "factory/control.sh"
        factory_control.write_text("#!/bin/sh\n")
        project = self.product / "factory/PROJECT.env"
        project.write_text('TEST_PATHS="apps/api/tests/"\n')
        ticket.write_text(
            "# T-110\n\nState: Planning\n"
            "Priority: normal\n"
            "Initiative: I-100\n"
            "Depends-On: none\n"
            "Product-Decisions: frozen\n"
            "Builder ownership: README.md only\n"
            "Fixture-Seams: none\n"
            "Authentication-Seams: none\n"
            "Protected-Test-Conflicts: none\n",
            encoding="utf-8",
        )
        run(
            "git", "add", str(ticket), str(initiative), str(fixture), str(application),
            str(workflow), str(factory_control), str(project), cwd=self.product,
        )
        run("git", "commit", "-qm", "seed protected fixture contract", cwd=self.product)
        base = run("git", "rev-parse", "HEAD", cwd=self.product)
        passport = {
            "branch": "ticket/T-110",
            "factory_sha": self.args.factory_sha,
            "head_sha": base,
            "protected_base_sha": base,
            "ticket": "T-110",
        }
        relative = "apps/api/tests/example.test.ts"
        entry = (
            f"{relative} => expected-literal, "
            f"{relative} => alternate-literal"
        )
        before = ticket.read_text(encoding="utf-8")
        self.assertFalse(STATE.safe_operator_context(
            self.args,
            before,
            before.replace("Fixture-Seams: none", f"Fixture-Seams: {relative}")
            .replace(
                "Protected-Test-Conflicts: none",
                "Protected-Test-Conflicts: apps/api/src/server.ts => export, "
                f"{relative} => expected-literal",
            ),
            base,
        ))
        ticket.write_text(
            before
            .replace("Fixture-Seams: none", f"Fixture-Seams: {relative}")
            .replace(
                "Protected-Test-Conflicts: none",
                f"Protected-Test-Conflicts: {entry}",
            ).rstrip()
            + "\n\nOPERATOR ANSWER: The protected literal is intentional.\n"
            + f"OPERATOR ANSWER RECEIPT: {self.args.receipt}\n",
            encoding="utf-8",
        )
        run("git", "add", str(ticket), cwd=self.product)
        run("git", "commit", "-qm", "record coupled operator context", cwd=self.product)
        context_head = run("git", "rev-parse", "HEAD", cwd=self.product)
        passport["head_sha"] = context_head
        passport["migration_history"] = [{
            "from_head_sha": base,
            "to_head_sha": context_head,
        }]
        ticket.write_text(
            ticket.read_text(encoding="utf-8").rstrip()
            + "\n\nOPERATOR RESUME: test-author\n"
            + f"OPERATOR RESUME RECEIPT: {self.args.receipt}\n",
            encoding="utf-8",
        )
        run("git", "add", str(ticket), cwd=self.product)
        run("git", "commit", "-qm", "resume protected fixture repair", cwd=self.product)
        self.assertEqual(
            STATE.operator_resume_role(self.args, passport, "builder"),
            "test-author",
        )

        def commit_context_and_resume(path: str, *, add_fixture: bool) -> str:
            run("git", "reset", "--hard", base, cwd=self.product)
            current = ticket.read_text(encoding="utf-8")
            if add_fixture:
                current = current.replace(
                    "Fixture-Seams: none", f"Fixture-Seams: {path}"
                )
            ticket.write_text(
                current.replace(
                    "Protected-Test-Conflicts: none",
                    f"Protected-Test-Conflicts: {path} => expected-literal",
                ),
                encoding="utf-8",
            )
            run("git", "add", str(ticket), cwd=self.product)
            run("git", "commit", "-qm", "record unsafe operator context", cwd=self.product)
            parent = run("git", "rev-parse", "HEAD", cwd=self.product)
            passport["head_sha"] = parent
            passport["migration_history"] = [{
                "from_head_sha": base,
                "to_head_sha": parent,
            }]
            ticket.write_text(
                ticket.read_text(encoding="utf-8").rstrip()
                + "\n\nOPERATOR RESUME: test-author\n"
                + f"OPERATOR RESUME RECEIPT: {self.args.receipt}\n",
                encoding="utf-8",
            )
            run("git", "add", str(ticket), cwd=self.product)
            run("git", "commit", "-qm", "attempt unsafe fixture resume", cwd=self.product)
            return parent

        parent = commit_context_and_resume(relative, add_fixture=False)
        with self.assertRaises(STATE.ContractResumeError) as raised:
            STATE.operator_resume_role(self.args, passport, "builder")
        self.assertEqual(raised.exception.reason_code, "resume_parent_not_migrated")
        self.assertEqual(raised.exception.evidence["offending_parent"], parent)

        for unsafe in (
            "apps/api/src/server.ts",
            ".github/workflows/ci.yml",
            "factory/control.sh",
        ):
            with self.subTest(unsafe_test_ownership=unsafe):
                parent = commit_context_and_resume(unsafe, add_fixture=True)
                with self.assertRaises(STATE.ContractResumeError) as raised:
                    STATE.operator_resume_role(self.args, passport, "builder")
                self.assertEqual(
                    raised.exception.reason_code, "resume_parent_not_migrated"
                )
                self.assertEqual(
                    raised.exception.evidence["offending_parent"], parent
                )

        run("git", "reset", "--hard", base, cwd=self.product)
        unsafe = "apps/api/src/server.ts"
        ticket.write_text(
            ticket.read_text(encoding="utf-8").replace(
                "Fixture-Seams: none", f"Fixture-Seams: {unsafe}"
            ),
            encoding="utf-8",
        )
        run("git", "add", str(ticket), cwd=self.product)
        run("git", "commit", "-qm", "seed unsafe preowned seam", cwd=self.product)
        preowned_head = run("git", "rev-parse", "HEAD", cwd=self.product)
        ticket.write_text(
            ticket.read_text(encoding="utf-8").replace(
                "Protected-Test-Conflicts: none",
                f"Protected-Test-Conflicts: {unsafe} => expected-literal",
            ),
            encoding="utf-8",
        )
        run("git", "add", str(ticket), cwd=self.product)
        run("git", "commit", "-qm", "append conflict to unsafe seam", cwd=self.product)
        context_head = run("git", "rev-parse", "HEAD", cwd=self.product)
        passport["head_sha"] = context_head
        passport["migration_history"] = [{
            "from_head_sha": preowned_head,
            "to_head_sha": context_head,
        }]
        ticket.write_text(
            ticket.read_text(encoding="utf-8").rstrip()
            + "\n\nOPERATOR RESUME: test-author\n"
            + f"OPERATOR RESUME RECEIPT: {self.args.receipt}\n",
            encoding="utf-8",
        )
        run("git", "add", str(ticket), cwd=self.product)
        run("git", "commit", "-qm", "attempt preowned unsafe resume", cwd=self.product)
        with self.assertRaises(STATE.ContractResumeError) as raised:
            STATE.operator_resume_role(self.args, passport, "builder")
        self.assertEqual(raised.exception.reason_code, "resume_parent_not_migrated")
        self.assertEqual(
            raised.exception.evidence["offending_parent"], context_head
        )

    def test_safe_operator_context_refuses_ambiguous_or_broad_answers(self) -> None:
        self.args.receipt = "b" * 64
        before = "# T-110\n\nState: Planning\n"
        answer = (
            "\nOPERATOR ANSWER: Keep the bounded decision.\n"
            f"OPERATOR ANSWER RECEIPT: {self.args.receipt}\n"
        )
        self.assertTrue(STATE.safe_operator_context(self.args, before, before + answer))

        prior = (
            before
            + "\nOPERATOR ANSWER: Prior blocker decision.\n"
            + f"OPERATOR ANSWER RECEIPT: {'a' * 64}\n"
        )
        replacement = (
            before
            + "\nOPERATOR ANSWER: Current blocker decision.\n"
            + f"OPERATOR ANSWER RECEIPT: {self.args.receipt}\n"
        )
        self.assertTrue(STATE.safe_operator_context(self.args, prior, replacement))
        self.assertFalse(STATE.safe_operator_context(
            self.args,
            prior,
            prior.replace("Prior blocker decision.", "Stale blocker decision."),
        ))

        conflicts = (
            "Protected-Test-Conflicts: apps/api/tests/one.test.ts => one, "
            "apps/api/tests/two.test.ts => two\n"
        )
        for label, changed in {
            "removed": "Protected-Test-Conflicts: apps/api/tests/one.test.ts => one\n",
            "reordered": (
                "Protected-Test-Conflicts: apps/api/tests/two.test.ts => two, "
                "apps/api/tests/one.test.ts => one\n"
            ),
            "duplicated": conflicts.replace(
                "\n", ", apps/api/tests/two.test.ts => two\n"
            ),
        }.items():
            with self.subTest(conflicts=label):
                self.assertFalse(STATE.safe_operator_context(
                    self.args, conflicts, changed,
                ))

        invalid = {
            "wrong-receipt": answer.replace(self.args.receipt, "c" * 64),
            "partial-answer": "\nOPERATOR ANSWER: Missing receipt.\n",
            "partial-receipt": f"\nOPERATOR ANSWER RECEIPT: {self.args.receipt}\n",
            "ambiguous": answer + answer,
            "malformed-receipt": (
                "\nOPERATOR ANSWER: Bad receipt.\n"
                "OPERATOR ANSWER RECEIPT: not-a-receipt\n"
            ),
            "oversized": (
                "\nOPERATOR ANSWER: "
                + ("x" * (STATE.OPERATOR_ANSWER_MAX_BYTES + 1))
                + "\n"
                f"OPERATOR ANSWER RECEIPT: {self.args.receipt}\n"
            ),
            "non-printable": answer.replace("bounded", "bounded\t"),
            "state": answer + "State: Building\n",
            "kit": answer + f"Kit-SHA: {'c' * 40}\n",
            "route": answer + "Route-Plan: alternate\n",
            "contract": answer + "Contract: changed\n",
            "provider": answer + "Provider: alternate\n",
            "application": answer + "Application: changed\n",
            "test": answer + "Tests: skipped\n",
            "ci": answer + "CI: waived\n",
        }
        for label, addition in invalid.items():
            with self.subTest(label=label):
                self.assertFalse(
                    STATE.safe_operator_context(self.args, before, before + addition)
                )

    def test_operator_resume_names_overfull_commit_with_safe_diff(self) -> None:
        self.args.receipt = "b" * 64
        path = self.product / "factory/tickets/T-110.md"
        before = path.read_text(encoding="utf-8")
        passport = {
            "branch": "ticket/T-110",
            "factory_sha": self.args.factory_sha,
            "head_sha": run("git", "rev-parse", "HEAD", cwd=self.product),
            "ticket": "T-110",
        }
        path.write_text(
            before.rstrip("\n")
            + "\n\nOPERATOR RESUME: builder\n"
            + f"OPERATOR RESUME RECEIPT: {self.args.receipt}\n"
            + "Operator ruling: preserve the protected test.\n",
            encoding="utf-8",
        )
        run("git", "add", str(path), cwd=self.product)
        run("git", "commit", "-qm", "overfull contract repair", cwd=self.product)

        with self.assertRaises(STATE.ContractResumeError) as raised:
            STATE.operator_resume_role(self.args, passport, "builder")
        error = raised.exception
        self.assertEqual(error.reason_code, "resume_commit_content_mismatch")
        self.assertEqual(error.evidence["actual_bytes"], len(path.read_bytes()))
        self.assertGreater(error.evidence["actual_bytes"], error.evidence["expected_bytes"])
        self.assertIsInstance(error.evidence["first_differing_line"], int)

    def test_operator_resume_accepts_exact_compact_directive_pair(self) -> None:
        self.args.receipt = "b" * 64
        path = self.product / "factory/tickets/T-110.md"
        passport = {
            "branch": "ticket/T-110",
            "factory_sha": self.args.factory_sha,
            "head_sha": run("git", "rev-parse", "HEAD", cwd=self.product),
            "ticket": "T-110",
        }
        path.write_text(
            path.read_text(encoding="utf-8").rstrip("\n")
            + "\nOPERATOR RESUME: planner\n"
            + f"OPERATOR RESUME RECEIPT: {self.args.receipt}\n",
            encoding="utf-8",
        )
        run("git", "add", str(path), cwd=self.product)
        run("git", "commit", "-qm", "compact contract repair", cwd=self.product)

        self.assertEqual(
            STATE.operator_resume_role(self.args, passport, "planner"),
            "planner",
        )

    def test_operator_resume_names_ambiguous_directive_pairs(self) -> None:
        self.args.receipt = "b" * 64
        path = self.product / "factory/tickets/T-110.md"
        passport = {
            "branch": "ticket/T-110",
            "factory_sha": self.args.factory_sha,
            "head_sha": run("git", "rev-parse", "HEAD", cwd=self.product),
            "ticket": "T-110",
        }
        path.write_text(
            path.read_text(encoding="utf-8").rstrip("\n")
            + "\n\nOPERATOR RESUME: builder\n"
            + f"OPERATOR RESUME RECEIPT: {self.args.receipt}\n"
            + "OPERATOR RESUME: planner\n"
            + f"OPERATOR RESUME RECEIPT: {self.args.receipt}\n",
            encoding="utf-8",
        )
        run("git", "add", str(path), cwd=self.product)
        run("git", "commit", "-qm", "ambiguous contract repair", cwd=self.product)

        with self.assertRaises(STATE.ContractResumeError) as raised:
            STATE.operator_resume_role(self.args, passport, "builder")
        self.assertEqual(
            raised.exception.reason_code, "resume_directives_ambiguous"
        )

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

    def test_operator_resume_authenticates_role_only_reissue(self) -> None:
        self.args.receipt = "b" * 64
        path = self.product / "factory/tickets/T-110.md"
        path.write_text(
            path.read_text(encoding="utf-8").rstrip("\n")
            + "\n\nOPERATOR RESUME: builder\n"
            + f"OPERATOR RESUME RECEIPT: {self.args.receipt}\n",
            encoding="utf-8",
        )
        run("git", "add", str(path), cwd=self.product)
        run("git", "commit", "-qm", "authorize builder repair", cwd=self.product)
        blocked_head = run("git", "rev-parse", "HEAD", cwd=self.product)
        passport = {
            "branch": "ticket/T-110",
            "factory_sha": self.args.factory_sha,
            "head_sha": blocked_head,
            "ticket": "T-110",
        }
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                "OPERATOR RESUME: builder", "OPERATOR RESUME: planner"
            ),
            encoding="utf-8",
        )
        run("git", "add", str(path), cwd=self.product)
        run("git", "commit", "-qm", "reissue repair to planner", cwd=self.product)

        self.assertEqual(
            STATE.operator_resume_role(self.args, passport, "builder"),
            "planner",
        )

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
        with self.assertRaises(STATE.ContractResumeError) as raised:
            STATE.operator_resume_role(self.args, passport, "test-author")
        self.assertEqual(
            raised.exception.reason_code, "resume_parent_not_migrated"
        )

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
        prior_receipt = "a" * 64
        secret = b"k" * 32
        (self.state_dir / "passport.key").write_bytes(secret)
        os.chmod(self.state_dir / "passport.key", 0o600)
        ticket = self.product / "factory/tickets/T-110.md"
        ticket.write_text(
            ticket.read_text(encoding="utf-8").rstrip("\n")
            + "\n\nOPERATOR RESUME: test-author\n"
            + f"OPERATOR RESUME RECEIPT: {prior_receipt}\n",
            encoding="utf-8",
        )
        run("git", "add", str(ticket), cwd=self.product)
        run("git", "commit", "-qm", "preserve historical directive", cwd=self.product)
        prior_head = run("git", "rev-parse", "HEAD", cwd=self.product)
        prior_tree = run("git", "rev-parse", "HEAD^{tree}", cwd=self.product)
        ticket.write_text(
            ticket.read_text(encoding="utf-8")
            + "OPERATOR RESUME: planner\n"
            + f"OPERATOR RESUME RECEIPT: {self.args.receipt}\n",
            encoding="utf-8",
        )
        run("git", "add", str(ticket), cwd=self.product)
        tree = run("git", "write-tree", cwd=self.product)
        head = run(
            "git", "commit-tree", tree, "-m", "reconstruct repair history",
            cwd=self.product,
        )
        run("git", "update-ref", "HEAD", head, cwd=self.product)
        passports = self.state_dir / "passports"
        passports.mkdir(mode=0o700)
        protected_base = "c" * 40
        route = "d" * 64
        body = {
            "branch": "ticket/T-110",
            "factory_sha": self.args.factory_sha,
            "head_sha": head,
            "migration_history": [{
                "from_factory_sha": self.args.factory_sha,
                "from_head_sha": prior_head,
                "from_passport_file_sha256": "e" * 64,
                "from_passport_sha256": "f" * 64,
                "from_protected_base_sha": protected_base,
                "from_route_plan_sha256": route,
                "rewrite_authorization_sha256": "9" * 64,
                "schema": STATE.PASSPORT_MIGRATION_SCHEMA,
                "to_factory_sha": self.args.factory_sha,
                "to_head_sha": head,
                "to_protected_base_sha": protected_base,
                "to_route_plan_sha256": route,
            }],
            "protected_base_sha": protected_base,
            "route_plan_sha256": route,
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
        prior = STATE.signed_repair({
            **{
                key: value for key, value in record.items()
                if key not in {"authentication_sha256", "repair_sha256"}
            },
            "blocked_receipt": prior_receipt,
            "head_sha": prior_head,
            "head_tree": prior_tree,
            "repair_role": "test-author",
        }, secret)
        prior_path = completed / f"T-110-{prior['repair_sha256']}.json"
        STATE.write_atomic(prior_path, prior)

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

        original = ticket.read_text(encoding="utf-8")
        prior_pair = (
            "OPERATOR RESUME: test-author\n"
            f"OPERATOR RESUME RECEIPT: {prior_receipt}\n"
        )
        current_pair = (
            "OPERATOR RESUME: planner\n"
            f"OPERATOR RESUME RECEIPT: {self.args.receipt}\n"
        )
        invalid = {
            "malformed": original + "OPERATOR RESUME: admin\n",
            "reordered": original.replace(
                prior_pair + current_pair, current_pair + prior_pair,
            ),
        }
        for label, text in invalid.items():
            with self.subTest(label=label):
                ticket.write_text(text, encoding="utf-8")
                with self.assertRaisesRegex(
                    STATE.StateError,
                    "operator resume lacks authenticated contract repair state",
                ):
                    STATE.contract_repair_stage(self.args)

        ticket.write_text(original, encoding="utf-8")
        duplicate = STATE.signed_repair({
            **{
                key: value for key, value in record.items()
                if key not in {"authentication_sha256", "repair_sha256"}
            },
            "passport_sha256": "1" * 64,
        }, secret)
        duplicate_path = completed / f"T-110-{duplicate['repair_sha256']}.json"
        STATE.write_atomic(duplicate_path, duplicate)
        with self.assertRaisesRegex(
            STATE.StateError,
            "operator resume lacks authenticated contract repair state",
        ):
            STATE.contract_repair_stage(self.args)
        duplicate_path.unlink()

        prior_path.unlink()
        unrelated_head = run(
            "git", "commit-tree", prior_tree, "-m", "unrelated repair",
            cwd=self.product,
        )
        unrelated = STATE.signed_repair({
            **{
                key: value for key, value in prior.items()
                if key not in {"authentication_sha256", "repair_sha256"}
            },
            "head_sha": unrelated_head,
        }, secret)
        STATE.write_atomic(
            completed / f"T-110-{unrelated['repair_sha256']}.json",
            unrelated,
        )
        with self.assertRaisesRegex(
            STATE.StateError,
            "operator resume lacks authenticated contract repair state",
        ):
            STATE.contract_repair_stage(self.args)

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

        with self.assertRaises(STATE.ContractResumeError) as raised:
            STATE.operator_resume_role(self.args, passport, "test-author")
        self.assertEqual(raised.exception.reason_code, "resume_receipt_mismatch")

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

    def test_qualification_resume_preserves_prior_transition_directive(self) -> None:
        prior_receipt = "a" * 64
        ticket = self.product / "factory/tickets/T-110.md"
        ticket.write_text(
            "# T-110\n\nState: Blocked-Escalated\nResume-State: Building\n"
            "OPERATOR RESUME: test-author\n"
            f"OPERATOR RESUME RECEIPT: {prior_receipt}\n",
            encoding="utf-8",
        )
        run("git", "add", str(ticket), cwd=self.product)
        run("git", "commit", "-qm", "historical resume", cwd=self.product)
        baseline = run("git", "rev-parse", "HEAD", cwd=self.product)
        transition = {
            "consumed": True,
            "head_sha": baseline,
            "schema": STATE.RECEIPT_SCHEMA,
            "ticket": "T-110",
        }
        transition["receipt_sha256"] = hashlib.sha256(
            STATE.canonical({
                key: value for key, value in transition.items()
                if key != "consumed"
            })
        ).hexdigest()
        self.args.receipt = transition["receipt_sha256"]
        STATE.write_atomic(self.state_dir / "T-110.json", transition)
        ticket.write_text(
            ticket.read_text(encoding="utf-8")
            + "OPERATOR RESUME: planner\n"
            + f"OPERATOR RESUME RECEIPT: {self.args.receipt}\n",
            encoding="utf-8",
        )
        run("git", "add", str(ticket), cwd=self.product)
        run("git", "commit", "-qm", "current resume", cwd=self.product)
        passport = {
            "branch": "ticket/T-110",
            "factory_sha": self.args.factory_sha,
            "head_sha": baseline,
            "schema": STATE.PASSPORT_SCHEMA,
            "ticket": "T-110",
        }
        with mock.patch.dict(os.environ, {
            "FACTORY_KIT_TRUST_SCOPE": "qualification-candidate",
            "FACTORY_QUALIFICATION_MODE": "isolated",
        }):
            self.assertEqual(
                STATE.operator_resume_role(self.args, passport, "test-author"),
                "planner",
            )

            ticket.write_text(
                ticket.read_text(encoding="utf-8").replace(
                    prior_receipt, "c" * 64, 1,
                ),
                encoding="utf-8",
            )
            run("git", "add", str(ticket), cwd=self.product)
            run("git", "commit", "-qm", "rewrite historical resume", cwd=self.product)
            with self.assertRaises(STATE.ContractResumeError) as raised:
                STATE.operator_resume_role(self.args, passport, "test-author")
            self.assertEqual(
                raised.exception.reason_code, "resume_directives_ambiguous",
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

    def test_active_repair_rebinds_after_operator_preflight_fix(self) -> None:
        secret = b"k" * 32
        (self.state_dir / "passport.key").write_bytes(secret)
        os.chmod(self.state_dir / "passport.key", 0o600)
        passports = self.state_dir / "passports"
        passports.mkdir(mode=0o700)
        ticket = self.product / "factory/tickets/T-110.md"
        ticket.write_text(
            ticket.read_text(encoding="utf-8").rstrip()
            + "\n\nProduct-Decisions: not frozen\n"
            + "OPERATOR RESUME: planner\n"
            + f"OPERATOR RESUME RECEIPT: {'b' * 64}\n",
            encoding="utf-8",
        )
        run("git", "add", str(ticket), cwd=self.product)
        run("git", "commit", "-qm", "resume blocked planner", cwd=self.product)
        repair_head = run("git", "rev-parse", "HEAD", cwd=self.product)
        body = {
            "branch": "ticket/T-110",
            "factory_sha": self.args.factory_sha,
            "head_sha": repair_head,
            "migration_history": [],
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
            "blocked_role": "planner",
            "branch": "ticket/T-110",
            "factory_sha": self.args.factory_sha,
            "head_sha": repair_head,
            "head_tree": run(
                "git", "rev-parse", "HEAD^{tree}", cwd=self.product
            ),
            "passport_sha256": passport["passport_sha256"],
            "repair_role": "planner",
            "schema": STATE.REPAIR_SCHEMA,
            "ticket": "T-110",
        }, secret)
        STATE.write_atomic(STATE.repair_path(self.args), record)
        attempts = STATE.contract_repair_attempt(self.args)

        ticket.write_text(
            ticket.read_text(encoding="utf-8").replace(
                "Product-Decisions: not frozen", "Product-Decisions: frozen"
            ),
            encoding="utf-8",
        )
        run("git", "add", str(ticket), cwd=self.product)
        run("git", "commit", "-qm", "apply operator preflight ruling", cwd=self.product)
        fixed_head = run("git", "rev-parse", "HEAD", cwd=self.product)

        def migrate(_args):
            migrated_body = {
                **body,
                "head_sha": fixed_head,
                "migration_history": [{
                    "from_factory_sha": self.args.factory_sha,
                    "from_head_sha": repair_head,
                    "from_passport_sha256": passport["passport_sha256"],
                    "schema": STATE.PASSPORT_MIGRATION_SCHEMA,
                    "to_factory_sha": self.args.factory_sha,
                    "to_head_sha": fixed_head,
                }],
                "parent_digest": passport["passport_sha256"],
            }
            migrated = dict(migrated_body)
            migrated["authentication_sha256"] = hmac.new(
                secret, STATE.canonical(migrated_body), hashlib.sha256
            ).hexdigest()
            migrated["passport_sha256"] = hashlib.sha256(
                STATE.canonical(migrated)
            ).hexdigest()
            STATE.write_atomic(passports / "T-110.json", migrated)

        with mock.patch.object(STATE, "migrate_passport", side_effect=migrate):
            self.assertEqual(
                STATE.contract_repair_stage(self.args), ("FIX planner", True)
            )

        rebound_passport, _ = STATE.authenticated_passport(self.args)
        rebound = STATE.load_repair(self.args, secret)
        self.assertEqual(rebound["head_sha"], fixed_head)
        self.assertEqual(
            rebound["passport_sha256"], rebound_passport["passport_sha256"]
        )
        archived = (
            STATE.repair_path(self.args).parent / "superseded"
            / f"T-110-{record['repair_sha256']}.json"
        )
        self.assertEqual(json.loads(archived.read_text()), record)
        self.assertEqual(STATE.contract_repair_attempt(self.args), attempts)

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

    def test_blocked_repair_survives_release_migration(self) -> None:
        old_factory = "b" * 40
        blocked_receipt = "c" * 64
        old_passport = "d" * 64
        repair_head = run("git", "rev-parse", "HEAD", cwd=self.product)
        ticket = self.product / "factory/tickets/T-110.md"
        ticket.write_text(
            ticket.read_text(encoding="utf-8") + "\nRepair blocked.\n",
            encoding="utf-8",
        )
        run("git", "add", str(ticket), cwd=self.product)
        run("git", "commit", "-qm", "block repair", cwd=self.product)
        blocked_head = run("git", "rev-parse", "HEAD", cwd=self.product)
        ticket.write_text(
            ticket.read_text(encoding="utf-8") + "\nMigrate route.\n",
            encoding="utf-8",
        )
        run("git", "add", str(ticket), cwd=self.product)
        run("git", "commit", "-qm", "migrate route", cwd=self.product)
        ticket.write_text(
            ticket.read_text(encoding="utf-8").replace(
                "State: Planning",
                "State: Blocked-Escalated\nResume-State: Building",
            ),
            encoding="utf-8",
        )
        run("git", "add", str(ticket), cwd=self.product)
        run("git", "commit", "-qm", "materialize blocked repair", cwd=self.product)
        pre_normalized_head = run("git", "rev-parse", "HEAD", cwd=self.product)
        normalization_parent = run(
            "git", "commit-tree", f"{repair_head}^{{tree}}",
            "-m", "normalization base", cwd=self.product,
        )
        current_head = run(
            "git", "commit-tree", f"{pre_normalized_head}^{{tree}}",
            "-p", normalization_parent, "-m", "normalize repair history",
            cwd=self.product,
        )
        run("git", "reset", "--hard", current_head, cwd=self.product)

        receipt_body = {
            "branch": "ticket/T-110",
            "contract_version": self.args.contract_version,
            "factory_sha": old_factory,
            "head_sha": repair_head,
            "head_tree": run(
                "git", "rev-parse", f"{repair_head}^{{tree}}", cwd=self.product
            ),
            "parent_digest": blocked_receipt,
            "passport_sha256": "e" * 64,
            "product_origin_sha256": hashlib.sha256(
                b"test-origin"
            ).hexdigest(),
            "project": self.args.project,
            "role": "planner",
            "schema": STATE.RECEIPT_SCHEMA,
            "stage": "FIX planner",
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
        manifest = (
            "run_id=blocked-repair\nphase=completed\n"
            "accounting_state=completed\n"
            f"contract_version={self.args.contract_version}\n"
            "ticket=T-110\nrole=planner\nexit_status=12\n"
            "role_exit=role_exit_contract_blocked\n"
            f"kit_sha={old_factory}\nrole_head_before={repair_head}\n"
            "role_branch_before=ticket/T-110\n"
            f"transition_receipt_sha256={receipt_digest}\n"
            "go_issued=1\ntask_submitted=1\n"
        ).encode()
        (self.product / "factory/runs/blocked-repair.meta").write_bytes(
            manifest
        )
        charge = {
            "accounting_state": "completed",
            "contract_version": self.args.contract_version,
            "factory_sha": old_factory,
            "head_before": repair_head,
            "manifest_sha256": hashlib.sha256(manifest).hexdigest(),
            "role": "planner",
            "run_id": "blocked-repair",
            "transition_receipt_sha256": receipt_digest,
        }
        passport = {
            "branch": "ticket/T-110",
            "charge_records": [
                {
                    "role": "builder",
                    "transition_receipt_sha256": blocked_receipt,
                },
                charge,
            ],
            "completed_role_evidence": [],
            "contract_version": self.args.contract_version,
            "current_stage": "FIX planner",
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
            "migration_history": [
                {
                    "from_factory_sha": old_factory,
                    "from_head_sha": blocked_head,
                    "from_passport_file_sha256": "1" * 64,
                    "from_passport_sha256": "2" * 64,
                    "from_protected_base_sha": "3" * 40,
                    "from_route_plan_sha256": "4" * 64,
                    "schema": STATE.PASSPORT_MIGRATION_SCHEMA,
                    "to_factory_sha": old_factory,
                    "to_head_sha": pre_normalized_head,
                    "to_protected_base_sha": "3" * 40,
                    "to_route_plan_sha256": "4" * 64,
                },
                {
                    "from_factory_sha": old_factory,
                    "from_head_sha": pre_normalized_head,
                    "from_passport_file_sha256": "7" * 64,
                    "from_passport_sha256": "8" * 64,
                    "from_protected_base_sha": "3" * 40,
                    "from_route_plan_sha256": "4" * 64,
                    "rewrite_authorization_sha256": "9" * 64,
                    "schema": STATE.PASSPORT_MIGRATION_SCHEMA,
                    "to_factory_sha": old_factory,
                    "to_head_sha": current_head,
                    "to_protected_base_sha": "3" * 40,
                    "to_route_plan_sha256": "4" * 64,
                },
                {
                    "from_factory_sha": old_factory,
                    "from_head_sha": current_head,
                    "from_passport_file_sha256": "a" * 64,
                    "from_passport_sha256": "b" * 64,
                    "from_protected_base_sha": "3" * 40,
                    "from_route_plan_sha256": "4" * 64,
                    "schema": STATE.PASSPORT_MIGRATION_SCHEMA,
                    "to_factory_sha": self.args.factory_sha,
                    "to_head_sha": current_head,
                    "to_protected_base_sha": "5" * 40,
                    "to_route_plan_sha256": "6" * 64,
                },
            ],
            "product_origin_sha256": hashlib.sha256(
                b"test-origin"
            ).hexdigest(),
            "project": self.args.project,
            "protected_base_sha": "5" * 40,
            "route_plan_sha256": "6" * 64,
            "ticket": "T-110",
            "transition_receipt_sha256": receipt_digest,
        }
        record = {
            "blocked_receipt": blocked_receipt,
            "blocked_role": "builder",
            "factory_sha": old_factory,
            "head_sha": repair_head,
            "passport_sha256": old_passport,
            "repair_role": "planner",
            "schema": STATE.REPAIR_SCHEMA,
        }
        with mock.patch.object(
            STATE, "authenticated_passport", return_value=(passport, b"k" * 32)
        ):
            self.assertTrue(
                STATE.migrated_contract_repair(self.args, passport, record)
            )
            self.assertFalse(STATE.migrated_contract_repair(
                self.args,
                {**passport, "current_stage": "RUN builder"},
                record,
            ))

        secret = b"k" * 32
        record.update({
            "branch": "ticket/T-110",
            "head_tree": run(
                "git", "rev-parse", f"{repair_head}^{{tree}}", cwd=self.product
            ),
            "ticket": "T-110",
        })
        STATE.write_atomic(
            STATE.repair_path(self.args), STATE.signed_repair(record, secret)
        )
        self.args.receipt = receipt_digest
        ticket.write_text(
            ticket.read_text(encoding="utf-8").rstrip()
            + "\n\n"
            "OPERATOR RESUME: planner\n"
            f"OPERATOR RESUME RECEIPT: {receipt_digest}\n",
            encoding="utf-8",
        )
        run("git", "add", str(ticket), cwd=self.product)
        run("git", "commit", "-qm", "authorize migrated repeated repair", cwd=self.product)
        directive_head = run("git", "rev-parse", "HEAD", cwd=self.product)
        resumed_passport = {
            **passport,
            "head_sha": directive_head,
            "migration_history": [
                *passport["migration_history"],
                {
                    "from_factory_sha": self.args.factory_sha,
                    "from_head_sha": current_head,
                    "from_passport_file_sha256": "c" * 64,
                    "from_passport_sha256": "d" * 64,
                    "from_protected_base_sha": "5" * 40,
                    "from_route_plan_sha256": "6" * 64,
                    "schema": STATE.PASSPORT_MIGRATION_SCHEMA,
                    "to_factory_sha": self.args.factory_sha,
                    "to_head_sha": directive_head,
                    "to_protected_base_sha": "5" * 40,
                    "to_route_plan_sha256": "6" * 64,
                },
            ],
        }

        with (
            mock.patch.object(
                STATE, "authenticated_passport",
                return_value=(resumed_passport, secret),
            ),
            mock.patch.object(
                STATE, "contract_blocked_receipt", return_value="planner"
            ),
            mock.patch.object(STATE, "migrate_passport") as migrate,
        ):
            self.assertEqual(
                STATE.contract_repair_stage(self.args), ("FIX planner", True)
            )
            result = STATE.block_transition(self.args)
        self.assertEqual(result["status"], "blocked")
        migrate.assert_called_once_with(self.args)

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
