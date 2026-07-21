#!/usr/bin/env python3
"""Conservative recovery and rollback tests for Contract 1.6."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
COORDINATOR = ROOT / "scripts" / "provider-coordinator.py"
RECOVERY = ROOT / "scripts" / "provider-recovery.py"


class RecoveryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        os.chmod(self.root, 0o700)
        self.db = self.root / "state.sqlite3"
        self.attempts = self.root / "attempts"
        self.attempts.mkdir(mode=0o700)
        self.broker_db = self.root / "broker.sqlite3"
        self.credentials = self.root / "credentials.json"
        self.credentials.write_text(
            json.dumps(
                {
                    "schema": "nysa.software-factory.provider-credentials/v1",
                    "routes": {
                        "route-a": {
                            "provider_family": "mock",
                            "upstream_origin": "https://provider.invalid",
                            "credential_header": "Authorization",
                            "credential_prefix": "Bearer ",
                            "credential_value": "test-secret",
                            "allowed_paths": ["/v1/messages"],
                            "allowed_models": ["model-a"],
                            "forward_headers": [],
                            "max_request_bytes": 1000,
                        }
                    },
                }
            )
        )
        os.chmod(self.credentials, 0o600)
        self.policy = self.root / "policy.json"
        self.policy.write_text(
            json.dumps(
                {
                    "schema": "factory-provider-concurrency-policy/v1",
                    "coupled_max_concurrent": 4,
                    "global": {
                        "max_concurrent": 4,
                        "max_starts": 20,
                        "window_seconds": 60,
                    },
                    "provider_families": {
                        "mock": {
                            "max_concurrent": 4,
                            "max_starts": 20,
                            "window_seconds": 60,
                        }
                    },
                    "account_routes": {
                        "local": {
                            "max_concurrent": 4,
                            "max_starts": 20,
                            "window_seconds": 60,
                        }
                    },
                }
            )
        )

    def tearDown(self):
        self.temp.cleanup()

    def coordinator(self, *arguments):
        result = subprocess.run(
            [
                sys.executable, str(COORDINATOR),
                "--db", str(self.db), *arguments,
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def reserve_submitted(self):
        self.coordinator(
            "reserve",
            "--operation-id", "reserve-a",
            "--attempt-id", "attempt-a",
            "--provider-family", "mock",
            "--account-route", "local",
            "--reserve-micro-usd", "1000",
            "--product-id", "product-a",
            "--ticket-id", "T-123",
            "--budget-day", "2026-07-20",
            "--product-daily-cap-micro-usd", "100000",
            "--ticket-cap-micro-usd", "100000",
            "--machine-daily-cap-micro-usd", "100000",
            "--policy", str(self.policy),
        )
        self.coordinator(
            "mark-go", "--operation-id", "go-a",
            "--attempt-id", "attempt-a", "--expected-version", "2",
        )
        self.coordinator(
            "mark-submitted", "--operation-id", "submitted-a",
            "--attempt-id", "attempt-a", "--expected-version", "3",
        )

    def recovery(self, action):
        return subprocess.run(
            [
                sys.executable, str(RECOVERY),
                "--db", str(self.db),
                "--broker-db", str(self.broker_db),
                "--broker-credentials", str(self.credentials),
                "--attempt-root", str(self.attempts),
                action,
            ],
            text=True,
            capture_output=True,
            check=False,
        )

    def test_missing_post_go_container_retains_unknown_and_blocks_rollback(self):
        self.reserve_submitted()
        plan = self.recovery("reconcile-plan")
        self.assertEqual(plan.returncode, 0, plan.stdout + plan.stderr)
        value = json.loads(plan.stdout)
        self.assertEqual(value["actions"][0]["attempt_id"], "attempt-a")
        self.assertEqual(
            value["actions"][0]["disposition"],
            "unknown_retain_reservation_and_slot",
        )
        rollback = self.recovery("rollback-check")
        self.assertEqual(rollback.returncode, 0)
        self.assertFalse(json.loads(rollback.stdout)["safe_to_disable"])
        self.coordinator(
            "terminalize", "--operation-id", "terminal-a",
            "--attempt-id", "attempt-a", "--expected-version", "4",
            "--result", "cancelled", "--charge-micro-usd", "1000",
        )
        safe = self.recovery("rollback-check")
        self.assertEqual(safe.returncode, 0, safe.stdout + safe.stderr)
        self.assertTrue(json.loads(safe.stdout)["safe_to_disable"])

    def test_legacy_interval_is_visible_and_blocks_rollback(self):
        self.coordinator(
            "legacy-enter", "--operation-id", "legacy-enter",
            "--interval-id", "legacy-a", "--product-id", "product-a",
        )
        status = self.recovery("status")
        self.assertEqual(status.returncode, 0, status.stdout + status.stderr)
        value = json.loads(status.stdout)
        self.assertEqual(value["legacy_intervals"][0]["interval_id"], "legacy-a")
        rollback = self.recovery("rollback-check")
        self.assertFalse(json.loads(rollback.stdout)["safe_to_disable"])


import hashlib


ROOT = Path(__file__).resolve().parents[1]
RECOVERY = ROOT / "scripts" / "provider-recovery.py"

FAKE_COORDINATOR = r"""#!/usr/bin/env python3
import json, os
mode = os.environ.get("RECOVERY_MODE", "active")
attempts = []
if mode != "empty":
    attempts = [{
        "attempt_id": "attempt-1",
        "reserve_micro_usd": 1000,
        "state": "submitted" if mode != "reserved" else "reserved",
    }]
print(json.dumps({
    "active_reserve_micro_usd": 1000 if attempts else 0,
    "attempts": attempts,
    "counts": {"submitted": len(attempts)},
    "legacy_intervals": [{"interval_id": "legacy-1"}] if mode == "legacy" else [],
    "schema": "factory-provider-coordinator/v1",
}))
"""

FAKE_EXECUTOR = r"""#!/usr/bin/env python3
import json, os
exists = os.environ.get("WORKER_EXISTS", "0") == "1"
print(json.dumps({
    "attempt_id": "attempt-1",
    "container_exists": exists,
    "container_running": exists,
    "schema": "nysa.software-factory.provider-container-status/v1",
}))
"""

FAKE_BROKER = r"""#!/usr/bin/env python3
import json, os
if os.environ.get("BROKER_FAIL") == "1":
    raise SystemExit(1)
print(json.dumps({
    "schema": "nysa.software-factory.provider-credential-broker/v1",
    "status": "ok",
    "tokens": [{"attempt_id": "attempt-1", "active": os.environ.get("TOKEN_ACTIVE") == "1"}],
}))
"""


class RecoveryBoundaryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        os.chmod(self.root, 0o700)
        self.coordinator = self.write("coordinator", FAKE_COORDINATOR)
        self.executor = self.write("executor", FAKE_EXECUTOR)
        self.broker = self.write("broker", FAKE_BROKER)
        self.credentials = self.root / "credentials.json"
        self.credentials.write_text("{}")
        os.chmod(self.credentials, 0o600)
        self.attempts = self.root / "attempts"
        self.attempts.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def write(self, name, content):
        path = self.root / name
        path.write_text(content)
        path.chmod(0o700)
        return path

    def command(
        self, action, *, mode="active", worker=False, token=False,
        broker_fail=False, extra=()
    ):
        environment = {
            **os.environ,
            "RECOVERY_MODE": mode,
            "WORKER_EXISTS": "1" if worker else "0",
            "TOKEN_ACTIVE": "1" if token else "0",
            "BROKER_FAIL": "1" if broker_fail else "0",
        }
        return subprocess.run(
            [
                sys.executable, str(RECOVERY),
                "--db", str(self.root / "state.sqlite3"),
                "--broker-db", str(self.root / "broker.sqlite3"),
                "--broker-credentials", str(self.credentials),
                "--attempt-root", str(self.attempts),
                "--coordinator", str(self.coordinator),
                "--executor", str(self.executor),
                "--credential-broker", str(self.broker),
                action,
                *map(str, extra),
            ],
            text=True,
            capture_output=True,
            check=False,
            env=environment,
            timeout=15,
        )

    def test_missing_post_go_worker_retains_reservation(self):
        result = self.command("reconcile-plan")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        action = json.loads(result.stdout)["actions"][0]
        self.assertEqual(
            action["disposition"], "unknown_retain_reservation_and_slot"
        )
        active = self.command("reconcile-plan", worker=True)
        self.assertEqual(
            json.loads(active.stdout)["actions"][0]["disposition"],
            "active_no_action",
        )

    def test_rollback_refuses_attempt_token_worker_and_legacy_state(self):
        for mode, worker, token, blocker in (
            ("active", False, False, "active_or_unknown_attempts"),
            ("active", True, False, "live_or_unknown_workers"),
            ("legacy", False, False, "legacy_intervals"),
            ("empty", False, True, "active_broker_tokens"),
        ):
            result = self.command(
                "rollback-check", mode=mode, worker=worker, token=token
            )
            value = json.loads(result.stdout)
            if blocker:
                self.assertIn(blocker, value["blockers"])
            self.assertFalse(value["safe_to_disable"])

    def test_clean_rollback_preserves_activation_as_evidence(self):
        activation = self.root / "activation.json"
        activation.write_text('{"enabled":true}\n')
        os.chmod(activation, 0o600)
        digest = hashlib.sha256(activation.read_bytes()).hexdigest()
        result = self.command(
            "disable",
            mode="empty",
            extra=("--activation", activation, "--expected-sha256", digest),
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        value = json.loads(result.stdout)
        self.assertFalse(activation.exists())
        self.assertTrue(Path(value["evidence_path"]).is_file())

    def test_unobservable_broker_blocks_rollback_and_health(self):
        status = json.loads(
            self.command("status", mode="empty", broker_fail=True).stdout
        )
        self.assertEqual(status["health"], "error")
        rollback = json.loads(
            self.command("rollback-check", mode="empty", broker_fail=True).stdout
        )
        self.assertIn("broker_unobservable", rollback["blockers"])
        self.assertFalse(rollback["safe_to_disable"])


if __name__ == "__main__":
    unittest.main()
