#!/usr/bin/env python3
"""Focused atomicity, policy, replay, and fail-closed coordinator tests."""

from __future__ import annotations

import fcntl
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
COORDINATOR = ROOT / "scripts" / "provider-coordinator.py"
COORDINATOR_SPEC = importlib.util.spec_from_file_location(
    "provider_coordinator", COORDINATOR,
)
assert COORDINATOR_SPEC is not None and COORDINATOR_SPEC.loader is not None
COORDINATOR_MODULE = importlib.util.module_from_spec(COORDINATOR_SPEC)
COORDINATOR_SPEC.loader.exec_module(COORDINATOR_MODULE)


class ProviderCoordinatorTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.db = self.root / "state-v2.sqlite3"
        self.account_db = self.root / "cursor-account.sqlite3"
        self.policy = self.root / "policy.json"
        self.owner_pid = os.getpid()
        self.owner_pgid = os.getpgrp()
        self.owner_start = " ".join(subprocess.check_output(
            ["ps", "-o", "lstart=", "-p", str(self.owner_pid)], text=True
        ).split())
        source_sha = "5" * 40
        self.recovery_descriptors = []
        recovery_environment = {
            "FACTORY_CROSS_RELEASE_SOURCE_SHA": source_sha,
            "FACTORY_CROSS_RELEASE_PRODUCT_ID": f"test:{source_sha}",
            "FACTORY_KIT_TRUST_SCOPE": "qualification-candidate",
        }
        for prefix, name in (
            ("FACTORY_DISPATCH_ADMISSION_LOCK", "admission.lock"),
            ("FACTORY_QUALIFICATION_CONTROLLER_LOCK", "controller.lock"),
        ):
            path = self.root / name
            path.touch(mode=0o600)
            descriptor = os.open(path, os.O_RDWR)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            self.recovery_descriptors.append(descriptor)
            recovery_environment[prefix] = str(path)
            recovery_environment[f"{prefix}_FD"] = str(descriptor)
        self.recovery_environment = recovery_environment
        self.write_policy()

    def tearDown(self):
        for descriptor in self.recovery_descriptors:
            os.close(descriptor)
        self.temporary.cleanup()

    def write_policy(
        self, coupled=6, global_concurrent=6, global_starts=20,
        family_concurrent=6, account_concurrent=6, account_starts=20, window=60,
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
                    "max_starts": account_starts,
                    "window_seconds": window,
                },
                "account-b": {
                    "max_concurrent": account_concurrent,
                    "max_starts": account_starts,
                    "window_seconds": window,
                },
            },
        }
        self.policy.write_text(json.dumps(value), encoding="utf-8")

    def command(
        self, *arguments, check=True, db=None, environment=None, pass_fds=(),
    ):
        result = subprocess.run(
            [
                sys.executable, str(COORDINATOR), "--db", str(db or self.db),
                *map(str, arguments),
            ],
            text=True,
            capture_output=True,
            check=False,
            env=(None if environment is None else {**os.environ, **environment}),
            pass_fds=pass_fds,
        )
        if check and result.returncode:
            self.fail(f"{arguments}: {result.stdout}\n{result.stderr}")
        return result

    def json_command(self, *arguments, **kwargs):
        return json.loads(self.command(*arguments, **kwargs).stdout)

    def account_command(
        self, *arguments, check=True, db=None, account_db=None, environment=None,
        pass_fds=(),
    ):
        return self.json_command(
            "--account-db", account_db or self.account_db, *arguments,
            check=check, db=db, environment=environment, pass_fds=pass_fds,
        )

    def account_acquire(
        self, lease, account="account-a", scope="qualification-candidate",
        wait=1, owner_pid=None, owner_pgid=None, owner_start=None, check=True,
        db=None,
    ):
        return self.account_command(
            "account-acquire", "--lease-id", lease,
            "--account-route", account, "--trust-scope", scope,
            "--owner-pid", owner_pid or self.owner_pid,
            "--owner-pgid", owner_pgid or self.owner_pgid,
            "--owner-start", owner_start or self.owner_start,
            "--policy", self.policy, "--wait-seconds", wait,
            check=check, db=db,
        )

    def account_release(
        self, lease, owner_pid=None, owner_pgid=None, owner_start=None,
    ):
        return self.account_command(
            "account-release", "--lease-id", lease,
            "--owner-pid", owner_pid or self.owner_pid,
            "--owner-pgid", owner_pgid or self.owner_pgid,
            "--owner-start", owner_start or self.owner_start,
        )

    def account_bind_runtime(
        self, lease, runtime_pid, runtime_start, owner_pid=None,
        owner_pgid=None, owner_start=None,
    ):
        return self.account_command(
            "account-bind-runtime", "--lease-id", lease,
            "--owner-pid", owner_pid or self.owner_pid,
            "--owner-pgid", owner_pgid or self.owner_pgid,
            "--owner-start", owner_start or self.owner_start,
            "--runtime-pid", runtime_pid, "--runtime-pgid", runtime_pid,
            "--runtime-start", runtime_start,
        )

    def account_validate(
        self, lease, runtime_pid, runtime_start, policy_sha256,
        account="account-a", scope="qualification-candidate", owner_pid=None,
        owner_pgid=None, owner_start=None,
    ):
        return self.account_command(
            "account-validate", "--lease-id", lease,
            "--account-route", account, "--trust-scope", scope,
            "--owner-pid", owner_pid or self.owner_pid,
            "--owner-pgid", owner_pgid or self.owner_pgid,
            "--owner-start", owner_start or self.owner_start,
            "--runtime-pid", runtime_pid, "--runtime-pgid", runtime_pid,
            "--runtime-start", runtime_start,
            "--expected-policy-sha256", policy_sha256,
            "--policy", self.policy,
        )

    def account_recover_preview(self, lease, *, check=True, environment=None):
        return self.account_command(
            "account-recover-preview", "--lease-id", lease,
            check=check, environment=environment,
        )

    def account_recover_apply(
        self, lease, preview, *, check=True, account_db=None,
    ):
        return self.account_command(
            "account-recover-apply", "--lease-id", lease,
            "--expected-database-sha256", preview["database_sha256"],
            "--expected-lease-sha256", preview["lease_sha256"],
            check=check, environment=self.recovery_environment,
            pass_fds=self.recovery_descriptors, account_db=account_db,
        )

    def reserve(
        self, attempt, operation=None, now=1000, family="openai",
        account="account-a", check=True, reserve_micro_usd=1_250_000,
        product="product-a", ticket="T-123", budget_day="2026-07-20",
        product_cap=100_000_000, ticket_cap=100_000_000,
        machine_cap=100_000_000,
    ):
        return self.json_command(
            "reserve",
            "--operation-id", operation or f"reserve-{attempt}",
            "--attempt-id", attempt,
            "--provider-family", family,
            "--account-route", account,
            "--reserve-micro-usd", reserve_micro_usd,
            "--product-id", product,
            "--ticket-id", ticket,
            "--budget-day", budget_day,
            "--product-daily-cap-micro-usd", product_cap,
            "--ticket-cap-micro-usd", ticket_cap,
            "--machine-daily-cap-micro-usd", machine_cap,
            "--policy", self.policy,
            "--now", now,
            check=check,
        )

    def transition(self, command, attempt, version, operation, now, *extra):
        arguments = list(extra)
        if command == "terminalize" and "--charge-micro-usd" not in arguments:
            arguments.extend(("--charge-micro-usd", "500000"))
        return self.json_command(
            command,
            "--operation-id", operation,
            "--attempt-id", attempt,
            "--expected-version", version,
            "--now", now,
            *arguments,
        )

    def prepare(
        self, attempt, operation=None, family="openai", account="account-a",
        reserve_micro_usd=1_250_000, product="product-a", ticket="T-123",
        product_cap=100_000_000, ticket_cap=100_000_000,
        machine_cap=100_000_000,
    ):
        return self.json_command(
            "prepare", "--operation-id", operation or f"prepare-{attempt}",
            "--attempt-id", attempt, "--provider-family", family,
            "--account-route", account,
            "--reserve-micro-usd", reserve_micro_usd,
            "--product-id", product, "--ticket-id", ticket,
            "--budget-day", "2026-07-20",
            "--product-daily-cap-micro-usd", product_cap,
            "--ticket-cap-micro-usd", ticket_cap,
            "--machine-daily-cap-micro-usd", machine_cap,
        )

    def test_six_way_coupled_limit_and_terminal_release(self):
        admitted = [
            self.reserve(f"run-{number}", ticket=f"T-{number + 100}")
            for number in range(6)
        ]
        self.assertTrue(all(item["admitted"] for item in admitted))
        denied = self.reserve("run-7", ticket="T-107")
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

    def test_legacy_and_isolated_intervals_are_mutually_exclusive(self):
        entered = self.json_command(
            "legacy-enter",
            "--operation-id", "legacy-enter-1",
            "--interval-id", "legacy-1",
            "--product-id", "product-a",
            "--now", "100",
        )
        self.assertTrue(entered["entered"])
        denied = self.reserve("isolated-1", now=101)
        self.assertFalse(denied["admitted"])
        self.assertIn(
            {"limit": "legacy_barrier", "scope": "machine"},
            denied["denials"],
        )
        exited = self.json_command(
            "legacy-exit",
            "--operation-id", "legacy-exit-1",
            "--interval-id", "legacy-1",
            "--now", "102",
        )
        self.assertTrue(exited["exited"])
        admitted = self.json_command(
            "admit",
            "--operation-id", "isolated-admit-1",
            "--attempt-id", "isolated-1",
            "--expected-version", "1",
            "--policy", self.policy,
            "--now", "103",
        )
        self.assertTrue(admitted["admitted"])
        blocked = self.json_command(
            "legacy-enter",
            "--operation-id", "legacy-enter-2",
            "--interval-id", "legacy-2",
            "--product-id", "product-b",
            "--now", "104",
        )
        self.assertFalse(blocked["entered"])
        self.assertIn(
            {"limit": "isolated_barrier", "scope": "machine"},
            blocked["denials"],
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
                "--product-id", "product-a",
                "--ticket-id", "T-123",
                "--budget-day", "2026-07-20",
                "--product-daily-cap-micro-usd", "100000000",
                "--ticket-cap-micro-usd", "100000000",
                "--machine-daily-cap-micro-usd", "100000000",
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

    def test_wait_admit_rechecks_without_persisting_denial_operations(self):
        self.write_policy(coupled=1, global_concurrent=1)
        self.assertTrue(self.reserve("holder", ticket="T-1")["admitted"])
        self.prepare("waiter", ticket="T-2")
        started = time.monotonic()
        waiting = subprocess.Popen([
            "python3", str(COORDINATOR), "--db", str(self.db), "wait-admit",
            "--operation-id", "wait-waiter", "--attempt-id", "waiter",
            "--expected-version", "1", "--policy", str(self.policy),
            "--wait-seconds", "3",
        ], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        time.sleep(0.25)
        self.transition(
            "terminalize", "holder", 2, "finish-holder", 1001,
            "--result", "succeeded", "--charge-micro-usd", "0",
        )
        stdout, stderr = waiting.communicate(timeout=5)
        self.assertEqual(waiting.returncode, 0, stderr)
        self.assertTrue(json.loads(stdout)["admitted"])
        self.assertLess(time.monotonic() - started, 2)
        with sqlite3.connect(self.db) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM operations WHERE command='wait-admit'"
                ).fetchone()[0],
                1,
            )

    def test_wait_admit_cancellation_stops_only_waiting_attempt(self):
        self.write_policy(coupled=1, global_concurrent=1)
        self.assertTrue(self.reserve("holder", ticket="T-1")["admitted"])
        self.prepare("waiter", ticket="T-2")
        cancel = self.root / "cancel"
        waiting = subprocess.Popen([
            "python3", str(COORDINATOR), "--db", str(self.db), "wait-admit",
            "--operation-id", "wait-cancel", "--attempt-id", "waiter",
            "--expected-version", "1", "--policy", str(self.policy),
            "--wait-seconds", "3", "--cancel-path", str(cancel),
        ], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        time.sleep(0.25)
        cancel.write_text("cancel\n", encoding="utf-8")
        stdout, stderr = waiting.communicate(timeout=5)
        self.assertEqual(waiting.returncode, 0, stderr)
        result = json.loads(stdout)
        self.assertFalse(result["admitted"])
        self.assertEqual(result["stopped_by"], str(cancel))
        attempts = {
            item["attempt_id"]: item for item in self.json_command("status")["attempts"]
        }
        self.assertEqual(attempts["holder"]["state"], "reserved")
        self.assertEqual(attempts["waiter"]["state"], "prepared")

    def test_wait_admit_budget_denial_is_immediate(self):
        self.assertTrue(self.reserve(
            "spent", reserve_micro_usd=100, ticket="T-1",
            product_cap=100, ticket_cap=100, machine_cap=100,
        )["admitted"])
        self.prepare(
            "blocked", reserve_micro_usd=100, ticket="T-2",
            product_cap=100, ticket_cap=100, machine_cap=100,
        )
        started = time.monotonic()
        result = self.json_command(
            "wait-admit", "--operation-id", "wait-budget",
            "--attempt-id", "blocked", "--expected-version", "1",
            "--policy", self.policy, "--wait-seconds", "3",
        )
        self.assertLess(time.monotonic() - started, 1)
        self.assertFalse(result["admitted"])
        self.assertFalse(result["timed_out"])
        self.assertIn(
            {"limit": "budget_micro_usd", "scope": "product_day"},
            result["denials"],
        )

    def test_seven_simultaneous_reservations_admit_exactly_six(self):
        barrier = threading.Barrier(8)
        outputs = []

        def reserve(number):
            barrier.wait()
            outputs.append(self.reserve(
                f"parallel-{number}",
                operation=f"parallel-reserve-{number}",
                now=200,
                ticket=f"T-{number + 200}",
            ))

        threads = [threading.Thread(target=reserve, args=(number,)) for number in range(7)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()

        self.assertEqual(
            sorted(item["admitted"] for item in outputs),
            [False, True, True, True, True, True, True],
        )
        status = self.json_command("status")
        self.assertEqual(status["counts"], {"prepared": 1, "reserved": 6})

    def test_parallel_budget_reservations_prevent_overspend(self):
        barrier = threading.Barrier(4)
        outputs = []

        def reserve(number):
            barrier.wait()
            outputs.append(self.reserve(
                f"budget-{number}",
                operation=f"budget-reserve-{number}",
                reserve_micro_usd=100,
                ticket=f"T-{200 + number}",
                product_cap=200,
                ticket_cap=200,
                machine_cap=1000,
            ))

        threads = [threading.Thread(target=reserve, args=(number,)) for number in range(3)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()
        self.assertEqual(
            sorted(item["admitted"] for item in outputs), [False, True, True]
        )
        denied = next(item for item in outputs if not item["admitted"])
        self.assertIn(
            {"limit": "budget_micro_usd", "scope": "product_day"},
            denied["denials"],
        )

        admitted = [item["attempt"]["attempt_id"] for item in outputs if item["admitted"]]
        self.transition(
            "terminalize", admitted[0], 2, "budget-terminal", 201,
            "--result", "succeeded", "--charge-micro-usd", "0",
        )
        blocked_attempt = denied["attempt"]["attempt_id"]
        retry = self.json_command(
            "admit",
            "--operation-id", "budget-retry",
            "--attempt-id", blocked_attempt,
            "--expected-version", 1,
            "--policy", self.policy,
            "--now", 202,
        )
        self.assertTrue(retry["admitted"])

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

    def test_cursor_account_admission_is_shared_by_route_not_lane(self):
        self.write_policy(account_concurrent=1)
        lane_two = self.root / "lane-two.sqlite3"
        first = self.account_acquire(
            "lane-one", scope="production-certified", db=self.db
        )
        self.assertTrue(first["admitted"])
        blocked = self.account_acquire("lane-two", wait=1, db=lane_two)
        self.assertFalse(blocked["admitted"])
        self.assertTrue(blocked["timed_out"])
        independent = self.account_acquire(
            "other-account", account="account-b", db=lane_two
        )
        self.assertTrue(independent["admitted"])
        status = self.account_command("account-status")
        self.assertEqual(
            {(item["lease_id"], item["account_route"]) for item in status["leases"]},
            {("lane-one", "account-a"), ("other-account", "account-b")},
        )
        self.assertTrue(self.account_release("lane-one")["released"])
        self.assertTrue(self.account_release("other-account")["released"])

    def test_production_waiter_wins_the_final_cursor_slot(self):
        self.write_policy(account_concurrent=1)
        self.assertTrue(self.account_acquire("holder")["admitted"])

        def command(lease, scope):
            return [
                "python3", str(COORDINATOR), "--db", str(self.db),
                "--account-db", str(self.account_db), "account-acquire",
                "--lease-id", lease, "--account-route", "account-a",
                "--trust-scope", scope, "--owner-pid", str(self.owner_pid),
                "--owner-pgid", str(self.owner_pgid),
                "--owner-start", self.owner_start, "--policy", str(self.policy),
                "--wait-seconds", "4",
            ]

        qualification = subprocess.Popen(
            command("qualification", "qualification-candidate"),
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        time.sleep(0.1)
        production = subprocess.Popen(
            command("production", "production-certified"),
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            leases = self.account_command("account-status")["leases"]
            if {item["lease_id"] for item in leases} == {
                "holder", "qualification", "production",
            }:
                break
            time.sleep(0.05)
        self.assertTrue(self.account_release("holder")["released"])
        production_stdout, production_stderr = production.communicate(timeout=3)
        self.assertEqual(production.returncode, 0, production_stderr)
        self.assertTrue(json.loads(production_stdout)["admitted"])
        self.assertIsNone(qualification.poll())
        self.assertTrue(self.account_release("production")["released"])
        qualification_stdout, qualification_stderr = qualification.communicate(
            timeout=3
        )
        self.assertEqual(qualification.returncode, 0, qualification_stderr)
        self.assertTrue(json.loads(qualification_stdout)["admitted"])
        self.assertTrue(self.account_release("qualification")["released"])

    def test_cursor_account_stale_owner_cleanup_is_process_group_safe(self):
        self.write_policy(account_concurrent=1)
        owner = subprocess.Popen(
            ["python3", "-c", "import time; time.sleep(30)"],
            start_new_session=True,
        )
        owner_start = " ".join(subprocess.check_output(
            ["ps", "-o", "lstart=", "-p", str(owner.pid)], text=True
        ).split())
        try:
            self.assertTrue(self.account_acquire(
                "crashed", owner_pid=owner.pid, owner_pgid=owner.pid,
                owner_start=owner_start,
            )["admitted"])
        finally:
            owner.terminate()
            owner.wait(timeout=3)
        restarted = self.account_acquire("restart")
        self.assertTrue(restarted["admitted"])
        self.assertIn("crashed", restarted["stale_releases"])
        self.assertTrue(self.account_release("restart")["released"])

    def test_cursor_account_release_is_bound_and_policy_mismatch_refuses(self):
        self.write_policy(account_concurrent=1)
        first = self.account_acquire("account-a")
        self.assertTrue(first["admitted"])
        runtime = subprocess.Popen(
            ["python3", "-c", "import time; time.sleep(30)"],
            start_new_session=True,
        )
        runtime_start = " ".join(subprocess.check_output(
            ["ps", "-o", "lstart=", "-p", str(runtime.pid)], text=True
        ).split())
        self.assertTrue(self.account_bind_runtime(
            "account-a", runtime.pid, runtime_start
        )["bound"])
        validated = self.account_validate(
            "account-a", runtime.pid, runtime_start,
            first["lease"]["policy_sha256"],
        )
        self.assertTrue(validated["valid"])
        self.assertTrue(validated["lease"]["started"])
        self.assertTrue(self.account_acquire(
            "account-b", account="account-b"
        )["admitted"])
        runtime.terminate()
        runtime.wait(timeout=3)
        self.assertTrue(self.account_release("account-a")["released"])
        remaining = self.account_command("account-status")["leases"]
        self.assertEqual([item["lease_id"] for item in remaining], ["account-b"])

        self.write_policy(global_concurrent=5, account_concurrent=1)
        unrelated = self.account_acquire("unrelated-policy-change")
        self.assertTrue(unrelated["admitted"])
        next_runtime = subprocess.Popen(
            ["python3", "-c", "import time; time.sleep(30)"],
            start_new_session=True,
        )
        next_runtime_start = " ".join(subprocess.check_output(
            ["ps", "-o", "lstart=", "-p", str(next_runtime.pid)], text=True
        ).split())
        try:
            self.assertTrue(self.account_bind_runtime(
                "unrelated-policy-change", next_runtime.pid, next_runtime_start
            )["bound"])
            self.assertTrue(self.account_validate(
                "unrelated-policy-change", next_runtime.pid, next_runtime_start,
                unrelated["lease"]["policy_sha256"],
            )["valid"])
        finally:
            next_runtime.terminate()
            next_runtime.wait(timeout=3)
        self.assertTrue(
            self.account_release("unrelated-policy-change")["released"]
        )

        self.write_policy(global_concurrent=5, account_concurrent=2)
        sequential = self.account_acquire(
            "sequential-mismatch", account="account-a", check=False
        )
        self.assertEqual(sequential["status"], "error")
        self.assertIn("start-window policies disagree", sequential["error"])
        mismatch = self.account_acquire(
            "mismatch", account="account-b", check=False
        )
        self.assertEqual(mismatch["status"], "error")
        self.assertIn("policies disagree across lanes", mismatch["error"])
        self.assertTrue(self.account_release("account-b")["released"])

    def test_cursor_account_recovery_is_exact_and_replays_without_losing_history(self):
        owner = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            start_new_session=True,
        )
        runtime = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            start_new_session=True,
        )
        owner_start = " ".join(subprocess.check_output(
            ["ps", "-o", "lstart=", "-p", str(owner.pid)], text=True,
        ).split())
        runtime_start = " ".join(subprocess.check_output(
            ["ps", "-o", "lstart=", "-p", str(runtime.pid)], text=True,
        ).split())
        try:
            admission = self.account_acquire(
                "recover-exact", owner_pid=owner.pid, owner_pgid=owner.pid,
                owner_start=owner_start,
            )
            self.assertTrue(admission["admitted"])
            self.assertTrue(self.account_bind_runtime(
                "recover-exact", runtime.pid, runtime_start,
                owner_pid=owner.pid, owner_pgid=owner.pid,
                owner_start=owner_start,
            )["bound"])
            self.assertTrue(self.account_validate(
                "recover-exact", runtime.pid, runtime_start,
                admission["lease"]["policy_sha256"], owner_pid=owner.pid,
                owner_pgid=owner.pid, owner_start=owner_start,
            )["valid"])
            self.assertTrue(self.account_acquire("recover-sibling")["admitted"])
            owner.terminate()
            owner.wait(timeout=3)
            runtime_live = self.account_recover_preview(
                "recover-exact", check=False,
            )
            self.assertEqual(runtime_live["status"], "error")
            self.assertIn("still live", runtime_live["error"])
            runtime.terminate()
            runtime.wait(timeout=3)

            preview = self.account_recover_preview("recover-exact")
            self.assertEqual(preview["status"], "planned")
            self.assertEqual(preview["lease"]["lease_id"], "recover-exact")
            first = self.account_recover_apply("recover-exact", preview)
            replay = self.account_recover_apply("recover-exact", preview)
            self.assertEqual(first, replay)
            self.assertEqual(first["status"], "absent")
            status = self.account_command("account-status")
            self.assertEqual(
                [item["lease_id"] for item in status["leases"]],
                ["recover-sibling"],
            )
            self.assertEqual(
                [item["lease_id"] for item in status["starts"]],
                ["recover-exact"],
            )
            self.assertTrue(self.account_release("recover-sibling")["released"])
        finally:
            for process in (owner, runtime):
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=3)

    def test_cursor_account_recovery_covers_waiting_active_and_absent(self):
        owners = [
            subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                start_new_session=True,
            )
            for _ in range(2)
        ]
        try:
            for lease, owner in zip(("recover-waiting", "recover-active"), owners):
                started = " ".join(subprocess.check_output(
                    ["ps", "-o", "lstart=", "-p", str(owner.pid)], text=True,
                ).split())
                self.assertTrue(self.account_acquire(
                    lease, owner_pid=owner.pid, owner_pgid=owner.pid,
                    owner_start=started,
                )["admitted"])
            with sqlite3.connect(self.account_db) as connection:
                connection.execute(
                    "UPDATE account_leases SET state='waiting',admitted_at=NULL "
                    "WHERE lease_id='recover-waiting'"
                )
            for owner in owners:
                owner.terminate()
                owner.wait(timeout=3)
            for lease in ("recover-waiting", "recover-active"):
                preview = self.account_recover_preview(lease)
                self.assertEqual(preview["lease"]["state"], lease.removeprefix("recover-"))
                self.assertEqual(
                    self.account_recover_apply(lease, preview)["status"], "absent",
                )

            missing = self.root / "missing-account.sqlite3"
            absent = self.account_command(
                "account-recover-preview", "--lease-id", "never-created",
                account_db=missing,
            )
            self.assertEqual(absent["status"], "absent")
            self.assertFalse(missing.exists())
            result = self.account_recover_apply(
                "never-created", absent, account_db=missing,
            )
            self.assertEqual(result["status"], "absent")
            self.assertFalse(missing.exists())
        finally:
            for owner in owners:
                if owner.poll() is None:
                    owner.terminate()
                    owner.wait(timeout=3)

    def test_cursor_account_recovery_refuses_live_unknown_and_changed_lease(self):
        self.assertTrue(self.account_acquire("recover-live")["admitted"])
        live = self.account_recover_preview("recover-live", check=False)
        self.assertEqual(live["status"], "error")
        self.assertIn("still live", live["error"])
        self.assertTrue(self.account_release("recover-live")["released"])

        owner = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            start_new_session=True,
        )
        started = " ".join(subprocess.check_output(
            ["ps", "-o", "lstart=", "-p", str(owner.pid)], text=True,
        ).split())
        try:
            self.assertTrue(self.account_acquire(
                "recover-drift", owner_pid=owner.pid, owner_pgid=owner.pid,
                owner_start=started,
            )["admitted"])
            owner.terminate()
            owner.wait(timeout=3)
            preview = self.account_recover_preview("recover-drift")
            unavailable = self.account_recover_preview(
                "recover-drift", check=False,
                environment={"PATH": str(self.root / "no-commands")},
            )
            self.assertEqual(unavailable["status"], "error")
            self.assertIn("liveness is unavailable", unavailable["error"])
            with sqlite3.connect(self.account_db) as connection:
                connection.execute(
                    "UPDATE account_leases SET requested_at_ms=requested_at_ms+1 "
                    "WHERE lease_id='recover-drift'"
                )
            changed = self.account_recover_apply(
                "recover-drift", preview, check=False,
            )
            self.assertEqual(changed["status"], "error")
            self.assertIn("identity changed", changed["error"])
            current = self.account_recover_preview("recover-drift")
            self.assertEqual(
                self.account_recover_apply("recover-drift", current)["status"],
                "absent",
            )
        finally:
            if owner.poll() is None:
                owner.terminate()
                owner.wait(timeout=3)

    def test_cursor_account_recovery_refuses_database_replacement(self):
        owner = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            start_new_session=True,
        )
        started = " ".join(subprocess.check_output(
            ["ps", "-o", "lstart=", "-p", str(owner.pid)], text=True,
        ).split())
        try:
            self.assertTrue(self.account_acquire(
                "recover-database", owner_pid=owner.pid, owner_pgid=owner.pid,
                owner_start=started,
            )["admitted"])
            owner.terminate()
            owner.wait(timeout=3)
            preview = self.account_recover_preview("recover-database")
            original = self.root / "original-account.sqlite3"
            replacement = self.root / "replacement-account.sqlite3"
            self.account_command("account-status", account_db=replacement)
            self.account_db.replace(original)
            replacement.replace(self.account_db)
            refused = self.account_recover_apply(
                "recover-database", preview, check=False,
            )
            self.assertEqual(refused["status"], "error")
            self.assertIn("database identity changed", refused["error"])
            with sqlite3.connect(original) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT count(*) FROM account_leases "
                        "WHERE lease_id='recover-database'"
                    ).fetchone()[0],
                    1,
                )
        finally:
            if owner.poll() is None:
                owner.terminate()
                owner.wait(timeout=3)

    def test_cursor_account_recovery_refuses_database_swap_during_apply(self):
        owner = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            start_new_session=True,
        )
        started = " ".join(subprocess.check_output(
            ["ps", "-o", "lstart=", "-p", str(owner.pid)], text=True,
        ).split())
        try:
            self.assertTrue(self.account_acquire(
                "recover-race", owner_pid=owner.pid, owner_pgid=owner.pid,
                owner_start=started,
            )["admitted"])
            owner.terminate()
            owner.wait(timeout=3)
            preview = self.account_recover_preview("recover-race")
            replacement = self.root / "replacement-race.sqlite3"
            displaced = self.root / "displaced-race.sqlite3"
            shutil.copy2(self.account_db, replacement)
            original_identity = COORDINATOR_MODULE.account_database_identity
            calls = 0

            def swap_after_final_check(path, connection=None):
                nonlocal calls
                calls += 1
                identity = original_identity(path, connection)
                if calls == 3:
                    self.account_db.replace(displaced)
                    replacement.replace(self.account_db)
                return identity

            args = types.SimpleNamespace(
                lease_id="recover-race",
                expected_database_sha256=preview["database_sha256"],
                expected_lease_sha256=preview["lease_sha256"],
            )
            with (
                mock.patch.object(
                    COORDINATOR_MODULE,
                    "require_qualification_recovery_capability",
                ),
                mock.patch.object(
                    COORDINATOR_MODULE, "account_database_identity",
                    side_effect=swap_after_final_check,
                ),
                self.assertRaisesRegex(
                    COORDINATOR_MODULE.CoordinatorError,
                    "database changed while open",
                ),
            ):
                COORDINATOR_MODULE.account_recover_apply_command(
                    self.account_db, args,
                )
            with sqlite3.connect(self.account_db) as connection:
                self.assertEqual(connection.execute(
                    "SELECT count(*) FROM account_leases "
                    "WHERE lease_id='recover-race'"
                ).fetchone()[0], 1)
        finally:
            if owner.poll() is None:
                owner.terminate()
                owner.wait(timeout=3)

    def test_cursor_account_recovery_requires_qualification_capability_and_scope(self):
        owner = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            start_new_session=True,
        )
        started = " ".join(subprocess.check_output(
            ["ps", "-o", "lstart=", "-p", str(owner.pid)], text=True,
        ).split())
        try:
            self.assertTrue(self.account_acquire(
                "recover-production", scope="production-certified",
                owner_pid=owner.pid, owner_pgid=owner.pid, owner_start=started,
            )["admitted"])
            owner.terminate()
            owner.wait(timeout=3)
            preview = self.account_recover_preview("recover-production")
            unsealed = self.account_command(
                "account-recover-apply", "--lease-id", "recover-production",
                "--expected-database-sha256", preview["database_sha256"],
                "--expected-lease-sha256", preview["lease_sha256"], check=False,
            )
            self.assertEqual(unsealed["status"], "error")
            self.assertIn("capability is invalid", unsealed["error"])
            wrong_scope = self.account_recover_apply(
                "recover-production", preview, check=False,
            )
            self.assertEqual(wrong_scope["status"], "error")
            self.assertIn("scope is invalid", wrong_scope["error"])
            with sqlite3.connect(self.account_db) as connection:
                self.assertEqual(connection.execute(
                    "SELECT count(*) FROM account_leases "
                    "WHERE lease_id='recover-production'"
                ).fetchone()[0], 1)
        finally:
            if owner.poll() is None:
                owner.terminate()
                owner.wait(timeout=3)

    def test_cursor_account_state_is_owner_only_and_secret_free(self):
        self.assertTrue(self.account_acquire("secret-free")["admitted"])
        self.assertEqual(self.account_db.stat().st_mode & 0o777, 0o600)
        raw = self.account_db.read_bytes()
        self.assertNotIn(b"credential", raw.lower())
        self.assertNotIn(b"auth.json", raw)
        with sqlite3.connect(self.account_db) as connection:
            columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(account_leases)")
            }
        self.assertNotIn("credential", columns)
        self.assertNotIn("model", columns)
        self.assertNotIn("product_id", columns)
        self.assertNotIn("ticket_id", columns)
        self.assertEqual(self.account_command("account-status")["starts"], [])
        self.assertTrue(self.account_release("secret-free")["released"])

    def test_cursor_account_database_security_refusals(self):
        def status(path):
            return self.account_command(
                "account-status", account_db=path, check=False
            )

        insecure_parent = self.root / "insecure-parent"
        insecure_parent.mkdir(mode=0o755)
        self.assertEqual(
            status(insecure_parent / "account.sqlite3")["status"], "error"
        )

        mode_db = self.root / "mode.sqlite3"
        self.assertEqual(status(mode_db)["schema"], "factory-cursor-account-admission/v1")
        mode_db.chmod(0o644)
        self.assertEqual(status(mode_db)["status"], "error")

        hardlink_db = self.root / "hardlink-source.sqlite3"
        self.assertIn("leases", status(hardlink_db))
        os.link(hardlink_db, self.root / "hardlink.sqlite3")
        self.assertEqual(status(hardlink_db)["status"], "error")

        symlink_target = self.root / "symlink-target.sqlite3"
        self.assertIn("leases", status(symlink_target))
        symlink_db = self.root / "symlink.sqlite3"
        symlink_db.symlink_to(symlink_target.name)
        self.assertEqual(status(symlink_db)["status"], "error")

        wrong_app = self.root / "wrong-app.sqlite3"
        with sqlite3.connect(wrong_app) as connection:
            connection.execute("PRAGMA application_id=123")
            connection.execute("CREATE TABLE marker(value TEXT)")
        wrong_app.chmod(0o600)
        self.assertEqual(status(wrong_app)["status"], "error")

        wrong_version = self.root / "wrong-version.sqlite3"
        with sqlite3.connect(wrong_version) as connection:
            connection.execute("PRAGMA application_id=1314472769")
            connection.execute("PRAGMA user_version=2")
            connection.execute("CREATE TABLE marker(value TEXT)")
        wrong_version.chmod(0o600)
        self.assertEqual(status(wrong_version)["status"], "error")

        wrong_schema = self.root / "wrong-schema.sqlite3"
        self.assertIn("leases", status(wrong_schema))
        with sqlite3.connect(wrong_schema) as connection:
            connection.execute(
                "UPDATE metadata SET value='wrong' WHERE key='schema'"
            )
        self.assertEqual(status(wrong_schema)["status"], "error")

    def test_cursor_account_start_window_is_shared_and_expires(self):
        self.write_policy(account_starts=1, window=60)
        first = self.account_acquire("first-start")
        runtime = subprocess.Popen(
            ["python3", "-c", "import time; time.sleep(30)"],
            start_new_session=True,
        )
        runtime_start = " ".join(subprocess.check_output(
            ["ps", "-o", "lstart=", "-p", str(runtime.pid)], text=True
        ).split())
        try:
            self.assertTrue(self.account_bind_runtime(
                "first-start", runtime.pid, runtime_start
            )["bound"])
            self.assertTrue(self.account_validate(
                "first-start", runtime.pid, runtime_start,
                first["lease"]["policy_sha256"],
            )["valid"])
        finally:
            runtime.terminate()
            runtime.wait(timeout=3)
        self.assertTrue(self.account_release("first-start")["released"])
        blocked = self.account_acquire("within-window", wait=1)
        self.assertFalse(blocked["admitted"])
        self.assertTrue(blocked["timed_out"])
        with sqlite3.connect(self.account_db) as connection:
            connection.execute(
                "UPDATE account_starts SET started_at=started_at-window_seconds-1"
            )
        after_window = self.account_acquire("after-window")
        self.assertTrue(after_window["admitted"])
        self.assertTrue(self.account_release("after-window")["released"])

    def test_many_account_waiters_do_not_starve_the_shared_database(self):
        self.write_policy(account_concurrent=1)
        self.assertTrue(self.account_acquire("holder")["admitted"])

        def arguments(number):
            return [
                "python3", str(COORDINATOR), "--db", str(self.db),
                "--account-db", str(self.account_db), "account-acquire",
                "--lease-id", f"waiter-{number}", "--account-route", "account-a",
                "--trust-scope", "qualification-candidate",
                "--owner-pid", str(self.owner_pid),
                "--owner-pgid", str(self.owner_pgid),
                "--owner-start", self.owner_start, "--policy", str(self.policy),
                "--wait-seconds", "1",
            ]

        waiters = [
            subprocess.Popen(
                arguments(number), text=True, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            for number in range(8)
        ]
        results = [waiter.communicate(timeout=5) for waiter in waiters]
        self.assertTrue(all(waiter.returncode == 0 for waiter in waiters), results)
        outputs = [json.loads(stdout) for stdout, _stderr in results]
        self.assertTrue(all(item["timed_out"] for item in outputs))
        self.assertNotIn("locked", "".join(stderr for _stdout, stderr in results).lower())
        self.assertTrue(self.account_release("holder")["released"])

    def test_waiting_owner_death_cannot_reinsert_or_admit_its_lease(self):
        self.write_policy(account_concurrent=1)
        self.assertTrue(self.account_acquire("holder")["admitted"])
        owner = subprocess.Popen(
            ["python3", "-c", "import time; time.sleep(30)"],
            start_new_session=True,
        )
        owner_start = " ".join(subprocess.check_output(
            ["ps", "-o", "lstart=", "-p", str(owner.pid)], text=True
        ).split())
        waiter = subprocess.Popen([
            "python3", str(COORDINATOR), "--db", str(self.db),
            "--account-db", str(self.account_db), "account-acquire",
            "--lease-id", "dead-waiter", "--account-route", "account-a",
            "--trust-scope", "qualification-candidate",
            "--owner-pid", str(owner.pid), "--owner-pgid", str(owner.pid),
            "--owner-start", owner_start, "--policy", str(self.policy),
            "--wait-seconds", "5",
        ], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            leases = self.account_command("account-status")["leases"]
            if any(item["lease_id"] == "dead-waiter" for item in leases):
                break
            time.sleep(0.05)
        owner.kill()
        owner.wait(timeout=3)
        stdout, stderr = waiter.communicate(timeout=3)
        self.assertEqual(waiter.returncode, 0, stderr)
        result = json.loads(stdout)
        self.assertFalse(result["admitted"])
        self.assertEqual(result["owner_unavailable"], "dead")
        self.assertNotIn(
            "dead-waiter",
            {item["lease_id"] for item in self.account_command("account-status")["leases"]},
        )
        self.assertTrue(self.account_release("holder")["released"])
        successor = self.account_acquire("live-successor")
        self.assertTrue(successor["admitted"])
        self.assertTrue(self.account_release("live-successor")["released"])

    def test_cursor_account_wait_stops_on_pre_go_maintenance(self):
        self.write_policy(account_concurrent=1)
        self.assertTrue(self.account_acquire("holder")["admitted"])
        maintenance = self.root / "MAINTENANCE"
        waiting = subprocess.Popen([
            "python3", str(COORDINATOR), "--db", str(self.db),
            "--account-db", str(self.account_db), "account-acquire",
            "--lease-id", "stopped", "--account-route", "account-a",
            "--trust-scope", "qualification-candidate",
            "--owner-pid", str(self.owner_pid),
            "--owner-pgid", str(self.owner_pgid),
            "--owner-start", self.owner_start, "--policy", str(self.policy),
            "--wait-seconds", "10", "--cancel-path", str(maintenance),
        ], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            leases = self.account_command("account-status")["leases"]
            if any(item["lease_id"] == "stopped" for item in leases):
                break
            time.sleep(0.05)
        started = time.monotonic()
        maintenance.touch()
        stdout, stderr = waiting.communicate(timeout=3)
        self.assertEqual(waiting.returncode, 0, stderr)
        result = json.loads(stdout)
        self.assertFalse(result["admitted"])
        self.assertEqual(result["stopped_by"], str(maintenance))
        self.assertLess(time.monotonic() - started, 1)
        leases = self.account_command("account-status")["leases"]
        self.assertEqual([item["lease_id"] for item in leases], ["holder"])
        self.assertTrue(self.account_release("holder")["released"])

    def test_active_runtime_is_retained_when_retry_sees_maintenance(self):
        self.write_policy(account_concurrent=1)
        runtime = subprocess.Popen(
            ["python3", "-c", "import time; time.sleep(30)"],
            start_new_session=True,
        )
        runtime_start = " ".join(subprocess.check_output(
            ["ps", "-o", "lstart=", "-p", str(runtime.pid)], text=True
        ).split())
        try:
            self.assertTrue(self.account_acquire("active-runtime")["admitted"])
            self.assertTrue(self.account_bind_runtime(
                "active-runtime", runtime.pid, runtime_start
            )["bound"])
            maintenance = self.root / "MAINTENANCE"
            maintenance.touch()
            retry = self.account_command(
                "account-acquire", "--lease-id", "active-runtime",
                "--account-route", "account-a", "--trust-scope",
                "qualification-candidate", "--owner-pid", self.owner_pid,
                "--owner-pgid", self.owner_pgid,
                "--owner-start", self.owner_start, "--policy", self.policy,
                "--wait-seconds", 1, "--cancel-path", maintenance,
            )
            self.assertFalse(retry["admitted"])
            self.assertEqual(retry["stopped_by"], str(maintenance))
            leases = self.account_command("account-status")["leases"]
            self.assertEqual([item["lease_id"] for item in leases], ["active-runtime"])
            competitor = self.account_acquire("competitor", wait=1)
            self.assertFalse(competitor["admitted"])
            self.assertTrue(competitor["timed_out"])
        finally:
            runtime.terminate()
            runtime.wait(timeout=3)
        self.assertTrue(self.account_release("active-runtime")["released"])

    def test_account_start_rejects_policy_changed_after_admission(self):
        admission = self.account_acquire("stale-policy")
        runtime = subprocess.Popen(
            ["python3", "-c", "import time; time.sleep(30)"],
            start_new_session=True,
        )
        runtime_start = " ".join(subprocess.check_output(
            ["ps", "-o", "lstart=", "-p", str(runtime.pid)], text=True
        ).split())
        try:
            self.assertTrue(self.account_bind_runtime(
                "stale-policy", runtime.pid, runtime_start
            )["bound"])
            self.write_policy(global_concurrent=5)
            refused = self.account_command(
                "account-validate", "--lease-id", "stale-policy",
                "--account-route", "account-a", "--trust-scope",
                "qualification-candidate", "--owner-pid", self.owner_pid,
                "--owner-pgid", self.owner_pgid,
                "--owner-start", self.owner_start,
                "--runtime-pid", runtime.pid, "--runtime-pgid", runtime.pid,
                "--runtime-start", runtime_start,
                "--expected-policy-sha256",
                admission["lease"]["policy_sha256"],
                "--policy", self.policy, check=False,
            )
            self.assertEqual(refused["status"], "error")
            self.assertIn("policy changed before account start", refused["error"])
            self.assertEqual(self.account_command("account-status")["starts"], [])
        finally:
            runtime.terminate()
            runtime.wait(timeout=3)
        self.assertTrue(self.account_release("stale-policy")["released"])

    def test_dead_requester_shared_pgid_retains_live_provider_group(self):
        self.write_policy(account_concurrent=1)
        requester = subprocess.Popen(
            ["python3", "-c", "import time; time.sleep(30)"]
        )
        requester_start = " ".join(subprocess.check_output(
            ["ps", "-o", "lstart=", "-p", str(requester.pid)], text=True
        ).split())
        runtime = subprocess.Popen(
            ["python3", "-c", "import time; time.sleep(30)"],
            start_new_session=True,
        )
        runtime_start = " ".join(subprocess.check_output(
            ["ps", "-o", "lstart=", "-p", str(runtime.pid)], text=True
        ).split())
        try:
            self.assertTrue(self.account_acquire(
                "topology", owner_pid=requester.pid,
                owner_pgid=self.owner_pgid, owner_start=requester_start,
            )["admitted"])
            self.assertTrue(self.account_bind_runtime(
                "topology", runtime.pid, runtime_start,
                owner_pid=requester.pid, owner_pgid=self.owner_pgid,
                owner_start=requester_start,
            )["bound"])
            requester.terminate()
            requester.wait(timeout=3)
            blocked = self.account_acquire("replacement", wait=1)
            self.assertFalse(blocked["admitted"])
            self.assertTrue(blocked["timed_out"])
            runtime.terminate()
            runtime.wait(timeout=3)
            replacement = self.account_acquire("replacement-after-drain")
            self.assertTrue(replacement["admitted"])
            self.assertIn("topology", replacement["stale_releases"])
            self.assertTrue(
                self.account_release("replacement-after-drain")["released"]
            )
        finally:
            if requester.poll() is None:
                requester.terminate()
                requester.wait(timeout=3)
            if runtime.poll() is None:
                runtime.terminate()
                runtime.wait(timeout=3)


if __name__ == "__main__":
    unittest.main()
