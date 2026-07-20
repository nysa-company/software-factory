#!/usr/bin/env python3
"""Lifecycle and conservative-failure tests for provider-runtime.py."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "scripts/provider-runtime.py"
COORDINATOR = ROOT / "scripts/provider-coordinator.py"

FAKE_EXECUTOR = r"""#!/usr/bin/env python3
import json
import os
import sys

mode = os.environ.get("FAKE_EXECUTOR_MODE", "success")
action = "cancel" if "cancel" in sys.argv else "execute"
if action == "cancel":
    print(json.dumps({
        "removed": mode == "cancel-ok",
        "schema": "nysa.software-factory.provider-container-cancellation/v1",
    }))
elif mode == "transport-failure":
    print("not-json")
    raise SystemExit(2)
else:
    print(json.dumps({
        "mode": "isolated-v1",
        "return_code": 7 if mode == "provider-failure" else 0,
        "schema": "nysa.software-factory.provider-execution-result/v1",
    }))
"""


class ProviderRuntimeTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.db = self.root / "state-v2.sqlite3"
        self.attempts = self.root / "attempts"
        self.executor = self.root / "fake-executor"
        self.executor.write_text(FAKE_EXECUTOR, encoding="utf-8")
        self.executor.chmod(0o700)
        self.policy = self.root / "policy.json"
        policy = {
            "schema": "factory-provider-concurrency-policy/v1",
            "coupled_max_concurrent": 6,
            "global": {
                "max_concurrent": 6, "max_starts": 20, "window_seconds": 60,
            },
            "provider_families": {
                "mock": {
                    "max_concurrent": 6,
                    "max_starts": 20,
                    "window_seconds": 60,
                },
            },
            "account_routes": {
                "local": {
                    "max_concurrent": 6,
                    "max_starts": 20,
                    "window_seconds": 60,
                },
            },
        }
        canonical = json.dumps(policy, sort_keys=True, separators=(",", ":"))
        self.policy.write_text(canonical + "\n", encoding="utf-8")
        self.policy_hash = hashlib.sha256(canonical.encode()).hexdigest()

    def tearDown(self):
        self.temporary.cleanup()

    def request(self, attempt="attempt-1", policy_hash=None):
        path = self.root / f"{attempt}.request.json"
        path.write_text(json.dumps({
            "attempt_id": attempt,
            "base_sha": "b" * 40,
            "command": ["worker"],
            "image": "worker@sha256:" + "a" * 64,
            "input": str(self.root / "input.json"),
            "policy_sha256": policy_hash or self.policy_hash,
            "role": "builder",
            "route_id": "mock-route",
            "schema": "nysa.software-factory.provider-execution-request/v1",
            "source": str(self.root),
            "ticket": "T-123",
        }, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
        return path

    def command(self, *arguments, mode="success"):
        environment = {**os.environ, "FAKE_EXECUTOR_MODE": mode}
        return subprocess.run(
            [
                sys.executable, str(RUNTIME),
                "--db", str(self.db),
                "--policy", str(self.policy),
                "--coordinator", str(COORDINATOR),
                "--executor", str(self.executor),
                *map(str, arguments),
            ],
            text=True,
            capture_output=True,
            check=False,
            env=environment,
            timeout=30,
        )

    def execute(self, attempt="attempt-1", mode="success", policy_hash=None):
        return self.command(
            "execute",
            "--request", self.request(attempt, policy_hash),
            "--attempt-root", self.attempts,
            "--provider-family", "mock",
            "--account-route", "local",
            "--reserve-micro-usd", "1000",
            mode=mode,
        )

    def status(self, attempt):
        result = subprocess.run(
            [
                sys.executable, str(COORDINATOR), "--db", str(self.db),
                "status", "--attempt-id", attempt,
            ],
            text=True, capture_output=True, check=True,
        )
        return json.loads(result.stdout)["attempts"][0]

    def test_success_and_provider_failure_terminalize(self):
        success = self.execute()
        self.assertEqual(success.returncode, 0, success.stdout + success.stderr)
        self.assertEqual(self.status("attempt-1")["terminal_result"], "succeeded")
        failed = self.execute("attempt-2", mode="provider-failure")
        self.assertEqual(failed.returncode, 0, failed.stdout + failed.stderr)
        self.assertEqual(self.status("attempt-2")["terminal_result"], "failed")

    def test_transport_failure_retains_slot_until_proven_cancellation(self):
        failed = self.execute(mode="transport-failure")
        self.assertEqual(failed.returncode, 2)
        self.assertEqual(self.status("attempt-1")["state"], "submitted")
        cancelled = self.command(
            "cancel",
            "--attempt-id", "attempt-1",
            "--attempt-root", self.attempts,
            mode="cancel-ok",
        )
        self.assertEqual(cancelled.returncode, 0, cancelled.stdout + cancelled.stderr)
        self.assertEqual(self.status("attempt-1")["terminal_result"], "cancelled")

    def test_policy_binding_mismatch_fails_before_reservation(self):
        result = self.execute(policy_hash="f" * 64)
        self.assertEqual(result.returncode, 2)
        self.assertIn("not bound to the active provider policy", result.stdout)
        self.assertFalse(self.db.exists())


if __name__ == "__main__":
    unittest.main()
