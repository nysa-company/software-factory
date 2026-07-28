#!/usr/bin/env python3
"""Focused deterministic publication ordering tests."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts/publication-lease.py"


class PublicationLeaseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.state = Path(self.temporary.name).resolve() / "controller"
        self.state.mkdir(mode=0o700)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def call(self, action: str, ticket: str, head: str = "", priority: str = "none",
             lease: str = "") -> dict:
        command = [
            "python3", str(HELPER), action, "--state-dir", str(self.state),
            "--ticket", ticket,
        ]
        if action in {"ready", "acquire"}:
            command.extend(["--head", head, "--priority", priority])
        elif action == "release":
            command.extend(["--lease", lease])
        result = subprocess.run(command, text=True, capture_output=True, check=True)
        return json.loads(result.stdout)

    def test_only_ordered_winner_holds_lease_and_release_unblocks_next(self) -> None:
        self.call("ready", "T-113", "d" * 40, "normal")
        self.call("ready", "T-111", "b" * 40, "normal")
        self.call("ready", "T-112", "c" * 40, "urgent")
        self.assertEqual(
            self.call("acquire", "T-111", "b" * 40)["status"], "queued"
        )
        first = self.call("acquire", "T-112", "c" * 40)
        self.assertEqual(first["status"], "acquired")
        self.assertRegex(first["lease"], r"^[0-9a-f]{64}$")
        renewed = self.call("acquire", "T-112", "c" * 40)
        self.assertEqual(renewed["lease"], first["lease"])
        self.assertGreaterEqual(renewed["expires_epoch"], first["expires_epoch"])
        with self.assertRaises(subprocess.CalledProcessError):
            self.call("withdraw", "T-112")
        self.assertEqual(
            self.call("acquire", "T-111", "b" * 40)["status"], "queued"
        )
        self.call("release", "T-112", lease=first["lease"])
        second = self.call("acquire", "T-111", "b" * 40)
        self.assertEqual(second["status"], "acquired")

    def test_withdrawn_stale_ticket_cannot_block_an_independent_ready_ticket(self) -> None:
        self.call("ready", "T-110", "a" * 40, "normal")
        self.call("ready", "T-111", "b" * 40, "normal")
        self.assertEqual(self.call("withdraw", "T-110")["status"], "withdrawn")
        self.assertEqual(self.call("withdraw", "T-110")["status"], "absent")
        self.assertEqual(
            self.call("acquire", "T-111", "b" * 40)["status"], "acquired"
        )


if __name__ == "__main__":
    unittest.main()
