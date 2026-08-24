#!/usr/bin/env python3
"""Regression tests for targeted attempt cancellation and process ownership."""

import csv
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
            "--product-id", "qualification:factory", "--ticket-id", "T-1",
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
