#!/usr/bin/env python3
"""Fast exhaustive tests for the pure ticket-state transition policy."""

from __future__ import annotations

import itertools
import os
from pathlib import Path
import sys
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/lib"))

from ticket_state_transition import (  # noqa: E402
    ALLOWED_TRANSITIONS,
    TransitionError,
    apply_factory_transition,
    exact_state,
    field,
    fresh_protocol_text,
    fresh_resume_text,
    parse_state,
    planner_spec_linter_authorization,
    qualification_epoch_text,
    validate_action_transition,
    validate_materialization,
)


LIFECYCLE = (
    "backlog",
    "ready",
    "planning",
    "building",
    "review",
    "awaiting approval",
    "approved",
    "blocked-escalated",
    "done",
    "canceled",
)
CANONICAL = {
    **{state: state.title() for state in LIFECYCLE},
    "awaiting approval": "Awaiting Approval",
    "blocked-escalated": "Blocked-Escalated",
}
EXPECTED = {
    "materialize": {
        ("backlog", "ready"),
        ("backlog", "canceled"),
        *(("blocked-escalated", target) for target in (
            "backlog", "ready", "planning", "building", "review",
        )),
    },
    "transition": {
        ("ready", "planning"),
        ("planning", "building"),
        ("building", "review"),
        ("review", "building"),
        *((source, "blocked-escalated") for source in (
            "ready", "planning", "building", "review",
            "awaiting approval", "approved",
        )),
    },
    "reviewer-reconcile": {("review", "building")},
    "qualification-backlog": {
        ("planning", "backlog"),
        ("building", "backlog"),
    },
}
RESUME_STATE_CONTRACTS = ("1.7.0", "1.8.0", "2.0.0")


class TicketTransitionPolicyTest(unittest.TestCase):
    def test_spec_linter_grants_are_ordered_and_one_use(self) -> None:
        fail = "SPEC-LINT: FAIL — reason\n"
        passed = "SPEC-LINT: PASS\n"
        grant3 = "OPERATOR AUTHORIZATION: spec-linter round 3\n"
        grant4 = "OPERATOR AUTHORIZATION: spec-linter round 4\n"
        grant5 = "OPERATOR AUTHORIZATION: spec-linter round 5\n"
        prefix = fail + passed + fail
        cases = {
            prefix: (3, "required"),
            prefix + grant3: (3, "authorized"),
            prefix + grant3 + fail: (4, "required"),
            prefix + grant3 + fail + grant4: (4, "authorized"),
            prefix + grant3 + fail + grant4 + passed: (5, "required"),
            prefix + grant3 + fail + grant4 + passed + grant5:
                (5, "authorized"),
            prefix + grant3 + fail + passed: (4, "invalid"),
            prefix + grant4: (3, "required"),
            prefix + grant3 * 2: (3, "invalid"),
        }
        for text, expected in cases.items():
            with self.subTest(expected=expected):
                self.assertEqual(planner_spec_linter_authorization(text), expected)
        self.assertIsNone(planner_spec_linter_authorization(fail + passed))

    def test_blocked_transition_projects_only_fresh_resume_controls(self) -> None:
        prior = "a" * 64
        current = "b" * 64
        baseline = (
            "State: Building\n"
            "OPERATOR RESUME: planner\n"
            f"OPERATOR RESUME RECEIPT: {prior}\n"
            "Audit: OPERATOR RESUME: builder is prose\n"
        )
        after = baseline + (
            "OPERATOR RESUME: test-author\n"
            f"OPERATOR RESUME RECEIPT: {current}\n"
        )
        self.assertEqual(
            fresh_resume_text(after, baseline),
            "State: Building\nAudit: OPERATOR RESUME: builder is prose\n"
            "OPERATOR RESUME: test-author\n"
            f"OPERATOR RESUME RECEIPT: {current}\n",
        )
        for changed in (
            after.replace("OPERATOR RESUME: planner\n", "", 1),
            after.replace(prior, "c" * 64, 1),
        ):
            with self.assertRaisesRegex(
                TransitionError, "blocked transition resume history changed",
            ):
                fresh_resume_text(changed, baseline)

    def test_qualification_epoch_projects_only_fresh_protocol_controls(self) -> None:
        baseline = (
            "State: Backlog\n"
            "SPEC-LINT: FAIL — old reason\n"
            "reviewer round 1: REQUEST CHANGES — old review\n"
            "reviewer round 1 FIX-OWNER: builder\n"
            "reviewer round 2 FIX-OWNER: test-author\n"
            "reviewer round 3 FIX-OWNER: both\n"
            "OPERATOR NOTE: reviewer run 1 void — duplicate\n"
            "OPERATOR AUTHORIZATION: planner round 2\n"
            "OPERATOR AUTHORIZATION: spec-linter round 3\n"
            "OPERATOR AUTHORIZATION: test-author round 2\n"
            "OPERATOR AUTHORIZATION: builder round 2\n"
            "OPERATOR AUTHORIZATION: reviewer round 2\n"
            "OPERATOR AUTHORIZATION: narrator round 2\n"
            "Audit: SPEC-LINT: PASS is prose\n"
        )
        current = baseline + (
            "SPEC-LINT: PASS — current scope is coherent\n"
            "reviewer round 2: APPROVE\n"
            "OPERATOR AUTHORIZATION: spec-linter round 3\n"
        )
        self.assertEqual(
            fresh_protocol_text(current, baseline),
            "State: Backlog\nAudit: SPEC-LINT: PASS is prose\n"
            "SPEC-LINT: PASS — current scope is coherent\n"
            "reviewer round 2: APPROVE\n"
            "OPERATOR AUTHORIZATION: spec-linter round 3\n",
        )
        self.assertEqual(baseline.splitlines()[0], "State: Backlog")

        protected = baseline.splitlines(keepends=True)
        for name, changed in {
            "deleted": "".join(protected[:1] + protected[2:]),
            "rewritten": baseline.replace("old reason", "new reason"),
            "reordered": "".join([protected[0], protected[2], protected[1], *protected[3:]]),
            "interposed": baseline.replace(
                "reviewer round 1: REQUEST CHANGES — old review\n",
                "SPEC-LINT: PASS\nreviewer round 1: REQUEST CHANGES — old review\n",
            ),
        }.items():
            with self.subTest(name=name), self.assertRaisesRegex(
                TransitionError, "protected qualification role-control history changed",
            ):
                fresh_protocol_text(changed, baseline)

        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                qualification_epoch_text(
                    Path("/path-that-does-not-exist"), "T-1", current,
                ),
                current,
            )

        with mock.patch.dict(os.environ, {
            "FACTORY_KIT_TRUST_SCOPE": "qualification-candidate",
            "FACTORY_QUALIFICATION_MODE": "takeover",
            "FACTORY_QUALIFICATION_PRODUCT_SHA": "invalid",
        }), self.assertRaisesRegex(
            TransitionError, "qualification role-control baseline is invalid",
        ):
            qualification_epoch_text(Path("/unavailable"), "T-1", current)

        with mock.patch.dict(os.environ, {
            "FACTORY_KIT_TRUST_SCOPE": "qualification-candidate",
            "FACTORY_QUALIFICATION_MODE": "takeover",
            "FACTORY_QUALIFICATION_PRODUCT_SHA": "a" * 40,
        }), mock.patch(
            "legacy_closeout._git_object", return_value=None,
        ), self.assertRaisesRegex(
            TransitionError, "qualification role-control baseline is unavailable",
        ):
            qualification_epoch_text(Path("/unavailable"), "T-1", current)

    def test_successor_uses_original_receipt_epoch_from_canonical_root(self) -> None:
        original = "b" * 40
        current_sha = "c" * 40
        baseline = "State: Backlog\n"
        current = baseline + "SPEC-LINT: PASS\n"
        release = Path("/private/tmp/releases") / ("a" * 40)
        canonical = Path("/private/tmp/product")
        parked = Path("/private/tmp/parked/T-1")
        with mock.patch.dict(os.environ, {
            "FACTORY_KIT_TRUST_SCOPE": "qualification-candidate",
            "FACTORY_QUALIFICATION_MODE": "isolated",
            "FACTORY_QUALIFICATION_RECEIPT_ID": "f" * 64,
            "FACTORY_QUALIFICATION_PRODUCT_SHA": current_sha,
            "FACTORY_QUALIFICATION_PRODUCT_TREE": "e" * 40,
            "FACTORY_RELEASE_PATH": str(release),
            "FACTORY_PROJECT": "relay",
            "FACTORY_ROOT": str(canonical),
        }, clear=True), mock.patch(
            "qualification_release.role_control_epoch",
            return_value=(original, "d" * 40),
        ) as epoch, mock.patch(
            "legacy_closeout._git_object",
            return_value=("e" * 40, "blob", baseline.encode()),
        ) as git_object:
            self.assertEqual(
                qualification_epoch_text(parked, "T-1", current), current,
            )
        epoch.assert_called_once_with(
            release, "relay", canonical, "f" * 64, current_sha, "e" * 40,
        )
        git_object.assert_called_once_with(
            parked, f"{original}:factory/tickets/T-1.md",
        )

    def test_takeover_keeps_current_product_epoch(self) -> None:
        current_sha = "c" * 40
        baseline = "State: Building\nSPEC-LINT: PASS\n"
        with mock.patch.dict(os.environ, {
            "FACTORY_KIT_TRUST_SCOPE": "qualification-candidate",
            "FACTORY_QUALIFICATION_MODE": "takeover",
            "FACTORY_QUALIFICATION_PRODUCT_SHA": current_sha,
            "FACTORY_RELEASE_PATH": "/private/tmp/releases/" + "a" * 40,
            "FACTORY_PROJECT": "relay",
            "FACTORY_ROOT": "/private/tmp/product",
        }, clear=True), mock.patch(
            "qualification_release.role_control_epoch",
            side_effect=AssertionError("isolated helper must not run"),
        ), mock.patch(
            "legacy_closeout._git_object",
            return_value=("e" * 40, "blob", baseline.encode()),
        ):
            self.assertEqual(
                qualification_epoch_text(
                    Path("/private/tmp/takeover"), "T-1", baseline,
                ),
                "State: Building\n",
            )

    def test_allowed_edges_are_the_complete_declared_policy(self) -> None:
        self.assertEqual(ALLOWED_TRANSITIONS, EXPECTED)

    def test_state_parser_requires_one_known_lifecycle_state(self) -> None:
        for text in (
            "# T-1\n",
            "State: Ready\nState: Building\n",
            "State: Unknown\n",
        ):
            with self.subTest(text=text), self.assertRaises(TransitionError):
                parse_state(text)
        self.assertEqual(parse_state("State: Ready\n"), "ready")

    def test_every_action_accepts_only_noops_and_declared_edges(self) -> None:
        for action, allowed in EXPECTED.items():
            for source, target in itertools.product(LIFECYCLE, repeat=2):
                with self.subTest(action=action, source=source, target=target):
                    if source == target or (source, target) in allowed:
                        validate_action_transition(action, source, target)
                    else:
                        with self.assertRaisesRegex(
                            TransitionError,
                            rf"illegal {action} transition",
                        ):
                            validate_action_transition(action, source, target)

    def test_factory_transition_mutates_every_legal_edge(self) -> None:
        for contract in RESUME_STATE_CONTRACTS:
            for source, target in EXPECTED["transition"]:
                with self.subTest(
                    contract=contract,
                    source=source,
                    target=target,
                ):
                    result = apply_factory_transition(
                        f"State: {CANONICAL[source]}\n",
                        CANONICAL[target],
                        contract,
                    )
                    self.assertEqual(exact_state(result), target)
                    if target == "blocked-escalated":
                        self.assertEqual(
                            field(result, "Resume-State"),
                            CANONICAL[source],
                        )

        historical = apply_factory_transition(
            "State: Ready\n",
            "Blocked-Escalated",
            "1.6.0",
        )
        self.assertEqual(exact_state(historical), "blocked-escalated")
        self.assertEqual(field(historical, "Resume-State"), "")

    def test_factory_transition_rejects_targets_and_ambiguous_resume(self) -> None:
        for text in (
            "# T-1\n",
            "State: Ready\nState: Building\n",
            "State: Unknown\n",
        ):
            with self.subTest(text=text), self.assertRaises(TransitionError):
                apply_factory_transition(text, "Planning", "2.0.0")
        for contract in RESUME_STATE_CONTRACTS:
            for target in ("Backlog", "Canceled"):
                with self.subTest(
                    contract=contract,
                    target=target,
                ), self.assertRaisesRegex(
                    TransitionError,
                    "illegal factory transition target",
                ):
                    apply_factory_transition(
                        "State: Ready\n",
                        target,
                        contract,
                    )
        for contract in RESUME_STATE_CONTRACTS:
            with self.subTest(contract=contract), self.assertRaisesRegex(
                TransitionError,
                "duplicate Resume-State",
            ):
                apply_factory_transition(
                    "State: Ready\nResume-State: Planning\nResume-State: Review\n",
                    "Blocked-Escalated",
                    contract,
                )

    def test_materialization_covers_every_operator_edge_and_guard(self) -> None:
        for current, effective in (
            ("# T-1\n", "State: Ready\n"),
            ("State: Ready\n", "State: Ready\nState: Building\n"),
            ("State: Unknown\n", "State: Ready\n"),
        ):
            with self.subTest(current=current, effective=effective), self.assertRaises(
                TransitionError,
            ):
                validate_materialization(current, effective)
        for source, target in EXPECTED["materialize"]:
            with self.subTest(source=source, target=target):
                current = f"State: {CANONICAL[source]}\n"
                if source == "blocked-escalated":
                    current += f"Resume-State: {CANONICAL[target]}\n"
                validate_materialization(
                    current,
                    f"State: {CANONICAL[target]}\n",
                )

        invalid = (
            ("State: Backlog\n", "State: Planning\n"),
            (
                "State: Blocked-Escalated\nResume-State: Building\n",
                "State: Planning\n",
            ),
            ("State: Review\n", "State: Awaiting Approval\n"),
            (
                "State: Approved\nOperator-Approval: Receipt\n",
                "State: Approved\n",
            ),
            (
                "State: Approved\nOperator-Approval: Linear\n",
                "State: Approved\n",
            ),
        )
        for current, effective in invalid:
            with self.subTest(
                current=current,
                effective=effective,
            ), self.assertRaises(TransitionError):
                validate_materialization(current, effective)


if __name__ == "__main__":
    unittest.main()
