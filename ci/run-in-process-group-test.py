#!/usr/bin/env python3
"""Focused checks for the pre-submission process-group gate."""

import importlib.util
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "run_in_process_group",
    ROOT / "scripts/lib/run-in-process-group.py",
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class Gate:
    def __init__(self, opens=False):
        self.opens = opens
        self.calls = 0

    def exists(self):
        self.calls += 1
        return self.opens and self.calls > 1


class ReadinessGateTests(unittest.TestCase):
    def test_slow_validated_controller_may_acknowledge_after_ten_seconds(self):
        with mock.patch.object(
            MODULE.time, "monotonic", side_effect=(0, 11, 12)
        ):
            with mock.patch.object(MODULE.time, "sleep"):
                MODULE.wait_for_gate(Gate(opens=True))

    def test_missing_controller_acknowledgement_still_times_out(self):
        with mock.patch.object(
            MODULE.time, "monotonic", side_effect=(0, 121)
        ):
            with mock.patch.object(MODULE.time, "sleep"):
                with self.assertRaisesRegex(
                    SystemExit,
                    "wrapper did not acknowledge process-group readiness",
                ):
                    MODULE.wait_for_gate(Gate())

    def test_gate_after_deadline_is_not_accepted(self):
        late_gate = Gate(opens=True)
        with mock.patch.object(
            MODULE.time, "monotonic", side_effect=(0, 121)
        ):
            with mock.patch.object(MODULE.time, "sleep"):
                with self.assertRaisesRegex(
                    SystemExit,
                    "wrapper did not acknowledge process-group readiness",
                ):
                    MODULE.wait_for_gate(late_gate)
        self.assertEqual(late_gate.calls, 0)


if __name__ == "__main__":
    unittest.main()
