#!/usr/bin/env python3
"""Focused checks for the pre-submission process-group gate."""

import importlib.util
import os
import subprocess
import sys
import tempfile
import time
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

    def test_submission_marker_ignores_a_stale_pid_named_temporary(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            submitted = root / ".run.submitted"
            stale = submitted.with_name(
                f"{submitted.name}.{os.getpid()}.tmp"
            )
            stale.write_text("stale\n", encoding="utf-8")

            MODULE.persist_submission(submitted, 12345)

            self.assertRegex(
                submitted.read_text(encoding="utf-8"),
                r"^pid=12345\nsubmitted_at_epoch_ns=[1-9][0-9]*\n$",
            )
            self.assertEqual(submitted.stat().st_mode & 0o777, 0o600)
            self.assertEqual(stale.read_text(encoding="utf-8"), "stale\n")

    def test_published_submission_does_not_interrupt_the_child(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            ready = root / "ready"
            gate = root / "gate"
            submitted = root / "submitted"
            completed = root / "completed"
            process = subprocess.Popen([
                sys.executable,
                str(ROOT / "scripts/lib/run-in-process-group.py"),
                str(ready), str(gate), str(submitted),
                str(root / "kill"), str(root / "maintenance"),
                str(root / "cancel"), sys.executable, "-c",
                "import pathlib,sys,time; time.sleep(.05); "
                "pathlib.Path(sys.argv[1]).write_text('done\\n')",
                str(completed),
            ])
            for _ in range(500):
                if ready.is_file():
                    break
                if process.poll() is not None:
                    break
                time.sleep(0.01)
            self.assertTrue(ready.is_file())
            gate.touch()
            self.assertEqual(process.wait(timeout=10), 0)
            self.assertRegex(
                submitted.read_text(encoding="utf-8"),
                r"^pid=\d+\nsubmitted_at_epoch_ns=[1-9][0-9]*\n$",
            )
            self.assertEqual(completed.read_text(encoding="utf-8"), "done\n")


if __name__ == "__main__":
    unittest.main()
