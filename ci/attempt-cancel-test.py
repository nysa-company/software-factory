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
        self.root = Path(self.temp.name) / "product"
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

    def manifest(self, process, started, *, ticket="T-1", go="0"):
        run_id = "run-1"
        values = {
            "run_id": run_id,
            "phase": "prepared" if go == "0" else "spawned",
            "accounting_schema": "1",
            "accounting_state": "reserved",
            "reserved_usd": "2.00",
            "go_issued": go,
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

    def test_sandbox_start_identity_matches_host_and_foreground_timeout_group(self):
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


if __name__ == "__main__":
    unittest.main()
