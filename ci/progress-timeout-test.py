#!/usr/bin/env python3
"""Focused authenticated inactivity and absolute timeout tests."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
WATCH = ROOT / "scripts/lib/progress-timeout.py"


def record(path: Path, sequence: int) -> None:
    value = {
        "event_sha256": hashlib.sha256(str(sequence).encode()).hexdigest(),
        "observed_monotonic_ns": time.monotonic_ns(),
        "sequence": sequence,
        "subtype": "completed",
        "type": "tool_call",
    }
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


class ProgressTimeoutTest(unittest.TestCase):
    def start(self, root: Path, soft: int, hard: int):
        journal = root / "progress.jsonl"
        journal.touch(mode=0o600)
        marker = root / "timeout.json"
        child = subprocess.Popen(["sleep", "10"])
        self.addCleanup(
            lambda: child.poll() is not None
            or (child.terminate(), child.wait(timeout=2))
        )
        watch = subprocess.Popen([
            sys.executable, str(WATCH),
            "--pid", str(child.pid), "--journal", str(journal),
            "--marker", str(marker), "--soft-seconds", str(soft),
            "--hard-seconds", str(hard), "--poll-seconds", "0.05",
        ])
        self.addCleanup(
            lambda: watch.poll() is not None
            or (watch.terminate(), watch.wait(timeout=2))
        )
        return child, watch, journal, marker

    def test_silent_process_stops_at_soft_limit(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            child, watch, _journal, marker = self.start(Path(raw), 1, 3)
            self.assertEqual(watch.wait(timeout=3), 0)
            child.wait(timeout=2)
            self.assertEqual(json.loads(marker.read_text())["reason"], "soft_timeout")

    def test_valid_progress_extends_soft_limit(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            child, watch, journal, marker = self.start(root, 1, 4)
            for sequence in range(1, 5):
                time.sleep(0.4)
                record(journal, sequence)
            child.terminate()
            child.wait(timeout=2)
            self.assertEqual(watch.wait(timeout=2), 0)
            self.assertFalse(
                marker.exists(),
                marker.read_text(encoding="utf-8") if marker.exists() else "",
            )

    def test_progress_never_extends_absolute_limit(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            child, watch, journal, marker = self.start(Path(raw), 1, 2)
            stop = threading.Event()

            def heartbeat() -> None:
                sequence = 0
                while not stop.wait(0.25):
                    sequence += 1
                    record(journal, sequence)

            writer = threading.Thread(target=heartbeat)
            writer.start()
            self.assertEqual(watch.wait(timeout=4), 0)
            stop.set()
            writer.join()
            child.wait(timeout=2)
            self.assertEqual(json.loads(marker.read_text())["reason"], "hard_timeout")

    def test_forged_progress_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            child, watch, journal, marker = self.start(Path(raw), 2, 4)
            journal.write_text('{"sequence":1,"event_sha256":"forged"}\n')
            self.assertEqual(watch.wait(timeout=2), 0)
            child.wait(timeout=2)
            self.assertEqual(
                json.loads(marker.read_text())["reason"], "invalid_progress"
            )


if __name__ == "__main__":
    unittest.main()
