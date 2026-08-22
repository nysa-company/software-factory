#!/usr/bin/env python3
"""Focused security, fixed-argv, and cross-project console tests."""

from __future__ import annotations

import argparse
import http.client
import importlib.util
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import unittest


ROOT = Path(__file__).resolve().parents[1]
CONSOLE_PATH = ROOT / "scripts" / "operator-console.py"
SPEC = importlib.util.spec_from_file_location("operator_console", CONSOLE_PATH)
CONSOLE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(CONSOLE)


class OperatorConsoleTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.registry_dir = self.root / "projects"
        self.registry_dir.mkdir(mode=0o700)
        for project in ("alpha", "bravo"):
            directory = self.registry_dir / project
            directory.mkdir(mode=0o700)
            active = directory / "active.json"
            active.write_text(
                json.dumps({"project": project, "product_path": f"/not/used/{project}"}),
                encoding="utf-8",
            )
            active.chmod(0o600)
        self.log = self.root / "launcher.log"
        self.launcher = self.root / "factory-launch"
        self.launcher.write_text(
            "#!/usr/bin/python3\n"
            "import json, pathlib, sys\n"
            f"log = pathlib.Path({str(self.log)!r})\n"
            "with log.open('a', encoding='utf-8') as handle:\n"
            "    handle.write(json.dumps(sys.argv[1:]) + '\\n')\n"
            "project = 'alpha' if sys.argv[1] == 'mismatch' else sys.argv[1]\n"
            "print(json.dumps({'project': project, 'argv': sys.argv[2:]}))\n",
            encoding="utf-8",
        )
        self.launcher.chmod(0o700)

    def tearDown(self):
        self.temporary.cleanup()

    def registry(self):
        return CONSOLE.ProjectRegistry(self.registry_dir)

    def invocations(self):
        if not self.log.exists():
            return []
        return [
            json.loads(line)
            for line in self.log.read_text(encoding="utf-8").splitlines()
        ]

    def start_server(self):
        state = CONSOLE.ConsoleState(self.registry(), self.launcher)
        server = CONSOLE.ConsoleServer(("127.0.0.1", 0), state, "127.0.0.1")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return state, server

    @staticmethod
    def request(server, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection(
            "127.0.0.1", server.server_address[1], timeout=3
        )
        encoded = None if body is None else json.dumps(body).encode()
        supplied = dict(headers or {})
        if encoded is not None:
            supplied.setdefault("Content-Type", "application/json")
            supplied.setdefault("Content-Length", str(len(encoded)))
        connection.request(method, path, body=encoded, headers=supplied)
        response = connection.getresponse()
        content = response.read()
        result = response.status, dict(response.getheaders()), content
        connection.close()
        return result

    def bootstrap(self, state, server):
        status, headers, _ = self.request(
            server, "GET", f"/bootstrap/{state.bootstrap}"
        )
        self.assertEqual(status, 303)
        cookie_header = headers["Set-Cookie"]
        self.assertIn("HttpOnly", cookie_header)
        self.assertIn("SameSite=Strict", cookie_header)
        self.assertIn("Path=/", cookie_header)
        return cookie_header.split(";", 1)[0]

    def test_registry_discovers_only_valid_active_records(self):
        self.assertEqual(self.registry().projects(), ["alpha", "bravo"])
        inactive = self.registry_dir / "inactive"
        inactive.mkdir(mode=0o700)
        self.assertEqual(self.registry().projects(), ["alpha", "bravo"])

        active = self.registry_dir / "alpha" / "active.json"
        active.write_text('{"project":"alpha","project":"bravo"}', encoding="utf-8")
        active.chmod(0o600)
        with self.assertRaisesRegex(CONSOLE.RegistryError, "invalid"):
            self.registry().projects()

    def test_registry_rejects_unsafe_active_record_metadata(self):
        active = self.registry_dir / "alpha" / "active.json"
        active.chmod(0o644)
        with self.assertRaisesRegex(CONSOLE.RegistryError, "unsafe"):
            self.registry().projects()
        active.chmod(0o600)

        active.write_text("x" * (CONSOLE.MAX_ACTIVE_RECORD_BYTES + 1), encoding="utf-8")
        active.chmod(0o600)
        with self.assertRaisesRegex(CONSOLE.RegistryError, "unsafe"):
            self.registry().projects()
        active.write_text('{"project":"alpha"}', encoding="utf-8")
        active.chmod(0o600)

        alias = self.root / "active-alias.json"
        os.link(active, alias)
        with self.assertRaisesRegex(CONSOLE.RegistryError, "unsafe"):
            self.registry().projects()
        alias.unlink()

        target = self.root / "active-target.json"
        target.write_text('{"project":"alpha"}', encoding="utf-8")
        target.chmod(0o600)
        active.unlink()
        active.symlink_to(target)
        with self.assertRaisesRegex(CONSOLE.RegistryError, "unsafe"):
            self.registry().projects()

    def test_registry_rejects_slug_mismatch_and_unsafe_directory(self):
        active = self.registry_dir / "alpha" / "active.json"
        active.write_text('{"project":"bravo"}', encoding="utf-8")
        active.chmod(0o600)
        with self.assertRaisesRegex(CONSOLE.RegistryError, "does not match"):
            self.registry().projects()

        active.write_text('{"project":"alpha"}', encoding="utf-8")
        active.chmod(0o600)
        project = self.registry_dir / "alpha"
        project.chmod(stat.S_IRWXU | stat.S_IWGRP)
        with self.assertRaisesRegex(CONSOLE.RegistryError, "directory is unsafe"):
            self.registry().projects()
        project.chmod(0o700)

        physical = self.root / "alpha-physical"
        project.rename(physical)
        project.symlink_to(physical, target_is_directory=True)
        with self.assertRaisesRegex(CONSOLE.RegistryError, "directory is unsafe"):
            self.registry().projects()

    def test_non_loopback_bind_is_rejected(self):
        with self.assertRaises(argparse.ArgumentTypeError):
            CONSOLE.loopback_address("0.0.0.0")
        self.assertEqual(CONSOLE.loopback_address("127.0.0.1"), "127.0.0.1")

    def test_snapshot_adapter_uses_only_fixed_argv_and_binds_project(self):
        client = CONSOLE.SNAPSHOT.LauncherClient(self.launcher)
        model = client.snapshot("alpha", "model")
        workflow = client.snapshot("bravo", "workflow")
        self.assertEqual(model["project"], "alpha")
        self.assertEqual(workflow["project"], "bravo")
        self.assertEqual(
            self.invocations(),
            [
                ["alpha", "models", "policy-candidates", "--json"],
                ["bravo", "operator-snapshot", "workflow", "--json"],
            ],
        )
        with self.assertRaisesRegex(CONSOLE.SNAPSHOT.SnapshotError, "selected project"):
            client.snapshot("mismatch", "model")
        with self.assertRaises(CONSOLE.SNAPSHOT.SnapshotError):
            client.snapshot("../alpha", "model")

    def test_control_actions_compile_to_fixed_launcher_argv(self):
        policy = {"schema": "factory-model-policy/v1"}
        digest = "a" * 64
        authorization = {
            "ticket": "T-123", "role": "spec-linter", "round": 3,
            "operator_id": "operator-1",
        }
        self.assertEqual(
            CONSOLE.SNAPSHOT.mutation_command(
                "ticket-authorize-round-plan", authorization,
            ),
            (
                "ticket-control", "authorize-round", "plan", "--ticket", "T-123",
                "--role", "spec-linter", "--round", "3", "--operator-id",
                "operator-1", "--json",
            ),
        )
        self.assertEqual(
            CONSOLE.SNAPSHOT.mutation_command(
                "ticket-authorize-round-apply",
                {**authorization, "approve_hash": digest},
            ),
            (
                "ticket-control", "authorize-round", "apply", "--ticket", "T-123",
                "--role", "spec-linter", "--round", "3", "--operator-id",
                "operator-1", "--approve-hash", digest, "--json",
            ),
        )
        self.assertEqual(
            CONSOLE.SNAPSHOT.mutation_command(
                "model-policy-apply",
                {
                    "policy": policy,
                    "expected_current_hash": digest,
                    "approve_hash": digest,
                },
            ),
            (
                "models", "policy-apply", "--policy",
                '{"schema":"factory-model-policy/v1"}',
                "--expected-current-hash", digest,
                "--approve-hash", digest, "--json",
            ),
        )
        envelope = CONSOLE.SNAPSHOT.mutation_command(
            "envelope-apply",
            {"changes": {"PER_RUN_BUDGET_USD": "8.00"}, "approve_hash": digest},
        )
        self.assertEqual(
            envelope,
            (
                "envelope", "apply", "--set", "PER_RUN_BUDGET_USD=8.00",
                "--approve-hash", digest, "--json",
            ),
        )
        cancellation = CONSOLE.SNAPSHOT.mutation_command(
            "attempt-cancel",
            {
                "ticket": "T-123", "run": "run-1",
                "reason": "budget_exhausted", "approve_hash": digest,
            },
        )
        self.assertEqual(
            cancellation,
            (
                "attempt", "cancel", "--ticket", "T-123", "--run", "run-1",
                "--reason", "budget_exhausted", "--approve-hash", digest, "--json",
            ),
        )
        with self.assertRaises(CONSOLE.SNAPSHOT.SnapshotError):
            CONSOLE.SNAPSHOT.mutation_command(
                "envelope-plan",
                {"changes": {"PER_RUN_BUDGET_USD": "8; touch /tmp/pwn"}},
            )
        for invalid in (
            {**authorization, "ticket": "../T-123"},
            {**authorization, "role": "reviewer"},
            {**authorization, "round": 2},
            {**authorization, "operator_id": "auto"},
        ):
            with self.assertRaises(CONSOLE.SNAPSHOT.SnapshotError):
                CONSOLE.SNAPSHOT.mutation_command(
                    "ticket-authorize-round-plan", invalid,
                )

    def test_bootstrap_is_one_time_and_routes_require_session(self):
        state, server = self.start_server()
        token = state.bootstrap
        cookie = self.bootstrap(state, server)
        second, _, _ = self.request(server, "GET", f"/bootstrap/{token}")
        self.assertEqual(second, 410)
        unauthorized, _, _ = self.request(server, "GET", "/api/projects")
        self.assertEqual(unauthorized, 401)
        ok, _, content = self.request(
            server, "GET", "/api/projects", headers={"Cookie": cookie}
        )
        self.assertEqual(ok, 200)
        self.assertEqual(json.loads(content), {"projects": ["alpha", "bravo"]})
        missing, _, _ = self.request(
            server, "GET", "/../../etc/passwd", headers={"Cookie": cookie}
        )
        self.assertEqual(missing, 404)

    def test_host_origin_csrf_and_allowlisted_mutation(self):
        state, server = self.start_server()
        cookie = self.bootstrap(state, server)
        authority = server.authority
        origin = server.origin
        wrong_host, _, _ = self.request(
            server, "GET", "/api/projects",
            headers={"Cookie": cookie, "Host": "localhost.invalid"},
        )
        self.assertEqual(wrong_host, 421)
        no_origin, _, _ = self.request(
            server,
            "POST",
            "/api/actions/model-enable",
            {"project": "alpha", "scope_type": "route", "scope_id": "route-one"},
            {"Cookie": cookie},
        )
        self.assertEqual(no_origin, 403)
        no_csrf, _, _ = self.request(
            server,
            "POST",
            "/api/actions/model-enable",
            {"project": "alpha", "scope_type": "route", "scope_id": "route-one"},
            {"Cookie": cookie, "Origin": origin, "Host": authority},
        )
        self.assertEqual(no_csrf, 403)
        session_status, _, session_body = self.request(
            server, "GET", "/api/session", headers={"Cookie": cookie}
        )
        self.assertEqual(session_status, 200)
        csrf = json.loads(session_body)["csrf"]
        accepted, _, accepted_body = self.request(
            server,
            "POST",
            "/api/actions/model-enable",
            {"project": "bravo", "scope_type": "route", "scope_id": "route-one"},
            {
                "Cookie": cookie,
                "Origin": origin,
                "Host": authority,
                "X-CSRF-Token": csrf,
            },
        )
        self.assertEqual(accepted, 200, accepted_body)
        self.assertEqual(
            self.invocations()[-1],
            [
                "bravo", "models", "enable", "--scope-type", "route",
                "--scope-id", "route-one", "--json",
            ],
        )
        authorized, _, authorized_body = self.request(
            server,
            "POST",
            "/api/actions/ticket-authorize-round-plan",
            {
                "project": "alpha", "ticket": "T-123", "role": "spec-linter",
                "round": 3, "operator_id": "operator-1",
            },
            {
                "Cookie": cookie,
                "Origin": origin,
                "Host": authority,
                "X-CSRF-Token": csrf,
            },
        )
        self.assertEqual(authorized, 200, authorized_body)
        self.assertEqual(
            self.invocations()[-1],
            [
                "alpha", "ticket-control", "authorize-round", "plan", "--ticket",
                "T-123", "--role", "spec-linter", "--round", "3", "--operator-id",
                "operator-1", "--json",
            ],
        )

    def test_snapshot_http_requests_remain_project_isolated(self):
        state, server = self.start_server()
        cookie = self.bootstrap(state, server)
        for selected in ("alpha", "bravo"):
            status, _, body = self.request(
                server,
                "GET",
                f"/api/snapshots/model?project={selected}",
                headers={"Cookie": cookie},
            )
            self.assertEqual(status, 200, body)
            value = json.loads(body)
            self.assertEqual(value["project"], selected)
            self.assertEqual(value["snapshot"]["project"], selected)
        duplicate, _, _ = self.request(
            server,
            "GET",
            "/api/snapshots/model?project=alpha&project=bravo",
            headers={"Cookie": cookie},
        )
        self.assertEqual(duplicate, 400)


if __name__ == "__main__":
    unittest.main()
