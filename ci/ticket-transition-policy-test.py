#!/usr/bin/env python3
"""Fast exhaustive tests for the pure ticket-state transition policy."""

from __future__ import annotations

import itertools
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/lib"))

from ticket_state_transition import (  # noqa: E402
    ALLOWED_TRANSITIONS,
    TransitionError,
    apply_factory_transition,
    exact_state,
    field,
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


class TicketTransitionPolicyTest(unittest.TestCase):
    def test_allowed_edges_are_the_complete_declared_policy(self) -> None:
        self.assertEqual(ALLOWED_TRANSITIONS, EXPECTED)

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
        for source, target in EXPECTED["transition"]:
            with self.subTest(source=source, target=target):
                result = apply_factory_transition(
                    f"State: {CANONICAL[source]}\n",
                    CANONICAL[target],
                    "1.8.0",
                )
                self.assertEqual(exact_state(result), target)
                if target == "blocked-escalated":
                    self.assertEqual(
                        field(result, "Resume-State"),
                        CANONICAL[source],
                    )

    def test_factory_transition_rejects_targets_and_ambiguous_resume(self) -> None:
        for target in ("Backlog", "Canceled"):
            with self.subTest(target=target), self.assertRaisesRegex(
                TransitionError,
                "illegal factory transition target",
            ):
                apply_factory_transition("State: Ready\n", target, "1.8.0")
        with self.assertRaisesRegex(
            TransitionError,
            "duplicate Resume-State",
        ):
            apply_factory_transition(
                "State: Ready\nResume-State: Planning\nResume-State: Review\n",
                "Blocked-Escalated",
                "1.8.0",
            )

    def test_materialization_covers_every_operator_edge_and_guard(self) -> None:
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
