#!/usr/bin/env python3
"""Regressions for one-use Linear model fallback approvals."""

import datetime as dt
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/lib/model-fallback-approval.py"


class ApprovalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "linear-map.json"
        now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
        self.approval_hash = "a" * 64
        self.approval = {
            "approval_hash": self.approval_hash,
            "comment_id": "comment-1",
            "expires_at": (now + dt.timedelta(minutes=10)).isoformat(),
            "failed_run_id": "run-1",
            "linear_created_at": (now - dt.timedelta(seconds=2)).isoformat(),
            "linear_updated_at": (now - dt.timedelta(seconds=1)).isoformat(),
            "nonce": "b" * 32,
            "observed_at": now.isoformat(),
            "operator_id": "operator-1",
            "operator_name": "Operator",
            "reason": "credits_exhausted",
            "schema": "model-fallback-linear-approval/v1",
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

    def test_read_returns_only_bounded_identity(self):
        value = json.loads(self.command("read").stdout)
        self.assertEqual(value["approval_hash"], self.approval_hash)
        self.assertEqual(value["operator_id"], "operator-1")
        self.assertNotIn("operator_name", value)

    def test_consume_is_atomic_and_one_use(self):
        self.command("consume", "--approval-hash", self.approval_hash)
        value = json.loads(self.path.read_text())
        entry = value["tickets"]["T-1"]
        self.assertNotIn("model_fallback_approval", entry)
        self.assertEqual(entry["consumed_model_fallback_comment_ids"], ["comment-1"])
        self.assertNotEqual(self.command("read", check=False).returncode, 0)

    def test_wrong_hash_expired_and_symlink_refuse(self):
        result = self.command("consume", "--approval-hash", "c" * 64, check=False)
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
