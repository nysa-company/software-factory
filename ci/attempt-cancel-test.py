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
from types import SimpleNamespace
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
COORDINATOR = ROOT / "scripts/provider-coordinator.py"
DOCTOR = ROOT / "scripts/factory-doctor.sh"


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

    def test_orphaned_submitted_wrapper_cancels_once_and_preserves_sibling(self):
        database = self.root.parent / "qualification/provider/accounting/state-v2.sqlite3"
        source_sha = "d" * 40
        (self.root / "factory/KIT_PIN").write_text(f"{source_sha}\n")
        (self.root / "factory/PROJECT.env").write_text(
            "MAX_CONCURRENT_TICKETS=3\n"
        )
        attempt_id = self.submitted_provider_attempt(
            database, attempt_id="run-1-cli",
        )
        provider, provider_start = self.spawn()
        values = self.manifest(
            provider, provider_start, go="1", provider_attempt_id=attempt_id,
        )
        values.update({
            "contract_version": "2.0.0", "kit_sha": source_sha,
            "submitted_at_epoch_ns": "", "task_submitted": "0",
            "ticket_kit_sha": source_sha,
        })
        self.write_meta(values)

        wrapper, wrapper_start = self.spawn()
        claim = self.stale_claim(
            self.root / "factory/.active-runs", wrapper, wrapper_start,
        )
        sibling_claim = claim.parent / "T-2.reviewer.lock"
        sibling_claim.mkdir()
        sibling_owner = (
            f"pid={wrapper.pid}\nprocess_start={wrapper_start}\n"
            f"token={'b' * 32}\n"
        ).encode()
        (sibling_claim / "owner").write_bytes(sibling_owner)
        wrapper.terminate()
        wrapper.wait(timeout=5)
        heartbeat, heartbeat_start = self.spawn()
        sibling, _sibling_start = self.spawn()
        wrapper_record = self.runs / "run-1.wrapper"
        wrapper_record.write_text(
            f"run_id=run-1\nwrapper_pid={wrapper.pid}\n"
            f"wrapper_process_start={wrapper_start}\n"
            f"heartbeat_pid={heartbeat.pid}\nheartbeat_pgid={heartbeat.pid}\n"
            f"heartbeat_process_start={heartbeat_start}\n"
        )
        for suffix in ("ready", "go", "gate"):
            (self.runs / f".run-1.{suffix}").touch()
        (self.runs / ".run-1.submitted").write_text(
            f"pid={provider.pid}\nsubmitted_at_epoch_ns=103500000000\n"
        )
        leases = self.root / "factory/.dispatch-leases"
        leases.mkdir()
        now = int(time.time())
        selected_lease = {
            "claimed_epoch": now - 1,
            "expires_epoch": now + 900,
            "lease_id": "b" * 64,
            "schema_version": 1,
            "ticket": "T-1",
        }
        sibling_lease = {
            "claimed_epoch": now - 1,
            "expires_epoch": now + 900,
            "lease_id": "c" * 64,
            "schema_version": 1,
            "ticket": "T-2",
        }
        (leases / "T-1.json").write_bytes(CANCEL.canonical(selected_lease))
        (leases / "T-2.json").write_bytes(CANCEL.canonical(sibling_lease))
        runtime_root = self.root.parent / "qualification"
        selected_runtime = runtime_root / f"attempts/{attempt_id}"
        selected_runtime.mkdir(parents=True)
        (selected_runtime / "owner").write_text(f"{attempt_id}\n")
        os.mkfifo(selected_runtime / "provider.pipe")
        sibling_runtime = runtime_root / "attempts/sibling-cli"
        sibling_runtime.mkdir()
        (sibling_runtime / "owner").write_text("sibling-cli\n")
        admission = self.root.parent / ".dispatch-admission.lock"
        controller = self.root.parent / "reconcile.lock"
        admission.touch(mode=0o600)
        controller.touch(mode=0o600)
        descriptor = os.open(admission, os.O_RDWR)
        controller_descriptor = os.open(controller, os.O_RDWR)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        fcntl.flock(controller_descriptor, fcntl.LOCK_EX)
        environment = {
            "FACTORY_CLI_RUNTIME_ROOT": str(runtime_root),
            "FACTORY_CROSS_RELEASE_PRODUCT_ID": f"qualification:{source_sha}",
            "FACTORY_CROSS_RELEASE_SOURCE_SHA": source_sha,
            "FACTORY_DISPATCH_ADMISSION_LOCK": str(admission),
            "FACTORY_DISPATCH_ADMISSION_LOCK_FD": str(descriptor),
            "FACTORY_PROVIDER_DB": str(database),
            "FACTORY_QUALIFICATION_CONTROLLER_LOCK": str(controller),
            "FACTORY_QUALIFICATION_CONTROLLER_LOCK_FD": str(
                controller_descriptor
            ),
        }
        try:
            with mock.patch.dict(os.environ, environment, clear=False):
                plan = CANCEL.calculate(
                    self.root, "T-1", "run-1", "operator_requested", "9" * 32,
                )
                self.assertEqual(plan["schema"], CANCEL.ORPHAN_PLAN_SCHEMA)
                original_manifest = (self.runs / "run-1.meta").read_bytes()
                submitted = self.provider_command(
                    database, "status", "--attempt-id", attempt_id,
                )["attempts"][0]
                values.update({
                    "task_submitted": "1",
                    "submitted_at_epoch_ns": str(
                        CANCEL.canonical_submission_ns(submitted)
                    ),
                })
                self.write_meta(values)
                submitted_plan = CANCEL.calculate(
                    self.root, "T-1", "run-1", "operator_requested", "8" * 32,
                )
                self.assertEqual(
                    submitted_plan["schema"], CANCEL.ORPHAN_PLAN_SCHEMA,
                )
                values["submitted_at_epoch_ns"] = str(
                    int(values["submitted_at_epoch_ns"]) - 1
                )
                self.write_meta(values)
                with self.assertRaisesRegex(
                    CANCEL.CancelError, "disagrees with the run manifest",
                ):
                    CANCEL.calculate(
                        self.root, "T-1", "run-1", "operator_requested", "7" * 32,
                    )
                (self.runs / "run-1.meta").write_bytes(original_manifest)
                saved_claim = claim.with_name("saved-builder.lock")
                claim.rename(saved_claim)
                claim.mkdir()
                (claim / "owner").write_bytes((saved_claim / "owner").read_bytes())
                with self.assertRaisesRegex(
                    CANCEL.CancelError, "active-run claim changed",
                ):
                    CANCEL.apply_plan(self.root, plan, 1)
                shutil.rmtree(claim)
                saved_claim.rename(claim)
                selected_lease["expires_epoch"] += 300
                (leases / "T-1.json").write_bytes(
                    CANCEL.canonical(selected_lease)
                )
                self.assertEqual(
                    CANCEL.calculate(
                        self.root, "T-1", "run-1", "operator_requested", "9" * 32,
                    ),
                    plan,
                )
                original_wrapper = wrapper_record.read_bytes()
                wrapper_record.write_bytes(original_wrapper + b"changed=1\n")
                with self.assertRaisesRegex(CANCEL.CancelError, "wrapper changed"):
                    CANCEL.apply_plan(self.root, plan, 1)
                self.assertEqual(
                    self.provider_command(
                        database, "status", "--attempt-id", attempt_id,
                    )["attempts"][0]["state"],
                    "submitted",
                )
                self.assertTrue(IDENTITY.group_alive(provider.pid))
                self.assertTrue(IDENTITY.group_alive(heartbeat.pid))
                wrapper_record.write_bytes(original_wrapper)
                provider_reaper = threading.Thread(target=provider.wait)
                heartbeat_reaper = threading.Thread(target=heartbeat.wait)
                provider_reaper.start()
                heartbeat_reaper.start()
                original_converge = CANCEL.converge_provider_attempt
                failed = False

                def fail_once(*args):
                    nonlocal failed
                    if not failed:
                        failed = True
                        raise CANCEL.CancelError("injected recovery interruption")
                    return original_converge(*args)

                with mock.patch.object(
                    CANCEL, "converge_provider_attempt", side_effect=fail_once,
                ):
                    with self.assertRaisesRegex(
                        CANCEL.CancelError, "injected recovery interruption",
                    ):
                        CANCEL.apply_plan(self.root, plan, 1)
                    self.assertFalse(IDENTITY.group_alive(heartbeat.pid))
                    self.assertFalse((leases / "T-1.json").exists())
                    self.assertTrue(selected_runtime.exists())
                    self.assertTrue(
                        (self.runs / "run-1.cancel-request.json").exists()
                    )
                    (self.root / ".gitignore").write_text("factory/\n")
                    (self.root / "factory/initiatives").mkdir()
                    (self.root / "factory/tickets").mkdir()
                    (self.root / "factory/PROJECT.env").write_text(
                        "MAX_CONCURRENT_TICKETS=3\nTEST_PATHS=tests\n"
                    )
                    qualification_manifest = self.root / "factory/QUALIFICATION.json"
                    qualification_manifest.write_text(json.dumps({
                        "budget_usd": "100.000000",
                        "capacity": 3,
                        "contract_version": "2.0.0",
                        "factory_sha": source_sha,
                        "generation": 1,
                        "per_run_budget_usd": "2.000000",
                        "per_ticket_budget_usd": "25.000000",
                        "schema": "nysa.software-factory.qualification/v2",
                        "target_done": 1,
                        "tickets": ["T-1"],
                    }))
                    (self.root / "factory/initiatives/I-1.md").write_text(
                        "# Test initiative\n"
                    )
                    (self.root / "factory/tickets/T-1.md").write_text(
                        "State: Building\nPriority: normal\nInitiative: I-1\n"
                        "Depends-On: none\nProduct-Decisions: frozen\n"
                        "Builder ownership: src/app.txt only\n"
                        "Fixture-Seams: none\nAuthentication-Seams: none\n"
                        "Protected-Test-Conflicts: none\n"
                        f"Kit-SHA: {source_sha}\n"
                    )
                    subprocess.run(
                        ["git", "init", "-q", str(self.root)], check=True,
                    )
                    subprocess.run(
                        ["git", "-C", str(self.root), "config", "user.name", "Test"],
                        check=True,
                    )
                    subprocess.run(
                        [
                            "git", "-C", str(self.root), "config", "user.email",
                            "test@example.invalid",
                        ],
                        check=True,
                    )
                    subprocess.run(
                        ["git", "-C", str(self.root), "add", ".gitignore"],
                        check=True,
                    )
                    subprocess.run(
                        [
                            "git", "-C", str(self.root), "add", "-f",
                            "factory/KIT_PIN", "factory/PROJECT.env",
                            "factory/QUALIFICATION.json", "factory/initiatives/I-1.md",
                            "factory/tickets/T-1.md",
                        ],
                        check=True,
                    )
                    subprocess.run(
                        ["git", "-C", str(self.root), "commit", "-qm", "fixture"],
                        check=True,
                    )
                    product_sha = subprocess.run(
                        ["git", "-C", str(self.root), "rev-parse", "HEAD"],
                        text=True, capture_output=True, check=True,
                    ).stdout.strip()
                    product_tree = subprocess.run(
                        ["git", "-C", str(self.root), "rev-parse", "HEAD^{tree}"],
                        text=True, capture_output=True, check=True,
                    ).stdout.strip()
                    doctor_environment = {
                        **environment,
                        "FACTORY_DOCTOR_TIMEOUT_SECONDS": "1",
                        "FACTORY_KIT_TRUST_SCOPE": "qualification-candidate",
                        "FACTORY_QUALIFICATION_PRODUCT_SHA": product_sha,
                        "FACTORY_QUALIFICATION_PRODUCT_TREE": product_tree,
                        "FACTORY_QUALIFICATION_MANIFEST": str(
                            qualification_manifest
                        ),
                        "FACTORY_RELEASE_CONTRACT_VERSION": "2.0.0",
                    }
                    doctor = subprocess.run(
                        [
                            str(DOCTOR), "--json", "--kit-dir", str(ROOT),
                            "--product-root", str(self.root),
                            "--kit-sha", source_sha,
                        ],
                        text=True,
                        capture_output=True,
                        check=False,
                        env={
                            **os.environ,
                            **doctor_environment,
                        },
                        timeout=30,
                    )
                    diagnosed = next(
                        item for item in json.loads(doctor.stdout)["checks"]
                        ["runtime"]["runs"] if item["run_id"] == "run-1"
                    )
                    self.assertEqual(
                        (
                            diagnosed["state"], diagnosed["ticket"],
                            diagnosed["recovery_command"],
                            diagnosed["recovery_reason"],
                        ),
                        (
                            "stale", "T-1", "qualification recover-plan",
                            "orphaned_cli_wrapper",
                        ),
                    )
                    drifted_doctor = subprocess.run(
                        [
                            str(DOCTOR), "--json", "--kit-dir", str(ROOT),
                            "--product-root", str(self.root),
                            "--kit-sha", source_sha,
                        ],
                        text=True,
                        capture_output=True,
                        check=False,
                        env={
                            **os.environ,
                            **doctor_environment,
                            "FACTORY_QUALIFICATION_PRODUCT_SHA": "e" * 40,
                        },
                        timeout=30,
                    )
                    drifted = next(
                        item for item in json.loads(drifted_doctor.stdout)["checks"]
                        ["runtime"]["runs"] if item["run_id"] == "run-1"
                    )
                    self.assertEqual(
                        (drifted["recovery_command"], drifted["recovery_reason"]),
                        (None, "qualification_identity_invalid"),
                    )
                    foreign_manifest = self.root.parent / "foreign.json"
                    foreign_manifest.write_text("{}\n")
                    foreign_doctor = subprocess.run(
                        [
                            str(DOCTOR), "--json", "--kit-dir", str(ROOT),
                            "--product-root", str(self.root),
                            "--kit-sha", source_sha,
                        ],
                        text=True,
                        capture_output=True,
                        check=False,
                        env={
                            **os.environ,
                            **doctor_environment,
                            "FACTORY_QUALIFICATION_MANIFEST": str(foreign_manifest),
                        },
                        timeout=30,
                    )
                    foreign = next(
                        item for item in json.loads(foreign_doctor.stdout)["checks"]
                        ["runtime"]["runs"] if item["run_id"] == "run-1"
                    )
                    self.assertEqual(
                        (foreign["recovery_command"], foreign["recovery_reason"]),
                        (None, "qualification_identity_invalid"),
                    )
                    with mock.patch.object(
                        CANCEL, "remove_bound_tree",
                        side_effect=CANCEL.CancelError(
                            "injected runtime cleanup interruption"
                        ),
                    ):
                        with self.assertRaisesRegex(
                            CANCEL.CancelError,
                            "injected runtime cleanup interruption",
                        ):
                            CANCEL.apply_plan(self.root, plan, 1)
                    quarantine = selected_runtime.with_name(
                        f".{attempt_id}.cancel-{plan['preview_hash'][:24]}"
                    )
                    self.assertFalse(selected_runtime.exists())
                    self.assertTrue(quarantine.exists())
                    original_remove = CANCEL.remove_bound_tree
                    interrupted_claim = False

                    def fail_claim_once(path, device, inode):
                        nonlocal interrupted_claim
                        if ".claim-" in path.name and not interrupted_claim:
                            interrupted_claim = True
                            raise CANCEL.CancelError(
                                "injected claim cleanup interruption"
                            )
                        return original_remove(path, device, inode)

                    with mock.patch.object(
                        CANCEL, "remove_bound_tree", side_effect=fail_claim_once,
                    ):
                        with self.assertRaisesRegex(
                            CANCEL.CancelError,
                            "injected claim cleanup interruption",
                        ):
                            CANCEL.apply_plan(self.root, plan, 1)
                    claim_quarantine = claim.with_name(
                        f".{claim.name}.claim-{plan['preview_hash'][:24]}"
                    )
                    self.assertFalse(claim.exists())
                    self.assertTrue(claim_quarantine.exists())
                    (claim_quarantine / "owner").unlink()
                    receipt = CANCEL.apply_plan(self.root, plan, 1)
                provider_reaper.join(timeout=5)
                heartbeat_reaper.join(timeout=5)
                self.assertEqual(CANCEL.apply_plan(self.root, plan, 1), receipt)
                self.assertEqual(
                    CANCEL.calculate(
                        self.root, "T-1", "run-1", "operator_requested", None,
                    ),
                    plan,
                )
            with self.assertRaisesRegex(
                CANCEL.CancelError, "sealed qualification recovery",
            ):
                CANCEL.apply_plan(self.root, plan, 1)
        finally:
            os.close(descriptor)
            os.close(controller_descriptor)

        terminal = self.provider_command(
            database, "status", "--attempt-id", attempt_id,
        )["attempts"][0]
        manifest = IDENTITY.parse_fields(
            (self.runs / "run-1.meta").read_bytes(), "run manifest",
        )
        self.assertEqual(
            (
                receipt["accounting_state"], receipt["charged_usd"],
                terminal["state"], terminal["terminal_result"],
                terminal["charge_micro_usd"], terminal["version"],
                manifest["task_submitted"], manifest["submitted_at_epoch_ns"],
            ),
            (
                "cancelled_conservative", "2.00", "terminal", "cancelled",
                2_000_000, 5, "1", "102999999999",
            ),
        )
        self.assertTrue(IDENTITY.group_alive(sibling.pid))
        self.assertEqual(json.loads((leases / "T-2.json").read_text()), sibling_lease)
        self.assertFalse(claim.exists())
        self.assertEqual((sibling_claim / "owner").read_bytes(), sibling_owner)
        self.assertFalse((leases / "T-1.json").exists())
        self.assertFalse(wrapper_record.exists())
        self.assertFalse(selected_runtime.exists())
        self.assertTrue(sibling_runtime.exists())
        for suffix in ("pid", "ready", "go", "gate", "submitted"):
            name = f"run-1.{suffix}" if suffix == "pid" else f".run-1.{suffix}"
            self.assertFalse((self.runs / name).exists())
        self.assertFalse((self.root / "factory/.launch.lock").exists())
        self.assertFalse((self.root / "factory/.dispatch-leases.lock").exists())

    def test_orphan_runtime_swap_after_preview_never_deletes_replacement(self):
        runtime_root = self.root.parent / "runtime"
        runtime = runtime_root / "attempts/run-1-cli"
        runtime.mkdir(parents=True)
        (runtime / "owner").write_text("run-1-cli\n")
        replacement = runtime.with_name("replacement")
        replacement.mkdir()
        (replacement / "keep").write_text("replacement\n")
        sibling = runtime.with_name("sibling")
        sibling.mkdir()
        (sibling / "keep").write_text("sibling\n")
        info = runtime.lstat()
        plan = {
            "pgid": 99999999,
            "preview_hash": "a" * 64,
            "provider_attempt": {"attempt_id": "run-1-cli"},
            "runtime": {
                "device": info.st_dev,
                "inode": info.st_ino,
                "owner_sha256": CANCEL.digest((runtime / "owner").read_bytes()),
                "path": str(runtime),
            },
            "schema": CANCEL.ORPHAN_PLAN_SCHEMA,
        }
        original_rename = os.rename
        saved = runtime.with_name("saved")

        def swap_then_rename(source, target, *args, **kwargs):
            original_rename(runtime, saved)
            original_rename(replacement, runtime)
            return original_rename(source, target, *args, **kwargs)

        with mock.patch.dict(
            os.environ, {"FACTORY_CLI_RUNTIME_ROOT": str(runtime_root)}, clear=False,
        ), mock.patch.object(CANCEL.os, "rename", side_effect=swap_then_rename):
            with self.assertRaisesRegex(CANCEL.CancelError, "runtime changed"):
                CANCEL.cleanup_orphan_runtime(plan)
        quarantine = runtime.with_name(
            f".{runtime.name}.cancel-{plan['preview_hash'][:24]}"
        )
        self.assertEqual((quarantine / "keep").read_text(), "replacement\n")
        self.assertEqual((saved / "owner").read_text(), "run-1-cli\n")
        self.assertEqual((sibling / "keep").read_text(), "sibling\n")

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
            plan.pop("active_claim")
            plan.pop("preview_hash")
            plan["preview_hash"] = CANCEL.digest(CANCEL.canonical(plan))
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

    def test_provider_only_dead_claim_requires_sealed_recovery_and_replays(self):
        database = self.root.parent / "qualification/provider/accounting/state-v2.sqlite3"
        release = "d" * 40
        process, started = self.spawn()
        attempt_id = f"1787640905-{process.pid}-cli"
        self.submitted_provider_attempt(
            database, attempt_id=attempt_id, submit=False,
            product_id=f"relay-proof:{release}",
        )
        authority_ledger = self.root.parent / "authority/operator/runtime-ledger.csv"
        authority_ledger.parent.mkdir(parents=True)
        authority_ledger.write_text(LEDGER_HEADER)
        claim = self.stale_claim(authority_ledger.parent / ".active-runs", process, started)
        environment = {
            "FACTORY_LEDGER": str(authority_ledger),
            "FACTORY_PROJECT": "relay-proof",
            "FACTORY_PROVIDER_DB": str(database),
            "FACTORY_PROVIDER_PRODUCT_ID": f"relay-proof:{release}",
            "FACTORY_RELEASE_SHA": release,
        }
        with mock.patch.dict(os.environ, environment, clear=False), \
                self.assertRaisesRegex(CANCEL.CancelError, "still alive"):
            CANCEL.calculate(
                self.root, "T-1", attempt_id, "operator_requested", None,
            )
        process.terminate()
        process.wait(timeout=5)
        admission = self.root.parent / ".dispatch-admission.lock"
        controller = self.root.parent / "reconcile.lock"
        admission.touch(mode=0o600)
        controller.touch(mode=0o600)
        descriptors = (os.open(admission, os.O_RDWR), os.open(controller, os.O_RDWR))
        for descriptor in descriptors:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
        sealed = {
            **environment,
            "FACTORY_CROSS_RELEASE_SOURCE_SHA": release,
            "FACTORY_CROSS_RELEASE_PRODUCT_ID": f"relay-proof:{release}",
            "FACTORY_DISPATCH_ADMISSION_LOCK": str(admission),
            "FACTORY_DISPATCH_ADMISSION_LOCK_FD": str(descriptors[0]),
            "FACTORY_QUALIFICATION_CONTROLLER_LOCK": str(controller),
            "FACTORY_QUALIFICATION_CONTROLLER_LOCK_FD": str(descriptors[1]),
        }
        try:
            with mock.patch.dict(os.environ, environment, clear=False):
                plan = CANCEL.calculate(
                    self.root, "T-1", attempt_id, "operator_requested", None,
                )
                with self.assertRaisesRegex(CANCEL.CancelError, "qualification recovery"):
                    CANCEL.apply_plan(self.root, plan, 1)
                self.assertFalse(
                    (self.runs / f"{attempt_id}.cancel-request.json").exists()
                )
            with mock.patch.dict(os.environ, sealed, clear=False):
                with self.assertRaisesRegex(CANCEL.CancelError, "qualification recovery"):
                    CANCEL.apply_plan(self.root, plan, 1)
                owner = claim / "owner"
                original_owner = owner.read_bytes()
                owner.write_text(
                    f"pid={process.pid}\nprocess_start={started}\ntoken={'c' * 32}\n"
                )
                with self.assertRaisesRegex(CANCEL.CancelError, "changed"):
                    CANCEL.prepare_provider_only_recovery(self.root, plan)
                owner.write_bytes(original_owner)
                replacement = claim / "replacement"
                replacement.write_bytes(original_owner)
                os.replace(replacement, owner)
                with self.assertRaisesRegex(CANCEL.CancelError, "changed"):
                    CANCEL.prepare_provider_only_recovery(self.root, plan)
                plan = CANCEL.calculate(
                    self.root, "T-1", attempt_id, "operator_requested", None,
                )
                original_rmdir = Path.rmdir
                interrupted = False

                def fail_after_owner_unlink(path):
                    nonlocal interrupted
                    if (
                        not interrupted
                        and path.name.startswith(".provider-only-cancel-")
                    ):
                        interrupted = True
                        raise OSError("simulated interruption")
                    return original_rmdir(path)

                with mock.patch.object(Path, "rmdir", fail_after_owner_unlink), \
                        self.assertRaisesRegex(OSError, "simulated interruption"):
                    CANCEL.prepare_provider_only_recovery(self.root, plan)
                recovery = claim.parent / CANCEL.provider_only_recovery_name(plan)
                recovery.chmod(0o777)
                with self.assertRaisesRegex(CANCEL.CancelError, "unsafe"):
                    CANCEL.prepare_provider_only_recovery(self.root, plan)
                recovery.chmod(0o700)
                claim.parent.chmod(0o777)
                with self.assertRaisesRegex(CANCEL.CancelError, "unsafe"):
                    CANCEL.prepare_provider_only_recovery(self.root, plan)
                claim.parent.chmod(0o755)
                CANCEL.prepare_provider_only_recovery(self.root, plan)
                receipt = CANCEL.apply_plan(self.root, plan, 1)
                self.assertEqual(CANCEL.apply_plan(self.root, plan, 1), receipt)
        finally:
            for descriptor in descriptors:
                os.close(descriptor)
        self.assertFalse(claim.exists())
        self.assertEqual(
            self.provider_command(
                database, "status", "--attempt-id", attempt_id,
            )["attempts"][0]["terminal_result"],
            "failed_pre_go",
        )

    def test_provider_only_claim_identity_fails_closed_on_edge_cases(self):
        process, started = self.spawn()
        active = self.root.parent / "authority/.active-runs"
        claim = self.stale_claim(active, process, started)
        process.terminate()
        process.wait(timeout=5)
        identity = CANCEL.provider_only_claim_identity(active, "T-1")
        self.assertIsNotNone(identity)
        with mock.patch.object(
            CANCEL.IDENTITY, "process_table",
            return_value={process.pid: SimpleNamespace(started=f"{started}-reused")},
        ):
            self.assertEqual(
                CANCEL.provider_only_claim_identity(active, "T-1"), identity,
            )
        sibling = active / "T-2.builder.lock"
        sibling.mkdir()
        (sibling / "owner").write_text(
            f"pid={process.pid}\nprocess_start={started}\ntoken={'b' * 32}\n"
        )
        with self.assertRaisesRegex(CANCEL.CancelError, "sibling"):
            CANCEL.provider_only_claim_identity(active, "T-1")
        shutil.rmtree(sibling)
        (claim / "owner").write_text(f"pid={process.pid}\n")
        with self.assertRaisesRegex(CANCEL.CancelError, "malformed"):
            CANCEL.provider_only_claim_identity(active, "T-1")

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

    def test_cross_release_recovers_legacy_submitted_manifest(self):
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
        values.update({
            "contract_version": "2.0.0", "kit_sha": source_sha,
            "provider_product_id": product_id, "submitted_at_epoch_ns": "",
            "task_submitted": "0", "ticket_kit_sha": source_sha,
        })
        self.write_meta(values)
        plan = CANCEL.calculate(
            self.root, "T-1", "run-1", "operator_requested", "8" * 32,
        )
        wrapper = self.runs / "run-1.wrapper"
        submitted = self.runs / ".run-1.submitted"
        self.assertFalse(wrapper.exists() or wrapper.is_symlink())
        self.assertFalse(submitted.exists() or submitted.is_symlink())
        process.kill()
        process.wait(timeout=5)
        with mock.patch.dict(
            os.environ, {"FACTORY_PROVIDER_DB": str(database)}, clear=False,
        ), self.assertRaisesRegex(CANCEL.CancelError, "identity disagrees"):
            CANCEL.apply_plan(self.root, plan, 1)
        admission = self.root.parent / ".dispatch-admission.lock"
        controller = self.root.parent / "reconcile.lock"
        runtime_root = self.root.parent / "provider-runtime"
        admission.touch(mode=0o600)
        controller.touch(mode=0o600)
        descriptors = (os.open(admission, os.O_RDWR), os.open(controller, os.O_RDWR))
        for descriptor in descriptors:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
        try:
            with mock.patch.dict(os.environ, {
                "FACTORY_PROVIDER_DB": str(database),
                "FACTORY_CLI_RUNTIME_ROOT": str(runtime_root),
                "FACTORY_CROSS_RELEASE_PRODUCT_ID": product_id,
                "FACTORY_CROSS_RELEASE_SOURCE_SHA": source_sha,
                "FACTORY_DISPATCH_ADMISSION_LOCK": str(admission),
                "FACTORY_DISPATCH_ADMISSION_LOCK_FD": str(descriptors[0]),
                "FACTORY_QUALIFICATION_CONTROLLER_LOCK": str(controller),
                "FACTORY_QUALIFICATION_CONTROLLER_LOCK_FD": str(descriptors[1]),
            }, clear=False):
                missing_timestamp = dict(values)
                missing_timestamp.pop("submitted_at_epoch_ns")
                fingerprints = {
                    "missing timestamp": missing_timestamp,
                    "nonblank timestamp": {
                        **values, "submitted_at_epoch_ns": "1",
                    },
                    "submitted manifest": {**values, "task_submitted": "1"},
                    "pre-GO manifest": {**values, "go_issued": "0"},
                    "wrong contract": {**values, "contract_version": "1.8.0"},
                    "wrong source": {**values, "kit_sha": "e" * 40},
                    "wrong ticket source": {
                        **values, "ticket_kit_sha": "e" * 40,
                    },
                    "wrong product": {
                        **values, "provider_product_id": f"foreign:{source_sha}",
                    },
                }
                for label, fingerprint in fingerprints.items():
                    with self.subTest(fingerprint=label), self.assertRaisesRegex(
                        CANCEL.CancelError, "identity disagrees",
                    ):
                        CANCEL.converge_provider_attempt(
                            self.root, fingerprint, plan,
                        )
                provider = self.provider_command(
                    database, "status", "--attempt-id", attempt_id,
                )["attempts"][0]
                terminal = {
                    **provider, "charge_micro_usd": provider["reserve_micro_usd"],
                    "state": "terminal", "terminal_at": 103,
                    "terminal_result": "cancelled", "updated_at": 103,
                    "version": 5,
                }
                provider_drift = {
                    "submitted version": {**provider, "version": 5},
                    "terminal version": {**terminal, "version": 6},
                    "terminal result": {**terminal, "terminal_result": "failed"},
                    "terminal charge": {
                        **terminal,
                        "charge_micro_usd": provider["reserve_micro_usd"] - 1,
                    },
                    "terminal timestamp": {**terminal, "updated_at": 104},
                    "ticket": {**provider, "ticket_id": "T-2"},
                    "family": {**provider, "provider_family": "anthropic"},
                    "route": {**provider, "account_route": "other"},
                    "policy": {**provider, "policy_sha256": "f" * 64},
                    "reserve": {
                        **provider,
                        "reserve_micro_usd": provider["reserve_micro_usd"] + 1,
                    },
                    "submission chronology": {
                        **provider, "go_at": provider["submitted_at"] + 1,
                    },
                }
                for label, drifted in provider_drift.items():
                    result = subprocess.CompletedProcess(
                        [], 0, json.dumps({"attempts": [drifted]}), "",
                    )
                    with self.subTest(provider=label), mock.patch.object(
                        CANCEL.subprocess, "run", return_value=result,
                    ) as runner, self.assertRaisesRegex(
                        CANCEL.CancelError, "identity disagrees|changed",
                    ):
                        CANCEL.converge_provider_attempt(self.root, values, plan)
                    self.assertEqual(runner.call_count, 1)
                wrapper.write_text("unexpected\n")
                with self.assertRaisesRegex(CANCEL.CancelError, "identity disagrees"):
                    CANCEL.apply_plan(self.root, plan, 1)
                wrapper.unlink()
                submitted.symlink_to(self.runs / "run-1.meta")
                with self.assertRaisesRegex(
                    CANCEL.CancelError, "unsafe stale attempt record",
                ):
                    CANCEL.apply_plan(self.root, plan, 1)
                submitted.unlink()
                provider_runtime = runtime_root / "attempts" / attempt_id
                provider_runtime.mkdir(parents=True)
                (provider_runtime / "owner").write_text(f"{attempt_id}\n")
                with self.assertRaisesRegex(CANCEL.CancelError, "identity disagrees"):
                    CANCEL.apply_plan(self.root, plan, 1)
                (provider_runtime / "owner").unlink()
                provider_runtime.rmdir()
                with mock.patch.object(
                    CANCEL, "replace_fields", side_effect=OSError("crash"),
                ), self.assertRaisesRegex(OSError, "crash"):
                    CANCEL.apply_plan(self.root, plan, 1)
                interrupted = self.provider_command(
                    database, "status", "--attempt-id", attempt_id,
                )["attempts"][0]
                interrupted_manifest = IDENTITY.parse_fields(
                    (self.runs / "run-1.meta").read_bytes(), "run manifest",
                )
                self.assertFalse((self.runs / "run-1.cancel.json").exists())
                self.assertEqual(
                    (
                        interrupted_manifest["task_submitted"],
                        interrupted_manifest["submitted_at_epoch_ns"],
                    ),
                    ("0", ""),
                )
                receipt = CANCEL.apply_plan(self.root, plan, 1)
                terminal = self.provider_command(
                    database, "status", "--attempt-id", attempt_id,
                )["attempts"][0]
                replay = CANCEL.apply_plan(self.root, plan, 1)
        finally:
            for descriptor in descriptors:
                os.close(descriptor)
        manifest = IDENTITY.parse_fields(
            (self.runs / "run-1.meta").read_bytes(), "run manifest",
        )
        self.assertEqual(receipt, replay)
        self.assertEqual(
            (
                interrupted["state"], interrupted["terminal_result"],
                interrupted["charge_micro_usd"], interrupted["version"],
                receipt["accounting_state"], receipt["charged_usd"],
                terminal["state"], terminal["terminal_result"],
                terminal["charge_micro_usd"], terminal["version"],
                manifest["task_submitted"],
                manifest["submitted_at_epoch_ns"],
            ),
            (
                "terminal", "cancelled", 2_000_000, 5,
                "cancelled_conservative", "2.00", "terminal", "cancelled",
                2_000_000, 5, "1", "102999999999",
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
