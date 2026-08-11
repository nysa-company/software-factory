#!/usr/bin/env python3
"""Adversarial tests for one-use operator receipts."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts" / "lib"))
import operator_receipt as receipts  # noqa: E402
from operator_receipt import OperatorReceiptError  # noqa: E402


class OperatorReceiptTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.state = Path(self._tmp.name).resolve() / "state"
        self.state.mkdir(mode=0o700)
        self.addCleanup(self._tmp.cleanup)

    def issue_approve(self, blob: str = "a" * 40) -> dict:
        return receipts.issue(
            self.state, "T-1", "approve", {"bundle_attestation_blob": blob},
        )

    def test_issue_consume_round_trip_all_actions(self) -> None:
        payloads = {
            "ready": {},
            "approve": {"bundle_attestation_blob": "a" * 40},
            "resume": {"resume_stage": "BUILD"},
            "cancel": {},
            "priority": {"priority": 2},
            "fallback": {"preview_sha256": "b" * 64},
        }
        for action, payload in payloads.items():
            value = receipts.issue(self.state, "T-1", action, payload)
            self.assertEqual(value["schema"], receipts.SCHEMA)
            self.assertFalse(value["consumed"])
            consumed = receipts.verify_consume(self.state, "T-1", action, payload)
            self.assertEqual(consumed["receipt_sha256"], value["receipt_sha256"])
            self.assertTrue(consumed["consumed"])

    def test_double_consume_refused(self) -> None:
        self.issue_approve()
        receipts.verify_consume(self.state, "T-1", "approve")
        with self.assertRaises(OperatorReceiptError):
            receipts.verify_consume(self.state, "T-1", "approve")

    def test_binding_mismatch_refused(self) -> None:
        self.issue_approve(blob="a" * 40)
        with self.assertRaises(OperatorReceiptError):
            receipts.verify_consume(
                self.state, "T-1", "approve",
                {"bundle_attestation_blob": "f" * 40},
            )
        # The failed attempt must not have consumed the receipt.
        value = receipts.verify_consume(
            self.state, "T-1", "approve", {"bundle_attestation_blob": "a" * 40},
        )
        self.assertTrue(value["consumed"])

    def test_forged_receipt_file_refused(self) -> None:
        value = self.issue_approve()
        path = self.state / "operator-receipts" / "T-1" / "approve-1.json"
        forged = dict(value)
        forged["payload"] = {"bundle_attestation_blob": "f" * 40}
        path.write_text(json.dumps(forged))
        os.chmod(path, 0o600)
        with self.assertRaises(OperatorReceiptError):
            receipts.verify_consume(self.state, "T-1", "approve")

    def test_wrong_ticket_and_action_isolated(self) -> None:
        self.issue_approve()
        with self.assertRaises(OperatorReceiptError):
            receipts.verify_consume(self.state, "T-2", "approve")
        with self.assertRaises(OperatorReceiptError):
            receipts.verify_consume(self.state, "T-1", "cancel")

    def test_replay_after_reissue_consumes_only_newest(self) -> None:
        self.issue_approve(blob="a" * 40)
        second = receipts.issue(
            self.state, "T-1", "approve", {"bundle_attestation_blob": "c" * 40},
        )
        consumed = receipts.verify_consume(self.state, "T-1", "approve")
        self.assertEqual(consumed["receipt_sha256"], second["receipt_sha256"])

    def test_idempotent_issue_returns_open_receipt(self) -> None:
        first = self.issue_approve()
        again = self.issue_approve()
        self.assertEqual(first["receipt_sha256"], again["receipt_sha256"])

    def test_missing_required_binding_refused_at_issue(self) -> None:
        for action, payload in (
            ("approve", {}),
            ("fallback", {}),
            ("resume", {}),
            ("priority", {}),
        ):
            with self.assertRaises(OperatorReceiptError):
                receipts.issue(self.state, "T-1", action, payload)

    def test_unknown_action_and_bad_ticket_refused(self) -> None:
        with self.assertRaises(OperatorReceiptError):
            receipts.issue(self.state, "T-1", "merge", {})
        with self.assertRaises(OperatorReceiptError):
            receipts.issue(self.state, "SF-1", "ready", {})

    def test_symlinked_receipt_refused(self) -> None:
        self.issue_approve()
        ticket_dir = self.state / "operator-receipts" / "T-1"
        real = ticket_dir / "approve-1.json"
        moved = ticket_dir / "elsewhere.json"
        real.rename(moved)
        real.symlink_to(moved)
        with self.assertRaises(OperatorReceiptError):
            receipts.verify_consume(self.state, "T-1", "approve")

    def test_world_readable_receipt_refused(self) -> None:
        self.issue_approve()
        path = self.state / "operator-receipts" / "T-1" / "approve-1.json"
        os.chmod(path, 0o644)
        with self.assertRaises(OperatorReceiptError):
            receipts.verify_consume(self.state, "T-1", "approve")

    def test_unsafe_state_dir_refused(self) -> None:
        os.chmod(self.state, 0o755)
        with self.assertRaises(OperatorReceiptError):
            receipts.issue(self.state, "T-1", "ready", {})
        os.chmod(self.state, 0o700)

    def test_relative_state_dir_refused(self) -> None:
        with self.assertRaises(OperatorReceiptError):
            receipts.issue(Path("relative"), "T-1", "ready", {})

    def test_digest_covers_payload(self) -> None:
        value = self.issue_approve()
        immutable = {
            key: item for key, item in value.items()
            if key not in {"consumed", "consumed_at_epoch", "receipt_sha256"}
        }
        digest = hashlib.sha256(receipts.canonical(immutable)).hexdigest()
        self.assertEqual(value["receipt_sha256"], digest)

    def test_pending_lists_only_unconsumed(self) -> None:
        self.issue_approve()
        receipts.issue(self.state, "T-2", "cancel", {})
        receipts.verify_consume(self.state, "T-1", "approve")
        open_values = receipts.pending(self.state)
        self.assertEqual(
            [(v["ticket"], v["action"]) for v in open_values],
            [("T-2", "cancel")],
        )

    def test_peek_does_not_consume(self) -> None:
        self.issue_approve()
        value = receipts.peek(self.state, "T-1", "approve")
        self.assertIsNotNone(value)
        self.assertFalse(value["consumed"])
        self.assertIsNone(
            receipts.peek(
                self.state, "T-1", "approve",
                {"bundle_attestation_blob": "f" * 40},
            )
        )
        receipts.verify_consume(self.state, "T-1", "approve")
        self.assertIsNone(receipts.peek(self.state, "T-1", "approve"))

    def test_receipt_files_are_0600(self) -> None:
        self.issue_approve()
        path = self.state / "operator-receipts" / "T-1" / "approve-1.json"
        self.assertEqual(stat.S_IMODE(path.lstat().st_mode), 0o600)

    def test_cli_round_trip(self) -> None:
        script = ROOT / "scripts" / "lib" / "operator_receipt.py"
        issue = subprocess.run(
            [
                sys.executable, str(script), "--state-dir", str(self.state),
                "issue", "--ticket", "T-9", "--action", "priority",
                "--payload", json.dumps({"priority": 1}),
            ],
            capture_output=True, text=True,
        )
        self.assertEqual(issue.returncode, 0, issue.stderr)
        consume = subprocess.run(
            [
                sys.executable, str(script), "--state-dir", str(self.state),
                "consume", "--ticket", "T-9", "--action", "priority",
                "--payload", json.dumps({"priority": 1}),
            ],
            capture_output=True, text=True,
        )
        self.assertEqual(consume.returncode, 0, consume.stderr)
        replay = subprocess.run(
            [
                sys.executable, str(script), "--state-dir", str(self.state),
                "consume", "--ticket", "T-9", "--action", "priority",
            ],
            capture_output=True, text=True,
        )
        self.assertEqual(replay.returncode, 1)
        self.assertIn("REFUSE", replay.stderr)


if __name__ == "__main__":
    unittest.main()
