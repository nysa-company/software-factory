#!/usr/bin/env python3
"""Focused atomicity, policy, replay, and fail-closed coordinator tests."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import threading
import unittest


ROOT = Path(__file__).resolve().parents[1]
COORDINATOR = ROOT / "scripts" / "provider-coordinator.py"


class ProviderCoordinatorTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.db = self.root / "state-v2.sqlite3"
        self.policy = self.root / "policy.json"
        self.write_policy()

    def tearDown(self):
        self.temporary.cleanup()

    def write_policy(
        self, coupled=6, global_concurrent=6, global_starts=20,
        family_concurrent=6, account_concurrent=6, window=60,
    ):
        value = {
            "schema": "factory-provider-concurrency-policy/v1",
            "coupled_max_concurrent": coupled,
            "global": {
                "max_concurrent": global_concurrent,
                "max_starts": global_starts,
                "window_seconds": window,
            },
            "provider_families": {
                "openai": {
                    "max_concurrent": family_concurrent,
                    "max_starts": 20,
                    "window_seconds": window,
                },
                "anthropic": {
                    "max_concurrent": family_concurrent,
                    "max_starts": 20,
                    "window_seconds": window,
                },
            },
            "account_routes": {
                "account-a": {
                    "max_concurrent": account_concurrent,
                    "max_starts": 20,
                    "window_seconds": window,
                },
                "account-b": {
                    "max_concurrent": account_concurrent,
                    "max_starts": 20,
                    "window_seconds": window,
                },
            },
        }
        self.policy.write_text(json.dumps(value), encoding="utf-8")

    def command(self, *arguments, check=True, db=None):
        result = subprocess.run(
            [
                "python3", str(COORDINATOR), "--db", str(db or self.db),
                *map(str, arguments),
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        if check and result.returncode:
            self.fail(f"{arguments}: {result.stdout}\n{result.stderr}")
        return result

    def json_command(self, *arguments, **kwargs):
        return json.loads(self.command(*arguments, **kwargs).stdout)

    def reserve(
        self, attempt, operation=None, now=1000, family="openai",
        account="account-a", check=True,
    ):
        return self.json_command(
            "reserve",
            "--operation-id", operation or f"reserve-{attempt}",
            "--attempt-id", attempt,
            "--provider-family", family,
            "--account-route", account,
            "--reserve-micro-usd", 1_250_000,
            "--policy", self.policy,
            "--now", now,
            check=check,
        )

    def transition(self, command, attempt, version, operation, now, *extra):
        return self.json_command(
            command,
            "--operation-id", operation,
            "--attempt-id", attempt,
            "--expected-version", version,
            "--now", now,
            *extra,
        )

    def test_six_way_coupled_limit_and_terminal_release(self):
        admitted = [self.reserve(f"run-{number}") for number in range(6)]
        self.assertTrue(all(item["admitted"] for item in admitted))
        denied = self.reserve("run-7")
        self.assertFalse(denied["admitted"])
        self.assertIn(
            {"limit": "max_concurrent", "scope": "coupled"}, denied["denials"]
        )

        terminal = self.transition(
            "terminalize", "run-0", 2, "terminal-0", 1001,
            "--result", "cancelled",
        )
        self.assertEqual(terminal["state"], "terminal")
        retried = self.json_command(
            "admit",
            "--operation-id", "admit-run-7-second",
            "--attempt-id", "run-7",
            "--expected-version", 1,
            "--policy", self.policy,
            "--now", 1001,
        )
        self.assertTrue(retried["admitted"])
        status = self.json_command("status")
        self.assertEqual(status["counts"]["reserved"], 6)
        self.assertEqual(status["active_reserve_micro_usd"], 7_500_000)

    def test_scope_limits_and_completed_starts_window(self):
        self.write_policy(family_concurrent=1, global_starts=1, window=10)
        first = self.reserve("first", now=100)
        self.assertTrue(first["admitted"])
        self.transition(
            "terminalize", "first", 2, "finish-first", 101,
            "--result", "succeeded",
        )
        blocked = self.reserve(
            "blocked", operation="reserve-blocked", now=105,
            family="anthropic", account="account-b",
        )
        self.assertFalse(blocked["admitted"])
        self.assertIn(
            {"limit": "max_starts", "scope": "global"}, blocked["denials"]
        )
        admitted = self.json_command(
            "admit",
            "--operation-id", "admit-after-window",
            "--attempt-id", "blocked",
            "--expected-version", 1,
            "--policy", self.policy,
            "--now", 111,
        )
        self.assertTrue(admitted["admitted"])
        self.write_policy(
            family_concurrent=1, global_starts=20, window=10
        )
        family_blocked = self.reserve(
            "same-family", now=112, family="anthropic", account="account-b"
        )
        self.assertFalse(family_blocked["admitted"])
        self.assertIn(
            {"limit": "max_concurrent", "scope": "provider_family"},
            family_blocked["denials"],
        )

    def test_state_cas_replay_and_unknown_reconciliation_are_conservative(self):
        original = self.reserve("attempt", operation="reserve-once")
        replay = self.reserve("attempt", operation="reserve-once", now=1000)
        self.assertEqual(original, replay)
        conflict = self.reserve(
            "other", operation="reserve-once", now=1000, check=False
        )
        self.assertEqual(conflict["status"], "error")

        go = self.transition("mark-go", "attempt", 2, "go-attempt", 1001)
        self.assertEqual(go["state"], "GO")
        stale = self.command(
            "mark-submitted",
            "--operation-id", "stale-submit",
            "--attempt-id", "attempt",
            "--expected-version", 2,
            "--now", 1002,
            check=False,
        )
        self.assertEqual(stale.returncode, 2)
        observation = self.root / "observations.json"
        observation.write_text(json.dumps({
            "schema": "factory-provider-observations/v1",
            "observations": [{
                "attempt_id": "attempt",
                "expected_version": 3,
                "outcome": "unknown",
            }],
        }), encoding="utf-8")
        reconciled = self.json_command(
            "reconcile", "--operation-id", "reconcile-unknown",
            "--input", observation, "--now", 1003,
        )
        self.assertEqual(reconciled["results"][0]["action"], "retained")
        value = json.loads(observation.read_text())
        value["observations"][0]["outcome"] = "failed"
        observation.write_text(json.dumps(value), encoding="utf-8")
        failed = self.json_command(
            "reconcile", "--operation-id", "reconcile-post-go-failure",
            "--input", observation, "--now", 1004,
        )
        self.assertEqual(failed["results"][0]["action"], "retained")
        self.assertEqual(
            self.json_command("status", "--attempt-id", "attempt")
            ["attempts"][0]["state"],
            "GO",
        )

    def test_begin_immediate_serializes_competing_admissions(self):
        self.write_policy(coupled=1, global_concurrent=1)
        for attempt in ("left", "right"):
            self.json_command(
                "prepare",
                "--operation-id", f"prepare-{attempt}",
                "--attempt-id", attempt,
                "--provider-family", "openai",
                "--account-route", "account-a",
                "--reserve-micro-usd", 1,
                "--now", 100,
            )
        barrier = threading.Barrier(3)
        outputs = []

        def admit(attempt):
            barrier.wait()
            outputs.append(self.json_command(
                "admit",
                "--operation-id", f"admit-{attempt}",
                "--attempt-id", attempt,
                "--expected-version", 1,
                "--policy", self.policy,
                "--now", 101,
            ))

        threads = [threading.Thread(target=admit, args=(name,)) for name in ("left", "right")]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(item["admitted"] for item in outputs), [False, True])

    def test_database_and_policy_paths_fail_closed(self):
        self.reserve("secure")
        mode = stat_mode = self.db.stat().st_mode & 0o777
        self.assertEqual(mode, 0o600, oct(stat_mode))
        with sqlite3.connect(self.db) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertEqual(
                connection.execute(
                    "SELECT value FROM metadata WHERE key='schema'"
                ).fetchone()[0],
                "factory-provider-state/v2",
            )

        real = self.root / "real.sqlite3"
        self.db.replace(real)
        self.db.symlink_to(real.name)
        unsafe = self.command("status", check=False)
        self.assertEqual(unsafe.returncode, 2)
        self.assertIn("unsafe", unsafe.stdout)

        self.db.unlink()
        real.replace(self.db)
        bad_policy = json.loads(self.policy.read_text())
        bad_policy["coupled_max_concurrent"] = 7
        self.policy.write_text(json.dumps(bad_policy))
        rejected = self.reserve(
            "bad-policy", operation="bad-policy", check=False
        )
        self.assertEqual(rejected["status"], "error")


if __name__ == "__main__":
    unittest.main()
