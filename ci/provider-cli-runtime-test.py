#!/usr/bin/env python3
"""Focused subscription CLI coordinator tests."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "scripts/provider-cli-runtime.py"
COORDINATOR = ROOT / "scripts/provider-coordinator.py"
INTEGRITY = ROOT / "scripts/lib/runs-integrity.py"


class ProviderCliRuntimeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        os.chmod(self.root, 0o700)
        self.db = self.root / "provider.sqlite3"
        self.account_db = self.root / "cursor-account.sqlite3"
        self.policy = self.root / "policy.json"
        limit = {"max_concurrent": 4, "max_starts": 20, "window_seconds": 60}
        account_limit = {"max_concurrent": 4, "max_starts": 20, "window_seconds": 60}
        self.policy.write_text(json.dumps({
            "schema": "factory-provider-concurrency-policy/v1",
            "coupled_max_concurrent": 4,
            "global": limit,
            "provider_families": {"openai": limit},
            "account_routes": {"codex": account_limit, "claude": account_limit},
        }, sort_keys=True, separators=(",", ":")) + "\n")
        self.owner_start = " ".join(subprocess.check_output(
            ["ps", "-o", "lstart=", "-p", str(os.getpid())], text=True
        ).split())

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def execute(
        self, attempt: str, command: list[str], ticket: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run([
            sys.executable, str(RUNTIME),
            "--coordinator", str(COORDINATOR),
            "--db", str(self.db),
            "--policy", str(self.policy),
            "--adapter", "codex",
            "--attempt-id", attempt,
            "--provider-family", "openai",
            "--account-route", "codex",
            "--reserve-micro-usd", "1000000",
            "--product-id", "product",
            "--ticket-id", ticket or f"T-{attempt}",
            "--budget-day", "2026-07-23",
            "--product-cap-micro-usd", "4000000",
            "--ticket-cap-micro-usd", "4000000",
            "--machine-cap-micro-usd", "4000000",
            "--", *command,
        ], text=True, capture_output=True, check=False, timeout=20)

    def status(self) -> dict:
        result = subprocess.run(
            [sys.executable, str(COORDINATOR), "--db", str(self.db), "status"],
            text=True, capture_output=True, check=True,
        )
        return json.loads(result.stdout)

    def account(
        self, action: str, lease: str, scope: str = "production-certified"
    ) -> dict:
        arguments = [
            sys.executable, str(COORDINATOR), "--db", str(self.db),
            "--account-db", str(self.account_db), f"account-{action}",
            "--lease-id", lease, "--owner-pid", str(os.getpid()),
            "--owner-pgid", str(os.getpgrp()),
            "--owner-start", self.owner_start,
        ]
        if action == "acquire":
            arguments.extend([
                "--account-route", "codex", "--trust-scope",
                scope, "--policy", str(self.policy),
                "--wait-seconds", "2",
            ])
        elif action == "bind-runtime":
            runtime = os.getpgrp()
            runtime_start = " ".join(subprocess.check_output(
                ["ps", "-o", "lstart=", "-p", str(runtime)], text=True
            ).split())
            arguments.extend([
                "--runtime-pid", str(runtime), "--runtime-pgid", str(runtime),
                "--runtime-start", runtime_start,
            ])
        result = subprocess.run(
            arguments, text=True, capture_output=True, check=True, timeout=10
        )
        return json.loads(result.stdout)

    def test_provider_outcome_stays_submitted_for_host_validation(self) -> None:
        success = self.execute("success", [sys.executable, "-c", "print('ok')"])
        failure = self.execute("failure", [sys.executable, "-c", "raise SystemExit(7)"])
        self.assertEqual(success.returncode, 0, success.stderr)
        self.assertEqual(failure.returncode, 7, failure.stderr)
        attempts = {item["attempt_id"]: item for item in self.status()["attempts"]}
        self.assertEqual(attempts["success"]["state"], "submitted")
        self.assertEqual(attempts["failure"]["state"], "submitted")
        self.assertIsNone(attempts["success"]["charge_micro_usd"])
        self.assertEqual(self.status()["active_reserve_micro_usd"], 2000000)

    def test_atomic_budget_refuses_fifth_reservation(self) -> None:
        for index in range(4):
            result = subprocess.run([
                sys.executable, str(COORDINATOR), "--db", str(self.db), "reserve",
                "--operation-id", f"hold-{index}", "--attempt-id", f"hold-{index}",
                "--provider-family", "openai", "--account-route", "codex",
                "--reserve-micro-usd", "1000000", "--product-id", "product",
                "--ticket-id", f"T-{index + 1}", "--budget-day", "2026-07-23",
                "--product-daily-cap-micro-usd", "4000000",
                "--ticket-cap-micro-usd", "4000000",
                "--machine-daily-cap-micro-usd", "4000000",
                "--policy", str(self.policy),
            ], text=True, capture_output=True, check=True)
            self.assertTrue(json.loads(result.stdout)["admitted"])
        refused = self.execute("fifth", [sys.executable, "-c", "print('must not run')"])
        self.assertEqual(refused.returncode, 8)
        self.assertNotIn("must not run", refused.stdout)

    def test_one_active_provider_call_per_ticket(self) -> None:
        first = subprocess.run([
            sys.executable, str(COORDINATOR), "--db", str(self.db), "reserve",
            "--operation-id", "ticket-first", "--attempt-id", "ticket-first",
            "--provider-family", "openai", "--account-route", "codex",
            "--reserve-micro-usd", "1000000", "--product-id", "product",
            "--ticket-id", "T-123", "--budget-day", "2026-07-23",
            "--product-daily-cap-micro-usd", "4000000",
            "--ticket-cap-micro-usd", "4000000",
            "--machine-daily-cap-micro-usd", "4000000",
            "--policy", str(self.policy),
        ], text=True, capture_output=True, check=True)
        self.assertTrue(json.loads(first.stdout)["admitted"])
        refused = self.execute(
            "ticket-second", [sys.executable, "-c", "print('must not run')"],
            ticket="T-123",
        )
        self.assertEqual(refused.returncode, 8)
        denial = self.status()["attempts"][-1]
        self.assertEqual(denial["state"], "terminal")
        self.assertEqual(denial["terminal_result"], "capacity_denied")

    def test_four_subscription_commands_overlap(self) -> None:
        command = [sys.executable, "-c", "import time; time.sleep(.6)"]
        started = time.monotonic()
        processes = [
            subprocess.Popen([
                sys.executable, str(RUNTIME),
                "--coordinator", str(COORDINATOR), "--db", str(self.db),
                "--policy", str(self.policy), "--adapter", "codex",
                "--attempt-id", f"overlap-{index}",
                "--provider-family", "openai", "--account-route",
                "codex",
                "--reserve-micro-usd", "1000000", "--product-id", "product",
                "--ticket-id", f"T-{index}", "--budget-day", "2026-07-23",
                "--product-cap-micro-usd", "4000000",
                "--ticket-cap-micro-usd", "1000000",
                "--machine-cap-micro-usd", "4000000", "--", *command,
            ], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            for index in range(4)
        ]
        results = [process.communicate(timeout=10) for process in processes]
        elapsed = time.monotonic() - started
        self.assertTrue(all(process.returncode == 0 for process in processes), results)
        self.assertLess(elapsed, 2.4)
        self.assertEqual(self.status()["counts"], {"submitted": 4})

    def test_cursor_command_requires_and_revalidates_host_account_lease(self) -> None:
        marker = self.root / "cursor-command"
        missing = subprocess.run([
            sys.executable, str(RUNTIME), "--coordinator", str(COORDINATOR),
            "--db", str(self.db), "--policy", str(self.policy),
            "--adapter", "cursor-openai", "--attempt-id", "cursor-missing",
            "--provider-family", "openai", "--account-route", "codex",
            "--reserve-micro-usd", "1", "--product-id", "product",
            "--ticket-id", "T-cursor-missing", "--budget-day", "2026-07-23",
            "--product-cap-micro-usd", "100", "--ticket-cap-micro-usd", "100",
            "--machine-cap-micro-usd", "100", "--", sys.executable, "-c",
            f"open({str(marker)!r}, 'w').close()",
        ], text=True, capture_output=True, check=False, timeout=10)
        self.assertEqual(missing.returncode, 2)
        self.assertIn("account admission lease is required", missing.stderr)
        self.assertFalse(marker.exists())

        lease = "cursor-valid-account"
        admission = self.account("acquire", lease)
        self.assertTrue(admission["admitted"])
        self.assertTrue(self.account("bind-runtime", lease)["bound"])
        policy_hash = admission["lease"]["policy_sha256"]
        valid = subprocess.run([
            sys.executable, str(RUNTIME), "--coordinator", str(COORDINATOR),
            "--db", str(self.db), "--policy", str(self.policy),
            "--adapter", "cursor-openai", "--account-db", str(self.account_db),
            "--account-lease-id", lease,
            "--account-owner-pid", str(os.getpid()),
            "--account-owner-pgid", str(os.getpgrp()),
            "--account-owner-start", self.owner_start,
            "--account-policy-sha256", policy_hash,
            "--trust-scope", "production-certified",
            "--attempt-id", "cursor-valid", "--provider-family", "openai",
            "--account-route", "codex", "--reserve-micro-usd", "1",
            "--product-id", "product", "--ticket-id", "T-cursor-valid",
            "--budget-day", "2026-07-23", "--product-cap-micro-usd", "100",
            "--ticket-cap-micro-usd", "100", "--machine-cap-micro-usd", "100",
            "--", sys.executable, "-c", f"open({str(marker)!r}, 'w').close()",
        ], text=True, capture_output=True, check=False, timeout=10)
        self.assertEqual(valid.returncode, 0, valid.stderr)
        self.assertTrue(marker.exists())

    def test_cursor_command_accepts_development_account_scope(self) -> None:
        marker = self.root / "development-cursor-command"
        lease = "cursor-development-account"
        admission = self.account("acquire", lease, scope="development-local")
        self.assertTrue(admission["admitted"])
        self.assertTrue(self.account("bind-runtime", lease)["bound"])
        result = subprocess.run([
            sys.executable, str(RUNTIME), "--coordinator", str(COORDINATOR),
            "--db", str(self.db), "--policy", str(self.policy),
            "--adapter", "cursor-openai", "--account-db", str(self.account_db),
            "--account-lease-id", lease,
            "--account-owner-pid", str(os.getpid()),
            "--account-owner-pgid", str(os.getpgrp()),
            "--account-owner-start", self.owner_start,
            "--account-policy-sha256", admission["lease"]["policy_sha256"],
            "--trust-scope", "development-local",
            "--attempt-id", "cursor-development", "--provider-family", "openai",
            "--account-route", "codex", "--reserve-micro-usd", "1",
            "--product-id", "product", "--ticket-id", "T-cursor-development",
            "--budget-day", "2026-07-23", "--product-cap-micro-usd", "100",
            "--ticket-cap-micro-usd", "100", "--machine-cap-micro-usd", "100",
            "--", sys.executable, "-c", f"open({str(marker)!r}, 'w').close()",
        ], text=True, capture_output=True, check=False, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(marker.exists())

    def test_authorized_sibling_manifests_are_preserved(self) -> None:
        runs = self.root / "runs"
        claims = self.root / "claims"
        runs.mkdir()
        claims.mkdir()
        start = " ".join(subprocess.check_output(
            ["ps", "-o", "lstart=", "-p", str(os.getpid())], text=True
        ).split())
        for index in range(2):
            attempt = f"sibling-{index}"
            reservation = subprocess.run([
                sys.executable, str(COORDINATOR), "--db", str(self.db), "reserve",
                "--operation-id", attempt, "--attempt-id", attempt,
                "--provider-family", "openai", "--account-route", "codex",
                "--reserve-micro-usd", "1000000", "--product-id", "product",
                "--ticket-id", f"T-{index}", "--budget-day", "2026-07-23",
                "--product-daily-cap-micro-usd", "4000000",
                "--ticket-cap-micro-usd", "1000000",
                "--machine-daily-cap-micro-usd", "4000000",
                "--policy", str(self.policy),
            ], text=True, capture_output=True, check=True)
            policy_hash = json.loads(reservation.stdout)["attempt"]["policy_sha256"]
            (runs / f"run-{index}.meta").write_text(
                "accounting_state=reserved\n"
                "provider_execution_mode=cli-concurrent-v1\n"
                f"provider_attempt_id={attempt}\nprovider_family=openai\n"
                f"account_route_id=codex\nactivation_policy_sha256={policy_hash}\n"
                f"ticket=T-{index}\nrole=builder\n"
            )
            claim = claims / f"T-{index}.builder.lock"
            claim.mkdir()
            (claim / "owner").write_text(
                f"pid={os.getpid()}\nprocess_start={start}\n"
                f"token={'0' * 31}{index}\n"
            )
        snapshot = subprocess.check_output([
            sys.executable, str(INTEGRITY), "snapshot-one", str(runs), "run-0.meta"
        ], text=True)
        accepted = subprocess.run([
            sys.executable, str(INTEGRITY), "check-concurrent", str(runs),
            str(claims), str(COORDINATOR), str(self.db),
        ], input=snapshot, text=True, capture_output=True)
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        waiting_attempt = f"1700000000-{os.getpid()}-cli"
        subprocess.run([
            sys.executable, str(COORDINATOR), "--db", str(self.db), "prepare",
            "--operation-id", "waiting-prepare", "--attempt-id", waiting_attempt,
            "--provider-family", "openai", "--account-route", "codex",
            "--reserve-micro-usd", "1000000", "--product-id", "product",
            "--ticket-id", "T-2", "--budget-day", "2026-07-23",
            "--product-daily-cap-micro-usd", "4000000",
            "--ticket-cap-micro-usd", "1000000",
            "--machine-daily-cap-micro-usd", "4000000",
        ], check=True, capture_output=True, text=True)
        waiting_claim = claims / "T-2.test-author.lock"
        waiting_claim.mkdir()
        (waiting_claim / "owner").write_text(
            f"pid={os.getpid()}\nprocess_start={start}\n"
            f"token={'2' * 32}\n"
        )
        for state in ("prepared", "reserved"):
            accepted = subprocess.run([
                sys.executable, str(INTEGRITY), "check-concurrent", str(runs),
                str(claims), str(COORDINATOR), str(self.db),
            ], input=snapshot, text=True, capture_output=True)
            self.assertEqual(accepted.returncode, 0, (state, accepted.stderr))
            if state == "prepared":
                subprocess.run([
                    sys.executable, str(COORDINATOR), "--db", str(self.db),
                    "admit", "--operation-id", "waiting-admit",
                    "--attempt-id", waiting_attempt, "--expected-version", "1",
                    "--policy", str(self.policy),
                ], check=True, capture_output=True, text=True)
        subprocess.run([
            sys.executable, str(COORDINATOR), "--db", str(self.db), "terminalize",
            "--operation-id", "waiting-terminal", "--attempt-id", waiting_attempt,
            "--expected-version", "2", "--result", "cancelled",
            "--charge-micro-usd", "0",
        ], check=True, capture_output=True, text=True)
        rejected_terminal = subprocess.run([
            sys.executable, str(INTEGRITY), "check-concurrent", str(runs),
            str(claims), str(COORDINATOR), str(self.db),
        ], input=snapshot, text=True, capture_output=True)
        self.assertNotEqual(rejected_terminal.returncode, 0)
        (waiting_claim / "owner").unlink()
        waiting_claim.rmdir()
        wrong_pid_attempt = f"1700000001-{os.getpid() + 1}-cli"
        subprocess.run([
            sys.executable, str(COORDINATOR), "--db", str(self.db), "prepare",
            "--operation-id", "wrong-pid-prepare",
            "--attempt-id", wrong_pid_attempt, "--provider-family", "openai",
            "--account-route", "codex", "--reserve-micro-usd", "1000000",
            "--product-id", "product", "--ticket-id", "T-3",
            "--budget-day", "2026-07-23",
            "--product-daily-cap-micro-usd", "4000000",
            "--ticket-cap-micro-usd", "1000000",
            "--machine-daily-cap-micro-usd", "4000000",
        ], check=True, capture_output=True, text=True)
        wrong_claim = claims / "T-3.builder.lock"
        wrong_claim.mkdir()
        (wrong_claim / "owner").write_text(
            f"pid={os.getpid()}\nprocess_start={start}\n"
            f"token={'3' * 32}\n"
        )
        rejected_pid = subprocess.run([
            sys.executable, str(INTEGRITY), "check-concurrent", str(runs),
            str(claims), str(COORDINATOR), str(self.db),
        ], input=snapshot, text=True, capture_output=True)
        self.assertNotEqual(rejected_pid.returncode, 0)
        subprocess.run([
            sys.executable, str(COORDINATOR), "--db", str(self.db), "terminalize",
            "--operation-id", "wrong-pid-terminal",
            "--attempt-id", wrong_pid_attempt, "--expected-version", "1",
            "--result", "cancelled", "--charge-micro-usd", "0",
        ], check=True, capture_output=True, text=True)
        (wrong_claim / "owner").unlink()
        wrong_claim.rmdir()
        sibling = runs / "run-1.meta"
        original = sibling.read_bytes()
        sibling.write_text(sibling.read_text().replace("provider_attempt_id=sibling-1", "provider_attempt_id=forged"))
        rejected = subprocess.run([
            sys.executable, str(INTEGRITY), "check-concurrent", str(runs),
            str(claims), str(COORDINATOR), str(self.db),
        ], input=snapshot, text=True, capture_output=True)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertNotEqual(sibling.read_bytes(), original)
        self.assertTrue((runs / "run-0.meta").is_file())


if __name__ == "__main__":
    unittest.main()
