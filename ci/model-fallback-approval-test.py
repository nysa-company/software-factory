#!/usr/bin/env python3
"""Regressions for one-use operator model fallback approvals."""

import datetime as dt
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/lib/model-fallback-approval.py"
sys.path.insert(0, str(ROOT / "scripts/lib"))
import operator_receipt as receipts  # noqa: E402


class ApprovalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name).resolve()
        self.path = base / "operator-map.json"
        self.state = base / "controller"
        self.state.mkdir(mode=0o700)
        now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
        self.approval_hash = "a" * 64
        receipt = receipts.issue(
            self.state, "T-1", "fallback",
            {"preview_sha256": self.approval_hash},
        )
        self.receipt_sha256 = receipt["receipt_sha256"]
        self.approval = {
            "approval_hash": self.approval_hash,
            "expires_at": (now + dt.timedelta(minutes=10)).isoformat(),
            "failed_run_id": "run-1",
            "nonce": "b" * 32,
            "observed_at": now.isoformat(),
            "operator_id": "operator-1",
            "reason": "credits_exhausted",
            "receipt_sha256": self.receipt_sha256,
            "schema": "model-fallback-receipt-approval/v1",
        }
        self.write({"tickets": {"T-1": {"model_fallback_approval": self.approval}}})

    def tearDown(self):
        self.temp.cleanup()

    def write(self, value):
        self.path.write_text(json.dumps(value))

    def command(self, action, *extra, check=True):
        result = subprocess.run(
            [
                sys.executable, str(SCRIPT), action,
                "--operator-map", str(self.path),
                "--ticket", "T-1",
                "--failed-run", "run-1",
                "--reason", "credits_exhausted",
                *extra,
            ],
            text=True,
            capture_output=True,
        )
        if check and result.returncode:
            self.fail(result.stderr)
        return result

    def consume_args(self):
        return ("--approval-hash", self.approval_hash, "--state-dir", str(self.state))

    def test_read_returns_only_bounded_identity(self):
        value = json.loads(self.command("read").stdout)
        self.assertEqual(value["approval_hash"], self.approval_hash)
        self.assertEqual(value["operator_id"], "operator-1")
        self.assertNotIn("operator_name", value)

    def test_consume_is_atomic_and_one_use(self):
        self.command("consume", *self.consume_args())
        value = json.loads(self.path.read_text())
        entry = value["tickets"]["T-1"]
        self.assertNotIn("model_fallback_approval", entry)
        self.assertEqual(
            entry["consumed_model_fallback_receipt_ids"], [self.receipt_sha256]
        )
        self.assertNotEqual(self.command("read", check=False).returncode, 0)
        consumed = json.loads(self.command(
            "verify-consumed",
            "--approval-hash", self.approval_hash,
            "--receipt-sha256", self.receipt_sha256,
        ).stdout)
        self.assertEqual(consumed["receipt_sha256"], self.receipt_sha256)
        self.assertNotEqual(self.command(
            "verify-consumed",
            "--approval-hash", self.approval_hash,
            "--receipt-sha256", "c" * 64,
            check=False,
        ).returncode, 0)

    def test_consume_requires_state_dir(self):
        result = self.command(
            "consume", "--approval-hash", self.approval_hash, check=False,
        )
        self.assertNotEqual(result.returncode, 0)

    def test_consume_fails_closed_when_authoritative_receipt_is_absent(self):
        empty_state = Path(self.temp.name).resolve() / "empty-controller"
        empty_state.mkdir(mode=0o700)
        result = self.command(
            "consume", "--approval-hash", self.approval_hash,
            "--state-dir", str(empty_state), check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("model-fallback-approval", result.stderr)

    def test_consume_also_consumes_authoritative_state_dir_receipt(self):
        self.command("consume", *self.consume_args())
        self.assertEqual(receipts.pending(self.state), [])

    def test_wrong_hash_expired_and_symlink_refuse(self):
        result = self.command(
            "consume", "--approval-hash", "c" * 64, "--state-dir", str(self.state),
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        expired = dict(self.approval)
        expired["expires_at"] = "2020-01-01T00:00:00+00:00"
        self.write({"tickets": {"T-1": {"model_fallback_approval": expired}}})
        self.assertNotEqual(self.command("read", check=False).returncode, 0)
        target = self.path.with_name("target.json")
        self.path.replace(target)
        self.path.symlink_to(target)
        self.assertNotEqual(self.command("read", check=False).returncode, 0)


if __name__ == "__main__":
    unittest.main()
