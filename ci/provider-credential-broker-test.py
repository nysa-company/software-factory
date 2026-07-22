#!/usr/bin/env python3
"""Security and lifecycle tests for the attempt-bound credential broker."""

from __future__ import annotations

import http.server
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
BROKER = ROOT / "scripts" / "provider-credential-broker.py"
SCHEMA = "nysa.software-factory.provider-credentials/v1"


class Upstream(http.server.ThreadingHTTPServer):
    allow_reuse_address = False

    def __init__(self, address):
        super().__init__(address, UpstreamHandler)
        self.requests = []


class UpstreamHandler(http.server.BaseHTTPRequestHandler):
    server: Upstream

    def log_message(self, format, *args):
        return

    def do_POST(self):
        length = int(self.headers["Content-Length"])
        body = self.rfile.read(length)
        self.server.requests.append(
            {
                "authorization": self.headers.get("Authorization"),
                "body": json.loads(body),
                "path": self.path,
                "x_api_key": self.headers.get("X-Api-Key"),
            }
        )
        response = json.dumps({"ok": True}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)


def free_port():
    with socket.socket() as value:
        value.bind(("127.0.0.1", 0))
        return value.getsockname()[1]


class BrokerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        os.chmod(self.root, 0o700)
        self.db = self.root / "broker.sqlite3"
        self.credentials = self.root / "credentials.json"
        self.secret = "upstream-secret-must-not-leak"
        self.upstream = Upstream(("127.0.0.1", 0))
        self.upstream_thread = threading.Thread(
            target=self.upstream.serve_forever, daemon=True
        )
        self.upstream_thread.start()
        self.write_credentials()
        self.broker = None

    def tearDown(self):
        if self.broker is not None:
            self.broker.terminate()
            try:
                self.broker.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                self.broker.kill()
                self.broker.communicate(timeout=5)
        self.upstream.shutdown()
        self.upstream.server_close()
        self.upstream_thread.join(timeout=5)
        self.temp.cleanup()

    def write_credentials(self, **changes):
        route = {
            "provider_family": "anthropic",
            "upstream_origin": f"http://127.0.0.1:{self.upstream.server_port}",
            "credential_header": "X-Api-Key",
            "credential_prefix": "",
            "credential_value": self.secret,
            "allowed_paths": ["/v1/messages"],
            "allowed_models": ["model-approved"],
            "forward_headers": ["Anthropic-Version"],
            "max_request_bytes": 4096,
        }
        route.update(changes)
        self.credentials.write_text(
            json.dumps({"schema": SCHEMA, "routes": {"route-a": route}})
        )
        os.chmod(self.credentials, 0o600)

    def command(self, *arguments, expected=0):
        result = subprocess.run(
            [
                sys.executable,
                str(BROKER),
                "--db",
                str(self.db),
                "--credentials",
                str(self.credentials),
                "--allow-http-loopback",
                *arguments,
            ],
            text=True,
            capture_output=True,
            check=False,
            timeout=15,
        )
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        self.assertNotIn(self.secret, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def issue(self, attempt="attempt-1", model="model-approved", **values):
        arguments = [
            "issue",
            "--attempt-id",
            attempt,
            "--route-id",
            "route-a",
            "--model",
            model,
            "--reserve-micro-usd",
            str(values.get("reserve", 5000)),
            "--ttl-seconds",
            str(values.get("ttl", 300)),
            "--max-requests",
            str(values.get("max_requests", 1)),
        ]
        return self.command(*arguments)

    def start_broker(self):
        port = free_port()
        self.broker = subprocess.Popen(
            [
                sys.executable,
                str(BROKER),
                "--db",
                str(self.db),
                "--credentials",
                str(self.credentials),
                "--allow-http-loopback",
                "serve",
                "--listen-port",
                str(port),
                "--allow-plaintext-loopback",
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        deadline = time.time() + 5
        while time.time() < deadline:
            if self.broker.poll() is not None:
                stdout, stderr = self.broker.communicate()
                self.fail(stdout + stderr)
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    break
            except OSError:
                time.sleep(0.05)
        else:
            self.fail("broker did not listen")
        return f"http://127.0.0.1:{port}"

    def request(self, url, token, *, path="/v1/messages", model="model-approved"):
        request = urllib.request.Request(
            url + path,
            data=json.dumps({"model": model, "messages": []}).encode(),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Anthropic-Version": "2023-06-01",
                "X-Api-Key": "container-injected-secret",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def test_proxy_substitutes_host_credential_and_enforces_one_request(self):
        issuance = self.issue()
        url = self.start_broker()
        status, response = self.request(url, issuance["broker_token"])
        self.assertEqual((status, response), (200, {"ok": True}))
        self.assertEqual(len(self.upstream.requests), 1)
        observed = self.upstream.requests[0]
        self.assertEqual(observed["x_api_key"], self.secret)
        self.assertIsNone(observed["authorization"])
        second, _ = self.request(url, issuance["broker_token"])
        self.assertEqual(second, 403)
        self.assertEqual(len(self.upstream.requests), 1)
        state = self.command("status", "--attempt-id", "attempt-1")
        self.assertEqual(state["tokens"][0]["used_requests"], 1)
        self.assertFalse(state["tokens"][0]["active"])
        self.assertFalse(state["tokens"][0]["request_in_flight"])
        self.assertNotIn("broker_token", state["tokens"][0])

    def test_token_is_bound_to_route_model_budget_expiry_and_attempt(self):
        issuance = self.issue(reserve=1234, max_requests=3)
        self.assertEqual(issuance["reserve_micro_usd"], 1234)
        duplicate = self.command(
            "issue",
            "--attempt-id",
            "attempt-1",
            "--route-id",
            "route-a",
            "--model",
            "model-approved",
            "--reserve-micro-usd",
            "1234",
            expected=2,
        )
        self.assertIn("live token", duplicate["error"])
        url = self.start_broker()
        wrong_model, _ = self.request(
            url, issuance["broker_token"], model="model-forbidden"
        )
        wrong_path, _ = self.request(
            url, issuance["broker_token"], path="/v1/other"
        )
        self.assertEqual((wrong_model, wrong_path), (403, 403))
        self.assertEqual(self.upstream.requests, [])

    def test_revocation_prevents_use_and_never_reports_token(self):
        issuance = self.issue()
        revoked = self.command("revoke", "--attempt-id", "attempt-1")
        self.assertTrue(revoked["revoked"])
        url = self.start_broker()
        status, _ = self.request(url, issuance["broker_token"])
        self.assertEqual(status, 403)
        report = self.command("status")
        self.assertNotIn(issuance["broker_token"], json.dumps(report))

    def test_credential_file_and_endpoint_security_fail_closed(self):
        os.chmod(self.credentials, 0o644)
        unsafe = self.command(
            "issue",
            "--attempt-id",
            "attempt-1",
            "--route-id",
            "route-a",
            "--model",
            "model-approved",
            "--reserve-micro-usd",
            "1",
            expected=2,
        )
        self.assertIn("unsafe", unsafe["error"])
        os.chmod(self.credentials, 0o600)
        self.write_credentials(upstream_origin="http://provider.invalid")
        rejected = subprocess.run(
            [
                sys.executable,
                str(BROKER),
                "--db",
                str(self.db),
                "--credentials",
                str(self.credentials),
                "issue",
                "--attempt-id",
                "attempt-1",
                "--route-id",
                "route-a",
                "--model",
                "model-approved",
                "--reserve-micro-usd",
                "1",
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(rejected.returncode, 2)
        self.assertIn("HTTPS", rejected.stdout)
        self.assertNotIn(self.secret, rejected.stdout + rejected.stderr)

    def test_plaintext_listener_is_test_only_and_loopback_only(self):
        result = self.command(
            "serve",
            "--listen-host",
            "0.0.0.0",
            "--listen-port",
            str(free_port()),
            "--allow-plaintext-loopback",
            expected=2,
        )
        self.assertIn("restricted to loopback", result["error"])


if __name__ == "__main__":
    unittest.main()
