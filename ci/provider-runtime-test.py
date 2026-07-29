#!/usr/bin/env python3
"""Lifecycle and conservative-failure tests for provider-runtime.py."""

from __future__ import annotations

import hashlib
import http.server
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
RUNTIME = ROOT / "scripts/provider-runtime.py"
COORDINATOR = ROOT / "scripts/provider-coordinator.py"
BROKER = ROOT / "scripts/provider-credential-broker.py"

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
        "schema": "nysa.software-factory.provider-execution-result/v2",
    }))
"""

FAKE_CONTROLLER = r"""#!/usr/bin/env python3
import json
print(json.dumps({
    "attempt_id": "attempt-1",
    "charge_micro_usd": 500,
    "schema": "nysa.software-factory.provider-artifact-controller/v1",
    "status": "applied",
}))
"""


class Upstream(http.server.ThreadingHTTPServer):
    allow_reuse_address = False

    def __init__(self, address, *, blocked=False):
        super().__init__(address, UpstreamHandler)
        self.observed_key = None
        self.blocked = blocked
        self.responses = {}
        self.releases = {}
        self.started = set()
        self.condition = threading.Condition()

    def wait_for(self, count):
        deadline = time.monotonic() + 5
        with self.condition:
            while len(self.started) < count:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self.condition.wait(remaining)
        return True

    def release(self, attempt):
        with self.condition:
            self.releases.setdefault(attempt, threading.Event()).set()


class UpstreamHandler(http.server.BaseHTTPRequestHandler):
    server: Upstream

    def log_message(self, format, *args):
        return

    def do_POST(self):
        length = int(self.headers["Content-Length"])
        payload = json.loads(self.rfile.read(length))
        attempt = payload.get("attempt", "default")
        self.server.observed_key = self.headers.get("X-Api-Key")
        with self.server.condition:
            self.server.started.add(attempt)
            release = self.server.releases.setdefault(attempt, threading.Event())
            self.server.condition.notify_all()
        if self.server.blocked:
            release.wait(10)
        mutation = json.dumps(
            {
                "files": ["app.txt"],
                "patch": (
                    "diff --git a/app.txt b/app.txt\n"
                    "index 1111111..2222222 100644\n"
                    "--- a/app.txt\n+++ b/app.txt\n"
                    "@@ -1 +1 @@\n-before\n+after\n"
                ),
            },
            separators=(",", ":"),
        )
        value = self.server.responses.get(attempt, {
            "choices": [{"message": {"content": mutation}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20},
        })
        body = value if isinstance(value, bytes) else json.dumps(value).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class ProviderRuntimeTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.db = self.root / "state-v2.sqlite3"
        self.attempts = self.root / "attempts"
        self.executor = self.root / "fake-executor"
        self.executor.write_text(FAKE_EXECUTOR, encoding="utf-8")
        self.executor.chmod(0o700)
        self.controller = self.root / "fake-controller"
        self.controller.write_text(FAKE_CONTROLLER, encoding="utf-8")
        self.controller.chmod(0o700)
        self.artifact_policy = self.root / "artifact-policy.json"
        self.artifact_policy.write_text("{}")
        self.apply_lock = self.root / "apply.lock"
        self.worker = self.root / "worker"
        self.worker.write_text("#!/bin/sh\nexit 0\n")
        self.processes = []
        self.servers = []
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
        for process in self.processes:
            process.terminate()
            process.communicate(timeout=5)
        for server, thread in self.servers:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        self.temporary.cleanup()

    def request(self, attempt="attempt-1", policy_hash=None, ticket="T-123"):
        path = self.root / f"{attempt}.request.json"
        path.write_text(json.dumps({
            "attempt_id": attempt,
            "base_sha": "b" * 40,
            "command": ["worker"],
            "image": "worker@sha256:" + "a" * 64,
            "input": str(self.root / f"{attempt}.input.json"),
            "policy_sha256": policy_hash or self.policy_hash,
            "role": "builder",
            "route_id": "mock-route",
            "schema": "nysa.software-factory.provider-execution-request/v3",
            "source": str(self.root),
            "ticket": ticket,
            "worker_program": str(self.worker),
            "worker_sha256": hashlib.sha256(self.worker.read_bytes()).hexdigest(),
        }, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
        return path

    def command(self, *arguments, mode="success"):
        environment = {**os.environ, "FAKE_EXECUTOR_MODE": mode}
        return subprocess.run(
            self.runtime_command(*arguments),
            text=True,
            capture_output=True,
            check=False,
            env=environment,
            timeout=30,
        )

    def runtime_command(self, *arguments):
        return [
            sys.executable, str(RUNTIME),
            "--db", str(self.db),
            "--policy", str(self.policy),
            "--coordinator", str(COORDINATOR),
            "--executor", str(self.executor),
            "--artifact-controller", str(self.controller),
            *map(str, arguments),
        ]

    def execute(
        self, attempt="attempt-1", mode="success", policy_hash=None, patch=False
    ):
        arguments = [
            "execute",
            "--request", self.request(attempt, policy_hash),
            "--attempt-root", self.attempts,
            "--provider-family", "mock",
            "--account-route", "local",
            "--reserve-micro-usd", "1000",
            "--product-id", "product-a",
            "--budget-day", "2026-07-20",
            "--product-daily-cap-micro-usd", "1000000",
            "--ticket-cap-micro-usd", "1000000",
            "--machine-daily-cap-micro-usd", "1000000",
        ]
        if patch:
            arguments.extend(
                [
                    "--artifact-mode", "patch-v1",
                    "--worktree", self.root,
                    "--artifact-policy", self.artifact_policy,
                    "--apply-lock", self.apply_lock,
                    "--expected-branch", "ticket/T-123",
                ]
            )
        return self.command(*arguments, mode=mode)

    def status(self, attempt):
        result = subprocess.run(
            [
                sys.executable, str(COORDINATOR), "--db", str(self.db),
                "status", "--attempt-id", attempt,
            ],
            text=True, capture_output=True, check=True,
        )
        return json.loads(result.stdout)["attempts"][0]

    def broker_fixture(self, upstream):
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        self.servers.append((upstream, thread))
        credentials = self.root / "credentials.json"
        secret = "host-only-provider-secret"
        credentials.write_text(json.dumps({
            "schema": "nysa.software-factory.provider-credentials/v1",
            "routes": {"mock-route": {
                "provider_family": "mock",
                "upstream_origin": f"http://127.0.0.1:{upstream.server_port}",
                "credential_header": "X-Api-Key",
                "credential_prefix": "",
                "credential_value": secret,
                "allowed_paths": ["/v1/messages"],
                "allowed_models": ["model-approved"],
                "forward_headers": [],
                "max_request_bytes": 100000,
            }},
        }))
        os.chmod(credentials, 0o600)
        broker_db = self.root / "broker.sqlite3"
        with __import__("socket").socket() as value:
            value.bind(("127.0.0.1", 0))
            broker_port = value.getsockname()[1]
        process = subprocess.Popen(
            [
                sys.executable, str(BROKER),
                "--db", str(broker_db),
                "--credentials", str(credentials),
                "--allow-http-loopback",
                "serve",
                "--listen-port", str(broker_port),
                "--allow-plaintext-loopback",
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.processes.append(process)
        deadline = time.time() + 5
        while time.time() < deadline:
            if process.poll() is not None:
                stdout, stderr = process.communicate()
                self.fail(stdout + stderr)
            try:
                with __import__("socket").create_connection(
                    ("127.0.0.1", broker_port), timeout=0.1
                ):
                    break
            except OSError:
                time.sleep(0.05)
        else:
            self.fail("broker did not listen")
        return broker_db, broker_port, credentials, secret

    def broker_arguments(self, attempt, fixture, *, timeout="900", ticket="T-123"):
        broker_db, broker_port, credentials, _ = fixture
        provider_request = self.root / f"{attempt}.provider-request.json"
        provider_request.write_text(json.dumps({
            "attempt": attempt, "model": "model-approved", "messages": [],
        }))
        os.chmod(provider_request, 0o600)
        return [
            "execute",
            "--request", self.request(attempt, ticket=ticket),
            "--attempt-root", self.attempts,
            "--provider-family", "mock",
            "--account-route", "local",
            "--reserve-micro-usd", "1000",
            "--product-id", "product-a",
            "--budget-day", "2026-07-20",
            "--product-daily-cap-micro-usd", "1000000",
            "--ticket-cap-micro-usd", "1000000",
            "--machine-daily-cap-micro-usd", "1000000",
            "--provider-transport", "broker",
            "--broker-db", broker_db,
            "--broker-credentials", credentials,
            "--broker-url", f"http://127.0.0.1:{broker_port}",
            "--broker-path", "/v1/messages",
            "--broker-model", "model-approved",
            "--provider-request", provider_request,
            "--broker-timeout", timeout,
            "--broker-allow-http-loopback",
        ]

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
        status = self.status("attempt-1")
        self.assertEqual(status["terminal_result"], "cancelled")
        self.assertIsNotNone(status["cancellation_requested_at"])
        self.assertEqual(status["cancellation_reason"], "operator_requested")

    def test_patch_artifact_is_applied_before_success_and_sets_actual_charge(self):
        result = self.execute(patch=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual(value["application"]["status"], "applied")
        status = self.status("attempt-1")
        self.assertEqual(status["terminal_result"], "succeeded")
        self.assertEqual(status["charge_micro_usd"], 500)

    def test_broker_turn_is_reserved_first_redacted_and_written_for_worker(self):
        upstream = Upstream(("127.0.0.1", 0))
        fixture = self.broker_fixture(upstream)
        broker_db, _, credentials, secret = fixture
        result = self.command(*self.broker_arguments("attempt-1", fixture))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(upstream.observed_key, secret)
        worker_input = json.loads((self.root / "attempt-1.input.json").read_text())
        self.assertEqual(worker_input["files"], ["app.txt"])
        self.assertNotIn(secret, result.stdout + result.stderr)
        broker_status = subprocess.run(
            [
                sys.executable, str(BROKER),
                "--db", str(broker_db),
                "--credentials", str(credentials),
                "--allow-http-loopback",
                "status", "--attempt-id", "attempt-1",
            ],
            text=True,
            capture_output=True,
            check=True,
        )
        self.assertFalse(json.loads(broker_status.stdout)["tokens"][0]["active"])

    def test_signal_cancels_only_targeted_broker_attempt_after_proven_drain(self):
        upstream = Upstream(("127.0.0.1", 0), blocked=True)
        fixture = self.broker_fixture(upstream)
        broker_db, _, credentials, _ = fixture
        attempts = [f"attempt-{number}" for number in range(1, 5)]
        processes = [
            subprocess.Popen(
                self.runtime_command(*self.broker_arguments(
                    attempt, fixture, ticket=f"T-{200 + number}"
                )),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            for number, attempt in enumerate(attempts, start=1)
        ]
        try:
            self.assertTrue(upstream.wait_for(4), "four broker calls did not overlap")
            processes[0].terminate()
            upstream.release("attempt-1")
            stdout, stderr = processes[0].communicate(timeout=5)
            self.assertEqual(processes[0].returncode, 2, stdout + stderr)
            cancelled = self.status("attempt-1")
            self.assertEqual(cancelled["terminal_result"], "cancelled")
            self.assertEqual(cancelled["cancellation_reason"], "controller_signal")

            replacement = self.execute("attempt-5")
            self.assertEqual(
                replacement.returncode, 0, replacement.stdout + replacement.stderr
            )
            status = subprocess.run(
                [sys.executable, str(COORDINATOR), "--db", str(self.db), "status"],
                text=True, capture_output=True, check=True,
            )
            value = json.loads(status.stdout)
            self.assertEqual(value["active_reserve_micro_usd"], 3000)
            self.assertEqual(
                {item["attempt_id"] for item in value["attempts"] if item["state"] == "submitted"},
                {"attempt-2", "attempt-3", "attempt-4"},
            )

            for attempt in attempts[1:]:
                upstream.release(attempt)
            for process in processes[1:]:
                stdout, stderr = process.communicate(timeout=5)
                self.assertEqual(process.returncode, 0, stdout + stderr)
        finally:
            for attempt in attempts:
                upstream.release(attempt)
            for process in processes:
                if process.poll() is None:
                    process.terminate()
                    process.communicate(timeout=5)

        report = subprocess.run(
            [
                sys.executable, str(BROKER),
                "--db", str(broker_db),
                "--credentials", str(credentials),
                "--allow-http-loopback", "status",
            ],
            text=True, capture_output=True, check=True,
        )
        self.assertTrue(all(
            token["active"] is False and token["request_in_flight"] is False
            for token in json.loads(report.stdout)["tokens"]
        ))
        final = subprocess.run(
            [sys.executable, str(COORDINATOR), "--db", str(self.db), "status"],
            text=True, capture_output=True, check=True,
        )
        self.assertEqual(json.loads(final.stdout)["active_reserve_micro_usd"], 0)

    def test_broker_timeout_retains_slot_until_request_drain_is_proven(self):
        upstream = Upstream(("127.0.0.1", 0), blocked=True)
        fixture = self.broker_fixture(upstream)
        process = subprocess.Popen(
            self.runtime_command(*self.broker_arguments(
                "attempt-timeout", fixture, timeout="0.2"
            )),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            self.assertTrue(upstream.wait_for(1), "broker request did not start")
            time.sleep(0.4)
            self.assertIsNone(process.poll(), "runtime released an in-flight request")
            self.assertEqual(self.status("attempt-timeout")["state"], "submitted")
            upstream.release("attempt-timeout")
            stdout, stderr = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 2, stdout + stderr)
        finally:
            upstream.release("attempt-timeout")
            if process.poll() is None:
                process.terminate()
                process.communicate(timeout=5)
        status = self.status("attempt-timeout")
        self.assertEqual(status["terminal_result"], "failed")
        self.assertEqual(status["charge_micro_usd"], status["reserve_micro_usd"])

    def test_provider_response_envelopes_and_malformed_results_settle_safely(self):
        upstream = Upstream(("127.0.0.1", 0))
        fixture = self.broker_fixture(upstream)
        mutation = json.dumps({"files": ["app.txt"], "patch": ""})
        upstream.responses.update({
            "chat": {"choices": [{"message": {"content": mutation}}]},
            "anthropic": {"content": [{"type": "text", "text": mutation}]},
            "responses": {"output": [{"content": [{"text": mutation}]}]},
            "invalid-json": b"{",
            "non-object": [],
            "missing-text": {},
            "invalid-mutation": {"choices": [{"message": {"content": "{"}}]},
            "invalid-schema": {"choices": [{"message": {"content": json.dumps({
                "files": "app.txt", "patch": "",
            })}}]},
        })
        for attempt in ("chat", "anthropic", "responses"):
            result = self.command(*self.broker_arguments(attempt, fixture))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(self.status(attempt)["terminal_result"], "succeeded")
        for attempt in (
            "invalid-json", "non-object", "missing-text", "invalid-mutation",
            "invalid-schema",
        ):
            result = self.command(*self.broker_arguments(attempt, fixture))
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            status = self.status(attempt)
            self.assertEqual(status["terminal_result"], "failed")
            self.assertEqual(status["charge_micro_usd"], status["reserve_micro_usd"])
            self.assertFalse((self.root / f"{attempt}.input.json").exists())

        final = subprocess.run(
            [sys.executable, str(COORDINATOR), "--db", str(self.db), "status"],
            text=True, capture_output=True, check=True,
        )
        self.assertEqual(json.loads(final.stdout)["active_reserve_micro_usd"], 0)

    def test_policy_binding_mismatch_fails_before_reservation(self):
        result = self.execute(policy_hash="f" * 64)
        self.assertEqual(result.returncode, 2)
        self.assertIn("not bound to the active provider policy", result.stdout)
        self.assertFalse(self.db.exists())


if __name__ == "__main__":
    unittest.main()
