#!/usr/bin/env python3
"""Regression tests for targeted attempt cancellation and process ownership."""

import csv
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
COORDINATOR = ROOT / "scripts/provider-coordinator.py"


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    sys.modules[name] = value
    spec.loader.exec_module(value)
    return value


IDENTITY = module("cancel_test_identity", ROOT / "scripts/lib/process-identity.py")
CANCEL = module("cancel_test", ROOT / "scripts/attempt-cancel.py")
LEDGER_HEADER = (
    "date,time,ticket,role,adapter,prompt_version,turns,cost_usd,exit_status,"
    "run_id,provider_family,model_id,selection_reason,cost_basis,adapter_version\n"
)


class AttemptCancellationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve() / "product"
        self.runs = self.root / "factory/runs"
        self.runs.mkdir(parents=True)
        (self.root / "factory/ledger.csv").write_text(LEDGER_HEADER)
        (self.root / "factory/runtime-ledger.csv").write_text(LEDGER_HEADER)
        self.processes = []

    def tearDown(self):
        for process in self.processes:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
        self.temp.cleanup()

    def spawn(self, *, ignore_term=False):
        code = "import time; time.sleep(30)"
        if ignore_term:
            code = (
                "import signal,time;"
                "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
                "print('ready',flush=True);"
                "time.sleep(30)"
            )
        process = subprocess.Popen(
            [sys.executable, "-c", code],
            start_new_session=True,
            stdout=subprocess.PIPE if ignore_term else subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        self.processes.append(process)
        if ignore_term:
            if process.stdout.readline() != "ready\n":
                self.fail("TERM-resistant fixture did not become ready")
            process.stdout.close()
        for _ in range(100):
            table = IDENTITY.process_table()
            if process.pid in table:
                return process, table[process.pid].started
            time.sleep(0.01)
        self.fail("test process did not appear")

    def manifest(
        self, process, started, *, ticket="T-1", go="0", provider_attempt_id="",
    ):
        run_id = "run-1"
        values = {
            "run_id": run_id,
            "phase": "prepared" if go == "0" else "spawned",
            "accounting_schema": "1",
            "accounting_state": "reserved",
            "reserved_usd": "2.00",
            "go_issued": go,
            "task_submitted": go,
            "submitted_at_epoch_ns": "102500000000" if go == "1" else "",
            "started_at": "2026-07-18T12:00:00Z",
            "terminal_at": "",
            "prompt_version": "1",
            "turns": "0",
            "effective_cost": "",
            "exit_status": "",
            "cost_basis": "",
            "ticket": ticket,
            "role": "builder",
            "adapter": "mock",
            "provider_family": "openai",
            "provider_attempt_id": provider_attempt_id,
            "provider_product_id": "qualification:factory",
            "account_route_id": "cursor",
            "activation_policy_sha256": getattr(self, "provider_policy_sha", ""),
            "model_id": "test",
            "selection_reason": "primary_ready",
            "adapter_version": "test",
            "pid": str(process.pid),
            "pgid": str(process.pid),
            "process_start": started,
            "role_exit": "",
            "cancellation_reason": "",
            "cancellation_preview_hash": "",
        }
        self.write_meta(values)
        (self.runs / f"{run_id}.pid").write_text(
            f"pid={process.pid}\npgid={process.pid}\n"
            f"run_id={run_id}\nprocess_start={started}\n"
        )
        return values

    def provider_command(self, database, *arguments):
        result = subprocess.run(
            [sys.executable, str(COORDINATOR), "--db", str(database), *arguments],
            text=True, capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def submitted_provider_attempt(
        self, database, attempt_id="provider-run-1", *, submit=True,
        product_id="qualification:factory",
    ):
        policy = database.with_name("policy.json")
        policy.parent.mkdir(parents=True, exist_ok=True)
        policy.parent.chmod(0o700)
        policy.write_text(json.dumps({
            "schema": "factory-provider-concurrency-policy/v1",
            "coupled_max_concurrent": 1,
            "global": {"max_concurrent": 1, "max_starts": 2, "window_seconds": 60},
            "provider_families": {
                "openai": {"max_concurrent": 1, "max_starts": 2, "window_seconds": 60},
            },
            "account_routes": {
                "cursor": {"max_concurrent": 1, "max_starts": 2, "window_seconds": 60},
            },
        }))
        reserve = self.provider_command(
            database, "reserve", "--operation-id", f"reserve-{attempt_id}",
            "--attempt-id", attempt_id, "--provider-family", "openai",
            "--account-route", "cursor", "--reserve-micro-usd", "2000000",
            "--product-id", product_id, "--ticket-id", "T-1",
            "--budget-day", "2026-08-23",
            "--product-daily-cap-micro-usd", "100000000",
            "--ticket-cap-micro-usd", "25000000",
            "--machine-daily-cap-micro-usd", "1000000000",
            "--policy", str(policy), "--now", "100",
        )["attempt"]
        self.provider_policy_sha = reserve.get("policy_sha256") or ""
        if not submit:
            return attempt_id
        go_result = self.provider_command(
            database, "mark-go", "--operation-id", f"go-{attempt_id}",
            "--attempt-id", attempt_id, "--expected-version", str(reserve["version"]),
            "--now", "101",
        )
        go = go_result.get("attempt", go_result)
        self.provider_policy_sha = go["policy_sha256"]
        self.provider_command(
            database, "mark-submitted", "--operation-id", f"submit-{attempt_id}",
            "--attempt-id", attempt_id, "--expected-version", str(go["version"]),
            "--now", "102",
        )
        return attempt_id

    def write_meta(self, values):
        (self.runs / "run-1.meta").write_text(
            "".join(f"{key}={value}\n" for key, value in values.items())
        )

    def stale_claim(self, root, process, started, *, owner=None):
        claim = root / "T-1.builder.lock"
        claim.mkdir(parents=True)
        (claim / "owner").write_text(owner or (
            f"pid={process.pid}\nprocess_start={started}\ntoken={'a' * 32}\n"
        ))
        return claim

    def expired_lease(self):
        leases = self.root / "factory/.dispatch-leases"
        leases.mkdir(exist_ok=True)
        lease = leases / "T-1.json"
        lease.write_bytes(CANCEL.canonical({
            "claimed_epoch": 1,
            "expires_epoch": 2,
            "lease_id": "b" * 64,
            "schema_version": 1,
            "ticket": "T-1",
        }))
        return lease

    def settle(self, process, values, plan, state):
        deadline = time.monotonic() + 5
        request = self.runs / "run-1.cancel-request.json"
        while not request.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        process.wait(timeout=5)
        (self.runs / "run-1.pid").unlink()
        values.update({
            "phase": state,
            "accounting_state": state,
            "terminal_at": "2026-07-18T12:01:00Z",
            "turns": "0",
            "effective_cost": "0" if state == "launch_void" else "2.00",
            "exit_status": "130",
            "cost_basis": "launch_void" if state == "launch_void" else "conservative_reservation",
            "role_exit": "cancelled",
            "cancellation_reason": plan["reason"],
            "cancellation_preview_hash": plan["preview_hash"],
        })
        self.write_meta(values)
        cost = "0" if state == "launch_void" else "2.00"
        with (self.root / "factory/runtime-ledger.csv").open("a", newline="") as handle:
            writer = csv.writer(handle, lineterminator="\n")
            writer.writerow([
                "2026-07-18", "12:00:00", "T-1", "builder", "mock", "1", "0",
                cost, "130", "run-1", "openai", "test", "primary_ready",
                values["cost_basis"], "test",
            ])
        CANCEL.emit_receipt(self.root, "T-1", "run-1")

    def test_pid_pgid_start_and_owner_are_all_required(self):
        process, started = self.spawn()
        values = self.manifest(process, started)
        identity = IDENTITY.load_identity(self.runs, "run-1", expected_ticket="T-1")
        self.assertEqual((identity.leader.pid, identity.leader.pgid), (process.pid, process.pid))
        with self.assertRaisesRegex(IDENTITY.IdentityError, "different ticket"):
            IDENTITY.load_identity(self.runs, "run-1", expected_ticket="T-2")
        for field, replacement, message in (
            ("pid", "99999999", "identity"),
            ("pgid", str(process.pid + 1), "disagree"),
            ("process_start", "stale", "disagree"),
        ):
            changed = dict(values)
            changed[field] = replacement
            self.write_meta(changed)
            with self.assertRaisesRegex(IDENTITY.IdentityError, message):
                IDENTITY.load_identity(self.runs, "run-1", expected_ticket="T-1")
        self.write_meta(values)

    def test_term_then_kill_escalation_revalidates_members(self):
        process, started = self.spawn(ignore_term=True)
        self.manifest(process, started, go="1")
        identity = IDENTITY.load_identity(self.runs, "run-1")
        reaper = threading.Thread(target=process.wait)
        reaper.start()
        self.assertEqual(IDENTITY.terminate(identity, 0.1), "KILL")
        reaper.join(timeout=2)

        member = identity.members[0]
        with mock.patch.object(
            IDENTITY, "process_table",
            return_value={member.pid + 1: IDENTITY.Process(member.pid + 1, member.pgid, member.started)},
        ):
            with self.assertRaisesRegex(IDENTITY.IdentityError, "membership changed"):
                IDENTITY.signal_group(identity, signal.SIGKILL)

    @unittest.skipUnless(
        sys.platform in {"darwin", "linux"}, "sandbox process probe is unsupported"
    )
    def test_sandbox_start_identity_is_stable_for_foreground_timeout_group(self):
        timeout = shutil.which("timeout")
        if timeout is None:
            self.skipTest("GNU timeout is unavailable")
        process = subprocess.Popen(
            [
                timeout, "--foreground", "30",
                sys.executable, "-c", "import time; time.sleep(30)",
            ],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.processes.append(process)
        for _ in range(100):
            table = IDENTITY.process_table()
            members = [item for item in table.values() if item.pgid == process.pid]
            if len(members) >= 2:
                break
            time.sleep(0.01)
        else:
            self.fail("foreground timeout did not retain its child process group")
        observed = subprocess.check_output(
            [
                sys.executable, str(ROOT / "scripts/lib/sandbox-ps.py"),
                "-o", "lstart=", "-p", str(process.pid),
            ],
            text=True,
        ).strip()
        if sys.platform == "linux":
            repeated = subprocess.check_output(
                [
                    sys.executable, str(ROOT / "scripts/lib/sandbox-ps.py"),
                    "-o", "lstart=", "-p", str(process.pid),
                ],
                text=True,
            ).strip()
            self.assertRegex(observed, r"^linux-start-[0-9]+$")
            self.assertEqual(repeated, observed)
        else:
            self.assertEqual(" ".join(observed.split()), table[process.pid].started)
        self.manifest(process, table[process.pid].started, go="1")
        identity = IDENTITY.load_identity(self.runs, "run-1")
        self.assertGreaterEqual(len(identity.members), 2)
        reaper = threading.Thread(target=process.wait)
        reaper.start()
        self.assertIn(IDENTITY.terminate(identity, 0.2), {"TERM", "KILL"})
        reaper.join(timeout=2)
        self.assertFalse(IDENTITY.group_alive(process.pid))

    def test_pre_go_cancel_is_zero_cost_and_replay_safe(self):
        process, started = self.spawn()
        values = self.manifest(process, started, go="0")
        plan = CANCEL.calculate(self.root, "T-1", "run-1", "operator_requested", "a" * 32)
        worker = threading.Thread(
            target=self.settle, args=(process, values, plan, "launch_void"),
        )
        worker.start()
        receipt = CANCEL.apply_plan(self.root, plan, 5)
        worker.join(timeout=5)
        self.assertEqual((receipt["accounting_state"], receipt["charged_usd"]), ("launch_void", "0"))
        self.assertEqual(CANCEL.apply_plan(self.root, plan, 1), receipt)

    def test_post_go_cancel_is_conservatively_charged(self):
        process, started = self.spawn(ignore_term=True)
        values = self.manifest(process, started, go="1")
        plan = CANCEL.calculate(self.root, "T-1", "run-1", "budget_exhausted", "b" * 32)
        worker = threading.Thread(
            target=self.settle,
            args=(process, values, plan, "cancelled_conservative"),
        )
        worker.start()
        receipt = CANCEL.apply_plan(self.root, plan, 5)
        worker.join(timeout=5)
        self.assertEqual(
            (receipt["accounting_state"], receipt["charged_usd"], receipt["reason"]),
            ("cancelled_conservative", "2.00", "budget_exhausted"),
        )

    def test_preview_hash_and_manifest_cas_refuse_drift(self):
        process, started = self.spawn()
        values = self.manifest(process, started)
        self.assertEqual(
            CANCEL.calculate(
                self.root, "T-1", "run-1", "budget_exhausted", None,
            ),
            CANCEL.calculate(
                self.root, "T-1", "run-1", "budget_exhausted", None,
            ),
        )
        plan = CANCEL.calculate(self.root, "T-1", "run-1", "budget_exhausted", "c" * 32)
        with self.assertRaisesRegex(CANCEL.CancelError, "preview hash"):
            CANCEL.validate_plan(plan, "0" * 64)
        values["turns"] = "1"
        self.write_meta(values)
        with self.assertRaisesRegex(CANCEL.CancelError, "changed after"):
            CANCEL.apply_plan(self.root, plan, 0.1)

    def test_competing_request_is_not_treated_as_replay(self):
        process, started = self.spawn()
        self.manifest(process, started)
        plan = CANCEL.calculate(self.root, "T-1", "run-1", "budget_exhausted", "d" * 32)
        other = {
            "plan": {**plan, "reason": "operator_requested"},
            "requested_at": "2026-07-18T12:00:00Z",
            "schema": CANCEL.REQUEST_SCHEMA,
        }
        (self.runs / "run-1.cancel-request.json").write_bytes(CANCEL.canonical(other))
        with self.assertRaisesRegex(CANCEL.CancelError, "another cancellation request"):
            CANCEL.apply_plan(self.root, plan, 0.1)

    def test_stale_provider_attempt_uses_authoritative_database_and_replays(self):
        database = self.root.parent / "qualification/provider/accounting/state-v2.sqlite3"
        attempt_id = self.submitted_provider_attempt(database)
        process, started = self.spawn()
        self.manifest(
            process, started, go="1", provider_attempt_id=attempt_id,
        )
        process.terminate()
        process.wait(timeout=5)
        plan = CANCEL.calculate(
            self.root, "T-1", "run-1", "operator_requested", "f" * 32,
        )
        with mock.patch.dict(
            os.environ, {"FACTORY_PROVIDER_DB": str(database)}, clear=False,
        ):
            receipt = CANCEL.apply_plan(self.root, plan, 1)
            self.assertEqual(CANCEL.apply_plan(self.root, plan, 1), receipt)
        attempt = self.provider_command(
            database, "status", "--attempt-id", attempt_id,
        )["attempts"][0]
        self.assertEqual(
            (
                receipt["accounting_state"], receipt["charged_usd"],
                attempt["state"], attempt["terminal_result"],
                attempt["charge_micro_usd"], attempt["version"],
            ),
            (
                "cancelled_conservative", "2.00", "terminal", "cancelled",
                2_000_000, 5,
            ),
        )

    def test_provider_only_pre_go_attempt_terminalizes_once_without_run_evidence(self):
        database = self.root.parent / "qualification/provider/accounting/state-v2.sqlite3"
        release = "d" * 40
        attempt_id = "1787640905-99999999-cli"
        self.submitted_provider_attempt(
            database, attempt_id=attempt_id, submit=False,
            product_id=f"relay-proof:{release}",
        )
        environment = {
            "FACTORY_PROJECT": "relay-proof",
            "FACTORY_PROVIDER_DB": str(database),
            "FACTORY_PROVIDER_PRODUCT_ID": f"relay-proof:{release}",
            "FACTORY_RELEASE_SHA": release,
        }
        with mock.patch.dict(os.environ, environment, clear=False):
            plan = CANCEL.calculate(
                self.root, "T-1", attempt_id, "operator_requested", "8" * 32,
            )
            receipt = CANCEL.apply_plan(self.root, plan, 1)
            self.assertEqual(
                CANCEL.calculate(
                    self.root, "T-1", attempt_id, "operator_requested", None,
                ),
                plan,
            )
            self.assertEqual(CANCEL.apply_plan(self.root, plan, 1), receipt)
        terminal = self.provider_command(
            database, "status", "--attempt-id", attempt_id,
        )["attempts"][0]
        self.assertEqual(
            (
                receipt["accounting_state"], receipt["charged_usd"],
                terminal["state"], terminal["terminal_result"],
                terminal["charge_micro_usd"], terminal["go_at"],
                terminal["submitted_at"],
            ),
            ("failed_pre_go", "0", "terminal", "failed_pre_go", 0, None, None),
        )
        base_run = attempt_id[:-4]
        self.assertFalse((self.runs / f"{base_run}.meta").exists())
        self.assertFalse((self.runs / f"{base_run}.pid").exists())
        with (self.root / "factory/runtime-ledger.csv").open(newline="") as handle:
            self.assertFalse(any(
                row.get("run_id") in {attempt_id, base_run}
                for row in csv.DictReader(handle)
            ))

    def test_provider_only_attempt_refuses_live_and_stale_wrapper_records(self):
        database = self.root.parent / "qualification/provider/accounting/state-v2.sqlite3"
        release = "c" * 40
        attempt_id = "1787640905-99999998-cli"
        self.submitted_provider_attempt(
            database, attempt_id=attempt_id, submit=False,
            product_id=f"relay-proof:{release}",
        )
        environment = {
            "FACTORY_PROJECT": "relay-proof",
            "FACTORY_PROVIDER_DB": str(database),
            "FACTORY_PROVIDER_PRODUCT_ID": f"relay-proof:{release}",
            "FACTORY_RELEASE_SHA": release,
        }
        heartbeat, started = self.spawn()
        base_run = attempt_id[:-4]
        wrapper = self.runs / f"{base_run}.wrapper"
        wrapper.write_text(
            f"run_id={base_run}\nwrapper_pid=99999998\n"
            f"wrapper_process_start=stale\nheartbeat_pid={heartbeat.pid}\n"
            f"heartbeat_pgid={heartbeat.pid}\nheartbeat_process_start={started}\n"
        )
        with mock.patch.dict(os.environ, environment, clear=False), \
                self.assertRaisesRegex(CANCEL.CancelError, "run evidence"):
            CANCEL.calculate(
                self.root, "T-1", attempt_id, "operator_requested", None,
            )
        wrapper.unlink()
        heartbeat.terminate()
        heartbeat.wait(timeout=5)
        stale = self.runs / f"{attempt_id}.wrapper"
        stale.write_text(
            f"run_id={attempt_id}\nwrapper_pid=99999998\n"
            "wrapper_process_start=stale\nheartbeat_pid=99999997\n"
            "heartbeat_pgid=99999997\nheartbeat_process_start=stale\n"
        )
        with mock.patch.dict(os.environ, environment, clear=False), \
                self.assertRaisesRegex(CANCEL.CancelError, "run evidence"):
            CANCEL.calculate(
                self.root, "T-1", attempt_id, "operator_requested", None,
            )
        status = self.provider_command(
            database, "status", "--attempt-id", attempt_id,
        )["attempts"][0]
        self.assertEqual(status["state"], "reserved")
        self.assertIsNone(status["charge_micro_usd"])

    def test_provider_only_attempt_refuses_account_lease_until_supported_release(self):
        database = self.root.parent / "qualification/provider/accounting/state-v2.sqlite3"
        account_database = database.with_name("cursor-account.sqlite3")
        release = "b" * 40
        attempt_id = "1787640905-99999997-cli"
        self.submitted_provider_attempt(
            database, attempt_id=attempt_id, submit=False,
            product_id=f"relay-proof:{release}",
        )
        owner_start = " ".join(subprocess.check_output(
            ["ps", "-o", "lstart=", "-p", str(os.getpid())], text=True,
        ).split())
        lease_id = f"{attempt_id}-account"
        owner = [
            "--lease-id", lease_id, "--owner-pid", str(os.getpid()),
            "--owner-pgid", str(os.getpgrp()), "--owner-start", owner_start,
        ]
        admission = self.provider_command(
            database, "--account-db", str(account_database), "account-acquire",
            *owner, "--account-route", "cursor", "--trust-scope",
            "qualification-candidate", "--policy", str(database.with_name("policy.json")),
            "--wait-seconds", "2",
        )
        self.assertTrue(admission["admitted"])
        environment = {
            "FACTORY_PROJECT": "relay-proof",
            "FACTORY_PROVIDER_DB": str(database),
            "FACTORY_CURSOR_ACCOUNT_DB": str(account_database),
            "FACTORY_PROVIDER_PRODUCT_ID": f"relay-proof:{release}",
            "FACTORY_RELEASE_SHA": release,
        }
        with mock.patch.dict(os.environ, environment, clear=False):
            plan = CANCEL.calculate(
                self.root, "T-1", attempt_id, "operator_requested", None,
            )
            with self.assertRaisesRegex(CANCEL.CancelError, "account lease"):
                CANCEL.apply_plan(self.root, plan, 1)
        self.assertEqual(
            self.provider_command(
                database, "status", "--attempt-id", attempt_id,
            )["attempts"][0]["state"],
            "reserved",
        )
        released = self.provider_command(
            database, "--account-db", str(account_database), "account-release", *owner,
        )
        self.assertTrue(released["released"])
        with mock.patch.dict(os.environ, environment, clear=False):
            CANCEL.apply_plan(self.root, plan, 1)
        self.assertEqual(
            self.provider_command(
                database, "--account-db", str(account_database), "account-status",
            )["leases"],
            [],
        )

    def test_provider_only_attempt_refuses_live_foreign_and_changed_state(self):
        database = self.root.parent / "qualification/provider/accounting/state-v2.sqlite3"
        release = "e" * 40
        process, _started = self.spawn()
        attempt_id = f"1787640905-{process.pid}-cli"
        self.submitted_provider_attempt(
            database, attempt_id=attempt_id, submit=False,
            product_id=f"relay-proof:{release}",
        )
        environment = {
            "FACTORY_PROJECT": "relay-proof",
            "FACTORY_PROVIDER_DB": str(database),
            "FACTORY_PROVIDER_PRODUCT_ID": f"relay-proof:{release}",
            "FACTORY_RELEASE_SHA": release,
        }
        with mock.patch.dict(os.environ, environment, clear=False), \
                self.assertRaisesRegex(CANCEL.CancelError, "still live"):
            CANCEL.calculate(
                self.root, "T-1", attempt_id, "operator_requested", None,
            )
        process.terminate()
        process.wait(timeout=5)
        with mock.patch.dict(os.environ, {
            **environment,
            "FACTORY_PROVIDER_PRODUCT_ID": f"foreign:{release}",
            "FACTORY_PROJECT": "foreign",
        }, clear=False), self.assertRaisesRegex(CANCEL.CancelError, "exact pre-GO"):
            CANCEL.calculate(
                self.root, "T-1", attempt_id, "operator_requested", None,
            )
        with mock.patch.dict(os.environ, environment, clear=False):
            plan = CANCEL.calculate(
                self.root, "T-1", attempt_id, "operator_requested", None,
            )
        before = self.provider_command(
            database, "status", "--attempt-id", attempt_id,
        )["attempts"][0]
        self.provider_command(
            database, "mark-go", "--operation-id", "provider-only-drift",
            "--attempt-id", attempt_id,
            "--expected-version", str(before["version"]),
        )
        with mock.patch.dict(os.environ, environment, clear=False), \
                self.assertRaisesRegex(CANCEL.CancelError, "does not match"):
            CANCEL.apply_plan(self.root, plan, 1)

    def test_cross_release_receipt_replay_repairs_legacy_provider_terminal(self):
        database = self.root.parent / "qualification/provider/accounting/state-v2.sqlite3"
        source_sha = "d" * 40
        product_id = f"qualification-fixture:{source_sha}"
        attempt_id = self.submitted_provider_attempt(
            database, product_id=product_id,
        )
        process, started = self.spawn()
        values = self.manifest(
            process, started, go="1", provider_attempt_id=attempt_id,
        )
        del values["provider_product_id"]
        del values["submitted_at_epoch_ns"]
        values.update({
            "contract_version": "2.0.0", "kit_sha": source_sha,
            "task_submitted": "0", "ticket_kit_sha": source_sha,
        })
        self.write_meta(values)
        plan = CANCEL.calculate(
            self.root, "T-1", "run-1", "operator_requested", "9" * 32,
        )
        process.kill()
        process.wait(timeout=5)
        (self.runs / "run-1.pid").unlink()
        (self.runs / "run-1.cancel-request.json").write_bytes(CANCEL.canonical({
            "plan": plan, "requested_at": "2026-07-18T12:01:00Z",
            "schema": CANCEL.REQUEST_SCHEMA,
        }))
        values.update({
            "phase": "cancelled_conservative",
            "accounting_state": "cancelled_conservative",
            "terminal_at": "2026-07-18T12:01:00Z",
            "turns": "0", "effective_cost": "2.00", "exit_status": "130",
            "cost_basis": "conservative_reservation", "role_exit": "cancelled",
            "cancellation_reason": plan["reason"],
            "cancellation_preview_hash": plan["preview_hash"],
        })
        self.write_meta(values)
        with (self.root / "factory/runtime-ledger.csv").open("a", newline="") as handle:
            csv.writer(handle, lineterminator="\n").writerow([
                "2026-07-18", "12:00:00", "T-1", "builder", "mock", "1", "0",
                "2.00", "130", "run-1", "openai", "test", "primary_ready",
                "conservative_reservation", "test",
            ])
        manifest_raw = (self.runs / "run-1.meta").read_bytes()
        receipt = {
            "accounting_state": "cancelled_conservative", "charged_usd": "2.00",
            "manifest_sha256": CANCEL.digest(manifest_raw),
            "preview_hash": plan["preview_hash"], "reason": plan["reason"],
            "run_id": "run-1", "schema": CANCEL.RECEIPT_SCHEMA,
            "terminal_at": values["terminal_at"], "ticket": "T-1",
        }
        (self.runs / "run-1.cancel.json").write_bytes(CANCEL.canonical(receipt))
        with mock.patch.dict(
            os.environ, {"FACTORY_PROVIDER_DB": str(database)}, clear=False,
        ), self.assertRaisesRegex(CANCEL.CancelError, "identity disagrees"):
            CANCEL.apply_plan(self.root, plan, 1)
        admission = self.root.parent / ".dispatch-admission.lock"
        controller = self.root.parent / "reconcile.lock"
        admission.touch(mode=0o600)
        controller.touch(mode=0o600)
        descriptor = os.open(admission, os.O_RDWR)
        controller_descriptor = os.open(controller, os.O_RDWR)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        fcntl.flock(controller_descriptor, fcntl.LOCK_EX)
        try:
            with mock.patch.dict(
                os.environ, {
                    "FACTORY_PROVIDER_DB": str(database),
                    "FACTORY_CROSS_RELEASE_PRODUCT_ID": product_id,
                    "FACTORY_CROSS_RELEASE_SOURCE_SHA": source_sha,
                    "FACTORY_DISPATCH_ADMISSION_LOCK": str(admission),
                    "FACTORY_DISPATCH_ADMISSION_LOCK_FD": str(descriptor),
                    "FACTORY_QUALIFICATION_CONTROLLER_LOCK": str(controller),
                    "FACTORY_QUALIFICATION_CONTROLLER_LOCK_FD": str(
                        controller_descriptor
                    ),
                }, clear=False,
            ):
                self.assertEqual(CANCEL.apply_plan(self.root, plan, 1), receipt)
                terminal = self.provider_command(
                    database, "status", "--attempt-id", attempt_id,
                )["attempts"][0]
                self.assertEqual(CANCEL.apply_plan(self.root, plan, 1), receipt)
                replay = self.provider_command(
                    database, "status", "--attempt-id", attempt_id,
                )["attempts"][0]
        finally:
            os.close(descriptor)
            os.close(controller_descriptor)
        self.assertEqual(
            (terminal["state"], terminal["terminal_result"],
             terminal["charge_micro_usd"], replay["version"]),
            ("terminal", "cancelled", 2_000_000, terminal["version"]),
        )

    def test_stale_terminal_provider_attempt_recovers_durable_actual_charge(self):
        self.assertEqual(CANCEL.micro_usd("0.00000001"), 1)
        database = self.root.parent / "qualification/provider/accounting/state-v2.sqlite3"
        attempt_id = self.submitted_provider_attempt(database)
        process, started = self.spawn()
        values = self.manifest(
            process, started, go="1", provider_attempt_id=attempt_id,
        )
        values.update({
            "phase": "terminalizing",
            "turns": "2",
            "effective_cost": "1.250000",
            "exit_status": "0",
            "cost_basis": "estimated_tokens",
            "terminal_at": "2026-07-18T12:01:00Z",
            "terminal_at_epoch_ns": "1784376060000000000",
            "terminal_intent_accounting_state": "completed",
            "terminal_intent_phase": "completed",
            "terminal_intent_result": "succeeded",
            "terminal_intent_charge_micro_usd": "1250000",
        })
        self.write_meta(values)
        before = self.provider_command(
            database, "status", "--attempt-id", attempt_id,
        )["attempts"][0]
        result = self.provider_command(
            database, "terminalize", "--operation-id", "actual-terminal",
            "--attempt-id", attempt_id,
            "--expected-version", str(before["version"]),
            "--result", "succeeded", "--charge-micro-usd", "1250000",
            "--now", "103",
        )
        terminal = result.get("attempt", result)
        process.kill()
        process.wait(timeout=5)
        plan = CANCEL.calculate(
            self.root, "T-1", "run-1", "operator_requested", "1" * 32,
        )
        with mock.patch.dict(
            os.environ, {"FACTORY_PROVIDER_DB": str(database)}, clear=False,
        ):
            receipt = CANCEL.apply_plan(self.root, plan, 1)
            replay = CANCEL.apply_plan(self.root, plan, 1)
        manifest = IDENTITY.parse_fields(
            (self.runs / "run-1.meta").read_bytes(), "run manifest",
        )
        row = CANCEL.ledger_row(
            self.root / "factory/runtime-ledger.csv", "run-1",
        )
        after = self.provider_command(
            database, "status", "--attempt-id", attempt_id,
        )["attempts"][0]
        self.assertEqual(replay, receipt)
        self.assertEqual(
            (
                receipt["accounting_state"], receipt["charged_usd"],
                manifest["phase"], manifest["accounting_state"],
                manifest["effective_cost"], row["cost_usd"],
                after["terminal_result"], after["charge_micro_usd"],
                after["version"],
            ),
            (
                "completed", "1.250000", "completed", "completed",
                "1.250000", "1.250000", "succeeded", 1_250_000,
                terminal["version"],
            ),
        )

    def test_stale_pre_go_intent_terminalizes_once_at_zero_cost(self):
        database = self.root.parent / "qualification/provider/accounting/state-v2.sqlite3"
        attempt_id = self.submitted_provider_attempt(database, submit=False)
        process, started = self.spawn()
        values = self.manifest(
            process, started, go="0", provider_attempt_id=attempt_id,
        )
        values.update({
            "phase": "terminalizing",
            "turns": "0",
            "effective_cost": "0",
            "exit_status": "3",
            "cost_basis": "launch_void",
            "terminal_at": "2026-07-18T12:01:00Z",
            "terminal_at_epoch_ns": "1784376060000000000",
            "terminal_intent_accounting_state": "launch_void",
            "terminal_intent_phase": "completed",
            "terminal_intent_result": "failed_pre_go",
            "terminal_intent_charge_micro_usd": "0",
        })
        self.write_meta(values)
        process.kill()
        process.wait(timeout=5)
        plan = CANCEL.calculate(
            self.root, "T-1", "run-1", "operator_requested", "2" * 32,
        )
        with mock.patch.dict(
            os.environ, {"FACTORY_PROVIDER_DB": str(database)}, clear=False,
        ):
            receipt = CANCEL.apply_plan(self.root, plan, 1)
            replay = CANCEL.apply_plan(self.root, plan, 1)
        manifest = IDENTITY.parse_fields(
            (self.runs / "run-1.meta").read_bytes(), "run manifest",
        )
        attempt = self.provider_command(
            database, "status", "--attempt-id", attempt_id,
        )["attempts"][0]
        self.assertEqual(replay, receipt)
        self.assertEqual(
            (
                receipt["accounting_state"], receipt["charged_usd"],
                manifest["phase"], manifest["accounting_state"],
                attempt["terminal_result"], attempt["charge_micro_usd"],
            ),
            ("launch_void", "0", "completed", "launch_void", "failed_pre_go", 0),
        )

    def test_stale_submitted_intent_terminalizes_once_at_actual_cost(self):
        database = self.root.parent / "qualification/provider/accounting/state-v2.sqlite3"
        attempt_id = self.submitted_provider_attempt(database)
        process, started = self.spawn()
        values = self.manifest(
            process, started, go="1", provider_attempt_id=attempt_id,
        )
        values.update({
            "phase": "terminalizing",
            "turns": "2",
            "effective_cost": "1.250000",
            "exit_status": "0",
            "cost_basis": "estimated_tokens",
            "terminal_at": "2026-07-18T12:01:00Z",
            "terminal_at_epoch_ns": "1784376060000000000",
            "terminal_intent_accounting_state": "completed",
            "terminal_intent_phase": "completed",
            "terminal_intent_result": "succeeded",
            "terminal_intent_charge_micro_usd": "1250000",
        })
        self.write_meta(values)
        before = self.provider_command(
            database, "status", "--attempt-id", attempt_id,
        )["attempts"][0]
        process.kill()
        process.wait(timeout=5)
        plan = CANCEL.calculate(
            self.root, "T-1", "run-1", "operator_requested", "3" * 32,
        )
        with mock.patch.dict(
            os.environ, {"FACTORY_PROVIDER_DB": str(database)}, clear=False,
        ):
            receipt = CANCEL.apply_plan(self.root, plan, 1)
            self.assertEqual(CANCEL.apply_plan(self.root, plan, 1), receipt)
        after = self.provider_command(
            database, "status", "--attempt-id", attempt_id,
        )["attempts"][0]
        self.assertEqual(
            (
                receipt["accounting_state"], receipt["charged_usd"],
                after["terminal_result"], after["charge_micro_usd"],
                after["version"],
            ),
            ("completed", "1.250000", "succeeded", 1_250_000, before["version"] + 1),
        )

    def test_stale_attempt_uses_external_ledger_and_sibling_claim_once(self):
        authority = self.root.parent / "qualification/operator"
        authority.mkdir(parents=True)
        ledger = authority / "runtime-ledger.csv"
        ledger.write_text(LEDGER_HEADER)
        durable = authority / "ledger.csv"
        durable.write_text(LEDGER_HEADER)
        (self.root / "factory/ledger.csv").unlink()
        database = self.root.parent / "qualification/provider/accounting/state-v2.sqlite3"
        attempt_id = self.submitted_provider_attempt(database)
        process, started = self.spawn()
        self.manifest(process, started, go="1", provider_attempt_id=attempt_id)
        process.terminate()
        process.wait(timeout=5)
        external_claim = self.stale_claim(
            authority / ".active-runs", process, started,
        )
        decoy_claim = self.stale_claim(
            self.root / "factory/.active-runs", process, started,
        )
        lease = self.expired_lease()
        environment = {
            "FACTORY_PROVIDER_DB": str(database),
            "FACTORY_LEDGER": str(ledger),
            "FACTORY_DURABLE_LEDGER": str(durable),
        }
        with mock.patch.dict(os.environ, environment, clear=False):
            plan = CANCEL.calculate(
                self.root, "T-1", "run-1", "operator_requested", "1" * 32,
            )
            receipt = CANCEL.apply_plan(self.root, plan, 1)
            self.assertEqual(CANCEL.apply_plan(self.root, plan, 1), receipt)
            self.assertEqual(CANCEL.ledger_row(ledger, "run-1")["exit_status"], "130")
        attempt = self.provider_command(
            database, "status", "--attempt-id", attempt_id,
        )["attempts"][0]
        self.assertEqual(
            (attempt["state"], attempt["charge_micro_usd"], attempt["version"]),
            ("terminal", 2_000_000, 5),
        )
        self.assertFalse(external_claim.exists())
        self.assertTrue(decoy_claim.exists())
        self.assertFalse(lease.exists())
        with self.assertRaises(CANCEL.CancelError):
            CANCEL.ledger_row(
                self.root / "factory/runtime-ledger.csv", "run-1",
            )

    def test_external_authority_refuses_unsafe_paths_before_provider_mutation(self):
        authority = self.root.parent / "qualification/operator"
        authority.mkdir(parents=True)
        ledger = authority / "runtime-ledger.csv"
        ledger.write_text(LEDGER_HEADER)
        database = self.root.parent / "qualification/provider/accounting/state-v2.sqlite3"
        attempt_id = self.submitted_provider_attempt(database)
        process, started = self.spawn()
        manifest = self.manifest(
            process, started, go="1", provider_attempt_id=attempt_id,
        )
        process.terminate()
        process.wait(timeout=5)
        claim = self.stale_claim(
            authority / ".active-runs", process, started, owner="invalid\n",
        )
        environment = {
            "FACTORY_PROVIDER_DB": str(database),
            "FACTORY_LEDGER": str(ledger),
            "FACTORY_DURABLE_LEDGER": str(self.root / "factory/ledger.csv"),
        }
        with mock.patch.dict(os.environ, environment, clear=False):
            plan = CANCEL.calculate(
                self.root, "T-1", "run-1", "operator_requested", "2" * 32,
            )
            with self.assertRaisesRegex(CANCEL.IDENTITY.IdentityError, "active-run owner"):
                CANCEL.apply_plan(self.root, plan, 1)
        attempt = self.provider_command(
            database, "status", "--attempt-id", attempt_id,
        )["attempts"][0]
        self.assertEqual(
            (attempt["state"], attempt["charge_micro_usd"]), ("submitted", None),
        )
        self.assertEqual(
            IDENTITY.parse_fields(
                (self.runs / "run-1.meta").read_bytes(), "run manifest",
            )["accounting_state"],
            manifest["accounting_state"],
        )
        self.assertTrue(claim.exists())

        (claim / "owner").write_text(
            f"pid={process.pid}\nprocess_start={started}\ntoken={'a' * 32}\n"
        )
        (self.root / "factory/ledger.csv").write_text("malformed\n")
        with mock.patch.dict(os.environ, environment, clear=False), \
                self.assertRaisesRegex(CANCEL.CancelError, "ledger projection"):
            CANCEL.apply_plan(self.root, plan, 1)
        attempt = self.provider_command(
            database, "status", "--attempt-id", attempt_id,
        )["attempts"][0]
        self.assertEqual(attempt["state"], "submitted")

        linked = authority / "linked.csv"
        linked.symlink_to(ledger)
        writable = authority / "writable.csv"
        writable.write_text(LEDGER_HEADER)
        writable.chmod(0o666)
        for configured, message in (
            ("relative.csv", "not absolute"),
            (str(authority / "missing.csv"), "missing"),
            (str(linked), "unsafe"),
            (str(writable), "unsafe"),
        ):
            with self.subTest(configured=configured), mock.patch.dict(
                os.environ, {"FACTORY_LEDGER": configured}, clear=False,
            ), self.assertRaisesRegex(CANCEL.CancelError, message):
                CANCEL.paths(self.root, "run-1")
        with mock.patch.dict(os.environ, {
            "FACTORY_LEDGER": str(ledger),
            "FACTORY_DURABLE_LEDGER": "relative.csv",
        }, clear=False), self.assertRaisesRegex(CANCEL.CancelError, "not absolute"):
            CANCEL.paths(self.root, "run-1")

    def test_stale_cleanup_preserves_a_replaced_dispatch_lease(self):
        lease = self.expired_lease()
        original = CANCEL.validate_stale_claims
        successor = {
            "claimed_epoch": int(time.time()),
            "expires_epoch": int(time.time()) + 900,
            "lease_id": "c" * 64,
            "schema_version": 1,
            "ticket": "T-1",
        }

        def replace_after_validation(*args):
            identity = original(*args)
            self.assertTrue((self.root / "factory/.launch.lock").is_dir())
            self.assertTrue(
                (self.root / "factory/.dispatch-leases.lock").is_dir()
            )
            replacement = lease.with_name("replacement.json")
            replacement.write_bytes(CANCEL.canonical(successor))
            os.replace(replacement, lease)
            return identity

        with mock.patch.object(
            CANCEL, "validate_stale_claims", side_effect=replace_after_validation,
        ), self.assertRaisesRegex(CANCEL.CancelError, "changed before cleanup"):
            CANCEL.release_stale_claims(
                self.root, self.root / "factory/.active-runs",
                {"ticket": "T-1", "role": "builder"}, int(time.time()),
            )
        self.assertEqual(json.loads(lease.read_text()), successor)
        self.assertFalse((self.root / "factory/.launch.lock").exists())
        self.assertFalse((self.root / "factory/.dispatch-leases.lock").exists())

    def test_stale_cleanup_refuses_a_lease_appearing_after_validation(self):
        lease = self.root / "factory/.dispatch-leases/T-1.json"
        original = CANCEL.validate_stale_claims

        def create_after_validation(*args):
            identity = original(*args)
            self.assertIsNone(identity)
            lease.parent.mkdir(exist_ok=True)
            lease.write_bytes(CANCEL.canonical({
                "claimed_epoch": int(time.time()),
                "expires_epoch": int(time.time()) + 900,
                "lease_id": "d" * 64,
                "schema_version": 1,
                "ticket": "T-1",
            }))
            return identity

        with mock.patch.object(
            CANCEL, "validate_stale_claims", side_effect=create_after_validation,
        ), self.assertRaisesRegex(CANCEL.CancelError, "appeared before cleanup"):
            CANCEL.release_stale_claims(
                self.root, self.root / "factory/.active-runs",
                {"ticket": "T-1", "role": "builder"}, int(time.time()),
            )
        self.assertTrue(lease.exists())
        self.assertFalse((self.root / "factory/.launch.lock").exists())
        self.assertFalse((self.root / "factory/.dispatch-leases.lock").exists())

    def test_stale_cleanup_refuses_malformed_or_writable_dispatch_lease(self):
        lease = self.expired_lease()
        valid = json.loads(lease.read_text())
        cases = (
            ({**valid, "lease_id": "foreign"}, 0o644),
            ({**valid, "claimed_epoch": True}, 0o644),
            ({**valid, "expires_epoch": 1}, 0o644),
            (valid, 0o666),
        )
        for value, mode in cases:
            with self.subTest(value=value, mode=oct(mode)):
                lease.write_bytes(CANCEL.canonical(value))
                lease.chmod(mode)
                with self.assertRaisesRegex(
                    CANCEL.CancelError, "dispatch lease is not expired",
                ):
                    CANCEL.validate_stale_claims(
                        self.root, self.root / "factory/.active-runs",
                        {"ticket": "T-1", "role": "builder"}, int(time.time()),
                    )

    def test_sealed_recovery_uses_held_admission_without_shared_locks(self):
        source_sha = "d" * 40
        manifest = {
            "contract_version": "2.0.0", "kit_sha": source_sha,
            "role": "builder", "ticket": "T-1",
        }
        admission = self.root.parent / ".dispatch-admission.lock"
        controller = self.root.parent / "reconcile.lock"
        admission.touch(mode=0o600)
        controller.touch(mode=0o600)
        descriptor = os.open(admission, os.O_RDWR)
        controller_descriptor = os.open(controller, os.O_RDWR)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        fcntl.flock(controller_descriptor, fcntl.LOCK_EX)
        try:
            with mock.patch.dict(os.environ, {
                "FACTORY_CROSS_RELEASE_SOURCE_SHA": source_sha,
                "FACTORY_CROSS_RELEASE_PRODUCT_ID": f"relay:{source_sha}",
                "FACTORY_DISPATCH_ADMISSION_LOCK": str(admission),
                "FACTORY_DISPATCH_ADMISSION_LOCK_FD": str(descriptor),
                "FACTORY_QUALIFICATION_CONTROLLER_LOCK": str(controller),
                "FACTORY_QUALIFICATION_CONTROLLER_LOCK_FD": str(
                    controller_descriptor
                ),
            }, clear=False):
                CANCEL.release_stale_claims(
                    self.root, self.root / "factory/.active-runs",
                    manifest, int(time.time()),
                )
        finally:
            os.close(descriptor)
            os.close(controller_descriptor)
        self.assertFalse((self.root / "factory/.launch.lock").exists())
        self.assertFalse((self.root / "factory/.dispatch-leases.lock").exists())

    def test_sealed_recovery_refuses_an_unheld_admission_lock(self):
        source_sha = "d" * 40
        admission = self.root.parent / ".dispatch-admission.lock"
        controller = self.root.parent / "reconcile.lock"
        admission.touch(mode=0o600)
        controller.touch(mode=0o600)
        descriptor = os.open(admission, os.O_RDWR)
        controller_descriptor = os.open(controller, os.O_RDWR)
        fcntl.flock(controller_descriptor, fcntl.LOCK_EX)
        try:
            with mock.patch.dict(os.environ, {
                "FACTORY_CROSS_RELEASE_SOURCE_SHA": source_sha,
                "FACTORY_CROSS_RELEASE_PRODUCT_ID": f"relay:{source_sha}",
                "FACTORY_DISPATCH_ADMISSION_LOCK": str(admission),
                "FACTORY_DISPATCH_ADMISSION_LOCK_FD": str(descriptor),
                "FACTORY_QUALIFICATION_CONTROLLER_LOCK": str(controller),
                "FACTORY_QUALIFICATION_CONTROLLER_LOCK_FD": str(
                    controller_descriptor
                ),
            }, clear=False), self.assertRaisesRegex(CANCEL.CancelError, "not held"):
                CANCEL.release_stale_claims(
                    self.root, self.root / "factory/.active-runs",
                    {
                        "contract_version": "2.0.0", "kit_sha": source_sha,
                        "role": "builder", "ticket": "T-1",
                    },
                    int(time.time()),
                )
        finally:
            os.close(descriptor)
            os.close(controller_descriptor)
        self.assertFalse((self.root / "factory/.launch.lock").exists())
        self.assertFalse((self.root / "factory/.dispatch-leases.lock").exists())

    def test_sealed_recovery_refuses_partial_or_mismatched_capability(self):
        source_sha = "d" * 40
        admission = self.root.parent / ".dispatch-admission.lock"
        other = self.root.parent / ".other-admission.lock"
        controller = self.root.parent / "reconcile.lock"
        admission.touch(mode=0o600)
        other.touch(mode=0o600)
        controller.touch(mode=0o600)
        descriptor = os.open(admission, os.O_RDWR)
        controller_descriptor = os.open(controller, os.O_RDWR)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        fcntl.flock(controller_descriptor, fcntl.LOCK_EX)
        manifest = {
            "contract_version": "2.0.0", "kit_sha": source_sha,
            "role": "builder", "ticket": "T-1",
        }
        try:
            with mock.patch.dict(os.environ, {
                "FACTORY_CROSS_RELEASE_SOURCE_SHA": source_sha,
            }, clear=True), self.assertRaisesRegex(CANCEL.CancelError, "incomplete"):
                CANCEL.sealed_recovery_locks_held(manifest)
            with mock.patch.dict(os.environ, {
                "FACTORY_CROSS_RELEASE_SOURCE_SHA": source_sha,
                "FACTORY_CROSS_RELEASE_PRODUCT_ID": f"relay:{source_sha}",
                "FACTORY_DISPATCH_ADMISSION_LOCK": str(other),
                "FACTORY_DISPATCH_ADMISSION_LOCK_FD": str(descriptor),
                "FACTORY_QUALIFICATION_CONTROLLER_LOCK": str(controller),
                "FACTORY_QUALIFICATION_CONTROLLER_LOCK_FD": str(
                    controller_descriptor
                ),
            }, clear=True), self.assertRaisesRegex(CANCEL.CancelError, "unsafe"):
                CANCEL.sealed_recovery_locks_held(manifest)
        finally:
            os.close(descriptor)
            os.close(controller_descriptor)

    def test_provider_database_selection_is_fail_closed(self):
        legacy = self.root.parent / "runtime/provider-state.sqlite3"
        attempt_id = self.submitted_provider_attempt(legacy)
        process, started = self.spawn()
        manifest = self.manifest(
            process, started, go="1", provider_attempt_id=attempt_id,
        )
        plan = CANCEL.calculate(
            self.root, "T-1", "run-1", "operator_requested", "0" * 32,
        )
        with mock.patch.dict(os.environ, {"FACTORY_PROVIDER_DB": ""}, clear=False):
            self.assertEqual(CANCEL.provider_database(self.root), legacy)

        missing = self.root.parent / "missing.sqlite3"
        with mock.patch.dict(
            os.environ, {"FACTORY_PROVIDER_DB": str(missing)}, clear=False,
        ), self.assertRaisesRegex(CANCEL.CancelError, "missing"):
            CANCEL.converge_provider_attempt(self.root, manifest, plan)

        linked = self.root.parent / "linked.sqlite3"
        linked.symlink_to(legacy)
        with mock.patch.dict(
            os.environ, {"FACTORY_PROVIDER_DB": str(linked)}, clear=False,
        ), self.assertRaisesRegex(CANCEL.CancelError, "unsafe"):
            CANCEL.converge_provider_attempt(self.root, manifest, plan)

        foreign = self.root.parent / "foreign.sqlite3"
        self.provider_command(foreign, "status")
        with mock.patch.dict(
            os.environ, {"FACTORY_PROVIDER_DB": str(foreign)}, clear=False,
        ), self.assertRaisesRegex(CANCEL.CancelError, "reconciliation failed"):
            CANCEL.converge_provider_attempt(self.root, manifest, plan)

        for field in (
            "provider_product_id", "provider_family", "account_route_id",
            "activation_policy_sha256", "go_issued", "task_submitted",
        ):
            changed = dict(manifest)
            changed[field] = "0" if field in {"go_issued", "task_submitted"} else "wrong"
            with self.subTest(provider_identity=field), mock.patch.dict(
                os.environ, {"FACTORY_PROVIDER_DB": str(legacy)}, clear=False,
            ), self.assertRaisesRegex(CANCEL.CancelError, "identity disagrees"):
                CANCEL.converge_provider_attempt(self.root, changed, plan)

        with mock.patch.dict(os.environ, {"FACTORY_PROVIDER_DB": ""}, clear=False):
            CANCEL.converge_provider_attempt(self.root, manifest, plan)
        terminal = self.provider_command(
            legacy, "status", "--attempt-id", attempt_id,
        )["attempts"][0]
        self.assertEqual(
            (terminal["state"], terminal["terminal_result"], terminal["charge_micro_usd"]),
            ("terminal", "cancelled", 2_000_000),
        )

    def test_stale_process_converges_without_signalling_or_replay(self):
        process, started = self.spawn()
        self.manifest(process, started, go="1")
        process.terminate()
        process.wait(timeout=5)
        claim = self.root / "factory/.active-runs/T-1.builder.lock"
        claim.mkdir(parents=True)
        (claim / "owner").write_text(
            f"pid={process.pid}\nprocess_start={started}\ntoken={'a' * 32}\n"
        )
        leases = self.root / "factory/.dispatch-leases"
        leases.mkdir()
        (leases / "T-1.json").write_bytes(CANCEL.canonical({
            "claimed_epoch": 1,
            "expires_epoch": 2,
            "lease_id": "b" * 64,
            "schema_version": 1,
            "ticket": "T-1",
        }))
        plan = CANCEL.calculate(
            self.root, "T-1", "run-1", "operator_requested", "e" * 32,
        )
        receipt = CANCEL.apply_plan(self.root, plan, 1)
        manifest = IDENTITY.parse_fields(
            (self.runs / "run-1.meta").read_bytes(), "run manifest",
        )
        self.assertEqual(
            (receipt["accounting_state"], receipt["charged_usd"]),
            ("cancelled_conservative", "2.00"),
        )
        self.assertEqual(
            (manifest["phase"], manifest["role_exit"]),
            ("cancelled_conservative", "cancelled"),
        )
        self.assertFalse((self.runs / "run-1.pid").exists())
        self.assertFalse(claim.exists())
        self.assertFalse((leases / "T-1.json").exists())
        self.assertEqual(
            CANCEL.ledger_row(
                self.root / "factory/runtime-ledger.csv", "run-1",
            )["exit_status"],
            "130",
        )
        self.assertEqual(CANCEL.apply_plan(self.root, plan, 1), receipt)


if __name__ == "__main__":
    unittest.main()
